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


def test_an_api_session_waiting_on_a_request_is_refused_and_nothing_is_written(
    app, ai, api_session, monkeypatch
):
    real = runtime.snapshot

    async def asking(key):
        snap = await real(key)
        return {**snap, "pending_requests": [{"id": "req-1", "kind": "approval"}]}

    monkeypatch.setattr(runtime, "snapshot", asking)
    script = _nudge(ai, api_session)
    cid = app.new()
    turn = app.ask(cid, "nudge it").json()["turn"]
    assert "approval or a question" in _result(script)["error"]
    assert _ledger_for(api_session) == [] and turn["proposals"] == []


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
    refused = [call("send_nudge", {"key": SHELL, "text": "go"}, cid=f"r{i}") for i in range(n)]
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
