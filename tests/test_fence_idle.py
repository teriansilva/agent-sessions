"""#2232: the step fence never takes a runtime snapshot on a second event loop.

`mission_fence.fenced_step_stop` asks `idle()` INSIDE the fence, holding `session_input._lock`, on
a worker thread. A chat session's snapshot takes that session's `asyncio.Lock`
(`chat_runtime._lock`), which belongs to the MAIN loop: driven from the worker's own loop it can
hang the fence under the global lock and leave the chat lock bound to a dead loop. Here the main
loop is the test's own; the fence double holds the REAL global lock around `idle()`, as the real
fence does, on a worker thread.
"""

# ruff: noqa: F811 — imported pytest fixtures
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from agent_sessions import (
    chat_runtime,
    fence_idle,
    mission_fence,
    mission_rework,
    mission_steps,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from test_mission_steps import (
    _adopt,
    _claim,
    _mission,
    _runs,
    _settle_evidence,
    anyio_backend,  # noqa: F401 — the anyio backend fixture
)

KEY = "claude-api:11111111-1111-4111-8111-111111111111"


class _LockingFence:
    """`fenced_step_stop` as it behaves: on a worker thread, holding the global lock, it asks
    `idle()` and settles `stopped` only on a True answer. Bounded: a fence that does not return
    within `limit` seconds is a hang (the bug), reported as such."""

    def __init__(self, limit: float = 3.0) -> None:
        self.limit = limit
        self.stopped = 0
        self.calls = 0

    async def __call__(self, mission_id, key, settle, *, idle=None):
        self.calls += 1

        def fenced() -> str:
            with session_input._lock:
                outcome = "stopped" if idle is None or idle() else "busy"
                settle(outcome)
                return outcome

        worker = asyncio.to_thread(fenced)
        try:
            outcome = await asyncio.wait_for(worker, self.limit)
        except TimeoutError:
            raise AssertionError("the fence hung under the global lock") from None
        self.stopped += outcome == "stopped"
        return outcome


def _chat_like_snapshot(idle: bool = True):
    """A runtime snapshot that, like `chat_runtime.get_session`, takes the session's chat lock."""

    async def snapshot(key, **_):
        async with chat_runtime._lock(key):
            return {"session_key": key, "active_turn": None if idle else "t1", "native": {}}

    return snapshot


def _global_lock_free(timeout: float = 1.0) -> bool:
    if not session_input._lock.acquire(timeout=timeout):
        return False
    session_input._lock.release()
    return True


@pytest.fixture
def stable_marker(monkeypatch):
    """These sessions have no persisted runtime: the lock-free marker is a stable stand-in."""
    monkeypatch.setattr(fence_idle, "marker_reader", lambda key: (lambda k=key: ("stable", k)))
    monkeypatch.setattr(fence_idle, "OBSERVE_TIMEOUT_S", 0.3)
    fence_idle._OUTSTANDING.clear()
    yield
    fence_idle._OUTSTANDING.clear()


# ---- the ordinary step stop (`mission_steps.reconcile`) ------------------------------------------


@pytest.mark.anyio
async def test_a_held_chat_lock_never_hangs_the_step_fence(monkeypatch, stable_marker):
    mid, doc = _mission()
    _adopt(mid, _claim(mid, cwd="/tmp/held"), key=KEY)
    _settle_evidence(mid, doc, "o1.posting")
    fence = _LockingFence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(runtime, "snapshot", _chat_like_snapshot())
    lock = chat_runtime._lock(chat_runtime._key(*KEY.split(":", 1)))
    lock_key = KEY
    monkeypatch.setattr(chat_runtime, "_lock", lambda k, _l=lock: _l)  # the session's ONE lock
    await lock.acquire()  # a turn is writing its records, on the main loop
    try:
        assert await mission_steps.reconcile(mid) == 0
        assert fence.stopped == 0 and _runs(mid)[0]["state"] == "running"
        assert await asyncio.to_thread(_global_lock_free), "the global lock stayed held"
    finally:
        lock.release()
    # The chat lock is still this loop's: a contended acquire works (not "bound to a different
    # event loop"), and the outstanding observation finishes on its own.
    await lock.acquire()
    waiter = asyncio.ensure_future(lock.acquire())
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(waiter, 1)
    lock.release()
    for _ in range(50):
        if lock_key not in fence_idle._OUTSTANDING:
            break
        await asyncio.sleep(0.02)
    # Next pass: the session is idle and the lock free, so the stop goes through.
    assert await mission_steps.reconcile(mid) == 1 and fence.stopped == 1


# ---- the rework fence (`mission_rework._settle_one`) ---------------------------------------------


@pytest.mark.anyio
async def test_a_held_chat_lock_never_hangs_the_rework_fence(monkeypatch, stable_marker):
    """The REAL rework fence path (`_settle_one` → `_fence_ready` → `_idle_snapshot`) with a
    runtime snapshot that takes the session's chat lock, as `chat_runtime.get_session` does."""
    fence = _LockingFence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(runtime, "snapshot", _chat_like_snapshot())
    lock = asyncio.Lock()
    monkeypatch.setattr(chat_runtime, "_lock", lambda k: lock)
    cand = {
        "run": {"step_id": "o1.review", "episode": 1, "session_key": KEY, "plan_id": "p"},
        "observation_id": "obs:x",
        "judged": False,
        "fresh_at": time.time(),
        "approval_id": None,
    }
    await lock.acquire()  # a turn is writing its records, on the main loop
    try:
        assert await mission_rework._settle_one("msn_" + "a" * 32, cand) is None
        assert fence.calls == 1 and fence.stopped == 0
        assert await asyncio.to_thread(_global_lock_free), "the global lock stayed held"
    finally:
        lock.release()
    waiter = asyncio.ensure_future(lock.acquire())  # still this loop's lock
    await asyncio.wait_for(waiter, 1)
    lock.release()


# ---- the observation's own contract ---------------------------------------------------------


@pytest.mark.anyio
async def test_one_outstanding_read_per_key_follows_the_underlying_work(monkeypatch):
    """A read that times out DURING its underlying I/O (a held worker) keeps its slot until that
    work has really finished: a retry meanwhile refuses (not idle); after it, a retry succeeds."""
    monkeypatch.setattr(fence_idle, "OBSERVE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(fence_idle, "marker_reader", lambda key: (lambda: ("stable",)))
    fence_idle._OUTSTANDING.clear()
    release = threading.Event()
    started = []

    def slow_io():
        started.append(time.monotonic())
        assert release.wait(10)
        return {"active_turn": None, "native": {}}

    async def snapshot(key):
        return await asyncio.to_thread(slow_io)

    monkeypatch.setattr(fence_idle, "_snapshot", snapshot)
    assert await fence_idle.observe(KEY) is None  # timed out during the I/O
    assert await fence_idle.observe(KEY) is None  # the worker still runs: refused, no new read
    assert len(started) == 1, "a second read was started while the first's worker still ran"
    release.set()
    for _ in range(100):
        if KEY not in fence_idle._OUTSTANDING:
            break
        await asyncio.sleep(0.02)
    assert KEY not in fence_idle._OUTSTANDING
    got = await fence_idle.observe(KEY)
    assert got is not None and got.idle and len(started) == 2


@pytest.mark.anyio
async def test_a_turn_that_starts_after_the_marker_is_never_validated_idle(monkeypatch):
    """Independent review of #2232: the marker is read FIRST. A turn that starts after it — here
    right after an idle snapshot — makes the fence's marker differ, so a busy session is never
    validated idle (with the marker read after the snapshot, it was)."""
    state = {"busy": False}
    monkeypatch.setattr(fence_idle, "marker_reader", lambda key: lambda: ("m", state["busy"]))

    async def idle_then_a_turn_starts(key):
        snap = {"active_turn": None, "native": {}}
        state["busy"] = True
        return snap

    monkeypatch.setattr(fence_idle, "_snapshot", idle_then_a_turn_starts)
    fence_idle._OUTSTANDING.clear()
    obs = await fence_idle.observe(KEY)
    assert obs is not None and obs.idle
    assert fence_idle.validate(obs) is None


def _obs(reader, marker=("m",), *, idle=True, age=0.0):
    return fence_idle.Observation(
        key=KEY,
        snapshot={"active_turn": None},
        marker=marker,
        idle=idle,
        at=time.monotonic() - age,
        reader=reader,
    )


def test_validate_refuses_every_change_and_every_doubt(monkeypatch):
    now = {"marker": ("m",)}

    def reader():  # the observation's own reader
        return now["marker"]

    assert fence_idle.validate(_obs(reader)) == {"active_turn": None}
    assert fence_idle.validate(None) is None
    assert fence_idle.validate(_obs(reader, idle=False)) is None  # busy when observed
    assert (
        fence_idle.validate(_obs(reader, age=fence_idle.OBSERVE_MAX_AGE_S + 1)) is None
    )  # too old
    now["marker"] = ("m", "a new turn")
    assert fence_idle.validate(_obs(reader)) is None  # a turn moved since
    now["marker"] = None
    assert fence_idle.validate(_obs(reader)) is None  # unreadable now


def test_the_chat_marker_is_a_stat_and_live_tasks(monkeypatch, tmp_path):
    """The chat reader: the log file's stat stamp, a turn running in this process, and the
    session's physical key — no log read, no chat lock. Any append moves it."""
    sid = "0c0c0c0c-3333-4333-8333-0c0c0c0c0c0c"
    log_file = tmp_path / f"{sid}.jsonl"
    log_file.write_text('{"turn": 1}\n')
    monkeypatch.setattr(
        runtime,
        "_observation_target",
        lambda k, op: ("claude-api", sid, runtime._ChatAdapter(), None),
    )
    monkeypatch.setattr(chat_runtime, "_chat_provider", lambda engine: object())
    monkeypatch.setattr(chat_runtime, "_root", lambda prov: tmp_path)
    physical = {"key": KEY}
    monkeypatch.setattr(mission_fence, "physical_of", lambda key: physical["key"])
    running: set[str] = set()
    monkeypatch.setattr(chat_runtime, "running_keys", lambda: set(running))
    from agent_sessions import chat_store

    def no_read(*a, **k):
        raise AssertionError("the chat reader parsed the log")

    monkeypatch.setattr(chat_store, "read", no_read)
    monkeypatch.setattr(chat_runtime, "_lock", no_read)
    reader = fence_idle.marker_reader(KEY)
    first = reader()
    assert first is not None and first == reader()
    with log_file.open("a") as f:
        f.write('{"turn": 2}\n')
    second = reader()
    assert second != first  # an append moved it
    running.add(chat_runtime._key("claude-api", sid))
    assert reader() != second  # a turn now runs in this process
    running.clear()
    physical["key"] = "claude-api:22222222-2222-4222-8222-222222222222"
    assert reader() != second  # the session was replaced
    log_file.unlink()
    assert reader() is None  # missing: never idle


SID = "0b0b0b0b-1111-4111-8111-0b0b0b0b0b0b"


def _native_reader(monkeypatch, root):
    """The real native marker reader over a journal directory `root` (its provider resolved)."""
    from agent_sessions import native_runtime

    native = runtime._NativeAdapter()
    monkeypatch.setattr(
        runtime, "_observation_target", lambda k, op: ("codex-api", SID, native, None)
    )
    monkeypatch.setattr(mission_fence, "physical_of", lambda key: key)
    monkeypatch.setattr(native_runtime, "_session", lambda e, s, retiring_ok=False: (object(), {}))
    monkeypatch.setattr(native_runtime, "_journal_root", lambda prov: root)
    return fence_idle.marker_reader(KEY)


def test_the_native_marker_is_the_active_segments_stat(monkeypatch, tmp_path):
    """The native reader: the active journal segment's stat stamp (`active_stamp`, the events
    wake's lock-free probe) — no journal parse, no settling. An append and a rollover move it."""
    from agent_sessions import native_runtime, native_segments

    seg0 = native_segments.segment_path(tmp_path, SID, 0)
    seg0.write_text('{"r": 1}\n')
    reader = _native_reader(monkeypatch, tmp_path)

    def no_parse(*a, **k):
        raise AssertionError("the native reader parsed the journal")

    monkeypatch.setattr(native_runtime, "_snapshot_sync", no_parse)
    monkeypatch.setattr(native_runtime, "_journal", no_parse)
    first = reader()
    assert first is not None and first == reader()
    with seg0.open("a") as f:
        f.write('{"r": 2}\n')
    second = reader()
    assert second != first  # an append moved it
    native_segments.segment_path(tmp_path, SID, 1).write_text('{"r": 3}\n')
    assert reader() != second  # a rollover moved the active segment


@pytest.mark.anyio
async def test_a_held_journal_lock_never_blocks_the_in_fence_read(monkeypatch, tmp_path):
    """Hermes on #2237: the journal's exclusive flock (an append or rollover in progress, or a
    stalled writer) must not hold the fence — the in-fence read only stats files. Under the
    real global lock, with the journal lock held by another thread, it answers at once, and
    another thread can still take the global lock."""
    import fcntl

    from agent_sessions import native_segments

    native_segments.segment_path(tmp_path, SID, 0).write_text('{"r": 1}\n')
    reader = _native_reader(monkeypatch, tmp_path)
    lock_fd = os.open(native_segments.lock_path(tmp_path, SID), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)  # a writer holds the journal
    try:

        def in_fence() -> float:
            with session_input._lock:
                t0 = time.monotonic()
                assert reader() is not None
                return time.monotonic() - t0

        took = await asyncio.wait_for(asyncio.to_thread(in_fence), 3)
        assert took < 0.5, f"the in-fence read waited {took:.2f}s on the journal lock"
        assert await asyncio.to_thread(_global_lock_free)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@pytest.mark.anyio
async def test_the_deadline_covers_the_first_read_and_keeps_its_slot(monkeypatch):
    """Hermes on #2237: the marker read is inside the observation's ONE deadline. A first read
    that hangs answers None in time, keeps the slot until it really finishes (a retry
    meanwhile refuses and starts nothing), and a retry after it succeeds."""
    monkeypatch.setattr(fence_idle, "OBSERVE_TIMEOUT_S", 0.2)
    fence_idle._OUTSTANDING.clear()
    release = threading.Event()
    resolved = []

    def slow_reader_resolution(key):
        resolved.append(time.monotonic())
        assert release.wait(10)
        return lambda: ("m",)

    async def idle(key):
        return {"active_turn": None, "native": {}}

    monkeypatch.setattr(fence_idle, "marker_reader", slow_reader_resolution)
    monkeypatch.setattr(fence_idle, "_snapshot", idle)
    t0 = time.monotonic()
    assert await fence_idle.observe(KEY) is None
    assert time.monotonic() - t0 < 1.0, "the first read was not under the deadline"
    assert await fence_idle.observe(KEY) is None and len(resolved) == 1
    release.set()
    for _ in range(100):
        if KEY not in fence_idle._OUTSTANDING:
            break
        await asyncio.sleep(0.02)
    got = await fence_idle.observe(KEY)
    assert got is not None and got.idle and len(resolved) == 2


@pytest.mark.anyio
async def test_two_concurrent_observations_of_one_key_start_one_read(monkeypatch):
    """Hermes on #2237: the slot is reserved before the first await — two concurrent callers
    never both start an observation of the same key."""
    fence_idle._OUTSTANDING.clear()
    gate = threading.Event()
    starts = []

    def reader_for(key):
        starts.append(key)
        assert gate.wait(10)
        return lambda: ("m",)

    async def idle(key):
        return {"active_turn": None, "native": {}}

    monkeypatch.setattr(fence_idle, "marker_reader", reader_for)
    monkeypatch.setattr(fence_idle, "_snapshot", idle)
    first = asyncio.ensure_future(fence_idle.observe(KEY))
    second = asyncio.ensure_future(fence_idle.observe(KEY))
    await asyncio.sleep(0.05)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert len(starts) == 1, starts
    assert (a is None) != (b is None)  # exactly one of them observed


def test_no_runtime_snapshot_is_driven_through_asyncio_run_in_the_fence_modules():
    """The fence's callers keep no `asyncio.run(...snapshot...)` (#2232)."""
    import inspect

    for mod in (mission_steps, mission_rework, fence_idle):
        assert "asyncio.run(runtime.snapshot" not in inspect.getsource(mod), mod.__name__
