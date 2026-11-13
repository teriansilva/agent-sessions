"""The sentences the worker is synthesizing: the one playing and a look-ahead (#1397).

No Pipecat imports, so CI tests it. A barge-in (`drop`) cancels ALL of them — the look-ahead and
the sentence playing right now — because an interruption clears playback without closing the TTS
generator that is still pulling audio from the endpoint.

Each sentence is synthesized with the parameters (voice, language, …) of the SAY it came from,
taken when that say arrived (`say` / `begin`): a later say switching voice or language never
reaches the sentences still queued from an earlier one (#1635 review).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .voice_text import split_speech

#: A synthesis this short says nothing about how its server sends audio.
PACE_MIN_AUDIO_S = 1.0
#: A server that streams has sent its first audio well before the sentence is finished; one that
#: does not sends everything at the end. Measured: 0.2 s of 3 s on VoxCPM2, against all of it.
PACE_FIRST_SHARE = 0.5


class Pace:
    """Whether the TTS server sends a sentence's audio WHILE it generates it, learnt from the
    sentences the call has already spoken (the greeting is the first). The worker voices whole
    sentences on a server that does (`voice_text.split_speech(whole=True)`) and keeps cutting
    them short on one that does not, where a short first piece is the only early audio there is.
    ``None``: nothing measured yet."""

    def __init__(self) -> None:
        self.streams: bool | None = None

    def note(self, first_s: float, total_s: float, audio_s: float) -> None:
        """One finished synthesis: its first audio came `first_s` after it was asked for, all of
        its `audio_s` seconds of audio after `total_s`. The newest one that says anything
        decides, so a server that changes (a cold first request, a fallback to whole files) is
        followed."""
        if audio_s < PACE_MIN_AUDIO_S or total_s <= 0 or first_s < 0:
            return
        self.streams = first_s <= PACE_FIRST_SHARE * total_s


async def paced(chunks, pace: Pace, rate: int, clock: Callable[[], float]):
    """`chunks` (one sentence's PCM16 mono audio at `rate`, as the endpoint sends it) passed on
    unchanged, with what it showed noted on `pace` — only for a sentence that came back WHOLE.
    One that failed, or was cut off by a barge-in, says nothing about how the server sends."""
    asked = clock()
    first: float | None = None
    size = 0
    async for chunk in chunks:
        if first is None:
            first = clock() - asked
        size += len(chunk)
        yield chunk
    if first is not None:
        pace.note(first, clock() - asked, size / (2 * rate))


def speech_pieces(text: str, pace: Pace, first_words: int) -> list[str]:
    """What one say is sent to the TTS server as. Whole sentences where the server is KNOWN to
    stream; else (it does not, or nothing was measured yet: the call's first words) the first
    piece is cut to `first_words`, the earliest audio such a server can give. That cold start is
    the one place a reply is still cut on a streaming server: its greeting."""
    if pace.streams:
        return split_speech(text, whole=True)
    return split_speech(text, first_max=first_words)


class SpeechOrder:
    """Sentences ask the TTS server one first-audio at a time. A server with one model takes
    whichever request reaches it first: a look-ahead sent together with the sentence before it won
    that race in 5 of 9 replies, and then the reply's first words waited for its second sentence
    (first audio 2.17 s on average, against 0.27 s in order). A look-ahead loses nothing by
    waiting: that server couldn't have started it sooner anyway."""

    def __init__(self) -> None:
        self._last: asyncio.Event | None = None

    def turn(self, task_of: Callable[[asyncio.Event | None, asyncio.Event], asyncio.Task]):
        """Start the next sentence's task: `task_of(wait_for, set_at_first_audio)`. Whatever
        becomes of that task, the sentence after it is let through: one cancelled before it ever
        ran never reaches its own `finally`."""
        before, started = self._last, asyncio.Event()
        self._last = started
        task = task_of(before, started)
        task.add_done_callback(lambda _: started.set())
        return task


class Synths:
    def __init__(self, make: Callable[[str], object], lookahead: int) -> None:
        self._make = make  # text → a synth with a `.task` (an asyncio task)
        self._lookahead = lookahead
        self.ahead: dict[str, object] = {}
        self.pending: list = []
        self.active: object | None = None
        self._queued: list[tuple] = []  # (text, params) of each sentence not yet started, in order

    def say(self, texts: list[str], params: object) -> None:
        """Queue one say's sentences with the parameters they are to be spoken with."""
        keys = [(t, params) for t in texts]
        self._queued.extend(keys)
        self.prefetch(keys)

    def begin(self, text: str, current: object):
        """The synth for the next queued sentence with this text, made with ITS say's
        parameters (`current` only for a sentence that was never queued)."""
        for i, key in enumerate(self._queued):
            if key[0] == text:
                del self._queued[i]
                return self.start(key)
        return self.start((text, current))

    def prefetch(self, texts: list) -> None:
        self.pending.extend(texts)
        self._top_up(started=None)

    def _top_up(self, started: str | None) -> None:
        if started in self.pending:
            self.pending.remove(started)
        for t in self.pending[: self._lookahead + (1 if started is None else 0)]:
            if t not in self.ahead:
                self.ahead[t] = self._make(t)

    def start(self, text: str):
        """The synth for the sentence starting to play now (prefetched, or made now)."""
        synth = self.ahead.pop(text, None) or self._make(text)
        self.active = synth
        self._top_up(started=text)
        return synth

    def finished(self, synth) -> None:
        if self.active is synth:
            self.active = None

    def drop(self) -> None:
        """Barge-in: cancel the sentence playing AND everything synthesized ahead."""
        for synth in [*self.ahead.values(), *([self.active] if self.active else [])]:
            synth.task.cancel()
        self.ahead.clear()
        self.pending.clear()
        self._queued.clear()
        self.active = None
