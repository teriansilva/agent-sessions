"""Paged transcript history route (#348 Phase 3): ``GET /api/sessions/{sid}/history``.

Serves older conversation content for scroll-up lazy-load. Read-only + authed
(``logged_in``); a GET, so there is no CSRF surface. The websocket attach payload and
the ``have``/``seq`` delta-resume contract are untouched — this is purely additive.

Response shape (always 200 for a resolvable session):

    {"ansi": "<utf-8 rendered ANSI block>", "cursor": <int|null>, "has_more": <bool>}

``cursor`` is a stable per-engine TURN index (see :mod:`agent_sessions.history`), never
a rendered-line offset — width changes and cap changes cannot shift it. Engines without
a transcript adapter answer the empty shape (``has_more=false``), not an error, so the
client can show end-of-history.

Concurrency: ONE in-flight render per session key. A second request while a render is
running gets **429** (documented contract — the client enforces single-inflight too, so
a 429 only fires on a misbehaving/racing client; it retries on the next scroll). The
check-and-set happens on the event loop before the executor hop, so it is race-free.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import engines, history, transcript

# Session keys with a history render currently in the thread pool. Mutated only on the
# event loop (before/after the executor await), so membership checks can't race.
_INFLIGHT: set[str] = set()


def register(app: FastAPI, *, logged_in) -> None:
    @app.get("/api/sessions/{sid}/history")
    async def session_history(
        sid: str,
        _: str = Depends(logged_in),
        before: int | None = Query(None, ge=0),
        lines: int | None = Query(None, ge=1),
        # No le= bound: very wide clients legitimately report cols>500 (the ws grid
        # CLAMPS to 500 rather than rejecting — mirror that, or every wide terminal
        # gets a 422 → permanent error pill instead of history).
        cols: int = Query(80, ge=1),
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        # Transcript adapters key off the REAL native id (opencode `message.session_id`,
        # codex rollout filename) — so the lookup uses `native` AS REQUESTED. Do NOT map
        # through engines.physical_key(): that resolves a reconciled real id back to its
        # `new-…` placeholder, which is right for LIVE resources (socket/lock/buffer) but
        # this route touches none — mapping here made an alias-backed session's history
        # query the placeholder id and come back empty (Hermes #365 finding 2).
        engine_id = prov.engine_id
        key = f"{engine_id}:{native}"
        if transcript.adapter_for(engine_id) is None:
            # No transcript store for this engine → a clean end-of-history, not an error.
            return JSONResponse({"ansi": "", "cursor": None, "has_more": False})
        if key in _INFLIGHT:
            raise HTTPException(status_code=429, detail="history render already in flight")
        _INFLIGHT.add(key)
        try:
            loop = asyncio.get_event_loop()
            page = await loop.run_in_executor(
                None,
                lambda: history.fetch_page(
                    engine_id, native, before=before, cols=max(20, min(500, cols)), lines=lines
                ),
            )
        finally:
            _INFLIGHT.discard(key)
        return JSONResponse(
            {
                "ansi": page.ansi.decode("utf-8", "replace"),
                "cursor": page.cursor,
                "has_more": page.has_more,
            }
        )
