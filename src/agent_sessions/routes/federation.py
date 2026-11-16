"""Federation routes (#1246) — pairing this instance with another BattleLab.

Operator API (a session for reads; every write also needs ``csrf_guard``, which carries the
Origin/Referer check):

* ``GET    /api/federation``                         — this instance, its peers, open pairings
* ``POST   /api/federation/invites``                 — mint a one-time invite (shown once)
* ``DELETE /api/federation/invites/{id}``            — withdraw it
* ``POST   /api/federation/pairings``                — ``{invite}``: accept another instance's
* ``POST   /api/federation/pairings/{id}/confirm``   — the short code matches
* ``POST   /api/federation/pairings/{id}/deny``
* ``PATCH  /api/federation/peers/{peer_id}``         — ``{name}``
* ``POST   /api/federation/peers/{peer_id}/revoke``

The wire (no cookie, no CSRF, no ``AUTH_MODE``):

* ``WS /ws/fed`` — the ONE door another instance uses. It is authenticated by the Noise
  handshake inside it (:mod:`federation.channel`): until that completes with a trusted key or a
  live invite, the other side gets handshake frames and nothing else. The upgrade is refused
  without reading a byte while this instance has no trusted peer and no open invite, and in any
  process that is not the store's federation owner. There is deliberately no
  federation HTTP route on the wire: the ``none``-mode middleware gives cookie-less HTTP requests
  an admin session, and a WebSocket never passes through it.
"""

from __future__ import annotations

import contextlib
import json

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from ..federation import channel as ch
from ..federation import store
from ..federation.service import Federation, FederationError

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/federation"
WS_PATH = "/ws/fed"
BODY_MAX = 16 * 1024


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _error(exc: FederationError) -> JSONResponse:
    return _json({"detail": exc.detail, "code": exc.code}, exc.status)


async def _body(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise FederationError("too_large", "request body is too large", status=413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise FederationError("too_large", "request body is too large", status=413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise FederationError("invalid", "request body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise FederationError("invalid", "request body must be a JSON object")
    return payload


class _ServerTransport:
    """An accepted Starlette WebSocket as a :class:`channel.FrameTransport`."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        try:
            await self._ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise ch.ChannelClosed() from exc

    async def recv(self) -> bytes:
        try:
            message = await self._ws.receive()
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise ch.ChannelClosed() from exc
        data = message.get("bytes")
        # A text frame, a disconnect, or a frame no Noise message can be: the connection is over.
        if message.get("type") != "websocket.receive" or not isinstance(data, bytes):
            raise ch.ChannelClosed()
        if len(data) > ch.WIRE_MAX:
            raise ch.ChannelClosed()
        return data

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()


def register(app: FastAPI, *, federation: Federation, logged_in, csrf_guard) -> None:
    def guarded(fn):
        async def run(*args, **kwargs):
            try:
                return _json(await fn(*args, **kwargs))
            except FederationError as exc:
                return _error(exc)
            except store.StoreError as exc:
                return _json({"detail": str(exc), "code": "store"}, exc.status)

        return run

    @app.get(PREFIX, dependencies=[Depends(logged_in)])
    async def federation_state() -> JSONResponse:
        return await guarded(_state)()

    async def _state() -> dict:
        return federation.snapshot()

    @app.post(f"{PREFIX}/invites", dependencies=[Depends(logged_in), Depends(csrf_guard)])
    async def federation_invite_create() -> JSONResponse:
        async def run() -> dict:
            return federation.create_invite()

        return await guarded(run)()

    @app.delete(
        f"{PREFIX}/invites/{{invite_id}}", dependencies=[Depends(logged_in), Depends(csrf_guard)]
    )
    async def federation_invite_cancel(invite_id: str) -> JSONResponse:
        async def run() -> dict:
            federation.cancel_invite(invite_id)
            return federation.snapshot()

        return await guarded(run)()

    @app.post(f"{PREFIX}/pairings", dependencies=[Depends(logged_in), Depends(csrf_guard)])
    async def federation_pairing_accept(request: Request) -> JSONResponse:
        async def run() -> dict:
            body = await _body(request)
            return federation.accept_invite(body.get("invite"))

        return await guarded(run)()

    @app.post(
        f"{PREFIX}/pairings/{{pairing_id}}/confirm",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_pairing_confirm(pairing_id: str) -> JSONResponse:
        return await guarded(federation.confirm)(pairing_id)

    @app.post(
        f"{PREFIX}/pairings/{{pairing_id}}/deny",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_pairing_deny(pairing_id: str) -> JSONResponse:
        return await guarded(federation.deny)(pairing_id)

    @app.patch(
        f"{PREFIX}/peers/{{peer_id}}", dependencies=[Depends(logged_in), Depends(csrf_guard)]
    )
    async def federation_peer_rename(peer_id: str, request: Request) -> JSONResponse:
        async def run() -> dict:
            body = await _body(request)
            return federation.rename(peer_id, body.get("name"))

        return await guarded(run)()

    @app.post(
        f"{PREFIX}/peers/{{peer_id}}/revoke",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_peer_revoke(peer_id: str) -> JSONResponse:
        return await guarded(federation.revoke)(peer_id)

    @app.websocket(WS_PATH)
    async def ws_fed(ws: WebSocket) -> None:
        # Decided BEFORE the upgrade is accepted: with nobody to talk to, the request is refused
        # as an ordinary HTTP 403 and not a byte of a handshake is read.
        if not federation.accepting():
            await ws.close()
            return
        await ws.accept()
        await federation.serve(_ServerTransport(ws))
