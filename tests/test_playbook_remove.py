"""Remove is the exact inverse of what a deployment can prove it owns, and nothing else."""

import json
import os
from pathlib import Path

import pytest

from agent_sessions import fileedit, projects, template_vars
from agent_sessions.playbooks import (
    apply,
    deployment_state,
    lifecycle,
    material_write,
    remove,
    review,
    store,
)
from test_playbook_apply import EXPECTED_RULES, _swap_same_bytes, bound, run
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


def applied(setup, **kw):
    entity, bound_result = bound(setup, **kw)
    run(entity, bound_result)
    return entity


def go(entity, op="remove-operation-1"):
    plan = remove.plan(entity.id, key=KEY)
    assert not plan["conflicts"], plan["conflicts"]
    return remove.remove(entity.id, op, plan["digest"], key=KEY)


def only_empty_docs(folder):
    return [p.name for p in folder.iterdir()] == ["docs"] and not any((folder / "docs").iterdir())


def retained_names():
    root = Path(fileedit.recovery_dir()) / apply.ENTRIES
    return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_remove_undoes_exactly_the_apply_and_releases_the_project(setup):
    folder, _ = setup
    entity = applied(setup)
    plan = remove.plan(entity.id, key=KEY)
    assert {c["path"]: c["action"] for c in plan["changes"]} == {
        "RULES.md": "remove",
        "docs/playbook.md": "remove",
    }
    result = go(entity)
    assert result["state"] == "removed" and result["kept_directories"] == ["docs"]
    # Only the folder apply created is left, empty: removal never deletes directories.
    assert only_empty_docs(folder)
    assert "removed" in retained_names()  # displaced bytes are retained, never deleted
    state = record(entity.id)
    assert state["state"] == "removed" and state["files"] == {} and state["directories"] == {}
    assert not {"binding_operation", "apply_operation", "removal_operation"} & set(state)
    assert deployment_state.active(entity.id) is None
    projects.delete(entity.id)  # the project fence is released


def test_an_operator_file_keeps_everything_but_the_region(setup):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity = applied(setup)
    assert "Check" in (folder / "RULES.md").read_text()
    go(entity)
    assert (folder / "RULES.md").read_text() == "Operator notes.\n"


def test_bindings_return_to_their_exact_prior_records(setup):
    template_vars.create_variable(
        {"name": "endpoint", "kind": "text", "value": "https://global.example/"}
    )
    entity = applied(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "remove-test-secret"}]
    )
    resolver = template_vars.resolver(entity.id)
    assert resolver.secret("token") == "remove-test-secret"
    go(entity)
    mine = [r for r in template_vars._read_strictly() if r["project_id"] == entity.id]
    assert mine == []
    assert [r["name"] for r in template_vars._read_strictly()] == ["endpoint"]
    assert "remove-test-secret" not in json.dumps(record(entity.id))


def test_an_operator_edit_is_a_conflict_and_nothing_is_removed(setup):
    folder, _ = setup
    entity = applied(setup)
    (folder / "RULES.md").write_text("my edit\n")
    plan = remove.plan(entity.id, key=KEY)
    assert plan["conflicts"] and plan["conflicts"][0]["path"] == "RULES.md"
    with pytest.raises(store.Conflict, match="conflicts"):
        remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert (folder / "docs" / "playbook.md").exists()
    assert record(entity.id)["state"] == "applied"


def test_a_same_byte_swap_of_an_owned_file_is_never_removed(setup):
    folder, _ = setup
    entity = applied(setup)
    _swap_same_bytes(folder / "RULES.md")
    plan = remove.plan(entity.id, key=KEY)
    assert any("operator edits" in c["reason"] for c in plan["conflicts"])
    with pytest.raises(store.Conflict):
        remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert (folder / "RULES.md").read_text() == EXPECTED_RULES


def test_a_stale_plan_digest_refuses(setup):
    folder, _ = setup
    entity = applied(setup)
    plan = remove.plan(entity.id, key=KEY)
    (folder / "unrelated.txt").write_text("x")  # not in the plan: digest still holds
    (folder / "docs" / "playbook.md").write_text("changed after the dry run")
    with pytest.raises(store.Conflict):
        remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert (folder / "RULES.md").exists()


def test_an_interrupted_removal_settles_by_same_id_retry_and_holds_until_then(setup, monkeypatch):
    folder, _ = setup
    entity = applied(setup)
    plan = remove.plan(entity.id, key=KEY)
    real = material_write.remove
    calls = []

    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "remove", fail_second)
        with pytest.raises(store.StoreError):
            remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert deployment_state.active(entity.id) is not None
    with pytest.raises(projects.ProjectError):
        projects.delete(entity.id)
    with pytest.raises(store.Conflict, match="pending removal"):
        remove.remove(entity.id, "remove-operation-2", plan["digest"], key=KEY)
    result = remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert result["state"] == "removed" and only_empty_docs(folder)
    assert remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY) == result


def test_a_directory_holding_operator_files_is_kept_and_reported(setup):
    folder, _ = setup
    entity = applied(setup)
    (folder / "docs" / "mine.md").write_text("operator file")
    result = go(entity)
    assert result["kept_directories"] == ["docs"]
    assert (folder / "docs" / "mine.md").read_text() == "operator file"
    assert not (folder / "docs" / "playbook.md").exists()


def test_a_replaced_created_directory_is_never_deleted(setup):
    folder, _ = setup
    entity = applied(setup)
    # Removal never deletes directories, so even a replaced one is simply left and reported.
    os.rename(folder / "docs", folder / "docs-aside")
    (folder / "docs").mkdir()  # an operator's directory at the same name
    result = remove.remove(
        entity.id, "remove-operation-1", remove.plan(entity.id, key=KEY)["digest"], key=KEY
    )
    assert result["kept_directories"] == ["docs"] and (folder / "docs").is_dir()


def test_remove_resolves_an_interrupted_apply_by_removing_only_proven_files(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = material_write.create
    calls = []

    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "create", fail_second)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert apply.status(entity.id)["state"] == "interrupted"
    (folder / "docs").mkdir(exist_ok=True)
    (folder / "docs" / "playbook.md").write_text("never proven ours")
    plan = remove.plan(entity.id, key=KEY)
    assert [c["path"] for c in plan["changes"] if c["action"] == "remove"] == ["RULES.md"]
    result = remove.remove(entity.id, "remove-operation-1", plan["digest"], key=KEY)
    assert result["state"] == "removed"
    assert not (folder / "RULES.md").exists()
    assert (folder / "docs" / "playbook.md").read_text() == "never proven ours"
    assert "apply_operation" not in record(entity.id)
    assert apply.status(entity.id)["state"] == "removed"


def test_a_removed_deployment_can_be_bound_and_applied_again(setup):
    folder, body = setup
    entity = applied(setup)
    go(entity)
    stored = record(entity.id)["inputs"]
    plan = review.build("review-demo", stored, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    rebound = lifecycle.bind(
        entity.id, "review-demo", stored, receipt["receipt"], "rebind-operation", key=KEY
    )
    assert run(entity, rebound, op="apply-operation-two")["state"] == "applied"
    assert (folder / "RULES.md").read_text() == EXPECTED_RULES


def test_remove_after_an_interrupted_re_apply_uses_the_proven_replacements(setup, monkeypatch):
    folder, _ = setup
    entity = applied(setup)
    stored = record(entity.id)["inputs"]
    changed = {
        **stored,
        "bindings": [{"name": "endpoint", "kind": "text", "value": "https://next.example/"}],
    }
    plan = review.build("review-demo", changed, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    rebound = lifecycle.bind(
        entity.id, "review-demo", changed, receipt["receipt"], "rebind-operation", key=KEY
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            apply, "_settle", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash at settle"))
        )
        with pytest.raises(store.StoreError):
            run(entity, rebound, op="apply-operation-two")
    assert "next.example" in (folder / "RULES.md").read_text()  # every new effect was done
    removal = remove.plan(entity.id, key=KEY)
    assert not removal["conflicts"], removal["conflicts"]
    remove.remove(entity.id, "remove-operation-1", removal["digest"], key=KEY)
    assert only_empty_docs(folder)


def test_an_operator_edit_around_an_interrupted_region_still_strips_the_region(setup, monkeypatch):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)
    real = material_write.create

    def fail(*a, **kw):  # the region replace ran first; the docs/playbook.md create crashes
        raise OSError("simulated crash")

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "create", fail)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert real is material_write.create
    text = (folder / "RULES.md").read_text()
    assert "Check https://example.com/health." in text
    with open(folder / "RULES.md", "r+") as fh:  # an in-place edit of the operator's prefix
        fh.write("OPERATOR notes.")
    removal = remove.plan(entity.id, key=KEY)
    assert [c["path"] for c in removal["changes"] if c["action"] != "keep"] == ["RULES.md"]
    remove.remove(entity.id, "remove-operation-1", removal["digest"], key=KEY)
    assert (folder / "RULES.md").read_text() == "OPERATOR notes.\n"
