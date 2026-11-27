"""Measured before merge (#2397): 50 concurrent `wait_session` calls against a real server, and the
driver batch `list_sessions` reads with 1,000 sessions. Opt-in (``AGENT_SESSIONS_MEASURE=1``): it
starts a real uvicorn server on loopback and real native workers, and prints the numbers the PR
reports. The targets are asserted too: stop latency < 4 s, each unrelated endpoint's p95 < 200 ms.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import os
import socket
import statistics
import threading
import time

import httpx
import pytest

from agent_sessions import ask_orchestrator, missions, session_driver
from agent_sessions import structured_runtime as runtime
from drive_harness import new_session
from test_native_runtime import host, ident, project, settle  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_MEASURE") != "1", reason="measurement: AGENT_SESSIONS_MEASURE=1"
)

WAITS = 50
SESSIONS = 10  # half idle, half streaming; five waits on each


def _p95(xs: list[float]) -> float:
    return statistics.quantiles(xs, n=20)[-1] if len(xs) >= 20 else max(xs)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """The app under uvicorn on loopback, on its own loop in a thread."""

    def __init__(self, app):
        import uvicorn

        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, lifespan="off", log_level="error")
        )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise AssertionError("the server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=20)


@pytest.mark.timeout(1200)
def test_measure_fifty_concurrent_waits(host, project, auth_cfg, capsys):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    keys = [new_session(project) for _ in range(SESSIONS)]
    for i, key in enumerate(keys):
        text = "HANG" if i % 2 == 0 else "STREAM"
        tid = ident()
        asyncio.run(runtime.submit_turn(key, operation_id=tid, text=text))
        asyncio.run(settle(key, tid, state=("running",)))
    app = create_app(auth_cfg)
    # The cookie is Secure: carried by hand to the loopback server.
    tc = TestClient(app, base_url="https://testserver")
    r = tc.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    cookie = "; ".join(f"{k}={v}" for k, v in tc.cookies.items())
    with _Server(app) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        turns: list = []

        async def one(key: str):
            turn = ask_orchestrator._Turn.__new__(ask_orchestrator._Turn)
            turn.stop = asyncio.Event()
            turn.keys = {key}
            turn.cid = "c" * 32
            turn.inflight = None
            turns.append(turn)
            began = time.monotonic()
            # As the tool runs: a read Stop abandons (`_stoppable`), whose flag the wait also
            # checks between its polls.
            try:
                out, _ = await ask_orchestrator._stoppable(
                    turn, ask_orchestrator._wait_session(turn, {"key": key, "seconds": 110})
                )
            except ask_orchestrator._Stopped:
                out = {"settled": False, "stopped": True}
            return out, time.monotonic() - began

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

        # The same server and sessions with no wait running: the host's own baseline.
        sample(3.0)  # warm
        baseline = sample(20.0)
        futures = [
            asyncio.run_coroutine_threadsafe(one(keys[i % SESSIONS]), srv.loop)
            for i in range(WAITS)
        ]
        time.sleep(2.0)  # every wait is waiting
        lat = sample(20.0)
        assert not any(f.done() for f in futures), "a wait returned before its deadline"
        stopped_at = time.monotonic()
        for turn in turns:
            srv.loop.call_soon_threadsafe(
                lambda t=turn: (t.stop.set(), t.inflight.cancel() if t.inflight else None)
            )
        latencies = []
        for f in futures:
            out, _took = f.result(timeout=30)
            latencies.append(time.monotonic() - stopped_at)
            assert out["settled"] is False
        stop_latency = max(latencies)
    report = {
        "waits": WAITS,
        "sessions": f"{SESSIONS} ({SESSIONS // 2} idle HANG, {SESSIONS // 2} streaming STREAM)",
        "stop_latency_s_max": round(stop_latency, 2),
        **{
            f"{p} p50/p95/max ms (n) during waits": (
                round(statistics.median(xs), 1),
                round(_p95(xs), 1),
                round(max(xs), 1),
                len(xs),
            )
            for p, xs in lat.items()
        },
        **{
            f"{p} p50/p95/max ms (n) baseline": (
                round(statistics.median(xs), 1),
                round(_p95(xs), 1),
                round(max(xs), 1),
                len(xs),
            )
            for p, xs in baseline.items()
        },
        "loadavg": os.getloadavg(),
    }
    with capsys.disabled():
        print("\nMEASURE wait_session:", report)
    assert stop_latency < 4.0, report
    for p, xs in lat.items():
        assert _p95(xs) < 200.0, (p, report)


@pytest.mark.timeout(600)
def test_measure_the_driver_batch_with_a_thousand_sessions(tmp_path, monkeypatch, capsys):
    """`list_sessions` reads its page's drivers in one batch: one store read, one membership
    read. Measured with a store of 1,000 records, for a page of 30 and for all 1,000 keys."""
    keys = [f"codex-api:{ident()}" for _ in range(1000)]
    doc = {
        "sessions": {
            k: {"who": "hq", "cid": "c" * 32, "generation": 1, "since": 0.0, "title": "t"}
            for k in keys
        }
    }
    from agent_sessions.atomicjson import atomic_write_json

    atomic_write_json(session_driver.path(), doc)
    reads = {"missions": 0}
    real = missions.all_active_memberships

    def members(**kw):
        reads["missions"] += 1
        return real(**kw)

    monkeypatch.setattr(missions, "all_active_memberships", members)
    out = {}
    for n in (30, 1000):
        runs = []
        for _ in range(20):
            t0 = time.monotonic()
            views = session_driver.views(keys[:n])
            runs.append((time.monotonic() - t0) * 1000)
        assert len(views) == n
        out[f"views({n}) p50/max ms"] = (round(statistics.median(runs), 2), round(max(runs), 2))
    assert reads["missions"] == 40  # one per call, never one per row
    with capsys.disabled():
        print("\nMEASURE driver batch (1,000 records):", out)
