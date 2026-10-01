"""The automation scheduler (#1201 Phase 1) — due checks, durable claims, crash recovery.

Follows #1042's discipline:

* **Kill switch** ``AGENT_SESSIONS_AUTOMATION_LOOP=0``: the loop never starts and every trigger,
  Run now included, is refused.
* **A non-inherited kernel ownership lock** (``flock`` on an ``O_CLOEXEC`` descriptor beside the
  store) is taken BEFORE recovery or firing, so a second instance sharing the store never mistakes a
  live peer's run for an interrupted one, and never fires beside it. A crash releases it. A
  non-owner keeps trying each tick, so a peer's death hands over within one interval.
* **Due slots are claimed durably** — ``automations_store.begin_run`` claims ``(automation, slot)``
  and records the run ``dispatching`` in ONE transaction that re-reads consent. That claim, not
  the lock, is what makes a slot single-fire across restarts and peers.
* **Missed slots collapse** into at most ONE catch-up run recording how many slots it covered, and
  an overdue backlog waits out a 10-minute startup grace first. The backlog is never replayed.
* **Interrupted is never replayed.** Each tick, every ``dispatching`` run whose owner PROCESS is
  gone becomes ``interrupted: outcome unknown``; its claim stands, so the slot never fires again. A
  live owner's run is never touched, and an outcome the store refused is retried until it lands.
* **Shutdown** stops firing first, then drains the runs already started.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import threading
import time
from pathlib import Path

from . import automation_runner as runner
from . import automations as model
from . import automations_store as store
from . import missions

log = logging.getLogger(__name__)

INTERVAL_S = 30.0
#: A slot fired this late is still "on time"; later than this it is a missed slot.
ON_TIME_S = 120.0
#: After startup an OVERDUE backlog waits this long before its one catch-up run (#1042).
STARTUP_GRACE_S = 600.0
RETENTION_EVERY_S = 3600.0
ONCE_LATE_S = 24 * 3600.0
RECEIPT_OUTDATED = store.RECEIPT_MISMATCH


def loop_enabled() -> bool:
    return runner.loop_enabled()


def lock_path() -> Path:
    p = store.db_path()
    return p.with_name(p.name + ".owner.lock")


class Scheduler:
    """One app instance's scheduler. ``tick`` is the whole due check, callable from tests."""

    def __init__(self, *, registry=None, clock=time.time, started_at: float | None = None) -> None:
        self.registry = registry
        self.clock = clock
        self.started_at = clock() if started_at is None else started_at
        self._fd: int | None = None
        self._stopping = False
        # Guards `_fd` and `_stopping` together, so a worker that wins the flock AFTER shutdown's
        # release can never store it: it sees `_stopping` under this lock and lets go.
        self._own_lock = threading.Lock()
        self._acquiring: asyncio.Future | None = None
        self._last_retention = 0.0
        self._task: asyncio.Task | None = None

    # ---- ownership ------------------------------------------------------------------------------

    def try_own(self) -> bool:
        """Take the kernel ownership lock, non-blocking. ``O_CLOEXEC``: no agent this app execs
        inherits it, so an unattended session can never keep a dead scheduler's lock alive."""
        with self._own_lock:
            if self._fd is not None:
                return True
            if self._stopping:
                return False
        p = lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._after_flock()
        with self._own_lock:
            if self._stopping or self._fd is not None:
                # Won AFTER shutdown began (or a racing attempt already owns): never keep it.
                _drop(fd)
                return self._fd is not None and not self._stopping
            self._fd = fd
            return True

    def _after_flock(self) -> None:
        """Test seam: runs on the worker thread between winning the flock and storing it."""

    def release(self) -> None:
        with self._own_lock:
            fd, self._fd = self._fd, None
        if fd is not None:
            _drop(fd)

    async def _own(self) -> bool:
        """``try_own`` on a worker thread, with the attempt OWNED through cancellation: a cancelled
        tick joins the attempt before the cancellation propagates, and shutdown awaits it before
        its final release — so no late win can outlive the release."""
        task = asyncio.ensure_future(asyncio.to_thread(self.try_own))
        self._acquiring = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _join(task)
            raise
        finally:
            if task.done():
                self._acquiring = None

    @property
    def owner(self) -> bool:
        return self._fd is not None

    # ---- the due check --------------------------------------------------------------------------

    def _before_claim(self, aid: str) -> None:
        """Test seam for the barrier: runs between the snapshot and the claim transaction."""

    async def tick(self) -> dict:
        now = self.clock()
        if not loop_enabled() or self._stopping:
            return {"owner": False, "fired": []}
        if not await self._own():
            # A NON-owner still settles its OWN parked outcomes: a Run now this process started
            # whose outcome the store refused lives only in this process's memory, so no owner
            # could ever record it — and the owner never touches a live peer's run (#1201 review).
            await runner.retry_unsettled()
            return {"owner": False, "fired": []}
        # EVERY tick, not only at takeover: a peer that dies during its own Run now leaves a run
        # only an owner can settle. Runs of a live owner are never touched.
        ids = await asyncio.to_thread(store.recover_interrupted, now=now)
        if ids:
            log.warning("automations: %d interrupted run(s) recorded, none replayed", len(ids))
        await runner.retry_unsettled()
        await self._sync_missions()
        fired: list[dict] = []
        for row in await asyncio.to_thread(store.list_all):
            try:
                run = await self._due(row, now)
            except Exception:  # noqa: BLE001 — one automation's failure must not stop the rest
                log.warning("automations: due check for %s failed", row["id"], exc_info=True)
                continue
            if run is not None:
                fired.append(run)
        if now - self._last_retention >= RETENTION_EVERY_S:
            self._last_retention = now
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.prune_runs, now=now)
        return {"owner": True, "fired": fired}

    async def _due(self, row: dict, now: float) -> dict | None:
        config = row["config"]
        if (
            config is None
            or not row["enabled"]
            or row["needs_reapproval"]
            or row["consented_at"] is None
        ):
            return None
        # A row that can never fire again reads EXPIRED or FINISHED; asking the operator to
        # re-approve something that will never run is noise, so nothing below applies to it.
        expires = config["policy"]["expires_at"]
        if expires is not None and now >= expires:
            return None
        if config["trigger"]["kind"] == "once":
            mark = await asyncio.to_thread(store.watermark, row["id"])
            base = max(float(row["active_since"] or 0), float(mark["fire_at"]) if mark else 0.0)
            if model.next_slot(config["trigger"], base) is None:
                return None
        # EVERY other enabled automation — Run-now-only and paused ones too — is checked for a
        # receipt that no longer matches its config BEFORE the remaining early returns: the claim
        # refuses such a row, and without the flag a Run-now-only automation would stay refused
        # with nothing to approve (#1252 review). One expected cause: a receipt from before the
        # consent lines disclosed every field.
        if row["consented_scope"] != model.scope_of(config, row["pins"]):
            await asyncio.to_thread(
                store.flag_reapproval,
                row["id"],
                RECEIPT_OUTDATED,
                observed_revision=row["revision"],
                receipt_mismatch=True,
            )
            return None
        if row["paused"]:
            return None
        # An approved input that changed pauses the automation for re-approval (#1201 round 2) —
        # but only an AFFIRMATIVE drift. An input that cannot be resolved right now (an unreadable
        # store) skips this check with its reason recorded, and the next tick tries again.
        try:
            current = await asyncio.to_thread(model.compute_pins, config)
        except model.PinsUnavailable as e:
            await asyncio.to_thread(store.set_check_note, row["id"], f"not checked: {e}")
            return None
        if row["check_note"]:
            await asyncio.to_thread(store.set_check_note, row["id"], "")
        drift = model.pins_drift(row["pins"], current)
        if drift:
            await asyncio.to_thread(
                store.flag_reapproval, row["id"], drift, observed_revision=row["revision"]
            )
            return None
        # Only now does the trigger matter: a Run-now-only automation is checked for drift above
        # like every other, so its approval path exists before anyone presses Run now (#1252).
        trigger = config["trigger"]
        if trigger["kind"] not in ("once", "schedule"):
            return None
        mark = await asyncio.to_thread(store.watermark, row["id"])
        base = max(
            float(row["active_since"] or row["consented_at"] or now),
            float(mark["fire_at"]) if mark else float("-inf"),
        )
        due = model.due_slots(trigger, base, now)
        if not due["count"]:
            return None
        first_at = due["first"][1]
        overdue = first_at < now - ON_TIME_S
        if overdue and now - self.started_at < STARTUP_GRACE_S:
            return None  # a backlog waits out the startup grace, then collapses into one run
        catch_up = overdue or due["count"] > 1
        slot, fire_at = due["last"]
        # A one-time run that is a day late is not what the operator asked for: record why it did
        # not run rather than firing it into a different day.
        skip = (
            "skipped: missed by more than 24 h"
            if trigger["kind"] == "once" and now - fire_at > ONCE_LATE_S
            else ""
        )
        self._before_claim(row["id"])
        res = await asyncio.to_thread(
            store.begin_run,
            row["id"],
            trigger=trigger["kind"],
            slot=slot,
            fire_at=fire_at,
            catch_up=catch_up,
            covered=due["count"],
            now=now,
            force_skip=skip,
            expect_revision=row["revision"],
        )
        run = res["run"]
        if run is None:
            return None
        if run["state"] == "dispatching":
            runner.spawn(run, registry=self.registry)
        return run

    async def _sync_missions(self) -> None:
        """A dispatched mission's run concludes with the MISSION: done → ok, failed/abandoned →
        failed, review → review. Brief delivery alone is never reported as success."""
        for run in await asyncio.to_thread(store.pending_missions):
            try:
                m = await missions.run_admitted(
                    lambda mid=run["mission_id"]: missions.get_mission(mid, events_limit=1)
                )
            except Exception:  # noqa: BLE001 — try again next tick
                log.debug("automations: mission %s unreadable", run["mission_id"], exc_info=True)
                continue
            if m is None:
                outcome, reason = "failed", "the mission no longer exists"
            else:
                state = str(m.get("state") or "")
                if state == "done":
                    outcome, reason = "ok", "mission done"
                elif state in ("failed", "abandoned"):
                    outcome, reason = "failed", f"mission {state}"
                elif state == "review":
                    outcome, reason = "review", "mission is waiting for your review"
                else:
                    continue
            res = await asyncio.to_thread(store.conclude, run["id"], outcome, reason)
            if res:
                await asyncio.to_thread(runner.notify, res)

    # ---- lifecycle ------------------------------------------------------------------------------

    async def run(self) -> None:
        if not loop_enabled():
            log.info("automations: loop disabled (AGENT_SESSIONS_AUTOMATION_LOOP=0)")
            return
        try:
            while not self._stopping:
                try:
                    await self.tick()
                except store.StoreUnsupported as e:
                    log.error("automations: %s", e)
                except Exception:  # noqa: BLE001 — never take the app down over a due check
                    log.warning("automations: a due check failed", exc_info=True)
                await asyncio.sleep(INTERVAL_S)
        finally:
            if self._stopping:
                self.release()

    async def shutdown(self) -> None:
        """Stop firing FIRST, then drain the runs already started, then release ownership.

        The due-check task is cancelled and joined before the drain looks at what is running, so
        no run can be spawned after it. A claim that committed as the tick was cancelled leaves a
        ``dispatching`` run nobody started; the next owner records it ``interrupted``."""
        with self._own_lock:
            self._stopping = True  # from here no acquisition can be stored
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._acquiring is not None:
            await _join(self._acquiring)
        await runner.drain()
        self.release()


def _drop(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


async def _join(task: asyncio.Future) -> None:
    """Wait until ``task`` has FINISHED, however often the waiter is cancelled."""
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    if not task.cancelled():
        task.exception()  # retrieved, so it is never reported as unobserved


#: The lifespan's scheduler, for the routes that report it.
SCHEDULER: Scheduler | None = None


def start(registry) -> asyncio.Task:
    global SCHEDULER
    SCHEDULER = Scheduler(registry=registry)
    SCHEDULER._task = asyncio.get_running_loop().create_task(
        SCHEDULER.run(), name="automation-loop"
    )
    return SCHEDULER._task


async def stop() -> None:
    sched = SCHEDULER
    if sched is not None:
        await sched.shutdown()
    else:
        await runner.drain()
