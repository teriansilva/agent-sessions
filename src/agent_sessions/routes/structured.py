"""Structured client routes (#1278). All `logged_in`; every mutation also `csrf_guard`.

These expose the transport-neutral facade (``structured_runtime``) for any client whose adapter
implements the operation — HTTP chat or a contained native worker. Request bodies carry only
operator input and caller-chosen UUIDs: no execution authority, worker capability, argv or
mission correlation is ever read from a browser (mission callers use the facade server-side).

* ``GET  /api/structured/clients/{engine}`` — implemented operations and live readiness.
* ``GET  /api/structured/clients/{engine}/models`` — the models the client's CLI reports (#1313).
* ``POST /api/structured/sessions {engine, cwd, operation_id, model?}`` — create; an exact repeat
  of ``operation_id`` returns the same session.
* ``GET  /api/structured/sessions/{key}`` — bounded snapshot (turns, exact pending requests).
* ``GET  /api/structured/sessions/{key}/events?after=&limit=`` — durable journal cursor page.
* ``POST /api/structured/sessions/{key}/turns {operation_id, text, expected_revision?}``
* ``POST /api/structured/sessions/{key}/decisions {decision_id, turn_id, request_id, decision,
  expected_revision?}`` — one exact pending request, allow/deny only.
* ``POST /api/structured/sessions/{key}/interrupt {operation_id, turn_id}``
* ``POST /api/structured/sessions/{key}/stop`` — close the worker; reports proved containment.
* ``GET  /api/structured/sessions/{key}/containment`` — live / gone / unknown.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import structured_runtime


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
            request, {"engine", "cwd", "operation_id", "model"}, {"engine", "cwd", "operation_id"}
        )
        if not isinstance(body["engine"], str):
            raise HTTPException(status_code=422, detail="engine must be a string")
        out = await _run(
            structured_runtime.create_session(
                body["engine"],
                body["cwd"],
                operation_id=body["operation_id"],
                model=body.get("model"),
            )
        )
        return JSONResponse(out, status_code=201)

    @app.get("/api/structured/sessions/{key}")
    async def structured_snapshot(key: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await _run(structured_runtime.snapshot(key)))

    @app.get("/api/structured/sessions/{key}/events")
    async def structured_events(
        key: str, after: int = 0, limit: int = 100, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        return JSONResponse(await _run(structured_runtime.events(key, after=after, limit=limit)))

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
            {"operation_id", "text", "expected_revision", "attachments"},
            {"operation_id", "text"},
        )
        out = await _run(
            structured_runtime.submit_turn(
                key,
                operation_id=body["operation_id"],
                text=body["text"],
                expected_revision=_revision(body.get("expected_revision")),
                # Upload names (#1332 Phase 3); the facade admits each before anything durable.
                attachments=body.get("attachments"),
            )
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
        body = await _json(request, fields | {"expected_revision"}, fields)
        for name in ("turn_id", "request_id", "decision"):
            if not isinstance(body[name], str):
                raise HTTPException(status_code=422, detail=f"{name} must be a string")
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
                )
            )
        )

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
