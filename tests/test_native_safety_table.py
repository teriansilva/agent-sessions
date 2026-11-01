"""The one safety table (#1725): what may end an API session's generation, and what only costs it
a row. `docs/invariants/native-runtime.md` holds the table; this file holds a test per row, and a
ratchet: a new `raise ProtocolError(...)` or a new row fails here until it is classified.

Safety rows keep ending the generation: continuing could lose, repeat or misattribute an effect.
Capacity and observation rows degrade: nothing effect-bearing can be lost, so they cost a row, or a
refusal the agent reads, and the connection lives.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import json
import re
import tracemalloc
import uuid
from pathlib import Path

import pytest

from agent_sessions import native_protocol, native_worker
from agent_sessions.native_protocol import (
    FRAME_SKIPPED,
    LOST_REQUEST,
    LOST_RESULT,
    MAX_FRAME_BYTES,
    MAX_PENDING,
    MAX_RESULTS,
    ClaudeCodec,
    CodexCodec,
    CopilotAcpCodec,
    OpencodeAcpCodec,
    ProtocolError,
    decode,
)

DOC = Path(__file__).parent.parent / "docs" / "invariants" / "native-runtime.md"
SOURCE = Path(native_protocol.__file__)
SESSION = "b6bf1a94-f584-4d7a-b2a2-850de2ab17f5"
OPERATION = "f84b4fa9-d57b-45c1-a219-26dc19b7f661"
TURN = "0199f3ac-53fb-7c10-81ef-ce7c2e732b28"
OPENCODE_SESSION = "ses_abc123"
AGENT = "battlelab-api-0123456789abcdef"


# --- the clients, each with a turn running -------------------------------------------------


def claude():
    codec = ClaudeCodec(SESSION)
    codec.generation = str(uuid.uuid4())
    codec.submit("Inspect", OPERATION)
    return codec


def codex():
    codec = CodexCodec()
    codec.generation = str(uuid.uuid4())
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    frame = codec.submit("Inspect", OPERATION)
    codec.feed({"id": frame["id"], "result": {"turn": {"id": TURN, "status": "inProgress"}}})
    return codec


def _acp(codec, session):
    frame = codec.load(session, "/tmp")
    codec.feed({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
    pinned = codec.pin_mode()
    mode = codec.agent if isinstance(codec, OpencodeAcpCodec) else native_protocol.COPILOT_MODE
    codec.feed(
        {
            "jsonrpc": "2.0",
            "id": pinned["id"],
            "result": {"configOptions": [{"id": "mode", "currentValue": mode}]},
        }
    )
    return codec


def opencode(*, running=True):
    codec = _acp(OpencodeAcpCodec(None, agent=AGENT), OPENCODE_SESSION)
    if running:
        codec.submit("Inspect", OPERATION)
    return codec


def copilot(*, running=True):
    codec = _acp(CopilotAcpCodec(None), SESSION)
    if running:
        codec.submit("Inspect", OPERATION)
    return codec


FACTORIES = {"claude": claude, "codex": codex, "opencode": opencode, "copilot": copilot}


def claude_request(**extra):
    return {
        "type": "control_request",
        "request_id": "native-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "input": {"command": "ls"},
            **extra,
        },
    }


def codex_request(request_id=2, **extra):
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "itemId": f"cmd-{request_id}",
            "command": "ls",
            **extra,
        },
    }


def acp_request(request_id=2, session=None, **extra):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": session,
            "toolCall": {
                "toolCallId": f"call-{request_id}",
                "kind": "execute",
                "title": "ls",
                "rawInput": {"command": "ls"},
                **extra,
            },
            "options": [
                {"optionId": "once", "kind": "allow_once"},
                {"optionId": "no", "kind": "reject_once"},
            ],
        },
    }


def claude_result(**extra):
    return {
        "type": "result",
        "subtype": "success",
        "uuid": str(uuid.uuid4()),
        "is_error": False,
        "result": "done",
        **extra,
    }


def task(subtype, task_id, **fields):
    return {"type": "system", "subtype": subtype, "task_id": task_id, **fields}


def task_list(*ids):
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [{"task_id": i, "task_type": "local_agent", "description": i} for i in ids],
    }


def kinds(events):
    return [event.kind for event in events]


# --- safety rows: each ends the generation, as a `ProtocolError` out of `feed` -------------------


def _session_identity(_):
    claude().feed({"type": "system", "session_id": str(uuid.uuid4())})


def _thread_identity(_):
    codec = CodexCodec()
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    codec.native_id = "another"
    frame = codec._request("thread/read", {"threadId": "another"})
    codec._expected_sessions.pop(frame["id"], None)
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})


def _requested_thread(_):
    codec = CodexCodec()
    frame = codec.read("thread-a")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": "thread-b"}}})


def _acp_identity(make, session):
    codec = make(running=False)
    frame = codec.load(session, "/tmp")
    codec.native_id = "ses_other" if session.startswith("ses_") else str(uuid.uuid4())
    codec.feed({"jsonrpc": "2.0", "id": frame["id"], "result": {}})


def _mode_left(make):
    codec = make(running=False)
    codec.feed(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": codec.native_id,
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "build"},
            },
        }
    )


def _result_identity(_):
    codec = claude()
    first = claude_result(origin={"kind": "task-notification"})
    codec.feed(first)
    codec.feed({**first, "result": "changed"})


def _request_identity(make, frame, changed):
    codec = make()
    codec.feed(frame)
    codec.feed(changed)


def _request_replayed(make, frame, decide):
    codec = make()
    codec.feed(frame)
    key = next(iter(codec._approvals))
    decide(codec, key)
    codec.feed(frame)


def _malformed_line(line):
    decode(line)


def _early_buffer(_):
    codec = CodexCodec()
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    codec.submit("Inspect", OPERATION)
    frame = {
        "method": "item/completed",
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "item": {"id": "x", "type": "agentMessage", "text": "x" * 200_000},
        },
    }
    for _ in range(MAX_PENDING):
        codec.feed(frame)


def _turn_status(_):
    codex().feed(
        {
            "method": "turn/completed",
            "params": {"threadId": SESSION, "turn": {"id": TURN, "status": "inProgress"}},
        }
    )


def _steer_mismatch(_):
    codec = codex()
    frame = codec.steer("more", str(uuid.uuid4()))
    codec.feed({"id": frame["id"], "result": {"turnId": "another"}})


#: Row id → how to produce it, and the message it ends with.
SAFETY_ROWS = {
    "safety-session-identity": [
        (_session_identity, None, "native session identity changed"),
        (_thread_identity, None, "native thread identity changed"),
        (_requested_thread, None, "does not match the requested thread"),
        (lambda _: _acp_identity(opencode, OPENCODE_SESSION), None, "identity changed"),
        (lambda _: _acp_identity(copilot, SESSION), None, "identity changed"),
    ],
    "safety-pinned-mode": [
        (lambda _: _mode_left(opencode), None, "left its pinned mode"),
        (lambda _: _mode_left(copilot), None, "left its pinned mode"),
    ],
    "safety-result-identity": [(_result_identity, None, "native result identity changed")],
    "safety-request-identity": [
        (
            lambda _: _request_identity(
                claude, claude_request(), claude_request(input={"command": "rm"})
            ),
            None,
            "request identity changed",
        ),
    ],
    "safety-request-replayed": [
        (
            lambda _: _request_replayed(
                claude, claude_request(), lambda c, k: c.decide(k, "reject")
            ),
            None,
            "already consumed",
        ),
    ],
    "safety-turn-outcome": [(_turn_status, None, "no terminal turn status")],
    "safety-steer-mismatch": [(_steer_mismatch, None, "does not match its turn")],
    "safety-early-buffer": [(_early_buffer, None, "event buffer exceeded")],
    "safety-malformed-frame": [
        (_malformed_line, b'{"a":1,"a":2}\n', "invalid native JSON frame"),
        (_malformed_line, b"{not json\n", "invalid native JSON frame"),
        (_malformed_line, b"[1]\n", "invalid native JSON frame"),
        (_malformed_line, b'{"a":1}\r\n', "exactly one JSONL record"),
        (lambda _: claude().feed({"type": 1}), None, "kind must be a string"),
        (lambda _: claude().feed({"type": "result"}), None, "canonical UUID"),
        (
            lambda _: claude().feed({"type": "control_request", "request_id": "r", "request": 1}),
            None,
            "control request must be an object",
        ),
        (
            lambda _: claude().feed(claude_request(input=1)),
            None,
            "input must be an object",
        ),
        (lambda _: codex().feed({"method": 1}), None, "method must be a string"),
        (lambda _: opencode().feed({"id": 1}), None, "not JSON-RPC"),
        (lambda _: copilot().feed({"id": 1}), None, "not JSON-RPC"),
    ],
}


@pytest.mark.parametrize(
    ("row", "produce", "argument", "message"),
    [
        (row, produce, argument, message)
        for row, cases in SAFETY_ROWS.items()
        for produce, argument, message in cases
    ],
)
def test_a_safety_row_ends_the_generation(row, produce, argument, message):
    with pytest.raises(ProtocolError, match=message):
        produce(argument)


# --- worker rows are pinned where the worker runs ------------------------------------------------

#: Row id → the test that pins it (real worker against a scripted native, or in process). Listed
#: so the ratchet below keeps table and tests in step.
WORKER_ROWS = {
    "worker-write-failed": "test_native_capacity.py::test_a_write_the_agent_never_took_ends_it",
    "worker-journal-unwritable": "test_native_journal_unwritable.py",
    "worker-dispatch-failed": "test_native_capacity.py::test_a_queue_failure_ends_the_generation",
    "worker-native-eof": "test_native_oom.py",
    "worker-init-refused": "test_native_runtime.py",
    "worker-stop": "test_native_liveness.py",
    "worker-idle-exit": "test_native_liveness.py",
    "worker-oom": "test_native_oom.py",
    "worker-rollover": "test_native_capacity.py::test_a_full_connection_rolls_over_and_resumes",
}


def test_the_worker_rows_name_tests_that_exist():
    tests = Path(__file__).parent
    for pinned in WORKER_ROWS.values():
        path, _, name = pinned.partition("::")
        text = (tests / path).read_text()
        assert not name or f"def {name}(" in text, pinned


# --- capacity and observation rows: each degrades, with the connection alive --------------------


def test_capacity_result_ledger_never_ends_a_turn():
    """Past the bound nothing more is retained and nothing is raised."""
    codec = claude()
    codec._results = {str(i): "x" for i in range(MAX_RESULTS)}
    assert codec.at_capacity()
    assert kinds(codec.feed(claude_result(parent_tool_use_id="t"))) == ["background"]
    assert len(codec._results) == MAX_RESULTS
    # Only the result naming the turn can complete it now: a repeat of what goes unrecorded would
    # not be caught, so an unnamed one is activity. The turn is still running.
    assert kinds(codec.feed(claude_result(origin={"kind": "human"}))) == ["background"]
    assert codec.operation_id == OPERATION
    (done,) = codec.feed(claude_result(origin={"kind": "human"}, user_message_uuid=OPERATION))
    assert done.kind == "turn_completed" and done.data["state"] == "completed"


def test_capacity_result_ledger_high_water_mark_precedes_the_bound():
    codec = claude()
    codec._results = {str(i): "x" for i in range(MAX_RESULTS - native_protocol.RESULTS_RESERVE - 1)}
    assert not codec.at_capacity()
    codec.feed(claude_result(parent_tool_use_id="t"))
    assert codec.at_capacity()
    # What is already admitted still goes: the codec refuses only at the hard bound.
    codec.feed(claude_result(origin={"kind": "human"}, user_message_uuid=OPERATION))
    assert codec.submit("queued before the mark", str(uuid.uuid4()))["type"] == "user"


def test_capacity_background_tasks_past_the_bound_stay_busy_and_say_so():
    codec = ClaudeCodec(SESSION)
    for n in range(MAX_PENDING):
        codec.feed(task("task_started", f"t{n}"))
    assert not codec.overflow
    (event,) = codec.feed(task("task_started", "one-too-many"))
    # No task id: the fold never holds more than the tracked set.
    assert event.kind == "background" and event.data["active"] and "task_id" not in event.data
    assert codec.overflow and "one-too-many" not in codec._background
    assert len(codec._background) == MAX_PENDING


def test_capacity_background_list_past_the_bound_keeps_the_tracked_tasks():
    codec = ClaudeCodec(SESSION)
    codec.feed(task("task_started", "zz-tracked"))
    codec.feed(task_list("zz-tracked", *[f"t{n:03}" for n in range(200)]))
    assert codec.overflow and "zz-tracked" in codec._background
    assert len(codec._background) == MAX_PENDING
    # A whole list within the bound settles it: the client said what runs.
    events = codec.feed(task_list("zz-tracked"))
    assert not codec.overflow and codec._background == {"zz-tracked"}
    assert sum(e.data.get("task_state") == "ended" for e in events) == MAX_PENDING - 1


def test_capacity_codex_terminals_past_the_bound_are_not_tracked_and_say_so():
    codec = codex()
    for n in range(MAX_PENDING + 1):
        codec.feed(
            {
                "method": "item/started",
                "params": {
                    "threadId": SESSION,
                    "turnId": TURN,
                    "item": {
                        "id": f"cmd-{n}",
                        "type": "commandExecution",
                        "status": "inProgress",
                        "processId": n + 1,
                        "command": "sleep 9",
                    },
                },
            }
        )
    assert codec.overflow and len(codec._commands) == MAX_PENDING


def test_capacity_early_buffer_drops_only_a_streamed_piece():
    codec = CodexCodec()
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    codec.submit("Inspect", OPERATION)
    delta = {
        "method": "item/agentMessage/delta",
        "params": {"threadId": SESSION, "turnId": TURN, "itemId": "m", "delta": "x" * 300_000},
    }
    for _ in range(10):
        assert codec.feed(delta) == []
    assert codec._early_size <= MAX_FRAME_BYTES
    # Its message's `item/completed` repeats the whole text, and a request is never dropped: the
    # pieces give way for it.
    held = len(codec._early_frames)
    assert codec.feed(codex_request(command="x" * 500_000)) == []
    assert codec._early_frames[-1]["id"] == 2 and len(codec._early_frames) <= held


@pytest.mark.parametrize("deltas", [200, MAX_PENDING, MAX_PENDING + 1])
def test_capacity_early_buffer_many_small_pieces_never_take_a_frame_that_matters(deltas):
    """A streamed piece gives way: nothing it can lose is worth the turn's end or a request."""
    codec = CodexCodec()
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    submitted = codec.submit("Inspect", OPERATION)
    delta = {
        "method": "item/agentMessage/delta",
        "params": {"threadId": SESSION, "turnId": TURN, "itemId": "m", "delta": "x"},
    }
    for _ in range(deltas):
        assert codec.feed(delta) == []
    done = {
        "method": "item/completed",
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "item": {"id": "m", "type": "agentMessage", "text": "whole"},
        },
    }
    assert codec.feed(done) == []
    assert codec.feed(codex_request(3)) == []
    assert len(codec._early_frames) <= MAX_PENDING
    assert codec._early_size == sum(len(native_protocol.encode(f)) for f in codec._early_frames)
    # What mattered is delivered once the turn is known, in order.
    out = codec.feed(
        {"id": submitted["id"], "result": {"turn": {"id": TURN, "status": "inProgress"}}}
    )
    kinds_out = kinds(out)
    assert "approval" in kinds_out
    assert any(e.kind == "text" and e.data["text"] == "whole" for e in out)


def test_capacity_file_changes_past_the_bound_are_not_retained():
    codec = codex()
    for n in range(MAX_PENDING + 1):
        codec.feed(
            {
                "method": "item/started",
                "params": {
                    "threadId": SESSION,
                    "turnId": TURN,
                    "item": {"id": f"edit-{n}", "type": "fileChange", "changes": []},
                },
            }
        )
    assert len(codec._patches) == MAX_PENDING


def test_capacity_mcp_calls_past_the_bound_are_not_retained():
    codec = codex()
    for n in range(MAX_PENDING + 1):
        codec.feed(
            {
                "method": "item/started",
                "params": {
                    "threadId": SESSION,
                    "turnId": TURN,
                    "item": {
                        "id": f"mcp-{n}",
                        "type": "mcpToolCall",
                        "server": "s",
                        "tool": "t",
                        "arguments": {"n": n},
                    },
                },
            }
        )
    assert len(codec._mcp_calls) == MAX_PENDING


def _open_requests(name):
    codec = FACTORIES[name]()
    if name == "claude":
        frames = [{**claude_request(), "request_id": f"r{n}"} for n in range(MAX_PENDING + 1)]
    elif name == "codex":
        frames = [codex_request(n + 1) for n in range(MAX_PENDING + 1)]
    else:
        frames = [acp_request(n + 1, codec.native_id) for n in range(MAX_PENDING + 1)]
    results = [codec.feed(frame) for frame in frames]
    return codec, results


@pytest.mark.parametrize("name", list(FACTORIES))
def test_capacity_more_open_requests_than_can_be_shown_are_refused(name):
    codec, results = _open_requests(name)
    assert len(codec._approvals) == MAX_PENDING
    assert kinds(results[-1]) == ["tool", "send"]  # a row and a refusal, the connection alive
    assert results[-1][0].data["state"] == "skipped"
    assert not any(e.kind == "approval" for e in results[-1])


def test_capacity_answered_requests_never_fill_up():
    """A long guarded turn answers more than MAX_PENDING: that used to end the generation."""
    codec = claude()
    for n in range(MAX_PENDING * 2):
        codec.feed({**claude_request(), "request_id": f"r{n}"})
        codec.decide(f"s:r{n}", "reject")
    assert not codec._approvals and len(codec._consumed) == MAX_PENDING * 2


def test_capacity_answered_requests_forget_the_oldest_past_their_bound():
    codec = claude()
    for n in range(native_protocol.MAX_CONSUMED + 1):
        codec._consumed.add(f"s:r{n}")
    assert len(codec._consumed) == native_protocol.MAX_CONSUMED
    assert "s:r0" not in codec._consumed and "s:r1" in codec._consumed


@pytest.mark.parametrize(
    ("make", "frame"),
    [
        (claude, claude_request(input={"command": "x" * native_protocol.MAX_TEXT})),
        (codex, codex_request(command="x" * native_protocol.MAX_TEXT)),
    ],
)
def test_capacity_a_request_too_large_to_present_is_refused(make, frame):
    codec = make()
    assert kinds(codec.feed(frame)) == ["tool", "send"]
    assert not codec._approvals and codec.operation_id == OPERATION


@pytest.mark.parametrize(
    ("make", "frame"),
    [
        (claude, lambda text: claude_request(agent_id="t", input={"command": text})),
    ],
)
def test_capacity_a_background_request_too_large_with_its_origin_is_refused(make, frame):
    """The frame alone fits; with the origin the operator must see, it can't be shown whole. Never
    a cut summary presented as complete."""
    codec = make()
    description = "d" * 1000
    codec.feed(task("task_started", "t", description=description, is_backgrounded=True))
    # As long as the frame can be while still under the bound on its own.
    text = "x" * (native_protocol.MAX_TEXT - 300)
    built = frame(text)
    assert not native_protocol._oversized(built)
    assert kinds(codec.feed(built)) == ["tool", "send"]
    assert not codec._approvals


def test_capacity_a_codex_agent_turns_request_too_large_with_its_origin_is_refused():
    codec = CodexCodec()
    codec.generation = str(uuid.uuid4())
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    agent = "agent-turn"
    codec.feed(
        {
            "method": "turn/started",
            "params": {"threadId": SESSION, "turn": {"id": agent, "status": "inProgress"}},
        }
    )
    built = codex_request(7, command="")
    built["params"]["turnId"] = agent
    # Exactly at the frame's own bound: it fits by itself, and not with the origin beside it.
    length = len(json.dumps(built, ensure_ascii=True, separators=(",", ":")))
    built["params"]["command"] = "x" * (native_protocol.MAX_TEXT - length)
    assert not native_protocol._oversized(built)
    assert kinds(codec.feed(built)) == ["tool", "send"]
    assert not codec._approvals


@pytest.mark.parametrize("name", ["opencode", "copilot"])
def test_capacity_an_acp_request_too_large_stays_decline_only(name):
    """Already so (#1312, #1565): shown in outline, and it can only be refused."""
    codec = FACTORIES[name]()
    frame = acp_request(2, codec.native_id, rawInput={"command": "x" * native_protocol.MAX_TEXT})
    (event,) = codec.feed(frame)
    assert event.kind == "approval" and event.data["choices"] == ["reject"]


def test_effect_an_unknown_request_is_refused_and_ends_nothing():
    (claude_refusal,) = claude().feed(
        {
            "type": "control_request",
            "request_id": "r",
            "request": {"subtype": "hook_callback"},
        }
    )
    assert claude_refusal.data["frame"]["response"]["subtype"] == "error"
    (codex_refusal,) = codex().feed({"id": 9, "method": "item/tool/call", "params": {}})
    assert codex_refusal.data["frame"]["error"]["code"] == -32601
    for name in ("opencode", "copilot"):
        (refusal,) = FACTORIES[name]().feed(
            {"jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file", "params": {}}
        )
        assert refusal.data["frame"]["error"]["code"] == -32601


# --- the oversized frame, per client ------------------------------------------------------------


OUTPUT_PREFIX = {
    "claude": b'{"type":"user","message":{"content":"xxxx',
    "codex": b'{"method":"item/completed","params":{"threadId":"t","item":{"aggregatedOutput":"xx',
    "opencode": b'{"jsonrpc":"2.0","method":"session/update","params":{"update":{"content":"xxx',
    "copilot": b'{"jsonrpc":"2.0","method":"session/update","params":{"update":{"content":"xxx',
}
RESULT_PREFIX = {
    "claude": b'{"type":"result","subtype":"success","uuid":"u","result":"xxxx',
    "codex": b'{"method":"turn/completed","params":{"threadId":"t","turn":{"id":"x","items":[',
}
REQUEST_PREFIX = {
    "claude": b'{"type":"control_request","request_id":"big-1","request":{"input":{"cmd":"xxx',
    "codex": b'{"id":77,"method":"item/commandExecution/requestApproval","params":{"cmd":"xxxx',
    "opencode": b'{"jsonrpc":"2.0","id":77,"method":"session/request_permission","params":{"a":"',
    "copilot": b'{"jsonrpc":"2.0","id":77,"method":"session/request_permission","params":{"a":"',
}


@pytest.mark.parametrize("name", list(FACTORIES))
def test_a_lost_output_frame_is_a_row_and_the_turn_goes_on(name):
    codec = FACTORIES[name]()
    (row,) = codec.frame_lost(OUTPUT_PREFIX[name])
    assert row.kind == "tool" and row.data["summary"] == FRAME_SKIPPED
    assert row.data["operation_id"] == OPERATION and row.data["state"] == "skipped"
    assert codec.operation_id == OPERATION and codec.lost_turn is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_lost_result_ends_the_turn_uncertain_never_a_success(name):
    codec = FACTORIES[name]()
    row, error, ended = codec.frame_lost(RESULT_PREFIX[name])
    assert row.kind == "tool"
    assert error.kind == "error" and error.data["operation_id"] == OPERATION
    assert error.data["message"] == LOST_RESULT
    assert ended.kind == "turn_completed" and ended.data["state"] == "failed"
    assert codec.operation_id is None


@pytest.mark.parametrize("name", ["opencode", "copilot"])
def test_a_lost_prompt_reply_ends_the_turn_uncertain(name):
    codec = FACTORIES[name]()
    prompt = next(k for k, (action, _) in codec._requests.items() if action == "session/prompt")
    prefix = f'{{"jsonrpc":"2.0","id":"{prompt}","result":{{"stopReason":"end_turn","x":"'.encode()
    row, error, ended = codec.frame_lost(prefix)
    assert row.kind == "tool" and error.kind == "error"
    assert ended.data["state"] == "failed" and error.data["message"] == LOST_RESULT
    assert codec.operation_id is None


@pytest.mark.parametrize("name", list(FACTORIES))
def test_a_lost_request_is_refused_and_the_turn_is_never_claimed_a_success(name):
    codec = FACTORIES[name]()
    row, refusal = codec.frame_lost(REQUEST_PREFIX[name])
    assert row.kind == "tool" and refusal.kind == "send"
    answer = refusal.data["frame"]
    if name == "claude":
        assert answer["response"] == {
            "subtype": "error",
            "request_id": "big-1",
            "error": "Unsupported or uncorrelated control request",
        }
    elif name == "codex":
        assert answer["id"] == 77 and answer["error"]["code"] == -32601
    else:
        assert answer["id"] == 77 and answer["result"]["outcome"]["outcome"] == "cancelled"
    assert codec.lost_turn == OPERATION and codec.operation_id == OPERATION
    # The agent went on and says the turn succeeded: the operator was never asked, so it isn't one.
    done = codec.settle_lost(
        [
            native_protocol._event(
                "turn_completed",
                operation_id=OPERATION,
                native_turn_id=TURN,
                state="completed",
                error="",
                text="all done",
            )
        ]
    )
    assert kinds(done) == ["error", "turn_completed"]
    assert done[0].data["message"] == LOST_REQUEST
    assert done[1].data["state"] == "failed" and done[1].data["text"] == "all done"
    assert codec.lost_turn is None


def test_an_interrupt_stays_an_interrupt_after_a_lost_request():
    codec = claude()
    codec.frame_lost(REQUEST_PREFIX["claude"])
    (done,) = codec.settle_lost(
        [
            native_protocol._event(
                "turn_completed",
                operation_id=OPERATION,
                native_turn_id=OPERATION,
                state="interrupted",
                error="",
            )
        ]
    )
    assert done.data["state"] == "interrupted"


@pytest.mark.parametrize("name", list(FACTORIES))
def test_an_unreadable_frame_is_what_a_turn_may_have_depended_on(name):
    codec = FACTORIES[name]()
    events = codec.frame_lost(b"\x00\x01 garbage")
    assert kinds(events) == ["tool", "error", "turn_completed"]
    assert codec.operation_id is None
    assert codec.unread_frame  # it may be a request: the worker ends the connection


def test_a_prefix_cannot_name_a_request_from_inside_a_payload():
    """Only the frame's own head counts: a tool's output quoting a request is output."""
    codec = claude()
    prefix = (
        b'{"type":"user","message":{"content":"{\\"type\\":\\"control_request\\",\\"request_id\\"'
    )
    assert kinds(codec.frame_lost(prefix)) == ["tool"]
    for make, head in (
        (
            codex,
            b'{"method":"item/completed","params":{"id":5,"text":"\\"method\\":\\"turn/completed\\"',
        ),
        (
            opencode,
            b'{"jsonrpc":"2.0","method":"session/update","params":{"id":5,"text":"session/request',
        ),
    ):
        codec = make()
        assert kinds(codec.frame_lost(head)) == ["tool"]
        assert codec.lost_turn is None and codec.operation_id == OPERATION


def test_claude_a_dropped_frame_ends_an_input_waiting_for_its_result_uncertain():
    """#1644 kept: an input whose lifecycle ended and whose result can't be read ends now. Since
    #1725 it reads uncertain, not `failed`: BattleLab can't say how it went."""
    codec = claude()
    codec.feed({"type": "command_lifecycle", "command_uuid": OPERATION, "state": "queued"})
    codec.feed({"type": "command_lifecycle", "command_uuid": OPERATION, "state": "started"})
    codec.feed({"type": "command_lifecycle", "command_uuid": OPERATION, "state": "completed"})
    assert codec._cycle == "ended"
    row, error, ended = codec.frame_lost(OUTPUT_PREFIX["claude"])
    assert row.kind == "tool" and error.data["message"] == LOST_RESULT
    assert ended.data["state"] == "failed" and codec.operation_id is None


def test_with_no_turn_running_a_lost_frame_is_only_a_diagnostic():
    codec = ClaudeCodec(SESSION)
    (event,) = codec.frame_lost(OUTPUT_PREFIX["claude"])
    assert event.kind == "error" and "operation_id" not in event.data


def test_the_agents_own_turn_carries_the_row_and_a_lost_result_ends_it():
    codec = ClaudeCodec(SESSION)
    codec.feed({"type": "system", "subtype": "status", "status": "requesting"})
    target = codec.background_turn
    assert target is not None
    (row,) = codec.frame_lost(OUTPUT_PREFIX["claude"])
    assert row.kind == "tool" and row.data["agent_turn"] == target
    row, ended = codec.frame_lost(RESULT_PREFIX["claude"])
    assert ended.kind == "background" and ended.data["background_turn"] is None
    assert ended.data["state"] == "failed" and not codec.unowned_turn


# --- the measurement the ledger's bound comes from --------------------------------------------


def test_the_result_ledger_at_its_bound_fits_the_measured_budget():
    """The issue's budget: ≤ 4 MiB per connection, from Python's own allocator, through `feed`."""
    codec = ClaudeCodec(SESSION)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(MAX_RESULTS):
            codec.feed(claude_result(parent_tool_use_id="t"))
        gc.collect()
        used = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert len(codec._results) == MAX_RESULTS
    assert used <= 4 * 1024 * 1024, used
    # And the docs say what was measured.
    assert f"{MAX_RESULTS:,}".replace(",", " ") in DOC.read_text() or str(MAX_RESULTS) in (
        DOC.read_text()
    )


# --- the ratchet --------------------------------------------------------------------------------


def _doc_table() -> str:
    text = DOC.read_text()
    start = text.index("<!-- safety-table -->")
    end = text.index("<!-- /safety-table -->")
    return text[start:end]


#: Raised only where BattleLab is about to WRITE (a submit, a decision, a create): the worker turns
#: them into a refusal of that one effect before anything goes out. They never end a generation.
WRITE_SIDE = {
    "invalid model identity",
    "invalid effort level",
    "turn text is empty or exceeds the size limit",
    "turn text is not valid UTF-8",
    "turn text exceeds the native frame size limit",
    "native frame exceeds the size limit",
    "native connection is closed or request capacity exceeded",
    "native turn is already active or connection closed",
    "that standing grant is not offered for this request",
    "this agent's requests take no answer",
    "unsupported permission decision",
    "permission request is no longer pending for this turn",
    "a background task's request is approved once or declined",
    "that background task is no longer running",
    "native thread is already bound",
    "native thread is not bound",
    "no correlated native turn to steer",
    "no correlated native turn to interrupt",
    "no turn of the agent's own to interrupt",
    "an MCP tool request is allowed once or denied",
    "native result replay capacity exceeded; a new connection is required",
    "this connection cannot write a message ahead of its turn",
    "a message cannot be written ahead of its own turn",
    "no written message to send now",
    "only an agent question takes an answer",
    "an answer goes with an answer decision, and only with one",
    "an agent question is answered, skipped or stopped, not approved",
    "expected an absolute validated path",
    "expected BattleLab's own opencode agent",
    "native session is already bound",
    "native session is not bound",
    "no model to select",
    "no effort to select",
    "native session is not bound in its pinned mode",
    "this client takes no images",
    "this request could not be presented completely",
    "the agent offered no one-shot approval",
    "copilot's ACP session/new carries no stdio MCP servers",
    "copilot's ACP session/load carries no stdio MCP servers",
    "this client grants no standing permission",
}


def _raised() -> set[str]:
    messages = set()
    for node in ast.walk(ast.parse(SOURCE.read_text())):
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and getattr(node.exc.func, "id", "") == "ProtocolError"
            and node.exc.args
            and isinstance(node.exc.args[0], ast.Constant)
        ):
            messages.add(node.exc.args[0].value)
    return messages


#: `raise ProtocolError(<not a literal>)`: its message can't be listed, so its site is. Each is what
#: a table row or `WRITE_SIDE` is for a literal: who raises it, and what it ends.
NON_LITERAL = {
    # An agent question's answer checked before it is written: refuses that one decision.
    "ClaudeCodec.check_answer": "str(exc)",
}


def _protocol_raises(tree: ast.AST) -> list[tuple[str, ast.Raise]]:
    """Every `raise` naming `ProtocolError`, however it builds its message, with where it is."""
    found: list[tuple[str, ast.Raise]] = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, [*stack, child.name])
                continue
            if (
                isinstance(child, ast.Raise)
                and child.exc is not None
                and "ProtocolError" in ast.unparse(child.exc)
            ):
                found.append((".".join(stack), child))
            walk(child, stack)

    walk(tree, [])
    return found


def test_every_protocol_error_is_literal_or_listed_by_site():
    """f-strings and variables can't hide a new way to end a generation from the ratchet."""
    dynamic = {}
    for where, node in _protocol_raises(ast.parse(SOURCE.read_text())):
        call = node.exc
        literal = (
            isinstance(call, ast.Call)
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        )
        if not literal:
            dynamic[where] = (
                ast.unparse(call.args[0])
                if isinstance(call, ast.Call) and call.args
                else ast.unparse(call)
            )
    assert dynamic == NON_LITERAL


#: Every worker function that ends the generation (`self.stop_child()`, a `self.reason`): the row
#: that classifies it. A new one fails below until it has a row.
WORKER_ENDS = {
    "Worker.flush": "worker-journal-unwritable",
    "Worker.send_now": "worker-journal-unwritable",  # no further write without durable evidence
    "Worker.withdraw": "worker-journal-unwritable",  # the same
    "Worker.retire": "worker-rollover",
    "Worker.on_signal": "worker-stop",
    "Worker.idle_watch": "worker-idle-exit",
    "Worker.dispatch_queue": "worker-dispatch-failed",
    "Worker.reader": "worker-native-eof",
    "Worker.handoff": "worker-write-failed",
    "Worker.serve_client": "worker-stop",
    "Worker.run": "worker-init-refused",
}


def _worker_ending_functions() -> set[str]:
    tree = ast.parse((SOURCE.parent / "native_worker.py").read_text())
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Worker")
    ending = set()
    for function in worker.body:
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(function):
            calls_stop = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stop_child"
            )
            sets_reason = isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "reason" for t in node.targets
            )
            if calls_stop or sets_reason:
                ending.add(f"Worker.{function.name}")
    return ending - {"Worker.stop_child", "Worker.__init__"}


def test_every_worker_path_that_ends_a_generation_has_its_row():
    """Derived from the worker's own code, not listed from memory."""
    assert _worker_ending_functions() == set(WORKER_ENDS)
    assert set(WORKER_ENDS.values()) <= set(WORKER_ROWS)


def test_every_protocol_error_is_classified_in_the_table():
    """A new way to end a generation fails here until it has a row, or is shown to be write-side."""
    table = _doc_table()
    unclassified = sorted(
        message for message in _raised() - WRITE_SIDE if f"`{message}`" not in table
    )
    assert not unclassified, unclassified
    # …and nothing is claimed for a message that is gone.
    for message in WRITE_SIDE:
        assert message in _raised(), message
    named = set(re.findall(r"`([a-z][^`]*)`", table))
    gone = {m for m in named if m.startswith(("native ", "invalid native")) and m not in _raised()}
    assert not gone, gone


#: Each capacity row's test, by the start of its name.
CAPACITY_ROWS = {
    "capacity-result-ledger": "test_capacity_result_ledger",
    "capacity-background-tasks": "test_capacity_background_tasks",
    "capacity-background-list": "test_capacity_background_list",
    "capacity-codex-terminals": "test_capacity_codex_terminals",
    "capacity-early-delta": "test_capacity_early_buffer",
    "capacity-file-changes": "test_capacity_file_changes",
    "capacity-mcp-calls": "test_capacity_mcp_calls",
    "capacity-open-requests": "test_capacity_more_open_requests",
    "capacity-answered-requests": "test_capacity_answered_requests",
    "capacity-oversized-request": "test_capacity_a_request_too_large",
    "capacity-oversized-frame": "test_a_lost_output_frame",
    "capacity-unterminated-tail": "test_capacity_an_unterminated_frame",
    "safety-lost-request": "test_a_lost_request_whose_id_cant_be_established",
    "capacity-discovery": "test_capacity_discovery_never_fails_the_connection",
    "capacity-plan-environment": "test_capacity_plan_and_environment_are_cut_or_ignored",
    "effect-unknown-request": "test_effect_an_unknown_request",
}


def test_every_row_of_the_table_has_a_test():
    rows = set(
        re.findall(r"^\| `((?:safety|worker|capacity|effect)-[a-z-]+)` \|", _doc_table(), re.M)
    )
    assert rows == set(SAFETY_ROWS) | set(WORKER_ROWS) | set(CAPACITY_ROWS)
    source = Path(__file__).read_text()
    capacity = (Path(__file__).parent / "test_native_capacity.py").read_text()
    for test in CAPACITY_ROWS.values():
        assert f"def {test}" in source or f"def {test}" in capacity, test


# --- a frame too large to read, through the worker's own reader: both ends, every field order -----


class _Reader:
    """Just what `Worker.lines` / `reader` touch, over the bytes of one native stream."""

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
        self.ready = asyncio.Event()
        self.reason = ""
        self.terminated = self.closing = False

    async def reap_window(self):
        pass

    def dropped_frame(self):
        return native_worker.Worker.dropped_frame(self)

    def record(self, events):
        self.recorded.extend(events)

    async def handle_events(self, events):
        self.handled.extend(events)

    def stop_child(self):
        pass

    def lines(self):
        return native_worker.Worker.lines(self)


def _read(codec, frame: dict | bytes) -> _Reader:
    raw = frame if isinstance(frame, bytes) else json.dumps(frame).encode() + b"\n"
    assert len(raw) > MAX_FRAME_BYTES

    async def run():
        reader = _Reader(raw, codec)  # its StreamReader needs the running loop
        await native_worker.Worker.reader(reader)
        return reader

    return asyncio.run(run())


BIG = {"blob": "x" * (MAX_FRAME_BYTES + 10)}
ASK = "session/request_permission"
APPROVE = "item/commandExecution/requestApproval"


def _orders(method, identity, **common):
    """A request with its id on each side of its payload, whichever way its fields come."""
    return {
        "id-first": {**common, "id": identity, "method": method, "params": BIG},
        "params-before-id": {**common, "method": method, "params": BIG, "id": identity},
        "params-first": {"params": BIG, **common, "method": method, "id": identity},
        "method-last": {**common, "id": identity, "params": BIG, "method": method},
    }


LOST_REQUESTS = [
    *(
        ("claude", name, frame, "r-9")
        for name, frame in {
            "type-first": {"type": "control_request", "request_id": "r-9", "request": BIG},
            "request-first": {"request": BIG, "request_id": "r-9", "type": "control_request"},
            "id-last": {"type": "control_request", "request": BIG, "request_id": "r-9"},
            "type-last": {"request_id": "r-9", "request": BIG, "type": "control_request"},
        }.items()
    ),
    *(("codex", name, frame, 77) for name, frame in _orders(APPROVE, 77).items()),
    *(("codex", f"str-{name}", frame, "x-7") for name, frame in _orders(APPROVE, "x-7").items()),
    *(("opencode", name, f, 77) for name, f in _orders(ASK, 77, jsonrpc="2.0").items()),
    *(("copilot", name, f, 77) for name, f in _orders(ASK, 77, jsonrpc="2.0").items()),
]


@pytest.mark.parametrize(
    ("name", "order", "frame", "identity"),
    LOST_REQUESTS,
    ids=[f"{n}-{o}-{i}" for n, o, _, i in LOST_REQUESTS],
)
def test_a_lost_request_is_answered_whichever_way_its_fields_come(name, order, frame, identity):
    """Answered, never silent: an unanswered callback can hang a background task for ever."""
    codec = FACTORIES[name]()
    reader = _read(codec, frame)
    answers = [e.data["frame"] for e in reader.handled if e.kind == "send"]
    assert len(answers) == 1, reader.handled
    if name == "claude":
        assert answers[0]["response"]["request_id"] == identity
        assert answers[0]["response"]["subtype"] == "error"
    elif name == "codex":
        assert answers[0]["id"] == identity and "error" in answers[0]
    else:
        assert answers[0]["id"] == identity
        assert answers[0]["result"]["outcome"]["outcome"] == "cancelled"
    assert not reader.reason  # the connection lives
    assert codec.lost_turn == OPERATION  # and the turn can't claim success


LOST_UNIDENTIFIED = [
    ("claude", "control_request", {"type": "control_request", "request": BIG}),
    ("claude", "id-between", {"request": BIG, "type": "control_request", "request_id": 7}),
    ("claude", "no-type", {"a": BIG, "type": "control_request", "request_id": "r", "b": BIG}),
    ("codex", "no-id", {"method": APPROVE, "params": BIG}),
    ("codex", "id-in-the-middle", {"method": APPROVE, "a": BIG, "id": 77, "b": BIG}),
    ("codex", "no-method", {"a": BIG, "method": APPROVE, "id": 7, "b": BIG}),
    ("opencode", "no-id", {"jsonrpc": "2.0", "method": ASK, "params": BIG}),
    (
        "opencode",
        "id-in-the-middle",
        {"jsonrpc": "2.0", "method": ASK, "a": BIG, "id": 7, "b": BIG},
    ),
    ("copilot", "no-id", {"jsonrpc": "2.0", "method": ASK, "params": BIG}),
    ("copilot", "id-in-the-middle", {"jsonrpc": "2.0", "method": ASK, "a": BIG, "id": 7, "b": BIG}),
]


@pytest.mark.parametrize(
    ("name", "case", "frame"), LOST_UNIDENTIFIED, ids=[f"{n}-{c}" for n, c, _ in LOST_UNIDENTIFIED]
)
def test_a_lost_request_whose_id_cant_be_established_ends_the_connection(name, case, frame):
    """No guessing and no silence: the callback can't be answered, so the agent would wait on it
    for ever. The connection ends, the turn reads uncertain, and the operator is told why."""
    codec = FACTORIES[name]()
    reader = _read(codec, frame)
    assert reader.reason, "the connection must end"
    assert not [e for e in reader.handled if e.kind == "send"]  # nothing answered blind
    uncertain = [e for e in reader.handled if e.kind == "error" and "operation_id" in e.data]
    assert uncertain and uncertain[0].data["message"] == native_protocol.LOST_UNREADABLE
    done = [e for e in reader.handled if e.kind == "turn_completed"]
    assert done and done[0].data["state"] == "failed" and codec.operation_id is None


NOT_REQUESTS = [
    # What looks like a request is inside a string value, at either end of the frame.
    (
        "opencode",
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"blob": BIG["blob"] + '","id":5,"method":"session/request_permission"'},
        },
    ),
    (
        "copilot",
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"text": '"id":5,"method":"session/request_permission"' + BIG["blob"]},
        },
    ),
    (
        "codex",
        {
            "method": "item/completed",
            "params": {"blob": BIG["blob"] + '","id":5}'},
        },
    ),
    (
        "claude",
        {
            "type": "user",
            "message": {"blob": BIG["blob"] + '","type":"control_request","request_id":"r"}'},
        },
    ),
]


@pytest.mark.parametrize(("name", "frame"), NOT_REQUESTS, ids=[n for n, _ in NOT_REQUESTS])
def test_a_tools_output_can_never_name_a_request(name, frame):
    codec = FACTORIES[name]()
    reader = _read(codec, frame)
    assert not [e for e in reader.handled if e.kind == "send"]
    assert not reader.reason and codec.operation_id == OPERATION and codec.lost_turn is None


def _rpc_frame(token: bytes, *, last: bool = False) -> bytes:
    body = b'"x":"' + b"x" * (MAX_FRAME_BYTES + 10) + b'"'
    method = b'"method":"item/commandExecution/requestApproval"'
    if last:
        return b"{" + method + b',"params":{' + body + b'},"id":' + token + b"}\n"
    return b'{"id":' + token + b"," + method + b',"params":{' + body + b"}}\n"


@pytest.mark.parametrize("last", [False, True], ids=["id-first", "id-last"])
@pytest.mark.parametrize(
    ("token", "value"),
    [
        (b"9007199254740991", 9007199254740991),  # the largest `_rpc_id` admits
        (b"-9007199254740991", -9007199254740991),
        (b"900719925474099", 900719925474099),
        (b"0", 0),
        (b'"abc-1"', "abc-1"),
    ],
)
def test_a_lost_requests_id_is_its_whole_token(token, value, last):
    codec = codex()
    reader = _read(codec, _rpc_frame(token, last=last))
    answers = [e.data["frame"] for e in reader.handled if e.kind == "send"]
    assert [a["id"] for a in answers] == [value]
    assert type(answers[0]["id"]) is type(value)


@pytest.mark.parametrize("last", [False, True], ids=["id-first", "id-last"])
@pytest.mark.parametrize(
    "token",
    [
        b"9007199254740992",  # one past what `_rpc_id` admits: never its first 15 digits
        b"90071992547409910",
        b"-9007199254740992",
        b"12345abc",
        b"1.5",
        b"1e3",
        b"007",
        b"null",
        b"true",
        b'"has space"',
    ],
)
def test_an_invalid_id_is_never_answered_as_another(token, last):
    codec = codex()
    reader = _read(codec, _rpc_frame(token, last=last))
    assert not [e for e in reader.handled if e.kind == "send"]
    assert reader.reason  # fails closed


def test_a_reply_or_notification_too_large_needs_no_answer():
    """Nothing waits on them, so no id is owed."""
    codec = opencode()
    prompt = next(k for k, (action, _) in codec._requests.items() if action == "session/prompt")
    reader = _read(codec, {"jsonrpc": "2.0", "result": BIG, "id": prompt})
    assert not reader.reason and not [e for e in reader.handled if e.kind == "send"]
    assert codec.operation_id is None  # its own prompt's reply: that turn ended uncertain
    codec = codex()
    reader = _read(codec, {"params": BIG, "method": "item/completed"})
    assert not reader.reason and codec.operation_id == OPERATION


def test_duplicate_fields_across_the_two_ends_are_never_chosen_between():
    codec = codex()
    frame = (
        b'{"id":1,"method":"item/commandExecution/requestApproval","params":{"x":"'
        + b"x" * (MAX_FRAME_BYTES + 10)
        + b'"},"id":2}\n'
    )
    reader = _read(codec, frame)
    assert not [e for e in reader.handled if e.kind == "send"] and reader.reason


# --- #1726's paths: discovery, the plan and the tools --------------------------------------------


def _codex_lost(frame):
    codec = codex()
    return codec, _read(codec, frame)


@pytest.mark.parametrize("method", ["skills/changed", "mcpServer/startupStatus/updated"])
def test_a_lost_discovery_notification_is_a_row_and_ends_nothing(method):
    """Nothing waits on them: the client sends them with no id, and discovery is best effort."""
    codec, reader = _codex_lost({"method": method, "params": BIG})
    assert not reader.reason and not codec.unread_frame and codec.operation_id == OPERATION


def _asked(codec, action):
    return next(k for k, (name, _) in codec._requests.items() if name == action)


def test_capacity_discovery_a_lost_reply_frees_what_waited_for_it():
    """`skills/list` holds later asks while it is out: a reply too large to read must release it,
    or the tools tab would never hear of a skill again."""
    codec = codex()  # binding the thread asked for it
    asked = _asked(codec, "skills/list")
    assert codec._skills_inflight
    reader = _read(codec, {"id": asked, "result": BIG})
    assert not reader.reason and not codec._skills_inflight and asked not in codec._requests


def test_capacity_discovery_never_fails_the_connection():
    codec = codex()
    # An error, or a reply that makes no sense: that list stays unreported, the connection lives.
    assert codec.feed({"id": _asked(codec, "skills/list"), "error": {"message": "no"}}) == []
    nonsense = codec.feed({"id": _asked(codec, "mcpServerStatus/list"), "result": {"data": "x"}})
    assert {event.kind for event in nonsense} <= {"environment"} and codec.operation_id == OPERATION
    # And when none can be asked at all.
    codec._requests = {f"r{n}": ("x", None) for n in range(MAX_PENDING)}
    assert codec._discover() == []


def test_capacity_plan_and_environment_are_cut_or_ignored_never_fatal():
    codec = codex()
    plan = {"threadId": SESSION, "turnId": TURN, "explanation": 7}
    assert codec.feed({"method": "turn/plan/updated", "params": {**plan, "plan": "nonsense"}}) == []
    rows = [{"step": "x" * 5000, "status": "new"} for _ in range(native_protocol.PLAN_ENTRIES + 5)]
    (event,) = codec.feed({"method": "turn/plan/updated", "params": {**plan, "plan": rows}})
    assert event.kind == "plan" and len(event.data["entries"]) == native_protocol.PLAN_ENTRIES
    assert event.data["entries"][0]["status"] == "pending"  # never done on a word it doesn't know
    assert len(event.data["entries"][0]["text"]) <= native_protocol.PLAN_TEXT
    native_protocol._names("nonsense", 3)
    assert native_protocol._names([{"name": 1}, 2, "ok"], 3) == ["ok"]


# --- "this needs no answer" is named, never inferred ----------------------------------------------

#: `ServerRequest.json` of `codex app-server generate-json-schema` (0.164.0): what waits on a reply.
CODEX_REQUESTS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/permissions/requestApproval",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
    "applyPatchApproval",
    "execCommandApproval",
)


@pytest.mark.parametrize(
    "method", [*CODEX_REQUESTS, "item/future/thing", "turn/future", "thread/x"]
)
def test_a_codex_frame_not_named_a_notification_with_its_id_out_of_reach_ends_the_connection(
    method,
):
    """An ordinary-sized copy gets an answer; left silent, the caller would wait for ever."""
    codec = codex()
    reader = _read(codec, {"method": method, "a": BIG, "id": 7, "b": BIG})
    assert reader.reason and codec.unread_frame
    assert not [e for e in reader.handled if e.kind == "send"]
    assert [e for e in reader.handled if e.kind == "turn_completed"] and codec.operation_id is None


@pytest.mark.parametrize("method", CODEX_REQUESTS)
def test_a_codex_request_with_its_id_in_reach_is_refused_whatever_it_is(method):
    codec = codex()
    reader = _read(codec, {"method": method, "params": BIG, "id": 7})
    assert [e.data["frame"]["id"] for e in reader.handled if e.kind == "send"] == [7]
    assert not reader.reason


@pytest.mark.parametrize(
    "method",
    [
        "item/agentMessage/delta",
        "item/completed",
        "item/started",
        "item/commandExecution/outputDelta",
        "item/mcpToolCall/progress",
        "turn/diff/updated",
        "turn/plan/updated",
        "thread/started",
        "thread/tokenUsage/updated",
        "account/rateLimits/updated",
        "fs/changed",
        "configWarning",
    ],
)
def test_a_true_codex_notification_costs_only_the_row(method):
    codec = codex()
    reader = _read(codec, {"method": method, "params": BIG})
    assert not reader.reason and not codec.unread_frame and codec.operation_id == OPERATION
    assert not [e for e in reader.handled if e.kind == "send"]
    assert [e.kind for e in reader.handled] == ["tool"]


def test_no_codex_request_is_ever_taken_for_a_notification():
    assert not set(CODEX_REQUESTS) & native_protocol.CODEX_NOTIFICATIONS


@pytest.mark.parametrize("make", [opencode, copilot], ids=["opencode", "copilot"])
@pytest.mark.parametrize(
    "method", ["session/update_future", "session/future", "fs/read_text_file", "terminal/create"]
)
def test_acp_only_session_update_is_taken_for_a_notification(make, method):
    codec = make()
    reader = _read(codec, {"jsonrpc": "2.0", "method": method, "a": BIG, "id": 7, "b": BIG})
    assert reader.reason and codec.unread_frame
    assert not [e for e in reader.handled if e.kind == "send"]


def test_claude_a_type_it_doesnt_know_is_ambiguous_not_output():
    codec = claude()
    reader = _read(codec, {"type": "future_request", "a": BIG, "request_id": "r", "b": BIG})
    assert reader.reason and codec.unread_frame
    uncertain = [e for e in reader.handled if e.kind == "error" and "operation_id" in e.data]
    assert uncertain and uncertain[0].data["message"] == native_protocol.LOST_UNREADABLE


@pytest.mark.parametrize(
    "kind",
    [
        "assistant",
        "user",
        "stream_event",
        "system",
        "rate_limit_event",
        "command_lifecycle",
        "control_response",
        "control_cancel_request",
        "tool_progress",
        "tool_use_summary",
        "auth_status",
        "prompt_suggestion",
        "keep_alive",
    ],
)
def test_claude_a_type_that_waits_for_nothing_costs_only_the_row(kind):
    codec = claude()
    reader = _read(codec, {"type": kind, "message": BIG})
    assert not reader.reason and codec.operation_id == OPERATION
    assert [e.kind for e in reader.handled] == ["tool"]
