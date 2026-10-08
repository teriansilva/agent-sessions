"""Daily, optional agent metadata checks, shared by manual and background refreshes.

One cross-process fence owns each attempt through verified acceptance. Persisting the attempt
before networking rate-limits failures and restarts too. This module never installs agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .plugins import feed, feed_client, process, storage

SETTINGS = "agent-catalog-settings.json"
INTERVAL = 86400
log = logging.getLogger(__name__)


def _read(path) -> dict:
    value = storage.read(path, max_bytes=4096)
    if value is None:
        return {
            "format": 1,
            "automatic": True,
            "last_attempt": None,
            "last_success": None,
            "error": None,
        }
    if (
        set(value) != {"format", "automatic", "last_attempt", "last_success", "error"}
        or type(value["format"]) is not int
        or value["format"] != 1
        or type(value["automatic"]) is not bool
        or any(
            value[k] is not None and (type(value[k]) is not int or value[k] <= 0)
            for k in ("last_attempt", "last_success")
        )
        or (value["error"] is not None and not isinstance(value["error"], str))
    ):
        raise storage.StateError("catalog refresh settings are unreadable; left untouched")
    return value


def status() -> dict:
    with storage.locked(SETTINGS) as path:
        return _read(path)


def configure(automatic: bool) -> dict:
    if type(automatic) is not bool:
        raise ValueError("automatic must be true or false")
    with storage.locked(SETTINGS) as path:
        value = _read(path)
        value["automatic"] = automatic
        storage.write(path, value)
        return value


def _refresh(*, automatic: bool) -> feed.Feed | None:
    with storage.locked("agent-catalog-refresh", wait=0):
        with storage.locked(SETTINGS) as path:
            value = _read(path)
            now = int(time.time())
            if automatic and (
                not value["automatic"]
                or (value["last_attempt"] is not None and now - value["last_attempt"] < INTERVAL)
            ):
                return None
            value["last_attempt"] = now
            storage.write(path, value)
        result = None
        failure = None
        try:
            result = asyncio.run(feed_client.refresh())
        except (ValueError, OSError) as exc:
            failure = exc
        with storage.locked(SETTINGS) as path:
            value = _read(path)  # preserve an opt-out saved while this request was in flight
            value["error"] = (
                "The update could not be verified or fetched. Previous catalog retained."
                if failure
                else None
            )
            if failure is None:
                value["last_success"] = int(time.time())
            storage.write(path, value)
        if failure is not None:
            raise failure
        return result


async def refresh(*, automatic: bool = False) -> feed.Feed | None:
    task = asyncio.create_task(asyncio.to_thread(_refresh, automatic=automatic))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await process.drain(task)
        raise


async def run() -> None:
    # Same operational/test kill switch as the application's other background workers.
    if os.environ.get("AGENT_SESSIONS_CATALOG_LOOP") == "0":
        return
    while True:
        try:
            await refresh(automatic=True)
        except (ValueError, OSError):
            log.info("Agent catalog check unavailable; retained previous catalog")
        await asyncio.sleep(60)
