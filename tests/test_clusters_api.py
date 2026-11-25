"""Session cluster routes and the cluster fields of ``/api/sessions`` (#1997).

Pinned here: the tri-state row stamp (cluster / ``None`` / absent when unknown), the filter
running before pagination, facets over the unfiltered set, the fail-soft unreadable store, mission
precedence, cross-project refusals, and auth + CSRF + Origin on every write.

The unreadable-store cases break the STORE (its path is a directory), not the reader.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import clusters_loop, engines, missions, prefs, projects, review
from agent_sessions import session_clusters as sc
from agent_sessions.main import create_app

A = "claude:11111111-1111-1111-1111-111111111111"  # /home/user/claude/repo-a
B = "claude:22222222-2222-2222-2222-222222222222"  # /home/user/claude/repo-a
C = "claude:33333333-3333-3333-3333-333333333333"  # /tmp/other
D = "claude:55555555-5555-5555-5555-555555555555"  # /home/user/claude/demoapp.io
DEFAULT = projects.DEFAULT_PROJECT_ID


@pytest.fixture
def api(auth_cfg, fake_jsonl):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    c.headers.update(
        {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    )
    return c


@pytest.fixture
def repo(fake_jsonl):
    """A project entity adopting repo-a: A and B are in it, C and D stay in Default."""
    # The folder name decodes lossily, so ask the scanner where A actually lives.
    cwd = next(s.cwd for s in engines.scan_all() if engines.session_key(s) == A)
    return projects.create("Repo A", folders=[cwd])


@pytest.fixture
def unreadable_store(monkeypatch, tmp_path):
    broken = tmp_path / "clusters-json-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_CLUSTERS", str(broken))


def _rows(c, query="limit=50"):
    return {s["id"]: s for s in c.get(f"/api/sessions?{query}").json()["sessions"]}


def _make(c, project_id, title):
    r = c.post("/api/clusters", json={"project_id": project_id, "title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _place(c, key, cid):
    return c.put(f"/api/sessions/{key}/cluster", json={"cluster_id": cid})


# --- /api/sessions ------------------------------------------------------------------------------


def test_rows_carry_their_cluster_or_null_and_the_lookup_agrees(api, repo):
    cid = _make(api, repo.id, "Login flow")
    assert _place(api, A, cid).json() == {
        "id": A,
        "cluster": {"id": cid, "title": "Login flow", "kept": True},
    }
    rows = _rows(api)
    assert rows[A]["cluster"] == {"id": cid, "title": "Login flow", "kept": True}
    assert rows[B]["cluster"] is None and rows[C]["cluster"] is None
    one = api.get(f"/api/sessions/{A}").json()
    assert one["cluster"] == rows[A]["cluster"]
    assert set(one) == set(rows[A])


def test_the_cluster_filter_runs_before_pagination_and_none_lists_the_rest(api, repo):
    cid = _make(api, repo.id, "Login flow")
    _place(api, A, cid)
    _place(api, B, cid)
    p0 = api.get(f"/api/sessions?cluster={cid}&limit=1&offset=0").json()
    assert p0["total"] == 2 and len(p0["sessions"]) == 1 and p0["next_offset"] == 1
    p1 = api.get(f"/api/sessions?cluster={cid}&limit=1&offset=1").json()
    assert p1["next_offset"] is None
    assert {p0["sessions"][0]["id"], p1["sessions"][0]["id"]} == {A, B}
    none = api.get("/api/sessions?cluster=none&limit=50").json()
    assert {s["id"] for s in none["sessions"]} == {C, D}
    assert "cluster_filter_unavailable" not in none


def test_cluster_facets_cover_the_unfiltered_set_and_list_empty_clusters(api, repo):
    cid = _make(api, repo.id, "Login flow")
    empty = _make(api, DEFAULT, "Empty one")
    _place(api, A, cid)
    d = api.get(f"/api/sessions?cluster={cid}&q=zzz-nothing&limit=50").json()
    assert d["total"] == 0
    assert d["facets"]["clusters"] == [
        {"id": empty, "title": "Empty one", "kept": True, "project_id": DEFAULT, "count": 0},
        {"id": cid, "title": "Login flow", "kept": True, "project_id": repo.id, "count": 1},
    ]


def test_mission_facets_count_per_project(api, repo, tmp_path):
    m = missions.create_mission("ship it", title="A mission", cwd=str(tmp_path))
    for key in (A, B, C):
        missions.adopt(m["id"], key)
    (facet,) = api.get("/api/sessions?limit=50").json()["facets"]["missions"]
    assert facet["count"] == 3 and facet["by_project"] == {repo.id: 2, DEFAULT: 1}


def test_a_mission_held_session_shows_its_mission_not_a_cluster(api, repo, tmp_path):
    cid = _make(api, repo.id, "Login flow")
    _place(api, A, cid)
    _place(api, B, cid)
    m = missions.create_mission("ship it", title="A mission", cwd=str(tmp_path))
    missions.adopt(m["id"], A)
    rows = _rows(api)
    assert rows[A]["mission"]["id"] == m["id"] and rows[A]["cluster"] is None
    d = api.get(f"/api/sessions?cluster={cid}&limit=50").json()
    assert [s["id"] for s in d["sessions"]] == [B]
    assert next(f for f in d["facets"]["clusters"] if f["id"] == cid)["count"] == 1
    # `none` is "in no group": a mission-held session is not listed there either.
    assert A not in {
        s["id"] for s in api.get("/api/sessions?cluster=none&limit=50").json()["sessions"]
    }
    # Placing a session a mission holds is refused; the mission is its group.
    assert _place(api, A, cid).status_code == 409
    # When the mission lets go, the placement is still there.
    missions.detach(m["id"], A)
    assert _rows(api)[A]["cluster"]["id"] == cid


def test_an_unreadable_store_leaves_the_stamp_absent_and_says_the_filter_is_unavailable(
    api, unreadable_store
):
    d = api.get("/api/sessions?limit=50").json()
    assert d["total"] == 4 and all("cluster" not in s for s in d["sessions"])
    assert "clusters" not in d["facets"]
    f = api.get("/api/sessions?cluster=tc_0123456789ab&limit=50").json()
    assert f["sessions"] == [] and f["total"] == 0 and f["cluster_filter_unavailable"] is True
    assert "cluster" not in api.get(f"/api/sessions/{A}").json()
    listed = api.get("/api/clusters").json()
    assert listed["unreadable"] is True and listed["clusters"] == []
    for r in (
        api.post("/api/clusters", json={"project_id": DEFAULT, "title": "x"}),
        api.patch("/api/clusters/tc_0123456789ab", json={"title": "x"}),
        api.delete("/api/clusters/tc_0123456789ab"),
        _place(api, A, "tc_0123456789ab"),
    ):
        assert r.status_code == 503, r.text


def test_an_unreadable_missions_store_leaves_the_cluster_unknown_too(
    api, repo, monkeypatch, tmp_path
):
    cid = _make(api, repo.id, "Login flow")
    _place(api, A, cid)
    broken = tmp_path / "missions-db-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(broken))
    missions.reset_schema_cache_for_test()
    try:
        d = api.get("/api/sessions?limit=50").json()
        assert all("cluster" not in s and "mission" not in s for s in d["sessions"])
        assert "clusters" not in d["facets"]
        f = api.get(f"/api/sessions?cluster={cid}&limit=50").json()
        assert f["cluster_filter_unavailable"] is True and f["sessions"] == []
    finally:
        missions.reset_schema_cache_for_test()


def test_turned_off_rows_say_null_the_filter_is_unavailable_and_the_store_is_kept(api, repo):
    cid = _make(api, repo.id, "Login flow")
    _place(api, A, cid)
    r = api.post("/api/prefs", json={"clusters": {"enabled": False}})
    assert r.status_code == 200 and r.json()["clusters"]["enabled"] is False
    d = api.get("/api/sessions?limit=50").json()
    assert all(s["cluster"] is None for s in d["sessions"]) and "clusters" not in d["facets"]
    assert api.get(f"/api/sessions?cluster={cid}").json()["cluster_filter_unavailable"] is True
    api.post("/api/prefs", json={"clusters": {"enabled": True}})
    assert _rows(api)[A]["cluster"]["id"] == cid


# --- the routes ---------------------------------------------------------------------------------


def test_list_rename_keep_let_hq_rename_merge_and_dissolve(api, repo):
    a = _make(api, repo.id, "Login flow")
    b = _make(api, repo.id, "Billing")
    _place(api, A, a)
    listed = api.get(f"/api/clusters?project={repo.id}").json()
    assert [(c["title"], c["count"], c["source"], c["kept"]) for c in listed["clusters"]] == [
        ("Billing", 0, "operator", True),
        ("Login flow", 1, "operator", True),
    ]
    assert listed["settings"]["enabled"] is True and listed["unreadable"] is False
    assert api.get(f"/api/clusters?project={DEFAULT}").json()["clusters"] == []

    assert api.patch(f"/api/clusters/{a}", json={"title": "Auth"}).json() == {
        "id": a,
        "title": "Auth",
        "kept": True,
    }
    assert api.patch(f"/api/clusters/{a}", json={"keep": False}).json()["kept"] is False
    assert api.patch(f"/api/clusters/{a}", json={"keep": True}).json()["kept"] is True
    for bad in (
        {},
        {"title": ""},
        {"title": "x" * 61},
        {"keep": "yes"},
        {"title": "x", "keep": True},
        {"name": "x"},
    ):
        assert api.patch(f"/api/clusters/{a}", json=bad).status_code == 422, bad

    assert api.post(f"/api/clusters/{a}/merge", json={"into": b}).json()["id"] == b
    assert _rows(api)[A]["cluster"]["id"] == b
    assert api.post(f"/api/clusters/{a}/merge", json={"into": b}).status_code == 404

    assert api.delete(f"/api/clusters/{b}").json() == {"ok": True}
    assert _rows(api)[A]["cluster"] is None
    assert api.delete(f"/api/clusters/{b}").status_code == 404
    assert api.delete("/api/clusters/not-an-id").status_code == 404


def test_cross_project_placement_and_merge_are_refused(api, repo):
    mine = _make(api, repo.id, "Login flow")
    other = _make(api, DEFAULT, "Elsewhere")
    assert _place(api, A, other).status_code == 409
    assert _place(api, C, mine).status_code == 409
    assert api.post(f"/api/clusters/{mine}/merge", json={"into": other}).status_code == 409
    assert sc.read_checked()["membership"] == {}


def test_create_needs_a_live_project_and_placing_needs_a_listed_session(api, repo):
    assert (
        api.post("/api/clusters", json={"project_id": "proj_nope", "title": "x"}).status_code == 404
    )
    assert api.post("/api/clusters", json={"title": "x"}).status_code == 404
    assert api.post("/api/clusters", json={"project_id": repo.id, "title": " "}).status_code == 422
    assert api.post("/api/clusters", json=["x"]).status_code == 422
    cid = _make(api, repo.id, "Login flow")
    archived = "claude:44444444-4444-4444-4444-444444444444"
    assert _place(api, archived, cid).status_code == 404
    assert _place(api, "claude:99999999-9999-9999-9999-999999999999", cid).status_code == 404
    assert _place(api, "not a session id", cid).status_code == 404
    assert api.put(f"/api/sessions/{A}/cluster", json={}).status_code == 422
    assert api.put(f"/api/sessions/{A}/cluster", json={"cluster_id": 7}).status_code == 422
    assert _place(api, A, "tc_000000000000").status_code == 404


def test_back_to_hqs_choice_keeps_the_session_in_place_but_unplaced(api, repo):
    cid = _make(api, repo.id, "Login flow")
    _place(api, A, cid)
    assert _place(api, A, None).json() == {"id": A, "cluster": None}
    assert sc.read_checked()["membership"][A] == {"cluster": cid, "placed": False}


def test_recluster_is_gated_and_runs_the_sweep_now(api, repo, monkeypatch):
    assert api.post("/api/clusters/recluster", json={}).status_code == 409  # no endpoint
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test/v1",
            "api_key": "sk-test",  # noqa: S106
            "model": "m",
        }
    )
    seen: list[dict] = []

    async def run_sweep(**kw):
        seen.append(kw)
        return {"projects": [{"project_id": repo.id, "status": "committed"}], "deferred": 0}

    monkeypatch.setattr(clusters_loop, "run_sweep", run_sweep)
    r = api.post("/api/clusters/recluster", json={"project_id": repo.id})
    assert r.status_code == 200 and r.json()["projects"][0]["status"] == "committed"
    assert api.post("/api/clusters/recluster", json={}).status_code == 200
    assert seen == [{"project_id": repo.id, "force": True}, {"project_id": None, "force": True}]
    assert api.post("/api/clusters/recluster", json={"project_id": "proj_nope"}).status_code == 404
    assert api.post("/api/clusters/recluster", json={"all": True}).status_code == 422

    async def not_configured(**kw):
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(clusters_loop, "run_sweep", not_configured)
    assert api.post("/api/clusters/recluster", json={}).status_code == 409
    prefs.set_clusters({"enabled": False})
    assert api.post("/api/clusters/recluster", json={}).status_code == 409


WRITES = [
    ("post", "/api/clusters", {"project_id": DEFAULT, "title": "x"}),
    ("post", "/api/clusters/recluster", {}),
    ("patch", "/api/clusters/tc_0123456789ab", {"title": "x"}),
    ("post", "/api/clusters/tc_0123456789ab/merge", {"into": "tc_0123456789ac"}),
    ("delete", "/api/clusters/tc_0123456789ab", None),
    ("put", f"/api/sessions/{A}/cluster", {"cluster_id": None}),
]


@pytest.mark.parametrize(("method", "url", "body"), WRITES, ids=[w[1] for w in WRITES])
def test_every_write_needs_login_csrf_and_origin(auth_cfg, fake_jsonl, api, method, url, body):
    kw = {} if body is None else {"json": body}
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert getattr(anon, method)(url, headers={"Origin": auth_cfg.origin}, **kw).status_code in (
        401,
        403,
    )
    csrf = api.headers["X-CSRF-Token"]
    for headers in (
        {"X-CSRF-Token": "wrong", "Origin": auth_cfg.origin},
        {"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    ):
        r = getattr(api, method)(url, headers=headers, **kw)
        assert r.status_code == 403, (headers, r.text)
    assert not sc._path().exists()


def test_reading_clusters_needs_login(auth_cfg, fake_jsonl):
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert anon.get("/api/clusters").status_code in (401, 403)


# --- prefs --------------------------------------------------------------------------------------


def test_the_clusters_prefs_block_defaults_validates_and_is_echoed(api):
    cfg = api.get("/api/config").json()["clusters"]
    assert cfg == {"enabled": True, "agent_tool": True, "interval_minutes": 30, "configured": False}
    r = api.post("/api/prefs", json={"clusters": {"agent_tool": False, "interval_minutes": 240}})
    assert r.json()["clusters"] == {
        "enabled": True,
        "agent_tool": False,
        "interval_minutes": 240,
        "configured": False,
    }
    for bad in (
        {"interval_minutes": 20},
        {"interval_minutes": True},
        {"enabled": "yes"},
        {"agent_tool": 1},
        {"interval": 30},
        "on",
    ):
        assert api.post("/api/prefs", json={"clusters": bad}).status_code == 422, bad
    assert prefs.get_clusters() == {"enabled": True, "agent_tool": False, "interval_minutes": 240}


def test_a_stored_block_with_junk_reads_as_defaults():
    prefs._set("clusters", {"enabled": "no", "interval_minutes": 7, "agent_tool": None})
    assert prefs.get_clusters() == {"enabled": True, "agent_tool": True, "interval_minutes": 30}
