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
