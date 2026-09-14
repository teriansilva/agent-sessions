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
    handoff,
    missions,
    perfstats,
    prefs,
    project_dirs,
    ptybridge,
    scopedspawn,
    session_input,
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


def _preflight_prefs(payload: dict) -> None:
    """Validate EVERY key in a /api/prefs payload BEFORE any of them is written.

    Without this the handler validates and writes key by key, so a request whose *second* key
    is invalid returns 422 having already persisted the first — the caller is told the write
    failed while half of it survived. Demonstrated on `{"theme": "light", "accent": "BAD"}`,
    both of which long predate the terminal text size (#859); the flaw is the endpoint's shape,
    not any one key's, so it is fixed here for all of them rather than for the newest one.

    Checks run in the same order as the writes below, so the `detail` a mixed-invalid payload
    reports is unchanged. The per-key checks in the handler are deliberately left in place as
    defence in depth: this pass and those writes are edited by different people at different
    times, and a key added to one but not the other must still fail closed.
    """

    def bad(detail: str) -> HTTPException:
        return HTTPException(status_code=422, detail=detail)

    if "theme" in payload and payload["theme"] not in prefs.THEMES:
        raise bad("unknown theme")
    if "accent" in payload and not prefs.is_valid_accent(payload["accent"]):
        raise bad("invalid accent")
    if "term_font_size" in payload and not prefs.is_valid_term_font_size(payload["term_font_size"]):
        raise bad("invalid term_font_size")
    if "term_font_family" in payload and not prefs.is_valid_term_font_family(
        payload["term_font_family"]
    ):
        raise bad("invalid term_font_family")
    if "compose_default" in payload and payload["compose_default"] not in prefs.COMPOSE_DEFAULTS:
        raise bad("unknown compose_default")
    if (
        "session_list_order" in payload
        and payload["session_list_order"] not in prefs.SESSION_LIST_ORDERS
    ):
        raise bad("unknown session_list_order")
    if "projects_mode" in payload and payload["projects_mode"] not in prefs.PROJECT_MODES:
        raise bad("unknown projects_mode")
    if "mission_playbooks" in payload:
        # STRICT here, and deliberately not the read path's forgiving normalization: an operator
        # typing a probe target into Settings must be told their mistake at the moment they can
        # still fix it, rather than have it silently degrade to a template that cannot gate. The
        # read path stays lenient because `prefs.json` is a file a person can also hand-edit, and
        # a whole install must not lose its playbooks over one bad row (#883).
        #
        # THE REVISION IS PART OF THE SHAPE, and it is checked HERE (#900 review 7, finding 2).
        # It used to be validated at the write, several setters later — so a mixed request such
        # as `{theme, mission_playbooks}` with no revision answered 422 having already changed
        # the theme. That is exactly the all-or-nothing contract this preflight exists for: a 422
        # must mean nothing was persisted, not that the keys before the bad one already landed.
        block = payload["mission_playbooks"]
        expect = block.get("revision") if isinstance(block, dict) else None
        if isinstance(expect, bool) or not isinstance(expect, int):
            raise bad("mission_playbooks.revision is required and names the version you read")
        try:
            prefs._coerce_mission_playbooks(block, strict=True)
        except prefs.PlaybookError as e:
            raise bad(f"invalid mission_playbooks: {e}") from None
    for key in ("default_project", "default_project_id"):
        if key in payload and not isinstance(payload[key], str):
            raise bad(f"{key} must be a string")
    for key in (
        "overview_expanded",
        "projects_hidden",
        "projects_included",
        "project_roots",
        "folder_exclusions",
    ):
        if key in payload:
            v = payload[key]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise bad(f"{key} must be a list of strings")
    for key, validator in (
        ("ai_review", prefs.validate_ai_review_patch),
        ("forge", prefs.validate_forge_patch),
        ("auto_sort", prefs.validate_auto_sort_patch),
        ("pulse", prefs.validate_pulse_patch),
        ("orchestrator", prefs.validate_orchestrator_patch),
    ):
        if key in payload:
            err = validator(payload[key])
            if err is not None:
                raise bad(err)
    # The key-origin policy (#956), checked up front too so a multi-block patch fails before
    # ANY block is written. `set_ai_review` re-checks inside its lock; that one is authoritative.
    if "ai_review" in payload:
        why = prefs.key_origin_violation(prefs.get_ai_review(), payload["ai_review"])
        if why is not None:
            raise bad(why)
    if "project_names" in payload:
        v = payload["project_names"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(val, str) for k, val in v.items()
        ):
            raise bad("project_names must be an object of string→string")
    if "onboarded" in payload and not isinstance(payload["onboarded"], bool):
        raise bad("onboarded must be a boolean")


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
            # Handoff-target capability (#597): the ONE source both the modal's engine
            # tiles and the server-side prepare rejection consume, so a disabled tile can
            # never disagree with what the server would accept. `seed_reason` is the
            # user-facing why-not (null when supported).
            can_seed, seed_reason = handoff.seed_start_state(p, present=bin_path is not None)
            return {
                "id": p.engine_id,
                "present": bin_path is not None,
                "supports_new": can_start,
                "supports_seed_start": can_seed,
                "seed_reason": seed_reason,
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

    @app.get("/api/perf")
    async def perf_probes(reset: bool = False, _: str = Depends(logged_in)) -> JSONResponse:
        # #652 measurement scaffold: p50/p95/p99 for the hot paths the perf umbrella
        # optimizes — `attach_prep_ms` (accept→attach: open_action probe + scan_all),
        # `attach_payload_build_ms` (replay/redraw payload build), and `api_sessions_ms`
        # (the whole /api/sessions pipeline). Admin-gated, in-process, no persistence.
        # `?reset=1` returns the current window THEN clears it, so a before/after run is
        # reset → exercise → snapshot. GET (read + optional in-memory clear), so no CSRF.
        snap = perfstats.snapshot()
        if reset:
            perfstats.reset()
        return JSONResponse(snap)

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
        # First-run onboarding flag (#463): an explicit pref wins. The installer now owns
        # that pref (#675) — it seeds onboarded=false on a genuine fresh install and true on
        # an upgrade — so the explicit branch is the normal path and the inference below is a
        # fallback for hand-rolled states (manual install, restored config, older installs
        # from before #675). Inference: a fresh state (no prefs, no scanned sessions) shows
        # the wizard, while any pref already set or ≥1 scanned session is treated as onboarded
        # so an upgrade never regresses into onboarding. Fail-safe to onboarded on a scan error
        # so a transient fault can't trap a returning user in the wizard.
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
                # Terminal text size in px (#859). Seeds a device that has no local choice
                # yet; localStorage is the device cache and WINS over this, which is what
                # keeps a phone at 10 px while the desktop stays at 13 px.
                "term_font_size": prefs.get_term_font_size(),
                # Terminal font FAMILY (#866). Same seeding contract as the size above: the
                # device cache wins, this only seeds a device that has never chosen.
                "term_font_family": prefs.get_term_font_family(),
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
                # Hidden project cwds (#174) — NOT a global hide (#615). Withholds every folder
                # as a launch location (picker); additionally hides an UNADOPTED folder's
                # sessions (sidebar + map). An adopted folder's sessions are exempt — archive
                # the project instead. See `prefs.get_projects_hidden` for the full contract.
                # The legacy `overview_excluded` alias is retired (#357 Phase 2) — old on-disk
                # values are union-merged into `projects_hidden` once at startup.
                "projects_hidden": prefs.get_projects_hidden(),
                # Project-visibility model (#335): mode (all|included) + the `included`-mode
                # allowlist. `all` (default) keeps the legacy hide-list behavior unchanged; the
                # client applies the same mode-exclusive rule as the server's `project_visible`.
                "projects_mode": prefs.get_projects_mode(),
                "projects_included": prefs.get_projects_included(),
                # Preferred new-session PROJECT (#615 Phase 2), by entity id. The picker
                # pre-selects it; a deleted/archived id falls back to the first unarchived
                # project. Supersedes `default_project` below.
                "default_project_id": prefs.get_default_project_id(),
                # Legacy preferred new-session start dir (#335 Phase 2). Retained only as the
                # fallback for an operator whose start directory belongs to no project — with any
                # project selected, its own `default_folder` (#448) wins. Also seeds Onboarding.
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
                # Forge connection (#891) — where the objective probes look. PUBLIC view only:
                # the token is write-only and surfaces here solely as `token_set`.
                "forge": prefs.public_forge(),
                # AI auto-sort config (#424 Phase 6) — opt-in; holds no secret of its own,
                # `configured` mirrors the reused ai_review endpoint readiness.
                "auto_sort": prefs.public_auto_sort(),
                # Pulse recent-work overview config (#441 Phase 3) — opt-in background scan +
                # window/depth; holds no secret of its own, `configured` mirrors the reused
                # ai_review endpoint readiness (depth ≥ medium synthesis needs it).
                "pulse": prefs.public_pulse(),
                # Pulse orchestrator config (#726) — opt-in; holds no secret of its own,
                # `configured` mirrors the reused ai_review endpoint readiness. Carries
                # `auto_verbs_ceiling` so the UI can SHOW that choose/answer/dispatch always
                # need a tap, rather than implying the tier alone decides.
                "orchestrator": prefs.public_orchestrator(),
                # Mission playbooks (#883), so Settings can EDIT them (#892). Until now the
                # templates that decide what "done" means were reachable only by hand-editing
                # `prefs.json` — the block was validated, defaulted and consumed, and had no
                # surface at all. Normalized on the way out like every other read, so the editor
                # is shown the same shape the server will accept back. No secret: a playbook
                # holds an operator-typed probe target and nothing else.
                "mission_playbooks": prefs.get_mission_playbooks(),
                # …and WHAT A PROBE TAKES, from `PROBE_ARG_SCHEMA` itself rather than a second
                # copy in the client. The editor uses it to offer the right fields per kind, so
                # an unknown argument is prevented rather than merely refused on save — and a
                # kind added to the schema gains its fields here without a client change. The
                # names are the schema's; the VALIDATION stays entirely server-side.
                "mission_probes": {
                    "kinds": sorted(missions.PROBE_KINDS),
                    "non_gating": sorted(missions.NON_GATING_PROBES),
                    "args": {
                        kind: {
                            "required": sorted(n for n, (req, _) in spec.items() if req),
                            "optional": sorted(n for n, (req, _) in spec.items() if not req),
                        }
                        for kind, spec in missions.PROBE_ARG_SCHEMA.items()
                    },
                    # …and the JSON TYPE of each one. Without it the editor can only ever send
                    # strings, and `http_status.expect_status` — which strictly requires an
                    # integer — is a field the UI offers and the server always refuses (#900
                    # review, finding 6). The names say WHICH arguments exist; these say what a
                    # well-formed value looks like. Validation is still entirely server-side.
                    "types": missions.PROBE_ARG_TYPES,
                },
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
        # Validate the WHOLE payload before writing any of it (#859 review): a 422 must mean
        # nothing was persisted, not that the keys before the bad one already landed.
        _preflight_prefs(payload)
        out: dict[str, object] = {}
        # The AI block commits FIRST (Hermes on #960). Its key-origin check runs again inside the
        # prefs lock and can refuse a patch the pre-check passed (another save landed in between);
        # committing it before any other block means that refusal leaves the whole patch unwritten.
        if "ai_review" in payload:
            # AI review config (#356): a REAL nested validator (URL shape, length caps,
            # interval floor, max_input_chars bounds, unknown-key rejection) — never a
            # nested pass-through. The api_key is masked-sentinel: ""/mask → unchanged,
            # null → cleared, anything else → replaced. The echo is the PUBLIC view.
            err = prefs.validate_ai_review_patch(payload["ai_review"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            try:
                prefs.set_ai_review(payload["ai_review"])
            except prefs.KeyOriginError as e:
                # Lost a race: the block changed between the pre-check and the lock.
                raise HTTPException(status_code=422, detail=str(e)) from None
            out["ai_review"] = prefs.public_ai_review()
        if "theme" in payload:
            if payload["theme"] not in prefs.THEMES:
                raise HTTPException(status_code=422, detail="unknown theme")
            out["theme"] = prefs.set_theme(payload["theme"])
        if "accent" in payload:
            if not prefs.is_valid_accent(payload["accent"]):
                raise HTTPException(status_code=422, detail="invalid accent")
            out["accent"] = prefs.set_accent(payload["accent"])
        if "term_font_size" in payload:
            # Strict on write, lenient on read (#859) — the same split as accent's
            # is_valid_accent / coerce_accent. Rejecting rather than clamping keeps this
            # endpoint's stated contract ("unknown value → 422, never silently coerced on
            # write") and means a client bug surfaces instead of being silently rounded away.
            if not prefs.is_valid_term_font_size(payload["term_font_size"]):
                raise HTTPException(status_code=422, detail="invalid term_font_size")
            out["term_font_size"] = prefs.set_term_font_size(payload["term_font_size"])
        if "term_font_family" in payload:
            # Strict on write, lenient on read (#866), exactly as for the size above. The
            # write gate also carries the security boundary: the value lands in a CSS
            # declaration and in xterm's font strings, so a forbidden character is a 422
            # rather than something the read path quietly drops.
            if not prefs.is_valid_term_font_family(payload["term_font_family"]):
                raise HTTPException(status_code=422, detail="invalid term_font_family")
            out["term_font_family"] = prefs.set_term_font_family(payload["term_font_family"])
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
        if "default_project_id" in payload:
            # Preferred new-session project (#615 Phase 2); "" clears it. Stored verbatim, NOT
            # checked against the store: an entity can be deleted or archived after the fact, and
            # the picker already falls back to the first unarchived project. Validating here would
            # only move that fallback earlier while adding a 422 the UI can't act on.
            v = payload["default_project_id"]
            if not isinstance(v, str):
                raise HTTPException(status_code=422, detail="default_project_id must be a string")
            out["default_project_id"] = prefs.set_default_project_id(v)
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
        if "forge" in payload:
            # The forge connection the objective probes read (#891). Same masked-sentinel
            # contract as the AI key — ""/mask preserve, null clears — so a form that round-trips
            # the masked value cannot silently erase a working credential. The echo is the PUBLIC
            # view: the token never comes back out.
            err = prefs.validate_forge_patch(payload["forge"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            prefs.set_forge(payload["forge"])
            out["forge"] = prefs.public_forge()
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
        if "orchestrator" in payload:
            # Pulse orchestrator config (#726): tier + threshold + cadence + prompts,
            # server-validated. `allowed_verbs` is checked against the AUTO_VERBS_V1 ceiling
            # — a patch naming `answer`/`choose`/`dispatch` is a 422, because a shipped
            # setting that can add them means they ARE autonomous whatever the docs say.
            err = prefs.validate_orchestrator_patch(payload["orchestrator"])
            if err is not None:
                raise HTTPException(status_code=422, detail=err)
            # OFF THE LOOP, like the mission routes and the per-session opt-out. An orchestrator
            # patch withdraws authority (a tier drop, a narrowed verb set), so `set_orchestrator`
            # commits inside the cross-process fence — and that fence is a synchronous `flock`
            # poll with a budget measured in seconds. Entering it on the event loop stalls every
            # other request while a sibling instance holds it (#888 review, finding 2).
            try:
                await asyncio.to_thread(prefs.set_orchestrator, payload["orchestrator"])
            except session_input.AuthorityFenceBusy:
                raise HTTPException(
                    status_code=503,
                    detail="the authorization fence is busy; retry",
                ) from None
            out["orchestrator"] = prefs.public_orchestrator()
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
        if "mission_playbooks" in payload:
            # Re-validated by `set_mission_playbooks` itself, which is the point of the preflight
            # comment above: this pass and the write are edited at different times, so the write
            # fails closed on its own rather than trusting that a preflight ran.
            try:
                block = payload["mission_playbooks"]
                # THE REVISION THE CLIENT READ, as a comparand (#900 review 5, finding 7). Taken
                # from the block itself because that is what the client round-trips; absent means
                # "no comparand", which the installer and the shipped defaults rely on.
                #
                # REQUIRED AT THE HTTP BOUNDARY (#900 review 6, finding 2), and REJECTED IN THE
                # PREFLIGHT (review 7, finding 2) so a mixed payload cannot persist a theme and
                # then 422. `None` means "no comparand" and exists for the installer and the
                # shipped defaults, which have nothing to compare against — but over HTTP it made
                # the whole concurrency check OPTIONAL: an authenticated stale client, including
                # an older cached PWA build, could omit the field and overwrite a newer block
                # wholesale. Re-read rather than trusted, for the reason above the write: this
                # pass and the preflight are edited at different times.
                expect = block.get("revision") if isinstance(block, dict) else None
                if isinstance(expect, bool) or not isinstance(expect, int):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "mission_playbooks.revision is required and names the version you "
                            "read"
                        ),
                    )
                out["mission_playbooks"] = prefs.set_mission_playbooks(
                    block, expect_revision=expect
                )
            except prefs.PlaybookConflict as e:
                # 409, WITH THE CURRENT BLOCK. A conflict the operator cannot see is one they can
                # only resolve by reloading and guessing what changed.
                raise HTTPException(
                    status_code=409,
                    detail=str(e),
                    headers={},
                ) from None
            except prefs.PlaybookError as e:
                raise HTTPException(
                    status_code=422, detail=f"invalid mission_playbooks: {e}"
                ) from None
        if not out:
            raise HTTPException(status_code=422, detail="no known preference key")
        return JSONResponse(out)
