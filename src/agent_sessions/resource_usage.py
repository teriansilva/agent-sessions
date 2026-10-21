"""Bounded, read-only cgroup task observations. No process spawning or history scans.

Leaf limits and shared ancestors are observed separately. Counts include threads;
denial counters are cumulative for the current group lifetime, never incident deltas.
An incomplete observation cannot establish healthy headroom.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import scopedspawn

CGROUP_ROOT = Path("/sys/fs/cgroup")
MAX_ENTRIES = 2048
MAX_GROUPS = 512
_UNIT = re.compile(r"(?:as-[A-Za-z0-9_.-]+\.scope|battlelab-native-[0-9a-f]{32}\.service)")


def _read(path: Path) -> str | None:
    try:
        with path.open() as stream:
            value = stream.read(513)
        return value.strip() if len(value) <= 512 else None
    except (OSError, UnicodeError):
        return None


def _number(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,20}", value) else None


def _counters(path: Path) -> dict:
    current = _number(_read(path / "pids.current"))
    raw_max = _read(path / "pids.max")
    maximum = _number(raw_max)
    events = _read(path / "pids.events")
    denied = None
    if events is not None:
        for line in events.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "max":
                denied = _number(parts[1])
    return {
        "group": path.name,
        "current": current,
        "maximum": maximum,
        "unlimited": raw_max == "max",
        "denied": denied,
    }


def observe(path: Path, *, cache: dict | None = None) -> dict:
    cache = {} if cache is None else cache
    levels = []
    cursor = path
    while cursor != CGROUP_ROOT and cursor.is_relative_to(CGROUP_ROOT):
        if cursor not in cache:
            cache[cursor] = _counters(cursor)
        levels.append(cache[cursor])
        cursor = cursor.parent
    unknown = any(
        x["current"] is None or (x["maximum"] is None and not x["unlimited"]) for x in levels
    )
    finite = [x for x in levels if x["current"] is not None and x["maximum"] is not None]
    pressure = max(
        (x["current"] / x["maximum"] if x["maximum"] else 1.0 for x in finite), default=None
    )
    severity = (
        "critical"
        if pressure is not None and pressure >= 0.95
        else "warning"
        if pressure is not None and pressure >= 0.8
        else "unknown"
        if unknown
        else "normal"
    )
    headroom = min((max(0, x["maximum"] - x["current"]) for x in finite), default=None)
    return {
        "unit": path.name,
        "kind": "api" if path.name.startswith("battlelab-native-") else "console",
        "own": levels[0],
        "ancestors": levels[1:],
        "severity": severity,
        "pressure": pressure,
        "headroom": None if unknown else headroom,
        "incomplete": unknown or any(x["denied"] is None for x in levels),
    }


def collect() -> dict:
    root = (
        CGROUP_ROOT
        / "user.slice"
        / f"user-{os.getuid()}.slice"
        / f"user@{os.getuid()}.service"
        / "app.slice"
    )
    rows = []
    cache = {}
    truncated = False
    error = None
    try:
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES or len(rows) >= MAX_GROUPS:
                    truncated = True
                    break
                if _UNIT.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    rows.append(observe(Path(entry.path), cache=cache))
    except OSError:
        error = "Running resource readings are unavailable on this host."
    # A real probe runs on launch; polling Settings must still work near task exhaustion.
    probe = scopedspawn._probe_state
    console = (
        "disabled"
        if not scopedspawn.enabled()
        else "unverified"
        if probe is None
        else "verified"
        if probe[0]
        else "unavailable"
    )
    rank = {"critical": 0, "warning": 1, "unknown": 2, "normal": 3}
    rows.sort(key=lambda row: (rank[row["severity"]], row["unit"]))
    return {
        "observed_at": time.time(),
        "console_containment": console,
        "groups": rows,
        "truncated": truncated,
        "error": error,
    }
