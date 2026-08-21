"""Prompt catalog routes (#824) — the ONE read/write surface for every AI system prompt.

* ``GET   /api/prompts``      — the catalog: all eleven prompts with their text, default,
  contract, cap and (for guarded ones) the read-only clause the server appends.
* ``PATCH /api/prompts/{id}`` — the single write route. ``{"value": "…"}`` or
  ``{"reset": true}``; the registry resolves the id to its storage binding server-side, so a
  client never sends or learns where a prompt lives and the panel needs no per-prompt shape.

Deliberately NOT on ``/api/config``: eleven prompts × (value + default) is ~50-100 KB of text
that only the Settings screen ever reads, and ``/api/config`` is on the SPA's boot path.

Auth matches every other settings surface (``routes/ai_review.py``): the read needs a session,
the write needs a session AND the CSRF guard (which also carries the Origin/Referer check).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import prompts as registry

# What a PATCH body may carry. Unknown keys are rejected rather than ignored so a typo'd
# field fails loudly instead of silently no-op'ing (the rule every prefs validator follows).
_PATCH_KEYS = frozenset({"value", "reset"})


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/prompts")
    async def list_prompts(_user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse({"prompts": registry.catalog()})

    @app.patch("/api/prompts/{pid}")
    async def patch_prompt(
        pid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            registry.get(pid)
        except registry.UnknownPromptError:
            raise HTTPException(status_code=404, detail="unknown prompt") from None
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="body must be an object")
        unknown = sorted(set(payload) - _PATCH_KEYS)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")

        if payload.get("reset") is True:
            registry.reset(pid)
        elif "value" in payload:
            err = registry.validate(pid, payload["value"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            # A blank value is accepted and coerced back to the default by the registry, so a
            # cleared field can never strand the feature that reads it.
            registry.set_value(pid, payload["value"])
        else:
            raise HTTPException(status_code=422, detail="body must carry `value` or `reset`")
        return JSONResponse(registry.entry(pid))
