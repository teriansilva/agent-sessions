"""The shared API facade executes real chat turns and refuses terminal fallback (#1275)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest

import test_chat_agent as chat_api
from agent_sessions import chat_runtime, chat_store, engines
from agent_sessions import structured_runtime as runtime
from agent_sessions.structured_types import normalize_context

endpoint = chat_api.endpoint
anyio_backend = chat_api.anyio_backend


def test_capabilities_are_implementation_support_not_console_launch_flags(endpoint):
    descriptor = runtime.describe(chat_api.ENGINE)
    assert descriptor.operations == ("create", "snapshot", "submit", "decide")
    assert not descriptor.ready and not descriptor.mission_ready
    chat_api.configure()
    assert runtime.describe(chat_api.ENGINE).ready
    for prov in engines.all_providers():
        if prov.manifest.runtime == "pty":
            item = runtime.describe(prov.engine_id)
            assert not item.operations and not item.ready and not item.mission_ready
            with pytest.raises(runtime.StructuredError, match="console"):
                runtime.require(prov.engine_id, "submit")
    assert not runtime.describe("unknown-client").ready
    for unsupported in ("interrupt", "events", "probe", "stop"):
        with pytest.raises(runtime.StructuredError):
            runtime.require(chat_api.ENGINE, unsupported)


@pytest.mark.anyio
async def test_manifest_can_withhold_create_even_for_an_implemented_adapter(
    endpoint, tmp_path, monkeypatch
):
    chat_api.configure()
    prov = engines.get(chat_api.ENGINE)
    monkeypatch.setattr(prov, "manifest", replace(prov.manifest, capabilities={"resume": True}))
    assert "create" not in runtime.describe(chat_api.ENGINE).operations
    with pytest.raises(runtime.StructuredError):
        await runtime.create_session(chat_api.ENGINE, str(tmp_path), operation_id=str(uuid.uuid4()))
    assert not list(prov.store_root().glob("*.jsonl"))
    assert not endpoint.requests


@pytest.mark.anyio
async def test_create_replay_and_turn_replay_use_one_real_agent_operation(endpoint, tmp_path):
    chat_api.configure()
    create_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    created = await runtime.create_session(chat_api.ENGINE, str(tmp_path), operation_id=create_id)
    repeated = await runtime.create_session(chat_api.ENGINE, str(tmp_path), operation_id=create_id)
    assert repeated == created
    assert created["state"] == "idle"
    assert created["model_requested"] == "test-model"
    assert created["model_effective"] is None
    endpoint.gate = asyncio.Event()
    context = {
        "mission_id": "mission-example",
        "flow_revision": "rev-1",
        "step_id": "write",
        "episode": 2,
    }
    args = dict(
        operation_id=turn_id,
        text="Inspect the project",
        expected_revision=created["revision"],
        context=context,
    )
    results = await asyncio.gather(
        runtime.submit_turn(created["session_key"], **args),
        runtime.submit_turn(created["session_key"], **args),
    )
    assert {result["operation_id"] for result in results} == {turn_id}
    assert all(result["state"] == "running" for result in results)
    endpoint.gate.set()
    task = chat_runtime.running_task(chat_api.ENGINE, create_id)
    if task is not None:
        await task
    replay = await runtime.submit_turn(created["session_key"], **args)
    assert replay["state"] == "completed"
    assert replay["context"] == context
    assert len(endpoint.requests) == 1
    observed = await runtime.snapshot(created["session_key"])
    assert observed["state"] == "idle"
    assert observed["revision"] > created["revision"]
    assert observed["event_cursor"] == observed["revision"]
    assert len(observed["turns"]) == 1


@pytest.mark.anyio
async def test_reused_operation_with_changed_context_or_payload_cannot_send(endpoint, tmp_path):
    chat_api.configure()
    operation_id = str(uuid.uuid4())
    created = await runtime.create_session(
        chat_api.ENGINE, str(tmp_path), operation_id=operation_id
    )
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.create_session(chat_api.ENGINE, str(other), operation_id=operation_id)
    assert error.value.status == 409
    tid = str(uuid.uuid4())
    await runtime.submit_turn(
        created["session_key"], operation_id=tid, text="hello", context={"episode": 1}
    )
    task = chat_runtime.running_task(chat_api.ENGINE, operation_id)
    if task is not None:
        await task
    for text, context in (("changed", {"episode": 1}), ("hello", {"episode": 2})):
        with pytest.raises(runtime.StructuredError) as error:
            await runtime.submit_turn(
                created["session_key"], operation_id=tid, text=text, context=context
            )
        assert error.value.status == 409
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_stale_revision_refuses_new_turn_without_network_effect(endpoint, tmp_path):
    chat_api.configure()
    created = await runtime.create_session(
        chat_api.ENGINE, str(tmp_path), operation_id=str(uuid.uuid4())
    )
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.submit_turn(
            created["session_key"],
            operation_id=str(uuid.uuid4()),
            text="stale",
            expected_revision=created["revision"] + 1,
        )
    assert error.value.status == 409
    assert not endpoint.requests
    assert (await runtime.snapshot(created["session_key"]))["turns"] == []


@pytest.mark.anyio
async def test_lost_response_recovery_does_not_require_endpoint_credentials(endpoint, tmp_path):
    from agent_sessions import chat_config

    chat_api.configure()
    operation_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    created = await runtime.create_session(
        chat_api.ENGINE, str(tmp_path), operation_id=operation_id
    )
    await runtime.submit_turn(created["session_key"], operation_id=turn_id, text="hello")
    task = chat_runtime.running_task(chat_api.ENGINE, operation_id)
    if task is not None:
        await task
    chat_config.set_config(chat_api.ENGINE, {"api_key": None})
    assert not runtime.describe(chat_api.ENGINE).ready
    recovered = await runtime.create_session(
        chat_api.ENGINE, str(tmp_path), operation_id=operation_id
    )
    assert recovered["session_key"] == created["session_key"]
    replay = await runtime.submit_turn(created["session_key"], operation_id=turn_id, text="hello")
    assert replay["state"] == "completed"
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(
            created["session_key"], operation_id=str(uuid.uuid4()), text="new"
        )
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_snapshot_is_bounded_and_preserves_explicit_unknown_outcome(endpoint, tmp_path):
    chat_api.configure()
    sid = str(uuid.uuid4())
    created = await runtime.create_session(chat_api.ENGINE, str(tmp_path), operation_id=sid)
    root = engines.get(chat_api.ENGINE).store_root()
    records = []
    for _ in range(60):
        tid = str(uuid.uuid4())
        records.extend(
            [
                {"type": "user", "turn_id": tid, "text": "x" * 25_000, "ts": 1},
                {"type": "assistant", "turn_id": tid, "text": "y" * 25_000, "ts": 2},
                {"type": "status", "turn_id": tid, "status": "done", "ts": 3},
            ]
        )
    records.append(
        {"type": "status", "turn_id": tid, "status": "failed", "code": "uncertain", "ts": 4}
    )
    chat_store.append(root, sid, *records)
    view = await runtime.snapshot(created["session_key"])
    assert view["state"] == "uncertain"
    assert len(view["turns"]) == 50 and view["omitted_turns"] == 10
    assert sum(len(t["text"] or "") + len(t["reply"] or "") for t in view["turns"]) <= 200_000
    assert any(t["text_truncated"] for t in view["turns"])
    assert view["turns"][-1]["state"] == "uncertain"
    assert not endpoint.requests


@pytest.mark.parametrize(
    "context",
    [
        {"authority": "yes"},
        {"episode": True},
        {"episode": -1},
        {"episode": 2**53},
        {"flow_revision": 1},
        {"step_id": {"nested": "value"}},
        {"mission_id": "../file"},
        [],
    ],
)
def test_correlation_cannot_carry_authority_or_unbounded_nested_data(context):
    with pytest.raises(ValueError):
        normalize_context(context)
