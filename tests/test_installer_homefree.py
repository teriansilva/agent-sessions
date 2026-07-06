"""Installer tests for the Home Free stream channel (#27).

Source install.sh's shell functions (minus `main`) and exercise them directly,
following the pattern used by the existing installer tests.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

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


def test_structural_invariants():
    s = INSTALL_SH.read_text()
    assert "AGENT_SESSIONS_REMOTE" in s
    assert "$APP-homefree.service" in s
    assert "python -m agent_sessions.homefree" in s
    assert "FULL CONTROL of this machine" in s  # anti-scam copy
    # a plain curl|sh must not default to contacting a relay
    assert "_mode=selfhost" in s
