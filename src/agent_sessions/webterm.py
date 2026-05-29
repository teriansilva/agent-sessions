"""Websocket ↔ PTY bridge for the per-session terminal (issue #49, Phase 2b).

Bridges a browser xterm.js websocket to a `dtach`-backed PTY (see `ptybridge`).
The agent runs under a persistent dtach master, so closing the browser detaches
(the agent keeps running) and reconnecting re-attaches — survives tab close and
app redeploys. Replaces the ttyd transport; no Zellij involved.

Wire protocol (we own both ends):
- client → server: JSON text frames — ``{"t":"i","d":"<input>"}`` for keystrokes,
  ``{"t":"r","cols":C,"rows":R}`` for resize. (Raw binary frames are also written
  through as input, for robustness.)
- server → client: raw **binary** frames = PTY output, forwarded verbatim.

Backpressure is natural: each output chunk is ``await``-sent before the next read,
so a slow client throttles the read loop (the PTY buffer fills and the agent
blocks on write) rather than growing an unbounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import struct
import termios
import time
from collections import OrderedDict

from . import ptybridge, sessionlock


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:  # master closed / child gone
        return b""


# Per-session scrollback. dtach has no scrollback, so on reattach an inline agent
# (claude/codex/gemini) only repaints its near-empty current frame, not the history
# — the terminal looks empty. We keep a capped ring of recent PTY output per session
# in the app process and replay it on connect, so reconnecting shows the conversation
# again. Survives browser disconnect (in-process); lost on app restart (rebuilds on
# the next live output). Keyed by the engine-qualified session id.
#
# `_TOTALS[key]` is a monotonic count of *all* bytes ever sent for the key (not just
# what's still in the ring). It powers delta-resume: a reconnecting client reports the
# absolute offset it last saw (`?have=`), and we stream only the bytes since then — so
# a transient ws drop continues seamlessly instead of re-replaying the whole ring (or,
# worse, blanking). See docs/session-handling.md §Reconnect continuity.
_MAX_BUF = 256 * 1024

# Hard cap on how many *distinct* session buffers we retain at once. Each entry is
# capped at `_MAX_BUF`, but without a ceiling on the *count* every session that ever
# attached would leave up to 256KB resident for the process lifetime (audit MEDIUM:
# unbounded growth over many sessions). The buffers are insertion-/access-ordered
# (`OrderedDict`, move-to-end on touch); when over the cap we evict the
# least-recently-used entry. A still-attached/alive session is touched on every
# output chunk, so it stays at the hot end and survives eviction — delta-resume for
# live sessions is preserved. Dead sessions also get dropped eagerly at end-of-run
# (see `_maybe_evict_ended`), so the LRU cap is a worst-case backstop, not the
# primary reclaim path.
_MAX_BUFFERS = 64
_BUFFERS: OrderedDict[str, bytearray] = OrderedDict()
_TOTALS: OrderedDict[str, int] = OrderedDict()
# Per-key wall-clock of the last byte we observed flowing from the agent (#156). Powers the
# "agent working" indicator (#156). Stamped from the byte-ingest path; the #183
# SessionStream keeps it fresh even with no browser attached. Best-effort and bounded by
# the same LRU as the buffers.
_LAST_OUTPUT_AT: OrderedDict[str, float] = OrderedDict()

# Post-attach replay grace (#195). A fresh ``dtach -a`` client triggers a screen REPLAY
# (the TUI repaints its current state via SIGWINCH) — a byte burst that is NOT new agent
# activity. For this long after an attach, ingested bytes still fill the scrollback ring
# but DON'T stamp the working signal, so merely selecting a session (or server startup
# discovery) can't flip the "agent working" dot. Genuine output after the window stamps
# as before. Per key → ``time.time()`` after which output counts as real again.
_ATTACH_REPLAY_GRACE_S = 0.5
_SUPPRESS_OUTPUT_UNTIL: dict[str, float] = {}


def note_attach(key: str) -> None:
    """Open the post-attach replay-grace window for ``key`` (#195). Called by every reader
    that attaches a fresh ``dtach -a`` client — the WS bridge (``run``) and the headless
    ``SessionStream`` — so the replay burst it triggers doesn't register as agent activity.
    """
    _SUPPRESS_OUTPUT_UNTIL[key] = time.time() + _ATTACH_REPLAY_GRACE_S


def _drop_buffer(key: str) -> None:
    _BUFFERS.pop(key, None)
    _TOTALS.pop(key, None)
    _LAST_OUTPUT_AT.pop(key, None)
    _SUPPRESS_OUTPUT_UNTIL.pop(key, None)


def _session_alive(buf_key: str) -> bool:
    """Is the dtach master for this engine-qualified session id still running?

    Used to protect a live session's scrollback from eviction (it's needed for a
    reconnect's delta-resume) and to eagerly reclaim a dead one. Best-effort: an
    unparseable key / lookup error is treated as NOT alive (i.e. evictable).
    """
    try:
        from . import engines

        prov, native = engines.parse_key(buf_key)
        return ptybridge.session_exists(prov.engine_id, native)
    except Exception:
        return False


def _enforce_buffer_cap() -> None:
    """Bound the number of retained buffers — but only by evicting buffers whose dtach
    master is GONE. A live session's scrollback is never evicted (an idle/attached
    session produces no output to refresh its LRU recency, yet still needs the buffer
    for delta-resume — the bug Hermes caught). So the cap reclaims dead/orphan buffers
    only; concurrent *live* sessions are all retained (their memory is legitimate and
    bounded by real concurrency), and dead ones are normally reaped eagerly at
    end-of-run via `_maybe_evict_ended`.
    """
    while len(_BUFFERS) > _MAX_BUFFERS:
        victim = next((k for k in _BUFFERS if not _session_alive(k)), None)
        if victim is None:
            break  # everything retained is live — keep it all
        _drop_buffer(victim)


def _buffer_append(key: str, data: bytes) -> None:
    buf = _BUFFERS.get(key)
    if buf is None:
        buf = bytearray()
        _BUFFERS[key] = buf
    _BUFFERS.move_to_end(key)  # most-recently-used
    buf.extend(data)
    _TOTALS[key] = _TOTALS.get(key, 0) + len(data)
    _TOTALS.move_to_end(key)
    # Skip the working-signal stamp while inside the post-attach replay grace (#195):
    # the screen-redraw burst is not new agent activity. Scrollback (buf/_TOTALS) is
    # always updated so a reattach still resumes the full screen.
    now = time.time()
    if now >= _SUPPRESS_OUTPUT_UNTIL.get(key, 0.0):
        _LAST_OUTPUT_AT[key] = now
        _LAST_OUTPUT_AT.move_to_end(key)
    if len(buf) > _MAX_BUF:
        del buf[: len(buf) - _MAX_BUF]
    _enforce_buffer_cap()


def get_last_output_at(key: str) -> float | None:
    """Wall-clock of the last byte observed from this session (#156). ``None`` if we've
    never seen output for it — either the session has no attached WS, or the buffer was
    evicted. Best-effort.
    """
    return _LAST_OUTPUT_AT.get(key)


def _maybe_evict_ended(buf_key: str | None) -> None:
    """Drop a session's retained buffer once its run ends and no dtach master survives.

    The buffer only earns its keep while the agent is still alive (a later reconnect
    delta-resumes from it). When the dtach master is gone there's nothing to resume,
    so we reclaim the memory immediately rather than waiting for the LRU backstop.
    Best-effort — any failure leaves the entry for `_enforce_buffer_cap` to reclaim.
    """
    if buf_key and not _session_alive(buf_key):
        _drop_buffer(buf_key)


def _resume_payload(key: str, have: int) -> tuple[bytes, int]:
    """Decide what to (re)send on connect: ``(payload, total)``.

    - No history yet → ``(b"", total)``.
    - Alt-screen TUI (opencode) → ``(b"", total)``: it repaints via SIGWINCH; replaying
      its frames corrupts the redraw, and we never blank on reconnect.
    - ``have`` is a valid absolute offset still inside the ring → the **delta** since
      ``have`` (seamless continuation across a drop).
    - Otherwise (fresh attach, or ``have`` fell behind the capped ring) → **full replay**.

    The caller follows the payload with a ``{"t":"seq","n":total}`` control frame so the
    client adopts ``total`` as its authoritative offset for the next reconnect.
    """
    total = _TOTALS.get(key, 0)
    ring = _BUFFERS.get(key) or b""
    if not ring or _in_alt_screen(bytes(ring)):
        return b"", total
    ring_start = total - len(ring)  # absolute offset of ring[0]
    if 0 < have <= total and have >= ring_start:
        return bytes(ring[have - ring_start :]), total
    return bytes(ring), total


def _in_alt_screen(buf: bytes) -> bool:
    """True if the session is currently on the alternate screen buffer.

    Replaying raw scrollback is right for *inline* agents (claude/codex/gemini) but
    wrong for an alt-screen TUI (opencode): the alt buffer has no scrollback, and
    replaying its frames corrupts the redraw (blank screen that only partially
    reappears on scroll). Such sessions redraw themselves via SIGWINCH on attach,
    so we skip the replay. Detected by the last 1049h (enter) vs 1049l (leave)."""
    return buf.rfind(b"\x1b[?1049h") > buf.rfind(b"\x1b[?1049l")


async def run(
    ws,
    argv: list[str],
    *,
    cwd: str,
    buf_key: str | None = None,
    cols: int = 80,
    rows: int = 24,
    lock: sessionlock.SessionLock | None = None,
    have: int = 0,
    read_only_gate: asyncio.Event | None = None,
) -> None:
    """Attach ``ws`` to the PTY of ``argv`` (a built dtach create-or-attach command).

    ``ws`` must already be ``accept``ed. Spawns the dtach client on a fresh PTY with
    ``cwd`` as its working dir, then pumps both directions until either side closes.
    On exit the dtach *client* is terminated (a detach); the dtach *master* keeps the
    agent alive for the next reconnect.

    ``lock`` (set only when this connection is launching a fresh master) is the
    single-writer lock; its fd is passed to the spawned process so the long-lived
    ``dtach`` master inherits it and holds the flock for the master's lifetime. We
    only borrow the fd here — the caller owns closing/transferring the lock.

    ``read_only_gate`` (set by the caller for secondary-tab attaches, or fired
    mid-session when another tab force-takes the owner role — #184 slice 3):
    when set, input frames (``i``, ``r``, raw bytes) are silently dropped server-
    side so a misbehaving secondary client can never write to the agent. Output
    keeps streaming so the secondary tab is read-only, not blind.
    """
    master, slave = os.openpty()
    _set_winsize(slave, rows, cols)
    # A real color terminal: without TERM, Ink-based agents (claude) disable color.
    # The web frontend is xterm.js, which is a 256-color / truecolor terminal.
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own session → the slave becomes the controlling tty
            close_fds=True,
            # Hand the single-writer lock fd to the dtach master it forks, so the flock
            # lives exactly as long as the running agent (survives an app restart). dtach
            # never closes inherited fds it doesn't manage. pass_fds forces inheritance.
            pass_fds=(lock.fd,) if lock is not None else (),
        )
    except OSError:
        os.close(master)
        os.close(slave)
        with contextlib.suppress(Exception):
            await ws.close(code=4500)
        return
    os.close(slave)  # parent keeps only the master end
    loop = asyncio.get_event_loop()

    # This fresh dtach client will trigger a screen replay; don't let that burst flip the
    # working dot (#195). Genuine output after the grace window stamps normally.
    if buf_key:
        note_attach(buf_key)

    # (Re)connect resume: replay history (or just the delta since the client's `have`
    # offset) so a reattach shows the prior conversation and a transient drop continues
    # seamlessly — never blank. Then send the authoritative byte offset as a control
    # frame. dtach has no scrollback of its own; alt-screen TUIs repaint via SIGWINCH.
    if buf_key:
        payload, total = _resume_payload(buf_key, have)
        if payload:
            with contextlib.suppress(Exception):
                await ws.send_bytes(payload)
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"t": "seq", "n": total}))

    async def pump_out() -> None:
        while True:
            data = await loop.run_in_executor(None, _read, master)
            if not data:
                break
            if buf_key is not None:
                _buffer_append(buf_key, data)
            await ws.send_bytes(data)  # awaited → natural backpressure

    def _gated() -> bool:
        return read_only_gate is not None and read_only_gate.is_set()

    async def pump_in() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is not None:
                try:
                    obj = json.loads(text)
                except (ValueError, TypeError):
                    continue
                kind = obj.get("t")
                # Read-only gate (#184): the secondary tab's WS may keep sending
                # frames; we silently drop input + resize so a misbehaving
                # client can never write to the dtach master. Server-side gate
                # is the source of truth — not the client.
                if kind == "i" and not _gated():
                    with contextlib.suppress(OSError):
                        os.write(master, obj.get("d", "").encode("utf-8", "replace"))
                elif kind == "r" and not _gated():
                    with contextlib.suppress(ValueError, TypeError):
                        _set_winsize(master, int(obj.get("rows", rows)), int(obj.get("cols", cols)))
                        # TIOCSWINSZ on the master doesn't reliably deliver SIGWINCH to
                        # the dtach client here, so dtach never forwards the new size to
                        # the agent's own pty (the terminal stayed a fixed size on window
                        # resize). Nudge the dtach client directly so it re-reads the tty
                        # size and resizes the program → the live agent re-renders wider.
                        with contextlib.suppress(ProcessLookupError, OSError):
                            proc.send_signal(signal.SIGWINCH)
            elif msg.get("bytes") is not None and not _gated():
                with contextlib.suppress(OSError):
                    os.write(master, msg["bytes"])

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(OSError):
            os.close(master)
        # Detach (don't kill the agent): terminate our dtach client; the master persists.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        with contextlib.suppress(Exception):
            await ws.close()
        # Reclaim the scrollback for a session whose dtach master has exited — there's
        # nothing left to resume. Live sessions keep their buffer (master still alive).
        _maybe_evict_ended(buf_key)
