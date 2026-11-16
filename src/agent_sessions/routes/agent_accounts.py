"""Agent account routes (#1820 P1a). All are the signed-in admin's; writes need the CSRF token.

* ``GET    /api/agent-accounts`` — every agent that holds several accounts, with its pool: ids,
  labels, the new-session pick and each home's problems. Never a path, never anything the vendor
  wrote, and nothing is spawned.
* ``POST   /api/agent-accounts/{engine}`` ``{label}`` — add an account and build its home.
* ``PATCH  /api/agent-accounts/{engine}/{account}`` ``{label}`` — rename it.
* ``DELETE /api/agent-accounts/{engine}/{account}`` — forget it. Its home stays on disk.
* ``PUT    /api/agent-accounts/{engine}/order`` ``{order}`` — the pool's order.
* ``PUT    /api/agent-accounts/{engine}/new-sessions`` ``{account}`` — an account id, or
  ``default``: what NEW sessions of this agent start on.
* ``POST   /api/agent-accounts/{engine}/{account}/status`` — ask the vendor's status command
  whether it is signed in (a subprocess: a POST, like the usage refresh).
* ``POST   /api/agent-accounts/{engine}/{account}/signin/cancel`` — end a running sign-in.
* ``WS     /ws/agent-accounts/{engine}/{account}/signin`` — the vendor's own sign-in, on a PTY
  that is no session (`agent_account_signin`).

An agent whose manifest declares no account kind answers 404 on every per-agent route.
"""

from __future__ import annotations

import asyncio
import functools
import json

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket

from .. import agent_account_signin, agent_account_store
from ..auth import origin_matches, session_uid
from ..engines import registry

MAX_BODY = 4096


def _provider(engine: str):
    prov = registry.get(engine)
    if (
        prov is None
        or agent_account_store.agent_accounts.recipe(
            getattr(prov.manifest, "accounts_kind", "none")
        )
        is None
    ):
        raise HTTPException(404, "this agent does not hold several accounts")
    return prov


async def _body(request: Request, *, required: set[str]) -> dict:
    data = await request.body()
    if len(data) > MAX_BODY:
        raise HTTPException(413, "request is too large")
    try:
        value = json.loads(data or b"{}")
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid request") from None
    if not isinstance(value, dict) or set(value) != required:
        raise HTTPException(422, "unexpected or missing fields")
    return value


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(functools.partial(fn, *args))
    except agent_account_store.StoreError as exc:
        raise HTTPException(409, str(exc)) from None


def register(app: FastAPI, *, logged_in, csrf_guard, cfg, must_change) -> None:
    read = [Depends(logged_in)]
    write = [Depends(logged_in), Depends(csrf_guard)]

    @app.get("/api/agent-accounts", dependencies=read)
    async def list_accounts():
        provs = [
            p
            for p in registry.present_providers()
            if agent_account_store.agent_accounts.recipe(
                getattr(p.manifest, "accounts_kind", "none")
            )
            is not None
        ]
        return {"agents": [await _call(agent_account_store.listing, p) for p in provs]}

    @app.post("/api/agent-accounts/{engine}", dependencies=write)
    async def add_account(engine: str, request: Request):
        prov = _provider(engine)
        body = await _body(request, required={"label"})
        return await _call(agent_account_store.add, prov, body["label"])

    @app.put("/api/agent-accounts/{engine}/order", dependencies=write)
    async def reorder_accounts(engine: str, request: Request):
        prov = _provider(engine)
        body = await _body(request, required={"order"})
        await _call(agent_account_store.reorder, prov, body["order"])
        return await _call(agent_account_store.listing, prov)

    @app.put("/api/agent-accounts/{engine}/new-sessions", dependencies=write)
    async def set_new_sessions(engine: str, request: Request):
        prov = _provider(engine)
        body = await _body(request, required={"account"})
        await _call(agent_account_store.set_new_sessions, prov, body["account"])
        return await _call(agent_account_store.listing, prov)

    @app.patch("/api/agent-accounts/{engine}/{account}", dependencies=write)
    async def rename_account(engine: str, account: str, request: Request):
        prov = _provider(engine)
        body = await _body(request, required={"label"})
        await _call(agent_account_store.rename, prov, account, body["label"])
        return await _call(agent_account_store.listing, prov)

    @app.delete("/api/agent-accounts/{engine}/{account}", dependencies=write)
    async def remove_account(engine: str, account: str):
        prov = _provider(engine)
        if agent_account_signin.running(prov.engine_id, account):
            raise HTTPException(409, "a sign-in is running for this account")
        await _call(agent_account_store.remove, prov, account)
        return await _call(agent_account_store.listing, prov)

    @app.post("/api/agent-accounts/{engine}/{account}/status", dependencies=write)
    async def account_status(engine: str, account: str):
        prov = _provider(engine)
        return {"signed_in": await _call(agent_account_store.signed_in, prov, account)}

    @app.post("/api/agent-accounts/{engine}/{account}/signin/cancel", dependencies=write)
    async def cancel_signin(engine: str, account: str):
        prov = _provider(engine)
        return {"closed": await agent_account_signin.close(prov.engine_id, account)}

    @app.websocket("/ws/agent-accounts/{engine}/{account}/signin")
    async def account_signin_socket(ws: WebSocket, engine: str, account: str):
        if session_uid(cfg, ws) != cfg.username or not origin_matches(cfg, ws) or must_change["v"]:
            await ws.close(code=4403)
            return
        prov = registry.get(engine)
        if (
            prov is None
            or agent_account_store.agent_accounts.recipe(
                getattr(prov.manifest, "accounts_kind", "none")
            )
            is None
        ):
            await ws.close(code=4404)
            return
        await ws.accept()
        await agent_account_signin.serve(ws, prov, account)
