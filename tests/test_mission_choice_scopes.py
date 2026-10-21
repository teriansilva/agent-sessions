"""#1185 remains reachable under the mission controller, with original scoped authority."""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import (
    automation,
    mission_choices,
    mission_supervisor,
    missions,
    orchestrator,
    prefs,
    review,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from test_auto_choose import MENU, SID, _paint, _running_mission, _typed
from test_auto_choose import world as _menu_world

menu_world = _menu_world


def _setup(monkeypatch, *, change=None, confidence=0.95, assessment="on_track"):
    mid = _running_mission()
    missions.instantiate_objectives(
        mid,
        [{"key": "ship", "title": "Ship", "probe": "forge_pr", "gate": True, "source": "playbook"}],
    )
    prefs.set_automation_policy("session", {"enabled": False, "autonomy": "off"})
    calls = []
    monkeypatch.setattr(review, "_require_config", lambda: None)
    monkeypatch.setattr(review, "gather_input", lambda *a: ("context " * 1000, "input-1"))

    async def model(messages, **kw):
        calls.append(messages)
        context = messages[-1]["content"].split("Session:\n", 1)[1]
        assert len(context) <= mission_supervisor._INPUT_MAX
        assert '"label": "Green"' in context and "SERVER-PARSED MENU" in context
        if change:
            change(mid)
        return {
            "assessment": assessment,
            "recap": "The agent is waiting at a colour menu.",
            "choose": {"option": 2, "confidence": confidence, "reason": "Green is requested"},
        }

    monkeypatch.setattr(review, "complete_json", model)
    return mid, calls


def test_mission_sweep_delivers_menu_with_standalone_automation_off(menu_world, monkeypatch):
    mid, calls = _setup(monkeypatch)
    result = asyncio.run(mission_supervisor.run_pass(mid))
    assert result["chosen"]["state"] == "delivered"
    assert result["chosen"]["authority"]["scope"] == "mission"
    assert result["chosen"]["rationale"] == "Green is requested"
    assert len(calls) == 1
    assert _typed(menu_world) == b"2"
    assert any(e["meta"].get("auto_choose") for e in missions.get_mission(mid)["events"])
    # No second model call for unchanged input, and no replay of a delivered choice.
    asyncio.run(mission_supervisor.run_pass(mid))
    assert len(calls) == 1
    assert _typed(menu_world) == b""


@pytest.mark.parametrize("change", ["menu", "ownership", "policy", "opt_in", "other_policy"])
def test_mission_menu_uses_original_context_and_authority(menu_world, monkeypatch, change):
    def mutate(mid):
        if change == "menu":
            _paint(MENU.replace("Green", "Purple"))
        elif change == "ownership":
            missions.detach(mid, SID)
            missions.adopt(mid, SID)
        elif change == "policy":
            prefs.set_automation_policy("mission", {"enabled": False})
            prefs.set_automation_policy("mission", {"enabled": True})
        elif change == "opt_in":
            missions.set_auto_choose(mid, False)
            missions.set_auto_choose(mid, True)
        else:
            prefs.set_automation_policy("session", {"enabled": True})

    mid, calls = _setup(monkeypatch, change=mutate)
    out = asyncio.run(mission_supervisor.run_pass(mid))
    assert len(calls) == 1
    assert bool(out.get("chosen")) is (change == "other_policy")
    assert _typed(menu_world) == (b"2" if change == "other_policy" else b"")
    if change != "other_policy":
        assert ledger.latest_by_id() == {}


@pytest.mark.parametrize("assessment,confidence", [("needs_approval", 0.99), ("on_track", 0.89)])
def test_low_confidence_and_operator_decisions_never_type(
    menu_world, monkeypatch, assessment, confidence
):
    mid, calls = _setup(monkeypatch, confidence=confidence, assessment=assessment)
    out = asyncio.run(mission_supervisor.run_pass(mid))
    assert len(calls) == 1
    assert _typed(menu_world) == b""
    if assessment == "needs_approval":
        assert out["escalated"] and not out.get("chosen")
    else:
        assert out["chosen"]["state"] == "escalated_low_confidence"


def test_mission_opt_in_roundtrip_survives_restart_and_does_not_revoke_standalone(menu_world):
    mid = _running_mission()
    held = {"session_id": SID, "mission_id": mid, "authority": automation.capture(SID, mid)}
    other = "claude:55555555-5555-4555-8555-555555555555"
    standalone = {"session_id": other, "authority": automation.capture(other)}
    epoch = session_input.current_policy_epoch("session")
    missions.set_auto_choose(mid, False)
    missions.set_auto_choose(mid, True)
    assert session_input.current_policy_epoch("session") == epoch
    session_input.reset()
    missions.reset_schema_cache_for_test()
    assert automation.check(held)[0] is False
    assert automation.check(standalone) == (True, "")


def test_standalone_pass_excludes_held_opted_in_menu(menu_world, monkeypatch):
    import time

    _running_mission()
    card = {"id": SID, "engine": "claude", "cwd": "/repo", "last_activity": time.time()}
    monkeypatch.setattr(orchestrator.pulse, "build_cards", lambda **kw: [card])
    assert orchestrator.eligible_cards()[0] == []


def test_archiving_mission_does_not_bump_standalone_policy_epoch(menu_world):
    from agent_sessions import mission_archive

    mid = _running_mission()
    before = session_input.current_policy_epoch("session")
    mission_archive._begin_archive_fenced(mid, abandon=True)
    assert session_input.current_policy_epoch("session") == before


@pytest.mark.parametrize(
    "raw",
    [
        True,
        2,
        {},
        {"option": True, "confidence": 1},
        {"option": 2, "confidence": True},
        {"option": 2, "confidence": float("nan")},
    ],
)
def test_model_cannot_manufacture_a_menu_or_confidence(raw):
    menu = {"options": [{"n": 2, "label": "Green"}]}
    assert mission_choices.reading({"choose": raw}, menu) is None
    assert mission_choices.reading({"choose": {"option": 2, "confidence": 1}}, None) is None


def test_two_waiting_menus_are_answered_across_two_sweeps(menu_world, monkeypatch):
    import contextlib
    import os
    import threading
    import tty

    from agent_sessions import scrollback

    mid, calls = _setup(monkeypatch)
    second = "claude:55555555-5555-4555-8555-555555555555"
    missions.adopt(mid, second, role="sub")
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(second, master, threading.Lock(), "headless")
    scrollback.note_cols(second, 120)
    scrollback._LAST_ROWS[second] = 40
    scrollback._buffer_append(second, b"\x1b[H\x1b[2J" + MENU.replace("\n", "\r\n").encode())
    try:
        first = asyncio.run(mission_supervisor.run_pass(mid))
        assert first["chosen"]["state"] == "delivered"
        assert _typed(menu_world) == b"2"
        assert _typed(slave) == b""
        assert len(calls) == 2
        assert first["per_session"][1]["held_back"]
        second_pass = asyncio.run(mission_supervisor.run_pass(mid))
        assert second_pass["chosen"]["state"] == "delivered"
        assert second_pass["chosen"]["session_id"] == second
        assert _typed(menu_world) == b""
        assert _typed(slave) == b"2"
        assert len(calls) == 3
        asyncio.run(mission_supervisor.run_pass(mid))
        assert len(calls) == 3
        assert _typed(slave) == b""
    finally:
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.parametrize("change", ["ownership", "policy", "opt_in", "menu"])
def test_rejected_menu_reading_gets_a_fresh_decision_next_sweep(menu_world, monkeypatch, change):
    changed = False

    def mutate_once(mid):
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "ownership":
            missions.detach(mid, SID)
            missions.adopt(mid, SID)
        elif change == "policy":
            prefs.set_automation_policy("mission", {"enabled": False})
            prefs.set_automation_policy("mission", {"enabled": True})
        elif change == "opt_in":
            missions.set_auto_choose(mid, False)
            missions.set_auto_choose(mid, True)
        else:
            _paint(MENU.replace("Green", "Purple"))

    mid, calls = _setup(monkeypatch, change=mutate_once)
    first = asyncio.run(mission_supervisor.run_pass(mid))
    assert not first.get("chosen")
    assert ledger.latest_by_id() == {}
    assert _typed(menu_world) == b""
    assert len(calls) == 1
    assert len([e for e in missions.get_mission(mid)["events"] if e["kind"] == "recap"]) == 1
    if change == "menu":
        _paint(MENU)
    second = asyncio.run(mission_supervisor.run_pass(mid))
    assert second["chosen"]["state"] == "delivered"
    assert len(calls) == 2
    assert _typed(menu_world) == b"2"
    assert len(ledger.latest_by_id()) == 1
    assert len([e for e in missions.get_mission(mid)["events"] if e["kind"] == "recap"]) == 2
    asyncio.run(mission_supervisor.run_pass(mid))
    assert len(calls) == 2
    assert _typed(menu_world) == b""


def test_indeterminate_menu_delivery_is_not_reconsidered(menu_world, monkeypatch):
    mid, calls = _setup(monkeypatch)
    send = session_input.send_input

    def uncertain_send(*args, **kwargs):
        result = send(*args, **kwargs)
        assert result.state == "delivered"
        raise RuntimeError("lost the outcome after sending bytes")

    monkeypatch.setattr(session_input, "send_input", uncertain_send)
    first = asyncio.run(mission_supervisor.run_pass(mid))
    assert first["chosen"]["state"] == "indeterminate"
    assert _typed(menu_world) == b"2"
    assert len(calls) == 1
    asyncio.run(mission_supervisor.run_pass(mid))
    assert len(calls) == 1
    assert _typed(menu_world) == b""
    assert len(ledger.latest_by_id()) == 1
