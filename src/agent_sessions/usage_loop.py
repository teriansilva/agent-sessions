"""Periodic per-agent usage sweep (#839).

Asks each reporting engine what it has spent, on an interval, and announces a crossing of the
operator's threshold once. Same reaper pattern as ``autosort_loop``, with three differences that
come straight from what a probe *is*:

* **It spawns CLIs, so it is single-flight.** ``claude -p "/usage"`` is a real process; two
  sweeps overlapping would double that for nothing. The manual ``POST …/refresh`` shares the
  same flag, so a page with a Refresh button cannot fan out probes per click.
* **It runs off the event loop.** Seconds of ``subprocess.run`` on the loop would stall every
  websocket the app is serving, so the whole sweep goes through ``asyncio.to_thread``.
* **Its cadence is generous.** A plan percentage moves over hours; the default is every 15
  minutes, and nothing here is worth waking a laptop for.

Kill switch: ``AGENT_SESSIONS_USAGE_LOOP=0`` — the task exits at startup and never probes, no
matter what prefs say. Probing is the one thing here an operator might want off wholesale.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading

from . import agent_usage, notifications, prefs

log = logging.getLogger("agent_sessions.usage_loop")

#: Minutes between sweeps. Not a pref: a probe is cheap but not free, and the number it reads
#: moves on the scale of hours. An operator who wants it *now* has the Refresh button.
INTERVAL_MINUTES = 15

_BACKOFF_MAX_MULT = 8

#: Single-flight. A `threading.Lock` and not an asyncio one because the sweep runs in a worker
#: thread — the manual refresh and the loop must exclude each other where they actually execute.
_lock = threading.Lock()
_running = False


def loop_enabled() -> bool:
    """Env kill-switch. ``AGENT_SESSIONS_USAGE_LOOP=0`` stops the background sweep entirely."""
    return (os.environ.get("AGENT_SESSIONS_USAGE_LOOP", "1") or "1") != "0"


def is_running() -> bool:
    with _lock:
        return _running


def refresh_once() -> dict:
    """One sweep: probe, persist, announce. **Blocking** — call it from a thread.

    Returns ``{"skipped": "busy"}`` rather than waiting when a sweep is already in flight. The
    caller is a UI button or a timer; neither benefits from queueing behind six subprocesses.
    """
    global _running
    with _lock:
        if _running:
            return {"skipped": "busy"}
        _running = True
    try:
        # No budgets passed: `refresh` re-reads them after the probes, so the evaluation runs
        # under the policy in force when it is made rather than one snapshotted 90 seconds ago.
        result = agent_usage.refresh()
        # Re-read at the settlement boundary, and **re-evaluate eligibility** rather than just
        # re-checking the toggle. Between `refresh`'s write and this delivery the operator can
        # switch alerts off *or raise the threshold*, and a crossing computed at 90% must not be
        # announced under a policy that now says 99%. Checking only `notify` left the second
        # case delivering under a withdrawn setting.
        budgets = prefs.get_agent_budgets()
        alerts = _still_eligible(result.get("alerts") or [], budgets)
        # **One crossing at a time**, each claimed immediately around its OWN delivery.
        #
        # Claiming the whole batch up front made the at-most-once crash trade contagious: dying
        # during the first delivery left every key pending, and recovery then committed all of
        # them — silencing alerts that were never even attempted. The trade is only defensible
        # for the crossing actually in flight.
        delivered: list[str] = []
        for alert in alerts:
            key = alert["key"]
            agent_usage.mark_pending([key])
            if _announce(alert, budgets):
                agent_usage.mark_announced([key])
                delivered.append(key)
            else:
                # A KNOWN failure comes straight back out, so it is retried rather than
                # recovered as "announced". Only an unknown fate — a crash — is assumed
                # delivered.
                agent_usage.drop_pending([key])
        result["alerts"] = alerts
        result["delivered"] = delivered
        return result
    finally:
        with _lock:
            _running = False


def _still_eligible(alerts: list[dict], budgets: dict) -> list[dict]:
    """The alerts the CURRENT policy would still raise — **recomputed**, not merely re-checked.

    Rechecking the stale `used_pct` against the current levels was only half the fence: the
    percentage itself is derived from the per-engine limit and manual count, so an operator who
    raises a limit from 100 to 200 turns a 95% crossing into 47.5% without touching the
    threshold at all. The row is therefore rebuilt from the latest budget before the level is
    applied, and the key is rebuilt with it — a key carries the limit it was measured against,
    so the one computed earlier no longer identifies this crossing.

    Dropped here means **withdrawn, not consumed**: nothing has been marked announced, so
    restoring the old budget announces it properly on the next sweep.
    """
    if not budgets.get("notify"):
        return []
    levels = set(agent_usage.alert_levels(budgets))
    cfg_all = budgets.get("engines") or {}
    out = []
    for a in alerts:
        engine = a.get("engine") or ""
        row = dict(a.get("row") or {})
        cfg = dict(cfg_all.get(engine) or {})
        cfg.setdefault("limit_tokens", 0)
        cfg.setdefault("manual_used", 0)
        row["limit_tokens"] = cfg["limit_tokens"]
        row["manual_used"] = cfg["manual_used"]
        pct = agent_usage.derive_pct(row, cfg)
        level = a.get("level")
        if level not in levels or not isinstance(pct, int | float) or pct < level:
            continue
        row["used_pct"] = pct
        out.append(
            {**a, "row": row, "used_pct": pct, "key": agent_usage.alert_key(engine, row, level)}
        )
    return out


def _announce(alert: dict, budgets: dict) -> bool:
    """One budget crossing → one bell entry (+ a push). ``True`` when it reached the operator.

    Not an ``escalation``: nothing is waiting on an answer, and the escalation dedupe is keyed on
    session activity, which a budget has none of. The de-duplication that matters here already
    happened in ``agent_usage.evaluate_alerts``.

    The return value is what gates `mark_announced`, so "did this land?" has to be answered
    honestly: a raised exception is **False** (retry next sweep), while `add` returning ``None``
    is **True** — that means an equivalent row is already in the bell, so the operator has been
    told and re-announcing would be the bug, not the fix.
    """
    engine = alert.get("engine") or "?"
    pct = alert.get("used_pct")
    key = str(alert.get("key") or "")
    row = alert.get("row") or {}
    # A cheap second line of defence behind the outbox: if this exact crossing is already a
    # visible bell row, do not write another. It cannot be the PRIMARY guard — the operator can
    # dismiss a row and the ring evicts at `NOTIFY_MAX`, so its absence proves nothing — but its
    # presence proves the crossing was announced, which is worth acting on when it is there.
    if key and _already_in_bell(key):
        log.info("usage alert already in the bell, re-committing: %s", key)
        return True
    window = ""
    windows = [w for w in row.get("windows") or [] if isinstance(w, dict)]
    if windows:
        window = str(max(windows, key=lambda w: w.get("used_pct") or 0).get("label") or "")
    try:
        rec = notifications.add(
            title=f"{engine} at {pct:.0f}% of its budget",
            project="",
            # No session: this is about the agent, not one conversation with it. The bell's
            # session link is left empty rather than pointed at an arbitrary row.
            session_id="",
            engine=engine,
            reason=(
                f"{window} window past the {budgets.get('threshold_pct')}% threshold"
                if window
                else f"past the {budgets.get('threshold_pct')}% threshold"
            ),
            action_id=key,
        )
    except Exception:  # noqa: BLE001 — a full bell must not stop the sweep
        log.exception("could not record usage alert for %s", engine)
        return False
    if rec:
        log.info("usage alert: %s at %s%%", engine, pct)
        # Best-effort, exactly like the orchestrator's caller: the bell row already exists, so a
        # dead push service degrades the alert rather than losing it — and losing it is what
        # returning False here would cause.
        with contextlib.suppress(Exception):
            notifications.fanout(rec)
    return True


def _already_in_bell(key: str) -> bool:
    """Has this exact crossing already been recorded? Fail-open: unreadable ⇒ announce.

    Guessing "already announced" from a failed read would recreate the lost-alert bug this whole
    path exists to avoid, so an unreadable bell means we announce (and may duplicate) rather than
    stay silent — the same direction `notifications.add` fails for escalations.
    """
    try:
        rows = notifications.listing()["notifications"]
    except Exception:  # noqa: BLE001
        log.exception("could not read the bell to check for a duplicate alert")
        return False
    return any(r.get("action_id") == key for r in rows)


async def sweep() -> dict:
    """One sweep, off the event loop. Safe to call directly from tests."""
    return await asyncio.to_thread(refresh_once)


async def run() -> None:
    """Background usage loop (started from the app lifespan, reaper pattern)."""
    if not loop_enabled():
        log.info("usage loop disabled (AGENT_SESSIONS_USAGE_LOOP=0)")
        return
    log.info("usage loop armed (every %d min)", INTERVAL_MINUTES)
    consecutive_failures = 0
    while True:
        await asyncio.sleep(INTERVAL_MINUTES * 60 * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            log.exception("usage sweep crashed")
            continue
        consecutive_failures = 0
