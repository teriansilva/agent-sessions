"""HQ's subagents: spawn handles, the session-limit queue, and the group a task's subagents make
(#2398, epic #2070 B2). Invariants: ``docs/invariants/hq-subagents.md``.

A subagent is an ordinary API session HQ's start card launched for one task. This module adds no
launch path and no run-claim store: the launch is the session card's own (`ask_proposals`), its
durable ``pending → deciding`` claim is the run claim, its session id is derived from the card
(`mission_structured.hq_ids`), and the driver record says which task drives it
(`session_driver`). What is here:

* **Handles.** ``spawn`` returns ``sp-<n>`` (unique in its task) before any session key exists.
  A handle has exactly one outcome at a time: ``queued`` (waiting for capacity, holding no slot),
  ``started`` (its card launched; it has a key), ``not_started`` (deadline passed, task stopped,
  the owning turn settled, a restart, or the launch did not happen), ``cancelled`` (the
  Commander's Cancel, or HQ's ``stop`` on the handle) or ``asks`` (its card waits for the
  Commander). The live handles of a running task are memory only (:data:`_TASKS`); every change of
  outcome is a ``subagent`` record of the task in its conversation, so a reader folds them
  (:func:`fold`) and history survives a restart.
* **The queue.** A spawn that finds the session limit or the specialist pool full is ``queued``:
  memory only, no slot held, at most :data:`SPAWN_QUEUE_MAX` per task, for at most
  :data:`SPAWN_QUEUE_MAX_S`. The task's ``wait`` re-checks capacity ONCE per tick for the whole
  task (:func:`capacity`, one ``session_capacity`` admission check and one pool read), never per
  entry. When a slot frees the start runs through its card as usual; the route's own limit still
  decides at create and returns the entry to the queue with nothing created.
* **Lifetime.** When the owning turn settles every still-``queued`` handle becomes
  ``not_started`` (:func:`settle`), and after a restart a ``queued`` record whose turn is not
  running here reads ``not_started`` (:func:`fold`). Nothing queued ever launches later, or
  without its task.

Nothing here names an engine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import ask_store

QUEUED, STARTED, NOT_STARTED, CANCELLED, ASKS = (
    "queued",
    "started",
    "not_started",
    "cancelled",
    "asks",
)
#: Queued spawns one task may hold, and how long one may wait for capacity.
SPAWN_QUEUE_MAX = 8
SPAWN_QUEUE_MAX_S = 900.0
#: Handles or keys one ``wait`` call takes, and the longest it waits.
WAIT_KEYS_MAX = 8
WAIT_MAX_S = 120.0
#: How often a ``wait`` wakes at least (the stop flag, its deadline, the task's queue).
WAIT_TICK_S = 1.0
#: How long ``spawn`` (and a queued start advanced by ``wait``) waits for its launch before it
#: says "starting": the launch settles on its card, and ``wait`` follows it.
SPAWN_ACT_WAIT_S = 5.0
#: Subagents the conversation's group panel shows at most, newest kept.
GROUP_MAX = 24
#: Spawn handles one task may mint.
HANDLES_MAX = 64
REASON_MAX = 280
#: What a handle left queued when its turn settled says.
SETTLED_REASON = "not started: the task ended before a slot freed"
DEADLINE_REASON = "not started: the session limit stayed full"
STOPPED_REASON = "not started: the Commander stopped this task"
RESTART_REASON = "not started: the server restarted before a slot freed"
CANCELLED_REASON = "cancelled before it started"

#: Capacity checks made (the measurement reads it: one per tick per task with queued handles).
STATS = {"capacity_checks": 0}
#: A handle whose start is under way (spawn's first attempt, or a queued one being advanced):
#: transient, never recorded, and not cancellable — the card's own claim decides it.
NEW = "new"


@dataclass
class Handle:
    name: str
    #: What spawn validated: the session card's entry and card record, minus the ids.
    entry: dict
    card: dict
    title: str
    agent: str
    outcome: str = QUEUED
    pid: str = ""
    key: str = ""
    reason: str = ""
    deadline: float = 0.0
    #: Its card settled for good (started, failed, dismissed…): nothing more to re-read.
    final: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def view(self) -> dict:
        out = {"handle": self.name, "outcome": self.outcome}
        if self.key:
            out["key"] = self.key
        if self.reason:
            out["reason"] = self.reason[:REASON_MAX]
        return out


@dataclass
class Task:
    cid: str
    turn_id: str
    handles: dict[str, Handle] = field(default_factory=dict)
    next_n: int = 1
    #: When the task last checked capacity for its queue (monotonic), and what it found.
    checked_at: float = -1e9
    #: When the task last re-read its handles' cards (monotonic): once per tick, for all of them.
    refreshed_at: float = -1e9

    def new(self, *, entry: dict, card: dict, title: str, agent: str) -> Handle:
        if self.next_n > HANDLES_MAX:
            raise ValueError("too many spawns in this task")
        h = Handle(f"sp-{self.next_n}", entry, card, title[:120], agent[:120])
        self.next_n += 1
        self.handles[h.name] = h
        return h

    def queued(self) -> list[Handle]:
        return [h for h in self.handles.values() if h.outcome == QUEUED]


_TASKS: dict[tuple[str, str], Task] = {}


def task(cid: str, turn_id: str) -> Task:
    t = _TASKS.get((cid, turn_id))
    if t is None:
        t = _TASKS[(cid, turn_id)] = Task(cid, turn_id)
    return t


def live(cid: str, turn_id: str) -> Task | None:
    return _TASKS.get((cid, turn_id))


def record(turn_id: str, h: Handle) -> dict:
    """The ``subagent`` record of one handle's outcome (`ask_store`)."""
    rec = {
        "type": "subagent",
        "turn_id": turn_id,
        "handle": h.name,
        "outcome": h.outcome,
        "title": h.title,
        "agent": h.agent,
        "reason": h.reason[:REASON_MAX],
    }
    if h.key:
        rec["key"] = h.key
    if h.pid:
        rec["pid"] = h.pid
    return rec


def cancel(cid: str, turn_id: str, name: str, *, reason: str = CANCELLED_REASON) -> Handle | None:
    """Cancel a QUEUED handle (the Commander's Cancel, or HQ's ``stop``). Synchronous: it changes
    the outcome in the same step it reads it, so a queue advance that takes the handle's lock
    afterwards finds it cancelled and launches nothing. None when there is no such queued handle."""
    t = _TASKS.get((cid, turn_id))
    h = t.handles.get(name) if t is not None else None
    if h is None or h.outcome != QUEUED:
        return None  # a start being advanced is NEW until it settles: too late to cancel
    h.outcome, h.reason = CANCELLED, reason
    return h


def settle(cid: str, turn_id: str, *, stopped: bool = False) -> list[dict]:
    """The owning turn settles: every still-queued handle is ``not_started`` and the task's live
    handles go. Returns the records to write with the turn's outcome. Synchronous."""
    t = _TASKS.pop((cid, turn_id), None)
    if t is None:
        return []
    out = []
    for h in t.handles.values():
        if h.outcome == QUEUED:
            h.outcome, h.reason = NOT_STARTED, STOPPED_REASON if stopped else SETTLED_REASON
            out.append(record(turn_id, h))
    return out


def expire(t: Task, now: float) -> list[Handle]:
    """Queued handles past their deadline become ``not_started``. Synchronous."""
    gone = []
    for h in t.queued():
        if now >= h.deadline:
            h.outcome, h.reason = NOT_STARTED, DEADLINE_REASON
            gone.append(h)
    return gone


# ---- capacity -----------------------------------------------------------------------------------


def capacity(entries: list[dict]) -> list[str]:
    """Whether each of ``entries`` (session card entries) could start NOW: "" when it could, else
    why not ("session limit 6 / 6", "the pool is full"). ONE session-limit check and ONE pool read
    for all of them. Advisory: the create's own admission still decides. Blocking."""
    from . import session_capacity

    STATS["capacity_checks"] += 1
    full = ""
    try:
        session_capacity.check()
    except session_capacity.SessionLimitReached as e:
        full = f"session limit {e.running} / {e.limit}" + (
            f" ({e.reserved} kept for automations)" if e.reserved else ""
        )
    if full:
        return [full for _ in entries]
    pinned = [e.get("specialist") for e in entries]
    if not any(pinned):
        return ["" for _ in entries]
    try:
        from . import specialist_capacity, specialists_store

        counts, total = specialists_store.active_counts()
        limit = specialist_capacity.max_instances()
    except Exception:  # noqa: BLE001 — unreadable: let the create's own admission decide
        return ["" for _ in entries]
    out = []
    taken: dict[str, int] = {}
    for spec in pinned:
        if not spec:
            out.append("")
            continue
        sid = spec.get("id")
        try:
            cap = int(specialists_store.get(sid)["current"]["capacity"])
        except Exception:  # noqa: BLE001
            out.append("")
            continue
        mine = counts.get(sid, 0) + taken.get(sid, 0)
        if total + sum(taken.values()) >= limit:
            out.append(f"the specialist pool is full ({total} of {limit} running)")
        elif mine >= cap:
            out.append(f"this specialist is at capacity ({mine} of {cap} running)")
        else:
            out.append("")
            taken[sid] = taken.get(sid, 0) + 1
    return out


# ---- the task's identity, as the driver badge names it -----------------------------------------


def task_ref(cid: str, turn_id: str) -> dict | None:
    """``{"turn_id", "n"}``: the task's ordinal among its conversation's questions ("HQ · task n").
    One conversation read. Blocking; None when it can't be read."""
    if not (ask_store.valid_cid(cid) and ask_store.valid_turn_id(turn_id)):
        return None
    try:
        records = ask_store.read(cid)
    except Exception:  # noqa: BLE001 — the badge then names no task
        return None
    seen: list[str] = []
    for r in records:
        if r.get("type") == "user" and r.get("turn_id") not in seen:
            seen.append(r["turn_id"])
    if turn_id not in seen:
        return None
    return {"turn_id": turn_id, "n": seen.index(turn_id) + 1}


# ---- reading them back --------------------------------------------------------------------------


def fold(records: list[dict], running: str | None = None) -> list[dict]:
    """Every handle of a conversation, its NEWEST record winning, in spawn order. A ``queued``
    handle whose turn is not ``running`` here (it settled without a record, or the server
    restarted) reads ``not_started``: nothing queued launches later. Pure."""
    by: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    tasks: list[str] = []
    for r in records:
        if r.get("type") == "user" and r.get("turn_id") not in tasks:
            tasks.append(r["turn_id"])
        if r.get("type") != "subagent":
            continue
        k = (r.get("turn_id"), r.get("handle"))
        if k not in by:
            order.append(k)
        by[k] = r
    out = []
    for k in order:
        r = dict(by[k])
        r.pop("type", None)
        r.pop("ts", None)
        if r.get("outcome") == QUEUED and r.get("turn_id") != running:
            r["outcome"], r["reason"] = NOT_STARTED, RESTART_REASON
        tid = r.get("turn_id")
        if tid in tasks:
            r["task_n"] = tasks.index(tid) + 1
        out.append(r)
    return out


def reconcile(rows: list[dict], cards: list[dict]) -> list[dict]:
    """Each folded handle against its card, the authoritative record of its start (Hermes 9131):
    a card the Commander started (or dismissed) after the task settled, a start interrupted by a
    restart whose session exists. A handle with no card (queued, cancelled before staging) keeps
    its recorded outcome. Pure."""
    from . import mission_structured

    by_id = {c.get("id"): c for c in cards if c.get("kind") == "session"}
    for r in rows:
        card = by_id.get(r.get("pid"))
        if card is None:
            continue
        st = card.get("state")
        key = str(card.get("session_key") or "")
        if st in ("deciding", "started", "unconfirmed") or (st == "interrupted" and key):
            if not key:
                try:
                    key = f"{card['engine']}:{mission_structured.hq_ids(card['id'])[0]}"
                except (KeyError, ValueError):
                    key = ""
            r["outcome"] = STARTED
            if st == "deciding":
                r["starting"] = True  # its create may not have happened yet (Hermes 9134)
            if key:
                r["key"] = key
            r["reason"] = "" if st in ("started", "deciding") else str(card.get("reason") or "")
        elif st == "dismissed":
            r["outcome"], r["reason"] = CANCELLED, "the Commander dismissed its card"
        elif st == "pending":
            # Back in its running task's queue after a limit refusal at create: still queued.
            # Otherwise the card waits for the Commander's own tap.
            if not (
                r.get("outcome") == QUEUED and card.get("notice") in ("session_limit", "capacity")
            ):
                r["outcome"] = ASKS
                r["reason"] = str(card.get("reason") or "its card waits for the Commander")
        elif st in ("failed", "stale", "interrupted"):
            r["outcome"] = NOT_STARTED
            r["reason"] = str(card.get("reason") or f"its start {st}")[:REASON_MAX]
    return rows


def keys_of(cid: str, turn_id: str) -> list[str]:
    """The session keys this task's started handles have (memory)."""
    t = _TASKS.get((cid, turn_id))
    return [h.key for h in t.handles.values() if h.key] if t is not None else []


def reset_for_test() -> None:
    _TASKS.clear()
    STATS["capacity_checks"] = 0


# ---- the conversation's group panel -------------------------------------------------------------


def _steered(rows: list[dict]) -> dict[str, int]:
    """How often HQ sent each session a step from this conversation (its done drive sends)."""
    out: dict[str, int] = {}
    for r in rows:
        if r.get("op") == "session.send" and r.get("state") == "done":
            key = r.get("target")
            if isinstance(key, str):
                out[key] = out.get(key, 0) + 1
    return out


#: A started handle whose session could not be read (Hermes 9134): ``starting`` while its card's
#: claim is still creating it, ``gone`` only once the runtime answered 404 for a card that
#: settled, else ``unreadable``: transient, and the panel keeps observing it.
STARTING, GONE, UNREADABLE = "starting", "gone", "unreadable"


def _live_state(snap: dict | None, view: dict, missing: str = GONE) -> str:
    """working | waiting | done | taken_over | mission, from the session's own snapshot; without
    one, ``missing`` (starting, gone or unreadable). Pure."""
    if view.get("who") == "commander" and view.get("generation"):
        return "taken_over"  # HQ drove it, then the Commander typed (no record: never HQ's yet)
    if view.get("who") == "mission":
        return "mission"
    if snap is None:
        return missing
    settled = {"completed", "failed", "interrupted", "uncertain", "unavailable"}
    if snap.get("pending_requests") or any(
        t.get("questions") for t in snap.get("turns") or [] if t.get("state") not in settled
    ):
        return "waiting"
    return "working" if snap.get("active_turn") else "done"


async def group(cid: str, running: str | None) -> list[dict]:
    """The conversation's subagents as its group panel shows them (#2398): each handle's newest
    outcome (`fold`), and for a started one its session's state, who drives it, its driver
    generation (Hand back is a CAS on it) and how often HQ steered it. At most `GROUP_MAX`, newest
    kept; one conversation read, one sidecar read, one driver read, one snapshot per session."""
    from . import session_driver
    from . import structured_runtime as runtime

    records = await asyncio.to_thread(ask_store.read, cid)
    try:
        cards = await asyncio.to_thread(ask_store.read_proposals, cid)
    except Exception:  # noqa: BLE001 — counts unknown: shown as none
        cards = []
    rows = reconcile(fold(records, running), cards)[-GROUP_MAX:]
    steered = _steered(cards)
    keys = [r["key"] for r in rows if r.get("key") and r.get("outcome") == STARTED]
    views = await asyncio.to_thread(session_driver.views, keys) if keys else {}
    for r in rows:
        key = r.get("key")
        if not key or r.get("outcome") != STARTED:
            r["state"] = r.get("outcome")
            continue
        missing = STARTING if r.get("starting") else UNREADABLE
        try:
            snap = await runtime.snapshot(key)
        except runtime.StructuredError as e:
            snap = None
            if e.status == 404 and not r.get("starting"):
                missing = GONE  # the runtime says it does not exist, and its start settled
        except Exception:  # noqa: BLE001 — transient: observed again on the next read
            snap = None
        view = views.get(key) or {}
        r["state"] = _live_state(snap, view, missing)
        r["active_turn"] = (snap or {}).get("active_turn")
        r["driver"] = {
            "who": view.get("who"),
            "generation": view.get("generation"),
            "here": view.get("cid") == cid,
        }
        r["steered"] = steered.get(key, 0)
    return rows
