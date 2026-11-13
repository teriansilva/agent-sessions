"""Missions refuse console clients server-side (#1902, #1273 P3f, #1613 D7/D17).

A mission runs on API sessions only. `mission_structured.admits` is the one admission, and every
producer asks it through `mission_plan.engine_options`: the picker, `PATCH /plan`, the first plan,
dispatch, spawn and adopt. `mission_dispatch.run` has no console branch left. A legacy mission that
already holds a terminal session keeps it for the operator: nothing automatic is typed into it, and
the operator's tap and relay still deliver.

Each negative test asserts the refusal's reason AND that nothing was created: no session, no
claim, no dispatch row, no keystrokes. The launchers are spies that fail the test if reached.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    automation,
    automation_runner,
    engines,
    headless_dispatch,
    mission_dispatch,
    mission_plan,
    mission_structured,
    missions,
    prefs,
    projects,
    review,
    scopedspawn,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.engines import registry
from agent_sessions.main import create_app
from agent_sessions.plugins import kinds

#: A terminal session `fake_jsonl` writes a real transcript for.
CONSOLE = "claude:11111111-1111-1111-1111-111111111111"
TERMINAL = "runs in a terminal"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated():
    session_input.reset()
    yield
    session_input.reset()


@pytest.fixture
def api_ready(monkeypatch):
    """Every API client can start (no host probe); each kind's mission bit is the real one."""
    monkeypatch.setattr(registry, "can_start", lambda prov: True)


@pytest.fixture
def no_launch(monkeypatch):
    """Both launchers, as spies a refused path must never reach."""
    reached: list[str] = []

    async def console(**kw):
        reached.append(f"console:{kw.get('engine')}")
        raise AssertionError("a mission reached the console launcher")

    async def structured(**kw):
        reached.append(f"structured:{kw.get('engine')}")
        raise AssertionError("a refused mission reached the structured launcher")

    monkeypatch.setattr(headless_dispatch, "dispatch", console)
    monkeypatch.setattr(mission_structured, "dispatch", structured)
    return reached


def _client(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    return c, hdr


@pytest.fixture
def routes(auth_cfg, tmp_home, tmp_path, fake_jsonl, api_ready, no_launch, monkeypatch):
    """A logged-in client over the REAL offer (no `engine_options` stub), a project entity, a real
    terminal transcript on disk, orchestration on, and both launchers as spies."""
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    proj = projects.create("repo", folders=[str(repo)], default_folder=str(repo))
    c, hdr = _client(auth_cfg)
    return c, hdr, proj, no_launch


def _old_console_plan(proj, *, state="planned"):
    """A plan written BEFORE the cutoff: it names a terminal client, and the store took it then."""
    m = missions.create_mission("ship it", project_id=proj.id, cwd=proj.default_folder)
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(
        m["id"], project_id=proj.id, cwd=proj.default_folder, engine="claude", brief="go"
    )
    missions.patch_objectives(m["id"], [{"op": "add", "key": "pr", "title": "a PR", "gate": True}])
    missions.settle_objectives_state(m["id"], "done")
    return m["id"], plan


def _dispatch_body(mid, plan, proj):
    return {
        "plan_id": plan["plan_id"],
        "expect_cwd": proj.default_folder,
        "expect_objectives": missions.objectives_digest(missions.get_mission(mid)["objectives"]),
    }


# ---- the admission --------------------------------------------------------------------------


def test_the_cutoff_lands_only_with_a_mission_ready_kind_to_offer():
    """Hermes' merge-order gate on 123884: refusing every console client is only safe while at
    least one API kind can hold a mission role (#1900 proved Claude's stream-json)."""
    assert any(kinds.API_MISSION_READY.values())


def test_admits_refuses_every_console_client_and_no_API_client_is_refused_for_being_one(
    api_ready,
):
    for prov in engines.all_providers():
        ok, why = mission_structured.admits(prov)
        if mission_structured.is_api(prov):
            ready = kinds.API_MISSION_READY.get(prov.manifest.api.kind, False)
            assert ok is ready, prov.engine_id
        else:
            assert (ok, why) == (False, mission_structured.CONSOLE_REFUSED), prov.engine_id


def test_the_refusal_names_the_api_client_to_use_or_says_none_exists_yet(api_ready, monkeypatch):
    assert mission_structured.console_refusal(engines.get("claude")) == (
        f"claude {TERMINAL}, and missions run on API clients: use claude-api"
    )
    monkeypatch.setitem(kinds.API_MISSION_READY, "codex-app-server", False)
    assert mission_structured.console_refusal(engines.get("codex")) == (
        f"codex {TERMINAL}, and none of its API clients can hold a mission role yet"
    )


def test_the_real_offer_holds_no_terminal_client(api_ready):
    offered = {o["id"] for o in mission_plan.engine_options()}
    assert offered, "a mission-ready API client should be offered"
    assert all(mission_structured.is_api(engines.get(e)) for e in offered), offered
    left_out = {u["id"]: u["reason"] for u in mission_plan.engine_unavailable()}
    assert TERMINAL in left_out["claude"]


def test_HQ_team_planning_refuses_a_terminal_client_with_the_same_words():
    """`mission_team._client`'s console branch (#1950), the one row the issue's table called
    covered but unasserted: the same constant as every other mission path."""
    from agent_sessions import mission_team

    client = mission_team._client(engines.get("claude"), {})
    assert client.mission_ready is False
    assert client.not_ready_reason == mission_structured.CONSOLE_REFUSED


def test_with_NO_mission_ready_client_the_plan_fails_with_the_remedy_and_asks_no_model(
    monkeypatch,
):
    """The empty picker (Hermes on 123884): no model call over an empty agent list."""
    monkeypatch.setattr(registry, "can_start", lambda prov: False)
    assert mission_plan.engine_options() == []

    async def model(*a, **k):
        raise AssertionError("the model was asked to pick from nothing")

    monkeypatch.setattr(review, "complete_json", model)
    mid = missions.create_mission("ship it")["id"]
    with pytest.raises(mission_plan.PlanError) as e:
        asyncio.run(mission_plan.propose(mid))
    assert e.value.status == 409 and str(e.value) == mission_plan.NO_MISSION_AGENT
    assert missions.get_plan(mid) is None


# ---- the routes: a direct API call --------------------------------------------------------------


def test_PATCH_plan_refuses_a_terminal_client_with_the_api_client_to_use(routes):
    c, hdr, proj, reached = routes
    mid, plan = _old_console_plan(proj)
    r = c.patch(
        f"/api/missions/{mid}/plan",
        json={"plan_id": plan["plan_id"], "engine": "codex"},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert TERMINAL in r.json()["detail"]
    assert missions.get_plan(mid)["plan_id"] == plan["plan_id"], "the plan was replaced"
    assert reached == []


def test_DISPATCH_of_an_OLD_console_plan_is_refused_before_the_claim(routes):
    c, hdr, proj, reached = routes
    mid, plan = _old_console_plan(proj)
    r = c.post(f"/api/missions/{mid}/dispatch", json=_dispatch_body(mid, plan, proj), headers=hdr)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert TERMINAL in detail and "use claude-api" in detail, detail
    # NOTHING CREATED, and the plan stays editable: not consumed, no claim, no dispatch row.
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_plan(mid)["plan_id"] == plan["plan_id"]
    assert missions.get_dispatch(mid) is None
    assert missions.active_session_keys(mid) == []
    assert reached == []


def test_ADOPT_refuses_a_terminal_session_and_records_nothing(routes):
    c, hdr, proj, reached = routes
    m = missions.create_mission("ship it", project_id=proj.id, cwd=proj.default_folder)
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CONSOLE}, headers=hdr)
    assert r.status_code == 409, r.text
    assert TERMINAL in r.json()["detail"]
    assert missions.active_session_keys(m["id"]) == []
    assert missions.all_active_memberships() == {}


def test_ADOPT_refuses_an_API_session_whose_client_is_not_mission_ready(routes, monkeypatch):
    c, hdr, proj, _reached = routes
    monkeypatch.setitem(kinds.API_MISSION_READY, "claude-stream-json", False)
    m = missions.create_mission("ship it", project_id=proj.id, cwd=proj.default_folder)
    key = "claude-api:33333333-3333-4333-8333-333333333333"
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": key}, headers=hdr)
    assert r.status_code == 409, r.text
    assert mission_structured.NOT_MISSION_READY in r.json()["detail"]
    assert missions.active_session_keys(m["id"]) == []


def test_SPAWN_refuses_a_terminal_client(routes):
    c, hdr, proj, reached = routes
    m = missions.create_mission("ship it", project_id=proj.id, cwd=proj.default_folder)
    for a, b in (("draft", "planned"), ("planned", "dispatching"), ("dispatching", "running")):
        missions.set_state(m["id"], a, b)
    missions.adopt(m["id"], CONSOLE)  # a LEGACY member, adopted before the cutoff
    r = c.post(
        f"/api/missions/{m['id']}/spawn",
        json={
            "parent_key": CONSOLE,
            "engine": "claude",
            "brief": "help",
            "expect_cwd": proj.default_folder,
        },
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert TERMINAL in r.json()["detail"]
    assert missions.active_session_keys(m["id"]) == [CONSOLE]
    assert reached == []


def test_config_says_missions_never_bypass_whatever_the_default(routes):
    """`mission_bypass` drives the Mission control settings line. A mission launch never skips
    permissions (#1613 D8), so it is `False` even with the operator's default on."""
    c, _hdr, _proj, _reached = routes
    prefs.set_agent_defaults({"bypass": True})
    assert prefs.unattended_bypass_granted() is True
    assert c.get("/api/config").json()["mission_bypass"] is False


# ---- the launcher: every other caller -----------------------------------------------------------


def test_a_CLAIMED_old_console_plan_is_refused_by_the_launch_itself(no_launch):
    """A caller that reaches `mission_dispatch.run` with a terminal client (a dispatch claimed
    before the upgrade, a spawn) is refused before anything is prepared."""
    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="p", cwd="/repo", engine="claude", brief="go")
    claimed = missions.claim_plan(m["id"], plan["plan_id"])
    out = asyncio.run(mission_dispatch.run(m["id"], claimed, registry=object()))
    assert out["outcome"] == "refused" and TERMINAL in out["reason"], out
    assert missions.get_mission(m["id"])["state"] == "planned"
    assert missions.get_dispatch(m["id"]) is None
    assert no_launch == []


def test_a_SPAWN_claim_naming_a_terminal_client_is_refused_and_the_mission_keeps_running(
    no_launch,
):
    m = missions.create_mission("ship it", cwd="/repo")["id"]
    for a, b in (("draft", "planned"), ("planned", "dispatching"), ("dispatching", "running")):
        missions.set_state(m, a, b)
    parent = "claude-api:11111111-1111-4111-8111-111111111111"
    missions.adopt(m, parent)
    spawn = missions.claim_spawn(
        m, parent_key=parent, engine="claude", cwd="/repo", brief="help", project_id="p"
    )
    out = asyncio.run(mission_dispatch.run(m, spawn, registry=object()))
    assert out["outcome"] == "refused" and TERMINAL in out["reason"], out
    assert out["state"] == "running" == missions.get_mission(m)["state"]
    assert missions.active_session_keys(m) == [parent]
    assert no_launch == []


def test_a_saved_automations_START_MISSION_never_launches_a_terminal_client(routes, monkeypatch):
    """The automation plans and dispatches a stored plan naming no engine of its own
    (`automation_runner._start_mission` → `dispatch_approved`). A plan that names a terminal
    client (here, what an old planner would have written) is refused with the reason."""
    from agent_sessions.routes import missions as mroutes

    _c, _hdr, proj, reached = routes

    async def producers(mid):
        missions.put_plan(
            mid, project_id=proj.id, cwd=proj.default_folder, engine="claude", brief="go"
        )
        missions.patch_objectives(mid, [{"op": "add", "key": "pr", "title": "a PR", "gate": True}])
        missions.settle_objectives_state(mid, "done")

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", producers)
    from test_automations import _made, _manual_run, _real_mission_config

    aid = _made(_real_mission_config(proj.id, "dispatch"))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] in ("refused", "failed"), res
    assert TERMINAL in str(res["run"].get("reason") or ""), res
    mid = res["run"]["mission_id"]
    assert missions.get_dispatch(mid) is None
    assert missions.active_session_keys(mid) == []
    assert reached == []


# ---- sends into a LEGACY mission's terminal member ----------------------------------------------


@pytest.fixture
def legacy(monkeypatch):
    """A running mission that adopted a terminal session before the cutoff, in yolo, with the
    terminal's write seam as a spy that honours `final_guard` the way `send_input` does."""
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    m = missions.create_mission("legacy", cwd="/tmp")["id"]
    for a, b in (("draft", "planned"), ("planned", "dispatching"), ("dispatching", "running")):
        missions.set_state(m, a, b)
    missions.adopt(m, CONSOLE)
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)
    monkeypatch.setattr(actuator, "check_precondition", lambda *a, **k: (True, ""))
    typed: list[bytes] = []

    def send(key, payload, **kw):
        guard = kw.get("final_guard")
        if guard is not None:
            ok, why = guard()
            if not ok:
                return session_input.Outcome(state="refused", detail=why)
        typed.append(payload)
        return session_input.Outcome(state="delivered")

    monkeypatch.setattr(session_input, "send_input", send)
    return m, typed


def _action(mid, *, state="approved", key=CONSOLE, **extra):
    rec = {
        "id": f"act-{len(ledger.latest_by_id())}",
        "state": state,
        "verb": "continue",
        "session_id": key,
        "confidence": 1.0,
        "ts": __import__("time").time(),
        "expires_at": __import__("time").time() + 600,
        "precondition": {},
        **({"mission_id": mid} if mid else {}),
        "authority": automation.capture(key, mid),
        **extra,
    }
    ledger.append(rec)
    return rec


@pytest.mark.anyio
async def test_an_AUTOMATIC_send_becomes_the_operators_proposal_and_types_nothing(legacy):
    """D17: "nudges to terminal sessions stay a proposal the Commander taps". The yolo pass's
    approval goes back to `proposed`, with the reason, and no tier or grant reaches the terminal."""
    mid, typed = legacy
    rec = _action(mid)
    assert await actuator.deliver_auto(rec) is None
    after = ledger.get(rec["id"])
    assert after["state"] == "proposed" and after["detail"] == actuator.CONSOLE_MEMBER_HELD
    assert typed == []


@pytest.mark.anyio
async def test_an_AUTONOMOUS_menu_answer_becomes_an_ordinary_one(legacy):
    mid, typed = legacy
    rec = _action(mid, verb="choose", option=1, auto_choose=True)
    assert await actuator.deliver_auto(rec) is None
    after = ledger.get(rec["id"])
    assert after["state"] == "proposed" and after["auto_choose"] is False
    assert typed == []


@pytest.mark.anyio
async def test_any_other_AUTOMATIC_caller_is_settled_stale_with_the_reason(legacy):
    mid, typed = legacy
    rec = _action(mid)
    out = await actuator.deliver(rec["id"], authority=lambda live: (True, ""))
    assert out["state"] == "stale" and out["detail"] == actuator.CONSOLE_MEMBER_HELD
    assert typed == []


def test_a_member_held_with_NO_mission_on_the_action_is_still_a_member(legacy):
    """Membership, not only the action's own field: an action into a session a mission holds now
    is into a mission's terminal member. (Its authority would refuse it too, for the scope; this
    is the predicate's own half.)"""
    hold = actuator.console_member_hold({"session_id": CONSOLE, "authority": {}})
    assert hold == actuator.CONSOLE_MEMBER_HELD


@pytest.mark.anyio
async def test_the_OPERATORS_TAP_still_delivers(legacy):
    mid, typed = legacy
    rec = _action(mid, state="proposed")
    out = await actuator.deliver(rec["id"], operator_approval=True)
    assert out["state"] == "delivered", out
    assert len(typed) == 1


@pytest.mark.anyio
async def test_the_OPERATORS_RELAY_still_delivers(legacy):
    mid, typed = legacy
    rec = _action(mid, state="approved", verb="relay", origin="operator", answer="carry on")
    out = await actuator.deliver(rec["id"])
    assert out["state"] == "delivered", out
    assert len(typed) == 1


@pytest.mark.anyio
async def test_a_PATROL_terminal_session_is_untouched_by_the_cutoff(legacy, monkeypatch):
    """Out of scope (#1902): a session no mission holds keeps its automatic sends."""
    _mid, typed = legacy
    free = "claude:44444444-4444-4444-8444-444444444444"
    rec = _action(None, key=free)
    called: list[str] = []

    async def deliver(aid, **kw):
        called.append(aid)
        return {"state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", deliver)
    assert await actuator.deliver_auto(rec) == {"state": "delivered"}
    assert called == [rec["id"]]


def test_an_UNREADABLE_membership_counts_as_held(monkeypatch):
    def boom():
        raise OSError("store unreadable")

    monkeypatch.setattr(missions, "all_active_memberships", boom)
    free = "claude:44444444-4444-4444-8444-444444444444"
    # Its own reason: a patrol session held only because nobody could tell is not a mission's.
    assert actuator.console_member_hold({"session_id": free, "authority": {}}) == (
        actuator.MEMBERSHIP_UNREADABLE
    )
    api = "claude-api:55555555-5555-4555-8555-555555555555"
    assert actuator.console_member_hold({"session_id": api, "authority": {}}) is None
