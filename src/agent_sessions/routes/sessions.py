"""Session-data routes (agent-sessions#265): list/search sessions + facets, project
entities (#361), the launch-folder list, rename, favorite/unfavorite (#122),
archive/unarchive, and bulk archive-older. Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import contextlib
import json
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import (
    aitasks,
    archive,
    autosort,
    engines,
    metadata,
    owner,
    prefs,
    project_dirs,
    projects,
    ptybridge,
    reaper,
    scanner,
    scrollback,
    sessionlock,
    webterm,
)

# How long after the last byte from the agent we still call the session "working" (#156).
# Picked to feel responsive without flapping between every keystroke of a streaming reply.
_WORKING_WINDOW_S = 10.0


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    def _row(s, m: metadata.SessionMeta, project_index: dict[str, projects.Project]) -> dict:
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
            # Structured project ref (#361): what the session BELONGS to, resolved by
            # THE shared resolver (explicit project_id → adopted-folder match → the
            # implicit folder group). `cwd` above stays the launch location. Breaking
            # change from the old `project_alias or cwd` string — the SPA (the only
            # consumer) moves in the same PR; with zero entities every ref is a folder
            # ref whose id == cwd, so behaviour is unchanged.
            "project": projects.resolve(
                s.cwd, m.project_id, project_index, alias=m.project_alias
            ).as_dict(),
            "last_mtime": s.last_mtime,
            "last_output_at": last_out,
            "working": (last_out is not None) and (time.time() - last_out < _WORKING_WINDOW_S),
            "first_user_message": s.first_user_message,
            # Display precedence (#356, mitigates #284): user title → ai_title → first
            # message — via THE shared helper, so search (`q` matches the displayed
            # title) and every row consumer agree.
            "title": metadata.display_title(m, s.first_user_message),
            "sticky": m.sticky,
            "sort_key": m.sort_key,
            # AI review surface (#356): summary line, advisory badge + reason, stale-age
            # source (reviewed_at), and the per-session opt-out for the row menu.
            "ai_summary": m.ai_summary,
            "ai_title": m.ai_title,
            "intervention_required": m.intervention_required,
            "intervention_reason": m.intervention_reason,
            "reviewed_at": m.reviewed_at,
            "review_excluded": m.review_excluded,
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

        sessions = list(engines.scan_all())
        # One-shot legacy migration (#361): per-session `project_alias` renames become
        # project entities adopting that cwd. Idempotence is a flag inside the store
        # (checked under its lock), so this is a cheap read once it has run.
        projects.ensure_alias_migration(
            [
                (engines.session_key(s), s.cwd, m.project_alias)
                for s in sessions
                for m in [_meta_for(s)]
                if m.project_alias
            ]
        )
        project_index = projects.load()

        # Visibility (#174/#335, refined by #361): folder visibility governs UNASSIGNED
        # folder-groups only. Once a session resolves to a project entity it is visible
        # iff the project is not archived — the user created the project explicitly, so
        # a hidden folder never hides an adopted project's sessions (archiving the
        # project is the hide mechanism). Stripped server-side BEFORE pagination +
        # facets, so totals/next_offset/facets all describe the visible-to-the-user set.
        mode = prefs.get_projects_mode()
        hidden = set(prefs.get_projects_hidden())
        included = set(prefs.get_projects_included())

        def _visible(row: dict) -> bool:
            # Project-resolved rows are always visible: hiding members happens through
            # the per-session archived flag (project archive, #361 Phase 2, archives
            # every member), never by dropping live rows — a row must always be
            # reachable in exactly one of the active/archived views.
            if row["project"]["kind"] == "project":
                return True
            return prefs.project_visible(row["cwd"], mode=mode, hidden=hidden, included=included)

        scoped = [
            row
            for s in sessions
            for row in [_row(s, _meta_for(s), project_index)]
            if row["archived"] == archived and _visible(row)
        ]
        # Facets for the project/agent dropdowns: distinct resolved refs over the visible
        # (already hide-filtered) archived-scoped set, computed BEFORE q/project/engine
        # filtering — so the dropdowns list every project/engine present, including ones
        # past the first page, regardless of what's currently filtered or loaded.
        # Entities sort first (alphabetical), then unassigned folder groups. Each facet
        # ref carries a `count` of scoped rows resolving to it (#361 Phase 3) — copied,
        # not mutated in place, because the same ref dict is embedded in the rows.
        distinct: dict[tuple[str, str], dict] = {}
        ref_counts: dict[tuple[str, str], int] = {}
        for r in scoped:
            ref = r["project"]
            key = (ref["kind"], ref["id"])
            distinct.setdefault(key, ref)
            ref_counts[key] = ref_counts.get(key, 0) + 1
        facets = {
            "projects": sorted(
                ({**ref, "count": ref_counts[key]} for key, ref in distinct.items()),
                key=lambda ref: (ref["kind"] != "project", ref["name"].casefold(), ref["id"]),
            ),
            "engines": sorted({r["engine"] for r in scoped}),
        }
        # Normalize filters; empty / whitespace-only means "no filter".
        q_norm = (q or "").strip().casefold()
        project_f = (project or "").strip() or None
        engine_f = (engine or "").strip() or None

        def _keep(r: dict) -> bool:
            if q_norm and q_norm not in (r["title"] or "").casefold():
                return False
            if project_f is not None:
                # Project filter (#361): matches the resolved ref id (entity id, or the
                # cwd for an unassigned folder group). The bare-cwd form is kept for
                # back-compat — a row whose cwd equals the filter matches even when it
                # now resolves into a project, which is exactly the pre-#361 result set.
                if r["project"]["id"] != project_f and r["cwd"] != project_f:
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
    async def list_projects(request: Request, _: str = Depends(logged_in)) -> JSONResponse:
        # Project ENTITIES (#361) — id/name/color/folders/archived + a resolved member
        # count. The launch-folder list this endpoint used to serve moved verbatim to
        # GET /api/folders (the SPA is the only consumer and moved with it). Archived
        # entities are hidden by default; Settings opts in via ?include_archived=1.
        include_archived = request.query_params.get("include_archived") == "1"
        project_index = projects.load()
        meta_index = metadata.load()
        aliases = metadata.load_aliases()
        counts: dict[str, int] = {}
        for s in engines.scan_all():
            key = engines.session_key(s)
            phys = engines.physical_key(key, aliases)
            m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
            ref = projects.resolve(s.cwd, m.project_id, project_index)
            if ref.kind == "project":
                counts[ref.id] = counts.get(ref.id, 0) + 1
        out = [
            {**p.as_dict(), "session_count": counts.get(p.id, 0)}
            for p in sorted(project_index.values(), key=lambda p: (p.name.casefold(), p.id))
            if include_archived or not p.archived
        ]
        return JSONResponse({"projects": out})

    @app.post("/api/projects")
    async def create_project(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Create an entity (#361): {name, color?, folders?}. "From a folder" is just
        # folders=[cwd]. Folder adoption is exclusive — a folder (or a folder nested
        # under / above one) already adopted by another project is a 409.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        folders = payload.get("folders", [])
        if not isinstance(folders, list):
            raise HTTPException(status_code=422, detail="folders must be a list")
        try:
            p = projects.create(payload.get("name"), color=payload.get("color"), folders=folders)
        except projects.ProjectError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse(p.as_dict())

    @app.post("/api/projects/auto-sort")
    async def auto_sort_now(
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # On-demand AI auto-sort (#424 Phase 6): run one bounded pass NOW, assigning unassigned
        # sessions to existing projects. Gated on the opt-in + a configured (reused) ai_review
        # endpoint, so the button can't run an unconfigured / opted-out sweep.
        if not prefs.get_auto_sort()["enabled"]:
            raise HTTPException(status_code=409, detail="auto-sort is disabled")
        if not prefs.public_ai_review()["configured"]:
            raise HTTPException(status_code=409, detail="the AI review endpoint is not configured")
        async with aitasks.track("auto-sort", "manual"):
            report = await autosort.run_sort()
        return JSONResponse(report)

    @app.patch("/api/projects/{pid}")
    async def patch_project(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Rename / recolor / adopt-release folders (#361). The archived flag is NOT
        # settable here — archiving has member-session semantics; use the dedicated
        # /archive + /unarchive endpoints below.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        unknown = set(payload) - {"name", "color", "folders"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {sorted(unknown)}")
        try:
            p = projects.update(
                pid,
                name=payload.get("name"),
                color=payload.get("color"),
                folders=payload.get("folders"),
            )
        except projects.ProjectError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse(p.as_dict())

    def _bulk_project_archive(pid: str, *, archive_members: bool) -> JSONResponse:
        # Project archive/unarchive (#361 Phase 2). Membership = THE resolver's view at
        # call time: sessions explicitly assigned (`project_id == pid`) PLUS sessions
        # folder-resolved into the project; a dangling `project_id` (deleted project)
        # is never swept in (the resolver drops it before it can match).
        #
        # Idempotent + blindly retryable: the ENTITY flag is set first, then each
        # member is reported as archived/unarchived, already_*, or failed(+reason) —
        # so after a partial failure the UI re-calls the same endpoint and only the
        # failed set is retried (the rest report already_*). Engine-aware via the
        # provider archive path: Claude moves its JSONL (+ sidecar flag, #194);
        # opencode/codex/gemini write the sidecar tri-state override only.
        index = projects.load()
        if pid not in index:
            raise HTTPException(status_code=404, detail="unknown project")
        try:
            projects.update(pid, archived=archive_members)
        except projects.ProjectError as e:  # store vanished between load and write
            raise HTTPException(status_code=e.status, detail=str(e)) from None

        meta_index = metadata.load()
        aliases = metadata.load_aliases()
        done, already, failed = (
            ("archived", "already_archived", "failed")
            if archive_members
            else ("unarchived", "already_unarchived", "failed")
        )
        results: list[dict] = []
        for s in engines.scan_all():
            key = engines.session_key(s)
            phys = engines.physical_key(key, aliases)
            m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
            ref = projects.resolve(s.cwd, m.project_id, index, alias=m.project_alias)
            if ref.kind != "project" or ref.id != pid:
                continue
            effective = m.archived if m.archived is not None else s.archived
            if effective == archive_members:
                results.append({"id": key, "result": already})
                continue
            try:
                prov, native = engines.parse_key(key)
                prov.archive(native) if archive_members else prov.unarchive(native)
                results.append({"id": key, "result": done})
            except (archive.ArchiveError, engines.EngineError, NotImplementedError) as e:
                results.append({"id": key, "result": failed, "reason": str(e) or type(e).__name__})
        return JSONResponse(
            {
                "id": pid,
                "archived": archive_members,
                "sessions": results,
                "counts": {
                    r: sum(1 for x in results if x["result"] == r) for r in (done, already, failed)
                },
            }
        )

    @app.post("/api/projects/{pid}/archive")
    async def archive_project(
        pid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return _bulk_project_archive(pid, archive_members=True)

    @app.post("/api/projects/{pid}/unarchive")
    async def unarchive_project(
        pid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return _bulk_project_archive(pid, archive_members=False)

    @app.delete("/api/projects/{pid}")
    async def delete_project(
        pid: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Removes the ENTITY only (#361): member sessions lose their assignment and
        # revert to folder grouping on the next resolve (a dangling project_id is
        # ignored, never an error). Session files are never touched.
        try:
            projects.delete(pid)
        except projects.ProjectError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"deleted": True, "id": pid})

    @app.patch("/api/sessions/{sid}/metadata")
    async def patch_session_metadata(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Session → project assignment is ONE metadata write (#361): {"project_id":
        # "p-…"} assigns, ""/null clears. This is the seam the AI auto-sorter
        # (follow-up to #356) will drive. Sidecar-only — engine stores stay read-only.
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict) or "project_id" not in payload:
            raise HTTPException(status_code=422, detail="project_id required")
        pid = payload.get("project_id")
        if pid is None:
            pid = ""
        if not isinstance(pid, str):
            raise HTTPException(status_code=422, detail="project_id must be a string")
        if pid and pid not in projects.load():
            raise HTTPException(status_code=422, detail="unknown project")
        m = metadata.patch(metadata.resolve_key(key), project_id=pid)
        return JSONResponse({"id": key, "project_id": m.project_id})

    @app.get("/api/folders")
    async def list_folders(request: Request, _: str = Depends(logged_in)) -> JSONResponse:
        # Launch-location folders: the new-session picker + the Settings folder manager.
        # Behaviour-preserving rename of the pre-#361 GET /api/projects (folders stay
        # what they were — where sessions launch; project entities live above).
        #
        # `all` mode (#174): hidden projects are excluded — picking a hidden project as a start
        # location would feel inconsistent with the user having said "I don't want to see this."
        #
        # `included` mode (#335): by default NOT filtered by the allowlist. The default list must
        # stay the FULL discovered set so the Settings manager can show every dir to curate —
        # filtering it would lock the user into their current allowlist with no way to add a new
        # dir (chicken-and-egg).
        #
        # `?visible=1` opts into the mode-aware filter (the prefs.project_visible source of truth,
        # same as /api/sessions): the new-session picker uses it so the dropdown mirrors the
        # curated sidebar instead of resurfacing every excluded dir. New dirs still enter the
        # allowlist via the Settings manager, the scoped create-folder flow, or a launch into a
        # not-yet-included cwd (the auto-include in the terminal route is unchanged).
        #
        # Source from ALL engines (#196): the sidebar filter dropdown derives its options from
        # /api/sessions facets (over engines.scan_all()). A Claude-only scan here would let an
        # opencode/gemini cwd appear in the filter but be unmanageable — the two lists drift.
        # scan_all() unifies the superset so every filterable project is manageable.
        all_pickable = scanner.pickable_projects(sessions=engines.scan_all())
        mode = prefs.get_projects_mode()
        if request.query_params.get("visible") == "1":
            hidden = set(prefs.get_projects_hidden())
            included = set(prefs.get_projects_included())
            folders = [
                c
                for c in all_pickable
                if prefs.project_visible(c, mode=mode, hidden=hidden, included=included)
            ]
        elif mode == "included":
            folders = list(all_pickable)
        else:
            hidden = set(prefs.get_projects_hidden())
            folders = [c for c in all_pickable if c not in hidden]
        return JSONResponse({"folders": [{"cwd": c, "label": c} for c in folders]})

    @app.post("/api/folders/mkdir")
    async def make_project_dir(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Create a new project directory from the UI (#335 Phase 3), scoped to an operator-
        # configured base root (AGENT_SESSIONS_PROJECT_ROOTS). Disabled by default — with no roots
        # the write surface does not exist. ALL containment/validation lives in project_dirs (the
        # security boundary): realpath-under-root + single-component name. Returns the new cwd; the
        # client then offers it as a start location (and it becomes pickable once a session runs).
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        root = payload.get("root")
        name = payload.get("name")
        if not isinstance(root, str) or not isinstance(name, str):
            raise HTTPException(status_code=422, detail="root and name must be strings")
        try:
            cwd = project_dirs.create_project_dir(root, name)
        except project_dirs.ProjectDirError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"cwd": cwd})

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

    def _set_favorite(sid: str, value: bool) -> JSONResponse:
        # Favorite (#122) = the existing sidecar `sticky` flag surfaced as a star; a
        # favorited session floats to the top of the first window via the sticky-first
        # sort in list_sessions. Engine-agnostic, exactly like rename and the project
        # assignment: a pure sidecar metadata write (opencode/codex/gemini included),
        # never the provider's native store. resolve_key mirrors the list read
        # precedence (logical → physical), so a reconciled opencode session's favorite
        # follows its real row instead of shadowing it under a sparse logical key.
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        m = metadata.patch(metadata.resolve_key(key), sticky=value)
        return JSONResponse({"id": key, "sticky": m.sticky})

    @app.post("/api/sessions/{sid}/favorite")
    async def favorite_session(
        sid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return _set_favorite(sid, True)

    @app.post("/api/sessions/{sid}/unfavorite")
    async def unfavorite_session(
        sid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return _set_favorite(sid, False)

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

    @app.post("/api/sessions/{sid}/restart")
    async def restart_session(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Recover a WEDGED session (#331): an agent process that is alive but has stopped reading
        # input AND painting (e.g. claude stalled mid-turn) can't be fixed by the rendering layer —
        # there is no live frame to draw. This kills the live dtach master via the shared reaper
        # helper and wipes the session's local terminal state, so the next ws attach finds no master
        # and relaunches it via the engine's resume argv. The on-disk transcript is untouched → the
        # conversation is preserved; only the stuck process + PTY + scrollback/mirror are dropped.
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        # Resolve to the PHYSICAL key the ws route keys live resources by (opencode placeholder
        # alias → its real id), so socket / owner / scrollback / VT mirror are all addressed under
        # the SAME mapping terminal.py uses (Hermes #331).
        phys_key = engines.physical_key(f"{prov.engine_id}:{native}")
        _eng, _, phys_native = phys_key.partition(":")

        # Body is optional; a tab identifies itself with (fp, tab_id) — the same pair both ownership
        # models key on — and may pass force=true to override the owner guard.
        payload: dict = {}
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        fp = str(payload.get("fp", "") or "")
        tab_id = str(payload.get("tab_id", "") or "")
        force = bool(payload.get("force", False))

        def _blocking_holder() -> dict | None:
            # The session's CURRENT live owner IF it is a DIFFERENT viewer than this caller, else
            # None. Mode-aware + authoritative (Hermes #332), never client-inferred:
            #   • takeover ON  → the on-disk lease (owner.read_owner); shared across prod+staging.
            #   • takeover OFF → the in-memory SessionRegistry claim (the #184 default path); the
            #     disk lease is empty in this mode, so without this a passive tab would see no
            #     holder and could nuke a session another active tab owns.
            # Either model naming a live, non-matching (fp, tab_id) blocks a non-forced restart.
            rec = owner.read_owner(prov.engine_id, phys_native)
            if rec is not None:
                same = rec.get("fp") == fp and rec.get("tab_id") == tab_id
                live = (time.time() - float(rec.get("last_seen", 0.0))) <= owner.LEASE_S
                if live and not same:
                    return {"label": str(rec.get("label", ""))[:80], "since": rec.get("since")}
            if registry is not None:
                claim = registry.current_owner(prov.engine_id, phys_native)
                if claim is not None and not claim.matches(fp, tab_id):
                    return {"label": "", "since": getattr(claim, "last_seen", None)}
            return None

        if not force:
            holder = _blocking_holder()
            if holder is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "another viewer is active", "holder": holder},
                )

        # Kill the master (shared reaper path: SIGTERM → grace → SIGKILL, frees the VT mirror). The
        # initial guard above is a point-in-time check; a viewer could (re)claim between it and the
        # signals (TOCTOU, Hermes #332). So unless this is an explicit force, re-assert the SAME
        # predicate right before SIGTERM and before SIGKILL via spare_if — a session that became
        # owned by someone else in that window is SPARED (no kill), and we skip all cleanup so its
        # lease + local terminal state are left intact.
        spare_if = None if force else (lambda: _blocking_holder() is None)
        outcome = await reaper.terminate_master(
            prov.engine_id, phys_native, key=phys_key, spare_if=spare_if
        )
        if outcome == "spared":
            raise HTTPException(
                status_code=409,
                detail={"error": "another viewer became active during restart"},
            )

        # Clean the rest of the local terminal state so the resume starts from a clean slate (no
        # stale-width ring replay): persisted scrollback + in-memory ring + VT mirror
        # (clear_scrollback → _drop_buffer), the now-meaningless owner lease, and any socket the
        # master left behind on a hard SIGKILL (a clean exit unlinks its own). Only reached when the
        # restart was NOT vetoed — so we never erase a lease that a new owner just took.
        with contextlib.suppress(Exception):
            scrollback.clear_scrollback([phys_key])
        with contextlib.suppress(Exception):
            owner.clear_owner(prov.engine_id, phys_native)
        # The sock unlink must NOT race a relaunch (2026-06-12 prod wedge): the client
        # auto-reconnects within the kill's grace window and can LAUNCH a NEW master
        # before this cleanup runs — unlinking then orphans the fresh master from its
        # path while it still holds the launch lock, and every later connect 4409-loops
        # forever. The single-writer lock is the truth: acquirable ⇒ no launcher/master
        # generation exists ⇒ any sock file is a stale leftover, safe to remove; held ⇒
        # a NEW generation owns the path — leave its socket alone.
        with contextlib.suppress(Exception):
            lk = sessionlock.acquire(phys_key)
            if lk is not None:
                try:
                    with contextlib.suppress(OSError):
                        ptybridge.socket_path(prov.engine_id, phys_native).unlink()
                finally:
                    lk.release()

        # Idempotent: "gone" (no live master) is a successful no-op — the next open launches it
        # fresh regardless. The client reconnects its ws to trigger the resume.
        return JSONResponse({"id": phys_key, "restarted": outcome != "gone", "master": outcome})
