"""Public share links (#1404 P1) — the grant store and the public authorization boundary.

Store tests pin what makes a link safe to hand out (the token is never stored, a dead link
resolves to nothing, revoke is fenced and announced); the HTTP tests drive an operator and an
anonymous visitor against one app, and pin that the two cookies never stand in for each other.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from agent_sessions import shares
from agent_sessions.auth import AuthConfig
from agent_sessions.main import create_app
from agent_sessions.routes import shares as shares_routes

TERM = "claude:11111111-1111-1111-1111-111111111111"
API = "codex-api:0f8fad5b-d9cb-469f-a165-70867728950e"


def _token(url: str) -> str:
    m = re.search(r"/p/([A-Za-z0-9_-]+)$", url)
    assert m, url
    return m.group(1)


# ---- store ---------------------------------------------------------------------------------


def test_the_token_and_password_never_reach_the_database(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    st = shares.ShareStore(d / "shares.db")
    share, token = st.create(session_key=API, kind="api", mode="view", password="correct horse")
    files = list(d.iterdir())  # the db and its WAL/SHM siblings
    assert files
    for f in files:
        raw = f.read_bytes()
        assert token.encode() not in raw
        assert b"correct horse" not in raw
    with sqlite3.connect(d / "shares.db") as con:
        (dig,) = con.execute("SELECT token_digest FROM shares").fetchone()
    assert dig == shares.digest(token) and dig != token
    assert "token" not in share.public() and "password_hash" not in share.public()


def test_the_database_is_created_0600(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    st.create(session_key=TERM, kind="terminal", mode="view")
    assert stat.S_IMODE(os.stat(tmp_path / "shares.db").st_mode) == 0o600


def test_only_a_live_link_resolves(tmp_path):
    now = {"t": 1_000_000.0}
    st = shares.ShareStore(tmp_path / "shares.db", clock=lambda: now["t"])
    live, t1 = st.create(session_key=TERM, kind="terminal", mode="view")
    soon, t2 = st.create(session_key=TERM, kind="terminal", mode="view", expires_at=now["t"] + 60)
    gone, t3 = st.create(session_key=TERM, kind="terminal", mode="view")
    st.revoke(gone.share_id)
    assert st.resolve_token(t1)[0].share_id == live.share_id
    assert st.resolve_token(t2) is not None
    assert st.resolve_token(t3) is None  # revoked
    assert st.resolve_token(t1[:-1] + ("A" if t1[-1] != "A" else "B")) is None  # wrong
    assert st.resolve_token("not a token!") is None
    now["t"] += 61
    assert st.resolve_token(t2) is None  # expired
    assert st.active(soon.share_id) is None
    assert [s.share_id for s in st.list_active()] == [live.share_id]


def test_a_terminal_link_is_never_a_send_link(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    with pytest.raises(shares.ShareError):
        st.create(session_key=TERM, kind="terminal", mode="send")


def test_create_refuses_a_past_or_absurd_expiry(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db", clock=lambda: 1_000_000.0)
    with pytest.raises(shares.ShareError):
        st.create(session_key=TERM, kind="terminal", mode="view", expires_at=999_999.0)
    with pytest.raises(shares.ShareError):
        st.create(
            session_key=TERM,
            kind="terminal",
            mode="view",
            expires_at=1_000_000.0 + shares.MAX_EXPIRY_S + 1,
        )


def test_revoke_waits_for_a_send_holding_the_fence(tmp_path):
    """The P2 send path re-checks the share and performs its effect under the fence; a revoke
    arriving meanwhile must land AFTER the effect, never between the check and the effect."""
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key=API, kind="api", mode="send")
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def send() -> None:
        with st.fence(share.share_id):
            assert st.active(share.share_id) is not None
            entered.set()
            release.wait(5)
            order.append("effect")

    t = threading.Thread(target=send)
    t.start()
    entered.wait(5)
    r = threading.Thread(target=lambda: (st.revoke(share.share_id), order.append("revoked")))
    r.start()
    time.sleep(0.05)
    assert order == []  # revoke is blocked on the fence
    release.set()
    t.join(5)
    r.join(5)
    assert order == ["effect", "revoked"]
    with st.fence(share.share_id):  # the next send sees it dead
        assert st.active(share.share_id) is None


def test_revoke_announces_to_listeners_and_a_failing_listener_cannot_stop_it(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key=TERM, kind="terminal", mode="view")
    heard: list[str] = []

    def boom(_sid: str) -> None:
        raise RuntimeError("listener bug")

    st.on_end(boom)
    off = st.on_end(heard.append)
    assert st.revoke(share.share_id) is True
    assert heard == [share.share_id]
    assert st.revoke(share.share_id) is False  # already ended: no second announcement
    assert heard == [share.share_id]
    off()


def test_revoke_for_session_ends_only_that_sessions_links(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    a, _ = st.create(session_key=TERM, kind="terminal", mode="view")
    b, _ = st.create(session_key=API, kind="api", mode="view")
    assert st.revoke_for_session(TERM, reason="archived") == 1
    assert st.get(a.share_id).revoked_reason == "archived"
    assert st.active(b.share_id) is not None


def test_sweep_drops_long_dead_rows_only(tmp_path):
    now = {"t": 1_000_000.0}
    st = shares.ShareStore(tmp_path / "shares.db", clock=lambda: now["t"])
    old, _ = st.create(session_key=TERM, kind="terminal", mode="view")
    st.revoke(old.share_id)
    keep, _ = st.create(session_key=TERM, kind="terminal", mode="view")
    now["t"] += 8 * 86400
    assert st.sweep() == 1
    assert st.get(old.share_id) is None and st.get(keep.share_id) is not None


def test_unlock_throttle_pauses_a_client_then_the_whole_share():
    now = {"t": 0.0}
    th = shares.UnlockThrottle(clock=lambda: now["t"])
    for i in range(shares.PER_CLIENT_FAILS):
        assert th.reserve("s", "1.1.1.1") is not None
        assert th.tries_left("s", "1.1.1.1") == shares.PER_CLIENT_FAILS - i - 1
    assert th.reserve("s", "1.1.1.1") is None and th.paused("s", "1.1.1.1")
    assert not th.paused("s", "2.2.2.2")  # another client still has tries
    for n in range(shares.PER_SHARE_FAILS - shares.PER_CLIENT_FAILS):
        assert th.reserve("s", f"10.0.0.{n}") is not None
    assert th.reserve("s", "3.3.3.3") is None  # the share as a whole is paused
    now["t"] += shares.PAUSE_S + 1
    assert not th.paused("s", "1.1.1.1") and not th.paused("s", "3.3.3.3")


def test_a_correct_password_hands_its_reservation_back():
    th = shares.UnlockThrottle()
    for _ in range(shares.PER_CLIENT_FAILS - 1):
        th.reserve("s", "1.1.1.1")
    stamp = th.reserve("s", "1.1.1.1")
    th.succeed("s", "1.1.1.1", stamp)
    assert th.tries_left("s", "1.1.1.1") == shares.PER_CLIENT_FAILS  # the client's count clears


def test_concurrent_guesses_cannot_exceed_the_share_budget():
    """Hermes on #1414: checking the budget before the slow PBKDF2 and counting after let a
    barrier of simultaneous guesses through. Reservation is atomic with the check."""
    th = shares.UnlockThrottle()
    n = shares.PER_SHARE_FAILS + 10
    barrier = threading.Barrier(n)
    got: list[float | None] = []

    def guess(i: int) -> None:
        barrier.wait()
        got.append(th.reserve("s", f"10.1.{i // 250}.{i % 250}"))

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for g in got if g is not None) == shares.PER_SHARE_FAILS


class _Req:
    def __init__(self, peer: str, headers: dict[str, str]):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


@pytest.mark.parametrize(
    "peer,headers,want",
    [
        ("127.0.0.1", {"x-real-ip": "203.0.113.7"}, "203.0.113.7"),  # the local proxy's word
        ("::1", {"x-real-ip": "2001:db8::1"}, "2001:db8::1"),
        ("127.0.0.1", {"x-real-ip": "not-an-ip"}, "127.0.0.1"),
        ("127.0.0.1", {"x-forwarded-for": "6.6.6.6"}, "127.0.0.1"),  # XFF is never read
        ("198.51.100.4", {"x-real-ip": "6.6.6.6"}, "198.51.100.4"),  # not from a local proxy
        ("198.51.100.4", {"x-forwarded-for": "6.6.6.6, 198.51.100.4"}, "198.51.100.4"),
    ],
)
def test_client_ip_trusts_only_the_local_proxy(peer, headers, want):
    assert shares_routes._client_ip(_Req(peer, headers)) == want


def test_replacing_the_key_ends_every_link(tmp_path):
    from agent_sessions import template_secrets

    st = shares.ShareStore(tmp_path / "shares.db")
    share, token = st.create(session_key=TERM, kind="terminal", mode="view")
    assert st.active(share.share_id) is not None
    key = Path(os.environ["AGENT_SESSIONS_TEMPLATE_SECRETS_KEY"])
    key.unlink()
    template_secrets._reset_for_tests()
    template_secrets.keyed_digest("x", "y")  # a NEW key is created
    assert st.active(share.share_id) is None  # an unlocked visitor's cookie names a dead row
    assert st.resolve_token(token) is None
    assert st.list_active() == []


# ---- HTTP ----------------------------------------------------------------------------------


@pytest.fixture
def app(auth_cfg, monkeypatch):
    """An app where every session the tests name exists and is live. The tests of the real
    existence / archived check build their own app over `fake_jsonl`."""
    monkeypatch.setattr(shares_routes, "_session_state", lambda prov, native: "live")
    return create_app(auth_cfg)


def _operator(app, cfg: AuthConfig) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": cfg.origin})
    return c


def _visitor(app, cfg: AuthConfig) -> TestClient:
    """A fresh browser context: no cookies, same-origin requests from the viewer page."""
    c = TestClient(app, base_url="https://testserver")
    c.headers.update({"Origin": cfg.origin})
    return c


def _create(op: TestClient, **body) -> dict:
    if body.get("mode") == "send":
        body.setdefault("accept_risk", True)
    r = op.post("/api/shares", json={"session": TERM, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_operator_creates_a_link_and_a_fresh_visitor_unlocks_it(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op, title="Deploy the relay")
    assert made["url"].startswith(f"{auth_cfg.origin}/p/")
    assert made["share"]["kind"] == "terminal" and made["share"]["mode"] == "view"
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": _token(made["url"])})
    assert r.status_code == 200
    view = r.json()["share"]
    link = view.pop("link")
    # The link's opaque identity: the viewer binds every snapshot to it (#1444); never the id.
    assert isinstance(link, str) and len(link) == 24 and made["share"]["share_id"] not in link
    assert view == {
        "kind": "terminal",
        "mode": "view",
        "title": "Deploy the relay",
        "expires_at": None,
        "name": None,
        "name_required": False,  # watching needs no name
    }
    cookie = r.headers["set-cookie"]
    assert "agent_sessions_share=" in cookie
    assert "Path=/api/public" in cookie  # one prefix: HTTP and the P3 WebSocket alike
    assert "SameSite=strict" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    me = v.get("/api/public/share")
    assert me.status_code == 200 and me.json()["share"]["title"] == "Deploy the relay"
    assert me.json()["share"]["link"] == link
    assert TERM not in me.text  # the visitor never learns the session key
    # Another link opened in the same browser names a different link.
    other = _create(op, title="Another")
    again = v.post("/api/public/unlock", json={"token": _token(other["url"])})
    assert again.status_code == 200 and again.json()["share"]["link"] != link
    assert v.get("/api/public/share").json()["share"]["link"] == again.json()["share"]["link"]


def test_the_title_a_visitor_sees_is_redacted_and_fails_closed(app, auth_cfg, monkeypatch):
    """The title defaults to the session's, which can fall back to its first message: it goes
    through the secret-value redactor like every other visitor string (#1444 review)."""
    from agent_sessions import template_secrets

    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["s3cret-value-123"])
    op = _operator(app, auth_cfg)
    made = _create(op, title="deploy with s3cret-value-123 now")
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": _token(made["url"])})
    assert r.status_code == 200
    assert (
        "s3cret-value-123" not in r.text
        and r.json()["share"]["title"] == "deploy with [secret] now"
    )
    me = v.get("/api/public/share")
    assert "s3cret-value-123" not in me.text and "[secret]" in me.json()["share"]["title"]

    def boom():
        raise template_secrets.RedactionUnavailable("store unreadable")

    monkeypatch.setattr(template_secrets, "redaction_values", boom)
    shut = v.get("/api/public/share")
    assert shut.status_code == 503 and "s3cret-value-123" not in shut.text


def test_an_unusable_key_makes_every_visitor_read_503_never_500(app, auth_cfg, monkeypatch):
    """The link identity is keyed (#1444): when the key can't be used, unlock, /share and /name
    refuse with 503 (fail closed), never a 500 or an unbound answer."""
    from agent_sessions import share_api, template_secrets

    op = _operator(app, auth_cfg)
    made = _create(op, mode="view")
    v = _visitor(app, auth_cfg)
    ok = v.post("/api/public/unlock", json={"token": _token(made["url"])})
    assert ok.status_code == 200
    csrf = ok.json()["csrf"]

    def boom(_share_id):
        raise template_secrets.SecretKeyUnavailable("key unusable")

    monkeypatch.setattr(share_api, "link_id", boom)
    assert v.post("/api/public/unlock", json={"token": _token(made["url"])}).status_code == 503
    assert v.get("/api/public/share").status_code == 503
    named = v.post("/api/public/name", json={"name": "Alex"}, headers={"X-CSRF-Token": csrf})
    assert named.status_code == 503


def test_the_url_is_returned_once_and_never_listed(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op)
    token = _token(made["url"])
    listed = op.get(f"/api/shares?session={TERM}")
    assert listed.status_code == 200 and listed.json()["available"] is True
    assert [s["share_id"] for s in listed.json()["shares"]] == [made["share"]["share_id"]]
    assert token not in listed.text and "/p/" not in listed.text
    assert listed.headers["cache-control"] == "no-store"


def test_an_api_session_may_be_shared_send_but_a_terminal_may_not(app, auth_cfg):
    op = _operator(app, auth_cfg)
    sent = {"session": API, "mode": "send", "accept_risk": True}
    assert op.post("/api/shares", json=sent).status_code == 201
    r = op.post("/api/shares", json={"session": TERM, "mode": "send"})
    assert r.status_code == 422


def test_a_send_link_is_created_only_after_the_risk_is_accepted(app, auth_cfg):
    from agent_sessions.routes.shares import SEND_RISK

    op = _operator(app, auth_cfg)
    for extra in ({}, {"accept_risk": False}, {"accept_risk": "yes"}, {"accept_risk": 1}):
        r = op.post("/api/shares", json={"session": API, "mode": "send", **extra})
        assert r.status_code == 422, extra
        assert SEND_RISK in r.json()["detail"]
        assert "saved secret values" in r.json()["detail"]
    assert op.get(f"/api/shares?session={API}").json()["shares"] == []
    # Accepted: the link is made, and the response restates the risk.
    r = op.post("/api/shares", json={"session": API, "mode": "send", "accept_risk": True})
    assert r.status_code == 201 and r.json()["risk"] == SEND_RISK
    # A view link needs no acceptance and carries no risk text.
    r = op.post("/api/shares", json={"session": API, "mode": "view"})
    assert r.status_code == 201 and "risk" not in r.json()


@pytest.mark.parametrize(
    "body,status",
    [
        ({}, 422),
        ({"session": "nonsense"}, 404),
        ({"session": TERM, "mode": "edit"}, 422),
        ({"session": TERM, "expires_at": "tomorrow"}, 422),
        ({"session": TERM, "expires_at": 1.0}, 422),  # in the past
        ({"session": TERM, "password": 5}, 422),
    ],
)
def test_create_rejects_bad_input(app, auth_cfg, body, status):
    op = _operator(app, auth_cfg)
    assert op.post("/api/shares", json=body).status_code == status


def test_create_needs_the_operator_and_csrf(app, auth_cfg):
    anon = _visitor(app, auth_cfg)
    assert anon.post("/api/shares", json={"session": TERM}).status_code == 401
    op = _operator(app, auth_cfg)
    del op.headers["X-CSRF-Token"]
    assert op.post("/api/shares", json={"session": TERM}).status_code == 403


def test_password_gate_and_throttle(app, auth_cfg):
    op = _operator(app, auth_cfg)
    token = _token(_create(op, password="opensesame")["url"])
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": token})
    assert r.status_code == 401 and r.json()["password_required"] is True
    assert "set-cookie" not in r.headers
    r = v.post("/api/public/unlock", json={"token": token, "password": "nope"})
    assert r.status_code == 401 and r.json()["tries_left"] == shares.PER_CLIENT_FAILS - 1
    ok = v.post("/api/public/unlock", json={"token": token, "password": "opensesame"})
    assert ok.status_code == 200 and "agent_sessions_share=" in ok.headers["set-cookie"]
    other = _visitor(app, auth_cfg)
    for i in range(shares.PER_CLIENT_FAILS):
        # A forged forwarding header per guess must not buy a fresh budget.
        other.post(
            "/api/public/unlock",
            json={"token": token, "password": "nope"},
            headers={"X-Forwarded-For": f"6.6.6.{i}", "X-Real-IP": f"7.7.7.{i}"},
        )
    paused = other.post(
        "/api/public/unlock",
        json={"token": token, "password": "opensesame"},
        headers={"X-Forwarded-For": "6.6.6.99", "X-Real-IP": "7.7.7.99"},
    )
    assert paused.status_code == 429 and paused.headers["retry-after"] == str(shares.PAUSE_S)


def test_unknown_revoked_and_expired_links_all_answer_the_same_404(app, auth_cfg):
    op = _operator(app, auth_cfg)
    revoked = _create(op)
    op.delete(f"/api/shares/{revoked['share']['share_id']}")
    soon = _create(op, expires_at=time.time() + 60)
    app.state.share_store._clock = lambda: time.time() + 120
    v = _visitor(app, auth_cfg)
    bodies = set()
    for tok in ("A" * 43, _token(revoked["url"]), _token(soon["url"])):
        r = v.post("/api/public/unlock", json={"token": tok})
        assert r.status_code == 404
        bodies.add(r.text)
    assert len(bodies) == 1


def test_a_revoke_stops_an_unlocked_visitor_on_the_next_request(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op)
    v = _visitor(app, auth_cfg)
    assert v.post("/api/public/unlock", json={"token": _token(made["url"])}).status_code == 200
    assert v.get("/api/public/share").status_code == 200
    r = op.delete(f"/api/shares/{made['share']['share_id']}")
    assert r.status_code == 200 and r.json() == {"revoked": True}
    assert v.get("/api/public/share").status_code == 404  # the cookie alone grants nothing


def test_expiry_stops_an_unlocked_visitor(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op, expires_at=time.time() + 60)
    v = _visitor(app, auth_cfg)
    unlocked = v.post("/api/public/unlock", json={"token": _token(made["url"])})
    assert "Max-Age=" in unlocked.headers["set-cookie"]
    max_age = int(re.search(r"Max-Age=(\d+)", unlocked.headers["set-cookie"]).group(1))
    assert max_age <= 60  # the cookie never outlives the link
    app.state.share_store._clock = lambda: time.time() + 120
    assert v.get("/api/public/share").status_code == 404


def test_revoke_all(app, auth_cfg):
    op = _operator(app, auth_cfg)
    _create(op)
    _create(op)
    assert op.post("/api/shares/revoke-all").json() == {"revoked": 2}
    assert op.get("/api/shares").json()["shares"] == []


def test_the_admin_cookie_is_not_a_share_cookie(app, auth_cfg):
    op = _operator(app, auth_cfg)
    _create(op)
    assert op.get("/api/public/share").status_code == 401  # signed in as the operator: nothing


def test_the_share_cookie_is_not_an_admin_cookie(app, auth_cfg):
    op = _operator(app, auth_cfg)
    v = _visitor(app, auth_cfg)
    v.post("/api/public/unlock", json={"token": _token(_create(op)["url"])})
    assert v.get("/api/public/share").status_code == 200
    for path in ("/api/shares", "/api/sessions", "/api/config", "/api/auth-check"):
        assert v.get(path).status_code == 401, path


def test_a_forged_share_cookie_is_refused(app, auth_cfg):
    v = _visitor(app, auth_cfg)
    v.cookies.set("agent_sessions_share", "eyJzaWQiOiJ4In0.forged.sig", path="/api/public")
    assert v.get("/api/public/share").status_code == 401


def test_unlock_needs_a_same_origin_caller(app, auth_cfg):
    op = _operator(app, auth_cfg)
    token = _token(_create(op)["url"])
    v = TestClient(app, base_url="https://testserver")
    r = v.post("/api/public/unlock", json={"token": token}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


def test_require_share_send_gate(app, auth_cfg):
    """The dependency P2's send routes use: a send link with its own CSRF token and Origin."""

    send_dep = app.state.require_share(send=True)

    @app.post("/api/public/_probe_send")
    async def _probe(request: Request, visitor=Depends(send_dep)):  # noqa: B008
        return {"ok": visitor.share.mode, "name": visitor.name}

    op = _operator(app, auth_cfg)
    view = _visitor(app, auth_cfg)
    vcsrf = view.post(
        "/api/public/unlock", json={"token": _token(_create(op, session=API)["url"])}
    ).json()["csrf"]
    # A view-link visitor with a csrf token AND a name is still refused: the mode is the gate
    # (#1414 independent review — the old check passed on "bad csrf" alone).
    view.post("/api/public/name", json={"name": "Vee"}, headers={"X-CSRF-Token": vcsrf})
    refused = view.post("/api/public/_probe_send", headers={"X-CSRF-Token": vcsrf})
    assert refused.status_code == 403 and refused.json()["detail"] == "this link is view only"

    send = _visitor(app, auth_cfg)
    made = _create(op, session=API, mode="send")
    csrf = send.post("/api/public/unlock", json={"token": _token(made["url"])}).json()["csrf"]
    assert send.post("/api/public/_probe_send").status_code == 403  # no CSRF header
    # Nobody types into a session anonymously: a send link needs a name first.
    nameless = send.post("/api/public/_probe_send", headers={"X-CSRF-Token": csrf})
    assert nameless.status_code == 403 and nameless.json()["detail"] == "enter your name first"
    assert send.get("/api/public/share").json()["share"]["name_required"] is True
    named = send.post(
        "/api/public/name", json={"name": "  Alex   Doe "}, headers={"X-CSRF-Token": csrf}
    )
    assert named.status_code == 200 and named.json()["share"]["name"] == "Alex Doe"
    assert named.json()["share"]["name_required"] is False
    ok = send.post("/api/public/_probe_send", headers={"X-CSRF-Token": csrf})  # same csrf
    assert ok.status_code == 200 and ok.json() == {"ok": "send", "name": "Alex Doe"}
    # Named, with the right csrf — a foreign Origin is still refused, by the Origin check itself.
    evil = send.post(
        "/api/public/_probe_send", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.test"}
    )
    assert evil.status_code == 403 and evil.json()["detail"] == "bad origin"
    evil_name = send.post(
        "/api/public/name",
        json={"name": "Mallory"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.test"},
    )
    assert evil_name.status_code == 403 and evil_name.json()["detail"] == "bad origin"


def test_public_prefixes_send_no_referrer_and_no_store(app, auth_cfg):
    v = _visitor(app, auth_cfg)
    for r in (
        v.get("/p/" + "A" * 43),  # the placeholder: no viewer bundle in this test build
        v.post("/api/public/unlock", json={"token": "A" * 43}),
        v.get("/api/public/share"),
    ):
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
    assert v.get("/healthz").headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_without_the_viewer_bundle_the_url_answers_a_plain_placeholder(
    auth_cfg, tmp_path, monkeypatch
):
    from agent_sessions import main

    # A build without `share.html` (an install from before P4's viewer), not whatever happens to
    # be built in this checkout's web/dist.
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA</html>")
    monkeypatch.setattr(main, "_WEB_DIST", dist)
    v = _visitor(create_app(auth_cfg), auth_cfg)
    r = v.get("/p/" + "A" * 43)
    assert r.status_code == 200
    assert "can't show shared sessions yet" in r.text
    assert "<script" not in r.text  # script-free: nothing for the CSP to hash


def test_the_viewer_page_is_served_for_any_token_and_never_the_spa_shell(
    auth_cfg, tmp_path, monkeypatch
):
    from agent_sessions import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA</html>")
    (dist / "share.html").write_text("<html>VIEWER</html>")
    monkeypatch.setattr(main, "_WEB_DIST", dist)
    v = _visitor(create_app(auth_cfg), auth_cfg)
    for tok in ("A" * 43, "whatever"):
        r = v.get(f"/p/{tok}")
        assert r.status_code == 200 and "VIEWER" in r.text
    # The bare prefix is where the viewer moves the address bar after unlocking (P4): a reload
    # there gets the viewer too, never the SPA shell.
    bare = v.get("/p/")
    assert bare.status_code == 200 and "VIEWER" in bare.text
    assert bare.headers["referrer-policy"] == "no-referrer"
    assert bare.headers["cache-control"] == "no-store"


def test_none_mode_turns_public_sharing_off(tmp_path, monkeypatch, fake_jsonl):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    cfg = AuthConfig.from_env()
    app = create_app(cfg)
    # A token minted before the switch must not open anything either.
    _share, token = app.state.share_store.create(session_key=TERM, kind="terminal", mode="view")
    c = TestClient(app, base_url="https://testserver")
    c.headers.update({"Origin": cfg.origin})
    listed = c.get("/api/shares").json()
    assert listed["available"] is False and "AUTH_MODE=none" in listed["reason"]
    assert [x["share_id"] for x in listed["shares"]] == [_share.share_id]  # still revocable
    csrf = c.get("/api/config").json()["csrf"]
    c.headers["X-CSRF-Token"] = csrf
    # TERM exists in `fake_jsonl`, so the 404 is the none-mode refusal, not "unknown session".
    made = c.post("/api/shares", json={"session": TERM})
    assert made.status_code == 404 and made.json()["detail"] == "public sharing unavailable"
    assert c.post("/api/public/unlock", json={"token": token}).status_code == 404
    assert c.get(f"/p/{token}").status_code == 404
    assert c.get("/api/public/share").status_code == 404
    # …and the old link can be ended, so it can't come back with single-user mode.
    assert c.delete(f"/api/shares/{_share.share_id}").json() == {"revoked": True}


# ---- logs ----------------------------------------------------------------------------------


def test_share_tokens_are_masked_in_uvicorn_logs(caplog):
    shares.install_log_redaction()
    tok = "Zk3Jx9" + "q" * 37
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", f"/p/{tok}", "1.1", 200
        )
        logging.getLogger("uvicorn.error").error("Exception in ASGI application at /p/%s", tok)
        logging.getLogger("uvicorn.error").warning(f"Invalid HTTP request /p/{tok}")
    assert tok not in caplog.text
    assert caplog.text.count(shares.REDACTED_PATH) == 3


def test_unlock_logs_neither_the_token_nor_the_password(app, auth_cfg, caplog):
    op = _operator(app, auth_cfg)
    token = _token(_create(op, password="s3cret-pw")["url"])
    v = _visitor(app, auth_cfg)
    with caplog.at_level(logging.DEBUG):
        v.post("/api/public/unlock", json={"token": token, "password": "wrong-pw"})
        v.post("/api/public/unlock", json={"token": token, "password": "s3cret-pw"})
        v.get(f"/p/{token}")
    # httpx is the VISITOR's client library logging its own request line, not the server.
    server = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )
    for secret in (token, "s3cret-pw", "wrong-pw"):
        assert secret not in server


# ---- archive ends a link -------------------------------------------------------------------


def test_archiving_a_session_ends_its_links(auth_cfg, fake_jsonl):
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    made = _create(op)
    v = _visitor(app, auth_cfg)
    v.post("/api/public/unlock", json={"token": _token(made["url"])}).raise_for_status()
    r = op.post(f"/api/sessions/{TERM}/archive")
    assert r.status_code == 200, r.text
    assert app.state.share_store.get(made["share"]["share_id"]).revoked_reason == "archived"
    assert v.get("/api/public/share").status_code == 404


def test_every_archive_path_ends_links_first():
    """Ratchet: every provider archive runs inside `shares.archiving(...)`, so a new archive path
    cannot forget the session's public links (or archive past a failure to end them)."""
    src = Path(shares.__file__).parent
    direct, via_idempotent = [], []
    for py in src.rglob("*.py"):
        if py.parts[-2] in ("engines", "plugins") or py.parent.name == "first_party":
            continue  # provider implementations, not archive paths
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            # Code lines only: a comment naming `shares.archiving(` must not satisfy the ratchet.
            window = "\n".join(
                ln for ln in lines[max(0, i - 3) : i] if not ln.lstrip().startswith("#")
            )
            if re.search(r"\bprov\.archive\(", line):
                if "def _archive_idempotent" in "\n".join(lines[max(0, i - 25) : i]):
                    continue  # covered by its callers, checked below
                direct.append(py.name)
                assert "shares.archiving(" in window, f"{py.name}:{i + 1}"
            if re.search(r"(?<!def )\b_archive_idempotent\(prov", line):
                via_idempotent.append(py.name)
                assert "shares.archiving(" in window, f"{py.name}:{i + 1}"
    assert len(direct) >= 3 and via_idempotent, (direct, via_idempotent)


def test_a_failure_to_end_the_links_aborts_the_archive(auth_cfg, fake_jsonl, monkeypatch):
    """Hermes on #1414: archiving past a revoke failure left a live link to an archived session.
    Fail closed — the session stays where it is and the operator is told."""
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    made = _create(op)

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.state.share_store, "revoke_for_session", broken)
    r = op.post(f"/api/sessions/{TERM}/archive")
    assert r.status_code == 503 and "not archived" in r.json()["detail"]
    live = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    assert (live / "11111111-1111-1111-1111-111111111111.jsonl").exists()  # not moved
    assert app.state.share_store.active(made["share"]["share_id"]) is not None


def test_create_refuses_an_unknown_or_archived_session(auth_cfg, fake_jsonl):
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    unknown = op.post(
        "/api/shares", json={"session": "claude:99999999-9999-4999-8999-999999999999"}
    )
    assert unknown.status_code == 404
    archived = op.post(
        "/api/shares", json={"session": "claude:44444444-4444-4444-4444-444444444444"}
    )
    assert archived.status_code == 409
    assert op.post("/api/shares", json={"session": TERM}).status_code == 201


def test_a_create_racing_an_archive_cannot_mint_a_link(auth_cfg, fake_jsonl):
    """Hermes on #1414: a create landing between the archive's revoke and its move left a live
    link. Create takes the session lock `archiving` holds across both, and re-reads the state."""
    import asyncio

    from agent_sessions import engines

    store = shares.store()
    prov, native = engines.parse_key(TERM)
    outcome: dict = {}
    entered = threading.Event()

    def create() -> None:
        entered.set()
        try:
            shares_routes._create_checked(
                store, key=TERM, password=None, kind="terminal", mode="view"
            )
            outcome["result"] = "created"
        except Exception as e:  # HTTPException 409
            outcome["result"] = getattr(e, "status_code", repr(e))

    async def archive() -> None:
        async with shares.archiving(TERM):
            t = threading.Thread(target=create)
            t.start()
            entered.wait(5)
            time.sleep(0.1)  # the create is now blocked on the session lock
            assert "result" not in outcome
            prov.archive(native)  # the move, still under the lock
        await asyncio.to_thread(t.join, 5)

    asyncio.run(archive())
    assert outcome["result"] == 409
    assert store.list_active(TERM) == []


def test_the_share_routes_module_exports_the_public_prefixes():
    assert shares_routes.PUBLIC_PREFIXES == ("/p/", "/api/public/")


# ---- visitor names -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,want",
    [
        ("Alex", "Alex"),
        ("  Alex   Doe  ", "Alex Doe"),
        ("Zoë", "Zoë"),
        ("x" * shares.MAX_VISITOR_NAME, "x" * shares.MAX_VISITOR_NAME),
        ("x" * (shares.MAX_VISITOR_NAME + 1), None),
        ("", None),
        ("   ", None),
        ("Al\nex", "Al ex"),  # whitespace, newline included, collapses to one space
        ("Al\x00ex", None),  # control
        ("Alex\u202egnirts", None),  # bidi override could disguise the name
        ("Al\u200bex", None),  # zero-width
        ("Al\u2028ex", "Al ex"),  # line separator is whitespace: collapsed, not kept
        (5, None),
        (None, None),
    ],
)
def test_clean_visitor_name(raw, want):
    assert shares.clean_visitor_name(raw) == want


def test_a_name_given_at_unlock_counts(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op, session=API, mode="send")
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": _token(made["url"]), "name": "Sam"})
    assert r.status_code == 200
    assert r.json()["share"]["name"] == "Sam" and r.json()["share"]["name_required"] is False
    assert v.get("/api/public/share").json()["share"]["name"] == "Sam"


def test_a_bad_name_is_refused_and_sets_nothing(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op, session=API, mode="send")
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": _token(made["url"]), "name": "a\u202eb"})
    assert r.status_code == 422 and "set-cookie" not in r.headers
    csrf = v.post("/api/public/unlock", json={"token": _token(made["url"])}).json()["csrf"]
    bad = v.post("/api/public/name", json={"name": "x" * 99}, headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 422
    assert v.get("/api/public/share").json()["share"]["name"] is None


def test_setting_a_name_needs_the_share_csrf_and_a_live_link(app, auth_cfg):
    op = _operator(app, auth_cfg)
    made = _create(op, session=API, mode="send")
    v = _visitor(app, auth_cfg)
    csrf = v.post("/api/public/unlock", json={"token": _token(made["url"])}).json()["csrf"]
    assert v.post("/api/public/name", json={"name": "Sam"}).status_code == 403  # no csrf
    op.delete(f"/api/shares/{made['share']['share_id']}")
    r = v.post("/api/public/name", json={"name": "Sam"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404  # revoked: nothing to name


def test_replacing_the_key_stops_an_already_unlocked_visitor(app, auth_cfg):
    """Hermes on #1414: the URL stopped resolving after a key change, but a cookie issued before
    it still read the share. The row is bound to the key that made it."""
    from agent_sessions import template_secrets

    op = _operator(app, auth_cfg)
    v = _visitor(app, auth_cfg)
    v.post("/api/public/unlock", json={"token": _token(_create(op)["url"])}).raise_for_status()
    assert v.get("/api/public/share").status_code == 200
    Path(os.environ["AGENT_SESSIONS_TEMPLATE_SECRETS_KEY"]).unlink()
    template_secrets._reset_for_tests()
    template_secrets.keyed_digest("x", "y")
    assert v.get("/api/public/share").status_code == 404


def test_a_stale_sidecar_cannot_make_a_gone_or_archived_session_shareable(auth_cfg, fake_jsonl):
    """Hermes round 2 on #1414: the sidecar's `archived: false` was read before the provider, so
    a deleted session, or a provider-archived one with a stale override, read as live."""
    from agent_sessions import metadata

    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    gone = "claude:99999999-9999-4999-8999-999999999999"
    metadata.patch(gone, archived=False)  # a sidecar row for a session with no history
    assert op.post("/api/shares", json={"session": gone}).status_code == 404
    archived = "claude:44444444-4444-4444-4444-444444444444"  # in the provider's archive tree
    metadata.patch(archived, archived=False)  # e.g. a move that landed while its stamp failed
    assert op.post("/api/shares", json={"session": archived}).status_code == 409
    metadata.patch(TERM, archived=True)  # the sidecar alone saying archived also refuses
    assert op.post("/api/shares", json={"session": TERM}).status_code == 409
    assert app.state.share_store.list_active() == []


def test_an_unreadable_sidecar_refuses_rather_than_grants(auth_cfg, fake_jsonl):
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    Path(os.environ["AGENT_SESSIONS_METADATA"]).parent.mkdir(parents=True, exist_ok=True)
    Path(os.environ["AGENT_SESSIONS_METADATA"]).write_text("{not json")
    assert op.post("/api/shares", json={"session": TERM}).status_code == 503
    assert app.state.share_store.list_active() == []


# ---- independent review of #1414 -----------------------------------------------------------


def test_the_redaction_keeps_uvicorns_access_record_formattable():
    """Nulling `record.args` made uvicorn's AccessFormatter (which unpacks five args) raise on every
    viewer hit. Format through uvicorn's own formatter, as production does."""
    import uvicorn.logging

    shares.install_log_redaction()
    tok = "Zk3Jx9" + "q" * 37
    lg = logging.getLogger("uvicorn.access")
    rec = lg.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", f"/p/{tok}", "1.1", 200),
        None,
    )
    for f in lg.filters:
        f.filter(rec)
    fmt = uvicorn.logging.AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s')
    line = fmt.format(rec)  # must not raise
    assert tok not in line and "/p/<redacted>" in line and "200" in line


def test_a_failing_provider_scan_refuses_with_503(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import engines

    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    prov, _native = engines.parse_key(TERM)

    def boom(*_a, **_k):
        raise OSError("store unreadable")

    monkeypatch.setattr(type(prov), "scan", boom)
    assert op.post("/api/shares", json={"session": TERM}).status_code == 503
    assert app.state.share_store.list_active() == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"session": "%s", "expires_at": NaN}',
        '{"session": "%s", "expires_at": Infinity}',
        '{"session": "%s", "expires_at": 1' + "0" * 400 + "}",
    ],
)
def test_non_finite_or_absurd_expiry_is_refused(app, auth_cfg, raw):
    op = _operator(app, auth_cfg)
    r = op.post("/api/shares", content=raw % TERM, headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_setting_a_name_never_stretches_the_cookie(app, auth_cfg, monkeypatch):
    """Each rename re-signed a fresh 12 h cookie, so a visitor never had to present the token
    again. The cookie carries the last real unlock (`iat`) through every re-issue."""
    import types

    op = _operator(app, auth_cfg)
    v = _visitor(app, auth_cfg)
    made = _create(op, session=API, mode="send")
    csrf = v.post("/api/public/unlock", json={"token": _token(made["url"])}).json()["csrf"]
    real = time.time
    later = types.SimpleNamespace(time=lambda: real() + 11 * 3600)
    monkeypatch.setattr(shares_routes, "time", later)
    r = v.post("/api/public/name", json={"name": "Sam"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    max_age = int(re.search(r"Max-Age=(\d+)", r.headers["set-cookie"]).group(1))
    assert max_age <= 3600  # what is left of the original 12 h, not a fresh 12 h
    monkeypatch.setattr(
        shares_routes, "time", types.SimpleNamespace(time=lambda: real() + 12 * 3600 + 5)
    )
    assert v.get("/api/public/share").status_code == 401  # unlock again


def test_a_cancelled_archive_never_strands_the_session_lock():
    import asyncio

    st = shares.store()
    lock = st.session_lock(TERM)
    lock.acquire()  # someone else holds it (a create mid-scan)

    async def go() -> None:
        async def archive() -> None:
            async with shares.archiving(TERM):
                pass

        task = asyncio.create_task(archive())
        await asyncio.sleep(0.1)  # the worker thread is now waiting on the lock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()  # the holder finishes; the waiting thread now acquires…
        for _ in range(50):  # …and the done-callback must hand it straight back
            await asyncio.sleep(0.05)
            if not lock.locked():
                return

    asyncio.run(go())
    assert not lock.locked()


def test_archiving_without_any_link_creates_no_share_database(tmp_path, monkeypatch):
    import asyncio

    db = tmp_path / "never" / "shares.db"
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(db))
    shares.reset_for_tests()

    async def go() -> None:
        async with shares.archiving(TERM):
            pass

    asyncio.run(go())
    assert not db.exists() and not db.parent.exists()


def test_no_usable_key_means_no_live_link(tmp_path, monkeypatch):
    from agent_sessions import template_secrets

    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key=TERM, kind="terminal", mode="view")
    for kid in ("bad", template_secrets.NO_KEY_KID):
        monkeypatch.setattr(template_secrets, "current_kid", lambda kid=kid: kid)
        assert st.active(share.share_id) is None


@pytest.fixture
def _not_owned(monkeypatch):
    """The #631 background-agent probe scans every host process; a sibling xdist worker's fake
    agent can 'own' these fixed ids (see test_project_archive)."""
    from agent_sessions import transcript_owner

    monkeypatch.setattr(transcript_owner, "transcript_is_owned", lambda *a, **k: False)


def test_archive_older_ends_the_links_of_what_it_archives(auth_cfg, fake_jsonl, _not_owned):
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    made = _create(op)
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / "11111111-1111-1111-1111-111111111111.jsonl", (old, old))
    r = op.post("/api/sessions/archive-older", json={"hours": 5})
    assert r.status_code == 200 and r.json()["archived"] >= 1
    assert app.state.share_store.get(made["share"]["share_id"]).revoked_reason == "archived"


def test_project_archive_ends_its_members_links(auth_cfg, fake_jsonl, _not_owned):
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    made = _create(op)
    pid = op.post(
        "/api/projects", json={"name": "Repo A", "folders": ["/home/user/claude/repo/a"]}
    ).json()["id"]
    assert op.post(f"/api/projects/{pid}/archive").status_code == 200
    assert app.state.share_store.get(made["share"]["share_id"]).revoked_reason == "archived"


def test_a_share_store_that_went_missing_refuses_the_archive(tmp_path, monkeypatch):
    """Hermes on #1414: "no database" was read as "no links", so a store moved aside during an
    archive let it complete, and restoring the file revived a link to the archived session."""
    import asyncio

    db = tmp_path / "s" / "shares.db"
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(db))
    shares.reset_for_tests()
    share, _ = shares.store().create(session_key=TERM, kind="terminal", mode="view")
    aside = tmp_path / "aside"
    aside.mkdir()
    moved = [
        f
        for f in db.parent.iterdir()
        if f.name.startswith("shares.db") and f != shares.marker_path(db)
    ]
    for f in moved:
        f.rename(aside / f.name)
    entered = []

    async def go() -> None:
        async with shares.archiving(TERM):
            entered.append(True)

    with pytest.raises(shares.SharesUnavailable):
        asyncio.run(go())
    assert entered == []  # the archive body (the move) never ran
    for f in moved:
        (aside / f.name).rename(db.parent / f.name)
    assert shares.store().active(share.share_id) is not None  # live — and its session not archived


def test_a_missing_store_is_never_recreated_by_any_open(auth_cfg, tmp_path, monkeypatch):
    """Independent review of #1414: only archive checked the marker; an anonymous unlock (or the
    operator's list) recreated an empty shares.db, and after a restart the archive "revoked"
    nothing. Every open refuses now, through restarts, and the routes answer 503."""
    import asyncio

    db = tmp_path / "s" / "shares.db"
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(db))
    shares.reset_for_tests()
    monkeypatch.setattr(shares_routes, "_session_state", lambda prov, native: "live")
    app = create_app(auth_cfg)
    op = _operator(app, auth_cfg)
    made = _create(op)
    token = _token(made["url"])
    aside = tmp_path / "aside"
    aside.mkdir()
    moved = [
        f
        for f in db.parent.iterdir()
        if f.name.startswith("shares.db") and f != shares.marker_path(db)
    ]
    for f in moved:
        f.rename(aside / f.name)
    v = _visitor(app, auth_cfg)
    r = v.post("/api/public/unlock", json={"token": token})  # anonymous
    assert r.status_code == 503 and r.headers["cache-control"] == "no-store"
    assert op.get("/api/shares").status_code == 503
    assert not db.exists()  # nothing recreated it
    shares.reset_for_tests()  # a restart / redeploy

    async def go() -> None:
        async with shares.archiving(TERM):
            raise AssertionError("the archive must not run")

    with pytest.raises(shares.SharesUnavailable):
        asyncio.run(go())
    assert not db.exists()
    for f in moved:
        (aside / f.name).rename(db.parent / f.name)
    assert shares.store().active(made["share"]["share_id"]) is not None


def test_archiving_gives_up_on_a_held_lock_after_its_timeout(monkeypatch):
    import asyncio

    monkeypatch.setattr(shares, "SESSION_LOCK_TIMEOUT_S", 0.2)
    lock = shares.store().session_lock(TERM)
    lock.acquire()
    ran = []

    async def go() -> None:
        async with shares.archiving(TERM):
            ran.append(True)

    try:
        with pytest.raises(shares.SharesUnavailable):
            asyncio.run(go())
    finally:
        lock.release()
    assert ran == []


def test_create_gives_up_on_a_held_lock_after_its_timeout(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(shares, "SESSION_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(shares_routes, "_session_state", lambda prov, native: "live")
    st = shares.store()
    lock = st.session_lock(TERM)
    lock.acquire()
    try:
        with pytest.raises(HTTPException) as e:
            shares_routes._create_checked(st, key=TERM, password=None, kind="terminal", mode="view")
    finally:
        lock.release()
    assert e.value.status_code == 503
    assert st.list_active() == []


def test_a_row_stamped_with_an_unusable_key_id_is_never_live(tmp_path, monkeypatch):
    from agent_sessions import template_secrets

    st = shares.ShareStore(tmp_path / "shares.db")
    for kid in ("bad", template_secrets.NO_KEY_KID):
        share, _ = st.create(session_key=TERM, kind="terminal", mode="view")
        with sqlite3.connect(tmp_path / "shares.db") as con:
            con.execute("UPDATE shares SET key_id = ? WHERE share_id = ?", (kid, share.share_id))
        monkeypatch.setattr(template_secrets, "current_kid", lambda kid=kid: kid)
        assert st.active(share.share_id) is None  # matching ids, but nothing usable to prove


def test_moving_the_whole_store_directory_still_refuses_the_archive(tmp_path, monkeypatch):
    """Hermes on #1414 (d86e05fe): the marker lived beside the database, so moving the directory
    took both and an archive read "never initialized". The second marker lives in the app home."""
    import asyncio

    store_dir = tmp_path / "cfg"
    db = store_dir / "shares.db"
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(db))
    shares.reset_for_tests()
    share, _ = shares.store().create(session_key=TERM, kind="terminal", mode="view")
    assert shares.home_marker_path(db).exists()
    store_dir.rename(tmp_path / "cfg-aside")  # the whole directory, markers and all
    shares.reset_for_tests()  # and a restart

    async def go() -> None:
        async with shares.archiving(TERM):
            raise AssertionError("the archive must not run")

    with pytest.raises(shares.SharesUnavailable):
        asyncio.run(go())
    with pytest.raises(shares.SharesUnavailable):
        shares.store().list_active()
    assert not store_dir.exists()  # nothing recreated it
    (tmp_path / "cfg-aside").rename(store_dir)
    assert shares.store().active(share.share_id) is not None  # live because nothing was archived


def test_a_new_store_path_is_not_blocked_by_an_old_stores_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(tmp_path / "old" / "shares.db"))
    shares.reset_for_tests()
    shares.store().create(session_key=TERM, kind="terminal", mode="view")
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(tmp_path / "new" / "shares.db"))
    shares.reset_for_tests()
    assert shares.store().list_active() == []  # a fresh store, not "missing"
