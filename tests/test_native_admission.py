"""Actual console process creation and alias publication share native admission (#1277)."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_native_discovery
import test_plugin_manager
import test_webterm
from agent_sessions import engines, main, metadata, mission_dispatch, native_ownership, webterm
from agent_sessions.plugins import admission, manager, process, storage
from test_native_discovery import NATIVE, OTHER, reserve

source = test_native_discovery.source
recipe = test_plugin_manager.recipe


def test_bound_and_pending_console_admission_are_refused_but_other_history_still_opens(source):
    reserve(source, NATIVE)
    with admission.acquire(source, NATIVE) as guard:
        assert "API session" in guard.reason
    with admission.acquire(source, OTHER) as guard:
        assert not guard.reason
    reserve(source)
    with admission.acquire(source, f"new-{OTHER}") as guard:
        assert "unresolved API creation" in guard.reason
    with admission.acquire(source, OTHER) as guard:  # a concrete existing history resumes
        assert not guard.reason
    with admission.acquire(engines.get("shell"), OTHER) as guard:
        assert not guard.reason


@pytest.mark.anyio
@pytest.mark.parametrize("attaching", [False, True])
async def test_webterm_checks_logical_history_before_actual_process_creation(
    source, monkeypatch, attaching
):
    reserve(source, NATIVE)
    closed = []

    async def close(**kwargs):
        closed.append(kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("an API-owned native history reached console process creation")

    monkeypatch.setattr(webterm.asyncio, "create_subprocess_exec", forbidden)
    await webterm.run(
        SimpleNamespace(close=close),
        ["/bin/true"],
        cwd="/tmp",
        buf_key=f"{source.engine_id}:new-{OTHER}",
        transcript_key=f"{source.engine_id}:{NATIVE}",
        **({"attach_provider": source} if attaching else {"launch_provider": source}),
    )
    assert len(closed) == 1 and "API session" in closed[0]["reason"]


def test_retiring_attach_preserves_unrelated_live_console_during_native_provisioning(
    source, monkeypatch
):
    reserve(source, NATIVE)
    reserve(source)
    monkeypatch.setattr(source, "retiring", True)
    monkeypatch.delitem(engines.registry._BY_ID, source.engine_id)
    monkeypatch.setitem(engines.registry._RETIRING, source.engine_id, source)
    with admission.acquire_attach(source, OTHER) as guard:
        assert not guard.reason
    with admission.acquire_attach(source, NATIVE) as guard:
        assert "API session" in guard.reason


@pytest.mark.anyio
async def test_attach_process_handoff_holds_no_launch_lock_and_closes_on_failure(
    source, monkeypatch
):
    attempts = []
    closed = []

    async def spawn(*args, **kwargs):
        # An attach creates no agent work: it must not hold the app-wide launch lock through
        # the dtach-client handoff (that queued every attach behind slow launches). Binding is
        # still ordered against it because bind refuses a history with a live console socket.
        with storage.locked(admission.LOCK, wait=0):
            pass
        attempts.append(True)
        raise OSError("synthetic attach process failure")

    async def close(**kwargs):
        closed.append(kwargs)

    monkeypatch.setattr(webterm.asyncio, "create_subprocess_exec", spawn)
    await webterm.run(
        SimpleNamespace(close=close),
        ["dtach", "-a"],
        cwd="/tmp",
        buf_key=f"{source.engine_id}:{OTHER}",
        attach_provider=source,
    )
    assert attempts == [True] and closed == [{"code": 4502}]
    with storage.locked(admission.LOCK, wait=0):
        pass


@pytest.mark.parametrize("takeover", [False, True])
def test_warm_terminal_route_refuses_owned_history_even_with_no_scope_boundary(
    source, monkeypatch, auth_cfg, takeover
):
    from agent_sessions import owner, prefs, project_dirs
    from agent_sessions.routes import terminal as route

    reserve(source, NATIVE)
    monkeypatch.setattr(owner, "takeover_enabled", lambda: takeover)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda: [])

    async def attach(_engine, _native):
        return route.sessions.ATTACH, None

    async def forbidden(*args, **kwargs):
        pytest.fail("warm attach handed an API-owned history to the console bridge")

    monkeypatch.setattr(route, "_open_action_offloop", attach)
    monkeypatch.setattr(webterm, "run", forbidden)
    client = test_webterm._client(auth_cfg)
    headers = test_webterm._login_headers(client, auth_cfg)
    assert (
        test_webterm._close_code(client, f"/ws/term/{source.engine_id}:{NATIVE}", headers) == 4502
    )


@pytest.mark.parametrize("takeover", [False, True])
def test_warm_terminal_rechecks_ownership_after_lookup_before_dtach_client_spawn(
    source, monkeypatch, auth_cfg, takeover
):
    from agent_sessions import owner, prefs, project_dirs
    from agent_sessions.routes import terminal as route

    monkeypatch.setattr(owner, "takeover_enabled", lambda: takeover)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda: [])
    monkeypatch.setattr(route.ptybridge, "attach_argv", lambda **_kwargs: ["dtach", "-a"])

    async def attach(_engine, _native):
        return route.sessions.ATTACH, None

    def lookup(*args, **kwargs):
        # Simulate an ownership change after the early route guard. The actual process
        # handoff must recheck rather than relying on that earlier snapshot.
        reserve(source, NATIVE)
        return None

    async def forbidden(*args, **kwargs):
        pytest.fail("ownership changed during attach setup but dtach still spawned")

    monkeypatch.setattr(route, "_open_action_offloop", attach)
    monkeypatch.setattr(engines, "resolve_session", lookup)
    monkeypatch.setattr(webterm.asyncio, "create_subprocess_exec", forbidden)
    client = test_webterm._client(auth_cfg)
    headers = test_webterm._login_headers(client, auth_cfg)
    with client.websocket_connect(f"/ws/term/{source.engine_id}:{NATIVE}", headers=headers) as ws:
        for _ in range(20):
            message = ws.receive()
            if message["type"] == "websocket.close":
                assert message["code"] == 4502
                assert "API session" in message["reason"]
                break
        else:
            pytest.fail("ownership refusal did not close the terminal")


def test_native_reservation_waits_until_console_process_handoff(source):
    script = """
import sys, uuid
from agent_sessions import engines, native_ownership
from agent_sessions.plugins import admission, storage
print('ready', flush=True)
sys.stdin.readline()
print('attempting', flush=True)
with storage.locked(admission.LOCK):
    native_ownership.reserve(
        'fixture-api:' + str(uuid.uuid4()),
        native_ownership.source_identity(engines.get(sys.argv[1])),
        operation_id=str(uuid.uuid4()), owner_token=str(uuid.uuid4()), request={})
print('reserved', flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, source.engine_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with admission.acquire(source, f"new-{OTHER}") as guard:
            assert not guard.reason
            child.stdin.write("start\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "attempting"
            assert not select.select([child.stdout], [], [], 0.15)[0]
        assert child.stdout.readline().strip() == "reserved"
        assert child.wait(timeout=10) == 0
        with admission.acquire(source, f"new-{OTHER}") as guard:
            assert "unresolved API creation" in guard.reason
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_new_terminal_refuses_pending_native_creation_with_a_deliberate_close(
    source, monkeypatch, auth_cfg
):
    from agent_sessions import prefs, project_dirs
    from agent_sessions.routes import terminal as route

    reserve(source)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda: [])
    monkeypatch.setattr(route.fsbrowse, "is_browsable_dir", lambda _cwd: True)
    released = []

    async def launch(_engine, _native):
        return route.sessions.LAUNCH, SimpleNamespace(transfer=lambda: released.append(True))

    async def forbidden(*args, **kwargs):
        pytest.fail("unresolved API creation reached the terminal bridge")

    monkeypatch.setattr(route, "_open_action_offloop", launch)
    monkeypatch.setattr(webterm, "run", forbidden)
    client = test_webterm._client(auth_cfg)
    headers = test_webterm._login_headers(client, auth_cfg)
    assert (
        test_webterm._close_code(
            client, f"/ws/term/{source.engine_id}:new-{OTHER}?new=1&cwd=/work", headers
        )
        == 4502
    )
    assert released == [True]


@pytest.mark.anyio
async def test_interactive_alias_rechecks_after_reconciliation(source, monkeypatch):
    def reconciled(*args):
        reserve(source, NATIVE)  # the result was already read when ownership became permanent
        return NATIVE

    monkeypatch.setattr(source, "reconcile_new_session", reconciled)
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    monkeypatch.setattr(main, "_RECONCILE_MAX_POLLS", 1)
    result = await main._reconcile_new_session(None, source, f"new-{OTHER}", "/work", set())
    assert result is None
    assert metadata.load_aliases() == {}


def test_mission_alias_repair_refuses_api_history_and_cross_client_aliases(source):
    placeholder = f"{source.engine_id}:new-{OTHER}"
    assert mission_dispatch.publish_binding(placeholder, f"{source.engine_id}:{OTHER}")
    reserve(source, NATIVE)
    with pytest.raises(admission.Refused, match="API session"):
        mission_dispatch.publish_binding(
            f"{source.engine_id}:new-{NATIVE}", f"{source.engine_id}:{NATIVE}"
        )
    with pytest.raises(admission.Refused, match="cross client"):
        mission_dispatch.publish_binding(f"shell:{OTHER}", f"{source.engine_id}:{OTHER}")
    assert metadata.load_aliases() == {placeholder: f"{source.engine_id}:{OTHER}"}


def test_retiring_console_can_repair_its_existing_alias(source, monkeypatch):
    monkeypatch.setattr(source, "retiring", True)
    monkeypatch.delitem(engines.registry._BY_ID, source.engine_id)
    monkeypatch.setitem(engines.registry._RETIRING, source.engine_id, source)
    placeholder, logical = f"{source.engine_id}:new-{OTHER}", f"{source.engine_id}:{OTHER}"
    assert mission_dispatch.publish_binding(placeholder, logical)
    assert metadata.load_aliases() == {placeholder: logical}


def candidate(recipe, purpose="verify"):
    generation = test_plugin_manager.installed(recipe)
    item = manager.begin_action(
        str(uuid.uuid4()), "fixture", generation, purpose, confirm_effects=True
    )
    if purpose == "signin":
        item = manager.claim_signin(item["id"])
    else:
        manager._set_operation(item["id"], "running")
    prov = manager.provider("fixture", manager.generation("fixture", generation))
    return prov, item


@pytest.mark.anyio
async def test_candidate_exception_requires_running_exact_generation(recipe):
    prov, item = candidate(recipe)
    with storage.locked("worker", wait=0):
        guard = await admission.acquire_candidate_async(prov, "version", item["id"])
        guard.release()
        # Candidate is inactive: the normal path never grants this exception.
        with admission.acquire(prov) as guard:
            assert guard.reason
        prov._record = replace(prov._record, manifest_sha256="0" * 64)
        with pytest.raises(admission.Refused, match="changed"):
            await admission.acquire_candidate_async(prov, "version", item["id"])
        manager._set_operation(item["id"], "complete")
        with pytest.raises(admission.Refused, match="no longer running"):
            await admission.acquire_candidate_async(prov, "version", item["id"])


@pytest.mark.anyio
async def test_candidate_spawn_refuses_before_service_process_creation(
    recipe, monkeypatch, tmp_path
):
    prov, item = candidate(recipe)
    with storage.locked("worker", wait=0):
        called = []

        def ownership(prov, native):
            called.append(native)
            raise native_ownership.OwnershipError("owned", "synthetic API ownership")

        async def forbidden(*args, **kwargs):
            pytest.fail("candidate ownership refusal reached process creation")

        monkeypatch.setattr(native_ownership, "check_console", ownership)
        monkeypatch.setattr(process.asyncio, "create_subprocess_exec", forbidden)
        with pytest.raises(admission.Refused, match="synthetic API ownership"):
            async with process.spawn(
                prov, "resume", cwd=tmp_path, native_id=NATIVE, operation_id=item["id"]
            ):
                pytest.fail("candidate ownership refusal yielded a process")
        assert called == [NATIVE]
