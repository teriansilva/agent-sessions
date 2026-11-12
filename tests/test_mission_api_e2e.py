# ruff: noqa: F811 — `host`, `project`, `store` are shared fixtures imported below
"""A mission supervises a Claude API session end to end (#1900, #1273 P3d).

The real path, over the real native worker and the fake Claude stream-json child (`host`): the
planner offers the client, the dispatch creates the session and sends its brief, an Edit approval
becomes the mission's one card, the operator decides it in the pane, the operator's relay is a
structured turn, the judge reads the structured history and quotes the agent's reply, and the
operator closes and archives the mission, which proves the worker gone. Only the model calls are
stubbed (`review.complete_json`, `review._require_config`).

Then: a restart mid-mission (the worker ends) is re-bound without relaunching anything, and the
`API_MISSION_READY` bit is the rollback switch."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from agent_sessions import (
    actuator,
    mission_dispatch_recover,
    mission_judge,
    mission_observe,
    mission_permission,
    mission_plan,
    mission_structured,
    missions,
    orchestrator,
    prefs,
    prompts,
    review,
    shelf_broker,
)
from agent_sessions import mission_supervisor as sup
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_mission_structured_dispatch import ENGINE, _claimed, _run, _users, store
from test_mission_structured_sends import _action, _auto, _deliver
from test_native_runtime import KINDS, host, ident, pending, project, settle

__all__ = ["host", "project", "store"]

GOAL = "The login flow rejects expired tokens"
#: What the fake agent answers to the relay: `echo:` + the words it was sent.
DONE_WORDS = "The login flow now rejects every expired token it is given"
SECRET = "NEW-CONTENT-NEVER-ON-A-CARD"


@pytest.fixture
def judged(store, monkeypatch):
    """The model, stubbed at its one transport: the judge answers what the test sets, the
    supervisor's own reading proposes nothing."""
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    state = {"judge": None, "inputs": []}

    async def complete_json(messages, **kw):
        if messages[0]["content"] == prompts.effective("mission_judge"):
            state["inputs"].append(messages[1]["content"])
            return state["judge"]
        return {"recap": "working", "assessment": "progress"}

    monkeypatch.setattr(review, "complete_json", complete_json)
    # The fixture client journals under its own root; the transcript reader resolves the store
    # through the environment, which conftest pins elsewhere. Point both at the same place.
    from agent_sessions.engines import registry

    monkeypatch.setenv("AGENT_SESSIONS_CHAT_DIR", str(registry.get(ENGINE).store_root()))
    return state


def _verdict(key: str, quote: str) -> dict:
    return {
        "met": True,
        "confidence": 0.97,
        "reason": "the agent said so",
        "evidence": [{"source": f"transcript:{key}", "quote": quote}],
    }


def _dispatched(project, brief="Harden the login flow") -> tuple[str, str]:
    assert ENGINE in {o["id"] for o in mission_plan.engine_options()}  # the planner offers it
    mid, claimed = _claimed(project, brief=brief)
    result = asyncio.run(_run(mid, claimed))
    assert result["outcome"] == "started", result
    key = result["session_key"]
    assert missions.get_mission(mid)["state"] == "running"
    assert key in missions.active_session_keys(mid)
    _session, brief_id = mission_structured.ids(claimed["plan_id"])
    asyncio.run(settle(key, brief_id))
    return mid, key


def _cards(key: str) -> list[dict]:
    return [
        r
        for r in ledger.latest_by_id().values()
        if r.get("origin") == mission_permission.ORIGIN and r.get("session_id") == key
    ]


def _live_cards(key: str) -> list[dict]:
    return [r for r in _cards(key) if r.get("state") in ledger.ESCALATION_STATES]


def _row(mid: str) -> dict:
    return next(o for o in missions.objectives(mid) if o["key"] == "j0")


def _judge(mid: str) -> dict:
    return asyncio.run(mission_judge.judge_one(mid, mission_judge.Budget()))


def _ended_events(mid: str, key: str) -> list[dict]:
    return [
        e
        for e in missions.get_mission(mid)["events"]
        if e.get("kind") == "session"
        and e.get("session_key") == key
        and (e.get("meta") or {}).get("ended")
    ]


def test_a_mission_supervises_a_claude_api_session_end_to_end(store, judged, host, project):
    mid, key = _dispatched(project)
    missions.patch_objectives(
        mid, [{"op": "add", "key": "j0", "title": GOAL, "probe": "supervisor_judged", "gate": True}]
    )

    # A COMPLETED TURN IS NEVER EVIDENCE: the brief's turn settled, and a verdict whose quote is
    # not in the transcript leaves the objective unknown and the mission running.
    judged["judge"] = _verdict(key, "the agent reports that all of the work is complete now")
    report = _judge(mid)
    assert report["judged"] == {"j0": "unknown"}, report
    assert _row(mid)["state"] != "met"
    assert missions.get_mission(mid)["state"] == "running"
    seen = judged["inputs"][-1]
    assert "user: Harden the login flow" in seen
    for word in ("completed", "idle", "turn_completed"):
        assert f": {word}" not in seen, seen

    # An Edit approval in the session becomes the mission's ONE card, with no payload on it.
    ask = ident()
    edit = {
        "tool_name": "Edit",
        "input": {"file_path": str(project / "app.py"), "old_string": "a", "new_string": SECRET},
    }
    asyncio.run(runtime.submit_turn(key, operation_id=ask, text="ASKJSON:" + json.dumps(edit)))
    waiting = asyncio.run(pending(key))
    [request] = waiting["pending_requests"]
    out = asyncio.run(sup.run_pass(mid))
    [card] = _live_cards(key)
    assert card["escalation_reason"] == mission_permission.API_REASON
    assert card["api_pending"]["count"] == 1
    assert card["api_pending"]["pane"] == mission_observe.pane_path(key)
    assert SECRET not in json.dumps(card)
    assert out["per_session"][0]["permission"]["card"] == card["id"]

    # The operator decides it in the pane, where the digest binding lives; the card retires.
    decided = asyncio.run(
        runtime.decide(
            key,
            decision_id=ident(),
            turn_id=ask,
            request_id=request["request_id"],
            decision="approve",
            user="operator",
        )
    )
    assert decided["handoff"] == "sent"
    asyncio.run(settle(key, ask))
    asyncio.run(sup.run_pass(mid))
    assert _live_cards(key) == []
    assert next(r for r in _cards(key) if r["id"] == card["id"])["state"] == "expired"

    # The operator runs a command in the pane; the transcript shows the call as a one-liner.
    ran = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=ran, text="RUN:pytest -q"))
    asyncio.run(settle(key, ran))

    # The operator's relay is one structured turn, and the agent answers it.
    rec = _deliver(_action(mid, key, text=DONE_WORDS))
    assert rec["state"] == "delivered", rec
    _snap, turn = asyncio.run(settle(key, mission_structured.action_operation_id(rec["id"])))
    assert turn["reply"] == f"echo:{DONE_WORDS}"
    assert _users(project).count(DONE_WORDS) == 1

    # The judge reads the structured history (tool one-liners included) and quotes the reply.
    judged["judge"] = _verdict(key, DONE_WORDS)
    rep = _judge(mid)
    assert rep["judged"] == {"j0": "met"}, rep
    assert _row(mid)["state"] == "met"
    assert "assistant/tool: Bash(pytest -q)" in judged["inputs"][-1]

    # The verdict moves the mission to review at most; the operator closes and archives it.
    asyncio.run(sup.run_pass(mid))
    state = missions.get_mission(mid)["state"]
    assert state in {"running", "review"}
    if state == "running":
        missions.set_state(mid, "running", "review")
    missions.set_state(mid, "review", "done")
    from agent_sessions import mission_archive

    asyncio.run(mission_archive.archive_mission(mid))
    assert asyncio.run(runtime.probe(key))["containment"] == "gone"


def _live_caps(key: str) -> int:
    con = missions._ready()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM mission_shelf_caps WHERE phys_key=? AND revoked_at IS NULL",
            (key,),
        ).fetchone()[0]
    finally:
        con.close()


def test_a_restart_mid_mission_records_the_end_once_and_relaunches_nothing(
    store, judged, host, project, monkeypatch
):
    from agent_sessions import secrets_broker

    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)
    mid, key = _dispatched(project)
    assert _live_caps(key) == 1  # the dispatched generation carries the mission's shelf
    worker = asyncio.run(runtime.snapshot(key))["native"]["worker"]
    launches, users = list(host.launches), _users(project)
    pre = orchestrator.precondition_for(key)  # proposed while the worker still ran, idle
    host.kill(worker)
    # A crashed worker of an open session is not `gone` to a probe (that needs a closed record),
    # so an automatic send refuses on it; its own unit and cgroup evidence prove it ended.
    assert asyncio.run(runtime.probe(key))["containment"] != "live"

    # The boot pass: one ended event, the shelf capability gone, membership unchanged.
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 1
    [ended] = _ended_events(mid, key)
    assert ended["meta"]["worker_id"] == worker
    assert key in missions.active_session_keys(mid)
    assert missions.get_mission(mid)["state"] == "running"
    assert _live_caps(key) == 0
    # …and a second pass (the supervisor's reconciling sweep) writes nothing.
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 0
    assert len(_ended_events(mid, key)) == 1
    assert host.launches == launches and _users(project) == users

    # An automatic send never brings the worker back; the operator's own words do.
    auto = _deliver(
        _action(mid, key, text="AUTO AFTER RESTART", origin="pulse", precondition=pre),
        authority=_auto,
    )
    assert auto["state"] == "stale" and "not running" in auto["detail"], auto
    assert host.launches == launches and "AUTO AFTER RESTART" not in _users(project)
    mine = _deliver(_action(mid, key, text="resume from here"))
    assert mine["state"] == "delivered", mine
    assert len(host.launches) == len(launches) + 1
    asyncio.run(settle(key, mission_structured.action_operation_id(mine["id"])))
    # The resumed generation is a new worker: it carries the mission's shelf again, and it is live.
    assert _live_caps(key) == 1
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 0


def test_a_failed_shelf_revoke_writes_no_ended_event_and_the_next_pass_retries_it(
    store, judged, host, project, monkeypatch
):
    """The record is written LAST, only once the capability is really gone: a revoke that fails
    (a locked store) leaves no ended event, so the next pass revokes and only then records it."""
    from agent_sessions import secrets_broker

    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)
    mid, key = _dispatched(project)
    assert _live_caps(key) == 1
    worker = asyncio.run(runtime.snapshot(key))["native"]["worker"]
    host.kill(worker)

    real = shelf_broker._revoke_ids

    def locked(ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(shelf_broker, "_revoke_ids", locked)
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 0
    assert _ended_events(mid, key) == []
    assert _live_caps(key) == 1

    monkeypatch.setattr(shelf_broker, "_revoke_ids", real)
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 1
    assert _live_caps(key) == 0
    [ended] = _ended_events(mid, key)
    assert ended["meta"]["worker_id"] == worker
    assert key in missions.active_session_keys(mid)


def test_the_mission_ready_bit_is_the_rollback_switch(store, judged, host, project, monkeypatch):
    mid, key = _dispatched(project)
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], False)
    # Observation keeps working…
    facts = asyncio.run(mission_observe.facts(key))
    assert facts["api"] and facts["readable"]
    # …the planner and picker give the reason…
    assert ENGINE not in {o["id"] for o in mission_plan.engine_options()}
    reasons = {u["id"]: u["reason"] for u in mission_plan.engine_unavailable()}
    assert reasons.get(ENGINE) == mission_structured.NOT_MISSION_READY
    # …a dispatch is refused…
    other, claimed = _claimed(project)
    assert asyncio.run(_run(other, claimed))["outcome"] == "refused"
    # …an automatic send is stale before anything is read…
    pre = orchestrator.precondition_for(key)
    auto = _deliver(
        _action(mid, key, text="AUTO ROLLED BACK", origin="pulse", precondition=pre),
        authority=_auto,
    )
    assert auto["state"] == "stale", auto
    assert auto["detail"] == mission_structured.NOT_MISSION_READY
    assert "AUTO ROLLED BACK" not in _users(project)
    # …and the operator's own relay is still delivered.
    mine = _deliver(_action(mid, key, text="the operator still gets through"))
    assert mine["state"] == "delivered", mine
    assert actuator  # the module every delivery above went through


def test_the_reconciling_sweep_rebinds_api_members(store, monkeypatch):
    from agent_sessions import mission_supervisor_loop

    calls = []

    async def rebind():
        calls.append(1)
        return 0

    monkeypatch.setattr(mission_dispatch_recover, "rebind_api_members", rebind)
    asyncio.run(mission_supervisor_loop.sweep(reconcile=True))
    assert calls == [1]
    asyncio.run(mission_supervisor_loop.sweep(reconcile=False))
    assert calls == [1]
