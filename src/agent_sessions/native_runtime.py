"""Web-side native runtime: contained worker lifecycle and the structured adapter (#1278).

The web process never speaks a native protocol itself. It reserves ownership, writes the one
private worker config, launches the fixed systemd service, pins its invocation, and then talks to
the worker over the private socket. Reads (snapshot/events) come straight from the durable
journal, so a browser or web restart observes everything without the worker's help.
Response start times come from the first recorded non-empty text, never observation time.
Background task display metadata is folded only from the live worker's journal observations;
it never supplies a turn, an interrupt target or a dispatch decision.

Lifecycle facts this module relies on (see ``docs/invariants/native-runtime.md``):

* One session record names one CURRENT generation and a closed flag, changed only under the
  per-session lifecycle lock. Launch holds that lock until the invocation is pinned, so a closer
  holding it observes "closed and drained" (``classify(..., start_closed=True)``).
* A generation is launched once. A failed or ambiguous launch is never retried under its id.
* A worker that is gone is replaced by a NEW generation (native resume) only when a turn is
  submitted; an unknown one refuses until stopped. Nothing is resent automatically.
* Creation reserves ownership first. Claude's history id is chosen here and bound before any
  process exists; a Codex thread is bound by its worker before it can accept a turn. An
  unresolved creation is discharged only after its containment is proved gone with no turn.
* IPC failures after a request write begins are uncertain (503), never definite non-application.
  Callers retain the original operation identity and recover its durable receipt.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import (
    chat_store,
    model_choice,
    native_attachments,
    native_containment,
    native_grants,
    native_ipc,
    native_journal,
    native_ownership,
    native_protocol,
    native_questions,
    native_state,
    risk_marks,
)
from .plugins import api_source, kinds, storage

READY_TIMEOUT = 120.0
SHUTTING_DOWN = "the native worker is shutting down; retry"
WORKER_IDLE_TIMEOUT = 1800.0  # an idle worker exits; the next turn resumes the native history
STOP_TIMEOUT = 30.0
IPC_TIMEOUT = 30.0
SNAPSHOT_TEXT = 200_000
_VERSION = re.compile(rb"(\d+)\.(\d+)\.(\d+)")
_FLOORS = {
    "codex-app-server": native_protocol.CODEX_MIN_VERSION,
    "claude-stream-json": native_protocol.CLAUDE_MIN_VERSION,
    "opencode-acp": native_protocol.OPENCODE_MIN_VERSION,
}
# The vendor CLI writes where ITS defaults point. A BattleLab store override that the vendor
# would not also use cannot be honoured without silently reading one history and writing another.
# (store path name or None for the root, the vendor's default under HOME). opencode's history is
# one SQLite file, which its console provider resolves through `store.path_env` (#1312).
_VENDOR_STORES = {
    "codex-app-server": (None, ".codex/sessions"),
    "claude-stream-json": (None, ".claude"),
    "opencode-acp": ("db", ".local/share/opencode/opencode.db"),
}
#: Adapters whose native history id is minted by the agent and bound by the worker from its
#: creation response (Codex `thread/start`, opencode `session/new`); Claude's is ours.
_ADOPTED = frozenset({"codex-app-server", "opencode-acp"})
_LEASES = "native-leases"
_LEASE_TOKEN = "native-leases"  # noqa: S105 — a marker the installer must contain, not a secret
_ERRORS = {
    "invalid": 422,
    "unsupported": 409,
    "conflict": 409,
    "stale": 409,
    "busy": 409,
    "unavailable": 503,
    "unauthorized": 403,
}


class NativeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- systemd and the host: one seam so tests can substitute a fake service manager -------------


class Host:
    """Real systemd user-manager operations. Every call is a literal argv, never a shell."""

    def runtime_dir(self) -> str:
        return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

    def available(self) -> str | None:
        runtime = self.runtime_dir()
        if not os.path.exists("/usr/bin/systemd-run") or not os.path.exists("/usr/bin/systemctl"):
            return "systemd is not installed; native clients need a systemd user manager"
        if not os.path.exists(os.path.join(runtime, "systemd", "private")):
            return "no systemd user manager is running for this account"
        if not os.path.isdir("/sys/fs/cgroup") or not os.path.exists(
            "/sys/fs/cgroup/cgroup.controllers"
        ):
            return "native containment needs the unified cgroup v2 hierarchy"
        return None

    def _env(self) -> dict[str, str]:
        runtime = self.runtime_dir()
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            "LANG": "C.UTF-8",
        }

    def run(self, argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 — literal reviewed argv, no shell
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
            stdin=subprocess.DEVNULL,
        )

    def launch(self, argv: list[str]) -> bool:
        try:
            return self.run(argv, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False  # ambiguous: the caller pins evidence or marks the generation unknown

    def show(self, worker: native_containment.WorkerIdentity):
        try:
            done = self.run(native_containment.show_argv(worker))
            return native_containment.parse_show(worker, done.stdout, returncode=done.returncode)
        except (OSError, subprocess.SubprocessError, native_containment.ContainmentError):
            return None

    def cgroup(self, control_group: str) -> native_containment.CgroupObservation:
        directory = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        try:
            text = (directory / "cgroup.events").read_text()
        except FileNotFoundError:
            try:
                os.stat(directory)
            except FileNotFoundError:
                return native_containment.CgroupObservation(control_group, "absent")
            except OSError:
                pass
            return native_containment.CgroupObservation(control_group, "unknown")
        except OSError:
            return native_containment.CgroupObservation(control_group, "unknown")
        try:
            return native_containment.parse_cgroup_events(control_group, text)
        except native_containment.ContainmentError:
            return native_containment.CgroupObservation(control_group, "unknown")

    def stop(self, argv: list[str]) -> bool:
        try:
            return self.run(argv, timeout=STOP_TIMEOUT).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def worker_running(self, worker: native_containment.WorkerIdentity, pid: object) -> bool:
        """Cheap observation for snapshots: that exact pid still runs inside its own unit.

        Not lifecycle evidence (``classify`` is); a reused pid in another cgroup reads dead.
        """
        if type(pid) is not int or pid <= 1:
            return False
        try:
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as fh:
                return any(line.rstrip("\n").endswith("/" + worker.unit) for line in fh)
        except OSError:
            return False


HOST = Host()


# --- readiness --------------------------------------------------------------------------------

_VERSIONS: dict[tuple, tuple[int, int, int] | None] = {}


def native_version(binary: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(binary)
    except OSError:
        return None
    key = (binary, st.st_ino, st.st_size, st.st_mtime_ns)
    if key not in _VERSIONS:
        try:
            done = subprocess.run(  # noqa: S603 — admitted binary, literal argv
                [binary, "--version"],
                check=False,
                capture_output=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
                env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            found = _VERSION.search(done.stdout or b"")
            _VERSIONS[key] = tuple(int(x) for x in found.groups()) if found else None
        except (OSError, subprocess.SubprocessError):
            _VERSIONS[key] = None
    return _VERSIONS[key]


def interpreter() -> tuple[str, Path | None]:
    """The exact installed interpreter for the worker, and the release directory to lease."""
    venv = Path(sys.prefix).resolve()
    release = venv.parent if venv.parent.parent.name == "releases" else None
    return str(venv / "bin" / "python"), release


def _installer_honours_leases(release: Path) -> bool:
    installer = release.parent.parent / "current" / "src" / "install.sh"
    try:
        return _LEASE_TOKEN in installer.read_text(errors="replace")
    except OSError:
        return False


def _vendor_store_matches(adapter: str, source) -> bool:
    try:
        name, default = _VENDOR_STORES[adapter]
        root = source.store_root() if name is None else source.store_path(name)
        expected = Path.home() / default
        return root is not None and Path(root).resolve() == expected.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def readiness(prov) -> tuple[bool, str | None]:
    """Live, local facts only; never a network probe and never an authorization grant."""
    adapter = prov.manifest.api.kind
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        return False, str(exc)
    reason = HOST.available()
    if reason:
        return False, reason
    try:
        binary = binding.source.entrypoint_path()
    except Exception as exc:  # noqa: BLE001 — EngineError/provenance: a refusal reason
        return False, str(exc)
    version = native_version(binary)
    if version is None or version < _FLOORS[adapter]:
        floor = ".".join(map(str, _FLOORS[adapter]))
        return False, f"{binding.source.engine_id} {floor} or later is required for native mode"
    if not _vendor_store_matches(adapter, binding.source):
        return False, "the agent's history store is overridden; native mode would write elsewhere"
    _python, release = interpreter()
    if release is not None and not _installer_honours_leases(release):
        return False, "the installed updater cannot retain a release for a running worker"
    return True, None


# --- release retention ------------------------------------------------------------------------


def _lease(release: Path | None, worker_id: str, *, remove: bool = False) -> None:
    try:
        native_state.lease(release, worker_id, remove=remove)
    except native_state.StateError as exc:
        raise NativeError(503, str(exc)) from None


def _proved_gone(worker_id: str) -> bool:
    """Pinned evidence only: an unpinned (intent / ambiguous) generation is never "gone" here."""
    identity = _identity(worker_id)
    if identity is None:
        return False
    observation = HOST.show(identity.worker)
    if observation is None or observation.active_state not in {"inactive", "failed"}:
        return False
    if observation.load_state == "loaded" and observation.invocation_id not in (
        None,
        identity.invocation_id,
    ):
        return False
    return HOST.cgroup(identity.control_group).state in {"absent", "empty"}


def _sweep_leases(release: Path | None) -> None:
    """Drop leases of generations whose pinned processes provably no longer run, in EVERY release.

    Workers drop their own lease on a clean exit; this catches crashes and kills. A lease whose
    generation has no pinned invocation yet (being launched right now by another session) is
    never touched.
    """
    if release is None:
        return
    with contextlib.suppress(OSError):
        for leases in release.parent.glob(f"*/{native_state.LEASES}"):
            for entry in leases.iterdir():
                try:
                    worker = native_containment.WorkerIdentity(entry.name)
                except native_containment.ContainmentError:
                    continue
                if _proved_gone(worker.worker_id):
                    _lease(leases.parent, worker.worker_id, remove=True)


# --- providers --------------------------------------------------------------------------------


def _api_provider(engine_id: str, *, retiring_ok: bool = False):
    """New work needs a live provider; stop/probe of an admitted worker must survive retirement."""
    from . import engines

    prov = engines.get(engine_id)
    if prov is None and retiring_ok:
        prov = engines.get_any(engine_id)
    if (
        prov is None
        or (engines.is_retiring(prov) and not retiring_ok)
        or prov.manifest.runtime != "api"
        or prov.manifest.api is None
        or prov.manifest.api.kind not in _FLOORS
    ):
        raise NativeError(404, "unknown or removed native client")
    return prov


def _journal_root(prov) -> Path:
    root = prov.store_root()
    if root is None:
        raise NativeError(500, "this client has no conversation store")
    return Path(root)


def _session(engine_id: str, session_id: str, *, retiring_ok: bool = False) -> tuple:
    """The provider and session record, proving the record belongs to THIS client.

    Sessions are stored by UUID; a key composed from another client's engine and this UUID must
    never reach the record (Hermes on #1278): every read and mutation checks the stored key,
    adapter and source against the client named in the request.
    """
    prov = _api_provider(engine_id, retiring_ok=retiring_ok)
    record = native_state.read_session(session_id)
    if (
        record is None
        or record.get("session_key") != f"{engine_id}:{session_id}"
        or record.get("adapter") != prov.manifest.api.kind
        or record.get("source_engine") != prov.manifest.api.source
    ):
        raise NativeError(404, "no such native session")
    return prov, record


# --- generations ------------------------------------------------------------------------------


def _permission_change(record: dict) -> dict:
    """A one-way, operator-bound mode change, separate from immutable creation identity."""
    change = record.get("permission_change")
    if change is None:
        return {}
    if (
        not isinstance(change, dict)
        or change.get("state") not in {"stopping", "enabled"}
        or not isinstance(change.get("actor"), str)
        or not change["actor"]
        or len(change["actor"]) > 128
    ):
        raise NativeError(503, "the session permission change is unreadable")
    try:
        native_state._uuid(change.get("operation_id"), "permission operation")
    except native_state.StateError:
        raise NativeError(503, "the session permission change is unreadable") from None
    return change


def _effective_bypass(record: dict) -> bool:
    change = _permission_change(record)
    return record["request"].get("bypass") is True or change.get("state") == "enabled"


@dataclass(frozen=True)
class Generation:
    session_id: str
    worker_id: str
    config: dict

    @property
    def binding(self) -> native_ipc.Binding:
        return native_ipc.Binding(
            self.config["session_key"],
            self.worker_id,
            self.config["connection_id"],
            self.config["adapter"],
        )


def _entrypoint(source) -> str:
    try:
        return source.entrypoint_path()
    except Exception as exc:  # noqa: BLE001 — EngineError/provenance refusal
        raise NativeError(409, str(exc)) from None


def _server_env() -> dict[str, str]:
    """Path settings the worker needs to resolve the SAME shared state; no secrets."""
    keep = {}
    for key, value in os.environ.items():
        if not key.startswith("AGENT_SESSIONS_"):
            continue
        if any(word in key for word in ("SECRET", "PASSWORD", "TOKEN", "KEY", "HASH", "TOTP")):
            continue
        keep[key] = value
    return keep


def _launch_locked(session_id: str, record: dict, prov, binding, *, mode: str) -> str:
    """Mint, persist intent, lease, launch and pin one generation. Caller holds the lock."""
    if _permission_change(record).get("state") == "stopping":
        raise NativeError(409, "finish enabling Skip permissions before sending another message")
    if _start_state(record) not in (None, STARTING, STARTED):
        # Two-phase skip start (#1339; state machine, operator decision on #1341): a skip
        # creation's generation is born only from `starting` (the `/start` that set it, under this
        # same lock) or `started`. Fail closed here, where every generation is born.
        raise NativeError(409, "this session has not been started; start or discard it")
    from . import memory, resource_limits

    policy = resource_limits.values()
    worker = native_containment.WorkerIdentity.mint()
    python, release = interpreter()
    state_dir = native_state.root()
    runtime = HOST.runtime_dir()
    adapter = record["adapter"]
    config = {
        "version": 1,
        "worker_id": worker.worker_id,
        "connection_id": str(uuid.uuid4()),
        "capability": native_ipc.Capability.create()._value,
        "session_key": record["session_key"],
        "adapter": adapter,
        "source_engine": record["source_engine"],
        "binary": _entrypoint(binding.source),
        "cwd": record["request"]["cwd"],
        "model": record["request"]["model"],
        # The requested level (#1407), read from the recorded request on EVERY generation, like
        # the model: a resume or a relaunch sends the level the session was created with.
        "effort": record["request"].get("effort"),
        # Persisted mode on every generation; only the operator's explicit transition changes it.
        "bypass": _effective_bypass(record),
        "mode": mode,
        "native_id": record.get("native_id"),
        "journal_root": str(_journal_root(prov)),
        "socket": f"{runtime}/agent-sessions-native/{worker.worker_id.replace('-', '')}.sock",
        "home": str(Path.home()),
        "path": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "user": os.environ.get("USER"),
        "logname": os.environ.get("LOGNAME"),
        "server_env": _server_env(),
        "thread_environment": resource_limits.thread_environment(
            os.environ, policy["library_threads"]
        ),
        "idle_timeout": WORKER_IDLE_TIMEOUT,
        # Shared project memory (#1441), bound per GENERATION: every resume or relaunch is a new
        # agent process and asks again, so a toggle reaches the next generation, never a live one.
        "memory_env": memory.launch_env(
            record["session_key"], record["request"]["cwd"], kind="api"
        ),
    }
    if adapter == "opencode-acp":
        # The store the source resolves (`store.path_env` first): the worker's maintenance
        # admission is keyed by this database, like compaction's (Hermes on #1336).
        database = binding.source.store_path("db")
        if database is None:
            raise NativeError(409, "the agent's history store has no database")
        config["store_database"] = str(Path(database).resolve(strict=False))
    if adapter in _ADOPTED and mode == "create":
        config["creation"] = {
            "operation_id": session_id,
            "owner_token": record["owner_token"],
        }
    native_state.write_config(worker.worker_id, config)
    native_state.update_lifecycle(
        worker.worker_id,
        phase="intent",
        unit=worker.unit,
        release=None if release is None else str(release),
        intent_at=time.time(),
    )
    record["current_worker"] = worker.worker_id
    record["closed"] = False
    # `workers` lists every generation not yet PROVED gone; a successor needs that list empty.
    record.setdefault("workers", []).append(worker.worker_id)
    record["generations"] = record.get("generations", 0) + 1
    native_state.write_session(session_id, record)  # durable startup intent BEFORE launch
    _sweep_leases(release)
    _lease(release, worker.worker_id)
    argv = native_containment.launch_argv(
        worker,
        python=python,
        state_dir=str(state_dir),
        home=str(Path.home()),
        runtime_dir=runtime if re.fullmatch(r"/run/user/[0-9]{1,10}", runtime) else None,
        tasks_max=policy["api_tasks"],
        memory_gib=policy["api_memory_gib"],
    )
    launched = HOST.launch(argv)
    observation = HOST.show(worker)
    if observation is None or observation.load_state != "loaded" or not observation.invocation_id:
        native_state.update_lifecycle(worker.worker_id, phase="launch_unknown", launched=launched)
        raise NativeError(503, "the native worker could not be started; stop the session")
    try:
        identity = native_containment.capture(worker, observation)
    except native_containment.ContainmentError:
        native_state.update_lifecycle(worker.worker_id, phase="launch_unknown", launched=launched)
        raise NativeError(503, "the native worker could not be started; stop the session") from None
    native_state.update_lifecycle(
        worker.worker_id,
        invocation_id=identity.invocation_id,
        control_group=identity.control_group,
        launched_at=time.time(),
    )
    return worker.worker_id


def _identity(worker_id: str) -> native_containment.InvocationIdentity | None:
    life = native_state.read_lifecycle(worker_id)
    try:
        return native_containment.InvocationIdentity(
            native_containment.WorkerIdentity(worker_id),
            life.get("invocation_id"),
            life.get("control_group"),
        )
    except native_containment.ContainmentError:
        return None


def classify(session_id: str, worker_id: str) -> str:
    """live / gone / unknown for one generation. Call under the session lock for "gone"."""
    identity = _identity(worker_id)
    if identity is None:
        return "unknown"
    record = native_state.read_session(session_id) or {}
    closed = record.get("current_worker") != worker_id or record.get("closed") is True
    return native_containment.classify(
        identity,
        HOST.show(identity.worker),
        HOST.cgroup(identity.control_group),
        start_closed=closed,
    )


async def _wait_ready(worker_id: str) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT
    worker = native_containment.WorkerIdentity(worker_id)
    checked = time.monotonic()
    while time.monotonic() < deadline:
        life = await asyncio.to_thread(native_state.read_lifecycle, worker_id)
        if life.get("phase") == "ready":
            return life
        if life.get("phase") in {"exited", "refused", "launch_unknown"}:
            raise NativeError(
                503, f"the native worker stopped: {life.get('reason') or life.get('phase')}"
            )
        if time.monotonic() - checked >= 1:
            # A worker that died before reporting (e.g. refused its config) leaves no phase.
            checked = time.monotonic()
            observation = await asyncio.to_thread(HOST.show, worker)
            if observation is not None and observation.active_state in {"inactive", "failed"}:
                raise NativeError(503, "the native worker exited before it became ready")
        await asyncio.sleep(0.1)
    raise NativeError(503, "the native worker did not become ready")


# --- private IPC client -----------------------------------------------------------------------


async def _call(gen: Generation, action: str, params: dict, *, timeout: float = IPC_TIMEOUT):
    binding = gen.binding
    request = native_ipc.validate_request(
        {
            **binding.envelope("request"),
            "request_id": str(uuid.uuid4()),
            "action": action,
            "params": params,
        }
    )
    capability = native_ipc.Capability(gen.config["capability"])
    request_started = False

    async def exchange():
        nonlocal request_started
        reader, writer = await asyncio.open_unix_connection(
            gen.config["socket"], limit=native_ipc.MAX_FRAME_BYTES + 1
        )
        try:
            writer.write(native_ipc.encode_handshake(binding, capability).data)
            await writer.drain()
            native_ipc.decode_welcome(await reader.readline(), expected=binding)
            frame = native_ipc.encode_request(binding, request["request_id"], action, params)
            request_started = True  # Even a failed/partial write may reach the worker.
            writer.write(frame)
            await writer.drain()
            return native_ipc.decode_response(await reader.readline(), request=request)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    try:
        response = await asyncio.wait_for(exchange(), timeout)
    except (OSError, ConnectionError) as exc:
        if request_started:
            raise NativeError(
                503, "the native worker connection was lost; the request may still be applied"
            ) from None
        raise _Unreachable(str(exc)) from None
    except native_ipc.IPCError:
        raise NativeError(503, "the native worker answered with an invalid frame") from None
    except TimeoutError:
        raise NativeError(
            503, "the native worker did not answer; the request may still be applied"
        ) from None
    if "error" in response:
        error = response["error"]
        raise NativeError(_ERRORS.get(error["code"], 503), error["message"])
    return response["result"]


class _Unreachable(Exception):
    pass


def engine_may_have_workers(engine_id: str) -> bool:
    """Could any worker of ``engine_id``'s sessions still be running? (#1311, Hermes on #1315)

    Retirement asks this for a removed API client: dropping its provider would make stop,
    probe and history unreachable. Only a generation recorded ``gone`` (cgroup-proved) counts as
    over; an unreadable state directory or record answers True. Both the session records and the
    worker directories are read, so a lost session record cannot hide a running worker. Local
    files only."""
    prefix = f"{engine_id}:"
    try:
        paths = list(native_state.root().glob("*.json"))
    except OSError:
        return True
    for path in paths:
        try:
            record = native_state.read(path)
        except (OSError, ValueError, native_state.StateError):
            return True
        if not isinstance(record, dict):
            return True
        if not str(record.get("session_key", "")).startswith(prefix):
            continue
        workers = {w for w in record.get("workers") or [] if isinstance(w, str)}
        if record.get("current_worker"):
            workers.add(record["current_worker"])
        for worker in workers:
            try:
                phase = native_state.read_lifecycle(worker).get("phase")
            except (OSError, ValueError, native_state.StateError):
                return True
            if phase != "gone":
                return True
    # Session records can be lost while their worker runs (Hermes on #1315): the WORKER side is
    # read too. Every worker not recorded gone that belongs to this client — or whose owner can no
    # longer be read — keeps it reachable. An empty session listing is never proof of no worker.
    try:
        worker_dirs = [d for d in (native_state.root() / "workers").iterdir() if d.is_dir()]
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for wdir in worker_dirs:
        try:
            if str(uuid.UUID(wdir.name)) != wdir.name:
                continue  # not a worker directory
        except ValueError:
            continue
        try:
            phase = native_state.read_lifecycle(wdir.name).get("phase")
            if phase == "gone":
                continue
            config = native_state.read_config(wdir.name)
        except (OSError, ValueError, native_state.StateError):
            return True
        if not isinstance(config, dict) or not isinstance(config.get("session_key"), str):
            return True  # a running worker nobody can attribute: keep every API client reachable
        if config["session_key"].startswith(prefix):
            return True
    return False


def _generation(session_id: str) -> Generation | None:
    record = native_state.read_session(session_id)
    if record is None or not record.get("current_worker") or record.get("closed"):
        return None
    worker_id = record["current_worker"]
    config = native_state.read_config(worker_id)
    if config is None:
        return None
    return Generation(session_id, worker_id, config)


# --- creation ---------------------------------------------------------------------------------


def _validate_create(cwd: object, model: object) -> tuple[str, str | None]:
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\x00" in cwd:
        raise NativeError(422, "cwd must be an absolute path")
    if os.path.normpath(cwd) != cwd or "%" in cwd:
        raise NativeError(422, "cwd must be a normalized path")
    if model in (None, "default"):
        model = None
    elif not isinstance(model, str) or native_protocol._MODEL.fullmatch(model) is None:
        raise NativeError(422, "model must be a model identifier")
    return cwd, model


def _create_sync(
    engine_id: str,
    session_id: str,
    cwd: str,
    model: str | None,
    bypass: bool = False,
    effort: str | None = None,
) -> bool:
    """Returns True when a generation must be awaited (a new launch happened).

    ``bypass`` (#1339) is part of the immutable create request: recorded only when set, so a
    guarded request is byte-identical to one written before the field existed, and a replay of
    the same operation id with the other mode conflicts instead of flipping it. ``effort`` (#1407)
    follows the same rule: recorded only when chosen, and part of the request a replay compares."""
    prov = _api_provider(engine_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    adapter = prov.manifest.api.kind
    if bypass and adapter not in kinds.API_BYPASS_KINDS:
        raise NativeError(422, "this client cannot skip permission prompts yet")
    session_key = f"{engine_id}:{session_id}"
    # The model is checked against the client's own list NOW, at the write boundary (#1313): an id
    # the client does not list is refused before anything exists. A refusal only blocks a NEW
    # creation; an exact replay compares the request as sent.
    refused = None
    try:
        model = model_choice.select_api(prov, model)
        effort = model_choice.select_api_effort(prov, model, effort)
    except model_choice.ModelRefused as exc:
        refused = exc
    request = {"cwd": cwd, "model": model, "adapter": adapter}
    if effort is not None:
        request["effort"] = effort
    if bypass:
        request["bypass"] = True
    root = _journal_root(prov)
    with native_state.session_lock(session_id):
        record = native_state.read_session(session_id)
        if record is not None:
            if record["request"] != request or record["session_key"] != session_key:
                raise NativeError(409, "operation id already created a different session")
            if record.get("closed") and not record.get("generations"):
                # A creation stopped before it ever launched is terminal (Hermes on #1278): its
                # replay must not reserve and launch after `stop` reported it gone.
                raise NativeError(409, "this creation was stopped; use a new operation id")
            if record.get("generations"):
                if record.get("native_id") is None and _live_worker(session_id) is None:
                    raise NativeError(409, "this creation did not complete; use a new operation id")
                return False  # exact replay: observe, never launch a second creation
        else:
            if refused is not None:
                raise NativeError(422, refused.detail)
            ready, reason = readiness(prov)
            if not ready:
                raise NativeError(409, reason or "native client is not ready")
            if not os.path.isdir(cwd):
                raise NativeError(422, "cwd is not a directory")
            record = {
                "version": 1,
                "session_key": session_key,
                "adapter": adapter,
                "source_engine": binding.source.engine_id,
                "request": request,
                "owner_token": str(uuid.uuid4()),
                "native_id": str(uuid.uuid4()) if adapter == "claude-stream-json" else None,
                "current_worker": None,
                "closed": False,
                "workers": [],
                "created_at": time.time(),
            }
            if bypass:
                record["start"] = {
                    "state": PENDING,
                    "gen": None,
                    "expires_at": time.time() + PENDING_START_TTL,
                }
            native_state.write_session(session_id, record)
        try:
            chat_store.create(
                root,
                session_id,
                cwd=cwd,
                request={"runtime": "api", "session_key": session_key},
                model=model,
            )
        except FileExistsError:
            native_journal.read(root, session_id)  # an existing header must be ours, intact
        if record["request"].get("bypass"):
            # A skip creation reserves and launches NOTHING from create — not the first call, not
            # a replay, not after an interrupted start (Hermes 5908): only `start` does (#1339).
            return False
        _reserve_and_launch_locked(session_id, record, prov, binding)
        return True


def _reserve_and_launch_locked(
    session_id: str, record: dict, prov, binding, *, mode: str = "create"
) -> str:
    """Commit ownership and launch the FIRST generation of a creation (or a retried skip start's
    successor). Caller holds the lock."""
    from .engines import registry

    session_key = record["session_key"]
    source = native_ownership.source_identity(binding.source)
    if source is None:
        raise NativeError(409, "the native source has no recognised history store")
    with storage.locked("launch", wait=30):
        if not registry.admits(binding.source) or not api_source.admits(binding):
            raise NativeError(409, "the agent changed or was removed")
        native_ownership.reserve(
            session_key,
            source,
            operation_id=session_id,
            request=dict(record["request"]),
            owner_token=record["owner_token"],
        )
        if record["native_id"] is not None:
            # Claude: the id is ours, so permanent exclusion commits before any process.
            native_ownership.bind(
                session_key,
                operation_id=session_id,
                owner_token=record["owner_token"],
                native_id=record["native_id"],
            )
    return _launch_locked(session_id, record, prov, binding, mode=mode)


async def create(
    engine_id: str,
    cwd: str,
    *,
    session_id: str,
    model: str | None = None,
    effort: str | None = None,
    bypass: bool = False,
    execution_admission=None,
) -> str:
    if not isinstance(bypass, bool):
        raise NativeError(422, "bypass must be a boolean")
    if effort is not None and (
        not isinstance(effort, str) or native_protocol._EFFORT.fullmatch(effort) is None
    ):
        raise NativeError(422, "effort must be a level identifier")
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    cwd, model = _validate_create(cwd, model)
    try:
        launched = await asyncio.to_thread(
            _create_sync, engine_id, session_id, cwd, model, bypass, effort
        )
    except (native_state.StateError, native_ownership.OwnershipError, storage.StateError) as exc:
        raise NativeError(503, str(exc)) from None
    except native_journal.JournalError as exc:
        raise NativeError(503, exc.detail) from None
    if launched:
        gen = await asyncio.to_thread(_generation, session_id)
        if gen is not None:
            try:
                await _wait_ready(gen.worker_id)
            except NativeError:
                # A creation that never became ready must not keep its reservation (which
                # holds new console launches for the source) — stop proves it gone first.
                with contextlib.suppress(Exception):
                    await stop(engine_id, session_id)
                raise
    return session_id


# --- start (the second phase of a skip-permissions creation, #1339) ---------------------------

#: How long a skip creation waits for its `start` before it is closed, never launched.
PENDING_START_TTL = 600.0


# The skip-start state machine (#1339; operator decision on #1341 after review 5926). ONE field,
# `record["start"] = {"state", "gen", "expires_at"}`, only on `bypass` creations; every transition
# is a compare-and-set under the session lock, and every launch path is gated on it:
#
#   pending ──/start──▶ starting(gen W) ──W ready (CAS state+gen)──▶ started
#      │                    │  W fails (CAS) ──▶ failed ──/start──▶ starting(W') …
#      ├─expiry─▶ expired   └──────── Discard (from pending/starting/failed) ──▶ discarded
#
# `expired` and `discarded` are terminal: nothing leaves them. A completion or failure that
# arrives for a generation that is no longer the starting one (Discard won the race, a newer
# start replaced it) is a no-op. Only `starting` (inside the `/start` that set it) and `started`
# may launch a generation; a turn needs `started`.
PENDING, STARTING, STARTED, FAILED = "pending", "starting", "started", "failed"
DISCARDED, EXPIRED = "discarded", "expired"


def _start_state(record: dict, now: float | None = None) -> str | None:
    """The skip start's state (expiry applied), or None for a guarded creation."""
    if not record["request"].get("bypass"):
        return None
    start = record.get("start") or {}
    state = start.get("state")
    if state not in {PENDING, STARTING, STARTED, FAILED, DISCARDED, EXPIRED}:
        return DISCARDED  # an unreadable state never launches anything
    if state == PENDING:
        expires = start.get("expires_at")
        if not isinstance(expires, int | float) or (now or time.time()) > expires:
            return EXPIRED
    return state


def _set_start(record: dict, state: str, gen: str | None = None) -> None:
    record["start"] = {**(record.get("start") or {}), "state": state, "gen": gen, "at": time.time()}


def pending_start_view(record: dict) -> dict:
    """What a snapshot says about a skip creation's start."""
    state = _start_state(record)
    if state is None or state in {STARTED, DISCARDED}:
        return {"pending_start": False}
    if state == EXPIRED:
        return {"pending_start": False, "start_expired": True}
    if state == PENDING:
        return {"pending_start": True, "start_expires_at": record["start"]["expires_at"]}
    return {"pending_start": True, "start_incomplete": True}  # starting / failed: Start reconciles


def _start_sync(engine_id: str, session_id: str) -> str | None:
    """The worker id this call launched, or None when the start is already complete."""
    prov, _ = _session(engine_id, session_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    with native_state.session_lock(session_id):
        _, record = _session(engine_id, session_id)
        state = _start_state(record)
        if state is None:
            if record.get("generations"):
                return None  # a guarded creation has nothing to start: observe
            raise NativeError(409, "this session has nothing to start")
        if state == STARTED:
            return None  # a retried start only observes
        if state == DISCARDED:
            raise NativeError(409, "this creation was discarded; use a new operation id")
        if state == EXPIRED:
            if record["start"].get("state") != EXPIRED:
                _set_start(record, EXPIRED)
                native_state.write_session(session_id, record)
            raise NativeError(410, "this start expired; start a new session")
        if state == STARTING:
            gen = record["start"].get("gen")
            if gen is not None and _live_worker(session_id) == gen:
                if native_state.read_lifecycle(gen).get("phase") == "ready":
                    # It came up; the process that would have recorded it died. Record it now.
                    _set_start(record, STARTED, gen)
                    native_state.write_session(session_id, record)
                    return None
                raise NativeError(409, "this session is starting; wait for it")
            # starting, but its worker is gone: treat as failed and start again below
        # pending, failed, or a dead starting: (re)launch. Checked on EVERY launch (Hermes 5913).
        if binding.source.engine_id != record["source_engine"]:
            raise NativeError(409, "the session's original native source is not available")
        ready, reason = readiness(prov)
        if not ready:
            raise NativeError(409, reason or "native client is not ready")
        mode = "create"
        if record.get("generations"):
            # A retry after a failed launch: every earlier generation must be PROVED gone first.
            if _settle_generations(session_id, record):
                raise NativeError(
                    409, "the previous start's worker is not proved gone; discard and start anew"
                )
            if record.get("native_id") is None:
                intent = native_ownership.lookup(record["session_key"])
                if intent is not None and intent.native_id is not None:
                    record["native_id"] = intent.native_id
            if record["adapter"] == "codex-app-server" and record.get("native_id"):
                mode = "resume"
            elif record["adapter"] == "claude-stream-json" and _claude_history_exists(
                binding.source, record["native_id"]
            ):
                mode = "resume"
        # The operator's start, durable BEFORE anything is reserved or launched.
        _set_start(record, STARTING, None)
        native_state.write_session(session_id, record)
        try:
            worker_id = _reserve_and_launch_locked(session_id, record, prov, binding, mode=mode)
        except Exception:
            _set_start(record, FAILED, None)
            native_state.write_session(session_id, record)
            raise
        _set_start(record, STARTING, worker_id)
        native_state.write_session(session_id, record)
        return worker_id


def _finish_start(session_id: str, worker_id: str, ok: bool) -> bool:
    """CAS `starting(worker_id)` → started / failed. False when the state moved on meanwhile
    (Discard won the race, or a newer start replaced this generation): then this is a no-op."""
    with native_state.session_lock(session_id):
        record = native_state.read_session(session_id)
        if record is None:
            return False
        start = record.get("start") or {}
        if start.get("state") != STARTING or start.get("gen") != worker_id:
            return False
        if ok and _live_worker(session_id) != worker_id:
            ok = False  # it reported ready, then vanished before this landed
        _set_start(record, STARTED if ok else FAILED, worker_id if ok else None)
        native_state.write_session(session_id, record)
        return ok


async def start(engine_id: str, session_id: str) -> str:
    try:
        worker_id = await asyncio.to_thread(_start_sync, engine_id, session_id)
    except (native_state.StateError, native_ownership.OwnershipError, storage.StateError) as exc:
        raise NativeError(503, str(exc)) from None
    if worker_id is None:
        return session_id
    try:
        await _wait_ready(worker_id)
    except NativeError:
        # Contain it (internal: NOT a discard), then fail the start only if still ours.
        with contextlib.suppress(Exception):
            await stop(engine_id, session_id, discard=False)
        await asyncio.to_thread(_finish_start, session_id, worker_id, False)
        raise
    if not await asyncio.to_thread(_finish_start, session_id, worker_id, True):
        raise NativeError(409, "this start was discarded or replaced before it completed")
    return session_id


# --- resume (a new generation for a session whose worker is proved gone) ----------------------


def _settle_generations(session_id: str, record: dict) -> list[str]:
    """Under the lock: forget generations proved gone; return those that are not (yet)."""
    remaining = []
    for worker_id in record.get("workers", []):
        if _gone(session_id, worker_id):
            life = native_state.read_lifecycle(worker_id)
            _lease(Path(life["release"]) if life.get("release") else None, worker_id, remove=True)
            native_state.forget_config(worker_id)
            native_state.update_lifecycle(worker_id, phase="gone", gone_at=time.time())
        else:
            remaining.append(worker_id)
    record["workers"] = remaining
    native_state.write_session(session_id, record)
    return remaining


def _claude_history_exists(source, native_id: str) -> bool:
    root = source.store_root()
    return root is not None and any(Path(root).glob(f"projects/*/{native_id}.jsonl"))


def _relaunch_sync(engine_id: str, session_id: str, permission_operation: str | None) -> str | None:
    prov, _ = _session(engine_id, session_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    with native_state.session_lock(session_id):
        _, record = _session(engine_id, session_id)
        if _permission_change(record).get("operation_id") != permission_operation:
            raise NativeError(
                409, "permissions changed while sending; review the session and send again"
            )
        if _start_state(record) not in (None, STARTED):
            # Refuse BEFORE touching the record: a turn must neither launch a skip creation that
            # `start` has not launched, nor close it as a side effect (#1339, Hermes 5908).
            raise NativeError(409, "this session has not been started; start or discard it")
        if binding.source.engine_id != record["source_engine"]:
            raise NativeError(409, "the session's original native source is not available")
        current = record.get("current_worker")
        if current and not record.get("closed") and classify(session_id, current) == "live":
            return None  # someone else relaunched it; use it
        # Close the gate, then require EVERY earlier generation proved gone (exact pinned
        # invocation, empty cgroup) before a successor may own the native history.
        record["closed"] = True
        native_state.write_session(session_id, record)
        if _settle_generations(session_id, record):
            raise NativeError(
                409, "a previous native worker's state is unknown; stop the session first"
            )
        if record.get("native_id") is None:
            # A worker may have committed ownership and died before recording it here.
            intent = native_ownership.lookup(record["session_key"])
            if intent is not None and intent.native_id is not None:
                record["native_id"] = intent.native_id
                native_state.write_session(session_id, record)
        if record.get("native_id") is None:
            raise NativeError(409, "this session's native history was never created; stop it")
        ready, reason = readiness(prov)
        if not ready:
            raise NativeError(409, reason or "native client is not ready")
        mode = "resume"
        if record["adapter"] == "claude-stream-json" and not _claude_history_exists(
            binding.source, record["native_id"]
        ):
            # Claude writes no history until a first turn: --resume of an id it never wrote
            # fails, while --session-id of one it did collides. Ask the store, not the journal.
            mode = "create"
        return _launch_locked(session_id, record, prov, binding, mode=mode)


async def _live_generation(
    engine_id: str, session_id: str, permission_operation: str | None
) -> Generation:
    record = await asyncio.to_thread(native_state.read_session, session_id)
    if record and _permission_change(record).get("state") == "stopping":
        raise NativeError(409, "finish enabling Skip permissions before sending another message")
    if _permission_change(record or {}).get("operation_id") != permission_operation:
        raise NativeError(
            409, "permissions changed while sending; review the session and send again"
        )
    gen = await asyncio.to_thread(_generation, session_id)
    if gen is not None:
        life = await asyncio.to_thread(native_state.read_lifecycle, gen.worker_id)
        if life.get("phase") in {"intent", "starting", "native_started"}:
            try:
                await _wait_ready(gen.worker_id)
                return gen
            except NativeError:
                pass  # it died starting: a successor below, once it is proved gone
        elif life.get("phase") == "ready":
            return gen
    worker_id = await asyncio.to_thread(_relaunch_sync, engine_id, session_id, permission_operation)
    if worker_id is not None:
        await _wait_ready(worker_id)
    gen = await asyncio.to_thread(_generation, session_id)
    if gen is None:
        raise NativeError(503, "the native worker is unavailable")
    return gen


async def _effect(
    engine_id: str,
    session_id: str,
    action: str,
    params: dict,
    permission_operation: str | None,
) -> dict:
    for attempt in range(3):
        gen = await _live_generation(engine_id, session_id, permission_operation)
        current = await asyncio.to_thread(native_state.read_session, session_id)
        if _permission_change(current or {}).get("operation_id") != permission_operation:
            raise NativeError(
                409, "permissions changed while sending; review the session and send again"
            )
        try:
            if action == "submit" and any(
                a["mime"] == native_attachments.FILE_MIME for a in params.get("attachments") or ()
            ):
                life = await asyncio.to_thread(native_state.read_lifecycle, gen.worker_id)
                if life.get("files") is not True:
                    worker = native_containment.WorkerIdentity(gen.worker_id)
                    if not await asyncio.to_thread(HOST.worker_running, worker, life.get("pid")):
                        raise _Unreachable("the old worker exited")
                    raise NativeError(409, "to attach documents, stop and resume this session")
            return await _call(gen, action, params)
        except _Unreachable:
            # The socket is gone. Nothing was claimed by THIS call (no connection), and the
            # worker refuses before claiming when closing, so retrying in a successor is safe.
            # A worker whose native child just died is still shutting down: wait for that
            # process to exit before deciding, or every retry sees the same dying generation.
            await _wait_exited(gen)
        except NativeError as exc:
            if exc.detail != SHUTTING_DOWN:
                raise
            await _wait_exited(gen)  # an idle exit raced this request; resume in a successor
        if attempt == 2:
            break
    raise NativeError(503, "the native worker is unreachable")


async def _wait_exited(gen: Generation) -> None:
    worker = native_containment.WorkerIdentity(gen.worker_id)
    for _ in range(300):
        life = await asyncio.to_thread(native_state.read_lifecycle, gen.worker_id)
        if not await asyncio.to_thread(HOST.worker_running, worker, life.get("pid")):
            await asyncio.to_thread(native_state.update_lifecycle, gen.worker_id, phase="exited")
            return
        await asyncio.sleep(0.1)


def _mark_unreachable(gen: Generation) -> None:
    life = native_state.update_lifecycle(gen.worker_id, unreachable_at=time.time())
    # Only a dead process counts: an `unknown` systemd answer under load must not hide a live
    # worker's pending approvals (the next effect will try it again).
    worker = native_containment.WorkerIdentity(gen.worker_id)
    if not HOST.worker_running(worker, life.get("pid")):
        native_state.update_lifecycle(gen.worker_id, phase="exited")


# --- journal projection -----------------------------------------------------------------------


def _paragraph(text: str, block: str) -> str:
    """Append one agent message to a reply, a blank line apart so Markdown keeps them apart."""
    if not block.strip():
        return text
    if text and not text.endswith("\n\n"):
        text += "\n\n"
    return text + block


def _keep(turn: dict, key: str, text: str) -> None:
    """Store a reply's last SNAPSHOT_TEXT characters, counting what the cut dropped so a tool
    call's place in the text (`_at`) still lines up after it."""
    cut = max(0, len(text) - SNAPSHOT_TEXT)
    turn[f"{key}_cut"] = turn.get(f"{key}_cut", 0) + cut
    turn[key] = text[cut:]


def _place_tools(turn: dict, source: str) -> None:
    """Each tool call's `at`: how many characters of the shown reply (kept from `source`) came
    before it, so the activity reads in order between the agent's messages."""
    for tool in turn["tools"]:
        start = tool.pop("_at", {}).get(source, 0) - turn.get(f"{source}_cut", 0)
        tool["at"] = min(max(start, 0), len(turn["reply"]))


def _journal(prov, session_id: str) -> native_journal.Journal:
    try:
        return native_journal.read(_journal_root(prov), session_id)
    except native_journal.JournalError as exc:
        raise NativeError(404 if exc.code == "unavailable" else 503, exc.detail) from None


def _observe_task(tasks: dict, ended: set[str], data: dict, ts: float) -> None:
    """Merge one live worker's task observation (#1461), independently of execution state.

    Terminal ids stay ended for this generation, so a late sparse/progress frame cannot revive
    them. Journal order chooses observations; their timestamps are labels, never ordering keys.
    """
    task_id = data["task_id"]
    state = data.get("task_state", data["state"])
    if state in native_protocol.TERMINAL_TASK_STATES:
        ended.add(task_id)
        tasks.pop(task_id, None)
        return
    if task_id in ended:
        return
    if task_id not in tasks:
        if len(tasks) >= native_protocol.MAX_PENDING:
            return
        tasks[task_id] = {
            "task_id": task_id,
            "state": state or "unknown",
            "started_at": ts,
            "updated_at": ts,
        }
    task = tasks[task_id]
    task["updated_at"] = ts
    if state:
        task["state"] = state
    for field in native_protocol.TASK_METADATA_LIMITS:
        if data.get(field):
            task[field] = data[field]


def project(
    journal: native_journal.Journal,
    record: dict,
    live_worker: str | None,
    *,
    max_turns: int | None = None,
) -> dict:
    """Project only displayed turn payloads; transcript consumers may request every turn."""
    turns: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    decided: set[str] = set()
    native_id = record.get("native_id")
    model_effective = None
    background = False
    background_worker: str | None = None  # the worker that reported `background`
    background_turn: tuple[str | None, str] | None = None  # (target, the worker that minted it)
    cwd = journal.header.get("cwd")
    # A removed message (#1447) is not part of the conversation: it never takes a slot in the
    # turn window, and is not counted as an omitted earlier turn.
    removed = native_journal.withdrawn(journal)
    submits = [
        op.operation_id
        for op in journal.operations.values()
        if op.request["action"] == "submit" and op.operation_id not in removed
    ]
    # Turns the agent ran for itself (#1438), from the global `background` events that mint their
    # target. They share ONE display window with operator turns, newest first, so a bounded read
    # shows what a full one would. Ordered by journal position, never by a wall clock.
    starts, settled = [], set()
    tasks, ended_tasks = {}, set()
    ended: set[str] = set()  # agent turns whose end (or a later start, or a new worker) was seen
    last: tuple[str, str] | None = None  # (agent turn, the worker that minted it)
    for item in journal.events.for_operations(set()):
        kind, data = item["event"]["kind"], item["event"]["data"]
        if kind == "background" and data.get("task_id") and item["worker_id"] == live_worker:
            _observe_task(tasks, ended_tasks, data, item["ts"])
        if kind == "background" and "background_turn" in data:
            if last is not None:
                ended.add(last[0])
            last = (data["background_turn"], item["worker_id"]) if data["background_turn"] else None
            if data["background_turn"]:
                starts.append((item["cursor"], data["background_turn"]))
        elif kind == "turn_completed" and data.get("operation_id"):
            settled.add(data["operation_id"])
    if last is not None and last[1] != live_worker:
        ended.add(last[0])
    # An own turn that ended having recorded nothing has nothing to show (#1438 follow-up): every
    # own turn before workers recorded output, and quiet ones since, would be an empty header
    # taking a slot in the window. A running one always shows — it is what is happening now.
    starts = [
        (cursor, agent)
        for cursor, agent in starts
        if agent not in ended or journal.events.recorded(agent)
    ]
    timeline = sorted(
        [(journal.operations[op].claimed_revision, 0, op) for op in submits]
        + [(cursor, 1, agent) for cursor, agent in starts],
    )
    shown = timeline if max_turns is None else timeline[-max_turns:] if max_turns else []
    # Live work is never cut (Hermes on #1439), however many turns the agent ran for itself since:
    # every message the live worker still holds queued, and the newest one it dispatched if it has
    # not completed (it may have waited behind those turns). Nothing else: an input that settles
    # without a completion (a steer, a refusal) is history, and history ages out as before.
    live_submits = [op for op in submits if journal.operations[op].worker_id == live_worker]
    dispatched = [
        op for op in live_submits if journal.operations[op].handoff in {"uncertain", "sent"}
    ]
    keep = {op for op in live_submits if journal.operations[op].handoff == "queued"}
    if dispatched and dispatched[-1] not in settled:
        keep.add(dispatched[-1])
    waiting = [entry for entry in timeline if entry[1] == 0 and entry[2] in keep]
    shown = sorted({*shown, *waiting})
    selected = {entry[2] for entry in shown if entry[1] == 0}
    selected_agents = {entry[2] for entry in shown if entry[1] == 1}
    agents: dict[str, dict] = {}
    open_agent: str | None = None
    for op in journal.operations.values():
        params = op.request["params"]
        if op.request["action"] == "submit":
            if op.operation_id not in selected:
                continue
            turns[op.operation_id] = {
                "turn_id": op.operation_id,
                "status": "pending",
                "text": params["text"],
                "ts": op.ts,
                "attachments": [
                    native_attachments.public(a) for a in params.get("attachments") or ()
                ],
                "reply": "",
                "reply_ts": None,
                "partial": "",
                "partial_item": None,
                "context": params.get("context") or {},
                "tools": [],
                "handoff": op.handoff,
                "worker_id": op.worker_id,
                "completed": False,
            }
        elif op.request["action"] == "decide":
            # Native request ids repeat (per turn / per connection): key by the exact callback.
            decided.add((params["approval_worker_id"], params["turn_id"], params["request_id"]))
        elif op.request["action"] == "send_now" and op.handoff != "not_sent":
            queued = turns.get(params["queued_turn_id"])
            if queued is not None:
                queued["delivery"] = "steering" if params["mode"] == "steer" else "interrupting"
                queued["delivery_turn_id"] = params["turn_id"]
                queued["delivery_operation_id"] = op.operation_id
                queued["delivery_handoff"] = op.handoff
    observations = (
        journal.events
        if max_turns is None
        else journal.events.for_operations(selected | selected_agents)
    )
    for item in observations:
        kind, data = item["event"]["kind"], item["event"]["data"]
        # Attribution is by FIELD: an agent turn's output never lands on an operator turn.
        turn = (
            agents.get(data["agent_turn"])
            if "agent_turn" in data
            else turns.get(data.get("operation_id") or "")
        )
        if kind == "session" and data.get("native_id"):
            native_id = data["native_id"]
        elif kind == "model" and data.get("model_effective"):
            model_effective = data["model_effective"]
        elif kind == "background":
            if item["worker_id"] == live_worker:
                background, background_worker = bool(data.get("active")), item["worker_id"]
            if "background_turn" in data:
                background_turn = (data["background_turn"], item["worker_id"])
                if open_agent is not None:
                    previous = agents.get(open_agent)
                    if previous is not None and previous["worker_id"] != item["worker_id"]:
                        # A new generation speaks: the old one died mid-turn, whatever this says.
                        _settle_agent(previous, "worker_ended")
                    else:
                        # Its end, or a start that implies it (one turn runs at a time).
                        _settle_agent(previous, data["state"])
                open_agent = data["background_turn"]
                if open_agent in selected_agents and open_agent not in agents:
                    agents[open_agent] = {
                        "kind": "agent",
                        "position": item["cursor"],
                        "turn_id": open_agent,
                        "status": "pending",
                        "ts": item.get("ts"),
                        "reply": "",
                        "reply_ts": None,
                        "partial": "",
                        "partial_item": None,
                        "tools": [],
                        "worker_id": item["worker_id"],
                        "completed": False,
                    }
        elif kind == "approval" and turn is not None:
            exact = (item["worker_id"], data["operation_id"], data["request_id"])
            pending[exact] = {**data, "worker_id": item["worker_id"]}
        elif kind == "approval_cancelled":
            for exact in [
                k for k in pending if k[0] == item["worker_id"] and k[2] == data["request_id"]
            ]:
                pending.pop(exact)
        elif turn is None:
            if kind == "turn_completed" and item["worker_id"] == live_worker:
                background = bool(data.get("background_active"))
                background_worker = item["worker_id"]
            continue
        elif kind == "text":
            if data["text"] and turn["reply_ts"] is None:
                turn["reply_ts"] = item.get("ts")
            if data["partial"]:
                # Streamed pieces of ONE message concatenate; a new message id starts a paragraph.
                item = data.get("item_id")
                if item and turn["partial_item"] and item != turn["partial_item"]:
                    _keep(turn, "partial", _paragraph(turn["partial"], data["text"]))
                else:
                    _keep(turn, "partial", turn["partial"] + data["text"])
                turn["partial_item"] = item or turn["partial_item"]
            else:
                # Each settled text block is its own message: without a break, "…done.Next…".
                _keep(turn, "reply", _paragraph(turn["reply"], data["text"]))
            if data.get("questions") and data.get("item_id"):
                # The newest question the agent asked in this turn (#1463), and the id an
                # answer names as its `reply_to`.
                turn["questions"] = data["questions"]
                turn["question_id"] = data["item_id"]
        elif kind == "tool":
            entry = {
                "name": data.get("tool") or "tool",
                "outcome": data.get("state") or ("completed" if data.get("completed") else ""),
                "summary": (data.get("summary") or "")[:500],
                "id": data["item_id"],
            }
            existing = next((t for t in turn["tools"] if t["id"] == entry["id"]), None)
            if existing is not None and not data.get("tool"):
                entry["name"] = existing["name"]
            if entry["name"] == "execute" and data.get("summary"):
                raw = _payload(data["summary"])
                if isinstance(raw, dict):
                    entry["summary"] = str(raw.get("command") or "Command unavailable")[:500]
            if existing is None:
                # Where the call came in the text so far — settled or still streaming, whichever
                # the reply turns out to be shown from.
                entry["_at"] = {
                    key: turn.get(f"{key}_cut", 0) + len(turn[key]) for key in ("reply", "partial")
                }
                turn["tools"].append(entry)
            else:
                # A result names only the call it answers (Claude's carry no tool): it keeps the
                # call's name rather than the placeholder (#1438).
                if not data.get("tool"):
                    entry.pop("name")
                existing.update({k: v for k, v in entry.items() if v})
            # The same advisory classifier as a pending card (#1339): a tool row that ran a
            # command — asked or, in a skip-permissions session, never asked — says so.
            row = existing if existing is not None else entry
            if data.get("summary"):
                risk = _tool_risk(row["name"], data["summary"], cwd)
                if risk is not None:
                    row["risk"] = risk
        elif kind == "input_accepted":
            turn["completed"] = True
            turn["status"], turn["delivery"] = "delivered", "steered"
        elif kind == "delivery_failed" and data["control_id"] == turn.get("delivery_operation_id"):
            turn["delivery"], turn["delivery_reason"] = "interrupt_failed", data["message"]
        elif kind == "turn_completed":
            turn["completed"] = True
            turn["status"] = "done" if data["state"] == "completed" else "failed"
            if data["state"] == "interrupted":
                turn["code"] = "interrupted"
            if data.get("error"):
                turn["reason"] = data["error"][:2000]
            if data.get("text") and not turn["reply"]:
                turn["reply"] = data["text"]
                if turn["reply_ts"] is None:
                    turn["reply_ts"] = item.get("ts")
            if item["worker_id"] == live_worker:
                background = bool(data.get("background_active"))
                background_worker = item["worker_id"]
            for key, value in list(pending.items()):
                if value.get("operation_id") == turn["turn_id"]:
                    pending.pop(key)
        elif kind == "error" and data.get("action") in {"turn/start", "turn/steer"}:
            if not turn["completed"]:  # the agent refused the turn: it definitely did not run
                turn["completed"] = True
                turn["status"] = "failed"
                turn["reason"] = data.get("message") or "the agent refused this turn"
        elif kind == "disconnected" or (kind == "error" and data.get("operation_id")):
            if kind == "error" and data.get("native_will_retry"):
                continue
            if not turn["completed"]:
                turn["status"], turn["code"] = "failed", "uncertain"
                turn["reason"] = data.get("message") or "the native connection closed"
    in_flight = None
    requests = []
    for turn in [*turns.values(), *agents.values()]:
        # A reply that came whole at the end has no settled text before any call: all sit at 0.
        if turn["reply"] or turn.get("kind") == "agent":
            _place_tools(turn, "reply")
        else:
            turn["reply"] = turn["partial"]
            _place_tools(turn, "partial")
    for turn in turns.values():
        if turn["handoff"] == "not_sent":
            turn["status"], turn["reason"] = (
                "failed",
                "not sent: the agent did not accept this turn",
            )
        elif turn["handoff"] == "queued":
            if turn["worker_id"] == live_worker:
                turn["status"] = "queued"
            else:
                turn["status"] = "failed"
                turn["reason"] = (
                    "not sent: the native worker stopped before this queued message ran"
                )
        elif not turn["completed"] and turn["status"] == "pending":
            if turn["worker_id"] != live_worker:
                turn["status"], turn["code"] = "failed", "uncertain"
                turn["reason"] = "the native worker ended before this turn settled"
            else:
                if turn.get("delivery") == "steering":
                    turn["status"] = "delivering"
                else:
                    in_flight = turn["turn_id"]
        if (
            turn.get("delivery") in {"interrupting", "interrupt_failed"}
            and turn["status"] != "queued"
        ):
            # "Interrupted by Send now — your next message is answered below" needs BOTH: the
            # interrupt was SENT and not refused (it may have stopped its target), and the message
            # itself was SENT (it really is answered below). Otherwise the link is dropped and the
            # target reads as plainly interrupted (#1442; Hermes on #1451).
            if (
                turn["delivery"] == "interrupt_failed"
                or turn.get("delivery_handoff") != "sent"
                or turn["handoff"] != "sent"
                or turn["status"] == "failed"
            ):
                turn.pop("delivery_turn_id", None)
            turn.pop("delivery", None)
            turn.pop("delivery_reason", None)
    for exact, approval in pending.items():
        request_id = exact[2]
        if exact in decided or approval["worker_id"] != live_worker:
            continue
        turn = turns.get(approval["operation_id"])
        if turn is None or turn["turn_id"] != in_flight:
            continue
        turn["status"] = "awaiting_approval"
        offered = _offered(record.get("adapter"), approval, cwd)
        requests.append(
            {
                "request_id": request_id,
                "turn_id": turn["turn_id"],
                "kind": approval["tool"],
                "item_id": approval["item_id"],
                "summary": approval["summary"],
                # The complete request the decision approves; a UI must render it in full
                # before offering approval (the digest binds the decision to these bytes).
                "payload": _payload(approval["summary"]),
                "payload_digest": approval["payload_digest"],
                # Approve is offered only for a request presented COMPLETELY (a file change's
                # patch, a permission prompt's whole context); declining needs no review.
                # Never for an agent question (#1463), whatever the worker offered: an approve
                # sends it no answers, and a worker from before #1463 still offers one.
                "choices": [
                    c
                    for c in approval["choices"]
                    if c != "approve"
                    or (
                        approval.get("complete") is True
                        and approval["tool"] != native_questions.ASK_TOOL
                    )
                ],
                "complete": approval.get("complete") is True,
                # #1339: the standing grants the request itself proposed, each labelled with what
                # it covers (risky ones included — operator direction), and the advisory risk.
                "always": [native_grants.public(c) for c in offered["always"]],
                "risk": offered["risk"],
                "operator_only": True,
                "context": dict(turn["context"]),
            }
        )
    for agent in agents.values():
        if agent["worker_id"] != live_worker:
            # Its worker is gone: nothing will end this turn now.
            _settle_agent(agent, "worker_ended")
    for turn_id, turn in turns.items():
        turn["position"] = journal.operations[turn_id].claimed_revision
    shown_turns = sorted([*turns.values(), *agents.values()], key=lambda t: t["position"])
    for turn in shown_turns:
        for key in (
            "partial",
            "partial_item",
            "handoff",
            "worker_id",
            "completed",
            "delivery_operation_id",
            "delivery_handoff",
            "position",
            "reply_cut",
            "partial_cut",
        ):
            turn.pop(key, None)
    return {
        "turns": shown_turns,
        "omitted_turns": len(timeline) - len(shown_turns),
        "revision": journal.revision,
        "cwd": journal.header["cwd"],
        "in_flight": in_flight,
        "model": record["request"]["model"],
        "effort": record["request"].get("effort"),
        "bypass": _effective_bypass(record),
        "can_enable_bypass": (
            record.get("adapter") in kinds.API_BYPASS_KINDS
            and bool(record.get("native_id"))
            and bool(record.get("generations"))
            and _start_state(record) in (None, STARTED)
            and not _effective_bypass(record)
        ),
        "permission_change": {
            key: value
            for key, value in _permission_change(record).items()
            if key in {"operation_id", "state"}
        },
        **pending_start_view(record),
        "model_effective": model_effective,
        "pending_requests": requests,
        "native": {
            "native_id": native_id,
            "worker": live_worker,
            # Only the live worker's own report: an exited or replaced generation's task ended
            # with it, so its last `active` would show an idle replacement busy forever.
            "background_active": background and background_worker == live_worker,
            "background_tasks": list(tasks.values()),
            # Only the live worker's own turn is a Send now target (#1427): a target minted by an
            # exited or replaced generation names a turn nothing can stop any more.
            "background_turn": background_turn[0]
            if background_turn is not None and background_turn[1] == live_worker
            else None,
        },
    }


def _settle_agent(agent: dict | None, state: str) -> None:
    """End a turn the agent ran for itself, as its end event says (#1438)."""
    if agent is None or agent["completed"]:
        return
    agent["completed"] = True
    if state == "interrupted":
        agent["status"], agent["code"] = "failed", "interrupted"
    elif state == "worker_ended":
        agent["status"], agent["reason"] = "failed", "the native worker ended during this turn"
    elif state == "failed":
        agent["status"], agent["reason"] = "failed", "the agent reported an error"
    else:
        agent["status"] = "done"


def _offered(adapter: str | None, approval: dict, cwd: str | None) -> dict:
    """What a pending request may be answered with beyond once (#1339), derived only from the
    journaled request as presented. Incomplete requests get no grant, and an agent question
    (#1463) neither grants nor runs anything, so it has no grant and no risk."""
    if approval.get("tool") == native_questions.ASK_TOOL:
        return {"always": [], "risk": None}
    if approval.get("complete") is not True:
        payload = _payload(approval["summary"])
        risk = native_grants.derive(adapter or "", approval.get("tool"), payload, cwd)["risk"]
        return {"always": [], "risk": risk}
    return native_grants.derive(
        adapter or "", approval.get("tool"), _payload(approval["summary"]), cwd
    )


def _tool_risk(name: str, summary: str, cwd: str | None) -> dict | None:
    """Original command data: Codex's command line, Claude/ACP's input, never a tool title."""
    if name == "commandExecution":
        return risk_marks.classify(summary, cwd)
    if name == "execute":
        payload = _payload(summary)
        return risk_marks.classify(
            payload.get("command") if isinstance(payload, dict) else None, cwd
        )
    if name in {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"}:
        payload = _payload(summary)
        return (
            risk_marks.classify_tool(name, payload, cwd)
            if isinstance(payload, dict)
            else risk_marks.unknown()
        )
    return None


def _payload(summary: str):
    try:
        return json.loads(summary)
    except ValueError:
        return summary


def _live_worker(session_id: str) -> str | None:
    gen = _generation(session_id)
    if gen is None:
        return None
    life = native_state.read_lifecycle(gen.worker_id)
    if life.get("phase") == "intent":
        # Being launched right now; a launch that never pinned anything is not live forever.
        recent = time.time() - float(life.get("intent_at") or 0) < READY_TIMEOUT
        return gen.worker_id if recent else None
    if life.get("phase") not in {"starting", "native_started", "ready"}:
        return None
    worker = native_containment.WorkerIdentity(gen.worker_id)
    return gen.worker_id if HOST.worker_running(worker, life.get("pid")) else None


def _snapshot_sync(engine_id: str, session_id: str) -> dict:
    # A read: it reaches a retiring client's history too (#1311). New work never does.
    prov, record = _session(engine_id, session_id, retiring_ok=True)
    from .structured_runtime import SNAPSHOT_TURNS

    live = _live_worker(session_id)
    view = project(_journal(prov, session_id), record, live, max_turns=SNAPSHOT_TURNS)
    # The running worker advertises its own implementation. A web update cannot teach an
    # already-running, release-pinned worker a new effect (#1389).
    life = native_state.read_lifecycle(live) if live else {}
    mode = life.get("send_now")
    if mode in {"steer", "interrupt"}:
        view["native"]["send_now"] = mode
    # Send now against a turn the agent runs for itself (#1427), only from a worker that says it
    # can; an older worker's target is never offered.
    if life.get("send_now_background") == "interrupt":
        view["native"]["send_now_background"] = "interrupt"
        # Its Interrupt button (#1438), only from a worker that accepts the direct request.
        view["native"]["interrupt_own"] = life.get("interrupt_own") is True
    else:
        view["native"]["background_turn"] = None
    view["native"]["files"] = not live or life.get("files") is True
    # Remove a queued message (#1447): only a live worker that says it can.
    if life.get("withdraw") is True:
        view["native"]["withdraw"] = True
    return view


# --- adapter operations ------------------------------------------------------------------------


async def snapshot(engine_id: str, session_id: str) -> dict:
    return await asyncio.to_thread(_snapshot_sync, engine_id, session_id)


async def events(engine_id: str, session_id: str, after: int, limit: int) -> dict:
    prov, _ = await asyncio.to_thread(partial(_session, engine_id, session_id, retiring_ok=True))
    journal = await asyncio.to_thread(_journal, prov, session_id)
    try:
        return await asyncio.to_thread(journal.page, after, limit)
    except native_journal.JournalError as exc:
        raise NativeError(_ERRORS.get(exc.code, 409), exc.detail) from None


def _replay(journal: native_journal.Journal, operation_id: str, request: dict) -> dict | None:
    previous = journal.operations.get(operation_id)
    if previous is None:
        return None
    if previous.request != request:
        raise NativeError(409, "operation id already used for a different request")
    return previous.receipt()


async def submit(
    engine_id: str,
    session_id: str,
    turn_id: str,
    text: str,
    *,
    expected_revision: int | None = None,
    context: dict | None = None,
    attachments: list[dict] | None = None,
    execution_admission=None,
    idempotent: bool = True,
    reply_to: str | None = None,
) -> dict:
    """``attachments``: files/pictures already admitted by ``native_attachments.admit``.
    ``reply_to``: the async question this message answers (#1463); the journal admits it only
    while that question is still open."""
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    try:
        native_protocol.validate_text(text, allow_empty=bool(attachments))
    except native_protocol.ProtocolError as exc:
        raise NativeError(422, str(exc)) from None
    prov, record = await asyncio.to_thread(_session, engine_id, session_id)
    # Pin before the first journal read, and across revision retries too. A request already
    # waiting on history must not acquire a mode enabled while it was waiting (#1388).
    permission_operation = _permission_change(record).get("operation_id")
    if _permission_change(record).get("state") == "stopping":
        raise NativeError(409, "finish enabling Skip permissions before sending another message")
    params = {"text": text, "context": context or {}}
    if attachments:
        params["attachments"] = attachments
    if reply_to is not None:
        params["reply_to"] = reply_to
    try:
        immutable = native_ipc.normalize_immutable_request({"action": "submit", "params": params})
    except native_ipc.IPCError as exc:
        raise NativeError(422, str(exc)) from None
    try:
        native_protocol.validate_text(
            native_attachments.text_with_files(text, immutable["params"].get("attachments") or []),
            allow_empty=bool(attachments),
        )
    except native_protocol.ProtocolError as exc:
        raise NativeError(422, str(exc)) from None
    if any(
        a["mime"] != native_attachments.FILE_MIME
        for a in immutable["params"].get("attachments") or ()
    ) and not kinds.API_IMAGE_INPUT.get(prov.manifest.api.kind, False):
        raise NativeError(422, "this client takes no images")
    # An explicit expectation still holds when the only records since are a SUCCESSOR's startup:
    # this send relaunched an idled-out worker, and a fresh generation journals before any claim.
    prior = await asyncio.to_thread(_live_worker, session_id)
    for _ in range(3):
        journal = await asyncio.to_thread(_journal, prov, session_id)
        receipt = _replay(journal, turn_id, immutable)
        if receipt is not None:
            break
        revision = journal.revision if expected_revision is None else expected_revision
        try:
            receipt = await _effect(
                engine_id,
                session_id,
                "submit",
                {**params, "operation_id": turn_id, "expected_revision": revision},
                permission_operation,
            )
            break
        except NativeError as exc:
            if "revision changed" not in exc.detail:
                raise
            # Unsolicited observations move the revision; only an implicit expectation retries.
            if expected_revision is None:
                continue
            after = await asyncio.to_thread(_journal, prov, session_id)
            gen = await asyncio.to_thread(_generation, session_id)
            if gen is not None and _successor_startup_only(
                after, expected_revision, prior, gen.worker_id
            ):
                expected_revision = after.revision
                continue
            raise
    else:
        raise NativeError(409, "the conversation kept changing; retry")
    if receipt["handoff"] == "not_sent":
        raise NativeError(
            409,
            "this turn was not sent: the agent is busy with another turn"
            + (", or an attached file changed" if attachments else ""),
        )
    return receipt


_STARTUP_EVENTS = frozenset({"initialized", "session", "background", "response"})


def _successor_startup_only(
    journal: native_journal.Journal, after: int, prior: str | None, successor: str
) -> bool:
    """Every record since ``after`` is a startup observation by ``successor``, the current
    generation, which was not live when the send began (``prior``).

    Such a generation has run no turn: what it journals before the claim (initialization,
    session facts, background state) is no activity the operator missed. Nor is a worker
    diagnostic bound to no operation or action — a history frame dropped as oversized while
    the native session loads: the snapshot shows it on no turn. A claim, a handoff,
    conversation content, or any record by another generation — the idled-out one's last
    background reports included — keeps the refusal."""
    if successor == prior:
        return False
    start = bisect.bisect_right(journal.events.cursors, after)
    newer = journal.events[start:]
    return len(newer) == journal.revision - after and all(
        event["worker_id"] == successor and _startup_record(event["event"]) for event in newer
    )


def _startup_record(event: dict) -> bool:
    if event["kind"] in _STARTUP_EVENTS:
        return True
    data = event.get("data") or {}
    return event["kind"] == "error" and not data.get("operation_id") and not data.get("action")


async def _existing_generation_call(session_id: str, action: str, params: dict) -> dict:
    """Controls target the RUNNING generation only: a successor could never answer the old
    callback, interrupt, or steer the old turn, so nothing is relaunched for them."""
    gen = await asyncio.to_thread(_generation, session_id)
    if gen is None:
        raise NativeError(409, "no native worker is running this session")
    try:
        return await _call(gen, action, params)
    except _Unreachable:
        await asyncio.to_thread(_mark_unreachable, gen)
        raise NativeError(409, "the native worker is no longer running") from None


async def _revisioned(prov, session_id, operation_id, immutable, expected_revision, prepare):
    """Replay first; then send with the journal revision, retrying only an IMPLICIT expectation
    when unsolicited observations (streamed output) moved it between read and claim."""
    for _ in range(5):
        journal = await asyncio.to_thread(_journal, prov, session_id)
        receipt = _replay(journal, operation_id, immutable)
        if receipt is not None:
            return receipt
        revision = journal.revision if expected_revision is None else expected_revision
        try:
            return await prepare(journal, revision)
        except NativeError as exc:
            if expected_revision is None and "revision changed" in exc.detail:
                continue
            raise
    raise NativeError(409, "the conversation kept changing; retry")


async def decide(
    engine_id: str,
    session_id: str,
    turn_id: str,
    request_id: str,
    decision: str,
    user: str,
    *,
    decision_id: str,
    expected_revision: int | None = None,
    grant: str | None = None,
    answers: list | None = None,
    execution_admission=None,
) -> dict:
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    if decision not in {"approve", "reject", "cancel", "always", "answer"}:
        raise NativeError(422, "decision must be approve, always, answer, reject or cancel")
    if (decision == "always") != (grant is not None):
        raise NativeError(422, "a standing grant goes with an always decision, and only with one")
    if (decision == "answer") != (answers is not None):
        raise NativeError(422, "answers go with an answer decision, and only with one")
    if answers is not None:
        try:
            native_questions.answer_shape(answers)
        except native_questions.QuestionError as exc:
            raise NativeError(422, str(exc)) from None
    if grant is not None and not native_grants.valid_grant_id(grant):
        raise NativeError(422, "grant must be one of the offered grant ids")
    if not isinstance(user, str) or not user or len(user) > 128:
        raise NativeError(422, "a decision needs its authenticated operator")
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    first = await asyncio.to_thread(_journal, prov, session_id)
    previous = first.operations.get(decision_id)
    if previous is not None:
        params = previous.request["params"]
        # A record from before actor binding has none; replaying it only observes its outcome
        # (no new effect), so any operator may read it back. Bound records require the same one.
        recorded_actor = params.get("actor", user)
        if previous.request["action"] != "decide" or (
            params["request_id"],
            params["decision"],
            params["turn_id"],
            recorded_actor,
            params.get("grant"),
            params.get("answers"),
        ) != (request_id, decision, turn_id, user, grant, answers):
            raise NativeError(409, "decision id already used for a different decision")
        return {
            "turn_id": turn_id,
            "request_id": request_id,
            "decision": decision,
            **previous.receipt(),
        }
    record = await asyncio.to_thread(native_state.read_session, session_id)
    live = await asyncio.to_thread(_live_worker, session_id)
    view = await asyncio.to_thread(project, first, record or {"request": {"model": None}}, live)
    approval = next(
        (
            r
            for r in view["pending_requests"]
            if (r["request_id"], r["turn_id"]) == (request_id, turn_id)
        ),
        None,
    )
    if approval is None:
        raise NativeError(409, "that approval is no longer pending")
    if decision in {"approve", "always"} and approval["kind"] == native_questions.ASK_TOOL:
        # Independently of the offered choices: a worker launched before #1463 still offers
        # approve for a question, and would send Claude the input without answers.
        raise NativeError(409, "an agent question is answered, skipped or stopped, not approved")
    if decision == "always":
        # Only a grant this snapshot itself offers (#1339): client-proposed, never invented or
        # altered. The worker re-derives it before writing anything. Never for a request that
        # can't be approved — an agent question (#1463) grants nothing.
        if "approve" not in approval["choices"] or grant not in {
            c["id"] for c in approval["always"]
        }:
            raise NativeError(409, "that standing grant is not offered for this request")
    elif decision not in approval["choices"]:
        raise NativeError(
            409,
            "this request could not be presented completely; it can only be declined"
            if decision == "approve"
            else "that decision is not offered for this request",
        )
    if decision == "answer":
        # The question as journaled and shown (#1463): only its own options, one unless it is
        # multi-select. The worker checks again against the request it holds.
        payload = approval.get("payload")
        try:
            if approval["kind"] != native_questions.ASK_TOOL or not isinstance(payload, dict):
                raise native_questions.QuestionError("this request takes no answer")
            native_questions.claude_answers(payload.get("input"), answers)
        except native_questions.QuestionError as exc:
            raise NativeError(409, str(exc)) from None
    event = next(
        e["event"]["data"]
        for e in reversed(first.events)
        if e["event"]["kind"] == "approval"
        and e["event"]["data"]["request_id"] == request_id
        and e["event"]["data"]["operation_id"] == turn_id
        and e["worker_id"] == live
    )
    params = {
        "turn_id": turn_id,
        "request_id": request_id,
        "item_id": event["item_id"],
        "payload_digest": event["payload_digest"],
        "decision": decision,
        "approval_worker_id": event["worker_id"],
        "approval_connection_id": event["connection_id"],
        "actor": user,  # who decided is part of the decision's durable identity
        **({"grant": grant} if grant is not None else {}),
        **({"answers": answers} if answers is not None else {}),
    }
    immutable = native_ipc.normalize_immutable_request({"action": "decide", "params": params})

    async def send(_journal_now, revision):
        return await _existing_generation_call(
            session_id,
            "decide",
            {**params, "operation_id": decision_id, "expected_revision": revision},
        )

    receipt = await _revisioned(prov, session_id, decision_id, immutable, expected_revision, send)
    if receipt["handoff"] == "not_sent":
        raise NativeError(409, "that approval is no longer pending; the decision was not sent")
    return {"turn_id": turn_id, "request_id": request_id, "decision": decision, **receipt}


async def interrupt(engine_id: str, session_id: str, *, operation_id: str, turn_id: str) -> dict:
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    params = {"turn_id": turn_id}
    immutable = native_ipc.normalize_immutable_request({"action": "interrupt", "params": params})

    async def send(_journal_now, revision):
        return await _existing_generation_call(
            session_id,
            "interrupt",
            {**params, "operation_id": operation_id, "expected_revision": revision},
        )

    receipt = await _revisioned(prov, session_id, operation_id, immutable, None, send)
    if receipt["handoff"] == "not_sent":
        raise NativeError(409, "that turn could not be interrupted yet; retry with a new id")
    return receipt


async def send_now(
    engine_id: str, session_id: str, *, operation_id: str, turn_id: str, queued_turn_id: str
) -> dict:
    """One deliberate selection of an existing message; no relaunch or duplicate submit."""
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    journal = await asyncio.to_thread(_journal, prov, session_id)
    previous = journal.operations.get(operation_id)
    if previous is not None and previous.request["action"] == "send_now":
        mode = previous.request["params"]["mode"]
    else:
        live = await asyncio.to_thread(_live_worker, session_id)
        life = await asyncio.to_thread(native_state.read_lifecycle, live) if live else {}
        mode = life.get("send_now")
        # A turn the agent runs for itself is always interrupted, whatever the worker's mode for
        # an operator turn (#1427): a steer would put the reply in a turn nobody owns.
        target = await asyncio.to_thread(_background_turn, journal, live)
        if target is not None and target == turn_id:
            mode = life.get("send_now_background")
            if mode != "interrupt":
                raise NativeError(409, "Send now is available in newly started sessions")
        if mode not in {"steer", "interrupt"}:
            raise NativeError(409, "Send now is available in newly started sessions")
    params = {"turn_id": turn_id, "queued_turn_id": queued_turn_id, "mode": mode}
    immutable = native_ipc.normalize_immutable_request({"action": "send_now", "params": params})

    async def send(_journal_now, revision):
        return await _existing_generation_call(
            session_id,
            "send_now",
            {**params, "operation_id": operation_id, "expected_revision": revision},
        )

    receipt = await _revisioned(prov, session_id, operation_id, immutable, None, send)
    if receipt["handoff"] == "not_sent":
        raise NativeError(409, "Send now was not applied; check the current message state")
    return receipt


async def withdraw(
    engine_id: str, session_id: str, *, operation_id: str, queued_turn_id: str
) -> dict:
    """Remove one still-queued message before the live worker sends it (#1447)."""
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    journal = await asyncio.to_thread(_journal, prov, session_id)
    if operation_id not in journal.operations:
        live = await asyncio.to_thread(_live_worker, session_id)
        life = await asyncio.to_thread(native_state.read_lifecycle, live) if live else {}
        if life.get("withdraw") is not True:
            raise NativeError(
                409, "Removing a queued message is available in newly started sessions"
            )
    params = {"queued_turn_id": queued_turn_id}
    immutable = native_ipc.normalize_immutable_request({"action": "withdraw", "params": params})

    async def send(_journal_now, revision):
        return await _existing_generation_call(
            session_id,
            "withdraw",
            {**params, "operation_id": operation_id, "expected_revision": revision},
        )

    receipt = await _revisioned(prov, session_id, operation_id, immutable, None, send)
    if receipt["handoff"] == "not_sent":
        raise NativeError(409, "The message was not removed; check the current message state")
    return receipt


def _background_turn(journal: native_journal.Journal, live: str | None) -> str | None:
    """The live worker's current Send now target for a turn the agent runs for itself."""
    if live is None:
        return None
    for index in range(len(journal.events) - 1, -1, -1):
        item = journal.events[index]
        event = item["event"]
        if event["kind"] == "background" and "background_turn" in event["data"]:
            return event["data"]["background_turn"] if item["worker_id"] == live else None
    return None


def _gone(session_id: str, worker_id: str) -> bool:
    if _identity(worker_id) is not None:
        return classify(session_id, worker_id) == "gone"
    # Never pinned: launch ran under this lock, so nothing can still be starting under the
    # name. Only a unit name systemd does not know at all counts as gone.
    observation = HOST.show(native_containment.WorkerIdentity(worker_id))
    return observation is not None and observation.load_state == "not-found"


def _stop_sync(engine_id: str, session_id: str, discard: bool = True) -> str:
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is None:
            raise NativeError(404, "no such native session")
        return _stop_locked(engine_id, session_id, record, discard)


def _stop_locked(engine_id: str, session_id: str, record: dict, discard: bool) -> str:
    """Close admission and prove containment while holding the session lifecycle lock."""
    record["closed"] = True
    if discard and _start_state(record) in {PENDING, STARTING, FAILED, EXPIRED}:
        # The operator's Discard of a skip creation that never completed its start: terminal,
        # in the same write — a later /start, a late readiness and a turn all refuse (5926).
        _set_start(record, DISCARDED)
    native_state.write_session(session_id, record)
    for worker_id in record.get("workers", []):
        identity = _identity(worker_id)
        if identity is None:
            continue
        observation = HOST.show(identity.worker)
        if observation is not None and observation.invocation_id == identity.invocation_id:
            with contextlib.suppress(native_containment.ContainmentError):
                HOST.stop(native_containment.stop_argv(identity, observation))
    deadline = time.monotonic() + STOP_TIMEOUT
    while True:
        remaining = []
        for worker_id in record.get("workers", []):
            if _gone(session_id, worker_id):
                life = native_state.read_lifecycle(worker_id)
                release = Path(life["release"]) if life.get("release") else None
                _lease(release, worker_id, remove=True)
                native_state.forget_config(worker_id)
                native_state.update_lifecycle(worker_id, phase="gone", gone_at=time.time())
            else:
                remaining.append(worker_id)
        record["workers"] = remaining
        native_state.write_session(session_id, record)
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if not remaining:
        # Still under the lifecycle lock: no replay or relaunch can reserve in between the
        # proof that nothing ran and the discharge (Hermes on #1278).
        _discharge_if_unused(engine_id, session_id, record)
        return "gone"
    live = any(classify(session_id, w) == "live" for w in remaining)
    return "live" if live else "unknown"


def _enable_bypass_sync(engine_id: str, session_id: str, operation_id: str, actor: str) -> None:
    # #1388: the operator explicitly enables the existing vendor bypass for THIS conversation.
    # Operator approval for Codex, Claude and OpenCode: PR #1393, comment 95140
    # (linked from the structured-clients invariant).
    # Never rewrite the create request: native ownership and create replay still bind that input.
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        prov, record = _session(engine_id, session_id)
        if prov.manifest.api.kind not in kinds.API_BYPASS_KINDS:
            raise NativeError(422, "this client cannot skip permission prompts")
        change = _permission_change(record)
        if change and (change["operation_id"], change["actor"]) != (operation_id, actor):
            raise NativeError(
                409, "a different operation already changed this session's permissions"
            )
        if change.get("state") == "enabled":
            return  # Exact replay observes; it must never stop a later generation.
        if (
            _start_state(record) not in (None, STARTED)
            or not record.get("generations")
            or not record.get("native_id")
        ):
            raise NativeError(409, "start this session before changing its permissions")
        if _effective_bypass(record):
            raise NativeError(409, "this session already skips permission prompts")
        if not change:
            change = {
                "operation_id": operation_id,
                "actor": actor,
                "state": "stopping",
                "requested_at": time.time(),
            }
            record["permission_change"] = change
            record["closed"] = True
            native_state.write_session(session_id, record)
        # The same lifecycle lock fences submit/relaunch, Stop, archive, and worker readiness.
        # A crash/unknown containment leaves the closed gate and durable retry identity intact.
        if _stop_locked(engine_id, session_id, record, False) != "gone":
            raise NativeError(
                503, "session tools may still be running; retry enabling Skip permissions"
            )
        _session(engine_id, session_id)  # a retired client cannot acquire this new permission
        change["state"] = "enabled"
        change["enabled_at"] = time.time()
        native_state.write_session(session_id, record)
        # Do not launch or resend anything. The next explicit operator message resumes history.


async def enable_bypass(engine_id: str, session_id: str, *, operation_id: str, user: str) -> None:
    try:
        native_state._uuid(operation_id, "permission operation")
    except native_state.StateError as exc:
        raise NativeError(422, str(exc)) from None
    if not isinstance(user, str) or not user or len(user) > 128:
        raise NativeError(422, "a permission change needs its authenticated operator")
    await asyncio.to_thread(_enable_bypass_sync, engine_id, session_id, operation_id, user)


def _discharge_if_unused(engine_id: str, session_id: str, record: dict) -> None:
    """Every generation is proved gone: a creation that never bound and never ran a turn
    releases its reservation (which otherwise holds new console launches for the source).
    The caller holds the session lifecycle lock."""
    if record.get("workers") or record.get("native_id") is not None:
        return
    with contextlib.suppress(NativeError):
        prov = _api_provider(engine_id, retiring_ok=True)
        root = _journal_root(prov)
        if (root / f"{session_id}.jsonl").exists():
            try:
                journal = native_journal.read(root, session_id)
            except native_journal.JournalError:
                return  # unreadable: cannot prove no turn was claimed, so keep the reservation
            if any(op.request["action"] == "submit" for op in journal.operations.values()):
                return
        # (no journal at all: creation stopped before its header, so no turn can exist)
        intent = native_ownership.lookup(record["session_key"])
        if intent is None or intent.native_id is not None:
            return
        with storage.locked("launch", wait=30):
            native_ownership.discharge(
                record["session_key"],
                operation_id=session_id,
                owner_token=record["owner_token"],
            )


def _discard_start(session_id: str) -> None:
    """Record the operator's Discard of an unfinished skip start FIRST, under the session lock,
    before any await (Hermes 5941): a completion that lands while the worker stop is in flight
    then fails its CAS instead of marking the discarded creation started."""
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is not None and _start_state(record) in {PENDING, STARTING, FAILED, EXPIRED}:
            _set_start(record, DISCARDED)
            native_state.write_session(session_id, record)


async def stop(engine_id: str, session_id: str, *, discard: bool = True) -> dict:
    await asyncio.to_thread(partial(_session, engine_id, session_id, retiring_ok=True))
    if discard:
        await asyncio.to_thread(_discard_start, session_id)
    gen = await asyncio.to_thread(_generation, session_id)
    if gen is not None:
        with contextlib.suppress(NativeError, _Unreachable):
            await _call(
                gen,
                "stop",
                {
                    "operation_id": str(uuid.uuid4()),
                    "expected_revision": (
                        await asyncio.to_thread(
                            _journal, _api_provider(engine_id, retiring_ok=True), session_id
                        )
                    ).revision,
                    "target_worker_id": gen.worker_id,
                },
                timeout=5,
            )
    return {
        "containment": await asyncio.to_thread(
            partial(_stop_sync, engine_id, session_id, discard=discard)
        )
    }


def _probe_sync(engine_id: str, session_id: str) -> str:
    _session(engine_id, session_id, retiring_ok=True)
    # Under the lifecycle lock: a creation or relaunch in progress holds it, so this never
    # certifies "gone" for a generation about to be launched (Hermes on #1278).
    with native_state.session_lock(session_id):
        _, record = _session(engine_id, session_id, retiring_ok=True)
        worker_id = record.get("current_worker")
        if worker_id:
            return classify(session_id, worker_id)
        if record.get("closed") and not record.get("workers"):
            return "gone"
        return "unknown"  # created but never launched (or its launch was interrupted)


async def probe(engine_id: str, session_id: str) -> dict:
    return {"containment": await asyncio.to_thread(_probe_sync, engine_id, session_id)}
