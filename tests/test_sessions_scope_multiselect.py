"""The project/cluster field as a multiselect (#2438): `project` and `cluster` are repeatable.

The field is ONE facet whose values OR together, ANDed with every other filter, applied before
paging, with facets computed before it. A chosen cluster narrows ITS OWN project and nothing else,
so today's single-value queries (including the tree's `project=p&cluster=c` pair) keep their
meaning. `cluster=none` stays a single, exclusive choice. Raw values are bounded.

Fixture (`conftest.fake_jsonl`): A and B live in repo-a (project ``p``), C in /tmp/other (project
``q``), D in Default. Clusters: c1 = {A}, c2 = {B} in ``p``; c3 = {C} in ``q``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import automations_store, engines, projects
from agent_sessions.main import create_app
from agent_sessions.routes import sessions as sessions_routes

A = "claude:11111111-1111-1111-1111-111111111111"
B = "claude:22222222-2222-2222-2222-222222222222"
C = "claude:33333333-3333-3333-3333-333333333333"
D = "claude:55555555-5555-5555-5555-555555555555"
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


def _cwd(key: str) -> str:
    return next(s.cwd for s in engines.scan_all() if engines.session_key(s) == key)


@pytest.fixture
def world(api):
    p = projects.create("Repo A", folders=[_cwd(A)]).id
    q = projects.create("Other", folders=[_cwd(C)]).id

    def make(project_id, title):
        r = api.post("/api/clusters", json={"project_id": project_id, "title": title})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def place(key, cid):
        assert api.put(f"/api/sessions/{key}/cluster", json={"cluster_id": cid}).status_code == 200

    c1, c2, c3 = make(p, "Login"), make(p, "Billing"), make(q, "Notes")
    place(A, c1)
    place(B, c2)
    place(C, c3)
    return {"p": p, "q": q, "c1": c1, "c2": c2, "c3": c3}


def _ids(api, query: str) -> set[str]:
    d = api.get(f"/api/sessions?limit=50&{query}").json()
    assert d["total"] == len(d["sessions"]), d
    return {s["id"] for s in d["sessions"]}


def _q(w: dict, **params: list[str]) -> str:
    return "&".join(f"{k}={w.get(v, v)}" for k, vs in params.items() for v in vs)


TRUTH_TABLE = [
    # (project values, cluster values, rows that match) — names resolve through the world dict.
    (["p"], [], {A, B}),
    (["p", "q"], [], {A, B, C}),
    (["p"], ["c1"], {A}),  # a chosen cluster narrows its own project
    (["p"], ["c1", "c2"], {A, B}),
    (["p", "q"], ["c1"], {A, C}),  # ... and only its own: q keeps its whole arm
    (["q"], ["c1"], {A, C}),
    (["p", "p"], ["c1", "c1"], {A}),  # duplicates are ignored
    (["p"], ["tc_000000000000"], {A, B}),  # an unknown cluster narrows nothing, matches nothing
    (["nope"], [], set()),  # an unknown project matches nothing
    ([], ["c3"], {C}),
    ([DEFAULT, "p"], [], {A, B, D}),
]


@pytest.mark.parametrize(("ps", "cs", "want"), TRUTH_TABLE)
def test_the_truth_table(api, world, ps, cs, want):
    assert _ids(api, _q(world, project=ps, cluster=cs)) == want


def test_one_value_is_todays_answer(api, world):
    # The single-value queries the tree sent before #2438, unchanged.
    assert _ids(api, f"project={world['p']}") == {A, B}
    assert _ids(api, f"project={world['p']}&cluster={world['c1']}") == {A}
    assert _ids(api, f"cluster={world['c1']}") == {A}
    # A legacy cwd still filters by its launch folder.
    assert _ids(api, f"project={_cwd(C)}") == {C}


def test_cluster_none_stays_a_single_exclusive_choice(api, world):
    assert _ids(api, "cluster=none") == {D}
    assert _ids(api, f"project={world['p']}&cluster=none") == set()
    assert _ids(api, f"project={DEFAULT}&cluster=none") == {D}
    for bad in (
        f"cluster=none&cluster={world['c1']}",
        f"cluster=none&project={world['p']}&project={world['q']}",
    ):
        assert api.get(f"/api/sessions?{bad}").status_code == 422, bad


def test_raw_values_are_bounded_before_deduplication(api, world):
    ok = "&".join([f"project={world['p']}"] * sessions_routes.MAX_SCOPE_VALUES)
    assert api.get(f"/api/sessions?{ok}").status_code == 200
    over = ok + f"&project={world['p']}"
    assert api.get(f"/api/sessions?{over}").status_code == 422
    over_c = "&".join([f"cluster={world['c1']}"] * (sessions_routes.MAX_SCOPE_VALUES + 1))
    assert api.get(f"/api/sessions?{over_c}").status_code == 422


def test_facets_do_not_move_with_the_selection(api, world):
    every = api.get("/api/sessions?limit=50").json()["facets"]
    for q in (
        _q(world, project=["p", "q"]),
        _q(world, project=["q"], cluster=["c1"]),
        _q(world, cluster=["c1", "c3"]),
    ):
        assert api.get(f"/api/sessions?limit=50&{q}").json()["facets"] == every, q


def test_the_union_filters_before_paging(api, world):
    q = _q(world, project=["p", "q"])
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = api.get(f"/api/sessions?{q}&limit=1&offset={offset}").json()
        assert page["total"] == 3
        seen += [s["id"] for s in page["sessions"]]
        offset = page["next_offset"]
    assert sorted(seen) == sorted([A, B, C])


def test_the_union_ands_with_the_other_filters_and_started_by(api, world, monkeypatch):
    # A was launched by an automation (#2437): the default "You" leaves it out of the union.
    monkeypatch.setattr(automations_store, "launched_among", lambda keys: {A} & set(keys))
    q = _q(world, project=["p", "q"])
    d = api.get(f"/api/sessions?limit=50&origin=operator&{q}").json()
    assert {s["id"] for s in d["sessions"]} == {B, C}
    assert d["origin_hidden"] == {"count": 1, "needs_you": 0}
    assert _ids(api, f"origin=automation&{q}") == {A}
    # ... and with the agent filter (every fixture row is claude: an unknown agent leaves none).
    assert _ids(api, f"engine=nobody&{q}") == set()


def test_an_unreadable_store_is_said_for_a_cluster_pick_only(api, world, monkeypatch, tmp_path):
    broken = tmp_path / "clusters-json-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_CLUSTERS", str(broken))
    d = api.get(f"/api/sessions?{_q(world, project=['q'], cluster=['c1'])}").json()
    assert d["cluster_filter_unavailable"] is True and d["sessions"] == []
    # Projects alone need no cluster store: answered as usual.
    p = api.get(f"/api/sessions?limit=50&{_q(world, project=['p', 'q'])}").json()
    assert "cluster_filter_unavailable" not in p
    assert {s["id"] for s in p["sessions"]} == {A, B, C}


def test_an_unreadable_automation_store_keeps_the_union_and_says_so(api, world, monkeypatch):
    monkeypatch.setattr(automations_store, "launched_among", lambda keys: None)
    d = api.get(f"/api/sessions?limit=50&origin=operator&{_q(world, project=['p', 'q'])}").json()
    assert {s["id"] for s in d["sessions"]} == {A, B, C}
    assert d["origin_filter_unavailable"] is True
