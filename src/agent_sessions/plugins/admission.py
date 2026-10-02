"""Order roster revocation and actual process creation across app workers (#1259).

The private, non-inherited lock is held only through process creation, never its lifetime.
Interactive acquisition is off-loop; abandoned async spawns are drained before release.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars

from . import provenance, storage

LOCK = "launch"
UNAVAILABLE = "agent launch admission is unavailable; retry after the plugin operation"


class Refused(RuntimeError):
    pass


class Guard:
    def __init__(self):
        self.stack = contextlib.ExitStack()
        self.reason = ""

    def release(self):
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def acquire(prov) -> Guard:
    from ..engines import registry

    guard = Guard()
    try:
        guard.stack.enter_context(storage.locked(LOCK))
        if not registry.admits(prov):
            guard.reason = (
                UNAVAILABLE if "manager" in registry.capture().problems else "agent removed"
            )
    except (OSError, ValueError, provenance.ProvenanceError):
        guard.release()
        guard.reason = UNAVAILABLE
    except BaseException:
        guard.release()
        raise
    return guard


async def acquire_async(prov) -> Guard:
    task = asyncio.get_running_loop().run_in_executor(
        None, contextvars.copy_context().run, acquire, prov
    )
    try:
        guard = await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None:
            task.result().release()
        raise
    if guard.reason:
        guard.release()
        raise Refused(guard.reason)
    return guard


@contextlib.asynccontextmanager
async def request(prov):
    """Hold admission through sending a request body, but never through its response."""
    guard = await acquire_async(prov)
    try:
        yield guard.release
    finally:
        guard.release()


def commit(prov, fn, *args, **kwargs):
    """Admit and persist new conversation work in one off-loop critical section."""
    with acquire(prov) as guard:
        if guard.reason:
            raise Refused(guard.reason)
        return fn(*args, **kwargs)


async def spawn(factory, prov, *, timeout: float):
    from .. import opencode_admission

    guard = await acquire_async(prov)
    # That helper owns cancellation/timeouts through process handoff and reaping, and accepts
    # any single-owner guard with release(). Neither guard's descriptor is inherited by children.
    return await opencode_admission.spawn(factory, guard, timeout=timeout)
