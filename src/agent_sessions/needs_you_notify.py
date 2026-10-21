"""Needs-you notifications — one per episode, retracted when it ends (#1086 Phase 4).

**One producer.** A session no mission holds is announced ONLY here; the orchestrator's pass no
longer raises its own rows for those sessions (`orchestrator._persist`). What "needs you" means is
not decided here either: it is the Ask page's NEEDS YOU list, read through the very same function
(`routes.pulse.build_needs_you`), so a notification can never exist for a session the list would
not show, and vice versa.

**Conservative.** Nothing is announced for a session merely being active, working or finished —
only for entering needs-you. An episode opens once and is persisted
(`notifications.sync_needs_you`), so a restart, a re-worded review or a bell dismissal never
announces the same state again.

**Withdrawable.** When the session stops needing the operator — answered in the terminal (its
review clears), its decision settled, withdrawn or expired, dismissed on Ask, archived, or taken
into a mission — the episode closes: its bell row is retired and, if it was pushed, a close push
for that episode's own tag retracts the device notification.

**An unreadable list closes nothing.** If membership or the ledger cannot be read, "who needs you"
is unknown, and unknown is not "nobody": the pass is skipped rather than retracting everything.

Kill switch: ``AGENT_SESSIONS_NEEDS_YOU_LOOP=0``. The operator's switch is Settings → Session
review → *Notify me when a session needs me*; switching it off closes every open episode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading

from . import needs_you, notifications, prefs, webpush

log = logging.getLogger("agent_sessions.needs_you_notify")

#: Seconds between syncs. A retraction or a new episode waits at most this long when nothing
#: pokes the loop; the operator's own actions (approve, dismiss, archive) poke it immediately.
INTERVAL_S = 60

_lock = threading.Lock()
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def loop_enabled() -> bool:
    """Env kill-switch. ``AGENT_SESSIONS_NEEDS_YOU_LOOP=0`` stops the background sync entirely."""
    return (os.environ.get("AGENT_SESSIONS_NEEDS_YOU_LOOP", "1") or "1") != "0"


def poke() -> None:
    """Run the next sync now rather than at the interval.

    Thread-safe (the ledger's settlement hook runs in worker threads) and a no-op when no loop is
    running, so a caller never has to know whether notifications are on.
    """
    wake, loop = _wake, _loop
    if wake is not None and loop is not None:
        with contextlib.suppress(RuntimeError):  # the loop has closed
            loop.call_soon_threadsafe(wake.set)


def _facts(row: dict) -> dict:
    action = row.get("action") or {}
    return {
        "title": row.get("title") or "",
        "project": (row.get("project") or {}).get("name") or "",
        "engine": row.get("engine") or "",
        "kind": row.get("kind") or "needs_inspection",
        "action_id": action.get("id") or "",
    }


def sync_once() -> dict:
    """One reconciliation: read who needs you, open/close episodes, push. **Blocking.**

    Single-flight: a second caller while one runs returns ``{"skipped": "busy"}``.
    """
    if not _lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        from .routes.pulse import build_needs_you  # deferred: routes import this module's peers

        enabled = bool(prefs.get_session_review().get("notify", True))
        current: dict[str, dict] = {}
        keep: set[str] = set()
        if enabled:
            try:
                out = build_needs_you(int(prefs.get_pulse()["window_days"]), strict=True)
            except needs_you.Unavailable:
                return {"skipped": "unavailable"}
            current = {r["id"]: _facts(r) for r in out.get("rows") or []}
            keep = set(out.get("needs_you_ids") or []) - set(current)
        push = bool(notifications.list_subscriptions())
        try:
            res = notifications.sync_needs_you(current, keep=keep, push=push)
        except notifications.StoreUnreadable:
            # The episodes on disk cannot be read: rewriting them would destroy the record of
            # what was announced and what is owed a retraction (Hermes 5231). Skip, retry later.
            log.warning("needs-you notifications: the notifications store is unreadable")
            return {"skipped": "store unreadable"}
        _deliver(res, push)
        return {"opened": len(res["opened"]), "closed": len(res["closed"])}
    finally:
        _lock.release()


def _deliver(res: dict, push: bool) -> None:
    """Push new episodes; owed retractions RIDE ON THEM, a bounded batch at a time.

    Every push this module sends SHOWS its own new notification. Subscriptions are
    ``userVisibleOnly``: a push that shows nothing spends the browser's silent-push budget and,
    once spent, makes Chrome show its own generic notification — and a push whose only job is to
    retract can always end up showing nothing when deliveries are reordered (Hermes 5239,
    finding 7). So there are no retraction-only pushes: owed retractions travel on the next shown
    push, the oldest first, at most ``webpush.CLOSE_MAX`` per push, and EXACTLY the batch that was
    sent is acknowledged when no device failed (finding 1). The rest stay owed on disk; the app
    closes them itself when it is opened (`listing()["close_tags"]`).
    """
    if not push:
        return
    pending = list(res.get("pending") or [])
    for rec in res["opened"]:
        # As many owed retractions as fit this push's ENCODED budget (Hermes 5265, finding 3).
        batch = webpush.fit_close(
            pending,
            title=rec.get("title", ""),
            project=rec.get("project", ""),
            url=notifications._link(rec),
            tag=str(rec.get("tag") or ""),
        )
        pending = pending[len(batch) :]
        try:
            sent = notifications.fanout(rec, close=batch)
        except Exception:  # noqa: BLE001 — best-effort; a batch carried here stays owed
            log.warning("needs-you notifications: a push failed", exc_info=True)
            continue
        if batch and not sent.get("failed"):
            _ack(batch)


def _ack(tags: list[str]) -> None:
    with contextlib.suppress(Exception):
        notifications.ack_closes(tags)


async def run() -> None:
    """The background sync. Exits at once under the kill-switch."""
    global _wake, _loop
    if not loop_enabled():
        return
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    while True:
        try:
            await asyncio.to_thread(sync_once)
        except Exception:  # noqa: BLE001 — never take the app down over a notification pass
            log.warning("needs-you notifications: a sync failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=INTERVAL_S)
        _wake.clear()
