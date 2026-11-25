"""A reply is voiced in whole sentences on a TTS server that streams.

Each request to the speech server is voiced on its own. Measured on VoxCPM2 (2026-11-13, 432
renders, six voices): a piece of one to five words left its voice's pitch in 12 of 72 renders, a
sentence of six words or more in none — cut pieces are what made HQ sound calm, then excited. A
server that streams starts speaking a fraction of a second into any request, so the worker cuts
nothing there; on one that sends a sentence only when it is finished, the short first piece
stays, because it is the earliest audio there is.
"""

import asyncio

import pytest

from agent_sessions import voice_text
from agent_sessions.voice_synth import Pace, paced, speech_pieces

REPLY = (
    "Understood, Commander. Twenty-five sessions are running, and three of them are waiting "
    "on you. The card is on your screen."
)


def test_a_short_reply_is_one_request_on_a_streaming_server():
    assert voice_text.split_speech(REPLY, whole=True) == [REPLY]


def test_the_same_reply_is_cut_for_a_server_that_does_not_stream():
    parts = voice_text.split_speech(REPLY, first_max=7)
    assert parts[0] == "Understood, Commander."
    assert len(parts) > 2 and " ".join(parts) == REPLY


def test_whole_never_cuts_the_first_piece_short_even_when_asked():
    one = "Twenty-five sessions are running, and three of them are waiting on you."
    assert voice_text.split_speech(one, first_max=7, whole=True) == [one]


def test_a_long_reply_goes_out_in_whole_sentences_up_to_the_limit():
    sentences = [f"Sentence number {i} has exactly eight words here." for i in range(12)]
    text = " ".join(sentences)
    parts = voice_text.split_speech(text, whole=True)
    assert " ".join(parts) == text
    assert len(parts) == 3  # 96 words: five sentences (40 words) a request, two left over
    for part in parts:
        assert len(part.split()) <= voice_text.WHOLE_WORDS
        assert part.endswith(".") and part.startswith("Sentence number")


def test_a_short_last_piece_joins_the_one_before_it():
    body = " ".join(f"w{i}" for i in range(38)) + "."
    text = f"{body} Go ahead, Commander."
    parts = voice_text.split_speech(text, whole=True)
    # 38 + 3 words is over the limit, but three words alone would be a fragment.
    assert parts == [text]


def test_a_request_is_never_longer_than_the_limit_plus_a_short_tail():
    most = voice_text.WHOLE_WORDS + voice_text.WHOLE_MIN_WORDS - 1
    for tail in range(1, 12):
        text = " ".join(f"w{i}" for i in range(40)) + ". " + " ".join(["x"] * tail) + "."
        parts = voice_text.split_speech(text, whole=True)
        assert " ".join(parts) == text
        assert all(len(p.split()) <= most for p in parts)
        assert len(parts) == (1 if tail < voice_text.WHOLE_MIN_WORDS else 2)


def test_a_sentence_longer_than_the_limit_is_cut_at_its_clauses():
    clause = "the session finished its rebase and the tests are running again"  # 11 words
    text = ", ".join([clause] * 6) + "."
    parts = voice_text.split_speech(text, whole=True)
    assert " ".join(parts) == text and len(parts) == 2
    assert all(len(p.split()) <= voice_text.WHOLE_WORDS for p in parts)
    assert parts[0].endswith(",")


def test_an_unpunctuated_run_is_still_bounded():
    text = " ".join(f"w{i}" for i in range(100))
    parts = voice_text.split_speech(text, whole=True)
    assert " ".join(parts) == text
    assert all(len(p.split()) <= voice_text.WHOLE_WORDS for p in parts)


def test_whole_keeps_every_word_of_an_ordinary_answer():
    text = (
        "It finished the rebase on main twelve minutes ago, and two auth tests fail; I have "
        "drafted a nudge to rerun them and fix the failures, it is on your screen. Done?"
    )
    assert " ".join(voice_text.split_speech(text, whole=True)).split() == text.split()


def test_nothing_to_say_is_no_request():
    assert voice_text.split_speech("   ", whole=True) == []


# --- Pace: does the server stream? ------------------------------------------------------------


def test_nothing_is_known_before_a_sentence_was_spoken():
    assert Pace().streams is None


def test_first_audio_early_in_the_sentence_is_a_streaming_server():
    pace = Pace()
    pace.note(first_s=0.22, total_s=3.0, audio_s=4.6)  # VoxCPM2 on pm-core, 2026-11-13
    assert pace.streams is True


def test_first_audio_only_at_the_end_is_a_server_that_does_not_stream():
    pace = Pace()
    pace.note(first_s=2.9, total_s=3.0, audio_s=4.6)
    assert pace.streams is False


def test_a_very_short_sentence_says_nothing():
    pace = Pace()
    pace.note(first_s=0.4, total_s=0.45, audio_s=0.6)  # "Yes."
    assert pace.streams is None
    pace.note(first_s=0.2, total_s=3.0, audio_s=4.0)
    pace.note(first_s=0.4, total_s=0.45, audio_s=0.6)
    assert pace.streams is True  # still what the last telling sentence showed


def test_the_newest_telling_sentence_decides():
    pace = Pace()
    pace.note(first_s=2.6, total_s=2.7, audio_s=2.7)  # a cold first request
    assert pace.streams is False
    pace.note(first_s=0.2, total_s=2.0, audio_s=3.0)
    assert pace.streams is True


def test_a_measurement_that_makes_no_sense_is_ignored():
    pace = Pace()
    pace.note(first_s=-1.0, total_s=3.0, audio_s=4.0)
    pace.note(first_s=0.2, total_s=0.0, audio_s=4.0)
    assert pace.streams is None


# --- what a say is sent as, by what the call has learnt -----------------------------------------


def test_before_anything_was_measured_the_first_piece_is_cut():
    """The call's first words (its greeting): nothing is known about the server yet. The one
    place a reply is still cut on a streaming server."""
    parts = speech_pieces(REPLY, Pace(), 7)
    assert parts[0] == "Understood, Commander." and len(parts) > 1


def test_on_a_server_that_does_not_stream_the_first_piece_stays_cut():
    pace = Pace()
    pace.note(first_s=2.9, total_s=3.0, audio_s=4.0)
    assert speech_pieces(REPLY, pace, 7) == voice_text.split_speech(REPLY, first_max=7)


def test_on_a_server_that_streams_the_reply_goes_out_whole():
    pace = Pace()
    pace.note(first_s=0.2, total_s=3.0, audio_s=4.0)
    assert speech_pieces(REPLY, pace, 7) == [REPLY]


def test_a_greeting_too_short_to_measure_leaves_the_next_reply_cut_until_one_is_measured():
    """The exception to "one voice" can outlive the greeting: a short sentence says nothing, so
    the reply after it is still cut; the first reply of a second or more settles it."""
    pace = Pace()
    pace.note(first_s=0.3, total_s=0.5, audio_s=0.8)  # "Go ahead, Commander."
    assert pace.streams is None
    assert len(speech_pieces(REPLY, pace, 7)) > 1  # still cut
    pace.note(first_s=0.25, total_s=3.1, audio_s=4.6)  # that reply's long piece came back whole
    assert speech_pieces(REPLY, pace, 7) == [REPLY]


# --- paced: what a synthesis shows, noted only when it came back whole --------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _second(rate=24000):
    return b"\x00" * (2 * rate)


def test_a_sentence_that_arrives_while_it_is_generated_is_noted_as_streamed():
    clock, pace = _Clock(), Pace()

    async def chunks():
        for _ in range(3):
            clock.now += 0.3
            yield _second()

    async def go():
        return [c async for c in paced(chunks(), pace, 24000, clock)]

    out = asyncio.run(go())
    assert out == [_second()] * 3  # passed on unchanged
    assert pace.streams is True  # first audio at 0.3 s of 0.9 s


def test_a_sentence_that_arrives_all_at_the_end_is_noted_as_not_streamed():
    clock, pace = _Clock(), Pace()

    async def chunks():
        clock.now += 2.5
        yield _second()
        yield _second()
        clock.now += 0.05

    async def go():
        return [c async for c in paced(chunks(), pace, 24000, clock)]

    asyncio.run(go())
    assert pace.streams is False


def test_a_failed_synthesis_notes_nothing():
    clock, pace = _Clock(), Pace()

    async def chunks():
        clock.now += 0.2
        yield _second()
        raise RuntimeError("the endpoint went away")

    async def go():
        return [c async for c in paced(chunks(), pace, 24000, clock)]

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert pace.streams is None


def test_a_synthesis_cut_off_by_a_barge_in_notes_nothing():
    clock, pace = _Clock(), Pace()

    async def chunks():
        while True:
            clock.now += 0.2
            yield _second()

    async def go():
        stream = paced(chunks(), pace, 24000, clock)
        await stream.__anext__()
        await stream.__anext__()
        await stream.aclose()  # the worker's `aclosing` on an interruption

    asyncio.run(go())
    assert pace.streams is None


def test_a_synthesis_without_audio_notes_nothing():
    pace = Pace()

    async def chunks():
        return
        yield b""

    async def go():
        return [c async for c in paced(chunks(), pace, 24000, _Clock())]

    assert asyncio.run(go()) == [] and pace.streams is None
