"""First-run onboarding flag (#463): the `onboarded` pref, its fresh-vs-existing-install
inference in `/api/config`, and the `POST /api/prefs` round-trip + validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions import prefs
from agent_sessions.main import create_app


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


# ---- store --------------------------------------------------------------------


def test_onboarded_unset_is_none(tmp_path):
    assert prefs.get_onboarded(tmp_path / "prefs.json") is None


def test_onboarded_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_onboarded(True, p) is True
    assert prefs.get_onboarded(p) is True
    assert prefs.set_onboarded(False, p) is False
    assert prefs.get_onboarded(p) is False


def test_has_any_prefs_signal(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.has_any_prefs(p) is False
    prefs.set_theme("light", p)  # any pref counts
    assert prefs.has_any_prefs(p) is True


# ---- /api/config inference ----------------------------------------------------


def test_fresh_install_is_not_onboarded(tmp_home, auth_cfg, monkeypatch):
    """No prefs + no scanned sessions (empty tmp HOME) ⇒ the wizard should show."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False


def test_existing_install_with_prefs_is_onboarded(tmp_home, auth_cfg, monkeypatch):
    """An install that has already set any pref is treated as onboarded (no regression)."""
    prefs_path = tmp_home / ".config" / "as" / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(prefs_path))
    prefs.set_theme("light", prefs_path)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is True


def test_install_with_sessions_is_onboarded(fake_jsonl, auth_cfg, monkeypatch):
    """No prefs, but ≥1 scanned session ⇒ existing install ⇒ onboarded (fake_jsonl lays
    down Claude JSONLs under tmp HOME)."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(fake_jsonl / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is True


def test_explicit_pref_wins_over_inference(fake_jsonl, auth_cfg, monkeypatch):
    """Even with sessions present, an explicit onboarded=false shows the wizard."""
    prefs_path = fake_jsonl / ".config" / "as" / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(prefs_path))
    prefs.set_onboarded(False, prefs_path)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False


# ---- POST /api/prefs ----------------------------------------------------------


def test_complete_onboarding_persists(tmp_home, auth_cfg, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False
    r = c.post(
        "/api/prefs",
        json={"onboarded": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json()["onboarded"] is True
    # Survives a fresh app/config read.
    assert c.get("/api/config").json()["onboarded"] is True


def test_onboarded_must_be_boolean(tmp_home, auth_cfg, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"onboarded": "yes"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
