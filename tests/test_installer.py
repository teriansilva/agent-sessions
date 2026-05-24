"""Phase 2 of the installable distribution (#65): the rootless installer.

`sh -n` + a structural guard always run; the end-to-end test actually runs the
installer (no systemd) against the local repo and checks the acceptance criteria:
atomic `current` symlink, env 0600 with a hashed credential (no plaintext at rest),
the generated password logs in against the stored hash, and a re-run upgrades in
place while keeping the prior release for rollback.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"


def test_install_sh_syntax():
    assert subprocess.run(["sh", "-n", str(INSTALL_SH)]).returncode == 0


def test_install_sh_structural_invariants():
    s = INSTALL_SH.read_text()
    assert "chmod 600" in s  # env file locked down
    assert "mv -Tf" in s and ".current." in s  # atomic flip via temp-link + rename(2)
    assert "127.0.0.1" in s  # localhost bind default
    assert "hash_password" in s and "AGENT_SESSIONS_PASSWORD=" not in s  # hash only, no plaintext
    assert "AGENT_SESSIONS_NO_SERVICE" in s  # degrades without systemd


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_installer_end_to_end(tmp_path):
    home = tmp_path / "prefix"
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    env = {
        **os.environ,
        "AGENT_SESSIONS_REPO": str(REPO),
        # Pin the exact commit — robust whether or not a local `main` branch exists
        # (CI checks out a detached PR ref); exercises the clone+checkout fallback.
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_PORT": "8799",
    }
    r = subprocess.run(
        ["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=600
    )
    assert r.returncode == 0, r.stderr

    # Atomic release slot + a *runnable* entrypoint (venv built at its final path, so the
    # console-script shebang is valid — guards against a non-relocatable moved venv).
    current = home / "current"
    assert current.is_symlink()
    entry = current / "venv" / "bin" / "agent-sessions"
    assert entry.exists()
    run = subprocess.run([str(entry), "version"], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip()
    releases = sorted((home / "releases").iterdir())
    assert len(releases) == 1

    # Env: 0600, holds the hash + secret, NO plaintext password persisted.
    envf = home / "env"
    assert oct(envf.stat().st_mode & 0o777) == "0o600"
    text = envf.read_text()
    assert "AGENT_SESSIONS_PASSWORD_HASH=pbkdf2_sha256$" in text
    assert "AGENT_SESSIONS_SECRET_KEY=" in text
    assert "\nAGENT_SESSIONS_PASSWORD=" not in text  # plaintext never written

    # Credentials printed once → the generated password logs in against the stored hash.
    m = re.search(r"password:\s*(\S+)", r.stdout)
    assert m, r.stdout
    password = m.group(1)
    hash_line = next(
        ln for ln in text.splitlines() if ln.startswith("AGENT_SESSIONS_PASSWORD_HASH=")
    )
    from agent_sessions.auth import verify_password

    assert verify_password(password, hash_line.split("=", 1)[1])

    # Re-run = idempotent upgrade: keeps the env (no new password printed) and keeps the
    # prior release dir for rollback; `current` points at the new one.
    r2 = subprocess.run(
        ["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=600
    )
    assert r2.returncode == 0, r2.stderr
    assert "password:" not in r2.stdout  # existing credentials kept
    assert envf.read_text() == text  # env untouched
    releases2 = sorted((home / "releases").iterdir())
    assert len(releases2) == 2  # prior kept for rollback
    assert current.resolve() == sorted(releases2)[-1].resolve()


def test_install_sh_has_update_rollback():
    s = INSTALL_SH.read_text()
    assert "_healthcheck" in s  # post-restart health check
    assert "rolling back" in s and "prev" in s  # rollback to the prior release on failure


def test_install_sh_optin_autoupdate_timer():
    s = INSTALL_SH.read_text()
    assert "AGENT_SESSIONS_AUTOUPDATE" in s  # opt-in flag
    assert "$APP-update.timer" in s  # the user timer
    assert "agent-sessions autoupdate" in s  # timer runs the autoupdate command
    # The timer runs detached from the install shell, so the opt-in + channel + repo must
    # be baked into the service (else it loses the channel and self-disables on first run).
    assert "Environment=AGENT_SESSIONS_AUTOUPDATE=1" in s
    assert "Environment=AGENT_SESSIONS_CHANNEL=" in s
    assert "Environment=AGENT_SESSIONS_REPO=" in s
