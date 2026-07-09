"""The rock-solid session guarantee: a session id can be resumed by at most one
process at a time (no double-resume / double-write), and the open policy attaches
rather than relaunching. See docs/session-handling.md."""

from __future__ import annotations

import socket

import pytest

from agent_sessions import ptybridge, sessionlock, sessions


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))


# ---- the single-writer lock --------------------------------------------------


def test_lock_path_sanitizes_key():
    # Path separators are squashed so a key can never escape the lock dir; literal
    # dots are harmless without a separator. The file must stay inside lock_dir().
    p = sessionlock.lock_path("claude:6a73-bad/../x")
    assert p.name.endswith(".lock") and "/" not in p.name
    assert p.parent == sessionlock.lock_dir()


def test_second_acquire_of_same_key_is_blocked():
    # THE no-double-resume guarantee: while one holder has the lock, a second
    # acquire of the same key (a separate open file description = models a second
    # process / app instance) fails → the caller must attach, not relaunch.
    first = sessionlock.acquire("claude:abc")
    assert first is not None
    assert sessionlock.acquire("claude:abc") is None  # contended → no second writer
    assert sessionlock.is_locked("claude:abc") is True
    first.release()
    # released → re-acquirable
    again = sessionlock.acquire("claude:abc")
    assert again is not None
    again.release()


def test_distinct_keys_dont_contend():
    a = sessionlock.acquire("claude:one")
    b = sessionlock.acquire("opencode:two")
    assert a is not None and b is not None
    a.release()
    b.release()


def test_lock_is_a_context_manager():
    with sessionlock.acquire("claude:ctx") as lk:
        assert lk is not None
        assert sessionlock.is_locked("claude:ctx") is True
    assert sessionlock.is_locked("claude:ctx") is False  # released on exit


# ---- open-or-attach policy ---------------------------------------------------


def test_open_action_attaches_when_master_exists(monkeypatch):
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: True)
    action, lock = sessions.open_action("claude", "abc")
    assert action == sessions.ATTACH and lock is None


def test_open_action_launches_when_free(monkeypatch):
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    action, lock = sessions.open_action("claude", "free")
    assert action == sessions.LAUNCH and lock is not None
    # holding the lock means a concurrent open is told it's busy, not launched twice
    assert sessions.open_action("claude", "free")[0] == sessions.BUSY
    lock.release()


def test_open_action_busy_when_locked_elsewhere(monkeypatch):
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    held = sessionlock.acquire("claude:held")  # another instance holds it, no local socket
    assert held is not None
    action, lock = sessions.open_action("claude", "held")
    assert action == sessions.BUSY and lock is None
    held.release()


def test_open_action_launch_unlinks_stale_sock(monkeypatch):
    """#165: under the held lock, an orphan `.sock` from a dead master is unambiguously
    stale (no one else can be racing for this key) — open_action's LAUNCH path must
    unlink it so the caller's subsequent `dtach -c` can `bind()` cleanly. Without this,
    `dtach -c` would fail with EADDRINUSE on the orphan file."""
    # Real ptybridge.session_exists is used (no monkeypatch): a bound-but-not-listening
    # orphan sock is what we set up; the connect-probe correctly classifies it dead.
    orphan = ptybridge.socket_path("claude", "ghost")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(orphan))
    s.close()  # file persists; nothing listening
    assert orphan.exists()

    action, lock = sessions.open_action("claude", "ghost")
    try:
        assert action == sessions.LAUNCH
        assert lock is not None
        # The stale sock has been removed under the lock — dtach -c can now bind cleanly.
        assert not orphan.exists()
    finally:
        if lock is not None:
            lock.release()


def test_open_action_attaches_only_when_master_is_actually_accepting(monkeypatch):
    """#165: ATTACH must be based on a live listener, not just a file. An orphan sock
    is treated as not-alive (connect-probe in session_exists) and routes to LAUNCH."""
    # The actual ptybridge.session_exists is exercised; an orphan sock returns False.
    orphan = ptybridge.socket_path("claude", "dead")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(orphan))
    s.close()

    action, lock = sessions.open_action("claude", "dead")
    try:
        # Dead master + acquirable lock → LAUNCH (not the historical "ATTACH-on-file"
        # bug where the client would have hit dtach -a → EOF on a phantom master).
        assert action == sessions.LAUNCH
        assert lock is not None
        assert not orphan.exists()  # cleaned up too
    finally:
        if lock is not None:
            lock.release()


def test_open_action_race_guard_attaches_if_master_appears(monkeypatch):
    # No socket on the first check, but a master appears before the post-acquire
    # recheck → release the just-won lock and ATTACH (never launch a duplicate).
    calls = {"n": 0}

    def exists(_e, _n):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(ptybridge, "session_exists", exists)
    action, lock = sessions.open_action("claude", "racy")
    assert action == sessions.ATTACH and lock is None
    assert sessionlock.is_locked("claude:racy") is False  # the won lock was released


# ---- lock handoff to the agent master ----------------------------------------


def test_transfer_hands_flock_to_inheriting_child():
    # THE cross-instance / restart guarantee: passing the lock fd to a long-lived
    # child (stand-in for the dtach master) and closing our fd WITHOUT unlocking keeps
    # the flock held for the child's lifetime — so another instance still can't resume
    # the id, and the lock frees only when the master dies.
    import subprocess
    import time

    lock = sessionlock.acquire("claude:xfer")
    assert lock is not None
    child = subprocess.Popen(["sleep", "10"], pass_fds=(lock.fd,))
    try:
        lock.transfer()  # close our fd, no LOCK_UN → the child still holds the flock
        assert sessionlock.is_locked("claude:xfer") is True  # held by the inheriting child
    finally:
        child.terminate()
        child.wait()
    time.sleep(0.1)
    assert sessionlock.is_locked("claude:xfer") is False  # released when the master died
