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

**Fail SAFE, loudly.** On timeout nothing is written and the seed stays pending — an unseeded
session with a warning beats bytes pasted into the void. The caller then reports `failed` with a
reason rather than `running`, which is condition 5's other half: a brief that did not land is not
a dispatch that worked.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import scrollback, session_input, webterm

log = logging.getLogger(__name__)

#: The gate's three signals and their thresholds are `webterm`'s, imported rather than restated.
#: Two copies of a readiness constant is two answers to "is it ready", and the interesting failure
#: is always the one where they disagree.
READY_TIMEOUT_S = webterm._SEED_READY_TIMEOUT_S
POLL_S = webterm._SEED_POLL_S
FIRST_PAINT_BYTES = webterm._SEED_FIRST_PAINT_BYTES
QUIET_S = webterm._SEED_QUIET_S
SETTLE_S = webterm._SEED_SETTLE_S


def _painted(key: str) -> bool:
    """Has the TUI actually drawn something?

    Read from the DURABLE observation rather than from a byte counter, because a headless reader
    has no per-run counter to keep: `session_stream` drains into the ring, and the ring is what
    survives. `note_first_paint` is stamped by whoever saw it first — this path or an attach — so
    a session that painted before the dispatcher looked is still correctly painted.
    """
    if scrollback.first_paint_seen(key):
        return True
    try:
        # The ring's own length. Read through the module's registry rather than a byte counter
        # because a headless reader keeps no per-run count: `session_stream` drains into the ring,
        # and the ring is the thing that survives a reader restart.
        n = len(scrollback._BUFFERS.get(key) or b"")
    except Exception:  # noqa: BLE001 — an unreadable ring is "not painted yet", never "ready"
        return False
    if n >= FIRST_PAINT_BYTES:
        scrollback.note_first_paint(key)
        return True
    return False


async def wait_ready(key: str, *, timeout: float | None = None) -> tuple[bool, str]:
    """Wait for armed + painted + quiet. Returns ``(ready, why_not)``.

    ``why_not`` is returned rather than logged-and-dropped because it is what the dispatch reports
    as its failure reason, and "the brief was never delivered" is useless without which of the
    three never came true.
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
    return False, f"the session never became ready ({', '.join(missing)} never true)"


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
