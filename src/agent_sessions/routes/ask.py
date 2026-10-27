"""`/api/ask/*` — the Ask sidebar's durable conversations (#1443 Phase 1).

* ``GET  /api/ask/conversations`` — the switcher's list, latest first (retention runs first).
* ``POST /api/ask/conversations`` — a new, empty conversation. Never deletes anything.
* ``GET  /api/ask/conversations/{id}`` — one conversation's turns.
* ``DELETE /api/ask/conversations/{id}`` — the operator deletes one conversation and everything
  keyed by it (``ask_store.delete_conversation``: retention's locks and pins). 409 while a turn
  runs in it or a proposal in it is mid-decision.
* ``POST /api/ask/conversations/{id}/message`` — ask; answers with the settled turn.
* ``POST /api/ask/conversations/{id}/message/stream`` — the same turn as NDJSON: ``progress``
  lines while it runs, then exactly ONE ``{"type": "turn", "status", "turn"}`` or
  ``{"type": "error", "status", "detail"}``.

* ``POST /api/ask/proposals/{id}/decide`` — ``{"decision": "start" | "dismiss", "bypass": bool}``
  for a session proposal (#1443 Phase 3a); ``{"decision": "confirm" | "dismiss"}`` for an HQ
  operation (#1559), which answers 202 and runs as a server task (``ask_operations``).
  ``start`` answers 202 while the launch runs and the card follows it on the next read; a
  repeat returns the entry as it is, never a second launch. An absent ``bypass`` is ``false``.
  The client names the proposal, never what it starts.

* ``GET /api/ask/proposals/{id}/content`` — a library card's draft (#1611): the files, the
  diff, or an automation's scope lines; 409 when the draft no longer matches its card.

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
import os
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

try:  # the package renamed; the old name still works but emits a PendingDeprecationWarning
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover - older pins
    from multipart.multipart import MultipartParser, parse_options_header

from .. import (
    actuator,
    asgi_call,
    ask_attachments,
    ask_follow,
    ask_operations,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    hq_contacts,
    prefs,
    voice_calls,
)

# The latest `seen` time a client may send (2100-01-01): any later or unbounded value is refused.
_MAX_TS = 4_102_444_800
#: Ingress ceiling over the per-file cap: multipart framing, and the bound on a chunked body.
_INGRESS_HEADROOM = 64 * 1024
_MAX_REQUEST_BYTES = ask_attachments.MAX_FILE_BYTES + _INGRESS_HEADROOM
#: A non-file part is tiny; bounding it separately stops a "field" carrying the payload.
_MAX_FIELD_BYTES = 8 * 1024


class _UploadMalformed(Exception):
    """The multipart request is malformed in a way the operator should see named."""


def _ascii_name(name: str) -> str:
    """The Content-Disposition filename: printable ASCII only, quotes stripped. The UTF-8 name is
    what the client shows; this is only the download dialog's anchor."""
    out = "".join(c for c in name if 32 <= ord(c) < 127 and c not in '"\\')
    return out[:200] or "file"


async def _read_one_attachment(request: Request) -> tuple[str, bytes]:
    """`(name, bytes)` for ONE file part, charged before the bytes are accepted. A second file
    part is refused (one file per request); fields come first and are ignored apart from size."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data":
        raise _UploadMalformed("this route takes a multipart upload")
    boundary = params.get(b"boundary")
    if not boundary:
        raise _UploadMalformed("the multipart request has no boundary")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_REQUEST_BYTES:
        raise ask_attachments.AttachmentRefused(
            f"that file is larger than the limit "
            f"({ask_attachments.MAX_FILE_BYTES // (1024 * 1024)} MB)",
            status=413,
        )

    state = {
        "is_file": False,
        "name": "",
        "buf": bytearray(),
        "total": 0,
        "saw": False,
        "field": bytearray(),
        "ended": False,  # the terminal boundary was seen (#1720 round 9)
    }
    header = {"field": bytearray(), "value": bytearray()}

    def on_part_begin() -> None:
        header["field"] = bytearray()
        header["value"] = bytearray()
        state["is_file"] = False

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header["value"] += data[start:end]

    def on_header_end() -> None:
        if bytes(header["field"]).lower() == b"content-disposition":
            _, p = parse_options_header(bytes(header["value"]))
            state["is_file"] = b"filename" in p
            if state["is_file"]:
                state["name"] = p.get(b"filename", b"").decode("utf-8", "replace")
        header["field"] = bytearray()
        header["value"] = bytearray()

    def on_headers_finished() -> None:
        if state["is_file"]:
            if state["saw"]:
                # ONE file per request — a second file part must never stream into the first
                # part's buffer (fileupload.py measured that failure mode).
                raise _UploadMalformed("only one file per request")
            state["saw"] = True

    def on_end() -> None:
        state["ended"] = True

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if not chunk:
            return
        if state["is_file"]:
            # The bound: charged BEFORE accepted. An under-declaring client gains nothing.
            if state["total"] + len(chunk) > ask_attachments.MAX_FILE_BYTES:
                raise ask_attachments.AttachmentRefused(
                    f"that file is larger than the limit "
                    f"({ask_attachments.MAX_FILE_BYTES // (1024 * 1024)} MB)",
                    status=413,
                )
            state["total"] += len(chunk)
            state["buf"] += chunk
        else:
            if len(state["field"]) + len(chunk) > _MAX_FIELD_BYTES:
                raise _UploadMalformed("an upload field is too large")
            state["field"] += chunk

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_end": on_end,
        },
    )
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_REQUEST_BYTES:
                raise ask_attachments.AttachmentRefused(
                    "that upload is larger than the limit", status=413
                )
            parser.write(chunk)
    except _UploadMalformed:
        raise
    except ask_attachments.AttachmentRefused:
        raise  # a cap refusal keeps its own status (413 stays 413)
    except Exception as e:
        raise _UploadMalformed("the upload could not be read") from e
    # The terminal boundary is REQUIRED (#1720 round 9): a transfer cut mid-body or missing
    # its closing boundary used to be accepted as a complete file — silently truncating
    # whatever was uploaded. Only `on_end` proves the parse reached the closing boundary.
    if not state["ended"]:
        raise _UploadMalformed("the upload ended early — the file was not fully sent")
    if not state["buf"]:
        raise _UploadMalformed("no file part in the request")
    return state["name"], bytes(state["buf"])


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

    @app.delete("/api/ask/conversations/{cid}")
    async def delete_conversation(
        cid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        try:
            await asyncio.to_thread(ask_store.delete_conversation, cid)
        except ask_store.ConversationNotFound:
            return _missing(cid)
        except ask_store.ConversationBusy as e:
            return JSONResponse({"detail": f"{e} — try again once it settles"}, status_code=409)
        except ask_store.ConversationNotDeleted:
            return JSONResponse(
                {"detail": "the conversation could not be deleted just now; nothing was removed"},
                status_code=503,
            )
        return JSONResponse({"deleted": True, "id": cid})

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
        # Files shared with this question (#1707): their staged ids, claimed under the turn lock.
        # Parsed before the text rule so a file-only message is a message.
        raw_atts = body.get("attachments")
        attachments: list[str] | None = None
        if raw_atts is not None:
            if not isinstance(raw_atts, list) or not all(isinstance(a, str) for a in raw_atts):
                return JSONResponse(
                    {"detail": "attachments must be a list of ids"}, status_code=422
                )
            if len(raw_atts) > ask_attachments.MAX_STAGED:
                return JSONResponse(
                    {"detail": f"at most {ask_attachments.MAX_STAGED} files per message"},
                    status_code=422,
                )
            attachments = [a for a in raw_atts if ask_attachments.valid_aid(a)]
            if len(attachments) != len(raw_atts):
                return JSONResponse({"detail": "malformed attachment id"}, status_code=422)
        if option is None and (not isinstance(text, str) or not text.strip()):
            if not attachments:
                return JSONResponse({"detail": "text (string) is required"}, status_code=422)
            text = ""  # a file-only message: the shared file is the message
        text = (text or "").strip()
        if len(text) > ask_orchestrator.QUERY_MAX:
            return JSONResponse(
                {"detail": f"text too long (max {ask_orchestrator.QUERY_MAX} chars)"},
                status_code=422,
            )
        # Never free text: anything but a known contact id is plain HQ (#1525).
        contact = hq_contacts.resolve(body.get("contact"))
        return turn_id, text, answers, option, contact, attachments  # type: ignore[return-value]

    async def _begin(cid: str, request: Request):
        """``(task, settled view)`` or the JSONResponse that refuses the turn."""
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        parsed = await _message_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        turn_id, text, answers, option, contact, attachments = parsed
        try:
            started = await ask_orchestrator.begin(
                cid,
                turn_id,
                text,
                working_keys=actuator.working_keys(registry),
                busy_keys=actuator.busy_keys(registry),
                answers=answers,
                option=option,
                contact=contact,
                attachments=attachments,
                operator=asgi_call.Operator.from_scope(request.scope),
                app=request.app,
            )
        except ask_store.ConversationNotFound:
            return _missing(cid)
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_orchestrator.TurnError as e:
            return JSONResponse({"detail": e.detail, **e.extra}, status_code=e.status)
        call = voice_calls.REGISTRY.current
        task = started[0]
        if task is not None and call is not None and call.cid == cid and call.state != "ended":
            # An ACCEPTED typed turn on the call's conversation (#1668): the Commander is there,
            # and any farewell HQ planned is off. A refused one changed nothing, and a replay of a
            # turn that already settled (`begin` returns no task) is not a new turn (Hermes 7010),
            # and neither is a retry of one still running (the same task, counted once, 7012).
            if call.on_typed_turn(task):
                # Once it settles, a question it ends on is one a spoken reply may answer.
                def settled(t: asyncio.Task, call=call) -> None:
                    if not t.cancelled() and t.exception() is None:
                        call.on_typed_settled(t.result())

                task.add_done_callback(settled)
        return started

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

    @app.get("/api/ask/proposals/{pid}/content")
    async def proposal_content(pid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        """A library card's draft (#1611): the files, the diff or the automation's scope lines,
        served only while the draft still matches the card's binding."""
        if not ask_proposals.valid_pid(pid):
            return JSONResponse({"detail": "no such proposal"}, status_code=404)
        try:
            status, body = await asyncio.to_thread(ask_operations.content, pid)
        except ask_store.StoreRefused as e:
            return _refused(e)
        except ask_store.SidecarUnreadable:
            return JSONResponse(
                {"detail": "the proposal could not be read just now"}, status_code=503
            )
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

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
        if decision not in ("start", "dismiss", "confirm"):
            return JSONResponse(
                {"detail": "decision must be start, confirm or dismiss"}, status_code=422
            )
        if not isinstance(bypass, bool):
            return JSONResponse({"detail": "bypass must be true or false"}, status_code=422)
        # An HQ operation's card inputs (#1559): what the OPERATOR picked on it, e.g. a folder.
        inputs = body.get("inputs")
        if inputs is not None and not isinstance(inputs, dict):
            return JSONResponse({"detail": "inputs must be an object"}, status_code=422)
        if registry is None and decision == "start":
            return JSONResponse(
                {"detail": "the session runner isn't available in this process"}, status_code=503
            )
        try:
            status, view = await ask_proposals.decide(
                pid,
                decision,
                bypass=bypass,
                registry=registry,
                # An HQ operation (#1559) runs the UI's own route as THIS operator: their cookie,
                # CSRF token and Origin, as this already-guarded request carried them.
                operator=asgi_call.Operator.from_scope(request.scope),
                app=request.app,
                inputs=inputs,
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

    # ---- shared files (#1707) ------------------------------------------------------------------

    @app.post("/api/ask/conversations/{cid}/attachments")
    async def upload_attachment(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """One file per request, parsed from the request stream and charged BEFORE any chunk is
        accepted (`routes/fileupload.py`'s bound — FastAPI's UploadFile spools first, which is
        exactly what this avoids). All types are accepted; the kind is decided from the bytes."""
        if not (ask_store.valid_cid(cid) and await asyncio.to_thread(ask_store.exists, cid)):
            return _missing(cid)
        try:
            name, data = await _read_one_attachment(request)
        except ask_attachments.AttachmentRefused as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        except _UploadMalformed as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        aid = ask_attachments.new_aid()
        try:
            desc = await asyncio.to_thread(ask_attachments.publish, cid, aid, name, data)
        except ask_attachments.AttachmentRefused as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        except ask_store.ConversationNotFound:
            return _missing(cid)
        except ask_store.StoreRefused as e:
            return _refused(e)
        return JSONResponse({"attachment": desc}, status_code=201)

    @app.delete("/api/ask/conversations/{cid}/attachments/{aid}")
    async def discard_attachment(
        cid: str, aid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """Take back a file no message claimed (Remove on a pill, or an upload whose pill went
        away). Frees the staged quota at once. A file some message already carries is NEVER
        discarded — sent history keeps its files (#1720 review)."""
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        status = await asyncio.to_thread(ask_attachments.discard, cid, aid)
        if status == "claimed":
            return JSONResponse(
                {"detail": "that file was already sent — history keeps it"}, status_code=409
            )
        if status == "retry":
            # Retryable (#1720 round 11): the durable marker could not be written. The file is
            # STILL staged and still counts — the client must keep the cleanup pending.
            return JSONResponse(
                {"detail": "the removal could not be recorded — try again"}, status_code=503
            )
        return JSONResponse({"discarded": True})

    @app.get("/api/ask/conversations/{cid}/attachments/{aid}")
    async def get_attachment(
        cid: str, aid: str, request: Request, _user: str = Depends(logged_in)
    ) -> Response:
        """The blob. `?download=1` forces a download; `?meta=1` is the tiny absence probe (the
        descriptor or 404, no bytes) so every kind can reach the *gone* state without a large
        download. An `other` kind is ALWAYS a download, so a shared HTML or SVG can never render
        here; the kind is the one PUBLISH persisted (never re-sniffed from a prefix). Images
        inline, text as `text/plain`. nosniff and no-store throughout."""
        if not ask_store.valid_cid(cid):
            return _missing(cid)
        if request.query_params.get("meta") == "1":
            # Absence detection for every kind, bytes-free. The existence check inside `open_blob`
            # is what rejects a blob whose conversation was deleted (a failed cleanup leaves one
            # on disk; it is still never served).
            try:
                fd, size = await asyncio.to_thread(ask_attachments.open_blob, cid, aid)
            except ask_attachments.AttachmentRefused as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status)
            os.close(fd)
            name, kind = await asyncio.to_thread(ask_attachments.read_meta, cid, aid)
            return JSONResponse(
                {"attachment": {"id": aid, "name": name, "kind": kind, "size": size}},
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        try:
            fd, size = await asyncio.to_thread(ask_attachments.open_blob, cid, aid)
        except ask_attachments.AttachmentRefused as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        name, kind = await asyncio.to_thread(ask_attachments.read_meta, cid, aid)
        download = request.query_params.get("download") == "1" or kind == "other"
        media = {"image": "image/png", "text": "text/plain"}.get(kind, "application/octet-stream")
        if kind == "image":
            # The exact image type only matters for the browser's decoder; read from the first
            # bytes, never from the client's word.
            head = await asyncio.to_thread(lambda: os.pread(fd, 16, 0))
            for sig, _t in (
                (b"\x89PNG", "image/png"),
                (b"\xff\xd8\xff", "image/jpeg"),
                (b"GIF8", "image/gif"),
                (b"RIFF", "image/webp"),
            ):
                if (head or b"").startswith(sig):
                    media = _t
                    break

        def stream():
            try:
                while True:
                    chunk = os.read(fd, 64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                os.close(fd)

        headers = {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'attachment; filename="{_ascii_name(name)}"' if download else "inline"
            ),
        }
        return StreamingResponse(stream(), media_type=media, headers=headers, status_code=200)
