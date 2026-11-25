"""The speech compatibility check (#1397 P0): does this pair of endpoints do what a call needs?

Run by Settings → HQ calls → **Test both** and by ``scripts/voice_conformance.py``. It makes real
calls through ``speech_endpoints`` (the only outbound module) and reports, per capability, whether
the endpoint passed, and a plain-language line for anything missing — "no streaming: higher first
audio latency", "no voice list: enter the voice". A call is offered only when the required
capabilities pass; the optional ones degrade the experience and are named, not hidden.

Required: STT transcribes; TTS synthesizes 16-bit PCM (raw or WAV) for the chosen voice.
Optional: model lists, a voice list, streamed synthesis (first audio before the whole sentence).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from . import speech_endpoints as se

#: The phrase synthesized and transcribed back. Plain words a speech model cannot mishear by
#: design, plus the vocabulary a call uses most ("Commander", a number).
PROBE_PHRASE = "Commander, the rebase finished twelve minutes ago."
PROBE_WORDS = ("commander", "rebase", "finished", "minutes")
#: Rate the probe asks synthesis for; the worker uses the same.
PROBE_RATE = 16_000
#: First audio later than this is reported as slow.
SLOW_FIRST_AUDIO_S = 1.5


@dataclass
class Check:
    id: str
    ok: bool
    required: bool
    note: str = ""
    value: object = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    def add(self, *a, **kw) -> Check:
        c = Check(*a, **kw)
        self.checks.append(c)
        return c

    def as_dict(self) -> dict:
        return {"usable": self.usable, "checks": [asdict(c) for c in self.checks]}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


async def check(stt: se.Endpoint, tts: se.Endpoint, voice: str) -> Report:
    rep = Report()

    # Discovery (optional): a list makes setup a pick instead of typing.
    for side, ep in (("stt", stt), ("tts", tts)):
        try:
            models = await se.list_models(ep)
            rep.add(f"{side}.models", True, False, value=models)
        except se.SpeechError as e:
            rep.add(f"{side}.models", False, False, f"no model list ({e.detail}): enter the model")
    try:
        voices = await se.list_voices(tts)
    except se.SpeechError as e:
        voices = None
        rep.add("tts.voices", False, False, f"no voice list ({e.detail}): enter the voice")
    else:
        if voices is None:
            rep.add("tts.voices", False, False, "no voice list: enter the voice")
        else:
            rep.add("tts.voices", True, False, value=voices)
            if voice and voice not in voices:
                rep.add("tts.voice", False, True, f"the endpoint does not list the voice {voice!r}")

    # Synthesis (required) + streaming (optional).
    pcm = bytearray()
    first = total = None
    started = time.monotonic()
    try:
        async for chunk in se.synthesize(tts, PROBE_PHRASE, voice=voice, out_rate=PROBE_RATE):
            if first is None and chunk:
                first = time.monotonic() - started
            pcm += chunk
        total = time.monotonic() - started
    except se.SpeechError as e:
        rep.add("tts.synthesize", False, True, f"synthesis failed: {e.detail}")
    if total is not None:
        seconds = len(pcm) / 2 / PROBE_RATE
        if seconds < 0.5:
            rep.add("tts.synthesize", False, True, "synthesis returned (almost) no audio")
        else:
            rep.add(
                "tts.synthesize",
                True,
                True,
                value={
                    "audio_s": round(seconds, 2),
                    "first_audio_s": round(first or total, 2),
                    "total_s": round(total, 2),
                    "real_time_factor": round(total / seconds, 2),
                },
            )
            streamed = first is not None and first < total * 0.6
            rep.add(
                "tts.streaming",
                streamed,
                False,
                "" if streamed else "no streaming: first audio waits for the whole sentence",
            )
            # Optional (#1397 P3): does the server ACCEPT a `language` field? Advertised languages
            # alone don't prove it; a strict server may refuse the unknown field. Only a probe that
            # comes back with audio lets a call send one.
            try:
                n = 0
                async for chunk in se.synthesize(
                    tts, "Ready.", voice=voice, out_rate=PROBE_RATE, language="en"
                ):
                    n += len(chunk)
                took = n > 0
            except se.SpeechError:
                took = False
            rep.add(
                "tts.language",
                took,
                False,
                "" if took else "no language field: HQ speaks every language in the voice's own",
            )
            if (first or total) > SLOW_FIRST_AUDIO_S:
                rep.add(
                    "tts.first_audio",
                    False,
                    False,
                    f"first audio after {first or total:.1f} s: HQ will start speaking late",
                )
            if total / seconds > 1.0:
                rep.add(
                    "tts.real_time",
                    False,
                    False,
                    f"synthesis is slower than real time ({total / seconds:.1f}×): long answers "
                    "pause between sentences",
                )

    # Transcription (required): the synthesized phrase must come back recognisable.
    if pcm:
        ok = False
        try:
            tr = await se.transcribe(stt, bytes(pcm), PROBE_RATE, language="en")
        except se.SpeechError as e:
            rep.add("stt.transcribe", False, True, f"transcription failed: {e.detail}")
        else:
            heard = _words(tr.text)
            hits = sum(1 for w in PROBE_WORDS if w in heard)
            ok = hits >= len(PROBE_WORDS) - 1
            # Only counts and timing: the report is shown and printed, so it never carries what
            # an endpoint said back (a transcript is a response body).
            rep.add(
                "stt.transcribe",
                ok,
                True,
                ""
                if ok
                else f"the probe phrase came back unrecognisable ({hits}/{len(PROBE_WORDS)} words)",
                value={
                    "matched_words": hits,
                    "expected_words": len(PROBE_WORDS),
                    "seconds": round(tr.seconds, 2),
                },
            )
        # Optional (#1397 P3): does the endpoint report the language it heard? Then a call can
        # follow the Commander's language; otherwise it stays in the chosen one.
        if ok:
            # The verdict must be PROVEN either way (#1635 reviews): `detects` — it reported a
            # language, even one we cannot map (then every utterance must still be heard as
            # English: strict); `no_language` — ONLY a successful JSON reply with no `language`
            # KEY at all (an empty or null one proves nothing about other utterances). Any
            # refusal (its reason is unknown), timeout, 5xx, unreachable or malformed reply is
            # `unknown`: never evidence that it cannot detect, which would relax the
            # spoken-confirmation gate.
            try:
                det = await se.transcribe(stt, bytes(pcm), PROBE_RATE, detect=True)
                verdict = (
                    "detects"
                    if det.reported_language
                    else "no_language"
                    if det.structured and not det.language_key
                    else "unknown"
                )
            except se.SpeechError:
                verdict = "unknown"
            rep.add(
                "stt.language",
                verdict == "detects",
                False,
                {
                    "detects": "",
                    "no_language": "no language detection: calls stay in the language you choose",
                    "unknown": "language detection could not be checked: test again (until then "
                    "a spoken confirm is refused)",
                }[verdict],
                value={"verdict": verdict},
            )
    return rep


def detects_language(rep: Report) -> bool | None:
    """The STT language verdict of a report: True / False only when proven, else None."""
    for c in rep.checks:
        if c.id == "stt.language":
            verdict = c.value.get("verdict") if isinstance(c.value, dict) else None
            return {"detects": True, "no_language": False}.get(verdict)
    return None
