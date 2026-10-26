"""Message boundaries for the long-turn fold (#1643): recorded where each agent message starts,
carried through the 200k kept window and the 20k snapshot slice, so a long turn sends its NEWEST
whole messages and says truthfully what it left out."""

from __future__ import annotations

import pytest

import test_native_agent_turns as agent_tests
import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, share_api, structured_runtime
from test_native_journal import request

conversation = journal_tests.conversation
CAP = structured_runtime.SNAPSHOT_TEXT


def text(operation_id, body, *, partial=False, item="item-1"):
    return {
        "kind": "text",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "item_id": item,
            "text": body,
            "partial": partial,
            "truncated": False,
        },
    }


def tool(operation_id, item):
    return {
        "kind": "tool",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "item_id": item,
            "tool": "Bash",
            "summary": f"echo {item}",
            "state": "completed",
        },
    }


def turn_with(root, sid, binding, events):
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    batch = [e(operation_id) for e in events]
    for i in range(0, len(batch), 50):  # the journal takes bounded batches
        journal.append_events(root, binding, batch[i : i + 50])
    return operation_id


def streamed(body, item, size=15_000):
    """One long message as the CLI streams it: pieces of one item (a settled block is bounded)."""
    return [
        (lambda o, piece=body[i : i + size]: text(o, piece, partial=True, item=item))
        for i in range(0, len(body), size)
    ]


def projected(root, sid, binding):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )


def sent(root, sid, binding, budget=None):
    (raw,) = [t for t in projected(root, sid, binding)["turns"] if t.get("kind") != "agent"]
    return structured_runtime._turn(raw, budget)


def messages(view):
    breaks = view["reply_breaks"]
    return [view["reply"][a:b].strip() for a, b in zip(breaks, [*breaks[1:], None], strict=True)]


def test_each_settled_message_starts_where_it_starts_even_with_its_own_blank_lines(conversation):
    root, sid, binding = conversation
    bodies = ["First step.", "A list:\n\n- one\n\n- two", "Done."]
    turn_with(root, sid, binding, [lambda o, b=b: text(o, b) for b in bodies])
    view = sent(root, sid, binding)
    assert messages(view) == bodies
    assert view["first_message_partial"] is False and view["reply_omitted"] is None


def test_streamed_pieces_of_one_message_are_one_message(conversation):
    root, sid, binding = conversation
    turn_with(
        root,
        sid,
        binding,
        [
            lambda o: text(o, "Read", partial=True, item="a"),
            lambda o: text(o, "ing.", partial=True, item="a"),
            lambda o: text(o, "Next one.", partial=True, item="b"),
        ],
    )
    assert messages(sent(root, sid, binding)) == ["Reading.", "Next one."]


def test_a_long_turn_sends_its_newest_whole_messages_and_counts_the_rest(conversation):
    root, sid, binding = conversation
    bodies = [f"Update {i:02}: " + "x" * 989 for i in range(60)]  # 1,000 each, ~60k
    events = []
    for i, b in enumerate(bodies):
        events += [lambda o, b=b: text(o, b), lambda o, i=i: tool(o, f"t{i}")]
    turn_with(root, sid, binding, events)
    view = sent(root, sid, binding)
    shown = messages(view)
    assert shown == bodies[-len(shown) :] and len(view["reply"]) <= CAP
    assert len(shown) == 19  # whole messages only: a 20th would not fit
    assert view["first_message_partial"] is False
    assert view["reply_omitted"]["messages"] == 60 - 19
    assert view["reply_omitted"]["chars"] == len(
        projected(root, sid, binding)["turns"][0]["reply"]
    ) - len(view["reply"])
    # Calls before the slice are history this view does not hold: left out, never piled onto
    # the first shown message; the ones shown sit after the message they followed.
    assert [t["id"] for t in view["tools"]] == [f"t{i}" for i in range(41, 60)]
    assert view["tools_truncated"] is True
    first_call = view["tools"][0]
    assert view["reply"][: first_call["at"]].strip() == bodies[41]


def test_the_kept_window_cutting_inside_a_message_is_a_partial_first_message(conversation):
    root, sid, binding = conversation
    # Eleven 19k messages: 209k, so the 200k kept window begins inside the first.
    bodies = [chr(ord("A") + i) * 19_000 for i in range(11)]
    turn_with(root, sid, binding, [lambda o, b=b: text(o, b) for b in bodies])
    raw = projected(root, sid, binding)["turns"][0]
    assert raw["reply_messages_cut"] == 1 and raw["reply_breaks"][0] > 0
    assert raw["reply_chars_cut"] > 0
    # The 20k slice then holds the newest whole message only: ten are not loaded, the one the
    # window cut inside included, and nothing shown is partial.
    view = sent(root, sid, binding)
    assert messages(view) == [bodies[-1]] and view["first_message_partial"] is False
    assert view["reply_omitted"]["messages"] == 10


def test_one_retained_start_after_a_window_cut_still_sends_the_newest_message(conversation):
    # Hermes on #1651: a streamed 210k message, then the answer. The 200k kept window cuts into
    # the first, so ONE message start survives; the answer must be what is sent, not the head.
    root, sid, binding = conversation
    turn_with(
        root,
        sid,
        binding,
        [*streamed("w" * 210_000, "a"), lambda o: text(o, "FINAL ANSWER", partial=True, item="b")],
    )
    raw = projected(root, sid, binding)["turns"][0]
    assert len(raw["reply_breaks"]) == 1 and raw["reply_breaks"][0] > 0
    view = sent(root, sid, binding)
    assert view["reply"] == "FINAL ANSWER"
    assert view["reply_breaks"] == [0] and view["first_message_partial"] is False
    assert view["reply_omitted"]["messages"] == 1
    assert all(b < len(view["reply"]) for b in view["reply_breaks"])


def test_the_newest_message_alone_over_the_cap_is_its_tail_marked_partial(conversation):
    root, sid, binding = conversation
    newest = "head-" + "y" * (CAP + 5000) + "-tail"
    turn_with(
        root,
        sid,
        binding,
        [lambda o: text(o, "Short first.", partial=True, item="a"), *streamed(newest, "b")],
    )
    view = sent(root, sid, binding)
    assert view["reply"] == newest[-CAP:] and view["reply"].endswith("-tail")
    assert view["reply_breaks"] == [0] and view["first_message_partial"] is True
    assert view["reply_truncated"] is False
    assert view["reply_omitted"] == {"messages": 1, "chars": len("Short first.\n\n") + 5010}


def test_what_the_shared_budget_leaves_is_spent_on_the_newest_message(conversation):
    root, sid, binding = conversation
    turn_with(root, sid, binding, [lambda o: text(o, "a" * 300), lambda o: text(o, "b" * 300)])
    asked = len("Inspect the project")  # the operator's own text is spent from it first
    view = sent(root, sid, binding, budget=[asked + 350])
    assert view["reply"] == "b" * 300 and view["first_message_partial"] is False
    tail = sent(root, sid, binding, budget=[asked + 120])
    assert tail["reply"] == "b" * 120 and tail["first_message_partial"] is True
    assert tail["reply_omitted"]["messages"] == 1


def test_a_spent_budget_sends_no_text_and_says_so(conversation):
    root, sid, binding = conversation
    turn_with(root, sid, binding, [lambda o: text(o, "a" * 30), lambda o: text(o, "b" * 30)])
    view = sent(root, sid, binding, budget=[len("Inspect the project")])
    assert view["reply"] == "" and view["reply_breaks"] == []
    assert view["first_message_partial"] is False
    assert view["reply_omitted"] == {"messages": 2, "chars": 62}


def test_a_spent_budget_counts_a_single_message_reply_as_not_loaded(conversation, monkeypatch):
    # Hermes on #1651: one message, no budget left — it must not vanish without a notice.
    root, sid, binding = conversation
    turn_with(root, sid, binding, [lambda o: text(o, "Only answer.")])
    view = sent(root, sid, binding, budget=[len("Inspect the project")])
    assert view["reply"] == "" and view["reply_breaks"] == []
    assert view["reply_omitted"] == {"messages": 1, "chars": len("Only answer.")}
    target = agent_tests.agent(root, sid, binding, monkeypatch, agent_tests.T0, "Own answer.")
    (raw,) = [t for t in projected(root, sid, binding)["turns"] if t["turn_id"] == target]
    own = structured_runtime._turn(raw, [0])
    assert own["reply"] == "" and own["reply_omitted"]["messages"] == 1


def test_one_long_message_keeps_its_head_as_always(conversation):
    root, sid, binding = conversation
    body = "start-" + "z" * (CAP + 10)
    turn_with(root, sid, binding, streamed(body, "only"))
    view = sent(root, sid, binding)
    assert view["reply"] == body[:CAP] and view["reply_truncated"] is True
    assert view["reply_breaks"] == [0] and view["first_message_partial"] is False


def test_an_agent_run_turn_gets_the_same_shape(conversation, monkeypatch):
    root, sid, binding = conversation
    target = agent_tests.agent(root, sid, binding, monkeypatch, agent_tests.T0, "Watching CI")
    (raw,) = [t for t in projected(root, sid, binding)["turns"] if t["turn_id"] == target]
    view = structured_runtime._turn(raw)
    assert view["kind"] == "agent" and view["reply_breaks"] == [0]
    assert view["first_message_partial"] is False and view["reply_omitted"] is None


def test_a_projection_without_boundaries_folds_nothing():
    view = structured_runtime._turn(
        {"turn_id": "t", "status": "completed", "text": "q", "reply": "a\n\nb", "tools": []}
    )
    assert "reply_breaks" not in view and "reply_omitted" not in view


@pytest.mark.parametrize("partial", [True, False])
def test_the_public_link_trims_a_reply_that_begins_mid_message(partial):
    turn = {
        "kind": "operator",
        "turn_id": "t",
        "operation_id": "t",
        "state": "completed",
        "text": "q",
        "reply": "Z" * 40 + " the rest",
        "reply_truncated": False,
        "first_message_partial": partial,
        "tools": [],
    }
    public = share_api._turn(turn, {}, share_api._Redactor([]), "share-1")
    assert public["reply"].startswith(share_api.CUT_MARK) is partial
    assert "reply_breaks" not in public and "first_message_partial" not in public
