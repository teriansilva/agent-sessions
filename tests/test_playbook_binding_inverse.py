"""Binding removal is scoped, owned, and never an implicit overwrite of an operator edit."""

import copy

import pytest

from agent_sessions import projects, template_vars
from agent_sessions.playbooks import binding_inverse, deployment_state, store
from test_playbook_lifecycle_binding import bind, prepared, record
from test_playbook_review import setup as _review_setup

setup = _review_setup


def apply_inverse_for_test(pid):
    # Exercise the planner against the existing mutation fence and secret-retirement seam;
    # production destination removal/recovery still needs its own coordinator.
    with projects.locked_index(), deployment_state.locked(pid) as locked:
        saved = locked.read()

        def mutate(records):
            planned, before, _ = binding_inverse.prepare(saved, records)
            for previous in before.values():
                template_vars._retire(previous)
            records[:] = planned

        template_vars._mutate(mutate)


def test_removing_one_projects_owned_text_and_secret_preserves_the_other_and_globals(setup):
    template_vars.create_variable(
        {"name": "endpoint", "kind": "text", "value": "https://global.example/"}
    )
    template_vars.create_variable({"name": "token", "kind": "secret", "value": "global-secret"})
    entities = []
    for label in ("first", "second"):
        folder = setup[0].parent / label
        folder.mkdir()
        entity, body, receipt = prepared(
            setup,
            folder=folder,
            bindings=[
                {"name": "endpoint", "kind": "text", "value": f"https://{label}.example/"},
                {"name": "token", "kind": "secret", "value": f"{label}-secret"},
            ],
        )
        bind(entity, body, receipt)
        entities.append(entity)
    snapshot = template_vars._read_strictly()
    apply_inverse_for_test(entities[0].id)
    expected = [r for r in snapshot if r["project_id"] != entities[0].id]
    assert template_vars._read_strictly() == expected
    resolver = template_vars.resolver(entities[1].id)
    assert resolver.text("endpoint")["value"] == "https://second.example/"
    assert resolver.secret("token") == "second-secret"
    assert template_vars.secret_values()["token"] == "global-secret"


def test_preexisting_project_bindings_are_restored_from_their_original_envelopes(setup):
    entity, body, receipt = prepared(setup)
    template_vars.bind_project(
        entity.id, [{"name": "token", "kind": "secret", "value": "prior-secret"}]
    )
    original = template_vars._read_strictly()
    # Review again after making the explicit preexisting project binding.
    from agent_sessions.playbooks import review
    from test_playbook_review import KEY

    body["bindings"] = [{"name": "token", "kind": "secret", "value": "deployed-secret"}]
    plan = review.build("review-demo", body, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bind(entity, body, receipt)
    apply_inverse_for_test(entity.id)
    assert template_vars._read_strictly() == original
    assert template_vars.resolver(entity.id).secret("token") == "prior-secret"


def test_an_operator_edit_to_a_bound_name_refuses_the_entire_inverse(setup):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "deployed-secret"}]
    )
    bind(entity, body, receipt)
    template_vars.bind_project(
        entity.id, [{"name": "token", "kind": "secret", "value": "operator-secret"}]
    )
    before = template_vars._read_strictly()
    with pytest.raises(store.Conflict, match="operator edits"):
        apply_inverse_for_test(entity.id)
    assert template_vars._read_strictly() == before


def test_a_deleted_global_cannot_be_silently_restored_as_a_dangling_reference(setup):
    from agent_sessions.playbooks import review
    from test_playbook_review import KEY

    global_record = template_vars.create_variable(
        {"name": "token", "kind": "secret", "value": "prior-global-secret"}
    )
    entity, body, _ = prepared(setup)
    template_vars.bind_project(entity.id, [{"name": "token", "kind": "secret", "ref": "global"}])
    body["bindings"] = [{"name": "token", "kind": "secret", "value": "own-deployed-secret"}]
    plan = review.build("review-demo", body, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bind(entity, body, receipt)
    template_vars.delete_variable("token", global_record["updated_at"])
    with pytest.raises(store.Conflict, match="global reference can no longer be restored"):
        apply_inverse_for_test(entity.id)
    assert template_vars.resolver(entity.id).secret("token") == "own-deployed-secret"


@pytest.mark.parametrize("damage", ["scope", "missing", "project", "extra"])
def test_damaged_binding_ownership_never_selects_another_scope(setup, damage):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "deployed-secret"}]
    )
    bind(entity, body, receipt)
    saved = copy.deepcopy(record(entity.id))
    if damage == "scope":
        saved["owned_bindings"]["token"].update(scope="global", project_id=None)
    elif damage == "missing":
        del saved["prior_bindings"]["token"]
    elif damage == "project":
        saved["owned_bindings"]["token"]["project_id"] = "p-other"
    else:
        saved["owned_bindings"]["token"]["unknown"] = True
    with pytest.raises(store.Conflict, match="ownership is damaged"):
        binding_inverse.prepare(saved, template_vars._read_strictly())
