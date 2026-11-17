"""#2084: OpenCode 2 as a native API client over ACP (`opencode2-api`, kind `opencode2-acp`).

Kinds and manifest first, then the codec against frames recorded from the real 2.2.7 binary,
then the worker's child environment, then the real worker against the scripted fake
(`fake_native.py`, named `opencode2`) through the runtime, then an opt-in run against the real
binary (`AGENT_SESSIONS_TEST_OPENCODE2_BIN`).

The gates pinned here: isolation precedes the first process (readiness `--version`, discovery
and the worker's child all run in the forced XDG tree, a 1.x overlap refuses before any spawn);
the child gets no listener password (2.x mints and scrubs its own — measured) and exactly
`[bin, "acp"]`; the session is pinned to BattleLab's own guarded or bypass agent, re-pinned on
every load, and a mode change away from it ends the generation; archive is sidecar state only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_manifest_api
import test_native_runtime as rt
from agent_sessions import (
    engines,
    native_grants,
    native_ipc,
    native_models,
    native_ownership,
    native_protocol,
    native_runtime,
    native_state,
    native_worker,
    sessionlock,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import opencode, opencode2
from agent_sessions.native_protocol import Opencode2AcpCodec, ProtocolError
from agent_sessions.plugins import api_source, kinds

frames, ident, pending, settle = rt.frames, rt.ident, rt.pending, rt.settle
project = rt.project  # the scripted-agent harness's fixture

ENGINE = "opencode2-api"
KIND = "opencode2-acp"
SES = "ses_e1a841c9fffeV6sPpC77kcJ1ut"
AGENT = "battlelab-api-0123456789abcdef"
FAKE = Path(__file__).with_name("fake_native.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- kinds and manifest -------------------------------------------------------------------------


def test_the_2x_kind_decides_every_table_a_kind_must_answer():
    assert kinds.API_SOURCE_KINDS[KIND] == ("opencode2-sqlite", "opencode2-sqlite")
    assert KIND not in kinds.API_BYPASS_KINDS  # guarded only until #2108
    assert kinds.API_IMAGE_INPUT[KIND] is False  # gated like 1.x (#1332)
    assert kinds.API_MISSION_READY[KIND] is False
    assert "#2084" in kinds.API_MISSION_REFUSAL[KIND]
    assert kinds.API_MCP_KINDS[KIND] == ("opencode2-config-content",)
    assert KIND in native_ipc.ADAPTERS
    assert ("api", KIND) in runtime._ADAPTERS
    assert native_runtime._FLOORS[KIND] == (2, 2, 7)
    assert KIND in native_runtime._ISOLATED and KIND not in native_runtime._VENDOR_STORES
    assert KIND in native_runtime._ADOPTED
    assert native_models._DISCOVER[KIND] is native_models._discover_offered


def test_the_manifest_is_sourced_from_the_2x_console_agent():
    prov = engines.get(ENGINE)
    m = prov.manifest
    assert (m.runtime, m.api.kind, m.api.source) == ("api", KIND, "opencode2")
    assert m.store.layout == "battlelab-chat" and m.store.env_override == (
        "AGENT_SESSIONS_OPENCODE2_API_DIR"
    )
    assert str(m.store.root).endswith("native/opencode2-api")
    assert m.tools_mcp == "opencode2-config-content"
    assert m.display.badge == "ota" and m.display.accent in kinds.ACCENT_TOKENS
    by_id = {p.engine_id: p for p in engines.all_providers()}
    assert api_source.validate_provider(prov, by_id).engine_id == "opencode2"


def test_a_1x_source_is_refused_for_the_2x_kind(tmp_path):
    doc = test_manifest_api._api(name="o2-wrong", source="opencode", kind=KIND)
    doc["tools"] = {"mcp": "opencode2-config-content"}
    wrong = test_manifest_api._provider(doc, tmp_path)
    by_id = {p.engine_id: p for p in engines.all_providers()}
    with pytest.raises(api_source.SourceError, match="incompatible with protocol"):
        api_source.validate_provider(wrong, {**by_id, wrong.engine_id: wrong})


# --- the codec, against frames recorded from 2.2.7 (scratch XDG tree, 2026-11-16) ----------------

INIT_227 = {
    "protocolVersion": 1,
    "agentCapabilities": {
        "loadSession": True,
        "mcpCapabilities": {"http": True, "sse": False},
        "promptCapabilities": {"embeddedContext": True, "image": True},
        "sessionCapabilities": {
            "additionalDirectories": {},
            "close": {},
            "delete": {},
            "fork": {},
            "list": {},
            "resume": {},
        },
        "_meta": {"opencode/child-session-updates": True},
    },
    "authMethods": [
        {
            "description": "Run `opencode auth login` in the terminal",
            "name": "Login with opencode",
            "id": "opencode-login",
        }
    ],
    "_meta": {"steering": {"supported": True}},
    "agentInfo": {"name": "OpenCode", "version": "2.2.7"},
}


def _options(mode: str) -> list[dict]:
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": "opencode/big-pickle",
            "options": [
                {"value": "opencode/beam-free", "name": "opencode/Beam Free"},
                {"value": "opencode/big-pickle", "name": "opencode/Big Pickle"},
                {"value": "not a model id", "name": "junk"},
            ],
        },
        {
            "id": "mode",
            "name": "Session Mode",
            "category": "mode",
            "type": "select",
            "currentValue": mode,
            "options": [
                {"value": AGENT, "name": AGENT, "description": "BattleLab API client"},
                {"value": "build", "name": "Build"},
                {"value": "plan", "name": "Plan"},
            ],
        },
    ]


SHELL_ASK = {
    "sessionId": SES,
    "toolCall": {
        "toolCallId": "chatcmpl-tool-a3ffde37f989228a",
        "name": "shell",
        "title": "touch marker_shell",
        "kind": "execute",
        "status": "pending",
        "locations": [{"path": "/w"}],
        "rawInput": {"command": "touch marker_shell", "cwd": "/w"},
    },
    "options": [
        {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
        {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
        {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
    ],
}
WRITE_ASK = {
    **SHELL_ASK,
    "toolCall": {
        "toolCallId": "chatcmpl-tool-bea17d5489dc6d69",
        "name": "write",
        "title": "marker_edit.txt",
        "kind": "edit",
        "status": "pending",
        "locations": [{"path": "/w/marker_edit.txt"}],
        "rawInput": {"path": "marker_edit.txt", "content": "hi\n"},
        "content": [
            {"type": "diff", "path": "/w/marker_edit.txt", "oldText": None, "newText": "hi\n"}
        ],
    },
}


def _codec() -> Opencode2AcpCodec:
    codec = Opencode2AcpCodec(agent=AGENT)
    init = codec.initialize()
    [event] = codec.feed({"jsonrpc": "2.0", "id": init["id"], "result": INIT_227})
    assert event.kind == "initialized" and codec.version == (2, 2, 7)
    new = codec.create("/w")
    assert new["method"] == "session/new" and new["params"] == {"cwd": "/w", "mcpServers": []}
    events = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": new["id"],
            "result": {"sessionId": SES, "configOptions": _options(AGENT)},
        }
    )
    assert events[0].kind == "session" and events[0].data["native_id"] == SES
    assert events[0].data["model_configured"] == "opencode/big-pickle"
    pin = codec.pin_mode()
    assert pin["params"] == {"sessionId": SES, "configId": "mode", "value": AGENT}
    [event] = codec.feed(
        {"jsonrpc": "2.0", "id": pin["id"], "result": {"configOptions": _options(AGENT)}}
    )
    assert event.kind == "session"
    return codec


def test_argv_is_exactly_acp_and_there_is_no_config_probe():
    assert Opencode2AcpCodec.argv("/opt/oc2/opencode") == ["/opt/oc2/opencode", "acp"]
    assert Opencode2AcpCodec.config_probe_argv is None  # never `debug config` for 2.x
    assert native_protocol.OpencodeAcpCodec.argv("/x")[1:] == [
        "acp",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        "--mdns=false",
    ]  # 1.x's unchanged


@pytest.mark.parametrize("version", ["2.2.6", "1.18.35"])
def test_a_cli_below_the_2x_floor_is_refused_at_the_handshake(version):
    codec = Opencode2AcpCodec(agent=AGENT)
    init = codec.initialize()
    reply = {**INIT_227, "agentInfo": {"name": "OpenCode", "version": version}}
    [event] = codec.feed({"jsonrpc": "2.0", "id": init["id"], "result": reply})
    assert event.kind == "error" and event.data["message"] == (
        "opencode 2 2.2.7 or later is required"
    )


def test_the_offered_models_are_taken_from_session_new():
    codec = _codec()
    assert codec.offered_models == [
        {"id": "opencode/beam-free", "label": "opencode/Beam Free"},
        {"id": "opencode/big-pickle", "label": "opencode/Big Pickle"},
    ]
    assert native_protocol.OpencodeAcpCodec(agent=AGENT)._offered([]) is None  # 1.x lists by CLI


def test_a_turn_streams_and_completes_with_2x_frames():
    codec = _codec()
    prompt = codec.submit("Reply with exactly the word KIWI42.", ident())
    assert prompt["method"] == "session/prompt"
    out = codec.feed(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": SES,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "KIWI42"},
                },
            },
        }
    )
    assert out[0].kind == "text" and out[0].data["text"] == "KIWI42"
    done = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": prompt["id"],
            "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 3429, "outputTokens": 99, "totalTokens": 8136},
                "_meta": {},
            },
        }
    )
    assert done[-1].kind == "turn_completed" and done[-1].data["state"] == "completed"


def test_a_prompt_error_fails_its_turn_with_the_agents_message():
    """Recorded on 2.2.7 when the free tier was exhausted (`provider.free-limit`)."""
    codec = _codec()
    prompt = codec.submit("hi", ident())
    done = codec.feed(
        {
            "jsonrpc": "2.0",
            "id": prompt["id"],
            "error": {
                "code": -32603,
                "message": "Internal error: Free usage limit reached. Connect a provider or "
                "subscribe to OpenCode Go to keep going, or switch models.",
                "data": {"service": "session", "errorName": "provider.free-limit"},
            },
        }
    )
    assert done[-1].kind == "turn_completed" and done[-1].data["state"] == "failed"
    assert "Free usage limit reached" in done[-1].data["error"]


def test_a_shell_request_is_presented_whole_and_a_reject_selects_reject():
    codec = _codec()
    turn = ident()
    codec.submit("touch it", turn)
    [ask] = codec.feed(
        {"jsonrpc": "2.0", "id": 7, "method": "session/request_permission", "params": SHELL_ASK}
    )
    assert ask.kind == "approval" and ask.data["complete"] is True
    assert ask.data["choices"] == ["approve", "reject"]
    frame = codec.decide(ask.data["request_id"], "reject")
    assert frame == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"outcome": {"outcome": "selected", "optionId": "reject"}},
    }


def test_a_new_file_write_with_a_null_old_text_is_presentable():
    """2.2.7 sends a new file's diff with ``oldText: null``; the file and its whole new text are
    there, so the request is complete — and still a request, never an implicit allow."""
    codec = _codec()
    codec.submit("write it", ident())
    [ask] = codec.feed(
        {"jsonrpc": "2.0", "id": 8, "method": "session/request_permission", "params": WRITE_ASK}
    )
    assert ask.data["complete"] is True
    # Without the diff it stays decline-only, as for 1.x.
    blind = {**WRITE_ASK, "toolCall": {**WRITE_ASK["toolCall"], "content": []}}
    blind["toolCall"]["rawInput"] = {"path": "marker_edit.txt"}
    [ask] = codec.feed(
        {"jsonrpc": "2.0", "id": 9, "method": "session/request_permission", "params": blind}
    )
    assert ask.data["complete"] is False and ask.data["choices"] == ["reject"]
    # 1.x keeps its stricter rule (a string old text).
    one_x = native_protocol.OpencodeAcpCodec(agent=AGENT)
    assert one_x._unpresentable(WRITE_ASK["toolCall"]) is not None


def test_a_mode_change_away_from_our_agent_ends_the_connection():
    """Measured on 2.2.7: `session/set_config_option mode build` is accepted and the next shell
    call then ran without asking. BattleLab never sends one, and an update showing another mode
    after the pin is a violation."""
    codec = _codec()
    with pytest.raises(ProtocolError, match="left its pinned mode"):
        codec.feed(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": SES,
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": _options("build"),
                    },
                },
            }
        )


def test_a_pin_the_agent_did_not_take_is_an_error():
    codec = Opencode2AcpCodec(agent=AGENT)
    init = codec.initialize()
    codec.feed({"jsonrpc": "2.0", "id": init["id"], "result": INIT_227})
    new = codec.create("/w")
    codec.feed(
        {
            "jsonrpc": "2.0",
            "id": new["id"],
            "result": {"sessionId": SES, "configOptions": _options("build")},
        }
    )
    pin = codec.pin_mode()
    [event] = codec.feed(
        {"jsonrpc": "2.0", "id": pin["id"], "result": {"configOptions": _options("build")}}
    )
    assert event.kind == "error"
    with pytest.raises(ProtocolError, match="pinned mode"):
        codec.submit("hi", ident())


def test_cancel_answers_every_pending_request_cancelled():
    codec = _codec()
    codec.submit("touch it", ident())
    codec.feed(
        {"jsonrpc": "2.0", "id": 7, "method": "session/request_permission", "params": SHELL_ASK}
    )
    assert codec.interrupt()["method"] == "session/cancel"
    sent = [e.data["frame"] for e in codec.cancel_pending() if e.kind == "send"]
    assert sent == [{"jsonrpc": "2.0", "id": 7, "result": {"outcome": {"outcome": "cancelled"}}}]


def test_no_standing_grant_is_offered_for_2x():
    payload = {"toolCall": SHELL_ASK["toolCall"], "options": SHELL_ASK["options"]}
    derived = native_grants.derive(KIND, "execute", payload, "/w")
    assert derived["always"] == []
    assert native_grants.derive("opencode-acp", "execute", payload, "/w")["always"]  # 1.x's


# --- the API agent and the worker's child environment ------------------------------------------


def _config(tmp_path, **extra) -> dict:
    root = tmp_path / "o2root"
    return {
        "worker_id": ident(),
        "session_key": f"{ENGINE}:{ident()}",
        "adapter": KIND,
        "journal_root": str(tmp_path / "journal"),
        "capability": native_worker.native_ipc.Capability.create()._value,
        "connection_id": ident(),
        "source_engine": "opencode2",
        "binary": "/opt/oc2/opencode",
        "cwd": str(tmp_path),
        "mode": "create",
        "home": str(tmp_path),
        "path": "/usr/bin:/bin",
        "forced_env": {var: str(root / sub) for var, sub in opencode2.XDG_DIRS.items()},
        **extra,
    }


def test_the_child_runs_in_the_forced_tree_under_our_guarded_agent(tmp_path):
    config = _config(tmp_path)
    a, b = native_worker.Worker(config, tmp_path), native_worker.Worker(config, tmp_path)
    assert isinstance(a.codec, Opencode2AcpCodec) and a.argv() == ["/opt/oc2/opencode", "acp"]
    assert a.agent != b.agent and a._server_password is None  # 2.x mints its own listener secret
    env = native_worker.child_environment(config, None, a.agent)
    assert {k: env[k] for k in opencode2.XDG_DIRS} == config["forced_env"]
    assert not {"OPENCODE_PASSWORD", "OPENCODE_SERVER_PASSWORD"} & set(env)
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["default_agent"] == a.agent and list(cfg["agents"]) == [a.agent]
    assert cfg["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    own = cfg["agents"][a.agent]
    assert own["mode"] == "primary" and own["permissions"] == opencode2.API_GUARDED_PERMISSIONS
    assert own["permissions"][0] == {"action": "*", "resource": "*", "effect": "ask"}  # first
    assert "server" not in cfg  # 2.x's ACP client decides its listener itself


def test_bypass_is_refused_until_the_grant_is_approved(tmp_path):
    """#2108: an allow-all agent for opencode2-api is a new blanket grant that waits for the
    operator's own approval. Refused twice: the kind cannot ask for it (create refuses the field),
    and the config builder cannot build it."""
    assert "opencode2-acp" not in kinds.API_BYPASS_KINDS
    assert "opencode-acp" in kinds.API_BYPASS_KINDS  # 1.x is unchanged
    agent = opencode.mint_api_agent()
    with pytest.raises(ValueError, match="guarded only"):
        opencode2.api_config_content(agent, bypass=True)
    cfg = json.loads(opencode2.api_config_content(agent))
    assert cfg["agents"][agent]["permissions"] == opencode2.API_GUARDED_PERMISSIONS
    assert all(r["effect"] != "allow" or r["action"] != "*" for r in cfg["permissions"])
    assert not hasattr(opencode2, "API_BYPASS_PERMISSIONS")


def test_the_forced_tree_is_laid_last_over_everything(tmp_path):
    """Even a memory/secrets path cannot shadow the tree: the XDG variables are applied last."""
    config = _config(tmp_path)
    worker = native_worker.Worker(config, tmp_path)
    env = native_worker.child_environment(config, None, worker.agent)
    assert list(env)[-len(opencode2.XDG_DIRS) :] == list(opencode2.XDG_DIRS)


@pytest.mark.parametrize(
    "forced",
    [
        {},
        None,
        {"XDG_DATA_HOME": "/r/data"},
        {**{v: "/r/x" for v in opencode2.XDG_DIRS}, "HOME": "/elsewhere"},
        {v: "relative" for v in opencode2.XDG_DIRS},
    ],
)
def test_a_generation_without_a_valid_tree_is_refused(tmp_path, forced):
    config = _config(tmp_path)
    if forced is None:
        config.pop("forced_env")
    else:
        config["forced_env"] = forced
    with pytest.raises(native_worker.WorkerError):
        native_worker.child_environment(config, None, native_worker.Worker(config, tmp_path).agent)


def test_an_unminted_agent_name_is_refused():
    with pytest.raises(ValueError, match="malformed API agent"):
        opencode2.api_config_content("build")
    with pytest.raises(ValueError):
        opencode2.api_config_content("battlelab-mission-0123456789abcdef")


# --- discovery: the session's own list, no process ----------------------------------------------


def test_models_come_from_the_recorded_session_list_and_spawn_nothing(tmp_path, monkeypatch):
    store = tmp_path / "o2api"
    store.mkdir()
    client = SimpleNamespace(store_root=lambda: store)
    with pytest.raises(native_models.ProbeError, match="start one"):
        native_models._discover_offered("/x", source=None, client=client)
    native_models.record_offered(
        store, [{"id": "opencode/big-pickle", "label": "Big Pickle"}, {"id": "bad id"}]
    )
    assert (store / native_models.OFFERED_FILE).stat().st_mode & 0o777 == 0o600
    [model] = native_models._discover_offered("/x", source=None, client=client)
    assert model["id"] == "opencode/big-pickle" and model["label"] == "Big Pickle"
    assert native_models._discover_offered.spawns_nothing is True


def test_two_clients_on_one_binary_never_read_each_others_models(tmp_path, monkeypatch):
    """#2095 review: the list is read from each CLIENT's own store, so the cache, its single-flight
    lock and its refresher key on that store too — not on the shared executable alone."""
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    source = SimpleNamespace(entrypoint_path=lambda: str(binary))
    monkeypatch.setattr(api_source, "resolve", lambda prov: SimpleNamespace(source=source))
    manifest = SimpleNamespace(api=SimpleNamespace(kind=KIND, source="opencode2"))

    def client(name, model):
        store = tmp_path / name
        store.mkdir()
        native_models.record_offered(store, [{"id": model, "label": model}])
        return SimpleNamespace(manifest=manifest, store_root=lambda: store)

    a, b = client("a", "vendor/model-a"), client("b", "vendor/model-b")
    monkeypatch.setattr(native_models, "_CACHE", {})
    monkeypatch.setattr(native_models, "_LOCKS", {})
    assert native_models._target(a).key != native_models._target(b).key
    assert [m["id"] for m in native_models.models(a).models] == ["vendor/model-a"]
    assert [m["id"] for m in native_models.models(b).models] == ["vendor/model-b"]
    assert native_models._offers(
        native_models.snapshot_for(b, "vendor/model-b"), "vendor/model-b", None
    )
    assert not native_models._offers(
        native_models.snapshot_for(b, "vendor/model-a"), "vendor/model-a", None
    )


def test_an_isolated_source_is_never_probed_by_a_spawning_discovery(
    tmp_home, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(tmp_path / "o2root"))
    source = engines.get("opencode2")
    ran = []
    target = native_models._Target(("k",), lambda b: ran.append(b) or [], "/x", source)
    listing = native_models._probe(target)
    assert listing.status == "unavailable" and "isolated tree" in listing.reason and ran == []
    # A tree that reaches 1.x's store refuses before anything, even a spawn-free discovery.
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(tmp_home / ".local/share"))
    target = native_models._Target(
        ("k2",), native_models._discover_offered, "/x", source, engines.get(ENGINE)
    )
    listing = native_models._probe(target)
    assert listing.status == "unavailable" and "overlaps" in listing.reason


# --- the real worker against the scripted fake ---------------------------------------------------


@pytest.fixture
def oc2(tmp_home, tmp_path, monkeypatch):
    bindir = tmp_path / "engine-bin"
    bindir.mkdir()
    script = bindir / "opencode2"  # the fake reports 2.2.7 under this name
    script.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    script.chmod(0o755)
    root = tmp_path / "o2root"
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_BIN", str(script))
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(root))
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_API_DIR", str(tmp_path / "o2api"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    # A scratch 1.x database where 2.x's defaults would put it: it must stay byte-identical.
    one_x = tmp_home / ".local/share/opencode/opencode.db"
    one_x.parent.mkdir(parents=True)
    con = sqlite3.connect(one_x)
    con.execute("CREATE TABLE session (id TEXT)")
    con.commit()
    con.close()
    short = tempfile.mkdtemp(prefix="bl2a-", dir="/tmp")  # AF_UNIX path budget, any TMPDIR
    fake = rt.FakeHost(short)
    monkeypatch.setattr(native_runtime, "HOST", fake)
    yield SimpleNamespace(
        host=fake, root=root, bin=script, one_x=one_x, digest=_sha(one_x), home=tmp_home
    )
    fake.close()
    shutil.rmtree(short, ignore_errors=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _create(work: Path, **kw) -> str:
    snap = await runtime.create_session(ENGINE, str(work), operation_id=ident(), **kw)
    return snap["session_key"]


@pytest.mark.anyio
async def test_a_session_runs_acp_in_the_forced_tree_and_records_its_models(
    oc2, project, monkeypatch
):
    seen = []
    real = native_runtime.native_version
    native_runtime._VERSIONS.clear()
    monkeypatch.setattr(
        native_runtime, "native_version", lambda b, env=None: seen.append(env) or real(b, env)
    )
    assert runtime.describe(ENGINE).ready, runtime.describe(ENGINE).reason
    key = await _create(project)
    tree = {var: str(oc2.root / sub) for var, sub in opencode2.XDG_DIRS.items()}
    assert seen and all(env == tree for env in seen)  # `--version` ran inside the tree
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "echo:hello"
    logged = [f for f in frames(project) if "env" in f]
    assert {tuple(f["argv"]) for f in logged} == {("acp",)}
    assert all(f["env"]["xdg"] == tree for f in logged)
    keys = set(logged[0]["env"]["keys"])
    assert not {"OPENCODE_PASSWORD", "OPENCODE_SERVER_PASSWORD"} & keys
    assert not {k for k in keys if k.startswith("AGENT_SESSIONS_")}
    cfg = json.loads(logged[0]["env"]["config"])
    assert cfg["agents"][cfg["default_agent"]]["permissions"] == opencode2.API_GUARDED_PERMISSIONS
    sent = [f["frame"]["method"] for f in frames(project) if f["frame"].get("method")]
    i = sent.index("session/new")
    assert sent[i + 1] == "session/set_config_option"  # pinned before any prompt
    assert _sha(oc2.one_x) == oc2.digest
    # The session's own model list, for the create form — no CLI process asked.
    listing = await asyncio.to_thread(native_models.models_fresh, engines.get(ENGINE))
    assert listing.status == "ok" and {m["id"] for m in listing.models} == {
        "local/fake-a",
        "local/fake-b",
    }


@pytest.mark.anyio
async def test_a_guarded_tool_call_waits_a_reject_wins_and_a_resume_stays_guarded(oc2, project):
    key = await _create(project)
    for generation in range(2):
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="TOOL:shell")
        [request] = (await pending(key))["pending_requests"]
        assert request["choices"] == ["approve", "reject"]
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
        if generation == 0:
            await runtime.stop(key)  # the next turn resumes through `session/load`
    sent = [f["frame"] for f in frames(project) if f["frame"].get("method")]
    methods = [f["method"] for f in sent]
    assert methods.count("session/new") == 1 and methods.count("session/load") == 1
    agents = [
        json.loads(c)["default_agent"]
        for c in dict.fromkeys(f["env"]["config"] for f in frames(project) if "env" in f)
    ]
    pins = [
        f["params"]["value"]
        for f in sent
        if f["method"] == "session/set_config_option" and f["params"]["configId"] == "mode"
    ]
    assert pins == agents and len(set(agents)) == 2  # each generation pins ITS agent
    assert methods[methods.index("session/load") + 1] == "session/set_config_option"


@pytest.mark.anyio
async def test_leaving_our_agent_mid_turn_ends_the_generation(oc2, project):
    key = await _create(project)
    turn = ident()
    try:
        await runtime.submit_turn(key, operation_id=turn, text="MODE_ESCAPE")
    except runtime.StructuredError as exc:
        assert exc.status == 503  # the end may beat the receipt on a loaded host
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["state"] == "uncertain"


@pytest.mark.anyio
async def test_an_overlapping_tree_refuses_before_any_process(oc2, project, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(oc2.home / ".local/share"))
    ran = []
    monkeypatch.setattr(native_runtime, "native_version", lambda *a, **k: ran.append(a))
    ready, reason = native_runtime.readiness(engines.get(ENGINE))
    assert not ready and "overlaps the opencode 1.x store" in reason
    with pytest.raises(runtime.StructuredError):
        await _create(project)
    assert ran == [] and oc2.host.launches == [] and frames(project) == []
    assert _sha(oc2.one_x) == oc2.digest
    with pytest.raises(native_runtime.NativeError, match="overlaps"):
        native_runtime.source_environment(KIND, engines.get("opencode2"))


def test_an_isolated_adapter_whose_source_reports_no_tree_is_refused():
    plain = SimpleNamespace()  # no `agent_env`: `forced_env` answers {}
    with pytest.raises(native_runtime.NativeError, match="isolated tree"):
        native_runtime.source_environment(KIND, plain)
    assert native_runtime.source_environment("opencode-acp", plain) == {}


@pytest.mark.anyio
async def test_api_ownership_fences_the_2x_console_on_the_same_session(oc2, project):
    key = await _create(project)
    native = (await runtime.snapshot(key))["native"]["native_id"]
    assert native.startswith("ses_")
    intent = native_ownership.lookup(key)
    assert intent.state == "bound" and intent.native_id == native
    console = engines.get("opencode2")
    source = native_ownership.source_identity(console)
    assert source == intent.source
    assert source.canonical_path == str(Path(console.store_path("db")).resolve())
    with pytest.raises(native_ownership.OwnershipError, match="belongs to an API session"):
        native_ownership.check_console(console, native)
    await runtime.stop(key)
    writer = sessionlock.acquire(f"opencode2:{native}")  # a live console on it
    try:
        with pytest.raises(runtime.StructuredError, match="another BattleLab writer"):
            await runtime.submit_turn(key, operation_id=ident(), text="hello")
    finally:
        writer.release()


# --- the real binary (opt-in) ---------------------------------------------------------------------

REAL = os.environ.get("AGENT_SESSIONS_TEST_OPENCODE2_BIN")
SETTLED = {"completed", "failed", "interrupted", "uncertain"}


def _descendants(pid: int) -> dict[int, str]:
    rows = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            cmd = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        rows[int(entry)] = (int(stat.rsplit(")", 1)[1].split()[1]), cmd.strip())
    out, frontier = {}, {pid}
    while frontier:
        nxt = {p for p, (ppid, _) in rows.items() if ppid in frontier and p not in out}
        out.update({p: rows[p][1] for p in nxt})
        frontier = nxt
    if pid in rows:
        out[pid] = rows[pid][1]
    return out


def _listeners(pids) -> list[tuple[str, int]]:
    inodes = set()
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if target.startswith("socket:"):
                    inodes.add(target[8:-1])
        except OSError:
            continue
    out = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(table).read_text().splitlines()[1:]:
            cols = line.split()
            if cols[3] == "0A" and cols[9] in inodes:
                ip, port = cols[1].split(":")
                out.append((ip, int(port, 16)))
    return out


def _files(top: Path) -> dict[str, int]:
    out = {}
    for path in top.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                out[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue
    return out


@pytest.mark.skipif(
    not REAL, reason="opt-in: AGENT_SESSIONS_TEST_OPENCODE2_BIN=<opencode 2.2.7> (free model)"
)
@pytest.mark.anyio
async def test_the_real_binary_end_to_end_isolated_and_guarded(oc2, project, monkeypatch):
    """new → reply → guarded wait/reject → stop → resume (still guarded) → archive (sidecar);
    the journal holds every turn; nothing outside the 2.x root is written, the scratch 1.x db is
    byte-identical, every listener is loopback and authenticated, no `serve --service`, nothing
    survives. A permissive project `opencode.json` (allow-all, `default_agent: build`) makes
    the guard behavioural: only BattleLab's own agent asks."""
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_BIN", REAL)
    (project / "opencode.json").write_text(
        json.dumps(
            {
                "permission": {"*": "allow", "bash": "allow", "edit": "allow"},
                "default_agent": "build",
                "agent": {"build": {"permission": {"*": "allow"}}},
            }
        )
    )
    native_runtime._VERSIONS.clear()
    version = native_runtime.native_version(
        REAL, native_runtime.source_environment(KIND, engines.get("opencode2"))
    )
    print(f"opencode 2 CLI version under test: {version}")  # noqa: T201 — recorded evidence
    assert version is not None and version >= (2, 2, 7)
    allowed = [
        oc2.root,
        oc2.home / "o2api",
        oc2.home / "plugin-state",
        oc2.home / ".config" / "agent-sessions",
        project,
        # BattleLab's own single-writer locks (the session and the native history).
        Path(os.environ.get("AGENT_SESSIONS_LOCK_DIR") or oc2.home / ".agent-sessions/locks"),
    ]
    before = _files(oc2.home)
    started = time.time()
    assert runtime.describe(ENGINE).ready, runtime.describe(ENGINE).reason
    key = await _create(project)
    sid = key.partition(":")[2]
    seen: dict[int, str] = {}  # every agent process observed, pid -> its command line

    def child() -> int:
        worker = native_state.read_session(sid)["current_worker"]
        return native_state.read_lifecycle(worker).get("native_pid") or -1

    try:
        for _ in range(100):
            tree_now = _descendants(child())
            seen.update(tree_now)
            if any("serve" in c for c in tree_now.values()):
                break
            await asyncio.sleep(0.1)
        assert any(c.endswith("serve --stdio --port 0") for c in seen.values()), seen
        assert not any("--service" in c for c in seen.values()), seen
        listen = _listeners(seen)
        assert listen and all(ip == "0100007F" for ip, _ in listen), listen
        for _, port in listen:
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=5)  # noqa: S310
            assert denied.value.code == 401

        async def drive(text, answer):
            turn, asked = ident(), []
            await runtime.submit_turn(key, operation_id=turn, text=text)
            for _ in range(3000):
                snap = await runtime.snapshot(key)
                seen.update(_descendants(child()))
                if snap["pending_requests"]:
                    request = snap["pending_requests"][0]
                    asked.append(request)
                    await runtime.decide(
                        key,
                        decision_id=ident(),
                        request_id=request["request_id"],
                        turn_id=turn,
                        decision=answer,
                        user="marcus",
                    )
                elif snap["turns"][-1]["state"] in SETTLED:
                    return asked, snap["turns"][-1]
                await asyncio.sleep(0.1)
            raise AssertionError("the turn did not settle")

        asked, settled = await drive(
            "Reply with exactly the word KIWI42 and nothing else.", "reject"
        )
        if "Free usage limit" in (settled.get("reason") or ""):
            # The vendor's free tier is metered per host; an exhausted quota proves nothing
            # about BattleLab either way (measured on 2.2.7: `provider.free-limit`).
            pytest.skip("opencode's free model quota is exhausted on this host")
        assert settled["state"] == "completed" and "KIWI42" in (settled["reply"] or ""), json.dumps(
            settled
        )[:3000]
        assert asked == []
        shell = (
            "Use your shell tool to run exactly: touch marker_shell   Do not ask me anything, "
            "just call the tool."
        )
        asked, settled = await drive(shell, "reject")
        assert any(
            r["kind"] == "execute" and "touch marker_shell" in json.dumps(r["payload"])
            for r in asked
        ), asked
        assert not (project / "marker_shell").exists()
        await runtime.stop(key)
        # Resume: a new generation loads the session, pins ITS agent, and still asks.
        asked, settled = await drive(shell.replace("marker_shell", "marker_resume"), "reject")
        assert any("touch marker_resume" in json.dumps(r["payload"]) for r in asked), asked
        assert not (project / "marker_resume").exists()
        snap = await runtime.snapshot(key)
        texts = json.dumps(snap["turns"])
        assert "KIWI42" in texts and "marker_shell" in texts and "marker_resume" in texts
        assert len(snap["turns"]) == 3
    finally:
        assert (await runtime.stop(key))["containment"] == "gone"
    await asyncio.sleep(1.0)
    survivors = [p for p, cmd in seen.items() if _descendants(p).get(p) == cmd]
    assert survivors == [], {p: seen[p] for p in survivors}
    # Archive is BattleLab's sidecar: the 2.x database is untouched by it.
    db = oc2.root / "data/opencode/opencode.db"
    native_digest = _sha(db)
    prov = engines.get(ENGINE)
    prov.archive(sid)
    assert _sha(db) == native_digest
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT time_archived FROM session_v2 WHERE parent_id IS NULL"
        ).fetchall()
    finally:
        con.close()
    assert rows and all(r[0] is None for r in rows)
    assert _sha(oc2.one_x) == oc2.digest
    after = _files(oc2.home)
    changed = [
        p
        for p, mtime in after.items()
        if (p not in before or before[p] != mtime)
        and not any(Path(p).is_relative_to(a) for a in allowed)
    ]
    assert changed == [], changed
    assert time.time() - started < 1800
