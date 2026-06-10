"""Server-owned session readers + the live-session registry (#183, slice 2).

Slice 2 of the session-stability foundation (slice 1 = #165). Makes the server
the source of truth about every live dtach session, so the scrollback ring +
``last_output_at`` stamp stay fresh whether or not a browser is attached. This
closes #179 (partial scrollback after a deploy or LRU eviction) and promotes
#156's working dot from browser-attached-only to "always accurate" without any
frontend change.

Ownership model — exactly one writer of ``webterm._BUFFERS[key]`` at any time:
- HEADLESS: ``SessionStream`` opens its own ``dtach -a`` and drains bytes into
  the shared ring.
- ATTACHED: the WS bridge (``webterm.run``) is the writer. The registry stops
  any server-owned stream before the browser starts reading.

The handoff is keyed by the PHYSICAL id (``engine:phys_native``) — the same
key ``webterm`` uses as ``buf_key`` / ``phys_key`` — so an opencode
placeholder→real alias never splits state across two keys. Per Hermes review
(#183) the launch ``sessionlock`` is intentionally NOT reused as a read-arbiter;
the registry's own mutex serialises attach/detach handoffs.

Phase 2 follow-up: fold the WS bridge into a subscribe-tail off
``SessionStream`` so there is literally one ``dtach -a`` per session.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import TYPE_CHECKING

from . import engines, ptybridge, webterm

if TYPE_CHECKING:
    from collections.abc import Iterable


# Same window as #156 so the API behaves identically whether ``working`` was
# sourced from the browser-attached path or the server-owned stream.
_WORKING_WINDOW_S = 10.0

# Read chunk from the stream's master fd. Matches webterm._read's implicit size.
_READ_CHUNK = 65536

# Per-tab claim lease (#184 slice 3). An owner is considered "stale" if its
# last heartbeat is older than this — at which point the next attach takes
# over without needing a ``force=1``. 5 seconds is the budget the WS owes for
# either keeping the connection alive or reasserting itself via heartbeat.
_CLAIM_LEASE_S = 5.0


class Claim:
    r"""One per-tab ownership token for a session (#184). The WS bridge that
    holds the owner role for a session keeps a reference and ``await``\s
    ``demoted`` in parallel with its bridge pumps; ``Registry.claim(force=True)``
    sets that event when another tab takes the role.
    """

    def __init__(self, fp: str, tab_id: str) -> None:
        self.fp = fp
        self.tab_id = tab_id
        self.last_seen = time.time()
        self.demoted = asyncio.Event()

    def matches(self, fp: str, tab_id: str) -> bool:
        return self.fp == fp and self.tab_id == tab_id

    def is_stale(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.last_seen > _CLAIM_LEASE_S


class SessionStream:
    """One server-owned ``dtach -a`` reader per (headless) live session.

    Owns a PTY + a dtach subprocess attached to the session's socket. The
    drain task reads bytes from the master fd and appends to the shared
    scrollback ring (``webterm._BUFFERS[key]``). Lifecycle: ``start()`` →
    drain loop → ``ended`` fires when dtach exits → ``stop()`` is idempotent.

    The registry stops the stream on browser attach and spawns a fresh one on
    detach (if the dtach master is still alive), so there is at most one writer
    to the buffer ring per key.
    """

    def __init__(self, engine: str, session_id: str) -> None:
        self.engine = engine
        self.session_id = session_id
        self.key = f"{engine}:{session_id}"
        self.started_at = time.time()
        self._master: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._subs: set[asyncio.Queue[bytes]] = set()
        self.ended = asyncio.Event()

    async def start(self) -> None:
        """Spawn the dtach client on a fresh PTY and start draining bytes."""
        argv = ptybridge.attach_argv(engine=self.engine, session_id=self.session_id)
        master, slave = os.openpty()
        # Size the reader's pty to the session's last-known geometry (else a sane default) BEFORE
        # dtach attaches. An unsized `openpty()` is 0×0, and dtach (`-r winch`) relays the
        # attaching client's size to the running program — so a headless reader at 0×0 collapses
        # the live agent to 0×0, which renders into nothing and poisons the byte ring with
        # degenerate-width frames (#297; a big contributor to the garble saga). Never attach at 0×0.
        last_cols = webterm.scrollback._LAST_COLS.get(self.key) or 80
        last_rows = webterm.scrollback._LAST_ROWS.get(self.key) or 24
        webterm._set_winsize(slave, max(1, int(last_rows)), max(1, int(last_cols)))
        try:
            self._proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                    close_fds=True,
                ),
                timeout=webterm.SPAWN_TIMEOUT_S,  # bounded like the viewer path (#346 Phase A)
            )
        except (TimeoutError, OSError):
            os.close(master)
            os.close(slave)
            self.ended.set()
            return
        os.close(slave)
        self._master = master
        # This headless attach also triggers a dtach screen replay; suppress that burst
        # from the working signal (#195) — otherwise startup discovery would light every
        # session's dot for the grace window. Real output after it stamps normally.
        webterm.note_attach(self.key)
        self._task = asyncio.create_task(self._drain(), name=f"session_stream:{self.key}")

    async def _drain(self) -> None:
        loop = asyncio.get_event_loop()
        assert self._master is not None
        try:
            while True:
                data = await loop.run_in_executor(None, self._read_once, self._master)
                if not data:
                    break
                webterm._buffer_append(self.key, data)
                # Best-effort fan-out: a slow subscriber drops its queue rather
                # than back-pressuring the whole drain loop. Reserved for the
                # phase-2 subscribe-tail consolidation.
                dead: list[asyncio.Queue[bytes]] = []
                for q in self._subs:
                    try:
                        q.put_nowait(data)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    self._subs.discard(q)
        finally:
            self.ended.set()

    @staticmethod
    def _read_once(fd: int) -> bytes:
        try:
            return os.read(fd, _READ_CHUNK)
        except OSError:
            return b""

    def subscribe(self) -> asyncio.Queue[bytes]:
        """Reserved for the phase-2 subscribe-tail consolidation (#183 follow-up)."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subs.discard(q)

    async def stop(self) -> None:
        """Tear down the drain task + dtach subprocess. Idempotent."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                with contextlib.suppress(Exception):
                    await self._proc.wait()
            self._proc = None
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None
        self.ended.set()


class SessionRegistry:
    """Process-wide registry of live dtach sessions + their server-owned readers.

    Mutations are all idempotent and serialised under one ``asyncio.Lock``:
    - ``discover()`` (startup): spawn a stream per ``ptybridge.list_sessions()``
      entry — each discovered session is by definition headless on startup.
    - ``on_attach(engine, sid)`` (called by the WS endpoint BEFORE the browser
      starts reading bytes): stops any server-owned stream for that key + flips
      ``attached`` true so the WS bridge becomes the sole writer.
    - ``on_detach(engine, sid)`` (called AFTER the WS bridge exits): if the
      dtach master is still alive, spawns a fresh server-owned stream; else
      drops the entry.
    - ``_watch_end()`` (per-stream, internal): when a stream's dtach exits on
      its own (agent died), drops the entry if it isn't attached.

    Sessions are keyed by the PHYSICAL id (``engine:phys_native``) — the same
    key ``webterm`` uses as ``buf_key`` / ``phys_key`` — so the opencode
    placeholder→real alias never splits state across two keys. No silent
    eviction of alive streams: ``stop_all()`` is the only path that ends a
    running stream (app shutdown).
    """

    def __init__(self) -> None:
        # key → {"engine", "sid", "attached" (bool), "stream" (SessionStream|None),
        #        "started_at" (float)}
        self._sessions: dict[str, dict] = {}
        self._state_subs: set[asyncio.Queue[dict]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def discover(self) -> None:
        """Scan dtach sockets + connect-probe; spawn a server-owned stream per
        live session."""
        for engine, sid in ptybridge.list_sessions():
            with contextlib.suppress(Exception):
                await self._ensure_headless(engine, sid)

    def _resolve_phys(self, engine: str, sid: str) -> tuple[str, str, str]:
        """Resolve the logical (engine, sid) to its PHYSICAL key and parts. The
        opencode placeholder→real alias means a logical ``ses_…`` may map to a
        placeholder physical key; everything else is identity.
        """
        phys_key = engines.physical_key(f"{engine}:{sid}")
        phys_engine, _, phys_sid = phys_key.partition(":")
        return phys_engine, phys_sid, phys_key

    async def _ensure_headless(self, engine: str, sid: str) -> None:
        """Internal: spawn a SessionStream for a headless session under the lock."""
        async with self._lock:
            if self._closed:
                return
            phys_engine, phys_sid, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is not None:
                if entry.get("attached"):
                    return  # browser owns the bytes — no headless stream
                stream = entry.get("stream")
                if stream is not None and not stream.ended.is_set():
                    return  # already running
            stream = SessionStream(phys_engine, phys_sid)
            await stream.start()
            if stream.ended.is_set():
                return  # start failed (dtach exec error / master gone)
            self._sessions[key] = {
                "engine": phys_engine,
                "sid": phys_sid,
                "attached": False,
                "stream": stream,
                "started_at": stream.started_at,
            }
            asyncio.create_task(self._watch_end(key, stream))
            await self._notify({"t": "added", "session": self._row(key)})

    async def on_attach(self, engine: str, sid: str) -> None:
        """Tell the registry a browser is about to start reading this session's
        bytes. Stops any server-owned stream so there's only one writer."""
        async with self._lock:
            if self._closed:
                return
            phys_engine, phys_sid, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is None:
                self._sessions[key] = {
                    "engine": phys_engine,
                    "sid": phys_sid,
                    "attached": True,
                    "stream": None,
                    "started_at": time.time(),
                }
                await self._notify({"t": "added", "session": self._row(key)})
                return
            stream = entry.get("stream")
            entry["attached"] = True
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.stop()
                entry["stream"] = None
                await self._notify({"t": "updated", "session": self._row(key)})

    async def on_detach(self, engine: str, sid: str) -> None:
        """Browser disconnected. If the dtach master is still alive, hand byte
        ownership back to a fresh server-owned SessionStream; else drop."""
        async with self._lock:
            if self._closed:
                return
            phys_engine, phys_sid, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is None:
                return
            entry["attached"] = False
            try:
                master_alive = ptybridge.session_exists(phys_engine, phys_sid)
            except Exception:
                master_alive = False
            if not master_alive:
                self._sessions.pop(key, None)
                await self._notify({"t": "removed", "session_id": key})
                return
            stream = SessionStream(phys_engine, phys_sid)
            await stream.start()
            if stream.ended.is_set():
                self._sessions.pop(key, None)
                await self._notify({"t": "removed", "session_id": key})
                return
            entry["stream"] = stream
            asyncio.create_task(self._watch_end(key, stream))
            await self._notify({"t": "updated", "session": self._row(key)})

    async def _watch_end(self, key: str, stream: SessionStream) -> None:
        await stream.ended.wait()
        # When dtach exits naturally the drain loop bails on EOF and sets
        # ``ended``, but it does NOT close the PTY master fd or reap the
        # subprocess — only ``stop()`` does that. Call it here so the natural
        # end path doesn't leak fds in the long-running FastAPI process. Safe
        # to call regardless: ``stop()`` is idempotent and a no-op for
        # already-cleaned streams. We close OUTSIDE the lock to avoid blocking
        # other registry mutators on subprocess reap.
        with contextlib.suppress(Exception):
            await stream.stop()
        async with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.get("stream") is not stream:
                return  # superseded by an attach/detach handoff
            if entry.get("attached"):
                entry["stream"] = None
                await self._notify({"t": "updated", "session": self._row(key)})
                return
            self._sessions.pop(key, None)
            await self._notify({"t": "removed", "session_id": key})

    def get(self, key: str) -> dict | None:
        return self._sessions.get(key)

    def keys(self) -> Iterable[str]:
        return list(self._sessions.keys())

    # ---- Per-tab claim lease (#184) ------------------------------------------

    async def claim(
        self,
        engine: str,
        sid: str,
        fp: str,
        tab_id: str,
        *,
        force: bool = False,
    ) -> tuple[str, Claim | None]:
        """Try to claim owner role for one (engine, sid) for the given (fp, tab_id).

        Returns ``("owner", claim)`` if the caller now holds the role — either no
        prior owner existed, the prior owner's lease was stale, the caller IS the
        prior owner (heartbeat case), or ``force=True`` was set. The previous
        owner's ``Claim.demoted`` event is fired on a successful force takeover
        so its WS bridge can transition itself to read-only / disconnect.

        Returns ``("secondary", None)`` if there is a live owner with a fresh
        lease that is not the caller; the caller should render read-only and
        offer a "Take over" affordance that calls ``claim(..., force=True)``.

        Backward-compatible: an empty ``fp`` or ``tab_id`` is taken as "no
        claim, just attach" → the caller gets owner status without recording
        a claim, so old clients never see the ownership protocol.
        """
        async with self._lock:
            phys_engine, phys_sid, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is None:
                # No registry entry yet — usually ws_term calls on_attach first,
                # but be defensive: bootstrap a stub entry the same way.
                entry = {
                    "engine": phys_engine,
                    "sid": phys_sid,
                    "attached": True,
                    "stream": None,
                    "started_at": time.time(),
                    "owner": None,
                }
                self._sessions[key] = entry
                await self._notify({"t": "added", "session": self._row(key)})
            # Legacy / no-claim attach (e.g. tests, older clients): grant
            # ownership without storing a Claim so the lease layer is invisible.
            if not fp or not tab_id:
                return ("owner", None)
            existing: Claim | None = entry.get("owner")
            if existing is None or existing.matches(fp, tab_id) or existing.is_stale() or force:
                if existing is not None and not existing.matches(fp, tab_id):
                    # Force or stale takeover: demote the prior owner so its
                    # bridge can transition itself out of write-mode.
                    existing.demoted.set()
                new_claim = (
                    existing
                    if (existing is not None and existing.matches(fp, tab_id))
                    else Claim(fp, tab_id)
                )
                new_claim.last_seen = time.time()
                entry["owner"] = new_claim
                await self._notify(
                    {
                        "t": "claim_changed",
                        "session_id": key,
                        "role": "owner",
                        "fp": fp,
                        "tab_id": tab_id,
                    }
                )
                return ("owner", new_claim)
            return ("secondary", None)

    def current_owner(self, engine: str, sid: str) -> Claim | None:
        """The session's CURRENT live owner claim (fp/tab), or ``None`` when it is unclaimed, its
        lease has gone stale, or the session is unknown. A read-only point snapshot for the manual
        restart authority check (#331) in the flag-OFF in-memory ownership mode — it never mutates
        state or extends the lease (unlike ``claim``/``refresh``). A one-tick-stale read is fine for
        that guard, so it skips ``self._lock`` and is safe to call synchronously from a request
        handler."""
        _pe, _ps, key = self._resolve_phys(engine, sid)
        entry = self._sessions.get(key)
        if entry is None:
            return None
        existing = entry.get("owner")
        if not isinstance(existing, Claim) or existing.is_stale():
            return None
        return existing

    async def refresh(self, engine: str, sid: str, fp: str, tab_id: str) -> bool:
        """Heartbeat: bump the lease's ``last_seen`` if the caller is the current
        owner. Returns ``True`` on a successful bump, ``False`` if the caller is
        no longer the owner (its bridge should transition to secondary).
        """
        async with self._lock:
            _, _, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is None:
                return False
            existing: Claim | None = entry.get("owner")
            if existing is None or not existing.matches(fp, tab_id):
                return False
            existing.last_seen = time.time()
            return True

    async def release(self, engine: str, sid: str, fp: str, tab_id: str) -> None:
        """Owner detached cleanly. Drops the claim only if (fp, tab_id) still
        matches — a forced takeover already replaced the owner, so we leave that
        one in place."""
        async with self._lock:
            _, _, key = self._resolve_phys(engine, sid)
            entry = self._sessions.get(key)
            if entry is None:
                return
            existing: Claim | None = entry.get("owner")
            if existing is None or not existing.matches(fp, tab_id):
                return
            entry["owner"] = None
            await self._notify(
                {
                    "t": "claim_changed",
                    "session_id": key,
                    "role": "released",
                    "fp": fp,
                    "tab_id": tab_id,
                }
            )

    def _row(self, key: str) -> dict:
        entry = self._sessions[key]
        last = webterm.get_last_output_at(key)
        return {
            "id": key,
            "engine": entry["engine"],
            "sid": entry["sid"],
            "started_at": entry["started_at"],
            "attached": entry["attached"],
            "last_output_at": last,
            "working": (last is not None) and (time.time() - last < _WORKING_WINDOW_S),
        }

    def snapshot(self) -> list[dict]:
        return [self._row(key) for key in self._sessions]

    def subscribe_state(self) -> asyncio.Queue[dict]:
        """Reserved for the phase-2 ``/ws/state`` endpoint (#183 follow-up)."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
        self._state_subs.add(q)
        return q

    def unsubscribe_state(self, q: asyncio.Queue[dict]) -> None:
        self._state_subs.discard(q)

    async def _notify(self, event: dict) -> None:
        dead: list[asyncio.Queue[dict]] = []
        for q in self._state_subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._state_subs.discard(q)

    async def stop_all(self) -> None:
        """Tear down every stream + close the registry. Used on app shutdown."""
        self._closed = True
        entries = list(self._sessions.values())
        self._sessions.clear()
        for e in entries:
            s = e.get("stream")
            if s is not None:
                with contextlib.suppress(Exception):
                    await s.stop()
