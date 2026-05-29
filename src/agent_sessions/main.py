"""FastAPI app for agent-sessions.

Surface: the React SPA shell, login/auth-check, a flat paginated session list
(with title search + project/agent-engine filters and server-computed facets),
the self-owned ws terminal (``/ws/term/{sid}``: attach / resume / new-session),
rename, archive/unarchive, the project list, and upload. See agent-sessions#4
(sidebar UX), #8 (findable list), #49 (ws terminal), #64 (React SPA cutover).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    accounts,
    archive,
    discover,
    engines,
    envfile,
    metadata,
    prefs,
    ptybridge,
    scanner,
    session_stream,
    sessions,
    sysinfo,
    twofactor,
    update,
    webterm,
)
from .auth import (
    _SESSION_COOKIE,
    AuthConfig,
    clear_preauth,
    clear_session,
    current_csrf,
    decode_preauth,
    enforce_origin,
    hash_password,
    issue_preauth,
    issue_session,
    origin_matches,
    require_csrf_and_origin,
    require_session,
    session_uid,
    verify_password,
)
from .version import get_version

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC = _HERE / "static"
# Built React SPA (Vite → web/dist), the only UI. Repo layout: <repo>/web/dist; a
# packaged install overrides via AGENT_SESSIONS_WEB_DIST.
_WEB_DIST = Path(
    os.environ.get("AGENT_SESSIONS_WEB_DIST") or (_HERE.parent.parent / "web" / "dist")
)
# Paths the SPA catch-all must never shadow (handled by their own routes / network-only).
_SPA_RESERVED = ("api", "ws", "login", "logout", "healthz", "static", "assets")

# opencode new-session reconcile tunables (#127). opencode mints its own ``ses_…`` id and
# may not write the DB row until the first message, so we poll opencode.db (read-only) for
# the new id rather than blocking the terminal. Bounded interval; no hard deadline — if
# the row never appears we just keep serving under the placeholder (the timeout path).
_OC_RECONCILE_INTERVAL_S = 0.5
# ~5 min of polling, then give up (session still served under the placeholder; no URL converge).
_OC_RECONCILE_MAX_POLLS = 600
# How long after the last byte from the agent we still call the session "working" (#156).
# Picked to feel responsive without flapping between every keystroke of a streaming reply.
_WORKING_WINDOW_S = 10.0


async def _reconcile_opencode(ws, prov, placeholder: str, cwd: str, snapshot) -> None:
    """Discover opencode's real ``ses_…`` for a placeholder launch, persist the alias,
    converge the client (#127).

    Runs concurrently with the PTY bridge. Polls opencode.db (read-only, fail-soft) for a
    session id in ``cwd`` not in ``snapshot``:
      * exactly one new id → that's ours: persist ``opencode:<placeholder> →
        opencode:<real>`` and send ``{"t":"id","sid":"opencode:<real>"}`` so the client
        replaces the URL and the sidebar de-dupes. One-shot, then stop.
      * ≥2 new ids (two same-cwd launches in the window) → AMBIGUOUS: do NOT guess; keep
        serving under the placeholder and stop reconciling (fail-safe — never the wrong
        session).
      * none yet → opencode hasn't written the row (may wait for first input); poll again.
    If the row never appears within the poll budget we stop quietly; the session keeps
    running under the placeholder (timeout path, never blocks the terminal).
    """
    placeholder_key = f"{prov.engine_id}:{placeholder}"
    for _ in range(_OC_RECONCILE_MAX_POLLS):
        await asyncio.sleep(_OC_RECONCILE_INTERVAL_S)
        result = await asyncio.to_thread(prov.reconcile_new_session, cwd, snapshot)
        if result is None:
            continue  # not written yet → keep polling
        if isinstance(result, list):
            return  # ambiguous → fail safe, stay on the placeholder
        real_key = f"{prov.engine_id}:{result}"
        # Persist the alias FIRST, and ONLY converge the client if that write succeeds. The
        # alias (real → placeholder) is what lets a later attach by the real id resolve back
        # to the placeholder's socket/lock/buffer (it survives an app restart). If we sent the
        # id frame without it, the browser URL would become /s/opencode/ses_… with no alias on
        # disk, so a reload/reattach by the real id could not find the placeholder and might
        # launch a SECOND writer for the same opencode session. On persist failure (full disk,
        # permissions, …) we stay quietly on the placeholder — the session keeps running there.
        try:
            await asyncio.to_thread(metadata.set_alias, placeholder_key, real_key)
        except Exception:
            return  # alias not durable → never converge; keep serving under the placeholder
        # Then converge the client: it replaces /s/opencode/new-… → /s/opencode/ses_…
        # (history replace, no reload, keep the socket) and the sidebar shows one row.
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"t": "id", "sid": real_key}))
        return


def _safe_next(raw: str | None) -> str:
    """Sanitize a post-login redirect target to a same-site path (open-redirect guard).

    Accept only a path beginning with a single ``/`` — reject absolute URLs,
    scheme-relative ``//host`` and backslash tricks ``/\\host`` that browsers may
    treat as host-relative. Anything else falls back to ``/``.
    """
    if raw and raw.startswith("/") and not raw.startswith(("//", "/\\")):
        return raw
    return "/"


def create_app(cfg: AuthConfig | None = None) -> FastAPI:
    cfg = cfg or AuthConfig.from_env()

    # Slice 2 of the session-stability foundation (#183): the registry is the
    # process-wide source of truth for every live dtach session. The lifespan
    # context discovers live sessions on startup (so the sidebar's working dot +
    # scrollback resume are accurate even before any browser attaches) and
    # tears the streams down on shutdown.
    registry = session_stream.SessionRegistry()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Best-effort: a discovery error must not block the app from serving
        # (the existing /api/sessions HTTP path keeps working as fallback).
        with contextlib.suppress(Exception):
            await registry.discover()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await registry.stop_all()

    app = FastAPI(
        title="agent-sessions",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.session_registry = registry
    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    if (_WEB_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    _logged_in = require_session(cfg)
    _csrf_guard = require_csrf_and_origin(cfg)

    # Runtime credential state: AuthConfig is frozen, but the admin password and the
    # first-run "must change password" flag change at runtime (forced first-login change /
    # the change endpoint). Login + the change flow read/update these live values; the new
    # hash is also persisted to the env file so it survives a restart.
    _env_file = Path(os.environ.get("AGENT_SESSIONS_ENV_FILE") or discover.default_env_path())
    _pw = {"hash": cfg.password_hash}
    _must_change = {
        # No password to change in `none` mode — the forced-change gate is always off.
        "v": cfg.auth_mode != "none"
        and os.environ.get("AGENT_SESSIONS_FORCE_PASSWORD_CHANGE", "")
        in {
            "1",
            "true",
            "yes",
        }
    }

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
        if password and verify_password(password, _pw["hash"]):
            return True
        return False

    def _apply_password_change(current: str, new: str) -> str | None:
        """Verify the current password, persist a new one (hash only) + clear the
        force-change flag, and update the live state. Returns an error string or None."""
        if not verify_password(current, _pw["hash"]):
            return "incorrect"
        if len(new) < 12:
            return "weak"
        new_hash = hash_password(new)
        envfile.update(_env_file, {accounts.HASH_KEY: new_hash, accounts.FORCE_CHANGE_KEY: None})
        _pw["hash"] = new_hash
        _must_change["v"] = False
        return None

    # Force the first-login password change "before anything else": while the flag is
    # set, a logged-in request to anything outside this allowlist is blocked — API/ws get
    # 403, page navigations are redirected to /change-password. (Unauthenticated requests
    # fall through to normal auth handling; ws is gated in its own handler.)
    _CHANGE_ALLOW = {
        "/change-password",
        "/api/password",
        "/api/config",
        "/api/auth-check",
        "/login",
        "/logout",
        "/healthz",
    }

    # `none` auth-mode (#13 / #32 Phase 3): no login at all. Before any route runs,
    # ensure every request carries a valid admin session cookie — auto-issue one when
    # absent so require_session / require_csrf_and_origin / current_csrf all behave as
    # if the single admin had logged in. We mint the signed cookie value, splice it
    # into the *request* cookies (so downstream deps decode a session this turn) and
    # set it on the *response* (so the browser keeps it). CSRF + Origin stay enforced:
    # they guard against cross-site requests, which matters even without auth.
    @app.middleware("http")
    async def _none_mode_autosession(request: Request, call_next):
        if cfg.auth_mode != "none":
            return await call_next(request)
        if session_uid(cfg, request) is None:
            stub = Response()
            issue_session(cfg, stub)
            set_cookie = stub.headers.get("set-cookie", "")
            token = set_cookie.split(";", 1)[0].split("=", 1)[1] if "=" in set_cookie else ""
            # Splice the freshly-minted cookie into this request so the route's
            # session deps see a valid session on this very turn.
            existing = request.headers.get("cookie", "")
            new_cookie = (
                f"{existing}; {_SESSION_COOKIE}={token}"
                if existing
                else (f"{_SESSION_COOKIE}={token}")
            )
            headers = [(k, v) for (k, v) in request.scope["headers"] if k.lower() != b"cookie"]
            headers.append((b"cookie", new_cookie.encode("latin-1")))
            request.scope["headers"] = headers
            # Drop Starlette's cached header/cookie parse so downstream deps re-read the
            # spliced cookie from the mutated scope.
            for attr in ("_headers", "_cookies"):
                if hasattr(request, attr):
                    delattr(request, attr)
            response = await call_next(request)
            response.headers.append("set-cookie", set_cookie)
            return response
        return await call_next(request)

    @app.middleware("http")
    async def _force_change_gate(request: Request, call_next):
        if _must_change["v"] and session_uid(cfg, request) is not None:
            path = request.url.path
            if path not in _CHANGE_ALLOW and not path.startswith(("/static/", "/assets/")):
                if path.startswith(("/api/", "/ws/")):
                    return JSONResponse({"detail": "password change required"}, status_code=403)
                return RedirectResponse("/change-password", status_code=303)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/auth-check")
    async def auth_check(request: Request) -> Response:
        # nginx `auth_request` only cares about the status code. In `none` mode there
        # is no login → always 204. In single-user mode, 204 with a valid cookie, else 401.
        if cfg.auth_mode == "none":
            return Response(status_code=204)
        if session_uid(cfg, request) is None:
            raise HTTPException(status_code=401, detail="no session")
        return Response(status_code=204)

    @app.get("/api/version")
    async def app_version(_: str = Depends(_logged_in)) -> JSONResponse:
        # Runtime version for the dashboard + the self-update flow (#65). Authed.
        return JSONResponse({"version": get_version()})

    @app.get("/api/engines")
    async def list_engines(_: str = Depends(_logged_in)) -> JSONResponse:
        # Discovery for the Settings "Connected agents" section: every known provider
        # with its presence + whether it can start a new session + the resolved binary
        # path (or null). Authed; GET, so no CSRF.
        return JSONResponse(
            {
                "engines": [
                    {
                        "id": p.engine_id,
                        "present": p.is_present(),
                        "supports_new": bool(getattr(p, "supports_new", False)),
                        "bin": discover.resolve(p.engine_id),
                    }
                    for p in engines.all_providers()
                ]
            }
        )

    @app.get("/api/system")
    async def system_info(_: str = Depends(_logged_in)) -> JSONResponse:
        # Host/system info for the Settings "System" section. Stdlib only, every field
        # fail-soft (omitted on error / non-Linux). No network interfaces / IPs. Authed.
        return JSONResponse(sysinfo.collect())

    @app.get("/api/update/check")
    async def update_check(_: str = Depends(_logged_in)) -> JSONResponse:
        # Compare the running version to the channel's latest on the remote (#65 Phase 5).
        return JSONResponse(update.check())

    @app.post("/api/update/apply")
    async def update_apply(
        _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
    ) -> JSONResponse:
        # Update to the channel's latest — no user-supplied ref/command. Re-runs the
        # installer detached (atomic release + flip + restart + health-check + rollback).
        if not update.apply():
            raise HTTPException(status_code=503, detail="self-update unavailable (not an install)")
        return JSONResponse({"status": "updating"}, status_code=202)

    @app.get("/api/config")
    async def app_config(request: Request, _: str = Depends(_logged_in)) -> JSONResponse:
        # SPA bootstrap (#64): the CSRF token for mutations + which engines can start a
        # new session (present + supports_new) + the terminal backend. Authed-only.
        return JSONResponse(
            {
                "csrf": current_csrf(cfg, request) or "",
                "new_session_engines": [
                    p.engine_id
                    for p in engines.present_providers()
                    if getattr(p, "supports_new", False)
                ],
                "terminal_backend": "ws",
                "must_change_password": _must_change["v"],
                # "single-user" | "none" — lets the SPA hide login/logout UI when there
                # is no login (#13 / #32 Phase 3).
                "auth_mode": cfg.auth_mode,
                # Per-user UI theme (#109). The SPA applies this at load so a non-Royal
                # choice carries across devices; localStorage is the device cache.
                "theme": prefs.get_theme(),
                # Sidebar body: the session list, or the squeezed Session Overview map (#139).
                # Persisted per-user like the theme; the SPA applies it at load.
                "sidebar_view": prefs.get_sidebar_view(),
                # Session Overview view-state (#144): expanded cluster cwds (default collapsed)
                # and project cwds excluded from the map. Per-user.
                "overview_expanded": prefs.get_overview_expanded(),
                # `overview_excluded` was the legacy name (#144); `projects_hidden` (#174) is
                # the same idea but with broader scope (sidebar list + filter + map + picker).
                # Both keys are emitted during the transition window so an old client tab still
                # reads its hidden list; new clients prefer `projects_hidden`.
                "overview_excluded": prefs.get_projects_hidden(),
                "projects_hidden": prefs.get_projects_hidden(),
                # Per-cwd custom project display names (#148).
                "project_names": prefs.get_project_names(),
                # Optional TOTP 2FA (#116): only the on/off bit for the Settings UI — never
                # the secret or recovery codes. In `none` mode 2FA is N/A → always false.
                "two_factor_enabled": cfg.auth_mode != "none" and twofactor.is_enabled(),
            }
        )

    @app.post("/api/prefs")
    async def set_prefs(
        request: Request,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        # Persist UI preferences (#109 theme, #139 sidebar_view, #144 overview lists). Each
        # provided key is validated server-side (unknown value → 422, never silently coerced
        # on write); other persisted keys are preserved. At least one known key must be present.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        out: dict[str, object] = {}
        if "theme" in payload:
            if payload["theme"] not in prefs.THEMES:
                raise HTTPException(status_code=422, detail="unknown theme")
            out["theme"] = prefs.set_theme(payload["theme"])
        if "sidebar_view" in payload:
            if payload["sidebar_view"] not in prefs.SIDEBAR_VIEWS:
                raise HTTPException(status_code=422, detail="unknown sidebar_view")
            out["sidebar_view"] = prefs.set_sidebar_view(payload["sidebar_view"])
        for key, setter in (
            ("overview_expanded", prefs.set_overview_expanded),
            # The legacy `overview_excluded` write path is kept for clients still on the old
            # API surface — internally it routes to the same `projects_hidden` storage so
            # the two never diverge (#174).
            ("overview_excluded", prefs.set_projects_hidden),
            ("projects_hidden", prefs.set_projects_hidden),
        ):
            if key in payload:
                v = payload[key]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise HTTPException(status_code=422, detail=f"{key} must be a list of strings")
                out[key] = setter(v)
        if "project_names" in payload:
            v = payload["project_names"]
            if not isinstance(v, dict) or not all(
                isinstance(k, str) and isinstance(val, str) for k, val in v.items()
            ):
                raise HTTPException(
                    status_code=422, detail="project_names must be an object of string→string"
                )
            out["project_names"] = prefs.set_project_names(v)
        if not out:
            raise HTTPException(status_code=422, detail="no known preference key")
        return JSONResponse(out)

    @app.post("/api/password")
    async def change_password_api(
        request: Request,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
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
        request: Request, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
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
        request: Request, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
    ) -> Response:
        # Verify a code against the pending secret → enable. Never enabled without a code.
        _require_2fa_available()
        code, _ = await _proof_from_body(request)
        if not (code and twofactor.confirm_enrollment(code)):
            raise HTTPException(status_code=400, detail="invalid or expired enrollment code")
        return Response(status_code=204)

    @app.post("/api/2fa/disable")
    async def twofa_disable(
        request: Request, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
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
        request: Request, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        # React SPA: serve the shell; the app handles auth via /api 401 (login at /login).
        # (The forced-password-change gate is enforced for all routes by middleware below.)
        return FileResponse(_WEB_DIST / "index.html")

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        # No login screen in `none` mode — bounce to the app.
        if cfg.auth_mode == "none":
            return RedirectResponse("/", status_code=303)
        # `next` lets the SPA bounce a 401 back to where the user was (open-redirect
        # guarded → same-site paths only); preserved through the POST via a hidden field.
        return _TEMPLATES.TemplateResponse(
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
        ok_pass = verify_password(password, _pw["hash"])  # live hash (changeable at runtime)
        if not (ok_user and ok_pass):
            return _TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "invalid credentials", "next": target},
                status_code=401,
            )
        # Optional second factor (#116): when 2FA is enabled, a correct password does NOT
        # mint a session — it issues a short-lived pre-auth cookie and shows the TOTP step.
        # The full session is minted only after POST /login/totp verifies the code.
        if twofactor.is_enabled():
            page = _TEMPLATES.TemplateResponse(
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
            return _TEMPLATES.TemplateResponse(
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
        return _TEMPLATES.TemplateResponse(request, "change_password.html", {"error": error})

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
            return _TEMPLATES.TemplateResponse(
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
    async def logout(_: None = Depends(_csrf_guard)) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        clear_session(resp)
        return resp

    def _row(s, m: metadata.SessionMeta) -> dict:
        key = engines.session_key(s)
        # #156 working signal: last byte we observed flowing into the shared ring.
        # Slice 2 (#183): the server-owned SessionStream writes under the PHYSICAL
        # key (opencode placeholder for a reconciled new-session). The row id stays
        # the LOGICAL key (real ``ses_…``) so the URL/sidebar are unchanged — but
        # the lookup must resolve through the alias map first, or a headless
        # reconciled-opencode row would always report idle.
        phys_key = engines.physical_key(key)
        last_out = webterm.get_last_output_at(phys_key)
        return {
            "id": key,
            "engine": s.engine,
            "uuid": s.uuid,
            "short_uuid": s.short_uuid,
            "cwd": s.cwd,
            "project": m.project_alias or s.cwd,
            "last_mtime": s.last_mtime,
            "last_output_at": last_out,
            "working": (last_out is not None) and (time.time() - last_out < _WORKING_WINDOW_S),
            "first_user_message": s.first_user_message,
            "title": m.title or s.first_user_message,
            "sticky": m.sticky,
            "sort_key": m.sort_key,
            # Effective archive state: the sidecar override wins when set (lets a
            # natively-archived opencode/codex row be unarchived), else the engine's
            # native state (claude's JSONL tree / opencode.db time_archived).
            "archived": m.archived if m.archived is not None else s.archived,
        }

    @app.get("/api/sessions")
    async def list_sessions(
        _: str = Depends(_logged_in),
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
        archived: bool = Query(False),
        q: str | None = Query(None),
        project: str | None = Query(None),
        engine: str | None = Query(None),
    ) -> JSONResponse:
        # Flat, paginated, newest-first. sticky floats to the top of the
        # *first window* (a first-window concept, not a global pin).
        meta_index = metadata.load()
        # opencode new-session alias (#127): the live row is the real ``ses_…`` from
        # scan_all (the placeholder never appears here — it isn't in opencode.db), so
        # there is no ghost row to drop. But metadata set while the session was still on
        # its placeholder (title/sticky/archive before reconcile) is keyed by the
        # placeholder; resolve each scanned id to its physical key so that metadata
        # follows the real row — one row, with its sidecar intact, no duplicate.
        aliases = metadata.load_aliases()

        def _meta_for(s) -> metadata.SessionMeta:
            key = engines.session_key(s)
            phys = engines.physical_key(key, aliases)
            return meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()

        # Hidden projects (#174) are stripped server-side BEFORE pagination + facets are
        # computed, so totals/next_offset/facet lists all describe the visible-to-the-user
        # set. Filtering only on the client would make `total` and the filter dropdown lie.
        # Hide is keyed by cwd (the row's `cwd` field), not the display name.
        hidden = set(prefs.get_projects_hidden())
        scoped = [
            row
            for s in engines.scan_all()
            for row in [_row(s, _meta_for(s))]
            if row["archived"] == archived and row["cwd"] not in hidden
        ]
        # Facets for the project/agent dropdowns: distinct values over the visible (already
        # hide-filtered) archived-scoped set, computed BEFORE q/project/engine filtering —
        # so the dropdowns list every project/engine present, including ones past the
        # first page, regardless of what's currently filtered or loaded.
        facets = {
            "projects": sorted({r["project"] for r in scoped}),
            "engines": sorted({r["engine"] for r in scoped}),
        }
        # Normalize filters; empty / whitespace-only means "no filter".
        q_norm = (q or "").strip().casefold()
        project_f = (project or "").strip() or None
        engine_f = (engine or "").strip() or None

        def _keep(r: dict) -> bool:
            if q_norm and q_norm not in (r["title"] or "").casefold():
                return False
            if project_f is not None and r["project"] != project_f:
                return False
            if engine_f is not None and r["engine"] != engine_f:
                return False
            return True

        # Filter BEFORE limit/offset so total + next_offset describe the filtered
        # set and "load more" stays within results.
        rows = [r for r in scoped if _keep(r)]
        rows.sort(key=lambda r: (not r["sticky"], -r["sort_key"], -r["last_mtime"]))
        window = rows[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(rows) else None
        return JSONResponse(
            {
                "sessions": window,
                "next_offset": next_offset,
                "total": len(rows),
                "facets": facets,
            }
        )

    @app.get("/api/projects")
    async def list_projects(_: str = Depends(_logged_in)) -> JSONResponse:
        # New-session picker + the Settings "Session overview" manager — hidden projects
        # (#174) are excluded here too. Picking a hidden project as a start location would
        # feel inconsistent with the user having explicitly said "I don't want to see this."
        #
        # Source from ALL engines (#196): the sidebar filter dropdown derives its options
        # from /api/sessions facets, which are computed over engines.scan_all(). If this
        # endpoint used the Claude-only scan (pickable_projects' default), an opencode/gemini
        # cwd would appear in the filter but be unmanageable here — the two lists drift.
        # Passing scan_all() unifies the superset so every filterable project is manageable.
        hidden = set(prefs.get_projects_hidden())
        return JSONResponse(
            {
                "projects": [
                    {"cwd": c, "label": c}
                    for c in scanner.pickable_projects(sessions=engines.scan_all())
                    if c not in hidden
                ]
            }
        )

    def _archived_scrollback_keys() -> list[str]:
        # Physical scrollback keys for every currently-archived session, across engines.
        # Mirrors `_row`'s effective-archived + alias resolution so the keys line up with
        # what `webterm` persisted (it keys the ring/disk by the PHYSICAL id). (#206)
        meta_index = metadata.load()
        aliases = metadata.load_aliases()
        keys: list[str] = []
        for s in engines.scan_all():
            key = engines.session_key(s)
            phys = engines.physical_key(key, aliases)
            m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
            archived = m.archived if m.archived is not None else s.archived
            if archived:
                keys.append(phys)
        return keys

    @app.get("/api/scrollback")
    async def scrollback_info(_: str = Depends(_logged_in)) -> JSONResponse:
        # Size of the persisted-scrollback cache (#206), for the Settings cache panel.
        return JSONResponse(webterm.scrollback_cache_stats())

    @app.post("/api/scrollback/clear")
    async def scrollback_clear(
        request: Request,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        # Clear the persisted-scrollback cache (#206). scope="all" wipes everything (also
        # reclaims orphaned files from deleted sessions); scope="archived" clears only the
        # caches of currently-archived sessions. Clearing drops the in-memory ring too, so
        # a cleared session won't be re-served from memory.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            payload = {}
        scope = payload.get("scope", "all") if isinstance(payload, dict) else "all"
        if scope == "all":
            result = webterm.clear_scrollback(None)
        elif scope == "archived":
            result = webterm.clear_scrollback(_archived_scrollback_keys())
        else:
            raise HTTPException(status_code=422, detail="scope must be 'all' or 'archived'")
        return JSONResponse({"scope": scope, **result})

    @app.websocket("/ws/term/{sid}")
    async def ws_term(ws: WebSocket, sid: str) -> None:
        # Accept FIRST, then close with a code on rejection. A pre-accept close fails
        # the ws handshake, and browsers report that as code 1006 (abnormal) — not our
        # 44xx — so the client reconnect loop never recognizes a deliberate reject and
        # hammers forever. Accepting then closing delivers the real code to onclose.
        # No shell is ever streamed before the checks pass, so the auth gate holds.
        await ws.accept()

        async def reject(code: int) -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=code)

        if session_uid(cfg, ws) is None:
            return await reject(4401)
        if not origin_matches(cfg, ws):
            return await reject(4403)
        if _must_change["v"]:
            return await reject(4403)  # forced password change pending — no sessions yet
        is_new = ws.query_params.get("new") == "1"
        try:
            # The opencode new-session placeholder (``new-<uuid>``) is a valid id ONLY on
            # the new=1 launch path (#127); resume/attach still requires the native shape.
            prov, native = engines.parse_key(sid, allow_new_placeholder=is_new)
        except engines.EngineError:
            return await reject(4404)

        # Alias resolution (#127): for opencode new-session we launch under a placeholder
        # and reconcile to opencode's real ``ses_…`` id, persisting a placeholder→real
        # alias. When a client later attaches by the *real* id (after the URL converged or
        # an app restart), its live resources (dtach socket / single-writer lock / buffer)
        # are still under the placeholder — so resolve the real id back to the physical
        # placeholder key before any socket/lock/buffer derivation. No-op for everything
        # else. Skipped on the new=1 launch (the placeholder IS the physical key).
        # Two ids, kept distinct (#127 review): `native` is the LOGICAL/real id from the
        # URL (a real ``ses_…`` or, on new=1, the placeholder) — used for scanned-session
        # matching + ``launch_argv`` (so a real id still resumes via ``opencode --session``
        # even when the placeholder master is gone). `phys_native` is the PHYSICAL key the
        # live resources (dtach socket / single-writer lock / scrollback buffer) sit under
        # — the placeholder for a reconciled opencode session, else == native. Never
        # overwrite `native` with the placeholder, or a real URL would 4404 on LAUNCH.
        phys_native = native
        if not is_new:
            resolved = engines.physical_key(f"{prov.engine_id}:{native}")
            if resolved != f"{prov.engine_id}:{native}":
                _eng, _, phys_native = resolved.partition(":")
        phys_key = f"{prov.engine_id}:{phys_native}"

        # Single-writer policy: ATTACH to a live master, LAUNCH under the launch lock,
        # or BUSY (no local master but the lock is held elsewhere — never relaunch).
        # Keyed by the PHYSICAL id so an attach by the real id finds the placeholder master.
        action, lock = sessions.open_action(prov.engine_id, phys_native)
        if action == sessions.BUSY:
            return await reject(4409)  # held by another writer; client should retry → attach
        # opencode new-session reconcile (#127): set when this connection launches an
        # opencode placeholder; runs concurrently with the PTY bridge to discover
        # opencode's real ``ses_…`` id, persist the alias, and converge the client URL.
        reconcile_task = None
        try:
            if action == sessions.ATTACH:
                # A live dtach session already exists → attach regardless of new/resume.
                # dtach -A attaches (ignoring the cmd), so a fresh session survives a
                # browser reload before it has written its on-disk history. cwd is only
                # for the (unused-on-attach) spawn; a scanned cwd if known, else home.
                scanned = next(
                    (
                        s
                        for s in engines.scan_all()
                        if s.engine == prov.engine_id and s.uuid == native
                    ),
                    None,
                )
                cwd = scanned.cwd if scanned else str(Path.home())
                launch = prov.launch_argv(native, cwd=cwd, bypass=True)
            elif is_new:
                # Start a FRESH session with this client-generated id, in a picker cwd.
                # Validate against the same all-engine superset the picker offers (#196),
                # so a cwd the UI presented is never rejected on launch.
                new_cwd = ws.query_params.get("cwd") or ""
                if new_cwd not in set(scanner.pickable_projects(sessions=engines.scan_all())):
                    return await reject(4404)
                # Honor the modal's permission-bypass choice (default on); only "0" is off.
                bypass = ws.query_params.get("bypass") != "0"
                # opencode can't pin a new-session id: it launches under the placeholder
                # and we DB-diff opencode.db to find the real id (#127). Snapshot the
                # cwd's existing ids BEFORE launch so the diff attributes the one new id
                # to us; then arm the concurrent reconcile. A None snapshot means the
                # baseline read FAILED (not empty) — we skip reconciliation entirely rather
                # than risk misattributing a pre-existing row, and serve under the placeholder.
                oc_snapshot = None
                if engines.is_opencode_new_placeholder(f"{prov.engine_id}:{native}"):
                    oc_snapshot = prov.snapshot_session_ids(new_cwd)
                try:
                    launch = prov.new_launch_argv(native, cwd=new_cwd, bypass=bypass)
                except NotImplementedError:
                    return await reject(4404)  # engine can't pin a new-session id
                cwd = new_cwd
                if oc_snapshot is not None:
                    reconcile_task = asyncio.create_task(
                        _reconcile_opencode(ws, prov, native, new_cwd, oc_snapshot)
                    )
            else:
                # Resume an EXISTING scanned session.
                sessions_all = engines.scan_all()
                match = next(
                    (s for s in sessions_all if s.engine == prov.engine_id and s.uuid == native),
                    None,
                )
                if match is None or match.cwd not in scanner.scanned_cwds(sessions_all):
                    return await reject(4404)
                launch = prov.launch_argv(native, cwd=match.cwd, bypass=True)
                cwd = match.cwd
            try:
                # Mode-explicit dtach (#165): on ATTACH the server has already verified
                # a live master exists, so `dtach -a` is correct (and refuses to silently
                # create a second master if the probe-vs-attach race lost). On LAUNCH the
                # server holds the lock and any stale sock was unlinked in `open_action`,
                # so `dtach -c` will bind cleanly. Socket is keyed by the PHYSICAL id so
                # attach/resume by the real id reaches the same master.
                if action == sessions.ATTACH:
                    argv = ptybridge.attach_argv(engine=prov.engine_id, session_id=phys_native)
                else:
                    argv = ptybridge.launch_argv(
                        engine=prov.engine_id, session_id=phys_native, launch_argv=launch
                    )
            except ptybridge.PtyBridgeError:
                return await reject(4500)  # misconfigured launch (e.g. bare-name binary)
            # Delta-resume: a reconnecting client reports the absolute byte offset it
            # last saw; we stream only the bytes since then (never re-blank). Bad/absent
            # value → 0 → full replay. buf_key is the PHYSICAL key (placeholder for an
            # opencode new-session) so scrollback stays under one key across the alias.
            try:
                have = max(0, int(ws.query_params.get("have", "0") or "0"))
            except (ValueError, TypeError):
                have = 0
            # Handoff to the server-owned SessionStream registry (#183 slice 2).
            # on_attach STOPS any running server-owned stream for this key, so the
            # WS bridge becomes the sole writer to ``_BUFFERS[phys_key]`` during
            # the attached window; on_detach (in the finally) spawns a fresh
            # server-owned stream if the dtach master is still alive. Best-effort
            # — registry errors must not affect the browser path.
            registry = app.state.session_registry
            with contextlib.suppress(Exception):
                await registry.on_attach(prov.engine_id, phys_native)
            # Per-tab claim (#184 slice 3): empty fp/tab from an older client
            # falls through as "owner with no recorded claim" (backward-compat).
            # ``force=1`` lets a deliberate takeover demote a stale or recent owner.
            fp = ws.query_params.get("fp", "") or ""
            tab_id = ws.query_params.get("tab", "") or ""
            force = ws.query_params.get("force", "") == "1"
            role = "owner"
            claim_obj: session_stream.Claim | None = None
            with contextlib.suppress(Exception):
                role, claim_obj = await registry.claim(
                    prov.engine_id, phys_native, fp, tab_id, force=force
                )
            # Read-only gate fires when the WS is a secondary OR when a force
            # takeover demotes the owner mid-session. Server-side gate is the
            # source of truth — pump_in drops input/resize while it's set.
            read_only_gate = asyncio.Event()
            if role == "secondary":
                read_only_gate.set()
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"t": "role", "role": role}))
            # Watcher: if another tab force-claims, demoted fires → flip gate
            # + tell the browser so it can render the read-only banner.
            demote_task: asyncio.Task | None = None
            if claim_obj is not None:

                async def _watch_demote() -> None:
                    assert claim_obj is not None
                    await claim_obj.demoted.wait()
                    read_only_gate.set()
                    with contextlib.suppress(Exception):
                        await ws.send_text(json.dumps({"t": "role", "role": "secondary"}))

                demote_task = asyncio.create_task(_watch_demote())
            try:
                await webterm.run(
                    ws,
                    argv,
                    cwd=cwd,
                    buf_key=phys_key,
                    lock=lock,
                    have=have,
                    read_only_gate=read_only_gate,
                )
            finally:
                if demote_task is not None:
                    demote_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await demote_task
                with contextlib.suppress(Exception):
                    if claim_obj is not None:
                        await registry.release(prov.engine_id, phys_native, fp, tab_id)
                with contextlib.suppress(Exception):
                    await registry.on_detach(prov.engine_id, phys_native)
        finally:
            # Cancel the reconcile probe, but NEVER let its cancellation (a BaseException,
            # not Exception) bypass the lock handoff below — nest it in its own try/finally
            # and suppress CancelledError too (#127 review).
            try:
                if reconcile_task is not None:
                    reconcile_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reconcile_task
            finally:
                # Hand the launch lock to the dtach master we spawned (it inherited the
                # fd), so the flock lives for the master's lifetime — closing our fd
                # without unlocking keeps it held while the master runs, and releases it if
                # no master was spawned (early reject) or once the master dies. ATTACH
                # holds no lock.
                if lock is not None:
                    lock.transfer()

    @app.post("/api/sessions/{sid}/rename")
    async def rename_session(
        sid: str,
        request: Request,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        payload = await request.json()
        title = str(payload.get("title", "")).strip()
        if not title:
            raise HTTPException(status_code=422, detail="title required")
        m = metadata.patch(key, title=title[:120])
        return JSONResponse({"id": key, "title": m.title})

    @app.post("/api/sessions/{sid}/archive")
    async def archive_session(
        sid: str, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            prov.archive(native)
        except archive.ArchiveError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NotImplementedError:
            raise HTTPException(
                status_code=400, detail=f"archive not supported for engine {prov.engine_id}"
            ) from None
        return JSONResponse({"id": f"{prov.engine_id}:{native}", "archived": True})

    @app.post("/api/sessions/{sid}/unarchive")
    async def unarchive_session(
        sid: str, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            prov.unarchive(native)
        except archive.ArchiveError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NotImplementedError:
            raise HTTPException(
                status_code=400, detail=f"unarchive not supported for engine {prov.engine_id}"
            ) from None
        return JSONResponse({"id": f"{prov.engine_id}:{native}", "archived": False})

    @app.post("/api/sessions/archive-older")
    async def archive_older(
        request: Request, _user: str = Depends(_logged_in), _csrf: None = Depends(_csrf_guard)
    ) -> JSONResponse:
        # Bulk-archive every (non-archived) session whose last activity is older than `hours`
        # (#142). Reuses the per-session archive; engines that can't archive (opencode/codex)
        # are skipped, not errored. Reversible — the archived sessions can be unarchived.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        hours = payload.get("hours") if isinstance(payload, dict) else None
        # Reject non-numbers, bool (a bool is an int in Python), ≤0, and absurd horizons.
        if (
            not isinstance(hours, int | float)
            or isinstance(hours, bool)
            or hours <= 0
            or hours > 24 * 3650
        ):
            raise HTTPException(status_code=422, detail="hours must be a positive number")
        cutoff = time.time() - hours * 3600.0
        archived = 0
        skipped = 0
        for s in engines.scan_all():
            if s.archived or (s.last_mtime or 0) >= cutoff:
                continue
            try:
                prov, native = engines.parse_key(engines.session_key(s))
                prov.archive(native)
                archived += 1
            except (NotImplementedError, archive.ArchiveError, engines.EngineError):
                skipped += 1  # engine can't archive / lost the file → leave it, keep going
        return JSONResponse({"archived": archived, "skipped": skipped})

    @app.post("/api/upload")
    async def upload_context(
        file: UploadFile = File(...),
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        # Save a pasted/dropped image or file so a Claude/opencode session can read
        # it by path (the web terminal can't carry image paste itself). Lands in a
        # shared ~/.agent-sessions/uploads/ — never in a project working tree.
        # Stream-read with a hard cap so a huge upload can't exhaust memory.
        max_bytes = 25 * 1024 * 1024
        size, chunks = 0, []
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="file too large (max 25 MB)")
            chunks.append(chunk)
        if not size:
            raise HTTPException(status_code=422, detail="empty upload")
        # Sanitise to a bare, safe basename — no path separators, no traversal.
        raw_name = Path(file.filename or "upload").name
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:80] or "upload"
        dest_dir = Path.home() / ".agent-sessions" / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"{stamp}-{safe}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stamp}-{n}-{safe}"
            n += 1
        dest.write_bytes(b"".join(chunks))
        return JSONResponse({"path": str(dest), "name": safe})

    # SPA history fallback (registered LAST so it never shadows the API/ws/auth routes
    # above). A real built file (sw.js, manifest.webmanifest, favicon…) is served as-is;
    # anything else (client routes like /s/claude/<id>) → index.html.
    @app.get("/{spa_path:path}", response_class=HTMLResponse)
    async def spa_fallback(spa_path: str) -> Response:
        if spa_path.split("/", 1)[0] in _SPA_RESERVED:
            raise HTTPException(status_code=404, detail="not found")
        candidate = (_WEB_DIST / spa_path).resolve()
        if spa_path and candidate.is_file() and _WEB_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_WEB_DIST / "index.html")

    return app


app = (
    create_app()
    if (
        "AGENT_SESSIONS_USERNAME" in os.environ
        or os.environ.get("AGENT_SESSIONS_AUTH_MODE") == "none"
    )
    else None
)
