"""FastAPI app for agent-sessions.

Surface: the React SPA shell, login/auth-check, a flat paginated session list
(with title search + project/agent-engine filters and server-computed facets),
the self-owned ws terminal (``/ws/term/{sid}``: attach / resume / new-session),
rename, favorite/unfavorite, archive/unarchive, the project list, and upload. See agent-sessions#4
(sidebar UX), #8 (findable list), #49 (ws terminal), #64 (React SPA cutover).

``create_app`` is a thin assembler (agent-sessions#265): it builds cfg/registry/app +
mounts, the runtime credential state (``_pw`` / ``_must_change``) and the auth/forced-
change middlewares, then registers each route group from ``routes/`` in the order
FastAPI must match them (system → auth → sessions → scrollback → ws/term → upload →
SPA catch-all LAST).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    ai_review_loop,
    automation_loop,
    autosort_loop,
    engines,
    metadata,
    mission_archive,
    mission_dispatch_recover,
    mission_objectives,
    mission_plan,
    mission_supervisor_loop,
    missions,
    needs_you_notify,
    orchestrator_loop,
    owner,
    prefs,
    projects,
    pulse_loop,
    reaper,
    scrollback,
    security_headers,
    session_stream,
    update_loop,
    usage_loop,
)
from . import (
    handoff as handoff_mod,
)
from .auth import (
    _SESSION_COOKIE,
    AuthConfig,
    decode_session_token,
    issue_session,
    require_csrf_and_origin,
    require_session,
    session_uid,
)
from .devicelink import DeviceLinkStore
from .routes import ai_review as ai_review_routes
from .routes import auth as auth_routes
from .routes import automations as automations_routes
from .routes import chat as chat_routes
from .routes import files as files_routes
from .routes import handoff as handoff_routes
from .routes import history as history_routes
from .routes import link as link_routes
from .routes import maintenance as maintenance_routes
from .routes import missions as missions_routes
from .routes import playbooks as playbooks_routes
from .routes import prompts as prompts_routes
from .routes import pulse as pulse_routes
from .routes import scrollback as scrollback_routes
from .routes import sessions as sessions_routes
from .routes import spa as spa_routes
from .routes import system as system_routes
from .routes import templates as templates_routes
from .routes import terminal as terminal_routes
from .routes import upload as upload_routes
from .routes import usage as usage_routes

# Re-export the post-login redirect sanitizer (now owned by routes/auth.py) under its
# historical name here, so tests that call main._safe_next still resolve it (#265).
from .routes.auth import _safe_next as _safe_next

# Re-export the "working" window (now owned by routes/sessions.py) under its historical
# name here, so callers (and tests) that read agent_sessions.main._WORKING_WINDOW_S keep
# resolving the same value (#265).
from .routes.sessions import _WORKING_WINDOW_S as _WORKING_WINDOW_S

log = logging.getLogger("agent_sessions.main")

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC = _HERE / "static"
# Built React SPA (Vite → web/dist), the only UI. Repo layout: <repo>/web/dist; a
# packaged install overrides via AGENT_SESSIONS_WEB_DIST.
_WEB_DIST = Path(
    os.environ.get("AGENT_SESSIONS_WEB_DIST") or (_HERE.parent.parent / "web" / "dist")
)
# Paths the SPA catch-all must never shadow (handled by their own routes / network-only).
# ``link`` = the device-link QR sign-in routes (#650), server-rendered / JSON, not the SPA.
_SPA_RESERVED = ("api", "ws", "login", "logout", "healthz", "static", "assets", "link")

# New-session reconcile tunables (#127/#315). Engines that mint their own id (opencode →
# ``ses_…`` in opencode.db; codex → ``rollout-…uuid.jsonl``) may not write that id until the
# first message/output, so we poll the engine's store (read-only) for the new id rather than
# blocking the terminal. Bounded interval; no hard deadline — if the id never appears we keep
# serving under the placeholder (the timeout path). These + ``_reconcile_new_session`` stay
# here (not routes/terminal.py) because tests monkeypatch them on ``main`` and call
# ``main._reconcile_new_session`` directly (#265).
_RECONCILE_INTERVAL_S = 0.5
# ~5 min of polling, then give up (session still served under the placeholder; no URL converge).
_RECONCILE_MAX_POLLS = 600


async def _reconcile_new_session(ws, prov, placeholder: str, cwd: str, snapshot) -> str | None:
    """Discover a mint-its-own-id engine's real session id for a placeholder launch, persist
    the alias, converge the client (#127 opencode / #315 codex). Engine-agnostic: drives any
    provider exposing ``reconcile_new_session`` (the per-engine store diff lives in the provider).

    ``ws`` may be ``None`` for a HEADLESS launch (#739). Everything before the client frame is
    identical and is the part that matters — the alias write is what lets a later attach by the
    real id resolve back to the placeholder's socket and lock, and skipping it would leave a
    dispatched session invisible to the app, which is exactly why #732 refused these engines
    outright. Only the ``{"t":"id"}`` converge is a viewer concern, and it is skipped rather than
    faked.

    Returns the real session id when one was resolved, otherwise ``None`` — a headless caller
    needs the answer, where a viewer only needed the side effects.

    Runs concurrently with the PTY bridge. Polls ``prov.reconcile_new_session(cwd, snapshot)``
    (read-only, fail-soft) for a session id in ``cwd`` not in ``snapshot``:
      * exactly one new id → that's ours: persist ``<engine>:<placeholder> → <engine>:<real>``
        and send ``{"t":"id","sid":"<engine>:<real>"}`` so the client replaces the URL and the
        sidebar de-dupes. One-shot, then stop.
      * ≥2 new ids (two same-cwd launches in the window) → AMBIGUOUS: do NOT guess; keep
        serving under the placeholder and stop reconciling (fail-safe — never the wrong
        session).
      * none yet → the engine hasn't written the id (may wait for first input); poll again.
    If the id never appears within the poll budget we stop quietly; the session keeps running
    under the placeholder (timeout path, never blocks the terminal).
    """
    placeholder_key = f"{prov.engine_id}:{placeholder}"
    for _ in range(_RECONCILE_MAX_POLLS):
        await asyncio.sleep(_RECONCILE_INTERVAL_S)
        result = await asyncio.to_thread(prov.reconcile_new_session, cwd, snapshot)
        if result is None:
            continue  # not written yet → keep polling
        if isinstance(result, list):
            return None  # ambiguous → fail safe, stay on the placeholder
        real_key = f"{prov.engine_id}:{result}"
        # Persist the alias FIRST, and ONLY converge the client if that write succeeds. The
        # alias (real → placeholder) is what lets a later attach by the real id resolve back
        # to the placeholder's socket/lock/buffer (it survives an app restart). If we sent the
        # id frame without it, the browser URL would become /s/opencode/ses_… with no alias on
        # disk, so a reload/reattach by the real id could not find the placeholder and might
        # launch a SECOND writer for the same opencode session. On persist failure (full disk,
        # permissions, …) we stay quietly on the placeholder — the session keeps running there.
        try:
            await asyncio.to_thread(metadata.set_alias, placeholder_key, real_key)
        except Exception:
            return None  # alias not durable → never converge; keep serving under the placeholder
        # The real (mint-its-own-id) session is now durable + discoverable → bust the sidebar's
        # scan snapshot so the just-reconciled row shows on the next /api/sessions without the TTL
        # lag (#561). The pinned-id path invalidates at launch in routes/terminal.py; reconciling
        # engines (opencode/codex/antigravity) only become discoverable here, after the alias write.
        engines.invalidate_scan_cache()
        # Handoff backlink (#597): if this placeholder was a handoff target, the source's
        # ``handoff_to`` can now point at the REAL id (never the placeholder). Inherits this
        # coroutine's fail-safe by construction — an ambiguous/timed-out reconcile never
        # reaches here, so the backlink is simply absent. Best-effort: a sidecar write
        # failure must not break the id converge below.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(handoff_mod.note_reconciled, placeholder_key, real_key)
        # Then converge the client: it replaces /s/opencode/new-… → /s/opencode/ses_…
        # (history replace, no reload, keep the socket) and the sidebar shows one row.
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"t": "id", "sid": real_key}))
        # The real session now exists + is durable — wake the AI-review loop to summarize it
        # promptly instead of waiting out the interval (#413). Gated/deduped inside the sweep.
        ai_review_loop.request_review_soon()
        return result


def create_app(cfg: AuthConfig | None = None) -> FastAPI:
    cfg = cfg or AuthConfig.from_env()

    # One-time prefs migration (#357 Phase 2): union-merge any legacy `overview_excluded`
    # hide list into `projects_hidden` and drop the old key. A pure no-op once migrated
    # (or on a fresh install); best-effort — a bad prefs file must not block startup.
    with contextlib.suppress(Exception):
        prefs.migrate_overview_excluded()

    # One-time prefs migration (#615 Phase 2): seed `default_project_id` from the legacy
    # `default_project` cwd when a project has adopted that folder. `prefs` can't import
    # `projects` (import direction), so the owner resolver is injected here. Leaves the cwd
    # in place — it is still the fallback when the start directory belongs to no project.
    # No-op once migrated / when nothing to migrate; best-effort, like the one above.
    def _owner_id_for_cwd(cwd: str) -> str:
        owner_project = projects.owning_project(cwd, projects.load())
        return owner_project.id if owner_project else ""

    with contextlib.suppress(Exception):
        prefs.migrate_default_project_id(_owner_id_for_cwd)

    # Slice 2 of the session-stability foundation (#183): the registry is the
    # process-wide source of truth for every live dtach session. The lifespan
    # context discovers live sessions on startup (so the sidebar's working dot +
    # scrollback resume are accurate even before any browser attaches) and
    # tears the streams down on shutdown.
    # The reload boundary of #853 P3: reconcile the loaded engine roster with the recorded one, so
    # an engine whose manifest was removed RETIRES (its live masters stay attachable) instead of
    # vanishing. Best-effort: a state problem is logged by the pass and must not block startup.
    try:
        engines.registry.apply_retirement()
    except Exception:  # noqa: BLE001 — startup must not fail on it, but it must not be silent
        log.exception("engine retirement pass failed; removed engines are not attachable")

    registry = session_stream.SessionRegistry()

    # In-place saves (#950) hold a kernel write lease, and a lease break is delivered as SIGIO —
    # whose default action terminates the process. A no-op handler (installed here because
    # `signal.signal` only works on the main thread) makes the break observable instead of fatal.
    # Deliberately a handler, not SIG_IGN: an ignored disposition would be inherited by every agent
    # this app execs. If it cannot be installed, the editor reports why and stays read-only.
    from . import fileedit

    fileedit.install_lease_signal_handler()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        from .plugins import manager as plugin_manager

        try:
            await asyncio.to_thread(plugin_manager.recover)
        except Exception:
            # A live worker keeps its ownership; corrupt records are preserved and surfaced by
            # the manager/roster APIs. Neither case is authority to replay installation work.
            log.warning("plugin operation recovery unavailable; no operation was replayed")

        # Cut over both policy scopes before any controller starts. A failed persist disables
        # automation reads for this process while the terminal and read-only surfaces remain up.
        try:
            await asyncio.to_thread(prefs.ensure_automation_policies)
        except Exception:
            log.exception("automation policy cutover failed; both controllers remain disabled")

        # Raise the default thread-pool ceiling (#280). Each live session's SessionStream._drain
        # parks one default-executor thread for the session's whole life, and the pool defaults to
        # only min(32, cpu+4). Past that, attach-time work (transcript render) can't get a thread
        # and the terminal "reconnects" forever. With many concurrent sessions that ceiling is low.
        with contextlib.suppress(Exception):
            from concurrent.futures import ThreadPoolExecutor

            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=512))
        # Deploy-hygiene guard (#434): the single-active-viewer take-over model is EXPERIMENTAL
        # and staging-only. It once leaked into the prod env file and made every non-active tab /
        # device blank, so surface it loudly at startup — an operator scanning the journal sees it
        # immediately instead of debugging a "blank terminal" report. No-op (and silent) when the
        # flag is absent/false, which is the default everywhere including all script installs.
        if owner.takeover_enabled():
            log.warning(
                "AGENT_SESSIONS_TAKEOVER is ON (instance=%s) — single-active-viewer take-over is "
                "EXPERIMENTAL / staging-only (#293/#434). Non-active viewers stream read-only. "
                "Unset AGENT_SESSIONS_TAKEOVER in the env file to restore the default behaviour.",
                owner.INSTANCE,
            )
        # Best-effort: a discovery error must not block the app from serving
        # (the existing /api/sessions HTTP path keeps working as fallback).
        with contextlib.suppress(Exception):
            await registry.discover()
        # Resolve any in-place save a crash left half-done (#950), off the event loop. Every
        # resolution action checks what exists first, and none removes the last name of bytes
        # that were displaced; a store that cannot be opened is reported by the next save.
        with contextlib.suppress(Exception):
            await asyncio.get_running_loop().run_in_executor(None, fileedit.startup_resolve)
        # Idle-session reaper (#279): tear down STALE (detached + long-idle) sessions so PTYs/
        # memory/tasks don't accumulate until the app slows. Disabled unless
        # AGENT_SESSIONS_REAP_IDLE_SECONDS > 0; defaults to dry-run (logs candidates, kills
        # nothing). Never reaps an attached or recently-active session.
        reaper_task = asyncio.create_task(reaper.run(registry))
        # Periodic AI session review (#356 Phase 2): same reaper pattern. The task exits
        # immediately under the AGENT_SESSIONS_AI_REVIEW_LOOP=0 kill-switch; otherwise it
        # re-reads the ai_review prefs every sweep, so the Settings enable toggle governs
        # it live without a restart.
        review_task = asyncio.create_task(ai_review_loop.run(registry))
        # Periodic AI auto-sort (#424 Phase 6): same reaper pattern, gated on the
        # `auto_sort` opt-in + the env kill-switch, re-read per sweep.
        autosort_task = asyncio.create_task(autosort_loop.run())
        # Periodic Pulse overview scan (#441 Phase 3): same reaper pattern, gated on the
        # `pulse.auto_enabled` opt-in + the env kill-switch + change-detection, re-read per
        # sweep. Skips when a manual scan holds the single-flight; an unchanged set is a no-op.
        pulse_task = asyncio.create_task(pulse_loop.run(registry))
        # Periodic Pulse orchestrator pass (#726 Phase 1): same reaper pattern, gated on the
        # `orchestrator.enabled` opt-in + the env kill-switch + change-detection, re-read per
        # sweep. Also performs one startup recovery of any action left mid-delivery by a
        # restart — moved to `indeterminate`, never auto-retried.
        orchestrator_task = asyncio.create_task(orchestrator_loop.run(registry))
        # The follow-through sweep (#885). Registered beside the orchestrator because it acts
        # THROUGH it — same master switch, same verb path — rather than as a second autonomy.
        supervisor_task = asyncio.create_task(mission_supervisor_loop.run(registry))
        # Daily in-app auto-update (#538): replaces the installer's systemd timer. Gated
        # per pass on the env-file AGENT_SESSIONS_AUTOUPDATE key, so the Settings → System
        # toggle governs it live without a restart.
        update_task = asyncio.create_task(update_loop.run())
        # Buffer-cap sweeper (#678): enforces the scrollback ring cap OFF the event loop
        # (periodic + kick-coalesced), so the byte pump never probes dtach sockets.
        cap_sweep_task = asyncio.create_task(scrollback.run_cap_sweeper())

        # Reconcile any mission archive OR unarchive a crash left half-applied, THEN prune (#846).
        # Both tear down or restore sessions, which are external effects that cannot be held in a
        # transaction — so the durable per-operation state is re-driven here rather than leaving a
        # mission stuck mid-flight with its agents still running. Idempotent by construction, and
        # bounded-retry rather than one shot: a transient store or provider error at boot would
        # otherwise fence the mission for the life of the process.
        #
        # **Sequenced, not concurrent.** Retention deletes closed missions, and a mission being
        # recovered is closed — run them together and the pruner can delete the very journal the
        # recovery pass is working from, taking the roster that named the agents it was about to
        # reap. The store refuses that either way (retention skips a mission with an operation
        # stamp), but ordering them means the two never even contend.
        #
        # A task, not an await, so a slow store never delays the app from serving.
        async def _mission_maintenance():
            await mission_archive.recover_with_retry()
            # A DISPATCH THAT OUTLIVED ITS PROCESS (#904 review 2). `dispatching` is a promise a
            # running process makes; killed mid-launch it leaves a mission stuck there, possibly
            # beside a live unattended agent nobody owns. FIRST among the mission passes,
            # because it is the one that can still ADOPT a session — retention and the scrub below
            # both act on closed missions, and this pass is what closes them.
            with contextlib.suppress(Exception):
                await mission_dispatch_recover.recover_once()
            # …and the projection of a late-bound adoption (#989): the store records where such a
            # session's runtime lives in the adopting commit, and the alias that lets everything
            # else find it is published after. A crash between the two is repaired here.
            with contextlib.suppress(Exception):
                await mission_dispatch_recover.repair_projections()
            # Objective production is a durable intent (#883): a crash between a mission's commit
            # and its model call leaves a checklist nobody will ever fill. Boot is the caller
            # that discharges it, exactly as it is for a half-finished archive above.
            with contextlib.suppress(Exception):
                await mission_objectives.recover_pending()
            # …and so is planning (#967): a new mission plans itself in a background task, and a
            # restart between the create and the plan must not leave it `pending` for ever.
            with contextlib.suppress(Exception):
                await mission_plan.recover_pending()
            # Discharge any scrub a busy reader left owed at the last shutdown. Sensitive text
            # sits on disk until this runs, so boot is the backstop for an obligation the
            # process that created it could not collect.
            with contextlib.suppress(Exception):
                await missions.run_admitted(missions.scrub_if_pending)
            # `instruction` / `brief` are verbatim operator text, so the window is a backstop on
            # how long any of it survives by default. Boot is the whole cadence in Phase 1 — the
            # app restarts on every auto-update — and a periodic pass rides the mission supervisor
            # when that lands (#840 Phase 5).
            try:
                await missions.run_admitted(missions.retention_pass)
            except missions.ScrubFailed as e:
                # The rows are gone; the log could not be truncated because something was reading
                # it. Logged rather than suppressed: the bytes are still on disk until the next
                # scrub, and silently swallowing that is the same "claimed a deletion that did not
                # happen" this pass exists to avoid. The next delete or boot completes it.
                log.warning("mission retention: %s", e)
            except Exception as e:  # noqa: BLE001 — never take the app down over housekeeping
                log.warning("mission retention failed: %s", type(e).__name__)

        mission_task = asyncio.create_task(_mission_maintenance())

        # Per-agent usage (#839): asks each engine what it has spent, on an interval, off the
        # event loop. Env kill-switch AGENT_SESSIONS_USAGE_LOOP=0; nothing else gates it,
        # because reading a quota is not a model call and costs no tokens.
        usage_task = asyncio.create_task(usage_loop.run())
        # Needs-you notifications (#1086 Phase 4): one withdrawable notification per episode, synced
        # from the Ask page's NEEDS YOU read. Env kill-switch AGENT_SESSIONS_NEEDS_YOU_LOOP=0.
        needs_you_task = asyncio.create_task(needs_you_notify.run())
        # Automations (#1201): the scheduler takes its kernel ownership lock before recovering or
        # firing anything. Env kill-switch AGENT_SESSIONS_AUTOMATION_LOOP=0 stops every trigger.
        automation_task = automation_loop.start(registry)
        try:
            yield
        finally:
            # Automations stop FIRING first, then drain the runs already started (their outcome is
            # recorded honestly, `interrupted` when it cannot be known), while their paths are up.
            with contextlib.suppress(Exception):
                await automation_loop.stop()
            # FIRST: a streamed mission turn whose reader has left belongs to no request, so the
            # server's wait for requests in flight does not cover it (#1224). Let it settle while
            # everything it uses is still up.
            with contextlib.suppress(Exception):
                await missions_routes.drain_turns()
            # Interrupt compaction, then drain its actual SQLite worker before its task/runner
            # or launch-admission lock can be released.
            await _app.state.opencode_compaction.shutdown()
            await _app.state.plugin_jobs.shutdown()
            for task in (
                reaper_task,
                review_task,
                autosort_task,
                pulse_task,
                orchestrator_task,
                supervisor_task,
                update_task,
                cap_sweep_task,
                mission_task,
                usage_task,
                needs_you_task,
                automation_task,
            ):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await registry.stop_all()

    app = FastAPI(
        title="agent-sessions",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.session_registry = registry

    @app.middleware("http")
    async def capture_engine_roster(request: Request, call_next):
        # Context propagates into the route's worker threads. A WebSocket is deliberately not
        # pinned for its lifetime: ATTACH retains its provider, while new work rechecks admission.
        with engines.registry.snapshot_scope(fresh=True):
            return await call_next(request)

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    if (_WEB_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    _logged_in = require_session(cfg)
    _csrf_guard = require_csrf_and_origin(cfg)

    # Runtime credential state: AuthConfig is frozen, but the admin password and the
    # first-run "must change password" flag change at runtime (forced first-login change /
    # the change endpoint). Login + the change flow (routes/auth.py) read/update these live
    # values by reference; the new hash is also persisted to the env file so it survives a
    # restart. ``_must_change`` is also read by the gate middleware below, the ws handler,
    # and /api/config — so it stays owned here and is passed to every consumer.
    _pw = {"hash": cfg.password_hash}
    _must_change = {
        # No password to change in `none` mode — the forced-change gate is always off.
        "v": cfg.auth_mode != "none"
        and os.environ.get("AGENT_SESSIONS_FORCE_PASSWORD_CHANGE", "")
        in {
            "1",
            "true",
            "yes",
        }
    }

    # Force the first-login password change "before anything else": while the flag is
    # set, a logged-in request to anything outside this allowlist is blocked — API/ws get
    # 403, page navigations are redirected to /change-password. (Unauthenticated requests
    # fall through to normal auth handling; ws is gated in its own handler.)
    _CHANGE_ALLOW = {
        "/change-password",
        "/api/password",
        "/api/config",
        "/api/auth-check",
        "/login",
        "/logout",
        "/healthz",
    }

    # `none` auth-mode (#13 / #32 Phase 3): no login at all. Before any route runs,
    # ensure every request carries a valid admin session cookie — auto-issue one when
    # absent so require_session / require_csrf_and_origin / current_csrf all behave as
    # if the single admin had logged in. We mint the signed cookie value, splice it
    # into the *request* cookies (so downstream deps decode a session this turn) and
    # set it on the *response* (so the browser keeps it). CSRF + Origin stay enforced:
    # they guard against cross-site requests, which matters even without auth.
    #
    # ONE stable session per app instance (#673): the SPA's boot wave is parallel and
    # cookie-less, and minting per request handed every response a DIFFERENT cookie —
    # each with its own embedded csrf. The browser / Home Free tunnel jar keeps whichever
    # Set-Cookie landed last (in practice /api/sessions, the slowest call), while the SPA
    # caches /api/config's csrf, so every mutation failed 403 "bad csrf" through the
    # relay. The token is cached on app state and every cookie-less request gets the SAME
    # cookie; the validate/mint/publish path is lock-serialized with a double-check so a
    # concurrent first wave can never mint twice, and an expired/invalid cached token
    # rotates under the same lock. A rotation RE-SIGNS the cookie but PINS the previous
    # csrf (Hermes on PR #674): the SPA caches /api/config's csrf once, so a rotated csrf
    # would re-create the 403s at every session_ttl expiry — the csrf is stable for the
    # app instance's lifetime instead. Per-PROCESS by design: the CLI and the systemd unit
    # run exactly one uvicorn worker — a multi-worker deployment would need a
    # process-independent stable-session design before relying on this.
    app.state.none_session = {"token": "", "set_cookie": "", "csrf": ""}
    app.state.none_mint_lock = asyncio.Lock()

    @app.middleware("http")
    async def _none_mode_autosession(request: Request, call_next):
        if cfg.auth_mode != "none":
            return await call_next(request)
        if session_uid(cfg, request) is None:
            cached = app.state.none_session
            if decode_session_token(cfg, cached["token"]) is None:
                async with app.state.none_mint_lock:
                    if decode_session_token(cfg, cached["token"]) is None:  # double-check
                        stub = Response()
                        cached["csrf"] = issue_session(cfg, stub, csrf=cached["csrf"] or None)
                        sc = stub.headers.get("set-cookie", "")
                        cached["token"] = sc.split(";", 1)[0].split("=", 1)[1] if "=" in sc else ""
                        cached["set_cookie"] = sc
            token, set_cookie = cached["token"], cached["set_cookie"]
            # Splice the cached cookie into this request so the route's session deps
            # see a valid session on this very turn.
            existing = request.headers.get("cookie", "")
            new_cookie = (
                f"{existing}; {_SESSION_COOKIE}={token}"
                if existing
                else (f"{_SESSION_COOKIE}={token}")
            )
            headers = [(k, v) for (k, v) in request.scope["headers"] if k.lower() != b"cookie"]
            headers.append((b"cookie", new_cookie.encode("latin-1")))
            request.scope["headers"] = headers
            # Drop Starlette's cached header/cookie parse so downstream deps re-read the
            # spliced cookie from the mutated scope.
            for attr in ("_headers", "_cookies"):
                if hasattr(request, attr):
                    delattr(request, attr)
            response = await call_next(request)
            response.headers.append("set-cookie", set_cookie)
            return response
        return await call_next(request)

    @app.middleware("http")
    async def _force_change_gate(request: Request, call_next):
        if _must_change["v"] and session_uid(cfg, request) is not None:
            path = request.url.path
            if path not in _CHANGE_ALLOW and not path.startswith(("/static/", "/assets/")):
                if path.startswith(("/api/", "/ws/")):
                    return JSONResponse({"detail": "password change required"}, status_code=403)
                return RedirectResponse("/change-password", status_code=303)
        return await call_next(request)

    # Browser containment headers (#612 P1). Registered LAST → outermost, so it stamps EVERY
    # response, including the gate's redirect/403 above. CSP resolved once (reads the built
    # index for the inline theme-init hash); ``setdefault`` lets a route override its own. WS
    # upgrades run in the websocket scope, not here, so the terminal socket is untouched.
    _headers = security_headers.base_headers(_WEB_DIST)

    @app.middleware("http")
    async def _containment_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _headers.items():
            response.headers.setdefault(key, value)
        if security_headers.is_https(
            forwarded_proto=request.headers.get("x-forwarded-proto"),
            scheme=request.url.scheme,
        ):
            response.headers.setdefault("Strict-Transport-Security", security_headers.HSTS)
        return response

    # Route groups register in the order FastAPI must match them (registration order is
    # significant; the SPA catch-all MUST be last). All live in routes/ (agent-sessions#265).

    # Info/settings routes (healthz, auth-check, version, engines, system, update
    # check/apply, config, prefs).
    system_routes.register(
        app,
        cfg=cfg,
        logged_in=_logged_in,
        csrf_guard=_csrf_guard,
        must_change=_must_change,
    )

    # Account/auth routes (password change, 2FA, login/logout). ``_pw`` / ``_must_change``
    # are passed by reference so the change flow mutates the same live state the middleware
    # and /api/config read.
    auth_routes.register(
        app,
        cfg=cfg,
        logged_in=_logged_in,
        csrf_guard=_csrf_guard,
        templates=_TEMPLATES,
        pw=_pw,
        must_change=_must_change,
    )

    # QR cross-device sign-in (#650): a signed-in phone authorizes a new client. The
    # in-process challenge store is the source of truth (the session cookie is HttpOnly and
    # can't be encoded in a QR). No login in `none` mode → the routes 404 there. The session
    # a new client is granted is ordinary, so the forced-change gate above still applies to it.
    link_routes.register(app, cfg=cfg, store=DeviceLinkStore(), templates=_TEMPLATES)

    # Session-data routes (list/search + facets, projects, rename, favorite/unfavorite,
    # archive/unarchive, archive-older) live in routes/sessions.py; scrollback stats/clear in
    # routes/scrollback.py. Both register here, before the ws handler + SPA catch-all.
    sessions_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard, registry=registry)
    scrollback_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Settings → Maintenance (#993): cache prune + bulk mission archival. Registered BEFORE
    # missions_routes so `/api/missions/archive-older` is matched ahead of `/api/missions/{id}`.
    maintenance_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

    # Paged transcript history for scroll-up lazy-load (#348 Phase 3). GET-only (no CSRF
    # surface); additive — the ws attach payload + delta-resume contract are untouched.
    history_routes.register(app, logged_in=_logged_in)
    # AI session review (#356 Phase 1): model-list proxy + manual review + exclude toggle.
    ai_review_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # API agents (#1209): conversations BattleLab holds itself, and each agent's endpoint.
    chat_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Prompt catalog (#824): the one read/write surface for every AI system prompt.
    prompts_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Cross-engine handoff (#597): prepare (seed preview + handle) / commit (mint + bind).
    handoff_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Pulse — recent-work overview (#441 Phase 2): cached overview + manual scan. Needs the
    # registry for the live "in flight" overlay; the shared /api/ai/activity is in system.py.
    pulse_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard, registry=registry)
    # MISSION CONTROL (#846, Phase 1 of #840): the mission record — list/get/create/adopt/detach/
    # state/archive/objectives. No new decision endpoint; approve/reject stay on the pulse routes.
    missions_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard, registry=registry)
    # Automations (#1201): same auth surface as missions; Run now dispatches with this registry.
    automations_routes.register(
        app, logged_in=_logged_in, csrf_guard=_csrf_guard, registry=registry
    )

    from .routes import plugins as plugin_routes

    plugin_routes.register(
        app, logged_in=_logged_in, csrf_guard=_csrf_guard, cfg=cfg, must_change=_must_change
    )

    # Web-terminal websocket (``/ws/term/{sid}``). ``_must_change`` gates new sessions;
    # ``_reconcile_new_session`` is passed in (it + its tunables stay module-level for tests).
    terminal_routes.register(
        app,
        cfg=cfg,
        registry=registry,
        must_change=_must_change,
        reconcile_new_session=_reconcile_new_session,
    )

    # Upload route (save a pasted/dropped file to the shared uploads dir). Registered
    # before the SPA catch-all.
    upload_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Instruction templates (#905 P1): its own store off /api/config, images via /api/uploads.
    templates_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)
    # Playbooks (#1191): the gallery across sources + versioned authoring of `local` playbooks.
    playbooks_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

    # File panel (#783/#784): bounded directory listing, file read, git status/diff/branches — and
    # since #807 the upload routes and since #806 the git WRITE routes, all POST and therefore
    # CSRF-guarded. Containment lives in files.py, filewrite.py and gitpanel.discover_repo; all
    # three are security boundaries.
    files_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

    # Per-agent usage + budgets (#839): the read is cheap and always available; probing the
    # agents is behind the explicit POST.
    usage_routes.register(app, logged_in=_logged_in, csrf_guard=_csrf_guard)

    # SPA shell + history fallback. The ``/{spa_path}`` catch-all is registered LAST so it
    # never shadows the API/ws/auth routes above. ``_WEB_DIST`` / ``_SPA_RESERVED`` stay
    # defined here so tests can monkeypatch ``main._WEB_DIST`` before create_app runs.
    spa_routes.register(app, web_dist=_WEB_DIST, spa_reserved=_SPA_RESERVED)

    return app


app = (
    create_app()
    if (
        "AGENT_SESSIONS_USERNAME" in os.environ
        or os.environ.get("AGENT_SESSIONS_AUTH_MODE") == "none"
    )
    else None
)
