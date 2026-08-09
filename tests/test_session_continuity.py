"""Session continuity across broker death (#165 slice 1).

The hard requirement: when the agent-sessions broker dies (service restart, deploy,
crash), the dtach master and the agent process it spawned MUST keep running, so the
user's in-flight conversation is preserved. A new broker then reattaches via
`dtach -a` to the still-live master.

These tests exercise the underlying dtach contract end-to-end — they use a real
`dtach` binary and a real subprocess, skipping cleanly if dtach isn't available.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time

import pytest

from agent_sessions import ptybridge

_DTACH = shutil.which("dtach")
pytestmark = pytest.mark.skipif(_DTACH is None, reason="dtach binary not available")


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))
    yield tmp_path / "pty"


# Deadlines here are generous ON PURPOSE (#624). These tests share a runner box with up to
# three concurrent ~20-minute `pr-validate` suites, and at load average 27 a 300 ms bet on
# "the master has come up AND the spawner has been reaped" simply loses — `pr-validate` then
# goes red on PRs that never touched the backend (measured twice in the same second on
# 2026-07-10, on a JSX-string-only diff and an unrelated PR).
#
# A longer deadline cannot mask a regression, because the invariant is one-directional: a
# master that died with its spawner never comes back to life, so polling can only remove false
# NEGATIVES. On an idle host every wait below returns on its first poll and costs nothing.
_STARTUP_TIMEOUT_S = 10.0
_SURVIVAL_TIMEOUT_S = 5.0


def _wait_until(pred, timeout: float, interval: float = 0.05) -> bool:
    """Poll ``pred`` until true or the deadline passes. Returns whether it became true."""
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _wait_for_sock(path, timeout=_STARTUP_TIMEOUT_S) -> bool:
    """Spin until the sock exists and a connect() succeeds, or timeout."""

    def _connectable() -> bool:
        if not path.exists():
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.1)
        try:
            s.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            s.close()

    return _wait_until(_connectable, timeout)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _sleep_child() -> tuple[list[str], str]:
    """A ~60s stand-in for the agent process, with a cmdline UNIQUE to this pytest process.

    These tests used to spawn a plain `/bin/sleep 60` and tear it down with a host-wide
    `pkill -9 -f "sleep 60"`. The runner is shared: two PRs' `pr-validate` jobs run
    concurrently on the same box, so one run's teardown reaped the OTHER run's child — the
    orphaned dtach master then exited and the other run failed with "master died with the
    spawner" / "no surviving dtach master". Observed on 2026-07-10, when PR #620 and PR #621
    each failed a different test in this file at the same second.

    `sleep` takes a fractional duration, so the pid makes the argv unique without changing
    what is being tested (a long-lived child that outlives its spawner).

    The base is 61, not 60, and that matters: `pkill -f` takes an unanchored REGEX, so the legacy
    `pkill -9 -f "sleep 60"` still matches a cmdline of `/bin/sleep 60.12345`. Any branch that has
    not yet picked up this commit would keep reaping our children. `sleep 61.12345` does not match
    it, so this test file is immune to concurrent runs of its own older self — which is what made
    the fix land in the first place (task 6341 killed task 6344's child one second after start).
    """
    marker = f"61.{os.getpid() % 100000:05d}"
    return ["/bin/sleep", marker], marker


def _reap(sock, marker: str) -> None:
    """Kill only THIS test's dtach master and its child, then drop the socket.

    Scoped two ways, both process-local: masters are matched by our unique socket path, and
    the child is killed via `pkill -P <master>` (its own children) plus a fallback on our
    unique marker for the case where the master already died and orphaned it.
    """
    for line in subprocess.run(
        ["pgrep", "-fa", "dtach"], capture_output=True, text=True
    ).stdout.splitlines():
        if str(sock) in line:
            pid = int(line.split()[0])
            subprocess.run(["pkill", "-9", "-P", str(pid)], check=False)  # the master's child
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
    # An orphaned child (master already dead) — matched on OUR marker, never a bare "sleep 60".
    subprocess.run(["pkill", "-9", "-f", re.escape(f"sleep {marker}")], check=False)
    if sock.exists():
        sock.unlink()


def test_dtach_master_survives_spawner_death():
    """The core invariant for #165: killing the process that spawned `dtach -c`
    (analogous to the broker exiting on a deploy) leaves the dtach master + its
    child command alive. Without this property, every deploy would kill agents."""
    sock = ptybridge.socket_path("test", "survive")
    child, marker = _sleep_child()
    # Use launch_argv (the production code path) to confirm it produces a working
    # create-only invocation that survives the spawner.
    argv = ptybridge.launch_argv(engine="test", session_id="survive", launch_argv=child)
    # dtach -c attaches in the foreground and refuses to start without a tty — the
    # production broker provides one via os.openpty(); we do the same here.
    master_fd, slave_fd = os.openpty()
    spawner = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)  # parent only needs the master end
    try:
        assert _wait_for_sock(sock), "dtach master never came up"

        # Killing the spawner is the test's stand-in for the broker process exiting
        # (e.g. systemctl --user restart agent-sessions). With KillMode=process the
        # broker's main PID dies but dtach + its child are not in the kill scope.
        spawner.terminate()
        spawner.wait(timeout=_SURVIVAL_TIMEOUT_S)

        # Wait for the CONDITION, not for a fixed number of milliseconds: the dtach client
        # needs a moment to notice the spawner died, and how long that takes is a property of
        # host load, not of the invariant under test. The MASTER stays up.
        assert _wait_until(
            lambda: sock.exists() and ptybridge.session_exists("test", "survive"),
            _SURVIVAL_TIMEOUT_S,
        ), "master died with the spawner — KillMode/start_new_session/setsid escape failed"

        # The child — the analogue of `claude --resume <uuid>` — is still alive. Match OUR
        # marker: a bare "sleep 60" would also match a concurrent run's child and pass falsely.
        ps = subprocess.run(
            ["pgrep", "-f", re.escape(f"sleep {marker}")], capture_output=True, text=True
        )
        # Either the child itself or the dtach master must still be alive.
        alive = subprocess.run(["pgrep", "-fa", "dtach"], capture_output=True, text=True)
        assert (
            str(sock) in alive.stdout or ps.stdout.strip()
        ), f"no surviving dtach master for sock {sock}"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        _reap(sock, marker)


def test_dtach_a_attaches_to_a_surviving_master():
    """After the broker has died and a new one comes up, `dtach -a <sock>` must
    succeed against the surviving master. This is what makes reconnect-after-deploy
    invisible to the user."""
    sock = ptybridge.socket_path("test", "reattach")
    child, marker = _sleep_child()
    argv = ptybridge.launch_argv(engine="test", session_id="reattach", launch_argv=child)
    # dtach -c attaches in the foreground and refuses to start without a tty — the
    # production broker provides one via os.openpty(); we do the same here.
    master_fd, slave_fd = os.openpty()
    spawner = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)  # parent only needs the master end
    try:
        # Two distinct failure modes, two distinct messages (#624). They used to collapse into
        # one assertion that reported "died with the spawner" for a master which had simply not
        # finished starting — a misdiagnosis that sent people at `start_new_session` when the
        # real answer was host load.
        assert _wait_for_sock(sock), "master never came up (start-up, not survival)"
        spawner.terminate()
        spawner.wait(timeout=_SURVIVAL_TIMEOUT_S)
        # Sanity: the master must actually be alive at this point, otherwise the dtach -a
        # below would correctly fail and we'd be testing the wrong thing.
        assert _wait_until(
            lambda: ptybridge.session_exists("test", "reattach"), _SURVIVAL_TIMEOUT_S
        ), "master came up, then died with the spawner — KillMode/start_new_session escape failed"

        # New broker would now spawn `dtach -a <sock>` — verify that works. Like -c,
        # dtach -a wants a tty; production gives it openpty(), we mirror that here.
        # Mirror production exactly: PTY pair for stdin/stdout/stderr (mixing a PIPE for
        # stderr fights dtach's controlling-terminal expectations and makes it exit 1).
        m2, s2 = os.openpty()
        attach = subprocess.Popen(
            ptybridge.attach_argv(engine="test", session_id="reattach"),
            stdin=s2,
            stdout=s2,
            stderr=s2,
            start_new_session=True,
            close_fds=True,
        )
        os.close(s2)
        try:
            # Give dtach -a a moment: a failed attach exits quickly; a success keeps
            # running until the master ends or the client is killed.
            time.sleep(0.5)
            assert (
                attach.poll() is None
            ), f"dtach -a failed to attach to the surviving master (exited {attach.returncode})"
        finally:
            attach.terminate()
            try:
                attach.wait(timeout=2)
            except subprocess.TimeoutExpired:
                attach.kill()
            try:
                os.close(m2)
            except OSError:
                pass
    finally:
        _reap(sock, marker)
