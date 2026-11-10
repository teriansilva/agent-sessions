"""Objectives in another project, chosen BY INDEX (#1797 S2b, #1613 P4 slice 2).

Two REAL deployments drive these: the procedure demo is reviewed, bound and applied to project A
(the mission's) and to project B with B's own bindings, through the lifecycle. A procedure applied
"in B" must carry B's identity, B's reviewed targets and B's variables, and the project is only
ever named by an index into a server-built list whose digest the client echoes.
"""

from __future__ import annotations

import copy
import sqlite3

import pytest

from agent_sessions import mission_procedures, missions, projects
from agent_sessions.playbooks import apply, lifecycle, review
from test_mission_procedures import (
    LOCAL,
    PB,
    _client,
    _counts,
    _deploy,
    _files,
    _login,
    _rows,
    _write_bundle,
)
from test_mission_procedures import setup as _setup
from test_playbook_review import KEY

setup = _setup

BETA = [
    {"name": "repo", "kind": "text", "value": "org/beta"},
    {"name": "work_branch", "kind": "text", "value": "feature/beta"},
]


def _deploy_beta(tmp_path, name: str = "Beta", bindings=None, n: int = 0) -> projects.Project:
    """Project B: its own folder, the same reviewed bundle, ITS OWN bindings, applied through the
    real lifecycle into the SAME missions store as A."""
    folder = tmp_path / name.lower()
    folder.mkdir()
    entity = projects.create(name, folders=[str(folder)], default_folder=str(folder))
    revision = _write_bundle(_files())
    inputs = {
        "destination": str(folder),
        "revision": revision,
        "bindings": BETA if bindings is None else bindings,
        "project_id": entity.id,
    }
    plan = review.build(PB, inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, PB, inputs, receipt, f"bind-op-2000{n}", key=KEY)
    apply.apply(entity.id, f"apply-op-2000{n}", bound["receipt"], key=KEY)
    return entity


@pytest.fixture
def two(setup, tmp_path, monkeypatch):
    folder, _body = setup
    a = _deploy(folder, tmp_path, monkeypatch, _files())
    a["beta"] = _deploy_beta(tmp_path)
    yield a
    missions.reset_schema_cache_for_test()


def _pick(mid: str, project_id: str) -> dict:
    listed = mission_procedures.project_list(mid)
    row = next(p for p in listed["projects"] if p["project_id"] == project_id)
    return {"index": row["index"], "projects": listed["digest"]}


def _apply_in(mid: str, project_id: str, flow_id: str = "dev") -> dict:
    chosen = _pick(mid, project_id)
    listing = mission_procedures.listing(mid, chosen)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == flow_id)
    return mission_procedures.apply_listed(mid, index, listing["listing"], project=chosen)


# ---- the project list ---------------------------------------------------------------------------


def test_the_project_list_is_every_unarchived_project_sorted_by_id(two, tmp_path):
    mid = two["mission"]
    bare = projects.create("No folder")
    gone = tmp_path / "gone"
    gone.mkdir()
    archived = projects.create("Old", folders=[str(gone)], default_folder=str(gone))
    projects.update(archived.id, archived=True)
    listed = mission_procedures.project_list(mid)
    ids = [p["project_id"] for p in listed["projects"]]
    assert ids == sorted(ids) and archived.id not in ids
    assert {two["project"].id, two["beta"].id, bare.id} <= set(ids)
    assert [p["index"] for p in listed["projects"]] == list(range(len(ids)))
    by_id = {p["project_id"]: p for p in listed["projects"]}
    assert by_id[two["beta"].id]["ready"] and by_id[two["beta"].id]["reason"] is None
    assert not by_id[bare.id]["ready"]
    assert by_id[bare.id]["reason"] == "that project has no folder to work in"
    assert set(by_id[bare.id]) == {"index", "project_id", "name", "ready", "reason"}


def test_a_rename_is_not_a_reshuffle(two):
    mid = two["mission"]
    chosen = _pick(mid, two["beta"].id)
    projects.update(two["beta"].id, name="Beta renamed")
    assert mission_procedures.project_list(mid)["digest"] == chosen["projects"]
    assert mission_procedures.listing(mid, chosen)["project_id"] == two["beta"].id


def test_a_moved_folder_is_a_reshuffle(two, tmp_path):
    mid = two["mission"]
    chosen = _pick(mid, two["beta"].id)
    moved = tmp_path / "beta-moved"
    moved.mkdir()
    old = two["beta"].folders[0]
    projects.update(two["beta"].id, folders=[old, str(moved)], default_folder=str(moved))
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.listing(mid, chosen)
    assert e.value.status == 409


# ---- applying in another project ----------------------------------------------------------------


def test_a_procedure_applied_in_another_project_carries_that_projects_identity(two):
    mid, beta = two["mission"], two["beta"].id
    chosen = _pick(mid, beta)
    listing = mission_procedures.listing(mid, chosen)
    assert listing["project_id"] == beta and listing["primary_project_id"] == two["project"].id
    assert listing["listing"] != mission_procedures.listing(mid)["listing"]
    assert _apply_in(mid, beta) == {"revision": 1, "objective": "o1"}
    rows = [r for r in missions.objectives(mid) if r["key"].startswith("o1--")]
    assert rows and {r["project_id"] for r in rows} == {beta}
    args = [r["probe_args"] for r in rows if r.get("probe_args")]
    assert any("org/beta" in str(a) or "feature/beta" in str(a) for a in args)
    assert not any("org/app" in str(a) or "feature/x" in str(a) for a in args)
    doc = missions.plan_revision(mid)["document"]
    assert doc["objectives"][0]["project_id"] == beta
    # The mission's own project is untouched: it is still the primary.
    assert missions.get_mission(mid)["project_id"] == two["project"].id


def test_a_mission_local_procedure_in_another_project_uses_that_projects_variables(two):
    mid, beta = two["mission"], two["beta"].id
    out = mission_procedures.apply_local(
        mid, "check-branch", copy.deepcopy(LOCAL), _pick(mid, beta)
    )
    assert out == {"revision": 1, "objective": "o1"}
    row = _rows(mid)["o1--on_branch"]
    assert row["probe_args"] == {"branch": "feature/beta"} and row["project_id"] == beta


def test_a_stale_project_list_or_a_bad_index_writes_nothing(two):
    mid, beta = two["mission"], two["beta"].id
    chosen = _pick(mid, beta)
    listing = mission_procedures.listing(mid, chosen)
    projects.create("Newcomer")
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, listing["listing"], project=chosen)
    assert e.value.status == 409
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(mid, "check-branch", copy.deepcopy(LOCAL), chosen)
    assert e.value.status == 409
    fresh = mission_procedures.project_list(mid)
    for bad, status in (
        ({"index": len(fresh["projects"]), "projects": fresh["digest"]}, 422),
        ({"index": -1, "projects": fresh["digest"]}, 422),
        ({"index": True, "projects": fresh["digest"]}, 422),
        ({"index": 0}, 422),
        ({"index": 0, "projects": fresh["digest"], "project_id": beta}, 422),
        ("p-" + "0" * 12, 422),
    ):
        with pytest.raises(missions.MissionError) as e:
            mission_procedures.listing(mid, bad)
        assert e.value.status == status, bad
    assert _counts(mid) == (0, 0)


def test_a_folder_less_project_cannot_be_chosen(two):
    mid = two["mission"]
    bare = projects.create("No folder")
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(
            mid, "check-branch", copy.deepcopy(LOCAL), _pick(mid, bare.id)
        )
    assert e.value.status == 409 and "no folder to work in" in str(e.value)
    assert _counts(mid) == (0, 0)


def test_a_project_without_a_deployment_is_ready_but_lists_nothing_to_apply(two, tmp_path):
    mid = two["mission"]
    folder = tmp_path / "plain"
    folder.mkdir()
    plain = projects.create("Plain", folders=[str(folder)], default_folder=str(folder))
    chosen = _pick(mid, plain.id)
    row = next(
        p for p in mission_procedures.project_list(mid)["projects"] if p["project_id"] == plain.id
    )
    assert row["ready"]
    listing = mission_procedures.listing(mid, chosen)
    assert listing["procedures"] == [] and listing["reason"]
    with pytest.raises(missions.MissionError):
        mission_procedures.apply_listed(mid, 0, listing["listing"], project=chosen)
    assert _counts(mid) == (0, 0)


JUDGED = {
    "format": 5,
    "title": "Judged",
    "steps": [
        {
            "id": "check",
            "title": "Check",
            "actor": {"kind": "agent"},
            "checklist": [{"key": "done", "title": "Done", "probe": "supervisor_judged"}],
        }
    ],
}


def _plain(tmp_path, name: str = "Plain") -> projects.Project:
    """A project with a folder and NO deployment: it can be archived or deleted at any time."""
    folder = tmp_path / name.lower()
    folder.mkdir()
    return projects.create(name, folders=[str(folder)], default_folder=str(folder))


@pytest.mark.parametrize("how", ["archive", "delete"])
def test_a_project_archived_or_deleted_under_the_apply_is_refused_in_the_transaction(
    two, tmp_path, monkeypatch, how
):
    """The project list was right when read; the project goes away between the read and the
    write. The transaction re-checks it, so nothing is written."""
    mid = two["mission"]
    plain = _plain(tmp_path)
    chosen = _pick(mid, plain.id)
    real = missions.apply_procedure

    def racing(*a, **kw):
        if how == "archive":
            projects.update(plain.id, archived=True)
        else:
            projects.delete(plain.id)
        return real(*a, **kw)

    monkeypatch.setattr(missions, "apply_procedure", racing)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED), chosen)
    assert e.value.status == 409 and "archived or no longer exists" in str(e.value)
    assert _counts(mid) == (0, 0)


def test_a_mission_local_procedure_in_a_project_without_a_deployment_applies(two, tmp_path):
    mid = two["mission"]
    plain = _plain(tmp_path)
    out = mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED), _pick(mid, plain.id))
    assert out == {"revision": 1, "objective": "o1"}
    assert _rows(mid)["o1--done"]["project_id"] == plain.id


@pytest.mark.parametrize("how", ["archive", "delete"])
def test_an_archived_or_deleted_project_is_refused_at_listing(two, tmp_path, how):
    mid = two["mission"]
    plain = _plain(tmp_path)
    chosen = _pick(mid, plain.id)
    if how == "archive":
        projects.update(plain.id, archived=True)
    else:
        projects.delete(plain.id)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.listing(mid, chosen)
    assert e.value.status == 409
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED), chosen)
    assert e.value.status == 409
    assert _counts(mid) == (0, 0)


def _move_mission(mid: str, project_id: str) -> None:
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET project_id=? WHERE id=?", (project_id, mid))
    con.commit()
    con.close()


def test_the_mission_moving_project_refuses_a_listing_built_for_another_project(two):
    """The listing binds the PRIMARY as well as the chosen project: a mission whose own project
    changed since the read refuses it, though the objective's project did not change."""
    mid, beta = two["mission"], two["beta"].id
    chosen = _pick(mid, beta)
    listing = mission_procedures.listing(mid, chosen)
    _move_mission(mid, beta)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, listing["listing"], project=chosen)
    assert e.value.status == 409 and "list changed" in str(e.value)
    assert _counts(mid) == (0, 0)


def test_the_mission_moving_project_under_the_apply_is_refused_in_the_transaction(two, monkeypatch):
    mid, beta = two["mission"], two["beta"].id
    real = missions.apply_procedure

    def racing(*a, **kw):
        _move_mission(mid, beta)
        return real(*a, **kw)

    monkeypatch.setattr(missions, "apply_procedure", racing)
    with pytest.raises(missions.MissionError) as e:
        _apply_in(mid, beta)
    assert e.value.status == 409 and "mission's project changed" in str(e.value)
    assert _counts(mid) == (0, 0)


# ---- the primary rule ---------------------------------------------------------------------------


def test_the_mission_may_move_to_any_project_its_objectives_name_and_no_other(two, tmp_path):
    mid, alpha, beta = two["mission"], two["project"].id, two["beta"].id
    _apply_in(mid, alpha)
    _apply_in(mid, beta)
    named = {o["project_id"] for o in missions.plan_revision(mid)["document"]["objectives"]}
    assert named == {alpha, beta}
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(missions.MissionError) as e:
        missions.put_plan(
            mid, project_id="p-ffffffffffff", cwd=str(elsewhere), engine="claude", brief="go"
        )
    assert e.value.status == 409
    plan = missions.put_plan(
        mid, project_id=beta, cwd=str(two["beta"].folders[0]), engine="claude", brief="go"
    )
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"], project_id="p-ffffffffffff", cwd=str(elsewhere))
    assert e.value.status == 409


def test_an_objective_whose_evidence_was_dropped_still_names_its_project(two):
    mid, alpha, beta = two["mission"], two["project"].id, two["beta"].id
    _apply_in(mid, alpha)
    _apply_in(mid, beta)
    revisions = _counts(mid)[1]
    for row in missions.objectives(mid):
        if row["project_id"] == beta:
            missions.patch_objectives(mid, [{"op": "drop", "key": row["key"]}])
    assert not [r for r in missions.objectives(mid) if r["project_id"] == beta]
    assert _counts(mid)[1] == revisions  # dropping evidence writes no plan revision
    missions.put_plan(
        mid, project_id=beta, cwd=str(two["beta"].folders[0]), engine="claude", brief="go"
    )


# ---- the routes ---------------------------------------------------------------------------------


def test_the_routes_choose_the_project_by_index(auth_cfg, two):
    mid, beta = two["mission"], two["beta"].id
    c = _client(auth_cfg)
    assert c.get(f"/api/missions/{mid}/procedures/projects").status_code == 401
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    r = c.get(f"/api/missions/{mid}/procedures/projects")
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    listed = r.json()
    index = next(p["index"] for p in listed["projects"] if p["project_id"] == beta)
    q = {"project": index, "projects": listed["digest"]}
    listing = c.get(f"/api/missions/{mid}/procedures", params=q).json()
    assert listing["project_id"] == beta
    dev = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "dev")
    stale = {"project": {"index": index, "projects": "0" * 64}, "index": dev}
    body = {"project": {"index": index, "projects": listed["digest"]}, "index": dev}
    r = c.post(
        f"/api/missions/{mid}/procedure", json={**stale, "listing": listing["listing"]}, headers=hdr
    )
    assert r.status_code == 409
    for bad in (
        {**body, "listing": listing["listing"], "project": 1},
        {**body, "listing": listing["listing"], "project": {"index": index}},
        {"project": body["project"]},
    ):
        r = c.post(f"/api/missions/{mid}/procedure", json=bad, headers=hdr)
        assert r.status_code == 422, bad
    assert _counts(mid) == (0, 0)
    r = c.post(
        f"/api/missions/{mid}/procedure", json={**body, "listing": listing["listing"]}, headers=hdr
    )
    assert r.status_code == 200 and r.json() == {"revision": 1, "objective": "o1"}
    local = {
        "project": body["project"],
        "mission_local": {"flow_id": "check-branch", "flow": LOCAL},
    }
    r = c.post(f"/api/missions/{mid}/procedure", json=local, headers=hdr)
    assert r.status_code == 200
    assert _rows(mid)["o2--on_branch"]["probe_args"] == {"branch": "feature/beta"}
    unknown = "msn_" + "0" * 32
    assert c.get(f"/api/missions/{unknown}/procedures/projects").status_code == 404


# ---- the independent review of S2b --------------------------------------------------------------


def test_the_same_procedure_with_the_same_values_in_another_project_is_another_objective(
    two, tmp_path
):
    """The duplicate check is per project: B's deployment is not A's, even when every input
    value happens to be the same."""
    mid = two["mission"]
    same = [
        {"name": "repo", "kind": "text", "value": "org/gamma"},
        {"name": "work_branch", "kind": "text", "value": "feature/x"},  # A's value too
    ]
    gamma = _deploy_beta(tmp_path, "Gamma", same, 1)
    assert _apply_in(mid, two["project"].id)["objective"] == "o1"
    assert _apply_in(mid, gamma.id)["objective"] == "o2"
    with pytest.raises(missions.MissionError) as e:  # still a duplicate within ONE project
        _apply_in(mid, gamma.id)
    assert e.value.status == 409 and "already applies" in str(e.value)


def test_the_same_mission_local_procedure_in_two_projects_is_two_objectives(two, tmp_path):
    mid = two["mission"]
    one, other = _plain(tmp_path, "One"), _plain(tmp_path, "Other")
    for p, oid in ((one, "o1"), (other, "o2")):
        out = mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED), _pick(mid, p.id))
        assert out["objective"] == oid


def test_a_recorded_merge_pins_the_mission_to_its_project(two):
    mid, alpha, beta = two["mission"], two["project"].id, two["beta"].id
    _apply_in(mid, alpha)
    _apply_in(mid, beta)
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET merge_sha=? WHERE id=?", ("a" * 40, mid))
    con.commit()
    con.close()
    folder = str(two["beta"].folders[0])
    with pytest.raises(missions.MissionError) as e:
        missions.put_plan(mid, project_id=beta, cwd=folder, engine="claude", brief="go")
    assert e.value.status == 409 and "recorded a merge" in str(e.value)
    plan = missions.put_plan(
        mid, project_id=alpha, cwd=str(two["folder"]), engine="claude", brief="go"
    )
    assert plan["plan_id"]


def test_the_launch_may_move_the_mission_to_another_named_project(two):
    mid, alpha, beta = two["mission"], two["project"].id, two["beta"].id
    _apply_in(mid, alpha)
    _apply_in(mid, beta)
    folder = str(two["beta"].folders[0])
    plan = missions.put_plan(mid, project_id=beta, cwd=folder, engine="claude", brief="go")
    missions.claim_plan(mid, plan["plan_id"], project_id=beta, cwd=folder)
    assert missions.get_mission(mid)["project_id"] == beta


def test_a_null_project_is_not_the_missions_own(auth_cfg, two):
    mid = two["mission"]
    c = _client(auth_cfg)
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    listing = c.get(f"/api/missions/{mid}/procedures").json()
    for body in (
        {"project": None, "index": 0, "listing": listing["listing"]},
        {"project": None, "mission_local": {"flow_id": "check-branch", "flow": LOCAL}},
        {"project": [0, "x"], "index": 0, "listing": listing["listing"]},
    ):
        r = c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr)
        assert r.status_code == 422, body
    assert _counts(mid) == (0, 0)


def test_a_pr_is_picked_from_the_chosen_projects_repo(auth_cfg, two, monkeypatch):
    from test_mission_inputs import FakeForge, _pr

    fake = FakeForge([_pr(21, "feature/b1", repo="org/beta"), _pr(22, "feature/a1")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid, beta = two["mission"], two["beta"].id
    chosen = _pick(mid, beta)
    listing = mission_procedures.listing(mid, chosen)
    dev = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "dev")
    c = _client(auth_cfg)
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    q = {"listing": listing["listing"], "project": chosen["index"], "projects": chosen["projects"]}
    r = c.get(f"/api/missions/{mid}/procedures/{dev}/inputs", params=q)
    assert r.status_code == 200
    offered = r.json()
    assert fake.calls == ["org/beta"]  # the chosen project's reviewed repo, and only it
    values = [(ch["value"], ch.get("number")) for ch in offered["inputs"]["work_branch"]]
    assert values[0] == ("feature/beta", None) and ("feature/b1", 21) in values
    pick = next(ch["index"] for ch in offered["inputs"]["work_branch"] if ch.get("number") == 21)
    body = {
        "project": chosen,
        "index": dev,
        "listing": listing["listing"],
        "inputs": {"work_branch": {"choice": pick}},
        "choices": offered["choices"],
    }
    r = c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    row = _rows(mid)["o1--pr_open"]
    assert row["probe_args"] == {"repo": "org/beta", "branch": "feature/b1", "number": 21}
    assert row["project_id"] == beta
    # The inputs read without the project is A's listing, not B's: a stale pairing is refused.
    r = c.get(f"/api/missions/{mid}/procedures/{dev}/inputs", params={"listing": q["listing"]})
    assert r.status_code == 409
