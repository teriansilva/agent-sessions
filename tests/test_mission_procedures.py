"""Applying a procedure to a mission (#1777 S1b, #1613 P4 slice 1).

A REAL deployment drives these: a format-5 playbook is reviewed, bound and applied to a project
through the lifecycle, and the mission side reads what that recorded. Nothing is faked between
"what the operator reviewed" and "what the mission's probes target".
"""

from __future__ import annotations

import copy
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    mission_plan_document as plan_doc,
)
from agent_sessions import (
    mission_procedures,
    mission_questions,
    mission_supervisor,
    missions,
    projects,
)
from agent_sessions.main import create_app
from agent_sessions.playbooks import apply, deployment_state, lifecycle, review, store
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup
PB = "review-demo"

MANIFEST = """format = 5
[identity]
id = "review-demo"
name = "Procedure demo"
publisher = "Example"
version = "1.0.0"
domain = "development"
summary = "Two procedures for the mission tests."
[[variables]]
name = "repo"
required = true
[[variables]]
name = "work_branch"
required = true
[[materials]]
path = "RULES.md"
disposition = "managed"
template = true
"""

DEV = """format = 5
title = "Ship a change"
[[steps]]
id = "plan"
title = "Plan it"
actor = { kind = "agent" }
role = { skills = ["forgejo-pr-flow"], min_grade = "strong" }
[[steps.checklist]]
key = "issue_ready"
title = "The issue states the scope"
probe = "supervisor_judged"
[[steps]]
id = "implement"
title = "Implement"
actor = { kind = "agent" }
after = ["plan"]
outputs = ["head_branch"]
[[steps.checklist]]
key = "branch_ready"
title = "On its branch"
probe = "git_local"
probe_args = { branch = "{{work_branch}}" }
[[steps.checklist]]
key = "pr_open"
title = "A pull request is open"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{work_branch}}" }
[[steps]]
id = "review"
title = "Review"
actor = { kind = "agent" }
role = { skills = ["code-review"], min_grade = "frontier" }
after = ["implement"]
distinct_from = [{ step = "implement", constraint = "specialist" }]
rework = { to = "implement", when = "review_approved", max_rounds = 2 }
[[steps.checklist]]
key = "review_approved"
title = "Approved at the current head"
probe = "forge_review"
probe_args = { repo = "{{repo}}", branch = "{{steps.implement.head_branch}}" }
[[steps]]
id = "merge"
title = "Merge"
actor = { kind = "operator" }
after = ["review"]
[[steps.checklist]]
key = "merge_ok"
title = "Operator confirms the merge"
probe = "none"
"""

NOTES = """format = 5
title = "Write it down"
[[steps]]
id = "write"
title = "Write"
actor = { kind = "agent" }
[[steps.checklist]]
key = "written"
title = "A finding is written down"
probe = "supervisor_judged"
required = false
"""

BINDINGS = [
    {"name": "repo", "kind": "text", "value": "org/app"},
    {"name": "work_branch", "kind": "text", "value": "feature/x"},
]


def _files() -> dict:
    return {
        "playbook.toml": MANIFEST,
        "template/RULES.md": "Work on {{repo}}.\n",
        "flows/dev.toml": DEV,
        "flows/notes.toml": NOTES,
    }


def _write_bundle(contents: dict) -> str:
    tree = store.tree_from_files(contents)
    root = store.local_root() / PB
    for relative, data in tree.files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree.digest()


def _deploy(folder, tmp_path, monkeypatch, contents: dict, bindings=None) -> dict:
    """Review, bind and apply `contents` to a new project through the real lifecycle, confirming
    NO individual target (the default a review shows stays unarmed); a fresh missions store."""
    import shutil

    shutil.rmtree(store.local_root() / PB, ignore_errors=True)
    revision = _write_bundle(contents)
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    missions.reset_schema_cache_for_test()
    entity = projects.create("Demo", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        "destination": str(folder),
        "revision": revision,
        "bindings": BINDINGS if bindings is None else bindings,
        "project_id": entity.id,
    }
    plan = review.build(PB, inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, PB, inputs, receipt, "bind-op-10000", key=KEY)
    apply.apply(entity.id, "apply-op-10000", bound["receipt"], key=KEY)
    mid = missions.create_mission("ship it", project_id=entity.id, cwd=str(folder))["id"]
    return {"project": entity, "folder": folder, "mission": mid, "revision": revision}


@pytest.fixture
def deployed(setup, tmp_path, monkeypatch):
    """A project running the procedure demo, applied through the real lifecycle; a missions
    store of its own; and a mission in that project."""
    folder, _body = setup
    yield _deploy(folder, tmp_path, monkeypatch, _files())
    missions.reset_schema_cache_for_test()


HEALTH = """format = 5
title = "Check health"
[[steps]]
id = "check"
title = "Check"
actor = { kind = "operator" }
[[steps.checklist]]
key = "up"
title = "The service answers"
probe = "http_status"
probe_args = { url = "{{health_url}}" }
"""


@pytest.fixture
def deployed_with_default(setup, tmp_path, monkeypatch):
    """The demo plus a procedure whose probe URL comes from a BUNDLE DEFAULT the operator never
    confirmed in the deployment review (the independent review on #1790)."""
    folder, _body = setup
    contents = _files()
    contents["playbook.toml"] = MANIFEST.replace(
        "[[materials]]",
        '[[variables]]\nname = "health_url"\ntype = "url"\n'
        'default = "https://bundle-chosen.example/health"\n[[materials]]',
    )
    contents["flows/health.toml"] = HEALTH
    yield _deploy(folder, tmp_path, monkeypatch, contents)
    missions.reset_schema_cache_for_test()


def _apply(mid: str, flow_id: str = "dev") -> dict:
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == flow_id)
    return mission_procedures.apply_listed(mid, index, listing["listing"])


def _rows(mid: str) -> dict:
    return {o["key"]: o for o in missions.objectives(mid)}


def _counts(mid: str) -> tuple[int, int]:
    con = sqlite3.connect(missions._db_path())
    try:
        return (
            con.execute(
                "SELECT COUNT(*) FROM mission_objectives WHERE mission_id=?", (mid,)
            ).fetchone()[0],
            con.execute(
                "SELECT COUNT(*) FROM mission_plan_revisions WHERE mission_id=?", (mid,)
            ).fetchone()[0],
        )
    finally:
        con.close()


def _set_state(mid: str, state: str) -> None:
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET state=? WHERE id=?", (state, mid))
    con.commit()
    con.close()


# ---- the listing --------------------------------------------------------------------------------


def test_the_listing_is_the_deployed_playbooks_procedures_by_index(deployed):
    out = mission_procedures.listing(deployed["mission"])
    assert out["reason"] is None
    assert out["project_id"] == deployed["project"].id
    assert out["playbook"] == {"id": PB, "name": "Procedure demo", "revision": deployed["revision"]}
    assert [(p["index"], p["flow_id"], p["ready"]) for p in out["procedures"]] == [
        (0, "dev", True),
        (1, "notes", True),
    ]
    assert out["procedures"][0]["steps"] == 4
    assert out["listing"] == mission_procedures.listing(deployed["mission"])["listing"]


def test_a_mission_without_a_project_lists_nothing_and_says_why(deployed):
    mid = missions.create_mission("no project")["id"]
    out = mission_procedures.listing(mid)
    assert out["procedures"] == [] and "no project" in out["reason"]
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, out["listing"])
    assert e.value.status == 409


def test_a_project_without_a_deployment_lists_nothing_and_says_why(deployed, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    entity = projects.create("Bare", folders=[str(other)], default_folder=str(other))
    mid = missions.create_mission("x", project_id=entity.id, cwd=str(other))["id"]
    out = mission_procedures.listing(mid)
    assert out["procedures"] == [] and out["reason"] == "this project has no deployed playbook"


def _edit_record(pid: str, fn) -> None:
    with deployment_state.locked(pid) as locked:
        rec = locked.read()
        fn(rec)
        locked.write(rec)


@pytest.mark.parametrize(
    ("break_it", "reason"),
    [
        (lambda rec: rec.pop("review_facts"), "no recorded review"),
        (
            lambda rec: rec.__setitem__(
                "apply_operation", {**rec["apply_operation"], "state": "intent"}
            ),
            "not settled",
        ),
    ],
    ids=["no-review-facts", "unsettled"],
)
def test_an_unusable_deployment_fails_closed_with_its_reason(deployed, break_it, reason):
    _edit_record(deployed["project"].id, break_it)
    out = mission_procedures.listing(deployed["mission"])
    assert out["procedures"] == [] and reason in out["reason"]


def test_a_playbook_changed_since_deployment_is_never_silently_remapped(deployed):
    contents = _files()
    contents["flows/notes.toml"] = NOTES.replace("Write it down", "Write it all down")
    _write_bundle(contents)
    out = mission_procedures.listing(deployed["mission"])
    assert out["procedures"] == [] and "update the deployment first" in out["reason"]


def test_an_unreadable_deployment_record_is_a_reason_not_no_playbook(deployed, monkeypatch):
    def boom(_pid):
        raise store.StoreError("the deployment record is damaged", status=409)

    monkeypatch.setattr(deployment_state, "active", boom)
    out = mission_procedures.listing(deployed["mission"])
    assert "cannot be read" in out["reason"]


# ---- applying a deployed procedure --------------------------------------------------------------


def test_applying_writes_one_revision_and_the_evidence_together(deployed):
    mid, pid = deployed["mission"], deployed["project"].id
    assert _apply(mid) == {"revision": 1, "objective": "o1"}
    rows = _rows(mid)
    assert list(rows) == [
        "o1--issue_ready",
        "o1--branch_ready",
        "o1--pr_open",
        "o1--review_approved",
        "o1--merge_ok",
    ]
    assert {r["source"] for r in rows.values()} == {"playbook"}
    assert {r["project_id"] for r in rows.values()} == {pid}
    assert rows["o1--pr_open"]["plan_step"] == "o1.implement"
    assert rows["o1--pr_open"]["probe"] == "forge_pr"
    assert rows["o1--pr_open"]["probe_args"] == {"repo": "org/app", "branch": "feature/x"}
    assert rows["o1--issue_ready"]["probe"] == "supervisor_judged"
    assert rows["o1--issue_ready"]["probe_args"] is None
    assert all(r["gate"] for r in rows.values())
    rev = missions.plan_revision(mid)
    assert rev["revision"] == 1 and rev["author_kind"] == "playbook"
    assert rev["reason"] == f"applied {PB}@{deployed['revision'][:12]} · dev"
    (o1,) = rev["document"]["objectives"]
    assert o1["project_id"] == pid and o1["procedure"]["source"] == "playbook"
    assert o1["procedure"]["revision"] == deployed["revision"]
    assert o1["inputs"] == [
        {"name": "repo", "source": "deployment"},
        {"name": "work_branch", "source": "deployment", "value": "feature/x"},
    ]
    review_step = next(s for s in rev["document"]["steps"] if s["id"] == "o1.review")
    assert review_step["role"] == {"skills": ["code-review"], "min_grade": "frontier"}
    assert review_step["rework"]["to"] == "o1.implement"


def test_waiting_evidence_is_a_required_none_gate_carrying_its_real_probe(deployed):
    mid = deployed["mission"]
    _apply(mid)
    row = _rows(mid)["o1--review_approved"]
    assert (row["probe"], row["probe_args"], row["gate"], row["state"]) == (
        "none",
        None,
        True,
        "pending",
    )
    assert row["pending"] == {
        "probe": "forge_review",
        "args": {"repo": "org/app"},
        "refs": {"branch": "{{steps.implement.head_branch}}"},
        "gate": True,
    }


def test_the_probe_runner_never_probes_a_waiting_row(deployed):
    from agent_sessions import mission_probes

    assert "none" not in mission_probes.MECHANICAL


def test_targets_come_from_the_deployments_review_not_the_current_bindings(deployed):
    """D-P4-2: the deployment recorded what the operator reviewed; a later change of the project's
    binding (simulated by the recorded fact diverging from it) never reaches a mission."""

    def reviewed_elsewhere(rec):
        for t in rec["review_facts"]["targets"]:
            if t["id"] == "flow:dev:implement:pr_open":
                t["args"]["repo"] = "org/reviewed"

    _edit_record(deployed["project"].id, reviewed_elsewhere)
    _apply(deployed["mission"])
    assert _rows(deployed["mission"])["o1--pr_open"]["probe_args"]["repo"] == "org/reviewed"


def test_an_unconfirmed_bundle_default_never_becomes_a_missions_target(deployed_with_default):
    """The review showed the default-derived target as needing confirmation; nothing was
    confirmed, the deployment still applied. The procedure is listed NOT ready, and applying it
    is refused and writes nothing — the bundle's chosen URL never becomes a probe target."""
    d = deployed_with_default
    out = mission_procedures.listing(d["mission"])
    health = next(p for p in out["procedures"] if p["flow_id"] == "health")
    assert health["ready"] is False and "playbook default (health_url)" in health["reason"]
    assert next(p for p in out["procedures"] if p["flow_id"] == "dev")["ready"] is True
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(d["mission"], health["index"], out["listing"])
    assert e.value.status == 409
    assert _counts(d["mission"]) == (0, 0)


def test_a_waiting_target_from_a_bundle_default_is_refused_too(deployed):
    def defaulted_pending(rec):
        for t in rec["review_facts"]["targets"]:
            if t["id"] == "flow:dev:review:review_approved":
                t["default_variables"] = ["repo"]

    _edit_record(deployed["project"].id, defaulted_pending)
    dev = mission_procedures.listing(deployed["mission"])["procedures"][0]
    assert dev["ready"] is False and "playbook default (repo)" in dev["reason"]


def test_an_operator_key_in_the_minted_namespace_is_refused(deployed):
    mid = deployed["mission"]
    for key in ("o1--issue_ready", "o12--x"):
        with pytest.raises(missions.MissionError) as e:
            missions.patch_objectives(mid, [{"op": "add", "key": key, "title": "t"}])
        assert e.value.status == 422 and "reserved" in str(e.value)
    missions.patch_objectives(mid, [{"op": "add", "key": "o1-mine", "title": "t"}])
    assert _apply(mid)["objective"] == "o1"


def test_an_unusable_binding_is_a_409_not_a_500(deployed, monkeypatch):
    from agent_sessions import template_vars

    def unusable(*_a, **_k):
        raise template_vars.BindingUnusable("token", "needs re-entry")

    monkeypatch.setattr(mission_procedures.binding, "resolve", unusable)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(deployed["mission"], "check-branch", copy.deepcopy(LOCAL))
    assert e.value.status == 409


def test_the_listing_digest_binds_the_reviewed_targets(deployed):
    """Hermes on #1790 (1): the same revision re-applied with other targets is a DIFFERENT list,
    so an old `{index, listing}` never applies a target the operator did not choose from."""
    mid = deployed["mission"]
    before = mission_procedures.listing(mid)

    def retarget(rec):
        for t in rec["review_facts"]["targets"]:
            if t["id"] == "flow:dev:implement:pr_open":
                t["args"]["repo"] = "org/other"

    _edit_record(deployed["project"].id, retarget)
    after = mission_procedures.listing(mid)
    assert after["listing"] != before["listing"]
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, before["listing"])
    assert e.value.status == 409
    assert _counts(mid) == (0, 0)


def test_the_rows_come_from_the_same_snapshot_the_digest_was_built_from(deployed, monkeypatch):
    """No second read of the deployment between the digest compare and the rows."""
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    calls = []
    real = mission_procedures._deployment

    def counted(pid):
        calls.append(pid)
        return real(pid)

    monkeypatch.setattr(mission_procedures, "_deployment", counted)
    mission_procedures.apply_listed(mid, 0, listing["listing"])
    assert len(calls) == 1


def test_a_dispatch_proposal_cannot_move_the_objectives_to_another_project(deployed, tmp_path):
    """Hermes on #1790 (2): the plan editor and the launch both refuse a project the plan's
    objectives do not name."""
    mid = deployed["mission"]
    _apply(mid)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(missions.MissionError) as e:
        missions.put_plan(
            mid, project_id="p-ffffffffffff", cwd=str(other), engine="claude", brief="go"
        )
    assert e.value.status == 409 and "objectives name project" in str(e.value)
    plan = missions.put_plan(
        mid,
        project_id=deployed["project"].id,
        cwd=str(deployed["folder"]),
        engine="claude",
        brief="go",
    )
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"], project_id="p-ffffffffffff", cwd=str(other))
    assert e.value.status == 409 and "objectives name project" in str(e.value)
    assert missions.get_mission(mid)["state"] == "planned"


def test_an_unreadable_deployment_refuses_a_mission_local_procedure(deployed, monkeypatch):
    """Hermes on #1790 (3): a failed read is not an absent deployment."""

    def boom(_pid):
        raise store.StoreError("the deployment record is damaged", status=409)

    monkeypatch.setattr(deployment_state, "active", boom)
    note = {
        "format": 5,
        "title": "n",
        "steps": [{"id": "s", "title": "s", "actor": {"kind": "none"}}],
    }
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(deployed["mission"], "note", note)
    assert e.value.status == 409 and "cannot be read" in str(e.value)
    assert _counts(deployed["mission"]) == (0, 0)


def test_an_optional_waiting_item_still_gates_while_it_waits(setup, tmp_path, monkeypatch):
    """Hermes on #1790 (4): a row no runner can probe never lets a mission settle around it, even
    when its item is optional; the item's own `required` waits in `pending.gate`. Through the
    real apply and the completion predicate."""
    folder, _body = setup
    contents = _files()
    contents["flows/dev.toml"] = DEV.replace(
        'key = "review_approved"\ntitle = "Approved at the current head"',
        'key = "review_approved"\ntitle = "Approved at the current head"\nrequired = false',
    )
    assert contents["flows/dev.toml"] != DEV
    d = _deploy(folder, tmp_path, monkeypatch, contents)
    mid = d["mission"]
    _apply(mid)
    row = _rows(mid)["o1--review_approved"]
    assert row["gate"] is True and row["pending"]["gate"] is False
    _set_state(mid, "running")
    _settle_all_but_the_waiting_row(mid)
    rows = missions.objectives(mid)
    assert missions.gate_tally(rows)[1] == 1
    assert missions.gates_settled(rows) is False
    missions.reset_schema_cache_for_test()


def test_a_procedure_the_review_does_not_cover_cannot_be_applied(deployed):
    _edit_record(
        deployed["project"].id,
        lambda rec: rec["review_facts"].update(
            targets=[
                t for t in rec["review_facts"]["targets"] if "flow:dev:" not in t.get("id", "")
            ]
        ),
    )
    out = mission_procedures.listing(deployed["mission"])
    dev = out["procedures"][0]
    assert dev["ready"] is False and "does not cover" in dev["reason"]
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(deployed["mission"], 0, out["listing"])
    assert e.value.status == 409
    assert _counts(deployed["mission"]) == (0, 0)


def test_a_stale_listing_or_a_bad_index_writes_nothing(deployed):
    mid = deployed["mission"]
    out = mission_procedures.listing(mid)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, "0" * 32)
    assert e.value.status == 409 and "changed" in str(e.value)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 2, out["listing"])
    assert e.value.status == 422
    for bad in (-1, True, "0", 0.0):
        with pytest.raises(missions.MissionError) as e:
            mission_procedures.apply_listed(mid, bad, out["listing"])
        assert e.value.status == 422
    assert _counts(mid) == (0, 0)


def test_a_duplicate_apply_is_refused_and_a_second_procedure_appends(deployed):
    mid = deployed["mission"]
    _apply(mid)
    before = _counts(mid)
    with pytest.raises(missions.MissionError) as e:
        _apply(mid)
    assert e.value.status == 409 and "already applies" in str(e.value)
    assert _counts(mid) == before
    assert _apply(mid, "notes") == {"revision": 2, "objective": "o2"}
    doc = missions.plan_revision(mid)["document"]
    assert [o["id"] for o in doc["objectives"]] == ["o1", "o2"]
    first = missions.plan_revision(mid, 1)["document"]
    assert doc["objectives"][0] == first["objectives"][0]  # the earlier objective is unchanged
    reasons = {s["objective"]: s["envelope"]["reason"] for s in doc["steps"]}
    assert reasons["o1"].endswith("· dev") and reasons["o2"].endswith("· notes")
    assert _rows(mid)["o2--written"]["gate"] is False


@pytest.mark.parametrize("state", ["dispatching", "running", "review", "done", "abandoned"])
def test_a_procedure_is_applied_only_before_dispatch(deployed, state):
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    _set_state(mid, state)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, listing["listing"])
    assert e.value.status == 409
    assert _counts(mid) == (0, 0)


def test_a_planned_mission_takes_a_procedure(deployed):
    _set_state(deployed["mission"], "planned")
    assert _apply(deployed["mission"])["revision"] == 1


def test_apply_racing_dispatch_one_wins_and_nothing_is_partial(deployed, monkeypatch):
    """The two serialize on `BEGIN IMMEDIATE`. Dispatch is simulated as its state write landing
    while the apply is between its listing read and its transaction."""
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    real = missions.apply_procedure
    gate = threading.Event()

    def racing(*a, **kw):
        _set_state(mid, "dispatching")  # dispatch won the race
        gate.set()
        return real(*a, **kw)

    monkeypatch.setattr(missions, "apply_procedure", racing)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, listing["listing"])
    assert gate.is_set() and e.value.status == 409
    assert _counts(mid) == (0, 0)


def test_two_concurrent_applies_of_different_procedures_never_share_an_objective_id(deployed):
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    results, errors = [], []

    def go(index):
        try:
            results.append(mission_procedures.apply_listed(mid, index, listing["listing"]))
        except missions.MissionError as e:  # pragma: no cover — must not happen
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(r["objective"] for r in results) == ["o1", "o2"]
    assert sorted(r["revision"] for r in results) == [1, 2]
    assert [o["id"] for o in missions.plan_revision(mid)["document"]["objectives"]] == ["o1", "o2"]


def test_a_mission_moved_to_another_project_refuses_the_old_listing(deployed, tmp_path):
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET project_id='p-ffffffffffff' WHERE id=?", (mid,))
    con.commit()
    con.close()
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, 0, listing["listing"])
    assert e.value.status == 409


# ---- the mixed-gate regression: waiting evidence keeps the mission open -------------------------


def _settle_all_but_the_waiting_row(mid: str) -> None:
    rows = _rows(mid)
    met = "o1--branch_ready"
    missions.observe_objective(mid, met, observed=True, value=True, detail="on its branch")
    others = [k for k in rows if k not in (met, "o1--review_approved")]
    missions.patch_objectives(mid, [{"op": "waive", "key": k} for k in others])


def test_a_waiting_row_keeps_a_mission_with_a_met_gate_unsettled(deployed):
    mid = deployed["mission"]
    _apply(mid)
    _set_state(mid, "running")
    _settle_all_but_the_waiting_row(mid)
    rows = missions.objectives(mid)
    by_key = {r["key"]: r for r in rows}
    assert by_key["o1--branch_ready"]["state"] == "met"
    assert missions.gate_tally(rows) == (5, 1)
    assert missions.gates_settled(rows) is False
    assert mission_supervisor.assess(mid)["likely_done"] is False
    assert (
        missions.propose_completion(mid, from_state="running", render=lambda *a, **k: "done")
        is False
    )
    assert "close_mission" not in [a for a, _ in mission_questions.offered_actions_for(rows)]
    con = missions._ready()
    try:
        con.execute("BEGIN IMMEDIATE")
        what, did = missions._apply_answer_con(con, mid, "close_mission", "", 1.0)
        con.execute("ROLLBACK")
    finally:
        con.close()
    assert did is False
    states = mission_procedures.plan(mid)["step_states"]
    assert states["o1.review"] != "done"
    assert missions.get_mission(mid)["state"] == "running"

    # …and once the operator waives it, the same mission IS settled.
    missions.patch_objectives(mid, [{"op": "waive", "key": "o1--review_approved"}])
    rows = missions.objectives(mid)
    assert missions.gates_settled(rows) is True
    assert mission_procedures.plan(mid)["step_states"]["o1.review"] == "done"


# ---- provenance ---------------------------------------------------------------------------------


def test_no_operator_patch_can_claim_a_step_project_or_pending_probe(deployed):
    mid = deployed["mission"]
    for field, value in (
        ("plan_step", "o1.review"),
        ("project_id", "p-ffffffffffff"),
        ("pending", {"probe": "http_status", "args": {"url": "https://evil.example"}}),
    ):
        with pytest.raises(missions.MissionError) as e:
            missions.patch_objectives(
                mid, [{"op": "add", "key": "mine", "title": "mine", field: value}]
            )
        assert e.value.status == 422 and field in str(e.value)
    missions.patch_objectives(mid, [{"op": "add", "key": "mine", "title": "mine"}])
    row = _rows(mid)["mine"]
    assert row["plan_step"] is None and row["pending"] is None
    assert row["project_id"] == deployed["project"].id  # the dual read, not the PATCH


@pytest.mark.parametrize("source", ["model", "instruction"])
def test_a_model_shaped_row_never_acquires_a_probe_or_target(deployed, source):
    mid = deployed["mission"]
    with pytest.raises(missions.MissionError):
        missions.instantiate_objectives(
            mid,
            [
                {
                    "key": "x",
                    "title": "x",
                    "probe": "forge_pr",
                    "probe_args": {"repo": "org/app", "branch": "b"},
                    "source": source,
                }
            ],
        )
    assert "x" not in _rows(mid)


def test_the_store_fixes_row_provenance_by_origin_not_by_the_caller(deployed):
    with pytest.raises(missions.MissionError) as e:
        missions.apply_procedure(
            deployed["mission"],
            project_id=deployed["project"].id,
            procedure={"source": "hq"},
            inputs=[],
            items=[],
            reason="r",
        )
    assert e.value.status == 500


# ---- mission-local procedures -------------------------------------------------------------------

LOCAL = {
    "format": 5,
    "title": "Check the branch",
    "steps": [
        {
            "id": "check",
            "title": "Check",
            "actor": {"kind": "agent"},
            "checklist": [
                {
                    "key": "on_branch",
                    "title": "On the branch",
                    "probe": "git_local",
                    "probe_args": {"branch": "{{work_branch}}"},
                }
            ],
        }
    ],
}


def test_a_mission_local_procedure_is_applied_with_operator_provenance(deployed):
    mid = deployed["mission"]
    out = mission_procedures.apply_local(mid, "check-branch", copy.deepcopy(LOCAL))
    assert out == {"revision": 1, "objective": "o1"}
    row = _rows(mid)["o1--on_branch"]
    assert row["source"] == "operator" and row["probe_args"] == {"branch": "feature/x"}
    rev = missions.plan_revision(mid)
    assert rev["author_kind"] == "operator"
    assert rev["document"]["objectives"][0]["procedure"]["source"] == "mission_local"
    assert rev["document"]["objectives"][0]["inputs"] == [
        {"name": "work_branch", "source": "project"}
    ]


def test_a_mission_local_procedure_is_held_to_the_flow_validator(deployed):
    bad = copy.deepcopy(LOCAL)
    bad["steps"][0]["checklist"][0]["probe_args"] = {"branch": "main"}  # a literal target
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(deployed["mission"], "check-branch", bad)
    assert e.value.status == 422 and "literal" in str(e.value)
    assert _counts(deployed["mission"]) == (0, 0)


def test_a_mission_local_target_from_a_playbook_default_is_refused(deployed, monkeypatch):
    real = mission_procedures.binding.targets

    def defaulted(bundle, resolved):
        out = real(bundle, resolved)
        for t in out:
            t["default_variables"] = ["work_branch"]
        return out

    monkeypatch.setattr(mission_procedures.binding, "targets", defaulted)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(deployed["mission"], "check-branch", copy.deepcopy(LOCAL))
    assert e.value.status == 409 and "bind a project value for work_branch" in str(e.value)
    assert _counts(deployed["mission"]) == (0, 0)


def test_a_mission_local_procedure_in_a_project_without_a_playbook_has_no_variables(
    deployed, tmp_path
):
    other = tmp_path / "bare"
    other.mkdir()
    entity = projects.create("Bare", folders=[str(other)], default_folder=str(other))
    mid = missions.create_mission("x", project_id=entity.id, cwd=str(other))["id"]
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_local(mid, "check-branch", copy.deepcopy(LOCAL))
    assert e.value.status == 422 and "undeclared" in str(e.value)
    judged = copy.deepcopy(LOCAL)
    judged["steps"][0]["checklist"][0] = {
        "key": "done",
        "title": "Done",
        "probe": "supervisor_judged",
    }
    assert mission_procedures.apply_local(mid, "judged", judged)["revision"] == 1


# ---- the plan read and its derived step states --------------------------------------------------


def test_step_states_are_derived_one_per_rule(deployed):
    mid = deployed["mission"]
    _apply(mid)
    states = mission_procedures.plan(mid)["step_states"]
    assert states == {
        "o1.plan": "ready",
        "o1.implement": "waiting",
        "o1.review": "waiting",
        "o1.merge": "waiting",
    }
    missions.patch_objectives(mid, [{"op": "waive", "key": "o1--issue_ready"}])
    states = mission_procedures.plan(mid)["step_states"]
    assert states["o1.plan"] == "done" and states["o1.implement"] == "ready"
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE mission_objectives SET state='failed' WHERE key='o1--pr_open'")
    con.commit()
    con.close()
    assert mission_procedures.plan(mid)["step_states"]["o1.implement"] == "failed"


def test_a_step_without_evidence_is_a_note():
    from agent_sessions.playbooks import validate

    authored = {
        "format": 5,
        "title": "t",
        "steps": [{"id": "think", "title": "Think", "actor": {"kind": "none"}}],
    }
    flow = validate.flow_document(authored, "f", [])
    o = {
        "id": "o1",
        "project_id": "p-x",
        "procedure": {
            "source": "mission_local",
            "flow_id": "f",
            "title": "t",
            "flow": flow,
            "digest": plan_doc.flow_digest(flow),
            "document": authored,
            "variables": [],
        },
        "inputs": [],
    }
    doc = plan_doc.build([o], {"o1": "r"})
    assert plan_doc.step_states(doc, []) == {"o1.think": "note"}


def test_a_dropped_evidence_row_counts_as_the_operator_not_requiring_it(deployed):
    mid = deployed["mission"]
    _apply(mid)
    missions.patch_objectives(mid, [{"op": "drop", "key": "o1--issue_ready"}])
    assert mission_procedures.plan(mid)["step_states"]["o1.plan"] == "done"


def test_the_plan_read_without_a_plan_and_for_unknown_revisions(deployed):
    mid = deployed["mission"]
    assert mission_procedures.plan(mid) == {"mission_id": mid, "plan": None}
    _apply(mid)
    _apply(mid, "notes")
    first = mission_procedures.plan(mid, 1)
    assert first["plan"]["revision"] == 1 and first["is_latest"] is False
    assert mission_procedures.plan(mid)["is_latest"] is True
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.plan(mid, 7)
    assert e.value.status == 404


# ---- dispatch is untouched ----------------------------------------------------------------------


def test_the_dispatch_path_reads_neither_the_plan_nor_pending():
    """Dispatch is unchanged in this slice (#1777): nothing types a step brief into a session,
    and nothing launches on the plan document's say-so."""
    from pathlib import Path

    from agent_sessions import mission_dispatch, mission_dispatch_recover, mission_plan

    for mod in (mission_dispatch, mission_dispatch_recover, mission_plan):
        text = Path(mod.__file__).read_text()
        for needle in (
            "mission_plan_revisions",
            "plan_revision",
            "mission_procedures",
            "mission_plan_document",
            "plan_step",
            '["pending"]',
            '.get("pending")',
        ):
            assert needle not in text, f"{mod.__name__} reads {needle}"


# ---- the routes ---------------------------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg) -> str:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_the_routes_need_login_and_csrf_and_404_unknown_missions(auth_cfg, deployed):
    mid = deployed["mission"]
    c = _client(auth_cfg)
    assert c.get(f"/api/missions/{mid}/procedures").status_code == 401
    assert c.get(f"/api/missions/{mid}/plan/revisions/latest").status_code == 401
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    listing = c.get(f"/api/missions/{mid}/procedures")
    assert listing.status_code == 200
    assert listing.headers.get("cache-control") == "no-store"
    body = {"index": 0, "listing": listing.json()["listing"]}
    assert c.post(f"/api/missions/{mid}/procedure", json=body).status_code in (401, 403)
    r = c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr)
    assert r.status_code == 200 and r.json() == {"revision": 1, "objective": "o1"}
    plan = c.get(f"/api/missions/{mid}/plan/revisions/latest").json()
    assert plan["plan"]["revision"] == 1 and plan["step_states"]["o1.plan"] == "ready"
    assert c.get(f"/api/missions/{mid}/plan/revisions/1").status_code == 200
    assert c.get(f"/api/missions/{mid}/plan/revisions/2").status_code == 404
    assert c.get(f"/api/missions/{mid}/plan/revisions/x").status_code == 422
    unknown = "msn_" + "0" * 32
    assert c.get(f"/api/missions/{unknown}/procedures").status_code == 404
    assert c.get(f"/api/missions/{unknown}/plan/revisions/latest").status_code == 404
    assert c.post(f"/api/missions/{unknown}/procedure", json=body, headers=hdr).status_code == 404
    # A foreign mission's plan is not this one's: another mission reads its own (none).
    other = missions.create_mission("other")["id"]
    assert c.get(f"/api/missions/{other}/plan/revisions/latest").json()["plan"] is None


def test_the_apply_route_takes_exactly_one_shape(auth_cfg, deployed):
    mid = deployed["mission"]
    c = _client(auth_cfg)
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    for body in (
        {},
        {"index": 0},
        {"index": 0, "listing": "x", "extra": 1},
        {"mission_local": {"flow_id": "a", "flow": {}}, "index": 0},
        {"mission_local": {"flow": {}}},
    ):
        assert c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr).status_code == 422
    big = {"mission_local": {"flow_id": "a", "flow": {"x": "y" * (300 * 1024)}}}
    assert c.post(f"/api/missions/{mid}/procedure", json=big, headers=hdr).status_code == 413
    ok = {"mission_local": {"flow_id": "check-branch", "flow": LOCAL}}
    assert c.post(f"/api/missions/{mid}/procedure", json=ok, headers=hdr).status_code == 200
    assert _counts(mid) == (1, 1)


def _direct_apply(mid: str, project_id: str) -> dict:
    """The store's own write, called past the procedure layer's checks: its fences must hold on
    their own, because the listing is read before (and outside) the transaction."""
    listing = mission_procedures.listing(mid)
    record, bundle, _reason = mission_procedures._deployment(listing["project_id"])
    flow = bundle["flows"]["notes"]
    items = mission_procedures._reviewed_items("notes", flow, record["review_facts"])
    return missions.apply_procedure(
        mid,
        project_id=project_id,
        procedure={
            "source": "playbook",
            "playbook_id": PB,
            "revision": listing["playbook"]["revision"],
            "flow_id": "notes",
            "title": flow["title"],
            "flow": flow,
            "digest": plan_doc.flow_digest(flow),
            "document": bundle["_documents"]["notes"],
            "variables": bundle["variables"],
            "manifest_format": bundle["format"],
        },
        inputs=[],
        items=items,
        reason="r",
    )


def test_the_store_refuses_a_mission_that_lost_its_project_inside_the_window(deployed):
    mid, pid = deployed["mission"], deployed["project"].id
    with pytest.raises(missions.MissionError) as e:
        _direct_apply_after(mid, pid)
    assert e.value.status == 409 and "no project" in str(e.value)
    assert _counts(mid) == (0, 0)


def _direct_apply_after(mid: str, pid: str) -> dict:
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET project_id=? WHERE id=?", (pid, mid))
    con.commit()
    con.close()
    listing = mission_procedures.listing(mid)
    record, bundle, _reason = mission_procedures._deployment(pid)
    flow = bundle["flows"]["notes"]
    items = mission_procedures._reviewed_items("notes", flow, record["review_facts"])
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET project_id=NULL WHERE id=?", (mid,))
    con.commit()
    con.close()
    return missions.apply_procedure(
        mid,
        project_id=pid,
        procedure={
            "source": "playbook",
            "playbook_id": PB,
            "revision": listing["playbook"]["revision"],
            "flow_id": "notes",
            "title": flow["title"],
            "flow": flow,
            "digest": plan_doc.flow_digest(flow),
            "document": bundle["_documents"]["notes"],
            "variables": bundle["variables"],
            "manifest_format": bundle["format"],
        },
        inputs=[],
        items=items,
        reason="r",
    )


def test_the_store_refuses_a_project_other_than_the_missions(deployed):
    mid = deployed["mission"]
    with pytest.raises(missions.MissionError) as e:
        _direct_apply(mid, "p-ffffffffffff")
    assert e.value.status == 409 and "project changed" in str(e.value)
    assert _counts(mid) == (0, 0)
    assert _direct_apply(mid, deployed["project"].id)["revision"] == 1


def test_a_reviewed_target_for_a_different_probe_kind_is_not_this_procedures(deployed):
    def other_kind(rec):
        for t in rec["review_facts"]["targets"]:
            if t["id"] == "flow:dev:implement:pr_open":
                t["probe"] = "forge_checks"

    _edit_record(deployed["project"].id, other_kind)
    dev = mission_procedures.listing(deployed["mission"])["procedures"][0]
    assert dev["ready"] is False and "does not cover" in dev["reason"]
