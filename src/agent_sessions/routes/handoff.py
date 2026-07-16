"""Cross-engine handoff routes (#597, Phase 1 — Quick mode).

Two-step prepare / commit so the modal can preview (and cancel) without side effects:

- ``POST /api/handoff/prepare`` — validates the source session (same identity gate +
  root/visibility scope as the resume path, BEFORE any transcript read) and the target
  engine's seed-start capability, builds the Quick seed, and returns
  ``{handle, preview, meta}``. Nothing is spawned; an abandoned handle just expires.
- ``POST /api/handoff`` — binds the handle to a freshly minted target session id and
  returns it; the client then navigates to the normal ``/s/:engine/:id`` launch route,
  which redeems the seed atomically at spawn time (see ``routes/terminal.py``).

Seed text lives only in the server-side handle store and the authed prepare response —
never in URLs, WS query params, argv, or the sidecar.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import discover, engines, handoff, prefs, project_dirs

_MODES = {"quick"}  # "ai" is Phase 2 — the UI shows it disabled, the server rejects it


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.post("/api/handoff/prepare")
    async def handoff_prepare(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        payload = await request.json()
        source_id = str(payload.get("source_id", ""))
        target_engine = str(payload.get("target_engine", ""))
        mode = str(payload.get("mode", "quick") or "quick")
        if mode not in _MODES:
            raise HTTPException(
                status_code=422, detail="only quick mode is available (AI summary is Phase 2)"
            )
        # Source identity gate FIRST — the same parse_key validation every session route
        # uses, before anything touches the transcript. Placeholders resolve to their
        # real id where an alias exists (a reconciled source hands off its real history).
        try:
            prov, native = engines.parse_key(source_id)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        source_key = f"{prov.engine_id}:{native}"
        if prov.engine_id == "shell":
            raise HTTPException(status_code=422, detail="shell sessions cannot be handed off")
        # Target capability — the SAME source /api/engines serves the UI tiles from, so a
        # disabled tile and this rejection can never disagree.
        tprov = engines.get(target_engine)
        if tprov is None:
            raise HTTPException(status_code=404, detail="unknown engine")
        ok, reason = handoff.seed_start_state(tprov, present=bool(discover.resolve(target_engine)))
        if not ok:
            raise HTTPException(status_code=422, detail=f"target engine unavailable: {reason}")
        # Source must be a scanned, in-scope session — the resume path's scope rule
        # (#465/#467): a scoped-out session is not readable through handoff either.
        sessions_all = await asyncio.to_thread(engines.scan_all)
        match = next(
            (s for s in sessions_all if s.engine == prov.engine_id and s.uuid == native), None
        )
        roots = project_dirs.effective_roots()
        exclusions = prefs.get_folder_exclusions()
        if match is None or (
            roots and not project_dirs.in_scope(match.cwd, roots=roots, exclusions=exclusions)
        ):
            raise HTTPException(status_code=404, detail="unknown session")
        # Transcript read: for a reconciled mint-own-id source the history lives under the
        # REAL id (#611) — resolve through the alias map like every transcript consumer.
        logical = engines.logical_key(source_key)
        _eng, _, logical_native = logical.partition(":")
        try:
            seed, meta = await asyncio.to_thread(
                handoff.build_quick_seed,
                prov.engine_id,
                logical_native,
                title=match.first_user_message,
                cwd=match.cwd,
            )
        except handoff.HandoffError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None
        handle = handoff.create_handle(source_key, target_engine, mode, seed, cwd=match.cwd)
        return JSONResponse({"handle": handle, "preview": seed, "meta": meta})

    @app.post("/api/handoff")
    async def handoff_commit(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        payload = await request.json()
        handle = str(payload.get("handle", ""))
        try:
            res = handoff.commit(handle)
        except handoff.HandoffError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None
        return JSONResponse(res)
