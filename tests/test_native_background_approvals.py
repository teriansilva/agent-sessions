"""A background task's permission request reaches the operator (#1725), and only as that task's.

The refusal matrix of the issue, at the three boundaries that each check for themselves: the codec
(what maps, what it binds), the worker (what it will write) and the facade (what it presents and
accepts), then the real worker against a scripted native. This is an approval path: every case
that could widen what an operator authorises is here.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import json
import uuid
from pathlib import Path

import pytest

import test_native_journal as journal_tests
import test_native_safety_table
from agent_sessions import (
    native_grants,
    native_ipc,
    native_journal,
    native_protocol,
    native_runtime,
    native_segments,
    share_api,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.native_processes import Floor
from agent_sessions.native_protocol import (
    MAX_CONSUMED,
    MAX_PENDING,
    ClaudeCodec,
    CodexCodec,
    ProtocolError,
)
from test_native_liveness import _task, _worker
from test_native_rollover_fences import roll
from test_native_runtime import ENGINES, frames, host, ident, pending, project, settle  # noqa: F401
from test_native_safety_table import (
    OPERATION,
    SESSION,
    TURN,
    acp_request,
    claude,
    claude_request,
    codex,
    codex_request,
    copilot,
    opencode,
)

conversation = journal_tests.conversation
AGENT = "agent-bg"
GRANT = "g0123456789abcdef"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def background(*, running=False):
    codec = claude() if running else ClaudeCodec(SESSION)
    codec.generation = codec.generation or str(uuid.uuid4())
    codec.feed(
        _task("task_started", AGENT, description="test-runner sub-agent", is_backgrounded=True)
    )
    return codec


def ask(codec, request_id="native-bg", **extra):
    fields = {"agent_id": AGENT, **extra}
    return codec.feed({**claude_request(**fields), "request_id": request_id})


# --- the codec: what maps, and what it binds --------------------------------------------------


def test_an_unknown_agent_id_is_refused():
    codec = background()
    (refusal,) = codec.feed(claude_request(agent_id="agent-never-reported"))
    assert refusal.kind == "send" and refusal.data["frame"]["response"]["subtype"] == "error"
    assert not codec._approvals


def test_an_unknown_agent_id_is_refused_during_an_operator_turn_too():
    """Never attributed to the turn that happens to be running (Hermes on #1278)."""
    codec = claude()
    (refusal,) = codec.feed(claude_request(agent_id="agent-never-reported"))
    assert refusal.kind == "send" and not codec._approvals


@pytest.mark.parametrize("running", [False, True], ids=["no-turn", "operator-turn-running"])
def test_a_mapped_request_is_pending_and_belongs_to_no_operator_turn(running):
    codec = background(running=running)
    (event,) = ask(codec)
    assert event.kind == "approval"
    assert event.data["operation_id"] is None and event.data["native_turn_id"] is None
    origin = event.data["background_task"]
    assert origin["task_id"] == AGENT and origin["agent_id"] == AGENT
    assert origin["generation"] == codec.generation
    assert origin["description"] == "test-runner sub-agent"
    # One-shot only: never `cancel` (it would interrupt the operator's turn), never `always`.
    assert event.data["choices"] == ["approve", "reject"]
    # What the operator is shown names the task.
    assert json.loads(event.data["summary"])["background_task"] == {
        "task_id": AGENT,
        "description": "test-runner sub-agent",
    }


def test_a_mapped_request_is_valid_at_the_ipc_boundary_and_a_half_one_is_not():
    codec = background()
    (event,) = ask(codec)
    data = {
        **event.data,
        "worker_id": str(uuid.uuid4()),
        "connection_id": str(uuid.uuid4()),
    }
    data["background_task"]["generation"] = str(uuid.uuid4())
    checked = native_ipc.normalize_event({"kind": "approval", "data": data})
    assert checked["data"]["background_task"]["task_id"] == AGENT
    # Never both: a request cannot be a task's AND an operator turn's.
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event(
            {
                "kind": "approval",
                "data": {**data, "operation_id": OPERATION, "native_turn_id": TURN},
            }
        )
    # An operator turn's still needs its turn.
    plain = {k: v for k, v in data.items() if k != "background_task"}
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "approval", "data": plain})
    # Its origin is a closed shape.
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event(
            {
                "kind": "approval",
                "data": {**data, "background_task": {**data["background_task"], "grant": "*"}},
            }
        )


def test_the_operator_turn_ending_leaves_it_pending():
    codec = background(running=True)
    ask(codec)
    (done,) = codec.feed(
        {
            "type": "result",
            "subtype": "success",
            "uuid": str(uuid.uuid4()),
            "is_error": False,
            "result": "ok",
        }
    )
    assert done.kind == "turn_completed" and codec.operation_id is None
    assert list(codec._approvals) == ["s:native-bg"]
    frame = codec.decide("s:native-bg", "approve")
    assert frame["response"]["response"]["behavior"] == "allow"


def test_it_holds_no_operator_message_back_but_keeps_the_worker_alive(conversation):
    codec = background()
    ask(codec)
    worker = _worker(conversation, codec)
    worker.floor = Floor("ok", ())
    codec.lifecycle_proven = True
    assert not worker.dispatch_busy()  # an operator message can go
    assert worker.native_busy() and worker.busy()  # and nothing idles out under the request


def test_an_operator_turns_own_request_still_holds_the_queue(conversation):
    codec = claude()
    codec.feed(claude_request())
    worker = _worker(conversation, codec)
    assert codec.turn_approvals() and worker.dispatch_busy()


def test_the_digest_binds_the_origin():
    first = background()
    second = background()
    (one,) = ask(first)
    (other,) = ask(second)
    # The same frame from another generation, or as another origin, is another request.
    assert one.data["payload_digest"] != other.data["payload_digest"]
    plain = claude()
    (own,) = plain.feed(claude_request())
    assert own.data["payload_digest"] != one.data["payload_digest"]


def test_a_repeat_of_the_same_request_keeps_its_origin():
    codec = background()
    (first,) = ask(codec)
    (again,) = ask(codec)
    assert again.data["background_task"] == first.data["background_task"]
    assert again.data["payload_digest"] == first.data["payload_digest"]


def test_the_task_exiting_refuses_and_retracts_it():
    codec = background()
    ask(codec)
    events = codec.feed(_task("task_notification", AGENT, status="completed"))
    assert [e.kind for e in events] == ["background", "send", "approval_cancelled"]
    assert events[1].data["frame"]["response"]["response"]["behavior"] == "deny"
    assert events[2].data["request_id"] == "s:native-bg"
    assert not codec._approvals
    with pytest.raises(ProtocolError):
        codec.decide("s:native-bg", "approve")


def test_the_whole_list_no_longer_naming_it_refuses_and_retracts_it():
    codec = background()
    ask(codec)
    events = codec.feed({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    assert "approval_cancelled" in [e.kind for e in events]
    assert not codec._approvals


def _restart(codec):
    return codec.feed(_task("task_started", AGENT, description="another", is_backgrounded=True))


def test_a_reused_task_id_retracts_what_the_old_task_asked():
    codec = background()
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    assert "approval_cancelled" in [e.kind for e in _restart(codec)]
    assert not codec.origin_live(origin)  # its epoch is gone


@pytest.mark.parametrize("ended", ["notification", "list"])
def test_a_request_from_a_reused_task_id_is_refused_at_once_never_held(ended):
    """The journal's fold keeps an ended id ended, so such a request could never be shown: held, it
    would wait for ever, with the session busy and a full ledger held. It is refused now."""
    codec = background()
    codec.feed(
        _task("task_notification", AGENT, status="completed")
        if ended == "notification"
        else {"type": "system", "subtype": "background_tasks_changed", "tasks": []}
    )
    _restart(codec)
    assert AGENT in codec._background  # the id runs again, as the client says
    events = ask(codec, request_id="native-2")
    assert not codec._approvals and "approval" not in [e.kind for e in events]
    (refusal,) = [e for e in events if e.kind == "send"]
    assert refusal.data["frame"]["response"]["subtype"] == "error"
    # With no turn to carry a row, it is counted for the pane instead of being silence.
    assert codec.requests_refused == 1


def test_a_refused_reused_id_is_a_row_on_a_running_turn():
    codec = background(running=True)
    codec.feed(_task("task_notification", AGENT, status="completed"))
    _restart(codec)
    row, refusal = ask(codec, request_id="native-2")
    assert row.kind == "tool" and row.data["state"] == "skipped"
    assert refusal.kind == "send" and codec.requests_refused == 0


def test_a_reused_id_cannot_wedge_the_session(conversation):
    """It idles out once the task does: no request is left open behind a card nobody can see."""
    codec = background()
    worker = _worker(conversation, codec)
    worker.floor = Floor("ok", ())
    codec.feed(_task("task_notification", AGENT, status="completed"))
    _restart(codec)
    ask(codec, request_id="native-2")
    assert not codec._approvals
    codec.feed(_task("task_notification", AGENT, status="completed"))
    assert not worker.busy()


# --- a foreground sub-agent: its request keeps today's refusal ---------------------------------


def _foreground(codec, **fields):
    return codec.feed(
        _task(
            "task_started",
            AGENT,
            description="a Task call the turn is waiting on",
            is_backgrounded=False,
            task_type="local_agent",
            **fields,
        )
    )


def test_a_foreground_sub_agents_request_is_refused_as_today_with_no_origin():
    """It is not "a background task, not your current turn": it IS the operator's turn's, blocked on
    a tool call. Decision on PR #1822: strictly safer to keep today's refusal than to present it."""
    codec = claude()
    _foreground(codec)
    (refusal,) = ask(codec)
    assert refusal.kind == "send" and refusal.data["frame"]["response"]["subtype"] == "error"
    assert not codec._approvals and codec.operation_id == OPERATION


def test_a_task_of_unknown_kind_is_no_origin():
    """Only what is positively known as backgrounded: a task that doesn't say stays refused."""
    codec = ClaudeCodec(SESSION)
    codec.generation = str(uuid.uuid4())
    codec.feed(_task("task_started", AGENT, description="says nothing"))
    (refusal,) = ask(codec)
    assert refusal.kind == "send" and not codec._approvals


def test_a_real_background_request_is_still_presented():
    codec = background()
    (event,) = ask(codec)
    assert event.kind == "approval" and event.data["background_task"]["task_id"] == AGENT


def test_a_list_change_does_not_touch_a_foreground_task():
    """The list names background tasks only: leaving it is no end for one that was never on it."""
    codec = claude()
    _foreground(codec)
    events = codec.feed(
        {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": [{"task_id": "other", "task_type": "local_bash", "description": "x"}],
        }
    )
    assert AGENT in codec._background
    assert not [e for e in events if e.data.get("task_id") == AGENT]
    # Its own terminal frame still ends it.
    codec.feed(_task("task_notification", AGENT, status="completed"))
    assert AGENT not in codec._background


def test_a_task_of_unknown_kind_still_ends_when_the_list_stops_naming_it():
    """#1724's rule, for what isn't known to be foreground: a ghost can't pin a worker."""
    codec = ClaudeCodec(SESSION)
    codec.feed(_task("task_started", "ghost"))
    codec.feed({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    assert "ghost" not in codec._background


@pytest.mark.parametrize("how", ["patch", "list"])
def test_a_foreground_task_moved_to_the_background_becomes_an_origin(how):
    codec = claude()
    _foreground(codec)
    codec.feed(
        _task("task_updated", AGENT, patch={"is_backgrounded": True})
        if how == "patch"
        else {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": [{"task_id": AGENT, "task_type": "local_agent", "description": "moved"}],
        }
    )
    (event,) = ask(codec)
    assert event.kind == "approval" and "background_task" in event.data


def test_a_stale_origin_is_refused_at_the_decision():
    codec = background()
    ask(codec)
    # Whatever got past the retraction: the task is not this one any more.
    codec._task_facts[AGENT]["epoch"] = 999
    with pytest.raises(ProtocolError, match="no longer running"):
        codec.decide("s:native-bg", "approve")
    assert "s:native-bg" in codec._approvals  # refused before anything is consumed


def test_another_generation_is_refused_at_the_decision():
    codec = background()
    ask(codec)
    codec.generation = str(uuid.uuid4())
    with pytest.raises(ProtocolError, match="no longer running"):
        codec.decide("s:native-bg", "approve")


@pytest.mark.parametrize("decision", ["cancel"])
def test_only_once_or_decline_at_the_codec(decision):
    codec = background()
    ask(codec)
    with pytest.raises(ProtocolError, match="approved once or declined"):
        codec.decide("s:native-bg", decision)
    assert "s:native-bg" in codec._approvals


SUGGESTION = {
    "type": "addRules",
    "rules": [{"toolName": "Bash", "ruleContent": "ls:*"}],
    "behavior": "allow",
    "destination": "session",
}


def test_always_is_refused_at_the_codec_whatever_the_request_proposed():
    """Even the very grant the request proposed: the same frame from the operator's own turn is
    offered it, so it is the origin that refuses it here."""
    plain = claude()
    plain.feed(claude_request(permission_suggestions=[SUGGESTION]))
    item = plain._approvals["s:native-1"]
    offered = native_grants.derive(plain.ADAPTER, item["kind"], item["presented"], "/tmp")["always"]
    assert offered, "the request must really propose a grant, or this proves nothing"
    grant = offered[0]["id"]
    assert plain.decide("s:native-1", "always", grant=grant, cwd="/tmp")

    codec = background()
    ask(codec, permission_suggestions=[SUGGESTION])
    with pytest.raises(ProtocolError, match="not offered"):
        codec.decide("s:native-bg", "always", grant=grant, cwd="/tmp")
    assert "s:native-bg" in codec._approvals


def test_a_question_from_a_background_task_can_only_be_skipped():
    codec = background()
    (event,) = codec.feed(
        claude_request(
            agent_id=AGENT,
            tool_name="AskUserQuestion",
            input={"questions": [{"question": "Which?", "options": [{"label": "a"}]}]},
        )
    )
    assert event.data["choices"] == ["reject"]


def test_an_answered_background_request_stays_answered_across_operator_turns():
    codec = background()
    ask(codec)
    codec.decide("s:native-bg", "reject")
    codec.submit("next", str(uuid.uuid4()))  # a new turn resets the turn's own consumed set
    with pytest.raises(ProtocolError, match="already consumed"):
        ask(codec)
    assert MAX_CONSUMED > MAX_PENDING


# --- Codex: a command of a turn the agent started itself -------------------------------------


AGENT_TURN = "0199f3ac-53fb-7c10-81ef-ce7c2e732b29"


def codex_agent_turn():
    codec = CodexCodec()
    codec.generation = str(uuid.uuid4())
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    codec.feed(
        {
            "method": "turn/started",
            "params": {"threadId": SESSION, "turn": {"id": AGENT_TURN, "status": "inProgress"}},
        }
    )
    assert codec.background_turn is not None
    return codec


def codex_ask(request_id=7, turn=AGENT_TURN, method="item/commandExecution/requestApproval"):
    return {
        "id": request_id,
        "method": method,
        "params": {"threadId": SESSION, "turnId": turn, "itemId": "cmd-9", "command": "ls"},
    }


def test_codex_a_command_of_the_agents_own_turn_is_pending_as_that_turns():
    codec = codex_agent_turn()
    (event,) = codec.feed(codex_ask())
    assert event.kind == "approval" and event.data["operation_id"] is None
    origin = event.data["background_task"]
    assert origin["kind"] == "agent_turn" and origin["agent_id"] == AGENT_TURN
    assert origin["task_id"] == codec.background_turn
    assert event.data["choices"] == ["approve", "reject"]
    assert codec.decide("i:7", "approve")["result"]["decision"] == "accept"


def test_codex_a_request_naming_any_other_turn_is_refused():
    codec = codex_agent_turn()
    (refusal,) = codec.feed(codex_ask(turn="another-turn"))
    assert refusal.kind == "send" and refusal.data["frame"]["error"]["code"] == -32601
    assert not codec._approvals


def test_codex_a_request_with_no_turn_running_is_refused():
    codec = CodexCodec()
    frame = codec.create("/tmp")
    codec.feed({"id": frame["id"], "result": {"thread": {"id": SESSION}}})
    (refusal,) = codec.feed(codex_ask())
    assert refusal.kind == "send" and not codec._approvals


@pytest.mark.parametrize(
    "method", ["item/fileChange/requestApproval", "mcpServer/elicitation/request"]
)
def test_codex_a_file_change_or_mcp_call_of_its_own_turn_stays_refused(method):
    codec = codex_agent_turn()
    (refusal,) = codec.feed(codex_ask(method=method))
    assert refusal.kind == "send" and not codec._approvals


def test_codex_the_agents_turn_ending_refuses_and_retracts_it():
    codec = codex_agent_turn()
    codec.feed(codex_ask())
    events = codec.feed(
        {
            "method": "turn/completed",
            "params": {"threadId": SESSION, "turn": {"id": AGENT_TURN, "status": "completed"}},
        }
    )
    assert [e.kind for e in events] == ["background", "send", "approval_cancelled"]
    assert events[1].data["frame"]["result"]["decision"] == "decline"
    assert not codec._approvals


def test_codex_an_operator_turns_requests_are_unchanged():
    codec = codex()
    (event,) = codec.feed(codex_request())
    assert event.data["operation_id"] == OPERATION and "background_task" not in event.data
    assert event.data["choices"] == ["approve", "reject", "cancel"]


# --- opencode and Copilot: nothing correlates, so today's refusal stays -------------------------


@pytest.mark.parametrize("make", [opencode, copilot], ids=["opencode", "copilot"])
def test_acp_a_request_outside_the_operators_turn_keeps_its_refusal(make):
    """ACP's request names a session and a tool call, never an agent or an agent-started turn."""
    codec = make(running=False)
    (refusal,) = codec.feed(acp_request(9, codec.native_id, agent_id="agent-1"))
    assert refusal.kind == "send"
    assert refusal.data["frame"]["result"]["outcome"]["outcome"] == "cancelled"
    assert not codec._approvals


# --- the worker: its own recheck, independent of the server ----------------------------------


def _decide(worker, binding, root, sid, event, decision, **params):
    request = journal_tests.request(
        binding,
        action="decide",
        revision=native_journal.read(root, sid).revision,
        turn_id=event.data["background_task"]["id"],
        request_id=event.data["request_id"],
        item_id=event.data["item_id"],
        payload_digest=event.data["payload_digest"],
        decision=decision,
        approval_worker_id=worker.worker_id,
        approval_connection_id=binding.connection_id,
        actor="operator",
        **params,
    )
    return asyncio.run(worker.effect(native_ipc.validate_request(request)))


def _live(conversation):  # noqa: F811
    root, sid, binding = conversation
    codec = ClaudeCodec(SESSION)
    worker = _worker(conversation, codec)
    codec.generation = worker.worker_id
    worker.floor = Floor("ok", ())
    worker.gate_open = lambda: True
    worker.config["cwd"] = "/tmp"
    written = []

    async def write(frame):
        written.append(frame)

    worker.write = write
    worker.written_frames = written
    codec.feed(
        _task("task_started", AGENT, description="test-runner sub-agent", is_backgrounded=True)
    )
    (event,) = ask(codec)
    worker.record([event])
    worker.flush()
    return root, sid, binding, worker, event


def test_the_worker_writes_an_approve_once_with_the_original_input(conversation):
    root, sid, binding, worker, event = _live(conversation)
    answer = _decide(worker, binding, root, sid, event, "approve")
    assert answer["result"]["handoff"] == "sent"
    (frame,) = worker.written_frames
    assert frame["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "ls"},
    }


def test_always_is_refused_at_the_worker(conversation):
    root, sid, binding, worker, event = _live(conversation)
    answer = _decide(worker, binding, root, sid, event, "always", grant=GRANT)
    assert answer["error"]["code"] == "unsupported" and not worker.written_frames
    assert event.data["request_id"] in worker.codec._approvals


@pytest.mark.parametrize("decision", ["cancel"])
def test_only_once_or_decline_at_the_worker(conversation, decision):
    root, sid, binding, worker, event = _live(conversation)
    answer = _decide(worker, binding, root, sid, event, decision)
    assert answer["error"]["code"] == "unsupported" and not worker.written_frames


def test_the_worker_refuses_a_task_that_ended_or_was_replaced(conversation):
    root, sid, binding, worker, event = _live(conversation)
    worker.codec._task_facts[AGENT]["epoch"] = 999  # the same id, another task
    answer = _decide(worker, binding, root, sid, event, "approve")
    assert answer["error"]["code"] == "stale" and not worker.written_frames


def test_the_worker_refuses_a_digest_or_turn_that_does_not_match(conversation):
    root, sid, binding, worker, event = _live(conversation)
    tampered = type(event)(event.kind, {**event.data, "payload_digest": "0" * 64})
    assert _decide(worker, binding, root, sid, tampered, "approve")["error"]["code"] == "stale"
    other = type(event)(
        event.kind,
        {
            **event.data,
            "background_task": {**event.data["background_task"], "id": str(uuid.uuid4())},
        },
    )
    assert _decide(worker, binding, root, sid, other, "approve")["error"]["code"] == "stale"
    assert not worker.written_frames


# --- the facade, then all of it together: the real worker against the scripted native ---------


async def _with_request(project):  # noqa: F811
    (project / "emit-lifecycle").write_text("")
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident()))[
        "session_key"
    ]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BGASK")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "started"
    (project / "ask-bg").write_text("")  # asked with no operator turn running
    snap = await pending(key)
    return key, snap, snap["pending_requests"][0]


def _answers(project, request_id="perm-bg"):  # noqa: F811
    return [
        f["frame"]["response"]
        for f in frames(project)
        if f["frame"].get("type") == "control_response"
        and f["frame"]["response"].get("request_id") == request_id
    ]


@pytest.mark.anyio
async def test_it_is_pending_with_no_operator_turn_and_decidable(host, project):  # noqa: F811
    key, snap, req = await _with_request(project)
    assert snap["state"] == "idle" and snap["active_turn"] is None
    assert req["background_task"] == {"task_id": AGENT, "description": "test-runner sub-agent"}
    assert req["choices"] == ["approve", "reject"] and req["always"] == []
    assert req["context"] == {}
    # Its turn id is its own: no operator turn has it.
    assert req["turn_id"] not in {t["turn_id"] for t in snap["turns"]}
    out = await runtime.decide(
        key,
        turn_id=req["turn_id"],
        request_id=req["request_id"],
        decision_id=ident(),
        decision="approve",
        user="operator",
    )
    assert out["handoff"] == "sent"
    (answer,) = _answers(project)
    assert answer["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "pytest -x tests/"},
    }
    assert not (await runtime.snapshot(key))["pending_requests"]
    (project / "end-bg").write_text("")


@pytest.mark.anyio
async def test_it_stays_decidable_while_and_after_operator_turns_run(host, project):  # noqa: F811
    key, _, req = await _with_request(project)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="meanwhile")
    _, turn = await settle(key, nxt)  # an open background request held no message back
    assert turn["reply"] == "echo:meanwhile"
    still = await runtime.snapshot(key)
    assert [r["request_id"] for r in still["pending_requests"]] == [req["request_id"]]
    await runtime.decide(
        key,
        turn_id=req["turn_id"],
        request_id=req["request_id"],
        decision_id=ident(),
        decision="reject",
        user="operator",
    )
    (answer,) = _answers(project)
    assert answer["response"]["behavior"] == "deny"
    (project / "end-bg").write_text("")


@pytest.mark.anyio
async def test_always_is_refused_at_the_facade_before_the_worker(host, project, monkeypatch):  # noqa: F811
    key, _, req = await _with_request(project)
    calls = []
    real = native_runtime._existing_generation_call

    async def counting(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "_existing_generation_call", counting)
    for decision, grant in (("always", GRANT), ("cancel", None)):
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.decide(
                key,
                turn_id=req["turn_id"],
                request_id=req["request_id"],
                decision_id=ident(),
                decision=decision,
                user="operator",
                grant=grant,
            )
        assert refused.value.status == 409
    assert not calls and not _answers(project)
    assert (await runtime.snapshot(key))["pending_requests"]
    (project / "end-bg").write_text("")


@pytest.mark.anyio
async def test_the_facade_refuses_always_and_cancel_even_if_they_were_offered(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Its own check, not just what the projection happens to offer: however a request came to
    list them (a future worker, a bug), the origin decides."""
    real = native_runtime._background_request

    def overoffered(*args):
        return {
            **real(*args),
            "choices": ["approve", "reject", "cancel"],
            "always": [{"id": GRANT, "label": "everything", "scope": "session"}],
        }

    monkeypatch.setattr(native_runtime, "_background_request", overoffered)
    monkeypatch.setattr(native_grants, "public", lambda choice: choice)
    key, _, req = await _with_request(project)
    calls = []
    real_call = native_runtime._existing_generation_call

    async def counting(*args, **kwargs):
        calls.append(args)
        return await real_call(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "_existing_generation_call", counting)
    for decision, grant in (("always", GRANT), ("cancel", None)):
        with pytest.raises(runtime.StructuredError, match="approved once or declined"):
            await runtime.decide(
                key,
                turn_id=req["turn_id"],
                request_id=req["request_id"],
                decision_id=ident(),
                decision=decision,
                user="operator",
                grant=grant,
            )
    assert not calls and not _answers(project)  # refused here, before the worker is asked
    (project / "end-bg").write_text("")


@pytest.mark.anyio
async def test_the_task_exiting_retracts_it_and_refuses_at_decision(host, project):  # noqa: F811
    key, _, req = await _with_request(project)
    (project / "end-bg").write_text("")
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if not snap["pending_requests"]:
            break
        await asyncio.sleep(0.05)
    assert not snap["pending_requests"]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.decide(
            key,
            turn_id=req["turn_id"],
            request_id=req["request_id"],
            decision_id=ident(),
            decision="approve",
            user="operator",
        )
    assert refused.value.status == 409
    assert all(a["response"]["behavior"] == "deny" for a in _answers(project))


@pytest.mark.anyio
async def test_a_stale_generation_is_refused(host, project):  # noqa: F811
    key, snap, req = await _with_request(project)
    inventory = snap["native"]["stop_inventory"]
    stopped = await runtime.operator_stop(
        key, worker=inventory["worker"], digest=inventory["digest"]
    )
    assert stopped["containment"] == "gone"
    assert not (await runtime.snapshot(key))["pending_requests"]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.decide(
            key,
            turn_id=req["turn_id"],
            request_id=req["request_id"],
            decision_id=ident(),
            decision="approve",
            user="operator",
        )
    assert refused.value.status == 409
    assert not _answers(project)


@pytest.mark.anyio
async def test_replaying_a_decision_observes_and_writes_once(host, project):  # noqa: F811
    key, _, req = await _with_request(project)
    decision_id = ident()
    kwargs = dict(
        turn_id=req["turn_id"],
        request_id=req["request_id"],
        decision_id=decision_id,
        decision="approve",
        user="operator",
    )
    first = await runtime.decide(key, **kwargs)
    again = await runtime.decide(key, **kwargs)
    assert first["handoff"] == again["handoff"] == "sent" and len(_answers(project)) == 1
    with pytest.raises(runtime.StructuredError):
        await runtime.decide(key, **{**kwargs, "decision": "reject"})
    (project / "end-bg").write_text("")


def _journal_request(root, sid, binding, worker):
    """A task of this generation, and a request it made, as the worker journals them."""
    codec = ClaudeCodec(SESSION)
    codec.generation = worker
    (started,) = codec.feed(
        _task("task_started", AGENT, description="test-runner sub-agent", is_backgrounded=True)
    )
    (asked,) = ask(codec)
    data = {**asked.data, "worker_id": worker, "connection_id": binding.connection_id}
    native_journal.append_events(
        root,
        binding,
        [{"kind": "background", "data": started.data}, {"kind": "approval", "data": data}],
    )
    return data


def _view(root, sid, live):
    return native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": "claude-stream-json"},
        live,
    )


def test_it_survives_a_journal_rollover_undecided(conversation, monkeypatch):
    """Carried like any open request (#1733): still pending, still the same digest."""
    root, sid, binding = conversation
    data = _journal_request(root, sid, binding, binding.worker_id)
    before = native_segments.indexes(root, sid)
    roll(root, sid, binding, monkeypatch)
    assert native_segments.indexes(root, sid) != before
    (req,) = _view(root, sid, binding.worker_id)["pending_requests"]
    assert req["payload_digest"] == data["payload_digest"]
    assert req["background_task"]["task_id"] == AGENT


def test_the_projection_needs_the_task_running_in_the_live_generation(conversation):
    root, sid, binding = conversation
    _journal_request(root, sid, binding, binding.worker_id)
    assert _view(root, sid, binding.worker_id)["pending_requests"]
    # Another generation is live: nothing of the old one's is offered.
    assert not _view(root, sid, str(uuid.uuid4()))["pending_requests"]
    assert not _view(root, sid, None)["pending_requests"]
    # The journal says the task ended: not offered, however the worker missed it.
    native_journal.append_events(
        root,
        binding,
        [
            {
                "kind": "background",
                "data": {"task_id": AGENT, "state": "completed", "task_state": "completed"},
            }
        ],
    )
    assert not _view(root, sid, binding.worker_id)["pending_requests"]


def test_an_id_that_ended_and_started_again_is_not_offered_from_the_journal(conversation):
    """A task id's end is final for its generation in the journal's fold, so the server's recheck
    can't be fooled by a reuse: the worker's epoch is the second, independent check."""
    root, sid, binding = conversation
    _journal_request(root, sid, binding, binding.worker_id)
    for state in ("completed", "running"):
        native_journal.append_events(
            root,
            binding,
            [
                {
                    "kind": "background",
                    "data": {"task_id": AGENT, "state": state, "task_state": state},
                }
            ],
        )
    assert not _view(root, sid, binding.worker_id)["pending_requests"]


def test_a_public_share_shows_no_origin(conversation):
    """Real codec output through the real public projection: what the codec presents names the
    task, its description and the command. None of it reaches a visitor, nor its kind or risk."""
    root, sid, binding = conversation
    codec = ClaudeCodec(SESSION)
    codec.generation = binding.worker_id
    (started,) = codec.feed(
        _task(
            "task_started", "agent-x", description="Migrate billing secrets", is_backgrounded=True
        )
    )
    (asked,) = codec.feed(claude_request(agent_id="agent-x", input={"command": "rm -rf /"}))
    assert "Migrate billing secrets" in asked.data["summary"]  # the operator's card names it
    data = {**asked.data, "worker_id": binding.worker_id, "connection_id": binding.connection_id}
    native_journal.append_events(
        root,
        binding,
        [{"kind": "background", "data": started.data}, {"kind": "approval", "data": data}],
    )
    raw = _view(root, sid, binding.worker_id)
    raw.setdefault("read_only", None)
    operator = runtime._snapshot(binding.session_key, raw)
    (shown,) = operator["pending_requests"]
    assert shown["background_task"]["description"] and shown["kind"] == "Bash" and shown["risk"]
    public = share_api.project(operator, {}, share_api._Redactor([]), "share-1")
    (request,) = public["pending_requests"]
    dumped = json.dumps(public)
    for private in (
        "agent-x",
        "Migrate billing secrets",
        "background_task",
        "agent_id",
        "rm -rf",
        "Bash",
        "native-1",
    ):
        assert private not in dumped, private
    assert request == {
        "request_id": shown["turn_id"],
        "turn_id": shown["turn_id"],
        "summary": share_api.BACKGROUND_REQUEST_SUMMARY,
    }


# --- a plan's task list and a background task are two different things ------------------------


def _task_tool(codec, name, tool_input, result, call="toolu_plan"):
    """A Task tool call and its structured result, as an operator turn's frames carry them."""
    codec.feed(
        {
            "type": "assistant",
            "session_id": SESSION,
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": call, "name": name, "input": tool_input}],
            },
        }
    )
    return codec.feed(
        {
            "type": "user",
            "session_id": SESSION,
            "tool_use_result": result,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call, "content": "ok"}],
            },
        }
    )


def test_a_plan_task_list_never_touches_the_background_tasks():
    """Interleaved: a running agent, its open request, and the plan's own list of tasks."""
    codec = background(running=True)
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    assert codec.origin_live(origin)
    _task_tool(codec, "TaskList", {}, {"tasks": []})
    assert codec.origin_live(origin)  # an empty plan says nothing of a running agent
    assert codec.feed(_task("task_progress", AGENT, description="still going"))
    _task_tool(
        codec, "TaskCreate", {"subject": "one"}, {"task": {"id": "1", "subject": "one"}}, "t2"
    )
    assert codec.origin_live(origin)
    # A plan task named like the agent is still only a plan task.
    _task_tool(
        codec, "TaskList", {}, {"tasks": [{"id": AGENT, "subject": "s", "status": "new"}]}, "t3"
    )
    assert codec.origin_live(origin) and codec.decide("s:native-bg", "approve")


def test_a_background_task_never_lands_in_the_plan():
    codec = background(running=True)
    events = _task_tool(codec, "TaskList", {}, {"tasks": [{"id": "1", "subject": "one"}]})
    plans = [e for e in events if e.kind == "plan"]
    assert [row["text"] for row in plans[-1].data["entries"]] == ["one"]
    codec.feed(_task("task_started", "other-agent", description="agent", is_backgrounded=True))
    events = _task_tool(codec, "TaskCreate", {"subject": "two"}, {"task": {"id": "2"}}, "t2")
    plans = [e for e in events if e.kind == "plan"]
    assert [row["text"] for row in plans[-1].data["entries"]] == ["one", "two"]


def test_no_codec_assigns_one_attribute_for_two_things():
    """A merge can put two `self._x = …` in one `__init__`: the second silently wins (#1725, #1726
    both named a map `_tasks`). Branches of an `if`, or a bare annotation, aren't that."""
    tree = ast.parse(Path(native_protocol.__file__).read_text())
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for init in (n for n in cls.body if getattr(n, "name", "") == "__init__"):
            seen = collections.Counter(
                target.attr
                for node in init.body
                if isinstance(node, ast.Assign | ast.AnnAssign)
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Attribute)
            )
            assert not [name for name, count in seen.items() if count > 1], cls.name


# --- a terminal or cancel frame too large to read ------------------------------------------------


def _read_lost(codec, frame):
    """The real worker's framing and hand-off, up to the frame: its EOF ends everything anyway."""

    async def run():
        raw = json.dumps(frame).encode() + b"\n"
        assert len(raw) > native_protocol.MAX_FRAME_BYTES
        reader = test_native_safety_table._Reader(raw, codec)
        async for line in reader.lines():
            assert line == b""
            await reader.dropped_frame()
        return reader

    return asyncio.run(run())


BIG_BODY = test_native_safety_table.BIG["blob"]


@pytest.mark.parametrize(
    "frame",
    [
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": AGENT,
            "status": "completed",
            "summary": BIG_BODY,
        },
        {
            "summary": BIG_BODY,
            "status": "failed",
            "task_id": AGENT,
            "subtype": "task_notification",
            "type": "system",
        },
    ],
    ids=["fields-first", "fields-last"],
)
def test_a_lost_task_notification_with_its_identity_retires_the_task(frame):
    codec = background()
    ask(codec)
    reader = _read_lost(codec, frame)
    assert not codec.unread_frame
    assert not codec._approvals and AGENT not in codec._background
    answers = [e for e in reader.handled if e.kind == "send"]
    assert [a.data["frame"]["response"]["response"]["behavior"] for a in answers] == ["deny"]
    assert "approval_cancelled" in [e.kind for e in reader.handled]
    # The journal hears of it the same way, so the task leaves the pane's list too.
    ended = [e for e in reader.handled if e.kind == "background" and e.data.get("task_id") == AGENT]
    assert ended and ended[0].data["task_state"] in native_protocol.TERMINAL_TASK_STATES
    with pytest.raises(ProtocolError):
        codec.decide("s:native-bg", "approve")


def test_a_lost_task_end_is_applied_in_the_cycle_waiting_for_a_result_too():
    codec = background()
    ask(codec)
    codec._cycle = "ended"
    frame = {"type": "system", "subtype": "task_notification", "task_id": AGENT}
    _read_lost(codec, {**frame, "status": "completed", "summary": BIG_BODY})
    assert not codec._approvals and AGENT not in codec._background


def test_a_lost_task_report_of_unknown_status_refuses_its_requests_and_stops_vouching():
    codec = background()
    ask(codec)
    reader = _read_lost(
        codec,
        {"type": "system", "subtype": "task_updated", "task_id": AGENT, "patch": {"x": BIG_BODY}},
    )
    assert not codec._approvals and [e for e in reader.handled if e.kind == "send"]
    assert codec.overflow  # it may still run: busy, and the set is no longer vouched for
    assert AGENT in codec._background


def test_a_lost_task_progress_is_a_report_that_was_not_read():
    """It can carry `is_backgrounded`, so what it says is not known either."""
    codec = background()
    ask(codec)
    _read_lost(
        codec, {"type": "system", "subtype": "task_progress", "task_id": AGENT, "x": BIG_BODY}
    )
    assert not codec._approvals and codec.overflow and AGENT in codec._quarantined


@pytest.mark.parametrize(
    "frame",
    [
        {
            "type": "system",
            "subtype": "task_notification",
            "a": BIG_BODY,
            "task_id": AGENT,
            "b": BIG_BODY,
        },
        {"type": "system", "a": BIG_BODY, "subtype": "task_notification", "b": BIG_BODY},
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"x": BIG_BODY}]},
        {"type": "control_cancel_request", "a": BIG_BODY, "request_id": "native-bg", "b": BIG_BODY},
    ],
    ids=["no-task-id", "no-subtype", "whole-list", "no-request-id"],
)
def test_a_lost_terminal_frame_with_no_identity_invalidates_every_background_request(frame):
    """Which task or request it ended can't be said, so none is asserted still live: every open
    background-origin request is refused and retracted, and the set is marked unknown."""
    codec = background()
    codec.feed(_task("task_started", "other", is_backgrounded=True))
    ask(codec)
    codec.feed({**claude_request(agent_id="other"), "request_id": "native-other"})
    reader = _read_lost(codec, frame)
    assert not codec._approvals
    assert len([e for e in reader.handled if e.kind == "send"]) == 2
    assert sum(e.kind == "approval_cancelled" for e in reader.handled) == 2
    assert codec.overflow and not codec.unread_frame
    for key in ("s:native-bg", "s:native-other"):
        with pytest.raises(ProtocolError):
            codec.decide(key, "approve")


def test_an_operator_turns_own_request_survives_a_lost_frame_about_something_else():
    codec = background(running=True)
    codec.feed(claude_request())
    ask(codec)
    _read_lost(
        codec, {"type": "system", "a": BIG_BODY, "subtype": "task_notification", "b": BIG_BODY}
    )
    assert list(codec._approvals) == ["s:native-1"]


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "control_cancel_request", "request_id": "native-bg", "padding": BIG_BODY},
        {"padding": BIG_BODY, "request_id": "native-bg", "type": "control_cancel_request"},
    ],
    ids=["id-first", "id-last"],
)
def test_a_lost_cancel_with_its_identity_retracts_that_request_alone(frame):
    codec = background()
    ask(codec)
    codec.feed(_task("task_started", "other", is_backgrounded=True))
    codec.feed({**claude_request(agent_id="other"), "request_id": "native-other"})
    reader = _read_lost(codec, frame)
    assert list(codec._approvals) == ["s:native-other"]
    assert [e.data["request_id"] for e in reader.handled if e.kind == "approval_cancelled"] == [
        "s:native-bg"
    ]
    assert not codec.overflow
    with pytest.raises(ProtocolError):
        ask(codec)  # a repeat of a withdrawn callback is refused, as for any answered one


def test_a_lost_frame_about_nothing_of_the_kind_leaves_requests_alone():
    codec = background()
    ask(codec)
    _read_lost(codec, {"type": "system", "subtype": "status", "x": BIG_BODY})
    _read_lost(codec, {"type": "assistant", "message": {"x": BIG_BODY}})
    assert codec._approvals and not codec.overflow


def test_codex_a_lost_resolved_frame_invalidates_its_own_turns_requests():
    codec = codex_agent_turn()
    codec.feed(codex_ask())
    reader = _read_lost(codec, {"method": "serverRequest/resolved", "params": {"x": BIG_BODY}})
    assert not codec._approvals and codec.overflow and not codec.unread_frame
    assert "approval_cancelled" in [e.kind for e in reader.handled]


def test_after_invalidation_the_set_is_unknown_until_a_whole_list_says():
    codec = background()
    ask(codec)
    _read_lost(codec, {"type": "control_cancel_request", "a": BIG_BODY, "b": BIG_BODY})
    assert codec.overflow
    codec.feed(
        {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": [{"task_id": AGENT, "task_type": "local_agent", "description": "d"}],
        }
    )
    assert not codec.overflow and AGENT in codec._background


# --- a task whose latest state a lost frame hid is quarantined ----------------------------------

LOST_TERMINAL = {
    "type": "system",
    "subtype": "task_updated",
    "task_id": AGENT,
    "patch": {"status": "completed", "padding": BIG_BODY},
}


def _whole_list(*ids):
    tasks = [{"task_id": task, "task_type": "local_agent", "description": "d"} for task in ids]
    return {"type": "system", "subtype": "background_tasks_changed", "tasks": tasks}


def _refused_at_once(codec, why, request_id="native-next", **extra):
    events = ask(codec, request_id=request_id, **extra)
    assert "approval" not in [e.kind for e in events]
    assert [e for e in events if e.kind == "send"]
    assert why in json.dumps([e.data for e in events if e.kind != "send"])
    assert codec._approvals == {} or f"s:{request_id}" not in codec._approvals


def _presented(codec, request_id="native-next"):
    (event,) = ask(codec, request_id=request_id)
    assert event.kind == "approval" and event.data["background_task"]["task_id"] == AGENT
    return event


def test_a_lost_terminal_update_then_a_new_request_is_not_presented_or_allowable():
    """The finding: the end was outside what was kept, so the id must not be trusted again."""
    codec = background()
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    _read_lost(codec, LOST_TERMINAL)
    assert not codec.origin_live(origin)
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)
    with pytest.raises(ProtocolError):
        codec.decide("s:native-next", "approve")
    # Whatever the agent repeats, it isn't held for a decision either.
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)
    assert not codec._approvals


def test_a_lost_update_then_a_whole_list_naming_the_task_lifts_it_at_a_new_epoch():
    codec = background()
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    _read_lost(codec, LOST_TERMINAL)
    events = codec.feed(_whole_list(AGENT))
    # The journal hears it runs again, in the same form as any task it starts to list.
    assert [e.data["task_state"] for e in events if e.kind == "background"] == ["running"]
    assert not codec.overflow and not codec._quarantined
    assert not codec.origin_live(origin)  # the id may name another task now: not that origin
    _presented(codec)
    frame = codec.decide("s:native-next", "approve")
    assert frame["response"]["response"]["behavior"] == "allow"


def test_a_lost_update_then_a_whole_list_not_naming_the_task_ends_it():
    codec = background()
    ask(codec)
    _read_lost(codec, LOST_TERMINAL)
    events = codec.feed(_whole_list())
    assert [e.data["task_state"] for e in events if e.kind == "background"] == ["ended"]
    assert AGENT not in codec._background and not codec._quarantined and not codec.overflow
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK)


def test_a_foreground_task_quarantined_ends_when_a_whole_list_doesnt_name_it():
    """Leaving the list is no end for a foreground task. Its own terminal frame is, and that may
    be what was lost: left, it would keep the worker busy for ever."""
    codec = claude()
    _foreground(codec)
    _read_lost(codec, LOST_TERMINAL)
    assert AGENT in codec._background
    events = codec.feed(_whole_list())
    assert AGENT not in codec._background
    # The journal hears it too: its strip row doesn't stay.
    assert [e.data["task_state"] for e in events if e.kind == "background"] == ["ended"]


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "system", "a": BIG_BODY, "subtype": "task_updated", "b": BIG_BODY},
        {
            "type": "system",
            "subtype": "task_updated",
            "a": BIG_BODY,
            "task_id": AGENT,
            "b": BIG_BODY,
        },
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"x": BIG_BODY}]},
        {"type": "control_cancel_request", "a": BIG_BODY, "request_id": "x", "b": BIG_BODY},
    ],
    ids=["no-subtype", "no-task-id", "whole-list", "no-request-id"],
)
def test_no_readable_identity_quarantines_every_task_known_then(frame):
    codec = background()
    codec.feed(_task("task_started", "other", is_backgrounded=True))
    origin = {**codec._task_origin("native-x", "other")}
    _read_lost(codec, frame)
    assert codec._quarantined == {AGENT, "other"} and not codec.origin_live(origin)
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK, "native-o", agent_id="other")
    # A task first reported after it was not what the lost frame was about.
    codec.feed(_task("task_started", "later", is_backgrounded=True))
    (event,) = ask(codec, request_id="native-later", agent_id="later")
    assert event.kind == "approval"
    # And a list settles them one by one.
    codec.feed(_whole_list(AGENT, "later"))
    assert not codec._quarantined and "other" not in codec._background
    _presented(codec)
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK, "native-o2", agent_id="other")


@pytest.mark.parametrize(
    "frame",
    [
        _task("task_progress", AGENT, description="still going"),
        _task("task_progress", AGENT, is_backgrounded=True),
        _task("task_notification", AGENT, status="running"),
        _task("task_updated", AGENT, patch={"status": "running", "is_backgrounded": True}),
        _task("task_updated", AGENT, patch={"is_backgrounded": True}),
        _task("task_updated", AGENT, patch={"description": "renamed"}),
    ],
    ids=[
        "progress",
        "progress-backgrounded",
        "notification-running",
        "updated-running",
        "updated-backgrounded",
        "updated-sparse",
    ],
)
def test_no_report_about_one_task_re_trusts_it(frame):
    """They are what a stale or partial view could say. Only a whole list knows what runs."""
    codec = background()
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    _read_lost(codec, LOST_TERMINAL)
    events = codec.feed(frame)
    # And its journal view doesn't move back to running either.
    assert [e.data["task_state"] for e in events if e.kind == "background"] == ["uncertain"]
    assert AGENT in codec._quarantined and codec.overflow and not codec.origin_live(origin)
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)


def test_duplicate_lost_frames_change_nothing_more():
    codec = background()
    _read_lost(codec, LOST_TERMINAL)
    reader = _read_lost(codec, LOST_TERMINAL)
    assert codec._quarantined == {AGENT}
    assert [e.data["task_state"] for e in reader.handled if e.kind == "background"] == ["uncertain"]
    codec.feed(_whole_list(AGENT))
    _presented(codec)
    assert not codec._quarantined


def test_a_lost_terminal_notification_after_it_ends_the_quarantined_task():
    codec = background()
    _read_lost(codec, LOST_TERMINAL)
    _read_lost(
        codec,
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": AGENT,
            "status": "completed",
            "summary": BIG_BODY,
        },
    )
    assert not codec._quarantined and AGENT not in codec._background
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK)


def test_a_readable_terminal_frame_ends_a_quarantined_task():
    codec = background()
    _read_lost(codec, LOST_TERMINAL)
    events = codec.feed(_task("task_notification", AGENT, status="completed"))
    assert [e.data["task_state"] for e in events if e.kind == "background"] == ["completed"]
    assert not codec._quarantined
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK)


def test_the_same_id_started_again_never_makes_a_quarantined_task_decidable():
    """A task_started of a tracked id is another task: its id stays spent for this generation, so
    a repeat of the frame can't re-trust what a lost one hid."""
    codec = background()
    _read_lost(codec, LOST_TERMINAL)
    codec.feed(_task("task_started", AGENT, description="again", is_backgrounded=True))
    assert not codec._quarantined and codec._task_facts[AGENT]["epoch"] > 1
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK)


LOST_START = {"type": "system", "subtype": "task_started", "task_id": AGENT, "x": BIG_BODY}


def test_a_task_started_for_a_named_unknown_id_is_its_new_epoch():
    """The lost frame named an id no task had: its own readable start says what it is."""
    codec = claude()
    _read_lost(codec, LOST_START)
    assert AGENT not in codec._background and codec._quarantined == {AGENT}
    codec.feed(_task("task_started", AGENT, is_backgrounded=True))
    assert not codec._quarantined
    _presented(codec)


def test_progress_claiming_a_named_unknown_id_is_backgrounded_does_not_trust_it():
    codec = claude()
    _read_lost(codec, LOST_START)
    codec.feed(_task("task_progress", AGENT, is_backgrounded=True))
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)
    # It is tracked now, so a start is the same id started again: spent.
    codec.feed(_task("task_started", AGENT, is_backgrounded=True))
    _refused_at_once(codec, native_protocol.REQUEST_ENDED_TASK)


def test_a_named_id_past_the_bound_refuses_every_task_never_trusts_one():
    """Past the bound nothing more is remembered, so nothing is trusted, until a list says."""
    codec = background()
    for n in range(MAX_PENDING + 3):
        _read_lost(codec, {**LOST_START, "task_id": f"id-{n}"})
    _refused_at_once(codec, native_protocol.REQUEST_UNCERTAIN_TASK)
    codec.feed(_whole_list(AGENT))
    _presented(codec)


def test_a_whole_list_past_the_bound_lifts_only_what_it_kept():
    codec = background()
    _read_lost(codec, LOST_TERMINAL)
    names = [AGENT, *(f"t{n:03}" for n in range(MAX_PENDING))]
    codec.feed(_whole_list(*names))
    assert codec.overflow and not codec._quarantined  # kept: what was tracked first
    _presented(codec)


def test_a_new_generation_trusts_nothing_from_the_old_one():
    codec = background()
    ask(codec)
    origin = codec._approvals["s:native-bg"]["origin"]
    _read_lost(codec, LOST_TERMINAL)
    fresh = claude()
    fresh.generation = "next"
    assert not fresh.origin_live(origin)
    fresh.feed(_task("task_started", AGENT, is_backgrounded=True))
    fresh.feed(_whole_list(AGENT))
    assert not fresh.origin_live(origin)


def test_codex_a_lost_resolved_says_nothing_of_its_turn_so_a_new_request_is_presented():
    """The agent's own turn is the origin, and its end is named on the frame's own ends."""
    codec = codex_agent_turn()
    _read_lost(codec, {"method": "serverRequest/resolved", "params": {"x": BIG_BODY}})
    (event,) = codec.feed(codex_ask(request_id=8))
    assert event.kind == "approval"


def test_codex_a_lost_completed_while_an_operator_turn_also_runs_retires_its_own_turns_requests():
    codec = codex_agent_turn()
    codec.feed(codex_ask())
    origin = next(iter(codec._approvals.values()))["origin"]
    codec.operation_id = str(uuid.uuid4())
    _read_lost(codec, {"method": "turn/completed", "params": {"x": BIG_BODY}})
    assert not codec._approvals and not codec.origin_live(origin)
    assert codec.operation_id is None and codec._unsolicited is None
    (refusal,) = codec.feed(codex_ask(request_id=9))
    assert refusal.kind == "send"


def test_codex_a_lost_turn_completed_with_its_name_out_of_reach_ends_the_connection():
    codec = codex_agent_turn()
    codec.feed(codex_ask())
    _read_lost(codec, {"a": BIG_BODY, "method": "turn/completed", "b": BIG_BODY})
    assert codec.unread_frame


def test_the_journal_view_shows_a_quarantined_task_as_uncertain_and_its_request_as_gone(
    conversation,
):
    root, sid, binding = conversation
    data = _journal_request(root, sid, binding, binding.worker_id)
    assert _view(root, sid, binding.worker_id)["pending_requests"]
    task = data["background_task"]["task_id"]
    state = {"task_id": task, "active": True}
    native_journal.append_events(
        root,
        binding,
        [
            {
                "kind": "background",
                "data": {**state, "state": "uncertain", "task_state": "uncertain"},
            }
        ],
    )
    view = _view(root, sid, binding.worker_id)
    # A stale pending the worker's retraction never reached: not offered, so not decidable.
    assert view["pending_requests"] == []
    assert [row["state"] for row in view["native"]["background_tasks"]] == ["uncertain"]
    # A sparse report doesn't move it back; a whole list naming it does.
    native_journal.append_events(
        root,
        binding,
        [{"kind": "background", "data": {**state, "state": "running", "task_state": "uncertain"}}],
    )
    assert _view(root, sid, binding.worker_id)["pending_requests"] == []
    native_journal.append_events(
        root,
        binding,
        [{"kind": "background", "data": {**state, "state": "running", "task_state": "running"}}],
    )
    assert _view(root, sid, binding.worker_id)["pending_requests"]


@pytest.mark.anyio
async def test_a_real_worker_quarantines_on_a_real_oversized_frame(host, project):  # noqa: F811
    key, _, first = await _with_request(project)
    (project / "lose-bg").write_text("")
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if (
            not snap["pending_requests"]
            and snap["native"]["background_tasks"][0]["state"] == "uncertain"
        ):
            break
        await asyncio.sleep(0.05)
    assert snap["native"]["background_tasks"][0]["state"] == "uncertain"
    assert not snap["pending_requests"] and snap["native"]["stop_inventory"]["overflow"]
    assert _answers(project)[0]["response"]["behavior"] == "deny"
    # It's stale through the facade too: nothing can be allowed.
    with pytest.raises(Exception):  # noqa: B017
        await runtime.decide(
            key,
            turn_id=first["turn_id"],
            request_id=first["request_id"],
            decision_id=ident(),
            decision="approve",
            user="operator",
        )
    # The same task asks again: refused at once, with a row, and nothing to decide.
    (project / "ask-again").write_text("")
    for _ in range(200):
        if _answers(project, "perm-again"):
            break
        await asyncio.sleep(0.05)
    assert _answers(project, "perm-again")[0]["subtype"] == "error"  # never held for a decision
    assert not (await runtime.snapshot(key))["pending_requests"]
    # A whole list names it, and its requests work again.
    (project / "list-bg").write_text("")
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if snap["native"]["background_tasks"][0]["state"] == "running":
            break
        await asyncio.sleep(0.05)
    assert snap["native"]["background_tasks"][0]["state"] == "running"
    (project / "ask-third").write_text("")
    third = (await pending(key))["pending_requests"][0]
    assert third["request_id"] == "s:perm-third"
    await runtime.decide(
        key,
        turn_id=third["turn_id"],
        request_id=third["request_id"],
        decision_id=ident(),
        decision="approve",
        user="operator",
    )
    for _ in range(200):
        if _answers(project, "perm-third"):
            break
        await asyncio.sleep(0.05)
    assert _answers(project, "perm-third")[0]["response"]["behavior"] == "allow"


# --- what a Stop retired, for the pane to attribute --------------------------------------------


def test_the_projection_names_requests_open_when_their_worker_ended(conversation):
    root, sid, binding = conversation
    data = _journal_request(root, sid, binding, binding.worker_id)
    (row,) = _view(root, sid, None)["native"]["ended_requests"]
    assert row == {
        "worker": binding.worker_id,
        "turn_id": data["background_task"]["id"],
        "request_id": data["request_id"],
    }
    # While its worker is live it is pending, not ended.
    assert "ended_requests" not in _view(root, sid, binding.worker_id)["native"]


def test_a_decided_or_retracted_request_was_not_retired_by_any_stop(conversation):
    root, sid, binding = conversation
    data = _journal_request(root, sid, binding, binding.worker_id)
    native_journal.append_events(
        root, binding, [{"kind": "approval_cancelled", "data": {"request_id": data["request_id"]}}]
    )
    assert "ended_requests" not in _view(root, sid, None)["native"]


@pytest.mark.anyio
async def test_stop_says_which_requests_it_retired_and_a_decided_one_is_not_among_them(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key, snap, req = await _with_request(project)
    inventory = snap["native"]["stop_inventory"]
    await runtime.operator_stop(key, worker=inventory["worker"], digest=inventory["digest"])
    after = await runtime.snapshot(key)
    assert not after["pending_requests"]
    assert after["native"]["ended_requests"] == [
        {
            "worker": inventory["worker"],
            "turn_id": req["turn_id"],
            "request_id": req["request_id"],
        }
    ]


@pytest.mark.anyio
async def test_a_request_another_tab_decided_before_stop_is_not_among_what_it_retired(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key, snap, req = await _with_request(project)
    await runtime.decide(
        key,
        turn_id=req["turn_id"],
        request_id=req["request_id"],
        decision_id=ident(),
        decision="reject",
        user="operator",
    )
    for _ in range(100):  # the task may read its answer; only its inventory can have moved
        inventory = (await runtime.snapshot(key))["native"]["stop_inventory"]
        if inventory["tasks"]:
            break
        await asyncio.sleep(0.05)
    await runtime.operator_stop(key, worker=inventory["worker"], everything=True)
    assert "ended_requests" not in (await runtime.snapshot(key))["native"]
