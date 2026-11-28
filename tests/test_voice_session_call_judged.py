"""#2341 O2 — a session's permission prompt on a session call is answered in natural words, in
any language, and judged by #1715 P2's judge (the operator's approval: comment 141855 on #1715).

Pinned here, each case in English and German:

* **the judge** — a reply the English grammar does not decide goes to ONE
  `voice_judge.judge_decision` (the guarded `voice_decision_judge` prompt), told the prompt as
  frozen when it was read back and the read-back itself, as data; ``allow_once`` is the request's
  single-use ``approve``, ``refuse`` its ``reject``;
* **asking back** — a hedge, a negated or conditional yes, a question (``other``) decides
  nothing, is not sent as a turn (the session's turn waits on this prompt), and asks again;
* **never by voice** — "don't allow it" never approves; a broader grant ("always", "immer") is
  refused BEFORE the grammar and the judge, and a judged ``broader`` is refused too;
* **the fence** — a verdict acts only while nothing was begun, typed or muted since the reply was
  heard; a prompt answered or changed on screen meanwhile is refused unsent by `decide`;
* **failing closed** — a judge past its time, a malformed or cut-off answer, a non-200 decides
  nothing and asks again; without HQ's endpoint there is no judge, and nothing is decided.
"""

# ruff: noqa: F811 — imported pytest fixtures

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_sessions import prompts, review, voice_judge, voice_session, voice_text
from test_voice_session_call import (  # noqa: F401 — the fixtures, used by name
    ASK,
    REQ,
    _asking,
    _call,
    _fast,
    _leg,
    _settle,
    _until,
    hq_configured,
    routes,
)

APPROVE, REJECT = "approve", "reject"


class _Resp:
    def __init__(self, content, status=200, finish="stop"):
        self.status_code = status
        self._body = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    def json(self):
        return self._body


@pytest.fixture
def judge(monkeypatch, hq_configured):
    """HQ's transport, answering each judgement with `judge.answer` (a `_Resp`, or a coroutine
    function returning one). `judge.sent` holds every request body."""

    class J:
        answer: object = _Resp("other")
        sent: list = []

    j = J()
    j.sent = []

    async def post(cfg, body):
        j.sent.append(body)
        a = j.answer
        return await a() if callable(a) else a

    monkeypatch.setattr(review, "_require_config", lambda: {"model": "m"})
    monkeypatch.setattr(review, "post_chat_response", post)
    return j


def _german(call) -> None:
    """A call whose STT tells languages apart, hearing German: the English grammar never decides."""
    call.detects_language = True
    call.language = "de"


def _said(call, text: str, turn: int, lang: str | None):
    async def go():
        await call.on_worker_event({"t": "user_started", "turn": turn})
        await call.on_worker_event({"t": "user_stopped", "turn": turn, "at": time.monotonic()})
        ev = {"t": "utterance", "text": text, "turn": turn}
        if lang:
            ev["heard_language"] = lang
        await call.on_worker_event(ev)
        await call.on_worker_event({"t": "turn_end", "turn": turn})

    return go()


def _setup(lang: str | None):
    call, wire = _call(_leg())
    if lang == "de":
        _german(call)
    elif lang == "en":
        call.detects_language = True
    return call, wire


def _ask_again(title: str = "docs-refresh") -> str:
    return f"{voice_session.waiting_line(title)} {voice_session.ALLOW_ASK}"


NOT_CAUGHT = f"{voice_session.NOT_CAUGHT_LINE} {voice_session.ALLOW_ASK}"


# --- natural words, judged ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang, said, verdict, decision",
    [
        ("en", "Yeah, go ahead.", "allow_once", APPROVE),
        ("de", "Ja, mach das.", "allow_once", APPROVE),
        ("en", "Turn that down, please.", "refuse", REJECT),
        ("de", "Nein, lehn das ab.", "refuse", REJECT),
    ],
)
def test_natural_words_allow_or_refuse_it_once_through_the_judge(
    routes, judge, lang, said, verdict, decision
):
    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp(verdict)
        op = await _asking(call, wire, routes)
        await _said(call, said, 2, lang)
        await _until(lambda: routes.decisions)
        d = routes.decisions[0]
        assert (d["turn_id"], d["request_id"], d["decision"], d["expected_digest"]) == (
            op,
            "7",
            decision,
            "3b0e9d2aa41c",
        )
        line = voice_session.ALLOWED_LINE if decision == APPROVE else voice_session.REFUSED_LINE
        await _until(lambda: line in wire.said())
        assert len(routes.posted) == 1  # the answer was never a turn

    asyncio.run(go())
    assert len(judge.sent) == 1
    system, user = judge.sent[0]["messages"]
    assert system["content"] == prompts.effective("voice_decision_judge")
    assert judge.sent[0]["temperature"] == 0
    # The prompt as frozen when it was read back, the read-back's FIXED words and the reply, as
    # quoted data only: never the request's command or folder, nor the session's title.
    item = {"kind": "permission", "can_allow_once": True}
    readback = "The session asks for permission. " + voice_session.ALLOW_ASK
    assert json.dumps(item) in user["content"]
    assert json.dumps(readback) in user["content"] and json.dumps(said) in user["content"]
    for words in ("npm test", "/home/u/proj", "docs-refresh"):
        assert words in ASK
        assert all(words not in m["content"] for m in judge.sent[0]["messages"])


def test_the_english_grammar_still_decides_at_once_without_the_judge(routes, judge):
    async def go():
        call, wire = _setup("en")
        judge.answer = _Resp("refuse")  # would be wrong: the grammar decides first
        await _asking(call, wire, routes)
        await _said(call, "Allow it.", 2, "en")
        await _until(lambda: routes.decisions)
        assert routes.decisions[0]["decision"] == APPROVE

    asyncio.run(go())
    assert judge.sent == []


def test_english_words_heard_as_german_never_take_the_grammar(routes, judge):
    """A turn not known to be English never takes the English fast path (#1715): it is judged."""

    async def go():
        call, wire = _setup("de")
        judge.answer = _Resp("cancel")
        await _asking(call, wire, routes)
        await _said(call, "Allow it.", 2, "de")
        await _until(lambda: voice_session.SKIPPED_LINE in wire.said())
        await _settle()
        assert routes.decisions == []

    asyncio.run(go())
    assert len(judge.sent) == 1


# --- ambiguity asks back -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang, hedge, then, verdict",
    [
        ("en", "Hmm, I'm not sure about that.", "Okay, yes, do it.", "allow_once"),
        ("de", "Hm, ich weiß nicht so recht.", "Okay, ja, mach.", "allow_once"),
        ("en", "What does that command do?", "Refuse it.", None),
        ("de", "Was macht das genau?", "Nein, lehn ab.", "refuse"),
    ],
)
def test_a_hedge_or_a_question_asks_back_and_is_never_a_turn(
    routes, judge, lang, hedge, then, verdict
):
    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp("other")
        await _asking(call, wire, routes)
        await _said(call, hedge, 2, lang)
        await _until(lambda: _ask_again() in wire.said())
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1  # nothing sent, no turn
        assert call.leg.decision is not None and call.leg.decision["armed"]
        # Still armed: the next answer decides it.
        if verdict is not None:
            judge.answer = _Resp(verdict)
        await _said(call, then, 3, lang)
        await _until(lambda: routes.decisions)
        expect = APPROVE if verdict == "allow_once" else REJECT
        assert routes.decisions[0]["decision"] == expect

    asyncio.run(go())


def test_stop_and_call_me_back_are_never_judged(routes, judge):
    async def go():
        call, wire = _setup("de")
        await _asking(call, wire, routes)
        await _said(call, "Stopp.", 2, "de")
        await _until(lambda: routes.interrupts)
        assert routes.decisions == []

    asyncio.run(go())
    assert judge.sent == []


# --- never by voice ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang, said, verdict",
    [
        ("en", "Don't allow it.", "refuse"),
        ("de", "Nein, erlaub das nicht.", "refuse"),
        ("en", "Don't allow it.", "other"),
        ("de", "Nein, erlaub das nicht.", "cancel"),
    ],
)
def test_dont_allow_it_never_approves(routes, judge, lang, said, verdict):
    assert voice_text.classify_decision(said, {"kind": "permission", "allow": {}, "deny": {}}) == (
        "other",
        None,
    )  # never the grammar's allow: it goes to the judge

    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp(verdict)
        await _asking(call, wire, routes)
        await _said(call, said, 2, lang)
        await _until(lambda: len(judge.sent) == 1)
        await _settle()
        assert all(d["decision"] != APPROVE for d in routes.decisions)
        if verdict == "refuse":
            await _until(lambda: routes.decisions)
            assert routes.decisions[0]["decision"] == REJECT
        else:
            assert routes.decisions == []

    asyncio.run(go())


@pytest.mark.parametrize(
    "lang, said",
    [("en", "Yes, and don't ask again."), ("de", "Ja, immer."), ("de", "für immer erlauben")],
)
def test_allow_always_is_refused_by_voice_before_the_judge(routes, judge, lang, said):
    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp("allow_once")  # would be wrong: the veto runs first
        await _asking(call, wire, routes)
        await _said(call, said, 2, lang)
        await _until(lambda: voice_session.BROADER_LINE in wire.said())
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1
        assert call.leg.decision is not None  # still armed for a once-only answer

    asyncio.run(go())
    assert judge.sent == []


@pytest.mark.parametrize("lang, said", [("en", "Yes, for good."), ("de", "Ja, auf Dauer.")])
def test_a_judged_broader_grant_is_refused_too(routes, judge, lang, said):
    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp("broader")
        await _asking(call, wire, routes)
        await _said(call, said, 2, lang)
        await _until(lambda: voice_session.BROADER_LINE in wire.said())
        await _settle()
        assert routes.decisions == []

    asyncio.run(go())


@pytest.mark.parametrize("lang", ["en", "de"])
def test_a_judged_yes_on_a_request_not_read_back_whole_allows_nothing(routes, judge, lang):
    async def go():
        call, wire = _setup(lang)
        judge.answer = _Resp("allow_once")
        req = {**REQ, "kind": "file_edit", "payload": {"path": "a.py", "patch": "+import os"}}
        await _asking(call, wire, routes, req)
        await _said(call, "Sure, go for it." if lang == "en" else "Klar, mach.", 2, lang)
        await _until(lambda: voice_session.NO_ALLOW_LINE in wire.said())
        await _settle()
        assert routes.decisions == []

    asyncio.run(go())
    _, user = judge.sent[0]["messages"]
    assert json.dumps({"kind": "permission", "can_allow_once": False}) in user["content"]
    on_screen = voice_session.ON_SCREEN_ALLOW + voice_session.REFUSE_ASK
    assert json.dumps(f"The session asks for permission. {on_screen}") in user["content"]


# --- the fence ---------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "de"])
def test_a_verdict_after_newer_words_began_decides_nothing(routes, judge, lang):
    """The snapshot is taken when the reply is heard: a verdict that lands after the Commander
    began speaking again is stale, and the newer words are handled after it, in order."""
    gate = asyncio.Event()

    async def go():
        call, wire = _setup(lang)

        async def slow():
            await gate.wait()
            return _Resp("allow_once")

        judge.answer = slow
        await _asking(call, wire, routes)
        await _said(call, "Yes, go on." if lang == "en" else "Ja, mach weiter.", 2, lang)
        await _until(lambda: len(judge.sent) == 1)
        await call.on_worker_event({"t": "user_started", "turn": 3})  # newer words begin
        judge.answer = _Resp("cancel")
        gate.set()
        await _said(call, "Wait." if lang == "en" else "Moment.", 3, lang)
        await _until(lambda: voice_session.SKIPPED_LINE in wire.said())
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1

    asyncio.run(go())
    assert len(judge.sent) == 2  # the newer words were judged after the older, never before


@pytest.mark.parametrize("lang", ["en", "de"])
def test_a_prompt_answered_on_screen_while_judged_is_refused_unsent(routes, judge, lang):
    gate = asyncio.Event()

    async def go():
        call, wire = _setup(lang)

        async def slow():
            await gate.wait()
            return _Resp("allow_once")

        judge.answer = slow
        await _asking(call, wire, routes)
        await _said(call, "Yes please." if lang == "en" else "Ja, bitte.", 2, lang)
        await _until(lambda: len(judge.sent) == 1)
        routes.pending = []  # answered on the pane meanwhile
        gate.set()
        await _until(lambda: voice_session.ANSWERED_LINE in wire.said())
        assert routes.decisions == []

    asyncio.run(go())


def test_a_verdict_after_the_call_moved_to_hq_decides_nothing(routes, judge):
    gate = asyncio.Event()

    async def go():
        call, wire = _setup("de")

        async def slow():
            await gate.wait()
            return _Resp("allow_once")

        judge.answer = slow
        await _asking(call, wire, routes)
        await _said(call, "Ja, mach.", 2, "de")
        await _until(lambda: len(judge.sent) == 1)
        call.leg = None  # the party changed while the reply was judged
        gate.set()
        await _settle()
        assert routes.decisions == []

    asyncio.run(go())


def test_stop_acts_at_once_and_stale_replies_queued_behind_a_judgement_cost_no_model_call(
    routes, judge
):
    """Review 9130: "stop" while a reply is judged is never queued behind it, and replies that
    waited behind it while newer words began are stale, so they never reach the model."""
    gate = asyncio.Event()

    async def go():
        call, wire = _setup("de")

        async def parked():
            await gate.wait()
            return _Resp("allow_once")

        judge.answer = parked
        await _asking(call, wire, routes)
        await _said(call, "Ja, mach.", 2, "de")
        await _until(lambda: len(judge.sent) == 1)
        await _said(call, "Hm, warte.", 3, "de")
        await _said(call, "Doch nicht.", 4, "de")
        await _said(call, "Stopp.", 5, "de")
        await _until(lambda: routes.interrupts)  # while the first judgement is still parked
        assert not gate.is_set() and len(judge.sent) == 1
        # New words after the stop, while the judgement is still parked: they answer no prompt.
        await _said(call, "Lies stattdessen die README.", 6, "de")
        gate.set()
        await _until(lambda: call.leg.held == 0)
        await _settle()
        assert len(judge.sent) == 1 and routes.decisions == []
        # Review 9133: the replies said to the stopped prompt never become turns; the words said
        # after the stop do, once.
        after = [b["text"] for b in routes.posted[1:]]
        assert after == ["[on a call] Lies stattdessen die README."]

    asyncio.run(go())


# --- failing closed ----------------------------------------------------------------------------


async def _past_time():
    await asyncio.sleep(1)
    return _Resp("allow_once")


@pytest.mark.parametrize(
    "answer",
    [
        _past_time,
        _Resp("allow once"),
        _Resp("allow_once."),
        _Resp("pick 1"),
        _Resp("send"),
        _Resp("allow_once", status=500),
        _Resp("allow_once", finish="length"),
        _Resp(None),
    ],
    ids=[
        "timeout",
        "malformed",
        "punctuated",
        "another kind's verdict",
        "a question's verdict",
        "a non-200",
        "cut off",
        "no content",
    ],
)
@pytest.mark.parametrize("lang", ["en", "de"])
def test_no_verdict_decides_nothing_and_asks_again(routes, judge, monkeypatch, answer, lang):
    monkeypatch.setattr(voice_judge, "JUDGE_TIMEOUT_S", 0.05)

    async def go():
        call, wire = _setup(lang)
        judge.answer = answer
        await _asking(call, wire, routes)
        await _said(call, "Yeah, sure." if lang == "en" else "Ja, sicher.", 2, lang)
        await _until(lambda: NOT_CAUGHT in wire.said())
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1
        assert call.leg.decision is not None and call.leg.decision["armed"]

    asyncio.run(go())


def test_without_hqs_endpoint_nothing_but_the_english_grammar_decides(routes):
    """A direct session call needs no HQ endpoint (#2341 §6b), and without one there is no judge:
    words the English grammar does not decide are the session's next turn, as before, and a
    German reply decides nothing."""

    async def go():
        call, wire = _setup("de")
        await _asking(call, wire, routes)
        await _said(call, "Ja, mach das.", 2, "de")
        await _until(lambda: len(routes.posted) == 2)
        await _settle()
        assert routes.decisions == [] and call.leg.decision is None

    asyncio.run(go())
