"""#1397 P0 — the speech endpoint client and the compatibility check.

Every request goes through `speech_endpoints._request`: the token only as a Bearer header to the
endpoint itself, no redirects followed, no ambient proxy, and no link-local / metadata
destinations. Synthesis accepts raw PCM (OpenAI's `pcm`) and WAV replies (servers that ignore
`pcm`), decided by the bytes, and resamples to the rate the call asks for.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import wave

import httpx
import pytest

from agent_sessions import speech_conformance as sc
from agent_sessions import speech_endpoints as se

BASE = "http://speech.lan:8000/v1"


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    """Resolve every host to a LAN address unless a test says otherwise."""

    async def fake_resolve(host, port):
        return [{"meta.test": "169.254.169.254", "v6ll.test": "fe80::1"}.get(host, "10.0.0.5")]

    monkeypatch.setattr(se, "_resolve", fake_resolve)


def _tone(seconds: float, rate: int) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *((i * 37) % 2000 - 1000 for i in range(n)))


def _wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def _mount(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(se, "_TRANSPORT", httpx.MockTransport(h))
    return seen


def run(coro):
    return asyncio.run(coro)


async def _collect(it) -> bytes:
    buf = bytearray()
    async for c in it:
        buf += c
    return bytes(buf)


# --- transport rules ---------------------------------------------------------------------------


def test_token_is_a_bearer_header_only_when_set(monkeypatch):
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    run(se.list_models(se.Endpoint(BASE, "m", "tok-123")))
    run(se.list_models(se.Endpoint(BASE, "m", "")))
    assert seen[0].headers["authorization"] == "Bearer tok-123"
    assert "authorization" not in seen[1].headers
    assert "tok-123" not in str(seen[0].url)


def test_redirects_are_not_followed(monkeypatch):
    seen = _mount(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"location": "http://evil.example/steal"}),
    )
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint(BASE, "m", "tok")))
    assert e.value.code == "redirect"
    assert len(seen) == 1


@pytest.mark.parametrize("url", ["http://meta.test/v1", "http://v6ll.test/v1"])
def test_link_local_destinations_are_refused_before_any_request(monkeypatch, url):
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint(url, "m", "tok")))
    assert e.value.code == "refused_destination"
    assert seen == []


def test_credentials_in_the_url_are_refused(monkeypatch):
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint("http://u:p@speech.lan/v1")))
    assert e.value.code == "invalid_url" and seen == []


def test_ambient_proxy_is_ignored():
    client = se._client(1.0)
    try:
        assert client._trust_env is False
        assert client.follow_redirects is False
    finally:
        run(client.aclose())


@pytest.mark.parametrize(
    "status,code", [(401, "rejected"), (403, "rejected"), (404, "unsupported"), (500, "failed")]
)
def test_status_codes_map_to_stable_codes(monkeypatch, status, code):
    _mount(monkeypatch, lambda r: httpx.Response(status, text="secret body"))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint(BASE, "m", "tok")))
    assert e.value.code == code
    assert "secret body" not in e.value.detail


# --- classify ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,kind",
    [
        ("http://localhost:8000/v1", "loopback"),
        ("http://127.0.0.1:8000/v1", "loopback"),
        ("http://10.1.2.17:8771/v1", "lan"),
        ("http://gpu-1.lan:8000/v1", "lan"),
        ("https://api.openai.com/v1", "remote"),
        ("http://8.8.8.8/v1", "remote"),
        ("nonsense", "invalid"),
    ],
)
def test_classify_host(url, kind):
    assert se.classify_host(url) == kind


# --- transcription -----------------------------------------------------------------------------


def test_transcribe_sends_a_wav_and_reads_text(monkeypatch):
    def handler(r: httpx.Request):
        assert r.url.path == "/v1/audio/transcriptions"
        body = r.content
        assert b'name="model"' in body and b"whisper-x" in body
        assert b"RIFF" in body
        return httpx.Response(200, json={"text": " Hello Commander. "})

    _mount(monkeypatch, handler)
    tr = run(se.transcribe(se.Endpoint(BASE, "whisper-x"), _tone(0.5, 16000), 16000))
    assert tr.text == "Hello Commander."


def test_transcribe_bounds_the_reply(monkeypatch):
    _mount(
        monkeypatch, lambda r: httpx.Response(200, content=b"x" * (se.TRANSCRIPT_MAX_BYTES + 10))
    )
    with pytest.raises(se.SpeechError) as e:
        run(se.transcribe(se.Endpoint(BASE), _tone(0.2, 16000), 16000))
    assert e.value.code == "too_large"


# --- synthesis ---------------------------------------------------------------------------------


def test_synthesize_raw_pcm_is_openai_rate(monkeypatch):
    pcm = _tone(1.0, se.OPENAI_PCM_RATE)

    def handler(r: httpx.Request):
        body = json.loads(r.content)
        assert body["response_format"] == "pcm" and body["voice"] == "nova"
        return httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})

    _mount(monkeypatch, handler)
    out = run(_collect(se.synthesize(se.Endpoint(BASE, "tts"), "Hi", voice="nova", out_rate=24000)))
    assert out == pcm


def test_synthesize_wav_reply_is_parsed_and_resampled(monkeypatch):
    pcm48 = _tone(1.0, 48000)
    _mount(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=_wav(pcm48, 48000), headers={"content-type": "audio/wav"}
        ),
    )
    out = run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="atlas", out_rate=16000)))
    assert abs(len(out) / 2 - 16000) <= 2  # one second at 16 kHz, header stripped


def test_synthesize_stereo_wav_becomes_mono(monkeypatch):
    stereo = _tone(0.5, 16000) * 2  # length only matters
    _mount(monkeypatch, lambda r: httpx.Response(200, content=_wav(stereo, 16000, channels=2)))
    out = run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=16000)))
    assert len(out) == len(stereo) // 2


def test_synthesize_refuses_compressed_audio(monkeypatch):
    _mount(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=b"ID3\x04" + b"\x00" * 100, headers={"content-type": "audio/mpeg"}
        ),
    )
    with pytest.raises(se.SpeechError) as e:
        run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=16000)))
    assert e.value.code == "unsupported_audio"


def test_synthesize_bounds_the_audio(monkeypatch):
    monkeypatch.setattr(se, "SPEECH_MAX_BYTES", 1000)
    _mount(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=b"\x00" * 5000, headers={"content-type": "audio/pcm"}
        ),
    )
    with pytest.raises(se.SpeechError) as e:
        run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=24000)))
    assert e.value.code == "too_large"


def test_parse_wav_header_skips_extra_chunks():
    raw = _wav(_tone(0.1, 8000), 8000)
    # Insert a LIST chunk between fmt and data.
    fmt_end = raw.index(b"data")
    patched = raw[:fmt_end] + b"LIST" + struct.pack("<I", 4) + b"abcd" + raw[fmt_end:]
    rate, ch, bits, off = se.parse_wav_header(patched)
    assert (rate, ch, bits) == (8000, 1, 16) and patched[off - 8 : off - 4] == b"data"


def test_resample_lengths():
    pcm = _tone(1.0, 48000)
    assert abs(len(se.resample16(pcm, 48000, 16000)) / 2 - 16000) <= 1
    assert se.resample16(pcm, 16000, 16000) == pcm


# --- voices ------------------------------------------------------------------------------------


def test_voices_from_kokoro_style_list(monkeypatch):
    _mount(monkeypatch, lambda r: httpx.Response(200, json={"voices": ["af_heart", "am_michael"]}))
    assert run(se.list_voices(se.Endpoint(BASE))) == ["af_heart", "am_michael"]


def test_voices_fall_back_to_health(monkeypatch):
    def handler(r: httpx.Request):
        if r.url.path.endswith("/audio/voices"):
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "ok", "voices": ["aria", "atlas"]})

    _mount(monkeypatch, handler)
    assert run(se.list_voices(se.Endpoint(BASE))) == ["aria", "atlas"]


def test_no_voice_list_means_manual_entry(monkeypatch):
    _mount(monkeypatch, lambda r: httpx.Response(404))
    assert run(se.list_voices(se.Endpoint(BASE))) is None


# --- the compatibility check -------------------------------------------------------------------


def _speech_server(
    rate=24000,
    raw=True,
    voices=("nova",),
    heard="Commander, the rebase finished twelve minutes ago.",
):
    def handler(r: httpx.Request):
        p = r.url.path
        if p.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if p.endswith("/audio/voices"):
            return httpx.Response(200, json={"voices": list(voices)})
        if p.endswith("/audio/speech"):
            pcm = _tone(2.0, rate)
            return httpx.Response(200, content=pcm if raw else _wav(pcm, rate))
        if p.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": heard})
        return httpx.Response(404)

    return handler


def test_check_passes_a_working_pair(monkeypatch):
    _mount(monkeypatch, _speech_server())
    rep = run(sc.check(se.Endpoint(BASE), se.Endpoint(BASE), "nova"))
    assert rep.usable
    ids = {c.id: c for c in rep.checks}
    assert ids["stt.transcribe"].ok and ids["tts.synthesize"].ok and ids["tts.voices"].ok


def test_check_fails_when_the_phrase_comes_back_wrong(monkeypatch):
    _mount(monkeypatch, _speech_server(heard="banana pancakes"))
    rep = run(sc.check(se.Endpoint(BASE), se.Endpoint(BASE), "nova"))
    assert not rep.usable
    assert any(c.id == "stt.transcribe" and not c.ok for c in rep.checks)


def test_the_report_never_carries_what_the_endpoint_said(monkeypatch):
    """A transcript is a response body: the report (shown in Settings, printed by the script)
    carries counts and timing only — even when the endpoint says something sensitive."""
    secret = "my password is hunter2-SENSITIVE"
    for heard in (secret, f"Commander, the rebase finished twelve minutes ago. {secret}"):
        _mount(monkeypatch, _speech_server(heard=heard))
        rep = run(sc.check(se.Endpoint(BASE), se.Endpoint(BASE), "nova"))
        assert "hunter2" not in json.dumps(rep.as_dict())


def test_check_flags_an_unknown_voice(monkeypatch):
    _mount(monkeypatch, _speech_server(voices=("other",)))
    rep = run(sc.check(se.Endpoint(BASE), se.Endpoint(BASE), "nova"))
    assert not rep.usable
    assert any(c.id == "tts.voice" and not c.ok for c in rep.checks)


def test_check_names_missing_optional_capabilities(monkeypatch):
    def handler(r):
        if r.url.path.endswith(("/models", "/audio/voices", "/health")):
            return httpx.Response(404)
        return _speech_server()(r)

    _mount(monkeypatch, handler)
    rep = run(sc.check(se.Endpoint(BASE), se.Endpoint(BASE), "nova"))
    assert rep.usable  # optional only
    notes = " ".join(c.note for c in rep.checks if not c.ok)
    assert "enter the voice" in notes and "enter the model" in notes


def test_check_reports_a_rejected_token(monkeypatch):
    _mount(monkeypatch, lambda r: httpx.Response(401))
    rep = run(sc.check(se.Endpoint(BASE, api_key="bad"), se.Endpoint(BASE, api_key="bad"), "nova"))
    assert not rep.usable
    assert any("rejected" in c.note for c in rep.checks)


# --- review 6826: rebinding, hostile WAV headers, streaming phase ------------------------------


def test_the_connection_goes_to_the_address_that_was_vetted(monkeypatch):
    """A rebinding name answers LAN first and the metadata address second: the host is resolved
    once, and the request goes to the vetted address with the configured name as Host / SNI."""
    answers = iter([["10.0.0.5"], ["169.254.169.254"]])
    calls = []

    async def rebinding(host, port):
        calls.append(host)
        return next(answers)

    monkeypatch.setattr(se, "_resolve", rebinding)
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    run(se.list_models(se.Endpoint("https://speech.example:8443/v1", "m", "tok")))
    assert calls == ["speech.example"]  # one resolution, never a second one at connect time
    req = seen[0]
    assert req.url.host == "10.0.0.5" and req.url.port == 8443
    assert req.headers["host"] == "speech.example:8443"
    assert req.extensions.get("sni_hostname") == "speech.example"


def test_any_refused_address_refuses_the_name(monkeypatch):
    async def mixed(host, port):
        return ["10.0.0.5", "169.254.169.254"]

    monkeypatch.setattr(se, "_resolve", mixed)
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint("http://speech.example/v1")))
    assert e.value.code == "refused_destination" and seen == []


def _wav_with_rate(rate: int, pcm: bytes = b"\x00\x01" * 100) -> bytes:
    good = _wav(pcm, 16000)
    i = good.index(b"fmt ") + 8
    return good[: i + 4] + struct.pack("<I", rate) + good[i + 8 :]


@pytest.mark.parametrize("rate", [0, 1, 4000, 10_000_000])
def test_a_hostile_wav_rate_is_refused(monkeypatch, rate):
    _mount(monkeypatch, lambda r: httpx.Response(200, content=_wav_with_rate(rate)))
    with pytest.raises(se.SpeechError) as e:
        run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=16000)))
    assert e.value.code == "unsupported_audio"


def _chunks(data: bytes, size: int):
    async def gen():
        for i in range(0, len(data), size):
            yield data[i : i + size]

    return gen()


@pytest.mark.parametrize("size", [1, 2, 3, 7, 641])
def test_a_fragmented_stream_resamples_like_one_buffer(monkeypatch, size):
    pcm48 = _tone(0.5, 48000)
    wav = _wav(pcm48, 48000)
    whole = se.resample16(pcm48, 48000, 16000)
    _mount(monkeypatch, lambda r: httpx.Response(200, content=_chunks(wav, size)))
    got = run(_collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=16000)))
    assert got == whole
    assert abs(len(got) / 2 - 8000) <= 2  # half a second at 16 kHz, not 3x too long


def test_raw_pcm_fragments_pass_through_unchanged(monkeypatch):
    pcm = _tone(0.25, se.OPENAI_PCM_RATE)
    _mount(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=_chunks(pcm, 3), headers={"content-type": "audio/pcm"}
        ),
    )
    got = run(
        _collect(se.synthesize(se.Endpoint(BASE), "Hi", voice="v", out_rate=se.OPENAI_PCM_RATE))
    )
    assert got == pcm


# --- second-review findings (#1621) -------------------------------------------------------------


def _resolving(monkeypatch, *addrs):
    async def fake(host, port):
        return list(addrs)

    monkeypatch.setattr(se, "_resolve", fake)


def test_only_identity_encoding_is_asked_for_and_accepted(monkeypatch):
    """A compressed reply would be inflated before any cap saw it (64 KiB of gzip → 64 MB)."""
    import gzip

    _resolving(monkeypatch, "10.0.0.5")
    body = gzip.compress(b"\0" * 1_000_000)
    seen = _mount(
        monkeypatch,
        lambda r: httpx.Response(200, content=body, headers={"content-encoding": "gzip"}),
    )
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint("http://speech.example/v1")))
    assert e.value.code == "unsupported_encoding"
    assert seen[0].headers["accept-encoding"] == "identity"

    async def speak():
        return [c async for c in se.synthesize(se.Endpoint("http://speech.example/v1"), "hi",
                                               voice="v", out_rate=24000)]  # fmt: skip

    with pytest.raises(se.SpeechError) as e:
        run(speak())
    assert e.value.code == "unsupported_encoding"


@pytest.mark.parametrize(
    "addrs",
    [("::1",), ("::1", "127.0.0.1"), ("::ffff:127.0.0.1",), ("127.0.0.1",)],
)
def test_loopback_is_allowed_in_every_form(monkeypatch, addrs):
    """`::1` sits in reserved ::/8 for `ipaddress`; a dual-stack `localhost` must still work."""
    _resolving(monkeypatch, *addrs)
    _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    assert run(se.list_models(se.Endpoint("http://localhost:8771/v1"))) == ["m"]


@pytest.mark.parametrize(
    "addr",
    ["fd00:ec2::254", "100.100.100.200", "2002:a9fe:a9fe::", "::ffff:169.254.169.254",
     "64:ff9b::a9fe:a9fe", "169.254.169.254"],
)  # fmt: skip
def test_metadata_addresses_are_refused_in_every_form(monkeypatch, addr):
    _resolving(monkeypatch, addr)
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint("http://speech.example/v1")))
    assert e.value.code == "refused_destination" and seen == []


class _Split(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p


def _speak_parts(monkeypatch, parts, ctype="application/octet-stream", out_rate=24000):
    _resolving(monkeypatch, "10.0.0.5")
    _mount(monkeypatch, lambda r: httpx.Response(200, stream=_Split(parts),
                                                 headers={"content-type": ctype}))  # fmt: skip

    ep = se.Endpoint("http://speech.example/v1")

    async def go():
        return b"".join([c async for c in se.synthesize(ep, "hi", voice="v", out_rate=out_rate)])

    return run(go())


def test_a_wav_split_inside_its_header_is_still_a_wav(monkeypatch):
    pcm = _tone(1.0, 24000)
    wav = _wav(pcm, 24000)
    out = _speak_parts(monkeypatch, [wav[:2], wav[2:]])
    assert out == pcm  # not the header played as samples


def test_chunks_after_wav_data_are_not_audio(monkeypatch):
    pcm = _tone(0.5, 24000)
    trailer = b"LIST" + struct.pack("<I", 12) + b"INFOISFT\x00\x00\x00\x00"
    out = _speak_parts(monkeypatch, [_wav(pcm, 24000) + trailer])
    assert out == pcm


def test_raw_pcm_shorter_than_a_header_still_plays(monkeypatch):
    out = _speak_parts(monkeypatch, [b"\x10\x00\x20\x00"])
    assert out == b"\x10\x00\x20\x00"


def test_the_resampled_tail_is_bounded_too(monkeypatch):
    """Review 6830: the flushed resampler tail counts against the cap like every other chunk."""
    pcm = _tone(0.05, 16000)  # resampled to 24 kHz: the flush emits a final tail
    _mount(monkeypatch, lambda r: httpx.Response(200, content=_wav(pcm, 16000)))
    for cap in range(2, 4000, 2):
        monkeypatch.setattr(se, "SPEECH_MAX_BYTES", max(cap, len(_wav(pcm, 16000))))
        out = 0
        try:

            async def go():
                n = 0
                async for chunk in se.synthesize(
                    se.Endpoint(BASE), "hi", voice="v", out_rate=24000
                ):
                    n += len(chunk)
                return n

            out = run(go())
        except se.SpeechError as e:
            assert e.code == "too_large"
            continue
        assert out <= se.SPEECH_MAX_BYTES, (cap, out)


@pytest.mark.parametrize("channels", [1, 2, 3, 5, 6, 7, 8])
def test_a_long_multichannel_wav_survives_the_resampling_slices(monkeypatch, channels):
    """Review 6839: a 64 KiB slice not divisible by the frame width split frames for 3/5/6/7
    channels — the wrong channel, dropped samples. Longer than one slice, every channel count."""
    frames = 20_000
    mono = [(i * 7) % 2000 - 1000 for i in range(frames)]
    pcm = b"".join(struct.pack("<" + "h" * channels, *([v] + [0] * (channels - 1))) for v in mono)
    wav = _wav(pcm, 24000, channels=channels)
    out = _speak_parts(monkeypatch, [wav])
    assert len(out) == frames * 2
    assert out == struct.pack("<" + "h" * frames, *mono)


# --- after a sized WAV `data` chunk (second review, follow-up) --------------------------------


def _understated(pcm: bytes, rate: int, claimed: int) -> bytes:
    wav = _wav(pcm, rate)
    off = wav.index(b"data") + 4
    return wav[:off] + struct.pack("<I", claimed) + wav[off + 4 :]


@pytest.mark.parametrize("bytewise", [False, True])
def test_an_understated_wav_length_plays_to_the_end(monkeypatch, bytewise):
    """Python's `wave` writing to a pipe puts the FIRST chunk's length in the header."""
    pcm = _tone(0.3, 24000)
    wav = _understated(pcm, 24000, 200)
    parts = [wav[i : i + 1] for i in range(len(wav))] if bytewise else [wav]
    assert _speak_parts(monkeypatch, parts) == pcm


@pytest.mark.parametrize("bytewise", [False, True])
def test_one_wav_per_sentence_plays_every_sentence(monkeypatch, bytewise):
    a, b = _tone(0.2, 24000), _tone(0.3, 24000)[::-1]
    body = _wav(a, 24000) + _wav(b, 24000)
    parts = [body[i : i + 1] for i in range(len(body))] if bytewise else [body]
    assert _speak_parts(monkeypatch, parts) == a + b


def test_a_second_wav_in_another_format_ends_the_audio(monkeypatch):
    a = _tone(0.2, 24000)
    assert _speak_parts(monkeypatch, [_wav(a, 24000) + _wav(_tone(0.2, 16000), 16000)]) == a


def test_a_metadata_chunk_still_ends_the_audio_bytewise(monkeypatch):
    pcm = _tone(0.1, 24000)
    body = _wav(pcm, 24000) + b"LIST" + struct.pack("<I", 12) + b"INFOISFT\x00\x00\x00\x00"
    assert _speak_parts(monkeypatch, [body[i : i + 1] for i in range(len(body))]) == pcm


@pytest.mark.parametrize(
    "addr", ["fd20:ce::254", "fd00:c1::a9fe:a9fe", "64:ff9b::7f00:1", "2002:7f00:1::"]
)
def test_more_metadata_and_translated_loopback_are_refused(monkeypatch, addr):
    _resolving(monkeypatch, addr)
    seen = _mount(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(se.SpeechError) as e:
        run(se.list_models(se.Endpoint("http://speech.example/v1")))
    assert e.value.code == "refused_destination" and seen == []
