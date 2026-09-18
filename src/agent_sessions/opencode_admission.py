"""Cross-instance OpenCode launch/maintenance admission (#993, #1040).

Shared holders cover launches through actual process handoff; compaction owns the exclusive
lock until its SQLite worker has exited. Unlike a session's writer lock, this descriptor is
never inherited by a child. A crash therefore releases admission without a stale marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import stat
from functools import partial

from . import sessionlock

REFUSAL = "OpenCode maintenance is running or unavailable; retry when maintenance finishes."


class Unavailable(RuntimeError):
    """Launch admission could not be established; never proceed on an unknown."""


class Admission:
    def __init__(self, fd: int):
        self.fd: int | None = fd

    def release(self) -> None:
        if self.fd is not None:
            # Nothing inherits this open file description, so closing its sole fd releases it.
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self) -> Admission:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire(*, exclusive: bool) -> Admission | None:
    path = sessionlock.lock_dir() / "maintenance-opencode.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            raise PermissionError("unsafe OpenCode admission lock")
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    return Admission(fd)


async def for_launch(engine: str) -> Admission | None:
    """Take shared admission off-loop, reclaiming an acquisition abandoned by its caller."""
    if engine != "opencode":
        return None
    task = asyncio.get_running_loop().run_in_executor(None, partial(acquire, exclusive=False))
    try:
        guard = await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None and task.result() is not None:
            task.result().release()
        raise
    except OSError as e:
        raise Unavailable(REFUSAL) from e
    if guard is None:
        raise Unavailable(REFUSAL)
    return guard


async def spawn(factory, guard: Admission, *, timeout: float):
    """Keep admission across subprocess creation even if its caller times out/disconnects.

    Cancelling an await must not release the fence while a pending creation can still spawn.
    Drain the shielded creation first, then reap only the returned client on abandonment. A
    detached master, if created, is now visible to the compactor's process scan.
    """
    task = asyncio.create_task(factory())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.CancelledError, TimeoutError):
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None:
            proc = task.result()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                reap = asyncio.create_task(proc.wait())
                while not reap.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(reap)
        raise
    finally:
        guard.release()
