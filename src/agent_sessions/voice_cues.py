"""HQ's thinking cue on a call (#2339): a short FIXED line ("Hmm, let me see.") played from audio
rendered ahead, so a question is acknowledged at once and the answer never waits for TTS.

No Pipecat imports and no numpy (the app's base install has neither), so CI tests it.

Why a cue is rendered ahead and checked, never synthesized when it is needed: VoxCPM2 voices a
line of one to five words outside its voice's pitch in about one take of six (measured on the PoC
renders of 2026-11-24: "Ah, there it is." at 470–500 Hz from voices that speak at 104 and 185 Hz;
"Okay, one sec." on atlas 0 of 6 takes within 25 %). So each take is kept only if its median
pitch is close to the pitch of a whole sentence the call has already spoken in that voice.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import math
import operator
import sys
from collections.abc import Awaitable, Callable

from .voice_text import WHOLE_MIN_WORDS

#: The cue lines, FIXED per language: never model or user text. At least three words each (a
#: shorter line drifts more often), casual, and saying nothing about what HQ will find.
CUE_LINES: dict[str, tuple[str, ...]] = {
    "en": ("Hmm, let me see.", "Okay, let me look.", "Right, let me check that."),
    "de": ("Hm, Moment mal.", "Okay, ich schau mal.", "Mal sehen, Moment."),
}
#: Takes rendered per line before it is given up for this call.
CUE_TAKES = 4
#: A take is kept when its median pitch is within this share of the voice's sentence pitch.
CUE_PITCH_TOL = 0.25
#: A kept take's length bounds (seconds, after `trim_quiet`): an empty or runaway render is not
#: a cue, and a cue still playing when the answer is ready delays it by at most what is left.
CUE_MIN_S = 0.4
CUE_MAX_S = 2.0
#: One take may take this long to render; a slower one is a failed take, never a held bank.
CUE_RENDER_TIMEOUT_S = 8.0
#: The worker accepts at most this many lines per voice, each at most this long.
CUE_TEXTS_MAX = 6
CUE_TEXT_MAX = 60
#: Sentence pitch is measured on at most this much of a sentence's audio (seconds).
REFERENCE_MAX_S = 4.0
#: Sentences averaged into a voice's reference pitch.
REFERENCES_KEPT = 3

# The pitch estimate works at 8 kHz on 30 ms frames, 60–500 Hz.
_RATE = 8000
_FRAME = 240
_LAG_MIN = _RATE // 500
_LAG_MAX = _RATE // 60
_VOICED = 0.5  # normalised autocorrelation a frame must reach to count as voiced
_FIRST_PEAK = 0.85  # the shortest lag within this share of the best one: no octave-down errors
_MIN_VOICED_FRAMES = 4


def _samples(pcm: bytes, rate: int) -> list[float]:
    """PCM16 mono at `rate` as floats at about 8 kHz (a box average as the anti-alias filter)."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    step = max(1, round(rate / _RATE))
    n = len(a) // step
    if step == 1:
        return [float(x) for x in a]
    return [sum(a[i * step : (i + 1) * step]) / step for i in range(n)]


def _frame_pitch(x: list[float], rate: int) -> float | None:
    n = len(x)
    mean = sum(x) / n
    x = [v - mean for v in x]
    best, lags = 0.0, []
    for lag in range(_LAG_MIN, min(_LAG_MAX, n // 2) + 1):
        a, b = x[: n - lag], x[lag:]
        num = sum(map(operator.mul, a, b))
        den = math.sqrt(sum(map(operator.mul, a, a)) * sum(map(operator.mul, b, b)))
        r = num / den if den > 0 else 0.0
        lags.append((lag, r))
        best = max(best, r)
    if best < _VOICED:
        return None
    for i, (lag, r) in enumerate(lags):
        # the first LOCAL peak close to the best one: a period, not a multiple of it
        prev = lags[i - 1][1] if i else -1.0
        nxt = lags[i + 1][1] if i + 1 < len(lags) else -1.0
        if r >= _FIRST_PEAK * best and r >= prev and r >= nxt:
            return rate / lag
    return None


def median_pitch(pcm: bytes, rate: int, max_s: float | None = None) -> float | None:
    """The median fundamental frequency (Hz) of the voiced frames of PCM16 mono audio, or None
    when too little of it is voiced to say. Coarse by design: it only has to tell a take in the
    voice's own register from one that left it, often by an octave or more."""
    if max_s is not None:
        pcm = pcm[: int(max_s * rate) * 2]
    x = _samples(pcm, rate)
    eff = rate / max(1, round(rate / _RATE))
    frames = [x[i : i + _FRAME] for i in range(0, len(x) - _FRAME + 1, _FRAME)]
    if not frames:
        return None
    energy = [math.sqrt(sum(v * v for v in f) / _FRAME) for f in frames]
    loud = max(energy)
    if loud <= 0:
        return None
    pitches = sorted(
        p
        for f, e in zip(frames, energy, strict=True)
        if e >= 0.1 * loud
        if (p := _frame_pitch(f, eff)) is not None
    )
    if len(pitches) < _MIN_VOICED_FRAMES:
        return None
    return pitches[len(pitches) // 2]


def trim_quiet(pcm: bytes, rate: int, pad_s: float = 0.03) -> bytes:
    """`pcm` without its quiet start and end (10 ms frames under 2 % of the loudest one), keeping
    `pad_s` around the sound: a cue is its words, not the render's lead-in or tail."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    n = max(1, rate // 100)
    peaks = [max(map(abs, a[i : i + n]), default=0) for i in range(0, len(a), n)]
    loud = max(peaks, default=0)
    if loud == 0:
        return b""
    on = [i for i, v in enumerate(peaks) if v >= 0.02 * loud]
    pad = int(pad_s * rate)
    start, end = max(0, on[0] * n - pad), min(len(a), (on[-1] + 1) * n + pad)
    return pcm[start * 2 : end * 2]


def take_ok(pcm: bytes, rate: int, reference: float | None) -> bool:
    """A rendered take may be a cue: a sane length, and its median pitch within CUE_PITCH_TOL of
    the voice's own sentence pitch. No reference, or a take whose pitch can't be read: no."""
    if not reference:
        return False
    seconds = len(pcm) / (2 * rate)
    if not CUE_MIN_S <= seconds <= CUE_MAX_S:
        return False
    pitch = median_pitch(pcm, rate)
    return pitch is not None and abs(pitch / reference - 1) <= CUE_PITCH_TOL


def is_sentence(text: str) -> bool:
    """A say piece long enough to measure its voice by (#1996: six words or more never left it)."""
    return len(text.split()) >= WHOLE_MIN_WORDS


class CueBank:
    """The worker's cues for one call: which lines each voice wants, the voice's reference
    pitch, the takes kept.

    `render(text, voice, language)` returns one take's PCM (at `rate`), spoken as a say in that
    language would be; `send` tells the app a line is ready; `idle()` says the call has nothing
    to speak or hear right now. Takes are rendered one at a time and only while idle, and
    `interrupt()` (any say, the Commander starting to speak) cancels the one in flight, which is
    rendered again later, uncounted."""

    #: How often the renderer looks for an idle moment (seconds).
    POLL_S = 0.25

    def __init__(
        self,
        render: Callable[[str, str, str | None], Awaitable[bytes]],
        send: Callable[[dict], Awaitable[None]],
        idle: Callable[[], bool],
        rate: int,
    ) -> None:
        self._render = render
        self._send = send
        self._idle = idle
        self.rate = rate
        self.wanted: dict[str, list[str]] = {}  # effective voice → lines, in request order
        self.languages: dict[tuple[str, str], str | None] = {}  # (voice, line) → its say's language
        self.refs: dict[str, list[float]] = {}  # voice → sentence pitches measured
        self.kept: dict[tuple[str, str], bytes] = {}
        self.tries: dict[tuple[str, str], int] = {}
        self._wake = asyncio.Event()
        self._reports: list[tuple[str, str]] = []  # kept lines asked for again: report once more
        self._take: asyncio.Task | None = None
        self._runner: asyncio.Task | None = None

    # -- the app's side --------------------------------------------------------------------------

    def want(self, voice: str, texts: object, language: object = None) -> None:
        """Lines the app may ask for in `voice` — the EFFECTIVE voice a say is spoken in, the name
        `cue_ready` reports back, so the app and the bank mean one voice by one name however it
        was reached (a language voice equal to the default included) — spoken with the say
        language `language` (None: the server detects it). A line already kept is reported
        again, so a later ask is never left waiting."""
        if not isinstance(texts, list):
            return
        lang = language if isinstance(language, str) and len(language) == 2 else None
        lines = self.wanted.setdefault(voice, [])
        for t in texts:
            if (isinstance(t, str) and 0 < len(t) <= CUE_TEXT_MAX and t not in lines
                    and len(lines) < CUE_TEXTS_MAX):  # fmt: skip
                lines.append(t)
                self.languages[(voice, t)] = lang
            elif isinstance(t, str) and (voice, t) in self.kept:
                self._reports.append((voice, t))
        self._wake.set()

    def take(self, voice: str, text: str) -> bytes | None:
        return self.kept.get((voice, text))

    # -- what the worker learns ------------------------------------------------------------------

    def reference(self, voice: str) -> float | None:
        """The voice's sentence pitch: the true median of its measured sentences (of two, their
        mean — never the higher one, which let a drifted take through on the live check)."""
        r = sorted(self.refs.get(voice, []))
        if not r:
            return None
        mid = len(r) // 2
        return r[mid] if len(r) % 2 else (r[mid - 1] + r[mid]) / 2

    async def note_sentence(self, voice: str, text: str, pcm: bytes) -> None:
        """A sentence spoken whole in `voice`: its pitch becomes part of the voice's reference."""
        if not is_sentence(text) or len(self.refs.get(voice, [])) >= REFERENCES_KEPT:
            return
        pitch = await asyncio.to_thread(median_pitch, pcm, self.rate, REFERENCE_MAX_S)
        if pitch is not None:
            refs = self.refs.setdefault(voice, [])
            if len(refs) < REFERENCES_KEPT:
                refs.append(pitch)
                self._wake.set()

    def interrupt(self) -> None:
        """Something needs the TTS or the line: the take in flight stops now."""
        if self._take is not None and not self._take.done():
            self._take.cancel()

    # -- rendering -------------------------------------------------------------------------------

    def _next(self) -> tuple[str, str] | None:
        for voice, lines in self.wanted.items():
            if self.reference(voice) is None:
                continue
            for t in lines:
                key = (voice, t)
                if key not in self.kept and self.tries.get(key, 0) < CUE_TAKES:
                    return key
        return None

    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.interrupt()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
            self._runner = None

    async def _report(self, voice: str, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._send({"t": "cue_ready", "voice": voice, "text": text})

    async def _run(self) -> None:
        while True:
            while self._reports:
                await self._report(*self._reports.pop(0))
            key = self._next()
            if key is None:
                self._wake.clear()
                if not self._reports:
                    await self._wake.wait()
                continue
            if not self._idle():
                await asyncio.sleep(self.POLL_S)
                continue
            voice, text = key
            self._take = asyncio.create_task(
                asyncio.wait_for(
                    self._render(text, voice, self.languages.get(key)), CUE_RENDER_TIMEOUT_S
                )
            )
            try:
                pcm = await self._take
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # the bank itself is stopping
                continue  # interrupted: not a try, rendered again once idle
            except Exception:  # noqa: BLE001 — a failed or timed-out render is a failed take
                pcm = b""
            finally:
                self._take = None
            pcm = await asyncio.to_thread(trim_quiet, pcm, self.rate)
            ok = await asyncio.to_thread(take_ok, pcm, self.rate, self.reference(voice))
            if ok:
                self.kept[key] = pcm
                await self._report(voice, text)
            else:
                self.tries[key] = self.tries.get(key, 0) + 1
