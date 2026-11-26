"""HQ sends connector messages, runs automations now and relays to missions itself (#2391, A1b).

The other half of `test_hq_authority_starts.py`: a connector message (the message card's own send,
against the fake Telegram server that records every request), an automation's run now (the
Automations page's own route) and a relay (the mission console's own route) act under *starts:
Act* and stay cards under *Ask*. The act's grant is asked again at the connector's outbound
boundary: a logout or a lowering before a delayed send sends nothing and returns the card. Run
now's class is its scope's, derived at staging, at the claim and at the route; a relay keeps the
mission's own guards, and a terminal session is always relayed on its card. `automation.edit`
and `memory.edit` (the two operations already classed *starts*) act too.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import (
    actuator,
    ask_messages,
    ask_mission,
    ask_operations,
    automation,
    automation_runner,
    hq_acts,
    hq_authority,
    memory,
    memory_index,
    mission_structured,
    missions,
    review,
)
from agent_sessions import automations_store as astore
from agent_sessions import memory_service as service
from agent_sessions.routes import automations as aroutes
from test_ask_library import _ask, _cards, _stored, mission_cwd  # noqa: F401
from test_ask_messages import _messenger, _settled, world  # noqa: F401
from test_ask_operations import _project, live  # noqa: F401
from test_ask_orchestrator import Script, ai, answer, app, call, calls  # noqa: F401
from test_hq_authority import _operator, _set

STARTS = {"reversible": "act", "starts": "act"}


def _record() -> dict:
    return {"class": "starts", "turn_id": "t" * 8, "via": None, "tainted": False}


def _act_on_loop(a, cid, pid, grant=None, **kw):
    """`ask_operations.act` on the app's own loop, which outlives it (as the server's does)."""
    grant = grant or hq_authority.ActGrant.now()

    async def act():
        return await ask_operations.act(
            cid, pid, operator=_operator(a), app=a.app, grant=grant, record=_record(), **kw
        )

    return a.c.portal.call(act)


# ---- connector messages -------------------------------------------------------------------------


def _message_turn(app, monkeypatch, text="The deploy is done.", *, taint=False):
    steps = [call("list_connectors", {}, cid="c1"),
             call("list_recipients", {"connector_index": 0}, cid="c2")]  # fmt: skip
    if taint:
        steps.insert(0, call("list_sessions", {}, cid="c0"))
    script = Script(
        *steps,
        call(
            "propose_message", {"connector_index": 0, "recipient_index": 0, "text": text}, cid="c3"
        ),  # fmt: skip
        answer("Sent."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    turn = app.ask(cid, "tell the room the deploy is done").json()["turn"]
    assert turn["status"] == "done", turn
    return cid, json.loads(script.tool_results(len(steps) + 1)[-1]["content"])


def test_under_starts_act_hq_sends_a_connector_message_in_the_turn(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    _set(**STARTS)
    cid, result = _message_turn(app, monkeypatch)
    assert result.get("ran") == "sent" and result["to"] == "Marcus (@marcus)", result
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    assert card["state"] == "sent" and card["decided_by"] == "hq"
    (sent,) = tg.sent
    assert sent["text"] == "The deploy is done." and sent["chat_id"] == 42
    (row,) = hq_acts.recent()
    assert row["op"] == "message.send" and row["state"] == "sent"
    assert row["title"] == "Telegram · Marcus (@marcus)"


def test_under_ask_a_message_stays_a_card_and_nothing_is_sent(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    _set("standard")
    cid, result = _message_turn(app, monkeypatch)
    assert "ran" not in result and tg.sent == []
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    assert card["state"] == "pending" and card["can_send"]


def test_after_untrusted_text_a_message_stays_a_card_that_says_why(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    _set(**STARTS)
    cid, result = _message_turn(app, monkeypatch, taint=True)
    assert result["ran"] is False and result["why_not"] == hq_authority.TAINT_REASON
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    assert card["state"] == "pending" and card["reason"] == hq_authority.TAINT_REASON
    assert tg.sent == []


def _message_card(app, monkeypatch) -> tuple[str, str]:
    _set("ask_first")
    cid, _ = _message_turn(app, monkeypatch)
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    _set(**STARTS)
    return cid, card["action_id"]


@pytest.mark.parametrize("revoke", ["logout", "lower"])
def test_a_revocation_before_a_delayed_send_sends_nothing_and_returns_the_card(
    world, monkeypatch, revoke
):
    """The grant is asked at the outbound boundary: a logout (or a lowering) while the send waits
    on the room check, the token or the connect sends nothing, and the card is the Commander's."""
    app, tg, _ = world
    _messenger("telegram")
    cid, pid = _message_card(app, monkeypatch)
    grant = hq_authority.ActGrant.now()

    async def revoke_now():
        if revoke == "logout":
            await hq_authority.logged_out()
        else:
            _set("ask_first")

    monkeypatch.setattr(ask_messages, "_HOOK_BEFORE_SEND", revoke_now)
    kind, out = _act_on_loop(app, cid, pid, grant)
    assert kind == "waits", out
    assert tg.sent == []
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    assert card["state"] == "pending" and card["can_send"] and "decided_by" not in card
    lines = [json.loads(x) for x in hq_acts.path().read_text().splitlines()]
    assert [r["state"] for r in lines] == ["deciding", hq_acts.RETURNED]
    assert hq_acts.recent() == []
    # The Commander's own tap sends it, once.
    monkeypatch.setattr(ask_messages, "_HOOK_BEFORE_SEND", None)
    r = app.c.post(f"/api/ask/proposals/{pid}/decide", json={"decision": "send"}, headers=app.hdr)
    assert r.status_code == 202, r.text
    assert _settled(app, cid)["state"] == "sent" and len(tg.sent) == 1


# ---- automation run now -------------------------------------------------------------------------


@pytest.fixture
def runs(monkeypatch):
    """The Automations page's run-now route, with the runner recording what it was asked."""
    asked: list[str] = []

    async def run_now(aid, *, registry):
        asked.append(aid)
        return {"state": "dispatching", "automation_id": aid}

    monkeypatch.setattr(automation_runner, "run_now", run_now)
    monkeypatch.setattr(aroutes, "public_run", lambda r: r)
    return asked


def _run_now(live, ai, aid):
    return _ask(
        live,
        ai,
        calls(("list_automations", {})),
        call("propose_library", {"group": "automation", "action": "run_now", "id": aid}, cid="p"),
        question="run the nightly audit now",
    )


def test_under_starts_act_hq_runs_an_automation_now(live, ai, mission_cwd, runs):
    aid = _stored(enabled=True)
    _set(**STARTS)
    cid, script = _run_now(live, ai, aid)
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result.get("ran") == "done" and "A run started" in result["outcome"], result
    (card,) = _cards(live, cid)
    assert card["op"] == "automation.run_now" and card["decided_by"] == "hq"
    assert runs == [aid]
    assert hq_acts.recent()[0]["op"] == "automation.run_now"


def test_under_ask_run_now_is_a_card_and_its_tap_runs_it(live, ai, mission_cwd, runs):
    from test_ask_operations import _confirm, _settled

    aid = _stored(enabled=True)
    _set("standard")
    cid, _ = _run_now(live, ai, aid)
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and runs == []
    assert _confirm(live, card["action_id"]).status_code == 202
    assert _settled(live, cid)["state"] == "done" and runs == [aid]


def test_an_external_scope_run_now_stays_a_card_under_starts_act(
    live, ai, mission_cwd, runs, monkeypatch
):
    monkeypatch.setattr(hq_authority, "automation_reach", lambda scope: "external")
    aid = _stored(enabled=True)
    _set(**STARTS)
    cid, script = _run_now(live, ai, aid)
    assert "ran" not in json.loads(script.tool_results(2)[-1]["content"])
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and runs == []


def test_a_scope_that_became_external_after_staging_is_a_card_at_the_claim(
    live, ai, mission_cwd, runs, monkeypatch
):
    aid = _stored(enabled=True)
    _set("ask_first")
    cid, _ = _run_now(live, ai, aid)
    (card,) = _cards(live, cid)
    _set(**STARTS)
    monkeypatch.setattr(hq_authority, "automation_reach", lambda scope: "external")
    kind, out = _act_on_loop(live, cid, card["action_id"])
    assert kind == "waits", out
    assert _cards(live, cid)[0]["state"] == "pending" and runs == []


def test_a_scope_that_changes_after_the_claim_is_refused_at_the_route(
    live, ai, mission_cwd, runs, monkeypatch
):
    aid = _stored(enabled=True)
    _set("ask_first")
    cid, _ = _run_now(live, ai, aid)
    (card,) = _cards(live, cid)
    _set(**STARTS)
    reads = {"n": 0}

    def reach(scope):
        reads["n"] += 1
        return "starts" if reads["n"] == 1 else "external"  # the claim's read, then the route's

    monkeypatch.setattr(hq_authority, "automation_reach", reach)
    kind, out = _act_on_loop(live, cid, card["action_id"])
    assert kind == "ran" and out["state"] == "stale", out
    assert "what it would do changed" in out["reason"]
    assert runs == []


def test_automation_edit_acts_under_starts_act(live, ai, mission_cwd):
    aid = _stored(enabled=True)
    _set(**STARTS)
    cid, script = _ask(
        live,
        ai,
        calls(("list_automations", {})),
        call(
            "propose_library",
            {
                "group": "automation",
                "action": "edit",
                "id": aid,
                "policy": {"pause_after_failures": 1},
            },
            cid="p",
        ),  # fmt: skip
    )
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result.get("ran") == "done", result
    assert astore.get(aid)["config"]["policy"]["pause_after_failures"] == 1


def test_memory_edit_acts_under_starts_act_once_its_switch_allows_reading_memory(live, ai):
    """Reading memory taints the turn (agents write it), so memory.edit, a *starts* operation,
    stays a card after `search_memory` unless the Commander switched starts on after untrusted
    text. With the switch on it acts."""
    service.submit(service.operator(), memory.HQ_SCOPE, title="Research protocol",
                   body="Use sources.", origin="one")  # fmt: skip
    memory_index.sync()

    def edit():
        ai(
            call("search_memory", {"query": "research protocol"}),
            call("propose_hq_memory_change",
                 {"ref": "note_1", "action": "edit", "body": "Use primary sources."}),
            answer("Done."),
        )  # fmt: skip
        cid = live.new()
        assert (
            live.ask(cid, "Correct our research protocol. Use primary sources.").status_code == 200
        )
        return live.get(cid)["turns"][-1]["proposals"][0]

    _set(**STARTS)
    card = edit()
    assert card["state"] == "pending" and card["reason"] == hq_authority.TAINT_REASON
    rev = hq_authority.view()["revision"]
    hq_authority.update({"revision": rev, "untrusted": {"starts": True}})
    card = edit()
    assert card["state"] == "done" and card["decided_by"] == "hq"
    assert memory.notes(memory.HQ_SCOPE)[0]["body"] == "Use primary sources."


# ---- relay to a mission -------------------------------------------------------------------------

KEY = "claude-api:11111111-2222-4333-8444-555555555555"


@pytest.fixture
def mission(live, mission_cwd, monkeypatch):
    """A mission that holds one session, the relay route's own door (`actuator.deliver`) recording
    what it was handed. The session is an API session unless a test says otherwise."""
    from agent_sessions import mission_objectives

    # The objective producer (the create's background task, and the recovery pass that retries
    # it) asks the model: here that request would take the scripted reply HQ's turn waits for.
    async def no_producer(_mid):
        return {}

    monkeypatch.setattr(mission_objectives, "propose_for_new_mission", no_producer)
    pid = _project(live, "web")
    r = live.c.post("/api/missions", json={"instruction": "ship it", "project_id": pid},
                    headers=live.hdr)  # fmt: skip
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    state = {"held": {KEY}, "api": True, "delivered": [], "refuse": "", "actions": [],
             "authority": [], "before_write": None}  # fmt: skip
    # HQ's reads of the mission (list, status, the card's facts) answer from memory: a loaded
    # runner's missions store can refuse (busy) or degrade the listing to empty, and neither is
    # what these tests pin. The relay route itself still writes the real mission's record.
    row = {"id": mid, "title": "ship it", "state": "running", "instruction": "ship it",
           "objectives": [], "needs_you": False, "archived_at": None}  # fmt: skip
    real_get = missions.get_mission
    state["real_get"] = real_get

    def get_mission(m, **kw):
        if m != mid:
            return real_get(m, **kw)
        return {**row, "sessions": [{"session_key": k} for k in sorted(state["held"])]}

    monkeypatch.setattr(missions, "get_mission", get_mission)

    async def run_admitted(fn):
        # Off the loop, without the store's admission bound: on a loaded runner the app's own
        # background passes hold its few slots and a read is refused (busy).
        return await asyncio.to_thread(fn)

    monkeypatch.setattr(missions, "run_admitted", run_admitted)
    monkeypatch.setattr(
        missions,
        "safe_list_missions",
        lambda **kw: {"missions": [{**row, "session_keys": sorted(state["held"])}]},
    )
    real_facts = ask_mission._mission_facts
    monkeypatch.setattr(
        ask_mission,
        "_mission_facts",
        lambda m: {
            "title": "ship it",
            "state": "running",
            "archived": False,
            "planned": True,
            "project_id": pid,
        }
        if m == mid
        else real_facts(m),  # fmt: skip
    )
    monkeypatch.setattr(missions, "active_session_keys", lambda m, **kw: set(state["held"]))
    monkeypatch.setattr(mission_structured, "is_api_key", lambda key: state["api"])

    def capture(key, m):
        if state.get("on_capture"):
            state["on_capture"]()  # inside the route, after the act's pre-route check
        return {"mission": m}

    monkeypatch.setattr(automation, "capture", capture)
    monkeypatch.setattr(
        automation, "append_operator_action", lambda rec: state["actions"].append(rec)
    )

    async def deliver(action_id, *, authority=None, **kw):
        # `actuator.deliver`'s door, as these tests need it: its own refusal, then the final
        # guard's `authority` (asked under the write lock before the first byte; a no settles the
        # action stale with nothing typed), then the write.
        state["authority"].append(authority is not None)
        if state["refuse"]:
            raise actuator.NotDeliverable(state["refuse"])
        if state["before_write"]:
            state["before_write"]()
        if authority is not None:
            ok, why = authority({})
            if not ok:
                return {"state": "stale", "detail": why}
        state["delivered"].append(action_id)
        return {"state": "delivered", "detail": ""}

    monkeypatch.setattr(actuator, "deliver", deliver)
    return {"id": mid, "state": state}


def _relay(live, ai, mid, text="The staging deploy is green; go ahead."):
    return _ask(
        live,
        ai,
        # Two requests, not one: tool calls of one reply may run concurrently, and mission_status
        # needs the id list_missions returns.
        calls(("list_missions", {})),
        calls(("mission_status", {"mission_id": mid})),
        call("propose_relay", {"mission_id": mid, "key": KEY, "text": text}, cid="r"),
        question="relay this to the mission",
    )


def test_under_starts_act_hq_relays_to_a_missions_api_session(live, ai, mission):
    _set(**STARTS)
    cid, script = _relay(live, ai, mission["id"])
    result = json.loads(script.tool_results(3)[-1]["content"])
    assert result.get("ran") == "done", result
    (card,) = _cards(live, cid)
    assert card["op"] == "mission.relay" and card["decided_by"] == "hq"
    assert len(mission["state"]["delivered"]) == 1
    # The mission's own record says it came through HQ.
    events = mission["state"]["real_get"](mission["id"], events_limit=50)["events"]
    relayed = [e for e in events if (e.get("meta") or {}).get("relay")]
    assert relayed and relayed[-1]["meta"].get("via") == "hq"


def test_a_relay_into_a_terminal_session_stays_a_card(live, ai, mission):
    mission["state"]["api"] = False
    _set(**STARTS)
    cid, script = _relay(live, ai, mission["id"])
    assert "ran" not in json.loads(script.tool_results(3)[-1]["content"])
    assert _cards(live, cid)[0]["state"] == "pending"
    assert mission["state"]["delivered"] == []


def test_a_relay_the_missions_own_guard_refuses_is_refused_at_its_route(live, ai, mission):
    """A held mission (the mission fence, the master switch, the OFF tier: `actuator.deliver`'s
    own refusal) refuses an act exactly as it refuses a tap: nothing is typed, and it says why."""
    mission["state"]["refuse"] = "the mission is on hold"
    _set(**STARTS)
    cid, script = _relay(live, ai, mission["id"])
    result = json.loads(script.tool_results(3)[-1]["content"])
    assert result.get("ran") == "failed" and "on hold" in result["outcome"], result
    assert mission["state"]["delivered"] == []


def test_a_relay_to_a_session_the_mission_no_longer_holds_is_out_of_date(live, ai, mission):
    _set("ask_first")
    cid, script = _relay(live, ai, mission["id"])
    cards = _cards(live, cid)
    assert len(cards) == 1, [m["content"][:400] for m in script.tool_results(3)]
    (card,) = cards
    mission["state"]["held"].clear()
    _set(**STARTS)
    kind, out = _act_on_loop(live, cid, card["action_id"])
    assert kind == "ran" and out["state"] == "stale" and "no longer holds" in out["reason"]
    assert mission["state"]["delivered"] == []


def test_the_relay_text_is_bounded_and_a_card_shows_it_whole(live, ai, mission):
    _set("ask_first")
    cid, script = _relay(live, ai, mission["id"], text="x" * 241)
    assert _cards(live, cid) == []
    assert "240" in script.tool_results(3)[-1]["content"]


def test_an_act_does_not_count_as_a_card_kind_so_other_changes_may_follow(world, monkeypatch):
    """The one-kind-per-step rule (#2251) sequences cards the Commander confirms. A card HQ ran
    itself waits on nobody, so a rename HQ made and a message it sends share one turn."""
    from agent_sessions import projects

    app, tg, _ = world
    _messenger("telegram")
    pid = _project(app, "scratch")
    _set(**STARTS)
    script = Script(
        call("list_projects", {}, cid="c0"),
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call(
            "propose_project_change",
            {"action": "rename", "project_index": 0, "name": "scratch-2"},
            cid="c3",
        ),  # fmt: skip
        call(
            "propose_message",
            {"connector_index": 0, "recipient_index": 0, "text": "Renamed it."},
            cid="c4",
        ),  # fmt: skip
        answer("Done."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    assert app.ask(cid, "rename it and tell the room").status_code == 200
    message = json.loads(script.tool_results(5)[-1]["content"])
    assert message["ran"] == "sent", message
    assert projects.load()[pid].name == "scratch-2" and len(tg.sent) == 1


@pytest.mark.parametrize("revoke", ["lower", "logout", "call"])
def test_a_revocation_inside_the_relay_route_types_nothing_and_returns_the_card(
    live, ai, mission, revoke
):
    """The act's grant rides into the relay route and is asked in `deliver`'s final guard, under
    the write lock before the first byte (Hermes on #2399): a lowering, a logout or the call
    ending while the route awaits its reads and writes types nothing, and the card is the
    Commander's again with the reason. The mission's own record says the relay did not land."""
    _set("ask_first")
    cid, script = _relay(live, ai, mission["id"])
    cards = _cards(live, cid)
    assert len(cards) == 1, [m["content"][:400] for m in script.tool_results(3)]
    _set(**STARTS)
    on_call = {"live": True}
    grant = hq_authority.ActGrant.now(live=lambda: on_call["live"])

    def revoke_now():
        if revoke == "lower":
            _set("ask_first")
        elif revoke == "logout":
            asyncio.run(hq_authority.logged_out())
        else:
            on_call["live"] = False

    mission["state"]["on_capture"] = revoke_now
    kind, out = _act_on_loop(live, cid, cards[0]["action_id"], grant)
    assert kind == "waits", out
    assert mission["state"]["delivered"] == [] and mission["state"]["authority"] == [True]
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and card["can_confirm"] and "decided_by" not in card
    assert card["reason"]
    events = mission["state"]["real_get"](mission["id"], events_limit=50)["events"]
    relayed = [e for e in events if (e.get("meta") or {}).get("relay")]
    assert relayed and relayed[-1]["meta"].get("state") != "delivered"
    # The Commander's own tap still sends it, without HQ's grant in the way.
    mission["state"]["on_capture"] = None
    from test_ask_operations import _confirm, _settled

    assert _confirm(live, card["action_id"]).status_code == 202
    assert _settled(live, cid)["state"] == "done"
    assert len(mission["state"]["delivered"]) == 1
    assert mission["state"]["authority"] == [True, False]
