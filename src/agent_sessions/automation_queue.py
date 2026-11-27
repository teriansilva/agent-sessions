"""The automation queue (#2422): an automation step that would launch a session waits for a free
slot instead of failing, and part of every launch limit is reserved for automations.

**One gate, at the launch seam** (`launch_slot`). The only step that launches is
``start_session``. Before anything is created, the gate asks both launch limits with
``origin=automation``: the session limit (`session_capacity.admit`) and, for a specialist, the
pool and the specialist's own capacity (`specialist_launch.for_generation`, one transaction in
`specialists_store.reserve`). Granted, the step HOLDS both (`LaunchHold`) and the launch uses
exactly those reservations (`native_runtime.create(hold=)`, `headless_dispatch.dispatch(
capacity_hold=)`). Refused by a limit, the granted half is released, nothing has been created,
and the run goes back to ``queued`` with which limit and why (`Blocked`). Any other refusal (an
archived specialist, a changed skill) refuses the run as before. A launch that is nonetheless
refused by a limit reports it TYPED (`Dispatch.capacity`), never in words.

**Durable, FIFO, bounded.** Queued runs live in the run ledger (`automations_store`, state
``queued``). Only the scheduler's owner drains (`drain`), each tick and on a coalesced wake-up
when a slot is released (`wake`). The drain walks queued runs oldest first, ``(created_at, id)``,
in pages of `PAGE`, and continues past a blocked prefix: a gate found full once in a pass (the
session limit, the pool, one specialist, one automation's ``max_concurrent``) marks every later
run needing it blocked without asking again, so rows blocked on one specialist never hide an
eligible row behind them. ``queued → dispatching`` is one transaction
(`automations_store.claim_queued`) that re-reads consent and ``max_concurrent`` and checks the run
is the oldest eligible one. A new arrival never jumps a waiter: while any run waits, a fresh one
queues behind it and the drain decides. Expiry (`automations_store.expire_queued`,
`QUEUE_MAX_WAIT_S`) is its own indexed update each tick, never bounded by the drain.

Launch work stays off the event loop: every census and store call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from . import automations_store as store
from . import session_capacity

log = logging.getLogger(__name__)

#: Queued runs read per page of the drain's walk, and the most it starts in one pass.
PAGE = 50
MAX_STARTS_PER_DRAIN = 50

#: The drain's gates: everything a start needs needs the session limit.
GATE_SESSION = "session"
GATE_POOL = "pool"


def gate_specialist(sid: str) -> str:
    return f"specialist:{sid}"


def gate_concurrency(aid: str) -> str:
    return f"automation:{aid}"


@dataclass
class LaunchHold:
    """The launch slots one automation step holds for its session, taken before anything was
    created. The launch takes each over (`take_session` / `take_claim`); `release` gives back
    whatever it did not take. Idempotent."""

    key: str | None
    session: session_capacity.Reservation | None = None
    claim: object = None  # specialist_launch.Claim

    def take_session(self) -> session_capacity.Reservation | None:
        slot, self.session = self.session, None
        return slot

    def take_claim(self):
        claim, self.claim = self.claim, None
        return claim

    def release(self) -> None:
        """Give back what the launch did not take. QUIET: no wake-up, since a hold given back
        only returns the capacity it took moments ago — a wake here would make a drain that
        holds and returns a slot wake itself for ever (Hermes on #2425)."""
        slot, claim = self.take_session(), self.take_claim()
        if slot is not None:
            slot.release()
        if claim is not None:
            from . import specialist_launch

            specialist_launch.abandon(claim, wake=False)


@dataclass(frozen=True)
class Blocked:
    """A launch limit is full: nothing was created. ``capacity`` is the typed reason
    (``session_limit`` | ``pool`` | ``specialist``), ``gate`` the drain's key for it."""

    capacity: str
    gate: str
    detail: str


def _action(run: dict) -> dict:
    return (((run.get("scope") or {}).get("config") or {}).get("action")) or {}


def _specialist_pin(run: dict) -> dict | None:
    action = _action(run)
    if not action.get("specialist"):
        return None
    pin = ((run.get("scope") or {}).get("pins") or {}).get("specialist")
    return pin if isinstance(pin, dict) else None


def launches(run: dict) -> bool:
    """Whether this run's step launches a session (the only kind the queue gates)."""
    return _action(run).get("kind") == "start_session"


def needs(run: dict) -> frozenset[str]:
    """The gates a queued run needs: its automation's concurrency, and for a launch the session
    limit — plus the pool and its specialist when it starts one."""
    out = {gate_concurrency(run["automation_id"])}
    if launches(run):
        out.add(GATE_SESSION)
        spec = _specialist_pin(run)
        if spec is not None:
            out |= {GATE_POOL, gate_specialist(str(spec.get("id")))}
    return frozenset(out)


def structured_key(run: dict) -> str | None:
    """The session key an API ``start_session`` will create — derived from the run id, so it is
    known before anything exists (`mission_structured.automation_ids`). None for a console one,
    whose id is minted by its launch."""
    from . import engines, mission_structured

    engine = _action(run).get("engine")
    prov = engines.get(engine) if isinstance(engine, str) else None
    if prov is None or not mission_structured.is_api(prov):
        return None
    session_id, _brief, _send = mission_structured.automation_ids(run["id"])
    return f"{engine}:{session_id}"


def launch_slot(run: dict) -> LaunchHold | Blocked:
    """THE GATE (blocking, off the loop): hold every launch slot this run's step needs, with
    ``origin=automation``, or say which limit is full. Nothing is created either way.

    Raises `specialist_launch.Refused` for a refusal that is not a limit's (an archived
    specialist, a skill changed): that refuses the run, it never queues it."""
    from . import specialist_launch
    from . import specialists_store as sstore

    key = structured_key(run)
    try:
        slot = session_capacity.admit(key, origin=session_capacity.AUTOMATION)
    except session_capacity.SessionLimitReached as exc:
        return Blocked(
            session_capacity.SESSION_LIMIT,
            GATE_SESSION,
            f"the session limit is full ({exc.running} of {exc.limit} running)",
        )
    hold = LaunchHold(key=key, session=slot)
    spec = _specialist_pin(run)
    if spec is None or key is None:
        return hold
    try:
        hold.claim = specialist_launch.for_generation(
            key,
            {"id": str(spec["id"]), "version": int(spec["version"])},
            None,
            origin=sstore.ORIGIN_AUTOMATION,
        )
    except specialist_launch.Refused as exc:
        hold.release()
        if exc.capacity == "pool":
            return Blocked("pool", GATE_POOL, exc.detail)
        if exc.capacity == "specialist":
            return Blocked("specialist", gate_specialist(str(spec["id"])), exc.detail)
        raise
    except BaseException:
        hold.release()
        raise
    return hold


async def acquire(run: dict) -> LaunchHold | Blocked:
    """`launch_slot` on a worker thread, its result OWNED through cancellation: the worker may
    win both slots after the cancellation arrives, and a hold nobody references would keep a
    durable specialist reservation with no launch. On cancellation the attempt is joined
    (shielded) and whatever it holds is released before the cancellation propagates
    (as `automation_effect_lock._try_owned`). Raises what `launch_slot` raises."""
    task = asyncio.ensure_future(asyncio.to_thread(launch_slot, run))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None:
            got = task.result()
            if isinstance(got, LaunchHold):
                got.release()
        raise


async def _claim_owned(run: dict, passed: frozenset[str], now: float) -> dict:
    """`claim_queued`, its result OWNED through cancellation: a claim that committed after the
    cancellation arrived left the run `dispatching` with nothing to start it; since nothing was
    created, it goes back to the queue (`requeue`) before the cancellation propagates."""
    task = asyncio.ensure_future(
        asyncio.to_thread(store.claim_queued, run["id"], passed=passed, now=now)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if not task.cancelled() and task.exception() is None:
            if task.result()["verdict"] == store.CLAIM_RUN:
                with contextlib.suppress(Exception):
                    store.requeue(
                        run["id"],
                        run.get("queue_capacity") or "session_limit",
                        run.get("reason") or "waiting for a free slot",
                    )
        raise


def _gate_rank(gate: str) -> int:
    """Which full gate a passed-over run is told it waits for: the widest first."""
    for i, prefix in enumerate((GATE_SESSION, GATE_POOL, "specialist:", "automation:")):
        if gate.startswith(prefix):
            return i
    return 9


# ---- the drain ----------------------------------------------------------------------------------


@dataclass
class DrainStats:
    """What one pass did — the burst test's measurements (#2422 rule 4)."""

    scanned: int = 0
    started: int = 0
    blocked: int = 0
    skipped: int = 0
    expired: int = 0
    pages: int = 0
    gate_asks: int = 0
    duration_s: float = 0.0
    started_ids: list[str] = field(default_factory=list)


_drain_locks: dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _drain_locks.get(id(loop))
    if lock is None:
        lock = _drain_locks[id(loop)] = asyncio.Lock()
    return lock


async def drain(*, registry, now: float | None = None) -> DrainStats:
    """One pass over the queue: expire what waited too long, then start every eligible waiter,
    oldest first, while slots are free. Owner-only (the caller checks); one pass at a time per
    process, and across processes `claim_queued`'s compare-and-set decides."""
    from . import automation_runner as runner

    global _maybe_waiting
    stats = DrainStats()
    async with _lock():
        t0 = time.perf_counter()
        ts = time.time() if now is None else now
        # Consent FIRST, for every waiter, whatever the launch limits say (Hermes on #2425): a run
        # whose automation was turned off, deleted, paused, edited or needs approval settles
        # `skipped` now — never left behind a full gate, and never expired as a capacity failure.
        try:
            stats.skipped += len(await asyncio.to_thread(store.settle_invalid_queued, now=ts))
        except Exception:  # noqa: BLE001 — `claim_queued` re-checks consent anyway
            log.warning("automation queue: the consent pass failed", exc_info=True)
        try:
            expired = await asyncio.to_thread(store.expire_queued, now=ts)
        except Exception:  # noqa: BLE001 — retried next tick; the walk still runs
            log.warning("automation queue: expiry failed", exc_info=True)
            expired = []
        for group in expired:
            stats.expired += len(group["runs"])
            if group["episode"]:
                await asyncio.to_thread(runner.announce_no_slot, group)
        # Each gate found full in this pass, with what it said; every later run needing it is
        # passed over without asking again, and told (in one write) what it waits for.
        blocked: dict[str, Blocked] = {}
        passed: set[str] = set()
        notes: list[tuple[str, str, str]] = []
        after: tuple[float, str] | None = None
        done = False
        while not done and stats.started < MAX_STARTS_PER_DRAIN:
            page = await asyncio.to_thread(store.queued_page, after, PAGE)
            stats.pages += 1
            if not page:
                break
            for run in page:
                after = (run["created_at"], run["id"])
                stats.scanned += 1
                hit = needs(run) & blocked.keys()
                if hit:
                    why = blocked[min(hit, key=_gate_rank)]
                    notes.append((run["id"], why.capacity, why.detail))
                    passed.add(run["id"])
                    stats.blocked += 1
                    continue
                hold: LaunchHold | None = None
                if await asyncio.to_thread(store.concurrency_full, run["automation_id"]):
                    # Its own automation is at `max_concurrent`: no slot is taken for it at all
                    # (a hold taken and given back would be work for nothing). `claim_queued`
                    # still decides, in its transaction.
                    why = Blocked(
                        "concurrency",
                        gate_concurrency(run["automation_id"]),
                        store.QUEUE_CONCURRENCY_DETAIL,
                    )
                    blocked[why.gate] = why
                    notes.append((run["id"], why.capacity, why.detail))
                    passed.add(run["id"])
                    stats.blocked += 1
                    continue
                if launches(run):
                    stats.gate_asks += 1
                    try:
                        got = await acquire(run)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — a refusal: the run itself settles it
                        got = None
                    if isinstance(got, Blocked):
                        blocked[got.gate] = got
                        notes.append((run["id"], got.capacity, got.detail))
                        passed.add(run["id"])
                        stats.blocked += 1
                        if got.gate == GATE_SESSION:
                            # Every start needs the session limit: nothing after this can start.
                            done = True
                            break
                        continue
                    hold = got
                try:
                    res = await _claim_owned(run, frozenset(passed), ts)
                except BaseException:
                    if hold is not None:
                        hold.release()
                    raise
                verdict = res["verdict"]
                if verdict == store.CLAIM_RUN:
                    runner.spawn(res["run"], registry=registry, hold=hold)
                    stats.started += 1
                    stats.started_ids.append(run["id"])
                    if stats.started >= MAX_STARTS_PER_DRAIN:
                        done = True
                        break
                    continue
                if hold is not None:
                    hold.release()
                if verdict == store.CLAIM_CONCURRENCY:
                    why = Blocked(
                        "concurrency",
                        gate_concurrency(run["automation_id"]),
                        store.QUEUE_CONCURRENCY_DETAIL,
                    )
                    blocked[why.gate] = why
                    notes.append((run["id"], why.capacity, why.detail))
                    passed.add(run["id"])
                    stats.blocked += 1
                elif verdict == store.CLAIM_SETTLED:
                    stats.skipped += 1
                elif verdict == store.CLAIM_NOT_FIRST:
                    # An older waiter this walk did not see (a peer's write): decide next pass.
                    done = True
                    break
        if notes:
            with contextlib.suppress(Exception):  # display only; the next pass writes it again
                await asyncio.to_thread(store.note_waiting_many, notes)
        # Something still waits if the walk stopped early or passed a blocked run.
        _maybe_waiting = done or bool(passed)
        stats.duration_s = time.perf_counter() - t0
    if stats.started or stats.expired or stats.skipped:
        log.info(
            "automation queue: started %d, expired %d, skipped %d, scanned %d in %.3f s",
            stats.started,
            stats.expired,
            stats.skipped,
            stats.scanned,
            stats.duration_s,
        )
    return stats


# ---- coalesced wake-ups -------------------------------------------------------------------------
#
# A released slot (a specialist reservation, a finished run) asks for a drain. Wake-ups coalesce:
# at most one drain runs and at most one more is pending, so a burst of releases never rescans the
# same backlog once per release. Nothing is woken while the queue is known empty.

_loop: asyncio.AbstractEventLoop | None = None
_task: asyncio.Task | None = None
_again = False
#: False only once a drain saw the queue empty; any queueing sets it again.
_maybe_waiting = True


def bind(loop: asyncio.AbstractEventLoop | None) -> None:
    """The scheduler's loop: where a wake-up from any thread runs its drain. None unbinds."""
    global _loop
    _loop = loop


def queued() -> None:
    """A run was just queued: the next wake-up must not be skipped as "nothing waits"."""
    global _maybe_waiting
    _maybe_waiting = True


def wake() -> None:
    """Ask for a drain soon. Thread-safe, never blocks, never raises; coalesced."""
    loop = _loop
    if loop is None or not _maybe_waiting or loop.is_closed():
        return
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_kick)


def _kick() -> None:
    global _task, _again
    if _task is not None and not _task.done():
        _again = True
        return
    _task = asyncio.get_running_loop().create_task(_woken(), name="automation-queue-wake")


async def _woken() -> None:
    global _again
    from . import automation_loop

    while True:
        _again = False
        sched = automation_loop.SCHEDULER
        if sched is None or not sched.owner or sched.stopping:
            return
        try:
            await drain(registry=sched.registry)
        except Exception:  # noqa: BLE001 — the next tick drains again
            log.warning("automation queue: a woken drain failed", exc_info=True)
            return
        if not _again:
            return


async def stop() -> None:
    """Shutdown: no new wake-up drains; one already running is cancelled and joined."""
    global _task
    bind(None)
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
