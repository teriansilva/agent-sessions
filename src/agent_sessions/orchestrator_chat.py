"""The Pulse chat that can act (#726 Phase 4).

Pulse Ask (#522) answers "which session was that?". This answers that *and* "tell the kimi
session to keep going" and "why did you nudge it?" — one conversation instead of three
surfaces. The operator should be able to talk to one agent about their whole fleet.

**Routing is one cheap classification, not a tool-calling loop.** A first bounded call decides
which of three intents the message is, and each intent then reuses machinery that already
exists and is already tested:

* ``find`` → ``pulse_chat.ask`` verbatim (2-stage retrieval, anti-hallucination id validation).
* ``history`` → the ledger. "Why did you do X" is answered from what was RECORDED, never
  re-inferred — a model reconstructing its own past reasoning is writing fiction, and this is
  the one question where the operator most needs the truth.
* ``instruct`` → the same verb path a scheduled pass uses: same closed verb set, same id
  validation against the slice actually sent, same precondition capture, same tier gating,
  same ledger. A chat message is not a privileged channel.

That last point is the design's whole safety story here. It would be easy to let the chat
write directly — the operator asked for it, after all — but then there would be two paths to a
PTY with two sets of guards, and the newer one would be the weaker. Every instruction becomes a
proposal and goes through approval exactly like a scheduled one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable

from . import orchestrator, prefs, prompts, pulse_chat, review
from . import orchestrator_ledger as ledger

QUERY_MAX = 2_000
ANSWER_MAX = 800
HISTORY_ROWS = 20


def _clamp(value: object, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap]


async def _classify(query: str, history: list[dict]) -> str:
    """One cheap call. Falls back to ``find`` on anything unexpected — the safe direction is
    always to describe rather than to act."""
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("chat_route")},
                # Sanitized at the sink (see pulse_chat.bound_history): the role gate is what
                # stops replayed, client-supplied turns from carrying a system message, and
                # applying it here keeps that provable from the call site.
                *pulse_chat.bound_history(history),
                {"role": "user", "content": query},
            ]
        )
    except review.ReviewError:
        return "find"
    intent = obj.get("intent")
    return intent if intent in ("find", "instruct", "history") else "find"


def _history_answer(limit: int = HISTORY_ROWS) -> dict:
    """What the orchestrator actually did, straight from the ledger.

    Deliberately NOT a model call. Asked "why did you nudge that session", a model would
    happily reconstruct a plausible rationale — which is exactly the question where a
    plausible answer is worse than none, because the operator is auditing an autonomous system
    and cannot tell reconstruction from record.
    """
    rows = ledger.feed(limit)
    return {
        "intent": "history",
        "answer": (f"{len(rows)} recent action(s)." if rows else "I haven't done anything yet."),
        "actions": rows,
        "matches": [],
    }


DECISIONS_MAX = 5
DECISION_LINE_MAX = 400


def mission_decisions(mission_id: str) -> tuple[set[str], list[str]]:
    """``(held session keys, open decisions)`` for ONE mission — the context a mission-scoped
    question is answered from (#1213). Blocking (missions store + ledger).

    The decisions, most concrete first, each one line of text the server wrote:

    * a pending ledger decision on one of the mission's sessions — a permission dialog names
      itself (`permission_prompts.summary`), anything else is its verb and rationale;
    * the mission's current, unresolved escalations (`missions.current_escalations`, the same
      predicates as the attention flag);
    * its open question.

    Nothing here comes from another mission or another session: the keys are the mission's own
    roster, and the ledger is filtered to them.
    """
    from . import missions, permission_prompts

    m = missions.get_mission(mission_id, events_limit=50, attention=True) or {}
    keys = {
        str(r.get("session_key"))
        for r in m.get("sessions") or []
        if r.get("session_key") and not r.get("removed_at")
    }
    lines: list[str] = []
    for a in ledger.live_actions():
        if str(a.get("session_id") or "") not in keys:
            continue
        if a.get("state") not in ledger.OPERATOR_PENDING_STATES:
            continue
        perm = (a.get("observed_prompt") or {}).get("permission")
        if isinstance(perm, dict):
            lines.append(permission_prompts.summary(perm))  # type: ignore[arg-type]
        else:
            verb = str(a.get("verb") or "action")
            why = _clamp(a.get("rationale"), DECISION_LINE_MAX)
            lines.append(
                f"{verb} on {a.get('engine') or 'a session'}" + (f": {why}" if why else "")
            )
    # The CURRENT escalations, by the store's own attention predicates — never "the newest
    # escalation event", which can be one already settled while an older one is still open.
    for reason in missions.current_escalations(mission_id):
        lines.append(_clamp(reason, DECISION_LINE_MAX))
    q = m.get("question")
    if isinstance(q, dict) and q.get("question"):
        lines.append("an open question: " + _clamp(q.get("question"), DECISION_LINE_MAX))
    seen: set[str] = set()
    uniq = [ln for ln in lines if ln and not (ln in seen or seen.add(ln))]
    return keys, uniq[:DECISIONS_MAX]


async def _mission_find(
    query: str,
    history: object,
    mission_id: str,
    *,
    working_keys: set[str] | None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """A mission's own question (#1213): answered from THIS mission's open decisions first, and
    retrieved only from this mission and the sessions it holds — never the fleet."""
    from . import missions

    keys, decisions = await missions.run_admitted(lambda: mission_decisions(mission_id))
    result = await pulse_chat.ask(
        query, history, working_keys=working_keys, scope=(keys, mission_id), on_event=on_event
    )
    if decisions:
        lead = (
            "Waiting on you in this mission: "
            + " — ".join(decisions)
            + ". Answer it on the decision card above."
        )
        answer = str(result.get("answer") or "").strip()
        result = {**result, "answer": f"{lead}\n\n{answer}" if answer else lead}
    return {"intent": "find", **result, "actions": [], "decisions": decisions}


async def ask(
    query: str,
    history: object = None,
    *,
    working_keys: set[str] | None = None,
    turn_id: str | None = None,
    mission_id: str | None = None,
    reserve_write: Callable[[list[str]], bool] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """One chat turn. Raises :class:`review.NotConfiguredError` (→409) /
    :class:`review.ReviewError` (→502), matching ``/api/pulse/ask``.

    ``turn_id`` and ``reserve_write`` exist for the mission console's `/message` route (#852) and
    are both optional, so every existing caller is unchanged.

    * ``turn_id`` **and ``mission_id``** are both stamped onto each recorded action as
      provenance. Qualifying by mission is not decoration: the turn key is
      ``(mission_id, turn_id)``, so two missions may legitimately use the same turn id, and
      matching on the id alone lets one mission's recovery adopt another mission's actions.
    * ``reserve_write`` is called **immediately before** the ledger append, receives the action
      ids about to be written, and returns False if this caller has been fenced out. It is a
      *reservation*, not a check: it commits a durable receipt — including that intended
      identity — so recovery can look for exactly those actions rather than infer from timing.
      A False answer aborts the append entirely: no action is written and none is claimed.
    * ``on_progress`` (#1224) is told which step the turn is on — ``classify``, the ask pipeline's
      own ``catalog`` / ``content`` steps with its provisional Stage-1 answer, or ``instruct`` — so
      the mission composer can show it while it runs. It is OBSERVATIONAL: it is never awaited,
      what it raises is swallowed, and nothing it does can change what the turn returns.
    """

    def emit(ev: dict) -> None:
        if on_progress is None:
            return
        # A provisional answer is shown as TEXT only: its cards are the final answer's to carry.
        if ev.get("type") == "answer":
            ev = {"type": "answer", "final": False, "answer": ev.get("answer") or ""}
        # A watcher can never fail the turn it watches.
        with contextlib.suppress(Exception):
            on_progress(ev)

    review._require_config()
    turns = pulse_chat.bound_history(history)
    emit({"type": "progress", "step": "classify"})
    intent = await _classify(query, turns)

    if intent == "history":
        return _history_answer()

    if intent == "find":
        if mission_id:
            return await _mission_find(
                query, history, mission_id, working_keys=working_keys, on_event=emit
            )
        result = await pulse_chat.ask(query, history, working_keys=working_keys, on_event=emit)
        return {"intent": "find", **result, "actions": []}

    # instruct — the same path a scheduled pass takes, with no shortcuts.
    cfg = prefs.get_automation_policy("mission" if mission_id else "session")
    now = time.time()
    cards, skipped = await asyncio.to_thread(
        orchestrator.eligible_cards, now=now, working_keys=working_keys, mission_id=mission_id
    )
    if not cards:
        return {
            "intent": "instruct",
            "answer": (
                "There's nothing I can act on right now — every session is either excluded, "
                "on an engine I can't drive, or already has an action waiting for you."
            ),
            "actions": [],
            "matches": [],
            "skipped": skipped,
        }

    slice_ = cards[: orchestrator.DIGEST_MAX]
    emit({"type": "progress", "step": "instruct", "sessions": len(slice_)})
    sent = {c["id"]: c for c in slice_}
    payload = {
        "instruction": query,
        "sessions": [orchestrator._digest_entry(c, now) for c in slice_],
    }
    obj = await review.complete_json(
        [
            {"role": "system", "content": prompts.effective("chat_instruct")},
            *pulse_chat.bound_history(turns),
            {"role": "user", "content": json.dumps(payload)},
        ]
    )
    # Same `now` the digest was built with, so staleness is measured against the instant
    # this pass observed rather than drifting to wall-clock between the two.
    # `dropped` carries what validation REMOVED and why. Without it a removal is invisible here
    # — `intended` would read False and the "On it." below would stand over an empty action
    # list, telling the operator a session had been nudged when nothing was even proposed.
    validation_dropped: list[dict] = []
    answer, actions = orchestrator._validate_actions(
        obj, sent, now=now, dropped=validation_dropped, cfg=cfg
    )
    answer = _clamp(answer or obj.get("answer"), ANSWER_MAX)

    # `complete_json` is the long await here exactly as it is in a scheduled pass, and policy
    # can change across it. Mirror `run_pass()`: re-read the config and re-derive eligibility
    # BEFORE recording anything. Asking the orchestrator to do something is not a standing
    # grant — an operator who withdrew agency, excluded a session, or lost engine actuation
    # support mid-call must not find an `approved` action waiting for them afterwards, and a
    # session that picked up a pending action from a concurrent scheduled pass must not get a
    # duplicate stacked on top of it.
    cfg = prefs.get_automation_policy("mission" if mission_id else "session")
    still_eligible = {
        c["id"]
        for c in await asyncio.to_thread(
            orchestrator._eligible_ids, working_keys, mission_id=mission_id
        )
    }
    # How many actions the model actually asked for — counted BEFORE the eligibility filter and
    # the cap below, and including the ones validation already removed. This is the number the
    # model's own sentence describes, so it is the only honest thing to reconcile the answer
    # against.
    intended_n = len(actions) + len(validation_dropped)
    actions = [a for a in actions if a["session_id"] in still_eligible]
    cap = int(cfg["max_actions_per_pass"])
    if len(actions) > cap:
        # Same fairness rule as the scheduled path: order by least-recently-acted-on so the
        # cap can't park on one session forever (see orchestrator.run_pass).
        last_seen = orchestrator._last_action_at()
        actions.sort(key=lambda a: last_seen.get(a["session_id"], 0.0))
    actions = actions[:cap]

    recorded: list[dict] = []
    for action in actions:
        card = sent[action["session_id"]]
        state, esc_reason = orchestrator._decide(action, cfg)
        rec: dict = {
            "id": uuid.uuid4().hex,
            # Tier gating applies to a chat instruction exactly as it does to a scheduled
            # pass. The operator asking for something is not itself an approval — they still
            # see what it resolved to and tap, unless the tier says otherwise.
            "state": state,
            "ts": now,
            "expires_at": now + int(cfg["proposal_ttl_minutes"]) * 60,
            "tier": cfg["autonomy"],
            "source": "chat",
            "session_id": action["session_id"],
            "engine": card.get("engine", ""),
            "title": _clamp(card.get("title"), orchestrator.TITLE_MAX),
            "project": _clamp((card.get("project") or {}).get("name"), orchestrator.PROJECT_MAX),
            "project_id": (card.get("project") or {}).get("id") or "",
            **{k: v for k, v in action.items() if k != "session_id"},
        }
        # Same single-writer rule as the scheduled pass (`orchestrator.run_pass`): whatever the
        # spread carried over is discarded, and the decision's own reason is written back.
        rec["authority"] = card["automation_authority"]
        if mission_id:
            rec["mission_id"] = mission_id
        rec.pop("escalation_reason", None)
        if esc_reason:
            rec["escalation_reason"] = esc_reason
        if action["verb"] in orchestrator.DELIVERING_VERBS:
            rec["precondition"] = await asyncio.to_thread(
                orchestrator.precondition_for,
                rec["authority"]["physical_key"],
            )
        elif action["verb"] == "escalate":
            rec["observed_prompt"] = await asyncio.to_thread(
                orchestrator.observed_prompt_for,
                rec["authority"]["physical_key"],
            )
        if turn_id:
            # Durable provenance, MISSION-QUALIFIED. Without the mission half, a recovering turn
            # in mission B can adopt an identically-named turn's actions from mission A — the
            # turn key is (mission_id, turn_id), and half a key is not a key.
            rec["turn_id"] = turn_id
            if mission_id:
                rec["mission_id"] = mission_id
        recorded.append(rec)

    if recorded:
        # THE LINEARIZATION POINT. The receipt commits before the append, so being fenced out is
        # discovered here rather than after the irreversible half has happened. Check-then-append
        # would let a reclaimed caller pass and then write, landing a second instruction that no
        # later fence could withdraw.
        # THE LINEARIZATION POINT, and it is inside the ledger's own lock rather than before it.
        # Reserving first and appending afterwards is still check-then-write: this caller can be
        # reclaimed in the gap and append anyway, landing a second instruction that no later fence
        # can withdraw. Passed as a GATE, it is evaluated in the same hold as the write, so being
        # fenced out means the append never happens at all.
        intended = [str(r.get("id")) for r in recorded if r.get("id")]
        # Whether the GATE refused, recorded by the gate itself. Inferring it from "the batch came
        # back empty" conflates being fenced out with the ordinary case where the ledger drops an
        # action because that session picked up a live one — two different situations, and only
        # one of them means nothing was sent on this turn's behalf.
        refused = {"v": False}

        def _gate() -> bool:
            ok = bool(reserve_write(intended))
            refused["v"] = not ok
            return ok

        gate = _gate if reserve_write is not None else None
        # Use what was actually WRITTEN: the ledger drops any action whose session
        # picked up a live one from a concurrent pass, and the reply must not claim
        # to have queued something that was refused.
        recorded = await asyncio.to_thread(lambda: orchestrator._persist(recorded, gate=gate))
        if refused["v"]:
            return {
                "intent": "instruct",
                "answer": "That turn was taken over by another request; nothing was sent.",
                "actions": [],
                "fenced": True,
            }
    # The model's own phrasing ("On it.", "Nudged both sessions.") describes what it INTENDED,
    # and by here that intention may have been overruled three ways: validation dropped the
    # action (a dead session), the post-call eligibility recheck revoked it, or the ledger
    # refused the slot to a concurrent pass.
    #
    # The rule is one comparison, not a special case per cause: if FEWER actions were recorded
    # than the model asked for, its sentence is a claim about work that did not happen. That
    # covers the partial case too — "Nudged both sessions" over one recorded action is exactly
    # as wrong as it is over none, and it is the more dangerous of the two, because the answer
    # looks corroborated by the action list sitting next to it.
    if intended_n and len(recorded) < intended_n:
        # Say WHICH wall it hit. "Nothing was queued" is honest but useless if the operator
        # cannot tell a policy refusal from a session that simply is not running any more.
        # Only `not_live` gets its own wording. The validator's `stale` reason cannot be
        # reached from here — `eligible_cards` already drops anything past the same
        # STALE_DELIVER_HOURS before the model is called — so a message for it would be
        # untestable text asserting something that never happens.
        not_live = sum(1 for d in validation_dropped if d.get("reason") == "not_live")
        if not recorded:
            answer = (
                "That session isn't running any more — there's no live terminal to type into, "
                "so I didn't queue anything. Open it and it can be picked up again."
                if not_live
                else "I had something to propose, but by the time I'd worked it out those "
                "sessions were no longer mine to act on — orchestration was switched off, they "
                "were excluded, or another pass got there first. Nothing was queued."
            )
        else:
            held = intended_n - len(recorded)
            why = (
                f"{held} isn't running any more, so there's no terminal to type into"
                if not_live >= held
                else f"{held} was no longer mine to act on"
            )
            answer = f"Queued {len(recorded)} of {intended_n} — {why}."
    return {
        "intent": "instruct",
        "answer": answer or ("Queued the action(s) below." if recorded else "Nothing matched."),
        "actions": recorded,
        "matches": [],
    }
