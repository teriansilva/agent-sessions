"""#1565 Phase 2: GitHub Copilot CLI as a native API client over ACP (`copilot --acp`).

Codec units first, then the real worker against the scripted `fake_native.py` copilot (the same
FakeHost as `test_native_runtime`).

The gates pinned here: session ids are plain v4 UUIDs; the session is pinned to the protocol's
agent-mode URI with `allow_all` echo-confirmed as the bypass switch; a model set is accepted
without an echo (1.0.68 enumerates no `model` option) while `reasoning_effort` is confirmed when
offered; approvals are one-shot and payload-bound with `allow_always` never offered or
selectable and `edit`/`delete`/`move` decline-only; a replayed history never yields a live
approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

import test_native_runtime as rt
from agent_sessions import native_protocol, native_runtime
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.native_protocol import CopilotAcpCodec, ProtocolError

# The scripted-agent harness and its fixtures (FakeHost, the fake binaries, the test roster).
host, project = rt.host, rt.project
FakeHost, frames, ident, pending, settle = rt.FakeHost, rt.frames, rt.ident, rt.pending, rt.settle

ENGINE = "copilot-api"
SES = "12345678-1234-4234-8234-1234567890ab"
MODE = native_protocol.COPILOT_MODE
AUTOPILOT = "https://agentclientprotocol.com/protocol/session-modes#autopilot"

ONCE = {"optionId": "once", "kind": "allow_once", "name": "Allow once"}
ALWAYS = {"optionId": "always", "kind": "allow_always", "name": "Always allow"}
REJECT = {"optionId": "reject", "kind": "reject_once", "name": "Reject"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- codec ----------------------------------------------------------------------------------


def _codec(*, pinned=True) -> CopilotAcpCodec:
    codec = CopilotAcpCodec()
    init = codec.initialize()
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": init["id"],
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "mcpCapabilities": {"http": True, "sse": True},
                    "promptCapabilities": {"image": True},
                },
                "agentInfo": {"name": "Copilot", "version": "1.0.68"},
                "authMethods": [{"id": "copilot-login"}],
            },
        }
    )
    assert event.kind == "initialized"
    new = codec.create("/w")
    assert new["params"] == {"cwd": "/w", "mcpServers": []}
    config = [{"id": "mode", "currentValue": MODE}, {"id": "allow_all", "currentValue": "off"}]
    [event] = codec.feed(
        {"jsonrpc": "2.0", "id": new["id"], "result": {"sessionId": SES, "configOptions": config}}
    )
    assert event.kind == "session" and event.data["native_id"] == SES
    assert event.data["model_configured"] is None and event.data["effort_configured"] is None
    if pinned:
        pin = codec.pin_mode()
        assert pin["params"] == {"sessionId": SES, "configId": "mode", "value": MODE}
        [event] = codec.feed(
            {"jsonrpc": "2.0", "id": pin["id"], "result": {"configOptions": config}}
        )
        assert event.kind == "session"
    return codec


def _ask(codec, rid=0, *, options=None, tool_call=None, session=SES) -> list:
    return codec.feed(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "session/request_permission",
            "params": {
                "sessionId": session,
                "toolCall": tool_call
                or {
                    "toolCallId": "call-1",
                    "title": "ls",
                    "kind": "execute",
                    "status": "pending",
                    "rawInput": {"command": "ls"},
                },
                "options": options if options is not None else [ALWAYS, ONCE, REJECT],
            },
        }
    )


def test_argv_is_literal_with_auto_update_off_and_the_mcp_pair():
    assert CopilotAcpCodec.argv("/opt/copilot") == ["/opt/copilot", "--acp", "--no-auto-update"]
    assert CopilotAcpCodec.argv("/opt/copilot", '{"mcpServers": {}}') == [
        "/opt/copilot",
        "--acp",
        "--no-auto-update",
        "--additional-mcp-config",
        '{"mcpServers": {}}',
    ]
    with pytest.raises(ProtocolError):
        CopilotAcpCodec.argv("relative/copilot")


def test_session_ids_are_plain_uuids():
    assert CopilotAcpCodec(SES).native_id == SES
    for bad in ("ses_AbC123", "12345678-1234-4234-8234-1234567890AB", f"{SES}x", 7):
        with pytest.raises(ProtocolError):
            CopilotAcpCodec(bad)
    codec = _codec(pinned=False)
    with pytest.raises(ProtocolError):
        codec.load("ses_not_a_uuid", "/w")
    with pytest.raises(ProtocolError):
        codec.load(str(uuid.uuid4()), "/w")  # a load names the session it was constructed with
    with pytest.raises(ProtocolError):
        codec.create("/w")  # already bound
    with pytest.raises(ProtocolError):
        # stdio MCP servers are refused, never silently dropped (#1565: they ride argv instead)
        CopilotAcpCodec().create("/w", [{"name": "m", "command": "x", "args": [], "env": []}])


def test_initialize_refuses_an_old_cli_or_another_protocol():
    for result in (
        {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "agentInfo": {}},
        {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"name": "Copilot", "version": "1.0.67"},
        },
        {
            "protocolVersion": 2,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"name": "Copilot", "version": "1.0.68"},
        },
        {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": {"version": "9.0.0"}},
        {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": False},
            "agentInfo": {"name": "Copilot", "version": "1.0.70"},
        },
    ):
        codec = CopilotAcpCodec()
        init = codec.initialize()
        [event] = codec.feed({"jsonrpc": "2.0", "id": init["id"], "result": result})
        assert event.kind == "error", result
    codec = CopilotAcpCodec()
    init = codec.initialize()
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": init["id"],
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "agentInfo": {"name": "Copilot", "version": "1.0.70"},
            },
        }
    )
    assert event.kind == "initialized" and codec.version == (1, 0, 70)


def test_no_turn_before_the_pin_and_a_mode_escape_after_it_is_a_violation():
    codec = _codec(pinned=False)
    with pytest.raises(ProtocolError, match="pinned mode"):
        codec.submit("hi", str(uuid.uuid4()))
    pin = codec.pin_mode()
    # The agent did not switch: the pin is refused, never assumed.
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": pin["id"],
            "result": {"configOptions": [{"id": "mode", "currentValue": AUTOPILOT}]},
        }
    )
    assert event.kind == "error" and "mode" in event.data["message"]
    codec = _codec()
    codec.submit("hi", str(uuid.uuid4()))
    for update in (
        {"sessionUpdate": "current_mode_update", "currentModeId": AUTOPILOT},
        {
            "sessionUpdate": "config_option_update",
            "configOptions": [{"id": "mode", "currentValue": AUTOPILOT}],
        },
    ):
        with pytest.raises(ProtocolError, match="pinned mode"):
            codec.feed(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": SES, "update": update},
                }
            )
    # Staying in the pinned mode is fine.
    assert (
        codec.feed(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": SES,
                    "update": {"sessionUpdate": "current_mode_update", "currentModeId": MODE},
                },
            }
        )
        == []
    )


def test_allow_all_is_the_echo_confirmed_bypass_switch():
    codec = _codec()
    gate = codec.set_allow_all(True)
    assert gate["params"] == {"sessionId": SES, "configId": "allow_all", "value": "on"}
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": gate["id"],
            "result": {
                "configOptions": [
                    {"id": "mode", "currentValue": MODE},
                    {"id": "allow_all", "currentValue": "off"},
                ]
            },
        }
    )
    assert event.kind == "error" and "allow_all" in event.data["message"]
    codec = _codec()
    gate = codec.set_allow_all(True)
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": gate["id"],
            "result": {
                "configOptions": [
                    {"id": "mode", "currentValue": MODE},
                    {"id": "allow_all", "currentValue": "on"},
                ]
            },
        }
    )
    assert event.kind == "session"


def test_a_model_set_is_accepted_without_an_echo_but_a_wrong_echo_is_an_error():
    codec = _codec()
    ask = codec.set_model("gpt-5.5")
    assert ask["params"] == {"sessionId": SES, "configId": "model", "value": "gpt-5.5"}
    # 1.0.68: accepted, never echoed — no `model` option in the replied configOptions at all.
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {
                "configOptions": [
                    {"id": "mode", "currentValue": MODE},
                    {"id": "allow_all", "currentValue": "off"},
                ]
            },
        }
    )
    assert event.kind == "session" and event.data["model_configured"] is None
    codec = _codec()
    ask = codec.set_model("gpt-5.5")
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {"configOptions": [{"id": "model", "currentValue": "other"}]},
        }
    )
    assert event.kind == "error" and "model" in event.data["message"]
    # A build that does enumerate it and DID select it confirms.
    codec = _codec()
    ask = codec.set_model("gpt-5.5")
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {"configOptions": [{"id": "model", "currentValue": "gpt-5.5"}]},
        }
    )
    assert event.kind == "session" and event.data["model_configured"] == "gpt-5.5"
    # An error reply is an error, best-effort or not.
    codec = _codec()
    ask = codec.set_model("nope")
    [event] = codec.feed(
        {"jsonrpc": "2.0", "id": ask["id"], "error": {"code": -32602, "message": "no such model"}}
    )
    assert event.kind == "error"


def test_effort_is_confirmed_when_offered_and_tolerated_when_not():
    codec = _codec()
    ask = codec.set_effort("high")
    assert ask["params"]["configId"] == "reasoning_effort"
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {"configOptions": [{"id": "reasoning_effort", "currentValue": "high"}]},
        }
    )
    assert event.kind == "session" and event.data["effort_configured"] == "high"
    # A build without the option answers with no such option: tolerated, never an error.
    codec = _codec()
    ask = codec.set_effort("high")
    [event] = codec.feed({"jsonrpc": "2.0", "id": ask["id"], "result": {"configOptions": []}})
    assert event.kind == "session" and event.data["effort_configured"] is None
    # Offered but NOT selected is an error.
    codec = _codec()
    ask = codec.set_effort("high")
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {"configOptions": [{"id": "reasoning_effort", "currentValue": "low"}]},
        }
    )
    assert event.kind == "error" and "reasoning_effort" in event.data["message"]


def test_approval_offers_one_shot_choices_and_allow_always_is_never_among_them():
    codec = _codec()
    op = str(uuid.uuid4())
    prompt = codec.submit("go", op)
    assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == SES
    [approval] = _ask(codec)
    data = approval.data
    assert data["operation_id"] == data["native_turn_id"] == op
    assert data["choices"] == ["approve", "reject"] and data["complete"] is True
    presented = json.loads(data["summary"])
    assert presented["toolCall"]["rawInput"] == {"command": "ls"}
    # Filtered entirely: not offered, not selectable (#1565).
    assert {o["kind"] for o in presented["options"]} == {"allow_once", "reject_once"}
    assert "allow_always" not in data["summary"]
    with pytest.raises(ProtocolError):
        codec.decide(data["request_id"], "always", grant="whatever")
    reply = codec.decide(data["request_id"], "approve")
    assert reply == {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
    }
    with pytest.raises(ProtocolError):  # consumed: one decision per request
        codec.decide(data["request_id"], "approve")


@pytest.mark.parametrize(
    "tool_call",
    [
        # edit / delete / move are never approvable for copilot, however complete (#1565).
        {
            "toolCallId": "c",
            "kind": "edit",
            "title": "/w/a.md",
            "rawInput": {"path": "/w/a.md", "newText": "hi\n"},
        },
        {"toolCallId": "c", "kind": "delete", "rawInput": {"path": "/w/a.md"}},
        {"toolCallId": "c", "kind": "move", "rawInput": {"from": "/w/a", "to": "/w/b"}},
        {"toolCallId": "c", "kind": "switch_mode", "rawInput": {"mode": AUTOPILOT}},
        {"toolCallId": "c", "rawInput": {"command": "ls"}},  # no kind at all
        {"toolCallId": "c", "kind": "execute", "title": "bash"},  # no rawInput
        {"toolCallId": "c", "kind": "execute", "title": "bash", "rawInput": {}},
        {"toolCallId": "c", "kind": "execute", "rawInput": {"command": "   "}},
        {"toolCallId": "c", "kind": "execute", "rawInput": {"command": ["ls"]}},
        {"toolCallId": "c", "kind": "read", "rawInput": {"command": "cat /w/a"}},  # no path
        {"toolCallId": "c", "kind": "fetch", "rawInput": {}},  # no url
        {"toolCallId": "c", "kind": "other"},  # no arguments at all
    ],
)
def test_a_request_without_its_consequential_input_is_decline_only(tool_call):
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, tool_call=tool_call)
    assert approval.data["choices"] == ["reject"] and approval.data["complete"] is False
    assert json.loads(approval.data["summary"])["declineOnly"]
    with pytest.raises(ProtocolError):
        codec.decide(approval.data["request_id"], "approve")


@pytest.mark.parametrize(
    "tool_call",
    [
        {"toolCallId": "c", "kind": "execute", "rawInput": {"command": "ls -la"}},
        {"toolCallId": "c", "kind": "read", "rawInput": {"path": "/w/a.md"}},
        {"toolCallId": "c", "kind": "fetch", "rawInput": {"url": "https://example.com"}},
        {"toolCallId": "c", "kind": "other", "rawInput": {"tool": "memory_write", "n": 1}},
    ],
)
def test_a_request_presenting_its_action_is_approvable(tool_call):
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, tool_call=tool_call)
    assert approval.data["choices"] == ["approve", "reject"] and approval.data["complete"]
    assert "declineOnly" not in json.loads(approval.data["summary"])


def test_reject_selects_reject_once_and_cancel_cancels():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, 3)
    reply = codec.decide(approval.data["request_id"], "reject")
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "reject"}
    [approval] = _ask(codec, 4)
    reply = codec.decide(approval.data["request_id"], "cancel")
    assert reply["result"]["outcome"] == {"outcome": "cancelled"}


@pytest.mark.parametrize(
    "options",
    [
        [ALWAYS, REJECT],  # only allow_always: filtered, so no one-shot approval exists
        [ONCE, {**ONCE, "optionId": "once-2"}, REJECT],  # two allow_once: not answerable
        [{"optionId": "x", "kind": "allow_forever"}, REJECT],  # an unknown kind
    ],
)
def test_without_exactly_one_allow_once_a_request_can_only_be_declined(options):
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, options=options)
    assert approval.data["choices"] == ["reject"] and approval.data["complete"] is False
    with pytest.raises(ProtocolError):
        codec.decide(approval.data["request_id"], "approve")
    reply = codec.decide(approval.data["request_id"], "reject")
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "reject"}


def test_no_reject_once_declines_as_cancelled():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, options=[ONCE])
    reply = codec.decide(approval.data["request_id"], "reject")
    assert reply["result"]["outcome"] == {"outcome": "cancelled"}


def test_the_digest_binds_the_payload_and_a_changed_re_ask_is_refused():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, 5)
    other = _codec()
    other.submit("go", str(uuid.uuid4()))
    changed = {
        "toolCallId": "call-1",
        "title": "ls",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "ls -la /etc"},
    }
    [approval2] = _ask(other, 5, tool_call=changed)
    assert approval2.data["payload_digest"] != approval.data["payload_digest"]
    # The same native request id re-asked with a different payload is refused, not replaced.
    with pytest.raises(ProtocolError, match="identity changed"):
        _ask(codec, 5, tool_call=changed)


def test_an_oversized_request_is_outlined_and_decline_only():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    huge = {
        "toolCallId": "call-big",
        "title": "big",
        "kind": "other",
        "rawInput": {"blob": "x" * 30_000},
    }
    [approval] = _ask(codec, tool_call=huge)
    assert approval.data["choices"] == ["reject"] and approval.data["complete"] is False
    assert len(approval.data["summary"]) < native_protocol.MAX_TEXT
    assert "too large" in approval.data["summary"]


def test_an_uncorrelated_permission_request_is_refused_never_presented():
    codec = _codec()
    # No live turn (e.g. replayed during session/load): answered `cancelled`, nothing pending.
    [send] = _ask(codec, 9)
    assert send.kind == "send"
    assert send.data["frame"]["result"] == {"outcome": {"outcome": "cancelled"}}
    codec.submit("go", str(uuid.uuid4()))
    [send] = _ask(codec, 10, session=str(uuid.uuid4()))
    assert send.kind == "send" and not codec._approvals


def test_load_replay_is_never_attributed_to_a_turn():
    codec = CopilotAcpCodec(SES)
    load = codec.load(SES, "/w")
    assert load["params"] == {"sessionId": SES, "cwd": "/w", "mcpServers": []}
    replay = [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old"}},
        {"sessionUpdate": "tool_call", "toolCallId": "c", "status": "pending", "kind": "execute"},
        {"sessionUpdate": "current_mode_update", "currentModeId": AUTOPILOT},  # before the pin
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
    ]
    for update in replay:
        assert (
            codec.feed(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": SES, "update": update},
                }
            )
            == []
        )
    # A hostile replayed permission request is cancelled on the wire, never presented.
    [send] = _ask(codec, 11)
    assert send.kind == "send"
    assert send.data["frame"]["result"] == {"outcome": {"outcome": "cancelled"}}
    # The load reply is an EMPTY object on 1.0.68.
    [event] = codec.feed({"jsonrpc": "2.0", "id": load["id"], "result": {}})
    assert event.kind == "session" and event.data["native_id"] == SES
    assert event.data["model_configured"] is None and event.data["effort_configured"] is None


def test_unknown_client_methods_are_refused():
    codec = _codec()
    [send] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "fs/write_text_file",
            "params": {"sessionId": SES, "path": "/etc/passwd", "content": "x"},
        }
    )
    assert send.data["frame"]["error"]["code"] == -32601


def test_turn_events_use_the_shared_vocabulary():
    codec = _codec()
    op = str(uuid.uuid4())
    prompt = codec.submit("go", op)

    def update(**fields):
        return codec.feed(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": SES, "update": fields},
            }
        )

    [text] = update(
        sessionUpdate="agent_message_chunk",
        messageId="prt_1",
        content={"type": "text", "text": "hel"},
    )
    assert text.kind == "text" and text.data["partial"] and text.data["item_id"] == "prt_1"
    assert update(sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "x"}) == []
    [tool] = update(
        sessionUpdate="tool_call_update",
        toolCallId="call|weird id",
        status="completed",
        kind="execute",
        title="ls",
        rawInput={"command": "ls"},
        content=[{"type": "content", "content": {"type": "text", "text": "out"}}],
    )
    assert tool.kind == "tool" and tool.data["completed"] and tool.data["output"] == "out"
    assert tool.data["item_id"].startswith("tool-")  # a provider id outside the alphabet
    assert tool.data["summary"] == '{"command":"ls"}'  # the command, never the display title
    interrupt = codec.interrupt()
    assert interrupt == {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": SES},
    }
    [done] = codec.feed(
        {"jsonrpc": "2.0", "id": prompt["id"], "result": {"stopReason": "cancelled"}}
    )
    assert done.kind == "turn_completed" and done.data["state"] == "interrupted"
    prompt = codec.submit("again", str(uuid.uuid4()))
    [done] = codec.feed({"jsonrpc": "2.0", "id": prompt["id"], "result": {"stopReason": "refusal"}})
    assert done.data["state"] == "failed" and "refusal" in done.data["error"]
    prompt = codec.submit("once more", str(uuid.uuid4()))
    [done] = codec.feed(
        {"jsonrpc": "2.0", "id": prompt["id"], "error": {"code": -32603, "message": "boom"}}
    )
    assert done.data["state"] == "failed" and done.data["error"] == "boom"


def test_cancel_answers_every_pending_permission_cancelled():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, 2)
    events = codec.cancel_pending()
    assert [e.kind for e in events] == ["send", "approval_cancelled"]
    assert events[0].data["frame"] == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"outcome": {"outcome": "cancelled"}},
    }
    with pytest.raises(ProtocolError):
        codec.decide(approval.data["request_id"], "approve")


def test_submit_refuses_images():
    codec = _codec()
    with pytest.raises(ProtocolError, match="no images"):
        codec.submit("look", str(uuid.uuid4()), images=[("image/png", "aGk=")])


# --- the worker -----------------------------------------------------------------------------


def _control(name: str, value: str) -> None:
    path = Path(os.environ["HOME"]) / ".fake-copilot" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(value)


async def _create(work: Path, **kwargs) -> str:
    return (await runtime.create_session(ENGINE, str(work), operation_id=ident(), **kwargs))[
        "session_key"
    ]


@pytest.mark.anyio
async def test_create_binds_the_agent_minted_uuid_and_streams_text(host, project):
    key = await _create(project)
    snap = await runtime.snapshot(key)
    native = snap["native"]["native_id"]
    assert str(uuid.UUID(native)) == native  # copilot mints plain v4 UUIDs
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "echo:hello"
    sent = [f["frame"] for f in frames(project) if f["frame"].get("method")]
    methods = [f["method"] for f in sent]
    assert methods == [
        "initialize",
        "session/new",
        "session/set_config_option",  # the mode pin
        "session/set_config_option",  # allow_all off
        "session/prompt",
    ]
    [new] = [f for f in sent if f["method"] == "session/new"]
    assert new["params"]["mcpServers"] == []  # stdio MCP servers are refused on ACP (#1565)
    pin, gate = [f["params"] for f in sent if f["method"] == "session/set_config_option"]
    assert pin == {"sessionId": native, "configId": "mode", "value": MODE}
    assert gate == {"sessionId": native, "configId": "allow_all", "value": "off"}
    # Model/effort selection is deferred (#1565 review): neither is ever set — the CLI
    # enumerates and echoes no model — and the create path refuses a non-default model while
    # discovery reports the list unavailable.
    assert not [
        f
        for f in sent
        if f["method"] == "session/set_config_option"
        and f["params"]["configId"] in ("model", "reasoning_effort")
    ]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(ENGINE, str(project), operation_id=ident(), model="gpt-5.5")
    assert refused.value.status == 422
    # Every generation's child got auto-update pinned off in its environment.
    envs = [f["env"] for f in frames(project) if "env" in f]
    assert envs and all(e["auto_update"] == "false" for e in envs)
    argvs = {tuple(f["argv"]) for f in frames(project)}
    assert argvs == {("--acp", "--no-auto-update")}


@pytest.mark.anyio
async def test_an_enumerated_model_selection_is_refused_at_the_worker(host, project, monkeypatch):
    """Create-to-worker with the ENUMERATING fake (#1565 review): a build that offers a `model`
    select lets the choice pass validation, but the worker can never confirm the set (copilot
    echoes nothing), so it refuses the spawn instead of running the vendor default under the
    chosen model's name."""
    from agent_sessions import native_models

    monkeypatch.setattr(native_models, "_CACHE", {})  # probe afresh, with the models switch on
    _control("models", "1")
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(
            ENGINE, str(project), operation_id=ident(), model="fake-copilot-a"
        )
    # 503: the worker exited before ready — its WorkerError, never a silent default.
    assert refused.value.status == 503
    # And nothing was ever written to the agent: the refusal precedes `initialize`.
    sent = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method") in ("session/new", "session/prompt")
    ]
    assert sent == []


@pytest.mark.anyio
async def test_a_permission_approve_reaches_the_agent_exactly_once(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    snap = await pending(key)
    [request] = snap["pending_requests"]
    assert request["choices"] == ["approve", "reject"] and request["complete"] is True
    assert request["always"] == []  # copilot's allow_always is never a standing grant (#1565)
    assert request["payload"]["toolCall"]["rawInput"] == {"command": "ls -la"}
    assert {o["kind"] for o in request["payload"]["options"]} == {"allow_once", "reject_once"}
    out = await runtime.decide(
        key,
        decision_id=ident(),
        request_id=request["request_id"],
        turn_id=turn,
        decision="approve",
        user="marcus",
    )
    assert out["handoff"] == "sent"
    _, settled = await settle(key, turn)
    assert settled["reply"] == "approved:once"
    answered = [f["frame"] for f in frames(project) if "outcome" in json.dumps(f["frame"])]
    assert answered == [
        {
            "jsonrpc": "2.0",
            "id": answered[0]["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
        }
    ]


@pytest.mark.anyio
async def test_an_edit_is_decline_only_end_to_end(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="EDIT")
    [request] = (await pending(key))["pending_requests"]
    assert request["choices"] == ["reject"] and request["complete"] is False
    assert request["payload"]["declineOnly"] and request["payload"]["toolCall"]["kind"] == "edit"
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.decide(
            key,
            decision_id=ident(),
            request_id=request["request_id"],
            turn_id=turn,
            decision="approve",
            user="marcus",
        )
    assert refused.value.status == 409
    await runtime.decide(
        key,
        decision_id=ident(),
        request_id=request["request_id"],
        turn_id=turn,
        decision="reject",
        user="marcus",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "approved:reject"


@pytest.mark.anyio
async def test_an_always_only_request_can_only_be_declined(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="ONLY_ALWAYS")
    [request] = (await pending(key))["pending_requests"]
    assert request["choices"] == ["reject"] and request["complete"] is False
    await runtime.decide(
        key,
        decision_id=ident(),
        request_id=request["request_id"],
        turn_id=turn,
        decision="reject",
        user="marcus",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "approved:reject"


@pytest.mark.anyio
async def test_a_loaded_history_never_presents_an_old_request_as_live(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="DIE")
    await settle(key, turn, state=("uncertain",))
    native = (await runtime.snapshot(key))["native"]["native_id"]
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="HANG")
    await asyncio.sleep(0.5)
    snap = await runtime.snapshot(key)
    assert snap["pending_requests"] == []
    assert snap["native"]["native_id"] == native
    sent = [f["frame"] for f in frames(project) if f["frame"].get("method")]
    assert [f["method"] for f in sent].count("session/load") == 1
    # The replayed permission request was refused on the wire, never presented.
    answered = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("result", {}).get("outcome") == {"outcome": "cancelled"}
    ]
    assert len(answered) == 1
    events = (await runtime.events(key, after=0))["events"]
    assert not [e for e in events if e["kind"] == "approval"]
    assert not [e for e in events if e["kind"] == "tool" and e["data"]["item_id"] == "call-old"]
    # The resumed generation re-pins the mode and re-states allow_all before any turn.
    pins = [
        f["params"]
        for f in sent
        if f["method"] == "session/set_config_option" and f["params"]["configId"] == "mode"
    ]
    assert len(pins) == 2 and all(p["value"] == MODE for p in pins)


@pytest.mark.anyio
async def test_leaving_the_pinned_mode_ends_the_generation(host, project):
    key = await _create(project)
    turn = ident()
    try:
        await runtime.submit_turn(key, operation_id=turn, text="MODE_ESCAPE")
    except runtime.StructuredError as exc:
        # The worker may end the generation before its receipt reaches us (a loaded host): an
        # unknown outcome (503), never a success the turn did not have.
        assert exc.status == 503
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["state"] == "uncertain"


@pytest.mark.anyio
async def test_interrupt_cancels_the_turn_and_its_pending_permission(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="HANG_APPROVE")
    [request] = (await pending(key))["pending_requests"]
    await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
    snap, settled = await settle(key, turn, state=("interrupted",))
    assert snap["pending_requests"] == []
    sent = [f["frame"] for f in frames(project)]
    cancel = next(i for i, f in enumerate(sent) if f.get("method") == "session/cancel")
    assert sent[cancel + 1]["result"] == {"outcome": {"outcome": "cancelled"}}
    with pytest.raises(runtime.StructuredError):
        await runtime.decide(
            key,
            decision_id=ident(),
            request_id=request["request_id"],
            turn_id=turn,
            decision="approve",
            user="marcus",
        )


@pytest.mark.anyio
async def test_bypass_creation_sets_allow_all_on(host, project):
    key = await _create(project, bypass=True)
    snap = await runtime.snapshot(key)
    assert snap["bypass"] and snap["pending_start"]  # consent given; the launch waits (#1339)
    started = await runtime.start_session(key)
    assert started["pending_start"] is False
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "echo:hello"
    gates = [
        f["frame"]["params"]
        for f in frames(project)
        if f["frame"].get("method") == "session/set_config_option"
        and f["frame"]["params"]["configId"] == "allow_all"
    ]
    assert [g["value"] for g in gates] == ["on"]


def test_readiness_needs_the_floor(host, tmp_path):
    prov = registry._BY_ID[ENGINE]
    assert native_runtime.readiness(prov) == (True, None)
    _control("version", "1.0.67")
    native_runtime._VERSIONS.clear()
    ready, reason = native_runtime.readiness(prov)
    assert not ready and "1.0.68 or later" in reason
