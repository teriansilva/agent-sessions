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
import sqlite3
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, metadata, missions, prefs, prompts, pulse_chat, review
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


def test_prefilter_keeps_a_recap_only_keyword_hit(monkeypatch):
    # The topic lives ONLY in the session's ai_recap — not its title, summary, cwd, or
    # project (#653). Before the recap fed the haystack this session was a non-hit and, being
    # the oldest, would have been dropped past the 150-cap by the 200 recent-noise sessions.
    noise = _sessions(200)
    hit = FakeSession("claude", _uuid(999), "/proj/generic", OLD)
    meta = {
        f"claude:{_uuid(999)}": metadata.SessionMeta(
            title="refactor the stream layer",  # deliberately no "backpressure"
            ai_recap="investigated the mux backpressure deadlock; fixed the writer",
            review_fingerprint="fp",
        )
    }
    _setup(monkeypatch, [*noise, hit], meta)
    catalog = pulse_chat.build_catalog(now=NOW)
    target = next(c for c in catalog if c["id"] == f"claude:{_uuid(999)}")
    # Guard the premise: the keyword is ONLY in the recap, so a hit here is the recap's doing.
    assert "backpressure" not in (
        f"{target.get('title') or ''} {target.get('ai_summary') or ''} "
        f"{target.get('cwd') or ''}".lower()
    )
    assert "backpressure" in pulse_chat._card_haystack(target)
    slice_ = pulse_chat._prefilter(catalog, "which session had the backpressure deadlock?")
    assert len(slice_) == pulse_chat.CATALOG_SLICE_MAX
    assert any(c["id"] == f"claude:{_uuid(999)}" for c in slice_)


def test_catalog_entry_prefers_bounded_recap_over_summary():
    # A conservative cap, pinned so the Stage-1 prompt can't silently grow with recap length,
    # and roomier than the one-line summary so the model sees the chronological brief (#653).
    assert pulse_chat.CATALOG_RECAP_MAX == 500
    assert pulse_chat.CATALOG_RECAP_MAX > pulse_chat.SUMMARY_MAX
    entry = pulse_chat._catalog_entry(
        {
            "id": f"claude:{_uuid(1)}",
            "_ai_recap": "R" * 2000,
            "ai_summary": "one liner",
            "last_activity": NOW,
        },
        NOW,
    )
    assert entry["summary"] == "R" * pulse_chat.CATALOG_RECAP_MAX
    # A short recap passes through whole (never padded, never the summary).
    short = pulse_chat._catalog_entry(
        {
            "id": f"claude:{_uuid(2)}",
            "_ai_recap": "did the thing",
            "ai_summary": "s",
            "last_activity": NOW,
        },
        NOW,
    )
    assert short["summary"] == "did the thing"


def test_catalog_entry_falls_back_to_summary_when_no_recap():
    # No recap (older/un-recapped session) → byte-for-byte the pre-#653 behaviour: the
    # ai_summary capped at SUMMARY_MAX, not CATALOG_RECAP_MAX.
    for recap in (None, ""):
        entry = pulse_chat._catalog_entry(
            {
                "id": f"claude:{_uuid(3)}",
                "_ai_recap": recap,
                "ai_summary": "S" * 300,
                "last_activity": NOW,
            },
            NOW,
        )
        assert entry["summary"] == "S" * pulse_chat.SUMMARY_MAX


def test_recap_is_retrieval_input_only_and_never_leaks(configured_ai, monkeypatch):
    # The recap feeds the Stage-1 catalog the model ranks on, but the internal _ai_recap key
    # (like every _-prefixed field) is stripped before any card reaches the client (#653).
    assert "_ai_recap" not in pulse_chat._public_card(
        {"id": "x", "title": "t", "_ai_recap": "secret", "_review_fingerprint": "fp"}
    )
    target = f"claude:{_uuid(1)}"
    _setup(
        monkeypatch,
        _sessions(3),
        {target: metadata.SessionMeta(ai_recap="secret chronological recap of the mux work")},
    )
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "cat", "matches": [{"id": target, "why": "recap mentions it"}]},
                {"answer": "the mux session.", "matches": [{"id": target, "why": "confirmed"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: mux tail", "fp"))
    result = asyncio.run(pulse_chat.ask("the mux work session?"))
    # Stage 1 saw the recap as the target's summary (retrieval input)...
    stage1_catalog = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    target_entry = next(e for e in stage1_catalog if e["id"] == target)
    assert target_entry["summary"] == "secret chronological recap of the mux work"
    # ...but the returned card carries NO _-prefixed field (recap never leaves the server).
    (match,) = result["matches"]
    assert not any(k.startswith("_") for k in match)


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


# ---- missions in the catalog (#1069) --------------------------------------------------


def _mission(title: str, instruction: str) -> str:
    """A real mission row in the per-test store (conftest isolates the DB)."""
    return missions.create_mission(instruction, title=title)["id"]


def test_missions_join_the_catalog_and_come_back_as_mission_matches(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    mid = _mission("Stabilise terminal reconnects", "fix the websocket reconnect storm")
    key = f"mission:{mid}"
    session = f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {
                    "answer": "cat",
                    "matches": [{"id": key, "why": "instruction"}, {"id": session, "why": "t"}],
                },
                {
                    "answer": "The reconnect mission; p1 did the work.",
                    "matches": [{"id": key, "why": "brief says so"}, {"id": session, "why": "x"}],
                },
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect", "fp"))
    result = asyncio.run(pulse_chat.ask("which mission fixed the websocket reconnect?"))

    # Stage 1 saw the mission as its own kind, with its instruction as the summary.
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    (entry,) = [e for e in stage1 if e["id"] == key]
    assert entry["kind"] == "mission"
    assert entry["summary"] == "fix the websocket reconnect storm"
    assert {e["kind"] for e in stage1} == {"mission", "session"}
    # Stage 2 verified the mission against its text, not a transcript it does not have.
    cands = json.loads(calls[1]["messages"][-1]["content"])["candidates"]
    (mc,) = [c for c in cands if c["id"] == key]
    assert mc["mission_brief"] == "fix the websocket reconnect storm"
    assert "transcript_tail" not in mc

    assert result["stage"] == "content"
    # `matches` stays session cards only; missions ride beside them, keyed by the BARE id the
    # `/mission?m=` deep link takes, and never carry the operator's instruction text.
    assert [m["id"] for m in result["matches"]] == [session]
    assert result["mission_matches"] == [
        {
            "id": mid,
            "title": "Stabilise terminal reconnects",
            "state": "draft",
            "project_id": "",
            "why": "brief says so",
        }
    ]


def test_invented_or_misshapen_mission_ids_are_dropped(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    mid = _mission("Real one", "do the thing")
    payload = {
        "answer": "found",
        "matches": [
            {"id": "mission:msn_" + "f" * 32, "why": "invented"},
            {"id": "mission:../../etc", "why": "junk"},
            {"id": mid, "why": "bare id was never in the catalog"},
            {"id": f"mission:{mid}", "why": "real"},
        ],
    }
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload, payload], calls))
    result = asyncio.run(pulse_chat.ask("which one?"))
    assert [m["id"] for m in result["mission_matches"]] == [mid]
    assert result["matches"] == []


def test_missions_alone_are_not_an_empty_catalog(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    _mission("Only a mission", "something")
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["stage"] == "catalog"
    assert len(calls) == 1


def test_a_broken_missions_store_degrades_to_sessions_only(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    _mission("Hidden by the failure", "x")

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pulse_chat, "build_mission_catalog", boom)
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["answer"] == "none"
    assert result["mission_matches"] == []
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    assert stage1 and all(e["kind"] == "session" for e in stage1)


def test_missions_matching_the_query_never_crowd_sessions_out_of_the_slice(
    configured_ai, monkeypatch
):
    # Missions have their own sub-quota: sixty keyword-hit missions still leave every session
    # in the Stage-1 catalog, and the missions themselves stop at MISSION_SLICE_MAX.
    _setup(monkeypatch, _sessions(3))
    for i in range(60):
        _mission(f"websocket mission {i}", "websocket reconnect")
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    asyncio.run(pulse_chat.ask("websocket"))
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    kinds = [e["kind"] for e in stage1]
    assert kinds.count("mission") == pulse_chat.MISSION_SLICE_MAX
    assert kinds.count("session") == 3


def test_an_operator_prompt_that_never_mentions_missions_still_works(configured_ai, monkeypatch):
    # An edited ask_catalog written before #1069 knows nothing about missions. The contract is
    # unchanged, so a reply naming only sessions is a normal answer with no mission matches.
    prompts.set_value("ask_catalog", "Find sessions. Reply with JSON {answer, matches}.")
    _setup(monkeypatch, _sessions(2))
    _mission("A mission", "x")
    session = f"claude:{_uuid(0)}"
    payload = {"answer": "p0", "matches": [{"id": session, "why": "w"}]}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload, payload]))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))
    result = asyncio.run(pulse_chat.ask("which?"))
    assert [m["id"] for m in result["matches"]] == [session]
    assert result["mission_matches"] == []


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


# ---- streamed ask (#1171) -----------------------------------------------------------


def _two_stage(monkeypatch, calls: list) -> str:
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
    return target


def test_events_stream_the_stage1_answer_before_stage2_runs(configured_ai, monkeypatch):
    calls: list = []
    target = _two_stage(monkeypatch, calls)

    async def collect():
        seen = []
        async for ev in pulse_chat.ask_events("reconnect?"):
            # How many model calls had been made when THIS event was handed out.
            seen.append((ev, len(calls)))
        return seen

    seen = asyncio.run(collect())
    kinds = [(ev["type"], ev.get("step") or ev.get("final")) for ev, _ in seen]
    assert kinds == [
        ("progress", "catalog"),
        ("answer", False),
        ("progress", "content"),
        ("answer", True),
    ]
    (catalog, n0), (early, n1), (content, n2), (final, n3) = seen
    assert n0 == 0 and catalog["sessions"] == 3
    # The whole point: the Stage-1 answer is out after ONE call, not after both.
    assert n1 == 1 and early["answer"] == "first look" and early["stage"] == "catalog"
    assert early["matches"][0]["id"] == target
    assert content["candidates"] == 1
    assert n3 == 2 and final["answer"] == "confirmed" and final["stage"] == "content"


def test_ask_is_the_last_event_without_its_event_fields(configured_ai, monkeypatch):
    _two_stage(monkeypatch, [])
    result = asyncio.run(pulse_chat.ask("reconnect?"))
    assert result["answer"] == "confirmed"
    assert "type" not in result and "final" not in result
    assert set(result) == {"answer", "matches", "mission_matches", "stage", "configured"}


def test_no_match_streams_one_final_answer_and_no_content_step(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "Nothing like that.", "matches": []}])
    )

    async def collect():
        return [ev async for ev in pulse_chat.ask_events("q")]

    evs = asyncio.run(collect())
    assert [e["type"] for e in evs] == ["progress", "answer"]
    assert evs[-1]["final"] is True and evs[-1]["stage"] == "catalog"


def _ndjson(r) -> list[dict]:
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_stream_route_sends_ndjson_events(auth_cfg, fake_jsonl, monkeypatch):
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})

    async def fake_events(query, history=None, *, working_keys=None):
        # The single-flight is held while the stream is being produced.
        assert aitasks.is_running("pulse-chat")
        yield {"type": "progress", "step": "catalog", "sessions": 1, "missions": 0}
        yield {
            "type": "answer",
            "final": True,
            "answer": "a",
            "matches": [],
            "mission_matches": [],
            "stage": "catalog",
            "configured": True,
        }

    monkeypatch.setattr(pulse_chat, "ask_events", fake_events)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/pulse/ask/stream", json={"query": "q"}, headers=hdr)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    evs = _ndjson(r)
    assert [e["type"] for e in evs] == ["progress", "answer"]
    # Released once the stream is done.
    assert not aitasks.is_running("pulse-chat")


def test_stream_route_failure_after_start_is_one_error_line(auth_cfg, fake_jsonl, monkeypatch):
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    monkeypatch.setattr(
        review, "_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(502))
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/pulse/ask/stream", json={"query": "where?"}, headers=hdr)
    assert r.status_code == 200
    last = _ndjson(r)[-1]
    assert last["type"] == "error" and last["status"] == 502
    assert "HTTP 502" in last["detail"]
    assert not aitasks.is_running("pulse-chat")


def test_stream_route_refuses_before_streaming(auth_cfg, fake_jsonl, monkeypatch):
    c = _client(auth_cfg)
    # Login and CSRF, like the plain route.
    assert c.post("/api/pulse/ask/stream", json={"query": "q"}).status_code in (401, 403)
    csrf = _login(c, auth_cfg)
    assert c.post("/api/pulse/ask/stream", json={"query": "q"}).status_code in (401, 403)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Unconfigured is a 409 the UI pre-gates on, not a stream.
    r = c.post("/api/pulse/ask/stream", json={"query": "q"}, headers=hdr)
    assert r.status_code == 409 and r.json()["configured"] is False
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    # The body bounds are the plain route's.
    assert c.post("/api/pulse/ask/stream", json={}, headers=hdr).status_code == 422
    long = "x" * (pulse_chat.QUERY_MAX + 1)
    assert c.post("/api/pulse/ask/stream", json={"query": long}, headers=hdr).status_code == 422
    # Another question in flight is a 409 with the activity snapshot, before any byte.
    monkeypatch.setattr(aitasks, "is_running", lambda *a, **k: True)
    r = c.post("/api/pulse/ask/stream", json={"query": "q"}, headers=hdr)
    assert r.status_code == 409
    assert "already running" in r.json()["detail"] and "running" in r.json()


def _stream_endpoint(app):
    (route,) = [r for r in app.routes if getattr(r, "path", "") == "/api/pulse/ask/stream"]
    return route.endpoint


def _request(body: dict):
    from starlette.requests import Request

    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/api/pulse/ask/stream", "headers": []},
        receive,
    )


def test_stream_delivers_stage1_before_stage2_and_holds_the_gate_across_it(
    auth_cfg, fake_jsonl, monkeypatch
):
    """Through the route's own body iterator, with Stage 2 PAUSED: the Stage-1 answer line is out
    while Stage 2 has not finished, the single-flight is held for exactly that span, and a plain
    `/api/pulse/ask` meanwhile is a 409 — the two routes share one gate."""
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    gate = asyncio.Event()

    async def paused_events(query, history=None, *, working_keys=None):
        yield {"type": "progress", "step": "catalog", "sessions": 3, "missions": 0}
        yield {
            "type": "answer",
            "final": False,
            "answer": "first look",
            "matches": [],
            "mission_matches": [],
            "stage": "catalog",
            "configured": True,
        }
        await gate.wait()  # Stage 2, paused
        yield {
            "type": "answer",
            "final": True,
            "answer": "confirmed",
            "matches": [],
            "mission_matches": [],
            "stage": "content",
            "configured": True,
        }

    monkeypatch.setattr(pulse_chat, "ask_events", paused_events)
    endpoint = _stream_endpoint(c.app)

    async def run():
        resp = await endpoint(_request({"query": "q"}), _user="marcus", _csrf=None)
        it = resp.body_iterator
        first = json.loads(await it.__anext__())
        early = json.loads(await it.__anext__())
        assert first["type"] == "progress"
        assert early == {**early, "type": "answer", "final": False, "answer": "first look"}
        # Stage 2 has NOT run, and the gate is held while it waits.
        assert aitasks.is_running("pulse-chat")
        # The plain route shares that gate.
        assert c.post("/api/pulse/ask", json={"query": "q"}, headers=hdr).status_code == 409
        gate.set()
        final = json.loads(await it.__anext__())
        assert final["final"] is True and final["answer"] == "confirmed"
        with pytest.raises(StopAsyncIteration):
            await it.__anext__()
        assert not aitasks.is_running("pulse-chat")

    asyncio.run(run())


def test_a_client_that_leaves_mid_stream_releases_the_gate(auth_cfg, fake_jsonl, monkeypatch):
    """A disconnect closes the body iterator (Starlette cancels the response). Whether it happens
    after the first line or while Stage 2 is in flight, the gate must come free — otherwise one
    abandoned tab blocks every later question with a 409."""
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    c = _client(auth_cfg)

    async def hanging_events(query, history=None, *, working_keys=None):
        yield {"type": "progress", "step": "catalog", "sessions": 1, "missions": 0}
        await asyncio.Event().wait()  # never finishes on its own
        yield {}  # pragma: no cover

    monkeypatch.setattr(pulse_chat, "ask_events", hanging_events)
    endpoint = _stream_endpoint(c.app)

    async def run():
        # Left after the first line.
        resp = await endpoint(_request({"query": "q"}), _user="marcus", _csrf=None)
        it = resp.body_iterator
        await it.__anext__()
        assert aitasks.is_running("pulse-chat")
        await it.aclose()
        assert not aitasks.is_running("pulse-chat")
        # Left while the pipeline is awaiting (cancelled mid-Stage-2).
        resp = await endpoint(_request({"query": "q"}), _user="marcus", _csrf=None)
        it = resp.body_iterator
        await it.__anext__()
        pending = asyncio.ensure_future(it.__anext__())
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not aitasks.is_running("pulse-chat")
        # A stream that was refused before it started never took the gate.
        resp = await endpoint(_request({}), _user="marcus", _csrf=None)
        assert resp.status_code == 422
        assert not aitasks.is_running("pulse-chat")

    asyncio.run(run())


def test_total_stage2_failure_streams_a_final_stage1_answer(configured_ai, monkeypatch):
    """The Stage-1 fallback holds for the stream too, and it is FINAL — the client's "checking
    transcripts…" must clear on it rather than wait for an answer that is not coming."""
    _setup(monkeypatch, _sessions(3))
    target = f"claude:{_uuid(1)}"
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport([{"answer": "first look", "matches": [{"id": target, "why": "t"}]}]),
    )

    def boom(key, n):
        raise review.ReviewError("nothing to review")

    monkeypatch.setattr(review, "gather_input", boom)

    async def collect():
        return [ev async for ev in pulse_chat.ask_events("q")]

    evs = asyncio.run(collect())
    finals = [e for e in evs if e["type"] == "answer" and e["final"]]
    assert len(finals) == 1 and evs[-1] is finals[0]
    assert finals[0]["answer"] == "first look" and finals[0]["stage"] == "catalog"
    assert finals[0]["matches"][0]["id"] == target
