"""#1667 — room noise never becomes an HQ turn.

Whisper invents short stock phrases on near-silence and non-speech sound ("Thank you.", "好",
"Obrigado." on the operator's first calls). An utterance that is NOTHING but such a phrase is
dropped before anything else: no HQ turn, no change to the call's language, and an armed spoken
confirmation keeps waiting for a real one. Confirmation and option words are never on the list, a
phrase inside a longer request is never dropped, and neither is a goodbye: whether it means the
call is over is HQ's judgement.
"""

from __future__ import annotations

import pytest

from agent_sessions import voice_text
from test_voice_confirm import NUDGE, Rig, run

# What room noise turned into on the operator's first calls. "Tschüss." was heard too, but a
# goodbye is never dropped: whether it ends the call is HQ's judgement (below).
HEARD_ON_THE_FIRST_CALLS = ["Thank you.", "好", "Obrigado."]


@pytest.mark.parametrize(
    "said",
    HEARD_ON_THE_FIRST_CALLS
    + [
        "thank you",
        "Thanks for watching!",
        "You",
        ".",
        "Danke.",
        "Vielen Dank.",
        "Gracias.",
        "Merci.",
        "谢谢。",
        "ご視聴ありがとうございました",
        "Untertitel der Amara.org-Community",
        "Untertitel im Auftrag des ZDF, 2017",
        "Untertitel im Auftrag des ZDF für funk, 2017",
        "Subtitles by the Amara.org community",
        "Sous-titrage Société Radio-Canada",
        "Sous-titres réalisés par la communauté d'Amara.org",
    ],
)
def test_whisper_stock_phrases_are_no_speech(said):
    assert voice_text.is_no_speech_phrase(said)


@pytest.mark.parametrize(
    "said",
    [
        "yes",
        "no",
        "confirm",
        "confirm the second",
        "cancel",
        "stop",
        "the first one",
        "Thank you, and how many sessions are running?",
        "Danke, und wie viele Sitzungen laufen?",
        "What needs me?",
        "Bye.",
        "Tschüss.",
        "Ciao!",
        "",
        # Requests that merely START like a subtitle credit (#1669 review):
        "Subtitles by Friday, please add them to the release checklist.",
        "Untertitel für das Demo-Video bis morgen erstellen",
        "Sous-titrage for the launch video, can you start it?",
    ],
)
def test_real_requests_and_confirmations_are_not_no_speech(said):
    assert not voice_text.is_no_speech_phrase(said)


@pytest.mark.parametrize("said", HEARD_ON_THE_FIRST_CALLS)
def test_a_stock_phrase_makes_no_turn_and_keeps_the_call_language(monkeypatch, said):
    r = Rig(monkeypatch)
    r.call.detects_language = True

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How many sessions are running?", heard_language="en",
                   language="en")  # fmt: skip
        if r.call.turn_task is not None:
            await r.call.turn_task
        before = len(r.asked)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text=said, heard_language="pt", language="pt")
        if r.call.turn_task is not None:
            await r.call.turn_task
        return before

    before = run(go())
    assert len(r.asked) == before  # no HQ turn for the noise
    assert r.call.language == "en"  # the call did not drift into the phrase's language
    assert r.call.heard_language == "en"


def test_an_armed_confirmation_survives_a_stock_phrase(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests on api-rebase")
        await r.readback_played()
        await r.ask("Thank you.")  # noise while the operator thinks
        assert r.call.challenge is not None  # still waiting for a real answer
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.asked == ["Rerun the tests on api-rebase"]


@pytest.mark.parametrize(
    ("current", "heard", "text", "after"),
    [
        ("en", "de", "Wie viele Sitzungen laufen gerade?", "de"),  # a real request switches
        ("en", "de", "Danke schön", "en"),  # under three words: the call keeps its language
        ("en", "en", "Thanks for watching", "en"),  # three words of noise: never a switch
        ("en", "pt", "Thanks for watching!", "en"),
        ("de", "de", "Untertitel der Amara.org-Community", "de"),
        ("en", None, "How are the sessions?", "en"),  # no language heard: unchanged
    ],
)
def test_the_worker_language_rule_ignores_stock_phrases(current, heard, text, after):
    """The worker keeps the call's language with this rule BEFORE the app sees the utterance, so
    the phrase filter has to hold here too (Hermes on #1667)."""
    assert voice_text.call_language(current, heard, text) == after


@pytest.mark.parametrize("said", ["Tschüss.", "Bye.", "Okay, that's all for now, thanks."])
def test_a_goodbye_reaches_hq_which_judges_whether_the_call_is_over(monkeypatch, said):
    """Whether words mean "the call is over" is HQ's call, never a word list's (operator,
    2026-10-24): a goodbye is never dropped as noise."""
    r = Rig(monkeypatch)

    async def go():
        await r.ask(said)

    run(go())
    assert r.asked == [said]
