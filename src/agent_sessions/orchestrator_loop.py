"""Periodic Pulse orchestrator loop (#726 Phase 1).

A reaper-pattern background task mirroring ``pulse_loop`` / ``ai_review_loop`` /
``autosort_loop``: every ``interval_minutes`` it runs one orchestrator pass — but only when
every gate agrees, so a machine full of idle sessions costs nothing.

Gating — all must hold before a single endpoint call happens:

* **Env kill-switch** ``AGENT_SESSIONS_ORCHESTRATOR_LOOP=0`` — the task exits at startup and
  never sweeps, regardless of prefs. The operator-level "stop it NOW" that doesn't require
  touching saved settings.
* **Prefs** ``session_assistance.enabled``, re-read on EVERY sweep so its toggle applies at
  the next wake without a restart. ``autonomy: off`` still runs the pass — observing and
  proposing is the *point* of that tier; it simply never queues anything for delivery.
* **Single-flight** — ``aitasks`` kind ``orchestrator``, so a manual "Run now" and the loop can
  never overlap.
* **Change detection** — the eligible session set's fingerprint is recomputed (cheap, FS only)
  and compared against the last pass's. An unchanged world is a no-op: no call, no cost. This
  is what keeps a 10-minute interval from being a 10-minute billing cycle.

Expiry and startup recovery ride here too: overdue proposals are expired every sweep, and a
``claimed`` action left by a crash is moved to ``indeterminate`` once at startup — never
auto-retried, because nothing on disk can prove whether its bytes landed.

Failures are swallowed (logged) with an interval backoff, so a flaky endpoint never crashes
the loop and is probed ever more gently.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time

from . import actuator, aitasks, mission_fence, orchestrator, prefs, review
from . import orchestrator_ledger as ledger

log = logging.getLogger("agent_sessions.orchestrator_loop")

_BACKOFF_MAX_MULT = 8

# Last pass's input fingerprint, so an unchanged world skips the endpoint entirely.
_last_fingerprint: str | None = None
# Rotation cursor into the eligible set, so a >cap world is covered across passes.
_next_offset: int = 0


def loop_enabled() -> bool:
    """Env kill-switch — overrides everything, prefs included."""
    return (os.environ.get("AGENT_SESSIONS_ORCHESTRATOR_LOOP", "1") or "1") != "0"


def reset_state() -> None:
    """Drop the cached fingerprint. Test hook — the module-level cache is process-global."""
    global _last_fingerprint, _next_offset
    _last_fingerprint = None
    _next_offset = 0


def _working_keys(registry) -> set[str]:
    """Live "in flight" overlay keys. Best-effort — a registry hiccup yields no overlay,
    never a crash. ``None`` registry (tests) → no overlay."""
    if registry is None:
        return set()
    keys: set[str] = set()
    with contextlib.suppress(Exception):
        for r in registry.snapshot():
            if r.get("working") or r.get("attached"):
                keys.add(r["id"])
    return keys


def _assessment_state(card: dict) -> tuple[str, str, str]:
    """``(status, context_source, stale reasons)`` of the card's assessment, as the digest
    sees it."""
    view, source = orchestrator.assessment_view(card)
    return view["status"], source, ",".join(view["stale_reasons"])


def world_fingerprint(
    cards: list[dict],
    cfg: dict | None = None,
    now: float | None = None,
    ledger_gen: str | None = None,
    review_cfg: dict | None = None,
    prompt_facts: dict[str, list] | None = None,
) -> str:
    """sha256 over the eligible session set's decision-relevant fields, PLUS the settings that
    change what a pass would decide.

    Deliberately EXCLUDES the live/working overlay: a session merely going live or idle is
    volatile display state and must not force a fresh endpoint call. It includes the review
    fingerprint, so a re-review that changes what a session *is doing* does re-trigger a pass.

    It also includes the decision-affecting config (tier, threshold, allowed verbs, caps,
    prompt, nudge). Without that, raising the tier from `suggest` to `yolo` — or rewriting the
    prompt — would leave the fingerprint identical, and every scheduled sweep would skip as
    "unchanged" while the operator waits for their new policy to do something.
    """
    cfg = cfg if cfg is not None else prefs.get_session_assistance()
    now = time.time() if now is None else now
    ledger_gen = ledger.generation() if ledger_gen is None else ledger_gen
    # #1086 Phase 2 (review 5180): the Session review settings change what the digest carries,
    # so they are policy too — switching Standard → Deep must not read as "unchanged". And with
    # recognition ON the digest carries each screen's prompt class and menu, so those are inputs:
    # their IDENTITY, never the raw screen, which streams and would force a pass every sweep.
    review_cfg = review_cfg if review_cfg is not None else prefs.get_session_review()
    if prompt_facts is None:
        prompt_facts = _prompt_facts(cards) if review_cfg.get("recognise_prompts") else {}
    payload = json.dumps(
        {
            "sessions": sorted(
                [
                    c["id"],
                    c.get("state", ""),
                    bool(c.get("intervention_required")),
                    c.get("_review_fingerprint", ""),
                    # The digest sends `age_hours` and prefers the RECAP over the summary, so
                    # both are decision-relevant inputs. Omitting them let a recap-only or
                    # activity-only change read as "unchanged" while the next digest — and the
                    # decision it produces — would differ. Age is bucketed to the hour so a
                    # ticking clock alone doesn't force a call every sweep.
                    c.get("_recap_fingerprint", ""),
                    # AGE, not the activity timestamp. The digest sends
                    # `age_hours = (now - last_activity)/3600`, which advances while a session
                    # sits idle; bucketing `last_activity` itself is CONSTANT for an idle
                    # session, so the digest could move from 1h to 2h with an identical
                    # fingerprint and the sweep would skip as unchanged. Bucketed hourly so a
                    # ticking clock alone doesn't force a call every sweep.
                    int(max(0.0, now - float(c.get("last_activity") or now)) // 3600),
                    # The assessment's freshness (#1020 review finding 4): the digest labels a
                    # record `assessment_stale` once a refresh fails or the session moves, and a
                    # failed refresh changes no review fingerprint — without these the pass that
                    # would read the new label is skipped as "unchanged".
                    c.get("_review_failed_at") or 0,
                    *_assessment_state(c),
                ]
                for c in cards
            ),
            # ANY ledger mutation — from the sweep, the state GET's housekeeping, an operator
            # approving/rejecting, or startup recovery — RESTORES work that a world-only
            # fingerprint cannot distinguish from "nothing changed": the world afterwards is
            # byte-identical to the world before the proposal existed.
            "ledger": ledger_gen,
            "session_review": [
                bool(review_cfg.get("recognise_prompts")),
                review_cfg.get("decision_context"),
            ],
            "prompts": sorted([k, *v] for k, v in prompt_facts.items()),
            "policy": [
                cfg.get("autonomy"),
                sorted(cfg.get("allowed_verbs") or []),
                cfg.get("confidence_min"),
                cfg.get("max_actions_per_pass"),
                hashlib.sha256(str(cfg.get("prompt", "")).encode()).hexdigest()[:16],
                hashlib.sha256(str(cfg.get("nudge_template", "")).encode()).hexdigest()[:16],
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _prompt_facts(cards: list[dict]) -> dict[str, list]:
    """``id -> [prompt_class, menu digest]`` for the fingerprint. Blocking (screen reads)."""
    out: dict[str, list] = {}
    for c in cards:
        try:
            observed = orchestrator.observed_prompt_for(mission_fence.physical_of(c["id"]))
        except Exception:  # noqa: BLE001 — unreadable is its own stable value
            out[c["id"]] = ["unreadable", ""]
            continue
        menu = json.dumps(observed.get("menu"), sort_keys=True, default=str)
        out[c["id"]] = [
            str(observed.get("prompt_class") or ""),
            hashlib.sha256(menu.encode()).hexdigest()[:16],
        ]
    return out


async def sweep(registry=None) -> dict:
    """One gated orchestrator sweep. Returns a small report:

    * ``{"skipped": "disabled"}`` — ``enabled`` off (never touches the endpoint).
    * ``{"skipped": "unconfigured"}`` — no AI endpoint configured yet.
    * ``{"skipped": "locked"}`` — a pass is already running.
    * ``{"skipped": "unchanged"}`` — the eligible set matches the last pass.
    * ``{"skipped": "empty"}`` — nothing eligible to manage.
    * ``{"ran": True, …}`` — a pass ran.

    Safe to call directly from tests.
    """
    global _last_fingerprint, _next_offset
    # THE OWED TERMINALIZATIONS, on an EXECUTION cadence (#903 review 6, finding 1).
    #
    # A claim this process abandoned but could not release reads as live, so the session looks
    # busy and later autonomous actions for it are dropped — and the read-time reconcile only
    # runs when somebody opens the mission. An operator who closes the page after an ambiguous
    # 502 had nothing left that would ever retry it.
    #
    # BEFORE the `enabled` gate, for the same reason `recover_once` is: a kill-switch for the
    # autonomous sweep is not a kill-switch for finishing what a delivery started.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(ledger.discharge_owed)
    cfg = prefs.get_session_assistance()
    if not cfg["enabled"]:
        # Reset change-detection on disable. Otherwise disable → re-enable inherits the cached
        # fingerprint and the first sweep after re-enabling skips as "unchanged" — the operator
        # turns it back on and nothing happens.
        _last_fingerprint = None
        return {"skipped": "disabled"}
    if not prefs.public_ai_review()["configured"]:
        return {"skipped": "unconfigured"}
    if aitasks.is_running("orchestrator"):
        return {"skipped": "locked"}

    # Housekeeping first: it is cheap, needs no endpoint, and must happen even on a sweep that
    # then short-circuits as unchanged — an overdue proposal should expire on time regardless.
    # Withdrawal rides with expiry, through the SAME helper the state read and the mission cards
    # use (#969), so no surface keeps offering a decision another has already retired.
    expired, _withdrawn = await asyncio.to_thread(actuator.housekeep_pending)

    working = _working_keys(registry)
    # Snapshotted HERE, on the event loop — `snapshot()` iterates a mutable dict (#969).
    busy = actuator.busy_keys(registry)
    cards, _skipped = await asyncio.to_thread(
        orchestrator.eligible_cards, working_keys=working, busy_keys=busy
    )

    # Settlement RESTORES work, and the fingerprint alone cannot see that.
    #
    # A pass stores the fingerprint of the eligible world as it was BEFORE it appended its
    # proposals. While a proposal is live its session is ineligible (the pending-drain), so
    # sweeps return `empty` and leave the fingerprint untouched. When that action later expires
    # or is rejected, the session becomes eligible again — and the world now matches the saved
    # pre-proposal fingerprint exactly, so the sweep skips as "unchanged" and the restored work
    # sits until some unrelated change (the hourly age bucket) happens to move it. With the
    # default 30-minute TTL that silently ignores the configured interval.
    #
    # So both transitions invalidate: anything expiring this sweep, and the eligible set being
    # emptied by pending actions. An extra pass costs one endpoint call; work that quietly
    # stops being reconsidered costs the feature its whole promise.
    if not cards:
        _last_fingerprint = None
        return {"skipped": "empty", "expired": len(expired)}
    fp = await asyncio.to_thread(world_fingerprint, cards, cfg)
    if fp == _last_fingerprint:
        return {"skipped": "unchanged", "expired": len(expired)}

    async with aitasks.single_flight("orchestrator", "auto"):
        report = await orchestrator.run_pass(
            working_keys=working,
            busy_keys=lambda: actuator.busy_keys(registry),
            offset=_next_offset,
        )
        # Deliver what the pass auto-approved. Without this the `yolo` tier is inert: the pass
        # records `approved` and nothing ever sends it, so the operator is told the orchestrator
        # acts on its own while it waits for a tap it was never supposed to need.
        delivered = await actuator.deliver_pass_actions(report["actions"], registry=registry)
    _next_offset = int(report.get("next_offset") or 0)

    # Only advance the fingerprint on a pass that actually completed, so a failed call
    # re-attempts next sweep instead of being masked as "unchanged".
    #
    # And only when the pass CONSUMED the whole eligible set. A pass sends at most
    # DIGEST_MAX cards; recording a fingerprint over the entire world would mark cards 41+
    # as seen even though the model never saw them, and with nothing else changing they would
    # never be reconsidered — permanent starvation of the tail. Leaving the fingerprint unset
    # makes the next sweep run again, which is the correct (if slightly costlier) behaviour.
    if report.get("truncated"):
        # Work remains — either sessions the digest couldn't fit, or model actions the
        # per-pass cap sliced off. Leave the fingerprint unset so the next sweep runs, and the
        # rotation cursor above ensures it covers DIFFERENT sessions rather than re-sending the
        # same slice forever.
        _last_fingerprint = None
        log.info(
            "orchestrator: work remains (%d eligible, %d over the action cap); next sweep "
            "continues from offset %d rather than skipping as unchanged",
            len(cards),
            report.get("over_cap", 0),
            _next_offset,
        )
    else:
        _last_fingerprint = fp
    return {
        "ran": True,
        "actions": len(report["actions"]),
        "delivered": len(delivered),
        "expired": len(expired),
        "truncated": bool(report.get("truncated")),
    }


#: How long the recovery retry waits between attempts, and how many it makes.
#:
#: Recovery reads a file that can be transiently unreadable — a full disk, a stale NFS handle, a
#: sibling holding the writer lock past the read's patience. One attempt turned that into "never",
#: which is the shape of the bug this whole retry exists to close: the ledger row stays `claimed`
#: and the mission's own record stays `sending`, with nothing that would ever look again.
RECOVERY_RETRY_S = 30.0
RECOVERY_ATTEMPTS = 10


async def recover_once() -> bool:
    """One startup-recovery attempt. True when it ran to completion.

    The boolean is the point: a suppressed failure and a successful pass used to be the same
    answer, so a transient error at boot ended recovery for the life of the process (#903 review
    3, finding 4). `recover_until_settled` is what turns "it failed" into "try again".
    """
    try:
        recovered = await asyncio.to_thread(ledger.recover_claimed)
    except Exception:  # noqa: BLE001
        log.warning("orchestrator: startup recovery could not read the ledger", exc_info=True)
        return False
    if recovered:
        log.warning(
            "orchestrator: %d action(s) left mid-delivery by a restart moved to "
            "indeterminate (never auto-retried): %s",
            len(recovered),
            ", ".join(recovered[:5]),
        )
    return True


async def recover_until_settled() -> None:
    """Retry startup recovery until it runs, INDEPENDENTLY of the sweep kill-switch.

    A `claimed` action means the process died between "about to write" and "wrote" —
    indistinguishable on disk, so it is parked as `indeterminate` rather than retried.

    It runs unconditionally because the actions needing it are no longer only the loop's own.
    The relay is a MANUAL route: an operator presses send, the action is claimed, the process
    dies. With recovery behind `AGENT_SESSIONS_ORCHESTRATOR_LOOP=0` — a supported configuration —
    that left the ledger row `claimed` and the mission's own record `sending` for ever, with
    nothing that would ever revisit either. A kill-switch for the autonomous sweep is not a
    kill-switch for crash recovery — and neither is one transient read error, which is why this
    keeps trying rather than returning after the first attempt (#903 review 3, finding 4).
    """
    for attempt in range(RECOVERY_ATTEMPTS):
        if await recover_once():
            return
        if attempt + 1 < RECOVERY_ATTEMPTS:
            await asyncio.sleep(RECOVERY_RETRY_S)
    log.error(
        "orchestrator: startup recovery never completed after %d attempts; actions left "
        "mid-delivery by a restart remain claimed",
        RECOVERY_ATTEMPTS,
    )


async def run(registry=None) -> None:
    """Background orchestrator loop (started from the app lifespan, reaper pattern)."""
    # BEFORE the kill-switch, and as its own TASK: see `recover_until_settled`. A task rather
    # than an await, because the retry can span minutes and the sweep must not wait on it — and
    # because with the loop disabled this function returns immediately, taking any inline retry
    # with it.
    recovery = asyncio.create_task(recover_until_settled())
    if not loop_enabled():
        log.info("orchestrator loop disabled (AGENT_SESSIONS_ORCHESTRATOR_LOOP=0)")
        # The recovery still has to finish. With the sweep off this is the only thing keeping it
        # alive, so it is awaited here rather than dropped on the floor.
        await recovery
        return
    log.info("orchestrator loop armed (gated on the orchestrator prefs per sweep)")
    consecutive_failures = 0
    while True:
        interval_s = max(
            prefs.ORCH_INTERVAL_MIN * 60,
            int(prefs.get_session_assistance()["interval_minutes"]) * 60,
        )
        await asyncio.sleep(interval_s * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            report = await sweep(registry)
        except asyncio.CancelledError:
            raise
        except review.NotConfiguredError:
            continue  # config vanished between the gate and the call — not a failure
        except Exception:
            consecutive_failures += 1
            log.exception("orchestrator sweep crashed")
            continue
        consecutive_failures = 0
        if report.get("ran"):
            log.info("orchestrator sweep: %d action(s) proposed", report.get("actions", 0))
