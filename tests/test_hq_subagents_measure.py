"""Measured before merge (#2398): 50 concurrent `wait` calls with 4 keys each against a real server,
plus one task holding 8 queued spawns that retry capacity, and the `driver` fact stamped on a
1,000-session listing. Opt-in (``AGENT_SESSIONS_MEASURE=1``): it starts a real uvicorn server on
loopback and real native workers, and prints the numbers the PR reports. The targets are asserted:
stop-to-return < 4 s, each unrelated endpoint's p95 < 200 ms, and ONE capacity check per tick for
the task with the queue (never one per queued entry)."""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid

import httpx
import pytest

from agent_sessions import ask_orchestrator, hq_subagents, session_capacity, session_driver
from agent_sessions import structured_runtime as runtime
from drive_harness import new_session
from test_hq_drive_measure import _p95, _Server
from test_native_runtime import host, ident, project, settle  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_MEASURE") != "1", reason="measurement: AGENT_SESSIONS_MEASURE=1"
)

WAITS = 50
KEYS_PER_WAIT = 4
SESSIONS = 10  # half idle (HANG), half streaming (STREAM)
QUEUED = 8


def _turn(keys: set[str]) -> ask_orchestrator._Turn:
    t = ask_orchestrator._Turn(
        cid="c" + uuid.uuid4().hex[:31],
        turn_id=str(uuid.uuid4()),
        question="q",
        working_keys=set(),
        progress=lambda ev: None,
    )
    t.keys = set(keys)
    return t


def _stats(xs: list[float]) -> tuple:
    return (round(statistics.median(xs), 1), round(_p95(xs), 1), round(max(xs), 1), len(xs))


@pytest.mark.timeout(1200)
def test_measure_fifty_multi_key_waits_and_a_queue(host, project, auth_cfg, capsys, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    keys = [new_session(project) for _ in range(SESSIONS)]
    for i, key in enumerate(keys):
        tid = ident()
        asyncio.run(runtime.submit_turn(key, operation_id=tid, text="HANG" if i % 2 else "STREAM"))
        asyncio.run(settle(key, tid, state=("running",)))

    # The limit stays full: the queued spawns keep retrying capacity for the whole run.
    def full(*, origin=session_capacity.OTHER):
        raise session_capacity.SessionLimitReached(6, 6)

    monkeypatch.setattr(session_capacity, "check", full)
    app = create_app(auth_cfg)
    tc = TestClient(app, base_url="https://testserver")
    r = tc.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    cookie = "; ".join(f"{k}={v}" for k, v in tc.cookies.items())
    hq_subagents.reset_for_test()
    with _Server(app) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        turns: list = []

        async def one(ks: list[str]):
            turn = _turn(set(ks))
            turns.append(turn)
            try:
                out, _ = await ask_orchestrator._stoppable(
                    turn, ask_orchestrator._wait(turn, {"keys": ks, "mode": "all", "seconds": 110})
                )
            except ask_orchestrator._Stopped:
                out = {"settled": False, "stopped": True}
            return out

        async def queue():
            turn = _turn(set())
            t = hq_subagents.task(turn.cid, turn.turn_id)
            names = []
            for _ in range(QUEUED):
                h = t.new(entry={}, card={}, title="p", agent="a")
                h.outcome = hq_subagents.QUEUED
                h.deadline = asyncio.get_running_loop().time() + hq_subagents.SPAWN_QUEUE_MAX_S
                names.append(h.name)
            turns.append(turn)
            try:
                out, _ = await ask_orchestrator._stoppable(
                    turn, ask_orchestrator._wait(turn, {"keys": names, "seconds": 110})
                )
            except ask_orchestrator._Stopped:
                out = {"settled": False, "stopped": True}
            return out

        def sample(seconds: float) -> dict[str, list[float]]:
            lat: dict[str, list[float]] = {"/healthz": [], "/api/sessions?limit=1": []}
            with httpx.Client(base_url=base, headers={"Cookie": cookie}, timeout=30) as c:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    for path in lat:
                        t0 = time.monotonic()
                        resp = c.get(path)
                        lat[path].append((time.monotonic() - t0) * 1000)
                        assert resp.status_code == 200, (path, resp.status_code)
                    time.sleep(0.05)
            return lat

        sample(3.0)
        baseline = sample(20.0)
        futures = [
            asyncio.run_coroutine_threadsafe(
                one([keys[(i + j) % SESSIONS] for j in range(KEYS_PER_WAIT)]), srv.loop
            )
            for i in range(WAITS)
        ]
        futures.append(asyncio.run_coroutine_threadsafe(queue(), srv.loop))
        time.sleep(2.0)
        checks0 = hq_subagents.STATS["capacity_checks"]
        t_window = time.monotonic()
        lat = sample(20.0)
        window = time.monotonic() - t_window
        checks = hq_subagents.STATS["capacity_checks"] - checks0
        assert not any(f.done() for f in futures), "a wait returned before its deadline"
        stopped_at = time.monotonic()
        for turn in turns:
            srv.loop.call_soon_threadsafe(
                lambda t=turn: (t.stop.set(), t.inflight.cancel() if t.inflight else None)
            )
        latencies = []
        for f in futures:
            out = f.result(timeout=30)
            latencies.append(time.monotonic() - stopped_at)
            assert out["settled"] is False
        stop_latency = max(latencies)
    report = {
        "workload": f"{WAITS} waits x {KEYS_PER_WAIT} keys over {SESSIONS} sessions "
        f"({SESSIONS // 2} HANG, {SESSIONS // 2} STREAM) + 1 task with {QUEUED} queued spawns",
        "stop_latency_s_max": round(stop_latency, 2),
        "capacity_checks_in_window": checks,
        "capacity_checks_per_s": round(checks / window, 2),
        **{f"{p} p50/p95/max ms (n) during waits": _stats(xs) for p, xs in lat.items()},
        **{f"{p} p50/p95/max ms (n) baseline": _stats(xs) for p, xs in baseline.items()},
        "loadavg": os.getloadavg(),
    }
    with capsys.disabled():
        print("\nMEASURE wait (B2):", report)
    assert stop_latency < 4.0, report
    for p, xs in lat.items():
        assert _p95(xs) < 200.0, (p, report)
    # ONE check per tick for the task, whatever its 8 entries: at most ~1 per WAIT_TICK_S.
    assert checks <= window / hq_subagents.WAIT_TICK_S + 2, report


@pytest.mark.timeout(600)
def test_measure_the_driver_fact_on_a_thousand_rows(capsys):
    """`GET /api/sessions` stamps `driver` from ONE store read per request: 1,000 records, 1,000
    rows (the full set is stamped before paging)."""
    from agent_sessions.atomicjson import atomic_write_json

    keys = [f"codex-api:{ident()}" for _ in range(1000)]
    doc = {
        "sessions": {
            k: {
                "who": "hq",
                "cid": "c" * 32,
                "generation": 1,
                "since": 0.0,
                "title": "t",
                "task": {"turn_id": str(uuid.uuid4()), "n": 1 + i % 9},
            }  # fmt: skip
            for i, k in enumerate(keys)
        }
    }
    atomic_write_json(session_driver.path(), doc)
    rows = [{"id": k, "mission": None} for k in keys]
    runs = []
    for _ in range(20):
        t0 = time.monotonic()
        recs = session_driver.records()
        for r in rows:
            r["driver"] = session_driver.row_fact(recs.get(r["id"]), bool(r.get("mission")))
        runs.append((time.monotonic() - t0) * 1000)
    assert rows[0]["driver"]["who"] == "hq"
    out = {"stamp 1,000 rows p50/max ms": (round(statistics.median(runs), 2), round(max(runs), 2))}
    with capsys.disabled():
        print("\nMEASURE driver stamp (1,000 records):", out)
