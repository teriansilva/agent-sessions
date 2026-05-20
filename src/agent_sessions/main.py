"""FastAPI app for agent-sessions PR 3 (minimal: list + open + login + auth-check).

Mobile drawer / rename / sticky / archive controls land in PR 4.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import metadata, scanner, zellij
from .auth import (
    AuthConfig,
    clear_session,
    current_csrf,
    enforce_origin,
    issue_session,
    require_csrf_and_origin,
    require_session,
    verify_password,
)

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC = _HERE / "static"


def create_app(cfg: AuthConfig | None = None) -> FastAPI:
    cfg = cfg or AuthConfig.from_env()
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
            {"csrf": csrf, "origin": cfg.origin},
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

    @app.get("/api/sessions")
    async def list_sessions(_: str = Depends(_logged_in)) -> JSONResponse:
        sessions = scanner.scan()
        meta_index = metadata.load()
        groups: dict[str, list[dict]] = {}
        for s in sessions:
            m = meta_index.get(s.uuid, metadata.SessionMeta())
            project = m.project_alias or s.cwd
            groups.setdefault(project, []).append(
                {
                    "engine": s.engine,
                    "uuid": s.uuid,
                    "short_uuid": s.short_uuid,
                    "cwd": s.cwd,
                    "last_mtime": s.last_mtime,
                    "first_user_message": s.first_user_message,
                    "title": m.title or s.first_user_message,
                    "sticky": m.sticky,
                    "sort_key": m.sort_key,
                    "archived": s.archived,
                }
            )
        for rows in groups.values():
            rows.sort(key=lambda r: (not r["sticky"], -r["sort_key"], -r["last_mtime"]))
        return JSONResponse(
            {"projects": [{"project": p, "sessions": rows} for p, rows in sorted(groups.items())]}
        )

    @app.post("/api/sessions/{uuid}/open")
    async def open_session(
        uuid: str,
        _user: str = Depends(_logged_in),
        _csrf: None = Depends(_csrf_guard),
    ) -> JSONResponse:
        sessions = scanner.scan()
        match = next((s for s in sessions if s.uuid == uuid), None)
        if match is None:
            raise HTTPException(status_code=404, detail="unknown session")
        meta = metadata.get(uuid)
        title = meta.title or match.first_user_message or "session"
        try:
            tab = zellij.open_or_switch(
                uuid=uuid,
                cwd=match.cwd,
                title=title,
                allowed_cwds=scanner.scanned_cwds(sessions),
            )
        except zellij.ZellijError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return JSONResponse({"tab": tab})

    return app


app = create_app() if "AGENT_SESSIONS_USERNAME" in __import__("os").environ else None
