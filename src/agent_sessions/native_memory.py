"""Memory pressure of a native worker's own unit (#1643): warn before the kill, never guess.

A worker generation runs as a transient unit with `MemoryMax` (the operator's API memory limit)
and `MemoryHigh` at 90% of it. That cap covers the worker, the agent and every tool the agent
starts, so a long autonomous turn can reach it with the host far from full. The kill then ends the
generation without warning. This module lets the worker watch its own cgroup and say so first.

* **Only its own cgroup.** The worker resolves the group from `/proc/self/cgroup` and accepts it
  only when its last component is its own unit and it equals the `control_group` the runtime
  pinned. No browser input, and no server-built path, reaches a cgroup file.
* **Working set, not page cache.** `memory.current − inactive_file` (`memory.stat`), so reclaimable
  cache never false-alarms. `memory.events:high` counting up means the kernel is throttling at
  `MemoryHigh`, which sits at 90% of the cap: that is the near-kill band, so it reads `critical`.
* **The tightest limit.** The worker's own cap is the operator's setting, but a parent slice may
  cap lower; every ancestor that sets `memory.max` is read too, and a warning from one says
  `scope: shared` — a limit set outside BattleLab, which the Resource limits setting cannot raise.
* **Levels with hysteresis.** `high` at 75% of the cap, `critical` at 90% or on throttling. A
  level rises at once and falls only after three consecutive samples below its threshold minus
  five points, so it never flaps.
* **Unknown is not healthy.** An unreadable group, an unpinned or foreign path or an unlimited
  cap is `unavailable`: no warning, and any warning already shown clears.

* **Kills, counted (#1732).** Under `OOMPolicy=continue` an overrun kills one process, not the
  generation. The worker counts them from its own unit's hierarchical `memory.events:oom_kill`
  (`oom_kills`) and journals each rise as a bounded `memory_oom_kill` event; no process is named.

The worker records the state in its own lifecycle sidecar. `public()` is the one reader both the
pane and the session list use: a warning is shown only while it is fresh (its worker keeps
sampling) and belongs to the pinned invocation, so a dead or replaced generation's warning expires
on its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
INTERVAL = 10.0
HIGH = 0.75
CRITICAL = 0.90
FALL_MARGIN = 0.05
FALL_SAMPLES = 3
FRESH = 3 * INTERVAL + 5.0  # three missed samples: the worker is gone or stuck
LEVELS = ("normal", "high", "critical")
SHOWN = frozenset({"high", "critical"})
LIVE_PHASES = frozenset({"starting", "native_started", "ready"})
_MAX_READ = 64 * 1024


@dataclass(frozen=True)
class Group:
    """One group whose limit applies: the worker's own unit or an ancestor that sets one."""

    path: str
    working_set: int
    maximum: int
    throttled: int  # this group's OWN limit's `high` events: a cumulative counter
    own: bool = False

    @property
    def ratio(self) -> float:
        return self.working_set / self.maximum


@dataclass(frozen=True)
class Sample:
    """Every group whose limit applies to the worker, its own unit first."""

    groups: tuple[Group, ...]

    @property
    def tightest(self) -> Group:
        return max(self.groups, key=lambda g: g.ratio)  # the first (own) wins a tie

    # The tightest group's figures: what the usage reads against.
    @property
    def working_set(self) -> int:
        return self.tightest.working_set

    @property
    def maximum(self) -> int:
        return self.tightest.maximum

    @property
    def scope(self) -> str:
        return "session" if self.tightest.own else "shared"

    @property
    def source(self) -> str:
        return self.tightest.path


def own_group(proc_cgroup: str, unit: str) -> str | None:
    """The cgroup v2 path in `/proc/self/cgroup` (`0::/…`), if it is this worker's own unit."""
    for line in proc_cgroup.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "" and path.startswith("/"):
            parts = path.rstrip("/").split("/")
            return path if parts[-1] == unit and ".." not in parts else None
    return None


def _read(path: Path) -> str | None:
    try:
        with path.open() as stream:
            text = stream.read(_MAX_READ + 1)
    except (OSError, UnicodeError):
        return None
    return text if len(text) <= _MAX_READ else None


def _number(text: str | None) -> int | None:
    text = None if text is None else text.strip()
    return int(text) if text and text.isdigit() and len(text) <= 20 else None


def _field(text: str | None, name: str) -> int | None:
    for line in (text or "").splitlines():
        key, _, value = line.partition(" ")
        if key == name:
            return _number(value)
    return None


MAX_DEPTH = 64  # a defensive bound on the ancestor walk; reaching it reads unavailable


def _usage(directory: Path) -> int | None:
    current = _number(_read(directory / "memory.current"))
    inactive = _field(_read(directory / "memory.stat"), "inactive_file")
    if current is None or inactive is None:
        return None
    return max(0, current - inactive)


def _throttled(directory: Path, *, own: bool) -> int | None:
    """`high` events of THIS group's own limit (`memory.events.local`, #1651): the hierarchical
    `memory.events` of an ancestor also counts siblings' throttling, which says nothing about this
    session. The worker's own unit holds only this session, so there the hierarchical file is an
    acceptable fallback on a kernel without `.local`; an ancestor's never is."""
    local = _field(_read(directory / "memory.events.local"), "high")
    if local is not None or not own:
        return local
    return _field(_read(directory / "memory.events"), "high")


def oom_kills(group: str, root: Path | None = None) -> int | None:
    """How many processes the kernel has OOM-killed in the worker's own unit (#1732): `oom_kill`
    of its HIERARCHICAL `memory.events`, so a kill in a nested child group counts too (`.local`
    would miss it). Cumulative over the unit's life; None when unreadable."""
    base = CGROUP_ROOT if root is None else root
    return _field(_read(base / group.lstrip("/") / "memory.events"), "oom_kill")


def sample(group: str, root: Path | None = None) -> Sample | None:
    """One reading of every group whose limit applies: the worker's own `memory.max` and every
    ancestor's that sets one (a parent slice capped below the operator's setting would otherwise
    read healthy until it kills). None if the own group is unreadable or unlimited, or an ancestor
    sets a limit whose usage cannot be read: an incomplete reading is never a healthy one. An
    ancestor without a memory controller or limit is skipped.

    Each group carries its OWN limit's throttle counter (Hermes, #1651): a sibling's throttling
    at a looser parent never counts, and the session's own throttling still counts while a busier
    parent has the higher ratio. An ancestor without `memory.events.local` contributes no throttle
    evidence (its hierarchical counter would be a sibling's)."""
    base = CGROUP_ROOT if root is None else root
    directory = base / group.lstrip("/")
    maximum = _number(_read(directory / "memory.max"))
    usage = _usage(directory)
    throttled = _throttled(directory, own=True)
    if usage is None or not maximum or throttled is None:
        return None
    groups = [Group(str(directory), usage, maximum, throttled, own=True)]
    cursor = directory.parent
    for depth in range(MAX_DEPTH + 1):
        if cursor == base or not cursor.is_relative_to(base):
            break
        if depth == MAX_DEPTH:
            # Deeper than any real hierarchy: an ancestor not read may cap tighter. Unknown, never
            # a complete-looking reading (Hermes on #1651).
            return None
        cap = _number(_read(cursor / "memory.max"))
        if cap:
            used = _usage(cursor)
            if used is None:
                return None
            groups.append(Group(str(cursor), used, cap, _throttled(cursor, own=False) or 0))
        cursor = cursor.parent
    return Sample(tuple(groups))


def _level(ratio: float, throttling: bool, margin: float = 0.0) -> str:
    """The level for this reading; `margin` lowers both thresholds (the bar for staying up)."""
    if throttling or ratio >= CRITICAL - margin:
        return "critical"
    if ratio >= HIGH - margin:
        return "high"
    return "normal"


class Tracker:
    """The level over successive samples: up at once, down only after FALL_SAMPLES quiet ones.

    `shown` is the group whose figures the warning states: always the tightest (closest to its
    hard cap: the most urgent). `throttled` says, separately, whose own limit was seen throttling
    this sample: "session", "shared" or None (Hermes on #1651: neither may hide the other)."""

    def __init__(self) -> None:
        self.level = "unavailable"
        self.shown: Group | None = None
        self.throttled: str | None = None
        self._counts: dict[str, int] | None = None
        self._quiet = 0

    def update(self, reading: Sample | None) -> str:
        if reading is None:
            self.level, self.shown, self._counts, self._quiet = "unavailable", None, None, 0
            self.throttled = None
            return self.level
        # A rise of a group's OWN counter since that same group's last reading. A group seen for
        # the first time is a fresh baseline, not throttling.
        before = self._counts or {}
        rising = [g for g in reading.groups if g.path in before and g.throttled > before[g.path]]
        self._counts = {g.path: g.throttled for g in reading.groups}
        throttling = bool(rising)
        tight = reading.tightest
        self.shown = tight
        self.throttled = "session" if any(g.own for g in rising) else "shared" if rising else None
        ratio = tight.ratio
        now = _level(ratio, throttling)
        if self.level == "unavailable" or LEVELS.index(now) >= LEVELS.index(self.level):
            self.level, self._quiet = now, 0
            return self.level
        # Lower than shown: only once it is clearly lower, three samples running.
        settled = _level(ratio, throttling, FALL_MARGIN)
        if LEVELS.index(settled) < LEVELS.index(self.level):
            self._quiet += 1
            if self._quiet >= FALL_SAMPLES:
                self.level, self._quiet = settled, 0
        else:
            self._quiet = 0
        return self.level


def state(
    level: str, shown: Group | None, invocation_id: str | None, throttled: str | None = None
) -> dict:
    """What the worker writes to its lifecycle sidecar as `memory`: the level, the figures of
    the most urgent group (`Tracker.shown`), and whose own limit was seen throttling, if any —
    a claim of throttling is never made without that evidence."""
    return {
        "level": level,
        "throttled": throttled if throttled in ("session", "shared") else None,
        "working_set": None if shown is None else shown.working_set,
        "max": None if shown is None else shown.maximum,
        "scope": None if shown is None else "session" if shown.own else "shared",
        "at": time.time(),
        "invocation_id": invocation_id,
    }


def public(life: dict, *, now: float | None = None) -> dict | None:
    """The warning a reader may show for this lifecycle, or None: only `high` / `critical`, only
    while fresh, only for the pinned invocation, with whole byte counts."""
    memory = life.get("memory")
    if not isinstance(memory, dict) or memory.get("level") not in SHOWN:
        return None
    if life.get("phase") not in LIVE_PHASES or "exit" in life:
        return None  # it exited (or systemd recorded its end): nothing left to warn about
    invocation = life.get("invocation_id")
    at, used, cap = memory.get("at"), memory.get("working_set"), memory.get("max")
    now = time.time() if now is None else now
    if (
        not invocation
        or memory.get("invocation_id") != invocation
        or not isinstance(at, int | float)
        or not 0 <= now - at <= FRESH
        or not isinstance(used, int)
        or not isinstance(cap, int)
        or cap <= 0
    ):
        return None
    scope = "shared" if memory.get("scope") == "shared" else "session"
    throttled = (
        memory.get("throttled") if memory.get("throttled") in ("session", "shared") else None
    )
    return {
        "level": memory["level"],
        "working_set": used,
        "max": cap,
        "scope": scope,
        "throttled": throttled,
    }
