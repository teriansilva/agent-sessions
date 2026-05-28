"""Session continuity across broker death (#165 slice 1).

The hard requirement: when the agent-sessions broker dies (service restart, deploy,
crash), the dtach master and the agent process it spawned MUST keep running, so the
user's in-flight conversation is preserved. A new broker then reattaches via
`dtach -a` to the still-live master.

These tests exercise the underlying dtach contract end-to-end — they use a real
`dtach` binary and a real subprocess, skipping cleanly if dtach isn't available.
"""

from __future__ import annotations

import os
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


def _wait_for_sock(path, timeout=2.0) -> bool:
    """Spin until the sock exists and a connect() succeeds, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.1)
            try:
                s.connect(str(path))
                return True
            except OSError:
                pass
            finally:
                s.close()
        time.sleep(0.05)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_dtach_master_survives_spawner_death():
    """The core invariant for #165: killing the process that spawned `dtach -c`
    (analogous to the broker exiting on a deploy) leaves the dtach master + its
    child command alive. Without this property, every deploy would kill agents."""
    sock = ptybridge.socket_path("test", "survive")
    # Use launch_argv (the production code path) to confirm it produces a working
    # create-only invocation that survives the spawner.
    argv = ptybridge.launch_argv(
        engine="test", session_id="survive", launch_argv=["/bin/sleep", "60"]
    )
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
        spawner.wait(timeout=2)

        # Give the dtach client a moment to notice spawner death; the MASTER stays up.
        time.sleep(0.3)
        assert sock.exists(), "sock file vanished after spawner died"
        assert ptybridge.session_exists(
            "test", "survive"
        ), "session_exists reports dead despite live master"

        # The sleep process — the analogue of `claude --resume <uuid>` — is still alive.
        # Find it via pgrep so we don't depend on the dtach PID tree shape.
        ps = subprocess.run(
            ["pgrep", "-f", f"sleep 60.*|.*{sock}.*sleep"],
            capture_output=True,
            text=True,
        )
        # Either the sleep itself or the dtach master must still be alive.
        alive = subprocess.run(["pgrep", "-fa", "dtach"], capture_output=True, text=True)
        assert (
            str(sock) in alive.stdout or "sleep 60" in ps.stdout
        ), f"no surviving dtach master for sock {sock}"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Tear down: kill surviving dtach masters referencing this sock + the sleep child.
        for line in subprocess.run(
            ["pgrep", "-fa", "dtach"], capture_output=True, text=True
        ).stdout.splitlines():
            if str(sock) in line:
                pid = int(line.split()[0])
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        subprocess.run(["pkill", "-9", "-f", "sleep 60"], check=False)
        if sock.exists():
            sock.unlink()


def test_dtach_a_attaches_to_a_surviving_master():
    """After the broker has died and a new one comes up, `dtach -a <sock>` must
    succeed against the surviving master. This is what makes reconnect-after-deploy
    invisible to the user."""
    sock = ptybridge.socket_path("test", "reattach")
    argv = ptybridge.launch_argv(
        engine="test", session_id="reattach", launch_argv=["/bin/sleep", "60"]
    )
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
        assert _wait_for_sock(sock)
        spawner.terminate()
        spawner.wait(timeout=2)
        time.sleep(0.3)
        # Sanity: the master must actually be alive at this point, otherwise the dtach -a
        # below would correctly fail and we'd be testing the wrong thing.
        assert ptybridge.session_exists(
            "test", "reattach"
        ), "master died with the spawner — KillMode/start_new_session/setsid escape failed"

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
        for line in subprocess.run(
            ["pgrep", "-fa", "dtach"], capture_output=True, text=True
        ).stdout.splitlines():
            if str(sock) in line:
                pid = int(line.split()[0])
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        subprocess.run(["pkill", "-9", "-f", "sleep 60"], check=False)
        if sock.exists():
            sock.unlink()
