"""The owner side of a shared TERMINAL session: its current screen as a redacted text frame
(#1246 §5, #2056).

One verb answers here, ``terminal.frame``. A terminal grant gives a peer one thing: the screen the
owner would see right now, as text. No byte stream, no escape sequence, no scrollback history, no
input, no resize. There is no cursor over raw bytes anywhere on the wire, so nothing can be split
across calls.

**The owner's terminal is never touched.** The frame is rendered from bytes this instance already
holds (``scrollback.screen_export``, which only reads). Nothing here attaches a client, sends a
signal, writes to the session or changes its geometry, and this module imports none of the
modules that could.

**Per call, in this order:** the per-call check (a TERMINAL grant, ``sharing._authorize``); the
settle decision; the render, which several watchers of one session share; redaction of the whole
frame with the values read for THIS call; the fingerprint, bound to THIS grant; the size of the
answer as the channel serializes it; and the per-call check again, as the last ``await`` before
the answer is returned.

**What is shared between calls is the unredacted grid only** (:class:`Renders`): one render per
change of a session's screen, however many peers watch it. It is never an answer and never a
redacted frame, it is keyed by the session and not by a grant, and it leaves this process only
through the steps above. So a call made while redaction is unavailable sends nothing and leaves
nothing behind that a later call could be answered from, and a render one grant's call caused is
redacted, fingerprinted and authorized again for every other call.

**Settling** narrows the window in which a value is on screen half written: a frame leaves only
when the screen has had no visible output for :data:`SETTLE_S`. It is not a guarantee, and nothing
here relies on it for secrecy: a value that is only partly on screen is not masked
(``frames.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

from .. import engines, scrollback
from . import channel as ch
from . import frames, grants, sharing

log = logging.getLogger(__name__)

#: A frame leaves when the screen has had no VISIBLE output for this long.
SETTLE_S = 0.5
#: A screen that never settles (a spinner) is exported once ONE call has waited this long on it,
#: marked ``settled: false``.
BUSY_BOUND_S = 3.0
#: Parked ``terminal.frame`` waits one peer may hold at once; the next is ``busy``.
FRAME_WAITS_PER_PEER = 4
#: How often a call parked on an unchanged screen looks for a change (dictionary reads).
POLL_S = 0.1
#: Sessions whose last render is kept. One grid is at most ``SCREEN_EXPORT_MAX_CELLS`` characters.
RENDERS_MAX = 16


class _Render:
    """The last export of one session's screen, and the lock that serializes rendering it."""

    __slots__ = ("lock", "outcome")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.outcome: scrollback.ScreenExport | scrollback.ScreenUnavailable | None = None


class Renders:
    """One render per change of a session's screen, shared by every call that watches it.

    Holds UNREDACTED grids (and refusals), keyed by the session's physical key and valid for
    exactly one ``scrollback.screen_version``. It belongs to one service, so to one event loop.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, _Render] = OrderedDict()

    async def export(self, key: str) -> scrollback.ScreenExport:
        """The session's current screen. Rendering is serialized per session: a second caller
        waits for the first one's render and reuses it when the screen did not change since.
        Raises :class:`scrollback.ScreenUnavailable`."""
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Render()
            while len(self._entries) > RENDERS_MAX:
                self._entries.popitem(last=False)
        self._entries.move_to_end(key)
        async with entry.lock:
            outcome = entry.outcome
            if outcome is None or outcome.version != scrollback.screen_version(key):
                try:
                    outcome = await asyncio.to_thread(scrollback.screen_export, key)
                except scrollback.ScreenUnavailable as exc:
                    outcome = exc
                entry.outcome = outcome
        if isinstance(outcome, scrollback.ScreenUnavailable):
            # A new exception per caller: the kept one is only a record of the refusal.
            raise scrollback.ScreenUnavailable(outcome.reason, outcome.version)
        return outcome


async def _authorize(fed, peer_id: str, args: dict):
    """THE per-call check, for a TERMINAL grant: an API grant is refused like an unknown one."""
    return await sharing._authorize(fed, peer_id, args, kind=grants.KIND_TERMINAL)


def _quiet(key: str) -> bool:
    """Whether the screen has had no visible output for :data:`SETTLE_S`. A session with no
    stamp has shown nothing since this process started."""
    at = scrollback.get_last_visible_output_at(key)
    return at is None or time.time() - at >= SETTLE_S


async def _frame(fed, grant, key: str) -> dict:
    """The redacted frame of ``key`` for ``grant``, built for THIS call. Every refusal of the
    exporter, and redaction or the keyed digest being unavailable, is ``unavailable``."""
    try:
        export = await fed.renders.export(key)
    except scrollback.ScreenUnavailable as exc:
        log.info("federation: a terminal frame was refused (%s)", exc.reason)
        raise ch.VerbError(sharing.UNAVAILABLE) from None

    def build() -> dict:
        lines = frames.redact(export.grid, export.cols)
        return {
            "fingerprint": frames.fingerprint(grant.grant_id, export.rows, export.cols, lines),
            "cols": export.cols,
            "rows": export.rows,
            "lines": lines,
        }

    return await sharing._projected(build)


async def _answer(fed, peer_id: str, grant, args: dict) -> dict:
    """The answer of one call: a frame, ``{settling: true}`` or ``{fingerprint, unchanged}``.

    The caller's ``wait`` never shortens settling. An unsettled frame leaves only when THIS call
    has watched the screen stay busy for :data:`BUSY_BOUND_S` without a break. When ``wait`` ends
    first, the answer carries no frame."""
    loop = asyncio.get_running_loop()
    key = await asyncio.to_thread(engines.physical_key, grant.session_key)
    have = args["have"]
    deadline = loop.time() + args["wait"]
    checked = loop.time()
    busy_since: float | None = None
    #: The screen version whose redacted frame is the one the caller already has.
    same: tuple | None = None
    while True:
        now = loop.time()
        quiet = _quiet(key)
        if quiet:
            busy_since = None
        elif busy_since is None:
            busy_since = now
        bound = busy_since is not None and now - busy_since >= BUSY_BOUND_S
        if (quiet or bound) and same != scrollback.screen_version(key):
            seen = scrollback.get_last_visible_output_at(key)
            version = scrollback.screen_version(key)
            frame = await _frame(fed, grant, key)
            if quiet and scrollback.get_last_visible_output_at(key) != seen:
                continue  # output arrived while the frame was built: it is not the settled one
            if have is None or frame["fingerprint"] != have:
                return {**frame, "settled": quiet}
            same = version
        if loop.time() >= deadline:
            if same is not None and same == scrollback.screen_version(key):
                return {"fingerprint": have, "unchanged": True}
            return {"settling": True}
        # Parked: until the deadline, a change, the settle moment or the busy bound.
        await asyncio.sleep(max(0.005, min(POLL_S, deadline - loop.time())))
        if loop.time() - checked >= sharing.WAIT_SLICE_S:
            checked = loop.time()
            # Ends the wait early when the grant ended. It never lets an answer out: the handler
            # runs the full check again directly before it returns one.
            granted = await asyncio.to_thread(
                grants.still_granted, fed.store, args["grant_id"], peer_id, args["revision"]
            )
            if not granted:
                raise ch.VerbError(sharing.NO_GRANT)


async def terminal_frame(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await _authorize(fed, peer_id, args)
    if args["wait"] > 0:
        if fed.frame_waits.get(peer_id, 0) >= FRAME_WAITS_PER_PEER:
            raise ch.VerbError("busy")
        fed.frame_waits[peer_id] = fed.frame_waits.get(peer_id, 0) + 1
        try:
            answer = await _answer(fed, peer_id, grant, args)
        finally:
            # Answered, refused or cancelled (the channel closed): the slot is free either way.
            fed.frame_waits[peer_id] -= 1
            if not fed.frame_waits[peer_id]:
                del fed.frame_waits[peer_id]
    else:
        answer = await _answer(fed, peer_id, grant, args)
    # Measured AFTER masking, as the channel will write it, envelope included. The exporter's
    # cell cap is not this guarantee: a grid of non-BMP characters well under it can be many
    # times the budget.
    if sharing.wire_size(answer) > sharing.BUDGET:
        raise ch.VerbError(sharing.UNAVAILABLE)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await _authorize(fed, peer_id, args)
    return answer
