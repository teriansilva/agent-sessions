"""Open-or-attach policy enforcing the single-agent invariant.

One session id ⇒ at most one running agent ⇒ one writer of its history. A session
is *launched* only when no live master exists AND we win the single-writer lock;
otherwise we *attach* to the running agent (never relaunch). See
``docs/session-handling.md``.
"""

from __future__ import annotations

from . import ptybridge, sessionlock

ATTACH = "attach"  # a live dtach master exists → attach to it (never relaunch)
LAUNCH = "launch"  # no master and we won the lock → caller creates the master
BUSY = "busy"  # no local master but the lock is held elsewhere → do not relaunch


def open_action(engine: str, native_id: str) -> tuple[str, sessionlock.SessionLock | None]:
    """Decide how to open ``{engine}:{native_id}``, enforcing one-id ⇒ one-agent.

    Returns ``(ATTACH, None)``, ``(BUSY, None)``, or ``(LAUNCH, lock)`` — in the
    LAUNCH case the caller creates the master and keeps ``lock`` for its lifetime.
    """
    key = f"{engine}:{native_id}"
    # A live master already runs the agent → attach; its holder owns the lock.
    if ptybridge.session_exists(engine, native_id):
        return ATTACH, None
    lock = sessionlock.acquire(key)
    if lock is None:
        # Locked but no local socket: another instance is launching/owns it.
        return BUSY, None
    # Race guard: a master may have appeared between the check and the acquire.
    if ptybridge.session_exists(engine, native_id):
        lock.release()
        return ATTACH, None
    return LAUNCH, lock


__all__ = ["ATTACH", "LAUNCH", "BUSY", "open_action"]
