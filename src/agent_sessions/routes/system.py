"""Info/settings routes (agent-sessions#265): healthz, auth-check, version, engines,
system, update check/apply, config, prefs. Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import contextlib
import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import discover, engines, prefs, sysinfo, twofactor, update, vtsidecar
from ..auth import AuthConfig, current_csrf, session_uid
from ..version import get_version


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    logged_in,
    csrf_guard,
    must_change: dict,
) -> None:
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/auth-check")
    async def auth_check(request: Request) -> Response:
        # nginx `auth_request` only cares about the status code. In `none` mode there
        # is no login → always 204. In single-user mode, 204 with a valid cookie, else 401.
        if cfg.auth_mode == "none":
            return Response(status_code=204)
        if session_uid(cfg, request) is None:
            raise HTTPException(status_code=401, detail="no session")
        return Response(status_code=204)

    @app.get("/api/version")
    async def app_version(_: str = Depends(logged_in)) -> JSONResponse:
        # Runtime version for the dashboard + the self-update flow (#65). Authed.
        return JSONResponse({"version": get_version()})

    @app.get("/api/engines")
    async def list_engines(_: str = Depends(logged_in)) -> JSONResponse:
        # Discovery for the Settings "Connected agents" section: every known provider
        # with its presence + whether it can start a new session + the resolved binary
        # path (or null). Authed; GET, so no CSRF.
        return JSONResponse(
            {
                "engines": [
                    {
                        "id": p.engine_id,
                        "present": p.is_present(),
                        "supports_new": bool(getattr(p, "supports_new", False)),
                        "bin": discover.resolve(p.engine_id),
                    }
                    for p in engines.all_providers()
                ]
            }
        )

    @app.get("/api/system")
    async def system_info(_: str = Depends(logged_in)) -> JSONResponse:
        # Host/system info for the Settings "System" section. Stdlib only, every field
        # fail-soft (omitted on error / non-Linux). No network interfaces / IPs. Authed.
        return JSONResponse(sysinfo.collect())

    @app.get("/api/update/check")
    async def update_check(_: str = Depends(logged_in)) -> JSONResponse:
        # Compare the running version to the channel's latest on the remote (#65 Phase 5).
        return JSONResponse(update.check())

    @app.post("/api/update/apply")
    async def update_apply(
        _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Update to the channel's latest — no user-supplied ref/command. Re-runs the
        # installer detached (atomic release + flip + restart + health-check + rollback).
        if not update.apply():
            raise HTTPException(status_code=503, detail="self-update unavailable (not an install)")
        return JSONResponse({"status": "updating"}, status_code=202)

    @app.get("/api/config")
    async def app_config(request: Request, _: str = Depends(logged_in)) -> JSONResponse:
        # SPA bootstrap (#64): the CSRF token for mutations + which engines can start a
        # new session (present + supports_new) + the terminal backend. Authed-only.
        return JSONResponse(
            {
                "csrf": current_csrf(cfg, request) or "",
                "new_session_engines": [
                    p.engine_id
                    for p in engines.present_providers()
                    if getattr(p, "supports_new", False)
                ],
                "terminal_backend": "ws",
                "must_change_password": must_change["v"],
                # "single-user" | "none" — lets the SPA hide login/logout UI when there
                # is no login (#13 / #32 Phase 3).
                "auth_mode": cfg.auth_mode,
                # Per-user UI theme (#109). The SPA applies this at load so a non-default
                # choice carries across devices; localStorage is the device cache.
                "theme": prefs.get_theme(),
                # Brand accent (#211 Phase 2): #rrggbb driving --accent + the xterm cursor.
                # Applied at load like the theme; localStorage is the device cache.
                "accent": prefs.get_accent(),
                # Sidebar body: the session list, or the squeezed Session Overview map (#139).
                # Persisted per-user like the theme; the SPA applies it at load.
                "sidebar_view": prefs.get_sidebar_view(),
                # Compose box default state on load: auto (device heuristic) | open | collapsed.
                # Per-user; the terminal applies it when mounting Compose.
                "compose_default": prefs.get_compose_default(),
                # Session Overview view-state (#144): expanded cluster cwds (default collapsed)
                # and project cwds excluded from the map. Per-user.
                "overview_expanded": prefs.get_overview_expanded(),
                # `overview_excluded` was the legacy name (#144); `projects_hidden` (#174) is
                # the same idea but with broader scope (sidebar list + filter + map + picker).
                # Both keys are emitted during the transition window so an old client tab still
                # reads its hidden list; new clients prefer `projects_hidden`.
                "overview_excluded": prefs.get_projects_hidden(),
                "projects_hidden": prefs.get_projects_hidden(),
                # Project-visibility model (#335): mode (all|included) + the `included`-mode
                # allowlist. `all` (default) keeps the legacy hide-list behavior unchanged; the
                # client applies the same mode-exclusive rule as the server's `project_visible`.
                "projects_mode": prefs.get_projects_mode(),
                "projects_included": prefs.get_projects_included(),
                # Per-cwd custom project display names (#148).
                "project_names": prefs.get_project_names(),
                # Optional TOTP 2FA (#116): only the on/off bit for the Settings UI — never
                # the secret or recovery codes. In `none` mode 2FA is N/A → always false.
                "two_factor_enabled": cfg.auth_mode != "none" and twofactor.is_enabled(),
                # Experimental (#329): faithful real-frame scroll-up via the VT sidecar. The
                # effective on/off bit (pref override, else env default) for the Settings toggle.
                "vt_scrollback": vtsidecar.enabled(),
            }
        )

    @app.post("/api/prefs")
    async def set_prefs(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Persist UI preferences (#109 theme, #139 sidebar_view, #144 overview lists). Each
        # provided key is validated server-side (unknown value → 422, never silently coerced
        # on write); other persisted keys are preserved. At least one known key must be present.
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="expected a JSON object")
        out: dict[str, object] = {}
        if "theme" in payload:
            if payload["theme"] not in prefs.THEMES:
                raise HTTPException(status_code=422, detail="unknown theme")
            out["theme"] = prefs.set_theme(payload["theme"])
        if "accent" in payload:
            if not prefs.is_valid_accent(payload["accent"]):
                raise HTTPException(status_code=422, detail="invalid accent")
            out["accent"] = prefs.set_accent(payload["accent"])
        if "sidebar_view" in payload:
            if payload["sidebar_view"] not in prefs.SIDEBAR_VIEWS:
                raise HTTPException(status_code=422, detail="unknown sidebar_view")
            out["sidebar_view"] = prefs.set_sidebar_view(payload["sidebar_view"])
        if "compose_default" in payload:
            if payload["compose_default"] not in prefs.COMPOSE_DEFAULTS:
                raise HTTPException(status_code=422, detail="unknown compose_default")
            out["compose_default"] = prefs.set_compose_default(payload["compose_default"])
        if "vt_scrollback" in payload:
            # Experimental (#329): flip VT-scrollback live + persist it. Turning it ON also
            # (best-effort) starts the sidecar so it takes effect without an app restart.
            v = payload["vt_scrollback"]
            if not isinstance(v, bool):
                raise HTTPException(status_code=422, detail="vt_scrollback must be a boolean")
            prefs.set_vt_scrollback(v)
            vtsidecar.set_enabled(v)
            if v:
                with contextlib.suppress(Exception):
                    await vtsidecar.ensure_started()
            out["vt_scrollback"] = vtsidecar.enabled()
        if "projects_mode" in payload:
            # Project-visibility mode (#335): all|included.
            if payload["projects_mode"] not in prefs.PROJECT_MODES:
                raise HTTPException(status_code=422, detail="unknown projects_mode")
            out["projects_mode"] = prefs.set_projects_mode(payload["projects_mode"])
        for key, setter in (
            ("overview_expanded", prefs.set_overview_expanded),
            # The legacy `overview_excluded` write path is kept for clients still on the old
            # API surface — internally it routes to the same `projects_hidden` storage so
            # the two never diverge (#174).
            ("overview_excluded", prefs.set_projects_hidden),
            ("projects_hidden", prefs.set_projects_hidden),
            # `included`-mode allowlist (#335).
            ("projects_included", prefs.set_projects_included),
        ):
            if key in payload:
                v = payload[key]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise HTTPException(status_code=422, detail=f"{key} must be a list of strings")
                out[key] = setter(v)
        if "project_names" in payload:
            v = payload["project_names"]
            if not isinstance(v, dict) or not all(
                isinstance(k, str) and isinstance(val, str) for k, val in v.items()
            ):
                raise HTTPException(
                    status_code=422, detail="project_names must be an object of string→string"
                )
            out["project_names"] = prefs.set_project_names(v)
        if not out:
            raise HTTPException(status_code=422, detail="no known preference key")
        return JSONResponse(out)
