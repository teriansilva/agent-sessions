"""A pending ScheduleWakeup keeps an API session alive until it fires (#1927).

The wakeup is the Claude CLI's own timer: it only fires while the child runs. The worker holds
its idle exit (and a rollover retire) for it, outside `busy()`, so a lapsed wake still exits at
exactly ``max(last_activity + idle_timeout, wake_due + WAKE_GRACE)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_ipc, native_journal, native_protocol, share_api
from agent_sessions import structured_runtime as runtime_api
from agent_sessions.native_processes import Floor
from agent_sessions.native_protocol import ClaudeCodec
from test_native_capacity import _claude_worker, _full
from test_native_liveness import _append, _binding, _project
from test_native_runtime import ident

conversation = journal_tests.conversation
SESSION = "b6bf1a94-f584-4d7a-b2a2-850de2ab17f5"


def _call(codec, tool_input, item="w1"):
    block = {"type": "tool_use", "id": item, "name": "ScheduleWakeup", "input": tool_input}
    return codec.feed(
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [block]}}
    )


def _result(codec, content, item="w1", error=False):
    block = {"type": "tool_result", "tool_use_id": item, "content": content}
    if error:
        block["is_error"] = True
    return codec.feed({"type": "user", "message": {"role": "user", "content": [block]}})


def _schedule(codec, delay, output=None, item="w1", **extra):
    _call(codec, {"delaySeconds": delay, "reason": "watch CI", "prompt": "go", **extra}, item)
    _result(codec, output if output is not None else f"Next wakeup scheduled (in {delay}s).", item)


def _agent_turn_starts(codec):
    """A fresh agent-started turn. The scheduling call's own output opened one: end it first,
    as its result does."""
    codec._agent_turn(False)
    codec._agent_turn(True)
    codec._agent_turn(False)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(native_protocol, "_monotonic", lambda: now[0])
    return now


# --- the codec: what is pending ------------------------------------------------------------


def test_the_delay_is_the_one_the_cli_reports_not_the_one_asked(clock):
    codec = ClaudeCodec(SESSION)
    _call(codec, {"delaySeconds": 600, "prompt": "go", "reason": "r"})
    assert codec.wake_due is None  # asked, not yet scheduled
    _result(codec, [{"type": "text", "text": "Next wakeup scheduled for 00:55:00 (in 633s)."}])
    assert codec.wake_due == 1000.0 + 633


def test_without_a_reported_delay_the_input_is_used_and_clamped(clock):
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800, output="scheduled")
    assert codec.wake_due == 1000.0 + 1800
    _schedule(codec, 99999, output="scheduled", item="w2")
    assert codec.wake_due == 1000.0 + 3600  # the CLI's own ceiling
    _schedule(codec, 1, output="scheduled", item="w3")
    assert codec.wake_due == 1000.0 + 60


def test_a_newer_wakeup_replaces_the_pending_one(clock):
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800)
    clock[0] += 100
    _schedule(codec, 300, item="w2")
    assert codec.wake_due == 1100.0 + 300


def test_stop_cancels_the_pending_wakeup(clock):
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800)
    _call(codec, {"stop": True}, "w2")
    _result(codec, "Loop stopped.", "w2")
    assert codec.wake_due is None


@pytest.mark.parametrize(
    ("tool_input", "output", "error"),
    [
        ({"delaySeconds": 900, "prompt": "go"}, "<tool_use_error>refused</tool_use_error>", True),
        ({"delaySeconds": "soon", "prompt": "go"}, "scheduled", False),
        ({"delaySeconds": True, "prompt": "go"}, "scheduled", False),
        ({"prompt": "go"}, "scheduled", False),
        ("not an object", "scheduled", False),
    ],
    ids=["refused", "text-delay", "bool-delay", "no-delay", "not-an-object"],
)
def test_a_refused_or_unreadable_call_leaves_what_was_pending(clock, tool_input, output, error):
    """The CLI keeps its timer when a later call fails: so does the codec. Nothing pending stays
    nothing pending, which is the idle rule as before."""
    codec = ClaudeCodec(SESSION)
    _call(codec, tool_input)
    _result(codec, output, error=error)
    assert codec.wake_due is None
    _schedule(codec, 1800, item="w2")
    _call(codec, tool_input, "w3")
    _result(codec, output, "w3", error=error)
    assert codec.wake_due == 1000.0 + 1800


@pytest.mark.parametrize(("delay", "clamped"), [(10**400, 3600), (-(10**400), 60)])
def test_an_integer_past_float_range_is_clamped_never_raised(clock, delay, clamped):
    """Review on #1929: JSON integers are unbounded, and `float()` of one past the float range
    raised OverflowError out of the reader. The clamp comes first."""
    codec = ClaudeCodec(SESSION)
    _schedule(codec, delay, output="scheduled")
    assert codec.wake_due == 1000.0 + clamped


def test_parallel_calls_each_keep_their_own_input(clock):
    """Review on #1929: two calls in one message; the later one refused keeps the earlier one's
    timer, as the CLI does."""
    codec = ClaudeCodec(SESSION)
    blocks = [
        {"type": "tool_use", "id": i, "name": "ScheduleWakeup", "input": {"delaySeconds": d}}
        for i, d in (("w1", 1800), ("w2", 900))
    ]
    codec.feed(
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": blocks}}
    )
    _result(codec, "scheduled", "w1")
    _result(codec, "refused", "w2", error=True)
    assert codec.wake_due == 1000.0 + 1800


def test_a_result_for_another_tool_changes_nothing(clock):
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800)
    _result(codec, "(in 5s)", item="some-bash-call")
    assert codec.wake_due == 1000.0 + 1800


def test_an_unrelated_agent_turn_before_the_wake_keeps_it_pending(clock):
    """Hermes on #1927: only the wake's own turn consumes it, never a task's or an earlier one."""
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800)
    clock[0] += 600
    _agent_turn_starts(codec)
    assert codec.wake_due == 1000.0 + 1800
    clock[0] = 1000.0 + 1800 - native_protocol.WAKE_EARLY  # the timer fires
    _agent_turn_starts(codec)
    assert codec.wake_due is None


# --- the worker: the idle exit waits for it -------------------------------------------------


def _watch(worker, seconds):
    async def run():
        watch = asyncio.create_task(worker.idle_watch())
        await asyncio.sleep(seconds)
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(run())


def _idle_worker(conversation, monkeypatch, *, idle=0.2, grace=0.2):
    _, _, _, worker = _claude_worker(conversation)
    worker.tick, worker.idle_timeout = 0.01, idle
    monkeypatch.setattr(native_protocol, "WAKE_GRACE", grace, raising=False)
    return worker


def test_a_pending_wake_past_the_idle_timeout_holds_the_exit(conversation, monkeypatch):
    worker = _idle_worker(conversation, monkeypatch)
    worker.codec.wake_due = time.monotonic() + 0.6
    _watch(worker, 0.4)  # twice the idle timeout: today's rule would have exited
    assert not worker.closing


def test_a_wake_that_never_fires_exits_at_due_plus_grace_not_later(conversation, monkeypatch):
    """The hold doesn't refresh `last_activity`: a lapsed wake exits at due + grace, not at
    due + grace + idle_timeout (Hermes on #1927)."""
    worker = _idle_worker(conversation, monkeypatch, idle=0.3, grace=0.1)
    worker.last_activity = time.monotonic()
    worker.codec.wake_due = time.monotonic() + 0.4
    exited: list[float] = []
    real = worker.settle
    worker.settle = lambda *a, **k: (exited.append(time.monotonic()), real(*a, **k))
    start = time.monotonic()
    _watch(worker, 1.2)
    assert worker.closing and exited
    took = exited[0] - start
    assert 0.5 <= took < 0.8  # due + grace = 0.5 (+ ticks); never due + grace + idle = 0.8


def test_with_nothing_pending_the_idle_rule_is_unchanged(conversation, monkeypatch):
    worker = _idle_worker(conversation, monkeypatch)
    _watch(worker, 0.4)
    assert worker.closing


def test_a_wake_due_before_the_idle_timeout_does_not_shorten_it(conversation, monkeypatch):
    worker = _idle_worker(conversation, monkeypatch, idle=0.4, grace=0.0)
    worker.last_activity = time.monotonic()
    worker.codec.wake_due = time.monotonic() + 0.05
    _watch(worker, 0.25)
    assert not worker.closing  # max(last_activity + idle, due + grace) = the idle timeout


def test_a_full_connection_is_not_retired_under_a_pending_wake(conversation, monkeypatch):
    """A rollover retire stops the child too, and the timer with it: it waits for the wake."""
    worker = _idle_worker(conversation, monkeypatch, idle=10.0)
    worker.floor = Floor("ok", ())
    _full(worker)
    worker.codec.wake_due = time.monotonic() + 0.5
    _watch(worker, 0.2)
    assert not worker.closing
    worker.codec.wake_due = None
    _watch(worker, 0.2)
    assert worker.closing


@pytest.mark.parametrize(("before_due", "consumed"), [(6.0, False), (4.0, True)])
def test_the_early_window_is_pinned_on_both_sides(clock, before_due, consumed):
    """Hermes on #1927: a turn 6 s before the due time is not the wake's; one 4 s before is."""
    codec = ClaudeCodec(SESSION)
    _schedule(codec, 1800)
    clock[0] = 1000.0 + 1800 - before_due
    _agent_turn_starts(codec)
    assert (codec.wake_due is None) is consumed


# --- Phase 2: what the pane is told -----------------------------------------------------------


@pytest.fixture
def wall(monkeypatch):
    monkeypatch.setattr(native_protocol, "_wall", lambda: 1_800_000_000.0)


def _wakeups(events):
    return [e.data for e in events if e.kind == "wakeup"]


def _feed_result(codec, content, item="w1", error=False):
    block = {"type": "tool_result", "tool_use_id": item, "content": content}
    if error:
        block["is_error"] = True
    return codec.feed({"type": "user", "message": {"role": "user", "content": [block]}})


def test_a_scheduled_wakeup_is_reported_with_its_wall_time_and_reason(clock, wall):
    codec = ClaudeCodec(SESSION)
    _call(codec, {"delaySeconds": 600, "prompt": "go", "reason": "  watching CI  "})
    events = _feed_result(codec, "Next wakeup scheduled for 00:55:00 (in 633s).")
    (data,) = _wakeups(events)
    assert data == {"state": "scheduled", "at": 1_800_000_633, "reason": "watching CI"}
    for event in events:
        native_ipc.normalize_event({"kind": event.kind, "data": event.data})


def test_the_reason_is_bounded_and_an_empty_one_is_left_out(clock, wall):
    codec = ClaudeCodec(SESSION)
    _call(codec, {"delaySeconds": 600, "reason": "x" * 5000})
    (data,) = _wakeups(_feed_result(codec, "scheduled"))
    assert len(data["reason"]) == native_protocol.WAKE_REASON and data["reason"].endswith("…")
    _call(codec, {"delaySeconds": 600, "reason": "   "}, "w2")
    (data,) = _wakeups(_feed_result(codec, "scheduled", "w2"))
    assert "reason" not in data


def test_stop_and_the_wake_firing_report_cleared_and_a_refusal_reports_nothing(clock, wall):
    codec = ClaudeCodec(SESSION)
    _call(codec, {"delaySeconds": 900})
    assert _wakeups(_feed_result(codec, "<tool_use_error>no</tool_use_error>", error=True)) == []
    _call(codec, {"stop": True}, "w2")
    assert _wakeups(_feed_result(codec, "Loop stopped.", "w2")) == []  # nothing was pending
    _schedule(codec, 900, item="w3")
    _call(codec, {"stop": True}, "w4")
    assert _wakeups(_feed_result(codec, "Loop stopped.", "w4")) == [{"state": "cleared"}]
    _schedule(codec, 900, item="w5")
    codec._agent_turn(False)
    clock[0] += 900
    assert _wakeups(codec._agent_turn(True)) == [{"state": "cleared"}]


@pytest.mark.parametrize(
    "data",
    [
        {"state": "scheduled"},  # when?
        {"state": "cleared", "at": 5},
        {"state": "cleared", "reason": "r"},
        {"state": "pending", "at": 5},
        {"state": "scheduled", "at": 0},
        {"state": "scheduled", "at": 5, "extra": 1},
    ],
)
def test_the_wakeup_event_is_closed(data):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "wakeup", "data": data})


def test_the_snapshot_shows_only_the_live_generations_pending_wakeup(conversation):
    root, sid, binding = conversation
    _append(root, binding, "wakeup", {"state": "scheduled", "at": 1_800_000_633, "reason": "CI"})
    native = _project(root, sid, binding.worker_id)
    assert native["wakeup"] == {"at": 1_800_000_633, "reason": "CI"}
    # A successor: the earlier generation's timer ended with it.
    assert _project(root, sid, ident())["wakeup"] is None
    assert _project(root, sid, None)["wakeup"] is None
    # Fired or cancelled: gone.
    _append(root, binding, "wakeup", {"state": "cleared"})
    assert _project(root, sid, binding.worker_id)["wakeup"] is None
    # A newer generation's own report is its own.
    later = _binding(binding)
    _append(root, later, "wakeup", {"state": "scheduled", "at": 1_800_009_000})
    assert _project(root, sid, later.worker_id)["wakeup"] == {"at": 1_800_009_000}


def test_a_public_share_never_sees_the_wakeup(conversation):
    root, sid, binding = conversation
    _append(
        root, binding, "wakeup", {"state": "scheduled", "at": 1_800_000_633, "reason": "SECRETX"}
    )
    raw = native_runtime_project(root, sid, binding.worker_id)
    snap = runtime_api._snapshot(binding.session_key, raw)
    assert snap["native"]["wakeup"]["reason"] == "SECRETX"  # the operator sees it
    public = json.dumps(share_api.project(snap, {}, share_api._Redactor([]), "link"))
    assert "SECRETX" not in public and "wakeup" not in public


def test_a_pending_wakeup_is_not_in_the_stop_inventory(conversation):
    """Phase 2's Stop decision: a wake alone is no background work to confirm. Stop ends it."""
    _, _, _, worker = _claude_worker(conversation)
    before = worker.inventory()
    worker.codec.wake_due = time.monotonic() + 900
    assert worker.inventory() == before and not worker.busy()


def native_runtime_project(root, sid, live):
    from agent_sessions import native_runtime

    return native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": "claude-stream-json"},
        live,
        max_turns=50,
    )
