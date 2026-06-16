"""Pulse recent-work overview — scan engine, cache, and routes (#441 Phase 2).

The scan engine is tested against a monkeypatched session scan + metadata (no real ``$HOME``);
the routes are tested through the real app (TestClient) over the ``fake_jsonl`` fixture. Pinned:
the window filter, archived/excluded exclusion, state classification + ordering, fingerprint
stability + change-detection, the ``cache_version`` miss, and the route contract (instant GET,
manual scan, single-flight 409, CSRF gate, shared activity endpoint).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, metadata, pulse
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


@pytest.fixture
def pulse_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PULSE_CACHE", str(tmp_path / "pulse-cache.json"))
    return tmp_path / "pulse-cache.json"


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


# ---- scan engine --------------------------------------------------------------


def test_window_filter(pulse_cache, monkeypatch):
    now = 1_000_000.0
    recent = FakeSession("claude", "u-recent", "/a", now - 3600)
    old = FakeSession("claude", "u-old", "/a", now - 10 * 86400)
    _setup(monkeypatch, [recent, old])
    cards = pulse.build_cards(window_days=3, now=now)
    assert [c["id"] for c in cards] == ["claude:u-recent"]


def test_excludes_archived_and_review_excluded(pulse_cache, monkeypatch):
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


def test_state_classification_and_order(pulse_cache, monkeypatch):
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


def test_fingerprint_stable_then_changes(pulse_cache, monkeypatch):
    now = 1_000_000.0
    s = FakeSession("claude", "u", "/a", now - 100)
    _setup(monkeypatch, [s], {"claude:u": metadata.SessionMeta(review_fingerprint="fp1")})
    a1 = asyncio.run(pulse.run_scan(now=now))
    a2 = asyncio.run(pulse.run_scan(now=now))
    assert a1["input_fingerprint"] == a2["input_fingerprint"]  # nothing changed → stable
    # A new review result on the same session changes the fingerprint (loop would re-scan).
    _setup(monkeypatch, [s], {"claude:u": metadata.SessionMeta(review_fingerprint="fp2")})
    a3 = asyncio.run(pulse.run_scan(now=now))
    assert a3["input_fingerprint"] != a1["input_fingerprint"]


def test_cache_roundtrip_and_version_miss(pulse_cache, monkeypatch):
    now = 1_000_000.0
    _setup(monkeypatch, [FakeSession("claude", "u", "/a", now - 100)])
    art = asyncio.run(pulse.run_scan(now=now))
    loaded = pulse.load_cache()
    assert loaded["generated_at"] == now
    assert loaded["input_fingerprint"] == art["input_fingerprint"]
    # A future shape bump invalidates the old artifact → treated as a miss (never mis-rendered).
    data = json.loads(pulse_cache.read_text())
    data["cache_version"] = pulse.CACHE_VERSION + 999
    pulse_cache.write_text(json.dumps(data))
    assert pulse.load_cache() is None


def test_fast_artifact_shape_and_strips_internal(pulse_cache, monkeypatch):
    now = 1_000_000.0
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(ai_summary="did a thing", review_fingerprint="fp")},
    )
    art = asyncio.run(pulse.run_scan(now=now))
    assert art["cache_version"] == pulse.CACHE_VERSION
    assert art["scan_depth"] == "fast"
    assert art["banner"] is None  # fast = no synthesis
    assert art["synthesis_skipped"] is False  # nothing requested to skip
    card = art["cards"][0]
    assert "_review_fingerprint" not in card  # internal fingerprint input is stripped
    assert card["ai_summary"] == "did a thing"
    assert {
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
    } <= set(card)


def test_empty_overview_shape():
    empty = pulse.empty_overview()
    assert empty["generated_at"] is None
    assert empty["cards"] == []
    assert empty["cache_version"] == pulse.CACHE_VERSION
    assert empty["window_days"] == pulse.WINDOW_DAYS_DEFAULT


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


def test_get_pulse_empty_before_first_scan(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/pulse").json()
    assert d["generated_at"] is None
    assert d["cards"] == []
    assert d["cache_version"] == pulse.CACHE_VERSION


def test_scan_then_cached_get(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    art = c.post("/api/pulse/scan", headers=hdr).json()
    assert art["generated_at"] is not None
    assert len(art["cards"]) >= 1  # the fake_jsonl live sessions land in the window
    assert all("_review_fingerprint" not in card for card in art["cards"])
    # GET now serves the cached artifact verbatim (same generated_at + fingerprint).
    g = c.get("/api/pulse").json()
    assert g["generated_at"] == art["generated_at"]
    assert g["input_fingerprint"] == art["input_fingerprint"]


def test_scan_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/pulse/scan")  # no CSRF / Origin
    assert r.status_code in (401, 403)


def test_scan_409_when_already_running(auth_cfg, fake_jsonl, monkeypatch):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    def _busy(*a, **k):
        raise aitasks.AlreadyRunning("pulse-scan")

    monkeypatch.setattr(aitasks, "single_flight", _busy)
    r = c.post("/api/pulse/scan", headers=hdr)
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]
    assert "running" in body and "last" in body  # the activity snapshot rides along


def test_ai_activity_endpoint(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/ai/activity")
    assert r.status_code == 200
    assert set(r.json()) == {"running", "last"}
