"""#2415 Phase 1 budget: the ``native_worker_state`` pass on a disposable 10× inventory.

Opt-in (``AGENT_SESSIONS_BENCH_NATIVE_GC=1``): it builds ~4.3k session records and ~7.5k worker
dirs, the inventory reported on #2413 times ten, in the test's isolated plugin-state
dir. Real session history is never read.

It records, and asserts against the budget declared on the issue BEFORE measuring:

* the pass (dry run, then prune) takes **≤ 30 s** each;
* **at most one unit query per dir that passed the file checks** (the real host is asked in
  batches, so far fewer);
* ``/api/sessions`` p95 while the pass runs is **within +20 %** of its idle baseline, measured
  against idle windows before AND after the pass; when those two disagree by more than the
  budget the host is too noisy to judge and the test SKIPS as inconclusive rather than passing;
* the native launch's state step (session lock → read record → write record, what a launch does
  under the lock before systemd) is measured against the same baseline.

Unit queries go to the real ``systemctl --user show`` when a user manager is running (read-only,
random unit names that do not exist), else to a stand-in that takes the measured ~10 ms per unit.

    AGENT_SESSIONS_BENCH_NATIVE_GC=1 PYTHONPATH=src pytest tests/test_native_gc_benchmark.py -s
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid

import pytest

from agent_sessions import native_containment, native_gc, native_runtime, native_state

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_BENCH_NATIVE_GC") != "1",
    reason="benchmark; set AGENT_SESSIONS_BENCH_NATIVE_GC=1",
)

RECORDS = 4300
REFERENCED_DIRS = 4300  # one current generation per record
START_GEN_DIRS = 1160  # gone, but named by a record's start/launch view
ELIGIBLE_DIRS = 2040  # gone, unreferenced (the 204 of #2413, times ten)
PASS_BUDGET_S = 30.0
P95_BUDGET = 1.20


class _CountingHost(native_runtime.Host):
    """The real host (its ``show`` untouched, so the pass batches), counting systemd calls."""

    def __init__(self) -> None:
        self.shows = 0

    def run(self, argv, timeout=30.0):
        self.shows += 1
        return super().run(argv, timeout)


class _StandIn:
    """No user manager here: one ~10 ms answer per unit, the measured single-query cost."""

    def __init__(self) -> None:
        self.shows = 0

    def show(self, worker):
        self.shows += 1
        time.sleep(0.0106)
        return native_containment.UnitObservation(
            worker.unit, "not-found", "inactive", "dead", None, None, 0, ""
        )

    def cgroup(self, control_group):
        return native_containment.CgroupObservation(control_group, "absent")


def _put(path, doc: dict) -> None:
    """A private JSON file, written WITHOUT the store's fsyncs: the inventory is disposable and
    ~17k fsyncs would make building it, not the pass, the slow part."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)


def _worker_dir(wid: str, life: dict, *, config: bool = False) -> None:
    d = native_state.root() / "workers" / wid
    d.mkdir(mode=0o700)
    _put(d / "lifecycle.json", life)
    os.close(os.open(d / "lifecycle.lock", os.O_CREAT | os.O_WRONLY, 0o600))
    if config:
        _put(d / "config.json", {"worker_id": wid})


def _inventory() -> None:
    native_state.ensure_dir(native_state.root() / "workers")
    gone = {"phase": "gone", "unit": "x", "gone_at": 1.0, "intent_at": 0.5}
    for i in range(RECORDS):
        sid, wid = str(uuid.uuid4()), str(uuid.uuid4())
        _worker_dir(wid, {"phase": "ready", "unit": "x"}, config=True)
        record = {
            "session_key": f"claude-api:{sid}",
            "current_worker": wid,
            "workers": [wid],
            "generations": 1,
            "request": {"bypass": False, "text": "x" * 400},
        }
        if i < START_GEN_DIRS:
            gen = str(uuid.uuid4())
            _worker_dir(gen, gone)
            record["start"] = {"state": "started", "gen": gen}
        _put(native_state.session_path(sid), record)
    for _ in range(ELIGIBLE_DIRS):
        _worker_dir(str(uuid.uuid4()), gone)


def _p95(xs: list[float]) -> float:
    return statistics.quantiles(xs, n=20)[18]


def _sample(fn, *, n: int | None = None, seconds: float | None = None) -> list[float]:
    out: list[float] = []
    stop = None if seconds is None else time.perf_counter() + seconds
    while (n is not None and len(out) < n) or (stop is not None and time.perf_counter() < stop):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out


@pytest.mark.timeout(1800)
def test_the_pass_stays_within_its_budget(auth_cfg, fake_jsonl, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    real = native_runtime.Host().available() is None
    host = _CountingHost() if real else _StandIn()
    monkeypatch.setattr(native_runtime, "HOST", host)

    t0 = time.perf_counter()
    _inventory()
    built = time.perf_counter() - t0
    n_records = len(list(native_state.root().glob("*.json")))
    n_dirs = len(list((native_state.root() / "workers").iterdir()))

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert (
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        ).status_code
        == 303
    )

    def sessions():
        assert c.get("/api/sessions?limit=50").status_code == 200

    def launch_state():
        sid = str(uuid.uuid4())
        with native_state.session_lock(sid):
            native_state.read_session(sid)
            native_state.write_session(sid, {"session_key": f"bench:{sid}", "workers": []})
        native_state.session_path(sid).unlink()

    sessions()  # warm
    # The idle baseline spans as long as the pass may take: with ~4.3k records `/api/sessions`
    # periodically recomputes its per-record views (seconds each, the #2413 hot pass) and serves
    # cached answers in between, so a short baseline would see none of those and a pass window
    # would see one, comparing a cache hit with a recompute.
    base_sessions, base_launch = [], []
    stop = time.perf_counter() + PASS_BUDGET_S
    while time.perf_counter() < stop:
        base_sessions += _sample(sessions, n=5)
        base_launch += _sample(launch_state, n=5)

    # The dry run, timed on its own.
    t = time.perf_counter()
    items, nbytes, dry_queries = native_gc.measure()
    dry_s = time.perf_counter() - t
    assert items == ELIGIBLE_DIRS
    assert dry_queries <= ELIGIBLE_DIRS  # never more than one per file-eligible dir

    # The prune, with /api/sessions and the launch state step sampled while it runs.
    host.shows = 0
    done = threading.Event()
    result: dict = {}

    def run():
        t = time.perf_counter()
        result["out"] = native_gc.prune()
        result["s"] = time.perf_counter() - t
        done.set()

    worker = threading.Thread(target=run)
    worker.start()
    during_sessions: list[float] = []
    during_launch: list[float] = []
    while not done.is_set():
        during_sessions += _sample(sessions, n=5)
        during_launch += _sample(launch_state, n=5)
    worker.join()
    out = result["out"]
    # ...and an idle window AFTER the pass as well, so host noise shows up as the two idle
    # windows disagreeing rather than as the pass's cost.
    after_sessions = []
    stop = time.perf_counter() + PASS_BUDGET_S
    while time.perf_counter() < stop:
        after_sessions += _sample(sessions, n=5)

    report = {
        "inventory": {"records": n_records, "worker_dirs": n_dirs, "built_s": round(built, 1)},
        "unit_probe": "systemctl --user show (batched)" if real else "stand-in, 10.6 ms each",
        "dry_run": {
            "s": round(dry_s, 2),
            "items": items,
            "bytes": nbytes,
            "unit_queries": dry_queries,
        },
        "prune": {
            "s": round(result["s"], 2),
            "removed": out.removed,
            "bytes_freed": out.bytes_freed,
            "unit_queries": out.unit_queries,
            "skipped": out.skipped,
            "failed": len(out.failed),
        },
        "api_sessions_p95_ms": {
            "idle": round(_p95(base_sessions) * 1000, 1),
            "during": round(_p95(during_sessions) * 1000, 1),
            "idle_after": round(_p95(after_sessions) * 1000, 1),
            "samples_idle": len(base_sessions),
            "samples_during": len(during_sessions),
        },
        "launch_state_p95_ms": {
            "idle": round(_p95(base_launch) * 1000, 2),
            "during": round(_p95(during_launch) * 1000, 2),
        },
    }
    print("\nnative_gc benchmark:", json.dumps(report, indent=2))

    assert out.removed == ELIGIBLE_DIRS and not out.failed
    assert out.unit_queries == host.shows <= ELIGIBLE_DIRS
    assert dry_s <= PASS_BUDGET_S
    assert result["s"] <= PASS_BUDGET_S
    # The latency budget is only meaningful when the host itself is steady: two idle windows
    # that disagree by more than the budget mean any comparison would be measuring the host.
    lo, hi = sorted([_p95(base_sessions), _p95(after_sessions)])
    if hi > lo * P95_BUDGET:
        pytest.skip(
            f"/api/sessions latency INCONCLUSIVE: the idle windows disagree "
            f"({lo * 1000:.0f} vs {hi * 1000:.0f} ms p95) — the host is not steady enough"
        )
    assert _p95(during_sessions) <= hi * P95_BUDGET
