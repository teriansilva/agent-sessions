"""Per-launch agent tools (#1506): how BattleLab's memory tool reaches an agent CLI.

The memory tool is an MCP server (`agent_sessions.memory_mcp`) the agent CLI starts as its own
child. It is wired in for ONE launch — a flag, a config override or a config environment
variable — so nothing is written into the workspace or into the vendor's own configuration, and
the operator's own MCP servers still load beside it. HOW a CLI takes it is a manifest fact
(`tools.mcp`, a member of `kinds.MCP_KINDS`); nothing here names an engine.

It is added only where `memory.launch_env` gave THIS launch its two memory paths (a new agent
process with memory on): never on an attach, never with memory off. The same launch-only rule as
the paths themselves: turning memory off reaches new processes, not a running one and its MCP
child.

**Shell-free.** The server's command is the app's own interpreter (`sys.executable`, absolute)
with literal arguments, and every payload is built by `json.dumps`, never by string pasting. The
interpreter runs isolated (``-I``): the agent starts it in the WORKSPACE, and without ``-I`` the
current directory would be first on ``sys.path`` — a planted ``json.py`` in a repository would run
inside the tool. Its declared environment is the two memory paths plus ``PATH``/``HOME``/``LANG``.

**A CLI that rejects the injection fails the launch visibly** with its own error; the tool is never
silently dropped and the launch is never retried without it. Each kind below records the CLI
version its exact payload was proven against (the #1506 smoke evidence).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import memory
from .plugins import kinds

log = logging.getLogger(__name__)

#: The server's name in every CLI's MCP table. Namespaced, so an operator's own server of another
#: name is never replaced; the CLIs merge by name and this entry exists for one launch only.
SERVER_NAME = "battlelab_memory"
MODULE = "agent_sessions.memory_mcp"

CLAUDE_FLAG = "claude-mcp-config-flag"  # `--mcp-config <json>`, claude 2.1.304
CODEX_OVERRIDE = "codex-config-override"  # `-c mcp_servers.<name>.*`, codex-cli 0.162.0
OPENCODE_CONTENT = "opencode-config-content"  # `OPENCODE_CONFIG_CONTENT.mcp`, opencode 1.18.35

#: The CLI version each kind's exact payload was proven against (#1506 smoke). A later version that
#: rejects the payload fails its launch with the CLI's own error, never silently.
PROVEN = {
    CLAUDE_FLAG: "claude 2.1.304",
    CODEX_OVERRIDE: "codex-cli 0.162.0",
    OPENCODE_CONTENT: "opencode 1.18.35",
}
assert frozenset(PROVEN) == kinds.MCP_KINDS  # every kind has a shape here, and only those

#: opencode merges this inline JSON config last, over its files (`engines/opencode.py`).
OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


@dataclass(frozen=True)
class Server:
    """The memory tool's command line and declared environment for one launch."""

    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class McpLaunch:
    """What a terminal or unattended launch adds: argv right after the binary (``prefix``) or at
    the end (``suffix``), and environment overrides. Empty = nothing added."""

    prefix: tuple[str, ...] = ()
    suffix: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.prefix or self.suffix or self.env)

    def argv(self, launch: list[str]) -> list[str]:
        """``launch`` (an engine argv, binary first) with this launch's additions."""
        if not self:
            return list(launch)
        return [launch[0], *self.prefix, *launch[1:], *self.suffix]


NONE = McpLaunch()


def mcp_kind(manifest) -> str | None:
    """The manifest's `tools.mcp`, or None (the agent gets the memory variables only)."""
    kind = getattr(manifest, "tools_mcp", None)
    return kind if kind in kinds.MCP_KINDS else None


def server(memory_env: Mapping[str, str] | None) -> Server | None:
    """The memory tool for a launch given ``memory_env``, or None when it got no memory paths.

    Both memory variables or nothing, each an absolute path without NUL or newline — the shape
    `memory.launch_env` returns and `native_worker.memory_environment` validates."""
    if not memory_env:
        return None
    if set(memory_env) != set(memory.ENV_KEYS) or any(
        not isinstance(v, str) or not v.startswith("/") or "\0" in v or "\n" in v
        for v in memory_env.values()
    ):
        log.warning("memory tool: malformed memory environment; not wired in")
        return None
    exe = sys.executable
    if not exe or not exe.startswith("/"):
        log.warning("memory tool: no absolute interpreter path; not wired in")
        return None
    env = {name: memory_env[name] for name in memory.ENV_KEYS}
    env["PATH"] = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = os.environ.get("HOME") or str(Path.home())
    env["LANG"] = os.environ.get("LANG") or "C.UTF-8"
    return Server(command=exe, args=("-I", "-m", MODULE), env=env)


# ---- one shape per kind --------------------------------------------------------------------------


def claude_mcp_config(s: Server) -> str:
    """The JSON string claude's `--mcp-config` takes (a config, not a file path)."""
    entry = {"type": "stdio", "command": s.command, "args": list(s.args), "env": dict(s.env)}
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, ensure_ascii=False)


def _toml(value) -> str:
    """A TOML value for codex's ``-c key=value``: strings, string arrays and string tables only.
    JSON string escapes are TOML basic-string escapes; ``ensure_ascii=False`` keeps non-ASCII
    literal, because TOML has no surrogate-pair escapes."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, Mapping):
        # Keys are fixed variable names (`server` builds them): bare TOML keys.
        assert all(k.replace("_", "").isalnum() and k.isascii() for k in value)
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value).__name__)


def codex_overrides(s: Server) -> tuple[str, ...]:
    """``-c mcp_servers.<name>.command=… -c ….args=… -c ….env=…`` — placed before any subcommand,
    where codex takes its global config overrides."""
    base = f"mcp_servers.{SERVER_NAME}"
    return (
        "-c",
        f"{base}.command={_toml(s.command)}",
        "-c",
        f"{base}.args={_toml(list(s.args))}",
        "-c",
        f"{base}.env={_toml(dict(s.env))}",
    )


def codex_thread_config(s: Server) -> dict:
    """The same server as a codex app-server per-thread ``config`` entry (dotted key → value)."""
    return {
        f"mcp_servers.{SERVER_NAME}": {
            "command": s.command,
            "args": list(s.args),
            "env": dict(s.env),
        }
    }


def opencode_entry(s: Server) -> dict:
    return {
        "type": "local",
        "command": [s.command, *s.args],
        "environment": dict(s.env),
        "enabled": True,
    }


def opencode_config_content(s: Server, existing: str | None) -> str | None:
    """``existing`` `OPENCODE_CONFIG_CONTENT` (the operator's, or a launch's own permission
    policy) with this server added under ``mcp``. None when ``existing`` is not a JSON object —
    it cannot be merged safely, and opencode refuses it on its own."""
    cfg: dict = {}
    if existing is not None and existing.strip():
        try:
            cfg = json.loads(existing)
        except ValueError:
            return None
        if not isinstance(cfg, dict):
            return None
    servers = cfg.get("mcp")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers[SERVER_NAME] = opencode_entry(s)
    return json.dumps({**cfg, "mcp": servers}, ensure_ascii=False)


def acp_servers(s: Server) -> list[dict]:
    """The server as an ACP ``mcpServers`` item (`session/new`, `session/load`). opencode starts
    it with its own environment underneath, so the listener password it holds is blanked here,
    as for every other local MCP server of an API child (#1336)."""
    env = [{"name": k, "value": v} for k, v in s.env.items()]
    env.append({"name": "OPENCODE_SERVER_PASSWORD", "value": ""})
    return [{"name": SERVER_NAME, "command": s.command, "args": list(s.args), "env": env}]


# ---- the seams ----------------------------------------------------------------------------------


def terminal_launch(
    manifest, memory_env: Mapping[str, str] | None, base_env: Mapping[str, str]
) -> McpLaunch:
    """What a terminal LAUNCH or an unattended dispatch adds for the memory tool, or `NONE`.

    ``base_env`` is the environment the agent will otherwise get (a config variable is merged
    into it, never replaced)."""
    kind, s = mcp_kind(manifest), server(memory_env)
    if kind is None or s is None:
        return NONE
    if kind == CLAUDE_FLAG:
        # At the END: `--mcp-config` takes several values, so nothing positional may follow it.
        return McpLaunch(suffix=("--mcp-config", claude_mcp_config(s)))
    if kind == CODEX_OVERRIDE:
        return McpLaunch(prefix=codex_overrides(s))
    content = opencode_config_content(s, base_env.get(OPENCODE_CONFIG_ENV))
    if content is None:
        log.warning("memory tool: %s is not a JSON object; not wired in", OPENCODE_CONFIG_ENV)
        return NONE
    return McpLaunch(env={OPENCODE_CONFIG_ENV: content})


def api_kind(adapter: str, kind: object) -> str | None:
    """A worker's ``memory_tool`` config value, checked: None, or exactly the kind its protocol
    carries (`kinds.API_MCP_KINDS`). Anything else raises `ValueError`."""
    if kind is None:
        return None
    if not isinstance(kind, str) or kind not in kinds.API_MCP_KINDS.get(adapter, ()):
        raise ValueError("invalid memory tool kind")
    return kind
