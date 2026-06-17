"""Periodic AI auto-sort loop (#424 Phase 6).

A reaper-pattern background task mirroring ``ai_review_loop``: every ``interval_minutes`` it
runs one bounded ``autosort.run_sort`` pass, but only when every gate agrees.

Gating — all must hold before a single endpoint call happens:

* **Env kill-switch** ``AGENT_SESSIONS_AUTO_SORT_LOOP=0`` — the task exits at startup and
  never sweeps, regardless of prefs (operator override).
* **Prefs**: ``auto_sort.enabled`` AND a configured (reused) ai_review endpoint, re-read on
  EVERY sweep, so the Settings toggle takes effect at the next wake without a restart.
* **Per-run cap** (``auto_sort.max_per_pass``, #459) inside ``autosort.run_sort`` bounds
  endpoint calls; calls are serialized and spaced.

Failures are swallowed (logged) with an interval backoff so a flaky endpoint never crashes
the loop and is probed ever more gently.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import aitasks, autosort, prefs

log = logging.getLogger("agent_sessions.autosort_loop")

# Failure-backoff multiplier ceiling (interval × up-to-8) for consecutive crashed sweeps.
_BACKOFF_MAX_MULT = 8


def loop_enabled() -> bool:
    """Env kill-switch — overrides everything. ``AGENT_SESSIONS_AUTO_SORT_LOOP=0`` keeps the
    task from ever sweeping; any other value arms it (still a per-sweep no-op until the prefs
    ``enabled`` flag + a configured endpoint say go)."""
    return (os.environ.get("AGENT_SESSIONS_AUTO_SORT_LOOP", "1") or "1") != "0"


def _ready(cfg: dict) -> bool:
    return bool(cfg.get("enabled")) and bool(prefs.public_ai_review().get("configured"))


async def sweep() -> dict:
    """One gated auto-sort pass. Returns the report (``skipped="disabled"`` when gated off so
    the loop never reaches the endpoint). Re-reads prefs so Settings changes apply live. Safe
    to call directly from tests."""
    cfg = prefs.get_auto_sort()
    if not _ready(cfg):
        return {"candidates": 0, "scanned": 0, "assigned": [], "skipped": "disabled"}
    # Track the real work in the shared AI-activity registry (#441) — only PAST the gate, so a
    # disabled/unconfigured sweep stays a silent no-op, not a phantom "auto-sort ran" entry.
    async with aitasks.track("auto-sort", "sweep"):
        return await autosort.run_sort()


async def run() -> None:
    """Background auto-sort loop (started from the app lifespan, reaper pattern). Exits
    immediately under the env kill-switch; otherwise sleeps ``interval_minutes`` (prefs,
    re-read every iteration) × the failure-backoff multiplier between sweeps."""
    if not loop_enabled():
        log.info("auto-sort loop disabled (AGENT_SESSIONS_AUTO_SORT_LOOP=0)")
        return
    log.info("auto-sort loop armed (gated on the auto_sort prefs per sweep)")
    consecutive_failures = 0
    while True:
        interval_s = max(
            prefs.AUTO_SORT_INTERVAL_MIN * 60,
            int(prefs.get_auto_sort()["interval_minutes"]) * 60,
        )
        await asyncio.sleep(interval_s * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            report = await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            log.exception("auto-sort sweep crashed")
            continue
        consecutive_failures = 0
        if report.get("assigned"):
            log.info("auto-sort sweep: assigned %d session(s)", len(report["assigned"]))
