"""Optional TOTP 2FA — endpoints + login pre-auth flow (issue #116, Phases 2-3).

Acceptance coverage: confirmed-code-only enablement, full session NOT minted until
/login/totp, recovery one-time use at login, invalid-code rejection, brute-force lockout,
fresh-proof for disable/regenerate, /api/config exposes only the on/off bit (never the
secret/codes), forced-password-change precedence, and AUTH_MODE=none disabling 2FA.
"""

from __future__ import annotations

import json

import pyotp
import pytest
from fastapi.testclient import TestClient

from agent_sessions import twofactor
from agent_sessions.auth import AuthConfig
from agent_sessions.main import create_app

# Renders login_totp.html from the installed package — see pyproject's deploy_shape marker.
pytestmark = pytest.mark.deploy_shape


def _client(cfg) -> TestClient:
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg, password="hunter2"):
    return c.post(
        "/login",
        data={"username": "marcus", "password": password},
        headers={"Origin": cfg.origin},
        follow_redirects=False,
    )


def _csrf(c):
    return c.get("/api/config").json()["csrf"]


@pytest.fixture
def twofa_env(auth_cfg, tmp_path, monkeypatch):
    """auth_cfg + an isolated 2FA secrets file + env file (never the real ones)."""
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    envf = tmp_path / "env"
    envf.write_text("AGENT_SESSIONS_USERNAME=marcus\n")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    return auth_cfg


def _enable_2fa(c, cfg):
    """Log in, enroll, confirm with a live TOTP → 2FA enabled. Returns (enroll_info, hdr)."""
    _login(c, cfg)
    hdr = {"X-CSRF-Token": _csrf(c), "Origin": cfg.origin}
    info = c.post("/api/2fa/enroll", headers=hdr).json()
    code = pyotp.TOTP(info["secret"]).now()
    assert c.post("/api/2fa/confirm", json={"code": code}, headers=hdr).status_code == 204
    return info, hdr


def test_config_flag_and_no_secret_leak(twofa_env):
    c = _client(twofa_env)
    _login(c, twofa_env)
    assert c.get("/api/config").json()["two_factor_enabled"] is False
    info, _ = _enable_2fa(c, twofa_env)
    cfgj = c.get("/api/config").json()
    assert cfgj["two_factor_enabled"] is True
    # /api/config must never carry the secret or recovery codes.
    assert "secret" not in cfgj
    assert "recovery_codes" not in cfgj
    assert info["secret"] not in c.get("/api/config").text


def test_enroll_requires_session_and_csrf(twofa_env):
    c = _client(twofa_env)
    # No session at all → rejected.
    assert c.post("/api/2fa/enroll", headers={"Origin": twofa_env.origin}).status_code == 401
    # Logged in but no CSRF token → 403.
    _login(c, twofa_env)
    assert c.post("/api/2fa/enroll", headers={"Origin": twofa_env.origin}).status_code == 403


def test_confirm_requires_valid_code(twofa_env):
    c = _client(twofa_env)
    _login(c, twofa_env)
    hdr = {"X-CSRF-Token": _csrf(c), "Origin": twofa_env.origin}
    c.post("/api/2fa/enroll", headers=hdr)
    # Wrong code → 400, still disabled.
    assert c.post("/api/2fa/confirm", json={"code": "000000"}, headers=hdr).status_code == 400
    assert c.get("/api/config").json()["two_factor_enabled"] is False


def test_login_requires_second_factor(twofa_env):
    setup = _client(twofa_env)
    info, _ = _enable_2fa(setup, twofa_env)

    c = _client(twofa_env)  # fresh — no cookies
    r = _login(c, twofa_env)  # correct password
    # Password alone does NOT mint a session: we get the TOTP step page (200), not a 303.
    assert r.status_code == 200
    assert "agent_sessions_preauth" in r.cookies
    assert "agent_sessions" not in r.cookies  # no full session cookie yet
    # The pre-auth cookie cannot pass require_session / reach /api/config.
    assert c.get("/api/config", follow_redirects=False).status_code == 401
    # Complete with a recovery code → full session minted.
    r2 = c.post(
        "/login/totp",
        data={"code": info["recovery_codes"][0], "next": "/"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert c.get("/api/config").json()["two_factor_enabled"] is True


def test_totp_login_happy_path_with_clock(twofa_env, monkeypatch):
    clock = {"now": 1_700_000_000}
    monkeypatch.setattr(twofactor.time, "time", lambda: clock["now"])
    setup = _client(twofa_env)
    _login(setup, twofa_env)
    hdr = {"X-CSRF-Token": _csrf(setup), "Origin": twofa_env.origin}
    info = setup.post("/api/2fa/enroll", headers=hdr).json()
    code = pyotp.TOTP(info["secret"]).at(clock["now"])
    assert setup.post("/api/2fa/confirm", json={"code": code}, headers=hdr).status_code == 204

    clock["now"] += twofactor.STEP_SECONDS  # next step — the confirm code can't be reused
    c = _client(twofa_env)
    assert _login(c, twofa_env).status_code == 200
    login_code = pyotp.TOTP(info["secret"]).at(clock["now"])
    r = c.post(
        "/login/totp",
        data={"code": login_code, "next": "/"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_login_totp_invalid_code_keeps_unauthenticated(twofa_env):
    setup = _client(twofa_env)
    _enable_2fa(setup, twofa_env)
    c = _client(twofa_env)
    _login(c, twofa_env)
    r = c.post(
        "/login/totp",
        data={"code": "000000", "next": "/"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert c.get("/api/config", follow_redirects=False).status_code == 401


def test_recovery_code_one_time_at_login(twofa_env):
    setup = _client(twofa_env)
    info, _ = _enable_2fa(setup, twofa_env)
    rc = info["recovery_codes"][0]
    hdr = {"Origin": twofa_env.origin}

    c = _client(twofa_env)
    _login(c, twofa_env)
    assert (
        c.post("/login/totp", data={"code": rc}, headers=hdr, follow_redirects=False).status_code
        == 303
    )
    # Same recovery code on a fresh login attempt → rejected (consumed).
    c2 = _client(twofa_env)
    _login(c2, twofa_env)
    assert (
        c2.post("/login/totp", data={"code": rc}, headers=hdr, follow_redirects=False).status_code
        == 401
    )


def test_login_totp_brute_force_lockout(twofa_env):
    setup = _client(twofa_env)
    _enable_2fa(setup, twofa_env)
    c = _client(twofa_env)
    _login(c, twofa_env)
    hdr = {"Origin": twofa_env.origin}
    for _ in range(10):
        c.post("/login/totp", data={"code": "000000"}, headers=hdr, follow_redirects=False)
    r = c.post("/login/totp", data={"code": "000000"}, headers=hdr, follow_redirects=False)
    assert r.status_code == 429


def test_login_totp_without_preauth_redirects(twofa_env):
    setup = _client(twofa_env)
    _enable_2fa(setup, twofa_env)
    c = _client(twofa_env)  # no pre-auth cookie
    r = c.post(
        "/login/totp",
        data={"code": "000000"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_disable_requires_fresh_proof(twofa_env):
    c = _client(twofa_env)
    _info, hdr = _enable_2fa(c, twofa_env)
    # No proof / wrong password → 403, still enabled.
    assert c.post("/api/2fa/disable", json={}, headers=hdr).status_code == 403
    assert c.post("/api/2fa/disable", json={"password": "nope"}, headers=hdr).status_code == 403
    assert c.get("/api/config").json()["two_factor_enabled"] is True
    # Correct password → 204, disabled.
    assert c.post("/api/2fa/disable", json={"password": "hunter2"}, headers=hdr).status_code == 204
    assert c.get("/api/config").json()["two_factor_enabled"] is False


def test_disable_accepts_current_totp_as_proof(twofa_env):
    c = _client(twofa_env)
    info, hdr = _enable_2fa(c, twofa_env)
    code = pyotp.TOTP(info["secret"]).now()
    assert c.post("/api/2fa/disable", json={"code": code}, headers=hdr).status_code == 204
    assert c.get("/api/config").json()["two_factor_enabled"] is False


def test_regenerate_recovery_requires_proof_and_replaces(twofa_env):
    c = _client(twofa_env)
    info, hdr = _enable_2fa(c, twofa_env)
    # No proof → 403.
    assert c.post("/api/2fa/recovery-codes", json={}, headers=hdr).status_code == 403
    r = c.post("/api/2fa/recovery-codes", json={"password": "hunter2"}, headers=hdr)
    assert r.status_code == 200
    new = r.json()["recovery_codes"]
    assert len(new) == twofactor.RECOVERY_COUNT
    assert set(new).isdisjoint(set(info["recovery_codes"]))


def test_reenroll_while_enabled_requires_fresh_proof(twofa_env):
    c = _client(twofa_env)
    _info, hdr = _enable_2fa(c, twofa_env)
    # 2FA already on: a bare enroll (stale-session attack) is refused without fresh proof.
    assert c.post("/api/2fa/enroll", headers=hdr).status_code == 403
    assert c.post("/api/2fa/enroll", json={"password": "nope"}, headers=hdr).status_code == 403
    # With the current password it's allowed (intentional re-enrollment).
    assert c.post("/api/2fa/enroll", json={"password": "hunter2"}, headers=hdr).status_code == 200


def test_corrupt_store_blocks_password_only_login(twofa_env):
    # Tamper with / corrupt the 2FA file → login must still demand a second factor (fail
    # closed), and /api/config reports 2FA on.
    from agent_sessions import twofactor

    twofactor.default_path().write_text("not valid json {{{")
    c = _client(twofa_env)
    r = _login(c, twofa_env)  # correct password
    assert r.status_code == 200  # TOTP step page, NOT a 303 full-session redirect
    assert "agent_sessions_preauth" in r.cookies
    assert "agent_sessions" not in r.cookies


def test_force_password_change_precedes_2fa(auth_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    envf = tmp_path / "env"
    envf.write_text("")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.setenv("AGENT_SESSIONS_FORCE_PASSWORD_CHANGE", "1")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # The force-change gate blocks enrollment (and every other /api/*) until the password
    # is changed → 2FA can't be set up first.
    hdr = {"X-CSRF-Token": _csrf(c), "Origin": auth_cfg.origin}
    assert c.post("/api/2fa/enroll", headers=hdr).status_code == 403


@pytest.fixture
def none_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    monkeypatch.delenv("AGENT_SESSIONS_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PASSWORD_HASH", raising=False)
    return AuthConfig.from_env()


def test_none_mode_2fa_unavailable(none_cfg):
    c = _client(none_cfg)
    cfgj = c.get("/api/config").json()
    assert cfgj["two_factor_enabled"] is False
    hdr = {"X-CSRF-Token": cfgj["csrf"], "Origin": none_cfg.origin}
    # Enrollment / management are N/A in `none` mode → 404.
    assert c.post("/api/2fa/enroll", headers=hdr).status_code == 404
    assert c.post("/api/2fa/disable", json={}, headers=hdr).status_code == 404


def _enroll_at(clock, twofa_env, monkeypatch):
    """Enable 2FA on a frozen clock; return the enroll info."""
    monkeypatch.setattr(twofactor.time, "time", lambda: clock["now"])
    setup = _client(twofa_env)
    _login(setup, twofa_env)
    hdr = {"X-CSRF-Token": _csrf(setup), "Origin": twofa_env.origin}
    info = setup.post("/api/2fa/enroll", headers=hdr).json()
    confirm = pyotp.TOTP(info["secret"]).at(clock["now"])
    assert setup.post("/api/2fa/confirm", json={"code": confirm}, headers=hdr).status_code == 204
    return info


def test_replayed_code_reports_already_used_not_invalid(twofa_env, monkeypatch):
    """#814 at the route: the second tab's *correct* code gets a message it can act on."""
    clock = {"now": 1_700_000_000}
    info = _enroll_at(clock, twofa_env, monkeypatch)
    clock["now"] += twofactor.STEP_SECONDS  # next step — the confirm code can't be reused
    shared = pyotp.TOTP(info["secret"]).at(clock["now"])
    post = dict(headers={"Origin": twofa_env.origin}, follow_redirects=False)

    tab1 = _client(twofa_env)
    _login(tab1, twofa_env)
    assert tab1.post("/login/totp", data={"code": shared, "next": "/"}, **post).status_code == 303
    cursor = json.loads(twofactor.default_path().read_text())["last_step"]

    # The replay must never reach the PBKDF2 recovery loop — a "recovery count unchanged"
    # assertion alone would pass even if it did, since a 6-digit code can't match anyway.
    calls: list[str] = []
    monkeypatch.setattr(
        twofactor, "verify_recovery_for_login", lambda c, *a, **k: calls.append(c) or False
    )

    tab2 = _client(twofa_env)  # same 30s code, one tick later
    _login(tab2, twofa_env)
    r = tab2.post("/login/totp", data={"code": shared, "next": "/"}, **post)
    assert r.status_code == 401
    assert "already used" in r.text
    assert "invalid code" not in r.text
    assert calls == []  # replay short-circuits before recovery verification
    # Fails closed: no session cookie minted, no API access, cursor untouched.
    assert "agent_sessions" not in tab2.cookies
    assert tab2.get("/api/config", follow_redirects=False).status_code == 401
    assert json.loads(twofactor.default_path().read_text())["last_step"] == cursor
    assert twofactor.recovery_remaining() == twofactor.RECOVERY_COUNT


def test_replays_count_toward_the_same_lockout(twofa_env, monkeypatch):
    """A replay is still a failed attempt — it must not be a free retry channel."""
    clock = {"now": 1_700_000_000}
    info = _enroll_at(clock, twofa_env, monkeypatch)
    clock["now"] += twofactor.STEP_SECONDS
    shared = pyotp.TOTP(info["secret"]).at(clock["now"])
    post = dict(headers={"Origin": twofa_env.origin}, follow_redirects=False)

    winner = _client(twofa_env)
    _login(winner, twofa_env)
    assert winner.post("/login/totp", data={"code": shared, "next": "/"}, **post).status_code == 303

    loser = _client(twofa_env)
    _login(loser, twofa_env)
    for _ in range(10):  # _TOTP_MAX_FAILS
        r = loser.post("/login/totp", data={"code": shared, "next": "/"}, **post)
        assert r.status_code == 401 and "already used" in r.text
    assert loser.post("/login/totp", data={"code": shared, "next": "/"}, **post).status_code == 429


def test_wrong_code_still_reports_invalid(twofa_env):
    """The new message must not swallow the genuine-wrong-code case."""
    setup = _client(twofa_env)
    _enable_2fa(setup, twofa_env)
    c = _client(twofa_env)
    _login(c, twofa_env)
    r = c.post(
        "/login/totp",
        data={"code": "000000", "next": "/"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "invalid code" in r.text
    assert "already used" not in r.text
