"""Cross-engine handoff routes (#597, Phases 1–2).

Two-step prepare / commit so the modal can preview (and cancel) without side effects:

- ``POST /api/handoff/prepare`` — validates the source session (same identity gate +
  root/visibility scope as the resume path, BEFORE any transcript read) and the target
  engine's seed-start capability, decides the target's launch cwd from the source
  session's PROJECT (#1033: an adopted session whose raw cwd sits outside the project
  launches the target in the project's default folder; otherwise the raw cwd rides),
  builds the seed in the requested ``mode`` (``quick`` or Phase 2's ``ai``), and returns
  ``{handle, preview, meta}``. Nothing is spawned; an
  abandoned handle just expires. An ``ai`` request whose endpoint is unconfigured or
  failing DEGRADES to ``quick`` — ``meta.degraded`` + ``meta.notice`` say so, and the
  modal surfaces it, rather than the handoff failing outright (issue #597 Phase 2).
- ``POST /api/handoff`` — binds the handle to a freshly minted target session id and
  returns it; the client then navigates to the normal ``/s/:engine/:id`` launch route,
  which redeems the seed atomically at spawn time (see ``routes/terminal.py``). An
  optional ``seed`` carries the user's EDITED preview; the server re-sanitizes it (it is
  untrusted input) before it can reach a PTY.

Seed text lives only in the server-side handle store and the authed prepare response —
never in URLs, WS query params, argv, or the sidecar.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import discover, engines, handoff, metadata, prefs, project_dirs, projects, review

_MODES = {"quick", "ai"}  # "ai" (Phase 2) degrades to "quick" when the endpoint is absent


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
            raise HTTPException(status_code=422, detail=f"unknown handoff mode: {mode!r}")
        # Optional source reference (#716): adds the source transcript's LOCATION to the seed
        # so the target agent can read past the cap when the brief is not enough. Opt-in,
        # because *following* it is what spends tokens. Resolved from the canonical logical id
        # below (never a placeholder), and silently omitted when it does not resolve.
        #
        # Validated as a REAL boolean, never coerced: this flag gates a privacy disclosure, and
        # `bool("false")` / `bool("0")` are both True — a client sending the string "false"
        # would silently opt IN and embed a local path in the seed. Unknown shapes are a 422,
        # matching how `routes/system.py` validates its booleans.
        raw_source_ref = payload.get("include_source_ref", False)
        if not isinstance(raw_source_ref, bool):
            raise HTTPException(status_code=422, detail="include_source_ref must be a boolean")
        include_source_ref = raw_source_ref
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
        # UNCONDITIONAL (#1033, the #867-round-4 shape): in_scope checks exclusions FIRST
        # and only then falls through on empty roots, so the old `roots and …` guard let an
        # explicitly excluded session through whenever no roots were configured.
        sessions_all = await asyncio.to_thread(engines.scan_all)
        match = next(
            (s for s in sessions_all if s.engine == prov.engine_id and s.uuid == native), None
        )
        roots = project_dirs.effective_roots()
        exclusions = prefs.get_folder_exclusions()
        if match is None or not project_dirs.in_scope(
            match.cwd, roots=roots, exclusions=exclusions
        ):
            raise HTTPException(status_code=404, detail="unknown session")
        # The source session's PROJECT decides the target's launch cwd (#1033): an adopted
        # session's raw cwd can be anywhere (e.g. ~ — the operator's default directory) while
        # the session displays under the adopted project. When the raw cwd sits inside the
        # project's folders the handoff keeps it; when it doesn't, the target launches in the
        # project's default folder (#448). Folderless legacy projects have no default folder
        # by design (`_from_raw`); new-session refuses to launch into one and so does the
        # handoff — a clear 422 asking for the default, never a silent fallback. Display
        # parity needs no sidecar write: the default folder is one of the entity's adopted
        # folders, so the target's row boundary-matches back to the same project.
        src_key = await asyncio.to_thread(metadata.resolve_key, source_key)
        src_meta = await asyncio.to_thread(metadata.get, src_key)
        project_index = await asyncio.to_thread(projects.load)
        ref = projects.resolve(
            match.cwd, src_meta.project_id, project_index, alias=src_meta.project_alias
        )
        cwd = match.cwd
        ent = project_index.get(ref.id) if ref.kind == "project" else None
        if ent is not None and projects.owning_project(match.cwd, {ent.id: ent}) is None:
            if not ent.default_folder:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f'the source session\'s project "{ent.name}" has no default folder '
                        "configured — set one for the project before handing off"
                    ),
                ) from None
            cwd = ent.default_folder
        # Transcript read: for a reconciled mint-own-id source the history lives under the
        # REAL id (#611) — resolve through the alias map like every transcript consumer.
        logical = engines.logical_key(source_key)
        _eng, _, logical_native = logical.partition(":")

        def _quick() -> tuple[str, dict]:
            return handoff.build_quick_seed(
                prov.engine_id,
                logical_native,
                title=match.first_user_message,
                cwd=cwd,
                include_source_ref=include_source_ref,
            )

        try:
            if mode == "ai":
                # Degrade, never fail (issue #597 Phase 2): an unconfigured endpoint, an
                # unreachable/erroring one, or an unusable answer all fall back to the
                # local Quick tail with a notice the modal shows. An EMPTY-transcript
                # HandoffError(409) is NOT a degrade — Quick would raise it too, so it
                # propagates as the same clean 409 either way.
                try:
                    seed, meta = await handoff.build_ai_seed(
                        prov.engine_id,
                        logical_native,
                        title=match.first_user_message,
                        cwd=cwd,
                        include_source_ref=include_source_ref,
                    )
                except (review.ReviewError, handoff.HandoffError) as e:
                    if isinstance(e, handoff.HandoffError) and e.status == 409:
                        raise
                    seed, meta = await asyncio.to_thread(_quick)
                    meta = {
                        **meta,
                        "requested_mode": "ai",
                        "degraded": True,
                        "notice": (
                            "AI review isn't configured — using the local quick tail."
                            if isinstance(e, review.NotConfiguredError)
                            else "AI summary failed — using the local quick tail."
                        ),
                    }
            else:
                seed, meta = await asyncio.to_thread(_quick)
        except handoff.HandoffError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None
        handle = handoff.create_handle(source_key, target_engine, str(meta["mode"]), seed, cwd=cwd)
        return JSONResponse({"handle": handle, "preview": seed, "meta": meta})

    @app.post("/api/handoff")
    async def handoff_commit(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        payload = await request.json()
        handle = str(payload.get("handle", ""))
        # The edited preview (#597 Phase 2). Untrusted input: handoff.commit re-sanitizes
        # it (control-strip + byte cap) before it can ever reach a PTY.
        raw_seed = payload.get("seed")
        seed = str(raw_seed) if isinstance(raw_seed, str) else None
        try:
            res = handoff.commit(handle, seed)
        except handoff.HandoffError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None
        return JSONResponse(res)
