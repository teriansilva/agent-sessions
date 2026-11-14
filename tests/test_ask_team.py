"""HQ plans the team (#1950): HQ's team tools, and the downgrade refused from HQ too.

HQ reads the team and one step's lists, and proposes; every proposal is checked by the server's
own `mission_team.apply_op` before a card exists. The review on Sonnet comes back to HQ as the
refusal (grade_below_min) and never becomes a card; a legal change is a card whose tap runs
`POST …/plan/ops` as the operator, recorded with author `hq`. HQ has no approve/start/launch tool.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json
import re
import time
import uuid

import pytest

from agent_sessions import ask_operations, ask_orchestrator, missions, projects
from agent_sessions import mission_team as team
from mission_team_fixture import context, document, store_plan
from test_ask_orchestrator import ai, answer, call, calls  # noqa: F401
from test_ask_sessions import Live

#: Positions in the fixture plan (intake, posting, cv, cover_letter, documents, review, …, record)
#: and in the override list (opus ×3, sonnet ×3, haiku).
REVIEW, RECORD = 5, 9
SONNET, HAIKU = 3, 6


@pytest.fixture
def live(auth_cfg, monkeypatch):
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(team, "snapshot", lambda: context())
    # A new draft mission is planned in the background (`mission_plan.recover_pending`), which
    # (and its objectives written, `mission_objectives.recover_pending`), which would answer with
    # THIS test's scripted AI replies and shift every round: not under test here.
    from agent_sessions import mission_objectives, mission_plan

    async def _no_planning(**kw):
        return {}

    monkeypatch.setattr(mission_plan, "recover_pending", _no_planning)
    monkeypatch.setattr(mission_objectives, "recover_pending", _no_planning)
    # The rail's read degrades to an empty list while the store is briefly locked (a busy shared
    # host); this test is about the team tools, so it waits that out instead of flaking.
    real = missions.safe_list_missions

    def patient(**kw):
        out = real(**kw)
        for _ in range(50):
            if out.get("store_error") is None:
                break
            time.sleep(0.1)
            out = real(**kw)
        return out

    monkeypatch.setattr(missions, "safe_list_missions", patient)
    a = Live(auth_cfg)
    mid = missions.create_mission("apply for the job", title="Apply")["id"]
    store_plan(mid, document())
    r = a.c.post(
        f"/api/missions/{mid}/plan/ops",
        json={"op": "assemble", "operation_id": str(uuid.uuid4()), "expected_revision": 1,
              "reason": "assemble"},
        headers=a.hdr,
    )  # fmt: skip
    assert r.status_code == 200, r.text
    a.mid = mid
    try:
        yield a
    finally:
        a.close()
        missions.reset_schema_cache_for_test()


def _revisions(mid):
    con = missions._ready(None)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM mission_plan_revisions WHERE mission_id=?", (mid,)
        ).fetchone()[0]
    finally:
        con.close()


def _turn(a, ai, step: int, tool: str, **args):
    script = ai(
        calls(("list_missions", {})),
        calls(("read_plan", {"mission_id": a.mid})),
        calls(("read_pool", {"mission_id": a.mid, "step_index": step})),
        call(tool, {"mission_id": a.mid, "step_index": step, "reason": "cheaper", **args}),
        answer("done"),
    )
    cid = a.new()
    r = a.ask(cid, "put the review on sonnet")
    assert r.status_code == 200, r.text
    result = json.loads(script.tool_results(4)[-1]["content"])
    cards = [p for t in a.get(cid)["turns"] for p in t["proposals"]]
    return result, cards, cid


def test_the_review_on_sonnet_is_refused_to_hq_and_never_becomes_a_card(live, ai):
    """THE DOWNGRADE REGRESSION, from HQ: the same code and text the plan view shows."""
    result, cards, _ = _turn(live, ai, REVIEW, "set_override", model_index=SONNET)
    assert "error" in result
    assert "sonnet is Specialist, below Senior, which application-review needs" in result["error"]
    assert "grade_below_min" in result["error"]
    assert cards == []
    assert _revisions(live.mid) == 2


def test_a_legal_override_is_a_card_whose_tap_writes_an_hq_revision(live, ai):
    result, cards, cid = _turn(live, ai, RECORD, "set_override", model_index=HAIKU)
    assert result.get("proposed") is True, result
    (card,) = cards
    assert card["op"] == "team.set_override"
    assert _revisions(live.mid) == 2, "staging writes nothing"
    r = live.c.post(
        f"/api/ask/proposals/{card['action_id']}/decide",
        json={"decision": "confirm"},
        headers=live.hdr,
    )
    assert r.status_code in (200, 202), r.text
    assert live.settled(cid)["state"] == "done"
    rev = missions.plan_revision(live.mid)
    assert rev["revision"] == 3 and rev["author_kind"] == "hq"
    rec = next(s for s in rev["document"]["steps"] if s["id"] == "o1.record")
    assert rec["envelope"]["assignment"]["override"]["model"] == "haiku"


def test_an_index_hq_was_not_given_is_refused(live, ai):
    result, cards, _ = _turn(live, ai, REVIEW, "set_override", model_index=999)
    assert "error" in result and cards == []


def test_hq_has_no_approve_start_or_launch_tool():
    names = set(ask_orchestrator.TOOLS)
    team_tools = {"read_plan", "read_pool", "assemble", "assign", "set_override", "clear_override"}
    assert team_tools <= names
    assert not [n for n in names if re.search(r"approve|launch|dispatch|start_mission", n)]
    for op in ask_operations.CATALOG.values():
        if op.group != "team":
            continue
        method, path, body = op.route(
            "m1",
            {"op": op.action, "operation_id": "x", "expected_revision": 1, "reason": "r",
             "step": "o1.cv", "index": 0, "listing": "l"},
            {},
        )  # fmt: skip
        assert (method, path) == ("POST", "/api/missions/m1/plan/ops")
        assert body["author"] == "hq" and body["op"] == op.action


# ---- a draft mission from HQ, then its team in the same conversation (#1950) ------------------


def _project(a, name: str, folder=None) -> str:
    r = a.c.post("/api/projects", json={"name": name}, headers=a.hdr)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    pid = body["id"] if "id" in body else body["project"]["id"]
    if folder is not None:
        projects.update(pid, folders=[str(folder)], default_folder=str(folder))
    return pid


def _mission_cards(a, cid):
    return [p for t in a.get(cid)["turns"] for p in t["proposals"] if p["op"] == "mission.create"]


def test_propose_mission_is_a_proposal_that_can_only_create_a_draft():
    """The new tool is a `proposal`, its route is `POST /api/missions` with no cwd, and HQ still
    has no approve / start / launch tool: a draft starts nothing (Begin is the operator's)."""
    assert ask_orchestrator.TOOLS["propose_mission"].kind == "proposal"
    assert "propose_mission" not in ask_orchestrator.DRAFT_READ_TOOLS
    op = ask_operations.CATALOG["mission.create"]
    method, path, body = op.route("p-1", {"instruction": "apply for the job"}, {})
    assert (method, path) == ("POST", "/api/missions")
    assert body == {"instruction": "apply for the job", "project_id": "p-1"}
    assert [o.id for o in ask_operations.CATALOG.values() if o.group == "mission"] == [
        "mission.create"
    ]
    assert not [n for n in ask_orchestrator.TOOLS if re.search(r"approve|launch|dispatch|begin", n)]


def test_hq_creates_a_draft_mission_then_assembles_its_team_in_one_conversation(live, ai, tmp_path):
    """THE ACCEPTANCE FLOW (#1950): no mission exists; HQ proposes a draft, the operator taps, the
    server creates it as a DRAFT (nothing launched); the next turn finds it, reads its plan and
    stages the team card."""
    pid = _project(live, "Marcus Privat", tmp_path)
    ai(
        calls(("list_projects", {})),
        call("propose_mission", {"project_index": 0, "instruction": "Apply for the job"}, cid="m"),
        answer("Confirm the card and I will assemble the team."),
    )
    cid = live.new()
    r = live.ask(cid, "plan a job application mission for Marcus Privat")
    assert r.status_code == 200, r.text
    (card,) = _mission_cards(live, cid)
    assert card["title"] == "Marcus Privat" and "press Begin" in card["text"]
    assert len(missions.safe_list_missions()["missions"]) == 1, "staging creates nothing"
    d = live.decide(card["action_id"], {"decision": "confirm"})
    assert d.status_code in (200, 202), d.text
    settled = live.settled(cid)
    assert settled["state"] == "done", (settled.get("summary"), settled.get("reason"))
    (new,) = [m for m in missions.safe_list_missions()["missions"] if m["title"] != "Apply"]
    assert new["state"] == "draft" and new["project_id"] == pid
    assert new["instruction"] == "Apply for the job"
    # The server writes the plan in the background; here the fixture plan stands in for it.
    store_plan(new["id"], document())

    script = ai(
        calls(("list_missions", {"state": "draft"})),
        calls(("read_plan", {"mission_id": new["id"]})),
        call("assemble", {"mission_id": new["id"], "reason": "assemble the team"}),
        answer("The team card is ready."),
    )
    r = live.ask(cid, "confirmed")
    assert r.status_code == 200, r.text
    listed = json.loads(script.tool_results(1)[-1]["content"])
    assert new["id"] in [m["mission_id"] for m in listed["missions"]]
    cards = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    assert [c["op"] for c in cards] == ["mission.create", "team.assemble"]
    assert missions.get_mission(new["id"])["state"] == "draft"


def test_a_project_hq_was_not_given_or_one_the_route_would_refuse_stages_no_draft(
    live, ai, tmp_path
):
    """An index never listed this turn, a project with no folder and an archived one are each
    HQ's tool result, never a card: the route would refuse the same."""
    archived = _project(live, "Old", tmp_path)
    projects.update(archived, archived=True)
    _project(live, "Bare")
    script = ai(
        call("propose_mission", {"project_index": 0, "instruction": "x"}, cid="a"),
        calls(("list_projects", {})),
        call("propose_mission", {"project_index": 7, "instruction": "x"}, cid="b"),
        call("propose_mission", {"project_index": 0, "instruction": "x"}, cid="c"),
        call("propose_mission", {"project_index": 1, "instruction": "x"}, cid="d"),
        answer("ok"),
    )
    cid = live.new()
    assert live.ask(cid, "plan it").status_code == 200
    said = {n: json.loads(script.tool_results(n)[-1]["content"])["error"] for n in (1, 3, 4, 5)}
    assert "list_projects" in said[1]
    assert "index" in said[3] or "list_projects" in said[3]
    assert "no folder to work in" in said[4]  # "Bare", listed first: it is not archived
    assert "archived" in said[5]  # "Old"
    assert _mission_cards(live, cid) == []


def test_a_project_renamed_before_the_tap_makes_the_card_stale_and_creates_nothing(
    live, ai, tmp_path
):
    pid = _project(live, "Marcus Privat", tmp_path)
    ai(
        calls(("list_projects", {})),
        call("propose_mission", {"project_index": 0, "instruction": "Apply for the job"}, cid="m"),
        answer("Confirm the card."),
    )
    cid = live.new()
    assert live.ask(cid, "plan it").status_code == 200
    (card,) = _mission_cards(live, cid)
    before = len(missions.safe_list_missions()["missions"])
    projects.update(pid, name="Renamed")
    d = live.decide(card["action_id"], {"decision": "confirm"})
    assert d.status_code in (200, 202), d.text
    assert live.settled(cid)["state"] == "stale"
    assert len(missions.safe_list_missions()["missions"]) == before


def test_a_draft_mission_card_is_never_voice_confirmed():
    from agent_sessions import voice_calls

    assert voice_calls._voice_operation("mission.create") is False
    assert voice_calls._voice_operation("team.assemble") is False


def test_a_long_instruction_is_refused(live, ai):
    _project(live, "P")
    long = "word " * 80
    script = ai(
        calls(("list_projects", {})),
        call("propose_mission", {"project_index": 0, "instruction": long}, cid="m"),
        answer("ok"),
    )
    cid = live.new()
    assert live.ask(cid, "plan it").status_code == 200
    assert "error" in json.loads(script.tool_results(2)[-1]["content"])
    assert _mission_cards(live, cid) == []
