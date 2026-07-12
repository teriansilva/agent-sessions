"""#561: the ``/api/sessions`` scan-snapshot cache + off-loop scan.

Phase 2 — ``list_sessions`` runs the blocking scan off the event loop.
Phase 3 — a short TTL + single-flight cache around ``engines.scan_all()`` keyed on the effective
home, invalidated by the mutating routes.

The autouse ``_isolate_scan_cache`` fixture (conftest) sets the TTL to 0 for the rest of the suite;
these tests opt back in with an explicit ``set_scan_cache_ttl`` and restore it via the fixture's
teardown.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from agent_sessions import engines
from agent_sessions.main import create_app


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _counting_scan():
    """A stub ``scan_all`` that counts its calls and returns an empty session list."""
    calls = {"n": 0}

    def scan():
        calls["n"] += 1
        return []

    return scan, calls


# ---- Phase 3: TTL cache primitives --------------------------------------------


def test_default_ttl_is_poll_friendly():
    """#652 L1: the DEFAULT TTL must stay well above the old 1.5 s so the scan snapshot is warm
    across the ~15 s poll window — otherwise a search-settle / project-switch between polls lands
    on a cold walk of the whole live+archive tree. The autouse fixture forces the runtime TTL to
    0, so assert the source default (a regression here silently reintroduces the cold-walk cost).
    Freshness is unaffected: every mutating route still calls ``invalidate_scan_cache`` — proven by
    the invalidate/archive/new-session tests below."""
    import inspect
    import re

    from agent_sessions.engines import registry

    m = re.search(r"^_SCAN_CACHE_TTL_S\s*=\s*([0-9.]+)", inspect.getsource(registry), re.M)
    assert m is not None and float(m.group(1)) >= 10.0


def test_second_call_within_ttl_does_no_disk_walk(monkeypatch):
    """Two calls within the TTL trigger exactly one real scan (the burst-collapse win)."""
    scan, calls = _counting_scan()
    monkeypatch.setattr(engines, "scan_all", scan)
    engines.set_scan_cache_ttl(30.0)

    engines.scan_all_cached()
    engines.scan_all_cached()
    engines.scan_all_cached()
    assert calls["n"] == 1


def test_invalidate_forces_a_fresh_walk(monkeypatch):
    scan, calls = _counting_scan()
    monkeypatch.setattr(engines, "scan_all", scan)
    engines.set_scan_cache_ttl(30.0)

    engines.scan_all_cached()
    assert calls["n"] == 1
    engines.invalidate_scan_cache()
    engines.scan_all_cached()
    assert calls["n"] == 2  # invalidation dropped the snapshot → re-walked


def test_ttl_zero_disables_the_cache(monkeypatch):
    scan, calls = _counting_scan()
    monkeypatch.setattr(engines, "scan_all", scan)
    engines.set_scan_cache_ttl(0.0)

    engines.scan_all_cached()
    engines.scan_all_cached()
    assert calls["n"] == 2  # no memoisation when disabled


def test_distinct_homes_do_not_share_a_snapshot(monkeypatch, tmp_path):
    """The cache keys on ``Path.home()`` — a different home must never serve another home's scan
    (the ``mktemp -d`` per-test-home isolation Hermes flagged)."""
    scan, calls = _counting_scan()
    monkeypatch.setattr(engines, "scan_all", scan)
    engines.set_scan_cache_ttl(30.0)

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    monkeypatch.setenv("HOME", str(home_a))
    engines.scan_all_cached()
    engines.scan_all_cached()
    assert calls["n"] == 1  # home A cached

    monkeypatch.setenv("HOME", str(home_b))
    engines.scan_all_cached()
    assert calls["n"] == 2  # home B is a distinct key → its own walk

    monkeypatch.setenv("HOME", str(home_a))
    engines.scan_all_cached()
    assert calls["n"] == 2  # home A's snapshot is still valid within the TTL


def test_concurrent_calls_single_flight(monkeypatch):
    """Two concurrent misses within the TTL must produce at most ONE real scan, not two cold ones
    racing (Hermes: the concurrency regression)."""
    calls = {"n": 0}
    started = threading.Event()

    def slow_scan():
        calls["n"] += 1
        started.set()
        # Hold the (single-flight) lock long enough that the second thread is guaranteed to arrive
        # while the first scan is still in flight.
        threading.Event().wait(0.2)
        return []

    monkeypatch.setattr(engines, "scan_all", slow_scan)
    engines.set_scan_cache_ttl(30.0)

    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        engines.scan_all_cached()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert calls["n"] == 1


# ---- Phase 3: the cache through the route + invalidation on archive -----------


def test_route_reuses_the_snapshot_and_archive_invalidates(auth_cfg, fake_jsonl, monkeypatch):
    real_scan = engines.scan_all
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return real_scan()

    monkeypatch.setattr(engines, "scan_all", counting)
    engines.set_scan_cache_ttl(30.0)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    first = c.get("/api/sessions?limit=50")
    assert first.status_code == 200
    ids = [s["id"] for s in first.json()["sessions"]]
    assert ids  # the fixture seeds live sessions
    # Count only the session-list reads from here (the /api/config bootstrap scans on its own,
    # uncached — it's not on the hot search path). The first GET above already populated the cache.
    calls["n"] = 0
    c.get("/api/sessions?limit=50")  # within the TTL → served from the snapshot
    c.get("/api/sessions?limit=50&offset=0")
    assert calls["n"] == 0  # both reads shared the existing snapshot — no disk walk

    # Archiving moves a Claude JSONL → the route must bust the snapshot so the next list re-walks.
    r = c.post(f"/api/sessions/{ids[0]}/archive", headers=hdr)
    assert r.status_code == 200
    c.get("/api/sessions?limit=50")
    assert calls["n"] == 1  # invalidation forced exactly one fresh walk


def test_reconcile_new_session_invalidates_scan_cache(monkeypatch, tmp_home):
    """A mint-its-own-id (reconciling) new session — opencode/codex/antigravity — becomes
    discoverable only after the durable alias write in ``_reconcile_new_session``, NOT at launch. So
    the reconcile path must bust the scan cache too, or the sidebar serves a stale pre-reconcile
    snapshot for up to the TTL and the just-created row is missing right after the client converges
    (Hermes review of #565). The pinned-id path (claude) is covered by the archive/launch
    invalidation tests; this covers the reconciling seam."""
    from agent_sessions import main

    scan, calls = _counting_scan()
    monkeypatch.setattr(engines, "scan_all", scan)
    engines.set_scan_cache_ttl(30.0)
    engines.scan_all_cached()  # populate the snapshot
    assert calls["n"] == 1

    class FakeProv:
        engine_id = "opencode"

        def reconcile_new_session(self, cwd, snapshot):
            return "ses_real123"  # a real id appears on the first poll

    class FakeWS:
        async def send_text(self, s):
            return None

    # Don't actually sleep 0.5s per poll, write a sidecar, or wake the review loop.
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    monkeypatch.setattr(main.metadata, "set_alias", lambda a, b: None)
    monkeypatch.setattr(main.ai_review_loop, "request_review_soon", lambda: None)

    asyncio.run(
        main._reconcile_new_session(FakeWS(), FakeProv(), "new-abcd", "/home/u/proj", set())
    )

    # Reconcile persisted the alias → it must have invalidated the cache, so the next list re-walks.
    engines.scan_all_cached()
    assert calls["n"] == 2


# ---- Phase 2: the scan runs off the event loop --------------------------------


def test_list_sessions_scans_off_the_event_loop(auth_cfg, fake_jsonl, monkeypatch):
    """The blocking scan must run in a worker thread, not on the event loop — proven by the scan
    observing NO running asyncio loop (``get_running_loop`` raises off-loop)."""
    off_loop = {"seen": None}

    def fake_cached():
        try:
            asyncio.get_running_loop()
            off_loop["seen"] = False  # ran ON the event loop → Phase 2 not in effect
        except RuntimeError:
            off_loop["seen"] = True  # ran in a worker thread → correct
        return []

    monkeypatch.setattr(engines, "scan_all_cached", fake_cached)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions")
    assert r.status_code == 200
    assert off_loop["seen"] is True
