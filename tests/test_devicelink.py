"""QR cross-device sign-in — device-authorization tests (#650).

Store-level tests pin the security-load-bearing invariants (atomic single-use, first-approval-
wins, expiry, outstanding cap); the HTTP tests drive the real end-to-end flow through two
clients — an unauthenticated "new client" and a signed-in "phone" — against one app instance
(so they share the in-process challenge store).
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from agent_sessions.auth import AuthConfig
from agent_sessions.devicelink import DeviceLinkStore
from agent_sessions.main import create_app

# ---- store: the atomic single-use core --------------------------------------------------


def test_claim_is_exactly_once_under_concurrency():
    store = DeviceLinkStore()
    ch = store.start()
    assert store.approve(ch.challenge_id)
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()  # line every thread up so they race on the same approved challenge
        results.append(store.claim(ch.claim_token))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _, minted in results if minted) == 1  # exactly one session minted
    assert all(state in ("approved", "consumed") for state, _ in results)


def test_approve_first_wins_and_no_deny_after_approve():
    store = DeviceLinkStore()
    ch = store.start()
    assert store.approve(ch.challenge_id) is True
    assert store.approve(ch.challenge_id) is False  # second approve is a no-op
    assert store.deny(ch.challenge_id) is False  # can't deny an already-approved challenge


def test_deny_blocks_claim():
    store = DeviceLinkStore()
    ch = store.start()
    assert store.deny(ch.challenge_id) is True
    assert store.claim(ch.claim_token) == ("denied", False)


def test_replay_after_consume_fails():
    store = DeviceLinkStore()
    ch = store.start()
    store.approve(ch.challenge_id)
    assert store.claim(ch.claim_token) == ("approved", True)
    assert store.claim(ch.claim_token) == ("consumed", False)  # replay is inert


def test_expiry_invalidates():
    now = {"t": 1000.0}
    store = DeviceLinkStore(ttl=90, clock=lambda: now["t"])
    ch = store.start()
    now["t"] += 91
    assert store.get(ch.challenge_id) is None
    assert store.approve(ch.challenge_id) is False
    assert store.claim(ch.claim_token) == ("expired", False)


def test_outstanding_cap_bounds_pending():
    store = DeviceLinkStore(max_outstanding=3)
    a = store.start()
    store.start()
    store.start()
    assert store.start() is None  # cap reached → caller returns 429
    store.deny(a.challenge_id)  # resolving one frees a pending slot
    assert store.start() is not None


# ---- HTTP: the real cross-device flow ---------------------------------------------------


def _app_and_clients(cfg: AuthConfig):
    app = create_app(cfg)
    origin = {"Origin": cfg.origin}
    new = TestClient(app, base_url="https://testserver")  # unauth "new client"
    phone = TestClient(app, base_url="https://testserver")  # signed-in "phone"
    r = phone.post(
        "/login",
        data={"username": cfg.username, "password": "hunter2"},
        headers=origin,
        follow_redirects=False,
    )
    assert r.status_code == 303
    return new, phone, origin


def test_full_flow_signs_in_the_new_client(auth_cfg):
    new, phone, origin = _app_and_clients(auth_cfg)
    body = new.post("/link/start", headers=origin).json()
    cid, claim = body["challenge_id"], body["claim_token"]
    assert new.get(f"/link/status?t={claim}").json()["state"] == "pending"
    assert new.get("/api/auth-check").status_code == 401  # not signed in yet
    assert (
        phone.post("/link/approve", data={"challenge_id": cid}, headers=origin).status_code == 200
    )
    assert new.get(f"/link/status?t={claim}").json()["state"] == "approved"  # sets the cookie
    assert new.get("/api/auth-check").status_code == 204  # now signed in


def test_start_requires_origin(auth_cfg):
    new, _phone, _origin = _app_and_clients(auth_cfg)
    assert new.post("/link/start").status_code == 403


def test_unauth_cannot_approve(auth_cfg):
    new, _phone, origin = _app_and_clients(auth_cfg)
    cid = new.post("/link/start", headers=origin).json()["challenge_id"]
    assert new.post("/link/approve", data={"challenge_id": cid}, headers=origin).status_code == 401


def test_approve_requires_origin(auth_cfg):
    new, phone, origin = _app_and_clients(auth_cfg)
    cid = new.post("/link/start", headers=origin).json()["challenge_id"]
    assert phone.post("/link/approve", data={"challenge_id": cid}).status_code == 403


def test_deny_surfaces_and_never_signs_in(auth_cfg):
    new, phone, origin = _app_and_clients(auth_cfg)
    body = new.post("/link/start", headers=origin).json()
    cid, claim = body["challenge_id"], body["claim_token"]
    phone.post("/link/deny", data={"challenge_id": cid}, headers=origin)
    assert new.get(f"/link/status?t={claim}").json()["state"] == "denied"
    assert new.get("/api/auth-check").status_code == 401


def test_status_consumes_exactly_once(auth_cfg):
    new, phone, origin = _app_and_clients(auth_cfg)
    body = new.post("/link/start", headers=origin).json()
    cid, claim = body["challenge_id"], body["claim_token"]
    phone.post("/link/approve", data={"challenge_id": cid}, headers=origin)
    assert new.get(f"/link/status?t={claim}").json()["state"] == "approved"
    assert new.get(f"/link/status?t={claim}").json()["state"] == "consumed"  # terminal on replay


def test_qr_encodes_only_the_public_challenge_id(auth_cfg):
    new, _phone, origin = _app_and_clients(auth_cfg)
    body = new.post("/link/start", headers=origin).json()
    r = new.get(f"/link/qr?c={body['challenge_id']}")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    svg = r.content.decode("utf-8", "replace")
    assert body["claim_token"] not in svg  # the PRIVATE poll token never enters the QR


def test_qr_unknown_challenge_is_404(auth_cfg):
    new, _phone, _origin = _app_and_clients(auth_cfg)
    assert new.get("/link/qr?c=nope").status_code == 404


def test_start_is_rate_limited(auth_cfg):
    new, _phone, origin = _app_and_clients(auth_cfg)
    codes = [new.post("/link/start", headers=origin).status_code for _ in range(14)]
    assert 429 in codes  # the login page can't be scripted into a challenge factory
    assert codes.count(200) <= 12


def test_forced_password_change_blocks_approval(auth_cfg, monkeypatch):
    # A signed-in phone that still owes the forced first-login password change is redirected to
    # /change-password by the gate, so it can't approve a new device — no TOTP/change bypass.
    monkeypatch.setenv("AGENT_SESSIONS_FORCE_PASSWORD_CHANGE", "1")
    app = create_app(auth_cfg)
    phone = TestClient(app, base_url="https://testserver")
    r = phone.post(
        "/login",
        data={"username": auth_cfg.username, "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = phone.get("/link/approve?c=anything", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/change-password"


def _none_cfg(monkeypatch) -> AuthConfig:
    monkeypatch.delenv("AGENT_SESSIONS_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    return AuthConfig.from_env()


def test_none_mode_disables_device_link(monkeypatch):
    cfg = _none_cfg(monkeypatch)
    c = TestClient(create_app(cfg), base_url="https://testserver")
    # No login exists in `none` mode → the whole surface 404s.
    assert c.post("/link/start", headers={"Origin": cfg.origin}).status_code == 404
    assert c.get("/link/status?t=x").status_code == 404
    assert c.get("/link/approve?c=x").status_code == 404


# ---- UI wiring (server side) -------------------------------------------------------------


def test_login_page_carries_the_qr_panel(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    html = c.get("/login").text
    assert 'id="qr-signin"' in html
    assert 'src="/static/link.js"' in html  # external script (CSP: script-src 'self')


def test_static_link_js_is_served(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.get("/static/link.js")
    assert r.status_code == 200
    assert "/link/start" in r.text and "/link/status" in r.text


def test_approve_page_renders_requester_details(auth_cfg):
    new, phone, origin = _app_and_clients(auth_cfg)
    cid = new.post("/link/start", headers={**origin, "User-Agent": "Chrome/149 macOS"}).json()[
        "challenge_id"
    ]
    html = phone.get(f"/link/approve?c={cid}").text
    assert "Approve sign-in on a new device?" in html
    assert "Chrome/149 macOS" in html  # requester UA surfaced for verification


def test_approve_page_invalid_for_unknown_challenge(auth_cfg):
    _new, phone, _origin = _app_and_clients(auth_cfg)
    html = phone.get("/link/approve?c=bogus").text
    assert "invalid or has expired" in html
