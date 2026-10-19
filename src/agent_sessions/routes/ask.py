"""`/api/ask/*` — the Ask sidebar's durable conversations (#1443 Phase 1).

* ``GET  /api/ask/conversations`` — the switcher's list, latest first (retention runs first).
* ``POST /api/ask/conversations`` — a new, empty conversation. Never deletes anything.
* ``GET  /api/ask/conversations/{id}`` — one conversation's turns.
* ``POST /api/ask/conversations/{id}/message`` — ask; answers with the settled turn.
* ``POST /api/ask/conversations/{id}/message/stream`` — the same turn as NDJSON: ``progress``
  lines while it runs, then exactly ONE ``{"type": "turn", "status", "turn"}`` or
  ``{"type": "error", "status", "detail"}``.

Both message routes take ``{"turn_id": <uuid>, "text"}``. A repeat of a settled ``turn_id`` returns
it without running the model again; a repeat while it runs watches the same turn. Leaving never
cancels a turn (``ask_orchestrator``'s module note). ``/api/pulse/ask[/stream]`` is unchanged: the
transient, read-only Ask seam stays for its other callers (#1397).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import actuator, ask_orchestrator, ask_store, prefs


def _ndjson(ev: dict) -> str:
    return json.dumps(ev) + "\n"


def _conversation_body(cid: str, records: list[dict]) -> dict:
    title = ""
    for r in records:
        if r.get("type") == "user" and isinstance(r.get("text"), str):
            title = " ".join(r["text"].split())[: ask_store.LIST_TITLE_MAX]
            break
    return {
        "id": cid,
        "title": title,
        "turns": ask_orchestrator.turn_views(records),
        "running": ask_orchestrator.running(cid),
    }


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    def _configured() -> bool:
        return bool(prefs.public_ai_review()["configured"])

    def _missing(cid: str) -> JSONResponse:
        return JSONResponse({"detail": "no such conversation"}, status_code=404)

    def _refused(e: ask_store.StoreRefused) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=503)

    @app.get("/api/ask/conversations")
    async def list_conversations(_: str = Depends(logged_in)) -> JSONResponse:
        def read() -> list[dict]:
            ask_store.sweep()
            return ask_store.list_conversations()

        return JSONResponse(
            {"conversations": await asyncio.to_thread(read), "configured": _configured()},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/ask/conversations")
    async def create_conversation(
        _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        def make() -> str:
            ask_store.sweep()
            return ask_store.create()

        try:
            cid = await asyncio.to_thread(make)
        except ask_store.StoreRefused as e:
            return _refused(e)
        return JSONResponse(_conversation_body(cid, []), status_code=201)

    @app.get("/api/ask/conversations/{cid}")
    async def get_conversation(cid: str, _: str = Depends(logged_in)) -> JSONResponse:
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        try:
            records = await ask_orchestrator.read_conversation(cid)
        except ask_store.ConversationNotFound:
            return _missing(cid)
        except ask_store.StoreRefused as e:
            return _refused(e)
        return JSONResponse(_conversation_body(cid, records), headers={"Cache-Control": "no-store"})

    async def _message_body(request: Request) -> tuple[str, str] | JSONResponse:
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"detail": "body must be a JSON object"}, status_code=422)
        turn_id, text = body.get("turn_id"), body.get("text")
        if not ask_store.valid_turn_id(turn_id):
            return JSONResponse({"detail": "turn_id (a UUID) is required"}, status_code=422)
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"detail": "text (string) is required"}, status_code=422)
        text = text.strip()
        if len(text) > ask_orchestrator.QUERY_MAX:
            return JSONResponse(
                {"detail": f"text too long (max {ask_orchestrator.QUERY_MAX} chars)"},
                status_code=422,
            )
        return turn_id, text  # type: ignore[return-value]

    async def _begin(cid: str, request: Request):
        """``(task, settled view)`` or the JSONResponse that refuses the turn."""
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        parsed = await _message_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        turn_id, text = parsed
        try:
            return await ask_orchestrator.begin(
                cid, turn_id, text, working_keys=actuator.working_keys(registry)
            )
        except ask_store.ConversationNotFound:
            return _missing(cid)
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_orchestrator.TurnError as e:
            return JSONResponse({"detail": e.detail, **e.extra}, status_code=e.status)

    @app.post("/api/ask/conversations/{cid}/message")
    async def message(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        started = await _begin(cid, request)
        if isinstance(started, JSONResponse):
            return started
        task, view = started
        if task is not None:
            # Shielded: a client that goes away ends this wait, never the turn.
            view = await asyncio.shield(task)
        return JSONResponse({"turn": view})

    async def _watch(cid: str, task: asyncio.Task) -> AsyncIterator[str]:
        """Relay progress, then exactly one last line from the turn's RESULT. Cancelled (the
        client left), it stops relaying and nothing else: `asyncio.wait` does not cancel what it
        waits on."""
        get: asyncio.Future | None = None
        # Subscribed only now that this response is actually streaming, and always dropped in the
        # `finally`: a client that leaves — before the first byte or later — leaves nothing behind.
        queue = ask_orchestrator.listen(cid, task)
        try:
            while True:
                get = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield _ndjson(get.result())
                    get = None
                    continue
                break
            get.cancel()
            get = None
            try:
                view = task.result()
            except Exception:  # noqa: BLE001 — the 200 is already on the wire
                yield _ndjson({"type": "error", "status": 500, "detail": "the turn failed"})
                return
            yield _ndjson({"type": "turn", "status": 200, "turn": view})
        finally:
            if get is not None:
                get.cancel()
            ask_orchestrator.unlisten(cid, queue)

    @app.post("/api/ask/conversations/{cid}/message/stream", response_model=None)
    async def message_stream(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse | StreamingResponse:
        started = await _begin(cid, request)
        if isinstance(started, JSONResponse):
            return started
        task, view = started

        async def settled() -> AsyncIterator[str]:
            yield _ndjson({"type": "turn", "status": 200, "turn": view})

        return StreamingResponse(
            _watch(cid, task) if task is not None else settled(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
