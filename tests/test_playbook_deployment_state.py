"""Deployment records serialize effects and make authoring deletion fail closed."""

import json
import os
import threading

import pytest

from agent_sessions.playbooks import deployment_state as state
from agent_sessions.playbooks import deployments, store


def record(pid="p-first"):
    return {"version": 1, "project_id": pid, "playbook_id": "state-demo", "state": "bound"}


def test_record_is_private_durable_and_seen_by_authoring_registry(tmp_home):
    with state.locked("p-first", create=True) as locked:
        assert locked.read() is None
        locked.write(record())
        assert locked.read() == record()
    path = store.local_root() / state.DIRECTORY / "p-first" / state.RECORD
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == [{"project_id": "p-first"}]


def test_project_locks_serialize_one_project_but_do_not_block_another(tmp_home, monkeypatch):
    monkeypatch.setattr(store, "LOCK_WAIT_S", 0.05)
    errors = []
    with state.locked("p-first", create=True):

        def other():
            try:
                with state.locked("p-first", create=True):
                    errors.append("acquired")
            except store.Busy:
                errors.append("busy")
            with state.locked("p-second", create=True) as locked:
                locked.write(record("p-second"))

        worker = threading.Thread(target=other)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive() and errors == ["busy"]
    with state.locked("p-first") as locked:
        locked.write(record())


def test_authoring_delete_cannot_cross_a_deployment_commit(tmp_home):
    started, finished = threading.Event(), threading.Event()
    seen = []

    def authoring():
        started.set()
        with store.root_lock(exclusive=True):
            seen.extend(state.Registry().projects_running("state-demo"))
        finished.set()

    with state.locked("p-first", create=True) as locked:
        worker = threading.Thread(target=authoring)
        worker.start()
        assert started.wait(timeout=3)
        assert not finished.wait(timeout=0.1)
        locked.write(record())
    assert finished.wait(timeout=3)
    worker.join(timeout=3)
    assert seen == [{"project_id": "p-first"}]


@pytest.mark.parametrize(
    "payload",
    [
        b"{bad",
        b"[]",
        b'{"version":2}',
        b'{"version":true}',
        b'{"version":1,"project_id":"p-other"}',
    ],
)
def test_damaged_or_newer_records_never_mean_no_deployment(tmp_home, payload):
    with state.locked("p-first", create=True) as locked:
        path = f"/proc/self/fd/{locked.project_fd}/{state.RECORD}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.close(fd)
        with pytest.raises(store.StoreError):
            locked.read()
    previous = deployments.set_registry(state.Registry())
    try:
        with pytest.raises(deployments.DeploymentsUnavailable):
            deployments.projects_running("state-demo")
    finally:
        deployments.set_registry(previous)


def test_record_symlink_never_reads_its_target(tmp_home, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text(json.dumps(record()))
    with state.locked("p-first", create=True) as locked:
        os.symlink(outside, state.RECORD, dir_fd=locked.project_fd)
        with pytest.raises(OSError):
            locked.read()


def test_removed_record_remains_a_receipt_but_does_not_hold_playbook(tmp_home):
    with state.locked("p-first", create=True) as locked:
        locked.write({**record(), "state": "removed"})
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == []
    with state.locked("p-first") as locked:
        assert locked.read()["state"] == "removed"


def test_failed_directory_sync_is_reported_even_when_record_was_published(tmp_home, monkeypatch):
    with state.locked("p-first", create=True) as locked:
        locked.write(record())

        def refuse(_):
            raise OSError("injected directory sync failure")

        monkeypatch.setattr(state.atomicjson, "fsync_dir", refuse)
        with pytest.raises(OSError, match="sync failure"):
            locked.write({**record(), "state": "applied"})
        assert locked.read()["state"] == "applied"


@pytest.mark.parametrize(
    "extra",
    [
        {"binding_operation": {"state": "intent"}},
        {"apply_operation": {"state": "intent"}},
        {"binding_operation": "damaged"},
    ],
)
def test_a_removed_record_with_an_unsettled_operation_still_holds_its_playbook(tmp_home, extra):
    with state.locked("p-first", create=True) as locked:
        locked.write({**record(), "state": "removed", **extra})
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == [{"project_id": "p-first"}]
    assert state.active("p-first") is not None


def test_an_unknown_record_state_is_damage_not_removal(tmp_home):
    with state.locked("p-first", create=True) as locked:
        with pytest.raises(store.StoreError, match="invalid"):
            locked.write({**record(), "state": "gone"})
        path = f"/proc/self/fd/{locked.project_fd}/{state.RECORD}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, json.dumps({**record(), "state": "gone"}).encode())
        os.close(fd)
        with pytest.raises(store.StoreError, match="damaged"):
            locked.read()


def _complete_binding(pid="p-first"):
    digest = "a" * 64
    return {
        "id": "binding-op-1",
        "request_digest": "b" * 64,
        "state": "complete",
        "before": {},
        "after": {},
        "apply_digest": digest,
        "result": {
            "project_id": pid,
            "deployment_id": "d-one",
            "operation_id": "binding-op-1",
            "state": "bound",
            "digest": digest,
            "receipt": "signed",
        },
    }


@pytest.mark.parametrize(
    "journal",
    [
        {"state": "complete"},
        {**_complete_binding(), "id": "Bad Id"},
        {**_complete_binding(), "apply_digest": "not-a-digest"},
        {**_complete_binding(), "result": {**_complete_binding()["result"], "project_id": "p-x"}},
        {**_complete_binding(), "extra": True},
        {**_complete_binding(), "before": [], "after": 42},
    ],
)
def test_a_malformed_complete_journal_keeps_a_removed_deployment_holding(tmp_home, journal):
    with state.locked("p-first", create=True) as locked:
        locked.write({**record(), "id": "d-one", "state": "removed", "binding_operation": journal})
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == [{"project_id": "p-first"}]
    assert state.active("p-first") is not None


def test_even_a_complete_journal_keeps_a_removed_deployment_holding(tmp_home):
    # Only a removal that settled and dropped its journals releases; a marker is not proof.
    removed = {
        **record(),
        "id": "d-one",
        "state": "removed",
        "binding_operation": _complete_binding(),
    }
    with state.locked("p-first", create=True) as locked:
        locked.write(removed)
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == [{"project_id": "p-first"}]
    assert state.active("p-first") is not None
    with state.locked("p-first") as locked:
        locked.write({k: v for k, v in removed.items() if k != "binding_operation"})
    with store.root_lock(exclusive=True):
        assert state.Registry().projects_running("state-demo") == []
    assert state.active("p-first") is None
