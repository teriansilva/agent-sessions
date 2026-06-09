"""Endpoint tests for the sidebar-UX surface: pagination, projects, rename,
archive/unarchive, new-session — including CSRF/origin gating."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

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
    # CSRF token comes from the SPA bootstrap endpoint (/api/config).
    return c.get("/api/config").json()["csrf"]


# ---- pagination ---------------------------------------------------------------


def test_sessions_paginated_shape(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions?limit=2&offset=0")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"sessions", "next_offset", "total", "facets"}
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


def test_filter_by_project(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get(f"/api/sessions?project={_REPO_A}&limit=50").json()
    assert d["total"] == 2
    assert all(s["project"] == _REPO_A for s in d["sessions"])


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
    # facets are computed over the full archived-scoped set, so they survive a
    # zero-match filter (the dropdowns must still offer every project).
    assert set(d["facets"]["projects"]) == {_REPO_A, _TMP_OTHER, _DEMOAPP}


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
    # Only one row loaded, but every live project must still be an option.
    d = c.get("/api/sessions?limit=1&offset=0").json()
    assert len(d["sessions"]) == 1
    assert set(d["facets"]["projects"]) == {_REPO_A, _TMP_OTHER, _DEMOAPP}
    assert d["facets"]["engines"] == ["claude"]


def test_facets_scoped_by_archived(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?archived=1&limit=50").json()
    assert d["facets"]["projects"] == [_OLD]
    assert d["facets"]["engines"] == ["claude"]


# ---- projects picker ----------------------------------------------------------


def test_projects_endpoint(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/projects")
    assert r.status_code == 200
    cwds = {p["cwd"] for p in r.json()["projects"]}
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
    # The opencode-only cwd appears in the filter facets …
    facet_projects = set(c.get("/api/sessions?limit=100").json()["facets"]["projects"])
    assert "/tmp/oc-only" in facet_projects
    # … and is therefore manageable via /api/projects (previously Claude-only → it drifted).
    picker = {p["cwd"] for p in c.get("/api/projects").json()["projects"]}
    assert "/tmp/oc-only" in picker


# ---- #174: server-side hide propagates to /api/sessions + /api/projects -----


def test_hidden_projects_filtered_from_sessions_and_facets(auth_cfg, fake_jsonl, tmp_home):
    """A hidden cwd disappears from `/api/sessions` rows, totals, and the project facet —
    so the sidebar's pagination + filter dropdown describe the visible set, not the
    full one (Hermes #174 review: client-only filtering would make totals lie)."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # Sanity: before hiding, /tmp/other is present.
    before = c.get("/api/sessions?limit=100").json()
    assert "/tmp/other" in {row["cwd"] for row in before["sessions"]}
    assert "/tmp/other" in {p for p in before["facets"]["projects"]} or before["facets"]["projects"]
    pre_total = before["total"]

    # Hide it via the new key and confirm it's absent server-side.
    prefs.set_projects_hidden(["/tmp/other"])
    after = c.get("/api/sessions?limit=100").json()
    assert "/tmp/other" not in {row["cwd"] for row in after["sessions"]}
    assert "/tmp/other" not in after["facets"]["projects"]
    assert after["total"] <= pre_total  # the hidden rows are gone from the total too


def test_hidden_projects_filtered_from_projects_endpoint(auth_cfg, fake_jsonl, tmp_home):
    """The new-session picker (`/api/projects`) must also drop hidden cwds — the user
    said they don't want to see this project anywhere."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert "/tmp/other" in {p["cwd"] for p in c.get("/api/projects").json()["projects"]}
    prefs.set_projects_hidden(["/tmp/other"])
    assert "/tmp/other" not in {p["cwd"] for p in c.get("/api/projects").json()["projects"]}


def test_legacy_overview_excluded_post_routes_to_projects_hidden(auth_cfg, fake_jsonl, tmp_home):
    """A client still POSTing the legacy `overview_excluded` key must end up writing the
    new `projects_hidden` storage, so existing tabs in the wild stay functional."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"overview_excluded": ["/tmp/other"]},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert prefs.get_projects_hidden() == ["/tmp/other"]


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


def test_upload_empty_is_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/upload",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_api_config_returns_csrf_engines_backend(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert d["csrf"]  # non-empty token for SPA mutations
    assert "claude" in d["new_session_engines"]
    assert d["terminal_backend"] == "ws"


def test_api_config_requires_auth(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    assert c.get("/api/config", follow_redirects=False).status_code in (401, 403)


def test_api_config_advertises_opencode_new_session(auth_cfg, fake_jsonl, opencode_db):
    # #127: opencode is now offered in the new-session picker (present + supports_new).
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
    monkeypatch.setattr(up, "apply", lambda: True)
    assert c.post("/api/update/apply", headers=hdr).status_code == 202
    monkeypatch.setattr(up, "apply", lambda: False)  # not an install
    assert c.post("/api/update/apply", headers=hdr).status_code == 503


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
    projects = c.get("/api/sessions?limit=200").json()["facets"]["projects"]
    assert len(projects) >= 2  # the fixture has several distinct project cwds
    keep = projects[0]
    r = c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [keep]},
        headers=hdr,
    )
    assert r.status_code == 200
    d = c.get("/api/sessions?limit=200").json()
    # only the allowlisted project survives — list + facets agree
    assert d["facets"]["projects"] == [keep]
    assert all(s["project"] == keep for s in d["sessions"])


def test_all_mode_hide_still_excludes(auth_cfg, fake_jsonl):
    # Regression: the legacy denylist behavior is unchanged in the default mode.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    projects = c.get("/api/sessions?limit=200").json()["facets"]["projects"]
    drop = projects[0]
    c.post("/api/prefs", json={"projects_hidden": [drop]}, headers=hdr)
    after = c.get("/api/sessions?limit=200").json()["facets"]["projects"]
    assert drop not in after


def test_projects_picker_unfiltered_in_included_mode(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    all_cwds = {p["cwd"] for p in c.get("/api/projects").json()["projects"]}
    assert all_cwds
    keep = sorted(all_cwds)[0]
    c.post(
        "/api/prefs",
        json={"projects_mode": "included", "projects_included": [keep]},
        headers=hdr,
    )
    # the picker still offers EVERY discovered dir (start anywhere → auto-include), not just the
    # allowlist — otherwise the curated mode would lock you out of adding a new project.
    incl_cwds = {p["cwd"] for p in c.get("/api/projects").json()["projects"]}
    assert incl_cwds == all_cwds


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
