"""Variable ID migration does not rewrite existing deployment journals or their receipts."""

import json

import pytest

from agent_sessions import template_vars, variables
from agent_sessions.playbooks import deployment_state, review
from test_playbook_binding_inverse import apply_inverse_for_test
from test_playbook_lifecycle_binding import Crash, bind, prepared, record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


def _as_v3():
    """Materialize the exact pre-upgrade record format, with its existing ciphertext."""
    records = template_vars._read_strictly()
    raw = json.dumps({"version": 3, "variables": records}).encode()
    template_vars.store_path().write_bytes(raw)
    return raw


def test_upgrade_preserves_bound_review_receipt_journal_and_inverse(setup):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "existing-deployment-secret"}]
    )
    result = bind(entity, body, receipt)
    saved = record(entity.id)
    original = _as_v3()
    before = review.build("review-demo", saved["inputs"], key=KEY)
    variables.migrate()
    after = review.build("review-demo", saved["inputs"], key=KEY)
    assert after.public["digest"] == before.public["digest"] == result["digest"]
    assert review.accept(after, result["receipt"], key=KEY)
    assert record(entity.id) == saved
    [backup] = list(template_vars.store_path().parent.glob("*.pre-v4-*"))
    assert backup.read_bytes() == original
    apply_inverse_for_test(entity.id)
    assert template_vars.project_bindings(entity.id) == []


@pytest.mark.parametrize("where", ["before_variables", "after_variables"])
def test_pending_legacy_bind_recovers_after_an_independent_upgrade(setup, monkeypatch, where):
    template_vars.create_variable({"name": "unused", "value": "retained"})
    _as_v3()
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "pending-deployment-secret"}]
    )
    with monkeypatch.context() as patch:
        if where == "before_variables":

            def fail(*args, **kwargs):
                raise Crash

            patch.setattr(template_vars, "atomic_write_json", fail)
        else:
            real = deployment_state.Locked.write

            def fail(self, value):
                if value["state"] == "bound":
                    raise Crash
                return real(self, value)

            patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            bind(entity, body, receipt)
    saved = record(entity.id)
    assert saved["state"] == "binding_intent"
    variables.migrate()
    assert record(entity.id) == saved
    recovered = bind(entity, body, receipt)
    assert recovered["state"] == "bound"
    assert template_vars.resolver(entity.id).secret("token") == "pending-deployment-secret"
    assert store_value("unused") == "retained"


def store_value(name):
    return template_vars.values()[name]


def test_retained_prior_reference_fences_delete_retype_and_name_recreation(setup):
    token = variables.create({"name": "token", "kind": "secret", "value": "prior-global-secret"})
    entity, body, _ = prepared(setup)
    template_vars.bind_project(entity.id, [{"name": "token", "kind": "secret", "ref": "global"}])
    body["bindings"] = [{"name": "token", "kind": "secret", "value": "own-project-secret"}]
    plan = review.build("review-demo", body, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bind(entity, body, receipt)
    with pytest.raises(template_vars.VariableInUse) as refused:
        variables.delete(token["id"], token["updated_at"])
    assert refused.value.projects == [entity.id]
    with pytest.raises(template_vars.VariableInUse):
        variables.update(
            token["id"],
            {"definition": {"required": True}, "value": "replacement-secret"},
            token["updated_at"],
        )
    # Even if a value disappears outside the API, its historical reference cannot adopt
    # a new identity with the same name.
    path = template_vars.store_path()
    broken = json.loads(path.read_text())
    broken["variables"] = [r for r in broken["variables"] if r["scope"] != "global"]
    path.write_text(json.dumps(broken))
    with pytest.raises(template_vars.VariableInUse):
        variables.create({"name": "token", "kind": "secret", "value": "unrelated-new-secret"})


@pytest.mark.parametrize("damage", ["missing", "owned-absent"])
def test_unreadable_deployment_inventory_refuses_global_deletion(setup, damage):
    host = variables.create({"name": "unused", "value": "retained"})
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    with deployment_state.locked(entity.id) as held:
        saved = held.read()
        if damage == "missing":
            saved["prior_bindings"] = None
        else:
            saved["prior_bindings"] = {"token": None}
            saved["owned_bindings"] = {"token": None}
        held.write(saved)
    before = template_vars.store_path().read_bytes()
    with pytest.raises(template_vars.VariableRefsUnknown):
        variables.delete(host["id"], host["updated_at"])
    assert template_vars.store_path().read_bytes() == before


def test_inventory_never_takes_a_project_lock_below_the_authority_fence(setup):
    from concurrent.futures import ThreadPoolExecutor

    from agent_sessions import projects

    host = variables.create({"name": "unused", "value": "retained"})
    entity, body, receipt = prepared(setup)
    bind(entity, body, receipt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with projects.locked_index(), deployment_state.locked(entity.id):
            # The inventory must finish while the earlier-order locks are still held in
            # this thread. Taking those locks below the authority fence would deadlock.
            future = pool.submit(variables.delete, host["id"], host["updated_at"])
            future.result(timeout=3)
    assert "unused" not in template_vars.values()
