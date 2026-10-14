"""Signed imports, editable provenance, whole-tree conflicts and uncertain publication."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_sessions import fileedit, renameat
from agent_sessions.gallery import contract, library, origins, skills, storage, trust
from agent_sessions.playbooks import store, tomlw
from agent_sessions.plugins import feed

FIXTURES = Path(__file__).with_name("fixtures") / "gallery"
NOW = 1791000000


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    assert fileedit.install_lease_signal_handler()
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(tmp_path / "playbooks"))
    monkeypatch.setattr(contract.time, "time", lambda: NOW)
    key = tmp_path / "test-key"
    subprocess.run(
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(feed.PRINCIPAL + " " + key.with_suffix(".pub").read_text())
    monkeypatch.setattr(feed, "SIGNERS", allowed)
    document = json.loads((FIXTURES / "index.json").read_bytes())

    def accept(doc):
        for entry in doc["entries"]:
            entry["digest"] = hashlib.sha256(contract.canonical(entry["content"])).hexdigest()
        raw = contract.canonical(doc)
        signature = subprocess.run(
            ["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(key), "-n", contract.NAMESPACE],
            input=raw,
            capture_output=True,
            check=True,
        ).stdout
        return trust.accept(
            raw, signature, dict(sequence=doc["sequence"], digest=hashlib.sha256(raw).hexdigest())
        )

    accept(document)
    return document, accept


def imported(catalog, kind="playbook", local_id="my-copy"):
    document, _ = catalog
    entry = next(e for e in document["entries"] if e["content"]["kind"] == kind)
    op = uuid.uuid4().hex
    result = library.import_copy(kind, local_id, entry["digest"], op, True)
    assert result["state"] == "complete"
    return result, entry, op


@pytest.mark.parametrize("existing", [False, True])
def test_skill_publication_refuses_changed_staging_and_retains_every_tree(
    catalog, monkeypatch, existing
):
    original_text = None
    if existing:
        imported(catalog, "skill")
        current = skills.get("my-copy")
        original_text = current["files"]["SKILL.md"]
    target = "edited-copy" if existing else "my-copy"
    rename = renameat.renameat2
    changed = False

    def race(src_fd, src, dst_fd, dst, flags):
        nonlocal changed
        if not changed and src.startswith(".staging-") and dst == target:
            changed = True
            staged = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
            try:
                child = os.open("SKILL.md", os.O_WRONLY | os.O_TRUNC, dir_fd=staged)
                with os.fdopen(child, "w") as stream:
                    stream.write("Raced instructions")
            finally:
                os.close(staged)
        return rename(src_fd, src, dst_fd, dst, flags)

    monkeypatch.setattr(renameat, "renameat2", race)
    with pytest.raises(store.Conflict):
        skills.save(target, {"SKILL.md": "Submitted instructions"})
    assert changed
    retained = [p.read_text() for p in storage.skills_root().rglob("SKILL.md")]
    assert "Raced instructions" in retained
    assert skills.get(target)["files"]["SKILL.md"] == "Raced instructions"
    if existing:
        assert original_text in retained


def next_version(catalog, kind="playbook", text="Upstream update"):
    doc, accept = catalog
    new = copy.deepcopy(next(e for e in doc["entries"] if e["content"]["kind"] == kind))
    new["content"]["version"] = "1.1.0"
    if kind == "playbook":
        manifest = contract.tomllib.loads(new["content"]["files"]["playbook.toml"])
        manifest["identity"]["version"] = "1.1.0"
        new["content"]["files"]["playbook.toml"] = tomlw.dumps(manifest)
    new["content"]["files"]["README.md"] = text
    doc["sequence"] += 1
    doc["entries"].append(new)
    accept(doc)
    return new


def test_frozen_cross_distribution_bytes_and_refusals():
    vectors = json.loads((FIXTURES / "vectors.json").read_text())
    assert (
        hashlib.sha256(Path(contract.__file__).read_bytes()).hexdigest()
        == vectors["validator_sha256"]
    )
    for name, digest in vectors["sha256"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    assert len(contract.parse((FIXTURES / "index.json").read_bytes(), now=NOW).entries) == 2
    for case in json.loads((FIXTURES / "refusals.json").read_text()):
        with pytest.raises(ValueError):
            contract.parse(case["raw"].encode(), now=NOW)


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_import_is_local_editable_exact_origin_and_id_only_rewrite(catalog, kind):
    result, entry, op = imported(catalog, kind)
    local = library.local(kind, "my-copy")
    origin = local["gallery_origin"]
    assert origin["state"] == "verified" and origin["entry_digest"] == entry["digest"]
    assert local["editable"]
    assert (
        origins.evidence(origin["pointer"], kind, "my-copy")["base_revision"] == local["revision"]
    )
    if kind == "playbook":
        manifest = contract.tomllib.loads(local["files"]["playbook.toml"])
        assert manifest["identity"]["id"] == "my-copy"
        assert local["files"]["flows/triage.toml"] == entry["content"]["files"]["flows/triage.toml"]
    else:
        assert local["files"]["notes/café.md"] == entry["content"]["files"]["notes/café.md"]
    assert library.import_copy(kind, "my-copy", entry["digest"], op, True) == result
    with pytest.raises(store.Conflict):
        library.import_copy(kind, "other-copy", entry["digest"], op, True)


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_ordinary_save_preserves_origin_and_three_way_update_retains_every_base(catalog, kind):
    imported(catalog, kind)
    before = library.local(kind, "my-copy")
    files = dict(before["files"])
    files["README.md"] = "Local edit"
    if kind == "playbook":
        store.update_playbook("my-copy", before["revision"], files)
    else:
        (storage.skills_root() / "my-copy" / "README.md").write_text("Local edit")
    edited = library.local(kind, "my-copy")
    assert edited["gallery_origin"] == before["gallery_origin"]
    incoming = next_version(catalog, kind)
    comparison = library.compare(kind, "my-copy", incoming["digest"])
    # The update screen shows "v<base> → v<incoming>" (#1440).
    assert comparison["base_version"] == before["gallery_origin"]["version"]
    assert comparison["incoming_version"] == incoming["content"]["version"]
    assert "README.md" in comparison["conflicts"]
    assert comparison["local"]["README.md"] == "Local edit"
    assert comparison["incoming"]["README.md"] == "Upstream update"
    merged = dict(comparison["proposed"])
    merged["README.md"] = "Reviewed merge"
    outcome = library.apply_update(
        kind,
        "my-copy",
        incoming["digest"],
        comparison["comparison"],
        merged,
        uuid.uuid4().hex,
        True,
        new_id="updated-copy",
    )
    assert outcome["state"] == "complete"
    assert library.local(kind, "my-copy") == edited
    after = library.local(kind, "updated-copy")
    assert after["files"]["README.md"] == "Reviewed merge"
    assert after["gallery_origin"]["previous"] == before["gallery_origin"]["pointer"]
    assert after["gallery_origin"]["original"] == before["gallery_origin"]["original"]
    evidence = origins.evidence(after["gallery_origin"]["pointer"], kind, "updated-copy")
    assert evidence["base_files"]["README.md"] == "Upstream update"
    assert origins.evidence(before["gallery_origin"]["pointer"], kind, "my-copy")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fault", ["content", "origin"])
def test_playbook_gallery_publication_verifies_content_and_origin_after_rename(
    catalog, monkeypatch, existing, fault
):
    before = None
    if existing:
        imported(catalog)
        before = store.get_playbook("my-copy")
        incoming = next_version(catalog)
        comparison = library.compare("playbook", "my-copy", incoming["digest"])
    else:
        incoming = catalog[0]["entries"][0]
    target = "updated-copy" if existing else "my-copy"
    rename = renameat.renameat2
    changed = False

    def race(src_fd, src, dst_fd, dst, flags):
        nonlocal changed
        if not changed and src.startswith(".staging-") and dst == target:
            changed = True
            staged = store.local_root() / src
            if fault == "content":
                (staged / "README.md").write_text("Unreviewed content")
            else:
                os.removexattr(staged, origins.XATTR)
        return rename(src_fd, src, dst_fd, dst, flags)

    monkeypatch.setattr(renameat, "renameat2", race)
    operation = uuid.uuid4().hex
    with pytest.raises(store.Conflict):
        if existing:
            library.apply_update(
                "playbook",
                "my-copy",
                incoming["digest"],
                comparison["comparison"],
                comparison["proposed"],
                operation,
                True,
                new_id="updated-copy",
            )
        else:
            library.import_copy("playbook", "my-copy", incoming["digest"], operation, True)
    assert changed
    assert library.operation(operation)["state"] == "uncertain"
    current = store.get_playbook(target)
    if fault == "content":
        assert current["files"]["README.md"] == "Unreviewed content"
    else:
        assert current["gallery_origin"]["state"] == "unavailable"
    if existing:
        assert store.get_playbook("my-copy") == before


def test_stale_comparison_cannot_overwrite_a_local_edit(catalog):
    imported(catalog)
    incoming = next_version(catalog)
    compared = library.compare("playbook", "my-copy", incoming["digest"])
    local = store.get_playbook("my-copy")
    files = dict(local["files"])
    files["README.md"] = "Late edit"
    store.update_playbook("my-copy", local["revision"], files)
    with pytest.raises(store.Conflict):
        library.apply_update(
            "playbook",
            "my-copy",
            incoming["digest"],
            compared["comparison"],
            compared["proposed"],
            uuid.uuid4().hex,
            True,
            new_id="updated-copy",
        )
    assert store.get_playbook("my-copy")["files"]["README.md"] == "Late edit"


@pytest.mark.parametrize(
    "fault", ["missing-record", "bad-record", "removed-pointer", "bad-pointer"]
)
def test_missing_or_tampered_provenance_is_visible_and_blocks_edit(catalog, fault):
    imported(catalog)
    old = store.get_playbook("my-copy")
    pointer = old["gallery_origin"]["pointer"]
    path = storage.root() / f"origin-{pointer}.json"
    if fault == "missing-record":
        path.rename(path.with_suffix(".retained"))
    elif fault == "bad-record":
        path.write_text("{}")
    elif fault == "removed-pointer":
        os.removexattr(store.local_root() / "my-copy", origins.XATTR)
    else:
        os.setxattr(store.local_root() / "my-copy", origins.XATTR, b"bad")
    current = store.get_playbook("my-copy")
    assert current["gallery_origin"]["state"] == "unavailable"
    with pytest.raises(store.StoreError):
        store.update_playbook(
            "my-copy", current["revision"], {**current["files"], "README.md": "attempt"}
        )
    assert store.get_playbook("my-copy")["files"] == old["files"]


def test_delete_then_create_and_duplicate_do_not_adopt_an_old_origin(catalog):
    imported(catalog)
    old = store.get_playbook("my-copy")
    duplicate = store.duplicate_playbook("my-copy", {"id": "new-copy"})
    assert duplicate["gallery_origin"] is None
    store.delete_playbook("my-copy", old["revision"])
    fresh = store.create_playbook(old["files"])
    assert fresh["gallery_origin"] is None
    assert origins.evidence(old["gallery_origin"]["pointer"], "playbook", "my-copy")


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_lost_response_after_rename_recovers_exact_operation_without_recreating(
    catalog, monkeypatch, kind
):
    document, _ = catalog
    entry = next(e for e in document["entries"] if e["content"]["kind"] == kind)
    op = uuid.uuid4().hex
    original = store._publish_new

    def crash(*args):
        original(*args)
        raise OSError("lost response")

    with monkeypatch.context() as m:
        m.setattr(store, "_publish_new", crash)
        with pytest.raises(OSError):
            library.import_copy(kind, "my-copy", entry["digest"], op, True)
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    inode = (root / "my-copy").stat().st_ino
    result = library.operation(op)
    assert result["state"] == "complete" and result["result"]["recovered"]
    assert library.import_copy(kind, "my-copy", entry["digest"], op, True)["state"] == "complete"
    assert (root / "my-copy").stat().st_ino == inode


@pytest.mark.parametrize("kind", ["playbook", "skill"])
@pytest.mark.parametrize("change", ["before_rename", "later_edit"])
def test_unverified_recovery_never_adopts_changed_bytes(catalog, monkeypatch, kind, change):
    entry = next(e for e in catalog[0]["entries"] if e["content"]["kind"] == kind)
    operation = uuid.uuid4().hex
    publish = store._publish_new
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    path = "README.md" if kind == "playbook" else "SKILL.md"

    def interrupted(fd, staging, local_id):
        if change == "before_rename":
            (root / staging / path).write_text("Changed before publication")
        publish(fd, staging, local_id)
        raise OSError("interrupted before publication verification")

    with monkeypatch.context() as m:
        m.setattr(store, "_publish_new", interrupted)
        with pytest.raises(OSError):
            library.import_copy(kind, "my-copy", entry["digest"], operation, True)
    if change == "later_edit":
        current = library.local(kind, "my-copy")
        updated = {**current["files"], path: "Later local edit"}
        if kind == "playbook":
            store.update_playbook("my-copy", current["revision"], updated)
        else:
            (storage.skills_root() / "my-copy" / path).write_text("Later local edit")
    before = library.local(kind, "my-copy")
    assert before["gallery_origin"]["state"] == "verified"
    assert library.operation(operation)["state"] == "uncertain"
    assert (
        library.import_copy(kind, "my-copy", entry["digest"], operation, True)["state"]
        == "uncertain"
    )
    assert library.local(kind, "my-copy")["files"] == before["files"]


def test_interruption_before_rename_does_not_guess_or_create_a_second_copy(catalog, monkeypatch):
    document, _ = catalog
    entry = document["entries"][0]
    op = uuid.uuid4().hex
    with monkeypatch.context() as m:
        m.setattr(store, "_publish_new", lambda *a: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError):
            library.import_copy("playbook", "my-copy", entry["digest"], op, True)
    assert library.operation(op)["state"] == "uncertain"
    assert (
        library.import_copy("playbook", "my-copy", entry["digest"], op, True)["state"]
        == "uncertain"
    )
    assert not (store.local_root() / "my-copy").exists()


def test_unsupported_xattrs_refuse_before_publication(catalog, monkeypatch):
    document, _ = catalog
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda *a, **kw: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )
    with pytest.raises(ValueError, match="cannot preserve"):
        library.import_copy(
            "playbook", "my-copy", document["entries"][0]["digest"], uuid.uuid4().hex, True
        )
    assert not (store.local_root() / "my-copy").exists()


def test_stale_and_withdrawn_metadata_remains_inspectable_but_cannot_import(catalog, monkeypatch):
    doc, accept = catalog
    digest = doc["entries"][0]["digest"]
    doc["sequence"] += 1
    doc["entries"][0]["available"] = False
    accept(doc)
    assert library.catalog()["entries"][0]["content"]
    with pytest.raises(ValueError):
        library.import_copy("playbook", "my-copy", digest, uuid.uuid4().hex, True)
    monkeypatch.setattr(contract.time, "time", lambda: NOW + 7200)
    assert library.catalog()["state"] == "stale"
    assert not library.inspect(digest, "my-copy")["importable"]


def test_rollback_missing_index_and_interrupted_floor_never_erase_evidence(catalog, monkeypatch):
    doc, accept = catalog
    original = copy.deepcopy(doc)
    doc["sequence"] = 2
    accept(doc)
    with pytest.raises(ValueError, match="rollback"):
        accept(original)
    index = storage.root() / "index.json"
    index.rename(index.with_suffix(".retained"))
    assert library.catalog()["state"] == "unavailable"
    with pytest.raises(ValueError, match="missing"):
        accept(doc)


def test_inspection_includes_validated_inputs_and_workflow(catalog):
    entry = catalog[0]["entries"][0]
    preview = library.inspect(entry["digest"], "my-copy")
    assert preview["importable"]
    assert preview["preview"]["inputs"][0]["name"] == "inbox_name"
    assert preview["preview"]["flows"][0]["steps"][0]["title"] == "Read the new messages"


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_committed_import_preserves_durability_warning_in_recorded_outcome(
    catalog, monkeypatch, kind
):
    monkeypatch.setattr(store, "_fsync_after", lambda fd: "Directory flush could not be confirmed")
    result, _, operation_id = imported(catalog, kind)
    assert result["result"]["durable"] is False
    assert library.operation(operation_id) == result
    assert library.local(kind, "my-copy")["gallery_origin"]["state"] == "verified"


@pytest.mark.parametrize("existing", [False, True])
def test_failed_skill_publication_leaves_a_later_writers_live_tree(
    catalog, tmp_path, monkeypatch, existing
):
    import shutil

    if existing:
        imported(catalog, "skill")
    target = "edited-copy" if existing else "my-copy"
    rename, read = renameat.renameat2, skills._read
    moved = tmp_path / "inspected-skill"
    raced = False

    def change_before_publish(src_fd, src, dst_fd, dst, flags):
        if src.startswith(".staging-") and dst == target:
            (storage.skills_root() / src / "SKILL.md").write_text("Unreviewed content")
        return rename(src_fd, src, dst_fd, dst, flags)

    def replace_after_read(fd, name):
        nonlocal raced
        out = read(fd, name)
        if not raced and out["files"]["SKILL.md"] == "Unreviewed content":
            raced = True
            live = storage.skills_root() / name
            live.rename(moved)
            shutil.copytree(moved, live)
            (live / "SKILL.md").write_text("Later writer")
        return out

    monkeypatch.setattr(renameat, "renameat2", change_before_publish)
    monkeypatch.setattr(skills, "_read", replace_after_read)
    with pytest.raises(store.Conflict):
        skills.save(target, {"SKILL.md": "Submitted"})
    assert raced
    assert (storage.skills_root() / target / "SKILL.md").read_text() == "Later writer"
    assert (moved / "SKILL.md").read_text() == "Unreviewed content"


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_legacy_pending_in_place_update_remains_uncertain(catalog, kind):
    imported(catalog, kind)
    op = uuid.uuid4().hex
    current = library.local(kind, "my-copy")
    with storage.locked("operation-" + op) as path:
        storage.write(
            path,
            {
                "state": "planned",
                "payload": {"action": "update", "kind": kind, "id": "my-copy"},
                "origin": current["gallery_origin"]["pointer"],
                "revision": current["revision"],
            },
        )
    assert library.operation(op)["state"] == "uncertain"
    assert library.local(kind, "my-copy") == current


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_recovery_waits_for_the_publication_lane_before_deciding(catalog, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entry = next(e for e in catalog[0]["entries"] if e["content"]["kind"] == kind)
    operation = uuid.uuid4().hex
    published, release = Event(), Event()
    publish = store._publish_new

    def pause_after_rename(*args):
        result = publish(*args)
        published.set()
        assert release.wait(8)
        return result

    monkeypatch.setattr(store, "_publish_new", pause_after_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(library.import_copy, kind, "my-copy", entry["digest"], operation, True)
        try:
            assert published.wait(5)
            with pytest.raises(contract.GalleryError, match="busy"):
                library.operation(operation)
            assert (
                storage.read(storage.root() / f"operation-{operation}.json")["state"] == "planned"
            )
        finally:
            release.set()
        expected = future.result()
    assert library.operation(operation) == expected
    assert "recovered" not in expected["result"]
