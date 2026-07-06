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

# A dev shell on the deploy host often exports AGENT_SESSIONS_HOST/ORIGIN/PORT (the running
# instance's own settings). Those would leak through {**os.environ} into an install env and defeat
# tests that assert the *default* bind, so strip them and let each test set the bind explicitly
# (cf. the known "pytest env leak" gotcha).
_BIND_ENV_KEYS = (
    "AGENT_SESSIONS_HOST",
    "AGENT_SESSIONS_ORIGIN",
    "AGENT_SESSIONS_PORT",
    "AGENT_SESSIONS_ASSUME_YES",
)


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in _BIND_ENV_KEYS}


def test_install_sh_syntax():
    assert subprocess.run(["sh", "-n", str(INSTALL_SH)]).returncode == 0


def test_install_sh_structural_invariants():
    s = INSTALL_SH.read_text()
    assert "chmod 600" in s  # env file locked down
    assert "mv -Tf" in s and ".current." in s  # atomic flip via temp-link + rename(2)
    assert "127.0.0.1" in s  # localhost bind default
    assert "hash_password" in s and "AGENT_SESSIONS_PASSWORD=" not in s  # hash only, no plaintext
    assert "AGENT_SESSIONS_NO_SERVICE" in s  # degrades without systemd


def test_install_sh_interactive_bind_selection():
    # #487: an interactive install offers a bind-address choice instead of silently leaving the
    # operator on an unreachable 127.0.0.1.
    s = INSTALL_SH.read_text()
    assert "choose_host" in s and "_host_ips" in s
    # The menu is driven over the controlling tty (the script body is stdin under `curl|sh`),
    # mirroring _confirm. Offers localhost (default), all-interfaces, and detected addresses.
    assert "/dev/tty" in s
    assert "0.0.0.0" in s
    # Addresses are enumerated WITHOUT root (iproute2 / hostname / ifconfig).
    assert "scope global" in s or "hostname -I" in s
    # A non-localhost bind requires an explicit, default-No confirm behind a security warning.
    assert "exposes" in s and "[y/N]" in s
    # The origin is re-derived from the chosen bind so same-origin/CSRF works over the LAN.
    assert "_recompute_origin" in s
    # Explicit overrides + non-interactive installs skip the prompt (safe default preserved)…
    assert "HOST_EXPLICIT" in s and "AGENT_SESSIONS_ASSUME_YES" in s
    # …and a re-run adopts the persisted bind (no silent revert to localhost on upgrade).
    assert "adopt_persisted_bind" in s


def test_install_sh_firewall_offer_and_primary_route_origin():
    # #504: a non-localhost interactive bind derives the origin from the default-route source IP
    # and offers to open the port in the host firewall.
    s = INSTALL_SH.read_text()
    # Origin derivation prefers the primary (default-route) address over the first enumerated one,
    # so a multi-homed host (docker bridges / VPN) doesn't hand back an unreachable internal IP.
    assert "_primary_ip" in s and "ip route get" in s
    # Firewall offer: ufw (Debian/Ubuntu) or firewalld (Fedora/RHEL), with a manual iptables hint.
    assert "_offer_firewall" in s
    assert "ufw allow" in s
    assert "firewall-cmd" in s and "--add-port" in s
    assert "iptables" in s  # manual fallback when neither tool is present
    # Default-No, gated behind sudo (mirrors the bind confirm).
    assert "needs sudo" in s and "[y/N]" in s
    # Firewall changes go through explicit argv — no inline shell interpreter in the installer.
    assert "sh -c" not in s and "bash -c" not in s


def _drive_choose_host(tmp_path, fakebin, feed, *, port="8765", timeout=25):
    """Source install.sh's functions (minus `main`) and call choose_host() under a real pty, with
    `fakebin` prepended to PATH so the stubbed ip/ufw/sudo never touch the host. `feed` is a list
    of (prompt-substring, reply-bytes) fired in order as prompts appear. Returns the pty output."""
    import pty
    import select
    import time

    if not hasattr(os, "fork"):  # pragma: no cover - non-POSIX
        pytest.skip("pty/fork unavailable")
    body = INSTALL_SH.read_text().replace('\nmain "$@"\n', "\n")
    harness = body + (
        f'\nPORT={port}\nHOST=127.0.0.1\nORIGIN="http://$HOST:$PORT"\n'
        "HOST_EXPLICIT=0\nORIGIN_EXPLICIT=0\nAPP=agent-sessions\n"
        "choose_host\n"
        'printf "RESULT HOST=%s\\n" "$HOST"\n'
        'printf "RESULT URL=%s\\n" "$ORIGIN"\n'
    )
    hp = tmp_path / "harness.sh"
    hp.write_text(harness)
    pid, fd = pty.fork()
    if pid == 0:  # child
        for k in _BIND_ENV_KEYS:  # a dev shell's leaked HOST/ASSUME_YES would skip the prompt
            os.environ.pop(k, None)
        os.environ["PATH"] = f"{fakebin}:" + os.environ.get("PATH", "")
        os.execvp("sh", ["sh", str(hp)])
    buf = b""
    pending = list(feed)
    t0 = time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([fd], [], [], 0.3)
        if not r:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        buf += data
        if pending and pending[0][0] in buf.decode(errors="replace"):
            _, reply = pending.pop(0)
            os.write(fd, reply)
    os.close(fd)
    return buf.decode(errors="replace")


def _write_fake_ip(fakebin):
    # A multi-homed host whose FIRST enumerated global address is an internal docker bridge, but
    # whose default route leaves via the real LAN IP. _primary_ip must win → origin = 10.0.0.5.
    (fakebin / "ip").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"route get"*) echo "1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 1000" ;;\n'
        '  *"addr show scope global"*)\n'
        '    echo "2: eth1    inet 172.18.0.1/16 brd 172.18.255.255 scope global eth1"\n'
        '    echo "3: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0" ;;\n'
        "esac\n"
    )
    (fakebin / "ip").chmod(0o755)


def test_installer_firewall_offer_accept_opens_port_and_uses_primary_origin(tmp_path):
    # #504 red→green: pick 0.0.0.0, confirm the bind, accept the firewall offer. The origin must be
    # the default-route source (10.0.0.5), NOT the first-enumerated docker bridge (172.18.0.1), and
    # the (stubbed) ufw rule must be applied. Hermetic: stub ip/ufw/sudo so the host is untouched.
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_fake_ip(fakebin)
    (fakebin / "ufw").write_text('#!/bin/sh\necho "FAKE-UFW $*"\n')
    (fakebin / "sudo").write_text('#!/bin/sh\nexec "$@"\n')  # run argv directly — never real sudo
    (fakebin / "ufw").chmod(0o755)
    (fakebin / "sudo").chmod(0o755)

    out = _drive_choose_host(
        tmp_path,
        fakebin,
        [("Choose an option", b"2\n"), ("anyway?", b"y\n"), ("Add this rule now", b"y\n")],
    )
    assert "RESULT HOST=0.0.0.0" in out
    # Primary-route source wins over the first enumerated address.
    assert "RESULT URL=http://10.0.0.5:8765" in out
    assert "RESULT URL=http://172.18.0.1" not in out
    # The rule was offered with the exact command and applied through the stub.
    assert "sudo ufw allow 8765/tcp" in out
    assert "FAKE-UFW allow 8765/tcp" in out
    assert "firewall: opened 8765/tcp" in out


def test_installer_firewall_offer_declined_is_noop(tmp_path):
    # Declining the firewall offer must NOT invoke sudo and must leave the install green, printing
    # the manual command instead (default-No, best-effort).
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_fake_ip(fakebin)
    (fakebin / "ufw").write_text('#!/bin/sh\necho "FAKE-UFW $*"\n')
    sentinel = tmp_path / "sudo_was_called"
    (fakebin / "sudo").write_text(f'#!/bin/sh\n: > "{sentinel}"\nexec "$@"\n')
    (fakebin / "ufw").chmod(0o755)
    (fakebin / "sudo").chmod(0o755)

    out = _drive_choose_host(
        tmp_path,
        fakebin,
        [("Choose an option", b"2\n"), ("anyway?", b"y\n"), ("Add this rule now", b"n\n")],
    )
    assert "RESULT HOST=0.0.0.0" in out
    assert "firewall left unchanged" in out
    assert "sudo ufw allow 8765/tcp" in out  # the manual command is still shown
    assert not sentinel.exists(), "declining the offer must not invoke sudo"


def test_installer_origin_falls_back_to_first_addr_without_default_route(tmp_path):
    # When `ip route get` yields nothing (no default route), _primary_ip is empty and the 0.0.0.0
    # origin falls back to the first enumerated address rather than an unusable http://0.0.0.0.
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    # No default route → `route get` prints nothing; only addr enumeration succeeds.
    (fakebin / "ip").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"route get"*) : ;;\n'
        '  *"addr show scope global"*)\n'
        '    echo "2: eth1    inet 172.18.0.1/16 brd 172.18.255.255 scope global eth1"\n'
        '    echo "3: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0" ;;\n'
        "esac\n"
    )
    (fakebin / "ufw").write_text('#!/bin/sh\necho "FAKE-UFW $*"\n')
    (fakebin / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    for f in ("ip", "ufw", "sudo"):
        (fakebin / f).chmod(0o755)

    out = _drive_choose_host(
        tmp_path,
        fakebin,
        [("Choose an option", b"2\n"), ("anyway?", b"y\n"), ("Add this rule now", b"y\n")],
    )
    assert "RESULT URL=http://172.18.0.1:8765" in out  # first enumerated address (fallback)
    assert "RESULT URL=http://0.0.0.0" not in out


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_installer_no_tty_keeps_localhost(tmp_path):
    # #487: with no AGENT_SESSIONS_HOST, no ASSUME_YES, and detached from any controlling
    # terminal (start_new_session=True → setsid → /dev/tty is unopenable), the interactive bind
    # prompt must skip and the bind must stay 127.0.0.1. This is the piped `curl|sh` contract:
    # an unattended install never blocks waiting for input and never silently exposes the host.
    home = tmp_path / "prefix"
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    env = {
        **_clean_env(),
        "AGENT_SESSIONS_REPO": str(REPO),
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
        "AGENT_SESSIONS_PORT": "8796",
    }
    # NB: deliberately NO host/ASSUME_YES here — the no-tty guard alone must prevent the hang.
    r = subprocess.run(
        ["sh", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert r.returncode == 0, r.stderr
    text = (home / "env").read_text()
    assert "AGENT_SESSIONS_HOST=127.0.0.1" in text
    assert "AGENT_SESSIONS_ORIGIN=http://127.0.0.1:8796" in text


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_installer_explicit_host_persists_and_is_adopted_on_rerun(tmp_path):
    # #487: an explicit AGENT_SESSIONS_HOST is persisted, and a re-run WITHOUT the env var still
    # keeps it (adopt_persisted_bind reads it back from the env file) — so an upgrade / autoupdate
    # never silently reverts a 0.0.0.0 / LAN bind to localhost.
    home = tmp_path / "prefix"
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    base = {
        **_clean_env(),
        "AGENT_SESSIONS_REPO": str(REPO),
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
        "AGENT_SESSIONS_PORT": "8795",
    }
    # First install with an explicit bind (an explicit host suppresses the prompt → no tty needed).
    r1 = subprocess.run(
        ["sh", str(INSTALL_SH)],
        env={**base, "AGENT_SESSIONS_HOST": "0.0.0.0"},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r1.returncode == 0, r1.stderr
    envf = home / "env"
    assert "AGENT_SESSIONS_HOST=0.0.0.0" in envf.read_text()

    # Re-run with NO host in the environment + detached from any tty: the persisted 0.0.0.0 must
    # survive (not revert to 127.0.0.1), and the prompt must not fire.
    r2 = subprocess.run(
        ["sh", str(INSTALL_SH)],
        env=base,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert r2.returncode == 0, r2.stderr
    assert "AGENT_SESSIONS_HOST=0.0.0.0" in envf.read_text()
    # The rendered URL reflects the persisted bind (derived origin), not localhost.
    assert "http://127.0.0.1:8795" not in r2.stdout


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


def test_install_sh_never_seeds_takeover_flag():
    # #434: AGENT_SESSIONS_TAKEOVER is an EXPERIMENTAL, staging-only flag. The installer must
    # never write it (so a fresh OR migrated customer install defaults to OFF) — an accidental
    # prod re-enable can then only ever happen out-of-band, never via `curl | sh`.
    assert "AGENT_SESSIONS_TAKEOVER" not in INSTALL_SH.read_text()


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
        # #487: skip the interactive bind prompt so the test never blocks on /dev/tty when
        # pytest runs from a real terminal (CI has no controlling tty, but a dev's shell does).
        "AGENT_SESSIONS_ASSUME_YES": "1",
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
    assert "AGENT_SESSIONS_TAKEOVER" not in text  # #434: experimental flag never seeded

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
        "AGENT_SESSIONS_ASSUME_YES": "1",  # #487: never block on the interactive bind prompt
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
        "AGENT_SESSIONS_ASSUME_YES": "1",  # #487: never block on the interactive bind prompt
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
    assert "AGENT_SESSIONS_TAKEOVER" not in text  # #434: migrate never adds the experimental flag
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
