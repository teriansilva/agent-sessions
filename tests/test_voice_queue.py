"""#2103 on a call: a question spoken while HQ works waits in the conversation's queue.

The call keeps no question of its own any more. A spoken question behind a running task is
queued in order like a typed one; "stop" stops the running task and leaves the queue; "stop
everything" is Stop all; a finished task the Commander is no longer waiting on is announced
with one fixed line; and a call that ended leaves its queued questions to run as typed ones
and is never spoken to. Driven through the call with the REAL orchestrator."""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import re

import pytest

from agent_sessions import ask_orchestrator, ask_store, voice_calls, voice_text
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures
from test_ask_orchestrator import ai, answer, app  # noqa: F401 — fixtures
from test_voice_confirm import Rig
from test_voice_draft_turns import _speak, _until

READY = voice_calls.ANSWER_READY_LINE
#: What HQ says to words heard while its own turn works when the judgement has no verdict (#2113).
FALLBACK = voice_calls.INTERJECT_FALLBACK


@pytest.fixture
def rig(monkeypatch, app):  # noqa: F811
    real_begin = ask_orchestrator.begin
    ask_orchestrator._RUNNING.clear()
    r = Rig(monkeypatch)
    monkeypatch.setattr(ask_orchestrator, "begin", real_begin)  # the real orchestrator
    r.call.cid = app.new()
    yield r
    ask_orchestrator._RUNNING.clear()
    ask_orchestrator._QUEUE.clear()
    ask_orchestrator._NO_SUCCESSOR.clear()


def _asked(script) -> list[str]:
    """What HQ's turns were asked: the last message of each turn request (the call-end
    judgement's own requests, which quote the exchange, are not turns)."""
    last = [str(b["messages"][-1]["content"]) for b in script.bodies]
    # A question that waited behind running work carries the waited note first (#2354).
    last = [m.split("\n\n", 1)[-1] if m.startswith("[Said about ") else m for m in last]
    return [m.split("\n")[0] for m in last if not m.startswith("The Commander said:")]


def _statuses(cid: str) -> list[str]:
    return [r["status"] for r in ask_store.read(cid) if r.get("type") == "status"]


def _questions(cid: str) -> list[tuple[str, str | None]]:
    return [(r["text"], r.get("via")) for r in ask_store.read(cid) if r.get("type") == "user"]


async def _queued(rig, n: int) -> None:
    await _until(lambda: len(ask_orchestrator.queued(rig.call.cid)) == n)


def test_two_spoken_questions_queue_in_order_and_both_answers_are_spoken(rig, held):
    """Words spoken while THIS call's own turn works are answered beside it (#2113 P3: here no
    verdict, so the fixed fallback) and wait in the conversation's queue, in order, with no new
    generation: every answer is the one the Commander is waiting on, and each is spoken."""

    async def scenario():
        script = held(answer("One."), answer("Two."), answer("Three."))
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)
        await _speak(rig, "Draft a status note.")
        await _queued(rig, 2)
        assert script.bodies == [] and rig.said_text() == [FALLBACK, FALLBACK]
        script.go.set()
        await _until(lambda: "Three." in rig.said_text())
        await _until(lambda: rig.call.state == "listening")
        return script

    script = run(scenario())
    cid = rig.call.cid
    assert _questions(cid) == [
        ("How far is the epic?", "call"),
        ("And who is working on it?", "call"),
        ("Draft a status note.", "call"),
    ]
    assert _statuses(cid) == [
        "pending", "queued", "queued", "done", "pending", "done", "pending", "done",
    ]  # fmt: skip
    assert _asked(script) == [q for q, _ in _questions(cid)]
    # (Which scripted reply answers which turn depends on the call-end judgement's own requests.)
    said = rig.said_text()
    assert said[:2] == [FALLBACK, FALLBACK] and len(said) == 5 and READY not in said
    queued = [m for m in rig.to_client if isinstance(m, dict) and m.get("t") == "queued"]
    assert [m["turn"]["status"] for m in queued] == ["queued", "queued"]
    turns = [m["turn"] for m in rig.to_client if isinstance(m, dict) and m.get("t") == "turn"]
    assert [t["status"] for t in turns] == ["done"] * 3  # all three answers are on screen


async def _typed(cid: str, text: str):
    """A question the Commander TYPED into the call's conversation: not this call's own turn."""
    import uuid

    task, _ = await ask_orchestrator.begin(cid, str(uuid.uuid4()), text, working_keys=set())
    return task


def test_an_answer_the_commander_is_no_longer_waiting_on_is_one_fixed_line(rig, held):
    """Behind a turn that is not the call's own (a typed one), each spoken question is a NEW
    question: the older one's answer is on screen and HQ says only that it has it; the newest
    question's answer is spoken."""

    async def scenario():
        script = held(answer("Typed."), answer("Two."), answer("Three."))
        typed = await _typed(rig.call.cid, "typed while on the call")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)
        await _speak(rig, "Draft a status note.")
        await _queued(rig, 2)
        assert rig.said_text() == []
        script.go.set()
        await typed
        await _until(lambda: "Three." in rig.said_text())
        await _until(lambda: rig.call.state == "listening")

    run(scenario())
    assert _statuses(rig.call.cid) == [
        "pending", "queued", "queued", "done", "pending", "done", "pending", "done",
    ]  # fmt: skip
    assert rig.said_text() == [READY, "Three."]


def test_a_spoken_stop_stops_the_running_task_and_leaves_the_queue(rig, held):
    async def scenario():
        script = held(answer("Two."))  # the stopped request is abandoned, never answered
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)
        await _speak(rig, "Stop.")
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        # The queued question starts behind the stopped one.
        await _until(lambda: _statuses(rig.call.cid)[-1] == "pending")
        script.go.set()
        await _until(lambda: "Two." in rig.said_text())
        return script

    script = run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "queued", "stopped", "pending", "done"]
    assert rig.said_text() == [FALLBACK, "Stopped.", "Two."]
    assert _asked(script) == ["And who is working on it?"] and rig.call.state != "ended"


def test_stop_everything_stops_the_running_task_and_withdraws_the_queue(rig, held):
    async def scenario():
        script = held(answer("never said"))
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)  # (words spoken before that verdict would join it, #2113 P3)
        await _speak(rig, "Draft a status note.")
        await _queued(rig, 2)
        await _speak(rig, "Stop everything.")
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: not ask_orchestrator._RUNNING)
        await _until(lambda: rig.call.state == "listening")
        return script

    script = run(scenario())
    assert sorted(_statuses(rig.call.cid)) == ["pending", "queued", "queued", "stopped",
                                               "withdrawn", "withdrawn"]  # fmt: skip
    assert rig.said_text() == [FALLBACK, FALLBACK, "Stopped."] and script.bodies == []
    assert rig.call.state == "listening" and rig.call.ending is None  # the call goes on
    heard = [m["text"] for m in rig.to_client if m.get("t") == "caption" and m.get("who") == "you"]
    assert heard[-1] == "Stop everything."  # captioned, never asked of HQ


def test_stop_everything_with_nothing_running_is_asked_of_hq(rig, ai):
    async def scenario():
        script = ai(answer("Nothing is running, Commander."))
        await _speak(rig, "Stop everything.")
        await _until(lambda: rig.said_text() != [])
        return script

    script = run(scenario())
    assert rig.said_text() == ["Nothing is running, Commander."]
    assert "Stop everything." in str(script.bodies[0]["messages"][-1]["content"])


def test_a_call_that_ended_leaves_its_questions_queued_and_is_never_spoken_to(rig, held):
    async def scenario():
        script = held(answer("One."), answer("Two."))
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)
        await rig.call.end("hangup")
        assert ask_orchestrator.queued(rig.call.cid) != []  # still queued in the conversation
        sent = (len(rig.to_client), len(rig.to_worker))
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid)[-2:] == ["pending", "done"])
        await _until(lambda: not ask_orchestrator._RUNNING)
        await asyncio.sleep(0.1)
        return script, sent

    script, sent = run(scenario())
    # Both ran, the queued one as a typed one would; nothing was said, shown or sent to the
    # ended call.
    assert _statuses(rig.call.cid) == ["pending", "queued", "done", "pending", "done"]
    assert _asked(script) == ["How far is the epic?", "And who is working on it?"]
    assert rig.said_text() == [FALLBACK] and (len(rig.to_client), len(rig.to_worker)) == sent


def test_a_turn_shown_on_a_live_call_is_seen_and_one_after_the_call_is_announced(
    rig, held, monkeypatch
):
    """The call screen is a display: what it was given is seen, so the bell stays quiet during
    the call. A queued question that settles after the hang-up is announced like any other."""
    from agent_sessions import hq_tasks, notifications

    monkeypatch.setenv("AGENT_SESSIONS_HQ_TASK_NOTICES", "1")
    # Long enough for the call to be given the turn first, also on a loaded runner.
    monkeypatch.setattr(hq_tasks, "SETTLE_NOTIFY_GRACE_S", 2.0)

    async def scenario():
        script = held(answer("One."), answer("Two."), answer("Three."))
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await _queued(rig, 1)
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid).count("done") == 2)
        await _until(lambda: rig.call.state == "listening")
        await _until(lambda: not hq_tasks._NOTICES)
        during = [r["title"] for r in notifications.listing()["notifications"]]
        # A third one, held; the Commander hangs up while a fourth waits behind it.
        script.go.clear()
        await _speak(rig, "Draft a status note.")
        await _until(lambda: ask_orchestrator.running(rig.call.cid))
        await _speak(rig, "And send it to the channel.")
        await _queued(rig, 1)
        await rig.call.end("hangup")
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid).count("done") == 4)
        await _until(lambda: not ask_orchestrator._RUNNING)
        await _until(lambda: not hq_tasks._NOTICES)
        return during

    during = run(scenario())
    assert during == []
    rows = notifications.listing()["notifications"]
    assert [r["title"] for r in rows] == ["HQ answered", "HQ answered"]
    assert {r["hq_conversation"] for r in rows} == {rig.call.cid}


def _suspend_queued_send(rig):
    """The call's client send of a `queued` message blocks until released (a slow socket): the
    window of review 8407 on #2125, in which the running turn settles and the queued one starts
    — and here even settles — before the call has looked for it."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = rig.call._to_client

    async def to_client(m):
        if isinstance(m, dict) and m.get("t") == "queued":
            reached.set()
            await release.wait()
        await real(m)

    rig.call._to_client = to_client
    return reached, release


@pytest.mark.parametrize("behind", ["own turn", "typed turn"])
def test_a_queued_answer_is_spoken_once_when_its_turn_started_and_settled_during_the_queued_send(
    rig, held, behind
):
    """Both queued-call paths keep the start handle they took with the admission across the
    client send: a turn promoted (and settled) while that send was suspended is still followed,
    presented and spoken, exactly once — never taken for one that never started."""

    async def scenario():
        script = held(answer("First."), answer("Newest."), answer("Newest."))
        reached, release = _suspend_queued_send(rig)
        if behind == "own turn":
            await _speak(rig, "How far is the epic?")  # then `_ask_behind` queues the next
            await asyncio.wait_for(script.reached.wait(), 15)
        else:
            # A typed turn holds the slot and the call does not know: `_dispatch` asks into
            # what it takes for a free slot, and `_run_turn` finds the question queued.
            typed = await _typed(rig.call.cid, "typed while on the call")
            await asyncio.wait_for(script.reached.wait(), 15)
            real_running = ask_orchestrator.running
            ask_orchestrator.running = lambda cid: False
        try:
            await _speak(rig, "And who is working on it?")
            await asyncio.wait_for(reached.wait(), 15)
        finally:
            if behind != "own turn":
                ask_orchestrator.running = real_running
        assert ask_orchestrator.queued(rig.call.cid) != []
        script.go.set()
        # The promotion AND both settles happen while the send is still suspended.
        await _until(lambda: _statuses(rig.call.cid).count("done") == 2)
        await _until(lambda: not ask_orchestrator._RUNNING)
        if behind != "own turn":
            await typed
        release.set()
        await _until(lambda: "Newest." in rig.said_text())
        await _until(lambda: rig.call.state == "listening")
        await asyncio.sleep(0.2)

    run(scenario())
    said = rig.said_text()
    assert said.count("Newest.") == 1 and READY not in said
    assert said == ([FALLBACK, "First.", "Newest."] if behind == "own turn" else ["Newest."])
    turns = [m["turn"] for m in rig.to_client if isinstance(m, dict) and m.get("t") == "turn"]
    newest = [t for t in turns if t["question"] == "And who is working on it?"]
    assert [t["status"] for t in newest] == ["done"]  # presented exactly once


def test_a_call_that_ended_during_the_queued_send_is_given_nothing(rig, held):
    async def scenario():
        script = held(answer("First."), answer("Newest."), answer("Newest."))
        reached, release = _suspend_queued_send(rig)
        await _speak(rig, "How far is the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "And who is working on it?")
        await asyncio.wait_for(reached.wait(), 15)
        await rig.call.end("hangup")
        sent = (len(rig.to_client), len(rig.to_worker))
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid).count("done") == 2)
        await _until(lambda: not ask_orchestrator._RUNNING)
        release.set()
        await asyncio.sleep(0.3)
        return sent

    sent = run(scenario())
    # Both ran (the queued one as a typed one would); the ended call was said and shown nothing
    # more than the one `queued` message that was already on its way.
    assert _statuses(rig.call.cid) == ["pending", "queued", "done", "pending", "done"]
    assert rig.said_text() == [FALLBACK]
    after = (len(rig.to_client), len(rig.to_worker))
    assert after[1] == sent[1] and after[0] - sent[0] <= 1
    assert not [m for m in rig.to_client[sent[0] :] if m.get("t") in ("turn", "caption")]


def test_a_full_queue_is_said_in_a_fixed_line(rig, held):
    async def scenario():
        held(answer("never"))
        await _speak(rig, "First question?")
        await _until(lambda: ask_orchestrator.running(rig.call.cid))
        for i in range(ask_orchestrator.QUEUE_MAX):
            await _speak(rig, f"Another question {i}?")
            await _queued(rig, i + 1)
        await _speak(rig, "One too many?")
        await _until(lambda: voice_calls.QUEUE_FULL_LINE in rig.said_text())
        assert len(ask_orchestrator.queued(rig.call.cid)) == ask_orchestrator.QUEUE_MAX

    run(scenario())
    assert rig.said_text()[-1] == voice_calls.QUEUE_FULL_LINE
    assert rig.said_text().count(voice_calls.QUEUE_FULL_LINE) == 1


def test_the_fixed_lines_are_number_free():
    """Spoken status lines carry no count: no digit and no number word used as a count."""
    assert voice_calls.ANSWER_READY_LINE == "I have that answer now."
    for line in (voice_calls.ANSWER_READY_LINE, voice_calls.QUEUE_FULL_LINE):
        assert not re.search(r"\d|\b(two|three|four|five|six|seven|eight|nine|ten)\b", line.lower())


@pytest.mark.parametrize(
    ("said", "languages", "stops"),
    [
        ("Stop everything.", None, True),
        ("stop all!", frozenset({"en"}), True),
        ("Cancel everything", None, True),
        ("Alles stoppen.", frozenset({"de"}), True),
        ("Stopp alles", frozenset({"de", "en"}), True),
        ("alles abbrechen", frozenset({"en"}), False),  # only in a listened language
        ("Stop everything on the staging box", None, False),  # the WHOLE utterance
        ("stop", None, False),  # the plain stop is its own phrase
        ("", None, False),
    ],
)
def test_a_stop_everything_phrase_is_the_whole_utterance_in_a_listened_language(
    said, languages, stops
):
    assert voice_text.is_stop_all_phrase(said, languages) is stops


def test_the_stop_everything_phrases_cover_the_stop_languages_and_never_collide():
    assert set(voice_text.STOP_ALL_PHRASES) == set(voice_text.STOP_PHRASES)
    for lang, phrases in voice_text.STOP_ALL_PHRASES.items():
        assert phrases and not (phrases & voice_text.STOP_PHRASES[lang])
        for phrase in phrases:
            assert not voice_text.is_no_speech_phrase(phrase)
