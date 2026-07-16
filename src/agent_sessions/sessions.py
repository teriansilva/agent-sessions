"""Open-or-attach policy enforcing the single-agent invariant.

One session id ⇒ at most one running agent ⇒ one writer of its history. A session
is *launched* only when no live master exists AND we win the single-writer lock;
otherwise we *attach* to the running agent (never relaunch). See
``docs/session-handling.md``.

On LAUNCH the caller hands the lock fd to the long-lived ``dtach`` master (via
``pass_fds``); the kernel then keeps the flock for exactly the master's lifetime and
releases it when the master dies — so the guarantee survives an app restart and
holds across instances even where the dtach socket isn't shared.
"""

from __future__ import annotations

from . import ptybridge, sessionlock

ATTACH = "attach"  # a live dtach master exists → attach to it (never relaunch)
LAUNCH = "launch"  # no master and we won the lock → caller creates the master
BUSY = "busy"  # no local master but the lock is held elsewhere → do not relaunch


def open_action(engine: str, native_id: str) -> tuple[str, sessionlock.SessionLock | None]:
    """Decide how to open ``{engine}:{native_id}``, enforcing one-id ⇒ one-agent.

    Returns ``(ATTACH, None)``, ``(BUSY, None)``, or ``(LAUNCH, lock)`` — in the
    LAUNCH case the caller creates the master and hands ``lock`` to it for its lifetime.

    Synchronous and free of ``await`` points, so it runs atomically with respect to
    other coroutines: two concurrent opens of the same not-yet-running id cannot both
    reach LAUNCH (the first holds the flock until the master inherits it; the second
    fails to acquire → BUSY).

    The single-writer guarantee rests on the kernel ``flock`` in ``sessionlock.acquire``,
    which is atomic across THREADS and PROCESSES — not merely across coroutines. So the
    connect path may dispatch this whole function via ``asyncio.to_thread`` to keep its
    blocking socket probes off the event loop (#652 T-P4): two concurrent launches on two
    worker threads still resolve to exactly one LAUNCH and the rest BUSY. It must be
    dispatched as ONE call, though — splitting the ``session_exists`` check from the
    ``acquire`` across ``await`` points would reopen the very race the flock closes.
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
    # Under the held lock, any lingering `.sock` file is necessarily an orphan from
    # a prior generation (no one else can be racing for this key). Unlink it so the
    # caller's subsequent `dtach -c` can `bind()` cleanly. #165.
    ptybridge.unlink_if_stale(engine, native_id)
    return LAUNCH, lock


__all__ = ["ATTACH", "LAUNCH", "BUSY", "open_action"]
