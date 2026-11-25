"""An API agent's structured question shows its options (#1463).

Codex 0.162 sends an async question as a settled ``agentMessage`` carrying ``delivery: "async"`` and
``questions: [{title, options}]`` (``AsyncUserInputQuestion`` in ``codex app-server
generate-json-schema``). The options travel as data to the snapshot; prose never becomes buttons.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_ipc, native_questions
from agent_sessions import native_journal as journal
from test_native_protocol import SESSION, TURN, codex_running
from test_native_reply_paragraphs import start
from test_native_response_timestamps import completed, snapshot

conversation = journal_tests.conversation

ASKED = [
    {
        "title": "Should project restrictions also isolate the host?",
        "options": [
            "Enforce project isolation (Recommended)",
            "UI/API restrictions for trusted teammates only",
        ],
    }
]


def message(method="item/completed", **item):
    return {
        "method": method,
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "item": {
                "type": "agentMessage",
                "id": "call_1",
                "text": "Should project restrictions also isolate the host?\n- Enforce …",
                **item,
            },
        },
    }


def test_codex_async_question_carries_its_options():
    [event] = codex_running().feed(message(delivery="async", questions=ASKED))
    assert event.kind == "text" and event.data["partial"] is False
    assert event.data["questions"] == ASKED
    # The boundary the worker crosses accepts exactly that shape.
    assert native_ipc.normalize_event({"kind": "text", "data": event.data})["data"] == event.data


@pytest.mark.parametrize(
    "item",
    [
        {"questions": ASKED},  # not an async delivery: an ordinary message
        {"delivery": "async"},  # no structured options
        {"delivery": "async", "questions": None},
        {"delivery": "async", "questions": [{"title": "Pick", "options": []}]},
        {"delivery": "async", "questions": [{"title": "", "options": ["a"]}]},
        {"delivery": "async", "questions": [{"title": "Pick", "options": "a, b"}]},
        {"delivery": "async", "questions": [ASKED[0], "not a question"]},
        {"delivery": "async", "questions": [ASKED[0]] * (native_questions.MAX_QUESTIONS + 1)},
    ],
)
def test_no_valid_structure_means_no_buttons_and_the_text_still_shows(item):
    [event] = codex_running().feed(message(**item))
    assert "questions" not in event.data
    assert event.data["text"].startswith("Should project restrictions")


def test_a_started_message_is_never_a_question():
    [event] = codex_running().feed(
        message(method="item/started", delivery="async", questions=ASKED)
    )
    assert event.data["partial"] is True and "questions" not in event.data


def test_labels_are_cleaned_capped_and_deduplicated():
    [event] = codex_running().feed(
        message(
            delivery="async",
            questions=[
                {
                    "title": "Pick\x1b[31m one" + "x" * 2000,
                    "options": ["Red\x07", "Red", "", "  ", "y" * 500, 7],
                }
            ],
        )
    )
    [question] = event.data["questions"]
    assert question["title"].startswith("Pick[31m one")
    assert len(question["title"]) == native_questions.TITLE_MAX
    assert question["options"] == ["Red", "y" * native_questions.OPTION_MAX]


@pytest.mark.parametrize(
    "title,option",
    [
        ("a" * (native_questions.TITLE_MAX - 1) + " b", "Red"),
        ("Pick", "y" * (native_questions.OPTION_MAX - 1) + " z"),
        ("  " + "a" * native_questions.TITLE_MAX, "  " + "y" * native_questions.OPTION_MAX),
    ],
)
def test_a_cut_on_a_space_still_crosses_the_journal_boundary(title, option):
    """The codec's output is canonical: cleaning it again changes nothing (Hermes #6464)."""
    [event] = codex_running().feed(
        message(delivery="async", questions=[{"title": title, "options": [option, "Blue"]}])
    )
    [question] = event.data["questions"]
    assert question["title"] == question["title"].strip()
    assert len(question["title"]) <= native_questions.TITLE_MAX
    assert all(
        o == o.strip() and len(o) <= native_questions.OPTION_MAX for o in question["options"]
    )
    assert native_ipc.normalize_event({"kind": "text", "data": event.data})["data"] == event.data


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [{"title": "Pick", "options": ["a"], "extra": 1}],
        [{"title": "Pick\x07", "options": ["a"]}],
        [{"title": "Pick", "options": ["a", "a"]}],
        [{"title": "Pick", "options": ["x" * (native_questions.OPTION_MAX + 1)]}],
        "Pick: a, b",
    ],
)
def test_the_journal_boundary_refuses_anything_the_codec_would_not_produce(questions):
    data = {
        "operation_id": "f84b4fa9-d57b-45c1-a219-26dc19b7f661",
        "native_turn_id": TURN,
        "text": "Pick",
        "partial": False,
        "truncated": False,
        "questions": questions,
    }
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "text", "data": data})


def _text(op, body, **extra):
    event = journal_tests.event(op, body)
    event["data"].update({"item_id": "m1", "partial": False, **extra})
    return event


def test_the_snapshot_turn_carries_the_newest_question(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    later = [{"title": "And the channel?", "options": ["main", "stable"]}]
    journal.append_events(
        root,
        binding,
        [
            _text(op, "First question", questions=ASKED, item_id="q1"),
            _text(op, "Plain prose\n- looks like\n- a menu"),
            _text(op, "Second question", questions=later, item_id="q2"),
            completed(op),
        ],
    )
    turn = snapshot(root, sid, binding)["turns"][0]
    assert turn["questions"] == later
    # The id an answer names: the settled message that carried the newest question.
    assert turn["question_id"] == "q2"


def test_a_turn_without_questions_has_no_key(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(root, binding, [_text(op, "- a\n- b"), completed(op)])
    turn = snapshot(root, sid, binding)["turns"][0]
    assert "questions" not in turn and "question_id" not in turn


# An answer names its question (`reply_to`), and the journal admits it only while that question
# is still open, inside the claim transaction. That is what makes an identical-id Retry safe:
# it replays, or it is refused once the question closed; it can never answer a newer one.


def _asked(conversation, item_id="q1"):
    root, sid, binding, op = start(conversation, "codex-app-server")
    journal.append_events(root, binding, [_text(op, "Pick", questions=ASKED, item_id=item_id)])
    return root, sid, binding, op


def _answer(root, sid, binding, reply_to="q1", **changes):
    return journal_tests.request(
        binding,
        revision=journal.read(root, sid).revision,
        text="Enforce project isolation (Recommended)",
        context={},
        reply_to=reply_to,
        **changes,
    )


def _refused(root, claim, **kw):
    with pytest.raises(journal.JournalError, match="already answered") as exc:
        journal.claim(root, claim, **kw)
    assert exc.value.code == "conflict"


def test_an_answer_to_the_open_question_is_admitted_and_replays(conversation):
    root, sid, binding, _ = _asked(conversation)
    answer = _answer(root, sid, binding)
    fresh, receipt = journal.claim(root, answer, queued=True)
    assert fresh
    # The binding is part of the operation's identity: a retry replays the original, even
    # though the question is closed now, and a changed binding conflicts.
    retry = {**answer, "params": {**answer["params"], "expected_revision": 999}}
    assert journal.claim(root, retry) == (False, receipt)
    rebound = {**answer, "params": {**answer["params"], "reply_to": "q9"}}
    with pytest.raises(journal.JournalError, match="another request"):
        journal.claim(root, rebound)


def test_a_second_answer_to_the_same_question_is_refused(conversation):
    root, sid, binding, _ = _asked(conversation)
    assert journal.claim(root, _answer(root, sid, binding), queued=True)[0]
    before = journal_tests.path_of(conversation).read_bytes()
    _refused(root, _answer(root, sid, binding), queued=True)
    assert journal_tests.path_of(conversation).read_bytes() == before


def test_a_newer_question_closes_the_older_one(conversation):
    root, sid, binding, op = _asked(conversation)
    later = [{"title": "And the channel?", "options": ["main"]}]
    journal.append_events(root, binding, [_text(op, "Again", questions=later, item_id="q2")])
    _refused(root, _answer(root, sid, binding, reply_to="q1"))
    assert journal.claim(root, _answer(root, sid, binding, reply_to="q2"))[0]


def test_an_identical_repeat_is_a_new_question(conversation):
    root, sid, binding, op = _asked(conversation)
    journal.append_events(root, binding, [_text(op, "Pick", questions=ASKED, item_id="q2")])
    _refused(root, _answer(root, sid, binding, reply_to="q1"))


def test_an_unknown_question_or_none_at_all_is_refused(conversation):
    root, sid, binding, op = start(conversation, "codex-app-server")
    _refused(root, _answer(root, sid, binding))
    journal.append_events(root, binding, [_text(op, "- a\n- b")])
    _refused(root, _answer(root, sid, binding))


def test_any_operator_message_claimed_after_the_question_closes_it(conversation):
    root, sid, binding, _ = _asked(conversation)
    typed = journal_tests.request(
        binding, revision=journal.read(root, sid).revision, text="my own answer", context={}
    )
    journal.claim(root, typed)
    journal.record_handoff(root, binding, typed["params"]["operation_id"], "sent")
    _refused(root, _answer(root, sid, binding))


def test_a_message_waiting_in_the_queue_closes_it_even_if_claimed_before(conversation):
    root, sid, binding = conversation
    binding = replace(binding, adapter="codex-app-server")
    running = journal_tests.request(binding, context={})
    journal.claim(root, running)
    journal.record_handoff(root, binding, running["params"]["operation_id"], "sent")
    waiting = journal_tests.request(binding, revision=journal.read(root, sid).revision, context={})
    journal.claim(root, waiting, queued=True)
    op = running["params"]["operation_id"]
    journal.append_events(root, binding, [_text(op, "Pick", questions=ASKED, item_id="q1")])
    # The queued message reaches Codex after the question, so it is the reply.
    _refused(root, _answer(root, sid, binding), queued=True)


def test_a_queued_message_of_a_stopped_worker_does_not_close_it(conversation):
    root, sid, binding = conversation
    old = replace(binding, adapter="codex-app-server")
    stranded = journal_tests.request(old, context={})
    journal.claim(root, stranded, queued=True)
    new = replace(old, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    running = journal_tests.request(new, revision=journal.read(root, sid).revision, context={})
    journal.claim(root, running)
    journal.record_handoff(root, new, running["params"]["operation_id"], "sent")
    op = running["params"]["operation_id"]
    journal.append_events(root, new, [_text(op, "Pick", questions=ASKED, item_id="q1")])
    assert journal.claim(root, _answer(root, sid, new), queued=True)[0]


def test_concurrent_answers_admit_exactly_one(conversation):
    root, sid, binding, _ = _asked(conversation)
    first, second = _answer(root, sid, binding), _answer(root, sid, binding)
    second["params"]["text"] = "UI/API restrictions for trusted teammates only"

    def attempt(claim):
        try:
            return journal.claim(root, claim, queued=True)[0]
        except journal.JournalError as exc:
            assert exc.code == "conflict"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [first, second]))
    assert sorted(outcomes) == [False, True]
    reopened = journal.read(root, sid)
    answers = [op for op in reopened.operations.values() if "reply_to" in op.request["params"]]
    assert len(answers) == 1


def test_the_ipc_boundary_bounds_reply_to(conversation):
    root, sid, binding, _ = _asked(conversation)
    for bad in ["", 7, "x" * 300, "bad id\n"]:
        with pytest.raises((native_ipc.IPCError, journal.JournalError)):
            journal.claim(root, _answer(root, sid, binding, reply_to=bad))
    with pytest.raises(native_ipc.IPCError):
        native_ipc.validate_request(
            journal_tests.request(binding, action="interrupt", reply_to="q1")
        )


# The first message DISPATCHED after the question is its answer: one queued before the question
# but sent after it closes it, one never sent does not (independent review of #1464).


def _queued_behind_running(conversation):
    root, sid, binding = conversation
    binding = replace(binding, adapter="codex-app-server")
    running = journal_tests.request(binding, context={})
    journal.claim(root, running)
    journal.record_handoff(root, binding, running["params"]["operation_id"], "sent")
    waiting = journal_tests.request(binding, revision=journal.read(root, sid).revision, context={})
    journal.claim(root, waiting, queued=True)
    op = running["params"]["operation_id"]
    journal.append_events(root, binding, [_text(op, "Pick", questions=ASKED, item_id="q1")])
    return root, sid, binding, waiting["params"]["operation_id"]


def test_a_message_queued_before_the_question_but_sent_after_it_closes_it(conversation):
    root, sid, binding, waiting = _queued_behind_running(conversation)
    journal.record_handoff(root, binding, waiting, "uncertain")
    journal.record_handoff(root, binding, waiting, "sent")
    _refused(root, _answer(root, sid, binding))


@pytest.mark.parametrize("queued", [True, False])
def test_a_message_that_was_never_sent_does_not_close_it(conversation, queued):
    root, sid, binding, _ = _asked(conversation)
    refused = journal_tests.request(
        binding, revision=journal.read(root, sid).revision, text="lost", context={}
    )
    journal.claim(root, refused, queued=queued)
    # Refused before any write, or removed from the queue (#1447): it never reached Codex.
    journal.record_handoff(root, binding, refused["params"]["operation_id"], "not_sent")
    assert journal.claim(root, _answer(root, sid, binding))[0]


def test_a_queued_answer_is_current_until_a_newer_question_is_asked(conversation):
    root, sid, binding, op = _asked(conversation)
    answer = _answer(root, sid, binding)
    assert journal.claim(root, answer, queued=True)[0]
    answer_id = answer["params"]["operation_id"]
    # A message queued BEHIND the answer does not take its place.
    behind = journal_tests.request(
        binding, revision=journal.read(root, sid).revision, text="later", context={}
    )
    journal.claim(root, behind, queued=True)
    assert journal.answer_current(journal.read(root, sid), answer_id)
    later = [{"title": "And the channel?", "options": ["main"]}]
    journal.append_events(root, binding, [_text(op, "Again", questions=later, item_id="q2")])
    assert not journal.answer_current(journal.read(root, sid), answer_id)


def test_an_answer_is_not_current_once_another_message_reached_the_agent(conversation):
    root, sid, binding, _ = _asked(conversation)
    answer = _answer(root, sid, binding)
    assert journal.claim(root, answer, queued=True)[0]
    # Defensive: only the queue head can be sent now, so no live path does this today.
    other = journal_tests.request(
        binding, revision=journal.read(root, sid).revision, text="mine", context={}
    )
    journal.claim(root, other, queued=True)
    journal.record_handoff(root, binding, other["params"]["operation_id"], "uncertain")
    assert not journal.answer_current(journal.read(root, sid), answer["params"]["operation_id"])
    # An ordinary message has no question to be current for.
    assert journal.answer_current(journal.read(root, sid), other["params"]["operation_id"])
