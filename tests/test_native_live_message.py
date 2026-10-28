"""Every message of a running operator turn streams (#1727 part 2).

Codex and Claude settle each message (a settled text block). The projection used to show the
settled reply alone once one existed, so the turn's next message appeared only when it finished.
The operator's snapshot (`live_text`) now shows the settled reply plus the message streaming now —
the partial text after the last settled block — marked `stream` for the pane. Tool rows keep
their place between messages; nothing is shown twice once the message settles; history, the
public link and every other reader keep the settled reply alone; agents' own turns never stream.
"""

from __future__ import annotations

# ruff: noqa: F811 — `conversation` is a fixture imported from test_native_journal
import uuid

import pytest

from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_agent_turns import background, own
from test_native_journal import conversation  # noqa: F401
from test_native_reply_paragraphs import start, text
from test_native_response_timestamps import completed


def snap(root, sid, binding, *, live=True, worker=None):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id if worker is None else worker,
        max_turns=50,
        live_text=live,
    )
    return structured_runtime._snapshot(binding.session_key, raw)["turns"][-1]


def ended(op, state, error=""):
    data = {"operation_id": op, "native_turn_id": "n", "state": state, "error": error}
    return {"kind": "turn_completed", "data": {**data, "background_active": False}}


def approval(op, binding):
    data = {
        "operation_id": op,
        "native_turn_id": "n",
        "request_id": "s:approve-1",
        "item_id": "cmd-2",
        "tool": "commandExecution",
        "summary": "rm -rf build",
        "choices": ["approve", "reject"],
        "payload_digest": "a" * 64,
        "worker_id": binding.worker_id,
        "connection_id": binding.connection_id,
        "complete": True,
    }
    return {"kind": "approval", "data": data}


def tool(op, item="c1"):
    data = {"operation_id": op, "native_turn_id": "n", "item_id": item, "tool": "commandExecution"}
    return {"kind": "tool", "data": {**data, "state": "completed", "summary": "npm test"}}


def delta(op, body, item=None):
    data = {"operation_id": op, "native_turn_id": "n", "text": body, "partial": True}
    named = {"item_id": item} if item else {}
    return {"kind": "text", "data": {**data, "truncated": False, **named}}


@pytest.mark.parametrize(
    "adapter, first, second",
    [
        # Codex: deltas name their message, and the settled block names the same one.
        ("codex-app-server", "m1", "m2"),
        # Claude: deltas name none; the settled block names the message id.
        ("claude-stream-json", None, None),
    ],
)
def test_the_message_streaming_after_a_settled_one_shows_with_a_tool_row_between(
    conversation, adapter, first, second
):
    root, sid, binding, op = start(conversation, adapter)
    journal.append_events(
        root,
        binding,
        [
            delta(op, "Looking at the ", first),
            delta(op, "code.", first),
            text(op, "Looking at the code.", item=first or "msg_1", partial=False),
            tool(op),
            delta(op, "Running ", second),
            delta(op, "the tests", second),
        ],
    )
    turn = snap(root, sid, binding)
    assert turn["state"] == "running"
    assert turn["reply"] == "Looking at the code.\n\nRunning the tests"
    assert turn["reply_breaks"] == [0, len("Looking at the code.\n\n")]
    assert turn["stream"] == {"item": second}
    # The command sits between the two messages: after the first, before the second.
    (call,) = turn["tools"]
    assert call["at"] == len("Looking at the code.")
    # Every other reader: the settled reply alone, as before, and nothing to extend.
    other = snap(root, sid, binding, live=False)
    assert other["reply"] == "Looking at the code." and "stream" not in other
    assert other["tools"][0]["at"] == len("Looking at the code.")


def test_a_message_settling_while_streaming_shows_once_and_the_next_one_opens(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [
            text(op, "One.", item="m1", partial=False),
            tool(op),
            delta(op, "Two so", "m2"),
            delta(op, " far", "m2"),
        ],
    )
    assert snap(root, sid, binding)["reply"] == "One.\n\nTwo so far"
    # The second message settles: its settled text replaces the streamed one, never adds to it.
    journal.append_events(root, binding, [text(op, "Two so far.", item="m2", partial=False)])
    settled = snap(root, sid, binding)
    assert settled["reply"] == "One.\n\nTwo so far."
    assert settled["reply"].count("Two") == 1
    assert "stream" not in settled  # nothing streams until the next message's first words
    assert settled["tools"][0]["at"] == len("One.")
    # The third message opens, a paragraph after the second.
    journal.append_events(root, binding, [delta(op, "Three", "m3")])
    opened = snap(root, sid, binding)
    assert opened["reply"] == "One.\n\nTwo so far.\n\nThree"
    assert opened["reply_breaks"] == [0, len("One.\n\n"), len("One.\n\nTwo so far.\n\n")]
    assert opened["stream"] == {"item": "m3"}


def test_a_message_switch_inside_the_stream_breaks_a_paragraph_and_names_the_new_one(
    conversation,
):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [
            text(op, "Settled.", item="m1", partial=False),
            delta(op, "Second", "m2"),
            delta(op, "Third", "m3"),
        ],
    )
    turn = snap(root, sid, binding)
    assert turn["reply"] == "Settled.\n\nSecond\n\nThird"
    assert turn["reply_breaks"] == [0, len("Settled.\n\n"), len("Settled.\n\nSecond\n\n")]
    assert turn["stream"] == {"item": "m3"}


@pytest.mark.parametrize("how", ["interrupted", "failed", "worker gone", "awaiting approval"])
def test_a_turn_that_stops_streaming_keeps_the_words_the_operator_watched_arrive(conversation, how):
    """Review of #1760: Stop, Send now, a failure or the worker's death left only the settled text,
    so the message just watched streaming vanished (an interrupted FIRST message keeps its partial).
    It stays, without `stream`: only a pending turn streams. So it does while an approval waits."""
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [text(op, "One.", item="m1", partial=False), tool(op), delta(op, "Two so far", "m2")],
    )
    worker = None
    if how in ("interrupted", "failed"):
        journal.append_events(root, binding, [ended(op, how, "boom" if how == "failed" else "")])
    elif how == "worker gone":
        worker = "00000000-0000-4000-8000-000000000000"  # another generation is the live one
    else:
        journal.append_events(root, binding, [approval(op, binding)])
    turn = snap(root, sid, binding, worker=worker)
    expected = {
        "interrupted": "interrupted",
        "failed": "failed",
        "worker gone": "uncertain",
        "awaiting approval": "awaiting_approval",
    }[how]
    assert turn["state"] == expected
    assert turn["reply"] == "One.\n\nTwo so far"
    assert turn["reply_breaks"] == [0, len("One.\n\n")]
    assert turn["tools"][0]["at"] == len("One.")
    assert "stream" not in turn
    # The default projection (public link, missions) is still the settled reply alone.
    assert snap(root, sid, binding, live=False, worker=worker)["reply"] == "One."


def test_a_normally_completed_turn_shows_its_settled_reply_and_no_stream(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [
            text(op, "One.", item="m1", partial=False),
            delta(op, "A draft never settled", "m2"),
            completed(op),
        ],
    )
    turn = snap(root, sid, binding)
    assert turn["state"] == "completed"
    assert turn["reply"] == "One." and "stream" not in turn


def test_blank_streamed_text_after_a_settled_message_shows_nothing_new(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root, binding, [text(op, "One.", item="m1", partial=False), delta(op, "  ", "m2")]
    )
    turn = snap(root, sid, binding)
    assert turn["reply"] == "One." and "stream" not in turn


def test_an_agents_own_turn_never_streams(conversation):
    """Its own turns (#1438) show settled messages only, live or not."""
    root, sid, binding, _ = start(conversation, "codex-app-server")
    target = str(uuid.uuid4())
    streaming = own(target, "Now the")
    streaming["data"]["partial"] = True
    journal.append_events(root, binding, [background(target), own(target, "Checked."), streaming])
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        live_text=True,
    )
    (agent,) = [t for t in raw["turns"] if t.get("kind") == "agent"]
    assert agent["reply"] == "Checked." and "stream" not in agent


BIG = 10_000  # one delta's text; MAX_TEXT bounds each event


def test_a_long_message_in_progress_never_moves_the_window_or_its_tool_row(conversation):
    """Review of #1760: past the 20k cap the window was chosen over the joined reply, so a long
    second message slid it on every snapshot and dropped message one and its tool row."""
    root, sid, binding, op = start(conversation, "codex-app-server")
    first = "Plan: " + "a" * 94
    journal.append_events(root, binding, [text(op, first, item="m1", partial=False), tool(op)])
    seen = []
    for _ in range(4):  # 10k, 20k, 30k, 40k of message two
        journal.append_events(root, binding, [delta(op, "b" * BIG, "m2")])
        turn = snap(root, sid, binding)
        assert turn["reply"].startswith(first + "\n\n")
        assert not turn["first_message_partial"] and turn["reply_omitted"] is None
        assert [t["at"] for t in turn["tools"]] == [len(first)]
        assert turn["reply_breaks"][:2] == [0, len(first) + 2]
        assert turn["stream"] == {"item": "m2"}
        seen.append(len(turn["reply"]))
    # Its own part is capped at the per-reply 20k, the newest words last.
    assert seen == [len(first) + 2 + n for n in (BIG, 2 * BIG, 2 * BIG, 2 * BIG)]


def test_two_settled_messages_keep_their_window_while_a_third_grows(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    one, two = "x" * 9_000, "y" * 9_000
    journal.append_events(
        root,
        binding,
        [
            text(op, one, item="m1", partial=False),
            tool(op),
            text(op, two, item="m2", partial=False),
        ],
    )
    for _ in range(3):  # the third grows past the 20k the three would share
        journal.append_events(root, binding, [delta(op, "z" * 2_000, "m3")])
        turn = snap(root, sid, binding)
        assert turn["reply"].startswith(one + "\n\n" + two + "\n\n")
        assert turn["reply_breaks"][:3] == [0, 9_002, 18_004]
        assert [t["at"] for t in turn["tools"]] == [9_000]
        assert turn["reply_omitted"] is None
    # It settles: the window is the settled reply's again, chosen exactly as before this change.
    journal.append_events(root, binding, [text(op, "z" * 6_000, item="m3", partial=False)])
    settled = snap(root, sid, binding)
    assert "stream" not in settled and settled["reply"].endswith("z" * 6_000)


def test_a_message_in_progress_past_200k_still_shows_its_newest_words(conversation):
    """Above the projection's 200k kept window the message's start is gone; it used to vanish."""
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(root, binding, [text(op, "One.", item="m1", partial=False)])
    for i in range(21):
        journal.append_events(root, binding, [delta(op, chr(ord("a") + i % 26) * BIG, "m2")])
    turn = snap(root, sid, binding)
    assert turn["reply"].startswith("One.\n\n")
    assert turn["reply"].endswith("u" * BIG)  # the newest delta, whole
    assert len(turn["reply"]) == len("One.\n\n") + 20_000
    assert turn["stream"] == {"item": "m2"}  # the pane still extends its end


def _raw(root, sid, binding):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=50,
        live_text=True,
    )
    return raw


@pytest.mark.parametrize("length", [20_000, 20_001])
@pytest.mark.parametrize("how", ["pending", "interrupted"])
def test_a_second_message_follows_a_first_one_at_and_past_the_cap(conversation, length, how):
    """Hermes on #1760: one settled message longer than the 20k cap is shown as its head with a
    cut marker, and the message streaming after it was dropped with its `stream` (and lost on
    interrupt). It follows as its own part now; the cut keeps its marker, in the text.

    A settled text event is itself bounded at 20,000 characters (native_ipc.MAX_EVENT_TEXT), so
    20,001 is the projected turn at 20,000 with one more character before the snapshot cuts it —
    the cut a non-live turn also meets when the shared text budget leaves it less."""
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [text(op, "a" * 20_000, item="m1", partial=False), tool(op), delta(op, "Two", "m2")],
    )
    journal.append_events(root, binding, [delta(op, " more", "m2")])
    if how == "interrupted":
        journal.append_events(root, binding, [ended(op, "interrupted")])
    raw = _raw(root, sid, binding)
    first = "a" * length
    for turn in raw["turns"]:
        turn["reply"] = first
        turn["tools"] = [{**t, "at": length} for t in turn["tools"]]
    (turn,) = structured_runtime._snapshot(binding.session_key, raw)["turns"]
    head = first if length <= 20_000 else first[:20_000] + "…"
    assert turn["reply"] == head + "\n\nTwo more"
    assert turn["reply_breaks"] == [0, len(head) + 2]
    assert turn["reply_truncated"] is False  # the marker is in the text, before message two
    assert [t["at"] for t in turn["tools"]] == [len(head)]  # after the first message, cut or not
    if how == "pending":
        assert turn["stream"] == {"item": "m2"}
    else:
        assert turn["state"] == "interrupted" and "stream" not in turn
    # Every other reader: the bounded first message alone, its cut flagged as before.
    for t in raw["turns"]:
        t.pop("reply_open", None), t.pop("reply_open_breaks", None), t.pop("stream", None)
    (other,) = structured_runtime._snapshot(binding.session_key, raw)["turns"]
    assert other["reply"] == first[:20_000] and other["reply_truncated"] is (length > 20_000)


def _another_turn(root, sid, binding, reply):
    """One more completed operator turn with a settled `reply`."""
    from test_native_journal import request

    claim = request(binding, revision=journal.read(root, sid).revision)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [text(op, reply, item="m1", partial=False), completed(op)])
    return op


@pytest.mark.parametrize("after", [9, 10])
def test_a_stopped_message_is_shown_whatever_the_shared_text_budget_left(conversation, after):
    """Hermes on #1760: with the snapshot's shared 200,000-character budget spent by newer turns,
    the stopped message's words vanished — no cut, nothing marked not loaded, and no history page
    could bring them back. The message in progress has its own slot outside that budget (up to
    20,000, its newest words), so it is always shown; the settled reply keeps its budgeted read
    (with nine newer turns some of it, with ten none: it then reads as not loaded, as before)."""
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(
        root,
        binding,
        [
            text(op, "s" * 20_000, item="m1", partial=False),
            delta(op, "VISIBLE WHILE RUNNING", "m2"),
            ended(op, "interrupted"),
        ],
    )
    for _ in range(after):
        _another_turn(root, sid, binding, "n" * 20_000)
    turns = structured_runtime._snapshot(binding.session_key, _raw(root, sid, binding))["turns"]
    stopped = next(t for t in turns if t["turn_id"] == op)
    assert stopped["state"] == "interrupted" and "stream" not in stopped
    assert stopped["reply"].endswith("VISIBLE WHILE RUNNING")
    if after == 10:
        assert stopped["reply"] == "VISIBLE WHILE RUNNING"
        assert stopped["reply_omitted"] == {"messages": 1, "chars": 20_000}
    # Bounded: the shared budget, plus at most one 20k slot per message in progress.
    sent = sum(len(t.get("text") or "") + len(t.get("reply") or "") for t in turns)
    assert sent <= structured_runtime.SNAPSHOT_TEXT_BUDGET + structured_runtime.SNAPSHOT_TEXT
    # The default projection (the public link, server-side readers) still never carries them.
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=50,
    )
    default = structured_runtime._snapshot(binding.session_key, raw)["turns"]
    assert not any("VISIBLE" in (t.get("reply") or "") for t in default)
