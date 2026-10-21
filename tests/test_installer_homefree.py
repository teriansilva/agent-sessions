"""Installer tests for the Home Free stream channel (#27).

Source install.sh's shell functions (minus `main`) and exercise them directly,
following the pattern used by the existing installer tests.
"""

import os
import re
import signal
import stat
import subprocess
import time
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


# ---- credential lifecycle: rotate / disable / show (#612 Phase 3) --------------


def _stream_env(tmp_path: Path) -> dict:
    """Env for a loopback stream-mode install under tmp dirs, with no real systemd."""
    return {
        "AGENT_SESSIONS_HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        # These tests are about key mechanics on a box with no systemd, not about the
        # agent-liveness boundary — assert the agent is stopped so they never depend on
        # whether the machine running pytest happens to have a relay agent of its own.
        # The boundary tests below override this back to "0" and stub the probes instead.
        "AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED": "1",
        "AGENT_SESSIONS_REMOTE": "stream",
        "AGENT_SESSIONS_RELAY_URL": "wss://box.example/relay/ws",
    }


def _setup_stream(tmp_path: Path, env: dict) -> Path:
    """Run a real stream-mode setup and return the homefree dir."""
    snippet = (
        'mkdir -p "$PREFIX"; '
        'printf "AGENT_SESSIONS_PASSWORD_HASH=x\\n" > "$ENVF"; '
        "homefree_maybe_setup </dev/null"
    )
    r = _run(snippet, tmp_path, env)
    assert r.returncode == 0, r.stderr
    return Path(env["AGENT_SESSIONS_HOME"]) / "homefree"


def test_rotate_key_replaces_the_key_preserves_the_name_and_keeps_a_rollback(tmp_path):
    """Rotation issues a new key, leaves the console name alone, and keeps the old key.

    The console name is the box's identity to the relay and to whoever already wrote it
    down — a key rotation is not a rename. The superseded key is kept because a rotation
    the operator regrets is otherwise unrecoverable on a box whose only gate is that key.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    name_before = (hf / "console_name").read_text()
    key_before = (hf / "access_key").read_text()

    r = _run("homefree_rotate_key", tmp_path, env)
    assert r.returncode == 0, r.stderr

    assert (hf / "console_name").read_text() == name_before, "rotation must not rename the box"
    key_after = (hf / "access_key").read_text()
    assert key_after != key_before
    assert stat.S_IMODE((hf / "access_key").stat().st_mode) == 0o600
    # The superseded key is kept, 0600, and is NOT the live one.
    assert (hf / "access_key.prev").read_text() == key_before
    assert stat.S_IMODE((hf / "access_key.prev").stat().st_mode) == 0o600
    # The new key is shown with the standing "this grants full control" warning.
    assert key_after.strip() in r.stdout
    assert "Never enter it for anyone who contacted you" in r.stdout


def test_rotate_key_is_idempotent_across_repeated_runs(tmp_path):
    """Rotating twice works twice and yields a distinct key each time."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    seen = {(hf / "access_key").read_text().strip()}
    for _ in range(3):
        r = _run("homefree_rotate_key", tmp_path, env)
        assert r.returncode == 0, r.stderr
        k = (hf / "access_key").read_text().strip()
        assert k not in seen, "a rotation must not re-issue a key it already used"
        seen.add(k)
        assert stat.S_IMODE((hf / "access_key").stat().st_mode) == 0o600


def test_rotate_key_leaves_the_old_key_live_when_generation_fails(tmp_path):
    """A failed rotation must not invalidate the working credential.

    This is the lockout case the ordering exists to prevent: the access key is the ONLY
    gate on a streamed box (AUTH_MODE=none), so a rotation that clears the old key before
    a good new one exists leaves a machine nobody can reach. `homefree_gen_key` is
    overridden to emit something that fails validation — the swap must never happen.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    r = _run("homefree_gen_key() { printf 'short\\n'; }; homefree_rotate_key", tmp_path, env)
    assert r.returncode != 0
    assert "failed validation" in (r.stdout + r.stderr)
    assert (hf / "access_key").read_text() == key_before, "the live key was invalidated on failure"


def test_rotate_key_refuses_when_home_free_was_never_set_up(tmp_path):
    env = _stream_env(tmp_path)
    r = _run("homefree_rotate_key", tmp_path, env)
    assert r.returncode != 0
    assert "not set up" in (r.stdout + r.stderr)


def test_disable_stops_the_agent_and_quarantines_the_key_only(tmp_path):
    """`--homefree-disable` takes the key out of the live config and touches nothing else.

    The blast radius is the point: disabling remote access must never be a way to lose
    local state, so the app unit, env, sessions and transcripts are all asserted untouched.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    home = Path(env["AGENT_SESSIONS_HOME"])
    key_before = (hf / "access_key").read_text()
    env_before = (home / "env").read_text()
    # Local state that must survive.
    (home / "sessions-marker").write_text("transcripts live here")

    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode == 0, r.stderr

    assert not (hf / "access_key").exists(), "the key is still in the live config"
    assert (hf / "access_key.disabled").read_text() == key_before
    assert stat.S_IMODE((hf / "access_key.disabled").stat().st_mode) == 0o600
    # Untouched: app env and local data.
    assert (home / "env").read_text() == env_before
    assert (home / "sessions-marker").read_text() == "transcripts live here"
    assert (hf / "console_name").exists(), "the box keeps its identity"


def test_disable_warns_that_auth_mode_none_is_still_in_effect(tmp_path):
    """Disabling says what it deliberately did NOT do.

    Enabling stream mode set AGENT_SESSIONS_AUTH_MODE=none. Flipping that back here would
    lock out an operator with no password set, so disable leaves app auth exactly as it
    found it — and tells the operator, rather than leaving a passwordless app behind
    silently. Following the contract and flagging the consequence, not quietly diverging.
    """
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    envf = Path(env["AGENT_SESSIONS_HOME"]) / "env"
    assert "AGENT_SESSIONS_AUTH_MODE=none" in envf.read_text()

    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "AGENT_SESSIONS_AUTH_MODE=none" in r.stdout
    assert "any LOCAL account" in r.stdout
    assert "restore password auth" in r.stdout


def test_disable_is_idempotent(tmp_path):
    """A second disable is a quiet success, not an error — config-management safe."""
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    assert _run("homefree_disable", tmp_path, env).returncode == 0
    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "already disabled" in r.stdout


def test_show_credentials_prints_existing_material_and_generates_nothing(tmp_path):
    """Read-only: the same name/key come back, byte for byte, on repeated calls."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    name = (hf / "console_name").read_text().strip()
    key = (hf / "access_key").read_text().strip()

    for _ in range(2):
        r = _run("homefree_show_credentials", tmp_path, env)
        assert r.returncode == 0, r.stderr
        assert name in r.stdout and key in r.stdout
        assert (hf / "access_key").read_text().strip() == key, "show must not rotate anything"


def test_show_credentials_fails_clearly_when_disabled(tmp_path):
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    assert _run("homefree_disable", tmp_path, env).returncode == 0
    r = _run("homefree_show_credentials", tmp_path, env)
    assert r.returncode != 0
    assert "disabled" in (r.stdout + r.stderr)


def test_show_credentials_fails_clearly_when_never_set_up(tmp_path):
    env = _stream_env(tmp_path)
    r = _run("homefree_show_credentials", tmp_path, env)
    assert r.returncode != 0
    assert "not set up" in (r.stdout + r.stderr)


def test_lifecycle_flags_never_run_an_install(tmp_path):
    """The flags short-circuit `main` — no release is built as a side effect.

    A maintenance command that also reinstalled the app would be a genuinely dangerous
    surprise on a production box, so this asserts the dispatch happens before any build.
    """
    s = INSTALL_SH.read_text()
    dispatch_at = s.index('homefree_lifecycle_dispatch "${1:-}"')
    build_at = s.index('build_release "$ref"')
    assert dispatch_at < build_at
    for flag in ("--homefree-rotate-key", "--homefree-disable", "--homefree-show-credentials"):
        assert flag in s


def test_unknown_homefree_flag_is_fatal_not_ignored(tmp_path):
    """A mistyped lifecycle flag must not fall through into a full install.

    Before this change install.sh parsed no arguments at all, so any flag was silently
    ignored — which for these commands would mean "reinstall the app" as the response to a
    typo. Only `--homefree-*` is claimed; other arguments keep the old pass-through.
    """
    env = _stream_env(tmp_path)
    r = _run('homefree_lifecycle_dispatch "--homefree-rotatekey"', tmp_path, env)
    assert r.returncode != 0
    assert "unknown option" in (r.stdout + r.stderr)
    ok = _run('homefree_lifecycle_dispatch "--something-else"; echo FELLTHROUGH', tmp_path, env)
    assert "FELLTHROUGH" in ok.stdout


# ---- systemctl failures must fail closed (#820 review) -------------------------


def _fake_systemctl(tmp_path: Path, *, failing_verb: str) -> Path:
    """A `systemctl` stub on PATH: the availability probe succeeds, `failing_verb` exits 1.

    The two must be distinguishable, because the production code treats them differently on
    purpose — an unavailable user systemd is a legitimate no-op, a failed *action* is not.
    A bare `systemctl --user` (no verb) is the availability probe.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        '# args: --user [verb] [unit];  bare "--user" is the availability probe → succeed\n'
        '[ "$#" -le 1 ] && exit 0\n'
        f'[ "$2" = "{failing_verb}" ] && exit 1\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bindir


def _run_with_systemd(snippet: str, tmp_path: Path, env: dict, bindir: Path):
    """Like `_run`, but with systemd 'available' (NO_SERVICE off) and a stubbed systemctl."""
    env = {**env, "AGENT_SESSIONS_NO_SERVICE": "0", "PATH": f"{bindir}:{os.environ['PATH']}"}
    return _run(snippet, tmp_path, env)


def test_rotate_fails_loudly_when_the_agent_cannot_be_restarted(tmp_path):
    """A rotation whose restart fails must not print the new key as if it were live.

    The agent read the old key at startup and holds it in memory, so until it restarts the
    new key does not work. Reporting success here hands the operator a credential that will
    not let them in — locking them out of their own box. Caught in review on this PR.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _fake_systemctl(tmp_path, failing_verb="restart")
    r = _run_with_systemd("homefree_rotate_key", tmp_path, env, bindir)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "still using the OLD key" in out
    assert "Access key:" not in out, "the success banner was printed despite a failed restart"
    # The new key IS on disk (that half succeeded) and the old one is recoverable — the
    # message says both, so the operator is not guessing about the state they are in.
    assert (hf / "access_key").read_text() != key_before
    assert (hf / "access_key.prev").read_text() == key_before


def test_disable_does_not_quarantine_the_key_when_the_agent_cannot_be_stopped(tmp_path):
    """If the agent cannot be stopped, the key stays put and the command fails.

    Moving the key while the agent is still running revokes nothing — it already holds the
    key in memory, so the box stays reachable with a credential the operator has been told is
    dead, and the only record of *which* key that is has been moved out of the way.
    Revocation that cannot be confirmed must not be reported as revocation.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _fake_systemctl(tmp_path, failing_verb="stop")
    r = _run_with_systemd("homefree_disable", tmp_path, env, bindir)
    assert r.returncode != 0
    assert "has NOT been revoked" in (r.stdout + r.stderr)
    assert (hf / "access_key").read_text() == key_before, "the key was moved anyway"
    assert not (hf / "access_key.disabled").exists()


def test_disable_fails_when_the_unit_cannot_be_disabled(tmp_path):
    """Stopped but not disabled means it comes back at login — also not "disabled"."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _fake_systemctl(tmp_path, failing_verb="disable")
    r = _run_with_systemd("homefree_disable", tmp_path, env, bindir)
    assert r.returncode != 0
    assert "may start again at login" in (r.stdout + r.stderr)
    assert (hf / "access_key").read_text() == key_before


# ---- the no-systemd / manual-agent boundary (#820 review, second pass) ---------
#
# Without a user systemd there is no unit to stop, but there may well still be an AGENT: the
# installer prints a manual `python -m agent_sessions.homefree` command for exactly that case.
# A running agent read the key at startup and holds it in memory, so mutating the key file
# underneath it revokes nothing. "Unavailable systemd" is therefore not by itself a licence to
# report success — what decides it is whether the agent can be managed, or at least verified.


def _no_systemd_bin(tmp_path: Path, *, agent: str) -> Path:
    """A PATH dir where the user-systemd probe fails and the process probes report `agent`.

    `agent` is one of:
      * "stopped"      — pgrep exits 1 (matched nothing): provably no agent running.
      * "running"      — pgrep exits 0: a hand-started agent is alive.
      * "unverifiable" — pgrep exits 2 (pgrep itself failed) and ps exits 1, so neither probe
                         can answer. Exit 2 is pgrep's real contract for its own failure,
                         which is why the production code separates >= 2 from 1 instead of
                         reading any non-zero exit as "nothing running".
    """
    bindir = tmp_path / f"nosystemd-{agent}"
    bindir.mkdir(exist_ok=True)
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 1\n")  # probe itself fails → unavailable
    (bindir / "systemctl").chmod(0o755)
    rc = {"stopped": 1, "running": 0, "unverifiable": 2}[agent]
    (bindir / "pgrep").write_text(f"#!/bin/sh\nexit {rc}\n")
    (bindir / "pgrep").chmod(0o755)
    if agent == "unverifiable":
        (bindir / "ps").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "ps").chmod(0o755)
    return bindir


def _unmanaged(env: dict) -> dict:
    """Drop the suite-wide "agent is stopped" assertion so the real probes are consulted."""
    return {**env, "AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED": "0"}


def test_service_actions_still_no_op_without_a_user_systemd(tmp_path):
    """The tolerant half: no user systemd AND no agent running is a legitimate no-op.

    This is the asymmetry that makes the change safe — an install that never had a service
    (a container, a plain non-service install) must not start failing because a unit it never
    created cannot be stopped. The pgrep stub is what makes this the *verified*-stopped case
    rather than an assumption: it exits 1, which is pgrep for "matched nothing".
    """
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    bindir = _no_systemd_bin(tmp_path, agent="stopped")
    r = _run_with_systemd("homefree_disable", tmp_path, _unmanaged(env), bindir)
    assert r.returncode == 0, r.stderr
    assert "quarantined" in r.stdout


def test_disable_refuses_while_an_unmanaged_agent_is_still_running(tmp_path):
    """No systemd + a live hand-started agent: refuse, and leave the key exactly where it is.

    Quarantining the key here produces the worst available outcome — the command reports Home
    Free disabled, the operator believes the credential is dead, and the agent goes on serving
    with it because it read the key into memory at startup. The only record of *which* key is
    still live would have been moved out of the way in the process.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _no_systemd_bin(tmp_path, agent="running")
    r = _run_with_systemd("homefree_disable", tmp_path, _unmanaged(env), bindir)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "still RUNNING" in out
    assert "pkill" in out, "the refusal must say how to stop it by hand"
    assert "Nothing has been changed" in out
    # The state assertions are the point: refuse *before* mutating, not after.
    assert (hf / "access_key").read_text() == key_before
    assert not (hf / "access_key.disabled").exists()


def test_rotate_refuses_while_an_unmanaged_agent_is_still_running(tmp_path):
    """Same boundary on rotation: a new key nobody accepts is a lockout, not a rotation.

    The running agent authenticates only against the key it loaded at startup, so printing the
    freshly generated one as live hands the operator a credential that cannot get in.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _no_systemd_bin(tmp_path, agent="running")
    r = _run_with_systemd("homefree_rotate_key", tmp_path, _unmanaged(env), bindir)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "still RUNNING" in out
    assert "Access key:" not in out, "a new key was printed as live"
    # Nothing was generated, swapped or rolled over — the refusal lands ahead of all of it.
    assert (hf / "access_key").read_text() == key_before
    assert not (hf / "access_key.prev").exists()
    assert not list(hf.glob("access_key.new.*"))


def test_already_disabled_still_refuses_while_an_unmanaged_agent_is_running(tmp_path):
    """ "Already disabled" is a success claim too, and it is false while an agent still serves.

    The key is gone from the live path, but an agent started before it was quarantined is
    still up and still holding it. Reporting "already disabled" is the same false assurance as
    reporting a fresh one, so that branch gets the same preflight.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    (hf / "access_key").rename(hf / "access_key.disabled")  # key already quarantined

    bindir = _no_systemd_bin(tmp_path, agent="running")
    r = _run_with_systemd("homefree_disable", tmp_path, _unmanaged(env), bindir)

    assert r.returncode != 0
    assert "still RUNNING" in (r.stdout + r.stderr)
    assert "already disabled" not in r.stdout


def test_disable_refuses_when_neither_probe_can_answer(tmp_path):
    """Unverifiable is not the same as stopped, and must not be collapsed into it.

    pgrep failing outright (exit >= 2) says nothing about whether an agent is running. Reading
    that as "found nothing" is precisely how a live agent slips through, so the command fails
    closed and names the override for an operator who can confirm by hand.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir = _no_systemd_bin(tmp_path, agent="unverifiable")
    r = _run_with_systemd("homefree_disable", tmp_path, _unmanaged(env), bindir)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "neither pgrep nor ps" in out
    assert "AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED=1" in out, "the way forward must be named"
    assert (hf / "access_key").read_text() == key_before
    assert not (hf / "access_key.disabled").exists()


def test_operator_may_confirm_the_agent_is_stopped_when_probes_are_unavailable(tmp_path):
    """The documented escape hatch works, so an unprobeable box is not permanently stuck.

    Without it a minimal container with no working pgrep or ps could never disable Home Free
    at all — a real regression. It is an explicit operator assertion rather than a silent
    fallback: the refusal above names it, and nothing reaches this path by default.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)

    bindir = _no_systemd_bin(tmp_path, agent="unverifiable")
    env = {**_unmanaged(env), "AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED": "1"}
    r = _run_with_systemd("homefree_disable", tmp_path, env, bindir)

    assert r.returncode == 0, r.stderr
    assert "quarantined" in r.stdout
    assert (hf / "access_key.disabled").exists()


def test_agent_probe_ignores_processes_that_merely_mention_the_module(tmp_path):
    """The probe matches a running interpreter, not any command line containing the string.

    An operator grepping the installer, an editor with this file open, or a config-management
    run all carry "agent_sessions.homefree" in their command line without being an agent. A
    substring match calls every one of them a live agent; the refusals are then noise, and
    noisy refusals get overridden by reflex — which is how the real one gets waved through.
    """
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    bindir = tmp_path / "nosystemd-mentions"
    bindir.mkdir()
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "systemctl").chmod(0o755)
    # No pgrep at all, so the ps branch is what gets exercised — and it is handed command
    # lines that talk about the module without being it.
    (bindir / "pgrep").write_text("#!/bin/sh\nexit 2\n")
    (bindir / "pgrep").chmod(0o755)
    (bindir / "ps").write_text(
        "#!/bin/sh\n"
        "echo 'grep -n agent_sessions.homefree install.sh'\n"
        "echo 'vim /home/u/install.sh -c /agent_sessions.homefree'\n"
        "echo '/bin/sh -c echo python -m agent_sessions.homefree'\n"
    )
    (bindir / "ps").chmod(0o755)

    r = _run_with_systemd("homefree_agent_state", tmp_path, _unmanaged(env), bindir)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "stopped", r.stdout

    # ...and the genuine article, launched the way the unit and the manual hint launch it.
    (bindir / "ps").write_text(
        "#!/bin/sh\n"
        "echo '/home/u/.local/share/agent-sessions/current/venv/bin/python "
        "-m agent_sessions.homefree'\n"
    )
    (bindir / "ps").chmod(0o755)
    r = _run_with_systemd("homefree_agent_state", tmp_path, _unmanaged(env), bindir)
    assert r.stdout.strip() == "running", r.stdout


# ---- the mode must be decided once, not re-probed mid-operation (#820 review, 3rd pass) ----


def _flapping_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    """A `systemctl` whose FIRST call succeeds and whose every later call fails.

    Models the user manager going away between the preflight and the action — a logout, a
    session teardown, a DBus restart. The first call is the mode decision; everything after
    it is the operation that was authorised on the strength of that decision.

    Returns (bindir, state_file); the caller passes the state path in via `$FLAP_STATE` so
    the counter lives in the test's tmp dir rather than anywhere shared.
    """
    bindir = tmp_path / "flapping"
    bindir.mkdir(exist_ok=True)
    state = tmp_path / "flap.count"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        'n="$(cat "$FLAP_STATE" 2>/dev/null || echo 0)"\n'
        'echo $((n + 1)) > "$FLAP_STATE"\n'
        '[ "$n" -eq 0 ] && exit 0\n'  # call 1: the availability probe — systemd is here
        "exit 1\n"  # every call after: the manager has gone away, probe and verb alike
    )
    stub.chmod(0o755)
    return bindir, state


def _run_flapping(snippet: str, tmp_path: Path, env: dict, bindir: Path, state: Path):
    return _run_with_systemd(snippet, tmp_path, {**env, "FLAP_STATE": str(state)}, bindir)


def test_disable_fails_when_systemd_vanishes_after_the_preflight(tmp_path):
    """A run that was authorised as systemd-managed must not fall back to "nothing to do".

    The preflight saw a user manager and let the operation through precisely *because*
    systemd would stop the agent. If the manager is gone by the time the stop is issued, the
    agent is still up holding the key — and treating that second failed probe as a legitimate
    no-op quarantines the key and reports revocation anyway. Same false success as before,
    reached through the back door of asking the question twice.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir, state = _flapping_systemctl(tmp_path)
    r = _run_flapping("homefree_disable", tmp_path, _unmanaged(env), bindir, state)

    assert r.returncode != 0, "disable reported success after systemd went away"
    assert "could not be stopped" in (r.stdout + r.stderr)
    assert (hf / "access_key").read_text() == key_before, "the key was quarantined anyway"
    assert not (hf / "access_key.disabled").exists()


def test_rotate_fails_when_systemd_vanishes_after_the_preflight(tmp_path):
    """Same for rotation: a key the running agent will never load is a lockout.

    The swap itself is correct and deliberate — the replacement must be on disk and validated
    before the old one stops being live — so the new key IS written here. What must not happen
    is the success banner: the agent is still authenticating with the old key.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()

    bindir, state = _flapping_systemctl(tmp_path)
    r = _run_flapping("homefree_rotate_key", tmp_path, _unmanaged(env), bindir, state)

    assert r.returncode != 0, "rotation reported success after systemd went away"
    out = r.stdout + r.stderr
    assert "still using the OLD key" in out
    assert "Access key:" not in out, "the new key was printed as live"
    # The swap DID happen and that is correct — the message says so, and says where the old
    # key is, so the operator is not left guessing which of the two the box will accept.
    assert (hf / "access_key").read_text() != key_before
    assert (hf / "access_key.prev").read_text() == key_before


def test_the_mode_is_decided_once_and_then_held(tmp_path):
    """The property itself, asserted directly rather than only through its symptoms.

    `homefree_select_mode` caches, so a second call must not re-probe — otherwise every
    caller downstream of the decision is exposed to a different answer than the one the
    operation was authorised under.
    """
    env = _stream_env(tmp_path)
    _setup_stream(tmp_path, env)
    bindir, state = _flapping_systemctl(tmp_path)

    r = _run_flapping(
        'homefree_select_mode; homefree_select_mode; homefree_select_mode; echo "$HOMEFREE_MODE"',
        tmp_path,
        _unmanaged(env),
        bindir,
        state,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "systemd"
    # Three calls, exactly one probe — the later ones never touched systemctl.
    assert state.read_text().strip() == "1", f"re-probed: {state.read_text()!r}"


# ---- the lifecycle is one transaction, not a sequence of careful steps (#820 review) ----


def _blocking_systemctl(tmp_path: Path, *, verb: str) -> tuple[Path, Path, Path]:
    """A `systemctl` that parks inside `verb` until released.

    This is what makes the concurrency tests deterministic rather than timing races: the first
    command is held *inside* its transaction, at a known point, until the test says otherwise.
    Returns (bindir, reached_marker, go_marker).
    """
    bindir = tmp_path / f"blocking-{verb}"
    bindir.mkdir(exist_ok=True)
    reached, go = tmp_path / "reached", tmp_path / "go"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        '[ "$#" -le 1 ] && exit 0\n'  # availability probe
        f'if [ "$2" = "{verb}" ]; then\n'
        '  : > "$HF_REACHED"\n'
        '  while [ ! -f "$HF_GO" ]; do sleep 0.05; done\n'
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bindir, reached, go


def _spawn(snippet: str, tmp_path: Path, env: dict, bindir: Path, reached: Path, go: Path):
    """Start a lifecycle command in the background and wait until it is inside its transaction."""
    base = {k: v for k, v in os.environ.items() if k not in _LEAK_KEYS}
    base.update(env)
    base.update(
        {
            "AGENT_SESSIONS_NO_SERVICE": "0",
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HF_REACHED": str(reached),
            "HF_GO": str(go),
        }
    )
    src = _sourceable(tmp_path)
    proc = subprocess.Popen(
        ["sh", "-c", f'. "{src}"; {snippet}'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=base,
        # Own session, so a test can signal the whole command tree as a unit. A shell cannot
        # run a TERM trap while it is blocked waiting on a foreground child, so signalling
        # only the shell would hang against a stub that is parked by design.
        start_new_session=True,
    )
    for _ in range(600):  # up to ~30s; the marker, not a sleep, is what we wait on
        if reached.exists():
            return proc
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"first command exited early: rc={proc.returncode} {out}{err}")
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("first command never reached the blocking point")


def _second(
    snippet: str, tmp_path: Path, env: dict, bindir: Path, reached: Path, go: Path, timeout=20
):
    """Run the second lifecycle command, BOUNDED.

    The bound is load-bearing rather than defensive. With no lock the second command walks
    straight into the transaction and parks on the same blocking stub, so an unbounded run
    hangs the whole suite instead of failing it — which is exactly what happened when this
    was first written against the unfixed script. A timeout here *is* the failure: it means
    the command was not refused.
    """
    base = {k: v for k, v in os.environ.items() if k not in _LEAK_KEYS}
    base.update(env)
    base.update(
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AGENT_SESSIONS_NO_SERVICE": "0",
            "HF_REACHED": str(reached),
            "HF_GO": str(go),
        }
    )
    src = _sourceable(tmp_path)
    try:
        return subprocess.run(
            ["sh", "-c", f'. "{src}"; {snippet}'],
            capture_output=True,
            text=True,
            env=base,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - only on a regression
        raise AssertionError(
            "the second lifecycle command was not refused: it entered the transaction and "
            "blocked alongside the first, which is the interleaving the lock exists to stop"
        ) from exc


def test_a_concurrent_disable_cannot_interleave_with_a_rotation(tmp_path):
    """The interleaving that leaves a box streaming under a key the operator was told is dead.

    Disable's own steps are all correct; so are rotation's. Run together they compose into a
    state neither can reach alone — disable stops the unit and quarantines the key, the
    rotation (already past its preflight) writes a fresh key and restarts the unit, and the
    box is live again on a credential the operator has just been told was revoked.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    bindir, reached, go = _blocking_systemctl(tmp_path, verb="restart")

    rot = _spawn("homefree_rotate_key", tmp_path, _unmanaged(env), bindir, reached, go)
    try:
        r = _second("homefree_disable", tmp_path, _unmanaged(env), bindir, reached, go)
        assert r.returncode != 0, "disable ran while a rotation was mid-transaction"
        assert "already running" in (r.stdout + r.stderr)
        assert not (hf / "access_key.disabled").exists(), "the key was quarantined mid-rotation"
    finally:
        go.touch()
        rot.communicate(timeout=60)
    assert rot.returncode == 0, "the rotation did not complete once it was left alone"


def test_two_rotations_cannot_race_away_the_rollback_key(tmp_path):
    """Concurrent rotations cost the roll-back: `access_key.prev` skips the intervening key.

    Both would report success, and the key `access_key.prev` names would be one the operator
    never saw as live — so the documented way back points at the wrong credential. That is a
    silent failure of the recovery path the whole rotation design is built around.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    first = (hf / "access_key").read_text()
    bindir, reached, go = _blocking_systemctl(tmp_path, verb="restart")

    rot = _spawn("homefree_rotate_key", tmp_path, _unmanaged(env), bindir, reached, go)
    try:
        r = _second("homefree_rotate_key", tmp_path, _unmanaged(env), bindir, reached, go)
        assert r.returncode != 0, "a second rotation ran while the first was mid-transaction"
        assert "already running" in (r.stdout + r.stderr)
    finally:
        go.touch()
        rot.communicate(timeout=60)
    assert rot.returncode == 0
    # Exactly one rotation happened, so the roll-back names the key that was actually live.
    assert (hf / "access_key.prev").read_text() == first
    assert (hf / "access_key").read_text() != first


def test_a_stale_lock_from_a_crashed_run_is_taken_over(tmp_path):
    """A crashed run must not wedge the box out of its own security commands forever.

    Taken over only when the owner is provably gone — never on age, which cannot tell a crash
    from a slow `systemctl`.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    lock = hf / ".lifecycle.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999\n")  # a PID that is not running

    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert (hf / "access_key.disabled").exists()


def test_a_live_lock_is_not_stolen(tmp_path):
    """The other half: an owner that IS alive keeps the lock, or the lock means nothing."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    lock = hf / ".lifecycle.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")  # pytest itself — definitely alive

    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode != 0
    assert "already running" in (r.stdout + r.stderr)
    assert not (hf / "access_key.disabled").exists()


def test_rotation_aborts_when_the_rollback_copy_cannot_be_written(tmp_path):
    """A rotation that cannot keep the old key must not rotate — and must not claim it did.

    The old code let the copy fail silently, replaced the live key, and printed "previous key
    kept at ...". The operator is then told a way back exists for a credential that is already
    gone, which is worse than having no roll-back at all: they stop looking for one.
    The failure is forced for real — a directory where the temp file must be written — rather
    than by stubbing `cp`, which would only prove something about the stub.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()
    (hf / "access_key.prev.tmp").mkdir()  # the roll-back temp path is not writable as a file

    r = _run("homefree_rotate_key", tmp_path, env)

    assert r.returncode != 0, "rotation proceeded without a roll-back copy"
    out = r.stdout + r.stderr
    assert "roll-back copy" in out
    assert "Access key:" not in out, "a new key was printed as live"
    # Aborted with the live key untouched — the whole point of doing this before the swap.
    assert (hf / "access_key").read_text() == key_before
    assert not (hf / "access_key.prev").exists()


# ---- the lock must be ownership-safe, and shared by every mutator (#820 review, 5th pass) ----


def test_a_lock_with_no_owner_is_not_stolen(tmp_path):
    """The publication window: created, but its PID not yet written.

    `mkdir` and the PID write cannot be one operation, so there is always an instant where a
    live holder owns a lock that names nobody. From the outside that is indistinguishable from
    a crash — so reading it as "stale" walks a second command straight through a live fence.
    Refusing is the only safe reading, and a retry a moment later costs nothing.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()
    lock = hf / ".lifecycle.lock"
    lock.mkdir()  # created; the owner has not published itself yet

    r = _run("homefree_disable", tmp_path, env)

    assert r.returncode != 0, "an unattributable lock was stolen"
    assert "names no owner" in (r.stdout + r.stderr)
    assert lock.exists(), "the live lock was removed"
    assert (hf / "access_key").read_text() == key_before
    assert not (hf / "access_key.disabled").exists()


def test_a_malformed_owner_is_not_stolen_either(tmp_path):
    """Garbage in the PID file is unverifiable, and unverifiable is not dead."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    lock = hf / ".lifecycle.lock"
    lock.mkdir()
    (lock / "pid").write_text("not-a-pid\n")

    r = _run("homefree_disable", tmp_path, env)
    assert r.returncode != 0
    assert "names no owner" in (r.stdout + r.stderr)
    assert not (hf / "access_key.disabled").exists()


def test_release_does_not_remove_a_lock_taken_over_by_someone_else(tmp_path):
    """Release is ownership-checked, not "clear whatever is at the path".

    After a takeover the directory at that path is somebody else's fence. Removing it on our
    way out would drop a lock we do not hold, while its owner keeps working inside it.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    r = _run(
        'homefree_lock_acquire "t"; echo 424242 > "$HOMEFREE_LOCKDIR/pid"; '
        'homefree_lock_release; [ -d "$HOMEFREE_LOCKDIR" ] && echo KEPT',
        tmp_path,
        env,
    )
    assert r.returncode == 0, r.stderr
    assert "KEPT" in r.stdout, "a lock owned by another process was removed"
    assert (hf / ".lifecycle.lock").exists()


def test_enabling_streaming_joins_the_same_transaction(tmp_path):
    """Setup writes the key and starts the unit, so it is a mutator and needs the same fence.

    Without this a reinstall could recreate the key and restart streaming while a disable was
    reporting it off, or invalidate the snapshot a rotation had already acted on.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    lock = hf / ".lifecycle.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")  # pytest itself — definitely alive
    (hf / "access_key").unlink()  # so a setup that ran would visibly recreate it

    r = _run("homefree_setup", tmp_path, env)

    assert r.returncode != 0, "setup ran while a lifecycle command held the lock"
    assert "already running" in (r.stdout + r.stderr)
    assert not (hf / "access_key").exists(), "setup recreated the key inside another fence"


def test_a_terminated_command_releases_the_lock_and_stops(tmp_path):
    """A handled signal must terminate, not just tidy up and carry on.

    Releasing the fence and then continuing is the one thing the lock forbids: the rest of the
    operation would run outside it, which is worse than never having taken it.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    bindir, reached, go = _blocking_systemctl(tmp_path, verb="restart")

    # No race between the signal and the handler: `_spawn` returns only once the stub has
    # written its marker, and the stub only runs from INSIDE the restart verb — which is
    # reached long after homefree_lock_acquire installed the traps. So the signal is always
    # delivered to a shell that already has a TERM handler; there is no window in which the
    # default disposition could decide the outcome instead.
    proc = _spawn("homefree_rotate_key", tmp_path, _unmanaged(env), bindir, reached, go)
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    out, err = proc.communicate(timeout=30)

    # WHAT DISCRIMINATES IS THE ABSENCE OF THE INTERRUPTED STEP'S OWN MESSAGE, not the exit
    # code. A trap that merely tidies up and returns lets the script RESUME: the interrupted
    # restart then fails, and rotation's `die` reports "still using the OLD key". A handler
    # that terminates never reaches that line, so the run says nothing at all. Measured on
    # both variants — the tidy-up trap prints exactly that message and exits 1; the
    # terminating handler prints nothing and exits 143.
    #
    # The exit code is asserted too, but only as a corroborating detail; on its own it is a
    # numeric outcome rather than evidence of cause.
    assert "still using the OLD key" not in (out + err), (
        "the script resumed past the signal — it ran the interrupted restart and reported it, "
        "which means the handler released the fence and let the operation continue outside it"
    )
    assert (out + err).strip() == "", f"unexpected output after the signal: {(out + err)!r}"
    assert proc.returncode == 143, f"not terminated by the handler (rc={proc.returncode})"
    assert not (hf / ".lifecycle.lock").exists(), "the lock was stranded"


def test_rotation_refuses_a_rollback_destination_that_is_a_directory(tmp_path):
    """`mv -f file DIR` SUCCEEDS by moving the file *inside* DIR — so the move proves nothing.

    Rotation would then swap the live key and announce a roll-back at a path that is not the
    file it names. The earlier regression made `access_key.prev.tmp` a directory, which fails
    at the write and never exercises this `mv` behaviour at all.
    """
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()
    (hf / "access_key.prev").mkdir()

    r = _run("homefree_rotate_key", tmp_path, env)

    assert r.returncode != 0, "rotation accepted a directory as the roll-back file"
    out = r.stdout + r.stderr
    assert "not a regular file" in out
    assert "Access key:" not in out, "a new key was printed as live"
    assert (hf / "access_key").read_text() == key_before
    assert (hf / "access_key.prev").is_dir()  # untouched, not silently populated


def test_rotation_refuses_a_rollback_destination_that_is_a_symlink(tmp_path):
    """Same for a symlink: the roll-back must be the file the message names, not a redirect."""
    env = _stream_env(tmp_path)
    hf = _setup_stream(tmp_path, env)
    key_before = (hf / "access_key").read_text()
    (hf / "access_key.prev").symlink_to(tmp_path / "somewhere-else")

    r = _run("homefree_rotate_key", tmp_path, env)

    assert r.returncode != 0
    assert "not a regular file" in (r.stdout + r.stderr)
    assert (hf / "access_key").read_text() == key_before
