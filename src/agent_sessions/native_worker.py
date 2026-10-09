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
import os
import secrets
import signal
import socket
import stat
import struct
import sys
import time
from pathlib import Path

from . import native_images, native_ipc, native_journal, native_protocol, native_state

EXIT_REFUSED = 3
EXIT_FAILED = 4
INIT_TIMEOUT = 90.0
IDLE_TIMEOUT = 1800.0  # an idle worker exits; the next turn resumes in a new generation
SHUTTING_DOWN = "the native worker is shutting down; retry"
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
PROBE_TIMEOUT = 30.0
PROBE_LIMIT = 4 * 1024 * 1024


class WorkerError(RuntimeError):
    pass


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
    `OPENCODE_CONFIG_CONTENT` defines BattleLab's own ask-everything agent (``agent``, minted per
    generation, the same construction as the unattended launch's) that the session is pinned to.
    The operator's own config and console opencode are untouched, and the app's own
    `OPENCODE_CONFIG_CONTENT` is not passed on (the environment is sanitized).
    """
    env = native_environment(config)
    if config["adapter"] == OPENCODE:
        if not isinstance(server_password, str) or len(server_password) < 32:
            raise WorkerError("the opencode listener password is missing")
        from .engines import opencode

        env["OPENCODE_SERVER_PASSWORD"] = server_password
        try:
            env["OPENCODE_CONFIG_CONTENT"] = opencode.api_config_content(agent, mcp)
        except ValueError as exc:
            raise WorkerError(str(exc)) from None
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
        )
        self.agent: str | None = None
        if self.adapter == "codex-app-server":
            self.codec = native_protocol.CodexCodec()
        elif self.adapter == OPENCODE:
            from .engines import opencode

            self.agent = opencode.mint_api_agent()  # this generation's own ask-everything agent
            self.codec = native_protocol.OpencodeAcpCodec(self.native_id, agent=self.agent)
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
        return native_protocol.ClaudeCodec.argv(
            binary,
            self.native_id,
            resume=self.config["mode"] == "resume",
            model=self.config.get("model"),
            bypass=self.config.get("bypass") is True,
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
                *self.argv(),
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
                self.stop_child()
                return

    async def flusher(self) -> None:
        while not self.done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.flush_now.wait(), FLUSH_INTERVAL)
            self.flush_now.clear()
            self.flush()

    def native_busy(self) -> bool:
        codec = self.codec
        return (
            codec.operation_id is not None
            or bool(self.priority_requests)
            or bool(getattr(codec, "_steers", None))
            or bool(codec._approvals)
            or bool(getattr(codec, "_background", None))
            or getattr(codec, "_session_state", None) in {"running", "requires_action"}
        )

    def busy(self) -> bool:
        return bool(self.queued) or self.native_busy()

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

    async def handle_events(self, events) -> None:
        self.last_activity = time.monotonic()
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

    @contextlib.contextmanager
    def queued_admission(self):
        """Recheck the current generation and source at delayed stdin handoff (#1378)."""
        from . import native_ownership
        from .engines import registry
        from .plugins import api_source, storage

        with native_state.session_lock(self.session_id, wait=0), storage.locked("launch", wait=0):
            if not self.gate_open() or self.terminated or self.closing:
                raise WorkerError("this worker generation is closed")
            with registry.snapshot_scope(fresh=True, require_current=True) as roster:
                prov = roster.by_id.get(self.session_key.partition(":")[0])
                if prov is None:
                    raise WorkerError("the queued message's agent was removed")
                binding = api_source.resolve(prov, roster=roster)
                if (
                    prov.manifest.api.kind != self.adapter
                    or binding.source.engine_id != self.config["source_engine"]
                    or binding.source.entrypoint_path() != self.config["binary"]
                ):
                    raise WorkerError("the queued message's agent changed")
                owner = native_ownership.lookup(self.session_key)
                if owner is None or owner.source != native_ownership.source_identity(
                    binding.source
                ):
                    raise WorkerError("the queued message's source store changed")
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
                    while self.queued and not self.native_busy():
                        if self.terminated or self.closing or self.done.is_set():
                            return
                        # Completion must be durable before the following turn can start.
                        self.flush()
                        if self.terminated:
                            return
                        params = self.queued[0]
                        admitted = False
                        try:
                            with self.queued_admission():
                                admitted = True
                                self.queued.pop(0)
                                await self.handoff("submit", params, queued=True)
                        except (native_state.LockBusy, storage.LockBusy):
                            if admitted:
                                raise
                            # Another session's launch is not a refusal. Keep the FIFO head
                            # queued, then retry outside all locks so Stop stays responsive.
                            retry = True
                            break
                        except Exception:
                            # Admission failure proves non-send only BEFORE handoff.
                            if admitted:
                                raise
                            self.queued.pop(0)
                            await asyncio.to_thread(
                                native_journal.record_handoff,
                                self.journal_root,
                                self.binding,
                                params["operation_id"],
                                "not_sent",
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
        try:
            async for line in self.lines():
                try:
                    events = self.codec.feed(native_protocol.decode(line))
                except native_protocol.ProtocolError as exc:
                    self.record(
                        [native_protocol.NativeEvent("error", {"message": f"protocol: {exc}"})]
                    )
                    self.reason = f"native protocol violation: {exc}"
                    break
                await self.handle_events(events)
        finally:
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
        await self.write(self.codec.initialized())
        cwd, model = self.config["cwd"], self.config.get("model")
        bypass = self.config.get("bypass") is True
        if self.native_id is not None:
            await self.request(self.codec.resume(self.native_id, cwd, model, bypass=bypass))
            return
        request = self.codec.create(cwd, model, bypass=bypass)
        event = await self.request(request)
        native_id = event.data["native_id"]
        await asyncio.to_thread(self.bind_created, request["id"], event)
        self.native_id = native_id

    async def initialize_opencode(self) -> None:
        """new → bind (or load), then pin our own agent, then the requested model — before any
        turn."""
        codec = self.codec
        assert isinstance(codec, native_protocol.OpencodeAcpCodec)
        cwd, model = self.config["cwd"], self.config.get("model")
        if self.native_id is not None:
            # `session/load` replays history; the codec attributes none of it to a turn and no
            # replayed tool call can become a live approval.
            await self.request(codec.load(self.native_id, cwd))
        else:
            request = codec.create(cwd)
            event = await self.request(request)
            native_id = event.data["native_id"]
            await asyncio.to_thread(self.bind_created, request["id"], event)
            self.native_id = native_id
        await self.request(codec.pin_mode())
        if model is not None:
            await self.request(codec.set_model(model))

    def bind_created(self, request_id: str, event: native_protocol.NativeEvent) -> None:
        """Bind only this connection's fresh-create reply, before the writer lock and any turn.

        Neither a supplied native id nor a resume/load reply is fresh-creation evidence. The
        codec correlates responses before releasing the waiter; verify that evidence again at
        the only call site that selects the ownership guard's fresh-create exception.
        """
        from . import native_ownership
        from .plugins import storage

        action = {"codex-app-server": "thread/start", OPENCODE: "session/new"}.get(self.adapter)
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

    def error(self, request: dict, code: str, message: str) -> dict:
        return {
            **self.binding.envelope("response"),
            "request_id": request["request_id"],
            "action": request["action"],
            "error": {"code": code, "message": message[:2000]},
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
            # Validate what can be validated BEFORE a durable claim; a refused request that
            # never claimed can be retried with the same operation id.
            if action == "decide":
                pending = self._pending(params)
                if pending is None:
                    return self.error(request, "stale", "that approval is no longer pending")
                if params["decision"] == "always" and isinstance(
                    self.codec, native_protocol.OpencodeAcpCodec
                ):
                    # opencode maps no standing grant yet (#1339 Phase 4): refused, never guessed.
                    return self.error(request, "unsupported", "no standing grant for this client")
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
            if action == "interrupt" and self.codec.operation_id != params["turn_id"]:
                return self.error(request, "stale", "that turn is not active")
            queued = action == "submit" and self.busy()
            try:
                fresh, receipt = await asyncio.to_thread(
                    native_journal.claim, self.journal_root, request, queued=queued
                )
            except native_journal.JournalError as exc:
                return self.error(
                    request,
                    exc.code if exc.code in {"conflict", "invalid", "busy"} else "unavailable",
                    exc.detail,
                )
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
            if queued is None or self.codec.operation_id != params["turn_id"]:
                return self.error(
                    request, "stale", "the message is no longer queued behind that turn"
                )
            if self.codec.native_turn_id is None:
                return self.error(
                    request, "busy", "the agent has not acknowledged the active turn yet"
                )
            if params["mode"] != self.codec.send_now_mode:
                return self.error(request, "unsupported", "this worker uses another delivery mode")
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
                if self.codec.operation_id != params["turn_id"]:
                    receipt = await asyncio.to_thread(
                        native_journal.record_handoff,
                        self.journal_root,
                        self.binding,
                        params["operation_id"],
                        "not_sent",
                    )
                elif params["mode"] == "steer":
                    self.queued.remove(queued)
                    delivered = await self.handoff("steer", queued, queued=True)
                    receipt = await asyncio.to_thread(
                        native_journal.record_handoff,
                        self.journal_root,
                        self.binding,
                        params["operation_id"],
                        delivered["handoff"],
                    )
                else:
                    self.priority_turn = params["turn_id"]
                    receipt = await self.handoff("interrupt", params, receipt=receipt)
                    if receipt["handoff"] != "sent":
                        self.priority_turn = None
                    elif self.priority_turn == params["turn_id"] and not any(
                        target[2] == params["operation_id"]
                        for target in self.priority_requests.values()
                    ):
                        # Notification-only cancellation has no ack. RPC cancellation promotes
                        # from its correlated success response; a refusal leaves FIFO intact.
                        self.promote_queued(params["queued_turn_id"])
                self.queue_wake.set()
                return self.receipt(request, receipt)
        except (native_state.LockBusy, storage.LockBusy):
            return self.error(request, "busy", "another launch is in progress; try Send now again")
        except native_journal.JournalError as exc:
            if exc.code not in {"conflict", "invalid", "busy"}:
                self.stop_child()  # no further write without durable evidence
            return self.error(
                request,
                exc.code if exc.code in {"conflict", "invalid", "busy"} else "unavailable",
                exc.detail,
            )
        except (WorkerError, api_source.SourceError) as exc:
            return self.error(request, "unavailable", str(exc))

    async def handoff(self, action: str, params: dict, *, receipt=None, queued=False) -> dict:
        try:
            frame = self.frame(action, params)
        except native_protocol.ProtocolError:
            frame = None
        if frame is None:
            receipt = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "not_sent",
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

    def frame(self, action: str, params: dict) -> dict | None:
        if action in {"submit", "steer"}:
            images = ()
            if params.get("attachments"):
                try:
                    # Re-read and re-verified against the journaled digest (#1332 Phase 3).
                    images = native_images.inline(params["attachments"])
                except native_images.ImageError as exc:
                    raise native_protocol.ProtocolError(str(exc)) from None
            method = self.codec.steer if action == "steer" else self.codec.submit
            return method(params["text"], params["operation_id"], images)
        if action == "decide":
            if isinstance(self.codec, native_protocol.OpencodeAcpCodec):
                # opencode maps no standing grant yet (#1339 Phase 4): its codec takes the one-shot
                # decision only, and an `always` never reaches it (the server offers none).
                return self.codec.decide(params["request_id"], params["decision"])
            return self.codec.decide(
                params["request_id"],
                params["decision"],
                grant=params.get("grant"),
                cwd=self.config["cwd"],
            )
        if action == "interrupt":
            frame = self.codec.interrupt()
            rid = frame.get("id", frame.get("request_id"))
            if rid is not None and params.get("queued_turn_id"):
                self.priority_requests[rid] = (
                    params["queued_turn_id"],
                    self.codec.native_turn_id,
                    params["operation_id"],
                    params["turn_id"],
                )
            if isinstance(self.codec, native_protocol.OpencodeAcpCodec):
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
            )
            tasks.append(asyncio.create_task(self.idle_watch()))
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
