"""The operator's profile routes (#1931): the avatar shown beside their messages.

``GET`` reads it back for the operator's own panes; ``PUT`` (raw image body) and ``DELETE`` change
it, both behind the operator cookie and the CSRF/Origin guard. Visitors of an API share link read
the same bytes through ``GET /api/public/avatar`` (``routes/public_api.py``), never through here.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import profile_avatar


def avatar_response(avatar: profile_avatar.Avatar | None) -> Response:
    """The one shape both the operator's and the visitor's read take."""
    if avatar is None:
        raise HTTPException(
            status_code=404, detail="no avatar", headers={"Cache-Control": "no-store"}
        )
    return Response(
        content=avatar.data, media_type=avatar.mime, headers=profile_avatar.response_headers()
    )


async def read_capped(request: Request, cap: int) -> bytes:
    """The request body, refused with 413 as soon as it passes ``cap`` — never read whole first."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise HTTPException(status_code=413, detail=f"image too large (max {cap // 1024} KiB)")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(status_code=413, detail=f"image too large (max {cap // 1024} KiB)")
    return bytes(buf)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/profile/avatar")
    async def get_avatar(_user: str = Depends(logged_in)) -> Response:
        return avatar_response(await asyncio.to_thread(profile_avatar.read))

    @app.put("/api/profile/avatar")
    async def put_avatar(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        data = await read_capped(request, profile_avatar.MAX_BYTES)
        try:
            version = await asyncio.to_thread(profile_avatar.write, data)
        except profile_avatar.AvatarError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"version": version}, headers={"Cache-Control": "no-store"})

    @app.delete("/api/profile/avatar")
    async def delete_avatar(
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            await asyncio.to_thread(profile_avatar.remove)
        except profile_avatar.AvatarError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"version": None}, headers={"Cache-Control": "no-store"})
