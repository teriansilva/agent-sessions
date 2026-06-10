"""Per-session transient scopes (#346 Phase B).

Unit tests cover the wrapper/fallback ladder with the probe mocked; the integration
tests at the bottom run only where a working `systemd-run --user` exists (CI runners
and dev hosts without a user manager skip them, the staging/prod host runs them).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess

import pytest

from agent_sessions import scopedspawn

ARGV = ["/usr/bin/dtach", "-c", "/tmp/x.sock", "-z", "-E", "-r", "winch", "/bin/agent"]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    scopedspawn.reset_cache_for_tests()
    monkeypatch.delenv("AGENT_SESSIONS_SESSION_SCOPES", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_SCOPE_PROPERTIES", raising=False)
    yield
    scopedspawn.reset_cache_for_tests()


def test_disabled_via_env_passes_through(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_SESSION_SCOPES", "0")
    argv, unit = scopedspawn.wrap(ARGV, engine="claude", session_id="abc")
    assert argv == ARGV and unit is None


def test_unavailable_probe_passes_through(monkeypatch):
    monkeypatch.setattr(scopedspawn, "_probe", lambda: False)
    argv, unit = scopedspawn.wrap(ARGV, engine="claude", session_id="abc")
    assert argv == ARGV and unit is None


def test_wrap_structure_and_payload_preserved(monkeypatch):
    monkeypatch.setattr(scopedspawn, "_probe", lambda: True)
    argv, unit = scopedspawn.wrap(ARGV, engine="claude", session_id="abcdef12-3456")
    assert unit is not None and unit.startswith("as-claude-abcdef12-") and unit.endswith(".scope")
    assert argv[1:5] == ["--user", "--scope", "--collect", "--quiet"]
    assert f"--unit={unit}" in argv
    # the payload argv survives verbatim after the `--` separator
    assert argv[argv.index("--") + 1 :] == ARGV
    # default per-session task budget present as a -p property
    assert "TasksMax=512" in argv


def test_unit_names_never_collide_on_rapid_relaunch(monkeypatch):
    # kill → instant relaunch must never hit `unit already exists` while --collect GC
    # lags: the nonce makes every wrap unique even for the same engine+sid.
    monkeypatch.setattr(scopedspawn, "_probe", lambda: True)
    names = {scopedspawn.wrap(ARGV, engine="claude", session_id="same-sid")[1] for _ in range(100)}
    assert len(names) == 100


def test_properties_validated_against_injection(monkeypatch):
    monkeypatch.setattr(scopedspawn, "_probe", lambda: True)
    monkeypatch.setenv(
        "AGENT_SESSIONS_SCOPE_PROPERTIES",
        "TasksMax=512 MemoryHigh=4G bad;rm=-rf --evil also=bad$(x)",
    )
    argv, _ = scopedspawn.wrap(ARGV, engine="claude", session_id="abc")
    assert "TasksMax=512" in argv and "MemoryHigh=4G" in argv
    assert not any("rm" in a or "evil" in a or "$(" in a for a in argv)


def test_failed_probe_recovers_after_cooldown(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return calls["n"] > 1  # first probe fails, later ones succeed

    monkeypatch.setattr(scopedspawn, "_probe", flaky)
    assert scopedspawn.available() is False
    # within the cooldown the cached failure holds (no re-probe storm per launch)
    assert scopedspawn.available() is False and calls["n"] == 1
    # past the cooldown the ladder recovers
    monkeypatch.setattr(scopedspawn, "_REPROBE_AFTER_S", 0.0)
    assert scopedspawn.available() is True


def test_unsafe_engine_and_sid_sanitized(monkeypatch):
    monkeypatch.setattr(scopedspawn, "_probe", lambda: True)
    _, unit = scopedspawn.wrap(ARGV, engine="we ird/✓", session_id="../../etc")
    assert unit is not None
    # systemd unit charset: nothing outside [A-Za-z0-9_.\-] plus the .scope suffix
    body = unit.removesuffix(".scope")
    assert all(c.isalnum() or c in "_.-" for c in body)
    assert "/" not in unit and " " not in unit


# ---- integration: real systemd-run (skipped where the user manager is absent) ----

_HAVE_SCOPES = scopedspawn._probe()


@pytest.mark.skipif(not _HAVE_SCOPES, reason="no working systemd-run --user on this host")
def test_real_scope_spawn_and_collect():
    # A wrapped trivial command runs to completion and the scope self-collects.
    argv, unit = scopedspawn.wrap(["/bin/true"], engine="test", session_id="itest")
    assert unit is not None
    r = subprocess.run(argv, capture_output=True, timeout=15)
    assert r.returncode == 0


@pytest.mark.skipif(not _HAVE_SCOPES, reason="no working systemd-run --user on this host")
def test_lock_fd_inherited_through_systemd_run(tmp_path, monkeypatch):
    # THE critical contract (#346): the single-writer flock fd passed via pass_fds must
    # survive the systemd-run exec chain into the long-lived payload, exactly as it does
    # for a directly spawned dtach master. Holder = a sleeping child; while it lives the
    # lock must read as held even after the parent closes its copy.
    from agent_sessions import sessionlock

    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    key = "claude:scope-lockfd"
    lock = sessionlock.acquire(key)
    assert lock is not None
    argv, unit = scopedspawn.wrap(["/bin/sleep", "30"], engine="claude", session_id="lockfd")
    assert unit is not None

    async def spawn():
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(lock.fd,),
        )

    proc = asyncio.run(spawn())
    try:
        # parent hands off: close our fd copy — the child's inherited fd keeps the flock
        lock.transfer()
        assert sessionlock.is_locked(key) is True
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        asyncio.run(_wait(proc))
    assert sessionlock.is_locked(key) is False


async def _wait(proc):
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=10)
