"""The contained per-session native protocol worker (#1278).

Started only by ``native_runtime`` as ``python -I -m agent_sessions.native_worker`` inside a
fresh transient systemd user service (``native_containment.launch_argv``). It is a protocol
adapter: it owns the native CLI's stdin/stdout, the session writer locks and the durable
journal records for its generation. It schedules nothing, judges nothing and stores no
credentials of its own.

Lifecycle, in order (each step refuses rather than guesses):

1. Read the private config from ``<state>/workers/<id>/config.json``; adopt the server's
   path environment so locks, ownership and roster resolve to the SAME shared directories.
2. Under the session lifecycle lock: refuse unless this generation is the current, open one,
   then take the per-app-session writer lock for the worker's lifetime.
3. Bound histories take the source writer lock BEFORE the native child exists. A new Codex
   thread is bound (``native_ownership.bind`` under launch admission) from the thread/start
   response, and its writer lock taken, before any turn can be accepted.
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
    {"approval", "approval_cancelled", "turn_started", "turn_completed", "disconnected", "session"}
)


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
    for key in ("USER", "LOGNAME"):
        if isinstance(config.get(key.lower()), str):
            env[key] = config[key.lower()]
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
        self.codec: native_protocol.CodexCodec | native_protocol.ClaudeCodec
        if self.adapter == "codex-app-server":
            self.codec = native_protocol.CodexCodec()
        else:
            self.codec = native_protocol.ClaudeCodec(self.native_id)
        self.locks: list = []
        self.proc: asyncio.subprocess.Process | None = None
        self.server: asyncio.AbstractServer | None = None
        self.pending: list[dict] = []
        self.flush_now = asyncio.Event()
        self.effects = asyncio.Lock()
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.waiters: dict[str, asyncio.Future] = {}
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
        return native_protocol.ClaudeCodec.argv(
            binary,
            self.native_id,
            resume=self.config["mode"] == "resume",
            model=self.config.get("model"),
        )

    async def spawn(self) -> None:
        if self.terminated:
            raise WorkerError("stopped before the native agent started")
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config["cwd"],
            env=native_environment(self.config),
            limit=native_protocol.MAX_FRAME_BYTES + 1,
        )
        self.report(phase="native_started", native_pid=self.proc.pid)

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

    def busy(self) -> bool:
        codec = self.codec
        return (
            codec.operation_id is not None
            or bool(codec._approvals)
            or bool(getattr(codec, "_background", None))
            or getattr(codec, "_session_state", None) in {"running", "requires_action"}
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

    async def handle_events(self, events) -> None:
        self.last_activity = time.monotonic()
        for event in events:
            if event.kind == "send":
                await self.write(event.data["frame"])
            elif event.kind in {"initialized", "session", "error"}:
                request_id = event.data.get("request_id")
                waiter = self.waiters.pop(request_id, None) if request_id else None
                if waiter is not None and not waiter.done():
                    waiter.set_result(event)
        self.record(events)

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
        await self.write(self.codec.initialized())
        cwd, model = self.config["cwd"], self.config.get("model")
        if self.native_id is not None:
            await self.request(self.codec.resume(self.native_id, cwd, model))
            return
        event = await self.request(self.codec.create(cwd, model))
        native_id = event.data["native_id"]
        await asyncio.to_thread(self.bind, native_id)
        self.native_id = native_id

    def bind(self, native_id: str) -> None:
        """Permanent ownership commits before the source lock and before any turn."""
        from . import native_ownership
        from .plugins import storage

        creation = self.config["creation"]
        with storage.locked("launch", wait=30):
            native_ownership.bind(
                self.session_key,
                operation_id=creation["operation_id"],
                owner_token=creation["owner_token"],
                native_id=native_id,
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
            if self.closing and action != "stop":
                return self.error(request, "unavailable", SHUTTING_DOWN)
            if action != "stop" and not self.gate_open():
                return self.error(request, "stale", "this worker generation is closed")
            if action != "stop" and (not self.ready.is_set() or self.done.is_set()):
                return self.error(request, "unavailable", "the native connection is not ready")
            if action == "stop" and params["target_worker_id"] != self.worker_id:
                return self.error(request, "conflict", "stop targets another worker generation")
            # Validate what can be validated BEFORE a durable claim; a refused request that
            # never claimed can be retried with the same operation id.
            if action == "decide":
                pending = self._pending(params)
                if pending is None:
                    return self.error(request, "stale", "that approval is no longer pending")
                if params["decision"] == "approve" and pending.get("complete") is not True:
                    # Defence in depth for the web gate: only a request presented in full
                    # (its patch / permission context included) can be approved.
                    return self.error(
                        request,
                        "unsupported",
                        "this request could not be presented completely; it can only be declined",
                    )
            if action == "interrupt" and self.codec.operation_id != params["turn_id"]:
                return self.error(request, "stale", "that turn is not active")
            try:
                fresh, receipt = await asyncio.to_thread(
                    native_journal.claim, self.journal_root, request
                )
            except native_journal.JournalError as exc:
                return self.error(
                    request,
                    exc.code if exc.code in {"conflict", "invalid"} else "unavailable",
                    exc.detail,
                )
            if not fresh:
                return self.receipt(request, receipt)
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
                return self.receipt(request, receipt)
            try:
                await self.write(frame)
            except (OSError, ConnectionError, AssertionError, TimeoutError):
                self.stop_child()  # a half-written frame leaves the protocol unusable
                return self.receipt(request, receipt)  # stays "uncertain": never resent
            receipt = await asyncio.to_thread(
                native_journal.record_handoff,
                self.journal_root,
                self.binding,
                params["operation_id"],
                "sent",
            )
            return self.receipt(request, receipt)

    def frame(self, action: str, params: dict) -> dict | None:
        if action == "submit":
            images = ()
            if params.get("attachments"):
                try:
                    # Re-read and re-verified against the journaled digest (#1332 Phase 3).
                    images = native_images.inline(params["attachments"])
                except native_images.ImageError as exc:
                    raise native_protocol.ProtocolError(str(exc)) from None
            return self.codec.submit(params["text"], params["operation_id"], images)
        if action == "decide":
            return self.codec.decide(params["request_id"], params["decision"])
        if action == "interrupt":
            return self.codec.interrupt()
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
            self.report(phase="ready", native_id=self.native_id, ready_at=time.time())
            tasks.append(asyncio.create_task(self.idle_watch()))
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
            native_stderr=self.stderr_tail.decode("utf-8", "replace"),
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
