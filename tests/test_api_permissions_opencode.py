"""#1339: OpenCode choices, original tool input and command → MCP approval progression."""

import asyncio
import json
import uuid

import pytest

import test_native_opencode as oc
from agent_sessions import native_grants, native_protocol, native_runtime
from agent_sessions import structured_runtime as runtime

host, project, anyio_backend = oc.host, oc.project, oc.anyio_backend


def update(codec, **fields):
    return codec.feed(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": oc.SES,
                "update": {"sessionUpdate": "tool_call_update", **fields},
            },
        }
    )


def mcp():
    return {
        "toolCallId": "call-mcp",
        "title": "forgejo_list_repo_labels",
        "kind": "other",
        "status": "pending",
        "rawInput": {},
    }


def test_empty_mcp_permission_uses_only_its_correlated_running_tool_input():
    codec = oc._codec()
    codec.submit("go", str(uuid.uuid4()))
    args = {"owner": "superstatus.io", "repo": "agent-sessions"}
    update(codec, **{**mcp(), "status": "in_progress", "rawInput": args})
    [approval] = oc._ask(codec, tool_call=mcp())
    payload = json.loads(approval.data["summary"])
    assert payload["toolCall"]["rawInput"] == {}  # original stays inspectable
    assert payload["toolContext"]["rawInput"] == args
    assert approval.data["complete"]
    assert (
        codec.decide(approval.data["request_id"], "approve")["result"]["outcome"]["optionId"]
        == "once"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"toolCallId": "some-other-call"},
        {"title": "another_tool"},
        {"kind": "execute"},
        {"status": "pending"},
        {"rawInput": {}},
        {"rawInput": {"huge": "x" * 21000}},
    ],
)
def test_a_different_or_incomplete_context_never_fills_permission_input(change):
    codec = oc._codec()
    codec.submit("go", str(uuid.uuid4()))
    update(codec, **{**mcp(), "status": "in_progress", "rawInput": {"repo": "r"}, **change})
    [approval] = oc._ask(codec, tool_call=mcp())
    assert not approval.data["complete"]
    with pytest.raises(native_protocol.ProtocolError):
        codec.decide(approval.data["request_id"], "approve")


def test_a_changed_context_cancels_its_pending_approval():
    codec = oc._codec()
    codec.submit("go", str(uuid.uuid4()))
    update(codec, **{**mcp(), "status": "in_progress", "rawInput": {"repo": "a"}})
    [approval] = oc._ask(codec, tool_call=mcp())
    events = update(codec, toolCallId="call-mcp", rawInput={"repo": "b"})
    assert any(e.kind == "approval_cancelled" for e in events)
    assert next(e for e in events if e.kind == "send").data["frame"]["result"]["outcome"] == {
        "outcome": "cancelled"
    }
    with pytest.raises(native_protocol.ProtocolError):
        codec.decide(approval.data["request_id"], "approve")


def test_always_is_the_exact_native_option_and_discloses_unknown_patterns_and_worker_lifetime():
    codec = oc._codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = oc._ask(codec)
    [grant] = native_grants.derive(
        "opencode-acp", "execute", json.loads(approval.data["summary"]), "/w"
    )["always"]
    assert "patterns are not provided" in grant["label"]
    assert "not saved" in grant["label"] and "worker restarts" in grant["label"]
    with pytest.raises(native_protocol.ProtocolError):
        codec.decide(approval.data["request_id"], "always", grant="g" + "0" * 16)
    reply = codec.decide(approval.data["request_id"], "always", grant=grant["id"], cwd="/w")
    assert reply["result"]["outcome"]["optionId"] == "always"


def test_command_risk_uses_original_input_not_a_benign_title():
    codec = oc._codec()
    codec.submit("go", str(uuid.uuid4()))
    call = {
        "toolCallId": "c",
        "kind": "execute",
        "title": "harmless description",
        "rawInput": {"command": "rm -rf dist/"},
    }
    [tool] = update(codec, **call)
    [approval] = oc._ask(codec, tool_call=call)
    assert (
        native_runtime._tool_risk("execute", tool.data["summary"], "/w")
        == native_grants.derive(
            "opencode-acp", "execute", json.loads(approval.data["summary"]), "/w"
        )["risk"]
    )
    assert native_runtime._tool_risk("execute", tool.data["summary"], "/w")["level"] == "risky"


@pytest.mark.anyio
async def test_allow_advances_from_command_to_mcp_and_completes(host, project):
    key = await oc._create(project)
    turn = oc.ident()
    await runtime.submit_turn(key, operation_id=turn, text="COMMAND_THEN_MCP")
    [first] = (await oc.pending(key))["pending_requests"]
    await runtime.decide(
        key,
        decision_id=oc.ident(),
        request_id=first["request_id"],
        turn_id=turn,
        decision="approve",
        user="marcus",
    )
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if (
            snap["pending_requests"]
            and snap["pending_requests"][0]["request_id"] != first["request_id"]
        ):
            break
        await asyncio.sleep(0.01)
    [second] = snap["pending_requests"]
    assert second["request_id"] != first["request_id"] and second["complete"]
    assert second["payload"]["toolContext"]["rawInput"]["repo"] == "agent-sessions"
    await runtime.decide(
        key,
        decision_id=oc.ident(),
        request_id=second["request_id"],
        turn_id=turn,
        decision="approve",
        user="marcus",
    )
    snap, settled = await oc.settle(key, turn)
    assert settled["reply"] == "command:once mcp:once" and not snap["pending_requests"]


@pytest.mark.anyio
async def test_opencode_always_crosses_both_gates(host, project):
    key = await oc._create(project)
    turn = oc.ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    [req] = (await oc.pending(key))["pending_requests"]
    [grant] = req["always"]
    with pytest.raises(runtime.StructuredError):
        await runtime.decide(
            key,
            decision_id=oc.ident(),
            request_id=req["request_id"],
            turn_id=turn,
            decision="always",
            grant="g" + "0" * 16,
            user="marcus",
        )
    # Independently bypass the server's check: even authenticated IPC cannot select an
    # invented standing grant. The valid approval remains pending and answerable afterwards.
    session_id = key.partition(":")[2]
    events = (await runtime.events(key, after=0))["events"]
    approval = next(e["data"] for e in events if e["kind"] == "approval")
    gen = native_runtime._generation(session_id)
    journal = native_runtime._journal(native_runtime._api_provider(oc.ENGINE), session_id)
    with pytest.raises(native_runtime.NativeError) as refused:
        await native_runtime._call(
            gen,
            "decide",
            {
                "turn_id": turn,
                "request_id": approval["request_id"],
                "item_id": approval["item_id"],
                "payload_digest": approval["payload_digest"],
                "decision": "always",
                "grant": "g" + "0" * 16,
                "approval_worker_id": gen.worker_id,
                "approval_connection_id": approval["connection_id"],
                "actor": "marcus",
                "operation_id": oc.ident(),
                "expected_revision": journal.revision,
            },
        )
    assert refused.value.status == 409 and "not offered" in refused.value.detail
    assert not [f for f in oc.frames(project) if f["frame"].get("result", {}).get("outcome")]
    await runtime.decide(
        key,
        decision_id=oc.ident(),
        request_id=req["request_id"],
        turn_id=turn,
        decision="always",
        grant=grant["id"],
        user="marcus",
    )
    _, settled = await oc.settle(key, turn)
    assert settled["reply"] == "approved:always"


@pytest.mark.anyio
@pytest.mark.parametrize("bypass", [False, True])
async def test_opencode_create_and_resume_keep_mode(host, project, bypass):
    snap = await runtime.create_session(
        oc.ENGINE, str(project), operation_id=oc.ident(), bypass=bypass
    )
    key = snap["session_key"]
    if bypass:
        assert not host.launches
        await runtime.start_session(key)
    turn = oc.ident()
    await runtime.submit_turn(key, operation_id=turn, text="DIE")
    await oc.settle(key, turn, state=("uncertain",))
    turn = oc.ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await oc.settle(key, turn)
    assert len(host.launches) == 2
    configs = [
        json.loads(f["env"]["config"]) for f in oc.frames(project) if f.get("env", {}).get("config")
    ]
    assert configs
    for config in configs:
        rules = config["agent"][config["default_agent"]]["permission"]
        assert set(rules.values()) == {"allow" if bypass else "ask"}
    assert (await runtime.snapshot(key))["bypass"] is bypass
