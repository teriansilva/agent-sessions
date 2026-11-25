"""Keeps native API clients' model lists warm (#1503).

Shortly after startup, every admitted API client whose source CLI is available is queued for one
model probe, and from then on each is re-queued just before its list passes `TTL_S`. So the New
session page finds a list on its first visit after a restart (once the warm-up reached that
client), and a list request almost never even sees a stale one.

Nothing here probes. It only queues: `native_models` owns the single background refresher, which
probes one client at a time on its own thread — never on the event loop, never two at once, and
never one already queued or running. The page triggers the same probes; this moves them earlier.

Kill switch: ``AGENT_SESSIONS_MODELS_LOOP=0`` — the task exits at startup and queues nothing. A
list is then read on demand, as before.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import native_models

log = logging.getLogger("agent_sessions.models_loop")

#: Lets the app finish starting before the first CLI is spawned.
INITIAL_DELAY_S = 5.0
#: How often the loop looks for work: a list about to expire (re-queued this far ahead of `TTL_S`,
#: so it is renewed before it goes stale) or a failed probe whose `FAILURE_TTL_S` has passed — so a
#: CLI that failed at startup and then recovered is retried within one failure TTL, even with
#: nobody on the page (#1503 PR review).
INTERVAL_S = native_models.FAILURE_TTL_S


def loop_enabled() -> bool:
    """Env kill-switch. ``AGENT_SESSIONS_MODELS_LOOP=0`` stops the warm-up entirely."""
    return (os.environ.get("AGENT_SESSIONS_MODELS_LOOP", "1") or "1") != "0"


def clients() -> list:
    """The API clients worth warming: admitted, not retiring, and able to start now — the answer
    the new-session picker shows. An unavailable source CLI is skipped, never probed."""
    from .engines import registry

    out = []
    for prov in registry.all_providers():
        manifest = getattr(prov, "manifest", None)
        if manifest is None or manifest.runtime != "api" or registry.is_retiring(prov):
            continue
        if registry.api_unavailable_reason(prov) is not None:
            continue
        out.append(prov)
    return out


def warm_once() -> list[str]:
    """Queue every client whose list is missing or about to expire. Blocking (it resolves
    binaries): call it from a thread. Returns the engine ids queued."""
    queued = []
    for prov in clients():
        try:
            if native_models.schedule(prov, ahead_s=INTERVAL_S):
                queued.append(prov.engine_id)
        except Exception:  # noqa: BLE001 — one client never stops the others' warm-up
            log.exception("could not queue a model probe for %s", prov.engine_id)
    return queued


async def run() -> None:
    """Background warm-up + refresh (started from the app lifespan, reaper pattern)."""
    if not loop_enabled():
        log.info("model list warm-up disabled (AGENT_SESSIONS_MODELS_LOOP=0)")
        return
    await asyncio.sleep(INITIAL_DELAY_S)
    while True:
        try:
            await asyncio.to_thread(warm_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("model list warm-up pass crashed")
        await asyncio.sleep(INTERVAL_S)
