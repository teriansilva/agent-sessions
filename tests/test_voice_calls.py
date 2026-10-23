"""#1397 P1 — calling HQ: settings, routes, the call socket and the HQ bridge.

Pinned here:

* **settings** — speech tokens are origin-bound like the AI key (checked in the locked merge),
  optional, and never returned; calls are offered only once both endpoints and voices are set;
* **routes** — every read is `logged_in`, every write is CSRF + Origin; the draft test uses a
  stored token only for its own origin; one call at a time;
* **the socket** — cookie, Origin and the forced-password gate before anything; then a first
  `hello` carrying the CSRF token before any worker starts or audio is accepted;
* **the bridge** — a heard utterance becomes an ordinary HQ turn (`via: "call"`) and what is
  spoken is the STORED answer (markdown removed) plus fixed card sentences — never anything else;
* **the store** — `via` round-trips, an unknown channel is refused, and a call turn sends the
  registered `hq_call` prompt between the persona and the guarded HQ prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import (
    ask_orchestrator,
    ask_store,
    hq_calls_settings,
    hq_contacts,
    prefs,
    prompts,
    speech_endpoints,
    voice_calls,
    voice_text,
)
from agent_sessions.main import create_app

STT = "http://speech.lan:8000/v1"
TTS = "http://speech.lan:8880/v1"
FULL = {
    "stt": {"base_url": STT, "model": "whisper-1"},
    "tts": {"base_url": TTS, "model": "tts", "api_key": "tts-secret-1"},
    "voices": {"woman": "aria", "man": "atlas"},
}


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(voice_calls, "REGISTRY", voice_calls.Registry())


@pytest.fixture(autouse=True)
def _lan(monkeypatch):
    async def fake_resolve(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(speech_endpoints, "_resolve", fake_resolve)


def _login(c, cfg) -> dict:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


@pytest.fixture
def app(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    hdr = _login(c, auth_cfg)
    return c, hdr, auth_cfg


# --- settings ----------------------------------------------------------------------------------


def test_tokens_are_never_returned_and_are_optional():
    hq_calls_settings.save(FULL)
    pub = hq_calls_settings.public()
    blob = json.dumps(pub)
    assert "tts-secret-1" not in blob
    assert pub["tts"]["api_key_set"] is True and pub["stt"]["api_key_set"] is False
    assert pub["configured"] is True  # STT has no token: allowed


def test_configured_needs_both_endpoints_and_voices():
    hq_calls_settings.save({"stt": FULL["stt"], "tts": FULL["tts"]})
    assert not hq_calls_settings.public()["configured"]
    hq_calls_settings.save({"voices": {"woman": "aria", "man": "atlas"}})
    assert hq_calls_settings.public()["configured"]


@pytest.mark.parametrize("keep", [{}, {"api_key": ""}, {"api_key": "  "}, {"api_key": "********"}])
def test_a_stored_token_cannot_follow_its_url_to_another_origin(keep):
    hq_calls_settings.save(FULL)
    with pytest.raises(hq_calls_settings.SettingsError, match="tts"):
        hq_calls_settings.save({"tts": {"base_url": "https://elsewhere.example/v1", **keep}})
    assert hq_calls_settings.get()["tts"]["base_url"] == TTS


def test_moving_origin_with_a_new_token_or_a_clear_is_allowed():
    hq_calls_settings.save(FULL)
    hq_calls_settings.save({"tts": {"base_url": "https://api.openai.com/v1", "api_key": "sk-new"}})
    assert hq_calls_settings.get()["tts"]["api_key"] == "sk-new"
    hq_calls_settings.save({"tts": {"base_url": "http://other.lan/v1", "api_key": None}})
    assert hq_calls_settings.get()["tts"]["api_key"] == ""


def test_same_origin_path_change_keeps_the_token():
    hq_calls_settings.save(FULL)
    hq_calls_settings.save({"tts": {"base_url": "http://speech.lan:8880/v2"}})
    assert hq_calls_settings.get()["tts"]["api_key"] == "tts-secret-1"


@pytest.mark.parametrize(
    "bad",
    [
        {"stt": {"base_url": "ftp://x"}},
        {"stt": {"nope": 1}},
        {"voices": {"child": "x"}},
        {"turn_taking": "slow"},
        {"interrupt": "yes"},
        {"unknown": 1},
    ],
)
def test_invalid_patches_are_refused(bad):
    with pytest.raises(hq_calls_settings.SettingsError):
        hq_calls_settings.save(bad)


def test_resolve_test_token_only_for_its_origin():
    stored = {"base_url": TTS, "api_key": "tts-secret-1"}
    assert hq_calls_settings.resolve_test_token(stored, TTS, None) == ("tts-secret-1", None)
    assert hq_calls_settings.resolve_test_token(stored, "https://evil.example/v1", None) == (
        "",
        None,
    )
    assert (
        hq_calls_settings.resolve_test_token(stored, "https://evil.example/v1", "mine")[0] == "mine"
    )


# --- routes ------------------------------------------------------------------------------------


def test_routes_need_login_and_csrf(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get("/api/voice/settings").status_code == 401
    assert c.get("/api/voice/capabilities").status_code == 401
    hdr = _login(c, auth_cfg)
    for path in (
        "/api/voice/settings",
        "/api/voice/test",
        "/api/voice/calls",
        "/api/voice/preview",
    ):
        r = c.post(path, json={}, headers={"Origin": auth_cfg.origin})
        assert r.status_code == 403, path
    assert c.post("/api/voice/settings", json={"captions": False}, headers=hdr).status_code == 200


def test_settings_route_masks_tokens(app):
    c, hdr, _ = app
    r = c.post("/api/voice/settings", json=FULL, headers=hdr)
    assert r.status_code == 200
    assert "tts-secret-1" not in r.text
    assert "tts-secret-1" not in c.get("/api/voice/settings").text


def test_draft_test_sends_the_stored_token_only_to_its_origin(app, monkeypatch):
    c, hdr, _ = app
    c.post("/api/voice/settings", json=FULL, headers=hdr)
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request):
        seen.append(r)
        return httpx.Response(404)

    monkeypatch.setattr(speech_endpoints, "_TRANSPORT", httpx.MockTransport(handler))
    body = {
        "stt": {"base_url": STT},
        "tts": {"base_url": "https://evil.example/v1"},
        "voice": "aria",
    }
    r = c.post("/api/voice/test", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["usable"] is False
    assert seen and all("tts-secret-1" not in r.headers.get("authorization", "") for r in seen)
    seen.clear()
    body["tts"] = {"base_url": TTS}
    c.post("/api/voice/test", json=body, headers=hdr)
    tts_calls = [r for r in seen if r.url.port == 8880]
    assert tts_calls and all(r.headers["authorization"] == "Bearer tts-secret-1" for r in tts_calls)


def test_capabilities_say_why_not(app, monkeypatch):
    c, hdr, _ = app
    caps = c.get("/api/voice/capabilities").json()
    assert caps["available"] is False and "set up calls" in caps["reason"]
    c.post("/api/voice/settings", json=FULL, headers=hdr)

    async def not_ready():
        return False, "the voice runtime is not installed (no pipecat)"

    monkeypatch.setattr(voice_calls, "runtime_ready", not_ready)
    caps = c.get("/api/voice/capabilities").json()
    assert caps["available"] is False and "runtime" in caps["reason"]
    assert caps["destinations"]["stt"] == "lan" and caps["destinations"]["tts"] == "lan"


@pytest.fixture
def ready(app, monkeypatch):
    c, hdr, cfg = app
    c.post("/api/voice/settings", json=FULL, headers=hdr)
    prefs.set_ai_review(
        {"enabled": True, "base_url": "https://ai.test/v1", "api_key": "k", "model": "m"}
    )

    async def yes():
        return True, ""

    monkeypatch.setattr(voice_calls, "runtime_ready", yes)
    return c, hdr, cfg


def test_one_call_at_a_time_and_hang_up(ready):
    c, hdr, _ = ready
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 201
    call = r.json()
    assert call["state"] == "dialing" and ask_store.exists(call["conversation_id"])
    assert c.post("/api/voice/calls", json={}, headers=hdr).status_code == 409
    assert c.delete(f"/api/voice/calls/{call['id']}", headers=hdr).json()["state"] == "ended"
    assert c.post("/api/voice/calls", json={}, headers=hdr).status_code == 201


def test_the_contact_picks_the_voice(ready):
    c, hdr, _ = ready
    c.post("/api/voice/calls", json={"contact": "russia-man"}, headers=hdr)
    assert voice_calls.REGISTRY.current.voice == "atlas"


# --- the socket --------------------------------------------------------------------------------


def _admit(c, hdr) -> dict:
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


def _ws(c, cfg, origin=None) -> dict:
    """The session cookie as a header: TestClient's websocket_connect does not reliably forward
    the cookie jar (see test_webterm._login_headers)."""
    return {
        "Origin": origin or cfg.origin,
        "Cookie": f"agent_sessions={c.cookies.get('agent_sessions')}",
    }


def _closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        for _ in range(10):
            ws.receive_text()
    return e.value.code


def test_socket_refuses_without_session(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    with c.websocket_connect("/ws/voice/x", headers={"Origin": auth_cfg.origin}) as ws:
        assert _closed_with(ws) == 4401


def test_socket_refuses_a_foreign_origin(ready):
    c, hdr, cfg = ready
    call = _admit(c, hdr)
    with c.websocket_connect(
        f"/ws/voice/{call['id']}", headers=_ws(c, cfg, "https://evil.example")
    ) as ws:
        assert _closed_with(ws) == 4403


def test_socket_refuses_an_unknown_call(ready):
    c, _, cfg = ready
    with c.websocket_connect("/ws/voice/nope", headers=_ws(c, cfg)) as ws:
        assert _closed_with(ws) == 4404


def test_socket_needs_the_csrf_hello_before_a_worker_starts(ready, monkeypatch):
    c, hdr, cfg = ready
    started = []

    async def never(settings):
        started.append(1)
        raise AssertionError("no worker before hello")

    monkeypatch.setattr(voice_calls, "start_worker", never)
    call = _admit(c, hdr)
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": "wrong"}))
        assert _closed_with(ws) == 4403
    assert started == []
    # A refused socket does not end the call: it stays its own session's to join.
    assert voice_calls.REGISTRY.current.state == "dialing"


def test_a_call_nobody_joins_ends(monkeypatch):
    monkeypatch.setattr(voice_calls, "DIAL_TIMEOUT_S", 0.05)
    call = voice_calls.Call(
        id="c", cid="x", contact="off", voice="v", operator=None, app=None, registry=None
    )
    asyncio.run(voice_calls.end_if_never_joined(call))
    assert call.state == "ended" and call.ended_reason == "not_connected"


def test_another_session_cannot_take_the_call_but_can_hang_up(ready, auth_cfg, monkeypatch):
    """BattleLab has one operator; a call's audio belongs to the SESSION that admitted it. A second
    signed-in session (another device) cannot open its socket, but may hang it up."""
    c, hdr, cfg = ready
    call = _admit(c, hdr)
    started = []

    async def never(settings):
        started.append(1)
        raise AssertionError("no worker for a foreign session")

    monkeypatch.setattr(voice_calls, "start_worker", never)
    other = TestClient(create_app(auth_cfg), base_url="https://testserver")
    other_hdr = _login(other, auth_cfg)
    with other.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(other, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": other_hdr["X-CSRF-Token"]}))
        assert _closed_with(ws) == 4403
    assert started == []
    assert voice_calls.REGISTRY.current.state == "dialing"  # still its own session's call
    r = other.delete(f"/api/voice/calls/{call['id']}", headers=other_hdr)
    assert r.status_code == 200 and r.json()["state"] == "ended"


# --- the bridge, end to end with a fake worker -------------------------------------------------

STORED = "**Two** auth tests fail in [api-rebase](http://x/s/1). I drafted a nudge."


class FakeWorker:
    """Stands in for the Pipecat worker on a real Unix socket: says it is ready, turns the first
    audio frame into an utterance, and records every `say`."""

    def __init__(self):
        self.said: list[str] = []
        self.config: dict | None = None
        self.server = None

    async def handler(self, conn):
        self.config = json.loads(await conn.recv())
        await conn.send(json.dumps({"t": "ready"}))
        sent = False
        async for msg in conn:
            if isinstance(msg, bytes):
                if not sent:
                    sent = True
                    await conn.send(json.dumps({"t": "utterance", "text": "How is api rebase?"}))
                continue
            ev = json.loads(msg)
            if ev.get("t") == "say":
                self.said.append(ev["text"])
                await conn.send(b"\x01\x00" * 100)
                # In order after the audio, as the real worker's non-urgent marker (P2).
                await conn.send(json.dumps({"t": "said", "id": ev.get("id")}))
            if ev.get("t") == "stop":
                return

    async def start(self, settings):
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="bl-voice-test-"))
        sock = d / "w.sock"
        self.server = await websockets.serve(self.handler, unix=True, path=str(sock))
        fake = self

        class W:
            def __init__(self):
                self.sock = sock

            async def stop(self):
                fake.server.close()

        return W()


@pytest.fixture
def worker(ready, monkeypatch):
    fw = FakeWorker()
    monkeypatch.setattr(voice_calls, "start_worker", fw.start)
    seen: dict = {}

    async def fake_begin(cid, turn_id, text, **kw):
        seen.update(kw, text=text, cid=cid)
        return None, {
            "turn_id": turn_id,
            "status": "done",
            "answer": STORED,
            "proposals": [
                {
                    "kind": "nudge",
                    "title": "api-rebase",
                    "action_id": "a1",
                    "text": "Rerun the auth tests.",
                }
            ],
            "ask": None,
            "via": "call",
        }

    monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
    return fw, seen


def test_a_call_asks_hq_and_speaks_the_stored_answer(ready, worker):
    c, hdr, cfg = ready
    fw, seen = worker
    call = _admit(c, hdr)
    got: list = []
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        while True:
            m = ws.receive()
            if m.get("text") and json.loads(m["text"]).get("t") == "connected":
                break
        ws.send_bytes(b"\x00" * 640)
        captions = []
        for _ in range(60):
            m = ws.receive()
            if m.get("bytes") is not None:
                got.append(m["bytes"])
                continue
            if m.get("text") is None:
                break  # the socket closed
            ev = json.loads(m["text"])
            if ev.get("t") == "caption":
                captions.append(ev)
            if ev.get("t") == "caption" and ev.get("kind") == "readback":
                break
        ws.send_text(json.dumps({"t": "end"}))
    # The worker was told no token: those ride its environment, never the socket.
    assert "api_key" not in json.dumps(fw.config)
    assert fw.config["tts"]["voice"] == "aria"
    # An ordinary HQ turn, from the call, in the call's conversation, with the call's contact.
    assert seen["via"] == "call" and seen["contact"] == "asia-woman"
    assert seen["text"] == "How is api rebase?" and seen["cid"] == call["conversation_id"]
    # What was spoken: the greeting, then exactly the stored answer as words, then the fixed
    # card sentence — nothing else.
    spoken = [s for s in fw.said if not s.startswith("HQ,")]
    assert (
        spoken[0]
        == voice_text.speakable(STORED)
        == "Two auth tests fail in api-rebase. I drafted a nudge."
    )
    # P2: a confirmable card is read back (fixed words) instead of a plain "on your screen".
    assert spoken[1] == (
        "A message to api-rebase: Rerun the auth tests. Say confirm to send it, or cancel."
    )
    assert any(cap["who"] == "you" and cap["text"] == "How is api rebase?" for cap in captions)
    assert got, "speech audio reaches the client"


def test_the_browsers_played_ack_arms_the_challenge_through_the_socket(ready, worker):
    """#1397 P2 through the real socket: the read-back's `said` reaches the browser, the browser
    acks `played`, and only then is the card offered for a spoken confirmation. (A route that
    dropped `played` once left every challenge unarmed while the unit tests stayed green.)"""
    c, hdr, cfg = ready
    call = _admit(c, hdr)
    events: list[dict] = []
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        readback = None
        for _ in range(200):
            m = ws.receive()
            if m.get("text") is None:
                if m.get("bytes") is not None:
                    continue
                break
            ev = json.loads(m["text"])
            events.append(ev)
            if ev.get("t") == "connected":
                ws.send_bytes(b"\x00" * 640)
            if ev.get("t") == "caption" and ev.get("kind") == "readback":
                readback = ev["id"]
            if ev.get("t") == "said" and ev.get("id") == readback:
                assert not any(e.get("t") == "challenge" and e.get("cards") for e in events)
                ws.send_text(json.dumps({"t": "played", "id": readback}))
                # A bounded end marker: the socket handles messages in order, so `muted` comes
                # back only after `played` was handled (and its challenge, if any, was sent).
                ws.send_text(json.dumps({"t": "mute", "on": False}))
            if ev.get("t") == "muted":
                break
        ws.send_text(json.dumps({"t": "end"}))
    assert readback is not None
    offered = [e for e in events if e.get("t") == "challenge"]
    assert offered == [{"t": "challenge", "cards": ["a1"], "expires_s": voice_calls.CHALLENGE_S}]


def test_the_needs_you_chip_reaches_the_call_through_the_socket(ready, worker):
    """The chip's {"t": "needs_you"} is dispatched by the socket to the Needs-you read-out."""
    c, hdr, cfg = ready
    call = _admit(c, hdr)
    events: list[dict] = []
    deadline = time.monotonic() + 15
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        for _ in range(2000):
            m = ws.receive()
            if m.get("text") is None:
                if m.get("bytes") is not None:
                    continue
                break
            ev = json.loads(m["text"])
            events.append(ev)
            if ev.get("t") == "connected":
                ws.send_text(json.dumps({"t": "needs_you"}))
                ws.send_text(json.dumps({"t": "mute", "on": False}))  # bounded end marker
            if ev.get("t") == "caption" and ev.get("kind") == "outcome":
                break
            if ev.get("t") == "muted":
                # The read-out runs beside the relay loop (it reads live screens): keep the
                # socket turning until it speaks, for at most 15 s.
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
                ws.send_text(json.dumps({"t": "mute", "on": False}))
        ws.send_text(json.dumps({"t": "end"}))
    outcome = [e["text"] for e in events if e.get("t") == "caption" and e.get("kind") == "outcome"]
    assert outcome and "needs you" in outcome[0]


# --- spoken text -------------------------------------------------------------------------------


def test_speakable_keeps_the_words_and_drops_markup():
    assert voice_text.speakable("## Status\n- **one** `two`\n- see https://x.y/z") == (
        "Status one two see a link"
    )


def test_split_speech_keeps_every_word():
    text = (
        "It finished the rebase on main twelve minutes ago, and two auth tests fail; I have "
        "drafted a nudge to rerun them and fix the failures, it is on your screen. Done?"
    )
    parts = voice_text.split_speech(text)
    assert " ".join(parts).split() == text.split()
    assert all(len(p.split()) <= voice_text.CHUNK_WORDS for p in parts)


@pytest.mark.parametrize(
    "said,idx",
    [("the second one", 1), ("option two", 1), ("No", 1), ("first", 0), ("one or two", None),
     ("do whatever you think is best", None),
     # #1626 review: "one" as a pronoun is not option one.
     ("No, not that one.", None), ("No. Just one.", None), ("that one", None), ("two", 1),
     ("the third", 2)],
)  # fmt: skip
def test_pick_option(said, idx):
    assert voice_text.pick_option(said, ["Yes", "No", "Later"]) == idx


# --- the store and the prompt ------------------------------------------------------------------


def test_via_round_trips_and_unknown_channels_are_refused():
    cid = ask_store.create()
    ask_store.append(cid, {"type": "user", "turn_id": "11111111-1111-4111-8111-111111111111",
                           "text": "hi", "qd": "0" * 64, "via": "call"})  # fmt: skip
    views = ask_orchestrator.turn_views(ask_store.read(cid))
    assert views[0]["via"] == "call"
    with pytest.raises(ValueError):
        ask_store._admit({"type": "user", "turn_id": "11111111-1111-4111-8111-111111111112",
                          "text": "x", "qd": "0" * 64, "via": "pigeon"}, [])  # fmt: skip


def test_typed_turns_have_no_via():
    cid = ask_store.create()
    ask_store.append(cid, {"type": "user", "turn_id": "11111111-1111-4111-8111-111111111113",
                           "text": "hi", "qd": "0" * 64})  # fmt: skip
    assert ask_orchestrator.turn_views(ask_store.read(cid))[0]["via"] is None


def test_call_turns_send_hq_call_between_persona_and_guarded_prompt():
    turn = ask_orchestrator._Turn(
        cid="c", turn_id="t", question="q", working_keys=set(), progress=lambda e: None,
        contact="asia-woman", via="call",
    )  # fmt: skip
    msgs = [*ask_orchestrator._persona(turn), *ask_orchestrator._call_style(turn)]
    assert msgs[0]["content"] == prompts.effective(hq_contacts.prompt_id("asia-woman"))
    assert msgs[1]["content"] == prompts.effective("hq_call")
    typed = ask_orchestrator._Turn(
        cid="c", turn_id="t", question="q", working_keys=set(), progress=lambda e: None
    )
    assert ask_orchestrator._call_style(typed) == []


def test_every_contact_has_a_voice_group():
    assert {c.voice_group for c in hq_contacts.CONTACTS} == set(hq_contacts.VOICE_GROUPS)


def test_worker_environment_is_sanitized(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "app-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "agent-secret")
    env = voice_calls._worker_env({"VOICE_TTS_TOKEN": "t"})
    assert "AGENT_SESSIONS_SECRET_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["VOICE_TTS_TOKEN"] == "t"


# --- second review of #1626 -----------------------------------------------------------------


class _RefusingWorker:
    """A worker whose socket answers the WebSocket handshake with HTTP 500."""

    def __init__(self):
        self.stopped = 0
        self.server = None

    async def start(self, settings):
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="bl-voice-test-"))
        sock = d / "w.sock"

        async def refuse(reader, writer):
            await reader.read(4096)
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\ncontent-length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_unix_server(refuse, path=str(sock))
        fake = self

        class W:
            def __init__(self):
                self.sock = sock

            async def stop(self):
                fake.stopped += 1
                fake.server.close()

        return W()


def test_a_failed_worker_handshake_stops_it_and_frees_the_registry(ready, monkeypatch):
    c, hdr, cfg = ready
    fw = _RefusingWorker()
    monkeypatch.setattr(voice_calls, "start_worker", fw.start)
    call = _admit(c, hdr)
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        for _ in range(20):
            m = ws.receive()
            if m.get("type") == "websocket.close":
                break
    assert fw.stopped == 1
    assert voice_calls.REGISTRY.current.state == "ended"
    _admit(c, hdr)  # not 409 "busy"


def test_a_second_socket_in_the_hello_window_starts_no_second_worker(ready, worker):
    c, hdr, cfg = ready
    fw, _ = worker
    starts = []
    orig = voice_calls.start_worker

    async def counting(settings):
        starts.append(1)
        return await orig(settings)

    voice_calls.start_worker = counting
    try:
        call = _admit(c, hdr)
        url = f"/ws/voice/{call['id']}"
        with (
            c.websocket_connect(url, headers=_ws(c, cfg)) as a,
            c.websocket_connect(url, headers=_ws(c, cfg)) as b,
        ):
            # Both passed the pre-hello check; A says hello first and claims the call.
            a.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
            for _ in range(50):
                m = a.receive()
                if m.get("text") and json.loads(m["text"]).get("t") == "connected":
                    break
            b.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
            closed = b.receive()
            assert closed.get("type") == "websocket.close" and closed.get("code") == 4409
            a.send_text(json.dumps({"t": "end"}))
    finally:
        voice_calls.start_worker = orig
    assert starts == [1]


def test_any_turn_error_is_spoken_and_the_queue_runs(monkeypatch):
    said: list = []
    call = voice_calls.Call(
        id="c", cid="x", contact="europe-man", voice="v", operator=None, app=None,
        registry=None, state="listening",
    )  # fmt: skip

    async def to_worker(m):
        if m.get("t") == "say":
            said.append(m["text"])

    async def to_client(m):
        pass

    call._to_worker, call._to_client = to_worker, to_client
    asked: list = []

    async def boom(turn_id, text, answers, option):
        asked.append(text)
        if text == "first":
            raise RuntimeError("store read failed")
        return {"status": "done", "answer": "Second answer.", "turn_id": turn_id}

    monkeypatch.setattr(call, "_run_turn", boom)

    async def go():
        call.queued = "second"
        await call._ask("first", call.generation)
        while call.turn_task is not None and not call.turn_task.done():
            await call.turn_task

    asyncio.run(go())
    assert asked == ["first", "second"]
    assert any(s.startswith("Sorry, Commander") for s in said) and "Second answer." in said
    assert call.state == "listening" and call.queued is None


def test_odd_ids_and_hellos_are_refused_not_crashed(ready):
    c, hdr, cfg = ready
    r = c.delete("/api/voice/calls/%C3%A4", headers=hdr)
    assert r.status_code == 404
    call = _admit(c, hdr)
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text("[1]")
        m = ws.receive()
        assert m.get("type") == "websocket.close" and m.get("code") in (4400, 4403)


def test_the_home_free_proxy_drops_header_values_that_would_smuggle_lines():
    from agent_sessions.homefree import appproxy

    t = appproxy.AppProxyTarget(app_port=1)
    out = t._rewrite_headers(
        {"X-Foo": "a\r\nX-BattleLab-Home-Free: 0", "X-Ok": "fine", "X-Nul": "a\0b"}
    )
    assert "X-Foo" not in out and "X-Nul" not in out and out["X-Ok"] == "fine"
    assert out[appproxy.HOME_FREE_HEADER] == "1"
    assert not any("\r" in v or "\n" in v for v in out.values())


# --- Hermes review 6842 ------------------------------------------------------------------------


def test_split_speech_cuts_long_unpunctuated_runs():
    text = " ".join(f"w{i}" for i in range(42))
    parts = voice_text.split_speech(text, first_max=voice_text.CHUNK_WORDS // 2)
    assert " ".join(parts).split() == text.split()
    assert all(len(p.split()) <= voice_text.CHUNK_WORDS for p in parts)


def _bare_call(**kw):
    return voice_calls.Call(id="c", cid="x", contact="europe-man", voice="v", operator=None,
                            app=None, registry=None, state="listening", **kw)  # fmt: skip


def test_a_stalled_send_cannot_keep_a_call_from_ending(monkeypatch):
    monkeypatch.setattr(voice_calls, "SEND_BUDGET_S", 0.05)
    call = _bare_call()

    async def stuck(m):
        await asyncio.sleep(3600)

    call._to_worker = call._to_client = stuck

    async def go():
        await asyncio.wait_for(call.end("hung_up"), 2)
        assert call._ended.is_set() and call.torn_down.is_set()  # no worker ever started

    asyncio.run(go())


def test_the_next_call_waits_for_the_last_ones_teardown():
    reg = voice_calls.Registry()

    async def go():
        first = await reg.admit(cid="a", contact="europe-man", voice="v", operator=None,
                                app=None, registry=reg)  # fmt: skip
        first.worker_started = True
        await first.end("hung_up")
        with pytest.raises(voice_calls.CallError) as e:
            await reg.admit(cid="b", contact="europe-man", voice="v", operator=None,
                            app=None, registry=reg)  # fmt: skip
        assert e.value.status == 409 and "hanging up" in e.value.detail
        first.mark_torn_down()
        await reg.admit(cid="b", contact="europe-man", voice="v", operator=None, app=None,
                        registry=reg)  # fmt: skip

    asyncio.run(go())


def test_signing_out_anywhere_ends_the_call(ready):
    c, hdr, cfg = ready
    _admit(c, hdr)
    r = c.post("/logout", headers=hdr, follow_redirects=False)
    assert r.status_code == 303
    assert voice_calls.REGISTRY.current.state == "ended"
    assert voice_calls.REGISTRY.current.ended_reason == "signed_out"


def test_a_busy_call_creates_no_conversation(ready, monkeypatch):
    c, hdr, cfg = ready
    _admit(c, hdr)
    created = []
    real = ask_store.create
    monkeypatch.setattr(ask_store, "create", lambda: created.append(1) or real())
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 409 and created == []


@pytest.mark.parametrize("mode,pid", [("brief", "hq_call"), ("detailed", "hq_call_detailed")])
def test_the_answers_setting_picks_the_call_prompt(ready, worker, mode, pid):
    c, hdr, cfg = ready
    fw, seen = worker
    c.post("/api/voice/settings", json={"answers": mode}, headers=hdr)
    call = _admit(c, hdr)
    assert voice_calls.REGISTRY.get(call["id"]).answers == mode
    turn = ask_orchestrator._Turn(cid="x", turn_id="t", question="q", working_keys=set(),
                                  via="call", call_answers=mode,
                                  progress=lambda ev: None)  # fmt: skip
    assert ask_orchestrator._call_style(turn)[0]["content"] == prompts.effective(pid)


def test_worker_stop_takes_its_children_with_it(tmp_path):
    import os
    import sys

    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import os, time; os.fork(); time.sleep(60)",
            start_new_session=True,
        )  # fmt: skip
        await asyncio.sleep(0.5)
        pgid = proc.pid
        w = voice_calls.Worker(proc, tmp_path / "w.sock", tmp_path)
        await w.stop()
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)

    asyncio.run(go())


def test_a_stuck_runtime_probe_is_killed(monkeypatch, tmp_path):
    import sys

    procs = []
    real = asyncio.create_subprocess_exec

    async def record(*a, **kw):
        p = await real(sys.executable, "-c", "import time; time.sleep(60)",
                       stdout=kw.get("stdout"), stderr=kw.get("stderr"))  # fmt: skip
        procs.append(p)
        return p

    monkeypatch.setattr(voice_calls, "RUNTIME_PROBE_S", 0.3)
    monkeypatch.setattr(voice_calls.asyncio, "create_subprocess_exec", record)
    monkeypatch.setattr(voice_calls, "_RUNTIME_CACHE", {})
    ok, why = asyncio.run(voice_calls.runtime_ready())
    assert not ok and procs and procs[0].returncode is not None


# --- Hermes review 6845 ------------------------------------------------------------------------


def test_a_cancelled_startup_still_frees_the_call(ready, monkeypatch):
    c, hdr, cfg = ready

    async def cancelled(settings):
        raise asyncio.CancelledError  # as if the handler was cancelled mid-startup

    monkeypatch.setattr(voice_calls, "start_worker", cancelled)
    call = _admit(c, hdr)
    with (
        contextlib.suppress(Exception),
        c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws,
    ):
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        for _ in range(20):
            if ws.receive().get("type") == "websocket.close":
                break
    cur = voice_calls.REGISTRY.current
    assert cur.state == "ended" and cur.torn_down.is_set()
    _admit(c, hdr)  # the slot is free


def test_start_worker_cancelled_while_waiting_kills_the_worker(monkeypatch, tmp_path):
    import os
    import sys

    procs = []
    real = asyncio.create_subprocess_exec

    async def spawn(*a, **kw):
        kw = {k: v for k, v in kw.items() if k in ("stdin", "stdout", "stderr", "cwd",
                                                    "start_new_session")}  # fmt: skip
        p = await real(sys.executable, "-c", "import time; time.sleep(60)", **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(voice_calls.asyncio, "create_subprocess_exec", spawn)

    async def go():
        task = asyncio.create_task(voice_calls.start_worker(
            {"stt": {"api_key": ""}, "tts": {"api_key": ""}}))  # fmt: skip
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.killpg(procs[0].pid, 0)

    asyncio.run(go())


@pytest.mark.parametrize("err", [OSError("disk"), RuntimeError("bug")])
def test_a_failed_conversation_create_frees_the_call(ready, monkeypatch, err):
    c, hdr, cfg = ready

    real = ask_store.create

    def boom(cid=None):
        raise err

    monkeypatch.setattr(ask_store, "create", boom)
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 500
    monkeypatch.setattr(ask_store, "create", real)  # never undo(): it drops the fixtures' pins
    for _ in range(50):
        cur = voice_calls.REGISTRY.current
        if cur is not None and cur.state == "ended" and cur.torn_down.is_set():
            break
        import time as _t

        _t.sleep(0.02)
    _admit(c, hdr)  # not stranded


def test_a_cancelled_runtime_probe_is_killed(monkeypatch):
    import sys

    procs = []
    real = asyncio.create_subprocess_exec

    async def record(*a, **kw):
        p = await real(sys.executable, "-c", "import time; time.sleep(60)",
                       stdout=kw.get("stdout"), stderr=kw.get("stderr"))  # fmt: skip
        procs.append(p)
        return p

    monkeypatch.setattr(voice_calls.asyncio, "create_subprocess_exec", record)
    monkeypatch.setattr(voice_calls, "_RUNTIME_CACHE", {})

    async def go():
        task = asyncio.create_task(voice_calls.runtime_ready())
        await asyncio.sleep(0.4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert procs and procs[0].returncode is not None

    asyncio.run(go())


def test_barge_in_stops_the_active_synthesis():
    """Through the interruption path (`drop_prefetch`), with the generator still open: Pipecat
    clears playback on an interruption but does not close the running TTS generator."""
    pytest.importorskip("pipecat")
    from agent_sessions import voice_worker

    async def go():
        bus = voice_worker.Bus()
        tts = voice_worker.EndpointTTS(speech_endpoints.Endpoint("http://x/v1"), "v", bus)
        tts._sample_rate = 24000

        class Slow:
            def __init__(self, text):
                self.queue = asyncio.Queue()
                self.task = asyncio.create_task(self._run())

            async def _run(self):
                while True:
                    await self.queue.put(b"\0" * 9600)
                    await asyncio.sleep(0.01)

        tts._synths._make = Slow
        gen = tts.run_tts("hello", "ctx")
        await gen.__anext__()  # the first frame plays …
        synth = tts._synths.active
        tts.drop_prefetch()  # … the Commander barges in (the generator stays open)
        await asyncio.sleep(0)
        assert synth.task.cancelled() or synth.task.done()
        await gen.aclose()

    asyncio.run(go())


def test_an_error_after_the_claim_still_frees_the_call(ready, monkeypatch):
    """Review 6846: the claim, the dialing send and the settings read sit inside the teardown
    scope — a failure there must not strand the one call slot."""
    c, hdr, cfg = ready
    call = _admit(c, hdr)

    def broken(*a, **kw):
        raise OSError("prefs unreadable")

    monkeypatch.setattr(hq_calls_settings, "get", broken)
    with (
        contextlib.suppress(Exception),
        c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws,
    ):
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        for _ in range(20):
            if ws.receive().get("type") == "websocket.close":
                break
    cur = voice_calls.REGISTRY.current
    assert cur.state == "ended" and cur.torn_down.is_set()


def test_a_create_that_fails_after_writing_leaves_no_conversation(ready, monkeypatch):
    c, hdr, cfg = ready
    before = set(ask_store.root().glob("*.jsonl")) if ask_store.root().exists() else set()

    def sync_fails(path):
        raise OSError("fsync failed")

    monkeypatch.setattr(ask_store, "_sync_dir", sync_fails)
    r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
    assert r.status_code == 500
    import time as _t

    for _ in range(100):
        after = set(ask_store.root().glob("*.jsonl"))
        if after == before:
            break
        _t.sleep(0.02)
    assert set(ask_store.root().glob("*.jsonl")) == before


def test_the_worker_imports_this_release_through_its_shim(tmp_path):
    """Review 6847: a separate voice environment holds Pipecat but not the app; the per-call shim
    makes it run THIS release's package (docs/voice/runtime.md)."""
    import subprocess
    import sys
    from pathlib import Path

    shim = voice_calls._shim(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c", "import agent_sessions.voice_text as m; print(m.__file__)"],
        env=voice_calls._worker_env({}, shim=shim), cwd=str(tmp_path), capture_output=True,
        text=True, check=True,
    )  # fmt: skip
    assert out.stdout.strip().startswith(str(tmp_path / "agent_sessions"))
    assert (tmp_path / "agent_sessions").resolve() == Path(voice_calls.__file__).resolve().parent


def test_a_hang_up_while_the_socket_sets_up_never_frees_the_slot_early(ready, worker, monkeypatch):
    """Independent review of #1626: worker_started is set WITH the claim; a DELETE during the
    settings read must not let a second call in while this socket can still start a worker,
    and no worker starts for a call that already ended."""
    import threading

    c, hdr, cfg = ready
    call = _admit(c, hdr)
    gate, entered = threading.Event(), threading.Event()
    real_get = hq_calls_settings.get
    starts = []

    def slow_get(*a, **kw):
        entered.set()
        gate.wait(10)
        return real_get(*a, **kw)

    async def no_start(settings):
        starts.append(1)
        raise voice_calls.CallError(503, "should not start", "worker")

    monkeypatch.setattr(hq_calls_settings, "get", slow_get)
    monkeypatch.setattr(voice_calls, "start_worker", no_start)
    # No admission snapshot: the socket falls back to reading the settings, the await held here.
    voice_calls.REGISTRY.current.settings = None
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        assert entered.wait(10)
        monkeypatch.setattr(hq_calls_settings, "get", real_get)
        r = c.delete(f"/api/voice/calls/{call['id']}", headers=hdr)
        assert r.status_code == 200
        r = c.post("/api/voice/calls", json={"contact": "asia-woman"}, headers=hdr)
        assert r.status_code == 409  # still hanging up: this socket hasn't torn down
        gate.set()
        for _ in range(20):
            if ws.receive().get("type") == "websocket.close":
                break
    assert starts == []  # no worker for the ended call
    cur = voice_calls.REGISTRY.current
    assert cur.torn_down.is_set()
    _admit(c, hdr)


def test_the_worker_is_configured_from_the_settings_the_call_was_admitted_with(ready, worker):
    """Second review of #1635: the call's language gate and the worker's STT config come from ONE
    read — a settings save between admission and the socket must not split them."""
    c, hdr, cfg = ready
    fw, _ = worker
    call = _admit(c, hdr)  # admitted on Auto
    r = c.post("/api/voice/settings", json={"language": "de"}, headers=hdr)
    assert r.status_code == 200, r.text
    with c.websocket_connect(f"/ws/voice/{call['id']}", headers=_ws(c, cfg)) as ws:
        ws.send_text(json.dumps({"t": "hello", "csrf": hdr["X-CSRF-Token"]}))
        for _ in range(50):
            m = ws.receive()
            if m.get("text") and json.loads(m["text"]).get("t") == "connected":
                break
        ws.send_text(json.dumps({"t": "end"}))
    assert fw.config["language"] is None  # Auto, as admitted — not the later "de" hint
