"""The voice worker (#1397): one call's speech, in its own process, behind a private socket.

Started by ``voice_calls.start_worker`` as ``python -m agent_sessions.voice_worker --socket PATH``
with a sanitized environment (only ``VOICE_STT_TOKEN`` / ``VOICE_TTS_TOKEN``). It serves ONE
WebSocket on that Unix socket; the app is its only client. It is a **speech terminal**: it hears
(Silero VAD → Smart Turn v3 end-of-turn → STT) and speaks (TTS), and knows nothing about HQ.

Protocol with the app (JSON text + binary PCM16 mono):

- app → worker: first ``{"t":"config", stt, tts, in_rate, out_rate, stop_secs, interrupt}``;
  then mic audio (binary, ``in_rate``), ``{"t":"say","id","text"}``, ``{"t":"mute","on"}``,
  ``{"t":"stop"}``.
- worker → app: ``{"t":"ready"}``, ``{"t":"user_started","turn"}``, ``{"t":"user_stopped"}``,
  ``{"t":"utterance","text","turn"}`` (``turn``: the number of the user turn it was heard in; a
  LATE transcript is ``{"t":"utterance","text","late":true}``), ``{"t":"interrupt"}`` (barge-in:
  the client flushes playback; Pipecat sends it at every user turn start),
  ``{"t":"bot_started"}``, ``{"t":"bot_stopped"}``, ``{"t":"error","endpoint","code","detail"}``;
  and speech audio (binary, ``out_rate``).

Pipecat (BSD-2-Clause) does the conversation mechanics; every byte to a speech endpoint goes
through ``speech_endpoints`` (the inventoried transport).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket

# Pipecat is an optional dependency (`agent-sessions[voice]`); importing this module without it
# fails loudly, which is how `voice_calls.runtime_ready` tells the runtime is missing.
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.audio.vad_processor import VADProcessor  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.serializers.base_serializer import FrameSerializer  # noqa: E402
from pipecat.services.settings import STTSettings, TTSSettings  # noqa: E402
from pipecat.services.stt_service import SegmentedSTTService  # noqa: E402
from pipecat.services.tts_service import TTSService  # noqa: E402
from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_start import VADUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy  # noqa: E402
from pipecat.turns.user_turn_processor import UserTurnProcessor  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from . import speech_endpoints as se
from .voice_synth import Synths
from .voice_text import call_language, split_speech
from .voice_turns import Turns

#: How long after the end of a turn we wait for its last transcript before giving up on it.
TRANSCRIPT_GRACE_S = 3.0
#: Synthesis look-ahead: sentences synthesized ahead of the one playing. Two hides most of a
#: slower-than-real-time TTS (measured on VoxCPM2: RTF 1.0–2.1) without flooding the server.
LOOKAHEAD = 1
#: The first chunk of each reply is cut to this many words: first audio sooner on slow TTS.
FIRST_WORDS = 7


class Bus:
    """The worker's side channel to the app: control messages pushed into the pipeline output."""

    def __init__(self) -> None:
        self.task: PipelineWorker | None = None
        #: Which transcript belongs to which turn (`voice_turns`): segments sent at VAD stops,
        #: results counted in order, a late result delivered on its own.
        self.turns = Turns()
        #: P3: ask STT for the language (where supported) / pass a fixed one; the call's language
        #: as last detected (a turn heard in one language, of 3+ words).
        self.detect_language = False
        self.language_hint: str | None = None
        self.detected: str | None = None
        self.segment_done = asyncio.Event()

    async def send(self, msg: dict) -> None:
        if self.task is not None:
            await self.task.queue_frame(OutputTransportMessageUrgentFrame(message=msg))


class WireSerializer(FrameSerializer):
    """Binary = PCM16 mono audio; text = one JSON object."""

    def __init__(self, in_rate: int) -> None:
        super().__init__()
        self._in_rate = in_rate

    async def serialize(self, frame: Frame):
        if self.should_ignore_frame(frame):
            return None  # Pipecat's RTVI client protocol is not ours
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, OutputTransportMessageFrame | OutputTransportMessageUrgentFrame):
            return json.dumps(frame.message, separators=(",", ":"))
        if isinstance(frame, InterruptionFrame):
            return '{"t":"interrupt"}'
        return None

    async def deserialize(self, data):
        if isinstance(data, bytes):
            return InputAudioRawFrame(audio=data, sample_rate=self._in_rate, num_channels=1)
        try:
            msg = json.loads(data)
        except ValueError:
            return None
        return InputTransportMessageFrame(message=msg) if isinstance(msg, dict) else None


class EndpointSTT(SegmentedSTTService):
    """Segmented STT against the configured OpenAI-compatible endpoint."""

    def __init__(self, ep: se.Endpoint, bus: Bus, **kw) -> None:
        super().__init__(settings=STTSettings(model=ep.model or None, language=None), **kw)
        self._ep = ep
        self._bus = bus

    @property
    def wants_wav_segments(self) -> bool:
        return False  # we wrap the PCM ourselves in speech_endpoints.transcribe

    async def run_stt(self, audio: bytes):
        try:
            tr = await se.transcribe(
                self._ep, audio, self.sample_rate, language=self._bus.language_hint,
                detect=self._bus.detect_language,
            )  # fmt: skip
        except se.SpeechError as e:
            self._done()
            await self._bus.send(
                {"t": "error", "endpoint": "stt", "code": e.code, "detail": e.detail}
            )
            yield ErrorFrame(error=f"stt: {e.detail}")
            return
        except Exception:  # noqa: BLE001 — unexpected: still a finished (empty) segment, so the
            # turn bookkeeping stays in step (Pipecat would swallow it and the count would lag)
            self._done()
            return
        # The language rides WITH this segment's words, so the turn's language is judged from
        # all of its segments (`Turns`), never from whichever result came back last.
        if tr.text:
            yield TranscriptionFrame(
                text=tr.text, user_id="commander", timestamp=str(time.time()), finalized=True,
                result={"language": tr.language},
            )  # fmt: skip
        else:
            self._done()

    def _done(self) -> None:
        self._bus.turns.segment_empty()
        self._bus.segment_done.set()


@dataclass(frozen=True)
class SayParams:
    """How one say is spoken (#1397 P3), fixed when it arrives."""

    voice: str
    language: str | None
    speed: float | None
    style: str | None


class _Synth:
    """One sentence being synthesized: chunks arrive on a queue as the endpoint sends them."""

    def __init__(self, tts: EndpointTTS, key: tuple) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run(tts, *key))

    async def _run(self, tts: EndpointTTS, text: str, p: SayParams) -> None:
        if tts.plain:  # the server refused an optional field once this call: none are sent
            p = SayParams(p.voice, None, None, None)
        try:
            # aclosing: an interrupted say (cancelled mid-stream) closes its HTTP stream now, not
            # whenever the abandoned generator is collected. `p`: its say's parameters, taken
            # when the say arrived — never the next say's.
            async with contextlib.aclosing(se.synthesize(
                tts._ep, text, voice=p.voice, out_rate=tts.sample_rate, speed=p.speed,
                language=p.language, extra={"style": p.style} if p.style else None,
                plain_fallback=tts.go_plain,
            )) as chunks:  # fmt: skip
                async for chunk in chunks:
                    await self.queue.put(chunk)
            await self.queue.put(None)
        except se.SpeechError as e:
            await self.queue.put(e)
        except asyncio.CancelledError:
            await self.queue.put(None)
            raise


class EndpointTTS(TTSService):
    """TTS against the configured endpoint. Audio is passed on as it arrives, and up to
    `lookahead` following sentences are synthesized while the current one plays."""

    def __init__(
        self, ep: se.Endpoint, voice: str, bus: Bus, lookahead: int = LOOKAHEAD, **kw
    ) -> None:
        super().__init__(
            settings=TTSSettings(model=ep.model or None, voice=voice, language=None), **kw
        )
        self._ep = ep
        self._voice = voice
        self._bus = bus
        self._synths = Synths(lambda key: _Synth(self, key), lookahead)
        #: P3: the contact's style (only when the server advertised it), speed and the language
        #: of the say being spoken.
        self.style: str | None = None
        self.speed: float | None = None
        self.language: str | None = None
        #: A voice for this say's language (#1397 P3), else the contact's voice.
        self.say_voice: str | None = None
        #: Set once the server refused a say carrying speed/language/style (#1635 review): from
        #: then on this call sends none of them, rather than failing every sentence.
        self.plain = False

    def go_plain(self) -> None:
        self.plain = True

    def can_generate_metrics(self) -> bool:
        return False

    def params(self) -> SayParams:
        """The parameters a say arriving NOW is spoken with."""
        return SayParams(self.say_voice or self._voice, self.language, self.speed, self.style)

    def prefetch(self, texts: list[str]) -> None:
        self._synths.say(texts, self.params())

    def drop_prefetch(self) -> None:
        """Barge-in: the sentence playing and the look-ahead stop synthesizing now."""
        self._synths.drop()

    async def run_tts(self, text: str, context_id: str):
        synth = self._synths.begin(text, self.params())
        step = self.sample_rate // 5 * 2  # 200 ms per frame
        try:
            async with contextlib.aclosing(self._frames(synth, step, context_id)) as frames:
                async for frame in frames:
                    yield frame
        finally:
            # Interrupted (the generator closed early) or done: the active synthesis stops here,
            # not when its stream happens to end (#1626 review).
            if not synth.task.done():
                synth.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await synth.task
            self._synths.finished(synth)

    async def _frames(self, synth: _Synth, step: int, context_id: str):
        buf = b""
        while True:
            item = await synth.queue.get()
            if isinstance(item, se.SpeechError):
                await self._bus.send(
                    {"t": "error", "endpoint": "tts", "code": item.code, "detail": item.detail}
                )
                yield ErrorFrame(error=f"tts: {item.detail}")
                return
            if item is None:
                break
            buf += item
            while len(buf) >= step:
                yield TTSAudioRawFrame(buf[:step], self.sample_rate, 1, context_id=context_id)
                buf = buf[step:]
        if buf:
            yield TTSAudioRawFrame(buf, self.sample_rate, 1, context_id=context_id)


class Bridge(FrameProcessor):
    """Turns heard speech into `utterance` messages and `say` messages into speech."""

    def __init__(self, bus: Bus, tts: EndpointTTS) -> None:
        super().__init__()
        self._bus = bus
        self._tts = tts
        self._grace: asyncio.Task | None = None  # the newest turn's flush (each awaits the last)
        self.muted = False
        # Numbers each user turn: `user_started` and that turn's `utterance` carry it, so the app
        # can tell when an utterance BEGAN even though its transcript arrives later (#1397 P2).
        self._turn = 0

    async def _flush(self, target: int, turn: int) -> None:
        text = self._bus.turns.flush(target)
        if text and not self.muted:
            heard = self._bus.turns.flushed_language
            await self._bus.send(
                {"t": "utterance", "text": text, "turn": turn,
                 "language": self._detect(heard, text), "heard_language": heard}
            )  # fmt: skip

    def _detect(self, heard: str | None, text: str) -> str | None:
        """The call's language: only a turn heard in one language, of 3+ words, may change it.
        The turn's own language (`heard_language`, any length) gates spoken confirmation."""
        self._bus.detected = call_language(self._bus.detected, heard, text)
        return self._bus.detected

    async def _when_transcribed(
        self, target: int, previous: asyncio.Task | None, turn: int
    ) -> None:
        """Flush THIS turn (its segments up to `target`) once they are back from STT, or after a
        grace; turns go out in order, each with only its own words."""
        deadline = time.monotonic() + TRANSCRIPT_GRACE_S
        if previous is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await previous
        while not self._bus.turns.settled(target):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self._bus.segment_done.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._bus.segment_done.wait(), timeout=left)
        await self._flush(target, turn)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._bus.turns.segment_sent()
        if isinstance(frame, TranscriptionFrame):
            res = frame.result if isinstance(frame.result, dict) else {}
            lang = res.get("language") if isinstance(res.get("language"), str) else None
            late = self._bus.turns.transcript(frame.text, lang)
            self._bus.segment_done.set()
            if late and not self.muted:
                heard = self._bus.turns.late_language
                await self._bus.send(
                    {"t": "utterance", "text": late, "late": True,
                     "language": self._detect(heard, late), "heard_language": heard}
                )  # fmt: skip
            return  # transcripts go to the app, not to TTS
        if isinstance(frame, UserStartedSpeakingFrame):
            self._turn += 1
            await self._bus.send({"t": "user_started", "turn": self._turn})
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._bus.send({"t": "user_stopped"})
            self._grace = asyncio.create_task(
                self._when_transcribed(self._bus.turns.target(), self._grace, self._turn)
            )
        elif isinstance(frame, InterruptionFrame):
            self._tts.drop_prefetch()
            await self._bus.send({"t": "interrupt"})
        elif isinstance(frame, BotStartedSpeakingFrame):
            await self._bus.send({"t": "bot_started"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._bus.send({"t": "bot_stopped"})
        elif isinstance(frame, InputTransportMessageFrame):
            await self._on_app(frame.message)
            return
        await self.push_frame(frame, direction)

    async def _on_app(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "say":
            lang = msg.get("language")
            self._tts.language = lang if isinstance(lang, str) and len(lang) == 2 else None
            v = msg.get("voice")
            self._tts.say_voice = v if isinstance(v, str) and 0 < len(v) <= 120 else None
            chunks = split_speech(str(msg.get("text") or ""), first_max=FIRST_WORDS)
            self._tts.prefetch(chunks)
            for c in chunks:
                await self.push_frame(TTSSpeakFrame(c))
            # In order with the audio (a NON-urgent message is sent by the output transport after
            # the frames before it): the app learns this say has been played out. An interruption
            # drops it, so a read-back that was talked over never arms a confirmation (#1397 P2).
            await self.push_frame(
                OutputTransportMessageFrame(message={"t": "said", "id": msg.get("id")})
            )
        elif t == "mute":
            self.muted = bool(msg.get("on"))
        elif t == "stop":
            await self.push_frame(EndFrame())


async def run_call(websocket) -> None:
    raw = await websocket.receive_text()
    cfg = json.loads(raw)
    if cfg.get("t") != "config":
        await websocket.close(code=4400)
        return
    in_rate, out_rate = int(cfg["in_rate"]), int(cfg["out_rate"])
    bus = Bus()
    stt_ep = se.Endpoint(cfg["stt"]["base_url"], cfg["stt"].get("model", ""),
                         os.environ.get("VOICE_STT_TOKEN", ""))  # fmt: skip
    tts_ep = se.Endpoint(cfg["tts"]["base_url"], cfg["tts"].get("model", ""),
                         os.environ.get("VOICE_TTS_TOKEN", ""))  # fmt: skip
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=in_rate,
            audio_out_enabled=True,
            audio_out_sample_rate=out_rate,
            add_wav_header=False,
            serializer=WireSerializer(in_rate),
        ),
    )
    stt = EndpointSTT(stt_ep, bus, sample_rate=in_rate)
    tts = EndpointTTS(
        tts_ep,
        cfg["tts"]["voice"],
        bus,
        lookahead=int(cfg.get("lookahead", LOOKAHEAD)),
        sample_rate=out_rate,
    )
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=in_rate, params=VADParams(stop_secs=float(cfg.get("stop_secs", 0.35)))
        )
    )
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    turns = UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            start=[VADUserTurnStartStrategy(enable_interruptions=bool(cfg.get("interrupt", True)))],
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
        )
    )
    tts.style = cfg["tts"].get("style") or None
    sp = cfg["tts"].get("speed")
    tts.speed = float(sp) if isinstance(sp, int | float) and sp != 1.0 else None
    bus.detect_language = bool(cfg.get("detect_language"))
    bus.language_hint = cfg.get("language") or None
    tts.language = bus.language_hint if cfg.get("tts_language_ok") else None
    bridge = Bridge(bus, tts)
    pipeline = Pipeline([transport.input(), vad, stt, turns, bridge, tts, transport.output()])
    task = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=in_rate, audio_out_sample_rate=out_rate),
        idle_timeout_secs=None,
        enable_rtvi=False,
    )
    bus.task = task

    @transport.event_handler("on_client_connected")
    async def _connected(_t, _ws):
        await bus.send({"t": "ready"})

    @transport.event_handler("on_client_disconnected")
    async def _gone(_t, _ws):
        await task.queue_frame(CancelFrame())

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(task)
    await runner.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    args = ap.parse_args(argv)

    import uvicorn

    app = FastAPI()

    @app.websocket("/call")
    async def call(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await run_call(ws)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            # One call per worker: when it ends, so does the process.
            asyncio.get_running_loop().call_later(0.2, lambda: os._exit(0))

    os.umask(0o077)
    # Pipecat logs every frame at INFO/DEBUG; a call needs warnings only.
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("VOICE_WORKER_LOG", "WARNING"))
    uvicorn.run(app, uds=args.socket, log_level="warning", ws_max_size=256 * 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
