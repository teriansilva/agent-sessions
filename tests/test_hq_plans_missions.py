"""HQ plans every mission; the one-agent planner is retired (#2152).

* HQ's draft card takes a flow HQ WROTE when no deployed procedure fits (`mission.create_local`):
  checked before a card exists (`mission_procedures.check_local`, the Library's skills), shown on
  the card, checked again at the tap, applied through the route's own `mission_local` path.
* Plan with HQ plans an EXISTING draft (`mission.plan`, D4): same id, no second mission, goal and
  history kept, refused once the mission holds a plan — at staging, at the tap and inside the
  store's transaction.
* Nothing writes a one-agent plan any more (D5): `POST`/`PATCH /plan` are 410, no planner runs on
  create or at boot, and a legacy plan still dispatches (D2).

A REAL deployment drives the deployed cases (the synthetic application playbook), and the model
is the fake endpoint (`test_ask_mission_procedure`'s).
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import ast
import copy
import sqlite3
from pathlib import Path

import pytest

from agent_sessions import (
    ask_mission,
    ask_operations,
    ask_store,
    mission_procedures,
    missions,
    projects,
    specialists,
)
from mission_team_fixture import FLOW, LIBRARY
from test_ask_mission_procedure import (  # noqa: F401
    _cards,
    _deploy,
    _model,
    _note_mid,
    _revision,
    _settled,
    _wait,
    hq,
)
from test_ask_orchestrator import ai, answer, call, calls  # noqa: F401
from test_playbook_review import setup  # noqa: F401

SRC = Path(__file__).resolve().parent.parent / "src" / "agent_sessions"

#: HQ's own flow for a review mission (mockup B): a reviewer, the manager, the operator's accept.
REVIEW_FLOW = {
    "format": 5,
    "title": "Review the MCP server",
    "steps": [
        {
            "id": "assess",
            "title": "Assess the MCP server",
            "brief": "Read the code, run its tests, list what is missing. Read-only.",
            "actor": {"kind": "agent"},
            "role": {"skills": ["application-review"], "min_grade": "frontier"},
            "checklist": [{"key": "assessed", "title": "Assessed", "probe": "supervisor_judged"}],
        },
        {
            "id": "report",
            "title": "Write the findings down",
            "actor": {"kind": "agent"},
            "role": {"skills": ["cv-writing"], "min_grade": "strong"},
            "after": ["assess"],
            "checklist": [{"key": "written", "title": "Written", "probe": "supervisor_judged"}],
        },
        {
            "id": "accept",
            "title": "Accept the review",
            "actor": {"kind": "operator"},
            "after": ["report"],
            "checklist": [{"key": "accepted", "title": "Accepted", "probe": "none"}],
        },
    ],
}


@pytest.fixture
def library(monkeypatch):
    """The Library's skills (`specialists.library_revisions`), mutable per test."""
    lib = dict(LIBRARY)
    monkeypatch.setattr(specialists, "library_revisions", lambda: dict(lib))
    return lib


def _bare(hq) -> str:
    return projects.create("Bare", folders=[str(hq.folder)], default_folder=str(hq.folder)).id


def _local_turn(flow=None, **extra):
    args = {
        "project_index": 0,
        "instruction": "Review the MCP server",
        "flow_id": "review",
        "flow": copy.deepcopy(REVIEW_FLOW if flow is None else flow),
        **extra,
    }
    return (
        calls(("list_projects", {})),
        call("propose_mission", args, cid="pm"),
        answer("Confirm the card."),
    )


def _plan_rows() -> int:
    con = sqlite3.connect(missions._db_path())
    try:
        return con.execute("SELECT COUNT(*) FROM mission_plans").fetchone()[0]
    finally:
        con.close()


# ---- HQ writes the flow when none fits --------------------------------------------------------


def test_an_hq_written_flow_is_shown_validated_applied_and_assembled(hq, library, monkeypatch):
    _bare(hq)
    model = _model(
        monkeypatch,
        *_local_turn(),
        lambda b: call("read_plan", {"mission_id": _note_mid(b)}, cid="rp"),
        lambda b: call("assemble", {"mission_id": _note_mid(b), "reason": "staff it"}, cid="as"),
        answer("The team card is ready."),
    )
    cid = hq.new()
    assert hq.ask(cid, "plan a review of the MCP server in Bare").status_code == 200
    (card,) = _cards(hq, cid)
    assert card["op"] == "mission.create_local" and card["content"] is True
    assert "flow HQ wrote" in card["text"]
    assert missions.safe_list_missions()["missions"] == [], "staging creates nothing"
    # THE CARD SHOWS THE FLOW: every step, who does it, the role's skills and MINIMUM grade.
    shown = hq.c.get(f"/api/ask/proposals/{card['action_id']}/content").json()
    assert shown["kind"] == "flow" and shown["source"] == "mission_local"
    assert [s["title"] for s in shown["steps"]] == [
        "Assess the MCP server",
        "Write the findings down",
        "Accept the review",
    ]
    assert shown["steps"][0]["skills"] == ["application-review"]
    assert shown["steps"][0]["min_grade"] == "Senior"
    assert shown["steps"][2]["actor"] == "operator"
    assert model.result(2)["proposed"] is True

    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    first, cont = conv["turns"]
    done = first["proposals"][0]
    (m,) = missions.safe_list_missions()["missions"]
    assert done["state"] == "done" and done["mission"] == {"id": m["id"]}
    rev = _revision(m["id"])
    assert rev["revision"] == 1
    assert rev["document"]["objectives"][0]["procedure"]["source"] == "mission_local"
    steps = {s["id"]: s for s in rev["document"]["steps"]}
    assert steps["o1.assess"]["role"] == {"skills": ["application-review"], "min_grade": "frontier"}
    # HQ continued by itself and staged the team for the steps it wrote.
    (team_card,) = cont["proposals"]
    assert team_card["op"] == "team.assemble" and team_card["mission"] == {"id": m["id"]}
    assert _plan_rows() == 0, "no one-agent plan was written"


def test_an_invalid_flow_is_refused_at_staging_and_never_becomes_a_card(hq, library, monkeypatch):
    _bare(hq)
    bad = copy.deepcopy(REVIEW_FLOW)
    bad["steps"][0]["checklist"][0] = {
        "key": "on_branch",
        "title": "On the branch",
        "probe": "git_local",
        "probe_args": {"branch": "main"},  # a literal target: the validator refuses it
    }
    model = _model(monkeypatch, *_local_turn(bad))
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    assert _cards(hq, cid) == []
    assert "literal" in model.result(2)["error"]
    assert missions.safe_list_missions()["missions"] == []


def test_a_skill_missing_from_the_library_is_refused(hq, library, monkeypatch):
    _bare(hq)
    del library["application-review"]
    model = _model(monkeypatch, *_local_turn())
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    assert _cards(hq, cid) == []
    err = model.result(2)["error"]
    assert "no skill application-review" in err and "list_skills" in err


def test_a_project_without_a_deployment_needs_a_flow_never_a_bare_draft(hq, monkeypatch):
    """Replaces #1950's "a draft with no procedure": HQ always plans with a flow (#2152)."""
    _bare(hq)
    model = _model(
        monkeypatch,
        calls(("list_projects", {})),
        call("propose_mission", {"project_index": 0, "instruction": "Do it"}, cid="a"),
        answer("I'll write a flow."),
    )
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    assert _cards(hq, cid) == []
    assert "no deployed playbook: write a flow" in model.result(2)["error"]


def test_a_skill_removed_after_staging_is_refused_at_confirm(hq, library, monkeypatch):
    _bare(hq)
    _model(monkeypatch, *_local_turn())
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    (card,) = _cards(hq, cid)
    del library["cv-writing"]
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    assert card["state"] == "stale" and "no skill cv-writing" in card["reason"]
    assert missions.safe_list_missions()["missions"] == [], "nothing was created"


def test_a_binding_change_after_staging_is_refused_at_confirm(hq, library, monkeypatch):
    """What the flow's targets resolve to is bound at staging (`check_local`'s digest): a binding
    that moves before the tap is a stale card, never a draft with other targets."""
    _bare(hq)
    _model(monkeypatch, *_local_turn())
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    (card,) = _cards(hq, cid)
    real = mission_procedures._resolve_local

    def moved(pid, fid, doc):
        procedure, inputs, items = real(pid, fid, doc)
        items = [{**it, "probe_args": {"url": "https://other.example/"}} for it in items]
        return procedure, inputs, items

    monkeypatch.setattr(mission_procedures, "_resolve_local", moved)
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    assert card["state"] == "stale" and "bindings" in card["reason"]
    assert missions.safe_list_missions()["missions"] == []


def test_create_succeeds_and_the_flow_apply_fails_partly_done_with_the_draft(
    hq, library, monkeypatch
):
    _bare(hq)
    _model(monkeypatch, *_local_turn())
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    (card,) = _cards(hq, cid)

    def refuse(*a, **k):
        raise mission_procedures.ProcedureError("the deployment moved", status=409)

    monkeypatch.setattr(mission_procedures, "apply_local", refuse)
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    (m,) = missions.safe_list_missions()["missions"]
    assert card["state"] == "partly_done" and card["mission"] == {"id": m["id"]}
    assert "Draft mission created" in card["summary"] and "was not applied" in card["summary"]
    assert "Nothing was written" not in card["summary"]
    assert _revision(m["id"]) is None
    assert len(conv["turns"]) == 1, "a partly done card does not continue"


# ---- Plan with HQ on an existing draft (D4) ---------------------------------------------------


def _draft(pid: str, folder) -> str:
    mid = missions.create_mission("Review the MCP server", project_id=pid, cwd=str(folder))["id"]
    missions.append_event(mid, "operator_msg", text="and tell me what's missing")
    return mid


def _plan_turn(mid: str, **extra):
    return (
        calls(("mission_status", {"mission_id": mid})),
        call("list_procedures", {"project_index": 0}, cid="lp")
        if "flow" not in extra
        else calls(("list_projects", {})),
        call("propose_mission", {"mission_id": mid, **extra}, cid="pm"),
        answer("Confirm the card."),
    )


def test_plan_with_hq_plans_the_same_draft_and_keeps_its_goal_and_history(hq, monkeypatch):
    pid = _deploy(hq.folder, {"application": FLOW})
    mid = _draft(pid, hq.folder)
    before = missions.get_mission(mid)
    history = [e["seq"] for e in before["events"]]
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid}), ("list_projects", {})),
        call("list_procedures", {"project_index": 0}, cid="lp"),
        call("propose_mission", {"mission_id": mid, "procedure_index": 0}, cid="pm"),
        answer("Confirm the card."),
        lambda b: answer("Planned."),
    )
    cid = hq.new()
    r = hq.ask(cid, "Plan this mission: Review the MCP server", mission=mid)
    assert r.status_code == 200, r.text
    (card,) = _cards(hq, cid)
    assert card["op"] == "mission.plan" and card["verb"] == "Apply to this mission"
    assert card["mission"] == {"id": mid} and card["title"] == before["title"]
    assert "no second mission" in card["text"]
    shown = hq.c.get(f"/api/ask/proposals/{card['action_id']}/content").json()
    assert shown["source"] == "playbook" and shown["title"] == "Application"
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    done = conv["turns"][0]["proposals"][0]
    assert done["state"] == "done" and done["mission"] == {"id": mid}
    (m,) = missions.safe_list_missions()["missions"]
    assert m["id"] == mid, "no second mission"
    after = missions.get_mission(mid)
    assert after["instruction"] == before["instruction"] and after["title"] == before["title"]
    assert set(history) <= {e["seq"] for e in after["events"]}, "its history is kept"
    assert _revision(mid)["revision"] == 1


def test_plan_with_hq_takes_hqs_own_flow_for_the_draft(hq, library, monkeypatch):
    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid})),
        call(
            "propose_mission",
            {"mission_id": mid, "flow_id": "review", "flow": copy.deepcopy(REVIEW_FLOW)},
            cid="pm",
        ),
        answer("Confirm the card."),
        lambda b: answer("Planned."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    (card,) = _cards(hq, cid)
    assert card["op"] == "mission.plan" and "flow HQ wrote" in card["text"]
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    _wait(hq, cid, _settled)
    assert [m["id"] for m in missions.safe_list_missions()["missions"]] == [mid]
    rev = _revision(mid)
    assert rev["document"]["objectives"][0]["procedure"]["source"] == "mission_local"


def test_a_planned_mission_is_refused_at_staging(hq, monkeypatch):
    pid = _deploy(hq.folder, {"application": FLOW})
    mid = _draft(pid, hq.folder)
    now = mission_procedures.listing(mid)
    mission_procedures.apply_listed(mid, 0, now["listing"])
    model = _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid})),
        call("propose_mission", {"mission_id": mid, "procedure_index": 0}, cid="pm"),
        answer("It is planned."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    assert _cards(hq, cid) == []
    assert "already has a plan" in model.result(2)["error"]


def test_a_plan_applied_between_staging_and_confirm_is_stale_at_the_tap(hq, monkeypatch):
    pid = _deploy(hq.folder, {"application": FLOW})
    mid = _draft(pid, hq.folder)
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid}), ("list_projects", {})),
        call("list_procedures", {"project_index": 0}, cid="lp"),
        call("propose_mission", {"mission_id": mid, "procedure_index": 0}, cid="pm"),
        answer("Confirm the card."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    (card,) = _cards(hq, cid)
    now = mission_procedures.listing(mid)
    mission_procedures.apply_listed(mid, 0, now["listing"])  # another request planned it
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    assert card["state"] == "stale" and "planned elsewhere" in card["reason"]
    assert _revision(mid)["revision"] == 1, "nothing more was applied"


def test_a_plan_that_lands_after_the_taps_check_is_refused_at_the_write_boundary(hq, monkeypatch):
    """The race the facts check cannot see (Hermes, comment 133912): another request applies a
    plan AFTER the tap re-read the mission. `if_unplanned` makes the store refuse it inside its
    own transaction, and the card says nothing was written."""
    pid = _deploy(hq.folder, {"application": FLOW})
    mid = _draft(pid, hq.folder)
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid}), ("list_projects", {})),
        call("list_procedures", {"project_index": 0}, cid="lp"),
        call("propose_mission", {"mission_id": mid, "procedure_index": 0}, cid="pm"),
        answer("Confirm the card."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    (card,) = _cards(hq, cid)
    real = ask_mission._plan_facts

    async def facts_then_race(target, fields):
        seen = await real(target, fields)  # the tap's own check: still a draft without a plan
        assert seen["planned"] is False
        now = mission_procedures.listing(target)
        mission_procedures.apply_listed(target, 0, now["listing"])  # another request, just after
        return seen

    monkeypatch.setattr(ask_mission, "_plan_facts", facts_then_race)
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    assert card["state"] == "failed", card
    assert "Not planned" in card["summary"] and "already has a plan" in card["summary"]
    assert _revision(mid)["revision"] == 1, "only the other request's plan"
    assert [m["id"] for m in missions.safe_list_missions()["missions"]] == [mid]


def test_the_procedure_route_refuses_if_unplanned_inside_the_store(hq):
    pid = _deploy(hq.folder, {"application": FLOW})
    mid = _draft(pid, hq.folder)
    now = mission_procedures.listing(mid)
    body = {"index": 0, "listing": now["listing"], "if_unplanned": True}
    r = hq.c.post(f"/api/missions/{mid}/procedure", json=body, headers=hq.hdr)
    assert r.status_code == 200, r.text
    r = hq.c.post(f"/api/missions/{mid}/procedure", json=body, headers=hq.hdr)
    assert r.status_code == 409 and "already has a plan" in r.json()["detail"]
    bad = {**body, "if_unplanned": "yes"}
    assert hq.c.post(f"/api/missions/{mid}/procedure", json=bad, headers=hq.hdr).status_code == 422
    assert _revision(mid)["revision"] == 1


def test_a_message_binds_only_a_real_mission(hq, monkeypatch):
    cid = hq.new()
    assert hq.ask(cid, "plan it", mission="not-an-id").status_code == 422
    assert hq.ask(cid, "plan it", mission="msn_" + "0" * 32).status_code == 404
    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    model = _model(monkeypatch, answer("ok"))
    assert hq.ask(cid, "plan it", mission=mid).status_code == 200
    users = [r for r in ask_store.read(cid) if r.get("type") == "user"]
    assert users[-1]["mission"] == mid
    asked = [m for m in model.bodies[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert mid in asked and "Never create a second mission" in asked
    # The binding is carried to the conversation's later turns (an answer to HQ's question).
    model = _model(monkeypatch, answer("ok"))
    assert hq.ask(cid, "the first one").status_code == 200
    asked = [m for m in model.bodies[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert mid in asked


# ---- nothing writes a one-agent plan (D5) -----------------------------------------------------


def test_POST_and_PATCH_plan_are_410_and_write_no_plan_row(hq):
    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    legacy = _draft(pid, hq.folder)
    plan = missions.put_plan(legacy, project_id=pid, cwd=str(hq.folder), engine="x", brief="go")
    assert _plan_rows() == 1
    for target in (mid, legacy):
        r = hq.c.post(f"/api/missions/{target}/plan", headers=hq.hdr)
        assert r.status_code == 410 and "HQ plans missions" in r.json()["detail"]
        for body in (
            {"project_id": pid, "engine": "x", "brief": "go"},  # a manual first plan
            {"plan_id": plan["plan_id"], "brief": "changed"},  # an edit of a legacy plan
        ):
            r = hq.c.patch(f"/api/missions/{target}/plan", json=body, headers=hq.hdr)
            assert r.status_code == 410
    assert _plan_rows() == 1
    assert missions.get_plan(legacy)["brief"] == "go" and missions.get_plan(mid) is None


def test_no_production_module_writes_a_one_agent_plan():
    """The ratchet: the store's plan writers have no caller outside `missions.py`, and the planner
    module is gone. (`missions.py`'s own legacy paths still write `mission_plans` rows for a
    LEGACY mission, D2: a refused launch puts its claimed plan back, and Start again may restore
    the failed launch's proposal as a plan row. Neither ever writes one for a mission that had
    no one-agent plan before.)"""
    assert not (SRC / "mission_plan.py").exists()
    writers = {"put_plan", "begin_planning", "settle_plan", "settle_plan_from_stored"}
    hits = []
    for path in SRC.rglob("*.py"):
        if path.name == "missions.py" and path.parent == SRC:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in writers:
                hits.append(f"{path.relative_to(SRC)}:{node.lineno} {node.attr}")
            if isinstance(node, ast.Name) and node.id == "mission_plan":
                hits.append(f"{path.relative_to(SRC)}:{node.lineno} mission_plan")
    assert hits == []


@pytest.fixture
def plain(auth_cfg, tmp_path, monkeypatch):
    """The app with its REAL create-time producer (the `hq` fixture stubs it)."""
    from test_ask_sessions import Live

    monkeypatch.setenv("AGENT_SESSIONS_MISSION_MAINTENANCE", "0")  # see `hq`
    missions.reset_schema_cache_for_test()
    a = Live(auth_cfg)
    a.folder = tmp_path / "work"
    a.folder.mkdir()
    try:
        yield a
    finally:
        a.close()
        missions.reset_schema_cache_for_test()


def test_no_planner_runs_on_create(plain, monkeypatch):
    from agent_sessions import mission_objectives

    asked = []

    async def objectives(mid, **kw):
        asked.append(mid)
        return {}

    monkeypatch.setattr(mission_objectives, "propose_for_new_mission", objectives)
    pid = projects.create("Bare", folders=[str(plain.folder)], default_folder=str(plain.folder)).id
    r = plain.c.post(
        "/api/missions", json={"instruction": "go", "project_id": pid}, headers=plain.hdr
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    m = missions.get_mission(mid)
    assert m["plan_state"] == "skipped" and m["plan_generation"] == 0
    assert missions.get_plan(mid) is None and _plan_rows() == 0
    assert mid in asked, "the objectives producer still runs"
    detail = plain.c.get(f"/api/missions/{mid}").json()
    assert detail["plan"] is None and detail["plan_revision"] is None
    assert "plan_options" not in detail


def test_boot_settles_a_pending_plan_without_a_model_call(hq):
    pid = _bare(hq)
    bare, kept = _draft(pid, hq.folder), _draft(pid, hq.folder)
    missions.put_plan(kept, project_id=pid, cwd=str(hq.folder), engine="x", brief="go")
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET plan_state='pending' WHERE id IN (?, ?)", (bare, kept))
    con.commit()
    con.close()
    settled = missions.settle_pending_plans()
    assert sorted(settled) == sorted([bare, kept])
    assert missions.get_mission(bare)["plan_state"] == "skipped"
    assert missions.get_mission(bare)["plan_detail"] == missions.HQ_PLANS
    assert missions.get_mission(kept)["plan_state"] == "ready", "the legacy plan stays runnable"
    assert missions.get_plan(kept)["brief"] == "go"
    assert missions.settle_pending_plans() == []
    main = (SRC / "main.py").read_text()
    assert (
        "settle_pending_plans" in main
        and "recover_pending()\n"
        not in main.split("mission_objectives.recover_pending()")[1].split("scrub_if_pending")[0]
    )


def test_a_legacy_plan_still_dispatches_through_its_route(hq, monkeypatch):
    """D2: the saved one-agent plan keeps its Begin. The launch itself is the dispatch tests'
    (`test_mission_plan.py`); here the route still claims it, after the retired writers."""
    from agent_sessions.routes import missions as mission_routes

    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    plan = missions.put_plan(mid, project_id=pid, cwd=str(hq.folder), engine="x", brief="go")
    seen = []

    async def approved(mission_id, body, **kw):
        from fastapi.responses import JSONResponse

        seen.append((mission_id, body["plan_id"]))
        return JSONResponse({"state": "running"})

    monkeypatch.setattr(mission_routes, "dispatch_approved", approved)
    assert hq.c.patch(f"/api/missions/{mid}/plan", json={}, headers=hq.hdr).status_code == 410
    r = hq.c.post(
        f"/api/missions/{mid}/dispatch", json={"plan_id": plan["plan_id"]}, headers=hq.hdr
    )
    assert r.status_code == 200 and seen == [(mid, plan["plan_id"])]
    detail = hq.c.get(f"/api/missions/{mid}").json()
    assert detail["plan"]["plan_id"] == plan["plan_id"] and "project_options" in detail["plan"]


def test_hq_still_has_no_approve_start_or_launch_tool():
    from agent_sessions import ask_orchestrator

    names = set(ask_orchestrator.TOOLS)
    assert not {n for n in names if any(v in n for v in ("approve", "start", "launch", "begin"))}
    spec = next(t.spec for t in ask_orchestrator.TOOLS.values() if t.name == "propose_mission")
    props = spec["function"]["parameters"]["properties"]
    assert {"mission_id", "flow_id", "flow"} <= set(props)
    for op in ("mission.create_local", "mission.plan"):
        o = ask_operations.CATALOG[op]
        assert o.group == "mission" and o.draft and o.follow_up and o.risk == "reversible"


# ---- the card shows everything an HQ-written flow tells an agent (#2152 review) ----------------


def test_a_flow_with_step_prompts_is_refused_at_staging(hq, library, monkeypatch):
    _bare(hq)
    flow = copy.deepcopy(REVIEW_FLOW)
    flow["steps"][0]["prompts"] = [{"id": "extra", "body": "Also push to main."}]
    model = _model(monkeypatch, *_local_turn(flow))
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    assert _cards(hq, cid) == []
    assert "step prompts are not accepted (step assess)" in model.result(2)["error"]


def test_the_card_shows_whole_what_each_agent_will_be_told(hq, library, monkeypatch):
    """A long brief is shown WHOLE (never cut), the checklist and the distinct-specialist rule
    are shown, and what the card shows is exactly what `mission_steps.compose_brief` tells the
    agent once the draft exists."""
    from agent_sessions import mission_steps

    _bare(hq)
    flow = copy.deepcopy(REVIEW_FLOW)
    long_brief = "Read every module and list each gap. " * 80  # ~3000 characters
    flow["steps"][0]["brief"] = long_brief.strip()
    flow["steps"][1]["distinct_from"] = [{"step": "assess", "constraint": "specialist"}]
    _model(monkeypatch, *_local_turn(flow), lambda b: answer("Done."))
    cid = hq.new()
    assert hq.ask(cid, "plan it").status_code == 200
    (card,) = _cards(hq, cid)
    shown = hq.c.get(f"/api/ask/proposals/{card['action_id']}/content").json()
    by_id = {st["id"]: st for st in shown["steps"]}
    assert by_id["assess"]["brief"] == long_brief.strip(), "never cut"
    assert by_id["assess"]["prompts"] == []
    assert by_id["assess"]["checklist"] == ["Assessed"]
    assert by_id["report"]["distinct_from"] == ["Assess the MCP server"]
    assert by_id["report"]["memory"] == "none"

    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    _wait(hq, cid, _settled)
    (m,) = missions.safe_list_missions()["missions"]
    mission = missions.get_mission(m["id"])
    doc = _revision(m["id"])["document"]
    for step in (s for s in doc["steps"] if s["actor"] == "agent"):
        objective = next(o for o in doc["objectives"] if o["id"] == step["objective"])
        told = mission_steps.compose_brief(mission, objective, step)
        st = by_id[step["procedure_step"]]
        assert f"Objective {objective['id']}: {shown['title']}" in told
        assert f"Your step: {st['title']} ({step['id']})" in told
        if st["brief"]:
            how = told.split("How to do this step:\n", 1)[1].split("\n\nDone means", 1)[0]
            assert how == st["brief"], "the agent is told exactly the brief the card showed"
        else:
            assert "How to do this step:" not in told
        assert told.split("Done means (an independent judge checks each):\n", 1)[1].split(
            "\n\n", 1
        )[0] == "\n".join(f"- {c}" for c in st["checklist"])


# ---- Plan with HQ: the binding stops steering once planned; the project is checked ------------


def test_a_planned_mission_no_longer_steers_the_conversation(hq, library, monkeypatch):
    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid})),
        call(
            "propose_mission",
            {"mission_id": mid, "flow_id": "review", "flow": copy.deepcopy(REVIEW_FLOW)},
            cid="pm",
        ),
        answer("Confirm the card."),
        lambda b: answer("Planned."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    (card,) = _cards(hq, cid)
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    assert _revision(mid) is not None
    model = _model(monkeypatch, answer("ok"))
    assert hq.ask(cid, "what else is running?").status_code == 200
    asked = [m for m in model.bodies[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "plan_mission" not in asked and "Never create a second mission" not in asked


def test_plan_with_hq_checks_the_drafts_project_at_staging_and_at_the_tap(hq, library, monkeypatch):
    pid = _bare(hq)
    mid = _draft(pid, hq.folder)
    args = {"mission_id": mid, "flow_id": "review", "flow": copy.deepcopy(REVIEW_FLOW)}
    _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid})),
        call("propose_mission", args, cid="pm"),
        answer("Confirm the card."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    (card,) = _cards(hq, cid)
    projects.update(pid, archived=True)  # after the card, before the tap
    assert hq.decide(card["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    conv = _wait(hq, cid, _settled)
    (card,) = [p for t in conv["turns"] for p in t["proposals"]]
    assert card["state"] == "stale" and "archived" in card["reason"]
    assert _revision(mid) is None
    # …and at staging: an archived project stages nothing.
    model = _model(
        monkeypatch,
        calls(("mission_status", {"mission_id": mid})),
        call("propose_mission", args, cid="pm2"),
        answer("It's archived."),
    )
    cid = hq.new()
    assert hq.ask(cid, "Plan this mission", mission=mid).status_code == 200
    assert _cards(hq, cid) == []
    assert "archived" in model.result(2)["error"]
