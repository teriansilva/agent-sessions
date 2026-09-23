"""Endpoint tests for the sidebar-UX surface: pagination, projects, rename,
archive/unarchive, new-session — including CSRF/origin gating."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent_sessions.auth import AuthConfig
from agent_sessions.main import create_app
from agent_sessions.routes import upload as upload_route


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
    # CSRF token comes from the SPA bootstrap endpoint (/api/config).
    return c.get("/api/config").json()["csrf"]


# ---- pagination ---------------------------------------------------------------


def test_sessions_paginated_shape(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions?limit=2&offset=0")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"sessions", "next_offset", "total", "facets", "live_total"}
    assert len(d["sessions"]) == 2
    # 4 live sessions in the fixture (1 archived excluded) → next_offset advances
    assert d["total"] == 4
    assert d["next_offset"] == 2
    # newest-first by mtime
    mtimes = [s["last_mtime"] for s in d["sessions"]]
    assert mtimes == sorted(mtimes, reverse=True)


def test_sessions_archived_filter(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    active = c.get("/api/sessions?archived=0").json()
    archived = c.get("/api/sessions?archived=1").json()
    assert all(not s["archived"] for s in active["sessions"])
    assert all(s["archived"] for s in archived["sessions"])
    assert archived["total"] == 1  # the one archived fixture


def test_archived_claude_session_stays_archived_when_live_jsonl_recreated(auth_cfg, fake_jsonl):
    """#194: archiving a claude session must stick even if a still-running agent recreates
    its JSONL under projects/ after the move. The sidecar flag (sticky) + scanner dedup keep
    it in the archived scope, exactly once — it must not bounce back into the active list."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "11111111-1111-1111-1111-111111111111"  # starts live in the fixture

    r = c.post(f"/api/sessions/claude:{uuid}/archive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True

    # Simulate the live agent recreating its JSONL under projects/ after the archive move.
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{uuid}.jsonl").write_text('{"cwd": "/home/user/claude/repo/a"}\n')

    active = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    archived = [
        s
        for s in c.get("/api/sessions?archived=1&limit=50").json()["sessions"]
        if s["uuid"] == uuid
    ]
    assert uuid not in active  # did NOT bounce back to active
    assert len(archived) == 1  # stays archived, exactly once (no cross-tree duplicate)

    # Unarchive clears the sticky flag → it returns to the active list.
    r = c.post(f"/api/sessions/claude:{uuid}/unarchive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is False
    active2 = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    assert uuid in active2


# ---- filters: search / project / engine --------------------------------------

# Fixture project keys (project_alias unset → key == cwd).
_REPO_A = "/home/user/claude/repo/a"  # sessions 1111 + 2222
_TMP_OTHER = "/tmp/other"  # session 3333
_DEMOAPP = "/home/user/claude/demoapp.io"  # session 5555
_OLD = "/home/user/claude/old"  # archived 4444


def test_search_by_title_substring_and_case_insensitive(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # "second" is the title of session 2222 only.
    d = c.get("/api/sessions?q=SeCoNd&limit=50").json()
    assert d["total"] == 1
    assert d["sessions"][0]["uuid"] == "22222222-2222-2222-2222-222222222222"
    # substring match against "first message on repo-a"
    d = c.get("/api/sessions?q=message&limit=50").json()
    assert {s["uuid"] for s in d["sessions"]} == {"11111111-1111-1111-1111-111111111111"}


def test_search_trims_and_empty_is_no_filter(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/sessions?q=%20%20second%20%20&limit=50").json()["total"] == 1
    # whitespace-only q is treated as no filter → all 4 live sessions
    assert c.get("/api/sessions?q=%20%20%20&limit=50").json()["total"] == 4


# ---- meaningless first-message title normalization (#284) ---------------------


def test_meaningless_first_message_normalizes_title_but_stays_searchable(auth_cfg, fake_jsonl):
    """#284: a freshly-created session whose first user record is a stray "a" must not
    surface "a" as its name — the API emits ``title == ""`` (the per-surface placeholder
    fills in client-side) — while the raw first message is retained on the row and stays
    searchable, so the normalization never narrows search results."""
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-fresh"
    proj.mkdir(parents=True)
    uuid = "66666666-6666-6666-6666-666666666666"
    (proj / f"{uuid}.jsonl").write_text(
        '{"type":"user","cwd":"/home/user/claude/fresh","message":{"content":"a"}}\n'
    )
    c = _client(auth_cfg)
    _login(c, auth_cfg)

    row = next(s for s in c.get("/api/sessions?limit=50").json()["sessions"] if s["uuid"] == uuid)
    assert row["title"] == ""  # NOT the raw "a"
    assert row["first_user_message"] == "a"  # raw value kept for search/diagnostics

    # Still findable by the raw first message even though the display title is "" — the
    # OLD predicate (title-only) would have dropped it, so this guards the search fix.
    found = c.get("/api/sessions?q=a&limit=50").json()
    assert uuid in {s["uuid"] for s in found["sessions"]}


def test_codex_long_first_message_stays_searchable_beyond_title_cap(
    auth_cfg, fake_jsonl, tmp_path, monkeypatch
):
    """#670 (Hermes on PR #672): the stored first_user_message is the search haystack and
    must stay RAW — title normalization (first line, 120 cap) happens at display time only.
    A term that appears after the first line / 120th character must still find the session."""
    croot = tmp_path / "codex-sessions" / "2026" / "05" / "15"
    croot.mkdir(parents=True)
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(tmp_path / "codex-sessions"))
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec777"
    prompt = "fix the widget " + "x" * 150 + "\nneedle-beyond-cap on the second line"
    recs = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": "/home/u/p"}},
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": prompt},
        },
    ]
    (croot / f"rollout-2026-05-15T15-33-57-{uuid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n"
    )
    c = _client(auth_cfg)
    _login(c, auth_cfg)

    row = next(s for s in c.get("/api/sessions?limit=50").json()["sessions"] if s["uuid"] == uuid)
    assert row["first_user_message"] == prompt  # raw, both lines
    assert row["title"] == ("fix the widget " + "x" * 150)[:120]  # bounded display title

    found = c.get("/api/sessions?q=needle-beyond-cap&limit=50").json()
    assert uuid in {s["uuid"] for s in found["sessions"]}


def test_one_char_manual_rename_survives_in_row(auth_cfg, fake_jsonl):
    """#284: the meaningfulness rule applies ONLY to auto-derived titles — a user's
    deliberate one-character rename is authoritative and renders verbatim."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(
        f"/api/sessions/{uuid}/rename",
        json={"title": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["title"] == "x"
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["title"] == "x"


def test_filter_by_project(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get(f"/api/sessions?project={_REPO_A}&limit=50").json()
    assert d["total"] == 2
    assert all(
        s["project"] == {"kind": "folder", "id": _REPO_A, "name": _REPO_A} for s in d["sessions"]
    )


def test_filter_by_engine(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/sessions?engine=claude&limit=50").json()["total"] == 4
    # no opencode sessions exist yet (#61) → empty
    assert c.get("/api/sessions?engine=opencode&limit=50").json()["total"] == 0


def test_filters_combine_with_and(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # project repo/a has 1111 ("first message on repo-a") + 2222 ("second");
    # only 1111's title contains "repo".
    d = c.get(f"/api/sessions?project={_REPO_A}&q=repo&limit=50").json()
    assert d["total"] == 1
    assert d["sessions"][0]["uuid"] == "11111111-1111-1111-1111-111111111111"


def test_no_match_is_empty_but_facets_remain(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?q=zzz-nothing-matches&limit=50").json()
    assert d["total"] == 0
    assert d["sessions"] == []
    assert d["next_offset"] is None
    # Facets are computed over the full archived-scoped set, so they survive a zero-match
    # filter (the dropdown must still offer every project). With no project entities, the four
    # live unadopted sessions fold into the synthetic Default project (#445).
    assert d["facets"]["projects"] == [
        {"kind": "project", "id": "__default__", "name": "Default", "color": "", "count": 4}
    ]


# ---- filtered pagination ------------------------------------------------------


def test_filtered_pagination_stays_within_results(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # project repo/a matches exactly 2; page through it one at a time.
    p0 = c.get(f"/api/sessions?project={_REPO_A}&limit=1&offset=0").json()
    assert p0["total"] == 2 and len(p0["sessions"]) == 1 and p0["next_offset"] == 1
    p1 = c.get(f"/api/sessions?project={_REPO_A}&limit=1&offset=1").json()
    assert p1["total"] == 2 and len(p1["sessions"]) == 1 and p1["next_offset"] is None
    # the two pages together cover both sessions, no overlap
    seen = {p0["sessions"][0]["uuid"], p1["sessions"][0]["uuid"]}
    assert seen == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }


# ---- facets -------------------------------------------------------------------


def test_facets_cover_full_set_beyond_first_page(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # One row loaded, but the Default count covers every live unadopted session (full-set
    # facet computation, #445): the dropdown's count isn't limited to the loaded page.
    d = c.get("/api/sessions?limit=1&offset=0").json()
    assert len(d["sessions"]) == 1
    assert d["facets"]["projects"] == [
        {"kind": "project", "id": "__default__", "name": "Default", "color": "", "count": 4}
    ]
    assert d["facets"]["engines"] == ["claude"]


def test_facets_scoped_by_archived(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?archived=1&limit=50").json()
    # The single archived session is unadopted → the Default aggregate, scoped to the archived view.
    assert d["facets"]["projects"] == [
        {"kind": "project", "id": "__default__", "name": "Default", "color": "", "count": 1}
    ]
    assert d["facets"]["engines"] == ["claude"]


# ---- #445: project facets list ENTITIES + a synthetic Default catch-all ------------


def test_facets_list_project_entities_plus_default(auth_cfg, fake_jsonl):
    """The project dropdown lists project ENTITIES (including empty ones), with unadopted
    sessions aggregated under the synthetic Default — never per-folder cwd entries. User
    projects sort first (by name), Default last."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Adopt repo/a (its 2 sessions resolve to the entity); add a folderless empty project too.
    a = c.post("/api/projects", json={"name": "Repo A", "folders": [_REPO_A]}, headers=hdr).json()[
        "id"
    ]
    empty = c.post("/api/projects", json={"name": "Empty"}, headers=hdr).json()["id"]
    facets = c.get("/api/sessions?limit=200").json()["facets"]["projects"]
    # user entities first (alpha), Default last; the 0-count empty project is still listed.
    assert facets == [
        {"kind": "project", "id": empty, "name": "Empty", "color": "", "count": 0},
        {"kind": "project", "id": a, "name": "Repo A", "color": "", "count": 2},
        {"kind": "project", "id": "__default__", "name": "Default", "color": "", "count": 2},
    ]


def test_filter_by_default_project(auth_cfg, fake_jsonl):
    """project=__default__ matches exactly the unadopted (folder-fallback) rows; adopted
    sessions are excluded. Bare-cwd filtering still works for back-compat (#445)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post("/api/projects", json={"name": "Repo A", "folders": [_REPO_A]}, headers=hdr)
    d = c.get("/api/sessions?project=__default__&limit=200").json()
    # the 2 unadopted live cwds (tmp/other + demoapp), not the 2 adopted repo/a rows
    assert {s["cwd"] for s in d["sessions"]} == {_TMP_OTHER, _DEMOAPP}
    assert all(s["project"]["kind"] == "folder" for s in d["sessions"])
    # bare-cwd back-compat still selects a launch folder directly
    assert c.get(f"/api/sessions?project={_TMP_OTHER}&limit=200").json()["total"] == 1


def test_default_filter_respects_included_mode(auth_cfg, fake_jsonl):
    """Default obeys folder visibility — project=__default__ under an included-mode allowlist
    returns only the visible unadopted rows, never bypassing curation (#445)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [_TMP_OTHER]},
        headers=hdr,
    )
    d = c.get("/api/sessions?project=__default__&limit=200").json()
    assert {s["cwd"] for s in d["sessions"]} == {_TMP_OTHER}  # demoapp + repo/a curated out


# ---- projects picker ----------------------------------------------------------


# ---- #567: per-project count == filtered-list total (shared scope) -------------------


def test_project_session_count_matches_filtered_list(auth_cfg, fake_jsonl):
    """#567: the Settings per-project ``session_count`` must equal the list's ``total`` for
    that project — the badge and the list share ONE scope (active + in-scope + visible).
    Before the fix the count also folded in archived sessions, overstating the badge."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Adopt repo/a → its two live sessions (1111, 2222) resolve to the entity.
    a = c.post("/api/projects", json={"name": "Repo A", "folders": [_REPO_A]}, headers=hdr).json()[
        "id"
    ]
    # Archive one member: it must drop from BOTH the list and the badge count.
    r = c.post("/api/sessions/claude:11111111-1111-1111-1111-111111111111/archive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True

    projs = {p["id"]: p for p in c.get("/api/projects").json()["projects"]}
    assert projs[a]["session_count"] == 1  # only the live member — not the archived one

    # The invariant that pins them together: every project's badge == its list total.
    for pid, p in projs.items():
        total = c.get(f"/api/sessions?project={pid}&limit=200").json()["total"]
        assert p["session_count"] == total, (pid, p["name"])


def test_project_count_drops_excluded_prefix_like_the_list(auth_cfg, fake_jsonl):
    """An excluded prefix drops an adopted project's sessions from the list; the badge count
    must drop with it — exclusion > curation, same as the list (#567/#465)."""
    import agent_sessions.routes.sessions as sessions_mod
    from agent_sessions import prefs

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    a = c.post("/api/projects", json={"name": "Repo A", "folders": [_REPO_A]}, headers=hdr).json()[
        "id"
    ]
    prefs.set_folder_exclusions([_REPO_A])
    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        projs = {p["id"]: p for p in c.get("/api/projects").json()["projects"]}
        total = c.get(f"/api/sessions?project={a}&limit=200").json()["total"]
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    assert projs[a]["session_count"] == total == 0  # excluded → dropped from both


def test_projects_endpoint(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/folders")
    assert r.status_code == 200
    cwds = {p["cwd"] for p in r.json()["folders"]}
    assert "/tmp/other" in cwds


def test_projects_endpoint_includes_all_engines(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    """#196: /api/projects must offer cwds from ALL engines — the Settings 'Session
    overview' manager has to list every project the sidebar filter can show, and the
    filter derives its options from the all-engine /api/sessions facets. Sourcing the
    manager from the Claude-only scan let opencode/gemini cwds drift between the two."""
    from agent_sessions import engines
    from agent_sessions.scanner import Session

    oc = Session(
        engine="opencode",
        uuid="ses_oconly",
        cwd="/tmp/oc-only",
        last_mtime=1.0,
        first_user_message="hi",
        archived=False,
    )
    monkeypatch.setattr(engines, "scan_all", lambda: [oc])

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # The opencode-only session folds into the synthetic Default project (it's unadopted), so
    # it still reaches the all-engine facet aggregate (#196/#445) …
    facets = c.get("/api/sessions?limit=100").json()["facets"]
    default = next(p for p in facets["projects"] if p["id"] == "__default__")
    assert default["count"] == 1
    # … and is therefore manageable via /api/folders (previously Claude-only → it drifted).
    picker = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    assert "/tmp/oc-only" in picker


# ---- #174: server-side hide propagates to /api/sessions + /api/projects -----


def test_hidden_projects_filtered_from_sessions_and_facets(auth_cfg, fake_jsonl, tmp_home):
    """A hidden cwd disappears from `/api/sessions` rows, totals, and the project facet —
    so the sidebar's pagination + filter dropdown describe the visible set, not the
    full one (Hermes #174 review: client-only filtering would make totals lie)."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # Sanity: before hiding, /tmp/other is present and folds into Default (#445).
    before = c.get("/api/sessions?limit=100").json()
    assert "/tmp/other" in {row["cwd"] for row in before["sessions"]}
    before_default = next(p for p in before["facets"]["projects"] if p["id"] == "__default__")
    pre_total = before["total"]

    # Hide it. An unadopted row keeps kind=="folder" and still obeys folder visibility, so the
    # hidden cwd leaves the rows, the total, AND the Default aggregate count — Default never
    # bypasses the curation (#445).
    prefs.set_projects_hidden(["/tmp/other"])
    after = c.get("/api/sessions?limit=100").json()
    assert "/tmp/other" not in {row["cwd"] for row in after["sessions"]}
    after_default = next(p for p in after["facets"]["projects"] if p["id"] == "__default__")
    assert after_default["count"] == before_default["count"] - 1
    assert after["total"] == pre_total - 1


def test_hidden_projects_filtered_from_projects_endpoint(auth_cfg, fake_jsonl, tmp_home):
    """The new-session picker (`/api/projects`) must also drop hidden cwds — the user
    said they don't want to see this project anywhere."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert "/tmp/other" in {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    prefs.set_projects_hidden(["/tmp/other"])
    assert "/tmp/other" not in {p["cwd"] for p in c.get("/api/folders").json()["folders"]}


def test_legacy_overview_excluded_post_is_retired_422(auth_cfg, fake_jsonl, tmp_home):
    """The legacy `overview_excluded` write alias is retired (#357 Phase 2): no client
    has sent it since #174, so the key now falls through to the no-known-key 422 — and
    it must never reach the `projects_hidden` storage."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"overview_excluded": ["/tmp/other"]},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
    assert prefs.get_projects_hidden() == []


# ---- rename -------------------------------------------------------------------


def test_rename_persists(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(
        f"/api/sessions/{uuid}/rename",
        json={"title": "My Refactor"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["title"] == "My Refactor"
    # reflected in the list
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["title"] == "My Refactor"


def test_rename_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "x"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_rename_empty_title_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/11111111-1111-1111-1111-111111111111/rename",
        json={"title": "   "},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- favorite / unfavorite (#122) ---------------------------------------------


def test_favorite_persists_and_pins_to_top(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "22222222-2222-2222-2222-222222222222"

    r = c.post(f"/api/sessions/claude:{uuid}/favorite", headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"id": f"claude:{uuid}", "sticky": True}

    # The single favorited row floats to the very top (sticky-first sort) and carries
    # sticky=True; every other row is non-sticky.
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert rows[0]["uuid"] == uuid and rows[0]["sticky"] is True
    assert all(not s["sticky"] for s in rows[1:])

    # Unfavorite clears the flag; it no longer pins.
    r = c.post(f"/api/sessions/claude:{uuid}/unfavorite", headers=hdr)
    assert r.status_code == 200 and r.json()["sticky"] is False
    rows2 = c.get("/api/sessions?limit=50").json()["sessions"]
    assert all(not s["sticky"] for s in rows2)


def test_favorite_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/claude:22222222-2222-2222-2222-222222222222/favorite",
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_favorite_unknown_engine_404(auth_cfg, fake_jsonl):
    # parse_key is the validation gate (like the other session routes): an unknown
    # engine id never reaches the sidecar.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/nosuchengine:whatever/favorite",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404


# ---- compose drafts (#477) ----------------------------------------------------

_DRAFT_SID = "claude:11111111-1111-1111-1111-111111111111"


def _upload_one(c, hdr) -> dict:
    """Upload a tiny file through the real route → a valid in-namespace attachment path."""
    r = c.post(
        "/api/upload",
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        headers=hdr,
    )
    assert r.status_code == 200
    return r.json()


def test_draft_save_get_clear_and_has_draft(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    # No draft initially.
    r = c.get(f"/api/sessions/{_DRAFT_SID}/draft")
    assert r.status_code == 200
    assert r.json()["text"] == "" and r.json()["attachments"] == []

    up = _upload_one(c, hdr)
    body = {"text": "work in progress", "attachments": [{"name": up["name"], "path": up["path"]}]}
    r = c.put(f"/api/sessions/{_DRAFT_SID}/draft", json=body, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"id": _DRAFT_SID, "has_draft": True}

    got = c.get(f"/api/sessions/{_DRAFT_SID}/draft").json()
    assert got["text"] == "work in progress"
    assert len(got["attachments"]) == 1
    assert got["attachments"][0]["name"] == up["name"]
    assert got["attachments"][0]["path"].endswith("shot.png")
    assert got["updated_at"] is not None

    # The cheap has_draft flag surfaces on the session row.
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["id"] == _DRAFT_SID)["has_draft"] is True

    # Empty text + no attachments clears it.
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft", json={"text": "", "attachments": []}, headers=hdr
    )
    assert r.status_code == 200 and r.json() == {"id": _DRAFT_SID, "has_draft": False}
    assert c.get(f"/api/sessions/{_DRAFT_SID}/draft").json()["text"] == ""
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["id"] == _DRAFT_SID)["has_draft"] is False


def test_draft_put_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft",
        json={"text": "x", "attachments": []},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_draft_get_requires_login(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)  # no login
    r = c.get(f"/api/sessions/{_DRAFT_SID}/draft")
    assert r.status_code in (401, 403)


def test_draft_unknown_engine_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.put(
        "/api/sessions/nosuchengine:whatever/draft",
        json={"text": "x", "attachments": []},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404


def test_draft_rejects_out_of_namespace_attachments(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    # Absolute path outside the upload namespace.
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft",
        json={"text": "x", "attachments": [{"name": "p", "path": "/etc/passwd"}]},
        headers=hdr,
    )
    assert r.status_code == 422

    # Parent-traversal that escapes the namespace.
    trav = str(Path(fake_jsonl) / ".agent-sessions" / "uploads" / ".." / ".." / "secret")
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft",
        json={"text": "x", "attachments": [{"name": "p", "path": trav}]},
        headers=hdr,
    )
    assert r.status_code == 422

    # Nothing persisted by the rejected writes.
    assert c.get(f"/api/sessions/{_DRAFT_SID}/draft").json()["text"] == ""


def test_draft_rejects_too_many_attachments(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    atts = [{"name": "a", "path": "/x/a"} for _ in range(51)]
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft", json={"text": "x", "attachments": atts}, headers=hdr
    )
    assert r.status_code == 422


def test_draft_rejects_oversized_text(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.put(
        f"/api/sessions/{_DRAFT_SID}/draft",
        json={"text": "x" * 100_001, "attachments": []},
        headers=hdr,
    )
    assert r.status_code == 422


# ---- archive / unarchive ------------------------------------------------------


def test_archive_endpoint_moves_and_filters(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(
        f"/api/sessions/{uuid}/archive", headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    )
    assert r.status_code == 200
    active = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    assert uuid not in active
    archived = {s["uuid"] for s in c.get("/api/sessions?archived=1&limit=50").json()["sessions"]}
    assert uuid in archived


def test_archive_unknown_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/99999999-9999-9999-9999-999999999999/archive",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404


# ---- opencode engine (#12) ----------------------------------------------------

_OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
_OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"  # opencode.db time_archived set (native)


def test_opencode_rows_appear_with_engine_facet(auth_cfg, fake_jsonl, opencode_db):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?limit=200").json()
    assert {"claude", "opencode"} <= set(d["facets"]["engines"])
    oc = [s for s in d["sessions"] if s["engine"] == "opencode"]
    assert oc and all(s["id"].startswith("opencode:ses_") for s in oc)
    # engine filter narrows to opencode only
    only = c.get("/api/sessions?engine=opencode&limit=200").json()
    assert only["total"] >= 1 and all(s["engine"] == "opencode" for s in only["sessions"])


def test_archive_opencode_via_sidecar(auth_cfg, fake_jsonl, opencode_db):
    # Archive flips the engine-agnostic sidecar flag (never opencode.db), so an
    # opencode session moves to the archived view and back, db left untouched.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    before = opencode_db.read_bytes()

    r = c.post(f"/api/sessions/opencode:{_OC_TOP}/archive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True
    active = c.get("/api/sessions?engine=opencode&archived=0&limit=200").json()["sessions"]
    assert all(s["id"] != f"opencode:{_OC_TOP}" for s in active)
    arch = c.get("/api/sessions?engine=opencode&archived=1&limit=200").json()["sessions"]
    assert any(s["id"] == f"opencode:{_OC_TOP}" for s in arch)

    r = c.post(f"/api/sessions/opencode:{_OC_TOP}/unarchive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is False
    assert opencode_db.read_bytes() == before  # opencode.db untouched throughout


def test_favorite_opencode_via_sidecar(auth_cfg, fake_jsonl, opencode_db):
    # Favorite is engine-agnostic sidecar metadata (#122): it flips `sticky` for an
    # opencode session and pins it, never writing opencode.db.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    before = opencode_db.read_bytes()

    r = c.post(f"/api/sessions/opencode:{_OC_TOP}/favorite", headers=hdr)
    assert r.status_code == 200 and r.json() == {"id": f"opencode:{_OC_TOP}", "sticky": True}
    rows = c.get("/api/sessions?engine=opencode&limit=200").json()["sessions"]
    assert rows[0]["id"] == f"opencode:{_OC_TOP}" and rows[0]["sticky"] is True

    r = c.post(f"/api/sessions/opencode:{_OC_TOP}/unfavorite", headers=hdr)
    assert r.status_code == 200 and r.json()["sticky"] is False
    assert opencode_db.read_bytes() == before  # opencode.db untouched throughout


def test_unarchive_natively_archived_opencode(auth_cfg, fake_jsonl, opencode_db):
    # A row archived in opencode.db (time_archived set) must be unarchivable: the
    # sidecar override (tri-state) wins over the native archived state in both
    # directions. Regression for the "or" bug (s.archived or m.archived).
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # starts in the archived view (native time_archived)
    arch = c.get("/api/sessions?engine=opencode&archived=1&limit=200").json()["sessions"]
    assert any(s["id"] == f"opencode:{_OC_ARCHIVED}" for s in arch)

    r = c.post(f"/api/sessions/opencode:{_OC_ARCHIVED}/unarchive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is False
    # now active, and gone from the archived view
    active = c.get("/api/sessions?engine=opencode&archived=0&limit=200").json()["sessions"]
    assert any(s["id"] == f"opencode:{_OC_ARCHIVED}" for s in active)
    arch = c.get("/api/sessions?engine=opencode&archived=1&limit=200").json()["sessions"]
    assert all(s["id"] != f"opencode:{_OC_ARCHIVED}" for s in arch)


def test_rename_opencode_is_sidecar_overlay(auth_cfg, fake_jsonl, opencode_db):
    # Rename writes our engine-agnostic sidecar (metadata.json), never opencode.db,
    # so it's allowed for opencode and persists in the list — and opencode.db is
    # left byte-for-byte untouched (the read-only-to-opencode guarantee).
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    before = opencode_db.read_bytes()
    r = c.post(
        f"/api/sessions/opencode:{_OC_TOP}/rename",
        json={"title": "renamed via sidebar"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["title"] == "renamed via sidebar"
    rows = c.get("/api/sessions?engine=opencode&limit=200").json()["sessions"]
    row = next(s for s in rows if s["id"] == f"opencode:{_OC_TOP}")
    assert row["title"] == "renamed via sidebar"
    assert opencode_db.read_bytes() == before  # opencode.db untouched


# ---- upload (paste/drop context) ----------------------------------------------


def test_upload_saves_to_shared_dir_and_returns_path(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/upload",
        files={"file": ("My Shot!.png", b"\x89PNG\r\n\x1a\n fake png bytes", "image/png")},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    p = Path(r.json()["path"])
    assert p.parent == tmp_home / ".agent-sessions" / "uploads"
    assert p.read_bytes().startswith(b"\x89PNG")
    # filename sanitised: no spaces / punctuation that could fight the shell or path
    assert re.fullmatch(r"\d{8}-\d{6}(-\d+)?-My_Shot_.png", p.name)


def test_upload_requires_csrf(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/upload",
        files={"file": ("x.txt", b"hi", "text/plain")},
        headers={"Origin": auth_cfg.origin},  # no X-CSRF-Token
    )
    assert r.status_code == 403


def test_upload_dir_is_0700_and_file_is_0600_from_first_write(auth_cfg, tmp_home):
    """Modes come from the code, not from the umask (#612 Phase 4).

    The umask is set as permissively as possible (0) for the duration, so a route that relied on
    the process default would produce 0755/0644 here and fail. That is the point: it makes the
    old `mkdir()` + `write_bytes()` shape red rather than accidentally green on a host whose
    umask happened to be tight.

    The umask is process-wide, so restoring it is not optional — a leak would leave every later
    test creating world-writable files. An earlier revision of this test also monkeypatched
    `os.umask` itself, which meant the `finally` called the *stub* and the process stayed at
    `000` (caught by Hermes in review). There is nothing to stub: the route never calls
    `umask`, it sets modes explicitly, which is the property under test.
    """
    old = os.umask(0)
    try:
        c = _client(auth_cfg)
        csrf = _login(c, auth_cfg)
        r = c.post(
            "/api/upload",
            files={"file": ("secret.log", b"token=abc123", "text/plain")},
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        )
        assert r.status_code == 200
        p = Path(r.json()["path"])
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    finally:
        os.umask(old)


def test_upload_mode_test_does_not_leak_the_process_umask(auth_cfg, tmp_home):
    """The umask this file borrows is given back — pinned, not assumed.

    A leaked `umask(0)` is invisible where it happens and surfaces as an unrelated test
    creating permissive files much later, which is about the worst debugging shape there is.
    This runs the mode test's own body and then asserts the process umask is back, so the leak
    cannot return unnoticed. Reintroducing the stub makes it fail with exactly the message
    review reported: `leaked process umask 0o0`.
    """
    before = os.umask(0o022)
    os.umask(before)  # read-only probe: umask() has no getter
    test_upload_dir_is_0700_and_file_is_0600_from_first_write(auth_cfg, tmp_home)
    after = os.umask(0o022)
    os.umask(after)
    assert after == before, f"upload mode test leaked process umask {oct(after)}"


def test_upload_corrects_a_pre_existing_world_readable_dir(auth_cfg, tmp_home):
    """An install that already has a 0755 uploads dir gets it tightened, not left alone.

    `mkdir(exist_ok=True)` ignores its `mode` argument when the directory exists, so every
    install created before this change would otherwise keep its old mode forever. This is the
    upgrade path, and it is the half a fresh-install test cannot cover.
    """
    stale = tmp_home / ".agent-sessions" / "uploads"
    stale.mkdir(parents=True)
    stale.chmod(0o755)
    assert stat.S_IMODE(stale.stat().st_mode) == 0o755

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/upload",
        files={"file": ("x.txt", b"hi", "text/plain")},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert stat.S_IMODE(stale.stat().st_mode) == 0o700


def test_upload_same_name_same_second_does_not_clobber(auth_cfg, tmp_home):
    """Two uploads of one basename inside the same second keep both files.

    A behaviour-preservation guard for the rewrite: the loop moved from `while dest.exists()`
    to a retry around `O_CREAT | O_EXCL`, and this pins that same-second collisions still get
    distinct names. Freezing the timestamp makes the collision deterministic rather than hoping
    both requests land in the same second.

    It does **not** demonstrate the TOCTOU narrowing that `O_EXCL` also buys — the old shape
    passes this too, because sequentially the first file does exist by the time the second
    request checks. The race needs a creation landing *between* the old code's check and its
    write, which is not reachable through the route once the window is gone. That improvement is
    structural, and claimed as such rather than as something these assertions prove.
    """
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    with mock.patch.object(upload_route.time, "strftime", return_value="20260812-120000"):
        first = c.post(
            "/api/upload", files={"file": ("a.txt", b"first", "text/plain")}, headers=hdr
        )
        second = c.post(
            "/api/upload", files={"file": ("a.txt", b"second", "text/plain")}, headers=hdr
        )
    assert first.status_code == second.status_code == 200
    p1, p2 = Path(first.json()["path"]), Path(second.json()["path"])
    assert p1 != p2
    assert p1.read_bytes() == b"first"
    assert p2.read_bytes() == b"second"


def test_upload_empty_is_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/upload",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_api_config_returns_csrf_engines_backend(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_home / "bin"))
    claude_bin = tmp_home / ".local" / "bin" / "claude"
    claude_bin.parent.mkdir(parents=True)
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert d["csrf"]  # non-empty token for SPA mutations
    assert "claude" in d["new_session_engines"]
    assert d["terminal_backend"] == "ws"


def test_api_config_requires_auth(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    assert c.get("/api/config", follow_redirects=False).status_code in (401, 403)


def test_api_config_advertises_opencode_new_session(
    auth_cfg, fake_jsonl, opencode_db, tmp_home, monkeypatch
):
    # #127: opencode is offered in the new-session picker when a launchable binary resolves.
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    oc_bin = tmp_home / ".opencode" / "bin" / "opencode"
    oc_bin.parent.mkdir(parents=True)
    oc_bin.write_text("#!/bin/sh\n")
    oc_bin.chmod(0o755)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert "opencode" in d["new_session_engines"]


def test_api_config_advertises_binary_only_opencode_new_session(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    # Fresh opencode installs have a CLI before the first opencode.db exists.
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    oc_bin = tmp_home / ".opencode" / "bin" / "opencode"
    oc_bin.parent.mkdir(parents=True)
    oc_bin.write_text("#!/bin/sh\n")
    oc_bin.chmod(0o755)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert "opencode" in d["new_session_engines"]


def test_list_no_ghost_row_after_reconcile(auth_cfg, fake_jsonl, opencode_db):
    # The no-ghost-row invariant (#127 / #64): after reconcile, the list shows exactly ONE
    # row for the reconciled session — the real ses_… — and the placeholder never appears.
    # Metadata set while on the placeholder (a rename) follows the real row via the alias.
    from agent_sessions import metadata

    oc_top = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"  # the real ses_ already seeded by opencode_db
    placeholder_key = "opencode:new-22222222-2222-2222-2222-222222222222"
    real_key = f"opencode:{oc_top}"
    # Rename happened while the session was still under its placeholder, then reconcile
    # recorded the alias placeholder→real.
    metadata.patch(placeholder_key, title="named-before-converge")
    metadata.set_alias(placeholder_key, real_key)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/sessions?engine=opencode&limit=200").json()["sessions"]
    ids = [s["id"] for s in rows]
    # Exactly one row for the real id; no placeholder/ghost row.
    assert ids.count(real_key) == 1
    assert placeholder_key not in ids
    assert not any(s["id"].startswith("opencode:new-") for s in rows)
    # The placeholder-era title carried over to the real row (alias-resolved metadata).
    real_row = next(s for s in rows if s["id"] == real_key)
    assert real_row["title"] == "named-before-converge"


def test_safe_next_rejects_open_redirects():
    from agent_sessions.main import _safe_next

    assert _safe_next("/s/claude/abc") == "/s/claude/abc"
    assert _safe_next("/") == "/"
    assert _safe_next(None) == "/"
    assert _safe_next("") == "/"
    assert _safe_next("//evil.com") == "/"  # scheme-relative
    assert _safe_next("/\\evil.com") == "/"  # backslash host trick
    assert _safe_next("https://evil.com") == "/"  # absolute URL


def test_login_get_carries_sanitized_next(auth_cfg):
    c = _client(auth_cfg)
    assert "/s/claude/abc" in c.get("/login?next=/s/claude/abc").text
    assert "//evil.com" not in c.get("/login?next=//evil.com").text  # sanitized → "/"


def test_login_post_redirects_to_next(auth_cfg):
    c = _client(auth_cfg)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2", "next": "/s/claude/abc"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/s/claude/abc"


def test_login_post_blocks_open_redirect(auth_cfg):
    c = _client(auth_cfg)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2", "next": "//evil.com"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # not //evil.com


def test_api_version_returns_version(auth_cfg, fake_jsonl):
    import agent_sessions

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/version").json()
    assert d["version"] == agent_sessions.__version__


def test_api_version_requires_auth(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    assert c.get("/api/version", follow_redirects=False).status_code in (401, 403)


def test_update_check_authed(auth_cfg, fake_jsonl, monkeypatch):
    import agent_sessions.update as up

    monkeypatch.setattr(
        up,
        "check",
        lambda: {"current": "x", "channel": "stable", "latest": None, "update_available": False},
    )
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/update/check").json()["channel"] == "stable"


def test_update_apply_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/update/apply", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


def test_update_apply_202_then_503(auth_cfg, fake_jsonl, monkeypatch):
    import agent_sessions.update as up

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    monkeypatch.setattr(up, "_SPAWNED_AT", None)  # a real spawn elsewhere must not 409 this
    monkeypatch.setattr(up, "apply", lambda: True)
    assert c.post("/api/update/apply", headers=hdr).status_code == 202
    monkeypatch.setattr(up, "apply", lambda: False)  # not an install
    assert c.post("/api/update/apply", headers=hdr).status_code == 503


def test_update_apply_busy_409(auth_cfg, fake_jsonl, monkeypatch):
    # #538 single-flight: while a scheduled pass holds the lock, manual apply is refused.
    import agent_sessions.update as up

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert up._RUN_LOCK.acquire(blocking=False)
    try:
        assert c.post("/api/update/apply", headers=hdr).status_code == 409
    finally:
        up._RUN_LOCK.release()


def test_update_check_gains_additive_auto_update_fields(auth_cfg, fake_jsonl, monkeypatch):
    # #538: `auto_update` + `last_auto` ride along; the pre-#538 keys keep their names and
    # semantics (the SPA's manual-check flow depends on them).
    import agent_sessions.update as up

    monkeypatch.setattr(
        up,
        "check",
        lambda: {"current": "x", "channel": "stable", "latest": None, "update_available": False},
    )
    monkeypatch.setattr(up, "auto_update_enabled", lambda: True)
    monkeypatch.setattr(up, "last_auto", lambda: {"ts": 123.0, "result": "up-to-date"})
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/update/check").json()
    assert {"current", "channel", "latest", "update_available"} <= set(d)
    assert d["auto_update"] is True
    assert d["last_auto"] == {"ts": 123.0, "result": "up-to-date"}


def test_update_settings_get_cheap_and_authed(auth_cfg, fake_jsonl, monkeypatch, tmp_path):
    # The card mounts on every Settings visit — the GET must not hit the remote.
    import agent_sessions.update as up

    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.delenv("AGENT_SESSIONS_AUTOUPDATE", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_CHANNEL", raising=False)
    monkeypatch.setattr(up, "_LAST_AUTO", None)
    monkeypatch.setattr(
        up, "latest_ref", lambda *_: (_ for _ in ()).throw(AssertionError("remote hit"))
    )
    c = _client(auth_cfg)
    assert c.get("/api/update/settings", follow_redirects=False).status_code in (401, 403)
    _login(c, auth_cfg)
    d = c.get("/api/update/settings").json()
    assert d == {"auto_update": False, "channel": "stable", "last_auto": None}


def test_update_settings_post_validation_and_roundtrip(auth_cfg, fake_jsonl, monkeypatch, tmp_path):
    import agent_sessions.update as up

    envf = tmp_path / "env"
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # CSRF required (state-changing).
    r = c.post(
        "/api/update/settings", json={"auto_update": True}, headers={"Origin": auth_cfg.origin}
    )
    assert r.status_code == 403
    # Strict validation: no keys / wrong types / unknown channel never reach the env file.
    bad = [{}, {"auto_update": "yes"}, {"channel": "beta"}]
    for body in bad:
        assert c.post("/api/update/settings", json=body, headers=hdr).status_code == 422
    assert not envf.exists()
    # Round-trip: persists the two fixed keys; the live read sees them immediately.
    d = c.post(
        "/api/update/settings", json={"auto_update": True, "channel": "main"}, headers=hdr
    ).json()
    assert d["auto_update"] is True and d["channel"] == "main"
    text = envf.read_text()
    assert "AGENT_SESSIONS_AUTOUPDATE=1" in text and "AGENT_SESSIONS_CHANNEL=main" in text
    assert up.auto_update_enabled() is True and up._channel() == "main"


# ---- bulk archive: archive-older (#142) ---------------------------------------


def test_archive_older_archives_only_old_sessions(auth_cfg, fake_jsonl):
    import os
    import time

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Age session 1111 to 10h ago; 2222 stays fresh.
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / "11111111-1111-1111-1111-111111111111.jsonl", (old, old))

    r = c.post("/api/sessions/archive-older", json={"hours": 5}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["archived"] == 1  # only the 10h-old one

    active = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    assert "11111111-1111-1111-1111-111111111111" not in active  # archived away
    assert "22222222-2222-2222-2222-222222222222" in active  # fresh, untouched


def test_archive_older_skips_owned_background_agent(auth_cfg, fake_jsonl, monkeypatch):
    # #631: archive-older must not move a Claude transcript a live process owns (a background
    # agent), even when it's old enough to sweep — it's skipped and stays in the live tree.
    import os
    import time

    from agent_sessions.routes import sessions as sroutes

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    owned = "11111111-1111-1111-1111-111111111111"
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / f"{owned}.jsonl", (old, old))
    monkeypatch.setattr(
        sroutes.transcript_owner, "transcript_is_owned", lambda uuid, **kw: uuid == owned
    )

    body = c.post("/api/sessions/archive-older", json={"hours": 5}, headers=hdr).json()
    assert body["archived"] == 0 and body["skipped"] >= 1  # the owned bg agent was skipped
    active = {s["uuid"] for s in c.get("/api/sessions?archived=0&limit=50").json()["sessions"]}
    assert owned in active  # still live
    assert list((fake_jsonl / ".claude" / "projects").glob(f"*/{owned}.jsonl"))  # JSONL not moved


def test_archive_older_releases_its_reservation_on_the_skip_path(auth_cfg, fake_jsonl, monkeypatch):
    """A session this request explicitly DECLINED to touch must not be left locked.

    The sweep took a reservation, then `continue`d past the only release on the background-agent
    branch — so an ordinary archive/unarchive/adopt of that session was refused for the whole
    reservation window, on behalf of an operation that had done nothing to it.
    """
    import os
    import time

    from agent_sessions import missions
    from agent_sessions.routes import sessions as sroutes

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    owned = "11111111-1111-1111-1111-111111111111"
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / f"{owned}.jsonl", (old, old))
    monkeypatch.setattr(
        sroutes.transcript_owner, "transcript_is_owned", lambda uuid, **kw: uuid == owned
    )

    body = c.post("/api/sessions/archive-older", json={"hours": 5}, headers=hdr).json()
    assert body["skipped"] >= 1
    assert missions.reservation_of(f"claude:{owned}") is None, "sweep leaked its reservation"
    # …and the proof that it matters: once the background agent is gone, the session is operable
    # immediately, rather than after the reservation window the sweep had no business opening.
    monkeypatch.setattr(sroutes.transcript_owner, "transcript_is_owned", lambda uuid, **kw: False)
    r = c.post(f"/api/sessions/claude:{owned}/archive", headers=hdr)
    assert r.status_code == 200, r.text


def test_archive_older_releases_its_reservation_when_the_provider_raises(
    auth_cfg, fake_jsonl, monkeypatch
):
    """The skip path was the reported leak; the ERROR path is the same shape one branch over."""
    import os
    import time

    from agent_sessions import engines, missions

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uid = "11111111-1111-1111-1111-111111111111"
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / f"{uid}.jsonl", (old, old))

    from agent_sessions import archive as archive_mod

    def boom(self, native):
        raise archive_mod.ArchiveError("nope")

    monkeypatch.setattr(engines.ClaudeProvider, "archive", boom)
    body = c.post("/api/sessions/archive-older", json={"hours": 5}, headers=hdr).json()
    assert body["skipped"] >= 1
    assert missions.reservation_of(f"claude:{uid}") is None, "error path leaked its reservation"


def test_archive_older_skips_engines_that_cannot_archive(auth_cfg, fake_jsonl, monkeypatch):
    # A provider whose archive() raises must be counted as skipped, never 500 the request.
    import os
    import time

    from agent_sessions import engines

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / "11111111-1111-1111-1111-111111111111.jsonl", (old, old))

    def boom(self, native):
        raise NotImplementedError

    monkeypatch.setattr(engines.ClaudeProvider, "archive", boom)
    r = c.post(
        "/api/sessions/archive-older",
        json={"hours": 5},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json() == {"archived": 0, "skipped": 1}  # the one old session, skipped not errored


def test_archive_older_validates_hours(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    for bad in (0, -3, "5", True, 24 * 3650 + 1):
        r = c.post("/api/sessions/archive-older", json={"hours": bad}, headers=hdr)
        assert r.status_code == 422, bad


def test_archive_older_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/archive-older", json={"hours": 5}, headers={"Origin": auth_cfg.origin}
    )
    assert r.status_code == 403


def test_sessions_row_exposes_working_and_last_output_at(auth_cfg, fake_jsonl):
    """#156: every /api/sessions row carries the per-key working signal sourced from
    webterm._LAST_OUTPUT_AT. Sessions never observed (no WS attach in this process) are
    last_output_at=null + working=false; observed-recently are working=true; old marks
    flip to false past the window. We don't patch ``time.time`` because the auth cookie
    is age-checked against wall-clock — instead we write the stamp directly to mimic a
    "fresh" or "stale" observation.
    """
    import time as _time

    from agent_sessions import main as main_mod
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()

    c = _client(auth_cfg)
    _login(c, auth_cfg)

    base = c.get("/api/sessions?limit=50").json()
    assert base["sessions"], "fixture should produce rows"
    assert all(s["last_output_at"] is None for s in base["sessions"])
    assert all(s["working"] is False for s in base["sessions"])

    # Fresh observation (within window) → row reports working=true.
    target = base["sessions"][0]
    fresh_ts = _time.time()
    webterm._LAST_OUTPUT_AT[target["id"]] = fresh_ts
    fresh = c.get("/api/sessions?limit=50").json()
    by_id = {s["id"]: s for s in fresh["sessions"]}
    assert by_id[target["id"]]["last_output_at"] == fresh_ts
    assert by_id[target["id"]]["working"] is True

    # Stale stamp (past the working window) → working flips back to false, timestamp
    # still echoed for client-side heuristics.
    stale_ts = _time.time() - (main_mod._WORKING_WINDOW_S + 1)
    webterm._LAST_OUTPUT_AT[target["id"]] = stale_ts
    stale = c.get("/api/sessions?limit=50").json()
    by_id = {s["id"]: s for s in stale["sessions"]}
    assert by_id[target["id"]]["last_output_at"] == stale_ts
    assert by_id[target["id"]]["working"] is False

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()


def test_sessions_row_resolves_through_opencode_alias_for_working(auth_cfg, fake_jsonl):
    """#183: /api/sessions must read last_output_at through the physical-key alias
    layer. For a reconciled opencode session the SessionStream writes to the
    PLACEHOLDER key (``opencode:new-…``), while the sidebar row's id is the
    LOGICAL real ``opencode:ses_…``. Without the alias lookup the row would
    always report idle even when the headless stream is actively dripping bytes.
    """
    import time as _time

    from agent_sessions import metadata, webterm

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()

    c = _client(auth_cfg)
    _login(c, auth_cfg)

    base = c.get("/api/sessions?limit=50").json()
    assert base["sessions"], "fixture should produce rows"
    target = base["sessions"][0]
    real_key = target["id"]
    placeholder_key = f"{target['engine']}:new-placeholder-12345"

    # Persist the alias under the same sidecar that ``engines.physical_key`` reads.
    metadata.set_alias(placeholder_key, real_key)

    # The headless SessionStream stamps the PLACEHOLDER (physical) key.
    fresh_ts = _time.time()
    webterm._LAST_OUTPUT_AT[placeholder_key] = fresh_ts

    fresh = c.get("/api/sessions?limit=50").json()
    by_id = {s["id"]: s for s in fresh["sessions"]}
    # The row's id stays LOGICAL (the real id) but its working/last_output_at
    # come from the PHYSICAL key via the alias layer.
    assert by_id[real_key]["last_output_at"] == fresh_ts
    assert by_id[real_key]["working"] is True

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()


# ---- #206: persisted-scrollback cache management ------------------------------


def test_scrollback_cache_info_and_clear(auth_cfg, fake_jsonl):
    """GET /api/scrollback reports cache size; POST /clear with scope=archived removes only
    archived sessions' caches, scope=all wipes everything. Clearing drops the in-memory
    ring too (#206)."""
    from agent_sessions import webterm

    # `_isolate_scrollback` (autouse) already points `_SCROLLBACK_DIR` at a tmp dir.
    active_key = "claude:11111111-1111-1111-1111-111111111111"  # live in fixture
    archived_key = "claude:44444444-4444-4444-4444-444444444444"  # archived in fixture
    webterm._buffer_append(active_key, b"active session output")
    webterm._buffer_append(archived_key, b"archived session output")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    info = c.get("/api/scrollback").json()
    assert info["files"] == 2 and info["bytes"] > 0

    # scope=archived → only the archived session's cache goes.
    r = c.post("/api/scrollback/clear", json={"scope": "archived"}, headers=hdr)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert not webterm._scrollback_path(archived_key).exists()
    assert webterm._scrollback_path(active_key).exists()

    # scope=all → the rest.
    r = c.post("/api/scrollback/clear", json={"scope": "all"}, headers=hdr)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert c.get("/api/scrollback").json()["files"] == 0


def test_scrollback_clear_rejects_bad_scope_and_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    # bad scope → 422
    assert (
        c.post(
            "/api/scrollback/clear",
            json={"scope": "nope"},
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        ).status_code
        == 422
    )
    # missing CSRF → 403
    assert (
        c.post(
            "/api/scrollback/clear", json={"scope": "all"}, headers={"Origin": auth_cfg.origin}
        ).status_code
        == 403
    )


# ---- project visibility: include-list mode (#335) -----------------------------


def test_included_mode_filters_sessions_to_allowlist(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    rows = c.get("/api/sessions?limit=200").json()["sessions"]
    cwds = sorted({s["cwd"] for s in rows})
    assert len(cwds) >= 2  # the fixture has several distinct launch cwds
    keep = cwds[0]
    r = c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [keep]},
        headers=hdr,
    )
    assert r.status_code == 200
    d = c.get("/api/sessions?limit=200").json()
    # Only sessions launched in the allowlisted cwd survive; they're unadopted, so the facet is
    # just the Default aggregate (#445) — list + facets agree on the visible set.
    assert all(s["cwd"] == keep for s in d["sessions"])
    assert [p["id"] for p in d["facets"]["projects"]] == ["__default__"]


def test_all_mode_hide_still_excludes(auth_cfg, fake_jsonl):
    # Regression: the legacy denylist behavior is unchanged in the default mode.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    rows = c.get("/api/sessions?limit=200").json()["sessions"]
    drop = sorted({s["cwd"] for s in rows})[0]
    c.post("/api/prefs", json={"projects_hidden": [drop]}, headers=hdr)
    after = c.get("/api/sessions?limit=200").json()["sessions"]
    assert drop not in {s["cwd"] for s in after}  # the denylisted cwd's sessions are gone


def test_projects_picker_unfiltered_in_included_mode(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    all_cwds = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    assert all_cwds
    keep = sorted(all_cwds)[0]
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [keep]},
        headers=hdr,
    )
    # the picker still offers EVERY discovered dir (start anywhere → auto-include), not just the
    # allowlist — otherwise the curated mode would lock you out of adding a new project.
    incl_cwds = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    assert incl_cwds == all_cwds


def test_projects_visible_param_applies_included_allowlist(auth_cfg, fake_jsonl):
    # ?visible=1 (#335 follow-up): the new-session dropdown mirrors the curated sidebar -
    # in `included` mode only the allowlist comes back; the unfiltered default (Settings
    # curation surface) is untouched.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    all_cwds = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    assert len(all_cwds) >= 2
    keep = sorted(all_cwds)[0]
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [keep]},
        headers=hdr,
    )
    visible = {p["cwd"] for p in c.get("/api/folders?visible=1").json()["folders"]}
    assert visible == {keep}
    # default stays the full set for Settings
    assert {p["cwd"] for p in c.get("/api/folders").json()["folders"]} == all_cwds


def test_projects_visible_param_drops_hidden_in_all_mode(auth_cfg, fake_jsonl):
    # ?visible=1 in the default `all` mode behaves like the legacy picker: denylisted
    # cwds are dropped.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    all_cwds = sorted(p["cwd"] for p in c.get("/api/folders").json()["folders"])
    assert len(all_cwds) >= 2
    drop = all_cwds[0]
    c.post("/api/prefs", json={"projects_hidden": [drop]}, headers=hdr)
    visible = {p["cwd"] for p in c.get("/api/folders?visible=1").json()["folders"]}
    assert drop not in visible
    assert set(all_cwds) - {drop} <= visible


def test_config_exposes_projects_mode_and_included(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    cfg = c.get("/api/config").json()
    assert cfg["projects_mode"] == "all" and cfg["projects_included"] == []
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": ["/x"]},
        headers=hdr,
    )
    cfg = c.get("/api/config").json()
    assert cfg["projects_mode"] == "included" and cfg["projects_included"] == ["/x"]


def test_projects_mode_invalid_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"projects_mode": "bogus"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_default_project_config_and_prefs(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.get("/api/config").json()["default_project"] == ""
    r = c.post("/api/prefs", json={"default_project": "/p/x"}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"default_project": "/p/x"}
    assert c.get("/api/config").json()["default_project"] == "/p/x"


def test_default_project_non_string_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"default_project": 123},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- scoped create-folder (#335 Phase 3) --------------------------------------


def test_mkdir_creates_under_configured_root(auth_cfg, fake_jsonl, tmp_path, monkeypatch):
    import os

    root = tmp_path / "code"
    root.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/folders/mkdir", json={"root": str(root), "name": "newproj"}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["cwd"] == os.path.realpath(root / "newproj")
    assert (root / "newproj").is_dir()
    # exposed in config so the UI can show the "New folder" affordance + its roots
    assert c.get("/api/config").json()["project_roots"] == [os.path.realpath(root)]


def test_mkdir_disabled_when_no_roots_404(auth_cfg, fake_jsonl, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_PROJECT_ROOTS", raising=False)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/folders/mkdir",
        json={"root": "/x", "name": "y"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404
    assert c.get("/api/config").json()["project_roots"] == []


def test_mkdir_bad_name_422(auth_cfg, fake_jsonl, tmp_path, monkeypatch):
    root = tmp_path / "code"
    root.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/folders/mkdir",
        json={"root": str(root), "name": "../escape"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_mkdir_root_not_allowed_403(auth_cfg, fake_jsonl, tmp_path, monkeypatch):
    root = tmp_path / "code"
    root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/folders/mkdir",
        json={"root": str(other), "name": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_mkdir_requires_csrf(auth_cfg, fake_jsonl, tmp_path, monkeypatch):
    root = tmp_path / "code"
    root.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/folders/mkdir",
        json={"root": str(root), "name": "x"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


# ---- #465: root-scoped + exclusion-filtered discovery (HARD scope) ------------


def test_sessions_unchanged_when_no_roots(auth_cfg, fake_jsonl, tmp_home):
    """Empty roots ⇒ the session list + facets are exactly today's (back-compat default)."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?limit=100").json()
    cwds = {row["cwd"] for row in d["sessions"]}
    assert {_REPO_A, _TMP_OTHER, _DEMOAPP} <= cwds


def test_sessions_root_scoped_drops_out_of_root_rows_and_facets(auth_cfg, fake_jsonl, tmp_home):
    """A HARD scope (#465): with project roots set, sessions whose cwd is NOT under a root drop
    from the /api/sessions list AND its facets (Default count shrinks), before pagination."""
    from agent_sessions import project_dirs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    before = c.get("/api/sessions?limit=100").json()
    assert _TMP_OTHER in {row["cwd"] for row in before["sessions"]}
    before_default = next(p for p in before["facets"]["projects"] if p["id"] == "__default__")

    # Root that boundary-contains the /home/user/claude fixture cwds but NOT /tmp/other. The
    # path need not exist on the host: scope is a pure boundary test, so we drive effective_roots
    # directly (the existing-dir realpath filter is covered in test_project_dirs).
    c.app.dependency_overrides = getattr(c.app, "dependency_overrides", {})
    import agent_sessions.routes.sessions as sessions_mod

    orig = project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        after = c.get("/api/sessions?limit=100").json()
    finally:
        sessions_mod.project_dirs.effective_roots = orig

    after_cwds = {row["cwd"] for row in after["sessions"]}
    assert _TMP_OTHER not in after_cwds  # outside the root → dropped
    assert _REPO_A in after_cwds  # under the root → kept
    after_default = next(p for p in after["facets"]["projects"] if p["id"] == "__default__")
    # /tmp/other was a Default (unadopted) row → the Default facet count drops by one.
    assert after_default["count"] == before_default["count"] - 1
    assert after["total"] == before["total"] - 1


def test_sessions_root_scope_honors_exclusions(auth_cfg, fake_jsonl, tmp_home):
    """An excluded prefix drops a row even when it's under a root (#465)."""
    import agent_sessions.routes.sessions as sessions_mod
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    prefs.set_folder_exclusions([_REPO_A])
    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        d = c.get("/api/sessions?limit=100").json()
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    cwds = {row["cwd"] for row in d["sessions"]}
    assert _REPO_A not in cwds  # excluded → dropped
    assert _DEMOAPP in cwds  # still under the root, not excluded


# ---- #520: explicit curation beats discovery roots (precedence: exclusion > curation > roots) ----
# (The "unknown folder outside roots → dropped" leg is covered by
# test_sessions_root_scoped_drops_out_of_root_rows_and_facets above.)


def test_sessions_root_scope_keeps_adopted_project_outside_roots(auth_cfg, fake_jsonl, tmp_home):
    """An adopted project whose folder is OUTSIDE the roots stays visible — explicit curation
    beats discovery roots, so the user never loses a project they explicitly created (#520)."""
    import agent_sessions.routes.sessions as sessions_mod

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Adopt /tmp/other (outside the /home/user/claude root) into a project entity.
    c.post("/api/projects", json={"name": "Tmp", "folders": [_TMP_OTHER]}, headers=hdr)
    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        d = c.get("/api/sessions?limit=100").json()
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    cwds = {row["cwd"] for row in d["sessions"]}
    assert _TMP_OTHER in cwds  # adopted → kept despite being outside the root
    assert _REPO_A in cwds  # under the root → kept


def test_sessions_root_scope_keeps_included_cwd_outside_roots(auth_cfg, fake_jsonl, tmp_home):
    """In `included` mode an allowlisted cwd OUTSIDE the roots stays visible (curation beats roots),
    while a non-allowlisted cwd — even under a root — is still hidden by included-mode (#520)."""
    import agent_sessions.routes.sessions as sessions_mod

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [_TMP_OTHER]},
        headers=hdr,
    )
    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        d = c.get("/api/sessions?limit=100").json()
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    cwds = {row["cwd"] for row in d["sessions"]}
    assert _TMP_OTHER in cwds  # included + outside root → kept (curation beats roots)
    assert _REPO_A not in cwds  # under the root but not allowlisted → hidden by included-mode


def test_sessions_root_scope_exclusion_beats_curation(auth_cfg, fake_jsonl, tmp_home):
    """Precedence rule 1: an explicit exclusion wins even over explicit curation — an adopted
    project whose folder is also excluded is still dropped (#520)."""
    import agent_sessions.routes.sessions as sessions_mod
    from agent_sessions import prefs

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post("/api/projects", json={"name": "Tmp", "folders": [_TMP_OTHER]}, headers=hdr)
    prefs.set_folder_exclusions([_TMP_OTHER])
    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        d = c.get("/api/sessions?limit=100").json()
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    cwds = {row["cwd"] for row in d["sessions"]}
    assert _TMP_OTHER not in cwds  # excluded → dropped despite being an adopted project
    assert _REPO_A in cwds


def test_favorite_pins_globally_across_page_boundary(auth_cfg, fake_jsonl):
    """The sticky pin is GLOBAL, not first-window-only (#520): a favorite that recency would place
    on a later page is pulled into the first window."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    base = c.get("/api/sessions?limit=100").json()
    assert base["total"] >= 3
    newest = base["sessions"][0]["uuid"]
    laggard = base["sessions"][-1]["uuid"]  # last by recency → would land on a later page
    assert laggard != newest
    c.post(f"/api/sessions/claude:{laggard}/favorite", headers=hdr)
    win = c.get("/api/sessions?limit=1").json()
    assert win["total"] > 1  # there ARE more pages → the pin reached across the boundary
    assert win["sessions"][0]["uuid"] == laggard
    assert win["sessions"][0]["sticky"] is True


def test_folders_root_scoped(auth_cfg, fake_jsonl, tmp_home):
    """/api/folders drops out-of-scope cwds when roots are set; a root sub-dir surfaces."""
    import agent_sessions.routes.sessions as sessions_mod

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert _TMP_OTHER in {p["cwd"] for p in c.get("/api/folders").json()["folders"]}

    orig = sessions_mod.project_dirs.effective_roots
    sessions_mod.project_dirs.effective_roots = lambda: ["/home/user/claude"]
    try:
        folders = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    finally:
        sessions_mod.project_dirs.effective_roots = orig
    assert _TMP_OTHER not in folders  # out of root → dropped
    assert _REPO_A in folders  # under root → kept


def test_folders_root_subdir_surfaces(auth_cfg, fake_jsonl, tmp_home):
    """A session-less immediate sub-dir of a configured (existing) root shows in /api/folders."""
    import os

    from agent_sessions import prefs

    code = tmp_home / "code"
    (code / "brand-new").mkdir(parents=True)
    prefs.set_project_roots([str(code)])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    folders = {p["cwd"] for p in c.get("/api/folders").json()["folders"]}
    assert os.path.realpath(code / "brand-new") in folders
    # The out-of-root fixture cwds are gone.
    assert _TMP_OTHER not in folders


# ---- AI recap on rows + api_key boundary (#481) -------------------------------


def test_ai_recap_on_rows_and_api_key_never_leaks(auth_cfg, fake_jsonl):
    from agent_sessions import metadata, prefs

    secret = "sk-must-not-leak-9999"  # noqa: S105 — test fixture value
    sid = "claude:11111111-1111-1111-1111-111111111111"  # exists in fake_jsonl
    prefs.set_ai_review({"base_url": "https://ai.test/v1", "api_key": secret, "model": "m"})
    metadata.patch(sid, ai_recap="Cloned the repo, then fixed the bug.")

    c = _client(auth_cfg)
    _login(c, auth_cfg)

    r = c.get("/api/sessions?limit=200")
    assert r.status_code == 200
    row = next(s for s in r.json()["sessions"] if s["id"] == sid)
    # The model-derived recap rides on the row…
    assert row["ai_recap"] == "Cloned the repo, then fixed the bug."
    # …but the API key never crosses the boundary, on the rows or anywhere else.
    assert secret not in r.text

    cfg_resp = c.get("/api/config")
    assert secret not in cfg_resp.text
    ai = cfg_resp.json()["ai_review"]
    assert "api_key" not in ai  # only the write-only marker is exposed
    assert ai.get("api_key_set") is True


def test_config_exposes_server_hostname(auth_cfg):
    # #503: the SPA footer shows which machine a tab is pointed at, sourced from /api/config.
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    conf = c.get("/api/config").json()
    assert conf["hostname"] == socket.gethostname()


# ---- archive reaps runtime resources (#523) -----------------------------------


def test_archive_route_reaps_runtime_then_archives(auth_cfg, fake_jsonl, monkeypatch):
    # #523: the archive route reclaims the live runtime footprint BEFORE recording the
    # archive (terminate-first), so a still-running claude can't recreate its JSONL between
    # the move and the kill.
    from agent_sessions import engines
    from agent_sessions.routes import sessions as sroutes

    order: list = []

    async def fake_cleanup(engine, native, *, spare_if=None, **_kw):
        order.append(("cleanup", engine, native))
        return "gone"

    real_archive = engines.ClaudeProvider.archive

    def spy_archive(self, native):
        order.append(("archive", native))
        return real_archive(self, native)

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", fake_cleanup)
    monkeypatch.setattr(engines.ClaudeProvider, "archive", spy_archive)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(f"/api/sessions/claude:{uuid}/archive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True
    assert order == [("cleanup", "claude", uuid), ("archive", uuid)]


def test_archive_succeeds_even_if_cleanup_raises(auth_cfg, fake_jsonl, monkeypatch):
    # #523: teardown is best-effort — a cleanup failure must never block the archive itself.
    from agent_sessions.routes import sessions as sroutes

    async def boom_cleanup(engine, native, *, spare_if=None, **_kw):
        raise RuntimeError("teardown blew up")

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", boom_cleanup)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.post(f"/api/sessions/claude:{uuid}/archive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True


def test_archive_route_refuses_when_a_late_bound_runtime_cannot_be_located(
    auth_cfg, fake_jsonl, monkeypatch
):
    # #994 review 2, finding 1: teardown stays best-effort, but not being able to NAME its target
    # does not. A late-id key with no alias is resolved through the mission store; when that store
    # cannot be read the archive is refused, never recorded over a possibly still running agent.
    from agent_sessions import missions
    from agent_sessions.routes import sessions as sroutes

    called: list = []

    async def rec_cleanup(engine, native, **_kw):
        called.append((engine, native))
        return "gone"

    def unreadable(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", rec_cleanup)
    monkeypatch.setattr(missions, "physical_key_of", unreadable)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    key = "kimi:session_11111111-2222-3333-4444-555555555555"
    r = c.post(f"/api/sessions/{key}/archive", headers=hdr)
    assert r.status_code == 503, r.text
    assert "could not tell where" in r.json()["detail"]
    assert called == [], "the teardown ran against a runtime nobody could locate"


def test_unarchive_does_not_reap_runtime(auth_cfg, fake_jsonl, monkeypatch):
    # #523: unarchive restores a session — it must NOT tear down runtime resources.
    from agent_sessions.routes import sessions as sroutes

    called: list = []

    async def rec_cleanup(engine, native, *, spare_if=None, **_kw):
        called.append((engine, native))
        return "gone"

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", rec_cleanup)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "11111111-1111-1111-1111-111111111111"
    c.post(f"/api/sessions/claude:{uuid}/archive", headers=hdr)
    called.clear()
    r = c.post(f"/api/sessions/claude:{uuid}/unarchive", headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is False
    assert called == []  # unarchive never reaps


def test_archive_older_reaps_each_and_continues_on_cleanup_error(auth_cfg, fake_jsonl, monkeypatch):
    # #523: bulk archive reaps per session and is best-effort — one session's teardown failure
    # must not abort the batch, and the archive still lands.
    import os
    import time

    from agent_sessions.routes import sessions as sroutes

    seen: list = []

    async def rec_cleanup(engine, native, *, spare_if=None, **_kw):
        seen.append((engine, native))
        raise RuntimeError("one session's teardown fails")

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", rec_cleanup)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    old = time.time() - 10 * 3600
    os.utime(proj / "11111111-1111-1111-1111-111111111111.jsonl", (old, old))

    r = c.post("/api/sessions/archive-older", json={"hours": 5}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["archived"] == 1  # archived despite the cleanup error
    assert ("claude", "11111111-1111-1111-1111-111111111111") in seen


# ---- background-agent archive guard (#631) ------------------------------------


def test_archive_refuses_when_transcript_owned_by_bg_agent(auth_cfg, fake_jsonl, monkeypatch):
    # #631: with our own master gone (cleanup returns "gone"), a live process STILL holding the
    # transcript is a Claude background agent we don't manage. Archive must REFUSE (409) rather
    # than shutil.move its open JSONL — moving it would diverge the file across the two trees.
    from agent_sessions.routes import sessions as sroutes

    async def gone_cleanup(engine, native, *, spare_if=None, **_kw):
        return "gone"  # a background agent has no dtach master → cleanup is a no-op

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", gone_cleanup)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    uuid = "11111111-1111-1111-1111-111111111111"
    jsonl = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a" / f"{uuid}.jsonl"
    with jsonl.open("r"):  # a live process (this one) holds the transcript open
        r = c.post(f"/api/sessions/claude:{uuid}/archive", headers=hdr)
    assert r.status_code == 409
    # The JSONL must NOT have moved — the live fork still owns it.
    assert jsonl.exists()
    assert not list((fake_jsonl / ".claude" / "projects-archive").glob(f"*/{uuid}.jsonl"))


# --- Custom per-session tag (#551) ------------------------------------------------------


def test_set_tag_surfaces_in_row(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(f"/api/sessions/claude:{uuid}/tag", json={"tag": "🔥 hotpath"}, headers=hdr)
    assert r.status_code == 200 and r.json()["tag"] == "🔥 hotpath"
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["tag"] == "🔥 hotpath"


def test_set_tag_clears_on_whitespace(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post(f"/api/sessions/claude:{uuid}/tag", json={"tag": "review"}, headers=hdr)
    r = c.post(f"/api/sessions/claude:{uuid}/tag", json={"tag": "   "}, headers=hdr)
    assert r.status_code == 200 and r.json()["tag"] == ""
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["tag"] == ""


def test_set_tag_is_length_capped(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(f"/api/sessions/claude:{uuid}/tag", json={"tag": "x" * 100}, headers=hdr)
    assert r.status_code == 200 and len(r.json()["tag"]) == 32


def test_set_tag_requires_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    # No X-CSRF-Token header → rejected before any sidecar write.
    r = c.post(
        f"/api/sessions/claude:{uuid}/tag",
        json={"tag": "nope"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_set_tag_unknown_session_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/sessions/claude:not-a-real-uuid/tag", json={"tag": "x"}, headers=hdr)
    assert r.status_code == 404


# --- Per-session color endpoint (#571) -----------------------------------------------


def test_set_color_surfaces_in_row(auth_cfg, fake_jsonl):
    """``POST /api/sessions/{sid}/color`` writes the sidecar field; ``/api/sessions``
    surfaces the same color in the row's metadata projection.
    """
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        f"/api/sessions/claude:{uuid}/color",
        json={"color": "#5FD7FF"},
        headers=hdr,
    )
    assert r.status_code == 200
    assert r.json() == {"id": f"claude:{uuid}", "color": "#5fd7ff"}  # normalized lower-case
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["color"] == "#5fd7ff"


def test_set_color_clears_on_empty_string(auth_cfg, fake_jsonl):
    """``{"color": ""}`` clears the override — a re-fetch returns ``""`` (#571)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Seed a color then clear it.
    c.post(f"/api/sessions/claude:{uuid}/color", json={"color": "#ff0"}, headers=hdr)
    r = c.post(f"/api/sessions/claude:{uuid}/color", json={"color": ""}, headers=hdr)
    assert r.status_code == 200 and r.json()["color"] == ""
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert next(s for s in rows if s["uuid"] == uuid)["color"] == ""


def test_set_color_clears_on_null(auth_cfg, fake_jsonl):
    """``{"color": null}`` (or missing) also clears — same as the metadata.write path."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post(f"/api/sessions/claude:{uuid}/color", json={"color": "#ff0"}, headers=hdr)
    r = c.post(f"/api/sessions/claude:{uuid}/color", json={"color": None}, headers=hdr)
    assert r.status_code == 200 and r.json()["color"] == ""


def test_set_color_rejects_invalid_hex(auth_cfg, fake_jsonl):
    """Invalid hex → 422 with the validator's helper string (NOT a 500, NOT a 400)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        f"/api/sessions/claude:{uuid}/color",
        json={"color": "not-a-color"},
        headers=hdr,
    )
    assert r.status_code == 422
    assert "color" in r.json()["detail"].lower()
    # Wrong-length hex (#1234) is also rejected.
    r = c.post(
        f"/api/sessions/claude:{uuid}/color",
        json={"color": "#1234"},
        headers=hdr,
    )
    assert r.status_code == 422


def test_set_color_requires_csrf(auth_cfg, fake_jsonl):
    """CSRF is mandatory on every state-changing request — color is no exception."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    # No X-CSRF-Token header → rejected before any sidecar write.
    r = c.post(
        f"/api/sessions/claude:{uuid}/color",
        json={"color": "#abc"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_set_color_unknown_session_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/sessions/claude:not-a-real-uuid/color",
        json={"color": "#abc"},
        headers=hdr,
    )
    assert r.status_code == 404


def test_set_color_unknown_engine_404(auth_cfg, fake_jsonl):
    """An engine id the registry doesn't recognize → 404, not 5xx (#265 identity gate)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/sessions/bogus:abc/color",
        json={"color": "#abc"},
        headers=hdr,
    )
    assert r.status_code == 404


def test_set_color_preserves_other_fields(auth_cfg, fake_jsonl):
    """A color write must not clobber adjacent sidecar fields (title, tag, sticky)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Seed: title, tag, favorite via existing endpoints.
    c.post(f"/api/sessions/claude:{uuid}/rename", json={"title": "My Title"}, headers=hdr)
    c.post(f"/api/sessions/claude:{uuid}/tag", json={"tag": "prod"}, headers=hdr)
    c.post(f"/api/sessions/claude:{uuid}/favorite", headers=hdr)
    # Now write the color.
    c.post(f"/api/sessions/claude:{uuid}/color", json={"color": "#abc"}, headers=hdr)
    # Re-read all three fields.
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    row = next(s for s in rows if s["uuid"] == uuid)
    assert row["color"] == "#abc"
    assert row["title"] == "My Title"
    assert row["tag"] == "prod"
    assert row["sticky"] is True


def test_api_config_advertises_shell_new_session(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    # #636: the plain-terminal "shell" engine is always offered when bash resolves — it's the
    # one engine present even on a host with no agent CLIs.
    bash = tmp_home / "bin" / "bash"
    bash.parent.mkdir(parents=True, exist_ok=True)
    bash.write_text("#!/bin/sh\n")
    bash.chmod(0o755)
    monkeypatch.setenv("AGENT_SESSIONS_BASH_BIN", str(bash))

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert "shell" in d["new_session_engines"]


# ---- single-session lookup (#867) ---------------------------------------------
#
# The session PANE reads this route, not the list. Everything below exists because the pane used
# to resolve its project / cwd / title out of the sidebar's store — one filtered, scope-stripped
# 20-row page — so anything deep-linked, reloaded off page 0, archived, or hidden by
# `projects_mode` / `projects_hidden` had no row anywhere in the client.


def test_session_lookup_returns_the_same_row_shape_as_the_list(auth_cfg, fake_jsonl):
    """One row, byte-identical to the list's — both go through `_row`, so the pane and the
    sidebar can never disagree about a session."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    listed = next(
        s for s in c.get("/api/sessions?limit=100").json()["sessions"] if s["uuid"] == uuid
    )
    r = c.get(f"/api/sessions/claude:{uuid}")
    assert r.status_code == 200
    assert r.json() == listed


def test_session_lookup_accepts_a_bare_uuid(auth_cfg, fake_jsonl):
    """`canonical_key` back-compat: a bare Claude UUID resolves to `claude:<uuid>`, so an old
    bookmark keeps working here exactly as it does on every other /api/sessions/{sid} route."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "11111111-1111-1111-1111-111111111111"
    r = c.get(f"/api/sessions/{uuid}")
    assert r.status_code == 200
    assert r.json()["id"] == f"claude:{uuid}"


def test_session_lookup_finds_an_archived_session(auth_cfg, fake_jsonl):
    """The list's default filter is `archived=false`, so an archived session opened by link had
    no row at any page. The lookup returns it with `archived: true` — a field, never a filter."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "44444444-4444-4444-4444-444444444444"
    assert uuid not in {s["uuid"] for s in c.get("/api/sessions?limit=100").json()["sessions"]}
    r = c.get(f"/api/sessions/claude:{uuid}")
    assert r.status_code == 200
    assert r.json()["archived"] is True


def test_session_lookup_ignores_the_list_visibility_scope(auth_cfg, fake_jsonl, tmp_home):
    """The deliberate difference from the list (#867).

    `_scope_filter()` answers "should this row appear in a LISTING". This is a lookup of one id
    whose live terminal the operator is already attached to, so hiding its folder name buys
    nothing — and applying the filter would leave exactly the sessions this route exists for
    (measured: 588 of 1390 on the author's install) still nameless.
    """
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "33333333-3333-3333-3333-333333333333"
    prefs.set_projects_hidden(["/tmp/other"])
    listed = c.get("/api/sessions?limit=100").json()["sessions"]
    assert uuid not in {s["uuid"] for s in listed}  # gone from the list…
    r = c.get(f"/api/sessions/claude:{uuid}")  # …still nameable in the pane
    assert r.status_code == 200
    assert r.json()["cwd"] == "/tmp/other"


def test_session_lookup_404s_on_unknown_and_malformed_ids(auth_cfg, fake_jsonl):
    """`canonical_key` is the gate: an unknown engine and a malformed native id are rejected
    before the scan, and a well-formed id nobody has is a plain 404. Never a 500."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    for sid in (
        "claude:99999999-9999-4999-8999-999999999999",  # well-formed, unknown
        "claude:not-a-uuid",  # fails the provider's id pattern
        "nosuchengine:11111111-1111-1111-1111-111111111111",  # unknown engine
        "..",  # not an id at all
    ):
        assert c.get(f"/api/sessions/{sid}").status_code == 404, sid


def test_session_lookup_404s_on_an_unreconciled_new_placeholder(auth_cfg, fake_jsonl):
    """`<engine>:new-<uuid>` (#127/#315) is accepted only on the ws launch path, so it 404s here.

    The CLIENT never asks for one (that would pin an error on a key about to be replaced) — this
    pins the server half: a placeholder that does arrive is a clean 404, never a 500.
    """
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions/opencode:new-11111111-1111-1111-1111-111111111111")
    assert r.status_code == 404


def test_session_lookup_requires_login(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    r = c.get("/api/sessions/claude:11111111-1111-1111-1111-111111111111")
    assert r.status_code == 401


def test_session_lookup_does_not_block_the_event_loop(auth_cfg, fake_jsonl):
    """The row build runs in a worker thread, so a cache miss cannot stall the terminal
    WebSockets that share the event loop (#678's shape, arriving through a new door).

    Driven over ASGI on ONE loop, deliberately: `TestClient` gives each request its own portal
    and loop, so two of its calls can never contend — a version of this test written on it
    passed against a handler that called the blocking builder directly, which is worse than no
    test at all.

    The discriminator is WHEN the sibling request lands, not how long it takes. Both requests
    start together; the health one yields briefly so the lookup reaches the blocking scan first.
    On the event loop the lookup pins it for `BLOCK_S`, so the health coroutine cannot even
    resume from that yield until the block is over and it lands at >= BLOCK_S. In a worker
    thread the loop stays free and it lands immediately.
    """
    import asyncio as _asyncio
    import time as _time

    import httpx

    from agent_sessions import engines

    BLOCK_S = 1.5
    real = engines.scan_all_cached

    def slow():
        _time.sleep(BLOCK_S)
        return real()

    async def drive():
        transport = httpx.ASGITransport(app=create_app(auth_cfg))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
            r = await ac.post(
                "/login",
                data={"username": "marcus", "password": "hunter2"},
                headers={"Origin": auth_cfg.origin},
            )
            assert r.status_code == 303

            t0 = _time.monotonic()

            async def health_when():
                await _asyncio.sleep(0.05)  # let the lookup reach the scan first
                resp = await ac.get("/healthz")
                return _time.monotonic() - t0, resp

            with mock.patch.object(engines, "scan_all_cached", slow):
                lookup, (landed_at, health) = await _asyncio.gather(
                    ac.get("/api/sessions/claude:11111111-1111-1111-1111-111111111111"),
                    health_when(),
                )
            return lookup, landed_at, health

    lookup, landed_at, health = _asyncio.run(drive())
    assert lookup.status_code == 200
    assert health.status_code == 200
    assert (
        landed_at < BLOCK_S / 2
    ), f"/healthz landed {landed_at:.2f}s in — the lookup held the event loop"


def _seed_session_at(tmp_home, uuid: str, cwd: Path) -> None:
    """A claude session whose cwd is a REAL directory — roots are normalized against the
    filesystem (`_normalize_roots` keeps only existing dirs), so a scope test written against
    the shared fixture's decoded-but-nonexistent paths silently tests nothing."""
    cwd.mkdir(parents=True, exist_ok=True)
    d = tmp_home / ".claude" / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uuid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(cwd), "message": {"content": "hi"}}) + "\n"
    )


def test_session_lookup_enforces_the_hard_root_scope(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    """Roots + `folder_exclusions` are a BOUNDARY, not a listing preference (#867 review).

    The ws resume path enforces exactly this predicate and calls bypassing it a back door, so a
    session outside the configured roots cannot be opened at all — and a caller holding a guessed
    or stale id must not be able to read its cwd and metadata through a route that checks only
    `logged_in`. Out of scope answers 404, the same way resume does.

    The soft half is unaffected: the archived and `projects_hidden` cases above still resolve.
    """
    inside_uuid = "aaaa1111-1111-4111-8111-111111111111"
    outside_uuid = "bbbb2222-2222-4222-8222-222222222222"
    root = tmp_home / "roots" / "work"
    _seed_session_at(tmp_home, inside_uuid, root / "repo")
    _seed_session_at(tmp_home, outside_uuid, tmp_home / "elsewhere" / "repo")

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # No roots ⇒ the feature is off and both resolve.
    assert c.get(f"/api/sessions/claude:{outside_uuid}").status_code == 200
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))
    assert c.get(f"/api/sessions/claude:{inside_uuid}").status_code == 200
    assert c.get(f"/api/sessions/claude:{outside_uuid}").status_code == 404


def test_session_lookup_honours_folder_exclusions(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    """An excluded prefix drops the row even inside a root — exclusion beats curation (#520)."""
    from agent_sessions import prefs

    uuid = "cccc3333-3333-4333-8333-333333333333"
    root = tmp_home / "roots2"
    _seed_session_at(tmp_home, uuid, root / "repo")
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 200
    prefs.set_folder_exclusions([str(root / "repo")])
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 404


def test_session_lookup_refuses_a_curated_project_outside_every_root(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    """Curation does NOT exempt a row from the lookup's boundary (#867 review round 2).

    The LIST lets an adopted project through outside a root (#520, `curated=True`), but
    `routes/terminal.py` calls the same helper with its default `curated=False` and refuses to
    resume it. The lookup follows the TERMINAL: its whole justification is "you can already open
    this session", so where resume refuses, naming it — cwd, title, first message, recap — is not
    ours either. Otherwise a caller holding a guessed id reads metadata for a session the
    terminal rejects as a back door.
    """
    from agent_sessions import projects

    uuid = "dddd4444-4444-4444-8444-444444444444"
    outside = tmp_home / "adopted" / "repo"
    _seed_session_at(tmp_home, uuid, outside)
    root = tmp_home / "roots3"
    root.mkdir(parents=True, exist_ok=True)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # Adopt the folder into a project, then scope to an unrelated root.
    projects.create("Adopted", folders=[str(outside)])
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))

    # The LIST still shows it — curation beats roots there, and that behaviour is untouched.
    listed = c.get("/api/sessions?limit=100").json()["sessions"]
    assert uuid in {s["uuid"] for s in listed}
    # The LOOKUP refuses it, matching what resume would do.
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 404


def test_session_lookup_refuses_an_included_cwd_outside_every_root(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    """The other curation route: `included` mode's allowlist. Same rule, same reason."""
    from agent_sessions import prefs

    uuid = "eeee5555-5555-4555-8555-555555555555"
    outside = tmp_home / "allowlisted" / "repo"
    _seed_session_at(tmp_home, uuid, outside)
    root = tmp_home / "roots4"
    root.mkdir(parents=True, exist_ok=True)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    prefs.set_projects_mode("included")
    prefs.set_projects_included([str(outside)])
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))

    listed = c.get("/api/sessions?limit=100").json()["sessions"]
    assert uuid in {s["uuid"] for s in listed}
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 404


def test_session_lookup_honours_exclusions_with_no_roots_configured(auth_cfg, fake_jsonl, tmp_home):
    """An exclusion drops the row even when NO roots are configured (#867 review round 3).

    The tempting shortcut — "empty roots ⇒ the #465 feature is off ⇒ everything is in scope" —
    skips `project_dirs.in_scope`, which checks exclusions FIRST and only then falls through on
    empty roots. With no roots set (the common case) that served rows under an explicitly
    excluded prefix, which is the most explicit opt-out the operator has (#520).
    """
    from agent_sessions import prefs

    uuid = "ffff6666-6666-4666-8666-666666666666"
    cwd = tmp_home / "excluded" / "repo"
    _seed_session_at(tmp_home, uuid, cwd)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert not prefs.get_project_roots()  # no roots — the shortcut's precondition
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 200
    prefs.set_folder_exclusions([str(cwd)])
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 404


def test_session_lookup_rewalks_before_answering_404(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    """A miss re-walks once, so the 404 is authoritative rather than an artifact of the TTL.

    `scan_all_cached` serves a short-TTL snapshot, and a session created outside the app is
    legitimately absent from a warm one. Answering 404 from that snapshot teaches the client
    "this id does not exist" about a session that does — and the client settles negatives.

    The TTL is enabled explicitly: conftest pins it to 0 for the suite, which makes the cache a
    pass-through and would leave this test asserting nothing (it passed against the unfixed
    endpoint until that was noticed).
    """
    from agent_sessions import engines
    from agent_sessions.routes import sessions as sessions_routes

    monkeypatch.setattr(sessions_routes, "_last_miss_rewalk", 0.0, raising=False)
    engines.set_scan_cache_ttl(30.0)
    monkeypatch.setattr(
        engines, "_SCAN_CACHE_TTL_S", 30.0, raising=False
    )  # belt and braces for the module-global read

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    uuid = "abcd7777-7777-4777-8777-777777777777"
    try:
        c.get("/api/sessions?limit=1")  # warm a snapshot WITHOUT the session
        _seed_session_at(tmp_home, uuid, tmp_home / "late" / "repo")
        # Sanity: the warm snapshot really does not know it — otherwise the assert below is free.
        assert all(s.uuid != uuid for s in engines.scan_all_cached())
        assert c.get(f"/api/sessions/claude:{uuid}").status_code == 200
    finally:
        engines.set_scan_cache_ttl(0.0)
        engines.invalidate_scan_cache()


def test_session_lookup_does_not_rewalk_for_a_scope_refusal(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    """A scope refusal is not a miss — re-walking cannot change it, so it must not pay for one.

    Pinned because the re-walk is the expensive branch: making every out-of-scope lookup force a
    full disk scan would hand any authenticated caller a cheap way to thrash the scanner.
    """
    from agent_sessions import engines

    uuid = "bcde8888-8888-4888-8888-888888888888"
    outside = tmp_home / "outside" / "repo"
    _seed_session_at(tmp_home, uuid, outside)
    root = tmp_home / "roots5"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(root))

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    c.get("/api/sessions?limit=1")  # warm the snapshot WITH the session in it

    calls = {"n": 0}
    real = engines.invalidate_scan_cache
    monkeypatch.setattr(
        engines,
        "invalidate_scan_cache",
        lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1],
    )
    assert c.get(f"/api/sessions/claude:{uuid}").status_code == 404
    assert calls["n"] == 0, "a scope refusal must not force a re-walk"


def test_session_lookup_throttles_the_miss_rewalk(auth_cfg, fake_jsonl, monkeypatch):
    """Repeated unknown ids must not each force a full provider scan (#867 review round 5).

    The re-walk exists for one narrow case — a session created moments ago and absent from a warm
    snapshot. Doing it per miss lets a handful of random authenticated 404s defeat the shared
    10 s cache for every other caller.
    """
    from agent_sessions import engines
    from agent_sessions.routes import sessions as sessions_routes

    monkeypatch.setattr(sessions_routes, "_last_miss_rewalk", 0.0, raising=False)
    calls = {"n": 0}
    real = engines.invalidate_scan_cache
    monkeypatch.setattr(
        engines,
        "invalidate_scan_cache",
        lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1],
    )

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    for n in range(4):
        sid = f"claude:9999999{n}-9999-4999-8999-999999999999"
        assert c.get(f"/api/sessions/{sid}").status_code == 404
    assert calls["n"] == 1, f"{calls['n']} forced scans for 4 misses — the throttle is not holding"


# --- /api/config: the operator's login name (#1058) ----------------------------------------------


def test_config_names_the_operator_and_carries_no_credential(auth_cfg, fake_jsonl):
    """The corner tile needs a name; it must never get anything else.

    The name is what the operator types at the login form, and this route is already
    `logged_in`, so exposing it widens nothing. What WOULD widen something is a companion field
    sneaking in beside it, so the credentials are asserted absent by value rather than by name:
    a future serialiser that spells the hash `pw` or `secret` still fails here.
    """
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    cfg = c.get("/api/config").json()
    assert cfg["username"] == "marcus"
    assert cfg["auth_mode"] == "single-user"

    blob = json.dumps(cfg)
    assert auth_cfg.password_hash not in blob
    assert auth_cfg.secret_key not in blob
    # …and the pbkdf2 marker cannot appear under ANY key name.
    assert "pbkdf2_sha256$" not in blob


def test_config_username_is_null_when_there_is_no_login(monkeypatch, tmp_path, fake_jsonl):
    """`auth_mode: "none"` has no operator to name, and the tile must not invent one.

    `AuthConfig` still carries a default username in that mode (the uid the auto-established
    session is signed with), so the route has to decide rather than pass it through — otherwise
    a no-login install would show a tile reading ADMIN, which claims a login it does not have.
    """
    monkeypatch.delenv("AGENT_SESSIONS_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    cfg_none = AuthConfig.from_env()
    # The default IS present on the config object — which is exactly why the route must not
    # forward it blindly.
    assert cfg_none.username == "admin"

    c = _client(cfg_none)
    body = c.get("/api/config").json()  # no login needed in this mode
    assert body["auth_mode"] == "none"
    assert body["username"] is None
