"""Terminal sessions report to HQ (#1882, P1a of #1878): the report capability, the store, Needs
you, the decision card's answer and its authority at the write.

What is pinned is what the Commander relies on: through the tool an agent reports only as
ITSELF; a report token and a secrets token never open each other's broker; a report's words never
reach a notification row, a push, a dedupe key or an episode key; an option tap types exactly
``Decision: <option>`` once; a report that moved on — replaced, dismissed, its launch restarted —
types NOTHING, whether it moves after the claim or under the write lock; a report takes one answer
(a second tap after one that may have been typed types nothing); a card is never offered as
actionable when every tap would be refused; a mission-held report opens no episode and wakes its
mission's supervisor.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import io
import json
import os
import pathlib
import socket
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    agent_reports,
    agent_tools,
    ask_orchestrator,
    hq_report_broker,
    hq_report_mcp,
    missions,
    needs_you,
    needs_you_notify,
    notifications,
    orchestrator,
    orchestrator_ledger,
    prefs,
    pulse,
    review,
    scrollback,
    secrets_broker,
    session_input,
    webpush,
)
from agent_sessions.main import create_app
from agent_sessions.routes import pulse as pulse_routes

#: The real socket-identity read, before the `hq` fixture replaces it with a table.
REAL_MASTER_IDENTITY = hq_report_broker._master_identity
#: …and the real checked metadata read, before a test replaces it.
from agent_sessions import metadata as _metadata  # noqa: E402

REAL_LOAD_CHECKED = _metadata.load_checked
#: …and the real card curation, before the `world` fixture replaces it.
REAL_BUILD_CARDS = pulse.build_cards

A = "claude:aaaaaaaa-0000-4000-8000-00000000000a"
B = "claude:aaaaaaaa-0000-4000-8000-00000000000b"
SCREEN = "I stopped here.\n> "
#: A hostile string in every agent-written field: it must never reach a row, a push or a key.
SENTINEL = "ZZ-SENTINEL-7f3a"
CRED = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class Manifest:
    tools_mcp = agent_tools.CLAUDE_FLAG


class NoTools:
    tools_mcp = None


def _card(sid, *, flagged=False):
    return {
        "id": sid,
        "engine": "claude",
        "title": f"title {sid[-1]}",
        "cwd": "/work/a",
        "project": {"kind": "folder", "id": "p1", "name": "Alpha"},
        "last_activity": time.time() - 60,
        "intervention_required": flagged,
        "intervention_reason": "",
        "ai_summary": "s",
    }


@pytest.fixture(autouse=True)
def _own_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NEEDS_YOU_DISMISSED", str(tmp_path / "dismissed.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_REPORTS", str(tmp_path / "reports" / "r.json"))
    # The switch is OFF by default (PR #1884, the Commander's decision); this module exercises the
    # tool, so it turns it on for every test, in the test's own prefs file.
    prefs.set_hq_reports(True)


@pytest.fixture
def hq(tmp_path, monkeypatch):
    """A serving report broker on a private runtime root; liveness by table."""
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(hq_report_broker, "_server_running", True)
    live: set[str] = {A, B}
    #: The master socket identity each live session shows now; a relaunch changes it.
    masters: dict[str, str] = {}
    #: The capability ids each session's master carries in its own environment
    #: (`ENV_CAP`); absent: the asking capability's own (its launch's master); ``"real"``: ask
    #: the kernel (`_master_carries`).
    environs: dict[str, object] = {}

    def identity(phys):
        if phys not in live:
            return "absent", None
        return masters.get(phys, f"master-1:{phys}"), runtime / "fake.sock"

    def liveness(conn, cap):
        # Like `_liveness_impl` without the probe: bind on the first live answer (`_bind`, the
        # real rule), and a bound capability is alive only on the master it bound to.
        ident, _sock = identity(cap["phys_key"])
        if ident == "absent":
            return hq_report_broker.DEAD
        if cap["master"] is None:
            carried = environs.get(cap["phys_key"])
            if carried == "real":
                carries = hq_report_broker._master_carries(cap["phys_key"], cap["id"])
            else:
                carries = True if carried is None else cap["id"] in carried
            return hq_report_broker._bind(conn, cap, ident, carries)
        return hq_report_broker.ALIVE if cap["master"] == ident else hq_report_broker.DEAD

    monkeypatch.setattr(hq_report_broker, "_master_identity", identity)
    monkeypatch.setattr(hq_report_broker, "_liveness", liveness)
    return {"live": live, "runtime": runtime, "masters": masters, "environs": environs}


def _launch(key=A) -> tuple[dict, str]:
    env = hq_report_broker.launch_env(key, key, engine="claude", manifest=Manifest())
    return env, open(env[hq_report_broker.ENV_TOKEN_FILE]).read()


DECISION = {
    "state": "needs_decision",
    "summary": "Both token stores pass the suite.",
    "question": "Where should session tokens live?",
    "options": ["Postgres", "Redis"],
}


def _file(token, report=None):
    return hq_report_broker.handle(token, "report", dict(report or DECISION))


# ---- the capability --------------------------------------------------------------------------


def test_a_launch_gets_a_report_capability_as_paths_only(hq):
    env, token = _launch()
    assert set(env) == set(hq_report_broker.ENV_KEYS)
    token_file = env[hq_report_broker.ENV_TOKEN_FILE]
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(token_file)).st_mode) == 0o700
    assert token not in json.dumps(env)
    assert token.encode() not in hq_report_broker.db_path().read_bytes()
    srv = agent_tools.hq_report_server(env)
    assert srv.name == "battlelab_hq"
    assert srv.args == ("-I", "-m", "agent_sessions.hq_report_mcp")  # literal argv, no shell
    # Every payload rides argv (world-readable /proc/<pid>/cmdline): paths only, never a token.
    for payload in (
        agent_tools.claude_mcp_config([srv]),
        " ".join(agent_tools.codex_overrides([srv])),
        agent_tools.copilot_mcp_config([srv]),
        agent_tools.opencode_config_content([srv], None),
    ):
        assert token not in payload and "battlelab_hq" in payload
    assert json.loads(agent_tools.copilot_mcp_config([srv]))["mcpServers"]["battlelab_hq"][
        "tools"
    ] == ["hq_report"]
    launch = agent_tools.terminal_launch(Manifest(), {}, {}, hq_env=env)
    assert "battlelab_hq" in launch.suffix[-1]


@pytest.mark.parametrize("why", ["switch off", "no tool channel", "not serving"])
def test_nothing_is_minted_without_the_switch_a_tool_channel_or_the_broker(hq, monkeypatch, why):
    manifest = Manifest()
    if why == "switch off":
        prefs.set_hq_reports(False)
    elif why == "no tool channel":
        manifest = NoTools()
    else:
        monkeypatch.setattr(hq_report_broker, "_server_running", False)
    assert hq_report_broker.launch_env(A, A, engine="claude", manifest=manifest) == {}
    assert list(hq_report_broker.tokens_root().glob("*/.token")) == []


def test_the_switch_is_off_by_default_and_only_an_explicit_true_turns_it_on(tmp_path):
    path = tmp_path / "fresh-prefs.json"
    assert prefs.get_hq_reports(path) is False  # a fresh install: no tool for any launch
    prefs.set_hq_reports(True, path)
    assert prefs.get_hq_reports(path) is True
    prefs.set_hq_reports(False, path)
    assert prefs.get_hq_reports(path) is False


def test_a_fresh_install_mints_no_capability(hq, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "untouched-prefs.json"))
    assert hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest()) == {}
    assert list(hq_report_broker.tokens_root().glob("*/.token")) == []


def test_a_token_files_only_for_its_own_session(hq):
    _env_a, token_a = _launch(A)
    _env_b, token_b = _launch(B)
    assert _file(token_a) == {"ok": True, "state": "needs_decision"}
    assert agent_reports.current(A)["summary"] == DECISION["summary"]
    assert agent_reports.current(B) is None
    # The tool takes no session: naming one is not a field the report has.
    for extra in ({"session": B}, {"session_key": B}, {"key": B}):
        assert _file(token_b, {**DECISION, **extra}) == {"ok": False, "error": "bad_request"}
    assert agent_reports.current(B) is None
    assert "session" not in json.dumps(hq_report_mcp.TOOLS[0]["inputSchema"]["properties"])


def test_a_report_token_and_a_secrets_token_never_open_each_others_broker(hq, monkeypatch):
    monkeypatch.setattr(secrets_broker, "_server_running", True)
    monkeypatch.setattr(secrets_broker, "_project_for", lambda key, cwd: "atlas")
    monkeypatch.setattr(secrets_broker, "_liveness", lambda conn, cap: secrets_broker.ALIVE)
    _env, report_token = _launch(A)
    senv = secrets_broker.launch_env(A, A, "/w", engine="claude", kind="terminal")
    secrets_token = open(senv[secrets_broker.ENV_TOKEN_FILE]).read()
    assert secrets_broker.handle(report_token, "list") == {
        "ok": False,
        "error": "unknown_capability",
    }
    assert hq_report_broker.handle(secrets_token, "report", DECISION) == {
        "ok": False,
        "error": "unknown_capability",
    }
    # …and each still works on its own broker.
    assert secrets_broker.handle(secrets_token, "list")["ok"] is True
    assert _file(report_token)["ok"] is True
    # Only one verb.
    assert hq_report_broker.handle(report_token, "list") == {"ok": False, "error": "bad_request"}


def test_a_dead_or_unconfirmed_launch_files_nothing(hq, monkeypatch):
    _env, token = _launch(A)
    hq["live"].discard(A)
    assert _file(token) == {"ok": False, "error": "launch_ended"}
    assert agent_reports.current(A) is None
    _env, token = _launch(B)
    monkeypatch.setattr(hq_report_broker, "_liveness", lambda c, cap: hq_report_broker.UNKNOWN)
    assert _file(token) == {"ok": False, "error": "launch_unconfirmed"}


def test_the_terminal_route_mints_on_launch_and_never_on_attach(
    auth_cfg, fake_jsonl, monkeypatch, engine_bin, hq
):
    import uuid

    from starlette.websockets import WebSocketDisconnect

    from agent_sessions import engines, owner, ptybridge, relaunch, scopedspawn, sessions, webterm

    monkeypatch.setattr(
        engines, "launchable_bin", lambda p: f"/usr/bin/{p.engine_id}" if p else None
    )

    async def fake_run(ws, argv, **kwargs):
        await ws.close(code=1000)

    action = {"now": sessions.LAUNCH}
    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(owner, "takeover_enabled", lambda: False)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (action["now"], None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    argvs = []
    monkeypatch.setattr(
        ptybridge, "launch_argv", lambda **kw: argvs.append(kw["launch_argv"]) or ["/bin/true"]
    )
    monkeypatch.setattr(ptybridge, "attach_argv", lambda **kw: ["/bin/true"])
    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    engine_bin("claude")
    asked = []
    real = hq_report_broker.launch_env
    monkeypatch.setattr(
        hq_report_broker, "launch_env", lambda *a, **k: asked.append(a) or real(*a, **k)
    )
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    cookie = c.cookies.get("agent_sessions")
    headers = {"Origin": auth_cfg.origin, "Cookie": f"agent_sessions={cookie}"}
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    cwd = next(r["cwd"] for r in rows if r["id"] == "claude:11111111-1111-1111-1111-111111111111")

    def connect(url):
        try:
            with c.websocket_connect(url, headers=headers) as ws:
                while (msg := ws.receive()).get("type") != "websocket.close":
                    pass
                return msg.get("code")
        except WebSocketDisconnect as e:
            return e.code

    assert connect(f"/ws/term/claude:{uuid.uuid4()}?new=1&cwd={cwd}") == 1000
    prefs.set_hq_reports(False)
    assert connect(f"/ws/term/claude:{uuid.uuid4()}?new=1&cwd={cwd}") == 1000
    action["now"] = sessions.ATTACH
    assert connect("/ws/term/claude:11111111-1111-1111-1111-111111111111") == 1000
    assert len(asked) == 2 and len(argvs) == 2  # the attach asked nothing and built no argv
    on, off = argvs
    server = json.loads(on[-1])["mcpServers"]["battlelab_hq"]
    assert set(server["env"]) >= hq_report_broker.ENV_KEYS
    assert server["args"] == ["-I", "-m", "agent_sessions.hq_report_mcp"]
    assert "battlelab_hq" not in " ".join(off)  # the switch off: nothing for that launch


# ---- the store ---------------------------------------------------------------------------------


def _store_file(**kw):
    return agent_reports.file(
        kw.pop("report", DECISION),
        session_key=kw.pop("session_key", A),
        phys_key=kw.pop("phys_key", A),
        engine="claude",
        generation=kw.pop("generation", "g1"),
        **kw,
    )


def test_rate_limits_one_per_30s_and_20_an_hour():
    t = 1_000_000.0
    _store_file(now=t)
    with pytest.raises(agent_reports.Refused) as e:
        _store_file(now=t + 29)
    assert e.value.code == "rate_limited"
    for i in range(1, 20):
        _store_file(now=t + 31 * i)
    with pytest.raises(agent_reports.Refused):
        _store_file(now=t + 31 * 20)  # the 21st within the hour
    _store_file(now=t + 3601)  # the window moved on
    assert hq_report_mcp.MEANING["rate_limited"].startswith("refused: too many reports — wait")


@pytest.mark.parametrize("field", ["summary", "question", "options"])
def test_a_credential_shaped_report_stores_nothing(field):
    bad = dict(DECISION)
    bad[field] = [CRED[:80]] if field == "options" else CRED
    with pytest.raises(agent_reports.Refused) as e:
        _store_file(report=bad)
    assert e.value.code in ("credential", "too_long")
    assert not agent_reports.store_path().exists()


def test_a_known_secret_in_an_option_refuses_the_whole_report():
    from agent_sessions import template_secrets

    planted = "hunter2-PLANTED-template-value-9f3k"
    template_secrets.register_typed([planted])
    with pytest.raises(agent_reports.Refused) as e:
        _store_file(report={**DECISION, "options": ["Postgres", f"use {planted}"]})
    assert e.value.code == "credential"
    assert not agent_reports.store_path().exists()


@pytest.mark.parametrize(
    "report",
    [
        {"state": "nope", "summary": "x"},
        {"state": "progress", "summary": ""},
        {"state": "progress", "summary": "x", "question": "q?"},
        {"state": "blocked", "summary": "x", "options": ["a"]},
        {**DECISION, "options": ["a", "b", "c", "d", "e"]},
        {**DECISION, "options": ["same", "same"]},
        {**DECISION, "summary": "x" * 281},
        {**DECISION, "options": ["y" * 81]},
        # A decision the Commander can't make (Hermes on #1884): no question, or no options.
        {k: v for k, v in DECISION.items() if k != "question"},
        {**DECISION, "question": "   "},
        {k: v for k, v in DECISION.items() if k != "options"},
        {**DECISION, "options": []},
    ],
)
def test_malformed_reports_are_refused(report):
    with pytest.raises(agent_reports.Refused):
        _store_file(report=report)


def test_control_and_format_characters_are_stripped():
    rep = _store_file(report={**DECISION, "summary": "a\x1b[31mb‮c​\n  d"})
    assert rep["summary"] == "a[31mbc d"


def test_an_unreadable_store_is_refused_and_never_rewritten():
    path = agent_reports.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(agent_reports.Refused) as e:
        _store_file()
    assert e.value.code == "store_unavailable"
    with pytest.raises(agent_reports.StoreUnreadable):
        agent_reports.end(A, "dismissed")
    assert path.read_text() == "{not json"
    with pytest.raises(agent_reports.StoreUnreadable):
        agent_reports.current_checked()


def test_history_is_bounded_and_a_newer_report_replaces_the_current_one():
    t = 2_000_000.0
    first = _store_file(now=t)
    second = _store_file(now=t + 31)
    assert agent_reports.current(A)["id"] == second["id"]
    (old,) = agent_reports.history(A, now=t + 31)
    assert old["id"] == first["id"] and old["ended_by"] == "replaced"
    for i in range(2, 40):
        _store_file(now=t + 3700 * i)
    assert len(agent_reports.history(A, now=t + 3700 * 39)) == agent_reports.HISTORY_MAX


# ---- the world Needs you reads -----------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    state = {"cards": [_card(A)], "held": set(), "screen": SCREEN, "live": True}
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [dict(c) for c in state["cards"]])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: set(state["held"]))
    real_observed = orchestrator.observed_screen

    def observed(key, strict=False, **kw):
        out = real_observed(key, **kw)
        out["permission"] = None
        return out

    monkeypatch.setattr(orchestrator, "observed_screen", observed)
    monkeypatch.setattr(scrollback, "live_tail_text", lambda key, n=4000: state["screen"])
    monkeypatch.setattr(session_input, "is_live", lambda key: state["live"])
    monkeypatch.setattr(review, "last_words", lambda key, n=1500: "")
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    return state


@pytest.fixture
def pushes(monkeypatch):
    sent: list[str] = []
    notifications._write(
        notifications._subs_path(),
        [{"id": "s1", "endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}}],
    )
    monkeypatch.setattr(
        webpush, "send", lambda sub, payload, **k: sent.append(bytes(payload).decode())
    )
    return sent


def _hostile():
    return {
        "state": "needs_decision",
        "summary": f"summary {SENTINEL}",
        "question": f"question {SENTINEL}?",
        "options": [f"one {SENTINEL}", f"two {SENTINEL}"],
    }


def test_a_report_enters_needs_you_with_a_fixed_kind_and_its_words_reach_no_row_push_or_key(
    hq, world, pushes
):
    _env, token = _launch(A)
    assert _file(token, _hostile())["ok"]
    out = pulse_routes.build_needs_you(1, strict=True)
    (row,) = out["rows"]
    assert row["kind"] == "report_decision" and row["report"]["options"][0] == f"one {SENTINEL}"
    assert row["report_answer"] == {"available": True, "reason": ""}
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 0}
    bell = [n for n in notifications.listing()["notifications"] if n.get("needs_you")]
    assert bell[0]["reason"] == "The agent asks you to decide"
    assert SENTINEL not in json.dumps(bell)
    assert pushes and all(SENTINEL not in p for p in pushes)
    raw = notifications._notifications_path().read_bytes()
    assert SENTINEL.encode() not in raw  # rows, episodes and their keys
    # Blocked has its own fixed label.
    agent_reports.end(A, "dismissed")
    agent_reports.file(
        {"state": "blocked", "summary": f"blocked {SENTINEL}"},
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=agent_reports.history(A)[0]["generation"],
        now=time.time() + 60,
    )
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["kind"] == "report_blocked" and "options" not in row["report"]


def test_progress_and_done_never_enter_needs_you(hq, world):
    _env, token = _launch(A)
    assert _file(token, {"state": "progress", "summary": "3 of 4 green"})["ok"]
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []
    assert agent_reports.latest_notes()[A]["summary"] == "3 of 4 green"


def test_an_unreadable_store_fails_the_strict_read_but_not_the_list(hq, world):
    path = agent_reports.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")
    with pytest.raises(needs_you.ReadIncomplete):
        pulse_routes.build_needs_you(1, strict=True)
    assert pulse_routes.build_needs_you(1)["rows"] == []


def test_a_mission_held_report_opens_no_episode_and_wakes_its_supervisor(
    hq, world, pushes, monkeypatch
):
    from agent_sessions import mission_supervisor, mission_supervisor_loop

    mid = "msn_" + "a" * 32
    world["held"] = {A}
    mission_supervisor_loop._take_report_wakes()
    _env, token = _launch(A)
    assert _file(token)["ok"]
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    assert pushes == []
    monkeypatch.setattr(mission_supervisor_loop, "_enabled", lambda: True)
    monkeypatch.setattr(mission_supervisor_loop, "_running_held_sessions", lambda: {A: mid})
    monkeypatch.setattr(mission_supervisor, "session_rest", lambda sk, p: None)
    assert asyncio.run(mission_supervisor_loop.watch()) == [mid]
    assert asyncio.run(mission_supervisor_loop.watch()) == []  # one wake per report


# ---- the decision card: an option tap ------------------------------------------------------------


class App:
    def __init__(self, cfg):
        self.c = TestClient(create_app(cfg), base_url="https://testserver")
        r = self.c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": cfg.origin},
        )
        assert r.status_code == 303
        self.hdr = {"X-CSRF-Token": self.c.get("/api/config").json()["csrf"], "Origin": cfg.origin}

    def answer(self, report_id, option, key=A):
        return self.c.post(
            f"/api/needs-you/{key}/report/answer",
            json={"report_id": report_id, "option": option},
            headers=self.hdr,
        )

    def dismiss(self, report_id=None, key=A):
        return self.c.post(
            f"/api/needs-you/{key}/report/dismiss",
            json={"report_id": report_id} if report_id else {},
            headers=self.hdr,
        )


@pytest.fixture
def app(auth_cfg):
    return App(auth_cfg)


@pytest.fixture
def writer(monkeypatch):
    """The PTY writer: the delivery's own final guard and precondition first, then the bytes."""
    sent: list[bytes] = []
    hooks = {"before_guard": None, "calls": 0}

    def send_input(phys, payload, *, precondition=None, final_guard=None, **kw):
        hooks["calls"] += 1
        if hooks["before_guard"] is not None:
            hooks["before_guard"]()
        if final_guard is not None:
            ok, why = final_guard()
            if not ok:
                return session_input.Outcome("refused", why)
        if precondition is not None:
            ok, why = precondition()
            if not ok:
                return session_input.Outcome("stale", why)
        sent.append(payload)
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)
    return {"sent": sent, "hooks": hooks}


def _actions(key=A):
    return [r for r in orchestrator_ledger.latest_by_id().values() if r.get("session_id") == key]


def test_an_option_tap_types_exactly_the_decision_once_and_ends_the_report(hq, world, app, writer):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    r = app.answer(rep["id"], 2)
    assert r.status_code == 200, r.text
    assert writer["sent"] == [session_input.bracketed_paste("Decision: Redis")]
    (act,) = _actions()
    assert act["state"] == "delivered" and act["source"] == "report"
    assert act["report_id"] == rep["id"] and act["verb"] == "answer"
    assert act["answer"] == "Decision: Redis"
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "answered"
    # A second tap on the same (now ended) report sends nothing.
    assert app.answer(rep["id"], 1).status_code == 409
    assert len(writer["sent"]) == 1


def test_a_tap_names_the_option_by_number_and_the_words_come_from_the_report(
    hq, world, app, writer
):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert app.answer(rep["id"], 3).status_code == 422
    assert app.answer(rep["id"], 0).status_code == 422
    assert app.answer("someotherid", 1).status_code == 409
    bad = app.c.post(
        f"/api/needs-you/{A}/report/answer",
        json={"report_id": rep["id"], "option": 1, "text": "rm -rf"},
        headers=app.hdr,
    )
    assert bad.status_code == 422
    assert writer["sent"] == [] and _actions() == []


def _race(kind: str):
    """Move the report on: replace it, dismiss it, or restart the launch."""
    if kind == "replaced":
        cur = agent_reports.current(A)
        agent_reports.file(
            {**DECISION, "summary": "a newer report"},
            session_key=A,
            phys_key=A,
            engine="claude",
            generation=cur["generation"],
            now=time.time() + 120,
        )
    elif kind == "dismissed":
        agent_reports.end(A, "dismissed")
    elif kind == "restarted":
        hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest())
    else:  # the launch generation changes while the report stays (a master that moved)
        conn = sqlite3.connect(hq_report_broker.db_path())
        conn.execute("UPDATE caps SET revoked_at = 1 WHERE phys_key = ?", (A,))
        conn.commit()
        conn.close()


RACES = ["replaced", "dismissed", "restarted", "generation"]


@pytest.mark.parametrize("kind", RACES)
def test_a_report_that_moves_on_after_the_claim_types_nothing(
    hq, world, app, writer, monkeypatch, kind
):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    real_claim = orchestrator_ledger.claim

    def claim(*a, **k):
        out = real_claim(*a, **k)
        _race(kind)
        return out

    monkeypatch.setattr(orchestrator_ledger, "claim", claim)
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409, r.text
    # Refused right after the claim: the writer is never even asked.
    assert writer["sent"] == [] and writer["hooks"]["calls"] == 0
    (act,) = _actions()
    assert act["state"] == "stale"


@pytest.mark.parametrize("kind", RACES)
def test_a_report_that_moves_on_under_the_write_lock_types_nothing(hq, world, app, writer, kind):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    writer["hooks"]["before_guard"] = lambda: _race(kind)
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409, r.text
    assert writer["sent"] == []
    (act,) = _actions()
    assert act["state"] == "stale"


def test_the_report_authority_rides_the_in_fence_fingerprint(hq, world):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    check, state = actuator._report_authority(
        {
            "source": "report",
            "report_id": rep["id"],
            "report_generation": rep["generation"],
            "authority": {"physical_key": A},
        }
    )
    before = state()
    assert check() == (True, "")
    _race("dismissed")
    assert state() != before and check()[0] is False
    # A report answer missing its binding is refused, never delivered on the screen alone.
    check, _state = actuator._report_authority({"source": "report", "authority": {}})
    assert check()[0] is False


@pytest.mark.parametrize("kind", ["replaced", "dismissed", "restarted"])
def test_a_report_change_is_ordered_against_byte_one(hq, world, kind):
    """Hermes on #1882: the in-fence fingerprint re-read orders a report change only if the change
    commits under the same cross-process fence. Holding it here stands in for a writer between
    that re-read and byte one: the change must WAIT, so it lands either before the re-read (the
    fingerprint differs and the answer settles `stale`, the test above) or after the bytes —
    never in between."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    before = agent_reports.authority_state(A)
    done = threading.Event()
    errors: list[BaseException] = []

    def change():
        try:
            _race(kind)
        except BaseException as e:  # noqa: BLE001 — surfaced below
            errors.append(e)
        done.set()

    with session_input.mutation_fence():
        t = threading.Thread(target=change)
        t.start()
        interleaved = done.wait(1.0)
        # Still the state the writer re-read: nothing committed while the fence was held.
        assert agent_reports.authority_state(A) == before
    t.join(15)
    assert not interleaved, f"a {kind} report change committed while a writer held the fence"
    assert not errors, errors
    assert agent_reports.authority_state(A) != before


def test_a_launch_inside_the_launch_fence_revokes_without_taking_it_again(hq, world):
    """An unattended dispatch mints inside `session_input.launch_fence`: the revocation of the
    session's earlier capability must not wait out the fence's own budget (fresh-fd flock)."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    t0 = time.monotonic()
    with session_input.launch_fence(timeout=5) as _epoch:
        env = hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest(), fenced=True)
    # Taking the fence a second time would wait out its WHOLE budget and then fail (no
    # capability, the report not ended); anything short of the budget proves it was not taken.
    assert time.monotonic() - t0 < session_input.MUTATION_FENCE_BUDGET_S
    assert env and agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "launch_ended"


def test_a_busy_fence_files_nothing_and_says_try_again(hq, world, monkeypatch):
    _env, token = _launch(A)

    @contextlib.contextmanager
    def busy():
        raise session_input.AuthorityFenceBusy("held elsewhere")
        yield  # pragma: no cover

    monkeypatch.setattr(session_input, "mutation_fence", busy)
    assert _file(token) == {"ok": False, "error": "store_unavailable"}
    assert agent_reports.current(A) is None


def test_dismiss_ends_the_report_and_types_nothing(hq, world, app, writer):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert app.dismiss("not-this-one").json() == {"dismissed": False}
    assert agent_reports.current(A) is not None
    assert app.dismiss(rep["id"]).json() == {"dismissed": True}
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "dismissed"
    assert writer["sent"] == [] and _actions() == []


# ---- the switch matrix -------------------------------------------------------------------------


@pytest.mark.parametrize("reports_on", [True, False], ids=["reports-on", "reports-off"])
@pytest.mark.parametrize("enabled", [True, False], ids=["orch-on", "orch-off"])
@pytest.mark.parametrize("autonomy", ["off", "suggest", "yolo"])
def test_the_switch_matrix(hq, world, app, writer, reports_on, enabled, autonomy):
    prefs.set_hq_reports(reports_on)
    prefs.set_orchestrator({"enabled": enabled, "autonomy": autonomy})
    env = hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest())
    if not reports_on:
        # Reports are filed only with the switch on: the launch has no capability at all.
        assert env == {} and agent_reports.current(A) is None
        assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []
        return
    assert _file(open(env[hq_report_broker.ENV_TOKEN_FILE]).read())["ok"]
    rep = agent_reports.current(A)
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    deliverable = enabled and autonomy != "off"
    assert row["report_answer"]["available"] is deliverable
    r = app.answer(rep["id"], 1)
    if deliverable:
        assert r.status_code == 200, r.text
        assert writer["sent"] == [session_input.bracketed_paste("Decision: Postgres")]
    else:
        # Never a card whose tap always refuses: it is offered disabled, with the reason, and a
        # tap anyway mints nothing and types nothing.
        assert row["report_answer"]["reason"] == pulse_routes.REPORT_ANSWER_OFF
        assert r.status_code == 409 and r.json()["detail"] == pulse_routes.REPORT_ANSWER_OFF
        assert writer["sent"] == [] and _actions() == []


def test_an_engine_that_takes_no_typed_input_shows_its_report_disabled(hq, world, monkeypatch):
    from agent_sessions import engines

    monkeypatch.setattr(engines, "supports_orchestrator_input", lambda prov: False)
    assert pulse_routes.report_answerable({"id": A}) == {
        "available": False,
        "reason": pulse_routes.REPORT_ANSWER_NOT_ACTUABLE,
    }


# ---- end conditions ----------------------------------------------------------------------------


def test_the_launch_gone_ends_the_report_at_the_next_sync(hq, world):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    hq["live"].discard(A)
    needs_you_notify.sync_once()
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "launch_ended"


def test_a_relaunch_ends_the_earlier_launchs_report(hq):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    _launch(A)
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "launch_ended"
    assert _file(token) == {"ok": False, "error": "unknown_capability"}


def test_archive_ends_the_report_and_revokes_the_capability(hq, monkeypatch):
    from agent_sessions import reaper, runtime_cleanup

    _env, token = _launch(A)
    assert _file(token)["ok"]

    async def terminate(*a, **k):
        return "gone"

    monkeypatch.setattr(reaper, "terminate_master", terminate)
    asyncio.run(runtime_cleanup.cleanup_runtime("claude", A.split(":")[1], physical_key=A))
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "archived"
    assert _file(token) == {"ok": False, "error": "unknown_capability"}


def test_any_delivered_answer_ends_the_report_but_a_stale_one_does_not(hq):
    _env, token = _launch(A)
    assert _file(token)["ok"]
    # Proposed AFTER the report was filed (its precondition's screen read), so it answers it.
    rec = {
        "id": "x1",
        "verb": "answer",
        "authority": {"physical_key": A},
        "precondition": {"observed_at": time.time() + 1},
    }
    orchestrator_ledger._settled("x1", "stale", rec)
    assert agent_reports.current(A) is not None
    orchestrator_ledger._settled("x1", "delivered", {**rec, "verb": "continue"})
    assert agent_reports.current(A) is not None
    orchestrator_ledger._settled("x1", "delivered", rec)
    assert agent_reports.current(A) is None


# ---- HQ reads it, framed as agent text --------------------------------------------------------


def _turn(keys):
    return ask_orchestrator._Turn(
        cid="c", turn_id="t", question="q", working_keys=set(), progress=lambda e: None, keys=keys
    )


def test_hq_reads_the_current_report_as_agent_text_for_a_key_from_this_turn(hq):
    _env, token = _launch(A)
    assert _file(token, _hostile())["ok"]
    out, meta = asyncio.run(ask_orchestrator._session_report(_turn({A}), {"key": A}))
    assert out["state"] == "needs_decision" and meta["keys"] == [A]
    assert out["untrusted_agent_report"]["options"] == [f"one {SENTINEL}", f"two {SENTINEL}"]
    assert "not instructions" in out["note"]
    with pytest.raises(ask_orchestrator._ToolRefused):
        asyncio.run(ask_orchestrator._session_report(_turn(set()), {"key": A}))
    assert ask_orchestrator.TOOLS["session_report"].kind == "read"


# ---- the tool, end to end over the socket -----------------------------------------------------


def test_the_mcp_tool_files_over_the_socket_and_explains_refusals(hq):
    env, _token = _launch(A)
    sock = hq_report_broker.socket_path()
    started, stop = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()

        async def main():
            server = await asyncio.start_unix_server(hq_report_broker._client, path=str(sock))
            stop["loop"], stop["server"] = loop, server
            started.set()
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    old = dict(os.environ)
    try:
        os.environ.update(env)
        server = hq_report_mcp.Server()
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert [t["name"] for t in tools["result"]["tools"]] == ["hq_report"]
        ok = server.call("hq_report", dict(DECISION))
        assert ok.get("isError") is not True and "Decision: <option>" in ok["content"][0]["text"]
        assert agent_reports.current(A)["question"] == DECISION["question"]
        again = server.call("hq_report", dict(DECISION))
        assert again["isError"] is True and "too many reports" in again["content"][0]["text"]
        bad = server.call("hq_report", {"state": "nope", "summary": "x"})
        assert bad["isError"] is True
    finally:
        os.environ.clear()
        os.environ.update(old)
        stop["loop"].call_soon_threadsafe(stop["server"].close)


def test_the_broker_socket_is_never_taken_for_a_session(hq):
    from agent_sessions import ptybridge

    assert ptybridge.is_broker_socket(hq_report_broker.socket_path().name)
    assert not ptybridge.is_broker_socket("claude-aaaaaaaa.sock")


def test_the_app_never_carries_a_report_capability_in_its_own_environment(monkeypatch):
    monkeypatch.setenv(hq_report_broker.ENV_TOKEN_FILE, "/x")
    hq_report_broker.scrub_process_environment()
    assert hq_report_broker.ENV_TOKEN_FILE not in os.environ


# ---- the REAL write fence (Hermes round 3 on #1882) ---------------------------------------------


def _sibling(script: str, *args: str) -> subprocess.Popen:
    """A second instance, as a real process: its own interpreter, the test's stores and lock dir."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE", "AGENT_SESSIONS_")) or k in _SIBLING_ENV
    }
    env["PYTHONPATH"] = "src"
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


_SIBLING_ENV = ("AGENT_SESSIONS_AGENT_REPORTS", "AGENT_SESSIONS_LOCK_DIR")


@pytest.mark.parametrize("kind", ["dismissed", "replaced"])
def test_a_SIBLING_report_change_in_the_write_window_sends_ZERO_bytes(
    hq, world, tmp_path, monkeypatch, kind
):
    """The real fence, not a fake writer: `session_input.send_input` on a real pty, with the
    answer's report authority as its in-fence fingerprint. A sibling instance takes the fence
    through the REAL report mutation (`agent_reports.end` / `file`), and commits inside the
    window after this writer's first read and before its byte one. The writer must either go
    first or see the change: here the sibling holds the fence first, so the re-read sees it,
    the outcome is `stale`, and nothing reaches the terminal."""
    import pty

    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    master, slave = pty.openpty()
    try:
        session_input.register_writer(A, master, threading.Lock(), "headless")
        _check, state = actuator._report_authority(
            {
                "source": "report",
                "report_id": rep["id"],
                "report_generation": rep["generation"],
                "authority": {"physical_key": A},
            }
        )
        sibling = _sibling(
            """
            import sys, time
            from agent_sessions import agent_reports, atomicjson
            real = atomicjson.atomic_write_json

            def slow(path, doc):
                print("held", flush=True)  # inside the fence and the store lock
                time.sleep(0.8)            # the writer is now waiting on the fence
                real(path, doc)

            agent_reports.atomic_write_json = slow
            phys, kind, gen = sys.argv[1], sys.argv[2], sys.argv[3]
            if kind == "dismissed":
                agent_reports.end(phys, "dismissed")
            else:
                agent_reports.file(
                    {"state": "blocked", "summary": "a newer report"},
                    session_key=phys, phys_key=phys, engine="claude", generation=gen,
                    now=time.time() + 120,
                )
            print("released", flush=True)
            """,
            A,
            kind,
            rep["generation"],
        )
        assert sibling.stdout.readline().strip() == "held", sibling.stderr.read()
        out = session_input.send_input(
            A, b"Decision: Postgres\r", require_quiet=False, policy_fingerprint=state
        )
        assert sibling.wait(timeout=15) == 0, sibling.stderr.read()
        assert out.state == "stale", f"expected a refusal, got {out.state}: {out.detail}"
        fcntl.fcntl(slave, fcntl.F_SETFL, os.O_NONBLOCK)
        try:
            got = os.read(slave, 4096)
        except BlockingIOError:
            got = b""
        assert got == b"", f"bytes reached the pty under a moved report: {got!r}"
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_an_UNCONTENDED_report_answer_still_reaches_the_pty(hq, world, tmp_path, monkeypatch):
    """The control: with no sibling, the same fingerprint lets the bytes through."""
    import pty

    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    master, slave = pty.openpty()
    try:
        session_input.register_writer(A, master, threading.Lock(), "headless")
        _check, state = actuator._report_authority(
            {
                "source": "report",
                "report_id": rep["id"],
                "report_generation": rep["generation"],
                "authority": {"physical_key": A},
            }
        )
        out = session_input.send_input(
            A, b"Decision: Postgres\r", require_quiet=False, policy_fingerprint=state
        )
        assert out.ok, f"{out.state}: {out.detail}"
        assert b"Decision: Postgres" in os.read(slave, 1024)
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


# ---- one answer per report (review on #1884) ----------------------------------------------------


def _outcome_writer(monkeypatch, outcomes: list):
    """A PTY writer that settles each call with the next scripted outcome; bytes only on
    ``delivered`` / ``aborted`` (a partial write)."""
    sent: list[bytes] = []

    def send_input(phys, payload, *, precondition=None, final_guard=None, **kw):
        if final_guard is not None:
            ok, why = final_guard()
            if not ok:
                return session_input.Outcome("refused", why)
        state = outcomes.pop(0) if outcomes else "delivered"
        if state in ("delivered", "aborted"):
            sent.append(payload)
        return session_input.Outcome(state, "scripted")

    monkeypatch.setattr(session_input, "send_input", send_input)
    return sent


@pytest.mark.parametrize(
    ("outcome", "settled"), [("indeterminate", "indeterminate"), ("aborted", "failed")]
)
def test_an_answer_that_may_have_been_typed_refuses_a_second_tap(
    hq, world, app, monkeypatch, outcome, settled
):
    """(a) indeterminate, (b) failed after the claim: the bytes may be on the pty, so the report
    is NOT ended (the agent may not have its answer) and a second tap types nothing."""
    sent = _outcome_writer(monkeypatch, [outcome])
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    app.answer(rep["id"], 1)
    (act,) = _actions()
    assert act["state"] == settled
    assert agent_reports.current(A)["id"] == rep["id"]  # not ended: the Commander must look
    before = list(sent)
    r = app.answer(rep["id"], 2)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == pulse_routes.REPORT_ANSWER_SENT_DETAIL
    assert sent == before and len(_actions()) == 1
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report_answer"] == {"available": False, "reason": pulse_routes.REPORT_ANSWER_SENT}
    # …and the write boundary refuses on its own: any other action for this report is spent.
    check, _state = actuator._report_authority(
        {
            "id": "another",
            "source": "report",
            "report_id": rep["id"],
            "report_generation": rep["generation"],
            "authority": {"physical_key": A},
        }
    )
    assert check() == (False, "an answer to this report was already sent, or may have been")


def test_a_delivered_answer_whose_end_failed_refuses_a_second_tap_and_heals_at_the_sync(
    hq, world, app, writer, monkeypatch
):
    """(c) delivered, but ending the report raised: the report stays current until the next
    reconcile ends it (bound to its id), and meanwhile a second tap types nothing."""
    real_end = agent_reports.end
    calls = {"n": 0}

    def end_once_busy(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise agent_reports.Busy("held")
        return real_end(*a, **k)

    monkeypatch.setattr(agent_reports, "end", end_once_busy)
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert app.answer(rep["id"], 1).status_code == 200
    assert calls["n"] == 1 and agent_reports.current(A)["id"] == rep["id"]
    r = app.answer(rep["id"], 2)
    assert r.status_code == 409 and r.json()["detail"] == pulse_routes.REPORT_ANSWER_SENT_DETAIL
    assert len(writer["sent"]) == 1
    hq_report_broker.reconcile_reports()
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "answered"


def test_a_clean_pre_byte_refusal_leaves_the_report_answerable(hq, world, app, writer, monkeypatch):
    """(d) the report check refused right after the claim (`stale`, nothing typed): the report is
    still current, so a fresh tap is allowed and delivers."""
    real = agent_reports.authority
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        return (False, "transient") if calls["n"] == 1 else real(*a, **k)

    monkeypatch.setattr(agent_reports, "authority", flaky)
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert app.answer(rep["id"], 1).status_code == 409
    assert [a["state"] for a in _actions()] == ["stale"] and writer["sent"] == []
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report_answer"]["available"] is True
    r = app.answer(rep["id"], 2)
    assert r.status_code == 200, r.text
    assert writer["sent"] == [session_input.bracketed_paste("Decision: Redis")]


def test_two_concurrent_taps_on_one_report_mint_one_answer(hq, world, app, monkeypatch):
    """(e) tap 1 passes the route's pre-check, then tap 2 mints and its answer settles
    `indeterminate` before tap 1 reaches the ledger lock: the check INSIDE the lock refuses it."""
    sent = _outcome_writer(monkeypatch, ["indeterminate"])
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    real = orchestrator.precondition_for
    first_in, go = threading.Event(), threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def precondition_for(key):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 1:  # tap 1, past the pre-check, before the ledger lock
            first_in.set()
            assert go.wait(20)
        return real(key)

    monkeypatch.setattr(orchestrator, "precondition_for", precondition_for)
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", app.answer(rep["id"], 1)))
    t.start()
    assert first_in.wait(20)
    app.answer(rep["id"], 2)  # tap 2: minted, claimed, settled indeterminate
    assert [a["state"] for a in _actions()] == ["indeterminate"]
    go.set()
    t.join(30)
    r = out["r"]
    assert r.status_code == 409 and r.json()["detail"] == pulse_routes.REPORT_ANSWER_SENT_DETAIL
    assert len(_actions()) == 1 and len(sent) == 0


def _ledger_action(**kw) -> dict:
    rec = {
        "id": kw.pop("id"),
        "state": "proposed",
        "ts": kw.pop("ts", time.time()),
        "session_id": A,
        "verb": "answer",
        "answer": "x",
        "authority": {"physical_key": A},
        **kw,
    }
    orchestrator_ledger.append(rec)
    return rec


def test_an_answer_ends_only_the_report_it_was_for(hq):
    """(f) an HQ answer proposed BEFORE a new report, delivered after it, leaves that report
    current; a report's own answer ends only its `report_id`."""
    _env, token = _launch(A)
    t0 = time.time()
    _ledger_action(id="hq1", ts=t0 - 10)  # HQ's text, queued before the agent asked
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert orchestrator_ledger.compare_and_set("hq1", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A)["id"] == rep["id"]
    # …while one proposed after the report was filed does answer it.
    _ledger_action(id="hq2", ts=time.time() + 1)
    assert orchestrator_ledger.compare_and_set("hq2", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A) is None
    # A report-sourced answer to an OLD report never ends the newer one.
    agent_reports.file(
        dict(DECISION),
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=rep["generation"],
        now=time.time() + 60,
    )
    newer = agent_reports.current(A)
    _ledger_action(id="r1", source="report", report_id=rep["id"], ts=time.time() + 120)
    assert orchestrator_ledger.compare_and_set("r1", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A)["id"] == newer["id"]
    _ledger_action(id="r2", source="report", report_id=newer["id"], ts=t0 - 100)
    assert orchestrator_ledger.compare_and_set("r2", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["id"] == newer["id"]


def test_the_settle_hook_never_blocks_the_event_loop(hq):
    """(g) ending the report waits on the write fence; on a running loop it runs in the executor,
    so the loop keeps ticking while a writer holds the fence, and the report ends once it frees."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    rec = {
        "id": "x1",
        "verb": "answer",
        "source": "report",
        "report_id": rep["id"],
        "authority": {"physical_key": A},
    }
    held, release = threading.Event(), threading.Event()

    def hold():
        with session_input.mutation_fence():
            held.set()
            release.wait(20)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(10)

    async def main():
        # ORDERING, not a stopwatch (robust under CI load): the hook must RETURN while the fence
        # is still held — the holder releases only after this — with the end handed to the
        # executor and not yet done. Run on the loop, it could only return after waiting out the
        # fence's budget, with nothing pending.
        orchestrator_ledger._settled("x1", "delivered", rec)
        pending = list(orchestrator_ledger._report_endings)
        deferred = bool(pending) and not any(f.done() for f in pending) and t.is_alive()
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.02)
            ticks += 1
        still = agent_reports.current(A)
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), 60)
        return deferred, ticks, still

    try:
        deferred, ticks, still = asyncio.run(main())
    finally:
        release.set()
        t.join(20)
    assert deferred and ticks == 10
    assert still is not None and still["id"] == rep["id"]  # it was waiting on the fence
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "answered"


# ---- request size (review on #1884) -------------------------------------------------------------


@contextlib.contextmanager
def _serving_broker():
    """The broker's client handler on its real socket, with the real stream limit."""
    sock = hq_report_broker.socket_path()
    started, stop = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()

        async def main():
            server = await asyncio.start_unix_server(
                hq_report_broker._client,
                path=str(sock),
                limit=hq_report_broker.REQUEST_MAX + 2,
            )
            stop["loop"], stop["server"] = loop, server
            started.set()
            async with server:
                await server.serve_forever()

        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    try:
        yield
    finally:
        stop["loop"].call_soon_threadsafe(stop["server"].close)


EMOJI = [chr(0x1F600 + i) for i in range(64)]


def _emoji(n: int, offset: int = 0) -> str:
    return "".join(EMOJI[(offset + i) % len(EMOJI)] for i in range(n))


def test_a_max_size_all_emoji_report_files_and_a_longer_one_is_too_long(hq):
    """(h) astral-plane characters are 12 bytes each as JSON's ASCII escapes: the largest valid
    report must still fit the request, and one past a field cap must be refused as too long —
    never as an unreachable service."""
    assert hq_report_mcp.REQUEST_MAX == hq_report_broker.REQUEST_MAX
    env, _token = _launch(A)
    biggest = {
        "state": "needs_decision",
        "summary": _emoji(agent_reports.SUMMARY_MAX),
        "question": _emoji(agent_reports.QUESTION_MAX, 7),
        "options": [_emoji(agent_reports.OPTION_MAX, 11 * (i + 1)) for i in range(4)],
    }
    # Even a client on json's default ASCII escapes fits the broker's cap.
    assert len(json.dumps({"token": "t" * 256, "op": "report", "report": biggest})) + 1 <= (
        hq_report_broker.REQUEST_MAX
    )
    with _serving_broker():
        assert hq_report_mcp.request(biggest, env=env) == {"ok": True, "state": "needs_decision"}
        assert agent_reports.current(A)["summary"] == biggest["summary"]
        over = {"state": "progress", "summary": _emoji(agent_reports.SUMMARY_MAX + 1)}
        assert hq_report_mcp.request(over, env=env) == {"ok": False, "error": "too_long"}
        # Past the request cap itself: refused as too long before it is sent…
        huge = {"state": "progress", "summary": _emoji(5000)}
        assert hq_report_mcp.request(huge, env=env) == {"ok": False, "error": "too_long"}
        # …and by the broker, from any client that sends it anyway.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(hq_report_broker.socket_path()))
            s.sendall(json.dumps({"token": "x", "op": "report", "report": huge}).encode() + b"\n")
            assert json.loads(s.makefile("rb").readline()) == {"ok": False, "error": "too_long"}


# ---- Hermes on #1884 (round 2) ------------------------------------------------------------------


def test_the_one_answer_survives_ledger_compaction(hq, world, app, monkeypatch):
    """The reservation lives on the REPORT, so 500+ newer terminal actions compacting the first
    answer's row away cannot make the report answerable again. (Compaction now also PINS that row
    while the report is current — `_report_pins`; it is switched off here to prove the
    reservation holds on its own.)"""
    monkeypatch.setattr(orchestrator_ledger, "_report_pins", lambda done, first_ts: set())
    sent = _outcome_writer(monkeypatch, ["indeterminate"])
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    app.answer(rep["id"], 1)
    (first,) = _actions()
    assert first["state"] == "indeterminate"
    path = orchestrator_ledger._path()
    t = time.time()
    with path.open("a") as fh:
        for i in range(orchestrator_ledger.HISTORY_MAX + 20):
            fh.write(json.dumps({"id": f"filler{i}", "state": "delivered", "ts": t + 1 + i}) + "\n")
    assert orchestrator_ledger.compact() > 0
    assert first["id"] not in orchestrator_ledger.latest_by_id()  # evicted
    assert not orchestrator_ledger.report_answer_spent(rep["id"])  # the ledger forgot
    before = list(sent)
    r = app.answer(rep["id"], 2)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert sent == before and _actions() == []
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report_answer"] == {"available": False, "reason": pulse_routes.REPORT_ANSWER_SENT}


def test_a_revocation_in_the_filing_gap_files_nothing(hq, monkeypatch):
    """The broker's liveness check runs before the filing fence; a revocation committing in
    between must refuse the report under the fence (`launch_ended`), storing nothing."""
    _env, token = _launch(A)
    real_guarded = agent_reports._guarded

    def guarded_then_revoked(rep):
        real_guarded(rep)
        _race("generation")  # the capability is revoked in the gap

    monkeypatch.setattr(agent_reports, "_guarded", guarded_then_revoked)
    assert _file(token) == {"ok": False, "error": "launch_ended"}
    assert agent_reports.current(A) is None and agent_reports.history(A) == []


def _flagged_world(world):
    world["cards"] = [_card(A, flagged=True)]
    return orchestrator.observed_screen(A)["fingerprint"]


def test_a_screen_dismissal_never_hides_a_newer_report(hq, world, pushes):
    """A dismissal hides the row only while the screen is unchanged — and a report row only if
    the dismissal was made over THAT report. A report the agent files on the same screen is new
    information: listed and announced."""
    fp = _flagged_world(world)
    from agent_sessions import needs_you_dismissals

    needs_you_dismissals.dismiss(A, fp)
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []  # the dismissal holds
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report"]["id"] == rep["id"]
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 0}
    # A dismissal made over THIS report hides it, until a newer report arrives.
    needs_you_dismissals.dismiss(A, fp, report_id=rep["id"])
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []
    agent_reports.file(
        {**DECISION, "summary": "a newer question"},
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=rep["generation"],
        now=time.time() + 60,
    )
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report"]["summary"] == "a newer question"
    # The pure builder, directly: the dismissal's report id decides.
    card = _card(A, flagged=True)
    newer = agent_reports.public(agent_reports.current(A))

    def build(report_id):
        return needs_you.build(
            [card],
            [],
            set(),
            observe=lambda r: {"fingerprint": fp},
            suppressed={A: fp},
            suppressed_reports={A: report_id},
            reports={A: newer},
        )["rows"]

    assert [r["id"] for r in build("")] == [A]
    assert [r["id"] for r in build(rep["id"])] == [A]
    assert build(newer["id"]) == []


def test_the_needs_you_dismiss_route_records_the_report_it_hid(hq, world, app):
    fp = _flagged_world(world)
    from agent_sessions import needs_you_dismissals

    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    r = app.c.post(
        f"/api/pulse/needs-you/{A}/dismiss", json={"shown_report_id": rep["id"]}, headers=app.hdr
    )
    assert r.status_code == 200, r.text
    recorded = needs_you_dismissals.suppressed_detail()[A]
    assert recorded == {"fingerprint": fp, "report_id": rep["id"], "action_ids": frozenset()}


def test_a_busy_fence_on_revocation_keeps_the_row_and_the_token(hq, monkeypatch):
    """A BOUND capability's revocation commits under the fence FIRST and removes the token only
    then: on a busy fence nothing changes — the live agent keeps its tool, and the next pass
    retries."""
    env, token = _launch(A)
    assert _file(token)["ok"]  # its first request BINDS it: only a bound one needs the fence
    token_file = env[hq_report_broker.ENV_TOKEN_FILE]
    (gen,) = [
        r[0]
        for r in sqlite3.connect(hq_report_broker.db_path()).execute(
            "SELECT id FROM caps WHERE phys_key = ?", (A,)
        )
    ]
    real_fence = session_input.mutation_fence

    @contextlib.contextmanager
    def busy():
        raise session_input.AuthorityFenceBusy("held elsewhere")
        yield  # pragma: no cover

    monkeypatch.setattr(session_input, "mutation_fence", busy)
    hq_report_broker.revoke_launch(A)
    assert hq_report_broker.generation_current(A, gen)  # still active
    assert os.path.exists(token_file)  # and the agent still has its token
    monkeypatch.setattr(session_input, "mutation_fence", real_fence)
    hq_report_broker.revoke_launch(A)
    assert not hq_report_broker.generation_current(A, gen)
    assert not os.path.exists(token_file)


def test_a_refused_tap_says_which_kind_of_refusal_it_is(hq, world, app, writer):
    """`stale`: nothing was typed and no earlier answer can have been; `spent`: an answer was
    already sent or may have been. The client may say "Nothing was sent" only for `stale`."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    r = app.answer("someotherid", 1)  # a report id held nowhere: never "nothing was sent"
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_UNCERTAIN
    writer["hooks"]["before_guard"] = lambda: _race("dismissed")
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_STALE
    assert writer["sent"] == []
    # …and a retap on that dismissed report: its ended row shows it was never answered (the
    # reservation of the refused tap was given back on its row too) — positive evidence: stale.
    r = app.answer(rep["id"], 2)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_STALE


# ---- Hermes on #1884 (addendum) -----------------------------------------------------------------

SECRET_IN_SUMMARY = "zq8-Vortex-Lumen-4471"


def test_a_secret_registered_during_filing_waits_for_the_commit(hq, monkeypatch):
    """(7) The guard and the commit sit inside ONE registration fence: a secret registered while
    a report is between its guard and its write is known strictly AFTER the commit — it cannot
    land in the window — and one registered before the report is refused."""
    from agent_sessions import template_secrets

    _env, token = _launch(A)
    report = {"state": "progress", "summary": f"key is {SECRET_IN_SUMMARY} now"}
    real_guarded = agent_reports._guarded
    registered = threading.Event()
    in_window: dict = {}

    def guarded_then_registration(rep):
        real_guarded(rep)

        def register():
            template_secrets.register_typed([SECRET_IN_SUMMARY])
            registered.set()

        t = threading.Thread(target=register)
        t.start()
        in_window["registered"] = registered.wait(0.5)
        in_window["thread"] = t

    monkeypatch.setattr(agent_reports, "_guarded", guarded_then_registration)
    try:
        out = _file(token, report)
        in_window["thread"].join(10)
        assert in_window["registered"] is False, "a secret was registered inside the window"
        assert out["ok"] and registered.is_set()  # clean when committed; known only after
        # …and now that it is known, the same text is refused and nothing is stored.
        monkeypatch.setattr(agent_reports, "_guarded", real_guarded)
        agent_reports.end(A, "dismissed")
        generation = agent_reports.history(A)[0]["generation"]
        with pytest.raises(agent_reports.Refused) as refused:
            agent_reports.file(
                report,
                session_key=A,
                phys_key=A,
                engine="claude",
                generation=generation,
                now=time.time() + 60,
            )
        assert refused.value.code == "credential"
        assert agent_reports.current(A) is None
    finally:
        with template_secrets._lock:
            template_secrets._typed.pop(SECRET_IN_SUMMARY, None)
            template_secrets._typed_whole.pop(SECRET_IN_SUMMARY, None)


def test_an_unreadable_ledger_refuses_the_answer_and_disables_the_card(hq, world, app, writer):
    """(8) Whether an earlier answer was sent is UNKNOWN when the ledger can't be read: the tap
    is refused (`unknown`), nothing is typed, and the card's options are disabled with the
    reason. (The reservation on the report is the primary source; this pins the ledger half
    on its own, so the report carries none.)"""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    orchestrator_ledger.append(
        {
            "id": "old-answer",
            "state": "indeterminate",
            "source": "report",
            "report_id": rep["id"],
            "claim_owner": "1:1",
            "session_id": B,
        }
    )
    path = orchestrator_ledger._path()
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        r = app.answer(rep["id"], 1)
        assert r.status_code == 503, r.text
        assert r.json()["code"] == pulse_routes.REFUSED_UNKNOWN
        assert writer["sent"] == [] and writer["hooks"]["calls"] == 0
        assert pulse_routes.report_answerable({"id": A, "report": {"id": rep["id"]}}, rep) == {
            "available": False,
            "reason": pulse_routes.REPORT_ANSWER_UNKNOWN,
        }
        check, _state = actuator._report_authority(
            {
                "id": "new",
                "source": "report",
                "report_id": rep["id"],
                "report_generation": rep["generation"],
                "authority": {"physical_key": A},
            }
        )
        assert check() == (False, agent_reports.ANSWER_UNKNOWN)
    finally:
        path.chmod(stat.S_IMODE(mode))
    assert agent_reports.answer_mark(agent_reports.current(A)) is None  # nothing reserved


def test_a_decision_needs_a_question_and_an_option_from_producer_to_card(hq, world):
    """(9) The tool tells the agent to wait for the Commander's choice, so a `needs_decision`
    must carry something to choose: refused at the producer without a question or options, and
    every stored decision reaches the card with as many buttons as options."""
    _env, token = _launch(A)
    no_choice = {"state": "needs_decision", "summary": "I need you", "question": "Which?"}
    assert _file(token, no_choice) == {"ok": False, "error": "bad_request"}
    assert "use blocked otherwise" in hq_report_mcp.MEANING["bad_request"]
    assert "use blocked otherwise" in hq_report_mcp.TOOLS[0]["description"]
    assert agent_reports.current(A) is None
    assert _file(token, {**DECISION, "options": ["Only one"]})["ok"]
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["kind"] == "report_decision"
    assert row["report"]["question"] and row["report"]["options"] == ["Only one"]


def test_expired_history_is_never_returned_and_is_swept_from_idle_sessions(hq):
    """(10) The seven-day bound holds on every read, and the broker's periodic pass removes
    expired ended reports from a session nobody wrote to since."""
    old = time.time() - 8 * 86400
    agent_reports.file(
        {"state": "progress", "summary": "old private text"},
        session_key=B,
        phys_key=B,
        engine="claude",
        generation="g-old",
        now=old,
    )
    agent_reports.end(B, "dismissed", now=old + 1)
    raw = agent_reports.store_path().read_text()
    assert "old private text" in raw  # nothing wrote to B since: still on disk
    assert agent_reports.history(B) == []  # …but never returned
    assert B not in agent_reports.latest_notes()
    hq_report_broker.reconcile()
    assert "old private text" not in agent_reports.store_path().read_text()
    assert agent_reports.sweep() == 0  # idempotent, and a no-op takes no fence


# ---- Hermes on #1884 (round 3) ------------------------------------------------------------------


def _caps(key=A):
    conn = sqlite3.connect(hq_report_broker.db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM caps WHERE phys_key = ? ORDER BY rowid", (key,))
        ]
    finally:
        conn.close()


@contextlib.contextmanager
def _busy_fence(monkeypatch):
    @contextlib.contextmanager
    def busy():
        raise session_input.AuthorityFenceBusy("held elsewhere")
        yield  # pragma: no cover

    real = session_input.mutation_fence
    monkeypatch.setattr(session_input, "mutation_fence", busy)
    try:
        yield
    finally:
        monkeypatch.setattr(session_input, "mutation_fence", real)


def test_a_retired_unbound_launch_cannot_file_as_its_replacement(hq, monkeypatch):
    """(1) The real relaunch interleaving: the old capability was minted and never used (never
    bound), the fence is busy at the relaunch, and the replacement's master is live. The old
    capability is revoked anyway — unbound, it needs no fence — and its token is refused."""
    env_old, token_old = _launch(A)
    (old,) = _caps()
    assert old["master"] is None
    with _busy_fence(monkeypatch):
        env_new, token_new = _launch(A)  # the relaunch, while a delivery holds the fence
    hq["masters"][A] = f"master-2:{A}"  # the replacement's master is the live one
    old_row = next(c for c in _caps() if c["id"] == old["id"])
    assert old_row["revoked_at"] is not None
    assert not os.path.exists(env_old[hq_report_broker.ENV_TOKEN_FILE])
    assert _file(token_old) == {"ok": False, "error": "unknown_capability"}
    assert env_new and _file(token_new)["ok"]  # the replacement reports as itself
    assert agent_reports.current(A)["generation"] != old["id"]


def test_an_older_launch_never_binds_a_newer_launchs_master(hq, monkeypatch):
    """(1) The bind rule on its own: even if an old, never-bound capability's revocation never
    got through, it may not bind the master a NEWER launch of the session started."""
    _env_old, token_old = _launch(A)
    real_revoke_session = hq_report_broker._revoke_session
    monkeypatch.setattr(hq_report_broker, "_revoke_session", lambda *a, **k: None)
    _env_new, token_new = _launch(A)
    monkeypatch.setattr(hq_report_broker, "_revoke_session", real_revoke_session)
    hq["masters"][A] = f"master-2:{A}"
    old, new = _caps()
    assert old["revoked_at"] is None and old["master"] is None  # the revocation was lost
    assert _file(token_old) == {"ok": False, "error": "launch_ended"}
    assert next(c for c in _caps() if c["id"] == old["id"])["master"] is None  # never bound
    assert _file(token_new)["ok"]
    assert next(c for c in _caps() if c["id"] == new["id"])["master"] == f"master-2:{A}"


def test_a_retired_bound_launch_stays_refused_by_its_master_identity(hq, monkeypatch):
    """(1) The bound variant: its fenced revocation can be stopped by a busy fence, but its token
    file went first (the relaunch's filesystem fence), and it is bound to ITS master — the
    replacement's is another — so it is refused whatever the fence does, while the replacement
    gets its own tool."""
    _env_old, token_old = _launch(A)
    assert _file(token_old)["ok"]  # bound to master-1
    with _busy_fence(monkeypatch):
        env_new = hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest())
        hq["masters"][A] = f"master-2:{A}"
        assert _file(token_old) == {"ok": False, "error": "launch_ended"}
    old_cap = _caps()[0]
    assert old_cap["master"] == f"master-1:{A}"
    conn = hq_report_broker._connect()
    try:
        row = conn.execute("SELECT * FROM caps WHERE id = ?", (old_cap["id"],)).fetchone()
        assert hq_report_broker._liveness(conn, row) == hq_report_broker.DEAD  # by identity
    finally:
        conn.close()
    assert _file(token_old) == {"ok": False, "error": "launch_ended"}  # and revoked, fence free
    assert _caps()[0]["revoked_at"] is not None
    token_new = open(env_new[hq_report_broker.ENV_TOKEN_FILE]).read()
    monkeypatch.setattr(agent_reports, "MIN_INTERVAL_S", 0.0)  # the old launch filed just now
    assert _file(token_new, {"state": "progress", "summary": "replacement here"})["ok"]


def _pending_escalation(aid, ts=None):
    from automation_helpers import append_current_action

    append_current_action(
        {
            "id": aid,
            "state": "escalated",
            "session_id": A,
            "verb": "answer",
            "answer": "x",
            "engine": "claude",
            "title": "t",
            "ts": ts or time.time(),
            "expires_at": time.time() + 3600,
        }
    )


def test_a_screen_dismissal_never_hides_a_newly_actionable_decision(hq, world, pushes, app):
    """(2) The dismissal covers the decisions the row carried; a NEW one on the same unchanged
    screen is listed and announced, and the dismissed occurrence alone stays hidden."""
    from agent_sessions import needs_you_dismissals

    world["cards"] = [_card(A)]  # decision-only: not flagged
    _pending_escalation("esc-1")
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["action"]["id"] == "esc-1"
    r = app.c.post(
        f"/api/pulse/needs-you/{A}/dismiss", json={"shown_action_ids": ["esc-1"]}, headers=app.hdr
    )
    assert r.status_code == 200, r.text
    assert needs_you_dismissals.suppressed_detail()[A]["action_ids"] == frozenset({"esc-1"})
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []  # that occurrence: hidden
    assert needs_you_notify.sync_once()["opened"] == 0
    _pending_escalation("esc-2", ts=time.time() + 1)  # a new decision, same screen
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["action"]["id"] == "esc-2"
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 0}
    # The pure builder: covered → hidden; anything uncovered → shown.
    fp = "fp-1"
    card = _card(A)
    pend = [
        {"id": "a1", "session_id": A, "can_approve": True, "ts": 1},
        {"id": "a2", "session_id": A, "can_approve": True, "ts": 2},
    ]

    def build(pending, covered):
        return needs_you.build(
            [card],
            pending,
            set(),
            observe=lambda r: {"fingerprint": fp},
            suppressed={A: fp},
            suppressed_actions={A: frozenset(covered)},
        )["rows"]

    assert build(pend[:1], {"a1"}) == []
    assert [r["id"] for r in build(pend, {"a1"})] == [A]
    assert [r["id"] for r in build(pend[1:], {"a1"})] == [A]
    assert build(pend, {"a1", "a2"}) == []


def test_an_attempted_unconfirmed_answer_is_reported_uncertain(hq, world, app, monkeypatch):
    """(3a) The approve path answers 200 for an attempted write too: the report route says which
    — `delivered` only when the PTY took every byte."""
    _outcome_writer(monkeypatch, ["indeterminate"])
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    r = app.answer(rep["id"], 1)
    assert r.status_code == 200 and r.json()["outcome"] == "uncertain"


def test_a_delivered_answer_reports_delivered_and_a_retry_on_it_is_spent(hq, world, app, writer):
    """(3a) delivered → `delivered`; (3b) a retry on the report that answer ENDED is `spent`,
    never "nothing was sent"."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    r = app.answer(rep["id"], 1)
    assert r.status_code == 200 and r.json()["outcome"] == "delivered"
    assert agent_reports.current(A) is None  # ended: answered
    again = app.answer(rep["id"], 2)
    assert again.status_code == 409 and again.json()["code"] == pulse_routes.REFUSED_SPENT
    assert len(writer["sent"]) == 1
    # …while a report that simply moved on (dismissed, never answered) is `stale`.
    agent_reports.file(
        dict(DECISION),
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=rep["generation"],
        now=time.time() + 60,
    )
    other = agent_reports.current(A)
    agent_reports.end(A, "dismissed")
    gone = app.answer(other["id"], 1)
    assert gone.status_code == 409 and gone.json()["code"] == pulse_routes.REFUSED_STALE


# ---- Hermes on #1884 (round 4) ------------------------------------------------------------------


def test_a_store_failure_at_relaunch_never_lets_the_old_token_claim_the_replacement(
    hq, monkeypatch
):
    """(1) The capability store fails at the relaunch (`database is locked`): the old row is not
    revoked AND the replacement gets no newer row, so neither revocation nor the newest-cap rule
    can stop the old, never-bound token. The filesystem fence does: the relaunch removed its
    token file first, and a capability whose token file is gone is dead."""
    env_old, token_old = _launch(A)
    (old,) = _caps()
    assert old["master"] is None
    real_connect = hq_report_broker._connect

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hq_report_broker, "_connect", locked)
    assert hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest()) == {}
    monkeypatch.setattr(hq_report_broker, "_connect", real_connect)
    hq["masters"][A] = f"master-2:{A}"  # the replacement's master is live
    assert not os.path.exists(env_old[hq_report_broker.ENV_TOKEN_FILE])
    assert _file(token_old) == {"ok": False, "error": "launch_ended"}
    (row,) = _caps()
    assert row["master"] is None  # never bound to the replacement
    assert agent_reports.current(A) is None and agent_reports.history(A) == []


def test_a_failed_revocation_at_relaunch_still_gives_the_replacement_its_own_tool(hq, monkeypatch):
    """(1) Only the revocation UPDATE fails: the replacement still gets a capability that works,
    and the old token is refused."""
    _env_old, token_old = _launch(A)

    def failing(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    real = hq_report_broker._revoke_session
    monkeypatch.setattr(hq_report_broker, "_revoke_session", failing)
    env_new = hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest())
    monkeypatch.setattr(hq_report_broker, "_revoke_session", real)
    hq["masters"][A] = f"master-2:{A}"
    assert _file(token_old) == {"ok": False, "error": "launch_ended"}
    token_new = open(env_new[hq_report_broker.ENV_TOKEN_FILE]).read()
    assert _file(token_new)["ok"]
    assert agent_reports.current(A)["generation"] == _caps()[-1]["id"]


def _dismiss(app, **body):
    r = app.c.post(f"/api/pulse/needs-you/{A}/dismiss", json=body, headers=app.hdr)
    assert r.status_code == 200, r.text


def test_a_dismissal_covers_only_the_decision_the_row_showed(hq, world, pushes, app):
    """(2) A is shown; B arrives; the operator dismisses what they saw (A). B is listed and
    announced; A alone stays hidden. A dismissal naming nothing records nothing."""
    from agent_sessions import needs_you_dismissals

    world["cards"] = [_card(A)]
    _pending_escalation("esc-a")
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["action"]["id"] == "esc-a"  # what the operator sees
    _pending_escalation("esc-b", ts=time.time() + 1)  # arrives after they looked
    _dismiss(app, shown_action_ids=["esc-a"])
    assert needs_you_dismissals.suppressed_detail()[A]["action_ids"] == frozenset({"esc-a"})
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["action"]["id"] == "esc-b"
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 0}
    orchestrator_ledger.transition("esc-b", "rejected")
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []  # A alone: hidden
    # An older client naming nothing hides nothing that carries a decision.
    _pending_escalation("esc-c", ts=time.time() + 2)
    _dismiss(app)
    assert needs_you_dismissals.suppressed_detail()[A]["action_ids"] == frozenset()
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["action"]["id"] == "esc-c"


def test_a_dismissal_covers_only_the_report_the_row_showed(hq, world, pushes, app):
    """(3) Report A is shown; the agent files B on the unchanged screen; the operator dismisses
    what they saw (A). B is visible and announced; a shown report that is not current is never
    recorded."""
    from agent_sessions import needs_you_dismissals

    _env, token = _launch(A)
    assert _file(token)["ok"]
    shown = agent_reports.current(A)
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report"]["id"] == shown["id"]
    assert needs_you_notify.sync_once()["opened"] == 1
    agent_reports.file(
        {**DECISION, "summary": "a newer question"},
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=shown["generation"],
        now=time.time() + 60,
    )
    _dismiss(app, shown_report_id=shown["id"])
    assert needs_you_dismissals.suppressed_detail()[A]["report_id"] == ""  # A was not current
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report"]["summary"] == "a newer question"
    # Still announced, and announced as the NEWER report (#2021): the episode that stood for
    # the replaced one closes and a new one opens in the same pass, never a gap between them.
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 1}
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    newer = agent_reports.current(A)
    _dismiss(app, shown_report_id=newer["id"])
    assert pulse_routes.build_needs_you(1, strict=True)["rows"] == []


def test_details_never_names_a_report_it_did_not_show(hq, world, app, pushes):
    """(2, round 5) The list shows report A; the agent files B; Details is opened and Dismiss is
    tapped. The request names A (the row's report — Details fetches no report id of its own), so
    nothing is recorded and B stays visible and announced."""
    from agent_sessions import needs_you_dismissals

    _env, token = _launch(A)
    assert _file(token)["ok"]
    shown = agent_reports.current(A)
    assert needs_you_notify.sync_once()["opened"] == 1
    agent_reports.file(
        {**DECISION, "summary": "B"},
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=shown["generation"],
        now=time.time() + 60,
    )
    d = app.c.get(f"/api/pulse/needs-you/{A}/details").json()
    assert "report_id" not in d  # Details carries no report of its own to dismiss
    _dismiss(app, shown_report_id=shown["id"])
    assert needs_you_dismissals.suppressed_detail()[A]["report_id"] == ""
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report"]["summary"] == "B"
    # B is announced in A's place (#2021): one episode closes, one opens, in the same pass.
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 1}


# ---- Hermes on #1884 (round 5) ------------------------------------------------------------------


@contextlib.contextmanager
def _stand_in_master(key, env_extra):
    """A real process the master lookup finds (its argv names the session's socket after `-c`),
    with the given variables in its OWN environment — read back through `/proc/<pid>/environ`."""
    from agent_sessions import ptybridge

    engine, _, native = key.partition(":")
    sock = str(ptybridge.socket_path(engine, native))
    env = {k: v for k, v in os.environ.items() if k != hq_report_broker.ENV_CAP}
    env.update(env_extra)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", sock], env=env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and hq_report_broker._master_carries(key, "probe") is None:
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait(10)


def test_a_double_fault_and_a_QUICK_relaunch_never_bind_the_old_token(hq, monkeypatch):
    """(1, round 6) Both fences fail at the relaunch — the token directory can't be read or
    unlinked AND the capability store is down — so the old, never-bound row and its token both
    survive and the replacement gets no newer row. The replacement's master starts IMMEDIATELY
    (no time window could tell them apart). On recovery the old token is refused: the
    replacement master's own environment does not carry the old capability's id."""
    env_old, token_old = _launch(A)
    (old,) = _caps()

    def broken(*a, **k):
        raise OSError("injected")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(hq_report_broker.os, "listdir", broken)
        m.setattr(hq_report_broker.os, "unlink", broken)
        m.setattr(hq_report_broker, "_connect", locked)
        assert hq_report_broker.launch_env(A, A, engine="claude", manifest=Manifest()) == {}
    assert os.path.exists(env_old[hq_report_broker.ENV_TOKEN_FILE])  # survived
    hq["masters"][A] = f"master-2:{A}"
    hq["environs"][A] = "real"
    with _stand_in_master(A, {}):  # the replacement: started now, no capability of its own
        assert _file(token_old) == {"ok": False, "error": "launch_ended"}
    (row,) = _caps()
    assert row["master"] is None and row["revoked_at"] is not None  # never bound; revoked
    assert agent_reports.current(A) is None and agent_reports.history(A) == []
    # The next launch's own capability binds ITS master, which carries its id.
    env_new, token_new = _launch(A)
    new_env = hq_report_broker.master_env(env_new)
    assert new_env == {hq_report_broker.ENV_CAP: _caps()[-1]["id"]}
    with _stand_in_master(A, new_env):
        assert _file(token_new)["ok"]


def test_a_capability_binds_only_the_master_whose_environment_carries_its_id(hq):
    """(1, round 6) Against the REAL kernel: the master carrying this capability's id binds; one
    carrying another id is DEAD; one carrying none — an older build's launch — is DEAD too (it can
    never be shown to be this capability's, so it is revoked, not retried for ever); no master to
    read is UNKNOWN (refused, never bound)."""
    _env, _token = _launch(A)
    (cap,) = _caps()
    conn = hq_report_broker._connect()
    try:
        row = conn.execute("SELECT * FROM caps WHERE id = ?", (cap["id"],)).fetchone()
        assert hq_report_broker._master_carries(A, cap["id"]) is None  # no master yet
        assert hq_report_broker._bind(conn, row, "m", None) == hq_report_broker.UNKNOWN
        with _stand_in_master(A, {hq_report_broker.ENV_CAP: "someone-else"}):
            assert hq_report_broker._master_carries(A, cap["id"]) is False
        with _stand_in_master(A, {}):
            carries = hq_report_broker._master_carries(A, cap["id"])
            assert carries is False
            assert hq_report_broker._bind(conn, row, "m", carries) == hq_report_broker.DEAD
        with _stand_in_master(A, {hq_report_broker.ENV_CAP: cap["id"]}):
            carries = hq_report_broker._master_carries(A, cap["id"])
            assert carries is True
            assert hq_report_broker._bind(conn, row, "m", carries) == hq_report_broker.ALIVE
    finally:
        conn.close()


def test_the_launch_environment_carries_the_cap_id_and_nothing_else_inherits_it(monkeypatch):
    """(1, round 6) The id reaches a launch's own environment (`master_env`), never the app's,
    and every spawn path scrubs it from what it inherits."""
    from agent_sessions import headless_dispatch, webterm

    env = {hq_report_broker.ENV_TOKEN_FILE: "/run/x/hq-report/abc-123/.token"}
    assert hq_report_broker.master_env(env) == {hq_report_broker.ENV_CAP: "abc-123"}
    assert hq_report_broker.master_env({}) == {}
    monkeypatch.setenv(hq_report_broker.ENV_CAP, "leaked")
    hq_report_broker.scrub_process_environment()
    assert hq_report_broker.ENV_CAP not in os.environ
    assert hq_report_broker.ENV_CAP in hq_report_broker.ALL_ENV_KEYS
    for mod in (webterm, headless_dispatch):
        assert "hq_report_broker.ALL_ENV_KEYS" in open(mod.__file__).read()


def _hq_answer(aid, *, ts, phys=A):
    from automation_helpers import append_current_action

    append_current_action(
        {
            "id": aid,
            "state": "proposed",
            "ts": ts,
            "session_id": A,
            "verb": "answer",
            "answer": "use Postgres",
            "engine": "claude",
            "title": "t",
            "expires_at": time.time() + 3600,
        }
    )


def test_an_ordinary_answer_delivered_after_the_report_spends_it_before_its_end_runs(
    hq, world, app, writer, monkeypatch
):
    """(3a) HQ's own answer is delivered after the agent asked; its end of the report has not run
    (delayed). An option tap must not send a SECOND answer: 409 `spent`, zero bytes."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    _hq_answer("hq-1", ts=float(rep["at"]) + 1)
    monkeypatch.setattr(orchestrator_ledger, "_end_answered_report", lambda *a, **k: None)
    assert orchestrator_ledger.compare_and_set("hq-1", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A)["id"] == rep["id"]  # its end has not run
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert writer["sent"] == [] and writer["hooks"]["calls"] == 0
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["report_answer"] == {"available": False, "reason": pulse_routes.REPORT_ANSWER_SENT}
    check, _state = actuator._report_authority(
        {
            "id": "an-option",
            "source": "report",
            "report_id": rep["id"],
            "report_generation": rep["generation"],
            "authority": {"physical_key": A},
        }
    )
    assert check() == (False, agent_reports.ANSWER_SPENT)


def test_an_ordinary_answers_failed_end_is_healed_by_the_reconcile(hq, world, monkeypatch):
    """(3b) Its end failed: the needs-you sync's reconcile ends the report, bound to its id; an
    answer proposed BEFORE the report never does."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    _hq_answer("hq-early", ts=float(rep["at"]) - 10)
    with monkeypatch.context() as m:
        m.setattr(orchestrator_ledger, "_end_answered_report", lambda *a, **k: None)
        orchestrator_ledger.compare_and_set("hq-early", frozenset({"proposed"}), "delivered")
    hq_report_broker.reconcile_reports()
    assert agent_reports.current(A)["id"] == rep["id"]  # an earlier answer is not this one's
    _hq_answer("hq-late", ts=float(rep["at"]) + 1)
    with monkeypatch.context() as m:
        m.setattr(agent_reports, "end", lambda *a, **k: (_ for _ in ()).throw(agent_reports.Busy()))
        orchestrator_ledger.compare_and_set("hq-late", frozenset({"proposed"}), "delivered")
    assert agent_reports.current(A)["id"] == rep["id"]  # the end failed
    hq_report_broker.reconcile_reports()
    assert agent_reports.current(A) is None
    assert agent_reports.history(A)[0]["ended_by"] == "answered"


# ---- Hermes on #1884 (round 6: compaction keeps spent evidence) ---------------------------------


def _fill_ledger(n=None):
    path = orchestrator_ledger._path()
    t = time.time()
    with path.open("a") as fh:
        for i in range(n or orchestrator_ledger.HISTORY_MAX + 20):
            fh.write(
                json.dumps({"id": f"filler{i}", "state": "delivered", "ts": t + 10 + i}) + "\n"
            )


def test_compaction_keeps_an_ordinary_answer_that_spends_a_current_report(
    hq, world, app, writer, monkeypatch
):
    """(2, round 6) HQ's answer was delivered after the agent asked, and its end of the report is
    held (not yet run). 520 newer terminal actions compact the ledger: the delivered answer is
    KEPT while the report is current, so the report stays spent — the tap is 409 `spent`, zero
    bytes. Once the report ends, the next compaction may evict it."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    _hq_answer("hq-1", ts=float(rep["at"]) + 1)
    monkeypatch.setattr(orchestrator_ledger, "_end_answered_report", lambda *a, **k: None)
    assert orchestrator_ledger.compare_and_set("hq-1", frozenset({"proposed"}), "delivered")
    _fill_ledger()
    assert orchestrator_ledger.compact() > 0
    kept = orchestrator_ledger.latest_by_id()
    assert "hq-1" in kept and "filler0" not in kept  # the tail was cut; the evidence was not
    assert kept["hq-1"]["proposed_ts"] == float(rep["at"]) + 1  # its proposal time survives
    assert orchestrator_ledger.answered_after(A, float(rep["at"]))
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert writer["sent"] == [] and writer["hooks"]["calls"] == 0
    # The report ends (the held end runs at last): nothing pins the row any more.
    agent_reports.end(A, "answered", report_id=rep["id"])
    _fill_ledger()
    orchestrator_ledger.compact()
    assert "hq-1" not in orchestrator_ledger.latest_by_id()


def _candidate(aid, *, ts, phys, state="delivered"):
    orchestrator_ledger.append(
        {
            "id": aid,
            "state": state,
            "ts": ts,
            "session_id": phys,
            "verb": "answer",
            "answer": "x",
            "authority": {"physical_key": phys},
        }
    )


def test_compaction_declines_while_the_reports_store_cannot_be_read(hq, world, app, writer):
    """(1, round 7) A current report's delivered ordinary answer sits behind 500 newer candidate
    answers (to sessions whose reports ended) and 500 unrelated rows. With the reports store
    unreadable, compaction can't know which rows spend a current report, so it DECLINES: every
    row is kept. With the store back, it pins the current report's answer — still spent."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    t = float(rep["at"])
    _candidate("hq-current", ts=t + 1, phys=A)
    for i in range(500):
        _candidate(f"cand{i}", ts=t + 10 + i, phys=f"claude:aaaaaaaa-0000-4000-8000-{i:012d}")
    path = orchestrator_ledger._path()
    with path.open("a") as fh:
        for i in range(500):
            fh.write(json.dumps({"id": f"other{i}", "state": "rejected", "ts": t + 600 + i}) + "\n")
    before = path.read_bytes()
    store = agent_reports.store_path()
    saved = store.read_bytes()
    store.write_text("[]")  # exists, cannot be read as a document
    try:
        assert orchestrator_ledger.compact() == 0  # declined
        assert path.read_bytes() == before  # nothing rewritten
    finally:
        store.write_bytes(saved)
    assert orchestrator_ledger.compact() > 0
    kept = orchestrator_ledger.latest_by_id()
    assert "hq-current" in kept and "cand0" not in kept  # pinned; the old tail was cut
    assert orchestrator_ledger.answered_after(A, t)
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert writer["sent"] == []


def test_an_unreadable_ledger_is_never_compacted_into_an_empty_one(hq, monkeypatch):
    """(2, round 7) A failed read (permission, I/O) is not an empty ledger: compaction declines
    without rewriting, the bytes are unchanged, and the answer is still there once access
    returns. A ledger that is genuinely empty or absent compacts as before."""
    _candidate("hq-1", ts=time.time(), phys=A)
    path = orchestrator_ledger._path()
    before = path.read_bytes()
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        assert orchestrator_ledger.compact() == 0
    finally:
        path.chmod(stat.S_IMODE(mode))
    assert path.read_bytes() == before
    assert orchestrator_ledger.latest_by_id()["hq-1"]["state"] == "delivered"
    # …and an I/O error, the same way.
    real = type(path).read_text

    def failing(self, *a, **k):
        if self == path:
            raise OSError(5, "Input/output error")
        return real(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(type(path), "read_text", failing)
        assert orchestrator_ledger.compact() == 0
    assert path.read_bytes() == before


def test_an_out_of_window_report_on_a_reconciled_session_is_listed_and_announced(
    hq, world, pushes, monkeypatch
):
    """(3, round 7) A launch on an engine that mints its own id files under its PLACEHOLDER
    physical key; reconciliation gives the card the real id. Outside the window, the report must
    still bring the card in: both sides are compared as resolved physical keys."""
    from agent_sessions import metadata

    placeholder = "opencode:new-aaaaaaaa-0000-4000-8000-00000000000c"
    real = "opencode:ses_15873112effeDtw4Xl4qIC4d"
    monkeypatch.setattr(metadata, "load_aliases", lambda *a, **k: {placeholder: real})
    # …and the strict read's own checked snapshot (round 8): the same map.
    monkeypatch.setattr(metadata, "load_checked", lambda *a, **k: ({}, {placeholder: real}, {}))
    card = {**_card(real), "engine": "opencode"}

    def cards(**k):
        return [dict(card)] if k.get("window_days") is None else []  # outside the window

    monkeypatch.setattr(pulse, "build_cards", cards)
    hq["live"].add(placeholder)
    env = hq_report_broker.launch_env(
        placeholder, placeholder, engine="opencode", manifest=Manifest()
    )
    assert _file(open(env[hq_report_broker.ENV_TOKEN_FILE]).read())["ok"]
    assert agent_reports.current(placeholder)["session_key"] == placeholder
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["id"] == real and row["kind"] == "report_decision"
    assert needs_you_notify.sync_once()["opened"] == 1


# ---- Hermes on #1884 (round 8) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "expect"),
    [
        (PermissionError(13, "Permission denied"), "unreadable"),
        (OSError(5, "Input/output error"), "unreadable"),
        (FileNotFoundError(2, "No such file or directory"), "absent"),
    ],
    ids=["EACCES", "EIO", "ENOENT"],
)
def test_a_socket_read_error_is_unknown_and_only_a_missing_socket_is_absent(
    hq, monkeypatch, err, expect
):
    """(1, round 8) Only a socket that is NOT THERE ends a launch: EACCES / EIO on its lstat
    are unknown — the capability and its report survive every pass; ENOENT still retires them."""
    from agent_sessions import ptybridge

    _env, token = _launch(A)
    assert _file(token)["ok"]  # bound to its master
    rep = agent_reports.current(A)
    engine, _, native = A.partition(":")
    sock = ptybridge.socket_path(engine, native)
    real_lstat = os.lstat

    def lstat(path, *a, **k):
        if str(path) == str(sock):
            raise err
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(hq_report_broker, "_master_identity", REAL_MASTER_IDENTITY)
    monkeypatch.setattr(hq_report_broker, "_liveness", hq_report_broker._liveness_impl)
    monkeypatch.setattr(hq_report_broker.os, "lstat", lstat)
    assert hq_report_broker._master_identity(A)[0] == expect
    hq_report_broker.reconcile_reports()
    hq_report_broker.reconcile()
    (cap,) = _caps()
    if expect == "unreadable":
        assert cap["revoked_at"] is None
        assert agent_reports.current(A)["id"] == rep["id"]
        assert hq_report_broker.generation_state(A, cap["id"]) is None
        assert hq_report_broker.generation_current(A, cap["id"]) is False  # authorization: no
    else:
        assert cap["revoked_at"] is not None
        assert agent_reports.current(A) is None
        assert agent_reports.history(A)[0]["ended_by"] == "launch_ended"


def _reconciled_report_world(hq, monkeypatch):
    from agent_sessions import metadata

    placeholder = "opencode:new-aaaaaaaa-0000-4000-8000-00000000000c"
    real = "opencode:ses_15873112effeDtw4Xl4qIC4d"
    card = {**_card(real), "engine": "opencode"}

    def cards(**k):
        return [dict(card)] if k.get("window_days") is None else []  # outside the window

    monkeypatch.setattr(pulse, "build_cards", cards)
    monkeypatch.setattr(metadata, "load_aliases", lambda *a, **k: {placeholder: real})
    monkeypatch.setattr(metadata, "load_checked", lambda *a, **k: ({}, {placeholder: real}, {}))
    hq["live"].add(placeholder)
    env = hq_report_broker.launch_env(
        placeholder, placeholder, engine="opencode", manifest=Manifest()
    )
    assert _file(open(env[hq_report_broker.ENV_TOKEN_FILE]).read())["ok"]
    return placeholder, real


def test_an_unreadable_alias_map_fails_the_strict_read_and_retracts_nothing(
    hq, world, pushes, monkeypatch
):
    """(2, round 8) The strict read carries ONE checked alias snapshot through card selection,
    report attachment and answerability. If the alias map can't be read, the strict read FAILS
    (`ReadIncomplete`) — the sync skips and retracts nothing — instead of reading "no aliases"
    and detaching the report from its card. The list route stays fail-soft."""
    from agent_sessions import metadata

    _placeholder, real = _reconciled_report_world(hq, monkeypatch)
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["id"] == real
    assert needs_you_notify.sync_once()["opened"] == 1

    def unreadable(*a, **k):
        raise metadata.MetadataUnreadable("EIO")

    monkeypatch.setattr(metadata, "load_checked", unreadable)
    monkeypatch.setattr(metadata, "load_aliases", lambda *a, **k: {})  # the fail-soft answer
    with pytest.raises(needs_you.ReadIncomplete):
        pulse_routes.build_needs_you(1, strict=True)
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}  # nothing retracted
    bell = [n for n in notifications.listing()["notifications"] if n.get("needs_you")]
    assert len(bell) == 1  # still announced: nothing retracted
    assert pulse_routes.build_needs_you(1)["rows"] == []  # the Ask list: fail-soft, as before


def _report_answer_row(aid, *, report_id, state, claimed, ts):
    rec = {
        "id": aid,
        "state": state,
        "ts": ts,
        "source": "report",
        "report_id": report_id,
        "session_id": A,
        "verb": "answer",
        "authority": {"physical_key": A},
    }
    if claimed:
        rec["claim_owner"] = "1:1"
    orchestrator_ledger.append(rec)


def test_clean_refusals_are_not_pinned_but_an_uncertain_answer_is(hq, world, app, writer):
    """(3, round 8) Compaction pins only answers the one-answer rule counts as spent. 500 clean
    pre-byte refusals (claimed then `stale`) and never-claimed expiries for the CURRENT report
    are evicted like any tail; an indeterminate one is kept and still spends the report."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    t = float(rep["at"])
    _report_answer_row(
        "uncertain", report_id=rep["id"], state="indeterminate", claimed=True, ts=t + 1
    )
    for i in range(250):  # all OLDER than the filler tail below
        _report_answer_row(
            f"stale{i}", report_id=rep["id"], state="stale", claimed=True, ts=t + 1 + i / 1000
        )
        _report_answer_row(
            f"exp{i}", report_id=rep["id"], state="expired", claimed=False, ts=t + 2 + i / 1000
        )
    _fill_ledger()
    assert orchestrator_ledger.compact() > 0
    kept = orchestrator_ledger.latest_by_id()
    assert "uncertain" in kept
    assert not any(k.startswith(("stale", "exp")) for k in kept)  # bounded: evicted
    assert orchestrator_ledger.report_answer_spent(rep["id"])
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert writer["sent"] == []


# ---- Hermes on #1884 (round 9: error classification, A2) ----------------------------------------
#
# The audit's rule, pinned site by site: a confirmed ABSENT (ENOENT) or a MISMATCH may take the
# destructive or completing action; anything UNKNOWN (EACCES, EIO, malformed) changes NOTHING —
# no revoke, no end, no retraction, no eviction, no strict read taken as complete.


@contextlib.contextmanager
def _fault(monkeypatch, path, kind, *, garbage="[]"):
    """Make reads of ``path`` fail the way ``kind`` says. ENOENT and EACCES are REAL (the file is
    removed / made unreadable); EIO is injected at every open path a module uses; malformed
    writes ``garbage``. The file is restored afterwards."""
    path = pathlib.Path(path)
    saved = path.read_bytes() if path.exists() else None
    target = str(path)
    if kind == "ENOENT":
        if path.exists():
            path.unlink()
        try:
            yield
        finally:
            if saved is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(saved)
    elif kind == "EACCES":
        mode = path.stat().st_mode
        path.chmod(0)
        try:
            yield
        finally:
            path.chmod(stat.S_IMODE(mode))
    elif kind == "EIO":
        real_io, real_os = io.open, os.open

        def _hit(p):
            return str(p) == target

        def io_open(file, *a, **k):
            if _hit(file):
                raise OSError(errno.EIO, "Input/output error")
            return real_io(file, *a, **k)

        def os_open(p, *a, **k):
            if _hit(p) or (k.get("dir_fd") is not None and target.endswith("/" + str(p))):
                raise OSError(errno.EIO, "Input/output error")
            return real_os(p, *a, **k)

        with monkeypatch.context() as m:
            m.setattr(io, "open", io_open)
            m.setattr("builtins.open", io_open)
            m.setattr(os, "open", os_open)
            yield
    elif kind == "malformed":
        path.write_text(garbage)
        try:
            yield
        finally:
            path.write_bytes(saved)
    else:  # pragma: no cover
        raise AssertionError(kind)


def _world():
    """Everything a destructive pass could change: the reports store, the ledger, every
    capability row, the notifications store."""

    def raw(p):
        p = pathlib.Path(p)
        return p.read_bytes() if p.exists() else None

    conn = sqlite3.connect(hq_report_broker.db_path())
    try:
        caps = sorted(tuple(r) for r in conn.execute("SELECT * FROM caps"))
    finally:
        conn.close()
    return {
        "reports": raw(agent_reports.store_path()),
        "ledger": raw(orchestrator_ledger._path()),
        "caps": caps,
        "notifications": raw(notifications._notifications_path()),
    }


def _setup_reporting(monkeypatch):
    monkeypatch.setattr(agent_reports, "MIN_INTERVAL_S", 0.0)
    _env, token = _launch(A)
    assert _file(token)["ok"]  # bound to its master
    return token


def _site_token(hq, world, pushes, monkeypatch):
    token = _setup_reporting(monkeypatch)
    (cap,) = _caps()
    path = hq_report_broker._launch_dir(cap["id"]) / hq_report_broker.TOKEN_FILE

    def run():
        return _file(token, {"state": "progress", "summary": "still here"})

    def absent(out):
        assert out == {"ok": False, "error": "launch_ended"}
        assert _caps()[0]["revoked_at"] is not None

    def unknown(out):
        assert out == {"ok": False, "error": "launch_unconfirmed"}

    return path, run, absent, unknown, ("ENOENT", "EACCES", "EIO")


def _site_socket(hq, world, pushes, monkeypatch):
    from agent_sessions import ptybridge

    _setup_reporting(monkeypatch)
    engine, _, native = A.partition(":")
    sock = ptybridge.socket_path(engine, native)
    monkeypatch.setattr(hq_report_broker, "_master_identity", REAL_MASTER_IDENTITY)
    monkeypatch.setattr(hq_report_broker, "_liveness", hq_report_broker._liveness_impl)
    real_lstat = os.lstat

    def run():
        hq_report_broker.reconcile_reports()
        hq_report_broker.reconcile()

    def absent(_out):
        assert _caps()[0]["revoked_at"] is not None and agent_reports.current(A) is None

    return (sock, run, absent, lambda _out: None, ("ENOENT", "EACCES", "EIO"), real_lstat)


def _site_reports_compaction(hq, world, pushes, monkeypatch):
    _setup_reporting(monkeypatch)
    rep = agent_reports.current(A)
    _hq_answer("hq-1", ts=float(rep["at"]) + 1)
    with monkeypatch.context() as m:
        m.setattr(orchestrator_ledger, "_end_answered_report", lambda *a, **k: None)
        orchestrator_ledger.compare_and_set("hq-1", frozenset({"proposed"}), "delivered")
    _fill_ledger()

    def run():
        return orchestrator_ledger.compact()

    def absent(_out):
        assert "hq-1" not in orchestrator_ledger.latest_by_id()  # no report: nothing pinned

    return agent_reports.store_path(), run, absent, None, ("ENOENT", "EACCES", "EIO", "malformed")


def _site_reports_sync(hq, world, pushes, monkeypatch):
    _setup_reporting(monkeypatch)
    assert needs_you_notify.sync_once()["opened"] == 1

    def run():
        return needs_you_notify.sync_once()

    def absent(out):
        assert out["closed"] == 1  # no report: the episode ends

    def unknown(out):
        assert out == {"skipped": "unavailable"}

    return (
        agent_reports.store_path(),
        run,
        absent,
        unknown,
        ("ENOENT", "EACCES", "EIO", "malformed"),
    )


def _site_ledger_compaction(hq, world, pushes, monkeypatch):
    _setup_reporting(monkeypatch)
    _fill_ledger()

    def run():
        return orchestrator_ledger.compact()

    def unknown(out):
        assert out == 0  # declined

    return orchestrator_ledger._path(), run, None, unknown, ("EACCES", "EIO")


def _site_ledger_admission(hq, world, pushes, monkeypatch, *, app=None, writer=None):
    _setup_reporting(monkeypatch)
    rep = agent_reports.current(A)
    orchestrator_ledger.append({"id": "unrelated", "state": "rejected", "ts": time.time()})

    def run():
        return app.answer(rep["id"], 1)

    def unknown(r):
        assert r.status_code == 503 and r.json()["code"] == pulse_routes.REFUSED_UNKNOWN
        assert writer["sent"] == []

    return orchestrator_ledger._path(), run, None, unknown, ("EACCES", "EIO")


def _site_aliases(hq, world, pushes, monkeypatch):
    from agent_sessions import metadata

    _setup_reporting(monkeypatch)
    assert needs_you_notify.sync_once()["opened"] == 1
    path = metadata._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"__aliases__": {}}))

    def run():
        return needs_you_notify.sync_once()

    def absent(out):
        assert out == {"opened": 0, "closed": 0}  # no sidecar: no aliases, the row stands

    def unknown(out):
        assert out == {"skipped": "unavailable"}

    return path, run, absent, unknown, ("ENOENT", "EACCES", "EIO", "malformed")


def _site_dismissals(hq, world, pushes, monkeypatch):
    from agent_sessions import needs_you_dismissals

    _setup_reporting(monkeypatch)
    assert needs_you_notify.sync_once()["opened"] == 1
    path = needs_you_dismissals._path()
    path.write_text(json.dumps({"dismissed": {}}))

    def run():
        return needs_you_notify.sync_once()

    def absent(out):
        assert out == {"opened": 0, "closed": 0}

    def unknown(out):
        assert out == {"skipped": "unavailable"}

    return path, run, absent, unknown, ("ENOENT", "EACCES", "EIO", "malformed")


SITES = {
    "token": _site_token,
    "socket": _site_socket,
    "reports-compaction": _site_reports_compaction,
    "reports-sync": _site_reports_sync,
    "ledger-compaction": _site_ledger_compaction,
    "ledger-admission": _site_ledger_admission,
    "aliases": _site_aliases,
    "dismissals": _site_dismissals,
}
GARBAGE = {
    "reports-compaction": '{"sessions": {"x": 1}}',  # parses; an entry this module can't read
    "reports-sync": '{"sessions": {"x": {"current": {"id": 5}}}}',
    "aliases": json.dumps({"__aliases__": {"opencode:new-x": 7}}),
    "dismissals": json.dumps({"dismissed": {A: {"fingerprint": 3}}}),
}


@pytest.mark.parametrize("kind", ["ENOENT", "EACCES", "EIO", "malformed"])
@pytest.mark.parametrize("site", sorted(SITES))
def test_the_error_matrix_unknown_changes_nothing(
    hq, world, pushes, app, writer, monkeypatch, site, kind
):
    """Every destructive site in the audit, under every injected error: ABSENT (ENOENT) may act;
    EACCES / EIO / malformed are UNKNOWN — the pass refuses or skips and nothing changes."""
    extra = {"app": app, "writer": writer} if site == "ledger-admission" else {}
    got = SITES[site](hq, world, pushes, monkeypatch, **extra)
    real_lstat = None
    if len(got) == 6:
        path, run, absent, unknown, kinds, real_lstat = got
    else:
        path, run, absent, unknown, kinds = got
    if kind not in kinds:
        pytest.skip(f"{kind} does not apply to {site}")
    if site == "socket":  # no real socket exists: ENOENT is real; the rest are injected on lstat
        err = {"EACCES": errno.EACCES, "EIO": errno.EIO}.get(kind)

        def lstat(p, *a, **k):
            if err is not None and str(p) == str(path):
                raise OSError(err, os.strerror(err))
            return real_lstat(p, *a, **k)

        monkeypatch.setattr(hq_report_broker.os, "lstat", lstat)
        fault = contextlib.nullcontext()
    else:
        fault = _fault(monkeypatch, path, kind, garbage=GARBAGE.get(site, "[]"))
    before = _world()
    with fault:
        out = run()
    if kind == "ENOENT":
        if absent is not None:
            absent(out)
        return
    if unknown is not None:
        unknown(out)
    after = _world()
    assert after == before, f"{site} under {kind} changed state"


def test_F1_a_transient_token_read_error_never_retires_a_live_launch(hq, monkeypatch):
    """F1: EIO / EACCES on the token file is unknown — `launch_unconfirmed`, the capability and
    its report intact; only a token file that is GONE (ENOENT) ends the launch."""
    token = _setup_reporting(monkeypatch)
    rep = agent_reports.current(A)
    (cap,) = _caps()
    path = hq_report_broker._launch_dir(cap["id"]) / hq_report_broker.TOKEN_FILE
    for kind in ("EIO", "EACCES"):
        with _fault(monkeypatch, path, kind):
            assert _file(token, {"state": "progress", "summary": kind}) == {
                "ok": False,
                "error": "launch_unconfirmed",
            }
        assert _caps()[0]["revoked_at"] is None
        assert agent_reports.current(A)["id"] == rep["id"]
    with _fault(monkeypatch, path, "ENOENT"):
        assert _file(token, {"state": "progress", "summary": "gone"}) == {
            "ok": False,
            "error": "launch_ended",
        }
    assert _caps()[0]["revoked_at"] is not None


def test_F2_one_alias_snapshot_from_curation_to_join(hq, world, pushes, monkeypatch):
    """F2: the alias map changes BETWEEN the card curation and the report join (a sidecar write
    in between): the strict read still uses ONE snapshot, so the row is there. Malformed aliases
    fail the read and the sync retracts nothing."""
    from agent_sessions import metadata

    placeholder, real = _reconciled_report_world(hq, monkeypatch)
    snapshot = ({}, {placeholder: real}, {})
    reads = {"n": 0}

    def load_checked(*a, **k):
        reads["n"] += 1
        # The first read is the snapshot; any later one sees the alias gone.
        return snapshot if reads["n"] == 1 else ({}, {}, {})

    monkeypatch.setattr(metadata, "load_checked", load_checked)
    monkeypatch.setattr(metadata, "load_aliases", lambda *a, **k: {})
    (row,) = pulse_routes.build_needs_you(1, strict=True)["rows"]
    assert row["id"] == real and row["report"] is not None
    assert reads["n"] == 1  # ONE snapshot carried through
    reads["n"] = 0
    assert needs_you_notify.sync_once()["opened"] == 1  # the sync's own read: one snapshot too
    # Malformed aliases (a real sidecar, read for real): the strict read fails, nothing retracted.
    monkeypatch.setattr(metadata, "load_checked", REAL_LOAD_CHECKED)
    path = metadata._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"__aliases__": {placeholder: 7}}))
    with pytest.raises(needs_you.ReadIncomplete):
        pulse_routes.build_needs_you(1, strict=True)
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert len([n for n in notifications.listing()["notifications"] if n.get("needs_you")]) == 1


def test_F3_an_answered_report_aged_out_of_every_store_is_never_nothing_was_sent(
    hq, world, app, writer, monkeypatch
):
    """F3: a report is answered (delivered) and ends; the history sweep removes it and
    compaction evicts its answer. A retry with the old report id finds no record anywhere: it
    must be `uncertain` ("may have been sent"), never `stale`."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    assert app.answer(rep["id"], 1).status_code == 200
    assert agent_reports.history(A)[0]["ended_by"] == "answered"
    # Seven days later: the sweep removes the ended row; compaction evicts the answer.
    later = time.time() + agent_reports.HISTORY_KEEP_S + 60
    assert agent_reports.sweep(now=later) >= 1
    assert all(r["id"] != rep["id"] for r in agent_reports.history(A))
    _fill_ledger()
    orchestrator_ledger.compact()
    assert not orchestrator_ledger.report_answer_spent(rep["id"])
    r = app.answer(rep["id"], 2)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_UNCERTAIN
    assert len(writer["sent"]) == 1


def test_F3_an_ended_report_keeps_its_answer_evidence_on_its_own_row(hq, world, app, writer):
    """F3: an ordinary answer delivered after the report, whose end failed, then the report is
    replaced: the ENDED row records that an answer went (`answered_by_delivery`), so a retry is
    `spent` even after compaction evicted the ledger row."""
    _env, token = _launch(A)
    assert _file(token)["ok"]
    rep = agent_reports.current(A)
    _hq_answer("hq-1", ts=float(rep["at"]) + 1)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(orchestrator_ledger, "_end_answered_report", lambda *a, **k: None)
        orchestrator_ledger.compare_and_set("hq-1", frozenset({"proposed"}), "delivered")
    agent_reports.file(
        {**DECISION, "summary": "newer"},
        session_key=A,
        phys_key=A,
        engine="claude",
        generation=rep["generation"],
        now=time.time() + 60,
    )
    old = next(r for r in agent_reports.history(A) if r["id"] == rep["id"])
    assert old["ended_by"] == "replaced" and old["answered_by_delivery"] is True
    _fill_ledger()
    orchestrator_ledger.compact()
    assert "hq-1" not in orchestrator_ledger.latest_by_id()
    r = app.answer(rep["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == pulse_routes.REFUSED_SPENT
    assert writer["sent"] == []


def test_F2_the_real_curation_reads_metadata_once_per_strict_read(hq, world, monkeypatch):
    """F2, through the REAL `pulse.build_cards`: one strict Needs-you read takes exactly ONE
    checked metadata snapshot — both card reads (in-window and full) curate through the one the
    report join uses."""
    from agent_sessions import metadata

    reads = {"n": 0}

    def load_checked(*a, **k):
        reads["n"] += 1
        return {}, {}, {}

    monkeypatch.setattr(pulse, "build_cards", REAL_BUILD_CARDS)
    monkeypatch.setattr(pulse, "checked_scan", lambda: [])
    monkeypatch.setattr(metadata, "load_checked", load_checked)
    _env, token = _launch(A)
    assert _file(token)["ok"]  # a current report forces the full (out-of-window) card read too
    pulse_routes.build_needs_you(1, strict=True)
    assert reads["n"] == 1
