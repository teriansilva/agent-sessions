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

Sharing an API session with a peer (#2054; to suggest or send as well, #2057) or a terminal
session (#2056; view only, always). The owner's side:

* ``GET    /api/federation/grants?session=<key>``    — the live grants (of one session)
* ``GET    /api/federation/grants?peer=<peer_id>``   — the live grants to one peer, with titles
* ``POST   /api/federation/grants``                  — ``{session_key, peer_id, level, expires_in,
  accept_risk}``; ``level`` is ``view`` unless named, ``send`` needs ``accept_risk: true``, and
  a terminal session takes ``view`` only
* ``PATCH  /api/federation/grants/{grant_id}``       — ``{revision, expires_in?, level?,
  accept_risk?}``
* ``POST   /api/federation/grants/{grant_id}/revoke``

A revoke or a change that cannot get the grant's fence (a delivery under it is in flight) commits
nothing and answers ``503 busy``; so does a peer revoke.

What a peer suggests (#2057). Only these routes decide, and only an operator session reaches them:

* ``GET    /api/federation/suggestions?session=<key>`` — suggestions with their text
* ``POST   /api/federation/suggestions/{id}/approve`` — ``{text?}``: the operator may edit first
* ``POST   /api/federation/suggestions/{id}/reject``

The receiving side (reads only; each read is a verb on the owner's live channel, and the owner
checks its grant for that call):

* ``GET    /api/federation/remote``                  — what trusted peers share with this instance
* ``GET    /api/federation/remote/{peer_id}/{grant_id}/snapshot``
* ``GET    /api/federation/remote/{peer_id}/{grant_id}/events?after=&limit=&wait=``
* ``GET    /api/federation/remote/{peer_id}/{grant_id}/history?before=``
* ``GET    /api/federation/remote/{peer_id}/{grant_id}/frame?have=&wait=`` — a shared TERMINAL
  session's current screen as a redacted text frame (#2056)

Writing to a session a peer shares (#2057; the owner decides on every call, whatever the mirror
says). ``client_key`` is the browser's retry key, a UUID, passed through unchanged and never
minted here:

* ``POST   /api/federation/remote/{peer_id}/{grant_id}/suggest`` — ``{client_key, text, actor?}``
* ``POST   /api/federation/remote/{peer_id}/{grant_id}/turns``   — ``{client_key, text, actor?,
  expected_revision?}``
* ``GET    /api/federation/remote/{peer_id}/{grant_id}/suggestions``
* ``POST   /api/federation/remote/{peer_id}/{grant_id}/suggestions/{id}/withdraw``

No channel answers ``503 peer_offline``. A refusal by the owner to a READ is re-resolved once (the
mirror is refreshed and the call repeated under the current revision); a grant that is still
refused, or no longer listed, answers ``404``. A refused WRITE (suggest, turns, withdraw) is
never repeated: it answers ``404`` at once and the mirror is refreshed in the background. What a
peer returns is validated (``federation/remote.py``) and passed on as data; it is never a path, a
key or HTML here.

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

import asyncio
import contextlib
import json
import math
import uuid

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from .. import chat_store
from ..federation import channel as ch
from ..federation import frames, grants, identity, inputs, notices, remote, sharing, store
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


#: A refused read of a remote session as an HTTP status. ``no_grant`` is 404 whatever the owner's
#: reason was (it does not say); everything not listed is the peer failing to answer usefully.
_READ_STATUS = {
    sharing.NO_GRANT: 404,
    "peer_offline": 503,
    "unavailable": 503,
    "busy": 429,
    "conflict": 409,
    "invalid": 422,
    "timeout": 504,
    "held": 409,
    "not_found": 404,
    "too_large": 413,
}
_READ_DETAIL = {
    sharing.NO_GRANT: "this session is not shared with this instance",
    "peer_offline": "the instance that shares this session is not connected",
    "unavailable": "the session can't be shown right now — try again",
    "busy": "the other instance is busy — try again",
    "conflict": "the session's history changed — read it again",
    "invalid": "that request isn't valid for this session",
    "timeout": "the other instance did not answer in time — send it again to see the outcome",
    "held": "that session is part of a mission right now — you can watch, not write",
    "not_found": "no such suggestion",
    "too_large": "that message is too long to send to another instance",
}


def _read_error(exc: remote.ReadError) -> FederationError:
    status = _READ_STATUS.get(exc.code, 502)
    code = exc.code if exc.code in _READ_STATUS else "bad_answer"
    detail = _READ_DETAIL.get(code, "the other instance sent an answer this one refused")
    return FederationError(code, detail, status=status)


def _remote_ids(peer_id: str, grant_id: str) -> None:
    """Shapes first: anything else is not a remote session this instance could hold."""
    if not identity.PEER_ID_RE.fullmatch(peer_id) or not grants.valid_grant_id(grant_id):
        raise FederationError("no_grant", _READ_DETAIL[sharing.NO_GRANT], status=404)


def _message(body: dict, *allowed: str) -> dict:
    """The arguments of ``input.suggest`` / ``input.send`` from an operator's request. Shapes
    only: the owner validates again and decides. ``client_key`` is passed on exactly as the
    browser sent it; this instance never mints one."""
    if set(body) - {"client_key", "text", "actor", *allowed}:
        raise FederationError("invalid", "unknown field")
    client_key, text = body.get("client_key"), body.get("text")
    try:
        canonical = isinstance(client_key, str) and str(uuid.UUID(client_key)) == client_key
    except ValueError:
        canonical = False
    if not canonical:
        raise FederationError("invalid", "client_key must be a UUID in its canonical form")
    if not isinstance(text, str) or not text.strip():
        raise FederationError("invalid", "text is required")
    if len(text) > inputs.MAX_TEXT:
        raise FederationError("invalid", "that message is too long")
    out: dict = {"client_key": client_key, "text": text}
    actor = body.get("actor")
    if actor is not None:
        if not isinstance(actor, str) or len(actor) > 4 * store.NAME_MAX:
            raise FederationError("invalid", "actor must be a short label")
        out["actor"] = actor
    return out


def _query_int(raw: str | None, name: str, *, default: int, low: int, high: int) -> int:
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 18 or not low <= int(raw) <= high:
        raise FederationError("invalid", f"{name} is out of range")
    return int(raw)


def _query_wait(raw: str | None) -> float:
    if raw is None:
        return 0.0
    try:
        wait = float(raw)
    except ValueError:
        raise FederationError("invalid", "wait must be a number of seconds") from None
    if not math.isfinite(wait) or not 0 <= wait <= sharing.WAIT_MAX_S:
        raise FederationError("invalid", f"wait must be 0-{int(sharing.WAIT_MAX_S)} seconds")
    return wait


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
    def guarded(fn, status: int = 200):
        async def run(*args, **kwargs):
            try:
                return _json(await fn(*args, **kwargs), status)
            except FederationError as exc:
                return _error(exc)
            except grants.GrantError as exc:
                return _json({"detail": exc.detail, "code": exc.code}, exc.status)
            except inputs.InputError as exc:
                payload = {"detail": exc.detail, "code": exc.code}
                if exc.state is not None:
                    payload["state"] = exc.state  # the decision that won
                return _json(payload, exc.status)
            except remote.ReadError as exc:
                return _error(_read_error(exc))
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

    # ── grants: what this instance shares with a peer (#2054) ─────────
    def _grant_view(grant: store.Grant, peers: dict[str, store.Peer]) -> dict:
        peer = peers.get(grant.peer_id)
        return {**grants.public(grant), "peer_name": peer.name if peer else ""}

    async def _changed(grant: store.Grant) -> dict:
        """After a grant change: tell the peer (a hint, best effort; on the event loop, where
        the channel lives), then answer the operator."""
        federation.notify_share_changed(grant.peer_id)
        # A grant that ended or was lowered withdrew its pending suggestions in the same
        # transaction: their bell rows go now.
        federation.background(asyncio.to_thread(notices.retract, federation.store))

        def answer() -> dict:
            peers = {p.peer_id: p for p in federation.store.list_peers()}
            return {
                "grant": _grant_view(grant, peers),
                "generation": federation.store.generation(),
            }

        return await asyncio.to_thread(answer)

    @app.get(f"{PREFIX}/grants", dependencies=[Depends(logged_in)])
    async def federation_grants(session: str = "", peer: str = "") -> JSONResponse:
        def read() -> dict:
            if peer and not identity.PEER_ID_RE.fullmatch(peer):
                raise FederationError("not_found", "unknown peer", status=404)
            rows = grants.listing(federation.store, session or None)
            peers = {p.peer_id: p for p in federation.store.list_peers()}
            views = [_grant_view(g, peers) for g in rows if not peer or g.peer_id == peer]
            if peer:
                # The per-peer list is shown without its sessions' panes around it (Settings →
                # Peers), so it carries each session's title: the operator's own, on their own
                # instance. A session that cannot be read has an empty one.
                titles = grants.titles([g["session_key"] for g in views])
                for view in views:
                    view["title"] = titles.get(view["session_key"], "")
            return {
                "grants": views,
                "generation": federation.store.generation(),
                "levels": list(grants.LEVELS),
                # What a request that names no level gets. Never `send`.
                "default_level": grants.DEFAULT_LEVEL,
                "default_expires_in": dict(grants.DEFAULT_EXPIRY_S),
                "max_expires_in": grants.MAX_EXPIRY_S,
                "max_expires_in_by_level": dict(grants.MAX_EXPIRY_BY_LEVEL),
                # Shown before the operator sends `accept_risk: true` for a `send` grant.
                "send_risk": grants.SEND_RISK,
            }

        async def run() -> dict:
            return await asyncio.to_thread(read)

        return await guarded(run)()

    @app.post(f"{PREFIX}/grants", dependencies=[Depends(logged_in), Depends(csrf_guard)])
    async def federation_grant_create(request: Request) -> JSONResponse:
        async def run() -> dict:
            body = await _body(request)
            # Off the loop: it waits for the session's archive fence and scans the provider.
            grant = await asyncio.to_thread(
                lambda: grants.create(
                    federation.store,
                    session_key=body.get("session_key"),
                    peer_id=body.get("peer_id"),
                    level=body.get("level", grants.DEFAULT_LEVEL),
                    expires_in=body.get("expires_in"),
                    accept_risk=body.get("accept_risk", False),
                )
            )
            return await _changed(grant)

        return await guarded(run, 201)()

    @app.patch(
        f"{PREFIX}/grants/{{grant_id}}", dependencies=[Depends(logged_in), Depends(csrf_guard)]
    )
    async def federation_grant_change(grant_id: str, request: Request) -> JSONResponse:
        async def run() -> dict:
            body = await _body(request)
            grant = await asyncio.to_thread(
                lambda: grants.change(
                    federation.store,
                    grant_id,
                    expires_in=body.get("expires_in"),
                    revision=body.get("revision"),
                    level=body.get("level"),
                    accept_risk=body.get("accept_risk", False),
                )
            )
            return await _changed(grant)

        return await guarded(run)()

    @app.post(
        f"{PREFIX}/grants/{{grant_id}}/revoke",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_grant_revoke(grant_id: str) -> JSONResponse:
        async def run() -> dict:
            grant = await asyncio.to_thread(grants.revoke, federation.store, grant_id)
            return await _changed(grant)

        return await guarded(run)()

    # ── suggestions: what a peer proposes, decided by the operator (#2057) ──
    async def _decided(suggestion: store.Suggestion) -> dict:
        def answer() -> dict:
            peers = {p.peer_id: p for p in federation.store.list_peers()}
            return {"suggestion": inputs.view(suggestion, peers)}

        return await asyncio.to_thread(answer)

    @app.get(f"{PREFIX}/suggestions", dependencies=[Depends(logged_in)])
    async def federation_suggestions(session: str = "") -> JSONResponse:
        async def run() -> dict:
            rows = await asyncio.to_thread(inputs.listing, federation.store, session or None)
            return {"suggestions": rows}

        return await guarded(run)()

    @app.post(f"{PREFIX}/suggestions/{{suggestion_id}}/approve")
    async def federation_suggestion_approve(
        suggestion_id: str,
        request: Request,
        user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        async def run() -> dict:
            body = await _body(request)
            if set(body) - {"text"}:
                raise FederationError("invalid", "unknown field")
            # THE approval. This route is the only caller of `decide_approve` (a source scan
            # pins it): no automation mode, no model and no other route reaches it.
            made = await inputs.decide_approve(
                federation, suggestion_id, text=body.get("text"), approver=str(user)
            )
            return await _decided(made)

        return await guarded(run)()

    @app.post(f"{PREFIX}/suggestions/{{suggestion_id}}/reject")
    async def federation_suggestion_reject(
        suggestion_id: str,
        user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        async def run() -> dict:
            gone = await inputs.decide_reject(federation, suggestion_id, approver=str(user))
            return await _decided(gone)

        return await guarded(run)()

    # ── remote: what peers share with this instance (#2054) ───────────
    @app.get(f"{PREFIX}/remote", dependencies=[Depends(logged_in)])
    async def federation_remote() -> JSONResponse:
        async def run() -> dict:
            return {"remote": await asyncio.to_thread(remote.listing, federation)}

        return await guarded(run)()

    @app.get(
        f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/snapshot", dependencies=[Depends(logged_in)]
    )
    async def federation_remote_snapshot(peer_id: str, grant_id: str) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            return await remote.read(
                federation, peer_id, grant_id, "session.snapshot", {}, check=remote.check_snapshot
            )

        return await guarded(run)()

    @app.get(f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/events", dependencies=[Depends(logged_in)])
    async def federation_remote_events(
        peer_id: str,
        grant_id: str,
        after: str | None = None,
        limit: str | None = None,
        wait: str | None = None,
    ) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            held = _query_wait(wait)
            extra = {
                "after": _query_int(after, "after", default=0, low=0, high=grants.REVISION_MAX),
                "limit": _query_int(
                    limit,
                    "limit",
                    default=sharing.EVENTS_LIMIT_MAX,
                    low=1,
                    high=sharing.EVENTS_LIMIT_MAX,
                ),
                "wait": held,
            }
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "session.events",
                extra,
                check=remote.check_events,
                # The wait plus a margin: the channel's 20 s default would fire before a 20 s
                # wait answers.
                timeout=remote.events_timeout(held),
            )

        return await guarded(run)()

    @app.get(f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/history", dependencies=[Depends(logged_in)])
    async def federation_remote_history(
        peer_id: str, grant_id: str, before: str | None = None
    ) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            if not chat_store.valid_turn_id(before):
                raise FederationError("invalid", "before must be a turn id")
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "session.history",
                {"before": before},
                check=remote.check_history,
            )

        return await guarded(run)()

    @app.get(f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/frame", dependencies=[Depends(logged_in)])
    async def federation_remote_frame(
        peer_id: str, grant_id: str, have: str | None = None, wait: str | None = None
    ) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            if have is not None and not frames.valid_fingerprint(have):
                raise FederationError("invalid", "have must be a frame's fingerprint")
            held = _query_wait(wait)
            extra: dict = {"wait": held}
            if have is not None:
                extra["have"] = have
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "terminal.frame",
                extra,
                check=lambda doc: remote.check_frame(doc, have),
                timeout=remote.events_timeout(held),
                kind=grants.KIND_TERMINAL,
            )

        return await guarded(run)()

    # ── remote: writing to a session a peer shares (#2057) ────────────
    @app.post(
        f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/suggest",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_remote_suggest(
        peer_id: str, grant_id: str, request: Request
    ) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            extra = _message(await _body(request))
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "input.suggest",
                extra,
                check=remote.check_suggestion,
                retry=False,  # a refused write is answered as it is, never repeated
            )

        return await guarded(run, 201)()

    @app.post(
        f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/turns",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_remote_turn(peer_id: str, grant_id: str, request: Request) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            body = await _body(request)
            extra = _message(body, "expected_revision")
            expected = body.get("expected_revision")
            if expected is not None:
                if type(expected) is not int or not 0 <= expected <= grants.REVISION_MAX:
                    raise FederationError("invalid", "expected_revision must be an integer")
                extra["expected_revision"] = expected
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "input.send",
                extra,
                check=remote.check_sent,
                # Past the owner's handler timeout: its own `timeout` answer arrives first.
                timeout=remote.INPUT_CALL_TIMEOUT_S,
                retry=False,  # a refused write is answered as it is, never repeated
            )

        return await guarded(run, 202)()

    @app.get(
        f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/suggestions", dependencies=[Depends(logged_in)]
    )
    async def federation_remote_suggestions(peer_id: str, grant_id: str) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "input.suggestions",
                {},
                check=remote.check_suggestions,
                # A read: a refusal is re-resolved once, as for the other receiving reads.
            )

        return await guarded(run)()

    @app.post(
        f"{PREFIX}/remote/{{peer_id}}/{{grant_id}}/suggestions/{{suggestion_id}}/withdraw",
        dependencies=[Depends(logged_in), Depends(csrf_guard)],
    )
    async def federation_remote_withdraw(
        peer_id: str, grant_id: str, suggestion_id: str
    ) -> JSONResponse:
        async def run() -> dict:
            _remote_ids(peer_id, grant_id)
            if not grants.valid_grant_id(suggestion_id):
                raise FederationError("not_found", "no such suggestion", status=404)
            return await remote.read(
                federation,
                peer_id,
                grant_id,
                "input.withdraw",
                {"suggestion_id": suggestion_id},
                check=remote.check_suggestion,
                retry=False,  # a refused write is answered as it is, never repeated
            )

        return await guarded(run)()

    @app.websocket(WS_PATH)
    async def ws_fed(ws: WebSocket) -> None:
        # Decided BEFORE the upgrade is accepted: with nobody to talk to, the request is refused
        # as an ordinary HTTP 403 and not a byte of a handshake is read.
        if not federation.accepting():
            await ws.close()
            return
        await ws.accept()
        await federation.serve(_ServerTransport(ws))
