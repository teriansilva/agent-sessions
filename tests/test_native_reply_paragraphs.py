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
