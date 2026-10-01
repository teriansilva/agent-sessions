"""Web-terminal websocket route (agent-sessions#265): ``/ws/term/{sid}`` — the
self-owned ws terminal (attach / resume / new-session), single-writer policy, the
server-owned SessionStream handoff, and per-tab claim/demote. Moved verbatim from
``main.create_app``.

OpenCode LAUNCH/NEW takes shared maintenance admission (#1040) before building the spawn;
compaction refuses it with retryable 4502. ATTACH does not create an engine. The non-inherited
admission is forwarded through both serving paths and released after actual process creation.

Two attach models live here, selected by ``owner.takeover_enabled()`` (#293,
default OFF):
- **flag OFF** — the original #184 path: in-memory ``SessionRegistry`` claim, a
  non-owner streams read-only with input gated.
- **flag ON** — single-active-viewer (``_serve_takeover``): ownership is anchored
  in a runtime-dir file (so prod + staging, which share the dtach masters,
  arbitrate correctly). A non-owner is NOT inert (#434): it streams the session
  **read-only** behind the take-over banner — input + resize are gated server-side
  so only the owner drives the pty geometry (#293's single-writer model holds) —
  and reconnects with ``force=1`` to take over. A second tab / device therefore
  sees live output instead of a blank screen, and an owner taken over mid-session
  is flipped to read-only IN PLACE rather than having its stream cut.

The new-session reconcile coroutine (``_reconcile_new_session``) and its ``_RECONCILE_*``
tunables stay in ``main`` and are passed in as ``reconcile_new_session``: tests monkeypatch
``main._RECONCILE_INTERVAL_S`` / ``main._RECONCILE_MAX_POLLS`` and call
``main._reconcile_new_session`` directly, so those bindings must live in that module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket

from .. import (
    ai_review_loop,
    engines,
    fsbrowse,
    handoff,
    missions,
    model_choice,
    opencode_admission,
    owner,
    perfstats,
    prefs,
    project_dirs,
    ptybridge,
    relaunch,
    scanner,
    scopedspawn,
    session_stream,
    sessionlock,
    sessions,
    transcript_owner,
    webterm,
)
from ..auth import AuthConfig, origin_matches, session_uid

log = logging.getLogger("agent_sessions.terminal")


async def resolve_physical_key(prov, native: str, *, is_new: bool) -> str | None:
    """The PHYSICAL key a terminal for ``prov:native`` must lock and attach under, or ``None``.

    The metadata alias answers first, as it always has (#127). **For a late-id engine it is not the
    only answer** (#994 review 1, finding 2): a mission that adopted a late-bound session records
    the placeholder in its store in the adopting commit and publishes the alias afterwards, so a
    publication that failed — or a restart whose repair pass has not run yet — leaves a real key
    with no alias. Resolving it to itself then takes the LOGICAL key's launch lock while the
    placeholder's master holds its own, and the ws launches a second writer beside the running
    agent. The store's mapping closes that window.

    ``None`` means the store could not be read for a key that might be mapped. The caller refuses
    rather than guessing, because the guess is exactly the duplicate writer. Pinned-id engines
    never have a mapping and never read the store; nor does the ``new=1`` launch, whose key is the
    physical one by construction. Read off the event loop, like every other store read here.
    """
    logical = f"{prov.engine_id}:{native}"
    if is_new:
        return logical
    resolved = engines.physical_key(logical)
    if resolved != logical or not getattr(prov, "new_session_reconciles", False):
        return resolved
    try:
        stored = await asyncio.to_thread(missions.physical_key_of, logical)
    except Exception:  # noqa: BLE001 — an unreadable mapping is not permission to launch
        log.warning("terminal %s: the mission store's session mapping could not be read", logical)
        return None
    return stored or logical


# How often the active viewer re-asserts its lease (#293). Must be < owner.LEASE_S so a
# live holder never reads as stale; the same call doubles as the demotion check — it
# returns False the moment another viewer (this process OR the other instance sharing the
# runtime dir) has taken the owner file, at which point we flip this viewer to read-only
# in place (gate input/resize + a fresh role frame) without dropping its stream (#434).
_HEARTBEAT_S = 2.0

# Handoff spawn-watch tasks (#597) — strong refs so the fire-and-forget aliveness gates
# aren't garbage-collected mid-sleep (asyncio only keeps weak refs to tasks).
_HANDOFF_WATCHES: set[asyncio.Task] = set()
# Provenance publication retry (#701 review round 3 P2): the watch is armed exactly once per
# target (reconnects never re-arm), so IT is the production retry vehicle for a transient
# sidecar-write failure — handoff.mark_spawned keeps retryable state and performs exactly
# the missing writes on each attempt.
_PUBLISH_ATTEMPTS = 3
_PUBLISH_RETRY_DELAY_S = 2.0
# Spawn-appearance wait (#703 review follow-up): the watch is armed at connection-accept
# time, BEFORE webterm spawns the dtach master — and that spawn can take up to
# webterm.SPAWN_TIMEOUT_S under load. So the instant-exit aliveness window must start only
# once the master has actually APPEARED; starting an 8 s timer from arm-time would abort a
# valid 8–15 s launch and delete its committed seed. Poll for the master up to the spawn
# timeout plus a margin, then apply the instant-exit check on top.
_SPAWN_APPEAR_MARGIN_S = 3.0
_SPAWN_APPEAR_POLL_S = 0.25
# Model-record watches (#1189) — strong refs for the same reason. Each one records a LAUNCH's model
# only once that launch's dtach master is accepting connections (the same appear-poll as the
# handoff watch above), and gives up — recording nothing — past the window. Past the spawn
# timeout so a spawn held in admission still records.
_MODEL_RECORD_WATCHES: set[asyncio.Task] = set()
_MODEL_RECORD_WAIT_S = webterm.SPAWN_TIMEOUT_S + 5.0
_MODEL_RECORD_POLL_S = 0.1


async def _record_model_once_live(
    engine: str, phys_native: str, key: str, sel: model_choice.Selection
) -> None:
    """Record `sel` under `key` once the master this LAUNCH spawns is live (#1189).

    The route holds the single-writer lock and `open_action` unlinked any stale socket before a
    LAUNCH, so a master accepting on this socket is the one this connection created. A refused
    argv never starts the watch; a failed spawn (4502, a bridge error, a client gone before the
    spawn) never brings a master up, so the prior record stays untouched — the route's teardown
    cancels the watch then, before it releases the lock, so it cannot outlive its connection and
    record for a master a LATER launch brings up on the same socket. The window is the backstop.
    """
    deadline = time.monotonic() + _MODEL_RECORD_WAIT_S
    while True:
        with contextlib.suppress(Exception):
            if await asyncio.to_thread(ptybridge.session_exists, engine, phys_native):
                await asyncio.to_thread(model_choice.record, key, sel)
                return
        if time.monotonic() >= deadline:
            log.info("no master for %s:%s came up; its model was not recorded", engine, phys_native)
            return
        await asyncio.sleep(_MODEL_RECORD_POLL_S)


async def _handoff_spawn_watch(engine: str, phys_native: str) -> None:
    """The one explicit spawn-success transition for a handoff target (#597): wait for the
    dtach master to APPEAR (bounded by the spawn timeout), then sleep out the same
    instant-exit window the relaunch backstop uses and either commit provenance (master
    still alive → ``handoff.mark_spawned``, retried on transient sidecar-write failures) or
    abort without any sidecar write (never appeared / died in the window →
    ``handoff.abort_spawn``). Armed at most once per target via ``handoff.arm_watch`` — a WS
    reconnect can never replay it."""
    key = f"{engine}:{phys_native}"
    # Phase 1 — wait for spawn success (the master coming up), not a fixed timer from
    # arm-time, so a slow launch isn't aborted mid-flight (#703 review follow-up).
    appear_deadline = time.monotonic() + webterm.SPAWN_TIMEOUT_S + _SPAWN_APPEAR_MARGIN_S
    appeared = False
    while time.monotonic() < appear_deadline:
        try:
            if await asyncio.to_thread(ptybridge.session_exists, engine, phys_native):
                appeared = True
                break
        except Exception:
            log.exception("handoff spawn-watch appearance probe failed for %s", key)
            return
        await asyncio.sleep(_SPAWN_APPEAR_POLL_S)
    if not appeared:
        # The master never came up within the spawn window → the launch genuinely failed.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(handoff.abort_spawn, key)
        return
    # Phase 2 — the master is up; hold the instant-exit window and confirm it STAYS up
    # (catches an agent that comes up then exits instantly, e.g. a misconfigured launch).
    await asyncio.sleep(relaunch._INSTANT_EXIT_S)
    try:
        alive = await asyncio.to_thread(ptybridge.session_exists, engine, phys_native)
    except Exception:
        log.exception("handoff spawn-watch aliveness probe failed for %s", key)
        return
    if not alive:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(handoff.abort_spawn, key)
        return
    for attempt in range(1, _PUBLISH_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(handoff.mark_spawned, key)
            return
        except Exception:
            if attempt == _PUBLISH_ATTEMPTS:
                log.exception(
                    "handoff provenance publication failed for %s after %d attempts",
                    key,
                    _PUBLISH_ATTEMPTS,
                )
            else:
                await asyncio.sleep(_PUBLISH_RETRY_DELAY_S)


def _holder_view(holder: dict | None) -> dict | None:
    """The gate payload's view of the current holder. ``label`` is client-supplied and
    UNTRUSTED — length-capped here and escaped by the UI; never used for authorization."""
    if not holder:
        return None
    return {"label": str(holder.get("label", ""))[:80], "since": holder.get("since")}


async def _open_action_offloop(
    engine: str, native: str
) -> tuple[str, sessionlock.SessionLock | None]:
    """``sessions.open_action`` off the event loop (#652 T-P4), made cancellation-safe.

    ``asyncio.to_thread``'s worker thread cannot be cancelled: if this coroutine is cancelled
    (client vanished mid-connect — most likely during the very slow-probe case T-P4 targets) while
    ``open_action`` is still running, the worker runs to completion and may return a ``LAUNCH``
    ``SessionLock``. That lock owns a raw fd with NO finalizer, so a dropped one keeps its ``flock``
    held → the session is wedged ``BUSY`` until the process restarts. So we ``shield`` the worker
    and, on cancellation, reap it via a done-callback that releases any lock it produced before the
    cancellation propagates. The callback (not a re-``await``) is used so a *second* cancellation —
    e.g. loop shutdown — can't skip the release."""
    fut = asyncio.ensure_future(asyncio.to_thread(sessions.open_action, engine, native))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:

        def _release_orphan(f: asyncio.Future) -> None:
            with contextlib.suppress(Exception):
                res = f.result()
                if res is not None and res[1] is not None:
                    res[1].release()

        if fut.done():
            _release_orphan(fut)
        else:
            fut.add_done_callback(_release_orphan)
        raise


async def _demotion_guard(
    engine: str, sid: str, conn_id: str, ws: WebSocket, read_only_gate: asyncio.Event
) -> None:
    """Owner-only lease keeper + demotion handler (#293/#434). Re-asserts the owner lease
    every ``_HEARTBEAT_S``; the instant the on-disk record stops naming us (another viewer,
    here or on the other instance sharing the runtime dir, took over) it flips this viewer
    to read-only IN PLACE — sets ``read_only_gate`` (so ``webterm.run`` drops any further
    input/resize) and sends a ``secondary`` role frame so the client shows the take-over
    banner — then returns. The stream itself keeps running: the displaced viewer watches
    the new owner's session read-only instead of going blank."""
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            if not await owner.heartbeat(engine, sid, conn_id):
                read_only_gate.set()
                holder = owner.read_owner(engine, sid)
                with contextlib.suppress(Exception):
                    await ws.send_text(
                        json.dumps(
                            {"t": "role", "role": "secondary", "holder": _holder_view(holder)}
                        )
                    )
                return
    except asyncio.CancelledError:
        raise


async def _serve_takeover(
    ws: WebSocket,
    *,
    registry: session_stream.SessionRegistry,
    engine: str,
    phys_native: str,
    phys_key: str,
    transcript_key: str,
    argv: list[str],
    cwd: str,
    init_cols: int,
    init_rows: int,
    lock,
    have: int,
    fp: str,
    tab_id: str,
    force: bool,
    label: str,
    accept_at: float | None = None,
    seed_key: str | None = None,
    maintenance_admission: opencode_admission.Admission | None = None,
) -> None:
    """Single-active-viewer attach (#293) with the read-only fallback (#434). Claims the
    runtime-dir owner file. A non-owner is NOT inert: it streams the session **read-only**
    (input + resize gated server-side) behind the take-over banner, so a second tab / device
    sees live output instead of a blank screen. Only the owner drives the pty geometry, so
    #293's single-writer model is preserved. If another viewer takes over mid-session the
    owner is flipped to read-only IN PLACE (gate + a fresh ``secondary`` role frame) without
    dropping its stream; a take-over is an explicit ``force=1`` reconnect from the banner."""
    conn_id = owner.new_conn_id()
    role, holder = await owner.claim(
        engine, phys_native, conn_id=conn_id, fp=fp, tab_id=tab_id, label=label, force=force
    )
    # The read-only gate is the single server-side source of truth for input/resize
    # suppression. A non-owner starts gated; the owner starts open and is gated live only
    # if it is demoted mid-session. The PTY stream runs either way — a non-owner is
    # read-only, never blank (#434).
    read_only_gate = asyncio.Event()
    if role == "owner":
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"t": "role", "role": "owner"}))
    else:
        read_only_gate.set()
        with contextlib.suppress(Exception):
            await ws.send_text(
                json.dumps({"t": "role", "role": "secondary", "holder": _holder_view(holder)})
            )
    # Owner only: keep the lease warm and flip to read-only in place on take-over. A
    # non-owner holds no lease (it never owns the record), so it needs no guard — it stays
    # read-only until the user hits "Take over" (a force=1 reconnect).
    guard = (
        asyncio.create_task(_demotion_guard(engine, phys_native, conn_id, ws, read_only_gate))
        if role == "owner"
        else None
    )
    attached = False
    try:
        with contextlib.suppress(Exception):
            await registry.on_attach(engine, phys_native, viewer_id=ws)
        attached = True
        # #652 measurement probe: same accept→attach latency as the #184 path, recorded
        # here too so the single-active-viewer attach model isn't a blind spot in /api/perf.
        if accept_at is not None:
            perfstats.record("attach_prep_ms", (time.monotonic() - accept_at) * 1000.0)
        await webterm.run(
            ws,
            argv,
            cwd=cwd,
            buf_key=phys_key,
            transcript_key=transcript_key,
            cols=init_cols,
            rows=init_rows,
            lock=lock,
            have=have,
            read_only_gate=read_only_gate,
            seed_key=seed_key,
            **({"maintenance_admission": maintenance_admission} if maintenance_admission else {}),
        )
    finally:
        if guard is not None:
            guard.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await guard
        # release() is conn_id-guarded: if we were taken over it names someone else and
        # this is a no-op (we never clobber the new owner).
        with contextlib.suppress(Exception):
            await owner.release(engine, phys_native, conn_id)
        if attached:
            with contextlib.suppress(Exception):
                await registry.on_detach(engine, phys_native, viewer_id=ws)


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    registry: session_stream.SessionRegistry,
    must_change: dict,
    reconcile_new_session,
) -> None:
    @app.websocket("/ws/term/{sid}")
    async def ws_term(ws: WebSocket, sid: str) -> None:
        # Accept FIRST, then close with a code on rejection. A pre-accept close fails
        # the ws handshake, and browsers report that as code 1006 (abnormal) — not our
        # 44xx — so the client reconnect loop never recognizes a deliberate reject and
        # hammers forever. Accepting then closing delivers the real code to onclose.
        # No shell is ever streamed before the checks pass, so the auth gate holds.
        await ws.accept()
        # #652 measurement probe: mark accept so we can record accept→attach latency
        # (open_action socket-probe + scan_all + role setup) right before webterm.run.
        accept_at = time.monotonic()

        async def reject(code: int, reason: str = "") -> None:
            with contextlib.suppress(Exception):
                # A close reason is at most 123 bytes on the wire; the code carries the meaning.
                await ws.close(
                    code=code, reason=reason.encode("utf-8")[:120].decode("utf-8", "ignore")
                )

        if session_uid(cfg, ws) is None:
            return await reject(4401)
        if not origin_matches(cfg, ws):
            return await reject(4403)
        if must_change["v"]:
            return await reject(4403)  # forced password change pending — no sessions yet
        is_new = ws.query_params.get("new") == "1"
        try:
            # The opencode new-session placeholder (``new-<uuid>``) is a valid id ONLY on
            # the new=1 launch path (#127); resume/attach still requires the native shape.
            prov, native = engines.parse_key(sid, allow_new_placeholder=is_new)
            # Everything below is a dtach master and a PTY (#853 §7): an engine that does not
            # run in a terminal is refused here, never treated as one.
            engines.require_pty(prov)
        except engines.EngineError:
            return await reject(4404)

        # Alias resolution (#127): for opencode new-session we launch under a placeholder
        # and reconcile to opencode's real ``ses_…`` id, persisting a placeholder→real
        # alias. When a client later attaches by the *real* id (after the URL converged or
        # an app restart), its live resources (dtach socket / single-writer lock / buffer)
        # are still under the placeholder — so resolve the real id back to the physical
        # placeholder key before any socket/lock/buffer derivation. No-op for everything
        # else. Skipped on the new=1 launch (the placeholder IS the physical key).
        # Two ids, kept distinct (#127 review): `native` is the LOGICAL/real id from the
        # URL (a real ``ses_…`` or, on new=1, the placeholder) — used for scanned-session
        # matching + ``launch_argv`` (so a real id still resumes via ``opencode --session``
        # even when the placeholder master is gone). `phys_native` is the PHYSICAL key the
        # live resources (dtach socket / single-writer lock / scrollback buffer) sit under
        # — the placeholder for a reconciled opencode session, else == native. Never
        # overwrite `native` with the placeholder, or a real URL would 4404 on LAUNCH.
        phys_native = native
        # The alias first, then — for a late-id engine with no alias — the mission store's own
        # mapping (#994 review 1, finding 2). `None` is a store we could not read for a key that
        # may be mapped: refused retryably, because resolving it to itself is the duplicate writer.
        resolved = await resolve_physical_key(prov, native, is_new=is_new)
        if resolved is None:
            return await reject(4502)
        if resolved != f"{prov.engine_id}:{native}":
            _eng, _, phys_native = resolved.partition(":")
        phys_key = f"{prov.engine_id}:{phys_native}"
        transcript_key = f"{prov.engine_id}:{native}"

        # Single-writer policy: ATTACH to a live master, LAUNCH under the launch lock,
        # or BUSY (no local master but the lock is held elsewhere — never relaunch).
        # Keyed by the PHYSICAL id so an attach by the real id finds the placeholder master.
        # #652 T-P4: run it OFF the event loop — it does 1–3 blocking socket-probe ladders
        # (up to ~1.7 s each under a slow/starved master) that would otherwise stall EVERY
        # other session's stream. It's dispatched as ONE atomic to_thread call: the single-
        # writer guarantee comes from the kernel flock (atomic across threads AND processes,
        # not merely across coroutines), so concurrent launches still resolve to one LAUNCH +
        # the rest BUSY. Never split the check/acquire across awaits — that would reopen the race.
        # `_open_action_offloop` also makes the dispatch cancellation-safe: a client that vanishes
        # mid-connect must not orphan a LAUNCH lock (its flock has no finalizer → session BUSY).
        action, lock = await _open_action_offloop(prov.engine_id, phys_native)
        if action == sessions.BUSY:
            return await reject(4409)  # held by another writer; client should retry → attach
        # A RETIRING engine (#853 P3 — its manifest is gone) is ATTACH-ONLY: a live master stays
        # reachable, but a dead one is never relaunched and no new session starts. Refused with the
        # terminal code, so the client stops retrying instead of hammering a launch that cannot be.
        if engines.is_retiring(prov) and action != sessions.ATTACH:
            if lock is not None:
                lock.release()
            return await reject(4404)
        # Bounded relaunch backstop (#631): if this key's launched agent has exited instantly
        # several times in a row, stop relaunching and close on a TERMINAL code (4500) so the
        # client's retry loop ends instead of hammering the backend forever. 4409 (busy) / 4502
        # (transient) stay retryable — only this backstop emits a terminal code, and only after
        # repeated instant exits. LAUNCH-only: ATTACH never relaunches, so it is never blocked.
        if action == sessions.LAUNCH and relaunch.blocked(phys_key):
            if lock is not None:
                lock.release()
            return await reject(4500)
        # Set once we are actually about to run a fresh master (past every reject) so the finally
        # can record whether that launch exited instantly (backstop bookkeeping, #631).
        launch_started_at: float | None = None
        # New-session reconcile (#127 opencode / #315 codex): set when this connection
        # launches a mint-its-own-id placeholder; runs concurrently with the PTY bridge to
        # discover the engine's real id, persist the alias, and converge the client URL.
        reconcile_task = None
        maintenance_admission = None
        # A requested model to record once the launched master is live (#1189): the key it is
        # recorded under and the resolved selection. Never set on ATTACH — `dtach -a` carries no
        # agent argv, so an attach cannot change the model and must not change the record.
        model_record: tuple[str, model_choice.Selection] | None = None
        model_watch: asyncio.Task | None = None
        try:
            if action == sessions.LAUNCH:
                try:
                    maintenance_admission = await opencode_admission.for_launch(prov.engine_id)
                except opencode_admission.Unavailable:
                    return await reject(4502)
            if action == sessions.ATTACH:
                # A live dtach session already exists → attach regardless of new/resume.
                # dtach -A attaches (ignoring the cmd), so a fresh session survives a
                # browser reload before it has written its on-disk history. cwd is only
                # for the (unused-on-attach) spawn; a scanned cwd if known, else home.
                # OFF the event loop (#867 review round 8), and ONE session rather than a walk of
                # every store (#991). This runs on EVERY warm reconnect, between the async dispatch
                # and `webterm.run()`; a map opening eight windows used to start eight concurrent
                # full walks that finished together 30–60 s later. `resolve_session` reads only
                # this session's binding — a single-key lookup where the engine has one, else a walk
                # that STARTED after this connect arrived — so the row authorized below is never the
                # sidebar's TTL snapshot. It is load-bearing for authorization, so it cannot simply
                # be dropped.
                scanned = await asyncio.to_thread(
                    # The CONNECT's arrival (same monotonic clock as the coordinator), not the
                    # moment a worker thread picks this up — or connects accepted before a walk
                    # would each demand a newer one (#991 review).
                    engines.resolve_session,
                    prov.engine_id,
                    native,
                    arrival=accept_at,
                )
                cwd = scanned.cwd if scanned else str(Path.home())
                # The hard boundary applies to ATTACH too (#867 review round 6). It used to live
                # only in the resume branch below, so a session with a LIVE dtach master was
                # attachable even when its cwd is excluded or outside every root — the lookup
                # 404s it and a cold resume refuses it, but a warm one handed over the terminal.
                # Whether a master happens to be running is not an authorization fact.
                #
                # An ATTACH with NOTHING scanned cannot be authorized on its cwd: `scanned` is
                # where the cwd comes from. It is usually a fresh session whose transcript has not
                # landed yet (a browser reload in the first seconds), whose cwd was scope-checked
                # on the launch that created it — but "usually" is not an authorization argument,
                # and a live master is not proof that the CURRENT config still allows that cwd
                # (roots or exclusions can change in between).
                #
                # So it fails CLOSED exactly where the operator has asked for a boundary, and open
                # where they have not (#867 review round 7): with no roots and no exclusions
                # configured there is nothing to enforce and the reload case is preserved for the
                # default install; with either configured, an unidentifiable session is refused.
                # A stricter fix — persisting the launch cwd at the master boundary so unknown
                # rows can be authorized properly — is a change to the session-lock layer and
                # belongs in its own issue, not here.
                _roots = project_dirs.effective_roots()
                _exclusions = prefs.get_folder_exclusions()
                _boundary_configured = bool(_roots) or bool(_exclusions)
                if (
                    scanned is None
                    and _boundary_configured
                    or scanned is not None
                    and not project_dirs.in_scope(scanned.cwd, roots=_roots, exclusions=_exclusions)
                ):
                    if lock is not None:
                        lock.release()
                    return await reject(4404)
                # No launch argv on ATTACH: `dtach -a` never execs the agent, and building one
                # would make attaching to a LIVE master depend on the binary still resolving
                # (#853 P2 — a removed or updated binary must never strand a running session).
                launch = None
            elif is_new:
                # Start a FRESH session with this client-generated id, in a picker cwd.
                # The new-session picker offers two sources, so the launch must accept BOTH or a
                # cwd the UI presented gets rejected: (1) pickable_projects — the all-engine
                # scanned cwds ∪ ~/claude subdirs (#196), which may live outside $HOME; and (2)
                # any directory the home-rooted folder picker can browse to (#448's
                # /api/folders/browse offers every $HOME subdir, well beyond pickable_projects).
                # Validating only against (1) 4404'd a browsed subfolder as "session not found"
                # (#457). fsbrowse.is_browsable_dir is the security boundary: its realpath
                # containment rejects any path whose target escapes $HOME.
                new_cwd = ws.query_params.get("cwd") or ""

                # Hard root scope (#465/#467): when project roots are configured a new session may
                # launch ONLY in an in-scope cwd — the same scope the list/picker/facets enforce —
                # so a direct ws request can't start outside it. pickable_projects is already
                # root/exclusion-scoped; the home-browsable branch gets the explicit in_scope guard.
                # Empty roots ⇒ unscoped (today's behaviour).
                #
                # The whole validation runs OFF the event loop as one unit (#991): the picker
                # needs every session's cwd, which is a disk walk (or a wait on the walk in
                # flight), and on the loop it froze every terminal stream for that long. The
                # picker is a list of offered cwds, not a session binding, so the TTL snapshot
                # is acceptable here.
                def _new_cwd_allowed() -> bool:
                    roots = project_dirs.effective_roots()
                    exclusions = prefs.get_folder_exclusions()
                    in_picker = new_cwd in set(
                        scanner.pickable_projects(
                            sessions=engines.scan_all_cached(), roots=roots, exclusions=exclusions
                        )
                    )
                    browsable_ok = fsbrowse.is_browsable_dir(new_cwd) and (
                        not roots
                        or project_dirs.in_scope(new_cwd, roots=roots, exclusions=exclusions)
                    )
                    return in_picker or browsable_ok

                if not await asyncio.to_thread(_new_cwd_allowed):
                    return await reject(4404)
                # Honor the modal's permission-bypass choice (default on); only "0" is off.
                bypass = ws.query_params.get("bypass") != "0"
                # The requested model (#1189), resolved by THE resolver before anything is
                # snapshotted, recorded or spawned: shape, membership in the manifest's list or the
                # operator's added ids, alias → id. A refusal is terminal (4422, never retried) and
                # never a silent fall back to `default`. The Selection it returns is the only thing
                # the launch argv accepts, so the id validated is the id launched.
                try:
                    model_sel = await asyncio.to_thread(
                        model_choice.select, prov, ws.query_params.get("model")
                    )
                except model_choice.ModelRefused as e:
                    return await reject(4422, e.detail)  # the finally hands back the lock
                # Mint-its-own-id engines (opencode, codex) can't pin a new-session id: they
                # launch under the placeholder and we diff the engine's store to find the real
                # id (#127/#315). Snapshot the cwd's existing ids BEFORE launch so the diff
                # attributes the one new id to us; then arm the concurrent reconcile. A None
                # snapshot means the baseline read FAILED (not empty) — we skip reconciliation
                # entirely rather than risk misattributing a pre-existing id, and serve under
                # the placeholder.
                new_snapshot = None
                if getattr(prov, "new_session_reconciles", False):
                    # A mint-its-own-id engine MUST launch under a ``new-<uuid>`` placeholder,
                    # never a real id: its ``new_launch_argv`` ignores ``native`` and starts a
                    # FRESH process, so a real id here would key the socket/lock/scrollback by an
                    # existing session's identity (collision) and skip reconcile. Reject before
                    # launch (the client always mints a placeholder for these engines).
                    if not engines.is_new_session_placeholder(f"{prov.engine_id}:{native}"):
                        return await reject(4404)
                    # Off the event loop (#991): for codex this is a recursive rollout walk that
                    # reads every matching file's head. Still taken BEFORE the launch argv is built,
                    # and a `None` (failed baseline read) still disables reconciliation below.
                    new_snapshot = await asyncio.to_thread(prov.snapshot_session_ids, new_cwd)
                try:
                    # OFF the loop: provenance walks every directory up to the binary (#853 P2),
                    # and on a host with networked user lookups that must not stall every stream.
                    launch = await asyncio.to_thread(
                        prov.new_launch_argv,
                        native,
                        cwd=new_cwd,
                        bypass=bypass,
                        **({"model": model_sel} if model_sel.flag else {}),
                    )
                except NotImplementedError:
                    return await reject(4404)  # engine can't pin a new-session id
                except engines.EngineError:
                    # No binary, or one provenance refuses (#853 §2b): the same terminal code a
                    # misconfigured launch has always had.
                    return await reject(4500)
                cwd = new_cwd
                if model_sel.model is not None:  # a new `default` launch has nothing to record
                    model_record = (f"{prov.engine_id}:{native}", model_sel)
                # Auto-include the launch cwd in `included` mode (#335): now that the new-session
                # request has PASSED validation (cwd is a real pickable project) and the launch is
                # accepted, add the dir to the allowlist so the session is visible in the curated
                # sidebar now. Reached only past the 4404 rejections above, so a typo/invalid
                # cwd never grows the list. No-op in `all` mode; best-effort (a write must never
                # block the terminal).
                if prefs.get_projects_mode() == "included":
                    with contextlib.suppress(Exception):
                        prefs.add_project_included(new_cwd)
                if new_snapshot is not None:
                    reconcile_task = asyncio.create_task(
                        reconcile_new_session(ws, prov, native, new_cwd, new_snapshot)
                    )
                if not getattr(prov, "new_session_reconciles", False):
                    # Pinned-id new session (e.g. claude, shell): the key is final at launch.
                    # An engine with no native store of its own (shell, #636) persists a record
                    # NOW — after cwd validation (every reject is above), and BEFORE the review
                    # wake / cache bust below — so the row is scannable/listable the instant the
                    # review loop can pick it up (never review-woken while invisible to scan).
                    # Best-effort: a sidecar write must never block the terminal. The
                    # PtyBridgeError path below removes it if the launch argv is rejected, so a
                    # failed new session leaves no phantom row. No-op for engines without the hook.
                    on_new = getattr(prov, "on_new_session", None)
                    if on_new is not None:

                        def _record_new_session() -> None:
                            with contextlib.suppress(Exception):
                                on_new(native, cwd=cwd)

                        # A file write — off the event loop like the rest of this path (#991).
                        await asyncio.to_thread(_record_new_session)
                    # The key is final, so wake the AI-review loop to summarize it promptly (#413).
                    # Mint-its-own-id engines are kicked from the reconcile coroutine instead.
                    ai_review_loop.request_review_soon()
                    # A new session just appeared (claude JSONL / shell record) → bust the
                    # sidebar's scan snapshot so it shows on the next list without the TTL lag
                    # (#561). Off the loop (#991): it takes the walk coordinator's lock.
                    await asyncio.to_thread(engines.invalidate_scan_cache)
            else:
                # Resume an EXISTING scanned session.
                # Same boundary, same reason (#867 review round 8): resolved exactly like ATTACH —
                # off the loop, one session, a binding read at or after this connect arrived (#991).
                match = await asyncio.to_thread(
                    # The CONNECT's arrival (same monotonic clock as the coordinator), not the
                    # moment a worker thread picks this up — or connects accepted before a walk
                    # would each demand a newer one (#991 review).
                    engines.resolve_session,
                    prov.engine_id,
                    native,
                    arrival=accept_at,
                )
                # Hard root scope (#465/#467): a session whose cwd is outside the configured roots
                # (or under an exclusion) is hidden from the list/picker AND not resumable here —
                # otherwise the ws would be a back door to the scoped-out sessions.
                #
                # The predicate is called UNCONDITIONALLY (#867 review round 4). It used to be
                # guarded by `roots and …`, on the reading that empty roots mean "the #465 feature
                # is off". But `in_scope` checks EXCLUSIONS FIRST and only then falls through on
                # empty roots — so with no roots configured (the common case) an explicitly
                # excluded session stayed resumable, which is precisely the back door this block
                # exists to close. Empty roots still leave the ROOT half off; an exclusion binds
                # either way.
                roots = project_dirs.effective_roots()
                exclusions = prefs.get_folder_exclusions()
                # (The old `match.cwd in scanned_cwds(all_sessions)` clause was implied by `match`
                # coming from that same list; a resolved row carries its own cwd, so it is gone.)
                if match is None or not project_dirs.in_scope(
                    match.cwd, roots=roots, exclusions=exclusions
                ):
                    return await reject(4404)
                # Background-agent guard (#631): this id is resumable on disk, but action is
                # LAUNCH (no master of ours) AND a live process already owns its transcript — a
                # Claude background agent (``claude daemon`` fork). ``claude --resume`` would print
                # "currently running as a background agent" and exit instantly, relaunch-looping.
                # Refuse with a terminal code so the client shows "not attachable" instead of
                # retrying. Claude-only (background agents are a Claude concept).
                if transcript_owner.owned_elsewhere(prov, native):
                    return await reject(4404)
                # A model on RESUME (#1189): applied only where the manifest says the engine honours
                # it (`on_resume`); otherwise only the model the session recorded is accepted — and
                # a session with nothing recorded matches nothing. Refused before spawn.
                try:
                    model_sel = await asyncio.to_thread(  # a sidecar read: off the loop
                        model_choice.select_resume,
                        prov,
                        ws.query_params.get("model"),
                        transcript_key,
                    )
                except model_choice.ModelRefused as e:
                    return await reject(4422, e.detail)  # the finally hands back the lock
                try:
                    launch = await asyncio.to_thread(  # off the loop, as above
                        prov.launch_argv,
                        native,
                        cwd=match.cwd,
                        bypass=True,
                        **({"model": model_sel} if model_sel.flag else {}),
                    )
                except engines.EngineError:
                    return await reject(4500)  # no binary / refused by provenance (#853 §2b)
                cwd = match.cwd
                if model_sel.flag or model_sel.replaces_record:
                    model_record = (transcript_key, model_sel)
            try:
                # Mode-explicit dtach (#165): on ATTACH the server has already verified
                # a live master exists, so `dtach -a` is correct (and refuses to silently
                # create a second master if the probe-vs-attach race lost). On LAUNCH the
                # server holds the lock and any stale sock was unlinked in `open_action`,
                # so `dtach -c` will bind cleanly. Socket is keyed by the PHYSICAL id so
                # attach/resume by the real id reaches the same master.
                if action == sessions.ATTACH:
                    argv = ptybridge.attach_argv(engine=prov.engine_id, session_id=phys_native)
                else:
                    argv = ptybridge.launch_argv(
                        engine=prov.engine_id, session_id=phys_native, launch_argv=launch
                    )
                    # Per-session transient scope (#346 Phase B): the dtach master + the
                    # agent tree it forks land in their own cgroup, so one runaway session
                    # can't fail the broker unit or drain its task budget. Falls through
                    # unwrapped when scopes are disabled/unavailable (logged inside wrap).
                    argv, scope_unit = scopedspawn.wrap(
                        argv, engine=prov.engine_id, session_id=phys_native
                    )
                    if scope_unit is not None:
                        log.info("launching %s in scope %s", phys_key, scope_unit)
            except ptybridge.PtyBridgeError:
                # A pinned-id new session persisted a record above; the launch argv was rejected,
                # so drop it rather than leave a phantom row (best-effort, no-op for engines
                # without the hook / for attach + resume where nothing was written).
                if is_new:
                    on_fail = getattr(prov, "on_new_session_failed", None)
                    if on_fail is not None:
                        with contextlib.suppress(Exception):
                            on_fail(native)
                return await reject(4500)  # misconfigured launch (e.g. bare-name binary)
            if model_record is not None:
                # The launch argv was accepted; record what was asked for once the master it
                # spawns is actually live — never before (a failed spawn keeps the prior record).
                # Keyed like every sidecar field (a placeholder carries it through adoption).
                model_watch = asyncio.create_task(
                    _record_model_once_live(prov.engine_id, phys_native, *model_record)
                )
                _MODEL_RECORD_WATCHES.add(model_watch)
                model_watch.add_done_callback(_MODEL_RECORD_WATCHES.discard)
            # Delta-resume: a reconnecting client reports the absolute byte offset it
            # last saw; we stream only the bytes since then (never re-blank). Bad/absent
            # value → 0 → full replay. buf_key is the PHYSICAL key (placeholder for an
            # opencode new-session) so scrollback stays under one key across the alias.
            try:
                have = max(0, int(ws.query_params.get("have", "0") or "0"))
            except (ValueError, TypeError):
                have = 0

            # Initial PTY size (#227): size the pty to the client's real grid up front, so a
            # launched agent renders at the right width from its first frame instead of starting
            # at 80x24 and then reflowing (garbling scrollback) when the client's first resize
            # lands. A reconnect/attach also sizes the dtach-client pty correctly from the start.
            def _dim(name: str, default: int, hi: int) -> int:
                try:
                    return max(1, min(hi, int(ws.query_params.get(name, "") or default)))
                except (ValueError, TypeError):
                    return default

            init_cols = _dim("cols", 80, 500)
            init_rows = _dim("rows", 24, 300)
            # Handoff to the server-owned SessionStream registry (#183 slice 2).
            # on_attach STOPS any running server-owned stream for this key, so the
            # WS bridge becomes the sole writer to ``_BUFFERS[phys_key]`` during
            # the attached window; on_detach (in the finally) spawns a fresh
            # server-owned stream if the dtach master is still alive. Best-effort
            # — registry errors must not affect the browser path.
            registry = app.state.session_registry
            fp = ws.query_params.get("fp", "") or ""
            tab_id = ws.query_params.get("tab", "") or ""
            force = ws.query_params.get("force", "") == "1"
            # Backstop timing (#631): mark when this fresh master started, so the finally can tell
            # whether it exited instantly and feed the bounded-relaunch guard. Only on LAUNCH — an
            # ATTACH runs no new master. Covers BOTH dispatch paths (take-over + #184) below.
            if action == sessions.LAUNCH:
                launch_started_at = time.monotonic()
            # Cross-engine handoff (#597): a committed handoff bound to this physical key
            # delivers its seed via webterm's PTY injector (input, never argv). ATTACH also
            # carries the key: if the launching viewer dropped before the TUI was ready, the
            # unredeemed seed is injected by the next attach — redemption stays atomic in
            # handoff.py either way. The spawn watch (the aliveness gate that later writes
            # provenance) arms at most once, on the connection that actually LAUNCHed.
            seed_key = phys_key if handoff.has_pending_seed(phys_key) else None
            if action == sessions.LAUNCH and handoff.arm_watch(phys_key):
                watch = asyncio.create_task(_handoff_spawn_watch(prov.engine_id, phys_native))
                _HANDOFF_WATCHES.add(watch)
                watch.add_done_callback(_HANDOFF_WATCHES.discard)
            # Single-active-viewer + explicit take-over (#293), flag-gated (default OFF →
            # the #184 path below is byte-identical, so merging this is a prod no-op). The
            # flag-on path anchors ownership in a runtime-dir file so prod + staging — which
            # SHARE the dtach masters — arbitrate correctly, and a non-owner is INERT: it
            # gets the gate, not a read-only byte stream.
            if owner.takeover_enabled():
                label = (ws.query_params.get("label", "") or "")[:80]
                await _serve_takeover(
                    ws,
                    registry=registry,
                    engine=prov.engine_id,
                    phys_native=phys_native,
                    phys_key=phys_key,
                    transcript_key=transcript_key,
                    argv=argv,
                    cwd=cwd,
                    init_cols=init_cols,
                    init_rows=init_rows,
                    lock=lock,
                    have=have,
                    fp=fp,
                    tab_id=tab_id,
                    force=force,
                    label=label,
                    accept_at=accept_at,
                    seed_key=seed_key,
                    **(
                        {"maintenance_admission": maintenance_admission}
                        if maintenance_admission
                        else {}
                    ),
                )
                return
            # ---- #184 path (flag OFF): in-memory claim + read-only secondary stream ----
            with contextlib.suppress(Exception):
                await registry.on_attach(prov.engine_id, phys_native, viewer_id=ws)
            # Per-tab claim (#184 slice 3): empty fp/tab from an older client
            # falls through as "owner with no recorded claim" (backward-compat).
            # ``force=1`` lets a deliberate takeover demote a stale or recent owner.
            role = "owner"
            claim_obj: session_stream.Claim | None = None
            with contextlib.suppress(Exception):
                role, claim_obj = await registry.claim(
                    prov.engine_id, phys_native, fp, tab_id, force=force
                )
            # Read-only gate fires when the WS is a secondary OR when a force
            # takeover demotes the owner mid-session. Server-side gate is the
            # source of truth — pump_in drops input/resize while it's set.
            read_only_gate = asyncio.Event()
            if role == "secondary":
                read_only_gate.set()
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"t": "role", "role": role}))
            # Watcher: if another tab force-claims, demoted fires → flip gate
            # + tell the browser so it can render the read-only banner.
            demote_task: asyncio.Task | None = None
            if claim_obj is not None:

                async def _watch_demote() -> None:
                    assert claim_obj is not None
                    await claim_obj.demoted.wait()
                    read_only_gate.set()
                    with contextlib.suppress(Exception):
                        await ws.send_text(json.dumps({"t": "role", "role": "secondary"}))

                demote_task = asyncio.create_task(_watch_demote())
            # #652 measurement probe: accept→attach prep latency (all the blocking
            # connect-path work before the bridge starts pumping — the open_action probe
            # ladder and the session resolution, #991). This is what T3/T-P4/L1 move.
            perfstats.record("attach_prep_ms", (time.monotonic() - accept_at) * 1000.0)
            try:
                await webterm.run(
                    ws,
                    argv,
                    cwd=cwd,
                    buf_key=phys_key,
                    transcript_key=transcript_key,
                    cols=init_cols,
                    rows=init_rows,
                    lock=lock,
                    have=have,
                    read_only_gate=read_only_gate,
                    seed_key=seed_key,
                    **(
                        {"maintenance_admission": maintenance_admission}
                        if maintenance_admission
                        else {}
                    ),
                )
            finally:
                if demote_task is not None:
                    demote_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await demote_task
                with contextlib.suppress(Exception):
                    if claim_obj is not None:
                        await registry.release(prov.engine_id, phys_native, fp, tab_id)
                with contextlib.suppress(Exception):
                    await registry.on_detach(prov.engine_id, phys_native, viewer_id=ws)
        finally:
            # Early rejection never reaches webterm's handoff; release its admission here.
            if maintenance_admission is not None:
                maintenance_admission.release()
            # Backstop bookkeeping (#631): if this connection launched a fresh master, record
            # how it ended. A master still alive (a normal detach) or one that ran past the
            # instant-exit window resets the key; a master gone within the window is an instant
            # exit that counts toward the relaunch cap. Best-effort — never let it break teardown.
            if launch_started_at is not None:
                with contextlib.suppress(Exception):
                    lived = time.monotonic() - launch_started_at
                    master_alive = ptybridge.session_exists(prov.engine_id, phys_native)
                    relaunch.note_exit(phys_key, lived, master_alive=master_alive)
            # A LAUNCH that left no master behind (the spawn failed, 4502, a bridge error, the
            # client gone before the spawn) stops its model-record watch HERE, while this
            # connection still holds the launch lock: once the lock is released a later connection
            # may bring a master up on the same socket, and this watch must never record its own
            # model for that one (#1189). A master that did come up outlives this client, so its
            # watch keeps running. Unknown liveness counts as no master — recording nothing keeps
            # the prior record, the strictly safer miss.
            if model_watch is not None and not model_watch.done():
                master_up = False
                with contextlib.suppress(Exception):
                    master_up = bool(ptybridge.session_exists(prov.engine_id, phys_native))
                if not master_up:
                    model_watch.cancel()
            # Cancel the reconcile probe, but NEVER let its cancellation (a BaseException,
            # not Exception) bypass the lock handoff below — nest it in its own try/finally
            # and suppress CancelledError too (#127 review).
            try:
                if reconcile_task is not None:
                    reconcile_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reconcile_task
            finally:
                # Hand the launch lock to the dtach master we spawned (it inherited the
                # fd), so the flock lives for the master's lifetime — closing our fd
                # without unlocking keeps it held while the master runs, and releases it if
                # no master was spawned (early reject) or once the master dies. ATTACH
                # holds no lock.
                if lock is not None:
                    lock.transfer()
