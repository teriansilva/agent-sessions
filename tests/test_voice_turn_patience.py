"""#1842: HQ lets the Commander finish. A pause long enough to end a turn is often a breath before
the rest, so the turn-taking setting gives a pause room first, and words that arrive while the
Commander is already speaking again wait for the rest: one question, asked once. Nothing HQ is
already working on is asked again."""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import ask_orchestrator, voice_calls
from test_voice_confirm import Rig, run


def _recording(monkeypatch, hold_first: bool = False):
    """`begin` that records each question; with `hold_first`, the first runs until `gate` is set."""
    gate = asyncio.Event()
    seen: list[dict] = []

    async def begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        if hold_first and len(seen) == 1:
            await gate.wait()
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done, Commander."}

    monkeypatch.setattr(ask_orchestrator, "begin", begin)
    return gate, seen


async def _settle(r: Rig) -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
        held = getattr(r.call, "held", None)
        if held is None and (r.call.turn_task is None or r.call.turn_task.done()):
            return


def test_words_while_the_commander_is_speaking_again_wait_for_the_rest(monkeypatch):
    """The first part's transcript lands after the Commander already resumed (it trails the
    pause by the VAD's wait and the STT): it is held, joined, and asked once."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")  # turn 1
        await r.ev(t="user_started")  # turn 2: speaking again before turn 1's words landed
        await r.ev(t="utterance", text="I wanted to ask you about the release", turn=1)
        await asyncio.sleep(0.05)
        assert seen == []  # held, not asked
        await r.ev(t="utterance", text="and whether the second session has finished.", turn=2)
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == [
        "I wanted to ask you about the release and whether the second session has finished."
    ]


def test_every_part_spoken_in_one_go_becomes_one_question(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="and the missions", turn=2)
        await r.ev(t="utterance", text="and tell me what is still open.", turn=3)
        await r.ev(t="turn_end", turn=3)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == [
        "Check the fleet and the missions and tell me what is still open."
    ]


def test_words_with_nobody_speaking_again_are_asked_at_once(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is the fleet?")
        await asyncio.sleep(0)
        assert [s["text"] for s in seen] == ["How is the fleet?"]
        await _settle(r)

    run(go())


def test_a_held_part_is_asked_alone_when_the_newer_turn_had_no_words(monkeypatch):
    """The newer turn was a breath or noise: the worker says so (`turn_end` with no utterance),
    and the first part goes out then — not before, and on no timer."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")  # a cough
        await r.ev(t="utterance", text="How is the fleet?", turn=1)
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        await asyncio.sleep(0.05)
        assert seen == []  # the cough's turn has not ended yet: its words may still come
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["How is the fleet?"]


def test_a_held_part_waits_for_every_turn_begun_since(monkeypatch):
    """Two breaths after the first part: it goes out only once BOTH have ended."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is the fleet?", turn=1)
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        await r.ev(t="user_started")
        await r.ev(t="turn_end", turn=2)
        await asyncio.sleep(0.02)
        assert seen == []  # turn 3 is still open
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="and the missions?", turn=3)
        await r.ev(t="turn_end", turn=3)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["How is the fleet? and the missions?"]


def test_a_rest_still_being_handled_is_never_asked_apart_from_its_first_part(monkeypatch):
    """Hermes 7631: the rest had arrived but was still being handled (its caption send stalled)
    when its turn's end could already have released the first part. It must wait for the rest."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    stall = _stalling(r, lambda m: m.get("t") == "caption" and m.get("text") == "and the missions")

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)  # held
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        rest = asyncio.create_task(r.ev(t="utterance", text="and the missions", turn=2))
        await asyncio.sleep(0.05)
        await r.ev(t="turn_end", turn=2)  # even a marker handled meanwhile releases nothing
        assert seen == []
        stall.set()
        await rest
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


async def _reader(r: Rig, items: list) -> None:
    """The relay's worker reader (`routes/voice.py`): one message at a time, each awaited whole —
    an event, or HQ's audio forwarded to the browser — so one stuck in a client send keeps the
    ones behind it unread. ``{"pause": s}``: the socket's next read takes this long."""
    for e in items:
        if isinstance(e, bytes):
            await r.call.client(e)
        elif "pause" in e:
            await asyncio.sleep(e["pause"])
        else:
            await r.ev(**e)


def _stalling(r: Rig, stuck) -> asyncio.Event:
    """The client send for the first message `stuck(msg)` matches waits until the event is set."""
    stall = asyncio.Event()
    send = r.call.client

    async def slow_client(msg):
        if stuck(msg) and not stall.is_set():
            await stall.wait()
        await send(msg)

    r.call.client = slow_client
    return stall


FRAME = b"\x00\x00" * 160


@pytest.mark.parametrize(
    "stuck",
    [
        pytest.param(lambda m: m == {"t": "user", "speaking": False}, id="the-stop-send"),
        pytest.param(lambda m: isinstance(m, bytes), id="an-audio-frame"),
    ],
)
def test_a_rest_unread_behind_a_stalled_send_is_never_asked_apart(monkeypatch, stuck):
    """Hermes 7633, 7636: a send on the worker reader stalls, so the rest's transcript waits
    UNREAD behind it. Nothing the app can see says the rest is coming except that its turn has
    not ended: the first part waits for exactly that, however long the stall."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    stall = _stalling(r, stuck)

    async def go():
        reader = asyncio.create_task(
            _reader(
                r,
                [
                    {"t": "user_started"},
                    {"t": "user_started"},
                    {"t": "utterance", "text": "Check the fleet", "turn": 1},  # held
                    {"t": "turn_end", "turn": 1},
                    {"t": "user_stopped"},
                    FRAME,
                    {"t": "utterance", "text": "and the missions", "turn": 2},
                    {"t": "turn_end", "turn": 2},
                ],
            )
        )
        await asyncio.sleep(0.2)
        assert seen == []
        stall.set()
        await reader
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


def test_a_rest_behind_a_stream_of_short_frames_is_never_asked_apart(monkeypatch):
    """Hermes 7637: many short forwards keep the reader busy, each too short to look stalled, with
    gaps between them. The rest, behind them, still joins its first part."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    frames: list = []
    for _ in range(20):
        frames += [FRAME, {"pause": 0.01}]

    async def go():
        await _reader(
            r,
            [
                {"t": "user_started"},
                {"t": "user_started"},
                {"t": "utterance", "text": "Check the fleet", "turn": 1},  # held
                {"t": "turn_end", "turn": 1},
                {"t": "user_stopped"},
                *frames,
                {"t": "utterance", "text": "and the missions", "turn": 2},
                {"t": "turn_end", "turn": 2},
            ],
        )
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


def test_nothing_hq_is_working_on_is_asked_again(monkeypatch):
    """Hermes on #1843: a request already sent is never replayed inside a longer one. Words
    after it started are the next question, as before."""
    r = Rig(monkeypatch)
    gate, seen = _recording(monkeypatch, hold_first=True)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Start a session for the auth refactor")
        await asyncio.sleep(0)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="in the backend project.")
        gate.set()
        await _settle(r)
        while r.call.turn_task is not None and not r.call.turn_task.done():
            await r.call.turn_task

    run(go())
    assert [s["text"] for s in seen] == [
        "Start a session for the auth refactor",
        "in the backend project.",
    ]


def test_an_answer_to_hqs_question_is_never_held(monkeypatch):
    """An answer stays an answer: it is decided at once, and what follows is a new request."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    question = {"question_id": "q1", "question": "Which one?", "options": ["Yes", "No"],
                "allow_text": True}  # fmt: skip

    async def go():
        r.call.ask, r.call.ask_since = question, r.call.user_starts
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Yes", turn=1)
        await asyncio.sleep(0)
        assert seen and seen[0]["option"] == 0
        await _settle(r)

    run(go())


def test_a_result_after_the_grace_joins_before_its_turn_ends(monkeypatch):
    """Hermes 7639: turn 2's words came back from STT after the worker's grace, as a LATE
    utterance — and its `turn_end` only after them. The held first part waits for that marker,
    so the late words join it: one question."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)  # held
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="and the missions", late=True)  # turn 2's, after the grace
        await asyncio.sleep(0.02)
        assert seen == []  # turn 2 has not ended: more of it may come
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


def test_a_rest_whose_turn_has_a_late_part_too_is_still_one_question(monkeypatch):
    """The rest's turn went out at its grace with only part of its words; the remainder comes back
    late. All of it joins the first part, asked once its turn has ended."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)  # held
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="and the missions", turn=2)
        await r.ev(t="utterance", text="that are still open.", late=True)
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions that are still open."]


def test_a_late_first_part_is_held_for_the_rest(monkeypatch):
    """Hermes 7641: turn 1's words came back from STT after its grace (LATE, with its own turn),
    while turn 2 had already begun. They wait for turn 2's rest like on-time words: one question."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        await r.ev(t="user_started")  # turn 2: speaking again
        await r.ev(t="utterance", text="Check the fleet", late=True, turn=1)
        await r.ev(t="turn_end", turn=1)
        await asyncio.sleep(0.02)
        assert seen == [] and r.call.user_speaking  # still mid-sentence in turn 2
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="and the missions", turn=2)
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


def test_a_late_answer_to_hqs_question_is_never_joined_to_the_next_request(monkeypatch):
    """Hermes 7642: an answer to HQ's open question came back from STT late while the Commander
    had begun a new request. It is not held as that request's first part: two, never merged."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    question = {"question_id": "q1", "question": "Which repository?", "allow_text": True}

    async def go():
        r.call.ask, r.call.ask_since = question, r.call.user_starts  # presented before turn 1
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        await r.ev(t="user_started")  # turn 2: a new request
        await r.ev(t="utterance", text="The first repository", late=True, turn=1)
        await r.ev(t="turn_end", turn=1)
        await asyncio.sleep(0)
        assert [s["text"] for s in seen] == ["The first repository"]  # not held
        await _settle(r)
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="What is the status?", turn=2)
        await r.ev(t="turn_end", turn=2)
        await _settle(r)

    run(go())
    assert [s["text"] for s in seen] == ["The first repository", "What is the status?"]
    # Bound to the question it answers (Hermes 7643); what follows is a new request.
    assert seen[0]["answers"] == "q1" and seen[0]["option"] is None
    assert seen[1]["answers"] is None and seen[1]["option"] is None


def test_a_late_reply_to_the_still_there_question_is_never_held(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        r.call.replying_still_there, r.call.still_there_since = True, r.call.user_starts
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Yes, still here", late=True, turn=1)
        await asyncio.sleep(0)
        assert [s["text"] for s in seen] == ["Yes, still here"]
        await _settle(r)

    run(go())
    assert seen[0]["replying_to"] == voice_calls.STILL_THERE_LINE  # Hermes 7643


def test_a_late_answer_picking_an_option_picks_it(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    question = {"question_id": "q1", "question": "Archive them?", "options": ["Yes", "No"],
                "allow_text": False}  # fmt: skip

    async def go():
        r.call.ask, r.call.ask_since = question, r.call.user_starts
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="No", late=True, turn=1)
        await r.ev(t="turn_end", turn=1)
        await _settle(r)

    run(go())
    assert len(seen) == 1 and seen[0]["answers"] == "q1" and seen[0]["option"] == 1


def test_late_words_begun_before_the_question_never_answer_it(monkeypatch):
    """Unchanged rule, late words too: only words BEGUN after HQ showed its question answer it."""
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    question = {"question_id": "q1", "question": "Which repository?", "allow_text": True}

    async def go():
        await r.ev(t="user_started")  # turn 1 begins…
        r.call.ask, r.call.ask_since = question, r.call.user_starts  # …then the question shows
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="Check the fleet", late=True, turn=1)
        await r.ev(t="turn_end", turn=1)
        await _settle(r)

    run(go())
    assert [(s["text"], s["answers"]) for s in seen] == [("Check the fleet", None)]


def test_a_late_transcript_of_an_unknown_turn_is_a_new_request(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)
    question = {"question_id": "q1", "question": "Which repository?", "allow_text": True}

    async def go():
        r.call.ask, r.call.ask_since = question, r.call.user_starts
        await r.ev(t="utterance", text="The first one", late=True, turn=99)  # no start known
        await _settle(r)

    run(go())
    assert [(s["text"], s["answers"]) for s in seen] == [("The first one", None)]


def test_a_late_part_with_nobody_speaking_again_is_asked_at_once(monkeypatch):
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        await r.ev(t="utterance", text="How is the fleet?", late=True, turn=1)
        await asyncio.sleep(0)
        assert [s["text"] for s in seen] == ["How is the fleet?"]
        await _settle(r)

    run(go())


def _worker(voice_worker, gate: asyncio.Event | None = None):
    """A worker bridge whose sends are recorded in the order they COMPLETE; with `gate`, each send
    waits for it (the app's socket backpressured)."""
    bus = voice_worker.Bus()
    sent: list[dict] = []

    async def send(msg):
        if gate is not None:
            await gate.wait()
        sent.append(msg)

    bus.send = send
    return voice_worker.Bridge(bus, tts=None), bus, sent


def test_the_worker_sends_a_slow_turn_whole_never_part_then_late(monkeypatch):
    """Hermes 7639, 7657, at the source: a turn whose STT is slow is sent once ALL of its words are
    back, as one utterance, and its `turn_end` after it. Nothing goes out early, so no part of it
    can arrive late or behind its marker."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    async def go():
        bridge, bus, sent = _worker(voice_worker)
        bus.turns.segment_sent()
        bus.turns.segment_sent()
        flush = asyncio.create_task(bridge._when_transcribed(bus.turns.target(), None, 2))
        await bridge._transcribed("and the", None)
        await asyncio.sleep(3.2)  # past the old 3 s grace: the second segment is still at STT
        assert sent == []  # nothing yet: no part, and no end
        await bridge._transcribed("missions", None)
        await asyncio.wait_for(flush, 1)
        return [(m["t"], m.get("turn"), m.get("late"), m.get("text")) for m in sent]

    assert run(go()) == [
        ("utterance", 2, None, "and the missions"),
        ("turn_end", 2, None, None),
    ]


def test_a_backpressured_worker_still_delivers_one_request(monkeypatch):
    """Hermes 7657, worker to relay: the worker's sends are held back while the turn's second
    segment comes back from STT. What reaches the app, in order, joins the held first part: ONE
    request for HQ."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    gate = asyncio.Event()

    async def worker_side():
        bridge, bus, sent = _worker(voice_worker, gate)
        bus.turns.segment_sent()
        bus.turns.segment_sent()
        flush = asyncio.create_task(bridge._when_transcribed(bus.turns.target(), None, 2))
        await bridge._transcribed("and the", None)
        await asyncio.sleep(3.2)  # past the old 3 s grace, the second segment still at STT
        late = asyncio.create_task(bridge._transcribed("missions", None))
        await asyncio.sleep(0.05)
        gate.set()  # the socket drains
        await asyncio.wait_for(asyncio.gather(flush, late), 1)
        return sent

    worker_out = run(worker_side())
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def relay():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)  # held
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        for m in worker_out:
            await r.ev(**m)
        await _settle(r)

    run(relay())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions"]


def test_a_turn_of_several_slow_segments_is_never_cut_by_the_wait(monkeypatch):
    """Hermes 7659: STT runs one segment at a time, each with its own timeout, so a whole turn
    can take longer than one timeout while every result is on time. The wait restarts on each
    result: the turn still goes out whole, as ONE request."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    monkeypatch.setattr(voice_worker, "TRANSCRIPT_WAIT_S", 0.2)  # one request's budget, scaled

    async def worker_side():
        bridge, bus, sent = _worker(voice_worker)
        for _ in range(3):
            bus.turns.segment_sent()
        flush = asyncio.create_task(bridge._when_transcribed(bus.turns.target(), None, 2))
        for words in ("and the", "missions", "that are open"):
            await asyncio.sleep(0.15)  # each within its own budget; together far past it
            await bridge._transcribed(words, None)
        await asyncio.wait_for(flush, 1)
        return sent

    worker_out = run(worker_side())
    assert [(m["t"], m.get("text")) for m in worker_out] == [
        ("utterance", "and the missions that are open"),
        ("turn_end", None),
    ]
    r = Rig(monkeypatch)
    _, seen = _recording(monkeypatch)

    async def relay():
        await r.ev(t="user_started")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Check the fleet", turn=1)  # held
        await r.ev(t="turn_end", turn=1)
        await r.ev(t="user_stopped")
        for m in worker_out:
            await r.ev(**m)
        await _settle(r)

    run(relay())
    assert [s["text"] for s in seen] == ["Check the fleet and the missions that are open"]


def test_a_stt_that_stopped_answering_still_ends_the_turn(monkeypatch):
    """The wait's only job: a service that returns nothing at all still lets the turn end."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    monkeypatch.setattr(voice_worker, "TRANSCRIPT_WAIT_S", 0.05)

    async def go():
        bridge, bus, sent = _worker(voice_worker)
        bus.turns.segment_sent()
        await asyncio.wait_for(bridge._when_transcribed(bus.turns.target(), None, 1), 1)
        return [(m["t"], m.get("turn")) for m in sent]

    assert run(go()) == [("turn_end", 1)]


def test_the_worker_tags_late_words_with_their_turn(monkeypatch):
    """Hermes 7641, the fallback: a result after `TRANSCRIPT_WAIT_S` (a service that stopped
    transcribing) still goes out, LATE and carrying the turn it was heard in."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    monkeypatch.setattr(voice_worker, "TRANSCRIPT_WAIT_S", 0.02)

    async def go():
        bridge, bus, sent = _worker(voice_worker)
        bus.turns.segment_sent()
        flush = asyncio.create_task(bridge._when_transcribed(bus.turns.target(), None, 1))
        await asyncio.sleep(0.1)
        await bridge._transcribed("Check the fleet", None)
        await asyncio.wait_for(flush, 1)
        return [(m["t"], m.get("turn"), m.get("late")) for m in sent]

    assert run(go()) == [("turn_end", 1, None), ("utterance", 1, True)]


def test_the_worker_ends_every_turn_with_a_marker_words_or_none():
    """The app's hold rests on this (#1842): `turn_end` after each turn's utterance, and alone for
    a turn with no words or while muted."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    async def go():
        bus = voice_worker.Bus()
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        bus.send = send
        bridge = voice_worker.Bridge(bus, tts=None)
        bus.turns.segment_sent()
        await bridge._transcribed("Check the fleet", None)
        await bridge._when_transcribed(bus.turns.target(), None, 1)
        await bridge._when_transcribed(bus.turns.target(), None, 2)  # a breath: no segment
        bridge.muted = True
        bus.turns.segment_sent()
        await bridge._transcribed("not for HQ", None)
        await bridge._when_transcribed(bus.turns.target(), None, 3)
        return [(m["t"], m.get("turn")) for m in sent]

    assert run(go()) == [
        ("utterance", 1),
        ("turn_end", 1),
        ("turn_end", 2),
        ("turn_end", 3),
    ]
