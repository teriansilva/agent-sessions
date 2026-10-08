"""#1339: the skip-start state machine (operator decision on #1341, after review 5926).

One field, `record["start"]`, moved only by compare-and-set under the session lock. This pins
every state × entry point for both adapters: the resulting state and how many generations were
launched. `expired` and `discarded` are terminal; only `/start` (from pending/failed) and a
`started` session may launch a generation; a late completion for a generation that is no longer
the starting one is a no-op."""

from __future__ import annotations

import pytest

from agent_sessions import native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures

SOURCES = ["codex", "claude"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _create(source, project):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(ENGINES[source], str(project), operation_id=op, bypass=True)
    return op, snap["session_key"]


async def _fail_next_start(monkeypatch):
    real = native_runtime._wait_ready
    state = {"armed": True}

    async def once(worker_id):
        if state["armed"]:
            state["armed"] = False
            raise native_runtime.NativeError(503, "the native worker stopped: boom")
        return await real(worker_id)

    monkeypatch.setattr(native_runtime, "_wait_ready", once)


async def _in_state(state, source, project, monkeypatch):  # noqa: F811
    op, key = await _create(source, project)
    if state == "expired":
        record = native_state.read_session(op)
        record["start"]["expires_at"] = 0
        native_state.write_session(op, record)
    elif state == "failed":
        await _fail_next_start(monkeypatch)
        with pytest.raises(runtime.StructuredError):
            await runtime.start_session(key)
    elif state == "started":
        await runtime.start_session(key)
    elif state == "discarded":
        await runtime.stop(key)
    elif state == "starting-dead":  # a start whose process died before launching anything
        record = native_state.read_session(op)
        record["start"].update(state="starting", gen=None)
        native_state.write_session(op, record)
    return op, key


def _state(op):
    return native_state.read_session(op)["start"]["state"]


# state → (what /start does, resulting state)
START = {
    "pending": ("launch", "started"),
    "failed": ("launch", "started"),
    "starting-dead": ("launch", "started"),
    "started": ("observe", "started"),
    "discarded": (409, "discarded"),
    "expired": (410, "expired"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("state", sorted(START))
async def test_start(host, project, monkeypatch, source, state):  # noqa: F811
    op, key = await _in_state(state, source, project, monkeypatch)
    before = len(host.launches)
    outcome, after = START[state]
    if isinstance(outcome, int):
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.start_session(key)
        assert exc.value.status == outcome
        assert len(host.launches) == before
    else:
        await runtime.start_session(key)
        assert len(host.launches) == before + (1 if outcome == "launch" else 0)
    assert _state(op) == after


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("state", ["pending", "failed", "starting-dead", "discarded", "expired"])
async def test_a_turn_launches_nothing_unless_started(host, project, monkeypatch, source, state):  # noqa: F811
    op, key = await _in_state(state, source, project, monkeypatch)
    before, recorded = len(host.launches), native_state.read_session(op)["start"]
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    assert exc.value.status == 409 and len(host.launches) == before
    assert native_state.read_session(op)["start"]["state"] == recorded["state"]  # untouched


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_a_started_session_takes_turns(host, project, monkeypatch, source):  # noqa: F811
    _, key = await _in_state("started", source, project, monkeypatch)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("state", sorted(START))
async def test_a_create_replay_only_observes(host, project, monkeypatch, source, state):  # noqa: F811
    op, key = await _in_state(state, source, project, monkeypatch)
    before, was = len(host.launches), _state(op)
    try:
        await runtime.create_session(ENGINES[source], str(project), operation_id=op, bypass=True)
    except runtime.StructuredError:
        pass  # a discarded/expired creation may refuse its replay — it never launches either way
    assert len(host.launches) == before and _state(op) == was


# state → resulting state after Discard
DISCARD = {
    "pending": "discarded",
    "failed": "discarded",
    "starting-dead": "discarded",
    "expired": "discarded",
    "discarded": "discarded",
    "started": "started",  # Stop of a running session is Stop, not a discard of its start
}


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("state", sorted(DISCARD))
async def test_discard(host, project, monkeypatch, source, state):  # noqa: F811
    op, key = await _in_state(state, source, project, monkeypatch)
    await runtime.stop(key)
    assert _state(op) == DISCARD[state]


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_a_discarded_failed_start_never_starts_again(host, project, monkeypatch, source):  # noqa: F811
    """Review 5926 (1): Discard after a failed start must be terminal."""
    op, key = await _in_state("failed", source, project, monkeypatch)
    await runtime.stop(key)
    before = len(host.launches)
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.start_session(key)
    assert exc.value.status == 409 and len(host.launches) == before
    assert _state(op) == "discarded"


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_a_late_completion_after_discard_is_a_no_op(host, project, monkeypatch, source):  # noqa: F811
    """Review 5926 (2): Discard lands while the worker comes up; its completion must not mark
    the discarded creation started, and no turn may relaunch it."""
    op, key = await _create(source, project)
    real = native_runtime._wait_ready

    async def ready_then_discarded(worker_id):
        life = await real(worker_id)
        await runtime.stop(key)  # the operator's Discard wins the race
        return life

    monkeypatch.setattr(native_runtime, "_wait_ready", ready_then_discarded)
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    assert _state(op) == "discarded"
    launched = len(host.launches)
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    assert len(host.launches) == launched


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_a_completion_for_a_replaced_generation_is_a_no_op(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    source,
):
    op, key = await _in_state("started", source, project, monkeypatch)
    assert native_runtime._finish_start(op, "not-the-starting-worker", True) is False
    assert native_runtime._finish_start(op, "not-the-starting-worker", False) is False
    assert _state(op) == "started"


def test_an_unreadable_start_state_never_launches():
    record = {"request": {"bypass": True}, "start": {"state": "bogus"}}
    assert native_runtime._start_state(record) == "discarded"
    assert native_runtime._start_state({"request": {"bypass": True}}) == "discarded"
    assert native_runtime._start_state({"request": {}}) is None


@pytest.mark.anyio
@pytest.mark.parametrize("source", SOURCES)
async def test_discard_in_flight_while_readiness_settles_is_still_terminal(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    source,
):
    """Review 5941: Discard reaches its worker-stop await while `/start` waits for readiness;
    readiness settles BEFORE the stop returns. The discard is recorded first, under the lock, so
    the completion's CAS fails and no turn can relaunch the creation."""
    import asyncio

    op, key = await _create(source, project)
    real_ready, real_call = native_runtime._wait_ready, native_runtime._call
    at_ready, ready_gate = asyncio.Event(), asyncio.Event()
    stop_entered, stop_gate = asyncio.Event(), asyncio.Event()

    async def held_ready(worker_id):
        life = await real_ready(worker_id)
        at_ready.set()
        await ready_gate.wait()
        return life

    async def held_call(gen, action, params, **kw):
        if action == "stop":
            stop_entered.set()
            await stop_gate.wait()
        return await real_call(gen, action, params, **kw)

    monkeypatch.setattr(native_runtime, "_wait_ready", held_ready)
    monkeypatch.setattr(native_runtime, "_call", held_call)
    starting = asyncio.create_task(runtime.start_session(key))
    await asyncio.wait_for(at_ready.wait(), 30)
    discarding = asyncio.create_task(runtime.stop(key))
    await asyncio.wait_for(stop_entered.wait(), 30)
    ready_gate.set()  # readiness settles while the stop IPC is still in flight
    with pytest.raises(runtime.StructuredError):
        await starting
    stop_gate.set()
    await discarding
    assert _state(op) == "discarded"
    launched = len(host.launches)
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    assert len(host.launches) == launched == 1
