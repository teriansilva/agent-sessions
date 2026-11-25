"""HQ's Dispatches routes (#1837, #1746 P3a): the lines HQ wrote about notifications.

* ``GET  /api/hq/dispatches`` — the newest written dispatches (newest first) with "N new".
* ``GET  /api/hq/dispatches/{bid}`` — one, or 404 for an unknown, pruned or unwritten id (one
  answer for all three: a link is no existence oracle).
* ``POST /api/hq/dispatches/seen`` — ``{"through": "<bid>"}``: move the unread cursor to that
  written dispatch, clamped on the server (never backwards, never to an unwritten id).

Read only: nothing here writes a line or touches a notification row. Each member's fixed text and
link come from its own snapshot in the dispatch store, so a dismissed or evicted row still shows.
An unreadable store answers ``unreadable`` (200, nothing listed) and is never rewritten.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import hq_dispatch


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/hq/dispatches")
    async def list_dispatches(_: str = Depends(logged_in)) -> JSONResponse:
        try:
            out = await asyncio.to_thread(hq_dispatch.list_view)
        except hq_dispatch.StoreUnreadable:
            return JSONResponse({"dispatches": [], "new": 0, "unreadable": True})
        return JSONResponse({**out, "unreadable": False})

    @app.get("/api/hq/dispatches/{bid}")
    async def get_dispatch(bid: str, _: str = Depends(logged_in)) -> JSONResponse:
        try:
            out = await asyncio.to_thread(hq_dispatch.one_view, bid)
        except hq_dispatch.StoreUnreadable:
            return JSONResponse(
                {"detail": "the dispatches cannot be read right now"}, status_code=503
            )
        if out is None:
            return JSONResponse({"detail": "that dispatch isn't here"}, status_code=404)
        return JSONResponse(out)

    @app.post("/api/hq/dispatches/seen")
    async def mark_dispatches_seen(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is a 422, not a 500
            body = None
        through = body.get("through") if isinstance(body, dict) else None
        if not isinstance(through, str):
            return JSONResponse({"detail": "through (a dispatch id) is required"}, status_code=422)
        try:
            out = await asyncio.to_thread(hq_dispatch.mark_seen, through)
        except hq_dispatch.StoreUnreadable:
            return JSONResponse(
                {"detail": "the dispatches cannot be read right now"}, status_code=503
            )
        return JSONResponse(out)
