"""AI session review routes (#356 Phase 1: manual reviews — the scheduler is Phase 2).

* ``GET  /api/ai-review/models`` — server-side proxy of the endpoint's ``/models`` so the
  API key never reaches the browser. 400 when unconfigured; an empty list when the endpoint
  answers but can't list models (the UI takes a typed model id); 502 only when the endpoint
  rejects the request or can't be reached.
* ``POST /api/ai-review/endpoint/test`` (#956) — check a DRAFT connection before it is saved.
  Never persists. The stored key is reused only for the origin it was saved for (one snapshot
  of the stored block decides); any other origin needs the key in the request, and a refused
  request makes no outbound call. ``listing: "unsupported"`` when the endpoint answers but
  cannot list models; 502 with the redacted gateway error when it rejects the request.
* ``POST /api/sessions/{sid}/review`` — manual "Review now". 409 while the feature is
  unconfigured; 502 on a failed review (the last good result + its stale age survive).
* ``POST /api/sessions/{sid}/review-exclude`` — per-session exclusion toggle.
"""

from __future__ import annotations

import contextlib
import json

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import aitasks, engines, metadata, prefs, review


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/ai-review/models")
    async def ai_review_models(
        _: str = Depends(logged_in), refresh: int = Query(0, ge=0, le=1)
    ) -> JSONResponse:
        try:
            models = await review.list_models(force=bool(refresh))
        except review.NotConfiguredError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except review.ModelsUnsupportedError:
            # Answered, but cannot list (#956): an empty list, not an error, so a 502 from this
            # route always means the saved connection was actually rejected or unreachable.
            return JSONResponse({"models": []})
        except review.ReviewError as e:
            # Upstream can't serve a list (no /models, error, timeout) — distinct from
            # "not configured" so the UI can fall back to free-text without nagging.
            raise HTTPException(status_code=502, detail=str(e)) from None
        return JSONResponse({"models": models})

    @app.post("/api/ai-review/endpoint/test")
    async def ai_review_endpoint_test(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be an object")
        unknown = sorted(set(body) - {"base_url", "api_key"})
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
        base_url = body.get("base_url")
        if (
            not isinstance(base_url, str)
            or not base_url.strip()
            or not prefs.is_valid_ai_base_url(base_url)
        ):
            raise HTTPException(status_code=422, detail="base_url must be an http(s) URL")
        api_key = body.get("api_key")
        if "api_key" in body and not isinstance(api_key, str):
            # null means "clear the key" when SAVING; a test has nothing to test without one.
            raise HTTPException(status_code=422, detail="api_key must be a string")
        if isinstance(api_key, str) and len(api_key) > prefs.AI_REVIEW_KEY_MAX:
            raise HTTPException(status_code=422, detail="api_key is too long")

        stored = prefs.get_ai_review()  # ONE snapshot decides both the origin and the key
        key, why = prefs.resolve_test_key(stored, base_url, api_key)
        if why is not None:
            raise HTTPException(status_code=422, detail=why)
        draft = {**stored, "base_url": base_url.strip(), "api_key": key}
        try:
            models = await review.list_models(force=True, cfg=draft)
        except review.ModelsUnsupportedError:
            return JSONResponse({"models": [], "listing": "unsupported"})
        except review.ReviewError as e:
            raise HTTPException(status_code=502, detail=str(e)) from None
        return JSONResponse({"models": models, "listing": "ok"})

    @app.post("/api/sessions/{sid}/review")
    async def review_session(
        sid: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        try:
            # Visible in the shared AI-activity surface (#441) for its (brief) duration.
            async with aitasks.track("ai-review", "manual"):
                fields = await review.run_review(key)
        except review.NotConfiguredError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except review.ReviewError as e:
            raise HTTPException(status_code=502, detail=str(e)) from None
        # The new DISPLAY title rides along so the sidebar row can update in place
        # without a refetch (precedence: user title → ai_title → first message). Read via
        # the resolved sidecar key (Hermes #367): a reconciled opencode session's user
        # title may live under its placeholder physical key.
        meta = metadata.get(metadata.resolve_key(key))
        return JSONResponse({"id": key, "title": metadata.display_title(meta, ""), **fields})

    @app.post("/api/sessions/{sid}/review-exclude")
    async def review_exclude(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
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
        # Toggle + write against the RESOLVED sidecar key (Hermes #367): for a reconciled
        # opencode session the sidecar (title/sticky/archive) lives under the placeholder
        # physical key; a sparse logical-key entry would shadow it in the list read path.
        mkey = metadata.resolve_key(key)
        if desired is None:
            desired = not metadata.get(mkey).review_excluded
        m = metadata.patch(mkey, review_excluded=desired)
        return JSONResponse({"id": key, "review_excluded": m.review_excluded})
