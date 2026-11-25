"""An API session's reply keeps its messages apart: each text block the agent sends between tool
calls is its own paragraph, never glued to the last one ("…the signup path.The reference…")."""

from dataclasses import replace

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import transcript
from test_native_journal import event, request
from test_native_response_timestamps import ADAPTERS, completed, snapshot

conversation = journal_tests.conversation


def text(operation_id, body, *, item, partial):
    e = event(operation_id, body)
    e["data"].update(item_id=item, partial=partial)
    return e


def start(conversation, adapter):
    root, sid, binding = conversation
    binding = replace(binding, adapter=adapter)
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    return root, sid, binding, operation_id


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_settled_blocks_are_separate_paragraphs(conversation, adapter):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            text(op, "I'll map the flows.", item="m1", partial=False),
            text(op, "", item="m2", partial=False),
            text(op, "The clone is stale.", item="m3", partial=False),
            completed(op),
        ],
    )
    reply = snapshot(root, sid, binding)["turns"][0]["reply"]
    assert reply == "I'll map the flows.\n\nThe clone is stale."
    folded = transcript._native_turns(root, sid)
    assert folded[-1].text == reply


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_streamed_pieces_join_but_a_new_message_breaks(conversation, adapter):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            text(op, "Checking ", item="m1", partial=True),
            text(op, "the theme.", item="m1", partial=True),
            text(op, "Found it.", item="m2", partial=True),
        ],
    )
    turn = snapshot(root, sid, binding)["turns"][0]
    assert turn["reply"] == "Checking the theme.\n\nFound it."
    assert "partial_item" not in turn


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_reply_that_was_only_streamed_reaches_the_transcript(conversation, adapter):
    """A client that only streams (opencode's API client sends no settled text and no final text)
    still has its closing message in the transcript HQ and the AI review read, joined as the pane
    shows it: a session that ended on a proposal must not read as "only tool calls"."""
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            text(op, "Let me check ", item="m1", partial=True),
            text(op, "the spec.", item="m1", partial=True),
            text(op, "Proposal: one fix. ", item="m2", partial=True),
            text(op, "Want me to proceed?", item="m2", partial=True),
            completed(op),
        ],
    )
    folded = transcript._native_turns(root, sid)
    assert folded[-1].role == "assistant"
    assert folded[-1].text == "Let me check the spec.\n\nProposal: one fix. Want me to proceed?"
    assert folded[-1].text == snapshot(root, sid, binding)["turns"][0]["reply"]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_settled_message_replaces_its_own_streamed_pieces(conversation, adapter):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            text(op, "Draft ", item="m1", partial=True),
            text(op, "Draft text.", item="m1", partial=False),
            text(op, "Still streaming", item="m2", partial=True),
            completed(op),
        ],
    )
    folded = transcript._native_turns(root, sid)
    assert folded[-1].text == "Draft text.\n\nStill streaming"


def unkeyed(operation_id, body, *, partial):
    """A text event with NO message id: Claude's stream events, and ACP without `messageId`."""
    e = event(operation_id, body)
    e["data"].pop("item_id", None)
    e["data"]["partial"] = partial
    return e


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_unkeyed_pieces_are_one_message_and_their_settlement_replaces_them(conversation, adapter):
    """Hermes 8922: pieces with no message id were one message EACH, and the settled answer was
    added after them as another ("Hello \\n\\nworld\\n\\nHello world")."""
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            unkeyed(op, "Hello ", partial=True),
            unkeyed(op, "world", partial=True),
            unkeyed(op, "Hello world", partial=False),
            completed(op),
        ],
    )
    assert transcript._native_turns(root, sid)[-1].text == "Hello world"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_an_unkeyed_streamed_only_reply_is_joined_not_split(conversation, adapter):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            unkeyed(op, "Want me ", partial=True),
            unkeyed(op, "to proceed?", partial=True),
            completed(op),
        ],
    )
    assert transcript._native_turns(root, sid)[-1].text == "Want me to proceed?"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_unkeyed_messages_stay_apart_around_a_tool_call_and_after_a_settlement(
    conversation, adapter
):
    root, sid, binding, op = start(conversation, adapter)
    tool = {
        "kind": "tool",
        "data": {
            "operation_id": op,
            "native_turn_id": "native-turn-1",
            "item_id": "t1",
            "tool": "grep",
            "state": "completed",
        },
    }
    journal.append_events(
        root,
        binding,
        [
            unkeyed(op, "Checking ", partial=True),
            unkeyed(op, "the spec.", partial=True),
            tool,
            unkeyed(op, "Found ", partial=True),
            unkeyed(op, "it.", partial=True),
            unkeyed(op, "Found it.", partial=False),
            unkeyed(op, "Next: ", partial=True),
            unkeyed(op, "the fix.", partial=True),
            completed(op),
        ],
    )
    assert transcript._native_turns(root, sid)[-1].text == (
        "Checking the spec.\n\nFound it.\n\nNext: the fix."
    )


def test_claudes_unkeyed_stream_settled_with_its_message_id_is_one_answer(conversation):
    """Hermes 8934: Claude streams `content_block_delta` frames with NO item id and settles the
    reply with `message.id`. That id is new to the fold, which made the settled answer a second
    copy ("Hello world\\n\\nHello world"). Driven through the real `ClaudeCodec`."""
    from agent_sessions.native_protocol import ClaudeCodec

    root, sid, binding, op = start(conversation, "claude-stream-json")
    codec = ClaudeCodec("b6bf1a94-f584-4d7a-b2a2-850de2ab17f5")
    codec.submit("Say hello", op)
    frames = [
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "Hello "}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "world"}}},
        {"type": "assistant", "parent_tool_use_id": None,
         "message": {"id": "msg_0001", "content": [{"type": "text", "text": "Hello world"}]}},
    ]  # fmt: skip
    events = [e for f in frames for e in codec.feed(f) if e.kind == "text"]
    shapes = [(bool(e.data.get("item_id")), e.data["partial"]) for e in events]
    assert shapes == [(False, True), (False, True), (True, False)]  # the producer's real shape
    journal.append_events(
        root, binding, [{"kind": e.kind, "data": dict(e.data)} for e in events] + [completed(op)]
    )
    assert transcript._native_turns(root, sid)[-1].text == "Hello world"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_keyed_draft_is_not_taken_over_by_another_messages_settlement(conversation, adapter):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            text(op, "First, streaming", item="m1", partial=True),
            text(op, "Second, settled.", item="m2", partial=False),
            completed(op),
        ],
    )
    assert transcript._native_turns(root, sid)[-1].text == "First, streaming\n\nSecond, settled."
