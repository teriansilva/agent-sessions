"""#1397 P3 — each General sounds like themselves, in the Commander's language.

Pinned here:

* a contact's profile is its override, else its own defaults (`hq_contacts.call_style` /
  `call_speed`), with the voice falling back to its group's;
* a style is sent ONLY when the TTS server advertised it, so a strict server never sees an unknown
  field; overrides are validated and reset with `null`;
* discovery reads a server's voices, styles and languages (VoxCPM's `/health` shape included);
* the worker is told the profile and whether to detect the language; a detected language is kept
  per call and passed to every `say`, unless the operator fixed the call's language;
* a contact preview speaks a FIXED line naming the contact, in its profile;
* the personas' prompt text is untouched (tone in the voice, not in an imitated accent).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import hq_calls_settings as hs
from agent_sessions import hq_contacts, speech_endpoints, voice_calls

BASE = {
    "stt": {"base_url": "http://speech.lan:8000/v1"},
    "tts": {"base_url": "http://speech.lan:8880/v1"},
    "voices": {"woman": "aria", "man": "atlas"},
}


@pytest.fixture(autouse=True)
def _lan(monkeypatch):
    async def fake_resolve(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(speech_endpoints, "_resolve", fake_resolve)


@pytest.fixture(autouse=True)
def _base_servers_were_tested():
    """A save may claim only what the server observed of THAT endpoint (#1635 review). BASE's two
    servers were tested and showed everything these tests save; any other endpoint was not."""
    hs.forget_observations()
    hs.observe("stt", BASE["stt"]["base_url"], "", "", stt_detects_language=True)
    hs.observe(
        "tts", BASE["tts"]["base_url"], "", "", tts_accepts_language=True,
        tts_styles=["calm", "dramatic", "serious", "narrator"],
        tts_voices=["aria", "atlas", "nova", "kerstin"], tts_languages=["en", "de"],
    )  # fmt: skip
    yield
    hs.forget_observations()


def test_defaults_come_from_the_contact():
    hs.save(BASE)
    p = hs.profile(hs.get(), "russia-man")
    assert p["voice"] == "atlas"
    assert p["style"] is None  # the server advertised no styles: never sent
    assert p["speed"] == hq_contacts.get("russia-man").call_speed


def test_an_advertised_style_is_sent():
    hs.save({**BASE, "tts_styles": ["calm", "dramatic", "serious"]})
    assert hs.profile(hs.get(), "russia-man")["style"] == "dramatic"
    assert hs.profile(hs.get(), "asia-woman")["style"] == "calm"
    # A contact whose default style the server lacks sends none.
    assert hs.profile(hs.get(), "us-woman")["style"] is None


def test_overrides_win_and_reset():
    hs.save({**BASE, "tts_styles": ["calm", "narrator"]})
    hs.save({"contacts": {"asia-woman": {"voice": "nova", "style": "narrator", "speed": 1.2}}})
    p = hs.profile(hs.get(), "asia-woman")
    assert (p["voice"], p["style"], p["speed"]) == ("nova", "narrator", 1.2)
    hs.save({"contacts": {"asia-woman": None}})
    assert hs.profile(hs.get(), "asia-woman")["voice"] == "aria"


def test_a_voice_that_left_the_server_falls_back_to_the_group():
    hs.save({**BASE, "tts_voices": ["aria", "atlas"]})
    hs.save({"contacts": {"us-woman": {"voice": "nova"}}})
    assert hs.profile(hs.get(), "us-woman")["voice"] == "aria"
    hs.save({"tts_voices": ["aria", "atlas", "nova"]})
    assert hs.profile(hs.get(), "us-woman")["voice"] == "nova"


@pytest.mark.parametrize(
    "bad",
    [
        {"contacts": {"nobody": {"voice": "x"}}},
        {"contacts": {"asia-woman": {"speed": 3}}},
        {"contacts": {"asia-woman": {"pitch": 2}}},
        {"language": "german"},
        {"language": "de\n"},
        {"contacts": {"asia-woman": {"language_voices": {"de\n": "kerstin"}}}},
        {"tts_styles": "calm"},
    ],
)
def test_bad_profiles_are_refused(bad):
    with pytest.raises(hs.SettingsError):
        hs.save(bad)


def test_every_contact_has_a_style_and_speed():
    for c in hq_contacts.CONTACTS:
        assert c.call_style and 0.75 <= c.call_speed <= 1.5


def test_the_personas_are_unchanged_by_voice_profiles():
    # Tone lives in the persona text, which still forbids imitated accents.
    for c in hq_contacts.CONTACTS:
        assert "no imitated accent or dialect spelling" in c.persona


def test_discovery_reads_voxcpm_health(monkeypatch):
    def handler(r: httpx.Request):
        if r.url.path.endswith("/audio/voices"):
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "voices": ["aria", "atlas"],
                "styles": ["calm", "dramatic"],
                "languages": "en,de + 28 others (no Romanian)",
            },
        )

    monkeypatch.setattr(speech_endpoints, "_TRANSPORT", httpx.MockTransport(handler))
    got = asyncio.run(speech_endpoints.discover(speech_endpoints.Endpoint("http://speech.lan/v1")))
    assert got == {
        "voices": ["aria", "atlas"],
        "styles": ["calm", "dramatic"],
        "languages": ["en", "de"],
    }


def test_the_worker_is_told_the_profile_and_detection():
    hs.save({**BASE, "tts_styles": ["calm"], "stt_detects_language": True})
    block = hs.get()
    prof = voice_calls.profile_for(block, "asia-woman")
    cfg = voice_calls.worker_config(block, prof["voice"], prof)
    assert (
        cfg["tts"]["style"] == "calm" and cfg["detect_language"] is True and cfg["language"] is None
    )
    assert cfg["tts_language_ok"] is False  # never probed: no language field is sent
    hs.save({"language": "de", "tts_accepts_language": True})
    cfg = voice_calls.worker_config(hs.get(), prof["voice"], prof)
    assert cfg["detect_language"] is False and cfg["language"] == "de"
    assert cfg["tts_language_ok"] is True
    # Fixed ENGLISH on a detecting endpoint: detection runs (it gates spoken confirmation), and
    # the STT hint is left off so the detection is real.
    hs.save({"language": "en"})
    cfg = voice_calls.worker_config(hs.get(), prof["voice"], prof)
    assert cfg["detect_language"] is True and cfg["language"] is None
    assert "api_key" not in json.dumps(cfg)


def _call(monkeypatch, fixed=None, tts_takes_language=True):
    sent: list = []
    call = voice_calls.Call(
        id="c", cid="x", contact="europe-man", voice="atlas", operator=None, app=None,
        registry=None, state="listening",
    )  # fmt: skip
    call.fixed_language = call.language = fixed
    call.tts_takes_language = tts_takes_language

    async def to_worker(m):
        sent.append(m)

    async def to_client(m):
        pass

    call._to_worker, call._to_client = to_worker, to_client

    async def no_turn(text, **kw):
        return None

    monkeypatch.setattr(call, "on_utterance", no_turn)
    return call, sent


def test_a_detected_language_is_spoken_back(monkeypatch):
    call, sent = _call(monkeypatch)

    async def go():
        await call.on_worker_event({"t": "utterance", "text": "Wie läuft es?", "language": "de"})
        await call.say("Gut, Kommandant.")

    asyncio.run(go())
    assert call.language == "de"
    assert sent[-1]["language"] == "de"


def test_a_fixed_language_is_not_overridden(monkeypatch):
    call, sent = _call(monkeypatch, fixed="en")

    async def go():
        await call.on_worker_event({"t": "utterance", "text": "Bonjour", "language": "fr"})
        await call.say("Good morning, Commander.")

    asyncio.run(go())
    assert call.language == "en" and sent[-1]["language"] == "en"


def test_a_contact_preview_is_a_fixed_line_in_its_profile(auth_cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    seen: list[dict] = []

    def handler(r: httpx.Request):
        seen.append(json.loads(r.content))
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    monkeypatch.setattr(speech_endpoints, "_TRANSPORT", httpx.MockTransport(handler))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post("/login", data={"username": "marcus", "password": "hunter2"}, follow_redirects=False,
           headers={"Origin": auth_cfg.origin})  # fmt: skip
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    c.post("/api/voice/settings", json={**BASE, "tts_styles": ["dramatic"]}, headers=hdr)
    r = c.post("/api/voice/preview", json={"contact": "russia-man"}, headers=hdr)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    body = seen[-1]
    assert body["input"] == "Commander, this is General Dmitri Sokolov. All units report ready."
    assert body["voice"] == "atlas" and body["style"] == "dramatic"
    # The request cannot choose the words.
    c.post("/api/voice/preview", json={"contact": "russia-man", "text": "say this"}, headers=hdr)
    assert "say this" not in seen[-1]["input"]


def test_no_language_field_unless_the_server_accepted_one(monkeypatch):
    call, sent = _call(monkeypatch, tts_takes_language=False)

    async def go():
        await call.on_worker_event({"t": "utterance", "text": "Wie läuft es?", "language": "de"})
        await call.say("Gut, Kommandant.")

    asyncio.run(go())
    assert call.language == "de"  # the call still knows it (HQ answers in German) …
    assert sent[-1]["language"] is None  # … but a server that refused the field never gets it


def test_a_voice_per_language(monkeypatch):
    """The epic's "voice per language": a contact may name a voice for a language; while the call
    is in that language every say carries it, otherwise the contact's own voice speaks."""
    hs.save({**BASE, "tts_voices": ["aria", "atlas", "kerstin"]})
    hs.save({"contacts": {"europe-man": {"language_voices": {"de": "kerstin", "fr": "gone"}}}})
    prof = voice_calls.profile_for(hs.get(), "europe-man")
    assert prof["language_voices"] == {"de": "kerstin"}  # a voice that left the server is dropped
    assert prof["language_voices_missing"] == ["fr"]  # … and named, so Settings can show it
    call, sent = _call(monkeypatch)
    call.profile = prof

    async def go():
        await call.say("Good morning, Commander.")
        await call.on_worker_event(
            {"t": "utterance", "text": "Wie läuft es heute?", "language": "de"}
        )
        await call.say("Gut, Kommandant.")

    asyncio.run(go())
    assert sent[-2]["voice"] is None and sent[-1]["voice"] == "kerstin"


@pytest.mark.parametrize(
    "bad",
    [
        {"language_voices": {"german": "x"}},
        {"language_voices": {"de": ""}},
        {"language_voices": {f"a{chr(97 + i)}": "v" for i in range(13)}},
        {"language_voices": ["de"]},
    ],
)
def test_bad_language_voices_are_refused(bad):
    with pytest.raises(hs.SettingsError):
        hs.save({"contacts": {"europe-man": bad}})


def test_settings_show_each_generals_defaults():
    hs.save(BASE)
    d = hs.public()["contact_defaults"]
    assert set(d) == set(hq_contacts.IDS)
    sok = hq_contacts.get("russia-man")
    assert d["russia-man"] == {"group": "man", "style": sok.call_style, "speed": sok.call_speed}


def test_probed_capabilities_reset_when_their_endpoint_changes():
    """Second review of #1635: what one server advertised or proved is not true of the next.
    Changing a side's URL or model clears its capabilities unless the same save sets them."""
    hs.save({**BASE, "tts_styles": ["calm"], "tts_voices": ["aria"], "tts_languages": ["en"],
             "tts_accepts_language": True, "stt_detects_language": True})  # fmt: skip
    hs.save({"tts": {"base_url": "http://other.lan:8880/v1"}})
    b = hs.get()
    assert b["tts_accepts_language"] is False
    assert b["tts_styles"] == b["tts_voices"] == b["tts_languages"] == []
    assert b["stt_detects_language"] is True  # the STT side did not change
    hs.save({"stt": {"model": "whisper-large"}})
    assert hs.get()["stt_detects_language"] is None  # unknown again: confirmation fails closed
    # The settings page tests the new endpoint and saves what it proved with it: kept.
    hs.observe("tts", "http://third.lan/v1", "", "", tts_accepts_language=True)
    hs.save({"tts": {"base_url": "http://third.lan/v1"}, "tts_accepts_language": True})
    assert hs.get()["tts_accepts_language"] is True
    hs.save({"tts": {"base_url": "http://third.lan/v1"}})  # unchanged: kept
    assert hs.get()["tts_accepts_language"] is True


def test_a_full_save_cannot_carry_claims_to_an_endpoint_never_tested():
    """Review 6884: a client saving a NEW URL together with the old server's claims keeps none of
    them — a claim needs the server's own observation of the endpoint being saved."""
    claims = {"stt_detects_language": True, "tts_accepts_language": True, "tts_styles": ["calm"]}
    hs.save({**BASE, **claims})
    assert {k: hs.get()[k] for k in claims} == claims
    hs.save({"stt": {"base_url": "http://new.lan/v1"}, "tts": {"base_url": "http://new.lan/v1"},
             **claims})  # fmt: skip
    b = hs.get()
    assert b["stt_detects_language"] is None and b["tts_accepts_language"] is False
    assert b["tts_styles"] == []


def test_a_claim_is_bounded_by_what_was_observed():
    hs.save({**BASE, "tts_styles": ["calm", "invented"], "tts_voices": ["aria", "ghost"]})
    b = hs.get()
    assert b["tts_styles"] == ["calm"] and b["tts_voices"] == ["aria"]
    hs.forget_observations()  # e.g. a restart: nothing observed yet …
    hs.save({"captions": False})  # … an unrelated save keeps what was stored
    assert hs.get()["tts_styles"] == ["calm"]
    hs.save({"tts_styles": ["calm", "narrator"]})  # … a NEW claim needs a new test
    assert hs.get()["tts_styles"] == ["calm"]


def test_the_test_and_voices_routes_record_what_they_observed(auth_cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions import speech_conformance
    from agent_sessions.main import create_app

    hs.forget_observations()

    async def fake_check(stt, tts, voice):
        rep = speech_conformance.Report()
        rep.add("stt.language", True, False, value={"verdict": "detects"})
        rep.add("tts.language", stt.base_url == "never", False)  # refused
        return rep

    async def fake_discover(ep):
        return {"voices": ["aria", "atlas"], "styles": ["calm"], "languages": ["en"]}

    async def no_models(ep):
        return None

    monkeypatch.setattr(speech_conformance, "check", fake_check)
    monkeypatch.setattr(speech_endpoints, "discover", fake_discover)
    monkeypatch.setattr(speech_endpoints, "list_models", no_models)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post("/login", data={"username": "marcus", "password": "hunter2"}, follow_redirects=False,
           headers={"Origin": auth_cfg.origin})  # fmt: skip
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    draft = {"stt": BASE["stt"], "tts": BASE["tts"], "voice": "aria"}
    assert c.post("/api/voice/test", json=draft, headers=hdr).status_code == 200
    r = c.post("/api/voice/voices", json={"side": "tts", "endpoint": BASE["tts"]}, headers=hdr)
    assert r.status_code == 200
    claims = {"stt_detects_language": True, "tts_accepts_language": True,
              "tts_styles": ["calm", "dramatic"], "tts_voices": ["aria"]}  # fmt: skip
    assert c.post("/api/voice/settings", json={**BASE, **claims}, headers=hdr).status_code == 200
    b = hs.get()
    assert b["stt_detects_language"] is True  # observed by the test
    assert b["tts_accepts_language"] is False  # the test saw it refused
    assert b["tts_styles"] == ["calm"] and b["tts_voices"] == ["aria"]  # within discovery


def test_a_language_with_a_trailing_newline_is_no_language(monkeypatch):
    call, _ = _call(monkeypatch)

    async def go():
        await call.on_worker_event(
            {"t": "utterance", "text": "Wie läuft es heute?", "language": "de\n",
             "heard_language": "en\n", "turn": 1}
        )  # fmt: skip

    asyncio.run(go())
    assert call.language is None and call.heard_language is None


def test_a_restart_never_downgrades_an_established_claim():
    """Review 6887: after a restart nothing is observed, yet re-saving the SAME endpoint with the
    claims the settings already hold keeps them — a lost detection flag would otherwise relax the
    spoken-confirmation gate."""
    claims = {"stt_detects_language": True, "tts_accepts_language": True, "tts_styles": ["calm"]}
    hs.save({**BASE, **claims})
    hs.forget_observations()  # the restart
    hs.save({**BASE, **claims})  # the settings page's full save, endpoints unchanged
    assert {k: hs.get()[k] for k in claims} == claims


def test_detection_is_unknown_until_tested_and_a_false_needs_evidence_too():
    assert hs.get()["stt_detects_language"] is None  # never tested
    hs.save({**BASE, "stt_detects_language": False})  # "can't detect" relaxes the gate …
    assert hs.get()["stt_detects_language"] is None  # … so it needs an observation as well
    hs.observe("stt", BASE["stt"]["base_url"], "", "", stt_detects_language=False)
    hs.save({**BASE, "stt_detects_language": False})
    assert hs.get()["stt_detects_language"] is False


def test_claims_belong_to_the_credential_too():
    """Review 6887: a new token is a new identity — its claims are unknown until tested, and an
    observation made with another token is never borrowed (rotation, a return to an old model)."""
    hs.save({**BASE, "stt_detects_language": True, "tts_accepts_language": True})
    hs.save(
        {"tts": {"base_url": BASE["tts"]["base_url"], "api_key": "sk-rotated-0123456789abcdef"}}
    )
    b = hs.get()
    assert b["tts_accepts_language"] is False and b["stt_detects_language"] is True
    hs.observe("tts", BASE["tts"]["base_url"], "kokoro", "", tts_accepts_language=True)  # old token
    hs.save({"tts": {"base_url": BASE["tts"]["base_url"], "model": "kokoro"},
             "tts_accepts_language": True})  # fmt: skip
    assert hs.get()["tts_accepts_language"] is False  # observed with ANOTHER token
    hs.observe("tts", BASE["tts"]["base_url"], "kokoro", "sk-rotated-0123456789abcdef",
               tts_accepts_language=True)  # fmt: skip
    hs.save({"tts_accepts_language": True})
    assert hs.get()["tts_accepts_language"] is True


@pytest.mark.parametrize(
    "detect_reply",
    [
        lambda: httpx.Response(422, json={"detail": "audio could not be decoded"}),
        lambda: httpx.Response(500, json={"detail": "busy"}),
        lambda: httpx.Response(200, json={"text": "x", "language": "cantonese"}),
        lambda: httpx.Response(200, json={"text": "confirm", "language": ""}),
        lambda: httpx.Response(200, json={"text": "confirm", "language": None}),
    ],
    ids=["refused-422", "server-error", "unmapped-language", "empty-language", "null-language"],
)
def test_an_unproven_detection_never_relaxes_the_confirmation_gate(
    auth_cfg, monkeypatch, detect_reply
):
    """Reviews 6890-6892, through the REAL conformance test → save → confirmation: a refusal of
    unknown reason, a 5xx, a language we cannot map, or an empty/null language is not proof the
    endpoint cannot detect.
    The page saves `false` as its report suggests — the setting never becomes `false`, and a
    spoken confirm with an unknown heard language is refused on an Auto call."""
    from fastapi.testclient import TestClient

    from agent_sessions import speech_conformance
    from agent_sessions.main import create_app

    hs.forget_observations()
    pcm = b"\x01\x00" * 24000

    def handler(r: httpx.Request):
        p = r.url.path
        if p.endswith("/audio/transcriptions"):
            if b"verbose_json" in r.content:
                return detect_reply()
            return httpx.Response(200, json={"text": speech_conformance.PROBE_PHRASE})
        if p.endswith("/audio/speech"):
            return httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})
        if p.endswith("/audio/voices"):
            return httpx.Response(200, json={"voices": ["aria"]})
        if p.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404)

    monkeypatch.setattr(speech_endpoints, "_TRANSPORT", httpx.MockTransport(handler))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post("/login", data={"username": "marcus", "password": "hunter2"}, follow_redirects=False,
           headers={"Origin": auth_cfg.origin})  # fmt: skip
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    draft = {"stt": BASE["stt"], "tts": BASE["tts"], "voice": "aria"}
    rep = c.post("/api/voice/test", json=draft, headers=hdr).json()
    assert rep["usable"], rep
    r = c.post("/api/voice/settings", json={**BASE, "stt_detects_language": False}, headers=hdr)
    assert r.status_code == 200
    assert hs.get()["stt_detects_language"] is not False  # never "cannot detect"
    call, _ = _call(monkeypatch)
    det = hs.get()["stt_detects_language"]
    call.detects_language = det if isinstance(det, bool) else None  # as admission does
    call.heard_language = None
    assert call._confirmation_language_ok() is False
