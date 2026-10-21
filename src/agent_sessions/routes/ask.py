"""`/api/ask/*` — the Ask sidebar's durable conversations (#1443 Phase 1).

* ``GET  /api/ask/conversations`` — the switcher's list, latest first (retention runs first).
* ``POST /api/ask/conversations`` — a new, empty conversation. Never deletes anything.
* ``GET  /api/ask/conversations/{id}`` — one conversation's turns.
* ``POST /api/ask/conversations/{id}/message`` — ask; answers with the settled turn.
* ``POST /api/ask/conversations/{id}/message/stream`` — the same turn as NDJSON: ``progress``
  lines while it runs, then exactly ONE ``{"type": "turn", "status", "turn"}`` or
  ``{"type": "error", "status", "detail"}``.

* ``POST /api/ask/proposals/{id}/decide`` — ``{"decision": "start" | "dismiss", "bypass": bool}``
  for a session proposal (#1443 Phase 3a). ``start`` answers 202 while the launch runs and the card
  follows it on the next read; a repeat returns the entry as it is, never a second launch. An
  absent ``bypass`` is ``false``. The client names the proposal, never what it starts.

* ``POST /api/ask/conversations/{id}/seen`` — everything in it so far has been seen (the unread
  marker, #1443 Phase 4). ``GET /api/ask/unread`` — whether any conversation has an unseen update
  (the Ask icon's dot). ``DELETE /api/ask/conversations/{id}/follow/{key}`` — stop following a
  session.

Both message routes take ``{"turn_id": <uuid>, "text", "contact"?}``. ``contact`` is the HQ
contact who answers (#1525), the client's resolved choice: a contact id or ``"off"``; absent,
unknown or not a string is ``"off"`` (plain HQ). It is stored on the turn, and every turn in the
conversation body carries its own ``contact``. A repeat of a settled ``turn_id`` returns
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

from .. import (
    actuator,
    ask_follow,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    hq_contacts,
    prefs,
)

# The latest `seen` time a client may send (2100-01-01): any later or unbounded value is refused.
_MAX_TS = 4_102_444_800


def _ndjson(ev: dict) -> str:
    return json.dumps(ev) + "\n"


def _conversation_body(cid: str, records: list[dict]) -> dict:
    title = ""
    for r in records:
        if r.get("type") == "user" and isinstance(r.get("text"), str):
            title = " ".join(r["text"].split())[: ask_store.LIST_TITLE_MAX]
            break
    follows = ask_follow.state(cid)
    following = set(follows["following"])
    updates = [
        {
            "key": r["key"],
            "kind": r["kind"],
            "title": r.get("title") or "",
            "ts": r.get("ts"),
            "following": r["key"] in following,
        }
        for r in records
        if r.get("type") == "update" and isinstance(r.get("key"), str)
    ]
    return {
        "id": cid,
        "title": title,
        "turns": ask_orchestrator.with_live(ask_orchestrator.turn_views(records), cid),
        "running": ask_orchestrator.running(cid),
        # #1443 Phase 4: what the follower wrote, in order; the client places each by its time.
        "updates": updates,
        "following": sorted(following),
        # Every active follow with its title: Unfollow is offered before any update exists.
        "follows": sorted(follows["follows"], key=lambda f: f["key"]),
        "unread": follows["unread"],
        # The follow state could not be read this time: not "nothing followed".
        "follows_incomplete": follows["incomplete"],
        # An update is stored but not appended yet: it will land; keep reading.
        "updates_pending": follows["pending"],
        # The update budget is spent: this conversation no longer follows sessions (#1517).
        "follows_exhausted": follows["exhausted"],
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
            rows = ask_store.list_conversations()
            unread = set(ask_follow.unread_conversations())
            return [{**r, "unread": r["id"] in unread} for r in rows]

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
        body = await asyncio.to_thread(_conversation_body, cid, records)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    async def _message_body(request: Request) -> tuple | JSONResponse:
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"detail": "body must be a JSON object"}, status_code=422)
        turn_id, text = body.get("turn_id"), body.get("text")
        answers, option = body.get("answers"), body.get("option")
        if not ask_store.valid_turn_id(turn_id):
            return JSONResponse({"detail": "turn_id (a UUID) is required"}, status_code=422)
        if answers is not None and not ask_store.valid_turn_id(answers):
            return JSONResponse({"detail": "answers must be a question id"}, status_code=422)
        if option is not None and (isinstance(option, bool) or not isinstance(option, int)):
            return JSONResponse({"detail": "option must be an index"}, status_code=422)
        if option is not None and answers is None:
            return JSONResponse({"detail": "an option needs the question it answers"}, 422)
        if option is not None and (text is None or text == ""):
            text = ""  # the server resolves the option to its stored reply
        elif not isinstance(text, str) or not text.strip():
            return JSONResponse({"detail": "text (string) is required"}, status_code=422)
        text = text.strip()
        if len(text) > ask_orchestrator.QUERY_MAX:
            return JSONResponse(
                {"detail": f"text too long (max {ask_orchestrator.QUERY_MAX} chars)"},
                status_code=422,
            )
        # Never free text: anything but a known contact id is plain HQ (#1525).
        contact = hq_contacts.resolve(body.get("contact"))
        return turn_id, text, answers, option, contact  # type: ignore[return-value]

    async def _begin(cid: str, request: Request):
        """``(task, settled view)`` or the JSONResponse that refuses the turn."""
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        parsed = await _message_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        turn_id, text, answers, option, contact = parsed
        try:
            return await ask_orchestrator.begin(
                cid,
                turn_id,
                text,
                working_keys=actuator.working_keys(registry),
                busy_keys=actuator.busy_keys(registry),
                answers=answers,
                option=option,
                contact=contact,
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
        else:
            view = (await asyncio.to_thread(ask_orchestrator.with_live, [view], cid))[0]
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
        if task is None:
            view = (await asyncio.to_thread(ask_orchestrator.with_live, [view], cid))[0]

        async def settled() -> AsyncIterator[str]:
            yield _ndjson({"type": "turn", "status": 200, "turn": view})

        return StreamingResponse(
            _watch(cid, task) if task is not None else settled(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/ask/proposals/{pid}/decide")
    async def decide_proposal(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        if not ask_proposals.valid_pid(pid):
            return JSONResponse({"detail": "no such proposal"}, status_code=404)
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"detail": "body must be a JSON object"}, status_code=422)
        decision, bypass = body.get("decision"), body.get("bypass", False)
        if decision not in ("start", "dismiss"):
            return JSONResponse({"detail": "decision must be start or dismiss"}, status_code=422)
        if not isinstance(bypass, bool):
            return JSONResponse({"detail": "bypass must be true or false"}, status_code=422)
        if registry is None:
            return JSONResponse(
                {"detail": "the session runner isn't available in this process"}, status_code=503
            )
        try:
            status, view = await ask_proposals.decide(
                pid, decision, bypass=bypass, registry=registry
            )
        except ask_proposals.ProposalError as e:
            extra = {"proposal": e.view} if e.view is not None else {}
            return JSONResponse({"detail": e.detail, **extra}, status_code=e.status)
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_store.SidecarUnreadable:
            return JSONResponse(
                {"detail": "the proposal could not be read just now; nothing was started"},
                status_code=503,
            )
        return JSONResponse({"proposal": view}, status_code=status)

    @app.get("/api/ask/unread")
    async def ask_unread(_: str = Depends(logged_in)) -> JSONResponse:
        cids = await asyncio.to_thread(ask_follow.unread_conversations)
        return JSONResponse(
            {"unread": bool(cids), "conversations": len(cids)},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/ask/conversations/{cid}/seen")
    async def ask_seen(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """``{"through": <ts>}``: the newest update the client has SHOWN. An update written after
        it read the conversation stays unread."""
        if not (ask_store.valid_cid(cid) and await asyncio.to_thread(ask_store.exists, cid)):
            return _missing(cid)
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        through = body.get("through") if isinstance(body, dict) else None
        # A time within the epoch range, checked before float(): a huge JSON integer would
        # overflow it (an unhandled 500), and inf/nan fail the range (Hermes on #1500).
        if (
            isinstance(through, bool)
            or not isinstance(through, int | float)
            or not 0 <= through <= _MAX_TS
        ):
            return JSONResponse({"detail": "through (a time) is required"}, status_code=422)
        try:
            remaining = await asyncio.to_thread(ask_follow.mark_seen, cid, float(through))
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_store.SidecarUnreadable:
            return JSONResponse({"detail": "could not be read just now"}, status_code=503)
        return JSONResponse({"unread": remaining})

    @app.delete("/api/ask/conversations/{cid}/follow/{key:path}")
    async def ask_unfollow(
        cid: str, key: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        if not (ask_store.valid_cid(cid) and await asyncio.to_thread(ask_store.exists, cid)):
            return _missing(cid)
        try:
            was = await asyncio.to_thread(ask_follow.unfollow, cid, key)
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_store.SidecarUnreadable:
            return JSONResponse({"detail": "could not be read just now"}, status_code=503)
        if not was:
            return JSONResponse({"detail": "not followed"}, status_code=404)
        return JSONResponse({"following": False})
