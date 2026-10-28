"""The contained per-session native protocol worker (#1278).

Started only by ``native_runtime`` as ``python -I -m agent_sessions.native_worker`` inside a
fresh transient systemd user service (``native_containment.launch_argv``). It is a protocol
adapter: it owns the native CLI's stdin/stdout, the session writer locks and the durable
journal records for its generation. It serializes queued operator messages and explicit
Send now selection (native steering or cancel-then-dispatch), judges nothing
and stores no credentials of its own.

Lifecycle, in order (each step refuses rather than guesses):

1. Read the private config from ``<state>/workers/<id>/config.json``; adopt the server's
   path environment so locks, ownership and roster resolve to the SAME shared directories.
2. Under the session lifecycle lock: refuse unless this generation is the current, open one,
   then take the per-app-session writer lock for the worker's lifetime.
3. Bound histories take the source writer lock BEFORE the native child exists. New Codex and
   opencode histories bind under launch admission from a correlated thread/start or session/new
   response. Only those fresh histories may coexist with unresolved console placeholders; their
   own writer locks are taken before any turn can be accepted.
4. Serve the private socket: peer UID, capability handshake, then closed IPC frames. Every
   effect is claimed durably in the journal (fsync) before its stdin write; exact replays
   observe the recorded receipt and write nothing.
5. Native EOF, a protocol violation or ``stop`` ends the generation. The worker never restarts
   its child: a later turn needs a NEW generation launched by the web process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socket
import stat
import struct
import sys
import time
from pathlib import Path

from . import (
    agent_tools,
    native_attachments,
    native_containment,
    native_exec,
    native_ipc,
    native_journal,
    native_memory,
    native_protocol,
    native_questions,
    native_segments,
    native_state,
)
from .plugins import kinds

EXIT_REFUSED = 3
EXIT_FAILED = 4
INIT_TIMEOUT = 90.0
IDLE_TIMEOUT = 1800.0  # an idle worker exits; the next turn resumes in a new generation
SHUTTING_DOWN = "the native worker is shutting down; retry"
SEND_NOW_WOULD_STOP = (
    "Send now would also stop this agent's background agents here; "
    "the message is sent when the current turn ends"
)
INVENTORY_CHANGED = (
    "the agent's background tasks or turn changed since you confirmed; "
    "review them and confirm again"
)
FLUSH_INTERVAL = 0.2
STDERR_TAIL = 4096
_URGENT = frozenset(
    {
        "approval",
        "approval_cancelled",
        "turn_started",
        "turn_completed",
        "input_accepted",
        "delivery_failed",
        "disconnected",
        "session",
    }
)


OPENCODE = "opencode-acp"
COPILOT = "copilot-acp"
PROBE_TIMEOUT = 30.0
PROBE_LIMIT = 4 * 1024 * 1024
#: How long the worker waits, once the agent's output ends, for the agent to exit by itself before
#: it stops it (#1732): an agent that died is reaped with its OWN return code, never one a stop
#: gave it.
REAP_WINDOW = 2.0
#: The turn of an OOM-counter read that failed: it matches no turn, so the next rise is
#: session-level (#1732).
_UNREAD = ("unread", "")
#: Why the agent ended, as the worker can state it (#1732): only facts, never a cause.
SIGKILL_DURING_OOM = "sigkill_during_oom"


class WorkerError(RuntimeError):
    pass


class AdmissionRefused(WorkerError):
    """An expected refusal of a queued message at delayed handoff (#1538), before anything was
    written to the agent. ``code`` is one of `native_journal.REASON_CODES`; the operator sees
    the fixed text the pane derives from it, never this exception's message."""

    MESSAGES = {
        "agent_removed": "the queued message's agent was removed",
        "agent_changed": "the queued message's agent changed",
        "source_store_changed": "the queued message's source store changed",
        "generation_closed": "this worker generation is closed",
        "question_replaced": "its question was answered or replaced",
    }

    def __init__(self, code: str):
        if code not in self.MESSAGES:
            raise ValueError(f"unknown queued refusal {code!r}")
        super().__init__(self.MESSAGES[code])
        self.code = code


log = logging.getLogger(__name__)


def log_queue_refusal(session_key: str, operation_id: str, code: str, exc: BaseException) -> None:
    """One line per operator message settled ``not_sent`` before stdin (#1538): the session key,
    the operation id, the reason code and the exception's TYPE NAME only. Never its message,
    which can carry paths, CLI output or credentials."""
    log.warning(
        "queued message not sent: session=%s operation=%s reason=%s error=%s",
        session_key,
        operation_id,
        code,
        type(exc).__name__,
    )


def _log_to_journal() -> None:
    """The contained worker's stdio is null (`native_containment.launch_argv`): its refusal lines
    go to the user's journal through the local syslog socket instead, tagged
    ``agent-sessions-native``. No socket (a container, a test) means no log, never a failure."""
    if not os.path.exists("/dev/log") or log.handlers:
        return
    try:
        handler = logging.handlers.SysLogHandler(address="/dev/log")
    except OSError:
        return
    handler.ident = "agent-sessions-native: "
    handler.setLevel(logging.WARNING)
    log.addHandler(handler)
    log.setLevel(logging.WARNING)


def admit_queued(roster, session_key: str, adapter: str, config: dict) -> None:
    """Is the agent a queued message was accepted for still the one this worker runs (#1538)?

    Raises `AdmissionRefused` with the reason. Every launch gate holds again: the API client and
    its source are in the fresh roster, the protocol and source engine are the launched ones, and
    the source store has the same owner. The source's binary is resolved AGAIN, through
    provenance, and must be selected the way it was at launch (`api_source.same_selection`). Its
    resolved path may differ: a vendor self-update retargets the launcher, while the message goes
    to the process that has been running since launch.
    """
    from . import native_ownership
    from .engine_errors import EngineError
    from .plugins import api_source

    prov = roster.by_id.get(session_key.partition(":")[0])
    api = getattr(getattr(prov, "manifest", None), "api", None)
    if prov is None or api is None or getattr(prov, "retiring", False):
        raise AdmissionRefused("agent_removed")
    source = roster.by_id.get(api.source)
    if source is None or getattr(source, "retiring", False):
        raise AdmissionRefused("agent_removed")
    try:
        binding = api_source.resolve(prov, roster=roster)
    except EngineError:
        raise AdmissionRefused("agent_changed") from None
    if api.kind != adapter or binding.source.engine_id != config["source_engine"]:
        raise AdmissionRefused("agent_changed")
    pinned = config.get("entrypoint")
    try:
        ep = binding.source.entrypoint_selection()
    except EngineError:
        raise AdmissionRefused("agent_changed") from None
    if pinned is None:
        # A configuration written before #1538 pinned only the path: keep its exact rule.
        if ep.path != config["binary"]:
            raise AdmissionRefused("agent_changed")
    elif not api_source.same_selection(pinned, ep, binding.source.trust):
        raise AdmissionRefused("agent_changed")
    owner = native_ownership.lookup(session_key)
    if owner is None or owner.source != native_ownership.source_identity(binding.source):
        raise AdmissionRefused("source_store_changed")


def _private_file(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or st.st_size > native_state.MAX_RECORD_BYTES
        ):
            raise WorkerError("worker config is not a private bounded file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            doc = json.loads(fh.read())
    finally:
        os.close(fd)
    if not isinstance(doc, dict):
        raise WorkerError("worker config is malformed")
    return doc


def _peer_uid(sock: socket.socket) -> int | None:
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def native_environment(config: dict) -> dict[str, str]:
    """The child's whole environment: no inherited server secrets, vendor stores by HOME."""
    env = {
        "HOME": config["home"],
        "PATH": config["path"],
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    from .resource_limits import THREAD_VARIABLES

    # Frozen at generation launch; old configs retain their original environment.
    threads = config.get("thread_environment", {})
    if not isinstance(threads, dict) or any(
        key not in THREAD_VARIABLES or not isinstance(value, str) or "\0" in value
        for key, value in threads.items()
    ):
        raise WorkerError("invalid library thread environment")
    env.update(threads)
    for key in ("USER", "LOGNAME"):
        if isinstance(config.get(key.lower()), str):
            env[key] = config[key.lower()]
    return env


def memory_environment(config: dict) -> dict[str, str]:
    """The shared project memory paths this generation was given (#1441), or ``{}``.

    Bound by the web process at generation launch and passed explicitly — never read from this
    process's environment. Both variables or neither, absolute paths, nothing else.
    """
    from .memory import ALL_ENV_KEYS, ENV_KEYS

    value = config.get("memory_env", {})
    if not isinstance(value, dict):
        raise WorkerError("invalid memory environment")
    if value and (
        set(value) not in (set(ENV_KEYS), set(ALL_ENV_KEYS))
        or any(
            not isinstance(path, str) or not path.startswith("/") or "\0" in path or "\n" in path
            for path in value.values()
        )
    ):
        raise WorkerError("invalid memory environment")
    return dict(value)


def memory_tool(config: dict):
    """The memory tool this generation carries (#1506), as an `agent_tools.Server`, or None.

    Only beside the generation's memory paths, and only the kind the adapter's protocol carries
    (`kinds.API_MCP_KINDS`); anything else refuses the spawn. Shaped from this worker's own
    interpreter, never from the config.
    """
    try:
        kind = agent_tools.api_kind(config["adapter"], config.get("memory_tool"))
    except ValueError:
        raise WorkerError("invalid memory tool") from None
    paths = memory_environment(config)
    if kind is None:
        return None
    if not paths:
        raise WorkerError("invalid memory tool")
    return agent_tools.server(paths)


def tool_servers(config: dict) -> list:
    """Every BattleLab tool this generation carries: the memory tool (above) and, for a session
    started from a specialist (#1693), the skills tool over its launch bundle. A bundle on an
    adapter whose protocol carries no MCP server refuses the spawn: a specialist never runs
    without the skills it was saved with."""
    out = [t for t in (memory_tool(config),) if t is not None]
    bundle = config.get("skills_bundle")
    if bundle is None:
        return out
    if not kinds.API_MCP_KINDS.get(config["adapter"]):
        raise WorkerError("this agent cannot load skills")
    try:
        skills = agent_tools.skills_server(bundle)
    except ValueError:
        raise WorkerError("invalid skill bundle") from None
    return [*out, skills]


def battlelab_session(config: dict) -> str:
    """The BattleLab session key the agent records as its own id (#1406), never the native id.

    A non-secret identifier (the `/s/<engine>/<id>` link). The web process composed it from the
    admitted roster; here it must be exactly the canonical API key shape (`native_ipc`'s gate,
    lowercase engine id and UUID, nothing around them), or the spawn is refused rather than
    telling the agent a wrong id. The worker does not consult its own roster for it.
    """
    try:
        return native_ipc.validate_session_key(config.get("session_key"))
    except native_ipc.IPCError:
        raise WorkerError("the session key is not canonical") from None


def child_environment(
    config: dict,
    server_password: str | None = None,
    agent: str | None = None,
    mcp: dict | None = None,
) -> dict[str, str]:
    """`native_environment` plus what one adapter's child needs and nothing else.

    `opencode acp` also opens an HTTP server on loopback that answers without authentication
    (provider configuration included) unless `OPENCODE_SERVER_PASSWORD` is set (#1312 spike).
    The password is minted by THIS process for this child only: it is never in the worker
    config, a journal record, the lifecycle state or any argv, so it reaches nothing but the
    child's environment (readable only by this same user, like every vendor credential).
    `OPENCODE_CONFIG_CONTENT` defines BattleLab's own guarded or bypass agent (``agent``, minted per
    generation, the same construction as the unattended launch's) that the session is pinned to.
    The operator's own config and console opencode are untouched, and the app's own
    `OPENCODE_CONFIG_CONTENT` is not passed on (the environment is sanitized).
    A copilot child gets `COPILOT_AUTO_UPDATE=false` beside its argv's `--no-auto-update` (#1565):
    the CLI otherwise self-downloads and re-execs outside the provenance-bound entrypoint.
    Every adapter's child also gets `BATTLELAB_SESSION` (`battlelab_session`), and the shared
    project memory paths when its generation was given them (`memory_environment`).
    """
    env = native_environment(config)
    env["BATTLELAB_SESSION"] = battlelab_session(config)
    env.update(memory_environment(config))
    if config["adapter"] == OPENCODE:
        if not isinstance(server_password, str) or len(server_password) < 32:
            raise WorkerError("the opencode listener password is missing")
        from .engines import opencode

        env["OPENCODE_SERVER_PASSWORD"] = server_password
        try:
            env["OPENCODE_CONFIG_CONTENT"] = opencode.api_config_content(
                agent, mcp, bypass=config.get("bypass") is True
            )
        except ValueError as exc:
            raise WorkerError(str(exc)) from None
    if config["adapter"] == COPILOT:
        env["COPILOT_AUTO_UPDATE"] = "false"
    return env


class Worker:
    def __init__(self, config: dict, state_dir: Path):
        self.config = config
        self.state_dir = state_dir
        self.worker_id = config["worker_id"]
        self.session_key = config["session_key"]
        self.session_id = self.session_key.partition(":")[2]
        self.adapter = config["adapter"]
        self.journal_root = Path(config["journal_root"])
        self.capability = native_ipc.Capability(config["capability"])
        self.binding = native_ipc.Binding(
            self.session_key, self.worker_id, config["connection_id"], self.adapter
        )
        self.native_id: str | None = config.get("native_id")
        self.codec: (
            native_protocol.CodexCodec
            | native_protocol.ClaudeCodec
            | native_protocol.OpencodeAcpCodec
            | native_protocol.CopilotAcpCodec
        )
        self.agent: str | None = None
        if self.adapter == "codex-app-server":
            self.codec = native_protocol.CodexCodec(effort=config.get("effort"))
        elif self.adapter == OPENCODE:
            from .engines import opencode

            self.agent = (
                opencode.mint_api_agent()
            )  # this generation's own immutable permission mode
            self.codec = native_protocol.OpencodeAcpCodec(self.native_id, agent=self.agent)
        elif self.adapter == COPILOT:
            self.codec = native_protocol.CopilotAcpCodec(self.native_id)
        else:
            self.codec = native_protocol.ClaudeCodec(self.native_id)
        # Minted by this process for this generation only; see `child_environment`.
        self._server_password = secrets.token_urlsafe(32) if self.adapter == OPENCODE else None
        self.locks: list = []
        self.proc: asyncio.subprocess.Process | None = None
        self.server: asyncio.AbstractServer | None = None
        self.pending: list[dict] = []
        self.flush_now = asyncio.Event()
        self.effects = asyncio.Lock()
        self.queued: list[dict] = []
        self.priority_turn: str | None = None
        # Queued messages a Send now promoted: each goes ahead of the agent's own queue (#1475).
        self.sent_now: set[str] = set()
        self.priority_requests: dict[str, tuple[str, str, str, str]] = {}
        self.queue_wake = asyncio.Event()
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.waiters: dict[str, asyncio.Future] = {}
        self.after_write: list[native_protocol.NativeEvent] = []
        self.stderr_tail = b""
        self.reason = ""
        self.closing = False
        self.terminated = False
        self.last_activity = time.monotonic()
        self.idle_timeout = float(config.get("idle_timeout") or IDLE_TIMEOUT)
        self.reported_send_now: str | None = None
        # OOM kills in this worker's unit (#1732). `_oom_base`: the hierarchical counter when the
        # worker started; `_oom_seen`: the last count journaled (kills since the base);
        # `_oom_turn`: the turn in flight at the last read; `_turn_key`: the turn the worker last
        # saw the codec in. `_oom_pin`: the pinned (group, invocation) once the runtime wrote it.
        self._oom_base: int | None = None
        self._oom_seen = 0
        self._oom_turn: tuple[str, str] | None = None
        self._turn_key: tuple[str, str] | None = None
        self._oom_pin: tuple[str, str] | None = None
        self._oom_invocation: str | None = None  # of the last successful read

    # --- lifecycle -------------------------------------------------------------------------

    def report(self, **fields) -> None:
        with contextlib.suppress(native_state.StateError, OSError):
            native_state.update_lifecycle(self.worker_id, **fields)

    def admit(self) -> None:
        from . import sessionlock

        with native_state.session_lock(self.session_id):
            if not native_state.admitted(self.session_id, self.worker_id):
                raise WorkerError("this worker generation is no longer current")
            lock = sessionlock.acquire(self.session_key)
            if lock is None:
                raise WorkerError("another worker owns this API session")
            self.locks.append(lock)
        if self.native_id is not None:
            self.lock_source(self.native_id)

    def lock_source(self, native_id: str) -> None:
        from . import sessionlock

        lock = sessionlock.acquire(f"{self.config['source_engine']}:{native_id}")
        if lock is None:
            raise WorkerError("the native history has another BattleLab writer")
        self.locks.append(lock)

    def argv(self) -> list[str]:
        binary = self.config["binary"]
        if self.adapter == "codex-app-server":
            return native_protocol.CodexCodec.argv(binary)
        if self.adapter == OPENCODE:
            return native_protocol.OpencodeAcpCodec.argv(binary)
        if self.adapter == COPILOT:
            tools = tool_servers(self.config)
            return native_protocol.CopilotAcpCodec.argv(
                binary, mcp_config=agent_tools.copilot_mcp_config(tools) if tools else None
            )
        tools = tool_servers(self.config)
        return native_protocol.ClaudeCodec.argv(
            binary,
            self.native_id,
            resume=self.config["mode"] == "resume",
            model=self.config.get("model"),
            bypass=self.config.get("bypass") is True,
            effort=self.config.get("effort"),
            mcp_config=agent_tools.claude_mcp_config(tools) if tools else None,
        )

    async def spawn(self) -> None:
        if self.terminated:
            raise WorkerError("stopped before the native agent started")
        guard = self.store_admission()
        try:
            # The probe runs under the same admission as the launch it configures. Its result
            # (operator MCP command lines) lives only in this child's environment: never in a
            # journal record, lifecycle state, the worker config or a log.
            mcp = await self.mcp_overrides() if self.adapter == OPENCODE else None
            self.proc = await asyncio.create_subprocess_exec(
                *self.spawn_argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config["cwd"],
                env=child_environment(self.config, self._server_password, self.agent, mcp),
                limit=native_protocol.MAX_FRAME_BYTES + 1,
            )
        finally:
            # Held through process creation only: from here the child is visible to the
            # compactor's process scan, which refuses to run while it holds the database.
            if guard is not None:
                guard.release()
        self.report(phase="native_started", native_pid=self.proc.pid)

    def spawn_argv(self) -> list[str]:
        """The agent's argv behind the exec trampoline (#1732, `native_exec`): the agent and
        everything it starts rank above this worker as an OOM victim. The worker's own value is
        never changed. Unreadable (or no absolute interpreter): the agent starts directly, as
        before #1732."""
        argv = self.argv()
        own = native_exec.own_value()
        if own is None or not sys.executable.startswith("/"):
            return argv
        return native_exec.argv(sys.executable, native_exec.raised(own), argv)

    async def mcp_overrides(self) -> dict:
        """`opencode debug config` in the session's directory, with the sanitized environment and
        a fresh password of its own, bounded in time and size; then the per-server overrides
        that keep the listener password out of local MCP servers (Hermes on #1336). Any failure
        refuses the launch: never an opencode child without the blanking."""
        from .engines import opencode

        env = native_environment(self.config)
        env["OPENCODE_SERVER_PASSWORD"] = secrets.token_urlsafe(32)
        try:
            probe = await asyncio.create_subprocess_exec(
                *native_protocol.OpencodeAcpCodec.config_probe_argv(self.config["binary"]),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.config["cwd"],
                env=env,
            )
        except (OSError, native_protocol.ProtocolError) as exc:
            raise WorkerError(f"the opencode configuration probe could not start: {exc}") from None
        try:
            out = await asyncio.wait_for(probe.stdout.read(PROBE_LIMIT + 1), PROBE_TIMEOUT)
            code = await asyncio.wait_for(probe.wait(), PROBE_TIMEOUT)
        except TimeoutError:
            raise WorkerError("the opencode configuration probe timed out") from None
        finally:
            if probe.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    probe.kill()
                await probe.wait()
        if code != 0 or len(out) > PROBE_LIMIT:
            raise WorkerError("the opencode configuration probe failed")
        try:
            return opencode.mcp_password_overrides(json.loads(out))
        except (ValueError, UnicodeError) as exc:
            raise WorkerError(f"the opencode configuration could not be used: {exc}") from None

    def store_admission(self):
        """opencode's shared SQLite store: take the SAME shared admission a console launch takes
        (`opencode_admission`, #993), keyed by the database the web process resolved for the
        source (`store_database`), so compaction through ANY engine on that file and this launch
        can never interleave — a source alias included (Hermes on #1336)."""
        if self.adapter != OPENCODE:
            return None
        from . import opencode_admission

        try:
            guard = opencode_admission.acquire(
                self.config["source_engine"],
                exclusive=False,
                database=self.config["store_database"],
            )
        except (OSError, KeyError, TypeError) as exc:
            raise WorkerError(opencode_admission.REFUSAL) from exc
        if guard is None:
            raise WorkerError(opencode_admission.REFUSAL)
        return guard

    # --- native protocol -------------------------------------------------------------------

    async def write(self, frame: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        # BattleLab's own frames; a turn's inlined pictures may exceed the read cap (#1332).
        self.proc.stdin.write(native_protocol.encode(frame, native_protocol.MAX_WRITE_FRAME_BYTES))
        # A child that stops reading must not hold the effect lock (and with it stop) forever.
        await asyncio.wait_for(self.proc.stdin.drain(), 15)

    def record(self, events) -> None:
        for event in events:
            if event.kind == "send":
                continue
            data = dict(event.data)
            if event.kind == "approval":
                data.update(worker_id=self.worker_id, connection_id=self.binding.connection_id)
            item = {"kind": event.kind, "data": data}
            last = self.pending[-1] if self.pending else None
            if (
                last is not None
                and item["kind"] == last["kind"] == "text"
                and data.get("partial")
                and last["data"].get("partial")
                and data.get("operation_id") == last["data"].get("operation_id")
                and data.get("agent_turn") == last["data"].get("agent_turn")
                and data.get("item_id") == last["data"].get("item_id")
                and len(last["data"]["text"]) + len(data["text"]) <= native_ipc.MAX_EVENT_TEXT
            ):
                last["data"]["text"] += data["text"]
                last["data"]["truncated"] = last["data"]["truncated"] or data["truncated"]
                continue
            self.pending.append(item)
            if event.kind in _URGENT or len(self.pending) >= native_ipc.MAX_EVENTS:
                self.flush_now.set()

    def flush(self) -> None:
        while self.pending:
            batch, self.pending = (
                self.pending[: native_ipc.MAX_EVENTS],
                self.pending[native_ipc.MAX_EVENTS :],
            )
            try:
                native_journal.append_events(self.journal_root, self.binding, batch)
            except native_journal.JournalError as exc:
                # A journal that cannot record observations must not keep accepting effects.
                self.reason = f"journal unavailable: {exc}"
                self.journal_failed(exc)
                self.stop_child()
                return

    async def flusher(self) -> None:
        while not self.done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.flush_now.wait(), FLUSH_INTERVAL)
            self.flush_now.clear()
            self.flush()

    def _turn_busy(self) -> bool:
        codec = self.codec
        return (
            codec.operation_id is not None
            or bool(self.priority_requests)
            or bool(getattr(codec, "_steers", None))
            or bool(codec._approvals)
            or getattr(codec, "_session_state", None) in {"running", "requires_action"}
            or getattr(codec, "unowned_turn", False)
        )

    def native_busy(self) -> bool:
        """Anything running at all, background tasks included: the worker must stay alive."""
        return self._turn_busy() or bool(getattr(self.codec, "_background", None))

    def dispatch_busy(self) -> bool:
        """May an operator message go to the agent now (#1427)?

        A running background task alone does not stop it once this generation's codec has PROVED
        it can tell the operator's output from a turn the agent starts for itself. Until then the
        old rule holds: a task could start such a turn, whose output would read as the reply.
        """
        if getattr(self.codec, "lifecycle_proven", False):
            return self._turn_busy()
        return self.native_busy()

    def busy(self) -> bool:
        return bool(self.queued) or self.native_busy()

    def _sent_now_cuts(self) -> bool:
        """Does the head, a message Send now selected, go now although the agent runs a turn of
        its own (#1475)? That turn may have started in the gap after the interrupt, before this
        dispatch; the message's `priority: "now"` cuts it, which is what Send now asked for. Only
        once the lifecycle is proved (the fence tells that turn's output from the message's) and
        with nothing else of the operator's in flight: no turn, no pending control, no approval."""
        codec = self.codec
        return (
            bool(self.queued)
            and self.queued[0]["operation_id"] in self.sent_now
            and codec.submit_now_cuts_own_turn
            and getattr(codec, "lifecycle_proven", False)
            and codec.operation_id is None
            and not self.priority_requests
            and not getattr(codec, "_steers", None)
            and not codec._approvals
        )

    def memory_sample(self, tracker: native_memory.Tracker) -> dict:
        """One reading of this worker's own unit (#1643): only the group `/proc/self/cgroup` names
        when it is this unit AND the group the runtime pinned; anything else reads unavailable."""
        unit = native_containment.WorkerIdentity(self.worker_id).unit
        life = native_state.read_lifecycle(self.worker_id)
        try:
            proc = Path("/proc/self/cgroup").read_text()
        except OSError:
            proc = ""
        group = native_memory.own_group(proc, unit)
        pinned = group is not None and group == life.get("control_group")
        reading = native_memory.sample(group) if pinned else None
        level = tracker.update(reading)
        return native_memory.state(
            level, tracker.shown, life.get("invocation_id"), tracker.throttled
        )

    async def memory_watch(self) -> None:
        """Sample memory pressure every INTERVAL; record a warning while it lasts (fresh `at`),
        and every change of level, so a reader can tell a live warning from a stale one."""
        tracker, written = native_memory.Tracker(), None
        while not self.done.is_set():
            with contextlib.suppress(native_state.StateError, OSError):
                memory = await asyncio.to_thread(self.memory_sample, tracker)
                if memory["level"] in native_memory.SHOWN or memory["level"] != written:
                    await asyncio.to_thread(self.report, memory=memory)
                    if memory["level"] != written:
                        # The change itself, on the event stream (#1643): a reader learns of it at
                        # once, and the journal keeps when each warning began and ended.
                        self.record(
                            [
                                native_protocol.NativeEvent(
                                    "memory_pressure",
                                    {
                                        key: memory.get(key)
                                        for key in (
                                            "level",
                                            "working_set",
                                            "max",
                                            "scope",
                                            "throttled",
                                        )
                                    },
                                )
                            ]
                        )
                    written = memory["level"]
            # The periodic OOM-counter read (#1732); turn transitions read it too.
            if not self.turn_transition():
                self.oom_read(self._turn_key, self._turn_key)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.done.wait(), native_memory.INTERVAL)

    # --- OOM kills in the unit (#1732) -----------------------------------------------------

    def _own_group(self) -> str | None:
        try:
            proc = Path("/proc/self/cgroup").read_text()
        except OSError:
            return None
        unit = native_containment.WorkerIdentity(self.worker_id).unit
        return native_memory.own_group(proc, unit)

    def oom_baseline(self) -> None:
        """The counter at the worker's start, before the agent exists: kills before it are not
        this generation's. Unreadable: the first successful read becomes the base (a kill before
        it is never claimed)."""
        group = self._own_group()
        self._oom_base = None if group is None else native_memory.oom_kills(group)

    def oom_count(self) -> tuple[int, str] | None:
        """(kills since the base, the pinned invocation), read from the worker's own unit only
        once the runtime pinned it (the same rule as `memory_sample`); None when unreadable."""
        if self._oom_pin is None:
            group = self._own_group()
            if group is None:
                return None
            life = native_state.read_lifecycle(self.worker_id)
            if group != life.get("control_group") or not isinstance(life.get("invocation_id"), str):
                return None
            self._oom_pin = (group, life["invocation_id"])
        total = native_memory.oom_kills(self._oom_pin[0])
        if total is None:
            return None
        if self._oom_base is None or total < self._oom_base:
            self._oom_base = total
        return total - self._oom_base, self._oom_pin[1]

    def _turn_now(self) -> tuple[str, str] | None:
        codec = self.codec
        if codec.operation_id is not None:
            return ("operation_id", codec.operation_id)
        if codec.background_turn is not None:
            return ("agent_turn", codec.background_turn)
        return None

    def turn_transition(self) -> bool:
        """Did the codec's turn change since the worker last looked? Then read the counter now,
        before the change is journaled. True when that read journaled a kill."""
        now = self._turn_now()
        if now == self._turn_key:
            return False
        before, self._turn_key = self._turn_key, now
        return self.oom_read(before, now)

    def oom_read(self, before, after) -> bool:
        """Read the counter; journal a rise since the last journaled count as ONE
        `memory_oom_kill` (cumulative count, invocation). It names a turn only when that turn
        was in flight at the previous read AND up to this one (``before``); otherwise it is a
        session-level notice. ``after`` is the turn in flight from this read on. True when a
        kill was journaled."""
        try:
            read = self.oom_count()
        except (native_state.StateError, OSError):
            read = None
        if read is None:
            self._oom_turn = _UNREAD  # where the next rise began is unknown: never a turn's
            return False
        count, self._oom_invocation = read
        rose = count > self._oom_seen
        if rose:
            data = {"count": count, "invocation_id": self._oom_invocation}
            if before is not None and before == self._oom_turn:
                data[before[0]] = before[1]
            self.record([native_protocol.NativeEvent("memory_oom_kill", data)])
            self._oom_seen = count
        self._oom_turn = after
        return rose

    async def reap_window(self) -> None:
        """The agent's output ended (#1732). Give it REAP_WINDOW to exit by itself: then its
        own return code stands, the counter is read once more, and if the agent was SIGKILLed
        while the count rose since the last journaled read, that is recorded (the facts, not a
        cause: a tool's OOM and an unrelated SIGKILL in one window read the same). Still alive,
        or stopped by BattleLab meanwhile: nothing is derived; the stop path runs as before."""
        proc = self.proc
        deadline = time.monotonic() + REAP_WINDOW
        while proc.returncode is None and not self.terminated and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if proc.returncode is None or self.terminated:
            return
        seen = self._oom_seen
        if not self.oom_read(self._turn_key, None):
            return
        if proc.returncode == -signal.SIGKILL:
            await asyncio.to_thread(
                self.report,
                agent_exit={
                    "reason": SIGKILL_DURING_OOM,
                    "returncode": proc.returncode,
                    "oom_kills": self._oom_seen - seen,
                    "invocation_id": self._oom_invocation,
                    "at": time.time(),
                },
            )

    async def idle_watch(self) -> None:
        """Exit when nothing is running; a later turn resumes the history in a new generation."""
        while not self.done.is_set():
            await asyncio.sleep(min(30.0, max(0.05, self.idle_timeout / 4)))
            if self.busy():
                self.last_activity = time.monotonic()
                continue
            if time.monotonic() - self.last_activity < self.idle_timeout:
                continue
            async with self.effects:  # no effect is between claim and handoff
                if self.busy():
                    continue
                self.closing = True
                self.report(idle_exit_at=time.time())
                self.stop_child()
                return

    def promote_queued(self, operation_id: str) -> None:
        selected = next((p for p in self.queued if p["operation_id"] == operation_id), None)
        if selected is not None:
            self.queued.remove(selected)
            self.queued.insert(0, selected)
            self.sent_now.add(operation_id)

    async def handle_events(self, events) -> None:
        self.last_activity = time.monotonic()
        # Before this batch is journaled: a turn that starts or settles here closes the OOM
        # counter's window first (#1732).
        self.turn_transition()
        delivery_events = []
        for event in events:
            if event.kind in {"response", "error"}:
                target = self.priority_requests.pop(event.data.get("request_id"), None)
                if target is not None and event.kind == "error":
                    # A reply from a finished turn cannot release a newer selection.
                    if self.priority_turn == target[3]:
                        self.priority_turn = None
                    delivery_events.append(
                        native_protocol.NativeEvent(
                            "delivery_failed",
                            {
                                "operation_id": target[0],
                                "native_turn_id": target[1],
                                "control_id": target[2],
                                "message": event.data.get("message")
                                or "the agent refused interruption",
                            },
                        )
                    )
                elif target is not None and self.priority_turn == target[3]:
                    self.promote_queued(target[0])
            if event.kind == "send":
                await self.write(event.data["frame"])
            elif event.kind in {"initialized", "session", "error"}:
                request_id = event.data.get("request_id")
                waiter = self.waiters.pop(request_id, None) if request_id else None
                if waiter is not None and not waiter.done():
                    waiter.set_result(event)
        self.record([*events, *delivery_events])
        self.queue_wake.set()
        await self.report_send_now_state()

    def send_now_state(self) -> str:
        """What Send now would do right now (#1644), for the composer: ``ready`` (nothing in the
        background is stopped; a protocol without that distinction is never refused for it) or
        ``would_stop_background``. A display hint only: every Send now is decided at its own
        admission."""
        would_stop = getattr(self.codec, "send_now_would_stop_background", None)
        return "would_stop_background" if would_stop is not None and would_stop() else "ready"

    async def report_send_now_state(self) -> None:
        state = self.send_now_state()
        if state == self.reported_send_now:
            return
        self.reported_send_now = state
        await asyncio.to_thread(self.report, send_now_state=state)

    @contextlib.contextmanager
    def queued_admission(self):
        """Recheck the current generation and source at delayed stdin handoff (#1378, #1538)."""
        from .engines import registry
        from .plugins import storage

        with native_state.session_lock(self.session_id, wait=0), storage.locked("launch", wait=0):
            if not self.gate_open() or self.terminated or self.closing:
                raise AdmissionRefused("generation_closed")
            with registry.snapshot_scope(fresh=True, require_current=True) as roster:
                admit_queued(roster, self.session_key, self.adapter, self.config)
                yield

    async def dispatch_queue(self) -> None:
        """Only this live connection may drain its FIFO; a successor never replays it."""
        from .plugins import storage

        try:
            while not self.done.is_set():
                await self.queue_wake.wait()
                self.queue_wake.clear()
                retry = False
                async with self.effects:
                    while self.queued and (not self.dispatch_busy() or self._sent_now_cuts()):
                        if self.terminated or self.closing or self.done.is_set():
                            return
                        # Completion must be durable before the following turn can start.
                        self.flush()
                        if self.terminated:
                            return
                        params = self.queued[0]
                        admitted = False
                        try:
                            if not await self._answer_current(params):
                                raise AdmissionRefused("question_replaced")
                            with self.queued_admission():
                                admitted = True
                                self.queued.pop(0)
                                now = params["operation_id"] in self.sent_now
                                self.sent_now.discard(params["operation_id"])
                                await self.handoff("submit", params, queued=True, now=now)
                        except (native_state.LockBusy, storage.LockBusy):
                            if admitted:
                                raise
                            # Another session's launch is not a refusal. Keep the FIFO head
                            # queued, then retry outside all locks so Stop stays responsive.
                            retry = True
                            break
                        except Exception as exc:
                            # Admission failure proves non-send only BEFORE handoff. Its reason
                            # is a closed code (#1538); anything unforeseen is `unexpected`, and
                            # only its type name is logged.
                            if admitted:
                                raise
                            code = exc.code if isinstance(exc, AdmissionRefused) else "unexpected"
                            log_queue_refusal(self.session_key, params["operation_id"], code, exc)
                            self.queued.pop(0)
                            self.sent_now.discard(params["operation_id"])
                            await asyncio.to_thread(
                                native_journal.record_handoff,
                                self.journal_root,
                                self.binding,
                                params["operation_id"],
                                "not_sent",
                                reason_code=code,
                            )
                if retry:
                    await asyncio.sleep(0.1)
                    self.queue_wake.set()
        except Exception as exc:  # a queue/journal failure cannot permit further native writes
            self.reason = f"queued message dispatch failed: {exc}"
            self.stop_child()

    async def lines(self):
        """Complete native lines, bounded. An oversized line is DROPPED, not fatal.

        One huge tool output must not end the generation (and a frame that can never be read
        must not make a session unresumable). The drop is recorded as an observation; a dropped
        permission request simply stays unanswered and the turn can still be interrupted.
        """
        assert self.proc is not None and self.proc.stdout is not None
        stream, buffer, dropping = self.proc.stdout, bytearray(), False
        limit = native_protocol.MAX_FRAME_BYTES
        while chunk := await stream.read(65536):
            buffer += chunk
            while True:
                index = buffer.find(b"\n")
                if index < 0:
                    if len(buffer) > limit:
                        if not dropping:
                            self.dropped_frame()
                        dropping = True
                        buffer.clear()
                    break
                line = bytes(buffer[: index + 1])
                del buffer[: index + 1]
                if dropping or len(line) > limit:
                    if not dropping:
                        self.dropped_frame()
                    dropping = False
                    yield b""  # the dropped frame has ended: the codec may be waiting for it
                    continue
                yield line
        if buffer and not dropping:
            # An unterminated tail is a frame the child never finished writing: never complete
            # it into a valid record (Hermes on #1278).
            self.record(
                [
                    native_protocol.NativeEvent(
                        "error", {"message": "the native agent exited mid-frame; it was discarded"}
                    )
                ]
            )

    def dropped_frame(self) -> None:
        self.record(
            [
                native_protocol.NativeEvent(
                    "error", {"message": "a native frame exceeded its size bound and was omitted"}
                )
            ]
        )

    async def reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        ended = False  # the agent's output ended (EOF), not a protocol violation
        try:
            async for line in self.lines():
                if not line:
                    # A frame was dropped (#1644): an input waiting for its result ends now.
                    lost = getattr(self.codec, "frame_lost", None)
                    await self.handle_events(lost() if lost is not None else [])
                    continue
                try:
                    events = self.codec.feed(native_protocol.decode(line))
                except native_protocol.ProtocolError as exc:
                    self.record(
                        [native_protocol.NativeEvent("error", {"message": f"protocol: {exc}"})]
                    )
                    self.reason = f"native protocol violation: {exc}"
                    break
                await self.handle_events(events)
            else:
                ended = True
        finally:
            if ended and not self.terminated:
                # Reap before stop (#1732): nothing new is admitted meanwhile (`closing` refuses
                # effects and the queue as an idle exit does), and the disconnect is journaled
                # after the counter's last read.
                self.closing = True
                await self.reap_window()
            with contextlib.suppress(native_protocol.ProtocolError):
                self.record([self.codec.eof()])
            for waiter in self.waiters.values():
                if not waiter.done():
                    waiter.set_exception(WorkerError("native connection closed"))
            self.stop_child()
            self.done.set()

    def stderr_text(self) -> str:
        text = self.stderr_tail.decode("utf-8", "replace")
        if self._server_password:
            text = text.replace(self._server_password, "[redacted]")
        return text

    async def stderr_reader(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + chunk)[-STDERR_TAIL:]

    async def request(self, frame: dict, timeout: float = INIT_TIMEOUT):
        waiter = asyncio.get_running_loop().create_future()
        self.waiters[frame["id"] if "id" in frame else frame["request_id"]] = waiter
        await self.write(frame)
        event = await asyncio.wait_for(waiter, timeout)
        if event.kind == "error":
            raise WorkerError(f"native {event.data.get('action')} failed: {event.data['message']}")
        return event

    async def initialize(self) -> None:
        await self.request(self.codec.initialize())
        if isinstance(self.codec, native_protocol.ClaudeCodec):
            return
        if isinstance(self.codec, native_protocol.OpencodeAcpCodec):
            await self.initialize_opencode()
            return
        if isinstance(self.codec, native_protocol.CopilotAcpCodec):
            await self.initialize_copilot()
            return
        await self.write(self.codec.initialized())
        cwd, model = self.config["cwd"], self.config.get("model")
        bypass = self.config.get("bypass") is True
        session = battlelab_session(self.config)
        shell_env = memory_environment(self.config)
        tools = tool_servers(self.config)
        mcp_config = agent_tools.codex_thread_config(tools) if tools else None
        if self.native_id is not None:
            await self.request(
                self.codec.resume(
                    self.native_id,
                    cwd,
                    model,
                    bypass=bypass,
                    session=session,
                    shell_env=shell_env,
                    mcp_config=mcp_config,
                )
            )
            return
        request = self.codec.create(
            cwd, model, bypass=bypass, session=session, shell_env=shell_env, mcp_config=mcp_config
        )
        event = await self.request(request)
        native_id = event.data["native_id"]
        await asyncio.to_thread(self.bind_created, request["id"], event)
        self.native_id = native_id

    async def initialize_opencode(self) -> None:
        """new → bind (or load), then pin our own agent, then the requested model and level —
        before any turn."""
        codec = self.codec
        assert isinstance(codec, native_protocol.OpencodeAcpCodec)
        cwd, model = self.config["cwd"], self.config.get("model")
        tools = tool_servers(self.config)
        servers = agent_tools.acp_servers(tools) if tools else None
        if self.native_id is not None:
            # `session/load` replays history; the codec attributes none of it to a turn and no
            # replayed tool call can become a live approval.
            await self.request(codec.load(self.native_id, cwd, servers))
        else:
            request = codec.create(cwd, servers)
            event = await self.request(request)
            native_id = event.data["native_id"]
            await asyncio.to_thread(self.bind_created, request["id"], event)
            self.native_id = native_id
        await self.request(codec.pin_mode())
        if model is not None:
            await self.request(codec.set_model(model))
            # A level belongs to a model (#1407): set after it, on every generation (a load too),
            # and confirmed from the echo like the model. A model without levels never gets one:
            # the create refused that combination against the same list.
            effort = self.config.get("effort")
            if effort is not None:
                await self.request(codec.set_effort(effort))

    async def initialize_copilot(self) -> None:
        """new → bind (or load), then the pinned agent mode and `allow_all` — before any turn
        (#1565). No `mcpServers` anywhere: copilot's ACP rejects stdio servers, so the memory
        tool rode in on this child's argv instead. A model/effort selection is REFUSED, never
        silently dropped: 1.0.68 enumerates no `model` option and echoes no set, so a request
        this worker cannot confirm would run the vendor default while the record says
        otherwise; `CopilotAcpCodec.set_model/set_effort` pin the seam for a future CLI whose
        listing and echo can be trusted."""
        codec = self.codec
        assert isinstance(codec, native_protocol.CopilotAcpCodec)
        if self.config.get("model") is not None or self.config.get("effort") is not None:
            raise WorkerError(
                "copilot cannot honour a model or effort selection — its ACP sets are never "
                "echoed, so the vendor default would run under a chosen model's name"
            )
        cwd = self.config["cwd"]
        if self.native_id is not None:
            # `session/load` replays history before its reply; the codec attributes none of it
            # to a turn and no replayed tool call can become a live approval.
            await self.request(codec.load(self.native_id, cwd))
        else:
            request = codec.create(cwd)
            event = await self.request(request)
            native_id = event.data["native_id"]
            await asyncio.to_thread(self.bind_created, request["id"], event)
            self.native_id = native_id
        await self.request(codec.pin_mode())
        await self.request(codec.set_allow_all(self.config.get("bypass") is True))

    def bind_created(self, request_id: str, event: native_protocol.NativeEvent) -> None:
        """Bind only this connection's fresh-create reply, before the writer lock and any turn.

        Neither a supplied native id nor a resume/load reply is fresh-creation evidence. The
        codec correlates responses before releasing the waiter; verify that evidence again at
        the only call site that selects the ownership guard's fresh-create exception.
        """
        from . import native_ownership
        from .plugins import storage

        action = {
            "codex-app-server": "thread/start",
            OPENCODE: "session/new",
            COPILOT: "session/new",
        }.get(self.adapter)
        native_id = event.data.get("native_id")
        if (
            action is None
            or self.config["mode"] != "create"
            or self.config.get("native_id") is not None
            or self.native_id is not None
            or event.kind != "session"
            or event.data.get("action") != action
            or event.data.get("request_id") != request_id
            or not native_id
            or self.codec.native_id != native_id
        ):
            raise WorkerError("native binding requires this connection's fresh create response")
        creation = self.config["creation"]
        with storage.locked("launch", wait=30):
            native_ownership.bind(
                self.session_key,
                operation_id=creation["operation_id"],
                owner_token=creation["owner_token"],
                native_id=native_id,
                fresh_create=True,
            )
        self.lock_source(native_id)
        with native_state.session_lock(self.session_id):
            record = native_state.read_session(self.session_id) or {}
            if record.get("native_id") not in (None, native_id):
                raise WorkerError("the session was bound to another native history")
            record["native_id"] = native_id
            native_state.write_session(self.session_id, record)

    def stop_child(self) -> None:
        self.terminated = True  # a stop before the child exists prevents its spawn
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()

    def kill_child(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()

    def release_lease(self) -> None:
        """Last act: the code no longer runs, so its release may be pruned again."""
        if self.proc is not None and self.proc.returncode is None:
            return  # a child we could not reap keeps the lease (the sweep needs evidence)
        with contextlib.suppress(Exception):
            release = native_state.read_lifecycle(self.worker_id).get("release")
            if release:
                native_state.lease(Path(release), self.worker_id, remove=True)

    # --- private IPC -----------------------------------------------------------------------

    def gate_open(self) -> bool:
        try:
            return native_state.admitted(self.session_id, self.worker_id)
        except native_state.StateError:
            return False

    def receipt(self, request: dict, result: dict) -> dict:
        return {
            **self.binding.envelope("response"),
            "request_id": request["request_id"],
            "action": request["action"],
            "result": result,
        }

    def journal_failed(self, exc: native_journal.JournalError) -> None:
        """Name WHY the record could not be written (#1733), best effort: on a full disk this
        write can fail too, and the turn then reads uncertain with the generic reason."""
        if exc.cause in native_segments.CAUSES:
            self.report(journal_failure=exc.cause)

    def journal_error(
        self, request: dict, exc: native_journal.JournalError, *, before_effect: bool = True
    ) -> dict:
        """A journal failure as a response. ``unwritable`` appended nothing, so for a claim made
        BEFORE any effect it is a known refusal (#1733) the pane shows as such — the draft kept, a
        NEW send offered. Where an effect may already have reached the agent (Send now writes the
        queued message before journaling its control), it stays unavailable: uncertain."""
        if exc.code == "unwritable" and not before_effect:
            self.journal_failed(exc)
            return self.error(request, "unavailable", exc.detail)
        if exc.code == "unwritable":
            self.journal_failed(exc)
            return self.error(
                request,
                "conflict",
                f"{exc.detail}; nothing was sent",
                native_ipc.JOURNAL_UNWRITABLE,
            )
        return self.error(
            request,
            exc.code if exc.code in {"conflict", "invalid", "busy"} else "unavailable",
            exc.detail,
        )

    def error(self, request: dict, code: str, message: str, reason: str | None = None) -> dict:
        return {
            **self.binding.envelope("response"),
            "request_id": request["request_id"],
            "action": request["action"],
            "error": {
                "code": code,
                "message": message[:2000],
                **({"reason": reason} if reason is not None else {}),
            },
        }

    def _pending(self, params: dict):
        approval = self.codec._approvals.get(params["request_id"])
        if (
            approval is None
            or params["approval_worker_id"] != self.worker_id
            or params["approval_connection_id"] != self.binding.connection_id
            or approval["operation_id"] != params["turn_id"]
            or approval.get("digest", native_protocol._digest(approval["frame"]))
            != params["payload_digest"]
            or (approval.get("item_id") or approval["params"].get("itemId")) != params["item_id"]
        ):
            return None
        return approval

    async def effect(self, request: dict) -> dict:
        action, params = request["action"], request["params"]
        async with self.effects:
            self.last_activity = time.monotonic()
            if (self.closing or self.terminated) and action != "stop":
                return self.error(request, "unavailable", SHUTTING_DOWN)
            if action != "stop" and not self.gate_open():
                return self.error(request, "stale", "this worker generation is closed")
            if action != "stop" and (not self.ready.is_set() or self.done.is_set()):
                return self.error(request, "unavailable", "the native connection is not ready")
            if action == "stop" and params["target_worker_id"] != self.worker_id:
                return self.error(request, "conflict", "stop targets another worker generation")
            if action == "send_now":
                return await self.send_now(request)
            if action == "withdraw":
                return await self.withdraw(request)
            # Validate what can be validated BEFORE a durable claim; a refused request that
            # never claimed can be retried with the same operation id.
            if action == "decide":
                pending = self._pending(params)
                if pending is None:
                    return self.error(request, "stale", "that approval is no longer pending")
                if params["decision"] == "always":
                    # A standing grant (#1339) is re-derived HERE from the request this
                    # connection holds — only what the client itself proposed — independently of
                    # the server that offered it.
                    try:
                        self.codec._always(
                            params["request_id"], params.get("grant"), self.config["cwd"]
                        )
                    except native_protocol.ProtocolError as exc:
                        return self.error(request, "unsupported", str(exc))
                if params["decision"] in {"approve", "always"} and (
                    pending.get("kind") == native_questions.ASK_TOOL
                ):
                    return self.error(
                        request,
                        "unsupported",
                        "an agent question is answered, skipped or stopped, not approved",
                    )
                if params["decision"] == "answer":
                    # An agent question's answer (#1463), checked HERE against the request this
                    # connection holds, independently of the server that relayed it.
                    try:
                        self.codec.check_answer(params["request_id"], params.get("answers"))
                    except native_protocol.ProtocolError as exc:
                        return self.error(request, "unsupported", str(exc))
                if (
                    params["decision"] in {"approve", "always"}
                    and pending.get("complete") is not True
                ):
                    # Defence in depth for the web gate: only a request presented in full
                    # (its patch / permission context included) can be approved.
                    return self.error(
                        request,
                        "unsupported",
                        "this request could not be presented completely; it can only be declined",
                    )
            if (
                action == "interrupt"
                and self.codec.operation_id != params["turn_id"]
                # A turn the agent runs for itself (#1438), named by the target the operator saw.
                # Checked again once claimed: `interrupt_background` refuses a target that moved.
                and not self._background_target(params["turn_id"])
            ):
                replayed = await self._replayed(request)
                if replayed is not None:
                    return replayed
                if self.codec.unowned_turn and self.codec.send_now_background is None:
                    return self.error(
                        request, "unsupported", "this agent can't interrupt a turn of its own"
                    )
                return self.error(request, "stale", "that turn is not active")
            queued = action == "submit" and (bool(self.queued) or self.dispatch_busy())
            if action == "submit" and not queued:
                # A direct turn's claim is its start: the OOM counter is read before it (#1732).
                if self.oom_read(self._turn_key, ("operation_id", params["operation_id"])):
                    self.flush()
            try:
                fresh, receipt = await asyncio.to_thread(
                    native_journal.claim, self.journal_root, request, queued=queued
                )
            except native_journal.JournalError as exc:
                return self.journal_error(request, exc)
            if not fresh:
                return self.receipt(request, receipt)
            if queued:
                self.queued.append(params)
                self.queue_wake.set()
                return self.receipt(request, receipt)
            return self.receipt(request, await self.handoff(action, params, receipt=receipt))

    async def send_now(self, request: dict) -> dict:
        """Promote one queued input under the SAME effect/admission fences as FIFO dispatch.

        The control claim binds selection, not a second copy of the user message. Codex steers
        the original submit; serial protocols cancel and let the existing dispatcher deliver it
        after terminal evidence. Neither a receipt nor a cancellation ack means completion.
        """
        from .plugins import api_source, storage

        params = request["params"]
        try:
            journal = await asyncio.to_thread(
                native_journal.read, self.journal_root, self.session_id
            )
            previous = journal.operations.get(params["operation_id"])
            if previous is not None:
                if previous.request != native_ipc.immutable_request(request):
                    return self.error(
                        request, "conflict", "native operation UUID binds another request"
                    )
                return self.receipt(request, previous.receipt())
            queued = next(
                (p for p in self.queued if p["operation_id"] == params["queued_turn_id"]), None
            )
            # The target is the operator turn running now, or a turn the agent runs for itself
            # (#1427): never the other, and never one that has since ended.
            background = self._background_target(params["turn_id"])
            if queued is not None and params["turn_id"] == getattr(self.codec, "_ahead", None):
                # Sent now already (#1526): it runs as soon as the turn it cut has ended.
                return self.error(
                    request, "busy", "a message is already waiting for this response to stop"
                )
            # Stop all and send (#1644): a turn or queued head that moved is a changed binding
            # like the tasks are, so the operator is asked again for what runs now.
            changed = "inventory_changed" if params["mode"] == "stop_all" else None
            if queued is None or not (background or self.codec.operation_id == params["turn_id"]):
                return self.error(
                    request, "stale", "the message is no longer queued behind that turn", changed
                )
            # FIFO is the contract (#1442): only the head of the queue goes now, so nothing ever
            # jumps the messages before it. An old tab offering a later one is refused here.
            if not self._queue_head(queued):
                return self.error(
                    request, "stale", "only the first queued message can be sent now", changed
                )
            if not background and self.codec.native_turn_id is None:
                return self.error(
                    request, "busy", "the agent has not acknowledged the active turn yet"
                )
            expected = self.codec.send_now_background if background else self.codec.send_now_mode
            stop_all = params["mode"] == "stop_all"
            if expected != "interrupt" if stop_all else params["mode"] != expected:
                return self.error(request, "unsupported", "this worker uses another delivery mode")
            if stop_all and not self._confirmed(params):
                return self.error(request, "stale", INVENTORY_CHANGED, "inventory_changed")
            if self._send_now_would_stop_background() and not (
                stop_all and self._confirmed(params)  # rechecked after every await before it
            ):
                return self.error(
                    request, "unsupported", SEND_NOW_WOULD_STOP, "would_stop_background"
                )
            if self.priority_turn == params["turn_id"]:
                return self.error(
                    request, "busy", "a message is already waiting for this response to stop"
                )
            with self.queued_admission():
                fresh, receipt = await asyncio.to_thread(
                    native_journal.claim, self.journal_root, request
                )
                if not fresh:
                    return self.receipt(request, receipt)
                # Native output is read independently while the claim fsyncs. Never cancel a
                # newer response, or consume a queued message when its observed turn ended.
                if (
                    (
                        not self._background_target(params["turn_id"])
                        if background
                        else self.codec.operation_id != params["turn_id"]
                    )
                    or not self._queue_head(queued)
                    # Stop all and send (#1644): what was confirmed is checked again HERE, after
                    # the claim; a task that started or ended meanwhile never authorizes it.
                    or (stop_all and not self._confirmed(params))
                ):
                    receipt = await asyncio.to_thread(
                        native_journal.record_handoff,
                        self.journal_root,
                        self.binding,
                        params["operation_id"],
                        "not_sent",
                    )
                elif params["mode"] == "steer":
                    # Asked BEFORE the message leaves the queue: a failed read leaves it queued.
                    current = await self._answer_current(queued)
                    self.queued.remove(queued)
                    self.sent_now.discard(queued["operation_id"])
                    if current:
                        delivered = await self.handoff("steer", queued, queued=True)
                        handoff = delivered["handoff"]
                    else:
                        # Steering leaves the queue too: an answer whose question was replaced
                        # meanwhile is never read as the reply to the newer one (#1463).
                        log_queue_refusal(
                            self.session_key,
                            queued["operation_id"],
                            "question_replaced",
                            AdmissionRefused("question_replaced"),
                        )
                        await asyncio.to_thread(
                            native_journal.record_handoff,
                            self.journal_root,
                            self.binding,
                            queued["operation_id"],
                            "not_sent",
                            reason_code="question_replaced",
                        )
                        handoff = "not_sent"
                    receipt = await asyncio.to_thread(
                        native_journal.record_handoff,
                        self.journal_root,
                        self.binding,
                        params["operation_id"],
                        handoff,
                    )
                elif self._native_send_now():
                    # Also for Stop all and send: the CLI's own way ends no background task, so
                    # it is taken whenever it is there; the confirmation only permits the Stop.
                    receipt = await self._send_now_native(queued, params, receipt)
                else:
                    receipt = await self._send_now_by_interrupt(params, receipt, stop_all=stop_all)
                self.queue_wake.set()
                return self.receipt(request, receipt)
        except (native_state.LockBusy, storage.LockBusy):
            return self.error(request, "busy", "another launch is in progress; try Send now again")
        except native_journal.JournalError as exc:
            if exc.code not in {"conflict", "invalid", "busy"}:
                self.stop_child()  # no further write without durable evidence
            return self.journal_error(request, exc, before_effect=False)
        except (WorkerError, api_source.SourceError) as exc:
            return self.error(request, "unavailable", str(exc))

    def _native_send_now(self) -> bool:
        ready = getattr(self.codec, "send_now_ready", None)
        return ready is not None and ready()

    def _send_now_would_stop_background(self) -> bool:
        would_stop = getattr(self.codec, "send_now_would_stop_background", None)
        return would_stop is not None and would_stop()

    def _confirmed(self, params: dict) -> bool:
        """Is what the operator confirmed for Stop all and send what runs now (#1644)? The
        generation and the exact set of background tasks; the turn and the queued head are
        checked beside it. Labels are never compared: two tasks may share one."""
        return params.get("confirm_worker_id") == self.worker_id and params.get(
            "confirm_tasks"
        ) == native_ipc.tasks_digest(getattr(self.codec, "_background", ()))

    async def _send_now_by_interrupt(
        self, params: dict, receipt: dict, *, stop_all: bool = False
    ) -> dict:
        """Send now on a serial protocol without its own: cancel the turn, and let the existing
        dispatcher deliver the message after terminal evidence. Never while that cancellation,
        a Stop, would also end background agents (#1526): the message keeps its place, unless
        the operator confirmed exactly those tasks (Stop all and send, #1644)."""
        if self._send_now_would_stop_background() and not (
            stop_all and self._confirmed(params)  # rechecked after every await before it
        ):
            return await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "not_sent",
            )
        self.priority_turn = params["turn_id"]
        receipt = await self.handoff("interrupt", params, receipt=receipt)
        if receipt["handoff"] != "sent":
            self.priority_turn = None
        elif self.priority_turn == params["turn_id"] and not any(
            target[2] == params["operation_id"] for target in self.priority_requests.values()
        ):
            # Notification-only cancellation has no ack. RPC cancellation promotes
            # from its correlated success response; a refusal leaves FIFO intact.
            self.promote_queued(params["queued_turn_id"])
        return receipt

    async def _send_now_native(self, queued: dict, params: dict, receipt: dict) -> dict:
        """The CLI's own Send now (#1526): the message is written to the CLI first, then a
        `send_now` interrupt names it. A plain interrupt is a Stop, which also ends every
        background agent; this cuts only the running turn. Whether the message is still current
        is asked BEFORE it leaves the queue: a failed read leaves it queued."""
        current = await self._answer_current(queued)
        if not self._native_send_now():
            # The connection changed while the journal was read (its fence was lost).
            return await self._send_now_by_interrupt(
                params, receipt, stop_all=params["mode"] == "stop_all"
            )
        self.queued.remove(queued)
        self.sent_now.discard(queued["operation_id"])
        if current:
            delivered = await self.handoff("ahead", queued, queued=True)
        else:
            # Like a steer: an answer whose question was replaced meanwhile is never sent (#1463).
            log_queue_refusal(
                self.session_key,
                queued["operation_id"],
                "question_replaced",
                AdmissionRefused("question_replaced"),
            )
            delivered = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                queued["operation_id"],
                "not_sent",
                reason_code="question_replaced",
            )
        if delivered["handoff"] != "sent":
            return await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "not_sent",
            )
        # The message is at the CLI and runs next whatever this request does: a refusal or
        # `nothing_waiting` only means it waits for the turn to end.
        self.priority_turn = params["turn_id"]
        return await self.handoff("send_now", params)

    async def _replayed(self, request: dict) -> dict | None:
        """The receipt of a request this journal already holds, so a retried interrupt answers
        as it did the first time even after its turn ended (#1438). None: never claimed."""
        params = request["params"]
        try:
            journal = await asyncio.to_thread(
                native_journal.read, self.journal_root, self.session_id
            )
        except native_journal.JournalError:
            return None
        previous = journal.operations.get(params["operation_id"])
        if previous is None:
            return None
        if previous.request != native_ipc.immutable_request(request):
            return self.error(request, "conflict", "native operation UUID binds another request")
        return self.receipt(request, previous.receipt())

    async def _answer_current(self, params: dict) -> bool:
        """May this queued message leave the queue now (#1463)? An async question's answer goes
        only to the question it was admitted for; a newer one asked while it waited is not its.
        Asked on EVERY way out of the queue (FIFO dispatch and Send now's steer), after a flush,
        so a question the agent just asked is in the journal read."""
        if "reply_to" not in params:
            return True
        self.flush()
        journal = await asyncio.to_thread(native_journal.read, self.journal_root, self.session_id)
        return native_journal.answer_current(journal, params["operation_id"])

    def _queue_head(self, queued: dict) -> bool:
        """Is ``queued`` the first message waiting (#1442)? Checked before the claim and again
        once it is fsynced: the head that changed meanwhile settles the control ``not_sent``."""
        return bool(self.queued) and self.queued[0] is queued

    async def withdraw(self, request: dict) -> dict:
        """Remove one still-queued message before it is sent (#1447).

        Runs under the same effect lock as FIFO dispatch and Send now, so a message is either
        removed here or handed off there, never both. The queued submit settles ``not_sent``;
        the control records ``sent`` once the entry is gone.
        """
        params = request["params"]
        try:
            journal = await asyncio.to_thread(
                native_journal.read, self.journal_root, self.session_id
            )
            previous = journal.operations.get(params["operation_id"])
            if previous is not None:
                if previous.request != native_ipc.immutable_request(request):
                    return self.error(
                        request, "conflict", "native operation UUID binds another request"
                    )
                return self.receipt(request, previous.receipt())
            queued = next(
                (p for p in self.queued if p["operation_id"] == params["queued_turn_id"]), None
            )
            if queued is None:
                return self.error(
                    request, "stale", "the message was already sent or is no longer queued"
                )
            fresh, receipt = await asyncio.to_thread(
                native_journal.claim, self.journal_root, request
            )
            if not fresh:
                return self.receipt(request, receipt)
            self.queued.remove(queued)
            self.sent_now.discard(params["queued_turn_id"])
            await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["queued_turn_id"],
                "not_sent",
            )
            receipt = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "sent",
            )
            self.queue_wake.set()
            return self.receipt(request, receipt)
        except native_journal.JournalError as exc:
            if exc.code not in {"conflict", "invalid", "busy"}:
                self.stop_child()  # no further write without durable evidence
            return self.journal_error(request, exc, before_effect=False)

    def _background_target(self, turn_id: str) -> bool:
        """Is ``turn_id`` the turn the agent is running for itself right now (#1427)?"""
        codec = self.codec
        return (
            codec.send_now_background is not None
            and codec.operation_id is None
            and codec.background_turn is not None
            and codec.background_turn == turn_id
        )

    async def handoff(
        self, action: str, params: dict, *, receipt=None, queued=False, now=False
    ) -> dict:
        refusal: Exception | None = None
        try:
            frame = self.frame(action, params, now=now)
        except native_protocol.ProtocolError as exc:
            frame, refusal = None, exc
        if frame is not None and self.turn_transition():
            self.flush()  # the counter's read lands before the turn's handoff (#1732)
        if frame is None:
            reason = {}
            if queued or action == "submit":
                # An operator message refused at its own handoff (an attachment that changed, a
                # codec refusal), queued or direct, was never written: a proved non-send with a
                # reason (#1538).
                reason = {"reason_code": "unexpected"}
                log_queue_refusal(
                    self.session_key,
                    params["operation_id"],
                    "unexpected",
                    refusal or native_protocol.ProtocolError(),
                )
            receipt = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "not_sent",
                **reason,
            )
            if action == "stop":
                receipt = {**receipt, "containment": "unknown"}
                self.stop_child()
            return receipt
        if queued:
            # The durable boundary: after this fsync, a crash can NEVER imply safe replay.
            receipt = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "uncertain",
            )
        try:
            await self.write(frame)
        except (OSError, ConnectionError, AssertionError, TimeoutError):
            self.after_write = []
            self.stop_child()
            return receipt  # remains uncertain: a half-written frame is never resent
        receipt = await asyncio.to_thread(
            native_journal.record_handoff,
            self.journal_root,
            self.binding,
            params["operation_id"],
            "sent",
        )
        after, self.after_write = self.after_write, []
        if after:
            try:
                await self.handle_events(after)
            except (OSError, ConnectionError, AssertionError, TimeoutError):
                self.stop_child()
        return receipt

    def frame(self, action: str, params: dict, *, now: bool = False) -> dict | None:
        if action in {"submit", "steer", "ahead"}:
            images = ()
            text = params["text"]
            if params.get("attachments"):
                try:
                    # Reverify before constructing either images or file references (#1396).
                    text, images = native_attachments.prepare(text, params["attachments"])
                except native_attachments.AttachmentError as exc:
                    raise native_protocol.ProtocolError(str(exc)) from None
            method = (
                self.codec.steer
                if action == "steer"
                else self.codec.submit_ahead
                if action == "ahead"
                else self.codec.submit_now
                if now
                else self.codec.submit
            )
            return method(text, params["operation_id"], images)
        if action == "decide":
            return self.codec.decide(
                params["request_id"],
                params["decision"],
                grant=params.get("grant"),
                cwd=self.config["cwd"],
                answers=params.get("answers"),
            )
        if action in {"interrupt", "send_now"}:
            # Any target but the operator turn running NOW goes to `interrupt_background`, which
            # refuses one that moved since the claim (#1438): a turn that ended between the
            # check and this write is reported, never swapped for whatever runs now. Send now's
            # own request (#1526) names the message it wrote, not a turn.
            frame = (
                self.codec.send_now_interrupt(params["queued_turn_id"])
                if action == "send_now"
                else self.codec.interrupt()
                if self.codec.operation_id == params["turn_id"]
                else self.codec.interrupt_background(params["turn_id"])
            )
            rid = frame.get("id", frame.get("request_id"))
            if rid is not None and params.get("queued_turn_id"):
                self.priority_requests[rid] = (
                    params["queued_turn_id"],
                    self.codec.native_turn_id,
                    params["operation_id"],
                    params["turn_id"],
                )
            if isinstance(
                self.codec, native_protocol.OpencodeAcpCodec | native_protocol.CopilotAcpCodec
            ):
                # ACP: a cancelled turn's pending permission requests are answered `cancelled`.
                # Taken in the same step as the cancel, before the turn's end can clear them.
                self.after_write = self.codec.cancel_pending()
            return frame
        return None  # stop: nothing is written to the agent; the child is terminated

    async def read_only(self, request: dict) -> dict:
        action, params = request["action"], request["params"]
        if action == "probe":
            if params["target_worker_id"] != self.worker_id:
                return self.error(request, "conflict", "probe targets another worker generation")
            return self.receipt(request, {"containment": "live"})
        try:
            journal = await asyncio.to_thread(
                native_journal.read, self.journal_root, self.session_id
            )
            if action == "events":
                return self.receipt(request, journal.page(params["after"], params["limit"]))
            page = journal.page(max(0, journal.revision - native_ipc.MAX_EVENTS), 100)
        except native_journal.JournalError as exc:
            return self.error(request, "unavailable", exc.detail)
        busy = self.codec.operation_id is not None
        return self.receipt(
            request,
            {
                **page,
                "state": "running" if busy else "idle",
                "native_id": self.native_id,
                "model_requested": self.config.get("model"),
                "model_effective": None,
            },
        )

    async def serve_client(self, reader, writer) -> None:
        try:
            if _peer_uid(writer.get_extra_info("socket")) != os.geteuid():
                return
            line = await asyncio.wait_for(reader.readline(), 10)
            try:
                native_ipc.decode_handshake(line, expected=self.binding, capability=self.capability)
            except native_ipc.IPCError:
                return
            writer.write(native_ipc.encode_welcome(self.binding))
            await writer.drain()
            while line := await reader.readline():
                try:
                    request = native_ipc.decode_request(line, expected=self.binding)
                except native_ipc.IPCError as exc:
                    writer.write(
                        json.dumps({"error": {"code": "invalid", "message": str(exc)}}).encode()
                        + b"\n"
                    )
                    await writer.drain()
                    return
                if request["action"] in native_ipc.EFFECT_ACTIONS:
                    response = await self.effect(request)
                else:
                    response = await self.read_only(request)
                writer.write(native_ipc.encode_response(response))
                await writer.drain()
                if request["action"] == "stop" and "result" in response:
                    self.stop_child()
        except (OSError, ConnectionError, asyncio.LimitOverrunError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def listen(self) -> None:
        path = Path(self.config["socket"])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(path.parent)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise WorkerError("worker socket directory is not private")
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self.serve_client, path=str(path), limit=native_ipc.MAX_FRAME_BYTES + 1
            )
        finally:
            os.umask(old_umask)

    # --- main ------------------------------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop_child)
        await asyncio.to_thread(self.admit)
        self.oom_baseline()
        await self.spawn()
        tasks = [
            asyncio.create_task(self.reader()),
            asyncio.create_task(self.stderr_reader()),
            asyncio.create_task(self.flusher()),
        ]
        try:
            await asyncio.wait_for(self.initialize(), INIT_TIMEOUT)
        except (TimeoutError, WorkerError, Exception) as exc:  # noqa: BLE001
            self.reason = self.reason or f"native initialization failed: {exc}"
            self.stop_child()
        else:
            self.flush()
            await self.listen()
            self.ready.set()
            self.report(
                phase="ready",
                native_id=self.native_id,
                ready_at=time.time(),
                send_now=self.codec.send_now_mode,
                # Send now against a turn the agent runs for itself (#1427); absent on workers
                # from before it, which the server then never offers it for.
                send_now_background=self.codec.send_now_background,
                # Remove a queued message (#1447); absent on older workers, which the server
                # then never offers it for.
                withdraw=True,
                # Interrupt a turn the agent runs for itself directly, not only via Send now
                # (#1438); absent on older workers, which the pane then offers no button for.
                interrupt_own=self.codec.send_now_background == "interrupt",
                files=True,
                # Reads and writes a `not_sent` handoff's `reason_code` (#1538): the web process
                # writes `worker_ended` for an earlier generation only while the live worker
                # says so, since an older reader refuses the field.
                handoff_reasons=True,
                # Reads a segmented journal and the `journal_unwritable` reason code (#1733).
                journal_segments=True,
            )
            tasks.append(asyncio.create_task(self.idle_watch()))
            tasks.append(asyncio.create_task(self.memory_watch()))
            tasks.append(asyncio.create_task(self.dispatch_queue()))
        await self.done.wait()
        if self.proc is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 10)
            if self.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        self.flush()
        if self.server is not None:
            self.server.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.config["socket"])
        for task in tasks:
            task.cancel()
        self.report(
            phase="exited",
            exited_at=time.time(),
            reason=self.reason[:2000],
            native_returncode=None if self.proc is None else self.proc.returncode,
            native_stderr=self.stderr_text(),
        )
        return 0 if not self.reason else EXIT_FAILED


def _adopt_environment(config: dict, state_dir: Path) -> None:
    for key, value in config.get("server_env", {}).items():
        if key.startswith("AGENT_SESSIONS_") and isinstance(value, str):
            os.environ[key] = value
    os.environ["HOME"] = config["home"]
    if native_state.root() != state_dir:
        raise WorkerError("worker state directory does not match the shared plugin state")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_sessions.native_worker")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    try:
        config = _private_file(
            state_dir / "workers" / native_state._uuid(args.worker_id) / "config.json"
        )
        if config.get("worker_id") != args.worker_id:
            raise WorkerError("worker config belongs to another generation")
        _adopt_environment(config, state_dir)
        _log_to_journal()
        worker = Worker(config, state_dir)
    except (OSError, ValueError, KeyError, WorkerError, native_state.StateError) as exc:
        print(f"native worker refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    worker.report(phase="starting", pid=os.getpid(), started_at=time.time())
    try:
        return asyncio.run(worker.run())
    except WorkerError as exc:
        worker.report(phase="refused", reason=str(exc)[:2000], exited_at=time.time())
        return EXIT_REFUSED
    except BaseException as exc:  # noqa: BLE001 — any failure must leave a definite phase
        worker.kill_child()
        worker.report(
            phase="exited", reason=f"worker failed: {exc!r}"[:2000], exited_at=time.time()
        )
        return EXIT_FAILED
    finally:
        for lock in worker.locks:
            with contextlib.suppress(Exception):
                lock.release()
        worker.release_lease()


if __name__ == "__main__":
    sys.exit(main())
