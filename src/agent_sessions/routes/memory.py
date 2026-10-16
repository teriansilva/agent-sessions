"""Shared project memory — the Memory page's routes (#1441 P2).

Reads need a login; every mutation also needs the CSRF token and the app's Origin. A scope is only
ever selected by its typed project id from the path, validated before any path is built. Every
response is no-store: notes are agent-written text and may be private. Blocking store work runs
off the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import memory, template_secrets
from .templates import _err, _json_object
from .upload import NO_STORE


async def _run(fn: Callable, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except memory.MemoryStoreError as exc:
        raise _err(exc.status, str(exc)) from None
    except template_secrets.RedactionUnavailable as exc:
        raise _err(503, f"secret check unavailable — nothing was taken in ({exc})") from None


def _ok(payload, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _qid(raw: str) -> int:
    if not raw.isdigit() or len(raw) > 12:
        raise _err(404, "no such item")
    return int(raw)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/memory")
    async def memory_overview(_user: str = Depends(logged_in)) -> JSONResponse:
        return _ok({"projects": await _run(memory.overview)})

    @app.get("/api/memory/{pid}")
    async def memory_view(pid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return _ok(await _run(memory.view, pid))

    @app.post("/api/memory/{pid}/ingest")
    async def memory_ingest(
        pid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # The page asks for a pass when it opens, so what it shows is never 30 s stale.
        await _run(memory.view, pid)  # 404 for anything that is not a live project
        return _ok(await _run(memory.ingest, pid))

    @app.get("/api/memory/{pid}/import")
    async def memory_import_preview(pid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # The agent memory stores found for the project's folders (#1471). Read only.
        return _ok(await _run(memory.import_preview, pid))

    @app.post("/api/memory/{pid}/import")
    async def memory_import(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Opaque source ids from a fresh preview — a path is never accepted (#1471).
        body = await _json_object(request)
        if set(body) != {"sources"}:
            raise _err(422, 'send {"sources": [id, …]}')
        return _ok(await _run(memory.import_notes, pid, body["sources"]))

    @app.put("/api/memory/{pid}/enabled")
    async def memory_enable(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json_object(request)
        if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
            raise _err(422, 'send {"enabled": true|false}')
        await _run(memory.set_enabled, pid, body["enabled"])
        return _ok({"enabled": body["enabled"]})

    @app.patch("/api/memory/{pid}/notes/{slug}")
    async def memory_edit_note(
        pid: str,
        slug: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        await _run(memory.update_note, pid, slug, await _json_object(request))
        return _ok({"slug": slug})

    @app.delete("/api/memory/{pid}/notes/{slug}")
    async def memory_delete_note(
        pid: str, slug: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        await _run(memory.delete_note, pid, slug)
        return Response(status_code=204, headers=NO_STORE)

    @app.post("/api/memory/{pid}/clear")
    async def memory_clear(
        pid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        await _run(memory.view, pid)  # 404 for anything that is not a live project
        await _run(memory.clear, pid)
        return Response(status_code=204, headers=NO_STORE)

    @app.get("/api/memory/{pid}/export")
    async def memory_export(pid: str, _user: str = Depends(logged_in)) -> Response:
        data = await _run(memory.export_zip, pid)
        return Response(
            data,
            media_type="application/zip",
            headers={**NO_STORE, "Content-Disposition": f'attachment; filename="memory-{pid}.zip"'},
        )

    @app.patch("/api/memory/{pid}/quarantine/{qid}")
    async def memory_release(
        pid: str,
        qid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json_object(request)
        if set(body) != {"text"} or not isinstance(body["text"], str):
            raise _err(422, 'send {"text": "…"}')
        slug = await _run(memory.release_quarantined, pid, _qid(qid), body["text"])
        return _ok({"slug": slug})

    @app.delete("/api/memory/{pid}/quarantine/{qid}")
    async def memory_drop_quarantined(
        pid: str, qid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        await _run(memory.delete_quarantined, pid, _qid(qid))
        return Response(status_code=204, headers=NO_STORE)
