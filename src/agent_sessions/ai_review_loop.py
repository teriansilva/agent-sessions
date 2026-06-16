"""Periodic AI session review loop (#356 Phase 2).

A reaper-pattern background task (see ``reaper.run``/``sweep``) that periodically reviews
the LIVE sessions in the registry through the Phase-1 engine (``review.run_review``), so
the sidebar's summaries / ⚠ badges stay current without anyone clicking "Review now".

Gating — every layer must agree before a single endpoint call happens:

* **Env kill-switch**: ``AGENT_SESSIONS_AI_REVIEW_LOOP=0`` disables the loop entirely —
  the task exits at startup and nothing is ever swept, regardless of prefs. Operator-level
  override for "the endpoint is misbehaving, stop the scheduler NOW" without touching the
  user's saved settings.
* **Prefs**: the ``ai_review.enabled`` flag AND a configured endpoint (base URL + key),
  re-read on EVERY sweep — toggling the Settings switch takes effect at the next wake
  with no restart. The interval comes from prefs too (schema-validated, ≥1 min).

Cost posture (the issue's "runaway cost / hammering the endpoint" risk):

* **Change detection first**: a session's review fingerprint (sha256 of the assembled
  input, exactly what ``review.run_review`` would persist) is computed locally and
  compared against the stored ``review_fingerprint`` BEFORE any network I/O — unchanged
  sessions never reach the endpoint.
* **Serialized calls**: endpoint calls run strictly one at a time with a small sleep
  between them, and a per-sweep cap bounds the worst case — a sweep can never stampede
  the endpoint no matter how many sessions changed (the rest are picked up next sweep).
* **Failure backoff**: consecutive all-failure sweeps double the sleep (capped) so a down
  endpoint is probed ever more gently; any success resets the cadence.

Staleness semantics ride on Phase 1 unchanged: a failed review raises inside
``run_review`` and persists NOTHING — ``reviewed_at``/``review_fingerprint`` only move on
success, so the failed session stays "changed" and is retried next sweep while the UI
keeps showing the last good result with its stale age.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import metadata, prefs, review

log = logging.getLogger("agent_sessions.ai_review_loop")

# Hard per-sweep bound on ENDPOINT CALLS (attempts, not successes — a failing endpoint
# must not widen the sweep). Overflow is picked up by the next sweep.
SWEEP_CAP = 4

# Pause between consecutive endpoint calls inside one sweep: serialization alone prevents
# concurrency, the spacing keeps even a capped sweep from bursting.
CALL_SPACING_S = 2.0

# Backoff multiplier ceiling for consecutive all-failure sweeps (interval × up-to-8).
_BACKOFF_MAX_MULT = 8

# Grace between an early wake (a freshly created session) and the sweep it triggers: lets the
# session's first output land so gather_input has something to hash (an empty session is a no-op
# skip anyway), and coalesces a burst of new sessions into ONE sweep.
KICK_GRACE_S = 3.0

# Set by ``run`` once the loop is live; ``request_review_soon`` wakes the sweep through it. None
# until the loop starts — a kick is then a safe no-op (env kill-switch off, or tests that drive
# ``sweep`` directly).
_wake: asyncio.Event | None = None


def request_review_soon() -> None:
    """Wake the review loop to sweep ahead of its interval (#413).

    Called when a new session is created so its summary / ⚠ badge populate promptly instead of
    waiting up to ``interval_minutes``. Keyless on purpose: it just advances the existing gated
    sweep, which already skips disabled / unconfigured / unchanged / empty sessions — so a kick
    can never force an endpoint call the periodic loop wouldn't have made. No-op until the loop
    is armed and on the kill-switch path."""
    if _wake is not None:
        _wake.set()


def loop_enabled() -> bool:
    """Env kill-switch — overrides everything. ``AGENT_SESSIONS_AI_REVIEW_LOOP=0`` keeps
    the background task from ever sweeping; any other value (default) arms the loop,
    which is still a no-op per sweep until the prefs ``enabled`` flag + endpoint config
    say go."""
    return (os.environ.get("AGENT_SESSIONS_AI_REVIEW_LOOP", "1") or "1") != "0"


def _configured(cfg: dict) -> bool:
    return bool(str(cfg["base_url"]).strip()) and bool(cfg["api_key"])


async def sweep(registry) -> tuple[list[str], int]:
    """One review pass over the live registry. Returns ``(reviewed_keys, failures)``.

    Re-reads prefs (enabled / configured / max_input_chars) so Settings changes apply
    without a restart. Skips excluded and archived sessions, then skips any session whose
    locally-computed fingerprint matches the stored one WITHOUT calling the endpoint.
    Remaining candidates are reviewed strictly one at a time (small sleep between calls,
    ``SWEEP_CAP`` attempts max). Safe to call directly from tests.
    """
    cfg = prefs.get_ai_review()
    if not cfg["enabled"] or not _configured(cfg):
        return [], 0
    max_chars = int(cfg["max_input_chars"])
    reviewed: list[str] = []
    failures = 0
    attempts = 0
    for row in registry.snapshot():
        if attempts >= SWEEP_CAP:
            break
        key = row.get("id")
        if not key:
            continue
        try:
            meta = metadata.get(metadata.resolve_key(key))
        except Exception:
            log.debug("ai-review: metadata read failed for %s — skipping", key, exc_info=True)
            continue
        if meta.review_excluded or meta.archived is True:
            continue
        # Change detection BEFORE any network I/O: gather_input is exactly what
        # run_review hashes+persists, so fingerprint equality ⇔ the endpoint would see
        # the same input it already reviewed. Nothing to review / a gather error just
        # skips the session (fail-soft, no endpoint call either way).
        try:
            _, fingerprint = await asyncio.to_thread(review.gather_input, key, max_chars)
        except Exception:
            # Includes ReviewError("nothing to review") — common for fresh/quiet
            # sessions; never worth an endpoint call, never worth log spam.
            log.debug("ai-review: no reviewable input for %s — skipping", key, exc_info=True)
            continue
        if fingerprint == meta.review_fingerprint:
            continue
        if attempts:
            await asyncio.sleep(CALL_SPACING_S)
        attempts += 1
        try:
            await review.run_review(key)
            reviewed.append(key)
        except review.NotConfiguredError:
            # Config cleared mid-sweep — nothing further can succeed this pass.
            break
        except review.ReviewError as e:
            # Fail-soft (#356): run_review persisted nothing, the session stays
            # "changed" and is retried next sweep. Message is operator-safe (no key).
            failures += 1
            log.warning("ai-review failed for %s: %s", key, e)
        except Exception:
            failures += 1
            log.exception("ai-review crashed for %s", key)
    return reviewed, failures


async def run(registry) -> None:
    """Background review loop (started from the app lifespan, reaper pattern). Exits
    immediately under the env kill-switch; otherwise sleeps ``interval_minutes`` (prefs,
    re-read every iteration) × the failure-backoff multiplier between sweeps."""
    global _wake
    if not loop_enabled():
        log.info("ai-review loop disabled (AGENT_SESSIONS_AI_REVIEW_LOOP=0)")
        return
    _wake = asyncio.Event()
    log.info("ai-review loop armed (gated on the ai_review prefs per sweep)")
    consecutive_failures = 0
    while True:
        interval_s = max(60, int(prefs.get_ai_review()["interval_minutes"]) * 60)
        delay = interval_s * min(2**consecutive_failures, _BACKOFF_MAX_MULT)
        # Sleep until the interval elapses OR a new session kicks us (request_review_soon).
        # On an early wake, wait a short grace so the session's first output lands and a burst
        # of new sessions coalesces into one sweep.
        try:
            await asyncio.wait_for(_wake.wait(), timeout=delay)
            await asyncio.sleep(KICK_GRACE_S)
        except TimeoutError:
            pass  # normal interval — no kick
        _wake.clear()
        try:
            reviewed, failures = await sweep(registry)
        except Exception:
            log.exception("ai-review sweep crashed")
            continue
        if failures and not reviewed:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if reviewed:
            log.info("ai-review sweep: reviewed %d session(s)", len(reviewed))
