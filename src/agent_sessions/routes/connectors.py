"""The connector API (#1750): add, configure, test, enable and remove a connector instance.

Login on every route; CSRF and Origin on every mutation (`csrf_guard`); strict, bounded JSON
bodies; fixed operations only. No response ever carries a token or an envelope (`store.public`),
and every response is ``no-store``. An unreadable store refuses every route (503) and is never
rewritten.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import template_secrets
from ..connectors import service, store

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/connectors"
BODY_MAX = 8 * 1024


class _BadRequest(Exception):
    def __init__(self, detail: str, status: int = 422, code: str = "bad_body") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.code = code


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


async def _run(fn: Callable, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except store.NotFound:
        raise _BadRequest("no such connector", 404, "not_found") from None
    except store.Refused as e:
        raise _BadRequest(str(e), 409 if e.code in _CONFLICTS else 422, e.code) from None


_CONFLICTS = frozenset(
    {"origin_needs_token", "needs_test", "needs_token", "needs_origin", "too_many"}
)


def _public_one(inst: dict) -> dict:
    from ..connectors import manifest

    return store.public(inst, manifest.bundled())


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.middleware("http")
    async def _connectors_errors(request: Request, call_next):
        path = request.url.path
        if not (path == PREFIX or path.startswith(PREFIX + "/")):
            return await call_next(request)
        try:
            response = await call_next(request)
        except _BadRequest as e:
            return _json({"detail": e.detail, "code": e.code}, e.status)
        except store.StoreUnreadable as e:
            return _json({"detail": str(e), "code": "store_unreadable"}, 503)
        except template_secrets.SecretKeyUnavailable as e:
            return _json({"detail": str(e), "code": "key_unavailable"}, 409)
        except Exception:
            return _json({"detail": "the connector store failed", "code": "failed"}, 500)
        response.headers.update(NO_STORE)
        return response

    @app.get(PREFIX)
    async def get_connectors(_user: str = Depends(logged_in)):
        return _json(await asyncio.to_thread(service.catalog))

    @app.post(PREFIX + "/instances", status_code=201)
    async def add_instance(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        body = await _body(request)
        if set(body) != {"manifest_id"} or not isinstance(body["manifest_id"], str):
            raise _BadRequest("send exactly {manifest_id}")
        inst = await _run(store.add, body["manifest_id"])
        return _json(_public_one(inst), 201)

    @app.patch(PREFIX + "/instances/{iid}")
    async def patch_instance(
        iid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        body = await _body(request)
        inst = await _run(store.patch, iid, body)
        return _json(_public_one(inst))

    @app.post(PREFIX + "/instances/{iid}/test")
    async def test_instance(
        iid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        if await _body(request):
            raise _BadRequest("a test takes an empty body")
        try:
            return _json(await service.run_test(iid))
        except store.NotFound:
            raise _BadRequest("no such connector", 404, "not_found") from None
        except store.Refused as e:
            raise _BadRequest(str(e), 409, e.code) from None

    @app.post(PREFIX + "/instances/{iid}/capabilities")
    async def set_capabilities(
        iid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        body = await _body(request)
        if set(body) != {"read"} or not isinstance(body["read"], bool):
            raise _BadRequest("send exactly {read: true|false}")
        inst = await _run(store.set_capability, iid, body["read"])
        return _json(_public_one(inst))

    @app.delete(PREFIX + "/instances/{iid}")
    async def delete_instance(
        iid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        await _run(store.remove, iid)
        return _json({"removed": iid})
