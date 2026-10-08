"""Web-side native runtime: contained worker lifecycle and the structured adapter (#1278).

The web process never speaks a native protocol itself. It reserves ownership, writes the one
private worker config, launches the fixed systemd service, pins its invocation, and then talks to
the worker over the private socket. Reads (snapshot/events) come straight from the durable
journal, so a browser or web restart observes everything without the worker's help.

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
"""

from __future__ import annotations

import asyncio
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
    native_containment,
    native_ipc,
    native_journal,
    native_ownership,
    native_protocol,
    native_state,
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
}
# The vendor CLI writes where ITS defaults point. A BattleLab store override that the vendor
# would not also use cannot be honoured without silently reading one history and writing another.
_VENDOR_STORES = {"codex-app-server": ".codex/sessions", "claude-stream-json": ".claude"}
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
        root = source.store_root()
        expected = Path.home() / _VENDOR_STORES[adapter]
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
        "mode": mode,
        "native_id": record.get("native_id"),
        "journal_root": str(_journal_root(prov)),
        "socket": f"{runtime}/agent-sessions-native/{worker.worker_id.replace('-', '')}.sock",
        "home": str(Path.home()),
        "path": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "user": os.environ.get("USER"),
        "logname": os.environ.get("LOGNAME"),
        "server_env": _server_env(),
        "idle_timeout": WORKER_IDLE_TIMEOUT,
    }
    if adapter == "codex-app-server" and mode == "create":
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

    async def exchange():
        reader, writer = await asyncio.open_unix_connection(
            gen.config["socket"], limit=native_ipc.MAX_FRAME_BYTES + 1
        )
        try:
            writer.write(native_ipc.encode_handshake(binding, capability).data)
            await writer.drain()
            native_ipc.decode_welcome(await reader.readline(), expected=binding)
            writer.write(native_ipc.encode_request(binding, request["request_id"], action, params))
            await writer.drain()
            return native_ipc.decode_response(await reader.readline(), request=request)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    try:
        response = await asyncio.wait_for(exchange(), timeout)
    except (OSError, ConnectionError) as exc:
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


def _create_sync(engine_id: str, session_id: str, cwd: str, model: str | None) -> bool:
    """Returns True when a generation must be awaited (a new launch happened)."""
    from .engines import registry

    prov = _api_provider(engine_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    adapter = prov.manifest.api.kind
    session_key = f"{engine_id}:{session_id}"
    # The model is checked against the client's own list NOW, at the write boundary (#1313): an id
    # the client does not list is refused before anything exists. A refusal only blocks a NEW
    # creation; an exact replay compares the request as sent.
    refused = None
    try:
        model = model_choice.select_api(prov, model)
    except model_choice.ModelRefused as exc:
        refused = exc
    request = {"cwd": cwd, "model": model, "adapter": adapter}
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
                request={"cwd": cwd, "model": model, "adapter": adapter},
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
        _launch_locked(session_id, record, prov, binding, mode="create")
        return True


async def create(
    engine_id: str,
    cwd: str,
    *,
    session_id: str,
    model: str | None = None,
    execution_admission=None,
) -> str:
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    cwd, model = _validate_create(cwd, model)
    try:
        launched = await asyncio.to_thread(_create_sync, engine_id, session_id, cwd, model)
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


def _relaunch_sync(engine_id: str, session_id: str) -> str | None:
    prov, _ = _session(engine_id, session_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    with native_state.session_lock(session_id):
        _, record = _session(engine_id, session_id)
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


async def _live_generation(engine_id: str, session_id: str) -> Generation:
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
    worker_id = await asyncio.to_thread(_relaunch_sync, engine_id, session_id)
    if worker_id is not None:
        await _wait_ready(worker_id)
    gen = await asyncio.to_thread(_generation, session_id)
    if gen is None:
        raise NativeError(503, "the native worker is unavailable")
    return gen


async def _effect(engine_id: str, session_id: str, action: str, params: dict) -> dict:
    for attempt in range(3):
        gen = await _live_generation(engine_id, session_id)
        try:
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


def _journal(prov, session_id: str) -> native_journal.Journal:
    try:
        return native_journal.read(_journal_root(prov), session_id)
    except native_journal.JournalError as exc:
        raise NativeError(404 if exc.code == "unavailable" else 503, exc.detail) from None


def project(journal: native_journal.Journal, record: dict, live_worker: str | None) -> dict:
    """The chat-shaped raw view the facade's ``_snapshot`` bounds and presents."""
    turns: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    decided: set[str] = set()
    native_id = record.get("native_id")
    model_effective = None
    background = False
    for op in journal.operations.values():
        params = op.request["params"]
        if op.request["action"] == "submit":
            turns[op.operation_id] = {
                "turn_id": op.operation_id,
                "status": "pending",
                "text": params["text"],
                "attachments": [
                    {"stored": a["stored"], "mime": a["mime"]}
                    for a in params.get("attachments") or ()
                ],
                "reply": "",
                "partial": "",
                "context": params.get("context") or {},
                "tools": [],
                "handoff": op.handoff,
                "worker_id": op.worker_id,
                "completed": False,
            }
        elif op.request["action"] == "decide":
            # Native request ids repeat (per turn / per connection): key by the exact callback.
            decided.add((params["approval_worker_id"], params["turn_id"], params["request_id"]))
    for item in journal.events:
        kind, data = item["event"]["kind"], item["event"]["data"]
        turn = turns.get(data.get("operation_id") or "")
        if kind == "session" and data.get("native_id"):
            native_id = data["native_id"]
        elif kind == "model" and data.get("model_effective"):
            model_effective = data["model_effective"]
        elif kind == "background":
            background = bool(data.get("active"))
        elif kind == "approval" and turn is not None:
            exact = (item["worker_id"], data["operation_id"], data["request_id"])
            pending[exact] = {**data, "worker_id": item["worker_id"]}
        elif kind == "approval_cancelled":
            for exact in [
                k for k in pending if k[0] == item["worker_id"] and k[2] == data["request_id"]
            ]:
                pending.pop(exact)
        elif turn is None:
            continue
        elif kind == "text":
            if data["partial"]:
                turn["partial"] = (turn["partial"] + data["text"])[-SNAPSHOT_TEXT:]
            else:
                turn["reply"] = (turn["reply"] + data["text"])[-SNAPSHOT_TEXT:]
        elif kind == "tool":
            entry = {
                "name": data.get("tool") or "tool",
                "outcome": data.get("state") or ("completed" if data.get("completed") else ""),
                "summary": (data.get("summary") or "")[:500],
                "id": data["item_id"],
            }
            existing = next((t for t in turn["tools"] if t["id"] == entry["id"]), None)
            if existing is None:
                turn["tools"].append(entry)
            else:
                existing.update({k: v for k, v in entry.items() if v})
        elif kind == "turn_completed":
            turn["completed"] = True
            turn["status"] = "done" if data["state"] == "completed" else "failed"
            if data["state"] == "interrupted":
                turn["code"] = "interrupted"
            if data.get("error"):
                turn["reason"] = data["error"][:2000]
            if data.get("text") and not turn["reply"]:
                turn["reply"] = data["text"]
            background = bool(data.get("background_active"))
            for key, value in list(pending.items()):
                if value.get("operation_id") == turn["turn_id"]:
                    pending.pop(key)
        elif kind == "error" and data.get("action") == "turn/start":
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
    for turn in turns.values():
        if not turn["reply"]:
            turn["reply"] = turn["partial"]
        if turn["handoff"] == "not_sent":
            turn["status"], turn["reason"] = "failed", "the agent did not accept this turn"
        elif not turn["completed"] and turn["status"] == "pending":
            if turn["worker_id"] != live_worker:
                turn["status"], turn["code"] = "failed", "uncertain"
                turn["reason"] = "the native worker ended before this turn settled"
            else:
                in_flight = turn["turn_id"]
    for exact, approval in pending.items():
        request_id = exact[2]
        if exact in decided or approval["worker_id"] != live_worker:
            continue
        turn = turns.get(approval["operation_id"])
        if turn is None or turn["turn_id"] != in_flight:
            continue
        turn["status"] = "awaiting_approval"
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
                "choices": [
                    c
                    for c in approval["choices"]
                    if c != "approve" or approval.get("complete") is True
                ],
                "complete": approval.get("complete") is True,
                "operator_only": True,
                "context": dict(turn["context"]),
            }
        )
    for turn in turns.values():
        for key in ("partial", "handoff", "worker_id", "completed"):
            turn.pop(key, None)
    return {
        "turns": list(turns.values()),
        "revision": journal.revision,
        "cwd": journal.header["cwd"],
        "in_flight": in_flight,
        "model": record["request"]["model"],
        "model_effective": model_effective,
        "pending_requests": requests,
        "native": {
            "native_id": native_id,
            "worker": live_worker,
            "background_active": background,
        },
    }


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
    return project(_journal(prov, session_id), record, _live_worker(session_id))


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
) -> dict:
    """``attachments``: pictures ALREADY admitted by ``native_images.admit`` (#1332 Phase 3)."""
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    try:
        native_protocol.validate_text(text, allow_empty=bool(attachments))
    except native_protocol.ProtocolError as exc:
        raise NativeError(422, str(exc)) from None
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    params = {"text": text, "context": context or {}}
    if attachments:
        if not kinds.API_IMAGE_INPUT.get(prov.manifest.api.kind, False):
            raise NativeError(422, "this client takes no images")
        params["attachments"] = attachments
    immutable = native_ipc.normalize_immutable_request({"action": "submit", "params": params})
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
            )
            break
        except NativeError as exc:
            # Unsolicited observations move the revision; only an implicit expectation retries.
            if expected_revision is None and "revision changed" in exc.detail:
                continue
            raise
    else:
        raise NativeError(409, "the conversation kept changing; retry")
    if receipt["handoff"] == "not_sent":
        raise NativeError(
            409,
            "this turn was not sent: the agent is busy with another turn"
            + (", or an attached image changed" if attachments else ""),
        )
    return receipt


async def _existing_generation_call(session_id: str, action: str, params: dict) -> dict:
    """Decisions and interrupts target the RUNNING generation only: a successor could never
    answer the old callback or interrupt the old turn, so nothing is relaunched for them."""
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
    execution_admission=None,
) -> dict:
    if execution_admission is not None:
        raise NativeError(
            409, "native clients cannot enforce caller authority across processes yet"
        )
    if decision not in {"approve", "reject", "cancel"}:
        raise NativeError(422, "decision must be approve, reject or cancel")
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
        ) != (request_id, decision, turn_id, user):
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
    if decision not in approval["choices"]:
        raise NativeError(
            409,
            "this request could not be presented completely; it can only be declined"
            if decision == "approve"
            else "that decision is not offered for this request",
        )
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


def _gone(session_id: str, worker_id: str) -> bool:
    if _identity(worker_id) is not None:
        return classify(session_id, worker_id) == "gone"
    # Never pinned: launch ran under this lock, so nothing can still be starting under the
    # name. Only a unit name systemd does not know at all counts as gone.
    observation = HOST.show(native_containment.WorkerIdentity(worker_id))
    return observation is not None and observation.load_state == "not-found"


def _stop_sync(engine_id: str, session_id: str) -> str:
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is None:
            raise NativeError(404, "no such native session")
        record["closed"] = True
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


async def stop(engine_id: str, session_id: str) -> dict:
    await asyncio.to_thread(partial(_session, engine_id, session_id, retiring_ok=True))
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
    return {"containment": await asyncio.to_thread(_stop_sync, engine_id, session_id)}


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
