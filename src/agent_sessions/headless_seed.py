"""Deliver a brief to a session with nobody watching — the injector's SECOND entry point (#739).

#732 shipped a headless launch whose brief was never delivered, because the seed injector lives
inside `webterm.run` and only runs when a browser attaches. A dispatched session therefore came up
idle and its handle expired on the 600s TTL. This module is the other entry point the issue asks
for — **not** a second injector.

**One injector, and this is not it.** `webterm._deliver_seed` owns the claim/ack protocol, the
bounded serialized write, and the abort-on-partial rule — the most failure-prone code in the repo.
Duplicating it would mean two implementations that will drift, and the one that drifts is the one
nobody is looking at. So the payload write is that function, unchanged; what lives here is the
part the viewer path gets for free and a headless launch does not: **something watching the
output**, and therefore a readiness gate.

**Where the bytes are observed.** A `dtach -n` master has no reader at all, so nothing would ever
see first paint. The app already solves this for every other headless session:
`session_stream.SessionRegistry` keeps one server-owned `dtach -a` reader per live session, which
drains into the scrollback ring and registers itself as the session's writer (`kind="headless"`).
So the gate reads the same durable signals the viewer path reads, and the write borrows the same
registered writer every other seam borrows.

**`DECSET 2004` is not readiness, and that is the whole reason this is hard.** At least one engine
arms bracketed paste in its preamble and then discards stdin through a long cold start — measured
at 91 bytes over 90 seconds on a cold codex. Gating on 2004 alone pastes the brief into the void
and reports success, which is the worst available outcome: an unseeded session that looks seeded.
The gate is therefore **armed AND painted AND quiet**, exactly as `webterm._inject_seed` computes
it, with the same constants.

**"Painted" is decided per engine (#966).** The byte rule (`FIRST_PAINT_BYTES`, 2048) was
calibrated engine-agnostically: a cold codex first-run home (~1.4 KB, not ready) fails it and a warm
one (~4 KB) passes. But claude's whole READY startup, through the production `dtach -n` + reader
path, is 1435 B at 80×24 and 1690 B at 120×40 (claude 2.1.272) — the same size as codex's not-ready
screen. No byte count separates those two, so the gate never opened for a headless claude and every
headless mission start failed "first-paint never true". Claude therefore gets a rule about its
SCREEN (`_painted_claude`, measured numbers at `CLAUDE_PAINT_MIN_ROWS`); every other engine keeps
the byte rule unchanged. The browser-attach gate in `webterm` is not touched: it counts one run's
live bytes at the browser's real size, where claude's screen does clear 2048 B.

**Fail SAFE, loudly.** On timeout nothing is written and the seed stays pending — an unseeded
session with a warning beats bytes pasted into the void. The caller then reports `failed` with a
reason rather than `running`, which is condition 5's other half: a brief that did not land is not
a dispatch that worked. The reason says what the paint rule actually saw, so "never ready" can be
told apart from "ready, but under a threshold that does not fit this engine".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import NamedTuple

from . import scrollback, session_input, vtscreen, webterm

log = logging.getLogger(__name__)

#: The gate's three signals and their thresholds are `webterm`'s, imported rather than restated.
#: Two copies of a readiness constant is two answers to "is it ready", and the interesting failure
#: is always the one where they disagree.
READY_TIMEOUT_S = webterm._SEED_READY_TIMEOUT_S
POLL_S = webterm._SEED_POLL_S
FIRST_PAINT_BYTES = webterm._SEED_FIRST_PAINT_BYTES
QUIET_S = webterm._SEED_QUIET_S
SETTLE_S = webterm._SEED_SETTLE_S

#: **Headless claude is painted when it is on the alternate screen with at least this many non-blank
#: rows drawn there**, rendered at the headless reader's size (`scrollback.reader_size`).
#:
#: Measured, not chosen (`tests/fixtures/claude_startup.PROVENANCE.md`: claude 2.1.272, real
#: `dtach -n` master + `dtach -a` reader, 80×24 and 120×40). The painted startup screen is **8–9**
#: non-blank rows at both sizes, and **every state before the paint read is 0 rows** — including a
#: real **61 B** window at 120×40 that is already bracketed-paste-armed and quiet for over a second,
#: which a gate on armed + quiet alone would have typed into. One of the painted rows (a login
#: warning) is account state, so the structural floor is 7; 4 sits well clear of both 0 and 7.
#:
#: **Fixed, not scaled by height**: claude's startup screen does not grow with the terminal — 9 then
#: 8 rows at 24 rows AND at 40. A height-scaled N would demand more rows from a taller reader that
#: draws exactly the same screen, and a tall enough one would never open.
CLAUDE_PAINT_MIN_ROWS = 4

# The alternate-screen switches. 1049 is what claude sends; 1047 and 47 are older spellings of the
# same switch that a terminal honours too, so leaving by one after entering by another is leaving.
_ALT_ENTER = (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h")
_ALT_LEAVE = (b"\x1b[?1049l", b"\x1b[?1047l", b"\x1b[?47l")
# How much of the current alt-screen visit is replayed. The rule runs on the event loop every POLL_S
# until it stamps, so the render is bounded: claude's whole startup is under 2 KB, and a full redraw
# of a large screen is well inside this.
_CLAUDE_SCREEN_BYTES = 64 * 1024


def _ring_len(key: str) -> int:
    # The ring's own length. Read through the module's registry rather than a byte counter because
    # a headless reader keeps no per-run count: `session_stream` drains into the ring, and the ring
    # is the thing that survives a reader restart.
    return len(scrollback._BUFFERS.get(key) or b"")


def _painted_by_bytes(key: str) -> bool:
    """Every engine but claude: enough ring bytes — the rule as it was before #966, unchanged.

    Read from the DURABLE observation rather than from a byte counter, because a headless reader
    has no per-run counter to keep. `note_first_paint` is stamped by whoever saw it first — this
    path or an attach — and ANY stamp counts here, so a session that painted before the dispatcher
    looked is still correctly painted.
    """
    if scrollback.first_paint_seen(key):
        return True
    try:
        n = _ring_len(key)
    except Exception:  # noqa: BLE001 — an unreadable ring is "not painted yet", never "ready"
        return False
    if n >= FIRST_PAINT_BYTES:
        scrollback.note_first_paint(key, source="bytes")
        return True
    return False


def _observed_by_bytes(key: str) -> str:
    rows, cols = scrollback.reader_size(key)
    try:
        n = _ring_len(key)
    except Exception:  # noqa: BLE001 — the reason must never be what fails
        return f"the ring could not be read at {cols}×{rows}"
    return f"{n} B of {FIRST_PAINT_BYTES} B at {cols}×{rows}"


def _claude_screen(key: str) -> tuple[bool, int, int, int]:
    """``(alt_screen_active, non_blank_rows, rows, cols)``, at the headless reader's size.

    Rows are counted from the last alt-screen ENTER onward, because `vtscreen` does not model the
    alternate screen (it skips every ``CSI ? … h/l``): text drawn on the primary screen before the
    switch would otherwise survive into the frame and be counted as claude's paint. Entering the
    alternate screen shows a blank one, which is exactly what replaying from that offset reproduces.
    Alt-screen state is read from the bytes for the same reason — the renderer cannot say.
    """
    rows, cols = scrollback.reader_size(key)
    with scrollback._RING_LOCK:
        ring = scrollback._BUFFERS.get(key)
        if not ring:
            return False, 0, rows, cols
        enter = max(ring.rfind(seq) for seq in _ALT_ENTER)
        if enter <= max(ring.rfind(seq) for seq in _ALT_LEAVE):
            return False, 0, rows, cols
        start = max(enter, len(ring) - _CLAUDE_SCREEN_BYTES)
        # A bounded cut can land inside a control string; its payload would then read as text.
        cut_inside = start > enter and vtscreen.starts_inside_control_string(ring, start)
        screen = bytes(ring[start:])
    if cut_inside:
        screen = vtscreen.drop_open_control_prefix(screen)
    frame = vtscreen.render(screen, rows, cols)
    return True, sum(1 for line in frame.split("\n") if line.strip()), rows, cols


def _painted_claude(key: str) -> bool:
    """Claude: on the alternate screen with `CLAUDE_PAINT_MIN_ROWS` rows drawn (see there).

    **A durable stamp counts only if it measured a screen.** `"attach"` (the browser path counted
    that run's own bytes at its real size) and `"screen:claude"` (this rule) do. `"bytes"` and a
    pre-#966 `"legacy"` stamp do NOT: the byte rule is exactly what cannot tell a ready claude from
    a not-ready one, so inheriting its verdict would reopen the bug through the side door. Such a
    stamp is re-evaluated from the ring instead, and upgraded when the screen rule holds.
    """
    if scrollback.first_paint_source(key) in scrollback.MEASURED_FIRST_PAINT_SOURCES:
        return True
    try:
        alt, drawn, _rows, _cols = _claude_screen(key)
    except Exception:  # noqa: BLE001 — an unreadable screen is "not painted yet", never "ready"
        return False
    if alt and drawn >= CLAUDE_PAINT_MIN_ROWS:
        scrollback.note_first_paint(key, source="screen:claude")
        return True
    return False


def _observed_claude(key: str) -> str:
    try:
        alt, drawn, rows, cols = _claude_screen(key)
    except Exception:  # noqa: BLE001 — the reason must never be what fails
        rows, cols = scrollback.reader_size(key)
        return f"the screen could not be read at {cols}×{rows}"
    state = "alt screen active" if alt else "alt screen not active"
    return f"{state}, {drawn} of {CLAUDE_PAINT_MIN_ROWS} rows at {cols}×{rows}"


class _PaintRule(NamedTuple):
    painted: Callable[[str], bool]
    #: What the rule saw, for the failure reason — declared beside the predicate so an engine can
    #: never gain a rule whose failure is described in another rule's terms.
    observed: Callable[[str], str]


_BY_BYTES = _PaintRule(_painted_by_bytes, _observed_by_bytes)

#: Engines whose "painted" is NOT the byte rule, keyed on the session key's engine prefix
#: (`claude:<uuid>`) — `_painted` is handed a key, not a provider. Anything absent: `_BY_BYTES`.
_PAINTED: dict[str, _PaintRule] = {"claude": _PaintRule(_painted_claude, _observed_claude)}


def _rule(key: str) -> _PaintRule:
    return _PAINTED.get(key.split(":", 1)[0], _BY_BYTES)


def _painted(key: str) -> bool:
    """Has the TUI actually drawn something? Per engine — see `_PAINTED`."""
    return _rule(key).painted(key)


async def wait_ready(key: str, *, timeout: float | None = None) -> tuple[bool, str]:
    """Wait for armed + painted + quiet. Returns ``(ready, why_not)``.

    ``why_not`` is returned rather than logged-and-dropped because it is what the dispatch reports
    as its failure reason, and "the brief was never delivered" is useless without which of the
    three never came true — and, for first-paint, what the rule saw instead.
    """
    deadline = time.monotonic() + (READY_TIMEOUT_S if timeout is None else timeout)
    last_epoch = -1
    last_change = time.monotonic()
    armed = painted = quiet = False
    while time.monotonic() < deadline:
        now = time.monotonic()
        # The screen epoch is the headless equivalent of the viewer path's byte counter: it moves
        # whenever the ring is mutated, so "unchanged for QUIET_S" is the same quiet.
        epoch = session_input.current_screen_epoch(key)
        if epoch != last_epoch:
            last_epoch, last_change = epoch, now
        armed = scrollback.has_mode(key, 2004)
        painted = _painted(key)
        # Only an EVEN epoch is a settled screen (the seqlock's odd interval means a mutation is
        # in flight), so a quiet reading taken mid-append is not quiet at all.
        quiet = (now - last_change) >= QUIET_S and session_input.screen_is_stable(key)
        if armed and painted and quiet:
            return True, ""
        await asyncio.sleep(POLL_S)
    missing = [
        name
        for name, ok in (("bracketed-paste", armed), ("first-paint", painted), ("quiet", quiet))
        if not ok
    ]
    reason = f"{', '.join(missing)} never true"
    if not painted:
        # e.g. "1432 B of 2048 B at 80×24" or "alt screen active, 3 of 4 rows at 80×24": a gate
        # that stays shut under a threshold that does not fit the engine must say so.
        reason += f": {_rule(key).observed(key)}"
    return False, f"the session never became ready ({reason})"


async def deliver(key: str, seed_key: str, *, timeout: float | None = None) -> tuple[bool, str]:
    """Deliver a pending seed to a headless session. Returns ``(delivered, reason)``.

    Never raises. Every failure path leaves the seed **pending** (the claim/ack protocol's
    ``retry``) or explicitly aborted, and says which — a dispatch that reports success for a brief
    that never landed is the failure this whole module exists to prevent.
    """
    ready, why = await wait_ready(key, timeout=timeout)
    if not ready:
        log.warning("headless seed for %s not injected: %s — session runs unseeded", seed_key, why)
        return False, why
    # The same final beat the viewer path takes: the gate opening is not the same as the prompt
    # being ready to accept a paste.
    await asyncio.sleep(SETTLE_S)

    try:
        borrowed = session_input.borrow_writer(key)
    except OSError as e:
        # A registration whose fd is already dead is a BROKEN owner, not an absent one, and the
        # two are worth telling apart in the reason the operator reads.
        return False, f"the session's writer is dead ({type(e).__name__})"
    if borrowed is None:
        return False, "nothing owns this session's bytes yet"
    # The fd is a DUP and its ownership passes to the pool: `_deliver_seed_owned_fd` closes it in
    # a `finally`, and `_deliver_seed_via_pool` reclaims it if the job is cancelled while still
    # queued. So nothing here closes it — a second close would land on whatever integer the kernel
    # handed out next.
    fd, lock, _token = borrowed
    try:
        # On the injector's OWN bounded pool, never the event loop and never the shared default
        # executor: `fd` is a blocking PTY fd, and a target that stops draining input must stall
        # one worker rather than every session this process serves (#678).
        delivered = await webterm._deliver_seed_via_pool(fd, seed_key, key, lock)
    except Exception as e:  # noqa: BLE001
        return False, f"the write failed ({type(e).__name__})"
    if delivered:
        return True, ""
    # `_deliver_seed` already acked (retry or abort) and logged which. From here the only honest
    # thing to say is that it did not land.
    return False, "the brief was not delivered"


def deliver_soon(key: str, seed_key: str) -> asyncio.Task:
    """Fire the delivery as a task, for a caller that has already reported its launch.

    Returned rather than detached so the caller owns its lifetime — a bare `create_task` can be
    garbage-collected mid-flight, and its failures surface as an "exception was never retrieved"
    warning nobody reads.
    """
    return asyncio.create_task(deliver(key, seed_key))
