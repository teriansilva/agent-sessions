"""Endpoint tests for the sidebar-UX surface: pagination, projects, rename,
archive/unarchive, new-session — including CSRF/origin gating."""

from __future__ import annotations

from fastapi.testclient import TestClient

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
    page = c.get("/", follow_redirects=False).text
    i = page.index("const CSRF = ") + len("const CSRF = ")
    return page[i : page.index(";", i)].strip().strip('"')


# ---- pagination ---------------------------------------------------------------


def test_sessions_paginated_shape(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions?limit=2&offset=0")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"sessions", "next_offset", "total"}
    assert len(d["sessions"]) == 2
    # 4 live sessions in the fixture (1 archived excluded) → next_offset advances
    assert d["total"] == 4
    assert d["next_offset"] == 2
    # newest-first by mtime
    mtimes = [s["last_mtime"] for s in d["sessions"]]
    assert mtimes == sorted(mtimes, reverse=True)


def test_sessions_archived_filter(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    active = c.get("/api/sessions?archived=0").json()
    archived = c.get("/api/sessions?archived=1").json()
    assert all(not s["archived"] for s in active["sessions"])
    assert all(s["archived"] for s in archived["sessions"])
    assert archived["total"] == 1  # the one archived fixture


# ---- projects picker ----------------------------------------------------------


def test_projects_endpoint(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/projects")
    assert r.status_code == 200
    cwds = {p["cwd"] for p in r.json()["projects"]}
    assert "/tmp/other" in cwds


# ---- rename -------------------------------------------------------------------


def test_rename_persists(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(
        f"/api/sessions/{uuid}/rename",
        json={"title": "My Refactor"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["title"] == "My Refactor"
    # reflected in the list
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["title"] == "My Refactor"


def test_rename_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "x"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_rename_empty_title_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "   "},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- archive / unarchive ------------------------------------------------------


def test_archive_endpoint_moves_and_filters(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(
        f"/api/sessions/{uuid}/archive", headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    )
    assert r.status_code == 200
    active = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    assert uuid not in active
    archived = {s["uuid"] for s in c.get("/api/sessions?archived=1&limit=50").json()["sessions"]}
    assert uuid in archived


def test_archive_unknown_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/99999999-9999-9999-9999-999999999999/archive",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404


# ---- new session --------------------------------------------------------------


def test_new_session_success(auth_cfg, fake_jsonl, monkeypatch):
    import agent_sessions.zellij as z

    monkeypatch.setattr(z, "new_session", lambda **kw: "new:demo")
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/projects/new",
        json={"cwd": "/tmp/other", "name": "demo", "bypass_permissions": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["tab"] == "new:demo"


def test_new_session_bad_cwd_400(auth_cfg, fake_jsonl):
    # No stub: the real new_session validates the cwd allowlist before any
    # subprocess and raises ZellijError → 400.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/projects/new",
        json={"cwd": "/etc", "name": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 400


def test_new_session_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/projects/new", json={"cwd": "/tmp/other"}, headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403
