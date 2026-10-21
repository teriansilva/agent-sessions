"""A captured edit approval cannot survive actual managed generation replacement."""

import asyncio
import uuid

import pytest

import test_chat_edits as edits
import test_chat_tools as reads
import test_plugins
from agent_sessions import chat_runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import manager
from test_plugin_manager import installed

project = edits.project
endpoint = edits.endpoint
anyio_backend = edits.anyio_backend
lease_signal = edits.lease_signal


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["disable", "replace"])
async def test_managed_change_refuses_the_captured_approval(
    endpoint, project, monkeypatch, lease_signal, change
):
    engine = "fixture-api"
    monkeypatch.setattr(reads, "ENGINE", engine)
    manifest = test_plugins.doc("apichat")
    manifest["identity"]["id"] = engine
    entry = {"manifest": manifest, "recipe": {"artifacts": []}}

    def candidate():
        generation = installed(entry)
        manager.set_endpoint(
            engine,
            generation,
            {"base_url": reads.URL, "api_key": reads.KEY, "model": "m", "tools": "write"},
        )
        manager.record_verification(
            engine,
            generation,
            [
                {
                    "check": "endpoint",
                    "passed": True,
                    "binding": manager._endpoint_binding(engine, generation),
                }
            ],
        )
        return generation

    first, second = candidate(), candidate()
    manager.activate(str(uuid.uuid4()), engine, first)
    # Endpoint setup above is already complete; the active scope is deliberately immutable.
    monkeypatch.setattr(reads, "configure", lambda _tools: None)
    sid, tid, view = await edits.propose(endpoint, project)
    captured = asyncio.Event()
    provider = chat_runtime._chat_provider

    def capture(engine_id):
        prov = provider(engine_id)
        captured.set()
        return prov

    monkeypatch.setattr(chat_runtime, "_chat_provider", capture)
    lock = chat_runtime._lock(chat_runtime._key(engine, sid))
    await lock.acquire()
    with registry.snapshot_scope():
        old = registry.get(engine)
        task = asyncio.create_task(edits.decide(sid, tid, view))
        try:
            await asyncio.wait_for(captured.wait(), 30)
            if change == "replace":
                await asyncio.to_thread(manager.activate, str(uuid.uuid4()), engine, second)
            else:
                await asyncio.to_thread(manager.deactivate, str(uuid.uuid4()), engine)
            assert not registry.admits(old)
        finally:
            lock.release()
        with pytest.raises(chat_runtime.ChatError) as exc:
            await task
        assert exc.value.status == 409
    assert (project / "README.md").read_text() == "hello\n"
    assert len(endpoint.requests) == 2  # read + proposal; no approval continuation was sent.
    row = manager.snapshot()["plugins"][engine]
    assert row["active"] == (second if change == "replace" else first)
    assert row["enabled"] is (change == "replace")
