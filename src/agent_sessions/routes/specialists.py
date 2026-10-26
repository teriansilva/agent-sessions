"""Specialist routes (#1693, #1613 P3) — `/api/specialists`.

* ``GET  /api/specialists``                       — the pool + the starter templates
* ``POST /api/specialists``                       — create (version 1)
* ``GET  /api/specialists/{id}``                  — one specialist, with every version
* ``PATCH /api/specialists/{id}``                 — ``{revision, draft}``: a new version
* ``POST /api/specialists/{id}/archive|unarchive`` — ``{revision}``
* ``GET  /api/specialists/{id}/versions/{n}``     — one pinned version

Reads need a session; every write needs the session AND ``csrf_guard`` (which carries the
Origin/Referer check). A draft is checked OFF the loop: an API client's model list may mean asking
its CLI. The grade in every response is derived from the operator's model map at read.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import model_grades, specialists
from .. import specialists_store as store

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/specialists"
#: A draft is well under 8 KiB (12 skills, a 280-character summary); anything past this is refused.
BODY_MAX = 32 * 1024


class _BadRequest(Exception):
    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


async def _body(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise _BadRequest("request body is too large", 413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise _BadRequest("request body is too large", 413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _BadRequest("request body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise _BadRequest("request body must be a JSON object")
    return payload


def _revision(body: dict) -> int:
    raw = body.get("revision")
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise _BadRequest("revision is required and is the number you loaded")
    return raw


def _library() -> dict[str, str] | None:
    try:
        return specialists.library_revisions()
    except Exception:  # noqa: BLE001 — an unreadable library flags nothing; it never fails a read
        return None


def _public(spec: dict, grades: dict, library: dict[str, str] | None) -> dict:
    return store.public(spec, grades=grades, library=library)


def _list() -> dict:
    grades, library = model_grades.get(), _library()
    return {
        "specialists": [_public(s, grades, library) for s in store.list_all()],
        "templates": list(specialists.TEMPLATES),
    }


def _detail(sid: str) -> dict:
    out = _public(store.get(sid), model_grades.get(), _library())
    out["versions"] = store.versions(sid)
    return out


def _create(draft: object) -> dict:
    body = specialists.check_draft(draft)
    return _detail(store.create(body)["id"])


def _update(sid: str, revision: int, draft: object) -> dict:
    store.get(sid)  # 404 before a draft check that may ask a CLI
    body = specialists.check_draft(draft)
    store.update(sid, body, expect_revision=revision)
    return _detail(sid)


def _archive(sid: str, revision: int, archived: bool) -> dict:
    store.set_archived(sid, archived, expect_revision=revision)
    return _detail(sid)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    async def _run(fn, *args, status: int = 200) -> JSONResponse:
        try:
            return _json(await asyncio.to_thread(fn, *args), status)
        except specialists.DraftRefused as e:
            return _json({"detail": e.detail}, e.status)
        except store.StoreError as e:
            return _json({"detail": str(e)}, e.status)
        except model_grades.GradesRefused as e:
            return _json({"detail": str(e)}, 422)

    def _sid(request: Request) -> str:
        sid = request.path_params["sid"]
        if not store.ID_RE.fullmatch(sid):
            raise _BadRequest("unknown specialist", 404)
        return sid

    def _bad(e: _BadRequest) -> JSONResponse:
        return _json({"detail": e.detail}, e.status)

    @app.get(PREFIX)
    async def list_specialists(_user: str = Depends(logged_in)) -> JSONResponse:
        return await _run(_list)

    @app.post(PREFIX)
    async def create_specialist(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            body = await _body(request)
        except _BadRequest as e:
            return _bad(e)
        return await _run(_create, body, status=201)

    @app.get(PREFIX + "/{sid}")
    async def get_specialist(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            sid = _sid(request)
        except _BadRequest as e:
            return _bad(e)
        return await _run(_detail, sid)

    @app.patch(PREFIX + "/{sid}")
    async def update_specialist(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            sid = _sid(request)
            body = await _body(request)
            if set(body) - {"revision", "draft"}:
                raise _BadRequest("expected {revision, draft}")
            revision = _revision(body)
        except _BadRequest as e:
            return _bad(e)
        return await _run(_update, sid, revision, body.get("draft"))

    async def _set_archived(request: Request, archived: bool) -> JSONResponse:
        try:
            sid = _sid(request)
            body = await _body(request)
            if set(body) - {"revision"}:
                raise _BadRequest("expected {revision}")
            revision = _revision(body)
        except _BadRequest as e:
            return _bad(e)
        return await _run(_archive, sid, revision, archived)

    @app.post(PREFIX + "/{sid}/archive")
    async def archive_specialist(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return await _set_archived(request, True)

    @app.post(PREFIX + "/{sid}/unarchive")
    async def unarchive_specialist(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return await _set_archived(request, False)

    @app.get(PREFIX + "/{sid}/versions/{n}")
    async def get_version(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            sid = _sid(request)
        except _BadRequest as e:
            return _bad(e)
        raw = request.path_params["n"]
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 6:
            return _json({"detail": "unknown specialist version"}, 404)
        return await _run(store.version, sid, int(raw))
