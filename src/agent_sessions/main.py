"""FastAPI app for agent-sessions.

Surface: login/auth-check, a flat paginated session list (with title search +
project/agent-engine filters and server-computed facets), open-or-switch,
new-session (with permission bypass), rename, archive/unarchive, and the
project picker. See agent-sessions#4 (sidebar UX) and #8 (findable list).
"""

from __future__ import annotations

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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import archive, engines, metadata, ptybridge, scanner, webterm, zellij
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

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC = _HERE / "static"


def create_app(cfg: AuthConfig | None = None) -> FastAPI:
    cfg = cfg or AuthConfig.from_env()
    # Which terminal the sidebar embeds: "ttyd" (the Zellij iframe, prod default) or
    # "ws" (the self-owned xterm.js page over /ws/term, issue #49). Staging sets "ws"
    # to exercise the rebuild end-to-end before cutover flips prod.
    terminal_backend = "ws" if os.environ.get("AGENT_SESSIONS_TERMINAL") == "ws" else "ttyd"
    app = FastAPI(title="agent-sessions", openapi_url=None, docs_url=None, redoc_url=None)
    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    _logged_in = require_session(cfg)
    _csrf_guard = require_csrf_and_origin(cfg)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/auth-check")
    async def auth_check(_: str = Depends(_logged_in)) -> Response:
        # nginx `auth_request` only cares about the status code.
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        csrf = current_csrf(cfg, request)
        if not csrf:
            return RedirectResponse("/login", status_code=303)
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"csrf": csrf, "origin": cfg.origin, "terminal_backend": terminal_backend},
        )

    @app.get("/term/{sid}", response_class=HTMLResponse)
    async def terminal_page(sid: str, request: Request) -> Response:
        # Self-owned xterm.js terminal page for one session, talking to /ws/term/{sid}.
        # Logged-in only; sid shape validated so we never render for a bogus id.
        if session_uid(cfg, request) is None:
            return RedirectResponse("/login", status_code=303)
        try:
            engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        return _TEMPLATES.TemplateResponse(
            request,
            "terminal.html",
            {"sid": sid, "sid_json": json.dumps(sid)},
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        return _TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": None},
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        response: Response,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Response:
        # Fail-closed Origin/Referer check on the login POST: reject cross-site
        # submits AND originless POSTs. Same contract as require_csrf_and_origin
        # (a session/CSRF can't exist yet at login, so we check origin only).
        enforce_origin(cfg, request)

        ok_user = hmac.compare_digest(username, cfg.username)
        ok_pass = verify_password(password, cfg.password_hash)
        if not (ok_user and ok_pass):
            return _TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "invalid credentials"},
                status_code=401,
            )
        redirect = RedirectResponse("/", status_code=303)
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
            "archived": s.archived,
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
            _row(s, meta_index.get(engines.session_key(s), metadata.SessionMeta()))
            for s in engines.scan_all()
            if s.archived == archived
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
        # Same gate as the HTTP routes: a valid session cookie + matching Origin.
        # Reject BEFORE accept so an unauthenticated client never reaches a shell.
        # (issue #49: no raw shell stream on an unauthenticated path.)
        if session_uid(cfg, ws) is None:
            await ws.close(code=4401)
            return
        if not origin_matches(cfg, ws):
            await ws.close(code=4403)
            return
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            await ws.close(code=4404)
            return
        sessions = engines.scan_all()
        match = next((s for s in sessions if s.engine == prov.engine_id and s.uuid == native), None)
        if match is None or match.cwd not in scanner.scanned_cwds(sessions):
            await ws.close(code=4404)
            return
        try:
            argv = ptybridge.dtach_argv(
                engine=prov.engine_id,
                session_id=native,
                launch_argv=prov.launch_argv(native, cwd=match.cwd, bypass=True),
            )
        except ptybridge.PtyBridgeError:
            # Misconfigured launch (e.g. an engine binary that resolved to a bare
            # name instead of an absolute path) — close deterministically before
            # accept rather than letting the route raise.
            await ws.close(code=4500)
            return
        await ws.accept()
        await webterm.run(ws, argv, cwd=match.cwd, buf_key=engines.session_key(match))

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
        if not cwd:
            raise HTTPException(status_code=422, detail="cwd required")
        prov = engines.get("claude")
        try:
            tab = prov.new_session(
                cwd=cwd,
                title=name,
                allowed_cwds=scanner.pickable_projects(),
                bypass=bypass,
            )
        except zellij.ZellijError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return JSONResponse({"tab": tab, "cwd": cwd, "name": name, "bypass": bypass})

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

    return app


app = create_app() if "AGENT_SESSIONS_USERNAME" in __import__("os").environ else None
