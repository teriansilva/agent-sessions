"""In-app daily auto-update loop (#538).

Replaces the installer's systemd user timer: one background task (reaper pattern, started
from the app lifespan) that once a day — first pass a few minutes after startup — runs
``update.autoupdate()`` when the operator enabled automatic updates in Settings → System.

Gating: the persisted ``AGENT_SESSIONS_AUTOUPDATE`` env-file key, re-read on EVERY pass via
``update.auto_update_enabled()`` — the Settings toggle applies at the next wake without a
restart. There is deliberately no separate env kill-switch: the setting IS the switch
(absent/0 = off, today's default posture).

The blocking work (``git ls-remote`` + the installer spawn) runs in a worker thread
(``asyncio.to_thread``) so a slow remote never stalls the event loop; ``update``'s module
lock makes each pass single-flight with the manual "Update now" endpoint. The result is
recorded in-memory (``update.record_auto``) for the Settings card — recent runtime status,
not an audit log; it resets on restart by design.
"""

from __future__ import annotations

import asyncio
import logging

from . import update

log = logging.getLogger("agent_sessions.update_loop")

FIRST_DELAY_S = 5 * 60  # let a fresh (re)start settle before the first pass
INTERVAL_S = 24 * 60 * 60  # fixed daily cadence (#538 — deliberately not configurable)


async def sweep() -> str | None:
    """One gated pass. None when auto-update is off (never touches the network);
    otherwise the ``update.autoupdate()`` status string, recorded for the Settings
    card. Safe to call directly from tests."""
    if not update.auto_update_enabled():
        return None
    result = await asyncio.to_thread(update.autoupdate)
    update.record_auto(result)
    return result


async def run() -> None:
    """Background auto-update loop (started from the app lifespan, reaper pattern)."""
    await asyncio.sleep(FIRST_DELAY_S)
    while True:
        try:
            result = await sweep()
            if result and result != "up-to-date":
                log.info("auto-update pass: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto-update pass crashed")
        await asyncio.sleep(INTERVAL_S)
