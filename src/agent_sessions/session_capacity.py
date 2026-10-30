"""The limit on running sessions (#1819): one gate at every place a top-level agent is born.

**What counts.** A running session: a live ``dtach`` master, a live API worker, or a launch under
way. Mission sessions are masters like any other. An agent's own subagents run inside its scope or
unit and are never seen here. An ended session holds nothing, so it does not count even unarchived.

**The gate.** `admit` counts and reserves under one ``flock``, in the lock directory every app
instance on the host shares, so two launches can't both take the last slot. The limit only ever
refuses a start. Lowering it stops nothing.

**A reservation is a held lock.** One file per launch, flocked by its holder, exactly as the
single-writer lock is: it lasts as long as the launch however slow that is, and the kernel drops it
if the holder dies. There is no clock to outlive and no pid to be reused. A file nobody holds is
left over from a crash, and the next count removes it.

**Unknown is not free.** A socket whose probe timed out counts as running. Two things read as
nothing instead, and both are logged: an unreadable API record (a corrupt old file must not hold a
slot for ever), and a count that can't run at all (an unlistable runtime directory, a lock that
never came free). This is a resource guard, not a security boundary, and a launch needs that
directory anyway. Refusing every start on a read error would stop the operator's work to protect a
count.

Blocking: runs off the event loop. Never spawns a process.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

from . import ptybridge, resource_limits, sessionlock

log = logging.getLogger("agent_sessions.session_capacity")

SESSION_LIMIT = "session_limit"  # the refusal's name on a dispatch and an API error
CLOSE_CODE = 4429  # the terminal socket's: terminal for the client, never retried
# Long enough for a census through wedged masters (a probe can take 1.7 s), since giving up admits.
LOCK_WAIT_S = 60.0
_LOCK_NAME = "session-capacity.lock"
_SLOTS_DIR = "session-slots"


class SessionLimitReached(Exception):
    """The limit is reached: nothing was started. The message is written for the operator."""

    def __init__(self, running: int, limit: int):
        self.running = running
        self.limit = limit
        super().__init__(
            f"Session limit reached: {running} of {limit} running. "
            "Archive a session or raise the limit to start another. Nothing was started."
        )


class Reservation:
    """One launch's slot: a held lock on its own file. `release` is idempotent and never blocks."""

    def __init__(self, fd: int | None = None, path: Path | None = None):
        self._fd = fd
        self._path = path

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self._path is not None:
            with contextlib.suppress(OSError):
                self._path.unlink()
        os.close(fd)  # the only descriptor: closing it drops the flock

    def __del__(self) -> None:
        # A launch cancelled while its reservation was being made never received this object.
        self.release()


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    fd = os.open(sessionlock.lock_dir() / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EACCES):
                    raise
            if time.monotonic() >= deadline:
                raise TimeoutError("the session limit's lock did not come free")
            time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing the only descriptor drops the flock


def _slots_dir() -> Path:
    path = sessionlock.lock_dir() / _SLOTS_DIR
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _held_slots() -> tuple[set[str], int]:
    """``(keys, anonymous)`` of the launches under way. Removes what a dead launch left."""
    keys: set[str] = set()
    anonymous = 0
    for path in _slots_dir().iterdir():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            continue  # released between the listing and here
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    key = json.loads(os.read(fd, 4096) or b"null")
                except ValueError:
                    key = None
                if isinstance(key, str):
                    keys.add(key)
                else:
                    anonymous += 1
                continue
            with contextlib.suppress(OSError):
                path.unlink()  # nobody holds it: its launch is over
        finally:
            os.close(fd)
    return keys, anonymous


def _take_slot(key: str | None) -> Reservation:
    path = _slots_dir() / secrets.token_hex(8)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        # Under the gate's lock: a counter can't see this file before it is held.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, json.dumps(key).encode())
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return Reservation(fd, path)


def stem(key: str) -> str:
    """A session key as its socket names it. ``ptybridge`` can only split a socket name at its first
    hyphen, so a plugin engine's ``my-agent:abc`` reads back as ``my:agent-abc``; compare these."""
    return key.replace(":", "-", 1)


def _api_running() -> set[str]:
    from . import native_runtime  # lazy: native_runtime gates its launches through this module

    return native_runtime.running_session_keys()


def running() -> tuple[set[str], int]:
    """``(keys, unknown)``: the sessions that run, and the sockets that could not be asked."""
    keys, unknown = ptybridge.master_census()
    found = {f"{engine}:{native}" for engine, native in keys} | _api_running()
    return found, unknown


def count() -> int:
    """How many sessions run right now, not counting launches under way."""
    keys, unknown = running()
    return len(keys) + unknown


def admit(key: str | None) -> Reservation:
    """Take a slot for a launch of `key` (None: its id isn't known yet) or raise.

    A key that already runs takes no second slot: a generation replacing itself, or a reservation
    still held while its master comes up, is one session.
    """
    limit = resource_limits.max_sessions()
    try:
        with _locked():
            keys, unknown = running()
            reserved, anonymous = _held_slots()
            held = {stem(k) for k in keys | reserved}
            taken = len(held) + anonymous + unknown
            if taken >= limit and (key is None or stem(key) not in held):
                raise SessionLimitReached(taken, limit)
            return _take_slot(key)
    except (OSError, TimeoutError):
        log.warning("session limit: could not count, admitting this launch", exc_info=True)
        return Reservation()


def check() -> None:
    """Raise if a launch would be refused now. For work that comes BEFORE a launch and costs
    something (an automation's planner call): advisory, the launch's own `admit` still decides."""
    admit(None).release()


__all__ = [
    "CLOSE_CODE",
    "SESSION_LIMIT",
    "Reservation",
    "SessionLimitReached",
    "admit",
    "check",
    "count",
    "running",
    "stem",
]
