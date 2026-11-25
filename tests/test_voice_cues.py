"""#2339 — HQ's thinking cue: a short FIXED line rendered ahead, kept only in the voice's own pitch.

`voice_cues` is Pipecat- and numpy-free. Its pitch estimate only has to tell a take in the voice's
register from one that left it (often by an octave or more, measured on VoxCPM2), so it is pinned
on synthetic voiced signals here; it was checked against pYIN on 106 real renders (#2339 body).
"""

from __future__ import annotations

import array
import asyncio
import math

import pytest

from agent_sessions import voice_cues
from agent_sessions.voice_cues import CueBank, median_pitch, take_ok

RATE = 24_000


def voiced(f0: float, seconds: float = 1.2, rate: int = RATE, vibrato: float = 0.0) -> bytes:
    """A voice-like PCM16 signal: harmonics of `f0` with falling amplitude, optional vibrato."""
    out = array.array("h")
    phase = 0.0
    for i in range(int(seconds * rate)):
        f = f0 * (1 + vibrato * math.sin(2 * math.pi * 5 * i / rate))
        phase += 2 * math.pi * f / rate
        v = sum(math.sin(k * phase) / k for k in range(1, 8))
        out.append(int(8000 * v))
    return out.tobytes()


def silence(seconds: float, rate: int = RATE) -> bytes:
    return b"\0\0" * int(seconds * rate)


@pytest.mark.parametrize("f0", [85.0, 104.0, 140.0, 185.0, 230.0, 320.0])
def test_the_pitch_of_a_voiced_signal_is_read_within_a_few_percent(f0):
    p = median_pitch(voiced(f0, vibrato=0.03), RATE)
    assert p is not None and abs(p / f0 - 1) < 0.06


def test_silence_and_noise_floor_have_no_pitch():
    assert median_pitch(silence(1.0), RATE) is None
    assert median_pitch(b"", RATE) is None


def test_quiet_lead_in_does_not_move_the_pitch():
    """A take that starts or ends quiet (the server's lead-in) is measured on its voiced part."""
    pcm = silence(0.3) + voiced(110.0) + silence(0.4)
    assert abs(median_pitch(pcm, RATE) / 110.0 - 1) < 0.06


def test_a_take_in_the_voice_is_kept_one_an_octave_up_is_not():
    ref = 104.0
    assert take_ok(voiced(110.0), RATE, ref)
    assert not take_ok(voiced(220.0), RATE, ref)  # the drift measured on "Okay, one sec."
    assert not take_ok(voiced(70.0), RATE, ref)


def test_no_reference_or_a_bad_length_is_never_a_cue():
    assert not take_ok(voiced(110.0), RATE, None)
    assert not take_ok(voiced(110.0, seconds=0.2), RATE, 110.0)  # too short: nothing said
    assert not take_ok(voiced(110.0, seconds=voice_cues.CUE_MAX_S + 0.5), RATE, 110.0)  # runaway
    assert not take_ok(silence(1.0), RATE, 110.0)


def test_the_cue_lines_are_fixed_short_and_whole():
    for lang, lines in voice_cues.CUE_LINES.items():
        assert len(lang) == 2 and lines
        for t in lines:
            assert 3 <= len(t.split()) and len(t) <= voice_cues.CUE_TEXT_MAX


# --- the bank -------------------------------------------------------------------------------------


class Bank:
    """A CueBank over a fake renderer: `takes[text]` lists the pitch each next take comes out at."""

    def __init__(self, takes: dict[str, list[float]], idle=lambda: True):
        self.takes = {t: list(v) for t, v in takes.items()}
        self.sent: list[dict] = []
        self.rendered: list[tuple] = []
        self.hold: asyncio.Event | None = None
        self.idle = idle

        async def render(text, voice, language):
            self.rendered.append((text, voice, language))
            if self.hold is not None:
                await self.hold.wait()
            return voiced(self.takes[text].pop(0))

        async def send(msg):
            self.sent.append(msg)

        self.bank = CueBank(render, send, lambda: self.idle(), RATE)
        self.bank.POLL_S = 0.001


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def until(pred, limit=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not pred():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.005)


def test_a_line_is_rendered_only_once_the_voice_has_a_reference_then_kept_and_reported():
    b = Bank({"Hmm, let me see.": [110.0]})

    async def go():
        b.bank.start()
        b.bank.want("atlas", ["Hmm, let me see."], "en")
        await asyncio.sleep(0.05)
        assert b.rendered == []  # no sentence heard in this voice yet: nothing to measure against
        await b.bank.note_sentence("atlas", "HQ, General Lukas speaking. Go ahead.", voiced(104.0))
        await until(lambda: b.sent)
        assert b.rendered == [("Hmm, let me see.", "atlas", "en")]
        assert b.sent == [{"t": "cue_ready", "voice": "atlas", "text": "Hmm, let me see."}]
        assert b.bank.take("atlas", "Hmm, let me see.") is not None
        await b.bank.stop()

    run(go())


def test_a_drifted_take_is_rendered_again_and_a_line_gives_up_after_its_takes():
    b = Bank({
        "Okay, let me look.": [230.0, 120.0],
        "Right, let me check that.": [230.0] * voice_cues.CUE_TAKES,
    })  # fmt: skip

    async def go():
        await b.bank.note_sentence("atlas", "This is a whole sentence of words.", voiced(104.0))
        b.bank.want("atlas", ["Okay, let me look.", "Right, let me check that."])
        b.bank.start()
        await until(lambda: len(b.rendered) == 2 + voice_cues.CUE_TAKES)
        await asyncio.sleep(0.05)
        assert len(b.rendered) == 2 + voice_cues.CUE_TAKES  # no take after the last
        assert b.sent == [{"t": "cue_ready", "voice": "atlas", "text": "Okay, let me look."}]
        assert b.bank.take("atlas", "Right, let me check that.") is None
        await b.bank.stop()

    run(go())


def test_nothing_is_rendered_while_the_call_is_busy():
    busy = [True]
    b = Bank({"Hmm, let me see.": [110.0]}, idle=lambda: not busy[0])

    async def go():
        await b.bank.note_sentence("atlas", "This is a whole sentence of words.", voiced(104.0))
        b.bank.want("atlas", ["Hmm, let me see."])
        b.bank.start()
        await asyncio.sleep(0.05)
        assert b.rendered == []
        busy[0] = False
        await until(lambda: b.sent)
        await b.bank.stop()

    run(go())


def test_an_interrupted_take_is_not_a_try_and_is_rendered_again():
    b = Bank({"Hmm, let me see.": [110.0, 110.0]})

    async def go():
        await b.bank.note_sentence("atlas", "This is a whole sentence of words.", voiced(104.0))
        b.hold = asyncio.Event()
        b.bank.want("atlas", ["Hmm, let me see."])
        b.bank.start()
        await until(lambda: len(b.rendered) == 1)
        b.bank.interrupt()  # a say arrived: real speech first
        await until(lambda: len(b.rendered) == 2)
        assert b.bank.tries == {}
        b.hold.set()
        await until(lambda: b.sent)
        await b.bank.stop()

    run(go())


def test_only_bounded_fixed_shaped_lines_are_accepted():
    b = Bank({})
    b.bank.want("atlas", "Hmm")  # not a list
    b.bank.want("atlas", ["", "x" * (voice_cues.CUE_TEXT_MAX + 1), 7])
    assert b.bank.wanted.get("atlas", []) == []
    b.bank.want("atlas", [f"line number {i}" for i in range(20)], "english")
    assert len(b.bank.wanted["atlas"]) == voice_cues.CUE_TEXTS_MAX
    assert b.bank.languages[("atlas", "line number 0")] is None  # not a language code


def test_a_short_say_piece_is_no_reference():
    b = Bank({})

    async def go():
        await b.bank.note_sentence("atlas", "Okay.", voiced(104.0))
        assert b.bank.reference("atlas") is None
        for _ in range(voice_cues.REFERENCES_KEPT + 2):
            await b.bank.note_sentence("atlas", "A whole sentence of six words.", voiced(104.0))
        assert len(b.bank.refs["atlas"]) == voice_cues.REFERENCES_KEPT

    run(go())


def test_a_take_is_trimmed_to_its_sound():
    pcm = silence(0.5) + voiced(110.0, seconds=1.0) + silence(0.7)
    t = voice_cues.trim_quiet(pcm, RATE)
    assert 1.0 <= len(t) / (2 * RATE) <= 1.1
    assert voice_cues.trim_quiet(silence(1.0), RATE) == b""


def test_a_take_that_never_finishes_rendering_is_a_failed_take(monkeypatch):
    monkeypatch.setattr(voice_cues, "CUE_RENDER_TIMEOUT_S", 0.05)
    sent: list = []
    rendered: list = []

    async def render(text, voice, language):
        rendered.append(text)
        await asyncio.Event().wait()  # a server that never answers

    async def send(msg):
        sent.append(msg)

    async def go():
        bank = CueBank(render, send, lambda: True, RATE)
        bank.POLL_S = 0.001
        await bank.note_sentence("atlas", "This is a whole sentence of words.", voiced(104.0))
        bank.want("atlas", ["Hmm, let me see."])
        bank.start()
        await until(lambda: len(rendered) == voice_cues.CUE_TAKES, limit=5)
        await asyncio.sleep(0.2)
        assert sent == [] and bank.tries[("atlas", "Hmm, let me see.")] == voice_cues.CUE_TAKES
        await bank.stop()

    run(go())


def test_the_reference_is_a_true_median():
    """Live check on #2339: references of 123 and 98 Hz must not let a 154 Hz take through."""
    b = Bank({})
    b.bank.refs["atlas"] = [123.0, 98.0]
    assert b.bank.reference("atlas") == 110.5
    assert not take_ok(voiced(154.0), RATE, b.bank.reference("atlas"))
    b.bank.refs["atlas"] = [123.0, 98.0, 104.0]
    assert b.bank.reference("atlas") == 104.0


def test_a_line_already_kept_is_reported_again_when_asked_again():
    """Hermes 8831: a German language voice equal to the default asks for lines in the SAME
    effective voice; one already kept must be reported again, never left unreported."""
    b = Bank({"Hmm, let me see.": [110.0], "Hm, Moment mal.": [110.0]})

    async def go():
        await b.bank.note_sentence("atlas", "This is a whole sentence of words.", voiced(104.0))
        b.bank.want("atlas", ["Hmm, let me see."])
        b.bank.start()
        await until(lambda: len(b.sent) == 1)
        b.bank.want("atlas", ["Hmm, let me see.", "Hm, Moment mal."], "de")
        await until(lambda: len(b.sent) == 3)
        assert sorted(m["text"] for m in b.sent) == [
            "Hm, Moment mal.",
            "Hmm, let me see.",
            "Hmm, let me see.",
        ]
        assert all(m["voice"] == "atlas" for m in b.sent) and len(b.rendered) == 2
        await b.bank.stop()

    run(go())
