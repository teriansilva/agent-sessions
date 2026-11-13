# ruff: noqa: F811 — `host`, `project`, `store` are shared fixtures imported below
"""A mission supervises each ACP API client end to end (#1903, #1273 P3).

The same path as `test_mission_api_e2e.py` (Claude, #1900), over the real native worker and the
fake ACP child, parameterized over the adapters that have a flip in this issue: the planner offers
the client, the dispatch creates the session and sends its brief, an edit approval becomes the
mission's one card (no payload on it), the operator decides it in the pane, the operator's relay
is a structured turn the agent answers, the judge quotes that answer, and the operator closes and
archives the mission, which proves the worker gone. Then: a mission text that starts a slash
command is refused before it is written, and the `API_MISSION_READY` bit is the rollback switch.

Only the model calls are stubbed (`review.complete_json`, `review._require_config`). The judge
reads the history the SESSION reports instead of the engine's own store (the fake child writes
none): `review._plain_transcript` is the one other seam, and the store readers have their own
tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import (
    actuator,
    mission_archive,
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
)
from agent_sessions import mission_supervisor as sup
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_mission_structured_dispatch import _run, store
from test_mission_structured_sends import _action, _auto, _deliver
from test_native_runtime import ENGINES, KINDS, frames, host, ident, pending, project, settle

__all__ = ["host", "project", "store"]

GOAL = "The login flow rejects expired tokens"
DONE_WORDS = "The login flow now rejects every expired token it is given"
#: What the fake's `EDIT` request carries in its diff: never on the mission's card.
EDIT_BODY = "+new"

#: Each adapter: the fake's names. The ask is the fake's `EDIT` turn (an edit request with its
#: diff), settled by the operator's `once`.
ADAPTERS = ["opencode"]


@pytest.fixture(params=ADAPTERS)
def adapter(request, monkeypatch):
    name = request.param
    # The flip itself is under test elsewhere: here the bit is what the code ships.
    assert kinds.API_MISSION_READY[KINDS[name]] is True, "the bit is the flip"
    return name


def _engine(adapter: str) -> str:
    return ENGINES[adapter]


def _prompts(work) -> list[str]:
    """The texts the ACP fake was prompted with, in order."""
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("method") == "session/prompt":
            out.append("".join(b.get("text", "") for b in frame["params"]["prompt"]))
    return out


@pytest.fixture
def judged(store, monkeypatch):
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    state = {"judge": None, "inputs": [], "key": None}

    async def complete_json(messages, **kw):
        if messages[0]["content"] == prompts.effective("mission_judge"):
            state["inputs"].append(messages[1]["content"])
            return state["judge"]
        return {"recap": "working", "assessment": "progress"}

    async def history():
        snap = await runtime.snapshot(state["key"])
        lines = []
        for turn in snap["turns"]:
            if turn.get("text"):
                lines.append(f"user: {turn['text']}")
            if turn.get("reply"):
                lines.append(f"assistant: {turn['reply']}")
        return "\n".join(lines)

    monkeypatch.setattr(review, "complete_json", complete_json)
    monkeypatch.setattr(
        review, "_plain_transcript", lambda key, aliases: asyncio.run(history()), raising=True
    )
    return state


def _claimed(adapter: str, cwd, brief="Harden the login flow"):
    m = missions.create_mission("ship it", cwd=str(cwd))
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(
        m["id"], project_id="prj_a", cwd=str(cwd), engine=_engine(adapter), brief=brief
    )
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"])


def _dispatched(adapter, project, judged) -> tuple[str, str]:
    assert _engine(adapter) in {o["id"] for o in mission_plan.engine_options()}
    mid, claimed = _claimed(adapter, project)
    result = asyncio.run(_run(mid, claimed))
    assert result["outcome"] == "started", result
    key = result["session_key"]
    judged["key"] = key
    assert missions.get_mission(mid)["state"] == "running"
    assert key in missions.active_session_keys(mid)
    _session, brief_id = mission_structured.ids(claimed["plan_id"])
    asyncio.run(settle(key, brief_id))
    return mid, key


def _live_cards(key: str) -> list[dict]:
    return [
        r
        for r in ledger.latest_by_id().values()
        if r.get("origin") == mission_permission.ORIGIN
        and r.get("session_id") == key
        and r.get("state") in ledger.ESCALATION_STATES
    ]


def _verdict(key: str, quote: str) -> dict:
    return {
        "met": True,
        "confidence": 0.97,
        "reason": "the agent said so",
        "evidence": [{"source": f"transcript:{key}", "quote": quote}],
    }


def test_a_mission_supervises_the_api_session_end_to_end(adapter, store, judged, host, project):
    mid, key = _dispatched(adapter, project, judged)
    assert _prompts(project) == ["Harden the login flow"]  # the brief, once
    missions.patch_objectives(
        mid, [{"op": "add", "key": "j0", "title": GOAL, "probe": "supervisor_judged", "gate": True}]
    )

    # A COMPLETED TURN IS NEVER EVIDENCE: a quote the transcript lacks leaves it unknown.
    judged["judge"] = _verdict(key, "the agent reports that all of the work is complete now")
    report = asyncio.run(mission_judge.judge_one(mid, mission_judge.Budget()))
    assert report["judged"] == {"j0": "unknown"}, report
    assert missions.get_mission(mid)["state"] == "running"

    # An edit approval in the session becomes the mission's ONE card, with no payload on it.
    ask = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=ask, text="EDIT"))
    [request] = asyncio.run(pending(key))["pending_requests"]
    out = asyncio.run(sup.run_pass(mid))
    [card] = _live_cards(key)
    assert card["escalation_reason"] == mission_permission.API_REASON
    assert card["api_pending"]["count"] == 1
    assert card["api_pending"]["pane"] == mission_observe.pane_path(key)
    assert EDIT_BODY not in json.dumps(card)
    assert out["per_session"][0]["permission"]["card"] == card["id"]

    # The operator decides it in the pane, where the input binding lives; the card retires.
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

    # The operator's relay is one structured turn, and the agent answers it.
    rec = _deliver(_action(mid, key, text=DONE_WORDS))
    assert rec["state"] == "delivered", rec
    _snap, turn = asyncio.run(settle(key, mission_structured.action_operation_id(rec["id"])))
    assert turn["reply"] == f"echo:{DONE_WORDS}"
    assert _prompts(project).count(DONE_WORDS) == 1

    judged["judge"] = _verdict(key, DONE_WORDS)
    rep = asyncio.run(mission_judge.judge_one(mid, mission_judge.Budget()))
    assert rep["judged"] == {"j0": "met"}, rep

    # The verdict moves the mission to review at most; the operator closes and archives it.
    asyncio.run(sup.run_pass(mid))
    if missions.get_mission(mid)["state"] == "running":
        missions.set_state(mid, "running", "review")
    missions.set_state(mid, "review", "done")
    asyncio.run(mission_archive.archive_mission(mid))
    assert asyncio.run(runtime.probe(key))["containment"] == "gone"


def test_a_mission_text_that_starts_a_slash_command_is_refused_before_it_is_written(
    adapter, store, judged, host, project
):
    """A prompt `/name` runs a command by itself, and a `subtask: true` one runs a child session
    under another agent, outside the guarded block (`test_mission_tool_routes.py`). The operator's
    own prompt keeps its commands; a mission's never reaches the client."""
    mid, key = _dispatched(adapter, project, judged)
    before = _prompts(project)
    rec = _deliver(_action(mid, key, text="/review the whole tree"))
    assert rec["state"] != "delivered", rec
    assert _prompts(project) == before, "the command text reached the agent"
    # A text that merely mentions one goes through.
    fine = _deliver(_action(mid, key, text="please run /review yourself"))
    assert fine["state"] == "delivered", fine
    # The operator's own turn, not guarded, is written as typed.
    own = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=own, text="/help"))
    asyncio.run(settle(key, own))
    assert _prompts(project)[-1] == "/help"


def test_the_mission_ready_bit_is_the_rollback_switch(
    adapter, store, judged, host, project, monkeypatch
):
    mid, key = _dispatched(adapter, project, judged)
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS[adapter], False)
    facts = asyncio.run(mission_observe.facts(key))  # observation keeps working
    assert facts["api"] and facts["readable"]
    assert _engine(adapter) not in {o["id"] for o in mission_plan.engine_options()}
    reasons = {u["id"]: u["reason"] for u in mission_plan.engine_unavailable()}
    assert reasons.get(_engine(adapter)) == mission_structured.NOT_MISSION_READY
    other, claimed = _claimed(adapter, project)
    assert asyncio.run(_run(other, claimed))["outcome"] == "refused"
    pre = orchestrator.precondition_for(key)
    auto = _deliver(
        _action(mid, key, text="AUTO ROLLED BACK", origin="pulse", precondition=pre),
        authority=_auto,
    )
    assert auto["state"] == "stale" and auto["detail"] == mission_structured.NOT_MISSION_READY
    assert "AUTO ROLLED BACK" not in _prompts(project)
    mine = _deliver(_action(mid, key, text="the operator still gets through"))
    assert mine["state"] == "delivered", mine
    assert actuator


def test_the_app_refuses_a_slash_text_itself_whatever_the_workers_age(
    adapter, store, judged, host, project, monkeypatch
):
    """A worker started before the worker-side check existed would send the command: the app's
    own check (`native_runtime._effect`) refuses before any worker is asked."""
    from agent_sessions import native_runtime

    mid, key = _dispatched(adapter, project, judged)
    sent = []
    real = native_runtime._call

    async def spy(gen, action, params):
        sent.append((action, params.get("text")))
        return await real(gen, action, params)

    monkeypatch.setattr(native_runtime, "_call", spy)
    rec = _deliver(_action(mid, key, text="/review the whole tree"))
    assert rec["state"] != "delivered", rec
    assert not [t for _a, t in sent if t and t.startswith("/")], sent
    fine = _deliver(_action(mid, key, text="a plain instruction"))
    assert fine["state"] == "delivered" and ("submit", "a plain instruction") in sent


def test_the_adapter_lookup_refuses_exactly_the_clients_that_run_slash_commands():
    from agent_sessions import native_protocol as np

    for kind in ("opencode-acp", "copilot-acp"):  # kind-data
        assert np.guarded_adapter_refusal(kind, "\ufeff/x")
        assert np.guarded_adapter_refusal(kind, "x /y") is None
    for kind in ("claude-stream-json", "codex-app-server"):  # kind-data
        assert np.guarded_adapter_refusal(kind, "/x") is None
    # An adapter nobody can name fails CLOSED for a slash text, and only for one.
    for kind in (None, "", "unknown"):
        assert np.guarded_adapter_refusal(kind, "/x")
        assert np.guarded_adapter_refusal(kind, "plain") is None


def test_the_worker_refuses_a_guarded_slash_text_on_its_own():
    """The second layer, pinned alone (the app's check hides it in the end-to-end tests): the
    worker's `frame` refuses a guarded text before the codec sees it, and keeps an unguarded one."""
    import types
    import uuid

    from agent_sessions import native_protocol, native_worker
    from agent_sessions.engines import opencode as oc

    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    me = types.SimpleNamespace(codec=codec, config={"cwd": "/w"})
    guarded = {"text": "/probe-sub", "operation_id": str(uuid.uuid4()), "authority": {}}
    with pytest.raises(native_protocol.ProtocolError, match="slash command"):
        native_worker.Worker.frame(me, "submit", guarded)
    with pytest.raises(native_protocol.ProtocolError, match="slash command"):
        native_worker.Worker.frame(me, "steer", {**guarded, "text": "﻿/probe-sub"})
    # Unguarded: past the guard (the unbound codec then refuses for its own reason).
    with pytest.raises(native_protocol.ProtocolError) as exc:
        native_worker.Worker.frame(
            me, "submit", {k: v for k, v in guarded.items() if k != "authority"}
        )
    assert "slash command" not in str(exc.value)
