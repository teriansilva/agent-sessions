"""React SPA static-serve contract (#64).

The React SPA is the only UI. Verifies the SPA shell + deep-link history fallback
+ real built files are served, and that the catch-all never shadows the
API/ws/auth routes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions import main
from agent_sessions.main import create_app


def _fake_dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root>spa-shell</div>")
    (d / "sw.js").write_text("/* service worker */")
    (d / "assets" / "index-abc.js").write_text("console.log(1)")
    return d


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def test_spa_serves_shell_deeplinks_and_files(auth_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_WEB_DIST", _fake_dist(tmp_path))
    c = _client(auth_cfg)
    assert "spa-shell" in c.get("/").text  # shell at /
    assert "spa-shell" in c.get("/s/claude/abc123").text  # deep client route → shell
    assert "spa-shell" in c.get("/some/unknown/route").text  # any client route → shell
    assert c.get("/sw.js").text.strip() == "/* service worker */"  # real built file served
    # The SPA catch-all must NOT shadow the API (handled by its own route → auth-gated).
    assert c.get("/api/sessions", follow_redirects=False).status_code in (401, 403)


def test_spa_path_traversal_blocked(auth_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_WEB_DIST", _fake_dist(tmp_path))
    c = _client(auth_cfg)
    # A traversal attempt resolves outside dist → falls through to the SPA shell, never a file.
    r = c.get("/../../etc/passwd")
    assert "root:" not in r.text
