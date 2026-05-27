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


# ---- sidebar_view (#139) ------------------------------------------------------


def test_default_sidebar_view_when_unset(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_sidebar_view(p) == "list"


def test_sidebar_view_round_trip_and_invalid(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_sidebar_view("overview", p) == "overview"
    assert prefs.get_sidebar_view(p) == "overview"
    assert prefs.set_sidebar_view("bogus", p) == "list"  # invalid → default


def test_sidebar_view_and_theme_coexist(tmp_path):
    # Setting one pref must not clobber the other (read-modify-write).
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_sidebar_view("overview", p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_sidebar_view(p) == "overview"


def test_config_exposes_sidebar_view(auth_cfg, tmp_home):
    prefs.set_sidebar_view("overview")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["sidebar_view"] == "overview"


def test_set_sidebar_view_endpoint_persists_without_clobbering_theme(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"theme": "dark"}, headers=hdrs).status_code == 200
    r = c.post("/api/prefs", json={"sidebar_view": "overview"}, headers=hdrs)
    assert r.status_code == 200 and r.json() == {"sidebar_view": "overview"}
    cfg = c.get("/api/config").json()
    assert cfg["sidebar_view"] == "overview" and cfg["theme"] == "dark"


def test_set_sidebar_view_unknown_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"sidebar_view": "spreadsheet"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_prefs_no_known_key_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"nope": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
