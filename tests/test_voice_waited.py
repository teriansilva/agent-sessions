"""#2354 — words said on a call while HQ works are answered at once, later with a reference back,
or not at all.

* The interjection judgement has its own budget (`voice_judge.INTERJECT_TIMEOUT_S`): the live model
  took 4.4-5.2 s for "No hurry", past the confirm judge's 4 s, so the words fell back to the queue.
  Each judgement logs its verdict class or why there was none, never the words.
* Words a CALL heard while HQ worked are stamped by the call (`heard["busy_since"]`) and run with
  `WAITED_NOTE` in front of them, however they reach a slot: the queue, a free slot after their
  judged turn settled, or handed back unconsumed after a steer. Typed questions, a switch's words
  and call questions said while nothing ran never get it.
* HQ may answer such a question with exactly `NO_REPLY`: the thread stores and shows
  `QUIET_ANSWER` (`quiet: true`), and the call says nothing for it — not even "I have that answer
  now." Anywhere else `NO_REPLY` is ordinary answer text."""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx
import pytest

from agent_sessions import ask_orchestrator, ask_store, review, voice_calls, voice_judge
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures
from test_ask_orchestrator import ai, answer, app, call, no_tools, route  # noqa: F401
from test_voice_confirm import Rig
from test_voice_draft_turns import _speak, _until
from test_voice_interject import (  # noqa: F401 — fixtures
    REAL_JUDGE,
    _questions,
    _statuses,
    _working,
    rig,
)

NOTE_HEAD = "[Said about "
#: More than `CALL_END_JUDGE_MAX_WORDS`: no call-end judgement asks the endpoint once more.
LONG = "please tell me where the federation epic stands and what is still open on it today"


@pytest.fixture(autouse=True)
def _clean():
    ask_orchestrator._RUNNING.clear()
    yield
    ask_orchestrator._RUNNING.clear()
    ask_orchestrator._QUEUE.clear()
    ask_orchestrator._NO_SUCCESSOR.clear()
    ask_orchestrator._STARTS.clear()


def _user_messages(body: dict) -> list[str]:
    return [str(m["content"]) for m in body["messages"] if m["role"] == "user"]


def _asked_with(script, words: str) -> str:
    """The newest user message of the request that asked ``words`` (other requests, such as the
    memory capture after a turn, ride on the same endpoint)."""
    asked = [m for b in script.bodies for m in _user_messages(b) if m.endswith(words)]
    assert asked, f"no request asked {words!r}"
    return asked[-1]


def _assistant_records(cid: str) -> list[dict]:
    return [r for r in ask_store.read(cid) if r["type"] == "assistant"]


# ---- the note ------------------------------------------------------------------------------------


def test_the_note_counts_seconds_then_minutes_and_names_the_quiet_reply():
    assert ask_orchestrator._waited_note(0.2).startswith("[Said about 1 seconds ago")
    assert "about 45 seconds ago" in ask_orchestrator._waited_note(45.4)
    assert "about 3 minutes ago" in ask_orchestrator._waited_note(190)
    note = ask_orchestrator._waited_note(10)
    assert note.endswith(f"reply with exactly {ask_orchestrator.NO_REPLY}.]")
    assert "answer was" not in note and "spoken" not in note  # never claims it was heard
    assert "{" not in note and "}" not in note


async def _send(cid: str, text: str, **kw):
    tid = str(uuid.uuid4())
    task, view = await ask_orchestrator.begin(cid, tid, text, working_keys=set(), **kw)
    return tid, task, view


@pytest.mark.parametrize("via", ["call", None])
def test_only_a_call_question_that_waited_gets_the_note(held, via):
    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, first, _ = await _send(cid, LONG, via=via)
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, task2, view2 = await _send(
            cid, "and what else is " + LONG, via=via, waited_since=time.monotonic() - 30
        )
        assert task2 is None and view2["status"] == "queued"
        started = ask_orchestrator.when_started(cid, t2)
        script.go.set()
        await first
        await (await asyncio.wait_for(started, 15))
        return script

    script = run(scenario())
    first_asked = _user_messages(script.bodies[0])[-1]
    second_asked = _asked_with(script, "and what else is " + LONG)
    assert not first_asked.startswith(NOTE_HEAD)  # asked into a free slot
    if via == "call":
        assert second_asked.startswith("[Said about 30 seconds ago")
        assert second_asked.endswith("and what else is " + LONG)
    else:
        assert not second_asked.startswith(NOTE_HEAD)  # typed: the thread shows it in place


def test_no_reply_on_a_waited_call_question_is_stored_quiet(held):
    async def scenario():
        script = held(answer("one"), answer("  NO_REPLY \n"))
        cid = ask_store.create()
        _, first, _ = await _send(cid, LONG, via="call")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await _send(cid, "No hurry", via="call", waited_since=time.monotonic())
        started = ask_orchestrator.when_started(cid, t2)
        script.go.set()
        await first
        view = await (await asyncio.wait_for(started, 15))
        return cid, script, view

    cid, script, view = run(scenario())
    assert view["status"] == "done"
    assert view["answer"] == ask_orchestrator.QUIET_ANSWER and view["quiet"] is True
    quiet = _assistant_records(cid)[-1]
    assert quiet["text"] == ask_orchestrator.QUIET_ANSWER and quiet["quiet"] is True
    # A short call question would be judged for a call end; a quiet answer never is.
    assert len(script.bodies) == 2
    # The next turn replays the quiet line, so HQ sees it said nothing on purpose.
    views = ask_orchestrator.turn_views(ask_store.read(cid))
    assert [v.get("quiet") for v in views] == [None, True]


@pytest.mark.parametrize("quiet", [True, False])
def test_no_call_end_judgement_on_a_quiet_answer_on_the_no_tools_path(no_tools, quiet):
    """The live adapter refuses tools: the router answers directly (`_no_tools_turn`). A short
    call question is judged for a call end there too — never when the answer is quiet."""

    async def scenario():
        script = no_tools(route({"search": False, "answer": "NO_REPLY" if quiet else "Fine."}))
        cid = ask_store.create()
        _, task, _ = await _send(cid, "No hurry", via="call", waited_since=time.monotonic())
        return script, await task

    script, view = run(scenario())
    assert view.get("quiet") is (True if quiet else None)
    assert script.router_calls == (1 if quiet else 2)  # the router, then (only) the judgement


def test_no_reply_from_a_question_that_did_not_wait_is_plain_text(held):
    async def scenario():
        held(answer("NO_REPLY"), free=1)
        cid = ask_store.create()
        _, task, _ = await _send(cid, LONG, via="call")
        return cid, await task

    cid, view = run(scenario())
    assert view["answer"] == "NO_REPLY" and "quiet" not in view
    assert "quiet" not in _assistant_records(cid)[-1]


def test_the_store_keeps_quiet_only_as_true():
    cid = ask_store.create()
    tid = str(uuid.uuid4())
    ask_store.append(
        cid,
        {"type": "user", "turn_id": tid, "text": "x", "qd": "0" * 64, "contact": "off"},
        {"type": "assistant", "turn_id": tid, "text": "a", "quiet": "yes"},
        {"type": "assistant", "turn_id": tid, "text": "b", "quiet": True},
    )
    recs = _assistant_records(cid)
    assert "quiet" not in recs[0] and recs[1]["quiet"] is True


# ---- the call ------------------------------------------------------------------------------------


def test_a_later_verdict_whose_answer_is_no_reply_says_nothing_more(rig, held):
    rig.interject_verdict = ("later", "Sure, finishing this first.")

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("NO_REPLY"))
        await _working(rig, script)
        await _speak(rig, "No hurry")
        await _until(lambda: "Sure, finishing this first." in rig.said_text())
        script.go.set()
        await _until(lambda: len(_assistant_records(rig.call.cid)) == 2)
        await _until(lambda: rig.call.call_turn is None and rig.call.queued is None)
        await asyncio.sleep(0.2)  # nothing else is on its way to the worker
        return script

    script = run(scenario())
    assert rig.said_text() == ["Sure, finishing this first.", "The epic is in phase three."]
    assert _questions(rig.call.cid) == ["Where is the federation epic?", "No hurry"]
    assert _asked_with(script, "No hurry").startswith(NOTE_HEAD)
    turns = [m["turn"] for m in rig.to_client if isinstance(m, dict) and m.get("t") == "turn"]
    assert turns[-1]["quiet"] is True  # the screen still gets the quiet line


def test_a_waited_question_that_is_answered_is_still_spoken(rig, held):
    rig.interject_verdict = ("later", "Sure, finishing this first.")

    async def scenario():
        script = held(
            answer("The epic is in phase three."),
            answer("About what needs you: two sessions do."),
        )
        await _working(rig, script)
        await _speak(rig, "Also, what needs me?")
        await _until(lambda: "Sure, finishing this first." in rig.said_text())
        script.go.set()
        await _until(lambda: "About what needs you: two sessions do." in rig.said_text())
        return script

    script = run(scenario())
    assert _asked_with(script, "Also, what needs me?").startswith(NOTE_HEAD)


def test_steer_words_the_turn_never_used_keep_their_provenance(rig, held):
    """Handed back by `take_unconsumed`, they may run in a free slot, not through the queue."""
    rig.interject_verdict = ("steer", "Ah, got it.")

    async def scenario():
        script = held(answer("Here is the epic."), answer("Issue 2059 is in phase three."))
        await _working(rig, script)
        await _speak(rig, "It's issue 2059.")
        await _until(lambda: "Ah, got it." in rig.said_text())
        script.go.set()
        await _until(lambda: "Issue 2059 is in phase three." in rig.said_text())
        return script

    script = run(scenario())
    assert _asked_with(script, "It's issue 2059.").startswith(NOTE_HEAD)
    assert rig.call.steer_since == {}  # taken with the words, never left behind


def test_words_whose_judged_turn_settled_meanwhile_keep_their_provenance(rig, held):
    gate = asyncio.Event()

    async def judged():
        await gate.wait()
        return ("later", "Sure, after this.")

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("Staging is green."))
        await _working(rig, script)
        await _speak(rig, "And how is staging doing these days, all green?")
        await _until(lambda: len(rig.interjected) == 1)
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid) == ["pending", "done"])
        await _until(lambda: "The epic is in phase three." in rig.said_text())
        await _until(
            lambda: not ask_orchestrator.running(rig.call.cid) and not rig.call._turn_running()
        )
        gate.set()  # its turn is gone: the words are a new question, in a FREE slot
        await _until(lambda: "Staging is green." in rig.said_text())
        return script

    script = run(scenario())
    assert _asked_with(script, "all green?").startswith(NOTE_HEAD)


def test_a_switch_asks_its_words_fresh(rig, held):
    rig.interject_verdict = ("switch", "Okay, dropping that.")

    async def scenario():
        script = held(answer("Staging is green."))
        await _working(rig, script)
        await _speak(rig, "Forget that, how is staging doing these days, all green?")
        await _until(lambda: _statuses(rig.call.cid)[:2] == ["pending", "stopped"])
        script.go.set()  # the replacement's request
        await _until(lambda: "Staging is green." in rig.said_text())
        return script

    script = run(scenario())
    assert not _asked_with(script, "all green?").startswith(NOTE_HEAD)


def test_a_question_said_while_nothing_ran_gets_no_note(rig, held):
    async def scenario():
        script = held(answer("The epic is in phase three."), free=5)
        await _speak(rig, "Where is the federation epic?")
        await _until(lambda: "The epic is in phase three." in rig.said_text())
        return script

    script = run(scenario())
    assert not _asked_with(script, "Where is the federation epic?").startswith(NOTE_HEAD)


def test_a_quiet_answer_says_nothing_but_its_cards_are_still_offered(monkeypatch):
    nudge = {"kind": "nudge", "action_id": "act-1", "title": "api-rebase", "text": "Rerun tests."}
    r = Rig(
        monkeypatch,
        {"answer": ask_orchestrator.QUIET_ANSWER, "quiet": True, "proposals": [nudge]},
    )
    asyncio.run(r.ask("No hurry"))
    assert ask_orchestrator.QUIET_ANSWER not in " ".join(r.said_text())
    assert r.said_text(), "the card is still read back"
    assert r.call.challenge is not None


def test_a_quiet_answer_overtaken_by_a_newer_question_says_nothing(monkeypatch):
    r = Rig(monkeypatch)

    async def scenario():
        heard = r.call._heard_now()
        view = {"turn_id": "t", "status": "done", "answer": ask_orchestrator.QUIET_ANSWER,
                "quiet": True}  # fmt: skip
        await r.call._present(view, r.call.generation - 1, heard, 0.0)
        assert r.said_text() == []
        await r.call._present({**view, "quiet": None}, r.call.generation - 1, heard, 0.0)
        assert r.said_text() == [voice_calls.ANSWER_READY_LINE]  # unchanged for any other

    asyncio.run(scenario())


# ---- the judgement's budget and its log ----------------------------------------------------------


class _Resp:
    status_code = 200

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]}


def _judge_with(monkeypatch, reply):
    monkeypatch.setattr(review, "_require_config", lambda: {"model": "m"})

    async def post(cfg, body):
        return await reply()

    monkeypatch.setattr(review, "post_chat_response", post)
    return asyncio.run(REAL_JUDGE("Where is the epic?", "thinking", "No hurry, take your time"))


def test_the_interjection_has_its_own_longer_budget():
    assert voice_judge.INTERJECT_TIMEOUT_S >= 2 * voice_judge.JUDGE_TIMEOUT_S


def test_a_verdict_slower_than_the_confirm_budget_still_counts_and_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(voice_judge, "JUDGE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(voice_judge, "INTERJECT_TIMEOUT_S", 2.0)

    async def slow():
        await asyncio.sleep(0.2)
        return _Resp('{"do": "status", "say": "Still on it, no rush."}')

    with caplog.at_level(logging.INFO, logger="agent_sessions.voice_judge"):
        verdict = _judge_with(monkeypatch, slow)
    assert verdict == ("status", "Still on it, no rush.")
    lines = [r.getMessage() for r in caplog.records if "interjection verdict" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("interjection verdict=status reason=ok ms=")
    assert "hurry" not in lines[0] and "rush" not in lines[0]  # never the words, nor the line


@pytest.mark.parametrize(
    ("reply", "reason"),
    [("timeout", "timeout"), ("http", "http_503"), ("parse", "parse")],
)
def test_no_verdict_logs_why(monkeypatch, caplog, reply, reason):
    monkeypatch.setattr(voice_judge, "INTERJECT_TIMEOUT_S", 0.05)

    async def answer_():
        if reply == "timeout":
            await asyncio.sleep(5)
        if reply == "http":
            return httpx.Response(503)
        return _Resp("not json")

    with caplog.at_level(logging.INFO, logger="agent_sessions.voice_judge"):
        assert _judge_with(monkeypatch, answer_) is None
    lines = [r.getMessage() for r in caplog.records if "interjection verdict" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith(f"interjection verdict=none reason={reason} ")
