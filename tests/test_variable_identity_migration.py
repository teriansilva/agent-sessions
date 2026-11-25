"""A failed upgrade leaves values intact; retired identities never become another value."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from agent_sessions import template_secrets, variable_document
from agent_sessions import template_vars as store

SECRET = "migration-secret-123"


def write_legacy(version):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"name": "host", "value": "one.test", "created_at": 1, "updated_at": 1}]
    if version >= 2:
        rows[0]["kind"] = "text"
        env = (
            template_secrets.encrypt_scoped("global", None, "token", SECRET)
            if version == 3
            else template_secrets.encrypt("token", SECRET)
        )
        rows.append(
            {"name": "token", "kind": "secret", "secret": env, "created_at": 1, "updated_at": 1}
        )
    if version == 3:
        for row in rows:
            row.update(scope="global", project_id=None)
        for pid in ("project-a", "project-b"):
            rows.append(
                {
                    "name": "host",
                    "kind": "text",
                    "value": pid,
                    "scope": "project",
                    "project_id": pid,
                    "created_at": 1,
                    "updated_at": 1,
                }
            )
        rows.append(
            {
                "name": "token",
                "kind": "secret",
                "ref": "global",
                "scope": "project",
                "project_id": "project-a",
                "created_at": 1,
                "updated_at": 1,
            }
        )
    raw = json.dumps({"version": version, "variables": rows}).encode()
    path.write_bytes(raw)
    return raw, rows


def doc():
    return json.loads(store.store_path().read_text())


def by_name(document, name, pid=None):
    return next(r for r in document["variables"] if r["name"] == name and r["project_id"] == pid)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_upgrade_keeps_exact_backup_and_ciphertext_with_complete_identity_ledger(tmp_home, version):
    original, old_rows = write_legacy(version)
    store.create_variable({"name": "new", "value": "added"})
    upgraded = doc()
    assert upgraded["version"] == 4
    backups = list(store.store_path().parent.glob("*.pre-v4-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert len(upgraded["identities"]) == len(old_rows) + 1
    ids = {r["id"] for r in upgraded["variables"]}
    assert len(ids) == len(old_rows) + 1
    assert all(variable_document.validate_id(vid) for vid in ids)
    for row in old_rows:
        current = by_name(upgraded, row["name"], row.get("project_id"))
        assert current["definition"]["value_type"] == "text"
        assert current["created_at"] == current["updated_at"] == 1
        if "secret" in row:
            assert current["secret"] == (
                row["secret"] if version == 3 else {**row["secret"], "aad": "legacy"}
            )
    if version >= 2:
        assert store.secret_values()["token"] == SECRET
        assert SECRET in store.secret_values_checked()
    if version == 3:
        assert store.resolver("project-a").text("host")["value"] == "project-a"
        assert store.resolver("project-b").text("host")["value"] == "project-b"
        assert store.resolver("project-a").secret("token") == SECRET
        with pytest.raises(store.BindingMissing):
            store.resolver("project-b").secret("token")


def test_value_edits_and_same_kind_project_rebinding_preserve_ids(tmp_home):
    store.create_variable({"name": "host", "value": "first"})
    store.bind_project("project-a", [{"name": "host", "value": "project"}])
    before = doc()
    store.update_variable("host", {"value": "second"}, by_name(before, "host")["updated_at"])
    store.bind_project("project-a", [{"name": "host", "ref": "global"}])
    after = doc()
    assert {r["id"] for r in before["variables"]} == {r["id"] for r in after["variables"]}
    assert before["identities"] == after["identities"]


def test_delete_recreate_retires_old_id_instead_of_reusing_it(tmp_home):
    v = store.create_variable({"name": "host", "value": "first"})
    old_id = by_name(doc(), "host")["id"]
    store.delete_variable("host", v["updated_at"])
    assert doc()["identities"][0]["id"] == old_id
    store.create_variable({"name": "host", "value": "second"})
    assert by_name(doc(), "host")["id"] != old_id
    assert len(doc()["identities"]) == 2


def test_project_kind_change_retires_the_prior_identity(tmp_home):
    store.bind_project("project-a", [{"name": "host", "value": "first"}])
    old_id = by_name(doc(), "host", "project-a")["id"]
    store.bind_project("project-a", [{"name": "host", "kind": "secret", "value": SECRET}])
    assert by_name(doc(), "host", "project-a")["id"] != old_id
    assert len(doc()["identities"]) == 2


def test_refused_write_does_not_upgrade_or_create_backup(tmp_home):
    original, _ = write_legacy(3)
    with pytest.raises(store.VariableConflict):
        store.update_variable("host", {"value": "second"}, 9)
    assert store.store_path().read_bytes() == original
    assert not list(store.store_path().parent.glob("*.pre-v4-*"))


@pytest.mark.parametrize("failure", ["backup", "publish"])
def test_interrupted_upgrade_keeps_old_store_and_retry_is_complete(tmp_home, monkeypatch, failure):
    original, _ = write_legacy(3)
    target = variable_document if failure == "backup" else store
    method = "retain_upgrade_backup" if failure == "backup" else "atomic_write_json"
    real = getattr(target, method)
    monkeypatch.setattr(target, method, Mock(side_effect=OSError("injected disk failure")))
    with pytest.raises(OSError):
        store.create_variable({"name": "new", "value": "added"})
    assert store.store_path().read_bytes() == original
    monkeypatch.setattr(target, method, real)
    store.create_variable({"name": "new", "value": "added"})
    assert doc()["version"] == 4
    assert len(list(store.store_path().parent.glob("*.pre-v4-*"))) == 1
    variable_document.validate(doc(), store._read(store.store_path())[0])


@pytest.mark.parametrize(
    "damage", ["missing-ledger", "missing-entry", "wrong-scope", "duplicate-id", "unknown-metadata"]
)
def test_orphaned_or_ambiguous_identity_refuses_use_and_write(tmp_home, damage):
    v = store.create_variable({"name": "host", "value": "first"})
    broken = doc()
    if damage == "missing-ledger":
        del broken["identities"]
    elif damage == "missing-entry":
        broken["identities"] = []
    elif damage == "wrong-scope":
        broken["identities"][0].update(scope="project", project_id="project-a")
    elif damage == "duplicate-id":
        broken["identities"].append(broken["identities"][0])
    else:
        broken["unknown"] = True
    store.store_path().write_text(json.dumps(broken))
    original = store.store_path().read_bytes()
    with pytest.raises(store.ResolutionUnavailable):
        store.resolver("project-a").text("host")
    for resolve in (store.values, store.secret_state, store.secret_values, store.revisions):
        with pytest.raises(store.ResolutionUnavailable):
            resolve()
    with pytest.raises(store.VariableError, match="nothing was written"):
        store.update_variable("host", {"value": "second"}, v["updated_at"])
    assert store.store_path().read_bytes() == original


@pytest.mark.parametrize("version", [None, False, True, 0, -1, 1.0, "3", [], {}, 5])
@pytest.mark.parametrize("action", ["bind", "remove"])
def test_reference_repair_never_accepts_an_invalid_store_version(tmp_home, version, action):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"version": version, "variables": []}).encode()
    path.write_bytes(original)
    with pytest.raises((store.VariableError, store.VariableStoreUnsupported)):
        if action == "bind":
            store.bind_project("project-a", [{"name": "host", "value": "own"}])
        else:
            store.remove_project_bindings("project-a")
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.pre-v4-*"))


def test_old_build_refuses_v4_reads_writes_and_redaction(tmp_home, monkeypatch):
    store.create_variable({"name": "host", "value": "first"})
    original = store.store_path().read_bytes()
    monkeypatch.setattr(store, "STORE_VERSION", 3)
    assert store.list_variables() == []
    with pytest.raises(store.VariableStoreUnsupported):
        store.create_variable({"name": "new", "value": "second"})
    with pytest.raises(store.VariableStoreUnsupported):
        store.secret_values_checked()
    assert store.store_path().read_bytes() == original


def test_damaged_legacy_inventory_is_not_partially_migrated(tmp_home):
    original, _ = write_legacy(3)
    broken = json.loads(original)
    broken["variables"].append({"name": "unreadable"})
    store.store_path().write_text(json.dumps(broken))
    original = store.store_path().read_bytes()
    with pytest.raises(store.VariableError, match="repair it"):
        store.create_variable({"name": "new", "value": "added"})
    assert store.store_path().read_bytes() == original
    assert not list(store.store_path().parent.glob("*.pre-v4-*"))


def _upgrade_writer(name, ready, release, result):
    from agent_sessions import variables

    ready.set()
    if not release.wait(15):
        raise RuntimeError("upgrade race was not released")
    row = variables.create({"name": name, "value": name})
    result.put(row["id"])


def test_two_processes_upgrade_once_without_lost_values_or_changed_ids(tmp_home):
    import multiprocessing

    original, _ = write_legacy(3)
    context = multiprocessing.get_context("spawn")
    ready = [context.Event(), context.Event()]
    release, result = context.Event(), context.Queue()
    workers = [
        context.Process(target=_upgrade_writer, args=(name, ready[i], release, result))
        for i, name in enumerate(("first", "second"))
    ]
    try:
        for worker in workers:
            worker.start()
        assert all(event.wait(10) for event in ready)
        release.set()
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        written = {result.get(timeout=1), result.get(timeout=1)}
        assert written == {by_name(doc(), name)["id"] for name in ("first", "second")}
        before = {r["name"]: r["id"] for r in doc()["variables"] if r["scope"] == "global"}
        from agent_sessions import variables

        variables.migrate()
        assert before == {r["name"]: r["id"] for r in doc()["variables"] if r["scope"] == "global"}
        [backup] = list(store.store_path().parent.glob("*.pre-v4-*"))
        assert backup.read_bytes() == original
    finally:
        release.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def test_backup_directory_flush_failure_prevents_publication_and_keeps_retryable_backup(
    tmp_home, monkeypatch
):
    original, _ = write_legacy(3)
    with monkeypatch.context() as patch:
        patch.setattr(
            variable_document, "fsync_dir", Mock(side_effect=OSError("injected fsync failure"))
        )
        with pytest.raises(OSError):
            store.create_variable({"name": "new", "value": "added"})
    assert store.store_path().read_bytes() == original
    [backup] = list(store.store_path().parent.glob("*.pre-v4-*"))
    assert backup.read_bytes() == original
    store.create_variable({"name": "new", "value": "added"})
    assert len(list(store.store_path().parent.glob("*.pre-v4-*"))) == 1
