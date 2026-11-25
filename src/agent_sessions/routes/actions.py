"""``GET /api/actions`` — Library → Actions, the gallery (#2248 P1). Read-only.

``GET /api/actions/scripts?project_id=`` — the files a *Run a script* action could pick in a
project's folder (#2248 P3): the wizard's script picker. Read-only, and only inside the roots and
outside ``folder_exclusions``; the save checks the pick again and pins its checksum. HQ coverage
``never`` (HQ never authors a command or picks a script).

Every action's manifest facts (label, category, description, typed inputs and outputs, effect
class, triggers), what it needs and whether that holds now, and how many automations use it. The
automation wizard's What step picks from the same list. Login, like `/api/automations`; there is no
write here, so no CSRF. `no-store`: availability and use change under it.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from .. import automations as model
from ..actions import registry

ACTIONS_API = "/api/actions"
SCRIPTS_API = "/api/actions/scripts"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def register(app: FastAPI, *, logged_in) -> None:
    @app.get(ACTIONS_API)
    async def list_actions(_user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(registry.gallery), headers=_NO_STORE)

    @app.get(SCRIPTS_API)
    async def list_scripts(project_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        from ..actions import run_script

        try:
            body = await asyncio.to_thread(run_script.list_scripts, project_id)
        except model.AutomationError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status, headers=_NO_STORE)
        return JSONResponse(body, headers=_NO_STORE)
