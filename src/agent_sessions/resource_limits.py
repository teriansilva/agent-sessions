"""Finite, next-launch resource policy (#1362); preferences never resize live groups.

The console's legacy property environment remains supported until a task limit is
explicitly saved. Library settings are defaults, not containment: inherited explicit
values win. Only the fixed library names below cross the native worker's env boundary.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from . import prefs, sysinfo

DEFAULTS = {"console_tasks": 4096, "api_tasks": 4096, "library_threads": 8, "api_memory_gib": 8}
BOUNDS = {
    "console_tasks": (256, 16384),
    "api_tasks": (256, 16384),
    "library_threads": (1, 64),
    "api_memory_gib": (1, 1024),
    "max_sessions": (sysinfo.MIN_SESSIONS, sysinfo.MAX_SESSIONS),
}
THREAD_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "TOKIO_WORKER_THREADS",
)
_BLOCK = "resource_limits"
_PROPERTY = re.compile(r"^[A-Za-z][A-Za-z0-9]*=[A-Za-z0-9.%:_-]+$")


def valid(key: str, value: object) -> bool:
    low, high = BOUNDS[key]
    return type(value) is int and low <= value <= high


def recommended() -> dict[str, int]:
    """The fixed defaults, plus the one value that is read off this host (#1819)."""
    return DEFAULTS | {"max_sessions": sysinfo.session_capacity()["recommended"]}


def saved() -> dict[str, int]:
    block = prefs.read_block(_BLOCK)
    if not isinstance(block, dict):
        return {}
    return {key: block[key] for key in BOUNDS if valid(key, block.get(key))}


def values() -> dict[str, int]:
    return recommended() | saved()


def max_sessions() -> int:
    """The limit in force. Unsaved, it follows the recommendation; saved, it stays."""
    stored = saved().get("max_sessions")
    return stored if stored is not None else sysinfo.session_capacity()["recommended"]


def save(patch: object) -> None:
    if not isinstance(patch, dict) or not patch or patch.keys() - BOUNDS.keys():
        raise ValueError(
            "expected console_tasks, api_tasks, library_threads, api_memory_gib or max_sessions"
        )
    for key, value in patch.items():
        if not valid(key, value):
            low, high = BOUNDS[key]
            raise ValueError(f"{key} must be an integer from {low} to {high}")

    def merge(old):
        clean = {
            k: v
            for k, v in (old.items() if isinstance(old, dict) else ())
            if k in BOUNDS and valid(k, v)
        }
        merged = clean | patch
        # Saving the recommendation means following it (#1819), so "Restore recommended" unpins
        # the limit instead of freezing today's number when the host later changes.
        if merged.get("max_sessions") == suggested:
            del merged["max_sessions"]
        return merged

    suggested = sysinfo.session_capacity()["recommended"]
    prefs.mutate_block(_BLOCK, merge)


def _valid_tasks(value: str) -> bool:
    if value == "infinity":
        return True  # existing operator override; the Settings API never accepts it
    if re.fullmatch(r"[0-9]{1,20}", value):
        return 0 < int(value) < 2**64
    if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,6})?%", value):
        return 0 < float(value[:-1]) <= 100
    return False


def console_policy(stored: dict[str, int] | None = None) -> dict:
    stored = saved() if stored is None else stored
    properties = []
    legacy = None
    rejected = False
    for token in os.environ.get("AGENT_SESSIONS_SCOPE_PROPERTIES", "").split():
        if not _PROPERTY.fullmatch(token):
            rejected = True
            continue
        key, value = token.split("=", 1)
        if key == "TasksMax":
            if _valid_tasks(value):
                legacy = value
            else:
                rejected = True
        else:
            properties.append(token)
    limit = str(stored.get("console_tasks", legacy or DEFAULTS["console_tasks"]))
    source = "settings" if "console_tasks" in stored else "environment" if legacy else "recommended"
    return {
        "tasks_max": limit,
        "source": source,
        "properties": [*properties, f"TasksMax={limit}"],
        "rejected": rejected,
    }


def thread_environment(
    environment: Mapping[str, str], default: int | None = None
) -> dict[str, str]:
    """Fixed names only, honoring explicit operator/provider values (including empty)."""
    if default is None:
        default = values()["library_threads"]
    if not valid("library_threads", default):
        raise ValueError("invalid library thread default")
    return {key: environment.get(key, str(default)) for key in THREAD_VARIABLES}


def settings() -> dict:
    stored = saved()
    capacity = sysinfo.session_capacity()
    suggested = DEFAULTS | {"max_sessions": capacity["recommended"]}
    configured = suggested | stored
    console = console_policy(stored)
    # A representable legacy value makes the untouched form reflect the actual next launch.
    if console["source"] == "environment" and console["tasks_max"].isdigit():
        legacy = int(console["tasks_max"])
        if valid("console_tasks", legacy):
            configured["console_tasks"] = legacy
    block = prefs.read_block(_BLOCK)
    invalid = block is not None and (
        not isinstance(block, dict)
        or any(k not in BOUNDS or not valid(k, v) for k, v in block.items())
    )
    return {
        "values": configured,
        "recommended": suggested,
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in BOUNDS.items()},
        "sources": {k: "settings" if k in stored else "recommended" for k in BOUNDS}
        | {"console_tasks": console["source"]},
        "console_tasks_max": console["tasks_max"],
        "capacity": capacity,
        "library_overrides": [
            {
                "name": k,
                "value": os.environ[k]
                if re.fullmatch(r"[0-9,]{1,32}", os.environ[k])
                else "external value",
            }
            for k in THREAD_VARIABLES
            if k in os.environ
        ],
        "notice": (
            "Invalid saved or legacy values were ignored; "
            "valid values and finite defaults remain in use."
        )
        if invalid or console["rejected"]
        else None,
    }
