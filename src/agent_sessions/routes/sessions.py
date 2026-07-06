"""Session-data routes (agent-sessions#265): list/search sessions + facets, project
entities (#361), the launch-folder list, rename, favorite/unfavorite (#122),
archive/unarchive, and bulk archive-older. Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import (
    aitasks,
    archive,
    autosort,
    engines,
    fsbrowse,
    metadata,
    prefs,
    project_dirs,
    projects,
    runtime_cleanup,
    scanner,
    webterm,
)
from . import upload

# How long after the last byte from the agent we still call the session "working" (#156).
# Picked to feel responsive without flapping between every keystroke of a streaming reply.
_WORKING_WINDOW_S = 10.0

# Compose-draft (#477) bounds: keep a server-side draft sane and the sidecar small. A draft
# is unsent prompt text + already-uploaded image attachment paths — never image blobs.
_DRAFT_TEXT_MAX = 100_000
_DRAFT_ATTACH_MAX = 50

# Custom per-session tag (#551): a short label shown before the AI summary. Capped so it stays
# a tag, not a second title, and the sidecar stays small.
_TAG_MAX = 32


def _clean_draft_payload(payload: object) -> dict | None:
    """Validate + normalize a PUT /draft body into the stored shape, or None to clear (#477).

    Stricter than shape-only (Hermes): each attachment ``path`` must resolve INSIDE the
    upload namespace (``~/.agent-sessions/uploads/``) — absolute-outside paths and ``..``
    traversal are rejected, so a persisted draft can never become a path-confusion surface.
    Returns None when the draft is empty (no non-whitespace text and no attachments), which
    the caller stores to clear any existing draft.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=422, detail="text must be a string")
    if len(text) > _DRAFT_TEXT_MAX:
        raise HTTPException(status_code=422, detail="draft text too large")
    raw = payload.get("attachments", []) or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="attachments must be a list")
    if len(raw) > _DRAFT_ATTACH_MAX:
        raise HTTPException(status_code=422, detail="too many attachments")
    updir = upload.uploads_dir().resolve()
    attachments: list[dict] = []
    for a in raw:
        if not isinstance(a, dict):
            raise HTTPException(status_code=422, detail="attachment must be an object")
        name, path = a.get("name", ""), a.get("path", "")
        if not isinstance(name, str) or not isinstance(path, str) or not path:
            raise HTTPException(status_code=422, detail="attachment name/path must be strings")
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=422, detail="invalid attachment path") from None
        if updir not in resolved.parents:
            raise HTTPException(status_code=422, detail="attachment outside upload namespace")
        attachments.append({"name": name[:200], "path": str(resolved)})
    if not text.strip() and not attachments:
        return None
    return {"text": text, "attachments": attachments, "updated_at": time.time()}


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
            # Last-activity time (#525): the newest conversation-record timestamp, NOT the raw JSONL
            # mtime — a bare resume no longer bumps it. Drives the default "Update" order + relTime.
            "last_mtime": s.last_mtime,
            # Derived per-engine creation time (#506) — the sort key when the user picks the
            # "Creation date" list order. Update order stays on `last_mtime` above.
            "created_at": s.created_at,
            "last_output_at": last_out,
            "working": (last_out is not None) and (time.time() - last_out < _WORKING_WINDOW_S),
            # Raw first message stays on the row for search + diagnostics ONLY — never a
            # display fallback (#284). `q` matches it directly (see `_keep`), so a session
            # whose meaningless first line normalizes `title` to "" is still searchable by
            # its original first message.
            "first_user_message": s.first_user_message,
            # Display precedence (#356, fixes #284): user title → ai_title → MEANINGFUL
            # first message (a stray "a" / "." normalizes to "") — via THE shared helper,
            # so every row consumer agrees on one value.
            "title": metadata.display_title(m, s.first_user_message),
            "sticky": m.sticky,
            # Custom per-session tag (#551): a short user label rendered before the AI summary
            # on the row's second line. "" when unset (the row renders exactly as before).
            "tag": m.tag,
            # AI review surface (#356): summary line, advisory badge + reason, stale-age
            # source (reviewed_at), and the per-session opt-out for the row menu.
            "ai_summary": m.ai_summary,
            "ai_title": m.ai_title,
            "intervention_required": m.intervention_required,
            "intervention_reason": m.intervention_reason,
            "reviewed_at": m.reviewed_at,
            "review_excluded": m.review_excluded,
            # Chronological "what happened" recap (#481) for the session-brief modal —
            # generated by the review pass over the whole transcript, shown on demand. "" until
            # the first review produces one (the modal shows a "no recap yet" state).
            "ai_recap": m.ai_recap,
            # Cheap "has an unsent compose draft" flag (#477) → the blue status-dot. The
            # full draft body is fetched per-session via GET /api/sessions/{id}/draft so
            # the list payload stays lean.
            "has_draft": metadata.has_draft(m),
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
        # Flat, paginated, newest-first. Favorited (sticky) rows are a GLOBAL pin (#520): the sort
        # runs over the whole filtered set before the window is sliced, so a favorite floats to the
        # top of the first page regardless of its recency / which page it would otherwise land on.
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

        # Root scope (#465) + explicit-curation precedence (#520): a HARD scope applied BEFORE
        # visibility, facets, and pagination, so the list + facets both describe the in-scope set.
        # Empty roots ⇒ no filtering (today's behaviour). With roots set the precedence is
        # exclusion > curation > roots (see project_dirs.in_scope): an excluded prefix always
        # drops the row; otherwise an adopted project (or, in `included` mode, an allowlisted
        # cwd) stays even outside a root, so the user never loses a session they explicitly
        # curated. Unknown/unadopted folders still obey the roots.
        roots = project_dirs.effective_roots()
        exclusions = prefs.get_folder_exclusions()

        def _in_scope(row: dict) -> bool:
            if not roots:
                return True
            curated = row["project"]["kind"] == "project" or (
                mode == "included" and row["cwd"] in included
            )
            return project_dirs.in_scope(
                row["cwd"], roots=roots, exclusions=exclusions, curated=curated
            )

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
            if row["archived"] == archived and _in_scope(row) and _visible(row)
        ]
        # Facets for the project/agent dropdowns (#445): the project dropdown lists PROJECT
        # ENTITIES, not folder paths. Computed over the visible (already hide-filtered)
        # archived-scoped set, BEFORE q/project/engine filtering — so the dropdown lists every
        # project present (and every empty one the user made), regardless of what's currently
        # filtered or loaded. Each facet is:
        #   * every non-archived project entity, with its scoped count — INCLUDING 0-count ones
        #     (an empty project must be filterable the moment it's created); plus
        #   * a synthetic "Default" catch-all aggregating the unadopted (`kind=="folder"`)
        #     fallback rows — folders are presented as a sub-property of projects.
        # Sorted user-entities-first (by name), Default last. `resolve()`/`_visible()` are
        # unchanged: unadopted rows still resolve to `kind=="folder"` internally and keep obeying
        # folder visibility, so Default never bypasses the `projects_hidden`/`included` curation.
        entity_counts: dict[str, int] = {}
        default_count = 0
        for r in scoped:
            ref = r["project"]
            if ref["kind"] == "project":
                entity_counts[ref["id"]] = entity_counts.get(ref["id"], 0) + 1
            else:  # unadopted folder fallback → the synthetic Default project
                default_count += 1
        project_facets = sorted(
            (
                {
                    "kind": "project",
                    "id": pid,
                    "name": p.name,
                    "color": p.color,
                    "count": entity_counts.get(pid, 0),
                }
                for pid, p in project_index.items()
                if not p.archived
            ),
            key=lambda ref: (ref["name"].casefold(), ref["id"]),
        )
        if default_count:
            project_facets.append(
                {
                    "kind": "project",
                    "id": projects.DEFAULT_PROJECT_ID,
                    "name": projects.DEFAULT_PROJECT_NAME,
                    "color": "",
                    "count": default_count,
                }
            )
        facets = {
            "projects": project_facets,
            "engines": sorted({r["engine"] for r in scoped}),
        }
        # Normalize filters; empty / whitespace-only means "no filter".
        q_norm = (q or "").strip().casefold()
        project_f = (project or "").strip() or None
        engine_f = (engine or "").strip() or None

        def _keep(r: dict) -> bool:
            # Match the displayed title OR the raw first message (#284): once a meaningless
            # auto-derived title normalizes to "", search must still find the session by
            # its original first message, so the normalization never narrows results.
            if (
                q_norm
                and q_norm not in (r["title"] or "").casefold()
                and q_norm not in (r["first_user_message"] or "").casefold()
            ):
                return False
            if project_f is not None:
                # Project filter (#361/#445): matches the resolved entity id, the synthetic
                # Default id (= every unadopted `kind=="folder"` row), or — for back-compat —
                # the bare cwd (old links/state still filter by a launch folder even though
                # it now resolves into a project or Default).
                if project_f == projects.DEFAULT_PROJECT_ID:
                    if r["project"]["kind"] != "folder":
                        return False
                elif r["project"]["id"] != project_f and r["cwd"] != project_f:
                    return False
            if engine_f is not None and r["engine"] != engine_f:
                return False
            return True

        # Filter BEFORE limit/offset so total + next_offset describe the filtered
        # set and "load more" stays within results.
        rows = [r for r in scoped if _keep(r)]
        # Sort order (#506): favorites (sticky) lead — a global pin (#520) — then the timestamp
        # tier the user picked: last-activity time (default; #525 — last real turn, not raw file
        # mtime, so a mere open no longer reorders) or creation date (stable). The created_at mode
        # tie-breaks on last_mtime so equal/zero creation times stay stable.
        if prefs.get_session_list_order() == "created_at":
            rows.sort(key=lambda r: (not r["sticky"], -r["created_at"], -r["last_mtime"]))
        else:
            rows.sort(key=lambda r: (not r["sticky"], -r["last_mtime"]))
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
            p = projects.create(
                payload.get("name"),
                color=payload.get("color"),
                folders=folders,
                default_folder=payload.get("default_folder"),  # #448: auto-adopted launch default
            )
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
        unknown = set(payload) - {"name", "color", "folders", "default_folder"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {sorted(unknown)}")
        try:
            p = projects.update(
                pid,
                name=payload.get("name"),
                color=payload.get("color"),
                folders=payload.get("folders"),
                default_folder=payload.get("default_folder"),  # #448
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
        #
        # Root-scoped + exclusion-filtered discovery (#465): when project roots are configured,
        # only folders under a root (+ each root's fresh sub-dirs) are discoverable, minus the
        # manual exclusion list. Empty roots ⇒ today's unscoped behaviour. The `?visible=1` /
        # mode filtering below still applies AFTER the scope.
        roots = project_dirs.effective_roots()
        exclusions = prefs.get_folder_exclusions()
        all_pickable = scanner.pickable_projects(
            sessions=engines.scan_all(), roots=roots, exclusions=exclusions
        )
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

    @app.get("/api/fs/dirs")
    async def fs_dirs(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # Folder-picker browse (#448): immediate subdirectories of `path` (default ~), bounded to
        # $HOME by fsbrowse (the security boundary — realpath containment, dotfiles skipped). Used
        # by the new-session folder override + the Settings default-folder picker.
        try:
            resolved, dirs = fsbrowse.list_dirs(request.query_params.get("path"))
        except fsbrowse.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"path": resolved, "home": fsbrowse.home_root(), "dirs": dirs})

    @app.post("/api/fs/mkdir")
    async def fs_mkdir(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Create a folder under a browsed parent (#448), bounded to $HOME by fsbrowse. Idempotent
        # (mkdir -p). Returns the new absolute path for the picker to select.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        parent = payload.get("parent")
        name = payload.get("name")
        if not isinstance(parent, str) or not isinstance(name, str):
            raise HTTPException(status_code=422, detail="parent and name must be strings")
        try:
            path = fsbrowse.make_dir(parent, name)
        except fsbrowse.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        return JSONResponse({"path": path})

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

    @app.post("/api/sessions/{sid}/tag")
    async def set_tag(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Custom per-session tag (#551): a short user label shown before the AI summary in the
        # sidebar row. Pure sidecar write, engine-agnostic, exactly like favorite/rename/draft —
        # resolve_key mirrors the list read precedence (logical → physical) so a reconciled
        # opencode session's tag follows its real row instead of shadowing it under a sparse
        # logical key. Never the review path, so re-review can't clobber it. An empty/whitespace
        # value clears the tag; the value is trimmed and length-capped.
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        payload = await request.json()
        tag = str(payload.get("tag", "")).strip()[:_TAG_MAX]
        m = metadata.patch(metadata.resolve_key(key), tag=tag)
        return JSONResponse({"id": key, "tag": m.tag})

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

    @app.get("/api/sessions/{sid}/draft")
    async def get_session_draft(sid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # The full compose draft (#477) for restoring the box when a session is reopened —
        # text + already-uploaded attachment pills. Sidecar-only, engine-agnostic; resolve_key
        # mirrors the list read precedence (logical → physical) so a reconciled opencode
        # session reads the same draft its row shows. A bare UUID resolves via canonical_key.
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        m = metadata.get(metadata.resolve_key(key))
        d = m.draft if isinstance(m.draft, dict) else {}
        return JSONResponse(
            {
                "id": key,
                "text": str(d.get("text", "")),
                "attachments": d.get("attachments") or [],
                "updated_at": d.get("updated_at"),
            }
        )

    @app.put("/api/sessions/{sid}/draft")
    async def put_session_draft(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Save (or clear) the compose draft (#477). Same write discipline as favorite/project:
        # a pure sidecar metadata write through resolve_key — opencode.db / codex stores stay
        # read-only. Empty text + no attachments ⇒ draft=None (cleared). Attachment paths are
        # validated to live inside the upload namespace by _clean_draft_payload.
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        draft = _clean_draft_payload(payload)
        m = metadata.patch(metadata.resolve_key(key), draft=draft)
        return JSONResponse({"id": key, "has_draft": metadata.has_draft(m)})

    @app.post("/api/sessions/{sid}/archive")
    async def archive_session(
        sid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            prov, native = engines.parse_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        # Reclaim the session's live runtime footprint BEFORE recording the archive (#523):
        # kill the dtach master + agent group, clear scrollback/VT + owner lease, unlink the
        # stale socket, release the single-writer lock. Terminate-first so a still-running
        # claude can't recreate its JSONL under projects/ between the move and the kill.
        # Best-effort — a teardown hiccup must never block the archive itself.
        with contextlib.suppress(Exception):
            await runtime_cleanup.cleanup_runtime(prov.engine_id, native)
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
        # (#142). Reuses the per-session archive + runtime cleanup (#523). Every present engine
        # archives (claude moves the JSONL, the rest flip the sidecar flag); the except below is
        # defensive — a provider whose archive() raises is counted as skipped, not errored.
        # Reversible — the archived sessions can be unarchived.
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
                # Free runtime resources before recording the archive (#523), best-effort
                # per session so one teardown hiccup never aborts the batch.
                with contextlib.suppress(Exception):
                    await runtime_cleanup.cleanup_runtime(prov.engine_id, native)
                prov.archive(native)
                archived += 1
            except (NotImplementedError, archive.ArchiveError, engines.EngineError):
                skipped += 1  # provider can't archive / lost the file → leave it, keep going
        return JSONResponse({"archived": archived, "skipped": skipped})
