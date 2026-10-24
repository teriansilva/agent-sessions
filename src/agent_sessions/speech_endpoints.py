"""Speech endpoints for calling HQ (#1397): the ONE module that talks to STT and TTS servers.

Speech is configured like the HQ model: each side is an OpenAI-compatible endpoint, a base URL, an
optional token and a model. The same code serves a self-hosted server (faster-whisper, Speaches,
Kokoro-FastAPI, VoxCPM, …) and OpenAI. Nothing here is vendor-specific beyond what the contract
below allows a server to differ in.

**The contract** (the P0 ADR, ``docs/voice/adr-p0.md``):

- Transcription: ``POST <base>/audio/transcriptions``, multipart ``file`` (16-bit mono WAV) +
  ``model`` (+ ``language``, ``response_format=json``); the reply is ``{"text": …}``.
- Synthesis: ``POST <base>/audio/speech``, JSON ``{model, input, voice, response_format: "pcm"}``
  (+ ``speed``, ``language``). The reply is EITHER raw 16-bit mono PCM at 24 kHz (what OpenAI sends
  for ``pcm``) OR a WAV file (servers that ignore ``pcm``, e.g. VoxCPM's 48 kHz WAV). Which one is
  decided by the bytes (a ``RIFF`` header) and the content type, never assumed. A streamed reply is
  consumed as it arrives, so a server that streams gives earlier first audio.
- Discovery: ``GET <base>/models``; voices from ``GET <base>/audio/voices`` (Kokoro-FastAPI) or
  ``GET <base>/health`` (``voices``), else none: the operator enters the voice by hand.
- Token: optional. Sent only as ``Authorization: Bearer`` to the endpoint's own origin.

**Transport rules, the same as the AI endpoint** (``docs/invariants/ai-key-origin.md``): no ambient
proxies (``trust_env=False``), no redirects (a redirect could carry the token elsewhere), TLS
verified, and a host that resolves to a link-local, multicast or unspecified address (cloud
metadata services live there) is refused before any request. Loopback and private LAN addresses are
allowed on purpose: self-hosting is the point.

Every outbound call is in ``_request``, the single inventoried site
(``tests/test_prompts_registry.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import os
import re
import socket
import struct
import time
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

#: OpenAI's `pcm` format: 16-bit little-endian mono at this rate.
OPENAI_PCM_RATE = 24_000
#: Bounds on what a server may send back, so a misbehaving endpoint cannot exhaust memory.
TRANSCRIPT_MAX_BYTES = 256 * 1024
FEED_SLICE = 64 * 1024  # bytes resampled per step (bounded event-loop work)
SPEECH_MAX_BYTES = 48 * 1024 * 1024
LIST_MAX_BYTES = 512 * 1024
#: The sample rates and channel counts a speech reply may claim.
RATE_MIN, RATE_MAX = 8_000, 96_000
#: Bounds on what we send.
TEXT_MAX = 4_000
AUDIO_MAX_BYTES = 16 * 1024 * 1024

CONNECT_TIMEOUT_S = 5.0
TRANSCRIBE_TIMEOUT_S = 30.0
SYNTH_TIMEOUT_S = 60.0
LIST_TIMEOUT_S = 8.0

#: Test seam: tests mount an `httpx.MockTransport` here.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class SpeechError(Exception):
    """A speech endpoint call failed. `code` is stable and short (the UI names the failure from
    it); `detail` is safe to show (never the token, never the reply body beyond a bounded line)."""

    def __init__(self, code: str, detail: str, *, status: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Endpoint:
    """One side of the speech configuration, as the server resolved it (token included). Never
    serialized to a client; `public()` is the client view."""

    base_url: str
    model: str = ""
    api_key: str = ""

    @property
    def base(self) -> str:
        return self.base_url.strip().rstrip("/")

    def public(self) -> dict:
        return {"base_url": self.base_url, "model": self.model, "api_key_set": bool(self.api_key)}


# --- destination checks ------------------------------------------------------------------------


def classify_host(url: str) -> str:
    """`loopback` / `lan` / `remote` / `invalid` for the "where your call goes" panel. Literal
    addresses are classified directly; a name is classified by its usual suffixes only — this is a
    label, never a security decision (`_vet_destination` is)."""
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return "invalid"
    if not host:
        return "invalid"
    if host in ("localhost",) or host.endswith(".localhost"):
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.endswith((".lan", ".local", ".home", ".internal", ".home.arpa")) or "." not in host:
            return "lan"
        return "remote"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "lan"
    return "remote"


# Cloud metadata endpoints outside the link-local ranges (AWS IPv6, Alibaba's CGNAT address).
_METADATA = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254", "fd00:ec2::254",  # AWS (IPv4, IPv6)
        "100.100.100.200",  # Alibaba Cloud
        "fd20:ce::254",  # Google Cloud IPv6
        "fd00:c1::a9fe:a9fe",  # Oracle Cloud IPv6
    )
)  # fmt: skip
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Refused destinations: link-local (metadata), named metadata addresses, multicast,
    unspecified, reserved. Loopback is allowed on purpose (a local speech server), including
    `::1`, which `ipaddress` files under reserved `::/8`. An IPv4 address embedded in IPv6
    (mapped, 6to4, NAT64) is judged as itself."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            # Loopback reached through a translator (NAT64, 6to4) is the GATEWAY's loopback, not
            # ours: only the plain mapped form (::ffff:127.x) counts as this host.
            if embedded.is_loopback and ip.ipv4_mapped is None:
                return True
            return _unsafe_ip(embedded)
    if ip.is_loopback:
        return False
    return (
        ip in _METADATA or ip.is_link_local or ip.is_multicast or ip.is_unspecified
        or ip.is_reserved
    )  # fmt: skip


async def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. A seam of its own so tests can pin resolution whatever
    event loop runs them."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class _Pinned:
    """A vetted destination: the URL to CONNECT to (the vetted address), the Host header and the
    TLS server name (the configured hostname, so the certificate is still checked against it)."""

    url: str
    host_header: str
    sni: str | None


async def _vet_destination(url: str) -> _Pinned:
    """Resolve the URL's host ONCE, refuse it when any address is link-local (169.254/16,
    fe80::/10 — where cloud metadata services answer), multicast, unspecified or reserved, and
    pin the connection to a vetted address. Connecting by name again would resolve again, and a
    rebinding name could answer differently the second time (Hermes on #1621)."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise SpeechError("invalid_url", "the endpoint URL is not valid") from e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SpeechError("invalid_url", "the endpoint URL must be http(s) with a host")
    if parts.username or parts.password:
        raise SpeechError("invalid_url", "the endpoint URL must not carry credentials")
    host = parts.hostname
    try:
        addrs = await _resolve(host, port)
    except OSError as e:
        raise SpeechError("unreachable", f"{host} could not be resolved") from e
    vetted: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            raise SpeechError(
                "refused_destination", f"{host} resolves to an unreadable address"
            ) from None
        if _unsafe_ip(ip):
            raise SpeechError("refused_destination", f"{host} resolves to a refused address")
        vetted.append(ip)
    if not vetted:
        raise SpeechError("unreachable", f"{host} could not be resolved")
    ip = vetted[0]
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = f"{literal}:{port}"
    pinned = parts._replace(netloc=netloc).geturl()
    host_header = parts.netloc.rsplit("@", 1)[-1]
    return _Pinned(pinned, host_header, host if parts.scheme == "https" else None)


# --- the one outbound site ---------------------------------------------------------------------


def _client(timeout: float, uds: str | None = None) -> httpx.AsyncClient:
    transport = _TRANSPORT
    if transport is None and uds is not None:
        transport = httpx.AsyncHTTPTransport(uds=uds)  # the local server: its socket, never TCP
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S),
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    )


def _headers(ep: Endpoint) -> dict[str, str]:
    return {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}


def _status_error(status: int, where: str) -> SpeechError:
    if status in (401, 403):
        return SpeechError("rejected", f"{where} rejected the token ({status})", status=status)
    if status in (404, 405):
        return SpeechError("unsupported", f"{where} does not offer this ({status})", status=status)
    if 300 <= status < 400:
        return SpeechError(
            "redirect", f"{where} answered with a redirect, which is not followed", status=status
        )
    return SpeechError("failed", f"{where} answered {status}", status=status)


#: This machine's speech server (`agent-sessions voice serve`, #1397 P4) is named by its loopback
#: URL in the settings, but it is reached ONLY over its Unix socket in the 0700 voice dir: loopback
#: is no boundary between local users (another user could bind the TCP port while ours is down),
#: a socket in an owner-only directory is. Nothing is ever sent to that TCP port.
LOCAL_SERVER_PORT = 8766


def _is_local_server(ep: Endpoint) -> bool:
    u = urlsplit(ep.base)
    return u.hostname in ("127.0.0.1", "localhost", "::1") and u.port == LOCAL_SERVER_PORT


def local_socket() -> str:
    """The local speech server's socket: from the worker's environment, else the voice dir."""
    env = os.environ.get("AGENT_SESSIONS_SPEECH_SOCKET")
    if env:
        return env
    from . import voice_setup

    return str(voice_setup.socket_path())


@contextlib.asynccontextmanager
async def _request(
    ep: Endpoint,
    method: str,
    path: str,
    *,
    timeout: float,
    json: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
):
    """THE outbound call (inventoried). Yields the streaming response once its status is 2xx."""
    url = f"{ep.base}{path}"
    uds = local_socket() if _is_local_server(ep) else None
    dest = await _vet_destination(url)
    # identity only: a compressed reply would be inflated before any cap could see it.
    headers = {**_headers(ep), "Host": dest.host_header, "Accept-Encoding": "identity"}
    extensions = {"sni_hostname": dest.sni} if dest.sni else None
    try:
        async with _client(timeout, uds) as client:
            req = client.build_request(
                method,
                dest.url,
                json=json,
                data=data,
                files=files,
                headers=headers,
                extensions=extensions,
            )
            resp = await client.send(req, stream=True)
            try:
                if not 200 <= resp.status_code < 300:
                    raise _status_error(resp.status_code, urlsplit(url).netloc)
                if resp.headers.get("content-encoding", "identity").strip().lower() not in (
                    "", "identity"
                ):  # fmt: skip
                    raise SpeechError(
                        "unsupported_encoding",
                        f"{urlsplit(url).netloc} answered compressed; only identity is accepted",
                    )
                yield resp
            finally:
                await resp.aclose()
    except SpeechError:
        raise
    except httpx.TimeoutException as e:
        raise SpeechError("timeout", f"{urlsplit(url).netloc} did not answer in time") from e
    except httpx.HTTPError as e:
        raise SpeechError("unreachable", f"{urlsplit(url).netloc} could not be reached") from e


async def _read_bounded(resp: httpx.Response, cap: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            raise SpeechError("too_large", "the endpoint answered with more data than allowed")
    return bytes(buf)


# --- audio helpers -----------------------------------------------------------------------------


def pcm16_to_wav(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def parse_wav_header(head: bytes) -> tuple[int, int, int, int] | None:
    """`(sample_rate, channels, bits, data_offset)` from the start of a RIFF/WAVE stream, or None
    when `head` is not (yet) a complete header. Tolerates extra chunks before `data` and a
    streaming `data` size of 0/0xFFFFFFFF."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int] | None = None
    while pos + 8 <= len(head):
        cid = head[pos : pos + 4]
        size = struct.unpack("<I", head[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            if body + 16 > len(head):
                return None
            audio_format, channels, rate = struct.unpack("<HHI", head[body : body + 8])
            bits = struct.unpack("<H", head[body + 14 : body + 16])[0]
            if audio_format not in (1, 0xFFFE):
                raise SpeechError("unsupported_audio", "the endpoint returned non-PCM WAV")
            # Untrusted header fields: a zero or tiny rate would divide by zero or multiply the
            # audio without bound when resampled (Hermes on #1621).
            if not RATE_MIN <= rate <= RATE_MAX or not 1 <= channels <= 8 or bits != 16:
                raise SpeechError(
                    "unsupported_audio",
                    "the endpoint returned WAV with an unsupported rate, channel count or width",
                )
            fmt = (rate, channels, bits)
        elif cid == b"data":
            if fmt is None:
                raise SpeechError("unsupported_audio", "WAV data before its format")
            return (*fmt, body)
        pos = body + size + (size & 1)
    return None


def to_mono16(pcm: bytes, channels: int, bits: int) -> bytes:
    """Interleaved PCM → 16-bit mono (first channel). Only 16-bit input is accepted."""
    if bits != 16:
        raise SpeechError("unsupported_audio", f"{bits}-bit audio is not supported")
    if channels == 1:
        return pcm
    frame = 2 * channels
    usable = len(pcm) - len(pcm) % frame
    return b"".join(pcm[i : i + 2] for i in range(0, usable, frame))


class Resampler:
    """Linear-interpolation resampler for 16-bit mono PCM that keeps its PHASE across chunks:
    the fractional read position and the samples it still needs carry over, so a stream cut
    into chunks of any size produces the same audio as one buffer (Hermes on #1621). Good
    enough for speech; dependency-free."""

    def __init__(self, src: int, dst: int) -> None:
        if not (RATE_MIN <= src <= RATE_MAX and RATE_MIN <= dst <= RATE_MAX):
            raise SpeechError("unsupported_audio", "unsupported sample rate")
        self.src, self.dst = src, dst
        self.step = src / dst
        self.buf: list[int] = []
        self.t = 0.0  # next output position, in source samples from buf[0]

    def feed(self, pcm: bytes) -> bytes:
        if self.src == self.dst:
            return pcm
        n = len(pcm) // 2
        if n:
            self.buf.extend(struct.unpack(f"<{n}h", pcm[: n * 2]))
        buf, t, step, out = self.buf, self.t, self.step, []
        while t + 1 < len(buf):
            j = int(t)
            a = buf[j]
            out.append(int(a + (buf[j + 1] - a) * (t - j)))
            t += step
        # Drop what no later position needs — never more than was received: the read position
        # may already be past the buffer (downsampling), and those samples are still to come.
        keep = min(int(t), len(buf))
        self.buf = buf[keep:]
        self.t = t - keep
        return struct.pack(f"<{len(out)}h", *out) if out else b""

    def flush(self) -> bytes:
        """The tail: positions that land on the last sample."""
        if self.src == self.dst or not self.buf:
            return b""
        out = []
        while self.t < len(self.buf):
            out.append(self.buf[int(self.t)])
            self.t += self.step
        self.buf = []
        return struct.pack(f"<{len(out)}h", *out) if out else b""


def resample16(pcm: bytes, src: int, dst: int) -> bytes:
    """One buffer, resampled (see `Resampler`)."""
    if src == dst or not pcm:
        return pcm
    r = Resampler(src, dst)
    return r.feed(pcm) + r.flush()


# --- the calls ---------------------------------------------------------------------------------


@dataclass
class Transcript:
    text: str
    language: str | None
    seconds: float
    #: The reply was a JSON object (not a `response_format=text` body or an error page).
    structured: bool = True
    #: The reply carried a non-empty `language` — even one `language_code` cannot map.
    reported_language: bool = False
    #: The reply had a `language` KEY at all (even empty or null): only its ABSENCE shows an
    #: endpoint that does not report languages.
    language_key: bool = False


async def transcribe(
    ep: Endpoint,
    pcm: bytes,
    rate: int,
    *,
    language: str | None = None,
    prompt: str | None = None,
    detect: bool = False,
) -> Transcript:
    """Transcribe one utterance of 16-bit mono PCM."""
    if not pcm:
        return Transcript("", None, 0.0)
    wav = pcm16_to_wav(pcm, rate)
    if len(wav) > AUDIO_MAX_BYTES:
        raise SpeechError("too_large", "the utterance is too long to transcribe")
    # `verbose_json` carries the detected language (#1397 P3); asked for only when the endpoint was
    # found to support it, so a strict server never gets a format it refuses.
    data = {
        "model": ep.model or "whisper-1",
        "response_format": "verbose_json" if detect else "json",
    }
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:400]
    started = time.monotonic()
    async with _request(
        ep,
        "POST",
        "/audio/transcriptions",
        timeout=TRANSCRIBE_TIMEOUT_S,
        data=data,
        files={"file": ("utterance.wav", wav, "audio/wav")},
    ) as resp:
        body = await _read_bounded(resp, TRANSCRIPT_MAX_BYTES)
    seconds = time.monotonic() - started
    try:
        import json as _json

        parsed = _json.loads(body)
        text = parsed.get("text") if isinstance(parsed, dict) else None
        lang = parsed.get("language") if isinstance(parsed, dict) else None
        structured = isinstance(parsed, dict)
        reported = structured and isinstance(lang, str) and bool(lang.strip())
        keyed = structured and "language" in parsed
    except ValueError:
        text, lang = body.decode("utf-8", "replace"), None  # response_format=text servers
        structured = reported = keyed = False
    if not isinstance(text, str):
        raise SpeechError("malformed", "the transcription reply has no text")
    return Transcript(text.strip(), language_code(lang), seconds, structured, reported, keyed)


# Whisper's language names (what OpenAI's `verbose_json` reports, e.g. "english"), to ISO 639-1.
_WHISPER_LANGUAGES = {
    "afrikaans": "af", "albanian": "sq", "amharic": "am", "arabic": "ar", "armenian": "hy",
    "assamese": "as", "azerbaijani": "az", "bashkir": "ba", "basque": "eu", "belarusian": "be",
    "bengali": "bn", "bosnian": "bs", "breton": "br", "bulgarian": "bg", "catalan": "ca",
    "chinese": "zh", "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl",
    "english": "en", "estonian": "et", "faroese": "fo", "finnish": "fi", "french": "fr",
    "galician": "gl", "georgian": "ka", "german": "de", "greek": "el", "gujarati": "gu",
    "haitian creole": "ht", "hausa": "ha", "hawaiian": "haw", "hebrew": "he", "hindi": "hi",
    "hungarian": "hu", "icelandic": "is", "indonesian": "id", "italian": "it", "japanese": "ja",
    "javanese": "jw", "kannada": "kn", "kazakh": "kk", "khmer": "km", "korean": "ko", "lao": "lo",
    "latin": "la", "latvian": "lv", "lingala": "ln", "lithuanian": "lt", "luxembourgish": "lb",
    "macedonian": "mk", "malagasy": "mg", "malay": "ms", "malayalam": "ml", "maltese": "mt",
    "maori": "mi", "marathi": "mr", "mongolian": "mn", "myanmar": "my", "nepali": "ne",
    "norwegian": "no", "nynorsk": "nn", "occitan": "oc", "pashto": "ps", "persian": "fa",
    "polish": "pl", "portuguese": "pt", "punjabi": "pa", "romanian": "ro", "russian": "ru",
    "sanskrit": "sa", "serbian": "sr", "shona": "sn", "sindhi": "sd", "sinhala": "si",
    "slovak": "sk", "slovenian": "sl", "somali": "so", "spanish": "es", "sundanese": "su",
    "swahili": "sw", "swedish": "sv", "tagalog": "tl", "tajik": "tg", "tamil": "ta", "tatar": "tt",
    "telugu": "te", "thai": "th", "tibetan": "bo", "turkish": "tr", "turkmen": "tk",
    "ukrainian": "uk", "urdu": "ur", "uzbek": "uz", "vietnamese": "vi", "welsh": "cy",
    "yiddish": "yi", "yoruba": "yo", "cantonese": "yue",
}  # fmt: skip


def language_code(value: object) -> str | None:
    """A reported language as a two-letter code, or None (unknown: never guessed)."""
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[a-z]{2}", value):
        return value
    code = _WHISPER_LANGUAGES.get(value.strip().lower())
    return code if code is not None and re.fullmatch(r"[a-z]{2}", code) else None


async def synthesize(
    ep: Endpoint,
    text: str,
    *,
    voice: str,
    out_rate: int,
    speed: float | None = None,
    language: str | None = None,
    extra: dict | None = None,
    plain_fallback: Callable[[], None] | None = None,
) -> AsyncIterator[bytes]:
    """Synthesize `text` and yield 16-bit mono PCM at `out_rate`, chunk by chunk as the endpoint
    delivers it. Raw PCM (OpenAI `pcm`) and WAV replies are both handled; anything else fails.

    `plain_fallback` (#1635 review): a server that REFUSES the request (400/422, before any audio)
    while optional fields were sent — `speed`, `language`, `extra` — is asked once more without
    them, and the callback is told so the caller stops sending them. Never for a conformance
    probe, which must see the refusal."""
    optional = speed is not None or bool(language) or bool(extra)
    got = False
    try:
        async with contextlib.aclosing(
            _synthesize(ep, text, voice=voice, out_rate=out_rate, speed=speed,
                        language=language, extra=extra)
        ) as chunks:  # fmt: skip
            async for chunk in chunks:
                got = True
                yield chunk
    except SpeechError as e:
        if got or not optional or plain_fallback is None or e.status not in (400, 422):
            raise
        plain_fallback()
        async with contextlib.aclosing(
            _synthesize(ep, text, voice=voice, out_rate=out_rate)
        ) as chunks:
            async for chunk in chunks:
                yield chunk


async def _synthesize(
    ep: Endpoint,
    text: str,
    *,
    voice: str,
    out_rate: int,
    speed: float | None = None,
    language: str | None = None,
    extra: dict | None = None,
) -> AsyncIterator[bytes]:
    text = text.strip()[:TEXT_MAX]
    if not text:
        return
    body: dict = {
        "model": ep.model or "tts-1",
        "input": text,
        "voice": voice,
        "response_format": "pcm",
    }
    if speed is not None:
        body["speed"] = speed
    if language:
        body["language"] = language
    if extra:
        body.update({k: v for k, v in extra.items() if k not in body})
    async with _request(ep, "POST", "/audio/speech", timeout=SYNTH_TIMEOUT_S, json=body) as resp:
        ctype = resp.headers.get("content-type", "").lower()
        head = bytearray()
        fmt: tuple[int, int, int] | None = None  # rate, channels, bits
        total = 0
        produced = 0
        carry = b""
        resampler: Resampler | None = None
        remaining: int | None = None  # bytes of WAV `data` still to play (None: to the end)
        after = b""  # bytes past a sized `data` chunk, held until they are understood

        def decide(head: bytes, final: bool) -> tuple[tuple[int, int, int], bytes] | None:
            """The stream's format and its first audio bytes, once enough has arrived to tell a
            WAV header from raw PCM (a short first read must not be taken for raw audio)."""
            nonlocal remaining
            if head[:4] == b"RIFF" or "wav" in ctype:
                parsed = parse_wav_header(head)
                if parsed is None:
                    if len(head) > 64 * 1024 or final:
                        raise SpeechError("unsupported_audio", "unreadable WAV header")
                    return None
                rate, channels, bits, off = parsed
                size = struct.unpack("<I", head[off - 4 : off])[0]
                remaining = None if size in (0, 0xFFFFFFFF) else size
                return (rate, channels, bits), head[off:]
            if len(head) < 12 and not final and b"RIFF".startswith(head[:4]):
                return None  # could still become a RIFF header
            if any(t in ctype for t in ("mpeg", "mp3", "ogg", "opus", "aac", "flac")):
                raise SpeechError("unsupported_audio", f"the endpoint returned {ctype}")
            return (OPENAI_PCM_RATE, 1, 16), head

        def produce(chunk: bytes) -> list[bytes]:
            nonlocal carry, produced, remaining, resampler, after
            if remaining is not None:
                after += chunk[remaining:]
                chunk = chunk[:remaining]
                remaining -= len(chunk)
            if resampler is None:
                resampler = Resampler(fmt[0], out_rate)
            data = carry + chunk
            frame = 2 * fmt[1]
            cut = len(data) - len(data) % frame
            carry = data[cut:]
            outs = []
            # Bounded work per step: never resample more than one slice before checking the cap.
            step = FEED_SLICE - FEED_SLICE % frame  # whole frames only (3, 5, 6, 7 channels)
            for i in range(0, cut, step):
                out = resampler.feed(to_mono16(data[i : min(i + step, cut)], fmt[1], fmt[2]))
                produced += len(out)
                if produced > SPEECH_MAX_BYTES:  # bounded AFTER resampling, too
                    raise SpeechError("too_large", "the endpoint sent more audio than allowed")
                if out:
                    outs.append(out)
            return outs

        def drain(final: bool) -> tuple[list[bytes], bool]:
            """What follows a sized WAV `data` chunk, as `(audio, stop)`:
            * another `RIFF` header (a server sending one WAV per sentence) plays on, in the same
              format, or stops if the format changed;
            * a metadata chunk (an ASCII id such as `LIST`) ends the audio;
            * anything else means the header understated the length (Python's `wave` writing to
              a pipe does), so it plays to the end."""
            nonlocal after, remaining
            outs: list[bytes] = []
            while remaining == 0 and after:
                buf = bytes(after)
                if len(buf) < 8 and not final:
                    return outs, False  # a chunk id and its size first, then judge
                if buf[:4] == b"RIFF":
                    parsed = parse_wav_header(buf)
                    if parsed is None:
                        if final or len(buf) > 64 * 1024:
                            return outs, True
                        return outs, False  # wait for the rest of the header
                    rate, channels, bits, off = parsed
                    if (rate, channels, bits) != fmt:
                        return outs, True
                    size = struct.unpack("<I", buf[off - 4 : off])[0]
                    after, remaining = b"", None if size in (0, 0xFFFFFFFF) else size
                    outs += produce(buf[off:])
                    continue
                if len(buf) >= 4 and all(0x20 <= c < 0x7F for c in buf[:4]):
                    return outs, True  # a metadata chunk: the audio is over
                after, remaining = b"", None  # an understated header: play to the end
                outs += produce(buf)
            return outs, False

        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > SPEECH_MAX_BYTES:
                raise SpeechError("too_large", "the endpoint sent more audio than allowed")
            if fmt is None:
                head += chunk
                got = decide(bytes(head), final=False)
                if got is None:
                    continue
                fmt, chunk = got
            elif remaining == 0:
                after += chunk
                chunk = b""
            for out in produce(chunk) if chunk else []:
                yield out
                await asyncio.sleep(0)
            outs, stop = drain(final=False)
            for out in outs:
                yield out
            if stop:
                break
        else:
            if fmt is None and head:
                fmt, first = decide(bytes(head), final=True)
                for out in produce(first):
                    yield out
            outs, _ = drain(final=True)
            for out in outs:
                yield out
        if resampler is not None:
            tail = resampler.flush()
            if tail:
                produced += len(tail)
                if produced > SPEECH_MAX_BYTES:  # the flushed tail counts against the cap too
                    raise SpeechError("too_large", "the endpoint sent more audio than allowed")
                yield tail


async def list_models(ep: Endpoint) -> list[str]:
    async with _request(ep, "GET", "/models", timeout=LIST_TIMEOUT_S) as resp:
        body = await _read_bounded(resp, LIST_MAX_BYTES)
    import json as _json

    try:
        parsed = _json.loads(body)
    except ValueError as e:
        raise SpeechError("malformed", "the model list is not JSON") from e
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        raise SpeechError("malformed", "the model list has no data")
    return [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)][:200]


async def list_voices(ep: Endpoint) -> list[str] | None:
    """The TTS endpoint's voices, or None when it publishes no list (enter the voice by hand)."""
    return (await discover(ep))["voices"]


def _names(items: object) -> list[str] | None:
    if not isinstance(items, list):
        return None
    names = [
        v if isinstance(v, str) else v.get("id") or v.get("name")
        for v in items
        if isinstance(v, str) or isinstance(v, dict)
    ]
    return [n for n in names if isinstance(n, str) and len(n) <= 120][:200]


def _languages(value: object) -> list[str] | None:
    """A server's languages as ISO 639-1 codes: a list, or a string such as "en,de + 28 others"
    (only the codes it names are taken)."""
    if isinstance(value, list):
        out = [v for v in value if isinstance(v, str)]
    elif isinstance(value, str):
        out = re.findall(r"\b([a-z]{2})\b", value.split("(")[0])
    else:
        return None
    return [c for c in dict.fromkeys(out) if re.fullmatch(r"[a-z]{2}", c)][:100] or None


async def discover(ep: Endpoint) -> dict:
    """What a TTS endpoint publishes about itself: `{voices, styles, languages}`, each a list or
    None (not published — enter by hand, and never send that field)."""
    import json as _json

    out: dict = {"voices": None, "styles": None, "languages": None}
    for path in ("/audio/voices", "/health"):
        try:
            async with _request(ep, "GET", path, timeout=LIST_TIMEOUT_S) as resp:
                body = await _read_bounded(resp, LIST_MAX_BYTES)
        except SpeechError as e:
            if e.code in ("unsupported", "failed") or any(v is not None for v in out.values()):
                continue  # not offered — or a second, optional lookup failed: keep what was found
            raise
        try:
            parsed = _json.loads(body)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        out["voices"] = out["voices"] or _names(parsed.get("voices"))
        out["styles"] = out["styles"] or _names(parsed.get("styles"))
        out["languages"] = out["languages"] or _languages(parsed.get("languages"))
    return out
