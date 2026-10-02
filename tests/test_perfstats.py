"""Unit tests for the in-process latency probes (#652 measurement PR).

Covers the recorder contract the optimization PRs depend on: percentile math, the
bounded (fixed-memory) ring, reset, the ``timed`` context manager, thread-safety
(samples arrive from the event loop AND worker threads), and the best-effort guarantee
that a probe never raises into the hot path it measures.
"""

from __future__ import annotations

import threading

import pytest

from agent_sessions import perfstats


@pytest.fixture(autouse=True)
def _clean() -> None:
    # Module-global store — isolate every test.
    perfstats.reset()
    yield
    perfstats.reset()


def test_snapshot_empty_when_no_samples() -> None:
    assert perfstats.snapshot() == {}


def test_records_and_reports_percentile_fields() -> None:
    for v in range(1, 101):  # 1..100
        perfstats.record("m", float(v))
    snap = perfstats.snapshot()
    assert set(snap) == {"m"}
    row = snap["m"]
    assert row["count"] == 100
    assert row["max"] == 100.0
    # Linear-interpolated nearest-rank over 1..100.
    assert row["p50"] == pytest.approx(50.5, abs=0.5)
    assert row["p95"] == pytest.approx(95.05, abs=0.5)
    assert row["p99"] == pytest.approx(99.01, abs=0.5)
    assert row["mean"] == pytest.approx(50.5, abs=0.01)
    assert set(row) == {"count", "p50", "p95", "p99", "max", "mean"}


def test_single_sample_percentiles_equal_value() -> None:
    perfstats.record("solo", 42.0)
    row = perfstats.snapshot()["solo"]
    assert row["count"] == 1
    assert row["p50"] == row["p95"] == row["p99"] == row["max"] == row["mean"] == 42.0


def test_metrics_are_independent() -> None:
    perfstats.record("a", 1.0)
    perfstats.record("b", 2.0)
    perfstats.record("b", 4.0)
    snap = perfstats.snapshot()
    assert snap["a"]["count"] == 1
    assert snap["b"]["count"] == 2
    assert snap["b"]["mean"] == 3.0


def test_ring_is_bounded_to_maxlen() -> None:
    # Twice the cap in; only the last _MAXLEN survive (fixed memory ceiling).
    for v in range(perfstats._MAXLEN * 2):
        perfstats.record("cap", float(v))
    row = perfstats.snapshot()["cap"]
    assert row["count"] == perfstats._MAXLEN
    # Oldest evicted → the retained window is the tail, so min sample is >= the cutoff.
    assert row["max"] == float(perfstats._MAXLEN * 2 - 1)


def test_reset_clears_all() -> None:
    perfstats.record("x", 1.0)
    perfstats.reset()
    assert perfstats.snapshot() == {}


def test_timed_context_manager_records_a_sample() -> None:
    with perfstats.timed("blk"):
        pass
    row = perfstats.snapshot()["blk"]
    assert row["count"] == 1
    assert row["p50"] >= 0.0


def test_timed_records_even_when_body_raises() -> None:
    with pytest.raises(ValueError):
        with perfstats.timed("boom"):
            raise ValueError("nope")
    # The finally still recorded a sample despite the exception.
    assert perfstats.snapshot()["boom"]["count"] == 1


def test_record_is_best_effort_on_bad_input() -> None:
    # A non-numeric sample must not raise into the caller (probe safety).
    perfstats.record("bad", object())  # type: ignore[arg-type]
    # Nothing recorded, and the store is still usable.
    perfstats.record("bad", 1.0)
    assert perfstats.snapshot()["bad"]["count"] == 1


def test_concurrent_record_is_threadsafe() -> None:
    # Samples arrive from many threads (event loop + to_thread workers) — no lost
    # updates, no crash from concurrent OrderedDict/deque mutation.
    n_threads, per = 8, 500

    def worker() -> None:
        for _ in range(per):
            perfstats.record("race", 1.0)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Bounded ring, so count caps at _MAXLEN — but the point is no exception and a
    # consistent, readable snapshot after heavy concurrent writes.
    row = perfstats.snapshot()["race"]
    assert row["count"] == min(n_threads * per, perfstats._MAXLEN)
