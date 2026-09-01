"""The follow-through supervisor: nudge against the OBJECTIVES, and know when to stop (#885).

This is the part of MISSION CONTROL the operator actually asked for — "agents stop or pause on
mundane things, or a PR fails, or the session stalls, and nothing picks it up". Everything else in
#840 exists so this module has something true to reason about.

**The budget is DERIVED, never counted.** A counter incremented at send time is wrong on one side
of a crash or the other: increment before the ledger append and the action may never land;
increment after and the crash loses the charge. So the store records WHICH action each nudge was
(`mission_supervisor_actions`), and the charge is read back from that action's terminal state in
the ledger. A restart after either side of the write yields the same number, because the number
was never stored.

**What a unit is bought with**, and the asymmetry is the point:

* ``delivered`` — bytes reached the agent. Charge 1. This is the only outcome that says the agent
  was actually told something.
* ``refused`` / ``stale`` / ``failed`` / ``expired`` / ``rejected`` — nothing reached the agent
  (a viewer was at the keyboard, policy withdrew, the session died). Charge 0. Charging these
  would drain a budget without anything being said, then escalate as though the agent had ignored
  three nudges — a lie about the agent rather than a report about it.
* ``indeterminate`` — the write may or may not have landed and nobody can say. Charge 1 **and
  stop**: a retry could be the second copy of an instruction the agent already has, and
  "delivered twice" is worse than "not delivered".

**The reset boundary is the EPISODE, not the input.** A budget resets when the objective's own
state changes — never on input churn — which is why the episode is a durable row rather than a
derivation. Re-deriving after progress would still see the earlier nudges.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid

from . import missions
from . import orchestrator_ledger as ledger

log = logging.getLogger(__name__)

#: Nudges per objective EPISODE before the supervisor stops and asks the operator. #840 said "a
#: nudge budget per objective, then it asks" without a number; three is the smallest count that
#: can distinguish "the agent needed a reminder" from "the agent is not moving", which is the
#: distinction the escalation is about.
NUDGE_BUDGET = 3

#: Terminal states that mean the bytes reached the agent. Only these cost a unit.
_CHARGED: frozenset[str] = frozenset({"delivered"})
#: Terminal, unknowable, and therefore END the automatic attempts as well as costing a unit.
_INDETERMINATE = "indeterminate"
#: Terminal states that cost the episode NOTHING — the attempt provably never became agent input.
#: `rejected` is the operator declining it, `failed`/`stale`/`expired` are the action dying before
#: delivery. The budget already ignores them (only `_CHARGED` and indeterminacy count), and the
#: RESERVATION has to agree: a binding left behind for one of these consumed a slot the budget says
#: is free, so three rejections could block a fourth attempt while `may_nudge` still said yes
#: (#888 review, finding 2).
_FREE_TERMINAL: frozenset[str] = frozenset({"failed", "rejected", "stale", "expired", "observed"})


def budget_state(
    mission_id: str,
    objective_key: str,
    *,
    episode: int | None = None,
    path=None,
) -> dict:
    """What this objective episode has actually spent, derived from the ledger.

    Returns ``{episode, spent, remaining, live, indeterminate, unreadable}``.

    ``unreadable`` is its own answer and not a zero: a ledger that will not read has not told us
    the budget is fresh, and treating it as fresh would hand a stalled objective an unlimited
    supply of nudges every time the store hiccups. The caller stands down on it.

    ``live`` counts actions that have not settled yet. They are NOT charged — an action still in
    flight has not told the agent anything — but they mean the pass must not send another one, or
    two nudges race into the same session.
    """
    if episode is None:
        episode, _ = missions.objective_episode(mission_id, objective_key, path=path)
    ids = missions.supervisor_action_ids(mission_id, objective_key, episode, path=path)
    if not ids:
        return {
            "episode": episode,
            "spent": 0,
            "remaining": NUDGE_BUDGET,
            "live": 0,
            "indeterminate": False,
            "unreadable": False,
        }

    # Bulk tri-state read of the WHOLE ledger, then look our ids up in it. The checked
    # reader is what distinguishes "this action is absent" from "the file would not
    # open", and those mean opposite things for a budget.
    status, latest = ledger.latest_by_id_checked()
    if status != "ok":
        return {
            "episode": episode,
            "spent": 0,
            "remaining": 0,
            "live": 0,
            "indeterminate": False,
            "unreadable": True,
        }

    # RECONCILE AS WE READ. The reservation counts bindings, and the budget counts ledger
    # outcomes; the two only agree if a binding whose action provably cost nothing is let go. This
    # is the one place that already holds both facts — the binding ids and their ledger states — so
    # it is where the two are reconciled. Forgetting is safe for exactly these states and no
    # others: each is terminal AND definitively not delivered, so there is nothing to account for.
    freeable = [
        aid for aid in ids if str((latest.get(aid) or {}).get("state") or "") in _FREE_TERMINAL
    ]
    if freeable:
        released: set[str] = set()
        for aid in freeable:
            # RECORD BEFORE RELEASE. The binding is the pending intent for the operator-visible
            # "held" record (see `nudge`), so letting it go before that record exists is exactly
            # how a transient store failure turned into permanent silence. `ensure_held_event` is
            # idempotent by `action_id`, so this is safe to reach on every read and cannot produce
            # a second event for an action whose record already landed.
            try:
                rec = latest.get(aid) or {}
                missions.ensure_held_event(
                    mission_id,
                    action_id=aid,
                    session_key=str(rec.get("session_id") or ""),
                    text=(
                        "A nudge was prepared but not delivered: "
                        f"{rec.get('error') or 'the delivery settled as ' + str(rec.get('state'))}"
                    ),
                    meta={
                        "source": "supervisor",
                        "objective_key": objective_key,
                        "episode": episode,
                        "held": True,
                        "state": str(rec.get("state") or ""),
                    },
                    path=path,
                )
                missions.forget_supervisor_action(mission_id, aid, path=path)
                released.add(aid)
            except Exception:  # noqa: BLE001 — keep the binding; the next read retries
                log.debug("mission %s: could not settle held action %s", mission_id, aid)
        ids = [aid for aid in ids if aid not in released]

    spent = live = 0
    indeterminate = False
    for aid in ids:
        state = str((latest.get(aid) or {}).get("state") or "")
        if not state:
            # Recorded as sent, absent from the ledger. Indistinguishable from an append that
            # landed and was lost, so it is treated the same way an `indeterminate` is: charged,
            # and the automatic attempts end.
            spent += 1
            indeterminate = True
        elif state in ledger.LIVE_STATES:
            live += 1
        elif state == _INDETERMINATE:
            spent += 1
            indeterminate = True
        elif state in _CHARGED:
            spent += 1
    return {
        "episode": episode,
        "spent": spent,
        "remaining": max(0, NUDGE_BUDGET - spent),
        "live": live,
        "indeterminate": indeterminate,
        "unreadable": False,
    }


def may_nudge(mission_id: str, objective_key: str, *, path=None) -> tuple[bool, str]:
    """``(allowed, why_not)`` for one more automatic nudge against this objective.

    Every refusal names itself, because "the supervisor did nothing" is the state the operator
    complained about and an unexplained silence is indistinguishable from a broken feature.
    """
    episode, stood_down = missions.objective_episode(mission_id, objective_key, path=path)
    if stood_down:
        return False, "the operator asked not to be told about this objective again"
    b = budget_state(mission_id, objective_key, episode=episode, path=path)
    if b["unreadable"]:
        return False, "the action ledger could not be read, so the budget is unknown"
    if b["indeterminate"]:
        return False, "a previous nudge may or may not have been delivered; not sending another"
    if b["live"]:
        return False, "a previous nudge has not settled yet"
    if b["remaining"] <= 0:
        return False, f"the {NUDGE_BUDGET}-nudge budget for this episode is spent"
    return True, ""


# ---------------------------------------------------------------- the mechanical pass


#: A session whose engine store has not grown since dispatch is STUCK, not running. #840 §9.6:
#: "alive" is a fact about a process, and the operator's actual question is whether the agent is
#: doing anything. A first-run prompt, an auth wall or a trust dialog all look identical to a
#: healthy session from the outside, and all of them produce this.
STALL_AFTER_S = 600.0


def _objective_rows(mission_id: str, *, path=None) -> list[dict]:
    try:
        return missions.objectives(mission_id, path=path)
    except Exception:  # noqa: BLE001 — a mission with no objectives is not an error here
        return []


def assess(mission_id: str, *, now: float | None = None, path=None) -> dict:
    """The MECHANICAL half. No model call, and it runs on EVERY pass.

    Returns ``{objectives: [...], likely_done: bool, blocked: [...], unmet_gates: int}``.

    **This half is deliberately not gated on the input fingerprint**, and that is the trap #885
    names: the fingerprint says whether the reviewable CONTENT changed, and the thing that most
    needs noticing — the final gate becoming met — is a change to the OBJECTIVE store, not to the
    transcript. Gating this on the fingerprint means a mission finishes and nobody says so.

    It is also what makes the model half affordable: by the time a model is asked anything, the
    cheap facts are already established, so the prompt is small and the answer is checkable.
    """
    ts = time.time() if now is None else now
    rows = _objective_rows(mission_id, path=path)
    out: list[dict] = []
    unmet_gates = 0
    for o in rows:
        key = str(o.get("key") or "")
        gate = bool(o.get("gate"))
        state = str(o.get("state") or "")
        met = state in ("met", "waived")
        if gate and not met:
            unmet_gates += 1
        episode, stood_down = missions.objective_episode(mission_id, key, path=path)
        b = budget_state(mission_id, key, episode=episode, path=path)
        allowed, why = may_nudge(mission_id, key, path=path)
        out.append(
            {
                "key": key,
                "title": o.get("title"),
                "gate": gate,
                "state": state,
                "met": met,
                "episode": episode,
                "stood_down": stood_down,
                "spent": b["spent"],
                "remaining": b["remaining"],
                "may_nudge": allowed,
                "why_not": why,
                # The DISCRIMINATORS, carried structurally rather than left to be inferred from
                # `remaining`. An unreadable ledger reports `remaining: 0` because no budget can be
                # justified — but that is "unknown", not "spent", and a consumer that reads only the
                # number cannot tell the two apart. It made the console label an unreadable ledger
                # SPENT while the sentence beside it said the budget was unknown, and it is also
                # what decides whether a refusal is TERMINAL (escalate) or merely current (wait).
                "unreadable": bool(b["unreadable"]),
                "indeterminate": bool(b["indeterminate"]),
                "live": int(b["live"]),
                "terminal": bool(
                    not b["unreadable"] and (b["indeterminate"] or b["remaining"] <= 0)
                ),
            }
        )
    return {
        "objectives": out,
        # `likely_done` is a PROPOSAL, never a close: nothing here marks anything met, and a
        # mission with no objectives is not "done" — it is unmeasured, which is a different fact.
        "likely_done": bool(rows) and unmet_gates == 0,
        "unmet_gates": unmet_gates,
        "checked_at": ts,
    }


def session_is_stalled(
    engine: str,
    native_id: str,
    *,
    since: float,
    baseline_mark: int | None = None,
    now: float | None = None,
    home=None,
) -> tuple[bool, str, int | None]:
    """Has this session's transcript GROWN? `(stalled, detail, mark_now)`.

    **Alive is not started, and started is not still going.** The process being up says nothing: a
    session sitting on a trust dialog, an auth wall or a first-run prompt is alive, silent and
    going nowhere. Neither does "has written something": a session that emits one startup turn and
    then hangs on exactly such a prompt had a turn, and the first version of this returned
    not-stalled for it forever (#888 review, finding 6).

    So the test is GROWTH against a baseline, not existence. `since` is when the count was last
    seen to move — the session's own dispatch time on the first pass, its last observed change
    afterwards — and `baseline_mark` is what it was then. `mark_now` is returned so the caller
    can persist a new baseline when it moves.
    """
    from pathlib import Path as _P

    from . import transcript

    ts = time.time() if now is None else now
    root = home or _P.home()

    # THE MARK IS THE ENGINE'S OWN MONOTONIC MEASURE, not a rendered turn count.
    #
    # The renderers cap at `DEFAULT_MAX_MESSAGES` (2000), so a busy session pinned at the cap has
    # a count that stops moving while the session is perfectly healthy — and would be reported
    # stalled forever, which is the opposite of what this function is for. Each engine registers
    # what actually grows for it: a file size for the file-backed ones, this session's own row
    # count for opencode, whose conversation is rows in a shared database.
    # The engine's OWN monotonic measure. Not the locator: that returns prose for the operator —
    # opencode's is the shared database plus a query, because there is no per-session file — so
    # sizing it always failed there and silently fell back to the capped count. Every file-backed
    # engine took the other branch, which is why it looked correct (#888 review, finding 3).
    mark = transcript.growth_mark(engine, native_id, root)

    if mark is None:
        # No locator, or the store is not a file we can size. Fall back to the adapter, which at
        # least distinguishes "wrote nothing" from "wrote something" — and say which signal was
        # used, because the two have different blind spots and a reader deserves to know.
        adapter = transcript.adapter_for(engine)
        if adapter is None:
            # No adapter and no locator means no evidence either way — `shell` has no transcript
            # at all. Never reported as stalled: an engine we cannot measure is not an engine that
            # has stopped.
            return False, f"no transcript signal for {engine}", None
        try:
            mark = len(list(adapter(native_id, root)))
        except (OSError, ValueError) as e:
            return False, f"store unreadable: {type(e).__name__}: {e}", None

    base = 0 if baseline_mark is None else int(baseline_mark)
    if mark > base:
        return False, f"the transcript grew ({base} -> {mark})", mark
    age = ts - since
    if age >= STALL_AFTER_S:
        what = "written nothing" if mark == 0 else "added nothing to its transcript"
        return True, f"the session has {what} in {int(age)}s", mark
    return False, f"no growth yet, {int(age)}s in", mark


# ---------------------------------------------------------------- the model half


async def consider(mission_id: str, session_key: str, *, path=None) -> dict:
    """The MODEL half — gated on the input fingerprint, and it proposes rather than acts.

    Returns ``{"skipped": reason}`` or ``{"recap", "assessment", "nudge", "input_fp"}``.

    **Gated, unlike `assess`.** Asking a model to summarise the same unchanged transcript on every
    sweep costs the operator money to be told nothing twice. `gather_input` already returns a
    fingerprint that moves exactly when the reviewable content moves, and the checkpoint records
    the last one — so an unchanged session skips the call entirely. `assess` deliberately does NOT
    share this gate: an objective becoming met moves no fingerprint at all.

    **It proposes; it does not send.** The reply names WHICH objective to nudge about, by key,
    from the list it was given. The bytes come from `actuator.render` against the existing verb,
    so there is no path from this model's text to a PTY — the same shape that keeps
    `mission_objectives` from authoring a probe target.

    Nothing here marks an objective met. `likely_done` is a proposal that the gates LOOK
    satisfied, and the gates are settled by observation.
    """
    from . import prompts, review

    try:
        review._require_config()
    except review.NotConfiguredError:
        # An unconfigured install is a choice, not a fault: the mechanical half still ran, so the
        # console keeps working and the operator is simply not paying for a recap.
        return {"skipped": "no AI endpoint is configured"}

    a = assess(mission_id, path=path)
    try:
        body, input_fp = await missions.run_admitted(
            lambda: review.gather_input(session_key, _INPUT_MAX)
        )
    except review.ReviewError as e:
        return {"skipped": f"nothing to review: {e}"}

    checkpoint = missions.supervisor_checkpoint(mission_id, session_key=session_key, path=path)
    if checkpoint.get("input_fp") == input_fp:
        return {"skipped": "the session has not changed since the last recap", "input_fp": input_fp}

    row = missions.get_mission(mission_id, path=path) or {}
    checklist = (
        "\n".join(
            f"- {o['key']}: {o['title']} — {o['state']}"
            + ("  [GATE]" if o["gate"] else "")
            + (f"  [nudges spent {o['spent']}/{NUDGE_BUDGET}]" if o["spent"] else "")
            for o in a["objectives"]
        )
        or "(no objectives)"
    )

    reply = await review.complete_json(
        [
            {"role": "system", "content": prompts.effective("mission_supervisor")},
            {
                "role": "user",
                "content": (
                    f"Instruction:\n{row.get('instruction') or row.get('title') or ''}\n\n"
                    f"Objectives:\n{checklist}\n\n"
                    f"Session:\n{body}"
                ),
            },
        ]
    )
    return _reading(reply if isinstance(reply, dict) else {}, a, input_fp)


#: What the session view is worth to this decision. Bounded because the supervisor runs per
#: mission on an interval, and an unbounded tail is what turns "watch the fleet" into a bill.
_INPUT_MAX = 6000

_ASSESSMENTS = frozenset({"on_track", "blocked", "needs_approval", "stalled", "likely_done"})


def _reading(reply: dict, a: dict, input_fp: str) -> dict:
    """Narrow the model's answer to something the caller may act on. Drops, never repairs.

    A `nudge` naming an objective that is not on the checklist is DROPPED rather than mapped to
    the nearest one: the model choosing which objective to nudge about is the whole of its
    authority here, and quietly substituting a different objective would exceed it.
    """
    keys = {o["key"] for o in a["objectives"]}
    assessment = reply.get("assessment")
    if assessment not in _ASSESSMENTS:
        assessment = "on_track"
    nudge = reply.get("nudge")
    if isinstance(nudge, dict) and str(nudge.get("objective_key") or "") in keys:
        nudge = {
            "objective_key": str(nudge["objective_key"]),
            "why": str(nudge.get("why") or "")[:400],
        }
    else:
        nudge = None
    recap = reply.get("recap")
    return {
        "recap": str(recap)[:2000] if isinstance(recap, str) else "",
        "assessment": assessment,
        "nudge": nudge,
        "input_fp": input_fp,
        # The mechanical reading rides along, so a caller never has to ask the model what the
        # store already knows.
        "likely_done": a["likely_done"],
    }


# ---------------------------------------------------------------- acting on the reading


#: The ONLY verb the supervisor may mint. `continue` is "carry on with what you were doing", which
#: is the whole of a nudge. `choose` and `answer` are excluded deliberately: they type an ANSWER
#: into a prompt, and a supervisor that guessed which option an agent should pick would be making
#: the operator's decision for them. `needs_approval` therefore escalates — it never becomes bytes.
NUDGE_VERB = "continue"


def _authority_state(mission_id: str, objective_key: str, *, session_key: str, path=None) -> tuple:
    """Every fact an automatic nudge rests on, as ONE comparable value.

    This is the single source both fences use, and that is the whole point of extracting it:
    `_still_authorized` turns it into a verdict with a sentence, and the delivery path folds the
    same tuple into the fingerprint the write fence re-reads immediately before byte one. A
    predicate and a fingerprint that were written separately would eventually disagree about what
    authorizes a write — one would gain a term the other never learned about.
    """
    # ONE transaction in the store, not three reads here. Assembled field by field, a detach
    # landing between the holder read and the objective read produced a tuple identical to the
    # pre-detach one — invisible to the fence's comparison, which is worse than a stale value.
    return missions.supervisor_authority(
        mission_id, objective_key, session_key=session_key, path=path
    )


def _still_authorized(
    mission_id: str, objective_key: str, *, session_key: str, episode: int | None = None, path=None
) -> tuple[bool, str]:
    """Is this write STILL authorized, right now? `(ok, why_not)`.

    EVERYTHING an automatic nudge rests on, in one predicate, so the two fences that need it cannot
    drift apart. Called once before the durable append and again — through
    `actuator.deliver_auto(extra_authority=...)` — under the final write fence, because the window
    between them contains a precondition capture, a ledger append and a lock queue, and an operator
    can act in any of it.

    The mission-ownership half is the one that makes this a boundary rather than a nicety: a
    session detached from this mission and adopted by another is no longer ours to type into, and
    a roster snapshot taken at the top of the pass cannot see that happen (#888 review, finding 1).

    Each refusal is its own sentence because the operator acts differently on each.
    """
    return missions.supervisor_authority_verdict(
        mission_id,
        _authority_state(mission_id, objective_key, session_key=session_key, path=path),
        episode=episode,
    )


async def nudge(
    mission_id: str,
    *,
    session_key: str,
    objective_key: str,
    why: str,
    registry=None,
    path=None,
) -> dict:
    """Send ONE nudge against one objective, through the existing verb path.

    **No new actuator entry point** (#885). The action is minted the way `orchestrator_chat` mints
    an `instruct`, persisted through the same check-and-append that enforces one live action per
    session, and delivered through `deliver_auto` — so it inherits the tier check, the viewer-busy
    rule, the precondition, the single-writer fence and the mission fence without any of them
    being restated here. A second delivery path is a second place for those to be forgotten.

    The binding is written BEFORE the action is persisted. That ordering is what makes the budget
    survive a crash: a binding with no ledger row reads as `indeterminate` (charged, and the
    automatic attempts stop), which is the honest answer for a write nobody can account for.
    """
    from . import actuator, engines, orchestrator, prefs

    # RE-AUTHORIZE AT THE WRITE BOUNDARY, not at the read that produced the proposal. Everything
    # this checks was already true when `assess()` ran, but a model call and a delivery wait sit
    # between then and here, and the operator can waive, drop, settle or stand the objective down
    # inside that window. Re-reading the policy at the point of the write is the same rule the
    # orchestrator learned the hard way; a proposal minted under a policy that has since changed
    # must not become bytes.
    ok, why_stale = _still_authorized(mission_id, objective_key, session_key=session_key, path=path)
    if not ok:
        return {"sent": False, "why": why_stale}

    allowed, refusal = may_nudge(mission_id, objective_key, path=path)
    if not allowed:
        return {"sent": False, "why": refusal}

    episode, _ = missions.objective_episode(mission_id, objective_key, path=path)
    cfg = prefs.get_orchestrator()
    action_id = uuid.uuid4().hex
    now = time.time()
    rec = {
        "id": action_id,
        "state": "approved" if cfg.get("autonomy") == "yolo" else "proposed",
        "verb": NUDGE_VERB,
        "session_id": session_key,
        "source": "supervisor",
        "mission_id": mission_id,
        "objective_key": objective_key,
        # THE EPISODE THIS WAS MINTED FOR, on the durable record. Without it a proposal made in
        # episode 1 stays deliverable in episode 2 — and a drop plus a re-add of the same key
        # recreates an objective the proposal was never about. Mission, objective and session
        # together do not identify the incarnation; the episode is what does (#888 review).
        "objective_episode": episode,
        "title": why[:200],
        "confidence": 1.0,
        "ts": now,
        "expires_at": now + int(cfg.get("proposal_ttl_minutes") or 30) * 60,
        "tier": cfg.get("autonomy"),
    }
    rec["precondition"] = await missions.run_admitted(
        lambda: orchestrator.precondition_for(engines.physical_key(session_key))
    )

    # RE-AUTHORIZE AT THE DURABLE APPEND FENCE. The early check ran before `precondition_for`,
    # which is an external read that can take real time — and ownership can move inside it. Hermes'
    # probe detached the session and adopted it into another mission during exactly that call, and
    # the action was still persisted. Two fences, not one: this one stops the durable write, and
    # `_nudge_authority` below stops the bytes.
    ok, why_stale = _still_authorized(
        mission_id, objective_key, session_key=session_key, episode=episode, path=path
    )
    if not ok:
        return {"sent": False, "why": why_stale}

    # BINDING FIRST — see the docstring. A crash between here and the append leaves a charge that
    # reads `indeterminate`, which is the truthful answer, rather than a nudge nobody counted.
    #
    # It is ALSO the reservation. `may_nudge` above is a read, and a read followed by a write is a
    # check-then-act: two overlapping passes both saw "one left" and both sent, delivering four
    # nudges against a budget of three. `max_per_episode` makes the count and the insert one
    # transaction, so the loser is told here instead of finding out from a written ledger.
    reserved = await missions.run_admitted(
        lambda: missions.record_supervisor_action(
            mission_id,
            session_key=session_key,
            objective_key=objective_key,
            episode=episode,
            action_id=action_id,
            max_per_episode=NUDGE_BUDGET,
            path=path,
        )
    )
    if not reserved:
        return {
            "sent": False,
            "why": f"the {NUDGE_BUDGET}-nudge budget for this episode is spent",
        }
    try:
        kept = await missions.run_admitted(lambda: orchestrator._persist([rec]))
    except BaseException:
        # AMBIGUOUS. Nobody can say whether the append landed, so the binding STAYS and reads
        # `indeterminate` on the next pass — charged, and the automatic attempts stop. This is the
        # fail-closed half, and it is the reason the binding is written first.
        raise
    if not kept:
        # KNOWN REFUSAL, which is the opposite case: `_persist` returned normally and kept nothing,
        # so the ledger definitively did not take the action. Leaving the binding would make the
        # next budget read see an id with no ledger row, treat it as indeterminate, and end the
        # automatic attempts for an entire episode over a write we KNOW never happened. Forgetting
        # it is not a weakening of fail-closed: fail-closed is for what cannot be determined, and
        # this outcome is determined.
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.forget_supervisor_action(mission_id, action_id, path=path)
            )
        return {"sent": False, "why": "the session already had a live action", "id": action_id}

    # THE FINAL FENCE is not passed in from here any more. The action record carries
    # `source=supervisor`, `mission_id` and `objective_key`, and `actuator.deliver` derives the
    # authority from THAT — so every delivery path enforces it, including the operator tapping
    # approve on a proposal minted minutes ago in suggest mode. Handing callbacks to this one call
    # protected only this one call: propose -> detach -> approve went straight through
    # (#888 review, finding 1).
    settled = await actuator.deliver_auto(kept[0], registry=registry)
    # `deliver_auto` returns the SETTLED ledger row for a refusal as well as a delivery — a
    # viewer-busy hold comes back as an ordinary `stale`/`failed` record, not as `None`. Reducing
    # that to `is not None` reported a refusal as a successful nudge, and the sweep then counted it.
    # Only `delivered` is a delivery.
    state = str((settled or {}).get("state") or "")
    sent = state == "delivered"
    out = {"sent": sent, "id": action_id, "episode": episode, "state": state or "not delivered"}
    if not sent:
        why = str((settled or {}).get("error") or "") or f"the delivery settled as {out['state']}"
        out["why"] = why
        # The hold has to be LEGIBLE, which is #885's actual requirement — a nudge that silently
        # did not happen is the "supervisor did nothing" complaint in another costume. The budget
        # is untouched: `budget_state` charges only `delivered`, and the reconciliation releases
        # the binding once the row is terminal-and-undelivered.
        # RECOVERABLE, not best-effort and not fatal. #885 requires a viewer hold to "say so in
        # the thread", and there is nothing to make this atomic with — the ledger row lives in a
        # different store. So the SUPERVISOR BINDING is the pending intent: it is written before
        # the append and released only once the record exists (see `budget_state`), which means a
        # transient store failure delays the operator's record rather than losing it, and the
        # append is idempotent by `action_id` so a retry cannot double it.
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.ensure_held_event(
                    mission_id,
                    action_id=action_id,
                    session_key=session_key,
                    text=f"A nudge was prepared but not delivered: {why}",
                    meta={
                        "source": "supervisor",
                        "objective_key": objective_key,
                        "episode": episode,
                        "held": True,
                        "state": out["state"],
                    },
                    path=path,
                )
            )
    return out


async def escalate(
    mission_id: str,
    *,
    session_key: str,
    objective_key: str,
    reason: str,
    path=None,
) -> bool:
    """Record the terminal escalation for this objective episode. True iff THIS pass won it.

    The uniqueness constraint arbitrates, so two overlapping passes cannot both announce. The
    caller notifies only on a True — announcing a row somebody else wrote is how one stall
    becomes three notifications.

    **The timeline event is written by `escalate_once` itself**, inside the same transaction as the
    arbitration row. It used to be appended here afterwards under `suppress`, which was a one-way
    trap: the unique row was already committed, so a failed append could never be repaired by a
    later pass — uniqueness had permanently closed the door on the only record the operator sees.
    """
    return bool(
        await missions.run_admitted(
            lambda: missions.escalate_once(
                mission_id,
                session_key=session_key,
                objective_key=objective_key,
                episode=missions.objective_episode(mission_id, objective_key, path=path)[0],
                reason=reason,
                path=path,
            )
        )
    )


# ---------------------------------------------------------------- one pass over one mission


#: Mission states the supervisor leaves alone. A closed mission needs no follow-through, and a
#: `draft` has not been dispatched yet — nudging a session it does not have would be nonsense.
_ELIGIBLE_STATES: frozenset[str] = frozenset({"running", "review"})


def _settled_by(o: dict) -> str:
    """WHY this objective counts as settled — the fact, not the flag.

    `waived` is called out separately from `met` on purpose: a waiver is the operator deciding the
    objective was not required, which is a different kind of claim from a probe having observed it
    hold, and a proposal that blurred them would overstate what was actually verified.
    """
    state = str(o.get("state") or "")
    if state == "met":
        return "observed to hold"
    if state == "waived":
        return "waived by the operator — not verified"
    return state or "unknown"


def _render_completion(rows: list[dict]) -> tuple[str, dict]:
    """Build the proposal from the objective rows the STORE read, not from an earlier assessment.

    Called inside `propose_completion`'s transaction, so the artifact enumerates the objectives as
    they are at the moment the mission moves — every current objective, and each one's current
    settlement fact. The caller supplies the wording; the store supplies what it is about.
    """
    objectives = [
        {
            "key": r.get("key"),
            "title": r.get("title"),
            "gate": bool(r.get("gate")),
            "state": r.get("state"),
        }
        for r in rows
    ]
    meta = {
        "source": "supervisor",
        "proposal": True,
        "objectives": [{**o, "settled_by": _settled_by(o)} for o in objectives],
    }
    return _completion_text(objectives), meta


def _completion_text(objectives: list[dict]) -> str:
    """The proposal, as the operator reads it in the timeline."""
    lines = ["Every gate is met. This looks finished — nothing has been closed."]
    for o in objectives:
        mark = "gate" if o.get("gate") else "goal"
        lines.append(f"- [{mark}] {o.get('title') or o.get('key')}: {_settled_by(o)}")
    return "\n".join(lines)


async def run_pass(mission_id: str, *, registry=None, path=None) -> dict:
    """One supervisor pass over one mission. Returns what it did, in the operator's terms.

    The order is the design, and each step is placed where it is for a reason:

    1. **Mechanical assessment**, always. It costs nothing and it is what notices a gate becoming
       met — a change no fingerprint can see.
    2. **Completion proposal** when every gate is satisfied, reached WITHOUT a model call: the
       checklist finishing is exactly the moment the input is most likely to be unchanged, so
       gating it on the fingerprint would swallow it. The gates are re-read inside the committing
       transaction, and the transition and its artifact land together or not at all.
    3. **Stop if the mission already needs the operator.** A pending decision, an open question or
       a flagged intervention means a human is mid-conversation with this mission, and `continue`
       would talk over the question it is waiting on. Read from the durable sources, before any
       model call or write.
    4. **Currently-held sessions only, and all of them.** The roster carries released sessions, and
       one that has been detached is no longer ours to type into. Each held session then gets its
       own model half, its own checkpoint and its own nudge decision (`_pass_one_session`).
    5. **The model half**, gated per session. Skipped when nothing that session's model reads moved.
    6. **At most one nudge**, through the existing verb path — unless the session is stalled or the
       model asked for approval, both of which escalate instead of writing.
    7. **Escalation** when a refusal is TERMINAL (exhausted or indeterminate, never merely live or
       unreadable) — once per objective episode, arbitrated by the database.

    Overlapping passes cannot double-recap, double-charge or double-announce: the recap advances
    with the fingerprint in one transaction, the nudge budget is reserved in the same transaction
    that binds the action, and the escalation is a unique row on `(mission, objective, episode)`.
    """
    row = await missions.run_admitted(lambda: missions.get_mission(mission_id, path=path))
    if row is None:
        return {"skipped": "unknown mission"}
    state = str(row.get("state") or "")
    if state not in _ELIGIBLE_STATES:
        return {"skipped": f"mission is {state}"}

    a = await missions.run_admitted(lambda: assess(mission_id, path=path))
    out: dict = {
        "assessment": None,
        "nudged": None,
        "escalated": None,
        "objectives": a["objectives"],
    }

    # (2) The completion proposal is MECHANICAL and comes before the gate, deliberately.
    if a["likely_done"]:
        out["likely_done"] = True
        if state != "review":
            # ONE transaction: re-verify the gates, flip the state, post the artifact. Separately
            # they had two failure windows — a gate added between the assessment and the flip still
            # went to review carrying the stale checklist, and an append that failed after the flip
            # left a mission in review with no proposal that no later pass could repair, because
            # the CAS had already won (#888 review, finding 5).
            moved = False
            with contextlib.suppress(Exception):
                moved = bool(
                    await missions.run_admitted(
                        lambda: missions.propose_completion(
                            mission_id,
                            from_state=state,
                            render=_render_completion,
                            path=path,
                        )
                    )
                )
            out["proposed_review"] = moved
        return out

    # (3) THE OPERATOR IS ALREADY BEING ASKED SOMETHING. A mission with a pending decision, an open
    # question or a flagged intervention is waiting on a human, and typing `continue` into it talks
    # over the very question it is waiting on. This is read from the durable attention sources
    # BEFORE any model call or PTY write, not inferred from the model's own opinion.
    needs = await missions.run_admitted(lambda: missions.derive_needs_you([mission_id], path=path))
    attention = (needs or {}).get(mission_id) or {}
    if attention.get("needs_you"):
        out["skipped"] = "the mission needs the operator"
        # `derive_needs_you` spells the reasons `why`; reading `needs_you_why` here made
        # this field silently always empty.
        out["needs_you_why"] = attention.get("why") or []
        return out

    # (3b) CURRENTLY-HELD SESSIONS ONLY. `get_mission` returns the complete historical roster, so
    # a detached session — one this mission released, possibly since adopted by another mission —
    # is still in the list. Nudging it would send autonomous input into a session this mission no
    # longer owns, which is an ownership boundary, not a tidiness point. `removed_at` is the fence.
    sessions = [
        str(sk)
        for sk in (
            srow.get("session_key")
            for srow in (row.get("sessions") or [])
            if srow.get("removed_at") is None
        )
        if sk
    ]
    if not sessions:
        return {**out, "skipped": "the mission holds no session"}

    # (3c) EVERY held session, not just the first. A mission with two live agents had exactly one
    # of them supervised, and the other was invisible to recap, nudge and escalation alike.
    out["sessions"] = sessions
    per_session: list[dict] = []
    # AT MOST ONE NUDGE PER PASS, across every held session — the step this function documents,
    # which the multi-session loop had quietly turned into one-per-session. The budget is
    # objective-level, so three sessions all proposing the same unmet objective could each reserve
    # and deliver a unit and exhaust the whole episode in a single five-minute pass.
    #
    # Recap and assessment still run for EVERY session: those are reads, they cost the operator
    # nothing beyond a model call already justified by a moved fingerprint, and suppressing them
    # would make the second session's transcript invisible. It is only ACTUATION that stops.
    actuated = False
    for session_key in sessions:
        res = await _pass_one_session(
            mission_id,
            session_key=session_key,
            row=row,
            a=a,
            registry=registry,
            path=path,
            may_actuate=not actuated,
        )
        per_session.append(res)
        if (res.get("nudged") or {}).get("sent") or res.get("escalated"):
            actuated = True
    out["per_session"] = per_session
    for r in per_session:
        if r.get("nudged") and r["nudged"].get("sent"):
            out["nudged"] = r["nudged"]
            break
    for r in per_session:
        if r.get("escalated"):
            out["escalated"] = r["escalated"]
            break
    for r in per_session:
        if r.get("assessment"):
            out["assessment"] = r["assessment"]
            break
    return out


async def _pass_one_session(
    mission_id: str,
    *,
    session_key: str,
    row: dict,
    a: dict,
    registry=None,
    path=None,
    may_actuate: bool = True,
) -> dict:
    """One held session's half of the pass. Split out so every session gets the same treatment.

    `may_actuate` is False once an earlier session in this pass has already nudged or escalated.
    The reading still happens — recaps are per session and cost nothing extra — but nothing is
    written, because the budget being spent is the OBJECTIVE'S and it is shared by every session
    on the mission.

    Each session checkpoints independently — a shared checkpoint would let one session's unchanged
    input suppress the model half for a different session that had moved.
    """
    from . import engines

    out: dict = {
        "session_key": session_key,
        "assessment": None,
        "nudged": None,
        "escalated": None,
    }

    # (3d) STALLED? A session whose engine store has not grown since it was dispatched is stuck,
    # not working — a trust dialog, an auth wall or a first-run prompt all look identical to a
    # healthy session from outside. This was defined and then never consulted, so the whole
    # "alive but wrote nothing" class silently did not exist.
    stalled = False
    with contextlib.suppress(Exception):
        engine, native = engines.parse_key(session_key)
        cp = await missions.run_admitted(
            lambda: missions.supervisor_checkpoint(mission_id, session_key=session_key, path=path)
        )
        # The baseline is THIS SESSION'S dispatch time, not the mission's `created_at`. A session
        # adopted onto an old mission would otherwise be judged against a clock that started long
        # before it existed and be called stalled on its first pass, with no grace at all.
        added_at = 0.0
        for srow in row.get("sessions") or []:
            if srow.get("session_key") == session_key and srow.get("removed_at") is None:
                added_at = float(srow.get("added_at") or 0.0)
                break
        since = float(cp.get("growth_at") or 0.0) or added_at or float(row.get("created_at") or 0.0)
        stalled, since_what, mark_now = session_is_stalled(
            engine.engine_id,
            native,
            since=since,
            baseline_mark=cp.get("growth_mark"),
        )
        if mark_now is not None and mark_now != (cp.get("growth_mark") or 0):
            # It MOVED — record the new baseline, so the stall clock restarts from progress
            # rather than from dispatch.
            await missions.run_admitted(
                lambda: missions.note_growth(
                    mission_id, session_key=session_key, mark=mark_now, path=path
                )
            )
        if stalled:
            out["stalled"] = since_what

    reading = await consider(mission_id, session_key, path=path)
    out["assessment"] = reading.get("assessment")
    if reading.get("skipped"):
        out["skipped_model"] = reading["skipped"]
    elif reading.get("input_fp"):
        # The recap and the checkpoint move together, so a crash between them cannot lose the
        # recap and then read the input as unchanged.
        await missions.run_admitted(
            lambda: missions.advance_checkpoint(
                mission_id,
                session_key=session_key,
                input_fp=reading["input_fp"],
                recap_text=reading.get("recap") or "",
                recap_meta={"assessment": reading.get("assessment")},
                path=path,
            )
        )

    # (4) `needs_approval` NEVER becomes bytes — it is the operator's decision, so it escalates.
    #
    # Dropping the proposal is only half of that sentence, and shipping only the first half was the
    # defect: the pass ended with no nudge, no escalation and `needs_you` still false, so the
    # operator was never told there was a decision waiting. "It escalates" has to mean a DURABLE
    # artifact — the model's own text is never the artifact, only the trigger for one.
    proposal = reading.get("nudge")
    escalate_because = ""
    if reading.get("assessment") == "needs_approval":
        proposal = None
        escalate_because = "the agent is waiting on a decision only you can make"

    # (4b) A STALLED session is not nudged, and that is the follow-through rather than the absence
    # of it. A session whose engine store has not grown since dispatch is sitting at something that
    # eats keystrokes — a trust dialog, an auth wall, a first-run prompt — so `continue` goes into a
    # wall and the budget drains against a wall. The operator is the only one who can clear it.
    if stalled:
        proposal = None
        escalate_because = f"the agent has written nothing since it started ({out['stalled']})"

    if escalate_because:
        if not may_actuate:
            out["held_back"] = "another session in this pass already acted"
            return out
        for o in a["objectives"]:
            if o["met"] or o["stood_down"]:
                continue
            reason = f"{o['title'] or o['key']}: {escalate_because}"
            if await escalate(
                mission_id,
                session_key=session_key,
                objective_key=o["key"],
                reason=reason,
                path=path,
            ):
                out["escalated"] = {"objective_key": o["key"], "reason": reason}
                _announce(row, session_key, reason)
                break
        return out

    if proposal and not may_actuate:
        out["held_back"] = "another session in this pass already acted"
        proposal = None

    if proposal:
        res = await nudge(
            mission_id,
            session_key=session_key,
            objective_key=proposal["objective_key"],
            why=proposal.get("why") or "",
            registry=registry,
            path=path,
        )
        out["nudged"] = res

    if not may_actuate:
        return out

    # (5) Escalate the objectives whose budget is TERMINALLY spent, once per episode.
    #
    # `may_nudge` being false is NOT the condition, and conflating them was a real defect: a merely
    # LIVE action ("a previous nudge has not settled yet") is an ordinary pending approval, and
    # escalating it turned every awaiting-approval objective into a terminal, once-per-episode
    # escalation that could never be retracted. An UNREADABLE ledger is likewise not terminal — it
    # is a fact about the file, not about the objective. Only exhaustion and indeterminacy end an
    # episode, and `assess` computes that as `terminal`.
    for o in a["objectives"]:
        if o["met"] or o["stood_down"] or o["may_nudge"] or not o.get("terminal"):
            continue
        reason = (
            f"{o['title'] or o['key']}: {o['why_not']}"
            if o["why_not"]
            else f"{o['title'] or o['key']} has not moved"
        )
        if await escalate(
            mission_id,
            session_key=session_key,
            objective_key=o["key"],
            reason=reason,
            path=path,
        ):
            out["escalated"] = {"objective_key": o["key"], "reason": reason}
            _announce(row, session_key, reason)
            break
    return out


def _announce(row: dict, session_key: str, reason: str) -> None:
    """Put the escalation in the bell. Best-effort — the durable record is the mission timeline.

    **No model-authored field reaches a dedupe key**, and here that falls out for free rather than
    being remembered: this is only called when `escalate_once` WON, and that is arbitrated by a
    database constraint on `(mission, session, objective, episode)` — none of which the model
    writes. `notifications.add` then applies its own session-identity rule on top.
    """
    from . import notifications

    with contextlib.suppress(Exception):
        notifications.add(
            title=reason[:200],
            project=str((row.get("project") or {}).get("name") or ""),
            session_id=session_key,
            engine=str(row.get("engine") or ""),
            reason="mission supervisor",
            escalation=True,
        )
