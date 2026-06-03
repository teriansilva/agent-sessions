"""Account/auth routes (agent-sessions#265): password change (API + server-rendered
page), optional TOTP 2FA enroll/confirm/disable/recovery-codes, login (form + submit),
the 2FA login step, and logout. Moved verbatim from ``main.create_app``.

The mutable ``pw`` / ``must_change`` dicts are created in ``create_app`` and passed in by
reference (shared-mutation semantics): ``pw`` holds the live admin password hash and
``must_change`` the forced-first-login flag that the ``_force_change_gate`` middleware,
the ws handler, and ``/api/config`` also read.
"""

from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import accounts, discover, envfile, twofactor
from ..auth import (
    AuthConfig,
    clear_preauth,
    clear_session,
    decode_preauth,
    enforce_origin,
    hash_password,
    issue_preauth,
    issue_session,
    session_uid,
    verify_password,
)


def _safe_next(raw: str | None) -> str:
    """Sanitize a post-login redirect target to a same-site path (open-redirect guard).

    Accept only a path beginning with a single ``/`` — reject absolute URLs,
    scheme-relative ``//host`` and backslash tricks ``/\\host`` that browsers may
    treat as host-relative. Anything else falls back to ``/``.
    """
    if raw and raw.startswith("/") and not raw.startswith(("//", "/\\")):
        return raw
    return "/"


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    logged_in,
    csrf_guard,
    templates: Jinja2Templates,
    pw: dict,
    must_change: dict,
) -> None:
    # Runtime credential state: AuthConfig is frozen, but the admin password and the
    # first-run "must change password" flag change at runtime (forced first-login change /
    # the change endpoint). Login + the change flow read/update these live values; the new
    # hash is also persisted to the env file so it survives a restart.
    _env_file = Path(os.environ.get("AGENT_SESSIONS_ENV_FILE") or discover.default_env_path())

    # Brute-force throttle for the 2FA login step. Single admin → one global counter is
    # enough. After _TOTP_MAX_FAILS failed code attempts the step locks for _TOTP_LOCKOUT_S;
    # any success resets it. (Replay protection is separate + persisted in twofactor.py.)
    _TOTP_MAX_FAILS = 10
    _TOTP_LOCKOUT_S = 300
    _totp_throttle = {"fails": 0, "locked_until": 0.0}

    def _totp_locked() -> bool:
        return time.time() < _totp_throttle["locked_until"]

    def _totp_note_fail() -> None:
        _totp_throttle["fails"] += 1
        if _totp_throttle["fails"] >= _TOTP_MAX_FAILS:
            _totp_throttle["locked_until"] = time.time() + _TOTP_LOCKOUT_S
            _totp_throttle["fails"] = 0

    def _totp_reset() -> None:
        _totp_throttle["fails"] = 0
        _totp_throttle["locked_until"] = 0.0

    def _verify_2fa_proof(code: str | None, password: str | None) -> bool:
        """Fresh proof for disable / regenerate: a current TOTP (non-consuming) OR the
        current password. Origin + CSRF are enforced by the route dependency on top."""
        if code and twofactor.check_totp(code):
            return True
        if password and verify_password(password, pw["hash"]):
            return True
        return False

    def _apply_password_change(current: str, new: str) -> str | None:
        """Verify the current password, persist a new one (hash only) + clear the
        force-change flag, and update the live state. Returns an error string or None."""
        if not verify_password(current, pw["hash"]):
            return "incorrect"
        if len(new) < 12:
            return "weak"
        new_hash = hash_password(new)
        envfile.update(_env_file, {accounts.HASH_KEY: new_hash, accounts.FORCE_CHANGE_KEY: None})
        pw["hash"] = new_hash
        must_change["v"] = False
        return None

    @app.post("/api/password")
    async def change_password_api(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> Response:
        # Change the admin password (current + new) for the SPA. The Jinja /change-password
        # page is the server-rendered equivalent. New must be ≥ 12 chars.
        payload = await request.json()
        err = _apply_password_change(
            str(payload.get("current_password", "")), str(payload.get("new_password", ""))
        )
        if err == "incorrect":
            raise HTTPException(status_code=403, detail="current password is incorrect")
        if err == "weak":
            raise HTTPException(status_code=422, detail="new password must be ≥ 12 characters")
        return Response(status_code=204)

    # ---- optional TOTP 2FA (#116) -------------------------------------------------
    # All authed + CSRF/origin guarded. In `none` mode there is no login → 2FA is N/A, so
    # these 404. While the forced-password-change flag is set, the /api/* gate already
    # blocks them (403) — so a password change always precedes enrollment.

    def _require_2fa_available() -> None:
        if cfg.auth_mode == "none":
            raise HTTPException(status_code=404, detail="2FA unavailable in this auth mode")

    async def _proof_from_body(request: Request) -> tuple[str | None, str | None]:
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        code = str(payload.get("code", "")).strip() or None
        password = payload.get("password")
        password = password if isinstance(password, str) and password else None
        return code, password

    @app.post("/api/2fa/enroll")
    async def twofa_enroll(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Begin enrollment → secret + otpauth URI + one-time recovery codes (shown once,
        # never returned again). Does not enable 2FA until /api/2fa/confirm.
        _require_2fa_available()
        # Re-enrolling while 2FA is ALREADY on would replace the active secret/recovery
        # codes — so it needs the same fresh proof as disable/regenerate. A first-time
        # enrollment (2FA off) just needs the authed session (you logged in moments ago).
        if twofactor.is_enabled():
            code, password = await _proof_from_body(request)
            if not _verify_2fa_proof(code, password):
                raise HTTPException(
                    status_code=403, detail="current 2FA code or password required to re-enroll"
                )
        return JSONResponse(twofactor.begin_enrollment(cfg.username))

    @app.post("/api/2fa/confirm")
    async def twofa_confirm(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        # Verify a code against the pending secret → enable. Never enabled without a code.
        _require_2fa_available()
        code, _ = await _proof_from_body(request)
        if not (code and twofactor.confirm_enrollment(code)):
            raise HTTPException(status_code=400, detail="invalid or expired enrollment code")
        return Response(status_code=204)

    @app.post("/api/2fa/disable")
    async def twofa_disable(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        # Turn 2FA off. Requires a FRESH proof (current TOTP or password) on top of the
        # session + CSRF/origin, so a stale logged-in browser can't silently weaken auth.
        _require_2fa_available()
        if not twofactor.is_enabled():
            return Response(status_code=204)  # already off — idempotent
        code, password = await _proof_from_body(request)
        if not _verify_2fa_proof(code, password):
            raise HTTPException(status_code=403, detail="current 2FA code or password required")
        twofactor.disable()
        return Response(status_code=204)

    @app.post("/api/2fa/recovery-codes")
    async def twofa_recovery_codes(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Regenerate recovery codes (invalidates the old set). Same fresh-proof requirement
        # as disable. Returns the new codes once.
        _require_2fa_available()
        if not twofactor.is_enabled():
            raise HTTPException(status_code=400, detail="2FA is not enabled")
        code, password = await _proof_from_body(request)
        if not _verify_2fa_proof(code, password):
            raise HTTPException(status_code=403, detail="current 2FA code or password required")
        return JSONResponse({"recovery_codes": twofactor.regenerate_recovery()})

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        # No login screen in `none` mode — bounce to the app.
        if cfg.auth_mode == "none":
            return RedirectResponse("/", status_code=303)
        # `next` lets the SPA bounce a 401 back to where the user was (open-redirect
        # guarded → same-site paths only); preserved through the POST via a hidden field.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "next": _safe_next(request.query_params.get("next"))},
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        response: Response,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ) -> Response:
        # No login in `none` mode — the auto-session middleware already established it.
        if cfg.auth_mode == "none":
            return RedirectResponse("/", status_code=303)
        # Fail-closed Origin/Referer check on the login POST: reject cross-site
        # submits AND originless POSTs. Same contract as require_csrf_and_origin
        # (a session/CSRF can't exist yet at login, so we check origin only).
        enforce_origin(cfg, request)

        target = _safe_next(next)
        ok_user = hmac.compare_digest(username, cfg.username)
        ok_pass = verify_password(password, pw["hash"])  # live hash (changeable at runtime)
        if not (ok_user and ok_pass):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "invalid credentials", "next": target},
                status_code=401,
            )
        # Optional second factor (#116): when 2FA is enabled, a correct password does NOT
        # mint a session — it issues a short-lived pre-auth cookie and shows the TOTP step.
        # The full session is minted only after POST /login/totp verifies the code.
        if twofactor.is_enabled():
            page = templates.TemplateResponse(
                request, "login_totp.html", {"error": None, "next": target}
            )
            issue_preauth(cfg, page, cfg.username)
            return page
        redirect = RedirectResponse(target, status_code=303)
        issue_session(cfg, redirect)
        return redirect

    @app.post("/login/totp")
    async def login_totp(
        request: Request,
        code: str = Form(...),
        next: str = Form("/"),
    ) -> Response:
        # Second factor step. Gated by the pre-auth cookie (set by /login after a correct
        # password). Origin-checked like the login POST; no CSRF token exists yet.
        enforce_origin(cfg, request)
        target = _safe_next(next)
        pre = decode_preauth(cfg, request)
        if pre is None:
            # No / expired pre-auth → restart at the password step.
            return RedirectResponse(f"/login?next={target}", status_code=303)

        def fail(msg: str, status: int = 401) -> Response:
            return templates.TemplateResponse(
                request, "login_totp.html", {"error": msg, "next": target}, status_code=status
            )

        if _totp_locked():
            return fail("too many attempts — try again later", status=429)

        code = code.strip()
        ok = twofactor.verify_totp_for_login(code) or twofactor.verify_recovery_for_login(code)
        if not ok:
            _totp_note_fail()
            return fail("invalid code")
        _totp_reset()
        redirect = RedirectResponse(target, status_code=303)
        issue_session(cfg, redirect)
        clear_preauth(redirect)
        return redirect

    @app.get("/change-password", response_class=HTMLResponse)
    async def change_password_form(request: Request, error: str | None = None) -> Response:
        if session_uid(cfg, request) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "change_password.html", {"error": error})

    @app.post("/change-password")
    async def change_password_submit(
        request: Request,
        current: str = Form(...),
        new: str = Form(...),
        confirm: str = Form(...),
    ) -> Response:
        # Server-rendered change page (the forced first-login wizard + a manual change).
        # Origin-checked + session-gated, mirroring the login POST.
        enforce_origin(cfg, request)
        if session_uid(cfg, request) is None:
            return RedirectResponse("/login", status_code=303)

        def fail(msg: str) -> Response:
            return templates.TemplateResponse(
                request, "change_password.html", {"error": msg}, status_code=400
            )

        if new != confirm:
            return fail("passwords do not match")
        err = _apply_password_change(current, new)
        if err == "incorrect":
            return fail("current password is incorrect")
        if err == "weak":
            return fail("new password must be at least 12 characters")
        # Re-issue the session and land on the app.
        redirect = RedirectResponse("/", status_code=303)
        issue_session(cfg, redirect)
        return redirect

    @app.post("/logout")
    async def logout(_: None = Depends(csrf_guard)) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        clear_session(resp)
        return resp
