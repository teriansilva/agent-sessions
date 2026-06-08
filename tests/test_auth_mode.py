"""`AUTH_MODE=none` contract tests (#13 / #32 Phase 3).

In `none` mode there is no login at all: the app auto-establishes the admin
session so the SPA + CSRF + Origin still work, but the user is never prompted for
credentials. These tests assert:
  - config + auth-check work without logging in (no cookie),
  - CSRF + Origin are STILL enforced on state-changing routes (not bypassed),
  - the SPA shell is served at `/` (no redirect to /login),
  - `/login` redirects to `/`.

The `single-user` default is exercised unchanged by test_auth.py / test_api.py /
test_password.py via the existing `auth_cfg` fixture — those prove the default
path is behavior-equivalent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import main
from agent_sessions.auth import AuthConfig
from agent_sessions.main import create_app


@pytest.fixture
def none_cfg(monkeypatch) -> AuthConfig:
    # No username / password hash in the env — `none` mode must not require them.
    monkeypatch.delenv("AGENT_SESSIONS_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    return AuthConfig.from_env()


def _client(cfg):
    # https:// so the secure session cookie is carried across requests.
    return TestClient(create_app(cfg), base_url="https://testserver")


def _fake_dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root>spa-shell</div>")
    return d


def test_none_mode_does_not_require_credentials(none_cfg):
    # from_env() built a config without AGENT_SESSIONS_USERNAME/PASSWORD_HASH set.
    assert none_cfg.auth_mode == "none"


# (a) /api/config 200 without logging in.
def test_config_without_login(none_cfg):
    c = _client(none_cfg)
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["csrf"]  # CSRF token still issued so mutations work
    assert body["auth_mode"] == "none"
    assert body["must_change_password"] is False


# (b) /api/auth-check → 204 without a cookie.
def test_auth_check_204_without_cookie(none_cfg):
    # Fresh client, no prior request → no cookie jar entry.
    c = TestClient(create_app(none_cfg), base_url="https://testserver")
    r = c.get("/api/auth-check")
    assert r.status_code == 204


# (c) CSRF + Origin still enforced on a mutation, but succeeds with them and no login.
def test_prefs_requires_csrf_even_without_login(none_cfg):
    c = _client(none_cfg)
    # Missing X-CSRF-Token → 403 (CSRF NOT bypassed in none mode).
    r = c.post("/api/prefs", json={"theme": "dark"}, headers={"Origin": none_cfg.origin})
    assert r.status_code == 403


def test_prefs_rejects_wrong_origin_without_login(none_cfg):
    c = _client(none_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    # Right CSRF but wrong Origin → 403 (Origin NOT bypassed in none mode).
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_prefs_succeeds_with_csrf_and_origin_no_login(none_cfg):
    c = _client(none_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": none_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json()["theme"] == "dark"


# (d) GET / serves the SPA shell without a redirect to /login.
def test_index_serves_spa_without_login(none_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_WEB_DIST", _fake_dist(tmp_path))
    c = _client(none_cfg)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "spa-shell" in r.text


# (e) /login redirects to /.
def test_login_redirects_to_root(none_cfg):
    c = _client(none_cfg)
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
