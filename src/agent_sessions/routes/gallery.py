"""Authenticated gallery inspection/import and inert local skills; all writes CSRF guarded."""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from ..gallery import contract, gallery_client, library, skills
from ..playbooks import store

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
MAX_BODY = 4 * 1024 * 1024


async def body(request, fields):
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            raise store.StoreError("Gallery request is too large.", status=413)
        chunks.append(chunk)
    try:
        value = json.loads(
            b"".join(chunks),
            object_pairs_hook=contract._pairs,
            parse_float=contract._no_float,
            parse_constant=contract._no_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise contract.GalleryError("Expected unambiguous JSON.") from None
    if not isinstance(value, dict) or set(value) != fields:
        raise contract.GalleryError("Unexpected or missing gallery request fields.")
    return value


def response(value, status=200):
    return JSONResponse(value, status_code=status, headers=NO_STORE)


def register(app, *, logged_in, csrf_guard):
    slots = asyncio.Semaphore(4)

    async def call(fn, *args, **kwargs):
        try:
            await asyncio.wait_for(slots.acquire(), 0.1)
        except TimeoutError:
            raise store.StoreError("The gallery is busy; try again.", status=503) from None
        try:
            return await gallery_client._finish(lambda: fn(*args, **kwargs))
        finally:
            slots.release()

    @app.middleware("http")
    async def errors(request, handler):
        if not request.url.path.startswith(("/api/gallery", "/api/library/skills")):
            return await handler(request)
        try:
            result = await handler(request)
        except store.StoreError as exc:
            return response({"detail": exc.detail, **exc.extra}, exc.status)
        except (ValueError, UnicodeError, TypeError, KeyError, RecursionError) as exc:
            return response({"detail": str(exc) or "Gallery validation refused."}, 422)
        except OSError:
            return response(
                {"detail": "Gallery storage is unavailable. Check the operation before retrying."},
                503,
            )
        result.headers.update(NO_STORE)
        return result

    @app.get("/api/gallery", dependencies=[Depends(logged_in)])
    async def catalog(request: Request):
        return response(await call(library.catalog))

    @app.post("/api/gallery/refresh", dependencies=[Depends(csrf_guard)])
    async def refresh(request: Request):
        try:
            await asyncio.wait_for(slots.acquire(), 0.1)
        except TimeoutError:
            raise store.StoreError("The gallery is busy; try again.", status=503) from None
        try:
            await gallery_client.refresh()
        finally:
            slots.release()
        return response(await call(library.catalog))

    @app.get("/api/gallery/content/{digest}", dependencies=[Depends(logged_in)])
    async def inspect(request: Request):
        return response(
            await call(
                library.inspect,
                request.path_params["digest"],
                request.query_params.get("local_id", "local-copy"),
            )
        )

    @app.post("/api/gallery/import", dependencies=[Depends(csrf_guard)])
    async def import_copy(request: Request):
        value = await body(request, {"kind", "id", "digest", "operation", "acknowledge"})
        return response(
            await call(
                library.import_copy,
                value["kind"],
                value["id"],
                value["digest"],
                value["operation"],
                value["acknowledge"],
            )
        )

    @app.get("/api/gallery/operations/{operation}", dependencies=[Depends(logged_in)])
    async def operation(request: Request):
        return response(await call(library.operation, request.path_params["operation"]))

    @app.post("/api/gallery/compare", dependencies=[Depends(csrf_guard)])
    async def compare(request: Request):
        value = await body(request, {"kind", "id", "digest"})
        return response(await call(library.compare, value["kind"], value["id"], value["digest"]))

    @app.post("/api/gallery/update", dependencies=[Depends(csrf_guard)])
    async def update(request: Request):
        value = await body(
            request,
            {"kind", "id", "new_id", "digest", "comparison", "files", "operation", "acknowledge"},
        )
        return response(
            await call(
                library.apply_update,
                value["kind"],
                value["id"],
                value["digest"],
                value["comparison"],
                value["files"],
                value["operation"],
                value["acknowledge"],
                new_id=value["new_id"],
            )
        )

    @app.get("/api/library/skills", dependencies=[Depends(logged_in)])
    async def skill_list(request: Request):
        return response(await call(skills.listing))

    @app.get("/api/library/skills/{id}", dependencies=[Depends(logged_in)])
    async def skill_detail(request: Request):
        return response(await call(skills.get, request.path_params["id"]))

    @app.post("/api/library/skills/{id}/copies", dependencies=[Depends(csrf_guard)])
    async def skill_save(request: Request):
        value = await body(request, {"revision", "origin", "files", "new_id", "operation"})
        store.revision(value["revision"])
        return response(
            await call(
                library.edit_skill_copy,
                request.path_params["id"],
                value["new_id"],
                value["revision"],
                value["origin"],
                value["files"],
                value["operation"],
            )
        )
