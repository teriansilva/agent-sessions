"""The operator's profile routes (#1931): the avatar shown beside their messages.

``GET`` reads it back for the operator's own panes; ``POST`` (a multipart ``file`` field) and
``DELETE`` change it, both behind the operator cookie and the CSRF/Origin guard.

**Multipart, not a raw image body.** The first cut took ``PUT`` with ``Content-Type: image/webp``,
and a WAF in front of the app (OWASP CRS 920420, "Request content type is not allowed by policy")
refused it before it reached the app — the operator saw only "forbidden". ``multipart/form-data``
is the shape every other upload here uses (``/api/upload``, ``/api/files/upload``): edge WAFs pass
it, and the Home Free tunnel already carries it (``e2e/appmode-avatar.spec.ts``).

Visitors of an API share link read the same bytes through ``GET /api/public/avatar``
(``routes/public_api.py``), never through here.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from python_multipart.exceptions import FormParserError
from starlette.formparsers import MultiPartException

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


#: Multipart framing around a MAX_BYTES image: boundary lines and part headers, generously.
_FORM_OVERHEAD = 16 * 1024


async def read_avatar_form(request: Request) -> bytes:
    """The ``file`` part's bytes, refused with 413 past ``MAX_BYTES``: on the (required) declared
    length before any parsing, and on the part itself. One file, at most one field."""
    cap = profile_avatar.MAX_BYTES
    too_large = HTTPException(status_code=413, detail=f"image too large (max {cap // 1024} KiB)")
    # A declared length is required: Starlette spools a multipart FILE part to disk before this
    # code sees it, so the length check below is the bound on what can land there. A browser's
    # FormData fetch and the Home Free tunnel (httpx, a whole buffered body) both always send one.
    declared = request.headers.get("content-length")
    if not (declared and declared.isdigit()):
        raise HTTPException(status_code=411, detail="Content-Length required")
    if int(declared) > cap + _FORM_OVERHEAD:
        raise too_large
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="send the image as multipart/form-data")
    # Malformed framing (a body that only CLAIMS to be multipart) is the client's fault: a typed
    # 400, never the parser's exception escaping as a 500 (Hermes on #1940). Starlette raises
    # its own MultiPartException for limits; python-multipart's parse errors are ValueErrors.
    try:
        async with request.form(max_files=1, max_fields=1, max_part_size=cap + 1) as form:
            part = form.get("file")
            if part is None or isinstance(part, str):
                raise HTTPException(status_code=422, detail="no file in the form")
            data = await part.read(cap + 1)
    except (MultiPartException, FormParserError, ValueError):
        raise HTTPException(status_code=400, detail="malformed multipart body") from None
    if len(data) > cap:
        raise too_large
    return data


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/profile/avatar")
    async def get_avatar(_user: str = Depends(logged_in)) -> Response:
        return avatar_response(await asyncio.to_thread(profile_avatar.read))

    @app.post("/api/profile/avatar")
    async def post_avatar(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        data = await read_avatar_form(request)
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
