"""Host/system info for the Settings → System section (discovery, #109 follow-up).

Stdlib only (no new deps). Every field is **fail-soft**: collected in its own
try/except so a missing ``/proc`` (non-Linux), a permission error, or an unreadable
file omits that one field rather than failing the whole endpoint. ``/proc``-backed
fields (mem/uptime) are simply absent off Linux. Deliberately **no** network
interfaces / IP addresses — the system card is host capacity, not topology.
"""

from __future__ import annotations

import math
import os
import platform
import shutil
import socket
from pathlib import Path
from typing import Any

from .version import get_version

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")

# The running-session recommendation (#1819). Measured on a live fleet of 66 sessions (2026-10):
# a session's working set had a median of 0.40 GiB, a mean of 0.67 and a p90 of 1.3, and together
# they kept a load of 4 to 8, about 8 sessions per busy CPU. These plan for a little tool work and
# some room.
SESSION_GIB = 1
SESSIONS_PER_CPU = 6
RESERVED_MIN_GIB = 2
RESERVED_SHARE = 0.10
FALLBACK_SESSIONS = 8  # neither memory nor CPUs could be read
MIN_SESSIONS, MAX_SESSIONS = 1, 256
_GIB = 1024**3


def _meminfo() -> dict[str, int]:
    """``MemTotal`` / ``MemAvailable`` in bytes from ``/proc/meminfo`` (Linux only)."""
    out: dict[str, int] = {}
    want = {"MemTotal": "mem_total", "MemAvailable": "mem_available"}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key in want:
                # value is in kB, e.g. "MemTotal:       16384256 kB"
                kb = int(rest.strip().split()[0])
                out[want[key]] = kb * 1024
    return out


def _uptime_seconds() -> float:
    """Seconds since boot from ``/proc/uptime`` (Linux only)."""
    with open("/proc/uptime", encoding="ascii") as fh:
        return float(fh.read().split()[0])


def collect() -> dict[str, Any]:
    """Best-effort host info. Each field guarded independently; failures omit the key."""
    info: dict[str, Any] = {}

    def _try(key: str, fn) -> None:
        try:
            val = fn()
        except Exception:  # noqa: BLE001 — fail-soft: any error just omits the field
            return
        if val is not None:
            info[key] = val

    _try("os", lambda: f"{platform.system()} {platform.release()}".strip())
    _try("platform", platform.platform)
    _try("arch", platform.machine)
    _try("python", platform.python_version)
    _try("version", get_version)
    _try("hostname", socket.gethostname)
    _try("cpus", os.cpu_count)

    def _load() -> dict[str, float] | None:
        # os.getloadavg() is absent on Windows and raises OSError if unsupported.
        if not hasattr(os, "getloadavg"):
            return None
        one, five, fifteen = os.getloadavg()
        return {"1": one, "5": five, "15": fifteen}

    _try("load", _load)

    def _mem() -> dict[str, int] | None:
        mem = _meminfo()
        return mem or None

    _try("_mem", _mem)
    if "_mem" in info:
        mem = info.pop("_mem")
        info.update(mem)

    def _disk() -> dict[str, int]:
        usage = shutil.disk_usage(Path.home())
        return {"disk_total": usage.total, "disk_free": usage.free}

    _try("_disk", _disk)
    if "_disk" in info:
        info.update(info.pop("_disk"))

    _try("uptime_seconds", _uptime_seconds)

    return info


def _shared_cgroups() -> list[Path]:
    """The cgroups this process shares with the sessions it starts, innermost first.

    A systemd unit's own group is left out: sessions run beside the app's ``.service``, under the
    same parents, so its limit doesn't bound them. Anything else (a container's scope, a namespaced
    root) holds the sessions too."""
    with PROC_CGROUP.open(encoding="ascii") as fh:
        for line in fh:
            if line.startswith("0::"):
                own = Path(line[3:].strip().lstrip("/"))
                break
        else:
            return []
    parents = [CGROUP_ROOT / parent for parent in own.parents]  # "a/b/c" → a/b, a, "."
    return parents if own.name.endswith(".service") else [CGROUP_ROOT / own, *parents]


def _cgroup_limits() -> tuple[int | None, int | None]:
    """The tightest ``(memory.max bytes, cpu.max rounded up)`` the shared cgroups set, or None."""
    memory: int | None = None
    cpus: int | None = None
    try:
        groups = _shared_cgroups()
    except (OSError, ValueError):
        return None, None
    # A cgroup v1 container shows its limit at this fixed path (unlimited reads as a huge number).
    memory_files = [CGROUP_ROOT / "memory" / "memory.limit_in_bytes"]
    memory_files += [group / "memory.max" for group in groups]
    for path in memory_files:
        try:
            raw = path.read_text(encoding="ascii").strip()
            if raw.isdigit():
                memory = int(raw) if memory is None else min(memory, int(raw))
        except (OSError, ValueError):
            pass
    for group in groups:
        try:
            quota, _, period = (group / "cpu.max").read_text(encoding="ascii").partition(" ")
            if quota.isdigit() and int(period) > 0:
                allowed = max(1, math.ceil(int(quota) / int(period)))
                cpus = allowed if cpus is None else min(cpus, allowed)
        except (OSError, ValueError):
            pass
    return memory, cpus


def _host_memory() -> int | None:
    try:
        return _meminfo().get("mem_total") or None
    except (OSError, ValueError, IndexError):
        return None


def _host_cpus() -> int | None:
    try:
        return len(os.sched_getaffinity(0)) or None
    except (AttributeError, OSError):
        return os.cpu_count()


def session_recommendation(memory_bytes: int | None, cpus: int | None) -> dict[str, Any]:
    """How many running sessions these resources carry, and which of them sets it. Pure."""
    by_memory = by_cpu = reserved = None
    if memory_bytes is not None:
        reserved = max(RESERVED_MIN_GIB * _GIB, int(memory_bytes * RESERVED_SHARE))
        by_memory = max(0, memory_bytes - reserved) // (SESSION_GIB * _GIB)
    if cpus is not None:
        by_cpu = cpus * SESSIONS_PER_CPU
    if by_memory is None and by_cpu is None:
        limited_by, wanted = "unknown", FALLBACK_SESSIONS
    elif by_cpu is None or (by_memory is not None and by_memory <= by_cpu):
        limited_by, wanted = "memory", by_memory
    else:
        limited_by, wanted = "cpu", by_cpu
    return {
        "memory_bytes": memory_bytes,
        "cpus": cpus,
        "reserved_bytes": reserved,
        "by_memory": by_memory,
        "by_cpu": by_cpu,
        "limited_by": limited_by,
        "recommended": max(MIN_SESSIONS, min(MAX_SESSIONS, wanted)),
    }


def session_capacity() -> dict[str, Any]:
    """This host's recommendation, from its hardware and any container limit on it (#1819).

    Total memory rather than available, so the value does not move with the load."""
    memory, cpus = _host_memory(), _host_cpus()
    cgroup_memory, cgroup_cpus = _cgroup_limits()
    if cgroup_memory is not None:
        memory = cgroup_memory if memory is None else min(memory, cgroup_memory)
    if cgroup_cpus is not None:
        cpus = cgroup_cpus if cpus is None else min(cpus, cgroup_cpus)
    return session_recommendation(memory, cpus)
