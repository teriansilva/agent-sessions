"""Idle-session reaper (#279).

Sessions accumulate without bound: each web terminal launches a ``dtach`` master backing a full
agent process (claude/opencode/gemini), and nothing reaps them — so over days dozens pile up,
holding GBs of RAM, pushing swap full, and climbing toward the systemd ``TasksMax`` ceiling until
the single-process app slows and eventually can't spawn new PTYs. That's "prod got very slow".

The reaper is a background task that periodically tears down **STALE** sessions only — ones that are
both DETACHED (no client attached) and IDLE (no PTY output) for longer than a TTL. It NEVER touches
an active session: a client attached, or recent output, always leaves it alone. Tearing down means
killing the live ``dtach`` master (the agent process exits); the engine's conversation transcript is
already on disk, so a reaped session stays fully resumable — only the live process + PTY + in-memory
VT mirror are reclaimed, not history.

Safety posture (the issue flags "killing active work" as the top risk):
- **Opt-in**: disabled unless ``AGENT_SESSIONS_REAP_IDLE_SECONDS`` > 0.
- **Dry-run by default**: ``AGENT_SESSIONS_REAP_DRY_RUN`` defaults on → logs every candidate it
  WOULD reap and kills nothing, so the selection can be validated before it acts for real.
- **Conservative selection**: attached or recently-active sessions are exempt; an explicit
  ``AGENT_SESSIONS_REAP_EXEMPT`` id list pins sessions out of reach.

Env:
- ``AGENT_SESSIONS_REAP_IDLE_SECONDS``     idle TTL; 0/unset disables the reaper entirely.
- ``AGENT_SESSIONS_REAP_INTERVAL_SECONDS`` sweep cadence (default 300).
- ``AGENT_SESSIONS_REAP_DRY_RUN``          "0" to actually reap; else observe only (default).
- ``AGENT_SESSIONS_REAP_EXEMPT``           comma-separated session ids never reaped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from typing import TYPE_CHECKING

from . import engines, ptybridge

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("agent_sessions.reaper")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (ValueError, TypeError):
        return default


def idle_ttl() -> int:
    return _env_int("AGENT_SESSIONS_REAP_IDLE_SECONDS", 0)


def interval() -> int:
    return max(5, _env_int("AGENT_SESSIONS_REAP_INTERVAL_SECONDS", 300))


# Grace between SIGTERM and the SIGKILL escalation. Some agents are slow to exit on the SIGHUP they
# get when their dtach master dies, and a few ignore SIGTERM outright — escalate so a reap always
# actually frees the session instead of re-logging the same survivor every sweep.
_REAP_GRACE_S = 3.0


def enabled() -> bool:
    return idle_ttl() > 0


def dry_run() -> bool:
    return (os.environ.get("AGENT_SESSIONS_REAP_DRY_RUN", "1") or "1") != "0"


def _exempt() -> set[str]:
    raw = os.environ.get("AGENT_SESSIONS_REAP_EXEMPT", "") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_stale(attached: bool, last_activity: float | None, now: float, ttl: int) -> bool:
    """Whether a session is a reap candidate: DETACHED and IDLE past the TTL.

    Never stale while a client is attached, or while ``last_activity`` is recent. An unknown
    ``last_activity`` (no signal at all) is treated as NOT stale — we never reap a session we have
    no idle evidence for. Pure + side-effect-free so the selection is unit-testable without procs.

    ``last_activity`` must be a REAL last-activity time, NOT session age — reaping by age alone
    could kill long-lived *active* sessions. The caller resolves it as ``max(last_output_at,
    last-activity time)``: ``last_output_at`` (live while the app runs) OR the engine last-activity
    time (``Session.last_mtime`` — since #525 the newest conversation-record timestamp, so a bare
    idle re-open no longer masquerades as activity; restart-proof — survives a deploy, and reflects
    the last real turn even for a session silent since before this process started, which
    ``last_output_at`` alone misses).
    """
    if attached:
        return False  # a client is viewing it — always active, never reap
    if last_activity is None:
        return False  # no idle signal → don't risk it
    return (now - last_activity) >= ttl


def _activity_mtimes() -> dict[tuple[str, str], float]:
    """``(engine, uuid) → last-activity time`` for every scannable session (best-effort). This is
    ``Session.last_mtime``, which since #525 is the newest conversation-record timestamp (last real
    turn), NOT the raw transcript file mtime — so a session that was merely *opened* (a bare resume
    bumps the file mtime via timestamp-less app-state records) is no longer seen as recently active
    and stays a valid reap candidate. Restart-proof: it survives a deploy because it's read off the
    on-disk transcript."""
    out: dict[tuple[str, str], float] = {}
    with contextlib.suppress(Exception):
        for s in engines.scan_all():
            out[(s.engine, s.uuid)] = s.last_mtime
    return out


def _last_activity(row: dict, mtimes: dict[tuple[str, str], float]) -> float | None:
    """The later of the live ``last_output_at`` and the transcript mtime; ``None`` if neither.
    Falls back to ``started_at`` (#398) so a session that never produced output can still be reaped.
    """
    candidates = [
        t
        for t in (
            row.get("last_output_at"),
            mtimes.get((row.get("engine"), row.get("sid"))),
            row.get("started_at"),
        )
        if t is not None
    ]
    return max(candidates) if candidates else None


def _find_master_pid(engine: str, sid: str) -> int | None:
    """PID of the ``dtach -c <sock>`` master for a session, by scanning /proc for the create-mode
    process bound to the session's socket. ``None`` if not found. (dtach writes no pidfile.)"""
    try:
        sock = str(ptybridge.socket_path(engine, sid)).encode()
    except Exception:
        return None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                parts = fh.read().split(b"\0")
        except OSError:
            continue
        # The MASTER is `dtach -c <sock> …`; the registry's reader is `dtach -a <sock>` (skip it).
        if b"-c" in parts and sock in parts:
            with contextlib.suppress(ValueError):
                return int(name)
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def _signal_tree(pid: int, sig: int) -> None:
    """Send ``sig`` to the session's whole process group (dtach master + the agent + its children).

    The dtach master is spawned in its own session (``start_new_session=True``), so its pgid == pid
    and signalling the group takes down the agent too — more reliable than SIGTERM to the master
    alone (which only HUPs the agent indirectly, and some agents ignore that). Falls back to the
    bare pid if the group can't be resolved."""
    try:
        os.killpg(os.getpgid(pid), sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


def _still_stale(registry, key: str, ttl: int, now: float | None = None) -> bool:
    """Re-check a single session's CURRENT state against the live registry — used right before each
    signal so a candidate that got re-attached or became active since the sweep snapshot is spared
    (Hermes #273). Gone from the registry → not stale (nothing to reap)."""
    now = time.time() if now is None else now
    mtimes = _activity_mtimes()
    for row in registry.snapshot():
        if row.get("id") == key:
            return is_stale(bool(row.get("attached")), _last_activity(row, mtimes), now, ttl)
    return False


async def terminate_master(
    engine: str,
    sid: str,
    *,
    key: str | None = None,
    grace_s: float = _REAP_GRACE_S,
    spare_if: Callable[[], bool] | None = None,
) -> str:
    """Terminate the ``dtach`` master (+ agent process group) for one session and free its in-memory
    VT mirror. Shared by the idle reaper (#279) and the manual session-restart endpoint (#331) so
    there is a single process-management path.

    SIGTERM the master's process group, wait ``grace_s``, then escalate to SIGKILL if it is still
    alive. The on-disk transcript is never touched → the session stays fully resumable; only the
    live process + PTY (+ VT mirror, when ``key`` is given) are reclaimed.

    ``spare_if`` is an optional predicate re-checked right before SIGTERM **and** right before the
    SIGKILL escalation; returning ``False`` aborts the kill (the reaper uses it to spare a session
    that got (re)attached or became active in the grace window). Returns the outcome:
    ``"gone"`` (no master found), ``"spared"`` (``spare_if`` vetoed), ``"term"`` (exited on
    SIGTERM), or ``"kill"`` (needed SIGKILL).
    """

    pid = _find_master_pid(engine, sid)
    if pid is None:
        return "gone"
    if spare_if is not None and not spare_if():
        return "spared"
    _signal_tree(pid, signal.SIGTERM)
    await asyncio.sleep(grace_s)
    outcome = "term"
    if _alive(pid):
        # The grace window is exactly when a reattach is most likely — re-validate before the
        # harder SIGKILL.
        if spare_if is not None and not spare_if():
            return "spared"
        _signal_tree(pid, signal.SIGKILL)
        outcome = "kill"
    return outcome


async def _reap_one(registry, row: dict, *, idle_s: int, dry: bool, ttl: int) -> None:
    key = row["id"]
    engine = row["engine"]
    sid = row["sid"]
    verb = "would reap" if dry else "reaping"
    log.warning(
        "%s stale session %s (engine=%s, detached, idle %ds >= TTL)",
        verb,
        key,
        engine,
        int(idle_s),
    )
    if dry:
        return
    # Tear down the live session via the shared helper: SIGTERM the process group (dtach master +
    # agent), escalate to SIGKILL after the grace, and free the VT mirror. The registry's own
    # SessionStream sees EOF and self-cleans (_watch_end drops the entry). History is on disk → the
    # session stays resumable; only the live process + PTY are reclaimed. ``spare_if`` re-checks the
    # LIVE registry before each signal so a candidate that got (re)attached since the sweep snapshot
    # — or during the SIGTERM grace — is never killed.
    if not _still_stale(registry, key, ttl):
        log.info("reaper: %s became active before reap — sparing", key)
        return
    outcome = await terminate_master(
        engine, sid, key=key, spare_if=lambda: _still_stale(registry, key, ttl)
    )
    if outcome == "gone":
        log.warning("reaper: no dtach master PID found for %s (already gone?)", key)
    elif outcome == "spared":
        log.info("reaper: %s became active before/during reap — sparing", key)
    elif outcome == "kill":
        log.warning("reaper: %s survived SIGTERM, escalated to SIGKILL", key)


async def sweep(registry, *, now: float | None = None) -> list[str]:
    """One reap pass. Returns the ids selected (logged either way; killed unless dry-run). Safe to
    call directly from tests."""
    now = time.time() if now is None else now
    ttl = idle_ttl()
    if ttl <= 0:
        return []
    exempt = _exempt()
    dry = dry_run()
    mtimes = _activity_mtimes()
    selected: list[str] = []
    for row in registry.snapshot():
        key = row["id"]
        if key in exempt or row.get("sid") in exempt:
            continue
        last_activity = _last_activity(row, mtimes)
        if not is_stale(bool(row.get("attached")), last_activity, now, ttl):
            continue
        selected.append(key)
        with contextlib.suppress(Exception):
            await _reap_one(registry, row, idle_s=now - (last_activity or now), dry=dry, ttl=ttl)
    return selected


async def run(registry) -> None:
    """Background reaper loop (started from the app lifespan). No-op when disabled."""
    if not enabled():
        return
    log.info(
        "reaper armed: idle TTL %ds, interval %ds, dry_run=%s",
        idle_ttl(),
        interval(),
        dry_run(),
    )
    while True:
        await asyncio.sleep(interval())
        with contextlib.suppress(Exception):
            reaped = await sweep(registry)
            if reaped:
                log.info(
                    "reaper sweep: %d stale session(s) %s",
                    len(reaped),
                    "observed (dry-run)" if dry_run() else "reaped",
                )
