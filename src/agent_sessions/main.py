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
import time
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    accounts,
    discover,
    engines,
    envfile,
    metadata,
    ptybridge,
    scanner,
    session_stream,
    sessions,
    twofactor,
    webterm,
)
from .auth import (
    _SESSION_COOKIE,
    AuthConfig,
    clear_preauth,
    clear_session,
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
from .routes import scrollback as scrollback_routes
from .routes import sessions as sessions_routes
from .routes import system as system_routes
from .routes import upload as upload_routes

# Re-export the "working" window (now owned by routes/sessions.py) under its historical
# name here, so callers (and tests) that read agent_sessions.main._WORKING_WINDOW_S keep
# resolving the same value (#265).
from .routes.sessions import _WORKING_WINDOW_S as _WORKING_WINDOW_S

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

    # Info/settings routes (healthz, auth-check, version, engines, system, update
    # check/apply, config, prefs) live in routes/system.py (agent-sessions#265).
    system_routes.register(
        app,
        cfg=cfg,
        logged_in=_logged_in,
        csrf_guard=_csrf_guard,
        must_change=_must_change,
    )

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

    # Session-data routes (list/search + facets, projects, rename, archive/unarchive,
    # archive-older) live in routes/sessions.py; scrollback stats/clear in
    # routes/scrollback.py (agent-sessions#265). Both register here, before the ws
    # handler + SPA catch-all, preserving registration order.
    sessions_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    scrollback_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

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

            # Initial PTY size (#227): size the pty to the client's real grid up front, so a
            # launched agent renders at the right width from its first frame instead of starting
            # at 80x24 and then reflowing (garbling scrollback) when the client's first resize
            # lands. A reconnect/attach also sizes the dtach-client pty correctly from the start.
            def _dim(name: str, default: int, hi: int) -> int:
                try:
                    return max(1, min(hi, int(ws.query_params.get(name, "") or default)))
                except (ValueError, TypeError):
                    return default

            init_cols = _dim("cols", 80, 500)
            init_rows = _dim("rows", 24, 300)
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
                    cols=init_cols,
                    rows=init_rows,
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

    # Upload route (save a pasted/dropped file to the shared uploads dir) lives in
    # routes/upload.py (agent-sessions#265). Registered before the SPA catch-all.
    upload_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

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
