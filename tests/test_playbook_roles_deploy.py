"""#1654: a project moves to a format 5 revision only once its journals settled; legacy records are
never reshaped; the fleet says why a first format 5 update is not batchable."""

import hashlib
import json

import pytest

from agent_sessions.playbooks import fleet, review, store
from test_playbook_apply import _interrupt_second_create, bound, run
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup


def _republish_format5() -> str:
    """Rewrite the seeded `review-demo` source as format 5 (same materials); its new revision."""
    new = files(version=5)
    new["flows/check.toml"] = new["flows/check.toml"].replace("format = 1\n", "format = 5\n", 1)
    source = store.local_root() / "review-demo"
    for relative, text in new.items():
        (source / relative).write_text(text)
    return store.tree_from_files(new).digest()


def test_an_interrupted_legacy_apply_fences_the_move_to_format5_then_recovers(setup, monkeypatch):
    folder, body = setup
    entity, bound_result = bound(setup)
    with monkeypatch.context() as patch:
        _interrupt_second_create(patch)
        with pytest.raises(store.StoreError):
            run(entity, bound_result)
    before = json.dumps(record(entity.id), sort_keys=True)
    revision = _republish_format5()
    moved = {**body, "project_id": entity.id, "revision": revision, "bindings": []}

    with pytest.raises(store.Conflict, match="pending playbook operation"):
        review.build("review-demo", moved, key=KEY)
    # The legacy record, its inputs and its journal are untouched by the refused review.
    assert json.dumps(record(entity.id), sort_keys=True) == before

    with pytest.raises(store.Conflict, match="playbook changed"):
        run(entity, bound_result)  # its reviewed revision is gone: recovery says so, writes nothing
    assert json.dumps(record(entity.id), sort_keys=True) == before

    # Revert to format 4 restores the exact bytes, so the reviewed revision is back: the SAME
    # operation settles, and only then does the project move to format 5.
    source = store.local_root() / "review-demo"
    for relative, text in files().items():
        (source / relative).write_text(text)
    assert run(entity, bound_result)["state"] == "applied"
    assert (folder / "docs" / "playbook.md").exists()
    revision = _republish_format5()
    plan = review.build("review-demo", {**moved, "revision": revision}, key=KEY)
    assert plan.public["assignments"] == []


def _source_hashes() -> dict:
    root = store.local_root() / "review-demo"
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_a_settled_legacy_deployment_reviews_format5_without_assignments(setup):
    folder, body = setup
    before = _source_hashes()
    entity, bound_result = bound(setup)
    run(entity, bound_result)
    assert record(entity.id)["apply_operation"]["state"] == "complete"
    # The read normaliser never writes: the legacy source is byte-identical after review, bind
    # and apply (#1654 "byte-hash unchanged after load, review and apply").
    assert _source_hashes() == before
    revision = _republish_format5()
    plan = review.build(
        "review-demo",
        {**body, "project_id": entity.id, "revision": revision, "bindings": []},
        key=KEY,
    )
    assert plan.public["playbook"]["format"] == 5 and plan.public["assignments"] == []


def test_the_fleet_says_why_a_first_format5_update_is_not_batchable(setup, monkeypatch):
    entity, bound_result = bound(setup)
    run(entity, bound_result)
    with monkeypatch.context() as m:
        m.setattr(fleet, "_role_format", lambda pid: True)
        listing = {
            "revision": "f" * 64,
            "projects": [
                {
                    "project_id": entity.id,
                    "deployment_id": bound_result["deployment_id"],
                    "state": "applied",
                    "update_available": True,
                }
            ],
        }
        m.setattr(fleet, "projects", lambda pid: listing)
        # Hermes 6955 #2: keyed on the DEPLOYED revision's format, not on stored assignments —
        # this legacy project recorded none, and is still not batched into its first format 5.
        legacy = record(entity.id)
        assert legacy["review_facts"]["format"] == 2 and not legacy["inputs"].get("assignments")
        m.setattr(fleet.state, "locked", _reader(legacy))
        rows = fleet.plan("review-demo", key=KEY)["projects"]
        assert rows == [
            {
                "project_id": entity.id,
                "deployment_id": bound_result["deployment_id"],
                "batchable": False,
                "reasons": [fleet._TO_ROLES],
            }
        ]
        # A baseline from before formats were recorded counts as legacy too.
        old = record(entity.id)
        old["review_facts"].pop("format")
        m.setattr(fleet.state, "locked", _reader(old))
        assert fleet.plan("review-demo", key=KEY)["projects"][0]["reasons"] == [fleet._TO_ROLES]
        # Once the project itself moved to format 5, the rule no longer applies.
        moved = record(entity.id)
        moved["review_facts"]["format"] = 5
        m.setattr(fleet.state, "locked", _reader(moved))
        assert fleet.plan("review-demo", key=KEY)["projects"][0]["reasons"] != [fleet._TO_ROLES]


def _reader(value):
    from contextlib import contextmanager

    class _Locked:
        def read(self):
            return value

    @contextmanager
    def locked(pid, **kw):
        yield _Locked()

    return locked
