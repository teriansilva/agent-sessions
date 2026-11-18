"""The per-grant fence: one advisory ``flock`` per grant, shared by every process over a store
(#1246 §5, #2057).

A peer's message is delivered under a grant, and the grant can be ended or changed by ANY app
process over the same store (``store.py``). An in-process lock cannot order those two, so the
fence is a file: ``<federation dir>/fences/<grant_id>``.

Who takes it:

* a delivery (``inputs.py``: ``input.send``, an approval, a replay), around the re-check of the
  grant, the record of the operation, the submit and the record of its receipt;
* every write that ends or changes a grant (``store.py``: a revoke, a level or expiry change, a
  peer revoke, an archive, the expiry sweep), around its one store transaction.

**Lock order, always:** the session lock (``shares.ShareStore.session_lock``; an archive and a
grant creation only) → grant fences **in grant-id order** → the store transaction. Nothing takes
them in another order. No fence is held across a wait on a peer or an operator.

**A fresh fd per acquisition.** ``flock`` belongs to an open file description: two holders in one
process contend exactly like two processes, and a holder that dies releases (the kernel closes
its fds). A fence file is never unlinked: unlinking one while a waiter holds an fd on it would
give two holders two different files.

**Bounded waits, in a worker thread.** ``flock`` has no timed acquire, so a wait is a
non-blocking attempt in a loop. :func:`acquire` blocks its thread and is never called on the event
loop. A wait that runs out raises :class:`FenceBusy` and the caller commits NOTHING.

**Released at the admission boundary.** :func:`run_fenced` runs a delivery in its own task, which
takes the fence, runs the body and releases the fence when the body has **returned or raised**.
The caller awaits it through a shield: when the caller's wait ends first (its call timed out, the
request was cancelled) the task goes on, still holding the fence, until the body is done.
Nothing here ever cancels the body; asking for a cancellation is not treated as its completion.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import fcntl
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

DIR_NAME = "fences"
DIR_MODE = 0o700
#: A grant id as ``store.new_grant_id`` mints one. Checked HERE too: the id names a file.
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")
#: How long a revoke, a change, a peer revoke or an archive waits for a delivery in flight. Past
#: it the write fails closed (``503 busy``): it never commits around an admission in flight.
REVOKE_WAIT_S = 30.0
#: How long a delivery waits for the fence (another delivery, or a grant change, holds it).
DELIVERY_WAIT_S = 10.0
#: The expiry sweep does not wait: a grant whose fence is held is swept on the next round. Expiry
#: is checked at every use, so nothing depends on the sweep.
SWEEP_WAIT_S = 0.0
_RETRY_S = 0.005
#: A hold longer than this is logged (code locations only, never content).
SLOW_HOLD_S = 5.0

#: The threads that WAIT for a fence on behalf of a delivery. Their own small pool, never the
#: default executor: the delivery that holds a fence needs default-executor threads to finish
#: (``asyncio.to_thread`` inside the runtime), and waiters parked there could starve it.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="fed-fence")
#: Deliveries in flight, so none is garbage-collected before its fence is released.
_RUNNING: set[asyncio.Future] = set()


class FenceBusy(RuntimeError):
    """A fence could not be taken inside the wait. Nothing was committed."""


def fence_dir(directory: Path) -> Path:
    return directory / DIR_NAME


def path_for(directory: Path, grant_id: str) -> Path:
    if not isinstance(grant_id, str) or _ID_RE.fullmatch(grant_id) is None:
        raise ValueError("not a grant id")
    return fence_dir(directory) / grant_id


def _open(path: Path) -> int:
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path.parent, DIR_MODE)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)


def acquire(directory: Path, grant_id: str, timeout: float) -> int:
    """BLOCKING. The fd that holds ``grant_id``'s fence, or :class:`FenceBusy` when ``timeout``
    ran out. Release it with :func:`release`, from any thread."""
    return _acquire_until(path_for(directory, grant_id), time.monotonic() + max(0.0, timeout))


def _acquire_until(path: Path, deadline: float) -> int:
    fd = _open(path)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
            if time.monotonic() >= deadline:
                raise FenceBusy("a delivery under this grant is still in flight")
            time.sleep(_RETRY_S)
    except BaseException:
        os.close(fd)
        raise


def release(fd: int | None) -> None:
    if fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


@contextlib.contextmanager
def hold(directory: Path, grant_ids: Iterable[str], *, timeout: float) -> Iterator[None]:
    """BLOCKING. Hold the fences of ``grant_ids``, taken **in grant-id order** under ONE deadline,
    for the body. :class:`FenceBusy` when any of them could not be had: the ones already taken
    are released and the body never runs."""
    # Every id is validated before anything is opened or ordered.
    paths = sorted({path_for(directory, grant_id) for grant_id in grant_ids})
    deadline = time.monotonic() + max(0.0, timeout)
    held: list[int] = []
    try:
        for path in paths:
            held.append(_acquire_until(path, deadline))
        yield
    finally:
        for fd in reversed(held):
            release(fd)


def held_elsewhere(directory: Path, grant_id: str) -> bool:
    """Whether someone holds ``grant_id``'s fence right now. For tests and diagnostics only: the
    answer is stale as soon as it is given, and no decision is made on it."""
    try:
        release(acquire(directory, grant_id, 0.0))
    except FenceBusy:
        return True
    return False


def _release_result(waiting: asyncio.Future) -> None:
    if not waiting.cancelled() and waiting.exception() is None:
        release(waiting.result())


def start_fenced(
    directory: Path,
    grant_id: str,
    body: Callable[[], Awaitable],
    *,
    wait: float | None = None,
) -> asyncio.Future:
    """Start ``await body()`` under ``grant_id``'s fence and return its task.

    The whole thing (take the fence in a worker thread, run the body, release) is ONE task of its
    own. Whoever waits for it does so through a shield (:func:`run_fenced`): if the waiter is
    cancelled or stops waiting, the task is not. It keeps the fence until the body has returned
    or raised, and only then releases it. The fence is therefore never released while a submit
    is still unwinding, and never leaked by a caller that went away while the worker thread was
    still waiting for it."""
    path = path_for(directory, grant_id)  # validated before anything is started
    budget = DELIVERY_WAIT_S if wait is None else wait
    loop = asyncio.get_running_loop()

    async def run():
        waiting = loop.run_in_executor(
            _POOL, _acquire_until, path, time.monotonic() + max(0.0, budget)
        )
        try:
            fd = await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # Only the loop itself cancels this task (a shutdown). The thread may still win the
            # fence afterwards: whatever it took is released, never left to a closed loop.
            waiting.add_done_callback(_release_result)
            raise
        started = time.monotonic()
        try:
            return await body()
        finally:
            # The admission boundary: the body returned or raised. Not earlier, whatever became
            # of whoever was waiting for it.
            release(fd)
            if time.monotonic() - started > SLOW_HOLD_S:
                log.warning("federation: a grant fence was held %.1fs", time.monotonic() - started)

    task = asyncio.ensure_future(run())
    _RUNNING.add(task)

    def done(finished: asyncio.Future) -> None:
        _RUNNING.discard(finished)
        # Retrieved here so an outcome nobody waited for is not reported as "never retrieved".
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(done)
    return task


async def run_fenced(
    directory: Path,
    grant_id: str,
    body: Callable[[], Awaitable],
    *,
    wait: float | None = None,
):
    """:func:`start_fenced`, awaited through a shield. Cancelling this await, or timing it out,
    leaves the delivery and its fence exactly as they are."""
    return await asyncio.shield(start_fenced(directory, grant_id, body, wait=wait))


def in_flight() -> int:
    """Deliveries of THIS process that hold or wait for a fence."""
    return len(_RUNNING)
