"""#1647: a skip start still inside admission reads as in flight, and a discarded one says so.

`/start` records `starting` with no generation BEFORE it waits on the global `launch` lock and the
ownership admission. A snapshot read takes no session lock, so on a loaded host it used to read
that record as a start that "didn't finish" and offer Start again / Discard while the start was
about to succeed (the operator discarded one 91 ms before its worker was ready). A discarded
creation used to read like a guarded session, so its composer stayed open for text every send
then refused.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_sessions import native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import storage
from test_native_runtime import ENGINES, host, ident, project  # noqa: F401 — fixtures


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _create(project, *, bypass=True):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(
        ENGINES["codex"], str(project), operation_id=op, bypass=bypass
    )
    return op, snap["session_key"]


def _admitting(op, age):
    """The record `/start` leaves while it waits for admission, written `age` seconds ago."""
    record = native_state.read_session(op)
    record["start"].update(state="starting", gen=None, at=time.time() - age)
    native_state.write_session(op, record)


@pytest.mark.anyio
async def test_a_start_waiting_on_the_launch_lock_reads_in_flight(host, project):  # noqa: F811
    op, key = await _create(project)
    with storage.locked("launch", wait=5):
        starting = asyncio.create_task(runtime.start_session(key))
        deadline = time.monotonic() + 10
        while native_state.read_session(op)["start"]["state"] != "starting":
            assert time.monotonic() < deadline, "the start never recorded itself"
            await asyncio.sleep(0.02)
        snap = await runtime.snapshot(key)
        assert native_state.read_session(op)["start"]["gen"] is None  # still waiting: no worker
        assert snap["pending_start"] is False and snap["start_in_flight"] is True
        assert "start_incomplete" not in snap and "start_failed" not in snap
    started = await starting
    assert started["pending_start"] is False and "start_in_flight" not in started


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("age", "in_flight"),
    [
        (native_runtime.START_ADMISSION_TIMEOUT - 1, True),
        (native_runtime.START_ADMISSION_TIMEOUT + 1, False),
    ],
)
async def test_admission_reads_in_flight_only_within_its_bound(host, project, age, in_flight):  # noqa: F811
    op, key = await _create(project)
    _admitting(op, age)
    snap = await runtime.snapshot(key)
    if in_flight:
        assert snap["pending_start"] is False and snap["start_in_flight"] is True
        assert "start_incomplete" not in snap
    else:
        # Its process is presumed gone: Start again reconciles it, as before #1647.
        assert snap["pending_start"] is True and snap["start_incomplete"] is True
        assert "start_in_flight" not in snap


@pytest.mark.anyio
async def test_a_start_stamped_in_the_future_is_not_in_flight(host, project):  # noqa: F811
    op, key = await _create(project)
    _admitting(op, -60)  # a clock step back must not hold the view in flight for longer
    snap = await runtime.snapshot(key)
    assert snap["pending_start"] is True and snap["start_incomplete"] is True


@pytest.mark.anyio
async def test_a_stale_admission_still_starts_again(host, project):  # noqa: F811
    op, key = await _create(project)
    _admitting(op, native_runtime.START_ADMISSION_TIMEOUT + 1)
    before = len(host.launches)
    await runtime.start_session(key)
    assert len(host.launches) == before + 1
    assert native_state.read_session(op)["start"]["state"] == "started"


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["pending", "admitting"])
async def test_a_discarded_creation_says_so(host, project, stage):  # noqa: F811
    op, key = await _create(project)
    if stage == "admitting":
        _admitting(op, 1)
    await runtime.stop(key)
    snap = await runtime.snapshot(key)
    assert snap["start_discarded"] is True
    assert snap["pending_start"] is False and "start_in_flight" not in snap


@pytest.mark.anyio
async def test_only_a_discarded_skip_start_reads_discarded(host, project):  # noqa: F811
    _, started = await _create(project)
    await runtime.start_session(started)
    await runtime.stop(started)  # Stop of a running session is Stop, never a discard
    assert "start_discarded" not in await runtime.snapshot(started)
    _, guarded = await _create(project, bypass=False)
    await runtime.stop(guarded)
    assert "start_discarded" not in await runtime.snapshot(guarded)
