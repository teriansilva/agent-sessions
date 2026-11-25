"""#2339 — the worker's side of HQ's thinking cue, on the real Pipecat classes.

A `cues` message tells the worker which FIXED lines to get ready in a voice; whole sentences it
speaks teach it that voice's pitch; a `say` marked `cue` plays the kept take with no TTS request,
or nothing at all, and is acked (`said`) either way. Any other say interrupts a take in flight.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import speech_endpoints

pytest.importorskip("pipecat")

from pipecat.frames.frames import OutputTransportMessageFrame, TTSSpeakFrame  # noqa: E402

from agent_sessions import voice_worker  # noqa: E402

RATE = 24000


def make_tts(monkeypatch, asked: list | None = None):
    async def synthesize(ep, text, **kw):
        if asked is not None:
            asked.append((text, kw))
        yield b"\1\0" * 4800
        yield b"\1\0" * 4800

    monkeypatch.setattr(voice_worker.se, "synthesize", synthesize)
    tts = voice_worker.EndpointTTS(
        speech_endpoints.Endpoint("http://x/v1"), "atlas", voice_worker.Bus()
    )
    tts._sample_rate = RATE
    return tts


class FakeBank:
    def __init__(self, kept=None):
        self.kept = kept or {}
        self.wanted: list = []
        self.interrupts = 0
        self.noted: list = []

    def want(self, voice, texts, language=None):
        self.wanted.append((voice, texts, language))

    def take(self, voice, text):
        return self.kept.get((voice, text))

    def interrupt(self):
        self.interrupts += 1

    async def note_sentence(self, voice, text, pcm):
        self.noted.append((voice, text, len(pcm)))


def bridge_of(tts):
    b = voice_worker.Bridge(tts._bus, tts)
    pushed: list = []

    async def push(frame, direction=None):
        pushed.append(frame)

    b.push_frame = push
    return b, pushed


def test_a_cued_say_plays_its_kept_take_without_a_tts_request(monkeypatch):
    asked: list = []
    tts = make_tts(monkeypatch, asked)
    tts.cues = FakeBank({("atlas", "Hmm, let me see."): b"\2\0" * 12000})
    bridge, pushed = bridge_of(tts)

    async def go():
        await bridge._on_app({"t": "say", "id": "s1", "text": "Hmm, let me see.", "cue": True})
        assert [type(f) for f in pushed] == [TTSSpeakFrame, OutputTransportMessageFrame]
        assert pushed[1].message == {"t": "said", "id": "s1"}
        frames = [f async for f in tts.run_tts("Hmm, let me see.", "ctx")]
        assert b"".join(f.audio for f in frames) == b"\2\0" * 12000
        assert asked == []  # no request: the take was rendered and checked ahead

    asyncio.run(go())


def test_a_cued_say_without_a_take_plays_nothing_and_is_still_acked(monkeypatch):
    asked: list = []
    tts = make_tts(monkeypatch, asked)
    tts.cues = FakeBank()
    bridge, pushed = bridge_of(tts)

    async def go():
        await bridge._on_app({"t": "say", "id": "s2", "text": "Hmm, let me see.", "cue": True})
        assert [type(f) for f in pushed] == [OutputTransportMessageFrame]
        assert tts.cue_audio == deque_empty()
        await asyncio.sleep(0.01)
        assert asked == []

    asyncio.run(go())


def deque_empty():
    from collections import deque

    return deque()


def test_the_cue_lines_reach_the_bank_and_a_real_say_interrupts_a_take(monkeypatch):
    tts = make_tts(monkeypatch)
    tts.cues = bank = FakeBank()
    bridge, _ = bridge_of(tts)

    async def go():
        await bridge._on_app(
            {"t": "cues", "voice": None, "language": "de", "texts": ["Hm, Moment mal."]}
        )
        await bridge._on_app({"t": "cues", "voice": "lukas", "texts": ["Hm, Moment mal."]})
        assert bank.wanted == [
            ("atlas", ["Hm, Moment mal."], "de"),
            ("lukas", ["Hm, Moment mal."], None),
        ]
        await bridge._on_app({"t": "say", "id": "s3", "text": "Three things need you."})
        assert bank.interrupts == 1 and bridge.said_at > 0
        tts.drop_prefetch()

    asyncio.run(go())


def test_a_whole_sentence_teaches_the_bank_its_voice_and_a_short_piece_does_not(monkeypatch):
    tts = make_tts(monkeypatch)
    tts.cues = bank = FakeBank()

    async def go():
        for text in ("Okay.", "The docs migration is waiting on a permission."):
            synth = voice_worker._Synth(tts, (text, tts.params()))
            while await synth.queue.get() is not None:
                pass
            await asyncio.sleep(0.01)
        assert bank.noted == [("atlas", "The docs migration is waiting on a permission.", 19200)]

    asyncio.run(go())


def test_a_take_is_rendered_as_a_say_would_be_and_is_bounded(monkeypatch):
    asked: list = []
    tts = make_tts(monkeypatch, asked)
    tts.speed, tts.style = 1.1, "warm"

    async def go():
        pcm = await tts.render_cue("Hm, Moment mal.", "lukas", "de")
        assert len(pcm) == 19200
        ((text, kw),) = asked
        assert (text, kw["voice"], kw["language"], kw["speed"], kw["extra"]) == (
            "Hm, Moment mal.",
            "lukas",
            "de",
            1.1,
            {"style": "warm"},
        )

    asyncio.run(go())

    async def endless(ep, text, **kw):
        while True:
            yield b"\1\0" * 24000

    monkeypatch.setattr(voice_worker.se, "synthesize", endless)

    async def go_long():
        pcm = await tts.render_cue("Hmm, let me see.", "atlas", None)
        assert len(pcm) <= int((voice_worker.CUE_MAX_S + 0.5) * RATE) * 2 + 48000

    asyncio.run(go_long())


def test_the_worker_is_idle_only_with_nothing_said_heard_or_synthesizing(monkeypatch):
    tts = make_tts(monkeypatch)
    tts.cues = FakeBank()
    bridge, _ = bridge_of(tts)
    assert bridge.idle()
    bridge.user_speaking = True
    assert not bridge.idle()
    bridge.user_speaking, bridge.bot_speaking = False, True
    assert not bridge.idle()
    bridge.bot_speaking = False
    bridge.said_at = voice_worker.time.monotonic()
    assert not bridge.idle()
