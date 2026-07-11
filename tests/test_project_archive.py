"""Project archive/unarchive (#361 Phase 2): entity flag + engine-aware bulk over the
resolver-derived member set, per-session result report, idempotent blind-retry
semantics, and archived-entity visibility."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions.main import create_app

_REPO_A = "/home/user/claude/repo/a"
_SID_1 = "claude:11111111-1111-1111-1111-111111111111"
_SID_2 = "claude:22222222-2222-2222-2222-222222222222"
_SID_3 = "claude:33333333-3333-3333-3333-333333333333"
_OC_TOP = "opencode:ses_aaaaaaaaaaaaaaaaaaaaaaaa"  # cwd /home/user/claude (conftest)


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


def _hdr(csrf, cfg):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _results(payload) -> dict[str, str]:
    return {row["id"]: row["result"] for row in payload["sessions"]}


def test_archive_mixed_engines_and_unarchive_restores(auth_cfg, fake_jsonl, opencode_db, tmp_home):
    """Claude members archive via the JSONL move (+ sidecar flag, #194); opencode
    members via the sidecar tri-state override only — its DB is never written."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    # /home/user/claude covers BOTH the claude repo/a sessions (nested cwd) and the
    # opencode top session (exact cwd).
    pid = c.post(
        "/api/projects", json={"name": "Mixed", "folders": ["/home/user/claude"]}, headers=h
    ).json()["id"]
    db_before = opencode_db.read_bytes()

    r = c.post(f"/api/projects/{pid}/archive", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["archived"] is True
    res = _results(d)
    assert res[_SID_1] == "archived" and res[_SID_2] == "archived"
    assert res[_OC_TOP] == "archived"
    assert d["counts"]["failed"] == 0
    # engine stores: claude JSONL moved to the archive tree; opencode.db untouched
    assert not list((tmp_home / ".claude" / "projects").glob("*/11111111-*.jsonl"))
    assert list((tmp_home / ".claude" / "projects-archive").glob("*/11111111-*.jsonl"))
    assert opencode_db.read_bytes() == db_before

    # archived members show in the archived view, grouped under the project ref
    rows = c.get("/api/sessions?archived=1&limit=50").json()["sessions"]
    archived_ids = {x["id"] for x in rows}
    assert {_SID_1, _SID_2, _OC_TOP} <= archived_ids
    assert all(x["project"]["id"] == pid for x in rows if x["id"] in {_SID_1, _SID_2, _OC_TOP})
    # … and the entity is hidden from the default list, present with the opt-in
    assert pid not in {p["id"] for p in c.get("/api/projects").json()["projects"]}
    assert pid in {p["id"] for p in c.get("/api/projects?include_archived=1").json()["projects"]}

    r = c.post(f"/api/projects/{pid}/unarchive", headers=h)
    res = _results(r.json())
    assert res[_SID_1] == "unarchived" and res[_OC_TOP] == "unarchived"
    assert list((tmp_home / ".claude" / "projects").glob("*/11111111-*.jsonl"))
    assert pid in {p["id"] for p in c.get("/api/projects").json()["projects"]}
    active_ids = {x["id"] for x in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert {_SID_1, _SID_2, _OC_TOP} <= active_ids


def test_archive_is_idempotent_blind_retry(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Twice", "folders": [_REPO_A]}, headers=h).json()[
        "id"
    ]
    first = _results(c.post(f"/api/projects/{pid}/archive", headers=h).json())
    assert set(first.values()) == {"archived"}
    second = c.post(f"/api/projects/{pid}/archive", headers=h).json()
    assert set(_results(second).values()) == {"already_archived"}
    assert second["counts"]["archived"] == 0


def test_membership_scope_explicit_in_dangling_out(auth_cfg, fake_jsonl):
    """Explicit `project_id` members are swept even with no folder match; a session
    whose explicit id dangles to a DELETED project is never swept in."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "NoFolders"}, headers=h).json()["id"]
    gone = c.post("/api/projects", json={"name": "Gone"}, headers=h).json()["id"]
    # explicit member in /tmp/other (not adopted by anything)
    c.patch(f"/api/sessions/{_SID_3}/metadata", json={"project_id": pid}, headers=h)
    # dangling: assigned to `gone`, then the entity is deleted
    c.patch(f"/api/sessions/{_SID_1}/metadata", json={"project_id": gone}, headers=h)
    c.delete(f"/api/projects/{gone}", headers=h)

    res = _results(c.post(f"/api/projects/{pid}/archive", headers=h).json())
    assert res == {_SID_3: "archived"}  # the dangling session is NOT in the report


def test_partial_failure_reports_and_blind_recall_retries_only_failed(
    auth_cfg, fake_jsonl, monkeypatch
):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Flaky", "folders": [_REPO_A]}, headers=h).json()[
        "id"
    ]

    from agent_sessions import archive as _archive
    from agent_sessions.engines import claude as claude_mod

    real = _archive.archive

    def flaky(uuid, home=None):
        if uuid.startswith("22222222"):
            raise _archive.ArchiveError("disk says no")
        return real(uuid, home)

    monkeypatch.setattr(claude_mod._archive, "archive", flaky)
    d = c.post(f"/api/projects/{pid}/archive", headers=h).json()
    res = _results(d)
    assert res[_SID_1] == "archived"
    assert res[_SID_2] == "failed"
    assert (
        "disk says no" in [row.get("reason", "") for row in d["sessions"] if row["id"] == _SID_2][0]
    )
    # the entity flag was set FIRST, so the project is archived despite the failure
    assert pid in {p["id"] for p in c.get("/api/projects?include_archived=1").json()["projects"]}

    # blind re-call with the failure healed: only the failed member is (re)archived
    monkeypatch.setattr(claude_mod._archive, "archive", real)
    res2 = _results(c.post(f"/api/projects/{pid}/archive", headers=h).json())
    assert res2[_SID_1] == "already_archived"
    assert res2[_SID_2] == "archived"


def test_archive_unknown_project_404_and_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    assert c.post("/api/projects/p-nope/archive", headers=h).status_code == 404
    assert c.post("/api/projects/p-x/archive").status_code == 403  # no CSRF
    assert c.post("/api/projects/p-x/unarchive").status_code == 403


def test_metadata_patch_still_validates_against_archived_projects(auth_cfg, fake_jsonl):
    """Assignment to an ARCHIVED project stays possible (it exists; Settings shows it
    via include_archived) — only deleted projects 422."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Arch"}, headers=h).json()["id"]
    c.post(f"/api/projects/{pid}/archive", headers=h)
    r = c.patch(f"/api/sessions/{_SID_1}/metadata", json={"project_id": pid}, headers=h)
    assert r.status_code == 200


# ---- per-member runtime teardown on project archive (#631) --------------------


def test_project_archive_reaps_each_member_runtime(auth_cfg, fake_jsonl, monkeypatch):
    # #631 Phase B: project archive used to never call cleanup_runtime, so every member's
    # dtach master + agent kept running. It must now reap each member BEFORE archiving it.
    from agent_sessions.routes import sessions as sroutes

    seen: list = []

    async def rec_cleanup(engine, native, *, spare_if=None):
        seen.append((engine, native))
        return "gone"

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", rec_cleanup)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    r = c.post(f"/api/projects/{pid}/archive", headers=h)
    assert r.status_code == 200
    assert set(seen) == {
        ("claude", "11111111-1111-1111-1111-111111111111"),
        ("claude", "22222222-2222-2222-2222-222222222222"),
    }


def test_project_archive_cleanup_failure_does_not_abort_batch(auth_cfg, fake_jsonl, monkeypatch):
    # #631 Phase B: per-member teardown is best-effort — one member's cleanup failure must not
    # abort the batch, and every member still archives.
    from agent_sessions.routes import sessions as sroutes

    async def boom_cleanup(engine, native, *, spare_if=None):
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", boom_cleanup)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    d = c.post(f"/api/projects/{pid}/archive", headers=h).json()
    res = _results(d)
    assert res[_SID_1] == "archived" and res[_SID_2] == "archived"
    assert d["counts"]["failed"] == 0


def test_project_archive_skips_owned_background_agent_member(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    # #631 (Hermes): a Claude background-agent transcript swept into a PROJECT archive must NOT
    # be moved out from under the live fork. It's reported `failed` and stays in the live tree,
    # while the other members archive normally — the batch stays retryable.
    from agent_sessions.routes import sessions as sroutes

    owned = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        sroutes.transcript_owner, "transcript_is_owned", lambda uuid, **kw: uuid == owned
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    d = c.post(f"/api/projects/{pid}/archive", headers=h).json()
    res = _results(d)
    assert res[_SID_1] == "failed"  # the owned bg-agent member is skipped, not moved
    reason = next(r.get("reason", "") for r in d["sessions"] if r["id"] == _SID_1)
    assert "background agent" in reason
    assert res[_SID_2] == "archived"  # the normal member still archives
    # the owned member's JSONL stays LIVE; only the normal member moved to the archive tree
    assert list((tmp_home / ".claude" / "projects").glob(f"*/{owned}.jsonl"))
    assert not list((tmp_home / ".claude" / "projects-archive").glob(f"*/{owned}.jsonl"))
    assert list((tmp_home / ".claude" / "projects-archive").glob("*/22222222-*.jsonl"))


def test_project_unarchive_never_reaps(auth_cfg, fake_jsonl, monkeypatch):
    # #631 Phase B: archive reaps each member; unarchive restores them and must NEVER reap.
    from agent_sessions.routes import sessions as sroutes

    calls: list = []

    async def rec_cleanup(engine, native, *, spare_if=None):
        calls.append((engine, native))
        return "gone"

    monkeypatch.setattr(sroutes.runtime_cleanup, "cleanup_runtime", rec_cleanup)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    c.post(f"/api/projects/{pid}/archive", headers=h)
    assert len(calls) == 2  # both members reaped on archive
    calls.clear()
    r = c.post(f"/api/projects/{pid}/unarchive", headers=h)
    assert r.status_code == 200
    assert calls == []  # unarchive never reaps
