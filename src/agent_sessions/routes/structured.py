"""Structured client routes (#1278). All `logged_in`; every mutation also `csrf_guard`.

These expose the transport-neutral facade (``structured_runtime``) for any client whose adapter
implements the operation — HTTP chat or a contained native worker. Request bodies carry only
operator input and caller-chosen UUIDs: no execution authority, worker capability, argv or
mission correlation is ever read from a browser (mission callers use the facade server-side).

* ``GET  /api/structured/clients/{engine}`` — implemented operations and live readiness.
* ``GET  /api/structured/clients/{engine}/models`` — the models the client's CLI reports (#1313).
* ``POST /api/structured/sessions {engine, cwd, operation_id, model?, bypass?, specialist?}`` —
  create; an exact repeat of ``operation_id`` returns the same session (a ``bypass`` one waits for
  ``start``). ``specialist`` is ``{id, version}`` (#1693): every generation re-reserves it.
  It answers once the worker is launched, not ready (#1505): the snapshot's ``launch`` view
  (``{gen, state, reason, since}``) turns ``ready`` or ``failed`` by itself.
* ``POST /api/structured/sessions/{key}/start`` — launch a skip-permissions creation (#1339);
  its create reserved and launched nothing. Idempotent. Answers at launch too (#1505).
* ``GET  /api/structured/sessions/{key}`` — bounded snapshot (turns, exact pending requests).
* ``GET  /api/structured/sessions/{key}/events?after=&limit=&wait=`` — durable journal cursor page;
  ``wait`` (≤ 3 s) holds a read with nothing new until the journal moves (#1727).
* ``GET  /api/structured/sessions/{key}/history?before=&limit=`` — the turns just older than one
  turn, for scrolling up past the snapshot's window (#1504); native clients only.
* ``POST /api/structured/sessions/{key}/turns {operation_id, text, expected_revision?,
  reply_to?}`` — ``reply_to``: the async question this message answers (#1463), admitted only
  while that question is still open.
* ``POST /api/structured/sessions/{key}/decisions {decision_id, turn_id, request_id, decision,
  grant?, expected_revision?}`` — one exact pending request: approve / reject / cancel, or
  ``always`` with the id of one proposed standing grant the snapshot offers (#1339).
* ``POST /api/structured/sessions/{key}/interrupt {operation_id, turn_id}``
* ``POST /api/structured/sessions/{key}/send-now {operation_id, turn_id, queued_turn_id}`` —
  steer or explicitly cancel the observed response and send the selected queued input next.
* ``POST /api/structured/sessions/{key}/withdraw {operation_id, queued_turn_id}`` — remove one
  still-queued message before it is sent (#1447).
* ``POST /api/structured/sessions/{key}/stop`` — close the worker; reports proved containment.
* ``GET  /api/structured/sessions/{key}/containment`` — live / gone / unknown.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import native_ipc, share_api, shares, structured_runtime

log = logging.getLogger("agent_sessions.routes.structured")


async def _json(request: Request, allowed: set[str], required: set[str]) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
    missing = sorted(required - set(body))
    if missing:
        raise HTTPException(status_code=422, detail=f"missing fields: {missing}")
    return body


def _revision(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise HTTPException(status_code=422, detail="expected_revision must be an integer")
    return value


def _specialist(raw: object) -> tuple[str, int] | None:
    from .. import specialist_launch

    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"id", "version"}:
        raise HTTPException(status_code=422, detail="specialist must be {id, version}")
    try:
        parsed = specialist_launch.parse(raw["id"], raw["version"])
    except specialist_launch.Refused as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from None
    if parsed is None:  # an empty `{id, version}` is not "no specialist"
        raise HTTPException(status_code=422, detail="specialist must be {id, version}")
    return parsed


async def _until_gone(receive) -> None:
    """Returns when the client hangs up (#1727). Not `Request.is_disconnected()`: under the app's
    `@app.middleware("http")` layers its pre-cancelled read loses the disconnect message every
    time, so a waiting read would hold its slot to the end of its wait. A plain read does not."""
    while (await receive())["type"] != "http.disconnect":
        pass


async def _run(coroutine):
    try:
        return await coroutine
    except structured_runtime.StructuredError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/structured/clients/{engine}")
    async def structured_client(engine: str, _user: str = Depends(logged_in)) -> JSONResponse:
        import asyncio

        descriptor = await asyncio.to_thread(structured_runtime.describe, engine)
        return JSONResponse(descriptor.as_dict())

    @app.get("/api/structured/clients/{engine}/models")
    async def structured_models(engine: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # What the client's own CLI says it can run (#1313); `unavailable` offers `default` only.
        import asyncio

        from .. import engines, native_models

        prov = engines.get(engine)
        if prov is None or getattr(prov.manifest, "runtime", None) != "api":
            raise HTTPException(status_code=404, detail="no such API client")
        listing = await asyncio.to_thread(native_models.models, prov)
        return JSONResponse(listing.as_dict())

    @app.post("/api/structured/sessions")
    async def structured_create(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _json(
            request,
            {"engine", "cwd", "operation_id", "model", "effort", "bypass", "specialist", "choice"},
            {"engine", "cwd", "operation_id"},
        )
        if not isinstance(body["engine"], str):
            raise HTTPException(status_code=422, detail="engine must be a string")
        # API first (#1728): a create of the AUTOMATIC default the form showed is refused, with the
        # new default, once its family's default moved; an explicit choice is honoured as before.
        from .. import engine_choice

        stale = await engine_choice.refuse_stale(
            body["engine"], body.get("choice"), body["operation_id"]
        )
        if stale is not None:
            return stale
        if not isinstance(body.get("bypass", False), bool):
            raise HTTPException(status_code=422, detail="bypass must be a boolean")
        if body.get("effort") is not None and not isinstance(body["effort"], str):
            raise HTTPException(status_code=422, detail="effort must be a string")
        # Started from a specialist (#1693 S4b): `{id, version}`, re-validated where it launches.
        specialist = _specialist(body.get("specialist"))
        out = await _run(
            structured_runtime.create_session(
                body["engine"],
                body["cwd"],
                operation_id=body["operation_id"],
                model=body.get("model"),
                effort=body.get("effort"),
                bypass=body.get("bypass", False),
                **({"specialist": specialist} if specialist is not None else {}),
                # The page opens on launch, not on ready (#1505): the snapshot's `launch` view
                # reports readiness. Only this route and `/start` below pass False.
                wait_ready=False,
            )
        )
        return JSONResponse(out, status_code=201)

    @app.post("/api/structured/sessions/{key}/start")
    async def structured_start(
        key: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # The second phase of a skip-permissions creation (#1339): the SPA calls it only after it
        # has received the create response, so a lost response can never launch unobserved.
        # It answers at launch (#1505); a refusal before any worker exists is recorded in the
        # snapshot's `launch` view too, so a lost answer still has a truthful state to read.
        return JSONResponse(await _run(structured_runtime.start_session(key, wait_ready=False)))

    async def _attributed(view: dict, key: str) -> dict:
        # A visitor's turns (#1404 P2) carry their self-chosen name in the operator's view too.
        try:
            authors = await asyncio.to_thread(
                shares.store().authors, view.get("session_key") or key
            )
        except Exception:  # the share store must never take the operator's own view down…
            log.warning("structured view: visitor attribution unavailable", exc_info=True)
            authors = None  # …but then every turn is UNKNOWN, never labelled the operator's
        return share_api.overlay_authors(view, authors)

    @app.get("/api/structured/sessions/{key}")
    async def structured_snapshot(key: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # The operator's own view: a running turn's message in progress shows too (#1727).
        snap = await _run(structured_runtime.snapshot(key, live_text=True))
        return JSONResponse(await _attributed(snap, key))

    @app.get("/api/structured/sessions/{key}/history")
    async def structured_history(
        key: str, before: str, limit: int = 50, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        # Older turns get the snapshot's attribution, failing closed the same way (#1504).
        # The operator's own pages keep a stopped turn's streamed words, as its snapshot (#1727).
        page = await _run(
            structured_runtime.history(key, before=before, limit=limit, live_text=True)
        )
        return JSONResponse(await _attributed(page, key))

    @app.post("/api/structured/sessions/{key}/skip-permissions")
    async def structured_enable_bypass(
        key: str,
        request: Request,
        user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(request, {"operation_id"}, {"operation_id"})
        return JSONResponse(
            await _run(
                structured_runtime.enable_bypass(key, operation_id=body["operation_id"], user=user)
            )
        )

    @app.get("/api/structured/sessions/{key}/events")
    async def structured_events(
        key: str,
        request: Request,
        after: int = 0,
        limit: int = 100,
        wait: float = 0,
        _user: str = Depends(logged_in),
    ) -> JSONResponse:
        # `wait` (#1727): hold a read with nothing past `after` until the journal moves, at most
        # `EVENTS_WAIT_MAX` (3 s) whatever is asked. Operator route only: the public share route
        # never waits and never carries `data`.
        watch = asyncio.ensure_future(_until_gone(request.receive)) if wait > 0 else None
        try:
            page = await _run(
                structured_runtime.wait_events(
                    key,
                    after=after,
                    limit=limit,
                    wait=wait,
                    disconnected=watch.done if watch is not None else None,
                )
            )
        finally:
            if watch is not None:
                watch.cancel()
        return JSONResponse(page)

    @app.get("/api/structured/sessions/{key}/containment")
    async def structured_probe(key: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await _run(structured_runtime.probe(key)))

    @app.post("/api/structured/sessions/{key}/turns")
    async def structured_submit(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(
            request,
            {"operation_id", "text", "expected_revision", "attachments", "reply_to"},
            {"operation_id", "text"},
        )
        try:
            out = await structured_runtime.submit_turn(
                key,
                operation_id=body["operation_id"],
                text=body["text"],
                expected_revision=_revision(body.get("expected_revision")),
                # Upload names (#1332 Phase 3); the facade admits each before anything durable.
                attachments=body.get("attachments"),
                reply_to=body.get("reply_to"),
            )
        except structured_runtime.StructuredError as exc:
            if exc.reason != native_ipc.JOURNAL_UNWRITABLE:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            # A known refusal before any claim (#1733): the pane keeps the draft and offers a NEW
            # send, never a Retry of an uncertain one. A closed reason, never free text.
            return JSONResponse(
                {"detail": exc.detail, "reason": exc.reason}, status_code=exc.status
            )
        return JSONResponse(out, status_code=202 if out.get("state") == "running" else 200)

    @app.post("/api/structured/sessions/{key}/decisions")
    async def structured_decide(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"decision_id", "turn_id", "request_id", "decision"}
        body = await _json(request, fields | {"expected_revision", "grant", "answers"}, fields)
        for name in ("turn_id", "request_id", "decision"):
            if not isinstance(body[name], str):
                raise HTTPException(status_code=422, detail=f"{name} must be a string")
        if "grant" in body and not isinstance(body["grant"], str):
            raise HTTPException(status_code=422, detail="grant must be a string")
        # An agent question's answer (#1463): only with `answer`; its shape and the offered
        # options are checked by the runtime and again by the worker.
        if ("answers" in body) != (body["decision"] == "answer"):
            raise HTTPException(
                status_code=422, detail="answers go with an answer decision, and only with one"
            )
        if "answers" in body and not isinstance(body["answers"], list):
            raise HTTPException(status_code=422, detail="answers must be a list")
        return JSONResponse(
            await _run(
                structured_runtime.decide(
                    key,
                    turn_id=body["turn_id"],
                    request_id=body["request_id"],
                    decision_id=body["decision_id"],
                    decision=body["decision"],
                    user=_user,
                    expected_revision=_revision(body.get("expected_revision")),
                    grant=body.get("grant"),
                    answers=body.get("answers"),
                )
            )
        )

    @app.post("/api/structured/sessions/{key}/send-now")
    async def structured_send_now(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"operation_id", "turn_id", "queued_turn_id"}
        body = await _json(request, fields | {"stop_all"}, fields)
        try:
            result = await structured_runtime.send_now(key, **body)
        except structured_runtime.StructuredError as exc:
            if exc.reason is None:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            # A closed reason the pane acts on (#1644): Wait or Stop all and send, or confirm
            # again. Never free text.
            return JSONResponse(
                {"detail": exc.detail, "reason": exc.reason}, status_code=exc.status
            )
        return JSONResponse(result, status_code=202)

    @app.post("/api/structured/sessions/{key}/withdraw")
    async def structured_withdraw(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"operation_id", "queued_turn_id"}
        body = await _json(request, fields, fields)
        return JSONResponse(await _run(structured_runtime.withdraw(key, **body)), status_code=202)

    @app.post("/api/structured/sessions/{key}/interrupt")
    async def structured_interrupt(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(request, {"operation_id", "turn_id"}, {"operation_id", "turn_id"})
        return JSONResponse(
            await _run(
                structured_runtime.interrupt(
                    key, operation_id=body["operation_id"], turn_id=body["turn_id"]
                )
            ),
            status_code=202,
        )

    @app.post("/api/structured/sessions/{key}/stop")
    async def structured_stop(
        key: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return JSONResponse(await _run(structured_runtime.stop(key)))
