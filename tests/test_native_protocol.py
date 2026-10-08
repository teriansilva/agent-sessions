"""Synthetic native protocol fixtures: no CLI, credentials or inference (#1278)."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from agent_sessions.native_protocol import (
    MAX_FRAME_BYTES,
    MAX_INPUT_TEXT,
    MAX_TEXT,
    ClaudeCodec,
    CodexCodec,
    ProtocolError,
    decode,
    encode,
    validate_text,
)

SESSION = "b6bf1a94-f584-4d7a-b2a2-850de2ab17f5"
OPERATION = "f84b4fa9-d57b-45c1-a219-26dc19b7f661"
TURN = "0199f3ac-53fb-7c10-81ef-ce7c2e732b28"


def codex_session():
    codec = CodexCodec()
    request = codec.create("/project", "gpt-6")
    event = codec.feed(
        {"id": request["id"], "result": {"thread": {"id": SESSION, "turns": []}, "model": "gpt-6"}}
    )[0]
    assert event.kind == "session"
    return codec


def codex_running():
    codec = codex_session()
    request = codec.submit("Inspect the project", OPERATION)
    event = codec.feed(
        {"id": request["id"], "result": {"turn": {"id": TURN, "items": [], "status": "inProgress"}}}
    )[0]
    assert event.kind == "turn_started"
    return codec


def codex_approval(request_id=2, **extra):
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "itemId": "item-1",
            "startedAtMs": 123,
            "command": "git status",
            **extra,
        },
    }


def claude_running():
    codec = ClaudeCodec(SESSION)
    codec.submit("Inspect the project", OPERATION)
    return codec


def claude_approval(request_id="native-1", **extra):
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "input": {"command": "git status", "timeout": 1000},
            **extra,
        },
    }


def test_codex_initialization_and_literal_argv_keep_console_flags_out():
    codec = CodexCodec()
    assert codec.argv("/native/agent") == ["/native/agent", "app-server", "--listen", "stdio://"]
    initialize = codec.initialize()
    assert initialize["params"]["clientInfo"]["name"] == "battlelab_api"
    assert codec.feed({"id": "foreign", "result": {}}) == []
    assert (
        codec.feed({"id": initialize["id"], "result": {"userAgent": "native"}})[0].kind
        == "initialized"
    )
    assert codec.initialized() == {"method": "initialized"}
    start = codec.create("/project", "gpt-6")
    assert start["params"] == {
        "cwd": "/project",
        "model": "gpt-6",
        "approvalPolicy": "untrusted",
        "approvalsReviewer": "user",
        "sandbox": "read-only",
    }


def test_claude_fixed_flags_preserve_auth_and_do_not_widen_permissions():
    argv = ClaudeCodec.argv("/native/agent", SESSION, model="claude-opus-4-7")
    assert "--print" in argv
    assert argv[argv.index("--permission-prompts") + 1] == "host"
    assert "--replay-user-messages" in argv
    assert "--permission-prompt-tool" in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert f"--session-id={SESSION}" in argv
    assert not {"--bare", "--dangerously-skip-permissions", "--allowedTools", "--settings"} & set(
        argv
    )
    assert f"--resume={SESSION}" in ClaudeCodec.argv("/native/agent", SESSION, resume=True)
    assert ClaudeCodec(SESSION).initialize()["request"] == {"subtype": "initialize", "hooks": None}


@pytest.mark.parametrize(
    "binary", ["relative", "--dangerous", "/tmp/../bin/agent", "/tmp/agent\n--flag"]
)
def test_binary_validation(binary):
    with pytest.raises(ProtocolError):
        CodexCodec.argv(binary)
    with pytest.raises(ProtocolError):
        ClaudeCodec.argv(binary, SESSION)


@pytest.mark.parametrize("model", ["--dangerous", "space model", "gpt=flags", "x\n--flags"])
def test_model_values_cannot_add_flags(model):
    with pytest.raises(ProtocolError):
        CodexCodec().create("/project", model)
    with pytest.raises(ProtocolError):
        ClaudeCodec.argv("/native/agent", SESSION, model=model)


def test_session_response_cannot_claim_configured_model_is_actual_execution():
    codec = CodexCodec()
    request = codec.create("/project", "alias")
    result = codec.feed(
        {
            "id": request["id"],
            "result": {
                "thread": {"id": SESSION, "turns": []},
                "model": "actual-selected-config",
                "api_key": "not-public",
            },
        }
    )[0].data
    assert result["model_configured"] == "actual-selected-config"
    assert result["model_effective"] is None
    assert "not-public" not in json.dumps(result)
    codec = ClaudeCodec(SESSION)
    event = codec.feed(
        {"type": "system", "subtype": "init", "session_id": SESSION, "model": "selected-config"}
    )[0]
    assert event.data["model_effective"] is None


def test_read_is_an_observation_not_resume_or_completion():
    codec = CodexCodec()
    request = codec.read(SESSION)
    assert request["method"] == "thread/read"
    turns = [
        {
            "id": f"turn-{i}",
            "status": "completed",
            "items": [{"type": "secret", "value": "not-public"}],
        }
        for i in range(65)
    ]
    events = codec.feed(
        {
            "id": request["id"],
            "result": {"thread": {"id": SESSION, "turns": turns, "model": "configured"}},
        }
    )
    assert [event.kind for event in events] == ["session"]
    assert len(events[0].data["turns"]) == 50
    assert events[0].data["omitted_turns"] == 15
    assert "not-public" not in json.dumps(events[0].data)
    resume = codec.resume(SESSION, "/project")
    assert resume["params"]["threadId"] == SESSION
    assert resume["params"]["approvalPolicy"] == "untrusted"
    assert "input" not in resume["params"]


@pytest.mark.parametrize("action", ["read", "resume"])
def test_native_response_must_match_requested_history(action):
    codec = CodexCodec()
    request = codec.read(SESSION) if action == "read" else codec.resume(SESSION, "/project")
    with pytest.raises(ProtocolError, match="requested thread"):
        codec.feed({"id": request["id"], "result": {"thread": {"id": "wrong-id", "turns": []}}})
    assert codec.native_id is None


def test_codex_early_notifications_wait_for_rpc_turn_identity():
    codec = codex_session()
    request = codec.submit("Hello", OPERATION)
    stale = {
        "method": "turn/completed",
        "params": {"threadId": SESSION, "turn": {"id": "older-turn", "status": "completed"}},
    }
    assert codec.feed(stale) == []
    assert (
        codec.feed(
            {
                "method": "turn/started",
                "params": {"threadId": SESSION, "turn": {"id": TURN, "status": "inProgress"}},
            }
        )
        == []
    )
    assert codec.feed(codex_approval()) == []
    events = codec.feed(
        {"id": request["id"], "result": {"turn": {"id": TURN, "status": "inProgress"}}}
    )
    assert [event.kind for event in events] == [
        "turn_started",
        "background",
        "approval",
    ]
    assert codec.operation_id == OPERATION
    assert codec.native_turn_id == TURN


def test_turn_handoff_carries_operation_identity_without_claiming_dedup():
    codec = codex_session()
    frame = codec.submit("Hello", OPERATION)
    assert frame["params"]["clientUserMessageId"] == OPERATION
    assert frame["params"]["input"] == [{"type": "text", "text": "Hello"}]
    with pytest.raises(ProtocolError, match="already active"):
        codec.submit("Hello", OPERATION)
    claude = ClaudeCodec(SESSION)
    frame = claude.submit("/clear @file", OPERATION)
    assert frame["uuid"] == OPERATION
    assert frame["client_composed"] is True
    assert frame["origin"] == {"kind": "human"}
    assert frame["message"]["content"] == "/clear @file"


@pytest.mark.parametrize(
    "decision,wire", [("approve", "accept"), ("reject", "decline"), ("cancel", "cancel")]
)
def test_codex_permissions_are_one_operation_only(decision, wire):
    codec = codex_running()
    event = codec.feed(
        codex_approval(
            proposedExecpolicyAmendment=["git"],
            proposedNetworkPolicyAmendments=[{"action": "allow", "host": "example.org"}],
        )
    )[0]
    response = codec.decide(event.data["request_id"], decision)
    assert response == {"id": 2, "result": {"decision": wire}}
    with pytest.raises(ProtocolError, match="no longer pending"):
        codec.decide(event.data["request_id"], decision)
    with pytest.raises(ProtocolError, match="already consumed"):
        codec.feed(codex_approval())


def test_rpc_integer_and_string_permission_ids_are_distinct():
    codec = codex_running()
    integer = codec.feed(codex_approval(2))[0]
    string = codec.feed(codex_approval("2"))[0]
    assert integer.data["request_id"] != string.data["request_id"]
    assert type(codec.decide(integer.data["request_id"], "reject")["id"]) is int
    assert type(codec.decide(string.data["request_id"], "reject")["id"]) is str
    with pytest.raises(ProtocolError):
        codec.feed(codex_approval(True))


@pytest.mark.parametrize(
    "mutate", ["foreign-thread", "foreign-turn", "unknown-effect", "lasting-filesystem"]
)
def test_unbound_or_unsupported_native_effects_are_refused(mutate):
    codec = codex_running()
    frame = codex_approval()
    if mutate == "foreign-thread":
        frame["params"]["threadId"] = "foreign"
    elif mutate == "foreign-turn":
        frame["params"]["turnId"] = "foreign"
    elif mutate == "unknown-effect":
        frame["method"] = "item/permissions/requestApproval"
    else:
        frame["method"] = "item/fileChange/requestApproval"
        frame["params"]["grantRoot"] = "/project"
    event = codec.feed(frame)[0]
    assert event.kind == "send"
    assert "error" in event.data["frame"]
    assert "result" not in event.data["frame"]


@pytest.mark.parametrize(
    "decision", ["acceptForSession", "bypassPermissions", "allow", "approve-always"]
)
def test_persistent_permission_decisions_are_never_serialized(decision):
    for codec, frame in [
        (codex_running(), codex_approval()),
        (claude_running(), claude_approval()),
    ]:
        request = codec.feed(frame)[0].data["request_id"]
        with pytest.raises(ProtocolError, match="unsupported permission decision"):
            codec.decide(request, decision)
        assert codec.decide(request, "reject")


def test_claude_approval_preserves_original_input_privately():
    codec = claude_running()
    request = claude_approval(
        permission_suggestions=[{"type": "setMode", "mode": "bypassPermissions"}]
    )
    event = codec.feed(request)[0]
    request["request"]["input"]["command"] = "mutated by caller"
    event.data["summary"] = "mutated by browser"
    response = codec.decide(event.data["request_id"], "approve")
    assert response["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "git status", "timeout": 1000},
    }
    assert "updatedPermissions" not in json.dumps(response)
    with pytest.raises(ProtocolError):
        codec.decide(event.data["request_id"], "approve")


def test_claude_cancel_denies_and_interrupts_only_the_pending_request():
    codec = claude_running()
    event = codec.feed(claude_approval())[0]
    response = codec.decide(event.data["request_id"], "cancel")["response"]
    assert response["request_id"] == "native-1"
    assert response["response"]["behavior"] == "deny"
    assert response["response"]["interrupt"] is True


def test_claude_unknown_hook_callback_does_not_create_authority():
    codec = claude_running()
    event = codec.feed(
        {
            "type": "control_request",
            "request_id": "hook-1",
            "request": {
                "subtype": "hook_callback",
                "callback_id": "arbitrary",
                "input": {"hook_event_name": "PreToolUse"},
            },
        }
    )[0]
    assert event.kind == "send"
    assert event.data["frame"]["response"]["subtype"] == "error"


def test_cancelled_permission_and_changed_identity_are_unusable():
    codec = claude_running()
    request = codec.feed(claude_approval())[0].data["request_id"]
    with pytest.raises(ProtocolError, match="identity changed"):
        codec.feed(claude_approval(input={"command": "different"}))
    event = codec.feed({"type": "control_cancel_request", "request_id": "native-1"})[0]
    assert event.kind == "approval_cancelled"
    with pytest.raises(ProtocolError):
        codec.decide(request, "approve")
    with pytest.raises(ProtocolError, match="already consumed"):
        codec.feed(claude_approval())


@pytest.mark.parametrize("native_state", ["completed", "failed", "interrupted"])
def test_codex_explicit_completion_clears_pending_permission(native_state):
    codec = codex_running()
    request = codec.feed(codex_approval())[0].data["request_id"]
    event = codec.feed(
        {
            "method": "turn/completed",
            "params": {
                "threadId": SESSION,
                "turn": {
                    "id": TURN,
                    "status": native_state,
                    "error": {"message": "native detail", "private": "not-public"},
                },
            },
        }
    )[0]
    assert event.data["state"] == native_state
    assert event.data["operation_id"] == OPERATION
    assert "not-public" not in json.dumps(event.data)
    with pytest.raises(ProtocolError):
        codec.decide(request, "approve")
    assert codec.operation_id is None


@pytest.mark.parametrize(
    "subtype,is_error,reason,state",
    [
        ("success", False, None, "completed"),
        ("success", True, None, "failed"),
        ("error_max_turns", False, None, "failed"),
        ("success", False, "aborted_streaming", "interrupted"),
        ("success", False, "aborted_tools", "interrupted"),
        ("success", False, "max_turns", "failed"),
    ],
)
def test_claude_result_semantics(subtype, is_error, reason, state):
    codec = claude_running()
    event = codec.feed(
        {
            "type": "result",
            "uuid": str(uuid.uuid4()),
            "session_id": SESSION,
            "subtype": subtype,
            "is_error": is_error,
            "terminal_reason": reason,
            "result": "Answer",
        }
    )[0]
    assert event.kind == "turn_completed"
    assert event.data["state"] == state
    assert event.data["operation_id"] == OPERATION


def test_claude_background_result_cannot_complete_submitted_turn():
    codec = claude_running()
    codec.feed(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "background-1",
            "session_id": SESSION,
        }
    )
    event = codec.feed(
        {
            "type": "result",
            "uuid": str(uuid.uuid4()),
            "subtype": "success",
            "is_error": False,
            "origin": {"kind": "task-notification"},
        }
    )[0]
    assert event.kind == "background"
    assert codec.operation_id == OPERATION
    result = codec.feed(
        {"type": "result", "uuid": str(uuid.uuid4()), "subtype": "success", "is_error": False}
    )[0]
    assert result.data["background_active"] is True
    event = codec.feed(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "background-1",
            "status": "completed",
        }
    )[0]
    assert event.data["active"] is False


def test_unknown_origin_is_background_and_missing_error_status_is_not_success():
    codec = claude_running()
    assert (
        codec.feed(
            {
                "type": "result",
                "uuid": str(uuid.uuid4()),
                "subtype": "success",
                "is_error": False,
                "origin": {"kind": "future-source"},
            }
        )[0].kind
        == "background"
    )
    with pytest.raises(ProtocolError, match="explicit error"):
        codec.feed({"type": "result", "uuid": str(uuid.uuid4()), "subtype": "success"})
    assert codec.operation_id == OPERATION


def test_claude_message_model_is_observed_and_subagent_model_is_not_parent():
    codec = claude_running()
    frame = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": "Answer"}],
        },
        "hidden": "not-public",
    }
    events = codec.feed(frame)
    assert [event.kind for event in events] == ["model", "text"]
    assert events[0].data["model_effective"] == "claude-opus-4-7"
    assert "not-public" not in json.dumps([event.data for event in events])
    frame["parent_tool_use_id"] = "parent-1"
    assert [event.kind for event in codec.feed(frame)] == ["background"]


def test_synthetic_claude_message_is_not_effective_model_evidence():
    codec = claude_running()
    events = codec.feed(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Error"}],
            },
        }
    )
    assert [event.kind for event in events] == ["text"]


def test_stream_deltas_and_tool_results_stay_bounded_and_structured():
    codec = codex_running()
    event = codec.feed(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": SESSION,
                "turnId": TURN,
                "itemId": "item-1",
                "delta": "a" * (MAX_TEXT + 10),
                "private": "not-public",
            },
        }
    )[0]
    assert len(event.data["text"]) == MAX_TEXT and event.data["truncated"] is True
    assert event.data["partial"] is True
    codec = claude_running()
    tool = codec.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "/project/file"},
                    }
                ]
            },
        }
    )[0]
    assert tool.kind == "tool" and tool.data["state"] == "running"
    result = codec.feed(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": "missing",
                    }
                ]
            },
        }
    )[0]
    assert result.data["state"] == "failed"
    delta = codec.feed(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )[0]
    assert delta.kind == "text" and delta.data["partial"]


def test_interrupt_is_a_request_and_never_fabricates_completion():
    codex = codex_running()
    request = codex.interrupt()
    assert request["params"] == {"threadId": SESSION, "turnId": TURN}
    event = codex.feed({"id": request["id"], "result": {}})[0]
    assert event.kind == "response" and codex.operation_id == OPERATION
    claude = claude_running()
    request = claude.interrupt()
    event = claude.feed(
        {
            "type": "control_response",
            "response": {"request_id": request["request_id"], "subtype": "success", "response": {}},
        }
    )[0]
    assert event.kind == "response" and claude.operation_id == OPERATION


@pytest.mark.parametrize("factory", [codex_running, claude_running])
def test_eof_is_uncertain_never_done_and_connection_cannot_retry(factory):
    codec = factory()
    event = codec.eof()
    assert event.kind == "disconnected"
    assert event.data["state"] == "uncertain"
    assert event.data["operation_id"] == OPERATION
    with pytest.raises(ProtocolError):
        codec.submit("Retry", OPERATION)
    with pytest.raises(ProtocolError):
        codec.feed({})


def test_native_request_errors_are_observed_without_resubmitting():
    codec = codex_session()
    request = codec.submit("Hi", OPERATION)
    event = codec.feed({"id": request["id"], "error": {"code": -32000, "message": "declined"}})[0]
    assert event.kind == "error"
    assert event.data["action"] == "turn/start"
    # The refused turn never ran, so the connection is free (review of #1278: it used to stay
    # "active" forever). The codec itself never resends; the journal claim is what keeps an
    # operation id to a single write, so the next turn needs a NEW operation id.
    assert codec.operation_id is None
    following = codec.submit("Next", str(uuid.uuid4()))
    assert following["method"] == "turn/start" and following["id"] != request["id"]


def test_foreign_claude_session_is_refused():
    with pytest.raises(ProtocolError, match="identity changed"):
        claude_running().feed(
            {
                "type": "result",
                "uuid": str(uuid.uuid4()),
                "session_id": str(uuid.uuid4()),
                "subtype": "success",
                "is_error": False,
            }
        )


def test_jsonl_framing_limits_and_invalid_numbers():
    frame = {"id": "request", "params": {"text": "a\nb"}}
    assert decode(encode(frame)) == frame
    for invalid in [
        b'{"id": 1}',
        b'{"id":\n',
        b"[]\n",
        b'{"x": NaN}\n',
        b'{"x": "\xff"}\n',
        b" " * (MAX_FRAME_BYTES + 1) + b"\n",
    ]:
        with pytest.raises(ProtocolError):
            decode(invalid)
    with pytest.raises(ProtocolError):
        encode({"text": "a" * MAX_FRAME_BYTES})
    nested = {}
    for _ in range(34):
        nested = {"x": nested}
    with pytest.raises(ProtocolError):
        encode(nested)


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_INPUT_TEXT + 1)])
def test_invalid_submission_does_not_occupy_turn(text):
    for codec in [codex_session(), ClaudeCodec(SESSION)]:
        with pytest.raises(ProtocolError):
            codec.submit(text, OPERATION)
        assert codec.operation_id is None


@pytest.mark.parametrize("identity", [True, None, "bad", SESSION.upper(), SESSION.replace("-", "")])
def test_native_uuid_inputs_are_canonical_and_errors_are_typed(identity):
    with pytest.raises(ProtocolError, match="canonical UUID"):
        ClaudeCodec(identity)
    with pytest.raises(ProtocolError, match="canonical UUID"):
        ClaudeCodec.argv("/native/agent", identity)
    for codec in [codex_session(), ClaudeCodec(SESSION)]:
        with pytest.raises(ProtocolError, match="canonical UUID"):
            codec.submit("Inspect", identity)
        assert codec.operation_id is None


def test_codex_resolved_callback_cannot_be_answered_later():
    codec = codex_running()
    request_id = codec.feed(codex_approval())[0].data["request_id"]
    event = codec.feed(
        {"method": "serverRequest/resolved", "params": {"threadId": SESSION, "requestId": 2}}
    )[0]
    assert event.kind == "approval_cancelled"
    with pytest.raises(ProtocolError):
        codec.decide(request_id, "approve")


def test_native_error_notification_is_not_turn_completion_or_client_retry():
    codec = codex_running()
    event = codec.feed(
        {
            "method": "error",
            "params": {
                "threadId": SESSION,
                "turnId": TURN,
                "willRetry": True,
                "error": {"message": "temporary native failure"},
            },
        }
    )[0]
    assert event.kind == "error"
    assert event.data["native_will_retry"] is True
    assert codec.operation_id == OPERATION


def test_codex_started_reply_exposes_request_correlation_once():
    codec = codex_session()
    request = codec.submit("Inspect", OPERATION)
    event = codec.feed(
        {"id": request["id"], "result": {"turn": {"id": TURN, "status": "inProgress"}}}
    )[0]
    assert event.data["request_id"] == request["id"]
    assert (
        codec.feed(
            {
                "method": "turn/started",
                "params": {"threadId": SESSION, "turn": {"id": TURN, "status": "inProgress"}},
            }
        )
        == []
    )


def test_early_event_buffer_is_bounded_before_turn_identity_is_known():
    codec = codex_session()
    codec.submit("Inspect", OPERATION)
    frame = {
        "method": "item/agentMessage/delta",
        "params": {"threadId": SESSION, "turnId": TURN, "itemId": "item-1", "delta": "x" * 100_000},
    }
    for _ in range(10):
        assert codec.feed(frame) == []
    with pytest.raises(ProtocolError, match="event buffer"):
        codec.feed(frame)


def test_claude_result_does_not_override_native_running_state():
    codec = claude_running()
    state = codec.feed(
        {
            "type": "system",
            "subtype": "session_state_changed",
            "state": "running",
            "sdk_host_only": True,
        }
    )[0]
    assert state.data["native_state"] == "running"
    result = codec.feed(
        {"type": "result", "uuid": str(uuid.uuid4()), "subtype": "success", "is_error": False}
    )[0]
    assert result.data["state"] == "completed"
    assert result.data["background_active"] is True
    assert result.data["native_state"] == "running"
    idle = codec.feed({"type": "system", "subtype": "session_state_changed", "state": "idle"})[0]
    assert idle.data["active"] is False


def test_claude_task_update_alone_can_settle_a_background_task():
    codec = claude_running()
    codec.feed({"type": "system", "subtype": "task_started", "task_id": "agent-task"})
    event = codec.feed(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "agent-task",
            "patch": {"status": "killed"},
        }
    )[0]
    assert event.kind == "background"
    assert event.data["active"] is False
    assert codec.operation_id == OPERATION


@pytest.mark.parametrize("origin", [[], "human", {}, {"kind": []}])
def test_malformed_claude_origin_cannot_masquerade_as_operator_turn(origin):
    codec = claude_running()
    with pytest.raises(ProtocolError, match="origin is malformed"):
        codec.feed(
            {
                "type": "result",
                "uuid": str(uuid.uuid4()),
                "subtype": "success",
                "is_error": False,
                "origin": origin,
            }
        )
    assert codec.operation_id == OPERATION


@pytest.mark.parametrize(
    "factory,frame", [(codex_running, codex_approval()), (claude_running, claude_approval())]
)
def test_approval_digest_binds_full_request_without_exposing_it(factory, frame):
    codec = factory()
    event = codec.feed(frame)[0]
    # Codex binds the frame AND what was presented (for a file change, its patch too).
    bound = (
        {"frame": frame, "presented": json.loads(event.data["summary"])}
        if factory is codex_running
        else frame
    )
    expected = hashlib.sha256(
        json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert event.data["payload_digest"] == expected
    assert "frame" not in event.data and "input" not in event.data and "params" not in event.data
    changed = json.loads(json.dumps(frame))
    changed["extra"] = "changed request metadata"
    with pytest.raises(ProtocolError, match="identity changed"):
        codec.feed(changed)


@pytest.mark.parametrize("model", ["x" * 257, "space model", {}, None])
def test_invalid_model_identity_is_unknown_not_truncated_evidence(model):
    codec = claude_running()
    events = codec.feed(
        {
            "type": "assistant",
            "message": {"model": model, "content": [{"type": "text", "text": "Answer"}]},
        }
    )
    assert [event.kind for event in events] == ["text"]
    configured = codec.feed(
        {"type": "system", "subtype": "init", "model": model, "session_id": SESSION}
    )[0]
    assert configured.data["model_configured"] is None


@pytest.mark.parametrize("text", ["x" * MAX_INPUT_TEXT, "😀" * MAX_INPUT_TEXT])
def test_chat_sized_inputs_are_supported_without_truncation(text):
    assert validate_text(text) == text
    for codec in [codex_session(), ClaudeCodec(SESSION)]:
        frame = codec.submit(text, OPERATION)
        encoded = encode(frame)
        assert len(encoded) < MAX_FRAME_BYTES
        restored = decode(encoded)
        actual = (
            restored["params"]["input"][0]["text"]
            if "params" in restored
            else restored["message"]["content"]
        )
        assert actual == text


@pytest.mark.parametrize("text", ["\0" * MAX_INPUT_TEXT, "unpaired\ud800"])
def test_input_frame_size_and_utf8_are_validated_before_turn_is_claimed(text):
    for codec in [codex_session(), ClaudeCodec(SESSION)]:
        with pytest.raises(ProtocolError):
            codec.submit(text, OPERATION)
        assert codec.operation_id is None


@pytest.mark.parametrize(
    "line",
    [
        b'{"type":"user","type":"result"}\n',
        b'{"request":{"allow":false,"allow":true}}\n',
        b'{\n"type":"result"\n}\n',
        b'{"type":"result"}\r\n',
        '{"type":"result"}\n'.encode("utf-16-be"),
        '{"type":"result"}\n'.encode("utf-32-be"),
        b'\xef\xbb\xbf{"type":"result"}\n',
    ],
)
def test_native_jsonl_has_unambiguous_utf8_fields_and_one_record(line):
    with pytest.raises(ProtocolError):
        decode(line)


def test_native_frame_size_includes_terminating_newline():
    frame = {"x": "a" * (MAX_FRAME_BYTES - 9)}
    encoded = encode(frame)
    assert len(encoded) == MAX_FRAME_BYTES
    assert decode(encoded) == frame
    with pytest.raises(ProtocolError):
        encode({"x": "a" * (MAX_FRAME_BYTES - 8)})


def test_claude_assistant_without_optional_message_id_reaches_ipc():
    from agent_sessions import native_ipc

    event = claude_running().feed(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Answer"}]}}
    )[0]
    assert "item_id" not in event.data
    assert native_ipc.normalize_event({"kind": event.kind, "data": event.data}) == {
        "kind": "text",
        "data": event.data,
    }


@pytest.mark.parametrize("identity", ["", "a" * 257, "space id", None, True])
def test_claude_assistant_identity_is_never_truncated_or_coerced(identity):
    with pytest.raises(ProtocolError, match="native identity"):
        claude_running().feed(
            {
                "type": "assistant",
                "message": {"id": identity, "content": [{"type": "text", "text": "Answer"}]},
            }
        )


def result_frame(**changes):
    return {
        "type": "result",
        "uuid": str(uuid.uuid4()),
        "session_id": SESSION,
        "subtype": "success",
        "is_error": False,
        **changes,
    }


def test_claude_repeated_result_cannot_complete_a_later_submission():
    codec = claude_running()
    first_result = result_frame()
    assert codec.feed(first_result)[0].data["operation_id"] == OPERATION
    second_operation = str(uuid.uuid4())
    codec.submit("Next turn", second_operation)
    assert codec.feed(first_result) == []
    assert codec.operation_id == second_operation
    with pytest.raises(ProtocolError, match="result identity changed"):
        codec.feed({**first_result, "result": "Changed"})
    assert codec.operation_id == second_operation
    assert codec.feed(result_frame())[0].data["operation_id"] == second_operation


def test_claude_idle_and_background_results_remain_replay_protected():
    codec = ClaudeCodec(SESSION)
    idle_result = result_frame()
    assert codec.feed(idle_result) == []
    codec.submit("New turn", OPERATION)
    assert codec.feed(idle_result) == []
    assert codec.operation_id == OPERATION
    background = result_frame(origin={"kind": "task-notification"})
    assert codec.feed(background)[0].kind == "background"
    assert codec.feed(background) == []
    assert codec.operation_id == OPERATION


@pytest.mark.parametrize("identity", [None, "", "bad", True, SESSION.upper()])
def test_claude_result_requires_unchanged_canonical_identity(identity):
    codec = claude_running()
    frame = result_frame(uuid=identity)
    if identity is None:
        frame.pop("uuid")
    with pytest.raises(ProtocolError, match="canonical UUID"):
        codec.feed(frame)
    assert codec.operation_id == OPERATION


def test_claude_result_ledger_never_evicts_a_completed_identity(monkeypatch):
    from agent_sessions import native_protocol

    monkeypatch.setattr(native_protocol, "MAX_RESULTS", 2)
    codec = claude_running()
    first = result_frame()
    codec.feed(first)
    codec.submit("Next turn", str(uuid.uuid4()))
    codec.feed(result_frame())
    assert codec.feed(first) == []
    with pytest.raises(ProtocolError, match="capacity"):
        codec.submit("Requires a fresh native connection", str(uuid.uuid4()))
    with pytest.raises(ProtocolError, match="capacity"):
        codec.feed(result_frame())
    assert codec.operation_id is None


@pytest.mark.parametrize(
    "factory,frame",
    [
        (codex_running, codex_approval(command="x" * MAX_TEXT + "; consequential suffix")),
        (codex_running, codex_approval(additionalPermissions={"hidden": "x" * MAX_TEXT})),
        (claude_running, claude_approval(input={"command": "x" * MAX_TEXT + "; suffix"})),
        (claude_running, claude_approval(input={"command": "😀" * (MAX_TEXT // 2)})),
    ],
)
def test_approval_cannot_hide_consequential_payload_beyond_presentation_bound(factory, frame):
    codec = factory()
    with pytest.raises(ProtocolError, match="presentation bound"):
        codec.feed(frame)
    assert codec._approvals == {}
    assert codec.operation_id == OPERATION


def _file_change(request_id=5):
    return {
        "id": request_id,
        "method": "item/fileChange/requestApproval",
        "params": {"threadId": SESSION, "turnId": TURN, "itemId": "edit-1", "reason": "edit"},
    }


def _patch(diff, turn=TURN):
    change = {"path": "a.py", "kind": {"type": "update"}, "diff": diff}
    return {
        "method": "item/fileChange/patchUpdated",
        "params": {"threadId": SESSION, "turnId": turn, "itemId": "edit-1", "changes": [change]},
    }


def test_a_file_change_presents_its_patch_but_is_never_approvable():
    """Nothing binds the patch Codex applies to the one shown (Hermes on #1278)."""
    codec = codex_running()
    assert codec.feed(_patch("-a\n+b\n")) == []
    event = codec.feed(_file_change())[0]
    assert event.kind == "approval" and event.data["complete"] is False
    presented = json.loads(event.data["summary"])
    assert presented["changes"][0]["diff"] == "-a\n+b\n" and presented["reason"] == "edit"
    reply = codec.decide(event.data["request_id"], "reject")
    assert reply == {"id": 5, "result": {"decision": "decline"}}


def test_a_command_approval_is_complete():
    event = codex_running().feed(codex_approval())[0]
    assert event.data["complete"] is True


def test_a_file_change_without_a_patch_or_from_another_turn_is_incomplete():
    codec = codex_running()
    assert codec.feed(_patch("-a\n+b\n", turn="another-turn")) == []  # not this turn's patch
    event = codec.feed(_file_change())[0]
    assert event.data["complete"] is False
    assert "changes" not in json.loads(event.data["summary"])


def test_a_patch_changing_after_presentation_declines_the_stale_approval():
    codec = codex_running()
    codec.feed(_patch("-a\n+b\n"))
    key = codec.feed(_file_change())[0].data["request_id"]
    events = codec.feed(_patch("-a\n+something else\n"))
    sends = [e.data["frame"] for e in events if e.kind == "send"]
    assert sends == [{"id": 5, "result": {"decision": "decline"}}]
    assert [e.kind for e in events if e.kind != "send"] == ["approval_cancelled"]
    with pytest.raises(ProtocolError):
        codec.decide(key, "approve")  # the presented callback is gone


def test_a_claude_prompt_presents_its_whole_permission_context():
    codec = claude_running()
    frame = claude_approval()
    frame["request"].update(blocked_path="/etc/hosts", decision_reason="outside cwd", title="t")
    event = codec.feed(frame)[0]
    presented = json.loads(event.data["summary"])
    assert (
        presented["blocked_path"] == "/etc/hosts" and presented["decision_reason"] == "outside cwd"
    )
    assert "subtype" not in presented and event.data["complete"] is True


def test_a_claude_sub_agent_prompt_is_refused():
    codec = claude_running()
    frame = claude_approval()
    frame["request"]["agent_id"] = "agent-1"
    (event,) = codec.feed(frame)
    assert event.kind == "send" and event.data["frame"]["response"]["subtype"] == "error"


def test_an_empty_initial_patch_is_not_a_presented_patch():
    """Hermes on #1278: `changes: []` from item/started counted as presented."""
    codec = codex_running()
    started = {
        "method": "item/started",
        "params": {
            "threadId": SESSION,
            "turnId": TURN,
            "item": {"id": "edit-1", "type": "fileChange", "status": "inProgress", "changes": []},
        },
    }
    codec.feed(started)
    event = codec.feed(_file_change())[0]
    assert event.data["complete"] is False
    codec2 = codex_running()
    codec2.feed(_patch(""))  # a change with an empty diff presents nothing either
    assert codec2.feed(_file_change())[0].data["complete"] is False


def test_an_identical_repeated_patch_keeps_the_pending_request():
    """Hermes on #1310: only a CHANGED proposal may invalidate a presented request."""
    codec = codex_running()
    codec.feed(_patch("-a\n+b\n"))
    key = codec.feed(_file_change())[0].data["request_id"]
    assert codec.feed(_patch("-a\n+b\n")) == []
    assert codec.decide(key, "reject") == {"id": 5, "result": {"decision": "decline"}}
