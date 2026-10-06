"""Order roster revocation and actual process creation across app workers (#1259).

The private, non-inherited lock is held only through process creation, never its lifetime.
Interactive acquisition is off-loop; abandoned async spawns are drained before release.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from functools import partial

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


def acquire(prov, native_id: str | None = None) -> Guard:
    from .. import native_ownership
    from ..engines import registry

    guard = Guard()
    try:
        guard.stack.enter_context(storage.locked(LOCK))
        if not registry.admits(prov):
            guard.reason = (
                UNAVAILABLE if "manager" in registry.capture().problems else "agent removed"
            )
        else:
            native_ownership.check_console(prov, native_id)
    except native_ownership.OwnershipError as exc:
        guard.release()
        guard.reason = str(exc)
    except (OSError, ValueError, provenance.ProvenanceError):
        guard.release()
        guard.reason = UNAVAILABLE
    except BaseException:
        guard.release()
        raise
    return guard


async def _acquire_async(acquire_guard) -> Guard:
    task = asyncio.get_running_loop().run_in_executor(
        None, contextvars.copy_context().run, acquire_guard
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


async def acquire_async(prov, native_id: str | None = None) -> Guard:
    return await _acquire_async(partial(acquire, prov, native_id))


def acquire_attach(prov, native_id: str) -> Guard:
    """Admit a console viewer, including a retiring provider, against permanent ownership.

    This creates no agent work and must not require the source binary or an active roster
    entry. Discovery returning no row does not authorize a warm attach to an API-owned history,
    so ownership is checked here too. It does NOT take the app-wide launch lock: an attach
    targets an existing live master, and binding refuses any history with a live (or unknown)
    console socket, so the two cannot interleave into an attach to a newly bound history.
    Holding the launch lock here made every attach queue behind slow launches (#1277 review).
    Unrelated live consoles remain attachable while native provisioning is pending.
    """
    from .. import native_ownership

    guard = Guard()
    try:
        native_ownership.check_console(prov, native_id, allow_pending=True)
    except native_ownership.OwnershipError as exc:
        guard.reason = str(exc)
    except (OSError, ValueError, provenance.ProvenanceError):
        guard.reason = UNAVAILABLE
    return guard


async def acquire_attach_async(prov, native_id: str) -> Guard:
    return await _acquire_async(partial(acquire_attach, prov, native_id))


def _candidate_provider(prov, purpose: str, operation_id: str):
    """The exact running, inactive candidate operation; never a general admission bypass.

    The verification worker and sign-in transport already hold the worker flock throughout
    this operation. Call only from their process runner, in worker -> launch lock order.
    """
    from .. import native_ownership
    from . import manager

    if purpose not in {"version", "new", "resume", "signin"}:
        raise Refused("invalid candidate process purpose")
    doc = manager.snapshot()
    item = doc["operations"].get(operation_id)
    kind = "signin" if purpose == "signin" else "verify"
    if (
        item is None
        or item["kind"] != kind
        or item["state"] != "running"
        or item["plugin_id"] != prov.engine_id
    ):
        raise Refused("the candidate operation is no longer running")
    row = doc["plugins"][prov.engine_id]
    generation_id = item["request"]["generation_id"]
    manager._require_inactive(row, generation_id)
    generation = row["generations"][generation_id]
    if generation["review"]["digest"] != item["review_digest"]:
        raise Refused("the candidate operation changed")
    expected = manager.provider(prov.engine_id, generation)
    if (
        (prov.manifest, prov.root, prov._record, prov.trust)
        != (expected.manifest, expected.root, expected._record, expected.trust)
        or native_ownership.source_identity(prov) != native_ownership.source_identity(expected)
        or prov.entrypoint() != expected.entrypoint()
    ):
        raise Refused("the candidate provider changed")


def _acquire_candidate(prov, purpose: str, operation_id: str, native_id: str | None) -> Guard:
    from .. import native_ownership

    guard = Guard()
    try:
        guard.stack.enter_context(storage.locked(LOCK))
        _candidate_provider(prov, purpose, operation_id)
        native_ownership.check_console(prov, native_id)
    except (OSError, ValueError, KeyError, Refused, provenance.ProvenanceError) as exc:
        guard.release()
        guard.reason = str(exc) if isinstance(exc, Refused) else UNAVAILABLE
    except native_ownership.OwnershipError as exc:
        guard.release()
        guard.reason = str(exc)
    except BaseException:
        guard.release()
        raise
    return guard


async def acquire_candidate_async(
    prov, purpose: str, operation_id: str, native_id: str | None = None
) -> Guard:
    return await _acquire_async(partial(_acquire_candidate, prov, purpose, operation_id, native_id))


@contextlib.asynccontextmanager
async def request(prov):
    """Hold admission through sending a request body, but never through its response."""
    guard = await acquire_async(prov)
    try:
        yield guard.release
    finally:
        guard.release()


def publish_alias(prov, physical_key: str, logical_key: str) -> None:
    """Publish an existing console runtime's native identity, ordered against API binding.

    This creates no agent work, so a retiring provider can repair an existing runtime alias.
    Mission callers enter only after their authority transaction has released its locks.
    """
    from .. import engines, metadata, native_ownership

    engine, sep, native = logical_key.partition(":")
    if not sep or engine != prov.engine_id or physical_key.partition(":")[0] != engine:
        raise Refused("a console alias cannot cross client identities")
    if not prov.manifest.session_id.accepts(native):
        raise Refused("a console alias has an invalid native identity")
    from ..engines import base

    if base._NEW_PLACEHOLDER_RE.fullmatch(physical_key.partition(":")[2]) is None:
        # Only a placeholder runtime is ever aliased: native ownership attributes a concrete
        # runtime id to its own history without consulting this index (#1277).
        raise Refused("only a new console's placeholder runtime can be aliased")
    try:
        engines.parse_key(physical_key, allow_new_placeholder=True)
    except engines.EngineError as exc:
        raise Refused("a console alias has an invalid runtime identity") from exc
    with storage.locked(LOCK):
        try:
            native_ownership.check_console(prov, native)
        except native_ownership.OwnershipError as exc:
            raise Refused(str(exc)) from exc
        metadata.set_alias(physical_key, logical_key)


def commit(prov, fn, *args, **kwargs):
    """Admit and persist new conversation work in one off-loop critical section."""
    with acquire(prov) as guard:
        if guard.reason:
            raise Refused(guard.reason)
        return fn(*args, **kwargs)


async def spawn(
    factory, prov, *, timeout: float, native_id: str | None = None, attach: bool = False
):
    from .. import opencode_admission

    if attach:
        if native_id is None:
            raise Refused("console attach has no native session identity")
        guard = await acquire_attach_async(prov, native_id)
    else:
        guard = await acquire_async(prov, native_id)
    # That helper owns cancellation/timeouts through process handoff and reaping, and accepts
    # any single-owner guard with release(). Neither guard's descriptor is inherited by children.
    return await opencode_admission.spawn(factory, guard, timeout=timeout)
