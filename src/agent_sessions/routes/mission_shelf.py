"""Mission shelf routes (#1740 P1a): the manifest, the verdict and the mission-bound read path.

Every route is ``logged_in``; the verdict also takes ``csrf_guard`` (which carries the
Origin/Referer check). Store work goes through :func:`missions.run_admitted`; file work through
the file panel's own bounded pool (`routes.files._run`), never the interpreter's default pool.

**The read path takes no path.** ``GET /api/missions/{id}/shelf/{item}/raw`` resolves the file
from the item row, and only when the item is THIS mission's; every read then re-proves the
mission's folders, the roots, `folder_exclusions`, no symlink and a regular file on the opened
descriptor (`mission_shelf.open_verified`), and decides the inline type from the bytes being
served (`mission_shelf.inline_type`). Streaming, ranges, the `max` cap, the raw-stream slots and
the sandbox headers are `/api/files/raw`'s own (`routes.files.stream_raw`). It is therefore
stricter than `/api/files/raw`, which is home-rooted (`docs/invariants/file-raw.md`).
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import files, mission_shelf, mission_shelf_git, missions
from . import files as files_routes

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
#: How much of a file the inline decision reads: enough for every magic number and for the text
#: document's NUL sniff (the file viewer's own 8 KiB).
_HEAD_BYTES = 8192


def _fail(e: missions.MissionError) -> JSONResponse:
    body: dict = {"detail": str(e)}
    if isinstance(e, mission_shelf.ShelfConflict):
        body["revision"] = e.revision
    return JSONResponse(body, status_code=e.status, headers=_NO_STORE)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/missions/{mission_id}/shelf")
    async def shelf_manifest(
        mission_id: str, request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        """One page of the shelf: `{revision, counts, items, next_cursor}`. Counts and file checks
        describe the whole shelf, before the filters."""
        q = request.query_params
        try:
            missions.validate_id(mission_id)
            if not q.get("cursor"):
                # Found-in-git work (git subprocesses) on a thread of its own, never inside the
                # store's admission pool; serialised per mission and never raising.
                await asyncio.to_thread(mission_shelf_git.refresh, mission_id, wait=False)
            out = await missions.run_admitted(
                lambda: mission_shelf.manifest(
                    mission_id,
                    cursor=q.get("cursor"),
                    kind=q.get("kind"),
                    source=q.get("source"),
                    verdict=q.get("verdict"),
                    found=False,
                )
            )
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.patch("/api/missions/{mission_id}/shelf/{item_id}")
    async def shelf_verdict(
        mission_id: str,
        item_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """``{expected_revision, verdict, note}`` → the item with its verdict, or 409 with the
        current revision when the shelf moved since it was read."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse(
                {"detail": "a JSON object is required"}, status_code=422, headers=_NO_STORE
            )
        try:
            out = await missions.run_admitted(
                lambda: mission_shelf.set_verdict(
                    mission_id,
                    item_id,
                    expected_revision=body.get("expected_revision"),
                    verdict=body.get("verdict"),
                    note=body.get("note", ""),
                )
            )
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.get("/api/missions/{mission_id}/shelf/{item_id}/diff")
    async def shelf_diff(
        mission_id: str, item_id: str, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        """A found code change's files and their patches (#1740 P1b). Takes no path: the patches
        were taken at collection, inside the mission's boundary, and are served as JSON text."""
        try:
            out = await missions.run_admitted(lambda: mission_shelf_git.diff(mission_id, item_id))
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.get("/api/missions/{mission_id}/shelf/{item_id}/raw")
    async def shelf_raw(
        mission_id: str, item_id: str, request: Request, _user: str = Depends(logged_in)
    ) -> Response:
        """The bytes of one file item, mission-bound (see the module docstring)."""
        headers = files_routes._RAW_HEADERS
        download = request.query_params.get("download") == "1"
        try:
            row, bound = await missions.run_admitted(
                lambda: mission_shelf.read_plan(mission_id, item_id)
            )
        except missions.MissionError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=headers) from None
        try:
            raw: files.RawFile = await files_routes._run(
                mission_id,
                files.open_raw_with,
                lambda: mission_shelf.open_verified(row["ref"], bound),
                orphan=files_routes._close_in_background,
            )
        except files.RawBusy as e:
            raise HTTPException(
                status_code=503,
                detail=str(e),
                headers={**headers, "Retry-After": str(files_routes._RAW_RETRY_AFTER_S)},
            ) from None
        except files.FsError as e:
            detail = (
                "the file is no longer on disk"
                if e.status == 404
                else "the file is outside this mission's project folders"
                if e.status == 403
                else "the file is not a regular file"
            )
            raise HTTPException(status_code=e.status, detail=detail, headers=headers) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not read the file", headers=headers
            ) from None
        # THIS frame owns `raw` until `stream_raw` takes it: anything raised here closes it. A busy
        # panel pool is a 503 like any other raw refusal; a cancellation stays a cancellation.
        try:
            head = await files_routes._run(mission_id, raw.read, 0, _HEAD_BYTES)
            inline = mission_shelf.inline_type(row, raw.path, head, raw.size)
        except files.FilesBusy as e:
            files_routes._close_in_background(raw)
            raise HTTPException(
                status_code=503,
                detail=str(e),
                headers={**headers, "Retry-After": str(files_routes._RAW_RETRY_AFTER_S)},
            ) from None
        except Exception:
            files_routes._close_in_background(raw)
            raise HTTPException(
                status_code=500, detail="could not read the file", headers=headers
            ) from None
        except BaseException:
            files_routes._close_in_background(raw)
            raise
        return await files_routes.stream_raw(
            request, raw, key=mission_id, download=download, inline_type=inline
        )
