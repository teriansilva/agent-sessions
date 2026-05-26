"""Auth contract tests: cookie required, CSRF required on state-changing routes,
Origin/Referer match, /api/auth-check shape used by nginx auth_request."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions.main import create_app


def _client(auth_cfg):
    # base_url must be https:// because the session cookie is set with secure=True;
    # TestClient (httpx) won't carry a secure cookie across http requests.
    return TestClient(create_app(auth_cfg), base_url="https://testserver")


# ---- /api/auth-check is the nginx auth_request endpoint -----------------------


def test_auth_check_without_cookie_returns_401(auth_cfg):
    r = _client(auth_cfg).get("/api/auth-check")
    assert r.status_code == 401


def test_auth_check_with_valid_cookie_returns_204(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    r = c.get("/api/auth-check")
    assert r.status_code == 204


# ---- /api/sessions requires login ---------------------------------------------


def test_sessions_endpoint_requires_cookie(auth_cfg, fake_jsonl):
    r = _client(auth_cfg).get("/api/sessions")
    assert r.status_code == 401


# ---- /login fail-closed Origin/Referer (Hermes PR #1 finding #1) --------------


def test_login_rejected_without_origin_or_referer(auth_cfg):
    # No Origin and no Referer → must be rejected, even with valid creds.
    r = _client(auth_cfg).post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_login_rejected_with_wrong_origin(auth_cfg):
    r = _client(auth_cfg).post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_login_accepted_via_referer_fallback(auth_cfg):
    # No Origin header, but a Referer under the right host → accepted.
    r = _client(auth_cfg).post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Referer": f"{auth_cfg.origin}/login"},
    )
    assert r.status_code == 303


# ---- CSRF + Origin enforcement on state-changing routes -----------------------


def _login(c, cfg):
    """Log in and return the CSRF token (read from /api/config, the SPA bootstrap)."""
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_rename_requires_csrf_token(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # Cookie present, but no X-CSRF-Token header.
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "x"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_rename_rejects_wrong_origin(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


# ---- /healthz is open (deploy probe) ------------------------------------------


def test_healthz_no_auth_required(auth_cfg):
    r = _client(auth_cfg).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
