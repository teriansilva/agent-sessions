"""HQ drives API sessions (#2397, epic #2070 A7): it sends the next step into a session it drives,
reads the reply, and closes it; the Commander takes over by typing; a mission keeps its sessions.

The real path end to end: an HQ turn over the scripted AI endpoint (`ai`) stages a catalog drive
entry, which acts (or waits on its card) through the in-process route, admitted by the card's
binding inside the write fence, into the real native worker over the fake native child (`host`).
The race tests are in `test_session_driver.py`."""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio

from agent_sessions import ask_store, hq_acts, session_driver
from agent_sessions import structured_runtime as runtime
from drive_harness import (  # noqa: F401
    card,
    card_row,
    drive_from,
    fleet,
    new_session,
    result,
    users,
)
from test_ask_orchestrator import ai, answer, app, call  # noqa: F401
from test_hq_authority import _set
from test_native_runtime import host, project, settle  # noqa: F401

STEP = "Run the i18n tests and fix every missing pl key."


def _send_turn(app, ai, fleet, key, *, cid=None, message=STEP, read_first=False):
    fleet["cards"] = [card_row(key)]
    # The session HQ drives from this conversation is a known key: no search (a search reads
    # agent-authored titles, which taints the turn).
    steps = []
    if read_first:
        steps.append(call("session_turns", {"key": key}, cid="r"))
    steps += [
        call("propose_session_change", {"action": "send", "key": key, "message": message}, cid="s"),
        answer("Sent."),
    ]
    script = ai(*steps)
    cid = cid or app.new()
    return cid, script


def test_under_starts_act_an_untainted_turn_sends_one_turn_into_a_driven_session(
    app, ai, host, project, fleet
):
    _set(reversible="act", starts="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    cid, script = _send_turn(app, ai, fleet, key, cid=cid)
    assert app.ask(cid, "have the web shop session fix the strings").status_code == 200
    got = result(script, "s")
    assert got.get("ran") == "done", repr(got)[:900]
    row = card(cid, "session.send")
    assert row["state"] == "done" and row["decided_by"] == "hq"
    assert users(project).count(STEP) == 1
    snap = asyncio.run(runtime.snapshot(key))
    sent = [t for t in snap["turns"] if t.get("text") == STEP]
    assert len(sent) == 1
    assert sent[0]["turn_id"] == session_driver.send_operation_id(row["id"])
    assert session_driver.sent_by_hq(sent[0])
    (logged,) = [a for a in hq_acts.recent() if a["op"] == "session.send"]
    assert logged["state"] == "done"


def test_under_ask_a_send_is_a_card_and_nothing_is_sent(app, ai, host, project, fleet):
    _set(reversible="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    cid, script = _send_turn(app, ai, fleet, key, cid=cid)
    app.ask(cid, "send it")
    assert result(script, "s").get("ran") in (None, False)
    assert card(cid, "session.send")["state"] == "pending"
    assert STEP not in users(project)
    assert ask_store.read_proposals(cid)


# ---- reading and waiting ------------------------------------------------------------------------


def _first_turn(key, text="look at the strings"):
    from test_native_runtime import ident

    tid = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=tid, text=text))
    asyncio.run(settle(key, tid))
    return tid


def test_session_turns_reads_settled_replies_with_a_cursor_and_taints(
    app, ai, host, project, fleet
):
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    first = _first_turn(key)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("session_turns", {"key": key}, cid="r"),
        call("session_turns", {"key": key, "after": first}, cid="r2"),
        answer("Read."),
    )
    app.ask(cid, "what did it say?")
    got = result(script, "r")
    assert got["driver"] == {"who": "hq", "from_this_conversation": True}
    (row,) = got["turns"]
    assert row["id"] == first and row["from"] == "you" and row["state"] == "completed"
    assert row["untrusted_reply"] == "echo:look at the strings"
    assert got["cursor"] == first
    assert result(script, "r2")["turns"] == []
    # Agent-authored text: the turn is tainted (its settled status says so).
    status = [r for r in ask_store.read(cid) if r.get("type") == "status"][-1]
    assert status.get("tainted") is not False


def test_d4_after_a_reply_was_read_the_next_send_is_a_card_with_the_reason(
    app, ai, host, project, fleet
):
    _set(reversible="act", starts="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    _first_turn(key)
    cid, script = _send_turn(app, ai, fleet, key, cid=cid, read_first=True)
    app.ask(cid, "keep going")
    got = result(script, "s")
    assert got.get("ran") is False and "asks first" in got["why_not"], got
    row = card(cid, "session.send")
    assert row["state"] == "pending" and "asks first" in row["reason"]
    assert STEP not in users(project)
    # The next turn of the conversation carries the taint, without reading anything.
    script = ai(
        call("propose_session_change", {"action": "send", "key": key, "message": "again"},
             cid="s"),
        answer("Proposed."),
    )  # fmt: skip
    app.ask(cid, "send again")
    assert "asks first" in result(script, "s")["why_not"]
    assert "again" not in users(project)


def test_d4_with_the_untrusted_starts_switch_on_the_send_acts(app, ai, host, project, fleet):
    from agent_sessions import hq_authority

    _set(reversible="act", starts="act")
    rev = hq_authority.view()["revision"]
    hq_authority.update({"revision": rev, "untrusted": {"starts": True}})
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    _first_turn(key)
    cid, script = _send_turn(app, ai, fleet, key, cid=cid, read_first=True)
    app.ask(cid, "keep going")
    assert result(script, "s").get("ran") == "done"
    assert users(project).count(STEP) == 1


def test_adopt_in_a_tainted_turn_is_a_card(app, ai, host, project, fleet):
    _set(reversible="act", starts="act")
    key = new_session(project)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),  # titles: tainting
        call("propose_session_change", {"action": "adopt", "key": key}, cid="a"),
        answer("Proposed."),
    )
    cid = app.new()
    app.ask(cid, "take the web shop session over")
    got = result(script, "a")
    assert got.get("ran") is False and "asks first" in got["why_not"], got
    assert session_driver.record(key) is None


def test_wait_session_reports_a_completed_turn_and_its_bounded_reply(app, ai, host, project, fleet):
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    long = "x" * 5000
    tid = _first_turn(key, long)
    fleet["cards"] = [card_row(key)]
    script = ai(call("wait_session", {"key": key, "turn_id": tid, "seconds": 5}, cid="w"),
                answer("Done."))  # fmt: skip
    app.ask(cid, "wait for it")
    got = result(script, "w")
    assert got["settled"] is True and got["state"] == "completed"
    assert len(got["untrusted_reply"]) == 4000 and got["reply_truncated"] is True
    assert got["untrusted_reply"].endswith("x")  # the newest part is kept


def test_wait_session_returns_at_its_deadline_holding_no_lock(
    app, ai, host, project, fleet, monkeypatch
):
    from agent_sessions import session_input
    from test_native_runtime import ident

    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    hang = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=hang, text="HANG"))
    asyncio.run(settle(key, hang, state=("running",)))
    fleet["cards"] = [card_row(key)]
    script = ai(call("wait_session", {"key": key, "seconds": 2}, cid="w"), answer("Waiting."))
    held: list[bool] = []
    real = runtime.snapshot

    async def spy(*a, **kw):
        held.append(session_input._lock.locked())
        return await real(*a, **kw)

    monkeypatch.setattr(runtime, "snapshot", spy)
    app.ask(cid, "wait")
    got = result(script, "w")
    assert got["settled"] is False and got["state"] == "running" and got["turn_id"] == hang
    assert 1.5 <= got["waited_seconds"] <= 8
    assert held and not any(held)


def test_wait_session_stops_when_the_task_is_stopped(host, project):
    from agent_sessions import ask_orchestrator
    from test_native_runtime import ident

    key = new_session(project)
    hang = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=hang, text="HANG"))
    asyncio.run(settle(key, hang, state=("running",)))

    async def go():
        turn = ask_orchestrator._Turn.__new__(ask_orchestrator._Turn)
        turn.stop = asyncio.Event()
        turn.keys = {key}
        turn.cid = "c" * 32
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, turn.stop.set)
        began = loop.time()
        out, _meta = await ask_orchestrator._wait_session(turn, {"key": key, "seconds": 60})
        return out, loop.time() - began

    out, took = asyncio.run(go())
    assert out["settled"] is False and "Stop" in out["note"]
    assert took < 5.0


def test_a_terminal_session_is_refused_by_every_drive_tool(app, ai, host, project, fleet):
    key = "claude:00000000-0000-4000-8000-0000000000ab"
    fleet["cards"] = [card_row(key, "claude")]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call("propose_session_change", {"action": "send", "key": key, "message": "x"}, cid="s"),
        call("propose_session_change", {"action": "adopt", "key": key}, cid="a"),
        call("session_turns", {"key": key}, cid="t"),
        call("wait_session", {"key": key}, cid="w"),
        answer("No."),
    )
    cid = app.new()
    app.ask(cid, "drive it")
    for c in ("s", "a", "t", "w"):
        assert "API sessions only" in result(script, c)["error"], c


# ---- send_nudge, list_sessions, session_status, close ---------------------------------------


def test_send_nudge_into_a_session_hq_drives_is_a_drive_send(app, ai, host, project, fleet):
    _set(reversible="act", starts="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    fleet["cards"] = [card_row(key)]
    script = ai(call("send_nudge", {"key": key, "text": STEP}, cid="n"), answer("Sent."))
    app.ask(cid, "tell it to fix the strings")
    got = result(script, "n")
    assert got["as"] == "session.send" and got.get("ran") == "done", got
    assert card(cid, "session.send")["decided_by"] == "hq"
    assert users(project).count(STEP) == 1


def test_send_nudge_into_a_driven_session_keeps_d4(app, ai, host, project, fleet):
    _set(reversible="act", starts="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    _first_turn(key)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("session_turns", {"key": key}, cid="r"),
        call("send_nudge", {"key": key, "text": STEP}, cid="n"),
        answer("Proposed."),
    )
    app.ask(cid, "tell it")
    got = result(script, "n")
    assert got["as"] == "session.send" and got.get("ran") is False, got
    assert STEP not in users(project)


def test_send_nudge_into_a_session_hq_does_not_drive_stages_no_drive_card(
    app, ai, host, project, fleet
):
    key = new_session(project)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call("send_nudge", {"key": key, "text": STEP}, cid="n"),
        answer("Sent."),
    )
    cid = app.new()
    app.ask(cid, "tell it")
    assert "as" not in result(script, "n")
    assert not [r for r in ask_store.read_proposals(cid) if r.get("op") == "session.send"]


def test_list_sessions_reads_who_drives_each_in_one_batch(
    app, ai, host, project, fleet, monkeypatch
):
    from agent_sessions import missions

    keys = [new_session(project) for _ in range(2)]
    cid = app.new()
    drive_from(keys[0], cid)
    calls_ = {"views": 0, "missions": 0}
    real_views, real_members = session_driver.views, missions.all_active_memberships

    def views(ks):
        calls_["views"] += 1
        return real_views(ks)

    def members(**kw):
        calls_["missions"] += 1
        return real_members(**kw)

    monkeypatch.setattr(session_driver, "views", views)
    monkeypatch.setattr(missions, "all_active_memberships", members)
    script = ai(call("list_sessions", {}, cid="l"), answer("Listed."))
    app.ask(cid, "list my sessions")
    rows = {r["key"]: r for r in result(script, "l")["sessions"]}
    assert rows[keys[0]]["driver"] == {"who": "hq", "from_this_conversation": True}
    assert rows[keys[1]]["driver"] == {"who": "you"}
    assert calls_ == {"views": 1, "missions": 1}


def test_session_status_of_an_api_session_has_its_structured_state(app, ai, host, project, fleet):
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    fleet["cards"] = [card_row(key)]
    script = ai(call("session_status", {"key": key}, cid="st"), answer("Idle."))
    app.ask(cid, "how is it?")
    got = result(script, "st")
    assert got["api"]["state"] == "idle" and got["api"]["active_turn"] is None
    assert got["driver"]["who"] == "hq"


def _plain_project(monkeypatch):
    """The test's folder is a pytest temp path, which the store boundary reads as
    credential-shaped: name the project plainly, as a real one is."""
    from agent_sessions import ask_operations

    real = ask_operations._session_facts

    async def facts(app_, operator, key):
        out = await real(app_, operator, key)
        return {**out, "project": "web-shop"} if out else out

    monkeypatch.setattr(ask_operations, "_session_facts", facts)


def test_close_under_destructive_act_closes_a_driven_api_session(
    app, ai, host, project, fleet, monkeypatch
):
    _plain_project(monkeypatch)
    _set(reversible="act", starts="act", destructive="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("propose_session_change", {"action": "close", "key": key}, cid="c"), answer("Closed.")
    )
    app.ask(cid, "the tests pass, close it")
    got = result(script, "c")
    assert got.get("ran") == "done", got
    assert card(cid, "session.close")["decided_by"] == "hq"


def test_close_under_destructive_ask_is_a_card(app, ai, host, project, fleet, monkeypatch):
    _plain_project(monkeypatch)
    _set(reversible="act", starts="act")
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("propose_session_change", {"action": "close", "key": key}, cid="c"), answer("Ok.")
    )
    app.ask(cid, "close it")
    assert result(script, "c").get("ran") in (None, False)
    assert card(cid, "session.close")["state"] == "pending"


def test_hq_started_api_session_is_recorded_as_hqs_before_the_create(monkeypatch):
    from agent_sessions import ask_proposals, mission_structured

    cid = ask_store.create()
    row = {"id": "a" * 32, "engine": "codex-api", "project": "web-shop"}
    ask_proposals._record_driver(cid, row)
    sid, _brief = mission_structured.hq_ids(row["id"])
    rec = session_driver.record(f"codex-api:{sid}")
    assert rec["who"] == "hq" and rec["cid"] == cid and rec["generation"] == 1


def test_session_turns_fits_the_tool_result_limit_with_twelve_long_escaped_turns(
    app, ai, host, project, fleet
):
    """Hermes review 9019: the WHOLE result is budgeted, not only the replies. Twelve turns whose
    text and replies are long and expand under JSON escaping (quotes, backslashes, newlines,
    non-ASCII) go through the real `_call` and come back bounded, never `too_large`, with a
    cursor HQ can page from."""
    import json

    from agent_sessions import ask_orchestrator

    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    chunk = 'He said "fix C:\\\\web\\\\shop" — größer\n'
    tids = [_first_turn(key, f"{i}: " + chunk * 120) for i in range(12)]
    fleet["cards"] = [card_row(key)]
    script = ai(call("session_turns", {"key": key}, cid="r"), answer("Read."))
    app.ask(cid, "what did it say?")
    got = result(script, "r")
    assert "error" not in got, got
    assert len(json.dumps(got, ensure_ascii=False)) <= ask_orchestrator.TOOL_RESULT_MAX
    ids = [r["id"] for r in got["turns"]]
    assert ids and ids[-1] == tids[-1]  # the newest turn is always kept
    assert got["cursor"] == tids[-1]  # every returned turn settled: HQ reads on from here
    assert got["turns"][-1].get("untrusted_reply")  # the newest reply is delivered
    assert got["earlier_turns_left_out"] or len(ids) == 12


def test_fitting_drops_the_oldest_rows_last_and_keeps_the_newest():
    """The fit, unit-sized: when trimming text is not enough, the oldest rows go; the cursor's
    walk still sees them (`_fit_turns` returns them)."""
    import json

    from agent_sessions import ask_orchestrator

    big = '"\\\\' * 1500  # escapes to twice its length and more
    rows = [
        {"id": f"t{i}", "from": "you", "state": "completed", "untrusted_reply": big}
        for i in range(12)
    ]
    out = {"turns": rows, "earlier_turns_left_out": False}
    dropped = ask_orchestrator._fit_turns(out)
    assert len(json.dumps(out, ensure_ascii=False)) <= ask_orchestrator.DRIVE_RESULT_MAX
    assert out["turns"][-1]["id"] == "t11" and out["turns"][-1].get("untrusted_reply")
    # Many rows: once no text is left to trim, the oldest rows go, oldest first.
    rows = [{"id": f"t{i:03}", "from": "you", "state": "completed"} for i in range(400)]
    out = {"turns": rows, "earlier_turns_left_out": False}
    dropped = ask_orchestrator._fit_turns(out)
    assert len(json.dumps(out, ensure_ascii=False)) <= ask_orchestrator.DRIVE_RESULT_MAX
    assert dropped and out["earlier_turns_left_out"] is True
    assert [r["id"] for r in dropped] == [f"t{i:03}" for i in range(len(dropped))]
    assert out["turns"][-1]["id"] == "t399"
