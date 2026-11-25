"""In-process latency probes (#652 measurement PR).

A tiny, dependency-free recorder for hot-path timings so each optimization PR in the
performance umbrella (#652) can report a concrete before/after p50/p95/p99 instead of
"feels faster". It records into a fixed-size ring of recent samples per metric — no
persistence, no unbounded growth, no external dependency.

Thread-safety is load-bearing here: samples are recorded from BOTH the asyncio event
loop (the terminal attach path) AND worker threads (``/api/sessions`` runs its scan in
``asyncio.to_thread``). A single lock guards the sample store; :func:`snapshot` copies
each ring under the lock and sorts/computes percentiles OUTSIDE it, so building a
snapshot never blocks the recording hot path. Recording is best-effort — it never
raises into the caller's hot path (a probe must not be able to break a session).

This is a measurement scaffold, not a feature: the endpoint is admin-gated
(``GET /api/perf``) and there is no user-facing surface.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Iterator

# Recent samples per metric. ``deque(maxlen=...)`` gives O(1) append and evicts the
# oldest sample automatically — a fixed memory ceiling regardless of traffic (2048
# floats per metric ≈ tens of KB total). Percentiles over "the last N samples" is
# exactly the recent-window view we want for before/after comparisons.
_MAXLEN = 2048

_lock = threading.Lock()
_samples: dict[str, deque[float]] = {}


def record(metric: str, ms: float) -> None:
    """Append one timing sample (in milliseconds) for ``metric``. Never raises.

    The only operation that can fail on the caller's hot path is coercing a bad sample
    to ``float`` — guarded here so a probe can never take down the path it measures. The
    lock + bounded-``deque`` append below cannot raise under normal operation.
    """
    try:
        sample = float(ms)
    except (TypeError, ValueError):
        return
    with _lock:
        dq = _samples.get(metric)
        if dq is None:
            dq = deque(maxlen=_MAXLEN)
            _samples[metric] = dq
        dq.append(sample)


@contextlib.contextmanager
def timed(metric: str) -> Iterator[None]:
    """Context manager that records the wall-clock duration of the block as ``metric``.

    Brackets ``time.monotonic()`` around the block, so it is correct even when the body
    ``await``\\ s (it measures elapsed real time, not CPU). Records in a ``finally`` so a
    partial/raising block still yields a sample.
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        record(metric, (time.monotonic() - t0) * 1000.0)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (``p`` in [0, 100]) over a PRE-SORTED list."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    k = (p / 100.0) * (n - 1)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def snapshot() -> dict[str, dict[str, float | int]]:
    """``{metric: {count, p50, p95, p99, max, mean}}`` over the retained window.

    Values are milliseconds, rounded to 2 dp. Each ring is copied under the lock, then
    sorted and reduced outside it so a large snapshot never stalls recording.
    """
    with _lock:
        copied = {m: list(dq) for m, dq in _samples.items()}
    out: dict[str, dict[str, float | int]] = {}
    for metric, vals in copied.items():
        if not vals:
            continue
        s = sorted(vals)
        out[metric] = {
            "count": len(s),
            "p50": round(_percentile(s, 50), 2),
            "p95": round(_percentile(s, 95), 2),
            "p99": round(_percentile(s, 99), 2),
            "max": round(s[-1], 2),
            "mean": round(sum(s) / len(s), 2),
        }
    return out


def reset() -> None:
    """Drop every retained sample. Baseline capture is reset → run load → snapshot."""
    with _lock:
        _samples.clear()
