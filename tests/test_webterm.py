"""The /ws/term route's auth + validation gates (issue #49 Phase 2b).

The happy-path PTY bridge needs a real dtach + engine binary, so it's validated
on staging; here we pin that an unauthenticated / cross-origin / unknown-session
client is rejected BEFORE the socket is accepted — no raw shell stream is ever
exposed without the same gate as the HTTP routes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions.auth import hash_password  # noqa: F401  (kept for parity w/ conftest)
from agent_sessions.main import create_app

_GOOD = "claude:11111111-1111-1111-1111-111111111111"


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login_headers(c, cfg, origin=None):
    """Log in and return ws headers carrying the session cookie explicitly.

    TestClient's websocket_connect does not reliably forward the cookie jar, so we
    pin the session cookie as a header — exactly what a browser sends."""
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    cookie = c.cookies.get("agent_sessions")
    return {"Origin": origin or cfg.origin, "Cookie": f"agent_sessions={cookie}"}


def test_ws_rejects_unauthenticated(auth_cfg):
    c = _client(auth_cfg)
    with pytest.raises(WebSocketDisconnect) as ei:  # closed before accept
        with c.websocket_connect(f"/ws/term/{_GOOD}", headers={"Origin": auth_cfg.origin}):
            pass
    assert ei.value.code == 4401


def test_ws_rejects_bad_origin(auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg, origin="https://evil.example")
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect(f"/ws/term/{_GOOD}", headers=headers):
            pass
    assert ei.value.code == 4403


def test_ws_rejects_bad_engine_id(auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws/term/claude:not-a-uuid", headers=headers):
            pass
    assert ei.value.code == 4404


def test_ws_rejects_unknown_session(fake_jsonl, auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    # valid auth + origin + uuid shape, but not in the scanned set → 4404
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect(
            "/ws/term/claude:99999999-9999-9999-9999-999999999999", headers=headers
        ):
            pass
    assert ei.value.code == 4404


def test_ws_closes_on_unresolvable_binary(fake_jsonl, auth_cfg, monkeypatch):
    # A valid, authed, scanned session whose engine binary resolved to a bare name
    # (not an absolute path) must close deterministically (4500), not raise before
    # accept. Regression for Hermes PR #51 review.
    from agent_sessions import zellij

    monkeypatch.setattr(zellij, "CLAUDE_BIN", "claude")  # bare name → PtyBridgeError
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect(
            "/ws/term/claude:11111111-1111-1111-1111-111111111111", headers=headers
        ):
            pass
    assert ei.value.code == 4500
