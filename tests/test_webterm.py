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


# ---- /term/{sid} xterm.js page (Phase 3) --------------------------------------


def test_term_page_requires_login(auth_cfg):
    c = _client(auth_cfg)
    r = c.get("/term/claude:11111111-1111-1111-1111-111111111111", follow_redirects=False)
    assert r.status_code == 303  # → /login


def test_term_page_ok_when_logged_in(auth_cfg):
    c = _client(auth_cfg)
    _login_headers(c, auth_cfg)  # sets the session cookie in the client jar
    r = c.get("/term/claude:11111111-1111-1111-1111-111111111111")
    assert r.status_code == 200
    assert "xterm" in r.text and "/ws/term/" in r.text


def test_term_page_bad_sid_404(auth_cfg):
    c = _client(auth_cfg)
    _login_headers(c, auth_cfg)
    r = c.get("/term/claude:not-a-uuid", follow_redirects=False)
    assert r.status_code == 404


def test_terminal_template_no_retry_covers_all_reject_codes():
    # Regression for Hermes PR #52 review: the client must NOT reconnect on any
    # deliberate server reject — esp. 4500 (misconfigured launch), which an earlier
    # `< 4500` range guard wrongly excluded (it would hammer every 1.2s).
    from pathlib import Path

    import agent_sessions

    html = (Path(agent_sessions.__file__).parent / "templates" / "terminal.html").read_text()
    assert "NO_RETRY" in html and "NO_RETRY.has(ev.code)" in html
    for code in ("4401", "4403", "4404", "4500"):
        assert code in html, f"reject code {code} missing from NO_RETRY set"


def test_ws_compose_parity_wiring():
    # Phase 4: the /term page must accept same-origin postMessage and the sidebar
    # must drive it in ws-mode (compose + nav keys). Static guard against regression.
    from pathlib import Path

    import agent_sessions

    tdir = Path(agent_sessions.__file__).parent / "templates"
    term = (tdir / "terminal.html").read_text()
    idx = (tdir / "index.html").read_text()
    # /term receiver: origin-checked message listener + key sequences + paste/clear
    assert "addEventListener('message'" in term
    assert "e.origin !== location.origin" in term
    assert "KEYSEQ" in term and "'\\x1b[200~'" in term  # bracketed paste
    # Key-message contract must match: sidebar posts {t:'key', name} → receiver must
    # read KEYSEQ[m.name], not m.key. Regression for Hermes PR #54 (name/key mismatch).
    assert "{ t: 'key', name }" in idx
    assert "KEYSEQ[m.name]" in term and "KEYSEQ[m.key]" not in term
    # sidebar driver: _wsPost + ws-branch in termKey + sendCompose
    assert "_wsPost(msg)" in idx
    assert "TERMINAL_BACKEND === 'ws'" in idx
