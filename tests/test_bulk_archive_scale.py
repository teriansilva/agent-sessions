"""Measured scale of a bulk archive (#2396): 100 and 1,000 sessions, some of them slow to stop and
some refused by a mission reservation, while an unrelated lightweight request keeps being served.

Opt-in (``AGENT_SESSIONS_MEASURE=1``): it times a real server on a real socket, so it is a
measurement for the PR, not a CI gate. It prints one line per size and asserts the declared budget:
an unrelated request's p95 stays under 200 ms during the sweep.
"""

from __future__ import annotations

import contextlib
import os
import socket
import statistics
import threading
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_MEASURE") != "1", reason="measurement; AGENT_SESSIONS_MEASURE=1"
)

P95_BUDGET_S = 0.200
SLOW_EVERY, SLOW_S = 10, 0.05  # every 10th member takes 50 ms more to stop
REFUSED_EVERY = 25  # every 25th member is held by a mission


def _sessions(home, n: int) -> None:
    root = home / ".claude" / "projects" / "-home-user-claude-scale"
    root.mkdir(parents=True)
    old = time.time() - 30 * 86400
    for i in range(n):
        f = root / f"{i:08d}-0000-4000-8000-000000000000.jsonl"
        f.write_text('{"type":"user","cwd":"/home/user/claude/scale","message":{"content":"x"}}\n')
        os.utime(f, (old, old))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("n", [100, 1000])
def test_bulk_archive_scale(n, auth_cfg, tmp_home, monkeypatch):
    import uvicorn

    from agent_sessions import missions, runtime_freeze, transcript_owner
    from agent_sessions.main import create_app

    monkeypatch.setattr(transcript_owner, "transcript_is_owned", lambda *a, **k: False)
    _sessions(tmp_home, n)

    real_frozen = runtime_freeze.frozen
    real_reserve = missions.reserve_session
    seen = {"n": 0}

    @contextlib.asynccontextmanager
    async def frozen(prov, native, **kw):
        import asyncio

        seen["n"] += 1
        if seen["n"] % SLOW_EVERY == 0:
            await asyncio.sleep(SLOW_S)
        async with real_frozen(prov, native, **kw):
            yield

    def reserve(key, *a, **k):
        if int(key.split(":")[1][:8]) % REFUSED_EVERY == 0:
            raise missions.MissionError("held by a mission")
        return real_reserve(key, *a, **k)

    monkeypatch.setattr(runtime_freeze, "frozen", frozen)
    monkeypatch.setattr(missions, "reserve_session", reserve)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(auth_cfg), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            time.sleep(0.05)
        # The app's cookie is Secure; the client talks plain HTTP to loopback, so carry it by hand.
        c = httpx.Client(base_url=base, timeout=600)
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        )
        cookie = r.headers["set-cookie"].split(";", 1)[0]
        cfg = c.get("/api/config", headers={"Cookie": cookie}).json()
        hdr = {"Cookie": cookie, "X-CSRF-Token": cfg["csrf"], "Origin": auth_cfg.origin}

        lat: list[float] = []
        done = threading.Event()

        def probe() -> None:
            with httpx.Client(base_url=base, timeout=10) as p:
                while not done.is_set():
                    t0 = time.perf_counter()
                    p.get("/healthz")
                    p.get("/api/sessions?limit=1", headers={"Cookie": cookie})
                    lat.append((time.perf_counter() - t0) / 2)
                    time.sleep(0.02)

        pt = threading.Thread(target=probe, daemon=True)
        t0 = time.perf_counter()
        scan0 = time.perf_counter()
        from agent_sessions.routes.sessions import archive_older_candidates

        cands = archive_older_candidates(time.time() - 3600)
        scan = time.perf_counter() - scan0
        pt.start()
        t0 = time.perf_counter()
        r = c.post("/api/sessions/archive-older", json={"hours": 1}, headers=hdr)
        total = time.perf_counter() - t0
        done.set()
        pt.join(timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        p95 = statistics.quantiles(lat, n=20)[-1] if len(lat) >= 20 else max(lat)
        print(
            f"\nSCALE n={n} candidates={len(cands)} scan={scan * 1000:.0f}ms "
            f"sweep={total:.2f}s archived={body['archived']} skipped={body['skipped']} "
            f"probe_samples={len(lat)} probe_p50={statistics.median(lat) * 1000:.0f}ms "
            f"probe_p95={p95 * 1000:.0f}ms probe_max={max(lat) * 1000:.0f}ms"
        )
        assert body["archived"] + body["skipped"] == n
        assert p95 < P95_BUDGET_S
    finally:
        server.should_exit = True
        t.join(timeout=10)
