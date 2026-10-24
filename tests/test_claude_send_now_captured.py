"""Send now and agent-run turns against frames captured from the real CLI (#1644).

See `fixtures/claude_send_now_captured.PROVENANCE.md`. Each fixture is replayed through
`ClaudeCodec` exactly as the worker drives it: a message written while nothing of the operator's
runs is `submit`, one written while a turn runs is `submit_ahead` (Send now), and the captured
`interrupt send_now` is `send_now_interrupt`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from agent_sessions import native_worker
from agent_sessions.native_protocol import ClaudeCodec, NativeEvent

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    return rows


def _session(rows: list[dict]) -> str:
    return next(r["frame"]["session_id"] for r in rows if r["frame"].get("session_id"))


def _replay(name: str, watch=None):
    """Feed one capture. ``watch(codec, row)`` runs before each client write. Returns the codec,
    every event, and the uuids of the client's messages in order."""
    rows = _load(name)
    codec = ClaudeCodec(_session(rows))
    codec._next = lambda action: "req"  # request ids are the worker's business
    events: list[NativeEvent] = []
    messages = []
    for row in rows:
        frame = row["frame"]
        if row["dir"] == "out":
            if watch is not None:
                watch(codec, row)
            if frame["type"] == "user":
                text, message = frame["message"]["content"], frame["uuid"]
                messages.append(message)
                if codec.operation_id is None and not codec.unowned_turn:
                    codec.submit(text, message)
                else:
                    codec.submit_ahead(text, message)
            elif frame["request"].get("send_now"):
                codec.send_now_interrupt(frame["request"]["message_uuid"])
            continue
        events += codec.feed(frame)
    return codec, events, messages


def _completed(events):
    return {
        e.data["operation_id"]: e.data
        for e in events
        if e.kind == "turn_completed" and e.data.get("operation_id")
    }


def test_a_message_taken_into_an_agent_turn_completes_and_keeps_the_fence():
    """The root cause: `completed` before the result that names the message (captured)."""
    distrusted = []
    codec, events, messages = _replay(
        "claude_message_taken_into_agent_turn",
        watch=lambda c, row: row["frame"]["type"] == "user"
        and distrusted.append(not c.lifecycle_proven),
    )
    first, taken, last = messages
    done = _completed(events)
    assert done[taken]["state"] == "completed"
    assert done[taken]["error"] == ""
    assert "continue-ack" in done[taken]["text"]
    assert all(d["state"] == "completed" for d in done.values())
    # Proved by the first clean cycle and never lost: the last message went in on a proven fence.
    assert distrusted == [True, False, False]
    assert codec.lifecycle_proven and not codec._tainted


@pytest.mark.parametrize(
    "name", ["claude_send_now_agent_turn_bash", "claude_send_now_agent_turn_monitor"]
)
def test_send_now_cuts_an_agent_turn_without_stopping_background_tasks(name):
    seen = {}

    def watch(codec, row):
        if row["frame"]["type"] != "user":
            return
        if codec.unowned_turn:
            # The operator writes while the agent runs a turn of its own, tasks running.
            seen["ready"] = codec.send_now_ready()
            seen["would_stop"] = codec.send_now_would_stop_background()
            seen["tasks"] = set(codec._background)
        elif "tasks" in seen:
            seen["after"] = set(codec._background)  # the next message, after the cut

    codec, events, messages = _replay(name, watch=watch)
    assert seen["tasks"], "the capture has background work running at Send now"
    assert seen["ready"] and not seen["would_stop"]
    asked = messages[1]
    assert _completed(events)[asked]["state"] == "completed"
    # The cut stopped only the turn's own foreground command: the background task still runs.
    assert seen["after"] and seen["after"] <= seen["tasks"]
    assert codec.lifecycle_proven


def test_a_notification_inside_an_operator_turn_never_distrusts():
    codec, events, messages = _replay("claude_notification_inside_operator_turn")
    assert all(d["state"] == "completed" for d in _completed(events).values())
    assert len(_completed(events)) == len(messages)
    assert codec.lifecycle_proven


# --- the wait after `completed` stays fail-closed ---------------------------------------------

SESSION = "b6bf1a94-f584-4d7a-b2a2-850de2ab17f5"
OPERATION = "f84b4fa9-d57b-45c1-a219-26dc19b7f661"


def _ended(proven: bool = True) -> ClaudeCodec:
    codec = ClaudeCodec(SESSION)
    if proven:
        codec.lifecycle_proven = True
    codec.submit("go", OPERATION)
    for state in ("queued", "started", "completed"):
        assert codec.feed(_life(state)) == []
    return codec


def _life(state, command=OPERATION):
    return {
        "type": "command_lifecycle",
        "command_uuid": command,
        "state": state,
        "uuid": str(uuid.uuid4()),
        "session_id": SESSION,
    }


def _result(**extra):
    return {
        "type": "result",
        "subtype": "success",
        "uuid": str(uuid.uuid4()),
        "is_error": False,
        "result": "done",
        "session_id": SESSION,
        **extra,
    }


@pytest.mark.parametrize("origin", [{"kind": "human"}, {"kind": "task-notification"}])
def test_the_result_naming_an_ended_input_completes_it(origin):
    codec = _ended()
    (done,) = codec.feed(_result(origin=origin, user_message_uuid=OPERATION))
    assert done.kind == "turn_completed" and done.data["operation_id"] == OPERATION
    assert done.data["state"] == "completed" and done.data["text"] == "done"
    assert codec.operation_id is None and codec.lifecycle_proven


def test_an_ended_input_in_an_unproven_generation_proves_the_fence():
    codec = _ended(proven=False)
    codec.feed(_result(origin={"kind": "task-notification"}, user_message_uuid=OPERATION))
    assert codec.lifecycle_proven


@pytest.mark.parametrize(
    "frame",
    [
        lambda: {
            "type": "assistant",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "message": {"id": "msg_1", "content": [{"type": "text", "text": "later"}]},
        },
        lambda: _result(origin={"kind": "human"}, user_message_uuid=str(uuid.uuid4())),
        lambda: _result(origin={"kind": "task-notification"}),
        lambda: _life("started", str(uuid.uuid4())),
        lambda: {"type": "system", "subtype": "status", "status": "requesting"},
    ],
    ids=["output", "foreign-result", "unbound-result", "stray-lifecycle", "new-request"],
)
def test_anything_but_its_result_ends_the_input_without_it(frame):
    codec = _ended()
    events = codec.feed(frame())
    failed = [e for e in events if e.kind == "turn_completed"]
    assert failed[0].data["operation_id"] == OPERATION
    assert failed[0].data["state"] == "failed"
    assert failed[0].data["error"] == "the agent ended this turn without its result"
    assert not codec.lifecycle_proven and codec.operation_id is None


def test_task_bookkeeping_between_completed_and_its_result_keeps_the_wait():
    codec = _ended()
    for frame in (
        {"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "stopped"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
        {"type": "rate_limit_event"},
    ):
        assert all(e.kind != "turn_completed" for e in codec.feed(frame))
    (done,) = [
        e
        for e in codec.feed(_result(origin={"kind": "human"}, user_message_uuid=OPERATION))
        if e.kind == "turn_completed"
    ]
    assert done.data["state"] == "completed"


def test_a_dropped_frame_ends_a_waiting_input():
    codec = _ended()
    (failed,) = codec.frame_lost()
    assert failed.data["operation_id"] == OPERATION and failed.data["state"] == "failed"
    assert codec.frame_lost() == []  # nothing waits any more


def test_cancelled_still_ends_the_input_at_once():
    codec = ClaudeCodec(SESSION)
    codec.submit("go", OPERATION)
    codec.feed(_life("queued"))
    codec.feed(_life("started"))
    (done,) = codec.feed(_life("cancelled"))
    assert done.data["state"] == "interrupted"


# --- the worker tells the codec when a frame was dropped ---------------------------------------


class _Stub:
    """Just what `NativeWorker.lines` / `reader` touch."""

    def __init__(self, data: bytes, codec):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        self.proc = type("P", (), {"stdout": reader})()
        self.codec = codec
        self.handled: list = []
        self.recorded: list = []
        self.waiters: dict = {}
        self.done = asyncio.Event()

    def dropped_frame(self):
        self.recorded.append("dropped")

    def record(self, events):
        self.recorded.extend(events)

    async def handle_events(self, events):
        self.handled.extend(events)

    def stop_child(self):
        pass

    def lines(self):
        return native_worker.Worker.lines(self)


def test_a_dropped_frame_reaches_the_codec(monkeypatch):
    codec = _ended()
    monkeypatch.setattr(native_worker.native_protocol, "MAX_FRAME_BYTES", 64)
    big = b'{"type":"result","pad":"' + b"x" * 200 + b'"}\n'

    async def run():
        stub = _Stub(big, codec)  # its StreamReader needs the running loop
        await native_worker.Worker.reader(stub)
        return stub

    stub = asyncio.run(run())
    assert "dropped" in stub.recorded
    (failed,) = [e for e in stub.handled if e.kind == "turn_completed"]
    assert failed.data["operation_id"] == OPERATION and failed.data["state"] == "failed"
