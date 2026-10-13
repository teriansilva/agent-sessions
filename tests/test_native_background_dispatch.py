"""An operator message goes past running background TASKS, never past a turn the agent runs for
itself (#1427), on the scripted Claude client (tests/fake_native.py `BGTASK`).

The release rule needs the CLI's `command_lifecycle` frames (`emit-lifecycle`): with them the
codec can tell the operator's output from a task-notification turn's unmarked output; without
them (an older CLI) a running task holds the queue exactly as before.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, frames, host, ident, project, settle  # noqa: F401

ENGINE = ENGINES["claude"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _with_running_task(work, *, lifecycle: bool) -> str:
    """A Claude session whose first turn left a background task running."""
    if lifecycle:
        (work / "emit-lifecycle").write_text("")
    key = (await runtime.create_session(ENGINE, str(work), operation_id=ident()))["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="BGTASK")
    _, turn = await settle(key, first)
    assert turn["reply"] == "started"
    return key


async def _state(key: str, turn_id: str) -> str | None:
    snap = await runtime.snapshot(key)
    return next((t["state"] for t in snap["turns"] if t["turn_id"] == turn_id), None)


async def _replies(key: str) -> list[str]:
    return [t["reply"] for t in (await runtime.snapshot(key))["turns"]]


async def _task_completed(key: str) -> None:
    for _ in range(400):
        page = await runtime.events(key, after=0)
        done = [e for e in page["events"] if e["kind"] == "background"]
        if any((e.get("data") or e).get("state") == "completed" for e in done):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the background task never completed")


@pytest.mark.anyio
async def test_a_message_goes_out_while_a_background_task_runs(host, project):  # noqa: F811
    """The operator's report: a message waited 14 minutes behind a running monitor task."""
    key = await _with_running_task(project, lifecycle=True)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    _, turn = await settle(key, nxt)  # the task has NOT been released
    assert turn["reply"] == "echo:two"
    # …and it really was still running when the reply came: no completion recorded yet.
    page = await runtime.events(key, after=0)
    tasks = [(e.get("data") or e).get("state") for e in page["events"] if e["kind"] == "background"]
    assert "running" in tasks and "completed" not in tasks
    # The task's own notification turn, once it comes, is never anybody's reply.
    (project / "release-bg-task").write_text("")
    await _task_completed(key)
    await asyncio.sleep(0.5)
    assert "notified" not in "".join(await _replies(key))


@pytest.mark.anyio
async def test_without_lifecycle_frames_a_running_task_still_holds_the_queue(host, project):  # noqa: F811
    """An older CLI proves no fence: today's rule stays, for every message. (That rule's own
    limit stays too: without lifecycle frames a notification turn's first output can still race
    a message written as the task ends, which only the fence above closes.)"""
    key = await _with_running_task(project, lifecycle=False)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await asyncio.sleep(1.0)
    assert await _state(key, nxt) == "queued"
    assert (await runtime.snapshot(key))["native"]["background_turn"] is None
    (project / "release-bg-task").write_text("")
    _, turn = await settle(key, nxt)
    assert turn["reply"].endswith("echo:two")


async def _notification_turn_in_flight(key: str, work, hold: str) -> None:
    (work / hold).write_text("")
    (work / "release-bg-task").write_text("")
    await _task_completed(key)
    await asyncio.sleep(0.3)  # the notification turn's opening frames reach the worker


@pytest.mark.anyio
async def test_a_turn_the_agent_runs_for_itself_holds_a_message_sent_during_it(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """Overlap: the worker has SEEN the agent's own turn (its output, no operator owns it), so
    the message waits in the queue, then gets only its own reply."""
    key = await _with_running_task(project, lifecycle=True)
    await _notification_turn_in_flight(key, project, "hold-notification")
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await asyncio.sleep(0.8)
    assert await _state(key, nxt) == "queued"
    (project / "release-notification").write_text("")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two"
    assert "notified" not in "".join(await _replies(key))


@pytest.mark.anyio
async def test_a_message_written_as_the_agent_starts_its_own_turn_gets_only_its_reply(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """The race: the agent's own turn has started but shown nothing when the message is written.
    Its output then arrives AFTER the write, before the message's own `started`: it is the
    agent's, never the operator's reply."""
    key = await _with_running_task(project, lifecycle=True)
    await _notification_turn_in_flight(key, project, "hold-notification-start")
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await asyncio.sleep(0.8)
    (project / "release-notification").write_text("")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two"
    assert "notified" not in "".join(await _replies(key))


@pytest.mark.anyio
async def test_the_worker_does_not_idle_out_while_a_task_runs(host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    key = await _with_running_task(project, lifecycle=True)
    worker = (await runtime.snapshot(key))["native"]["worker"]
    await asyncio.sleep(2.5)
    assert (await runtime.snapshot(key))["native"]["worker"] == worker
    (project / "release-bg-task").write_text("")


# --- Phase 2: Send now against a turn the agent runs for itself --------------------------------


async def _queued_behind_agent_turn(work) -> tuple[str, str, str]:
    """A message queued while the agent runs a notification turn; returns (key, queued, target)."""
    key = await _with_running_task(work, lifecycle=True)
    await _notification_turn_in_flight(key, work, "hold-notification")
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    for _ in range(100):
        native = (await runtime.snapshot(key))["native"]
        if native.get("background_turn"):
            break
        await asyncio.sleep(0.05)
    assert native["send_now_background"] == "interrupt" and native["background_turn"]
    assert await _state(key, nxt) == "queued"
    return key, nxt, native["background_turn"]


@pytest.mark.anyio
async def test_send_now_interrupts_the_agents_own_turn_and_delivers_the_message(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """The operator's case end to end: the queued message goes now, not when the agent's turn
    (and the work behind it) ends; the agent's own output stays nobody's reply."""
    key, nxt, target = await _queued_behind_agent_turn(project)
    receipt = await runtime.send_now(key, operation_id=ident(), turn_id=target, queued_turn_id=nxt)
    assert receipt["handoff"] == "sent"
    _, turn = await settle(key, nxt)  # release-notification was never created
    assert turn["reply"] == "echo:two"
    assert "notified" not in "".join(await _replies(key))
    assert any(
        f["frame"].get("type") == "control_request"
        and f["frame"]["request"]["subtype"] == "interrupt"
        for f in frames(project)
    )
    assert (await runtime.snapshot(key))["native"]["background_turn"] is None


@pytest.mark.anyio
async def test_a_worker_without_the_capability_is_never_offered_or_sent_it(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """A worker from before #1427 (no `send_now_background` in its ready report): the target is
    hidden from the snapshot and refused by the server, and the message waits as before."""
    key, nxt, target = await _queued_behind_agent_turn(project)
    worker = (await runtime.snapshot(key))["native"]["worker"]
    native_state.update_lifecycle(worker, send_now_background=None)
    assert (await runtime.snapshot(key))["native"]["background_turn"] is None
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.send_now(key, operation_id=ident(), turn_id=target, queued_turn_id=nxt)
    assert refused.value.status == 409
    (project / "release-notification").write_text("")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two"


@pytest.mark.anyio
async def test_send_now_reaches_the_agents_own_turn_before_its_first_output(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """Hermes on #1429 (review 6203): a turn the agent started for itself, known from its running
    state, is a Send now target before it has written anything; the queue is never held with no
    way through."""
    (project / "emit-session-state").write_text("")
    key = await _with_running_task(project, lifecycle=True)
    await _notification_turn_in_flight(key, project, "hold-notification-start")  # no output yet
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    for _ in range(100):
        native = (await runtime.snapshot(key))["native"]
        if native.get("background_turn"):
            break
        await asyncio.sleep(0.05)
    assert native["background_turn"] and native["send_now_background"] == "interrupt"
    assert await _state(key, nxt) == "queued"
    await runtime.send_now(
        key, operation_id=ident(), turn_id=native["background_turn"], queued_turn_id=nxt
    )
    _, turn = await settle(key, nxt)  # release-notification is never created
    assert turn["reply"] == "echo:two"
    assert "notified" not in "".join(await _replies(key))


@pytest.mark.anyio
async def test_send_now_reaches_the_agents_own_turn_before_output_on_the_current_cli(
    host,  # noqa: F811
    project,  # noqa: F811
):
    """The CURRENT CLI's frames (no session state at all): the agent's own turn is announced by
    `status: requesting` and then writes nothing yet. The target is already live and Send now
    goes through."""
    key = await _with_running_task(project, lifecycle=True)  # no emit-session-state
    await _notification_turn_in_flight(key, project, "hold-notification-start")  # no output yet
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    for _ in range(100):
        native = (await runtime.snapshot(key))["native"]
        if native.get("background_turn"):
            break
        await asyncio.sleep(0.05)
    assert native["background_turn"] and native["send_now_background"] == "interrupt"
    assert await _state(key, nxt) == "queued"
    await runtime.send_now(
        key, operation_id=ident(), turn_id=native["background_turn"], queued_turn_id=nxt
    )
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two"
    assert "notified" not in "".join(await _replies(key))
