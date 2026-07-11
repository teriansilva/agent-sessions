"""Pulse "Ask" — natural-language session retrieval (#522): engine + route.

The engine is tested against a monkeypatched session scan + metadata and a
``httpx.MockTransport`` LLM (CI never touches the network), mirroring ``test_pulse.py``.
Pinned: the full-history catalog (no Pulse window) + archived/excluded exclusion, the
keyword prefilter's never-drop-a-hit cap, id validation against the slice actually sent
(invented/duplicate ids dropped), the 0/1/2-LLM-call cases (empty catalog / no matches /
happy path), per-candidate Stage-2 skip + total-failure degrade, history clamping, and the
route contract (422 bounds, unconfigured 409, endpoint-down 502, concurrent-ask 409 with
the activity snapshot, CSRF gate).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, metadata, prefs, pulse_chat, review
from agent_sessions.main import create_app

SECRET = "sk-pulse-chat-test"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"

NOW = 1_000_000.0
OLD = NOW - 45 * 86400  # far outside any Pulse window (max 30d)


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str = "first message"
    archived: bool = False

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


@pytest.fixture(autouse=True)
def _reset_activity():
    aitasks.reset()
    yield
    aitasks.reset()


@pytest.fixture
def configured_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    return tmp_path


def _setup(monkeypatch, sessions, meta=None):
    monkeypatch.setattr(pulse_chat.pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse_chat.pulse.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(pulse_chat.pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse_chat.pulse.projects, "load", lambda *a, **k: {})


def _uuid(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


def _sessions(n: int, *, mtime: float = NOW - 100) -> list[FakeSession]:
    return [FakeSession("claude", _uuid(i), f"/proj/p{i}", mtime) for i in range(n)]


def _seq_transport(payloads: list[dict], calls: list | None = None):
    """A MockTransport answering the i-th completion with ``payloads[i]`` (last one
    repeats). ``calls`` collects each request's decoded body for prompt assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        payload = payloads[min((len(calls) if calls is not None else 1) - 1, len(payloads) - 1)]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return httpx.MockTransport(handler)


# ---- catalog ------------------------------------------------------------------------


def test_catalog_covers_full_history_and_filters(monkeypatch):
    ancient = FakeSession("claude", _uuid(1), "/a", OLD)
    archived = FakeSession("claude", _uuid(2), "/a", NOW - 50, archived=True)
    excluded = FakeSession("claude", _uuid(3), "/a", NOW - 50)
    recent = FakeSession("claude", _uuid(4), "/a", NOW - 50)
    meta = {f"claude:{_uuid(3)}": metadata.SessionMeta(review_excluded=True)}
    _setup(monkeypatch, [ancient, archived, excluded, recent], meta)
    ids = [c["id"] for c in pulse_chat.build_catalog(now=NOW)]
    # The 45-day-old session IS in the catalog (no window); archived/excluded are not.
    assert f"claude:{_uuid(1)}" in ids
    assert f"claude:{_uuid(4)}" in ids
    assert f"claude:{_uuid(2)}" not in ids
    assert f"claude:{_uuid(3)}" not in ids


def test_prefilter_caps_slice_and_keeps_every_keyword_hit(monkeypatch):
    # 200 recent noise sessions + ONE old session whose title matches the query. The hit
    # must survive the 150-cap even though recency alone would have dropped it.
    noise = _sessions(200)
    hit = FakeSession("claude", _uuid(999), "/proj/ws", OLD)
    meta = {
        f"claude:{_uuid(999)}": metadata.SessionMeta(
            title="fix websocket reconnect backoff", review_fingerprint="fp"
        )
    }
    _setup(monkeypatch, [*noise, hit], meta)
    catalog = pulse_chat.build_catalog(now=NOW)
    slice_ = pulse_chat._prefilter(catalog, "the websocket reconnect bug I worked on")
    assert len(slice_) == pulse_chat.CATALOG_SLICE_MAX
    assert any(c["id"] == f"claude:{_uuid(999)}" for c in slice_)


# ---- ask(): call counts, stages, validation -----------------------------------------


def test_empty_catalog_makes_zero_llm_calls(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([{}], calls))
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["stage"] == "empty"
    assert result["matches"] == []
    assert result["answer"]  # a deterministic server-side line, not model output
    assert calls == []


def test_no_matches_is_one_call_stage_catalog(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(3))
    calls: list = []
    payload = {"answer": "Nothing like that.", "matches": []}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload], calls))
    result = asyncio.run(pulse_chat.ask("did I ever port this to zig?"))
    assert result["stage"] == "catalog"
    assert result["matches"] == []
    assert result["answer"] == "Nothing like that."
    assert len(calls) == 1


def test_happy_path_is_exactly_two_calls_and_cards_carry_why(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(3))
    target = f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "cat", "matches": [{"id": target, "why": "title mentions it"}]},
                {
                    "answer": "That was your p1 session.",
                    "matches": [{"id": target, "why": "transcript confirms"}],
                },
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect stuff", "fp"))
    result = asyncio.run(pulse_chat.ask("the reconnect bug session?"))
    assert len(calls) == 2
    assert result["stage"] == "content"
    assert result["answer"] == "That was your p1 session."
    (match,) = result["matches"]
    assert match["why"] == "transcript confirms"
    # The match is the full public Pulse-card shape (the frontend Card renders it as-is).
    for key in (
        "id",
        "engine",
        "title",
        "cwd",
        "project",
        "last_activity",
        "ai_summary",
        "intervention_required",
        "intervention_reason",
        "reviewed_at",
        "live",
        "state",
        "synthesis",
    ):
        assert key in match, key
    assert "_review_fingerprint" not in match
    # Stage 2 saw the transcript tail for the candidate it was asked about.
    stage2_user = json.loads(calls[1]["messages"][-1]["content"])
    assert stage2_user["candidates"][0]["transcript_tail"] == "user: reconnect stuff"


def test_invented_and_duplicate_ids_are_dropped(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    known = f"claude:{_uuid(0)}"
    payload = {
        "answer": "found",
        "matches": [
            {"id": "claude:99999999-9999-4999-8999-999999999999", "why": "invented"},
            {"id": known, "why": "real"},
            {"id": known, "why": "duplicate"},
            {"id": "not-a-key", "why": "junk shape"},
        ],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload]))
    # No Stage 2: force every gather to fail so the Stage-1 validation is what we observe.
    monkeypatch.setattr(
        review, "gather_input", lambda *a: (_ for _ in ()).throw(review.ReviewError("none"))
    )
    result = asyncio.run(pulse_chat.ask("which one?"))
    assert [m["id"] for m in result["matches"]] == [known]
    assert result["matches"][0]["why"] == "real"


def test_malformed_model_output_raises_review_error(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(1))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(review.ReviewError):
        asyncio.run(pulse_chat.ask("hello?"))


def test_stage2_skips_broken_candidate_keeps_others(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(3))
    broken, good = f"claude:{_uuid(0)}", f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {
                    "answer": "cat",
                    "matches": [{"id": broken, "why": "b"}, {"id": good, "why": "g"}],
                },
                {"answer": "refined", "matches": [{"id": good, "why": "confirmed"}]},
            ],
            calls,
        ),
    )

    def gather(key, n):
        if key == broken:
            raise review.ReviewError("nothing to review")
        return ("user: tail", "fp")

    monkeypatch.setattr(review, "gather_input", gather)
    result = asyncio.run(pulse_chat.ask("which?"))
    # The broken candidate never reached Stage 2, the good one did — and the ask succeeded.
    stage2_user = json.loads(calls[1]["messages"][-1]["content"])
    assert [c["id"] for c in stage2_user["candidates"]] == [good]
    assert result["stage"] == "content"
    assert [m["id"] for m in result["matches"]] == [good]


def test_total_stage2_failure_degrades_to_stage1(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    target = f"claude:{_uuid(0)}"
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            payload = {"answer": "stage1 answer", "matches": [{"id": target, "why": "w"}]}
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
            )
        return httpx.Response(502)  # Stage 2 call fails entirely

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))
    result = asyncio.run(pulse_chat.ask("which?"))
    assert result["stage"] == "catalog"  # degraded, not errored
    assert result["answer"] == "stage1 answer"
    assert [m["id"] for m in result["matches"]] == [target]


def test_history_is_clamped(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(1))
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "a", "matches": []}], calls)
    )
    history = [{"role": "user", "content": f"turn {i} " + "x" * 5000} for i in range(20)] + [
        {"role": "tool", "content": "dropped"},
        {"bad": "shape"},
        "junk",
    ]
    asyncio.run(pulse_chat.ask("q?", history))
    messages = calls[0]["messages"]
    replayed = messages[1:-1]  # between the system prompt and the catalog user message
    assert len(replayed) == pulse_chat.HISTORY_TURNS_MAX
    assert all(len(m["content"]) <= pulse_chat.HISTORY_TURN_CHARS_MAX for m in replayed)
    assert all(m["role"] in ("user", "assistant") for m in replayed)
    # The newest turns won the clamp.
    assert replayed[-1]["content"].startswith("turn 19")


# ---- route --------------------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_ask_unconfigured_is_409_with_configured_false(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/pulse/ask", json={"query": "where did I fix the ws bug?"}, headers=hdr)
    assert r.status_code == 409
    body = r.json()
    assert body["configured"] is False
    assert "not configured" in body["detail"]


def test_ask_endpoint_down_is_502_with_detail(auth_cfg, fake_jsonl, monkeypatch):
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    monkeypatch.setattr(
        review, "_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(502))
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/pulse/ask", json={"query": "where?"}, headers=hdr)
    assert r.status_code == 502
    assert "HTTP 502" in r.json()["detail"]


def test_ask_query_bounds_are_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/pulse/ask", json={}, headers=hdr).status_code == 422
    assert c.post("/api/pulse/ask", json={"query": "   "}, headers=hdr).status_code == 422
    long = "x" * (pulse_chat.QUERY_MAX + 1)
    assert c.post("/api/pulse/ask", json={"query": long}, headers=hdr).status_code == 422


def test_ask_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/pulse/ask", json={"query": "q"})  # no CSRF / Origin
    assert r.status_code in (401, 403)


def test_ask_requires_login(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    r = c.post("/api/pulse/ask", json={"query": "q"})
    assert r.status_code in (401, 403)


def test_concurrent_ask_is_409_with_activity_snapshot(auth_cfg, fake_jsonl, monkeypatch):
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    def _busy(*a, **k):
        raise aitasks.AlreadyRunning("pulse-chat")

    monkeypatch.setattr(aitasks, "single_flight", _busy)
    r = c.post("/api/pulse/ask", json={"query": "q"}, headers=hdr)
    assert r.status_code == 409
    body = r.json()
    assert "already running" in body["detail"]
    assert "running" in body and "last" in body  # the activity snapshot rides along
