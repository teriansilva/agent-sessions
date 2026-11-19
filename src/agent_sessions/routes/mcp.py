"""Project MCP server routes (#2163): the Library's registry, per-project enablement and checks.

Every route is authenticated; every mutation also passes the CSRF + Origin guard. No response
ever carries a credential value: a server record holds only variable NAMES, and a check's
details passed `mcp_check.withhold` before they were stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .. import mcp_check, mcp_install, mcp_jobs, mcp_registry, projects
from .templates import _err, _expected, _json, _json_object
from .upload import NO_STORE

_check_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _error(e: mcp_registry.RegistryError) -> JSONResponse:
    status = (
        404
        if isinstance(e, mcp_registry.NotFound)
        else 409
        if isinstance(e, mcp_registry.Conflict)
        else 422
    )
    return JSONResponse({"detail": str(e)}, status_code=status, headers=NO_STORE)


async def _run(fn: Callable, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except mcp_registry.RegistryError as e:
        return _error(e)


def _project_ids() -> list[str]:
    return [pid for pid, p in projects.load().items() if not getattr(p, "archived", False)]


def _summary(rec: dict) -> dict:
    """An index row: the record plus what the list shows (tools, projects, state)."""
    ids = _project_ids()
    st = mcp_registry.status(rec, ids)
    rows = st["projects"]
    enabled = [r for r in rows if r["enabled"]]
    tools = 0
    for c in reversed(mcp_registry.checks(rec["id"])):
        if c["outcome"] == "passed" and c["rev"] == rec["rev"]:
            tools = len(c["tools"])
            break
    if not rec["enabled"]:
        state = {"kind": "disabled"}
    elif any(r["check"] == "failed" for r in enabled):
        failing = [r["project_id"] for r in enabled if r["check"] == "failed"]
        state = {"kind": "failing", "projects": failing}
    elif any(r["credentials"] == "missing" for r in enabled):
        state = {
            "kind": "needs_credentials",
            "projects": [r["project_id"] for r in enabled if r["credentials"] == "missing"],
        }
    elif any(r["check"] in ("stale", "not_run") for r in enabled):
        state = {
            "kind": "needs_check",
            "projects": [r["project_id"] for r in enabled if r["check"] in ("stale", "not_run")],
        }
    elif enabled:
        state = {"kind": "ready"}
    else:
        state = {"kind": "not_enabled"}
    return {
        **mcp_registry.public(rec),
        "tool_count": tools,
        "enabled_projects": [r["project_id"] for r in enabled],
        "project_count": len(ids),
        "state": state,
    }


def _detail(sid: str) -> dict:
    rec = mcp_registry.get(sid)
    return {
        "server": _summary(rec),
        "status": mcp_registry.status(rec, _project_ids()),
        "checks": mcp_registry.checks(sid)[::-1],
    }


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/mcp/servers")
    async def list_servers(_user: str = Depends(logged_in)):
        def run():
            return {"servers": [_summary(r) for r in mcp_registry.list_servers()]}

        result = await _run(run)
        return result if isinstance(result, Response) else _json(result)

    @app.post("/api/mcp/servers", status_code=201)
    async def create_server(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        result = await _run(mcp_registry.create, await _json_object(request))
        return result if isinstance(result, Response) else _json(_summary(result), 201)

    @app.get("/api/mcp/servers/{sid}")
    async def get_server(sid: str, _user: str = Depends(logged_in)):
        result = await _run(_detail, sid)
        return result if isinstance(result, Response) else _json(result)

    @app.patch("/api/mcp/servers/{sid}")
    async def update_server(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        expected = _expected(payload.pop("expected_updated_at", None))
        result = await _run(mcp_registry.update, sid, payload, expected)
        return result if isinstance(result, Response) else _json(_summary(result))

    @app.put("/api/mcp/servers/{sid}/projects")
    async def put_projects(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        expected = _expected(payload.pop("expected_updated_at", None))
        if set(payload) != {"projects"}:
            raise _err(422, "send projects and expected_updated_at")
        result = await _run(mcp_registry.set_projects, sid, payload["projects"], expected)
        return result if isinstance(result, Response) else _json(_summary(result))

    @app.post("/api/mcp/servers/{sid}/check")
    async def check_server(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        scope = payload.get("scope")
        if set(payload) != {"scope"} or not isinstance(scope, str):
            raise _err(422, 'send {"scope": "shared" | <project id>}')
        if scope != mcp_registry.SHARED and scope not in await asyncio.to_thread(_project_ids):
            raise _err(422, "no such project")
        lock = _check_locks.setdefault((sid, scope), asyncio.Lock())
        if lock.locked():
            raise _err(409, "a check of this server for this scope is already running")
        async with lock:
            try:
                row = await mcp_check.run_check(sid, scope)
            except mcp_registry.RegistryError as e:
                return _error(e)
        return _json(row)

    def _started(op: mcp_jobs.Operation) -> JSONResponse:
        return JSONResponse(op.view(), status_code=202, headers=NO_STORE)

    @app.post("/api/mcp/servers/{sid}/review")
    async def review_server(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        """Start resolving a package server's exact version into a lock (an operation; poll
        `GET /api/mcp/operations/{request_id}`). Installs nothing; the result is what would run."""
        payload = await _json_object(request)
        if set(payload) != {"request_id"}:
            raise _err(422, "send a request_id (a UUID)")
        try:
            rec = await asyncio.to_thread(mcp_registry.get, sid)
            if rec["source"]["kind"] not in ("npm", "pypi"):
                raise mcp_registry.RegistryError("only a package server is reviewed")
            op = mcp_jobs.start(
                sid, "review", payload["request_id"], lambda: mcp_install.review(rec)
            )
        except mcp_registry.RegistryError as e:
            return _error(e)
        return _started(op)

    @app.post("/api/mcp/servers/{sid}/install")
    async def install_server(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        """Start installing exactly the reviewed lock ``review_id`` (an operation; poll
        `GET /api/mcp/operations/{request_id}`); its result is the server's summary."""
        payload = await _json_object(request)
        if set(payload) != {"request_id", "review_id"}:
            raise _err(422, "send the review_id of a review and a request_id (a UUID)")
        review_id = payload["review_id"]

        def run() -> dict:
            installed = mcp_install.install(mcp_registry.get(sid), review_id)
            return _summary(mcp_registry.set_installed(sid, installed))

        try:
            await asyncio.to_thread(mcp_registry.get, sid)
            op = mcp_jobs.start(sid, "install", payload["request_id"], run)
        except mcp_registry.RegistryError as e:
            return _error(e)
        return _started(op)

    @app.get("/api/mcp/operations/{op_id}")
    async def mcp_operation(op_id: str, _user: str = Depends(logged_in)):
        """A review or install operation: ``running``, then ``done`` with its result or
        ``failed`` with the reason in words."""
        try:
            return _json(mcp_jobs.get(op_id).view())
        except mcp_registry.RegistryError as e:
            return _error(e)

    @app.delete("/api/mcp/servers/{sid}", status_code=204)
    async def delete_server(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        expected = _expected(request.query_params.get("expected_updated_at"))
        result = await _run(mcp_registry.delete, sid, expected)
        return (
            result if isinstance(result, Response) else Response(status_code=204, headers=NO_STORE)
        )
