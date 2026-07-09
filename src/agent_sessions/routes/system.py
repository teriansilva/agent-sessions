"""Info/settings routes (agent-sessions#265): healthz, auth-check, version, engines,
system, update check/apply, config, prefs. Moved verbatim from ``main.create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import (
    aitasks,
    discover,
    engines,
    prefs,
    project_dirs,
    ptybridge,
    scopedspawn,
    sysinfo,
    twofactor,
    update,
)
from ..auth import AuthConfig, current_csrf, session_uid
from ..version import get_version


def _dtach_master_sock(parts: list[bytes]) -> str | None:
    """The socket path iff ``parts`` is a real ``dtach -c <sock> …`` master cmdline.

    Strict on purpose (Hermes #354): argv[0]'s basename must be the configured dtach
    binary's, and the socket must be the argument immediately after ``-c`` — dtach's
    own argv contract. Anything looser maps unrelated processes that merely carry a
    ``-c`` flag and a ``*.sock`` argument (e.g. ``python -c … foo.sock``) as masters,
    and the operator view would report a wrong pid/scope/footprint for the session.
    """
    if not parts or not parts[0]:
        return None
    want = os.path.basename(ptybridge.DTACH_BIN).encode()
    if os.path.basename(parts[0]) != want:
        return None
    try:
        i = parts.index(b"-c")
    except ValueError:
        return None
    if i + 1 >= len(parts) or not parts[i + 1].endswith(b".sock"):
        return None
    try:
        return parts[i + 1].decode()
    except UnicodeDecodeError:
        return None


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
        # with whether the CLI is installed + whether this host can start a new session +
        # the resolved binary path (or null). Authed; GET, so no CSRF.
        def row(p: engines.EngineProvider) -> dict:
            bin_path = discover.resolve(p.engine_id)
            can_start = bool(bin_path and getattr(p, "supports_new", False))
            return {
                "id": p.engine_id,
                "present": bin_path is not None,
                "supports_new": can_start,
                "bin": bin_path,
            }

        return JSONResponse({"engines": [row(p) for p in engines.all_providers()]})

    @app.get("/api/ai/activity")
    async def ai_activity(_: str = Depends(logged_in)) -> JSONResponse:
        # Shared AI-task surface (#441 Phase 1): what AI work is running right now (Pulse
        # scans, AI-review/auto-sort sweeps + their on-demand runs) plus the last run per
        # kind. Read-only; the Settings "AI activity" panel polls it. Cheap (in-process
        # registry, no I/O), GET so no CSRF.
        return JSONResponse(aitasks.snapshot())

    @app.get("/api/system")
    async def system_info(_: str = Depends(logged_in)) -> JSONResponse:
        # Host/system info for the Settings "System" section. Stdlib only, every field
        # fail-soft (omitted on error / non-Linux). No network interfaces / IPs. Authed.
        return JSONResponse(sysinfo.collect())

    @app.get("/api/system/sessions")
    async def system_sessions(_: str = Depends(logged_in)) -> JSONResponse:
        # Per-session isolation view (#346 Phase C; feeds #279's operator surface).
        # One /proc walk maps every live `dtach -c <sock>` master to its pid, then the
        # scope unit + cgroup footprint are read back from the kernel — stateless, so
        # it stays correct across broker restarts and reports pre-scopes masters as
        # scope: null. Off the sidebar hot path on purpose (operator-priced, not
        # poll-priced); the walk runs in the thread pool to keep the event loop clean.
        def _collect() -> list[dict]:
            masters: dict[str, int] = {}
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/cmdline", "rb") as fh:
                        parts = fh.read().split(b"\0")
                except OSError:
                    continue
                sock_arg = _dtach_master_sock(parts)
                if sock_arg is not None:
                    with contextlib.suppress(ValueError):
                        masters[sock_arg] = int(name)
            rows = []
            for sock in sorted(ptybridge.runtime_dir().glob("*.sock")):
                pid = masters.get(str(sock))
                row: dict = {"sock": sock.name, "pid": pid, "scope": None}
                if pid is not None:
                    row["scope"] = scopedspawn.scope_of(pid)
                    stats = scopedspawn.scope_stats(pid)
                    if stats:
                        row.update(stats)
                rows.append(row)
            return rows

        return JSONResponse({"sessions": await asyncio.to_thread(_collect)})

    @app.get("/api/update/check")
    async def update_check(_: str = Depends(logged_in)) -> JSONResponse:
        # Compare the running version to the channel's latest on the remote (#65 Phase 5).
        # The remote compare is a blocking `git ls-remote` (up to 15 s) → worker thread.
        # #538: additive `auto_update` + `last_auto` fields; the pre-#538 fields keep their
        # names and semantics (the SPA's manual-check flow depends on them).
        info = await asyncio.to_thread(update.check)
        info["auto_update"] = update.auto_update_enabled()
        info["last_auto"] = update.last_auto()
        return JSONResponse(info)

    @app.get("/api/update/settings")
    async def update_settings_get(_: str = Depends(logged_in)) -> JSONResponse:
        # Cheap read (no network) for the Settings card mount (#538) — `check` would hit
        # the remote, which the card must not do on every Settings visit.
        out = update.settings()
        out["last_auto"] = update.last_auto()
        return JSONResponse(out)

    @app.post("/api/update/settings")
    async def update_settings_set(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Persist the auto-update opt-in + release channel (#538). Strictly the two fixed
        # env-file keys — bool/enum validated here, never raw user input into the env file.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="expected {auto_update?, channel?}")
        auto_update = body.get("auto_update")
        channel = body.get("channel")
        if auto_update is None and channel is None:
            raise HTTPException(status_code=422, detail="expected {auto_update?, channel?}")
        if auto_update is not None and not isinstance(auto_update, bool):
            raise HTTPException(status_code=422, detail="auto_update must be a boolean")
        if channel is not None and channel not in update.CHANNELS:
            raise HTTPException(
                status_code=422, detail=f"channel must be one of {list(update.CHANNELS)}"
            )
        try:
            out = update.set_settings(auto_update=auto_update, channel=channel)
        except OSError:
            raise HTTPException(status_code=503, detail="could not persist settings") from None
        out["last_auto"] = update.last_auto()
        return JSONResponse(out)

    @app.post("/api/update/apply")
    async def update_apply(
        _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Update to the channel's latest — no user-supplied ref/command. Re-runs the
        # installer detached (atomic release + flip + restart + health-check + rollback).
        # Single-flight with the scheduled auto-update pass (#538).
        status = update.apply_manual()
        if status == "unavailable":
            raise HTTPException(status_code=503, detail="self-update unavailable (not an install)")
        if status == "busy":
            raise HTTPException(status_code=409, detail="an update is already in progress")
        return JSONResponse({"status": "updating"}, status_code=202)

    @app.get("/api/config")
    async def app_config(request: Request, _: str = Depends(logged_in)) -> JSONResponse:
        # SPA bootstrap (#64): the CSRF token for mutations + which engines can start a
        # new session (present + supports_new) + the terminal backend. Authed-only.
        # First-run onboarding flag (#463): an explicit pref wins; otherwise infer — a fresh
        # install (no prefs, no scanned sessions) shows the wizard, while an existing install
        # (any pref already set, or ≥1 scanned session) is treated as already onboarded so an
        # upgrade never regresses into onboarding. Fail-safe to onboarded on a scan error so a
        # transient fault can't trap a returning user in the wizard.
        onboarded_explicit = prefs.get_onboarded()
        if onboarded_explicit is not None:
            onboarded_val = onboarded_explicit
        elif prefs.has_any_prefs():
            onboarded_val = True
        else:
            try:
                onboarded_val = any(True for _ in engines.scan_all())
            except Exception:
                onboarded_val = True
        return JSONResponse(
            {
                "csrf": current_csrf(cfg, request) or "",
                # First-run onboarding wizard gate (#463) — see the inference above.
                "onboarded": onboarded_val,
                "new_session_engines": [
                    p.engine_id
                    for p in engines.all_providers()
                    if getattr(p, "supports_new", False) and discover.resolve(p.engine_id)
                ],
                "terminal_backend": "ws",
                "must_change_password": must_change["v"],
                # "single-user" | "none" — lets the SPA hide login/logout UI when there
                # is no login (#13 / #32 Phase 3).
                "auth_mode": cfg.auth_mode,
                # Server hostname (#503): shown in the SPA's footer classbar so an operator can see
                # which machine a tab is pointed at. Cosmetic; the OS hostname, not a secret.
                "hostname": socket.gethostname(),
                # Per-user UI theme (#109). The SPA applies this at load so a non-default
                # choice carries across devices; localStorage is the device cache.
                "theme": prefs.get_theme(),
                # Brand accent (#211 Phase 2): #rrggbb driving --accent + the xterm cursor.
                # Applied at load like the theme; localStorage is the device cache.
                "accent": prefs.get_accent(),
                # Compose box default state on load: auto (device heuristic) | open | collapsed.
                # Per-user; the terminal applies it when mounting Compose.
                "compose_default": prefs.get_compose_default(),
                # Session-list sort order (#506): recent_activity (newest update first, default)
                # or created_at (stable, newest-created first). Server-side sort key; the SPA's
                # Appearance toggle writes it and the list refetches.
                "session_list_order": prefs.get_session_list_order(),
                # Session Overview view-state (#144): expanded cluster cwds (default collapsed).
                # Per-user.
                "overview_expanded": prefs.get_overview_expanded(),
                # Project cwds hidden globally (#174): sidebar list + filter + map + picker.
                # The legacy `overview_excluded` alias is retired (#357 Phase 2) — old on-disk
                # values are union-merged into `projects_hidden` once at startup.
                "projects_hidden": prefs.get_projects_hidden(),
                # Project-visibility model (#335): mode (all|included) + the `included`-mode
                # allowlist. `all` (default) keeps the legacy hide-list behavior unchanged; the
                # client applies the same mode-exclusive rule as the server's `project_visible`.
                "projects_mode": prefs.get_projects_mode(),
                "projects_included": prefs.get_projects_included(),
                # Preferred new-session start dir (#335 Phase 2); the picker pre-selects it when
                # still pickable, else falls back silently.
                "default_project": prefs.get_default_project(),
                # Base dirs under which the UI may create a new project folder (#335 Phase 3) AND
                # the root scope for discovery (#465) — the merged effective list (prefs roots, else
                # the env fallback). Empty ⇒ the "New folder" affordance is hidden, the mkdir
                # endpoint is a no-op, and discovery is unscoped (today's behaviour).
                "project_roots": project_dirs.project_roots(),
                # Manual exclusion list (#465): boundary-aware path prefixes dropped from discovery
                # even when under a root (for ephemerals that slip past is_ephemeral_cwd).
                "folder_exclusions": prefs.get_folder_exclusions(),
                # Per-cwd custom project display names (#148).
                "project_names": prefs.get_project_names(),
                # Optional TOTP 2FA (#116): only the on/off bit for the Settings UI — never
                # the secret or recovery codes. In `none` mode 2FA is N/A → always false.
                "two_factor_enabled": cfg.auth_mode != "none" and twofactor.is_enabled(),
                # AI session review config (#356) — the PUBLIC view only: the API key is
                # write-only and surfaces here solely as `api_key_set` (never the value).
                "ai_review": prefs.public_ai_review(),
                # AI auto-sort config (#424 Phase 6) — opt-in; holds no secret of its own,
                # `configured` mirrors the reused ai_review endpoint readiness.
                "auto_sort": prefs.public_auto_sort(),
                # Pulse recent-work overview config (#441 Phase 3) — opt-in background scan +
                # window/depth; holds no secret of its own, `configured` mirrors the reused
                # ai_review endpoint readiness (depth ≥ medium synthesis needs it).
                "pulse": prefs.public_pulse(),
            }
        )

    @app.post("/api/prefs")
    async def set_prefs(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Persist UI preferences (#109 theme, #144 overview lists, #211 accent). Each
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
        if "compose_default" in payload:
            if payload["compose_default"] not in prefs.COMPOSE_DEFAULTS:
                raise HTTPException(status_code=422, detail="unknown compose_default")
            out["compose_default"] = prefs.set_compose_default(payload["compose_default"])
        if "session_list_order" in payload:
            if payload["session_list_order"] not in prefs.SESSION_LIST_ORDERS:
                raise HTTPException(status_code=422, detail="unknown session_list_order")
            out["session_list_order"] = prefs.set_session_list_order(payload["session_list_order"])
        if "projects_mode" in payload:
            # Project-visibility mode (#335): all|included.
            if payload["projects_mode"] not in prefs.PROJECT_MODES:
                raise HTTPException(status_code=422, detail="unknown projects_mode")
            out["projects_mode"] = prefs.set_projects_mode(payload["projects_mode"])
        if "default_project" in payload:
            # Preferred new-session cwd (#335 Phase 2); "" clears it. Stored verbatim — the picker
            # validates pickability on read, so a stale value just falls back, never errors.
            v = payload["default_project"]
            if not isinstance(v, str):
                raise HTTPException(status_code=422, detail="default_project must be a string")
            out["default_project"] = prefs.set_default_project(v)
        for key, setter in (
            ("overview_expanded", prefs.set_overview_expanded),
            # `projects_hidden` is the only hide-list key (#174); the legacy
            # `overview_excluded` write alias is retired (#357 Phase 2).
            ("projects_hidden", prefs.set_projects_hidden),
            # `included`-mode allowlist (#335).
            ("projects_included", prefs.set_projects_included),
            # Discovery root scope + manual exclusion list (#465). Same list-of-strings shape.
            ("project_roots", prefs.set_project_roots),
            ("folder_exclusions", prefs.set_folder_exclusions),
        ):
            if key in payload:
                v = payload[key]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise HTTPException(status_code=422, detail=f"{key} must be a list of strings")
                stored = setter(v)
                # For `project_roots` echo the EFFECTIVE (merged, normalized, existing-dir-only)
                # list so the client sees what actually took effect (#465); others echo the raw
                # stored value.
                out[key] = project_dirs.project_roots() if key == "project_roots" else stored
        if "ai_review" in payload:
            # AI review config (#356): a REAL nested validator (URL shape, length caps,
            # interval floor, max_input_chars bounds, unknown-key rejection) — never a
            # nested pass-through. The api_key is masked-sentinel: ""/mask → unchanged,
            # null → cleared, anything else → replaced. The echo is the PUBLIC view.
            err = prefs.validate_ai_review_patch(payload["ai_review"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            prefs.set_ai_review(payload["ai_review"])
            out["ai_review"] = prefs.public_ai_review()
        if "auto_sort" in payload:
            # AI auto-sort opt-in (#424 Phase 6): enable + interval, server-validated
            # (unknown-key rejection, interval bounds). Holds no secret — it reuses the
            # ai_review endpoint. The echo is the PUBLIC view (adds `configured`).
            err = prefs.validate_auto_sort_patch(payload["auto_sort"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            prefs.set_auto_sort(payload["auto_sort"])
            out["auto_sort"] = prefs.public_auto_sort()
        if "pulse" in payload:
            # Pulse overview config (#441 Phase 3): auto_enabled + interval + window + depth,
            # server-validated (unknown-key rejection, bounds, known depth). Holds no secret —
            # it reuses the ai_review endpoint. The echo is the PUBLIC view (adds `configured`).
            err = prefs.validate_pulse_patch(payload["pulse"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            prefs.set_pulse(payload["pulse"])
            out["pulse"] = prefs.public_pulse()
        if "project_names" in payload:
            v = payload["project_names"]
            if not isinstance(v, dict) or not all(
                isinstance(k, str) and isinstance(val, str) for k, val in v.items()
            ):
                raise HTTPException(
                    status_code=422, detail="project_names must be an object of string→string"
                )
            out["project_names"] = prefs.set_project_names(v)
        if "onboarded" in payload:
            # First-run onboarding flag (#463): the wizard POSTs {onboarded: true} on
            # completion (or skip). Boolean only; preserves other keys.
            v = payload["onboarded"]
            if not isinstance(v, bool):
                raise HTTPException(status_code=422, detail="onboarded must be a boolean")
            out["onboarded"] = prefs.set_onboarded(v)
        if not out:
            raise HTTPException(status_code=422, detail="no known preference key")
        return JSONResponse(out)
