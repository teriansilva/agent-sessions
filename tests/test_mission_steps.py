# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""One API session per agent step through ONE claim-and-launch seam (#1581 S1, #1613 D15).

The store's claim (`missions.claim_step`) with every refusal, the step branch of the dispatch
settlement and of recovery, and the seam (`mission_steps.start_step`) on the real path:
`mission_dispatch.run` → `mission_structured.dispatch` → the structured facade → the real native
worker over the fake native child (`host`), with every store under `tmp_path`. The plan is
#1950's synthetic application mission (`mission_team_fixture`).
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import threading

import pytest

from agent_sessions import (
    mission_dispatch,
    mission_fence,
    mission_plan_document,
    mission_steps,
    mission_structured,
    mission_team,
    missions,
    prefs,
    projects,
    scopedspawn,
)
from agent_sessions import specialists_store as sstore
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from mission_team_fixture import GRADES, document, store_plan
from mission_team_fixture import context as team_context
from mission_team_fixture import spec as team_spec
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    KINDS,
    frames,
    host,
    ident,
    project,
)

ENGINE = ENGINES["claude"]
SPEC = ("cv-writer", 1)
DEAD_OWNER = "1:not-a-start-time"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _assignment(sid: str = SPEC[0], version: int = SPEC[1], **over) -> dict:
    a = {
        "specialist": {"id": sid, "version": version},
        "override": None,
        "reason": "test",
        "alternates": [],
        "notes": [],
    }
    a.update(over)
    return a


def _doc(assign: dict | None = None, *, objectives: int = 1) -> dict:
    """The application flow with the agent steps assigned (default: every one to `SPEC`)."""
    doc = document(objectives=objectives)
    for s in doc["steps"]:
        if s["actor"] == "agent":
            a = (assign or {}).get(s["id"], _assignment())
            s["envelope"]["assignment"] = copy.deepcopy(a)
    return mission_plan_document.validate_document(doc)


def _evidence(mid: str, doc: dict, *, met: tuple[str, ...] = ()) -> None:
    """One gating row per evidence key, pending unless its STEP is in `met` (then waived)."""
    con = missions._ready(None)
    try:
        con.execute("BEGIN IMMEDIATE")
        ord_ = 0
        for s in doc["steps"]:
            for k in s["evidence"]:
                ord_ += 1
                con.execute(
                    "INSERT INTO mission_objectives (mission_id, key, ord, title, probe, gate, "
                    "state, source) VALUES (?,?,?,?,'none',1,?,'playbook')",
                    (mid, k, ord_, k, "waived" if s["id"] in met else "pending"),
                )
        con.execute("COMMIT")
    finally:
        con.close()


def _settle_evidence(mid: str, doc: dict, step_id: str, state: str = "waived") -> None:
    step = next(s for s in doc["steps"] if s["id"] == step_id)
    con = missions._ready(None)
    try:
        for k in step["evidence"]:
            con.execute(
                "UPDATE mission_objectives SET state=? WHERE mission_id=? AND key=?",
                (state, mid, k),
            )
        con.commit()
    finally:
        con.close()


def _mission(cwd: str = "/tmp", *, doc: dict | None = None, met=("o1.intake",), state="planned"):
    mid = missions.create_mission("apply for the job", title="Apply")["id"]
    doc = doc or _doc()
    store_plan(mid, doc)
    _evidence(mid, doc, met=met)
    if state != "draft":
        missions.set_state(mid, "draft", "planned")
    return mid, doc


def _claim(mid: str, step: str = "o1.posting", *, cwd: str = "/tmp/a", revision: int = 1, **kw):
    return missions.claim_step(
        mid,
        step,
        revision=revision,
        engine=ENGINE,
        model=None,
        effort=None,
        specialist=SPEC,
        project_id="p-synthetic01",
        cwd=cwd,
        brief="do the step",
        **kw,
    )


def _runs(mid: str) -> list[dict]:
    return missions.step_runs(mid)


def _code(e) -> str:
    return e.value.code


@pytest.fixture
def quiet(monkeypatch):
    """Every step session is idle and its fenced stop is proved (the store-level tests' sessions
    are keys only; the real fence is exercised on the real path below)."""
    return _fence(monkeypatch, idle=True, outcome="stopped")


def _fence(monkeypatch, *, idle: bool | None, outcome: str) -> list:
    """A stand-in for the adoption fence that answers `outcome` (and asks `idle` like the real one,
    on a worker thread). ``idle=None`` keeps the real idleness check."""
    seen = []

    def run(key, settle, idle_fn):
        got = outcome if idle_fn is None or idle_fn() else "busy"
        if got != "busy":
            seen.append(key)
        settle(got)
        return got

    async def fenced(mission_id, key, settle, *, idle=None):
        return await asyncio.to_thread(run, key, settle, idle)

    if idle is not None:
        monkeypatch.setattr(mission_steps, "_idle", lambda key: idle)
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fenced)
    return seen


# ---- the claim ----------------------------------------------------------------------------------


def test_the_claim_writes_the_run_the_dispatch_and_the_state_together():
    mid, _doc_ = _mission()
    plan = _claim(mid)
    run = plan["run"]
    assert run["state"] == "claimed" and run["episode"] == 1 and run["revision"] == 1
    assert run["plan_id"] == plan["plan_id"] and plan["plan_id"].startswith("stp_")
    assert run["prior_state"] == "planned" and run["authority"] == "operator"
    d = missions.get_dispatch(mid)
    assert d["plan_id"] == plan["plan_id"] and d["spawn_parent"] is None
    m = missions.get_mission(mid)
    assert m["state"] == "dispatching" and m["cwd"] == "/tmp/a"
    assert any((e.get("meta") or {}).get("step_run") == "claimed" for e in m["events"])
    assert plan["specialist"] == SPEC and plan["engine"] == ENGINE


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ("o1.cv", "not_ready"),  # waits for posting
        ("o1.intake", "not_agent"),  # the operator's step
        ("o1.nope", "unknown_step"),
    ],
)
def test_a_step_that_is_not_a_ready_agent_step_is_refused_and_nothing_is_written(step, code):
    mid, _ = _mission()
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, step)
    assert _code(e) == code
    assert _runs(mid) == [] and missions.get_dispatch(mid) is None
    assert missions.get_mission(mid)["state"] == "planned"


def test_an_unassigned_step_is_refused():
    doc = document()  # no team
    mid, _ = _mission(doc=doc)
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid)
    assert _code(e) == "unassigned"


def test_a_plan_edit_between_the_read_and_the_claim_starts_nothing():
    mid, doc = _mission()
    store_plan(mid, doc)  # revision 2
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, revision=1)
    assert _code(e) == "revision_superseded" and e.value.extra["latest_revision"] == 2
    assert _runs(mid) == []


def test_a_closed_mission_and_a_launch_in_flight_are_refused():
    mid, _ = _mission()
    _claim(mid)
    with pytest.raises(missions.StepRefused) as e:  # dispatching: one launch in flight
        _claim(mid, "o1.posting")
    assert _code(e) == "not_launchable"
    mid2, _ = _mission(state="draft")
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid2)
    assert _code(e) == "not_launchable"


def _adopt(mid: str, plan: dict, key: str = "claude-api:11111111-1111-4111-8111-111111111111"):
    assert missions.note_dispatch_session(mid, key, expect_plan=plan["plan_id"])
    out = missions.settle_dispatch(
        mid, to="running", detail="ok", session_key=key, expect_plan=plan["plan_id"]
    )
    missions.clear_dispatch(mid, expect_plan=plan["plan_id"])
    return out


def test_an_adopted_step_session_runs_as_the_steps_and_the_mission_runs():
    mid, _ = _mission()
    plan = _claim(mid)
    key = "claude-api:11111111-1111-4111-8111-111111111111"
    assert _adopt(mid, plan, key)["adopted"]
    run = _runs(mid)[0]
    assert run["state"] == "running" and run["session_key"] == key and run["ended_at"] is None
    m = missions.get_mission(mid)
    assert m["state"] == "running"
    roster = {s["session_key"]: s for s in m["sessions"]}
    assert roster[key]["role"] == "step" and roster[key]["spawned_by"] == "step:o1.posting#1"
    assert missions.step_run_of_session(key)["step_id"] == "o1.posting"
    assert mission_steps.run_of_session(key) == {
        "mission_id": mid,
        "step_id": "o1.posting",
        "episode": 1,
        "state": "running",
    }


@pytest.mark.parametrize("prior", ["planned", "running"])
def test_a_refused_launch_returns_the_mission_to_where_it_was_and_restores_no_proposal(
    prior, quiet
):
    mid, doc = _mission()
    if prior == "running":  # a first step is under way
        _adopt(mid, _claim(mid))
        _settle_evidence(mid, doc, "o1.posting")
        asyncio.run(mission_steps.reconcile(mid))
    step = "o1.posting" if prior == "planned" else "o1.cv"
    plan = _claim(mid, step, cwd="/tmp/b")
    out = missions.settle_dispatch(
        mid, to="planned", detail="the client refused", expect_plan=plan["plan_id"]
    )
    assert out["settled"] and out["state"] == prior
    assert missions.get_mission(mid)["state"] == prior
    assert missions.get_plan(mid) is None, "a step launch never restores a dispatch proposal"
    run = missions.step_run(mid, step, 1)
    assert run["state"] == "refused" and run["ended_at"] is not None
    assert missions.get_dispatch(mid) is None


def test_a_failed_launch_that_kept_its_record_is_unknown_until_proved_stopped():
    mid, _ = _mission()
    plan = _claim(mid)
    key = "claude-api:22222222-2222-4222-8222-222222222222"
    missions.note_dispatch_session(mid, key, expect_plan=plan["plan_id"])
    missions.settle_dispatch(
        mid, to="failed", detail="never ready", keep_record=True, expect_plan=plan["plan_id"]
    )
    assert missions.get_mission(mid)["state"] == "planned"
    run = _runs(mid)[0]
    assert run["state"] == "unknown" and run["session_key"] == key
    with pytest.raises(missions.StepRefused) as e:  # the record holds the slot: no relaunch
        _claim(mid)
    assert _code(e) == "launch_in_flight"
    assert missions.clear_dispatch(mid, expect_plan=plan["plan_id"], stopped=True)
    assert _runs(mid)[0]["state"] == "failed"
    nxt = _claim(mid)  # a new EPISODE, never the old one replayed
    assert nxt["run"]["episode"] == 2 and nxt["plan_id"] != plan["plan_id"]


def test_a_failed_launch_with_nothing_left_is_failed():
    mid, _ = _mission()
    plan = _claim(mid)
    missions.settle_dispatch(mid, to="failed", detail="boom", expect_plan=plan["plan_id"])
    assert _runs(mid)[0]["state"] == "failed"
    assert missions.get_mission(mid)["state"] == "planned"


def test_a_mission_abandoned_during_the_launch_leaves_the_run_unknown_or_over():
    mid, _ = _mission()
    plan = _claim(mid)
    missions.set_state(mid, "dispatching", "abandoned")
    key = "claude-api:33333333-3333-4333-8333-333333333333"
    out = missions.settle_dispatch(
        mid, to="running", detail="", session_key=key, expect_plan=plan["plan_id"]
    )
    assert not out["settled"] and out["retained"]
    assert _runs(mid)[0]["state"] == "unknown"


def test_a_launch_that_never_settled_is_answered_when_its_record_goes():
    """A crash after the mission was moved away mid-launch: the next pass clears the keyless
    record, and its run is `refused`, never a `claimed` that holds a slot and a folder for ever."""
    from agent_sessions import mission_dispatch_recover

    mid, _ = _mission()
    _claim(mid, owner=DEAD_OWNER)
    missions.set_state(mid, "dispatching", "planned")
    asyncio.run(mission_dispatch_recover.recover_once())
    assert missions.get_dispatch(mid) is None
    assert _runs(mid)[0]["state"] == "refused"
    assert _claim(mid)["run"]["episode"] == 2


def test_an_open_run_and_the_concurrency_cap_refuse(monkeypatch):
    doc = _doc(objectives=2)
    mid, _ = _mission(doc=doc, met=("o1.intake", "o2.intake"))
    _adopt(mid, _claim(mid, "o1.posting", cwd="/tmp/a"))
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o1.posting", cwd="/tmp/b")
    assert _code(e) == "open_run"
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o2.posting", cwd="/tmp/b", cap=1)
    assert _code(e) == "cap" and e.value.extra == {"open": 1, "cap": 1}
    _adopt(mid, _claim(mid, "o2.posting", cwd="/tmp/b", cap=2), "claude-api:" + ident())
    assert sum(r["state"] == "running" for r in _runs(mid)) == 2


def test_the_cap_is_never_above_the_hard_maximum():
    assert missions.STEP_CONCURRENCY_MAX == 8
    assert 1 <= missions.STEP_CONCURRENCY <= missions.STEP_CONCURRENCY_MAX


def test_two_open_runs_never_share_a_folder_in_any_mission():
    a, _ = _mission()
    b, _ = _mission()
    _adopt(a, _claim(a, cwd="/tmp/shared"))
    with pytest.raises(missions.StepRefused) as e:
        _claim(b, cwd="/tmp/shared")
    assert _code(e) == "shared_cwd" and e.value.extra["holder"]["mission_id"] == a
    # Closing a mission stops nothing: its step's open run still holds the folder (independent
    # review on #2001). Only a proved stop or the step's own settlement gives it back.
    missions.set_state(a, "running", "abandoned")
    with pytest.raises(missions.StepRefused) as e:
        _claim(b, cwd="/tmp/shared")
    assert _code(e) == "shared_cwd"


def test_two_concurrent_claims_of_one_step_make_one_run():
    mid, _ = _mission()
    barrier = threading.Barrier(2)
    results: list = []

    def go():
        barrier.wait()
        try:
            results.append(_claim(mid)["plan_id"])
        except missions.StepRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if str(r).startswith("stp_")) == 1, results
    assert len(_runs(mid)) == 1


def test_an_approval_bound_into_the_claim_must_still_be_in_force():
    """Slice B's hook: `approval_id` is compared in the claim's own transaction."""
    mid, _ = _mission()
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, approval_id="apr-00000000000000000000", authority="approval")
    assert _code(e) == "approval_changed" and _runs(mid) == []


def test_a_step_member_is_minted_only_by_its_launch():
    mid, _ = _mission()
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(mid, "claude-api:" + ident(), role="step")
    assert e.value.status == 422


def test_a_rollback_past_v40_waits_for_step_launches_to_settle():
    mid, _ = _mission()
    plan = _claim(mid)
    db = missions._db_path()
    with pytest.raises(missions.MissionError) as e:
        missions.rollback_schema(39, path=db)
    assert e.value.status == 409
    missions.settle_dispatch(mid, to="planned", detail="x", expect_plan=plan["plan_id"])
    assert missions.rollback_schema(39, path=db)["to"] == 39
    missions.reset_schema_cache_for_test()


def test_the_board_names_a_folder_another_mission_holds(monkeypatch):
    monkeypatch.setattr(mission_steps, "resolve_dir", lambda pid: (pid, "/tmp/shared-board"))
    a, _ = _mission()
    b, _ = _mission()
    _claim(a, cwd="/tmp/shared-board")
    posting = next(s for s in mission_steps.view_sync(b)["steps"] if s["id"] == "o1.posting")
    assert not posting["can_start"] and "another mission" in posting["why_not"]


def test_a_consent_bound_into_the_claim_must_still_be_in_force():
    mid, _ = _mission()
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, consent_id="cns-gone", authority="consent")
    assert _code(e) == "consent_changed" and _runs(mid) == []


# ---- the fence check ----------------------------------------------------------------------------


def test_the_launch_verdict_refuses_a_superseded_plan_a_settled_run_and_a_revoked_approval():
    mid, doc = _mission()
    plan = _claim(mid)
    assert missions.step_launch_verdict(plan["plan_id"]) is None
    store_plan(mid, doc)
    assert "plan changed" in missions.step_launch_verdict(plan["plan_id"])
    assert "no longer current" in missions.step_launch_verdict("stp_nope")
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_step_runs SET approval_id='apr-x', revision=2 WHERE plan_id=?",
            (plan["plan_id"],),
        )
        con.commit()
    finally:
        con.close()
    assert "approval" in missions.step_launch_verdict(plan["plan_id"])


# ---- settle, stop, reconcile --------------------------------------------------------------------


def test_reconcile_records_a_done_step_once_and_releases_its_session(quiet):
    mid, doc = _mission()
    key = "claude-api:44444444-4444-4444-8444-444444444444"
    _adopt(mid, _claim(mid), key)
    assert asyncio.run(mission_steps.reconcile(mid)) == 0
    _settle_evidence(mid, doc, "o1.posting")
    assert asyncio.run(mission_steps.reconcile(mid)) == 1
    assert quiet == [key], "the step's session is stopped before its run settles"
    assert asyncio.run(mission_steps.reconcile(mid)) == 0  # once
    run = _runs(mid)[0]
    assert run["state"] == "done" and run["ended_at"] is not None
    held = missions.active_session_keys(mid)
    assert key not in held, "a done step's session leaves the active roster"
    m = missions.get_mission(mid)
    assert any((e.get("meta") or {}).get("step_run") == "done" for e in m["events"])


def test_evidence_that_arrives_mid_turn_holds_the_slot_and_the_folder(monkeypatch):
    """Hermes on #1581: the judge can settle a step while its agent is still in a turn. The run
    stays `running` (slot and folder held) until the session is idle and its stop is proved."""
    mid, doc = _mission()
    _adopt(mid, _claim(mid, cwd="/tmp/held"))
    _settle_evidence(mid, doc, "o1.posting")
    stops = _fence(monkeypatch, idle=None, outcome="stopped")

    async def busy_snapshot(key):
        return {"session_key": key, "active_turn": "turn-1", "native": {}}

    monkeypatch.setattr(runtime, "snapshot", busy_snapshot)
    assert asyncio.run(mission_steps.reconcile(mid)) == 0
    assert stops == [] and _runs(mid)[0]["state"] == "running"
    other, _ = _mission()
    with pytest.raises(missions.StepRefused) as e:
        _claim(other, cwd="/tmp/held")
    assert _code(e) == "shared_cwd"

    async def idle_snapshot(key):
        return {"session_key": key, "active_turn": None, "native": {"background_active": False}}

    monkeypatch.setattr(runtime, "snapshot", idle_snapshot)
    assert asyncio.run(mission_steps.reconcile(mid)) == 1
    assert len(stops) == 1 and _runs(mid)[0]["state"] == "done"
    assert _claim(other, cwd="/tmp/held")["run"]["state"] == "claimed"


def test_an_unreadable_session_is_never_stopped_by_reconcile(monkeypatch):
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    _settle_evidence(mid, doc, "o1.posting")
    stops = _fence(monkeypatch, idle=None, outcome="stopped")

    async def unreadable(key):
        raise runtime.StructuredError(503, "could not read")

    monkeypatch.setattr(runtime, "snapshot", unreadable)
    assert asyncio.run(mission_steps.reconcile(mid)) == 0
    assert stops == [] and _runs(mid)[0]["state"] == "running"


def test_a_stop_that_is_not_proved_keeps_the_run_running(monkeypatch):
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    _settle_evidence(mid, doc, "o1.posting")
    _fence(monkeypatch, idle=True, outcome="leaked")
    assert asyncio.run(mission_steps.reconcile(mid)) == 0
    assert _runs(mid)[0]["state"] == "running"


def test_finish_is_a_compare_and_set():
    mid, _ = _mission()
    _adopt(mid, _claim(mid))
    assert missions.finish_step_run(
        mid, "o1.posting", 1, to="stopped", detail="x", expect="running"
    )
    assert (
        missions.finish_step_run(mid, "o1.posting", 1, to="stopped", detail="x", expect="running")
        is None
    )
    with pytest.raises(missions.MissionError):
        missions.finish_step_run(mid, "o1.posting", 1, to="done", detail="x", expect="unknown")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "want"),
    [
        ("stopped", "stopped"),
        ("leaked", "unknown"),
        ("detached", "failed"),
        ("elsewhere", "failed"),
    ],
)
async def test_stop_settles_what_the_fence_proved(monkeypatch, outcome, want):
    mid, _ = _mission()
    key = "claude-api:55555555-5555-4555-8555-555555555555"
    _adopt(mid, _claim(mid), key)
    seen = _fence(monkeypatch, idle=True, outcome=outcome)
    run = await mission_steps.stop_step(mid, "o1.posting", 1)
    assert seen == [key] and run["state"] == want
    assert (key in missions.active_session_keys(mid)) is (want == "unknown")


@pytest.mark.anyio
async def test_stop_refuses_a_run_still_being_started():
    mid, _ = _mission()
    _claim(mid)
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.stop_step(mid, "o1.posting", 1)
    assert _code(e) == "launch_in_flight"


# ---- schema -------------------------------------------------------------------------------------


def test_the_schema_is_v40_and_rolls_back_to_39():
    mid, _ = _mission()
    plan = _claim(mid)
    missions.settle_dispatch(mid, to="planned", detail="x", expect_plan=plan["plan_id"])
    db = missions._db_path()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION >= 40
    con.close()
    assert missions.rollback_schema(39, path=db) == {"from": missions.SCHEMA_VERSION, "to": 39}
    missions.reset_schema_cache_for_test()
    assert len(_runs(mid)) == 1  # converges, nothing dropped


def test_an_upgraded_store_gets_the_same_step_runs_schema_as_a_fresh_one():
    missions.create_mission("fresh", cwd="/tmp")
    db = missions._db_path()

    def tables():
        con = sqlite3.connect(db)
        try:
            return {
                r[0]: " ".join(r[1].split())
                for r in con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name LIKE 'mission_step_runs%'"
                )
            }
        finally:
            con.close()

    fresh = tables()
    assert set(fresh) == {
        "mission_step_runs",
        "mission_step_runs_open_cwd",
        "mission_step_runs_session",
    }
    con = sqlite3.connect(db)
    con.execute("DROP TABLE mission_step_runs")
    con.execute("PRAGMA user_version=39")  # a v39 store: the later steps are idempotent
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions.create_mission("upgraded", cwd="/tmp")
    assert tables() == fresh


def test_step_runs_leave_with_their_mission():
    mid, _ = _mission()
    _claim(mid)
    missions.set_state(mid, "dispatching", "abandoned")
    assert missions.delete_mission(mid)
    con = missions._ready(None)
    try:
        assert con.execute("SELECT COUNT(*) FROM mission_step_runs").fetchone()[0] == 0
    finally:
        con.close()


# ---- the brief ----------------------------------------------------------------------------------


def test_the_brief_is_server_written_from_the_step_and_its_checklist():
    doc = _doc()
    step = next(s for s in doc["steps"] if s["id"] == "o1.cv")
    text = mission_steps.compose_brief(
        {"title": "Apply", "instruction": "apply for the job"}, doc["objectives"][0], step
    )
    assert "ONE step of the mission “Apply”" in text
    assert "Tailor the CV (o1.cv)" in text and "- The CV is tailored" in text
    assert "apply for the job" in text


# ---- the seam, on the real path -----------------------------------------------------------------


@pytest.fixture
def seam(host, tmp_path, project, monkeypatch):
    """A mission-ready Claude API client over the fake child, one real specialist on it, one
    real project, orchestration on: what `start_step` reads, all under `tmp_path`."""
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    prefs.set_orchestrator({"enabled": True})
    missions.reset_schema_cache_for_test()
    spec = sstore.create(
        {
            "title": "CV writer",
            "summary": "",
            "skills": [],
            "engine": ENGINE,
            "model": "default",
            "effort": None,
            "capacity": 2,
        }
    )
    # The team's context: the real specialist as the pool holds it, on a client that offers
    # sonnet, graded strong (P5's `qualify` runs at every start).
    skills = ["career-archive", "cv-writing", "application-review"]
    team_state = {"ctx": team_context(specs=[team_spec(spec["id"], "sonnet", skills, capacity=2)])}
    monkeypatch.setattr(mission_team, "load_context", lambda doc: team_state["ctx"])
    # The fence's grade re-read: the fake client lists no models, so the specialist runs on the
    # agent's default model, which the operator's map grades here.
    team_state["grade"] = "frontier"
    monkeypatch.setattr(mission_steps, "_grade_now", lambda e, m: team_state["grade"])
    proj = projects.create("repo", folders=[str(project)], default_folder=str(project))
    doc = document()
    doc["objectives"][0]["project_id"] = proj.id
    for s in doc["steps"]:
        if s["actor"] == "agent":
            s["envelope"]["assignment"] = _assignment(spec["id"], 1)
    doc = mission_plan_document.validate_document(doc)
    mid = missions.create_mission("apply for the job", title="Apply")["id"]
    store_plan(mid, doc)
    _evidence(mid, doc, met=("o1.intake",))
    missions.set_state(mid, "draft", "planned")
    yield {
        "mid": mid,
        "doc": doc,
        "spec": spec,
        "cwd": str(project),
        "project": project,
        "team": team_state,
        "skills": skills,
    }
    missions.reset_schema_cache_for_test()


def _users(work) -> list[str]:
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("type") == "user":
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
    return out


@pytest.mark.anyio
async def test_a_step_starts_its_own_api_session_from_its_specialist(seam, monkeypatch):
    monkeypatch.setattr(prefs, "unattended_bypass_granted", lambda: True)  # #1215 grant ON
    calls = []
    real_create = mission_structured.runtime.create_session

    async def create(*a, **kw):
        calls.append(kw)
        return await real_create(*a, **kw)

    monkeypatch.setattr(mission_structured.runtime, "create_session", create)
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert out["outcome"] == "started", out
    run = out["run"]
    assert run["state"] == "running" and run["specialist"] == {
        "id": seam["spec"]["id"],
        "version": 1,
    }
    key = run["session_key"]
    assert (
        key
        == f"{ENGINE}:{mission_structured.ids(missions.step_runs(seam['mid'])[0]['plan_id'])[0]}"
    )
    assert calls[0]["bypass"] is False, "a step session never skips permission prompts"
    assert calls[0]["specialist"] == (seam["spec"]["id"], 1)
    snap = await runtime.snapshot(key)
    assert not snap.get("bypass")
    brief = next(t for t in snap["turns"] if t.get("context"))
    assert brief["context"]["step_id"] == "o1.posting" and brief["context"]["episode"] == 1
    sent = _users(seam["project"])
    assert len(sent) == 1 and "Read the posting (o1.posting)" in sent[0]
    m = missions.get_mission(seam["mid"])
    assert m["state"] == "running"
    assert {s["session_key"]: s["role"] for s in m["sessions"]}[key] == "step"
    # The specialist's slot is held by the session (per generation, #1693 S4b).
    assert sstore.last_reservation([key])["specialist_id"] == seam["spec"]["id"]
    view = mission_steps.view_sync(seam["mid"])
    posting = next(s for s in view["steps"] if s["id"] == "o1.posting")
    assert posting["why_not"] == "already running" and posting["runs"][0]["state"] == "running"
    with pytest.raises(missions.StepRefused) as e:  # the next step waits for its evidence
        await mission_steps.start_step(seam["mid"], "o1.cv", revision=1, expect_cwd=seam["cwd"])
    assert _code(e) == "not_ready"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_plan_edit_after_the_claim_refuses_the_create_under_the_fence(seam, monkeypatch):
    real_create = mission_structured.runtime.create_session

    async def edit_then_create(*a, **kw):
        store_plan(seam["mid"], seam["doc"])  # lands between the claim and the create
        return await real_create(*a, **kw)

    monkeypatch.setattr(mission_structured.runtime, "create_session", edit_then_create)
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert out["outcome"] == "refused", out
    assert out["run"]["state"] == "refused" and "plan changed" in out["run"]["detail"]
    assert missions.get_mission(seam["mid"])["state"] == "planned"
    assert _users(seam["project"]) == []


@pytest.mark.anyio
async def test_a_plan_edit_after_the_create_refuses_the_brief_and_stops_the_session(
    seam, monkeypatch
):
    real_create = mission_structured.runtime.create_session

    async def create_then_edit(*a, **kw):
        snap = await real_create(*a, **kw)
        store_plan(seam["mid"], seam["doc"])  # lands between the create and the brief
        return snap

    monkeypatch.setattr(mission_structured.runtime, "create_session", create_then_edit)
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert out["outcome"] == "failed", out
    assert out["run"]["state"] in ("failed", "unknown")
    assert _users(seam["project"]) == [], "the brief went out after the plan changed"
    key = out["run"]["session_key"]
    assert (await runtime.probe(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_refusing_admission_claims_nothing(seam):
    """Slice B's hook: `admit` runs before the claim; a refusal writes no run."""
    asked = []

    def admit(mid, step, boundary, **kw):
        asked.append((mid, step, boundary))
        return {"ok": False, "reasons": [{"code": "grade_below_min", "text": "x"}], "new": True}

    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(
            seam["mid"],
            "o1.posting",
            revision=1,
            authority=mission_steps.Authority("approval"),
            admit=admit,
        )
    assert (
        _code(e) == "admission_refused" and e.value.extra["reasons"][0]["code"] == "grade_below_min"
    )
    assert asked == [(seam["mid"], "o1.posting", "launch")]
    assert missions.step_runs(seam["mid"]) == []
    with pytest.raises(ValueError):  # an operator launch takes no admission
        await mission_steps.start_step(seam["mid"], "o1.posting", revision=1, admit=admit)


@pytest.mark.anyio
async def test_an_operator_start_runs_the_teams_qualification(seam):
    """Hermes on #1581: a grade drift after the assignment refuses the operator's start too."""
    drifted = copy.deepcopy(GRADES)
    del drifted[ENGINE]["sonnet"]  # the operator un-graded the model after the assignment
    seam["team"]["ctx"] = team_context(
        specs=[team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2)],
        grades=drifted,
    )
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert _code(e) == "not_qualified"
    assert "ungraded" in {r["code"] for r in e.value.extra["reasons"]}
    assert missions.step_runs(seam["mid"]) == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["archived", "grade", "newer_version"])
async def test_a_change_after_the_preflight_refuses_the_create_under_the_fence(
    seam, monkeypatch, change
):
    """The window between the preflight and the effect: what can change outside the store is
    re-read inside the write fence (`fence_verdict`)."""
    authority = mission_steps.OPERATOR
    if change == "newer_version":
        # D-P6-1 applies to an APPROVAL-bound launch: bind one through a harness admission.
        con = missions._ready(None)
        try:
            con.execute(
                "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
                "operation_id, request_digest, created_at) VALUES "
                "('apr-test', ?, 1, 'd', '{}', 'op-1', 'r', 1.0)",
                (seam["mid"],),
            )
            con.commit()
        finally:
            con.close()
        authority = mission_steps.Authority("approval")
    real_create = mission_structured.runtime.create_session

    async def change_then_create(*a, **kw):
        if change == "archived":
            cur = sstore.get(seam["spec"]["id"])
            sstore.set_archived(seam["spec"]["id"], True, expect_revision=cur["revision"])
        elif change == "grade":
            seam["team"]["grade"] = None  # the operator un-graded the model meanwhile
        else:
            cur = sstore.get(seam["spec"]["id"])
            body = {k: v for k, v in cur["current"].items() if k not in ("version", "created_at")}
            sstore.update(seam["spec"]["id"], {**body, "summary": "edited"}, expect_revision=1)
        return await real_create(*a, **kw)

    monkeypatch.setattr(mission_structured.runtime, "create_session", change_then_create)
    kw = {}
    if authority.kind == "approval":
        kw["admit"] = lambda mid, step, boundary, **_: {
            "ok": True,
            "reasons": [],
            "approval_id": "apr-test",
            "new": False,
        }
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, authority=authority, **kw
    )
    assert out["outcome"] == "refused", out
    assert out["run"]["state"] == "refused"
    assert _users(seam["project"]) == []


def _bind_authority(mid: str, *, consent: bool) -> dict:
    """An approval in force (and a standing consent on it), as slice A's store writes them."""
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) VALUES "
            "('apr-bound', ?, 1, 'd', '{}', 'op-apr', 'r', 1.0)",
            (mid,),
        )
        if consent:
            con.execute(
                "INSERT INTO mission_standing_consents (id, mission_id, approval_id, "
                "scope_digest, scope_json, expires_at, operation_id, request_digest, created_at) "
                "VALUES ('cns-bound', ?, 'apr-bound', 'd', '{}', 9e12, 'op-cns', 'r', 1.0)",
                (mid,),
            )
        con.commit()
    finally:
        con.close()
    if consent:
        auth = mission_steps.Authority("consent", "cns-bound")
    else:
        auth = mission_steps.Authority("approval")

    def admit(mid_, step, boundary, **kw):
        return {"ok": True, "reasons": [], "approval_id": "apr-bound", "new": False}

    return {"authority": auth, "admit": admit}


@pytest.mark.anyio
@pytest.mark.parametrize("consent", [False, True])
@pytest.mark.parametrize("at", ["create", "brief"])
async def test_a_revocation_after_the_preflight_refuses_the_effect(seam, monkeypatch, consent, at):
    """Hermes on #1581 (128008): the approval or consent the preflight named is re-read inside
    the write fence at BOTH effects."""
    bound = _bind_authority(seam["mid"], consent=consent)
    real_create = mission_structured.runtime.create_session

    def revoke():
        if consent:
            missions.revoke_consent(seam["mid"], "cns-bound")
        else:
            missions.revoke_approval(seam["mid"], "apr-bound")

    async def create(*a, **kw):
        if at == "create":
            revoke()
            return await real_create(*a, **kw)
        snap = await real_create(*a, **kw)
        revoke()
        return snap

    monkeypatch.setattr(mission_structured.runtime, "create_session", create)
    out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1, **bound)
    run = out["run"]
    assert run["authority"] == ("consent" if consent else "approval")
    assert _users(seam["project"]) == [], "a brief went out after the authority was revoked"
    if at == "create":
        assert out["outcome"] == "refused" and run["state"] == "refused"
    else:
        assert out["outcome"] == "failed" and run["state"] in ("failed", "unknown")
        assert (await runtime.probe(run["session_key"]))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_done_step_is_stopped_and_closed_so_nothing_automated_reaches_it(seam):
    """The real path: evidence met → the idle session is stopped, the run is `done`, and no open
    mission holds the session any more (every automated send asks the holder first)."""
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    assert missions.holder_of(key) == seam["mid"]
    _settle_evidence(seam["mid"], seam["doc"], "o1.posting")
    assert await mission_steps.reconcile(seam["mid"]) == 1
    run = missions.step_runs(seam["mid"])[0]
    assert run["state"] == "done"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert missions.holder_of(key) is None
    assert key not in missions.active_session_keys(seam["mid"])
    # …and the next step is free to start in the same folder.
    nxt = await mission_steps.start_step(seam["mid"], "o1.cv", revision=1, expect_cwd=seam["cwd"])
    assert nxt["outcome"] == "started", nxt
    await runtime.stop(nxt["run"]["session_key"])


async def _started(seam) -> str:
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert out["outcome"] == "started", out
    return out["run"]["session_key"]


@pytest.mark.anyio
async def test_stop_on_the_real_path_proves_the_session_gone(seam):
    key = await _started(seam)
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "stopped"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert key not in missions.active_session_keys(seam["mid"])


@pytest.mark.anyio
async def test_a_second_stop_after_an_unproved_one_still_stops_the_member(seam, monkeypatch):
    """Independent review on #2001: an unproved Stop leaves the run `unknown` while the session is
    still this mission's step member. The next Stop must stop it, never record it as somebody
    else's and drop it."""
    from agent_sessions import runtime_cleanup

    key = await _started(seam)
    real = runtime_cleanup.contain_native

    async def leaks(engine, native):
        raise runtime_cleanup.UnresolvableRuntime("not proved")

    monkeypatch.setattr(runtime_cleanup, "contain_native", leaks)
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "unknown"
    assert key in missions.active_session_keys(seam["mid"])
    monkeypatch.setattr(runtime_cleanup, "contain_native", real)
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "stopped", run
    assert (await runtime.probe(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_detached_step_session_is_never_stopped_by_reconcile(seam):
    """Independent review on #2001: the operator detached the session; the step's evidence then
    settles. The run settles, and the session (no longer the mission's) keeps running."""
    key = await _started(seam)
    missions.detach(seam["mid"], key)
    _settle_evidence(seam["mid"], seam["doc"], "o1.posting")
    assert await mission_steps.reconcile(seam["mid"]) == 1
    run = missions.step_runs(seam["mid"])[0]
    assert run["state"] == "done" and "detached" in run["detail"]
    assert (await runtime.probe(key))["containment"] == "live"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_stop_never_kills_a_session_another_mission_now_holds(seam):
    key = await _started(seam)
    missions.detach(seam["mid"], key)
    other = missions.create_mission("another", cwd=seam["cwd"])["id"]
    missions.adopt(other, key)
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "failed" and "another mission" in run["detail"]
    assert (await runtime.probe(key))["containment"] == "live"
    assert missions.holder_of(key) == other
    await runtime.stop(key)


@pytest.mark.anyio
async def test_stopping_an_unknown_run_discharges_its_kept_record(seam):
    """An `unknown` run kept its dispatch record (a worker may exist): a proved stop answers the
    run AND frees the mission for the next launch, without waiting for a restart."""
    facts = mission_steps.launch_facts(seam["mid"], "o1.posting", revision=1)
    plan = missions.claim_step(
        seam["mid"],
        "o1.posting",
        revision=1,
        engine=facts["engine"],
        model=facts["model"],
        effort=facts["effort"],
        specialist=facts["specialist"],
        project_id=facts["project_id"],
        cwd=facts["cwd"],
        brief="b",
    )
    session_id, _ = mission_structured.ids(plan["plan_id"])
    key = f"{ENGINE}:{session_id}"
    missions.note_dispatch_session(seam["mid"], key, expect_plan=plan["plan_id"])
    await runtime.create_session(
        ENGINE, seam["cwd"], operation_id=session_id, specialist=facts["specialist"]
    )
    missions.settle_dispatch(
        seam["mid"], to="failed", detail="x", keep_record=True, expect_plan=plan["plan_id"]
    )
    assert missions.step_runs(seam["mid"])[0]["state"] == "unknown"
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "stopped"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert missions.get_dispatch(seam["mid"]) is None
    nxt = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert nxt["outcome"] == "started" and nxt["run"]["episode"] == 2
    await runtime.stop(nxt["run"]["session_key"])


@pytest.mark.anyio
async def test_a_request_cancelled_while_the_claim_runs_never_strands_it(seam, monkeypatch):
    """Hermes on #2001: cancel the request while the claim's worker is in flight. The claim still
    commits, and its launch still runs and settles: never a keyless `claimed` run holding a slot
    and a folder beside a `dispatching` mission."""
    entered, release = threading.Event(), threading.Event()
    real_claim = missions.claim_step

    def slow_claim(*a, **kw):
        entered.set()
        assert release.wait(30)
        return real_claim(*a, **kw)

    monkeypatch.setattr(missions, "claim_step", slow_claim)
    task = asyncio.ensure_future(
        mission_steps.start_step(seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"])
    )
    for _ in range(600):
        if entered.is_set():
            break
        await asyncio.sleep(0.05)
    assert entered.is_set()
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = missions.step_runs(seam["mid"])[0]
    assert run["state"] == "running", run  # launched and settled, not stranded
    assert missions.get_dispatch(seam["mid"]) is None
    assert missions.get_mission(seam["mid"])["state"] == "running"
    await runtime.stop(run["session_key"])


@pytest.mark.anyio
async def test_the_seam_carries_the_launch_fences(seam):
    prefs.set_orchestrator({"enabled": False})
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert _code(e) == "orchestration_off"


@pytest.mark.anyio
async def test_a_folder_that_moved_since_the_view_is_refused(seam):
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(
            seam["mid"], "o1.posting", revision=1, expect_cwd="/somewhere/else"
        )
    assert _code(e) == "cwd_moved" and missions.step_runs(seam["mid"]) == []


@pytest.mark.anyio
async def test_a_per_mission_override_is_refused_not_launched_without_its_specialist(seam):
    doc = copy.deepcopy(seam["doc"])
    for s in doc["steps"]:
        if s["id"] == "o1.posting":
            s["envelope"]["assignment"]["override"] = {
                "engine": ENGINE,
                "model": "claude-opus-5",
                "effort": None,
            }
    store_plan(seam["mid"], doc)
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(seam["mid"], "o1.posting", revision=2)
    assert _code(e) == "override_unsupported"


@pytest.mark.anyio
async def test_recovery_adopts_a_briefed_step_session_as_the_steps_and_never_sends_again(seam):
    from agent_sessions import mission_dispatch_recover

    facts = mission_steps.launch_facts(seam["mid"], "o1.posting", revision=1)
    plan = missions.claim_step(
        seam["mid"],
        "o1.posting",
        revision=1,
        engine=facts["engine"],
        model=facts["model"],
        effort=facts["effort"],
        specialist=facts["specialist"],
        project_id=facts["project_id"],
        cwd=facts["cwd"],
        brief="recover me",
        owner=DEAD_OWNER,
    )
    session_id, brief_id = mission_structured.ids(plan["plan_id"])
    key = f"{ENGINE}:{session_id}"
    assert missions.note_dispatch_session(seam["mid"], key, expect_plan=plan["plan_id"])
    await runtime.create_session(
        ENGINE, seam["cwd"], operation_id=session_id, specialist=facts["specialist"]
    )
    await runtime.submit_turn(key, operation_id=brief_id, text="recover me")
    assert await mission_dispatch_recover.recover_once() == 1
    run = missions.step_runs(seam["mid"])[0]
    assert run["state"] == "running" and run["session_key"] == key
    assert missions.get_mission(seam["mid"])["state"] == "running"
    assert _users(seam["project"]).count("recover me") == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_recovery_of_a_step_that_never_got_its_brief_starts_nothing_again(seam):
    from agent_sessions import mission_dispatch_recover

    facts = mission_steps.launch_facts(seam["mid"], "o1.posting", revision=1)
    plan = missions.claim_step(
        seam["mid"],
        "o1.posting",
        revision=1,
        engine=facts["engine"],
        model=facts["model"],
        effort=facts["effort"],
        specialist=facts["specialist"],
        project_id=facts["project_id"],
        cwd=facts["cwd"],
        brief="never sent",
        owner=DEAD_OWNER,
    )
    session_id, _ = mission_structured.ids(plan["plan_id"])
    key = f"{ENGINE}:{session_id}"
    missions.note_dispatch_session(seam["mid"], key, expect_plan=plan["plan_id"])
    await runtime.create_session(
        ENGINE, seam["cwd"], operation_id=session_id, specialist=facts["specialist"]
    )
    assert await mission_dispatch_recover.recover_once() == 1
    run = missions.step_runs(seam["mid"])[0]
    assert run["state"] in ("failed", "unknown")
    assert missions.get_mission(seam["mid"])["state"] == "planned"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert "never sent" not in _users(seam["project"])


@pytest.mark.anyio
async def test_the_routes_need_login_csrf_and_exact_bodies(seam, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    mid = seam["mid"]
    assert c.get(f"/api/missions/{mid}/steps").status_code == 401
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    view = c.get(f"/api/missions/{mid}/steps")
    assert view.status_code == 200 and view.headers["cache-control"] == "no-store"
    posting = next(s for s in view.json()["steps"] if s["id"] == "o1.posting")
    assert posting["can_start"] and posting["cwd"] == seam["cwd"]
    url = f"/api/missions/{mid}/steps/o1.posting/start"
    assert c.post(url, json={"revision": 1, "expect_cwd": seam["cwd"]}).status_code == 403
    bad = c.post(url, json={"revision": 1, "expect_cwd": seam["cwd"], "x": 1}, headers=hdr)
    assert bad.status_code == 422
    assert c.post(url, json={"revision": 1}, headers=hdr).status_code == 422
    stale = c.post(url, json={"revision": 9, "expect_cwd": seam["cwd"]}, headers=hdr)
    assert stale.status_code == 409 and stale.json()["code"] == "revision_superseded"
    stop = c.post(f"/api/missions/{mid}/steps/o1.posting/stop", json={"episode": 1}, headers=hdr)
    assert stop.status_code == 404


def test_dispatch_run_threads_the_step_into_the_structured_door(monkeypatch):
    """`mission_dispatch.run` hands a step plan's model, effort, specialist and step to the one
    door, and nothing new reaches the lead's dispatch."""
    seen = {}

    async def dispatch(**kw):
        seen.update(kw)
        raise mission_dispatch.headless_dispatch.DispatchError("stop here")

    mid, _ = _mission()
    plan = _claim(mid)
    plan["model"], plan["effort"] = "claude-opus-5", "high"
    monkeypatch.setattr(mission_structured, "dispatch", dispatch)
    monkeypatch.setattr(mission_dispatch.engines, "get", lambda e: type("P", (), {})())
    monkeypatch.setattr(mission_structured, "is_api", lambda p: True)
    out = asyncio.run(mission_dispatch.run(mid, plan, registry=object()))
    assert out["outcome"] == "refused"
    assert seen["specialist"] == SPEC and seen["effort"] == "high"
    assert seen["model"] == "claude-opus-5" and seen["step"] == ("o1.posting", 1)
    assert missions.step_runs(mid)[0]["state"] == "refused"
