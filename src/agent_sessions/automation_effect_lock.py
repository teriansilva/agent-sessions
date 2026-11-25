"""The per-automation EFFECT lock (#1201): authority and effect in one critical section.

A run's final authority read (enabled, not paused unless Run now, receipt == scope, not needing
re-approval, same revision) and its effect — the mission create → plan → dispatch, the session
launch, the send's writes — happen while the run holds this lock. Every authority-changing write
(disable, pause, enable/consent, edit, delete) takes the same lock before it commits.

* **Cross-process**: ``flock`` on ``<lock dir>/automation-<id>.lock`` (the shared single-writer lock
  dir, ``AGENT_SESSIONS_LOCK_DIR``), opened ``O_CLOEXEC`` so no agent the app execs inherits it.
  ``flock`` locks belong to an open file description, so two holders in ONE process contend too.
* **Process-local first**: an async holder takes a per-automation ``asyncio.Lock`` before polling
  the ``flock``, so this process's own runs queue on the loop instead of spinning threads.
* **A writer waits a bounded time** (``WRITER_WAIT_S``). If it gets the lock it commits and no
  effect can follow, because the run's final read would see the change. If the wait runs out it
  STILL commits — withdrawing authority must never be the thing that fails — and reports
  ``in_flight``: a run already past its final read may still complete. An acknowledged change
  WITHOUT ``in_flight`` therefore means no later effect.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from pathlib import Path

from . import sessionlock
from .automations_store import ID_RE as store_ids

#: How long an authority-changing write waits for a run mid-effect before committing anyway.
WRITER_WAIT_S = 10.0
#: How long a run waits to begin its effect (another run of the same automation may hold it).
RUNNER_WAIT_S = 30.0
POLL_S = 0.05

IN_FLIGHT_DETAIL = "a run already in progress may still complete"

_async_locks: dict[str, asyncio.Lock] = {}


def path_for(aid: str) -> Path:
    # ASCII only: `str.isalnum` accepts "²" and every other Unicode digit, which must never name a
    # lock file. Callers look the automation up first, so an unknown id never gets this far.
    if not isinstance(aid, str) or not store_ids.fullmatch(aid):
        raise ValueError("not an automation id")
    return sessionlock.lock_dir() / f"automation-{aid}.lock"


def _try(p: Path) -> int | None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release(fd: int | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def acquire(aid: str, timeout: float) -> int | None:
    """BLOCKING: the lock's fd, or None when ``timeout`` ran out."""
    p = path_for(aid)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        fd = _try(p)
        if fd is not None or time.monotonic() >= deadline:
            return fd
        time.sleep(POLL_S)


@contextlib.contextmanager
def writer(aid: str, timeout: float | None = None):
    """For an authority-changing write. Yields ``True`` if the lock was held for the commit,
    ``False`` if the wait ran out (commit anyway; report ``in_flight``)."""
    fd = acquire(aid, WRITER_WAIT_S if timeout is None else timeout)
    try:
        yield fd is not None
    finally:
        _release(fd)


@contextlib.contextmanager
def runner_sync(aid: str, timeout: float | None = None):
    """For a run's effect on a worker thread. Yields ``True`` when held; ``False`` means another
    effect of this automation held it for the whole wait, and the run must not proceed."""
    fd = acquire(aid, RUNNER_WAIT_S if timeout is None else timeout)
    try:
        yield fd is not None
    finally:
        _release(fd)


async def _try_owned(p: Path) -> int | None:
    """One non-blocking attempt on a worker thread, whose result this coroutine OWNS even if it is
    cancelled: the worker may win the ``flock`` after the cancellation arrives, and an fd nobody
    holds a reference to would keep the automation locked for the life of the process. On
    cancellation the attempt is joined (shielded) and whatever it acquired is released before the
    cancellation propagates."""
    task = asyncio.ensure_future(asyncio.to_thread(_try, p))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None:
            _release(task.result())
        raise


@contextlib.asynccontextmanager
async def runner(aid: str, timeout: float | None = None):
    """For a run's effect on the event loop: the process-local lock, then the ``flock``.

    (Writers and the send path acquire on a worker thread with the blocking :func:`acquire`,
    which a request cancellation cannot interrupt: the thread finishes and its ``with`` releases.)
    """
    lock = _async_locks.setdefault(aid, asyncio.Lock())
    wait = RUNNER_WAIT_S if timeout is None else timeout
    try:
        await asyncio.wait_for(lock.acquire(), wait)
    except TimeoutError:
        yield False
        return
    fd = None
    try:
        deadline = time.monotonic() + wait
        p = path_for(aid)
        while True:
            fd = await _try_owned(p)
            if fd is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        yield fd is not None
    finally:
        _release(fd)
        lock.release()
