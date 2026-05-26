"""Unit tests for the app-preferences store + the theme config/write endpoints (#109)."""

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
    # CSRF token comes from the SPA bootstrap endpoint (/api/config).
    return c.get("/api/config").json()["csrf"]


# ---- store --------------------------------------------------------------------


def test_default_theme_when_unset(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_theme(p) == "royal"


def test_set_and_get_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_theme("dark", p) == "dark"
    assert prefs.get_theme(p) == "dark"


def test_invalid_theme_coerced_to_default(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_theme("neon", p) == "royal"
    assert prefs.get_theme(p) == "royal"


def test_corrupt_file_tolerated(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text("{ this is not json")
    assert prefs.get_theme(p) == "royal"
    # a write recovers the file
    assert prefs.set_theme("light", p) == "light"
    assert prefs.get_theme(p) == "light"


def test_set_preserves_other_keys(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text('{"keepme": 7}')
    prefs.set_theme("dark", p)
    import json

    data = json.loads(p.read_text())
    assert data == {"keepme": 7, "theme": "dark"}


# ---- endpoints ----------------------------------------------------------------


def test_config_exposes_theme(auth_cfg, tmp_home):
    prefs.set_theme("light")  # writes under tmp_home/.config/... (HOME monkeypatched)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["theme"] == "light"


def test_set_theme_endpoint_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json() == {"theme": "dark"}
    assert c.get("/api/config").json()["theme"] == "dark"


def test_set_theme_unknown_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"theme": "neon"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_set_theme_requires_csrf(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"theme": "dark"}, headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


def test_set_theme_non_object_json_422(auth_cfg, tmp_home):
    # A valid-CSRF request with a JSON array/string/number must be a controlled 422,
    # not a 500 from .get() on a non-dict (Hermes PR #111 review).
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for body in ([], "dark", 7):
        r = c.post(
            "/api/prefs",
            json=body,
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        )
        assert r.status_code == 422, body
