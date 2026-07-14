"""Phase 2 of the installable distribution (#65): the rootless installer.

`sh -n` + a structural guard always run; the end-to-end test actually runs the
installer (no systemd) against the local repo and checks the acceptance criteria:
atomic `current` symlink, env 0600 with a hashed credential (no plaintext at rest),
the generated password logs in against the stored hash, and a re-run upgrades in
place while keeping the prior release for rollback.
"""

from __future__ import annotations

import json
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


def _run_adopt_bind(tmp_path, *, envf_lines, port="8765", port_explicit="0", host_explicit="0"):
    """Source install.sh (minus `main`), point ENVF at a fixture env file, set the pre-adoption
    bind vars, call adopt_persisted_bind(), and echo the resulting PORT/HOST. No tty, no service —
    a direct unit test of the persisted-bind adoption."""
    envf = tmp_path / "env"
    envf.write_text(envf_lines)
    body = INSTALL_SH.read_text().replace('\nmain "$@"\n', "\n")
    harness = body + (
        f'\nENVF="{envf}"\n'
        f"PORT={port}\nPORT_EXPLICIT={port_explicit}\n"
        f"HOST=127.0.0.1\nHOST_EXPLICIT={host_explicit}\nORIGIN_EXPLICIT=0\n"
        "adopt_persisted_bind\n"
        'printf "RESULT PORT=%s\\n" "$PORT"\n'
        'printf "RESULT HOST=%s\\n" "$HOST"\n'
    )
    hp = tmp_path / "harness.sh"
    hp.write_text(harness)
    r = subprocess.run(
        ["sh", str(hp)], env=_clean_env(), capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_installer_adopts_persisted_port_on_rerun(tmp_path):
    # An env-less re-run (autoupdate / manual `sh install.sh`) must NOT regenerate the unit with the
    # 8765 default and orphan a persisted reverse-proxy port (a proxied :3402 flips to :8765 → 502).
    # adopt_persisted_bind reads the persisted port back from the env file, mirroring host adoption.
    out = _run_adopt_bind(
        tmp_path, envf_lines="AGENT_SESSIONS_HOST=10.0.0.5\nAGENT_SESSIONS_PORT=3402\n"
    )
    assert "RESULT PORT=3402" in out

    # An explicit port on THIS run still wins over the persisted value.
    out = _run_adopt_bind(
        tmp_path, envf_lines="AGENT_SESSIONS_PORT=3402\n", port="9999", port_explicit="1"
    )
    assert "RESULT PORT=9999" in out

    # Port adoption is independent of host: a re-run that sets HOST explicitly but omits PORT must
    # still keep the persisted port (the host branch early-returns, the port branch runs first).
    out = _run_adopt_bind(tmp_path, envf_lines="AGENT_SESSIONS_PORT=3402\n", host_explicit="1")
    assert "RESULT PORT=3402" in out


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


def test_install_sh_stamps_release_version_into_ui_build():
    # #661: build_web() threads the just-installed package version into the Vite build as
    # AGENT_SESSIONS_VERSION so the footer shows it and every release busts the PWA precache.
    s = INSTALL_SH.read_text()
    build_web = s[s.index("build_web()") :]
    # The stamp comes from the release venv's own CLI (build_release pip-installs before
    # build_web runs), tolerating failure (empty ⇒ vite falls back to "dev").
    assert '/venv/bin/agent-sessions" version' in build_web
    # Both npm invocations run with the env set — `npm ci` spawns no build, but the stamp on
    # `npm run build` is the load-bearing one.
    assert 'AGENT_SESSIONS_VERSION="$app_version" "$NPM" run build' in build_web
    # And the web build config actually consumes it, with the documented "dev" fallback.
    vite_cfg = (INSTALL_SH.parent / "web" / "vite.config.ts").read_text()
    assert 'process.env.AGENT_SESSIONS_VERSION || "dev"' in vite_cfg


def test_install_sh_seeds_onboarding_pref():
    # #675: a genuine fresh install seeds onboarded=false so the setup wizard shows even when
    # the engines' session history was preserved; an upgrade seeds true / leaves it. The seed
    # goes through the app's own prefs writer and honors a custom AGENT_SESSIONS_PREFS.
    s = INSTALL_SH.read_text()
    assert "seed_onboarding" in s
    # Fresh vs upgrade keys off a *completed* prior install (a valid `current` symlink), not
    # the mere presence of `releases/` — a failed first install can leave `releases/` behind.
    assert "FRESH=1" in s and '[ -L "$CURRENT" ] && [ -e "$CURRENT" ]' in s
    assert "set_onboarded" in s and "from agent_sessions import prefs" in s
    # only writes when currently unset (idempotent — never clobbers an operator's choice)
    assert "get_onboarded() is None" in s


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
        # #675: point the onboarding pref at a temp file so the seed never touches the real
        # ~/.config/agent-sessions/prefs.json (per the "never touch real config" rule).
        "AGENT_SESSIONS_PREFS": str(tmp_path / "prefs.json"),
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

    # #675: a genuine fresh install seeds onboarded=false so the setup wizard shows even
    # though this test's HOME may carry scanned sessions.
    prefs_file = tmp_path / "prefs.json"
    assert prefs_file.exists(), "installer did not seed the onboarding pref"
    assert json.loads(prefs_file.read_text()).get("onboarded") is False

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
    # #675: the re-run is an upgrade — it must NOT flip the already-seeded pref (only writes
    # when unset), so the fresh install's onboarded=false is preserved, not reset to true.
    assert json.loads(prefs_file.read_text()).get("onboarded") is False


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_installer_failed_first_install_residue_is_still_fresh(tmp_path):
    # #675 (Hermes review): a failed first install can leave an empty `releases/` behind (the
    # trap removes only the half-built release dir). A retry must still be treated as FRESH —
    # fresh/upgrade keys off a completed install (`current` symlink), not `releases/` presence —
    # so it seeds onboarded=false and the wizard shows.
    home = tmp_path / "prefix"
    (home / "releases").mkdir(parents=True)  # residue: dir exists, but no `current` was created
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    prefs_file = tmp_path / "prefs.json"
    env = {
        **os.environ,
        "AGENT_SESSIONS_REPO": str(REPO),
        "AGENT_SESSIONS_REF": head,
        "AGENT_SESSIONS_HOME": str(home),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_ASSUME_YES": "1",
        "AGENT_SESSIONS_PORT": "8798",
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
        "AGENT_SESSIONS_PREFS": str(prefs_file),
    }
    r = subprocess.run(
        ["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=600
    )
    assert r.returncode == 0, r.stderr
    assert (home / "current").is_symlink()  # this run is the one that completed the install
    assert json.loads(prefs_file.read_text()).get("onboarded") is False


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


def test_install_sh_never_creates_the_legacy_autoupdate_timer():
    # #538: the systemd autoupdate timer is retired — the app schedules the daily check
    # itself. The installer only ever REMOVES the legacy units (migration), never writes
    # a unit for them.
    s = INSTALL_SH.read_text()
    assert "Environment=AGENT_SESSIONS_AUTOUPDATE=1" not in s  # the old baked-in service
    assert 'cat > "$UNIT_DIR/$APP-update' not in s  # no unit rendering for update timer
    assert "$APP-update.timer" in s  # the migration still references the legacy units


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


# ---- #538: in-app auto-update — no installer opt-in, legacy timer migration -----------


def test_install_sh_no_legacy_autoupdate_param():
    # Automatic updates are managed in the app (Settings → System); the installer must not
    # create the legacy timer or read the retired opt-in outside the migration path.
    s = INSTALL_SH.read_text()
    assert "manage_autoupdate" not in s
    assert "AUTOUPDATE_ONCALENDAR" not in s
    assert "migrate_legacy_autoupdate" in s
    # migration: preserve an enabled timer as the env-file opt-in, then remove both units
    assert "_env_set_if_absent AGENT_SESSIONS_AUTOUPDATE 1" in s
    assert 'rm -f "$UNIT_DIR/$APP-update.timer" "$UNIT_DIR/$APP-update.service"' in s
    # and never under NO_SERVICE (scratch/test installs must not touch real user units)
    migrate = s.split("migrate_legacy_autoupdate() {", 1)[1].split("\n}", 1)[0]
    assert "AGENT_SESSIONS_NO_SERVICE" in migrate


def test_install_sh_adopts_persisted_channel():
    # A re-run without AGENT_SESSIONS_CHANNEL follows the UI-persisted choice; an explicit
    # env var on the run still wins and is persisted back to the env file.
    s = INSTALL_SH.read_text()
    assert "adopt_persisted_channel" in s and "CHANNEL_EXPLICIT" in s
    assert '_env_set AGENT_SESSIONS_CHANNEL "$CHANNEL"' in s


def _extract_fn(source: str, name: str) -> str:
    """The named shell function verbatim: from its `name() {` line to the first bare `}`
    line (or the same line for one-liners like `_env_has`)."""
    lines = source.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    if lines[start].rstrip().endswith("}"):
        return lines[start]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _migration_harness(tmp_path, *, timer_enabled: bool, env_lines: str = ""):
    """Run migrate_legacy_autoupdate verbatim (extracted from install.sh) against a fake
    systemctl + scratch UNIT_DIR/ENVF, and return (exit_code, env_text, unit_dir)."""
    s = INSTALL_SH.read_text()
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    (unit_dir / "agent-sessions-update.timer").write_text("[Timer]\n")
    (unit_dir / "agent-sessions-update.service").write_text("[Service]\n")
    envf = tmp_path / "env"
    envf.write_text(env_lines)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    # is-enabled reflects the scenario; every other verb succeeds silently.
    rc = 0 if timer_enabled else 1
    stub.write_text(
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = is-enabled ] && exit {rc}; done\nexit 0\n'
    )
    stub.chmod(0o755)
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/bin/sh\nset -eu\n"
        "APP=agent-sessions\n"
        f'ENVF="{envf}"\n'
        f'UNIT_DIR="{unit_dir}"\n'
        "log() { :; }\n"
        + _extract_fn(s, "_env_has")
        + "\n"
        + _extract_fn(s, "_env_set_if_absent")
        + "\n"
        + _extract_fn(s, "migrate_legacy_autoupdate")
        + "\nmigrate_legacy_autoupdate\n"
    )
    env = {**_clean_env(), "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("AGENT_SESSIONS_NO_SERVICE", None)
    proc = subprocess.run(["sh", str(driver)], env=env, capture_output=True, text=True)
    return proc.returncode, envf.read_text(), unit_dir


def test_migration_enabled_timer_becomes_env_opt_in(tmp_path):
    code, env_text, unit_dir = _migration_harness(tmp_path, timer_enabled=True)
    assert code == 0
    assert "AGENT_SESSIONS_AUTOUPDATE=1" in env_text
    assert not (unit_dir / "agent-sessions-update.timer").exists()
    assert not (unit_dir / "agent-sessions-update.service").exists()


def test_migration_disabled_timer_removes_units_without_opt_in(tmp_path):
    code, env_text, unit_dir = _migration_harness(tmp_path, timer_enabled=False)
    assert code == 0
    assert "AGENT_SESSIONS_AUTOUPDATE" not in env_text
    assert not (unit_dir / "agent-sessions-update.timer").exists()


def test_migration_respects_an_existing_env_choice(tmp_path):
    # The operator already toggled OFF in the UI; a still-lingering enabled timer must not
    # flip the setting back on (set-if-absent semantics).
    code, env_text, _ = _migration_harness(
        tmp_path, timer_enabled=True, env_lines="AGENT_SESSIONS_AUTOUPDATE=0\n"
    )
    assert code == 0
    assert "AGENT_SESSIONS_AUTOUPDATE=0" in env_text
    assert "AGENT_SESSIONS_AUTOUPDATE=1" not in env_text
