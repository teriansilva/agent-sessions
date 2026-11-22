"""Resume an interrupted bind, apply or removal by its operation id alone (#2263).

A page that never held the original request (a reload, another browser, a dead session) can settle
the one operation the server journaled, and only that one. Omission is the only trigger: a request
that IS sent must still match, a request-free call never starts an operation, and a completed one
answers with its recorded result without doing work again. Each case below is a fresh caller: it
passes `None` where the original request would go.
"""

import json
import threading

import pytest

from agent_sessions import template_vars
from agent_sessions.playbooks import (
    apply,
    deployment_state,
    lifecycle,
    material_write,
    remove,
    review,
    store,
)
from test_playbook_apply import _interrupt_second_create, bound, run
from test_playbook_lifecycle_binding import Crash, prepared, record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup
SECRET = "resume-test-secret"


def _write_record(pid, value):
    with deployment_state.locked(pid) as locked:
        locked.write(value)


def _together(fn, n=2):
    """Run `fn` from n threads at once; return each outcome (a result or the exception)."""
    out: list = [None] * n
    gate = threading.Barrier(n)

    def go(i):
        gate.wait()
        try:
            out[i] = fn()
        except Exception as e:  # noqa: BLE001 - the outcome is what the test compares
            out[i] = e

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


# ---------------------------------------------------------------- removal


def applied(setup, **kw):
    entity, bound_result = bound(setup, **kw)
    run(entity, bound_result)
    return entity


def _interrupted_removal(setup, monkeypatch, op="remove-operation-1"):
    entity = applied(setup)
    plan = remove.plan(entity.id, key=KEY)
    real, calls = material_write.remove, []

    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "remove", fail_second)
        with pytest.raises(store.StoreError):
            remove.remove(entity.id, op, plan["digest"], key=KEY)
    assert apply.status(entity.id)["operation"] == "remove"
    return entity, plan


def test_a_fresh_caller_resumes_an_interrupted_removal_and_a_lost_response_replays(
    setup, monkeypatch
):
    folder, _ = setup
    entity, plan = _interrupted_removal(setup, monkeypatch)
    result = remove.remove(entity.id, "remove-operation-1", None, key=KEY)
    assert result["state"] == "removed"
    assert not (folder / "RULES.md").exists()
    assert apply.status(entity.id)["state"] == "removed"
    # The response was lost: the same request-free call answers from history and does nothing.
    before = record(entity.id)
    assert remove.remove(entity.id, "remove-operation-1", None, key=KEY) == result
    assert record(entity.id) == before
    # A present, different request under the completed id still refuses.
    with pytest.raises(store.Conflict, match="already names another request"):
        remove.remove(entity.id, "remove-operation-1", "0" * 64, key=KEY)
    # The original request replays too, as before.
    assert remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY) == result


def test_a_request_free_removal_never_starts_one(setup):
    folder, _ = setup
    entity = applied(setup)
    before = record(entity.id)
    with pytest.raises(store.Conflict, match="no removal operation is pending"):
        remove.remove(entity.id, "remove-operation-1", None, key=KEY)
    assert record(entity.id) == before
    assert (folder / "RULES.md").exists()
    assert apply.status(entity.id)["state"] == "applied"


def test_a_removal_crash_before_its_durable_intent_leaves_nothing_to_resume(setup, monkeypatch):
    folder, _ = setup
    entity = applied(setup)
    plan = remove.plan(entity.id, key=KEY)
    real = deployment_state.Locked.write

    def fail(self, value):
        if "removal_operation" in value:
            raise Crash
        return real(self, value)

    with monkeypatch.context() as patch:
        patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert apply.status(entity.id)["state"] == "applied"
    with pytest.raises(store.Conflict, match="no removal operation is pending"):
        remove.remove(entity.id, "remove-operation-1", None, key=KEY)
    assert (folder / "RULES.md").exists()


def test_a_removal_resume_refuses_a_wrong_id_a_wrong_digest_and_a_damaged_journal(
    setup, monkeypatch
):
    entity, _ = _interrupted_removal(setup, monkeypatch)
    with pytest.raises(store.Conflict, match="pending removal"):
        remove.remove(entity.id, "remove-operation-2", None, key=KEY)
    with pytest.raises(store.Conflict, match="already names another request"):
        remove.remove(entity.id, "remove-operation-1", "0" * 64, key=KEY)
    with pytest.raises(store.StoreError):
        remove.remove(entity.id, "remove-operation-1", "not a digest", key=KEY)
    damaged = record(entity.id)
    damaged["removal_operation"]["digest"] = "nope"
    _write_record(entity.id, damaged)
    with pytest.raises(store.Conflict, match="removal journal is damaged"):
        remove.remove(entity.id, "remove-operation-1", None, key=KEY)
    assert apply.status(entity.id)["state"] == "interrupted"


def test_a_removal_resume_refuses_an_operator_edit_made_during_the_interruption(setup, monkeypatch):
    folder, _ = setup
    entity, _ = _interrupted_removal(setup, monkeypatch)
    left = [p for p in ("RULES.md", "docs/playbook.md") if (folder / p).exists()]
    assert left, "the interruption left one owned file in place"
    (folder / left[0]).write_text("the operator's own edit")
    with pytest.raises(store.Conflict):
        remove.remove(entity.id, "remove-operation-1", None, key=KEY)
    assert (folder / left[0]).read_text() == "the operator's own edit"
    assert apply.status(entity.id)["state"] == "interrupted"


def test_two_concurrent_request_free_removals_settle_one_operation(setup, monkeypatch):
    entity, _ = _interrupted_removal(setup, monkeypatch)
    a, b = _together(lambda: remove.remove(entity.id, "remove-operation-1", None, key=KEY))
    assert isinstance(a, dict) and a == b and a["state"] == "removed"
    assert list(record(entity.id)["removal_history"]) == ["remove-operation-1"]


# ---------------------------------------------------------------- apply


def _interrupted_apply(setup, monkeypatch, **kw):
    entity, bound_result = bound(setup, **kw)
    with monkeypatch.context() as patch:
        _interrupt_second_create(patch)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert apply.status(entity.id)["operation"] == "apply"
    return entity, bound_result


def test_a_fresh_caller_resumes_an_interrupted_apply_and_a_lost_response_replays(
    setup, monkeypatch
):
    folder, _ = setup
    entity, bound_result = _interrupted_apply(setup, monkeypatch)
    result = apply.apply(entity.id, "apply-operation-one", None, key=KEY)
    assert result["state"] == "applied"
    assert (folder / "docs" / "playbook.md").exists()
    before = record(entity.id)
    assert apply.apply(entity.id, "apply-operation-one", None, key=KEY) == result
    assert record(entity.id) == before
    with pytest.raises(store.Conflict, match="already names another request"):
        apply.apply(entity.id, "apply-operation-one", "x" * 40, key=KEY)
    assert run(entity, bound_result) == result


def test_a_request_free_apply_never_starts_one(setup):
    folder, _ = setup
    entity, _ = bound(setup)
    before = record(entity.id)
    with pytest.raises(store.Conflict, match="no apply operation is pending"):
        apply.apply(entity.id, "apply-operation-one", None, key=KEY)
    assert record(entity.id) == before
    assert list(folder.iterdir()) == []


def test_an_apply_crash_before_its_durable_intent_leaves_nothing_to_resume(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = deployment_state.Locked.write

    def fail(self, value):
        if "apply_operation" in value:
            raise Crash
        return real(self, value)

    with monkeypatch.context() as patch:
        patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            run(entity, bound_result)
    with pytest.raises(store.Conflict, match="no apply operation is pending"):
        apply.apply(entity.id, "apply-operation-one", None, key=KEY)
    assert list(folder.iterdir()) == []


def test_an_apply_resume_refuses_a_wrong_id_a_wrong_receipt_and_a_damaged_journal(
    setup, monkeypatch
):
    entity, _ = _interrupted_apply(setup, monkeypatch)
    with pytest.raises(store.Conflict, match="pending apply"):
        apply.apply(entity.id, "apply-operation-two", None, key=KEY)
    with pytest.raises(store.Conflict, match="already names another request"):
        apply.apply(entity.id, "apply-operation-one", "x" * 40, key=KEY)
    damaged = record(entity.id)
    damaged["apply_operation"]["attempt"] = -1
    _write_record(entity.id, damaged)
    with pytest.raises(store.Conflict, match="apply journal is damaged"):
        apply.apply(entity.id, "apply-operation-one", None, key=KEY)


def test_an_apply_resume_refuses_changed_bindings_and_writes_nothing_more(setup, monkeypatch):
    folder, _ = setup
    entity, _ = _interrupted_apply(
        setup,
        monkeypatch,
        bindings=[{"name": "endpoint", "kind": "text", "value": "https://a.example/"}],
    )
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))

    def change(records):
        for r in records:
            if r["project_id"] == entity.id and r["name"] == "endpoint":
                r["value"] = "https://b.example/"
                r["updated_at"] = template_vars._bumped(r["updated_at"])

    template_vars._mutate(change)
    with pytest.raises(store.Conflict, match="inputs changed"):
        apply.apply(entity.id, "apply-operation-one", None, key=KEY)
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == files
    assert apply.status(entity.id)["state"] == "interrupted"


def test_two_concurrent_request_free_applies_settle_one_operation(setup, monkeypatch):
    entity, _ = _interrupted_apply(setup, monkeypatch)
    a, b = _together(lambda: apply.apply(entity.id, "apply-operation-one", None, key=KEY))
    assert isinstance(a, dict) and a == b and a["state"] == "applied"
    assert list(record(entity.id)["apply_history"]) == ["apply-operation-one"]


# ---------------------------------------------------------------- bind


def _interrupted_bind(setup, monkeypatch, where="after_variables", op="operation-one"):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": SECRET}]
    )
    with monkeypatch.context() as patch:
        if where == "before_variables":
            patch.setattr(
                template_vars, "atomic_write_json", lambda *a, **kw: (_ for _ in ()).throw(Crash())
            )
        else:
            real = deployment_state.Locked.write

            def fail(self, value):
                if value["state"] == "bound":
                    raise Crash
                return real(self, value)

            patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            lifecycle.bind(entity.id, "review-demo", body, receipt, op, key=KEY)
    assert record(entity.id)["state"] == "binding_intent"
    return entity, body, receipt


@pytest.mark.parametrize("where", ["before_variables", "after_variables"])
def test_a_fresh_caller_resumes_an_interrupted_bind_without_its_inputs(setup, monkeypatch, where):
    folder, _ = setup
    entity, body, receipt = _interrupted_bind(setup, monkeypatch, where)
    result = lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)
    assert result["state"] == "bound"
    assert template_vars.resolver(entity.id).secret("token") == SECRET
    assert record(entity.id)["state"] == "bound"
    # It stops at bound: no destination file, no apply journal.
    assert list(folder.iterdir()) == []
    assert "apply_operation" not in record(entity.id)
    # No input value is ever in the answer.
    assert SECRET not in json.dumps(result)
    # Lost response: the same request-free call answers from history, unchanged.
    before = record(entity.id)
    assert lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY) == result
    assert record(entity.id) == before
    # The original request still replays to the same result.
    assert (
        lifecycle.bind(entity.id, "review-demo", body, receipt, "operation-one", key=KEY) == result
    )


def test_a_request_free_bind_never_starts_one(setup):
    entity, _, _ = prepared(setup)
    with pytest.raises(store.Conflict, match="no binding operation is pending"):
        lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)
    assert record(entity.id) is None
    assert template_vars.project_bindings(entity.id) == []


def test_a_bind_crash_before_its_durable_intent_leaves_nothing_to_resume(setup, monkeypatch):
    entity, body, receipt = prepared(
        setup, bindings=[{"name": "token", "kind": "secret", "value": SECRET}]
    )
    real = deployment_state.Locked.write

    def fail(self, value):
        if value.get("state") == "binding_intent":
            raise Crash
        return real(self, value)

    with monkeypatch.context() as patch:
        patch.setattr(deployment_state.Locked, "write", fail)
        with pytest.raises(Crash):
            lifecycle.bind(entity.id, "review-demo", body, receipt, "operation-one", key=KEY)
    with pytest.raises(store.Conflict, match="no binding operation is pending"):
        lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)
    assert template_vars.project_bindings(entity.id) == []


@pytest.mark.parametrize(
    "partial",
    [
        ("review-demo", None, None),
        (None, {}, None),
        (None, None, "a-receipt"),
        ("review-demo", {}, None),
    ],
)
def test_a_partial_bind_request_refuses_and_is_never_a_resume(setup, monkeypatch, partial):
    entity, _, _ = _interrupted_bind(setup, monkeypatch)
    with pytest.raises(store.StoreError, match="whole request, or only its operation_id"):
        lifecycle.bind(entity.id, *partial, "operation-one", key=KEY)
    assert record(entity.id)["state"] == "binding_intent"


def test_a_bind_resume_refuses_a_wrong_id_a_wrong_request_and_a_damaged_journal(setup, monkeypatch):
    entity, body, receipt = _interrupted_bind(setup, monkeypatch)
    with pytest.raises(store.Conflict, match="no binding operation is pending"):
        lifecycle.bind(entity.id, None, None, None, "operation-two", key=KEY)
    with pytest.raises(store.Conflict, match="already names another request"):
        lifecycle.bind(
            entity.id, "review-demo", {**body, "bindings": []}, receipt, "operation-one", key=KEY
        )
    damaged = record(entity.id)
    damaged["binding_operation"]["apply_digest"] = "nope"
    _write_record(entity.id, damaged)
    with pytest.raises(store.Conflict, match="damaged"):
        lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)


def test_a_bind_resume_never_overwrites_an_operator_edit_and_keeps_other_bindings(
    setup, monkeypatch
):
    entity, _, _ = _interrupted_bind(setup, monkeypatch)
    template_vars.create_variable({"name": "shared", "kind": "text", "value": "global-value"})
    template_vars.bind_project(
        entity.id, [{"name": "token", "kind": "secret", "value": "operator-secret"}]
    )
    with pytest.raises(store.Conflict, match="changed during recovery"):
        lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)
    assert template_vars.resolver(entity.id).secret("token") == "operator-secret"
    assert template_vars.resolver(entity.id).text("shared")["value"] == "global-value"
    assert record(entity.id)["state"] == "binding_intent"


def test_two_concurrent_request_free_binds_settle_one_operation(setup, monkeypatch):
    entity, _, _ = _interrupted_bind(setup, monkeypatch)
    a, b = _together(lambda: lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY))
    assert isinstance(a, dict) and a == b and a["state"] == "bound"
    assert list(record(entity.id)["binding_history"]) == ["operation-one"]


def test_a_resumed_bind_settles_at_any_receipt_age_and_apply_still_needs_a_valid_one(
    setup, monkeypatch
):
    folder, _ = setup
    entity, _, _ = _interrupted_bind(setup, monkeypatch)
    # Every receipt is past its lifetime from here on.
    monkeypatch.setattr(review, "REVIEW_TTL", -1)
    result = lifecycle.bind(entity.id, None, None, None, "operation-one", key=KEY)
    assert result["state"] == "bound"
    with pytest.raises(store.StoreError):
        apply.apply(entity.id, "apply-operation-one", result["receipt"], key=KEY)
    with pytest.raises(store.Conflict, match="no apply operation is pending"):
        apply.apply(entity.id, "apply-operation-one", None, key=KEY)
    assert list(folder.iterdir()) == []
    # Within its lifetime the recorded receipt is what the separate apply step sends.
    monkeypatch.setattr(review, "REVIEW_TTL", 3600)
    assert apply.apply(entity.id, "apply-operation-one", result["receipt"], key=KEY)["state"] == (
        "applied"
    )
