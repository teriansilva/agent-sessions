"""Ask follows the sessions it started, and the sessions its delivered nudges reached (#1443
Phase 4, #1517).

A conversation FOLLOWS a session when:

* its session card reached *Started* (``ask_proposals`` records it, with the bound key), or
* a nudge it proposed was ``delivered`` or ``indeterminate`` (:data:`NUDGE_FOLLOWS`). The ledger
  row is that outcome's only record, stamped server-side with the proposing conversation
  (``ask_conversation``). Each pass admits the settled Ask rows still in the ledger, and ledger
  compaction admits every eligible row BEFORE it deletes it, declining when an admission cannot be
  made durable (:func:`project_for_compaction`). Dismissed, expired, refused and failed nudges
  never follow.

Each pass RECONCILES started sessions from the proposal sidecars, so a follow the launch could not
record is taken later. What a conversation followed for (the proposal or action id) is CONSUMED
and never forgotten, in the same sidecar write that follows, so replaying it never follows again:
Unfollow sticks.

At most :data:`ask_store.MAX_FOLLOWS` at once. A started session past that is not followed (its
card reads *Started* either way). A delivered nudge past it is said in the conversation as a
``not_followed`` update, counted against the same :data:`ask_store.MAX_UPDATES` budget; once that
budget is spent nothing more is queued and the conversation body says ``follows_exhausted``.

**The follower is deterministic** — no model call, no notification. Every pass (``run``, about every
:data:`INTERVAL_S`) it reads, once, each streamed session's last visible-output time
(:func:`output_times`) and the session catalog, and for each followed session compares with what it
saw last time:

* it produced output newer than the last ``finished`` accounted for, and is quiet now → ``finished``
  ("finished its turn") — an edge in its own output clock, so a turn that began and ended between
  two passes is not missed;
* it newly needs the operator (its card's ``intervention_required``) → ``needs_you``;
* it has left a COMPLETE catalog read on :data:`MISSES_TO_END` passes in a row (archived, gone,
  or now outside the roots / inside an exclusion — the catalog is read through
  ``in_scope_cards``, the boundary Ask's own tools use) → ``ended``, and the follow ends. An
  incomplete read (an engine or the metadata unreadable) counts no absence.

The baseline is when the follow began: output counts from ``since``, and a session Ask started
needed no one before it, so an intervention already set at the first pass is reported. Each
change appends an ``update`` record to
the conversation — a KIND plus the session's title, never model text; the client authors the
words — and stamps ``last_update_at``. ``seen_at`` (``POST …/seen``) against it is the unread dot.
There is no second notifier: push and the bell stay with ``needs_you_notify``.

Past :data:`ask_store.MAX_UPDATES` updates a conversation's follows end, so its size stays bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid

from . import ask_store

log = logging.getLogger(__name__)

INTERVAL_S = 60.0
#: Passes a followed session must be missing from the catalog before its follow ends: one missed
#: read (a slow scan, a store mid-write) is not an archive.
MISSES_TO_END = 2


def loop_enabled() -> bool:
    """Kill switch. ``AGENT_SESSIONS_ASK_FOLLOW=0`` stops the follower."""
    return (os.environ.get("AGENT_SESSIONS_ASK_FOLLOW", "1") or "1") != "0"


# ---- following ---------------------------------------------------------------------------------


def follow(
    cid: str, key: str, *, title: str, via: str, reason: str, since: float | None = None
) -> bool:
    """Follow ``key`` from conversation ``cid`` FOR ``reason`` — the id of the started proposal. A
    reason is CONSUMED whether or not the follow is taken (the
    cap may be full), and never forgotten (``ask_store.MAX_CONSUMED`` bounds it by construction):
    replaying it — every pass reconciles every reason — never follows again, so Unfollow sticks.
    Returns whether ``key`` is followed now. Blocking."""
    if not ask_store.exists(cid):
        return False
    with ask_store.follows_lock(cid):
        if not ask_store.exists(cid):
            return False  # retention removed it meanwhile: never leave a sidecar behind
        doc = ask_store.read_follows(cid)
        rows = doc["follows"]
        row = next((f for f in rows if f.get("key") == key), None)
        if reason in doc["consumed"]:
            return bool(row and row.get("active"))
        if len(doc["consumed"]) >= ask_store.MAX_CONSUMED:
            return bool(row and row.get("active"))
        doc["consumed"].append(reason)
        taken = _take(doc, key, title=title, via=via, since=since)
        ask_store.write_follows(cid, doc)
    return taken


def _take(doc: dict, key: str, *, title: str, via: str, since: float | None) -> bool:
    """Follow ``key`` in ``doc`` if there is room (``MAX_FOLLOWS``) and update budget. The CALLER
    holds the follows lock and writes ``doc``. Returns whether ``key`` is followed now."""
    rows = doc["follows"]
    row = next((f for f in rows if f.get("key") == key), None)
    if row and row.get("active"):
        return True
    room = sum(1 for f in rows if f.get("active")) < ask_store.MAX_FOLLOWS
    if not room or doc["updates"] >= ask_store.MAX_UPDATES:
        return False
    if row is None:
        if len(rows) >= ask_store.MAX_FOLLOW_ROWS:
            # Forget the oldest ENDED row; what it was followed for stays consumed.
            ended = [f for f in rows if not f.get("active")]
            if ended:
                rows.remove(ended[0])
        if len(rows) >= ask_store.MAX_FOLLOW_ROWS:
            return False
        row = {"key": key}
        rows.append(row)
    row.update(
        title=title,
        via=via,
        active=True,
        out_through=None,
        # A session Ask STARTED began at the start decision, so it needed no one before the follow
        # did. Starting from False reports an intervention that arrives before the first pass
        # (Hermes on #1500). A NUDGED session may already have needed someone: None, baseline only.
        needs_you=False if via == "start" else None,
        misses=0,
        # From when its output counts: the start decision, or the nudge's delivery.
        since=time.time() if since is None else float(since),
    )
    return True


#: Ledger outcomes after which a nudge's session is followed (#1517): the PTY accepted the bytes,
#: or it may have. Dismissed, expired, refused or failed-before-the-first-byte never follow.
NUDGE_FOLLOWS = frozenset({"delivered", "indeterminate"})


def admit_nudge(cid: str, key: str, *, title: str, action_id: str, since: float) -> str:
    """Admit one delivered Ask nudge (#1517): consume its action id and, in the SAME sidecar write,
    follow ``key`` or record why not. Never appends to the conversation itself: a queued update is
    flushed by the next follower pass (this also runs inside the ledger's compaction lock).
    Returns ``"followed"``, ``"not_followed"`` (the follow cap: an update row is queued),
    ``"exhausted"`` (the update budget is spent: no row, the conversation says so), ``"consumed"``
    (already admitted) or ``"gone"`` (no such conversation). Blocking; raises when the sidecar
    cannot be read or written, so compaction declines."""
    if not ask_store.exists(cid):
        return "gone"
    with ask_store.follows_lock(cid):
        if not ask_store.exists(cid):
            return "gone"
        doc = ask_store.read_follows(cid)
        if action_id in doc["consumed"] or len(doc["consumed"]) >= ask_store.MAX_CONSUMED:
            return "consumed"
        doc["consumed"].append(action_id)
        if _take(doc, key, title=title, via="nudge", since=since):
            outcome = "followed"
        elif doc["updates"] < ask_store.MAX_UPDATES:
            now = time.time()
            # Appended, never replacing what is pending: one counter, bumped at QUEUE time.
            doc["outbox"] = [
                *doc["outbox"],
                {
                    "key": key,
                    "kind": "not_followed",
                    "title": title,
                    "ts": now,
                    "event_id": uuid.uuid4().hex,
                },
            ]
            doc["updates"] += 1
            doc["last_update_at"] = now
            outcome = "not_followed"
        else:
            outcome = "exhausted"
        ask_store.write_follows(cid, doc)
    return outcome


def _nudge_rows(rows) -> list[tuple[str, dict]]:
    """``(conversation, row)`` for every Ask nudge whose outcome earns a follow."""
    out = []
    for r in rows:
        cid = r.get("ask_conversation")
        if (
            r.get("source") == "ask"
            and r.get("state") in NUDGE_FOLLOWS
            and isinstance(cid, str)
            and ask_store.valid_cid(cid)
            and isinstance(r.get("id"), str)
            and isinstance(r.get("session_id"), str)
        ):
            out.append((cid, r))
    return out


def _admit_row(cid: str, r: dict) -> str:
    # `ts` of a settled row is its settling record's: the delivery, the follow's `since`.
    return admit_nudge(
        cid,
        r["session_id"],
        title=str(r.get("title") or ""),
        action_id=r["id"],
        since=float(r.get("ts") or time.time()),
    )


def _follow_delivered_nudges() -> None:
    """The live path: settled Ask nudges still in the ledger. Reads the ledger WITHOUT its lock and
    then takes follows locks, so the lock order stays ledger → follows (compaction's) and never
    the reverse. An unreadable ledger is skipped for this pass."""
    from . import orchestrator_ledger

    status, rows = orchestrator_ledger.latest_by_id_checked()
    if status != "ok":
        return
    for cid, r in _nudge_rows(rows.values()):
        with contextlib.suppress(Exception):
            _admit_row(cid, r)


def project_for_compaction(doomed: list[dict]) -> bool:
    """Ledger compaction's Ask projection (#1517), called INSIDE the ledger lock over the rows it is
    about to delete. Admits every eligible Ask nudge first; False (compaction declines and the
    rows stay) if any admission could not be made durable."""
    for cid, r in _nudge_rows(doomed):
        try:
            _admit_row(cid, r)
        except Exception as e:  # noqa: BLE001 — a decline, not a crash
            log.warning(
                "ask follow: could not admit nudge before compaction (%s)", type(e).__name__
            )
            return False
    return True


def unfollow(cid: str, key: str) -> bool:
    """Stop following ``key``. Returns whether it was followed. Blocking."""
    with ask_store.follows_lock(cid):
        doc = ask_store.read_follows(cid)
        row = next((f for f in doc["follows"] if f.get("key") == key and f.get("active")), None)
        if row is None:
            return False
        row["active"] = False
        ask_store.write_follows(cid, doc)
    return True


def mark_seen(cid: str, through: float) -> bool:
    """Updates up to ``through`` (the newest one the client has SHOWN) have been seen in ``cid``.
    Never later than now, so an update written after the client read cannot be marked seen by it.
    Returns whether an unseen update remains. Blocking."""
    with ask_store.follows_lock(cid):
        if not ask_store.exists(cid):
            return False
        doc = ask_store.read_follows(cid)
        mark = min(float(through), time.time())
        if mark > doc["seen_at"]:
            doc["seen_at"] = mark
            ask_store.write_follows(cid, doc)
        return doc["last_update_at"] > doc["seen_at"]


def state(cid: str) -> dict:
    """``{"following": [keys], "follows": [{key, title}], "unread": bool}`` for one conversation;
    ``follows`` names every ACTIVE follow, so the client can offer Unfollow before any update
    exists (Hermes on #1500). ``since`` maps EVERY remembered follow, active or ended, to when its
    latest follow began: an update older than that belongs to an earlier follow of the session
    (#1755), so a notice it superseded never comes back after Unfollow. ``unread`` is False when
    the sidecar cannot be read. Blocking."""
    empty = {"following": [], "follows": [], "since": {}, "unread": False, "last_update_at": 0.0}
    try:
        doc = ask_store.read_follows(cid)
    except ask_store.ConversationNotFound:
        return {**empty, "incomplete": False, "pending": False, "exhausted": False}
    except ask_store.SidecarUnreadable:
        # Unknown is not "following nothing" (Hermes on #1500): the client keeps what it last
        # verified and keeps reading until a read succeeds.
        return {**empty, "incomplete": True, "pending": False, "exhausted": False}
    active = [f for f in doc["follows"] if f.get("active") and f.get("key")]
    return {
        "incomplete": False,
        # The update budget is spent: follows have ended and no new row will appear (#1517).
        "exhausted": doc["updates"] >= ask_store.MAX_UPDATES,
        # An update stored but not yet appended to the conversation (an interrupted or racing
        # flush): the open screen keeps reading until it lands (Hermes on #1500).
        "pending": bool(doc.get("outbox")),
        "following": [f["key"] for f in active],
        "follows": [{"key": f["key"], "title": f.get("title") or ""} for f in active],
        "since": {
            f["key"]: float(f["since"])
            for f in doc["follows"]
            if f.get("key") and isinstance(f.get("since"), int | float)
        },
        "unread": doc["last_update_at"] > doc["seen_at"],
        "last_update_at": doc["last_update_at"],
    }


def unread_conversations() -> list[str]:
    """Conversations with an update newer than their ``seen_at``. Blocking."""
    out: list[str] = []
    with contextlib.suppress(FileNotFoundError):
        for name in sorted(os.listdir(ask_store.root() / "follows")):
            cid = name[: -len(".json")] if name.endswith(".json") else ""
            if ask_store.valid_cid(cid) and ask_store.exists(cid) and state(cid)["unread"]:
                out.append(cid)
    return out


# ---- the pass ----------------------------------------------------------------------------------


def _followed() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with contextlib.suppress(FileNotFoundError):
        for name in sorted(os.listdir(ask_store.root() / "follows")):
            cid = name[: -len(".json")] if name.endswith(".json") else ""
            if not (ask_store.valid_cid(cid) and ask_store.exists(cid)):
                continue
            try:
                doc = ask_store.read_follows(cid)
            except ask_store.SidecarUnreadable:
                continue
            rows = [f for f in doc["follows"] if f.get("active")]
            if rows or doc["outbox"]:
                out[cid] = rows
    return out


def _follow_started_sessions() -> None:
    """Sessions Ask STARTED follow from their conversation — reconciled from the proposal sidecars
    every pass, so a follow the launch could not record (a stop, a failed write) is taken later.
    A consumed proposal id is a no-op, so Unfollow is never undone."""
    with contextlib.suppress(FileNotFoundError):
        for name in sorted(os.listdir(ask_store.root() / "proposals")):
            cid = name[: -len(".json")] if name.endswith(".json") else ""
            if not (ask_store.valid_cid(cid) and ask_store.exists(cid)):
                continue
            try:
                rows = ask_store.read_proposals(cid)
            except ask_store.SidecarUnreadable:
                continue
            for r in rows:
                key, pid = r.get("session_key"), r.get("id")
                if r.get("state") != "started" or not isinstance(key, str) or not key:
                    continue
                with contextlib.suppress(Exception):
                    follow(
                        cid,
                        key,
                        title=str(r.get("project") or ""),
                        via="start",
                        reason=pid,
                        since=r.get("decided_at"),
                    )


def _physical(key: str) -> str:
    from . import engines, metadata

    try:
        return engines.physical_key(key, metadata.load_aliases())
    except Exception:  # noqa: BLE001
        return key


def output_times(registry) -> dict[str, float]:
    """Each streamed session's last VISIBLE output time (the registry's ``visible_output_at``,
    what ``actuator.busy_keys`` reads). Call on the event loop."""
    out: dict[str, float] = {}
    if registry is None:
        return out
    with contextlib.suppress(Exception):
        for r in registry.snapshot():
            seen = r.get("visible_output_at")
            if isinstance(seen, int | float) and not isinstance(seen, bool):
                out[r["id"]] = float(seen)
    return out


def pass_once(
    activity: dict[str, float],
    *,
    catalog: list[dict] | None = None,
    complete: bool = True,
    now: float | None = None,
) -> int:
    """One follower pass. ``activity`` is :func:`output_times`, read on the loop. Returns the number
    of updates written. Blocking.

    ``complete`` says whether ``catalog`` is the WHOLE set. Only a complete read may count a
    session's absence toward ``ended``, which is irreversible. The default read is strict
    (``pulse.build_cards(strict=True)``): an engine or the metadata read incompletely falls back to
    the fail-soft catalog for this pass's other transitions, and absence counts nothing."""
    from .actuator import BUSY_WINDOW_S

    now = time.time() if now is None else now
    _follow_started_sessions()
    _follow_delivered_nudges()
    followed = _followed()
    if not followed:
        return 0
    if catalog is None:
        from . import pulse, pulse_chat
        from .routes.pulse import in_scope_cards

        busy = {k for k, t in activity.items() if now - t < BUSY_WINDOW_S}
        try:
            catalog = in_scope_cards(
                pulse.build_cards(window_days=None, working_keys=busy, strict=True)
            )
        except pulse.CardsIncomplete as e:
            log.info("ask follow: the session read was incomplete, so no follow ends: %s", e)
            catalog = in_scope_cards(pulse_chat.build_catalog(working_keys=busy))
            complete = False
    cards = {c.get("id"): c for c in catalog if isinstance(c, dict)}
    written = 0
    for cid in followed:
        try:
            written += _pass_conversation(cid, activity, cards, now, BUSY_WINDOW_S, complete)
        except Exception as e:  # noqa: BLE001 — one conversation never stops the others
            log.warning("ask follow pass failed for a conversation (%s)", type(e).__name__)
    return written


def _flush(cid: str, doc: dict, *, fresh: bool = False) -> None:
    """Append the outbox's records the conversation does not hold yet (by event id), then clear it.
    The CALLER holds the follows lock. Idempotent: a stop anywhere in here is finished next pass.
    ``fresh``: the outbox holds only events minted this pass, which cannot be in the conversation
    yet — no need to read it."""
    if not doc["outbox"]:
        return
    have: set = set()
    if not fresh:
        have = {r.get("event_id") for r in ask_store.read(cid) if r.get("type") == "update"}
    missing = [o for o in doc["outbox"] if o.get("event_id") not in have]
    if missing:
        ask_store.append(cid, *({**o, "type": "update"} for o in missing))
    doc["outbox"] = []
    ask_store.write_follows(cid, doc)


def _pass_conversation(
    cid: str,
    activity: dict[str, float],
    cards: dict,
    now: float,
    quiet_after: float,
    complete: bool = True,
) -> int:
    """Diff one conversation's follows. THE OUTBOX PROTOCOL: the new state, the updates (with stable
    event ids) and the unread time are written to the sidecar FIRST, in one atomic write; then the
    updates are appended to the conversation; then the outbox is cleared. A stop between any two
    steps never re-detects a transition (its state is already stored) and never loses or doubles
    an update (the next pass flushes what the conversation lacks, by event id)."""
    with ask_store.follows_lock(cid):
        if not ask_store.exists(cid):
            return 0  # retention removed it meanwhile
        doc = ask_store.read_follows(cid)
        _flush(cid, doc)  # whatever an interrupted pass left behind
        updates: list[dict] = []
        for f in doc["follows"]:
            if not f.get("active"):
                continue
            key = f["key"]
            card = cards.get(key)
            if card is None:
                if not complete:
                    continue  # absent from an incomplete read proves nothing; the count stands
                f["misses"] = int(f.get("misses") or 0) + 1
                if f["misses"] >= MISSES_TO_END:
                    f["active"] = False
                    updates.append({"key": key, "kind": "ended", "title": f.get("title") or ""})
                continue
            f["misses"] = 0
            title = str(card.get("title") or "") or f.get("title") or ""
            f["title"] = title
            # FINISHED is an EDGE in the session's own output clock, not two samples of "busy": it
            # produced output newer than the last `finished` accounted for, and is quiet now. A
            # turn that began and ended between two passes still leaves a newer output time.
            out_at = activity.get(key, activity.get(_physical(key)))
            if out_at is None:
                # After a restart the in-memory clock is empty until the session writes again;
                # the stamp on disk still knows a turn that finished before it (Hermes on
                # #1500). It only ever lags the live one, so it can't invent a turn.
                from . import scrollback

                out_at = scrollback.persisted_visible_output_at(_physical(key))
            quiet = out_at is None or now - out_at >= quiet_after
            # Before the first report, the baseline is when the follow BEGAN (`since`): output
            # from before it never reports, output after it does — even a turn that ran and ended
            # before the first pass ever looked.
            through = f.get("out_through")
            if through is None:
                through = float(f.get("since") or 0.0)
                f["out_through"] = through
            if out_at is not None and out_at > through and quiet:
                updates.append({"key": key, "kind": "finished", "title": title})
                f["out_through"] = out_at
            needs = bool(card.get("intervention_required"))
            if f.get("needs_you") is False and needs:
                updates.append({"key": key, "kind": "needs_you", "title": title})
            f["needs_you"] = needs
        updates = updates[: max(ask_store.MAX_UPDATES - doc["updates"], 0)]
        if updates:
            doc["outbox"] = [{**u, "ts": now, "event_id": uuid.uuid4().hex} for u in updates]
            doc["updates"] += len(updates)
            doc["last_update_at"] = now
        if doc["updates"] >= ask_store.MAX_UPDATES:
            for f in doc["follows"]:
                f["active"] = False
        ask_store.write_follows(cid, doc)  # 1. state + outbox + unread, atomically
        _flush(cid, doc, fresh=True)  # 2. append, 3. clear
    return len(updates)


async def run(registry) -> None:
    """The follower loop. Busy keys are read ON the loop (the registry snapshot iterates a dict the
    loop mutates); everything else runs in a worker thread."""
    fails = 0
    while True:
        await asyncio.sleep(INTERVAL_S * min(2**fails, 8))
        if not loop_enabled():
            continue
        try:
            activity = output_times(registry)
            await asyncio.to_thread(pass_once, activity)
            fails = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a failed pass backs off, never stops the loop
            fails += 1
            log.warning("ask follow pass failed (%s)", type(e).__name__)
