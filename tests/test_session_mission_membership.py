"""Mission membership on session rows (#948 P4).

The session list is where adoption moves (#948 §5): a held row shows its mission, the list filters
by mission, and the pane header reads the same fact through the single-row lookup. Three contracts
are pinned here:

* **Tri-state.** ``mission`` is the holder, ``None`` when no mission holds the session, and ABSENT
  when the store could not be read — "unknown" must never read as "not in a mission".
* **Filter before pagination.** ``total`` and ``next_offset`` describe the filtered set, exactly as
  the project/agent filters do.
* **Facets over the unfiltered set.** The dropdown keeps every mission however the list is narrowed.

The unreadable-store cases break the STORE (its path is a directory), not the function that reads
it — patching the reader to raise would test a door production cannot reach.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import missions
from agent_sessions.main import create_app

A = "claude:11111111-1111-1111-1111-111111111111"
B = "claude:22222222-2222-2222-2222-222222222222"
C = "claude:33333333-3333-3333-3333-333333333333"
UNHELD = "claude:55555555-5555-5555-5555-555555555555"


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


@pytest.fixture
def held(fake_jsonl, tmp_path):
    alpha = missions.create_mission(
        "ship the first thing", title="Alpha mission", cwd=str(tmp_path)
    )
    beta = missions.create_mission("ship the second thing", title="Beta mission", cwd=str(tmp_path))
    missions.adopt(alpha["id"], A)
    missions.adopt(alpha["id"], B)
    missions.adopt(beta["id"], C)
    return alpha, beta


@pytest.fixture
def unreadable_store(monkeypatch, tmp_path):
    """The mission store's path is a directory, so opening it fails the way a real fault does."""
    broken = tmp_path / "missions-db-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(broken))
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


def _rows(c, query="limit=50"):
    return {s["id"]: s for s in c.get(f"/api/sessions?{query}").json()["sessions"]}


def test_rows_carry_the_holding_mission_or_null(auth_cfg, held):
    alpha, beta = held
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = _rows(c)
    assert rows[A]["mission"] == {
        "id": alpha["id"],
        "title": "Alpha mission",
        "state": alpha["state"],
    }
    assert rows[C]["mission"]["id"] == beta["id"]
    assert rows[UNHELD]["mission"] is None


def test_mission_filter_runs_before_pagination(auth_cfg, held):
    alpha, _ = held
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    p0 = c.get(f"/api/sessions?mission={alpha['id']}&limit=1&offset=0").json()
    assert p0["total"] == 2 and len(p0["sessions"]) == 1 and p0["next_offset"] == 1
    p1 = c.get(f"/api/sessions?mission={alpha['id']}&limit=1&offset=1").json()
    assert p1["total"] == 2 and len(p1["sessions"]) == 1 and p1["next_offset"] is None
    assert {p0["sessions"][0]["id"], p1["sessions"][0]["id"]} == {A, B}


def test_mission_none_lists_only_unheld_sessions(auth_cfg, held):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?mission=none&limit=50").json()
    assert [s["id"] for s in d["sessions"]] == [UNHELD]
    assert d["total"] == 1


def test_mission_facets_cover_the_unfiltered_set(auth_cfg, held):
    alpha, beta = held
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # A filter that matches nothing must not shrink the facets.
    d = c.get(f"/api/sessions?mission={beta['id']}&q=zzz-nothing&limit=50").json()
    assert d["total"] == 0
    assert d["facets"]["missions"] == [
        {"id": alpha["id"], "title": "Alpha mission", "state": alpha["state"], "count": 2},
        {"id": beta["id"], "title": "Beta mission", "state": beta["state"], "count": 1},
    ]
    assert d["facets"]["no_mission"] == 1
    assert "mission_filter_unavailable" not in d


def test_a_detach_is_visible_on_the_next_request(auth_cfg, held):
    alpha, _ = held
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert _rows(c)[A]["mission"]["id"] == alpha["id"]
    missions.detach(alpha["id"], A)
    d = c.get("/api/sessions?limit=50").json()
    assert {s["id"]: s for s in d["sessions"]}[A]["mission"] is None
    assert d["facets"]["missions"][0]["count"] == 1
    assert d["facets"]["no_mission"] == 2


def test_the_single_row_lookup_carries_the_same_mission(auth_cfg, held):
    alpha, _ = held
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/sessions/{A}").json()["mission"]["id"] == alpha["id"]
    assert c.get(f"/api/sessions/{UNHELD}").json()["mission"] is None


def test_an_unreadable_store_leaves_membership_unknown_not_empty(
    auth_cfg, fake_jsonl, unreadable_store
):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?limit=50").json()
    # The list itself still works.
    assert d["total"] == 4
    assert all("mission" not in s for s in d["sessions"])
    assert "missions" not in d["facets"] and "no_mission" not in d["facets"]
    assert "mission" not in c.get(f"/api/sessions/{A}").json()


def test_a_mission_filter_the_store_cannot_answer_says_so(auth_cfg, fake_jsonl, unreadable_store):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?mission=none&limit=50").json()
    assert d["sessions"] == [] and d["total"] == 0 and d["next_offset"] is None
    assert d["mission_filter_unavailable"] is True
    # Facets still describe the scoped set, so the other dropdowns keep working.
    assert d["facets"]["engines"] == ["claude"]
