"""Real descriptor/lease effects preserve operator entries at every raced boundary."""

import os
from pathlib import Path

import pytest

from agent_sessions import prefs
from agent_sessions.fsbrowse import FsError
from agent_sessions.playbooks import destination, material_write, mutation_plan
from test_file_edit import root as _edit_root

root = _edit_root


@pytest.fixture
def effect(root, tmp_path):
    prefs.set_project_roots([str(root)])
    entry = tmp_path / "effect"
    entry.mkdir(mode=0o700)
    fd = os.open(entry, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield destination.review_folder(str(root)), fd, entry
    finally:
        os.close(fd)


def creation(kind="file"):
    return mutation_plan.Change(
        "ALIAS.md" if kind == "symlink" else "material.bin",
        "create",
        destination.Node("absent"),
        destination.Node("symlink", target="RULES.md")
        if kind == "symlink"
        else destination.Node("file", data=b"\x00\xffreviewed"),
        None,
    )


def run_create(effect, change=None, *, progress=lambda _: None, admit=lambda: None):
    folder, entry, _ = effect
    material_write.create(folder, change or creation(), {}, entry, progress=progress, admit=admit)


def removal(effect, kind="file"):
    folder, _, _ = effect
    path = Path(folder.path) / ("ALIAS.md" if kind == "symlink" else "material.bin")
    if kind == "file":
        path.write_bytes(b"\x00\xffowned")
    else:
        path.symlink_to("RULES.md")
    nodes = destination.snapshot(folder, [path.name])
    return mutation_plan.Change(
        path.name, "remove", nodes[path.name], destination.Node("absent"), None
    )


def run_remove(effect, change, *, progress=lambda _: None, admit=lambda: None):
    folder, entry, _ = effect
    material_write.remove(folder, change, {}, entry, progress=progress, admit=admit)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_create_publishes_exact_bytes_or_typed_alias_and_drops_its_settled_pin(effect, kind):
    change = creation(kind)
    progress = []
    run_create(effect, change, progress=progress.append)
    path = Path(effect[0].path) / change.path
    assert (
        os.readlink(path) == "RULES.md"
        if kind == "symlink"
        else path.read_bytes() == change.after.data
    )
    assert path.lstat().st_nlink == 1
    assert not (effect[2] / "candidate").exists()
    assert [p["phase"] for p in progress] == ["intent", "staged", "done"]


def test_create_cannot_replace_an_entry_that_appears_after_review(effect):
    path = Path(effect[0].path) / creation().path

    def progress(value):
        if value["phase"] == "staged":
            path.write_bytes(b"operator claimant")

    with pytest.raises(FileExistsError):
        run_create(effect, progress=progress)
    assert path.read_bytes() == b"operator claimant"
    assert (effect[2] / "candidate").read_bytes() == creation().after.data


def test_create_links_the_opened_candidate_not_a_substituted_staging_name(effect):
    def progress(value):
        if value["phase"] == "staged":
            (effect[2] / "candidate").rename(effect[2] / "kept-candidate")
            (effect[2] / "candidate").write_bytes(b"substitute")

    with pytest.raises(FsError, match="recovery pin changed"):
        run_create(effect, progress=progress)
    assert (Path(effect[0].path) / creation().path).read_bytes() == creation().after.data
    assert (effect[2] / "candidate").read_bytes() == b"substitute"


def test_alias_publication_uses_its_pinned_symlink_inode_even_if_staging_is_substituted(effect):
    change = creation("symlink")

    def progress(value):
        if value["phase"] == "staged":
            (effect[2] / "candidate").rename(effect[2] / "kept-candidate")
            (effect[2] / "candidate").symlink_to("OTHER.md")

    with pytest.raises(FsError, match="recovery pin changed"):
        run_create(effect, change, progress=progress)
    assert os.readlink(Path(effect[0].path) / change.path) == "RULES.md"
    assert os.readlink(effect[2] / "candidate") == "OTHER.md"


@pytest.mark.parametrize("replace", [False, True])
def test_post_install_refusal_retains_the_candidate_and_preserves_a_raced_writer(effect, replace):
    path = Path(effect[0].path) / creation().path
    calls = 0

    def admit():
        nonlocal calls
        calls += 1
        if calls == 3:
            if replace:
                path.rename(path.with_name("operator-kept"))
                path.write_bytes(b"operator replacement")
            raise FsError("admission revoked", status=409)

    with pytest.raises(FsError, match="admission revoked"):
        run_create(effect, admit=admit)
    if replace:
        assert path.read_bytes() == b"operator replacement"
        assert (effect[2] / "withdrawn").read_bytes() == b"operator replacement"
    else:
        assert not path.exists()
        assert (effect[2] / "withdrawn").read_bytes() == creation().after.data


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_remove_retains_the_exact_owned_inode_without_unlinking_it(effect, kind):
    change = removal(effect, kind)
    progress = []
    run_remove(effect, change, progress=progress.append)
    path = Path(effect[0].path) / change.path
    retained = effect[2] / "removed"
    assert not path.exists() and not path.is_symlink()
    assert retained.lstat().st_ino == change.before.identity[1]
    assert (
        os.readlink(retained) == "RULES.md"
        if kind == "symlink"
        else retained.read_bytes() == change.before.data
    )
    assert [p["phase"] for p in progress] == ["intent", "done"]


def test_removal_refuses_an_operator_edit_before_displacement(effect):
    change = removal(effect)
    path = Path(effect[0].path) / change.path
    path.write_bytes(b"operator edit")
    with pytest.raises(FsError, match="changed after review"):
        run_remove(effect, change)
    assert path.read_bytes() == b"operator edit"
    assert list(effect[2].iterdir()) == []


def test_removal_restores_an_entry_substituted_after_the_lease(effect):
    change = removal(effect)
    path = Path(effect[0].path) / change.path

    def progress(value):
        if value["phase"] == "intent":
            path.rename(path.with_name("kept-owned"))
            path.write_bytes(b"operator replacement")

    with pytest.raises(FsError, match="replaced during removal"):
        run_remove(effect, change, progress=progress)
    assert path.read_bytes() == b"operator replacement"
    assert (effect[2] / "removed").read_bytes() == b"operator replacement"
    assert path.with_name("kept-owned").read_bytes() == change.before.data


def test_remove_restores_and_retains_when_the_done_checkpoint_fails(effect):
    change = removal(effect)

    def progress(value):
        if value["phase"] == "done":
            raise OSError("checkpoint failed")

    with pytest.raises(OSError, match="checkpoint failed"):
        run_remove(effect, change, progress=progress)
    assert (Path(effect[0].path) / change.path).read_bytes() == change.before.data
    assert (effect[2] / "removed").read_bytes() == change.before.data


def test_existing_retention_never_gets_overwritten_by_a_retry(effect):
    change = removal(effect)
    (effect[2] / "removed").write_bytes(b"earlier retained bytes")
    with pytest.raises(FileExistsError):
        run_remove(effect, change)
    assert (Path(effect[0].path) / change.path).read_bytes() == change.before.data
    assert (effect[2] / "removed").read_bytes() == b"earlier retained bytes"


def test_parent_replacement_and_symlink_components_refuse_before_writing(effect):
    folder, entry, _ = effect
    docs = Path(folder.path) / "docs"
    docs.mkdir()
    nodes = destination.snapshot(folder, ["docs/material.bin"])
    change = creation()
    change = mutation_plan.Change(
        "docs/material.bin", change.action, change.before, change.after, None
    )
    docs.rename(docs.with_name("kept-docs"))
    docs.symlink_to("kept-docs", target_is_directory=True)
    with pytest.raises(OSError):
        material_write.create(
            folder, change, nodes, entry, progress=lambda _: None, admit=lambda: None
        )
    assert list(docs.iterdir()) == []
    assert list(effect[2].iterdir()) == []
