"""Existing-session bypass: real worker/IPC, frozen create identity, proved containment."""

# ruff: noqa: F811 — imported isolated pytest fixtures

import asyncio
import json
import threading

import pytest

import test_native_send_now as guidance
from agent_sessions import native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_native_clients import app_client  # noqa: F401 — authenticated isolated client
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    KINDS,
    frames,
    host,
    ident,
    pending,
    project,
    settle,
)

SOURCES = [source for source, kind in KINDS.items() if kind in kinds.API_BYPASS_KINDS]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_enable_retains_history_create_identity_and_resumes_with_bypass(
    host, project, source
):  # noqa: F811
    sid = ident()
    before = await runtime.create_session(ENGINES[source], str(project), operation_id=sid)
    key = before["session_key"]
    assert before["can_enable_bypass"] and not before["bypass"]
    native = before["native"]["native_id"]
    original = native_state.read_session(sid)["request"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="hello")
    await settle(key, first)

    op = ident()
    enabled = await runtime.enable_bypass(key, operation_id=op, user="operator")
    assert enabled["bypass"] and not enabled["can_enable_bypass"]
    assert enabled["permission_change"] == {"operation_id": op, "state": "enabled"}
    assert enabled["native"]["worker"] is None and len(host.launches) == 1
    assert enabled["native"]["native_id"] == native
    assert enabled["turns"][0]["reply"] == "echo:hello"
    assert native_state.read_session(sid)["request"] == original
    replay = await runtime.create_session(ENGINES[source], str(project), operation_id=sid)
    assert replay["bypass"] and len(host.launches) == 1

    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="again")
    snap, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:again" and snap["native"]["native_id"] == native
    assert len(host.launches) == 2
    config = native_state.read_config(native_state.read_session(sid)["current_worker"])
    assert config["bypass"] is True
    if source == "codex":
        resume = [
            f["frame"]["params"]
            for f in frames(project)
            if f["frame"].get("method") == "thread/resume"
        ]
        assert resume[-1]["approvalPolicy"] == "never"
        assert resume[-1]["sandbox"] == "danger-full-access"
    elif source == "claude":
        assert any("bypassPermissions" in f["argv"] for f in frames(project))
    elif source == "opencode":
        configs = [
            json.loads(f["env"]["config"])
            for f in frames(project)
            if f.get("env", {}).get("config")
        ]
        assert configs
        rules = configs[-1]["agent"][configs[-1]["default_agent"]]["permission"]
        assert set(rules.values()) == {"allow"}
    # A retry of the old operation never stops the newer worker.
    again = await runtime.enable_bypass(key, operation_id=op, user="operator")
    assert again["native"]["worker"] == snap["native"]["worker"]
    for operation_id, user in [(ident(), "operator"), (op, "someone-else")]:
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.enable_bypass(key, operation_id=operation_id, user=user)
        assert exc.value.status == 409


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_enable_closes_pending_callback_and_does_not_replay_old_turn(host, project, source):  # noqa: F811
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    turn_id = ident()
    await runtime.submit_turn(key, operation_id=turn_id, text="APPROVE")
    waiting = await pending(key)
    request = waiting["pending_requests"][0]
    changed = await runtime.enable_bypass(key, operation_id=ident(), user="operator")
    assert not changed["pending_requests"] and changed["bypass"]
    assert changed["active_turn"] is None
    assert changed["turns"][0]["state"] not in {"running", "queued", "awaiting_approval"}
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.decide(
            key,
            decision_id=ident(),
            turn_id=turn_id,
            request_id=request["request_id"],
            decision="approve",
            user="operator",
        )
    assert exc.value.status == 409
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_uncertain_containment_blocks_relaunch_and_same_operation_recovers(
    host, project, monkeypatch
):  # noqa: F811
    sid = ident()
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=sid))[
        "session_key"
    ]
    op = ident()
    with monkeypatch.context() as failure:
        failure.setattr(native_runtime, "STOP_TIMEOUT", 0)
        failure.setattr(host, "stop", lambda argv: False)
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.enable_bypass(key, operation_id=op, user="operator")
        assert exc.value.status == 503
        snap = await runtime.snapshot(key)
        assert not snap["bypass"] and snap["permission_change"]["state"] == "stopping"
        with pytest.raises(runtime.StructuredError):
            await runtime.submit_turn(key, operation_id=ident(), text="must not run")
        assert len(host.launches) == 1
    # Simulate a new HTTP process: all recovery state is on disk, no task-local callback.
    enabled = await runtime.enable_bypass(key, operation_id=op, user="operator")
    assert enabled["bypass"]
    next_turn = ident()
    await runtime.submit_turn(key, operation_id=next_turn, text="recovered")
    await settle(key, next_turn)
    assert len(host.launches) == 2


@pytest.mark.anyio
async def test_two_identical_enable_calls_share_one_change_and_stop_is_still_available(
    host, project
):  # noqa: F811
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    op = ident()
    result = await asyncio.gather(
        *(runtime.enable_bypass(key, operation_id=op, user="operator") for _ in range(2))
    )
    assert all(s["bypass"] for s in result) and len(host.launches) == 1
    assert (await runtime.stop(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_unstarted_skip_creation_and_invalid_operator_never_launch(host, project):  # noqa: F811
    key = (
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=ident(), bypass=True
        )
    )["session_key"]
    for user in ["operator", ""]:
        with pytest.raises(runtime.StructuredError):
            await runtime.enable_bypass(key, operation_id=ident(), user=user)
    assert not host.launches


def test_route_binds_authenticated_operator_and_requires_csrf(app_client, auth_cfg, monkeypatch):
    calls = []

    async def enable(key, **kwargs):
        calls.append((key, kwargs))
        return {"bypass": True}

    monkeypatch.setattr(runtime, "enable_bypass", enable)
    key, op = f"codex-api:{ident()}", ident()
    path = f"/api/structured/sessions/{key}/skip-permissions"
    assert app_client.post(path, json={"operation_id": op, "user": "someone"}).status_code == 422
    assert (
        app_client.post(
            path, json={"operation_id": op}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert not calls
    assert app_client.post(path, json={"operation_id": op}).json() == {"bypass": True}
    assert calls == [(key, {"operation_id": op, "user": auth_cfg.username})]


@pytest.mark.anyio
async def test_malformed_permission_record_cannot_enable_or_relaunch(host, project):
    sid = ident()
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=sid))[
        "session_key"
    ]
    await runtime.stop(key)
    record = native_state.read_session(sid)
    record["permission_change"] = {"state": "enabled"}
    native_state.write_session(sid, record)
    with pytest.raises(runtime.StructuredError):
        await runtime.enable_bypass(key, operation_id=ident(), user="operator")
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="must not run")
    assert len(host.launches) == 1
    assert (await runtime.stop(key))["containment"] == "gone"


@pytest.mark.anyio
@pytest.mark.parametrize("closer", ["stop", "archive"])
async def test_quiescence_fences_concurrent_submit_and_containment(
    host, project, monkeypatch, closer
):
    from agent_sessions import runtime_cleanup

    sid = ident()
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=sid))[
        "session_key"
    ]
    entered, release = threading.Event(), threading.Event()
    actual_stop = host.stop

    def hold_stop(argv):
        entered.set()
        assert release.wait(10)
        return actual_stop(argv)

    monkeypatch.setattr(host, "stop", hold_stop)
    changing = asyncio.create_task(
        runtime.enable_bypass(key, operation_id=ident(), user="operator")
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.submit_turn(key, operation_id=ident(), text="must not be queued")
        assert exc.value.status == 409
        closing = asyncio.create_task(
            runtime.stop(key)
            if closer == "stop"
            else runtime_cleanup.contain_native(ENGINES["codex"], sid)
        )
    finally:
        release.set()
    await asyncio.gather(changing, closing)
    snap = await runtime.snapshot(key)
    assert snap["bypass"] and snap["native"]["worker"] is None
    assert not snap["turns"] and len(host.launches) == 1


@pytest.mark.anyio
async def test_a_late_ready_observation_does_not_reopen_a_quiesced_worker(
    host, project, monkeypatch
):
    actual_wait = native_runtime._wait_ready
    sid = ident()

    async def enable_before_ready_returns(worker_id):
        life = await actual_wait(worker_id)
        await runtime.enable_bypass(
            f"{ENGINES['codex']}:{sid}", operation_id=ident(), user="operator"
        )
        return life

    monkeypatch.setattr(native_runtime, "_wait_ready", enable_before_ready_returns)
    snap = await runtime.create_session(ENGINES["codex"], str(project), operation_id=sid)
    assert snap["bypass"] and snap["native"]["worker"] is None
    assert native_state.read_session(sid)["closed"]
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_an_in_flight_send_is_not_retried_across_a_permission_change(
    host, project, monkeypatch
):
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    actual_generation = native_runtime._live_generation
    armed = True

    async def interleave(engine, sid, permission_operation):
        nonlocal armed
        if armed:
            armed = False
            await runtime.enable_bypass(key, operation_id=ident(), user="operator")
        return await actual_generation(engine, sid, permission_operation)

    monkeypatch.setattr(native_runtime, "_live_generation", interleave)
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.submit_turn(key, operation_id=ident(), text="must not cross the change")
    assert exc.value.status == 409 and "permissions changed" in exc.value.detail
    assert not (await runtime.snapshot(key))["turns"]
    assert len(host.launches) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("revision_retry", [False, True])
async def test_send_keeps_its_permission_mode_across_journal_reads_and_revision_retries(
    host, project, monkeypatch, revision_retry
):
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    actual_effect = native_runtime._effect
    armed = True

    async def interleave(*args, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            await runtime.enable_bypass(key, operation_id=ident(), user="operator")
            if revision_retry:
                raise native_runtime.NativeError(409, "revision changed")
        return await actual_effect(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "_effect", interleave)
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.submit_turn(key, operation_id=ident(), text="must not acquire the new mode")
    assert exc.value.status == 409 and "permissions changed" in exc.value.detail
    assert not (await runtime.snapshot(key))["turns"]
    assert len(host.launches) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_permission_containment_prevents_send_now_from_dispatching_the_old_queue(
    host, project, monkeypatch, source
):
    key, active, _, urgent = await guidance.queued_session(source, project)
    operation = ident()
    with monkeypatch.context() as failure:
        failure.setattr(native_runtime, "STOP_TIMEOUT", 0)
        failure.setattr(host, "stop", lambda argv: False)
        with pytest.raises(runtime.StructuredError):
            await runtime.enable_bypass(key, operation_id=operation, user="operator")
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
        assert exc.value.status == 409
    await runtime.enable_bypass(key, operation_id=operation, user="operator")
    assert len(guidance.input_frames(project)) == 1 and len(host.launches) == 1
    next_turn = ident()
    await runtime.submit_turn(key, operation_id=next_turn, text="new explicit message")
    await settle(key, next_turn)
    assert len(guidance.input_frames(project)) == 2 and len(host.launches) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_a_send_now_receipt_can_be_recovered_after_enabling_bypass(host, project, source):
    key, active, _, urgent = await guidance.queued_session(source, project)
    operation = ident()
    receipt = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert receipt["handoff"] == "sent"
    await runtime.enable_bypass(key, operation_id=ident(), user="operator")
    count = len(guidance.input_frames(project))
    replay = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert replay == receipt
    assert len(guidance.input_frames(project)) == count and len(host.launches) == 1
