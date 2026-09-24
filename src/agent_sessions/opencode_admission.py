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


def maintained_engines() -> list[str]:
    """EVERY engine whose store the compaction kind maintains — each one whose manifest selects
    `maintenance = ["sqlite-vacuum"]` (#853 P3), in roster order. Each has its own database and
    its own admission lock; none is ever dropped for not being first."""
    from . import engines

    return engines.ids_where(lambda m: "sqlite-vacuum" in m.maintenance)


def maintained_engine() -> str | None:
    """The DEFAULT compaction target (the first in roster order) — what a request that names no
    engine means, which keeps the pre-P3 endpoints' single-target shape working."""
    ids = maintained_engines()
    return ids[0] if ids else None


def admits(engine: str) -> bool:
    """Does a launch of ``engine`` take shared admission? Its manifest says so
    (`launch.admission = "sqlite-store-shared"`), never its id."""
    from . import engines

    m = engines.manifest_of(engine)
    return m is not None and m.launch.admission == "sqlite-store-shared"


def acquire(engine: str | None = None, *, exclusive: bool) -> Admission | None:
    """Take ``engine``'s admission lock (default: the maintained engine). One lock file per engine,
    `maintenance-<id>.lock` — which for opencode is the name it has always had, so an instance
    running the previous build and this one still fence each other."""
    engine = engine or maintained_engine()
    if engine is None:
        raise OSError("no engine selects store maintenance")
    path = sessionlock.lock_dir() / f"maintenance-{engine}.lock"
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
    if not admits(engine):
        return None
    task = asyncio.get_running_loop().run_in_executor(
        None, partial(acquire, engine=engine, exclusive=False)
    )
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
