"""Settings → AI → HQ → Authority (#2390, epic #2070 A1) and the acts log.

* ``GET /api/hq/authority`` — the classes, the derived preset, the revision, which classes can act
  in this version and which are RISKY (`hq_authority.view`).
* ``PUT /api/hq/authority`` — ``{"revision", "preset"}`` or ``{"revision", "classes"}``: a
  compare-and-set on the revision (409 when stale). Login, CSRF and Origin like every write, and
  **never from HQ itself**: an in-process request (`asgi_call`, how every HQ operation reaches a
  route) is refused 403, so no catalog entry, now or later, can change HQ's own authority.
* ``GET /api/hq/acts?limit=`` — what HQ did on its own, newest first (`hq_acts.recent`).
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import asgi_call, hq_acts, hq_authority

LOCKED_FROM_HQ = "HQ can't change its own authority; only you can, in Settings"


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    app.state.logout_hooks = [*getattr(app.state, "logout_hooks", []), hq_authority.logged_out]

    @app.get("/api/hq/authority")
    async def get_authority(_: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(hq_authority.view))

    @app.put("/api/hq/authority")
    async def put_authority(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        if asgi_call.in_process(request.scope):
            return JSONResponse({"detail": LOCKED_FROM_HQ}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is a 422, not a 500
            body = None
        try:
            out = await asyncio.to_thread(hq_authority.update, body)
        except hq_authority.AuthorityError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        return JSONResponse(out)

    @app.get("/api/hq/acts")
    async def list_acts(limit: int = 50, _: str = Depends(logged_in)) -> JSONResponse:
        acts = await asyncio.to_thread(hq_acts.recent, max(1, min(limit, hq_acts.READ_MAX)))
        return JSONResponse({"acts": acts})
