"""Web-terminal websocket route (agent-sessions#265): ``/ws/term/{sid}`` — the
self-owned ws terminal (attach / resume / new-session), single-writer policy, the
server-owned SessionStream handoff, and per-tab claim/demote. Moved verbatim from
``main.create_app``.

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
from pathlib import Path

from fastapi import FastAPI, WebSocket

from .. import (
    ai_review_loop,
    engines,
    fsbrowse,
    owner,
    prefs,
    ptybridge,
    scanner,
    scopedspawn,
    session_stream,
    sessions,
    webterm,
)
from ..auth import AuthConfig, origin_matches, session_uid

log = logging.getLogger("agent_sessions.terminal")

# How often the active viewer re-asserts its lease (#293). Must be < owner.LEASE_S so a
# live holder never reads as stale; the same call doubles as the demotion check — it
# returns False the moment another viewer (this process OR the other instance sharing the
# runtime dir) has taken the owner file, at which point we flip this viewer to read-only
# in place (gate input/resize + a fresh role frame) without dropping its stream (#434).
_HEARTBEAT_S = 2.0


def _holder_view(holder: dict | None) -> dict | None:
    """The gate payload's view of the current holder. ``label`` is client-supplied and
    UNTRUSTED — length-capped here and escaped by the UI; never used for authorization."""
    if not holder:
        return None
    return {"label": str(holder.get("label", ""))[:80], "since": holder.get("since")}


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
        await webterm.run(
            ws,
            argv,
            cwd=cwd,
            buf_key=phys_key,
            cols=init_cols,
            rows=init_rows,
            lock=lock,
            have=have,
            read_only_gate=read_only_gate,
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

        async def reject(code: int) -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=code)

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
        if not is_new:
            resolved = engines.physical_key(f"{prov.engine_id}:{native}")
            if resolved != f"{prov.engine_id}:{native}":
                _eng, _, phys_native = resolved.partition(":")
        phys_key = f"{prov.engine_id}:{phys_native}"

        # Single-writer policy: ATTACH to a live master, LAUNCH under the launch lock,
        # or BUSY (no local master but the lock is held elsewhere — never relaunch).
        # Keyed by the PHYSICAL id so an attach by the real id finds the placeholder master.
        action, lock = sessions.open_action(prov.engine_id, phys_native)
        if action == sessions.BUSY:
            return await reject(4409)  # held by another writer; client should retry → attach
        # New-session reconcile (#127 opencode / #315 codex): set when this connection
        # launches a mint-its-own-id placeholder; runs concurrently with the PTY bridge to
        # discover the engine's real id, persist the alias, and converge the client URL.
        reconcile_task = None
        try:
            if action == sessions.ATTACH:
                # A live dtach session already exists → attach regardless of new/resume.
                # dtach -A attaches (ignoring the cmd), so a fresh session survives a
                # browser reload before it has written its on-disk history. cwd is only
                # for the (unused-on-attach) spawn; a scanned cwd if known, else home.
                scanned = next(
                    (
                        s
                        for s in engines.scan_all()
                        if s.engine == prov.engine_id and s.uuid == native
                    ),
                    None,
                )
                cwd = scanned.cwd if scanned else str(Path.home())
                launch = prov.launch_argv(native, cwd=cwd, bypass=True)
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
                if not (
                    new_cwd in set(scanner.pickable_projects(sessions=engines.scan_all()))
                    or fsbrowse.is_browsable_dir(new_cwd)
                ):
                    return await reject(4404)
                # Honor the modal's permission-bypass choice (default on); only "0" is off.
                bypass = ws.query_params.get("bypass") != "0"
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
                    new_snapshot = prov.snapshot_session_ids(new_cwd)
                try:
                    launch = prov.new_launch_argv(native, cwd=new_cwd, bypass=bypass)
                except NotImplementedError:
                    return await reject(4404)  # engine can't pin a new-session id
                cwd = new_cwd
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
                    # Pinned-id new session (e.g. claude): the key is final at launch, so wake
                    # the AI-review loop to summarize it promptly (#413). Mint-its-own-id engines
                    # are kicked from the reconcile coroutine once their real id is durable.
                    ai_review_loop.request_review_soon()
            else:
                # Resume an EXISTING scanned session.
                sessions_all = engines.scan_all()
                match = next(
                    (s for s in sessions_all if s.engine == prov.engine_id and s.uuid == native),
                    None,
                )
                if match is None or match.cwd not in scanner.scanned_cwds(sessions_all):
                    return await reject(4404)
                launch = prov.launch_argv(native, cwd=match.cwd, bypass=True)
                cwd = match.cwd
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
                return await reject(4500)  # misconfigured launch (e.g. bare-name binary)
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
            try:
                await webterm.run(
                    ws,
                    argv,
                    cwd=cwd,
                    buf_key=phys_key,
                    cols=init_cols,
                    rows=init_rows,
                    lock=lock,
                    have=have,
                    read_only_gate=read_only_gate,
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
