"""BattleLab's local speech server (#1397 P4): OpenAI-compatible STT + TTS on this machine.

`agent-sessions voice setup` copies this file into its own venv (`$PREFIX/voice/server/`) and
runs it as the `agent-sessions-speech` user service on a Unix socket in the 0700 voice dir — never
a TCP port: loopback is no boundary between local users, an owner-only directory is. It
deliberately imports NOTHING from the app: it runs under a different interpreter with different
packages.

It serves exactly what `speech_endpoints` speaks, and no more:

* `GET  /v1/models`, `GET /v1/audio/voices`, `GET /health` (voices + languages);
* `POST /v1/audio/transcriptions` — faster-whisper; `json` | `verbose_json` (+ language) | `text`;
* `POST /v1/audio/speech` — Kokoro; `pcm` (24 kHz s16le, streamed per sentence) | `wav`.

Every route needs `Authorization: Bearer <token>` (the token file is 0600; compared in constant
time). One request runs at a time per model; uploads, text and each synthesis's OUTPUT (bytes and
time) are bounded, and a reader that stops reading releases the model (`bounded_stream`). Errors
are short codes, never tracebacks.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import io
import os
import socket
import struct
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

OUTPUT_MAX = 32 * 1024 * 1024  # bytes of one synthesis (~11 min of 24 kHz s16 mono)
SYNTH_MAX_S = 180.0  # one synthesis, start to end
STALL_S = 10.0  # a reader not taking audio for this long loses the model
CLEANUP_S = 2.0  # a generator's close/cancel, at most; then the model is released anyway
UPLOAD_MAX = 10 * 1024 * 1024  # bytes of one transcription request
TEXT_MAX = 4000  # characters of one synthesis request
TTS_RATE = 24_000
STT_MODEL_ID = "faster-whisper"
TTS_MODEL_ID = "kokoro-v1.0"

# ISO 639-1 → Kokoro's language codes. A voice's first letter is its native language.
KOKORO_LANGS = {
    "en": "en-us", "es": "es", "fr": "fr-fr", "hi": "hi", "it": "it", "pt": "pt-br",
    "ja": "ja", "zh": "cmn",
}  # fmt: skip
VOICE_PREFIX_LANG = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "j": "ja",
    "p": "pt-br", "z": "cmn",
}  # fmt: skip


def kokoro_lang(voice: str, language: str | None) -> str | None:
    """The language Kokoro speaks: the requested one, or the voice's own when none was asked
    for. English stays British for a British voice. ``None``: a language Kokoro cannot speak —
    refused, never read with another language's sounds (#1716: German came out as English
    phonemes)."""
    native = VOICE_PREFIX_LANG.get(voice[:1], "en-us")
    if language is None:
        return native
    if language in KOKORO_LANGS:
        if language == "en" and native.startswith("en"):
            return native
        return KOKORO_LANGS[language]
    return None


def wav_header(n_bytes: int, rate: int = TTS_RATE) -> bytes:
    return b"RIFF" + struct.pack("<I", 36 + n_bytes) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16
    ) + b"data" + struct.pack("<I", n_bytes)  # fmt: skip


class Whisper:
    """faster-whisper on CPU, loaded once."""

    def __init__(self, model_dir: Path, threads: int):
        from faster_whisper import WhisperModel

        self._m = WhisperModel(str(model_dir), device="cpu", compute_type="int8",
                               cpu_threads=threads)  # fmt: skip

    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, str | None, float]:
        segments, info = self._m.transcribe(
            io.BytesIO(audio), language=language, beam_size=1, condition_on_previous_text=False
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language, float(info.duration)


class Kokoro:
    """Kokoro v1.0 (ONNX, int8) on CPU, loaded once."""

    def __init__(self, model: Path, voices: Path):
        from kokoro_onnx import Kokoro as _K

        self._k = _K(str(model), str(voices))
        self.voices = sorted(self._k.get_voices())
        self.languages = sorted(KOKORO_LANGS)

    async def stream(self, text: str, voice: str, speed: float, lang: str) -> AsyncIterator[bytes]:
        import numpy as np

        async for samples, _rate in self._k.create_stream(
            text, voice=voice, speed=speed, lang=lang
        ):
            yield (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)


def create_app(token: str, stt, tts) -> FastAPI:
    """`stt` has `transcribe(bytes, language) -> (text, language, duration)`; `tts` has `voices`,
    `languages` and `stream(text, voice, speed, lang)`. Both are injected so tests run without
    models."""
    if len(token) < 16:
        raise ValueError("token too short")
    want = token.encode()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    stt_lock, tts_lock = asyncio.Lock(), asyncio.Lock()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        got = request.headers.get("authorization", "")
        scheme, _, value = got.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), want):
            return _err(401, "unauthorized")
        if request.method == "POST":
            try:
                length = int(request.headers.get("content-length", ""))
            except ValueError:
                return _err(411, "length_required")
            if length > UPLOAD_MAX + 64 * 1024:
                return _err(413, "too_large")
        return await call_next(request)

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": STT_MODEL_ID, "object": "model", "owned_by": "battlelab"},
            {"id": TTS_MODEL_ID, "object": "model", "owned_by": "battlelab"},
        ]}  # fmt: skip

    @app.get("/v1/audio/voices")
    async def voices():
        return {"voices": tts.voices}

    @app.get("/health")
    async def health():
        return {"status": "ok", "voices": tts.voices, "languages": tts.languages}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        form = await request.form(max_files=1, max_fields=8)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            return _err(400, "no_file")
        audio = await upload.read(UPLOAD_MAX + 1)
        if len(audio) > UPLOAD_MAX:
            return _err(413, "too_large")
        fmt = str(form.get("response_format") or "json")
        if fmt not in ("json", "verbose_json", "text"):
            return _err(400, "unsupported_format")
        lang = form.get("language")
        lang = lang if isinstance(lang, str) and len(lang) == 2 and lang.isalpha() else None
        async with stt_lock:
            try:
                text, detected, duration = await asyncio.to_thread(stt.transcribe, audio, lang)
            except Exception:  # noqa: BLE001 - a bad upload is the caller's, not a crash
                return _err(400, "undecodable")
        if fmt == "text":
            return PlainTextResponse(text)
        if fmt == "verbose_json":
            return {"task": "transcribe", "language": detected, "duration": duration,
                    "text": text, "segments": []}  # fmt: skip
        return {"text": text}

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _err(400, "malformed")
        if not isinstance(body, dict):
            return _err(400, "malformed")
        text = body.get("input")
        voice = body.get("voice")
        if not isinstance(text, str) or not text.strip():
            return _err(400, "no_input")
        if len(text) > TEXT_MAX:
            return _err(413, "too_long")
        if voice not in tts.voices:
            return _err(400, "unknown_voice")
        speed = body.get("speed", 1.0)
        if not isinstance(speed, int | float) or not 0.5 <= speed <= 2.0:
            return _err(400, "bad_speed")
        fmt = body.get("response_format", "pcm")
        if fmt not in ("pcm", "wav"):
            return _err(400, "unsupported_format")
        language = body.get("language")
        if language is not None and not isinstance(language, str):
            # A malformed language is refused, never taken as "none asked for" (Hermes 7200):
            # that would speak the text in the voice's own language. JSON `null` IS "none asked
            # for", as an absent field is.
            return _err(400, "bad_language")
        lang = kokoro_lang(voice, language)
        if lang is None:
            return _err(400, "unsupported_language")  # the caller must not retry without it

        stream = bounded_stream(
            tts_lock, lambda: tts.stream(text.strip(), voice, float(speed), lang)
        )
        # The first chunk BEFORE the response starts: a synthesis that fails or overruns early is
        # an error status, never a 200 with empty or partial audio.
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = b""
        except SynthesisLimit:
            return _err(413, "output_too_large")
        except Exception:  # noqa: BLE001 — a short code, never a traceback
            return _err(502, "synthesis_failed")
        if fmt == "pcm":
            return StreamingResponse(pcm_body(first, stream), media_type="audio/pcm",
                                     headers={"X-Sample-Rate": str(TTS_RATE)})  # fmt: skip
        data = bytearray(first)
        try:
            async for chunk in stream:
                data += chunk
        except SynthesisLimit:
            return _err(413, "output_too_large")
        except Exception:  # noqa: BLE001
            return _err(502, "synthesis_failed")
        return Response(wav_header(len(data)) + bytes(data), media_type="audio/wav")

    return app


async def pcm_body(first: bytes, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """A PCM response body. EVERY yield is inside the cleanup scope, so a reader gone even after
    the first chunk releases the model at once. A later failure or bound RAISES here: the
    connection is aborted, so the client sees an incomplete stream, never truncated audio marked
    complete."""
    try:
        if first:
            yield first
        async for chunk in stream:
            yield chunk
    finally:
        await stream.aclose()


def _exit_for_restart() -> None:
    """A synthesis that won't stop holds the only model: two at once could corrupt it, and nothing
    in-process can stop it. Exit so the service manager (`Restart=on-failure`) starts us clean."""
    print("speech server: a synthesis would not stop; exiting to be restarted", file=sys.stderr)
    os._exit(75)


#: Called once a synthesis is stuck (tests replace it). The model is NEVER released to another
#: synthesis while stuck work may still be running.
ON_STUCK = _exit_for_restart


class SynthesisLimit(Exception):
    """A synthesis ran past its output or time bound: never passed off as complete audio."""


async def bounded_stream(
    lock: asyncio.Lock,
    source,
    *,
    out_max: int | None = None,
    total_s: float | None = None,
    stall_s: float | None = None,
) -> AsyncIterator[bytes]:
    """`source()`'s chunks, produced under a LEASE on `lock` into a small buffer, within `out_max`
    bytes and `total_s` seconds in all. A failure or a bound reached RAISES to the reader
    (`SynthesisLimit` for a bound): a truncated synthesis is never ended as if complete.

    The reader is never held past the bounds: each next chunk is awaited for only the time left
    (`asyncio.wait`, which never waits on a cancellation), cleanup is bounded by `CLEANUP_S`, a
    reader that takes nothing for `stall_s` ends the producer, and a watchdog ends the reader at
    `total_s + CLEANUP_S`. The MODEL is released only once its work has really stopped: model work
    that won't stop is never overlapped by the next synthesis — the lease stays held and
    `ON_STUCK` runs (the server exits and its service manager restarts it)."""
    out_max = OUTPUT_MAX if out_max is None else out_max
    total_s = SYNTH_MAX_S if total_s is None else total_s
    stall_s = STALL_S if stall_s is None else stall_s
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    lease = {"held": False, "expired": False, "stuck": False}
    pending: set[asyncio.Future] = set()  # model work started and not yet finished
    acquired = asyncio.Event()  # the clock starts at the lease, not while queued behind another

    def release() -> None:
        if lease["held"] and not lease["stuck"]:
            lease["held"] = False
            lock.release()  # an asyncio.Lock may be released by any task

    def stuck() -> None:
        if not lease["stuck"]:
            lease["stuck"] = True
            ON_STUCK()

    async def bounded(aw, timeout: float, *, model: bool = False):
        """`aw`'s result within `timeout`, else TimeoutError — without waiting on its cancel.
        `model`: a step of the model's own work, tracked until it has really finished."""
        fut = asyncio.ensure_future(aw)
        if model:
            pending.add(fut)
            fut.add_done_callback(pending.discard)
        done, _ = await asyncio.wait({fut}, timeout=timeout)
        if not done:
            fut.cancel()
            raise TimeoutError
        return fut.result()

    async def produce() -> None:
        await lock.acquire()
        lease["held"] = True
        acquired.set()
        deadline = loop.time() + total_s
        gen = source()
        sent = 0
        try:
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    raise SynthesisLimit("synthesis took too long")
                try:
                    chunk = await bounded(gen.__anext__(), left, model=True)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    raise SynthesisLimit("synthesis took too long") from None
                sent += len(chunk)
                if sent > out_max:
                    raise SynthesisLimit("synthesis output too large")
                await bounded(q.put(chunk), stall_s)  # TimeoutError: a stalled reader
        finally:
            running = lambda: [f for f in pending if not f.done()]  # noqa: E731
            try:
                # A cancelled step finishes on its next turn: give model work CLEANUP_S to stop.
                if running():
                    await asyncio.wait(running(), timeout=CLEANUP_S)
                aclose = getattr(gen, "aclose", None)
                if aclose is not None and not running():
                    await bounded(aclose(), CLEANUP_S, model=True)
            except BaseException:  # noqa: BLE001, S110 — cancelled or failed meanwhile: decide anyway
                pass
            if running():
                stuck()  # its work is still running: the model is NOT handed to anyone else
            else:
                release()

    async def watchdog() -> None:
        await acquired.wait()
        await asyncio.sleep(total_s + CLEANUP_S)
        if not task.done():
            lease["expired"] = True  # the reader is told now
            task.cancel()
            await asyncio.wait({task}, timeout=CLEANUP_S)
            if not task.done() or any(not f.done() for f in pending):
                stuck()

    task = asyncio.create_task(produce())
    dog = asyncio.create_task(watchdog())
    try:
        while True:
            if lease["expired"]:
                raise SynthesisLimit("synthesis took too long")
            if task.done() and q.empty():
                exc = None if task.cancelled() else task.exception()
                if exc is not None:
                    raise exc
                return
            try:
                yield await bounded(q.get(), 0.5)
            except TimeoutError:
                continue
    finally:
        dog.cancel()
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait({task}, timeout=CLEANUP_S)
        if task.done() and not any(not f.done() for f in pending):
            release()  # (the producer normally did already)
        elif lease["held"]:
            stuck()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="speech_server")
    p.add_argument("--uds", required=True, type=Path, help="the Unix socket to listen on")
    p.add_argument("--models", required=True, type=Path)
    p.add_argument("--stt", default="base")
    p.add_argument("--threads", type=int, default=2)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 2  # e.g. `--host`: there is no TCP listener to ask for
    parent = a.uds.parent
    try:
        if parent.is_symlink() or (parent.stat().st_mode & 0o077):
            print("the socket's directory must be owner-only (0700)", file=sys.stderr)
            return 2
    except OSError:
        print("the socket's directory does not exist", file=sys.stderr)
        return 2
    token = os.environ.get("SPEECH_SERVER_TOKEN", "")
    if len(token) < 16:
        print("SPEECH_SERVER_TOKEN missing", file=sys.stderr)
        return 2
    import uvicorn

    stt = Whisper(a.models / f"faster-whisper-{a.stt}", a.threads)
    tts = Kokoro(a.models / "kokoro-v1.0.int8.onnx", a.models / "voices-v1.0.bin")
    if a.uds.is_socket():
        a.uds.unlink()  # a stale socket from an earlier run (the dir is ours alone)
    # Bound here, owner-only (uvicorn's own `uds=` would chmod it 0666), and handed over by fd.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(a.uds))
    finally:
        os.umask(old)
    os.chmod(a.uds, 0o600)
    uvicorn.run(create_app(token, stt, tts), fd=sock.fileno(), log_level="warning",
                access_log=False)  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
