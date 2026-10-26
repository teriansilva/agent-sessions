"""A tool call's place in the reply: the activity reads in the order the agent worked, between
the messages it wrote rather than all before them."""

import json

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_agent_turns import background, own, raw, snapshot
from test_native_journal import request

conversation = journal_tests.conversation


def submit(root, sid, binding):
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    return operation_id


def text(op, value, *, partial=False, item="m1"):
    data = {"operation_id": op, "native_turn_id": "n1", "item_id": item, "text": value}
    return {"kind": "text", "data": {**data, "partial": partial, "truncated": False}}


def tool(op, item_id, state="running"):
    data = {"operation_id": op, "native_turn_id": "n1", "item_id": item_id, "state": state}
    if state == "running":
        data.update(tool="Bash", summary=json.dumps({"command": f"echo {item_id}"}))
    return {"kind": "tool", "data": data}


def done(op, value=""):
    data = {"operation_id": op, "native_turn_id": "n1", "state": "completed", "error": ""}
    return {"kind": "turn_completed", "data": {**data, **({"text": value} if value else {})}}


def placed(turn):
    return [(t["id"], t["at"]) for t in turn["tools"]]


def test_each_call_sits_after_the_text_written_before_it(conversation):
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = [
        tool(op, "a"),
        tool(op, "a", "completed"),
        text(op, "Reading the launch path."),
        tool(op, "b"),
        text(op, "Now the decide module.", item="m2"),
        tool(op, "c"),
        # A result names only its call: the call keeps the place it was made at.
        tool(op, "b", "completed"),
        done(op),
    ]
    journal.append_events(root, binding, events)
    (turn,) = snapshot(root, sid, binding)["turns"]
    first = len("Reading the launch path.")
    assert turn["reply"] == "Reading the launch path.\n\nNow the decide module."
    assert placed(turn) == [("a", 0), ("b", first), ("c", len(turn["reply"]))]
    assert not {"_at"} & set(turn["tools"][0])
    assert not {"reply_cut", "partial_cut"} & set(raw(root, sid, binding)["turns"][0])


def test_a_streamed_only_reply_places_calls_in_the_streamed_text(conversation):
    # opencode streams every message and settles none: the reply shown IS the streamed text.
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = [
        text(op, "Look", partial=True, item="m1"),
        text(op, "ing.", partial=True, item="m1"),
        tool(op, "a"),
        text(op, "Done.", partial=True, item="m2"),
    ]
    journal.append_events(root, binding, events)
    (turn,) = snapshot(root, sid, binding)["turns"]
    assert turn["reply"] == "Looking.\n\nDone."
    assert placed(turn) == [("a", len("Looking."))]


def test_a_reply_that_only_came_whole_at_the_end_keeps_every_call_before_it(conversation):
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    journal.append_events(root, binding, [tool(op, "a"), tool(op, "b"), done(op, "All done.")])
    (turn,) = snapshot(root, sid, binding)["turns"]
    assert turn["reply"] == "All done."
    assert placed(turn) == [("a", 0), ("b", 0)]


def test_a_place_survives_the_runtime_keeping_only_the_reply_tail(conversation, monkeypatch):
    monkeypatch.setattr(native_runtime, "SNAPSHOT_TEXT", 10)
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = [
        tool(op, "a"),
        text(op, "aaaaaaaa"),
        tool(op, "b"),
        text(op, "bbbbbbbb", item="m2"),
        tool(op, "c"),
    ]
    journal.append_events(root, binding, events)
    (turn,) = raw(root, sid, binding)["turns"]
    # "aaaaaaaa\n\nbbbbbbbb" kept as its last 10: the 8 dropped characters shift every place.
    assert turn["reply"] == "\n\nbbbbbbbb"
    assert placed(turn) == [("a", 0), ("b", 0), ("c", 10)]


def test_the_facade_cut_puts_later_calls_at_the_end_of_what_it_shows(conversation, monkeypatch):
    monkeypatch.setattr(structured_runtime, "SNAPSHOT_TEXT", 5)
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = [text(op, "abc"), tool(op, "a"), text(op, "defghij", item="m2"), tool(op, "b")]
    journal.append_events(root, binding, events)
    source = raw(root, sid, binding)
    view = structured_runtime._snapshot(binding.session_key, source)
    (turn,) = view["turns"]
    # Two messages over the cap (#1643): the newest one, here only its tail (marked partial), not
    # the first characters. A call made before what is shown is left out, never clamped onto it.
    assert turn["reply"] == "fghij" and not turn["reply_truncated"]
    assert turn["first_message_partial"] and turn["tools_truncated"]
    assert placed(turn) == [("b", 5)]
    # The facade answers with its own rows; the runtime's projection is left as it was.
    assert [t["at"] for t in source["turns"][0]["tools"]] == [3, len("abc\n\ndefghij")]


@pytest.mark.parametrize("reply", ["Checked it.", ""])
def test_an_own_turn_places_its_calls_too(conversation, reply):
    root, sid, binding = conversation
    target = "00000000-0000-4000-8000-000000000001"
    events = [
        background(target),
        own(target, "Looking."),
        own(target, tool=("a", "Bash", "{}", "running")),
    ]
    if reply:
        events.append(own(target, reply))
    journal.append_events(root, binding, events)
    (turn,) = snapshot(root, sid, binding)["turns"]
    assert turn["kind"] == "agent"
    assert placed(turn) == [("a", len("Looking."))]
