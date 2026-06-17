"""Pulse routes (#441 Phase 2): the cached recent-work overview + manual scan.

* ``GET  /api/pulse`` — the cached overview artifact, served instantly; it NEVER triggers a
  scan. Returns the "never scanned" empty overview (at the configured window/depth) before the
  first scan (or on a cache miss).
* ``POST /api/pulse/scan`` — run one scan now and return the fresh artifact. Uses the configured
  ``pulse`` window/depth (#441 Phase 3), overridable per-request by an optional JSON body
  ``{"depth": …, "window_days": …}`` (the page's depth control). The single ``409`` case is
  "a Pulse scan is already running" (single-flight, #441 Phase 1) — its body carries the live
  AI-activity snapshot so the UI shows the running scan, not an error. An **unconfigured AI
  gateway never 409s here**: depth ≥ medium degrades to ``fast`` curation and returns **200**
  with ``synthesis_skipped: true`` (the page always works).

The shared ``GET /api/ai/activity`` surface lives in ``routes/system.py``.
"""

from __future__ import annotations

import contextlib

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import aitasks, prefs, pulse


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    def _working_keys() -> set[str]:
        # Live "in flight" overlay: a session is live if its server-owned stream has recent
        # output (working) or a viewer is attached. Match either the logical or physical key
        # (a reconciled opencode session registers under its placeholder). Best-effort — a
        # registry hiccup must never fail the scan, it just yields no live overlay.
        if registry is None:
            return set()
        keys: set[str] = set()
        with contextlib.suppress(Exception):
            for r in registry.snapshot():
                if r.get("working") or r.get("attached"):
                    keys.add(r["id"])
        return keys

    @app.get("/api/pulse")
    async def get_pulse(_: str = Depends(logged_in)) -> JSONResponse:
        cached = pulse.load_cache()
        if cached is not None:
            return JSONResponse(cached)
        cfg = prefs.get_pulse()
        return JSONResponse(pulse.empty_overview(cfg["window_days"], cfg["scan_depth"]))

    @app.post("/api/pulse/scan")
    async def scan_pulse(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Configured window/depth, overridable by an optional body (the page depth control).
        # A bad/absent body falls back to prefs — the scan is never blocked on a parse error.
        cfg = prefs.get_pulse()
        window_days, depth = cfg["window_days"], cfg["scan_depth"]
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                if "depth" in body:
                    depth = pulse.coerce_depth(body["depth"])
                if "window_days" in body:
                    window_days = pulse.coerce_window_days(body["window_days"])
        working = _working_keys()
        try:
            async with aitasks.single_flight("pulse-scan", "manual"):
                artifact = await pulse.run_scan(
                    window_days=window_days, depth=depth, working_keys=working
                )
        except aitasks.AlreadyRunning:
            # The only 409: another Pulse scan holds the single-flight. Hand back the live
            # activity so the page renders "scan already running", not a broken state.
            return JSONResponse(
                {"detail": "a Pulse scan is already running", **aitasks.snapshot()},
                status_code=409,
            )
        return JSONResponse(artifact)
