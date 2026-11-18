"""#2076 on a call: "stop" stops HQ's running turn, never the call.

An utterance that is NOTHING but a stop phrase, heard while a turn runs, stops that turn; HQ says
"Stopped." and keeps listening. With nothing running the same words go to HQ like any others,
and a sentence that merely contains one is a request. The call screen's Stop does the same.
Driven through the call with the REAL orchestrator and a scripted model endpoint."""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_sessions import ask_orchestrator, ask_store, voice_calls, voice_text
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures
from test_ask_orchestrator import ai, answer, app  # noqa: F401 — fixtures
from test_voice_confirm import Rig
from test_voice_draft_turns import _speak, _until

#: The call's own records (#2137): what it heard and where that went. Not an HQ turn.
_CALL_RECORDS = ("heard", "heard_link", "said", "counts")


def _only_the_call_heard(rig, words: list[str]) -> None:
    """The conversation holds no HQ turn: only the call's record of the words it heard, and the
    spoken stop linked as a stop (#2137). The discarded draft's words are linked to no turn."""
    recs = ask_store.read(rig.call.cid)
    assert [r["type"] for r in recs if r["type"] not in _CALL_RECORDS] == ["conversation"]
    assert [r["text"] for r in recs if r["type"] == "heard"] == words
    assert [r["route"] for r in recs if r["type"] == "heard_link"] == ["stop"]


@pytest.fixture
def rig(monkeypatch, app):  # noqa: F811
    real_begin = ask_orchestrator.begin
    r = Rig(monkeypatch)
    monkeypatch.setattr(ask_orchestrator, "begin", real_begin)  # the real orchestrator
    r.call.cid = app.new()
    return r


def _statuses(cid: str) -> list[str]:
    return [r["status"] for r in ask_store.read(cid) if r.get("type") == "status"]


@pytest.mark.parametrize(
    ("said", "languages", "stops"),
    [
        ("Stop.", None, True),
        ("stop!", frozenset({"en"}), True),
        ("Cancel that.", None, True),
        ("Never mind", None, True),
        ("Stopp!", frozenset({"de"}), True),
        ("Abbrechen.", frozenset({"de", "en"}), True),
        # Only in the languages the call listens in.
        ("abbrechen", frozenset({"en"}), False),
        ("basta", frozenset({"en", "de"}), False),
        # The WHOLE utterance: a sentence that contains one is a request.
        ("Stop the staging deploy", None, False),
        ("cancel that mission please, and tell me why", None, False),
        ("don't stop", None, False),
        ("", None, False),
        ("...", None, False),
    ],
)
def test_a_stop_phrase_is_the_whole_utterance_in_a_listened_language(said, languages, stops):
    assert voice_text.is_stop_phrase(said, languages) is stops


def test_no_stop_phrase_is_a_goodbye_or_a_stock_noise_phrase():
    """Stopping never ends the call, and room noise is dropped before a stop is even considered."""
    for phrases in voice_text.STOP_PHRASES.values():
        for phrase in phrases:
            assert not voice_text.is_no_speech_phrase(phrase)
            assert not any(bye in phrase for bye in ("bye", "tschüss", "ciao", "hang up"))


def test_saying_stop_while_hq_works_stops_the_turn_and_the_call_stays_open(rig, held):  # noqa: F811
    async def scenario():
        script = held(answer("never said"))
        await _speak(rig, "How far are we with the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        assert rig.call.state == "thinking" and ask_orchestrator.running_turn(rig.call.cid)
        await _speak(rig, "Stop.")
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: rig.call.state == "listening")
        return script

    script = run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "stopped"]
    assert rig.said_text() == ["Stopped."]  # no answer, and no "Sorry, I couldn't answer that"
    assert rig.call.state == "listening" and rig.call.ending is None  # the call goes on
    # The request in flight was abandoned, and "stop" itself was never asked of HQ.
    assert script.bodies == [] and not script.go.is_set()
    turns = [m["turn"] for m in rig.to_client if isinstance(m, dict) and m.get("t") == "turn"]
    assert [t["status"] for t in turns] == ["stopped"]
    heard = [m["text"] for m in rig.to_client if m.get("t") == "caption" and m.get("who") == "you"]
    assert heard == ["How far are we with the epic?", "Stop."]


def test_stop_with_nothing_running_is_asked_of_hq_like_any_other_words(rig, ai):  # noqa: F811
    async def scenario():
        script = ai(answer("Nothing is running, Commander."))
        await _speak(rig, "Stop.")
        await _until(lambda: rig.said_text() != [])
        return script

    script = run(scenario())
    assert rig.said_text() == ["Nothing is running, Commander."]
    assert _statuses(rig.call.cid) == ["pending", "done"]
    assert "Stop." in str(script.bodies[0]["messages"][-1]["content"])


def test_a_request_that_contains_stop_does_not_stop_the_turn(rig, held):  # noqa: F811
    async def scenario():
        script = held(answer("first"), answer("second"))
        await _speak(rig, "How far are we with the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await _speak(rig, "Stop the staging deploy when it is done")
        # A new request, kept for when this turn settles (#2113 P3: once the interjection
        # judgement has answered — here, no verdict, so it waits as it always did).
        await _until(lambda: rig.call.queued is not None)
        assert voice_calls.STOPPED_LINE not in rig.said_text()
        script.go.set()
        await _until(lambda: len(_statuses(rig.call.cid)) == 4)

    run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "done", "pending", "done"]


def test_noise_and_hqs_own_echo_never_stop_the_turn(rig, held):  # noqa: F811
    """The "not the operator" drops run first: an utterance in a language the call does not
    listen in is dropped before it can be a stop phrase."""

    async def scenario():
        script = held(answer("the answer"))
        rig.call.listen_languages = frozenset({"en"})
        await _speak(rig, "How far are we with the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        await rig.ev(t="user_started")
        await rig.ev(t="utterance", text="Stop.", heard_language="fr")
        await rig.ev(t="turn_end", turn=rig.turn)
        await asyncio.sleep(0.1)
        assert ask_orchestrator.running_turn(rig.call.cid) is not None
        script.go.set()
        await _until(lambda: rig.said_text() != [])

    run(scenario())
    assert rig.said_text() == ["the answer"] and _statuses(rig.call.cid) == ["pending", "done"]


def test_the_call_screens_stop_stops_the_turn_and_nothing_else(rig, held):  # noqa: F811
    async def scenario():
        assert rig.call.stop_turn() is False  # nothing running: nothing said, nothing changed
        script = held(answer("never said"))
        await _speak(rig, "How far are we with the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        assert rig.call.stop_turn() is True
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: rig.call.state == "listening")

    run(scenario())
    assert rig.said_text() == ["Stopped."] and rig.call.state != "ended"
    assert _statuses(rig.call.cid) == ["pending", "stopped"]


def test_stop_drops_a_draft_that_was_not_committed_yet(rig, held, monkeypatch):  # noqa: F811
    """With turn-taking drafts (#1845) the words may still be a draft: "stop" drops it — nothing
    of it was stored or shown — and is never joined to it as more of the question."""

    async def scenario():
        rig.call.commit_pause = 30.0  # never commits on its own within this test
        script = held(answer("never said"))
        await _speak(rig, "How far are we with the epic?")
        await asyncio.wait_for(script.reached.wait(), 15)
        assert rig.call.draft is not None
        await _speak(rig, "Never mind.")
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: not ask_orchestrator._RUNNING)

    run(scenario())
    _only_the_call_heard(rig, ["How far are we with the epic?", "Never mind."])
    assert rig.said_text() == ["Stopped."] and rig.call.draft is None


def test_the_stopped_line_and_the_status_lines_carry_no_number():
    lines = [
        voice_calls.STOPPED_LINE,
        voice_calls.LOOKING,
        voice_calls.WORKING,
        voice_calls.STALLED,
    ]
    for said in [*lines, *voice_calls.STILL_WORKING]:
        assert not any(ch.isdigit() for ch in said)


def test_the_call_screen_is_told_the_step_and_how_many_are_done(rig, ai, monkeypatch):  # noqa: F811
    async def look(turn, args):
        return {"ok": True}, {"count": 1}

    spec = ask_orchestrator._spec("look", "a test read", {}, [])
    monkeypatch.setitem(
        ask_orchestrator.TOOLS, "look", ask_orchestrator.Tool("look", "read", spec, look)
    )

    async def scenario():
        from test_ask_orchestrator import calls

        ai(calls(("look", {}), ("look", {})), answer("Two things."))
        await _speak(rig, "Look twice.")
        await _until(lambda: rig.said_text() != [])

    run(scenario())
    progress = [m for m in rig.to_client if isinstance(m, dict) and m.get("t") == "progress"]
    assert [(m["step"], m["steps"]) for m in progress] == [
        ("thinking", 0),
        ("look", 0),
        ("look", 1),
        ("thinking", 2),
    ]


# ---- a stop against a draft's commit (review of #2080) ------------------------------------------


def _hold_the_stop_caption(rig: Rig) -> tuple[asyncio.Event, asyncio.Event]:
    """The send of the spoken stop's own caption waits for `go` (and says it was `reached`)."""
    reached, go = asyncio.Event(), asyncio.Event()
    real = rig.call._to_client

    async def to_client(m):
        if isinstance(m, dict) and m.get("t") == "caption" and m.get("who") == "you":
            if voice_text.is_stop_phrase(str(m.get("text") or "")):
                reached.set()
                await go.wait()
        await real(m)

    rig.call._to_client = to_client
    return reached, go


def test_a_draft_the_quiet_timer_tries_to_commit_during_a_spoken_stop_is_still_discarded(rig, held):  # noqa: F811
    """The stop claims the open draft before anything awaits. While its caption is still being
    sent, the quiet timer's commit finds the draft gone: the turn is never committed, nothing of
    it is stored, and "Stopped." is true."""

    async def scenario():
        rig.call.commit_pause = 30.0
        script = held(answer("never said"))
        reached, go = _hold_the_stop_caption(rig)
        await _speak(rig, "Send the release notes to the docs session.")
        await asyncio.wait_for(script.reached.wait(), 15)
        d = rig.call.draft
        assert d is not None
        await _speak(rig, "Stop.")
        await asyncio.wait_for(reached.wait(), 15)  # the stop is parked at its caption send
        await rig.call._commit_draft(d)  # the quiet timer fires NOW
        assert not d.committed.is_set() and ask_orchestrator.running_turn(rig.call.cid) is None
        go.set()
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: not ask_orchestrator._RUNNING)
        return script

    script = run(scenario())
    # The captured turn really ended: never stored, never answered, the endpoint never let go.
    recs = ask_store.read(rig.call.cid)
    assert [r["type"] for r in recs if r["type"] not in _CALL_RECORDS] == ["conversation"]
    assert [r["route"] for r in recs if r["type"] == "heard_link"] == ["stop"]
    assert rig.said_text() == ["Stopped."] and not script.go.is_set()


def test_a_stop_heard_while_a_drafts_commit_is_being_written_stops_that_turn(
    rig, held, monkeypatch
):  # noqa: F811
    """During the commit's write the draft is neither `self.draft` nor a visible running turn.
    A stop heard then is still a stop: it waits for the commit and stops the turn by its id."""
    real_commit = ask_orchestrator.commit

    async def scenario():
        rig.call.commit_pause = 30.0
        script = held(answer("never said"))
        in_commit, go = asyncio.Event(), asyncio.Event()

        async def slow_commit(cid, turn_id):
            in_commit.set()
            await go.wait()
            return await real_commit(cid, turn_id)

        monkeypatch.setattr(ask_orchestrator, "commit", slow_commit)
        await _speak(rig, "Send the release notes to the docs session.")
        await asyncio.wait_for(script.reached.wait(), 15)
        d = rig.call.draft
        committing = asyncio.create_task(rig.call._commit_draft(d))
        await asyncio.wait_for(in_commit.wait(), 15)
        # The window: the draft is claimed for its commit, and nothing is visibly running yet.
        assert rig.call.draft is None and ask_orchestrator.running_turn(rig.call.cid) is None
        await _speak(rig, "Stop.")
        assert rig.call.queued is None and voice_calls.STOPPED_LINE not in rig.said_text()
        go.set()
        await committing
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: not ask_orchestrator._RUNNING)
        return d.turn_id, script

    turn_id, script = run(scenario())
    records = ask_store.read(rig.call.cid)
    assert [r["text"] for r in records if r["type"] == "user"] == [
        "Send the release notes to the docs session."
    ]  # "Stop." was never asked of HQ, nor joined to the question
    statuses = [r["status"] for r in records if r["type"] == "status" and r["turn_id"] == turn_id]
    assert statuses == ["pending", "stopped"]
    assert rig.said_text() == ["Stopped."] and not script.go.is_set()


def test_stopped_is_not_said_for_a_turn_that_answered_first(rig, ai, monkeypatch):  # noqa: F811
    """The answer won the settle: nothing was stopped, so HQ does not claim it was."""

    async def never_stops(cid, turn_id):
        return False

    async def scenario():
        monkeypatch.setattr(ask_orchestrator, "stop", never_stops)
        monkeypatch.setattr(ask_orchestrator, "running_turn", lambda cid: str(uuid.uuid4()))
        assert rig.call.stop_turn(said="Stop.") is True
        await asyncio.sleep(0.2)

    run(scenario())
    assert voice_calls.STOPPED_LINE not in rig.said_text()


def test_a_stop_during_a_drafts_commit_never_starts_the_effect_waiting_on_it(rig, ai, monkeypatch):
    """The draft's model reply asked for a send, which waits for the commit (`_call`). Stop is
    accepted while the commit's append is being written. Its flag is on that turn BEFORE the
    commit releases the waiter, so the send — not in flight when the Commander said stop —
    never starts, and "Stopped." is true (review round 2 of #2080)."""
    from test_ask_orchestrator import calls

    started: list[str] = []

    async def send(turn, args):
        started.append("effect_started")
        return {"sent": True}, {"sent": "yes", "count": 1}

    spec = ask_orchestrator._spec("send_thing", "a test send", {}, [])
    tool = ask_orchestrator.Tool("send_thing", "send", spec, send)
    monkeypatch.setitem(ask_orchestrator.TOOLS, "send_thing", tool)
    real_append = ask_orchestrator._append

    async def scenario():
        rig.call.commit_pause = 30.0
        script = ai(calls(("send_thing", {})), answer("Sent."))
        in_append, go = asyncio.Event(), asyncio.Event()

        async def held_append(cid, *records):
            if any(r.get("type") == "user" for r in records):  # the commit's own write
                in_append.set()
                await go.wait()
            return await real_append(cid, *records)

        monkeypatch.setattr(ask_orchestrator, "_append", held_append)
        await _speak(rig, "Send the release notes to the docs session.")
        await _until(lambda: len(script.bodies) == 1)
        await asyncio.sleep(0.2)  # the reply is in: `send_thing` is parked on the commit
        d = rig.call.draft
        turn = ask_orchestrator._RUNNING[rig.call.cid].turn
        assert d is not None and started == [] and not turn.draft.committed.is_set()
        committing = asyncio.create_task(rig.call._commit_draft(d))
        await asyncio.wait_for(in_append.wait(), 15)
        await _speak(rig, "Stop.")
        # The fence is on the exact turn NOW: not behind the commit, nor behind the caption.
        assert turn.stop.is_set() and not turn.draft.committed.is_set()
        go.set()
        await committing
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())
        await _until(lambda: not ask_orchestrator._RUNNING)
        return d.turn_id, script

    turn_id, script = run(scenario())
    assert started == []  # the send never started
    records = ask_store.read(rig.call.cid)
    statuses = [r["status"] for r in records if r["type"] == "status" and r["turn_id"] == turn_id]
    assert statuses == ["pending", "stopped"]
    assert [r for r in records if r["type"] == "tool"] == []
    assert rig.said_text() == ["Stopped."] and len(script.bodies) == 1
