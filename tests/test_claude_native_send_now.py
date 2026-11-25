"""#1526: Claude's own Send now — the message written first, then a `send_now` interrupt.

A plain `interrupt` is the CLI's Stop: it ends the running turn AND every background agent. The
CLI's Send now (`interrupt_send_now_v1` on `system/init`) cuts only the running turn, for a message
it already holds. Frame orders below are the ones captured from Claude CLI 2.1.304 (#1526):

- operator turn cut: B queued → (send_now) → result A `aborted_tools` (user_message_uuid A) →
  lifecycle A cancelled → lifecycle B started → B's output → result B → B completed;
- the agent's own (task-notification) turn cut: B queued → (send_now) → result `aborted_tools`
  with no user_message_uuid → lifecycle B started → …
"""

from __future__ import annotations

import pytest

from agent_sessions.native_protocol import ClaudeCodec, ProtocolError
from test_native_protocol import (
    NEXT,
    OPERATION,
    SESSION,
    _agent_text,
    _life,
    _operator_turn,
    _owner_text,
    _result,
    _say,
)

CUT = "5adb83a9-2b1c-4d3e-8f4a-5b6c7d8e9f01"
CAPABLE = {
    "type": "system",
    "subtype": "init",
    "model": "claude-opus-5-5",
    "session_id": SESSION,
    "capabilities": ["interrupt_receipt_v1", "interrupt_cancel_queued_v1", "interrupt_send_now_v1"],
}


def _aborted(**extra):
    return _result(
        "",
        subtype="error_during_execution",
        is_error=True,
        terminal_reason="aborted_tools",
        **extra,
    )


def _task(task_id, subtype="task_started", **extra):
    return {
        "type": "system",
        "subtype": subtype,
        "task_id": task_id,
        "session_id": SESSION,
        **extra,
    }


def _proven(capable=True):
    """A generation whose fence one complete operator cycle has proved."""
    codec = ClaudeCodec(SESSION)
    if capable:
        codec.feed(CAPABLE)
    _operator_turn(codec, OPERATION, "first")
    assert codec.lifecycle_proven
    return codec


def _running(codec, operation=CUT):
    """An operator turn that has started and is working (a background agent in flight)."""
    codec.submit("work", operation)
    out = []
    for frame in (
        _life(operation, "queued"),
        _life(operation, "started"),
        _task("agent-1"),  # the subagent it launched with run_in_background
        _say("working", user_message_uuid=operation),
    ):
        out += codec.feed(frame)
    return out


def _completions(events):
    return [(e.data["operation_id"], e.data["state"]) for e in events if e.kind == "turn_completed"]


def test_the_capability_is_read_from_system_init():
    codec = ClaudeCodec(SESSION)
    assert not codec.native_send_now
    codec.feed(CAPABLE)
    assert codec.native_send_now
    # Claude repeats `system/init` at every turn; one without the field says nothing new.
    codec.feed({k: v for k, v in CAPABLE.items() if k != "capabilities"})
    assert codec.native_send_now
    codec.feed({**CAPABLE, "capabilities": ["interrupt_receipt_v1"]})
    assert not codec.native_send_now


def test_an_operator_turn_is_cut_and_the_written_message_takes_over():
    codec = _proven()
    events = _running(codec)
    assert codec.send_now_ready()
    frame = codec.submit_ahead("PONG?", NEXT)
    assert frame["type"] == "user" and frame["uuid"] == NEXT and "priority" not in frame
    assert codec.operation_id == CUT  # the cut turn stays the operation until it ends
    control = codec.send_now_interrupt(NEXT)
    assert control["request"] == {"subtype": "interrupt", "send_now": True, "message_uuid": NEXT}
    assert not codec.send_now_ready()  # one message ahead at a time
    for frame in (
        _life(NEXT, "queued"),
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": control["request_id"],
                "response": {"still_queued": [NEXT], "send_now": "stopped"},
            },
        },
        _task("bash-fg", "task_notification", status="stopped"),  # the cut turn's own tool
        _aborted(origin={"kind": "human"}, user_message_uuid=CUT),
    ):
        events += codec.feed(frame)
    assert _completions(events) == [(CUT, "interrupted")]
    assert codec.operation_id == NEXT and codec.native_turn_id == NEXT
    for frame in (
        _life(CUT, "cancelled"),
        _life(NEXT, "started"),
        _say("PONG", user_message_uuid=NEXT),
        _result("PONG", origin={"kind": "human"}, user_message_uuid=NEXT),
        _life(NEXT, "completed"),
    ):
        events += codec.feed(frame)
    assert _completions(events) == [(CUT, "interrupted"), (NEXT, "completed")]
    assert _owner_text(events) == {CUT: "working", NEXT: "PONG"}
    assert codec.lifecycle_proven and codec.operation_id is None
    assert codec._background == {"agent-1"}  # the background agent was never reported ended


def test_the_cut_turns_lifecycle_end_before_its_result_still_completes_it_once():
    codec = _proven()
    events = _running(codec)
    codec.submit_ahead("PONG?", NEXT)
    for frame in (
        _life(NEXT, "queued"),
        _life(CUT, "cancelled"),  # its end, without the result naming it
        _aborted(origin={"kind": "human"}, user_message_uuid=CUT),  # late: nobody's reply now
        _life(NEXT, "started"),
        _say("PONG", user_message_uuid=NEXT),
        _result("PONG", origin={"kind": "human"}, user_message_uuid=NEXT),
    ):
        events += codec.feed(frame)
    assert _completions(events) == [(CUT, "interrupted"), (NEXT, "completed")]
    assert _owner_text(events) == {CUT: "working", NEXT: "PONG"}


def test_the_written_message_starting_first_closes_the_cut_turn_and_distrusts_the_fence():
    codec = _proven()
    events = _running(codec)
    codec.submit_ahead("PONG?", NEXT)
    for frame in (_life(NEXT, "queued"), _life(NEXT, "started"), _say("PONG")):
        events += codec.feed(frame)
    assert _completions(events) == [(CUT, "interrupted")]
    assert codec.operation_id == NEXT and not codec.lifecycle_proven
    assert _owner_text(events) == {CUT: "working", NEXT: "PONG"}


def test_the_agents_own_turn_is_cut_for_a_plainly_submitted_message():
    codec = _proven()
    events = []
    for frame in (
        _task("agent-1"),
        _task("bash-6s", "task_notification", status="completed"),  # wakes the agent
        {"type": "system", "subtype": "status", "status": "requesting", "session_id": SESSION},
        _say("on it"),
    ):
        events += codec.feed(frame)
    assert codec.unowned_turn and codec.operation_id is None
    assert codec.send_now_ready()
    frame = codec.submit_ahead("PONG?", NEXT)  # no operator turn: it simply is the operation
    assert frame["uuid"] == NEXT and codec.operation_id == NEXT
    codec.send_now_interrupt(NEXT)
    for frame in (
        _life(NEXT, "queued"),
        _aborted(),  # the agent's own turn: no user_message_uuid (captured)
        _life(NEXT, "started"),
        _say("PONG", user_message_uuid=NEXT),
        _result("PONG", origin={"kind": "human"}, user_message_uuid=NEXT),
        _life(NEXT, "completed"),
    ):
        events += codec.feed(frame)
    assert not codec.unowned_turn
    assert list(_agent_text(events).values()) == ["on it"]
    assert _owner_text(events) == {NEXT: "PONG"}
    assert _completions(events) == [(NEXT, "completed")]
    assert codec.lifecycle_proven and codec._background == {"agent-1"}


def test_a_written_message_the_cli_drops_completes_as_failed_and_the_turn_runs_on():
    codec = _proven()
    events = _running(codec)
    codec.submit_ahead("PONG?", NEXT)
    events += codec.feed(_life(NEXT, "queued")) + codec.feed(_life(NEXT, "cancelled"))
    assert _completions(events) == [(NEXT, "failed")]
    assert codec.operation_id == CUT and codec._ahead is None
    events += codec.feed(_say(" more", user_message_uuid=CUT))
    events += codec.feed(_result("done", origin={"kind": "human"}, user_message_uuid=CUT))
    assert _completions(events) == [(NEXT, "failed"), (CUT, "completed")]
    assert codec.operation_id is None


@pytest.mark.parametrize("outcome", ["nothing_waiting", "delivering"])
def test_a_send_now_that_stops_nothing_leaves_the_message_next(outcome):
    codec = _proven()
    events = _running(codec)
    codec.submit_ahead("PONG?", NEXT)
    control = codec.send_now_interrupt(NEXT)
    response = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": control["request_id"],
            "response": {"still_queued": [NEXT], "send_now": outcome},
        },
    }
    assert [e.kind for e in codec.feed(response)] == ["response"]
    assert codec.operation_id == CUT and codec._ahead == NEXT
    for frame in (
        _life(NEXT, "queued"),
        _say(" finished", user_message_uuid=CUT),
        _result("finished", origin={"kind": "human"}, user_message_uuid=CUT),
        _life(CUT, "completed"),
        _life(NEXT, "started"),
        _say("PONG", user_message_uuid=NEXT),
    ):
        events += codec.feed(frame)
    assert _completions(events) == [(CUT, "completed")]
    assert _owner_text(events) == {CUT: "working finished", NEXT: "PONG"}


def test_the_first_turn_of_a_generation_may_send_now_once_it_has_started():
    codec = ClaudeCodec(SESSION)
    codec.feed(CAPABLE)
    codec.submit("work", CUT)
    assert not codec.send_now_ready()  # written: nothing seen yet
    codec.feed(_life(CUT, "queued"))
    assert not codec.send_now_ready()
    codec.feed(_life(CUT, "started"))
    assert not codec.lifecycle_proven and codec.send_now_ready()
    events = codec.feed(_say("working"))
    codec.submit_ahead("PONG?", NEXT)
    for frame in (
        _life(NEXT, "queued"),
        _aborted(origin={"kind": "human"}, user_message_uuid=CUT),
        _life(CUT, "cancelled"),  # the cut turn's clean end proves the fence
        _life(NEXT, "started"),
        _say("PONG", user_message_uuid=NEXT),
    ):
        events += codec.feed(frame)
    assert codec.lifecycle_proven
    assert _owner_text(events) == {CUT: "working", NEXT: "PONG"}


def test_send_now_is_not_ready_without_the_capability_or_with_a_lost_fence():
    codec = _proven(capable=False)
    _running(codec)
    assert not codec.send_now_ready()
    with pytest.raises(ProtocolError):
        codec.submit_ahead("PONG?", NEXT)
    assert codec.send_now_would_stop_background()  # a Stop would end agent-1

    tainted = _proven()
    tainted.submit("work", CUT)
    tainted.feed(_life(CUT, "queued"))
    tainted.feed(_life("9d1f0b8e-1c2d-4e3f-8a9b-0c1d2e3f4a5b", "started"))  # an anomaly
    assert not tainted.send_now_ready()


def test_without_background_work_the_fallback_stops_nothing():
    codec = _proven(capable=False)
    codec.submit("work", CUT)
    codec.feed(_life(CUT, "queued"))
    codec.feed(_life(CUT, "started"))
    assert not codec.send_now_would_stop_background()
    codec.feed(_task("agent-1"))
    assert codec.send_now_would_stop_background()
    codec.feed(_task("agent-1", "task_notification", status="completed"))
    assert not codec.send_now_would_stop_background()


def test_send_now_names_only_a_written_message():
    codec = _proven()
    _running(codec)
    with pytest.raises(ProtocolError):
        codec.send_now_interrupt(NEXT)  # not written
    with pytest.raises(ProtocolError):
        codec.submit_ahead("again", CUT)  # its own turn
    codec.submit_ahead("PONG?", NEXT)
    with pytest.raises(ProtocolError):
        codec.submit_ahead("third", "6f1e2d3c-4b5a-4968-8776-655443322110")


def test_stop_stays_a_plain_interrupt():
    codec = _proven()
    _running(codec)
    assert codec.interrupt()["request"] == {"subtype": "interrupt"}


def test_a_fresh_generation_knows_only_the_tasks_its_cli_reports():
    """The narrowed contract (#1526, Hermes): without the CLI's own Send now, the fallback Stop is
    refused only for background work THIS generation's CLI has reported. A fresh process that has
    reported none may be interrupted as before; a capable CLI never needs the fallback."""
    codec = ClaudeCodec(SESSION)  # a replacement generation: nothing seen yet
    codec.submit("work", CUT)
    codec.feed(_life(CUT, "queued"))
    codec.feed(_life(CUT, "started"))
    assert not codec.send_now_ready() and not codec.send_now_would_stop_background()
    codec.feed(CAPABLE)
    assert codec.send_now_ready()


@pytest.mark.parametrize("promotion", ["result-first", "lifecycle-first"])
@pytest.mark.parametrize("drop", ["cancelled", "discarded"])
def test_a_promoted_message_dropped_before_it_starts_settles_failed(promotion, drop):
    """Hermes on #1528: once the cut turn has ended the written message IS the operation; the CLI
    dropping it before its `started` must still complete it, never leave the queue waiting."""
    codec = _proven()
    events = _running(codec)
    codec.submit_ahead("PONG?", NEXT)
    events += codec.feed(_life(NEXT, "queued"))
    ends = [_aborted(origin={"kind": "human"}, user_message_uuid=CUT), _life(CUT, "cancelled")]
    for frame in ends if promotion == "result-first" else ends[::-1]:
        events += codec.feed(frame)
    assert codec.operation_id == NEXT
    events += codec.feed(_life(NEXT, drop))
    assert _completions(events) == [(CUT, "interrupted"), (NEXT, "failed")]
    assert codec.operation_id is None and codec._cycle is None
    # The connection is free for the next message.
    codec.submit("again", "6f1e2d3c-4b5a-4968-8776-655443322110")
