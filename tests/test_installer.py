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


def test_install_sh_builds_and_serves_react_ui():
    s = INSTALL_SH.read_text()
    # The React UI is built from source at install time and the app is pointed at it.
    assert "build_web" in s
    assert "npm run build" in s.replace('"$NPM" run build', "npm run build")
    # Serving/runtime defaults are applied idempotently (set-if-absent) so an upgrade of an
    # existing install also cuts over — not just a fresh write.
    assert "migrate_env" in s and "_env_set_if_absent" in s
    # The React UI + ws terminal are the only product; the app no longer reads
    # AGENT_SESSIONS_UI / AGENT_SESSIONS_TERMINAL, so the installer no longer sets them.
    assert "AGENT_SESSIONS_UI" not in s
    assert "AGENT_SESSIONS_TERMINAL" not in s
    assert "AGENT_SESSIONS_WEB_DIST" in s and "src/web/dist" in s
    assert "AGENT_SESSIONS_RUNTIME_DIR" in s


def test_install_sh_self_contained_toolchain():
    s = INSTALL_SH.read_text()
    # Missing prereqs are auto-installed (distro) or vendored (Node/Python), not just rejected.
    assert "_pkg_install" in s
    assert "ensure_node" in s and "nodejs.org/dist" in s  # vendored Node fallback, no sudo
    # Python >= 3.11 is resolved (override > system > distro) and, as a last resort, a pinned
    # relocatable standalone CPython is vendored into the toolchain dir — no sudo (#333).
    assert "ensure_python" in s and "python-build-standalone" in s
    assert "AGENT_SESSIONS_PYTHON" in s  # explicit interpreter override is honored
    # Supply chain: the vendored-Python tarball is checksum-pinned and verified BEFORE unpack/use —
    # TLS + a mutable release URL alone is not enough for a rootless `curl | sh` path (#333).
    assert "want_sha" in s and "checksum mismatch" in s and "_sha256" in s
    # All four supported assets carry a pinned digest (linux x86_64/aarch64, macOS x86_64/arm64).
    for sha in ("9be5c21b", "f0c9ea00", "e6776f05", "0c21806e"):
        assert sha in s, f"missing pinned checksum {sha}"
    # Verification must precede extraction (refuse a tampered tarball before it is unpacked).
    assert s.index("checksum mismatch") < s.index("could not unpack standalone Python")
    assert "dtach" in s  # ws-PTY backend ensured
    assert "preflight_report" in s  # up-front validation summary


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
        # Keep this test fast + Node-free; the real UI build is covered by the
        # Node-gated test below.
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
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
    # The React UI + ws terminal are the only product (no UI/terminal env switch); the
    # generated env just points at the built dist + the ws-PTY runtime dir.
    assert "AGENT_SESSIONS_UI=" not in text
    assert "AGENT_SESSIONS_TERMINAL=" not in text
    assert f"AGENT_SESSIONS_WEB_DIST={home}/current/src/web/dist" in text
    assert f"AGENT_SESSIONS_RUNTIME_DIR={home}/pty" in text

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


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_UI_BUILD") != "1" or not shutil.which("npm"),
    reason="opt-in real npm ci + Vite build; set AGENT_SESSIONS_TEST_UI_BUILD=1",
)
def test_installer_builds_the_react_ui(tmp_path):
    # Opt-in heavy test: a full install WITHOUT skipping the build must produce a served
    # SPA (web/dist/index.html) inside the release `current` points at. Gated behind an
    # env flag because it runs `npm ci` (network + registry) which isn't deterministic in
    # a sandboxed CI container; the structural test above guards the wiring on every run,
    # and the install path is validated locally + during the prod cutover.
    home = tmp_path / "prefix"
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    env = {
        **os.environ,
        "AGENT_SESSIONS_REPO": str(REPO),
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        # NO_SERVICE is essential: the systemd unit path is per-user (not per-HOME), so
        # without this the test would render + restart the host's real agent-sessions unit.
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_PORT": "8798",
    }
    r = subprocess.run(
        ["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=900
    )
    assert r.returncode == 0, r.stderr
    dist_index = home / "current" / "src" / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"no built SPA at {dist_index}\n{r.stdout[-2000:]}"
    assert (home / "pty").is_dir()  # ws-PTY runtime dir created


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_installer_migrates_existing_env_to_react(tmp_path):
    # An upgrade of a pre-existing (older) env must gain the serving + runtime flags
    # WITHOUT clobbering the existing secret/credential lines — otherwise re-running the
    # installer wouldn't actually point an existing deployment at the built dist.
    home = tmp_path / "prefix"
    home.mkdir()
    envf = home / "env"
    envf.write_text(
        "AGENT_SESSIONS_USERNAME=marcus\n"
        "AGENT_SESSIONS_PASSWORD_HASH=pbkdf2_sha256$keepme\n"
        "AGENT_SESSIONS_SECRET_KEY=keepmesecret\n"
        "AGENT_SESSIONS_ORIGIN=https://terminal.example\n"
    )
    envf.chmod(0o600)
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    env = {
        **os.environ,
        "AGENT_SESSIONS_REPO": str(REPO),
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
        "AGENT_SESSIONS_PORT": "8797",
    }
    r = subprocess.run(
        ["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=600
    )
    assert r.returncode == 0, r.stderr
    text = envf.read_text()
    # New serving/runtime flags added (no UI/terminal switch — that's the only product now)…
    assert "AGENT_SESSIONS_UI=" not in text
    assert "AGENT_SESSIONS_TERMINAL=" not in text
    assert f"AGENT_SESSIONS_WEB_DIST={home}/current/src/web/dist" in text
    assert f"AGENT_SESSIONS_RUNTIME_DIR={home}/pty" in text
    # …existing secrets/credentials preserved, exactly once each (no clobber, no dup).
    assert "AGENT_SESSIONS_PASSWORD_HASH=pbkdf2_sha256$keepme\n" in text
    assert "AGENT_SESSIONS_SECRET_KEY=keepmesecret\n" in text
    assert text.count("AGENT_SESSIONS_WEB_DIST=") == 1
    assert text.count("AGENT_SESSIONS_SECRET_KEY=") == 1
    assert oct(envf.stat().st_mode & 0o777) == "0o600"  # still locked down
    assert (home / "pty").is_dir()
    # No new password printed (credentials kept).
    assert "password:" not in r.stdout


def test_install_sh_has_update_rollback():
    s = INSTALL_SH.read_text()
    assert "_healthcheck" in s  # post-restart health check
    assert "rolling back" in s and "prev" in s  # rollback to the prior release on failure


def test_install_sh_unit_puts_local_bin_on_path():
    s = INSTALL_SH.read_text()
    # The rendered service unit must carry ~/.local/bin on PATH so sessions spawned by the
    # app (claude/opencode/codex/gemini) resolve there — otherwise the claude CLI nags about
    # "~/.local/bin is not in your PATH". %h is the systemd-user home-dir specifier.
    assert "Environment=PATH=%h/.local/bin:" in s


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


def test_uninstall_sh_is_safe_and_complete():
    p = REPO / "uninstall.sh"
    assert p.is_file()
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0  # parses
    s = p.read_text()
    # Removes what the installer created…
    for token in ("systemctl --user", "daemon-reload", ".local/share", "-update.timer"):
        assert token in s, f"uninstall.sh missing {token!r}"
    assert "rm -rf" in s and "_confirm" in s  # destructive but confirmed first
    # …and NEVER deletes the agents' own conversation stores. The only lines mentioning them
    # must be comments / the "kept" note, never an `rm`.
    for ln in s.splitlines():
        code = ln.split("#", 1)[0]
        assert not ("rm " in code and any(d in code for d in (".claude", ".codex", ".gemini"))), ln
