"""`AUTH_MODE=none` contract tests (#13 / #32 Phase 3).

In `none` mode there is no login at all: the app auto-establishes the admin
session so the SPA + CSRF + Origin still work, but the user is never prompted for
credentials. These tests assert:
  - config + auth-check work without logging in (no cookie),
  - CSRF + Origin are STILL enforced on state-changing routes (not bypassed),
  - the SPA shell is served at `/` (no redirect to /login),
  - `/login` redirects to `/`.

The `single-user` default is exercised unchanged by test_auth.py / test_api.py /
test_password.py via the existing `auth_cfg` fixture — those prove the default
path is behavior-equivalent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import main
from agent_sessions.auth import AuthConfig, decode_session_token
from agent_sessions.main import create_app


@pytest.fixture
def none_cfg(monkeypatch) -> AuthConfig:
    # No username / password hash in the env — `none` mode must not require them.
    monkeypatch.delenv("AGENT_SESSIONS_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    return AuthConfig.from_env()


def _client(cfg):
    # https:// so the secure session cookie is carried across requests.
    return TestClient(create_app(cfg), base_url="https://testserver")


def _fake_dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root>spa-shell</div>")
    return d


def test_none_mode_does_not_require_credentials(none_cfg):
    # from_env() built a config without AGENT_SESSIONS_USERNAME/PASSWORD_HASH set.
    assert none_cfg.auth_mode == "none"


# (a) /api/config 200 without logging in.
def test_config_without_login(none_cfg):
    c = _client(none_cfg)
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["csrf"]  # CSRF token still issued so mutations work
    assert body["auth_mode"] == "none"
    assert body["must_change_password"] is False


# (b) /api/auth-check → 204 without a cookie.
def test_auth_check_204_without_cookie(none_cfg):
    # Fresh client, no prior request → no cookie jar entry.
    c = TestClient(create_app(none_cfg), base_url="https://testserver")
    r = c.get("/api/auth-check")
    assert r.status_code == 204


# (c) CSRF + Origin still enforced on a mutation, but succeeds with them and no login.
def test_prefs_requires_csrf_even_without_login(none_cfg):
    c = _client(none_cfg)
    # Missing X-CSRF-Token → 403 (CSRF NOT bypassed in none mode).
    r = c.post("/api/prefs", json={"theme": "dark"}, headers={"Origin": none_cfg.origin})
    assert r.status_code == 403


def test_prefs_rejects_wrong_origin_without_login(none_cfg):
    c = _client(none_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    # Right CSRF but wrong Origin → 403 (Origin NOT bypassed in none mode).
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_prefs_succeeds_with_csrf_and_origin_no_login(none_cfg):
    c = _client(none_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": none_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json()["theme"] == "dark"


# (d) GET / serves the SPA shell without a redirect to /login.
def test_index_serves_spa_without_login(none_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_WEB_DIST", _fake_dist(tmp_path))
    c = _client(none_cfg)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "spa-shell" in r.text


# (e) /login redirects to /.
def test_login_redirects_to_root(none_cfg):
    c = _client(none_cfg)
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ---- one stable none-mode session per app instance (#673) ---------------------
#
# The SPA's boot wave is parallel and cookie-less; minting per request gave every
# response a DIFFERENT cookie (each with its own embedded csrf). The browser / Home
# Free tunnel jar keeps whichever Set-Cookie landed last while the SPA caches
# /api/config's csrf → every mutation 403 "bad csrf" through the relay.


def test_two_cookieless_clients_share_one_cookie_and_csrf_works_cross_response(none_cfg):
    # The red repro from the issue, turned green: csrf from ONE cookie-less response,
    # cookie from ANOTHER (the jar keeps the last Set-Cookie) → the mutation succeeds
    # because both responses now carry the SAME session.
    app = create_app(none_cfg)
    c1 = TestClient(app, base_url="https://testserver")
    c2 = TestClient(app, base_url="https://testserver")
    r1 = c1.get("/api/config")
    r2 = c2.get("/api/config")
    ck1 = r1.headers.get("set-cookie", "").split(";", 1)[0]
    ck2 = r2.headers.get("set-cookie", "").split(";", 1)[0]
    assert ck1 and ck1 == ck2  # one stable cookie, not a fresh mint per request
    csrf = r1.json()["csrf"]
    r = c2.post(  # c2's jar cookie + c1's csrf — the exact cross-response pair that 403'd
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": none_cfg.origin},
    )
    assert r.status_code == 200


def test_parallel_cookieless_requests_mint_exactly_once(none_cfg, monkeypatch):
    # Deterministic contended window (no sleeps): an outer ASGI gate holds BOTH requests
    # until both are in flight, so both enter the middleware cookie-less inside the initial
    # cache-miss window. The lock + double-check must collapse them to ONE mint and one
    # identical Set-Cookie.
    import asyncio

    import httpx

    from agent_sessions import auth as auth_mod

    mints = 0
    real_issue = auth_mod.issue_session

    def counting_issue(cfg, response, **kw):
        nonlocal mints
        mints += 1
        return real_issue(cfg, response, **kw)

    monkeypatch.setattr(main, "issue_session", counting_issue)
    app = create_app(none_cfg)

    class Barrier:
        """Hold /api/* requests until `n` of them have arrived, then release together."""

        def __init__(self, inner, n):
            self.inner = inner
            self.n = n
            self.arrived = 0
            self.evt: asyncio.Event | None = None

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/api/"):
                if self.evt is None:
                    self.evt = asyncio.Event()
                self.arrived += 1
                if self.arrived >= self.n:
                    self.evt.set()
                await self.evt.wait()
            await self.inner(scope, receive, send)

    async def run() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=Barrier(app, 2))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            r1, r2 = await asyncio.gather(
                c.get("/api/config", headers={"Cookie": ""}),
                c.get("/api/auth-check", headers={"Cookie": ""}),
            )
            return (
                r1.headers.get("set-cookie", ""),
                r2.headers.get("set-cookie", ""),
            )

    sc1, sc2 = asyncio.run(run())
    assert mints == 1  # the contended first wave minted exactly once
    assert sc1 and sc1 == sc2  # both responses converge on the identical cookie


def test_invalid_cached_token_rotates_once(none_cfg):
    # A garbage/expired cached token (TTL passed, secret rotated, hand-edited state) is
    # re-validated on the cookie-less path and re-minted — once, then reused.
    app = create_app(none_cfg)
    app.state.none_session["token"] = "garbage-not-a-signed-token"
    app.state.none_session["set_cookie"] = "agent_sessions=garbage-not-a-signed-token; Path=/"
    c1 = TestClient(app, base_url="https://testserver")
    r1 = c1.get("/api/config")
    assert r1.status_code == 200
    fresh = r1.headers.get("set-cookie", "")
    assert "garbage-not-a-signed-token" not in fresh  # rotated, not replayed
    # and the rotated token is now the stable one for the next cookie-less client
    c2 = TestClient(app, base_url="https://testserver")
    r2 = c2.get("/api/config")
    assert r2.headers.get("set-cookie", "") == fresh


def test_rotation_preserves_csrf_so_cached_client_token_survives(none_cfg):
    # Hermes on PR #674: the SPA fetches /api/config ONCE and caches its csrf. A TTL
    # rotation that re-rolled the csrf would 403 every mutation until a reload. Rotation
    # must re-sign the cookie but PIN the csrf: old cached csrf + rotated cookie → 200.
    app = create_app(none_cfg)
    c = TestClient(app, base_url="https://testserver")
    csrf = c.get("/api/config").json()["csrf"]  # the SPA's one-time cached token
    # Invalidate the cached token (what TTL expiry / secret rotation looks like to the
    # validator), then let a background GET from a fresh cookie-less client rotate it.
    app.state.none_session["token"] = "expired-or-garbage"
    app.state.none_session["set_cookie"] = "agent_sessions=expired-or-garbage; Path=/"
    c2 = TestClient(app, base_url="https://testserver")
    r_bg = c2.get("/api/auth-check")  # triggers the rotation
    assert r_bg.status_code == 204
    rotated = app.state.none_session["set_cookie"]
    # The garbage was replaced by a freshly-signed, VALID session. Do NOT assert the cookie
    # STRING changed: rotation pins {uid, csrf}, so a re-sign inside the same itsdangerous
    # 1-second timestamp is byte-identical — asserting inequality was a clock race (#706).
    # Assert the properties rotation actually promises instead.
    assert rotated and "expired-or-garbage" not in rotated
    assert decode_session_token(none_cfg, app.state.none_session["token"]) is not None
    # …and the csrf was PINNED across the re-mint (the #674 invariant this test guards).
    assert app.state.none_session["csrf"] == csrf
    # The mutation still carries the ORIGINAL cached csrf, now with the rotated cookie.
    r = c2.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": none_cfg.origin},
    )
    assert r.status_code == 200


def test_mint_serialized_even_with_suspension_inside_the_window(none_cfg, monkeypatch):
    # Hermes on PR #674 (non-blocking): on a single event loop the mint window has no
    # suspension point, so the barrier test alone can't distinguish lock+double-check
    # from no lock. Force a real suspension inside the window via an instrumented lock
    # whose acquire always yields — both requests then pass the outer cache-miss check
    # before either mints, and only the double-check prevents a second mint.
    import asyncio

    import httpx

    from agent_sessions import auth as auth_mod

    mints = 0
    real_issue = auth_mod.issue_session

    def counting_issue(cfg, response, **kw):
        nonlocal mints
        mints += 1
        return real_issue(cfg, response, **kw)

    monkeypatch.setattr(main, "issue_session", counting_issue)
    app = create_app(none_cfg)

    class YieldingLock:
        """asyncio.Lock whose acquire ALWAYS suspends first — forces the contended
        window that the fast-path (uncontended) acquire never exposes in-loop."""

        def __init__(self) -> None:
            self._lock = asyncio.Lock()

        async def __aenter__(self):
            await asyncio.sleep(0)  # real suspension point inside the mint window
            await self._lock.acquire()
            return self

        async def __aexit__(self, *exc):
            self._lock.release()

    app.state.none_mint_lock = YieldingLock()

    async def run() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            r1, r2 = await asyncio.gather(
                c.get("/api/config", headers={"Cookie": ""}),
                c.get("/api/auth-check", headers={"Cookie": ""}),
            )
            return r1.headers.get("set-cookie", ""), r2.headers.get("set-cookie", "")

    sc1, sc2 = asyncio.run(run())
    assert mints == 1  # double-check under forced suspension: exactly one mint
    assert sc1 and sc1 == sc2
