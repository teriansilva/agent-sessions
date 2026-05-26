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


# ---- filters: search / project / engine --------------------------------------

# Fixture project keys (project_alias unset → key == cwd).
_REPO_A = "/home/user/claude/repo/a"  # sessions 1111 + 2222
_TMP_OTHER = "/tmp/other"  # session 3333
_example-app = "/home/user/claude/example-app"  # session 5555
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
    assert set(d["facets"]["projects"]) == {_REPO_A, _TMP_OTHER, _example-app}


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
    assert set(d["facets"]["projects"]) == {_REPO_A, _TMP_OTHER, _example-app}
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
