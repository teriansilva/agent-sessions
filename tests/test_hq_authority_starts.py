"""HQ starts sessions, sends messages and runs automations without a card (#2391, #2070 A1b).

On top of A1 (`test_hq_authority.py`): the *starts or sends* class acts. Pinned here: the class
table covers every proposal kind, with the effective-action rule (a bypass start is *widens*, an
automation whose scope reaches outside or can't be read is *external*); a session start (an API
card, bypass off), a connector message, a run now and a relay act under *Act* and stay cards under
*Ask*; a terminal start and a bypass start stay cards; D4: after HQ read untrusted text, these
stay cards that say why, in that turn and the next, unless the class's switch is on (a missing or
malformed switch reads off); the act's grant is re-checked at every effect boundary (before the
create, before the first turn, at the connector's outbound boundary, at the route), with a
refusal before any effect returning the card to the Commander and a refusal between create and
first turn reported as both; the class is derived again at the claim and at the route; two acts
on one card run once; a restart mid-act reads interrupted; and the acts log tells the truth.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_sessions import (
    ask_operations,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    headless_dispatch,
    hq_acts,
    hq_authority,
    mission_structured,
    prefs,
)
from test_ask_operations import (  # noqa: F401 — fixtures are used by name
    ai,
    answer,
    app,
    call,
    calls,
    live,
)
from test_ask_sessions import world  # noqa: F401
from test_hq_authority import _operator, _set

STARTS = {"reversible": "act", "starts": "act"}


def _record(klass: str = "starts", tainted: bool = False) -> dict:
    return {"class": klass, "turn_id": "t" * 8, "via": None, "tainted": tainted}


def _act(a, cid, pid, grant=None, **kw):
    grant = grant or hq_authority.ActGrant.now()
    return asyncio.run(
        ask_operations.act(
            cid, pid, operator=_operator(a), app=a.app, grant=grant, record=_record(), **kw
        )
    )


# ---- the class table ----------------------------------------------------------------------------


def test_the_class_table_is_pinned_against_every_proposal_kind():
    assert set(hq_authority.KIND_CLASS) == set(ask_store.PROPOSAL_KINDS)
    assert set(hq_authority.CLASS_OF) == set(ask_operations.CATALOG)
    assert hq_authority.ACTABLE == {"reversible", "starts", "destructive"}  # + A1c (#2396)
    # The four journeys and the two already-classed starts.
    for op in ("automation.run_now", "mission.relay", "automation.edit", "memory.edit"):
        assert hq_authority.CLASS_OF[op] == "starts", op
    # A nudge keeps its own authority (#1960): it never takes this path.
    assert hq_authority.KIND_CLASS["nudge"] is None


@pytest.mark.parametrize(
    "row, klass",
    [
        ({"kind": "session", "runtime": "api", "bypass_default": True}, "starts"),
        ({"kind": "session", "runtime": "api", "bypass_default": False}, "starts"),
        ({"kind": "session", "bypass_default": False}, "starts"),
        ({"kind": "session", "bypass_default": True}, "widens"),  # the tap would bypass
        ({"kind": "session"}, "widens"),  # unknown: the stronger class
        ({"kind": "message"}, "starts"),
        ({"kind": "operation", "op": "project.rename"}, "reversible"),
        ({"kind": "operation", "op": "automation.run_now", "facts": {"reach": "starts"}}, "starts"),
        (
            {"kind": "operation", "op": "automation.run_now", "facts": {"reach": "external"}},
            "external",
        ),  # fmt: skip
        ({"kind": "operation", "op": "automation.run_now", "facts": {"reach": "widens"}}, "widens"),
        ({"kind": "operation", "op": "automation.run_now", "facts": {}}, "external"),
        ({"kind": "operation", "op": "automation.run_now", "facts": {"reach": "x"}}, "external"),
        ({"kind": "operation", "op": "no.such"}, "widens"),
        ({"kind": "nudge"}, "widens"),
    ],
)
def test_the_effective_class_takes_the_stronger_reading(row, klass):
    assert hq_authority.effective_class(row) == klass


@pytest.mark.parametrize(
    "scope, reach",
    [
        ({"action": "tell_hq"}, "starts"),
        ({"action": "start_mission", "bypass": False}, "starts"),
        ({"action": "send_to_session"}, "starts"),
        ({"action": "start_session", "bypass": False}, "starts"),
        ({"action": "start_session", "bypass": True}, "widens"),
        ({"action": "start_session"}, "widens"),  # no bypass fact: the stronger reading
        ({"action": "plugin"}, "external"),  # a webhook call, a push, a share
        ({"action": "something_new"}, "external"),
        ({}, "external"),
        (None, "external"),  # could not be read
        ("start_session", "external"),
    ],
)
def test_an_automations_reach_is_read_off_its_approved_scope(scope, reach):
    assert hq_authority.automation_reach(scope) == reach


# ---- D4: the "also after untrusted text" switch -------------------------------------------------


def test_the_untrusted_switch_is_off_by_default_cas_written_and_never_set_by_a_preset():
    v = hq_authority.view()
    assert v["untrusted"] == {"starts": False, "destructive": False}
    assert v["untrusted_classes"] == ["starts", "destructive"]
    v = hq_authority.update({"revision": 0, "untrusted": {"starts": True}})
    assert v["untrusted"] == {"starts": True, "destructive": False} and v["revision"] == 1
    with pytest.raises(hq_authority.AuthorityError) as e:
        hq_authority.update({"revision": 0, "untrusted": {"starts": False}})
    assert e.value.status == 409
    # A preset sets the classes, never a switch, in either direction.
    assert hq_authority.update({"revision": 1, "preset": "ask_first"})["untrusted"]["starts"]
    assert hq_authority.update({"revision": 2, "preset": "full"})["untrusted"]["starts"]
    for body in (
        {"revision": 3, "untrusted": {"starts": "true"}},
        {"revision": 3, "untrusted": {"starts": 1}},
        {"revision": 3, "untrusted": {"reversible": True}},  # screen and reversible need none
        {"revision": 3, "untrusted": {"external": True}},  # with its own acting, later
        {"revision": 3, "untrusted": {}},
        {"revision": 3, "untrusted": []},
        {"revision": 3, "untrusted": {"starts": True}, "preset": "full"},
    ):
        with pytest.raises(hq_authority.AuthorityError) as e:
            hq_authority.update(body)
        assert e.value.status == 422, body


@pytest.mark.parametrize(
    "stored", [None, "true", 1, "yes", [], {}, {"starts": "true"}, {"starts": 1}, {"starts": None}]
)
def test_a_missing_or_malformed_switch_reads_off(stored):
    block = {"classes": STARTS, "revision": 3}
    if stored is not None:
        block["untrusted"] = stored
    prefs.mutate_block(hq_authority.KEY, lambda _cur: block)
    snap = hq_authority.snapshot()
    assert hq_authority.acts(snap, "starts")
    assert hq_authority.taint_blocks(snap, "starts", True)
    assert not hq_authority.taint_blocks(snap, "starts", False)
    assert not hq_authority.taint_blocks(snap, "reversible", True)  # epic §1
    assert hq_authority.view()["untrusted"] == {"starts": False, "destructive": False}


def test_the_route_writes_the_switch_against_the_revision(live):
    r = live.c.put(
        "/api/hq/authority", json={"revision": 0, "untrusted": {"starts": True}}, headers=live.hdr
    )
    assert r.status_code == 200 and r.json()["untrusted"]["starts"] is True
    r = live.c.put(
        "/api/hq/authority", json={"revision": 0, "untrusted": {"starts": False}}, headers=live.hdr
    )
    assert r.status_code == 409 and hq_authority.view()["untrusted"]["starts"] is True


# ---- session starts -----------------------------------------------------------------------------


@pytest.fixture
def api_world(world, monkeypatch):
    """`world` (one project, Claude, session assistance on, bypass default off), with Claude an
    API client and `mission_structured.hq_start` a stand-in that runs the card's fenced check
    before the create and before the first turn, as the real one does, and records both."""
    monkeypatch.setattr(ask_proposals, "is_api", lambda engine: True)
    monkeypatch.setattr(ask_proposals, "start_state", lambda engine: (True, ""))
    world.update(created=[], turns=[], contained=[], before_create=None, before_turn=None,
                 slow=0.0, starts=0)  # fmt: skip

    async def hq_start(*, proposal_id, engine, cwd, brief, on_key, check, **_kw):
        world["starts"] += 1
        session_id, _brief_id = mission_structured.hq_ids(proposal_id)
        out = headless_dispatch.Dispatch(
            key=f"{engine}:{session_id}", engine=engine, native=session_id, cwd=cwd
        )
        on_key(out.key)
        if world["before_create"]:
            world["before_create"]()
        ok, why = check()
        if not ok:
            out.reason = why
            return out
        if world["slow"]:
            await asyncio.sleep(world["slow"])
        world["created"].append(out.key)
        out.launched = out.started = True
        if world["before_turn"]:
            world["before_turn"]()
        ok, why = check()
        if not ok:
            out.reason = f"the brief was not delivered: {why}"
            out.seed_outcome = "zero_write"
            out.teardown = "stopped"
            world["contained"].append(out.key)
            return out
        world["turns"].append((out.key, brief))
        out.briefed = True
        out.seed_outcome = "delivered"
        return out

    monkeypatch.setattr(mission_structured, "hq_start", hq_start)
    return world


def _start_turn(ai, *, taint: bool = False):
    reads = [("list_projects", {}), ("list_agents", {})]
    if taint:
        reads.insert(0, ("list_sessions", {}))  # a session's titles: agent-authored text
    return ai(
        calls(*reads),
        call(
            "propose_session",
            {"project_index": 0, "engine_index": 0, "brief": "Add Polish as a UI language."},
            cid="s",
        ),
        answer("Started."),
    )


def _session_result(script) -> dict:
    return json.loads(script.tool_results(2)[-1]["content"])


def _cards(a, cid) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _entry(a, cid) -> dict:
    (row,) = ask_store.read_proposals(cid)
    return row


def test_under_starts_act_hq_starts_an_api_session_at_once(live, ai, api_world):
    _set(**STARTS)
    script = _start_turn(ai)
    cid = live.new()
    assert live.ask(cid, "start a session in api that adds Polish").status_code == 200
    result = _session_result(script)
    assert result.get("ran") == "started" and result["session_key"].startswith("claude:"), result
    (card,) = _cards(live, cid)
    assert card["state"] == "started" and card["decided_by"] == "hq"
    assert card["bypass"] is False and card["session_key"] == result["session_key"]
    entry = _entry(live, cid)
    assert entry["decided_by"] == "hq" and entry["bypass"] is False
    assert api_world["turns"] == [(result["session_key"], "Add Polish as a UI language.")]
    (row,) = hq_acts.recent()
    assert row["op"] == "session.start" and row["state"] == "started"


def test_under_ask_a_start_stays_a_card_and_nothing_starts(live, ai, api_world):
    _set("standard")  # reversible acts; starts asks
    script = _start_turn(ai)
    cid = live.new()
    live.ask(cid, "start a session in api")
    assert "ran" not in _session_result(script)
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and "decided_by" not in card
    assert api_world["starts"] == 0 and hq_acts.recent() == []


def test_a_bypass_start_and_a_terminal_start_stay_cards(live, ai, api_world, monkeypatch):
    _set(**STARTS)
    # A terminal agent whose start the tap would make with bypass on: widens (A1c).
    monkeypatch.setattr(ask_proposals, "is_api", lambda engine: False)
    prefs.set_agent_defaults({"bypass": True})
    script = _start_turn(ai)
    cid = live.new()
    live.ask(cid, "start a session in api")
    assert "ran" not in _session_result(script)
    assert hq_authority.effective_class(_entry(live, cid)) == "widens"
    # The same terminal agent with bypass off: still a card (#1613 D17).
    prefs.set_agent_defaults({"bypass": False})
    script = _start_turn(ai)
    cid2 = live.new()
    live.ask(cid2, "start a session in api")
    assert "ran" not in _session_result(script)
    assert [c["state"] for c in _cards(live, cid) + _cards(live, cid2)] == ["pending", "pending"]
    assert api_world["launches"] == [] and api_world["starts"] == 0


def test_a_hostile_report_read_then_a_start_stays_a_card_that_says_why(live, ai, api_world):
    """The epic's acceptance: HQ read agent-authored text, then "start a session to delete X" is
    a card, nothing runs; the same in the next turn of the conversation; the switch on acts."""
    _set(**STARTS)
    script = _start_turn(ai, taint=True)
    cid = live.new()
    live.ask(cid, "read the report and start a session to delete X")
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result["ran"] is False and "asks first" in result["why_not"], result
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and hq_authority.TAINT_REASON in card["reason"]
    assert api_world["starts"] == 0
    # The next turn of the same conversation carries the taint, without reading anything.
    script = _start_turn(ai)
    live.ask(cid, "now start it")
    assert _session_result(script)["ran"] is False
    assert api_world["starts"] == 0
    # The Commander switched starts on after untrusted text: it acts.
    rev = hq_authority.view()["revision"]
    hq_authority.update({"revision": rev, "untrusted": {"starts": True}})
    script = _start_turn(ai, taint=True)
    live.ask(cid, "and once more")
    assert _session_result(script)["ran"] == "started"
    assert api_world["starts"] == 1
    tainted = [json.loads(x) for x in hq_acts.path().read_text().splitlines()]
    assert tainted and all(r["tainted"] is True for r in tainted)


def _session_card(a, ai) -> tuple[str, str]:
    """A pending API session card staged under Ask: (cid, card id)."""
    _set("ask_first")
    _start_turn(ai)
    cid = a.new()
    a.ask(cid, "start a session in api")
    (card,) = _cards(a, cid)
    _set(**STARTS)
    return cid, card["action_id"]


def test_authority_lowered_between_the_claim_and_the_create_returns_it_with_nothing_created(
    live, ai, api_world
):
    cid, pid = _session_card(live, ai)
    grant = hq_authority.ActGrant.now()
    api_world["before_create"] = lambda: _set("ask_first")
    kind, out = _act(live, cid, pid, grant)
    assert kind == "waits" and "changed the Authority" in out["reason"], out
    assert api_world["created"] == [] and api_world["turns"] == []
    (card,) = _cards(live, cid)
    assert card["state"] == "pending" and card["can_start"] and "decided_by" not in card
    assert card["session_key"] == ""
    entry = _entry(live, cid)
    assert entry["state"] == "pending" and entry.get("decided_by") is None
    # The log says so: claimed, then returned; the reader lists nothing that ran.
    lines = [json.loads(x) for x in hq_acts.path().read_text().splitlines()]
    assert [r["state"] for r in lines] == ["deciding", hq_acts.RETURNED]
    assert hq_acts.recent() == []
    # The Commander's tap still starts it.
    _set("ask_first")
    r = live.decide(pid)
    assert r.status_code == 202
    assert live.settled(cid)["state"] == "started" and len(api_world["turns"]) == 1


def test_a_call_that_ended_between_create_and_first_turn_reports_both_and_never_retries(
    live, ai, api_world
):
    cid, pid = _session_card(live, ai)
    on_call = {"live": True}
    grant = hq_authority.ActGrant.now(live=lambda: on_call["live"])
    api_world["before_turn"] = lambda: on_call.update(live=False)
    kind, card = _act(live, cid, pid, grant)
    assert kind == "ran" and card["state"] == "failed", card
    assert "the session was created, but its first message was not sent" in card["reason"]
    assert "call" in card["reason"] and "nothing is running" in card["reason"]
    assert len(api_world["created"]) == 1 and api_world["turns"] == []
    assert api_world["contained"] == api_world["created"]
    (row,) = hq_acts.recent()
    assert row["state"] == "failed" and "was created" in row["outcome"]
    # Never retried: neither an act nor a tap starts it again.
    _set(**STARTS)
    assert _act(live, cid, pid)[0] == "decided"
    assert live.decide(pid).json()["proposal"]["state"] == "failed"
    assert api_world["starts"] == 1


def test_an_act_after_logout_or_after_the_call_ended_starts_nothing(live, ai, api_world):
    cid, pid = _session_card(live, ai)
    grant = hq_authority.ActGrant.now()
    asyncio.run(hq_authority.logged_out())
    kind, out = _act(live, cid, pid, grant)
    assert kind == "waits" and "signed out" in out["reason"]
    kind, out = _act(live, cid, pid, hq_authority.ActGrant.now(live=lambda: False))
    assert kind == "waits" and "call" in out["reason"]
    assert api_world["starts"] == 0 and _cards(live, cid)[0]["state"] == "pending"


def test_two_concurrent_acts_on_one_start_run_once(live, ai, api_world):
    cid, pid = _session_card(live, ai)
    api_world["slow"] = 0.3
    grant = hq_authority.ActGrant.now()

    async def both():
        op = _operator(live)
        return await asyncio.gather(
            *(ask_operations.act(cid, pid, operator=op, app=live.app, grant=grant,
                                 record=_record(), lock_wait=2.0) for _ in range(2))
        )  # fmt: skip

    results = asyncio.run(both())
    assert sorted(k for k, _ in results) == ["decided", "ran"], results
    assert api_world["starts"] == 1 and len(api_world["turns"]) == 1


def test_a_restart_mid_start_reads_interrupted_and_is_never_replayed(live, ai, api_world):
    cid, pid = _session_card(live, ai)
    ask_proposals._update(cid, pid, state="deciding", decided_at=time.time(), decided_by="hq",
                          authority_rev=1, bypass=False)  # fmt: skip
    hq_acts.append({"pid": pid, "cid": cid, "op": "session.start", "state": "deciding"})
    (row,) = hq_acts.recent()
    assert row["state"] == "interrupted"
    (card,) = _cards(live, cid)
    assert card["state"] == "interrupted" and not card["can_start"]
    assert api_world["starts"] == 0


def test_a_slow_start_past_its_deadline_is_reported_starting_and_settles_on_its_card(
    live, ai, api_world
):
    cid, pid = _session_card(live, ai)
    api_world["slow"] = 0.5
    grant = hq_authority.ActGrant.now()

    async def act():
        return await ask_operations.act(cid, pid, operator=_operator(live), app=live.app,
                                        grant=grant, record=_record(), wait=0.05)  # fmt: skip

    # On the app's own loop, which outlives the act, as the running server's does.
    kind, card = live.c.portal.call(act)
    assert kind == "running" and card["state"] == "deciding"
    end = time.monotonic() + 30  # a loaded runner
    while _cards(live, cid)[0]["state"] == "deciding" and time.monotonic() < end:
        time.sleep(0.05)
    assert _cards(live, cid)[0]["state"] == "started"
    assert hq_acts.recent()[0]["state"] == "started"


def test_a_ten_act_turn_is_bounded_by_its_deadlines_and_budget(live, ai, api_world, monkeypatch):
    """Ten starts in one turn whose launches outlast every deadline (scaled down here; the real
    values and a measured run are in the PR): the turn waits on each effect at most its deadline,
    on the decide lock at most its own, and on all of them together at most the turn's budget.
    What is still starting settles on its card; what could not claim the lock stays a card."""
    monkeypatch.setattr(ask_proposals, "ACT_START_WAIT_S", 0.3)
    monkeypatch.setattr(ask_operations, "ACT_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(ask_orchestrator, "ACT_TURN_BUDGET_S", 1.5)
    api_world["slow"] = 5.0  # unbounded, ten of these would hold the turn for 50 s
    _set(**STARTS)
    proposals = [
        ("propose_session",
         {"project_index": 0, "engine_index": 0, "brief": f"Task {i}: add Polish strings."})
        for i in range(10)
    ]  # fmt: skip
    script = ai(calls(("list_projects", {}), ("list_agents", {})), calls(*proposals),
                answer("Starting."))  # fmt: skip
    cid = live.new()
    began = time.monotonic()
    assert live.ask(cid, "start ten sessions").status_code == 200
    took = time.monotonic() - began
    assert took < 15, took  # the budget (1.5 s) plus a loaded runner's overhead, never 50 s
    results = [json.loads(m["content"]) for m in script.tool_results(2)][2:]
    assert len(results) == 10
    assert results[0]["ran"] == "running" and "starting" in results[0]["note"]
    # The first launch holds the conversation's decide lock: the rest wait at most the lock's
    # deadline (or nothing, once the budget is spent) and stay cards, said so.
    assert all(r["ran"] is False and r["why_not"] for r in results[1:]), results[1:]
    states = sorted(c["state"] for c in _cards(live, cid))
    assert states == ["deciding"] + ["pending"] * 9
