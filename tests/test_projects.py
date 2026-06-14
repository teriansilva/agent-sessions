"""Project entities (#361 Phase 1): store CRUD + folder-conflict rules, THE shared
resolver (precedence, boundary-aware prefix, dangling ids), the one-shot
``project_alias`` → entity migration, and the API surface (entities CRUD, the
``/api/folders`` split, assignment via session-metadata PATCH, facet/filter
back-compat, hidden-folder × adopted-project visibility in both directions, the
read-only-engine guarantee, and the zero-entities default)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from agent_sessions import metadata, prefs, projects
from agent_sessions.main import create_app

# fake_jsonl fixture cwds (see conftest).
_REPO_A = "/home/user/claude/repo/a"
_TMP_OTHER = "/tmp/other"
_DEMOAPP = "/home/user/claude/demoapp.io"
_SID_1 = "claude:11111111-1111-1111-1111-111111111111"
_SID_2 = "claude:22222222-2222-2222-2222-222222222222"
_SID_3 = "claude:33333333-3333-3333-3333-333333333333"


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


# ---- store: CRUD + validation ---------------------------------------------------


def test_create_load_roundtrip(tmp_home):
    p = projects.create("Cayoo", color="#5FD7FF", folders=["/a/b/"])
    assert p.id.startswith("p-")
    assert p.color == "#5fd7ff"  # normalized lowercase
    assert p.folders == ("/a/b",)  # trailing slash stripped
    index = projects.load()
    assert index[p.id].name == "Cayoo"
    assert not index[p.id].archived


def test_create_validation(tmp_home):
    with pytest.raises(projects.ProjectError):
        projects.create("")  # name required
    with pytest.raises(projects.ProjectError):
        projects.create("X", color="red")  # not #rgb/#rrggbb
    with pytest.raises(projects.ProjectError):
        projects.create("X", folders=["relative/path"])  # absolute only


def test_update_rename_color_folders(tmp_home):
    p = projects.create("Old", folders=["/a"])
    q = projects.update(p.id, name="New", color="#fff", folders=["/b"])
    assert (q.name, q.color, q.folders) == ("New", "#fff", ("/b",))
    # None means unchanged; "" clears color
    q = projects.update(p.id, color="")
    assert q.name == "New" and q.color == ""


def test_delete_unknown_404(tmp_home):
    with pytest.raises(projects.ProjectError) as e:
        projects.delete("p-nope")
    assert e.value.status == 404


def test_load_is_fail_soft(tmp_home):
    path = projects._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt")
    assert projects.load() == {}


# ---- store: folder uniqueness (409) + release/re-adopt ---------------------------


def test_folder_conflict_exact_duplicate_409(tmp_home):
    projects.create("One", folders=["/a"])
    with pytest.raises(projects.ProjectError) as e:
        projects.create("Two", folders=["/a"])
    assert e.value.status == 409


def test_folder_conflict_nested_at_different_depth_409(tmp_home):
    # Two projects must never both own a point on one folder path — in EITHER
    # direction (child claimed after parent, parent claimed after child).
    projects.create("One", folders=["/a"])
    with pytest.raises(projects.ProjectError) as e:
        projects.create("Two", folders=["/a/b"])
    assert e.value.status == 409
    projects.create("Three", folders=["/x/y"])
    with pytest.raises(projects.ProjectError) as e:
        projects.create("Four", folders=["/x"])
    assert e.value.status == 409


def test_folder_boundary_sibling_is_not_a_conflict(tmp_home):
    projects.create("One", folders=["/a"])
    projects.create("Two", folders=["/a-foo"])  # boundary-aware: no conflict


def test_same_project_may_own_nested_folders(tmp_home):
    p = projects.create("One", folders=["/a"])
    projects.update(p.id, folders=["/a", "/a/b"])  # no conflict with itself


def test_release_frees_folder_for_readoption(tmp_home):
    p = projects.create("One", folders=["/a"])
    projects.update(p.id, folders=[])  # release
    q = projects.create("Two", folders=["/a"])  # re-adopt elsewhere
    assert q.folders == ("/a",)


# ---- resolver --------------------------------------------------------------------


def test_resolve_precedence_explicit_wins(tmp_home):
    projects.create("A", folders=["/a"])
    b = projects.create("B")
    index = projects.load()
    # cwd matches A's folder, but the explicit assignment to B wins.
    assert projects.resolve("/a/sub", b.id, index).id == b.id


def test_resolve_boundary_aware_prefix(tmp_home):
    a = projects.create("A", folders=["/a"])
    index = projects.load()
    assert projects.resolve("/a", "", index).id == a.id
    assert projects.resolve("/a/b/c", "", index).id == a.id
    ref = projects.resolve("/a-foo", "", index)  # sibling: NOT a member
    assert ref.kind == "folder" and ref.id == "/a-foo"


def test_resolve_most_specific_folder_wins(tmp_home):
    # Overlapping adopted folders can't be created via the API (409), but the
    # resolver must still pick the most specific owner for hand-edited stores —
    # and for one project owning both /a and /a/b the answer is trivially stable.
    index = {
        "p-outer": projects.Project(id="p-outer", name="Outer", folders=("/a",)),
        "p-inner": projects.Project(id="p-inner", name="Inner", folders=("/a/b",)),
    }
    assert projects.resolve("/a/b/c", "", index).id == "p-inner"
    assert projects.resolve("/a/z", "", index).id == "p-outer"


def test_resolve_dangling_project_id_falls_through(tmp_home):
    a = projects.create("A", folders=["/a"])
    index = projects.load()
    # dangling explicit id → folder mapping still applies
    assert projects.resolve("/a/x", "p-deleted", index).id == a.id
    # dangling + no folder match → implicit folder ref, never an error
    ref = projects.resolve("/elsewhere", "p-deleted", index)
    assert ref.kind == "folder" and ref.id == "/elsewhere"


def test_resolve_alias_read_fallback_names_folder_ref(tmp_home):
    # Transition fallback (one release): an unmigrated alias renames the implicit
    # folder group — exactly the pre-#361 `project_alias or cwd` string.
    ref = projects.resolve("/some/dir", "", {}, alias="My Rename")
    assert ref.kind == "folder" and ref.id == "/some/dir" and ref.name == "My Rename"
    assert projects.resolve("/some/dir", "", {}).name == "/some/dir"


# ---- migration: project_alias → entities ------------------------------------------


def test_alias_migration_collapses_shared_pairs_and_backs_up(tmp_home, caplog):
    # Two sessions share one (cwd → alias) pair ⇒ ONE entity; a second pair ⇒ its own.
    mpath = metadata._default_path()
    metadata.patch(_SID_1, title="t1")
    raw = json.loads(mpath.read_text())
    raw[_SID_1]["project_alias"] = "Repo A"
    raw[_SID_2] = {"project_alias": "Repo A"}
    raw[_SID_3] = {"project_alias": "Other"}
    mpath.write_text(json.dumps(raw))

    with caplog.at_level(logging.INFO, logger="agent_sessions.projects"):
        projects.ensure_alias_migration(
            [
                (_SID_1, _REPO_A, "Repo A"),
                (_SID_2, _REPO_A, "Repo A"),
                (_SID_3, _TMP_OTHER, "Other"),
            ]
        )
    index = projects.load()
    by_name = {p.name: p for p in index.values()}
    assert set(by_name) == {"Repo A", "Other"}
    assert by_name["Repo A"].folders == (_REPO_A,)
    # one-shot summary log is observable
    assert any("2 entity(ies) created" in r.message for r in caplog.records)
    # the original sidecar was backed up before the rewrite …
    bak = mpath.with_name(mpath.name + ".pre-projects.bak")
    assert bak.exists() and "Repo A" in bak.read_text()
    # … and the migrated alias fields were stripped (write path retired)
    after = json.loads(mpath.read_text())
    assert after[_SID_1]["project_alias"] == ""
    assert after[_SID_2]["project_alias"] == ""


def test_alias_migration_is_one_shot(tmp_home):
    projects.ensure_alias_migration([(_SID_1, _REPO_A, "Repo A")])
    assert len(projects.load()) == 1
    # second call is a no-op even with new pairs — the flag is set
    projects.ensure_alias_migration([(_SID_3, _TMP_OTHER, "Other")])
    assert len(projects.load()) == 1


def test_alias_migration_skips_already_adopted_folder(tmp_home):
    p = projects.create("Existing", folders=[_REPO_A])
    projects.ensure_alias_migration([(_SID_1, _REPO_A, "Repo A")])
    index = projects.load()
    assert {q.name for q in index.values()} == {"Existing"}
    assert index[p.id].folders == (_REPO_A,)


def test_migration_runs_via_sessions_endpoint(auth_cfg, fake_jsonl):
    # End-to-end: a legacy alias in the sidecar becomes an entity on the first list
    # request, and the rows for that cwd resolve to it (rename survives, as a real
    # project now). project_alias is written via the raw file — patch() retired it.
    mpath = metadata._default_path()
    metadata.patch(_SID_1, title="seed")
    raw = json.loads(mpath.read_text())
    raw[_SID_1]["project_alias"] = "Repo A"
    mpath.write_text(json.dumps(raw))

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    by_id = {r["id"]: r for r in rows}
    ref = by_id[_SID_1]["project"]
    assert ref["kind"] == "project" and ref["name"] == "Repo A"
    # the sibling session in the same cwd folder-resolves into the same entity
    assert by_id[_SID_2]["project"] == ref
    ents = c.get("/api/projects").json()["projects"]
    assert [e["name"] for e in ents] == ["Repo A"]
    assert ents[0]["folders"] == [_REPO_A]
    assert ents[0]["session_count"] == 2


def test_patch_rejects_project_alias_writes(tmp_home):
    with pytest.raises(ValueError):
        metadata.patch(_SID_1, project_alias="nope")


# ---- API: entities CRUD ------------------------------------------------------------


def test_projects_crud_roundtrip(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)

    r = c.post("/api/projects", json={"name": "Cayoo", "folders": [_TMP_OTHER]}, headers=h)
    assert r.status_code == 200
    pid = r.json()["id"]

    r = c.patch(f"/api/projects/{pid}", json={"name": "Cayoo 2", "color": "#5fd7ff"}, headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "Cayoo 2" and r.json()["color"] == "#5fd7ff"

    ents = c.get("/api/projects").json()["projects"]
    assert [e["id"] for e in ents] == [pid]
    assert ents[0]["session_count"] == 1  # the /tmp/other fixture session

    r = c.delete(f"/api/projects/{pid}", headers=h)
    assert r.status_code == 200
    assert c.get("/api/projects").json()["projects"] == []


def test_projects_create_conflict_409_and_patch_archived_rejected(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    r = c.post("/api/projects", json={"name": "A", "folders": ["/a"]}, headers=h)
    pid = r.json()["id"]
    assert (
        c.post("/api/projects", json={"name": "B", "folders": ["/a/b"]}, headers=h).status_code
        == 409
    )
    # archive semantics are Phase 2 (entity flag + member bulk) — not patchable here
    assert c.patch(f"/api/projects/{pid}", json={"archived": True}, headers=h).status_code == 422


def test_projects_mutations_require_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.post("/api/projects", json={"name": "X"}).status_code == 403
    assert c.patch("/api/projects/p-x", json={"name": "X"}).status_code == 403
    assert c.delete("/api/projects/p-x").status_code == 403
    assert c.patch(f"/api/sessions/{_SID_1}/metadata", json={"project_id": ""}).status_code == 403


# ---- API: assignment via session metadata ------------------------------------------


def test_assign_and_clear_project_id(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Side"}, headers=h).json()["id"]

    r = c.patch(f"/api/sessions/{_SID_3}/metadata", json={"project_id": pid}, headers=h)
    assert r.status_code == 200 and r.json()["project_id"] == pid
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    ref = {x["id"]: x for x in rows}[_SID_3]["project"]
    assert ref == {"kind": "project", "id": pid, "name": "Side", "color": ""}

    # a folder-less project groups manually-assigned sessions; clearing reverts
    r = c.patch(f"/api/sessions/{_SID_3}/metadata", json={"project_id": None}, headers=h)
    assert r.json()["project_id"] == ""
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert {x["id"]: x for x in rows}[_SID_3]["project"]["kind"] == "folder"


def test_assign_unknown_project_422_unknown_session_404(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    r = c.patch(f"/api/sessions/{_SID_1}/metadata", json={"project_id": "p-nope"}, headers=h)
    assert r.status_code == 422
    r = c.patch("/api/sessions/bogus/metadata", json={"project_id": ""}, headers=h)
    assert r.status_code == 404


def test_dangling_assignment_falls_back_to_folder(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Gone"}, headers=h).json()["id"]
    c.patch(f"/api/sessions/{_SID_3}/metadata", json={"project_id": pid}, headers=h)
    c.delete(f"/api/projects/{pid}", headers=h)
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    ref = {x["id"]: x for x in rows}[_SID_3]["project"]
    assert ref == {"kind": "folder", "id": _TMP_OTHER, "name": _TMP_OTHER}


def test_assignment_writes_sidecar_only(auth_cfg, fake_jsonl, tmp_home):
    """Read-only-engine guarantee: assigning a project touches the app sidecar, never
    the engine store (here: the claude JSONL is byte-identical afterwards)."""
    jsonl = (
        tmp_home
        / ".claude"
        / "projects"
        / "-tmp-other"
        / "33333333-3333-3333-3333-333333333333.jsonl"
    )
    before = jsonl.read_bytes()
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P"}, headers=h).json()["id"]
    c.patch(f"/api/sessions/{_SID_3}/metadata", json={"project_id": pid}, headers=h)
    assert jsonl.read_bytes() == before
    assert metadata.get(_SID_3).project_id == pid


# ---- API: facets + filter back-compat ----------------------------------------------


def test_facets_list_entities_before_folder_groups(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "Zeta", "folders": [_TMP_OTHER]}, headers=h).json()[
        "id"
    ]
    facets = c.get("/api/sessions?limit=50").json()["facets"]["projects"]
    # entity first despite the "Z" name; folder groups follow, alphabetical
    assert facets[0] == {"kind": "project", "id": pid, "name": "Zeta", "color": "", "count": 1}
    assert [f["kind"] for f in facets[1:]] == ["folder", "folder"]


def test_facet_refs_carry_counts(auth_cfg, fake_jsonl):
    # Each distinct facet ref reports how many scoped rows resolve to it (#361 Phase 3) —
    # the dropdown renders "Name (N)". Counts live on the FACET copies only; the per-row
    # `project` ref shape is unchanged.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    d = c.get("/api/sessions?limit=50").json()
    counts = {(f["kind"], f["id"]): f["count"] for f in d["facets"]["projects"]}
    assert counts[("project", pid)] == 2  # both repo/a fixture sessions
    assert counts[("folder", _TMP_OTHER)] == 1
    assert counts[("folder", _DEMOAPP)] == 1
    assert all("count" not in r["project"] for r in d["sessions"])


def test_filter_by_entity_id_and_bare_cwd_back_compat(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post("/api/projects", json={"name": "P", "folders": [_REPO_A]}, headers=h).json()["id"]
    # by entity id → both repo/a sessions
    d = c.get(f"/api/sessions?project={pid}&limit=50").json()
    assert d["total"] == 2
    # bare cwd (the pre-#361 filter value) still resolves to the same rows
    d = c.get(f"/api/sessions?project={_REPO_A}&limit=50").json()
    assert d["total"] == 2
    assert all(s["project"]["id"] == pid for s in d["sessions"])


# ---- API: hidden-folder × adopted-project, both directions --------------------------


def test_hidden_folder_does_not_hide_adopted_projects_sessions(auth_cfg, fake_jsonl):
    """Direction 1: adopting a HIDDEN folder — the project's sessions are visible
    (membership is independent of folder visibility), but the folder stays hidden
    as a LAUNCH location (adoption never auto-includes it in the picker)."""
    prefs.set_projects_hidden([_TMP_OTHER])
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    # hidden: gone from rows + picker
    assert _TMP_OTHER not in {r["cwd"] for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    pid = c.post(
        "/api/projects", json={"name": "Adopted", "folders": [_TMP_OTHER]}, headers=h
    ).json()["id"]
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    mine = [r for r in rows if r["cwd"] == _TMP_OTHER]
    assert mine and all(r["project"]["id"] == pid for r in mine)
    # … but the folder is still not offered as a launch location
    visible = {f["cwd"] for f in c.get("/api/folders?visible=1").json()["folders"]}
    assert _TMP_OTHER not in visible


def test_hiding_folder_never_removes_it_from_its_project(auth_cfg, fake_jsonl):
    """Direction 2: hiding an adopted folder afterwards — the project keeps the
    folder and its sessions stay visible."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(csrf, auth_cfg)
    pid = c.post(
        "/api/projects", json={"name": "Keeps", "folders": [_TMP_OTHER]}, headers=h
    ).json()["id"]
    prefs.set_projects_hidden([_TMP_OTHER])
    ents = {e["id"]: e for e in c.get("/api/projects").json()["projects"]}
    assert ents[pid]["folders"] == [_TMP_OTHER]
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    assert any(r["cwd"] == _TMP_OTHER and r["project"]["id"] == pid for r in rows)


# ---- zero entities: today's behaviour, exactly ---------------------------------------


def test_zero_entities_rows_are_plain_folder_refs(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/sessions?limit=50").json()
    for r in d["sessions"]:
        assert r["project"] == {"kind": "folder", "id": r["cwd"], "name": r["cwd"]}
    assert all(f["kind"] == "folder" for f in d["facets"]["projects"])
    assert c.get("/api/projects").json()["projects"] == []
