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

#: How long to keep confirming that a SIGKILLed group has actually gone. SIGKILL is not
#: refusable, but reaping is not instantaneous and a process in an uninterruptible wait outlives
#: it briefly. Confirmed rather than assumed, because a caller's whole decision — "may I report
#: this launch as stopped" — rests on the answer (#898 review 4, finding 1).
_KILL_CONFIRM_S = 0.25
_KILL_CONFIRM_TRIES = 8


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
    """PID of the ``dtach`` master for a session, by scanning /proc for the create-mode process
    bound to the session's socket. ``None`` if not found. (dtach writes no pidfile.)

    **Both create modes count** (#739). A headless launch uses ``-n`` rather than ``-c``, and a
    matcher that knew only about ``-c`` would not see that master at all — so archive-time cleanup
    and the split-brain guard would silently skip every dispatched session, leaving unreapable
    agents behind with nothing on screen to say so. The two flags are matched together, and only
    an ``-a`` attach (the registry's reader) is excluded.
    """
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
        # The MASTER is `dtach -c <sock> …` (viewer) or `dtach -n <sock> …` (headless, #739);
        # the registry's reader is `dtach -a <sock>` and is skipped.
        if sock in parts and (b"-c" in parts or b"-n" in parts):
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


def _proc_table() -> dict[int, tuple[int, int, bool]]:
    """`{pid: (ppid, pgid, is_zombie)}` for every process we can read. Never raises.

    One pass, because the callers below ask three questions about the same snapshot and reading
    procfs three times would let the answers disagree with each other.
    """
    out: dict[int, tuple[int, int, bool]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue  # it exited while we were looking, which is the answer we wanted anyway
        # `comm` is parenthesised and may itself contain spaces and brackets, so the fields are
        # taken after the LAST ')' — state, ppid, pgrp.
        cut = raw.rfind(b")")
        fields = raw[cut + 2 :].split()
        if len(fields) < 3:
            continue
        with contextlib.suppress(ValueError):
            out[int(name)] = (int(fields[1]), int(fields[2]), fields[0] == b"Z")
    return out


#: Where the unified (v2) hierarchy is mounted. A constant rather than a `/proc/mounts` parse:
#: this is the path on every systemd host the app supports, and a wrong guess degrades to the
#: process-tree boundary rather than to a wrong answer.
CGROUP_ROOT = "/sys/fs/cgroup"


def _cgroup_of(pid: int) -> str | None:
    """The pid's cgroup-v2 path (``/user.slice/…/as-claude-….scope``), or None.

    None means "no usable cgroup boundary here" — a v1-only host, an unreadable procfs entry, or
    a process that has already gone. Every caller degrades to the process tree on None.
    """
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # v2 is the single `0::<path>` line; v1 controllers carry a controller list.
                if line.startswith("0::"):
                    path = line[3:].strip()
                    return path or None
    except OSError:
        return None
    return None


def _cgroup_members(path: str) -> set[int] | None:
    """Every pid currently in that cgroup, or None if it cannot be read.

    None and `set()` are different answers and the difference is the point: an empty set is
    "the boundary is empty", which is what lets a teardown report success, while None is "we
    could not look", which must never be read as success.
    """
    rel = path.lstrip("/")
    try:
        with open(f"{CGROUP_ROOT}/{rel}/cgroup.procs", encoding="ascii") as fh:
            return {int(x) for x in fh.read().split() if x.isdigit()}
    except OSError:
        return None


def _containment_cgroup(pid: int) -> str | None:
    """The pid's cgroup, but ONLY when it is a boundary we may treat as the session's.

    The disqualifier is the same one that applies to process groups, for the same reason: this
    app's own cgroup contains this app. A dispatch that launched through `scopedspawn` sits in a
    transient scope of its own, which is a strictly smaller set — and if it did not, signalling
    "the boundary" would be signalling the broker (#898 review 6).
    """
    own = _cgroup_of(os.getpid())
    theirs = _cgroup_of(pid)
    if theirs is None or theirs == own:
        return None
    return theirs


def _containment(pid: int) -> tuple[set[int], set[int]]:
    """`(pids, pgids)` — the process tree rooted at `pid`, and the groups it OWNS.

    **The boundary is the tree, not one process group** (#898 review 5, finding 1). `dtach` calls
    `setsid()` on the target so the pty gets its own controlling terminal, so the master and the
    agent it launched are in DIFFERENT groups and different sessions — measured on this host: a
    master at pgid 1173666 with its agent at 1173667. A group-wide signal aimed at the master
    therefore contains the master and nothing else, and a teardown that checks only that group
    reports success over a live, permission-bypassed agent.

    **Captured BEFORE any signal, and that is the whole reason it is a snapshot.** Killing the
    master reparents its children to init, so a tree walk afterwards finds nothing at all and
    would report a clean teardown for exactly the case this exists to catch.

    A pgid is included only when its LEADER is inside the tree — a descendant that joined a group
    somebody else established is signalled by pid, never by group. That is what stops a teardown
    reaching processes it did not launch (#898 review 5, finding 3), and it is checked here
    rather than recomputed at the signal site, where the validation was previously thrown away.
    """
    table = _proc_table()
    kids: dict[int, list[int]] = {}
    for child, (parent, _pg, _z) in table.items():
        kids.setdefault(parent, []).append(child)
    pids: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        if cur in pids or cur not in table:
            continue
        pids.add(cur)
        stack.extend(kids.get(cur, ()))
    if not pids:
        return set(), set()
    try:
        ours = os.getpgid(0)
    except OSError:
        ours = -1
    pgids = {
        pg
        for p in pids
        for pg in (table[p][1],)
        # The leader must be IN the tree — otherwise the group is somebody else's — and it must
        # not be ours, or the escalation is a SIGKILL to this process.
        if pg in pids and pg != ours
    }
    return pids, pgids


def _boundary_alive(pids: set[int], cgroup: str | None = None) -> bool:
    """Is anything in the boundary still running? Zombies do not count.

    The master is spawned by this app, so the instant it exits it is an unreaped child sitting in
    the process table — and counting that as alive makes every clean teardown report a leak.

    **The cgroup is re-read, not remembered** (#898 review 6). A pid snapshot cannot see a
    survivor that did not exist when it was taken: a target that answers SIGTERM by forking a
    child and exiting hands that child to init, and a tree walk rooted at the dead master will
    never find it again. The cgroup does — membership is inherited across fork and survives
    reparenting, which is exactly the property a snapshot lacks.

    An unreadable cgroup returns to the pid answer rather than to `False`: "we could not look" is
    not "it is empty".
    """
    table = _proc_table()
    if cgroup is not None:
        members = _cgroup_members(cgroup)
        if members is not None:
            # Ourselves excluded for the same reason the group check excludes our own group: on a
            # host with no scope isolation the session can share this app's cgroup, and counting
            # this process would make every teardown report a leak for ever.
            live = {p for p in members if p != os.getpid()}
            if any(p in table and not table[p][2] for p in live):
                return True
    return any(p in table and not table[p][2] for p in pids)


def _signal_boundary(
    pids: set[int], pgids: set[int], sig: int, *, cgroup: str | None = None
) -> None:
    """Signal a boundary `_containment` captured. **Never recomputes a group.**

    That recomputation was the bug (#898 review 5, finding 3): the caller validated which groups
    were ours to touch and then the signal path called `os.getpgid(pid)` again and `killpg`'d
    whatever came back — including the group it had just been told to leave alone. The validated
    identity is passed in, and a pid whose group did not qualify is signalled on its own.

    Groups first so a group signal reaches members that appeared after the snapshot; then the
    pids, which covers anything whose group was somebody else's.
    """
    # THE CGROUP FIRST, because it is the only member list that includes what was forked after
    # the snapshot was taken. `_containment_cgroup` has already refused this app's own cgroup, so
    # this cannot be a signal to the broker; ourselves is skipped again here because that
    # guarantee is worth two lines rather than one.
    if cgroup is not None:
        for p in _cgroup_members(cgroup) or ():
            if p == os.getpid():
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(p, sig)
    for pg in pgids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pg, sig)
    for p in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(p, sig)


def _signal_tree(pid: int, sig: int) -> None:
    """Signal the tree rooted at `pid`, capturing its boundary first.

    Kept as the one-argument entry point the reaper's other callers use; `terminate_master`
    captures the boundary itself because it has to ask about it again afterwards.
    """
    pids, pgids = _containment(pid)
    if not pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)
        return
    _signal_boundary(pids, pgids, sig)


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
    ``"gone"`` (no master found), ``"spared"`` (``spare_if`` vetoed), ``"term"`` (the boundary was
    empty after SIGTERM), ``"kill"`` (needed SIGKILL), or ``"leaked"`` (something in the boundary
    survived SIGKILL, so the caller must not report a clean teardown).

    **The boundary is the process TREE rooted at the master, captured before anything is
    signalled** (#898 reviews 4 and 5, finding 1). Two earlier answers were both too small: the
    master pid alone missed a child that ignores SIGHUP and SIGTERM, and the master's process
    GROUP missed the agent entirely — `dtach` calls `setsid()` on its target, so the two are in
    different groups and different sessions. Measured on this host: master pgid 1173666, agent
    pgid 1173667. A teardown reporting success over a live, permission-bypassed agent is the one
    answer this function must never give.
    """

    pid = _find_master_pid(engine, sid)
    if pid is None:
        return "gone"
    if spare_if is not None and not spare_if():
        return "spared"
    # CAPTURED BEFORE THE SIGNAL, because killing the master reparents its children to init and
    # the tree walk afterwards would find nothing — a clean-looking teardown over a live agent.
    # THE DURABLE BOUNDARY, when there is one. A transient scope's cgroup contains everything the
    # session forks and keeps containing it after a reparent, so it answers the question a pid
    # snapshot cannot (#898 review 6). None on a host with no scope isolation, and then the tree
    # snapshot below is the whole boundary — reduced, and reported as such by the same rules.
    cgroup = _containment_cgroup(pid)
    pids, pgids = _containment(pid)
    if not pids:
        pids = {pid}
    _signal_boundary(pids, pgids, signal.SIGTERM, cgroup=cgroup)
    await asyncio.sleep(grace_s)
    outcome = "term"
    if _boundary_alive(pids, cgroup):
        # The grace window is exactly when a reattach is most likely — re-validate before the
        # harder SIGKILL.
        if spare_if is not None and not spare_if():
            return "spared"
        # RE-CAPTURED: SIGTERM may have caused the agent to fork a cleanup child, and a snapshot
        # taken before the signal cannot know about it. Unioned rather than replaced, because the
        # original members are the ones that have to be confirmed gone.
        more, more_groups = _containment(pid)
        pids |= more
        pgids |= more_groups
        _signal_boundary(pids, pgids, signal.SIGKILL, cgroup=cgroup)
        outcome = "kill"
        for _ in range(_KILL_CONFIRM_TRIES):
            await asyncio.sleep(_KILL_CONFIRM_S)
            if not _boundary_alive(pids, cgroup):
                break
            # A survivor the snapshot never knew about — forked during teardown and reparented —
            # is only reachable through the cgroup, and only by signalling it again now that it
            # is a member. Re-signalling members already dead is a no-op.
            _signal_boundary(set(), set(), signal.SIGKILL, cgroup=cgroup)
        else:
            outcome = "leaked"
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
