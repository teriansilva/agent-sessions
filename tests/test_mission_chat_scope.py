"""#1213 — a mission's chat answers about THAT mission, never from another session.

The incident: asked "What is the decision?" inside a mission whose agent sat on a permission
dialog, the turn ran as a fleet-wide `find` and answered with an unrelated session's decision.
Two missions and a standalone session here, each with a decision of its own; a mission-scoped
question must lead with its own mission's open decisions and retrieve only its own sessions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_sessions import (
    missions,
    orchestrator_chat,
    permission_prompts,
    prefs,
    pulse_chat,
    review,
    vtscreen,
)
from automation_helpers import append_current_action

A_SESSION = "opencode:ses_MissionA1213"
B_SESSION = "claude:44444444-4444-4444-4444-444444444444"
LONE = "claude:55555555-5555-5555-5555-555555555555"
FIX = Path(__file__).parent / "fixtures"


def _perm(name: str, geom: tuple[int, int], engine: str) -> dict:
    cells = vtscreen.render_cells((FIX / name).read_bytes(), *geom)
    p = permission_prompts.parse("\n".join(c.text for c in cells), engine, cells)
    assert p is not None
    return p


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True})
    a = missions.create_mission("mission A", cwd="/tmp")["id"]
    b = missions.create_mission("mission B", cwd="/tmp")["id"]
    missions.adopt(a, A_SESSION)
    missions.adopt(b, B_SESSION)
    now = time.time()

    def esc(eid, sid, mid, **extra):
        append_current_action(
            {
                "id": eid,
                "state": "escalated",
                "verb": "escalate",
                "session_id": sid,
                "mission_id": mid,
                "confidence": 1.0,
                "ts": now,
                "expires_at": now + 600,
                **extra,
            }
        )

    esc(
        "a-perm",
        A_SESSION,
        a,
        engine="opencode",
        observed_prompt={"permission": _perm("opencode_permission_grep.raw", (46, 65), "opencode")},
    )
    esc(
        "b-perm",
        B_SESSION,
        b,
        engine="claude",
        observed_prompt={"permission": _perm("claude_permission_bash.raw", (40, 100), "claude")},
    )
    esc("lone", LONE, None, engine="claude", rationale="the bell recommendation from #1057")

    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})

    async def classify(query, history):
        return "find"

    monkeypatch.setattr(orchestrator_chat, "_classify", classify)
    catalog = [
        {"id": sid, "title": f"session {sid}", "engine": sid.split(":")[0], "last_activity": now}
        for sid in (A_SESSION, B_SESSION, LONE)
    ]
    monkeypatch.setattr(pulse_chat, "build_catalog", lambda **k: [dict(c) for c in catalog])
    monkeypatch.setattr(pulse_chat, "_catalog_entry", lambda c, now: {"id": c["id"]})
    sent: list = []

    async def complete(messages, **kw):
        sent.append(json.loads(messages[-1]["content"]))
        return {"answer": "model text", "matches": []}

    monkeypatch.setattr(review, "complete_json", complete)
    return a, b, sent


@pytest.mark.anyio
async def test_a_mission_question_leads_with_its_own_decision(world):
    a, _b, sent = world
    out = await orchestrator_chat.ask("What is the decision?", mission_id=a)
    assert out["intent"] == "find"
    assert out["answer"].startswith("Waiting on you in this mission: opencode asks permission")
    assert 'Grep "(?i)mission"' in out["answer"]
    assert "Bash command" not in out["answer"], "mission B's decision leaked in"
    assert "#1057" not in out["answer"], "a standalone session's decision leaked in"
    assert out["answer"].endswith("model text")
    # The model only ever saw THIS mission and ITS session.
    ids = {e["id"] for e in sent[0]["catalog"]}
    assert A_SESSION in ids
    assert B_SESSION not in ids and LONE not in ids
    assert {i for i in ids if i.startswith(pulse_chat.MISSION_PREFIX)} == {
        pulse_chat.MISSION_PREFIX + a
    }


@pytest.mark.anyio
async def test_the_other_mission_gets_its_own(world):
    _a, b, sent = world
    out = await orchestrator_chat.ask("what does the agent need?", mission_id=b)
    assert "claude asks permission — Bash command" in out["answer"]
    assert "Grep" not in out["answer"]
    assert {e["id"] for e in sent[0]["catalog"]} & {A_SESSION, LONE} == set()


@pytest.mark.anyio
async def test_a_mission_with_nothing_open_says_only_what_the_model_found(world):
    a, _b, _sent = world
    from agent_sessions import orchestrator_ledger as ledger

    ledger.compare_and_set("a-perm", ledger.ESCALATION_STATES, "rejected", None)
    out = await orchestrator_chat.ask("What is the decision?", mission_id=a)
    assert out["answer"] == "model text"
    assert out["decisions"] == []


@pytest.mark.anyio
async def test_a_fleet_question_is_unchanged(world):
    _a, _b, sent = world
    out = await orchestrator_chat.ask("which session asked about the bell?")
    assert "decisions" not in out
    assert {A_SESSION, B_SESSION, LONE} <= {e["id"] for e in sent[0]["catalog"]}


@pytest.mark.anyio
async def test_the_answer_names_the_OPEN_escalation_not_the_newest(world):
    """#1218 review, finding 5: two objectives escalated; the NEWER one is then waived. The mission
    still waits on the older one, and that — not the newest event — is what the answer names."""
    a, _b, _sent = world
    from agent_sessions import orchestrator_ledger as ledger

    ledger.compare_and_set("a-perm", ledger.ESCALATION_STATES, "rejected", None)
    missions.instantiate_objectives(
        a,
        [
            {
                "key": "old",
                "title": "old goal",
                "probe": "forge_pr",
                "gate": True,
                "source": "playbook",
            },
            {
                "key": "new",
                "title": "new goal",
                "probe": "forge_pr",
                "gate": True,
                "source": "playbook",
            },
        ],
    )
    for key, at in (("old", 100.0), ("new", 200.0)):
        assert missions.escalate_once(
            a,
            session_key=A_SESSION,
            objective_key=key,
            episode=missions.objective_episode(a, key)[0],
            reason=f"{key} goal: stuck",
            now=at,
        )
    missions.patch_objectives(a, [{"op": "waive", "key": "new"}])
    assert missions.current_escalations(a) == ["old goal: stuck"]
    out = await orchestrator_chat.ask("What is the decision?", mission_id=a)
    assert "old goal: stuck" in out["answer"]
    assert "new goal: stuck" not in out["answer"]
