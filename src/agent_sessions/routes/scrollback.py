"""Scrollback-cache routes (agent-sessions#265): cache stats + clear (all/archived).
Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import scrollback, webterm


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/scrollback")
    async def scrollback_info(_: str = Depends(logged_in)) -> JSONResponse:
        # Size of the persisted-scrollback cache (#206), for the Settings cache panel.
        return JSONResponse(webterm.scrollback_cache_stats())

    @app.post("/api/scrollback/clear")
    async def scrollback_clear(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
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
            # The archived-key resolver is shared with Settings → Maintenance's prune (#993).
            result = webterm.clear_scrollback(scrollback.archived_keys())
        else:
            raise HTTPException(status_code=422, detail="scope must be 'all' or 'archived'")
        return JSONResponse({"scope": scope, **result})
