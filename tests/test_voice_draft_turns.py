"""#1845 on a call: HQ drafts its answer as soon as the words arrive and commits it only once the
Commander has been quiet for the turn-taking setting's pause, timed from the worker's own stop.
More words before then join the draft; nothing of a draft is said, shown or stored before its
commit. Driven through the call with the REAL orchestrator and a scripted model endpoint."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_sessions import ask_orchestrator, ask_store, voice_calls
from test_ask_draft_turns import _free, held, run  # noqa: F401 — fixtures
from test_ask_orchestrator import ai, answer, app  # noqa: F401 — fixtures
from test_voice_confirm import Rig

PAUSE = 0.6  # the commit pause these tests run with
STOP = 0.1  # the worker's own stop, already part of the pause


@pytest.fixture
def rig(monkeypatch, app):  # noqa: F811
    real_begin = ask_orchestrator.begin
    r = Rig(monkeypatch)
    monkeypatch.setattr(ask_orchestrator, "begin", real_begin)  # the real orchestrator
    monkeypatch.setattr(voice_calls, "WORKER_STOP_S", STOP)
    r.call.cid = app.new()
    r.call.commit_pause = PAUSE
    return r


def _questions(cid: str) -> list[str]:
    return [r["text"] for r in ask_store.read(cid) if r.get("type") == "user"]


def _rounds(script) -> int:
    """HQ's own model requests (they offer tools), not the call's farewell judgement."""
    return sum(1 for b in script.bodies if "tools" in b)


def _answers(r: Rig) -> list[str]:
    return r.said_text()


async def _speak(r: Rig, text: str, *, stopped_ago: float = 0.0, end: bool = True) -> None:
    """One worker turn: started, stopped (at its own instant), its transcript, its end."""
    await r.ev(t="user_started")
    await r.ev(t="user_stopped", turn=r.turn, at=time.monotonic() - stopped_ago)
    await r.ev(t="utterance", text=text)
    if end:
        await r.ev(t="turn_end", turn=r.turn)


async def _until(pred, timeout: float = 20.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def _done(r: Rig) -> None:
    await _until(lambda: r.call.turn_task is not None and r.call.turn_task.done())


def test_a_fast_answer_waits_for_the_pause_and_nothing_shows_before_it(rig, held):  # noqa: F811
    script = held(answer("All quiet, Commander."))
    script.go.set()
    r = rig

    async def go():
        await _speak(r, "Anything running?")
        stopped = time.monotonic()
        await _until(lambda: _rounds(script) == 1)
        await asyncio.sleep(0.2)  # the model has answered; the pause has not passed
        assert r.call.draft is not None
        assert _answers(r) == [] and _questions(r.call.cid) == []
        assert r.call.state == "listening"  # not even "thinking" is shown for a draft
        assert not any(m.get("t") == "progress" for m in r.to_client)
        await _done(r)
        return time.monotonic() - stopped

    took = run(go())
    assert took >= PAUSE - STOP - 0.05
    assert _answers(r) == ["All quiet, Commander."]
    assert _questions(r.call.cid) == ["Anything running?"]


def test_the_pause_is_timed_from_the_workers_stop_not_the_transcript(rig, held):  # noqa: F811
    script = held(answer("Two missions."))
    script.go.set()
    r = rig

    async def go():
        # The transcript trails the stop by most of the pause (the STT): little is left to wait.
        await _speak(r, "How many missions?", stopped_ago=PAUSE - STOP - 0.1)
        arrived = time.monotonic()
        await _done(r)
        return time.monotonic() - arrived

    assert run(go()) < 0.35
    assert _questions(r.call.cid) == ["How many missions?"]


def test_more_words_before_the_commit_join_the_draft(rig, held):  # noqa: F811
    script = held(answer("Release is green and the docs session finished."))
    script.go.set()
    r = rig

    async def go():
        await _speak(r, "I wanted to ask about the release")
        await asyncio.sleep(0.2)  # a breath shorter than the pause
        await _speak(r, "and whether the docs session finished.")
        await _done(r)

    run(go())
    whole = "I wanted to ask about the release and whether the docs session finished."
    assert _questions(r.call.cid) == [whole]  # one question, stored once
    asked = [b for b in script.bodies if "tools" in b]
    assert whole in str(asked[-1]["messages"])
    assert _answers(r) == ["Release is green and the docs session finished."]
    assert _free(r.call.cid) and not ask_orchestrator._RUNNING


def test_a_late_transcript_joins_the_draft(rig, held):  # noqa: F811
    script = held(answer("Done."))
    script.go.set()
    r = rig

    async def go():
        await _speak(r, "Restart the build", end=False)  # part of it is still at STT
        await asyncio.sleep(0.1)
        await r.ev(t="utterance", text="on the api session.", late=True)
        await r.ev(t="turn_end", turn=r.turn)  # only after its late words (Hermes 7639)
        await _done(r)

    run(go())
    assert _questions(r.call.cid) == ["Restart the build on the api session."]


def test_a_breath_with_no_words_postpones_the_commit_and_keeps_the_draft(rig, held):  # noqa: F811
    script = held(answer("Nothing is waiting."))
    script.go.set()
    r = rig

    async def go():
        await _speak(r, "What is waiting for me?")
        await asyncio.sleep(0.1)
        await r.ev(t="user_started")  # noise, or a breath
        await r.ev(t="user_stopped", turn=r.turn, at=time.monotonic())
        await asyncio.sleep(PAUSE)  # the pause has passed, but its turn has not ended
        assert r.call.draft is not None and _questions(r.call.cid) == []
        await r.ev(t="turn_end", turn=r.turn)  # it brought no words
        await _done(r)

    run(go())
    assert _rounds(script) == 1  # the same draft, never asked again
    assert _questions(r.call.cid) == ["What is waiting for me?"]
    assert _answers(r) == ["Nothing is waiting."]


def test_an_old_draft_can_never_be_committed_once_superseded(rig, held):  # noqa: F811
    script = held(answer("ok"))
    r = rig

    async def go():
        await _speak(r, "First part")
        await _until(lambda: script.reached.is_set())
        old = r.call.draft
        await _speak(r, "and the rest.")
        await _until(lambda: r.call.draft is not None and r.call.draft is not old)
        await r.call._commit_draft(old)  # a stale timer, firing late
        assert _questions(r.call.cid) == []
        assert r.call.draft is not None and r.call.draft is not old
        script.go.set()
        await _done(r)

    run(go())
    assert _questions(r.call.cid) == ["First part and the rest."]


def test_a_typed_turn_discards_the_draft(rig, held):  # noqa: F811
    script = held(answer("typed answer"), free=0)
    r = rig

    async def go():
        await _speak(r, "Spoken half")
        await _until(lambda: script.reached.is_set())
        task, _ = await ask_orchestrator.begin(
            r.call.cid, "00000000-0000-4000-8000-0000000000aa", "Typed instead", working_keys=set()
        )
        r.call.on_typed_turn(task)
        script.go.set()
        await task
        await _done(r)

    run(go())
    assert _questions(r.call.cid) == ["Typed instead"]
    assert _answers(r) == []  # the dropped draft says nothing
    assert r.call.draft is None


def test_hanging_up_drops_the_draft_and_frees_the_conversation(rig, held):  # noqa: F811
    script = held(answer("never said"))
    r = rig

    async def go():
        await _speak(r, "Check the")
        await _until(lambda: script.reached.is_set())
        await r.call.end("hung_up")
        await _done(r)

    run(go())
    assert _questions(r.call.cid) == [] and _answers(r) == []
    assert _free(r.call.cid) and not ask_orchestrator._RUNNING


def test_muting_asks_what_was_said_at_once(rig, held):  # noqa: F811
    script = held(answer("Asked."))
    script.go.set()
    r = rig

    async def go():
        await _speak(r, "Archive the idle sessions")
        await _until(lambda: r.call.draft is not None)
        r.call.muted = True
        await r.call.commit_draft()
        assert _questions(r.call.cid) == ["Archive the idle sessions"]
        await _done(r)

    run(go())
    assert _answers(r) == ["Asked."]


def test_quick_drafts_nothing(rig, held):  # noqa: F811
    script = held(answer("Fast."))
    script.go.set()
    r = rig
    r.call.commit_pause = voice_calls.COMMIT_PAUSE_S["quick"]

    async def go():
        await _speak(r, "Status?")
        await asyncio.sleep(0)
        assert r.call.draft is None
        await _done(r)

    run(go())
    assert _questions(r.call.cid) == ["Status?"]


@pytest.mark.parametrize(("setting", "pause"), [("quick", 0.0), ("natural", 1.5), ("patient", 2.5)])
def test_each_setting_waits_its_own_pause_and_the_worker_stops_short(setting, pause):
    settings = {**voice_calls.hq_calls_settings.get(), "turn_taking": setting}
    assert voice_calls.commit_pause(settings) == pause
    assert voice_calls.worker_config(settings, "aria")["stop_secs"] == voice_calls.WORKER_STOP_S
