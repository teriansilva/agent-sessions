"""Periodic Pulse scan loop (#441 Phase 3).

A reaper-pattern background task mirroring ``autosort_loop`` / ``ai_review_loop``: every
``interval_minutes`` it refreshes the cached recent-work overview — but only when the gates
agree AND the underlying sessions actually changed, so "constantly reads the sessions" stays
cheap (and, at ``fast`` depth, free).

Gating — all must hold before a scan happens:

* **Env kill-switch** ``AGENT_SESSIONS_PULSE_LOOP=0`` — the task exits at startup and never
  sweeps, regardless of prefs (operator override).
* **Prefs** ``pulse.auto_enabled``, re-read on EVERY sweep, so the Settings toggle takes
  effect at the next wake without a restart. ``interval_minutes`` / ``window_days`` /
  ``scan_depth`` are read per sweep too.
* **Single-flight** — if a Pulse scan is already running (a manual "Scan now"), the sweep
  is skipped (``aitasks`` per-kind guard); the two never overlap.
* **Change detection** — the in-window input fingerprint is recomputed (cheap, FS only) and
  compared to the cached artifact's; an unchanged set is a no-op (no scan, no LLM call), the
  same posture as the AI-review loop.

Failures are swallowed (logged) with an interval backoff so a flaky endpoint (depth ≥ medium)
never crashes the loop and is probed ever more gently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from . import aitasks, prefs, pulse

log = logging.getLogger("agent_sessions.pulse_loop")

# Failure-backoff multiplier ceiling (interval × up-to-8) for consecutive crashed sweeps.
_BACKOFF_MAX_MULT = 8


def loop_enabled() -> bool:
    """Env kill-switch — overrides everything. ``AGENT_SESSIONS_PULSE_LOOP=0`` keeps the task
    from ever sweeping; any other value arms it (still a per-sweep no-op until the prefs
    ``auto_enabled`` flag says go)."""
    return (os.environ.get("AGENT_SESSIONS_PULSE_LOOP", "1") or "1") != "0"


def _working_keys(registry) -> set[str]:
    """Live "in flight" overlay keys (working or attached). Best-effort — a registry hiccup
    yields no overlay, never a crash. ``None`` registry (tests) → no overlay."""
    if registry is None:
        return set()
    keys: set[str] = set()
    with contextlib.suppress(Exception):
        for r in registry.snapshot():
            if r.get("working") or r.get("attached"):
                keys.add(r["id"])
    return keys


async def sweep(registry=None) -> dict:
    """One gated Pulse sweep. Returns a small report:

    * ``{"skipped": "disabled"}`` — ``auto_enabled`` off (never touches the cache/endpoint).
    * ``{"skipped": "locked"}`` — a Pulse scan is already running (single-flight held).
    * ``{"skipped": "unchanged"}`` — the in-window set matches the cache (no scan, no LLM).
    * ``{"scanned": True, …}`` — a fresh scan ran and the cache was rewritten.

    Re-reads prefs so Settings changes apply live. Safe to call directly from tests.
    """
    cfg = prefs.get_pulse()
    if not cfg["auto_enabled"]:
        return {"skipped": "disabled"}
    if aitasks.is_running("pulse-scan"):
        return {"skipped": "locked"}
    window_days = int(cfg["window_days"])
    depth = str(cfg["scan_depth"])
    # Change detection BEFORE the single-flight + any scan/LLM work: an unchanged in-window
    # set (same window/depth) is a no-op. The fingerprint is content-only (it excludes the
    # volatile live overlay), exactly like the cached artifact's `input_fingerprint`.
    cached = pulse.load_cache()
    if cached is not None and cached.get("input_fingerprint") == await pulse.fingerprint_for(
        window_days=window_days, depth=depth
    ):
        return {"skipped": "unchanged"}
    working = _working_keys(registry)
    async with aitasks.single_flight("pulse-scan", "auto"):
        artifact = await pulse.run_scan(window_days=window_days, depth=depth, working_keys=working)
    return {
        "scanned": True,
        "cards": len(artifact["cards"]),
        "synthesis_skipped": artifact["synthesis_skipped"],
    }


async def run(registry=None) -> None:
    """Background Pulse loop (started from the app lifespan, reaper pattern). Exits immediately
    under the env kill-switch; otherwise sleeps ``interval_minutes`` (prefs, re-read every
    iteration) × the failure-backoff multiplier between sweeps."""
    if not loop_enabled():
        log.info("pulse loop disabled (AGENT_SESSIONS_PULSE_LOOP=0)")
        return
    log.info("pulse loop armed (gated on the pulse prefs per sweep)")
    consecutive_failures = 0
    while True:
        interval_s = max(
            prefs.PULSE_INTERVAL_MIN * 60,
            int(prefs.get_pulse()["interval_minutes"]) * 60,
        )
        await asyncio.sleep(interval_s * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            report = await sweep(registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            log.exception("pulse sweep crashed")
            continue
        consecutive_failures = 0
        if report.get("scanned"):
            log.info("pulse sweep: refreshed overview (%d cards)", report.get("cards", 0))
