"""The memory tool's per-launch wiring (#1506): `agent_tools` and the manifest's `tools.mcp`.

Each kind's exact payload (what the #1506 probes proved each CLI accepts), nothing added with memory
off or for an engine that declares no kind, and the closed vocabulary in the manifest validator.
The seams themselves are pinned in `test_memory_tool_seams.py`.
"""

from __future__ import annotations

import json
import sys
import tomllib
from types import SimpleNamespace

import pytest

from agent_sessions import agent_tools, engines, memory
from agent_sessions.plugins import FIRST_PARTY_DIR, kinds
from agent_sessions.plugins.manifest import ManifestError, load_bytes

PATHS = {memory.ENV_READ: "/m/p-1/notes", memory.ENV_INBOX: "/m/p-1/inbox/m" + "0" * 32}


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/op")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(sys, "executable", "/opt/bl/venv/bin/python")


SERVER_ENV = {
    memory.ENV_READ: PATHS[memory.ENV_READ],
    memory.ENV_INBOX: PATHS[memory.ENV_INBOX],
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/op",
    "LANG": "en_US.UTF-8",
}
ARGS = ["-I", "-m", "agent_sessions.memory_mcp"]


def manifest(kind):
    return SimpleNamespace(tools_mcp=kind)


def test_the_server_is_the_apps_own_interpreter_isolated_with_a_minimal_env():
    s = agent_tools.server(PATHS)
    assert s.command == "/opt/bl/venv/bin/python"
    # `-I`: the CLI starts it in the WORKSPACE; the cwd must never be on sys.path.
    assert list(s.args) == ARGS
    assert dict(s.env) == SERVER_ENV


@pytest.mark.parametrize(
    "bad",
    [
        {},
        None,
        {memory.ENV_READ: "/m/notes"},
        {**PATHS, "LD_PRELOAD": "/x.so"},
        {**PATHS, memory.ENV_READ: "relative"},
        {**PATHS, memory.ENV_INBOX: "/m/in\nbox"},
    ],
)
def test_no_server_without_two_valid_memory_paths(bad):
    assert agent_tools.server(bad) is None


def test_no_server_without_an_absolute_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    assert agent_tools.server(PATHS) is None


def test_claude_gets_one_mcp_config_flag_at_the_end():
    mcp = agent_tools.terminal_launch(manifest("claude-mcp-config-flag"), PATHS, {})
    assert mcp.prefix == () and dict(mcp.env) == {}
    flag, payload = mcp.suffix
    assert flag == "--mcp-config"
    assert json.loads(payload) == {
        "mcpServers": {
            "battlelab_memory": {
                "type": "stdio",
                "command": "/opt/bl/venv/bin/python",
                "args": ARGS,
                "env": SERVER_ENV,
            }
        }
    }
    # LAST: the flag is variadic, so a positional after it would be swallowed as a config.
    launch = ["/bin/claude", "--resume", "x", "--dangerously-skip-permissions"]
    assert mcp.argv(launch) == [*launch, "--mcp-config", payload]


def test_codex_gets_three_config_overrides_before_any_subcommand():
    mcp = agent_tools.terminal_launch(manifest("codex-config-override"), PATHS, {})
    assert mcp.suffix == () and dict(mcp.env) == {}
    assert mcp.prefix == (
        "-c",
        'mcp_servers.battlelab_memory.command="/opt/bl/venv/bin/python"',
        "-c",
        'mcp_servers.battlelab_memory.args=["-I", "-m", "agent_sessions.memory_mcp"]',
        "-c",
        "mcp_servers.battlelab_memory.env={"
        f'BATTLELAB_MEMORY = "{PATHS[memory.ENV_READ]}", '
        f'BATTLELAB_MEMORY_INBOX = "{PATHS[memory.ENV_INBOX]}", '
        'PATH = "/usr/bin:/bin", HOME = "/home/op", LANG = "en_US.UTF-8"}',
    )
    assert mcp.argv(["/bin/codex", "resume", "abc"]) == ["/bin/codex", *mcp.prefix, "resume", "abc"]


def test_codex_overrides_are_valid_toml_for_awkward_paths(monkeypatch):
    """codex parses each value as TOML: quotes, backslashes and non-ASCII must survive exactly."""
    odd = {
        memory.ENV_READ: '/m/"quoted" \\ dir/ü/notes',
        memory.ENV_INBOX: "/m/tab\there/😀/inbox",
    }
    mcp = agent_tools.terminal_launch(manifest("codex-config-override"), odd, {})
    parsed = {}
    for pair in mcp.prefix[1::2]:
        key, _, value = pair.partition("=")
        parsed[key.rsplit(".", 1)[1]] = tomllib.loads(f"v = {value}")["v"]
    assert parsed["command"] == "/opt/bl/venv/bin/python"
    assert parsed["args"] == ARGS
    assert parsed["env"][memory.ENV_READ] == odd[memory.ENV_READ]
    assert parsed["env"][memory.ENV_INBOX] == odd[memory.ENV_INBOX]


def test_opencode_gets_its_config_content_merged_never_replaced():
    operator = {
        "provider": {"x": {"npm": "y"}},
        "mcp": {"theirs": {"type": "remote", "url": "https://example.test"}},
        "default_agent": "battlelab-mission-0123456789abcdef",
    }
    base = {"OPENCODE_CONFIG_CONTENT": json.dumps(operator)}
    mcp = agent_tools.terminal_launch(manifest("opencode-config-content"), PATHS, base)
    assert mcp.prefix == mcp.suffix == ()
    assert list(mcp.env) == ["OPENCODE_CONFIG_CONTENT"]
    merged = json.loads(mcp.env["OPENCODE_CONFIG_CONTENT"])
    assert merged["provider"] == operator["provider"]
    assert merged["default_agent"] == operator["default_agent"]  # a launch's own policy is kept
    assert merged["mcp"]["theirs"] == operator["mcp"]["theirs"]
    assert merged["mcp"]["battlelab_memory"] == {
        "type": "local",
        "command": ["/opt/bl/venv/bin/python", *ARGS],
        "environment": SERVER_ENV,
        "enabled": True,
    }
    # Nothing in the app's environment: the content is ours alone.
    alone = agent_tools.terminal_launch(manifest("opencode-config-content"), PATHS, {})
    assert json.loads(alone.env["OPENCODE_CONFIG_CONTENT"]) == {
        "mcp": {"battlelab_memory": merged["mcp"]["battlelab_memory"]}
    }


@pytest.mark.parametrize("existing", ["{not json", "[1, 2]", '"text"'])
def test_opencode_content_that_is_not_an_object_is_left_alone(existing):
    base = {"OPENCODE_CONFIG_CONTENT": existing}
    assert agent_tools.terminal_launch(manifest("opencode-config-content"), PATHS, base) is (
        agent_tools.NONE
    )


@pytest.mark.parametrize("kind", sorted(kinds.MCP_KINDS))
def test_nothing_is_added_with_memory_off(kind):
    for off in ({}, None):
        mcp = agent_tools.terminal_launch(manifest(kind), off, {"OPENCODE_CONFIG_CONTENT": "{}"})
        assert mcp is agent_tools.NONE and not mcp
        assert mcp.argv(["/bin/x", "a"]) == ["/bin/x", "a"]


def test_nothing_is_added_for_an_engine_without_the_kind():
    for m in (manifest(None), manifest("made-up-kind"), SimpleNamespace(), None):
        assert agent_tools.terminal_launch(m, PATHS, {}) is agent_tools.NONE


def test_api_payloads_match_the_terminal_ones():
    s = agent_tools.server(PATHS)
    assert agent_tools.codex_thread_config(s) == {
        "mcp_servers.battlelab_memory": {
            "command": "/opt/bl/venv/bin/python",
            "args": ARGS,
            "env": SERVER_ENV,
        }
    }
    [entry] = agent_tools.acp_servers(s)
    assert entry["name"] == "battlelab_memory"
    assert entry["command"] == "/opt/bl/venv/bin/python" and entry["args"] == ARGS
    assert entry["env"] == [{"name": k, "value": v} for k, v in SERVER_ENV.items()] + [
        {"name": "OPENCODE_SERVER_PASSWORD", "value": ""}  # the listener password never reaches it
    ]


def test_api_kind_is_only_the_kind_the_protocol_carries():
    assert agent_tools.api_kind("codex-app-server", None) is None
    for adapter, (kind,) in kinds.API_MCP_KINDS.items():
        assert agent_tools.api_kind(adapter, kind) == kind
        for other in kinds.MCP_KINDS - {kind}:
            with pytest.raises(ValueError):
                agent_tools.api_kind(adapter, other)
    for bad in ("", 1, ["claude-mcp-config-flag"]):
        with pytest.raises(ValueError):
            agent_tools.api_kind("claude-stream-json", bad)


def test_every_kind_records_the_cli_version_it_was_proven_against():
    assert set(agent_tools.PROVEN) == kinds.MCP_KINDS
    assert set(kinds.API_MCP_KINDS) == kinds.API_KINDS


# ---- manifests ---------------------------------------------------------------------------------


def test_the_six_first_party_cli_manifests_declare_their_kind_and_no_other_does():
    declared = {
        e: engines.manifest_of(e).tools_mcp
        for e in engines.engine_ids()
        if engines.manifest_of(e) is not None
    }
    assert {e: k for e, k in declared.items() if k is not None} == {
        "claude": "claude-mcp-config-flag",
        "claude-api": "claude-mcp-config-flag",
        "codex": "codex-config-override",
        "codex-api": "codex-config-override",
        "opencode": "opencode-config-content",
        "opencode-api": "opencode-config-content",
    }


def _with_tools(engine: str, block: str) -> str:
    """The first-party manifest's text with its `[tools]` block replaced by ``block``."""
    text = (FIRST_PARTY_DIR / engine / "plugin.toml").read_text()
    head, sep, tail = text.partition("[tools]\nmcp = ")
    if sep:
        text = head + tail.split("\n", 1)[1]
    return text.replace("[display]", f"{block}\n[display]", 1)


def _parse(text: str):
    return load_bytes(text.encode(), name="plugin.toml")


@pytest.mark.parametrize(
    ("engine", "block", "error"),
    [
        ("claude", '[tools]\nmcp = "claude-skill-dir"', "tools.mcp"),
        ("claude", '[tools]\nmcp = "codex-config-override"\nextra = 1', "tools"),
        ("claude-api", '[tools]\nmcp = "codex-config-override"', "tools.mcp"),
        ("codex-api", '[tools]\nmcp = "claude-mcp-config-flag"', "tools.mcp"),
        ("shell", '[tools]\nmcp = "claude-mcp-config-flag"', "tools.mcp"),
    ],
)
def test_the_validator_refuses_an_unknown_or_mismatched_kind(engine, block, error):
    with pytest.raises(ManifestError) as exc:
        _parse(_with_tools(engine, block))
    assert error in str(exc.value)


def test_a_valid_declaration_parses_and_absence_means_none():
    assert _parse(_with_tools("codex", '[tools]\nmcp = "codex-config-override"')).tools_mcp == (
        "codex-config-override"
    )
    assert _parse(_with_tools("gemini", "")).tools_mcp is None
