"""Queued operator input runs serially in the real contained worker (#1378)."""

import asyncio

import pytest

import test_native_images as images
import test_native_runtime as rt
from agent_sessions import native_journal, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import manager, storage

host = rt.host
project = rt.project
uploads = images.uploads
PNG = images.PNG
ENGINES, frames, ident, pending, settle = rt.ENGINES, rt.frames, rt.ident, rt.pending, rt.settle


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_followups_queue_and_finish_in_order_without_interrupt(host, project, source):
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    queued = [ident(), ident()]
    for index, tid in enumerate(queued):
        sent = await runtime.submit_turn(key, operation_id=tid, text=f"follow-up {index}")
        assert sent["state"] == "queued"
        replay = await runtime.submit_turn(key, operation_id=tid, text=f"follow-up {index}")
        assert replay["state"] == "queued"
    snap = await runtime.snapshot(key)
    assert snap["active_turn"] == first
    assert [t["state"] for t in snap["turns"]] == ["awaiting_approval", "queued", "queued"]
    with pytest.raises(runtime.StructuredError, match="different request"):
        await runtime.submit_turn(key, operation_id=queued[0], text="changed")
    await runtime.decide(
        key,
        decision_id=ident(),
        user="alice",
        turn_id=first,
        request_id=request["request_id"],
        decision="approve",
    )
    snap, _ = await settle(key, queued[-1])
    assert [t["state"] for t in snap["turns"]] == ["completed"] * 3
    assert [t["reply"] for t in snap["turns"][1:]] == ["echo:follow-up 0", "echo:follow-up 1"]
    writes = [f["frame"] for f in frames(project)]
    assert not any(f.get("method") in {"turn/interrupt", "session/cancel"} for f in writes)
    users = [
        f
        for f in writes
        if f.get("method") in {"turn/start", "session/prompt"} or f.get("type") == "user"
    ]
    assert len(users) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_stop_never_sends_waiting_messages(host, project, source):
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    await runtime.submit_turn(key, operation_id=ident(), text="HANG")
    waiting = ident()
    assert (await runtime.submit_turn(key, operation_id=waiting, text="later"))["state"] == "queued"
    assert (await runtime.stop(key))["containment"] == "gone"
    _, stopped = await settle(key, waiting)
    assert stopped["state"] == "failed" and "not sent" in stopped["reason"]
    await asyncio.sleep(0.1)
    assert len(host.launches) == 1
    writes = [f["frame"] for f in frames(project)]
    assert (
        len(
            [
                f
                for f in writes
                if f.get("method") in {"turn/start", "session/prompt"} or f.get("type") == "user"
            ]
        )
        == 1
    )


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_crash_preserves_unsent_history_but_successor_never_replays_it(host, project, source):
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    await runtime.submit_turn(key, operation_id=ident(), text="HANG")
    queued = ident()
    await runtime.submit_turn(key, operation_id=queued, text="must not replay")
    host.kill((await runtime.snapshot(key))["native"]["worker"])
    _, failed = await settle(key, queued)
    assert "not sent" in failed["reason"]
    again = await runtime.submit_turn(key, operation_id=queued, text="must not replay")
    assert again["state"] == "failed" and len(host.launches) == 1
    fresh = ident()
    await runtime.submit_turn(key, operation_id=fresh, text="new deliberate message")
    _, settled = await settle(key, fresh)
    assert settled["reply"] == "echo:new deliberate message"
    assert len(host.launches) == 2
    users = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method") in {"turn/start", "session/prompt"}
        or f["frame"].get("type") == "user"
    ]
    assert len(users) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_queue_rechecks_images_and_continues_after_a_changed_image(
    host, project, source, uploads
):
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="APPROVE")
    req = (await pending(key))["pending_requests"][0]
    name = uploads("20261008-010000-shot.png", PNG)
    changed, next_turn = ident(), ident()
    await runtime.submit_turn(key, operation_id=changed, text="picture", attachments=[name])
    await runtime.submit_turn(key, operation_id=next_turn, text="after picture")
    (uploads.dir / name).write_bytes(PNG + b"changed")
    await runtime.decide(
        key,
        decision_id=ident(),
        user="alice",
        turn_id=first,
        request_id=req["request_id"],
        decision="approve",
    )
    snap, _ = await settle(key, next_turn)
    assert [t["state"] for t in snap["turns"]] == ["completed", "failed", "completed"]
    assert "not sent" in snap["turns"][1]["reason"]


@pytest.mark.anyio
async def test_a_full_queue_refuses_and_another_session_still_runs(host, project):
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    await runtime.submit_turn(key, operation_id=ident(), text="HANG")
    ids = [ident() for _ in range(native_journal.MAX_QUEUED)]
    for tid in ids:
        await runtime.submit_turn(key, operation_id=tid, text="wait")
    rejected = ident()
    with pytest.raises(runtime.StructuredError, match="queue is full"):
        await runtime.submit_turn(key, operation_id=rejected, text="over limit")
    assert (await runtime.submit_turn(key, operation_id=ids[0], text="wait"))["state"] == "queued"
    assert rejected not in {t["turn_id"] for t in (await runtime.snapshot(key))["turns"]}
    other = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    tid = ident()
    await runtime.submit_turn(other, operation_id=tid, text="independent")
    _, settled = await settle(other, tid)
    assert settled["reply"] == "echo:independent"


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
@pytest.mark.parametrize("removed", [False, True])
async def test_running_turn_drains_only_while_provider_is_still_admitted(
    host, project, source, removed
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    first, followup = ident(), ident()
    journal_root = native_runtime._journal_root(registry.get(engine))
    await runtime.submit_turn(key, operation_id=first, text="WAIT_RELEASE")
    assert (await runtime.submit_turn(key, operation_id=followup, text="follow-up"))[
        "state"
    ] == "queued"
    if removed:
        manager.deactivate(ident(), engine)
    (project / "release-queued-turn").touch()
    if removed:
        # The test's in-memory fixture provider is removed too; inspect its durable journal
        # directly to verify the independent worker's real catalog recheck.
        for _ in range(200):
            journal = native_journal.read(journal_root, key.partition(":")[2])
            if journal.operations[followup].handoff == "not_sent":
                break
            await asyncio.sleep(0.05)
        assert journal.operations[followup].handoff == "not_sent"
        assert any(
            item["event"]["kind"] == "turn_completed"
            and item["event"]["data"].get("operation_id") == first
            for item in journal.events
        )
    else:
        snap, finished = await settle(key, followup)
        assert snap["turns"][0]["state"] == "completed"
        assert finished["state"] == "completed"
        assert finished["reply"] == "echo:follow-up"
    users = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method") in {"turn/start", "session/prompt"}
        or f["frame"].get("type") == "user"
    ]
    assert len(users) == (1 if removed else 2)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
@pytest.mark.parametrize("lock_kind", ["launch", "lifecycle"])
async def test_queued_messages_wait_for_competing_lock_then_run_once_in_order(
    host, project, source, lock_kind
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    first, second, third = ident(), ident(), ident()
    await runtime.submit_turn(key, operation_id=first, text="WAIT_RELEASE")
    for tid, text in [(second, "second"), (third, "third")]:
        assert (await runtime.submit_turn(key, operation_id=tid, text=text))["state"] == "queued"
    root = native_runtime._journal_root(registry.get(engine))
    session_id = key.partition(":")[2]
    # This process owns the same lock another session launch/plugin operation would hold;
    # the separately contained worker must retain both messages through that contention.
    lock = (
        storage.locked("launch", wait=0)
        if lock_kind == "launch"
        else native_state.session_lock(session_id, wait=0)
    )
    with lock:
        (project / "release-queued-turn").touch()
        for _ in range(200):
            journal = native_journal.read(root, session_id)
            if any(
                e["event"]["kind"] == "turn_completed"
                and e["event"]["data"].get("operation_id") == first
                for e in journal.events
            ):
                break
            await asyncio.sleep(0.025)
        else:
            pytest.fail("the preceding turn did not complete while admission was locked")
        for _ in range(20):
            await asyncio.sleep(0.025)
            journal = native_journal.read(root, session_id)
            assert [journal.operations[tid].handoff for tid in (second, third)] == [
                "queued",
                "queued",
            ]
    snap, _ = await settle(key, third)
    assert [t["state"] for t in snap["turns"]] == ["completed"] * 3
    assert [t["reply"] for t in snap["turns"][1:]] == ["echo:second", "echo:third"]
    users = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method") in {"turn/start", "session/prompt"}
        or f["frame"].get("type") == "user"
    ]
    assert len(users) == 3
