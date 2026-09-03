"""Template routes (#905, P1) — the operator's library of reusable messages.

* ``GET    /api/templates``                 — the library, most recently used first, plus the bounds
* ``POST   /api/templates``                 — create; the server mints the id
* ``PATCH  /api/templates/{id}``            — whole-record replace of the editable fields, fenced by
  ``expected_updated_at``: a stale edit is 409 carrying the current record, and nothing is written
* ``DELETE /api/templates/{id}?expected_updated_at=…`` — the same fence
* ``POST   /api/templates/{id}/used``       — bump the usage counters (never ``updated_at``)

Deliberately NOT on ``/api/config`` (see ``templates.py``). Auth matches every other settings
surface: the reads need a session, the writes need a session AND the CSRF guard (which also
carries the Origin/Referer check). Every rule on a body runs server-side in ``templates.validate``;
the editor's own checks are a courtesy, not the gate.

**Every response is ``no-store``, success and error alike** — a template body is verbatim
instructions, and a 409's ``current`` record carries one too. Applied at the outermost boundary
(a middleware, the ``routes/files.py`` shape) so the 401 that ``Depends(logged_in)`` raises before
any handler runs, and an exception that escapes a handler, are covered as well.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import templates as store
from .upload import NO_STORE

#: What a PATCH body may carry beyond the editable fields. Unknown keys are refused rather than
#: ignored, so a server-owned field sent as input fails loudly.
_PATCH_EXTRA = frozenset({"expected_updated_at"})


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise _err(422, "invalid JSON body") from None
    if not isinstance(payload, dict):
        raise _err(422, "body must be an object")
    return payload


def _expected(value: object) -> float:
    if value is None:
        raise _err(422, "expected_updated_at is required")
    try:
        return store.parse_fence(value)
    except store.TemplateError as e:
        raise _err(422, str(e)) from None


def _conflict(exc: store.TemplateConflict) -> JSONResponse:
    return JSONResponse(
        {"detail": "template changed since you loaded it", "current": exc.current},
        status_code=409,
        headers=NO_STORE,
    )


def _json(payload: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=NO_STORE)


def _err(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers=NO_STORE)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.middleware("http")
    async def _templates_are_never_cached(request: Request, call_next):
        path = request.url.path
        if not (path == "/api/templates" or path.startswith("/api/templates/")):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse(
                {"detail": "the template store failed"}, status_code=500, headers=NO_STORE
            )
        response.headers.update(NO_STORE)
        return response

    @app.get("/api/templates")
    async def list_templates(_user: str = Depends(logged_in)) -> JSONResponse:
        return _json({"templates": store.list_templates(), "limits": store.LIMITS})

    @app.post("/api/templates", status_code=201)
    async def create_template(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        payload = await _json_object(request)
        try:
            rec = store.create_template(payload)
        except store.TemplateError as e:
            raise _err(422, str(e)) from None
        except store.TemplateStoreUnsupported as e:
            raise _err(409, str(e)) from None
        return _json(rec, 201)

    @app.patch("/api/templates/{tid}")
    async def update_template(
        tid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        payload = await _json_object(request)
        expected = _expected(payload.get("expected_updated_at"))
        fields = {k: v for k, v in payload.items() if k not in _PATCH_EXTRA}
        try:
            rec = store.update_template(tid, fields, expected)
        except store.TemplateError as e:
            raise _err(422, str(e)) from None
        except store.TemplateStoreUnsupported as e:
            raise _err(409, str(e)) from None
        except store.TemplateNotFound:
            raise _err(404, "unknown template") from None
        except store.TemplateConflict as e:
            return _conflict(e)
        return _json(rec)

    @app.delete("/api/templates/{tid}")
    async def delete_template(
        tid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> Response:
        expected = _expected(request.query_params.get("expected_updated_at"))
        try:
            store.delete_template(tid, expected)
        except store.TemplateStoreUnsupported as e:
            raise _err(409, str(e)) from None
        except store.TemplateNotFound:
            raise _err(404, "unknown template") from None
        except store.TemplateConflict as e:
            return _conflict(e)
        return Response(status_code=204, headers=NO_STORE)

    @app.post("/api/templates/{tid}/used")
    async def mark_used(
        tid: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            rec = store.mark_used(tid)
        except store.TemplateStoreUnsupported as e:
            raise _err(409, str(e)) from None
        except store.TemplateNotFound:
            raise _err(404, "unknown template") from None
        return _json(rec)
