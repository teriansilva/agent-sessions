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
* **Read fresh, bounded, outside the global lock (#2035).** The cached observation only nominates a
  candidate. Before anything is stopped, the `when` row is read NOW: a probe row is asked again
  through the probe pass's own fenced path (`mission_probes.probe_row`), a judged row's output is
  gathered (`mission_judge.gather`) and its fingerprint compared. Both run before
  `session_input._lock` is taken, within `WHEN_READ_TIMEOUT_S`. A read that does not answer in time
  consumes nothing this pass, and its late answer is never acted on (nothing waits for it; it only
  records an observation; the thread it leaves is bounded by the read's own limits — the probe's
  HTTP timeout, the judge's gather caps). Inside the fence, nothing slow runs: the store is asked
  whether the read is younger than `WHEN_READ_MAX_AGE_S`, the approval in force is the one the read
  began under, and the row still holds the same observation (its incarnation, the source episode and
  the fact, `missions.rework_observation`), for a probe row one whose read began at or after this
  fresh read. A judged read is bound to the output generation of the sessions it gathered from,
  read before and after the gather outside the lock; in the fence only the reviewer's is compared,
  from the one snapshot the idle check reads (`_fence_ready`). The consume binds the approval
  again in its own transaction.
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

* **A round held back by an `unknown` run says so (#2035 P3).** When a review asks for changes
  but a step the reopen would reset still has an `unknown` run (never settled automatically), the
  round waits and nothing is consumed; ONE bell row and push per blocking run
  (`mission-rework-blocked:<mission>:<edge>:<step>:<episode>`, `once`) tells the operator to settle
  that run, and it is retracted, on devices too, when the run settles or the edge stops mattering.

Called from `mission_steps.reconcile` (every supervisor pass and every start). Never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import missions

log = logging.getLogger(__name__)

#: The fresh `when` read's budget, in seconds (#2035): a probe request, or the judge's gather of
#: the reviewer's output, that has not answered by then consumes nothing this pass.
WHEN_READ_TIMEOUT_S = 20.0
#: How old that read may be when the fence checks it (the fence may wait for the global lock):
#: an older one consumes nothing, and the next pass reads again.
WHEN_READ_MAX_AGE_S = 60.0

#: Rework's question options: the existing answer actions, each with its server consequence.
QUESTION_ACTIONS = ("note_answer", "waive_objective", "stand_down_objective")


def observation_id(row: dict, since: float) -> str | None:
    """The `when` row's not-met observation identity since `since`, or None (the store's rule,
    `missions.rework_observation`, which `consume_rework` re-derives in its transaction)."""
    return missions.rework_observation(row, since)


def candidates_sync(
    mission_id: str, *, only_plan: str | None = None, judge: bool = True
) -> list[dict]:
    """The running source runs whose review asked for changes, with the observation, that the
    store would consume a round for NOW. Blocking. `only_plan` narrows it to one run. `judge`:
    whether a judged row's verdict is compared with the output as it is now (`gather`, the slow
    part); without it only the store is read: the nomination, and the fence's re-check (#2035)."""
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
        if judged is not None and judge:
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
        out.append(
            {
                "run": run,
                "edge": edge,
                "observation_id": oid,
                # A JUDGED observation is read fresh by gathering the output; any other by
                # asking the probe again (the same distinction as above).
                "judged": judged is not None,
                "read_at": obs.get("read_at"),
            }
        )
    return out


def _read_when(mission_id: str, key: str) -> None:
    """Ask the `when` probe row NOW through the probe pass's fenced path (the slow part: a forge
    request). Blocking; never under a session lock."""
    from . import mission_probes

    mission_probes.probe_row(mission_id, key)


def _recorded_since(cand: dict, since: float) -> bool:
    """A probe row's observation is one whose read began at or after `since`."""
    at = cand.get("read_at")
    return isinstance(at, int | float) and not isinstance(at, bool) and float(at) >= since


def _marker(snap: dict) -> tuple:
    """A session's output generation in one snapshot: its turn count, its last turn's id and
    status, and whether a turn or a background task runs. Any turn that starts, settles or is
    added changes it."""
    turns = snap.get("turns") or []
    last = turns[-1] if turns else {}
    return (
        len(turns) + int(snap.get("omitted_turns") or 0),
        str(last.get("turn_id") or ""),
        str(last.get("status") or ""),
        bool(snap.get("active_turn")),
        bool((snap.get("native") or {}).get("background_active")),
    )


async def _snapshot_async(key: str) -> dict:
    """One runtime snapshot, awaited ON THE MAIN LOOP (independent review of #2230): a chat
    session's snapshot takes that session's `asyncio.Lock`, which belongs to this loop, so it
    is never driven from another loop (`asyncio.run` in a worker thread)."""
    from . import structured_runtime as runtime

    return await runtime.snapshot(key)


async def _output_generation(keys: list[str], source: str | None) -> tuple | None:
    """The OUTPUT GENERATION of the sessions a judged read gathers from and of the reviewer (#2035,
    Hermes on #2230): `(key, _marker)` per session, awaited on the main loop, before and after the
    gather — never in the fence. None when the reviewer cannot be read (fail closed); another
    session that cannot be read (a terminal one) contributes a constant."""
    out: list[tuple] = []
    for key in sorted(set(keys) | ({source} if source else set())):
        try:
            snap = await _snapshot_async(key)
        except Exception:  # noqa: BLE001 — could not look
            if key == source:
                return None
            out.append((key, None))
            continue
        out.append((key, _marker(snap)))
    return tuple(out)


def _gathered_keys(mission_id: str) -> list[str]:
    """The sessions a judged read gathers from (`mission_judge._recent_sessions`)."""
    from . import mission_judge

    return list(mission_judge._recent_sessions(mission_id))


async def _fresh(mission_id: str, cand: dict) -> dict | None:
    """THE FRESH, BOUNDED `when` READ (#2035), before any lock: the candidate as the evidence says
    NOW, or None (no longer a request, not a round the store would consume, or the read did not
    answer within `WHEN_READ_TIMEOUT_S` — its late answer is never acted on)."""
    plan = cand["run"]["plan_id"]
    started = time.time()
    try:
        # Bound BEFORE the read: an approval replaced while it runs is caught in the fence.
        approval = await asyncio.to_thread(missions.approval_in_force_id, mission_id)
    except Exception:  # noqa: BLE001 — could not look: not now
        return None
    probe = not cand.get("judged")
    source = cand["run"].get("session_key") or None
    seen: dict = {}

    def read_probe() -> list[dict]:
        _read_when(mission_id, cand["edge"]["when"])
        return candidates_sync(mission_id, only_plan=plan)

    async def read_judged() -> list[dict]:
        # The judged row's output is gathered in a worker, outside every session lock. Its OUTPUT
        # GENERATION is awaited here on the main loop BEFORE and AFTER the gather (Hermes and the
        # independent review on #2230): a session whose turn moved during the gather, or a
        # different set of sessions, discards the read.
        keys = await asyncio.to_thread(_gathered_keys, mission_id)
        before = await _output_generation(keys, source)
        if before is None:
            return []
        got = await asyncio.to_thread(lambda: candidates_sync(mission_id, only_plan=plan))
        if await asyncio.to_thread(_gathered_keys, mission_id) != keys:
            return []
        if await _output_generation(keys, source) != before:
            return []
        # The reviewer's own generation is checked once more in the fence, from the one snapshot
        # `_idle_snapshot` validates there: a reviewer that answered again after the gather.
        seen["reviewer"] = next((m for k, m in before if k == source), None) if source else None
        return got

    try:
        now = await asyncio.wait_for(
            asyncio.to_thread(read_probe) if probe else read_judged(), WHEN_READ_TIMEOUT_S
        )
    except TimeoutError:
        log.info("mission %s: the review's fresh read timed out; no round this pass", mission_id)
        return None
    except Exception:  # noqa: BLE001 — unknown is not "changes requested"
        log.debug("mission %s: the review's fresh read failed", mission_id, exc_info=True)
        return None
    if not now or (probe and not _recorded_since(now[0], started)):
        return None
    return {
        **now[0],
        "fresh_at": started,
        "approval_id": approval,
        "reviewer_marker": seen.get("reviewer"),
    }


def _still_valid(mission_id: str, cand: dict) -> bool:
    """Asked INSIDE the fence, before the stop, and reading only the store (#2035: nothing slow
    under the global lock): the fresh read is young enough, the approval it began under is still
    the one in force, and the row still holds that observation (for a probe row, one read at or
    after the fresh read; a judged row's reviewer is checked by `_fence_ready`, from the snapshot
    the idle check already read). Store reads only. False on doubt."""
    try:
        if time.time() - float(cand["fresh_at"]) > WHEN_READ_MAX_AGE_S:
            return False
        if missions.approval_in_force_id(mission_id) != cand["approval_id"]:
            return False
        now = candidates_sync(mission_id, only_plan=cand["run"]["plan_id"], judge=False)
    except Exception:  # noqa: BLE001 — could not look: not now
        return False
    if not now or now[0]["observation_id"] != cand["observation_id"]:
        return False
    return bool(cand.get("judged")) or _recorded_since(now[0], cand["fresh_at"])


async def _observe(key: str):
    """The reviewer's pre-fence observation, awaited on the MAIN loop (`fence_idle.observe`,
    #2232): no runtime snapshot is ever taken inside the fence."""
    from . import fence_idle

    return await fence_idle.observe(key)


def _idle_snapshot(key: str, obs=None) -> dict | None:  # noqa: ARG001 — the key names the session
    """The reviewer's OBSERVED snapshot when it was idle and has not moved since (#2232:
    `fence_idle.validate`, lock-free and `asyncio`-free), else None. Asked inside the fence."""
    from . import fence_idle

    return fence_idle.validate(obs)


def _fence_ready(mission_id: str, key: str, cand: dict, obs=None) -> bool:
    """Asked INSIDE the fence: the reviewer is idle as observed before the fence and unchanged
    since (#2232), a judged read's reviewer has not moved since before the gather (its turn
    settled, a new one, the move to idle — Hermes on #2230), and the store still says this round
    (`_still_valid`). A lock-free marker read, then store reads."""
    snap = _idle_snapshot(key, obs)
    if snap is None:
        return False
    if cand.get("judged") and cand.get("reviewer_marker") != _marker(snap):
        return False
    return _still_valid(mission_id, cand)


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
            expect_approval=cand["approval_id"],
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
            if _still_valid(mission_id, cand):
                settle("stopped")

        await missions.run_admitted(direct)
    else:
        obs = await _observe(key)  # on this loop, BEFORE the fence (#2232)
        await mission_fence.fenced_step_stop(
            mission_id,
            key,
            settle,
            idle=lambda: _fence_ready(mission_id, key, cand, obs),
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
    """Retract one round's notification, on devices too (#2154). The closure is persisted with the
    row's removal (`notifications.retract_episode`) BEFORE the round is marked closed, so a failed
    notifications write leaves the round open and the next pass retries the whole retraction."""
    from . import notifications

    notifications.retract_episode(notice_key(mission_id, rec["edge_from"], rec["round"]))
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
            key = notice_key(mission_id, rec["edge_from"], rec["round"])
            row = notifications.add(
                title=f"Mission asks you: {title}"[:200],
                project="",
                session_id="",
                engine="",
                reason=f"{rec['edge_from']}: the plan's rework rounds are used",
                action_id=key,
                once=True,
                # Its own never-reused device tag (#2154): its retraction closes exactly it.
                tag=notifications.mission_episode_tag(key),
            )
            if row:
                notifications.push(row)
            missions.mark_rework_notice(mission_id, rec["edge_from"], rec["round"])
            raised += 1 if row else 0
            # Answered between the read and the row: retract it now, not on a later pass.
            if not _holds(mission_id, rec, by_id):
                _retract(mission_id, rec)
        except Exception:  # noqa: BLE001 — the bell is another store; retried next pass
            log.warning("mission %s: rework notification not settled", mission_id, exc_info=True)
    return raised


#: A round held back by an `unknown` run (#2035 P3): its bell row's action id prefix.
BLOCKED = "mission-rework-blocked:"


def blocked_key(mission_id: str, edge_from: str, step: str, episode: int) -> str:
    """The pending-round notice's identity: the mission, the edge and the BLOCKING run (its step
    and episode). No model text. A different blocking run is a new notice; the same one is never
    announced twice (`once`)."""
    return f"{BLOCKED}{mission_id}:{edge_from}:{step}:{int(episode)}"


def blocked_sync(mission_id: str) -> list[dict]:
    """The rounds a review's "changes requested" would consume NOW but for an `unknown` run of a
    step the reopen would reset (`missions.rework_blocked`): `{edge_from, step, episode, key}`.
    Reads the store only (the cached observation; no probe, no gather) and consumes nothing.
    Blocking."""
    runs = [r for r in missions.step_runs(mission_id) if r["state"] == "running"]
    if not runs:
        return []
    latest = missions.plan_revision(mission_id)
    if latest is None:
        return []
    by_id = {s["id"]: s for s in latest["document"]["steps"]}
    rows = {o["key"]: o for o in missions.objectives(mission_id)}
    out = []
    for run in runs:
        edge = (by_id.get(run["step_id"]) or {}).get("rework")
        if not edge or int(run["revision"]) != int(latest["revision"]):
            continue
        row = rows.get(edge["when"])
        oid = observation_id(row, float(run["created_at"])) if row is not None else None
        if oid is None:
            continue
        got = missions.rework_blocked(
            mission_id,
            edge_from=run["step_id"],
            source_episode=int(run["episode"]),
            observation_id=oid,
        )
        if got is not None:
            out.append(
                {
                    "edge_from": run["step_id"],
                    "step": got["step"],
                    "episode": got["episode"],
                    "key": blocked_key(mission_id, run["step_id"], got["step"], got["episode"]),
                }
            )
    return out


def _parse_blocked(mission_id: str, key: str) -> tuple[str, str, int] | None:
    """`(edge_from, step, episode)` of one of this mission's pending-round keys, or None."""
    rest = key.removeprefix(f"{BLOCKED}{mission_id}:")
    if rest == key:
        return None
    edge_from, sep, tail = rest.partition(":")
    step, sep2, episode = tail.rpartition(":")
    if not (sep and sep2 and edge_from and step and episode.isdigit()):
        return None
    return edge_from, step, int(episode)


def _blocked_holds(mission_id: str, key: str) -> bool:
    """Whether a raised pending-round notice still stands, on LASTING facts only (independent
    review of #2035 P3): its blocking run is still `unknown`, the mission is not finished, the
    edge is still on the latest plan with the blocking step in its reset span, its `when` row is
    not settled (met or waived: the review passed), and its source
    still has a `running` run on that revision whose round was neither consumed nor exhausted.
    A blip of the evidence (an unknown probe read, a stale verdict, the mission paused) never
    retracts it: the notice is announced `once`, so a retraction on a blip could never be undone
    while the same run still holds the round. Blocking; False on a key it cannot read."""
    parsed = _parse_blocked(mission_id, key)
    if parsed is None:
        return False
    edge_from, step, episode = parsed
    blocker = missions.step_run(mission_id, step, episode)
    if blocker is None or blocker["state"] != "unknown":
        return False
    mission = missions.get_mission(mission_id, events_limit=1) or {}
    if not mission or str(mission.get("state") or "") in missions.TERMINAL_STATES:
        return False
    latest = missions.plan_revision(mission_id)
    if latest is None:
        return False
    doc = latest["document"]
    edge = next((x.get("rework") for x in doc["steps"] if x["id"] == edge_from), None)
    if not edge or step not in missions.rework_reset_steps(doc, edge_from, edge["to"]):
        return False
    when = next((o for o in missions.objectives(mission_id) if o["key"] == edge["when"]), None)
    if when is None or str(when.get("state") or "") in ("met", "waived"):
        return False  # the review passed (a settled fact, not a blip), or the row is gone
    sources = [
        r
        for r in missions.step_runs(mission_id)
        if r["step_id"] == edge_from
        and r["state"] == "running"
        and int(r["revision"]) == int(latest["revision"])
    ]
    if not sources:
        return False
    rounds = [r for r in missions.reworks(mission_id) if r["edge_from"] == edge_from]
    if len(rounds) > int(edge["max_rounds"]):
        return False  # exhausted: its one question stands, not this notice
    consumed = {int(r["source_episode"]) for r in rounds}
    return any(int(r["episode"]) not in consumed for r in sources)


def _retract_blocked(mission_id: str, raised_now: set[str] = frozenset()) -> int:
    """Retract this mission's pending-round notices that no longer stand (`_blocked_holds`), on
    devices too (#2154's episode close). A notice raised on THIS pass is kept. Strict read: an
    unreadable bell raises, it never reads as "none"."""
    from . import notifications

    n = 0
    for key in notifications.actions_with_prefix(f"{BLOCKED}{mission_id}:"):
        if key not in raised_now and not _blocked_holds(mission_id, key):
            n += notifications.retract_episode(key)
    return n


def notify_blocked_sync(mission_id: str) -> int:
    """THE ONE PRODUCER of the pending-round notice (#2035 P3): ONE bell row and push per blocking
    run while it holds the round back (`once`: deduplicated across passes, retries and restarts by
    the store's tombstone), retracted when it no longer does. No round is consumed to get a
    record: the notice IS the record, and it is derived again every pass. Blocking; best-effort.
    Returns how many were raised."""
    from . import notifications

    blocked = blocked_sync(mission_id)
    _retract_blocked(mission_id, {b["key"] for b in blocked})
    if not blocked:
        return 0
    # Raised only on a CURRENT request; retracted only on lasting facts (`_blocked_holds`).
    title = str((missions.get_mission(mission_id, events_limit=1) or {}).get("title") or "")
    raised = 0
    for b in blocked:
        row = notifications.add(
            title=f"Mission rework waits: {title}"[:200],
            project="",
            session_id="",
            engine="",
            reason=(
                f"{b['edge_from']}: a rework round waits for {b['step']}, whose last run ended "
                "unknown; settle that run on the mission board"
            ),
            action_id=b["key"],
            once=True,
            tag=notifications.mission_episode_tag(b["key"]),
        )
        if row:
            notifications.push(row)
            raised += 1
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
    try:
        # The pending-round notices too (#2035 P3): a mission that stops, a run settled on the
        # board, a plan change — gone with the route that changed it, not on a later pass.
        n += _retract_blocked(mission_id)
    except Exception:  # noqa: BLE001 — the next pass retracts it
        log.debug("mission %s: pending-round notices not retracted", mission_id, exc_info=True)
    return n


async def sweep(mission_id: str) -> int:
    """One pass: consume the rounds the evidence asks for, open the owed questions, then raise or
    retract their notifications. Returns how many rounds were consumed. Never raises."""
    n = 0
    try:
        # The CACHED evidence only nominates (no gather): each candidate is read fresh below.
        cands = await asyncio.to_thread(lambda: candidates_sync(mission_id, judge=False))
    except Exception:  # noqa: BLE001 — retried by the next pass
        log.debug("mission %s: rework could not read", mission_id, exc_info=True)
        cands = []
    for cand in cands:
        try:
            fresh = await _fresh(mission_id, cand)
            if fresh is None:
                continue
            rec = await _settle_one(mission_id, fresh)
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
    try:
        await asyncio.to_thread(notify_blocked_sync, mission_id)
    except Exception:  # noqa: BLE001 — the next pass raises or retracts it
        log.debug("mission %s: pending-round notice not settled", mission_id, exc_info=True)
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
