"""#2341 P1 — calling an API session: the session leg of a call.

Pinned here:

* **admission** — only a session whose manifest takes structured turns is a party: a terminal
  session, an unknown engine, a retired one, an archived or read-only session is refused with a
  FIXED line; the route admits a session party without HQ's endpoint and creates no HQ
  conversation for it;
* **the leg** — a heard utterance becomes exactly ONE composer turn through the pane's own route,
  with ONE operation id; an uncertain outcome is said and never resent; a queued turn says so; the
  reply is spoken in the session's own voice, code and tables as FIXED lines;
* **fences** — the session's own line heard back is dropped as echo; nothing of the session's
  reaches HQ (no HQ turn, no record in HQ's conversation);
* **§1b, the party change** — in both directions a late reply, a late progress event, a late HQ
  answer and a late judge result are dropped, the old party's echo is still cut, and a switch
  racing a submission lets exactly one of the two act.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    asgi_call,
    ask_orchestrator,
    ask_store,
    hq_calls_settings,
    prefs,
    prompts,
    speech_endpoints,
    voice_calls,
    voice_session,
)
from agent_sessions.main import create_app

KEY = "claude-api:11111111-2222-4333-8444-555555555555"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(voice_calls, "REGISTRY", voice_calls.Registry())
    monkeypatch.setattr(voice_session, "FOLLOW_POLL_S", 0.01)
    monkeypatch.setattr(voice_session, "EVENTS_WAIT_S", 0.0)
    monkeypatch.setattr(voice_session, "SESSION_FIRST_PROGRESS_S", 0.0)
    monkeypatch.setattr(voice_session, "SESSION_PROGRESS_GAP_S", 0.0)


class FakeRoutes:
    """The pane's routes, as `asgi_call.call` reaches them in process: one API session."""

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.posted: list[dict] = []
        self.interrupts: list[dict] = []
        self.archived = False
        self.read_only = None
        self.bypass = False
        self.pending: list[dict] = []
        #: What the next submission answers: (status, body) or an exception to raise.
        self.answer: object = None
        self.decisions: list[dict] = []
        self.decision_answer: object = None
        self.hold: asyncio.Event | None = None

    def turn(self, op: str) -> dict:
        return next(t for t in self.turns if t["turn_id"] == op)

    async def call(self, app, operator, method, path, body, *, query="", **kw):
        R = asgi_call.Response
        if method == "GET" and path == f"/api/sessions/{KEY}":
            return R(
                200,
                {
                    "title": "docs-refresh",
                    "archived": self.archived,
                    "project": {"name": "agent-sessions"},
                },
            )
        if method == "GET" and path == f"/api/structured/sessions/{KEY}":
            active = next((t["turn_id"] for t in self.turns if t["state"] == "running"), None)
            return R(
                200,
                {
                    "turns": [dict(t) for t in self.turns],
                    "event_cursor": len(self.posted),
                    "active_turn": active,
                    "bypass": self.bypass,
                    "read_only": self.read_only,
                    "pending_requests": list(self.pending),
                },
            )
        if method == "GET" and path.endswith("/events"):
            await asyncio.sleep(0)
            return R(200, {"events": [], "next_cursor": 0})
        if method == "POST" and path == f"/api/structured/sessions/{KEY}/turns":
            self.posted.append(body)
            if self.hold is not None:
                await self.hold.wait()
            answer, self.answer = self.answer, None
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return R(*answer)
            t = {"turn_id": body["operation_id"], "state": "running", "reply": None, "tools": []}
            self.turns.append(t)
            return R(202, {"state": "running", "handoff": "sent", "turn_id": t["turn_id"]})
        if method == "POST" and path.endswith("/decisions"):
            self.decisions.append(body)
            answer, self.decision_answer = self.decision_answer, None
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return R(*answer)
            self.pending = [p for p in self.pending if p["request_id"] != body["request_id"]]
            return R(200, {"decision_id": body["decision_id"]})
        if method == "POST" and path.endswith("/interrupt"):
            self.interrupts.append(body)
            for t in self.turns:
                if t["turn_id"] == body["turn_id"]:
                    t["state"] = "interrupted"
            return R(202, {})
        return R(404, {"detail": "no such route"})


@pytest.fixture
def routes(monkeypatch):
    fake = FakeRoutes()
    monkeypatch.setattr(voice_session.asgi_call, "call", fake.call)
    return fake


@pytest.fixture
def hq_configured():
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test/v1",
            "api_key": "k",
            "model": "m",
        }
    )


class Wire:
    def __init__(self) -> None:
        self.client: list = []
        self.worker: list = []

    def said(self) -> list[str]:
        return [m["text"] for m in self.worker if isinstance(m, dict) and m.get("t") == "say"]

    def says(self) -> list[dict]:
        return [m for m in self.worker if isinstance(m, dict) and m.get("t") == "say"]


def _leg(**kw) -> voice_session.SessionLeg:
    return voice_session.SessionLeg(
        key=KEY,
        title="docs-refresh",
        engine="Claude",
        project="agent-sessions",
        voice="sol",
        **kw,
    )


def _call(leg=None):
    wire = Wire()
    call = voice_calls.Call(
        id="c",
        cid=ask_store.new_cid(),
        contact="europe-man",
        voice="aria",
        operator=None,
        app=None,
        registry=None,
        state="listening",
    )

    async def to_client(m):
        wire.client.append(m)

    async def to_worker(m):
        wire.worker.append(m)

    call._to_client, call._to_worker = to_client, to_worker
    if leg is not None:
        call.leg, call.party_gen, call.hq_created = leg, 1, False
    return call, wire


async def _until(pred, t: float = 2.0) -> None:
    end = time.monotonic() + t
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0.005)


def _heard(call, text: str, turn: int):
    """The worker hears `text` in its own turn `turn` (started now)."""

    async def go():
        await call.on_worker_event({"t": "user_started", "turn": turn})
        await call.on_worker_event({"t": "user_stopped", "turn": turn, "at": time.monotonic()})
        await call.on_worker_event({"t": "utterance", "text": text, "turn": turn})
        await call.on_worker_event({"t": "turn_end", "turn": turn})

    return go()


# --- the reply as words, tool kinds, phrases, voices -------------------------------------------


def test_a_reply_is_spoken_without_markup_with_code_and_tables_as_fixed_lines():
    reply = (
        "## Done\n\nI fixed **two** tests. Here is the patch:\n\n```diff\n- a\n+ b\n```\n\n"
        "| file | result |\n|---|---|\n| a.py | ok |\n\nSee https://example.com/x for more."
    )
    out = voice_session.speakable_reply(reply)
    assert out == (
        "Done I fixed two tests. Here is the patch: I've put the code in the chat. "
        "I've put a table in the chat. See a link for more."
    )
    assert "```" not in out and "|" not in out


def test_a_long_reply_is_cut_at_a_sentence_and_says_the_rest_is_in_the_chat():
    reply = " ".join(f"Sentence number {i} is here." for i in range(200))
    out = voice_session.speakable_reply(reply)
    assert len(out) <= voice_session.SESSION_SPOKEN_MAX + len(voice_session.REST_LINE) + 1
    assert out.endswith("is here. " + voice_session.REST_LINE)


def test_only_the_last_message_of_a_reply_is_spoken():
    turn = {"reply": "Looking at the tests.\n\nAll green now.", "reply_breaks": [0, 23]}
    assert voice_session.last_message(turn) == "All green now."
    assert voice_session.last_message({"reply": "Only one."}) == "Only one."


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Bash", "run"),
        ("execute", "run"),
        ("Read", "read"),
        ("read_file", "read"),
        ("Edit", "edit"),
        ("apply_patch", "edit"),
        ("Grep", "search"),
        ("Glob", "search"),
        ("WebFetch", "web"),
        ("Task", "subagent"),
        ("mystery", "other"),
        (None, "other"),
    ],
)
def test_a_tool_call_is_said_by_its_kind_never_its_text(name, kind):
    assert voice_session.tool_kind(name) == kind
    assert voice_session.PROGRESS_LINES[kind]


@pytest.mark.parametrize(
    ("said", "back"),
    [
        ("Back to HQ.", True),
        ("put me back to HQ please", True),
        ("HQ", True),
        ("Zurück zu HQ, bitte.", True),
        ("back to headquarters", True),
        ("go back to h q", True),
        ("Tell HQ about the tests", False),
        ("back to the tests", False),
        ("HQ said the tests are back and we should go now please", False),
    ],
)
def test_back_to_hq_is_a_short_phrase_ending_on_hq(said, back):
    assert voice_session.is_back_to_hq(said) is back


def test_a_session_voice_is_stable_never_hqs_and_shared_only_without_another():
    block = {"tts_voices": ["aria", "sol", "atlas", "nova"], "voices": {"woman": "aria"}}
    v, shared = voice_session.session_voice(block, KEY, "aria")
    assert v != "aria" and not shared
    assert voice_session.session_voice(block, KEY, "aria") == (v, False)
    assert voice_session.session_voice({**block, "session_voices": {KEY: "nova"}}, KEY, "aria") == (
        "nova",
        False,
    )
    # A kept pick that is HQ's voice on this call is not used.
    assert (
        voice_session.session_voice({**block, "session_voices": {KEY: "aria"}}, KEY, "aria")[0]
        != "aria"
    )
    one = {"tts_voices": [], "voices": {"woman": "aria", "man": "aria"}}
    assert voice_session.session_voice(one, KEY, "aria") == ("aria", True)


def test_the_session_voice_pick_is_kept_bounded_and_out_of_the_settings_view():
    hq_calls_settings.set_session_voice(KEY, "sol")
    assert hq_calls_settings.get()["session_voices"] == {KEY: "sol"}
    assert "session_voices" not in hq_calls_settings.public()
    with pytest.raises(hq_calls_settings.SettingsError):
        hq_calls_settings.set_session_voice("not a key", "sol")


# --- admission ---------------------------------------------------------------------------------


def _resolve(key):
    block = {"tts_voices": ["aria", "sol"], "voices": {"woman": "aria", "man": "atlas"}}
    return asyncio.run(voice_session.resolve(None, None, key, block, "aria"))


def test_an_api_session_is_admitted_with_its_title_project_engine_and_voice(routes):
    leg = _resolve(KEY)
    assert (leg.key, leg.title, leg.project, leg.voice) == (
        KEY,
        "docs-refresh",
        "agent-sessions",
        "sol",
    )
    assert leg.engine and not leg.voice_shared


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("claude:11111111-2222-4333-8444-555555555555", "not_structured"),
        ("nope:abc", "unknown"),
        ("claude-api:not a uuid\n", "unknown"),
        (123, "unknown"),
    ],
)
def test_admission_refuses_what_is_not_an_api_session(routes, key, why):
    with pytest.raises(voice_session.Unavailable) as e:
        _resolve(key)
    assert e.value.why == why
    assert e.value.line == "That session can't take calls right now."


def test_admission_refuses_a_retired_engine(routes, monkeypatch):
    monkeypatch.setattr(voice_session.engines.registry, "admits", lambda prov: False)
    with pytest.raises(voice_session.Unavailable) as e:
        _resolve(KEY)
    assert e.value.why == "retired"


@pytest.mark.parametrize("attr", ["archived", "read_only"])
def test_admission_refuses_an_archived_or_read_only_session_with_its_fixed_line(routes, attr):
    setattr(routes, attr, True if attr == "archived" else "retiring")
    with pytest.raises(voice_session.Unavailable) as e:
        _resolve(KEY)
    assert e.value.line == "docs-refresh can't take calls right now."


# --- the leg -----------------------------------------------------------------------------------


def test_an_utterance_is_one_composer_turn_with_one_operation_id_and_the_reply_is_spoken(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Run the auth tests.", 1)
        await _until(lambda: len(routes.posted) == 1)
        body = routes.posted[0]
        assert UUID_RE.match(body["operation_id"])
        # The first turn carries the registered call instruction, visibly marked, then the words.
        assert body["text"] == (
            f"[on a call] {prompts.effective('session_call')}\n\nRun the auth tests."
        )
        t = routes.turn(body["operation_id"])
        t["tools"] = [{"id": "x1", "name": "Bash"}]
        await _until(lambda: "Running a command." in wire.said())
        t.update(state="completed", reply="Both pass now.\n\n```\nok\n```")
        await _until(lambda: "Both pass now. I've put the code in the chat." in wire.said())
        say = next(m for m in wire.says() if m["text"].startswith("Both pass"))
        assert say["voice"] == "sol"  # the session's own voice, never the contact's
        # A second utterance: the mark alone, a new operation id.
        await _heard(call, "And the web ones?", 2)
        await _until(lambda: len(routes.posted) == 2)
        assert routes.posted[1]["text"] == "[on a call] And the web ones?"
        assert routes.posted[1]["operation_id"] != body["operation_id"]
        await _settle()
        assert len(routes.posted) == 2  # one turn per utterance, never more

    asyncio.run(go())


@pytest.mark.parametrize(
    "answer",
    [
        (200, {"state": "uncertain"}),
        (200, {"state": "running", "handoff": "uncertain"}),
        (500, {"detail": "boom"}),
        RuntimeError("the route raised"),
    ],
)
def test_an_uncertain_outcome_is_said_and_never_resent(routes, answer):
    async def go():
        call, wire = _call(_leg())
        routes.answer = answer
        await _heard(call, "Deploy it.", 1)
        line = "I can't tell whether that reached docs-refresh; check its chat."
        await _until(lambda: line in wire.said())
        await _settle()
        assert len(routes.posted) == 1

    asyncio.run(go())


def test_a_refused_turn_says_so_and_a_queued_one_says_it_waits(routes):
    async def go():
        call, wire = _call(_leg())
        routes.answer = (409, {"detail": "busy"})
        await _heard(call, "One.", 1)
        await _until(
            lambda: "docs-refresh couldn't take that just now. Check its chat." in wire.said()
        )
        routes.answer = (202, {"state": "queued", "handoff": "queued"})
        await _heard(call, "Two.", 2)
        await _until(lambda: "docs-refresh is still working. That's queued next." in wire.said())

    asyncio.run(go())


def test_the_sessions_own_line_heard_back_is_echo_and_never_a_turn(routes):
    async def go():
        call, wire = _call(_leg())
        await call.say("The build is green and the tests pass.", leg=call.leg)
        await _heard(call, "The build is green and the tests pass.", 1)
        await _settle()
        assert routes.posted == []

    asyncio.run(go())


def test_nothing_of_the_session_leg_reaches_hq(routes, monkeypatch):
    asked: list = []

    async def begin(*a, **kw):
        asked.append(a)
        raise AssertionError("an HQ turn on a session leg")

    monkeypatch.setattr(ask_orchestrator, "begin", begin)

    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Summarise the open PRs.", 1)
        await _until(lambda: routes.posted)
        routes.turn(routes.posted[0]["operation_id"]).update(
            state="completed", reply="Three are open."
        )
        await _until(lambda: "Three are open." in wire.said())
        await call.flush_records()
        return call

    call = asyncio.run(go())
    assert asked == []
    # No HQ conversation was made for a call that began on the session, and nothing was written.
    assert not ask_store.exists(call.cid)


def test_stop_said_while_the_session_works_interrupts_its_turn(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Refactor the parser.", 1)
        await _until(lambda: routes.posted)
        await _until(lambda: call.leg.running)
        await _heard(call, "Stop.", 2)
        await _until(lambda: routes.interrupts)
        assert routes.interrupts[0]["turn_id"] == routes.posted[0]["operation_id"]
        await _until(lambda: "docs-refresh stopped." in wire.said())
        assert len(routes.posted) == 1  # "stop" was not sent as a turn

    asyncio.run(go())


def test_a_decision_the_turn_waits_on_is_said_once(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Delete the build dir.", 1)
        await _until(lambda: routes.posted)
        op = routes.posted[0]["operation_id"]
        routes.pending = [{"request_id": "r1", "turn_id": op, "kind": "permission"}]
        await _until(
            lambda: "docs-refresh needs a decision from you. It's in its chat." in wire.said()
        )
        await _settle()
        assert wire.said().count("docs-refresh needs a decision from you. It's in its chat.") == 1

    asyncio.run(go())


# --- back to HQ, and the §1b party-change fence --------------------------------------------------


def test_back_to_hq_without_hqs_endpoint_says_so_and_stays_on_the_session(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Back to HQ.", 1)
        await _until(lambda: voice_session.NO_HQ_LINE in wire.said())
        assert call.leg is not None and routes.posted == []

    asyncio.run(go())


def test_session_to_hq_a_late_reply_and_progress_are_not_spoken_and_the_echo_is_cut(
    routes, hq_configured, monkeypatch
):
    asked: list = []

    async def begin(cid, turn_id, text, **kw):
        asked.append(text)
        return None, {"turn_id": turn_id, "status": "done", "answer": "Noted.", "via": "call"}

    monkeypatch.setattr(ask_orchestrator, "begin", begin)

    async def go():
        call, wire = _call(_leg())
        await call.say("The parser has three failing cases left.", leg=call.leg)
        await _heard(call, "Fix them.", 1)
        await _until(lambda: call.leg is not None and call.leg.running)
        op = routes.posted[0]["operation_id"]
        await _heard(call, "Back to HQ.", 2)
        await _until(lambda: call.leg is None)
        # HQ's first word on this call (it began on the session): its own greeting.
        assert any(s.startswith("HQ, General") for s in wire.said())
        assert {"t": "flush"} in wire.worker  # the old party's queued speech is dropped
        assert ask_store.exists(call.cid)  # HQ's conversation, now that the Commander is there
        before = len(wire.said())
        t = routes.turn(op)
        t["tools"] = [{"id": "late", "name": "Read"}]
        t.update(state="completed", reply="All three fixed.")
        await _settle()
        assert wire.said()[before:] == []  # no late progress, no late reply on HQ's line
        # The session's line, still audible, is cut as echo: never asked of HQ.
        await _heard(call, "The parser has three failing cases left.", 3)
        await _settle()
        assert asked == []

    asyncio.run(go())


def test_hq_to_session_a_late_hq_answer_and_a_late_judge_result_are_dropped(routes):
    async def go():
        call, wire = _call()
        ask_store.create(call.cid)  # an HQ call: its conversation exists
        call.hq_greeted = True
        call.challenge = {
            "cards": [{"id": "a" * 32}],
            "say_id": "s",
            "gen": call.generation,
            "armed_at": time.monotonic(),
            "expires_at": time.monotonic() + 20,
        }
        ch = call.challenge
        snap = {
            "armed_at": ch["armed_at"],
            "gen": ch["gen"],
            "starts": call.user_starts,
            "typed": call.typed_turns,
        }
        assert call._judge_still_holds(ch, snap)
        gen = call.generation
        await call.say("Let me look that up.")  # HQ, before the switch
        await call.switch_party(_leg())
        assert "docs-refresh here. Go ahead." in wire.said()
        assert call.challenge is None and not call._judge_still_holds(ch, snap)
        before = len(wire.said())
        # HQ's answer to the question in flight lands now: never spoken on the session's line.
        view = {"turn_id": "t1", "status": "done", "answer": "Here is what I found."}
        await call._present(view, gen, {"turn": None}, time.monotonic())
        assert wire.said()[before:] == []
        # HQ's line still audible is cut as echo: never sent to the session.
        await _heard(call, "Let me look that up.", 1)
        await _settle()
        assert routes.posted == []
        # The hand-off is one line in HQ's conversation, never the leg's words.
        await call.flush_records()
        said = [r for r in ask_store.read(call.cid) if r.get("type") == "said"]
        assert [r["kind"] for r in said] == ["handoff"]
        assert said[0]["text"].startswith("Put through to docs-refresh, ")

    asyncio.run(go())


def test_a_switch_before_the_submission_drops_the_words_unsent(routes, hq_configured):
    async def go():
        call, wire = _call(_leg())
        await voice_session.on_utterance(call, "Ship it.")  # minted, submission not run yet
        await call.switch_party(None)
        await _settle()
        assert routes.posted == []

    asyncio.run(go())


def test_a_submission_already_under_way_runs_on_but_its_reply_is_not_spoken(routes, hq_configured):
    async def go():
        call, wire = _call(_leg())
        routes.hold = asyncio.Event()
        await voice_session.on_utterance(call, "Ship it.")
        await _until(lambda: routes.posted)
        await call.switch_party(None)
        routes.hold.set()
        await _settle()
        assert len(routes.posted) == 1  # exactly one of the two acted: the submission
        op = routes.posted[0]["operation_id"]
        routes.turn(op).update(state="completed", reply="Shipped.")
        await _settle()
        assert "Shipped." not in wire.said()

    asyncio.run(go())


# --- the route ---------------------------------------------------------------------------------


@pytest.fixture
def client(auth_cfg, monkeypatch):
    async def lan(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(speech_endpoints, "_resolve", lan)

    async def yes():
        return True, ""

    monkeypatch.setattr(voice_calls, "runtime_ready", yes)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    c.post(
        "/api/voice/settings",
        headers=hdr,
        json={
            "stt": {"base_url": "http://speech.lan:8000/v1", "model": "whisper-1"},
            "tts": {"base_url": "http://speech.lan:8880/v1", "model": "tts"},
            "voices": {"woman": "aria", "man": "atlas"},
        },
    )
    return c, hdr


def test_a_call_to_a_session_needs_no_hq_endpoint_and_makes_no_hq_conversation(client, monkeypatch):
    c, hdr = client
    seen: dict = {}

    async def resolve(app, operator, key, block, hq_voice):
        seen.update(key=key, hq_voice=hq_voice, operator=operator)
        return _leg()

    monkeypatch.setattr(voice_session, "resolve", resolve)
    caps = c.get("/api/voice/capabilities").json()
    assert caps["sessions"] is True and caps["available"] is False  # HQ has no endpoint
    r = c.post("/api/voice/calls", json={"contact": "asia-woman", "party": KEY}, headers=hdr)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["party"]["key"] == KEY and body["party"]["voice"] == "sol"
    assert not ask_store.exists(body["conversation_id"])
    assert seen["key"] == KEY and seen["hq_voice"] == "aria"
    # The operator the call acts as is the one who admitted it (cookie, CSRF, Origin).
    names = {k for k, _ in seen["operator"].headers}
    assert {b"cookie", b"x-csrf-token", b"origin"} <= names
    # Without a party, HQ's endpoint is still required.
    c.delete(f"/api/voice/calls/{body['id']}", headers=hdr)
    assert c.post("/api/voice/calls", json={}, headers=hdr).json()["code"] == "ai"


def test_a_call_to_a_terminal_session_is_refused_with_its_fixed_line(client):
    c, hdr = client
    r = c.post(
        "/api/voice/calls",
        json={"party": "claude:11111111-2222-4333-8444-555555555555"},
        headers=hdr,
    )
    assert r.status_code == 409
    assert r.json() == {
        "detail": "That session can't take calls right now.",
        "code": "unavailable",
        "why": "not_structured",
    }
    assert c.post("/api/voice/calls", json={"party": 7}, headers=hdr).status_code == 422
    assert voice_calls.REGISTRY.current is None  # nothing was admitted


def test_the_party_route_needs_csrf(client):
    c, _ = client
    r = c.post("/api/voice/calls", json={"party": KEY})
    assert r.status_code in (401, 403)


# --- P3: HQ puts you through ---------------------------------------------------------------------


class _Turn:
    def __init__(self, via="call", keys=()):
        self.via, self.keys, self.put_through = via, set(keys), None


def test_put_through_takes_only_a_session_this_turn_found_and_only_on_a_call():
    turn = _Turn(keys={KEY})
    out, _ = asyncio.run(ask_orchestrator._put_through(turn, {"session": KEY}))
    assert out["putting_through"] is True and turn.put_through == KEY
    for t, args in [
        (_Turn(via=None, keys={KEY}), {"session": KEY}),  # a typed turn
        (_Turn(keys=set()), {"session": KEY}),  # a key the model wrote by itself
        (_Turn(keys={"nope"}), {"session": "nope"}),  # not a session key
        (_Turn(keys={KEY}), {"session": 7}),
    ]:
        with pytest.raises(ask_orchestrator._ToolRefused):
            asyncio.run(ask_orchestrator._put_through(t, args))
        assert t.put_through is None
    assert "put_through" in ask_orchestrator.CALL_ONLY_TOOLS
    assert "put_through" not in ask_orchestrator.DRAFT_READ_TOOLS  # waits for the commit


@pytest.fixture
def hq_call(routes, hq_configured, monkeypatch):
    async def resolve(app, operator, key, block, hq_voice):
        if key != KEY:
            raise voice_session.Unavailable("That session can't take calls right now.", "unknown")
        return _leg()

    monkeypatch.setattr(voice_session, "resolve", resolve)
    call, wire = _call()
    call.settings = {"tts_voices": [], "voices": {"woman": "aria", "man": "atlas"}}
    ask_store.create(call.cid)
    call.hq_greeted = True
    return call, wire


def _played(call, wire, text):
    sid = next(m["id"] for m in wire.says() if m["text"] == text)
    return call.on_played(sid)


def test_hq_puts_the_call_through_after_its_fixed_line_has_played(hq_call):
    call, wire = hq_call

    async def go():
        view = {"turn_id": "t1", "status": "done", "answer": "Sure.", "put_through": KEY}
        await call._present(view, call.generation, {"turn": None}, time.monotonic())
        assert wire.said() == ["Putting you through to docs-refresh."]  # not HQ's own words
        assert {"t": "transfer", "party": _leg().public()} in wire.client
        assert call.leg is None  # not yet: the line has not played
        await _played(call, wire, "Putting you through to docs-refresh.")
        assert call.leg is not None and call.leg.key == KEY
        assert wire.said()[-1] == "docs-refresh here. Go ahead."
        assert wire.says()[-1]["voice"] == "sol"
        await call.flush_records()
        said = [r for r in ask_store.read(call.cid) if r.get("type") == "said"]
        assert said[-1]["kind"] == "handoff" and said[-1]["text"].startswith("Put through to docs")

    asyncio.run(go())


def test_stay_with_hq_or_newer_words_drop_the_hand_off(hq_call, monkeypatch):
    call, wire = hq_call

    async def go():
        await call.put_through(KEY)
        await call.cancel_transfer()
        await _played(call, wire, "Putting you through to docs-refresh.")
        assert call.leg is None
        assert {"t": "transfer", "party": None} in wire.client
        await call.put_through(KEY)
        call.generation += 1  # the Commander asked something newer meanwhile
        await _played(call, wire, "Putting you through to docs-refresh.")
        assert call.leg is None

    asyncio.run(go())


def test_a_session_that_cant_take_calls_keeps_the_call_with_hq(hq_call):
    call, wire = hq_call

    async def go():
        await call.put_through("claude:11111111-2222-4333-8444-555555555555")
        assert wire.said() == ["That session can't take calls right now."]
        assert call.leg is None and call.transfer is None

    asyncio.run(go())


def test_back_to_hq_resumes_the_same_conversation(hq_call, monkeypatch):
    call, wire = hq_call
    asked: list = []

    async def begin(cid, turn_id, text, **kw):
        asked.append((cid, text))
        return None, {"turn_id": turn_id, "status": "done", "answer": "Noted.", "via": "call"}

    monkeypatch.setattr(ask_orchestrator, "begin", begin)

    async def go():
        cid = call.cid
        await call.put_through(KEY)
        await _played(call, wire, "Putting you through to docs-refresh.")
        await _heard(call, "Back to HQ.", 1)
        await _until(lambda: call.leg is None)
        assert wire.said()[-1] == voice_session.BACK_LINE
        await _heard(call, "What did it say about the tests?", 2)
        await _until(lambda: asked)
        assert asked == [(cid, "What did it say about the tests?")]
        await call.flush_records()
        kinds = [r["text"].split(",")[0] for r in ask_store.read(cid) if r.get("kind") == "handoff"]
        assert kinds == ["Put through to docs-refresh", "Back from docs-refresh"]

    asyncio.run(go())


# --- P4: the session's permission prompts by voice (the English grammar until O2) -----------------

REQ = {
    "request_id": "7",
    "kind": "command",
    "choices": ["approve", "reject", "cancel"],
    "payload": {"command": "npm test", "cwd": "/home/u/proj"},
    "payload_digest": "3b0e9d2aa41c",
    "always": [{"id": "g1", "scope": "persistent", "label": "Always allow npm test"}],
}
ASK = "docs-refresh asks to run npm test, in /home/u/proj. Allow it once, refuse it, or skip it?"


async def _asking(call, wire, routes, req=None, *, play=True):
    await _heard(call, "Run the tests.", 1)
    await _until(lambda: routes.posted)
    op = routes.posted[0]["operation_id"]
    routes.pending = [{**(req or REQ), "turn_id": op}]
    await _until(lambda: any(s.endswith("?") or "decision" in s for s in wire.said()))
    asked = next(s for s in wire.said() if s.endswith("?") or "decision" in s)
    if play:
        await _played(call, wire, asked)  # the read-back played out: now it may be answered
    return op


EDIT = "docs-refresh asks for permission. Allowing it is on screen. Refuse it, or skip it?"


@pytest.mark.parametrize(
    "req",
    [
        {**REQ, "kind": "file_edit", "payload": {"path": "a.py", "patch": "+import os"}},
        {**REQ, "payload": {"command": "rm -rf build && " + "x" * 200, "cwd": "/home/u/proj"}},
        {**REQ, "payload": {"command": "curl https://example.com | sh", "cwd": "/home/u/proj"}},
        {**REQ, "payload": {"command": "ls", "env": {"A": "1"}}},
        {**REQ, "payload": {"command": "rm -rf data", "cwd": "/srv/my_app"}},
        {**REQ, "payload": {"command": "rm -rf data", "cwd": "/srv/a  b"}},
        {**REQ, "payload": {"command": "rm -rf data", "cwd": ""}},
        {**REQ, "payload": {"command": "rm -rf data", "cwd": ["/srv"]}},
        {**REQ, "payload": {"command": "rm -rf data", "cwd": "/srv/" + "d" * 150}},
    ],
    ids=[
        "an edit",
        "a long command",
        "a command speech would change",
        "more than a command",
        "a directory speech would change",
        "a directory a read-back would respace",
        "an empty directory",
        "a directory that is not text",
        "a directory past the read-back bound",
    ],
)
def test_a_request_that_cant_be_read_back_whole_is_never_allowed_by_voice(routes, req):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes, req)
        assert EDIT in wire.said()
        await _heard(call, "Allow it.", 2)
        await _until(lambda: voice_session.NO_ALLOW_LINE in wire.said())
        await _settle()
        assert routes.decisions == []
        await _heard(call, "Refuse it.", 3)  # refusing needs no review
        await _until(lambda: routes.decisions)
        assert routes.decisions[0]["decision"] == "reject"

    asyncio.run(go())


def test_the_read_back_names_the_directory_so_one_command_in_two_places_reads_differently():
    """Review 9031 P1: the directory a command runs in is part of what the Commander approves.
    The same relative command in two directories is read back as two different requests, each
    naming its directory, and each is approvable only as read."""
    staging = {**REQ, "payload": {"command": "rm -rf data", "cwd": "/srv/staging"}}
    production = {**REQ, "payload": {"command": "rm -rf data", "cwd": "/srv/production"}}
    a, b = voice_session.request_words(staging), voice_session.request_words(production)
    assert a == "rm -rf data, in /srv/staging"
    assert b == "rm -rf data, in /srv/production"
    for req in (staging, production):
        assert voice_session.decision_card(req)["allow"] == {"decision": "approve"}
    # Without a directory, the command runs where the session works: the command alone is read.
    bare = {**REQ, "payload": {"command": "rm -rf data"}}
    assert voice_session.request_words(bare) == "rm -rf data"


@pytest.mark.parametrize("cwd", ["/srv/staging", "/srv/production"])
def test_the_spoken_permission_reads_its_directory_before_it_can_be_allowed(routes, cwd):
    async def go():
        req = {**REQ, "payload": {"command": "rm -rf data", "cwd": cwd}}
        call, wire = _call(_leg())
        await _asking(call, wire, routes, req)
        asked = f"docs-refresh asks to run rm -rf data, in {cwd}. " + voice_session.ALLOW_ASK
        assert asked in wire.said()
        await _heard(call, "Allow it.", 2)
        await _until(lambda: routes.decisions)
        assert routes.decisions[0]["decision"] == "approve"

    asyncio.run(go())


def test_an_answer_before_the_read_back_has_played_answers_nothing(routes):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes, play=False)
        await _heard(call, "Allow it.", 2)  # queued, not heard yet
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1
        # Read again, and answered once THAT one has played.
        await _until(lambda: wire.said().count(ASK) == 2)
        sid = [m["id"] for m in wire.says() if m["text"] == ASK][-1]
        await call.on_played(sid)
        await _heard(call, "Allow it.", 3)
        await _until(lambda: routes.decisions)
        assert routes.decisions[0]["decision"] == "approve"

    asyncio.run(go())


def test_a_read_back_talked_over_is_void_and_read_again(routes):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes, play=False)
        await call.on_worker_event({"t": "interrupt"})  # a barge-in cut it off
        sid = next(m["id"] for m in wire.says() if m["text"] == ASK)
        await call.on_played(sid)  # a late ack of the cut line arms nothing
        assert call.leg.decision is None or not call.leg.decision["armed"]
        await _until(lambda: wire.said().count(ASK) == 2)

    asyncio.run(go())


def test_calling_a_session_already_at_work_follows_that_turn(routes):
    async def go():
        busy = "22222222-2222-4333-8444-555555555555"
        routes.turns.append({"turn_id": busy, "state": "running", "reply": None, "tools": []})
        routes.pending = [{**REQ, "turn_id": busy}]
        call, wire = _call(_leg())
        await call.greet()
        await _until(lambda: ASK in wire.said())  # its prompt, without a new message
        await _until(lambda: call.leg.running == [busy])
        assert voice_session.stop_turn(call) is True
        await _until(lambda: routes.interrupts)
        assert routes.interrupts[0]["turn_id"] == busy and routes.posted == []
        await _until(lambda: "docs-refresh stopped." in wire.said())

    asyncio.run(go())


WORK_START = {"t": "progress", "step": voice_session.WORKING_STEP}


def test_joining_a_turn_with_no_tool_yet_opens_the_working_band_with_stop(routes):
    """Review 9031 P2: a turn waiting on its first model reply (``tools: []``) gets the screen's
    working band, and so its Stop, when the call joins it, not at its first tool event."""

    async def go():
        busy = "33333333-2222-4333-8444-555555555555"
        routes.turns.append({"turn_id": busy, "state": "running", "reply": None, "tools": []})
        call, wire = _call(_leg())
        await call.greet()
        await _until(lambda: WORK_START in wire.client)
        assert not any(m.get("t") == "progress" and "steps" in m for m in wire.client)
        assert voice_session.stop_turn(call) is True
        await _until(lambda: routes.interrupts)
        assert routes.interrupts[0]["turn_id"] == busy

    asyncio.run(go())


def test_a_submitted_turn_opens_the_band_before_any_tool_and_closes_it_when_it_settles(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Summarise the changelog.", 1)
        await _until(lambda: WORK_START in wire.client)
        op = routes.posted[0]["operation_id"]
        routes.turn(op).update(state="completed", reply="Two entries.")
        await _until(lambda: {"t": "leg_turn", "state": "completed"} in wire.client)
        after = wire.client[wire.client.index({"t": "leg_turn", "state": "completed"}) :]
        assert WORK_START not in after  # nothing else runs: the band stays closed

    asyncio.run(go())


def test_a_queued_turn_keeps_the_band_open_when_the_turn_before_it_settles(routes):
    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Run the tests.", 1)
        await _until(lambda: len(routes.posted) == 1)
        await _heard(call, "Then update the docs.", 2)
        await _until(lambda: len(routes.posted) == 2 and len(call.leg.running) == 2)
        first = routes.posted[0]["operation_id"]
        n = len(wire.client)
        routes.turn(first).update(state="completed", reply="Green.")
        await _until(lambda: {"t": "leg_turn", "state": "completed"} in wire.client[n:])
        tail = wire.client[n:]
        assert WORK_START in tail[tail.index({"t": "leg_turn", "state": "completed"}) :]

    asyncio.run(go())


def test_allow_it_answers_the_prompt_once_through_the_panes_route_bound_to_its_digest(routes):
    async def go():
        call, wire = _call(_leg())
        op = await _asking(call, wire, routes)
        assert ASK in wire.said()
        await _heard(call, "Allow it.", 2)
        await _until(lambda: routes.decisions)
        d = routes.decisions[0]
        assert UUID_RE.match(d["decision_id"])
        assert (d["turn_id"], d["request_id"], d["decision"], d["expected_digest"]) == (
            op,
            "7",
            "approve",
            "3b0e9d2aa41c",
        )
        await _until(lambda: voice_session.ALLOWED_LINE in wire.said())
        assert len(routes.posted) == 1  # "allow it" was not a turn

    asyncio.run(go())


def test_allow_always_is_never_by_voice_and_skip_leaves_it_in_the_chat(routes):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes)
        await _heard(call, "Allow always.", 2)
        await _until(lambda: voice_session.BROADER_LINE in wire.said())
        await _heard(call, "Skip it.", 3)
        await _until(lambda: voice_session.SKIPPED_LINE in wire.said())
        await _settle()
        assert routes.decisions == [] and len(routes.posted) == 1

    asyncio.run(go())


def test_a_prompt_answered_on_screen_meanwhile_is_refused_unsent(routes):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes)
        routes.pending = []  # answered on the pane before the spoken answer is sent
        await _heard(call, "Refuse it.", 2)
        await _until(lambda: voice_session.ANSWERED_LINE in wire.said())
        assert routes.decisions == []

    asyncio.run(go())


def test_a_prompt_whose_bytes_changed_is_refused_unsent(routes):
    async def go():
        call, wire = _call(_leg())
        op = await _asking(call, wire, routes)
        routes.pending = [{**REQ, "turn_id": op, "payload_digest": "ffff"}]
        await _heard(call, "Allow it.", 2)
        await _until(lambda: voice_session.ANSWERED_LINE in wire.said())
        assert routes.decisions == []

    asyncio.run(go())


@pytest.mark.parametrize(
    ("answer", "line"),
    [
        (
            (401, {"detail": "unauthorized"}),
            "docs-refresh didn't take that answer. Check its chat.",
        ),
        ((409, {"detail": "archived"}), "docs-refresh didn't take that answer. Check its chat."),
        (
            (500, {"detail": "boom"}),
            "I can't tell whether that answer reached docs-refresh; check its chat.",
        ),
        (
            RuntimeError("raised"),
            "I can't tell whether that answer reached docs-refresh; check its chat.",
        ),
    ],
)
def test_a_logout_an_archive_or_an_unknown_outcome_while_waiting_is_said_never_resent(
    routes, answer, line
):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes)
        routes.decision_answer = answer
        await _heard(call, "Allow it.", 2)
        await _until(lambda: line in wire.said())
        await _settle()
        assert len(routes.decisions) == 1

    asyncio.run(go())


def test_an_agents_question_is_announced_and_answered_in_the_chat(routes):
    async def go():
        call, wire = _call(_leg())
        await _asking(call, wire, routes, {**REQ, "kind": "AskUserQuestion", "choices": ["answer"]})
        assert "docs-refresh needs a decision from you. It's in its chat." in wire.said()
        assert call.leg.decision is None

    asyncio.run(go())


def test_the_panes_decision_route_passes_an_expected_digest_and_checks_it(client, monkeypatch):
    from agent_sessions import structured_runtime

    c, hdr = client
    seen: dict = {}

    async def decide(key, **kw):
        seen.update(kw)
        return {"session_key": KEY, "decision_id": kw["decision_id"]}

    monkeypatch.setattr(structured_runtime, "decide", decide)
    body = {
        "decision_id": "11111111-2222-4333-8444-555555555555",
        "turn_id": "t",
        "request_id": "7",
        "decision": "approve",
    }
    url = f"/api/structured/sessions/{KEY}/decisions"
    assert (
        c.post(url, json={**body, "expected_digest": "3b0e9d2aa41c"}, headers=hdr).status_code
        == 200
    )
    assert seen["expected_digest"] == "3b0e9d2aa41c"
    assert c.post(url, json=body, headers=hdr).status_code == 200
    assert seen["expected_digest"] is None  # the pane's own decisions carry none, as before
    for bad in ("", 7, "x" * 129, "a\nb"):
        r = c.post(url, json={**body, "expected_digest": bad}, headers=hdr)
        assert r.status_code == 422, bad


def test_the_call_screen_picks_the_sessions_voice_from_its_pool_never_hqs(routes):
    async def go():
        call, wire = _call(_leg(voices=("sol", "nova")))
        await voice_session.set_voice(call, "aria")  # HQ's voice is not on offer
        await voice_session.set_voice(call, "../etc")
        assert call.leg.voice == "sol"
        await voice_session.set_voice(call, "nova")
        assert call.leg.voice == "nova"
        assert hq_calls_settings.get()["session_voices"][KEY] == "nova"
        assert wire.client[-1]["t"] == "party" and wire.client[-1]["party"]["voice"] == "nova"
        # The same party re-described, never a switch (review 9091): the screen keeps its band.
        assert wire.client[-1]["update"] is True
        await call.say("Done.", leg=call.leg)
        assert wire.says()[-1]["voice"] == "nova"

    asyncio.run(go())


def test_the_workers_echo_of_a_party_flush_never_cuts_the_new_partys_greeting(
    routes, hq_configured
):
    async def go():
        call, wire = _call()
        ask_store.create(call.cid)
        await call.switch_party(_leg())
        assert {"t": "flush"} in wire.worker
        greeting = next(m for m in wire.says() if m["text"] == "docs-refresh here. Go ahead.")
        interrupts = sum(1 for m in wire.client if m == {"t": "interrupt"})
        await call.on_worker_event({"t": "interrupt"})  # the flush's echo
        assert sum(1 for m in wire.client if m == {"t": "interrupt"}) == interrupts
        assert greeting["id"] in call.say_ids  # its played ack still counts
        await call.on_worker_event({"t": "interrupt"})  # a real barge-in after it
        assert sum(1 for m in wire.client if m == {"t": "interrupt"}) == interrupts + 1

    asyncio.run(go())


# --- P4: call me back ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "yes"),
    [
        ("Call me back when you're done.", True),
        ("ring me when it's finished", True),
        ("Ruf mich zurück, wenn du fertig bist.", True),
        ("Call me back", True),
        ("Tell the user to call me back about the invoice after lunch tomorrow please ok", False),
        ("Run the tests", False),
    ],
)
def test_call_me_back_is_a_short_phrase(said, yes):
    assert voice_session.is_call_me_back(said) is yes


def test_call_me_back_records_the_running_turn_says_so_and_hangs_up(routes):
    from agent_sessions import voice_callbacks

    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Refactor the parser.", 1)
        await _until(lambda: routes.posted)
        op = routes.posted[0]["operation_id"]
        await _until(lambda: call.leg.running)
        await _heard(call, "Call me back when you're done.", 2)
        await _until(lambda: voice_session.CALL_BACK_LINE in wire.said())
        rows = voice_callbacks.pending(KEY)
        assert [(r["key"], r["turn_id"], r["title"]) for r in rows] == [(KEY, op, "docs-refresh")]
        assert len(routes.posted) == 1  # not a turn
        await _played(call, wire, voice_session.CALL_BACK_LINE)
        assert call.state == "ended" and call.ended_reason == "call_back"

    asyncio.run(go())


def test_call_me_back_with_nothing_running_keeps_the_call(routes):
    from agent_sessions import voice_callbacks

    async def go():
        call, wire = _call(_leg())
        await _heard(call, "Call me back.", 1)
        await _until(lambda: voice_session.NOTHING_RUNNING_LINE in wire.said())
        assert call.state != "ended" and voice_callbacks.pending() == []

    asyncio.run(go())


TURN = "11111111-2222-4333-8444-555555555555"


def _snap_with(state):
    async def snapshot(key, **kw):
        return {"turns": [{"turn_id": TURN, "state": state}]}

    return snapshot


@pytest.mark.parametrize(
    ("state", "line"),
    [
        ("completed", "docs-refresh is done."),
        ("interrupted", "docs-refresh stopped."),
        ("failed", "docs-refresh ran into an error."),
    ],
)
def test_the_callback_fires_once_when_that_turn_settles(monkeypatch, state, line):
    from agent_sessions import notifications, structured_runtime, voice_callbacks

    row = voice_callbacks.add(
        key=KEY, turn_id=TURN, title="docs-refresh", project="agent-sessions", engine="claude-api"
    )
    monkeypatch.setattr(structured_runtime, "snapshot", _snap_with("running"))
    assert asyncio.run(voice_callbacks.sweep()) == 0  # still working: nothing yet
    assert voice_callbacks.pending() and notifications.listing()["notifications"] == []
    monkeypatch.setattr(structured_runtime, "snapshot", _snap_with(state))
    assert asyncio.run(voice_callbacks.sweep()) == 1
    rows = notifications.listing()["notifications"]
    assert [(r["title"], r["reason"], r["session_id"]) for r in rows] == [
        ("docs-refresh", line, KEY)
    ]
    assert voice_callbacks.pending() == []
    # A duplicate settlement (the record put back, as a crash between the notice and its removal
    # would leave it) finds the notice already given.
    assert voice_callbacks.announce(row, state) is None
    assert len(notifications.listing()["notifications"]) == 1


def test_a_callback_survives_a_restart_and_a_gone_session_lapses_silently(monkeypatch):
    from agent_sessions import notifications, structured_runtime, voice_callbacks

    voice_callbacks.add(key=KEY, turn_id=TURN, title="docs-refresh", project="", engine="")
    # Persisted: a fresh read (a restarted app) still holds it.
    assert [r["turn_id"] for r in voice_callbacks.pending()] == [TURN]

    async def gone(key, **kw):
        raise structured_runtime.StructuredError(404, "no such structured session")

    monkeypatch.setattr(structured_runtime, "snapshot", gone)
    assert asyncio.run(voice_callbacks.sweep()) == 0
    assert voice_callbacks.pending() == [] and notifications.listing()["notifications"] == []


def test_the_pane_lists_and_cancels_a_callback(client):
    from agent_sessions import voice_callbacks

    c, hdr = client
    row = voice_callbacks.add(key=KEY, turn_id=TURN, title="docs-refresh", project="", engine="")
    got = c.get("/api/voice/callbacks", params={"key": KEY}).json()["callbacks"]
    assert [(r["id"], r["turn_id"]) for r in got] == [(row["id"], TURN)]
    assert c.get("/api/voice/callbacks", params={"key": "x y"}).status_code == 422
    assert c.delete(f"/api/voice/callbacks/{row['id']}").status_code in (401, 403)  # CSRF
    assert c.delete(f"/api/voice/callbacks/{row['id']}", headers=hdr).json() == {"cancelled": True}
    assert c.delete(f"/api/voice/callbacks/{row['id']}", headers=hdr).status_code == 404
    assert voice_callbacks.pending() == []


@pytest.mark.parametrize("how", ["cancelled", "replaced"])
def test_a_callback_cancelled_or_replaced_while_its_turn_is_read_is_never_announced(
    monkeypatch, how
):
    from agent_sessions import notifications, structured_runtime, voice_callbacks

    row = voice_callbacks.add(key=KEY, turn_id=TURN, title="docs-refresh", project="", engine="")

    async def parked(key, **kw):
        # Cancel (or a newer "call me back") lands while the sweep reads the session.
        if how == "cancelled":
            assert voice_callbacks.remove(row["id"]) is True
        else:
            voice_callbacks.add(
                key=KEY, turn_id="33333333-2222-4333-8444-555555555555", title="docs-refresh",
                project="", engine="",
            )  # fmt: skip
        return {"turns": [{"turn_id": TURN, "state": "completed"}]}

    monkeypatch.setattr(structured_runtime, "snapshot", parked)
    assert asyncio.run(voice_callbacks.sweep()) == 0
    assert notifications.listing()["notifications"] == []
