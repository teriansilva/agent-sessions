"""The per-session freeze fence (#2064): one lock that archive, unarchive, every launch and the
archived-but-running sweep share, plus the cancel request a freeze posts for running chat turns.

**Why a lock of its own.** "Is this session archived?" followed by "start it" is a check-then-act,
and an archive that tears the runtime down and then records the flag has a window between the two.
Both are closed by ordering everything on one per-session ``flock``:

* a **launcher** (terminal LAUNCH/ATTACH decision, native start/relaunch, a chat turn's admission)
  holds it **shared** across *read the archive state → start → hand-off*, and releases it at once;
* an **archive writer** holds it **exclusive** across *freeze the runtime → record the archive*, so
  no launch can interleave between the teardown and the commit of the flag;
* **unarchive** holds it exclusive across the provider's unarchive only (it starts nothing);
* the **sweep** holds it exclusive and re-reads the archive state under it, so a sweep queued
  behind an unarchive sees the session active and never stops a newly unarchived generation.

The lock lives next to the single-writer locks (``sessionlock.lock_dir()``), which are shared by
every app instance on the host, so the fence holds across processes and across prod + staging.
The kernel drops a ``flock`` when its holder dies, so a crash never leaves a session fenced.

**Lock order** — one order everywhere, never inverted: mission reservation → **freeze lock** →
chat turn fence → native lifecycle lock / single-writer launch lock.

**The cancel marker** (``<key>.cancel``) is how a freeze reaches a chat turn running in ANOTHER app
process. It is honoured only while a freeze holds the lock exclusively (`cancel_requested`), and
every freeze removes its own marker before it releases the lock, so a failed or crashed archive
can never cancel a later turn in a session that is still active: a leftover marker with the lock
free is inert, and the next launcher deletes it.

Acquisition never blocks a thread inside ``flock``: it retries a non-blocking attempt until a
deadline, so a caller that gives up leaves nothing behind that could acquire the lock later.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import sessionlock

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
#: How long a launcher waits for an archive / unarchive of the same session to finish.
SHARED_TIMEOUT = 35.0
#: How long a freeze waits for in-flight launch decisions of the same session to hand off.
EXCLUSIVE_TIMEOUT = 30.0
_POLL = 0.05


class FreezeBusy(Exception):
    """The fence could not be taken before the deadline: an archive, unarchive or launch of the
    same session is still in progress. Retryable; nothing was done."""


def _dir() -> Path:
    d = sessionlock.lock_dir() / "freeze"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _name(key: str) -> str:
    if not key:
        raise ValueError("empty session key")
    return _UNSAFE.sub("_", key)


def lock_path(key: str) -> Path:
    return _dir() / f"{_name(key)}.lock"


def marker_path(key: str) -> Path:
    return _dir() / f"{_name(key)}.cancel"


class Held:
    """One held fence. ``release()`` is idempotent."""

    def __init__(self, key: str, fd: int, exclusive: bool):
        self.key = key
        self.fd: int | None = fd
        self.exclusive = exclusive

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _open(key: str) -> int:
    return os.open(lock_path(key), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)


def acquire(key: str, *, exclusive: bool, timeout: float) -> Held:
    """Take the fence, retrying a NON-BLOCKING attempt until ``timeout``. Raises `FreezeBusy`."""
    mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    fd = _open(key)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FreezeBusy(
                        "an archive, unarchive or launch of this session is in progress"
                    ) from None
                time.sleep(_POLL)
    except BaseException:
        os.close(fd)
        raise
    held = Held(key, fd, exclusive)
    if not exclusive:
        # Holding it shared proves no freeze holds it exclusively, so a marker left here belongs
        # to a freeze that died: inert, and this launcher clears it.
        _drop_stale_marker(key)
    return held


async def acquire_async(key: str, *, exclusive: bool, timeout: float) -> Held:
    """`acquire` off the event loop, cancellation-safe: a fence the worker thread takes after its
    awaiting caller was cancelled is released, never orphaned."""
    task = asyncio.ensure_future(
        asyncio.to_thread(acquire, key, exclusive=exclusive, timeout=timeout)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        def _release(done: asyncio.Future) -> None:
            with contextlib.suppress(BaseException):
                done.result().release()

        task.add_done_callback(_release)
        raise


@contextlib.contextmanager
def shared(key: str, *, timeout: float = SHARED_TIMEOUT):
    held = acquire(key, exclusive=False, timeout=timeout)
    try:
        yield held
    finally:
        held.release()


@contextlib.asynccontextmanager
async def shared_async(key: str, *, timeout: float = SHARED_TIMEOUT):
    held = await acquire_async(key, exclusive=False, timeout=timeout)
    try:
        yield held
    finally:
        held.release()


@contextlib.asynccontextmanager
async def exclusive_async(key: str, *, timeout: float = EXCLUSIVE_TIMEOUT):
    held = await acquire_async(key, exclusive=True, timeout=timeout)
    try:
        yield held
    finally:
        held.release()


def held_exclusively(key: str) -> bool:
    """Does some holder — in any process, this one included — hold the fence exclusively now?
    Probed with a non-blocking SHARED attempt on a fresh descriptor."""
    try:
        fd = os.open(lock_path(key), os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# ---- the cancel marker ----------------------------------------------------------------------


def write_marker(key: str) -> str:
    """Post a cancel request for ``key``'s running turns; return its token. The caller MUST hold
    the fence exclusively, and must `clear_marker` before it releases it."""
    token = uuid.uuid4().hex
    path = marker_path(key)
    tmp = path.with_name(f".{path.name}.{token}")
    body = json.dumps({"token": token, "pid": os.getpid(), "ts": time.time()})
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return token


def _read_token(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw.get("token") if isinstance(raw, dict) else None


def clear_marker(key: str, token: str) -> None:
    """Remove the marker this freeze posted (never another freeze's)."""
    path = marker_path(key)
    if _read_token(path) == token:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _drop_stale_marker(key: str) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        marker_path(key).unlink()


def cancel_requested(key: str) -> bool:
    """Should a running turn of ``key`` stop now? Only while a freeze holds the fence exclusively:
    a marker whose freeze ended or died is inert."""
    try:
        if not marker_path(key).exists():
            return False
    except (OSError, ValueError):
        return False
    try:
        return held_exclusively(key)
    except OSError:
        return False


__all__ = [
    "EXCLUSIVE_TIMEOUT",
    "SHARED_TIMEOUT",
    "FreezeBusy",
    "Held",
    "acquire",
    "acquire_async",
    "cancel_requested",
    "clear_marker",
    "exclusive_async",
    "held_exclusively",
    "lock_path",
    "marker_path",
    "shared",
    "shared_async",
    "write_marker",
]
