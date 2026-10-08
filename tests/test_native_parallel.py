"""#1361: independent API histories coexist with each other and unresolved consoles."""

from __future__ import annotations

import asyncio

import pytest

import test_native_runtime as rt
from agent_sessions import (
    native_discovery,
    native_ipc,
    native_ownership,
    native_protocol,
    native_runtime,
    native_worker,
    sessionlock,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import storage
from agent_sessions.scanner import Session
from test_native_runtime import ENGINES, frames, ident, settle

host, project = rt.host, rt.project


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source,bypass",
    [("codex", False), ("codex", True), ("opencode", False), ("claude", False), ("claude", True)],
)
async def test_parallel_creates_and_turns_with_unresolved_console(
    host,
    project,
    monkeypatch,
    source,
    bypass,  # noqa: F811
):
    both_launched = asyncio.Event()
    launched = 0
    wait_ready = native_runtime._wait_ready

    async def together(worker_id):
        nonlocal launched
        launched += 1
        if launched == 2:
            both_launched.set()
        # Neither create can return before the other's worker has launched: real overlap,
        # not two sequential starts that happen to keep their idle workers around.
        await asyncio.wait_for(both_launched.wait(), 30)
        return await wait_ready(worker_id)

    async def create():
        snap = await runtime.create_session(
            ENGINES[source], str(project), operation_id=ident(), bypass=bypass
        )
        return await runtime.start_session(snap["session_key"]) if bypass else snap

    monkeypatch.setattr(native_runtime, "_wait_ready", together)
    # Claude pins its console UUID at launch; it never uses a late-id placeholder.
    physical = f"{source}:{ident() if source == 'claude' else 'new-' + ident()}"
    placeholder = sessionlock.acquire(physical)
    assert placeholder is not None
    with placeholder:
        outcomes = await asyncio.gather(create(), create(), return_exceptions=True)
        assert all(isinstance(result, dict) for result in outcomes), [str(r) for r in outcomes]
        assert len({r["native"]["native_id"] for r in outcomes}) == 2
        assert len({r["native"]["worker"] for r in outcomes}) == 2
        assert all(not r.get("pending_start") for r in outcomes)
        assert len(host.launches) == 2
        keys = [r["session_key"] for r in outcomes]
        # Source history can already be in a cached session walk when creation binds it.
        # The two API rows survive; their native histories must not appear again as consoles.
        source_rows = [
            Session(source, r["native"]["native_id"], str(project), 1, "New session", False)
            for r in outcomes
        ]
        unrelated = Session(source, ident(), str(project), 1, "existing console", False)
        api_rows = [
            Session(ENGINES[source], key.split(":", 1)[1], str(project), 1, "", False)
            for key in keys
        ]
        visible = native_discovery.filter_cached([*source_rows, unrelated, *api_rows])
        assert {f"{r.engine}:{r.uuid}" for r in visible} == {
            *keys,
            f"{source}:{unrelated.uuid}",
        }
        turns = [ident(), ident()]
        await asyncio.gather(
            *(
                runtime.submit_turn(k, operation_id=t, text="HANG")
                for k, t in zip(keys, turns, strict=True)
            )
        )
        await asyncio.gather(
            *(settle(k, t, state=("running",)) for k, t in zip(keys, turns, strict=True))
        )
        # Each worker keeps only its own writer locks, not a source-wide launch/ledger fence.
        with storage.locked("launch", wait=0), storage.locked("native-ownership", wait=0):
            assert sessionlock.acquire(physical) is None
        assert (await runtime.stop(keys[0]))["containment"] == "gone"
        assert (await runtime.probe(keys[1]))["containment"] == "live"
        await settle(keys[1], turns[1], state=("running",))
        await runtime.interrupt(keys[1], operation_id=ident(), turn_id=turns[1])
        await settle(keys[1], turns[1], state=("interrupted",))
        # A successor resumes its own history while the other worker and console stay live.
        resumed_turn = ident()
        await runtime.submit_turn(keys[0], operation_id=resumed_turn, text="again")
        snap, settled = await settle(keys[0], resumed_turn)
        assert settled["reply"] == "echo:again"
        assert snap["native"]["native_id"] == outcomes[0]["native"]["native_id"]
        assert len(host.launches) == 3
        await asyncio.gather(*(runtime.stop(k) for k in keys))


@pytest.mark.anyio
async def test_all_three_api_clients_run_together(host, project):  # noqa: F811
    created = await asyncio.gather(
        *(
            runtime.create_session(ENGINES[source], str(project), operation_id=ident())
            for source in ("codex", "opencode", "claude")
        )
    )
    keys = [snap["session_key"] for snap in created]
    turns = [ident() for _ in keys]
    await asyncio.gather(
        *(
            runtime.submit_turn(key, operation_id=turn, text="HANG")
            for key, turn in zip(keys, turns, strict=True)
        )
    )
    await asyncio.gather(
        *(settle(key, turn, state=("running",)) for key, turn in zip(keys, turns, strict=True))
    )
    assert len({snap["native"]["worker"] for snap in created}) == 3
    assert len(host.launches) == 3
    for key, turn in zip(keys, turns, strict=True):
        assert (await runtime.probe(key))["containment"] == "live"
        await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
        await settle(key, turn, state=("interrupted",))
    await asyncio.gather(*(runtime.stop(key) for key in keys))


@pytest.mark.anyio
async def test_failed_skip_start_can_retry_alongside_a_console(host, project, monkeypatch):  # noqa: F811
    snap = await runtime.create_session(
        ENGINES["codex"], str(project), operation_id=ident(), bypass=True
    )
    key = snap["session_key"]
    wait_ready = native_runtime._wait_ready

    async def fail_after_ready(worker_id):
        await wait_ready(worker_id)
        raise native_runtime.NativeError(503, "injected readiness failure")

    physical = f"codex:new-{ident()}"
    with sessionlock.acquire(physical):
        monkeypatch.setattr(native_runtime, "_wait_ready", fail_after_ready)
        with pytest.raises(runtime.StructuredError, match="injected readiness failure"):
            await runtime.start_session(key)
        assert (await runtime.snapshot(key))["start_incomplete"]
        monkeypatch.setattr(native_runtime, "_wait_ready", wait_ready)
        started = await runtime.start_session(key)
        assert not started["pending_start"]
        # A successful start needs no further start, and replay cannot launch another worker.
        assert not (await runtime.start_session(key))["pending_start"]
        assert len(host.launches) == 2
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="hello")
        _, completed = await settle(key, turn)
        assert completed["reply"] == "echo:hello"
        methods = [f["frame"].get("method") for f in frames(project)]
        assert methods.count("thread/start") == 1 and methods.count("thread/resume") == 1
        await runtime.stop(key)


@pytest.mark.parametrize("source", ["codex", "opencode"])
@pytest.mark.parametrize(
    "bad", ["load", "request", "guessed", "different-id", "existing", "resume", "claude", "error"]
)
def test_fresh_binding_requires_this_connections_create_response(
    tmp_path, monkeypatch, source, bad
):
    adapter = "codex-app-server" if source == "codex" else "opencode-acp"
    native = ident() if source == "codex" else "ses_NewNativeHistory"
    request = ident()
    config = {
        "worker_id": ident(),
        "session_key": f"{ENGINES[source]}:{ident()}",
        "adapter": adapter,
        "journal_root": str(tmp_path / "journal"),
        "capability": native_ipc.Capability.create()._value,
        "connection_id": ident(),
        "mode": "create",
    }
    data = {
        "action": "thread/start" if source == "codex" else "session/new",
        "request_id": request,
        "native_id": native,
    }
    if bad == "load":
        data["action"] = "thread/resume" if source == "codex" else "session/load"
    elif bad == "request":
        data["request_id"] = ident()
    elif bad == "existing":
        config["native_id"] = native
    elif bad == "resume":
        config["mode"] = "resume"
    elif bad == "claude":
        config.update(adapter="claude-stream-json", native_id=ident())
    worker = native_worker.Worker(config, tmp_path)
    worker.codec.native_id = None if bad == "guessed" else native
    if bad == "different-id":
        data["native_id"] = ident()
    event = native_protocol.NativeEvent("error" if bad == "error" else "session", data)

    def forbidden(*args, **kwargs):
        pytest.fail("an uncorrelated or non-create response reached ownership binding")

    monkeypatch.setattr(native_ownership, "bind", forbidden)
    with pytest.raises(native_worker.WorkerError, match="fresh create response"):
        worker.bind_created(request, event)
