"""The connector API (#1750): add, configure, test, enable and remove a connector instance.

Login on every route; CSRF and Origin on every mutation (`csrf_guard`); strict, bounded JSON
bodies; fixed operations only. No response ever carries a token or an envelope (`store.public`),
and every response is ``no-store``. An unreadable store refuses every route (503) and is never
rewritten.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import connector_http, notify_routes, template_secrets
from ..connectors import candidates, service, store

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
    {
        "origin_needs_token", "needs_test", "needs_token", "needs_origin", "too_many", "stale",
        "recipients_unbound", "not_private", "identity_unchecked",
    }
)  # fmt: skip


def _instance(iid: str) -> dict:
    return store._find(store.read(), iid)


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
        notify_routes.poke()  # a new generation turns a route off; its copies settle (#1841)
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
            out = await service.run_test(iid)
            notify_routes.poke()
            return _json(out)
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
        if set(body) == {"read"} and isinstance(body["read"], bool):
            inst = await _run(store.set_capability, iid, body["read"])
        elif set(body) == {"send"} and isinstance(body["send"], bool):
            # A messenger's send switch (#1832): on needs a test and recipients bound to this
            # bot and origin; off always succeeds, at once.
            inst = await _run(store.set_send, iid, body["send"])
        else:
            raise _BadRequest("send exactly {read: true|false} or {send: true|false}")
        return _json(_public_one(inst))

    @app.get(PREFIX + "/instances/{iid}/recipients/candidates")
    async def recipient_candidates(iid: str, _user: str = Depends(logged_in)):
        """What the bot can reach now (#1832): read from the service WITHOUT the store lock, and
        kept as the snapshot a save is checked against."""
        try:
            return _json(await candidates.list_candidates(iid))
        except store.NotFound:
            raise _BadRequest("no such connector", 404, "not_found") from None
        except store.Refused as e:
            raise _BadRequest(str(e), 409, e.code) from None
        except connector_http.ConnectorError as e:
            raise _BadRequest(e.detail, 502, e.code) from None

    @app.put(PREFIX + "/instances/{iid}/recipients")
    async def put_recipients(
        iid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        """The allowlist (#1832): ids only. Each must be an eligible candidate of the snapshot
        the server took, at the instance's current generation; labels are the server's. An email
        connector (#1836) takes typed addresses instead: ``{addresses: [{address, label}],
        generation}``, each checked by `store.set_addresses`."""
        body = await _body(request)
        if set(body) == {"addresses", "generation"}:
            inst = await _run(store.set_addresses, iid, body["addresses"], body["generation"])
        elif set(body) == {"ids"}:
            inst = await _run(store.set_recipients, iid, body["ids"], candidates.snapshot(iid))
        else:
            raise _BadRequest("send exactly {ids: [...]} or {addresses: [...], generation}")
        notify_routes.poke()
        return _json(_public_one(inst))

    @app.post(PREFIX + "/instances/{iid}/notify")
    async def set_notify(
        iid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        """A messenger's notify route (#1841): every field validated, refusals in words. On
        pins the destination's identity from a fresh read (Telegram's candidate snapshot, the
        Matrix room's members) and sets `since` to now."""
        settings = await _run(store.check_notify_body, await _body(request))
        inst = await _run(_instance, iid)
        identity = None
        if settings["enabled"]:
            try:
                identity = await service.notify_identity(inst, settings["destination"])
            except store.Refused as e:
                raise _BadRequest(str(e), 409 if e.code in _CONFLICTS else 422, e.code) from None
        out = await _run(
            functools.partial(
                store.set_notify, iid, settings, generation=inst["generation"], identity=identity
            )
        )
        notify_routes.poke()
        return _json(_public_one(out))

    @app.get(PREFIX + "/instances/{iid}/deliveries")
    async def get_deliveries(iid: str, _user: str = Depends(logged_in)):
        """The route's newest deliveries (#1841): states, times, labels and links — never the
        message text. 503 in words when the route ledger can't be read."""
        await _run(_instance, iid)
        try:
            rows = await asyncio.to_thread(notify_routes.deliveries, iid)
        except notify_routes.StoreUnreadable:
            raise _BadRequest(
                "the notification ledger can't be read, so nothing is sent until it can",
                503,
                "ledger_unreadable",
            ) from None
        return _json({"deliveries": rows})

    @app.delete(PREFIX + "/instances/{iid}")
    async def delete_instance(
        iid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        await _run(store.remove, iid)
        notify_routes.poke()  # its delivered copies settle `retract_lost` on the next pass
        return _json({"removed": iid})
