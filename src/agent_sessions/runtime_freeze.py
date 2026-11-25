"""Freeze a session — archive does what Stop does, everything (#2064).

An archived session is frozen: no process BattleLab owns, no subprocess the agent started, no
native worker generation, no chat turn task. Every archive writer (single, archive-older, project,
mission) and the archived-but-running sweep go through :func:`frozen`, which

1. holds the session's freeze fence (`freeze_lock`) **exclusively** — no launch decision of this
   session can start until the caller has recorded the archive and the block exits;
2. posts the cancel request for chat turns running in any process;
3. stops the runtime and **confirms** it gone, by runtime:

   * **native API session** → the operator's *Stop everything* (`native_runtime.stop` confirmed
     with ``everything: True`` for the current generation): it ends every process in the
     generation's unit — the agent, its background tasks, MCP servers, anything started with
     ``&``, ``setsid`` or a double fork, since none of them leave the unit's cgroup — and journals
     that work as ended by Stop. Containment must be ``gone``, which `native_runtime` only reports
     for a pinned invocation whose unit is inactive and whose cgroup is absent or empty;
   * **terminal session** → the idle reaper's tree + cgroup teardown (`cleanup_runtime` →
     `reaper.terminate_master`): the whole process tree rooted at the master is captured before
     any signal, and the transient scope's cgroup also covers children that reparented to init.
     ``leaked`` (something survived SIGKILL) or an exception is a failure. Then the single-writer
     lock is ACQUIRED and held for the block: proof that no master of this session runs anywhere,
     and no LAUNCH can win it until the archive is recorded;
   * **chat-runtime session** → every running turn, in any process, is cancelled by its own
     watcher; the freeze then takes the session's turn fence, which a turn releases only once it
     has settled (including in-flight writes). Holding it proves nothing runs.

4. yields to the caller, which records the archive; on exit it clears its marker, releases what it
   holds and finally the fence.

A runtime it cannot confirm stopped raises `FreezeFailed` BEFORE yielding, and the caller records
nothing: a session reported archived has had its runtime confirmed stopped.

What is never touched: a Claude background agent BattleLab does not own (the callers keep #631's
refusal), and anything that left BattleLab's boundary — handed to another service manager
(``systemd-run --user``), another host, or, on a host without scope isolation, double-forked and
reparented before the tree snapshot. Nothing is ever signalled by process name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import archive_gate, engines, freeze_lock, runtime_cleanup, sessionlock

log = logging.getLogger("agent_sessions.runtime_freeze")

#: How long a freeze waits for running chat turns (any process) to cancel and settle.
TURN_SETTLE_TIMEOUT = 30.0
#: Rounds of "stop the master, then take the writer lock" before a terminal freeze gives up. A
#: launch decision that was admitted just before the fence closed can still be spawning.
_TERMINAL_ROUNDS = 6
_TERMINAL_RETRY_S = 0.5


class FreezeFailed(Exception):
    """The runtime could not be confirmed stopped. Nothing may be recorded archived."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class NotArchived(Exception):
    """`frozen(require_archived=True)`: the session was unarchived before the fence was taken."""


def runtime_kind(prov) -> str:
    """``native`` · ``chat`` · ``terminal`` — where this engine's runtime lives."""
    runtime = getattr(getattr(prov, "manifest", None), "runtime", None)
    if runtime == "api":
        return "native"
    if runtime == "chat":
        return "chat"
    return "terminal"


async def _stop_native(engine: str, native: str) -> None:
    """Stop everything of the current generation and require containment ``gone``."""
    from . import native_runtime, native_state, shelf_broker

    try:
        gen = await asyncio.to_thread(native_runtime._generation, native)
        result: dict | None = None
        for attempt in range(2):
            try:
                if gen is None:
                    result = await native_runtime.stop(engine, native)
                else:
                    result = await native_runtime.stop(
                        engine, native, confirm={"worker": gen.worker_id, "everything": True}
                    )
                break
            except native_runtime.NativeError as exc:
                if exc.status == 409 and attempt == 0:
                    # A generation that was already starting when the fence closed replaced the
                    # one we confirmed: no successor can launch now, so confirm the current one.
                    gen = await asyncio.to_thread(native_runtime._generation, native)
                    continue
                raise FreezeFailed(f"the API session could not be stopped: {exc.detail}") from None
    except FreezeFailed:
        raise
    except (native_state.StateError, OSError) as exc:
        raise FreezeFailed(f"the API session could not be stopped: {exc}") from None
    containment = (result or {}).get("containment")
    if containment != "gone":
        raise FreezeFailed(
            "the API session's processes could not be confirmed stopped "
            f"(containment: {containment or 'unknown'})"
        )
    # Its mission shelf capability ends with the worker it was minted for (#1900).
    with contextlib.suppress(Exception):
        await asyncio.to_thread(shelf_broker.revoke_launch, f"{engine}:{native}")


async def _acquire_writer(key: str) -> sessionlock.SessionLock | None:
    """`sessionlock.acquire` off the loop, cancellation-safe: a lock the worker thread obtains
    after its awaiting freeze was cancelled is released, never dropped — a `SessionLock` owns a
    raw fd with no finalizer, so a dropped one leaves the session BUSY for the process's life."""
    task = asyncio.ensure_future(asyncio.to_thread(sessionlock.acquire, key))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        def _release(done: asyncio.Future) -> None:
            with contextlib.suppress(BaseException):
                late = done.result()
                if late is not None:
                    late.release()

        task.add_done_callback(_release)
        raise


async def _stop_terminal(engine: str, native: str, runtime_key: str) -> sessionlock.SessionLock:
    """Tear the master's tree + scope down, then hold the single-writer lock as proof."""
    for _round in range(_TERMINAL_ROUNDS):
        try:
            outcome = await runtime_cleanup.cleanup_runtime(
                engine, native, physical_key=runtime_key
            )
        except runtime_cleanup.UnresolvableRuntime as exc:
            raise FreezeFailed(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — a teardown that raised confirmed nothing
            raise FreezeFailed(f"the session could not be stopped ({type(exc).__name__})") from None
        if outcome == "leaked":
            raise FreezeFailed("a process of the session survived SIGKILL")
        lock = await _acquire_writer(runtime_key)
        if lock is not None:
            return lock
        # A launch admitted before the fence closed holds the writer lock until its master owns
        # it: wait, then stop that master too.
        await asyncio.sleep(_TERMINAL_RETRY_S)
    raise FreezeFailed("the session kept a live writer")


async def _settle_chat(prov, native: str):
    """Cancel running turns in every process, then hold the turn fence once they settled."""
    from . import chat_runtime

    try:
        root = chat_runtime._root(prov)
    except chat_runtime.ChatError as exc:
        raise FreezeFailed(exc.detail) from None
    chat_runtime.cancel_for_archive(f"{prov.engine_id}:{native}")
    deadline = time.monotonic() + TURN_SETTLE_TIMEOUT
    while True:
        try:
            fence = await chat_runtime._fence(root, native)
        except Exception as exc:  # noqa: BLE001 — a fence that cannot be read proves nothing
            raise FreezeFailed(f"the conversation's turn lock is unavailable: {exc}") from None
        if fence is not None:
            return fence
        if time.monotonic() >= deadline:
            raise FreezeFailed("a turn is still finishing")
        await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def frozen(prov, native: str, *, require_archived: bool = False):
    """Hold the session frozen for the block (see the module docstring). Raises `FreezeFailed`
    (or `NotArchived` with ``require_archived``) before yielding; nothing is held after it."""
    engine = prov.engine_id
    try:
        runtime_key = await runtime_cleanup.resolve_runtime_key(engine, native)
    except runtime_cleanup.UnresolvableRuntime as exc:
        raise FreezeFailed(str(exc)) from None
    try:
        held = await freeze_lock.acquire_async(
            runtime_key, exclusive=True, timeout=freeze_lock.EXCLUSIVE_TIMEOUT
        )
    except freeze_lock.FreezeBusy:
        raise FreezeFailed(
            "another archive, unarchive or launch of this session is in progress — try again"
        ) from None
    token: str | None = None
    proofs: list = []
    try:
        if require_archived:
            state = await asyncio.to_thread(archive_gate.effective_state, prov, native)
            if state != archive_gate.ARCHIVED:
                raise NotArchived(f"{engine}:{native}")
        token = await asyncio.to_thread(freeze_lock.write_marker, runtime_key)
        kind = runtime_kind(prov)
        if kind == "native":
            await _stop_native(engine, native)
        elif kind == "chat":
            proofs.append(await _settle_chat(prov, native))
        else:
            proofs.append(await _stop_terminal(engine, native, runtime_key))
        yield
    finally:
        # The marker goes BEFORE the fence is released: no launcher admitted afterwards can see it.
        if token is not None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(freeze_lock.clear_marker, runtime_key, token)
        for proof in proofs:
            with contextlib.suppress(Exception):
                proof.release()
        held.release()


# ---- the archived-but-running sweep --------------------------------------------------------

#: Seconds between sweeps after the boot pass. Env override; ``0`` disables the loop.
SWEEP_INTERVAL = 60.0


def _interval() -> float:
    raw = os.environ.get("AGENT_SESSIONS_ARCHIVE_SWEEP_INTERVAL")
    if raw is None:
        return SWEEP_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return SWEEP_INTERVAL


def _terminal_keys() -> set[str]:
    """Physical key of every terminal session with a live master (its writer lock is held)."""
    from . import ptybridge

    found: set[str] = set()
    try:
        entries = list(ptybridge.runtime_dir().iterdir())
    except OSError:
        log.warning("archive sweep: the runtime dir could not be listed", exc_info=True)
        return found
    for p in entries:
        if p.suffix != ".sock":
            continue
        engine, sep, native = p.stem.partition("-")
        if not (sep and engine and native) or engines.get_any(engine) is None:
            continue
        try:
            if ptybridge.socket_path(engine, native) != p:
                continue  # cannot name ONE session
        except Exception:  # noqa: BLE001, S112 — an unmappable name cannot name one session
            continue
        key = f"{engine}:{native}"
        with contextlib.suppress(Exception):
            if sessionlock.is_locked(key):
                found.add(key)
    return found


def _live_keys() -> set[str]:
    """Logical key of every session this process can see running."""
    from . import chat_runtime, native_runtime

    keys: set[str] = set()
    for phys in _terminal_keys():
        try:
            keys.add(engines.logical_key(phys))
        except Exception:  # noqa: BLE001
            keys.add(phys)
    with contextlib.suppress(Exception):
        keys |= native_runtime.running_session_keys()
    keys |= chat_runtime.running_keys()
    return keys


async def sweep_once() -> list[str]:
    """Freeze every running session whose effective state is ``archived``; return what stopped.
    ``unreadable`` is skipped — a sweep never stops a runtime on uncertainty."""
    stopped: list[str] = []
    keys = await asyncio.to_thread(_live_keys)
    for key in sorted(keys):
        try:
            prov, native = engines.parse_key(key)
            state = await asyncio.to_thread(archive_gate.effective_state, prov, native)
        except Exception:  # noqa: BLE001
            log.warning("archive sweep: could not read %s's archive state", key, exc_info=True)
            continue
        if state != archive_gate.ARCHIVED:
            continue
        try:
            async with frozen(prov, native, require_archived=True):
                pass
        except NotArchived:
            continue
        except FreezeFailed as exc:
            log.error("archive sweep: archived session %s is still running: %s", key, exc.reason)
            continue
        log.warning("archive sweep: stopped archived session %s", key)
        stopped.append(key)
    return stopped


async def run() -> None:
    """Boot pass, then one pass every `SWEEP_INTERVAL`.

    ``AGENT_SESSIONS_ARCHIVE_SWEEP_INTERVAL=0`` keeps only the boot pass;
    ``AGENT_SESSIONS_ARCHIVE_SWEEP_LOOP=0`` disables it (the test suite)."""
    if os.environ.get("AGENT_SESSIONS_ARCHIVE_SWEEP_LOOP", "1") == "0":
        return
    interval = _interval()
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the backstop must outlive one bad pass
            log.exception("archive sweep: pass failed")
        if interval <= 0:
            return
        await asyncio.sleep(interval)


__all__ = [
    "FreezeFailed",
    "NotArchived",
    "frozen",
    "run",
    "runtime_kind",
    "sweep_once",
]
