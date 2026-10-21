"""Public-link routes for API sessions (#1404 P2): view and, on a `send` link, send.

Every route takes the visitor through ``require_share`` (P1): the share cookie, the row re-read on
every request, and — for the mutating routes — a `send` link, the share's CSRF token + Origin and
a visitor name. On top of that, here:

* **Only API links.** A terminal link (P3) gets 404 on every route here.
* **A projection, never the operator's snapshot** (``share_api.project``), redacted, fail closed.
* **The mutation seam.** Each send runs in a worker thread that holds the share's fence, re-checks
  the row inside it, and only then hands the facade call to the event loop
  (``_fenced``). A revoke takes the same fence, so it lands before the effect or after it, never
  between the check and the effect.
* **Server-minted operation ids.** A visitor never names an operation: ``record_turn`` mints it
  (with the visitor's retry key for idempotency), so a visitor can't claim an operator turn.
* **Approvals, start/stop and settings are not here** — they stay with the operator.
* **A mission-held session** can be watched, not sent to.
* **Images:** a visitor attaches only images they uploaded through this link, and reads back only
  images this session's turns show.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import missions, native_images, share_api, shares, template_secrets
from .. import structured_runtime as runtime
from . import upload as upload_routes

MAX_TEXT = 20_000  # the facade's own per-turn text budget
MAX_PUBLIC_IMAGE_BYTES = native_images.MAX_IMAGE_BYTES
# Uploads stay on disk whether or not a turn attaches them; bound what one link can leave behind.
MAX_UPLOADS_PER_SHARE = 20
# The longest one upload request may stream its body. Bounding it is what makes an old reserved
# slot provably abandoned (see `shares.PENDING_UPLOAD_TTL_S`).
UPLOAD_READ_TIMEOUT_S = 60


class _Gone(Exception):
    """The link died between the request's first check and its mutation seam."""


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _api_only(share: shares.Share) -> None:
    if share.kind != "api":
        raise HTTPException(status_code=404, detail="not found")


def _structured_error(e: runtime.StructuredError) -> HTTPException:
    # The facade's messages are written for an operator and can name internal state; a visitor
    # gets the status and a plain reason.
    if e.status == 404:
        return HTTPException(status_code=404, detail="this session is no longer available")
    if e.status == 409:
        return HTTPException(status_code=409, detail="the session can't take that right now")
    if e.status == 422:
        return HTTPException(status_code=422, detail="that request isn't valid for this session")
    return HTTPException(status_code=503, detail="the session is unavailable right now — try again")


async def _snapshot(key: str) -> dict:
    try:
        return await runtime.snapshot(key)
    except runtime.StructuredError as e:
        raise _structured_error(e) from None


def _redactor():
    try:
        return share_api.redactor()
    except template_secrets.RedactionUnavailable:
        raise HTTPException(
            status_code=503, detail="this session can't be shown right now — try again"
        ) from None


# The threads that hold a share's fence while its facade call runs. Their OWN small pool, never the
# shared default executor: a fence holder waits on a coroutine that itself needs default-executor
# threads (`asyncio.to_thread` inside the facade), so holders parked in the default pool could
# starve it and wedge the whole app (#1423 independent review). Sends on one share are also
# serialized by an asyncio lock first, so at most one thread per share waits on the fence.
_FENCE_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="share-fence")
_SEND_LOCKS: dict[str, asyncio.Lock] = {}
# A facade call that never finishes must not hold the fence (and so block revoke) forever.
EFFECT_TIMEOUT_S = 120


def _send_lock(share_id: str) -> asyncio.Lock:
    lock = _SEND_LOCKS.get(share_id)
    if lock is None:
        lock = _SEND_LOCKS[share_id] = asyncio.Lock()
    return lock


async def _fenced(
    store: shares.ShareStore, share_id: str, effect, *, session_key: str | None = None
):
    """Run ``effect()`` — a zero-argument function returning the facade coroutine — on the event
    loop while a dedicated thread holds the share's fence and has re-checked, inside it, that the
    row is live (and, with ``session_key``, that no mission holds the session).

    The fence is a thread lock taken ONLY off the loop (see ``ShareStore.fence``); the loop stays
    free to run the facade call, and a revoke (also off the loop) waits for the effect or blocks
    it, never landing in between. ``effect()`` itself is called in that thread, so its own
    synchronous prelude (``record_turn``) runs under the fence too."""
    loop = asyncio.get_running_loop()

    def run():
        with store.fence(share_id):
            if store.active(share_id) is None:
                raise _Gone
            if session_key is not None:
                _not_mission_held(session_key)
            fut = asyncio.run_coroutine_threadsafe(effect(), loop)
            try:
                return fut.result(timeout=EFFECT_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                raise

    async with _send_lock(share_id):
        try:
            return await loop.run_in_executor(_FENCE_POOL, run)
        except _Gone:
            raise HTTPException(status_code=404, detail="link unavailable") from None
        except runtime.StructuredError as e:
            raise _structured_error(e) from None
        except shares.ShareError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except concurrent.futures.TimeoutError:
            raise HTTPException(
                status_code=503, detail="the session didn't answer in time — try again"
            ) from None


async def _fenced_sync(store: shares.ShareStore, share_id: str, fn):
    """Run the synchronous ``fn()`` while a fence-pool thread holds the share's fence and has
    re-checked, inside it, that the row is live. For effects with no facade coroutine (the upload
    slot and the upload write): a revoke lands before them (they never run) or after."""

    def run():
        with store.fence(share_id):
            if store.active(share_id) is None:
                raise _Gone
            return fn()

    try:
        return await asyncio.get_running_loop().run_in_executor(_FENCE_POOL, run)
    except _Gone:
        raise HTTPException(status_code=404, detail="link unavailable") from None


def _not_mission_held(key: str) -> None:
    if missions.session_mission(key) is not None:
        raise HTTPException(
            status_code=409,
            detail="this session is part of a mission right now — you can watch, not send",
        )


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expected an object")
    return body


def _client_key(body: dict) -> str:
    raw = body.get("client_key")
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="client_key must be a UUID") from None


async def _handle_or_503(share_id: str, stored: str) -> str:
    try:
        return await asyncio.to_thread(share_api.image_handle, share_id, stored)
    except template_secrets.SecretKeyUnavailable:
        raise HTTPException(status_code=503, detail="images are unavailable right now") from None


def register(app: FastAPI, *, store: shares.ShareStore, require_share) -> None:
    _view = require_share()
    _send = require_share(send=True)

    @app.get("/api/public/session")
    async def public_session(
        visitor=Depends(_view),  # noqa: B008 — a closure-local dependency
    ) -> Response:
        share = visitor.share
        _api_only(share)
        snap = await _snapshot(share.session_key)
        authors = await asyncio.to_thread(store.authors, share.session_key)

        def build():
            # Off the loop: reading the secret store and redacting a full snapshot are real work,
            # and a slow projection must never stall every other request (#1423 review).
            view = share_api.project(snap, authors, _redactor(), share.share_id)
            view["link"] = share_api.link_id(share.share_id)
            return view

        try:
            view = await asyncio.to_thread(build)
        except template_secrets.SecretKeyUnavailable:  # image handles are keyed: fail closed
            raise HTTPException(
                status_code=503, detail="this session can't be shown right now — try again"
            ) from None
        mission_held = await asyncio.to_thread(missions.session_mission, share.session_key)
        view["can_send"] = share.mode == "send" and not view["read_only"] and mission_held is None
        view["mission_held"] = mission_held is not None
        return _no_store(JSONResponse(view))

    @app.get("/api/public/events")
    async def public_events(
        after: int = 0,
        limit: int = 1,
        visitor=Depends(_view),  # noqa: B008
    ) -> Response:
        share = visitor.share
        _api_only(share)
        limit = max(1, min(int(limit), 100))
        try:
            page = await runtime.events(share.session_key, after=max(0, int(after)), limit=limit)
        except runtime.StructuredError as e:
            raise _structured_error(e) from None
        try:
            link = await asyncio.to_thread(share_api.link_id, share.share_id)
        except template_secrets.SecretKeyUnavailable:
            raise HTTPException(
                status_code=503, detail="this session can't be shown right now — try again"
            ) from None
        return _no_store(JSONResponse(share_api.project_events(page, link)))

    @app.get("/api/public/uploads/{image}")
    async def public_upload(image: str, visitor=Depends(_view)) -> Response:  # noqa: B008
        """An image by its OPAQUE handle (``share_api.image_handle``), resolved server-side
        against the images this session's shown turns reference."""
        share = visitor.share
        _api_only(share)
        snap = await _snapshot(share.session_key)
        try:
            handles = await asyncio.to_thread(share_api.image_handles, snap, share.share_id)
        except template_secrets.SecretKeyUnavailable:
            raise HTTPException(
                status_code=503, detail="images are unavailable right now"
            ) from None
        stored = handles.get(image)
        if stored is None:
            raise HTTPException(status_code=404, detail="no such image")
        suffix = os.path.splitext(stored)[1].lower()
        mime = upload_routes.IMAGE_TYPES.get(suffix)
        if mime is None:
            raise HTTPException(status_code=404, detail="no such image")
        try:
            fd, st = upload_routes.open_upload(stored)
        except OSError:
            raise HTTPException(status_code=404, detail="no such image") from None
        fh = os.fdopen(fd, "rb")
        return StreamingResponse(
            upload_routes._iter_fd(fh, st.st_size),
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(st.st_size),
            },
        )

    @app.post("/api/public/upload")
    async def public_upload_image(
        request: Request,
        visitor=Depends(_send),  # noqa: B008
    ) -> Response:
        """A visitor's image, as the RAW request body (``Content-Type: image/…``). Read with a hard
        cap charged per chunk, so nothing is spooled before the limit applies; the format is
        sniffed from the bytes, not trusted from the header."""
        share = visitor.share
        _api_only(share)
        await asyncio.to_thread(_not_mission_held, share.session_key)
        snap = await _snapshot(share.session_key)
        if not snap.get("images"):
            raise HTTPException(status_code=409, detail="this session doesn't take images")
        # A slot is RESERVED before any byte is accepted (count + claim in one transaction), under
        # the share's fence with the row re-checked, so concurrent uploads can't slip under the cap
        # and a revoked link reserves nothing. It is handed back on any failure.
        slot = await _fenced_sync(
            store,
            share.share_id,
            lambda: store.reserve_upload(share.share_id, MAX_UPLOADS_PER_SHARE),
        )
        if slot is None:
            raise HTTPException(status_code=429, detail="this link has uploaded enough images")
        filled = False
        try:

            async def read() -> tuple[list[bytes], bytes]:
                size, chunks, head = 0, [], b""
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_PUBLIC_IMAGE_BYTES:
                        raise HTTPException(status_code=413, detail="images can be at most 5 MiB")
                    chunks.append(chunk)
                    if len(head) < 16:
                        head += chunk[: 16 - len(head)]
                return chunks, head

            try:
                chunks, head = await asyncio.wait_for(read(), UPLOAD_READ_TIMEOUT_S)
            except TimeoutError:
                raise HTTPException(status_code=408, detail="the upload took too long") from None
            if not chunks or not any(chunks):
                raise HTTPException(status_code=422, detail="empty upload")
            mime = native_images.sniff(head)
            if mime is None:
                raise HTTPException(status_code=415, detail="only PNG, JPEG, GIF and WebP images")
            ext = {v: k for k, v in upload_routes.IMAGE_TYPES.items()}.get(mime)

            def settle():
                # Under the fence, the row re-checked: a link revoked while the body streamed
                # writes nothing. A slot that's gone means the file must not stay on disk.
                dest, _safe = upload_routes.store_upload(chunks, f"shared-image{ext}")
                if not store.fill_upload(share.share_id, slot, dest.name):
                    with contextlib.suppress(OSError):
                        os.unlink(dest)
                    raise HTTPException(status_code=409, detail="the upload slot expired")
                return dest

            dest = await _fenced_sync(store, share.share_id, settle)
            filled = True
        finally:
            if not filled:
                await asyncio.to_thread(store.release_upload, share.share_id, slot)
        # `stored` is the name THIS visitor attaches by (it is ours — "shared-image" plus a
        # timestamp — so it carries no operator filename); `image` is its read-back handle.
        return _no_store(
            JSONResponse(
                {
                    "stored": dest.name,
                    "image": await _handle_or_503(share.share_id, dest.name),
                    "mime": mime,
                },
                status_code=201,
            )
        )

    @app.post("/api/public/turns")
    async def public_turn(
        request: Request,
        visitor=Depends(_send),  # noqa: B008
    ) -> Response:
        share = visitor.share
        _api_only(share)
        body = await _body(request)
        client_key = _client_key(body)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="text is required")
        if len(text) > MAX_TEXT:
            raise HTTPException(status_code=422, detail="that message is too long")
        attachments = body.get("attachments") or []
        if not isinstance(attachments, list) or not all(isinstance(a, str) for a in attachments):
            raise HTTPException(status_code=422, detail="attachments must be a list of names")
        for stored in attachments:
            if not await asyncio.to_thread(store.owns_upload, share.share_id, stored):
                # Uploads share one folder; a visitor attaches only what THEY uploaded here.
                raise HTTPException(status_code=422, detail="attach images you uploaded here")
        expected = body.get("expected_revision")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            raise HTTPException(status_code=422, detail="expected_revision must be an integer")
        await asyncio.to_thread(_not_mission_held, share.session_key)  # early, cheap refusal

        def effect():
            op, _fresh = store.record_turn(
                share_id=share.share_id,
                session_key=share.session_key,
                client_key=client_key,
                author=visitor.name,
            )
            return runtime.submit_turn(
                share.session_key,
                operation_id=op,
                text=text,
                expected_revision=expected,
                attachments=attachments or None,
            )

        out = await _fenced(store, share.share_id, effect, session_key=share.session_key)
        state = out.get("state") if isinstance(out, dict) else None
        return _no_store(
            JSONResponse({"state": state}, status_code=202 if state == "running" else 200)
        )

    @app.post("/api/public/interrupt")
    async def public_interrupt(
        request: Request,
        visitor=Depends(_send),  # noqa: B008
    ) -> Response:
        """Stop the turn that is running now (the composer's Stop)."""
        share = visitor.share
        _api_only(share)
        body = await _body(request)
        turn_id = body.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise HTTPException(status_code=422, detail="turn_id is required")
        await asyncio.to_thread(_not_mission_held, share.session_key)
        snap = await _snapshot(share.session_key)
        # A turn the agent runs for itself (#1438) is the operator's to stop, never a visitor's:
        # refused by name, not left to the active-turn check below.
        if any(
            t.get("kind") == "agent" and t.get("turn_id") == turn_id
            for t in snap.get("turns") or []
        ) or turn_id == (snap.get("native") or {}).get("background_turn"):
            raise HTTPException(
                status_code=403, detail="only the owner can interrupt the agent's own turn"
            )
        if snap.get("active_turn") != turn_id:
            raise HTTPException(status_code=409, detail="that turn isn't running")
        op = str(uuid.uuid4())
        await _fenced(
            store,
            share.share_id,
            lambda: runtime.interrupt(share.session_key, operation_id=op, turn_id=turn_id),
            session_key=share.session_key,
        )
        return _no_store(JSONResponse({"interrupted": True}, status_code=202))

    @app.post("/api/public/send-now")
    async def public_send_now(
        request: Request,
        visitor=Depends(_send),  # noqa: B008
    ) -> Response:
        """Deliver a message queued through THIS link now. An operator's queued message, or
        another link's, is not theirs to push ahead. (Per link, not per visitor: names are
        self-chosen, so two people on one link are not told apart.)"""
        share = visitor.share
        _api_only(share)
        body = await _body(request)
        queued = body.get("queued_turn_id")
        if not isinstance(queued, str) or not queued:
            raise HTTPException(status_code=422, detail="queued_turn_id is required")
        await asyncio.to_thread(_not_mission_held, share.session_key)
        snap = await _snapshot(share.session_key)
        authors = await asyncio.to_thread(store.authors, share.session_key)
        mine = {
            t.get("turn_id")
            for t in snap.get("turns") or []
            if (authors.get(t.get("operation_id")) or {}).get("share_id") == share.share_id
        }
        if queued not in mine:
            raise HTTPException(status_code=403, detail="you can only send your own message now")
        # The operator turn running now, else a turn the agent runs for itself that the worker
        # advertises it can interrupt (#1427) — the same target the operator pane picks.
        active = snap.get("active_turn")
        native = snap.get("native") or {}
        if not active and native.get("send_now_background") == "interrupt":
            active = native.get("background_turn")
        if not active:
            raise HTTPException(status_code=409, detail="nothing is running to send it into")
        op = str(uuid.uuid4())
        await _fenced(
            store,
            share.share_id,
            lambda: runtime.send_now(
                share.session_key, operation_id=op, turn_id=active, queued_turn_id=queued
            ),
            session_key=share.session_key,
        )
        return _no_store(JSONResponse({"sent": True}, status_code=202))
