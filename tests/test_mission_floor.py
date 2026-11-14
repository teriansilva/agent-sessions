# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""The floor for step sessions: nothing is approved automatically (#1581 S2, #1613 D8).

#1581 S1 starts one API session per agent step. The floor (irreversible, outward, money,
credentials) stays the Commander's because BattleLab settles NO tool request of a step session on
its own: every one waits for an operator decision on the session's own approvals card, and no
code path but the operator's decision route reaches `structured_runtime.decide`. These tests pin
that, and the inventory of what BattleLab does to a step session without the operator.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from agent_sessions import mission_steps, missions, prefs
from agent_sessions import structured_runtime as runtime
from test_mission_steps import _settle_evidence, _users, seam  # noqa: F401
from test_native_runtime import ENGINES, KINDS, host, ident, project  # noqa: F401

SRC = Path(missions.__file__).parent


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _calls(tree: ast.AST, name: str) -> list[int]:
    """Lines calling ``<something>.<name>(…)``: a decision always goes through a runtime object
    (a local helper merely named `decide` is not one)."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def test_only_the_operators_decision_route_settles_a_tool_request():
    """A RATCHET over the whole app: `decide` is reached from the operator's two decision routes
    (structured sessions and the chat client) and from the facade that implements it. Any other
    caller would be BattleLab approving a tool request on its own."""
    allowed = {
        "routes/structured.py",  # POST /api/structured/sessions/{key}/decisions (login + CSRF)
        "routes/chat.py",  # the chat client's own decision route
        "structured_runtime.py",  # the facade: delegates to the adapters
        "native_worker.py",  # the worker forwarding an admitted decision to the CLI
        "routes/ask.py",  # HQ proposals: the operator's decision on a staged card
    }
    found = {}
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC))
        lines = _calls(ast.parse(path.read_text()), "decide")
        if lines:
            found[rel] = lines
    assert set(found) <= allowed, {k: v for k, v in found.items() if k not in allowed}
    # The step seam itself never decides anything.
    for mod in ("mission_steps.py", "routes/mission_steps.py", "mission_structured.py"):
        assert not _calls(ast.parse((SRC / mod).read_text()), "decide"), mod


def test_the_only_verb_the_supervisor_may_take_alone_is_continue():
    """What BattleLab does to a held session without a tap: the global ceiling of automatic verbs
    is `{"continue"}`, a nudge typed into a session the mission holds (inside BattleLab). Nothing
    outward, no decision, no launch."""
    assert prefs.AUTO_VERBS_V1 == frozenset({"continue"})


@pytest.mark.anyio
async def test_a_step_sessions_tool_request_waits_for_the_operator(seam):
    """The real path: a started step session asks for a permission. Reconcile and the evidence
    settling do not answer it; only the operator's decision does."""
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    snap = await runtime.snapshot(key)
    assert not snap.get("bypass")
    turn = ident()
    # The fake child asks one exact permission request for this operator text.
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if snap["pending_requests"]:
            break
        await asyncio.sleep(0.05)
    request = snap["pending_requests"][0]
    assert snap["state"] == "awaiting_approval"
    # BattleLab's own passes run: nothing settles the request.
    assert await mission_steps.reconcile(seam["mid"]) == 0
    _settle_evidence(seam["mid"], seam["doc"], "o1.posting")
    assert await mission_steps.reconcile(seam["mid"]) == 0, "a step mid-approval is not idle"
    snap = await runtime.snapshot(key)
    assert [r["request_id"] for r in snap["pending_requests"]] == [request["request_id"]]
    assert missions.step_runs(seam["mid"])[0]["state"] == "running"
    # The operator decides; then the step settles on its next pass.
    await runtime.decide(
        key,
        decision_id=ident(),
        turn_id=turn,
        request_id=request["request_id"],
        decision="reject",
        user="marcus",
    )
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if not snap["pending_requests"] and not snap.get("active_turn"):
            break
        await asyncio.sleep(0.05)
    assert await mission_steps.reconcile(seam["mid"]) == 1
    assert missions.step_runs(seam["mid"])[0]["state"] == "done"


@pytest.mark.anyio
async def test_a_bypass_default_never_reaches_a_step_session(seam, monkeypatch):
    """#1215's grant on, the operator's default `bypass` on: a step session still asks."""
    monkeypatch.setattr(prefs, "unattended_bypass_granted", lambda: True)
    out = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    key = out["run"]["session_key"]
    assert not (await runtime.snapshot(key)).get("bypass")
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if snap["pending_requests"]:
            break
        await asyncio.sleep(0.05)
    assert snap["pending_requests"], "the tool request was not asked: something skipped it"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_step_launch_is_never_replayed_once_its_brief_is_on_record(seam):
    """An indeterminate effect is never repeated: starting the same step again while its run is
    open is refused, and the brief reached the agent exactly once."""
    first = await mission_steps.start_step(
        seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
    )
    assert first["outcome"] == "started"
    with pytest.raises(missions.StepRefused) as e:
        await mission_steps.start_step(
            seam["mid"], "o1.posting", revision=1, expect_cwd=seam["cwd"]
        )
    assert e.value.code == "open_run"
    assert len(_users(seam["project"])) == 1
    await runtime.stop(first["run"]["session_key"])


@pytest.mark.anyio
async def test_a_step_whose_session_was_never_created_stops_as_proved_empty(seam):
    """A key minted, no create on record (a launch cancelled in between): the run is `unknown`;
    its Stop proves nothing ran and frees the mission, without waiting for a restart."""
    from agent_sessions import mission_structured

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
    key = f"{ENGINES['claude']}:{mission_structured.ids(plan['plan_id'])[0]}"
    missions.note_dispatch_session(seam["mid"], key, expect_plan=plan["plan_id"])
    missions.settle_dispatch(
        seam["mid"], to="failed", detail="x", keep_record=True, expect_plan=plan["plan_id"]
    )
    assert missions.step_runs(seam["mid"])[0]["state"] == "unknown"
    run = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert run["state"] == "stopped"
    assert missions.get_dispatch(seam["mid"]) is None
