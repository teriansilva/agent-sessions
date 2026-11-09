"""One announcement per card and per question on a call (#1911).

HQ's own answer is spoken first, then the fixed card sentences. A question HQ asks with
`ask_operator` is stored as the turn's answer text too (`ask_orchestrator._asked_records`), so the
question used to be said twice on every such turn."""

import asyncio
import dataclasses
import json

import pytest

from agent_sessions import ask_orchestrator, voice_text

ASK = {
    "question_id": "q1",
    "question": "Which project should the deep research session go into?",
    "options": ["Home Lab", "Web App"],
    "allow_text": False,
}


def test_a_question_the_answer_already_spoke_is_not_asked_again():
    out = voice_text.card_sentences({"ask": ASK}, said=ASK["question"])
    assert out == ["One: Home Lab. Two: Web App."]


def test_containment_ignores_case_spacing_and_markdown_but_nothing_else():
    said = "Got it.  **which project should the deep  research session go into?**"
    assert voice_text.card_sentences({"ask": ASK}, said=said) == ["One: Home Lab. Two: Web App."]


def test_a_paraphrase_is_asked_again_so_nothing_goes_unsaid():
    out = voice_text.card_sentences({"ask": ASK}, said="Where should the research go?")
    assert out == [
        "Which project should the deep research session go into? One: Home Lab. " "Two: Web App."
    ]


def test_a_free_text_question_already_spoken_adds_no_line():
    ask = {**ASK, "options": [], "allow_text": True}
    assert voice_text.card_sentences({"ask": ask}, said=ASK["question"]) == []


def test_without_the_spoken_answer_the_question_is_read_as_before():
    out = voice_text.card_sentences({"ask": ASK})
    assert out[0].startswith("Which project should the deep research session go into? One:")


# -- HQ is told the call announces its card (#1911) ---------------------------------------------

STAGED = {"proposed": True, "note": "The operator now sees a card."}


def _run(monkeypatch, via, result):
    async def fake(turn, args):
        return dict(result), {"count": 1}

    tool = dataclasses.replace(ask_orchestrator.TOOLS["propose_session"], run=fake)
    monkeypatch.setitem(ask_orchestrator.TOOLS, "propose_session", tool)
    turn = ask_orchestrator._Turn(cid="x", turn_id="t", question="q", working_keys=set(),
                                  via=via, progress=lambda ev: None)  # fmt: skip
    content, _args, _meta = asyncio.run(ask_orchestrator._call(turn, "propose_session", "{}"))
    return json.loads(content)


def test_a_card_staged_on_a_call_tells_hq_the_call_reads_it(monkeypatch):
    out = _run(monkeypatch, "call", STAGED)
    assert out == {**STAGED, "on_call": ask_orchestrator.CALL_CARD_NOTE}


@pytest.mark.parametrize(
    ("via", "result"),
    [(None, STAGED), ("call", {"proposed": False, "note": "nothing staged"})],
    ids=["typed turn", "nothing staged"],
)
def test_no_note_off_a_call_or_without_a_card(monkeypatch, via, result):
    assert "on_call" not in _run(monkeypatch, via, result)
