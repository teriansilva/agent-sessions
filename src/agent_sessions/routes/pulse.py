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
* ``POST /api/pulse/ask`` (#522) — one natural-language question over past sessions
  (``pulse_chat.ask``). Its own single-flight kind ``pulse-chat`` (an ask never blocks a
  scan, or vice-versa; concurrent asks 409 with the activity snapshot). Deliberate contrast
  with ``/scan``: an **unconfigured endpoint is a 409** (``configured: false``) and an
  endpoint failure a **502** — a chat has no useful non-LLM fallback, so it surfaces the
  condition instead of returning an empty "answer". The UI pre-gates on ``configured``;
  these are backstops.

The shared ``GET /api/ai/activity`` surface lives in ``routes/system.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import (
    aitasks,
    engines,
    metadata,
    notifications,
    orchestrator,
    orchestrator_ledger,
    prefs,
    pulse,
    pulse_chat,
    review,
    webpush,
)

# How many ledger rows the activity feed carries. Bounded so a long-lived install's
# history can't make the Pulse page payload grow without limit.
FEED_LIMIT = 100


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

    @app.post("/api/pulse/ask")
    async def ask_pulse(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Hand-rolled body parsing (like /scan): the bounds are the contract (#522) —
        # a missing/empty/oversized query is a 422 with a plain detail.
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str) or not query.strip():
            return JSONResponse({"detail": "query (string) is required"}, status_code=422)
        query = query.strip()
        if len(query) > pulse_chat.QUERY_MAX:
            return JSONResponse(
                {"detail": f"query too long (max {pulse_chat.QUERY_MAX} chars)"},
                status_code=422,
            )
        history = body.get("history") if isinstance(body, dict) else None
        try:
            # Separate kind from "pulse-scan" ON PURPOSE: an ask never blocks a scan (or
            # vice-versa); only concurrent ASKS serialize.
            async with aitasks.single_flight("pulse-chat", "ask"):
                result = await pulse_chat.ask(query, history, working_keys=_working_keys())
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "a question is already running", **aitasks.snapshot()},
                status_code=409,
            )
        except review.NotConfiguredError:
            # Contrast with /scan (which degrades to 200/fast): a chat has no non-LLM
            # fallback, so an unconfigured endpoint surfaces as a 409 the UI pre-gates on.
            return JSONResponse(
                {"detail": "AI endpoint is not configured", "configured": False},
                status_code=409,
            )
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return JSONResponse(result)

    # --- orchestrator (#726 Phase 1) ---------------------------------------------------
    # Pulse gains agency. These join the `/api/pulse/*` family on purpose rather than opening
    # an `/api/orchestrator/*` namespace: the operator-facing name is Pulse, and the existing
    # `/^\/api/` service-worker denylist entry already covers everything here.

    @app.get("/api/pulse/orchestrator")
    async def get_orchestrator_state(_: str = Depends(logged_in)) -> JSONResponse:
        """Cached state: config, pending actions, and the activity feed. NEVER runs a pass —
        same contract as `GET /api/pulse` (cache-only, instant)."""
        cfg = prefs.public_orchestrator()
        expired = await asyncio.to_thread(orchestrator_ledger.expire_due)
        pending, feed = await asyncio.to_thread(_pending_and_feed)
        return JSONResponse(
            {
                "config": cfg,
                "pending": pending,
                "feed": feed,
                "expired_now": len(expired),
                **aitasks.snapshot(),
            }
        )

    def _pending_and_feed() -> tuple[list[dict], list[dict]]:
        live = orchestrator_ledger.live_actions()
        pending = [r for r in live if r.get("state") in ("proposed", "approved", "escalated")]
        return pending, orchestrator_ledger.feed(FEED_LIMIT)

    @app.post("/api/pulse/orchestrate")
    async def run_orchestrator(
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Run one pass now. Its own single-flight kind so a pass never blocks a Pulse scan or
        an Ask (or vice-versa); only concurrent passes serialize.

        Deliberately contrasts with `/scan`, matching `/ask`: an unconfigured endpoint is a
        **409** and an endpoint failure a **502**. A scan degrades to fast curation because the
        page must still render; a *decision* has no useful non-LLM fallback, so it says so
        rather than returning an empty action list that reads as "nothing needs you".
        """
        try:
            async with aitasks.single_flight("orchestrator", "manual"):
                report = await orchestrator.run_pass(working_keys=_working_keys())
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "an orchestrator pass is already running", **aitasks.snapshot()},
                status_code=409,
            )
        except review.NotConfiguredError:
            return JSONResponse(
                {"detail": "AI endpoint is not configured", "configured": False},
                status_code=409,
            )
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        pending, feed = await asyncio.to_thread(_pending_and_feed)
        return JSONResponse({**report, "pending": pending, "feed": feed})

    # --- notifications + Web Push (#726 Phase 3) --------------------------------------
    # In-app first: the bell always works. Push is the extra that wakes the operator when the
    # tab is closed, and its absence must never mean an escalation goes unheard.

    @app.get("/api/pulse/notifications")
    async def get_notifications(_: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(notifications.listing))

    @app.post("/api/pulse/notifications/read")
    async def mark_notifications_read(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        ids: list[str] | None = None
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("ids"), list):
                ids = [i for i in body["ids"] if isinstance(i, str)]
        n = await asyncio.to_thread(notifications.mark_read, ids)
        return JSONResponse({"marked": n, **await asyncio.to_thread(notifications.listing)})

    @app.get("/api/pulse/push/key")
    async def get_push_key(_: str = Depends(logged_in)) -> JSONResponse:
        """The VAPID PUBLIC key. The private half never leaves the server."""
        return JSONResponse(
            {
                "public_key": await asyncio.to_thread(webpush.public_key),
                "subscriptions": await asyncio.to_thread(notifications.list_subscriptions),
            }
        )

    @app.post("/api/pulse/push/subscribe")
    async def push_subscribe(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "invalid JSON"}, status_code=422)
        sub = body.get("subscription") if isinstance(body, dict) else None
        try:
            public = await asyncio.to_thread(notifications.subscribe, sub or {})
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        # The echo is the PUBLIC view: an opaque id and the endpoint's ORIGIN. The endpoint
        # itself is a per-device capability and never travels back to a client.
        return JSONResponse(public)

    @app.post("/api/pulse/push/unsubscribe")
    async def push_unsubscribe(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        sub_id = ""
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                sub_id = str(body.get("id") or "")
        removed = await asyncio.to_thread(notifications.unsubscribe, sub_id)
        return JSONResponse(
            {
                "removed": removed,
                "subscriptions": await asyncio.to_thread(notifications.list_subscriptions),
            }
        )

    @app.post("/api/sessions/{sid}/orchestrator-exclude")
    async def toggle_orchestrator_exclude(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Withdraw (or restore) the orchestrator's agency over ONE session (#726).

        A dedicated toggle mirroring `POST /api/sessions/{sid}/review-exclude` rather than a
        `PATCH …/metadata` write: that route is project_id-only by contract (it 422s without
        one), and widening it would change a shared surface for an unrelated concern.

        This is NOT `review_excluded`. An unmanaged session stays listed, stays summarised,
        stays flagged needs-you — it only stops being something the orchestrator may act on.
        """
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        # Optional body {"excluded": bool}; absent/invalid → toggle the stored state.
        desired: bool | None = None
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("excluded"), bool):
                desired = body["excluded"]
        # Write against the RESOLVED sidecar key, like review-exclude: for a reconciled
        # opencode session the sidecar lives under the placeholder physical key.
        mkey = metadata.resolve_key(key)
        if desired is None:
            desired = not metadata.get(mkey).orchestrator_excluded
        m = metadata.patch(mkey, orchestrator_excluded=desired)
        return JSONResponse({"id": key, "orchestrator_excluded": m.orchestrator_excluded})

    @app.get("/api/pulse/evidence/{session_id:path}")
    async def get_evidence(
        session_id: str,
        request: Request,
        _user: str = Depends(logged_in),
    ) -> JSONResponse:
        """Server-pulled evidence for one session: the live screen, a transcript tail, or the
        recap. The model only ever names a *kind*; every byte here comes from the real session,
        fetched now — a model that can quote a screen can invent one.

        Never cached and never persisted into the ledger, so the operator always reads the
        current screen rather than a frozen one.
        """
        try:
            engines.parse_key(session_id)
        except Exception:
            return JSONResponse({"detail": "unknown session id"}, status_code=404)
        kind = request.query_params.get("kind", "screen")
        if kind not in orchestrator.EVIDENCE_KINDS:
            return JSONResponse(
                {"detail": f"kind must be one of {list(orchestrator.EVIDENCE_KINDS)}"},
                status_code=422,
            )
        # Blocking: the ring replay + FS reads must never run on the event loop (#678).
        result = await asyncio.to_thread(orchestrator.evidence_for, session_id, kind)
        # This response carries live terminal / transcript content, and its whole contract is
        # "what the session shows RIGHT NOW". A cached copy is both a stale-evidence hazard
        # (approving against a screen that has moved) and a data-exposure one (session content
        # sitting in a disk cache). Deny caching explicitly rather than relying on defaults.
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )
