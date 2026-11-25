"""A TTS server's voices, styles and languages are read again when none are saved.

Moving the TTS endpoint drops the old server's lists (they were true of THAT server). A switch
saved without a Read list left them empty: every General fell back to the group voice, no style
was sent, and Settings said "This server doesn't list speaking styles" of a server that lists ten.

Pinned here:

* a save leaving all three lists empty reads them from the stored server, within evidence;
* lists already saved are never re-read, and a server publishing none (or unreachable) stays
  empty without failing the save;
* a call admitted with no saved lists reads them first, so the General speaks in their own voice;
* a read that finishes after a switch, or after the lists were filled, changes nothing.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_sessions import hq_calls_settings as hs
from agent_sessions import hq_contacts, prefs, speech_endpoints, voice_calls
from agent_sessions.main import create_app
from agent_sessions.routes import voice as voice_routes
from agent_sessions.routes.voice import heal_tts_lists as REAL_HEAL  # before conftest's stub

OLD = "http://old-speech.lan:8781/v1"
NEW = "http://new-speech.lan:8781/v1"
SETUP = {
    "stt": {"base_url": "http://speech.lan:8771/v1", "model": "whisper-1"},
    "tts": {"base_url": OLD, "model": "tts", "api_key": "old-key"},
    "voices": {"woman": "aria", "man": "atlas"},
}
LISTED = {
    "voices": ["aria", "atlas", "claire", "hana", "kenji"],
    "styles": ["calm", "narrator", "serious"],
    "languages": ["en", "de"],
}


@pytest.fixture
def server(monkeypatch):
    """The real heal, over a fake discovery that records which endpoints it was asked."""
    monkeypatch.setattr(voice_routes, "heal_tts_lists", REAL_HEAL)
    asked: list[str] = []
    answer: dict = {"lists": LISTED}

    async def discover(ep):
        asked.append(ep.base_url)
        got = answer["lists"]
        if isinstance(got, Exception):
            raise got
        return dict(got)

    monkeypatch.setattr(speech_endpoints, "discover", discover)
    return asked, answer


@pytest.fixture
def client(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post("/login", data={"username": "marcus", "password": "hunter2"},
               follow_redirects=False, headers={"Origin": auth_cfg.origin})  # fmt: skip
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    return c, hdr


def _lists() -> dict:
    b = hs.get()
    return {k.removeprefix("tts_"): b[k] for k in ("tts_voices", "tts_styles", "tts_languages")}


def test_moving_the_tts_server_without_a_read_keeps_its_lists(server, client):
    """The 2026-11-13 switch to a new speech host, as the API saves it: no lists in the patch."""
    asked, _ = server
    c, hdr = client
    hs.save(SETUP)  # set up on the old server (lists empty: nothing read there)
    asked.clear()
    r = c.post("/api/voice/settings", headers=hdr,
               json={"tts": {"base_url": NEW, "model": "tts", "api_key": "new-key"}})  # fmt: skip
    assert r.status_code == 200
    assert asked == [NEW]
    assert _lists() == LISTED
    assert r.json()["tts_styles"] == LISTED["styles"]  # the answer the page adopts shows them


def test_a_save_that_empties_the_lists_reads_them_again(server, client):
    """Settings' own Save after a URL change with no Read list sends the lists as []."""
    c, hdr = client
    hs.save(SETUP)
    patch = {"tts": {"base_url": NEW, "api_key": "new-key"},
             "tts_voices": [], "tts_styles": [], "tts_languages": []}  # fmt: skip
    assert c.post("/api/voice/settings", json=patch, headers=hdr).status_code == 200
    assert _lists() == LISTED


def test_saved_lists_are_never_read_again(server, client):
    asked, _ = server
    c, hdr = client
    hs.save(SETUP)
    c.post("/api/voice/settings", json={"captions": False}, headers=hdr)  # heals once
    asked.clear()
    assert c.post("/api/voice/settings", json={"captions": True}, headers=hdr).status_code == 200
    assert asked == []


@pytest.mark.parametrize(
    "answer",
    [
        {"voices": None, "styles": None, "languages": None},
        speech_endpoints.SpeechError("unreachable", "the server did not answer"),
    ],
    ids=["publishes-none", "unreachable"],
)
def test_a_server_without_lists_stays_empty_and_the_save_stands(server, client, answer):
    _, ans = server
    ans["lists"] = answer
    c, hdr = client
    hs.save(SETUP)
    r = c.post("/api/voice/settings", json={"captions": False}, headers=hdr)
    assert r.status_code == 200 and r.json()["captions"] is False
    assert _lists() == {"voices": [], "styles": [], "languages": []}


def test_nothing_is_read_before_a_tts_server_is_saved(server, client):
    asked, _ = server
    c, hdr = client
    assert c.post("/api/voice/settings", json={"captions": False}, headers=hdr).status_code == 200
    assert asked == []


def test_a_call_reads_the_lists_first_so_the_general_speaks_in_their_own_voice(
    server, client, monkeypatch
):
    asked, _ = server
    c, hdr = client
    hs.save(SETUP)  # saved without the route: no lists, as on the live install
    ai = {"base_url": "https://ai.test/v1", "api_key": "k", "model": "m"}
    prefs.set_ai_review({"egress_acknowledged": True, "enabled": True, **ai})

    async def yes():
        return True, ""

    monkeypatch.setattr(voice_calls, "runtime_ready", yes)
    general = hq_contacts.get("asia-woman")
    assert general.call_voices[0] in LISTED["voices"] and general.call_style in LISTED["styles"]
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 201, r.text
    assert asked == [OLD]
    call = voice_calls.REGISTRY.current
    assert call.voice == general.call_voices[0]  # not the group's "aria"
    assert hs.profile(hs.get(), "asia-woman")["style"] == general.call_style


def test_a_late_read_of_the_old_server_never_erases_the_new_one(monkeypatch):
    """Hermes 8957: A's read is still out when the endpoint moves to B and B's heal saves B's
    lists. A's answer then arrives — and changes nothing."""
    monkeypatch.setattr(voice_routes, "heal_tts_lists", REAL_HEAL)
    b_lists = {"voices": ["new-voice"], "styles": ["new-style"], "languages": ["de"]}

    async def scenario():
        release_a = asyncio.Event()
        a_asked = asyncio.Event()

        async def discover(ep):
            if ep.base_url == OLD:
                a_asked.set()
                await release_a.wait()
                return dict(LISTED)  # disjoint from B's
            return dict(b_lists)

        monkeypatch.setattr(speech_endpoints, "discover", discover)
        hs.save(SETUP)
        heal_a = asyncio.ensure_future(REAL_HEAL())
        await a_asked.wait()
        hs.save({"tts": {"base_url": NEW, "model": "tts", "api_key": "new-key"}})
        assert await REAL_HEAL() is True  # B's own heal succeeds
        release_a.set()
        return await heal_a

    assert asyncio.run(scenario()) is False
    assert _lists() == b_lists
    assert hs.get()["tts"]["base_url"] == NEW


def test_a_late_read_never_overwrites_lists_saved_meanwhile(server):
    """Same endpoint, but a Read list + Save filled the lists while the heal's read was out."""
    hs.save(SETUP)
    hs.observe("tts", OLD, "tts", "old-key", tts_voices=["aria"], tts_styles=["calm"])
    hs.save({"tts_voices": ["aria"], "tts_styles": ["calm"]})
    saved = _lists()
    assert hs.fill_tts_lists(hs.get()["tts"], {f"tts_{k}": v for k, v in LISTED.items()}) is False
    assert _lists() == saved


def test_odd_entries_from_a_server_are_dropped_not_refused(server):
    _, ans = server
    ans["lists"] = {"voices": ["aria", "x" * 41, ""], "styles": ["calm"], "languages": None}
    hs.save(SETUP)
    assert asyncio.run(REAL_HEAL()) is True
    assert _lists() == {"voices": ["aria"], "styles": ["calm"], "languages": []}
