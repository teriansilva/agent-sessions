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
import logging
import os
import select
import signal
import struct
import termios
import threading
import time  # noqa: F401 — kept so `webterm.time` stays patchable by tests
from concurrent.futures import ThreadPoolExecutor

from . import perfstats, scrollback, session_input, sessionlock, tty_health
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
    attach_modes_payload,
    clear_scrollback,
    get_last_output_at,
    live_tail_text,
    note_attach,
    scrollback_cache_stats,
)

log = logging.getLogger("agent_sessions.webterm")


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

# #443 — hold the shrunk geometry until the agent has DEMONSTRABLY rendered it (its first output
# bytes after the shrink) before restoring, so the restore is a SECOND, distinct resize the agent
# can't coalesce with the shrink inside its own SIGWINCH debounce. A fixed _NUDGE_GAP_S alone
# raced a busy agent's debounce (under load the shrink+restore merged → net-zero geometry → the
# agent rendered nothing → a blank live region while the transcript replayed). Waiting on real
# output is timing-INDEPENDENT: it works whatever the agent's debounce window is. _NUDGE_MAX_WAIT_S
# bounds the wait so a silent/wedged/exited agent (one that never repaints) still restores and the
# connection never hangs; _NUDGE_POLL_S is how often we re-check the output counter.
_NUDGE_MAX_WAIT_S = 1.0
_NUDGE_POLL_S = 0.02

# Re-nudge window (#349): a client resize landing within this many seconds of attach can
# coalesce with the fresh-attach nudge inside the agent's own resize debounce — shrink +
# client-resize + restore net out to zero geometry change and the agent never repaints
# (the mobile keyboard/address-bar blank). A WIDTH-changing resize inside this window
# therefore schedules one debounced TRAILING repaint at the latest accepted geometry,
# making the nudge un-coalesceable instead of re-tuning timing constants.
_RENUDGE_WINDOW_S = 2.0

# WebSocket heartbeat interval (#398). Abruptly closed connections (laptop lid, network drop)
# can stay "attached" on the server for 30+ minutes until TCP times out. Sending a periodic
# ping makes the server's `send` fail faster, triggering disconnect cleanup.
_HEARTBEAT_INTERVAL_S = 20.0

# Upper bound on creating the dtach client/master subprocess (#346 Phase A). Spawning is
# normally instant; under resource pressure (task-limit EAGAIN, memory stalls) it can fail
# slowly or hang, and an unbounded spawn wedges this connection's coroutine. Timeout and
# OSError both close 4502 (transient, client retries) — never 4500, which is reserved for
# non-retryable launch misconfiguration and permanently kills the client's terminal.
SPAWN_TIMEOUT_S = 15.0

# Bounded wait between SIGTERM and the SIGKILL escalation when tearing down an owned dtach
# client (#532). Module-level so tests can shrink it.
_TERMINATE_WAIT_S = 3.0

# Handoff seed injection (#597). The seed is delivered as terminal input — a bracketed
# paste written to the PTY, exactly like typed input, never argv — but ONLY once the
# freshly launched TUI is genuinely ready to read input. Readiness needs all three of:
#
#   1. bracketed paste armed (DECSET 2004, tracked by scrollback's mode scan) — the
#      agent speaks the paste protocol at all;
#   2. FIRST PAINT — at least `_SEED_FIRST_PAINT_BYTES` of output since attach, i.e. the
#      TUI has actually drawn its UI;
#   3. QUIET — no output for `_SEED_QUIET_S`, i.e. that paint finished and it's idle.
#
# (2) is the one measured empirically (#597 Phase 2): a cold `codex` arms 2004 in its
# terminal-init PREAMBLE ~0.3 s in — long before its input pipeline exists — then emits
# ~91 bytes over the next 12 s while it initialises, DISCARDING anything written to its
# stdin. Gating on 2004 alone therefore pasted the seed into the void and reported
# success; a fresh-install codex silently produced an unseeded session. A TUI that has
# rendered a full screen and gone quiet is one that has started its event loop, and that
# is engine-agnostic — no per-engine probes, no timing guess.
#
# All three are bounded by `_SEED_READY_TIMEOUT_S`; on timeout we fail SAFE (no injection,
# logged) rather than spraying bytes at a half-booted TUI.
_SEED_READY_TIMEOUT_S = 45.0
_SEED_POLL_S = 0.25
# A booted TUI's first full-screen render is kilobytes; a not-yet-ready preamble is a few
# hundred. Measured for codex (#597 Phase 2, at 120x40 with the attach SIGWINCH nudge): a
# genuinely COLD first-run home paints ~1.4 KB then stalls in first-run setup (not ready),
# while a WARM home paints ~4 KB of full UI (ready). 2 KB sits cleanly between: it delivers
# to a ready TUI and fails safe on a cold one mid-first-run-setup (the documented first-run
# limitation), rather than pasting into a TUI that is still initialising.
_SEED_FIRST_PAINT_BYTES = 2048
_SEED_QUIET_S = 1.0
_SEED_SETTLE_S = 0.75  # final beat after the gate opens, before pasting
# Delivery bound (#701 review round 3): the whole paste+CR must land within this window or
# the claim is settled (retry when nothing was written, abort after a partial write) — a
# target that keeps the PTY open but stops draining input can never hold a claim forever.
_SEED_WRITE_TIMEOUT_S = 10.0
# Per-cycle write chunk. A BLOCKING pipe/pty write never returns short — it blocks until
# every byte is accepted — so each under-lock write must be small enough that a positive
# writability check guarantees full acceptance: pipes report writable only with ≥ PIPE_BUF
# (4096) free, and the tty driver wakes/polls writers at ≥ 256 bytes of room. 256 is the
# common floor.
_SEED_WRITE_CHUNK = 256
# Deliveries run on their OWN bounded pool (round-3 P1), never the loop's shared default
# executor: pump_out's PTY reads and attach work live there, and a few wedged targets
# could otherwise occupy every shared worker and stall unrelated sessions. Worst case
# here: the two delivery workers block until their write deadline; later deliveries queue.
_SEED_MAX_DELIVERY_WORKERS = 2
_seed_pool: ThreadPoolExecutor | None = None


def _seed_executor() -> ThreadPoolExecutor:
    global _seed_pool
    if _seed_pool is None:
        _seed_pool = ThreadPoolExecutor(
            max_workers=_SEED_MAX_DELIVERY_WORKERS, thread_name_prefix="handoff-seed"
        )
    return _seed_pool


# A submit is Enter — CR (what a real terminal sends) or LF. Keystrokes that merely edit the
# input box carry neither, so this distinguishes "the user typed" from "the user sent".
_SUBMIT_BYTES = (b"\r", b"\n")


def _note_submit(buf_key: str | None, data: bytes) -> None:
    """Tell the reviewer the user submitted a line (#611). Called from the ws input path only
    once the read-only gate has passed, so a secondary tab never kicks. Best-effort and
    edge-triggered downstream — the input path must never fail on the reviewer's account."""
    if not buf_key or not data:
        return
    if any(b in data for b in _SUBMIT_BYTES):
        scrollback.note_user_submit(buf_key)


async def terminate_then_kill(proc: asyncio.subprocess.Process, *, timeout: float) -> None:
    """Escalating teardown for an owned subprocess: SIGTERM → bounded wait → SIGKILL.

    A dtach client can survive SIGTERM (#532: one was found futex-stuck with its tty fds
    already deleted, after its bridge had closed the pty). A leaked client stops reading
    its socket, and the dtach *master* — single-threaded, select()ing on client
    writability with no timeout — can then stall its broadcast loop for every other
    viewer of that session. Escalate to SIGKILL when the bounded wait expires so a
    teardown can never leak the process. Shared by the viewer bridge (`run`) and the
    headless reader (`session_stream.SessionStream.stop`) so the two paths cannot drift.
    """
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def _force_repaint(
    master: int,
    proc: asyncio.subprocess.Process,
    rows: int,
    cols: int,
    *,
    out_bytes: dict | None = None,
) -> None:
    """Force one full repaint from a winch-only-repaint agent: shrink the pty, then restore (#329).

    Mirrors the resize path: TIOCSWINSZ alone doesn't reliably reach the agent through the dtach
    client, so we also SIGWINCH it. The shrink is 2-D and held past the agent's resize-debounce so
    the pair is seen as two distinct resizes whose intermediate frame can't match the agent's
    internal model (a 1-col nudge could, leaving width-stable idle frames blank — #329).

    #443 — make the pair un-coalesceable WITHOUT guessing a timing constant. When ``out_bytes`` (the
    live agent→client byte counter, advanced by ``pump_out``) is supplied, hold the shrunk geometry
    until the agent has actually rendered it — i.e. its first bytes after the shrink arrive — before
    restoring. A fixed gap alone raced a busy agent's SIGWINCH debounce: shrink+restore merged into
    a net-zero geometry change the agent never repainted, leaving the live region blank while the
    transcript replayed. The wait is bounded by ``_NUDGE_MAX_WAIT_S`` so a silent/wedged agent still
    restores. With no ``out_bytes`` the historical fixed-gap behaviour is preserved. Best-effort
    (closed master / exited agent suppressed)."""
    nudge_rows = max(2, rows - _NUDGE_ROWS_DELTA)
    nudge_cols = max(2, cols - _NUDGE_COLS_DELTA)
    with contextlib.suppress(ProcessLookupError, OSError):
        before = out_bytes["n"] if out_bytes is not None else None
        _set_winsize(master, nudge_rows, nudge_cols)
        proc.send_signal(signal.SIGWINCH)
        # Wait for proof the agent processed the shrink (it repainted → bytes flowed), so the
        # restore below is a genuinely distinct resize event. Bounded; a silent agent falls through.
        if before is not None:
            deadline = time.monotonic() + _NUDGE_MAX_WAIT_S
            while out_bytes["n"] == before and time.monotonic() < deadline:
                await asyncio.sleep(_NUDGE_POLL_S)
        await asyncio.sleep(_NUDGE_GAP_S)
        _set_winsize(master, rows, cols)
        proc.send_signal(signal.SIGWINCH)


class _InputGate:
    """Holds owner keystrokes while the PTY raw-mode repair is in flight (#805 review r2).

    The repair runs concurrently with ``pump_in`` and it ends in ``TCSAFLUSH`` — which is
    correct for the *stale* backlog the cooked terminal accumulated, but indiscriminate about
    when those bytes arrived. A keystroke the operator types in the 150 ms confirm window is
    written into a still-canonical terminal, sits in its line buffer, and is then thrown away by
    the very flush that heals the session. The operator sees their first keystroke after
    attaching vanish.

    So input written while the probe runs is queued rather than dropped, and replayed once the
    terminal can actually receive it. The distinction the flush cannot make — *typed before the
    repair* (stale, discard) versus *typed during it* (intentional, keep) — is one this gate can,
    because it sits on the only path owner bytes take.
    """

    __slots__ = ("_held", "_queue")

    def __init__(self, held: bool) -> None:
        self._held = held
        self._queue: list[bytes] = []

    @property
    def held(self) -> bool:
        return self._held

    def hold(self, data: bytes) -> bool:
        """Queue ``data`` and return True, or return False if the caller should write it now."""
        if not self._held:
            return False
        self._queue.append(data)
        return True

    def release(self) -> list[bytes]:
        """Open the gate; hand back everything queued, in arrival order. Idempotent."""
        self._held = False
        queued, self._queue = self._queue, []
        return queued


async def _repair_tty(buf_key: str | None) -> None:
    """Heal a PTY stuck out of raw mode before this viewer types into it (#804).

    Cooked, the line discipline eats every keystroke and the operator watches ``^[[B`` pile up
    while the agent hears nothing — so the check belongs exactly here, at the attach.

    **The whole probe goes through ``to_thread``, not just its reads.** ``webterm.run`` is an
    event-loop coroutine serving every websocket in the process, and ``tty_health.ensure_raw``
    walks ``/proc`` and sleeps for its confirm read. Calling it inline would stall every session
    this process serves for the duration — the #678 treadmill, in a new place. Pinned by
    ``test_webterm.py::test_attach_tty_repair_never_blocks_the_event_loop``.

    Best-effort in the strongest sense: a health check must never be able to fail an attach, so
    every exception is swallowed and the viewer connects regardless.
    """
    if not buf_key:
        return
    with contextlib.suppress(Exception):
        await asyncio.to_thread(tty_health.ensure_raw, buf_key)


def _nudge_plan(have: int, blank_attach: bool) -> float | None:
    """Pre-nudge settle (seconds) for the fresh-attach repaint, or ``None`` to skip nudging.

    #652 T-P1 — cut the blank-screen window on connect/reconnect for alt-screen agents
    (claude/opencode), whose fresh attach delivers an EMPTY payload so the forced SIGWINCH repaint
    is the ONLY thing that fills the screen (the client's own connect-time resize is usually the
    SAME size the URL already carried → a no-op the TUI ignores).

    - ``have > 0`` and NOT blank: a live continuation holds its own screen — nudging would flicker
      it (#304), so skip entirely (``None``).
    - ``have <= 0`` (fresh page load / launch): the screen is blank and nothing else will paint it,
      so fire the repaint IMMEDIATELY — ``0.0`` settle. The #443 ``out_bytes`` wait inside
      ``_force_repaint`` still holds the shrink until the agent has rendered it, so the shrink→
      restore pair stays un-coalesceable WITHOUT the pre-settle. (Was a fixed ``_NUDGE_SETTLE_S``,
      which added ~0.3 s of dead blank before every fresh attach.)
    - ``have > 0`` but blank (a reconnect that delivered nothing visible, #349): keep the small
      ``_NUDGE_SETTLE_S`` — its screen may still be mid-update from the continuation payload, so let
      it quiesce before the shrink.
    """
    if have > 0 and not blank_attach:
        return None
    return 0.0 if have <= 0 else _NUDGE_SETTLE_S


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:  # master closed / child gone
        return b""


def _deliver_seed(
    fd: int,
    seed_key: str,
    buf_key: str | None,
    write_lock: threading.Lock | None = None,
) -> bool:
    """Claim + deliver the handoff seed, then acknowledge — the claim/ack protocol (#597
    review round 2). Runs in a WORKER THREAD, never on the event loop: ``fd`` is a blocking
    PTY fd, and a target that stops draining input must stall only this thread — a blocking
    write on the loop would freeze every session this process serves (the #678 failure
    mode). The protocol keeps the delivery invariant honest across every failure mode:

    - the claim serializes claimants (a second viewer gets ``None`` while one is in flight);
    - nothing written yet (dead fd / write-timeout before the first byte) → ack ``retry``:
      the seed stays pending for the next attach;
    - PARTIAL write (error or timeout mid-payload) → ack ``abort`` + log: an unterminated
      bracketed paste already reached the TUI, so a blind replay would corrupt the prompt —
      consume it explicitly rather than silently half-lose it;
    - full ``paste + CR`` written → ack ``delivered``: consumed exactly once.

    The write itself is BOUNDED (rounds 3+4 P1): ``select()`` writability alone is only a
    snapshot — ``pump_in`` writes the same PTY, and an unserialized competitor could
    consume the window between the select and a blocking write, wedging this worker past
    its deadline. So every writer to this PTY is SERIALIZED through the bridge's
    ``write_lock``: the (cheap) wait-for-writability select runs outside the lock, then
    writability is re-checked under the lock — with the only other writer excluded, a
    positive check cannot be consumed, and a PTY/pipe write with confirmed room accepts
    at least one byte without blocking. The lock acquire itself is deadline-bounded, so a
    target that stops draining input times out and settles the claim instead of pinning
    this worker forever. (A pty-master reopen via /proc/self/fd is NOT an option here —
    reopening /dev/ptmx mints a brand-new pty, so O_NONBLOCK-on-own-description is out.)

    Returns True when the seed was fully delivered."""
    from . import handoff  # late import: webterm is imported by handoff's route layer

    seed = handoff.claim_seed(seed_key)
    if seed is None:
        return False  # consumed or claimed by another viewer — single delivery held
    lock = write_lock if write_lock is not None else threading.Lock()
    data = b"\x1b[200~" + seed.encode("utf-8", "replace") + b"\x1b[201~\r"
    written = 0
    failure = None  # "error" | "timeout"
    deadline = time.monotonic() + _SEED_WRITE_TIMEOUT_S
    view = memoryview(data)
    while written < len(data):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = "timeout"
            break
        try:
            # Cheap writability wait OUTSIDE the lock (never holds up pump_in while idle)…
            select.select([], [fd], [], min(0.5, remaining))
            if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                failure = "timeout"
                break
            try:
                # …then the authoritative re-check UNDER the lock: the only other writer
                # is excluded, so confirmed room can't vanish before our write. The chunk
                # is capped at the writability-guarantee floor — a blocking write never
                # returns short, so a larger write could still block past the deadline.
                _, writable, _ = select.select([], [fd], [], 0)
                if not writable:
                    continue
                written += os.write(fd, view[written : written + _SEED_WRITE_CHUNK])
            finally:
                lock.release()
        except OSError:
            failure = "error"
            break
    if failure is not None:
        if written == 0:
            handoff.ack_seed(seed_key, "retry")  # clean failure — next attach retries
        else:
            handoff.ack_seed(seed_key, "abort")
            log.warning(
                "handoff seed for %s aborted after a partial PTY write (%s, %d/%d bytes)",
                seed_key,
                failure,
                written,
                len(data),
            )
        return False
    handoff.ack_seed(seed_key, "delivered")
    _note_submit(buf_key, b"\r")
    return True


def _deliver_seed_owned_fd(
    fd: int,
    seed_key: str,
    buf_key: str | None,
    write_lock: threading.Lock | None = None,
) -> bool:
    """`_deliver_seed` wrapper that OWNS ``fd`` (a dup of the bridge's pty master) and
    closes it when done — the delivery thread's fd lifetime is decoupled from the bridge
    teardown, so a viewer disconnect mid-write can't yank the fd out from under it."""
    try:
        return _deliver_seed(fd, seed_key, buf_key, write_lock)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


async def _deliver_seed_via_pool(
    fd: int,
    seed_key: str,
    buf_key: str | None,
    write_lock: threading.Lock | None = None,
) -> bool:
    """Run the owned-fd delivery on the dedicated pool, reclaiming ownership if the job is
    cancelled while still QUEUED (#701 review round 4 P2): a saturated pool means the work
    item may never start, so its ``finally``-close never runs — without this reclaim every
    attach/disconnect cycle under saturation would leak one dup'd master fd. A job that
    already STARTED cannot be cancelled (concurrent.futures semantics) and closes the fd
    itself. Returns True when the seed was fully delivered."""
    cf = _seed_executor().submit(_deliver_seed_owned_fd, fd, seed_key, buf_key, write_lock)
    try:
        return await asyncio.wrap_future(cf)
    except asyncio.CancelledError:
        if cf.cancelled() or cf.cancel():  # never ran → the fd is still ours to close
            with contextlib.suppress(OSError):
                os.close(fd)
        raise


async def run(
    ws,
    argv: list[str],
    *,
    cwd: str,
    buf_key: str | None = None,
    transcript_key: str | None = None,
    cols: int = 80,
    rows: int = 24,
    lock: sessionlock.SessionLock | None = None,
    have: int = 0,
    read_only_gate: asyncio.Event | None = None,
    stop_event: asyncio.Event | None = None,
    seed_key: str | None = None,
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

    ``buf_key`` is the physical runtime key (dtach/lock/scrollback ring). ``transcript_key``
    is the logical session key for saved transcript replay; alias-backed Codex sessions need
    these to differ because the runtime placeholder is not the rollout UUID.

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

    ``seed_key`` (cross-engine handoff, #597): the engine-qualified key a pending
    handoff seed is bound to. The injector task waits for the TUI to arm bracketed
    paste, then REDEEMS the seed from :mod:`handoff` at write time — the atomic
    single-redemption there (not this task) is what guarantees a reconnect or a
    second viewer can never paste it twice. The seed goes to the PTY as input,
    never argv.
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
    # Every writer to this master is serialized (#701 round 4 P1): pump_in and the seed
    # delivery worker share this lock, so delivery's under-lock writability check can't be
    # invalidated by a concurrent keystroke write between its select and its write.
    write_lock = threading.Lock()

    # This fresh dtach client will trigger a screen replay; don't let that burst flip the
    # working dot (#195). Genuine output after the grace window stamps normally.
    if buf_key:
        scrollback.note_attach(buf_key)
        # Pin this session's ring for the sweep (#678): an attached viewer's scrollback is
        # never a cap-eviction victim (delta-resume needs it); released in the finally below.
        scrollback.note_viewer_attached(buf_key)
        # Track the agent's launch geometry so the headless SessionStream reader can size its pty
        # to the session's last-known rows on a detached attach (session_stream reads _LAST_ROWS).
        scrollback._LAST_ROWS[buf_key] = rows

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
        # #652 measurement probe: cost of building the replay/redraw payload — the raw-ring
        # copy/scan (`_resume_payload`) or the width-correct transcript render
        # (`_transcript_payload`). This is what T4/T5 (and the ring maintenance) move.
        _build_at = time.monotonic()
        payload, total = scrollback._resume_payload(buf_key, have)
        # Scroll-up source (#262). The raw ring is authored at the agent's fixed pty width; replay
        # at a DIFFERENT client width mis-positions its absolute cursor moves and garbles. So replay
        # it ONLY for a genuine same-width continuation — a brief WS blip where the client still
        # holds the matching-width scrollback and only needs the byte-delta. EVERY other case
        # re-renders the engine's SAVED TRANSCRIPT (#242) at this client's width — clean,
        # width-correct — and falls back to a clean-load clear when there's no adapter:
        #   • fresh page load (have<=0) — client xterm is empty;
        #   • cross-width client — the ring's width ≠ this client's;
        #   • ahead-of-ring reconnect (have > total, #484) — an app restart rehydrates the ring
        #     head-trimmed to _MAX_BUF while restoring the authored width, so a SAME-width reconnect
        #     can carry a pre-restart `have` that now exceeds the smaller `total`. The client holds
        #     MORE than the ring does, so this is NOT a continuation: it lands here for the width-
        #     correct transcript / clean-load clear instead — replaying the ring UNDER the client's
        #     stale scrollback would render the whole conversation twice;
        #   • post-restart SAME-width reconnect with have <= total — the width sidecar (#348)
        #     restores the ring's authored width on rehydrate, so THIS is a continuation and replays
        #     the ring delta. Only a cross-width client, an ahead-of-ring have>total (above), or a
        #     missing sidecar from a pre-#348 mirror (⇒ buffer_cols None) lands here for the width-
        #     correct transcript instead (#206).
        # `total` is unchanged in every branch, so the `seq` frame below stays the real byte offset
        # and delta-resume is unaffected.
        # Authored width of the replayable ring (None when unknown/mixed) — the raw
        # continuation contract, NOT merely the last client width (Hermes #360 r4).
        buffer_cols = scrollback.ring_cols(buf_key)
        if scrollback._is_same_width_continuation(have, total, buffer_cols, cols):
            # Same-width continuation: the client already holds a valid screen and the
            # (possibly empty) payload is just the byte delta — an empty delta means
            # "up to date", NOT blank. Never nudge it (Hermes #359: the #304 no-flicker
            # reconnect must survive the blank-attach rule).
            blank_attach = False
        if not scrollback._is_same_width_continuation(have, total, buffer_cols, cols):
            # Not a same-width continuation: serve the engine's saved transcript scroll-up, else a
            # clean-load clear. Synthetic: `total` is unchanged so the `seq` frame + delta-resume
            # stay unaffected. The transcript scroll-up ends at "now" → the live replay below
            # duplicates the tail; `synthetic` marks payloads that need the boundary rule.
            synthetic = False
            # Runtime resources for reconciled mint-own-id engines live under their placeholder
            # key (dtach socket / lock / ring), but transcripts live under the REAL engine id.
            # Use the logical key for transcript replay so alias-backed Codex sessions do not
            # attach with an empty clean-load screen.
            tres = await loop.run_in_executor(
                None, scrollback._transcript_payload, transcript_key or buf_key, cols, rows
            )
            if tres is not None:
                payload, hist_cursor = tres
                synthetic = True
            else:
                # No transcript → clean-load: clear on a width mismatch (no garbled cross-width
                # replay), reset the ring so a later same-width attach can't replay stale bytes.
                clear = scrollback._clean_load_payload(total, cols, buffer_cols)
                if clear is not None:
                    scrollback._reset_ring(buf_key)
                    payload = clear
                    payload_is_clear = True
        perfstats.record("attach_payload_build_ms", (time.monotonic() - _build_at) * 1000.0)
        # Track this client's width for the clean-load fallback / resize logic (every
        # connect) — attach-aware so the persisted sidecar only ever claims a width the
        # retained ring was actually authored at (Hermes #360 round 3).
        scrollback.note_attach_width(buf_key, cols)
        # Re-emit the agent's active private modes (#397) BEFORE the scroll-up payload, so this
        # fresh xterm re-learns the mouse-reporting / alternate-scroll / bracketed-paste modes
        # the agent set ONCE at startup — long gone from the stream, and absent from an
        # alt-screen session's empty payload (it repaints CONTENT via SIGWINCH but never
        # re-emits its mode setup). Independent of the scroll-up content decision so it survives
        # every branch (empty/continuation/transcript/clean-load); idempotent for a same-width
        # continuation whose xterm already holds them. `total`/`seq` are untouched — these bytes
        # are synthetic, like the transcript scroll-up, so delta-resume offsets stay correct.
        mode_prefix = scrollback.attach_modes_payload(buf_key)
        if mode_prefix:
            with contextlib.suppress(Exception):
                await ws.send_bytes(mode_prefix)
        if payload and not payload_is_clear and locals().get("synthetic"):
            # Mark the boundary between the synthetic scroll-up above and the live dtach
            # replay below (same idiom as the client-side seams): both representations
            # end at "now", so the current screen appears twice — unlabelled, that reads
            # as corruption (operator: "still a mess"). `synthetic` exists only when the
            # non-continuation branch ran; same-width raw continuations are byte-accurate
            # (nothing duplicated) and stay unmarked.
            _label = " live screen ↓ "
            _fill = max(4, cols - len(_label))
            payload += (
                b"\r\n\x1b[38;5;240m"
                + ("─" * (_fill // 2) + _label + "─" * (_fill - _fill // 2)).encode()
                + b"\x1b[0m\r\n"
            )
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
    # #443: live agent→client bytes since attach. _force_repaint watches this to hold its shrink
    # until the agent has rendered it, making the shrink→restore pair un-coalesceable (no timing
    # guess). Only pump_out (genuine agent output) advances it — the replay payload below is sent
    # directly, so an idle agent's counter stays put until the shrink actually forces a repaint.
    out_bytes = {"n": 0}

    # Owner-input hold during handoff seed delivery (#703 review follow-up). While a seed is
    # pending injection, owner keystrokes are QUEUED rather than written, so the seed is
    # guaranteed to be the target's first prompt and no user bytes can split the
    # bracketed-paste frame (pump_in and the delivery worker share `write_lock`, but the
    # per-chunk release meant a keystroke could still land mid-paste). The queue is flushed
    # after the seed lands; if the seed is NOT delivered this run (TUI never ready, or the
    # viewer dropped), the queued bytes are discarded — the TUI wasn't accepting input
    # anyway, and the still-pending seed must stay first for the next attach.
    seed_hold = {"active": seed_key is not None}
    seed_queue: list[bytes] = []

    # Owner input is gated until the PTY raw-mode probe finishes (#805 r2): the probe ends in
    # TCSAFLUSH, which would otherwise swallow the very keystrokes the operator typed while it
    # ran. Held only when there is something to repair (a buf_key); released in _nudge_repaint's
    # finally, so a cancelled or failed probe can never strand the operator's bytes.
    input_gate = _InputGate(bool(buf_key))

    def _release_seed_hold(*, delivered: bool) -> None:
        if not seed_hold["active"]:
            return
        seed_hold["active"] = False
        queued = b"".join(seed_queue)
        seed_queue.clear()
        if delivered and queued:
            with contextlib.suppress(OSError), write_lock:
                os.write(master, queued)
            _note_submit(buf_key, queued)

    def _write_owner_input(data: bytes) -> None:
        # Queue while the PTY repair is in flight, then while a seed is pending; otherwise write
        # straight through under the lock.
        if input_gate.hold(data):
            return
        if seed_hold["active"]:
            seed_queue.append(data)
            return
        with contextlib.suppress(OSError), write_lock:
            os.write(master, data)
        _note_submit(buf_key, data)

    def _schedule_trailing_nudge() -> None:
        # Debounce: a mobile resize burst (keyboard + address-bar animation) collapses
        # to exactly one trailing repaint after the geometry quiets. Cancelled (with the
        # whole bundle) on disconnect.
        t = renudge["task"]
        if t is not None and not t.done():
            t.cancel()

        async def _trail() -> None:
            await asyncio.sleep(_NUDGE_SETTLE_S)
            await _force_repaint(master, proc, cur["rows"], cur["cols"], out_bytes=out_bytes)

        renudge["task"] = asyncio.create_task(_trail())

    async def pump_out() -> None:
        while True:
            data = await loop.run_in_executor(None, _read, master)
            if not data:
                break
            out_bytes["n"] += len(data)  # #443: proof-of-repaint signal for _force_repaint
            # #678 probe: the per-chunk event-loop cost (sanitize + ring append + WS send).
            _chunk_at = time.monotonic()
            if buf_key is not None:
                data = scrollback.sanitize_live_output(buf_key, data)
                if not data:
                    continue
            if buf_key is not None:
                # Scrollback bookkeeping is BEST-EFFORT and must never tear down a live viewer:
                # the bytes reach the client via `send_bytes` below regardless. A ring/registry
                # error here used to propagate out of `pump_out`, complete the bridge's
                # `asyncio.wait`, and collapse the whole connection — surfacing to the user as a
                # spurious black-screen-then-reconnect. Swallow + log instead of disconnecting.
                try:
                    scrollback._buffer_append(buf_key, data)
                except Exception:
                    log.exception("scrollback append failed for %s; continuing", buf_key)
            await ws.send_bytes(data)  # awaited → natural backpressure
            perfstats.record("pump_chunk_ms", (time.monotonic() - _chunk_at) * 1000.0)

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
                    data = obj.get("d", "").encode("utf-8", "replace")
                    _write_owner_input(data)
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
                        ring_width_changed = bool(
                            buf_key and new_cols != scrollback._LAST_COLS.get(buf_key)
                        )
                        if ring_width_changed:
                            scrollback._reset_ring(buf_key)
                        if buf_key:
                            # The ring was reset (or the width is unchanged), so the persisted
                            # single-width claim stays truthful.
                            scrollback.note_cols(buf_key, new_cols)
                            scrollback._LAST_ROWS[buf_key] = new_rows
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
                _write_owner_input(msg["bytes"])

    async def _nudge_repaint() -> None:
        # A viewer is about to type, so this is the moment a PTY stuck out of raw mode has to be
        # caught (#804). Runs BEFORE the `_nudge_plan` early-return: a session that needs no
        # repaint can still need its terminal back. Owner input is gated across the probe and
        # replayed after it (#805 r2) — the repair's TCSAFLUSH must discard the cooked terminal's
        # stale backlog, never the keystrokes typed while we were fixing it.
        try:
            await _repair_tty(buf_key)
        finally:
            for chunk in input_gate.release():
                _write_owner_input(chunk)
        # When and how long to wait before the forced repaint (#304/#349/#443, and #652 T-P1:
        # fire immediately for a fresh attach) is decided by `_nudge_plan` — see its docstring.
        settle = _nudge_plan(have, blank_attach)
        if settle is None:
            return
        if settle:
            await asyncio.sleep(settle)
        await _force_repaint(master, proc, cur["rows"], cur["cols"], out_bytes=out_bytes)

    async def heartbeat() -> None:
        """Periodically send a ping frame to detect dead connections (#398)."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            # Sending a small JSON frame. Unknown types are ignored by the client.
            await ws.send_text(json.dumps({"t": "p"}))

    async def _inject_seed() -> None:
        # Handoff seed delivery (#597). Readiness = bracketed paste armed AND the TUI has
        # actually painted AND that paint has gone quiet — see the _SEED_* block above for
        # why 2004 alone is not readiness (a cold codex arms it in its preamble and then
        # discards stdin for many seconds). Fail-safe on timeout: an unseeded session with
        # a loud log beats bytes pasted into the void and reported as delivered. Every
        # await sits BEFORE the claim, so a viewer that drops leaves the seed pending.
        #
        # First-paint is DURABLE (PR #703 review round 4): `out_bytes` counts only this
        # `run()`'s live bytes — a reconnect REPLAYS the ring rather than re-emitting it, so
        # a seed left pending because the first viewer dropped before the quiet window would
        # otherwise see `painted == False` forever. We persist the first-paint observation
        # (`scrollback.note_first_paint`) the moment we see it, and honour a prior
        # observation (`first_paint_seen`) on the next attach, so the documented
        # "delivered on the next attach" path actually succeeds for an already-idle TUI.
        # Owner input is HELD (queued) for the whole readiness+delivery window and released
        # in the finally: flushed after a successful delivery (so the seed was first, then
        # the user's queued keystrokes), discarded otherwise (a not-ready TUI wasn't taking
        # input, and a still-pending seed must stay first for the next attach). #703 review.
        delivered = False
        try:
            deadline = time.monotonic() + _SEED_READY_TIMEOUT_S
            last_n, last_change = -1, time.monotonic()
            ready = False
            while time.monotonic() < deadline:
                now = time.monotonic()
                n = out_bytes["n"]
                if n != last_n:
                    last_n, last_change = n, now
                armed = bool(buf_key) and scrollback.has_mode(buf_key, 2004)
                if buf_key and n >= _SEED_FIRST_PAINT_BYTES:
                    scrollback.note_first_paint(buf_key)  # durable across the next attach
                painted = (n >= _SEED_FIRST_PAINT_BYTES) or (
                    bool(buf_key) and scrollback.first_paint_seen(buf_key)
                )
                quiet = (now - last_change) >= _SEED_QUIET_S
                if armed and painted and quiet:
                    ready = True
                    break
                log.debug(
                    "handoff readiness %s: armed=%s painted=%s quiet=%s (n=%d, idle=%.2fs)",
                    seed_key,
                    armed,
                    painted,
                    quiet,
                    n,
                    now - last_change,
                )
                await asyncio.sleep(_SEED_POLL_S)
            if not ready:
                log.warning(
                    "handoff seed for %s not injected: TUI never became ready "
                    "(bracketed-paste armed=%s, output=%dB of %dB first-paint, prior-paint=%s) "
                    "— session runs unseeded",
                    seed_key,
                    bool(buf_key) and scrollback.has_mode(buf_key, 2004),
                    out_bytes["n"],
                    _SEED_FIRST_PAINT_BYTES,
                    bool(buf_key) and scrollback.first_paint_seen(buf_key),
                )
                return
            await asyncio.sleep(_SEED_SETTLE_S)
            if not seed_key:
                return
            # Delivery runs on the DEDICATED bounded pool on a dup'd fd (review rounds 2-4):
            # never the event loop, never the loop's shared default executor (pump_out and
            # attach work live there), non-cancellable once RUNNING (asyncio cancellation
            # interrupts the await, not the thread — the claim is always acked, and the write
            # has a hard deadline), and ownership-safe when cancelled while still QUEUED
            # (the dup is reclaimed — see _deliver_seed_via_pool).
            try:
                fd = os.dup(master)
            except OSError:
                return
            with contextlib.suppress(asyncio.CancelledError):
                delivered = await _deliver_seed_via_pool(fd, seed_key, buf_key, write_lock)
        finally:
            _release_seed_hold(delivered=bool(delivered))

    # Declare this bridge the current byte-owner for the session (#726), sharing the SAME
    # `write_lock` pump_in and the seed injector already serialise on — so the orchestrator's
    # send_input seam is a third participant in one lock, not a second lock nobody knows about.
    # Registered only while a viewer actually owns the bytes; released in the finally below,
    # after which the registry's headless SessionStream re-registers itself.
    input_writer_token = (
        session_input.register_writer(buf_key, master, write_lock, "attached") if buf_key else None
    )

    nudge_task = asyncio.create_task(_nudge_repaint())
    seed_task = asyncio.create_task(_inject_seed()) if seed_key else None
    tasks = [
        asyncio.create_task(pump_out()),
        asyncio.create_task(pump_in()),
        asyncio.create_task(heartbeat()),
    ]
    # A demotion (another viewer took over, #293) ends the stream alongside the pumps.
    stop_waiter = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    waiters = tasks + ([stop_waiter] if stop_waiter is not None else [])
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # FIRST: stop advertising this fd. A write racing teardown must find no writer rather
        # than a master that is about to be closed under it.
        if input_writer_token is not None and buf_key:
            session_input.unregister_writer(buf_key, input_writer_token)
        for t in tasks:
            t.cancel()
        nudge_task.cancel()  # short-lived; cancel in case we tore down mid-nudge
        if seed_task is not None:
            # A viewer dropping pre-injection leaves the seed unredeemed for the next attach.
            seed_task.cancel()
        if renudge["task"] is not None:
            renudge["task"].cancel()  # stale trailing repaint must not fire post-disconnect
        if stop_waiter is not None:
            stop_waiter.cancel()

        # Re-ordered cleanup (#398): close the master BEFORE gathering tasks.
        # pump_out is blocked on a read(master) in an executor; it won't check
        # its cancellation until that read returns. Closing the master here
        # makes the read return immediately (b''), letting the task finish
        # and be gathered without hanging for 30+ minutes on a silent session.
        with contextlib.suppress(OSError):
            os.close(master)

        with contextlib.suppress(Exception):
            renudge_tasks = [renudge["task"]] if renudge["task"] is not None else []
            seed_tasks = [seed_task] if seed_task is not None else []
            await asyncio.gather(
                *tasks, nudge_task, *renudge_tasks, *seed_tasks, return_exceptions=True
            )
        if stop_waiter is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stop_waiter
        # Detach (don't kill the agent): terminate our dtach client; the master persists.
        # Escalate to SIGKILL if the client survives SIGTERM (#532) — a leaked client
        # wedges the dtach master's broadcast loop for every viewer of this session.
        await terminate_then_kill(proc, timeout=_TERMINATE_WAIT_S)
        # On a demotion we leave the socket OPEN so the route can send the gate frame;
        # every other exit (client gone / agent died) closes it as before.
        if stop_event is None or not stop_event.is_set():
            with contextlib.suppress(Exception):
                await ws.close()
        # Unpin (#678): the post-detach grace keeps the ring safe from the cap sweep long
        # enough for a transient-drop reconnect to delta-resume.
        if buf_key:
            scrollback.note_viewer_detached(buf_key)
        # Reclaim the scrollback for a session whose dtach master has exited — there's
        # nothing left to resume. Live sessions keep their buffer (master still alive).
        scrollback._maybe_evict_ended(buf_key)
