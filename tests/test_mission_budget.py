# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""One budget per step-mode mission: turns and wall clock, admitted atomically (#1581 S3).

The budget is created by a mission's first step claim and covers every turn BattleLab admits into
its sessions without a tap: each step's brief and each automatic send. No mission-ready client
reports token usage on its turns, so the budget is a workload limit (turns and time) and says so.
Reaching a cap holds the mission (`paused: budget`): no step claim and no automatic send until
the operator raises a cap; the mission needs them (never silent).
"""

from __future__ import annotations

import asyncio
import threading

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


def _caps(mid: str, turns: int, seconds: int = 3600) -> dict:
    b = missions.budget(mid)
    return missions.set_budget(
        mid, turns_cap=turns, seconds_cap=seconds, expect_revision=b["revision"]
    )


def test_the_first_step_claim_creates_the_budget_and_charges_its_brief():
    mid, _ = _mission()
    assert missions.budget(mid) is None  # not a step-mode mission yet
    _claim(mid)
    b = missions.budget(mid)
    assert b["turns"] == {"used": 1, "cap": missions.BUDGET_TURNS_DEFAULT}
    assert b["seconds"]["cap"] == missions.BUDGET_SECONDS_DEFAULT
    assert b["units"] == "turns and time" and b["estimated"] is True
    assert b["hold"] is None


def test_a_mission_without_steps_is_never_charged():
    mid = missions.create_mission("no steps")["id"]
    assert missions.charge_turn(mid, ident(), "claude-api:" + ident()) is None
    assert missions.budget(mid) is None


def test_the_last_turn_holds_the_mission_and_the_next_one_is_refused(quiet):
    mid, doc = _mission()
    _adopt(mid, _claim(mid))  # 1 turn: the brief
    _caps(mid, turns=2)
    key = "claude-api:" + ident()
    assert missions.charge_turn(mid, "op-a", key) is None  # the last unit: admitted
    b = missions.budget(mid)
    assert b["turns"]["used"] == 2 and b["hold"] == "budget"  # reaching the cap holds now
    assert missions.derive_needs_you([mid])[mid] == {"needs_you": True, "why": ["budget"]}
    assert "paused" in missions.charge_turn(mid, "op-b", key)
    assert missions.charge_turn(mid, "op-a", key) is None  # a replay is never charged twice
    assert missions.budget(mid)["turns"]["used"] == 2
    # A step claim is refused while held.
    _settle_evidence(mid, doc, "o1.posting")
    asyncio.run(mission_steps.reconcile(mid))
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o1.cv", cwd="/tmp/b")
    assert e.value.code == "budget"


def test_two_turns_contending_for_the_last_unit_admit_exactly_one():
    mid, _ = _mission()
    _claim(mid)
    _caps(mid, turns=2)
    barrier = threading.Barrier(2)
    results: list = []

    def go(op):
        barrier.wait()
        results.append(missions.charge_turn(mid, op, "claude-api:" + ident()))

    threads = [threading.Thread(target=go, args=(f"op-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is None for r in results) == [False, True], results
    assert missions.budget(mid)["turns"]["used"] == 2


def test_a_claim_that_would_pass_the_cap_is_refused_and_holds(quiet):
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    _caps(mid, turns=2)
    _settle_evidence(mid, doc, "o1.posting")
    asyncio.run(mission_steps.reconcile(mid))
    # The second brief takes the last unit and holds the mission at once.
    plan = _claim(mid, "o1.cv", cwd="/tmp/b")
    assert missions.budget(mid)["hold"] == "budget"
    missions.settle_dispatch(mid, to="planned", detail="x", expect_plan=plan["plan_id"])
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o1.cover_letter", cwd="/tmp/c")
    assert e.value.code == "budget"


def test_the_wall_clock_holds_between_turns(quiet):
    mid, _ = _mission()
    _claim(mid, now=1000.0)
    assert not missions.hold_if_spent(mid, now=1000.0 + missions.BUDGET_SECONDS_DEFAULT - 1)
    assert missions.hold_if_spent(mid, now=1000.0 + missions.BUDGET_SECONDS_DEFAULT)
    b = missions.budget(mid)
    assert b["hold"] == "budget" and "running time" in b["hold_detail"]
    assert "paused" in missions.charge_turn(mid, ident(), "claude-api:" + ident())


def test_raising_a_cap_lifts_the_hold_only_when_it_leaves_room():
    mid, _ = _mission()
    _claim(mid)
    _caps(mid, turns=1)  # used 1 of 1: the next turn holds
    assert "spent" in missions.charge_turn(mid, ident(), "k:x")
    assert missions.budget(mid)["hold"] == "budget"
    b = _caps(mid, turns=1)  # no room yet
    assert b["hold"] == "budget"
    b = _caps(mid, turns=5)
    assert b["hold"] is None and b["turns"] == {"used": 1, "cap": 5}
    with pytest.raises(missions.MissionError) as e:  # a stale revision
        missions.set_budget(mid, turns_cap=9, seconds_cap=3600, expect_revision=1)
    assert e.value.status == 409
    for bad in (0, 10_001, True, "5"):
        with pytest.raises(missions.MissionError):
            missions.set_budget(mid, turns_cap=bad, seconds_cap=3600, expect_revision=b["revision"])


def test_charges_survive_a_restart_and_stay_one_per_turn():
    mid, _ = _mission()
    _claim(mid)
    assert missions.charge_turn(mid, "op-x", "k:x") is None
    missions.reset_schema_cache_for_test()  # a fresh process reading the same store
    assert missions.charge_turn(mid, "op-x", "k:x") is None
    assert missions.budget(mid)["turns"]["used"] == 2


@pytest.mark.anyio
async def test_an_automatic_send_into_a_held_mission_sends_nothing(seam):
    """The real path: the supervisor's automatic send is admitted inside the write fence; a held
    budget refuses it there, so nothing reaches the agent."""
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    for _ in range(200):  # the brief's turn settles
        if not (await runtime.snapshot(key)).get("active_turn"):
            break
        await asyncio.sleep(0.05)
    _caps(seam["mid"], turns=1)

    def check():
        return True, ""

    state, detail = await mission_structured.send(
        action_id="act-" + ident(),
        key=key,
        text="continue please",
        mission_id=seam["mid"],
        check=check,
        precondition=await mission_structured.precondition(key),
        autonomous=True,
    )
    assert state == "stale" and "budget" in detail, (state, detail)
    assert "continue please" not in _users(seam["project"])
    # An operator-approved send is the operator's act: it is not charged.
    _caps(seam["mid"], turns=1)
    state, _detail = await mission_structured.send(
        action_id="act-" + ident(),
        key=key,
        text="from the operator",
        mission_id=seam["mid"],
        check=check,
        precondition=await mission_structured.precondition(key),
        autonomous=False,
    )
    assert state == "delivered"
    assert missions.budget(seam["mid"])["turns"]["used"] == 1
    await runtime.stop(key)


def test_budget_holds_leave_with_their_mission():
    mid, _ = _mission()
    plan = _claim(mid)
    missions.settle_dispatch(mid, to="planned", detail="x", expect_plan=plan["plan_id"])
    missions.set_state(mid, "planned", "abandoned")
    assert missions.delete_mission(mid)
    con = missions._ready(None)
    try:
        for t in ("mission_budgets", "mission_turn_charges"):
            assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t  # noqa: S608
    finally:
        con.close()


def test_the_budget_route_is_the_operators(seam, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    mid = seam["mid"]
    _claim(mid, cwd=seam["cwd"])
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    url = f"/api/missions/{mid}/budget"
    body = {"turns_cap": 9, "seconds_cap": 3600, "expect_revision": 1}
    assert c.post(url, json=body).status_code in (401, 403)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post(url, json=body).status_code == 403  # no CSRF
    assert c.post(url, json={**body, "x": 1}, headers=hdr).status_code == 422
    assert c.post(url, json={"turns_cap": 9}, headers=hdr).status_code == 422
    ok = c.post(url, json=body, headers=hdr)
    assert ok.status_code == 200 and ok.json()["budget"]["turns"]["cap"] == 9
    assert ok.headers["cache-control"] == "no-store"
    assert c.post(url, json=body, headers=hdr).status_code == 409  # the revision moved
    view = c.get(f"/api/missions/{mid}/steps").json()
    assert view["budget"]["turns"]["cap"] == 9 and view["budget"]["estimated"] is True


def test_a_cap_reached_in_the_claim_is_held_and_committed_before_the_refusal(quiet):
    """The claim's one writing refusal: lower the caps to what is used (no hold yet), claim, and
    the hold is on disk with its decision."""
    mid, doc = _mission()
    _adopt(mid, _claim(mid))
    b = missions.budget(mid)
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    assert missions.budget(mid)["hold"] is None  # used 1 of 1, nothing has asked since
    _settle_evidence(mid, doc, "o1.posting")
    asyncio.run(mission_steps.reconcile(mid))
    with pytest.raises(missions.StepRefused) as e:
        _claim(mid, "o1.cv", cwd="/tmp/b")
    assert e.value.code == "budget"
    assert missions.budget(mid)["hold"] == "budget"
    assert missions.derive_needs_you([mid])[mid]["why"] == ["budget"]


def test_the_budget_flag_never_sticks_to_a_closed_mission(quiet):
    """Independent review: stop the steps and close the mission, and it no longer needs you."""
    mid, _ = _mission()
    _adopt(mid, _claim(mid))
    b = missions.budget(mid)
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    assert missions.charge_turn(mid, ident(), "k:x")  # spent: held
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is True
    asyncio.run(mission_steps.stop_all(mid))
    missions.set_state(mid, "running", "abandoned")
    assert missions.derive_needs_you([mid])[mid] == {"needs_you": False, "why": []}


def test_the_wall_clock_never_holds_a_mission_that_launches_nothing(quiet):
    mid, doc = _mission()
    _adopt(mid, _claim(mid, now=1000.0))
    missions.set_state(mid, "running", "review")
    late = 1000.0 + missions.BUDGET_SECONDS_DEFAULT + 1
    assert not missions.hold_if_spent(mid, now=late)
    assert missions.budget(mid)["hold"] is None


def test_a_retried_admission_yields_to_the_operators_pause():
    mid, _ = _mission()
    _claim(mid)
    assert missions.charge_turn(mid, "op-r", "k:x") is None
    missions.set_pause(mid, paused=True)
    assert "paused" in missions.charge_turn(mid, "op-r", "k:x")  # the same turn, re-checked
    assert missions.budget(mid)["turns"]["used"] == 2  # and never charged twice


def test_the_board_offers_the_servers_raise():
    mid, _ = _mission()
    _claim(mid)
    b = missions.budget(mid)
    assert b["offer"] == {"turns_cap": b["turns"]["cap"], "seconds_cap": b["seconds"]["cap"]}
    missions.set_budget(mid, turns_cap=1, seconds_cap=3600, expect_revision=b["revision"])
    assert missions.budget(mid)["offer"]["turns_cap"] == 11  # half again, at least 10


def test_an_unreadable_budget_default_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSION_BUDGET_TURNS", "lots")
    assert missions._env_int("AGENT_SESSIONS_MISSION_BUDGET_TURNS", 60, 1, 10_000) == 60
    monkeypatch.setenv("AGENT_SESSIONS_MISSION_BUDGET_TURNS", "999999")
    assert missions._env_int("AGENT_SESSIONS_MISSION_BUDGET_TURNS", 60, 1, 10_000) == 10_000


def test_an_upgraded_store_gets_the_same_budget_schema_as_a_fresh_one():
    import sqlite3

    missions.create_mission("fresh", cwd="/tmp")
    db = missions._db_path()

    def tables():
        con = sqlite3.connect(db)
        try:
            return {
                r[0]: " ".join(r[1].split())
                for r in con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name LIKE 'mission_budgets%' "
                    "OR name LIKE 'mission_turn_charges%'"
                )
            }
        finally:
            con.close()

    fresh = tables()
    assert set(fresh) == {"mission_budgets", "mission_turn_charges", "mission_turn_charges_mission"}
    con = sqlite3.connect(db)
    con.execute("DROP TABLE mission_turn_charges")
    con.execute("DROP TABLE mission_budgets")
    con.execute("PRAGMA user_version=40")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions.create_mission("upgraded", cwd="/tmp")
    assert tables() == fresh
