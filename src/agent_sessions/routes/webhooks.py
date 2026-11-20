"""``/api/webhooks`` — the webhook list (#2221 P1): list, create, read, rename, rotate, delete.

Login + CSRF + Origin like every Library write. A secret is returned ONCE, by the call that makes
it (create, rotate), and never by a read. Read `docs/invariants/webhooks.md`.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import automations_store as store
from .. import webhooks

PREFIX = "/api/webhooks"
BODY_MAX = 4096
_NO_STORE = {"Cache-Control": "no-store"}


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


def _err(e: Exception) -> JSONResponse:
    status = getattr(e, "status", 500)
    if isinstance(e, store.NotFound):
        status = 404
    elif isinstance(e, store.Conflict):
        status = 409
    return _json({"detail": str(e) or "refused"}, status)


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > BODY_MAX:
        raise webhooks.WebhookError("request too large", 413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise webhooks.WebhookError("not JSON") from None
    if not isinstance(body, dict):
        raise webhooks.WebhookError("expected an object")
    return body


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    async def _run(fn, *args, status: int = 200):
        try:
            return _json(await asyncio.to_thread(fn, *args), status)
        except (webhooks.WebhookError, store.StoreError) as e:
            return _err(e)

    @app.get(PREFIX)
    async def list_webhooks(_user: str = Depends(logged_in)) -> JSONResponse:
        return await _run(lambda: {"webhooks": webhooks.list_all()})

    @app.post(PREFIX)
    async def create_webhook(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            body = await _body(request)
            if set(body) - {"name"}:
                raise webhooks.WebhookError("a new webhook takes only a name")
        except webhooks.WebhookError as e:
            return _err(e)

        def _make() -> dict:
            pub, secret = webhooks.create(body.get("name"))
            return {"webhook": pub, "secret": secret}

        return await _run(_make, status=201)

    @app.get(PREFIX + "/{wid}")
    async def get_webhook(wid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return await _run(webhooks.get, wid)

    @app.patch(PREFIX + "/{wid}")
    async def rename_webhook(
        wid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
            if set(body) - {"name", "revision"}:
                raise webhooks.WebhookError("a change takes only name and revision")
            rev = body.get("revision")
            if rev is not None and (not isinstance(rev, int) or isinstance(rev, bool)):
                raise webhooks.WebhookError("revision must be a number")
        except webhooks.WebhookError as e:
            return _err(e)
        return await _run(lambda: webhooks.rename(wid, body.get("name"), expect_revision=rev))

    @app.post(PREFIX + "/{wid}/rotate")
    async def rotate_webhook(
        wid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return await _run(
            lambda: {"secret": webhooks.rotate(wid), "webhook": webhooks.get(wid)}, status=201
        )

    @app.delete(PREFIX + "/{wid}")
    async def delete_webhook(
        wid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return await _run(lambda: (webhooks.delete(wid), {"deleted": wid})[1])
