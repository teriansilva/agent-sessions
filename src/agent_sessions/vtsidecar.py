"""Client + process manager for the Path B faithful-scrollback VT sidecar (#271/#273).

Flag-gated by ``AGENT_SESSIONS_VT_SCROLLBACK`` (default OFF). When off, every entry point is a
no-op / returns ``None``, so prod and default behavior are byte-identical (the attach path keeps
using the transcript). When on, this spawns the Node sidecar (``vt-sidecar/dist/server.mjs``) and
talks to it over a unix socket with the newline-delimited JSON protocol
(``feed``/``snapshot``/``reset``/``end``/``health``/``version``).

**Fail-safe by construction:** a disabled flag, a missing ``node`` / bundle, an unreachable or slow
sidecar, or any protocol error yields ``None`` / a no-op — the caller (the attach path, #273
sub-step 3) then falls back to the transcript/clean-load path and the live stream is never blocked.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
from pathlib import Path

# Snapshot request budget (#273): exceed → treat as failure and fall back to the transcript.
_SNAPSHOT_TIMEOUT = 0.5
# Feed can be a large ring replay (rebuild-from-ring on attach), so it gets a generous budget.
_FEED_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 1.0
# Each response is one newline-terminated JSON line, and a rebuilt snapshot can be a few MB — far
# past asyncio's default 64 KB StreamReader limit. Past that limit `readline()` raises, the read
# loop tears down the connection, and every rebuild for a real-sized scrollback silently returns
# None → transcript fallback (the bug that made VT scroll-up only ever work for tiny test sessions).
# Size the read buffer above the sidecar's max snapshot payload (2 MB), with headroom for escaping.
_STREAM_LIMIT = 32 * 1024 * 1024


# Runtime override for the VT-scrollback flag, set by the experimental Settings toggle (#329).
# None ⇒ use the env default. In-memory so enabled() stays cheap on the hot attach path;
# persistence is the caller's job (prefs.set_vt_scrollback). Seeded from the persisted pref at
# startup (main.py), then flipped live by POST /api/prefs.
_runtime_override: bool | None = None


def enabled() -> bool:
    if _runtime_override is not None:
        return _runtime_override
    return (os.environ.get("AGENT_SESSIONS_VT_SCROLLBACK", "0") or "0") != "0"


def set_enabled(value: bool) -> None:
    """Override the VT-scrollback flag for this process (the experimental UI toggle, #329).
    Overrides the env default; the caller persists the choice via ``prefs.set_vt_scrollback``."""
    global _runtime_override
    _runtime_override = bool(value)


def _sock_path() -> str:
    return os.environ.get("AGENT_SESSIONS_VT_SIDECAR_SOCK") or str(
        Path.home() / ".agent-sessions" / "vt-sidecar.sock"
    )


def _sidecar_js() -> str | None:
    """The bundled sidecar entrypoint: an explicit ``AGENT_SESSIONS_VT_SIDECAR_JS`` override, else
    ``<repo>/vt-sidecar/dist/server.mjs`` relative to this file. ``None`` if it isn't built."""
    env = os.environ.get("AGENT_SESSIONS_VT_SIDECAR_JS")
    if env:
        return env if Path(env).exists() else None
    cand = Path(__file__).resolve().parents[2] / "vt-sidecar" / "dist" / "server.mjs"
    return str(cand) if cand.exists() else None


def _node_bin() -> str | None:
    """The Node binary that runs the sidecar. The installer may VENDOR Node (under
    ``$PREFIX/.toolchain``) just to build the UI/sidecar, and the runtime systemd unit's PATH won't
    include it — so it records the resolved path in ``AGENT_SESSIONS_VT_SIDECAR_NODE``. Prefer that
    (absolute vendored path, or ``node``), else fall back to ``node`` on PATH. ``None`` when neither
    resolves → the sidecar stays disabled and the attach path falls back to the transcript."""
    env = os.environ.get("AGENT_SESSIONS_VT_SIDECAR_NODE")
    if env:
        # An absolute path must exist; a bare name (e.g. "node") is resolved on PATH.
        return env if (os.path.isabs(env) and Path(env).exists()) else (shutil.which(env) or None)
    return shutil.which("node")


class _Sidecar:
    """Owns the sidecar subprocess + a multiplexed unix-socket connection (JSON-RPC by id)."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._id = 0
        self._conn_lock = asyncio.Lock()
        # Live-mirror state (#273). `_geom[key]` = the agent's pty (cols, rows), set on attach/
        # resize. Its presence also GATES feeds (only mirror sessions a client opened). All mirror
        # ops (open/feed/snapshot) funnel through one FIFO queue drained by a single pump task, so
        # live bytes reach the emulator strictly IN ORDER — concurrent fire-and-forget writes could
        # otherwise interleave on the socket and corrupt the mirror.
        self._geom: dict[str, tuple[int, int]] = {}
        self._mirror_q: asyncio.Queue | None = None
        self._mirror_task: asyncio.Task | None = None
        # Keys whose mirror is known UNTRUSTWORTHY — a feed was dropped (queue full), an open/feed
        # op failed, or the connection dropped (sidecar may have restarted, emulators gone). A dirty
        # mirror is missing bytes, so `live_snapshot()` returns None for it (→ caller falls back to
        # the transcript, never a partial mirror passed off as faithful — Hermes #273). Cleared by a
        # fresh reopen in `note_resize` (which drops the stale emulator first and rebuilds from the
        # live feed forward).
        self._dirty: set[str] = set()

    # --- process lifecycle (driven by the app lifespan) ---------------------------------------
    async def ensure_started(self) -> None:
        if not enabled():
            return
        js = _sidecar_js()
        node = _node_bin()
        if not js or not node:
            return  # not built / no node → stay disabled, caller falls back to transcript
        if self._proc is not None and self._proc.returncode is None:
            return
        sock = _sock_path()
        Path(sock).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        env = dict(os.environ, AGENT_SESSIONS_VT_SIDECAR_SOCK=sock)
        with contextlib.suppress(Exception):
            self._proc = await asyncio.create_subprocess_exec(
                node,
                js,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        for _ in range(60):  # wait up to ~3s for the socket to appear
            if Path(sock).exists():
                return
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._mirror_task is not None:
            self._mirror_task.cancel()
        self._mirror_task = self._mirror_q = None
        self._geom.clear()
        self._dirty.clear()
        if self._read_task is not None:
            self._read_task.cancel()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._reader = self._writer = self._read_task = None
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=3)
        self._proc = None

    # --- connection + request/response --------------------------------------------------------
    async def _conn(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            return True
        async with self._conn_lock:
            if self._writer is not None and not self._writer.is_closing():
                return True
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(_sock_path(), limit=_STREAM_LIMIT),
                    _CONNECT_TIMEOUT,
                )
                self._read_task = asyncio.create_task(self._read_loop())
                return True
            except Exception:
                self._reader = self._writer = None
                return False

    async def _read_loop(self) -> None:
        try:
            assert self._reader is not None
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                with contextlib.suppress(Exception):
                    msg = json.loads(line)
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except Exception:  # noqa: S110 — best-effort reader; on any error we drop to cleanup below
            pass
        # connection lost — wake any waiters so they fall back rather than hang
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()
        self._reader = self._writer = None
        # The sidecar may have died/restarted → every mirror it held is gone. Mark all known mirrors
        # dirty so their snapshots fail safe until a fresh reopen rebuilds them (Hermes #273).
        self._dirty.update(self._geom.keys())

    async def _request(self, op: str, timeout: float, **kw) -> dict | None:
        if not enabled() or not await self._conn():
            return None
        self._id += 1
        rid = self._id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            assert self._writer is not None
            self._writer.write((json.dumps({"id": rid, "op": op, **kw}) + "\n").encode())
            await self._writer.drain()
            msg = await asyncio.wait_for(fut, timeout)
        except Exception:
            self._pending.pop(rid, None)
            return None
        return msg if (msg and msg.get("ok")) else None

    # --- live mirror (#273) -------------------------------------------------------------------
    # One FIFO queue + one pump task serialize every mirror op so the emulator is fed strictly in
    # byte order. Items are (op, kwargs, future|None); a future is set only for snapshot, so a
    # snapshot is ordered AFTER all feeds enqueued before it and reflects every byte fed so far.
    def _ensure_mirror_pump(self) -> None:
        if self._mirror_q is None:
            self._mirror_q = asyncio.Queue(maxsize=20000)
            self._mirror_task = asyncio.create_task(self._mirror_pump())

    async def _mirror_pump(self) -> None:
        assert self._mirror_q is not None
        while True:
            op, kw, fut = await self._mirror_q.get()
            # A snapshot is on the attach/reconnect hot path — bound it by the tight snapshot budget
            # (0.5s), not the generous feed budget (5s), so a slow sidecar can't stall an attach.
            timeout = _SNAPSHOT_TIMEOUT if op == "snapshot" else _FEED_TIMEOUT
            try:
                msg = await self._request(op, timeout, **kw)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                # An open/feed with no ok response (failure, or the sidecar rejecting a feed for an
                # unknown session) left the mirror missing bytes → mark it dirty (Hermes #273).
                if msg is None and op in ("open", "feed") and kw.get("key"):
                    self._dirty.add(kw["key"])
            except Exception:
                if op in ("open", "feed") and kw.get("key"):
                    self._dirty.add(kw["key"])
                if fut is not None and not fut.done():
                    fut.set_result(None)
            finally:
                self._mirror_q.task_done()

    def note_resize(self, key: str, cols: int, rows: int) -> None:
        """Record the agent's pty geometry for ``key`` and (re)size its mirror emulator. Sync +
        fire-and-forget — called on attach (launch size) and on every agent resize. Runs before
        feeds at that size so live bytes render at the agent width (repaints overwrite, no dup)."""
        if not enabled():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        c, r = max(2, int(cols)), max(2, int(rows))
        prev = self._geom.get(key)
        # A WIDTH change means the mirror's existing scrollback was authored at the OLD width;
        # reflowing absolute-positioned TUI (Claude/Ink) to a new width garbles it — that is the
        # #293 cross-/mixed-width garble (e.g. a desktop-rendered session opened on a phone). So
        # treat a width change like a desync: drop the emulator and rebuild SINGLE-width from here.
        # The pre-resize backlog then fails safe to the clean transcript (live_snapshot → None until
        # it re-warms), so the snapshot is NEVER cross-width and can NEVER garble. A height-only
        # change (mobile address bar) keeps the buffer and just resizes.
        width_changed = prev is not None and prev[0] != c
        was_dirty = key in self._dirty
        self._geom[key] = (c, r)
        self._ensure_mirror_pump()
        try:
            if was_dirty or width_changed:
                # Drop the desynced/old-width emulator first so `open` rebuilds a clean one fed from
                # here forward; its pre-reset history is served by the transcript until it re-warms.
                self._mirror_q.put_nowait(("end", {"key": key}, None))
            self._mirror_q.put_nowait(("open", {"key": key, "cols": c, "rows": r}, None))
            # Clear dirty ONLY once the recovery ops are actually queued. If the queue was full we
            # never enqueued them, so the mirror is still stale — keep it dirty so live_snapshot
            # fails safe to the transcript rather than serving the old emulator as faithful (#273).
            self._dirty.discard(key)
        except asyncio.QueueFull:
            self._dirty.add(key)

    def note_feed(self, key: str, data: bytes) -> None:
        """Append live PTY bytes to the mirror (sync, fire-and-forget). No-op unless the session has
        been opened (``note_resize``) — so we only mirror what a client is viewing, and the emulator
        is always sized before its first feed. Drops on overload (mirror reseeds on attach)."""
        if not enabled() or key not in self._geom or self._mirror_q is None:
            return
        if key in self._dirty:
            return  # desynced mirror — don't keep feeding a buffer we'll discard; reopens on attach
        b64 = base64.b64encode(data).decode("ascii")
        try:
            self._mirror_q.put_nowait(("feed", {"key": key, "data": b64}, None))
        except asyncio.QueueFull:
            # Dropped a chunk → the mirror is missing bytes. Mark dirty so snapshots fail safe until
            # the next attach reopens it clean (Hermes #273).
            self._dirty.add(key)

    async def live_snapshot(self, key: str, cols: int, rows: int) -> bytes | None:
        """Snapshot the LIVE mirror at the client width. Routed through the FIFO queue so it lands
        after all pending feeds. ``None`` when the session isn't mirrored yet (cold) or the snapshot
        is empty → caller falls back to the transcript (never the dup-prone ring replay)."""
        if not enabled() or key not in self._geom or key in self._dirty or self._mirror_q is None:
            return None  # never mirrored / desynced → fail safe to the transcript
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        try:
            self._mirror_q.put_nowait(("snapshot", {"key": key, "cols": cols, "rows": rows}, fut))
        except asyncio.QueueFull:
            return None
        try:
            # Bound the CALLER wait by the snapshot budget too: if the queue is backed up behind
            # feeds, fall back fast rather than stall the attach (Hermes #273).
            msg = await asyncio.wait_for(fut, _SNAPSHOT_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            return None
        if not msg:
            return None
        result = str(msg.get("result") or "")
        return result.encode("utf-8", "replace") if result else None

    def note_session_end_geom(self, key: str) -> None:
        """Forget a session's mirror geometry so feeds stop being queued for it (teardown)."""
        self._geom.pop(key, None)
        self._dirty.discard(key)

    # --- contract -----------------------------------------------------------------------------
    async def feed(self, key: str, data: bytes) -> bool:
        msg = await self._request(
            "feed", _FEED_TIMEOUT, key=key, data=base64.b64encode(data).decode("ascii")
        )
        return msg is not None

    async def snapshot(self, key: str, cols: int, rows: int) -> bytes | None:
        msg = await self._request("snapshot", _SNAPSHOT_TIMEOUT, key=key, cols=cols, rows=rows)
        if msg is None:
            return None
        return str(msg.get("result") or "").encode("utf-8", "replace")

    async def rebuild(self, key: str, data: bytes, cols: int, rows: int) -> bytes | None:
        """Rebuild the emulator from ``data`` (the session ring) and snapshot at the client width,
        in one atomic request. ``None`` on disabled/unreachable/slow → caller falls back."""
        msg = await self._request(
            "rebuild",
            _FEED_TIMEOUT,
            key=key,
            data=base64.b64encode(data).decode("ascii"),
            cols=cols,
            rows=rows,
        )
        if msg is None:
            return None
        return str(msg.get("result") or "").encode("utf-8", "replace")

    async def reset(self, key: str) -> None:
        await self._request("reset", _SNAPSHOT_TIMEOUT, key=key)

    async def end(self, key: str) -> None:
        await self._request("end", _SNAPSHOT_TIMEOUT, key=key)

    async def health(self) -> dict | None:
        msg = await self._request("health", _CONNECT_TIMEOUT)
        return msg.get("result") if msg else None


_singleton = _Sidecar()


async def ensure_started() -> None:
    await _singleton.ensure_started()


async def stop() -> None:
    await _singleton.stop()


async def feed(key: str, data: bytes) -> bool:
    return await _singleton.feed(key, data)


async def snapshot(key: str, cols: int, rows: int) -> bytes | None:
    return await _singleton.snapshot(key, cols, rows)


async def rebuild(key: str, data: bytes, cols: int, rows: int) -> bytes | None:
    return await _singleton.rebuild(key, data, cols, rows)


async def end(key: str) -> None:
    await _singleton.end(key)


async def health() -> dict | None:
    return await _singleton.health()


def note_resize(key: str, cols: int, rows: int) -> None:
    _singleton.note_resize(key, cols, rows)


def note_feed(key: str, data: bytes) -> None:
    _singleton.note_feed(key, data)


async def live_snapshot(key: str, cols: int, rows: int) -> bytes | None:
    return await _singleton.live_snapshot(key, cols, rows)


def note_session_end(key: str) -> None:
    """Fire-and-forget sidecar teardown from sync code (``scrollback._drop_buffer``). No-op when
    disabled or no event loop is running (e.g. unit tests touching the ring directly)."""
    if not enabled():
        return
    _singleton.note_session_end_geom(key)  # stop queuing feeds for it
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(end(key))
