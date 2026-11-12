"""An API session's transcript (`transcript._native_turns`, #1900) shows what the agent DID, not
only what it said: each tool call as a one-liner and each finished call's output, bounded, like a
console session's transcript. Approval payloads and turn state are never transcript, and the fold
is capped like every other reader's."""

import json

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import transcript
from test_native_journal import event, request
from test_native_response_timestamps import completed

conversation = journal_tests.conversation


def _turn(root, binding, text="Inspect the project"):
    sid = binding.session_key.partition(":")[2]
    claim = request(binding, revision=journal.read(root, sid).revision, text=text)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    return op


def _tool(op, item, **data):
    return {
        "kind": "tool",
        "data": {"operation_id": op, "native_turn_id": "t1", "item_id": item, **data},
    }


def _approval(op, summary, binding):
    return {
        "kind": "approval",
        "data": {
            "operation_id": op,
            "native_turn_id": "t1",
            "request_id": "s:approval-1",
            "item_id": "edit-1",
            "tool": "Edit",
            "summary": summary,
            "choices": ["approve", "reject"],
            "payload_digest": "a" * 64,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "complete": True,
        },
    }


def test_tool_calls_and_their_outputs_are_bounded_transcript_lines(conversation):
    root, sid, binding = conversation
    op = _turn(root, binding)
    secret_body = "CONTENT-BEING-WRITTEN " * 50
    journal.append_events(
        root,
        binding,
        [
            _tool(
                op,
                "edit-1",
                tool="Edit",
                state="running",
                call=True,
                summary=json.dumps({"file_path": "/w/app.py", "new_string": secret_body}),
            ),
            _tool(op, "edit-1", state="completed", output="The file /w/app.py was updated. " * 40),
            _tool(op, "bash-1", tool="Bash", state="running", call=True, summary="pytest -q"),
            event(op, "All green."),
            completed(op),
        ],
    )
    turns = transcript._native_turns(root, sid)
    assert [(t.role, t.kind) for t in turns] == [
        ("user", "text"),
        ("assistant", "tool"),
        ("tool", "result"),
        ("assistant", "tool"),
        ("assistant", "text"),
    ]
    assert turns[1].text == "Edit(/w/app.py)"
    # Only the call's key argument: the content being written is never transcript.
    assert "CONTENT-BEING-WRITTEN" not in "\n".join(t.text for t in turns)
    assert len(turns[2].text) <= transcript._RESULT_MAX
    assert turns[3].text == "Bash(pytest -q)"
    assert turns[-1].text == "All green."


def test_approval_payloads_and_turn_state_are_never_transcript(conversation):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(
        root,
        binding,
        [
            _approval(
                op, json.dumps({"file_path": "/w/x", "new_string": "APPROVAL-PAYLOAD"}), binding
            ),
            completed(op),
        ],
    )
    turns = transcript._native_turns(root, sid)
    rendered = "\n".join(t.text for t in turns)
    assert "APPROVAL-PAYLOAD" not in rendered
    # A completed turn with no reply adds nothing: turn state is never written as a line.
    assert [(t.role, t.text) for t in turns] == [("user", "Inspect the project")]


def test_tool_lines_pass_the_pre_cut_guard(conversation):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(
        root,
        binding,
        [
            _tool(
                op, "b", tool="Bash", state="running", call=True, summary="export T=hunter2-token"
            ),
            _tool(op, "b", state="completed", output="hunter2-token printed"),
        ],
    )
    token = transcript.PRE_CUT_GUARD.set(lambda s: s.replace("hunter2-token", "[REDACTED]"))
    try:
        turns = transcript._native_turns(root, sid)
    finally:
        transcript.PRE_CUT_GUARD.reset(token)
    rendered = "\n".join(t.text for t in turns)
    assert "hunter2" not in rendered
    assert "[REDACTED]" in rendered


def test_the_fold_is_capped_and_says_so(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(transcript, "DEFAULT_MAX_MESSAGES", 3)
    for i in range(3):
        op = _turn(root, binding, text=f"Message {i}")
        journal.append_events(root, binding, [event(op, f"reply {i}"), completed(op)])
    turns = transcript._native_turns(root, sid)
    assert [t.text for t in turns] == ["reply 1", "Message 2", "reply 2"]
    assert transcript.was_truncated(turns)


@pytest.mark.parametrize("summary", ['{"file_path": "/w/a', "[1, 2"])
def test_an_unparseable_json_summary_shows_no_argument(conversation, summary):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(
        root, binding, [_tool(op, "w", tool="Write", state="running", call=True, summary=summary)]
    )
    assert transcript._native_turns(root, sid)[-1].text == "Write()"
