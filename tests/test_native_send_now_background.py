"""#1526: Send now on a Claude API session never stops the agent's background agents.

Real workers and codecs against the scripted Claude client (tests/fake_native.py). With
`stop-ends-tasks` the fake behaves like the real CLI: a plain `interrupt` is a Stop that also ends
every background task (`BGTASK`); its `send_now` interrupt (`advertise-send-now`) does not.
"""

from __future__ import annotations

import asyncio

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_worker
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, frames, host, ident, project, settle  # noqa: F401

ENGINE = ENGINES["claude"]
conversation = journal_tests.conversation


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _hanging_with_background_agent(work, *, capable: bool):
    """A turn that runs until cut, while a background task its first turn started keeps running.
    Returns the session, the running turn and the queued message."""
    for flag in ("emit-lifecycle", "stop-ends-tasks", *(["advertise-send-now"] if capable else [])):
        (work / flag).write_text("")
    key = (await runtime.create_session(ENGINE, str(work), operation_id=ident()))["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="BGTASK")
    _, turn = await settle(key, first)
    assert turn["reply"] == "started"
    active, waiting = ident(), ident()
    await runtime.submit_turn(key, operation_id=active, text="HANG")
    for _ in range(200):
        if any(e["kind"] == "turn_started" for e in (await runtime.events(key, after=0))["events"]):
            break
        await asyncio.sleep(0.02)
    await runtime.submit_turn(key, operation_id=waiting, text="urgent")
    await asyncio.sleep(0.3)
    return key, active, waiting


def _interrupts(work):
    return [
        f["frame"]["request"]
        for f in frames(work)
        if f["frame"].get("type") == "control_request"
        and f["frame"]["request"]["subtype"] == "interrupt"
    ]


def _inputs(work):
    return [f["frame"]["uuid"] for f in frames(work) if f["frame"].get("type") == "user"]


async def _task_states(key):
    events = (await runtime.events(key, after=0))["events"]
    return [
        (e.get("data") or e).get("state")
        for e in events
        if e["kind"] == "background" and (e.get("data") or e).get("task_id") == "bg1"
    ]


@pytest.mark.anyio
async def test_send_now_cuts_the_turn_and_the_background_agent_keeps_running(host, project):  # noqa: F811
    key, active, waiting = await _hanging_with_background_agent(project, capable=True)
    receipt = await runtime.send_now(
        key, operation_id=ident(), turn_id=active, queued_turn_id=waiting
    )
    assert receipt["handoff"] == "sent"
    _, turn = await settle(key, waiting)
    assert turn["reply"] == "echo:urgent"
    _, cut = await settle(key, active)
    assert cut["state"] == "interrupted"
    # The CLI's own Send now, naming the message written to it first — never a Stop.
    assert _interrupts(project) == [
        {"subtype": "interrupt", "send_now": True, "message_uuid": waiting}
    ]
    assert _inputs(project).count(waiting) == 1  # written once, never again by the FIFO
    assert "stopped" not in await _task_states(key)


@pytest.mark.anyio
async def test_stop_stays_a_stop(host, project):  # noqa: F811
    key, active, waiting = await _hanging_with_background_agent(project, capable=True)
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    _, turn = await settle(key, waiting)  # the queued message still runs after it
    assert turn["reply"] == "echo:urgent"
    assert _interrupts(project) == [{"subtype": "interrupt"}]
    for _ in range(100):
        if "stopped" in await _task_states(key):
            break
        await asyncio.sleep(0.05)
    assert "stopped" in await _task_states(key)


@pytest.mark.anyio
async def test_without_the_cli_send_now_it_is_refused_while_background_work_runs(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key, active, waiting = await _hanging_with_background_agent(project, capable=False)
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=waiting)
    assert native_worker.SEND_NOW_WOULD_STOP in error.value.detail
    await asyncio.sleep(0.3)
    assert _interrupts(project) == []  # nothing was cut, nothing was stopped
    snap = await runtime.snapshot(key)
    assert next(t for t in snap["turns"] if t["turn_id"] == waiting)["state"] == "queued"
    assert "stopped" not in await _task_states(key)


# --- the worker alone: the agent's own turn, and a write whose outcome is unknown ---------------


def _own_turn_worker(conversation, monkeypatch):
    from test_native_send_now import background_worker

    worker, queued, control = background_worker(conversation, monkeypatch, "claude")
    worker.codec.native_send_now = True
    worker.codec._background.add("agent-1")
    return worker, queued, control


@pytest.mark.anyio
async def test_the_agents_own_turn_is_cut_by_writing_the_message_then_sending_it_now(
    conversation, monkeypatch
):
    from agent_sessions import native_journal

    worker, queued, control = _own_turn_worker(conversation, monkeypatch)
    writes = []

    async def write(frame):
        writes.append(frame)

    monkeypatch.setattr(worker, "write", write)
    result = await worker.effect(control)
    assert result["result"]["handoff"] == "sent"
    message, request = writes
    op = queued["params"]["operation_id"]
    assert message["type"] == "user" and message["uuid"] == op and "priority" not in message
    assert request["request"] == {"subtype": "interrupt", "send_now": True, "message_uuid": op}
    assert worker.queued == [] and worker.codec.operation_id == op
    journal = native_journal.read(conversation[0], conversation[1])
    assert journal.operations[op].handoff == "sent"
    # A second Send now naming the turn it cut finds nothing queued behind it.
    again = await worker.effect({**control, "params": {**control["params"]}})
    assert again["result"]["handoff"] == "sent" and len(writes) == 2  # replayed, not rewritten


@pytest.mark.anyio
async def test_a_message_write_that_fails_is_never_resent_and_nothing_is_cut(
    conversation, monkeypatch
):
    from agent_sessions import native_journal

    worker, queued, control = _own_turn_worker(conversation, monkeypatch)
    stopped = []

    async def write(frame):
        raise ConnectionResetError("half written")

    monkeypatch.setattr(worker, "write", write)
    monkeypatch.setattr(worker, "stop_child", lambda: stopped.append(True))
    result = await worker.effect(control)
    assert result["result"]["handoff"] == "not_sent"
    assert stopped  # no further write without durable evidence
    op = queued["params"]["operation_id"]
    journal = native_journal.read(conversation[0], conversation[1])
    assert journal.operations[op].handoff == "uncertain"  # never replayed by a successor
    assert worker.queued == []
