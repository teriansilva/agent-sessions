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

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import aitasks, metadata, prefs, pulse, review
from agent_sessions.main import create_app

SECRET = "sk-pulse-test"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"


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


# ---- synthesis depths (medium / slow, #441 Phase 4) ----------------------------


@pytest.fixture
def configured_ai(tmp_path, monkeypatch):
    """Point prefs at tmp with a configured + enabled ai_review block (the reused gateway), and
    zero the synthesis call-spacing so the per-session pass doesn't sleep in tests."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    monkeypatch.setattr(pulse, "SYNTH_CALL_SPACING_S", 0)
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    return tmp_path


def _json_transport(payload: dict, calls: list | None = None):
    """A MockTransport returning the SAME JSON object for every completion. A payload carrying
    both ``banner`` and ``line`` keys serves the banner call and the per-session calls alike."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return httpx.MockTransport(handler)


def test_medium_makes_one_banner_call(pulse_cache, configured_ai, monkeypatch):
    now = 1_000_000.0
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(ai_summary="did x", review_fingerprint="fp")},
    )
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _json_transport({"banner": "Pick up tests"}, calls))
    art = asyncio.run(pulse.run_scan(depth="medium", now=now))
    assert len(calls) == 1  # exactly one synthesis call — the banner
    assert art["scan_depth"] == "medium"
    assert art["banner"] == "Pick up tests"
    assert art["synthesis_skipped"] is False
    assert art["cards"][0]["synthesis"] is None  # medium = banner only, no per-session pass


def test_slow_synthesizes_each_session_then_banner(pulse_cache, configured_ai, monkeypatch):
    now = 1_000_000.0
    sessions = [FakeSession("claude", f"u{i}", "/a", now - 100 * (i + 1)) for i in range(3)]
    meta = {
        f"claude:u{i}": metadata.SessionMeta(ai_summary=f"s{i}", review_fingerprint=f"fp{i}")
        for i in range(3)
    }
    _setup(monkeypatch, sessions, meta)
    calls: list = []
    payload = {"line": "continue here", "banner": "3 in flight"}
    monkeypatch.setattr(review, "_TRANSPORT", _json_transport(payload, calls))
    art = asyncio.run(pulse.run_scan(depth="slow", now=now))
    assert len(calls) == 3 + 1  # one per in-window session + the banner
    assert art["banner"] == "3 in flight"
    assert all(c["synthesis"] == "continue here" for c in art["cards"])
    assert art["synthesis_skipped"] is False


def test_slow_caps_per_session_calls(pulse_cache, configured_ai, monkeypatch):
    now = 1_000_000.0
    n = pulse.SLOW_SESSION_CAP + 3
    sessions = [FakeSession("claude", f"u{i:02d}", "/a", now - 100 * (i + 1)) for i in range(n)]
    meta = {
        f"claude:u{i:02d}": metadata.SessionMeta(ai_summary=f"s{i}", review_fingerprint=f"fp{i}")
        for i in range(n)
    }
    _setup(monkeypatch, sessions, meta)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _json_transport({"line": "go", "banner": "b"}, calls))
    art = asyncio.run(pulse.run_scan(depth="slow", now=now))
    # Bounded: SLOW_SESSION_CAP per-session calls + 1 banner — the rest keep their ai_summary.
    assert len(calls) == pulse.SLOW_SESSION_CAP + 1
    synthesized = [c for c in art["cards"] if c["synthesis"] is not None]
    assert len(synthesized) == pulse.SLOW_SESSION_CAP


def test_unconfigured_endpoint_degrades_to_fast(pulse_cache, tmp_path, monkeypatch):
    # The single unconfigured contract: depth >= medium degrades to fast curation, flagged
    # `synthesis_skipped`, NEVER raising — the cards keep their existing ai_summary lines.
    now = 1_000_000.0
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))  # ai_review unset
    monkeypatch.setattr(review, "_TRANSPORT", None)
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(ai_summary="did x", review_fingerprint="fp")},
    )
    art = asyncio.run(pulse.run_scan(depth="medium", now=now))
    assert art["banner"] is None
    assert art["synthesis_skipped"] is True
    assert art["scan_depth"] == "medium"  # records what was requested
    assert art["cards"][0]["synthesis"] is None
    assert art["cards"][0]["ai_summary"] == "did x"


def test_synthesis_output_is_bounded_plain_data(pulse_cache, configured_ai, monkeypatch):
    # Model output is DATA: whitespace collapsed, length-capped (rendered as plain text in UI).
    now = 1_000_000.0
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(review_fingerprint="fp")},
    )
    payload = {"banner": "  a\n\nb  " + "x" * 5000, "line": "  multi   space  "}
    monkeypatch.setattr(review, "_TRANSPORT", _json_transport(payload))
    art = asyncio.run(pulse.run_scan(depth="slow", now=now))
    assert len(art["banner"]) <= pulse.BANNER_MAX
    assert "\n" not in art["banner"]
    assert art["cards"][0]["synthesis"] == "multi space"


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


def test_config_exposes_pulse_and_prefs_roundtrip(auth_cfg, fake_jsonl):
    # /api/config carries the public pulse block (#441 Phase 3); POST /api/prefs validates +
    # persists a partial patch, and a bad depth is a 422 (never silently coerced on write).
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    cfg = c.get("/api/config").json()
    assert cfg["pulse"]["scan_depth"] == "fast"
    assert cfg["pulse"]["auto_enabled"] is False
    assert cfg["pulse"]["configured"] is False  # no ai_review endpoint → synthesis would degrade
    r = c.post(
        "/api/prefs",
        json={"pulse": {"auto_enabled": True, "scan_depth": "medium", "window_days": 7}},
        headers=hdr,
    )
    assert r.status_code == 200
    assert r.json()["pulse"]["auto_enabled"] is True
    after = c.get("/api/config").json()["pulse"]
    assert after["scan_depth"] == "medium" and after["window_days"] == 7
    bad = c.post("/api/prefs", json={"pulse": {"scan_depth": "turbo"}}, headers=hdr)
    assert bad.status_code == 422


def test_scan_honors_body_depth_override(auth_cfg, fake_jsonl):
    # The page's depth control can request a one-off depth via the POST body; with no ai_review
    # endpoint a medium scan degrades to fast (200 + synthesis_skipped), never 409.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    art = c.post("/api/pulse/scan", json={"depth": "medium"}, headers=hdr).json()
    assert art["scan_depth"] == "medium"
    assert art["synthesis_skipped"] is True
    assert art["banner"] is None


# ---- #611: the slow per-session pass prefers the recap over the distilled summary --------


def test_slow_pass_sends_the_recap_when_one_exists(pulse_cache, configured_ai, monkeypatch):
    """`ai_recap` (#481) is a ≤1500-char chronological brief on the same sidecar; `ai_summary`
    is one ≤200-char line distilled from it. Same call, same cost — hand the model the fuller
    input rather than re-summarizing a summary."""
    now = 1_000_000.0
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {
            "claude:u": metadata.SessionMeta(
                ai_summary="did x",
                ai_recap="Cloned repo.\nRan the suite.\nTwo tests fail.",
                review_fingerprint="fp",
            )
        },
    )
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _json_transport({"line": "fix them", "banner": "b"}, calls)
    )
    art = asyncio.run(pulse.run_scan(depth="slow", now=now))

    session_call = json.loads(calls[0]["messages"][1]["content"])
    assert session_call["summary"] == "Cloned repo.\nRan the suite.\nTwo tests fail."
    assert art["cards"][0]["synthesis"] == "fix them"
    # The internal field never leaks into the public artifact.
    assert "_ai_recap" not in art["cards"][0]


def test_slow_pass_falls_back_to_the_summary_without_a_recap(
    pulse_cache, configured_ai, monkeypatch
):
    now = 1_000_000.0
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(ai_summary="did x", review_fingerprint="fp")},
    )
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _json_transport({"line": "go on", "banner": "b"}, calls)
    )
    asyncio.run(pulse.run_scan(depth="slow", now=now))
    assert json.loads(calls[0]["messages"][1]["content"])["summary"] == "did x"


def test_recap_change_moves_the_scan_input_fingerprint(pulse_cache, configured_ai, monkeypatch):
    """Hermes on PR #618: the `slow` pass now feeds `_ai_recap` into synthesis, so the recap is a
    scan INPUT. If the input fingerprint still hashed only `_review_fingerprint`, a recap that
    arrived later (summary fingerprint unchanged — a failed recap call finally succeeding, or a
    legacy session backfilled) would leave `fingerprint_for()` identical and the Phase-3 loop
    would skip the scan as unchanged, stranding the pre-recap synthesis forever."""
    now = 1_000_000.0
    session = FakeSession("claude", "u", "/a", now - 100)

    _setup(
        monkeypatch,
        [session],
        {
            "claude:u": metadata.SessionMeta(
                ai_summary="did x", review_fingerprint="fp", recap_fingerprint=""
            )
        },
    )
    before = asyncio.run(pulse.fingerprint_for(window_days=3, depth="slow", now=now))

    # Same summary + same review fingerprint; only the recap landed.
    _setup(
        monkeypatch,
        [session],
        {
            "claude:u": metadata.SessionMeta(
                ai_summary="did x",
                review_fingerprint="fp",
                ai_recap="Cloned repo.\nRan the suite.",
                recap_fingerprint="rfp",
            )
        },
    )
    after = asyncio.run(pulse.fingerprint_for(window_days=3, depth="slow", now=now))

    assert before != after
