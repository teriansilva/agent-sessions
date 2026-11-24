"""Shared artifact repositories (#2347 P1): the operator API, ``/api/repos…``.

The same surface as the artifact operator routes: every route is ``logged_in``, every mutation
also takes ``csrf_guard`` (CSRF + Origin), and store work runs off the event loop. A refusal is
``{detail: <words>, code: <code>}`` with the store's status. The repository's items are served by
the artifact hosting routes (``/artifacts/i/<item>/``), never here.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import artifact_store, repo_store

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _fail(e: Exception) -> JSONResponse:
    if isinstance(e, artifact_store.ArtifactRefused):
        words = repo_store.WORDS.get(e.code, repo_store.WORDS["bad_request"])
        return JSONResponse(
            {"detail": words, "code": e.code}, status_code=e.status, headers=_NO_STORE
        )
    return JSONResponse(
        {"detail": "the artifact store is unavailable", "code": "store_unavailable"},
        status_code=503,
        headers=_NO_STORE,
    )


def _bad(words: str) -> JSONResponse:
    return JSONResponse(
        {"detail": words, "code": "bad_request"}, status_code=422, headers=_NO_STORE
    )


async def _body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


_ERRORS = (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/repos")
    async def repos_list(_user: str = Depends(logged_in)) -> JSONResponse:
        try:
            repos = await asyncio.to_thread(repo_store.list_repos)
        except _ERRORS as e:
            return _fail(e)
        return JSONResponse(
            {"repositories": repos, "quota_default": repo_store.QUOTA_DEFAULT}, headers=_NO_STORE
        )

    @app.post("/api/repos")
    async def repos_create(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _body(request)
        if body is None or set(body) - {"name", "description"}:
            return _bad("{name, description?} is required")
        try:
            out = await asyncio.to_thread(
                repo_store.create, body.get("name"), body.get("description")
            )
        except _ERRORS as e:
            return _fail(e)
        return JSONResponse(out, status_code=201, headers=_NO_STORE)

    @app.get("/api/repos/{rid}")
    async def repos_get(rid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            out = await asyncio.to_thread(repo_store.get, rid)
        except _ERRORS as e:
            return _fail(e)
        if out is None:
            return _fail(artifact_store.ArtifactRefused("unknown_repository", status=404))
        return JSONResponse(out, headers=_NO_STORE)

    @app.patch("/api/repos/{rid}")
    async def repos_update(
        rid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _body(request)
        if body is None or not body or set(body) - {"name", "description"}:
            return _bad("{name?, description?} is required")
        try:
            out = await asyncio.to_thread(
                lambda: repo_store.update(
                    rid, name=body.get("name"), description=body.get("description")
                )
            )
        except _ERRORS as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.delete("/api/repos/{rid}")
    async def repos_delete(
        rid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            gone = await asyncio.to_thread(repo_store.delete, rid)
        except _ERRORS as e:
            return _fail(e)
        if not gone:
            return _fail(artifact_store.ArtifactRefused("unknown_repository", status=404))
        return JSONResponse({"deleted": rid}, headers=_NO_STORE)

    @app.get("/api/repos/{rid}/items")
    async def repos_items(
        rid: str, request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        try:
            out = await asyncio.to_thread(
                repo_store.items, rid, cursor=request.query_params.get("cursor") or None
            )
        except _ERRORS as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.post("/api/repos/{rid}/items")
    async def repos_add(
        rid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """``{artifact_id, revision?, item_id?}``: an existing artifact's revision (the latest by
        default) becomes a new item, or a new revision of ``item_id``. No byte is copied."""
        body = await _body(request)
        if (
            body is None
            or "artifact_id" not in body
            or set(body)
            - {
                "artifact_id",
                "revision",
                "item_id",
            }
        ):
            return _bad("{artifact_id, revision?, item_id?} is required")
        try:
            out = await asyncio.to_thread(
                lambda: repo_store.add_artifact(
                    rid,
                    body.get("artifact_id"),
                    revision=body.get("revision"),
                    item_id=body.get("item_id"),
                )
            )
        except _ERRORS as e:
            return _fail(e)
        return JSONResponse(out, status_code=201, headers=_NO_STORE)

    @app.delete("/api/repos/{rid}/items/{iid}")
    async def repos_remove(
        rid: str, iid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            gone = await asyncio.to_thread(repo_store.remove_item, rid, iid)
        except _ERRORS as e:
            return _fail(e)
        if not gone:
            return _fail(artifact_store.ArtifactRefused("unknown_item", status=404))
        return JSONResponse({"removed": iid}, headers=_NO_STORE)
