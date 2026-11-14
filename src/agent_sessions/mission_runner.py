"""HQ starts the approved plan's steps: the runner under an approval in force (#1951 slice B).

#1613 D3, recorded by the operator: approving the enumerated plan is the tap. Under an approval in
force, each `ready`, assigned agent step is started through #1581's ONE seam,
`mission_steps.start_step(…, authority=Authority("approval"), admit=mission_approval.admit)`,
which re-checks every pin before the claim, binds the approval into the claim and re-reads it
inside the write fence at the create and at the brief. This module adds no second admission path
and no fence of its own: it only decides WHICH step to offer the seam, and when.

* **What it offers** (D-P6-7): the agent steps the board says can start now (`view_sync`'s
  `can_start`: ready, no open run, the cap, a free folder, orchestration on, no pause, stop or
  budget hold), minus the steps the operator holds (D-P6-11, "ask me before this step starts").
  The seam re-checks each of those facts itself.
* **An approved alternate** (D-P6-12, D6) is offered only when the pick fails qualification on
  its client or its usage (`mission_steps.SWITCH_CODES`), in the approved order, each admitted on
  its own pin. A full pool is a reason to wait.
* **A refusal** is the seam's: `admit` records it once per (approval, step, change) (D-P6-8). A
  refusal nobody tapped for raises ONE bell row and push per refusal record (D-P6-9), retracted
  when the approval it was recorded under is no longer in force, or the step starts.
* **A refused step is offered again no sooner than `RETRY_S`**, doubling per consecutive
  refusal: up to `ADMISSION_RETRY_MAX_S` when nothing was claimed (a remap back launches within
  minutes), up to `RETRY_MAX_S` for a launch refused at its effects. A different change than the
  last one, or a new approval, starts the back-off again.
* **Each step once**: a step that has run, whoever started it, is never offered again (the
  claim's `already_ran` enforces it at the seam).

Called from the supervisor's fast ring (`mission_supervisor_loop.sweep`), which runs only while
orchestration is on and autonomy is not `off`. Each mission's launches run as one background task,
so a slow create never holds the supervisor's sweep. Kill switch:
`AGENT_SESSIONS_MISSION_RUNNER=0`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import mission_approval, mission_steps, missions

log = logging.getLogger(__name__)

APPROVAL = mission_steps.Authority("approval")
#: The least time between two offers of a step the seam refused.
RETRY_S = 60.0
#: Refusal codes that only mean "not now" (a launch in flight, the cap, a busy folder): offered
#: again at the next sweep rather than after `RETRY_S`.
_TRANSIENT = frozenset({"launch_in_flight", "cap", "shared_cwd", "not_ready", "open_run"})
_inflight: dict[str, asyncio.Task] = {}
_retry_at: dict[tuple[str, str, str], float] = {}
#: Consecutive refused offers per step: the back-off doubles with each, up to `RETRY_MAX_S`, so a
#: launch that keeps failing at its effects is not retried every minute (independent review).
_failures: dict[tuple[str, str, str], int] = {}
#: The longest back-off between two offers of a step that keeps being refused at its effects.
RETRY_MAX_S = 3600.0
#: The longest back-off when the refusals claimed nothing (the admission, the preflight or the
#: claim refused): each costs one re-check, and a binding remapped back should launch within
#: minutes, not an hour (independent review of #2019).
ADMISSION_RETRY_MAX_S = 300.0
#: What the last refusal of a step was (its change digest, or its code): a refusal for a different
#: one starts the back-off again.
_changes: dict[tuple[str, str, str], str | None] = {}
#: Set by `drain`: no new pass starts once shutdown began.
_closing = False


def enabled() -> bool:
    return os.getenv("AGENT_SESSIONS_MISSION_RUNNER", "1") != "0"


def runs() -> bool:
    """Whether this host lets the runner start anything at all: its own kill switch and the
    supervisor's (`mission_supervisor_loop._enabled`'s env half; the runner rides its fast ring).
    Orchestration and autonomy are the board's own `switched_off`. Read by the board's line."""
    return enabled() and os.getenv("AGENT_SESSIONS_MISSION_SUPERVISOR", "1") != "0"


def notice_key(approval_id: str, change: str) -> str:
    """The bell row's action id for one refusal record: the approval and the change digest, so a
    retry against the same change raises nothing new. No model text."""
    return f"mission-refusal:{approval_id}:{change.removeprefix('sha256:')[:24]}"


def offers_sync(mission_id: str) -> dict:
    """The steps the runner may offer the seam now, in plan order, with their alternates count.
    Blocking (the board's own read). Empty unless an approval is in force."""
    approval = missions.approval(mission_id)
    if approval is None or approval.get("revoked_at") is not None:
        return {"approval_id": None, "revision": None, "steps": []}
    view = mission_steps.view_sync(mission_id)
    held = missions.step_holds(mission_id)
    steps = [
        {"id": s["id"], "alternates": len((s.get("assignment") or {}).get("alternates") or [])}
        for s in view["steps"]
        if s["actor"] == "agent"
        and s["can_start"]
        and s["id"] not in held
        and not s.get("runner_done")
    ]
    return {
        "approval_id": approval["id"],
        "title": str(view.get("title") or ""),
        "revision": view["revision"],
        "steps": steps,
    }


def _notify(mission_id: str, title: str, step_id: str, extra: dict) -> None:
    """One bell row and push for a refusal nobody tapped for (D-P6-9). Best-effort: the refusal
    is already recorded, and the board shows it."""
    from . import notifications

    approval_id, change = extra.get("approval_id"), extra.get("change")
    if not (extra.get("new") and approval_id and change):
        return
    try:
        rec = notifications.add(
            title=f"Mission step not started: {title}"[:200],
            project="",
            session_id="",
            engine="",
            reason=f"{step_id}: {mission_approval.class_text(extra.get('reasons') or [])}",
            action_id=notice_key(approval_id, change),
        )
        if rec and notifications.list_subscriptions():
            with contextlib.suppress(Exception):
                notifications.fanout(rec)
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("mission %s: refusal notification failed", mission_id, exc_info=True)


def _has_run(mission_id: str, step_id: str) -> bool:
    return any(
        r["step_id"] == step_id and r["state"] != "refused" for r in missions.step_runs(mission_id)
    )


def _in_force(mission_id: str, approval_id: str | None) -> bool:
    live = missions.approval(mission_id)
    return bool(
        approval_id
        and live is not None
        and live.get("revoked_at") is None
        and live["id"] == approval_id
    )


def _notify_unless_started(mission_id: str, title: str, step_id: str, extra: dict) -> None:
    """`_notify`, unless nothing is left to answer: the step started meanwhile (the operator's
    Start between this pass's read and its refusal), or the approval the refusal was recorded
    under is no longer in force (revoked or superseded meanwhile). Both are checked again AFTER
    the row is raised, and it is retracted then, because a Start, a revoke or a re-approval
    retracts only what was there when it finished (independent review of #2019, Hermes 8155)."""
    approval_id = extra.get("approval_id")
    try:
        if _has_run(mission_id, step_id) or not _in_force(mission_id, approval_id):
            return
    except Exception:  # noqa: BLE001 — unknown is not "answered": notify (more, never less)
        log.debug("mission %s: state unreadable before a notice", mission_id, exc_info=True)
    _notify(mission_id, title, step_id, extra)
    try:
        if _has_run(mission_id, step_id):
            retract_sync(mission_id, step_id=step_id)
        elif not _in_force(mission_id, approval_id):
            retract_sync(mission_id)  # every notice of an approval no longer in force
    except Exception:  # noqa: BLE001
        log.debug("mission %s: state unreadable after a notice", mission_id, exc_info=True)


def retract_sync(mission_id: str, *, step_id: str | None = None) -> int:
    """Retract the refusal notifications that no longer stand: every one recorded under an
    approval that is not the one in force, and (with `step_id`) that step's under the one in force,
    once it started. Blocking; best-effort. Returns how many rows went."""
    from . import notifications

    try:
        live = missions.approval(mission_id)
        live_id = live["id"] if live is not None and live.get("revoked_at") is None else None
        n = 0
        for r in missions.refusals(mission_id):
            if r["approval_id"] == live_id and r["step"] != step_id:
                continue
            n += notifications.dismiss_for_action(notice_key(r["approval_id"], r["change_digest"]))
        return n
    except Exception:  # noqa: BLE001
        log.debug("mission %s: refusal notifications not retracted", mission_id, exc_info=True)
        return 0


async def _start(mission_id: str, step_id: str, revision: int, use, registry) -> dict:
    return await mission_steps.start_step(
        mission_id,
        step_id,
        revision=revision,
        authority=APPROVAL,
        admit=mission_approval.admit,
        use=use,
        registry=registry,
    )


def _switchable(reasons: list[dict]) -> str | None:
    """When EVERY reason a pick was refused for says its client can't run it (out of quota, or not
    mission-ready), the text of the first: an approved alternate may launch instead (D6). Any
    other reason is the plan's to fix, and no alternate replaces it."""
    if not reasons:
        return None
    for r in reasons:
        cause = r.get("cause") or r.get("code")
        if cause not in mission_approval.SWITCH_CAUSES:
            return None
    return str(reasons[0].get("text") or "")


async def _offer(
    mission_id: str, title: str, step: dict, revision: int, registry
) -> tuple[str, str | None]:
    """Offer one step to the seam: the pick, then (only when its client can't run it) each
    approved alternate in order, each admitted on its own pin. Returns `(outcome, what)`: the
    outcome is `started`, `refused` or `wait`; `what` is set only for a refusal that claimed
    nothing (the change digest of the first admission refusal, else the last code), and is None
    for a launch refused or failed at its effects. A refusal is notified once, and only when
    nothing started: an alternate that ran in the pick's place leaves nothing to answer."""
    sid = step["id"]
    uses = [mission_steps.PRIMARY]
    last: missions.StepRefused | None = None
    first_admission: dict | None = None
    i = 0
    while i < len(uses):
        use = uses[i]
        i += 1
        try:
            out = await _start(mission_id, sid, revision, use, registry)
        except missions.StepRefused as e:
            last = e
            if e.code == "admission_refused" and first_admission is None:
                first_admission = e.extra
            if use.kind == "primary" and step["alternates"]:
                why = (
                    _switchable(e.extra.get("reasons") or [])
                    if e.code in ("admission_refused", "not_qualified")
                    else None
                )
                if why is not None:
                    uses += [mission_steps.Use.alternate(n, why) for n in range(step["alternates"])]
            continue
        except missions.MissionError as e:
            log.info("mission %s: step %s not offered: %s", mission_id, sid, e)
            return "wait", None
        if out.get("outcome") == "started":
            await asyncio.to_thread(retract_sync, mission_id, step_id=sid)
            return "started", None
        return "refused", None  # refused or failed at its effects: the run says so
    if first_admission is not None:
        # Off the loop: the push fanout blocks on each device (independent review of #2019).
        await asyncio.to_thread(_notify_unless_started, mission_id, title, sid, first_admission)
    if last is not None and last.code in _TRANSIENT:
        return "wait", None
    what = (first_admission or {}).get("change") or (last.code if last is not None else "")
    return "refused", str(what)


async def advance(mission_id: str, registry=None, *, now: float | None = None) -> dict:
    """Offer every step the runner may start now, in plan order. Returns `{step: outcome}`.
    Never raises an `Exception` (the supervisor calls it)."""
    out: dict[str, str] = {}
    try:
        offers = await missions.run_admitted(lambda: offers_sync(mission_id))
    except Exception:  # noqa: BLE001 — retried by the next sweep
        log.debug("mission %s: runner could not read", mission_id, exc_info=True)
        return out
    if offers["approval_id"] is None:
        return out
    await asyncio.to_thread(retract_sync, mission_id)
    t = time.monotonic() if now is None else now
    for step in offers["steps"]:
        if _closing:  # shutdown began during this pass: offer nothing more
            break
        # Keyed by the approval too: a new approval (or a revoked one) starts a fresh back-off.
        key = (mission_id, step["id"], offers["approval_id"])
        if _retry_at.get(key, 0.0) > t:
            out[step["id"]] = "later"
            continue
        try:
            got, what = await _offer(
                mission_id, offers["title"], step, offers["revision"], registry
            )
        except Exception:  # noqa: BLE001 — one step must not stop the others
            log.warning("mission %s: step %s offer failed", mission_id, step["id"], exc_info=True)
            # Nothing was refused (a store error, say): the short back-off, never the hour.
            got, what = "refused", "error"
        out[step["id"]] = got
        if got == "refused":
            if key in _changes and _changes[key] != what:
                _failures.pop(key, None)  # a different change than the last: start again
            _changes[key] = what
            n = _failures.get(key, 0) + 1
            _failures[key] = n
            cap = RETRY_MAX_S if what is None else ADMISSION_RETRY_MAX_S
            _retry_at[key] = t + min(RETRY_S * 2 ** (n - 1), cap)
        else:
            _retry_at.pop(key, None)
            if got == "started":
                _failures.pop(key, None)
                _changes.pop(key, None)
    return out


async def sweep(registry=None) -> int:
    """Start one background `advance` per mission under an approval in force that has none
    running. Returns how many were started. Never raises an `Exception`."""
    if not enabled() or _closing:
        return 0
    try:
        mids = await missions.run_admitted(missions.approved_missions)
    except Exception:  # noqa: BLE001 — retried by the next sweep
        log.debug("mission runner: worklist unavailable", exc_info=True)
        return 0
    n = 0
    for mid in mids:
        if _closing:  # shutdown began while the worklist was read
            break
        task = _inflight.get(mid)
        if task is not None and not task.done():
            continue
        task = asyncio.ensure_future(advance(mid, registry))
        _inflight[mid] = task
        task.add_done_callback(
            lambda t, m=mid: _inflight.pop(m, None) if _inflight.get(m) is t else None
        )
        n += 1
    return n


#: How long shutdown waits for the runner's launches in flight to settle (the seam joins each
#: claim to its launch; one cut short is settled by startup recovery).
SHUTDOWN_WAIT_S = 20.0


async def drain(timeout: float = SHUTDOWN_WAIT_S) -> None:
    """Let the runner's `advance` tasks settle before the app goes away; cancel what is still
    running (the seam joins a claim to its launch even then). No new pass starts after it."""
    global _closing
    _closing = True
    pending = {t for t in _inflight.values() if not t.done()}
    if not pending:
        return
    log.info("waiting for %d mission runner pass(es) to settle", len(pending))
    _done, late = await asyncio.wait(pending, timeout=timeout)
    for t in late:
        t.cancel()
    if late:
        await asyncio.wait(late, timeout=5)


def reset_for_test() -> None:
    global _closing
    _closing = False
    _inflight.clear()
    _retry_at.clear()
    _failures.clear()
    _changes.clear()


__all__ = [
    "APPROVAL",
    "RETRY_S",
    "advance",
    "drain",
    "enabled",
    "notice_key",
    "offers_sync",
    "retract_sync",
    "runs",
    "sweep",
]
