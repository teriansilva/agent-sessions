# ruff: noqa: F811 — `app`, `ai`, `host`, `project` are shared fixtures imported below
"""HQ nudges reach API sessions (#1960).

`send_nudge` admits an API session (`runtime = "api"`) as it admits a terminal agent, and sends
at once on HQ's own authority. The precondition it records is the structured one (last operator
turn, idle), and the send goes through `actuator.deliver` → `mission_structured.send`: one guarded
structured turn, never keystrokes. The real native worker runs over the fake native child (`host`).

A refused call gives its slot back, so refusals never use up the turn's budget.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_sessions import (
    actuator,
    ask_orchestrator,
    mission_structured,
    orchestrator_ledger,
    prefs,
    pulse_chat,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from test_ask_orchestrator import App, ai, answer, app, call  # noqa: F401
from test_native_runtime import ENGINES, frames, host, ident, pending, project  # noqa: F401

ENGINE = ENGINES["claude"]
SHELL = "shell:00000000-0000-4000-8000-0000000000aa"
TEXT = "Hermes approved #1899 — merge it and continue."


def _users(work) -> list[str]:
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("type") == "user":
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
    return out


def _card(key: str, engine: str) -> dict:
    return {
        "id": key,
        "engine": engine,
        "title": "crew list filter",
        "cwd": "/work/game",
        "project": {"kind": "folder", "id": "p1", "name": "game"},
        "state": "idle",
        "last_activity": time.time() - 9 * 3600,
    }


@pytest.fixture
def fleet(monkeypatch):
    """HQ's find and catalog see exactly the sessions in ``state["cards"]``; assistance is on."""
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    state: dict = {"cards": []}

    async def fake_ask(query, history=None, *, working_keys=None, on_event=None, scope=None):
        return {
            "answer": "found",
            "matches": [
                {"id": c["id"], "title": c["title"], "state": "idle", "why": "stuck"}
                for c in state["cards"]
            ],
            "mission_matches": [],
            "stage": "catalog",
            "configured": True,
        }

    async def catalog(self):
        return [dict(c) for c in state["cards"]]

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    monkeypatch.setattr(ask_orchestrator._Turn, "catalog", catalog)
    return state


@pytest.fixture
def api_session(host, project, fleet):
    key = asyncio.run(runtime.create_session(ENGINE, str(project), operation_id=ident()))[
        "session_key"
    ]
    fleet["cards"].append(_card(key, ENGINE))
    return key


def _nudge(ai, key, text=TEXT):
    return ai(
        call("find_sessions", {"query": "stuck"}, cid="f"),
        call("send_nudge", {"key": key, "text": text}, cid="n"),
        answer("Sent."),
    )


def _result(script, n=2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


def _ledger_for(key) -> list[dict]:
    return [r for r in orchestrator_ledger.latest_by_id().values() if r.get("session_id") == key]


def test_an_api_session_is_nudged_as_one_structured_turn_at_once(
    app, ai, api_session, project, monkeypatch
):
    typed: list = []
    monkeypatch.setattr(session_input, "send_input", lambda *a, **k: typed.append(a))
    script = _nudge(ai, api_session)
    cid = app.new()
    (card,) = app.ask(cid, "nudge the stuck sessions").json()["turn"]["proposals"]
    assert _result(script)["sent"] is True
    assert typed == []  # never keystrokes
    assert _users(project).count(TEXT) == 1
    (rec,) = _ledger_for(api_session)
    assert rec["state"] == "delivered" and rec["engine"] == ENGINE and rec["answer"] == TEXT
    # The structured precondition, not a screen fingerprint: there is no screen.
    pre = rec["precondition"]
    assert pre["key"] == api_session and pre["api_idle"] is True
    assert "screen_fingerprint" not in pre
    snap = asyncio.run(runtime.snapshot(api_session))
    turn = next(t for t in snap["turns"] if t.get("text") == TEXT)
    assert turn["turn_id"] == mission_structured.action_operation_id(card["action_id"])
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["state"] == "delivered"


def test_an_api_session_waiting_on_a_request_gets_the_text_queued_never_as_an_answer(
    app, ai, api_session, project, monkeypatch
):
    """#1960 Phase 2: an open approval or question no longer refuses HQ. The text is a NEW turn
    queued behind it; a structured approval is a decision, never answered by text."""
    real = runtime.snapshot

    async def asking(key):
        snap = await real(key)
        return {**snap, "pending_requests": [{"id": "req-1", "kind": "approval"}]}

    monkeypatch.setattr(runtime, "snapshot", asking)
    script = _nudge(ai, api_session)
    app.ask(app.new(), "nudge it")
    assert _result(script)["sent"] is True
    assert _users(project).count(TEXT) == 1
    (rec,) = _ledger_for(api_session)
    assert rec["state"] == "delivered"


def test_a_working_api_session_gets_the_text_as_a_non_autonomous_send(
    app, ai, api_session, project, monkeypatch
):
    """HQ's send is the Commander's instruction, not an automatic one: it may queue behind a
    running turn, which an automatic send (needing the idle session it was proposed against) may
    not."""
    seen: list[dict] = []
    real_send = mission_structured.send

    async def spy(**kw):
        seen.append(kw)
        return await real_send(**kw)

    monkeypatch.setattr(mission_structured, "send", spy)
    monkeypatch.setattr(actuator, "busy_keys", lambda registry, now=None: {api_session})
    script = _nudge(ai, api_session)
    app.ask(app.new(), "nudge it")
    assert _result(script)["sent"] is True
    assert seen and seen[0]["autonomous"] is False and seen[0]["attended"] is True


# ---- interrupt on an API session (#1960 Phase 2) ---------------------------------------------


def _interrupt(ai, key, text=None):
    args = {"key": key, "interrupt": True}
    if text is not None:
        args["text"] = text
    return ai(
        call("find_sessions", {"query": "stuck"}, cid="f"),
        call("send_nudge", args, cid="n"),
        answer("Stopped."),
    )


@pytest.fixture
def running(monkeypatch):
    """The session reports an operator turn running; interrupt requests are recorded."""
    state = {"active": "turn-running", "calls": [], "fail": None}
    real = runtime.snapshot

    async def snap(key):
        out = await real(key)
        return {**out, "active_turn": state["active"]} if state["active"] else out

    async def interrupt(key, *, operation_id, turn_id, execution_admission=None):
        if state["fail"] is not None:
            raise state["fail"]
        # Every HQ stop carries its admission to the worker's claim (Hermes on #2135).
        assert execution_admission is not None and execution_admission.binding == "orchestrator"
        state["calls"].append({"key": key, "operation_id": operation_id, "turn_id": turn_id})
        return {"session_key": key, "interrupted": True}

    monkeypatch.setattr(runtime, "snapshot", snap)
    monkeypatch.setattr(runtime, "interrupt", interrupt)
    return state


def test_interrupt_stops_the_exact_active_turn_then_sends_the_text(
    app, ai, api_session, project, running
):
    script = _interrupt(ai, api_session, "Stop and summarise what you have so far.")
    app.ask(app.new(), "stop it and ask for a summary")
    res = _result(script)
    assert res["sent"] is True and res["detail"].startswith("stopped the running turn")
    (rec,) = _ledger_for(api_session)
    assert running["calls"] == [
        {
            "key": api_session,
            "operation_id": mission_structured.interrupt_operation_id(rec["id"]),
            "turn_id": "turn-running",
        }
    ]
    assert _users(project).count("Stop and summarise what you have so far.") == 1


def test_interrupt_alone_sends_nothing_after_the_stop(app, ai, api_session, project, running):
    before = list(_users(project))
    script = _interrupt(ai, api_session)
    app.ask(app.new(), "stop it")
    assert _result(script)["sent"] is True
    assert len(running["calls"]) == 1 and _users(project) == before
    (rec,) = _ledger_for(api_session)
    assert rec["detail"] == "stopped the running turn" and rec["answer"] == ""


def test_interrupting_an_idle_session_stops_nothing_and_says_so(app, ai, api_session, running):
    running["active"] = None
    script = _interrupt(ai, api_session)
    app.ask(app.new(), "stop it")
    assert running["calls"] == []
    assert "nothing was running" in _result(script)["detail"]


def test_an_interrupt_of_unknown_outcome_sends_no_text(app, ai, api_session, project, running):
    running["fail"] = runtime.StructuredError(502, "worker went away")
    script = _interrupt(ai, api_session, "Then do this instead.")
    app.ask(app.new(), "stop it and redirect")
    res = _result(script)
    assert res["sent"] == "unknown" or res["sent"] is False
    assert "Then do this instead." not in _users(project)
    (rec,) = _ledger_for(api_session)
    assert rec["state"] == "indeterminate" and "could not be stopped" in rec["detail"]


def test_an_api_session_that_cannot_be_read_is_refused(app, ai, api_session, monkeypatch):
    async def unreadable(key):
        raise runtime.StructuredError(503, "worker down")

    monkeypatch.setattr(runtime, "snapshot", unreadable)
    script = _nudge(ai, api_session)
    app.ask(app.new(), "nudge it")
    assert "cannot read that session" in _result(script)["error"]
    assert _ledger_for(api_session) == []


def test_a_shell_still_takes_no_input_from_hq(app, ai, fleet):
    fleet["cards"].append(_card(SHELL, "shell"))
    script = _nudge(ai, SHELL)
    cid = app.new()
    app.ask(cid, "nudge the shell")
    assert "does not take input from HQ" in _result(script)["error"]
    assert _ledger_for(SHELL) == []


# ---- the proposal budget -------------------------------------------------------------------------


def test_refused_sends_do_not_spend_the_turns_budget(app, ai, api_session, fleet):
    """The 2026-11-12 turn: refusals first, then the one session that can take a nudge."""
    fleet["cards"].append(_card(SHELL, "shell"))
    n = ask_orchestrator.MAX_SENDS_PER_TURN
    # Each a fresh attempt (#2113: ten IDENTICAL refused sends would be a loop and wrapped up).
    refused = [call("send_nudge", {"key": SHELL, "text": f"go {i}"}, cid=f"r{i}") for i in range(n)]
    script = ai(
        call("find_sessions", {"query": "stuck"}, cid="f"),
        *refused,
        call("send_nudge", {"key": api_session, "text": TEXT}, cid="ok"),
        answer("done"),
    )
    app.ask(app.new(), "nudge everything")
    results = [json.loads(r["content"]) for r in script.tool_results(n + 2)]
    assert all("does not take input" in r["error"] for r in results[1 : n + 1])
    assert results[-1].get("sent") is True
    assert len(_ledger_for(api_session)) == 1


# --- #2008: HQ's own send reaches an API session that skips permission prompts -------------------


@pytest.fixture
def bypass_session(host, project, fleet):
    """An API session created to skip permission prompts, started, in HQ's fleet."""

    async def make():
        snap = await runtime.create_session(ENGINE, str(project), operation_id=ident(), bypass=True)
        await runtime.start_session(snap["session_key"])
        return snap["session_key"]

    key = asyncio.run(make())
    fleet["cards"].append(_card(key, ENGINE))
    return key


def test_hq_s_nudge_reaches_a_session_that_skips_permission_prompts(
    app, ai, bypass_session, project
):
    """The operator's call of 2026-11-13: "nothing was sent: guarded work can't run in a session
    that skips permission prompts". The Commander asked HQ for the message in that turn; the
    operator's approval of this is on #2008."""
    script = _nudge(ai, bypass_session)
    app.ask(app.new(), "nudge the stuck sessions")
    assert _result(script)["sent"] is True
    assert _users(project).count(TEXT) == 1
    (rec,) = _ledger_for(bypass_session)
    assert rec["state"] == "delivered"
    assert "skips permission prompts" in rec["detail"]  # said on the record


def test_an_unattended_send_into_that_session_is_still_refused(bypass_session, project):
    """A mission's, an automation's or the supervisor's send carries no `attended`: refused as
    before, and nothing is written."""

    async def go():
        return await mission_structured.send(
            action_id=ident(),
            key=bypass_session,
            text="UNATTENDED",
            mission_id=None,
            check=lambda: (True, ""),
            precondition=None,
            autonomous=False,
        )

    state, detail = asyncio.run(go())
    assert state == "stale" and "skips permission prompts" in detail
    assert "UNATTENDED" not in _users(project)


def test_the_same_send_attended_is_delivered_and_says_so(bypass_session, project):
    async def go():
        return await mission_structured.send(
            action_id=ident(),
            key=bypass_session,
            text="ATTENDED",
            mission_id=None,
            check=lambda: (True, ""),
            precondition=None,
            autonomous=False,
            attended=True,
        )

    state, detail = asyncio.run(go())
    assert state == "delivered" and detail.endswith("(this session skips permission prompts)")
    assert _users(project).count("ATTENDED") == 1


def test_an_attended_send_is_still_refused_when_its_authority_is_withdrawn(bypass_session, project):
    async def go():
        return await mission_structured.send(
            action_id=ident(),
            key=bypass_session,
            text="WITHDRAWN",
            mission_id=None,
            check=lambda: (False, "session assistance left Suggest/Yolo before the write"),
            precondition=None,
            autonomous=False,
            attended=True,
        )

    state, _detail = asyncio.run(go())
    assert state != "delivered"
    assert "WITHDRAWN" not in _users(project)


def test_a_session_that_asks_first_says_nothing_about_skipping(app, ai, api_session, project):
    script = _nudge(ai, api_session)
    app.ask(app.new(), "nudge the stuck sessions")
    assert _result(script)["sent"] is True
    (rec,) = _ledger_for(api_session)
    assert "skips permission prompts" not in (rec.get("detail") or "")


def test_only_hq_s_send_and_the_operator_s_tap_are_attended():
    """Every other caller of `actuator.deliver` leaves `attended` unset: found by reading the
    source, so a new caller that sets it shows up here."""
    import pathlib
    import re

    src = pathlib.Path(ask_orchestrator.__file__).parent
    setters = sorted(
        p.name
        for p in src.rglob("*.py")
        if re.search(r"attended\s*=\s*True", p.read_text(encoding="utf-8"))
    )
    assert setters == ["ask_orchestrator.py"]


def test_a_turn_replaced_before_the_stop_is_not_stopped_and_gets_no_text(
    app, ai, api_session, project, running, monkeypatch
):
    """The turn HQ targeted ended and ANOTHER started before the stop request (Hermes on #1960):
    that one was never the Commander's to stop, so nothing is stopped and nothing is sent."""

    async def replaced(key, *, operation_id, turn_id, execution_admission=None):
        running["active"] = "turn-other"
        raise runtime.StructuredError(409, "not the active turn")

    monkeypatch.setattr(runtime, "interrupt", replaced)
    script = _interrupt(ai, api_session, "Then do this instead.")
    app.ask(app.new(), "stop it and redirect")
    assert _result(script)["sent"] is False
    assert "Then do this instead." not in _users(project)
    (rec,) = _ledger_for(api_session)
    assert rec["state"] == "stale" and "different turn" in rec["detail"]


def test_a_stop_refused_while_the_same_turn_still_runs_is_not_idle(
    app, ai, api_session, project, running, monkeypatch
):
    """Hermes on #2135: the worker answers 409 for a stop it could not send yet. If the SAME turn
    is still active on re-read, the turn did not end — the text must not follow."""

    async def not_yet(key, *, operation_id, turn_id, execution_admission=None):
        raise runtime.StructuredError(409, "interrupt could not be sent yet")

    monkeypatch.setattr(runtime, "interrupt", not_yet)
    script = _interrupt(ai, api_session, "Then do this instead.")
    app.ask(app.new(), "stop it and redirect")
    assert _result(script)["sent"] is False
    assert "Then do this instead." not in _users(project)
    (rec,) = _ledger_for(api_session)
    assert rec["state"] == "stale" and "could not be stopped" in rec["detail"]
