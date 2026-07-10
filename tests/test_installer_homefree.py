"""Installer tests for the Home Free stream channel (#27).

Source install.sh's shell functions (minus `main`) and exercise them directly,
following the pattern used by the existing installer tests.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"

# The relay's console-name rule (relay/registry.py NAME_RE).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}[a-z0-9]$")

_LEAK_KEYS = (
    "AGENT_SESSIONS_HOST",
    "AGENT_SESSIONS_ORIGIN",
    "AGENT_SESSIONS_PORT",
    "AGENT_SESSIONS_ASSUME_YES",
    "AGENT_SESSIONS_REMOTE",
    "AGENT_SESSIONS_RELAY_URL",
)


def _sourceable(tmp_path: Path) -> Path:
    # install.sh with its `main "$@"` invocation removed so functions can be sourced.
    src = INSTALL_SH.read_text().replace('\nmain "$@"\n', "\n:\n")
    out = tmp_path / "install_src.sh"
    out.write_text(src)
    return out


def _run(snippet: str, tmp_path: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in _LEAK_KEYS}
    base.update(env or {})
    src = _sourceable(tmp_path)
    return subprocess.run(
        ["sh", "-c", f'. "{src}"; {snippet}'],
        capture_output=True,
        text=True,
        env=base,
    )


def _run_stdout_pty(
    snippet: str, tmp_path: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in _LEAK_KEYS}
    base.update(env or {})
    src = _sourceable(tmp_path)
    master_fd, slave_fd = os.openpty()
    try:
        proc = subprocess.Popen(
            ["sh", "-c", f'. "{src}"; {snippet}'],
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            env=base,
        )
        os.close(slave_fd)
        slave_fd = -1
        chunks = []
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        return subprocess.CompletedProcess(
            proc.args,
            proc.wait(),
            b"".join(chunks).decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)


def test_generated_name_matches_relay_rule(tmp_path):
    for _ in range(6):
        r = _run("homefree_gen_name", tmp_path)
        assert r.returncode == 0, r.stderr
        assert NAME_RE.match(r.stdout.strip()), r.stdout


def test_generated_key_has_enough_entropy(tmp_path):
    r = _run("homefree_gen_key", tmp_path)
    key = r.stdout.strip()
    assert re.fullmatch(r"[a-z0-9]+", key), key  # base32-lower or hex fallback
    assert len(key) >= 26  # >=128 bits (32 base32 chars = 160 bits; hex fallback = 40)


def test_selfhost_is_default_when_non_interactive(tmp_path):
    home = tmp_path / "home"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "AGENT_SESSIONS_NO_SERVICE": "1",
    }
    # No AGENT_SESSIONS_REMOTE + no tty (stdin from /dev/null) → self-host, no relay touched.
    r = _run("homefree_maybe_setup </dev/null", tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert not (home / "homefree").exists()


def test_stream_mode_writes_0600_creds_and_unit(tmp_path):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(cfg),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
        "AGENT_SESSIONS_RELAY_URL": "wss://box.example/relay/ws",
    }
    r = _run("homefree_maybe_setup </dev/null", tmp_path, env)
    assert r.returncode == 0, r.stderr

    hf = home / "homefree"
    name_f = hf / "console_name"
    key_f = hf / "access_key"
    assert name_f.exists() and key_f.exists()
    assert stat.S_IMODE(name_f.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_f.stat().st_mode) == 0o600
    assert NAME_RE.match(name_f.read_text().strip())

    unit = (cfg / "systemd" / "user" / "agent-sessions-homefree.service").read_text()
    assert "ExecStart=" in unit and "python -m agent_sessions.homefree" in unit
    assert "wss://box.example/relay/ws" in unit
    # the credentials + anti-scam warning are shown to the user
    assert "Console name:" in r.stdout and "Access key:" in r.stdout
    assert "Never enter it for anyone who contacted you" in r.stdout


def test_stream_mode_defaults_to_battlelab_relay(tmp_path):
    # Turnkey: with no AGENT_SESSIONS_RELAY_URL, stream mode targets the BattleLab public
    # relay and prints the public connect URL — no placeholder, no env var needed.
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(cfg),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
    }
    r = _run("homefree_maybe_setup </dev/null", tmp_path, env)
    assert r.returncode == 0, r.stderr
    unit = (cfg / "systemd" / "user" / "agent-sessions-homefree.service").read_text()
    assert "wss://relay.battlelab.superstatus.io/relay/ws" in unit
    assert "REPLACE-WITH-YOUR-RELAY" not in unit
    assert "https://battlelab.superstatus.io/connect" in r.stdout


def test_stream_credentials_tty_output_uses_real_escape_bytes(tmp_path):
    r = _run_stdout_pty("homefree_print_credentials atlas-2471 key123", tmp_path)
    assert r.returncode == 0, r.stderr
    assert "\\033[" not in r.stdout
    assert "\x1b[1mConnect at:" in r.stdout
    assert "\x1b[1;31m* SECURITY:" in r.stdout
    assert "https://battlelab.superstatus.io/connect" in r.stdout


def test_structural_invariants():
    s = INSTALL_SH.read_text()
    assert "AGENT_SESSIONS_REMOTE" in s
    assert "$APP-homefree.service" in s
    assert "python -m agent_sessions.homefree" in s
    assert "FULL CONTROL of this machine" in s  # anti-scam copy
    # a plain curl|sh must not default to contacting a relay
    assert "_mode=selfhost" in s


def test_stream_appmode_loopback_sets_auth_none_and_app_port(tmp_path):
    """Loopback bind (default) → option A: the box app becomes AUTH_MODE=none (no login
    prompt), the Home Free unit carries HOMEFREE_APP_PORT, and the auth change is disclosed."""
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(cfg),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
        "AGENT_SESSIONS_RELAY_URL": "wss://box.example/relay/ws",
        # HOST defaults to 127.0.0.1 → loopback → app-mode
    }
    snippet = (
        'mkdir -p "$PREFIX"; '
        'printf "AGENT_SESSIONS_PASSWORD_HASH=x\\n" > "$ENVF"; '
        'printf "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1\\n" >> "$ENVF"; '
        "homefree_maybe_setup </dev/null"
    )
    r = _run(snippet, tmp_path, env)
    assert r.returncode == 0, r.stderr

    unit = (cfg / "systemd" / "user" / "agent-sessions-homefree.service").read_text()
    assert "HOMEFREE_APP_PORT=8765" in unit  # agent reverse-proxies the box app

    envf = (home / "env").read_text()
    assert "AGENT_SESSIONS_AUTH_MODE=none" in envf  # option A: single access-key gate
    assert "FORCE_PASSWORD_CHANGE" not in envf  # dropped (inert under AUTH_MODE=none)
    assert "FULL-APP mode" in r.stdout  # the auth handoff is disclosed


def test_stream_non_loopback_refuses_homefree(tmp_path):
    """A non-loopback bind cannot use app-only streaming, so Home Free is not enabled."""
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(cfg),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
        "AGENT_SESSIONS_RELAY_URL": "wss://box.example/relay/ws",
        "AGENT_SESSIONS_HOST": "0.0.0.0",  # exposed → NOT loopback
        "AGENT_SESSIONS_ORIGIN": "http://box.example",
    }
    snippet = (
        'mkdir -p "$PREFIX"; '
        'printf "AGENT_SESSIONS_PASSWORD_HASH=x\\n" > "$ENVF"; '
        'printf "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1\\n" >> "$ENVF"; '
        "homefree_maybe_setup </dev/null"
    )
    r = _run(snippet, tmp_path, env)
    assert r.returncode != 0
    assert "requires AGENT_SESSIONS_HOST=127.0.0.1" in r.stderr
    assert not (cfg / "systemd" / "user" / "agent-sessions-homefree.service").exists()

    envf = (home / "env").read_text()
    assert "AGENT_SESSIONS_AUTH_MODE=none" not in envf  # password stays in place


@pytest.mark.parametrize("host", ["localhost", "::1"])
def test_stream_loopback_alias_refuses_homefree(tmp_path, host):
    """Only the exact 127.0.0.1 default enables app-only streaming (#596 review)."""
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    env = {
        "AGENT_SESSIONS_HOME": str(home),
        "XDG_CONFIG_HOME": str(cfg),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
        "AGENT_SESSIONS_RELAY_URL": "wss://box.example/relay/ws",
        "AGENT_SESSIONS_HOST": host,
    }
    snippet = (
        'mkdir -p "$PREFIX"; '
        'printf "AGENT_SESSIONS_PASSWORD_HASH=x\\n" > "$ENVF"; '
        "homefree_maybe_setup </dev/null"
    )
    r = _run(snippet, tmp_path, env)
    assert r.returncode != 0
    assert "requires AGENT_SESSIONS_HOST=127.0.0.1" in r.stderr
    assert not (cfg / "systemd" / "user" / "agent-sessions-homefree.service").exists()
    assert "AGENT_SESSIONS_AUTH_MODE=none" not in (home / "env").read_text()  # password kept
