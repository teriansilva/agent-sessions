"""The pages outside the SPA in NIGHTJAR (#1596, epic #1531 Phase 12), proven on the SERVED pages.

The restyle may change only how these pages look. So every page is rendered through the real app
and held to its contract: the stylesheets it loads (served from the installed package), every form
field name and action, the hidden ``next``, the hooks ``/static/link.js`` reads, and the error line.
These forms carry NO CSRF token and none is added: login and TOTP are guarded by the same-origin
check (TOTP also by the pre-auth cookie); change-password and device-link approve/deny by a session
plus the same-origin check. Those guards are asserted here too, negatives included.

Marked ``deploy_shape``: the wheel pass renders these from site-packages, so a stylesheet or font
missing from the package data fails there, not in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from agent_sessions import main
from agent_sessions.main import create_app

pytestmark = pytest.mark.deploy_shape

STATIC = Path(main.__file__).parent / "static" / "nightjar"
SHEETS = ["fonts.css", "tokens.css", "motion.css", "nightjar.css", "pages.css"]


def _client(cfg) -> TestClient:
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg, password="hunter2", next_="/"):
    return c.post(
        "/login",
        data={"username": "marcus", "password": password, "next": next_},
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


def _assert_nightjar_page(html: str) -> None:
    """The shared shell every page outside the SPA wears: the five sheets in order, the atmosphere,
    and none of the old HUD (inline palette, corner brackets, the classification footer)."""
    links = re.findall(r'<link rel="stylesheet" href="/static/nightjar/([a-z.]+)">', html)
    assert links == SHEETS
    assert 'class="nj-page"' in html
    assert 'class="nj-atmo"' in html
    assert "<style" not in html  # one palette, from tokens.css — never restated
    assert "Unclassified" not in html and "classbar" not in html
    assert 'class="cnr' not in html
    assert '<meta name="color-scheme" content="dark light">' in html


# ---- the stylesheets ship and are served before sign-in ---------------------------------------


def test_every_page_sheet_and_font_is_served_without_a_session(auth_cfg):
    c = _client(auth_cfg)
    for sheet in SHEETS:
        r = c.get(f"/static/nightjar/{sheet}")
        assert r.status_code == 200, sheet
        assert r.headers["content-type"].startswith("text/css"), sheet
    fonts = sorted(p.name for p in (STATIC / "fonts").glob("*.woff2"))
    assert fonts, "the fonts ship with the package"
    for font in fonts:
        r = c.get(f"/static/nightjar/fonts/{font}")
        assert r.status_code == 200, font
    # Every font the sheet names is one that ships.
    named = set(re.findall(r'url\("fonts/([^"]+)"\)', c.get("/static/nightjar/fonts.css").text))
    assert named and named <= set(fonts)


def test_the_page_tokens_follow_the_device_theme(auth_cfg):
    css = _client(auth_cfg).get("/static/nightjar/tokens.css").text
    # Dark is the bare :root default; the light set also applies when the DEVICE prefers light and
    # nothing stored a theme (these pages have no stored choice).
    assert "@media (prefers-color-scheme: light)" in css
    assert ":root:not([data-theme])" in css


# ---- login ------------------------------------------------------------------------------------


def test_login_page_contract(auth_cfg):
    r = _client(auth_cfg).get("/login?next=/s/claude/abc")
    assert r.status_code == 200
    html = r.text
    _assert_nightjar_page(html)
    assert '<form class="nj-frame page-card page-form" method="post" action="/login"' in html
    assert 'name="username"' in html and 'autocomplete="username"' in html
    assert 'name="password"' in html and 'autocomplete="current-password"' in html
    assert '<input type="hidden" name="next" value="/s/claude/abc">' in html
    assert ">Sign in</button>" in html
    # The QR panel's hooks, exactly as /static/link.js reads them; hidden until it has a challenge.
    assert '<div id="qr-extra" class="page-stack" hidden>' in html
    assert 'id="qr-signin"' in html and 'data-next="/s/claude/abc"' in html
    assert 'id="qr-img"' in html and 'id="qr-status"' in html
    assert '<script src="/static/link.js" defer></script>' in html
    assert "page-error" not in html  # no error line until there is an error


def test_login_next_is_open_redirect_guarded_on_the_page(auth_cfg):
    html = _client(auth_cfg).get("/login?next=https://evil.test/x").text
    assert 'name="next" value="/"' in html


def test_login_error_line(auth_cfg):
    c = _client(auth_cfg)
    r = _login(c, auth_cfg, password="wrong")
    assert r.status_code == 401
    _assert_nightjar_page(r.text)
    assert '<p class="page-error" role="alert">' in r.text
    assert "invalid credentials" in r.text


def test_login_is_refused_cross_origin(auth_cfg):
    r = _client(auth_cfg).post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": "https://evil.test"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# ---- TOTP and recovery ------------------------------------------------------------------------


def _enable_2fa(c, cfg):
    _login(c, cfg)
    hdr = {"X-CSRF-Token": _csrf(c), "Origin": cfg.origin}
    info = c.post("/api/2fa/enroll", headers=hdr).json()
    code = pyotp.TOTP(info["secret"]).now()
    assert c.post("/api/2fa/confirm", json={"code": code}, headers=hdr).status_code == 204
    return info


def test_totp_step_contract_and_error_line(twofa_env):
    _enable_2fa(_client(twofa_env), twofa_env)
    c = _client(twofa_env)
    r = _login(c, twofa_env, next_="/missions")
    assert r.status_code == 200
    html = r.text
    _assert_nightjar_page(html)
    assert 'method="post" action="/login/totp"' in html
    assert 'name="code"' in html and 'autocomplete="one-time-code"' in html
    assert '<input type="hidden" name="next" value="/missions">' in html
    assert "or a recovery code" in html
    bad = c.post(
        "/login/totp",
        data={"code": "000000", "next": "/missions"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    assert '<p class="page-error" role="alert">' in bad.text and "invalid code" in bad.text


def test_totp_without_a_preauth_goes_back_to_login(twofa_env):
    r = _client(twofa_env).post(
        "/login/totp",
        data={"code": "123456", "next": "/"},
        headers={"Origin": twofa_env.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_totp_is_refused_cross_origin(twofa_env):
    _enable_2fa(_client(twofa_env), twofa_env)
    c = _client(twofa_env)
    _login(c, twofa_env)
    r = c.post(
        "/login/totp",
        data={"code": "123456"},
        headers={"Origin": "https://evil.test"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# ---- change password --------------------------------------------------------------------------


def test_change_password_contract_and_error_line(auth_cfg):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/change-password")
    assert r.status_code == 200
    html = r.text
    _assert_nightjar_page(html)
    assert 'method="post" action="/change-password"' in html
    fields = (("current", "current-password"), ("new", "new-password"), ("confirm", "new-password"))
    for name, ac in fields:
        assert f'name="{name}" type="password"' in html and f'autocomplete="{ac}"' in html
    assert ">Set password</button>" in html
    bad = c.post(
        "/change-password",
        data={"current": "hunter2", "new": "a-long-new-password", "confirm": "different-one-here"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert '<p class="page-error" role="alert">' in bad.text
    assert "passwords do not match" in bad.text


def test_change_password_needs_a_session(auth_cfg):
    c = _client(auth_cfg)
    r = c.get("/change-password", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.post(
        "/change-password",
        data={"current": "x", "new": "y" * 12, "confirm": "y" * 12},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_change_password_is_refused_cross_origin(auth_cfg):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/change-password",
        data={"current": "hunter2", "new": "y" * 12, "confirm": "y" * 12},
        headers={"Origin": "https://evil.test"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# ---- device link: the four outcomes ----------------------------------------------------------


def _pair(cfg):
    """One app, two clients: the requester (no session) and a signed-in phone. The challenge store
    lives in the app, so both must talk to the same one."""
    app = create_app(cfg)
    requester = TestClient(app, base_url="https://testserver")
    phone = TestClient(app, base_url="https://testserver")
    _login(phone, cfg)
    return requester, phone


def _challenge(c, cfg) -> str:
    r = c.post("/link/start", headers={"Origin": cfg.origin})
    assert r.status_code == 200
    return r.json()["challenge_id"]


def test_device_link_confirm_contract(auth_cfg):
    requester, phone = _pair(auth_cfg)
    cid = _challenge(requester, auth_cfg)
    r = phone.get(f"/link/approve?c={cid}")
    assert r.status_code == 200
    html = r.text
    _assert_nightjar_page(html)
    assert 'data-mode="confirm"' in html
    assert "Approve sign-in on a new device?" in html
    assert '<form method="post" action="/link/deny">' in html
    assert '<form method="post" action="/link/approve">' in html
    assert html.count(f'name="challenge_id" value="{cid}"') == 2
    assert ">Deny</button>" in html and ">Approve</button>" in html
    assert 'class="page-warn"' in html and "full access" in html


@pytest.mark.parametrize(
    ("action", "mode", "title"),
    [("approve", "approved", "Approved"), ("deny", "denied", "Denied")],
)
def test_device_link_outcomes(auth_cfg, action, mode, title):
    requester, phone = _pair(auth_cfg)
    cid = _challenge(requester, auth_cfg)
    r = phone.post(
        f"/link/{action}", data={"challenge_id": cid}, headers={"Origin": auth_cfg.origin}
    )
    assert r.status_code == 200
    _assert_nightjar_page(r.text)
    assert f'data-mode="{mode}"' in r.text and f">{title}</h1>" in r.text


def test_device_link_unavailable(auth_cfg):
    phone = _client(auth_cfg)
    _login(phone, auth_cfg)
    r = phone.get("/link/approve?c=not-a-challenge")
    _assert_nightjar_page(r.text)
    assert 'data-mode="invalid"' in r.text and "Request unavailable" in r.text


def test_device_link_approve_needs_a_session_and_the_origin(auth_cfg):
    requester, phone = _pair(auth_cfg)
    cid = _challenge(requester, auth_cfg)
    r = requester.get(f"/link/approve?c={cid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    r = phone.post(
        "/link/approve",
        data={"challenge_id": cid},
        headers={"Origin": "https://evil.test"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # A POST with no session is refused outright (401); only the GET page goes through /login.
    for verb in ("approve", "deny"):
        r = requester.post(
            f"/link/{verb}",
            data={"challenge_id": cid},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        )
        assert r.status_code == 401, verb


# ---- the share placeholder -------------------------------------------------------------------


def test_share_placeholder_page(auth_cfg, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA</html>")
    monkeypatch.setattr(main, "_WEB_DIST", dist)
    r = TestClient(create_app(auth_cfg), base_url="https://testserver").get("/p/" + "A" * 43)
    assert r.status_code == 200
    html = r.text
    _assert_nightjar_page(html)
    assert '<meta name="robots" content="noindex">' in html
    assert "<title>Shared BattleLab session</title>" in html
    assert "can't show shared sessions yet" in html
    assert "<script" not in html
