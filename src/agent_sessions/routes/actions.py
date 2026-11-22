"""``GET /api/actions`` — Library → Actions, the gallery (#2248 P1). Read-only.

Every action's manifest facts (label, category, description, typed inputs and outputs, effect
class, triggers), what it needs and whether that holds now, and how many automations use it. The
automation wizard's What step picks from the same list. Login, like `/api/automations`; there is no
write here, so no CSRF. `no-store`: availability and use change under it.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..actions import registry

ACTIONS_API = "/api/actions"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def register(app: FastAPI, *, logged_in) -> None:
    @app.get(ACTIONS_API)
    async def list_actions(_user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(registry.gallery), headers=_NO_STORE)
