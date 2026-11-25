"""Session lineage over the API (#2325): main sessions only, lazy children and attention routes,
the scope boundary, archive-with-subagents, and the main-only consumers (Pulse, AI review)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_sessions import lineage, metadata
from agent_sessions.main import create_app

K1, K2, K3, K5 = (f"claude:{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}" for c in "1235")
REPO_A = "/home/user/claude/repo/a"


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


@pytest.fixture
def c(auth_cfg, fake_jsonl):
    client = _client(auth_cfg)
    client.csrf = _login(client, auth_cfg)
    client.hdr = {"X-CSRF-Token": client.csrf, "Origin": auth_cfg.origin}
    return client


def _ids(page):
    return [s["id"] for s in page["sessions"]]


def _needs_you(key):
    metadata.patch(key, intervention_required=True, intervention_reason="approve a command")


def test_list_returns_main_sessions_with_lineage_fields(c):
    lineage.link(K2, K1, "launch")
    page = c.get("/api/sessions?limit=50").json()
    assert K2 not in _ids(page)
    assert page["total"] == 3
    main = next(s for s in page["sessions"] if s["id"] == K1)
    assert main["child_count"] == 1 and main["descendant_count"] == 1
    assert main["rollup"] == "idle" and main["attention_count"] == 0
    # No filter → the page shape is unchanged.
    assert "attention_outside" not in page


def test_search_never_matches_a_child_and_the_attention_strip_counts_it(c):
    lineage.link(K2, K1, "launch")
    _needs_you(K2)
    hit = c.get("/api/sessions?q=second").json()  # 2222's own first message
    assert hit["total"] == 0
    assert hit["attention_outside"]["count"] == 1
    assert [m["id"] for m in hit["attention_outside"]["mains"]] == [K1]
    # The main session matching the search carries its child's attention.
    main = c.get("/api/sessions?q=first+message").json()
    assert _ids(main) == [K1] and main["sessions"][0]["attention"] == [K2]
    assert main["attention_outside"]["count"] == 0


def test_a_tag_filter_that_hides_a_main_shows_its_childs_attention_outside(c):
    """#2338 review: a tag filter is a filter like any other — a main session it hides takes its
    subagent's attention into the strip, never silently off the page."""
    lineage.link(K2, K1, "launch")
    _needs_you(K2)
    metadata.patch(K1, tag="bug")
    # Tag-only mismatch: the main is hidden, and the strip names it.
    miss = c.get("/api/sessions?tag=release").json()
    assert K1 not in _ids(miss)
    assert miss["attention_outside"]["count"] == 1
    assert [m["id"] for m in miss["attention_outside"]["mains"]] == [K1]
    # Matching tag: the main is listed and carries its child's attention; nothing is outside.
    hit = c.get("/api/sessions?tag=BUG").json()
    assert _ids(hit) == [K1] and hit["sessions"][0]["attention"] == [K2]
    assert hit["attention_outside"]["count"] == 0
    # A blank tag is no filter: the unfiltered page shape, with no strip.
    assert "attention_outside" not in c.get("/api/sessions", params={"tag": "  "}).json()


def test_facets_and_engine_filter_count_main_sessions_only(c):
    lineage.link(K2, K1, "launch")
    page = c.get("/api/sessions?engine=claude&limit=50").json()
    assert page["total"] == 3
    # Every fixture session is an unadopted folder → the synthetic Default project's facet count.
    counts = {p["id"]: p.get("count") for p in page["facets"]["projects"]}
    assert sum(v for v in counts.values() if isinstance(v, int)) == 3


def test_attention_outside_names_every_filtered_out_main(c, fake_jsonl):
    """#2325 review: the strip's count is uncapped, and so is the set of main sessions it names —
    51 filtered-out mains, each with one subagent that needs the operator, are all named."""
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    for i in range(51):
        main = f"bbbbbbbb-0000-0000-0000-{i:012d}"
        kid = f"cccccccc-0000-0000-0000-{i:012d}"
        (proj / f"{main}.jsonl").write_text('{"type":"user","message":{"content":"zz main"}}\n')
        (proj / f"{kid}.jsonl").write_text('{"type":"user","message":{"content":"zz kid"}}\n')
        lineage.link(f"claude:{kid}", f"claude:{main}", "launch")
        _needs_you(f"claude:{kid}")
    page = c.get("/api/sessions?q=first+message").json()  # matches only 1111
    assert page["attention_outside"]["count"] == 51
    assert len(page["attention_outside"]["mains"]) == 51


def test_children_route_is_lazy_paged_and_ordered(c):
    lineage.link(K2, K1, "launch", now=2)
    lineage.link(K5, K1, "launch", now=1)
    page = c.get(f"/api/sessions/{K1}/children?limit=1").json()
    assert page["total"] == 2 and _ids(page) == [K5] and page["next_offset"] == 1
    rest = c.get(f"/api/sessions/{K1}/children?limit=1&offset=1").json()
    assert _ids(rest) == [K2] and rest["next_offset"] is None
    assert c.get(f"/api/sessions/{K3}/children").json()["total"] == 0
    assert c.get("/api/sessions/claude:nope/children").status_code == 404


def test_attention_route_lists_every_attention_descendant_with_its_path(c):
    lineage.link(K2, K1, "launch")
    lineage.link(K5, K2, "launch")  # grandchild
    _needs_you(K5)
    page = c.get(f"/api/sessions/{K1}/attention").json()
    assert page["total"] == 1
    assert page["sessions"][0]["id"] == K5
    assert [p["id"] for p in page["sessions"][0]["path"]] == [K1, K2]


def test_more_than_ten_attention_descendants_page_through_the_route(c, fake_jsonl):
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    kids = []
    for i in range(12):
        native = f"aaaaaaaa-0000-0000-0000-{i:012d}"
        (proj / f"{native}.jsonl").write_text('{"type":"user","message":{"content":"kid"}}\n')
        key = f"claude:{native}"
        kids.append(key)
        lineage.link(key, K1, "launch", now=float(i))
        _needs_you(key)
    main = next(s for s in c.get("/api/sessions?limit=50").json()["sessions"] if s["id"] == K1)
    assert main["attention_count"] == 12 and len(main["attention"]) == 10
    seen = []
    offset = 0
    while offset is not None:
        page = c.get(f"/api/sessions/{K1}/attention?limit=5&offset={offset}").json()
        assert page["total"] == 12
        seen += _ids(page)
        offset = page["next_offset"]
    assert seen == kids
    # 12 kids + 1 grandchild-free main: the children route pages the same way.
    assert c.get(f"/api/sessions/{K1}/children?limit=50").json()["total"] == 12


def test_an_out_of_scope_descendant_leaks_nothing(c, monkeypatch):
    import agent_sessions.routes.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod.project_dirs, "effective_roots", lambda: ["/home/user/claude"])
    lineage.link(K3, K1, "launch")  # 3333 lives under /tmp/other: outside the root
    _needs_you(K3)
    page = c.get("/api/sessions?limit=50").json()
    assert K3 not in _ids(page)
    main = next(s for s in page["sessions"] if s["id"] == K1)
    assert main["child_count"] == 0 and main["descendant_count"] == 0
    assert main["rollup"] is None and main["attention_count"] == 0 and main["attention"] == []
    assert c.get(f"/api/sessions/{K1}/children").json()["total"] == 0
    assert c.get(f"/api/sessions/{K1}/attention").json()["total"] == 0
    assert c.get(f"/api/sessions/{K3}/children").status_code == 404


def test_lookup_names_the_parent_only_inside_the_boundary(c, monkeypatch):
    lineage.link(K2, K1, "launch")
    row = c.get(f"/api/sessions/{K2}").json()
    assert row["parent_key"] == K1 and row["lineage_source"] == "launch"
    main = c.get(f"/api/sessions/{K1}").json()
    assert main["child_count"] == 1 and "parent_key" not in main
    import agent_sessions.routes.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod.project_dirs, "effective_roots", lambda: ["/tmp/other"])
    lineage.link(K3, K1, "launch")
    row3 = c.get(f"/api/sessions/{K3}").json()
    assert "parent_key" not in row3


def test_project_count_matches_the_main_session_list(c):
    a = c.post("/api/projects", json={"name": "Repo A", "folders": [REPO_A]}, headers=c.hdr).json()
    lineage.link(K2, K1, "launch")
    projs = {p["id"]: p for p in c.get("/api/projects").json()["projects"]}
    total = c.get(f"/api/sessions?project={a['id']}&limit=200").json()["total"]
    assert projs[a["id"]]["session_count"] == total == 1


def test_an_archived_parent_makes_its_child_an_orphan_main_session(c):
    lineage.link(K2, K1, "launch")
    assert c.post(f"/api/sessions/{K1}/archive", headers=c.hdr).status_code == 200
    page = c.get("/api/sessions?limit=50").json()
    orphan = next(s for s in page["sessions"] if s["id"] == K2)
    assert orphan["orphan"] is True and "parent_key" not in orphan


# --- archive with subagents --------------------------------------------------------------------


def test_archive_with_subagents_archives_each_and_reports_per_key(c):
    lineage.link(K2, K1, "launch")
    r = c.post(
        f"/api/sessions/{K1}/archive-with-subagents",
        json={"subagents": [K2, K5]},
        headers=c.hdr,
    )
    assert r.status_code == 200
    results = {x["id"]: x for x in r.json()["results"]}
    assert results[K1]["result"] == "archived"
    assert results[K2]["result"] == "archived"
    # K5 was never a subagent of K1: re-validated, skipped, untouched.
    assert results[K5] == {"id": K5, "result": "skipped", "reason": "no longer a subagent"}
    live = _ids(c.get("/api/sessions?limit=50").json())
    assert K1 not in live and K2 not in live and K5 in live


def test_archive_with_subagents_stops_when_the_main_session_is_refused(c, monkeypatch):
    import agent_sessions.routes.sessions as sessions_mod

    lineage.link(K2, K1, "launch")
    real = sessions_mod._reserve_or_refuse

    def refuse(key):
        if key == K1:
            raise HTTPException(status_code=409, detail="a mission holds this session")
        return real(key)

    monkeypatch.setattr(sessions_mod, "_reserve_or_refuse", refuse)
    r = c.post(
        f"/api/sessions/{K1}/archive-with-subagents", json={"subagents": [K2]}, headers=c.hdr
    )
    out = r.json()["results"]
    assert out[0] == {"id": K1, "result": "skipped", "reason": "a mission holds this session"}
    assert out[1]["result"] == "skipped"
    m2 = metadata.load().get(K2)
    assert m2 is None or m2.archived is not True  # K2 untouched


def test_archive_with_subagents_reports_a_child_failure_without_retrying(c, monkeypatch):
    import agent_sessions.routes.sessions as sessions_mod

    lineage.link(K2, K1, "launch")
    calls = []
    real = sessions_mod._archive_reserved

    async def flaky(prov, native, key):
        calls.append(key)
        if key == K2:
            raise HTTPException(status_code=503, detail="teardown failed")
        return await real(prov, native, key)

    monkeypatch.setattr(sessions_mod, "_archive_reserved", flaky)
    r = c.post(
        f"/api/sessions/{K1}/archive-with-subagents", json={"subagents": [K2]}, headers=c.hdr
    )
    results = r.json()["results"]
    assert [x["result"] for x in results] == ["archived", "failed"]
    assert results[1]["reason"] == "teardown failed"
    assert calls.count(K2) == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"subagents": "x"}, {"subagents": [1]}, {"subagents": [K2, K2]}, {"subagents": [K1]}],
)
def test_archive_with_subagents_validates_its_body(c, body):
    r = c.post(f"/api/sessions/{K1}/archive-with-subagents", json=body, headers=c.hdr)
    assert r.status_code == 422


def test_archive_with_subagents_needs_csrf(c):
    r = c.post(f"/api/sessions/{K1}/archive-with-subagents", json={"subagents": []})
    assert r.status_code in (401, 403)


# --- main-only consumers -----------------------------------------------------------------------


def test_pulse_main_only_drops_children_and_needs_you_keeps_them(fake_jsonl):
    from agent_sessions import pulse, pulse_chat

    lineage.link(K2, K1, "launch")
    all_ids = {c["id"] for c in pulse.build_cards(window_days=None)}
    main_ids = {c["id"] for c in pulse.build_cards(window_days=None, main_only=True)}
    assert K2 in all_ids and K2 not in main_ids and K1 in main_ids
    assert K2 not in {c["id"] for c in pulse_chat.build_catalog()}


def test_ai_review_sweep_skips_children(fake_jsonl, monkeypatch):
    from agent_sessions import ai_review_loop

    monkeypatch.setattr(ai_review_loop, "_api_sessions", lambda: [])
    lineage.link(K2, K1, "launch")
    ranked, live, _ = ai_review_loop._rank([K1, K2, K3])
    keys = [k for k, _m in ranked]
    assert K2 not in keys and K1 in keys and K3 in keys
    assert K2 in live  # still known to the sweep, just not reviewed by it


def test_attribution_loop_is_registered_in_the_lifespan():
    src = Path(__file__).resolve().parents[1] / "src" / "agent_sessions" / "main.py"
    text = src.read_text()
    assert "lineage_attribution.run()" in text and "lineage_task," in text
