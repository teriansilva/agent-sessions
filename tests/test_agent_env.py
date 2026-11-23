"""The ONE environment an agent child is started with (#1078).

Before this fix every launch copied the app's whole environment, so an agent could read
`AGENT_SESSIONS_SECRET_KEY` with `env` and mint an admin session. These tests make a real child
dump its own environment and assert the app's secret names are absent, through the helper and
through both probe spawn sites. The launch paths themselves are pinned where their fixtures live
(`tests/test_headless_dispatch.py`, `tests/test_webterm.py`).
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from agent_sessions import agent_env, agent_usage, engine_auth

#: Every name the app reads that is (or names the location of) one of its own secrets. The
#: pattern also drops a name added later, which is why the helper is not an allowlist.
SECRET_NAMES = (
    "AGENT_SESSIONS_SECRET_KEY",
    "AGENT_SESSIONS_PASSWORD_HASH",
    "AGENT_SESSIONS_VAPID_KEYS",
    "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY",
    "AGENT_SESSIONS_RELEASE_KEY",
    "AGENT_SESSIONS_ACTION_SECRETS",
    "AGENT_SESSIONS_PLAYBOOK_SECRETS",
    "AGENT_SESSIONS_SECRETS_RUNTIME",
    # Matched by *PASSWORD*, though the value is a boolean and not a credential: harmless to drop.
    "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE",
)

#: Names an agent may legitimately need. Only the app's own `AGENT_SESSIONS_*` secrets are in
#: scope; a vendor credential is the agent's own and must survive.
KEPT_NAMES = (
    "AGENT_SESSIONS_CLAUDE_BIN",
    "AGENT_SESSIONS_USERNAME",
    "AGENT_SESSIONS_ORIGIN",
    "AGENT_SESSIONS_HOME",
    "ANTHROPIC_API_KEY",
    "PATH",
    "HOME",
    "TERM",
)

_DUMP_ENV = "import os; print('\\n'.join(sorted(os.environ)))"


@pytest.fixture
def secrets_in_env(monkeypatch):
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, f"secret-value-for-{name.lower()}")
    monkeypatch.setenv("AGENT_SESSIONS_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "admin")
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://example.test")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", "/tmp/agent-sessions-home")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "vendor-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp")
    monkeypatch.setenv("TERM", "xterm-256color")
    yield


def test_scrubbed_drops_the_names_the_app_reads_as_secrets_and_keeps_the_agents_own(
    secrets_in_env,
):
    env = agent_env.scrubbed()
    for name in SECRET_NAMES:
        assert name not in env, f"{name} reached a child environment"
    for name in KEPT_NAMES:
        assert name in env, f"{name} was scrubbed although the agent may need it"


def test_scrubbed_scrubs_a_caller_supplied_mapping_without_reading_os_environ(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_SECRET_KEY", raising=False)
    base = {name: "x" for name in SECRET_NAMES}
    base.update({"AGENT_SESSIONS_CLAUDE_BIN": "/bin/claude", "PATH": "/bin"})
    out = agent_env.scrubbed(base)
    assert not [name for name in out if agent_env.is_secret(name)]
    assert out["AGENT_SESSIONS_CLAUDE_BIN"] == "/bin/claude"
    assert out["PATH"] == "/bin"


def test_a_vendor_key_that_merely_ends_in_KEY_is_not_the_apps_secret():
    """The deny rule is scoped to the app's own prefix, so it cannot eat a provider key."""
    assert agent_env.is_secret("AGENT_SESSIONS_SECRET_KEY") is True
    assert agent_env.is_secret("ANTHROPIC_API_KEY") is False
    assert agent_env.is_secret("GITHUB_TOKEN") is False


def test_the_engine_auth_probe_child_never_sees_the_apps_secrets(secrets_in_env):
    """`run_probe` with `env` omitted is the app's own environment, inherited as before."""
    completed, out = engine_auth.run_probe([sys.executable, "-c", _DUMP_ENV])
    assert completed is True
    for name in SECRET_NAMES:
        assert name not in out
    assert "ANTHROPIC_API_KEY" in out  # the agent's own credential still reaches it


def test_run_probe_also_scrubs_an_explicit_environment_built_from_os_environ(secrets_in_env):
    """A provider's preflight builds `{**os.environ, **forced_env(...)}` and hands it in."""
    completed, out = engine_auth.run_probe([sys.executable, "-c", _DUMP_ENV], env=dict(os.environ))
    assert completed is True
    for name in SECRET_NAMES:
        assert name not in out


def test_the_agent_usage_probe_child_never_sees_the_apps_secrets(secrets_in_env):
    """`agent_usage._run` used to pass no `env=`, so the child inherited everything."""
    code, out = agent_usage._run([sys.executable, "-c", _DUMP_ENV])
    assert code == 0
    for name in SECRET_NAMES:
        assert name not in out
    assert "ANTHROPIC_API_KEY" in out


def test_no_module_copies_the_process_environment_at_a_spawn_site():
    """A ratchet: a new raw copy is how this bug would come back, so it fails the suite.

    The two allowed copies build a lookup mapping, never a child environment; their call sites
    do not spawn. Everything that spawns goes through `agent_env.scrubbed()`.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_sessions"
    allowed = {
        "mcp_launch.py": "a settings lookup, never a child environment",
        "plugins/manager.py": "provider env-override lookups, never a child environment",
    }
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("dict(os.environ)", "os.environ.copy()"):
            if needle in text:
                rel = str(path.relative_to(root))
                if rel not in allowed:
                    offenders.append(f"{rel}: {needle}")
    assert offenders == [], (
        "a raw process-environment copy is back; build the child environment with "
        f"agent_env.scrubbed() instead: {offenders}"
    )
