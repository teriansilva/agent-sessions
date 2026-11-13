"""#2008 2c — a new-session card whose brief is too long to read back can still be confirmed by
voice (the operator's own approval is recorded on the issue).

On the operator's calls "confirm" never started a session: a card was offered for a spoken
confirmation only when its brief could be read back word for word in 300 characters, and the
briefs HQ wrote ran 350 to 600. Now such a card is ANNOUNCED — what starts, where, and that the
brief was not read — and confirmed like any other. Only a new session: a message and an
operation's effect are still read in full or stay on screen.
"""

from __future__ import annotations

from agent_sessions import voice_calls, voice_text
from test_voice_confirm import Rig, run

LONG = (
    "Conceptualize a teacher and team-building mode. A host creates a room, picks which games "
    "can be played in it, and curates the evening as a playlist: what is played first, second "
    "and third. The room can be branded with colours and a logo. Players join by scanning a QR "
    "code, with no login. At the end the room shows the overall winner and which games people "
    "liked most. Write the concept as a plan with open questions listed at the end."
)
SESSION = {"proposals": [{"kind": "session", "action_id": "prop-9", "title": "Gameplay",
                          "agent": "Claude Code", "text": LONG}]}  # fmt: skip


def test_the_brief_really_is_too_long_to_read_back():
    assert len(LONG) > voice_text.READBACK_MAX and voice_text.verbatim(LONG) is None


def test_a_long_brief_is_announced_not_read_and_marked_so():
    (card,) = voice_calls.confirmable_cards(SESSION)
    assert card["id"] == "prop-9" and card["kind"] == "session" and card["announced"] is True
    assert card["line"] == (
        "A new Claude Code session in Gameplay. Its brief is too long to read out; "
        "it is on your screen."
    )
    assert LONG[:40] not in card["line"]  # never a cut piece passed off as the brief


def test_a_short_brief_is_still_read_word_for_word():
    short = {"kind": "session", "action_id": "p1", "title": "docs", "agent": "Claude Code"}
    view = {"proposals": [{**short, "text": "Refresh the install docs."}]}
    (card,) = voice_calls.confirmable_cards(view)
    assert card["line"] == "A new session in docs: Refresh the install docs."
    assert card["announced"] is False


def test_saying_confirm_starts_the_announced_session_through_its_own_route(monkeypatch):
    r = Rig(monkeypatch, SESSION)

    async def go():
        await r.ask("Start a session for the team-building mode")
        assert r.said_text()[-1] == (
            "A new Claude Code session in Gameplay. Its brief is too long to read out; "
            "it is on your screen. Say confirm to start it, or cancel."
        )
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    decide = [x for x in r.routes if x[1] == "/api/ask/proposals/prop-9/decide"]
    assert decide == [("POST", "/api/ask/proposals/prop-9/decide",
                       {"decision": "start", "bypass": False})]  # fmt: skip
    assert r.call.challenge is None  # single use


def test_the_confirmation_is_bound_to_the_card_that_was_announced(monkeypatch):
    """A changed brief is a NEW card with a new id: the challenge names prop-9 and nothing else,
    so a brief revised after the announcement is never what "confirm" starts."""
    r = Rig(monkeypatch, SESSION)

    async def go():
        await r.ask("Start a session for the team-building mode")
        assert [c["id"] for c in r.call.challenge["cards"]] == ["prop-9"]

    run(go())


def test_a_long_message_or_operation_effect_still_stays_on_screen():
    view = {"proposals": [
        {"kind": "nudge", "action_id": "n1", "title": "api", "text": LONG},
        {"kind": "message", "action_id": "m1", "recipient": "ops", "connector": "matrix",
         "text": LONG},
        {"kind": "operation", "action_id": "o1", "op": "project.archive", "title": "docs",
         "text": LONG},
    ]}  # fmt: skip
    assert voice_calls.confirmable_cards(view) == []


def test_a_session_card_with_no_brief_is_not_offered():
    view = {"proposals": [{"kind": "session", "action_id": "p1", "title": "docs", "text": "  "}]}
    assert voice_calls.confirmable_cards(view) == []


def test_the_agent_name_cannot_smuggle_lines_into_the_announcement():
    view = {"proposals": [{"kind": "session", "action_id": "p1", "title": "docs",
                           "agent": "Claude\nCode  " + "x" * 200, "text": LONG}]}  # fmt: skip
    (card,) = voice_calls.confirmable_cards(view)
    assert "\n" not in card["line"] and len(card["line"]) < 160
