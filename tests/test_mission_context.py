"""`/context` route contract (#852, Phase 2a of #840).

These pin the security envelope `/context` does **not** inherit by being adjacent to the file
panel: it matches neither the `/api/files/` nor the `/api/git/` prefix the no-store middleware is
gated on, so it would otherwise serve git status — which carries absolute paths — cacheable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import missions
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


@pytest.fixture
def mission(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    return missions.create_mission("do it")["id"]


def test_context_requires_login(auth_cfg, mission):
    c = _client(auth_cfg)
    assert c.get(f"/api/missions/{mission}/context").status_code == 401


def test_context_is_never_cacheable_on_success(auth_cfg, mission, tmp_path):
    """Git status carries absolute paths. The no-store middleware is gated on the `/api/files/`
    and `/api/git/` prefixes, so this route inherits nothing by being adjacent."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mission}/context")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_context_is_never_cacheable_on_an_auth_failure(auth_cfg, mission):
    """`Depends(logged_in)` raises its 401 BEFORE any handler runs — the escape the middleware's
    own docstring records having to close, reopened by any route under a new prefix."""
    c = _client(auth_cfg)
    r = c.get(f"/api/missions/{mission}/context")
    assert r.status_code == 401
    assert r.headers.get("cache-control") == "no-store"


def test_context_takes_no_path_from_the_client(auth_cfg, mission):
    """The cwd comes from the mission row and nowhere else, so there is nothing to traverse with.
    A path query parameter must be inert, not merely validated."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mission}/context", params={"path": "../../etc"})
    assert r.status_code == 200
    body = r.json()
    assert body["cwd"] == "" or "etc" not in body["cwd"]


def test_context_fails_closed_when_git_is_unavailable_and_leaks_no_path(
    auth_cfg, tmp_path, monkeypatch
):
    """A named failure KIND, never the path that caused it.

    `cwd` itself is a deliberate part of this response (§14) — the property is that a failure
    does not smuggle additional filesystem detail out through the error, which is how an
    exception message leaks a path the response was never meant to carry.
    """
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    secret = tmp_path / "very-secret-dir"
    secret.mkdir()
    mid = missions.create_mission("x", project_id="p", cwd=str(secret))["id"]

    from agent_sessions.routes import missions as mroutes

    def boom(*a, **k):
        raise RuntimeError(f"failed reading {secret}")

    monkeypatch.setattr(mroutes.gitpanel, "git_status", boom)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mid}/context")
    assert r.status_code == 200
    assert r.json()["git"] is None
    assert r.json()["git_error"] == "RuntimeError"
    assert str(secret) not in r.json()["git_error"]
    assert "failed reading" not in r.text, "the exception message must not reach the client"
