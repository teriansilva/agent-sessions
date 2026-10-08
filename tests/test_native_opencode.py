"""#1312: opencode as a native API client over ACP (`opencode acp`).

Codec units first, then the real worker against the scripted `fake_native.py` opencode (the same
FakeHost as `test_native_runtime`), then an opt-in run against the installed CLI.

The security gates pinned here: the listener password reaches only the child; the session is
pinned to BattleLab's own ask-everything agent; approvals are exact one-shot choices (never
`allow_always`) bound to the presented payload; a replayed history never yields a live approval;
ownership fences console and API opencode on one `ses_*`; an API launch takes the same shared
admission as compaction.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

import test_manifest_api
import test_native_runtime as rt
from agent_sessions import (
    engines,
    native_ownership,
    native_protocol,
    native_runtime,
    native_state,
    native_worker,
    opencode_admission,
    sessionlock,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import opencode as opencode_engine
from agent_sessions.engines import registry
from agent_sessions.native_protocol import OpencodeAcpCodec, ProtocolError

# The scripted-agent harness and its fixtures (FakeHost, the fake binaries, the test roster).
host, project = rt.host, rt.project
FakeHost, frames, ident, pending, settle = rt.FakeHost, rt.frames, rt.ident, rt.pending, rt.settle

ENGINE = "opencode-api"
SES = "ses_AbC123def456"
AGENT = "battlelab-api-0123456789abcdef"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- codec ----------------------------------------------------------------------------------


def _codec(*, pinned=True) -> OpencodeAcpCodec:
    codec = OpencodeAcpCodec(agent=AGENT)
    init = codec.initialize()
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": init["id"],
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "agentInfo": {"name": "OpenCode", "version": "1.18.35"},
            },
        }
    )
    assert event.kind == "initialized"
    new = codec.create("/w")
    assert new["params"] == {"cwd": "/w", "mcpServers": []}
    config = [{"id": "mode", "currentValue": "plan"}, {"id": "model", "currentValue": "local/a"}]
    [event] = codec.feed(
        {"jsonrpc": "2.0", "id": new["id"], "result": {"sessionId": SES, "configOptions": config}}
    )
    assert event.kind == "session" and event.data["native_id"] == SES
    assert event.data["model_configured"] == "local/a" and event.data["model_effective"] is None
    if pinned:
        pin = codec.pin_mode()
        assert pin["params"] == {"sessionId": SES, "configId": "mode", "value": AGENT}
        config[0]["currentValue"] = AGENT
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
                "options": options
                if options is not None
                else [
                    {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
                    {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
                ],
            },
        }
    )


def test_argv_is_literal_and_opens_no_exposed_listener():
    assert OpencodeAcpCodec.argv("/opt/opencode") == [
        "/opt/opencode",
        "acp",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        "--mdns=false",
    ]
    with pytest.raises(ProtocolError):
        OpencodeAcpCodec.argv("relative/opencode")


def test_session_ids_are_opencode_ids_not_uuids():
    assert native_protocol.opencode_session_id(SES) == SES
    for bad in (str(uuid.uuid4()), "ses_", "ses_a/b", "ses_-x", "../ses_a", 7):
        with pytest.raises(ProtocolError):
            native_protocol.opencode_session_id(bad)
    with pytest.raises(ProtocolError):
        OpencodeAcpCodec(str(uuid.uuid4()), agent=AGENT)
    for agent in ("build", "battlelab-api", "battlelab-api-XYZ", "plan"):
        with pytest.raises(ProtocolError):  # only BattleLab's own minted agent can be pinned
            OpencodeAcpCodec(agent=agent)


def test_initialize_refuses_an_old_cli_or_another_protocol():
    for result in (
        {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "agentInfo": {}},
        {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"version": "1.18.34"},
        },
        {
            "protocolVersion": 2,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"version": "1.18.35"},
        },
        {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": {"version": "9.0.0"}},
    ):
        codec = OpencodeAcpCodec(agent=AGENT)
        init = codec.initialize()
        [event] = codec.feed({"jsonrpc": "2.0", "id": init["id"], "result": result})
        assert event.kind == "error", result


def test_no_turn_before_the_agent_pin_and_a_mode_change_after_it_is_a_violation():
    codec = _codec(pinned=False)
    with pytest.raises(ProtocolError, match="pinned mode"):
        codec.submit("hi", str(uuid.uuid4()))
    pin = codec.pin_mode()
    # The agent did not switch: the pin is refused, never assumed.
    [event] = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": pin["id"],
            "result": {"configOptions": [{"id": "mode", "currentValue": "plan"}]},
        }
    )
    assert event.kind == "error" and "mode" in event.data["message"]
    codec = _codec()
    codec.submit("hi", str(uuid.uuid4()))
    for update in (
        {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"},
        {"sessionUpdate": "current_mode_update", "currentModeId": "build"},
        {
            "sessionUpdate": "config_option_update",
            "configOptions": [{"id": "mode", "currentValue": "plan"}],
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
    # Staying in our own agent is fine.
    assert (
        codec.feed(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": SES,
                    "update": {"sessionUpdate": "current_mode_update", "currentModeId": AGENT},
                },
            }
        )
        == []
    )


def test_approval_offers_one_shot_choices_only_and_never_answers_always():
    codec = _codec()
    op = str(uuid.uuid4())
    prompt = codec.submit("go", op)
    assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == SES
    [approval] = _ask(codec)
    assert approval.kind == "approval"
    data = approval.data
    assert data["operation_id"] == data["native_turn_id"] == op
    assert data["choices"] == ["approve", "reject"] and data["complete"] is True
    presented = json.loads(data["summary"])
    assert presented["toolCall"]["rawInput"] == {"command": "ls"}
    assert {o["kind"] for o in presented["options"]} == {"allow_once", "reject_once"}
    assert "always" not in data["summary"]
    for refused in ("cancel", "always", "allow_always", "approve_always"):
        with pytest.raises(ProtocolError):
            codec.decide(data["request_id"], refused)
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
        {"toolCallId": "c", "kind": "execute", "title": "bash"},  # no rawInput
        {"toolCallId": "c", "kind": "execute", "title": "bash", "rawInput": {}},
        {"toolCallId": "c", "kind": "execute", "rawInput": {"command": "   "}},
        {"toolCallId": "c", "kind": "execute", "rawInput": {"command": ["ls"]}},
        {"toolCallId": "c", "kind": "edit", "rawInput": {"filepath": "/w/a.md"}},  # no diff
        {"toolCallId": "c", "kind": "edit", "content": [{"type": "diff", "path": "/w/a"}]},
        {"toolCallId": "c", "kind": "fetch", "rawInput": {}},
        {"toolCallId": "c", "kind": "other"},
        {"toolCallId": "c", "kind": "switch_mode", "rawInput": {"mode": "yolo"}},
        {"toolCallId": "c", "rawInput": {"command": "ls"}},  # no kind
    ],
)
def test_a_request_without_its_action_is_decline_only(tool_call):
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
        {
            "toolCallId": "c",
            "kind": "edit",
            "content": [{"type": "diff", "path": "/w/new.md", "oldText": "", "newText": "hi\n"}],
        },
        {"toolCallId": "c", "kind": "fetch", "rawInput": {"url": "https://example.com"}},
    ],
)
def test_a_request_presenting_its_action_is_approvable(tool_call):
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, tool_call=tool_call)
    assert approval.data["choices"] == ["approve", "reject"] and approval.data["complete"]
    assert "declineOnly" not in json.loads(approval.data["summary"])


def test_reject_selects_reject_once():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, 3)
    reply = codec.decide(approval.data["request_id"], "reject")
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "reject"}


@pytest.mark.parametrize(
    "options",
    [
        [{"optionId": "always", "kind": "allow_always"}, {"optionId": "r", "kind": "reject_once"}],
        [
            {"optionId": "a", "kind": "allow_once"},
            {"optionId": "b", "kind": "allow_once"},
            {"optionId": "r", "kind": "reject_once"},
        ],
        [{"optionId": "x", "kind": "allow_forever"}, {"optionId": "r", "kind": "reject_once"}],
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
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "r"}


def test_no_reject_once_declines_as_cancelled():
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, options=[{"optionId": "once", "kind": "allow_once"}])
    reply = codec.decide(approval.data["request_id"], "reject")
    assert reply["result"]["outcome"] == {"outcome": "cancelled"}


def test_an_edit_presents_its_whole_diff_and_the_digest_binds_it():
    edit = {
        "toolCallId": "call-e",
        "title": "/w/a.md",
        "kind": "edit",
        "status": "pending",
        "locations": [{"path": "/w/a.md"}],
        "rawInput": {"filepath": "/w/a.md", "diff": "-old\n+new\n"},
        "content": [{"type": "diff", "path": "/w/a.md", "oldText": "old\n", "newText": "new\n"}],
    }
    codec = _codec()
    codec.submit("go", str(uuid.uuid4()))
    [approval] = _ask(codec, 5, tool_call=edit)
    presented = json.loads(approval.data["summary"])
    assert presented["toolCall"] == edit and approval.data["complete"] is True
    other = _codec()
    other.submit("go", str(uuid.uuid4()))
    changed = {**edit, "rawInput": {"filepath": "/w/a.md", "diff": "-old\n+evil\n"}}
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
        "title": "/w/big",
        "kind": "edit",
        "rawInput": {"filepath": "/w/big", "diff": "+" + "x" * 30_000},
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
    [send] = _ask(codec, 10, session="ses_other")
    assert send.kind == "send" and not codec._approvals


def test_load_replay_is_never_attributed_to_a_turn():
    codec = OpencodeAcpCodec(SES, agent=AGENT)
    load = codec.load(SES, "/w")
    assert load["params"] == {"sessionId": SES, "cwd": "/w", "mcpServers": []}
    replay = [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old"}},
        {"sessionUpdate": "tool_call", "toolCallId": "c", "status": "pending", "kind": "execute"},
        {"sessionUpdate": "current_mode_update", "currentModeId": "plan"},  # before the pin
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
    [event] = codec.feed({"jsonrpc": "2.0", "id": load["id"], "result": {"configOptions": []}})
    assert event.kind == "session" and event.data["native_id"] == SES


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
        content=[{"type": "content", "content": {"type": "text", "text": "out"}}],
    )
    assert tool.kind == "tool" and tool.data["completed"] and tool.data["output"] == "out"
    assert tool.data["item_id"].startswith("tool-")  # a provider id outside the alphabet
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


# --- the worker -----------------------------------------------------------------------------


def _config(tmp_path, **extra) -> dict:
    return {
        "worker_id": ident(),
        "session_key": f"{ENGINE}:{ident()}",
        "adapter": "opencode-acp",
        "journal_root": str(tmp_path / "journal"),
        "capability": native_worker.native_ipc.Capability.create()._value,
        "connection_id": ident(),
        "source_engine": "opencode",
        "store_database": str(Path(engines.get("opencode").store_path("db")).resolve()),
        "binary": "/opt/opencode",
        "cwd": str(tmp_path),
        "mode": "create",
        "home": str(tmp_path),
        "path": "/usr/bin:/bin",
        **extra,
    }


def test_the_child_environment_carries_a_fresh_password_and_the_forced_ask(tmp_path):
    config = _config(tmp_path)
    a, b = native_worker.Worker(config, tmp_path), native_worker.Worker(config, tmp_path)
    assert a._server_password != b._server_password and len(a._server_password) >= 32
    assert a.agent != b.agent  # each generation mints its own agent
    env = native_worker.child_environment(config, a._server_password, a.agent)
    assert env["OPENCODE_SERVER_PASSWORD"] == a._server_password
    forced = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert forced["default_agent"] == a.agent and forced["permission"] == {"*": "ask"}
    assert list(forced["agent"]) == [a.agent]
    own = forced["agent"][a.agent]
    assert own["mode"] == "primary" and "prompt" not in own and "model" not in own
    permission = own["permission"]
    assert next(iter(permission)) == "*"  # the catch-all first: nothing after it allows
    assert set(permission.values()) == {"ask"}
    assert {"bash", "edit", "webfetch", "task", "external_directory", "read"} <= set(permission)
    assert set(env) == {
        "HOME",
        "PATH",
        "LANG",
        "TERM",
        "NO_COLOR",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_CONFIG_CONTENT",
    }
    with pytest.raises(native_worker.WorkerError):
        native_worker.child_environment(config, None, a.agent)  # never an open listener
    with pytest.raises(native_worker.WorkerError):
        native_worker.child_environment(config, a._server_password, "build")
    assert a._server_password not in json.dumps(config)
    other = native_worker.child_environment({**config, "adapter": "codex-app-server"})
    assert "OPENCODE_SERVER_PASSWORD" not in other


@pytest.mark.anyio
async def test_spawn_holds_shared_store_admission_through_process_creation(
    tmp_home, tmp_path, monkeypatch
):
    worker = native_worker.Worker(_config(tmp_path), tmp_path)
    monkeypatch.setattr(worker, "report", lambda **fields: None)
    seen = {}

    async def create(*argv, **kwargs):
        # Compaction's exclusive admission cannot be taken while the probe or child is created.
        exclusive = opencode_admission.acquire("opencode", exclusive=True)
        if argv[1:] == ("debug", "config"):
            seen["probe_exclusive"], seen["probe_env"] = exclusive, kwargs["env"]

            class Probe:
                returncode = 0

                class stdout:  # noqa: N801
                    @staticmethod
                    async def read(_n):
                        return b'{"mcp": {}}'

                @staticmethod
                async def wait():
                    return 0

            return Probe()
        seen["exclusive"] = exclusive
        seen["argv"], seen["env"] = argv, kwargs["env"]

        class Proc:
            pid = 4242
            returncode = 0

        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    await worker.spawn()
    assert seen["exclusive"] is None and seen["probe_exclusive"] is None
    assert seen["argv"] == tuple(OpencodeAcpCodec.argv("/opt/opencode"))
    # The probe gets a password of its own, never the child's.
    probe_password = seen["probe_env"]["OPENCODE_SERVER_PASSWORD"]
    assert len(probe_password) >= 32 and probe_password != worker._server_password
    assert seen["env"]["OPENCODE_SERVER_PASSWORD"] == worker._server_password
    # …and released once the child exists (it is then visible to the compactor's scan).
    guard = opencode_admission.acquire("opencode", exclusive=True)
    assert guard is not None
    # While maintenance holds it, the worker refuses to start a child at all.
    try:
        worker2 = native_worker.Worker(_config(tmp_path), tmp_path)
        with pytest.raises(native_worker.WorkerError, match="maintenance"):
            await worker2.spawn()
    finally:
        guard.release()


# --- end to end with the scripted agent -----------------------------------------------------


def _control(name: str, value: str) -> None:
    path = Path(os.environ["HOME"]) / ".fake-opencode" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(value)


async def _create(work: Path) -> str:
    return (await runtime.create_session(ENGINE, str(work), operation_id=ident()))["session_key"]


def _all_state_bytes(tmp_path: Path) -> bytes:
    out = b""
    for path in tmp_path.rglob("*"):
        if (
            path.is_file()
            # The test's own artifacts: the fake binary, its logs and the operator's config.
            and not {"engine-bin", ".fake-opencode"} & set(path.parts)
            and path.name not in {"native-frames.jsonl", "mcp-children.jsonl"}
        ):
            out += path.read_bytes()
    return out


@pytest.mark.anyio
async def test_listener_password_reaches_only_the_child(host, project, tmp_path):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    snap, settled = await settle(key, turn)
    assert settled["reply"] == "echo:hello"
    logged = frames(project)
    passwords = {f["env"]["password"] for f in logged if "env" in f}
    assert len(passwords) == 1
    password = passwords.pop()
    assert isinstance(password, str) and len(password) >= 32
    # The listener is pinned to loopback on the command line, whatever the operator's config says.
    assert {tuple(f["argv"]) for f in logged} == {
        ("acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false")
    }
    env_keys = set(logged[0]["env"]["keys"])
    assert {"OPENCODE_SERVER_PASSWORD", "OPENCODE_CONFIG_CONTENT"} <= env_keys
    assert not {k for k in env_keys if k.startswith("AGENT_SESSIONS_")}
    forced = json.loads(logged[0]["env"]["config"])
    assert set(forced["agent"][forced["default_agent"]]["permission"].values()) == {"ask"}
    assert forced["server"] == {"hostname": "127.0.0.1", "mdns": False}
    # Never on an argv (systemd's or the worker's), in the journal, state, lifecycle or events.
    assert password not in json.dumps(host.launches)
    worker = native_state.read_session(key.partition(":")[2])["current_worker"]
    pid = native_state.read_lifecycle(worker)["pid"]
    assert password.encode() not in Path(f"/proc/{pid}/cmdline").read_bytes()
    assert password.encode() not in _all_state_bytes(tmp_path)
    assert password not in json.dumps(await runtime.events(key, after=0))
    assert password not in json.dumps(snap)
    # A successor generation gets its own.
    await runtime.stop(key)
    await runtime.submit_turn(key, operation_id=ident(), text="again")
    after = {f["env"]["password"] for f in frames(project) if "env" in f}
    assert len(after) == 2


@pytest.mark.anyio
async def test_our_agent_is_pinned_after_new_and_after_load(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="DIE")
    await settle(key, turn, state=("uncertain",))
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="hi")
    await settle(key, nxt)
    sent = [f["frame"] for f in frames(project) if f["frame"].get("method")]
    methods = [f["method"] for f in sent]
    assert methods.count("session/new") == 1 and methods.count("session/load") == 1
    pins = [
        f["params"]
        for f in sent
        if f["method"] == "session/set_config_option" and f["params"]["configId"] == "mode"
    ]
    # Each generation pins ITS OWN freshly minted agent — the one its environment defines.
    agents = [
        json.loads(cfg)["default_agent"]
        for cfg in dict.fromkeys(f["env"]["config"] for f in frames(project) if "env" in f)
    ]
    assert [p["value"] for p in pins] == agents and len(set(agents)) == 2
    assert all(a.startswith("battlelab-api-") for a in agents)
    # Each pin precedes the first prompt of its generation.
    for start in ("session/new", "session/load"):
        i = methods.index(start)
        assert methods[i + 1] == "session/set_config_option"


@pytest.mark.anyio
async def test_a_mode_that_cannot_be_pinned_refuses_the_creation(host, project):
    _control("stuck", "1")
    with pytest.raises(runtime.StructuredError) as refused:
        await _create(project)
    assert refused.value.status == 503 and "mode" in refused.value.detail
    assert not [f for f in frames(project) if f["frame"].get("method") == "session/prompt"]


@pytest.mark.anyio
async def test_leaving_our_agent_mid_turn_ends_the_generation(host, project):
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
async def test_an_operator_allow_cannot_reach_our_agent(host, project):
    """The operator's opencode config allows webfetch and an MCP tool everywhere (config level,
    `build`, and an agent squatting the un-minted name): our minted agent still asks."""
    import fake_native

    # The catch-all FIRST, so our config-level `"*": "ask"` merges into its position and the
    # specific allows still come after it: only our agent's own block can make these ask.
    allow = {"*": "allow", "webfetch": "allow", "fakemcp_search": "allow"}
    operator = {
        "permission": allow,
        "agent": {
            "build": {"permission": allow},
            "battlelab-api": {"permission": allow},
            "plan": {"permission": allow},
        },
    }
    _control("operator.json", json.dumps(operator))
    # The fake's evaluator does allow these for the operator's own agents (a real negative).
    assert fake_native._permission(operator, "build", "webfetch") == "allow"
    key = await _create(project)
    for permission in ("webfetch", "fakemcp_search"):  # a known key and an MCP tool (wildcard)
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text=f"TOOL:{permission}")
        [request] = (await pending(key))["pending_requests"]
        await runtime.decide(
            key,
            decision_id=ident(),
            request_id=request["request_id"],
            turn_id=turn,
            decision="reject",
            user="marcus",
        )
        _, settled = await settle(key, turn)
        assert settled["reply"] == "asked:reject"


def _mcp_operator(marker: str) -> dict:
    """The operator's opencode config: two local MCP servers (one disabled stays disabled, one
    carries a secret in its environment) and a remote one."""
    return {
        "mcp": {
            "alpha": {
                "type": "local",
                "command": ["mcp-alpha", "--arg", marker],
                "environment": {"MARK": "alpha", "API_TOKEN": "operator-secret"},
            },
            "beta": {"type": "local", "command": ["mcp-beta"], "enabled": True},
            "gamma": {"type": "local", "command": ["mcp-gamma"], "enabled": False},
            "remote": {"type": "remote", "url": "https://mcp.example.invalid"},
        }
    }


def _mcp_children(project: Path) -> list[dict]:
    path = project / "mcp-children.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.anyio
async def test_local_mcp_servers_never_get_the_listener_password(host, project, tmp_path):
    marker = f"ARG-{uuid.uuid4().hex}"
    _control("operator.json", json.dumps(_mcp_operator(marker)))
    key = await _create(project)
    children = {c["name"]: c for c in _mcp_children(project)}
    assert set(children) == {"alpha", "beta"}  # gamma stays disabled, remote is not local
    assert not any(c["has_password"] for c in children.values())
    # The operator's own environment and command survive the merge (nothing copied over them).
    assert children["alpha"]["keys"] == ["API_TOKEN", "MARK", "OPENCODE_SERVER_PASSWORD"]
    assert children["alpha"]["command"] == ["mcp-alpha", "--arg", marker]
    forced = json.loads(next(f["env"]["config"] for f in frames(project) if "env" in f))
    assert set(forced["mcp"]) == {"alpha", "beta", "gamma"}
    assert forced["mcp"]["gamma"]["enabled"] is False
    assert "operator-secret" not in json.dumps(forced)  # environment values are never copied
    # The restated command lines live only in the child's environment.
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)
    assert marker.encode() not in _all_state_bytes(tmp_path)
    assert marker not in json.dumps(await runtime.events(key, after=0))
    assert marker not in json.dumps(host.launches)


@pytest.mark.anyio
@pytest.mark.parametrize("probe", ["fail", "garbage", "masked"])
async def test_a_failed_config_probe_refuses_the_launch(host, project, probe):
    _control("operator.json", json.dumps(_mcp_operator("x")))
    _control("probe", probe)
    with pytest.raises(runtime.StructuredError) as refused:
        await _create(project)
    assert refused.value.status == 503 and "configuration" in refused.value.detail
    assert frames(project) == [] and _mcp_children(project) == []  # opencode acp never ran


def test_mcp_overrides_restate_only_what_is_safe():
    overrides = opencode_engine.mcp_password_overrides(
        {
            "mcp": {
                "a": {"type": "local", "command": ["x"], "environment": {"K": "***"}},
                "r": {"type": "remote", "url": "https://h.invalid", "headers": {"A": "***"}},
            }
        }
    )
    assert overrides == {
        "a": {"type": "local", "command": ["x"], "environment": {"OPENCODE_SERVER_PASSWORD": ""}}
    }
    assert opencode_engine.mcp_password_overrides({}) == {}
    for bad in (
        [],
        {"mcp": []},
        {"mcp": {"a": {"type": "local"}}},
        {"mcp": {"a": {"type": "local", "command": []}}},
        {"mcp": {"a": {"type": "local", "command": ["x", "***"]}}},
        {"mcp": {"a": {"type": "local", "command": ["x"], "enabled": "yes"}}},
        {"mcp": {"a": {"type": "stdio", "command": ["x"]}}},
    ):
        with pytest.raises(ValueError):
            opencode_engine.mcp_password_overrides(bad)


@pytest.mark.anyio
async def test_approval_is_one_shot_and_bound_to_the_presented_request(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    snap = await pending(key)
    [request] = snap["pending_requests"]
    assert request["choices"] == ["approve", "reject"] and request["complete"] is True
    assert request["payload"]["toolCall"]["rawInput"] == {"command": "ls -la"}
    assert {o["kind"] for o in request["payload"]["options"]} == {"allow_once", "reject_once"}
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.decide(
            key,
            decision_id=ident(),
            request_id=request["request_id"],
            turn_id=turn,
            decision="cancel",
            user="marcus",
        )
    assert refused.value.status == 409
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
    answers = [f["frame"] for f in frames(project) if "result" in f["frame"]]
    assert all("always" not in json.dumps(a) for a in answers)


@pytest.mark.anyio
async def test_an_always_only_request_can_only_be_declined(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="ONLY_ALWAYS")
    [request] = (await pending(key))["pending_requests"]
    assert request["choices"] == ["reject"] and request["complete"] is False
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
@pytest.mark.parametrize("variant", ["MISSING", "EMPTY", "EMPTYCMD", "NODIFF"])
async def test_a_blind_request_cannot_be_approved_at_any_gate(host, project, variant):
    """Hermes on #1336: approve needs the action presented. Every gate refuses it: the snapshot
    offers decline only, the runtime refuses approve, and the WORKER refuses an approve sent to
    it directly over IPC (bypassing the runtime's choice check)."""
    key = await _create(project)
    session_id = key.partition(":")[2]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=f"BLIND_{variant}")
    [request] = (await pending(key))["pending_requests"]
    assert request["choices"] == ["reject"] and request["complete"] is False
    assert request["payload"]["declineOnly"]
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
    events = (await runtime.events(key, after=0))["events"]
    approval = next(e["data"] for e in events if e["kind"] == "approval")
    gen = native_runtime._generation(session_id)
    journal = native_runtime._journal(native_runtime._api_provider(ENGINE), session_id)
    with pytest.raises(native_runtime.NativeError) as worker_refused:
        await native_runtime._call(
            gen,
            "decide",
            {
                "turn_id": turn,
                "request_id": approval["request_id"],
                "item_id": approval["item_id"],
                "payload_digest": approval["payload_digest"],
                "decision": "approve",
                "approval_worker_id": gen.worker_id,
                "approval_connection_id": approval["connection_id"],
                "actor": "marcus",
                "operation_id": ident(),
                "expected_revision": journal.revision,
            },
        )
    assert worker_refused.value.status == 409
    assert "presented completely" in worker_refused.value.detail
    # Nothing was answered "once" on the wire; the request is still declinable.
    assert not [f for f in frames(project) if "once" in json.dumps(f["frame"].get("result"))]
    await runtime.decide(
        key,
        decision_id=ident(),
        request_id=request["request_id"],
        turn_id=turn,
        decision="reject",
        user="marcus",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "blind:reject"


@pytest.mark.anyio
async def test_an_edit_shows_its_diff_and_can_be_approved_once(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="EDIT")
    [request] = (await pending(key))["pending_requests"]
    tool = request["payload"]["toolCall"]
    assert tool["rawInput"]["diff"] == "-old\n+new\n"
    assert tool["content"][0]["oldText"] == "old\n" and tool["content"][0]["newText"] == "new\n"
    assert request["choices"] == ["approve", "reject"]
    await runtime.decide(
        key,
        decision_id=ident(),
        request_id=request["request_id"],
        turn_id=turn,
        decision="approve",
        user="marcus",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "approved:once"
    # opencode's follow-up fs/write_text_file is refused: BattleLab serves no files.
    refusals = [f["frame"] for f in frames(project) if "error" in f["frame"]]
    assert refusals and refusals[0]["error"]["code"] == -32601


@pytest.mark.anyio
async def test_a_loaded_history_never_presents_an_old_request_as_live(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="DIE")
    await settle(key, turn, state=("uncertain",))
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="HANG")
    await asyncio.sleep(0.5)
    snap = await runtime.snapshot(key)
    assert snap["pending_requests"] == []
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
async def test_a_failed_prompt_fails_its_turn(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="FAIL")
    _, settled = await settle(key, turn, state=("failed",))
    assert "boom" in settled["reason"]


# --- ownership and the shared store ---------------------------------------------------------


@pytest.mark.anyio
async def test_api_ownership_fences_the_console_on_the_same_session(host, project):
    key = await _create(project)
    native = (await runtime.snapshot(key))["native"]["native_id"]
    assert native.startswith("ses_")
    intent = native_ownership.lookup(key)
    assert intent.state == "bound" and intent.native_id == native
    console = engines.get("opencode")
    api_source = native_ownership.source_identity(console)
    assert api_source == intent.source
    assert api_source.canonical_path == str(Path(console.store_path("db")).resolve())
    with pytest.raises(native_ownership.OwnershipError, match="belongs to an API session"):
        native_ownership.check_console(console, native)
    native_ownership.check_console(console, "ses_SomeOtherOne")  # unrelated histories stay


@pytest.mark.anyio
async def test_a_console_writer_fences_the_api_on_the_same_session(host, project):
    key = await _create(project)
    native = (await runtime.snapshot(key))["native"]["native_id"]
    await runtime.stop(key)
    console = sessionlock.acquire(f"opencode:{native}")  # a live console opencode on it
    assert console is not None
    try:
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.submit_turn(key, operation_id=ident(), text="hello")
        assert "another BattleLab writer" in refused.value.detail
        assert not [f for f in frames(project) if f["frame"].get("method") == "session/load"]
    finally:
        console.release()
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)


@pytest.mark.anyio
async def test_an_unresolved_console_creation_refuses_api_binding(host, project):
    placeholder = sessionlock.acquire(f"opencode:new-{ident()}")
    assert placeholder is not None
    try:
        with pytest.raises(runtime.StructuredError) as refused:
            await _create(project)
        assert "console creation" in refused.value.detail
    finally:
        placeholder.release()


@pytest.mark.anyio
async def test_compaction_admission_refuses_an_api_launch(host, project):
    guard = opencode_admission.acquire("opencode", exclusive=True)
    assert guard is not None
    try:
        with pytest.raises(runtime.StructuredError) as refused:
            await _create(project)
        assert "maintenance" in refused.value.detail
        assert frames(project) == []  # no opencode child ever started
    finally:
        guard.release()
    await _create(project)


def _alias(tmp_path, monkeypatch) -> str:
    """A compatible console alias of opencode (another id, the same database), registered."""
    doc = test_manifest_api._doc("opencode")
    doc["identity"]["id"] = doc["display"]["name"] = "opencode-alt"
    doc["display"]["badge"] = "ocx"
    doc["binary"]["aliases"] = ["opencode"]  # the same vendor binary
    from agent_sessions.plugins import PluginProvider, manifest, provenance

    alias = PluginProvider(
        manifest.parse(doc),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "opencode-alt",
        home=tmp_path,
        env=dict(os.environ),  # the fake binary, and the database the canonical one resolves
        state_dir=tmp_path / "state",
    )
    providers = [*registry._PROVIDERS, alias]
    api = test_manifest_api._api(name="ocalt-api", source="opencode-alt", kind="opencode-acp")
    providers.append(test_manifest_api._provider(api, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    canonical = engines.get("opencode").store_path("db")
    assert Path(alias.store_path("db")).resolve() == Path(canonical).resolve()
    return str(Path(canonical).resolve())


def test_an_alias_launch_and_canonical_compaction_exclude_each_other(host, tmp_path, monkeypatch):
    """Hermes on #1336: admission is keyed by the DATABASE, so a launch through a source alias
    and a compaction through the canonical engine (and the reverse) cannot both hold it."""
    db = _alias(tmp_path, monkeypatch)
    # Compaction first: the alias launch (console or API worker) is refused.
    gate = opencode_admission.acquire("opencode", exclusive=True)
    assert gate is not None
    try:
        assert opencode_admission.acquire("opencode-alt", exclusive=False) is None
        worker = native_worker.Worker(
            _config(tmp_path, source_engine="opencode-alt", store_database=db), tmp_path
        )
        with pytest.raises(native_worker.WorkerError, match="maintenance"):
            worker.store_admission()
    finally:
        gate.release()
    # The alias launch first: compaction through the canonical engine is refused.
    launch = opencode_admission.acquire("opencode-alt", exclusive=False)
    assert launch is not None
    try:
        assert opencode_admission.acquire("opencode", exclusive=True) is None
    finally:
        launch.release()
    worker = native_worker.Worker(
        _config(tmp_path, source_engine="opencode-alt", store_database=db), tmp_path
    )
    guard = worker.store_admission()
    try:
        assert opencode_admission.acquire("opencode", exclusive=True) is None
        assert opencode_admission.acquire("opencode-alt", exclusive=True) is None
    finally:
        guard.release()
    free = opencode_admission.acquire("opencode", exclusive=True)
    assert free is not None
    free.release()


@pytest.mark.anyio
async def test_canonical_compaction_refuses_an_api_launch_through_an_alias(
    host, project, tmp_path, monkeypatch
):
    _alias(tmp_path, monkeypatch)
    gate = opencode_admission.acquire("opencode", exclusive=True)
    assert gate is not None
    try:
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.create_session("ocalt-api", str(project), operation_id=ident())
        assert "maintenance" in refused.value.detail
        assert frames(project) == []
    finally:
        gate.release()
    key = (await runtime.create_session("ocalt-api", str(project), operation_id=ident()))[
        "session_key"
    ]
    assert (await runtime.snapshot(key))["native"]["native_id"].startswith("ses_")


def test_readiness_needs_the_floor_and_opencode_s_own_database(host, tmp_path, monkeypatch):
    prov = registry._BY_ID[ENGINE]
    assert native_runtime.readiness(prov) == (True, None)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_path / "elsewhere.db"))
    ready, reason = native_runtime.readiness(prov)
    assert not ready and "overridden" in reason
    monkeypatch.setenv(
        "AGENT_SESSIONS_OPENCODE_DB", str(Path.home() / ".local/share/opencode/opencode.db")
    )
    _control("version", "1.18.34")
    native_runtime._VERSIONS.clear()
    ready, reason = native_runtime.readiness(prov)
    assert not ready and "1.18.35 or later" in reason


# --- the installed CLI (opt-in) -------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_REAL_OPENCODE") != "1",
    reason="opt-in: AGENT_SESSIONS_TEST_REAL_OPENCODE=1 drives the installed opencode (inference)",
)
@pytest.mark.anyio
async def test_real_opencode_create_approval_and_stop(tmp_home, tmp_path, monkeypatch):
    """The operator's opencode (its own login/config/models) through the real worker: create
    (which also proves our minted agent is offered as a mode and was selected), a bash call and
    an edit that must ASK and are approved once, a webfetch that must ASK and is refused, stop.
    The listener is checked to refuse an unauthenticated request. Sessions are written to the
    operator's opencode DB, in a throwaway directory under the real HOME."""
    import pwd
    import urllib.error
    import urllib.request

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    binary = os.path.join(real_home, ".opencode", "bin", "opencode")
    if not os.access(binary, os.X_OK):
        pytest.skip("opencode is not installed at ~/.opencode/bin")
    monkeypatch.setenv("HOME", real_home)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_BIN", binary)
    monkeypatch.setenv(
        "AGENT_SESSIONS_OPENCODE_DB", os.path.join(real_home, ".local/share/opencode/opencode.db")
    )
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    providers = [p for p in registry._PROVIDERS if p.engine_id != ENGINE]
    doc = test_manifest_api._api(name=ENGINE, source="opencode", kind="opencode-acp")
    providers.append(test_manifest_api._provider(doc, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    short = tempfile.mkdtemp(prefix="bloc-", dir="/tmp")
    fake = FakeHost(short)
    monkeypatch.setattr(native_runtime, "HOST", fake)
    work = Path(tempfile.mkdtemp(prefix="acp-1312-test.", dir=real_home))
    key = None
    try:
        assert runtime.describe(ENGINE).ready, runtime.describe(ENGINE).reason
        key = (await runtime.create_session(ENGINE, str(work), operation_id=ident()))["session_key"]
        session_id = key.partition(":")[2]
        worker = native_state.read_session(session_id)["current_worker"]
        child = native_state.read_lifecycle(worker)["native_pid"]
        listen = subprocess.run(
            ["/usr/bin/ss", "-ltnpH"], capture_output=True, text=True, check=False
        ).stdout
        ports = [line.split()[3] for line in listen.splitlines() if f"pid={child}," in line]
        assert ports, "opencode acp opened no listener to check"
        for addr in ports:
            assert addr.startswith("127.0.0.1:")
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f"http://{addr}/config/providers", timeout=5)  # noqa: S310
            assert denied.value.code == 401

        async def drive(text, answer):
            """Run one turn, answering EVERY request (our agent asks for everything, reads too)
            with ``answer(request)``; return the requests seen and the settled turn."""
            turn, seen = ident(), []
            await runtime.submit_turn(key, operation_id=turn, text=text)
            for _ in range(3600):
                snap = await runtime.snapshot(key)
                if snap["pending_requests"]:
                    request = snap["pending_requests"][0]
                    assert "always" not in json.dumps(request["payload"].get("options"))
                    seen.append(request)
                    await runtime.decide(
                        key,
                        decision_id=ident(),
                        request_id=request["request_id"],
                        turn_id=turn,
                        decision=answer(request),
                        user="marcus",
                    )
                elif snap["turns"][-1]["state"] != "running":
                    return seen, snap["turns"][-1]
                await asyncio.sleep(0.1)
            raise AssertionError("the turn did not settle")

        seen, settled = await drive(
            "Use your bash tool to run exactly: echo hi > hi.txt   Then reply done.",
            lambda r: "approve",
        )
        assert settled["state"] == "completed", settled
        assert any(r["kind"] == "execute" and "echo hi" in json.dumps(r["payload"]) for r in seen)
        assert all(r["choices"] == ["approve", "reject"] for r in seen)
        assert (work / "hi.txt").read_text().strip() == "hi"
        # An edit asks too, presents its diff, and completes once approved (opencode's follow-up
        # fs/write_text_file is refused: BattleLab serves no files).
        seen, settled = await drive(
            "Use your write tool (not bash) to create notes.md containing the single "
            "line: hello. Then reply done.",
            lambda r: "approve",
        )
        assert settled["state"] == "completed", settled
        assert any(r["kind"] == "edit" and "hello" in json.dumps(r["payload"]) for r in seen)
        assert (work / "notes.md").read_text().strip() == "hello"
        # webfetch — allowed by opencode's own defaults — asks under our agent; it is refused,
        # so nothing leaves the host.
        seen, settled = await drive(
            "Use your webfetch tool to fetch https://example.com and reply with its title.",
            lambda r: "reject",
        )
        assert any(r["kind"] == "fetch" or "example.com" in json.dumps(r["payload"]) for r in seen)
    finally:
        if key is not None:
            assert (await runtime.stop(key))["containment"] == "gone"
        fake.close()
        shutil.rmtree(short, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_REAL_OPENCODE") != "1",
    reason="opt-in: AGENT_SESSIONS_TEST_REAL_OPENCODE=1 runs the installed opencode",
)
def test_real_opencode_listener_stays_on_loopback_despite_a_hostile_server_config(tmp_path):
    """Hermes on #1336: an operator's global `server.mdns: true` / `hostname: 0.0.0.0` must not
    widen the listener. Our exact argv and child environment, a throwaway HOME carrying that
    config, no credentials and no inference: every socket of the child is on 127.0.0.1."""
    import pwd
    import time

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    binary = os.path.join(real_home, ".opencode", "bin", "opencode")
    if not os.access(binary, os.X_OK):
        pytest.skip("opencode is not installed at ~/.opencode/bin")
    home = Path(tempfile.mkdtemp(prefix="oc-listen-1336.", dir=real_home))
    proc = None
    try:
        (home / ".config" / "opencode").mkdir(parents=True)
        (home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps({"server": {"mdns": True, "hostname": "0.0.0.0"}})  # noqa: S104
        )
        worker = native_worker.Worker(_config(tmp_path, home=str(home), binary=binary), tmp_path)
        env = native_worker.child_environment(worker.config, worker._server_password, worker.agent)
        proc = subprocess.Popen(  # noqa: S603 — the worker's own literal argv
            worker.argv(),
            cwd=home,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(native_protocol.encode(OpencodeAcpCodec(agent=worker.agent).initialize()))
        proc.stdin.flush()
        assert b'"result"' in proc.stdout.readline()
        time.sleep(1.5)
        sockets = subprocess.run(
            ["/usr/bin/ss", "-ltunpH"], capture_output=True, text=True, check=False
        ).stdout
        mine = [line.split()[4] for line in sockets.splitlines() if f"pid={proc.pid}," in line]
        assert mine, "opencode acp opened no listener to check"
        assert all(addr.startswith("127.0.0.1:") for addr in mine), mine
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(10)
        shutil.rmtree(home, ignore_errors=True)


_PROBE_MCP = """
import json, os, sys
with open(sys.argv[1], "a") as fh:  # a boolean only: the value itself is never written
    fh.write(json.dumps({"has_password": bool(os.environ.get("OPENCODE_SERVER_PASSWORD")),
                         "mark": os.environ.get("MARK")}) + "\\n")
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if "id" not in m:
        continue
    if m.get("method") == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"),
             "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "1"}}
    else:
        r = {"tools": []} if m.get("method") == "tools/list" else {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_REAL_OPENCODE") != "1",
    reason="opt-in: AGENT_SESSIONS_TEST_REAL_OPENCODE=1 runs the installed opencode",
)
@pytest.mark.anyio
async def test_real_opencode_local_mcp_servers_start_without_the_password(tmp_path, monkeypatch):
    """Hermes on #1336, against the installed CLI: a throwaway HOME whose config declares two
    local probe MCP servers (one with its own environment). Our worker's real spawn — the
    `debug config` probe, the restated overrides — then session/new: both servers start, both
    see an empty password, and the operator's own environment still arrives. No credentials,
    no inference."""
    import pwd
    import sys
    import time

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    binary = os.path.join(real_home, ".opencode", "bin", "opencode")
    if not os.access(binary, os.X_OK):
        pytest.skip("opencode is not installed at ~/.opencode/bin")
    home = Path(tempfile.mkdtemp(prefix="oc-mcp-1336.", dir=real_home))
    worker = None
    try:
        (home / ".config" / "opencode").mkdir(parents=True)
        script, out = home / "probe_mcp.py", home / "probe.jsonl"
        script.write_text(_PROBE_MCP)
        servers = {
            "alpha": {
                "type": "local",
                "command": [sys.executable, "-I", str(script), str(out)],
                "environment": {"MARK": "alpha"},
            },
            "beta": {
                "type": "local",
                "command": [sys.executable, "-I", str(script), str(out)],
                "enabled": True,
            },
        }
        (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({"mcp": servers}))
        worker = native_worker.Worker(
            _config(tmp_path, home=str(home), binary=binary, cwd=str(home)), tmp_path
        )
        monkeypatch.setattr(worker, "report", lambda **fields: None)
        await worker.spawn()
        codec = OpencodeAcpCodec(agent=worker.agent)
        for frame in (codec.initialize(), codec.create(str(home))):
            worker.proc.stdin.write(native_protocol.encode(frame))
            await worker.proc.stdin.drain()
            while True:
                reply = json.loads(await asyncio.wait_for(worker.proc.stdout.readline(), 60))
                if reply.get("id") == frame["id"]:
                    assert "result" in reply, reply.get("error")
                    break
        for _ in range(100):
            seen = out.read_text().splitlines() if out.exists() else []
            if len(seen) >= 2:
                break
            time.sleep(0.1)
        records = sorted((json.loads(line) for line in seen), key=lambda r: str(r["mark"]))
        assert records == [
            {"has_password": False, "mark": None},
            {"has_password": False, "mark": "alpha"},
        ]
    finally:
        if worker is not None and worker.proc is not None and worker.proc.returncode is None:
            worker.proc.kill()
            await worker.proc.wait()
        shutil.rmtree(home, ignore_errors=True)
