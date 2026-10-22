"""Live host telemetry for the NIGHTJAR footer (#1531): CPU %, RAM, and HQ's token use.

Host CPU and RAM are already shown to the logged-in operator (Settings → System → Host,
``sysinfo``); this is the same information, live. Served only behind the login
(``GET /api/telemetry``) and never on a pre-auth page.

**CPU is sampled lazily and cached.** One ``/proc/stat`` read yields cumulative jiffies; the
percentage is the busy share of the delta since the previous read. A read is taken at most every
``MIN_INTERVAL`` seconds, so any number of open tabs polling every few seconds costs one tiny file
read per interval. The first request after start takes two reads ``FIRST_GAP`` apart. Linux only:
elsewhere CPU is ``None`` and the footer says so.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from . import hq_usage, sysinfo

#: Seconds a CPU sample is reused before /proc/stat is read again.
MIN_INTERVAL = 2.0
#: The gap between the two reads of the very first sample.
FIRST_GAP = 0.25

_lock = threading.Lock()
_prev: tuple[int, int] | None = None  # (total, idle) jiffies at the last read
_last: tuple[float, float | None] = (0.0, None)  # (monotonic time, pct) of the cached sample


def _read_stat() -> tuple[int, int]:
    """(total, idle) jiffies from the aggregate ``cpu`` line of /proc/stat."""
    with open("/proc/stat", encoding="ascii") as fh:
        first = fh.readline().split()
    if not first or first[0] != "cpu":
        raise OSError("no aggregate cpu line")
    vals = [int(x) for x in first[1:]]
    # user nice system idle iowait irq softirq steal [guest guest_nice — already in user/nice]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals[:8])
    return total, idle


def _pct(prev: tuple[int, int], cur: tuple[int, int]) -> float | None:
    dt = cur[0] - prev[0]
    if dt <= 0:
        return None
    busy = dt - (cur[1] - prev[1])
    return round(max(0.0, min(100.0, 100.0 * busy / dt)), 1)


async def cpu_pct() -> float | None:
    """The host's CPU busy %, from a sample at most ``MIN_INTERVAL`` old; None off Linux."""
    global _prev, _last
    now = time.monotonic()
    with _lock:
        at, pct = _last
        if _prev is not None and now - at < MIN_INTERVAL:
            return pct
        first = _prev is None
    try:
        if first:
            a = _read_stat()
            await asyncio.sleep(FIRST_GAP)
            b = _read_stat()
            with _lock:
                _prev, _last = b, (time.monotonic(), _pct(a, b))
                return _last[1]
        cur = _read_stat()
    except (OSError, ValueError, IndexError):
        return None
    with _lock:
        pct = _pct(_prev, cur) if _prev else None
        _prev, _last = cur, (time.monotonic(), pct)
        return pct


def memory() -> dict[str, Any] | None:
    """Used / total bytes and the used %, from ``MemTotal - MemAvailable``; None off Linux."""
    try:
        mem = sysinfo._meminfo()
    except (OSError, ValueError):
        return None
    total, avail = mem.get("mem_total"), mem.get("mem_available")
    if not total or avail is None:
        return None
    used = max(0, total - avail)
    return {"used": used, "total": total, "pct": round(100.0 * used / total, 1)}


async def collect() -> dict[str, Any]:
    return {
        "cpu_pct": await cpu_pct(),
        "mem": memory(),
        "hq": hq_usage.snapshot(),
        "as_of": time.time(),
    }


def _reset_for_tests() -> None:
    global _prev, _last
    with _lock:
        _prev, _last = None, (0.0, None)
