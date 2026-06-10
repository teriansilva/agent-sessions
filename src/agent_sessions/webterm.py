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

The per-session **scrollback** (the capped ring, its on-disk mirror, and the resume /
scroll-up decisions) lives in the sibling :mod:`scrollback` module (#265 S2). Its public
names are re-exported below so ``webterm.<name>`` keeps working for callers and tests;
``webterm.scrollback`` is the module itself (the patch target for its tunables).
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
import time  # noqa: F401 — kept so `webterm.time` stays patchable by tests

from . import scrollback, sessionlock, vtsidecar
from .scrollback import (  # noqa: F401 — re-exported so `webterm.<name>` stays the public surface
    _ATTACH_REPLAY_GRACE_S,
    _BUFFERS,
    _CLEAN_LOAD_CLEAR,
    _LAST_COLS,
    _LAST_OUTPUT_AT,
    _LAST_ROWS,
    _LOADED_FROM_DISK,
    _MAX_BUF,
    _SUPPRESS_OUTPUT_UNTIL,
    _TOTALS,
    _buffer_append,
    _clean_load_payload,
    _drop_buffer,
    _ensure_loaded,
    _in_alt_screen,
    _is_same_width_continuation,
    _key_from_path,
    _maybe_evict_ended,
    _reset_ring,
    _resume_payload,
    _scrollback_path,
    _session_alive,
    _transcript_payload,
    clear_scrollback,
    get_last_output_at,
    live_tail_text,
    note_attach,
    scrollback_cache_stats,
)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    # Never size a pty to 0 in either axis: a 0×0 controlling tty makes Ink-style agents
    # render into nothing (the #292/#293 garble at the source). Floor at 1 as a last-resort
    # guard; callers should already drop degenerate resizes (see pump_in).
    rows, cols = max(1, rows), max(1, cols)
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# Fresh-attach repaint nudge (#304/#329). A same-size attach delivers no SIGWINCH, so an agent
# that only redraws on a geometry change (Ink/claude, ratatui/codex) never repaints — a BLANK
# screen on an idle session, or FRAGMENTS over a live one, when you switch to it (dtach keeps no
# screen to replay, so nothing else shows the current frame). After such an attach we briefly
# shrink the pty and restore it, forcing ONE clean full repaint of the current screen — no
# reconstruction, the SSH/tmux-reattach feel.
#
# #329 hardening — why a 1-column nudge wasn't enough (blank "only on some sessions"):
#  * The shrink→restore pair must be processed by the agent as TWO distinct resizes. Ink/ratatui
#    DEBOUNCE rapid SIGWINCHs; if the pair (and the client's own connect-resize) coalesce, the net
#    size is UNCHANGED and the agent rewrites nothing → blank. So _GAP must exceed the debounce
#    window, and _SETTLE must let the client's connect-resize land FIRST.
#  * The intermediate frame must DIFFER from the agent's internal screen model, or its reconciler
#    writes nothing even after a real resize. A 1-col change renders byte-identical for a
#    width-stable idle frame (a short input box) — exactly the sessions that stayed blank. A
#    larger, 2-D shrink (cols and rows) reflows + moves the bottom-anchored UI, so the rewrite
#    can't be skipped.
_NUDGE_SETTLE_S = 0.3
_NUDGE_GAP_S = 0.25
# How much to shrink for the intermediate frame — enough that the agent's re-layout can't match
# its model. Bounded so the transient frame stays sane on small terminals.
_NUDGE_COLS_DELTA = 8
_NUDGE_ROWS_DELTA = 2

# Re-nudge window (#349): a client resize landing within this many seconds of attach can
# coalesce with the fresh-attach nudge inside the agent's own resize debounce — shrink +
# client-resize + restore net out to zero geometry change and the agent never repaints
# (the mobile keyboard/address-bar blank). A WIDTH-changing resize inside this window
# therefore schedules one debounced TRAILING repaint at the latest accepted geometry,
# making the nudge un-coalesceable instead of re-tuning timing constants.
_RENUDGE_WINDOW_S = 2.0

# Upper bound on creating the dtach client/master subprocess (#346 Phase A). Spawning is
# normally instant; under resource pressure (task-limit EAGAIN, memory stalls) it can fail
# slowly or hang, and an unbounded spawn wedges this connection's coroutine. Timeout and
# OSError both close 4502 (transient, client retries) — never 4500, which is reserved for
# non-retryable launch misconfiguration and permanently kills the client's terminal.
SPAWN_TIMEOUT_S = 15.0


async def _force_repaint(
    master: int, proc: asyncio.subprocess.Process, rows: int, cols: int
) -> None:
    """Force one full repaint from a winch-only-repaint agent: shrink the pty, then restore (#329).

    Mirrors the resize path: TIOCSWINSZ alone doesn't reliably reach the agent through the dtach
    client, so we also SIGWINCH it. The shrink is 2-D and held past the agent's resize-debounce so
    the pair is seen as two distinct resizes whose intermediate frame can't match the agent's
    internal model (a 1-col nudge could, leaving width-stable idle frames blank — #329). Best-effort
    (closed master / exited agent suppressed)."""
    nudge_rows = max(2, rows - _NUDGE_ROWS_DELTA)
    nudge_cols = max(2, cols - _NUDGE_COLS_DELTA)
    with contextlib.suppress(ProcessLookupError, OSError):
        _set_winsize(master, nudge_rows, nudge_cols)
        proc.send_signal(signal.SIGWINCH)
        await asyncio.sleep(_NUDGE_GAP_S)
        _set_winsize(master, rows, cols)
        proc.send_signal(signal.SIGWINCH)


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:  # master closed / child gone
        return b""


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
    stop_event: asyncio.Event | None = None,
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

    ``stop_event`` (single-active-viewer take-over, #293): when set, the bridge
    stops pumping and returns WITHOUT closing the websocket — the caller (the
    take-over route) sends the gate frame on the still-open socket and waits for
    the client to reconnect with ``force=1``. The dtach client is still detached
    (the agent keeps running); only this viewer's stream ends.
    """
    master, slave = os.openpty()
    _set_winsize(slave, rows, cols)
    # A real color terminal: without TERM, Ink-based agents (claude) disable color.
    # The web frontend is xterm.js, which is a 256-color / truecolor terminal.
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
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
            ),
            timeout=SPAWN_TIMEOUT_S,
        )
    except (TimeoutError, OSError):
        # Transient start failure (EAGAIN at the cgroup task ceiling, spawn stall under
        # memory pressure). 4502 = retryable: the client backs off and reconnects, instead
        # of the pre-#346 behaviour of dying permanently on a momentary resource blip.
        os.close(master)
        os.close(slave)
        with contextlib.suppress(Exception):
            await ws.close(code=4502)
        return
    os.close(slave)  # parent keeps only the master end
    loop = asyncio.get_event_loop()

    # This fresh dtach client will trigger a screen replay; don't let that burst flip the
    # working dot (#195). Genuine output after the grace window stamps normally.
    if buf_key:
        scrollback.note_attach(buf_key)
        # Open/size the VT live mirror (#273) at the agent's launch geometry BEFORE any snapshot or
        # feed, so the persistent emulator exists and renders incoming bytes at the agent's width.
        # No-op unless the flag is on. The mirror is then fed by `_buffer_append` for this session's
        # whole life (attached pump AND detached SessionStream), so a later reattach snapshots a
        # fully-current, duplicate-free console history.
        scrollback._LAST_ROWS[buf_key] = rows
        vtsidecar.note_resize(buf_key, cols, rows)

    # (Re)connect resume: replay history (or just the delta since the client's `have`
    # offset) so a reattach shows the prior conversation and a transient drop continues
    # seamlessly — never blank. Then send the authoritative byte offset as a control
    # frame. dtach has no scrollback of its own; alt-screen TUIs repaint via SIGWINCH.
    # `blank_attach` (#349): true when this attach delivered nothing the user can see —
    # no payload at all, or only the clean-load clear. Such an attach must always get
    # the repaint nudge (even at have>0): an idle agent would otherwise leave the
    # client staring at a cleared screen until its next input byte.
    blank_attach = True
    payload_is_clear = False
    # Exact first-page cursor for scroll-up lazy-load (#348 / Hermes #365 r2): set when the
    # attach payload came from the transcript renderer, which knows the first turn index it
    # delivered. Sent to the client as {"t":"hist","cursor":N} right after the seq frame.
    hist_cursor: int | None = None
    if buf_key:
        payload, total = scrollback._resume_payload(buf_key, have)
        # Scroll-up source (#262). The raw ring is authored at the agent's fixed pty width; replay
        # at a DIFFERENT client width mis-positions its absolute cursor moves and garbles. So replay
        # it ONLY for a genuine same-width continuation — a brief WS blip where the client still
        # holds the matching-width scrollback and only needs the byte-delta. EVERY other case
        # re-renders the engine's SAVED TRANSCRIPT (#242) at this client's width — clean,
        # width-correct — and falls back to a clean-load clear when there's no adapter:
        #   • fresh page load (have<=0) — client xterm is empty;
        #   • cross-width client — the ring's width ≠ this client's;
        #   • post-restart CROSS-width reconnect — the width sidecar (#348) restores the
        #     ring's authored width on rehydrate, so a SAME-width have>0 reconnect after a
        #     restart IS a continuation now and replays the ring delta; only a client at a
        #     different width (or a missing sidecar from a pre-#348 mirror ⇒ buffer_cols
        #     None) lands here and gets the width-correct transcript instead (#206).
        # `total` is unchanged in every branch, so the `seq` frame below stays the real byte offset
        # and delta-resume is unaffected.
        # Authored width of the replayable ring (None when unknown/mixed) — the raw
        # continuation contract, NOT merely the last client width (Hermes #360 r4).
        buffer_cols = scrollback.ring_cols(buf_key)
        if scrollback._is_same_width_continuation(have, buffer_cols, cols):
            # Same-width continuation: the client already holds a valid screen and the
            # (possibly empty) payload is just the byte delta — an empty delta means
            # "up to date", NOT blank. Never nudge it (Hermes #359: the #304 no-flicker
            # reconnect must survive the blank-attach rule).
            blank_attach = False
        if not scrollback._is_same_width_continuation(have, buffer_cols, cols):
            # Path B (#271/#273): the faithful real-console snapshot from the VT sidecar, rebuilt
            # from the ring at this client's width. Flag-gated + fail-safe (None when off/unhealthy)
            # — then we fall back to transcript scroll-up, then a clean-load clear. Synthetic:
            # `total` is unchanged so the `seq` frame + delta-resume are unaffected.
            vtpayload = await scrollback._vt_snapshot_payload(buf_key, cols, rows)
            if vtpayload is not None:
                payload = vtpayload
            else:
                tres = await loop.run_in_executor(
                    None, scrollback._transcript_payload, buf_key, cols, rows
                )
                if tres is not None:
                    payload, hist_cursor = tres
                else:
                    # No transcript → clean-load: clear on a width mismatch (no garbled cross-width
                    # replay), reset the ring so a later same-width attach can't replay stale bytes.
                    # With VT on, the ring is the source we rebuild from at any width — never reset
                    # it (#273), or the next attach loses its faithful scroll-up.
                    clear = scrollback._clean_load_payload(total, cols, buffer_cols)
                    if clear is not None:
                        if not vtsidecar.enabled():
                            scrollback._reset_ring(buf_key)
                        payload = clear
                        payload_is_clear = True
        # Track this client's width for the clean-load fallback / resize logic (every
        # connect) — attach-aware so the persisted sidecar only ever claims a width the
        # retained ring was actually authored at (Hermes #360 round 3).
        scrollback.note_attach_width(buf_key, cols)
        if payload:
            blank_attach = payload_is_clear
            with contextlib.suppress(Exception):
                await ws.send_bytes(payload)
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"t": "seq", "n": total}))
        if hist_cursor is not None:
            # The transcript attach path EXPORTS its exact turn boundary (#348 / Hermes #365
            # r2 finding 1): the client seeds its history loader from this frame and always
            # sends `before=` — the server never re-derives the "already delivered" boundary
            # from rendered line counts at a possibly different width (a resize between
            # attach and the first lazy-load made that re-derivation skip turns).
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"t": "hist", "cursor": hist_cursor}))

    # Latest ACCEPTED geometry (#349): nudges must restore to what the client most
    # recently negotiated, not the attach-time grid — restoring stale attach geometry
    # would undo a mobile width that settled during the attach window.
    cur = {"rows": rows, "cols": cols}
    attach_at = time.monotonic()
    renudge: dict = {"task": None}

    def _schedule_trailing_nudge() -> None:
        # Debounce: a mobile resize burst (keyboard + address-bar animation) collapses
        # to exactly one trailing repaint after the geometry quiets. Cancelled (with the
        # whole bundle) on disconnect.
        t = renudge["task"]
        if t is not None and not t.done():
            t.cancel()

        async def _trail() -> None:
            await asyncio.sleep(_NUDGE_SETTLE_S)
            await _force_repaint(master, proc, cur["rows"], cur["cols"])

        renudge["task"] = asyncio.create_task(_trail())

    async def pump_out() -> None:
        while True:
            data = await loop.run_in_executor(None, _read, master)
            if not data:
                break
            if buf_key is not None:
                scrollback._buffer_append(buf_key, data)
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
                        new_cols = int(obj.get("cols", cols))
                        new_rows = int(obj.get("rows", rows))
                        # Drop spurious/degenerate resizes (a transient 0×0 / 1-col frame from a
                        # mobile layout or address-bar glitch). Sizing the agent pty to 0×0 while
                        # the VT mirror floors at 2×2 desyncs agent-width from mirror-feed-width →
                        # the absolute-cursor repaints don't overwrite → garbled scroll-up. That
                        # desync IS the #293/#292 garble (the repro session had a 0×0 agent pty).
                        # Ignore anything below a real terminal so the agent + mirror stay in
                        # lockstep; clamp the upper bound to the same envelope as the initial grid.
                        if new_cols < 2 or new_rows < 2:
                            continue
                        new_cols = min(500, new_cols)
                        new_rows = min(300, new_rows)
                        # A genuine WIDTH change re-renders the agent; without resetting, the ring
                        # would hold mixed-width bytes and re-garble on a later same-width reload.
                        # Reset so the ring stays single-width = the agent's current width (#245).
                        # (Height-only resizes — the common mobile address-bar case — don't change
                        # cols, so they never reset and scrollback survives them.)
                        # EXCEPT with VT on (#273): the live mirror tracks the agent's geometry and
                        # reflows on resize, so the ring is no longer the scroll-up source and must
                        # NOT be wiped — else a width change destroys delta-resume continuity.
                        ring_width_changed = bool(
                            buf_key and new_cols != scrollback._LAST_COLS.get(buf_key)
                        )
                        if ring_width_changed and not vtsidecar.enabled():
                            scrollback._reset_ring(buf_key)
                        if buf_key:
                            # VT on + width change keeps the (now mixed-width) ring →
                            # drop the persisted single-width claim (Hermes #360 r3);
                            # otherwise the ring was reset or the width is unchanged,
                            # so persisting stays truthful.
                            scrollback.note_cols(
                                buf_key,
                                new_cols,
                                persist=not (ring_width_changed and vtsidecar.enabled()),
                            )
                            scrollback._LAST_ROWS[buf_key] = new_rows
                            # Track the agent's new pty geometry on the live mirror so subsequent
                            # bytes render at the right size (repaints overwrite, no dup) (#273).
                            vtsidecar.note_resize(buf_key, new_cols, new_rows)
                        _set_winsize(master, new_rows, new_cols)
                        # TIOCSWINSZ on the master doesn't reliably deliver SIGWINCH to
                        # the dtach client here, so dtach never forwards the new size to
                        # the agent's own pty (the terminal stayed a fixed size on window
                        # resize). Nudge the dtach client directly so it re-reads the tty
                        # size and resizes the program → the live agent re-renders wider.
                        with contextlib.suppress(ProcessLookupError, OSError):
                            proc.send_signal(signal.SIGWINCH)
                        # #349: a WIDTH change inside the attach window can coalesce with
                        # the fresh-attach nudge into a net-zero geometry event the agent
                        # never repaints for. Re-arm one trailing repaint at the latest
                        # accepted geometry instead of trusting timing.
                        width_changed = new_cols != cur["cols"]
                        cur["cols"], cur["rows"] = new_cols, new_rows
                        if width_changed and time.monotonic() - attach_at <= _RENUDGE_WINDOW_S:
                            _schedule_trailing_nudge()
            elif msg.get("bytes") is not None and not _gated():
                with contextlib.suppress(OSError):
                    os.write(master, msg["bytes"])

    async def _nudge_repaint() -> None:
        # A FRESH attach needs it (#304); a have>0 reconnect normally doesn't (it holds
        # its screen, nudging would flicker) — EXCEPT when this attach delivered nothing
        # visible (`blank_attach`, #349): an idle agent must still be forced to paint or
        # the client sits on a cleared screen until its next input byte.
        if have > 0 and not blank_attach:
            return
        await asyncio.sleep(_NUDGE_SETTLE_S)
        await _force_repaint(master, proc, cur["rows"], cur["cols"])

    nudge_task = asyncio.create_task(_nudge_repaint())
    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    # A demotion (another viewer took over, #293) ends the stream alongside the pumps.
    stop_waiter = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    waiters = tasks + ([stop_waiter] if stop_waiter is not None else [])
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        nudge_task.cancel()  # short-lived; cancel in case we tore down mid-nudge
        if renudge["task"] is not None:
            renudge["task"].cancel()  # stale trailing repaint must not fire post-disconnect
        if stop_waiter is not None:
            stop_waiter.cancel()
        with contextlib.suppress(Exception):
            renudge_tasks = [renudge["task"]] if renudge["task"] is not None else []
            await asyncio.gather(*tasks, nudge_task, *renudge_tasks, return_exceptions=True)
        if stop_waiter is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stop_waiter
        with contextlib.suppress(OSError):
            os.close(master)
        # Detach (don't kill the agent): terminate our dtach client; the master persists.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        # On a demotion we leave the socket OPEN so the route can send the gate frame;
        # every other exit (client gone / agent died) closes it as before.
        if stop_event is None or not stop_event.is_set():
            with contextlib.suppress(Exception):
                await ws.close()
        # Reclaim the scrollback for a session whose dtach master has exited — there's
        # nothing left to resume. Live sessions keep their buffer (master still alive).
        scrollback._maybe_evict_ended(buf_key)
