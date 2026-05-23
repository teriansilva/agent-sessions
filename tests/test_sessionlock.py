"""The rock-solid session guarantee: a session id can be resumed by at most one
process at a time (no double-resume / double-write), and the open policy attaches
rather than relaunching. See docs/session-handling.md."""

from __future__ import annotations

import pytest

from agent_sessions import ptybridge, sessionlock, sessions


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))


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
