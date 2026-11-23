"""An API session's transcript (`transcript._native_turns`, #1900) shows what the agent DID, not
only what it said: each tool call as a one-liner and each finished call's output, bounded, like a
console session's transcript. Approval payloads and turn state are never transcript, and the fold
is capped like every other reader's."""

import json

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import transcript
from agent_sessions.native_protocol import ClaudeCodec
from test_native_journal import event, request
from test_native_protocol import SESSION as CODEC_SESSION
from test_native_protocol import _tool_result
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


def _fold_through_the_codec(conversation, tool_id, content):
    """A Claude tool result fed through the real codec, journaled and folded (#1982)."""
    root, sid, binding = conversation
    op = _turn(root, binding)
    codec = ClaudeCodec(CODEC_SESSION)
    codec.submit("Inspect the project", op)
    (produced,) = codec.feed(_tool_result(tool_id, content))
    journal.append_events(root, binding, [{"kind": produced.kind, "data": produced.data}])
    return transcript._native_turns(root, sid)[-1]


def test_a_list_content_tool_result_folds_to_its_text_not_json(conversation):
    blocks = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    turn = _fold_through_the_codec(conversation, "toolu_list", blocks)
    assert (turn.role, turn.kind, turn.text) == ("tool", "result", "hello world")
    assert '"type":"text"' not in turn.text


def test_a_mixed_list_content_tool_result_keeps_a_placeholder_for_non_text(conversation):
    blocks = [
        {"type": "text", "text": "saved the file"},
        {"type": "image", "source": {"data": "QUJD" * 100}},
    ]
    turn = _fold_through_the_codec(conversation, "toolu_mixed", blocks)
    assert turn.text == "saved the file [image]"
    assert "QUJD" not in turn.text


@pytest.mark.parametrize(
    "stored",
    [
        '[{"type":"invoice","id":42,"amount":99}]',  # a domain object that has a `type`
        '[{"type":"text","text":"hi","meta":{}}]',  # content-shaped, with an extra key
        '[{"type":"text","value":"hi"}]',  # content-shaped, wrong shape
        '[{"type":"image","data":"QUJD"}]',  # image block without its `source`
        '[{"type":"unknown","text":"hi"}]',  # a type no content block carries
        '[{"type":"text","text":"hi"},42]',  # not every element is a block
        # Exactly block-shaped JSON is still TEXT (#1982 review 8770): a stored string is never
        # re-parsed, so a result that prints a content-block fixture keeps every field.
        '[{"type":"text","text":"hi"}]',
        '[{"type":"image","source":{"url":"https://example.invalid/report.png"}}]',
    ],
)
def test_a_stored_string_is_never_reparsed_as_content_blocks(conversation, stored):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(root, binding, [_tool(op, "t", state="completed", output=stored)])
    assert transcript._native_turns(root, sid)[-1].text == stored


def test_a_deeply_nested_tool_output_survives_codec_journal_and_fold(conversation):
    """P1 (#1982 review): a plain result of 9,999 `[` … 9,999 `]` is 19,999 characters (inside
    ``MAX_TEXT``), journaled intact, but decoding it must not raise ``RecursionError`` and break
    the whole fold."""
    root, sid, binding = conversation
    op = _turn(root, binding)
    nested = "[" * 9999 + "0" + "]" * 9999
    codec = ClaudeCodec(CODEC_SESSION)
    codec.submit("Inspect the project", op)
    (produced,) = codec.feed(_tool_result("toolu_deep", nested))
    assert produced.kind == "tool" and produced.data["output"] == nested
    journal.append_events(root, binding, [{"kind": produced.kind, "data": produced.data}])
    turn = transcript._native_turns(root, sid)[-1]
    assert turn.kind == "result"
    assert turn.text == nested[: transcript._RESULT_MAX - 1] + "…"


def test_a_truncated_stored_result_shows_as_stored(conversation):
    root, sid, binding = conversation
    op = _turn(root, binding)
    stored = '[{"type":"text","text":"hello world"'  # cut at MAX_TEXT before the list closed
    journal.append_events(root, binding, [_tool(op, "t", state="completed", output=stored)])
    assert transcript._native_turns(root, sid)[-1].text == stored


def test_a_text_result_that_looks_like_json_is_shown_as_stored(conversation):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(root, binding, [_tool(op, "t", state="completed", output="[1, 2, 3]")])
    assert transcript._native_turns(root, sid)[-1].text == "[1, 2, 3]"


@pytest.mark.parametrize("summary", ['{"file_path": "/w/a', "[1, 2"])
def test_an_unparseable_json_summary_shows_no_argument(conversation, summary):
    root, sid, binding = conversation
    op = _turn(root, binding)
    journal.append_events(
        root, binding, [_tool(op, "w", tool="Write", state="running", call=True, summary=summary)]
    )
    assert transcript._native_turns(root, sid)[-1].text == "Write()"
