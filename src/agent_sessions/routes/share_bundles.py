"""Share bundles (#1714): export a selection to one file, import one through a bound preview.

All routes are authenticated; every POST is CSRF- and Origin-guarded; every response is
`no-store`. The upload is a raw `application/octet-stream` body streamed against the format's
size bound (no multipart parser, nothing spooled to disk), and every store read and write runs
off the event loop. No passphrase or secret value is accepted or returned (#1714 P5 is not
approved).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response

from ..gallery import contract as gallery_contract
from ..gallery import gallery_client
from ..playbooks import store
from ..sharebundle import format as fmt
from ..sharebundle import kinds, service, state

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
MAX_JSON_BODY = 256 * 1024
PREFIX = "/api/share"


def _response(value: object, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers=NO_STORE)


async def _read(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise service.ShareError(
            f"the file is larger than {limit} bytes", status=413, reason="too_large"
        )
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise service.ShareError(
                f"the file is larger than {limit} bytes", status=413, reason="too_large"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _json(request: Request, allowed: set[str], required: set[str]) -> dict:
    raw = await _read(request, MAX_JSON_BODY)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=gallery_contract._pairs,
            parse_float=gallery_contract._no_float,
            parse_constant=gallery_contract._no_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise service.ShareError("expected unambiguous JSON", status=422) from None
    if not isinstance(value, dict) or not required <= set(value) <= allowed:
        raise service.ShareError("unexpected or missing request fields", status=422)
    return value


def _refusal(exc: BaseException) -> JSONResponse | None:
    """The answer for a refusal, or None for an exception that isn't one."""
    if isinstance(exc, fmt.Refused):
        return _response({"detail": exc.detail, "reason": exc.reason}, 422)
    if isinstance(exc, store.StoreError):
        return _response({"detail": exc.detail, **exc.extra}, exc.status)
    if isinstance(exc, state.Busy):
        return _response({"detail": str(exc)}, 503)
    if isinstance(exc, state.StateError | OSError):
        # Never "nothing changed": a confirm can fail after some copies were published.
        # Confirming the same preview again is safe — it never makes a second copy.
        return _response(
            {
                "detail": "Share storage is unavailable. Check your library; confirming the "
                "same preview again never makes a second copy."
            },
            503,
        )
    if isinstance(exc, gallery_contract.GalleryError | kinds.ItemRefused):
        return _response({"detail": str(exc)}, 422)
    return None


async def _guarded(work):
    """Run a route's work and answer its refusals INSIDE the route, so every response passes
    through the app's own middleware (security headers included) like any other answer."""
    try:
        return await work
    except Exception as exc:
        answer = _refusal(exc)
        if answer is None:
            raise
        return answer


def register(app, *, logged_in, csrf_guard) -> None:
    slots = asyncio.Semaphore(2)

    @contextlib.asynccontextmanager
    async def admitted():
        try:
            await asyncio.wait_for(slots.acquire(), 0.1)
        except TimeoutError:
            raise service.ShareError("sharing is busy; try again", status=503) from None
        try:
            yield
        finally:
            slots.release()

    async def call(fn, *args):
        async with admitted():
            return await gallery_client._finish(fn, *args)

    @app.middleware("http")
    async def no_store(request, handler):
        # Headers only: it never builds a response itself (refusals are answered in the route,
        # inside every other middleware), so nothing here can skip the security headers.
        result = await handler(request)
        if request.url.path.startswith(PREFIX + "/"):
            result.headers.update(NO_STORE)
        return result

    @app.get(PREFIX + "/catalog", dependencies=[Depends(logged_in)])
    async def share_catalog(request: Request):
        return await _guarded(_catalog())

    async def _catalog():
        return _response(await call(service.catalog))

    @app.get(PREFIX + "/imports", dependencies=[Depends(logged_in)])
    async def share_imports(request: Request):
        return await _guarded(_imports())

    async def _imports():
        return _response({"imports": await call(service.imports)})

    @app.post(PREFIX + "/export", dependencies=[Depends(csrf_guard)])
    async def share_export(request: Request):
        return await _guarded(_export(request))

    async def _export(request: Request):
        value = await _json(request, {"items", "source", "include_secrets"}, {"items"})
        if value.get("include_secrets", False) is not False:
            raise service.ShareError(
                "secret values are never exported; secrets travel as definitions only",
                status=422,
            )
        name, data = await call(service.export, value["items"], value.get("source"))
        return Response(
            data,
            media_type="application/x-tar",
            headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post(PREFIX + "/import/preview", dependencies=[Depends(csrf_guard)])
    async def share_preview(request: Request):
        return await _guarded(_preview(request))

    async def _preview(request: Request):
        # Admission before the body is read: at most two bundles are ever held in memory.
        async with admitted():
            data = await _read(request, fmt.MAX_BUNDLE_BYTES)
            return _response(await gallery_client._finish(service.preview, data))

    @app.post(PREFIX + "/import", dependencies=[Depends(csrf_guard)])
    async def share_import(request: Request):
        return await _guarded(_import(request))

    async def _import(request: Request):
        value = await _json(request, {"token", "selection", "settle"}, {"token", "selection"})
        return _response(
            await call(service.confirm, value["token"], value["selection"], value.get("settle"))
        )
