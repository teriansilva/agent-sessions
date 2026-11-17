"""#1702 — a slow HQ turn never reads as a dropped call.

While an HQ turn runs on a call, HQ says a short FIXED status line whenever it has been quiet too
long: first after FIRST_PROGRESS_S, then every PROGRESS_EVERY_S, and every STALL_EVERY_S once the
model has kept it waiting STALL_S. Each FIXED line is short and casual, picked from the turn's
real state only — whether a read tool runs (by tool NAME), how many status lines the turn has said,
whether the model has stalled — never tool arguments or tool output, and never a duration or a
step count. A status line's playback is no sign of the operator, so a forgotten call is still
asked and hung up (#1668).

#2113 P2 — HQ thinks out loud: when the model wrote its own short aside beside a round of tool
calls (through the store's text boundary, `ask_orchestrator.aside_text`), that aside is said
INSTEAD of the fixed line, after ASIDE_QUIET_S of quiet and never within ASIDE_GAP_S of the last
status line. With no aside the fixed lines are unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import ask_orchestrator, voice_calls
from test_voice_call_end import Clock
from test_voice_confirm import Rig, run


def progress_says(r: Rig) -> list[dict]:
    """The status lines said."""
    ids = {
        m["id"]
        for m in r.to_client
        if isinstance(m, dict) and m.get("t") == "caption" and m.get("kind") == "progress"
    }
    return [s for s in r.says() if s["id"] in ids]


def thinking(r: Rig, clock: Clock, **work) -> dict:
    """A call whose HQ turn is in flight, HQ quiet since now."""
    r.call.state = "thinking"
    r.call.hq_quiet_at = clock.now
    r.call.work = {
        "since": clock.now,
        "phase": "model",
        "phase_since": clock.now,
        "tool": None,
        "updates": 0,
        **work,
    }
    return r.call.work


# --- the fixed lines -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("work", "waited", "line"),
    [
        ({"phase": "tool", "tool": "session_status"}, 20, "Let me check."),
        ({"phase": "tool", "tool": "find_sessions"}, 20, "Let me check."),
        ({"phase": "tool", "tool": "send_nudge"}, 15, "Working on it."),
        ({"phase": "tool", "tool": "propose_session"}, 15, "Working on it."),
        ({"phase": "tool", "tool": "no_such_tool"}, 15, "Working on it."),
        ({"phase": "model"}, 12, "Working on it."),
        ({"phase": None}, 25, "Working on it."),
        ({"phase": "tool", "tool": "session_status", "updates": 2}, 40, "Still on it."),
        ({"phase": "model", "updates": 3}, 60, "One moment."),
        ({"phase": "model", "updates": 4}, 80, "Still working on this."),
        ({"phase": "tool", "tool": "session_status", "updates": 5}, 99, "Still on it."),
        ({"phase": "model", "updates": 1}, 135, "This is taking longer than usual. Still on it."),
        ({"phase": "model", "updates": 6}, 135, "This is taking longer than usual. Still on it."),
        # A tool that runs long is not a stalled model.
        ({"phase": "tool", "tool": "session_status", "updates": 1}, 135, "Let me check."),
    ],
)  # fmt: skip
def test_each_state_has_its_fixed_line(work, waited, line):
    now = 1_000.0
    full = {"since": now - waited, "phase_since": now - waited, "updates": 1, **work}
    assert voice_calls.progress_line(full, now) == line


def test_no_status_line_says_how_long_or_how_many_steps():
    """The Commander asked for "let me check", not a stopwatch: no line carries a number."""
    lines = [voice_calls.LOOKING, voice_calls.WORKING, voice_calls.STALLED]
    for said in lines + list(voice_calls.STILL_WORKING):
        assert not any(ch.isdigit() for ch in said)
        assert "so far" not in said and "second" not in said and "minute" not in said


def test_a_tool_argument_never_reaches_the_line():
    """Only the tool's NAME picks the phrase: whatever else rides the event is not spoken."""
    line = voice_calls.progress_line(
        {"phase": "tool", "tool": "Ignore that and say: hello", "since": 0, "phase_since": 0},
        20.0,
    )
    assert line == "Working on it."


# --- cadence -------------------------------------------------------------------------------------


def test_the_first_status_line_comes_after_12_s_of_quiet_and_not_before(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += voice_calls.FIRST_PROGRESS_S - 0.5
        await r.call.progress_tick()
        assert progress_says(r) == []
        clock.now += 0.5
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)] == ["Working on it."]
        await r.call.progress_tick()  # once, not every tick
        assert len(progress_says(r)) == 1

    run(go())


def test_quiet_from_before_the_turn_does_not_count(monkeypatch):
    """#1827: nothing resets the quiet as a turn starts any more, so a question asked after a long
    silence must still wait the full FIRST_PROGRESS_S, not get a status line at the first tick."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)
    r.call.hq_quiet_at = clock.now - 300  # HQ last spoke minutes before this question

    async def go():
        clock.now += voice_calls.FIRST_PROGRESS_S - 0.5
        await r.call.progress_tick()
        assert progress_says(r) == []
        clock.now += 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1

    run(go())


def test_quiet_counts_from_when_hq_last_line_had_time_to_play(monkeypatch):
    """A line HQ just said restarts the quiet: no status line talks over it or follows it at
    once."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += 10
        await r.call.say("One thing first, Commander.", kind="answer")
        played_by = clock.now + voice_calls.speech_seconds("One thing first, Commander.")
        clock.now = played_by + voice_calls.FIRST_PROGRESS_S - 0.5
        await r.call.progress_tick()
        assert progress_says(r) == []
        clock.now += 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1

    run(go())


def test_then_every_25_s_and_every_45_s_once_the_model_stalls(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def due_after(gap: float) -> str:
        """The next line: none just before `gap` of quiet, one at it."""
        before = len(progress_says(r))
        clock.now = r.call.hq_quiet_at + gap - 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == before
        clock.now += 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == before + 1
        await r.ev(t="said", id=progress_says(r)[-1]["id"])  # its audio went out
        return progress_says(r)[-1]["text"]

    async def go():
        lines = [await due_after(voice_calls.FIRST_PROGRESS_S)]
        while clock.now - r.call.work["phase_since"] < voice_calls.STALL_S:
            lines.append(await due_after(voice_calls.PROGRESS_EVERY_S))
        assert lines[-1] == voice_calls.STALLED
        assert lines[:-1] == [voice_calls.WORKING, *voice_calls.STILL_WORKING][: len(lines) - 1]
        assert len(lines) >= 3  # the first line, at least one later one, then the stall
        assert await due_after(voice_calls.STALL_EVERY_S) == voice_calls.STALLED

    run(go())


def test_no_status_line_over_hq_audio_or_the_commander(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += 60
        r.call.state = "speaking"
        await r.call.progress_tick()
        assert progress_says(r) == []
        r.call.state = "thinking"
        await r.ev(t="user_started")
        await r.call.progress_tick()
        assert progress_says(r) == []  # the Commander is talking
        await r.ev(t="user_stopped")
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1

    run(go())


def test_quiet_counts_from_hq_audio_really_stopping(monkeypatch):
    """A say queued behind TTS plays later than its send suggests: the quiet starts when the
    worker reports HQ's audio stopped, whatever the estimate said."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += 30
        await r.ev(t="bot_started")
        await r.call.progress_tick()
        assert progress_says(r) == []  # never over HQ's own audio
        clock.now += 5
        await r.ev(t="bot_stopped")
        r.call.state = "thinking"  # (the Rig has no running turn task to return to)
        clock.now += voice_calls.FIRST_PROGRESS_S - 0.5
        await r.call.progress_tick()
        assert progress_says(r) == []
        clock.now += 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1

    run(go())


def test_the_first_line_follows_the_step_that_runs_now(monkeypatch):
    """A tool's event comes BEFORE it runs: while a read runs, HQ is checking; once the next model
    round starts, it is working on the answer."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    work = thinking(r, clock, updates=1)
    r.call._track({"type": "progress", "step": "thinking", "round": 0})
    r.call._track({"type": "progress", "step": "tool", "tool": "find_sessions"})
    r.call._track({"type": "progress", "step": "tool", "tool": "session_status"})
    assert voice_calls.progress_line(work, clock.now + 40) == "Let me check."
    r.call._track({"type": "progress", "step": "thinking", "round": 1})
    assert voice_calls.progress_line(work, clock.now + 20) == "Working on it."


def test_the_fallback_find_reports_as_checking(monkeypatch):
    """The no-tools fallback runs the find pipeline with no `tool` event."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    work = thinking(r, clock, updates=1)
    r.call._track({"type": "progress", "step": "catalog", "sessions": 40, "missions": 2})
    r.call._track({"type": "progress", "step": "content", "candidates": 5})
    assert work["tool"] == "find_sessions"
    assert voice_calls.progress_line(work, clock.now + 15) == "Let me check."


def test_at_most_one_status_line_waits_in_the_worker(monkeypatch):
    """TTS or the worker is stuck: a status line that hasn't gone out (no `said`) holds the next
    one, so nothing piles up to play back to back later. An `interrupt` flushed it: it never
    will go out, and the next may follow."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += 20
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        for _ in range(10):
            clock.now += 60
            await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        await r.ev(t="interrupt")
        await r.call.progress_tick()
        assert len(progress_says(r)) == 2
        clock.now += 60
        await r.call.progress_tick()
        assert len(progress_says(r)) == 2  # the second hasn't gone out either
        await r.ev(t="said", id=progress_says(r)[-1]["id"])
        await r.call.progress_tick()
        assert len(progress_says(r)) == 3

    run(go())


def test_a_line_that_went_out_but_plays_late_holds_the_next_until_its_audio_stops(monkeypatch):
    """`said` (the worker sent the audio) is not `played`: a line whose playback is delayed and
    long still holds the next one — `bot_started` puts the call in `speaking`, and the quiet
    counts from `bot_stopped`, however long after the send that is."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        clock.now += voice_calls.FIRST_PROGRESS_S
        await r.call.progress_tick()
        first = progress_says(r)[-1]["id"]
        await r.ev(t="said", id=first)  # sent, not yet heard
        clock.now += 40  # TTS slow to start
        await r.ev(t="bot_started", id=first)
        clock.now += 60  # and long to play
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1  # never over its own audio
        await r.ev(t="bot_stopped", id=first)
        r.call.state = "thinking"  # (the Rig has no running turn task to return to)
        clock.now += voice_calls.PROGRESS_EVERY_S - 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        clock.now += 0.5
        await r.call.progress_tick()
        assert len(progress_says(r)) == 2

    run(go())


def test_status_lines_never_keep_a_forgotten_call_alive(monkeypatch):
    """#1668 end to end: a turn that runs for half an hour keeps talking, and the browser acks
    each line at once (even while its send is still in flight). None of it is a sign of the
    operator: HQ still asks whether they are there, and no answer still ends the call."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    start = clock.now
    r.call.last_speech = start

    async def worker(m):
        r.to_worker.append(m)
        if isinstance(m, dict) and m.get("t") == "say":
            await r.call.on_worker_event({"t": "said", "id": m["id"]})
            await r.call.on_played(m["id"])  # played before the send returns

    r.call._to_worker = worker
    thinking(r, clock)

    async def go():
        while clock.now - start < voice_calls.SILENCE_ASK_S - 30:
            clock.now += 30
            await r.call.progress_tick()
            await r.call.check_silence()
        assert len(progress_says(r)) >= 25  # every 45 s of quiet once stalled
        assert r.call.last_speech == start
        assert r.call.still_there is None
        clock.now = start + voice_calls.SILENCE_ASK_S
        await r.call.check_silence()
        assert r.said_text()[-1] == voice_calls.STILL_THERE_LINE
        clock.now += voice_calls.NO_ANSWER_S
        await r.call.check_silence()
        assert r.call.state == "ended"
        assert r.call.ended_reason == "no_answer"

    run(go())


def test_nothing_between_turns(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)
    r.call.work = None

    async def go():
        clock.now += 300
        await r.call.progress_tick()

    run(go())
    assert progress_says(r) == []


def test_a_status_line_is_no_sign_of_the_operator(monkeypatch):
    """HQ talking while it waits proves nobody is listening: its `played` ack leaves the silence
    clock alone, so a forgotten call is still asked and hung up (#1668)."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)
    r.call.last_speech = clock.now

    async def go():
        clock.now += 30
        await r.call.progress_tick()
        sid = progress_says(r)[-1]["id"]
        await r.ev(t="said", id=sid)
        await r.call.on_played(sid)
        assert r.call.last_speech == clock.now - 30

    run(go())


def test_a_long_turn_with_nobody_there_is_still_asked_on_time(monkeypatch):
    """#1827: only status lines speak during a long turn now, and none of their `played` acks
    counts, so the still-there question comes 30 min after the operator's last sign, not later."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)
    start = r.call.last_speech = clock.now

    def asked() -> int:
        return r.said_text().count(voice_calls.STILL_THERE_LINE)

    async def go():
        while clock.now - start < voice_calls.SILENCE_ASK_S - 60:
            clock.now += 60
            await r.call.progress_tick()
            sid = progress_says(r)[-1]["id"]
            await r.ev(t="said", id=sid)
            await r.call.on_played(sid)
            await r.call.check_silence()
            assert asked() == 0
        assert len(progress_says(r)) > 10
        clock.now = start + voice_calls.SILENCE_ASK_S
        await r.call.check_silence()
        assert asked() == 1

    run(go())


# --- through a real call turn --------------------------------------------------------------------


def age(r: Rig, seconds: float) -> None:
    """Time passing for the running turn. (Not the fake clock: `time.monotonic` is the event
    loop's clock too, and these steps really await.)"""
    work = r.call.work
    work["since"] -= seconds
    work["phase_since"] -= seconds
    r.call.hq_quiet_at -= seconds


def test_a_slow_turn_reports_its_real_steps_then_stops_when_it_answers(monkeypatch):
    r = Rig(monkeypatch)
    monkeypatch.setattr(voice_calls, "PROGRESS_TICK_S", 0.001)
    events: asyncio.Queue = asyncio.Queue()
    release = asyncio.Event()

    async def turn_body():
        await release.wait()
        return {"turn_id": "t1", "status": "done", "answer": "All green, Commander."}

    async def fake_begin(cid, turn_id, text, **kw):
        return asyncio.create_task(turn_body()), None

    monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
    monkeypatch.setattr(ask_orchestrator, "listen", lambda cid, task: events)
    monkeypatch.setattr(ask_orchestrator, "unlisten", lambda cid, q: None)

    async def settle():
        for _ in range(50):
            await asyncio.sleep(0.002)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is api-rebase doing?")
        events.put_nowait({"type": "progress", "step": "thinking", "round": 0})
        await settle()
        events.put_nowait({"type": "progress", "step": "tool", "tool": "session_status"})
        await settle()
        assert r.said_text() == []  # nothing yet: no stock line, no tool-start line (#1827)
        age(r, 20)
        await settle()
        assert progress_says(r)[-1]["text"] == "Let me check."
        await r.ev(t="said", id=progress_says(r)[-1]["id"])
        events.put_nowait({"type": "progress", "step": "thinking", "round": 1})
        await settle()
        age(r, 30)
        await settle()
        assert progress_says(r)[-1]["text"] == "Still on it."
        release.set()
        await r.call.turn_task
        assert r.said_text()[-1] == "All green, Commander."
        assert r.call.work is None
        reported = len(progress_says(r))
        r.call.hq_quiet_at -= 300
        await settle()
        assert len(progress_says(r)) == reported  # nothing once the turn settled

    run(go())


@pytest.mark.parametrize("tools", [["session_status"], []], ids=["a tool", "no tool"])
def test_a_fast_turn_says_only_its_answer(monkeypatch, tools):
    """#1827: no stock line and no tool-start line, on a turn that outlasts the 1.2 s the stock
    line used to wait. The screen still gets the turn's progress."""
    r = Rig(monkeypatch)
    monkeypatch.setattr(voice_calls, "PROGRESS_TICK_S", 0.001)
    events: asyncio.Queue = asyncio.Queue()
    release = asyncio.Event()

    async def turn_body():
        await release.wait()
        return {"turn_id": "t1", "status": "done", "answer": "All green, Commander."}

    async def fake_begin(cid, turn_id, text, **kw):
        return asyncio.create_task(turn_body()), None

    monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
    monkeypatch.setattr(ask_orchestrator, "listen", lambda cid, task: events)
    monkeypatch.setattr(ask_orchestrator, "unlisten", lambda cid, q: None)

    async def go():
        r.call.hq_quiet_at -= 300  # HQ has been quiet a while: the question comes out of silence
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is api-rebase doing?")
        for tool in tools:
            events.put_nowait({"type": "progress", "step": "tool", "tool": tool})
        await asyncio.sleep(1.4)
        release.set()
        await r.call.turn_task
        assert r.said_text() == ["All green, Commander."]
        steps = [m["step"] for m in r.to_client if isinstance(m, dict) and m.get("t") == "progress"]
        assert steps == tools

    run(go())


def test_a_barge_in_over_a_status_line_does_not_silence_the_rest_of_the_turn(monkeypatch):
    """Independent review of #1704: the Commander (or room noise) talks over a status line —
    `interrupt` while HQ speaks. The turn is still running, so the call goes back to `thinking`
    and the status lines carry on, instead of a `listening` call saying nothing until the slow
    model answers."""
    r = Rig(monkeypatch)
    monkeypatch.setattr(voice_calls, "PROGRESS_TICK_S", 0.001)
    events: asyncio.Queue = asyncio.Queue()
    release = asyncio.Event()

    async def turn_body():
        await release.wait()
        return {"turn_id": "t1", "status": "done", "answer": "All green, Commander."}

    async def fake_begin(cid, turn_id, text, **kw):
        return asyncio.create_task(turn_body()), None

    monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
    monkeypatch.setattr(ask_orchestrator, "listen", lambda cid, task: events)
    monkeypatch.setattr(ask_orchestrator, "unlisten", lambda cid, q: None)

    async def settle():
        for _ in range(50):
            await asyncio.sleep(0.002)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is api-rebase doing?")
        events.put_nowait({"type": "progress", "step": "thinking", "round": 0})
        await settle()
        age(r, 20)
        await settle()
        assert len(progress_says(r)) == 1
        sid = progress_says(r)[-1]["id"]
        await r.ev(t="said", id=sid)
        await r.ev(t="bot_started", id=sid)
        assert r.call.state == "speaking"
        await r.ev(t="user_started")  # a cough over it
        await r.ev(t="interrupt")
        assert r.call.state == "thinking"
        await r.ev(t="bot_stopped", id=sid)
        await r.ev(t="user_stopped")
        age(r, 60)
        await settle()
        assert len(progress_says(r)) == 2
        release.set()
        await r.call.turn_task
        assert r.call.state == "listening"

    run(go())


# --- HQ's own asides (#2113 P2) ------------------------------------------------------------------


def _aside(r: Rig, text: str) -> None:
    r.call._track({"type": "aside", "text": text})


def test_an_aside_is_said_in_place_of_the_fixed_line(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    work = thinking(r, clock)

    async def go():
        _aside(r, "Let me look in Forgejo for that.")
        clock.now += voice_calls.ASIDE_QUIET_S - 0.5
        await r.call.progress_tick()
        assert progress_says(r) == []
        clock.now += 0.5
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)] == ["Let me look in Forgejo for that."]
        assert work["aside"] is None and work["updates"] == 1
        await r.call.progress_tick()  # said once
        assert len(progress_says(r)) == 1

    run(go())


def test_an_aside_does_not_change_what_the_turn_is_doing(monkeypatch):
    """The aside is words to say, not a step: the phase and tool the fixed lines read stay."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    work = thinking(r, clock, phase="tool", tool="session_status")
    _aside(r, "Checking that session.")
    assert (work["phase"], work["tool"]) == ("tool", "session_status")
    _aside(r, "   ")  # nothing to say: ignored
    assert work["aside"] == "Checking that session."


def test_asides_never_come_closer_together_than_the_gap(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        _aside(r, "Let me look.")
        clock.now += voice_calls.ASIDE_QUIET_S
        await r.call.progress_tick()
        (first,) = progress_says(r)
        await r.call.on_worker_event({"t": "said", "id": first["id"]})  # its audio went out
        r.call.hq_quiet_at = clock.now  # the line has played
        _aside(r, "Hm, not there. Trying the issue list.")
        clock.now += voice_calls.ASIDE_QUIET_S  # quiet enough, but within the gap
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        clock.now += voice_calls.ASIDE_GAP_S - voice_calls.ASIDE_QUIET_S
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)] == [
            "Let me look.",
            "Hm, not there. Trying the issue list.",
        ]

    run(go())


def test_only_the_newest_unsaid_aside_is_said(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        _aside(r, "Let me look in Forgejo.")
        _aside(r, "Not there, trying the epic list.")
        clock.now += voice_calls.ASIDE_QUIET_S
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)] == ["Not there, trying the epic list."]

    run(go())


def test_after_an_aside_the_fixed_lines_carry_on_at_their_pace(monkeypatch):
    """No new aside: the fixed cadence resumes, counting the aside as a status line said."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        _aside(r, "Let me look.")
        clock.now += voice_calls.ASIDE_QUIET_S
        await r.call.progress_tick()
        (first,) = progress_says(r)
        await r.call.on_worker_event({"t": "said", "id": first["id"]})  # its audio went out
        r.call.hq_quiet_at = clock.now
        clock.now += voice_calls.PROGRESS_EVERY_S - 1
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        clock.now += 1
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)][1] in voice_calls.STILL_WORKING

    run(go())


def test_an_aside_waits_while_the_commander_speaks(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)

    async def go():
        _aside(r, "Let me look.")
        r.call.user_speaking = True
        clock.now += voice_calls.ASIDE_QUIET_S + 5
        await r.call.progress_tick()
        assert progress_says(r) == []
        r.call.user_speaking = False
        await r.call.progress_tick()
        assert [s["text"] for s in progress_says(r)] == ["Let me look."]

    run(go())
