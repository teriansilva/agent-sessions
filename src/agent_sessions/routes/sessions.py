"""Session-data routes (agent-sessions#265): list/search sessions + facets, the
project list, rename, archive/unarchive, and bulk archive-older. Moved verbatim
from ``main.create_app``.
"""

from __future__ import annotations

import json
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import archive, engines, metadata, prefs, scanner, webterm

# How long after the last byte from the agent we still call the session "working" (#156).
# Picked to feel responsive without flapping between every keystroke of a streaming reply.
_WORKING_WINDOW_S = 10.0


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    def _row(s, m: metadata.SessionMeta) -> dict:
        key = engines.session_key(s)
        # #156 working signal: last byte we observed flowing into the shared ring.
        # Slice 2 (#183): the server-owned SessionStream writes under the PHYSICAL
        # key (opencode placeholder for a reconciled new-session). The row id stays
        # the LOGICAL key (real ``ses_…``) so the URL/sidebar are unchanged — but
        # the lookup must resolve through the alias map first, or a headless
        # reconciled-opencode row would always report idle.
        phys_key = engines.physical_key(key)
        last_out = webterm.get_last_output_at(phys_key)
        return {
            "id": key,
            "engine": s.engine,
            "uuid": s.uuid,
            "short_uuid": s.short_uuid,
            "cwd": s.cwd,
            "project": m.project_alias or s.cwd,
            "last_mtime": s.last_mtime,
            "last_output_at": last_out,
            "working": (last_out is not None) and (time.time() - last_out < _WORKING_WINDOW_S),
            "first_user_message": s.first_user_message,
            "title": m.title or s.first_user_message,
            "sticky": m.sticky,
            "sort_key": m.sort_key,
            # Effective archive state: the sidecar override wins when set (lets a
            # natively-archived opencode/codex row be unarchived), else the engine's
            # native state (claude's JSONL tree / opencode.db time_archived).
            "archived": m.archived if m.archived is not None else s.archived,
        }

    @app.get("/api/sessions")
    async def list_sessions(
        _: str = Depends(logged_in),
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
        archived: bool = Query(False),
        q: str | None = Query(None),
        project: str | None = Query(None),
        engine: str | None = Query(None),
    ) -> JSONResponse:
        # Flat, paginated, newest-first. sticky floats to the top of the
        # *first window* (a first-window concept, not a global pin).
        meta_index = metadata.load()
        # opencode new-session alias (#127): the live row is the real ``ses_…`` from
        # scan_all (the placeholder never appears here — it isn't in opencode.db), so
        # there is no ghost row to drop. But metadata set while the session was still on
        # its placeholder (title/sticky/archive before reconcile) is keyed by the
        # placeholder; resolve each scanned id to its physical key so that metadata
        # follows the real row — one row, with its sidecar intact, no duplicate.
        aliases = metadata.load_aliases()

        def _meta_for(s) -> metadata.SessionMeta:
            key = engines.session_key(s)
            phys = engines.physical_key(key, aliases)
            return meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()

        # Hidden projects (#174) are stripped server-side BEFORE pagination + facets are
        # computed, so totals/next_offset/facet lists all describe the visible-to-the-user
        # set. Filtering only on the client would make `total` and the filter dropdown lie.
        # Hide is keyed by cwd (the row's `cwd` field), not the display name.
        hidden = set(prefs.get_projects_hidden())
        scoped = [
            row
            for s in engines.scan_all()
            for row in [_row(s, _meta_for(s))]
            if row["archived"] == archived and row["cwd"] not in hidden
        ]
        # Facets for the project/agent dropdowns: distinct values over the visible (already
        # hide-filtered) archived-scoped set, computed BEFORE q/project/engine filtering —
        # so the dropdowns list every project/engine present, including ones past the
        # first page, regardless of what's currently filtered or loaded.
        facets = {
            "projects": sorted({r["project"] for r in scoped}),
            "engines": sorted({r["engine"] for r in scoped}),
        }
        # Normalize filters; empty / whitespace-only means "no filter".
        q_norm = (q or "").strip().casefold()
        project_f = (project or "").strip() or None
        engine_f = (engine or "").strip() or None

        def _keep(r: dict) -> bool:
            if q_norm and q_norm not in (r["title"] or "").casefold():
                return False
            if project_f is not None and r["project"] != project_f:
                return False
            if engine_f is not None and r["engine"] != engine_f:
                return False
            return True

        # Filter BEFORE limit/offset so total + next_offset describe the filtered
        # set and "load more" stays within results.
        rows = [r for r in scoped if _keep(r)]
        rows.sort(key=lambda r: (not r["sticky"], -r["sort_key"], -r["last_mtime"]))
        window = rows[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(rows) else None
        return JSONResponse(
            {
                "sessions": window,
                "next_offset": next_offset,
                "total": len(rows),
                "facets": facets,
            }
        )

    @app.get("/api/projects")
    async def list_projects(_: str = Depends(logged_in)) -> JSONResponse:
        # New-session picker + the Settings "Session overview" manager — hidden projects
        # (#174) are excluded here too. Picking a hidden project as a start location would
        # feel inconsistent with the user having explicitly said "I don't want to see this."
        #
        # Source from ALL engines (#196): the sidebar filter dropdown derives its options
        # from /api/sessions facets, which are computed over engines.scan_all(). If this
        # endpoint used the Claude-only scan (pickable_projects' default), an opencode/gemini
        # cwd would appear in the filter but be unmanageable here — the two lists drift.
        # Passing scan_all() unifies the superset so every filterable project is manageable.
        hidden = set(prefs.get_projects_hidden())
        return JSONResponse(
            {
                "projects": [
                    {"cwd": c, "label": c}
                    for c in scanner.pickable_projects(sessions=engines.scan_all())
                    if c not in hidden
                ]
            }
        )

    @app.post("/api/sessions/{sid}/rename")
    async def rename_session(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        payload = await request.json()
        title = str(payload.get("title", "")).strip()
        if not title:
            raise HTTPException(status_code=422, detail="title required")
        m = metadata.patch(key, title=title[:120])
        return JSONResponse({"id": key, "title": m.title})

    @app.post("/api/sessions/{sid}/archive")
    async def archive_session(
        sid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            prov.archive(native)
        except archive.ArchiveError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NotImplementedError:
            raise HTTPException(
                status_code=400, detail=f"archive not supported for engine {prov.engine_id}"
            ) from None
        return JSONResponse({"id": f"{prov.engine_id}:{native}", "archived": True})

    @app.post("/api/sessions/{sid}/unarchive")
    async def unarchive_session(
        sid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            prov.unarchive(native)
        except archive.ArchiveError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NotImplementedError:
            raise HTTPException(
                status_code=400, detail=f"unarchive not supported for engine {prov.engine_id}"
            ) from None
        return JSONResponse({"id": f"{prov.engine_id}:{native}", "archived": False})

    @app.post("/api/sessions/archive-older")
    async def archive_older(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Bulk-archive every (non-archived) session whose last activity is older than `hours`
        # (#142). Reuses the per-session archive; engines that can't archive (opencode/codex)
        # are skipped, not errored. Reversible — the archived sessions can be unarchived.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        hours = payload.get("hours") if isinstance(payload, dict) else None
        # Reject non-numbers, bool (a bool is an int in Python), ≤0, and absurd horizons.
        if (
            not isinstance(hours, int | float)
            or isinstance(hours, bool)
            or hours <= 0
            or hours > 24 * 3650
        ):
            raise HTTPException(status_code=422, detail="hours must be a positive number")
        cutoff = time.time() - hours * 3600.0
        archived = 0
        skipped = 0
        for s in engines.scan_all():
            if s.archived or (s.last_mtime or 0) >= cutoff:
                continue
            try:
                prov, native = engines.parse_key(engines.session_key(s))
                prov.archive(native)
                archived += 1
            except (NotImplementedError, archive.ArchiveError, engines.EngineError):
                skipped += 1  # engine can't archive / lost the file → leave it, keep going
        return JSONResponse({"archived": archived, "skipped": skipped})
