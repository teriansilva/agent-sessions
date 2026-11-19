"""``POST /hooks/a/{automation_id}`` — the review loop's Forgejo webhook (#2202).

**The one state-changing route with no session cookie, no CSRF token and no Origin check**, so it
authenticates itself: the body must carry a valid ``X-Forgejo-Signature`` (hex HMAC-SHA256 under
the automation's webhook secret). Read `docs/invariants/forge-review-loop.md`.

* **Uniform 404.** An unknown id, a malformed id, an automation that is not an enabled, approved
  webhook automation, one with no secret yet, and a missing or bad signature all answer the same
  body. Each path computes one HMAC, so they share a timing class: nothing can be enumerated.
* **Bounded.** The body is read incrementally and refused (413) at the first byte past
  `forge_hook.BODY_MAX`; a global rate limit runs before anything is looked up (429), and a
  per-automation one after the signature is proved.
* **No echo.** An accepted delivery is 202 ``{"accepted": true}``; nothing of the payload, the
  automation or its runs is returned. The work happens in the background
  (`review_loop_runner.handle`), where every outcome is recorded on the automation.
* **Kill switch.** ``AGENT_SESSIONS_AUTOMATION_LOOP=0`` refuses every delivery (503).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import logging
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import automation_runner, forge_hook, review_loop_runner
from .. import automations_store as store

log = logging.getLogger("agent_sessions.routes.hooks")

PREFIX = "/hooks/a/"
_ID_RE = re.compile(r"^[a-z0-9]{1,64}$")
#: Deliveries a minute: per automation (after the signature) and across all of them (before).
PER_AUTOMATION_PER_MIN = 30
GLOBAL_PER_MIN = 120
_NOT_FOUND = {"detail": "Not Found"}
_NO_STORE = {"Cache-Control": "no-store"}
#: A stand-in secret, so a delivery for nothing still costs one HMAC (the same timing class).
_DUMMY = hashlib.sha256(b"agent-sessions hooks: no such automation").hexdigest()

_TASKS: set[asyncio.Task] = set()


class _Window:
    """A sliding one-minute window of arrivals. Process-local: a second instance has its own."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._seen: dict[str, collections.deque] = {}

    def allow(self, key: str, now: float) -> bool:
        q = self._seen.setdefault(key, collections.deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._seen) > 4096:  # bounded: forget idle keys
            for k in [k for k, v in self._seen.items() if not v]:
                del self._seen[k]
        return True


_GLOBAL = _Window(GLOBAL_PER_MIN)
_PER = _Window(PER_AUTOMATION_PER_MIN)


def reset_for_tests() -> None:
    _GLOBAL._seen.clear()
    _PER._seen.clear()


def _not_found() -> JSONResponse:
    return JSONResponse(_NOT_FOUND, status_code=404, headers=_NO_STORE)


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None past `forge_hook.BODY_MAX` (read incrementally, never buffered past it)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > forge_hook.BODY_MAX:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _secrets_for(aid: str) -> list[str]:
    """The secrets that verify a delivery for ``aid`` — empty unless it is an enabled, approved,
    runnable webhook automation. Every refusal looks the same to the caller. BLOCKING."""
    if not _ID_RE.match(aid):
        return []
    try:
        row = store.get(aid)
    except store.StoreError:
        return []
    config = row.get("config")
    if (
        config is None
        or config["trigger"]["kind"] != "webhook"
        or not row["enabled"]
        or row["consented_at"] is None
        or row["needs_reapproval"]
    ):
        return []
    try:
        return store.hook_secrets(aid)
    except store.StoreError:
        return []


def register(app: FastAPI, *, registry=None) -> None:
    @app.post(PREFIX + "{aid}")
    async def forgejo_hook(aid: str, request: Request) -> JSONResponse:
        now = time.monotonic()
        if not _GLOBAL.allow("*", now):
            return JSONResponse({"detail": "slow down"}, status_code=429, headers=_NO_STORE)
        if not automation_runner.loop_enabled():
            return JSONResponse(
                {"detail": "automations are switched off on this server"},
                status_code=503,
                headers=_NO_STORE,
            )
        body = await _read_capped(request)
        if body is None:
            return JSONResponse({"detail": "too large"}, status_code=413, headers=_NO_STORE)
        secrets = await asyncio.to_thread(_secrets_for, aid)
        header = request.headers.get(forge_hook.SIGNATURE_HEADER)
        if not secrets:
            # The same HMAC an existing automation would cost, then the same answer.
            forge_hook.signature_ok(body, header, [_DUMMY])
            return _not_found()
        if not forge_hook.signature_ok(body, header, secrets):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.note_unsigned, aid)
            return _not_found()
        if not _PER.allow(aid, now):
            return JSONResponse({"detail": "slow down"}, status_code=429, headers=_NO_STORE)
        headers = {k.lower(): v for k, v in request.headers.items()}
        task = asyncio.get_running_loop().create_task(
            review_loop_runner.handle(aid, body, headers, registry=registry),
            name=f"forgejo-hook:{aid}",
        )
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return JSONResponse({"accepted": True}, status_code=202, headers=_NO_STORE)


async def drain(timeout: float = 5.0) -> None:
    """Shutdown: let deliveries already being handled finish (their runs drain separately)."""
    pending = [t for t in _TASKS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
