"""A managed agent never updates itself: BattleLab's catalog owns its version.

2026-11-19: a BattleLab-launched, MANAGED claude (a pinned, digest-verified generation under the
plugins home) ran Claude Code's own updater, which did `npm install -g @anthropic-ai/claude-code`
per `~/.claude.json` and removed the operator's `~/.local/bin/claude`. Every process BattleLab
starts from a managed entrypoint now carries the vendor's documented "no self-update" switch
(`plugins.provider.self_update_off`); an adopted binary — the operator's own CLI — does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_plugins as plugins
from agent_sessions import agent_usage, native_models, native_runtime
from agent_sessions.engines import claude, opencode2
from agent_sessions.plugins import PluginProvider, kinds, process, provenance
from agent_sessions.plugins.provider import forced_env, isolation_env, self_update_off

OFF = {"DISABLE_AUTOUPDATER": "1"}
CATALOG = Path(__file__).parent.parent / "src" / "agent_sessions" / "agent_catalog.json"


def _claude_doc() -> dict:
    d = plugins.doc("claude")
    d["install"] = plugins._install(
        package="@anthropic-ai/claude-code-linux-x64", entrypoint="bin/claude"
    )
    return d


def managed(tmp_path: Path, *, env=None) -> PluginProvider:
    """A managed claude generation, laid out and recorded as the plugin manager does."""
    m = plugins.parse(_claude_doc())
    root = tmp_path / "plugins" / "claude" / "generations" / "g1"
    ep = plugins.exe(root / "bin" / "claude", b"#!/bin/true\n#managed\n")
    rec = provenance.Record(
        install_entrypoint="bin/claude", install_sha256=plugins.sha(ep), manifest_sha256=m.digest
    )
    prov = PluginProvider(
        m, trust=provenance.LOCAL, root=root, env=env or {}, home=tmp_path, record=rec
    )
    return prov.attach_kind(claude.ClaudeProvider())


def adopted(tmp_path: Path, *, env=None, state_dir=None) -> PluginProvider:
    """The in-tree claude finding the operator's own CLI on its search path."""
    plugins.exe(tmp_path / ".local" / "bin" / "claude")
    prov = PluginProvider(
        plugins.parse(_claude_doc()),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "in-tree",
        env=env or {},
        home=tmp_path,
        state_dir=state_dir,
    )
    return prov.attach_kind(claude.ClaudeProvider())


# --- who gets the switch --------------------------------------------------------------------------


def test_a_managed_entrypoint_gets_the_switch(tmp_path):
    prov = managed(tmp_path)
    assert prov.entrypoint().state == provenance.MANAGED
    assert self_update_off(prov) == OFF
    assert forced_env(prov) == OFF


def test_an_adopted_binary_keeps_its_vendor_behaviour(tmp_path):
    prov = adopted(tmp_path)
    assert prov.entrypoint().state == provenance.ADOPTED
    assert self_update_off(prov) == {} and forced_env(prov) == {}


def test_an_env_override_outside_the_root_is_adopted_and_gets_no_switch(tmp_path):
    """The operator pinned a vendor CLI over a recorded install: theirs, so theirs to update."""
    state = tmp_path / "state"
    installed = plugins.exe(tmp_path / "in-tree" / "bin" / "claude", b"#!/bin/true\n#rec\n")
    elsewhere = plugins.exe(tmp_path / "vendor" / "claude")
    probe = adopted(tmp_path, state_dir=state)
    plugins.write_record(
        probe, install_entrypoint="bin/claude", install_sha256=plugins.sha(installed)
    )
    assert adopted(tmp_path, state_dir=state).entrypoint().state == provenance.MANAGED
    assert self_update_off(adopted(tmp_path, state_dir=state)) == OFF
    pinned = adopted(tmp_path, state_dir=state, env={"AGENT_SESSIONS_CLAUDE_BIN": str(elsewhere)})
    assert pinned.entrypoint().state == provenance.ADOPTED
    assert self_update_off(pinned) == {}


def test_a_refused_entrypoint_asks_for_nothing_and_the_launch_reports_it(tmp_path):
    prov = managed(tmp_path)
    (prov.root / "bin" / "claude").write_bytes(b"#!/bin/true\n#tampered\n")
    assert self_update_off(prov) == {}
    with pytest.raises(Exception, match="no longer matches"):
        prov.entrypoint_path()


def test_the_isolation_tree_wins_over_the_switch(tmp_path, monkeypatch):
    """`isolation_env` is laid last; it alone decides whether an agent is isolated."""

    class Kind:
        def agent_env(self):
            return {"SHARED": "isolation", "XDG_DATA_HOME": "/tree"}

    monkeypatch.setitem(
        kinds.SELF_UPDATE_OFF, "claude-projects", {"DISABLE_AUTOUPDATER": "1", "SHARED": "update"}
    )
    prov = managed(tmp_path)
    prov.kind = Kind()
    assert forced_env(prov) == {
        "DISABLE_AUTOUPDATER": "1",
        "SHARED": "isolation",
        "XDG_DATA_HOME": "/tree",
    }
    assert isolation_env(prov) == {"SHARED": "isolation", "XDG_DATA_HOME": "/tree"}
    assert isolation_env(managed(tmp_path / "plain")) == {}  # the switch is never isolation


# --- the audit, pinned ----------------------------------------------------------------------------


def test_each_catalog_agents_switch_is_pinned():
    """The audit: every catalog agent with a binary either has its vendor's environment switch,
    turns its updater off through its manifest (copilot), or is named here as having none."""
    assert kinds.SELF_UPDATE_OFF == {
        "claude-projects": {"DISABLE_AUTOUPDATER": "1"},
        "opencode-sqlite": {"OPENCODE_DISABLE_AUTOUPDATE": "1"},
        "opencode2-sqlite": {"OPENCODE_DISABLE_AUTOUPDATE": "1"},
        "antigravity-cli": {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
    }
    doc = json.loads(CATALOG.read_text())
    layouts = {
        a["manifest"]["store"]["layout"]: a["manifest"]["identity"]["id"]
        for a in doc["agents"]
        if a["manifest"].get("binary")
    }
    # codex only prompts; gemini and kimi have only a settings file in the operator's config.
    no_env_switch = {"codex-rollouts", "gemini-tmp", "kimi-code", "copilot-sessions"}
    assert set(layouts) == set(kinds.SELF_UPDATE_OFF) | no_env_switch
    for layout in kinds.SELF_UPDATE_OFF:
        assert layout in kinds.STORE_LAYOUTS


def test_copilot_turns_its_updater_off_on_every_launch_through_its_manifest():
    doc = json.loads(CATALOG.read_text())
    [m] = [a["manifest"] for a in doc["agents"] if a["manifest"]["identity"]["id"] == "copilot"]
    assert "--no-auto-update" in m["launch"]["base_args"]


# --- every spawn seam BattleLab owns --------------------------------------------------------------


def test_the_console_launch_carries_it(monkeypatch, tmp_path):
    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    observed = []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop at the spawn boundary")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            pass

    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key="claude:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
            launch_provider=managed(tmp_path),
        )
    )
    [env] = observed
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_sign_in_version_and_verification_carry_it_after_env_i(tmp_path):
    argv = process._wrapped(
        ["/x/claude", "--version"],
        Path("/tmp"),
        "u.service",
        5,
        agent_env=forced_env(managed(tmp_path)),
    )
    tail = argv[argv.index("/usr/bin/env") :]
    assert tail[1] == "-i" and "DISABLE_AUTOUPDATER=1" in tail


def test_the_api_worker_child_carries_it(tmp_path):
    """Readiness `--version` and the worker's child both take `source_environment`; the worker
    lays the config's `forced_env` last over its child's environment."""
    from agent_sessions import native_worker

    forced = native_runtime.source_environment("claude-stream-json", managed(tmp_path))
    assert forced == OFF
    assert native_worker.forced_environment({"forced_env": forced}) == OFF
    # Only the exact switch rides beside the tree; anything else over the child is refused.
    for bad in ({"DISABLE_AUTOUPDATER": "0"}, {"LD_PRELOAD": "/x.so"}):
        with pytest.raises(native_worker.WorkerError):
            native_worker.forced_environment({"forced_env": bad})
    tree = {k: f"/t/{k}" for k in opencode2.XDG_DIRS}
    both = {**tree, "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    assert (
        native_worker.forced_environment({"forced_env": both, "adapter": "opencode2-acp"}) == both
    )
    # The switch alone is no isolation: an opencode 2 child without its tree is still refused.
    with pytest.raises(native_worker.WorkerError, match="no isolated environment"):
        native_worker.forced_environment(
            {"forced_env": {"OPENCODE_DISABLE_AUTOUPDATE": "1"}, "adapter": "opencode2-acp"}
        )


def test_an_isolated_adapter_needs_its_tree_not_just_the_switch(monkeypatch, tmp_path):
    prov = managed(tmp_path)
    monkeypatch.setattr(native_runtime, "_ISOLATED", frozenset({"claude-stream-json"}))
    with pytest.raises(native_runtime.NativeError, match="isolated tree"):
        native_runtime.source_environment("claude-stream-json", prov)


def test_the_model_probe_carries_it(monkeypatch, tmp_path):
    seen = []

    def popen(argv, **kwargs):
        seen.append(kwargs["env"])
        raise OSError("stop at the spawn boundary")

    monkeypatch.setattr(subprocess, "Popen", popen)
    target = native_models._Target(("k",), native_models._claude, "/x/claude", managed(tmp_path))
    assert native_models._probe(target).status == "unavailable"
    [env] = seen
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert native_models._SPAWN_ENV.get() is None  # reset after the probe
    # An adopted source's probe environment is exactly what it was.
    seen.clear()
    target = native_models._Target(("k2",), native_models._claude, "/x/claude", adopted(tmp_path))
    native_models._probe(target)
    assert "DISABLE_AUTOUPDATER" not in seen[0]


def test_the_usage_probe_carries_it(monkeypatch, tmp_path):
    seen = []

    def popen(argv, **kwargs):
        seen.append(kwargs.get("env"))
        raise OSError("stop at the spawn boundary")

    import contextlib

    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        agent_usage.admission,
        "acquire",
        lambda prov: contextlib.nullcontext(SimpleNamespace(reason=None)),
    )
    for prov, want in ((managed(tmp_path), "1"), (adopted(tmp_path), None)):
        token = agent_usage._PROBE_PROVIDER.set(prov)
        try:
            assert agent_usage._run(["/x/claude", "-p", "/usage"])[0] == 127
        finally:
            agent_usage._PROBE_PROVIDER.reset(token)
        env = seen.pop()
        got = (env if env is not None else dict(os.environ)).get("DISABLE_AUTOUPDATER")
        assert got == want
