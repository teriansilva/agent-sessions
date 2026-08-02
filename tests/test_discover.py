"""Engine autodiscovery (#65 Phase 3): precedence + env-file rewrite."""

from __future__ import annotations

import stat
from pathlib import Path

from agent_sessions import discover


def _make_exec(path: Path) -> str:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_resolve_precedence_explicit_env_first(tmp_path, monkeypatch):
    monkeypatch.setattr(discover.shutil, "which", lambda _n: "/usr/bin/claude")
    explicit = _make_exec(tmp_path / "claude")
    assert discover.resolve("claude", {"AGENT_SESSIONS_CLAUDE_BIN": explicit}) == explicit


def test_resolve_broken_explicit_falls_through_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(discover.shutil, "which", lambda _n: "/usr/bin/claude")
    bogus = str(tmp_path / "missing")  # not executable
    assert discover.resolve("claude", {"AGENT_SESSIONS_CLAUDE_BIN": bogus}) == "/usr/bin/claude"


def test_resolve_path_then_known_dir(tmp_path, monkeypatch):
    # PATH wins over known dirs…
    monkeypatch.setattr(discover.shutil, "which", lambda _n: "/usr/bin/opencode")
    assert discover.resolve("opencode", {}) == "/usr/bin/opencode"
    # …and with nothing on PATH, a known dir is used.
    monkeypatch.setattr(discover.shutil, "which", lambda _n: None)
    monkeypatch.setattr(discover, "_DIRS", {"opencode": [str(tmp_path)]})
    found = _make_exec(tmp_path / "opencode")
    assert discover.resolve("opencode", {}) == found


def test_resolve_codex_checks_npm_global_bin(tmp_path, monkeypatch):
    """Codex commonly comes from npm, same as Gemini; doctor must pin its absolute path."""
    npm_prefix = tmp_path / "npm"
    (npm_prefix / "bin").mkdir(parents=True)
    found = _make_exec(npm_prefix / "bin" / "codex")

    monkeypatch.setattr(discover.shutil, "which", lambda n: "/usr/bin/npm" if n == "npm" else None)
    monkeypatch.setattr(discover, "_DIRS", {**discover._DIRS, "codex": ["/nonexistent"]})
    monkeypatch.setattr(
        discover.subprocess,
        "run",
        lambda *a, **kw: type("Result", (), {"returncode": 0, "stdout": str(npm_prefix)})(),
    )

    assert discover.resolve("codex", {}) == found


def test_resolve_not_found_is_none(monkeypatch):
    monkeypatch.setattr(discover.shutil, "which", lambda _n: None)
    monkeypatch.setattr(discover, "_DIRS", {"codex": ["/nonexistent"]})
    assert discover.resolve("codex", {}) is None


def test_antigravity_env_knob_keys_on_agy_binary(tmp_path, monkeypatch):
    # antigravity's binary is `agy`, not `antigravity`: the env knob is AGENT_SESSIONS_AGY_BIN,
    # and the PATH probe looks up `agy`. Keeps base.AGY_BIN and doctor's written line in sync.
    assert discover.envvar("antigravity") == "AGENT_SESSIONS_AGY_BIN"
    explicit = _make_exec(tmp_path / "agy")
    assert discover.resolve("antigravity", {"AGENT_SESSIONS_AGY_BIN": explicit}) == explicit
    monkeypatch.setattr(discover.shutil, "which", lambda n: "/usr/bin/agy" if n == "agy" else None)
    assert discover.resolve("antigravity", {}) == "/usr/bin/agy"


def test_antigravity_known_dir_and_not_npm_global(tmp_path, monkeypatch):
    # nothing on PATH -> the ~/.local/bin probe finds `agy`; antigravity is NOT npm-global, so the
    # npm prefix is never consulted (the curl installer drops a single binary in ~/.local/bin).
    monkeypatch.setattr(discover.shutil, "which", lambda _n: None)
    monkeypatch.setattr(discover, "_DIRS", {**discover._DIRS, "antigravity": [str(tmp_path)]})

    def _boom(*_a, **_kw):
        raise AssertionError("npm prefix must not be consulted for antigravity")

    monkeypatch.setattr(discover, "_npm_global_bin", _boom)
    found = _make_exec(tmp_path / "agy")
    assert discover.resolve("antigravity", {}) == found
    assert "antigravity" not in discover._NPM_GLOBAL_ENGINES


def test_kimi_env_knob_and_install_dir(tmp_path, monkeypatch):
    # kimi's binary name matches its engine id, so the knob is the plain AGENT_SESSIONS_KIMI_BIN
    # (no _BIN_NAME entry needed) and the PATH probe looks up `kimi`.
    assert discover.envvar("kimi") == "AGENT_SESSIONS_KIMI_BIN"
    # The installer's own dir must be probed — a service started before the shell rc was
    # re-sourced won't see ~/.kimi-code/bin on PATH.
    assert "~/.kimi-code/bin" in discover._DIRS["kimi"]
    explicit = _make_exec(tmp_path / "kimi")
    assert discover.resolve("kimi", {"AGENT_SESSIONS_KIMI_BIN": explicit}) == explicit
    monkeypatch.setattr(
        discover.shutil, "which", lambda n: "/usr/bin/kimi" if n == "kimi" else None
    )
    assert discover.resolve("kimi", {}) == "/usr/bin/kimi"


def test_kimi_known_dir_and_not_npm_global(tmp_path, monkeypatch):
    # The curl installer drops a single native binary in ~/.kimi-code/bin and only appends that dir
    # to the shell rc, so the dir probe is what usually resolves it when PATH is stale. kimi is NOT
    # npm-global: upstream ships an npm fallback for musl, but its path is unverified (#714), so
    # the npm prefix must never be consulted.
    monkeypatch.setattr(discover.shutil, "which", lambda _n: None)
    monkeypatch.setattr(discover, "_DIRS", {**discover._DIRS, "kimi": [str(tmp_path)]})

    def _boom(*_a, **_kw):
        raise AssertionError("npm prefix must not be consulted for kimi")

    monkeypatch.setattr(discover, "_npm_global_bin", _boom)
    found = _make_exec(tmp_path / "kimi")
    assert discover.resolve("kimi", {}) == found
    assert "kimi" not in discover._NPM_GLOBAL_ENGINES


def test_write_env_bins_preserves_others_and_is_0600(tmp_path):
    env = tmp_path / "env"
    env.write_text("AGENT_SESSIONS_USERNAME=admin\nAGENT_SESSIONS_CLAUDE_BIN=/old/claude\n")
    discover.write_env_bins(env, {"claude": "/new/claude", "codex": "/bin/codex", "opencode": None})
    text = env.read_text()
    assert "AGENT_SESSIONS_USERNAME=admin" in text  # untouched
    assert "AGENT_SESSIONS_CLAUDE_BIN=/new/claude" in text  # updated
    assert "AGENT_SESSIONS_CODEX_BIN=/bin/codex" in text  # added
    assert "OPENCODE_BIN" not in text  # not found → not written
    assert "/old/claude" not in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_doctor_cli_writes_bins(tmp_path, monkeypatch):
    from agent_sessions import cli

    env = tmp_path / "env"
    env.write_text("AGENT_SESSIONS_SECRET_KEY=x\n")
    monkeypatch.setattr(
        discover,
        "discover",
        lambda env=None: {
            "claude": "/usr/bin/claude",
            "opencode": None,
            "codex": None,
            "gemini": None,
            "antigravity": None,
            "kimi": None,
            "shell": None,
        },
    )
    rc = cli.main(["doctor", "--env", str(env)])
    assert rc == 0
    text = env.read_text()
    assert "AGENT_SESSIONS_SECRET_KEY=x" in text
    assert "AGENT_SESSIONS_CLAUDE_BIN=/usr/bin/claude" in text


def test_doctor_cli_dry_run_does_not_write(tmp_path, monkeypatch):
    from agent_sessions import cli

    env = tmp_path / "env"
    env.write_text("AGENT_SESSIONS_SECRET_KEY=x\n")
    monkeypatch.setattr(
        discover,
        "discover",
        lambda env=None: {
            "claude": "/usr/bin/claude",
            "opencode": None,
            "codex": None,
            "gemini": None,
            "antigravity": None,
            "kimi": None,
            "shell": None,
        },
    )
    cli.main(["doctor", "--env", str(env), "--dry-run"])
    assert "CLAUDE_BIN" not in env.read_text()


def test_shell_engine_env_knob_keys_on_bash_binary(tmp_path, monkeypatch):
    # The shell engine's binary is `bash`, not `shell` (#636): the env knob is
    # AGENT_SESSIONS_BASH_BIN and the PATH probe looks up `bash`, matching base.BASH_BIN.
    assert "shell" in discover.ENGINES
    assert discover.envvar("shell") == "AGENT_SESSIONS_BASH_BIN"
    explicit = _make_exec(tmp_path / "bash")
    assert discover.resolve("shell", {"AGENT_SESSIONS_BASH_BIN": explicit}) == explicit
    monkeypatch.setattr(
        discover.shutil, "which", lambda n: "/usr/bin/bash" if n == "bash" else None
    )
    assert discover.resolve("shell", {}) == "/usr/bin/bash"
    assert "shell" not in discover._NPM_GLOBAL_ENGINES
