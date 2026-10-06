"""Playbook gallery + local authoring routes (#1191 PR 1) — `/api/playbooks`.

* ``GET    /api/playbooks``                    — every source's cards (fail-soft per bundle) +
  ``default``
* ``POST   /api/playbooks``                    — create a ``local`` playbook ``{files}`` → 201
* ``GET    /api/playbooks/{pid}``              — detail: card, ``files``, ``documents``, README
* ``PUT    /api/playbooks/{pid}``              — replace a local playbook ``{revision, files}``
* ``DELETE /api/playbooks/{pid}?revision=…``   — delete a local playbook (refused while projects
  run it)
* ``POST   /api/playbooks/{pid}/duplicate``    — ``{revision?, id?, name?}`` → a new local one, 201
* ``PUT    /api/playbooks/{pid}/default``      — ``{revision, expect_default}`` → set the default
* ``DELETE /api/playbooks/{pid}/default``      — clear the default (only if it is ``pid``)
* ``POST   /api/playbooks/{pid}/review``       — resolve unsaved deployment inputs; no effects
* ``POST   /api/playbooks/{pid}/review/confirm`` — confirm named targets on the exact review

Reads need a session; every write needs the session AND ``csrf_guard`` (which carries the
Origin/Referer check). Every response is ``no-store``. Each handler reads its own input — the path
id, and a body or ``revision`` query value it validates itself — so FastAPI binds nothing else from
the request (pinned by a route audit test). The store is blocking and owns its lock from acquire
to release on ONE worker thread, so a cancelled request never strands it.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..playbooks import review, store

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/playbooks"
#: A bundle is at most 8 MiB of files; base64 and JSON escaping inflate that, so the body cap is
#: twice it. Anything past this is refused while it streams in, never buffered first.
BODY_MAX = 2 * 8 * 1024 * 1024


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _err(e: store.StoreError) -> JSONResponse:
    return _json({"detail": e.detail, **e.extra}, e.status)


async def _body(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise store.StoreError("request body is too large", status=413)
    # READ INCREMENTALLY: a chunked body has no Content-Length, and `request.body()` would buffer
    # all of it before any size check. Stop at the first chunk past the cap.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise store.StoreError("request body is too large", status=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise store.StoreError("request body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise store.StoreError("request body must be a JSON object")
    return payload


def _only(body: dict, allowed: set[str], required: set[str], what: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise store.StoreError(f"{what} does not take {', '.join(unknown)}")
    missing = sorted(required - set(body))
    if missing:
        raise store.StoreError(f"{what} needs {', '.join(missing)}")


def _create(body: dict) -> dict:
    _only(body, {"files"}, {"files"}, "create")
    return store.create_playbook(body["files"])


def _update(pid: str, body: dict) -> dict:
    _only(body, {"revision", "files"}, {"revision", "files"}, "a save")
    return store.update_playbook(pid, store.revision(body["revision"]), body["files"])


def _set_default(pid: str, body: dict) -> dict:
    _only(body, {"revision", "expect_default"}, {"revision", "expect_default"}, "set default")
    return store.set_default(pid, store.revision(body["revision"]), body["expect_default"])


def register(app: FastAPI, *, logged_in, csrf_guard, signing_key: str) -> None:
    @app.middleware("http")
    async def _playbooks_are_never_cached(request: Request, call_next):
        path = request.url.path
        if not (path == PREFIX or path.startswith(PREFIX + "/")):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            return _json({"detail": "the playbook store failed"}, 500)
        response.headers.update(NO_STORE)
        return response

    async def _run(fn, *args):
        """Off the event loop. The store takes AND releases its lock on this one worker thread;
        if the request is cancelled the thread still runs to its `finally`."""
        try:
            return await asyncio.to_thread(fn, *args)
        except store.StoreError as e:
            return _err(e)

    async def _read_body(request: Request):
        try:
            return await _body(request)
        except store.StoreError as e:
            return _err(e)

    @app.get(PREFIX)
    async def list_playbooks(_user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(store.list_playbooks)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.post(PREFIX)
    async def create_playbook(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(_create, body)
        return out if isinstance(out, JSONResponse) else _json(out, 201)

    @app.get(PREFIX + "/{pid}")
    async def get_playbook(pid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(store.get_playbook, pid)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.put(PREFIX + "/{pid}")
    async def update_playbook(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # The SOURCE decides first: a bundled/catalog playbook is a 403 before its body is read.
        gate = await _run(store.require_writable, pid)
        if isinstance(gate, JSONResponse):
            return gate
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(_update, pid, body)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.delete(PREFIX + "/{pid}")
    async def delete_playbook(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        raw = request.query_params.get("revision")
        out = await _run(store.delete_playbook, pid, raw)
        # A committed delete is reported as one even when its state could not be flushed
        # (`state_durable: false` + the reason) — a write that happened never answers "failed".
        return out if isinstance(out, JSONResponse) else _json({"deleted": pid, **out})

    @app.post(PREFIX + "/{pid}/duplicate")
    async def duplicate_playbook(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(store.duplicate_playbook, pid, body)
        return out if isinstance(out, JSONResponse) else _json(out, 201)

    @app.put(PREFIX + "/{pid}/default")
    async def set_default_playbook(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(_set_default, pid, body)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.delete(PREFIX + "/{pid}/default")
    async def clear_default_playbook(
        pid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        out = await _run(store.clear_default, pid)
        return out if isinstance(out, JSONResponse) else _json(out)

    def _review(pid: str, body: dict, confirm: bool) -> dict:
        if not confirm:
            return review.build(pid, body, key=signing_key).public
        _only(
            body, {"inputs", "digest", "targets"}, {"inputs", "digest", "targets"}, "confirmation"
        )
        plan = review.build(pid, body["inputs"], key=signing_key)
        return review.confirm(plan, body["digest"], body["targets"], key=signing_key)

    @app.post(PREFIX + "/{pid}/review")
    async def review_playbook(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(_review, pid, body, False)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.post(PREFIX + "/{pid}/review/confirm")
    async def confirm_playbook_review(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _read_body(request)
        if isinstance(body, JSONResponse):
            return body
        out = await _run(_review, pid, body, True)
        return out if isinstance(out, JSONResponse) else _json(out)
