"""Per-agent usage routes (#839).

* ``GET   /api/agents/usage`` — one row per agent: what it reports, when it was asked, whether
  that answer is stale, and the operator's own limits. Never probes; serving a page must not
  depend on six CLIs answering.
* ``POST  /api/agents/usage/refresh`` — ask the agents now. Bounded by the same single-flight as
  the background sweep, so a page with a Refresh button can't fan out six probes per click.
* ``PATCH /api/agents/budgets`` — the operator's threshold, notify toggle, per-engine limits and
  manual counters. Validated server-side; the write merges inside ``prefs._mutate``'s lock.

The split is deliberate: the read is cheap and always available, and every *slow* thing lives
behind the explicit POST.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import agent_usage, prefs, usage_loop


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/agents/usage")
    async def get_agent_usage(_: str = Depends(logged_in)) -> JSONResponse:
        budgets = prefs.get_agent_budgets()
        return JSONResponse(
            {
                "agents": agent_usage.snapshot(budgets=budgets),
                "budgets": budgets,
                "refreshing": usage_loop.is_running(),
            }
        )

    @app.post("/api/agents/usage/refresh")
    async def refresh_agent_usage(
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Off the event loop: `claude -p` is seconds of subprocess, and blocking here would
        # stall every websocket the app is serving.
        #
        # **The worker's own answer decides the status, not a pre-check.** An `is_running()`
        # test here would be a TOCTOU: another sweep can take the lock in the gap between the
        # check and the thread starting, and the route would then report a 200 "refreshed"
        # for figures it never refreshed. `refresh_once` holds the only lock that can settle
        # this, so it is the only thing entitled to say the sweep was skipped.
        result = await asyncio.to_thread(usage_loop.refresh_once)
        if result.get("skipped") == "busy":
            # 409 rather than a queue: the probes spawn CLIs, and the honest answer to "refresh
            # while a refresh is running" is that one already is.
            return JSONResponse({"detail": "a usage refresh is already running"}, status_code=409)
        budgets = prefs.get_agent_budgets()
        return JSONResponse({"agents": agent_usage.snapshot(budgets=budgets), "budgets": budgets})

    @app.patch("/api/agents/budgets")
    async def patch_agent_budgets(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            patch = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body") from None
        err = prefs.validate_agent_budgets_patch(patch)
        if err:
            raise HTTPException(status_code=422, detail=err)
        budgets = prefs.set_agent_budgets(patch)
        # The rows come back with the patch, because a limit is only meaningful next to the
        # count it is compared against — saving "10M" and being told nothing would leave the
        # operator to guess whether they had just crossed their own threshold.
        return JSONResponse({"budgets": budgets, "agents": agent_usage.snapshot(budgets=budgets)})
