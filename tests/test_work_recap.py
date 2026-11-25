"""RECENT WORK above Ask (#1086): structured, validated against its inputs, cached, never a
model call on read — then the routes, including the roots/exclusions boundary."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, prefs, prompts, pulse, review, work_recap
from agent_sessions.main import create_app

NOW = 1_800_000_000.0


def _card(sid, *, last, recap="did a thing\nnow waiting on review", engine="claude", cwd=None):
    return {
        "id": sid,
        "engine": engine,
        "title": f"title {sid}",
        "cwd": cwd or f"/work/{sid.split(':')[1]}",
        "project": {"kind": "folder", "id": "p1", "name": "Alpha"},
        "last_activity": last,
        "ai_summary": "one line",
        "_ai_recap": recap,
    }


CARDS = [_card("claude:a", last=NOW - 3600), _card("codex:b", last=NOW - 600, engine="codex")]


def test_inputs_are_most_recent_first_and_bounded(monkeypatch):
    monkeypatch.setattr(work_recap, "SESSIONS_MAX", 1)
    got = work_recap.inputs_from(CARDS)
    assert [i["key"] for i in got] == ["codex:b"]
    monkeypatch.setattr(work_recap, "SESSIONS_MAX", 40)
    monkeypatch.setattr(work_recap, "INPUT_MAX", len(CARDS[0]["_ai_recap"]))
    assert len(work_recap.inputs_from(CARDS)) == 1  # the char budget stops the second


def test_local_entries_use_the_recaps_CURRENT_STATE_line_oldest_first():
    entries = work_recap.local_entries(work_recap.inputs_from(CARDS))
    assert [e["session_key"] for e in entries] == ["claude:a", "codex:b"]
    assert entries[0]["text"] == "now waiting on review"  # the LAST line, not the first


def test_validate_drops_what_the_inputs_and_window_cannot_vouch_for():
    inputs = work_recap.inputs_from(CARDS)
    start = NOW - 86400
    obj = {
        "entries": [
            {"session_key": "codex:b", "ts": NOW - 500, "text": "Shipped the fix."},
            {"session_key": "claude:ghost", "ts": NOW - 400, "text": "not an input"},
            {"session_key": "claude:a", "ts": start - 10, "text": "before the window"},
            {"session_key": "claude:a", "ts": NOW + 9999, "text": "in the future"},
            {"session_key": "claude:a", "ts": NOW - 3000, "text": "   "},
            {"session_key": "claude:a", "ts": True, "text": "a bool is not a time"},
            {"session_key": "claude:a", "ts": NOW - 3500, "text": "Started\x07 the\n\nrefactor."},
            "not a dict",
        ]
    }
    got = work_recap.validate(obj, inputs, window_start=start, now=NOW)
    assert [(e["session_key"], e["text"]) for e in got] == [
        ("claude:a", "Started the refactor."),  # control bytes and newlines cleaned
        ("codex:b", "Shipped the fix."),
    ]
    assert work_recap.validate({"entries": "nope"}, inputs, window_start=start, now=NOW) == []
    assert work_recap.validate(["x"], inputs, window_start=start, now=NOW) == []


def test_read_never_calls_the_model_and_is_local_without_a_cache(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("read must not call the model")

    monkeypatch.setattr(review, "complete_json", boom)
    out = work_recap.read(CARDS, window_days=1, configured=True, now=NOW)
    assert out["source"] == "local" and out["stale"] is True
    assert [e["session_key"] for e in out["entries"]] == ["claude:a", "codex:b"]
    assert out["entries"][0]["engine"] == "claude" and out["entries"][0]["project"]["id"] == "p1"
    # The session's own recap rides along for ▸ — no second call to expand it.
    assert "waiting on review" in out["entries"][0]["session_recap"]


def _model(monkeypatch, reply):
    calls = []

    async def fake(messages, **_k):
        calls.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(review, "complete_json", fake)
    return calls


def test_generate_writes_a_validated_ai_recap_and_reuses_it_when_nothing_changed(monkeypatch):
    calls = _model(
        monkeypatch,
        {
            "entries": [
                {"session_key": "claude:a", "ts": NOW - 3500, "text": "Started the refactor."}
            ]
        },
    )
    out = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert out["source"] == "ai" and out["stale"] is False
    assert [e["text"] for e in out["entries"]] == ["Started the refactor."]
    # The system prompt is the registry's, byte for byte.
    assert calls[0][0] == {"role": "system", "content": prompts.effective("pulse_recap")}
    sent = json.loads(calls[0][1]["content"])
    assert {s["key"] for s in sent["sessions"]} == {"claude:a", "codex:b"}

    again = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW + 60))
    assert len(calls) == 1  # unchanged inputs → no second completion
    assert again["source"] == "ai"
    # A read now serves it, and is stale only once the sessions move on.
    assert work_recap.read(CARDS, window_days=1, configured=True, now=NOW)["stale"] is False
    moved = [*CARDS[:1], _card("codex:b", last=NOW - 10, engine="codex")]
    assert work_recap.read(moved, window_days=1, configured=True, now=NOW)["stale"] is True


def test_a_different_window_is_not_served_from_the_cache(monkeypatch):
    _model(
        monkeypatch,
        {"entries": [{"session_key": "claude:a", "ts": NOW - 3500, "text": "One."}]},
    )
    asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert work_recap.read(CARDS, window_days=2, configured=True, now=NOW)["source"] == "local"


def test_no_endpoint_is_local_never_an_error(monkeypatch):
    _model(monkeypatch, review.NotConfiguredError("no endpoint"))
    out = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert out["source"] == "local" and out["configured"] is False and out["stale"] is False


def test_a_failed_or_empty_reply_keeps_the_previous_ai_recap(monkeypatch):
    _model(
        monkeypatch,
        {"entries": [{"session_key": "claude:a", "ts": NOW - 3500, "text": "Good one."}]},
    )
    asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    moved = [*CARDS[:1], _card("codex:b", last=NOW - 10, engine="codex")]

    _model(monkeypatch, review.ReviewError("endpoint said 500"))
    failed = asyncio.run(work_recap.generate(moved, window_days=1, now=NOW + 60))
    assert failed["source"] == "ai" and failed["stale"] is True and "500" in failed["error"]
    assert [e["text"] for e in failed["entries"]] == ["Good one."]

    _model(monkeypatch, {"entries": [{"session_key": "nobody", "ts": NOW, "text": "x"}]})
    empty = asyncio.run(work_recap.generate(moved, window_days=1, now=NOW + 120))
    assert (
        empty["source"] == "ai" and empty["error"] == "the AI endpoint returned no usable entries"
    )
    assert [e["text"] for e in empty["entries"]] == ["Good one."]


def test_a_NON_FINITE_model_time_is_dropped_and_never_poisons_the_cache(monkeypatch):
    """Review 5032 finding 1: the real parser accepts `"ts": NaN`, every window comparison
    against it is false, and a NaN written to the cache made every later response unserialisable."""
    raw = '{"entries": [{"session_key": "claude:a", "ts": NaN, "text": "nan time"},'
    raw += ' {"session_key": "claude:a", "ts": Infinity, "text": "inf time"}]}'
    reply = json.loads(raw)  # Python's json, like the endpoint parser, accepts both
    _model(monkeypatch, reply)
    out = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert out["source"] == "local" and out["error"] == "the AI endpoint returned no usable entries"
    assert not work_recap._cache_path().exists()  # nothing invalid was written
    json.dumps(out, allow_nan=False)  # the response serialises strictly


def test_a_poisoned_cache_is_served_sanitised_and_REPLACED_by_the_next_refresh(monkeypatch):
    """Review 5044's note, asserted exactly: a file that needed cleaning on load is served with
    only its sane entries AND is dirty — stale on read, a miss on refresh — so the next refresh
    replaces it on disk rather than leaving the NaN there."""
    path = work_recap._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = work_recap.fingerprint(work_recap.inputs_from(CARDS), 1)
    # Written with allow_nan so the file carries a literal NaN, as a bad earlier write would have.
    doc = {
        "cache_version": 1,
        "window_days": 1,
        "input_fingerprint": fp,
        "generated_at": 1,
        "source": "ai",
        "entries": [
            {"session_key": "claude:a", "ts": float("nan"), "text": "bad"},
            {"session_key": "codex:b", "ts": NOW - 100, "text": "Good."},
        ],
    }
    path.write_text(json.dumps(doc, allow_nan=True))
    assert "NaN" in path.read_text()

    got = work_recap.read(CARDS, window_days=1, configured=True, now=NOW)
    json.dumps(got, allow_nan=False)
    assert [e["text"] for e in got["entries"]] == ["Good."]
    assert got["stale"] is True  # dirty: a refresh would write something different

    calls = _model(
        monkeypatch,
        {"entries": [{"session_key": "claude:a", "ts": NOW - 3500, "text": "Fresh."}]},
    )
    asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW + 1))
    assert len(calls) == 1  # same inputs, but a dirty artifact is never a cache hit
    assert "NaN" not in path.read_text()
    after = work_recap.read(CARDS, window_days=1, configured=True, now=NOW + 1)
    assert [e["text"] for e in after["entries"]] == ["Fresh."] and after["stale"] is False


def test_an_OVERSIZED_integer_time_through_the_real_parser_is_dropped_not_raised(monkeypatch):
    """Review 5044: a 400-digit integer is valid JSON, `review._extract_json` accepts it, and
    `float()` of it raised OverflowError before the entry could be dropped — failing the refresh
    instead of serving the existing summary. Driven through the REAL parser."""
    huge = "9" * 400
    content = (
        '{"entries": [{"session_key": "claude:a", "ts": ' + huge + ', "text": "huge time"},'
        ' {"session_key": "codex:b", "ts": ' + str(int(NOW - 100)) + ', "text": "Kept."}]}'
    )
    parsed = review._extract_json(content)
    assert isinstance(parsed["entries"][0]["ts"], int)
    _model(monkeypatch, parsed)
    out = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert [e["text"] for e in out["entries"]] == ["Kept."]
    json.dumps(out, allow_nan=False)

    # All-invalid: nothing usable → the local fallback, still serialisable, nothing written.
    only_huge = review._extract_json(
        '{"entries": [{"session_key": "claude:a", "ts": ' + huge + ', "text": "x"}]}'
    )
    work_recap._cache_path().unlink()
    _model(monkeypatch, only_huge)
    fallback = asyncio.run(work_recap.generate(CARDS, window_days=1, now=NOW))
    assert fallback["source"] == "local"
    json.dumps(fallback, allow_nan=False)
    assert not work_recap._cache_path().exists()


def test_cached_entries_age_out_of_the_ROLLING_window_on_read_and_refresh(monkeypatch):
    """Review 5032 finding 4: an entry 30 s inside the lower bound, then the clock advances 60 s.
    The session is still in the window; its old entry is not, and must not be served as current."""
    edge = NOW - 86400 + 30
    cards = [_card("claude:a", last=NOW), _card("codex:b", last=NOW - 600, engine="codex")]
    calls = _model(
        monkeypatch,
        {
            "entries": [
                {"session_key": "claude:a", "ts": edge, "text": "Old edge step."},
                {"session_key": "codex:b", "ts": NOW - 600, "text": "Recent step."},
            ]
        },
    )
    asyncio.run(work_recap.generate(cards, window_days=1, now=NOW))
    later = NOW + 60
    got = work_recap.read(cards, window_days=1, configured=True, now=later)
    assert [e["text"] for e in got["entries"]] == ["Recent step."]
    assert got["stale"] is True  # a refresh would write something different
    asyncio.run(work_recap.generate(cards, window_days=1, now=later))
    assert len(calls) == 2  # aged-out content is a MISS, not a cache hit


# ---- routes -----------------------------------------------------------------------------------


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
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


@pytest.fixture
def cards(monkeypatch):
    rows = [
        _card("claude:a", last=NOW - 3600, cwd="/work/inside"),
        _card("codex:b", last=NOW - 600, engine="codex", cwd="/secret/outside"),
    ]
    monkeypatch.setattr(pulse, "build_cards", lambda **_k: [dict(r) for r in rows])
    return rows


def test_recap_routes_require_login_and_the_refresh_requires_csrf(auth_cfg, fake_jsonl, cards):
    c = _client(auth_cfg)
    assert c.get("/api/pulse/recap").status_code == 401
    assert c.post("/api/pulse/recap").status_code in (401, 403)
    _login(c, auth_cfg)
    no_csrf = c.post("/api/pulse/recap", headers={"Origin": auth_cfg.origin})
    assert no_csrf.status_code == 403


def test_recap_read_is_local_and_honours_folder_exclusions(auth_cfg, fake_jsonl, cards):
    prefs.set_folder_exclusions(["/secret"])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    body = c.get("/api/pulse/recap", params={"window_days": 5}).json()
    assert body["window_days"] == 3  # coerced like every read
    assert body["source"] == "local"
    # The excluded folder's session is neither named nor summarised.
    assert [e["session_key"] for e in body["entries"]] == ["claude:a"]


def test_needs_you_honours_folder_exclusions_too(auth_cfg, fake_jsonl, cards, monkeypatch):
    from agent_sessions import missions, orchestrator, orchestrator_ledger

    for r in cards:
        r["intervention_required"] = True
    monkeypatch.setattr(orchestrator_ledger, "live_actions", lambda *a, **k: [])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {})
    monkeypatch.setattr(orchestrator, "observed_screen", lambda key: None)
    prefs.set_folder_exclusions(["/secret"])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/pulse/needs-you").json()["rows"]
    assert [r["id"] for r in rows] == ["claude:a"]


def test_recap_refresh_generates_and_is_single_flight(auth_cfg, fake_jsonl, cards, monkeypatch):
    _model(
        monkeypatch,
        {"entries": [{"session_key": "claude:a", "ts": NOW - 3500, "text": "Did it."}]},
    )
    monkeypatch.setattr(work_recap.time, "time", lambda: NOW)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/pulse/recap", json={"window_days": 1}, headers=hdr)
    assert r.status_code == 200 and r.json()["source"] == "ai"

    class Busy:
        async def __aenter__(self):
            raise aitasks.AlreadyRunning("work-recap")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(aitasks, "single_flight", lambda *a, **k: Busy())
    busy = c.post("/api/pulse/recap", json={}, headers=hdr)
    assert busy.status_code == 409
