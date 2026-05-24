"""FastAPI app for agent-sessions.

Surface: login/auth-check, a flat paginated session list (with title search +
project/agent-engine filters and server-computed facets), open-or-switch,
new-session (with permission bypass), rename, archive/unarchive, and the
project picker. See agent-sessions#4 (sidebar UX) and #8 (findable list).
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import time
import urllib.parse
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

from . import archive, engines, metadata, ptybridge, scanner, sessions, webterm, zellij
from .auth import (
    AuthConfig,
    clear_session,
    current_csrf,
    enforce_origin,
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
# Built React SPA (Vite → web/dist). Repo layout: <repo>/web/dist; a packaged
# install overrides via AGENT_SESSIONS_WEB_DIST. Served only when AGENT_SESSIONS_UI=react.
_WEB_DIST = Path(
    os.environ.get("AGENT_SESSIONS_WEB_DIST") or (_HERE.parent.parent / "web" / "dist")
)
# Paths the SPA catch-all must never shadow (handled by their own routes / network-only).
_SPA_RESERVED = ("api", "ws", "term", "login", "logout", "healthz", "static", "assets")


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
    # Which terminal the sidebar embeds: "ttyd" (the Zellij iframe, prod default) or
    # "ws" (the self-owned xterm.js page over /ws/term, issue #49). Staging sets "ws"
    # to exercise the rebuild end-to-end before cutover flips prod.
    terminal_backend = "ws" if os.environ.get("AGENT_SESSIONS_TERMINAL") == "ws" else "ttyd"
    # Serve the built React SPA (#64 rebuild) instead of the Jinja UI when opted in.
    # Default stays "jinja" so prod + the existing test suite are unchanged.
    react_ui = os.environ.get("AGENT_SESSIONS_UI") == "react" and _WEB_DIST.is_dir()
    app = FastAPI(title="agent-sessions", openapi_url=None, docs_url=None, redoc_url=None)
    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    if react_ui and (_WEB_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    _logged_in = require_session(cfg)
    _csrf_guard = require_csrf_and_origin(cfg)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/auth-check")
    async def auth_check(_: str = Depends(_logged_in)) -> Response:
        # nginx `auth_request` only cares about the status code.
        return Response(status_code=204)

    @app.get("/api/version")
    async def app_version(_: str = Depends(_logged_in)) -> JSONResponse:
        # Runtime version for the dashboard + the self-update flow (#65). Authed.
        return JSONResponse({"version": get_version()})

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
                "terminal_backend": terminal_backend,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        # React SPA: serve the shell; the app handles auth via /api 401 (login at /login).
        if react_ui:
            return FileResponse(_WEB_DIST / "index.html")
        csrf = current_csrf(cfg, request)
        if not csrf:
            return RedirectResponse("/login", status_code=303)
        # Engines that can start a NEW session (present + capable) — drives the New
        # dialog's agent picker. claude/opencode spawn via Zellij; codex is resume-only.
        new_engines = [
            p.engine_id for p in engines.present_providers() if getattr(p, "supports_new", False)
        ]
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "csrf": csrf,
                "origin": cfg.origin,
                "terminal_backend": terminal_backend,
                "new_session_engines": json.dumps(new_engines),
            },
        )

    @app.get("/term/{sid}", response_class=HTMLResponse)
    async def terminal_page(
        sid: str,
        request: Request,
        new: bool = Query(False),
        cwd: str | None = Query(None),
        bypass: bool = Query(True),
    ) -> Response:
        # Self-owned xterm.js terminal page for one session, talking to /ws/term/{sid}.
        # Logged-in only; sid shape validated so we never render for a bogus id.
        # `new=1&cwd=…&bypass=…` is forwarded to the ws so a fresh session launches in
        # that cwd, honoring the modal's permission-bypass choice.
        if session_uid(cfg, request) is None:
            return RedirectResponse("/login", status_code=303)
        try:
            engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        ws_query = ""
        if new and cwd:
            ws_query = (
                "?new=1&cwd="
                + urllib.parse.quote(cwd, safe="")
                + "&bypass="
                + ("1" if bypass else "0")
            )
        return _TEMPLATES.TemplateResponse(
            request,
            "terminal.html",
            {
                "sid": sid,
                "sid_json": json.dumps(sid),
                "ws_query_json": json.dumps(ws_query),
                "csrf_json": json.dumps(current_csrf(cfg, request) or ""),
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
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
        # Fail-closed Origin/Referer check on the login POST: reject cross-site
        # submits AND originless POSTs. Same contract as require_csrf_and_origin
        # (a session/CSRF can't exist yet at login, so we check origin only).
        enforce_origin(cfg, request)

        target = _safe_next(next)
        ok_user = hmac.compare_digest(username, cfg.username)
        ok_pass = verify_password(password, cfg.password_hash)
        if not (ok_user and ok_pass):
            return _TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "invalid credentials", "next": target},
                status_code=401,
            )
        redirect = RedirectResponse(target, status_code=303)
        issue_session(cfg, redirect)
        return redirect

    @app.post("/logout")
    async def logout(_: None = Depends(_csrf_guard)) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        clear_session(resp)
        return resp

    def _row(s, m: metadata.SessionMeta) -> dict:
        return {
            "id": engines.session_key(s),
            "engine": s.engine,
            "uuid": s.uuid,
            "short_uuid": s.short_uuid,
            "cwd": s.cwd,
            "project": m.project_alias or s.cwd,
            "last_mtime": s.last_mtime,
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
        scoped = [
            row
            for s in engines.scan_all()
            for row in [_row(s, meta_index.get(engines.session_key(s), metadata.SessionMeta()))]
            if row["archived"] == archived
        ]
        # Facets for the project/agent dropdowns: distinct values over the full
        # archived-scoped set, computed BEFORE q/project/engine filtering — so the
        # dropdowns list every project/engine present, including ones past the
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
        return JSONResponse(
            {"projects": [{"cwd": c, "label": c} for c in scanner.pickable_projects()]}
        )

    @app.post("/api/sessions/{sid}/open")
    async def open_session(
        sid: str,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        sessions = engines.scan_all()
        match = next((s for s in sessions if s.engine == prov.engine_id and s.uuid == native), None)
        if match is None:
            raise HTTPException(status_code=404, detail="unknown session")
        meta = metadata.get(engines.session_key(match))
        title = meta.title or match.first_user_message or "session"
        try:
            tab = prov.open_or_switch(
                native,
                cwd=match.cwd,
                title=title,
                allowed_cwds=scanner.scanned_cwds(sessions),
                bypass=True,
            )
        except zellij.ZellijError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return JSONResponse({"tab": tab})

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
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            return await reject(4404)

        # Single-writer policy: ATTACH to a live master, LAUNCH under the launch lock,
        # or BUSY (no local master but the lock is held elsewhere — never relaunch).
        action, lock = sessions.open_action(prov.engine_id, native)
        if action == sessions.BUSY:
            return await reject(4409)  # held by another writer; client should retry → attach
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
            elif ws.query_params.get("new") == "1":
                # Start a FRESH session with this client-generated id, in a picker cwd.
                new_cwd = ws.query_params.get("cwd") or ""
                if new_cwd not in set(scanner.pickable_projects()):
                    return await reject(4404)
                # Honor the modal's permission-bypass choice (default on); only "0" is off.
                bypass = ws.query_params.get("bypass") != "0"
                try:
                    launch = prov.new_launch_argv(native, cwd=new_cwd, bypass=bypass)
                except NotImplementedError:
                    return await reject(4404)  # engine can't pin a new-session id
                cwd = new_cwd
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
                argv = ptybridge.dtach_argv(
                    engine=prov.engine_id, session_id=native, launch_argv=launch
                )
            except ptybridge.PtyBridgeError:
                return await reject(4500)  # misconfigured launch (e.g. bare-name binary)
            # Delta-resume: a reconnecting client reports the absolute byte offset it
            # last saw; we stream only the bytes since then (never re-blank). Bad/absent
            # value → 0 → full replay.
            try:
                have = max(0, int(ws.query_params.get("have", "0") or "0"))
            except (ValueError, TypeError):
                have = 0
            await webterm.run(ws, argv, cwd=cwd, buf_key=sid, lock=lock, have=have)
        finally:
            # Hand the launch lock to the dtach master we spawned (it inherited the fd),
            # so the flock lives for the master's lifetime — closing our fd without
            # unlocking keeps it held while the master runs, and releases it if no master
            # was spawned (early reject) or once the master dies. ATTACH holds no lock.
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

    @app.post("/api/projects/new")
    async def new_session(
        request: Request,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        payload = await request.json()
        cwd = str(payload.get("cwd", "")).strip()
        name = str(payload.get("name", "")).strip() or "session"
        bypass = bool(payload.get("bypass_permissions", True))
        # Engine picker (defaults to claude → unchanged behavior for old clients). Only
        # engines that are present AND can start a new session are accepted.
        engine = str(payload.get("engine", "claude")).strip() or "claude"
        if not cwd:
            raise HTTPException(status_code=422, detail="cwd required")
        prov = engines.get(engine)
        if prov is None or not getattr(prov, "supports_new", False) or not prov.is_present():
            raise HTTPException(status_code=422, detail=f"cannot start a new {engine} session")
        try:
            tab = prov.new_session(
                cwd=cwd,
                title=name,
                allowed_cwds=scanner.pickable_projects(),
                bypass=bypass,
            )
        except zellij.ZellijError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return JSONResponse(
            {"tab": tab, "cwd": cwd, "name": name, "engine": engine, "bypass": bypass}
        )

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

    if react_ui:
        # SPA history fallback (registered LAST so it never shadows the API/ws/term/
        # auth routes above). A real built file (sw.js, manifest.webmanifest, favicon…)
        # is served as-is; anything else (client routes like /s/claude/<id>) → index.html.
        @app.get("/{spa_path:path}", response_class=HTMLResponse)
        async def spa_fallback(spa_path: str) -> Response:
            if spa_path.split("/", 1)[0] in _SPA_RESERVED:
                raise HTTPException(status_code=404, detail="not found")
            candidate = (_WEB_DIST / spa_path).resolve()
            if spa_path and candidate.is_file() and _WEB_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(_WEB_DIST / "index.html")

    return app


app = create_app() if "AGENT_SESSIONS_USERNAME" in __import__("os").environ else None
