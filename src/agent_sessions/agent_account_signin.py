"""Sign one of an agent's accounts in, through the vendor's own flow (#1820 P1a).

The transport is the plugin sign-in's (`plugins.signin`, #1259/#1849): binary PTY frames and
strict resize messages only, one socket at a time, a bounded grace window for a socket a phone
browser loses, and nothing sent to a session, scrollback, transcript, log, review or recap. Only
what runs differs: the reviewed recipe's login command, in the account's home, under the live
agent's launch admission (`plugins.process.spawn`, purpose ``account-signin``).

A sign-in is keyed by (agent, account): a second socket for the same account attaches to the one
running, or is refused while another owns it, exactly as for a plugin sign-in. Whether it worked is
asked of the CLI afterwards (`agent_account_store.signed_in`); nothing printed here is read.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket

from . import agent_account_store
from .plugins import process, signin

_LIVE: dict[tuple[str, str], signin._Live] = {}
#: Refused before anything ran (no such account, a broken home, another sign-in starting).
REFUSED = 4409


async def close(engine: str, account_id: str, *, timeout: float = 10) -> bool:
    """End a running sign-in now (the operator's explicit cancel). True when one was running."""
    live = _LIVE.get((engine, account_id))
    if live is None:
        return False
    live.closing.set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(live.ended.wait(), timeout)
    return True


def running(engine: str, account_id: str) -> bool:
    return (engine, account_id) in _LIVE


async def serve(ws: WebSocket, prov, account_id: str) -> str:
    """Drive one account's sign-in over ``ws`` (already accepted). Returns the outcome state."""
    key = (prov.engine_id, account_id)
    live = _LIVE.get(key)
    if live is not None:
        await signin._attach(live, ws)
        return "attached"
    try:
        account = await asyncio.to_thread(agent_account_store.for_signin, prov, account_id)
    except agent_account_store.StoreError as exc:
        with contextlib.suppress(Exception):
            await ws.close(code=REFUSED, reason=str(exc)[:120])
        return "refused"
    if key in _LIVE:  # another socket started it while this one looked the account up
        await signin._attach(_LIVE[key], ws)
        return "attached"
    live = signin._Live()
    _LIVE[key] = live
    state = "interrupted"
    try:
        async with process.spawn(
            prov, "account-signin", cwd=account.home, account=account
        ) as terminal:
            state, _error = await signin._drive(live, terminal, ws)
    except process.CleanupError:
        state = "cleanup_pending"
    except asyncio.CancelledError:
        raise
    except Exception:
        state = "failed"
    finally:
        if _LIVE.get(key) is live:
            del _LIVE[key]
        live.ended.set()
        code = 1000 if state == "complete" else REFUSED
        for sock in [ws, *(s for s, _ in live.offered)]:
            with contextlib.suppress(Exception):
                await sock.close(code=code)
        for _, released in live.offered:
            if not released.done():
                released.set_result(None)
    return state
