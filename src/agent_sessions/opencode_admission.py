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
import hashlib
import os
import stat
from functools import partial
from pathlib import Path

from . import sessionlock

REFUSAL = "OpenCode maintenance is running or unavailable; retry when maintenance finishes."


class Unavailable(RuntimeError):
    """Launch admission could not be established; never proceed on an unknown."""


class Admission:
    def __init__(self, *fds: int):
        self.fds: list[int] = list(fds)

    @property
    def fd(self) -> int | None:
        return self.fds[0] if self.fds else None

    def release(self) -> None:
        # Nothing inherits these open file descriptions, so closing each sole fd releases it.
        while self.fds:
            os.close(self.fds.pop())

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
    return m is not None and m.launch is not None and m.launch.admission == "sqlite-store-shared"


def database_identity(database: str | os.PathLike) -> str:
    """The admission key of one SQLite store: its resolved path, the way `native_ownership`
    resolves a database source (`Path.resolve(strict=False)`), hashed into a lock-file name."""
    path = str(Path(database).expanduser().resolve(strict=False))
    if not os.path.isabs(path) or "\x00" in path:
        raise OSError("invalid OpenCode database identity")
    return hashlib.sha256(path.encode()).hexdigest()[:32]


def _database(engine: str) -> Path:
    from . import engines

    prov = engines.get_any(engine)
    db = prov.store_path("db") if prov is not None else None
    if db is None:
        raise OSError(f"{engine} has no database to admit launches against")
    return Path(db)


def _lock(name: str, *, exclusive: bool) -> int | None:
    path = sessionlock.lock_dir() / name
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
    return fd


def acquire(
    engine: str | None = None, *, exclusive: bool, database: str | os.PathLike | None = None
) -> Admission | None:
    """Take the admission of ``engine``'s store (default: the maintained engine).

    Keyed by the DATABASE, not the engine id (Hermes on #1336): every engine whose store resolves
    to one file — the canonical `opencode`, a compatible alias, a native API client's source —
    takes `maintenance-db-<identity>.lock`, so a launch through any of them and a compaction
    through any other exclude each other. ``database`` names the store directly (a worker or a
    not-yet-live candidate that cannot ask the roster); otherwise the engine's own provider
    resolves it (`store.path_env` first). The historic per-engine `maintenance-<id>.lock` is
    taken first as well, so an instance still running the previous build stays fenced.
    Both are non-blocking: either busy means None, and nothing is held.
    """
    engine = engine or maintained_engine()
    if engine is None:
        raise OSError("no engine selects store maintenance")
    identity = database_identity(database if database is not None else _database(engine))
    held = Admission()
    try:
        for name in (f"maintenance-{engine}.lock", f"maintenance-db-{identity}.lock"):
            fd = _lock(name, exclusive=exclusive)
            if fd is None:
                held.release()
                return None
            held.fds.append(fd)
    except BaseException:
        held.release()
        raise
    return held


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
