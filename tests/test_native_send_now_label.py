"""#1442: a reply Send now cut off names the message that cut it — only when it really did."""

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_journal import request

conversation = journal_tests.conversation


def snapshot(root, sid, binding):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )
    return {
        t["turn_id"]: t for t in structured_runtime._snapshot(binding.session_key, raw)["turns"]
    }


def cut_by_send_now(root, sid, binding, control_handoff, refused=False, message="sent"):
    """An operator turn running, a message queued behind it, Send now (interrupt) for it; the
    running turn then settles interrupted and the message goes out."""
    running = request(binding)
    target = running["params"]["operation_id"]
    journal.claim(root, running)
    journal.record_handoff(root, binding, target, "sent")
    outcome = message
    queued = request(binding, revision=journal.read(root, sid).revision)
    message = queued["params"]["operation_id"]
    journal.claim(root, queued, queued=True)
    control = request(
        binding,
        action="send_now",
        turn_id=target,
        queued_turn_id=message,
        mode="interrupt",
        revision=journal.read(root, sid).revision,
    )
    journal.claim(root, control)
    journal.record_handoff(root, binding, control["params"]["operation_id"], control_handoff)
    events = []
    if refused:
        events.append(
            {
                "kind": "delivery_failed",
                "data": {
                    "operation_id": message,
                    "native_turn_id": "native-turn-1",
                    "control_id": control["params"]["operation_id"],
                    "message": "the agent refused interruption",
                },
            }
        )
    events.append(
        {
            "kind": "turn_completed",
            "data": {
                "operation_id": target,
                "native_turn_id": "native-turn-1",
                "state": "interrupted",
                "error": "",
            },
        }
    )
    journal.append_events(root, binding, events)
    if outcome == "sent":
        journal.record_handoff(root, binding, message, "uncertain")
        journal.record_handoff(root, binding, message, "sent")
    elif outcome == "not_sent":  # admission refused it after the interrupt went
        journal.record_handoff(root, binding, message, "not_sent")
    # "queued": it still waits — the interrupt went, the message has not
    return target, message


def test_a_sent_send_now_interrupt_keeps_naming_the_turn_it_cut(conversation):
    root, sid, binding = conversation
    target, message = cut_by_send_now(root, sid, binding, "sent")
    turns = snapshot(root, sid, binding)
    assert turns[target]["state"] == "interrupted"
    assert turns[message].get("delivery_turn_id") == target
    assert "delivery" not in turns[message]  # it went out: no longer "interrupting"


@pytest.mark.parametrize(
    ("handoff", "refused"), [("uncertain", False), ("sent", True)], ids=["uncertain", "refused"]
)
def test_a_send_now_that_may_not_have_cut_it_names_nothing(conversation, handoff, refused):
    """An uncertain control, or one the agent refused: the turn ended interrupted for some
    other reason (the Interrupt button, Esc), and nothing may claim Send now did it."""
    root, sid, binding = conversation
    target, message = cut_by_send_now(root, sid, binding, handoff, refused=refused)
    turns = snapshot(root, sid, binding)
    assert turns[target]["state"] == "interrupted"
    assert "delivery_turn_id" not in turns[message]


def test_a_message_that_never_went_out_claims_nothing_even_after_its_interrupt_went(
    conversation,
):
    """Hermes on #1451: the control was sent and the target stopped, but the queued message was
    then refused (`not_sent`). Nothing is "answered below": no link, and the target keeps its
    plain interrupted state."""
    root, sid, binding = conversation
    target, message = cut_by_send_now(root, sid, binding, "sent", message="not_sent")
    turns = snapshot(root, sid, binding)
    assert turns[target]["state"] == "interrupted"
    assert turns[message]["state"] == "failed"
    assert "delivery_turn_id" not in turns[message]


def test_while_the_message_still_waits_the_snapshot_says_so(conversation):
    """Hermes on #1451: the window between the interrupt and the message going out. The projector
    keeps the message `queued` + `interrupting` with its link (that drives "sends next"); this is
    the exact shape `web/e2e/structured-send-now-queue.spec.ts` feeds the pane, which must NOT
    claim "answered below" for it."""
    root, sid, binding = conversation
    target, message = cut_by_send_now(root, sid, binding, "sent", message="queued")
    turns = snapshot(root, sid, binding)
    assert turns[target]["state"] == "interrupted"
    waiting = turns[message]
    assert waiting["state"] == "queued"
    assert waiting["delivery"] == "interrupting" and waiting["delivery_turn_id"] == target
