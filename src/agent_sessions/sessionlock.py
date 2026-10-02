"""Single-writer session lock — the rock-solid guarantee that a session id is
resumed by **at most one process at a time** (no double-resume, no double-write),
even across separate app instances on the same host (prod + staging), because they
share one lock directory.

Mechanism: an advisory exclusive ``fcntl.flock`` on a per-session lock file. The
kernel releases it automatically when the holder's open file description is closed
(including on process death), so a crash can never leave a permanent lock. The lock
fd is meant to be inherited by the long-lived agent (``dtach``) master so the lock
lives exactly as long as the running session (wired in the terminal phase).

See ``docs/session-handling.md`` for the full contract.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
from pathlib import Path

# Only safe filename chars; the real identity is still the full "{engine}:{id}" key.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def lock_dir() -> Path:
    """Shared directory holding per-session lock files. Created 0700 on demand.

    Shared (not per-instance) on purpose: it's what makes the single-writer
    guarantee hold across two app instances (e.g. prod + staging) on one host.
    """
    d = Path(
        os.environ.get("AGENT_SESSIONS_LOCK_DIR") or (Path.home() / ".agent-sessions" / "locks")
    )
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def lock_path(key: str) -> Path:
    """Lock file for a session ``key`` (``"{engine}:{native_id}"``)."""
    if not key:
        raise ValueError("empty session key")
    return lock_dir() / f"{_UNSAFE.sub('_', key)}.lock"


class SessionLock:
    """Holds the exclusive lock for one session key until ``release()`` / context exit.

    ``fd`` is exposed so the launch path can pass it (non-CLOEXEC) to the agent's
    persistent master, handing ownership to the process whose lifetime should hold it.
    """

    def __init__(self, key: str, fd: int, path: Path):
        self.key = key
        self.fd = fd
        self.path = path

    def release(self) -> None:
        """Drop the lock outright: unlock then close our fd.

        Use when no one else should hold it (e.g. we lost a race and will attach).
        Do NOT use after handing the fd to a master — ``LOCK_UN`` releases the lock
        for the whole shared open file description, defeating the handoff; use
        ``transfer`` there instead.
        """
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None  # type: ignore[assignment]

    def transfer(self) -> None:
        """Close our fd **without** unlocking — hand the flock to whoever inherited it.

        The kernel keeps the lock alive while any fd on the same open file description
        stays open (e.g. the ``dtach`` master that inherited it via ``pass_fds``); it
        releases only when the last such fd closes (master death). If no one inherited
        it, closing the last fd releases it immediately — so this is also correct on a
        launch that never managed to spawn a master.
        """
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None  # type: ignore[assignment]

    def __enter__(self) -> SessionLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire(key: str) -> SessionLock | None:
    """Try to take the single-writer lock for ``key`` (non-blocking).

    Returns a ``SessionLock`` if we are now the sole writer, or ``None`` if another
    holder (process / app instance) already has it — in which case the caller MUST
    attach to the existing session, never relaunch a second agent for the same id.
    """
    path = lock_path(key)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None  # held elsewhere → attach, don't relaunch
        raise
    return SessionLock(key, fd, path)


def is_locked(key: str) -> bool:
    """True if another holder currently has the lock (probe; does not keep it)."""
    lk = acquire(key)
    if lk is None:
        return True
    lk.release()
    return False


__all__ = ["SessionLock", "acquire", "is_locked", "lock_dir", "lock_path"]
