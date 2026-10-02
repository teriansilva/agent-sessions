"""The periodic supervisor sweep (#885, Phase 5a of #840).

`mission_supervisor.run_pass` is the whole decision; this only decides WHEN to run it and over
which missions. It shares the reaper pattern and actuator with the session assistant, while
its gates read only mission orchestration policy (#1019).

Gating — all must hold before a mission is swept:

* **Env kill-switch** ``AGENT_SESSIONS_MISSION_SUPERVISOR=0`` — the task exits at startup and
  never sweeps, whatever prefs say.
* **Mission orchestration switches**, re-read every sweep: `enabled`, and `autonomy != "off"`.
  Session-assistance settings do not authorize or stop a mission sweep.
* **Single-flight** — one supervisor sweep at a time, so a slow mission cannot overlap the next
  sweep and double-charge a budget.

Failures are swallowed per mission and logged: one stuck mission must not stop the fleet, which
is the whole complaint this feature exists to answer.

Cadence (#1214) — three clocks, one single-flight:

* **Fast sweep**, every `FAST_INTERVAL_S` (30 s), over `running` missions on their own keyset ring.
* **Slow sweep**, every `INTERVAL_S` (300 s), over `review` missions on a separate ring, plus the
  delivered-nudge reconciliation — neither needs to be faster, and a separate ring keeps a large
  running fleet from starving the review one.
* **Prompt watch**, every `WATCH_INTERVAL_S` (5 s): a held session of a running mission that has
  COME TO REST (`mission_supervisor.session_rest`) wakes its mission's pass once per rest episode,
  so a permission prompt is read within seconds rather than at the next sweep. The watch decides
  nothing — the woken pass is the ordinary `run_pass`, with every gate it always had.

Looking often is cheap; the model is not. Every pass goes through ONE `mission_pace.Pace`, whose
bounds are per unit of wall-clock time (reads, model calls, asks, probes), and the judge budgets
are per `JUDGE_WINDOW_S` window rather than per sweep — so ten times the passes is not ten times
the spend, and an idle mission costs no model call at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import aitasks, mission_judge, mission_pace, mission_supervisor, missions, prefs

log = logging.getLogger(__name__)

#: How often the SLOW sweep runs — `review` missions and the delivered-nudge reconciliation. It was
#: the cadence of every mission until #1214.
INTERVAL_S = 300.0
#: How often RUNNING missions are swept (#1214). Cheap because the spend is not per pass: a session
#: is read at most once per `mission_pace.READ_INTERVAL_S` unless it came to rest, and the model is
#: called only when its input moved, within a per-window cap (`mission_pace`).
FAST_INTERVAL_S = 30.0
#: How often the prompt watch looks for a held session that has come to rest (#1214). Local reads
#: only: a clock per session, a cached screen class while quiet 3–20 s, else a store mark.
WATCH_INTERVAL_S = 5.0
#: The window the judge budgets refill on (#1214). Was "per sweep", when a sweep was five minutes.
JUDGE_WINDOW_S = 300.0
#: The least a sweep waits after the previous one finished, when it overran its interval — a sweep
#: is scheduled from the START of the last one, so an overrun is followed at once, but never
#: back-to-back without the watch and the early readings getting a turn.
MIN_SWEEP_GAP_S = 2.0

#: The states each ring sweeps, and the durable cursor it keeps.
FAST_STATES = ("running",)
SLOW_STATES = ("review",)

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
#: The judge budget early readings share (#1088 review): every early reading in one judge window
#: draws on this one, replaced by a full one when the next window opens (`refresh_judge_window`;
#: per SWEEP until #1214, when a sweep was five minutes). It is a SEPARATE budget from the sweeps'
#: own, so the true bound is: at most `JUDGE_CALLS_PER_SWEEP` (6) judge calls per `JUDGE_WINDOW_S`
#: for all sweeps, plus at most 6 for all early readings in the same window, and never more than
#: `JUDGE_CALLS_PER_MISSION` (2) per mission per pass. Kept separate on purpose: an early reading is
#: the operator's newest mission, and the sweeps that follow should not find their budget spent.
_early_budget: mission_judge.Budget = mission_judge.Budget()
#: The missions the loop's sweeps supervised since the last judge phase (#1214 review). The loop
#: JUDGES ONCE PER WINDOW, over this whole set — not per sweep over one 25-mission page. A budget
#: carried across the pages of a ring would be spent by whichever page the window opened on, and
#: `judge_batch`'s least-recently-served order only rotates within the ids it is given: with two
#: pages, the second never saw a call. Given every mission supervised in the window, the order is
#: global again, and the spend is still `JUDGE_CALLS_PER_SWEEP` per `JUDGE_WINDOW_S`.
_judge_due: set[str] = set()
#: When the sweeps' current judge window opened, on the monotonic clock; `None` = no window yet.
_window_at: float | None = None
#: When the early readings' current budget window opened — a separate clock (#1214 review).
_early_window_at: float | None = None
#: The one spend gate every pass the loop makes goes through (#1214). Module-level so an early
#: reading, a woken pass and a sweep share one set of clocks.
_pace: mission_pace.Pace = mission_pace.Pace()
#: session key -> the rest episode the watch last woke its mission for (#1214).
_watch_seen: dict[str, str] = {}
#: When the prompt watch is next due, on the monotonic clock (#1214). Module-level because a sweep
#: services it BETWEEN its passes too: a sweep holds the single-flight for as long as its batch
#: takes, and a watch that waited for the whole batch would miss its deadline by exactly that.
_next_watch: float = 0.0
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


def refresh_judge_window(now: float | None = None) -> bool:
    """The SWEEPS' judge window (#1214): opens a new one once `JUDGE_WINDOW_S` has passed since the
    last one opened, and returns True when it did — that sweep runs the window's judge phase (see
    `_judge_due`). Only a paced sweep calls this: the early readings keep their own clock
    (`refresh_early_budget`), so an early reading at a window boundary can never take the sweeps'
    judge phase for itself (#1214 review)."""
    global _window_at
    t = time.monotonic() if now is None else now
    if _window_at is not None and t - _window_at < JUDGE_WINDOW_S:
        return False
    _window_at = t
    return True


def refresh_early_budget(now: float | None = None) -> None:
    """The EARLY readings' budget refills once per `JUDGE_WINDOW_S` on its own clock (#1214):
    within a window it carries over however many early readings and sweeps run."""
    global _early_budget, _early_window_at
    t = time.monotonic() if now is None else now
    if _early_window_at is not None and t - _early_window_at < JUDGE_WINDOW_S:
        return
    _early_window_at = t
    _early_budget = mission_judge.Budget()


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
    refresh_early_budget(t)
    report: dict[str, str] = {}
    return await _run_due(t, report, registry)


async def _run_due(t: float, report: dict[str, str], registry) -> dict:
    for mid in due_early(t):
        _due, made = _early.pop(mid)
        res: dict | None = None
        try:
            res = await mission_supervisor.run_pass(mid, registry=registry, pace=_pace)
        except Exception as e:  # noqa: BLE001 — one mission's early read must not stop the loop
            log.warning("mission %s: early supervisor reading failed: %s", mid, e)
        # …then the judge phase for this one mission, on the early readings' shared budget. Its
        # verdict is counted by the NEXT pass, after revalidation, exactly as in a sweep.
        # Re-checked per mission, like the sweep's judge phase: an operator who switched
        # supervision off while this batch was running gets no further model calls from it.
        if _enabled():
            with contextlib.suppress(Exception):
                await mission_judge.judge_batch([mid], _early_budget)
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
#: The slow ring's own cursor (#1214) — `review` missions, swept every `INTERVAL_S`.
SLOW_CURSOR_KEY = "sweep_cursor:review"


def _enabled() -> bool:
    if os.getenv("AGENT_SESSIONS_MISSION_SUPERVISOR", "1") == "0":
        return False
    cfg = prefs.get_mission_orchestration()
    return bool(cfg.get("enabled")) and str(cfg.get("autonomy") or "off") != "off"


async def sweep(
    registry=None,
    *,
    states: tuple[str, ...] = ELIGIBLE_STATES,
    cursor_key: str = _CURSOR_KEY,
    reconcile: bool = True,
    paced: bool = False,
    now: float | None = None,
) -> dict:
    """One sweep over one ring of eligible missions. Returns a small report for the caller/tests.

    The loop runs two rings (#1214): `FAST_STATES` every `FAST_INTERVAL_S` on `_CURSOR_KEY`, and
    `SLOW_STATES` every `INTERVAL_S` on `SLOW_CURSOR_KEY`, which also reconciles. The loop's sweeps
    are `paced`: every pass goes through the shared `_pace`, the judge runs once per window over
    every mission supervised in it (`_judge_due`), and a mission with a pending early reading is
    left to it. The defaults are the
    single unpaced ring this was before, for direct callers: a fresh judge budget per call.
    """
    # BEFORE the enabled gate: a delivery made while the orchestrator was on still owes its thread
    # record after it is switched off, and this only ever writes that record (#983 review).
    if reconcile:
        await _reconcile_delivered()
    if not _enabled():
        return {"skipped": "disabled"}
    await _forget_legacy_state()
    if paced:
        # The judge runs once per WINDOW, not per sweep (#1214; per sweep since #1088 review).
        judge_now = refresh_judge_window(now)
        refresh_early_budget(now)
    else:
        # A direct call is one sweep window of its own, as before #1214.
        global _early_budget
        _early_budget = mission_judge.Budget()
        judge_now = True

    # A KEYSET CURSOR over the complete eligible set — not a rebuilt prefix of a list page.
    #
    # Two earlier spellings were both fair over the wrong set. Taking the first page of a
    # newest-first list never reached the tail; paging that list to a `WORKLIST_MAX` ceiling moved
    # the boundary without removing it, and logging the truncation does not supervise anything past
    # it. `missions.supervisor_worklist` walks `id > cursor` in id order, so there is no ceiling and
    # no prefix: the cursor advances through every eligible mission and wraps, which is what makes
    # "every eligible mission is eventually visited" true rather than aspirational.
    try:
        cursor = await missions.run_admitted(lambda: missions.get_supervisor_state(cursor_key))
        batch = await missions.run_admitted(
            lambda: missions.supervisor_worklist(
                states=states, after=cursor, limit=MISSIONS_PER_SWEEP
            )
        )
        if not batch and cursor is not None:
            # End of the ring — wrap, so a sweep never does nothing merely because the cursor sits
            # past the last id.
            batch = await missions.run_admitted(
                lambda: missions.supervisor_worklist(
                    states=states, after=None, limit=MISSIONS_PER_SWEEP
                )
            )
    except Exception:  # noqa: BLE001 — a store hiccup must not kill the loop
        log.debug("mission supervisor: worklist unavailable", exc_info=True)
        return {"skipped": "worklist unavailable"}

    if not batch:
        with contextlib.suppress(Exception):
            await missions.run_admitted(lambda: missions.set_supervisor_state(cursor_key, None))
        out = {"swept": 0, "nudged": 0, "escalated": 0}
        if paced and judge_now and _judge_due:
            # The window's judgments are still owed for what earlier sweeps supervised.
            await _judge_phase(_take_judge_due([]), out, mission_judge.Budget())
        return out

    out = {"swept": 0, "nudged": 0, "escalated": 0}

    # ONE judge budget for the whole sweep (#1088): at most `JUDGE_CALLS_PER_SWEEP` model calls
    # across every mission it visits, on top of the per-mission cap inside the pass.
    #
    # TWO PHASES (#1097 round 9). Phase 1 supervises every mission in cursor order — stale marks,
    # assessment, completion, nudges — and makes no judge call. Phase 2 then judges the batch's
    # running missions least recently served first, charging the sweep's budget only for calls
    # actually made: nothing is held for a mission not yet visited, so no number of missions with
    # nothing to read can keep one with work waiting.
    #
    # Phase 2 runs after phase 1 finished or failed with an ordinary error — never after a
    # CANCELLATION (shutdown: `CancelledError` is not an `Exception`, so it propagates without a
    # single judge call), and never once the orchestrator was switched off mid-sweep (#1097 round
    # 10): turning supervision off stops the model calls, not just the next sweep.
    # WHICH missions phase 2 judges: a direct sweep judges its own batch; the loop judges once per
    # window over every mission its sweeps supervised since the last judge phase (`_judge_due`).
    def _judge_ids() -> list[str] | None:
        if not paced:
            return batch
        if not judge_now:
            _judge_due.update(batch)
            return None
        return _take_judge_due(batch)

    try:
        result = await _sweep_batch(batch, out, registry, cursor_key=cursor_key, paced=paced)
    except Exception:
        ids = _judge_ids()
        if ids is not None:
            await _judge_phase(ids, out, mission_judge.Budget())
        raise
    ids = _judge_ids()
    if ids is None:
        out["judge_skipped"] = "not this window"
    else:
        await _judge_phase(ids, out, mission_judge.Budget())
    return result


def _take_judge_due(batch: list[str]) -> list[str]:
    """Everything supervised since the last judge phase, plus `batch`, in id order — and reset."""
    ids = sorted(_judge_due | set(batch))
    _judge_due.clear()
    return ids


async def _judge_phase(batch: list[str], out: dict, budget: mission_judge.Budget) -> None:
    """Phase 2 of a sweep, if supervision is still on. Best-effort: never raises an `Exception`."""
    with contextlib.suppress(Exception):
        if not _enabled():
            out["judge_skipped"] = "disabled"
            return
        judged = await mission_judge.judge_batch(batch, budget)
        out["judge_calls"] = sum(int(r.get("calls") or 0) for r in judged.values())


async def _sweep_batch(
    batch: list[str], out: dict, registry, *, cursor_key: str = _CURSOR_KEY, paced: bool = False
) -> dict:
    """Run one pass per mission in `batch`, advancing the durable cursor after every attempt."""
    for mid in batch:
        failed = False
        # A mission whose EARLY reading is still pending is left to it (#1214): the early reading
        # waits for the agent to have done something (#1064), and a 30 s sweep reading the empty
        # screen right after dispatch would spend the call the early reading exists to place well.
        if paced and mid in _early:
            out["deferred_to_early"] = out.get("deferred_to_early", 0) + 1
            res = None
        else:
            try:
                if paced:
                    res = await mission_supervisor.run_pass(mid, registry=registry, pace=_pace)
                else:
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
            await missions.run_admitted(lambda m=mid: missions.set_supervisor_state(cursor_key, m))
        except Exception as e:  # noqa: BLE001
            # A cursor that will not advance is not a slow sweep, it is a STUCK one: every
            # subsequent sweep re-selects the same low-id batch and the tail is never visited,
            # while the report still counts missions as swept. Suppressing that reported fair
            # progress the store had not actually made (#888 review, finding 7). Stop the sweep
            # and say so, rather than looping on a prefix.
            log.error("mission supervisor: cursor did not advance past %s: %s", mid, e)
            out["cursor_error"] = str(e)
            return out
        # BETWEEN WORK UNITS (#1214): a resting session found while this batch runs is served after
        # the pass in flight, not after the whole batch. The flight is already ours. Only inside the
        # running loop (`_wake` set): a direct `sweep()` call is one sweep and nothing else.
        if paced and _wake is not None:
            with contextlib.suppress(Exception):
                woke = await service_watch(registry)
                if woke:
                    out["woken"] = out.get("woken", 0) + len(woke)
        if failed or res is None:
            continue
        out["swept"] += 1
        if (res.get("nudged") or {}).get("sent"):
            out["nudged"] += 1
        if res.get("escalated"):
            out["escalated"] += 1
    return out


#: Supervisor-state keys an earlier build wrote and nothing reads any more (#1097 round 8): the
#: durable starved-judgment queue, replaced by the stateless plan.
_LEGACY_KEYS = ("judge_starved",)


_legacy_forgotten = False


async def _forget_legacy_state() -> None:
    """Delete supervisor-state keys nothing reads. Once per process, from the first sweep, so boot
    does not wait on the store; best-effort."""
    global _legacy_forgotten
    if _legacy_forgotten:
        return
    _legacy_forgotten = True
    for key in _LEGACY_KEYS:
        with contextlib.suppress(Exception):
            await missions.run_admitted(lambda k=key: missions.set_supervisor_state(k, None))


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


def _running_held_sessions() -> dict[str, str]:
    """``session_key -> mission_id`` for every session a RUNNING mission holds. Blocking (store)."""
    rows = missions.active_membership_rows()
    return {sk: str(r["id"]) for sk, r in rows.items() if str(r.get("state") or "") == "running"}


async def watch(*, pace: mission_pace.Pace | None = None) -> list[str]:
    """One prompt-watch tick (#1214): the missions to wake, each at most once, in a stable order.

    A mission is woken when one of its held sessions has come to rest on an episode the watch has
    not woken it for yet. Coalesced: one wake per mission however many of its sessions rest, and the
    same episode never wakes twice — so a prompt that sits there for an hour is one reading. New
    output ends the episode, and the next rest is a new one. Local reads only, off the loop; the
    watch decides nothing — `run_woken` runs the ordinary pass.
    """
    if not _enabled():
        _watch_seen.clear()
        return []
    p = _pace if pace is None else pace
    try:
        held = await missions.run_admitted(_running_held_sessions)
    except Exception:  # noqa: BLE001 — a busy store is retried on the next tick
        log.debug("mission supervisor: watch could not read the held sessions", exc_info=True)
        return []
    # Forget sessions no running mission holds any more, so the map stays the live set.
    for sk in [k for k in _watch_seen if k not in held]:
        _watch_seen.pop(sk, None)
    woken: list[str] = []
    for sk in sorted(held):
        mid = held[sk]
        try:
            episode = await asyncio.to_thread(mission_supervisor.session_rest, sk, p)
        except Exception:  # noqa: BLE001 — an unreadable session is simply not at rest
            log.debug("mission supervisor: watch could not read %s", sk, exc_info=True)
            continue
        if episode is None or _watch_seen.get(sk) == episode:
            continue
        _watch_seen[sk] = episode
        if mid not in woken:
            woken.append(mid)
    return woken


async def run_woken(mission_ids: list[str], registry=None) -> dict:
    """Pass each woken mission now, through the same pace every pass shares. No judge call: the
    judge is the sweep's second phase, and a woken pass is about a session that stopped, not about
    a gate. Returns ``{mission_id: "passed" | "failed"}``."""
    report: dict[str, str] = {}
    for mid in mission_ids:
        if not _enabled():
            break
        try:
            await mission_supervisor.run_pass(mid, registry=registry, pace=_pace)
            report[mid] = "passed"
        except Exception as e:  # noqa: BLE001 — one mission must not stop the others
            log.warning("mission %s: woken supervisor pass failed: %s", mid, e)
            report[mid] = "failed"
    return report


async def service_watch(registry=None, *, now: float | None = None) -> dict:
    """Run the prompt watch if it is due, and pass the missions it wakes. The caller holds the
    supervisor's single-flight. Returns ``{mission_id: "passed" | "failed"}`` (empty if not due)."""
    global _next_watch
    t = time.monotonic() if now is None else now
    if t < _next_watch:
        return {}
    _next_watch = t + WATCH_INTERVAL_S
    woken = await watch()
    if not woken:
        return {}
    report = await run_woken(woken, registry)
    log.info("mission supervisor: woken by a resting session %s", report)
    return report


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
    start = time.monotonic()
    # Two rings and a watch (#1214), each with its own clock and its own failure backoff.
    rings = {
        "fast": {
            "states": FAST_STATES,
            "cursor": _CURSOR_KEY,
            "interval": FAST_INTERVAL_S,
            "reconcile": False,
            "next": start + FAST_INTERVAL_S,
            "failures": 0,
        },
        "slow": {
            "states": SLOW_STATES,
            "cursor": SLOW_CURSOR_KEY,
            "interval": INTERVAL_S,
            "reconcile": True,
            "next": start + INTERVAL_S,
            "failures": 0,
        },
    }
    global _next_watch
    _next_watch = start + WATCH_INTERVAL_S
    try:
        while True:
            # WAKEABLE, not a flat sleep (#1064): whichever clock falls due first, or a request.
            # Cleared BEFORE the target is computed, so a request landing in between still wakes it.
            _wake.clear()
            targets = [r["next"] for r in rings.values()] + [_next_watch]
            pending = next_early_due()
            if pending is not None:
                targets.append(pending)
            target = min(targets)
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

            if now >= _next_watch:
                try:
                    async with aitasks.single_flight("mission-supervisor", "sweep"):
                        await service_watch(registry, now=now)
                except aitasks.AlreadyRunning:
                    # The next tick looks again, and the episode is not marked seen — but WAIT:
                    # left due, the next iteration's timeout would be zero — a spin.
                    _next_watch = now + WATCH_INTERVAL_S
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("mission supervisor prompt watch crashed")

            for name, ring in rings.items():
                if time.monotonic() < ring["next"]:
                    continue
                began = time.monotonic()
                try:
                    async with aitasks.single_flight("mission-supervisor", "sweep"):
                        report = await sweep(
                            registry,
                            states=ring["states"],
                            cursor_key=ring["cursor"],
                            reconcile=ring["reconcile"],
                            paced=True,
                        )
                except aitasks.AlreadyRunning:
                    ring["next"] = time.monotonic() + ring["interval"] * min(
                        2 ** ring["failures"], 8
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    ring["failures"] += 1
                    log.exception("mission supervisor %s sweep crashed", name)
                    ring["next"] = time.monotonic() + ring["interval"] * min(
                        2 ** ring["failures"], 8
                    )
                    continue
                ring["failures"] = 0
                # From the START of this sweep, so the cadence is the interval and not interval
                # plus however long the sweep took — but never back-to-back (#1214).
                ring["next"] = max(began + ring["interval"], time.monotonic() + MIN_SWEEP_GAP_S)
                if report.get("swept"):
                    log.info(
                        "mission supervisor (%s): swept %d, nudged %d, escalated %d",
                        name,
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
    "FAST_INTERVAL_S",
    "INTERVAL_S",
    "JUDGE_WINDOW_S",
    "WATCH_INTERVAL_S",
    "MISSIONS_PER_SWEEP",
    "refresh_judge_window",
    "request_early_pass",
    "run",
    "run_due_early",
    "run_woken",
    "sweep",
    "watch",
]
