"""Web-terminal websocket route (agent-sessions#265): ``/ws/term/{sid}`` — the
self-owned ws terminal (attach / resume / new-session), single-writer policy, the
server-owned SessionStream handoff, and per-tab claim/demote. Moved verbatim from
``main.create_app``.

The opencode new-session reconcile coroutine (``_reconcile_opencode``) and its
``_OC_RECONCILE_*`` tunables stay in ``main`` and are passed in as ``reconcile_opencode``:
tests monkeypatch ``main._OC_RECONCILE_INTERVAL_S`` / ``main._OC_RECONCILE_MAX_POLLS`` and
call ``main._reconcile_opencode`` directly, so those bindings must live in that module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket

from .. import (
    engines,
    ptybridge,
    scanner,
    session_stream,
    sessions,
    webterm,
)
from ..auth import AuthConfig, origin_matches, session_uid


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    registry: session_stream.SessionRegistry,
    must_change: dict,
    reconcile_opencode,
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
        # opencode new-session reconcile (#127): set when this connection launches an
        # opencode placeholder; runs concurrently with the PTY bridge to discover
        # opencode's real ``ses_…`` id, persist the alias, and converge the client URL.
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
                # Validate against the same all-engine superset the picker offers (#196),
                # so a cwd the UI presented is never rejected on launch.
                new_cwd = ws.query_params.get("cwd") or ""
                if new_cwd not in set(scanner.pickable_projects(sessions=engines.scan_all())):
                    return await reject(4404)
                # Honor the modal's permission-bypass choice (default on); only "0" is off.
                bypass = ws.query_params.get("bypass") != "0"
                # opencode can't pin a new-session id: it launches under the placeholder
                # and we DB-diff opencode.db to find the real id (#127). Snapshot the
                # cwd's existing ids BEFORE launch so the diff attributes the one new id
                # to us; then arm the concurrent reconcile. A None snapshot means the
                # baseline read FAILED (not empty) — we skip reconciliation entirely rather
                # than risk misattributing a pre-existing row, and serve under the placeholder.
                oc_snapshot = None
                if engines.is_opencode_new_placeholder(f"{prov.engine_id}:{native}"):
                    oc_snapshot = prov.snapshot_session_ids(new_cwd)
                try:
                    launch = prov.new_launch_argv(native, cwd=new_cwd, bypass=bypass)
                except NotImplementedError:
                    return await reject(4404)  # engine can't pin a new-session id
                cwd = new_cwd
                if oc_snapshot is not None:
                    reconcile_task = asyncio.create_task(
                        reconcile_opencode(ws, prov, native, new_cwd, oc_snapshot)
                    )
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
            with contextlib.suppress(Exception):
                await registry.on_attach(prov.engine_id, phys_native)
            # Per-tab claim (#184 slice 3): empty fp/tab from an older client
            # falls through as "owner with no recorded claim" (backward-compat).
            # ``force=1`` lets a deliberate takeover demote a stale or recent owner.
            fp = ws.query_params.get("fp", "") or ""
            tab_id = ws.query_params.get("tab", "") or ""
            force = ws.query_params.get("force", "") == "1"
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
                    await registry.on_detach(prov.engine_id, phys_native)
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
