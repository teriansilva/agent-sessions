"""On a call HQ asks the way a person asks (#2342): no "Say confirm…", no "say the number", no
"The options are: one, …", and "on your screen" only where the Commander has to look.

Until the operator approves #1715 P2 (H2 on #2342), a Needs-you decision keeps its English
grammar, so every question it asks names words that grammar accepts: each rendered prompt is run
through the classifier with the reply it invites. Short questions heard back from the speaker are
echo (#2342 §2c), without lowering the three-word rule for anything else."""

from __future__ import annotations

import re

import pytest

from agent_sessions import voice_calls, voice_text
from agent_sessions.voice_text import (
    classify_confirmation,
    classify_decision,
    is_skip,
    or_list,
    ordinal_list,
    readback,
    strip_echo,
    which_of,
)
from test_voice_needs_you import MENU
from test_voice_needs_you_decide import _hand_row

#: What a person would never say on the phone (#2342).
BANNED = re.compile(
    r"\bsay (confirm|allow|refuse|dismiss|the)\b|the number|on your screen to|the options are"
    r"|\bone, [^.?]*; two, ",  # the old "one, X; two, Y" list
    re.IGNORECASE,
)


def _menu_row() -> dict:
    return {"title": "docs", "project": {"id": "", "name": ""}, "kind": "choice",
            "action": {"id": "esc-m", "verb": "escalate", "menu": MENU}}  # fmt: skip


def _question_row() -> dict:
    return {"title": "api-rebase", "project": {"id": "", "name": ""}, "kind": "question",
            "action": {"id": "ans-q", "verb": "answer", "answer": "Use main.",
                       "can_approve": True}}  # fmt: skip


def _hq_cards() -> list[dict]:
    return [
        {"id": "a", "kind": "nudge", "verb": "send it", "line": "A message to docs: Rerun it."},
        {"id": "b", "kind": "session", "verb": "start it", "line": "A new session in docs: Go."},
        {"id": "c", "kind": "operation", "verb": "save it", "line": "Save a new skill."},
        {"id": "d", "kind": "operation", "verb": "do it", "line": "Archive, for docs: Stops it."},
        {"id": "e", "kind": "suggest", "verb": "use it", "line": "I suggest /srv/web."},
    ]


@pytest.mark.parametrize(
    "card, question",
    [
        (0, "Want me to send it?"),
        (1, "Shall I start it?"),
        (2, "Shall I save it?"),
        (3, "Shall I go ahead?"),
        (4, "Shall I use it?"),
    ],
)
def test_an_hq_card_ends_on_one_plain_question(card, question):
    c = _hq_cards()[card]
    assert readback([c]) == f"{c['line']} {question}"


def test_several_cards_are_asked_which_one_not_told_to_say_a_number():
    said = readback(_hq_cards()[:3])
    assert said.endswith("Which one should I do, the first, the second or the third?")
    # the existing grammar and the judge still take "confirm the second"
    assert classify_confirmation("confirm the second", 3) == ("confirm", 1)


def test_no_rendered_call_line_tells_the_commander_which_words_to_say():
    menu = voice_calls._menu_card(_menu_row())
    perm = voice_calls._permission_card(_hand_row())
    question = voice_calls._question_card(_question_row())
    assert menu and perm and question
    lines = [readback([c]) for c in _hq_cards()]
    lines += [readback(_hq_cards()[:4]), readback([menu]), readback([perm]), readback([question])]
    lines += [voice_text.readback_screen_only([menu]), voice_calls.needs_you_sentence(_menu_row())]
    lines += voice_text.card_sentences(
        {"proposals": [{"kind": "nudge", "action_id": "x", "title": "docs"},
                       {"kind": "session", "action_id": "y", "title": "docs"}],
         "ask": {"question": "Which project?", "options": ["Home Lab", "Web App"]}}
    )  # fmt: skip
    for line in lines:
        assert not BANNED.search(line), line


# --- pre-H2: every Needs-you question names words its grammar accepts -------------------------


def test_a_permission_prompt_is_answered_by_the_words_it_names():
    card = voice_calls._permission_card(_hand_row())
    said = readback([card])
    assert said.endswith("Allow it once, refuse it, or skip it?")
    assert classify_decision("allow it once", card) == ("choose", card["allow"])
    assert classify_decision("refuse it", card) == ("choose", card["deny"])
    assert is_skip("skip it")
    # a plain yes decides nothing before H2: it is not a word the question offered
    assert classify_decision("yes", card)[0] == "other"


def test_a_menu_is_read_by_place_and_answered_by_place_until_h2():
    card = voice_calls._menu_card(_menu_row())
    said = readback([card])
    assert "The first is main and the second is release 2.0." in said
    assert said.endswith("Which one, or skip it?")
    assert classify_decision("the second one", card) == ("choose", card["options"][1])
    assert classify_decision("the first", card) == ("choose", card["options"][0])
    assert is_skip("skip it")
    # name-only answers wait for H2 (#1715 P2): the gate is pinned here
    assert classify_decision("release 2.0", card)[0] == "other"


def test_a_suggested_answer_is_sent_skipped_or_cancelled_by_the_words_it_names():
    card = voice_calls._question_card(_question_row())
    assert readback([card]).endswith("Send it, skip it, or cancel?")
    assert classify_confirmation("send it", 1) == ("confirm", 0)
    assert is_skip("skip it")
    assert classify_confirmation("cancel", 1) == ("cancel", None)


# --- the small helpers -------------------------------------------------------------------------


def test_choices_read_like_a_person_reads_them():
    assert or_list(["Claude", "Codex"]) == "Claude or Codex?"
    assert or_list(["docs", "web", "api"]) == "docs, web or api?"
    assert ordinal_list(["main"]) == "The first is main."
    assert ordinal_list(["a", "b", "c"]) == "The first is a, the second is b and the third is c."
    assert which_of(2) == "the first or the second"


# --- short questions heard back are echo (#2342 §2c) -------------------------------------------

LINE = "Default folder, for web: it changes. The first is /srv/web. Which folder?"


@pytest.mark.parametrize("heard", ["Which folder?", "which folder", "Which  folder ?"])
def test_a_short_question_heard_back_whole_is_echo(heard):
    assert strip_echo(heard, [LINE]) == ""


@pytest.mark.parametrize(
    "heard", ["which, the first one", "the first", "which folder is that", "Yes."]
)
def test_a_reply_that_is_not_the_whole_question_is_kept(heard):
    assert strip_echo(heard, [LINE]) == heard


def test_a_short_statement_is_never_dropped_as_echo():
    # "Yes." read back word for word inside a message is exactly what the Commander may answer
    assert strip_echo("Yes.", ["A message to docs: Yes. Want me to send it?"]) == "Yes."


# --- a card the judge does not hear asks for a phrase the grammar takes (Hermes 8839) ---------

MESSAGE = {"kind": "message", "action_id": "m1", "recipient": "ops", "connector": "matrix",
           "text": "Deploy is done."}  # fmt: skip
NUDGE_CARD = {"kind": "nudge", "action_id": "act-1", "title": "api-rebase", "text": "Rerun tests."}


def _call(monkeypatch, view, judge=("confirm", 0)):
    from test_voice_confirm import Rig

    r = Rig(monkeypatch, view)
    r.verdict = judge  # were the judge asked, it would confirm: it must NOT be asked here
    return r


@pytest.mark.parametrize(
    "said, posts",
    [("Send it.", 1), ("yes", 0)],
)
def test_a_connector_message_names_the_phrase_its_grammar_takes(monkeypatch, said, posts):
    from test_voice_confirm import run

    r = _call(monkeypatch, {"proposals": [MESSAGE]})

    async def go():
        await r.ask("Tell ops the deploy is done")
        assert r.said_text()[-1] == (
            "A message to ops on matrix: Deploy is done. Send it, or cancel?"
        )
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert r.judged == []  # a connector message is never judged
    assert len([p for m, p, _ in r.routes if m == "POST"]) == posts


def test_a_batch_with_a_connector_message_asks_by_the_grammar_too(monkeypatch):
    from test_voice_confirm import run

    r = _call(monkeypatch, {"proposals": [NUDGE_CARD, MESSAGE]})

    async def go():
        await r.ask("nudge api-rebase and tell ops")
        assert r.said_text()[-1].endswith("Confirm the first or the second, or cancel?")
        await r.readback_played()
        await r.ask("confirm")  # several cards: it asks which, naming the phrase again
        assert r.said_text()[-1] == "Confirm the first or the second, or cancel?"
        await r.readback_played()
        await r.ask("confirm the second")

    run(go())
    assert r.judged == []
    assert [p for m, p, _ in r.routes if m == "POST"] == ["/api/ask/proposals/m1/decide"]


def test_judged_cards_alone_keep_the_plain_question(monkeypatch):
    from test_voice_confirm import TWO, run

    r = _call(monkeypatch, TWO)

    async def go():
        await r.ask("do both")
        assert r.said_text()[-1].endswith("Which one should I do, the first or the second?")

    run(go())
