"""Scrollback-cache routes (agent-sessions#265): cache stats + clear (all/archived).
Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import engines, metadata, webterm


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
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
            result = webterm.clear_scrollback(_archived_scrollback_keys())
        else:
            raise HTTPException(status_code=422, detail="scope must be 'all' or 'archived'")
        return JSONResponse({"scope": scope, **result})
