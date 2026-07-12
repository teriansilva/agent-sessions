"""Browser containment headers (#612 P1)."""

from __future__ import annotations

import base64
import hashlib

from fastapi.testclient import TestClient

from agent_sessions import main, security_headers
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


def test_containment_headers_on_api_response(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/config")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]
    # Mic stays granted to same-origin so push-to-talk dictation (#483/#486) works — an empty
    # `microphone=()` allowlist would deny it to self and break voice input.
    assert "microphone=(self)" in r.headers["Permissions-Policy"]
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "connect-src 'self'" in csp  # same-origin /api + terminal WebSocket


def test_containment_headers_on_login_page(auth_cfg):
    c = _client(auth_cfg)
    r = c.get("/login")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_script_src_locked_down_but_styles_allow_inline(auth_cfg):
    c = _client(auth_cfg)
    csp = c.get("/login").headers["Content-Security-Policy"]
    directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
    # script-src must NOT allow 'unsafe-inline' — that's the whole point (XSS guard).
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'self'" in directives["script-src"]
    # style-src keeps 'unsafe-inline' (xterm runtime styles + template <style> can't be hashed).
    assert "'unsafe-inline'" in directives["style-src"]


def test_hsts_only_over_tls(auth_cfg):
    # The header key is the raw X-Forwarded-Proto (works even without uvicorn --proxy-headers).
    assert security_headers.is_https(forwarded_proto="https", scheme="http") is True
    assert security_headers.is_https(forwarded_proto="http", scheme="http") is False
    assert security_headers.is_https(forwarded_proto=None, scheme="https") is True
    assert security_headers.is_https(forwarded_proto=None, scheme="http") is False

    c = _client(auth_cfg)
    over_tls = c.get("/login", headers={"X-Forwarded-Proto": "https"})
    assert over_tls.headers["Strict-Transport-Security"] == security_headers.HSTS
    plain = c.get("/login", headers={"X-Forwarded-Proto": "http"})
    assert "Strict-Transport-Security" not in plain.headers


def test_inline_theme_script_is_hashed_not_unsafe_inline(tmp_path, monkeypatch, auth_cfg):
    # A built index with an inline theme-init script → its exact sha256 lands in script-src, so
    # the real SPA boots under CSP without 'unsafe-inline'. The external module script is NOT
    # hashed (it's allowed by 'self').
    dist = tmp_path / "dist"
    dist.mkdir()
    inline = '(function(){document.documentElement.dataset.theme="dark";})();'
    (dist / "index.html").write_text(
        f"<!doctype html><html><head><script>{inline}</script>"
        '<script type="module" crossorigin src="/assets/main.js"></script>'
        "</head><body></body></html>"
    )
    expected = (
        "'sha256-" + base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode() + "'"
    )
    assert security_headers.inline_script_hashes(dist) == [expected]

    csp = security_headers.content_security_policy([expected])
    assert f"script-src 'self' {expected}" in csp

    # And end-to-end: create_app resolves the CSP against the built index → the response CSP
    # carries the hash, so the served SPA's inline script is permitted.
    monkeypatch.setattr(main, "_WEB_DIST", dist)
    c = _client(auth_cfg)
    assert expected in c.get("/login").headers["Content-Security-Policy"]


def test_no_built_index_falls_back_to_self_only(tmp_path):
    assert security_headers.inline_script_hashes(tmp_path) == []  # nothing to read
    csp = security_headers.content_security_policy([])
    assert "script-src 'self';" in csp + ";"
