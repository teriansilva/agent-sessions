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

**The skills tool (#1693) rides the same channel.** A launch from a specialist also carries
`battlelab_skills` (`agent_sessions.skills_mcp`), whose ONE declared path is that launch's
read-only skill bundle (`skill_bundles`). Every shape below takes one server or several; each is
keyed by its own name, so the two never collide and the operator's own servers still merge in.

**A CLI that rejects the injection fails the launch visibly** with its own error; the tool is never
silently dropped and the launch is never retried without it. Each kind below records the CLI
version its exact payload was proven against (the #1506 smoke evidence).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import memory
from .plugins import kinds

log = logging.getLogger(__name__)

#: The server's name in every CLI's MCP table. Namespaced, so an operator's own server of another
#: name is never replaced; the CLIs merge by name and this entry exists for one launch only.
SERVER_NAME = "battlelab_memory"
MODULE = "agent_sessions.memory_mcp"
#: The skills tool (#1693): its own name in every MCP table, beside the memory tool.
SKILLS_SERVER_NAME = "battlelab_skills"
SKILLS_MODULE = "agent_sessions.skills_mcp"
#: Its one environment path: this launch's read-only skill bundle.
SKILLS_ENV = "BATTLELAB_SKILLS_BUNDLE"

CLAUDE_FLAG = "claude-mcp-config-flag"  # `--mcp-config <json>`, claude 2.1.304
CODEX_OVERRIDE = "codex-config-override"  # `-c mcp_servers.<name>.*`, codex-cli 0.162.0
OPENCODE_CONTENT = "opencode-config-content"  # `OPENCODE_CONFIG_CONTENT.mcp`, opencode 1.18.35
# `--additional-mcp-config <json>`, copilot 1.0.68 — loads stdio MCP servers into TUI AND
# `--acp` sessions (the ACP `session/new` mcpServers parameter rejects stdio servers, #1565).
COPILOT_FLAG = "copilot-additional-mcp-config"

#: The CLI version each kind's exact payload was proven against (#1506 smoke). A later version that
#: rejects the payload fails its launch with the CLI's own error, never silently.
PROVEN = {
    CLAUDE_FLAG: "claude 2.1.304",
    CODEX_OVERRIDE: "codex-cli 0.162.0",
    OPENCODE_CONTENT: "opencode 1.18.35",
    COPILOT_FLAG: "copilot 1.0.68",
}
assert frozenset(PROVEN) == kinds.MCP_KINDS  # every kind has a shape here, and only those

#: opencode merges this inline JSON config last, over its files (`engines/opencode.py`).
OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


@dataclass(frozen=True)
class Server:
    """One BattleLab tool's command line and declared environment for one launch."""

    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    #: Its key in every CLI's MCP table.
    name: str = SERVER_NAME


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

    Both memory variables or nothing — plus, for a session started from a specialist, both of
    that specialist's notebook variables (#1693 S3) — each an absolute path without NUL or
    newline: the shape `memory.launch_env` returns and `native_worker.memory_environment`
    validates."""
    if not memory_env:
        return None
    if set(memory_env) not in (set(memory.ENV_KEYS), set(memory.ALL_ENV_KEYS)) or any(
        not isinstance(v, str) or not v.startswith("/") or "\0" in v or "\n" in v
        for v in memory_env.values()
    ):
        log.warning("memory tool: malformed memory environment; not wired in")
        return None
    exe = _interpreter("memory tool")
    if exe is None:
        return None
    env = {name: memory_env[name] for name in memory.ALL_ENV_KEYS if name in memory_env}
    return Server(command=exe, args=("-I", "-m", MODULE), env=_with_base_env(env))


def skills_server(bundle: str | None) -> Server | None:
    """The skills tool for a launch bound to ``bundle`` (an absolute path, `skill_bundles.build`),
    or None when the launch carries no skills."""
    if bundle is None:
        return None
    if (
        not isinstance(bundle, str)
        or not bundle.startswith("/")
        or "\0" in bundle
        or "\n" in bundle
    ):
        raise ValueError("invalid skill bundle path")
    from . import skill_bundles

    try:
        skill_bundles.verify(bundle)  # a bundle BattleLab built, never any other directory
    except skill_bundles.BundleRefused as exc:
        raise ValueError(str(exc)) from None
    exe = _interpreter("skills tool")
    if exe is None:
        raise ValueError("no absolute interpreter path for the skills tool")
    return Server(
        command=exe,
        args=("-I", "-m", SKILLS_MODULE),
        env=_with_base_env({SKILLS_ENV: bundle}),
        name=SKILLS_SERVER_NAME,
    )


def _interpreter(what: str) -> str | None:
    exe = sys.executable
    if not exe or not exe.startswith("/"):
        log.warning("%s: no absolute interpreter path; not wired in", what)
        return None
    return exe


def _with_base_env(env: dict[str, str]) -> dict[str, str]:
    env["PATH"] = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = os.environ.get("HOME") or str(Path.home())
    env["LANG"] = os.environ.get("LANG") or "C.UTF-8"
    return env


def _all(s: Server | Sequence[Server]) -> list[Server]:
    return [s] if isinstance(s, Server) else list(s)


# ---- one shape per kind --------------------------------------------------------------------------


def claude_mcp_config(servers: Server | Sequence[Server]) -> str:
    """The JSON string claude's `--mcp-config` takes (a config, not a file path)."""
    entries = {
        s.name: {"type": "stdio", "command": s.command, "args": list(s.args), "env": dict(s.env)}
        for s in _all(servers)
    }
    return json.dumps({"mcpServers": entries}, ensure_ascii=False)


def _toml(value) -> str:
    """A TOML value for codex's ``-c key=value``: strings, string arrays and string tables only.
    JSON string escapes are TOML basic-string escapes; ``ensure_ascii=False`` keeps non-ASCII
    literal, because TOML has no surrogate-pair escapes."""
    if isinstance(value, str):
        # JSON leaves DEL (U+007F) raw; TOML forbids it in a basic string.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, Mapping):
        # Keys are fixed variable names (`server` builds them): bare TOML keys.
        assert all(k.replace("_", "").isalnum() and k.isascii() for k in value)
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value).__name__)


def codex_overrides(servers: Server | Sequence[Server]) -> tuple[str, ...]:
    """``-c mcp_servers.<name>.command=… -c ….args=… -c ….env=…`` per server — placed before any
    subcommand, where codex takes its global config overrides."""
    out: list[str] = []
    for s in _all(servers):
        base = f"mcp_servers.{s.name}"
        out += [
            "-c",
            f"{base}.command={_toml(s.command)}",
            "-c",
            f"{base}.args={_toml(list(s.args))}",
            "-c",
            f"{base}.env={_toml(dict(s.env))}",
        ]
    return tuple(out)


def codex_thread_config(servers: Server | Sequence[Server]) -> dict:
    """The same servers as codex app-server per-thread ``config`` entries (dotted key → value)."""
    return {
        f"mcp_servers.{s.name}": {"command": s.command, "args": list(s.args), "env": dict(s.env)}
        for s in _all(servers)
    }


def opencode_entry(s: Server) -> dict:
    return {
        "type": "local",
        "command": [s.command, *s.args],
        "environment": dict(s.env),
        "enabled": True,
    }


def opencode_config_content(servers: Server | Sequence[Server], existing: str | None) -> str | None:
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
    table = cfg.get("mcp")
    table = dict(table) if isinstance(table, dict) else {}
    for s in _all(servers):
        table[s.name] = opencode_entry(s)
    return json.dumps({**cfg, "mcp": table}, ensure_ascii=False)


# The exact tool names each first-party server exposes — memory_mcp.TOOLS / skills_mcp.TOOLS,
# pinned against those modules by tests/test_agent_tools.py. Never a wildcard: copilot's own
# default for a server is already "all tools", so only a named list is a real boundary, and a
# new server fails closed here until its tools are named.
_COPILOT_SERVER_TOOLS: dict[str, list[str]] = {
    SERVER_NAME: ["memory_index", "memory_read", "memory_search", "memory_write"],
    SKILLS_SERVER_NAME: ["skill_read", "skills_list"],
}


def copilot_mcp_config(servers: Server | Sequence[Server]) -> str:
    """The JSON string copilot's `--additional-mcp-config` takes (#1565): the same stdio
    ``mcpServers`` table as claude's flag, with each server's tools NAMED (never ``["*"]``).
    A server without a named list raises — the grant stays exactly as wide as intended."""
    entries = {}
    for s in _all(servers):
        tools = _COPILOT_SERVER_TOOLS.get(s.name)
        if tools is None:
            raise ValueError(f"copilot MCP config: no named tool list for server {s.name!r}")
        entries[s.name] = {
            "type": "stdio",
            "command": s.command,
            "args": list(s.args),
            "env": dict(s.env),
            "tools": list(tools),
        }
    return json.dumps({"mcpServers": entries}, ensure_ascii=False)


def acp_servers(servers: Server | Sequence[Server]) -> list[dict]:
    """The servers as ACP ``mcpServers`` items (`session/new`, `session/load`). opencode starts
    them with its own environment underneath, so the listener password it holds is blanked here,
    as for every other local MCP server of an API child (#1336)."""
    out = []
    for s in _all(servers):
        env = [{"name": k, "value": v} for k, v in s.env.items()]
        env.append({"name": "OPENCODE_SERVER_PASSWORD", "value": ""})
        out.append({"name": s.name, "command": s.command, "args": list(s.args), "env": env})
    return out


# ---- the seams ----------------------------------------------------------------------------------


class SkillsRefused(ValueError):
    """A launch bound to skills that cannot carry them. Refused, never launched without them."""


def terminal_launch(
    manifest,
    memory_env: Mapping[str, str] | None,
    base_env: Mapping[str, str],
    *,
    skills_bundle: str | None = None,
) -> McpLaunch:
    """What a terminal LAUNCH or an unattended dispatch adds for BattleLab's tools, or `NONE`.

    ``base_env`` is the environment the agent will otherwise get (a config variable is merged
    into it, never replaced). The memory tool is best-effort (memory still reaches the agent as
    variables); the skills tool is not: a launch bound to a skill bundle that this agent cannot
    carry raises `SkillsRefused`, so a specialist never starts without the skills it was saved
    with."""
    kind = mcp_kind(manifest)
    try:
        skills = skills_server(skills_bundle)
    except ValueError as exc:
        raise SkillsRefused(str(exc)) from None
    if skills is not None and kind is None:
        raise SkillsRefused("this agent cannot load skills: it has no tool channel")
    servers = [x for x in (server(memory_env), skills) if x is not None]
    if kind is None or not servers:
        return NONE
    if kind == CLAUDE_FLAG:
        # At the END: `--mcp-config` takes several values, so nothing positional may follow it.
        return McpLaunch(suffix=("--mcp-config", claude_mcp_config(servers)))
    if kind == CODEX_OVERRIDE:
        return McpLaunch(prefix=codex_overrides(servers))
    if kind == COPILOT_FLAG:
        # A flag with ONE value, safe at the end: copilot's launch argv carries no positional.
        return McpLaunch(suffix=("--additional-mcp-config", copilot_mcp_config(servers)))
    content = opencode_config_content(servers, base_env.get(OPENCODE_CONFIG_ENV))
    if content is None:
        if skills is not None:
            raise SkillsRefused(
                f"{OPENCODE_CONFIG_ENV} is not a JSON object, so the skills tool cannot be added"
            )
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
