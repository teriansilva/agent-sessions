"""Fleet listing and the combined update review: batch only what needs no decision of its own."""

import copy

import pytest

from agent_sessions import projects
from agent_sessions.playbooks import apply, deployment_state, fleet, lifecycle, review, store
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup
BINDINGS = [
    {"name": "endpoint", "kind": "text", "value": "https://ops.example/health"},
    {"name": "status", "kind": "text", "value": "200"},
]


def _write(contents):
    tree = store.tree_from_files(contents)
    root = store.local_root() / "review-demo"
    for relative, data in tree.files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree.digest()


@pytest.fixture
def fleet_of_two(setup, tmp_path):
    first, body = setup
    second = tmp_path / "second"
    second.mkdir()
    deployed = []
    for i, folder in enumerate((first, second), 1):
        entity = projects.create(f"Fleet {i}", folders=[str(folder)], default_folder=str(folder))
        inputs = {
            **body,
            "destination": str(folder),
            "bindings": BINDINGS,
            "project_id": entity.id,
        }
        plan = review.build("review-demo", inputs, key=KEY)
        assert not any(t["requires_confirmation"] for t in plan.public["targets"])
        receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
        bound = lifecycle.bind(
            entity.id, "review-demo", inputs, receipt, f"bind-op-{i}0000", key=KEY
        )
        apply.apply(entity.id, f"apply-op-{i}0000", bound["receipt"], key=KEY)
        deployed.append((entity, folder))
    return deployed


def _new_revision(mutate=None):
    contents = files()
    contents["template/RULES.md"] = "Check {{endpoint}} carefully.\n"
    if mutate:
        mutate(contents)
    return _write(contents)


def test_the_listing_shows_every_project_and_whether_an_update_is_available(fleet_of_two):
    listing = fleet.projects("review-demo")
    assert {p["project_id"] for p in listing["projects"]} == {e.id for e, _ in fleet_of_two}
    assert not any(p["update_available"] for p in listing["projects"])
    revision = _new_revision()
    listing = fleet.projects("review-demo")
    assert listing["revision"] == revision
    assert all(p["update_available"] and p["state"] == "applied" for p in listing["projects"])


def test_a_text_only_update_batches_every_project_under_one_digest(fleet_of_two):
    _new_revision()
    combined = fleet.plan("review-demo", key=KEY)
    assert [p["batchable"] for p in combined["projects"]] == [True, True]
    for row in combined["projects"]:
        assert row["reasons"] == []
        [change] = row["changes"]  # the operator sees exactly what will be written
        assert change["path"] == "RULES.md" and change["action"] == "replace"
        assert "-Check https://ops.example/health." in change["diff"]
        assert "+Check https://ops.example/health carefully." in change["diff"]
    assert combined["digest"] == fleet.plan("review-demo", key=KEY)["digest"]  # deterministic


def test_an_operator_conflict_sends_only_that_project_to_its_own_review(fleet_of_two):
    _new_revision()
    (entity, folder), _ = fleet_of_two
    (folder / "RULES.md").write_text("my own edit\n")
    rows = {p["project_id"]: p for p in fleet.plan("review-demo", key=KEY)["projects"]}
    assert rows[entity.id]["batchable"] is False
    assert "the update has material conflicts" in rows[entity.id]["reasons"]
    assert sum(p["batchable"] for p in rows.values()) == 1


def test_a_refused_update_review_is_a_reason_not_an_error(fleet_of_two):
    def add_required(contents):
        contents["playbook.toml"] += '\n[[variables]]\nname = "region"\ntype = "text"\n'
        contents["template/RULES.md"] = "Check {{endpoint}} in {{region}}.\n"

    _new_revision(add_required)
    combined = fleet.plan("review-demo", key=KEY)
    assert not any(p["batchable"] for p in combined["projects"])
    assert all("refuses" in p["reasons"][0] for p in combined["projects"])


def test_an_interrupted_or_baseline_free_deployment_is_never_batched(fleet_of_two):
    (first, _), (second, _) = fleet_of_two
    with deployment_state.locked(first.id) as locked:
        rec = locked.read()
        rec.pop("review_facts")  # applied before baselines were recorded
        locked.write(rec)
    with deployment_state.locked(second.id) as locked:
        rec = locked.read()
        rec["apply_operation"] = {**rec["apply_operation"], "state": "intent"}
        locked.write(rec)
    _new_revision()
    rows = {p["project_id"]: p for p in fleet.plan("review-demo", key=KEY)["projects"]}
    assert "no recorded review baseline" in rows[first.id]["reasons"][0]
    assert rows[second.id]["reasons"] == ["the deployment is interrupted"]


BASE = {
    "conflicts": [],
    "targets": [{"id": "connection:service", "url": "https://a/", "requires_confirmation": False}],
    "capability_requests": ["shared_memory"],
    "assignments": [{"step": "s", "assignment": {"engine": "e", "model": "m"}}],
    "variables": [{"name": "endpoint"}],
}


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda p: p["conflicts"].append({"path": "x"}), "material conflicts"),
        (
            lambda p: p["targets"][0].update(requires_confirmation=True),
            "needs individual confirmation",
        ),
        (lambda p: p["targets"][0].update(url="https://b/"), "new or changed"),
        (
            lambda p: p["targets"].append(
                {"id": "connection:x", "url": "https://c/", "requires_confirmation": False}
            ),
            "new or changed",
        ),
        (lambda p: p["capability_requests"].append("unattended_start"), "new capability"),
        (lambda p: p["assignments"][0]["assignment"].update(model="m2"), "assignment changed"),
        (lambda p: p["variables"].append({"name": "region"}), "adds a variable"),
    ],
)
def test_each_disqualifying_condition_on_its_own(change, reason):
    baseline = apply.review_facts(BASE)
    assert fleet._reasons(baseline, copy.deepcopy(BASE)) == []
    public = copy.deepcopy(BASE)
    change(public)
    assert any(reason in r for r in fleet._reasons(baseline, public))


def test_a_dropped_capability_or_variable_does_not_disqualify():
    baseline = apply.review_facts(BASE)
    public = copy.deepcopy(BASE)
    public["capability_requests"] = []
    public["variables"] = []
    assert fleet._reasons(baseline, public) == []


def test_the_fleet_routes_need_a_session_and_the_review_needs_csrf_and_origin(
    fleet_of_two, auth_cfg
):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    assert c.get("/api/playbooks/review-demo/projects").status_code == 401
    assert c.post("/api/playbooks/review-demo/fleet/review", json={}).status_code == 401
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    listing = c.get("/api/playbooks/review-demo/projects")
    assert listing.status_code == 200 and len(listing.json()["projects"]) == 2
    assert listing.headers["cache-control"] == "no-store"
    assert c.post("/api/playbooks/review-demo/fleet/review", json={}).status_code == 403
    _new_revision()
    combined = c.post("/api/playbooks/review-demo/fleet/review", json={}, headers=hdr)
    assert combined.status_code == 200 and combined.json()["digest"]
    assert [p["batchable"] for p in combined.json()["projects"]] == [True, True]
