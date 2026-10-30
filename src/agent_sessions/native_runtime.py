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
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path

from . import (
    chat_store,
    launchtiming,
    model_choice,
    native_attachments,
    native_containment,
    native_grants,
    native_ipc,
    native_journal,
    native_memory,
    native_ownership,
    native_protocol,
    native_questions,
    native_segments,
    native_state,
    risk_marks,
    secrets_broker,
)
from .plugins import api_source, kinds, storage

log = logging.getLogger("agent_sessions.native_runtime")

READY_TIMEOUT = 120.0
TOOLS_SHOWN = 64  # the newest calls of a turn a snapshot sends (`structured_runtime._tools`)
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
    "copilot-acp": native_protocol.COPILOT_MIN_VERSION,
}
# The vendor CLI writes where ITS defaults point. A BattleLab store override that the vendor
# would not also use cannot be honoured without silently reading one history and writing another.
# (store path name or None for the root, the vendor's default under HOME). opencode's history is
# one SQLite file, which its console provider resolves through `store.path_env` (#1312).
_VENDOR_STORES = {
    "codex-app-server": (None, ".codex/sessions"),
    "claude-stream-json": (None, ".claude"),
    "opencode-acp": ("db", ".local/share/opencode/opencode.db"),
    # The console copilot manifest names no store path_env, so both always address ~/.copilot.
    "copilot-acp": (None, ".copilot"),
}
#: Adapters whose native history id is minted by the agent and bound by the worker from its
#: creation response (Codex `thread/start`, ACP `session/new`); Claude's is ours.
_ADOPTED = frozenset({"codex-app-server", "opencode-acp", "copilot-acp"})
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
    def __init__(
        self,
        status: int,
        detail: str,
        reason: str | None = None,
        inventory: dict | None = None,
    ):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        # A closed refusal reason the pane acts on (#1644, `native_ipc.SEND_NOW_REFUSALS`).
        self.reason = reason
        # A refused Stop (#1724): the background work running now, for the pane to confirm.
        self.inventory = inventory
        # The request may have reached the worker but no answer came back (a lost connection,
        # a timeout): the only failure that says nothing about what the worker decided (#1724).
        self.transport = False


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


#: Crash cleanup, not launch-critical (#1505): at most one sweep per this many seconds per
#: process. A dead generation's lease can outlive it this much longer; a live one is never swept.
SWEEP_INTERVAL = 60.0
_SWEPT_AT: float | None = None
_SWEEP_LOCK = threading.Lock()


def _sweep_leases(release: Path | None) -> None:
    """Drop leases of generations whose pinned processes provably no longer run, in EVERY release.

    Workers drop their own lease on a clean exit; this catches crashes and kills. A lease whose
    generation has no pinned invocation yet (being launched right now by another session) is
    never touched. Throttled to once per ``SWEEP_INTERVAL`` per process (#1505): it walks every
    release and asks systemd about each lease, and every launch used to pay for that.
    """
    global _SWEPT_AT
    if release is None:
        return
    with _SWEEP_LOCK:
        now = time.monotonic()
        if _SWEPT_AT is not None and now - _SWEPT_AT < SWEEP_INTERVAL:
            return
        _SWEPT_AT = now
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


def _entrypoint(source) -> tuple[str, dict]:
    """The provenance-checked binary a generation spawns, and how it was selected (#1538): a
    queued message is admitted later against that selection, never against the path alone."""
    from .plugins import api_source

    try:
        ep = source.entrypoint_selection()
    except Exception as exc:  # noqa: BLE001 — EngineError/provenance refusal
        raise NativeError(409, str(exc)) from None
    return ep.path, api_source.selection(ep, source.trust)


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


def _specialist_claim(record: dict, adapter: str):
    """The specialist slot for one generation of a session started from one (#1693 S4b), or None:
    the same version, renewed or re-reserved (`specialist_launch.for_generation`). Refused as a
    `NativeError`; an adapter that can't carry its skills is refused and the slot freed."""
    specialist = record["request"].get("specialist")
    if specialist is None:
        return None
    from . import specialist_launch

    try:
        claim = specialist_launch.for_generation(
            record["session_key"], specialist, record.get("specialist_slot")
        )
    except specialist_launch.Refused as exc:
        raise NativeError(409, exc.detail) from None
    if claim.bundle is not None and not kinds.API_MCP_KINDS.get(adapter):
        specialist_launch.abandon(claim)
        raise NativeError(422, "this agent cannot load skills")
    return claim


def _launch_locked(session_id: str, record: dict, prov, binding, *, mode: str, claim=None) -> str:
    """Mint, persist intent, lease, launch and pin one generation. Caller holds the lock.
    ``claim``: a specialist slot the caller already took for it; otherwise one is taken here when
    the session was started from a specialist (#1693 S4b). Whatever refuses or fails BEFORE the
    spawn — a check, the interpreter, the entrypoint's provenance, a state write — releases it;
    once a spawn may have happened, only proof does (a stop, a failed generation, reconcile)."""
    from . import specialist_launch

    held = {"claim": claim, "spawning": False}
    try:
        return _launch_generation(session_id, record, prov, binding, mode=mode, held=held)
    except BaseException:
        if not held["spawning"]:
            specialist_launch.abandon(held["claim"])
        raise


def _launch_generation(
    session_id: str, record: dict, prov, binding, *, mode: str, held: dict
) -> str:
    from . import agent_tools, memory, resource_limits

    if _permission_change(record).get("state") == "stopping":
        raise NativeError(409, "finish enabling Skip permissions before sending another message")
    if _start_state(record) not in (None, STARTING, STARTED):
        # Two-phase skip start (#1339; state machine, operator decision on #1341): a skip
        # creation's generation is born only from `starting` (the `/start` that set it, under this
        # same lock) or `started`. Fail closed here, where every generation is born.
        raise NativeError(409, "this session has not been started; start or discard it")

    policy = resource_limits.values()
    worker = native_containment.WorkerIdentity.mint()
    python, release = interpreter()
    state_dir = native_state.root()
    runtime = HOST.runtime_dir()
    adapter = record["adapter"]
    binary, selection = _entrypoint(binding.source)
    config = {
        "version": 1,
        "worker_id": worker.worker_id,
        "connection_id": str(uuid.uuid4()),
        "capability": native_ipc.Capability.create()._value,
        "session_key": record["session_key"],
        "adapter": adapter,
        "source_engine": record["source_engine"],
        "binary": binary,
        "entrypoint": selection,
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
    }
    # Started from a specialist (#1693 S4b): each generation is a new agent process, so it takes
    # its own slot — the same version, re-reserved — and its skill bundle here, once everything
    # above that can refuse the launch has passed. Released at once if nothing is spawned; after a
    # spawn, only on proof (a stop, a failed generation, reconcile).
    specialist = record["request"].get("specialist")
    if held["claim"] is None:
        held["claim"] = _specialist_claim(record, adapter)
    claim = held["claim"]
    if claim is not None:
        record["specialist_slot"] = {"reservation_id": claim.reservation_id, "owner": claim.owner}
        config["skills_bundle"] = claim.bundle
    # Shared project memory (#1441), bound per GENERATION: every resume or relaunch is a new
    # agent process and asks again, so a toggle reaches the next generation, never a live one.
    # Bound last, once everything above that can refuse the launch has passed.
    config["memory_env"] = memory.launch_env(
        record["session_key"],
        record["request"]["cwd"],
        kind="api",
        specialist=specialist["id"] if specialist is not None else None,
    )
    # The memory tool (#1506), only beside the paths: the manifest's kind, which the worker
    # checks against its protocol (`agent_tools.api_kind`) and shapes into that protocol's own
    # slot.
    config["memory_tool"] = agent_tools.mcp_kind(prov.manifest) if config["memory_env"] else None
    # The secrets capability (#1786), per GENERATION like memory: bound to this worker, so it is
    # live exactly while this generation's invocation is the active one.
    config["secrets_env"] = secrets_broker.launch_env(
        record["session_key"],
        record["session_key"],
        record["request"]["cwd"],
        engine=record["session_key"].partition(":")[0],
        kind="api",
        worker_id=worker.worker_id,
    )
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
    launchtiming.mark("config")
    _sweep_leases(release)
    launchtiming.mark("sweep")
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
    launchtiming.mark("lease")
    held["spawning"] = True  # from here a worker may exist: its slot is released only on proof
    launched = HOST.launch(argv)
    launchtiming.mark("spawn")
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
        memory_gib=policy["api_memory_gib"],  # names the limit if it kills this one (#1643)
    )
    launchtiming.mark("pin")
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
            lost = NativeError(
                503, "the native worker connection was lost; the request may still be applied"
            )
            lost.transport = True
            raise lost from None
        raise _Unreachable(str(exc)) from None
    except native_ipc.IPCError:
        raise NativeError(503, "the native worker answered with an invalid frame") from None
    except TimeoutError:
        silent = NativeError(
            503, "the native worker did not answer; the request may still be applied"
        )
        silent.transport = True
        raise silent from None
    if "error" in response:
        error = response["error"]
        raise NativeError(
            _ERRORS.get(error["code"], 503), error["message"], reason=error.get("reason")
        )
    return response["result"]


class _Unreachable(Exception):
    pass


_PRESSURE_TTL = 5.0
_pressure_cache: tuple[Path, float, dict[str, dict]] | None = None
_pressure_lock = threading.Lock()


def memory_pressures() -> dict[str, dict]:
    """Session key → the memory warning of its CURRENT generation (#1643), for the session list.

    One pass over the session records (local files, no subprocess, no `/proc`), cached for a few
    seconds so a list poll never re-reads them per row. Only open sessions' current workers are
    asked, through `native_memory.public`: fresh, of the pinned invocation, still running. An
    unreadable record or sidecar is skipped (no warning), never a healthy reading."""
    global _pressure_cache
    now = time.monotonic()
    try:
        root = native_state.root()
        with _pressure_lock:
            cached = _pressure_cache
            if cached is not None and cached[0] == root and now - cached[1] < _PRESSURE_TTL:
                return cached[2]
        paths = list(root.glob("*.json"))
    except (OSError, native_state.StateError, ValueError):
        return {}
    shown: dict[str, dict] = {}
    for path in paths:
        try:
            record = native_state.read(path)
            if not isinstance(record, dict) or record.get("closed") is True:
                continue
            worker, key = record.get("current_worker"), record.get("session_key")
            if not isinstance(worker, str) or not isinstance(key, str):
                continue
            warning = native_memory.public(native_state.read_lifecycle(worker))
        except (OSError, ValueError, native_state.StateError):
            continue
        if warning is not None:
            shown[key] = warning
    with _pressure_lock:
        _pressure_cache = (root, now, shown)
    return shown


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
    record_launch: bool = False,
    specialist: tuple[str, int] | None = None,
) -> str | None:
    """The worker id of a new launch (a generation to await), or None when nothing launched.

    ``record_launch`` (#1505, the HTTP route only): the caller returns at launch, so the new
    generation's launch view (`starting`) is written in the same locked section that launched it,
    for the background readiness waiter to settle.

    ``bypass`` (#1339) is part of the immutable create request: recorded only when set, so a
    guarded request is byte-identical to one written before the field existed, and a replay of
    the same operation id with the other mode conflicts instead of flipping it. ``effort`` (#1407)
    follows the same rule: recorded only when chosen, and part of the request a replay compares."""
    prov = _api_provider(engine_id)
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        raise NativeError(409, str(exc)) from None
    launchtiming.mark("resolve")
    adapter = prov.manifest.api.kind
    if bypass and adapter not in kinds.API_BYPASS_KINDS:
        raise NativeError(422, "this client cannot skip permission prompts yet")
    session_key = f"{engine_id}:{session_id}"
    # The model is checked against the client's own list NOW, at the write boundary (#1313): an id
    # the client does not list is refused before anything exists. Model and effort are checked
    # against ONE snapshot of that list, and an id absent from a stale one is re-asked once
    # (#1503). A refusal only blocks a NEW creation; an exact replay compares the request as sent.
    refused = None
    try:
        model, effort = model_choice.select_api_choice(prov, model, effort)
    except model_choice.ModelRefused as exc:
        refused = exc
    launchtiming.mark("model")
    request = {"cwd": cwd, "model": model, "adapter": adapter}
    if effort is not None:
        request["effort"] = effort
    if bypass:
        request["bypass"] = True
    if specialist is not None:
        # Started from a specialist (#1693 S4b): part of the immutable request, so a replay with
        # another (or none) conflicts, and every generation re-reserves THIS version.
        request["specialist"] = {"id": specialist[0], "version": specialist[1]}
    root = _journal_root(prov)
    with native_state.session_lock(session_id):
        launchtiming.mark("lock")
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
                return None  # exact replay: observe, never launch a second creation
        else:
            if refused is not None:
                raise NativeError(422, refused.detail)
            if specialist is not None:
                from . import specialist_launch

                try:
                    specialist_launch.check_api(
                        engine_id,
                        *specialist,
                        model=model,
                        effort=effort,
                        carries_skills=bool(kinds.API_MCP_KINDS.get(adapter)),
                    )
                except specialist_launch.Refused as exc:
                    raise NativeError(422, exc.detail) from None
            ready, reason = readiness(prov)
            launchtiming.mark("version")
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
        launchtiming.mark("record")
        if record["request"].get("bypass"):
            # A skip creation reserves and launches NOTHING from create — not the first call, not
            # a replay, not after an interrupted start (Hermes 5908): only `start` does (#1339).
            return None
        worker_id = _reserve_and_launch_locked(session_id, record, prov, binding)
        if record_launch:
            # Same lock, same write sequence as the launch: the route's caller sees `starting`
            # from its first snapshot, and only this generation's waiter may settle it.
            _write_launch(record, worker_id, STARTING)
            native_state.write_session(session_id, record)
        return worker_id


def _reserve_and_launch_locked(
    session_id: str, record: dict, prov, binding, *, mode: str = "create"
) -> str:
    """Commit ownership and launch the FIRST generation of a creation (or a retried skip start's
    successor). Caller holds the lock."""
    from . import specialist_launch
    from .engines import registry

    session_key = record["session_key"]
    source = native_ownership.source_identity(binding.source)
    if source is None:
        raise NativeError(409, "the native source has no recognised history store")
    # A specialist's slot first (#1693 S4b): a refusal (at capacity, archived) must leave no
    # pending ownership behind — an unresolved creation refuses the source's console launches.
    claim = _specialist_claim(record, record["adapter"])
    try:
        with storage.locked("launch", wait=30):
            launchtiming.mark("launch_lock")
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
    except BaseException:
        specialist_launch.abandon(claim)
        raise
    launchtiming.mark("admit")
    return _launch_locked(session_id, record, prov, binding, mode=mode, claim=claim)


async def create(
    engine_id: str,
    cwd: str,
    *,
    session_id: str,
    model: str | None = None,
    effort: str | None = None,
    bypass: bool = False,
    execution_admission=None,
    wait_ready: bool = True,
    specialist: tuple[str, int] | None = None,
) -> str:
    """Create (or replay) one API session. ``wait_ready=True`` (every internal caller) returns
    once the worker reports ready, as it always has. ``wait_ready=False`` (#1505, the HTTP route
    only) returns as soon as the generation is recorded and launched; a background waiter then
    applies today's exact failure handling and settles the session's ``launch`` view."""
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
    timings = launchtiming.start(engine_id, "create")
    # Set once a background waiter owns the rest of this start (and its timing line).
    handed: dict = {}
    launch = partial(
        _create_sync,
        engine_id,
        session_id,
        cwd,
        model,
        bypass,
        effort,
        record_launch=not wait_ready,
        specialist=specialist,
    )
    try:
        try:
            if not wait_ready:
                # The route answers at launch; the waiter is installed by the launch itself.
                await _launch_handed_off(launch, engine_id, session_id, "create", handed)
                launchtiming.mark("respond")
                return session_id
            launched = await asyncio.to_thread(launch)
        except (
            native_state.StateError,
            native_ownership.OwnershipError,
            storage.StateError,
        ) as exc:
            raise NativeError(503, str(exc)) from None
        except native_journal.JournalError as exc:
            raise NativeError(503, exc.detail) from None
        if not launched:
            return session_id  # a replay, or a skip creation: nothing was launched
        gen = await asyncio.to_thread(_generation, session_id)
        if gen is None:
            return session_id
        try:
            await _wait_ready(gen.worker_id)
            launchtiming.mark("ready")
        except NativeError:
            # A creation that never became ready must not keep its reservation (which
            # holds new console launches for the source) — stop proves it gone first.
            with contextlib.suppress(Exception):
                await stop(engine_id, session_id)
            launchtiming.mark("failed")
            raise
        return session_id
    finally:
        if not handed:
            timings.emit()


# --- start (the second phase of a skip-permissions creation, #1339) ---------------------------

#: How long a skip creation waits for its `start` before it is closed, never launched.
PENDING_START_TTL = 600.0

#: How long a `starting` with no generation yet reads as a start still in progress (#1647). That
#: record is written before admission; `_start_sync` holds the session lock until it replaces it
#: with `starting(W)` or `failed`, after at most the 30 s `launch` lock wait plus admission and the
#: sweep (measured up to ~17 s together on a loaded host). Past this bound the process that wrote
#: it is presumed gone, and the view offers Start again as before.
START_ADMISSION_TIMEOUT = 120.0


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


def pending_start_view(record: dict, live_worker: str | None = None) -> dict:
    """What a snapshot says about a skip creation's start.

    Starting and failed are separate (#1505): a launched worker that is not ready yet is
    `start_in_flight` (no Start, and never "nothing has run"); a failed start is `start_failed`
    with its reason when the launch view has one. A `starting` whose worker is gone still reads
    incomplete, so Start reconciles it as before.

    A `starting` with no generation yet is a start still inside admission (#1647): `/start` wrote
    it before waiting on the launch lock, so while it is recent it is in flight, never "didn't
    finish". A discarded creation says so (#1647): it takes no message and never runs."""
    state = _start_state(record)
    if state is None or state == STARTED:
        return {"pending_start": False}
    if state == DISCARDED:
        return {"pending_start": False, "start_discarded": True}
    if state == EXPIRED:
        return {"pending_start": False, "start_expired": True}
    if state == PENDING:
        return {"pending_start": True, "start_expires_at": record["start"]["expires_at"]}
    if state == STARTING:
        gen = record["start"].get("gen")
        if gen is not None and gen == live_worker:
            return {"pending_start": False, "start_in_flight": True}
        at = record["start"].get("at")
        admitting = isinstance(at, int | float) and 0 <= time.time() - at < START_ADMISSION_TIMEOUT
        if gen is None and admitting:
            return {"pending_start": False, "start_in_flight": True}  # still being admitted
        return {"pending_start": True, "start_incomplete": True}  # a dead starting: Start again
    view = launch_view(record)
    reason = view.get("reason") if view is not None and view["state"] == FAILED else None
    return {
        "pending_start": True,
        "start_incomplete": True,
        "start_failed": True,
        **({"start_failed_reason": reason} if reason else {}),
    }


def _start_sync(engine_id: str, session_id: str, record_launch: bool = False) -> str | None:
    """The worker id this call launched, or None when the start is already complete.

    ``record_launch`` (#1505, the HTTP route only): the launched generation's view is recorded
    as `starting` in the same write as `starting(W)`, and a refusal BEFORE any worker exists is
    recorded as a `failed` launch view (``gen: null``) under the session lock before it raises —
    no waiter exists to publish it, and the route's caller may never see the response. The start
    state itself moves exactly as without it, so Start again works."""
    prov, _ = _session(engine_id, session_id)
    unresolved = None
    try:
        binding = api_source.resolve(prov)
    except api_source.SourceError as exc:
        if not record_launch:
            raise NativeError(409, str(exc)) from None
        unresolved, binding = str(exc), None
    launchtiming.mark("resolve")
    with native_state.session_lock(session_id):
        launchtiming.mark("lock")
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
        try:
            if unresolved is not None:
                raise NativeError(409, unresolved)
            if binding.source.engine_id != record["source_engine"]:
                raise NativeError(409, "the session's original native source is not available")
            ready, reason = readiness(prov)
            launchtiming.mark("version")
            if not ready:
                raise NativeError(409, reason or "native client is not ready")
            mode = "create"
            if record.get("generations"):
                # A retry after a failed launch: every earlier generation must be PROVED gone.
                if _settle_generations(session_id, record):
                    raise NativeError(
                        409,
                        "the previous start's worker is not proved gone; discard and start anew",
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
        except NativeError as exc:
            if record_launch:
                _write_launch(record, None, FAILED, exc.detail)
                native_state.write_session(session_id, record)
            raise
        # The operator's start, durable BEFORE anything is reserved or launched.
        _set_start(record, STARTING, None)
        native_state.write_session(session_id, record)
        try:
            worker_id = _reserve_and_launch_locked(session_id, record, prov, binding, mode=mode)
        except Exception as exc:
            _set_start(record, FAILED, None)
            if record_launch:
                _write_launch(
                    record,
                    None,
                    FAILED,
                    exc.detail
                    if isinstance(exc, NativeError)
                    else "the native worker could not be started",
                )
            native_state.write_session(session_id, record)
            raise
        _set_start(record, STARTING, worker_id)
        if record_launch:
            _write_launch(record, worker_id, STARTING)
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


async def start(engine_id: str, session_id: str, *, wait_ready: bool = True) -> str:
    """Launch a skip creation's generation. ``wait_ready`` as for :func:`create` (#1505)."""
    timings = launchtiming.start(engine_id, "start")
    handed: dict = {}
    launch = partial(_start_sync, engine_id, session_id, record_launch=not wait_ready)
    try:
        try:
            if not wait_ready:
                # The route answers at launch; the waiter is installed by the launch itself.
                await _launch_handed_off(launch, engine_id, session_id, "start", handed)
                launchtiming.mark("respond")
                return session_id
            worker_id = await asyncio.to_thread(launch)
        except (
            native_state.StateError,
            native_ownership.OwnershipError,
            storage.StateError,
        ) as exc:
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
            launchtiming.mark("failed")
            raise
        if not await asyncio.to_thread(_finish_start, session_id, worker_id, True):
            launchtiming.mark("failed")
            raise NativeError(409, "this start was discarded or replaced before it completed")
        launchtiming.mark("ready")
        return session_id
    finally:
        if not handed:
            timings.emit()


# --- the launch view and background readiness (#1505) ----------------------------------------
#
# The HTTP create/start return once a generation is recorded and launched (`wait_ready=False`).
# Readiness then moves to the snapshot: `record["launch"] = {gen, state, reason, since}`, one
# bounded, GENERATION-SCOPED view. `starting` is written with the launch, under the session lock.
# The generation's background waiter owns it from then on and writes ONLY that field, and only
# while it still names its generation as `starting` (`_settle_launch`): it never touches `closed`,
# `current_worker`, the reservation or start admission, so a failure recorded on a record its own
# `stop` just closed reopens nothing. A replacement start rewrites `gen`, and the operator's
# Discard drops the view, so a stale waiter's write is a no-op. A refusal before any worker
# exists is recorded by `_start_sync` with `gen: null`.

LAUNCH_READY = "ready"
LAUNCH_REASON_MAX = 200
RESTARTED = "BattleLab restarted before the worker became ready"
UNCONFIRMED_STOP = "couldn't confirm the worker stopped"
#: Background readiness waiters (and the startup reconciliation pass), drained at shutdown.
_READINESS_TASKS: set[asyncio.Task] = set()


def _launch_reason(reason: object) -> str:
    text = " ".join(str(reason or "the native worker did not become ready").split())
    return text if len(text) <= LAUNCH_REASON_MAX else text[: LAUNCH_REASON_MAX - 1] + "…"


def _write_launch(record: dict, gen: str | None, state: str, reason: object = None) -> None:
    view = {"gen": gen, "state": state, "since": time.time()}
    if state == FAILED:
        view["reason"] = _launch_reason(reason)
    record["launch"] = view


def launch_view(record: dict) -> dict | None:
    """The snapshot's launch view, or None when there is none (or it is unreadable)."""
    view = record.get("launch")
    if not isinstance(view, dict) or view.get("state") not in {STARTING, LAUNCH_READY, FAILED}:
        return None
    gen = view.get("gen")
    if gen is not None and not isinstance(gen, str):
        return None
    since = view.get("since")
    out = {
        "gen": gen,
        "state": view["state"],
        "since": since if isinstance(since, int | float) else None,
    }
    if view["state"] == FAILED:
        out["reason"] = _launch_reason(view.get("reason"))
    return out


def _settle_launch(session_id: str, gen: str, state: str, reason: object = None) -> bool:
    """Owned-generation settlement: `launch` only, only while it is still `gen`'s `starting`."""
    with native_state.session_lock(session_id):
        record = native_state.read_session(session_id)
        if record is None:
            return False
        view = record.get("launch")
        if not isinstance(view, dict) or view.get("gen") != gen or view.get("state") != STARTING:
            return False
        _write_launch(record, gen, state, reason)
        native_state.write_session(session_id, record)
        return True


#: Transient errors (a lock or state read that failed) are retried with these pauses before a
#: readiness owner gives up; the last failure is logged by the task's done-callback.
LAUNCH_RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0)
#: The slow in-process owner (#1505): every this many seconds, every `starting` launch view with
#: no live owner in this process gets one again — after an owner exhausted its retries, or a
#: launch whose record landed after a scan. Cancelled at shutdown.
RECONCILE_INTERVAL = 30.0
#: How long shutdown waits for launch threads still in flight before cancelling owners, so a
#: thread that is about to persist `starting` does so before the next process can scan.
LAUNCH_DRAIN_TIMEOUT = 75.0
#: Launch threads in flight (route create/start), keyed by nothing: drained, never cancelled.
_LAUNCHES: set[asyncio.Future] = set()
#: Worker id → the task that owns settling its launch view in this process.
_OWNERS: dict[str, asyncio.Task] = {}
_sleep = asyncio.sleep  # one seam for the retry / periodic clocks (tests use a fake clock)


def _owned_task(coro, what: str, gen: str | None = None) -> asyncio.Task | None:
    """A drained background task whose unexpected failure is logged, never lost silently. With
    ``gen``, it is that launch's ONE owner in this process: a second owner is not started."""
    if gen is not None and gen in _OWNERS and not _OWNERS[gen].done():
        coro.close()
        return None
    task = asyncio.create_task(coro)
    _READINESS_TASKS.add(task)
    if gen is not None:
        _OWNERS[gen] = task

    def done(finished: asyncio.Task) -> None:
        _READINESS_TASKS.discard(finished)
        if gen is not None and _OWNERS.get(gen) is finished:
            del _OWNERS[gen]
        if not finished.cancelled() and finished.exception() is not None:
            log.error("%s failed", what, exc_info=finished.exception())

    task.add_done_callback(done)
    return task


def _spawn_waiter(
    engine_id: str, session_id: str, worker_id: str, kind: str
) -> asyncio.Task | None:
    return _owned_task(
        _await_launch(engine_id, session_id, worker_id, kind),
        "an API session's readiness waiter",
        gen=worker_id,
    )


async def _launch_handed_off(launch, engine_id: str, session_id: str, kind: str, handed: dict):
    """Run a route's launch thread so that ownership survives the request being cancelled.

    A cancelled request does not stop the thread: it may still launch a `starting` generation.
    The waiter is therefore installed from the THREAD's completion (a done-callback on a shielded
    task), never from the request's continuation, so every launched generation gets its owner.
    Shutdown waits for these (bounded) instead of cancelling them: cancelling the task would not
    stop the thread, only lose its handoff."""
    task = asyncio.ensure_future(asyncio.to_thread(launch))
    _LAUNCHES.add(task)

    def hand_off(done: asyncio.Future) -> None:
        _LAUNCHES.discard(done)
        if done.cancelled() or done.exception() is not None:
            return  # nothing launched
        worker_id = done.result()
        if worker_id:
            handed["waiter"] = True
            _spawn_waiter(engine_id, session_id, worker_id, kind)

    task.add_done_callback(hand_off)
    return await asyncio.shield(task)


def _emit_timings(outcome: str) -> None:
    launchtiming.mark(outcome)
    recorder = launchtiming.current()
    if recorder is not None:
        recorder.emit()


async def _retrying(step, what: str):
    """Run ``step()`` with bounded retries on transient errors. A `NativeError` is an answer,
    not a transient failure, unless it is a 503 (state or lock unavailable); cancellation is
    never retried. The last failure propagates (and is logged by the task's done-callback)."""
    for delay in (*LAUNCH_RETRY_DELAYS, None):
        try:
            return await step()
        except asyncio.CancelledError:
            raise
        except NativeError as exc:
            if exc.status != 503 or delay is None:
                raise
            log.warning("%s hit a transient error; retrying in %ss", what, delay, exc_info=True)
            await _sleep(delay)
        except Exception:
            if delay is None:
                raise
            log.warning("%s hit a transient error; retrying in %ss", what, delay, exc_info=True)
            await _sleep(delay)


def _start_is_started(session_id: str, worker_id: str) -> bool:
    record = native_state.read_session(session_id) or {}
    start = record.get("start") or {}
    return start.get("state") == STARTED and start.get("gen") == worker_id


async def _settle_once(engine_id: str, session_id: str, worker_id: str, kind: str) -> str:
    """One attempt to settle a launch; idempotent, so a transient failure can simply retry."""
    try:
        await _wait_ready(worker_id)
    except NativeError as exc:
        await asyncio.to_thread(
            _fail_owned_generation, engine_id, session_id, worker_id, kind, exc.detail
        )
        return "failed"
    if kind == "start":
        ok = await asyncio.to_thread(_finish_start, session_id, worker_id, True)
        # A retry after a transient error may find its own completion already recorded.
        if not ok and not await asyncio.to_thread(_start_is_started, session_id, worker_id):
            # It vanished after reporting ready (`_finish_start` recorded `failed`), or Discard / a
            # newer start moved the state on — then the generation fence makes this a no-op.
            await asyncio.to_thread(
                _settle_launch,
                session_id,
                worker_id,
                FAILED,
                "the native worker exited before it became ready",
            )
            return "failed"
    await asyncio.to_thread(_settle_launch, session_id, worker_id, LAUNCH_READY)
    return "ready"


async def _await_launch(engine_id: str, session_id: str, worker_id: str, kind: str) -> None:
    """One generation's readiness, after its route returned. Cancellation is NOT a failure: a
    cancelled waiter settles nothing (the worker is its own unit and may still come up), and the
    next app start's `reconcile_launches` settles the view instead. A transient read or lock
    error is retried (bounded), so the only owner of a launch does not vanish on a hiccup."""
    outcome = await _retrying(
        lambda: _settle_once(engine_id, session_id, worker_id, kind), "an API session's launch"
    )
    _emit_timings(outcome)


def _owns_launch(record: dict, worker_id: str, kind: str) -> bool:
    view = record.get("launch")
    if not isinstance(view, dict) or view.get("gen") != worker_id or view.get("state") != STARTING:
        return False
    if kind == "start":
        start = record.get("start") or {}
        return start.get("state") == STARTING and start.get("gen") == worker_id
    return True


def _stop_generation_locked(session_id: str, record: dict, worker_id: str) -> str:
    """Stop exactly ``worker_id``'s pinned invocation and prove it gone. Never any other
    generation of the session. The caller holds the session lock."""
    identity = _identity(worker_id)
    if identity is not None:
        observation = HOST.show(identity.worker)
        if observation is not None and observation.invocation_id == identity.invocation_id:
            with contextlib.suppress(native_containment.ContainmentError):
                HOST.stop(native_containment.stop_argv(identity, observation))
    deadline = time.monotonic() + STOP_TIMEOUT
    while True:
        if _gone(session_id, worker_id):
            life = native_state.read_lifecycle(worker_id)
            release = Path(life["release"]) if life.get("release") else None
            _lease(release, worker_id, remove=True)
            native_state.forget_config(worker_id)
            native_state.update_lifecycle(worker_id, phase="gone", gone_at=time.time())
            record["workers"] = [w for w in record.get("workers", []) if w != worker_id]
            native_state.write_session(session_id, record)
            return "gone"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return "live" if classify(session_id, worker_id) == "live" else "unknown"


def _release_specialist_slot(record: dict) -> None:
    """The record's session runs nothing (the caller holds that proof, under its lock): the slot
    its generation created is released (#1693 S4b). A re-taken slot names no owner and is left to
    reconcile, which settles it on its own proof."""
    slot = record.get("specialist_slot")
    if slot is not None:
        from . import specialist_launch

        specialist_launch.release_slot(slot)


def _fail_owned_generation(
    engine_id: str, session_id: str, worker_id: str, kind: str, reason: str
) -> bool:
    """The failure handling of a launch that never became ready, fenced BEFORE any side effect.

    Under the session lock, and only while ``worker_id`` still owns the launch view (and, for a
    skip start, the start state): close admission for that generation, stop THAT generation's
    worker only, then — as the waiting create/start always did — discharge an unused creation's
    reservation, or fail the start; finally record `failed` + reason. A replacement generation,
    a Discard or a settled view makes this a no-op that touches nothing (False)."""
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is None or not _owns_launch(record, worker_id, kind):
            return False
        if record.get("current_worker") == worker_id:
            record["closed"] = True
        native_state.write_session(session_id, record)
        contained = _stop_generation_locked(session_id, record, worker_id)
        if contained == "gone" and kind == "create" and not record.get("workers"):
            _discharge_if_unused(engine_id, session_id, record)
        if contained == "gone" and not record.get("workers"):
            # Nothing of this session runs (proved under its lock): its specialist slot is free.
            _release_specialist_slot(record)
        if kind == "start":
            _set_start(record, FAILED, None)
        if contained != "gone":
            reason = f"{reason}; {UNCONFIRMED_STOP}"
        _write_launch(record, worker_id, FAILED, reason)
        native_state.write_session(session_id, record)
        return True


async def drain_readiness() -> None:
    """Shutdown: first let launch threads in flight finish (bounded), so their `starting` view is
    on disk before this process goes — the next start's scan settles it. Then cancel every owner
    without settling it (see `_await_launch`), and await them."""
    launches = [task for task in _LAUNCHES if not task.done()]
    if launches:
        _done, pending = await asyncio.wait(launches, timeout=LAUNCH_DRAIN_TIMEOUT)
        if pending:
            log.warning("%d API session launch(es) still in flight at shutdown", len(pending))
    tasks = list(_READINESS_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _starting_launches() -> tuple[list[tuple[str, str, str, str]], int]:
    """(engine, session id, worker id, kind) for every record whose launch view is `starting`,
    and how many records could not be read (the caller retries those)."""
    found, unreadable = [], 0
    paths = sorted(native_state.root().glob("*.json"))
    for path in paths:
        try:
            record = native_state.read(path)
        except (OSError, ValueError, native_state.StateError):
            unreadable += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("request"), dict):
            continue
        view = launch_view(record)
        if view is None or view["state"] != STARTING or not view["gen"]:
            continue
        engine, _, session_id = str(record.get("session_key", "")).partition(":")
        if not engine or session_id != path.stem:
            continue
        kind = "start" if record["request"].get("bypass") else "create"
        found.append((engine, session_id, view["gen"], kind))
    return found, unreadable


def _invocation_live(worker_id: str) -> bool:
    """The generation's pinned invocation is the unit's active one (systemd's word, not a file)."""
    identity = _identity(worker_id)
    if identity is None:
        return False
    observation = HOST.show(identity.worker)
    return (
        observation is not None
        and observation.active_state == "active"
        and observation.invocation_id == identity.invocation_id
    )


def _restart_verdict(worker_id: str) -> str:
    """ready / failed / starting for a launch an earlier app process left `starting`. A `ready`
    lifecycle counts only while that generation's worker is still live: a worker that reported
    ready and then died left the file behind."""
    phase = native_state.read_lifecycle(worker_id).get("phase")
    if phase == "ready":
        return LAUNCH_READY if _invocation_live(worker_id) else FAILED
    if phase in {"exited", "refused", "launch_unknown", "gone"} or _proved_gone(worker_id):
        return FAILED
    return STARTING


#: Why a launch an owner found already dead failed, when no earlier process is to blame.
STOPPED_BEFORE_READY = "the native worker stopped before it became ready"


async def _reconcile_once(
    engine_id: str, session_id: str, worker_id: str, kind: str, reason: str
) -> None:
    verdict = await asyncio.to_thread(_restart_verdict, worker_id)
    if verdict == LAUNCH_READY:
        if kind == "start":
            ok = await asyncio.to_thread(_finish_start, session_id, worker_id, True)
            if not ok and not await asyncio.to_thread(_start_is_started, session_id, worker_id):
                await asyncio.to_thread(_settle_launch, session_id, worker_id, FAILED, reason)
                return
        await asyncio.to_thread(_settle_launch, session_id, worker_id, LAUNCH_READY)
    elif verdict == FAILED:
        await asyncio.to_thread(
            _fail_owned_generation, engine_id, session_id, worker_id, kind, reason
        )
    else:
        # Still starting: this owner waits for it (one owner per launch in this process).
        await _settle_once(engine_id, session_id, worker_id, kind)


async def reconcile_launches(*, periodic: bool = False) -> None:
    """Give every `starting` launch view without an owner in this process a retrying owner.

    Run once at startup (``RESTARTED`` names the earlier process), and with ``periodic`` it keeps
    going every ``RECONCILE_INTERVAL`` until shutdown: an owner that exhausted its retries, an
    unreadable record, or a record that landed after a scan is picked up again in-process."""
    reason = RESTARTED
    while True:
        try:
            found, unreadable = await asyncio.to_thread(_starting_launches)
        except Exception:  # noqa: BLE001 — the state directory itself: the next pass lists again
            log.warning("launch reconciliation could not list API sessions", exc_info=True)
            found, unreadable = [], 1
        for engine_id, session_id, worker_id, kind in found:
            _owned_task(
                _retrying(
                    partial(_reconcile_once, engine_id, session_id, worker_id, kind, reason),
                    "launch reconciliation",
                ),
                "launch reconciliation for an API session",
                gen=worker_id,
            )
        if unreadable:
            log.warning("launch reconciliation could not read %d API session(s)", unreadable)
        if not periodic:
            return
        reason = STOPPED_BEFORE_READY  # later passes: this process was here all along
        await _sleep(RECONCILE_INTERVAL)


def start_reconciliation() -> asyncio.Task:
    """The startup pass, then the slow periodic owner, as one drained task."""
    return _owned_task(reconcile_launches(periodic=True), "launch reconciliation")


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
        view = launch_view(record)
        if view is not None and view["state"] == STARTING:
            # Its waiter still owns this launch (#1505): a successor now would race the waiter's
            # failure handling, whose stop would close it. Settles within one readiness wait.
            raise NativeError(409, "this session is starting; wait for it")
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
    call's place in the text (`_at`) still lines up after it. Message starts (`_starts`, raw
    offsets, #1643) before the kept window are dropped and counted: the message the cut lands
    inside loses its start, so the window is known to begin mid-message."""
    cut = max(0, len(text) - SNAPSHOT_TEXT)
    turn[f"{key}_cut"] = turn.get(f"{key}_cut", 0) + cut
    turn[key] = text[cut:]
    starts = turn.get(f"{key}_starts")
    if cut and starts:
        kept = [s for s in starts if s >= turn[f"{key}_cut"]]
        turn[f"{key}_starts_cut"] = turn.get(f"{key}_starts_cut", 0) + len(starts) - len(kept)
        turn[f"{key}_starts"] = kept


def _append(turn: dict, key: str, block: str, *, continues: bool = False) -> None:
    """Add agent text to ``key``: a new message a paragraph apart, or (``continues``) more of the
    streamed message in progress. Records where each message starts (#1643), in raw offsets like
    a tool's `_at`, so the fold and the newest-whole-messages slice can tell messages apart even
    when one contains blank lines of its own."""
    before = turn[key]
    after = before + block if continues else _paragraph(before, block)
    starts = turn.setdefault(f"{key}_starts", [])
    if after != before and (not continues or not (starts or turn.get(f"{key}_starts_cut"))):
        start = 0 if continues else len(after) - len(block)
        starts.append(turn.get(f"{key}_cut", 0) + start)
    _keep(turn, key, after)


def _place_breaks(turn: dict, source: str) -> None:
    """The shown reply's message starts (#1643) in its own coordinates, and what the kept window
    already dropped: whole and partial messages (`reply_messages_cut`) and characters."""
    cut = turn.get(f"{source}_cut", 0)
    turn["reply_breaks"] = [s - cut for s in turn.get(f"{source}_starts", []) if s >= cut]
    turn["reply_messages_cut"] = turn.get(f"{source}_starts_cut", 0)
    turn["reply_chars_cut"] = cut


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


#: Why work ended with its worker (#1724), strongest first: a kernel OOM kill names the end
#: whatever else was in flight; then the operator's Stop; then the native connection ending.
END_REASONS = ("oom", "stop", "protocol", "worker_ended")


def _worker_ends(
    inventories: dict,
    settlements: dict,
    tasks_of: Callable[[str], list[dict]],
    live_worker: str | None,
    ended_reason: Callable[[str], str | None],
    timeline: list[tuple[int, str]],
    window: set[str],
    *,
    first: bool,
) -> list[dict]:
    """Each ended generation's last-observed active work, as rows (#1724). A generation that
    settled itself (Stop, idle, protocol end) lists what it settled. One that could not (OOM,
    SIGKILL, crash) is settled HERE, from its last journaled inventory, idempotently: the same
    journal always yields the same rows, and nothing is written. Work started and ended between
    two inventories was never recorded, and no row claims it. An idle exit is a row of its own;
    an end with nothing active is none.

    Each end is ANCHORED to the newest turn of the whole conversation before it (``after_turn``;
    None before the first turn) and travels with that turn (Hermes on PR #1757): a snapshot or a
    history page carries exactly the ends anchored to the turns of its own window, and the window
    that reaches the first turn (``first``) also those before it. Windows partition the turns, so
    every recorded end is reachable through the snapshot and its pages, once. No count cap: a
    generation is launched only by the session's creation or an operator message, so a window
    holds at most about one end per turn it shows (plus the creation's)."""
    ends = []
    for worker_id in {*inventories, *settlements} - {live_worker}:
        settled = settlements.get(worker_id)
        item = settled or inventories[worker_id]
        ends.append((item["cursor"], worker_id, item, settled is not None))
    rows: list[dict] = []
    positions = [position for position, _ in timeline]
    for cursor, worker_id, item, settled in sorted(ends):
        at = bisect.bisect_left(positions, cursor)
        anchor = timeline[at - 1][1] if at else None
        if not (anchor in window if anchor is not None else first):
            continue  # another window's: the snapshot or the history page holding its turn
        data = item["event"]["data"]
        # What each engine task was reported as, folded at append (#1805): a task still running
        # when its worker ended is one the fold kept.
        facts = {task["task_id"]: task for task in tasks_of(worker_id)}
        if settled and data["reason"] == "idle":
            row: dict = {"reason": "idle", "idle_for": data.get("idle_for")}
        elif data["engine"] or data["processes"] or data["more"] or data["scan"] == "unknown":
            # Precedence (#1724): oom > stop > protocol > worker_ended.
            oom = ended_reason(worker_id) is not None
            row = {
                "reason": "oom" if oom else data["reason"] if settled else "worker_ended",
                "scan": "unknown" if data["scan"] == "unknown" else "ok",
                "tasks": [
                    {
                        "task_id": task_id,
                        "source": "engine",
                        "state": "ended_with_worker",
                        **{
                            key: value
                            for key, value in facts.get(task_id, {}).items()
                            if key in ("description", "task_type", "started_at")
                        },
                    }
                    for task_id in data["engine"]
                ],
                "processes": [
                    {**p, "source": "process", "state": "ended_with_worker"}
                    for p in data["processes"]
                ],
                "more": data["more"],
                "engine_more": data.get("engine_more", 0),
            }
        else:
            continue
        row.update(
            worker=worker_id,
            # When it was settled; for an abrupt end, when its work was last observed.
            at=item["ts"],
            settled=settled,
            after_turn=anchor,
        )
        rows.append(row)
    return rows


def _stop_inventory(item: dict | None, live_worker: str, tasks: dict) -> dict:
    """The live worker's background work as its last inventory journaled it (#1724). No
    inventory yet means nothing ran that it had to record: the empty set's digest."""
    if item is None:
        data = {"engine": [], "processes": [], "more": 0, "engine_more": 0, "scan": "ok"}
        digest = native_ipc.stop_digest(live_worker, (), (), "ok")
        observed = None
    else:
        data, digest, observed = item["event"]["data"], item["event"]["data"]["digest"], item["ts"]
    return {
        "worker": live_worker,
        "digest": digest,
        "scan": "unknown" if data["scan"] == "unknown" else "ok",
        "tasks": [
            {
                "task_id": task_id,
                **{
                    key: tasks[task_id][key]
                    for key in ("description", "task_type", "started_at")
                    if key in tasks.get(task_id, {})
                },
            }
            for task_id in data["engine"]
        ],
        "processes": [dict(p) for p in data["processes"]],
        "more": data["more"],
        # Tasks past the listed 128 (#1724): with `more`, an inventory over the display limits
        # is never confirmed exactly, only by Stop everything.
        "engine_more": data.get("engine_more", 0),
        "observed_at": observed,
    }


def _show_open(turn: dict, streaming: bool) -> None:
    """The message an operator turn was streaming after its settled ones (#1727), handed to the
    snapshot SEPARATELY (``reply_open``, its message starts in ``reply_open_breaks``): the snapshot
    picks its window over the settled reply alone, exactly as before, and appends this after it,
    so text still streaming never moves that window or the tool rows in it. Kept for a turn that
    ended without settling it too (interrupted, failed, uncertain) or waits on an approval: the
    words the operator watched arrive never vanish. ``stream`` only while it ``streaming``.

    Not for a reply shown from the streamed text already (no settled block: the partial IS the
    reply), nor for a turn that completed normally (its text settled)."""
    text = turn["open"]
    if (
        turn["status"] == "done"
        or "stream" in turn
        or not text.strip()
        or not (turn.get("reply_starts") or turn.get("reply_starts_cut"))
    ):
        return
    cut = turn.get("open_cut", 0)
    turn["reply_open"] = text
    # In its own kept text's coordinates; a start the 200k window dropped is simply absent.
    turn["reply_open_breaks"] = [s - cut for s in turn.get("open_starts", []) if s >= cut]
    if streaming:
        turn["stream"] = {"item": turn["open_item"]}


def project(
    journal: native_journal.Journal,
    record: dict,
    live_worker: str | None,
    *,
    max_turns: int | None = None,
    before: str | None = None,
    ended_workers: frozenset[str] | set[str] = frozenset(),
    ended_reason: Callable[[str], str | None] = lambda worker_id: None,
    ended_code: Callable[[str], str] = lambda worker_id: "worker_ended",
    live_text: bool = False,
) -> dict:
    """Project only displayed turn payloads; transcript consumers may request every turn.

    ``ended_reason`` (#1643) names why a gone generation ended when systemd said so (its memory
    limit), replacing the generic "worker ended" text. It explains; it never changes a status.
    ``ended_code`` (#1733): the `not_sent` reason a message still queued by a generation PROVED
    ended derives — `journal_unwritable` when that generation stopped because the session's
    record could not be written.

    ``before`` (#1504) projects the ``max_turns`` turns just older than that turn instead of the
    newest window: a page of history, keyed by turn id so turns appended meanwhile never shift
    it. ``history_before`` is where the next older page starts, or None at the first turn.

    ``live_text`` (#1727, the operator's own snapshot only): the running operator turn whose reply
    already has settled messages also shows the message it is streaming now — the partial text
    after its last settled block — marked ``stream`` so the pane can extend it. Every other
    reader (history, the public link, missions) keeps the settled reply alone."""
    turns: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    decided: set[str] = set()
    native_id = record.get("native_id")
    model_effective = None
    # What the client REPORTS it runs (#1511): the newest `session` event's model and level. A
    # report, not evidence; `model_effective` stays the per-turn observation.
    model_configured = effort_configured = None
    background = False
    background_worker: str | None = None  # the worker that reported `background`
    background_at = -1  # the cursor of the record that set it, against the newest heartbeat
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
    # Folded from every `background` record at append (#1805); plain heartbeats are not indexed.
    tasks = journal.events.tasks_of(live_worker) if live_worker is not None else []
    ended: set[str] = set()  # agent turns whose end (or a later start, or a new worker) was seen
    last: tuple[str, str] | None = None  # (agent turn, the worker that minted it)
    # The background inventory (#1724), per generation: its newest inventory and its settlement,
    # and what each engine task was reported as, for the rows of work that ended with a worker.
    inventories: dict[str, dict] = {}
    settlements: dict[str, dict] = {}
    unresumed: dict[str, int] = {}
    for item in journal.events.for_operations(set()):
        kind, data = item["event"]["kind"], item["event"]["data"]
        if kind == "background_inventory":
            inventories[item["worker_id"]] = item
        elif kind == "background_settled":
            settlements.setdefault(item["worker_id"], item)
        elif kind in {"stop_aborted", "stop_resumed"} and "unresumed" in data:
            # Jobs an interrupted exact Stop paused and could not confirm resumed (#1724).
            unresumed[item["worker_id"]] = data["unresumed"]
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
    if before is None:
        shown = timeline if max_turns is None else timeline[-max_turns:] if max_turns else []
    else:
        at = next((i for i, entry in enumerate(timeline) if entry[2] == before), None)
        if at is None:
            raise NativeError(409, "history cursor not found")
        shown = timeline[max(0, at - (max_turns or 0)) : at]
    # The oldest turn of the window, when anything older exists. Only the window counts: a live
    # turn pulled forward below is shown, but it is not where history continues.
    history_before = shown[0][2] if shown and shown[0] != timeline[0] else None
    # The window's own turns (before any live turn is pulled forward): the worker ends anchored to
    # them travel with this view (#1724); a turn pulled forward carries its ends on its page.
    window_turns = {entry[2] for entry in shown}
    window_first = not timeline or (bool(shown) and shown[0] == timeline[0])
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
    if before is None:
        shown = sorted({*shown, *waiting})
    selected = {entry[2] for entry in shown if entry[1] == 0}
    selected_agents = {entry[2] for entry in shown if entry[1] == 1}
    agents: dict[str, dict] = {}
    open_agent: str | None = None
    oom_counts: dict[str, int] = {}  # invocation → the newest cumulative count shown (#1732)
    oom_between: list[tuple[int, int]] = []  # session-level OOM notices: (cursor, kills)
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
                # The message streaming now (#1727): partial text since the last settled block.
                "open": "",
                "open_item": None,
                "context": params.get("context") or {},
                "tools": [],
                "handoff": op.handoff,
                "queued_claim": op.queued,
                "reason_code": op.reason_code,
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
        if kind == "session":
            # A report of a DIFFERENT model retires the earlier observation (#1511): it was made
            # under the previous model and must not outrank what the client now says it runs.
            # The same model reported again (claude's `system/init` opens every turn) keeps it.
            if data.get("model_configured") != model_configured:
                model_effective = None
            model_configured = data.get("model_configured")
            effort_configured = data.get("effort_configured")
        if kind == "session" and data.get("native_id"):
            native_id = data["native_id"]
        elif kind == "model" and data.get("model_effective"):
            model_effective = data["model_effective"]
        elif kind == "background":
            if item["worker_id"] == live_worker:
                background, background_worker = bool(data.get("active")), item["worker_id"]
                background_at = item["cursor"]
            if "background_turn" in data:
                background_turn = (data["background_turn"], item["worker_id"])
                if open_agent is not None:
                    previous = agents.get(open_agent)
                    if previous is not None and previous["worker_id"] != item["worker_id"]:
                        # A new generation speaks: the old one died mid-turn, whatever this says.
                        _settle_agent(previous, "worker_ended", ended_reason(previous["worker_id"]))
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
        elif kind == "memory_oom_kill":
            # #1732: one notice per (invocation, cumulative count), so a re-read, a reload or a
            # replayed record never doubles it; two kills between reads are one notice of 2.
            killed = data["count"] - oom_counts.get(data["invocation_id"], 0)
            if killed > 0:
                oom_counts[data["invocation_id"]] = data["count"]
                if "operation_id" in data or "agent_turn" in data:
                    if turn is not None:  # a turn outside this window keeps its own notice
                        turn["oom_kills"] = turn.get("oom_kills", 0) + killed
                else:
                    oom_between.append((item["cursor"], killed))
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
                background_at = item["cursor"]
            continue
        elif kind == "text":
            if data["text"] and turn["reply_ts"] is None:
                turn["reply_ts"] = item.get("ts")
            if data["partial"]:
                # Streamed pieces of ONE message concatenate; a new message id starts a paragraph.
                item = data.get("item_id")
                if item and turn["partial_item"] and item != turn["partial_item"]:
                    _append(turn, "partial", data["text"])
                else:
                    _append(turn, "partial", data["text"], continues=True)
                turn["partial_item"] = item or turn["partial_item"]
                if "open" in turn:
                    # The same rules for the message in progress alone, from its own start.
                    if item and turn["open_item"] and item != turn["open_item"]:
                        _append(turn, "open", data["text"])
                    else:
                        _append(turn, "open", data["text"], continues=True)
                    turn["open_item"] = item or turn["open_item"]
            else:
                # Each settled text block is its own message: without a break, "…done.Next…".
                _append(turn, "reply", data["text"])
                if "open" in turn:
                    # It settles what was streaming: nothing is in progress until the next delta.
                    for key in ("open_starts", "open_cut", "open_starts_cut"):
                        turn.pop(key, None)
                    turn["open"], turn["open_item"] = "", None
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
            # Indexed (not a scan of every earlier call): a long turn has tens of thousands.
            index = turn.setdefault("_tool_index", {})
            found = index.get(entry["id"])
            existing = None if found is None else found[1]
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
                index[entry["id"]] = (len(turn["tools"]) - 1, entry)
                if len(turn["tools"]) > TOOLS_SHOWN:
                    # Fell out of the calls a snapshot shows: its risk is never asked for.
                    turn["tools"][-TOOLS_SHOWN - 1].pop("_risk_sources", None)
            else:
                # A result names only the call it answers (Claude's carry no tool): it keeps the
                # call's name rather than the placeholder (#1438).
                if not data.get("tool"):
                    entry.pop("name")
                existing.update({k: v for k, v in entry.items() if v})
            # The same advisory classifier as a pending card (#1339): a tool row that ran a
            # command — asked or, in a skip-permissions session, never asked — says so. Only a
            # call a snapshot can show (the newest TOOLS_SHOWN) is classified, once, after the
            # walk (`_classify_tools`): classifying every call of a 19,000-call turn on every
            # read took ~20 s per snapshot.
            row = existing if existing is not None else entry
            place = len(turn["tools"]) - 1 if existing is None else found[0]
            if data.get("summary") and len(turn["tools"]) - place <= TOOLS_SHOWN:
                # With the name the call had THEN: a later event may report the same call under
                # another kind, which must not reclassify an earlier command (Hermes on #1711).
                row.setdefault("_risk_sources", []).append((row["name"], data["summary"]))
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
                background_at = item["cursor"]
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
                # #1732: the worker outlives its agent now; once it is gone, what it recorded of
                # the agent's end (or systemd's of the unit's) explains the disconnect.
                gone = item["worker_id"] != live_worker
                turn["reason"] = (
                    data.get("message")
                    or (ended_reason(item["worker_id"]) if gone else None)
                    or "the native connection closed"
                )
    # The live worker's newest plain heartbeat (#1805), unindexed, is newer than whatever set the
    # flag above: it is the report the flag would have ended on.
    beat = journal.events.heartbeat_of(live_worker) if live_worker is not None else None
    if beat is not None and beat[0] > background_at:
        background, background_worker = beat[1], live_worker
    _classify_tools([*turns.values(), *agents.values()], cwd)
    in_flight = None
    requests = []
    for turn in [*turns.values(), *agents.values()]:
        # A reply that came whole at the end has no settled text before any call: all sit at 0.
        if turn["reply"] or turn.get("kind") == "agent":
            _place_tools(turn, "reply")
            _place_breaks(turn, "reply")
        else:
            turn["reply"] = turn["partial"]
            _place_tools(turn, "partial")
            _place_breaks(turn, "partial")
            if not turn["completed"]:
                # Shown from the streamed text of a turn still open (#1727): the pane may extend
                # it with that turn's next partial text events, from the message it names here.
                turn["stream"] = {"item": turn["partial_item"]}
    for turn in turns.values():
        if turn["handoff"] == "not_sent":
            # An operator message proved never sent (#1538), queued OR direct (a Send again is
            # usually direct): its reason, as a closed code, and its own derived retry id, so a
            # chain of failed attempts stays recoverable at its newest link. Never a Send now
            # control (not a turn) or an uncertain handoff. A direct refusal written before
            # #1538 has no code and reads `unexpected`.
            _not_sent(turn, turn["reason_code"] or "unexpected", journal)
        elif turn["handoff"] == "queued":
            if turn["worker_id"] != live_worker and turn["worker_id"] in ended_workers:
                # Its worker ended, on AUTHORITATIVE evidence (`_exit_proved`), with the message
                # still queued: the same proved non-send. The durable record is written by
                # `_settle_stranded`; this derivation is the fallback when it could not be.
                _not_sent(turn, ended_code(turn["worker_id"]), journal)
            else:
                # Live, or not seen live by an observation that proves nothing (a `/proc` read
                # can fail): still pending. Neither `not_sent` nor Send again (#1538).
                turn["status"] = "queued"
        elif not turn["completed"] and turn["status"] == "pending":
            if turn["worker_id"] != live_worker:
                turn["status"], turn["code"] = "failed", "uncertain"
                turn["reason"] = (
                    ended_reason(turn["worker_id"])
                    or "the native worker ended before this turn settled"
                )
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
    if live_text:
        for turn in turns.values():
            _show_open(turn, turn["turn_id"] == in_flight and turn["status"] == "pending")
    for agent in agents.values():
        if agent["worker_id"] != live_worker:
            # Its worker is gone: nothing will end this turn now.
            _settle_agent(agent, "worker_ended", ended_reason(agent["worker_id"]))
    for turn_id, turn in turns.items():
        turn["position"] = journal.operations[turn_id].claimed_revision
    shown_turns = sorted([*turns.values(), *agents.values()], key=lambda t: t["position"])
    worker_ends = _worker_ends(
        inventories,
        settlements,
        journal.events.tasks_of,
        live_worker,
        ended_reason,
        [(entry[0], entry[2]) for entry in timeline],
        window_turns,
        first=window_first,
    )
    live_tasks = [{**task, "source": "engine"} for task in tasks]
    stop_inventory = None
    if live_worker is not None:
        stop_inventory = _stop_inventory(
            inventories.get(live_worker), live_worker, {task["task_id"]: task for task in tasks}
        )
        live_tasks += [
            {
                "task_id": f"pid:{p['pid']}:{p['start']}",
                "source": "process",
                "state": "running",
                "pid": p["pid"],
                "comm": p["comm"],
                "started_at": p["started_at"],
                "updated_at": stop_inventory["observed_at"],
            }
            for p in stop_inventory["processes"]
        ]
    oom_start = _place_oom_between(oom_between, journal, timeline, turns, agents)
    for turn in shown_turns:
        for key in (
            "partial",
            "partial_item",
            "handoff",
            "queued_claim",
            "reason_code",
            "worker_id",
            "completed",
            "delivery_operation_id",
            "delivery_handoff",
            "position",
            "reply_cut",
            "partial_cut",
            "reply_starts",
            "partial_starts",
            "reply_starts_cut",
            "partial_starts_cut",
            "_tool_index",
            "open",
            "open_item",
            "open_cut",
            "open_starts",
            "open_starts_cut",
        ):
            turn.pop(key, None)
    return {
        "turns": shown_turns,
        # Session-level OOM notices before the first turn (#1732), shown only with that turn.
        **({"oom_kills_start": oom_start} if oom_start else {}),
        "omitted_turns": len(timeline) - len(shown_turns),
        "history_before": history_before,
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
        **pending_start_view(record, live_worker),
        # The generation-scoped launch view (#1505): starting / ready / failed + reason.
        "launch": launch_view(record),
        "model_effective": model_effective,
        "model_configured": model_configured,
        "effort_configured": effort_configured,
        "pending_requests": requests,
        "native": {
            "native_id": native_id,
            "worker": live_worker,
            # Only the live worker's own report: an exited or replaced generation's task ended
            # with it, so its last `active` would show an idle replacement busy forever.
            "background_active": background and background_worker == live_worker,
            # Engine-reported tasks and the process floor's agent processes (#1724): two kinds
            # of observation, never merged into one job count.
            "background_tasks": live_tasks,
            # What an operator Stop ends (#1724): the live worker's last inventory, with the
            # digest its Stop is confirmed against. The worker rechecks it before stopping.
            "stop_inventory": stop_inventory,
            # Work still running when an earlier generation ended (#1724), and idle exits.
            "worker_ends": worker_ends,
            # Jobs an interrupted exact Stop paused and could not resume (#1724): the pane says
            # so and offers Stop everything. Only the live worker's; 0 when none.
            "paused_unresumed": unresumed.get(live_worker, 0) if live_worker else 0,
            # Only the live worker's own turn is a Send now target (#1427): a target minted by an
            # exited or replaced generation names a turn nothing can stop any more.
            "background_turn": background_turn[0]
            if background_turn is not None and background_turn[1] == live_worker
            else None,
        },
    }


#: What a `not_sent` reason code means (#1538), fixed text only: never CLI output, a path or an
#: exception message. The pane holds the same table for its "Not sent: <reason>" line.
NOT_SENT_REASONS = {
    "agent_removed": "this session's agent was removed while the message waited",
    "agent_changed": (
        "this session's agent was switched to a different install while the message waited"
    ),
    "source_store_changed": "this session's history store changed owner while the message waited",
    "generation_closed": "the session was stopped before this message could go out",
    "question_replaced": "the question it answered was answered or replaced first",
    "worker_ended": "the session ended while this message waited",
    "journal_unwritable": (
        "BattleLab couldn't write this session's record while the message waited"
    ),
    "unexpected": "BattleLab could not hand this message to the agent",
}


def _not_sent(turn: dict, code: str, journal: native_journal.Journal) -> None:
    """``retry`` is the ONE operation id Send again submits for this message, derived from its own
    (`native_journal.retry_id`). ``retry_recorded``: the journal holds that operation, whatever
    became of it, so this message is history on every tab and device. ``retried``: it holds it and
    it was NOT settled `not_sent` (delivered, pending or uncertain). A retry that failed before
    stdin carries its own `not_sent` with its own derived id: the chain is recovered at its
    newest link, and no id of a failed claim is ever submitted again."""
    code = code if code in NOT_SENT_REASONS else "unexpected"
    retry = native_journal.retry_id(turn["turn_id"])
    attempt = journal.operations.get(retry)
    turn["status"] = "failed"
    turn["reason"] = f"not sent: {NOT_SENT_REASONS[code]} — nothing was delivered"
    turn["not_sent"] = {
        "code": code,
        "retry": retry,
        "retry_recorded": attempt is not None,
        "retried": attempt is not None and attempt.handoff != "not_sent",
    }


def _place_oom_between(
    notices: list[tuple[int, int]],
    journal: native_journal.Journal,
    timeline: list[tuple[int, int, str]],
    turns: dict[str, dict],
    agents: dict[str, dict],
) -> int:
    """Put each session-level OOM notice (#1732) after the turn that STARTED last before it: an
    operator turn when it was handed to the agent (a queued one starts at its dispatch, not its
    claim), an agent's own turn at its first event. It is shown between that turn and the next
    as `oom_kills_after`, never as the turn's own. A notice before any turn is returned (shown
    only while the first turn is); one after a turn outside the window is that page's to show."""
    starts = sorted(
        (
            (journal.operations[ident].dispatched_revision or 10**18, ident)
            if kind == 0
            else (position, ident)
            for position, kind, ident in timeline
        )
    )
    before = 0
    for cursor, killed in notices:
        index = bisect.bisect_left(starts, (cursor, "")) - 1
        if index < 0:
            before += killed if not timeline or timeline[0][2] in {*turns, *agents} else 0
            continue
        turn = turns.get(starts[index][1]) or agents.get(starts[index][1])
        if turn is not None:
            turn["oom_kills_after"] = turn.get("oom_kills_after", 0) + killed
    return before


def _settle_agent(agent: dict | None, state: str, reason: str | None = None) -> None:
    """End a turn the agent ran for itself, as its end event says (#1438)."""
    if agent is None or agent["completed"]:
        return
    agent["completed"] = True
    if state == "interrupted":
        agent["status"], agent["code"] = "failed", "interrupted"
    elif state == "worker_ended":
        # Its worker died mid-turn: what the turn changed is unknown, exactly as for an operator
        # turn (#1643), whatever the reason.
        agent["status"], agent["code"] = "failed", "uncertain"
        agent["reason"] = reason or "the native worker ended during this turn"
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


def _classify_tools(turns: list[dict], cwd: str | None) -> None:
    """The advisory risk of each call a snapshot can show (its turn's newest TOOLS_SHOWN): the
    newest of its events that classifies, under the name the call had at that event, as when
    every event was classified on arrival."""
    for turn in turns:
        for row in turn["tools"][-TOOLS_SHOWN:]:
            for name, summary in reversed(row.pop("_risk_sources", None) or ()):
                risk = _tool_risk(name, summary, cwd)
                if risk is not None:
                    row["risk"] = risk
                    break


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


def _exit_proved(session_id: str, worker_id: str) -> bool:
    """Authoritative evidence that this generation can never hand a queued message off (#1538).

    Its generation was closed or replaced (its own admission refuses then), its lifecycle says it
    exited or was refused, or `_proved_gone` holds (the unit and cgroup evidence the lease sweep
    trusts). `_live_worker` being None is NOT such evidence: it is a cheap `/proc` observation
    that reads "not running" on any read error."""
    record = native_state.read_session(session_id) or {}
    if record.get("current_worker") != worker_id or record.get("closed") is True:
        return True
    if native_state.read_lifecycle(worker_id).get("phase") in {"exited", "refused"}:
        return True
    return _proved_gone(worker_id)


#: The I/O error classes of an unwritable record (#1733, `native_segments.CAUSES`), as the
#: operator reads them: fixed text, never an exception message.
JOURNAL_CAUSES = {"disk_full": "disk full", "io_error": "I/O error", "write_failed": "write failed"}


def _journal_failed(worker_id: str) -> bool:
    try:
        return native_state.read_lifecycle(worker_id).get("journal_failure") in JOURNAL_CAUSES
    except (native_state.StateError, OSError):
        return False


def _ended_code(worker_id: str) -> str:
    return "journal_unwritable" if _journal_failed(worker_id) else "worker_ended"


#: The agent died by SIGKILL while the OOM counter of its unit rose (#1732). Facts, not a cause:
#: no record ties a kill to a victim, so it never says the agent ran out of memory.
AGENT_KILLED_DURING_OOM = (
    "the agent was killed (SIGKILL) while a process in this session ran out of memory"
)


def _ended_reason(worker_id: str) -> str | None:
    """Why a gone generation ended, when systemd recorded it for the pinned invocation (#1643):
    only `oom-kill` is named. WHICH limit killed it is never claimed (no counter proves it, Hermes
    on #1651); the reason states facts instead: the memory reading the worker took just before
    (fresh: within `native_memory.FRESH` of the end) against the limit it was measured against,
    else this session's own configured limit. Otherwise the worker's own record of how its agent
    ended (#1732, `agent_exit`), for the same pinned invocation: only `sigkill_during_oom`, in
    factual words. Anything else, missing or mismatched reads as None (generic)."""
    try:
        life = native_state.read_lifecycle(worker_id)
    except (native_state.StateError, OSError):
        return None
    failure = life.get("journal_failure")
    if failure in JOURNAL_CAUSES:
        # #1733: it stopped because its record could not be written. Still UNCERTAIN: the agent
        # may have done more than the record shows, and nothing is resent because of it.
        return f"BattleLab couldn't write this session's record ({JOURNAL_CAUSES[failure]})"
    ended = life.get("exit")
    if (
        not isinstance(ended, dict)
        or ended.get("result") != "oom-kill"
        or not life.get("invocation_id")
        or ended.get("invocation_id") != life.get("invocation_id")
    ):
        agent = life.get("agent_exit")
        if (
            isinstance(agent, dict)
            and agent.get("reason") == "sigkill_during_oom"
            and life.get("invocation_id")
            and agent.get("invocation_id") == life.get("invocation_id")
        ):
            return AGENT_KILLED_DURING_OOM
        return None
    memory, at = life.get("memory"), ended.get("at")
    if (
        isinstance(memory, dict)
        and memory.get("invocation_id") == life["invocation_id"]
        and isinstance(at, int | float)
        and isinstance(memory.get("at"), int | float)
        and 0 <= at - memory["at"] <= native_memory.FRESH
        and isinstance(memory.get("working_set"), int)
        and isinstance(memory.get("max"), int)
        and memory["max"] > 0
    ):
        used, cap = _gb(memory["working_set"]), _gb(memory["max"])
        if memory.get("scope") == "shared":
            return (
                f"killed: out of memory — last measured at {used} of {cap} GB in a group this "
                "session shares, limited outside BattleLab"
            )
        return (
            f"killed: out of memory — last measured at {used} of its {cap} GB limit "
            "(Settings → Resource limits)"
        )
    gib = life.get("memory_gib")
    if isinstance(gib, int) and gib > 0:
        return (
            f"killed: out of memory (this session's limit is {gib} GB — Settings → Resource limits)"
        )
    return "killed: out of memory"


def _gb(size: int) -> str:
    """Bytes as GB (binary, like the limit setting): one decimal below 10, never a trailing .0."""
    value = size / 1024**3
    shown = round(value, 1) if value < 10 else round(value)
    return f"{shown:g}"


def _stranded_workers(journal: native_journal.Journal, live: str | None) -> set[str]:
    return {
        op.worker_id
        for op in journal.operations.values()
        if op.request["action"] == "submit" and op.handoff == "queued" and op.worker_id != live
    }


def _settle_stranded(
    prov, session_id: str, journal: native_journal.Journal, live: str | None
) -> tuple[native_journal.Journal, set[str]]:
    """Make "the worker ended with this message still queued" durable (#1538).

    Returns the journal to project and the generations PROVED ended (`_exit_proved`), which is
    the only ground on which `project` derives `worker_ended`. A generation merely not observed
    live is inconclusive: nothing is written or derived, and its messages stay pending. For a
    proved one, the first read writes the `native_handoff` `not_sent` record with `reason_code:
    worker_ended`, under the session's lifecycle lock (re-reading the live worker and the evidence
    under it), through `native_journal.settle_worker_ended`, which writes only while the message
    is still `queued`. Not while a live worker older than #1538 runs: its journal reader refuses
    the new field. Anything that prevents the write (the lock busy, a read-only journal) leaves
    the derivation to show the same reason."""
    candidates = _stranded_workers(journal, live)
    try:
        ended = {worker for worker in candidates if _exit_proved(session_id, worker)}
    except (native_state.StateError, OSError):
        return journal, set()
    if not ended:
        return journal, ended
    wrote = False
    try:
        with native_state.session_lock(session_id, wait=0):
            live = _live_worker(session_id)
            if (
                live is not None
                and native_state.read_lifecycle(live).get("handoff_reasons") is not True
            ):
                return journal, ended
            root, key = _journal_root(prov), journal.header["request"]["session_key"]
            proved = {w for w in _stranded_workers(journal, live) if _exit_proved(session_id, w)}
            # `journal_unwritable` (#1733) only where no live reader predates it.
            specific = (
                live is None or native_state.read_lifecycle(live).get("journal_segments") is True
            )
            rollable = _segments_safe(session_id)
            for op in list(journal.operations.values()):
                if (
                    op.request["action"] == "submit"
                    and op.handoff == "queued"
                    and op.worker_id in proved
                ):
                    wrote |= native_journal.settle_worker_ended(
                        root,
                        key,
                        op.operation_id,
                        op.worker_id,
                        _ended_code(op.worker_id) if specific else "worker_ended",
                        # Never roll over under a generation that reads segment 0 alone.
                        may_roll=rollable,
                    )
    except (native_state.StateError, native_journal.JournalError, NativeError, OSError):
        return journal, ended
    return (_journal(prov, session_id) if wrote else journal), ended


def _snapshot_sync(engine_id: str, session_id: str, live_text: bool = False) -> dict:
    # A read: it reaches a retiring client's history too (#1311). New work never does.
    prov, record = _session(engine_id, session_id, retiring_ok=True)
    from .structured_runtime import SNAPSHOT_TURNS

    live = _live_worker(session_id)
    journal, ended = _settle_stranded(prov, session_id, _journal(prov, session_id), live)
    view = project(
        journal,
        record,
        live,
        max_turns=SNAPSHOT_TURNS,
        ended_workers=ended,
        ended_reason=cache(_ended_reason),
        ended_code=cache(_ended_code),
        live_text=live_text,
    )
    # The running worker advertises its own implementation. A web update cannot teach an
    # already-running, release-pinned worker a new effect (#1389).
    life = native_state.read_lifecycle(live) if live else {}
    mode = life.get("send_now")
    if mode in {"steer", "interrupt"}:
        view["native"]["send_now"] = mode
    # Send now against a turn the agent runs for itself (#1427), only from a worker that says it
    # can; an older worker's target is never offered.
    # What Send now would do right now (#1644), as the live worker last reported it: a display
    # hint for the composer, never authority. Unknown without a live worker or a report.
    state = life.get("send_now_state")
    view["native"]["send_now_state"] = (
        state if live and state in {"ready", "would_stop_background"} else "unknown"
    )
    if life.get("send_now_background") == "interrupt":
        view["native"]["send_now_background"] = "interrupt"
        # Its Interrupt button (#1438), only from a worker that accepts the direct request.
        view["native"]["interrupt_own"] = life.get("interrupt_own") is True
    else:
        view["native"]["background_turn"] = None
    view["native"]["files"] = not live or life.get("files") is True
    # Memory pressure of the live generation's unit (#1643): only a fresh warning of the pinned
    # invocation; a gone, replaced or unreadable one shows nothing.
    view["native"]["memory_pressure"] = native_memory.public(life) if live else None
    # Remove a queued message (#1447): only a live worker that says it can.
    if life.get("withdraw") is True:
        view["native"]["withdraw"] = True
    return view


def model_facts(engine_id: str, session_id: str) -> dict:
    """The model and reasoning level of one session (#1511), for the one-session lookup: what was
    requested, what the client reports it runs (the newest `session` event) and the newest per-turn
    observation. Reads only the journal's session-wide events — `session` and `model` are indexed
    there — and projects no turns. Blocking; raises like a snapshot read."""
    prov, record = _session(engine_id, session_id, retiring_ok=True)
    facts = {"model_configured": None, "effort_configured": None, "model_effective": None}
    try:
        events = list(_journal(prov, session_id).events.for_operations(set()))
    except (NativeError, native_journal.JournalError, OSError):
        events = []  # not written yet (a starting session) or unreadable: the request still holds
    for item in events:
        kind, data = item["event"]["kind"], item["event"]["data"]
        if kind == "session":
            # The same rule as `project`: only a different reported model retires the observation.
            if data.get("model_configured") != facts["model_configured"]:
                facts["model_effective"] = None
            facts["model_configured"] = data.get("model_configured")
            facts["effort_configured"] = data.get("effort_configured")
        elif kind == "model" and data.get("model_effective"):
            facts["model_effective"] = data["model_effective"]
    return {
        "model_requested": record["request"]["model"],
        "effort_requested": record["request"].get("effort"),
        **facts,
    }


# --- adapter operations ------------------------------------------------------------------------


async def snapshot(engine_id: str, session_id: str, *, live_text: bool = False) -> dict:
    return await asyncio.to_thread(_snapshot_sync, engine_id, session_id, live_text)


def _history_sync(
    engine_id: str, session_id: str, before: str, limit: int, live_text: bool = False
) -> dict:
    # The snapshot's read (#1311): a retiring client's history is still readable.
    prov, record = _session(engine_id, session_id, retiring_ok=True)
    live = _live_worker(session_id)
    journal, ended = _settle_stranded(prov, session_id, _journal(prov, session_id), live)
    view = project(
        journal,
        record,
        live,
        max_turns=limit,
        before=before,
        ended_workers=ended,
        ended_reason=cache(_ended_reason),
        ended_code=cache(_ended_code),
        live_text=live_text,
    )
    return {
        "turns": view["turns"],
        "history_before": view["history_before"],
        # The ends anchored to this page's turns (#1724, Hermes on PR #1757): history reaches them.
        "worker_ends": view["native"]["worker_ends"],
    }


async def history(
    engine_id: str, session_id: str, before: str, limit: int, *, live_text: bool = False
) -> dict:
    return await asyncio.to_thread(_history_sync, engine_id, session_id, before, limit, live_text)


async def events(engine_id: str, session_id: str, after: int, limit: int) -> dict:
    prov, _ = await asyncio.to_thread(partial(_session, engine_id, session_id, retiring_ok=True))
    journal = await asyncio.to_thread(_journal, prov, session_id)
    try:
        return await asyncio.to_thread(journal.page, after, limit)
    except native_journal.JournalError as exc:
        raise NativeError(_ERRORS.get(exc.code, 409), exc.detail) from None


def _journal_stamp_sync(engine_id: str, session_id: str) -> Callable[[], tuple | None]:
    prov, _ = _session(engine_id, session_id, retiring_ok=True)
    root = _journal_root(prov)

    def stamp() -> tuple | None:
        # Only a wake hint (#1727): the page itself is always read through `events`, under the
        # journal's shared lock and its privacy checks. Appends come from the worker's own
        # process, so a file stat — no IPC — is what crosses that boundary (and an app restart).
        # The ACTIVE segment's (#1733): after a rollover the original file never changes again.
        try:
            return native_segments.active_stamp(root, session_id)
        except (OSError, ValueError):
            return None

    return stamp


async def journal_stamp(engine_id: str, session_id: str) -> Callable[[], tuple | None]:
    """A cheap probe of this session's journal identity and size, for a waiting events read."""
    return await asyncio.to_thread(_journal_stamp_sync, engine_id, session_id)


def _segments_safe(session_id: str) -> bool:
    """May THIS process roll the session's journal over (#1733)? Only when no generation that
    could still append reads segment 0 alone: every recorded generation either reads segments
    (`journal_segments`) or is PROVED gone. A cheap liveness observation proves nothing (Hermes
    on #1771): a worker of an older release missed by it may hold a writable descriptor of
    segment 0 and append behind the successor's pin. Unreadable or MISSING state is not proof of
    absence (a record can be lost while its worker still runs): not safe. The caller holds the
    session lifecycle lock, so no generation can be registered between this proof and its use.
    """
    try:
        record = native_state.read_session(session_id)
        workers_field = record.get("workers") if isinstance(record, dict) else None
        if not isinstance(record, dict) or not isinstance(workers_field or [], list):
            return False
        workers = set(workers_field or [])
        if record.get("current_worker"):
            workers.add(record["current_worker"])
        for worker_id in workers:
            life = native_state.read_lifecycle(worker_id)
            if life.get("journal_segments") is True or life.get("phase") == "gone":
                continue
            if not _proved_gone(worker_id):
                return False
    except (native_state.StateError, NativeError, OSError):
        return False
    return True


def _prepare_journal(prov, session_id: str) -> None:
    """Pre-claim admission under the journal's stable lock (#1733): roll the record over now if
    the claim could reach its soft bound, so concurrent sends never strand a claim behind a
    rollover that fails. 409 ``journal_unwritable`` when it cannot be written.

    Never while a generation that reads segment 0 alone may still run (`_segments_safe`, under
    the session lifecycle lock): such a worker keeps the old single-file bound until it is proved
    gone; its successor runs this release and rolls over itself."""
    try:
        # The proof and the rollover under the lifecycle lock that every launch holds while it
        # registers a generation (Hermes on #1771): none can start between the two.
        with native_state.session_lock(session_id):
            if _segments_safe(session_id):
                native_journal.prepare(_journal_root(prov), session_id)
    except (native_state.LockBusy, native_state.StateError):
        return  # unproved: leave it; the worker that claims rolls over itself
    except native_journal.JournalError as exc:
        if exc.code == "unwritable":
            cause = JOURNAL_CAUSES.get(exc.cause or "", "write failed")
            raise NativeError(
                409,
                f"BattleLab couldn't write this session's record ({cause}); nothing was sent",
                reason=native_ipc.JOURNAL_UNWRITABLE,
            ) from None
        raise NativeError(404 if exc.code == "unavailable" else 503, exc.detail) from None


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
        if receipt is not None and journal.operations[turn_id].queued:
            # A queued message's replay observes how it ended, `not_sent` with its reason
            # included (#1538): the turn shows it, and Send again is a new operation id.
            return receipt
        if receipt is not None:
            break
        revision = journal.revision if expected_revision is None else expected_revision
        # Before anything is launched or claimed (#1733): make room for this claim. A rollover
        # that cannot be written is a KNOWN refusal — nothing was claimed or sent.
        await asyncio.to_thread(_prepare_journal, prov, session_id)
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
            if _nothing_to_review(
                after, expected_revision, prior, gen.worker_id if gen is not None else None
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


# `memory_pressure` (#1643): the successor's memory reading, journaled on its first sample — often
# before the operator's first claim. A reading of its own unit, never conversation activity.
_STARTUP_EVENTS = frozenset(
    {
        "initialized",
        "session",
        "background",
        "response",
        "memory_pressure",
        # The readiness inventory (#1724), journaled before the first turn when it is not empty.
        "background_inventory",
    }
)


def _nothing_to_review(
    journal: native_journal.Journal, after: int, prior: str | None, successor: str | None
) -> bool:
    """Every record since ``after`` is one the operator could not have been answering: a status
    report (`_status_record`), from any generation, or a startup observation by ``successor``,
    the current generation, which was not live when the send began (``prior``).

    Such a generation has run no turn: what it journals before the claim (initialization,
    session facts, background state) is no activity the operator missed. Nor is a worker
    diagnostic bound to no operation or action — a history frame dropped as oversized while
    the native session loads: the snapshot shows it on no turn. A claim, a handoff,
    conversation content, a turn the agent starts or ends for itself, or any other record by
    another generation keeps the refusal."""
    start = bisect.bisect_right(journal.events.cursors, after)
    newer = journal.events[start:]
    fresh = successor is not None and successor != prior
    return len(newer) == journal.revision - after and all(
        _status_record(event["event"])
        or (fresh and event["worker_id"] == successor and _startup_record(event["event"]))
        for event in newer
    )


def _status_record(event: dict) -> bool:
    """A background task's progress or an activity heartbeat: an idle agent's running subagent
    writes one every few seconds, and an explicit expectation refused for it could never be met
    while the task runs. One carrying ``background_turn`` starts or ends a turn the agent runs
    for itself (#1427): that is activity, and keeps the refusal. The background inventory and an
    idle tick's resume report (#1724) are the same kind of report: what runs, not a turn."""
    if event["kind"] in {"background_inventory", "stop_resumed"}:
        return True
    return event["kind"] == "background" and "background_turn" not in (event.get("data") or {})


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


STOP_ALL_CHANGED = (
    "The agent's background tasks or turn changed since you confirmed; "
    "review them and confirm again"
)


async def send_now(
    engine_id: str,
    session_id: str,
    *,
    operation_id: str,
    turn_id: str,
    queued_turn_id: str,
    stop_all: dict | None = None,
) -> dict:
    """One deliberate selection of an existing message; no relaunch or duplicate submit.

    ``stop_all`` (#1644): the operator confirmed ending these background tasks of this worker
    (``{"worker": id, "task_ids": [...]}``) if only a Stop can cut the turn. The worker checks
    it against what runs, before and after the claim; any change refuses ``inventory_changed``."""
    prov, _ = await asyncio.to_thread(_session, engine_id, session_id)
    journal = await asyncio.to_thread(_journal, prov, session_id)
    previous = journal.operations.get(operation_id)
    confirm = {}
    if stop_all is not None:
        confirm = {
            "confirm_worker_id": stop_all["worker"],
            "confirm_tasks": native_ipc.tasks_digest(stop_all["task_ids"]),
        }
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
        if stop_all is not None:
            # Only a worker whose Send now can be a Stop has background tasks to confirm.
            if mode != "interrupt":
                raise NativeError(409, "Stop all and send is not available for this agent")
            if stop_all["worker"] != live:
                raise NativeError(409, STOP_ALL_CHANGED, reason="inventory_changed")
            mode = "stop_all"
    params = {"turn_id": turn_id, "queued_turn_id": queued_turn_id, "mode": mode}
    if mode == "stop_all":
        params.update(confirm)
    immutable = native_ipc.normalize_immutable_request({"action": "send_now", "params": params})

    async def send(_journal_now, revision):
        return await _existing_generation_call(
            session_id,
            "send_now",
            {**params, "operation_id": operation_id, "expected_revision": revision},
        )

    receipt = await _revisioned(prov, session_id, operation_id, immutable, None, send)
    if receipt["handoff"] == "not_sent":
        if mode == "stop_all":
            # Rechecked after the claim (#1644): what was confirmed no longer runs.
            raise NativeError(409, STOP_ALL_CHANGED, reason="inventory_changed")
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
        if discard:
            # A Stop is the session's end, proved under its lock. Enabling Skip permissions stops
            # the worker only to restart it (`discard=False`): the session keeps its slot.
            _release_specialist_slot(record)
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
    then fails its CAS instead of marking the discarded creation started.

    The same write drops the launch view (#1505), so a waiter still waiting on that generation
    settles nothing and runs no cleanup."""
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is None:
            return
        changed = False
        if _start_state(record) in {PENDING, STARTING, FAILED, EXPIRED}:
            _set_start(record, DISCARDED)
            changed = True
        if "launch" in record:
            del record["launch"]
            changed = True
        if changed:
            native_state.write_session(session_id, record)


STOP_CHANGED = "The session's background work changed since you looked; confirm again"
STOP_UNCHECKED = (
    "Stop not applied: the worker did not answer the background-work check, so nothing was "
    "stopped by BattleLab. If the worker did receive it, it ends only what you confirmed."
)
STOP_CHECK_TIMEOUT = 15.0
STOP_UNREACHABLE = (
    "BattleLab can't reach this session's worker to check its background work; nothing was "
    "stopped. Stop everything ends everything the agent started."
)


def _stop_inventory_sync(engine_id: str, session_id: str) -> dict | None:
    return _snapshot_sync(engine_id, session_id)["native"].get("stop_inventory")


def _unreachable_inventory_sync(engine_id: str, session_id: str, gen: Generation) -> dict:
    """The escape hatch of a Stop whose check failed (#1724): the generation it binds and what
    that worker last journaled, marked unreachable. Its Stop everything names that generation."""
    with contextlib.suppress(NativeError, native_journal.JournalError):
        shown = _stop_inventory_sync(engine_id, session_id)
        if shown is not None and shown.get("worker") == gen.worker_id:
            return {**shown, "reach": "unreachable"}
    return {
        "worker": gen.worker_id,
        "digest": None,
        "scan": "unknown",
        "tasks": [],
        "processes": [],
        "more": 0,
        "engine_more": 0,
        "observed_at": None,
        "reach": "unreachable",
    }


def _unreachable_error(engine_id: str, session_id: str, gen: Generation) -> NativeError:
    return NativeError(
        503,
        STOP_UNREACHABLE,
        reason="worker_unreachable",
        inventory=_unreachable_inventory_sync(engine_id, session_id, gen),
    )


def _settle_unreachable(prov, session_id: str, gen: Generation) -> None:
    """A Stop everything ended a worker that could not settle itself (#1724): record its last
    journaled work as ended by the operator's Stop, once, for that generation only, and only
    after its containment was proved gone. Best effort: without it the reader still settles
    the generation from its last inventory (`worker_ended`)."""
    with contextlib.suppress(native_journal.JournalError, NativeError, OSError):
        journal = _journal(prov, session_id)
        last = None
        for item in journal.events.for_operations(set()):
            if item["worker_id"] != gen.worker_id:
                continue
            if item["event"]["kind"] == "background_settled":
                return  # it settled itself (SIGTERM) before it went
            if item["event"]["kind"] == "background_inventory":
                last = item["event"]["data"]
        data = (
            {
                key: last[key]
                for key in ("engine", "processes", "more", "scan", "engine_more")
                if key in last
            }
            if last is not None
            else {"engine": [], "processes": [], "more": 0, "scan": "unknown"}
        )
        native_journal.append_events(
            _journal_root(prov),
            gen.binding,
            [{"kind": "background_settled", "data": {**data, "reason": "stop"}}],
        )


def _stop_under_way(worker_id: str) -> bool:
    """The worker accepted a Stop (#1724): a second one (double click, second tab) joins it."""
    try:
        return bool(native_state.read_lifecycle(worker_id).get("stop_accepted_at"))
    except (native_state.StateError, OSError):
        return False


class _Superseded(Exception):
    """A confirmed Stop's generation is no longer the session's current one (#1724)."""


def _stop_confirmed_sync(engine_id: str, session_id: str, worker_id: str, discard: bool) -> str:
    """The close/stop boundary of a confirmed Stop (#1724), revalidated under the lifecycle lock:
    it closes the session only while ``worker_id`` (the generation the operator confirmed) is
    still its current one, or it is closed already (another Stop). A successor that started in
    between — the confirmed worker accepted, exited and a new send launched another — was never
    confirmed: `_Superseded`, and nothing is stopped or closed."""
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        record = native_state.read_session(session_id)
        if record is None:
            raise NativeError(404, "no such native session")
        if not record.get("closed") and record.get("current_worker") not in (None, worker_id):
            raise _Superseded
        if discard and "launch" in record:
            del record["launch"]  # as `_discard_start` does: the operator's Stop drops the view
        return _stop_locked(engine_id, session_id, record, discard)


def _gone_locked(session_id: str, worker_id: str) -> bool:
    with native_state.session_lock(session_id, wait=STOP_TIMEOUT):
        return _gone(session_id, worker_id)


def _needs_confirmation(session_id: str, gen: Generation) -> bool:
    """A Stop is confirmed against the generation's background work only once it can have run
    any (#1724): a started session whose worker checks confirmations (`stop_confirm`, from the
    release that keeps work alive). Discarding a creation that never started, or a worker still
    starting, ends nothing an operator turn began; an older worker keeps the unconfirmed Stop."""
    record = native_state.read_session(session_id) or {}
    if _start_state(record) in {PENDING, STARTING, FAILED, EXPIRED}:
        return False
    life = native_state.read_lifecycle(gen.worker_id)
    return life.get("stop_confirm") is True and life.get("phase") == "ready"


async def _confirm_stop(engine_id: str, session_id: str, gen: Generation, confirm: dict) -> bool:
    """Ask the live worker to settle and stop ONLY if what the operator confirmed is what runs
    (#1724). Returns True when the worker accepted (it settled and is stopping its child), False
    when it was proved gone already. Every other outcome raises: a changed set is a 409 with the
    current inventory, a failed check a 503 — never a fall-through to the unit stop."""
    worker = confirm.get("worker")
    if worker not in (None, gen.worker_id):
        # The operator confirmed another generation's work: what runs now was never shown.
        raise NativeError(
            409,
            STOP_CHANGED,
            reason="inventory_changed",
            inventory=await asyncio.to_thread(_stop_inventory_sync, engine_id, session_id),
        )
    digest = confirm.get("digest") or native_ipc.stop_digest(gen.worker_id, (), (), "ok")
    journal = await asyncio.to_thread(
        _journal, _api_provider(engine_id, retiring_ok=True), session_id
    )
    try:
        await _call(
            gen,
            "stop",
            {
                "operation_id": str(uuid.uuid4()),
                "expected_revision": journal.revision,
                "target_worker_id": gen.worker_id,
                "confirm_digest": digest,
            },
            timeout=STOP_CHECK_TIMEOUT,
        )
    except _Unreachable:
        if await asyncio.to_thread(_gone_locked, session_id, gen.worker_id):
            return False  # nothing left to confirm: the unit stop below proves it
        if await asyncio.to_thread(_stop_under_way, gen.worker_id):
            return True  # a Stop already accepted for it is ending it: join that one
        raise await asyncio.to_thread(_unreachable_error, engine_id, session_id, gen) from None
    except NativeError as exc:
        if exc.reason != "inventory_changed" and await asyncio.to_thread(
            _stop_under_way, gen.worker_id
        ):
            return True  # a Stop already accepted for it is ending it: join that one
        if exc.reason == "inventory_changed":
            raise NativeError(
                409,
                STOP_CHANGED,
                reason="inventory_changed",
                inventory=await asyncio.to_thread(_stop_inventory_sync, engine_id, session_id),
            ) from None
        if exc.transport:
            # A refusal, never a fall-through: nothing stopped. The worker could not answer, so
            # the pane offers Stop everything: never a dead end for a wedged worker (Hermes r3).
            raise await asyncio.to_thread(_unreachable_error, engine_id, session_id, gen) from None
        # The worker answered and refused: its own refusal, nothing stopped.
        raise NativeError(exc.status, f"Stop not applied: {exc.detail}") from None
    return True


async def _confirm_everything(engine_id: str, session_id: str, gen: Generation) -> bool:
    """Stop everything (#1724, the operator's second kind of confirmation): disclosed as ending
    everything the agent started in this generation, listed or not, and bound to it (the caller
    checked the generation and closes through the pinned, lifecycle-locked boundary). The worker
    settles and stops on it: True. A worker that cannot answer — no socket, a lost connection, a
    timeout — cannot settle, but the explicit confirmation still reaches the unit stop: False
    (the runtime settles its last inventory). A worker that ANSWERS with a refusal did not
    accept: its refusal, and nothing is stopped (Hermes on PR #1757)."""
    journal = await asyncio.to_thread(
        _journal, _api_provider(engine_id, retiring_ok=True), session_id
    )
    try:
        await _call(
            gen,
            "stop",
            {
                "operation_id": str(uuid.uuid4()),
                "expected_revision": journal.revision,
                "target_worker_id": gen.worker_id,
                "confirm_everything": True,
            },
            timeout=STOP_CHECK_TIMEOUT,
        )
    except _Unreachable:
        return False
    except NativeError as exc:
        if exc.transport:
            return False
        if await asyncio.to_thread(_stop_under_way, gen.worker_id):
            return True  # a Stop already accepted for it is ending it: join that one
        raise NativeError(exc.status, f"Stop not applied: {exc.detail}") from None
    return True


async def stop(
    engine_id: str, session_id: str, *, discard: bool = True, confirm: dict | None = None
) -> dict:
    """Close the session and prove containment. ``confirm`` (#1724, the operator's Stop), bound
    to ``worker``: EXACT — ``digest`` of a complete inventory the pane showed, rechecked by the
    live worker against a fresh scan before anything is stopped; or EVERYTHING —
    ``everything: True``, disclosed as ending all the agent started in that generation. Either
    closes only through the pinned, lifecycle-locked boundary. Without ``confirm`` (archive,
    internal callers) the existing unconfirmed path runs."""
    prov, _ = await asyncio.to_thread(partial(_session, engine_id, session_id, retiring_ok=True))
    if confirm is not None:
        gen = await asyncio.to_thread(_generation, session_id)
        if gen is not None and confirm.get("worker") not in (None, gen.worker_id):
            # The operator confirmed another generation's work (a Stop everything included); this
            # one — a successor — was never shown: refresh, nothing stopped (Hermes on #1757).
            raise NativeError(
                409,
                STOP_CHANGED,
                reason="inventory_changed",
                inventory=await asyncio.to_thread(_stop_inventory_sync, engine_id, session_id),
            )
        under_way = gen is not None and await asyncio.to_thread(_stop_under_way, gen.worker_id)
        if under_way or (
            gen is not None and await asyncio.to_thread(_needs_confirmation, session_id, gen)
        ):
            accepted = True
            if under_way:
                pass  # a Stop of THIS generation was accepted: join it at the pinned close below
            elif confirm.get("everything"):
                accepted = await _confirm_everything(engine_id, session_id, gen)
            else:
                await _confirm_stop(engine_id, session_id, gen, confirm)
            try:
                contained = await asyncio.to_thread(
                    _stop_confirmed_sync, engine_id, session_id, gen.worker_id, discard
                )
            except _Superseded:
                # The confirmed worker ended and a successor started before the close: nothing
                # of the successor was confirmed, so nothing of it is stopped (Hermes, #1757).
                raise NativeError(
                    409,
                    STOP_CHANGED,
                    reason="inventory_changed",
                    inventory=await asyncio.to_thread(_stop_inventory_sync, engine_id, session_id),
                ) from None
            if not accepted and contained == "gone":
                # The worker could not settle itself: its last journaled work ended by Stop.
                await asyncio.to_thread(_settle_unreachable, prov, session_id, gen)
            return {"containment": contained}
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
