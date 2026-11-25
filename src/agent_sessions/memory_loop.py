"""Shared project memory ingestion loop (#1441).

Every `INTERVAL_S` it runs one `memory.ingest_all` pass off the event loop: inbox files of every
project with memory on move into that project's store (or quarantine, or are refused). The page
also triggers a pass when it opens, so the interval only bounds how stale an unwatched store is.

Env kill-switch ``AGENT_SESSIONS_MEMORY_LOOP=0`` keeps the task from ever ingesting. A crashed
pass is logged and the next one waits longer (backoff), never the loop dying.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import memory

log = logging.getLogger("agent_sessions.memory_loop")

INTERVAL_S = 30.0
_BACKOFF_MAX_MULT = 8


def loop_enabled() -> bool:
    return (os.environ.get("AGENT_SESSIONS_MEMORY_LOOP", "1") or "1") != "0"


async def run() -> None:
    if not loop_enabled():
        log.info("project memory loop disabled (AGENT_SESSIONS_MEMORY_LOOP=0)")
        return
    consecutive_failures = 0
    while True:
        await asyncio.sleep(INTERVAL_S * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            await asyncio.to_thread(memory.ingest_all)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            log.exception("project memory pass crashed")
            continue
        consecutive_failures = 0
