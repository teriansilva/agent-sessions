"""Call me back (#2341 §4): a one-shot notice when ONE turn of an API session settles.

On a call with an API session, "call me back when you're done" ends the call and records a
callback bound to ``(session key, turn id)`` of the turn that is running. It fires ONCE, when
that turn settles — completed, failed, stopped or interrupted, each with its own FIXED line — as
an ordinary notification through the existing delivery (`notifications.add` + `fanout`): the bell
always, a push where one is subscribed. There is no second producer and no ring (#1397 P6).

- **Persisted**: a small record per callback (`AGENT_SESSIONS_VOICE_CALLBACKS`, 0600), so a
  restart before the turn settles keeps it; a turn the recovery reports interrupted fires the
  "stopped" line.
- **One shot**: the notification's own tombstone (`once=True`, keyed by the callback's id) is the
  claim, written with the row under the store's lock, so a duplicate settlement (two sweeps, a
  restart between the notice and the record's removal) finds it already announced.
- **Cancel** on the session's pane removes the record; nothing fires after it.
- **What it says** is FIXED: the session's title (the bell row's title, as every session row)
  and one of `LINES`. Never the agent's text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from pathlib import Path

from . import notifications, structured_runtime
from .atomicjson import atomic_write_json, json_write_lock, read_json_doc

log = logging.getLogger(__name__)

#: How often pending callbacks are checked against their sessions.
SWEEP_S = 5.0
#: Callbacks kept at once; the oldest goes first past it.
MAX = 50
#: A callback whose turn never settles (or is no longer in the session's view) lapses after this.
EXPIRE_S = 7 * 86400.0
#: The bell row's action id: `voicecb:<id>` (the web offers Call on it, and it is the one-shot key).
ACTION_PREFIX = "voicecb:"
ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}:[A-Za-z0-9._-]{1,128}\Z")
TURN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

#: The FIXED line per settlement (`structured_runtime._outcome`), after the session's title.
LINES = {
    "completed": "is done.",
    "interrupted": "stopped.",
    "failed": "ran into an error.",
    "uncertain": "ran into an error.",
    "unavailable": "ran into an error.",
}


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_VOICE_CALLBACKS",
            str(Path.home() / ".config" / "agent-sessions" / "voice-callbacks.json"),
        )
    )


def _rows(doc: dict) -> list[dict]:
    rows = doc.get("callbacks") if isinstance(doc, dict) else None
    return [
        r
        for r in rows or ()
        if isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and ID_RE.match(r["id"])
        and isinstance(r.get("key"), str)
        and KEY_RE.match(r["key"])
        and isinstance(r.get("turn_id"), str)
        and TURN_RE.match(r["turn_id"])
    ]


def pending(key: str | None = None) -> list[dict]:
    """Callbacks still waiting, oldest first (of one session's key, if given). Blocking."""
    rows = _rows(read_json_doc(_path()))
    return [r for r in rows if key is None or r["key"] == key]


def add(*, key: str, turn_id: str, title: str, project: str, engine: str) -> dict:
    """Record a callback for the turn `turn_id` of session `key`. One per session: a newer one
    replaces an older one of the same session. Blocking."""
    if not KEY_RE.match(key) or not TURN_RE.match(turn_id):
        raise ValueError("not a session turn")
    row = {
        "id": uuid.uuid4().hex,
        "key": key,
        "turn_id": turn_id,
        "title": str(title or "")[: notifications.TITLE_MAX],
        "project": str(project or "")[:120],
        "engine": str(engine or "")[:40],
        "at": time.time(),
    }
    p = _path()
    with json_write_lock(p):
        rows = [r for r in _rows(read_json_doc(p)) if r["key"] != key]
        rows = [*rows, row][-MAX:]
        atomic_write_json(p, {"callbacks": rows})
    return row


def remove(cb_id: str) -> bool:
    """Drop one callback (Cancel, or after its notice). False when it was not there. Blocking."""
    p = _path()
    with json_write_lock(p):
        rows = _rows(read_json_doc(p))
        kept = [r for r in rows if r["id"] != cb_id]
        if len(kept) == len(rows):
            return False
        atomic_write_json(p, {"callbacks": kept})
    return True


def fire(cb_id: str, state: str) -> dict | None:
    """Announce callback `cb_id` for its turn's settlement `state` and drop its record, in ONE
    step under the store's lock — the lock Cancel (`remove`) and a replacing `add` take too. So a
    callback cancelled or replaced while its turn was being read is never announced, and one
    announced is gone before a Cancel can answer `cancelled`. The bell row is written inside the
    lock (store order: call-backs, then notifications; nothing takes them the other way); the
    push goes after it. None when it was not live any more, or was announced already. Blocking."""
    p = _path()
    with json_write_lock(p):
        rows = _rows(read_json_doc(p))
        row = next((r for r in rows if r["id"] == cb_id), None)
        if row is None:
            return None
        rec = announce(row, state)
        atomic_write_json(p, {"callbacks": [r for r in rows if r["id"] != cb_id]})
    if rec is not None:
        with contextlib.suppress(Exception):
            if notifications.list_subscriptions():
                notifications.fanout(rec)
    return rec


def announce(row: dict, state: str) -> dict | None:
    """The callback's ONE bell row for its turn's settlement `state`; its tombstone is the
    one-shot claim. None when it was announced already. Blocking; `fire` holds the lock."""
    return notifications.add(
        title=row["title"] or "Your session",
        project=row.get("project") or "",
        session_id=row["key"],
        engine=row.get("engine") or "",
        reason=f"{row['title'] or 'It'} {LINES.get(state, LINES['failed'])}",
        action_id=f"{ACTION_PREFIX}{row['id']}",
        once=True,
    )


async def sweep(now: float | None = None) -> int:
    """Check every pending callback once: a turn that settled is announced, then its record goes.
    A session that is gone, or a callback past `EXPIRE_S`, lapses silently. Never raises."""
    fired = 0
    now = time.time() if now is None else now
    for row in await asyncio.to_thread(pending):
        try:
            snap = await structured_runtime.snapshot(row["key"])
        except structured_runtime.StructuredError as exc:
            if exc.status in (404, 410) or now - float(row.get("at") or 0) > EXPIRE_S:
                await asyncio.to_thread(remove, row["id"])
            continue
        except Exception:  # noqa: BLE001 — a failed read is retried on the next sweep
            log.warning("call-me-back: a session could not be read", exc_info=True)
            continue
        turn = next(
            (t for t in snap.get("turns") or () if t.get("turn_id") == row["turn_id"]), None
        )
        state = (turn or {}).get("state")
        if state not in LINES:
            if now - float(row.get("at") or 0) > EXPIRE_S:
                await asyncio.to_thread(remove, row["id"])
            continue
        try:
            rec = await asyncio.to_thread(fire, row["id"], state)
        except Exception:  # noqa: BLE001 — not announced: the next sweep tries again
            log.warning("call-me-back: the notice was not written", exc_info=True)
            continue
        fired += rec is not None
    return fired


async def run() -> None:
    """The sweeper, for the app's lifetime. Quiet with nothing pending."""
    while True:
        with contextlib.suppress(Exception):
            if await asyncio.to_thread(pending):
                await sweep()
        await asyncio.sleep(SWEEP_S)
