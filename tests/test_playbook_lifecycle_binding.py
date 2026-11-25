"""Binding is one reviewed, recoverable operation on the existing scoped variable store."""

import json

import pytest

from agent_sessions import projects, template_vars
from agent_sessions.playbooks import deployment_state, lifecycle, review, store
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


def prepared(setup, *, bindings=None, project=True, folder=None):
    original_folder, body = setup
    folder = folder or original_folder
    entity = projects.create("Bound project", folders=[str(folder)], default_folder=str(folder))
    body = {**body, "destination": str(folder), "bindings": bindings or []}
    if project:
        body["project_id"] = entity.id
    plan = review.build("review-demo", body, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)[
        "receipt"
    ]
    return entity, body, receipt


def bind(entity, body, receipt, op="operation-one"):
    return lifecycle.bind(entity.id, "review-demo", body, receipt, op, key=KEY)


def record(pid):
    with deployment_state.locked(pid) as locked:
        return locked.read()


def test_bind_persists_exact_inputs_but_no_plaintext_secret_or_destination_file(setup):
    entity, body, receipt = prepared(
        setup,
        bindings=[
            {"name": "endpoint", "kind": "text", "value": "https://operator.example/health"},
            {"name": "token", "kind": "secret", "value": "binding-test-secret"},
        ],
    )
    result = bind(entity, body, receipt)
    resolver = template_vars.resolver(entity.id)
    assert resolver.text("endpoint")["value"] == "https://operator.example/health"
    assert resolver.secret("token") == "binding-test-secret"
    state = record(entity.id)
    assert state["state"] == "bound" and state["binding_operation"]["state"] == "complete"
    assert "binding-test-secret" not in json.dumps(state)
    assert "binding-test-secret" not in json.dumps(result)
    assert state["inputs"]["bindings"] == []
    current = review.build("review-demo", state["inputs"], key=KEY)
    assert current.public["digest"] == result["digest"]
    assert review.accept(current, result["receipt"], key=KEY)
    assert list(setup[0].iterdir()) == []


def test_implicit_global_fallback_becomes_a_deletion_dependency(setup):
    global_value = template_vars.create_variable(
        {"name": "endpoint", "kind": "text", "value": "https://global.example/"}
    )
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    saved = next(r for r in template_vars._read_strictly() if r["project_id"] == entity.id)
    assert saved["ref"] == "global"
    with pytest.raises(template_vars.VariableInUse):
        template_vars.delete_variable("endpoint", global_value["updated_at"])


def test_exact_replay_does_not_rebind_and_a_changed_request_cannot_reuse_the_id(setup):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "first-secret"}]
    )
    first = bind(entity, body, receipt)
    variables = template_vars._read_strictly()
    assert bind(entity, body, receipt) == first
    assert template_vars._read_strictly() == variables
    changed = {**body, "bindings": [{"name": "token", "kind": "secret", "value": "second-secret"}]}
    with pytest.raises(store.Conflict, match="another request"):
        bind(entity, changed, receipt)
    assert template_vars.resolver(entity.id).secret("token") == "first-secret"


def test_pre_project_review_binds_only_to_the_chosen_new_project(setup):
    entity, body, receipt = prepared(
        setup,
        project=False,
        bindings=[{"name": "token", "kind": "secret", "value": "project-secret"}],
    )
    bind(entity, body, receipt)
    assert template_vars.resolver(entity.id).secret("token") == "project-secret"


def test_earlier_binding_replay_remains_observation_after_another_bind_completed(setup):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "first-secret"}]
    )
    first = bind(entity, body, receipt)
    second_inputs = {
        **body,
        "bindings": [{"name": "token", "kind": "secret", "value": "second-secret"}],
    }
    second_plan = review.build("review-demo", second_inputs, key=KEY)
    second_receipt = review.confirm(second_plan, second_plan.public["digest"], [], key=KEY)[
        "receipt"
    ]
    bind(entity, second_inputs, second_receipt, "operation-two")
    assert bind(entity, body, receipt) == first
    assert template_vars.resolver(entity.id).secret("token") == "second-secret"
    assert record(entity.id)["prior_bindings"]["token"] is None


def test_two_projects_bind_the_same_names_without_sharing_secrets(setup):
    first, first_inputs, first_receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "first-secret"}]
    )
    bind(first, first_inputs, first_receipt)
    second_folder = setup[0].parent / "second"
    second_folder.mkdir()
    second, second_inputs, second_receipt = prepared(
        setup,
        folder=second_folder,
        bindings=[{"name": "token", "kind": "secret", "value": "second-secret"}],
    )
    bind(second, second_inputs, second_receipt)
    assert template_vars.resolver(first.id).secret("token") == "first-secret"
    assert template_vars.resolver(second.id).secret("token") == "second-secret"
    assert template_vars.secret_state() == {}


class Crash(BaseException):
    pass


@pytest.mark.parametrize("where", ["before_variables", "after_variables"])
def test_retry_reconciles_a_crash_on_either_side_of_variable_publication(setup, monkeypatch, where):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "recover-secret"}]
    )
    with monkeypatch.context() as patch:
        if where == "before_variables":

            def fail(*args, **kwargs):
                raise Crash

            patch.setattr(template_vars, "atomic_write_json", fail)
        else:
            real_write = deployment_state.Locked.write

            def fail(self, value):
                if value["state"] == "bound":
                    raise Crash
                return real_write(self, value)

            patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            bind(entity, body, receipt)
    assert record(entity.id)["state"] == "binding_intent"
    with pytest.raises(store.Conflict, match="pending"):
        bind(entity, body, receipt, op="another-operation")
    settled = bind(entity, body, receipt)
    assert settled["state"] == "bound"
    assert template_vars.resolver(entity.id).secret("token") == "recover-secret"
    assert record(entity.id)["state"] == "bound"


def test_stale_review_never_overwrites_project_bindings(setup):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "stale-secret"}]
    )
    (setup[0] / "RULES.md").write_text("Operator edited after review\n")
    with pytest.raises(store.Conflict):
        bind(entity, body, receipt)
    assert template_vars.project_bindings(entity.id) == []
    assert record(entity.id) is None


def test_authoring_refuses_deleting_a_bound_playbook(setup):
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    with pytest.raises(store.InUse):
        store.delete_playbook("review-demo", body["revision"])


def test_recovery_never_overwrites_a_binding_edited_after_the_crash(setup, monkeypatch):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "intended-secret"}]
    )
    real_write = deployment_state.Locked.write
    with monkeypatch.context() as patch:

        def fail(self, value):
            if value["state"] == "bound":
                raise Crash
            return real_write(self, value)

        patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            bind(entity, body, receipt)
    template_vars.bind_project(
        entity.id, [{"name": "token", "kind": "secret", "value": "operator-secret"}]
    )
    with pytest.raises(store.Conflict, match="changed during recovery"):
        bind(entity, body, receipt)
    assert template_vars.resolver(entity.id).secret("token") == "operator-secret"


def test_a_bound_project_cannot_be_deleted_archived_or_release_its_destination(setup):
    folder, _ = setup
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    with pytest.raises(projects.ProjectError, match="deployment"):
        projects.delete(entity.id)
    with pytest.raises(projects.ProjectError, match="deployment"):
        projects.update(entity.id, archived=True)
    other = folder.parent / "other"
    other.mkdir()
    with pytest.raises(projects.ProjectError, match="deployment"):
        projects.update(entity.id, folders=[str(other)], default_folder=str(other))
    assert list(projects.load()[entity.id].folders) == [str(folder)]
    # Unrelated edits still work.
    assert projects.update(entity.id, name="Renamed").name == "Renamed"


def test_a_damaged_deployment_record_refuses_project_deletion(setup):
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    with deployment_state.locked(entity.id) as locked:
        path = f"/proc/self/fd/{locked.project_fd}/{deployment_state.RECORD}"
        with open(path, "w") as fh:
            fh.write("{damaged")
    with pytest.raises(projects.ProjectError, match="cannot be read"):
        projects.delete(entity.id)
    assert entity.id in projects.load()


def _mark_removed(pid, *, keep_journal):
    with deployment_state.locked(pid) as locked:
        removed = {**locked.read(), "state": "removed"}
        if not keep_journal:
            removed.pop("binding_operation")
        locked.write(removed)
    return removed


def test_a_removed_deployment_still_holding_a_journal_refuses_review_and_rebinding(setup):
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    removed = _mark_removed(entity.id, keep_journal=True)
    assert deployment_state.holds(removed)
    with pytest.raises(store.Conflict, match="unsettled operation"):
        review.build("review-demo", body, key=KEY)
    with pytest.raises(store.Conflict, match="unsettled operation"):
        bind(entity, body, receipt, op="operation-two")
    assert record(entity.id) == removed  # the journal is never discarded


def test_a_settled_journal_free_removal_can_be_bound_again(setup):
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    _mark_removed(entity.id, keep_journal=False)
    plan = review.build("review-demo", body, key=KEY)
    fresh = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)["receipt"]
    assert bind(entity, body, fresh, op="operation-two")["state"] == "bound"
    assert record(entity.id)["state"] == "bound"
