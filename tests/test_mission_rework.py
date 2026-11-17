# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""Bounded rework (#1951 §4, #1193's contracts): a review that asks for changes reopens its target
as a new episode, at most `max_rounds` times per edge, consumed once; past the bound, ONE question
and nothing launched.

The plan is #1950's synthetic application flow (`mission_team_fixture`): `review` carries
`rework = {to = "cv", when = "review_ok", max_rounds = 3}`, and `cv → documents → review`.
"""

from __future__ import annotations

import time
import uuid

import pytest

from agent_sessions import mission_plan_document as plan_doc
from agent_sessions import mission_rework, mission_runner, mission_steps, missions
from test_mission_runner import _approve, _grade_drift, _stop_all
from test_mission_steps import (  # noqa: F401 — shared fixtures
    _mission,
    anyio_backend,
    seam,
)
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    host,
    ident,
    project,
)

SRC = "o1.review"
TO = "o1.cv"


@pytest.fixture(autouse=True)
def _fresh_runner():
    mission_runner.reset_for_test()
    yield
    mission_runner.reset_for_test()


def _step(doc: dict, sid: str) -> dict:
    return next(s for s in doc["steps"] if s["id"] == sid)


def _when(doc: dict) -> str:
    return _step(doc, SRC)["rework"]["when"]


def _set_rows(mid: str, doc: dict, steps: tuple[str, ...], state: str) -> None:
    con = missions._ready(None)
    try:
        for sid in steps:
            for k in _step(doc, sid)["evidence"]:
                con.execute(
                    "UPDATE mission_objectives SET state=?, met_at=? WHERE mission_id=? AND key=?",
                    (state, time.time() if state == "met" else None, mid, k),
                )
        con.commit()
    finally:
        con.close()


def _run(mid: str, step: str, episode: int, state: str = "running", **over) -> None:
    """A step run as #1581's claim would have written it (no session: the stop needs none)."""
    row = {
        "mission_id": mid,
        "step_id": step,
        "episode": episode,
        "revision": 1,
        "state": state,
        "plan_id": f"stp_{uuid.uuid4().hex}",
        "session_key": None,
        "engine": "claude-api",
        "specialist_id": "cv-writer",
        "specialist_version": 1,
        "use_kind": "primary",
        "cwd": f"/tmp/rework-{uuid.uuid4().hex[:6]}",
        "authority": "operator",
        "prior_state": "planned",
        "created_at": time.time() - 60,
        "updated_at": time.time() - 60,
    }
    row.update(over)
    con = missions._ready(None)
    try:
        cols = ",".join(row)
        con.execute(
            f"INSERT INTO mission_step_runs ({cols}) VALUES ({','.join('?' for _ in row)})",  # noqa: S608
            tuple(row.values()),
        )
        con.commit()
    finally:
        con.close()


_REVIEW_IDS = iter(range(1000, 10**6))


def _review_asked_for_changes(mid: str, doc: dict, review: str | None = None) -> None:
    """The review's probe saw changes requested, as `forge_review` reports it: `state`, and WHICH
    reviews requested them at which head (a new review each call unless named)."""
    out = missions.observe_objective(
        mid,
        _when(doc),
        observed=True,
        value=False,
        detail="changes were requested",
        extra={
            "state": "REQUEST_CHANGES",
            "reviews": review or str(next(_REVIEW_IDS)),
            "sha": "head1",
        },
    )
    assert out is not None


def _consume(mid: str, doc: dict, episode: int) -> dict | None:
    """The review's episode `episode` asks for changes NOW, and its round is consumed with the
    observation's own identity (as the pass does)."""
    _review_asked_for_changes(mid, doc)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    run = missions.step_run(mid, SRC, episode)
    oid = missions.rework_observation(row, float(run["created_at"]))
    assert oid is not None
    return missions.consume_rework(mid, edge_from=SRC, source_episode=episode, observation_id=oid)


def _doc_rounds(rounds: int) -> dict:
    """The application flow with the review's rework edge at `rounds` (the fixture's says 3), so
    a test of the bound pins what the PLAN says: a pin at other rounds is refused (#2033) and the
    claim falls closed to one run, which would pass a bound test without testing the bound."""
    from mission_team_fixture import FLOW, document
    from test_mission_steps import _assignment

    assert FLOW.count("max_rounds = 3") == 1, "the fixture flow's rework edge moved"
    # The edge lives in the procedure and is projected onto the step, so the plan is built from
    # the flow (a step edited on its own no longer matches its procedure step).
    doc = document(FLOW.replace("max_rounds = 3", f"max_rounds = {rounds}"))
    for s in doc["steps"]:
        if s["actor"] == "agent":
            s["envelope"]["assignment"] = _assignment()
    doc = plan_doc.validate_document(doc)
    assert _step(doc, SRC)["rework"]["max_rounds"] == rounds
    return doc


def _at_review(state: str = "running", *, doc: dict | None = None):
    """A mission whose posting, cv, cover letter and documents are met and whose review runs."""
    mid, doc = _mission(doc=doc, met=("o1.intake",), state="planned")
    _set_rows(mid, doc, ("o1.posting", TO, "o1.cover_letter", "o1.documents"), "met")
    _run(mid, TO, 1, "done")
    _run(mid, SRC, 1, state)
    _running(mid)
    return mid, doc


def _running(mid: str) -> None:
    """The mission runs, as it does while one of its steps runs (only a running mission reworks)."""
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE missions SET state='running', cwd=COALESCE(cwd, '/tmp/rework-mission') "
            "WHERE id=?",
            (mid,),
        )
        con.commit()
    finally:
        con.close()


def _raw(mid: str) -> dict:
    con = missions._ready(None)
    try:
        return {
            r["key"]: dict(r)
            for r in con.execute("SELECT * FROM mission_objectives WHERE mission_id=?", (mid,))
        }
    finally:
        con.close()


def _states(mid: str, doc: dict) -> dict:
    return plan_doc.step_states(doc, missions.objectives(mid))


# ---- the store: consumed once, per edge, reset the span ------------------------------------------


def test_a_round_reopens_the_target_and_resets_every_step_after_it():
    mid, doc = _at_review()
    before = _raw(mid)
    rec = _consume(mid, doc, 1)
    assert rec["new"] and rec["outcome"] == "reopened" and rec["round"] == 1
    # The source run settled in the SAME transaction (Hermes on #2032).
    assert missions.step_run(mid, SRC, 1)["state"] == "done"
    assert rec["edge_to"] == TO and rec["max_rounds"] == 3
    after = _raw(mid)
    for sid in (TO, "o1.documents"):
        for k in _step(doc, sid)["evidence"]:
            assert after[k]["state"] == "pending", k
            # EPISODE FENCING: a new incarnation and probe generation, so an in-flight probe or
            # judgment of the old episode can neither satisfy nor retarget the new one.
            assert after[k]["incarnation"] != before[k]["incarnation"]
            assert after[k]["probe_gen"] == before[k]["probe_gen"] + 1
    for k in _step(doc, "o1.cover_letter")["evidence"]:
        assert after[k]["state"] == "met", "a step that does not come after the target is untouched"
    states = _states(mid, doc)
    assert states[TO] == "ready" and states["o1.documents"] == "waiting"


def test_an_old_episodes_judgment_cannot_settle_the_new_episode():
    mid, doc = _at_review()
    key = _step(doc, TO)["evidence"][0]
    old = "inc-episode-1"
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET incarnation=? WHERE mission_id=? AND key=?",
            (old, mid, key),
        )
        con.commit()
    finally:
        con.close()
    _consume(mid, doc, 1)
    late = missions.observe_objective(
        mid, key, observed=True, value=True, detail="late", expect_incarnation=old
    )
    assert late is None
    assert next(o for o in missions.objectives(mid) if o["key"] == key)["state"] == "pending"


def test_a_reopen_settles_the_pending_decisions_of_the_steps_it_makes_stale():
    """Descendants go stale: a question held on a reset step's evidence is no longer open, and the
    supervisor episode of every reset row advanced (its escalations settle)."""
    mid, doc = _at_review()
    key = _step(doc, "o1.documents")["evidence"][0]
    _set_rows(mid, doc, ("o1.documents",), "pending")
    q = missions.open_question(
        mid,
        key,
        "Which layout?",
        [
            {"label": "A", "action": "note_answer"},
            {"label": "B", "action": "waive_objective"},
        ],
    )
    assert q and missions.open_question_row(mid) is not None
    _set_rows(mid, doc, ("o1.documents",), "met")
    con = missions._ready(None)
    try:
        before = con.execute(
            "SELECT episode FROM mission_objective_episode WHERE mission_id=? AND objective_key=?",
            (mid, key),
        ).fetchone()
    finally:
        con.close()
    _consume(mid, doc, 1)
    assert missions.open_question_row(mid) is None
    con = missions._ready(None)
    try:
        after = con.execute(
            "SELECT episode FROM mission_objective_episode WHERE mission_id=? AND objective_key=?",
            (mid, key),
        ).fetchone()
    finally:
        con.close()
    assert int(after["episode"]) > int(before["episode"] if before else 1)


def test_a_round_is_consumed_once_per_episode_and_per_observation_across_a_restart():
    mid, doc = _at_review()
    first = _consume(mid, doc, 1)
    assert first["new"]
    missions.reset_schema_cache_for_test()  # a restart: a new process reading the same store
    again = missions.consume_rework(mid, edge_from=SRC, source_episode=1, observation_id="obs:b")
    same_obs = missions.consume_rework(
        mid, edge_from=SRC, source_episode=2, observation_id=first["observation_id"]
    )
    assert not again["new"] and again["round"] == 1
    assert not same_obs["new"] and same_obs["round"] == 1
    assert len(missions.reworks(mid)) == 1


def test_the_cap_is_per_edge_and_round_four_reopens_nothing():
    mid, doc = _at_review()
    for n in (1, 2, 3):
        if n > 1:
            _set_rows(mid, doc, (TO, "o1.documents"), "met")
            _run(mid, SRC, n)
        rec = _consume(mid, doc, n)
        assert rec["outcome"] == "reopened" and rec["round"] == n
    _set_rows(mid, doc, (TO, "o1.documents"), "met")
    _run(mid, SRC, 4)
    rec = _consume(mid, doc, 4)
    assert rec["outcome"] == "exhausted" and rec["round"] == 4 and rec["question_at"] is None
    assert _states(mid, doc)[TO] == "done", "an exhausted round reset nothing"
    assert missions.step_run(mid, SRC, 4)["state"] == "done"
    _run(mid, SRC, 5)
    assert _consume(mid, doc, 5) is None
    assert [r["outcome"] for r in missions.reworks(mid)] == ["reopened"] * 3 + ["exhausted"]


def test_nothing_is_consumed_for_a_run_that_is_not_running_or_a_superseded_revision():
    mid, doc = _at_review(state="done")
    assert missions.consume_rework(mid, edge_from=SRC, source_episode=1, observation_id="x") is None
    _run(mid, SRC, 2, revision=1)
    from mission_team_fixture import store_plan

    store_plan(mid, doc)  # revision 2
    assert missions.consume_rework(mid, edge_from=SRC, source_episode=2, observation_id="x") is None
    assert missions.reworks(mid) == []


def test_a_passing_review_that_landed_since_the_caller_looked_consumes_nothing():
    """Hermes on #2032: the store re-derives the observation in the transaction that acts on it."""
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    oid = missions.rework_observation(row, float(missions.step_run(mid, SRC, 1)["created_at"]))
    missions.observe_objective(mid, _when(doc), observed=True, value=True, detail="passes now")
    got = missions.consume_rework(mid, edge_from=SRC, source_episode=1, observation_id=oid)
    assert got is None and missions.reworks(mid) == []
    assert _states(mid, doc)[TO] == "done", "a passing review reset its target"
    assert missions.step_run(mid, SRC, 1)["state"] == "running"


def test_a_failure_inside_the_round_leaves_nothing_half_done(monkeypatch):
    """Hermes on #2032: the round, the reset and the source run commit together or not at all."""
    mid, doc = _at_review()

    def boom(*a, **k):
        raise RuntimeError("injected")

    real = missions._finish_step_run_tx
    monkeypatch.setattr(missions, "_finish_step_run_tx", boom)
    with pytest.raises(RuntimeError):
        _consume(mid, doc, 1)
    assert missions.reworks(mid) == []
    assert _states(mid, doc)[TO] == "done"
    assert missions.step_run(mid, SRC, 1)["state"] == "running"
    # (Never `monkeypatch.undo()`: it would also drop conftest's store pins.)
    monkeypatch.setattr(missions, "_finish_step_run_tx", real)
    assert _consume(mid, doc, 1)["new"], "the next pass finds the same request again"


def test_a_step_with_no_rework_edge_consumes_nothing():
    mid, _doc = _at_review()
    _run(mid, "o1.documents", 2)
    got = missions.consume_rework(
        mid, edge_from="o1.documents", source_episode=2, observation_id="x"
    )
    assert got is None


# ---- the pass: evidence, never prose ----------------------------------------------------------


@pytest.mark.anyio
async def test_a_review_that_asked_for_changes_consumes_one_round_and_the_run_settles():
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    assert await mission_rework.sweep(mid) == 1
    run = next(r for r in missions.step_runs(mid) if r["step_id"] == SRC)
    assert run["state"] == "done" and "rework round 1 of 3" in run["detail"]
    assert _states(mid, doc)[TO] == "ready"
    # A repeated sweep of the same evidence consumes nothing.
    assert await mission_rework.sweep(mid) == 0
    assert len(missions.reworks(mid)) == 1
    view = mission_steps.view_sync(mid)
    chip = next(s for s in view["steps"] if s["id"] == TO)["rework"]
    assert {k: chip[k] for k in ("round", "max_rounds", "exhausted", "text")} == {
        "round": 1,
        "max_rounds": 3,
        "exhausted": False,
        "text": "rework 1 of 3",
    }


@pytest.mark.anyio
async def test_evidence_from_before_the_episode_started_never_counts():
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    con = missions._ready(None)
    try:  # the review's current episode started AFTER that observation
        con.execute(
            "UPDATE mission_step_runs SET created_at=? WHERE mission_id=? AND step_id=?",
            (time.time() + 60, mid, SRC),
        )
        con.commit()
    finally:
        con.close()
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []


@pytest.mark.anyio
async def test_a_stale_read_or_a_met_review_asks_for_nothing():
    mid, doc = _at_review()
    missions.observe_objective(mid, _when(doc), observed=False, value=False, detail="unreachable")
    assert await mission_rework.sweep(mid) == 0
    missions.observe_objective(mid, _when(doc), observed=True, value=True, detail="passes")
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []


@pytest.mark.anyio
async def test_a_verdict_on_output_the_agent_has_moved_past_is_not_the_reviews_answer(monkeypatch):
    from agent_sessions import mission_judge

    mid, doc = _at_review()
    obs = {
        "at": time.time(),
        "detail": "not met",
        "value": False,
        "judged": {
            "met": False,
            "confidence": 0.05,
            "threshold": 0.9,
            "evidence": [{"source": "transcript:review", "quote": "the summary misses sources"}],
            "fingerprint": "old",
        },
    }
    con = missions._ready(None)
    try:
        import json

        con.execute(
            "UPDATE mission_objectives SET observed=? WHERE mission_id=? AND key=?",
            (json.dumps(obs), mid, _when(doc)),
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(mission_judge, "gather", lambda mid_, cwd: object())
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "new")
    assert await mission_rework.sweep(mid) == 0
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "old")
    assert await mission_rework.sweep(mid) == 1


@pytest.mark.anyio
async def test_past_the_bound_exactly_one_question_and_nothing_reopened():
    mid, doc = _at_review()
    for n in (1, 2, 3):
        if n > 1:
            _set_rows(mid, doc, (TO, "o1.documents"), "met")
            _run(mid, SRC, n, created_at=time.time() - 30)
        _review_asked_for_changes(mid, doc)
        assert await mission_rework.sweep(mid) == 1
    _set_rows(mid, doc, (TO, "o1.documents"), "met")
    _run(mid, SRC, 4, created_at=time.time() - 30)
    _review_asked_for_changes(mid, doc)
    assert await mission_rework.sweep(mid) == 1
    assert _states(mid, doc)[TO] == "done", "the exhausted round reopened the target"
    q = missions.open_question_row(mid)
    assert q is not None and q["objective"] == _when(doc)
    assert "a 4th time" in q["question"] and "covers 3 rework rounds" in q["question"]
    assert {o["action"] for o in q["options"]} == set(mission_rework.QUESTION_ACTIONS)
    events = [
        e for e in missions.get_mission(mid, events_limit=200)["events"] if e["kind"] == "question"
    ]
    assert len(events) == 1
    # Further passes ask nothing more (deduplicated by edge and round).
    assert await mission_rework.sweep(mid) == 0
    events = [
        e for e in missions.get_mission(mid, events_limit=200)["events"] if e["kind"] == "question"
    ]
    assert len(events) == 1
    chip = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == SRC)["rework"]
    assert chip["exhausted"] and chip["text"].startswith("rework used")


# ---- the bound in the claim -------------------------------------------------------------------


def test_beyond_the_approved_bound_a_launch_nobody_tapped_is_refused():
    """1 + max_rounds runs of each step in the edge's span; the operator's own Start is not
    bounded. The plan's edge says ONE round (`_doc_rounds`), so the pins are the plan's and the
    bound under test is 1 + 1 = 2, not the fail-closed 1 of a refused pin."""
    mid, doc = _mission(
        doc=_doc_rounds(1), met=("o1.intake", "o1.posting", "o1.cv", "o1.cover_letter")
    )
    pins = {"steps": [{"id": "o1.documents"}], "rework": _rework_pins(mid)}
    import json

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

    def claim(authority: str):
        return missions.claim_step(
            mid,
            "o1.documents",
            revision=1,
            engine="claude-api",
            model=None,
            effort=None,
            specialist=("cv-writer", 1),
            project_id="p-synthetic01",
            cwd="/tmp/bound",
            brief="b",
            authority=authority,
            approval_id="apr-b" if authority != "operator" else None,
        )

    # HQ starts each step once: with no reopen since its last run, it is `already_ran`.
    with pytest.raises(missions.StepRefused) as e:
        claim("approval")
    assert e.value.code == "already_ran"
    # A reopen since then lets a launch nobody tapped for in, up to the approved bound (1 + 1).
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
    with pytest.raises(missions.StepRefused) as e:
        claim("approval")
    assert e.value.code == "bound_reached"
    assert claim("operator")["plan_id"].startswith("stp_")


# ---- on the real path: the reopen is a new episode, admitted like any launch -----------------


@pytest.mark.anyio
async def test_the_reopen_is_a_new_episode_and_a_stale_pin_refuses_it(seam, monkeypatch):
    """The reopen goes through the runner and #1581's one seam like any launch: a stale pin (here
    a grade drift) refuses it with nothing claimed, and once the pin holds again the SAME approval
    starts the target as episode 2, in a new session."""
    from test_mission_runner import _bell

    bell = _bell(monkeypatch)
    mid, doc = seam["mid"], seam["doc"]
    _approve(mid)
    _set_rows(mid, doc, ("o1.posting", TO, "o1.cover_letter", "o1.documents"), "met")
    approval = missions.approval(mid)
    # Episode 1 of cv ran under this approval: HQ starts each step ONCE, and a rework reopen is
    # the only further episode the approval covers.
    _run(mid, TO, 1, "done", specialist_id=seam["spec"]["id"], approval_id=approval["id"])
    _run(mid, SRC, 1, specialist_id=seam["spec"]["id"], approval_id=approval["id"])
    _running(mid)
    view = mission_steps.view_sync(mid)
    assert next(s for s in view["steps"] if s["id"] == TO)["runner_done"]
    _review_asked_for_changes(mid, doc)
    assert await mission_rework.sweep(mid) == 1
    view = mission_steps.view_sync(mid)
    assert not next(s for s in view["steps"] if s["id"] == TO)["runner_done"]
    good = seam["team"]["ctx"]
    _grade_drift(seam)
    out = await mission_runner.advance(mid, now=0.0)
    assert out.get(TO) == "refused", out
    assert [r["episode"] for r in missions.step_runs(mid) if r["step_id"] == TO] == [1]
    assert any(r["step"] == TO for r in missions.refusals(mid))
    # …and it ASKS, like any launch refused nobody tapped for (fifth independent review): the step
    # ran before, so "it started since" counts only runs after the offer.
    assert len(bell) == 1, "a refused rework reopen was recorded but never notified"
    seam["team"]["ctx"] = good
    out = await mission_runner.advance(mid, now=mission_runner.RETRY_S + 1)
    assert out.get(TO) == "started", out
    run2 = next(r for r in missions.step_runs(mid) if r["step_id"] == TO and r["episode"] == 2)
    assert run2["session_key"] and run2["authority"] == "approval"
    await _stop_all(seam)


def test_the_schema_is_v43_and_rolls_back_to_42(tmp_path):
    import sqlite3

    db = tmp_path / "missions.sqlite3"
    missions._ready(db).close()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 43
    assert con.execute("SELECT name FROM sqlite_master WHERE name='mission_reworks'").fetchone()
    con.close()
    assert missions.rollback_schema(42, path=db) == {"from": 43, "to": 42}
    missions.reset_schema_cache_for_test()
    missions._ready(db).close()  # re-opening converges
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 43
    con.close()


def test_rework_rounds_leave_with_their_mission():
    mid, doc = _at_review()
    _consume(mid, doc, 1)
    assert missions.delete_mission(mid)
    con = missions._ready(None)
    try:
        assert con.execute("SELECT COUNT(*) FROM mission_reworks").fetchone()[0] == 0
    finally:
        con.close()


# ---- the one question: atomic with its obligation, retried on a transient failure ---------------


def _exhaust(mid: str, doc: dict) -> None:
    for n in (1, 2, 3, 4):
        if n > 1:
            _set_rows(mid, doc, (TO, "o1.documents"), "met")
            _run(mid, SRC, n)
        _consume(mid, doc, n)
    assert missions.reworks(mid)[-1]["outcome"] == "exhausted"


def _questions(mid: str) -> list[dict]:
    return [
        e for e in missions.get_mission(mid, events_limit=500)["events"] if e["kind"] == "question"
    ]


@pytest.mark.anyio
async def test_a_transient_fence_failure_leaves_the_question_owed_and_it_is_asked_next_pass(
    monkeypatch,
):
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    _exhaust(mid, doc)
    real = mission_fence.fenced_write
    calls = {"n": 0}

    async def busy_once(mission_id, fn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise missions.MissionError("the session is busy; try again", status=503)
        return await real(mission_id, fn, **kw)

    monkeypatch.setattr(mission_fence, "fenced_write", busy_once)
    assert await mission_rework.ask_owed(mid) == 0
    assert _questions(mid) == [] and missions.reworks(mid)[-1]["question_at"] is None
    assert await mission_rework.ask_owed(mid) == 1
    assert len(_questions(mid)) == 1 and missions.reworks(mid)[-1]["question_at"] is not None


@pytest.mark.anyio
async def test_overlapping_passes_open_one_question_per_round():
    import asyncio

    mid, doc = _at_review()
    _exhaust(mid, doc)
    got = await asyncio.gather(mission_rework.ask_owed(mid), mission_rework.ask_owed(mid))
    assert sum(got) == 1
    assert len(_questions(mid)) == 1


@pytest.mark.anyio
async def test_a_question_that_can_never_be_asked_is_retired_not_retried():
    mid, doc = _at_review()
    _exhaust(mid, doc)
    _set_rows(mid, doc, (SRC,), "waived")  # the operator waived the review meanwhile
    assert await mission_rework.ask_owed(mid) == 0
    assert _questions(mid) == []
    assert missions.reworks(mid)[-1]["question_at"] is not None
    # …and the board does not claim the operator was asked.
    chip = mission_rework.view(mid, doc)[SRC]
    assert chip["text"] == "rework used: 3 of 3; the review was settled, nothing to ask"


@pytest.mark.anyio
async def test_a_candidate_whose_review_passed_since_is_not_settled():
    """Hermes on #2032: the pass re-checks the candidate before it stops or resets anything."""
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    (cand,) = mission_rework.candidates_sync(mid)
    missions.observe_objective(mid, _when(doc), observed=True, value=True, detail="passes now")
    assert mission_rework.candidates_sync(mid) == []
    assert await mission_rework._settle_one(mid, cand) is None
    assert missions.reworks(mid) == []
    assert missions.step_run(mid, SRC, 1)["state"] == "running"


# ---- independent review of #2032 at e6099e10: each finding pinned ------------------------------


def _set_obs(mid: str, doc: dict, obs: dict) -> None:
    import json

    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET observed=? WHERE mission_id=? AND key=?",
            (json.dumps(obs), mid, _when(doc)),
        )
        con.commit()
    finally:
        con.close()


@pytest.mark.anyio
async def test_a_met_verdict_under_the_confidence_floor_is_not_a_request_for_changes(monkeypatch):
    """Finding 1 (HIGH): the store keeps a met verdict below the floor as `value=False`; that is
    unknown, never "changes requested", and consumes no round. A not-met verdict does."""
    from agent_sessions import mission_judge

    mid, doc = _at_review()
    monkeypatch.setattr(mission_judge, "gather", lambda mid_, cwd: object())
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "fp")
    low = {"met": True, "confidence": 0.85, "fingerprint": "fp"}
    _set_obs(mid, doc, {"at": time.time(), "detail": "met, unsure", "value": False, "judged": low})
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []
    assert next(r for r in missions.step_runs(mid) if r["step_id"] == SRC)["state"] == "running"
    no = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "evidence": [{"source": "transcript:review", "quote": "the cv lists no project"}],
        "fingerprint": "fp",
    }
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": no})
    assert await mission_rework.sweep(mid) == 1


class _Fence:
    """`mission_fence.fenced_step_stop` as it behaves: `idle()` is asked inside the fence, and
    only a True answer stops the session (`stopped`); otherwise `busy`, nothing stopped."""

    def __init__(self) -> None:
        self.calls = 0
        self.stopped = 0

    async def __call__(self, mission_id, key, settle, *, idle=None):
        self.calls += 1
        outcome = "stopped" if idle is None or idle() else "busy"
        self.stopped += outcome == "stopped"
        settle(outcome)
        return outcome


KEY = "claude-api:9a41c0de-1111-4111-8111-2c7b2c7b2c7b"


@pytest.mark.anyio
async def test_a_request_past_an_exhausted_edge_stops_nothing(monkeypatch):
    """Finding 2: once the rounds are used, a review the operator starts again and that asks for
    changes is not stopped (the store would consume nothing), and its run is left running to the
    ordinary reconcile, not stopped on every pass."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    _exhaust(mid, doc)
    _set_rows(mid, doc, (TO, "o1.documents"), "met")
    _run(mid, SRC, 5, session_key=KEY, created_at=time.time() - 30)
    _review_asked_for_changes(mid, doc)
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(mission_rework, "_idle", lambda key: True)
    assert mission_rework.candidates_sync(mid) == []
    for _ in range(3):
        assert await mission_rework.sweep(mid) == 0
    assert fence.calls == 0, "the session of a request past the bound was stopped"
    run5 = next(r for r in missions.step_runs(mid) if r["step_id"] == SRC and r["episode"] == 5)
    assert run5["state"] == "running"
    assert len(missions.reworks(mid)) == 4


@pytest.mark.anyio
async def test_the_decision_is_asked_again_inside_the_fence_before_the_stop(monkeypatch):
    """Finding 2, the fence half: a candidate read before the edge was exhausted (its observation
    still current) stops nothing once the store would consume nothing any more."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    _exhaust(mid, doc)
    _set_rows(mid, doc, (TO, "o1.documents"), "met")
    _run(mid, SRC, 5, session_key=KEY, created_at=time.time() - 30)
    _review_asked_for_changes(mid, doc)
    run5 = missions.step_run(mid, SRC, 5)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    oid = missions.rework_observation(row, float(run5["created_at"]))
    assert oid is not None, "the request itself is current"
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(mission_rework, "_idle", lambda key: True)
    cand = {"run": run5, "edge": _step(doc, SRC)["rework"], "observation_id": oid}
    assert await mission_rework._settle_one(mid, cand) is None
    assert fence.calls == 1 and fence.stopped == 0, "a round nobody would consume stopped it"
    assert missions.step_run(mid, SRC, 5)["state"] == "running"


@pytest.mark.anyio
async def test_a_session_stopped_for_a_round_that_was_not_taken_does_not_stay_running(monkeypatch):
    """Finding 2, the race: the decision said "consume", the session was proved stopped, and the
    consume then took nothing. The run settles `stopped` instead of staying `running`."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_step_runs SET session_key=? WHERE mission_id=? AND step_id=?",
            (KEY, mid, SRC),
        )
        con.commit()
    finally:
        con.close()
    _review_asked_for_changes(mid, doc)
    monkeypatch.setattr(mission_fence, "fenced_step_stop", _Fence())
    monkeypatch.setattr(mission_rework, "_idle", lambda key: True)
    monkeypatch.setattr(missions, "consume_rework", lambda *a, **k: None)
    assert await mission_rework.sweep(mid) == 0
    run = next(r for r in missions.step_runs(mid) if r["step_id"] == SRC)
    assert run["state"] == "stopped" and "rework round that was not taken" in run["detail"]


@pytest.mark.anyio
async def test_the_exhausted_question_raises_one_notification_retracted_when_it_settles(
    monkeypatch,
):
    """Finding 5b: ONE bell row (and push) per exhausted round while its question holds, from one
    producer, raised once; the answer retracts it, and nothing raises it again."""
    from agent_sessions import notifications
    from test_mission_runner import _bell

    bell = _bell(monkeypatch)
    mid, doc = _at_review()
    _exhaust(mid, doc)
    await mission_rework.ask_owed(mid)
    key = mission_rework.notice_key(mid, SRC, 4)

    def rows() -> list[dict]:
        return [r for r in notifications.listing()["notifications"] if r.get("action_id") == key]

    assert mission_rework.notify_sync(mid) == 1
    assert mission_rework.notify_sync(mid) == 0
    assert await mission_rework.sweep(mid) == 0
    assert len(rows()) == 1 and len(bell) == 1
    assert "Mission asks you" in rows()[0]["title"]
    q = missions.open_question_row(mid)
    missions.answer_question(mid, q["seq"], option_index=0)
    assert mission_rework.retract_sync(mid) == 1
    assert rows() == [], "the answered question's bell row outlived it"
    assert mission_rework.notify_sync(mid) == 0 and len(bell) == 1
    assert missions.reworks(mid)[-1]["notice_closed_at"] is not None


def _rework_pins(mid: str) -> list[dict]:
    """The plan's rework pins as an approval records them (`mission_approval.rework_pins`, the
    reset span, #2033). Never with other rounds: such a pin is refused and bounds nothing, so a
    test wanting another bound changes the PLAN (`_doc_rounds`), and one wanting a stale pin says
    so with `_pin(rework=...)`."""
    from agent_sessions import mission_approval

    return mission_approval.rework_pins(missions.plan_revision(mid)["document"])


def _bound(mid: str, aid: str, step: str) -> int | None:
    """The runs of `step` the approval covers, as the claim reads it (`_approved_runs_tx`); None
    is the fail-closed answer of pins that can't say."""
    con = missions._ready(None)
    try:
        return missions._approved_runs_tx(
            con, mid, aid, step, missions.plan_revision(mid)["document"]
        )
    finally:
        con.close()


def _pin(mid: str, steps: tuple[str, ...], *, rework=None) -> str:
    """An approval in force pinning `steps` and the review's rework edge (its reset span, as
    `pins` records it; `rework` overrides the edges, e.g. an approval given before #2033)."""
    import json

    aid = f"apr-{uuid.uuid4().hex[:20]}"
    pins = {
        "steps": [{"id": s} for s in steps],
        "rework": _rework_pins(mid) if rework is None else rework,
    }
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) VALUES (?, ?, 1, 'd', ?, ?, 'r', 1)",
            (aid, mid, json.dumps(pins), uuid.uuid4().hex),
        )
        con.commit()
    finally:
        con.close()
    return aid


def _claim(mid: str, step: str, authority: str, aid: str | None, **kw) -> dict:
    return missions.claim_step(
        mid,
        step,
        revision=1,
        engine="claude-api",
        model=None,
        effort=None,
        specialist=("cv-writer", 1),
        project_id="p-synthetic01",
        cwd=f"/tmp/rework-claim-{uuid.uuid4().hex[:6]}",
        brief="b",
        authority=authority,
        approval_id=aid if authority != "operator" else None,
        **kw,
    )


def _land(mid: str, plan_id: str, state: str = "stopped") -> None:
    """The claimed launch settles (as the dispatch would): the run ends, the slot frees."""
    con = missions._ready(None)
    try:
        con.execute("UPDATE mission_step_runs SET state=? WHERE plan_id=?", (state, plan_id))
        con.execute("DELETE FROM mission_dispatches WHERE plan_id=?", (plan_id,))
        con.execute(
            "UPDATE missions SET state='running', cwd=COALESCE(cwd, '/tmp/rework-mission') "
            "WHERE id=?",
            (mid,),
        )
        con.commit()
    finally:
        con.close()


def _code(fn) -> str:
    with pytest.raises(missions.StepRefused) as e:
        fn()
    return e.value.code


def test_after_a_reopen_hq_starts_the_target_once_and_then_already_ran():
    """Finding 8: a reopen lets HQ claim cv ONCE more (episode 2, which consumes it); a second
    claim under the approval is `already_ran`."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "not_ready"
    _consume(mid, doc, 1)
    run2 = _claim(mid, TO, "approval", aid)
    assert run2["run"]["episode"] == 2 and run2["run"]["rework_ref"] == f"{SRC}#1"
    _land(mid, run2["plan_id"])
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "already_ran"
    assert TO not in missions.open_reopens(mid, doc)
    view = mission_steps.view_sync(mid)
    assert next(s for s in view["steps"] if s["id"] == TO)["runner_done"]


def test_a_reopen_of_cv_does_not_let_a_step_outside_its_span_run_again():
    """Finding 8: cover_letter does not come after cv, so cv's reopen gives it no further run."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    _run(mid, "o1.cover_letter", 1, "stopped")
    _set_rows(mid, doc, ("o1.cover_letter",), "pending")
    _consume(mid, doc, 1)
    assert "o1.cover_letter" not in missions.open_reopens(mid, doc)
    assert _code(lambda: _claim(mid, "o1.cover_letter", "approval", aid)) == "already_ran"


def test_a_reopen_is_consumed_by_a_record_never_by_comparing_clocks():
    """Finding 7: an operator's run claimed AFTER the reopen consumes it even when its clock reads
    earlier than the reopen's (both are taken before the store's lock), so HQ never restarts a
    step the operator started and stopped."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    t = time.time()
    _review_asked_for_changes(mid, doc)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    oid = missions.rework_observation(row, float(missions.step_run(mid, SRC, 1)["created_at"]))
    missions.consume_rework(mid, edge_from=SRC, source_episode=1, observation_id=oid, now=t)
    mine = _claim(mid, TO, "operator", None, now=t - 10)
    assert mine["run"]["rework_ref"] == f"{SRC}#1"
    _land(mid, mine["plan_id"])
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "already_ran"


def test_the_servers_words_never_advise_what_it_would_refuse():
    """Finding 6: past the bound the target's evidence is not reset, so "start o1.cv yourself" is
    refused `not_ready`; and a running mission can't be approved again."""
    text = mission_rework.question_text(
        {"round": 4, "max_rounds": 3, "edge_from": SRC, "edge_to": TO}
    )
    assert f"start {TO}" not in text
    assert f"start {SRC} again yourself" in text
    mid, doc = _mission(
        doc=_doc_rounds(1), met=("o1.intake", "o1.posting", "o1.cv", "o1.cover_letter")
    )
    aid = _pin(mid, ("o1.documents",))
    assert _bound(mid, aid, "o1.documents") == 2  # 1 + the plan's one round, not fail-closed
    _run(mid, "o1.documents", 1, "done")
    _run(mid, "o1.documents", 2, "stopped")
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_reworks (mission_id, edge_from, edge_to, round, max_rounds, "
            "source_episode, observation_id, revision, outcome, granted_json, created_at) "
            "VALUES (?, ?, ?, 1, 1, 1, 'obs:x', 1, 'reopened', "
            '\'["o1.cv", "o1.documents", "o1.review"]\', ?)',
            (mid, SRC, TO, time.time()),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o1.documents", "approval", aid)
    assert e.value.code == "bound_reached"
    assert "approve" not in str(e.value) and "start it yourself" in str(e.value)


@pytest.mark.anyio
async def test_bound_reached_is_one_decision_and_one_notice_and_is_not_offered_again(monkeypatch):
    """Finding 5a: a claim refused past the approved bound is recorded like any refusal (once per
    change), raises ONE bell row, shows on the board, and the runner offers the step no more."""
    from test_mission_runner import _bell

    bell = _bell(monkeypatch)
    mid, doc = _mission(
        doc=_doc_rounds(1), met=("o1.intake", "o1.posting", "o1.cv", "o1.cover_letter")
    )
    aid = _pin(mid, ("o1.documents",))
    assert _bound(mid, aid, "o1.documents") == 2  # 1 + the plan's one round, not fail-closed
    _run(mid, "o1.documents", 1, "done", approval_id=aid, authority="approval")
    _run(mid, "o1.documents", 2, "stopped")  # the operator's own run counts toward the bound
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_reworks (mission_id, edge_from, edge_to, round, max_rounds, "
            "source_episode, observation_id, revision, outcome, granted_json, created_at) "
            "VALUES (?, ?, ?, 1, 1, 1, 'obs:x', 1, 'reopened', "
            '\'["o1.cv", "o1.documents", "o1.review"]\', ?)',
            (mid, SRC, TO, time.time()),
        )
        con.commit()
    finally:
        con.close()

    async def start(mission_id, step_id, revision, use, registry):
        return _claim(mission_id, step_id, "approval", aid)

    monkeypatch.setattr(mission_runner, "_start", start)
    step = {"id": "o1.documents", "alternates": 0}
    for _ in range(2):
        got = await mission_runner._offer(mid, "Apply", step, 1, None)
        assert got[0] == "refused"
    refs = [r for r in missions.refusals(mid) if r["step"] == "o1.documents"]
    assert len(refs) == 1 and refs[0]["reasons"][0]["code"] == "bound_reached"
    assert len(bell) == 1, "past the bound must be asked once, not silent and not twice"
    # Another run of the operator's since: a NEW decision (one per approval, step and run count).
    _run(mid, "o1.documents", 3, "stopped")
    assert (await mission_runner._offer(mid, "Apply", step, 1, None))[0] == "refused"
    assert len(bell) == 2, "a refusal at a new run count was taken for the old one"
    assert "the runs your approval covers for it are used" in bell[0]["reason"]
    view = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == "o1.documents")
    assert view["runner_done"], "the runner would offer a step past its bound again"
    assert view["refusal"]["codes"] == ["bound_reached"]
    assert "approve" not in view["refusal"]["remedy"].lower()


def test_a_refused_launch_does_not_consume_the_reopen():
    """A run that ends `refused` started nothing: the reopen it named is still the one further
    episode HQ may start."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    _consume(mid, doc, 1)
    first = _claim(mid, TO, "approval", aid)
    _land(mid, first["plan_id"], "refused")
    again = _claim(mid, TO, "approval", aid)
    assert again["run"]["rework_ref"] == f"{SRC}#1"


@pytest.mark.anyio
async def test_a_crash_between_the_bell_row_and_its_record_announces_nothing_twice(monkeypatch):
    """The exhausted question's row is announced at most once EVER (`once`): a pass that raised
    the row and failed to record it does not raise a second one on the next pass."""
    from agent_sessions import notifications
    from test_mission_runner import _bell

    bell = _bell(monkeypatch)
    mid, doc = _at_review()
    _exhaust(mid, doc)
    await mission_rework.ask_owed(mid)
    real = missions.mark_rework_notice

    def crash(*a, **k):
        raise RuntimeError("crashed after the row")

    monkeypatch.setattr(missions, "mark_rework_notice", crash)
    mission_rework.notify_sync(mid)
    monkeypatch.setattr(missions, "mark_rework_notice", real)
    mission_rework.notify_sync(mid)
    key = mission_rework.notice_key(mid, SRC, 4)
    assert len(bell) == 1
    rows = [r for r in notifications.listing()["notifications"] if r.get("action_id") == key]
    assert len(rows) == 1


@pytest.mark.anyio
async def test_the_next_pass_retracts_an_answered_questions_notification():
    """The sweep retracts too (a question settled any way: answered here), not only the routes."""
    mid, doc = _at_review()
    _exhaust(mid, doc)
    await mission_rework.sweep(mid)
    assert missions.reworks(mid)[-1]["notice_at"] is not None
    q = missions.open_question_row(mid)
    missions.answer_question(mid, q["seq"], option_index=0)
    await mission_rework.sweep(mid)
    assert missions.reworks(mid)[-1]["notice_closed_at"] is not None


@pytest.mark.anyio
async def test_a_question_on_a_review_the_operator_stood_down_is_retired_not_retried():
    """ "Stop telling me" is as final for the owed question as a waive: `open_question` refuses it
    every time, so it is retired instead of being retried on every pass."""
    mid, doc = _at_review()
    _exhaust(mid, doc)
    episode = missions.objective_hold(mid, _when(doc))[0]
    assert missions.stand_down(mid, _when(doc), episode=episode)
    assert await mission_rework.ask_owed(mid) == 0
    assert _questions(mid) == []
    assert missions.reworks(mid)[-1]["question_at"] is not None


# ---- second independent review of #2032 (at 2093642d) -------------------------------------------


def _parallel_mission():
    """The flow with `cover_letter` AFTER cv but not before review: a reopen of cv resets it (it
    comes after the target) while the approval's span (cv through review) does not hold it."""
    import mission_team_fixture as mtf
    from agent_sessions import mission_plan_document
    from test_mission_steps import _assignment

    flow = mtf.FLOW
    i = flow.index('id = "cover_letter"')
    j = flow.index('after = ["posting"]', i)
    flow = flow[:j] + 'after = ["cv"]' + flow[j + len('after = ["posting"]') :]
    flow = flow.replace('after = ["cv", "cover_letter"]', 'after = ["cv"]')
    d = mtf.document(flow)
    for s in d["steps"]:
        if s["actor"] == "agent":
            s["envelope"]["assignment"] = _assignment()
    mid, doc = _mission(doc=mission_plan_document.validate_document(d), met=("o1.intake",))
    _set_rows(mid, doc, ("o1.posting", TO, "o1.cover_letter", "o1.documents"), "met")
    return mid, doc


def test_a_reopen_grants_every_reset_step_that_ran_parallel_branches_included():
    """#2033 (the operator's decision, D-P6-14): the approved span IS the reset span. A parallel
    branch after the target (cover_letter: after cv, not before review) is reset, granted a
    further run and started by HQ under the approval, with no tap; and it is fenced like the
    target, so a fresh read of its old output can't settle it before its new run (#2035)."""
    from agent_sessions import mission_approval

    mid, doc = _parallel_mission()
    span = missions.rework_reset_steps(doc, SRC, TO)
    assert "o1.cover_letter" in span and "o1.record" in span
    assert mission_approval.rework_pins(doc)[0]["steps"] == span
    agents = tuple(s["id"] for s in doc["steps"] if s["actor"] == "agent")
    aid = _pin(mid, agents)
    for sid in (TO, "o1.cover_letter", "o1.documents"):
        _run(mid, sid, 1, "done")
    _run(mid, SRC, 1)
    _running(mid)
    rec = _consume(mid, doc, 1)
    assert rec["outcome"] == "reopened"
    # Every reset step that had run; pull_request and record never ran, so collect nothing.
    assert missions.rework_granted(rec) == [TO, "o1.cover_letter", "o1.documents", SRC]
    letter = _step(doc, "o1.cover_letter")["evidence"][0]
    assert {o["key"]: o for o in missions.objectives(mid)}[letter]["state"] == "pending"
    assert missions.open_reopens(mid, doc)["o1.cover_letter"] == f"{SRC}#1"
    got = missions.observe_objective(mid, letter, observed=True, value=True, detail="written")
    assert got["state"] == "pending", "the parallel branch's old output skipped its new run"
    assert got["observed"]["awaits_rework_run"] == "o1.cover_letter"
    c = _claim(mid, TO, "approval", aid)
    _land(mid, c["plan_id"], "done")
    _set_rows(mid, doc, (TO,), "met")
    letter_run = _claim(mid, "o1.cover_letter", "approval", aid)
    assert letter_run["run"]["rework_ref"] == f"{SRC}#1"
    texts = [e["text"] for e in missions.get_mission(mid, events_limit=200)["events"]]
    assert not any("must be started again by you" in (t or "") for t in texts)


@pytest.mark.anyio
async def test_a_step_that_never_ran_collects_no_reopens():
    """Finding 1 (b): steps after the source never ran between rounds; a reopen grants them
    nothing, so their first run is no rework and a Stop is never turned into a restart."""
    mid, doc = _at_review()
    _exhaust(mid, doc)
    reopens = missions.open_reopens(mid, doc)
    assert "o1.pull_request" not in reopens
    view = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == "o1.pull_request")
    assert view["rework"] is None, "a step that never ran shows a rework round"


def test_a_reopen_waits_while_a_step_it_would_reset_still_runs():
    """Finding 1 (c): an open episode of a step in the reset span could answer the NEW episode's
    evidence, so nothing is consumed until it settled."""
    mid, doc = _at_review()
    _run(mid, "o1.documents", 1, "running", cwd="/tmp/rework-docs-open")
    assert _consume(mid, doc, 1) is None
    assert missions.reworks(mid) == []
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_step_runs SET state='done' WHERE mission_id=? AND step_id=?",
            (mid, "o1.documents"),
        )
        con.commit()
    finally:
        con.close()
    assert _consume(mid, doc, 1)["new"]


def test_only_a_running_mission_reworks():
    """Finding 3: a mission the operator moved to review keeps the evidence it was judged on."""
    mid, doc = _at_review()
    con = missions._ready(None)
    try:
        con.execute("UPDATE missions SET state='review' WHERE id=?", (mid,))
        con.commit()
    finally:
        con.close()
    assert _consume(mid, doc, 1) is None
    assert missions.reworks(mid) == []
    assert _states(mid, doc)[TO] == "done"


def test_a_waived_row_stays_waived_through_a_reopen():
    mid, doc = _at_review()
    key = _step(doc, "o1.documents")["evidence"][0]
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET state='waived' WHERE mission_id=? AND key=?",
            (mid, key),
        )
        con.commit()
    finally:
        con.close()
    assert _consume(mid, doc, 1)["outcome"] == "reopened"
    assert next(o for o in missions.objectives(mid) if o["key"] == key)["state"] == "waived"


@pytest.mark.anyio
async def test_the_reconcile_pass_runs_the_rework_sweep():
    """`mission_steps.reconcile` (every supervisor pass and every start) is where rework runs."""
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    await mission_steps.reconcile(mid)
    assert [r["outcome"] for r in missions.reworks(mid)] == ["reopened"]


def test_a_v42_store_upgrades_to_v43_with_the_rework_column(tmp_path):
    """The migration adds `mission_step_runs.rework_ref` to a store a v42 build wrote."""
    import sqlite3

    db = tmp_path / "missions.sqlite3"
    missions._ready(db).close()
    con = sqlite3.connect(db)
    con.execute("DROP TABLE mission_reworks")
    con.execute("ALTER TABLE mission_step_runs DROP COLUMN rework_ref")
    con.execute("PRAGMA user_version=42")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions._ready(db).close()
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_step_runs)")}
    assert "rework_ref" in cols
    assert con.execute("SELECT name FROM sqlite_master WHERE name='mission_reworks'").fetchone()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 43
    con.close()


def test_an_approved_span_step_that_never_ran_gets_no_reopen_and_runs_once():
    """Inside the approved span too, only a step that had run is granted a further run: one that
    never ran (here documents, whose evidence was met without a run) starts as its first run,
    consumes nothing, and a Stop of it is never restarted by HQ."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    rec = _consume(mid, doc, 1)
    assert missions.rework_granted(rec) == [TO, SRC]
    _set_rows(mid, doc, (TO,), "met")
    first = _claim(mid, "o1.documents", "approval", aid)
    assert first["run"]["rework_ref"] is None
    _land(mid, first["plan_id"], "stopped")
    assert _code(lambda: _claim(mid, "o1.documents", "approval", aid)) == "already_ran"


def test_a_mission_with_another_launch_in_flight_still_reworks():
    """Third independent review, finding 1: a claim of another step moves the mission to
    `dispatching` while the review's session is being stopped; the round must still be taken, or
    the stop's fallback would settle the review `stopped` and the mission would stall silently."""
    mid, doc = _at_review()
    con = missions._ready(None)
    try:
        con.execute("UPDATE missions SET state='dispatching' WHERE id=?", (mid,))
        con.commit()
    finally:
        con.close()
    assert _consume(mid, doc, 1)["outcome"] == "reopened"


@pytest.mark.anyio
async def test_a_newer_request_in_the_same_episode_is_not_the_candidate_that_was_read(monkeypatch):
    """Third independent review, finding 5: the fence's re-check compares the observation, so a
    candidate read before a newer not-met result landed stops nothing (the next pass takes the
    newer one)."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_step_runs SET session_key=? WHERE mission_id=? AND step_id=?",
            (KEY, mid, SRC),
        )
        con.commit()
    finally:
        con.close()
    _review_asked_for_changes(mid, doc)
    (cand,) = mission_rework.candidates_sync(mid)
    missions.observe_objective(
        mid,
        _when(doc),
        observed=True,
        value=False,
        detail="changes were requested again",
        extra={"state": "REQUEST_CHANGES", "reviews": "r-newer", "sha": "head1"},
    )
    assert mission_rework.candidates_sync(mid)[0]["observation_id"] != cand["observation_id"]
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(mission_rework, "_idle", lambda key: True)
    assert await mission_rework._settle_one(mid, cand) is None
    assert fence.stopped == 0, "a candidate the review had moved past stopped its session"
    assert missions.step_run(mid, SRC, 1)["state"] == "running"


@pytest.mark.anyio
async def test_a_review_still_to_come_is_not_a_request_for_changes():
    """Fourth independent review (HIGH): `forge_review` reads "no review at the current head yet"
    as observed and not met, and the reviewer can end a turn (approving, or waiting for CI) before
    the next probe. Only an observation that SAYS changes were requested consumes a round."""
    mid, doc = _at_review()
    missions.observe_objective(
        mid, _when(doc), observed=True, value=False, detail="no review at the current head yet"
    )
    assert mission_rework.candidates_sync(mid) == []
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []
    assert _states(mid, doc)[TO] == "done"
    assert missions.step_run(mid, SRC, 1)["state"] == "running"
    missions.observe_objective(
        mid,
        _when(doc),
        observed=True,
        value=False,
        detail="changes were requested",
        extra={"state": "REQUEST_CHANGES"},  # a request that names no review: not a fact
    )
    assert await mission_rework.sweep(mid) == 0
    _review_asked_for_changes(mid, doc)
    assert await mission_rework.sweep(mid) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["detached", "elsewhere", "member"])
async def test_a_session_that_is_not_this_steps_to_stop_consumes_nothing(monkeypatch, outcome):
    """Fourth independent review (MEDIUM): the real fence settles `detached` / `elsewhere` /
    `member` without asking the re-check and without a stop; nothing is consumed then."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_step_runs SET session_key=? WHERE mission_id=? AND step_id=?",
            (KEY, mid, SRC),
        )
        con.commit()
    finally:
        con.close()
    _review_asked_for_changes(mid, doc)

    async def fence(mission_id, key, settle, *, idle=None):
        settle(outcome)
        return outcome

    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []
    assert missions.step_run(mid, SRC, 1)["state"] == "running"


def test_the_bound_counts_only_the_edges_whose_span_holds_the_step():
    """1 + the rounds of each pinned edge whose span holds the step: cv's edge spans cv and every
    step after it (#2033), so cover_letter (before cv, not after it) gets its first run only."""
    import json

    mid, _doc = _at_review()
    pins = {
        "steps": [{"id": TO}, {"id": "o1.cover_letter"}, {"id": "o1.record"}],
        "rework": _rework_pins(mid),
    }
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) "
            "VALUES ('apr-two', ?, 1, 'd', ?, 'o', 'r', 1)",
            (mid, json.dumps(pins)),
        )
        doc = missions.plan_revision(mid)["document"]
        assert missions._approved_runs_tx(con, mid, "apr-two", TO, doc) == 4
        assert missions._approved_runs_tx(con, mid, "apr-two", "o1.record", doc) == 4
        assert missions._approved_runs_tx(con, mid, "apr-two", "o1.cover_letter", doc) == 1
    finally:
        con.close()


def test_a_failed_episode_spends_an_instance_of_the_approved_bound():
    """Hermes 8181/8183/8185: a `failed` episode may already have created a session or attempted
    its effects, so it counts against the approval's 1 + N like every run but a refused one."""
    mid, doc = _at_review(doc=_doc_rounds(1))
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    assert _bound(mid, aid, TO) == 2  # 1 + the plan's one round, not fail-closed
    _run(mid, TO, 2, "failed")  # the operator's own episode, which failed
    _consume(mid, doc, 1)
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "bound_reached"


# ---- fifth independent review of #2032 (at f3662a17) --------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "verdict",
    [
        # Unsure: the judge's probability of MET is in between (confidence is P(met)).
        {
            "met": False,
            "confidence": 0.5,
            "threshold": 0.9,
            "evidence": [{"source": "transcript:review", "quote": "q"}],
        },
        {"met": False, "confidence": 0.05, "threshold": 0.9, "evidence": []},
    ],
)
async def test_an_unsure_or_unquoted_not_met_verdict_is_a_review_still_to_come(
    monkeypatch, verdict
):
    """HIGH: the judge answers "not met, low confidence" when the sources don't show the review
    yet; that, or a verdict that quotes nothing, consumes no round."""
    from agent_sessions import mission_judge

    mid, doc = _at_review()
    monkeypatch.setattr(mission_judge, "gather", lambda mid_, cwd: object())
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "fp")
    judged = {**verdict, "fingerprint": "fp"}
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": judged})
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == []


@pytest.mark.anyio
async def test_an_outstanding_request_re_read_in_the_next_episode_consumes_no_second_round():
    """MEDIUM: a probe's request is identified by the FACT (which reviews, at which head), not by
    the read's time or the row's incarnation, so one outstanding request consumes one round; a new
    request in the next episode consumes the next."""
    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc, review="77")
    assert await mission_rework.sweep(mid) == 1
    _set_rows(mid, doc, (TO, "o1.documents"), "met")
    _run(mid, SRC, 2, created_at=time.time() - 30)
    _review_asked_for_changes(mid, doc, review="77")  # the same request, read again
    assert await mission_rework.sweep(mid) == 0
    assert len(missions.reworks(mid)) == 1
    assert missions.step_run(mid, SRC, 2)["state"] == "running"
    _review_asked_for_changes(mid, doc, review="77,91")  # a new request in episode 2
    assert await mission_rework.sweep(mid) == 1
    assert [r["round"] for r in missions.reworks(mid)] == [1, 2]


@pytest.mark.anyio
async def test_a_question_owed_while_the_mission_is_in_review_is_asked_once_it_runs_again(
    monkeypatch,
):
    """LOW: `review` can go back to running, so an owed question is not retired there."""
    from agent_sessions import mission_fence

    mid, doc = _at_review()
    _exhaust(mid, doc)
    real = mission_fence.fenced_write

    def state(to: str) -> None:
        con = missions._ready(None)
        try:
            con.execute("UPDATE missions SET state=? WHERE id=?", (to, mid))
            con.commit()
        finally:
            con.close()

    async def in_review(mission_id, fn, **kw):
        state("review")
        raise missions.MissionError("mission is review, so there is nothing to ask about", 409)

    monkeypatch.setattr(mission_fence, "fenced_write", in_review)
    assert await mission_rework.ask_owed(mid) == 0
    assert missions.reworks(mid)[-1]["question_at"] is None, "retired while only in review"
    state("running")
    monkeypatch.setattr(mission_fence, "fenced_write", real)
    assert await mission_rework.ask_owed(mid) == 1


def test_a_round_is_not_consumed_for_a_run_on_a_superseded_revision():
    """The store's own revision check (not only the pass's): a current request on a run of an
    older plan revision consumes nothing."""
    from mission_team_fixture import store_plan

    mid, doc = _at_review()
    _review_asked_for_changes(mid, doc)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    oid = missions.rework_observation(row, float(missions.step_run(mid, SRC, 1)["created_at"]))
    assert oid is not None
    store_plan(mid, doc)  # revision 2; the review's run is on revision 1
    got = missions.consume_rework(mid, edge_from=SRC, source_episode=1, observation_id=oid)
    assert got is None and missions.reworks(mid) == []


@pytest.mark.anyio
async def test_a_question_answered_while_its_row_is_raised_is_retracted_in_the_same_pass(
    monkeypatch,
):
    from agent_sessions import notifications

    mid, doc = _at_review()
    _exhaust(mid, doc)
    await mission_rework.ask_owed(mid)
    real = notifications.add

    def add_then_answer(**kw):
        rec = real(**kw)
        q = missions.open_question_row(mid)
        missions.answer_question(mid, q["seq"], option_index=0)
        return rec

    monkeypatch.setattr(notifications, "add", add_then_answer)
    assert mission_rework.notify_sync(mid) == 1
    key = mission_rework.notice_key(mid, SRC, 4)
    assert not [
        r for r in notifications.listing()["notifications"] if r.get("action_id") == key
    ], "a row raised for an answered question stayed until a later pass"
    assert missions.reworks(mid)[-1]["notice_closed_at"] is not None


def test_what_a_when_row_must_say_to_count_as_a_request_for_changes():
    """`missions.rework_observation`, case by case: only an explicit, sure, named request counts."""
    at = time.time()

    def obs(**o) -> dict:
        return {
            "state": "pending",
            "incarnation": "inc",
            "observed": {"at": at, "detail": "d", "value": False, **o},
        }

    quoted = {
        "threshold": 0.9,
        "evidence": [{"source": "transcript:review", "quote": "q"}],
        "fingerprint": "f",
    }
    sure = {**quoted, "confidence": 0.05}  # confidence is the judge's P(met): sure it is NOT
    # A met verdict the operator rejected is stored not-met above the floor: still no request.
    rejected = {"met": True, **quoted, "confidence": 0.95}
    assert missions.rework_observation(obs(judged=rejected), at - 1) is None
    # …and a "met" that contradicts its own low P(met) is no answer either.
    contradiction = {"met": True, **quoted, "confidence": 0.05}
    assert missions.rework_observation(obs(judged=contradiction), at - 1) is None
    assert missions.rework_observation(obs(judged={"met": False, **sure}), at - 1) is not None
    unsure = {"met": False, **quoted, "confidence": 0.5}
    assert missions.rework_observation(obs(judged=unsure), at - 1) is None
    # The old, inverted reading: a not-met with P(met) 0.95 is a contradiction, never a request.
    inverted = {"met": False, **quoted, "confidence": 0.95}
    assert missions.rework_observation(obs(judged=inverted), at - 1) is None
    unquoted = {"met": False, **sure, "evidence": []}
    assert missions.rework_observation(obs(judged=unquoted), at - 1) is None
    # A probe: the request must be stated AND named.
    assert missions.rework_observation(obs(reviews="5", sha="h"), at - 1) is None
    assert missions.rework_observation(obs(state="REQUEST_CHANGES", sha="h"), at - 1) is None
    named = obs(state="REQUEST_CHANGES", reviews="5", sha="h")
    assert missions.rework_observation(named, at - 1) is not None
    assert missions.rework_observation(named, at + 1) is None, "from before the episode"


@pytest.mark.anyio
async def test_a_not_met_verdict_quoting_only_context_asks_for_nothing(monkeypatch):
    """Sixth independent review: a not-met verdict must quote evidence that COUNTS for the agent,
    the same bar as a met verdict; a context-only screen quote un-meets nothing."""
    import types

    from agent_sessions import mission_judge

    mid, doc = _at_review()
    inp = types.SimpleNamespace(screen_counts=set())
    monkeypatch.setattr(mission_judge, "gather", lambda mid_, cwd: inp)
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda i, row: "fp")
    judged = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "evidence": [{"source": "screen:claude:abc", "quote": "the brief is missing"}],
        "fingerprint": "fp",
    }
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": judged})
    assert await mission_rework.sweep(mid) == 0
    judged["evidence"] = [{"source": "transcript:review", "quote": "the brief is missing"}]
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": judged})
    assert await mission_rework.sweep(mid) == 1


def test_a_claim_consumes_the_oldest_open_reopen_first():
    mid, _doc = _at_review()
    con = missions._ready(None)
    try:
        for rnd in (1, 2):
            con.execute(
                "INSERT INTO mission_reworks (mission_id, edge_from, edge_to, round, max_rounds, "
                "source_episode, observation_id, revision, outcome, granted_json, created_at) "
                "VALUES (?, ?, ?, ?, 3, ?, ?, 1, 'reopened', ?, ?)",
                (mid, SRC, TO, rnd, rnd, f"obs:{rnd}", f'["{TO}"]', time.time()),
            )
        con.commit()
        assert missions._open_reopen_tx(con, mid, {"steps": []}, TO) == f"{SRC}#1"
    finally:
        con.close()


def test_a_judged_requests_identity_is_bound_to_the_rows_incarnation():
    """The same output judged on a re-added row (a new incarnation) is a new observation."""
    at = time.time()
    judged = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "evidence": [{"source": "transcript:review", "quote": "q"}],
        "fingerprint": "f",
    }

    def row(inc: str) -> dict:
        return {
            "state": "pending",
            "incarnation": inc,
            "observed": {"at": at, "detail": "d", "value": False, "judged": judged},
        }

    a = missions.rework_observation(row("inc-a"), at - 1)
    b = missions.rework_observation(row("inc-b"), at - 1)
    assert a and b and a != b


def test_a_fresh_read_of_the_old_output_cannot_complete_a_reopened_step_before_its_new_run():
    """Hermes 8191/8193: a reopen resets the target's evidence, but a FRESH probe or judgment of
    the unchanged old output would settle it again and skip the new session. Until a run of the
    step consumes the reopen, such a read is recorded and does not settle; after it, it does."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    _consume(mid, doc, 1)
    key = _step(doc, TO)["evidence"][0]
    got = missions.observe_objective(mid, key, observed=True, value=True, detail="branch is there")
    assert got["state"] == "pending", "the old episode's output completed the reopened step"
    assert got["observed"]["awaits_rework_run"] == TO and got["observed"]["value"] is False
    assert _states(mid, doc)[TO] == "ready"
    # HQ's claim consumes the reopen, but a CLAIMED launch has not started: still fenced.
    run2 = _claim(mid, TO, "approval", aid)
    assert run2["run"]["rework_ref"] == f"{SRC}#1"
    got = missions.observe_objective(mid, key, observed=True, value=True, detail="branch is there")
    assert got["state"] == "pending", "a launch still being claimed lifted the fence"
    # The new episode is running: now the same read counts.
    _set_run_state(run2["plan_id"], "running")
    got = missions.observe_objective(mid, key, observed=True, value=True, detail="branch is there")
    assert got["state"] == "met"
    # A step the reopen did not grant a new run (cover_letter is outside the span) is unfenced.
    letter = _step(doc, "o1.cover_letter")["evidence"][0]
    _set_rows(mid, doc, ("o1.cover_letter",), "pending")
    got = missions.observe_objective(mid, letter, observed=True, value=True, detail="written")
    assert got["state"] == "met"


def test_a_real_probe_between_the_reset_and_the_launch_leaves_the_target_ready(monkeypatch):
    """The producer path end to end: `mission_probes.run_for_mission` reading the unchanged world
    after the reset (an unchanged branch: "met") settles nothing for the reopened step, and the
    board still offers it for its new episode."""
    from agent_sessions import forge, mission_probes

    mid, doc = _at_review()
    _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    key = _step(doc, TO)["evidence"][0]
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
    _consume(mid, doc, 1)
    asked: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: asked.append(o["key"]) or forge.Fact.seen(True, "on branch work"),
    )
    mission_probes.run_for_mission(mid)
    assert key in asked, "the probe never read the reopened step's row"
    row = next(o for o in missions.objectives(mid) if o["key"] == key)
    assert row["state"] == "pending" and row["observed"]["awaits_rework_run"] == TO
    view = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == TO)
    assert view["derived"] == "ready" and not view["runner_done"]


def _set_run_state(plan_id: str, state: str) -> None:
    con = missions._ready(None)
    try:
        con.execute("UPDATE mission_step_runs SET state=? WHERE plan_id=?", (state, plan_id))
        con.commit()
    finally:
        con.close()


def test_a_failed_relaunch_keeps_the_fence_until_a_new_episode_really_starts():
    """Ninth independent review (F1): a reopen consumed by a launch that FAILED started no new
    episode, so the old output still cannot complete the step; the operator's later Start, once
    running, lifts the fence (its run names no reopen: the failed one consumed it)."""
    mid, doc = _at_review()
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC))
    _consume(mid, doc, 1)
    key = _step(doc, TO)["evidence"][0]
    run2 = _claim(mid, TO, "approval", aid)
    _land(mid, run2["plan_id"], "failed")
    got = missions.observe_objective(mid, key, observed=True, value=True, detail="still there")
    assert got["state"] == "pending", "a failed relaunch let the old output complete the step"
    assert _states(mid, doc)[TO] == "ready"
    run3 = _claim(mid, TO, "operator", None)
    assert run3["run"]["rework_ref"] is None
    _set_run_state(run3["plan_id"], "running")
    got = missions.observe_objective(mid, key, observed=True, value=True, detail="still there")
    assert got["state"] == "met"


@pytest.mark.anyio
async def test_an_exhausted_round_whose_edge_left_the_plan_is_retired(monkeypatch):
    """Ninth independent review (L3): a later revision without the rework edge (another procedure)
    can never ask the round's question; it is retired, and the board does not say the operator was
    asked."""
    import copy

    mid, doc = _at_review()
    _exhaust(mid, doc)
    bare = copy.deepcopy(doc)
    for st in bare["steps"]:
        st.pop("rework", None)
    latest = missions.plan_revision(mid)
    monkeypatch.setattr(
        missions, "plan_revision", lambda m, **k: {**latest, "revision": 2, "document": bare}
    )
    assert await mission_rework.ask_owed(mid) == 0
    rec = missions.reworks(mid)[-1]
    assert rec["question_at"] is not None and rec["question_seq"] is None
    assert mission_rework.view(mid, bare)[SRC]["text"].endswith("nothing to ask")


def test_pins_that_cannot_be_read_bound_a_step_to_its_first_run():
    """Ninth independent review (L4): an approval whose pins can't say how many runs it covers
    fails CLOSED, so a rework reopen is no licence for a launch nobody tapped for."""
    mid, doc = _at_review()
    aid = "apr-unreadablepins01"
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) VALUES (?, ?, 1, 'd', 'not json', 'o', "
            "'r', 1)",
            (aid, mid),
        )
        con.commit()
    finally:
        con.close()
    _consume(mid, doc, 1)
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "bound_reached"


# ---- #2033: the approved span is the reset span, one definition -------------------------------


def test_the_reopen_grant_reads_the_one_span_definition(monkeypatch):
    """The grant is every step of `missions.rework_reset_steps` that had run: change that one
    definition and the grant follows it (documents ran, but is outside the span asked here)."""
    mid, doc = _at_review()
    _run(mid, "o1.documents", 1, "done")
    monkeypatch.setattr(missions, "rework_reset_steps", lambda d, f, t: [t, f])
    rec = _consume(mid, doc, 1)
    assert missions.rework_granted(rec) == [TO, SRC]


def test_the_grant_and_the_claim_bound_cover_the_same_steps():
    """Every step a reopen grants is one the approval's bound gives a further run, and the
    claim admits it: pins, grant and bound agree on the steps HQ may re-run."""
    from agent_sessions import mission_approval

    mid, doc = _parallel_mission()
    agents = tuple(s["id"] for s in doc["steps"] if s["actor"] == "agent")
    aid = _pin(mid, agents)
    pins = {"steps": [{"id": s} for s in agents], "rework": _rework_pins(mid)}
    for sid in (TO, "o1.cover_letter", "o1.documents"):
        _run(mid, sid, 1, "done")
    _run(mid, SRC, 1)
    _running(mid)
    granted = missions.rework_granted(_consume(mid, doc, 1))
    bounds = mission_approval._session_bounds(pins)
    ran = {TO, "o1.cover_letter", "o1.documents", SRC}
    assert granted == [s for s in mission_approval.rework_pins(doc)[0]["steps"] if s in ran]
    assert all(bounds[s] == 4 for s in granted)
    con = missions._ready(None)
    try:
        for s in granted:
            assert missions._approved_runs_tx(con, mid, aid, s, doc) == bounds[s]
    finally:
        con.close()


def test_an_approval_pinned_under_the_old_span_bounds_the_claim_to_the_first_run():
    """Defence in depth behind the admission's re-check: the claim's own bound reads the pins
    through `covered_runs`, which refuses a span the definition does not give. An approval given
    before #2033 (cv through review) therefore covers no rework run at the claim, so a reopen is
    no licence for a launch nobody tapped for; the operator's own Start still runs it."""
    mid, doc = _at_review()
    old = [{"from": SRC, "to": TO, "max_rounds": 3, "steps": [TO, "o1.documents", SRC]}]
    aid = _pin(mid, (TO, "o1.cover_letter", "o1.documents", SRC), rework=old)
    _consume(mid, doc, 1)
    assert _code(lambda: _claim(mid, TO, "approval", aid)) == "bound_reached"
    assert _claim(mid, TO, "operator", None)["run"]["rework_ref"] == f"{SRC}#1"


def test_a_widened_span_leaves_holds_to_the_operator():
    """Hermes on #2033: widening the span widens nothing else. A held step of the span (a parallel
    branch HQ could otherwise start) is still asked about before HQ starts it; the operator's own
    Start runs it."""
    mid, doc = _parallel_mission()
    agents = tuple(s["id"] for s in doc["steps"] if s["actor"] == "agent")
    aid = _pin(mid, agents)
    for sid in ("o1.posting", TO, "o1.cover_letter", "o1.documents"):
        _run(mid, sid, 1, "done")
    _run(mid, SRC, 1)
    _running(mid)
    rec = _consume(mid, doc, 1)
    assert "o1.cover_letter" in missions.rework_granted(rec)
    missions.set_step_hold(mid, "o1.cover_letter", hold=True)
    c = _claim(mid, TO, "approval", aid)
    _land(mid, c["plan_id"], "done")
    _set_rows(mid, doc, (TO,), "met")
    assert _code(lambda: _claim(mid, "o1.cover_letter", "approval", aid)) == "held"
    assert _claim(mid, "o1.cover_letter", "operator", None)["run"]["rework_ref"] == f"{SRC}#1"
