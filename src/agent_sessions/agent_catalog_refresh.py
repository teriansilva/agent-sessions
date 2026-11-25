"""Daily agent catalog checks, shared by manual and background refreshes.

One cross-process fence owns each attempt through verified acceptance. Persisting the attempt
before networking rate-limits failures and restarts too. A refresh itself only fetches and
verifies the catalog. After the background loop starts, and after every successful check, it hands
over to `agent_autoupdate`, which may install, offline-check and activate a newer recipe for an
installed catalog-managed agent (#1994). Nothing else is ever installed or activated from here.
The catalog's own `automatic` flag survives only as a migration input: #1994 moved it to the one
pause switch (`update.auto_update_enabled`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from .plugins import feed, feed_client, process, storage

SETTINGS = "agent-catalog-settings.json"
INTERVAL = 86400
RETRY = 3600  # a failed attempt is retried sooner, still recorded before networking (#1604)
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
            wait = RETRY if value["error"] else INTERVAL
            if automatic and (
                not value["automatic"]
                or (value["last_attempt"] is not None and now - value["last_attempt"] < wait)
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
        # A failed worker must finish writing its error and release the fence, but its
        # exception must not replace cancellation: run() would catch it and keep looping.
        with contextlib.suppress(Exception):
            await process.drain(task)
        raise


BUSY_RETRY = 600  # an unattended update that found another operation running tries again


async def run() -> None:
    # Same operational/test kill switch as the application's other background workers.
    if os.environ.get("AGENT_SESSIONS_CATALOG_LOOP") == "0":
        return
    from . import agent_autoupdate

    # A restart resumes any interrupted unattended update before the first check is due.
    retry_at = 0.0 if await agent_autoupdate.run_once() == "busy" else None
    while True:
        accepted = None
        try:
            accepted = await refresh(automatic=True)
        except (ValueError, OSError):
            log.info("Agent catalog check unavailable; retained previous catalog")
        if accepted is not None or (retry_at is not None and time.monotonic() >= retry_at):
            result = await agent_autoupdate.run_once()
            retry_at = time.monotonic() + BUSY_RETRY if result == "busy" else None
        await asyncio.sleep(60)
