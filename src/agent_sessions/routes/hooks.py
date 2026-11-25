"""The webhook address (#2221): ``POST /hooks/w/{webhook_id}`` for a webhook from the list. Read
`docs/invariants/webhooks.md`. The retired review loop's own address (``/hooks/a/…``, #2202,
removed in #2222) answers the uniform 404 for every id.

**The state-changing routes with no session cookie, no CSRF token and no Origin check**, so they
authenticate themselves: an HMAC-SHA256 of the raw body under the webhook's secret, in any common
header form (`webhooks.signature_header`). No provider is configured or needed.

* **Uniform 404.** An unknown or malformed id, a webhook with no secret, and a missing or bad
  signature all answer the same body, each after one HMAC: nothing can be enumerated. A bad
  signature for an existing webhook is counted on it.
* **Bounded.** The body is read incrementally and refused (413) past `forge_hook.BODY_MAX`; a global
  rate limit runs before anything is looked up (429), a per-webhook one after the signature.
* **Recorded once, decided per automation.** A verified delivery updates the webhook's last delivery
  (time, event label, and its SHAPE — field names and types, never values), then each target
  automation decides on its own (`webhook_actions.handle`). A list webhook accepts deliveries
  before any automation uses it, so a test delivery can teach the wizard its fields.
* **No echo.** 202 ``{"accepted": true}``. **Kill switch:** ``AGENT_SESSIONS_AUTOMATION_LOOP=0``
  refuses every delivery (503).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import automation_runner, forge_hook, webhook_actions, webhooks
from .. import automations_store as store

log = logging.getLogger("agent_sessions.routes.hooks")

#: The retired review loop's per-automation address (#2202): every id is the uniform 404 now.
RETIRED_PREFIX = "/hooks/a/"
LIST_PREFIX = "/hooks/w/"
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


def _secrets(wid: str | None) -> list[str]:
    if not wid:
        return []
    try:
        return webhooks.secrets(wid)
    except store.StoreError:
        return []


def _shape(body: bytes) -> list[dict] | None:
    try:
        return webhooks.shape_of(json.loads(body))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _event_label(headers: dict) -> str:
    for name in ("x-forgejo-event", "x-gitea-event", "x-github-event"):
        if headers.get(name):
            return forge_hook.display(headers.get(name))
    return "delivery"


def register(app: FastAPI, *, registry=None) -> None:
    async def _deliver(request: Request, wid: str | None, targets) -> JSONResponse:
        """One signed delivery to webhook ``wid``: verified once, recorded once (time, event label,
        shape — never values), then handed to each target automation, each deciding alone."""
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
        secrets = await asyncio.to_thread(_secrets, wid)
        header = webhooks.signature_header(request.headers)
        if not secrets:
            # The same HMAC an existing webhook would cost, then the same answer.
            forge_hook.signature_ok(body, header, [_DUMMY])
            return _not_found()
        if not forge_hook.signature_ok(body, header, secrets):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(webhooks.note_unsigned, wid)
            return _not_found()
        if not _PER.allow(wid, now):
            return JSONResponse({"detail": "slow down"}, status_code=429, headers=_NO_STORE)
        headers = {k.lower(): v for k, v in request.headers.items()}
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                webhooks.note_delivery, wid, _event_label(headers), "received", shape=_shape(body)
            )
        aids = await asyncio.to_thread(targets)
        for aid in aids:
            task = asyncio.get_running_loop().create_task(
                webhook_actions.handle(aid, body, headers, registry=registry, source=wid),
                name=f"webhook:{wid}:{aid}",
            )
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
        return JSONResponse({"accepted": True}, status_code=202, headers=_NO_STORE)

    @app.post(RETIRED_PREFIX + "{aid}")
    async def retired_hook(aid: str) -> JSONResponse:
        # #2202's per-automation address, retired with the review loop (#2222): nothing is
        # routed, nothing is read, and the answer is the one an unknown webhook gets.
        return _not_found()

    @app.post(LIST_PREFIX + "{wid}")
    async def list_hook(wid: str, request: Request) -> JSONResponse:
        # A webhook from the list (#2221): accepted even before any automation uses it, so its
        # first test delivery can teach the wizard its fields. Each enabled automation that
        # picked it decides on its own.
        known = _ID_RE.match(wid) and await asyncio.to_thread(webhooks.exists_pickable, wid)
        target = wid if known else None
        return await _deliver(request, target, lambda: webhooks.consumers(wid) if known else [])


async def drain(timeout: float = 5.0) -> None:
    """Shutdown: let deliveries already being handled finish (their runs drain separately)."""
    pending = [t for t in _TASKS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
