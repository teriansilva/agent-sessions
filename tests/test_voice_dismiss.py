"""#2120 — an HQ card dismissed, changed and explained by voice.

Pinned here:
- "dismiss it" on an armed HQ card dismisses it through the card's own decide route with the
  screen's `{"decision": "dismiss"}`; "cancel" still leaves it on screen;
- the same ordinal rule as a confirm: several cards need the number ("Which one? Say dismiss and
  the number."), "dismiss all" is refused, a negated or questioned dismiss is never one;
- the judge's `dismiss` acts like its `confirm`; on a card that can't be dismissed by voice (a
  nudge, any Needs-you row) a dismiss is a cancel and reaches no route;
- a card that replaced another is read back as "Changed: …"; a replaced card is neither offered
  nor announced;
- a card the call can't confirm by voice says WHY it is on screen.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import asgi_call, voice_calls, voice_judge, voice_text
from test_voice_confirm import NUDGE, Rig

SESSION = {"kind": "session", "action_id": "prop-2", "title": "docs refresh",
           "text": "Refresh the install docs."}  # fmt: skip
OP = {"kind": "operation", "action_id": "prop-3", "op": "project.rename", "title": "scratch",
      "label": "Rename project", "text": "Renames the project to web."}  # fmt: skip


def run(coro):
    return asyncio.run(coro)


def _rig(monkeypatch, *cards: dict) -> Rig:
    r = Rig(monkeypatch, {"proposals": list(cards)})
    recorded = asgi_call.call  # the Rig's own recorder

    async def answering(app, operator, method, path, body, **kw):
        resp = await recorded(app, operator, method, path, body, **kw)
        if body == {"decision": "dismiss"}:
            return asgi_call.Response(200, {"proposal": {"state": "dismissed"}})
        return resp

    monkeypatch.setattr(asgi_call, "call", answering)
    return r


def _dismiss(pid: str) -> tuple:
    return ("POST", f"/api/ask/proposals/{pid}/decide", {"decision": "dismiss"})


@pytest.mark.parametrize("card", [SESSION, OP])
def test_dismiss_it_dismisses_through_the_cards_own_route(monkeypatch, card):
    r = _rig(monkeypatch, card)

    async def go():
        await r.ask("start that")
        await r.readback_played()
        await r.ask("dismiss it")

    run(go())
    assert r.routes == [_dismiss(card["action_id"])]
    assert r.said_text()[-1] == "Dismissed."
    assert r.asked == ["start that"]  # "dismiss it" was not a new question
    assert r.call.challenge is None  # single use


def test_no_dismiss_it_is_a_dismiss_and_cancel_still_leaves_the_card(monkeypatch):
    r = _rig(monkeypatch, SESSION)

    async def go():
        await r.ask("start that")
        await r.readback_played()
        await r.ask("cancel")
        assert r.routes == [] and r.said_text()[-1] == "Okay. It stays on your screen."
        await r.ask("start that again")
        await r.readback_played()
        await r.ask("no, dismiss it")

    run(go())
    assert r.routes == [_dismiss("prop-2")]


@pytest.mark.parametrize("said", ["don't dismiss it", "dismiss it?", "dismiss it later maybe"])
def test_a_negated_or_hedged_dismiss_is_never_one(monkeypatch, said):
    r = _rig(monkeypatch, SESSION)

    async def go():
        await r.ask("start that")
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert not any(b == {"decision": "dismiss"} for _, _, b in r.routes)


def test_several_cards_need_the_number_and_all_is_refused(monkeypatch):
    r = _rig(monkeypatch, SESSION, OP)

    async def go():
        await r.ask("both please")
        await r.readback_played()
        await r.ask("dismiss")
        assert r.said_text()[-1] == "Which one? Say dismiss and the number."
        await r.readback_played()
        await r.ask("dismiss all")
        assert r.said_text()[-1] == "I can only confirm one at a time."
        await r.readback_played()
        await r.ask("dismiss the second one")

    run(go())
    assert r.routes == [_dismiss("prop-3")]


def test_a_nudge_is_not_dismissed_by_voice(monkeypatch):
    """A nudge is a ledger action, not a sidecar card: "dismiss" leaves it, as it always did."""
    r = _rig(monkeypatch, NUDGE["proposals"][0])

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("dismiss it")

    run(go())
    assert r.routes == [] and r.said_text()[-1] == "Okay. It stays on your screen."


def test_the_judges_dismiss_settles_like_its_confirm(monkeypatch):
    r = _rig(monkeypatch, SESSION)
    r.verdict = ("dismiss", 0)

    async def go():
        await r.ask("start that")
        await r.readback_played()
        await r.ask("nee, weg damit")

    run(go())
    assert r.judged and r.judged[-1][2] == "nee, weg damit"
    assert r.routes == [_dismiss("prop-2")]


def test_the_judges_dismiss_on_a_nudge_is_a_cancel(monkeypatch):
    r = _rig(monkeypatch, NUDGE["proposals"][0])
    r.verdict = ("dismiss", 0)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("schmeiß das weg")

    run(go())
    assert r.routes == [] and r.said_text()[-1] == "Okay. It stays on your screen."


def test_a_dismiss_after_the_window_closed_does_nothing(monkeypatch):
    r = _rig(monkeypatch, SESSION)
    monkeypatch.setattr(voice_calls, "CHALLENGE_S", 0.01)

    async def go():
        await r.ask("start that")
        await r.readback_played()
        await asyncio.sleep(0.05)
        await r.ask("dismiss it")

    run(go())
    assert r.routes == []


@pytest.mark.parametrize("kind", ["permission", "menu", "pick", "needs_you", "nudge"])
def test_needs_you_rows_and_nudges_are_never_dismissable(kind):
    assert not voice_calls._dismissable([{"kind": kind}])
    assert voice_calls._dismissable([{"kind": "session"}, {"kind": "operation"}])


def test_the_judge_parses_dismiss_like_confirm():
    p = voice_judge.parse_card_verdict
    assert p("dismiss", 1) == ("dismiss", 0)
    assert p("dismiss 2", 2) == ("dismiss", 1)
    assert p("dismiss", 2) == ("ambiguous_dismiss", None)
    assert p("dismiss 3", 2) == ("ambiguous_dismiss", None)
    assert p("cancel 2", 2) is None


# ---- a changed card, and why a card stays on screen ---------------------------------------------


def test_a_card_that_replaced_another_is_read_back_as_a_change(monkeypatch):
    r = _rig(monkeypatch, {**SESSION, "replaces": "a" * 32})

    async def go():
        await r.ask("change the brief")

    run(go())
    assert r.said_text()[-1] == (
        "Changed: A new session in docs refresh: Refresh the install docs. "
        "Say confirm to start it, or cancel."
    )


def test_a_replaced_card_is_neither_offered_nor_announced():
    view = {"proposals": [{**SESSION, "state": "dismissed", "replaced_by": "b" * 32}]}
    assert voice_calls.confirmable_cards(view) == []
    assert voice_text.card_sentences(view) == []


def test_a_project_without_its_folder_says_why_it_is_on_screen():
    card = {"kind": "operation", "action_id": "c" * 32, "op": "project.create",
            "title": "Game Place Promotions", "label": "Create project", "needs": ["folder"],
            "text": "Creates the project in the folder you choose below."}  # fmt: skip
    view = {"proposals": [card]}
    reasons = voice_calls.screen_reasons(view, suggestions={})
    assert voice_text.card_sentences(view, reasons=reasons) == [
        "I've drafted this: Create project, for Game Place Promotions. Its folder doesn't exist "
        "yet, and I can't pick a new one by voice. Confirm it on your screen."
    ]


@pytest.mark.parametrize(
    ("op", "why"),
    [
        ("mission.create", "A draft mission is confirmed on screen for now."),
        ("secret.create", "Secrets are only ever confirmed on screen."),
    ],
)
def test_screen_only_groups_say_why(op, why):
    from agent_sessions import ask_operations

    if op not in ask_operations.CATALOG:
        pytest.skip(f"{op} is not in this catalog")
    card = {"kind": "operation", "action_id": "d" * 32, "op": op, "title": "x", "text": "y."}
    assert voice_calls.screen_reasons({"proposals": [card]}) == {"d" * 32: why}
