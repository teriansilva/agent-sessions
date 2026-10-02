"""Actual staged archives, durable retries and fail-closed activation (#1259)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

import test_plugin_artifacts
import test_plugin_feed
from agent_sessions.plugins import LoadResult, load_first_party, manager, provenance, storage

signer = test_plugin_feed.signer


def request_id():
    return str(uuid.uuid4())


@pytest.fixture
def recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))
    body = b"#!/bin/true\n"
    data = test_plugin_artifacts.archive([("bin/fixture", body, tarfile.REGTYPE)])
    entry = test_plugin_feed.entry()
    entry["manifest"]["install"].update(
        kind="tarball",
        authority="github.com",
        package="@example/fixture",
        version="v1",
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
        entrypoint="bin/fixture",
    )
    entry["recipe"]["artifacts"] = [
        {
            "url": test_plugin_artifacts.URL,
            "sha256": hashlib.sha256(data).hexdigest(),
            "destination": ".",
        }
    ]
    monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: data)
    return entry


def planned(recipe):
    review = manager.review(local=recipe)
    return manager.begin_install(request_id(), review["id"], review["digest"], confirm_local=True)


def installed(recipe):
    item = manager.run_install(planned(recipe)["id"])
    assert item["state"] == "installed", item
    return item["id"]


def test_incompatible_platform_refuses_before_any_artifact_work(recipe, monkeypatch):
    import platform

    recipe["manifest"]["install"]["platform"] = "linux-x64"
    item = planned(recipe)
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: pytest.fail("downloaded wrong arch"))
    assert manager.run_install(item["id"])["state"] == "failed"
    assert not manager.generation_root("fixture", item["id"]).exists()


def test_copied_generation_cannot_run_or_activate_on_an_incompatible_host(recipe, monkeypatch):
    import platform

    recipe["manifest"]["install"]["platform"] = "linux-x64"
    generation = verified(recipe)
    row = manager.snapshot()["plugins"]["fixture"]["generations"][generation]
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    with pytest.raises(manager.ManagerError, match="requires linux-x64"):
        manager.provider("fixture", row)
    with pytest.raises(manager.ManagerError, match="requires linux-x64"):
        manager.activate(request_id(), "fixture", generation)


def verified(recipe):
    generation = installed(recipe)
    row = manager.snapshot()["plugins"]["fixture"]
    prov = manager.provider("fixture", row["generations"][generation])
    results = [{"check": c, "passed": True} for c in manager.required_checks(prov)]
    if prov.manifest.runtime == "chat":
        results[0]["binding"] = manager._endpoint_binding("fixture", generation)
    manager.record_verification("fixture", generation, results)
    return generation


def test_local_confirmation_is_fresh_exact_and_one_use(recipe):
    review = manager.review(local=recipe)
    for digest, confirm in [(review["digest"], False), ("0" * 64, True)]:
        with pytest.raises(manager.ManagerError):
            manager.begin_install(request_id(), review["id"], digest, confirm_local=confirm)
    rid = request_id()
    first = manager.begin_install(rid, review["id"], review["digest"], confirm_local=True)
    assert manager.begin_install(rid, review["id"], review["digest"], confirm_local=True) == first
    with pytest.raises(manager.ManagerError, match="different request"):
        manager.begin_install(rid, review["id"], review["digest"], confirm_local=False)
    with pytest.raises(manager.ManagerError, match="expired or was used"):
        manager.begin_install(request_id(), review["id"], review["digest"], confirm_local=True)


def test_expired_review_never_starts_work(recipe, monkeypatch):
    review = manager.review(local=recipe)
    monkeypatch.setattr(manager, "_now", lambda: review["expires_at"])
    with pytest.raises(manager.ManagerError, match="expired"):
        manager.begin_install(request_id(), review["id"], review["digest"], confirm_local=True)
    assert manager.snapshot()["operations"] == {}


def test_install_stages_real_bytes_without_enabling_and_repeat_never_fetches(recipe, monkeypatch):
    gen = installed(recipe)
    row = manager.snapshot()["plugins"]["fixture"]
    assert row["active"] is None and row["enabled"] is None and row["candidate"] == gen
    prov = manager.provider("fixture", row["generations"][gen])
    assert Path(prov.entrypoint_path()).read_bytes() == b"#!/bin/true\n"
    assert prov.entrypoint().state == provenance.MANAGED
    monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: pytest.fail("repeated fetch"))
    assert manager.run_install(gen)["state"] == "installed"
    assert "fixture" not in manager.overlay(LoadResult()).providers


def test_unverified_partial_or_failed_checks_cannot_enable(recipe):
    gen = installed(recipe)
    with pytest.raises(manager.ManagerError, match="verification"):
        manager.activate(request_id(), "fixture", gen)
    with pytest.raises(manager.ManagerError, match="required checks"):
        manager.record_verification("fixture", gen, [{"check": "binary", "passed": True}])
    prov = manager.provider("fixture", manager.snapshot()["plugins"]["fixture"]["generations"][gen])
    manager.record_verification(
        "fixture", gen, [{"check": c, "passed": False} for c in manager.required_checks(prov)]
    )
    with pytest.raises(manager.ManagerError, match="verification"):
        manager.activate(request_id(), "fixture", gen)


def test_activation_response_and_pointer_are_one_commit_and_repeated_request_is_safe(recipe):
    gen = verified(recipe)
    rid = request_id()
    first = manager.activate(rid, "fixture", gen)
    assert manager.activate(rid, "fixture", gen) == first
    state = manager.snapshot()
    assert state["plugins"]["fixture"]["active"] == gen
    assert state["operations"][rid] == first
    live = manager.overlay(LoadResult()).providers["fixture"]
    assert live.root == manager.generation_root("fixture", gen)
    with pytest.raises(manager.ManagerError, match="different request"):
        manager.deactivate(rid, "fixture")


def fill_operation_history():
    with storage.locked(manager.DOCUMENT) as path:
        doc = manager.snapshot()
        template = next(o for o in doc["operations"].values() if o["state"] == "complete")
        while len(doc["operations"]) < manager.MAX_OPERATIONS:
            rid = request_id()
            doc["operations"][rid] = {**copy.deepcopy(template), "id": rid}
        storage.write(path, doc)
    return manager.snapshot()


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize("managed", [False, True])
def test_full_history_still_revokes_known_agents_without_losing_audit_or_retry(
    recipe, monkeypatch, managed, remove
):
    from agent_sessions.engines import registry

    if managed:

        class FixtureKind(registry.GeminiProvider):
            engine_id = "fixture"

        monkeypatch.setitem(
            registry.STORE_KINDS, recipe["manifest"]["store"]["layout"], FixtureKind
        )
        gen = verified(recipe)
        activation = manager.activate(request_id(), "fixture", gen)
        plugin_id = "fixture"
    else:
        gen = None
        manager.deactivate(request_id(), "retired-fixture")
        plugin_id = "claude"
    registry.reload()
    admitted = registry.live_provider(plugin_id)
    assert admitted is not None and registry.admits(admitted)
    before = fill_operation_history()
    revision = before["roster_revision"]
    rid = request_id()
    result = manager.deactivate(
        rid, plugin_id, remove=remove, expected_active=gen, expected_revision=revision
    )
    assert result["state"] == "complete"
    assert not registry.admits(admitted)
    after = manager.snapshot()
    assert after["plugins"][plugin_id]["enabled"] is False
    assert after["roster_revision"] == rid
    assert len(after["operations"]) == manager.MAX_OPERATIONS + 1
    assert {k: after["operations"][k] for k in before["operations"]} == before["operations"]
    assert (
        after["plugins"][plugin_id]["generations"]
        == before["plugins"].get(plugin_id, {"generations": {}})["generations"]
    )
    # Replay survives process restart and binds the original confirmation, even though the
    # current revision has advanced. It never creates another record or repeats a decision.
    probe = """
import json, sys
from agent_sessions.plugins import manager
rid, plugin_id, remove, gen, revision = json.loads(sys.argv[1])
print(json.dumps(manager.deactivate(rid, plugin_id, remove=remove,
    expected_active=gen, expected_revision=revision)))
"""
    replay = subprocess.run(
        [sys.executable, "-c", probe, json.dumps([rid, plugin_id, remove, gen, revision])],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(replay.stdout) == result
    assert manager.snapshot() == after
    with pytest.raises(manager.ManagerError, match="different request"):
        manager.deactivate(rid, plugin_id, remove=not remove)
    if managed:
        assert manager.activate(activation["id"], plugin_id, gen) == activation
        assert manager.snapshot() == after  # replay is not a way to re-enable at the cap
        with pytest.raises(manager.ManagerError, match="history is full"):
            manager.activate(request_id(), plugin_id, gen)
        for kind in ("signin", "verify"):
            with pytest.raises(manager.ManagerError, match="history is full"):
                manager.begin_action(request_id(), plugin_id, gen, kind, confirm_effects=True)
    for target in (plugin_id, "unknown-fixture"):
        with pytest.raises(manager.ManagerError, match="history is full"):
            manager.deactivate(request_id(), target, remove=remove)
    assert manager.snapshot() == after
    # One overflow revocation cannot consume another known agent's opportunity to revoke.
    manager.deactivate(request_id(), "shell", remove=remove, expected_revision=rid)
    assert manager.snapshot()["plugins"]["shell"]["enabled"] is False
    assert len(manager.snapshot()["operations"]) == manager.MAX_OPERATIONS + 2


@pytest.mark.parametrize("changed", ["generation", "revision"])
def test_full_history_revocation_still_requires_current_confirmation(recipe, changed):
    gen = verified(recipe)
    manager.activate(request_id(), "fixture", gen)
    before = fill_operation_history()
    with pytest.raises(manager.ManagerError, match="changed; refresh and confirm again"):
        manager.deactivate(
            request_id(),
            "fixture",
            remove=True,
            expected_active=request_id() if changed == "generation" else gen,
            expected_revision=request_id() if changed == "revision" else before["roster_revision"],
        )
    assert manager.snapshot() == before


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("full_count", [False, True])
def test_byte_full_history_revocation_stays_readable_and_durable(
    recipe, remove, managed, full_count
):
    if managed:
        gen = verified(recipe)
        manager.activate(request_id(), "fixture", gen)
        plugin_id = "fixture"
    else:
        gen = None
        manager.deactivate(request_id(), "retired-fixture")
        plugin_id = "claude"
    if full_count:
        fill_operation_history()
    with storage.locked(manager.DOCUMENT) as path:
        before = manager.snapshot()
        # Synthetic retained metadata puts an otherwise complete history exactly 64 bytes
        # below the actual 8 MiB write boundary, both before and at the operation-count cap.
        item = next(iter(before["operations"].values()))
        item["padding"] = ""
        size = len(json.dumps(before, indent=2, sort_keys=True).encode())
        item["padding"] = "x" * (storage.MAX_STATE_BYTES - size - 64)
        storage.write(path, before)
        assert path.stat().st_size == storage.MAX_STATE_BYTES - 64
    rid = request_id()
    revision = before["roster_revision"]
    result = manager.deactivate(
        rid, plugin_id, remove=remove, expected_active=gen, expected_revision=revision
    )
    assert result["state"] == "complete"
    after = manager.snapshot()
    assert path.stat().st_size > storage.MAX_STATE_BYTES
    assert path.stat().st_size - (storage.MAX_STATE_BYTES - 64) < 1024
    assert after["plugins"][plugin_id]["enabled"] is False
    assert after["roster_revision"] == rid
    assert {k: after["operations"][k] for k in before["operations"]} == before["operations"]
    assert (
        after["plugins"][plugin_id]["generations"]
        == before["plugins"].get(plugin_id, {"generations": {}})["generations"]
    )
    # A fresh process reads and replays the durable response above the ordinary write limit.
    probe = """
import json, sys
from agent_sessions.plugins import manager, load_first_party
rid, plugin_id, remove, gen, revision = json.loads(sys.argv[1])
assert manager.snapshot()['plugins'][plugin_id]['enabled'] is False
assert plugin_id not in manager.overlay(load_first_party()).providers
print(json.dumps(manager.deactivate(rid, plugin_id, remove=remove,
    expected_active=gen, expected_revision=revision)))
"""
    replay = subprocess.run(
        [sys.executable, "-c", probe, json.dumps([rid, plugin_id, remove, gen, revision])],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(replay.stdout) == result
    for target in (plugin_id, "unknown-fixture"):
        with pytest.raises(ValueError, match="full|too large"):
            manager.deactivate(request_id(), target, remove=remove)
    if managed:
        with pytest.raises(ValueError, match="full|too large"):
            manager.activate(request_id(), plugin_id, gen)
    with pytest.raises(storage.StateError, match="too large"):
        manager.review(local=recipe)
    assert manager.snapshot() == after
    manager.deactivate(request_id(), "shell", remove=remove, expected_revision=rid)
    final = manager.snapshot()
    assert final["plugins"]["shell"]["enabled"] is False
    assert len(final["operations"]) == len(before["operations"]) + 2


@pytest.mark.parametrize("remove", [False, True])
def test_byte_full_first_revocation_can_publish_its_revision_baseline(recipe, remove):
    from agent_sessions.engines import registry

    gen = installed(recipe)  # ordinary staging creates no roster revision or sentinel
    registry.reload()
    with storage.locked(manager.DOCUMENT) as path:
        before = manager.snapshot()
        assert "roster_revision" not in before
        assert not path.with_name(manager.REVISION_MARKER).exists()
        before["operations"][gen]["padding"] = ""
        size = len(json.dumps(before, indent=2, sort_keys=True).encode())
        before["operations"][gen]["padding"] = "x" * (storage.MAX_STATE_BYTES - size - 1)
        storage.write(path, before)
    rid = request_id()
    result = manager.deactivate(
        rid, "claude", remove=remove, expected_active=None, expected_revision=None
    )
    after = manager.snapshot()
    assert after["plugins"]["claude"]["enabled"] is False
    assert after["roster_revision"] == rid and after["operations"][rid] == result
    assert after["operations"][gen] == before["operations"][gen]
    assert after["plugins"]["fixture"] == before["plugins"]["fixture"]
    assert path.with_name(manager.REVISION_MARKER).exists()


def fill_state_bytes(operation_id):
    with storage.locked(manager.DOCUMENT) as path:
        doc = manager.snapshot()
        doc["operations"][operation_id]["padding"] = ""
        size = len(json.dumps(doc, indent=2, sort_keys=True).encode())
        doc["operations"][operation_id]["padding"] = "x" * (storage.MAX_STATE_BYTES - size - 1)
        storage.write(path, doc)
        assert path.stat().st_size == storage.MAX_STATE_BYTES - 1
    return doc


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize(
    "settle", ["expiry", "claim-expiry", "cancel", "recover", "failed", "cleanup"]
)
def test_byte_full_pending_outcome_cannot_strand_revocation(recipe, monkeypatch, remove, settle):
    from agent_sessions.engines import registry
    from agent_sessions.plugins import process

    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    gen = installed(recipe)
    pending = manager.begin_action(request_id(), "fixture", gen, "signin")
    if settle in {"recover", "failed", "cleanup"}:
        manager._set_operation(pending["id"], "running")
    registry.reload()
    before = fill_state_bytes(gen)
    monkeypatch.setattr(manager, "_now", lambda: pending["created_at"] + manager.REVIEW_SECONDS)
    if settle == "claim-expiry":
        with storage.locked("worker", wait=0):
            with pytest.raises(manager.ManagerError, match="sign-in expired"):
                manager.claim_signin(pending["id"])
    elif settle == "cancel":
        manager.cancel_ready(pending["id"])
    elif settle == "recover":
        monkeypatch.setattr(process, "stop_operation", lambda rid: rid == pending["id"])
        manager.recover()
    elif settle == "failed":
        with storage.locked("worker", wait=0):
            manager._set_operation(pending["id"], "failed", error="verification interrupted")
    elif settle == "cleanup":
        with storage.locked("worker", wait=0):
            manager._set_operation(
                pending["id"], "cleanup_pending", error="temporary process cleanup is pending"
            )
        blocked = manager.snapshot()
        with pytest.raises(manager.ManagerError, match="busy"):
            manager.deactivate(request_id(), "claude", remove=remove)
        monkeypatch.setattr(process, "stop_operation", lambda _rid: False)
        with pytest.raises(manager.ManagerError, match="cleanup is pending"):
            manager.recover()
        assert manager.snapshot() == blocked
        monkeypatch.setattr(process, "stop_operation", lambda rid: rid == pending["id"])
        manager.recover()
    rid = request_id()
    result = manager.deactivate(
        rid, "claude", remove=remove, expected_active=None, expected_revision=None
    )
    after = manager.snapshot()
    assert after["operations"][pending["id"]]["state"] in {"failed", "interrupted"}
    assert after["operations"][gen] == before["operations"][gen]
    assert after["plugins"]["fixture"] == before["plugins"]["fixture"]
    assert after["plugins"]["claude"]["enabled"] is False
    assert after["roster_revision"] == rid and after["operations"][rid] == result
    probe = """
import json, sys
from agent_sessions.plugins import manager
rid, pending, remove = json.loads(sys.argv[1])
assert manager.snapshot()['plugins']['claude']['enabled'] is False
assert manager.operation(pending)['state'] in {'failed', 'interrupted'}
print(json.dumps(manager.deactivate(rid, 'claude', remove=remove,
    expected_active=None, expected_revision=None)))
"""
    replay = subprocess.run(
        [sys.executable, "-c", probe, json.dumps([rid, pending["id"], remove])],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(replay.stdout) == result
    assert manager.snapshot() == after


@pytest.mark.parametrize("failure", ["candidate-too-large", "oversized-diagnostic"])
def test_byte_full_install_failure_is_durable_and_leaves_revocation_available(
    recipe, monkeypatch, failure
):
    from agent_sessions.engines import registry

    item = planned(recipe)
    registry.reload()
    before = fill_state_bytes(item["id"])
    if failure == "oversized-diagnostic":

        def refuse(*_args):
            raise manager.artifacts.ArtifactError("\u2600" * 1024)

        monkeypatch.setattr(manager.artifacts, "fetch", refuse)
    result = manager.run_install(item["id"])
    assert result["state"] == "failed"
    if failure == "oversized-diagnostic":
        assert result["error"] == "operation diagnostic exceeded the state limit"
    assert (
        len(
            json.dumps(
                {k: result[k] for k in ("state", "error", "updated_at")}, indent=2, sort_keys=True
            ).encode()
        )
        <= manager.MAX_OUTCOME_BYTES
    )
    assert result["review"] == before["operations"][item["id"]]["review"]
    assert result["padding"] == before["operations"][item["id"]]["padding"]
    manager.deactivate(request_id(), "claude")
    after = manager.snapshot()
    assert after["plugins"]["claude"]["enabled"] is False
    assert "fixture" not in after["plugins"]
    assert after["operations"][item["id"]] == result


def test_changed_executable_refuses_enable_and_verification(recipe):
    gen = verified(recipe)
    target = manager.generation_root("fixture", gen) / "bin/fixture"
    target.write_text("changed")
    with pytest.raises(provenance.ProvenanceError):
        manager.activate(request_id(), "fixture", gen)
    assert manager.snapshot()["plugins"]["fixture"]["active"] is None


def test_old_generation_stays_active_during_update_failure_and_rollback(recipe, monkeypatch):
    old = verified(recipe)
    manager.activate(request_id(), "fixture", old)
    old_provider = manager.overlay(LoadResult()).providers["fixture"]
    update = planned(recipe)
    monkeypatch.setattr(
        manager.artifacts, "fetch", lambda *_: (_ for _ in ()).throw(OSError("secret URL"))
    )
    failed = manager.run_install(update["id"])
    assert failed["state"] == "failed" and "secret" not in json.dumps(failed)
    assert manager.snapshot()["plugins"]["fixture"]["active"] == old
    assert old_provider.entrypoint_path() == str(
        manager.generation_root("fixture", old) / "bin/fixture"
    )
    manager.deactivate(request_id(), "fixture", remove=True)
    assert "fixture" not in manager.overlay(LoadResult()).providers
    assert old_provider.root.is_dir()
    manager.activate(request_id(), "fixture", old)
    assert (
        manager.overlay(LoadResult()).providers["fixture"].entrypoint_path()
        == old_provider.entrypoint_path()
    )


def test_managed_provider_ignores_legacy_override_and_keeps_its_generation_record(
    recipe, monkeypatch
):
    first = verified(recipe)
    manager.activate(request_id(), "fixture", first)
    old = manager.overlay(LoadResult()).providers["fixture"]
    second = verified(recipe)
    env_var = old.manifest.binary.env_var
    monkeypatch.setenv(env_var, "/bin/true")
    manager.activate(request_id(), "fixture", second)
    new = manager.overlay(LoadResult()).providers["fixture"]
    assert old.entrypoint_path() != new.entrypoint_path()
    assert str(old.root) in old.entrypoint_path() and str(new.root) in new.entrypoint_path()


@pytest.mark.parametrize(
    "boundary", ["planned", "staging", "candidate-before-commit", "candidate-after-commit"]
)
def test_crash_recovery_never_infers_success_or_replays(recipe, monkeypatch, boundary):
    old = verified(recipe)
    manager.activate(request_id(), "fixture", old)
    item = planned(recipe)
    original_write = storage.write
    original_extract = manager.artifacts.extract

    def crash_write(path, doc):
        if path.name == manager.DOCUMENT and doc["operations"][item["id"]]["state"] == "installed":
            if boundary == "candidate-after-commit":
                original_write(path, doc)
            raise SystemExit("simulated crash")
        original_write(path, doc)

    def crash_extract(*args, **kwargs):
        original_extract(*args, **kwargs)
        raise SystemExit("simulated crash")

    with monkeypatch.context() as m:
        if boundary == "staging":
            m.setattr(manager.artifacts, "extract", crash_extract)
        elif boundary.startswith("candidate-"):
            m.setattr(storage, "write", crash_write)
        if boundary != "planned":
            with pytest.raises(SystemExit):
                manager.run_install(item["id"])
    manager.recover()
    expected = "installed" if boundary == "candidate-after-commit" else "interrupted"
    assert manager.operation(item["id"])["state"] == expected
    assert manager.snapshot()["plugins"]["fixture"]["active"] == old
    monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: pytest.fail("replayed install"))
    assert manager.run_install(item["id"])["state"] == expected


def test_live_worker_cannot_be_recovered_or_raced_by_another_mutation(recipe, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    fetch = manager.artifacts.fetch

    def paused(*args):
        entered.set()
        assert finish.wait(10)
        return fetch(*args)

    monkeypatch.setattr(manager.artifacts, "fetch", paused)
    item = planned(recipe)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(manager.run_install, item["id"])
        try:
            assert entered.wait(10)
            assert manager.operation(item["id"])["state"] == "running"
            for action in (manager.recover, lambda: manager.deactivate(request_id(), "fixture")):
                with pytest.raises(storage.StateError, match="busy"):
                    action()
            with pytest.raises(manager.ManagerError, match="busy"):
                planned(recipe)
        finally:
            finish.set()
        assert future.result()["state"] == "installed"


def test_corrupt_row_disables_only_it_broken_document_never_revives_disabled_agent(recipe):
    loaded = load_first_party()
    original = set(loaded.providers)
    gen = verified(recipe)
    manager.activate(request_id(), "fixture", gen)
    path = storage.root() / manager.DOCUMENT
    with storage.locked(manager.DOCUMENT):
        doc = storage.read(path)
        doc["plugins"]["fixture"] = {"enabled": True}
        storage.write(path, doc)
    result = manager.overlay(loaded)
    assert set(result.providers) == original and "fixture" in result.problems
    path.write_text("broken")
    result = manager.overlay(load_first_party())
    assert not result.providers and "manager" in result.problems
    with pytest.raises(ValueError):
        manager.review(local=recipe)
    assert path.read_text() == "broken"


def test_legacy_records_are_not_rewritten_by_staging_or_disabled_rows(recipe):
    directory = storage.root()
    with storage.directory(directory):
        pass
    legacy = directory / "fixture.json"
    data = json.dumps({"confirmed_path": "/private/vendor", "confirmed_sha256": "a" * 64})
    legacy.write_text(data)
    legacy.chmod(0o600)
    installed(recipe)
    manager.deactivate(request_id(), "fixture", remove=True)
    assert legacy.read_text() == data


def test_aggregate_expansion_limit_is_checked_before_writing_the_archive(recipe, monkeypatch):
    monkeypatch.setattr(manager, "MAX_TOTAL_EXPANDED", 100)
    item = planned(recipe)
    failed = manager.run_install(item["id"])
    assert failed["state"] == "failed"
    assert not (manager.generation_root("fixture", item["id"]) / "bin/fixture").exists()


def test_adoption_requires_separate_exact_path_confirmation_and_never_downloads(
    recipe, tmp_path, monkeypatch
):
    vendor = tmp_path / "vendor" / "fixture"
    vendor.parent.mkdir()
    vendor.write_bytes(b"#!/bin/true\n")
    vendor.chmod(0o700)
    review = manager.review(local=recipe, adopted_path=str(vendor))
    with pytest.raises(manager.ManagerError, match="adopted path"):
        manager.begin_install(request_id(), review["id"], review["digest"], confirm_local=True)
    item = manager.begin_install(
        request_id(), review["id"], review["digest"], confirm_local=True, confirm_adopted=True
    )
    monkeypatch.setattr(
        manager.artifacts, "fetch", lambda *_: pytest.fail("adoption downloaded an artifact")
    )
    assert manager.run_install(item["id"])["state"] == "installed"
    gen = manager.snapshot()["plugins"]["fixture"]["generations"][item["id"]]
    prov = manager.provider("fixture", gen)
    assert prov.entrypoint().state == provenance.ADOPTED
    assert prov.entrypoint_path() == str(vendor)
    assert gen["review"]["source"] == "local", "verification must not upgrade trust"
    vendor.write_text("changed")
    with pytest.raises(provenance.ProvenanceError):
        prov.entrypoint()


def test_adopted_file_changed_between_review_and_install_refuses_without_confirmation_reuse(
    recipe, tmp_path
):
    vendor = tmp_path / "fixture"
    vendor.write_bytes(b"#!/bin/true\n")
    vendor.chmod(0o700)
    review = manager.review(local=recipe, adopted_path=str(vendor))
    item = manager.begin_install(
        request_id(), review["id"], review["digest"], confirm_local=True, confirm_adopted=True
    )
    vendor.write_bytes(b"#!/bin/false\n")
    assert manager.run_install(item["id"])["state"] == "failed"
    assert manager.snapshot()["plugins"] == {}
    replacement = manager.review(local=recipe, adopted_path=str(vendor))
    assert replacement["digest"] != review["digest"]
    with pytest.raises(manager.ManagerError, match="reviewed bytes"):
        manager.begin_install(
            request_id(),
            replacement["id"],
            review["digest"],
            confirm_local=True,
            confirm_adopted=True,
        )


@pytest.mark.parametrize("after_commit", [False, True])
def test_activation_crash_and_retry_commit_pointer_and_result_together(
    recipe, monkeypatch, after_commit
):
    gen = verified(recipe)
    rid = request_id()
    write = storage.write

    def crash(path, doc):
        if rid in doc.get("operations", {}):
            if after_commit:
                write(path, doc)
            raise SystemExit("crashed")
        write(path, doc)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write", crash)
        with pytest.raises(SystemExit):
            manager.activate(rid, "fixture", gen)
    manager.recover()
    doc = manager.snapshot()
    assert (doc["plugins"]["fixture"]["active"] == gen) == after_commit
    assert (rid in doc["operations"]) == after_commit
    manager.activate(rid, "fixture", gen)
    assert manager.snapshot()["plugins"]["fixture"]["active"] == gen


def test_managed_manifest_is_retained_for_live_master_after_removal(recipe, monkeypatch):
    from agent_sessions import engines
    from agent_sessions.engines import registry
    from agent_sessions.plugins import roster_state

    class FixtureKind:
        engine_id = "fixture"
        id_pattern = None

        def scan(self):
            return []

    monkeypatch.setitem(registry.STORE_KINDS, "gemini-tmp", FixtureKind)
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_: {"fixture"})
    try:
        gen = verified(recipe)
        manager.activate(request_id(), "fixture", gen)
        registry.reload()
        assert engines.get("fixture") is not None
        assert "fixture" in roster_state.load().manifests
        manager.deactivate(request_id(), "fixture", remove=True)
        registry.reload()
        assert engines.get("fixture") is None
        retiring = engines.get_any("fixture")
        assert retiring is not None and retiring.retiring
        with pytest.raises(engines.EngineError, match="removed"):
            retiring.entrypoint_path()
        assert manager.generation_root("fixture", gen).is_dir()
    finally:
        # Restore the actual kind before leaving the process-wide registry usable by other tests.
        from agent_sessions.engines.gemini import GeminiProvider

        monkeypatch.setitem(registry.STORE_KINDS, "gemini-tmp", GeminiProvider)
        monkeypatch.setattr(registry, "_engines_with_masters", lambda *_: set())
        registry.reload()


@pytest.mark.parametrize("managed", [False, True])
def test_signed_update_preserves_valid_legacy_binding_until_activation(
    recipe, signer, tmp_path, monkeypatch, managed
):
    from agent_sessions.plugins import FIRST_PARTY_DIR, feed, plugins_home

    doc = tomllib.loads((FIRST_PARTY_DIR / "gemini" / "plugin.toml").read_text())
    original = load_first_party().providers["gemini"]
    vendor = (
        plugins_home() / "gemini" / "bin" / "gemini" if managed else tmp_path / "vendor" / "gemini"
    )
    vendor.parent.mkdir(parents=True)
    vendor.write_bytes(b"#!/bin/false\n")
    vendor.chmod(0o700)
    digest = hashlib.sha256(vendor.read_bytes()).hexdigest()
    record = provenance.Record(
        install_entrypoint="bin/gemini" if managed else None,
        install_sha256=digest if managed else None,
        confirmed_path=None if managed else str(vendor),
        confirmed_sha256=None if managed else digest,
        manifest_sha256=original.manifest.digest,
    )
    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_BIN", str(vendor))
    with storage.locked("gemini.json") as path:
        storage.write(path, asdict(record))
    legacy_bytes = path.read_bytes()
    before = manager.overlay(load_first_party()).providers["gemini"].entrypoint()
    assert before.path == str(vendor)
    assert before.state == (provenance.MANAGED if managed else provenance.ADOPTED)

    doc["install"] = recipe["manifest"]["install"]
    doc["binary"]["aliases"] = ["fixture"]
    signed_entry = {"manifest": doc, "recipe": recipe["recipe"]}
    now = int(time.time())
    data = feed.canonical(
        {
            "contract": 1,
            "sequence": 1,
            "issued_at": now,
            "expires_at": now + 3600,
            "plugins": [signed_entry],
        }
    )
    feed.accept(data, signer(data))
    review = manager.review(plugin_id="gemini")
    item = manager.begin_install(request_id(), review["id"], review["digest"])
    # A planned update, restart interruption, or staged candidate cannot replace the legacy row.
    manager.recover()
    assert manager.operation(item["id"])["state"] == "interrupted"
    assert manager.overlay(load_first_party()).providers["gemini"].entrypoint() == before
    review = manager.review(plugin_id="gemini")
    item = manager.begin_install(request_id(), review["id"], review["digest"])
    assert manager.run_install(item["id"])["state"] == "installed"
    assert manager.overlay(load_first_party()).providers["gemini"].entrypoint() == before
    gen = manager.snapshot()["plugins"]["gemini"]["generations"][item["id"]]
    prov = manager.provider("gemini", gen)
    manager.record_verification(
        "gemini", item["id"], [{"check": c, "passed": True} for c in manager.required_checks(prov)]
    )
    manager.activate(request_id(), "gemini", item["id"])
    manager.recover()
    after = manager.overlay(load_first_party()).providers["gemini"].entrypoint()
    assert after.path != before.path and after.state == provenance.MANAGED
    assert Path(after.path).read_bytes() == b"#!/bin/true\n"
    assert path.read_bytes() == legacy_bytes and vendor.read_bytes() == b"#!/bin/false\n"


def test_missing_feed_is_reported_without_creating_a_review(recipe):
    with pytest.raises(manager.ManagerError, match="catalog"):
        manager.review(plugin_id="gemini")
    assert manager.snapshot()["reviews"] == {}


def test_committed_activation_and_revocation_apply_before_response(recipe):
    from agent_sessions.engines import registry
    from agent_sessions.plugins import FIRST_PARTY_DIR

    # The chat store is identity-independent; a made-up CLI ID cannot bind a vendor-only kind.
    manifest = tomllib.loads((FIRST_PARTY_DIR / "apichat" / "plugin.toml").read_text())
    manifest["identity"]["id"] = "fixture"
    recipe.clear()
    recipe.update(manifest=manifest, recipe={"artifacts": []})
    gen = verified(recipe)
    manager.activate(request_id(), "fixture", gen)
    live = registry.live_provider("fixture")
    assert live is not None and registry.admits(live)
    with registry.snapshot_scope() as captured:
        manager.deactivate(request_id(), "fixture")
        assert registry.current() is captured
        assert not registry.admits(live)
    assert registry.get("fixture") is None
    manager.activate(request_id(), "fixture", gen)
    again = registry.live_provider("fixture")
    assert again is not None and registry.admits(again)
    manager.deactivate(request_id(), "fixture", remove=True)
    assert not registry.admits(again)


def test_post_replace_fsync_error_keeps_published_installed_outcome(recipe, monkeypatch):
    item = planned(recipe)
    fsync = storage.os.fsync
    failed = False

    def fault(fd):
        nonlocal failed
        if not failed and manager.operation(item["id"])["state"] == "installed":
            failed = True
            raise OSError("simulated directory fsync error after replace")
        return fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", fault)
    outcome = manager.run_install(item["id"])
    assert failed and outcome["state"] == "installed"
    assert outcome["error"] is None
    assert manager.snapshot()["plugins"]["fixture"]["candidate"] == item["id"]
    assert manager.run_install(item["id"])["state"] == "installed"


@pytest.mark.parametrize("phase", ["fetch", "extract", "record"])
def test_final_install_work_exceeding_budget_never_publishes(recipe, monkeypatch, phase):
    now = [0.0]
    monkeypatch.setattr(manager.budget.time, "monotonic", lambda: now[0])
    target = manager.artifacts if phase != "record" else manager
    name = phase if phase != "record" else "_record"
    original = getattr(target, name)

    def overrun(*args, **kwargs):
        value = original(*args, **kwargs)
        now[0] += manager.MAX_INSTALL_SECONDS + 1
        return value

    monkeypatch.setattr(target, name, overrun)
    item = manager.run_install(planned(recipe)["id"])
    assert item["state"] == "failed" and "time limit" in item["error"]
    assert manager.snapshot()["plugins"] == {}


@pytest.mark.parametrize("damage", ["missing", "legacy"])
@pytest.mark.parametrize("enabled", [False, True])
def test_lost_revision_closes_admission_until_exact_state_is_restored(
    recipe, damage, enabled, monkeypatch
):
    from agent_sessions.engines import registry
    from agent_sessions.plugins import admission

    class FixtureKind(registry.GeminiProvider):
        engine_id = "fixture"

    monkeypatch.setitem(registry.STORE_KINDS, recipe["manifest"]["store"]["layout"], FixtureKind)
    gen = verified(recipe)
    manager.activate(request_id(), "fixture", gen)
    before = registry.get("fixture")
    assert before is not None and registry.admits(before)
    if not enabled:
        manager.deactivate(request_id(), "fixture")
    path = storage.root() / manager.DOCUMENT
    original = path.read_bytes()
    monkeypatch.setattr(registry, "_engines_with_masters", lambda _candidates=(): {"fixture"})
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(json.dumps({"contract": 1, "plugins": {}, "operations": {}, "reviews": {}}))
    for _ in range(2):
        registry.sync_committed()
        assert not registry.admits(before)
        assert "manager" in registry.capture().problems
        with admission.acquire(before) as guard:
            assert guard.reason == admission.UNAVAILABLE
        # Explicit roster reload cannot revive packaged providers from the missing document.
        registry.reload()
    with pytest.raises(manager.ManagerError, match="revision"):
        manager.deactivate(request_id(), "fixture")
    assert path.exists() == (damage != "missing")
    path.write_bytes(original)
    path.chmod(0o600)
    registry.sync_committed()
    assert registry.admits(before) is enabled
    assert "manager" not in registry.capture().problems


@pytest.mark.parametrize("damage", ["missing", "legacy"])
@pytest.mark.parametrize("remove", [False, True])
def test_disabled_builtin_stays_closed_after_manager_loss_and_process_restart(
    recipe, damage, remove
):
    manager.deactivate(request_id(), "claude", remove=remove)
    path = storage.root() / manager.DOCUMENT
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(json.dumps({"contract": 1, "plugins": {}, "operations": {}, "reviews": {}}))
    probe = """
import json
from agent_sessions.plugins import load_first_party, manager
loaded = manager.overlay(load_first_party())
print(json.dumps({"providers": sorted(loaded.providers), "problems": loaded.problems}))
"""

    def restarted():
        result = subprocess.run(
            [sys.executable, "-c", probe],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    damaged = restarted()
    assert not damaged["providers"] and "manager" in damaged["problems"]
    path.write_bytes(original)
    path.chmod(0o600)
    restored = restarted()
    assert "claude" not in restored["providers"]
    assert "shell" in restored["providers"] and "manager" not in restored["problems"]


def test_revision_marker_is_durable_before_first_decision_and_survives_failed_commit(
    recipe, monkeypatch
):
    write = storage.write

    def fail_commit(path, doc, **kwargs):
        if path.name == manager.DOCUMENT and doc.get("operations"):
            assert storage.read(path.with_name(manager.REVISION_MARKER)) == {
                "contract": 1,
                "revision_required": True,
            }
            raise OSError("decision commit failed")
        return write(path, doc, **kwargs)

    monkeypatch.setattr(storage, "write", fail_commit)
    with pytest.raises(OSError, match="decision commit failed"):
        manager.deactivate(request_id(), "claude")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from agent_sessions.plugins import load_first_party, manager
loaded = manager.overlay(load_first_party())
assert 'claude' in loaded.providers and 'manager' not in loaded.problems
""",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
