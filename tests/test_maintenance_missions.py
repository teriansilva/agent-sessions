"""#993 increment 1 — "Archive old missions": the bulk sweep over ``mission_archive``.

The sweep adds no archival of its own. What is pinned is that it only ever asks
``archive_mission(abandon=False)`` about TERMINAL, unarchived, old-enough missions; that its
fences' answers are reported honestly (an unresolved turn is a skip, a session that could not be
archived is a failure while its mission still archives); that the side effect on live terminals
is counted rather than hidden; and that it runs on the app's own loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import archive as archive_mod
from agent_sessions import maintenance, metadata, mission_archive, missions
from agent_sessions.main import create_app

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
KEY = {u: f"claude:{u}" for u in (A, B, C)}
DAY = 86400.0


class FakeSession:
    def __init__(self, uuid, archived=False):
        self.uuid = uuid
        self.archived = archived


class FakeProvider:
    engine_id = "claude"

    def __init__(self):
        self.rows = {}

    def add(self, native):
        self.rows[native] = FakeSession(native)

    def scan(self):
        return list(self.rows.values())

    def archive(self, native):
        row = self.rows.get(native)
        if row is None or row.archived:
            raise archive_mod.ArchiveError(f"session {native} not found to archive")
        row.archived = True
        metadata.patch(f"claude:{native}", archived=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "metadata.json"))
    missions.reset_schema_cache_for_test()
    prov = FakeProvider()
    for u in (A, B, C):
        prov.add(u)

    def _parse(key, **kw):
        engine, _, native = key.partition(":")
        if engine != "claude" or native not in prov.rows:
            from agent_sessions import engines as real

            raise real.EngineError(f"unknown: {key}")
        return prov, native

    live: set[str] = set()

    async def _cleanup(engine, native, **kw):
        live.discard(f"{engine}:{native}")  # the master is terminated
        return "term"

    monkeypatch.setattr(mission_archive.engines, "parse_key", _parse)
    monkeypatch.setattr(mission_archive.engines, "invalidate_scan_cache", lambda: None)
    monkeypatch.setattr(mission_archive.runtime_cleanup, "cleanup_runtime", _cleanup)
    monkeypatch.setattr(mission_archive.transcript_owner, "transcript_is_owned", lambda n: False)
    monkeypatch.setattr(maintenance, "_session_live", lambda key: key in live)

    calls: list[tuple[str, bool, object, object]] = []
    real_archive = mission_archive.archive_mission

    async def _spy(mission_id, *, abandon=False):
        calls.append((mission_id, abandon, asyncio.get_running_loop(), threading.current_thread()))
        return await real_archive(mission_id, abandon=abandon)

    monkeypatch.setattr(maintenance.mission_archive, "archive_mission", _spy)
    prov.live = live
    prov.calls = calls
    yield prov
    missions.reset_schema_cache_for_test()


def _backdate(mission_id: str, days: float) -> None:
    t = time.time() - days * DAY
    with contextlib.closing(missions._ready()) as con:
        con.execute("UPDATE missions SET closed_at=?, updated_at=? WHERE id=?", (t, t, mission_id))
        with contextlib.suppress(Exception):
            con.commit()


def _mission(state="done", holding=(), days=40.0, turn=False) -> str:
    m = missions.create_mission("ship it", cwd="/repo")
    mid = m["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    for u in holding:
        missions.adopt(mid, KEY[u], role="primary" if u == holding[0] else "sub")
    if turn:
        verdict, _ = missions.claim_turn(mid, "turn-1", "sha")
        assert verdict == missions.TURN_CLAIMED
    if state != "running":
        missions.set_state(mid, "running", state, outcome=state)
    _backdate(mid, days)
    return mid


# ---- dry run ------------------------------------------------------------------------------


def test_dry_run_counts_old_terminal_missions_their_sessions_and_live_terminals(env):
    old = _mission("done", holding=(A, B))
    _mission("done", holding=(C,), days=1)  # too young
    _mission("running", days=90)  # never a candidate
    env.live.add(KEY[A])
    out = asyncio.run(maintenance.missions_dry_run(30))
    assert out == {"eligible": 1, "sessions": 2, "live_sessions": 1, "unresolved": []}
    assert missions.get_mission(old)["archived_at"] is None  # read-only
    assert env.calls == []


# ---- the sweep ----------------------------------------------------------------------------


def test_old_terminal_missions_are_archived_and_stopped_terminals_are_counted(env):
    old = _mission("done", holding=(A, B))
    young = _mission("failed", holding=(C,), days=1)
    env.live.add(KEY[A])  # a terminal mission whose roster session is STILL running
    out = asyncio.run(maintenance.archive_old_missions(30))
    assert out == {
        "archived": 1,
        "sessions_archived": 2,
        "terminals_stopped": 1,
        "skipped": [],
        "failed": [],
    }
    assert missions.get_mission(old)["archived_at"] is not None
    assert missions.get_mission(young)["archived_at"] is None
    assert [(mid, abandon) for mid, abandon, _l, _t in env.calls] == [(old, False)]


def test_a_live_mission_is_never_abandoned(env):
    running = _mission("running", holding=(A,), days=90)
    out = asyncio.run(maintenance.archive_old_missions(1))
    assert out["archived"] == 0
    assert env.calls == []
    row = missions.get_mission(running)
    assert row["state"] == "running" and row["archived_at"] is None


def test_an_unresolved_turn_is_skipped_and_the_rest_continue(env):
    blocked = _mission("done", holding=(A,), turn=True)
    clean = _mission("done", holding=(B,))
    assert asyncio.run(maintenance.missions_dry_run(30))["unresolved"] == [blocked]
    out = asyncio.run(maintenance.archive_old_missions(30))
    assert out["archived"] == 1
    assert [s["mission_id"] for s in out["skipped"]] == [blocked]
    assert "unresolved turn" in out["skipped"][0]["reason"]
    assert missions.get_mission(blocked)["archived_at"] is None
    assert missions.get_mission(clean)["archived_at"] is not None


def test_a_partial_teardown_failure_is_reported_while_the_mission_still_archives(env, monkeypatch):
    mid = _mission("done", holding=(A, B))
    monkeypatch.setattr(
        mission_archive.transcript_owner, "transcript_is_owned", lambda native: native == A
    )
    out = asyncio.run(maintenance.archive_old_missions(30))
    assert out["archived"] == 1
    assert out["sessions_archived"] == 1
    assert [(f["mission_id"], f["session_key"]) for f in out["failed"]] == [(mid, KEY[A])]
    assert "background agent" in out["failed"][0]["reason"]
    assert missions.get_mission(mid)["archived_at"] is not None


def test_archived_missions_are_excluded_and_a_rerun_is_idempotent(env):
    already = _mission("abandoned", holding=(C,))
    asyncio.run(mission_archive.archive_mission(already))
    env.calls.clear()
    target = _mission("done", holding=(A,))
    first = asyncio.run(maintenance.archive_old_missions(30))
    assert first["archived"] == 1
    assert [c[0] for c in env.calls] == [target]
    second = asyncio.run(maintenance.archive_old_missions(30))
    assert second == {
        "archived": 0,
        "sessions_archived": 0,
        "terminals_stopped": 0,
        "skipped": [],
        "failed": [],
    }
    assert len(env.calls) == 1


def test_a_previously_skipped_session_is_counted_by_the_preview_and_the_result(env):
    """The lifecycle Hermes reproduced on PR #1000.

    A session another mission held is marked `archive_state='skipped'` by the first archive. That
    row is HISTORY, not a decision: `begin_archive` clears every skip and re-evaluates it, so once
    the other mission lets go, the session is torn down after all. A preview built on
    `sessions_governed_by_archive` (which filters those rows out) promised 0 sessions and 0 live
    terminals, then archived one and stopped its terminal — and the result said 0/0 too.
    """
    # A holds the session and finishes (which RELEASES it); B then adopts the same session and is
    # still running when A is archived.
    a = _mission("done", holding=(A,), days=40)
    b = _mission("running", holding=(A,), days=40)
    assert missions.get_mission(b)["state"] == "running"

    # Archiving A now marks the session `skipped` — B holds it.
    asyncio.run(mission_archive.archive_mission(a))
    assert missions.archive_sessions_for(a)[0]["archive_state"] == "skipped"
    # Unarchive A WITHOUT restoring its sessions, then let B finish and release the session.
    asyncio.run(mission_archive.unarchive_mission(a, sessions=False))
    missions.set_state(b, "running", "done", outcome="done")
    asyncio.run(mission_archive.archive_mission(b))
    _backdate(a, 40)
    env.live.add(KEY[A])  # its terminal is running again
    env.calls.clear()

    # The preview must now say what the sweep will actually do.
    preview = asyncio.run(maintenance.missions_dry_run(30))
    assert preview["eligible"] == 1
    assert preview["sessions"] == 1
    assert preview["live_sessions"] == 1

    out = asyncio.run(maintenance.archive_old_missions(30))
    assert out["archived"] == 1
    assert out["sessions_archived"] == 1
    assert out["terminals_stopped"] == 1
    assert KEY[A] not in env.live  # the terminal really was stopped


def test_outcome_counts_come_from_the_sessions_actually_settled(env):
    """`sessions_archived` is read from the roster's settled rows, never assumed from the preview:
    an `already_archived` session counts, a `failed` one does not."""
    mid = _mission("done", holding=(A, B))
    env.rows[A].archived = True  # already archived before the sweep — settles `already_archived`
    metadata.patch(KEY[A], archived=True)
    out = asyncio.run(maintenance.archive_old_missions(30))
    assert out["archived"] == 1
    assert out["sessions_archived"] == 2  # already_archived + done
    assert out["failed"] == []
    states = {r["session_key"]: r["archive_state"] for r in missions.archive_sessions_for(mid)}
    assert states == {KEY[A]: "already_archived", KEY[B]: "done"}


def test_archival_runs_on_the_app_loop_not_a_worker(env):
    _mission("done", holding=(A,))

    async def main():
        loop = asyncio.get_running_loop()
        await maintenance.archive_old_missions(30)
        return loop

    loop = asyncio.run(main())
    ((_, _, seen_loop, seen_thread),) = env.calls
    assert seen_loop is loop
    assert seen_thread is threading.main_thread()


# ---- routes -------------------------------------------------------------------------------


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


def test_the_dry_run_route_is_not_captured_by_the_mission_id_route(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/missions/archive-older?older_than_days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["eligible"] == 0 and body["unresolved"] == [] and body["runner"] is None


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?older_than_days=",
        "?older_than_days=0",
        "?older_than_days=abc",
        "?older_than_days=-1",
        "?older_than_days=3651",
        "?older_than_days=2.5",
    ],
)
def test_the_dry_run_query_is_strict(auth_cfg, tmp_home, query):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/missions/archive-older{query}").status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"older_than_days": "30"},
        {"older_than_days": True},
        {"older_than_days": 0},
        {"older_than_days": -3},
        {"older_than_days": 2.5},
        {"older_than_days": 3651},
        {"older_than_days": 30, "abandon": True},
        [30],
    ],
)
def test_the_archive_body_is_strict(auth_cfg, tmp_home, payload):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/missions/archive-older",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_the_archive_route_requires_csrf(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/missions/archive-older",
        json={"older_than_days": 30},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_mission_archival_is_refused_while_a_prune_runs(auth_cfg, tmp_home, monkeypatch):
    """One runner for every maintenance job: a prune in flight refuses the mission sweep."""
    started = threading.Event()
    release = threading.Event()

    def blocking(categories):
        started.set()
        assert release.wait(10)
        return {"removed": 0, "bytes_freed": 0, "skipped": [], "failed": [], "failed_total": 0}

    monkeypatch.setattr(maintenance, "prune_caches", blocking)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    first: dict = {}

    def go():
        first["r"] = c.post(
            "/api/maintenance/prune", json={"categories": ["stale_sockets"]}, headers=hdr
        )

    t = threading.Thread(target=go)
    t.start()
    try:
        assert started.wait(10)
        r = c.post("/api/missions/archive-older", json={"older_than_days": 30}, headers=hdr)
        assert r.status_code == 409
        assert r.json()["busy"]["job"] == "prune"
    finally:
        release.set()
        t.join(20)
    assert first["r"].status_code == 200
