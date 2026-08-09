"""Per-session transient systemd scopes (#346 Phase B).

Every session master used to live inside the broker's own service cgroup, so one
runaway session's OOM kill or task-budget exhaustion was every session's problem.
This module wraps a LAUNCH argv (``dtach -c … <agent>``) in::

    systemd-run --user --scope --collect --quiet \
        --unit as-<engine>-<sid8>-<nonce>.scope \
        [-p Key=Value …] -- <argv…>

so the dtach master — and the whole agent process tree it forks — lands in its own
transient scope. ``--scope`` mode matters: systemd-run fork/execs the payload itself
(no manager round-trip for stdio), so the caller's PTY wiring, ``start_new_session``
controlling-tty setup and ``pass_fds`` lock-fd inheritance carry through unchanged.
``--collect`` garbage-collects the scope when its last process exits.

Only the LAUNCH path is wrapped. Viewer attaches (``dtach -a``) and headless
readers stay in the broker cgroup — they are cheap and die with their websocket.

Fallback ladder — never refuse a session because isolation is unavailable:
  * ``AGENT_SESSIONS_SESSION_SCOPES=0``  → passthrough, logged once as *disabled (config)*.
  * ``systemd-run`` missing / probe fails → passthrough, logged once as *unavailable (probe)*
    (re-probed after a cooldown so a transiently broken user manager can recover).
  * a scope creation that fails at spawn time surfaces as the process exiting
    immediately — the connection closes 4502 (retryable, #346 Phase A) and the next
    attempt re-runs the ladder.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import time

log = logging.getLogger("agent_sessions.scopedspawn")

SYSTEMD_RUN_BIN = os.environ.get("AGENT_SESSIONS_SYSTEMD_RUN_BIN") or "systemd-run"

# Re-probe cadence after a FAILED probe. A succeeding probe is cached for the process
# lifetime — a user manager that worked once practically never goes away, while one
# that is briefly absent (e.g. during login-session churn) deserves another look.
_REPROBE_AFTER_S = 300.0

# `-p` properties come from the environment; accept only a strict Key=Value shape so a
# weird env value can never smuggle extra argv tokens into the spawn.
_PROP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*=[A-Za-z0-9.%:_-]+$")

# Unit names share dtach's sanitizer intent: engine ids / uuids only ever contain safe
# chars, but never trust them blindly.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")

# Probe cache: None = not probed yet; (available, stamp) otherwise.
_probe_state: tuple[bool, float] | None = None
_logged_disabled = False
_logged_unavailable = False


def enabled() -> bool:
    """Scopes are on unless the operator opted out (default on where available)."""
    return os.environ.get("AGENT_SESSIONS_SESSION_SCOPES", "1") != "0"


def _probe() -> bool:
    """One real scope round-trip: the only reliable signal that the user manager is up,
    DBus is reachable and transient scopes are permitted. ~50-150 ms, run rarely."""
    binary = shutil.which(SYSTEMD_RUN_BIN)
    if binary is None:
        return False
    try:
        r = subprocess.run(  # noqa: S603
            [binary, "--user", "--scope", "--collect", "--quiet", "--", "/bin/true"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def available() -> bool:
    """Cached probe with failure-side cooldown (success is cached forever)."""
    global _probe_state
    now = time.monotonic()
    if _probe_state is not None:
        ok, stamp = _probe_state
        if ok or (now - stamp) < _REPROBE_AFTER_S:
            return ok
    ok = _probe()
    _probe_state = (ok, now)
    return ok


#: Per-session task budget. Well below the broker's own ``TasksMax=8192``, so the stated intent
#: — exhaust your OWN scope, never the host — still holds.
#:
#: Raised from 512 (#785). 512 was not a theoretical ceiling that ordinary work stays under: a
#: journal scan found **8 real sessions** hitting `cgroup: fork rejected by pids controller` in
#: five days, and one of them (2026-08-04 01:27:54) is the same second in which a `node` process
#: inside that scope sent SIGINT to `systemd --user`, which activated `exit.target` and tore down
#: every user unit on the host for 5h45m. The signal is the far more serious defect and is not
#: fixed here — a cgroup scope bounds resources, not signals, and closing that needs either
#: systemd ≥ 256 (`PrivatePIDs=`) or a dedicated UID (see the issue). But an agent that routinely
#: runs out of PIDs is the state from which that misfire was reached, and it is also just broken
#: for the user: `fork()` starts returning EAGAIN mid-task.
DEFAULT_TASKS_MAX = 2048


def _properties() -> list[str]:
    """Validated ``-p Key=Value`` pairs from AGENT_SESSIONS_SCOPE_PROPERTIES.

    Default budget: ``DEFAULT_TASKS_MAX``, well below the broker's own ceiling, so a
    fork bomb in one session exhausts its own scope, not the host. Memory properties
    are deliberately NOT defaulted — sessions legitimately run heavy builds; operators
    opt in (after probing controller delegation on staging, #346)."""
    raw = os.environ.get("AGENT_SESSIONS_SCOPE_PROPERTIES", f"TasksMax={DEFAULT_TASKS_MAX}")
    out: list[str] = []
    for token in raw.split():
        if _PROP_RE.match(token):
            out += ["-p", token]
        else:
            log.warning("scope property %r rejected (expected Key=Value); dropped", token)
    return out


def unit_name(engine: str, session_id: str) -> str:
    """``as-<engine>-<sid8>-<nonce>.scope`` — the nonce guarantees a kill → instant
    relaunch never collides with a predecessor scope that --collect hasn't GC'd yet."""
    e = _UNSAFE.sub("_", engine)[:16]
    s = _UNSAFE.sub("_", session_id)[:8]
    return f"as-{e}-{s}-{secrets.token_hex(4)}.scope"


def wrap(argv: list[str], *, engine: str, session_id: str) -> tuple[list[str], str | None]:
    """Wrap a LAUNCH argv in a transient scope when possible.

    Returns ``(argv, unit_name)`` when wrapped, ``(argv, None)`` on any fallback —
    the caller spawns whatever comes back and never needs to care which path won.
    """
    global _logged_disabled, _logged_unavailable
    if not enabled():
        if not _logged_disabled:
            _logged_disabled = True
            log.info("session scopes disabled (config: AGENT_SESSIONS_SESSION_SCOPES=0)")
        return argv, None
    if not available():
        if not _logged_unavailable:
            _logged_unavailable = True
            log.warning(
                "session scopes unavailable (probe: systemd-run --user not usable); "
                "sessions spawn unscoped in the broker cgroup"
            )
        return argv, None
    _logged_unavailable = False  # recovered — a later outage should log again
    unit = unit_name(engine, session_id)
    wrapped = [
        shutil.which(SYSTEMD_RUN_BIN) or SYSTEMD_RUN_BIN,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        f"--description=agent-sessions: {engine} session {session_id}",
        *_properties(),
        "--",
        *argv,
    ]
    return wrapped, unit


def reset_cache_for_tests() -> None:
    """Test hook: forget the probe result and one-shot log flags."""
    global _probe_state, _logged_disabled, _logged_unavailable
    _probe_state = None
    _logged_disabled = False
    _logged_unavailable = False


# ---- observability (#346 Phase C) — stateless discovery from the cgroup tree ------
#
# No wrap-time bookkeeping: the kernel already knows which scope a master landed in,
# and reading it back survives broker restarts and never goes stale.

CGROUP_ROOT = "/sys/fs/cgroup"
_PROC_ROOT = "/proc"  # overridable in tests


def scope_of(pid: int) -> str | None:
    """The ``as-…scope`` unit ``pid`` lives in, or None (unscoped / not Linux / gone).

    Reads ``/proc/<pid>/cgroup`` (v2: a single ``0::<path>`` line) and returns the
    unit only for scopes this module created — foreign scopes report as None so the
    listing can't mislabel e.g. a left-over manually-run unit as session isolation.
    """
    try:
        with open(f"{_PROC_ROOT}/{pid}/cgroup", encoding="utf-8") as fh:
            for line in fh:
                path = line.strip().rpartition(":")[2]
                leaf = path.rpartition("/")[2]
                if leaf.startswith("as-") and leaf.endswith(".scope"):
                    return leaf
    except OSError:
        pass
    return None


def _cgroup_path_of(pid: int) -> str | None:
    try:
        with open(f"{_PROC_ROOT}/{pid}/cgroup", encoding="utf-8") as fh:
            for line in fh:
                return line.strip().rpartition(":")[2]
    except OSError:
        pass
    return None


def scope_stats(pid: int) -> dict[str, int] | None:
    """``memory.current`` / ``pids.current`` of the scope ``pid`` lives in.

    Only meaningful when the pid is in one of our ``as-…scope`` units (the whole
    session tree shares that cgroup, so the numbers are the session's true footprint).
    Returns None when unscoped or the controllers aren't readable.
    """
    if scope_of(pid) is None:
        return None
    path = _cgroup_path_of(pid)
    if not path:
        return None
    out: dict[str, int] = {}
    for key, fname in (("memory_bytes", "memory.current"), ("tasks", "pids.current")):
        try:
            with open(f"{CGROUP_ROOT}{path}/{fname}", encoding="utf-8") as fh:
                out[key] = int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return out or None
