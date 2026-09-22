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
import time

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

#: THE FIRST READING SHOULD NOT WAIT A SWEEP (#1064). A mission that has just gone `running` is the
#: one the operator is most likely watching, and the ordinary cadence left it silent for up to
#: `INTERVAL_S` — four minutes after dispatch it showed a plan row and a dispatch row and nothing
#: about what its agent was doing. So the launch asks for ONE early reading, due this long after
#: it lands: at dispatch the agent has only just been briefed, and a recap of a screen with
#: nothing on it is a call spent on nothing.
EARLY_READING_DELAY_S = 45.0

#: How many attempts one launch's early reading gets. An attempt whose sessions were all skipped
#: before the model — nothing changed yet, nothing to review — is re-armed at the same spacing; a
#: skip costs no model call, so this bounds WAITING, not spend. The early path makes at most one
#: model call per launch, and stops at the first attempt that produced a reading. Past the cap the
#: mission is read at the next ordinary sweep, which is exactly today's behaviour.
EARLY_READING_ATTEMPTS = 3
#: How long a due early reading waits when the supervisor's single-flight is already held. Both
#: acquisitions live inside this loop today, so this cannot fire now — it exists so a future second
#: holder turns into a short wait rather than a zero-timeout spin (#1064 review).
EARLY_CONTENTION_BACKOFF_S = 5.0

#: Pending early readings: mission id -> (due, on the monotonic clock; attempts already made).
#: IN MEMORY, deliberately. A restart loses a pending request and the mission is read at the next
#: sweep — the behaviour before #1064 — so there is nothing to persist and nothing to recover.
_early: dict[str, tuple[float, int]] = {}
#: Set by `run()` so a request can wake a loop that would otherwise sleep the whole interval. `None`
#: when no loop is running (tests, tooling), in which case a request is simply recorded.
_wake: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None


def request_early_pass(mission_id: str, *, now: float | None = None) -> None:
    """Ask for this mission's first supervisor reading soon, rather than at the next sweep (#1064).

    Called from the one place a launch becomes `running` with the mission owning its session
    (`mission_dispatch._conclude`). Never raises and never blocks: it records a due time and wakes
    the loop. A second request for the same mission restarts its schedule rather than stacking.
    """
    if not mission_id:
        return
    t = time.monotonic() if now is None else now
    _early[mission_id] = (t + EARLY_READING_DELAY_S, 0)
    _poke()


def _poke() -> None:
    """Wake `run()` so it recomputes when it next has work. Safe from any thread."""
    ev, loop = _wake, _wake_loop
    if ev is None or loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        ev.set()
    else:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(ev.set)


def defer_due(now: float, by: float = EARLY_CONTENTION_BACKOFF_S) -> None:
    """Push every due early reading to `now + by`, keeping its attempt count — contention is not an
    attempt, and the request is never dropped because someone else held the flight."""
    for mid, (due, made) in list(_early.items()):
        if due <= now:
            _early[mid] = (now + by, made)


def due_early(now: float) -> list[str]:
    """The missions whose early reading is due at `now`, in a stable order."""
    return sorted(m for m, (due, _) in _early.items() if due <= now)


def next_early_due() -> float | None:
    """When the next early reading falls due, or `None` when none is pending."""
    return min((due for due, _ in _early.values()), default=None)


def _produced_a_reading(res: dict | None) -> bool:
    """Did this pass actually READ a session — the model ran, rather than being skipped first?"""
    if not isinstance(res, dict):
        return False
    return any(
        isinstance(r, dict) and not r.get("skipped_model") and r.get("assessment") is not None
        for r in res.get("per_session") or []
    )


def _worth_retrying(res: dict | None) -> bool:
    """Would another attempt shortly have a chance to read something?

    Yes when every session was skipped for a reason that time can cure — the screen had not changed
    yet, or there was nothing to review yet. No when the pass never reached a session (the mission
    left `running`, holds no session, needs the operator) or the model is not configured at all:
    retrying those only spends attempts the ordinary sweep will make anyway.
    """
    if not isinstance(res, dict) or res.get("skipped"):
        return False
    per = [r for r in res.get("per_session") or [] if isinstance(r, dict)]
    if not per:
        return False
    for r in per:
        why = str(r.get("skipped_model") or "")
        if "no AI endpoint" in why:
            return False
    return True


async def run_due_early(registry=None, *, now: float | None = None) -> dict:
    """Run each due early reading once. Returns ``{mission_id: "read" | "re-armed" | "gave up"}``.

    The caller holds the same single-flight a sweep does, so an early pass and a sweep never
    overlap; and the same `_enabled()` gate applies — an operator who switched supervision off has
    not asked for an early reading either, so pending requests are dropped rather than kept.
    """
    if not _enabled():
        _early.clear()
        return {"skipped": "disabled"}
    t = time.monotonic() if now is None else now
    report: dict[str, str] = {}
    for mid in due_early(t):
        _due, made = _early.pop(mid)
        res: dict | None = None
        try:
            res = await mission_supervisor.run_pass(mid, registry=registry)
        except Exception as e:  # noqa: BLE001 — one mission's early read must not stop the loop
            log.warning("mission %s: early supervisor reading failed: %s", mid, e)
        made += 1
        if _produced_a_reading(res):
            report[mid] = "read"
        elif made < EARLY_READING_ATTEMPTS and _worth_retrying(res):
            _early[mid] = (t + EARLY_READING_DELAY_S, made)
            report[mid] = "re-armed"
        else:
            report[mid] = "gave up"
    return report


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
    global _wake, _wake_loop
    _wake = asyncio.Event()
    _wake_loop = asyncio.get_running_loop()
    failures = 0
    next_sweep = time.monotonic() + INTERVAL_S
    try:
        while True:
            # WAKEABLE, not a flat sleep (#1064). The sweep cadence is exactly what it was — the
            # timeout is still the next sweep, backoff included — but an early reading that falls
            # due first is served when it is due, and a new request wakes the wait so it is seen.
            # Cleared BEFORE the target is computed, so a request landing in between still wakes it.
            _wake.clear()
            pending = next_early_due()
            target = next_sweep if pending is None else min(next_sweep, pending)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_wake.wait(), max(0.0, target - time.monotonic()))
            now = time.monotonic()

            if due_early(now):
                try:
                    async with aitasks.single_flight("mission-supervisor", "sweep"):
                        early = await run_due_early(registry, now=now)
                    if any(v == "read" for v in early.values() if isinstance(v, str)):
                        log.info("mission supervisor: early readings %s", early)
                except aitasks.AlreadyRunning:
                    # Someone else holds the flight. Keep the requests and their attempt counts,
                    # but WAIT: left due, the next iteration's timeout would be zero — a spin.
                    defer_due(now)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("mission supervisor early reading crashed")

            if now < next_sweep:
                continue
            try:
                async with aitasks.single_flight("mission-supervisor", "sweep"):
                    report = await sweep(registry)
            except aitasks.AlreadyRunning:
                next_sweep = time.monotonic() + INTERVAL_S * min(2**failures, 8)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception("mission supervisor sweep crashed")
                next_sweep = time.monotonic() + INTERVAL_S * min(2**failures, 8)
                continue
            failures = 0
            next_sweep = time.monotonic() + INTERVAL_S
            if report.get("swept"):
                log.info(
                    "mission supervisor: swept %d, nudged %d, escalated %d",
                    report["swept"],
                    report["nudged"],
                    report["escalated"],
                )
    finally:
        _wake = None
        _wake_loop = None


__all__ = [
    "EARLY_READING_ATTEMPTS",
    "EARLY_READING_DELAY_S",
    "INTERVAL_S",
    "MISSIONS_PER_SWEEP",
    "request_early_pass",
    "run",
    "run_due_early",
    "sweep",
]
