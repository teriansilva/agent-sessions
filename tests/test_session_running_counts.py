"""RUNNING vs WORKING, counted over the whole list and the whole host (#1085).

The footers said "20 ENGAGED · 1 LIVE" on a host with a dozen agents up: both numbers were counted
over the sidebar's loaded 20-row page, and LIVE meant "printed in the last 10 s". Running is a live
dtach master, stamped per row and totalled over the FILTERED set — never the page — and the host
count ignores filters and scope entirely.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import ptybridge, webterm
from agent_sessions.main import create_app
from agent_sessions.routes import sessions as sessions_routes

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
OUTSIDE = "99999999-9999-4999-8999-999999999999"  # a running agent the list does not show


@pytest.fixture(autouse=True)
def _fresh_cache():
    sessions_routes._running_cache = None
    yield
    sessions_routes._running_cache = None


def _client(cfg):
    c = TestClient(create_app(cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c


def _running(monkeypatch, *sids):
    calls = []

    def fake():
        calls.append(1)
        return [("claude", s) for s in sids]

    monkeypatch.setattr(ptybridge, "list_sessions", fake)
    return calls


def test_running_is_stamped_and_totalled_over_the_filtered_set_not_the_page(
    auth_cfg, fake_jsonl, monkeypatch
):
    _running(monkeypatch, A, B, OUTSIDE)
    c = _client(auth_cfg)
    d = c.get("/api/sessions?limit=1&offset=0").json()
    assert len(d["sessions"]) == 1
    # Two of the listed sessions run, although only one row was loaded.
    assert d["live_total"] == 2
    by_id = {r["id"]: r["running"] for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert by_id[f"claude:{A}"] is True
    assert by_id[f"claude:{B}"] is True
    assert by_id[f"claude:{C}"] is False
    # A filter narrows the list count…
    only_tmp = c.get("/api/sessions?q=hello tmp").json()
    assert only_tmp["total"] == 1 and only_tmp["live_total"] == 0
    # …never the host count, which also includes the agent the list does not show.
    assert c.get("/api/agents").json()["live"] == 3


def test_working_is_the_subset_that_printed_in_the_window(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch, A, B)
    now = time.time()
    last = {f"claude:{A}": now - 1, f"claude:{B}": now - 600}
    monkeypatch.setattr(webterm, "get_last_output_at", lambda k: last.get(k))
    d = _client(auth_cfg).get("/api/agents").json()
    # B is running but quiet (thinking, or waiting on the operator): live, not working.
    assert d == {"live": 2, "working": 1}


def test_the_probe_runs_once_per_ttl_and_fails_soft(auth_cfg, fake_jsonl, monkeypatch):
    calls = _running(monkeypatch, A)
    c = _client(auth_cfg)
    c.get("/api/sessions")
    c.get("/api/sessions?limit=1")
    c.get("/api/agents")
    assert len(calls) == 1

    def boom():
        raise OSError("runtime dir gone")

    sessions_routes._running_cache = None
    monkeypatch.setattr(ptybridge, "list_sessions", boom)
    d = c.get("/api/sessions").json()
    assert d["live_total"] == 0
    assert d["total"] >= 1
    assert c.get("/api/agents").json() == {"live": 0, "working": 0}


def test_agents_route_counts_the_host_without_the_list(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch, A, OUTSIDE)
    c = _client(auth_cfg)
    assert c.get("/api/agents").json() == {"live": 2, "working": 0}


def test_agents_route_requires_login(auth_cfg, fake_jsonl):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get("/api/agents", follow_redirects=False).status_code in (401, 303)
