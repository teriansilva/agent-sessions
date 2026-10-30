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

from agent_sessions import voice_calls, voice_text
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


# --- #1807: HQ's own voice heard back, and other languages --------------------------------------

# What the operator's calls actually turned into HQ turns (2026-10-27/29).
HQ_HEARD_BACK = "Hey, what's up One moment, Commander."
OTHER_LANGUAGES = [
    ("Espero que siga en el Hotel Región.", "es"),
    ("Ég æt brafnum við höfður í núna.", "is"),
    ("Hei, búna! Cê faz?", "pt"),
]


@pytest.mark.parametrize(
    ("heard", "said", "kept"),
    [
        (HQ_HEARD_BACK, ["One moment, Commander."], "Hey, what's up"),
        ("One moment, Commander.", ["One moment, Commander."], ""),  # nothing but the echo
        ("checking now commander", ["Checking now, Commander."], ""),  # case and commas aside
        # Two words in a row are not evidence: the operator's own words stay.
        ("Open the session", ["Shall I open the docs session?"], "Open the session"),
        ("Yes, confirm.", ["Say confirm to send it, or cancel."], "Yes, confirm."),
        ("How many sessions are running?", [], "How many sessions are running?"),
        # Every echoed stretch goes; the operator's words around them are kept as said.
        (
            "Two auth tests fail. Rerun them please",
            ["Two auth tests fail on api-rebase."],
            "Rerun them please",
        ),
    ],
)
def test_strip_echo_removes_only_runs_of_hqs_own_words(heard, said, kept):
    assert voice_text.strip_echo(heard, said) == kept


async def _hq_says_then_operator(r, line: str, heard: str, *, played_first: bool = False):
    sid = await r.call.say(line, kind="ack")
    if played_first:
        await r.call.on_played(sid)
    r.turn += 1
    await r.ev(t="user_started", turn=r.turn)
    await r.ev(t="utterance", text=heard, turn=r.turn, heard_language="en", language="en")
    if r.call.turn_task is not None:
        await r.call.turn_task


def test_hqs_line_heard_back_is_cut_from_the_operators_words(monkeypatch):
    r = Rig(monkeypatch)
    run(_hq_says_then_operator(r, "One moment, Commander.", HQ_HEARD_BACK))
    assert r.asked == ["Hey, what's up"]


def test_an_utterance_that_is_only_hqs_line_makes_no_turn(monkeypatch):
    r = Rig(monkeypatch)
    run(_hq_says_then_operator(r, "One moment, Commander.", "One moment, Commander."))
    assert r.asked == []


def test_a_line_long_played_out_is_no_echo(monkeypatch):
    """Only a line that could still be coming out of the speaker counts: once it has played and
    its tail has passed, the operator quoting it is the operator speaking."""
    r = Rig(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(voice_calls.time, "monotonic", lambda: clock[0])

    async def go():
        sid = await r.call.say("Checking now, Commander.", kind="ack")
        await r.call.on_played(sid)
        clock[0] += voice_calls.ECHO_TAIL_S + 5
        r.turn += 1
        await r.ev(t="user_started", turn=r.turn)
        await r.ev(t="utterance", text="Checking now, Commander.", turn=r.turn,
                   heard_language="en", language="en")  # fmt: skip
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert r.asked == ["Checking now, Commander."]


def test_a_late_transcript_is_left_as_heard(monkeypatch):
    r = Rig(monkeypatch)

    async def go():
        await r.call.say("One moment, Commander.", kind="ack")
        await r.ev(t="utterance", text=HQ_HEARD_BACK, late=True, heard_language="en")
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert r.asked == [HQ_HEARD_BACK]


@pytest.mark.parametrize(("said", "lang"), OTHER_LANGUAGES)
def test_an_utterance_in_a_language_hq_does_not_listen_in_makes_no_turn(monkeypatch, said, lang):
    r = Rig(monkeypatch)
    r.call.detects_language = True
    r.call.listen_languages = voice_calls.listen_languages({"tts_languages": ["en", "de"]})

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How many sessions are running?", heard_language="en",
                   language="en")  # fmt: skip
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.ev(t="user_started")
        await r.ev(t="utterance", text=said, heard_language=lang, language=lang)
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert r.asked == ["How many sessions are running?"]
    assert r.call.language == "en" and r.call.heard_language == "en"


@pytest.mark.parametrize(("said", "lang"), [("Wie viele Sitzungen laufen?", "de"),
                                            ("How many are running?", "en"),
                                            ("How many are running?", None)])  # fmt: skip
def test_the_languages_hq_answers_in_are_heard(monkeypatch, said, lang):
    r = Rig(monkeypatch)
    r.call.listen_languages = voice_calls.listen_languages({"tts_languages": ["en", "de"]})
    run(r.ask(said) if lang is None else _utter(r, said, lang))
    assert r.asked == [said]


async def _utter(r, said, lang):
    await r.ev(t="user_started")
    await r.ev(t="utterance", text=said, heard_language=lang, language=lang)
    if r.call.turn_task is not None:
        await r.call.turn_task


@pytest.mark.parametrize(
    ("settings", "langs"),
    [
        ({"tts_languages": ["en", "de"]}, {"en", "de"}),
        ({"tts_languages": ["en"], "language": "de"}, {"en", "de"}),  # the fixed one too
        ({"tts_languages": [], "language": "de"}, None),  # nothing listed: no filter
        ({}, None),
        ({"tts_languages": ["en", "bogus!"]}, {"en"}),
    ],
)
def test_listen_languages_are_the_ones_hq_can_answer_in(settings, langs):
    got = voice_calls.listen_languages(settings)
    assert (set(got) if got is not None else None) == langs


def test_a_foreign_language_never_becomes_the_calls_language_in_the_worker_rule():
    """Hermes 7431: the worker updates the call's language BEFORE the app drops the utterance."""
    listen = voice_calls.listen_languages({"tts_languages": ["en", "de"]})
    assert voice_text.call_language("en", "pt", "Hei, búna! Cê faz?", listen) == "en"
    assert voice_text.call_language("en", "de", "Wie viele Sitzungen laufen?", listen) == "de"
    assert (
        voice_text.call_language("en", "pt", "Hei, búna! Cê faz?") == "pt"
    )  # no filter: as before


def test_foreign_noise_then_a_short_english_confirm_still_confirms(monkeypatch):
    """Hermes 7431, across the worker/app boundary: three words of Portuguese noise, then
    "confirm". The worker's language (computed by its own rule, `voice_text.call_language`, fed
    the listen set it is configured with) and the app's both stay English, so the confirmation is
    heard as English and settles the card."""
    settings = {"tts_languages": ["en", "de"]}
    r = Rig(monkeypatch, NUDGE)
    r.call.detects_language = True
    r.call.listen_languages = voice_calls.listen_languages(settings)
    worker_listen = frozenset(voice_calls.worker_config(_SETTINGS, "aria")["listen_languages"])
    worker_lang = ["en"]

    async def say(text, heard):
        worker_lang[0] = voice_text.call_language(worker_lang[0], heard, text, worker_listen)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text=text, heard_language=heard, language=worker_lang[0])
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    async def go():
        await say("Rerun the tests on api-rebase", "en")
        await r.readback_played()
        await say("Hei, búna! Cê faz?", "pt")  # noise in another language: dropped
        await say("confirm", "en")

    run(go())
    assert worker_lang[0] == "en" and r.call.language == "en"
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.asked == ["Rerun the tests on api-rebase"]


def test_a_poisoned_worker_language_is_never_adopted_by_the_app(monkeypatch):
    """Defence in depth: even a worker that tags an English utterance with a language the call
    does not listen in (an older worker, stale state) cannot move the app's call language."""
    r = Rig(monkeypatch)
    r.call.language = "en"
    r.call.listen_languages = voice_calls.listen_languages({"tts_languages": ["en", "de"]})
    run(_tagged(r, "How many sessions are running?", heard="en", lang="pt"))
    assert r.call.language == "en"


async def _tagged(r, text, *, heard, lang):
    await r.ev(t="user_started")
    await r.ev(t="utterance", text=text, heard_language=heard, language=lang)
    if r.call.turn_task is not None:
        await r.call.turn_task


_SETTINGS = {
    "turn_taking": "natural",
    "interrupt": True,
    "stt": {"base_url": "http://stt.lan/v1", "model": "whisper-1"},
    "tts": {"base_url": "http://tts.lan/v1", "model": "tts"},
    "tts_languages": ["en", "de"],
}


def test_the_worker_is_told_the_languages_the_call_listens_in():
    assert voice_calls.worker_config(_SETTINGS, "aria")["listen_languages"] == ["de", "en"]
    assert (
        voice_calls.worker_config({**_SETTINGS, "tts_languages": []}, "aria")["listen_languages"]
        == []
    )


def test_hqs_echo_misheard_in_another_listened_language_never_moves_the_call(monkeypatch):
    """Hermes 7438: the worker adopts a SUPPORTED language from an utterance the app then drops as
    HQ's own echo. "One moment, Commander." misheard as German moved the worker to `de`, the next
    short English "confirm" was tagged `de`, the app adopted it and refused the confirmation. The
    app now decides the call's language from the utterances it keeps, by their own heard
    language."""
    settings = {"tts_languages": ["en", "de"]}
    r = Rig(monkeypatch, NUDGE)
    r.call.detects_language = True
    r.call.language = "en"
    r.call.listen_languages = voice_calls.listen_languages(settings)
    worker_listen = frozenset(voice_calls.worker_config(_SETTINGS, "aria")["listen_languages"])
    worker_lang = ["en"]

    async def say(text, heard):
        worker_lang[0] = voice_text.call_language(worker_lang[0], heard, text, worker_listen)
        r.turn += 1
        await r.ev(t="user_started", turn=r.turn)
        await r.ev(t="utterance", text=text, turn=r.turn, heard_language=heard,
                   language=worker_lang[0])  # fmt: skip
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    async def go():
        await say("Rerun the tests on api-rebase", "en")
        await r.readback_played()
        await r.call.say("One moment, Commander.", kind="ack")  # still coming out of the speaker
        await say("One moment, Commander.", "de")  # its echo, misheard as German: dropped
        assert worker_lang[0] == "de"  # the worker's running guess WAS moved …
        await say("confirm", "en")

    run(go())
    assert r.call.language == "en"  # … but the call's language is the app's, and stayed English
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.asked == ["Rerun the tests on api-rebase"]


def test_the_call_language_follows_a_kept_utterances_own_heard_language(monkeypatch):
    r = Rig(monkeypatch)
    r.call.language = "en"
    r.call.listen_languages = voice_calls.listen_languages({"tts_languages": ["en", "de"]})
    # A real German request moves the call to German, whatever the worker's tag says.
    run(_tagged(r, "Wie viele Sitzungen laufen gerade?", heard="de", lang="en"))
    assert r.call.language == "de"
    # A short reply never moves it (the shared 3-word rule).
    run(_tagged(r, "Okay", heard="en", lang="en"))
    assert r.call.language == "de"


def test_a_dropped_echo_then_an_untagged_word_never_moves_the_call(monkeypatch):
    """Hermes 7443: the worker's tag is CUMULATIVE. After an echo misheard as German was dropped,
    an utterance with no heard language of its own still carried the worker's `de`; adopting it
    moved the call. The app never adopts the tag: only a kept utterance's own heard language
    counts, so the call stays English through an untagged word and a short English reply."""
    settings = {"tts_languages": ["en", "de"]}
    r = Rig(monkeypatch)
    r.call.detects_language = True
    r.call.language = "en"
    r.call.listen_languages = voice_calls.listen_languages(settings)
    worker_listen = frozenset(voice_calls.worker_config(_SETTINGS, "aria")["listen_languages"])
    worker_lang = ["en"]

    async def say(text, heard):
        worker_lang[0] = voice_text.call_language(worker_lang[0], heard, text, worker_listen)
        r.turn += 1
        await r.ev(t="user_started", turn=r.turn)
        await r.ev(t="utterance", text=text, turn=r.turn, heard_language=heard,
                   language=worker_lang[0])  # fmt: skip
        if r.call.turn_task is not None:
            await r.call.turn_task

    async def go():
        await r.call.say("One moment, Commander.", kind="ack")
        await say("One moment, Commander.", "de")  # its echo, misheard as German: dropped
        assert (worker_lang[0], r.call.language) == ("de", "en")
        await say("well", None)  # untagged: carries the worker's stale `de`
        assert r.call.language == "en"
        await say("status", "en")
        assert r.call.language == "en"

    run(go())


@pytest.mark.parametrize(
    "heard",
    [
        "Thank you One moment, Commander.",
        "One moment, Commander. Thank you.",
        "Danke. Stand by, Commander.",
    ],
)
def test_a_stock_phrase_left_after_the_echo_is_cut_makes_no_turn(monkeypatch, heard):
    """Hermes 7453: the stock-phrase check ran on the raw transcript, so room noise plus HQ's echo
    passed it whole, and cutting the echo handed the stock phrase alone to HQ."""
    r = Rig(monkeypatch)
    line = "Stand by, Commander." if "Stand by" in heard else "One moment, Commander."
    run(_hq_says_then_operator(r, line, heard))
    assert r.asked == []


def test_an_operators_words_left_after_the_echo_still_reach_hq(monkeypatch):
    r = Rig(monkeypatch)
    run(
        _hq_says_then_operator(
            r, "One moment, Commander.", "Thank you One moment, Commander. Status?"
        )
    )
    assert r.asked == ["Thank you Status?"]
