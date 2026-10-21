"""Apply writes exactly the bound review, records ownership last, and recovers by retry."""

import json
import os
from pathlib import Path

import pytest

from agent_sessions import fileedit, template_vars
from agent_sessions.playbooks import (
    apply,
    deployment_state,
    lifecycle,
    material_write,
    review,
    store,
)
from test_playbook_lifecycle_binding import bind, prepared, record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


def bound(setup, **kw):
    entity, body, receipt = prepared(setup, **kw)
    result = bind(entity, body, receipt)
    return entity, result


def run(entity, result, op="apply-operation-one"):
    return apply.apply(entity.id, op, result["receipt"], key=KEY)


def test_apply_writes_the_bound_materials_and_records_ownership_last(setup):
    folder, _ = setup
    entity, bound_result = bound(setup)
    result = run(entity, bound_result)
    assert result["state"] == "applied" and result["deployment_id"] == bound_result["deployment_id"]
    assert (folder / "RULES.md").read_text() == "Check https://example.com/health.\n"
    assert "Inspect" in (folder / "docs" / "playbook.md").read_text()
    state = record(entity.id)
    assert state["state"] == "applied" and state["generation"] == 1
    assert set(state["files"]) == {"RULES.md", "docs/playbook.md"}
    assert set(state["directories"]) == {"docs"}
    assert state["apply_operation"]["state"] == "complete"
    assert apply.status(entity.id)["state"] == "applied"


def test_an_exact_replay_returns_the_result_without_writing_and_an_id_is_never_reused(setup):
    folder, _ = setup
    entity, bound_result = bound(setup)
    first = run(entity, bound_result)
    inode = (folder / "RULES.md").stat().st_ino
    assert run(entity, bound_result) == first
    assert (folder / "RULES.md").stat().st_ino == inode
    with pytest.raises(store.Conflict, match="another request"):
        apply.apply(entity.id, "apply-operation-one", bound_result["receipt"] + "x", key=KEY)


def test_apply_needs_a_binding_and_its_signed_review(setup):
    folder, _ = setup
    entity, body, receipt = prepared(setup)
    with pytest.raises(store.Conflict, match="bind"):
        apply.apply(entity.id, "apply-operation-one", "not-a-receipt", key=KEY)
    bind(entity, body, receipt)
    with pytest.raises(store.StoreError):
        apply.apply(entity.id, "apply-operation-one", "not-a-receipt", key=KEY)
    assert not (folder / "RULES.md").exists()


def test_operator_text_receives_a_region_through_the_guarded_replace(setup):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)
    run(entity, bound_result)
    text = (folder / "RULES.md").read_text()
    assert text.startswith("Operator notes.\n")
    assert "Check https://example.com/health." in text
    assert record(entity.id)["files"]["RULES.md"]["kind"] == "region"


def _interrupt_second_create(patch):
    """Fail the second create; `patch` is a scoped `monkeypatch.context()` (undo drops pins)."""
    real = material_write.create
    calls = []

    def create(folder, change, *a, **kw):
        calls.append(change.path)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(folder, change, *a, **kw)

    patch.setattr(material_write, "create", create)
    return calls


def test_an_interrupted_apply_reports_each_file_and_a_same_id_retry_settles_it(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    with monkeypatch.context() as patch:
        _interrupt_second_create(patch)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    status = apply.status(entity.id)
    assert status["state"] == "interrupted" and status["operation_id"] == "apply-operation-one"
    by_path = {row["path"]: row["state"] for row in status["files"]}
    assert by_path == {"RULES.md": "written", "docs/playbook.md": "not written"}
    # Ownership is not recorded for a half-applied plan.
    assert record(entity.id)["files"] == {}
    with pytest.raises(store.Conflict, match="pending apply"):
        run(entity, bound_result, op="apply-operation-two")
    with pytest.raises(store.Conflict, match="pending apply"):
        lifecycle.bind(
            entity.id, "review-demo", {}, bound_result["receipt"], "rebind-op-1", key=KEY
        )
    result = run(entity, bound_result)
    assert result["state"] == "applied"
    assert (folder / "docs" / "playbook.md").exists()
    assert apply.status(entity.id)["state"] == "applied"


def test_a_retry_refuses_an_operator_edit_made_during_the_interruption(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    with monkeypatch.context() as patch:
        _interrupt_second_create(patch)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    (folder / "docs").mkdir(exist_ok=True)
    (folder / "docs" / "playbook.md").write_text("the operator's own file")
    with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
        run(entity, bound_result)
    assert (folder / "docs" / "playbook.md").read_text() == "the operator's own file"
    assert apply.status(entity.id)["state"] == "interrupted"


def test_a_retry_refuses_when_the_bound_inputs_changed(setup, monkeypatch):
    entity, bound_result = bound(
        setup, bindings=[{"name": "endpoint", "kind": "text", "value": "https://a.example/"}]
    )
    with monkeypatch.context() as patch:
        _interrupt_second_create(patch)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)

    def change(records):
        for r in records:
            if r["project_id"] == entity.id and r["name"] == "endpoint":
                r["value"] = "https://b.example/"
                r["updated_at"] = template_vars._bumped(r["updated_at"])

    template_vars._mutate(change)
    with pytest.raises(store.Conflict, match="inputs changed"):
        run(entity, bound_result)


def test_apply_records_no_secret_value(setup):
    entity, bound_result = bound(
        setup, bindings=[{"name": "token", "kind": "secret", "value": "apply-test-secret"}]
    )
    run(entity, bound_result)
    with deployment_state.locked(entity.id) as locked:
        assert "apply-test-secret" not in json.dumps(locked.read())


def test_a_crash_after_a_write_but_before_its_record_is_unknown_and_retry_adopts_the_bytes(
    setup, monkeypatch
):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = material_write.create

    def create(folder_, change, parents, entry, *, progress, admit):
        def lost(update):
            if update["phase"] == "done":
                raise OSError("simulated crash before the effect was journaled")
            progress(update)

        return real(folder_, change, parents, entry, progress=lost, admit=admit)

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "create", create)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    status = apply.status(entity.id)
    assert {row["path"]: row["state"] for row in status["files"]}["RULES.md"] == "unknown"
    assert run(entity, bound_result)["state"] == "applied"
    assert (folder / "RULES.md").read_text() == "Check https://example.com/health.\n"


def test_an_interrupted_guarded_replace_is_retried_from_the_reviewed_bytes(setup, monkeypatch):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)
    with monkeypatch.context() as patch:
        patch.setattr(
            apply.fileedit,
            "save_bytes",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated crash")),
        )
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert (folder / "RULES.md").read_text() == "Operator notes.\n"
    assert run(entity, bound_result)["state"] == "applied"
    assert "Check https://example.com/health." in (folder / "RULES.md").read_text()


def test_a_crash_before_the_candidate_pin_is_dropped_does_not_wedge_the_retry(setup, monkeypatch):
    """The published file keeps a second link in the private entry until the pin goes."""
    folder, _ = setup
    entity, bound_result = bound(setup)
    real_unlink = os.unlink
    failed = []

    def unlink(name, *a, **kw):
        if name == "candidate" and not failed:
            failed.append(name)
            raise OSError("simulated crash before the pin was dropped")
        return real_unlink(name, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert failed and (folder / "RULES.md").stat().st_nlink == 2
    assert run(entity, bound_result)["state"] == "applied"
    assert (folder / "RULES.md").stat().st_nlink == 1
    assert (folder / "docs" / "playbook.md").exists()


def test_a_pin_release_never_touches_a_different_inode_under_the_entry_name(setup, monkeypatch):
    entity, bound_result = bound(setup)
    real_unlink = os.unlink
    failed = []

    def unlink(name, *a, **kw):
        if name == "candidate" and not failed:
            failed.append(name)
            raise OSError("simulated crash before the pin was dropped")
        return real_unlink(name, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    effect = record(entity.id)["apply_operation"]["effects"]["RULES.md"]
    pin = Path(fileedit.recovery_dir()) / apply.ENTRIES / effect["entry"] / "candidate"
    pin.rename(pin.with_name("moved"))  # the second link survives under another name
    pin.write_text("someone else's file")
    with pytest.raises(store.StoreError):
        run(entity, bound_result)  # the recorded inode is not at `candidate`: nothing is deleted
    assert pin.read_text() == "someone else's file"
    assert pin.with_name("moved").exists()


EXPECTED_RULES = "Check https://example.com/health.\n"


def test_a_claimant_with_the_exact_bytes_before_the_effect_is_never_adopted(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = apply.replay_plan.resume

    def resume_then_claim(*a, **kw):
        plan = real(*a, **kw)
        (folder / "RULES.md").write_text(EXPECTED_RULES)  # another writer, identical bytes
        return plan

    with monkeypatch.context() as patch:
        patch.setattr(apply.replay_plan, "resume", resume_then_claim)
        with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
            run(entity, bound_result)
    assert record(entity.id)["files"] == {}
    assert (folder / "RULES.md").read_text() == EXPECTED_RULES


def test_a_claimant_during_an_interruption_is_not_adopted_by_the_retry(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    with monkeypatch.context() as patch:
        patch.setattr(
            material_write,
            "create",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated crash")),
        )
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    (folder / "RULES.md").write_text(EXPECTED_RULES)
    with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
        run(entity, bound_result)
    assert record(entity.id)["files"] == {}


def test_parent_creation_never_lands_in_a_displaced_project_folder(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    moved = folder.with_name("moved-away")
    real_mkdir = os.mkdir

    def displace_then_mkdir(name, *a, **kw):
        if str(name).startswith(".battlelab-dir-") and not moved.exists():
            folder.rename(moved)
        return real_mkdir(name, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", displace_then_mkdir)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert moved.exists() and not (moved / "docs").exists()
    assert not [p for p in moved.iterdir() if p.name.startswith(".battlelab-dir-")]


def test_a_same_byte_claimant_after_the_create_is_refused_at_settlement(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = apply._settle

    def replace_then_settle(folder_, changes, effects, previous):
        staged = folder / "claimant.tmp"
        staged.write_text(EXPECTED_RULES)
        staged.replace(folder / "RULES.md")  # a new inode, identical bytes
        return real(folder_, changes, effects, previous)

    with monkeypatch.context() as patch:
        patch.setattr(apply, "_settle", replace_then_settle)
        with pytest.raises(store.Conflict, match="replaced before the apply record settled"):
            run(entity, bound_result)
    assert record(entity.id)["files"] == {}


def test_an_ambiguous_interrupted_replace_fails_closed_and_stays_interrupted(setup, monkeypatch):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)

    def foreign_write_then_fail(path, content, *a, **kw):
        staged = folder / "claimant.tmp"
        staged.write_bytes(content)
        staged.replace(path)  # another writer lands the exact proposed bytes
        raise OSError("simulated failure of our own save")

    with monkeypatch.context() as patch:
        patch.setattr(apply.fileedit, "save_bytes", foreign_write_then_fail)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
        run(entity, bound_result)
    # Restoring the reviewed BYTES into the claimant's inode is still not the reviewed file.
    (folder / "RULES.md").write_text("Operator notes.\n")
    with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
        run(entity, bound_result)
    assert record(entity.id)["files"] == {}
    assert apply.status(entity.id)["state"] == "interrupted"


def test_a_failure_after_mkdir_in_a_displaced_parent_withdraws_the_directory(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    moved = folder.with_name("moved-away")
    real_mkdir, real_fsync = os.mkdir, os.fsync
    armed = []

    def mkdir(name, *a, **kw):
        result = real_mkdir(name, *a, **kw)
        if str(name).startswith(".battlelab-dir-"):
            armed.append(True)
        return result

    def fsync(fd):
        if armed and not moved.exists():
            folder.rename(moved)
            raise OSError("simulated sync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", mkdir)
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert moved.exists() and not (moved / "docs").exists()
    assert not [p for p in moved.iterdir() if p.name.startswith(".battlelab-dir-")]


def test_a_claimant_after_settlement_is_ownership_loss_at_the_next_review(setup, monkeypatch):
    """Settlement and the record write cannot be atomic; the recorded inode carries the proof."""
    folder, _ = setup
    entity, bound_result = bound(setup)
    real = apply._ownership

    def claim_then_record(*a, **kw):
        staged = folder / "claimant.tmp"
        staged.write_text(EXPECTED_RULES)
        staged.replace(folder / "RULES.md")  # new inode, identical bytes, after `_settle`
        return real(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply, "_ownership", claim_then_record)
        run(entity, bound_result)
    owned = record(entity.id)["files"]["RULES.md"]
    assert owned["inode"] != [
        os.stat(folder / "RULES.md").st_dev,
        os.stat(folder / "RULES.md").st_ino,
    ]
    stored = record(entity.id)["inputs"]
    later = review.build("review-demo", stored, key=KEY)
    assert any(
        c["path"] == "RULES.md" and "operator edits" in c["reason"]
        for c in later.public["conflicts"]
    )


def test_an_unpinned_staging_directory_is_left_alone_and_nothing_is_published(setup, monkeypatch):
    """Without a pin there is no proof which directory is ours: never delete, never publish."""
    folder, _ = setup
    entity, bound_result = bound(setup)
    moved = folder.with_name("moved-away")
    real_open = apply.destination._open

    def displace_then_fail(name, *a, **kw):
        if str(name).startswith(".battlelab-dir-") and not moved.exists():
            folder.rename(moved)
            raise OSError("simulated failure right after mkdir")
        return real_open(name, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply.destination, "_open", displace_then_fail)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert moved.exists() and not (moved / "docs").exists()
    assert record(entity.id)["apply_operation"]["directories"].get("docs") is None


def test_rollback_never_deletes_a_directory_swapped_in_at_the_staging_name(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    real_open = apply.destination._open
    foreign = []

    def swap_then_fail(name, *a, **kw):
        if str(name).startswith(".battlelab-dir-") and not foreign:
            (folder / name).rename(folder / "ours-moved-aside")
            (folder / name).mkdir()  # another writer's empty directory at the known name
            foreign.append(folder / name)
            raise OSError("simulated pin failure")
        return real_open(name, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply.destination, "_open", swap_then_fail)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    assert foreign and foreign[0].is_dir()  # the claimant is untouched
    assert not (folder / "docs").exists()


def _kept(identity=(1, 7, 0, 0)):
    from agent_sessions.playbooks import destination, materials, mutation_plan

    node = destination.Node("file", identity, data=EXPECTED_RULES.encode())
    owned = {
        "kind": "file",
        "disposition": "managed",
        "digest": materials.digest(node.data),
    }
    return mutation_plan.Change("RULES.md", "keep", node, node, owned)


def test_adoption_and_every_kept_managed_file_pin_an_inode_proof():
    change = _kept()
    # Reference adoption / a legacy unpinned record: the REVIEWED inode becomes the proof.
    assert apply._proof(change, {}, {}) == [1, 7]
    # A previously recorded proof is carried forward unchanged.
    assert apply._proof(change, {}, {"RULES.md": {"inode": [1, 9]}}) == [1, 9]
    with pytest.raises(store.Conflict, match="no reviewed identity"):
        apply._proof(_kept(identity=()), {}, {})


def test_an_adopted_file_replaced_with_the_same_bytes_cannot_be_removed():
    from agent_sessions.playbooks import destination, mutation_plan

    change = _kept()
    owned = {**change.ownership, "inode": apply._proof(change, {}, {})}
    substitute = destination.Node("file", (1, 8, 0, 0), data=EXPECTED_RULES.encode())
    changes, conflicts = mutation_plan.build(
        [], {"RULES.md": substitute}, {"RULES.md": owned}, "d-x"
    )
    assert changes == [] and "operator edits" in conflicts[0]["reason"]


def _swap_same_bytes(path):
    staged = path.with_name("claimant.tmp")
    staged.write_bytes(path.read_bytes())
    staged.replace(path)  # identical bytes, a different inode


def test_a_same_byte_swap_of_the_reviewed_file_is_never_written(setup, monkeypatch):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)
    real = apply.replay_plan.resume

    def resume_then_swap(*a, **kw):
        plan = real(*a, **kw)
        _swap_same_bytes(folder / "RULES.md")
        return plan

    with monkeypatch.context() as patch:
        patch.setattr(apply.replay_plan, "resume", resume_then_swap)
        with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
            run(entity, bound_result)
    assert (folder / "RULES.md").read_text() == "Operator notes.\n"


def test_a_swap_between_the_check_and_the_guarded_save_is_refused_by_the_save(setup, monkeypatch):
    folder, _ = setup
    (folder / "RULES.md").write_text("Operator notes.\n")
    entity, bound_result = bound(setup)
    real = apply.fileedit.save_bytes

    def swap_then_save(path, *a, **kw):
        _swap_same_bytes(folder / "RULES.md")
        return real(path, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply.fileedit, "save_bytes", swap_then_save)
        with pytest.raises(store.StoreError, match="replaced since it was reviewed"):
            run(entity, bound_result)
    assert (folder / "RULES.md").read_text() == "Operator notes.\n"
    assert record(entity.id)["files"] == {}


def test_save_bytes_refuses_a_target_or_parent_other_than_the_reviewed_inodes(tmp_home):
    tmp_path = tmp_home / "project"
    tmp_path.mkdir()
    target = tmp_path / "RULES.md"
    target.write_bytes(b"old\n")
    st, parent = target.stat(), tmp_path.stat()
    expect = fileedit.hashlib.sha256(b"old\n").hexdigest()
    ok = ([st.st_dev, st.st_ino], [parent.st_dev, parent.st_ino])
    for identity in (
        ([st.st_dev, st.st_ino + 1], ok[1]),
        (ok[0], [parent.st_dev, parent.st_ino + 1]),
    ):
        with pytest.raises(fileedit.SaveRefused, match="replaced since it was reviewed"):
            fileedit.save_bytes(
                str(target),
                b"new\n",
                expect,
                root=str(tmp_path),
                admit=lambda _p: None,
                identity=identity,
            )
        assert target.read_bytes() == b"old\n"
    saved = fileedit.save_bytes(
        str(target), b"new\n", expect, root=str(tmp_path), admit=lambda _p: None, identity=ok
    )
    assert target.read_bytes() == b"new\n" and saved["inode"]


def test_a_same_byte_swap_of_an_owned_whole_file_is_never_replaced(setup, monkeypatch):
    folder, _ = setup
    entity, bound_result = bound(setup)
    run(entity, bound_result)  # RULES.md is now an owned whole file
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
    real = apply.replay_plan.resume

    def resume_then_swap(*a, **kw):
        result = real(*a, **kw)
        _swap_same_bytes(folder / "RULES.md")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(apply.replay_plan, "resume", resume_then_swap)
        with pytest.raises(store.Conflict, match="neither the reviewed nor the applied"):
            run(entity, rebound, op="apply-operation-two")
    assert (folder / "RULES.md").read_text() == EXPECTED_RULES
