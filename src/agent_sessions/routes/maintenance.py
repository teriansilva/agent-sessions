"""Settings → Maintenance routes (#993): cache prune, missions and admitted DB compaction.

Every route is ``logged_in``; the mutating ones add ``csrf_guard`` (which also carries the
Origin/Referer check). Bodies are strict — a wrong type, an unknown category or an extra key is a
422 before anything runs, never a coercion into the effectful default.

Registered BEFORE ``routes.missions``: ``/api/missions/archive-older`` must be matched ahead of
``/api/missions/{mission_id}``, or the GET would be read as a mission id.

All mutations run through the app's single :class:`maintenance.Runner`, so a second submission
while any maintenance job runs is a **409** naming that job — refused, not queued.
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import maintenance, missions, opencode_compact

DAYS_MAX = 3650
_DIGITS = re.compile(r"\A[0-9]{1,5}\Z")


def _unprocessable(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=422)


async def _object_body(request: Request) -> dict | None:
    """The body as a JSON object, or ``None`` (absent, unparseable, or not an object)."""
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _strict_days(value: object) -> int | None:
    # `True` is an int in Python — reject it on type, like POST /api/prefs does.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= DAYS_MAX else None


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    runner = maintenance.Runner()
    app.state.maintenance_runner = runner
    compact = opencode_compact.Service(runner)
    app.state.opencode_compaction = compact

    def _busy(e: maintenance.MaintenanceBusy) -> JSONResponse:
        return JSONResponse({"detail": maintenance.BUSY_DETAIL, "busy": e.info}, status_code=409)

    @app.get("/api/maintenance/prune")
    async def prune_dry_run(_user: str = Depends(logged_in)) -> JSONResponse:
        categories = await asyncio.to_thread(maintenance.dry_run_caches)
        database = await asyncio.to_thread(opencode_compact.measure)
        return JSONResponse(
            {"categories": categories, "compact": database, "runner": runner.busy_info()}
        )

    @app.get("/api/maintenance/compact")
    async def compact_info(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # One retained result per app. A known id never silently resolves to a later job,
        # including after restart (no retained job). GET without an id discovers the latest.
        job = compact.snapshot()
        wanted = request.query_params.get("job_id")
        if wanted is not None and (job is None or job["id"] != wanted):
            return JSONResponse(
                {"detail": "Compaction job is no longer available."}, status_code=404
            )
        info = await asyncio.to_thread(opencode_compact.measure)
        return JSONResponse({"compact": info, "job": job, "runner": runner.busy_info()})

    @app.post("/api/maintenance/compact")
    async def compact_start(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _object_body(request)
        if body is None or set(body) != {"confirm"} or body["confirm"] is not True:
            return _unprocessable('body must be exactly {"confirm": true}')
        try:
            accepted, job = await compact.start()
        except maintenance.MaintenanceBusy as e:
            return _busy(e)
        return JSONResponse(
            {"job": job, "runner": runner.busy_info()}, status_code=202 if accepted else 409
        )

    @app.post("/api/maintenance/prune")
    async def prune(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _object_body(request)
        if body is None or set(body) != {"categories"}:
            return _unprocessable('body must be exactly {"categories": [...]}')
        cats = body["categories"]
        if (
            not isinstance(cats, list)
            or not cats
            or any(not isinstance(c, str) or c not in maintenance.CATEGORIES for c in cats)
            or len(set(cats)) != len(cats)
        ):
            return _unprocessable(
                "categories must be a non-empty list of distinct names from "
                + ", ".join(maintenance.CATEGORIES)
            )
        selected = list(cats)
        try:
            result = await runner.run(
                "prune", lambda: asyncio.to_thread(maintenance.prune_caches, selected)
            )
        except maintenance.MaintenanceBusy as e:
            return _busy(e)
        return JSONResponse(result)

    @app.get("/api/missions/archive-older")
    async def missions_archive_older_dry_run(
        request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        raw = request.query_params.get("older_than_days", "")
        days = int(raw) if _DIGITS.match(raw) else None
        if days is None or not 1 <= days <= DAYS_MAX:
            return _unprocessable(f"older_than_days must be an integer from 1 to {DAYS_MAX}")
        try:
            out = await maintenance.missions_dry_run(days)
        except missions.MissionError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        out["runner"] = runner.busy_info()
        return JSONResponse(out)

    @app.post("/api/missions/archive-older")
    async def missions_archive_older(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _object_body(request)
        if body is None or set(body) != {"older_than_days"}:
            return _unprocessable('body must be exactly {"older_than_days": <int>}')
        days = _strict_days(body["older_than_days"])
        if days is None:
            return _unprocessable(f"older_than_days must be an integer from 1 to {DAYS_MAX}")
        try:
            result = await runner.run("missions", lambda: maintenance.archive_old_missions(days))
        except maintenance.MaintenanceBusy as e:
            return _busy(e)
        except missions.MissionError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        return JSONResponse(result)
