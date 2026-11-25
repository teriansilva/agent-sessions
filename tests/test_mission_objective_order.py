"""Order between objectives, authored at apply only (#1797 S2c, #1613 P4 slice 2).

The order is planning data: `after` on an objective names objectives already in the plan, so a
cycle cannot be expressed, and the derived step states follow it. Dispatch does not (P6).
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from agent_sessions import (
    mission_plan_document as plan_doc,
)
from agent_sessions import mission_procedures, missions, projects
from agent_sessions.playbooks import apply, lifecycle, review
from test_mission_cross_project_apply import JUDGED, _pick
from test_mission_procedures import (
    LOCAL,
    PB,
    _client,
    _counts,
    _deploy,
    _files,
    _login,
    _write_bundle,
)
from test_mission_procedures import setup as _setup
from test_playbook_review import KEY

setup = _setup


@pytest.fixture
def deployed(setup, tmp_path, monkeypatch):
    folder, _body = setup
    yield _deploy(folder, tmp_path, monkeypatch, _files())
    missions.reset_schema_cache_for_test()


def _apply(mid: str, flow_id: str, *, after=None, project=None) -> dict:
    listing = mission_procedures.listing(mid, project)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == flow_id)
    return mission_procedures.apply_listed(
        mid, index, listing["listing"], project=project, after=after
    )


def _row(mid: str, key: str) -> dict:
    return next(o for o in missions.objectives(mid) if o["key"] == key)


def _waive_objective(mid: str, oid: str) -> None:
    keys = [o["key"] for o in missions.objectives(mid) if o["key"].startswith(f"{oid}--")]
    missions.patch_objectives(mid, [{"op": "waive", "key": k} for k in keys])


# ---- the validator ------------------------------------------------------------------------------


def _doc_with_two(deployed) -> dict:
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes", after=["o1"])
    return copy.deepcopy(missions.plan_revision(mid)["document"])


@pytest.mark.parametrize(
    "after, needle",
    [
        (["o9"], "not an earlier objective"),
        (["o2"], "the objective itself"),
        (["o1", "o1"], "twice"),
        ([], "must list"),
        ("o1", "must list"),
        ([1], "invalid objective id"),
    ],
)
def test_the_validator_holds_after_to_earlier_objectives(deployed, after, needle):
    doc = _doc_with_two(deployed)
    doc["objectives"][1]["after"] = after
    with pytest.raises(plan_doc.PlanError) as e:
        plan_doc.validate_document(doc)
    assert needle in str(e.value)


def test_a_forward_reference_is_refused_so_no_cycle_can_be_written(deployed):
    doc = _doc_with_two(deployed)
    doc["objectives"][0]["after"] = ["o2"]
    with pytest.raises(plan_doc.PlanError) as e:
        plan_doc.validate_document(doc)
    assert "not an earlier objective" in str(e.value)


# ---- apply --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "after, status", [(["o9"], 422), (["o2"], 422), (["o1", "o1"], 422), ("o1", 422), ([3], 422)]
)
def test_a_bad_after_at_apply_writes_nothing(deployed, after, status):
    mid = deployed["mission"]
    _apply(mid, "dev")
    before = _counts(mid)
    with pytest.raises(missions.MissionError) as e:
        _apply(mid, "notes", after=after)
    assert e.value.status == status
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED), None, after)
    assert e.value.status == status
    assert _counts(mid) == before


def test_after_is_written_on_the_new_objective_and_empty_means_none(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev", after=[])
    _apply(mid, "notes", after=["o1"])
    doc = missions.plan_revision(mid)["document"]
    assert "after" not in doc["objectives"][0]
    assert doc["objectives"][1]["after"] == ["o1"]
    # Earlier revisions are untouched: order is authored on the NEW objective only.
    assert "after" not in missions.plan_revision(mid, 1)["document"]["objectives"][0]


def test_a_different_order_is_not_a_new_objective(deployed):
    """Order is immutable for the plan's lifetime: re-applying the same procedure with the same
    inputs is a duplicate whatever its `after` says."""
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes")
    with pytest.raises(missions.MissionError) as e:
        _apply(mid, "notes", after=["o1"])
    assert e.value.status == 409 and "already applies" in str(e.value)


# ---- derived state ------------------------------------------------------------------------------


def test_an_objective_waits_for_the_objectives_it_comes_after(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes", after=["o1"])
    out = mission_procedures.plan(mid)
    assert out["step_states"]["o1.plan"] == "ready"
    assert out["step_states"]["o2.write"] == "waiting"
    assert out["objective_states"] == {"o1": "open", "o2": "open"}
    _waive_objective(mid, "o1")
    out = mission_procedures.plan(mid)
    assert out["objective_states"]["o1"] == "done"
    assert out["step_states"]["o2.write"] == "ready"


def test_without_after_objectives_are_independent_as_in_slice_1(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes")
    assert mission_procedures.plan(mid)["step_states"]["o2.write"] == "ready"


def test_every_open_step_waits_while_the_objective_it_comes_after_is_open(deployed):
    """A first step already done from its own evidence keeps `done`, but does NOT release the rest
    of its objective while the objective it comes after is still open (the independent review)."""
    mid = deployed["mission"]
    _apply(mid, "notes")
    _apply(mid, "dev", after=["o1"])
    states = mission_procedures.plan(mid)["step_states"]
    assert states["o2.plan"] == "waiting" and states["o2.implement"] == "waiting"
    missions.patch_objectives(mid, [{"op": "waive", "key": "o2--issue_ready"}])
    states = mission_procedures.plan(mid)["step_states"]
    assert states["o2.plan"] == "done"  # its own fact, though o1 is not done
    assert states["o2.implement"] == "waiting"  # o1 is still open
    _waive_objective(mid, "o1")
    states = mission_procedures.plan(mid)["step_states"]
    assert states["o2.implement"] == "ready" and states["o2.review"] == "waiting"


def test_a_note_only_objective_is_done_and_a_dropped_row_counts_as_waived(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    mission_procedures.apply_local(mid, "judged", copy.deepcopy(JUDGED))
    keys = [o["key"] for o in missions.objectives(mid) if o["key"].startswith("o1--")]
    missions.patch_objectives(mid, [{"op": "drop", "key": k} for k in keys])
    assert mission_procedures.plan(mid)["objective_states"]["o1"] == "done"
    doc = copy.deepcopy(missions.plan_revision(mid)["document"])
    for s in doc["steps"]:
        if s["objective"] == "o2":
            s["evidence"] = []  # a note-only objective, as the derivation sees one
    assert plan_doc.objective_states(doc, missions.objectives(mid))["o2"] == "done"


# ---- rollback: a build that predates S2c --------------------------------------------------------


def _append_raw(mid: str, doc: dict) -> None:
    """A revision written by ANOTHER build (one that knows a key this one does not): direct
    INSERT, the only write the append-only table's triggers allow."""
    con = sqlite3.connect(missions._db_path())
    row = con.execute(
        "SELECT * FROM mission_plan_revisions WHERE mission_id=? ORDER BY revision DESC LIMIT 1",
        (mid,),
    ).fetchone()
    cols = [d[0] for d in con.execute("SELECT * FROM mission_plan_revisions LIMIT 0").description]
    values = dict(zip(cols, row, strict=True))
    values["revision"] += 1
    values["document_json"] = json.dumps(doc)
    marks = ", ".join("?" * len(cols))
    con.execute(
        f"INSERT INTO mission_plan_revisions ({', '.join(cols)}) VALUES ({marks})",
        [values[c] for c in cols],
    )
    con.commit()
    con.close()


def test_a_plan_with_a_key_this_build_does_not_know_reads_but_refuses_to_append(deployed):
    """What a build without S2c does with a plan that has an `after` (a key it does not know):
    the read never re-validates, so it reads; the next append re-validates every earlier
    objective, so it refuses (422) and writes nothing. Shown here with a key THIS build does not
    know, standing in for `after` on the older build. (Cross-checked by the independent review
    against the parent build itself: a real `after` there reads order-blind, and the next apply
    is a 422 with nothing written.)"""
    mid = deployed["mission"]
    _apply(mid, "dev")
    doc = copy.deepcopy(missions.plan_revision(mid)["document"])
    doc["objectives"][0]["later_key"] = ["o0"]
    _append_raw(mid, doc)
    out = mission_procedures.plan(mid)
    assert out["plan"]["revision"] == 2 and out["step_states"]["o1.plan"] == "ready"
    before = _counts(mid)
    with pytest.raises(missions.MissionError) as e:
        _apply(mid, "notes")
    assert e.value.status == 422
    assert _counts(mid) == before


# ---- the route ----------------------------------------------------------------------------------


def test_the_route_takes_after_in_both_shapes(auth_cfg, deployed):
    mid = deployed["mission"]
    c = _client(auth_cfg)
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    listing = c.get(f"/api/missions/{mid}/procedures").json()
    dev = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "dev")
    body = {"index": dev, "listing": listing["listing"]}
    assert c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr).status_code == 200
    local = {"mission_local": {"flow_id": "check-branch", "flow": LOCAL}}
    for bad in (
        {**local, "after": "o1"},
        {**local, "after": ["o7"]},
        {**local, "after": [None]},
        {**local, "after": None},
        {**body, "after": None},
    ):
        r = c.post(f"/api/missions/{mid}/procedure", json=bad, headers=hdr)
        assert r.status_code == 422, bad
    r = c.post(f"/api/missions/{mid}/procedure", json={**local, "after": ["o1"]}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"revision": 2, "objective": "o2"}
    notes = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "notes")
    r = c.post(
        f"/api/missions/{mid}/procedure",
        json={"index": notes, "listing": listing["listing"], "after": ["o1", "o2"]},
        headers=hdr,
    )
    assert r.status_code == 200
    plan = c.get(f"/api/missions/{mid}/plan/revisions/latest").json()
    assert plan["plan"]["document"]["objectives"][2]["after"] == ["o1", "o2"]
    assert plan["objective_states"] == {"o1": "open", "o2": "open", "o3": "open"}
    assert plan["step_states"]["o3.write"] == "waiting"


# ---- the three-project planning acceptance case -------------------------------------------------


def _deploy_into(tmp_path, name: str, repo: str, n: int) -> projects.Project:
    folder = tmp_path / name.lower()
    folder.mkdir()
    entity = projects.create(name, folders=[str(folder)], default_folder=str(folder))
    inputs = {
        "destination": str(folder),
        "revision": _write_bundle(_files()),
        "bindings": [
            {"name": "repo", "kind": "text", "value": repo},
            {"name": "work_branch", "kind": "text", "value": f"feature/{name.lower()}"},
        ],
        "project_id": entity.id,
    }
    plan = review.build(PB, inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, PB, inputs, receipt, f"bind-op-3000{n}", key=KEY)
    apply.apply(entity.id, f"apply-op-3000{n}", bound["receipt"], key=KEY)
    return entity


def test_a_three_project_plan_follows_its_order(setup, tmp_path, monkeypatch):
    """#1797's acceptance case, planning only: Mail, then Files, then Finance — three real
    lifecycle deployments, one mission, one plan, and a scripted forge offering Files' open PR as
    an input. Execution is P6."""
    from test_mission_inputs import FakeForge, _pr

    folder, _body = setup
    base = _deploy(folder, tmp_path, monkeypatch, _files())
    fake = FakeForge([_pr(31, "feature/files-1", repo="org/files")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    try:
        mid = base["mission"]
        mail = base["project"]
        files = _deploy_into(tmp_path, "Files", "org/files", 1)
        finance = _deploy_into(tmp_path, "Finance", "org/finance", 2)
        _apply(mid, "dev")  # o1: Mail (the mission's own project)
        # o2: Files, its branch picked BY INDEX from the scripted forge's open PRs.
        chosen = _pick(mid, files.id)
        listing = mission_procedures.listing(mid, chosen)
        dev = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "dev")
        offered = mission_procedures.input_choices(mid, dev, listing["listing"], chosen)
        assert fake.calls == ["org/files"]
        pick = next(c["index"] for c in offered["inputs"]["work_branch"] if c.get("number") == 31)
        mission_procedures.apply_listed(
            mid,
            dev,
            listing["listing"],
            inputs={"work_branch": {"choice": pick}},
            choices=offered["choices"],
            project=chosen,
            after=["o1"],
        )
        _apply(mid, "dev", after=["o2"], project=_pick(mid, finance.id))  # o3: Finance
        doc = missions.plan_revision(mid)["document"]
        named = [o["project_id"] for o in doc["objectives"]]
        assert named == [mail.id, files.id, finance.id]
        assert missions.get_mission(mid)["project_id"] in set(named)
        for oid, pid, repo in (
            ("o1", mail.id, "org/app"),
            ("o2", files.id, "org/files"),
            ("o3", finance.id, "org/finance"),
        ):
            rows = [o for o in missions.objectives(mid) if o["key"].startswith(f"{oid}--")]
            assert rows and {r["project_id"] for r in rows} == {pid}
            pr = next(r for r in rows if r["key"] == f"{oid}--pr_open")
            assert pr["probe_args"]["repo"] == repo

        assert _row(mid, "o2--pr_open")["probe_args"] == {
            "repo": "org/files",
            "branch": "feature/files-1",
            "number": 31,
        }

        def states(step: str) -> tuple[str, str, str]:
            s = mission_procedures.plan(mid)["step_states"]
            return s[f"o1.{step}"], s[f"o2.{step}"], s[f"o3.{step}"]

        assert states("plan") == ("ready", "waiting", "waiting")
        # Files' first step is met from its own evidence; the rest of Files still waits on Mail.
        missions.patch_objectives(mid, [{"op": "waive", "key": "o2--issue_ready"}])
        assert states("plan") == ("ready", "done", "waiting")
        assert states("implement") == ("waiting", "waiting", "waiting")
        _waive_objective(mid, "o1")
        assert states("implement") == ("done", "ready", "waiting")
        assert states("plan")[2] == "waiting"
        _waive_objective(mid, "o2")
        assert states("plan") == ("done", "done", "ready")
        _waive_objective(mid, "o3")
        assert set(mission_procedures.plan(mid)["objective_states"].values()) == {"done"}
    finally:
        missions.reset_schema_cache_for_test()
