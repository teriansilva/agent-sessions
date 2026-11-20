"""#2035 Phase 1: a reopened step settles only from evidence of its NEW episode.

A rework reopen resets the target's rows and grants the step a new episode. A row of it settles
only from a read that BEGAN after a later run of the step was claimed, once that run has really
started (`running`, `done`, `stopped`) — never `claimed`, `failed` or `unknown`. A read begun
before the new run, even if its answer lands after the run started, is the old episode's output:
it does not settle. The external fact need not change. Probes and the judge both go through the
one settlement path (`missions.observe_objective` → `_rework_fenced_tx`).
"""

from __future__ import annotations

import time

import pytest

from agent_sessions import forge, mission_judge, mission_probes, missions
from test_mission_rework import (
    SRC,
    TO,
    _at_review,
    _claim,
    _consume,
    _fresh_runner,  # noqa: F401 — the autouse runner reset
    _fresh_when_read,  # noqa: F401 — the autouse fresh `when` read
    _land,
    _pin,
    _running,
    _set_run_state,
    _states,
    _step,
    anyio_backend,  # noqa: F401 — the anyio backend fixture
)

SPAN = (TO, "o1.cover_letter", "o1.documents", SRC)
JUDGED = {
    "met": True,
    "confidence": 0.97,
    "threshold": 0.9,
    "evidence": [{"source": "transcript:cv", "quote": "The CV is rewritten for the posting."}],
    "fingerprint": "fp-new",
}


def _reopened():
    mid, doc = _at_review()
    aid = _pin(mid, SPAN)
    _consume(mid, doc, 1)
    return mid, doc, aid, _step(doc, TO)["evidence"][0]


def _settle(mid, key, *, read_at, judged=None):
    kw = {"judged": judged, "expect_probe": "supervisor_judged"} if judged else {}
    return missions.observe_objective(
        mid, key, observed=True, value=True, detail="seen", read_at=read_at, **kw
    )


def _judged_row(mid, key) -> None:
    """Make the reset row a judged one, as a judged target is."""
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET probe='supervisor_judged', probe_args=NULL "
            "WHERE mission_id=? AND key=?",
            (mid, key),
        )
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("kind", ["world_fact", "judged"])
def test_a_fresh_read_of_the_unchanged_old_output_does_not_settle_but_the_new_runs_does(kind):
    mid, doc, aid, key = _reopened()
    judged = None
    if kind == "judged":
        _judged_row(mid, key)
        judged = dict(JUDGED)
    # Fresh, after the reset, of the old output: no new run yet — fenced.
    got = _settle(mid, key, read_at=time.time(), judged=judged)
    assert got["state"] == "pending" and got["observed"]["awaits_rework_run"] == TO
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    # A read begun in the new episode settles, though the fact itself did not change.
    got = _settle(mid, key, read_at=time.time(), judged=judged and {**JUDGED})
    assert got["state"] == "met", got["observed"]


@pytest.mark.parametrize("kind", ["world_fact", "judged"])
def test_a_read_begun_before_the_new_run_but_delivered_after_it_does_not_settle(kind):
    """THE TIMESTAMP RACE: the read started on the old output, the new run started while it was
    in flight, and its answer lands now. It is the old episode's evidence."""
    mid, doc, aid, key = _reopened()
    judged = None
    if kind == "judged":
        _judged_row(mid, key)
        judged = dict(JUDGED)
    began = time.time()
    time.sleep(0.01)
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    got = _settle(mid, key, read_at=began, judged=judged)
    assert got["state"] == "pending", "a read begun on the old output settled the reopened row"
    assert got["observed"]["awaits_rework_run"] == TO
    # No known start at all is never proof of the new episode.
    got = _settle(mid, key, read_at=None, judged=judged and {**JUDGED})
    assert got["state"] == "pending"


@pytest.mark.parametrize(
    "state, lifts",
    [
        ("running", True),
        ("done", True),
        ("stopped", True),
        ("claimed", False),
        ("failed", False),
        ("unknown", False),
    ],
)
def test_only_a_real_later_episode_lifts_the_fence(state, lifts):
    mid, doc, aid, key = _reopened()
    run = _claim(mid, TO, "approval", aid)
    if state in ("failed", "stopped"):
        _land(mid, run["plan_id"], state)
    elif state != "claimed":
        _set_run_state(run["plan_id"], state)
    got = _settle(mid, key, read_at=time.time())
    assert (got["state"] == "met") is lifts, (state, got["observed"])
    if not lifts:
        assert _states(mid, doc)[TO] in ("ready", "running", "claimed", "unknown", "failed")


def test_the_real_probe_path_records_when_its_read_began(monkeypatch):
    """`mission_probes.run_for_mission` end to end: a probe whose request began before the new run
    started and returned after does not settle the reopened row; the next probe, begun in the new
    episode, does."""
    mid, doc, aid, key = _reopened()
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET probe='git_local', probe_args=NULL "
            "WHERE mission_id=? AND key=?",
            (mid, key),
        )
        con.commit()
    finally:
        con.close()
    started: list[str] = []

    def slow_probe(m, o, **_):
        # The new run starts while this request is in flight.
        if not started and o["key"] == key:
            run = _claim(mid, TO, "approval", aid)
            _set_run_state(run["plan_id"], "running")
            started.append(run["plan_id"])
        return forge.Fact.seen(True, "on branch work")

    monkeypatch.setattr(mission_probes, "probe_one", slow_probe)
    mission_probes.run_for_mission(mid)
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert started, "the probe never read the reopened step's row"
    assert row["state"] == "pending" and row["observed"]["awaits_rework_run"] == TO
    assert row["observed"]["read_at"] < missions.step_run(mid, TO, 2)["created_at"]
    # The next pass reads in the new episode: it settles.
    mission_probes.run_for_mission(mid)
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert row["state"] == "met", row["observed"]


QUOTE = "I rewrote the whole CV for the posting and committed it on the work branch."


@pytest.mark.anyio
@pytest.mark.parametrize("gathered", ["before_the_new_run", "in_the_new_episode"])
async def test_the_real_judge_path_carries_when_its_gather_began(monkeypatch, gathered):
    """`mission_judge.judge_row` end to end, with the model's reply faked: the same met verdict
    settles the reopened row only when its input was gathered in the new episode."""
    from agent_sessions import review

    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    t0 = time.time()
    inp = mission_judge.gather("msn_" + "0" * 32, None)  # stamps when it began
    assert inp.gathered_at is not None and inp.gathered_at >= t0
    inp.sources = {"transcript:claude:cv": QUOTE}
    inp.fingerprint = "fp-cv"
    if gathered == "before_the_new_run":
        inp.gathered_at = time.time()
        time.sleep(0.01)
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    if gathered == "in_the_new_episode":
        time.sleep(0.01)
        inp.gathered_at = time.time()

    async def verdict(messages, **_):
        return {
            "met": True,
            "confidence": 0.99,
            "reason": "the CV was rewritten",
            "evidence": [{"source": "transcript:claude:cv", "quote": QUOTE}],
        }

    monkeypatch.setattr(review, "complete_json", verdict)
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    await mission_judge.judge_row(mid, row, inp, "Judge it.")
    got = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert got["observed"].get("read_at") == inp.gathered_at
    if gathered == "before_the_new_run":
        assert got["state"] == "pending" and got["observed"]["awaits_rework_run"] == TO
    else:
        assert got["state"] == "met", got["observed"]


# ---- Phase 4: a `when` row that can never drive rework is refused at validation ---------------


def test_a_rework_edge_on_a_row_that_can_never_ask_for_changes_is_refused():
    import mission_team_fixture as mtf
    from agent_sessions.playbooks import PlaybookFormatError

    never = mtf.FLOW.replace(
        'rework = { to = "cv", when = "review_ok", max_rounds = 3 }',
        'rework = { to = "cv", when = "looked_at", max_rounds = 3 }\n'
        '[[steps.checklist]]\nkey = "looked_at"\ntitle = "Somebody looked"\nprobe = "none"',
    )
    assert never != mtf.FLOW
    with pytest.raises(PlaybookFormatError) as e:
        mtf.procedure(never)
    assert "looked_at" in str(e.value) and "never ask for changes" in str(e.value)
    # The two kinds that CAN drive it are accepted (the fixture's is judged; the bundled
    # forgejo-workflow review is forge_review and deploys).
    assert mtf.procedure(mtf.FLOW)["flow"]
    from agent_sessions import missions as m

    assert m.REWORK_DRIVING_PROBES == {"supervisor_judged", "forge_review"}


# ---- Phase 5: a consent-authorised claim is held to its approval's bound ------------------------


def _bounded_mission():
    """A mission whose documents step ran twice under approval `apr-b` (bound 1 + 1 = 2), with a
    reopen since, so only the bound can refuse a launch nobody tapped for."""
    import json

    from test_mission_rework import _bound, _doc_rounds, _mission, _rework_pins, _run

    mid, doc = _mission(
        doc=_doc_rounds(1), met=("o1.intake", "o1.posting", "o1.cv", "o1.cover_letter")
    )
    pins = {"steps": [{"id": "o1.documents"}], "rework": _rework_pins(mid)}
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) "
            "VALUES ('apr-b', ?, 1, 'd', ?, 'op', 'r', 1)",
            (mid, json.dumps(pins)),
        )
        con.commit()
    finally:
        con.close()
    assert _bound(mid, "apr-b", "o1.documents") == 2
    _run(mid, "o1.documents", 1, "done")
    _run(mid, "o1.documents", 2, "stopped")
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_reworks (mission_id, edge_from, edge_to, round, max_rounds, "
            "source_episode, observation_id, revision, outcome, granted_json, created_at) "
            "VALUES (?, ?, ?, 1, 1, 1, 'obs:x', 1, 'reopened', "
            '\'["o1.cv", "o1.documents", "o1.review"]\', ?)',
            (mid, SRC, TO, time.time() + 1),
        )
        con.commit()
    finally:
        con.close()
    return mid


def _consent_on(mid: str, approval_id: str) -> str:
    cid = "con-" + approval_id
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_standing_consents (id, mission_id, approval_id, scope_digest, "
            "scope_json, expires_at, operation_id, request_digest, created_at) "
            "VALUES (?, ?, ?, 's', '{}', ?, ?, 'r', 1)",
            (cid, mid, approval_id, time.time() + 86400, "op-" + cid),
        )
        con.commit()
    finally:
        con.close()
    return cid


def _claim_as(mid: str, authority: str, *, consent_id=None, approval_id=None):
    return missions.claim_step(
        mid,
        "o1.documents",
        revision=1,
        engine="claude-api",
        model=None,
        effort=None,
        specialist=("cv-writer", 1),
        project_id="p-synthetic01",
        cwd="/tmp/consent-bound",
        brief="b",
        authority=authority,
        approval_id=approval_id,
        consent_id=consent_id,
    )


def _effects(mid: str) -> tuple[int, int]:
    con = missions._ready(None)
    try:
        runs = con.execute(
            "SELECT COUNT(*) FROM mission_step_runs WHERE mission_id=?", (mid,)
        ).fetchone()[0]
        dispatches = con.execute(
            "SELECT COUNT(*) FROM mission_dispatches WHERE mission_id=?", (mid,)
        ).fetchone()[0]
        return int(runs), int(dispatches)
    finally:
        con.close()


@pytest.mark.parametrize("approval", ["apr-b", "apr-gone"])
def test_a_consent_authorised_claim_past_the_bound_is_refused_with_no_effects(approval):
    """The consent rests on approval `apr-b` (bound 2, both runs used): past 1 + N, refused. A
    consent whose approval cannot be read fails closed (the first run only): refused too."""
    mid = _bounded_mission()
    cid = _consent_on(mid, approval)
    before = _effects(mid)
    with pytest.raises(missions.StepRefused) as e:
        _claim_as(mid, "consent", consent_id=cid)
    assert e.value.code == "bound_reached", e.value.code
    assert _effects(mid) == before, "a refused claim wrote a run or a dispatch"
    # The operator's own Start is not bounded, as before.
    assert _claim_as(mid, "operator")["plan_id"].startswith("stp_")


# ---- the judge's other paths (independent review of the first round) ---------------------------


def test_a_threshold_recompute_settles_a_new_episode_verdict_on_a_reopened_step():
    """`mark_stale_judgments` re-scores the SAME read against a new threshold: it keeps that read's
    start, so a verdict gathered in the new episode still counts once the threshold allows it —
    never stuck behind the fence for the life of the mission."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    inp = mission_judge.JudgeInput(
        sources={"transcript:claude:cv": QUOTE}, fingerprint="fp-x", gathered_at=time.time()
    )
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    fp = mission_judge.row_fingerprint(inp, row)
    verdict = {**JUDGED, "confidence": 0.92, "threshold": 0.95, "fingerprint": fp}
    got = _settle(mid, key, read_at=inp.gathered_at, judged=verdict)
    assert got["state"] == "pending" and not got["observed"].get("awaits_rework_run")
    wl = {"rows": [next(o for o in missions.objectives(mid) if o["key"] == key)]}
    out = mission_judge.mark_stale_judgments(mid, wl, inp, 0.9)
    assert out["recomputed"] == 1
    got = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert got["state"] == "met", got["observed"]


@pytest.mark.anyio
async def test_a_gather_that_began_before_the_new_run_is_old_evidence(monkeypatch):
    """`gather` stamps when it BEGAN, not when it finished: the new run is claimed and starts while
    the gather is still reading, so the verdict on that input is the old episode's output."""
    from agent_sessions import review

    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)

    def run_starts_mid_gather(cwd):
        run = _claim(mid, TO, "approval", aid)
        _set_run_state(run["plan_id"], "running")
        return QUOTE

    monkeypatch.setattr(mission_judge, "_recent_sessions", lambda m: [])
    monkeypatch.setattr(mission_judge, "_checkout_diff", run_starts_mid_gather)
    inp = mission_judge.gather(mid, "/tmp/x")
    assert missions.step_run(mid, TO, 2)["created_at"] > inp.gathered_at

    async def verdict(messages, **_):
        return {
            "met": True,
            "confidence": 0.99,
            "reason": "rewritten",
            "evidence": [{"source": "diff", "quote": QUOTE}],
        }

    monkeypatch.setattr(review, "complete_json", verdict)
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    await mission_judge.judge_row(mid, row, inp, "Judge it.")
    got = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert got["state"] == "pending" and got["observed"]["awaits_rework_run"] == TO


def test_a_threshold_recompute_never_settles_an_old_episode_verdict():
    """The other direction: a verdict gathered BEFORE the new run is fenced and keeps its old read
    start; a threshold lowered after the run started re-scores it and it still does not count."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    inp = mission_judge.JudgeInput(
        sources={"transcript:claude:cv": QUOTE}, fingerprint="fp-old", gathered_at=time.time()
    )
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    fp = mission_judge.row_fingerprint(inp, row)
    _settle(
        mid, key, read_at=inp.gathered_at, judged={**JUDGED, "threshold": 0.95, "fingerprint": fp}
    )
    time.sleep(0.01)
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    wl = {"rows": [next(o for o in missions.objectives(mid) if o["key"] == key)]}
    mission_judge.mark_stale_judgments(mid, wl, inp, 0.9)
    got = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert got["state"] == "pending" and got["observed"]["awaits_rework_run"] == TO


# ---- Hermes on #2227: a held verdict is judged once more when a read would count -----------------


async def _judge_pass(mid: str, monkeypatch, *, start_run_mid_gather=None, reply=None) -> int:
    """One production judge pass (`judge_one`) over unchanged output; returns the model calls."""
    from agent_sessions import review

    calls = {"n": 0}

    def diff(cwd):
        if start_run_mid_gather is not None:
            start_run_mid_gather()
        return QUOTE  # the same output every pass: the same fingerprint

    async def verdict(messages, **_):
        calls["n"] += 1
        return reply or {
            "met": True,
            "confidence": 0.99,
            "reason": "rewritten",
            "evidence": [{"source": "diff", "quote": QUOTE}],
        }

    monkeypatch.setattr(mission_judge, "_recent_sessions", lambda m: [])
    monkeypatch.setattr(mission_judge, "_checkout_diff", diff)
    monkeypatch.setattr(review, "_require_config", lambda: {})
    monkeypatch.setattr(review, "complete_json", verdict)
    await mission_judge.judge_one(mid, mission_judge.Budget(5))
    return calls["n"]


def _row(mid, key) -> dict:
    return next(o for o in missions.objectives(mid) if o["key"] == key)


@pytest.mark.anyio
async def test_a_held_verdict_is_judged_once_more_once_a_read_would_count(monkeypatch):
    """A gather that began before the new run (which starts mid-read) writes a verdict the fence
    holds. The next pass reads the SAME output: reusing that verdict would leave the row pending
    for ever, so it is judged once more, and settles. After that nothing more is spent."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    started: list[str] = []

    def start():
        if not started:
            run = _claim(mid, TO, "approval", aid)
            _set_run_state(run["plan_id"], "running")
            _running(mid)  # the launch completed: the mission runs again, as after any dispatch
            started.append(run["plan_id"])

    assert await _judge_pass(mid, monkeypatch, start_run_mid_gather=start) == 1
    held = _row(mid, key)
    assert held["state"] == "pending" and held["observed"]["awaits_rework_run"] == TO
    # The same output, read in the new episode: judged once more, and it counts.
    assert await _judge_pass(mid, monkeypatch) == 1
    got = _row(mid, key)
    assert got["state"] == "met", got["observed"]
    assert await _judge_pass(mid, monkeypatch) == 0, "a settled row was judged again"


@pytest.mark.anyio
async def test_a_held_verdict_costs_nothing_more_while_no_new_run_has_started(monkeypatch):
    """Before the new episode starts, a re-read of the same output would be held again: the
    verdict is reused, and no model call is paid for it, pass after pass."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    assert await _judge_pass(mid, monkeypatch) == 1
    assert _row(mid, key)["observed"]["awaits_rework_run"] == TO
    for _ in range(3):
        assert await _judge_pass(mid, monkeypatch) == 0
    assert _row(mid, key)["state"] == "pending"


@pytest.mark.anyio
async def test_phase_one_sees_a_held_verdict_due_once_a_read_would_count(monkeypatch):
    """Independent review of #2227: the sweep's phase-1 check (an input remembered from the
    supervision phase) must not skip a held verdict as "nothing due" once the new run started."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    assert await _judge_pass(mid, monkeypatch) == 1  # held: no new run yet
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    _running(mid)
    time.sleep(0.01)
    prior = mission_judge.gather(mid, "/tmp/x")  # the same output, as phase 1 read it
    mission_judge._remember_phase1_input(mid, prior)
    assert await _judge_pass(mid, monkeypatch) == 1, "phase 1 skipped it as nothing due"
    assert _row(mid, key)["state"] == "met"


@pytest.mark.anyio
async def test_a_not_met_re_judge_of_a_held_verdict_is_not_judged_again(monkeypatch):
    """The once-more judgment says NOT met: it is not held (nothing settled), so the idle skip
    applies again — one call, then none."""

    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    assert await _judge_pass(mid, monkeypatch) == 1
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    _running(mid)
    no = {
        "met": False,
        "confidence": 0.05,
        "reason": "the CV lists no project",
        "evidence": [{"source": "diff", "quote": QUOTE}],
    }
    calls = [await _judge_pass(mid, monkeypatch, reply=no) for _ in range(3)]
    assert calls == [1, 0, 0], calls
    got = _row(mid, key)
    assert got["state"] == "pending" and not got["observed"].get("awaits_rework_run")


@pytest.mark.anyio
@pytest.mark.parametrize("guard", ["rejected", "waived"])
async def test_a_held_verdict_never_overrides_the_operators_guards(monkeypatch, guard):
    """A rejected fingerprint and a waived row still win over the once-more judgment."""
    mid, doc, aid, key = _reopened()
    _judged_row(mid, key)
    assert await _judge_pass(mid, monkeypatch) == 1
    run = _claim(mid, TO, "approval", aid)
    _set_run_state(run["plan_id"], "running")
    _running(mid)
    fp = _row(mid, key)["observed"]["judged"]["fingerprint"]
    con = missions._ready(None)
    try:
        if guard == "rejected":
            con.execute(
                "UPDATE mission_objectives SET judge_rejected_fp=? WHERE mission_id=? AND key=?",
                (fp, mid, key),
            )
        else:
            con.execute(
                "UPDATE mission_objectives SET state='waived' WHERE mission_id=? AND key=?",
                (mid, key),
            )
        con.commit()
    finally:
        con.close()
    assert await _judge_pass(mid, monkeypatch) == 0
