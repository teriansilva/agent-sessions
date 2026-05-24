"""Forced first-login password change + change endpoint (#65 Phase 4b)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions.auth import verify_password
from agent_sessions.main import create_app


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg, password="hunter2"):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": password},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    return r.status_code


def _csrf(c):
    page = c.get("/", follow_redirects=False).text
    i = page.index("const CSRF = ") + len("const CSRF = ")
    return page[i : page.index(";", i)].strip().strip('"')


def test_api_password_change_validates_and_updates_live(auth_cfg, tmp_path, monkeypatch):
    envf = tmp_path / "env"
    envf.write_text("AGENT_SESSIONS_USERNAME=marcus\n")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    c = _client(auth_cfg)
    assert _login(c, auth_cfg) == 303
    csrf = _csrf(c)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    # wrong current → 403; weak new → 422.
    assert (
        c.post(
            "/api/password",
            json={"current_password": "nope", "new_password": "abcdefgh"},
            headers=hdr,
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/password",
            json={"current_password": "hunter2", "new_password": "short"},
            headers=hdr,
        ).status_code
        == 422
    )

    # success → 204, env updated (hash verifies the new password), and the LIVE app now
    # authenticates with the new password (old one rejected) without a restart.
    assert (
        c.post(
            "/api/password",
            json={"current_password": "hunter2", "new_password": "brandnewpw"},
            headers=hdr,
        ).status_code
        == 204
    )
    text = envf.read_text()
    hash_line = next(
        ln for ln in text.splitlines() if ln.startswith("AGENT_SESSIONS_PASSWORD_HASH=")
    )
    assert verify_password("brandnewpw", hash_line.split("=", 1)[1])
    assert _login(c, auth_cfg, password="brandnewpw") == 303
    assert _login(c, auth_cfg, password="hunter2") == 401


def test_api_password_requires_csrf(auth_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/password",
        json={"current_password": "hunter2", "new_password": "brandnewpw"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_must_change_gate_and_change_page(auth_cfg, tmp_path, monkeypatch):
    envf = tmp_path / "env"
    envf.write_text("")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.setenv("AGENT_SESSIONS_FORCE_PASSWORD_CHANGE", "1")
    c = _client(auth_cfg)
    assert _login(c, auth_cfg) == 303

    # Logged in + must-change → index forces the change page; config advertises it.
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/change-password"
    assert c.get("/api/config").json()["must_change_password"] is True
    assert c.get("/change-password").status_code == 200  # the wizard form
    # …and the rest of the authed API is BLOCKED until the password is changed (not just
    # the index): app routes 403, page navigations redirect; only the allowlist works.
    assert c.get("/api/sessions", follow_redirects=False).status_code == 403
    assert c.get("/term/claude:abc", follow_redirects=False).status_code == 303

    # Mismatch is rejected; a valid change clears the flag and lands on the app.
    bad = c.post(
        "/change-password",
        data={"current": "hunter2", "new": "brandnewpw", "confirm": "nope"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    ok = c.post(
        "/change-password",
        data={"current": "hunter2", "new": "brandnewpw", "confirm": "brandnewpw"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200  # no longer gated
    assert c.get("/api/config").json()["must_change_password"] is False
    assert "FORCE_PASSWORD_CHANGE" not in envf.read_text()
