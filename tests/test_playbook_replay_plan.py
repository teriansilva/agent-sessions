"""Recovery recomputes live dependencies against a bounded original material pre-state."""

import copy
import json
import os

import pytest

from agent_sessions import prefs, template_vars
from agent_sessions.playbooks import replay_plan, review, store
from test_playbook_lifecycle_binding import bind, prepared, record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


def accepted(setup):
    entity, inputs, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "frozen-private-secret"}]
    )
    bind(entity, inputs, receipt)
    basis = record(entity.id)
    plan = review.build("review-demo", basis["inputs"], key=KEY)
    return basis, plan, replay_plan.freeze(plan)


def resume(basis, frozen):
    return replay_plan.resume("review-demo", basis["inputs"], basis, frozen, key=KEY)


def test_frozen_prestate_contains_no_binding_secret_or_envelope(setup):
    basis, plan, frozen = accepted(setup)
    wire = json.dumps(frozen)
    assert "frozen-private-secret" not in wire
    assert "ciphertext" not in wire and "dependencies" not in wire and "bindings" not in wire
    assert resume(basis, json.loads(wire)).public == plan.public


def test_existing_text_bytes_and_old_timestamps_round_trip_and_changed_bytes_refuse(setup):
    path = setup[0] / "RULES.md"
    path.write_bytes(b"Operator preface\r\n")
    os.utime(path, ns=(-1_000_000_000, -1_000_000_000))
    basis, plan, frozen = accepted(setup)
    assert resume(basis, json.loads(json.dumps(frozen))).nodes == plan.nodes
    frozen["prestate"]["RULES.md"]["data"] = "@@not-base64@@"
    with pytest.raises(store.Conflict, match="pre-state is damaged"):
        resume(basis, frozen)


def test_recovery_preserves_the_original_plan_after_its_first_file_effect(setup):
    basis, plan, frozen = accepted(setup)
    # An operation's own writes invalidate a fresh public review, but not its frozen before
    # image. The effect coordinator must separately reconcile this live file before continuing.
    (setup[0] / "RULES.md").write_text("Check https://example.com/health.\n")
    assert (
        review.build("review-demo", basis["inputs"], key=KEY).public["digest"]
        != plan.public["digest"]
    )
    assert resume(basis, frozen).public["digest"] == plan.public["digest"]


@pytest.mark.parametrize("change", ["binding", "policy", "source", "record"])
def test_live_dependency_changes_refuse_recovery(setup, change):
    basis, _, frozen = accepted(setup)
    if change == "binding":
        template_vars.bind_project(
            basis["project_id"],
            [{"name": "endpoint", "kind": "text", "value": "https://changed.example/"}],
        )
    elif change == "policy":
        prefs.set_folder_exclusions([str(setup[0])])
    elif change == "source":
        (store.local_root() / "review-demo" / "template" / "RULES.md").write_text("changed\n")
    else:
        basis["generation"] += 1
    with pytest.raises(store.StoreError):
        resume(basis, frozen)


def test_freezing_unsaved_secret_inputs_refuses_before_serializing_them(setup):
    plan = review.build(
        "review-demo",
        {**setup[1], "bindings": [{"name": "token", "kind": "secret", "value": "unsaved-secret"}]},
        key=KEY,
    )
    with pytest.raises(store.Conflict, match="bind the reviewed inputs"):
        replay_plan.freeze(plan)


@pytest.mark.parametrize(
    "damage", ["version", "digest", "missing", "extra", "identity", "kind", "data", "path"]
)
def test_damaged_or_changed_prestate_never_authorizes_recovery(setup, damage):
    basis, _, frozen = accepted(setup)
    frozen = copy.deepcopy(frozen)
    if damage == "version":
        frozen["version"] = 2
    elif damage == "digest":
        frozen["digest"] = "0" * 64
    elif damage == "missing":
        del frozen["prestate"]["RULES.md"]
    elif damage == "extra":
        frozen["prestate"]["unknown"] = dict(frozen["prestate"]["RULES.md"])
    elif damage == "identity":
        frozen["prestate"]["RULES.md"]["identity"] = [True]
    elif damage == "kind":
        frozen["prestate"]["RULES.md"]["kind"] = "device"
    elif damage == "data":
        frozen["prestate"]["RULES.md"]["data"] = "unreviewed bytes"
    else:
        frozen["prestate"]["../escape"] = frozen["prestate"].pop("RULES.md")
    with pytest.raises(store.Conflict):
        resume(basis, frozen)


def test_public_review_cannot_take_a_supplied_prestate(setup):
    with pytest.raises(store.StoreError, match="review takes"):
        review.build("review-demo", {**setup[1], "_prestate": {}}, key=KEY)
