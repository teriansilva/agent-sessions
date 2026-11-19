"""Pulse cards and ``GET /api/pulse`` (#441, live since #2179).

The card builder is tested against a monkeypatched session scan + metadata (no real ``$HOME``);
the routes through the real app (TestClient) over the ``fake_jsonl`` fixture. Pinned: the window
filter, archived/excluded exclusion, state classification + ordering, the public card boundary,
the live GET (no scan, no cache, no model call), the out-of-window pending action, the retired
routes, the trimmed `pulse` prefs block, and that no scan loop starts.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, automation, metadata, prefs, pulse, review
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app


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


def _setup(monkeypatch, sessions, meta=None):
    monkeypatch.setattr(pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse.projects, "load", lambda *a, **k: {})


# ---- card builder -------------------------------------------------------------


def test_window_filter(monkeypatch):
    now = 1_000_000.0
    recent = FakeSession("claude", "u-recent", "/a", now - 3600)
    old = FakeSession("claude", "u-old", "/a", now - 10 * 86400)
    _setup(monkeypatch, [recent, old])
    cards = pulse.build_cards(window_days=3, now=now)
    assert [c["id"] for c in cards] == ["claude:u-recent"]


def test_card_title_follows_the_keep_session_titles_choice(monkeypatch, tmp_path):
    """#1510: the card title is what a needs-you notification and Ask show, so it skips the AI
    title exactly when the session list does."""
    now = 1_000_000.0
    s = FakeSession("claude", "u-t", "/a", now - 3600, first_user_message="fix the parser")
    _setup(monkeypatch, [s], {"claude:u-t": metadata.SessionMeta(ai_title="Refactor parser")})
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    assert pulse.build_cards(window_days=3, now=now)[0]["title"] == "Refactor parser"
    prefs.set_session_review({"rename_sessions": False})
    assert pulse.build_cards(window_days=3, now=now)[0]["title"] == "fix the parser"


def test_excludes_archived_and_review_excluded(monkeypatch):
    now = 1_000_000.0
    native_arch = FakeSession("claude", "u-arch", "/a", now - 3600, archived=True)
    sidecar_arch = FakeSession("claude", "u-sarch", "/a", now - 3600)
    excluded = FakeSession("claude", "u-excl", "/a", now - 3600)
    ok = FakeSession("claude", "u-ok", "/a", now - 3600)
    meta = {
        "claude:u-sarch": metadata.SessionMeta(archived=True),  # sidecar override wins
        "claude:u-excl": metadata.SessionMeta(review_excluded=True),
    }
    _setup(monkeypatch, [native_arch, sidecar_arch, excluded, ok], meta)
    cards = pulse.build_cards(window_days=3, now=now)
    assert [c["id"] for c in cards] == ["claude:u-ok"]


def test_state_classification_and_order(monkeypatch):
    now = 1_000_000.0
    needs = FakeSession("claude", "u-need", "/a", now - 100)
    inflight = FakeSession("claude", "u-live", "/a", now - 200)
    recent = FakeSession("claude", "u-recent", "/a", now - 3600)
    idle = FakeSession("claude", "u-idle", "/a", now - 2 * 86400)
    meta = {
        "claude:u-need": metadata.SessionMeta(
            intervention_required=True, intervention_reason="blocked"
        )
    }
    _setup(monkeypatch, [idle, recent, inflight, needs], meta)
    cards = pulse.build_cards(window_days=3, now=now, working_keys={"claude:u-live"})
    assert [(c["id"], c["state"]) for c in cards] == [
        ("claude:u-need", "needs_you"),
        ("claude:u-live", "in_flight"),
        ("claude:u-recent", "recently_active"),
        ("claude:u-idle", "idle"),
    ]
    assert cards[0]["intervention_reason"] == "blocked"
    assert cards[1]["live"] is True


def test_overview_strips_every_internal_field(monkeypatch):
    """The public card boundary (#2179): `build_cards` carries the recap, the assessment and the
    fingerprints for server-side callers; none of them may leave in a response."""
    now = 1_000_000.0
    s = FakeSession("claude", "u-1", "/a", now - 60)
    meta = {
        "claude:u-1": metadata.SessionMeta(
            ai_recap="private recap", review_fingerprint="fp", recap_fingerprint="rfp"
        )
    }
    _setup(monkeypatch, [s], meta)
    internal = {k for k in pulse.build_cards(window_days=1, now=now)[0] if k.startswith("_")}
    assert {"_ai_recap", "_review_fingerprint", "_recap_fingerprint"} <= internal
    ov = pulse.overview(window_days=1, now=now)
    assert ov["window_days"] == 1 and ov["generated_at"] == now
    assert [c["id"] for c in ov["cards"]] == ["claude:u-1"]
    assert not [k for c in ov["cards"] for k in c if k.startswith("_")]
    assert "synthesis" not in ov["cards"][0]  # the slow scan's line went with the scan


def test_overview_coerces_the_window(monkeypatch):
    now = 1_000_000.0
    _setup(monkeypatch, [FakeSession("claude", "u-old", "/a", now - 2.5 * 86400)])
    assert pulse.overview(window_days=1, now=now)["cards"] == []
    # 99 reads as the 3-day ceiling (the one read rule, #1086), so a 2.5-day-old card is in.
    wide = pulse.overview(window_days=99, now=now)
    assert wide["window_days"] == 3 and len(wide["cards"]) == 1


# ---- routes -------------------------------------------------------------------


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


def _no_model_calls(monkeypatch) -> list:
    """Record every model call: `review._post_chat` is the only transport (prompt registry)."""
    calls: list = []

    async def _record(*a, **k):
        calls.append((a, k))
        raise AssertionError("no model call is expected here")

    monkeypatch.setattr(review, "_post_chat", _record)
    return calls


def _append_action(rec):
    if rec.get("session_id") and rec.get("state") in ledger.OPERATOR_PENDING_STATES:
        rec = {**rec, "authority": automation.capture(rec["session_id"], rec.get("mission_id"))}
    return ledger.append(rec)


def test_get_pulse_builds_live_cards_with_no_scan(auth_cfg, fake_jsonl, monkeypatch):
    """#2179: the Missions shelf's titles come from cards built for the request. No scan has run,
    the scan is not even configurable any more, nothing is written to a cache and no model is
    called — and the session titles are there."""
    calls = _no_model_calls(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/pulse").json()
    titles = {card["id"]: card["title"] for card in d["cards"]}
    assert titles["claude:11111111-1111-1111-1111-111111111111"] == "first message on repo-a"
    assert titles["claude:33333333-3333-3333-3333-333333333333"] == "hello tmp"
    assert "claude:44444444-4444-4444-4444-444444444444" not in titles  # archived
    assert d["window_days"] == prefs.get_pulse()["window_days"]
    assert isinstance(d["generated_at"], float)
    for gone in ("cache_version", "scan_depth", "input_fingerprint", "synthesis_skipped"):
        assert gone not in d
    assert calls == []
    assert not list(fake_jsonl.rglob("pulse-cache.json"))


def test_get_pulse_sends_no_internal_field(auth_cfg, fake_jsonl, monkeypatch):
    """The `_`-prefixed fields `build_cards` carries (recap, assessment, fingerprints) are server
    inputs: stripped on the live path exactly as the cache artifact used to strip them."""
    key = "claude:11111111-1111-1111-1111-111111111111"
    metadata.patch(key, ai_recap="a private recap", review_fingerprint="fp")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    raw = c.get("/api/pulse").text
    assert "a private recap" not in raw
    cards = json.loads(raw)["cards"]
    assert key in {card["id"] for card in cards}
    assert not [k for card in cards for k in card if k.startswith("_")]


def test_an_out_of_window_pending_action_is_still_projected(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """The live path keeps the ledger overlay's reach: a pending action on a session OUTSIDE the
    recent window has no card of its own, so `_attach_pending` synthesizes one for it."""
    old = fake_jsonl / ".claude" / "projects" / "-tmp-other"
    old_file = old / "33333333-3333-3333-3333-333333333333.jsonl"
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old_file, (ten_days_ago, ten_days_ago))
    sid = "claude:33333333-3333-3333-3333-333333333333"
    _append_action(
        {
            "id": "act-old",
            "state": "escalated",
            "verb": "escalate",
            "session_id": sid,
            "engine": "claude",
            "title": "An old session",
            "project": "other",
        }
    )
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    cards = {card["id"]: card for card in c.get("/api/pulse").json()["cards"]}
    assert sid in cards
    card = cards[sid]
    assert card["synthesized_for_action"] is True
    assert card["pending_action"]["id"] == "act-old"
    assert (
        card["pending_action"]["projection"]
        == ledger.project_for_operator("escalated")["projection"]
    )
    assert card["state"] == "needs_you"
    # …while the in-window sessions still get their ordinary cards.
    assert "claude:11111111-1111-1111-1111-111111111111" in cards


@pytest.mark.parametrize("path", ["/api/pulse/scan", "/api/pulse/ask", "/api/pulse/ask/stream"])
def test_the_retired_pulse_routes_are_gone(auth_cfg, fake_jsonl, path):  # noqa: ARG001
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(path, json={"query": "anything", "depth": "slow"}, headers=hdr)
    assert r.status_code in (404, 405), (path, r.status_code)


def test_ai_activity_endpoint(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/ai/activity")
    assert r.status_code == 200
    assert set(r.json()) == {"running", "last"}


def test_config_exposes_the_recent_window_and_refuses_the_retired_fields(auth_cfg, fake_jsonl):  # noqa: ARG001
    """/api/config's `pulse` block is the recent window plus `configured` (#2179); a write of
    a retired scan field is a 422 and persists nothing."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.get("/api/config").json()["pulse"] == {"window_days": 1, "configured": False}
    r = c.post("/api/prefs", json={"pulse": {"window_days": 3}}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["pulse"] == {"window_days": 3, "configured": False}
    assert c.get("/api/config").json()["pulse"]["window_days"] == 3
    over = c.post("/api/prefs", json={"pulse": {"window_days": 7}}, headers=hdr)
    assert over.status_code == 422  # #1086: the window is 1–3 days, and a write is strict
    for retired in ({"auto_enabled": True}, {"interval_minutes": 15}, {"scan_depth": "slow"}):
        r = c.post("/api/prefs", json={"pulse": {**retired, "window_days": 2}}, headers=hdr)
        assert r.status_code == 422, retired
    assert c.get("/api/config").json()["pulse"]["window_days"] == 3


# ---- prefs block ---------------------------------------------------------------------


def test_pulse_prefs_are_the_window_only_and_leave_retired_values_in_place(tmp_path):
    path = tmp_path / "prefs.json"
    stored = {"auto_enabled": True, "interval_minutes": 15, "scan_depth": "slow", "window_days": 2}
    path.write_text(json.dumps({"pulse": stored}))
    # Read: the window only; the retired values are not read…
    assert prefs.get_pulse(path) == {"window_days": 2}
    assert prefs.validate_pulse_patch({"window_days": 3}) is None
    assert prefs.validate_pulse_patch({"window_days": 4}) is not None  # #1086: ceiling is 3
    assert prefs.validate_pulse_patch({"bogus": 1}) is not None
    for retired in ("auto_enabled", "interval_minutes", "scan_depth"):
        assert prefs.validate_pulse_patch({retired: stored[retired]}) is not None, retired
    # …and a write leaves them in the file, untouched (no migration, nothing to roll back).
    assert prefs.set_pulse({"window_days": 3}, path) == {"window_days": 3}
    assert json.loads(path.read_text())["pulse"] == {**stored, "window_days": 3}


def test_pulse_window_constants_in_sync(tmp_path):
    # prefs.py owns the PULSE_* bounds and pulse.py mirrors them (pulse imports prefs; prefs never
    # imports pulse, so there is no cycle) — this guard fails if one drifts.
    assert prefs.PULSE_WINDOW_MIN == pulse.WINDOW_DAYS_MIN
    assert prefs.PULSE_WINDOW_MAX == pulse.WINDOW_DAYS_MAX
    assert prefs.get_pulse(tmp_path / "nope.json")["window_days"] == pulse.WINDOW_DAYS_DEFAULT


# ---- startup: no scan loop -----------------------------------------------------------


def test_no_pulse_scan_loop_starts_and_no_scan_model_call_is_made(
    auth_cfg, fake_jsonl, monkeypatch
):
    """#2179: the app starts no Pulse scan task. Every task the lifespan creates is recorded by
    the module its coroutine lives in; the AI review, clustering and orchestration loops are
    intentionally kept (and disabled here by their kill-switches), so with them off nothing at
    startup or on a few card reads reaches the model transport."""
    assert importlib.util.find_spec("agent_sessions.pulse_loop") is None
    for var in (
        "AGENT_SESSIONS_AI_REVIEW_LOOP",
        "AGENT_SESSIONS_AUTO_SORT_LOOP",
        "AGENT_SESSIONS_CLUSTERS_LOOP",
        "AGENT_SESSIONS_ORCHESTRATOR_LOOP",
    ):
        monkeypatch.setenv(var, "0")
    calls = _no_model_calls(monkeypatch)
    started: list[str] = []
    real_create_task = asyncio.create_task

    def _recording_create_task(coro, *a, **k):
        frame = getattr(coro, "cr_frame", None)
        started.append(frame.f_globals.get("__name__", "") if frame else "")
        return real_create_task(coro, *a, **k)

    monkeypatch.setattr(asyncio, "create_task", _recording_create_task)
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        _login(c, auth_cfg)
        for _ in range(3):
            assert c.get("/api/pulse").status_code == 200
    # The recorder saw the lifespan's tasks (a kept loop proves it was wired), and none is a scan.
    assert "agent_sessions.orchestrator_loop" in started
    assert not [m for m in started if m in ("agent_sessions.pulse_loop", "agent_sessions.pulse")]
    assert calls == []
