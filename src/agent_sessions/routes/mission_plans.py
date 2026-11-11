"""Mission plans in the Library (#1924, #1613 P7a) — `/api/mission-plans`.

* ``GET  /api/mission-plans``                         — the Library (``?archived=1`` adds archived)
* ``GET  /api/mission-plans/{id}``                    — one plan: its document and derived steps
* ``POST /api/mission-plans/{id}/archive``            — ``{archived, expect_revision}``
* ``POST /api/missions/{id}/plan/save-as-plan``       — ``{revision, title, summary?, slot_labels}``

Reads need a session; every write needs the session AND ``csrf_guard`` (which carries the
Origin/Referer check); every response is ``no-store``. **Inert:** no route here creates a mission,
a dispatch proposal or a session — a mission plan is planning data (P5/P6 plan and run from one,
under plan approval).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import mission_plan_library as library
from .. import missions

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/mission-plans"
#: Title, summary and twenty slot labels: well under 16 KiB.
BODY_MAX = 32 * 1024
SAVE_KEYS = frozenset({"revision", "title", "summary", "slot_labels"})


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


async def _body(request: Request, allowed: frozenset[str]) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise library.LibraryError("request body is too large", status=413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise library.LibraryError("request body is too large", status=413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise library.LibraryError("request body is not valid JSON", status=422) from None
    if not isinstance(payload, dict):
        raise library.LibraryError("request body must be a JSON object", status=422)
    extra = sorted(set(payload) - allowed)
    if extra:
        raise library.LibraryError(f"unknown field {extra[0]!r}", status=422)
    return payload


def _list(archived: bool) -> dict:
    return {"plans": [library.public(p) for p in library.list_all(archived=archived)]}


def _detail(pid: str) -> dict:
    return library.public(library.get(pid), full=True)


def _archive(pid: str, body: dict) -> dict:
    library.set_archived(pid, body.get("archived"), expect_revision=body.get("expect_revision"))
    return _detail(pid)


def _save(mission_id: str, body: dict) -> dict:
    plan = library.save_from_mission(
        mission_id,
        body.get("revision"),
        title=body.get("title"),
        summary=body.get("summary"),
        slot_labels=body.get("slot_labels"),
    )
    return {"id": plan["id"], "version": plan["version"]}


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    def _fail(e: missions.MissionError) -> JSONResponse:
        return _json({"detail": str(e)}, e.status)

    @app.get(PREFIX)
    async def list_mission_plans(
        archived: int = 0, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        try:
            return _json(await asyncio.to_thread(_list, archived == 1))
        except missions.MissionError as e:
            return _fail(e)

    @app.get(PREFIX + "/{pid}")
    async def get_mission_plan(pid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            return _json(await asyncio.to_thread(_detail, pid))
        except missions.MissionError as e:
            return _fail(e)

    @app.post(PREFIX + "/{pid}/archive")
    async def archive_mission_plan(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request, frozenset({"archived", "expect_revision"}))
            return _json(await asyncio.to_thread(_archive, pid, body))
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/plan/save-as-plan")
    async def save_mission_as_plan(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Save one of the mission's plan revisions to the Library as a new mission plan.
        Reads the missions store through its admission, writes only the Library."""
        try:
            body = await _body(request, SAVE_KEYS)
            out = await missions.run_admitted(lambda: _save(mission_id, body))
        except missions.MissionError as e:
            return _fail(e)
        return _json(out, 201)
