# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""The mission's controls over its steps (#1581 S4): pause, continue, stop.

Pause holds the mission (`hold = 'operator'`): no step is claimed, no automatic turn is admitted,
and a launch already claimed is refused at its effects (the pause beats a pending launch, the
order #1951 slice B relies on). Continue lifts only the operator's own pause. Stop pauses, then
stops every open step through the fence.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_steps, mission_structured, missions
from agent_sessions import structured_runtime as runtime
from test_mission_steps import (  # noqa: F401
    _adopt,
    _claim,
    _mission,
    _settle_evidence,
    _users,
    quiet,
    seam,
)
from test_native_runtime import ENGINES, KINDS, host, ident, project  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_pause_holds_claims_and_automatic_turns_and_continue_lifts_it():
    mid, _ = _mission()
    plan = _claim(mid)
    missions.settle_dispatch(mid, to="planned", detail="x", expect_plan=plan["plan_id"])
    b = missions.set_pause(mid, paused=True)
    assert b["hold"] == "operator"
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid)
    assert e.value.code == "budget" and "paused" in str(e.value)
    assert "paused" in missions.charge_turn(mid, ident(), "k:x")
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False  # the operator did it
    assert missions.set_pause(mid, paused=False)["hold"] is None
    assert _claim(mid)["run"]["episode"] == 2


def test_continue_never_lifts_a_spent_budget_and_pause_needs_a_started_step():
    mid, _ = _mission()
    with pytest.raises(missions.MissionError) as e:
        missions.set_pause(mid, paused=True)
    assert e.value.status == 409
    _claim(mid)
    b = missions.budget(mid)
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    assert missions.charge_turn(mid, ident(), "k:x")  # spent: held 'budget'
    with pytest.raises(missions.MissionError) as e:
        missions.set_pause(mid, paused=False)
    assert e.value.status == 409 and "raise a cap" in str(e.value)


@pytest.mark.anyio
async def test_a_pause_after_the_claim_refuses_the_launch_at_its_effect(seam, monkeypatch):
    """The pause beats a pending launch: committed between the claim and the create, nothing
    starts."""
    real_create = mission_structured.runtime.create_session

    async def pause_then_create(*a, **kw):
        missions.set_pause(seam["mid"], paused=True)
        return await real_create(*a, **kw)

    monkeypatch.setattr(mission_structured.runtime, "create_session", pause_then_create)
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert out["outcome"] == "refused" and out["run"]["state"] == "refused"
    assert "paused" in out["run"]["detail"]
    assert _users(seam["project"]) == []


@pytest.mark.anyio
async def test_stop_all_pauses_and_stops_every_open_step(seam):
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    res = await mission_steps.stop_all(seam["mid"])
    assert [r["state"] for r in res["stopped"]] == ["stopped"]
    assert (await runtime.probe(key))["containment"] == "gone"
    assert missions.budget(seam["mid"])["hold"] == "operator"
    with pytest.raises(missions.StepRefused):
        await mission_steps.start_step(
            seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
        )


@pytest.mark.anyio
async def test_pause_with_interrupt_interrupts_a_running_turn(seam, monkeypatch):
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    calls = []
    real_stop = runtime.stop

    async def busy(k):
        return {"active_turn": "turn-7"}

    async def interrupt(k, *, operation_id, turn_id):
        calls.append((k, turn_id))
        return {}

    monkeypatch.setattr(runtime, "snapshot", busy)
    monkeypatch.setattr(runtime, "interrupt", interrupt)
    res = await mission_steps.pause(seam["mid"], interrupt=True)
    assert res["interrupted"] == ["o1.posting"] and calls == [(key, "turn-7")]
    await real_stop(key)


def test_a_held_mission_offers_no_start(quiet, monkeypatch):
    from agent_sessions import prefs

    monkeypatch.setattr(mission_steps, "resolve_dir", lambda pid: (pid, "/tmp/folder"))
    prefs.set_orchestrator({"enabled": True})
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    _settle_evidence(mid, doc, "o1.posting")
    import asyncio

    asyncio.run(mission_steps.reconcile(mid))
    view = mission_steps.view_sync(mid)
    cv = next(s for s in view["steps"] if s["id"] == "o1.cv")
    assert cv["can_start"]
    missions.set_pause(mid, paused=True)
    cv = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == "o1.cv")
    assert not cv["can_start"] and cv["why_not"] == "the mission is paused"


def test_stop_all_keeps_going_when_a_run_moved(monkeypatch, quiet):
    import asyncio

    mid, _ = _mission()
    _adopt(mid, _claim(mid))
    calls = []
    real = mission_steps.stop_step

    async def moved_then_real(m, step, episode):
        calls.append(step)
        if len(calls) == 1:
            raise missions.StepRefused("not_open", "this step run is done; nothing to stop")
        return await real(m, step, episode)

    monkeypatch.setattr(mission_steps, "stop_step", moved_then_real)
    out = asyncio.run(mission_steps.stop_all(mid))
    assert calls == ["o1.posting"] and out["stopped"][0]["note"].startswith("this step run")


def test_the_control_routes_are_the_operators(seam, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    mid = seam["mid"]
    _claim(mid, cwd=seam["cwd"])
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    base = f"/api/missions/{mid}/steps"
    assert c.post(f"{base}/pause", json={}).status_code in (401, 403)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    for path in ("pause", "continue", "stop-all"):
        assert c.post(f"{base}/{path}", json={}).status_code == 403  # no CSRF
        assert c.post(f"{base}/{path}", json={"x": 1}, headers=hdr).status_code == 422
    assert c.post(f"{base}/pause", json={"interrupt": "yes"}, headers=hdr).status_code == 422
    ok = c.post(f"{base}/pause", json={"interrupt": False}, headers=hdr)
    assert ok.status_code == 200 and ok.json()["budget"]["hold"] == "operator"
    assert ok.headers["cache-control"] == "no-store"
    assert c.post(f"{base}/continue", json={}, headers=hdr).json()["budget"]["hold"] is None


@pytest.mark.anyio
async def test_stop_all_waits_for_a_launch_whose_brief_was_already_handed_over(seam, monkeypatch):
    """Hermes on #2007: Stop mission lands after the brief reached the agent but before the
    launch settled (the run is still `claimed`). It must not skip that step: it waits for the
    settlement, then stops the session it became."""
    import asyncio

    real_dispatch = mission_structured.dispatch
    stopping: list = []

    async def dispatch_then_stop(**kw):
        out = await real_dispatch(**kw)
        stopping.append(asyncio.ensure_future(mission_steps.stop_all(seam["mid"])))
        await asyncio.sleep(0.3)  # stop-all reads the run while it is still `claimed`
        return out

    monkeypatch.setattr(mission_structured, "dispatch", dispatch_then_stop)
    started = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = started["session_key"]
    res = await stopping[0]
    assert [r["state"] for r in res["stopped"]] == ["stopped"], res
    assert (await runtime.probe(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_stop_that_cannot_wait_is_recorded_and_finished_once_the_launch_settles(
    seam, monkeypatch
):
    """Hermes on #2007: Stop mission answers before an in-flight launch settles. It must say the
    step is still pending (never "stopped"), and the recorded stop must still stop that session
    once its launch settles."""

    monkeypatch.setattr(mission_steps, "SETTLE_WAIT_S", 0.0)
    real_dispatch = mission_structured.dispatch
    answers: list = []

    async def dispatch_then_stop(**kw):
        out = await real_dispatch(**kw)
        answers.append(await mission_steps.stop_all(seam["mid"]))  # the run is still `claimed`
        return out

    monkeypatch.setattr(mission_structured, "dispatch", dispatch_then_stop)
    started = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = started["session_key"]
    assert answers[0]["stopped"] == []
    assert [r["step_id"] for r in answers[0]["pending"]] == ["o1.posting"]
    assert missions.budget(seam["mid"])["stopping"] is True
    # The launch settled `running`; the recorded stop finishes it on the next pass.
    await mission_steps.reconcile(seam["mid"])
    assert missions.step_runs(seam["mid"])[0]["state"] == "stopped"
    assert (await runtime.probe(key))["containment"] == "gone"
    # Continue clears the recorded stop.
    assert missions.set_pause(seam["mid"], paused=False)["stopping"] is False


@pytest.mark.anyio
async def test_pause_with_interrupt_reports_a_launch_it_could_not_reach(seam, monkeypatch):
    monkeypatch.setattr(mission_steps, "SETTLE_WAIT_S", 0.0)
    real_dispatch = mission_structured.dispatch
    answers: list = []

    async def dispatch_then_pause(**kw):
        out = await real_dispatch(**kw)
        answers.append(await mission_steps.pause(seam["mid"], interrupt=True))
        return out

    monkeypatch.setattr(mission_structured, "dispatch", dispatch_then_pause)
    started = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert answers[0]["interrupted"] == [] and answers[0]["not_interrupted"] == ["o1.posting"]
    await runtime.stop(started["session_key"])


def test_a_stop_recorded_under_a_spent_budget_stays_visible_and_continue_cancels_it(
    quiet, monkeypatch
):
    """Independent review on #2007: budget hold → Stop mission → Raise. The stop must stay named
    on the board, refuse starts, and Continue must cancel it (not 409)."""
    from agent_sessions import prefs

    monkeypatch.setattr(mission_steps, "resolve_dir", lambda pid: (pid, "/tmp/folder-b"))
    prefs.set_orchestrator({"enabled": True})
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    b = missions.budget(mid)
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    assert missions.charge_turn(mid, ident(), "k:x")  # spent: hold 'budget'
    import asyncio

    asyncio.run(mission_steps.stop_all(mid))
    b = missions.budget(mid)
    assert b["hold"] == "budget" and b["stopping"] is True
    missions.set_budget(mid, turns_cap=50, seconds_cap=3600, expect_revision=b["revision"])
    b = missions.budget(mid)
    assert b["hold"] is None and b["stopping"] is True
    _settle_evidence(mid, doc, "o1.posting")
    asyncio.run(mission_steps.reconcile(mid))
    cv = next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == "o1.cv")
    assert not cv["can_start"] and cv["why_not"] == "the mission is being stopped"
    assert missions.set_pause(mid, paused=False)["stopping"] is False


def test_a_recorded_stop_refuses_a_claimed_launch_at_its_effect_whatever_the_hold():
    mid, _ = _mission()
    plan = _claim(mid)
    assert missions.step_launch_verdict(plan["plan_id"]) is None
    b = missions.budget(mid)
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    missions.charge_turn(mid, ident(), "k:x")  # hold 'budget': a budget hold alone refuses nothing
    assert missions.step_launch_verdict(plan["plan_id"]) is None
    missions.set_pause(mid, paused=True, stopping=True)
    assert "paused" in missions.step_launch_verdict(plan["plan_id"])


def test_a_v41_store_without_the_stop_column_converges():
    import sqlite3

    missions.create_mission("x", cwd="/tmp")
    db = missions._db_path()
    con = sqlite3.connect(db)
    con.execute("DROP TABLE mission_budgets")
    con.execute(
        "CREATE TABLE mission_budgets (mission_id TEXT PRIMARY KEY, turns_cap INTEGER NOT NULL, "
        "seconds_cap INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1, started_at REAL NOT "
        "NULL, hold TEXT, hold_detail TEXT, hold_at REAL, updated_at REAL NOT NULL)"
    )
    con.execute("PRAGMA user_version=40")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions.create_mission("y", cwd="/tmp")
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_budgets)")}
    con.close()
    assert "stopping" in cols


def test_a_recorded_stop_is_finished_even_with_supervision_off(monkeypatch):
    import asyncio

    from agent_sessions import mission_supervisor_loop as loop

    calls = []

    async def finish():
        calls.append(1)
        return 0

    monkeypatch.setattr(mission_steps, "finish_recorded_stops", finish)
    monkeypatch.setattr(loop, "_enabled", lambda: False)
    out = asyncio.run(loop.sweep(reconcile=False))
    assert out == {"skipped": "disabled"} and calls == [1]
