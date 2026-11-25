"""Changing and withdrawing an HQ card (#2120).

Pinned here:
- a propose tool's `replaces` stages the corrected card, then settles the named card as replaced
  (dismissed, `replaced_by`); the new card carries `replaces` and its own id comes back as `card`;
- a wrong `replaces` (no such card, another conversation's, another kind) stages nothing;
- a card something else decided first is never relabelled "replaced", and nothing is undone;
- a decision running in the conversation (the decide lock held) leaves the old card as it is;
- two replacements of one card: exactly one wins;
- a failed staging leaves the old card pending, and a failed replacement leaves both pending;
- `withdraw_card` dismisses a pending card of THIS conversation through its own decide route,
  as the turn's operator, and refuses everything else.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_sessions import ask_orchestrator, ask_proposals, ask_store, projects
from test_ask_operations import _project, live  # noqa: F401 — fixtures are used by name
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    _login,
    ai,
    answer,
    app,
    call,
    calls,
)


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _result(script, n: int = 2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


def _rename(a, ai, cid: str, name: str, **extra) -> tuple[dict, object]:
    """One turn proposing a project rename; returns (the propose tool's result, the script)."""
    script = ai(
        calls(("list_projects", {})),
        call(
            "propose_project_change",
            {"action": "rename", "project_index": 0, "name": name, **extra},
            cid="p",
        ),
        answer("Here it is."),
    )
    r = a.ask(cid, f"rename the project to {name}")
    assert r.status_code == 200, r.text
    return _result(script), script


def _first_card(a, ai) -> tuple[str, str]:
    _project(a, "scratch")
    cid = a.new()
    got, _ = _rename(a, ai, cid, "gameplays-promotions")
    assert got["proposed"] is True and ask_proposals.valid_pid(got["card"])
    return cid, got["card"]


# ---- replaces ------------------------------------------------------------------------------------


def test_a_replacement_closes_the_old_card_as_replaced_and_nothing_of_it_runs(live, ai):
    cid, old = _first_card(live, ai)
    got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=old)
    assert got["proposed"] is True and got["replaced"] is True
    assert "nothing of it ran" in got["replaced_note"]
    new = got["card"]
    cards = {c["action_id"]: c for c in _cards(live, cid)}
    assert cards[old]["state"] == "dismissed" and cards[old]["replaced_by"] == new
    assert cards[new]["state"] == "pending" and cards[new]["replaces"] == old
    assert "replaced_by" not in cards[new]
    # Nothing was renamed: neither card was confirmed.
    assert {p.name for p in projects.load().values()} == {"scratch"}


def test_a_replaced_card_can_no_longer_be_confirmed(live, ai):
    cid, old = _first_card(live, ai)
    _rename(live, ai, cid, "game-place-promotions", replaces=old)
    r = live.c.post(
        f"/api/ask/proposals/{old}/decide", json={"decision": "confirm"}, headers=live.hdr
    )
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "dismissed"
    assert {p.name for p in projects.load().values()} == {"scratch"}


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ("0" * 32, "replaces names no card of this conversation; nothing was proposed"),
        ("not-an-id", "replaces must be the action_id of a card you proposed"),
        ("", "replaces must be the action_id of a card you proposed"),
    ],
)
def test_a_wrong_replaces_stages_nothing(live, ai, bad, why):
    cid, old = _first_card(live, ai)
    got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=bad)
    assert got["error"].startswith(f"refused: {why}")
    cards = _cards(live, cid)
    assert [c["action_id"] for c in cards] == [old] and cards[0]["state"] == "pending"


def test_another_conversations_card_cannot_be_replaced(live, ai):
    _cid, old = _first_card(live, ai)
    other = live.new()
    got, _ = _rename(live, ai, other, "game-place-promotions", replaces=old)
    assert "no card of this conversation" in got["error"]
    assert _cards(live, other) == []


def test_a_card_of_another_kind_is_refused_before_anything_stages(live, ai):
    cid, old = _first_card(live, ai)
    why = ask_proposals.replace_refusal(cid, old, "session")
    assert "not a session card" in why and "nothing was proposed" in why


def test_a_card_decided_first_is_never_relabelled_replaced(live, ai):
    cid, old = _first_card(live, ai)
    r = live.c.post(
        f"/api/ask/proposals/{old}/decide", json={"decision": "dismiss"}, headers=live.hdr
    )
    assert r.json()["proposal"]["state"] == "dismissed"
    got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=old)
    assert got["proposed"] is True and got["replaced"] is False
    assert "already dismissed" in got["replaced_note"]
    cards = {c["action_id"]: c for c in _cards(live, cid)}
    assert "replaced_by" not in cards[old]  # a tap dismissed it, not the replacement
    assert "replaces" not in cards[got["card"]]
    assert cards[got["card"]]["state"] == "pending"


def test_a_running_decision_leaves_the_old_card_as_it_is(live, ai, monkeypatch):
    cid, old = _first_card(live, ai)
    monkeypatch.setattr(ask_proposals, "DECIDE_LOCK_WAIT_S", 0.1)
    fd = ask_store.try_decide_lock(cid)  # a decision of this conversation is running
    assert fd is not None
    try:
        got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=old)
    finally:
        os.close(fd)
    assert got["proposed"] is True and got["replaced"] is False
    assert "being decided right now" in got["replaced_note"]
    states = {c["action_id"]: c["state"] for c in _cards(live, cid)}
    assert states == {old: "pending", got["card"]: "pending"}


def test_a_failed_staging_leaves_the_old_card_pending(live, ai):
    cid, old = _first_card(live, ai)
    script = ai(
        call(
            "propose_project_change",
            {"action": "rename", "project_index": 7, "name": "x", "replaces": old},
            cid="p",
        ),
        answer("I couldn't."),
    )
    assert live.ask(cid, "rename it").status_code == 200
    assert "error" in _result(script, 1)
    cards = _cards(live, cid)
    assert [(c["action_id"], c["state"]) for c in cards] == [(old, "pending")]


def test_a_failed_replacement_leaves_both_cards_pending(live, ai, monkeypatch):
    cid, old = _first_card(live, ai)

    async def boom(*_a, **_k):
        raise OSError("disk went away between staging and the compare-and-set")

    monkeypatch.setattr(ask_proposals, "supersede", boom)
    got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=old)
    assert got["proposed"] is True and got["replaced"] is False
    assert "both cards are on screen" in got["replaced_note"]
    states = {c["action_id"]: c["state"] for c in _cards(live, cid)}
    assert states == {old: "pending", got["card"]: "pending"}


def test_two_replacements_of_one_card_exactly_one_wins(live, ai):
    cid, old = _first_card(live, ai)
    b, _ = _rename(live, ai, cid, "first-try")
    c, _ = _rename(live, ai, cid, "second-try")

    async def both():
        return await asyncio.gather(
            ask_proposals.supersede(cid, old, b["card"]),
            ask_proposals.supersede(cid, old, c["card"]),
        )

    outcomes = asyncio.run(both())
    # Exactly one wins. The other finds the card already dismissed — or, on a loaded box, still
    # waits on the decide lock past its patience and leaves it alone (busy). Never two winners.
    assert outcomes.count("replaced") == 1
    assert set(outcomes) - {"replaced"} <= {"dismissed", "busy"}
    row = ask_proposals._row(cid, old)
    assert row["state"] == "dismissed" and row["replaced_by"] in (b["card"], c["card"])


def test_the_store_keeps_replaced_by_only_on_a_dismissed_card():
    entry = {"id": "a" * 32, "turn_id": "11111111-1111-1111-1111-111111111111"}
    with pytest.raises(ValueError):
        ask_store._admit_entry(
            {**entry, "kind": "message", "state": "pending", "replaced_by": "b" * 32},
            [],
            fresh=False,
        )


# ---- withdraw_card -------------------------------------------------------------------------------


def _withdraw(a, ai, cid: str, card: str):
    script = ai(call("withdraw_card", {"card": card}, cid="w"), answer("Dropped."))
    assert a.ask(cid, "dismiss that card").status_code == 200
    return _result(script, 1)


def test_withdraw_dismisses_a_pending_card_of_this_conversation(live, ai):
    cid, old = _first_card(live, ai)
    got = _withdraw(live, ai, cid, old)
    assert got == {"withdrawn": True, "note": "The card is dismissed; nothing of it ran."}
    (card,) = _cards(live, cid)
    assert card["state"] == "dismissed" and "replaced_by" not in card
    assert {p.name for p in projects.load().values()} == {"scratch"}


def test_withdraw_runs_through_the_decide_route_as_the_operator(live, ai, monkeypatch):
    from agent_sessions import asgi_call

    seen: list[tuple] = []
    real = asgi_call.call

    async def spy(app, operator, method, path, body, **kw):
        seen.append((method, path, body, type(operator).__name__))
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", spy)
    cid, old = _first_card(live, ai)
    _withdraw(live, ai, cid, old)
    assert ("POST", f"/api/ask/proposals/{old}/decide", {"decision": "dismiss"}, "Operator") in seen


def test_withdraw_reports_a_card_already_decided_and_changes_nothing(live, ai):
    cid, old = _first_card(live, ai)
    live.c.post(f"/api/ask/proposals/{old}/decide", json={"decision": "dismiss"}, headers=live.hdr)
    got = _withdraw(live, ai, cid, old)
    assert got["withdrawn"] is False and "already dismissed" in got["note"]


def test_withdraw_refuses_another_conversations_card(live, ai):
    _cid, old = _first_card(live, ai)
    other = live.new()
    got = _withdraw(live, ai, other, old)
    assert "no card of this conversation" in got["error"]
    assert ask_proposals._row(_cid, old)["state"] == "pending"


@pytest.mark.parametrize("bad", ["0" * 32, "nope"])
def test_withdraw_refuses_an_unknown_card(live, ai, bad):
    cid, old = _first_card(live, ai)
    got = _withdraw(live, ai, cid, bad)
    assert got["error"].startswith("refused:")
    assert ask_proposals._row(cid, old)["state"] == "pending"


def test_withdraw_refuses_a_turn_without_an_operator(live, ai):
    cid, old = _first_card(live, ai)
    turn = ask_orchestrator._Turn(
        cid=cid, turn_id="t", question="q", working_keys=set(), progress=lambda _e: None
    )  # no operator, no app
    with pytest.raises(ask_orchestrator._ToolRefused) as e:
        asyncio.run(ask_orchestrator._withdraw_card(turn, {"card": old}))
    assert e.value.code == "no_operator"
    assert ask_proposals._row(cid, old)["state"] == "pending"


def test_only_replaceable_propose_tools_take_replaces():
    for name, tool in ask_orchestrator.TOOLS.items():
        props = tool.spec["function"]["parameters"]["properties"]
        assert ("replaces" in props) == (name in ask_orchestrator.REPLACEABLE), name
    # Team cards are screen decisions: never replaceable by voice or by HQ.
    assert not {"assemble", "assign", "set_override", "clear_override"} & set(
        ask_orchestrator.REPLACEABLE
    )


def test_withdraw_takes_no_card_slot_and_works_in_a_full_conversation(live, ai, monkeypatch):
    cid, old = _first_card(live, ai)
    # The conversation holds as many cards as it can: a propose tool is refused, a withdraw is not.
    monkeypatch.setattr(ask_store, "MAX_SESSION_PROPOSALS", 1)
    got = _withdraw(live, ai, cid, old)
    assert got["withdrawn"] is True
    assert ask_proposals._row(cid, old)["state"] == "dismissed"


# ---- list_cards ----------------------------------------------------------------------------------


def test_list_cards_gives_the_ids_and_live_states_newest_first(live, ai):
    cid, old = _first_card(live, ai)
    got, _ = _rename(live, ai, cid, "game-place-promotions", replaces=old)
    script = ai(call("list_cards", {}, cid="l"), answer("Two cards."))
    assert live.ask(cid, "which cards are there?").status_code == 200
    cards = _result(script, 1)["cards"]
    assert [c["card"] for c in cards] == [got["card"], old]
    assert cards[0]["state"] == "pending" and cards[0]["kind"] == "operation"
    assert cards[1] == {
        "card": old,
        "kind": "operation",
        "state": "dismissed",
        "what": cards[1]["what"],
        "replaced_by": got["card"],
    }
    assert cards[1]["what"].startswith("Rename project, for scratch:")


def test_list_cards_shows_only_this_conversations_cards(live, ai):
    _first_card(live, ai)
    other = live.new()
    script = ai(call("list_cards", {}, cid="l"), answer("None."))
    assert live.ask(other, "which cards are there?").status_code == 200
    assert _result(script, 1)["cards"] == []
