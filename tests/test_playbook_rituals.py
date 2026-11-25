"""Rituals are recorded under a stable identity, declared and never scheduled (#1201 pending)."""

import pytest

from agent_sessions import projects
from agent_sessions.playbooks import apply, lifecycle, remove, review, store
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup

RUNBOOK = """+++
title = "Nightly check"
trigger = "ritual"
bail = ["the endpoint is down twice in a row"]
+++
Check the endpoint and report.
"""


@pytest.fixture
def ritual_demo(setup):
    folder, body = setup
    contents = files()
    contents["playbook.toml"] = (
        contents["playbook.toml"].replace('id = "review-demo"', 'id = "ritual-demo"')
        + '\n[[rituals]]\nname = "nightly-check"\nrunbook = "check"\nschedule = "daily"\n'
    )
    contents["runbooks/check.md"] = RUNBOOK
    tree = store.tree_from_files(contents)
    for relative, data in tree.files.items():
        path = store.local_root() / "ritual-demo" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    entity = projects.create("Ritual project", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        **body,
        "revision": tree.digest(),
        "destination": str(folder),
        "bindings": [],
        "project_id": entity.id,
    }
    return entity, inputs


def _apply(entity, inputs, n="1"):
    plan = review.build("ritual-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    bound = lifecycle.bind(
        entity.id, "ritual-demo", inputs, receipt["receipt"], f"bind-op-{n}0000", key=KEY
    )
    _apply.receipt = bound["receipt"]
    return apply.apply(entity.id, f"apply-op-{n}0000", bound["receipt"], key=KEY)


def test_review_shows_rituals_as_declared_and_unscheduled(ritual_demo):
    entity, inputs = ritual_demo
    rituals = review.build("ritual-demo", inputs, key=KEY).public["rituals"]
    assert rituals == [
        {
            "name": "nightly-check",
            "runbook": "check",
            "schedule": "daily",
            "state": "declared",
            "scheduled": False,
        }
    ]


def test_apply_records_each_ritual_under_its_stable_identity(ritual_demo):
    entity, inputs = ritual_demo
    result = _apply(entity, inputs)
    expected = {
        "deployment": result["deployment_id"],
        "ritual": "nightly-check",
        "version": "1.0.0",
    }
    [ritual] = record(entity.id)["rituals"]
    assert ritual["identity"] == expected
    assert ritual["state"] == "declared" and ritual["scheduled"] is False
    assert apply.status(entity.id)["rituals"] == [ritual]


def test_a_replay_or_reapply_keeps_one_identity_per_ritual(ritual_demo):
    entity, inputs = ritual_demo
    first = _apply(entity, inputs)
    [before] = record(entity.id)["rituals"]
    assert apply.apply(entity.id, "apply-op-10000", _apply.receipt, key=KEY) == first  # replay
    _apply(entity, inputs, "2")  # a second operation over the same inputs
    assert record(entity.id)["rituals"] == [before]


def test_remove_retires_the_identities_and_creates_or_deletes_nothing(ritual_demo):
    entity, inputs = ritual_demo
    result = _apply(entity, inputs)
    plan = remove.plan(entity.id, key=KEY)
    removed = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert removed["rituals_retired"] == [
        {"deployment": result["deployment_id"], "ritual": "nightly-check", "version": "1.0.0"}
    ]
    assert record(entity.id)["rituals"] == []
