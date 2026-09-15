"""The periodic supervisor sweep (#885, Phase 5a of #840).

`mission_supervisor.run_pass` is the whole decision; this only decides WHEN to run it and over
which missions. Same reaper pattern as `orchestrator_loop` / `pulse_loop`, and the same gates,
because an operator who has switched autonomy off has switched THIS off too.

Gating — all must hold before a mission is swept:

* **Env kill-switch** ``AGENT_SESSIONS_MISSION_SUPERVISOR=0`` — the task exits at startup and
  never sweeps, whatever prefs say.
* **The orchestrator's own switches**, re-read every sweep: `enabled`, and `autonomy != "off"`.
  The supervisor nudges through the orchestrator's verb path, so it inherits its master switch
  rather than adding a second one an operator has to find.
* **Single-flight** — one supervisor sweep at a time, so a slow mission cannot overlap the next
  sweep and double-charge a budget.

Failures are swallowed per mission and logged: one stuck mission must not stop the fleet, which
is the whole complaint this feature exists to answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from . import aitasks, mission_supervisor, missions, prefs

log = logging.getLogger(__name__)

#: How often the fleet is swept. Deliberately slower than the orchestrator's own pass: a
#: supervisor that runs every minute is a supervisor that spends the operator's money re-reading
#: transcripts nobody wrote to. The model half is fingerprint-gated on top of this.
INTERVAL_S = 300.0

#: How many missions one sweep will visit. A ceiling on WORK, never on the set that work is drawn
#: from — conflating those two is the bug this file has now had twice. The worklist is a keyset
#: cursor over every eligible mission (see `sweep`), so a mission this sweep does not reach is
#: reached by a later one; there is no prefix and nothing past a boundary.
MISSIONS_PER_SWEEP = 25

#: The states a mission must be in to be supervised. `review` is included because a mission
#: proposed for completion can still be pushed back into motion by an operator edit, and dropping
#: it there would strand the missions closest to done.
ELIGIBLE_STATES = ("running", "review")

#: Where the last sweep stopped — the id it finished on, resumed with `id > cursor`.
#:
#: DURABLE, in `supervisor_state`. In process memory it reset on every restart, and a service that
#: restarts before completing a revolution re-selects the lowest ids forever while the tail is
#: never reached — fairness a restart silently erases is not fairness (#888 review, finding 6).
#: Restarts are routine here: a deploy, a crash, an operator toggling the orchestrator.
_CURSOR_KEY = "sweep_cursor"


def _enabled() -> bool:
    if os.getenv("AGENT_SESSIONS_MISSION_SUPERVISOR", "1") == "0":
        return False
    cfg = prefs.get_orchestrator()
    return bool(cfg.get("enabled")) and str(cfg.get("autonomy") or "off") != "off"


async def sweep(registry=None) -> dict:
    """One sweep over the eligible missions. Returns a small report for the caller/tests."""
    # BEFORE the enabled gate: a delivery made while the orchestrator was on still owes its thread
    # record after it is switched off, and this only ever writes that record (#983 review).
    await _reconcile_delivered()
    if not _enabled():
        return {"skipped": "disabled"}

    # A KEYSET CURSOR over the complete eligible set — not a rebuilt prefix of a list page.
    #
    # Two earlier spellings were both fair over the wrong set. Taking the first page of a
    # newest-first list never reached the tail; paging that list to a `WORKLIST_MAX` ceiling moved
    # the boundary without removing it, and logging the truncation does not supervise anything past
    # it. `missions.supervisor_worklist` walks `id > cursor` in id order, so there is no ceiling and
    # no prefix: the cursor advances through every eligible mission and wraps, which is what makes
    # "every eligible mission is eventually visited" true rather than aspirational.
    try:
        cursor = await missions.run_admitted(lambda: missions.get_supervisor_state(_CURSOR_KEY))
        batch = await missions.run_admitted(
            lambda: missions.supervisor_worklist(
                states=ELIGIBLE_STATES, after=cursor, limit=MISSIONS_PER_SWEEP
            )
        )
        if not batch and cursor is not None:
            # End of the ring — wrap, so a sweep never does nothing merely because the cursor sits
            # past the last id.
            batch = await missions.run_admitted(
                lambda: missions.supervisor_worklist(
                    states=ELIGIBLE_STATES, after=None, limit=MISSIONS_PER_SWEEP
                )
            )
    except Exception:  # noqa: BLE001 — a store hiccup must not kill the loop
        log.debug("mission supervisor: worklist unavailable", exc_info=True)
        return {"skipped": "worklist unavailable"}

    if not batch:
        with contextlib.suppress(Exception):
            await missions.run_admitted(lambda: missions.set_supervisor_state(_CURSOR_KEY, None))
        return {"swept": 0, "nudged": 0, "escalated": 0}

    out = {"swept": 0, "nudged": 0, "escalated": 0}
    for mid in batch:
        failed = False
        try:
            res = await mission_supervisor.run_pass(mid, registry=registry)
        except Exception as e:  # noqa: BLE001 — one stuck mission must not stop the fleet
            log.warning("mission %s: supervisor pass failed: %s", mid, e)
            failed = True
        # Advance after every ATTEMPT, not only after a success, and never before the attempt.
        #
        # Advancing before means a crash mid-batch skips work that never happened. Advancing only
        # on success means a mission that fails CONSISTENTLY pins the cursor to itself: when it is
        # the last eligible id, every sweep re-selects exactly that row, the page is never empty,
        # and the wrap at the top of `sweep` is never reached — one broken mission starves the
        # entire fleet (#888 review, finding 3). An attempt that failed is still an attempt.
        try:
            await missions.run_admitted(lambda m=mid: missions.set_supervisor_state(_CURSOR_KEY, m))
        except Exception as e:  # noqa: BLE001
            # A cursor that will not advance is not a slow sweep, it is a STUCK one: every
            # subsequent sweep re-selects the same low-id batch and the tail is never visited,
            # while the report still counts missions as swept. Suppressing that reported fair
            # progress the store had not actually made (#888 review, finding 7). Stop the sweep
            # and say so, rather than looping on a prefix.
            log.error("mission supervisor: cursor did not advance past %s: %s", mid, e)
            out["cursor_error"] = str(e)
            return out
        if failed:
            continue
        out["swept"] += 1
        if (res.get("nudged") or {}).get("sent"):
            out["nudged"] += 1
        if res.get("escalated"):
            out["escalated"] += 1
    return out


async def _reconcile_delivered() -> None:
    """Restore any delivered supervisor nudge's missing thread record (#983 review). Never raises.

    Runs where this loop already recovers state: once at boot, then at the top of every sweep —
    the supervisor is the producer of these deliveries, and the sweep interval bounds how long a
    thread can be missing one. It reads the ledger and writes events only; it never types.
    """
    from . import actuator

    try:
        n = await missions.run_admitted(actuator.reconcile_delivered_nudges)
    except Exception:  # noqa: BLE001 — a store hiccup is retried by the next sweep
        log.debug("mission supervisor: delivered-nudge reconciliation failed", exc_info=True)
        return
    if n:
        log.info("mission supervisor: restored %d delivered nudge record(s) to the thread", n)


async def run(registry=None) -> None:
    """The loop. Cancelled at shutdown like every other background task."""
    if os.getenv("AGENT_SESSIONS_MISSION_SUPERVISOR", "1") == "0":
        log.info("mission supervisor loop: disabled by env")
        return
    # BOOT: a record whose write failed just before a restart is restored now, not a sweep later.
    await _reconcile_delivered()
    failures = 0
    while True:
        await asyncio.sleep(INTERVAL_S * min(2**failures, 8))
        try:
            async with aitasks.single_flight("mission-supervisor", "sweep"):
                report = await sweep(registry)
        except aitasks.AlreadyRunning:
            continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            failures += 1
            log.exception("mission supervisor sweep crashed")
            continue
        failures = 0
        if report.get("swept"):
            log.info(
                "mission supervisor: swept %d, nudged %d, escalated %d",
                report["swept"],
                report["nudged"],
                report["escalated"],
            )


__all__ = ["INTERVAL_S", "MISSIONS_PER_SWEEP", "run", "sweep"]
