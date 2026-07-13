"""Host/system info for the Settings → System section (discovery, #109 follow-up).

Stdlib only (no new deps). Every field is **fail-soft**: collected in its own
try/except so a missing ``/proc`` (non-Linux), a permission error, or an unreadable
file omits that one field rather than failing the whole endpoint. ``/proc``-backed
fields (mem/uptime) are simply absent off Linux. Deliberately **no** network
interfaces / IP addresses — the system card is host capacity, not topology.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
from pathlib import Path
from typing import Any

from .version import get_version


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
