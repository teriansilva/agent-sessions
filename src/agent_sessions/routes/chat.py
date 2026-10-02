"""Routes for `chat`-runtime agents (#853 P9a, #1209). All `logged_in`; every mutation also
`csrf_guard` (CSRF token + Origin). No route returns an API key or its envelope.

* ``POST /api/chat/new {engine, cwd}`` — start a conversation (pinned id). 409 when the agent has no
  endpoint configured. ``cwd`` is metadata for project grouping only: nothing runs there.
* ``GET  /api/chat/{sid}`` — the conversation, with each turn's state. Reading is how a client
  recovers after a reload or a lost response; a turn left pending by a restart settles here.
* ``POST /api/chat/{sid}/messages {turn_id, text}`` — 202 ``pending``. Idempotent on ``turn_id``:
  a repeat returns the settled result or 409 while in flight; a reused id with different text is
  409. 413 when the message cannot fit the endpoint's context window.
* ``POST /api/chat/{sid}/turns/{turn_id}/retry`` — re-send the SAME failed turn (no copy).
* ``POST /api/chat/{sid}/turns/{turn_id}/proposals/{proposal_id}/decide {decision}`` —
  approve or reject the exact stored proposal. Repeated decisions return its durable outcome.
* ``GET/PATCH /api/agents/{engine}/endpoint`` — the agent's endpoint (public view; the key is only
  ever written). 422 on a patch the origin policy refuses (#956) or that leaves no budget.
* ``POST /api/agents/{engine}/endpoint/test {base_url, api_key?}`` — check a DRAFT; saves nothing.
  A refused draft (another origin, no new key) makes no outbound request.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import chat_config, chat_runtime, engines, prefs, review
from ..engines.base import EngineError


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    return body


def _chat_key(sid: str) -> tuple[str, str]:
    try:
        prov, native = engines.parse_key(sid)
    except EngineError:
        raise HTTPException(status_code=404, detail="no such conversation") from None
    if prov.manifest.runtime != "chat":
        raise HTTPException(status_code=409, detail="not a chat conversation")
    return prov.engine_id, native


def _chat_engine(engine_id: str) -> str:
    prov = engines.get(engine_id)
    if prov is None or prov.manifest.runtime != "chat":
        raise HTTPException(status_code=404, detail="no such chat agent")
    return engine_id


def _refused(e: chat_runtime.ChatError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.post("/api/chat/new")
    async def chat_new(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _json(request)
        unknown = sorted(set(body) - {"engine", "cwd"})
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
        engine_id = body.get("engine")
        if not isinstance(engine_id, str):
            raise HTTPException(status_code=422, detail="engine must be a string")
        _chat_engine(engine_id)
        try:
            sid = await chat_runtime.new_session(engine_id, body.get("cwd"))
        except chat_runtime.ChatError as e:
            raise _refused(e) from None
        return JSONResponse({"id": f"{engine_id}:{sid}"}, status_code=201)

    @app.get("/api/chat/{sid}")
    async def chat_get(sid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        engine_id, native = _chat_key(sid)
        try:
            return JSONResponse(await chat_runtime.get_session(engine_id, native))
        except chat_runtime.ChatError as e:
            raise _refused(e) from None

    @app.post("/api/chat/{sid}/messages")
    async def chat_send(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        engine_id, native = _chat_key(sid)
        body = await _json(request)
        unknown = sorted(set(body) - {"turn_id", "text"})
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
        try:
            out = await chat_runtime.send(engine_id, native, body.get("turn_id"), body.get("text"))
        except chat_runtime.ChatError as e:
            raise _refused(e) from None
        return JSONResponse(out, status_code=202 if out["turn"]["status"] == "pending" else 200)

    @app.post("/api/chat/{sid}/turns/{turn_id}/retry")
    async def chat_retry(
        sid: str,
        turn_id: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        engine_id, native = _chat_key(sid)
        try:
            out = await chat_runtime.retry(engine_id, native, turn_id)
        except chat_runtime.ChatError as e:
            raise _refused(e) from None
        return JSONResponse(out, status_code=202)

    @app.post("/api/chat/{sid}/turns/{turn_id}/proposals/{proposal_id}/decide")
    async def chat_decide(
        sid: str,
        turn_id: str,
        proposal_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        engine_id, native = _chat_key(sid)
        body = await _json(request)
        if set(body) != {"decision"}:
            raise HTTPException(status_code=422, detail="expected only decision")
        try:
            out = await chat_runtime.decide(
                engine_id,
                native,
                turn_id,
                proposal_id,
                body["decision"],
                _user,
            )
        except chat_runtime.ChatError as e:
            raise _refused(e) from None
        return JSONResponse(out)

    @app.get("/api/agents/{engine_id}/endpoint")
    async def agent_endpoint(engine_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(chat_config.public(_chat_engine(engine_id)))

    @app.patch("/api/agents/{engine_id}/endpoint")
    async def agent_endpoint_set(
        engine_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        _chat_engine(engine_id)
        body = await _json(request)
        try:
            return JSONResponse(chat_config.set_config(engine_id, body))
        except (chat_config.ChatConfigError, prefs.KeyOriginError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from None

    @app.post("/api/agents/{engine_id}/endpoint/test")
    async def agent_endpoint_test(
        engine_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        _chat_engine(engine_id)
        body = await _json(request)
        return await test_endpoint_draft(engine_id, body)


async def test_endpoint_draft(engine_id: str, body: dict) -> JSONResponse:
    unknown = sorted(set(body) - {"base_url", "api_key"})
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
    base_url = body.get("base_url")
    if not isinstance(base_url, str) or not prefs.is_valid_ai_base_url(base_url):
        raise HTTPException(status_code=422, detail="base_url must be an http(s) URL")
    api_key = body.get("api_key")
    if "api_key" in body and not isinstance(api_key, str):
        raise HTTPException(status_code=422, detail="api_key must be a string")
    if isinstance(api_key, str) and len(api_key) > prefs.AI_REVIEW_KEY_MAX:
        raise HTTPException(status_code=422, detail="api_key is too long")
    draft, why = chat_config.draft_for_test(engine_id, base_url, api_key)
    if why is not None:
        raise HTTPException(status_code=422, detail=why)
    try:
        models = await review.list_models(force=True, cfg=draft)
    except review.ModelsUnsupportedError:
        return JSONResponse({"models": [], "listing": "unsupported"})
    except review.ReviewError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    return JSONResponse({"models": models, "listing": "ok"})
