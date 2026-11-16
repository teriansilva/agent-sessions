"""Bounded rework (#1951 §4, moved from P4; #1193's contracts): a review that asks for changes
reopens its target as a NEW EPISODE, at most `max_rounds` times per edge.

A flow step may carry `rework = {to, when, max_rounds}`: a failed `when` result reopens `to`. In a
step-mode mission (#1581) that is read here, from the evidence, never from a model's prose:

* **When.** The source step's run is `running` (its current episode), and its `when` row holds an
  observation that says NOT MET: observed (never a stale or unknown read), made after that episode
  started (episode fencing: evidence from an earlier episode never counts), and, for a judged row,
  a verdict that says not met (a met verdict under the confidence floor is unknown, never "changes
  requested") whose fingerprint is the one the judge would compute from the output NOW (a verdict
  on output the agent has since moved past is not the review's answer); for a probe row, an
  observation that SAYS changes were requested (`forge_review`'s `REQUEST_CHANGES`: "no review
  yet" is a review still to come).
* **Consumed once.** `missions.consume_rework` keys a round by `(mission, edge, round)`; a source
  episode and an observation each consume at most one, so a repeated sweep, a retry and a restart
  consume nothing. The cap is per edge.
* **Decided before anything is stopped.** A candidate is only a round the store would consume now
  (`missions.rework_decision`), and it is read again inside the fence, before the stop: a request
  past an exhausted edge, or one already consumed, stops nothing and is left to the ordinary
  reconcile.
* **Reopen = a new episode.** Inside the adoption fence, once the reviewer's session is idle, its
  answer is still the current one and the stop is PROVED (`mission_fence.fenced_step_stop`, the
  reconcile's own path), the round is consumed. ONE transaction holds the round, the reset and the
  source run's settlement (`done`), and re-derives the observation from the row. The store resets
  the evidence of the target and every step after it (new incarnations, so an in-flight probe or
  judgment of the old episode can neither satisfy nor retarget the new one; their supervisor
  episodes advance, so pending decisions settle). The target is then `ready`: under an approval
  the runner starts it through #1581's one seam, which re-checks every pin (a stale pin refuses
  the reopen and asks); without one, the operator's Start does. The reopen GRANTS a further run to
  the steps of the approved span that had run before it; the first run of each CONSUMES it (its
  claim records it on the run row), so HQ starts each of them once more. Until a new episode of
  the step has actually started, the store lets no read of the old output settle its evidence
  (`missions._rework_fenced_tx`).
  Nothing here launches anything.
* **Round `max_rounds + 1` reopens nothing.** The run settles, and ONE question is opened about
  the `when` row (server-worded, the existing answer actions); the question and its obligation
  commit in one transaction. A failure leaves it owed and retried, unless it can never be asked.
  It raises ONE bell row and push (this module is its one producer), retracted when the question
  no longer holds. The claim refuses any further launch of the step under the approval
  (`bound_reached`): beyond the bound is a question, never a launch.

Called from `mission_steps.reconcile` (every supervisor pass and every start). Never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from . import missions

log = logging.getLogger(__name__)

#: Rework's question options: the existing answer actions, each with its server consequence.
QUESTION_ACTIONS = ("note_answer", "waive_objective", "stand_down_objective")


def observation_id(row: dict, since: float) -> str | None:
    """The `when` row's not-met observation identity since `since`, or None (the store's rule,
    `missions.rework_observation`, which `consume_rework` re-derives in its transaction)."""
    return missions.rework_observation(row, since)


def candidates_sync(mission_id: str, *, only_plan: str | None = None) -> list[dict]:
    """The running source runs whose review asked for changes, with the observation, that the
    store would consume a round for NOW. Blocking. `only_plan` narrows it to one run (the fence's
    re-check)."""
    runs = [
        r
        for r in missions.step_runs(mission_id)
        if r["state"] == "running" and (only_plan is None or r["plan_id"] == only_plan)
    ]
    if not runs:
        return []
    latest = missions.plan_revision(mission_id)
    if latest is None:
        return []
    by_id = {s["id"]: s for s in latest["document"]["steps"]}
    rows = {o["key"]: o for o in missions.objectives(mission_id)}
    out = []
    judge_input = None
    for run in runs:
        step = by_id.get(run["step_id"])
        edge = (step or {}).get("rework")
        if not edge or int(run["revision"]) != int(latest["revision"]):
            continue
        row = rows.get(edge["when"])
        if row is None:
            continue
        oid = observation_id(row, float(run["created_at"]))
        if oid is None:
            continue
        if (
            missions.rework_decision(
                mission_id,
                edge_from=run["step_id"],
                source_episode=int(run["episode"]),
                observation_id=oid,
            )
            is None
        ):
            # Nothing the store would consume (the edge is exhausted, or this episode or
            # observation consumed its round already): never stop the session for it.
            continue
        obs = row.get("observed") if isinstance(row.get("observed"), dict) else {}
        judged = obs.get("judged") if isinstance(obs.get("judged"), dict) else None
        if judged is not None:
            # The verdict must be on the output as it is NOW (mission-judge.md: stale means a
            # changed fingerprint); one the agent has since moved past is not its answer.
            from . import mission_judge

            if judge_input is None:
                mission = missions.get_mission(mission_id, events_limit=1) or {}
                judge_input = mission_judge.gather(mission_id, mission.get("cwd"))
            if judged.get("fingerprint") != mission_judge.row_fingerprint(judge_input, row):
                continue
            # The same bar as a met verdict: at least one quote that COUNTS for this agent (a
            # transcript, the diff, or a screen that is its only output), never context alone,
            # since this verdict un-meets other steps' evidence.
            if not any(
                mission_judge._counts(str(q.get("source") or ""), judge_input)
                for q in judged.get("evidence") or []
                if isinstance(q, dict)
            ):
                continue
        out.append({"run": run, "edge": edge, "observation_id": oid})
    return out


def _still_fresh(mission_id: str, cand: dict) -> bool:
    """Asked INSIDE the fence, before the stop: is this still the review's current answer (the
    same observation, for a verdict on the output as it is now) and a round the store would
    consume? Blocking; False on doubt."""
    try:
        now = candidates_sync(mission_id, only_plan=cand["run"]["plan_id"])
    except Exception:  # noqa: BLE001 — could not look: not now
        return False
    return bool(now) and now[0]["observation_id"] == cand["observation_id"]


def _idle(key: str) -> bool:
    from .mission_steps import _idle as idle

    return idle(key)


async def _settle_one(mission_id: str, cand: dict) -> dict | None:
    """Stop the source episode's session (idle, its answer still current and still a round the
    store would consume, proved, under the fence) and consume its round in the same fence. The
    store settles the source run in the transaction that consumes the round and re-derives the
    observation there. Returns the round row, or None when nothing was settled this pass."""
    from . import mission_fence

    run = cand["run"]
    got: list[dict | None] = []

    def settle(outcome: str) -> None:
        if outcome != "stopped":
            # Still working, no longer the current answer, not proved, or the session is not this
            # mission's step to stop (detached by the operator, held elsewhere or as another
            # member: the fence never asked the re-check then). Nothing is consumed: the ordinary
            # reconcile owns that run.
            return
        rec = missions.consume_rework(
            mission_id,
            edge_from=run["step_id"],
            source_episode=int(run["episode"]),
            observation_id=cand["observation_id"],
        )
        got.append(rec)
        if rec is None and outcome == "stopped" and run.get("session_key"):
            # The session was proved stopped, and the round was taken by nobody (the state moved
            # between the fence's re-check and the consume): the run must not stay `running`
            # around a session that is gone, or every later pass would find it again.
            missions.finish_step_run(
                mission_id,
                run["step_id"],
                int(run["episode"]),
                to="stopped",
                detail="its session was stopped for a rework round that was not taken; start "
                "it again yourself",
                expect="running",
            )

    key = run.get("session_key")
    if not key:

        def direct() -> None:
            if _still_fresh(mission_id, cand):
                settle("stopped")

        await missions.run_admitted(direct)
    else:
        await mission_fence.fenced_step_stop(
            mission_id,
            key,
            settle,
            idle=lambda: _idle(key) and _still_fresh(mission_id, cand),
        )
    return got[0] if got else None


def question_text(rec: dict) -> str:
    """Past the bound the target's evidence is not reset, so starting the target would be refused
    (`not_ready`); the review step itself stays startable by the operator's tap (D-P6-16)."""
    n = int(rec["round"])
    m = int(rec["max_rounds"])
    return (
        f"{rec['edge_from']} asked for changes a {missions._ordinal(n)} time. The plan "
        f"covers {m} rework round{'s' if m != 1 else ''} ({rec['edge_from']} → "
        f"{rec['edge_to']}), so nothing was started and {rec['edge_to']} was not sent back. "
        f"Waive the review, stop following it up, or keep it open: you can still start "
        f"{rec['edge_from']} again yourself."
    )


def _options() -> list[dict]:
    from . import mission_questions as mq

    labels = {
        "note_answer": "Keep it open: I'll decide",
        "waive_objective": "The review is not needed",
        "stand_down_objective": "Stop following it up",
    }
    return [
        {
            "label": labels[a],
            "action": a,
            "consequence": mq.CONSEQUENCE[a],
            "settling": a in mq.SETTLING,
        }
        for a in QUESTION_ACTIONS
    ]


def _retired(mission_id: str, when_key: str) -> bool:
    """Is the question no longer askable for a TERMINAL reason (the mission is done, failed,
    abandoned or archived, its review row was met, waived or dropped, or the operator stood it
    down)? Blocking. A transient failure, or a mission in `review`, is not this."""
    m = missions.get_mission(mission_id, events_limit=1)
    # TERMINAL only: a mission in `review` can go back to running, and its question is still owed.
    if m is None or str(m.get("state") or "") in missions.TERMINAL_STATES:
        return True
    if m.get("archived_at") is not None:
        return True
    row = next((o for o in missions.objectives(mission_id) if o["key"] == when_key), None)
    if row is None or str(row.get("state") or "") in ("met", "waived"):
        return True
    return missions.objective_hold(mission_id, when_key)[1]


async def ask_owed(mission_id: str) -> int:
    """Open the ONE question every exhausted round still owes. The question and the round's
    obligation commit in ONE transaction under the write fence (`open_question(rework_round=…)`),
    so overlapping passes, retries and a crash can never open two. A failure leaves it owed and
    retried, unless the question is no longer askable for a terminal reason (then it is retired).
    Returns how many were opened."""
    from . import mission_fence

    recs = await missions.run_admitted(lambda: missions.reworks(mission_id))
    owed = [r for r in recs if r["outcome"] == "exhausted" and r["question_at"] is None]
    if not owed:
        return 0
    latest = await missions.run_admitted(lambda: missions.plan_revision(mission_id))
    if latest is None:
        return 0
    by_id = {s["id"]: s for s in latest["document"]["steps"]}
    n = 0
    for rec in owed:
        edge = (by_id.get(rec["edge_from"]) or {}).get("rework")
        if not edge:
            # The plan no longer has this rework edge: its question can never be asked. Retired,
            # so it is not skipped on every pass and the board does not say it was asked.
            await missions.run_admitted(
                lambda r=rec: missions.mark_rework_question(mission_id, r["edge_from"], r["round"])
            )
            continue
        try:
            await mission_fence.fenced_write(
                mission_id,
                lambda r=rec, k=edge["when"]: missions.open_question(
                    mission_id,
                    k,
                    question_text(r),
                    _options(),
                    origin={"rework": {"edge_from": r["edge_from"], "round": int(r["round"])}},
                    rework_round=(r["edge_from"], int(r["round"])),
                ),
            )
            n += 1
        except Exception as e:  # noqa: BLE001 — owed, and retried next pass…
            log.info("mission %s: rework question not opened: %s", mission_id, e)
            # …unless it can never be asked: then it is retired, so it is not retried for ever.
            if await missions.run_admitted(lambda k=edge["when"]: _retired(mission_id, k)):
                await missions.run_admitted(
                    lambda r=rec: missions.mark_rework_question(
                        mission_id, r["edge_from"], r["round"]
                    )
                )
    return n


def notice_key(mission_id: str, edge_from: str, rnd: int) -> str:
    """The bell row's action id for one exhausted round's question: the mission, the edge and
    the round. No model text."""
    return f"mission-rework:{mission_id}:{edge_from}:{int(rnd)}"


def _holds(mission_id: str, rec: dict, by_id: dict) -> bool:
    edge = (by_id.get(rec["edge_from"]) or {}).get("rework")
    if not edge or rec["question_seq"] is None:
        return False
    return missions.question_holds(mission_id, edge["when"], int(rec["question_seq"]))


def _retract(mission_id: str, rec: dict) -> None:
    from . import notifications

    notifications.dismiss_for_action(notice_key(mission_id, rec["edge_from"], rec["round"]))
    missions.mark_rework_notice(mission_id, rec["edge_from"], rec["round"], closed=True)


def notify_sync(mission_id: str) -> int:
    """THE ONE PRODUCER of the exhausted question's notification: ONE bell row and push per
    (edge, round) while its question holds (`once`: announced at most once ever), retracted when
    it no longer does (answered, superseded, reset, or released by the mission's state). Blocking;
    best-effort (the question itself is the durable record). Returns how many were raised."""
    from . import notifications

    latest = missions.plan_revision(mission_id)
    if latest is None:
        return 0
    by_id = {s["id"]: s for s in latest["document"]["steps"]}
    raised = 0
    for rec in missions.reworks(mission_id):
        if rec["outcome"] != "exhausted" or rec["notice_closed_at"] is not None:
            continue
        try:
            if not _holds(mission_id, rec, by_id):
                if rec["question_at"] is not None:
                    _retract(mission_id, rec)  # never raised, or raised and now settled
                continue
            if rec["notice_at"] is not None:
                continue
            title = str((missions.get_mission(mission_id, events_limit=1) or {}).get("title") or "")
            row = notifications.add(
                title=f"Mission asks you: {title}"[:200],
                project="",
                session_id="",
                engine="",
                reason=f"{rec['edge_from']}: the plan's rework rounds are used",
                action_id=notice_key(mission_id, rec["edge_from"], rec["round"]),
                once=True,
            )
            if row and notifications.list_subscriptions():
                with contextlib.suppress(Exception):
                    notifications.fanout(row)
            missions.mark_rework_notice(mission_id, rec["edge_from"], rec["round"])
            raised += 1 if row else 0
            # Answered between the read and the row: retract it now, not on a later pass.
            if not _holds(mission_id, rec, by_id):
                _retract(mission_id, rec)
        except Exception:  # noqa: BLE001 — the bell is another store; retried next pass
            log.warning("mission %s: rework notification not settled", mission_id, exc_info=True)
    return raised


def retract_sync(mission_id: str) -> int:
    """Retract every exhausted round's notification whose question no longer holds, at once (the
    answer and state routes call it, so an answered question's bell row goes with the answer, not
    on a later pass, and a mission that stops being reconciled leaves none behind). Raises
    nothing new. Blocking; best-effort. Returns how many it retracted."""
    n = 0
    try:
        latest = missions.plan_revision(mission_id)
        if latest is None:
            return 0
        by_id = {s["id"]: s for s in latest["document"]["steps"]}
        for rec in missions.reworks(mission_id):
            if (
                rec["outcome"] != "exhausted"
                or rec["question_at"] is None
                or rec["notice_closed_at"] is not None
                or _holds(mission_id, rec, by_id)
            ):
                continue
            _retract(mission_id, rec)
            n += 1
    except Exception:  # noqa: BLE001 — the next pass retracts it
        log.debug("mission %s: rework notifications not retracted", mission_id, exc_info=True)
    return n


async def sweep(mission_id: str) -> int:
    """One pass: consume the rounds the evidence asks for, open the owed questions, then raise or
    retract their notifications. Returns how many rounds were consumed. Never raises."""
    n = 0
    try:
        cands = await asyncio.to_thread(candidates_sync, mission_id)
    except Exception:  # noqa: BLE001 — retried by the next pass
        log.debug("mission %s: rework could not read", mission_id, exc_info=True)
        cands = []
    for cand in cands:
        try:
            rec = await _settle_one(mission_id, cand)
            n += 1 if rec and rec.get("new") else 0
        except Exception:  # noqa: BLE001 — one edge must not stop the others
            log.warning("mission %s: rework not settled", mission_id, exc_info=True)
    try:
        await ask_owed(mission_id)
    except Exception:  # noqa: BLE001
        log.debug("mission %s: rework question not asked", mission_id, exc_info=True)
    try:
        await asyncio.to_thread(notify_sync, mission_id)
    except Exception:  # noqa: BLE001
        log.debug("mission %s: rework notification not raised", mission_id, exc_info=True)
    return n


def view(mission_id: str, doc: dict) -> dict[str, dict]:
    """Per step, its rework state for the board (8b): the latest round that granted it a further
    run, and an exhausted edge's source. Server text. Blocking. (Whether HQ may start a step
    again is the claim's own rule, `missions.open_reopens`, never this chip.)"""
    out: dict[str, dict] = {}
    for rec in missions.reworks(mission_id):
        m = int(rec["max_rounds"])
        if rec["outcome"] == "reopened":
            for sid in missions.rework_granted(rec):
                out[sid] = {
                    "round": int(rec["round"]),
                    "max_rounds": m,
                    "exhausted": False,
                    "text": f"rework {rec['round']} of {m}",
                }
        else:
            retired = rec["question_at"] is not None and rec["question_seq"] is None
            out[rec["edge_from"]] = {
                "round": int(rec["round"]),
                "max_rounds": m,
                "exhausted": True,
                "text": f"rework used: {m} of {m}; "
                + (
                    "the review was settled, nothing to ask"
                    if retired
                    else "you were asked what next"
                ),
            }
    return out


__all__ = [
    "QUESTION_ACTIONS",
    "ask_owed",
    "candidates_sync",
    "notice_key",
    "notify_sync",
    "observation_id",
    "question_text",
    "retract_sync",
    "sweep",
    "view",
]
