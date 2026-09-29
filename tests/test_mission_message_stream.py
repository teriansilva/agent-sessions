"""`POST /api/missions/{id}/message/stream` — a mission turn, streamed (#1224).

What is pinned:

* the stream is `/message` with its steps shown: the same refusals as HTTP statuses, and exactly
  one last line carrying the body `/message` returns (so replay after a stream is identical);
* progress is OBSERVATIONAL and the stream is a VIEW — a client that leaves (before the first
  line, or while the model runs, under both ASGI disconnect models) never cancels the turn, and a
  same-id retry through `/message` while it runs answers 202 without a second model call;
* the provisional Stage-1 answer really goes out on the wire BEFORE Stage 2 runs — through the real
  ask pipeline, not a fake — and the Stage-2-failure fallback still settles on Stage 1's answer.

`TestClient` buffers a whole response, so it can neither observe a line before the next one nor
disconnect mid-stream. The lifetime and delivery tests drive the ASGI app directly (`_drive`).
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from agent_sessions import aitasks, missions, orchestrator_chat, pulse_chat, review
from agent_sessions.main import create_app
from agent_sessions.routes import missions as mroutes
from test_pulse_chat import BASE, SECRET, _seq_transport, _sessions, _setup, _uuid

#: Captured before any fixture stubs it — `review` is one module, shared with the routes.
_REAL_REQUIRE_CONFIG = review._require_config


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """About the ROUTE, not about whether an AI endpoint is configured (see the sibling file)."""
    monkeypatch.setattr(mroutes.review, "_require_config", lambda: {})
    aitasks.reset()
    yield
    aitasks.reset()


@pytest.fixture
def configured_ai(tmp_path, monkeypatch):
    """A real, configured AI endpoint whose transport each test replaces (as test_pulse_chat)."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    return tmp_path


@pytest.fixture
def mission(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    return missions.create_mission("do it")["id"]


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


class Session:
    """One logged-in app. `post` is TestClient (whole responses); `drive` is raw ASGI."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = create_app(cfg)
        self.c = TestClient(self.app, base_url="https://testserver")
        self.csrf = _login(self.c, cfg)
        self.hdr = {"X-CSRF-Token": self.csrf, "Origin": cfg.origin}

    def post(self, path, payload):
        return self.c.post(path, json=payload, headers=self.hdr)

    async def drive(self, path, payload, *, gone_after=None, spec="2.3", on_line=None):
        """POST through the ASGI app. `gone_after=N` disconnects after N body lines (0 = before
        any). `spec` picks Starlette's disconnect model: < 2.4 listens for `http.disconnect`,
        >= 2.4 learns of it when a write fails (`OSError`)."""
        body = json.dumps(payload).encode()
        cookie = "; ".join(f"{k}={v}" for k, v in self.c.cookies.items())
        headers = [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"origin", self.cfg.origin.encode()),
            (b"x-csrf-token", self.csrf.encode()),
            (b"cookie", cookie.encode()),
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
        }
        pending = [{"type": "http.request", "body": body, "more_body": False}]
        gone = asyncio.Event()
        if gone_after == 0:
            gone.set()
        out: dict = {"status": None, "lines": [], "raw": b""}

        async def receive():
            if pending:
                return pending.pop(0)
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
                return
            if m["type"] != "http.response.body" or not m.get("body"):
                return
            if gone.is_set() and spec >= "2.4":
                raise OSError("client went away")
            out["raw"] += m["body"]
            for ln in m["body"].decode().splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except ValueError:
                    continue
                out["lines"].append(ev)
                if on_line is not None:
                    await on_line(ev)
            if gone_after is not None and len(out["lines"]) >= gone_after:
                gone.set()

        try:
            await self.app(scope, receive, send)
        except (OSError, ClientDisconnect):
            # Spec >= 2.4: the failed write surfaces out of the app, as the server would see it.
            if not gone.is_set():
                raise
        return out


async def _all_turns_done(timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if not mroutes._TURN_TASKS:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a streamed turn never finished")


def _gated_ask(calls: list, gate: asyncio.Event | None = None, answer: str = "the answer"):
    """A stand-in for `orchestrator_chat.ask` that reports a step, then (optionally) waits."""

    async def ask(text, history=None, *, on_progress=None, **kw):
        calls.append(text)
        if on_progress is not None:
            on_progress({"type": "progress", "step": "classify"})
        if gate is not None:
            await gate.wait()
        return {"intent": "find", "answer": answer, "actions": [], "matches": []}

    return ask


def _kinds(lines):
    """`(type, detail)` per line: the step, the answer's `final`, or the turn/error status."""
    detail = {"progress": "step", "answer": "final", "turn": "status", "error": "status"}
    return [(e["type"], e.get(detail[e["type"]])) for e in lines]


# --- the same route guards and refusals, as HTTP statuses --------------------------------------


def test_stream_requires_login(auth_cfg, mission):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(f"/api/missions/{mission}/message/stream", json={})
    assert r.status_code == 401


def test_stream_requires_csrf(auth_cfg, mission):
    s = Session(auth_cfg)
    r = s.c.post(
        f"/api/missions/{mission}/message/stream",
        json={"message": "hi", "turn_id": "t1"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("path_mission", "payload", "status"),
    [
        (None, {"message": "hi"}, 422),
        (None, {"message": 3, "turn_id": "t1"}, 422),
        ("m_does_not_exist", {"message": "hi", "turn_id": "t1"}, 404),
    ],
)
def test_refusals_before_the_model_are_http_statuses_not_streams(
    auth_cfg, mission, monkeypatch, path_mission, payload, status
):
    async def boom(*a, **k):
        raise AssertionError("a refused turn must not reach the model")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", boom)
    s = Session(auth_cfg)
    r = s.post(f"/api/missions/{path_mission or mission}/message/stream", payload)
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    assert missions.unresolved_turn_keys() == set()


def test_a_busy_flight_is_a_409_before_the_stream_starts(auth_cfg, mission, monkeypatch):
    monkeypatch.setattr(mroutes.aitasks, "is_running", lambda kind: True)
    s = Session(auth_cfg)
    r = s.post(f"/api/missions/{mission}/message/stream", {"message": "hi", "turn_id": "t1"})
    assert r.status_code == 409
    assert r.json()["detail"] == "a question is already running"


# --- what the stream carries --------------------------------------------------------------------


def test_steps_then_exactly_one_turn_line_equal_to_the_message_replay(
    auth_cfg, mission, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _gated_ask(calls))
    s = Session(auth_cfg)
    payload = {"message": "hi", "turn_id": "t1"}
    r = s.post(f"/api/missions/{mission}/message/stream", payload)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["cache-control"] == "no-store"
    lines = _lines(r.text)
    assert _kinds(lines) == [("progress", "classify"), ("turn", 200)]
    turn = lines[-1]["turn"]
    assert turn["state"] == "done" and turn["answer"] == "the answer"

    # The stream's last line IS `/message`'s body: a replay through `/message` is identical.
    again = s.post(f"/api/missions/{mission}/message", payload)
    assert again.status_code == 200
    assert again.json() == turn
    assert calls == ["hi"]


def test_a_replay_through_the_stream_is_only_the_turn_line(auth_cfg, mission, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _gated_ask(calls))
    s = Session(auth_cfg)
    payload = {"message": "hi", "turn_id": "t1"}
    first = s.post(f"/api/missions/{mission}/message", payload).json()
    lines = _lines(s.post(f"/api/missions/{mission}/message/stream", payload).text)
    assert _kinds(lines) == [("turn", 200)]
    assert lines[0]["turn"] == first
    assert calls == ["hi"]


def test_a_live_turn_through_the_stream_is_one_202_turn_line(auth_cfg, mission, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("a live turn must not call the model again")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", boom)
    missions.claim_turn(mission, "t1", hashlib.sha256(b"hi").hexdigest())
    s = Session(auth_cfg)
    lines = _lines(
        s.post(f"/api/missions/{mission}/message/stream", {"message": "hi", "turn_id": "t1"}).text
    )
    assert _kinds(lines) == [("turn", 202)]
    assert lines[0]["turn"]["state"] == "in_progress"


def test_a_backend_failure_after_the_start_is_one_error_line(auth_cfg, mission, monkeypatch):
    async def fails(text, history=None, *, on_progress=None, **kw):
        on_progress({"type": "progress", "step": "classify"})
        raise review.ReviewError("endpoint said no")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", fails)
    s = Session(auth_cfg)
    lines = _lines(
        s.post(f"/api/missions/{mission}/message/stream", {"message": "hi", "turn_id": "t1"}).text
    )
    assert _kinds(lines) == [("progress", "classify"), ("error", 502)]
    # The route's own authored detail — never the backend's text, never mission content.
    assert lines[-1]["detail"] == "the chat backend failed (ReviewError)"
    assert "endpoint said no" not in json.dumps(lines)


def test_a_progress_watcher_that_raises_never_fails_the_turn(monkeypatch):
    """`on_progress` is observational inside `ask` itself, not just in the route's queue."""
    monkeypatch.setattr(orchestrator_chat.review, "_require_config", lambda: {})

    async def classify(q, turns):
        return "history"

    monkeypatch.setattr(orchestrator_chat, "_classify", classify)
    monkeypatch.setattr(orchestrator_chat, "_history_answer", lambda: {"intent": "history"})

    def explode(ev):
        raise RuntimeError("watcher broke")

    out = asyncio.run(orchestrator_chat.ask("what did you do?", on_progress=explode))
    assert out == {"intent": "history"}


def test_a_full_queue_drops_progress_never_the_turn(auth_cfg, mission, monkeypatch):
    """A reader that cannot keep up loses PROGRESS lines; the turn and its last line survive."""

    async def chatty(text, history=None, *, on_progress=None, **kw):
        for i in range(mroutes.TURN_STREAM_QUEUE_MAX * 3):
            on_progress({"type": "progress", "step": "catalog", "sessions": i, "missions": 0})
        return {"intent": "find", "answer": "still here", "actions": [], "matches": []}

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", chatty)
    s = Session(auth_cfg)
    lines = _lines(
        s.post(f"/api/missions/{mission}/message/stream", {"message": "hi", "turn_id": "t1"}).text
    )
    assert lines[-1]["type"] == "turn" and lines[-1]["turn"]["answer"] == "still here"
    assert len(lines) - 1 <= mroutes.TURN_STREAM_QUEUE_MAX


# --- the stream is a VIEW: leaving never cancels the turn ---------------------------------------


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("gone_after", [0, 1])
def test_a_client_that_leaves_never_cancels_the_turn(
    auth_cfg, mission, monkeypatch, spec, gone_after
):
    """Before the first line (0) or while the model runs (1), under both disconnect models: the
    stream ends, the turn settles, and its answer is in the timeline."""
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _gated_ask(calls, gate, "kept"))
        s = Session(auth_cfg)
        drive = asyncio.create_task(
            s.drive(
                f"/api/missions/{mission}/message/stream",
                {"message": "hi", "turn_id": "t1"},
                gone_after=gone_after,
                spec=spec,
            )
        )
        await asyncio.wait({drive}, timeout=2)
        if spec < "2.4" or gone_after == 0:
            # The response is over (a 2.4 server only learns of the leave on its next write,
            # which a held model does not produce — that stream ends when the turn does).
            assert drive.done()
        # Either way the turn is running with nobody watching it, and it is not cancelled.
        assert mroutes._TURN_TASKS, "the turn must outlive the stream that asked for it"
        gate.set()
        await drive
        await _all_turns_done()

    asyncio.run(scenario())
    assert calls == ["hi"]
    turn = missions.get_turn(mission, "t1")
    assert turn["state"] == "done"
    kinds = [e["kind"] for e in missions.get_mission(mission)["events"]]
    assert "assistant_msg" in kinds


def test_a_same_id_retry_through_message_while_live_is_202_and_one_execution(
    auth_cfg, mission, monkeypatch
):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _gated_ask(calls, gate))
        s = Session(auth_cfg)
        payload = {"message": "hi", "turn_id": "t1"}
        await s.drive(f"/api/missions/{mission}/message/stream", payload, gone_after=1)
        retry = await s.drive(f"/api/missions/{mission}/message", payload)
        assert retry["status"] == 202
        assert json.loads(retry["raw"])["state"] == "in_progress"
        gate.set()
        await _all_turns_done()
        # …and once settled, the same id replays the answer rather than asking again.
        done = await s.drive(f"/api/missions/{mission}/message", payload)
        assert done["status"] == 200 and json.loads(done["raw"])["answer"] == "the answer"

    asyncio.run(scenario())
    assert calls == ["hi"]
    assert not mroutes._TURN_TASKS


def test_a_detached_turns_failure_is_observed_and_it_is_released(caplog):
    """Nobody may be left holding an unretrieved exception, and the task set may not leak."""
    seen: list = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: seen.append(ctx))

        async def broken():
            raise ValueError("not a refusal")

        async def refused():
            raise missions.MissionError("a question is already running", status=409)

        a = mroutes._spawn_turn(broken(), "m1")
        b = mroutes._spawn_turn(refused(), "m2")
        await asyncio.wait({a, b})
        await asyncio.sleep(0)
        del a, b
        gc.collect()

    with caplog.at_level(logging.WARNING, logger=mroutes.log.name):
        asyncio.run(scenario())
    assert not mroutes._TURN_TASKS
    assert not [c for c in seen if "exception was never retrieved" in str(c.get("message"))]
    warned = [r.getMessage() for r in caplog.records]
    assert any("m1" in w and "ValueError" in w for w in warned)
    assert not any("m2" in w for w in warned), "a refusal is an outcome, not a failure"


# --- through the REAL ask pipeline: delivery order and the Stage-2 fallback --------------------


def _real_find(monkeypatch, calls):
    """The real `orchestrator_chat.ask` → real `pulse_chat` pipeline; only the model and the
    session scan are fakes. The classifier says "find"."""

    async def classify(q, turns):
        return "find"

    monkeypatch.setattr(orchestrator_chat, "_classify", classify)
    # A REAL configured endpoint (`configured_ai`) — the model calls go through `_post_chat`.
    monkeypatch.setattr(review, "_require_config", _REAL_REQUIRE_CONFIG)
    _setup(monkeypatch, _sessions(3))
    target = f"claude:{_uuid(1)}"
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "first look", "matches": [{"id": target, "why": "title"}]},
                {"answer": "confirmed", "matches": [{"id": target, "why": "transcript"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect", "fp"))


def test_the_provisional_answer_is_on_the_wire_before_stage_two_runs(
    auth_cfg, configured_ai, mission, monkeypatch
):
    calls: list = []
    _real_find(monkeypatch, calls)
    real_refine = pulse_chat._stage2_refine
    stage2_started: list = []

    async def scenario():
        gate = asyncio.Event()

        async def held_refine(*a, **k):
            stage2_started.append(True)
            await gate.wait()
            return await real_refine(*a, **k)

        monkeypatch.setattr(pulse_chat, "_stage2_refine", held_refine)
        seen_before_release: list = []

        async def on_line(ev):
            if ev["type"] == "answer":
                # The provisional answer is DELIVERED while Stage 2 is still held.
                seen_before_release.append((ev, gate.is_set(), len(calls)))
                gate.set()

        s = Session(auth_cfg)
        out = await s.drive(
            f"/api/missions/{mission}/message/stream",
            {"message": "reconnect?", "turn_id": "t1"},
            on_line=on_line,
        )
        return out, seen_before_release

    out, seen = asyncio.run(scenario())
    assert _kinds(out["lines"]) == [
        ("progress", "classify"),
        ("progress", "catalog"),
        ("answer", False),
        ("progress", "content"),
        ("turn", 200),
    ]
    (ev, released, n_calls) = seen[0]
    assert released is False and n_calls == 1
    # Text only — the provisional answer carries no cards; the settled turn does.
    assert ev == {"type": "answer", "final": False, "answer": "first look"}
    assert out["lines"][-1]["turn"]["answer"] == "confirmed"
    assert stage2_started == [True]


def test_a_failed_stage_two_still_settles_on_the_stage_one_answer(
    auth_cfg, configured_ai, mission, monkeypatch
):
    _real_find(monkeypatch, [])

    async def refine_fails(*a, **k):
        return None  # what `_stage2_refine` answers when every candidate failed

    monkeypatch.setattr(pulse_chat, "_stage2_refine", refine_fails)
    s = Session(auth_cfg)
    lines = _lines(
        s.post(
            f"/api/missions/{mission}/message/stream", {"message": "reconnect?", "turn_id": "t1"}
        ).text
    )
    assert lines[-1]["type"] == "turn"
    assert lines[-1]["turn"]["answer"] == "first look"


# --- independent review of #1227 ----------------------------------------------------------------


def test_shutdown_waits_for_a_streamed_turn_nobody_is_watching(auth_cfg, mission, monkeypatch):
    """`/message` ran its turn inside the request, and the server waits for requests in flight,
    so a restart let the turn finish. A streamed turn whose reader left belongs to no request: the
    app's own shutdown must wait for it, or the closing loop cancels it half-way (left
    `in_progress`, archive fence shut, a delivery possibly half done)."""
    app = create_app(auth_cfg)
    settled: list = []

    async def scenario():
        async with app.router.lifespan_context(app):
            gate = asyncio.Event()

            async def turn():
                await gate.wait()
                settled.append("settled")
                return {}

            mroutes._spawn_turn(turn(), mission)
            # Released only AFTER shutdown has begun — the turn is mid-flight when it starts.
            asyncio.get_running_loop().call_later(0.3, gate.set)
        assert settled == ["settled"], "shutdown returned before the turn settled"

    asyncio.run(scenario())
    assert not mroutes._TURN_TASKS


def test_progress_queued_when_the_turn_finishes_is_still_delivered():
    """A slow reader can fall behind: the turn finishes while lines are still queued. They go out
    BEFORE the last line, never dropped because the task won the race to `asyncio.wait`."""

    async def scenario():
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for step in ("classify", "catalog"):
            queue.put_nowait({"type": "progress", "step": step})
        queue.put_nowait({"type": "answer", "final": False, "answer": "early"})

        async def done():
            return {"state": "done", "answer": "late"}

        task = asyncio.get_running_loop().create_task(done())
        await task  # finished before the reader has read a single line
        return [json.loads(ln) async for ln in mroutes._watch_turn(task, queue)]

    lines = asyncio.run(scenario())
    assert _kinds(lines) == [
        ("progress", "classify"),
        ("progress", "catalog"),
        ("answer", False),
        ("turn", 200),
    ]
