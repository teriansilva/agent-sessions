"""#1463 Phase 2: Claude's `AskUserQuestion` is answered with the operator's choices.

Claude Code sends the question as a `can_use_tool` callback in every permission mode (captured:
`fixtures/claude_ask_user_question.json`). An Approve sends no answers ("The user did not answer the
questions"), so a question is never approved: it is answered, skipped or stopped, and the answer is
checked by the server and again by the worker against the request it holds.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import test_native_clients as client_tests
import test_native_journal as journal_tests
from agent_sessions import native_ipc, native_questions, native_runtime
from agent_sessions import native_journal as journal
from agent_sessions import structured_runtime as runtime
from agent_sessions.native_protocol import ProtocolError
from test_api_always import _answers, _ask, _suggestion
from test_native_protocol import OPERATION, claude_running
from test_native_reply_paragraphs import start
from test_native_response_timestamps import snapshot
from test_native_runtime import (  # noqa: F401 — fixtures
    ENGINES,
    frames,
    host,
    ident,
    pending,
    project,
    settle,
)

conversation = journal_tests.conversation
app_client = client_tests.app_client

CAPTURED = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_ask_user_question.json").read_text()
)
QUESTION = CAPTURED["request"]["input"]["questions"][0]["question"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def frame(**input_overrides):
    out = copy.deepcopy(CAPTURED)
    out["request"]["input"].update(input_overrides)
    return out


def held(frame_=None):
    codec = claude_running()
    [event] = codec.feed(frame_ or CAPTURED)
    return codec, event


def _q(text, labels, multi=False):
    return {
        "question": text,
        "header": "H",
        "options": [{"label": label, "description": ""} for label in labels],
        "multiSelect": multi,
    }


# --- the codec ---------------------------------------------------------------------------------


def test_the_captured_question_is_answered_skipped_or_stopped_never_approved():
    codec, event = held()
    assert event.kind == "approval" and event.data["tool"] == "AskUserQuestion"
    assert event.data["choices"] == ["answer", "reject", "cancel"]
    # The journal boundary accepts exactly that.
    stamped = {**event.data, "worker_id": ident(), "connection_id": ident()}
    native_ipc.normalize_event({"kind": "approval", "data": stamped})
    key = event.data["request_id"]
    for decision in ("approve", "always"):
        with pytest.raises(ProtocolError):
            codec.decide(key, decision, grant="g" + "0" * 16 if decision == "always" else None)
    assert key in codec._approvals  # refused before anything is consumed


def test_an_answer_sends_the_original_input_plus_answers_keyed_by_the_question():
    codec, event = held()
    reply = codec.decide(event.data["request_id"], "answer", answers=[{"selected": ["Blue"]}])
    assert reply["response"]["request_id"] == CAPTURED["request_id"]
    assert reply["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {**CAPTURED["request"]["input"], "answers": {QUESTION: "Blue"}},
    }


def test_own_words_are_the_answer_as_typed():
    codec, event = held()
    reply = codec.decide(event.data["request_id"], "answer", answers=[{"other": "Green, really"}])
    assert reply["response"]["response"]["updatedInput"]["answers"] == {QUESTION: "Green, really"}


def test_multi_select_joins_in_offered_order_and_a_comma_in_a_label_passes_verbatim():
    questions = [_q("Which surfaces?", ["Web, app", "Docs", "Landing"], multi=True)]
    codec, event = held(frame(questions=questions))
    reply = codec.decide(
        event.data["request_id"], "answer", answers=[{"selected": ["Landing", "Web, app"]}]
    )
    # Claude Code's own picker joins with ", " too: a comma in a label is its ambiguity as well.
    assert reply["response"]["response"]["updatedInput"]["answers"] == {
        "Which surfaces?": "Web, app, Landing"
    }


@pytest.mark.parametrize(
    "answers",
    [
        [{"selected": ["Green"]}],  # not offered
        [{"selected": ["Red", "Blue"]}],  # one question, not multi-select
        [{"selected": []}],
        [{"selected": ["Blue"]}, {"selected": ["Red"]}],  # more answers than questions
        [{"selected": ["Blue"], "other": "x"}],
        [{"other": ""}],
        [{"other": "bad\x07bell"}],
        [{"other": "x" * (native_questions.OTHER_MAX + 1)}],
        [],
        "Blue",
    ],
)
def test_an_answer_the_request_did_not_offer_is_refused_and_nothing_is_consumed(answers):
    codec, event = held()
    with pytest.raises(ProtocolError):
        codec.decide(event.data["request_id"], "answer", answers=answers)
    assert event.data["request_id"] in codec._approvals


@pytest.mark.parametrize(
    "questions",
    [
        [_q("Same?", ["a", "b"]), _q("Same?", ["c", "d"])],  # answers are keyed by question text
        [_q("Pick", ["a", "a"])],
        [_q("", ["a"])],
        [_q("Pick\x1b[31m", ["a"])],
        [_q("Pick", [""])],
        [_q("Pick", ["a"])] * 5,
        "not a list",
    ],
)
def test_a_question_battlelab_cant_answer_exactly_can_only_be_skipped(questions):
    codec, event = held(frame(questions=questions))
    assert event.data["choices"] == ["reject", "cancel"]
    with pytest.raises(ProtocolError):
        codec.decide(event.data["request_id"], "answer", answers=[{"selected": ["a"]}])
    reply = codec.decide(event.data["request_id"], "reject")
    assert reply["response"]["response"]["behavior"] == "deny"


def test_skip_declines_and_stop_also_interrupts():
    codec, event = held()
    skip = codec.decide(event.data["request_id"], "reject")["response"]["response"]
    assert skip["behavior"] == "deny" and skip["interrupt"] is False
    codec, event = held()
    stop = codec.decide(event.data["request_id"], "cancel")["response"]["response"]
    assert stop["behavior"] == "deny" and stop["interrupt"] is True


@pytest.mark.parametrize(
    "tool_input",
    [
        {"command": "git status"},
        # Question-SHAPED input on another tool: only the tool's name makes it a question, so an
        # answer here would be a Bash approval (independent review of #1487).
        {"command": "rm -rf x", "questions": CAPTURED["request"]["input"]["questions"]},
    ],
)
def test_any_other_tool_takes_no_answer(tool_input):
    codec = claude_running()
    [event] = codec.feed(
        {
            "type": "control_request",
            "request_id": "native-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_use_id": "toolu_1",
                "input": tool_input,
            },
        }
    )
    assert event.data["choices"] == ["approve", "reject", "cancel"]
    key = event.data["request_id"]
    with pytest.raises(ProtocolError, match="only an agent question"):
        codec.check_answer(key, [{"selected": ["Blue"]}])
    with pytest.raises(ProtocolError):
        codec.decide(key, "answer", answers=[{"selected": ["Blue"]}])
    assert key in codec._approvals
    assert codec.operation_id == OPERATION


# --- the private IPC boundary --------------------------------------------------------------------


def _decide_params(**extra):
    return {
        "operation_id": ident(),
        "expected_revision": 3,
        "turn_id": ident(),
        "request_id": "s:perm-2",
        "item_id": "toolu_2",
        "payload_digest": "0" * 64,
        "approval_worker_id": ident(),
        "approval_connection_id": ident(),
        "actor": "alice",
        **extra,
    }


def test_ipc_carries_answers_only_with_an_answer_decision():
    ok = native_ipc._params(
        "decide", _decide_params(decision="answer", answers=[{"selected": ["Blue"]}])
    )
    assert ok["answers"] == [{"selected": ["Blue"]}]
    for bad in (
        _decide_params(decision="answer"),
        _decide_params(decision="approve", answers=[{"selected": ["Blue"]}]),
        _decide_params(decision="answer", answers=[{"pick": "Blue"}]),
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc._params("decide", bad)


def test_ipc_refuses_a_question_that_is_also_approvable():
    data = {
        "operation_id": ident(),
        "native_turn_id": ident(),
        "request_id": "s:x",
        "item_id": "toolu_1",
        "tool": "AskUserQuestion",
        "summary": "{}",
        "choices": ["approve", "answer"],
        "payload_digest": "0" * 64,
        "worker_id": ident(),
        "connection_id": ident(),
    }
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "approval", "data": data})


# --- through the runtime, in both permission modes ---------------------------------------------


async def _session(project, bypass):  # noqa: F811
    snap = await runtime.create_session(
        ENGINES["claude"],
        str(project),
        operation_id=ident(),
        **({"bypass": True} if bypass else {}),
    )
    if bypass:
        await runtime.start_session(snap["session_key"])
    return snap["session_key"]


def _asked(**input_overrides):
    request = frame(**input_overrides)["request"]
    return _ask(**{k: v for k, v in request.items() if k not in {"subtype", "tool_use_id"}})


@pytest.mark.anyio
@pytest.mark.parametrize("bypass", [False, True])
async def test_the_question_reaches_the_operator_and_the_answer_reaches_claude(
    host,  # noqa: F811
    project,  # noqa: F811
    bypass,
):
    key = await _session(project, bypass)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=_asked())
    snap = await pending(key)
    assert snap["bypass"] is bypass
    (request,) = snap["pending_requests"]
    assert request["kind"] == "AskUserQuestion"
    assert request["choices"] == ["answer", "reject", "cancel"]
    assert request["always"] == [] and request["risk"] is None
    assert request["payload"]["input"]["questions"][0]["question"] == QUESTION
    args = dict(user="alice", turn_id=turn, request_id=request["request_id"], decision="answer")
    did = ident()
    await runtime.decide(key, decision_id=did, answers=[{"selected": ["Blue"]}], **args)
    # An exact replay observes; the same id with other answers conflicts.
    again = await runtime.decide(key, decision_id=did, answers=[{"selected": ["Blue"]}], **args)
    assert again["handoff"] == "sent"
    with pytest.raises(runtime.StructuredError) as changed:
        await runtime.decide(key, decision_id=did, answers=[{"selected": ["Red"]}], **args)
    assert changed.value.status == 409
    await settle(key, turn)
    (answer,) = _answers(project, "claude")
    assert answer["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {**CAPTURED["request"]["input"], "answers": {QUESTION: "Blue"}},
    }


@pytest.mark.anyio
async def test_the_server_refuses_without_calling_the_worker(host, project, monkeypatch):  # noqa: F811
    key = await _session(project, False)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=_asked())
    request = (await pending(key))["pending_requests"][0]
    calls = []
    real = native_runtime._existing_generation_call

    async def spy(session_id, action, params):
        calls.append(action)
        return await real(session_id, action, params)

    monkeypatch.setattr(native_runtime, "_existing_generation_call", spy)
    base = dict(user="alice", turn_id=turn, request_id=request["request_id"])
    cases = [
        (dict(decision="approve"), 409),  # a question is never approved
        (dict(decision="always", grant="g" + "0" * 16), 409),
        (dict(decision="answer", answers=[{"selected": ["Green"]}]), 409),
        (dict(decision="answer", answers=[{"selected": ["Red", "Blue"]}]), 409),
        (dict(decision="answer"), 422),
        (dict(decision="reject", answers=[{"selected": ["Blue"]}]), 422),
        (dict(decision="answer", answers=[{"selected": ["Blue"], "other": "x"}]), 422),
    ]
    for kwargs, status in cases:
        with pytest.raises(runtime.StructuredError) as exc:
            await runtime.decide(key, decision_id=ident(), **base, **kwargs)
        assert exc.value.status == status, kwargs
    assert "decide" not in calls
    assert _answers(project, "claude") == []


@pytest.mark.anyio
async def test_the_worker_refuses_an_answer_the_request_it_holds_does_not_take(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Even if the server were tricked into forwarding it, the worker checks the held request."""
    key = await _session(project, False)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=_asked())
    request = (await pending(key))["pending_requests"][0]
    # The server's own check is switched off here; the worker process keeps the real one.
    with monkeypatch.context() as patched:
        patched.setattr(native_questions, "claude_answers", lambda *_: {})
        with pytest.raises(runtime.StructuredError):
            await runtime.decide(
                key,
                decision_id=ident(),
                user="alice",
                turn_id=turn,
                request_id=request["request_id"],
                decision="answer",
                answers=[{"selected": ["Green"]}],
            )
    assert _answers(project, "claude") == []
    await _still_answerable(key, turn, request, project)


async def _still_answerable(key, turn, request, project):  # noqa: F811
    """Refused BEFORE any claim: the question is still pending and a real answer still lands."""
    snap = await runtime.snapshot(key)
    assert [r["request_id"] for r in snap["pending_requests"]] == [request["request_id"]]
    await runtime.decide(
        key,
        decision_id=ident(),
        user="alice",
        turn_id=turn,
        request_id=request["request_id"],
        decision="answer",
        answers=[{"selected": ["Blue"]}],
    )
    await settle(key, turn)
    (answer,) = _answers(project, "claude")
    assert answer["response"]["response"]["updatedInput"]["answers"] == {QUESTION: "Blue"}


@pytest.mark.anyio
async def test_the_worker_refuses_to_approve_a_question_even_if_the_server_offered_it(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """An approve sends Claude no answers, so the worker refuses it for a question on its own."""
    key = await _session(project, False)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=_asked())
    request = (await pending(key))["pending_requests"][0]
    real = native_runtime.project

    def offering_approve(*args, **kwargs):
        view = real(*args, **kwargs)
        for r in view["pending_requests"]:
            # Disguised as another tool too, so the server's own refusal is bypassed and only
            # the worker's check (against the request it holds) stands.
            r["choices"], r["kind"] = ["approve", *r["choices"]], "Bash"
        return view

    # The server's checks are switched off; the worker process keeps the real one.
    with monkeypatch.context() as patched:
        patched.setattr(native_runtime, "project", offering_approve)
        with pytest.raises(runtime.StructuredError):
            await runtime.decide(
                key,
                decision_id=ident(),
                user="alice",
                turn_id=turn,
                request_id=request["request_id"],
                decision="approve",
            )
    assert _answers(project, "claude") == []
    await _still_answerable(key, turn, request, project)


@pytest.mark.anyio
async def test_a_question_offers_no_standing_grant_even_when_claude_suggests_one(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key = await _session(project, False)
    turn = ident()
    request = frame()["request"]
    request["permission_suggestions"] = [_suggestion("", tool="AskUserQuestion")]
    text = _ask(**{k: v for k, v in request.items() if k not in {"subtype", "tool_use_id"}})
    await runtime.submit_turn(key, operation_id=turn, text=text)
    (pending_request,) = (await pending(key))["pending_requests"]
    assert pending_request["always"] == [] and pending_request["risk"] is None


@pytest.mark.anyio
async def test_a_question_with_repeated_texts_can_still_be_skipped(host, project):  # noqa: F811
    key = await _session(project, False)
    turn = ident()
    await runtime.submit_turn(
        key, operation_id=turn, text=_asked(questions=[_q("Same?", ["a"]), _q("Same?", ["b"])])
    )
    request = (await pending(key))["pending_requests"][0]
    assert request["choices"] == ["reject", "cancel"]
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.decide(
            key,
            decision_id=ident(),
            user="alice",
            turn_id=turn,
            request_id=request["request_id"],
            decision="answer",
            answers=[{"selected": ["a"]}, {"selected": ["b"]}],
        )
    assert exc.value.status == 409
    await runtime.decide(
        key,
        decision_id=ident(),
        user="alice",
        turn_id=turn,
        request_id=request["request_id"],
        decision="reject",
    )
    await settle(key, turn)
    (answer,) = _answers(project, "claude")
    assert answer["response"]["response"]["behavior"] == "deny"


@pytest.mark.anyio
async def test_a_chat_client_takes_no_answers():
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.decide(
            f"apichat:{ident()}",
            decision_id=ident(),
            user="alice",
            turn_id=ident(),
            request_id="r",
            decision="answer",
            answers=[{"selected": ["x"]}],
        )
    assert exc.value.status in {404, 422}


# --- a worker from before #1463 (Hermes #6473) ---------------------------------------------------
#
# Workers stay pinned to their release while the server updates. An older Claude worker journals a
# question as an ordinary approval offering approve, and its approve sends the input WITHOUT
# answers. The server must fail closed on its own: no approve offered, and none accepted.


def test_an_old_workers_question_is_never_offered_approve(conversation):
    root, sid, binding, op = start(conversation, "claude-stream-json")
    request = CAPTURED["request"]
    journal.append_events(
        root,
        binding,
        [
            {
                "kind": "approval",
                "data": {
                    "operation_id": op,
                    "native_turn_id": "native-turn-1",
                    "request_id": "s:perm-old",
                    "item_id": "toolu_old",
                    "tool": "AskUserQuestion",
                    "summary": json.dumps({"tool_name": "AskUserQuestion", **request}),
                    "choices": ["approve", "reject", "cancel"],
                    "payload_digest": "a" * 64,
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "complete": True,
                },
            }
        ],
    )
    (pending_request,) = snapshot(root, sid, binding)["pending_requests"]
    assert pending_request["kind"] == "AskUserQuestion"
    assert pending_request["choices"] == ["reject", "cancel"]
    assert pending_request["always"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("decision", ["approve", "always"])
async def test_the_server_refuses_to_approve_a_question_even_when_offered(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    decision,
):
    key = await _session(project, False)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=_asked())
    request = (await pending(key))["pending_requests"][0]
    real = native_runtime.project

    def as_an_old_worker_offered_it(*args, **kwargs):
        view = real(*args, **kwargs)
        for r in view["pending_requests"]:
            r["choices"] = ["approve", *r["choices"]]
            r["always"] = [{"id": "g" + "0" * 16}]
        return view

    calls = []
    spied = native_runtime._existing_generation_call

    async def spy(session_id, action, params):
        calls.append(action)
        return await spied(session_id, action, params)

    with monkeypatch.context() as patched:
        patched.setattr(native_runtime, "project", as_an_old_worker_offered_it)
        patched.setattr(native_runtime, "_existing_generation_call", spy)
        with pytest.raises(runtime.StructuredError, match="not approved") as refused:
            await runtime.decide(
                key,
                decision_id=ident(),
                user="alice",
                turn_id=turn,
                request_id=request["request_id"],
                decision=decision,
                **({"grant": "g" + "0" * 16} if decision == "always" else {}),
            )
    assert refused.value.status == 409
    assert "decide" not in calls
    assert _answers(project, "claude") == []


# --- what the operator sees is what they answer (Hermes #6477) ---------------------------------


@pytest.mark.parametrize(
    "field,cap",
    [("description", native_questions.DESCRIPTION_MAX), ("header", native_questions.HEADER_MAX)],
)
@pytest.mark.parametrize("unit", ["x", "\U0001f600"])  # an emoji is TWO UTF-16 units on the card
def test_text_the_card_would_cut_is_not_offered_as_choices(field, cap, unit):
    def asked(length):
        q = _q("Ship it?", ["Yes", "No"])
        tail = " only if signed"
        width = 2 if unit != "x" else 1
        fill = (length - len(tail)) // width
        text = unit * fill + "x" * ((length - len(tail)) - fill * width) + tail
        if field == "header":
            q["header"] = text
        else:
            q["options"][0]["description"] = text
        return held(frame(questions=[q]))[1]

    assert asked(cap).data["choices"] == ["answer", "reject", "cancel"]
    # One character past the cut: the condition at its end would be hidden, so skip/stop only.
    assert asked(cap + 1).data["choices"] == ["reject", "cancel"]


def test_the_route_pairs_answers_with_an_answer_decision_only(app_client, monkeypatch):
    calls = []

    async def decide(*args, **kwargs):
        calls.append(kwargs)
        return {"decision": kwargs["decision"]}

    monkeypatch.setattr(runtime, "decide", decide)
    url = f"/api/structured/sessions/claude-api:{ident()}/decisions"
    base = {"decision_id": ident(), "turn_id": ident(), "request_id": "s:perm-2"}
    for body in (
        {**base, "decision": "answer"},  # an answer without answers
        {**base, "decision": "reject", "answers": [{"selected": ["Blue"]}]},
        {**base, "decision": "approve", "answers": [{"selected": ["Blue"]}]},
        {**base, "decision": "answer", "answers": {"selected": ["Blue"]}},  # not a list
    ):
        assert app_client.post(url, json=body).status_code == 422, body
    assert calls == []
    answers = [{"selected": ["Blue"]}]
    response = app_client.post(url, json={**base, "decision": "answer", "answers": answers})
    assert response.status_code == 200
    assert calls[0]["answers"] == answers and calls[0]["decision"] == "answer"
