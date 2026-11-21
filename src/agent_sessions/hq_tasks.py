"""HQ's tasks (#2103, #2070 B1): the running and queued HQ turns as one list, and being told
when one settles unseen. Invariants: ``docs/invariants/hq-tasks.md``, ``ask-orchestrator.md``.

A task is an HQ turn, identified by ``(conversation id, turn id)``. This module holds no state
of its own and no store: the list is read from the conversations' own records and the
orchestrator's running registry, and the one thing it writes is a bell row.

**The list** (:func:`listing`): every turn running or waiting in this process, and the last few
settled ones. Only what the store keeps: a question's first line as stored (redacted there),
the conversation's title, the status, step counts and times. Never raw text, never model text.

**Being told** is decided on the seen cursor, not on presence: the cursor (``ask_follow``'s
``seen_at``) is the newest update a browser DISPLAYED. When a turn settles, a notification is
due; it is written after :data:`SETTLE_NOTIFY_GRACE_S` and only if the cursor has not reached
the turn's SETTLEMENT by then (never the earlier question: having seen a turn queued or running
acknowledges nothing). One row per turn, keyed ``hq-turn:<cid>:<turn id>``, announced at most
once ever (the bell's own tombstone, written with the row), retracted when the cursor passes
it. A fixed title, the conversation's stored title, and no model text. The PUSH carries the
fixed title and the conversation's link only: the title of the conversation is the Commander's
question, and a row's `reason` never travels in a push (#726).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import ask_orchestrator, ask_store, notifications

log = logging.getLogger("agent_sessions.hq_tasks")

#: How long a settled turn waits for a browser that is showing its conversation to mark it seen
#: before the bell is told.
SETTLE_NOTIFY_GRACE_S = 5.0
#: A settled, unseen turn older than this is never announced by the start-up reconcile.
RECONCILE_WINDOW_S = 24 * 3600
#: How many settled turns the list keeps, newest first.
RECENT_MAX = 6
#: How many conversations with nothing running are read for the list, newest-changed first.
RECENT_CONVERSATIONS = 8
#: The first line of a question, as the list shows it.
FIRST_LINE_MAX = 160

#: The bell row's title per outcome. Fixed words: nothing a model wrote reaches the bell.
TITLES = {
    "answered": "HQ answered",
    "question": "HQ has a question",
    "stopped": "HQ stopped",
    "failed": "HQ could not answer",
}
KEY_PREFIX = "hq-turn:"

_NOTICES: set[asyncio.Task] = set()


def notice_key(cid: str, turn_id: str) -> str:
    return f"{KEY_PREFIX}{cid}:{turn_id}"


def outcome(view: dict) -> str | None:
    """What a settled turn's notification says, or None for a turn none is due for: one that is
    still running or waiting, or that never ran (withdrawn, not started)."""
    status = view.get("status")
    if status == "done":
        ask = view.get("ask")
        return "question" if isinstance(ask, dict) and not ask.get("answered") else "answered"
    if status in ("stopped", "failed"):
        return status
    return None


def first_line(text: object) -> str:
    """The first non-empty line of a STORED question (already through the store boundary)."""
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:FIRST_LINE_MAX]
    return ""


def _title(records: list[dict]) -> str:
    for r in records:
        if r.get("type") == "user" and isinstance(r.get("text"), str):
            return " ".join(r["text"].split())[: ask_store.LIST_TITLE_MAX]
    return ""


def _seen_at(cid: str) -> float:
    """The conversation's seen cursor. Unreadable is "not seen": the Commander is told."""
    try:
        return float(ask_store.read_follows(cid)["seen_at"])
    except Exception:  # noqa: BLE001 — unknown fails toward telling
        return 0.0


def unseen(views: list[dict], seen_at: float, now: float) -> list[dict]:
    """The settled turns a notification is due for: each ran, settled within
    :data:`RECONCILE_WINDOW_S`, and the seen cursor has not reached its SETTLEMENT."""
    out = []
    for v in views:
        at = v.get("settled_at")
        if outcome(v) is None or not isinstance(at, int | float):
            continue
        if at > seen_at and now - at <= RECONCILE_WINDOW_S:
            out.append(v)
    return out


def notify_unseen(cid: str, turn_id: str | None = None) -> list[dict]:
    """Write the bell row for ``turn_id`` (or for every due turn of ``cid``) if its settlement is
    still unseen, at most once per turn ever. Returns the rows written. Blocking."""
    try:
        records = ask_store.read(cid)
    except (ask_store.ConversationNotFound, ask_store.StoreRefused):
        return []
    views = ask_orchestrator.turn_views(records)
    due = unseen(views, _seen_at(cid), time.time())
    if turn_id is not None:
        due = [v for v in due if v["turn_id"] == turn_id]
    written = []
    for v in due:
        try:
            rec = notifications.add(
                title=TITLES[outcome(v)],
                project="",
                session_id="",
                engine="hq",
                # The conversation's name as the store keeps it: the Commander's own first
                # question, redacted there. Never the answer, never model text.
                reason=_title(records),
                action_id=notice_key(cid, v["turn_id"]),
                once=True,
                hq_conversation=cid,
            )
        except Exception:  # noqa: BLE001 — the answer is in the conversation either way
            log.warning("HQ settle notification not written", exc_info=True)
            continue
        if rec is None:
            continue
        written.append(rec)
        with contextlib.suppress(Exception):
            if notifications.list_subscriptions():
                # The row as it is: `fanout` sends a title, the row's `project` (empty here)
                # and the link, never its `reason` (#726). The conversation's name is the
                # Commander's own question, so it stays in the bell and never leaves in a push.
                notifications.fanout(rec)
    # A browser may have marked the turn seen between the check above and the write.
    with contextlib.suppress(Exception):
        retract_seen(cid)
    return written


def retract_seen(cid: str) -> int:
    """Remove the bell rows of ``cid``'s turns whose settlement the seen cursor has passed.
    Returns how many went. Blocking."""
    prefix = f"{KEY_PREFIX}{cid}:"
    try:
        rows = notifications.listing()["notifications"]
    except Exception:  # noqa: BLE001 — nothing retracted on an unreadable bell
        return 0
    keys = {str(r.get("action_id") or "") for r in rows}
    keys = {k for k in keys if k.startswith(prefix)}
    if not keys:
        return 0
    try:
        views = ask_orchestrator.turn_views(ask_store.read(cid))
    except ask_store.ConversationNotFound:
        views = []  # the conversation is gone: its rows have nothing to open
        seen = float("inf")
    except ask_store.StoreRefused:
        return 0
    else:
        seen = _seen_at(cid)
    settled = {v["turn_id"]: v.get("settled_at") for v in views}
    n = 0
    for key in keys:
        at = settled.get(key[len(prefix) :])
        if seen == float("inf") or (isinstance(at, int | float) and at <= seen):
            n += notifications.dismiss_for_action(key)
    return n


def notices_enabled() -> bool:
    """Kill switch ``AGENT_SESSIONS_HQ_TASK_NOTICES=0``: no settle notification is written and
    the start-up reconcile writes none (it still settles what the last process left)."""
    return os.environ.get("AGENT_SESSIONS_HQ_TASK_NOTICES", "1") != "0"


def on_settled(cid: str, turn_id: str) -> None:
    """A turn settled: after the grace, tell the Commander if it is still unseen. Never raises."""
    if not notices_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def later() -> None:
        await asyncio.sleep(SETTLE_NOTIFY_GRACE_S)
        await asyncio.to_thread(notify_unseen, cid, turn_id)

    task = loop.create_task(later(), name=f"hq-notice:{cid}")
    _NOTICES.add(task)
    task.add_done_callback(_NOTICES.discard)


async def reconcile() -> int:
    """At start: settle what the last process left running or queued, then write the row for
    every settled, unseen turn younger than a day that has none (the app stopped between a
    settle and its row). Returns how many rows were written."""
    try:
        rows = await asyncio.to_thread(ask_store.list_conversations)
    except Exception:  # noqa: BLE001 — a start-up convenience, never fatal
        return 0
    n = 0
    for row in rows:
        cid = row["id"]
        if time.time() - float(row.get("updated_at") or 0) > RECONCILE_WINDOW_S:
            continue
        with contextlib.suppress(Exception):
            await ask_orchestrator.read_conversation(cid)  # interrupted / not started
            if notices_enabled():
                n += len(await asyncio.to_thread(notify_unseen, cid))
    return n


async def drain() -> None:
    """Shutdown: notices still waiting out their grace are dropped; `reconcile` writes them at
    the next start."""
    for task in list(_NOTICES):
        task.cancel()
    if _NOTICES:
        await asyncio.wait(list(_NOTICES), timeout=1.0)


def _task_row(cid: str, title: str, v: dict) -> dict:
    steps = v.get("steps") or {}
    live = steps.get("live") if isinstance(steps.get("live"), dict) else None
    state = v.get("status")
    if state == "pending":
        state = "running"
    elif outcome(v) == "question":
        state = "question"
    return {
        "cid": cid,
        "turn_id": v["turn_id"],
        "state": state,
        # The server's continuation after a card (`via: "card"`) is not the Commander's
        # question: its text is the server's note and is never listed.
        "question": "" if v.get("via") == "card" else first_line(v.get("question")),
        "via": v.get("via"),
        "conversation": title,
        "steps": steps.get("count") or 0,
        "step": live.get("tool") if live else None,
        "stopping": bool(v.get("stopping")),
        "asked_at": v.get("asked_at"),
        "started_at": v.get("started_at"),
        "ended_at": v.get("settled_at"),
        "elapsed_ms": v.get("elapsed_ms"),
    }


def _conversation_rows(cid: str) -> list[dict]:
    try:
        records = ask_store.read(cid)
    except (ask_store.ConversationNotFound, ask_store.StoreRefused):
        return []
    views = ask_orchestrator.with_running(ask_orchestrator.turn_views(records), cid)
    title = _title(records)
    return [_task_row(cid, title, v) for v in views]


def listing() -> dict:
    """``{tasks, recent, counts}``: what runs and waits here, the turns parked on an unanswered
    question, and the last settled ones. Blocking."""
    active = set(ask_orchestrator.active())
    now = time.time()
    tasks: list[dict] = []
    recent: list[dict] = []
    others = 0
    for conv in ask_store.list_conversations():
        cid = conv["id"]
        fresh = now - float(conv.get("updated_at") or 0) <= RECONCILE_WINDOW_S
        if cid not in active:
            if not fresh or others >= RECENT_CONVERSATIONS:
                continue
            others += 1  # newest-changed first: a bounded read, whatever the store holds
        rows = _conversation_rows(cid)
        waiting = set(ask_orchestrator.queued(cid))
        running = ask_orchestrator.running_turn(cid)
        newest = rows[-1]["turn_id"] if rows else None
        for row in rows:
            tid = row["turn_id"]
            if row["state"] == "running" and tid == running:
                tasks.append(row)
            elif row["state"] == "queued" and tid in waiting:
                tasks.append(row)
            elif row["state"] == "question" and tid == newest and fresh:
                # Parked on a question nobody answered yet, and nothing was asked since.
                tasks.append(row)
            elif row["state"] not in ("running", "queued") and fresh:
                at = row.get("ended_at")
                if isinstance(at, int | float) and now - at <= RECONCILE_WINDOW_S:
                    recent.append(
                        {**row, "state": "answered" if row["state"] == "done" else row["state"]}
                    )
    order = {"running": 0, "queued": 1, "question": 2}
    tasks.sort(key=lambda r: (order[r["state"]], r.get("asked_at") or 0))
    # One conversation's tasks stay together: its running turn, then what waits behind it.
    by_cid: dict[str, list[dict]] = {}
    for row in tasks:
        by_cid.setdefault(row["cid"], []).append(row)
    tasks = [row for rows in by_cid.values() for row in rows]
    recent.sort(key=lambda r: -(r.get("ended_at") or 0))
    counts = {
        "running": sum(1 for r in tasks if r["state"] == "running"),
        "queued": sum(1 for r in tasks if r["state"] == "queued"),
        "question": sum(1 for r in tasks if r["state"] == "question"),
    }
    return {"tasks": tasks, "recent": recent[:RECENT_MAX], "counts": counts}
