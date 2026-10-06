"""A deployment review reads a bounded, identity-bound destination without changing it."""

from __future__ import annotations

import os

import pytest

from agent_sessions import prefs
from agent_sessions.fsbrowse import FsError
from agent_sessions.playbooks import destination, schema
from agent_sessions.playbooks.errors import PlaybookFormatError


@pytest.fixture
def project(tmp_path):
    prefs.set_project_roots([str(tmp_path)])
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_snapshot_records_complete_bytes_parents_and_absent_nodes_without_creating_them(project):
    (project / "docs").mkdir()
    (project / "docs" / "notes.md").write_bytes(b"reviewed\x00bytes")
    folder = destination.review_folder(str(project))
    nodes = destination.snapshot(folder, ["docs/notes.md", "new/sub/missing"])
    assert nodes["docs"].kind == "directory"
    assert nodes["docs/notes.md"].data == b"reviewed\x00bytes"
    assert nodes["docs/notes.md"].identity[1] == (project / "docs/notes.md").stat().st_ino
    assert all(nodes[path].kind == "absent" for path in ("new", "new/sub", "new/sub/missing"))
    assert not (project / "new").exists()


def test_review_cannot_be_reused_after_the_destination_folder_is_replaced(project):
    folder = destination.review_folder(str(project))
    project.rename(project.with_name("original"))
    project.mkdir()
    with pytest.raises(FsError, match="folder was replaced"):
        destination.snapshot(folder, ["notes.md"])


@pytest.mark.parametrize("change", ["exclude", "no_roots", "other_root"])
def test_live_scope_changes_refuse_a_previously_reviewed_folder(project, tmp_path, change):
    folder = destination.review_folder(str(project))
    if change == "exclude":
        prefs.set_folder_exclusions([str(project)])
    elif change == "no_roots":
        prefs.set_project_roots([str(tmp_path / "missing-root")])
    else:
        other = tmp_path / "other"
        other.mkdir()
        prefs.set_project_roots([str(other)])
    with pytest.raises(FsError, match="outside the configured"):
        destination.snapshot(folder, ["notes.md"])


def test_scope_does_not_treat_a_sibling_prefix_as_contained(project):
    prefs.set_project_roots([str(project)])
    sibling = project.with_name("project-other")
    sibling.mkdir()
    with pytest.raises(FsError, match="outside the configured"):
        destination.review_folder(str(sibling))


def test_intermediate_symlink_is_not_followed_even_when_its_target_is_inside(project):
    (project / "actual").mkdir()
    (project / "actual" / "notes.md").write_text("operator file")
    (project / "alias").symlink_to("actual", target_is_directory=True)
    with pytest.raises(OSError):
        destination.snapshot(destination.review_folder(str(project)), ["alias/notes.md"])


def test_leaf_link_is_reported_without_reading_its_target(project, tmp_path):
    outside = tmp_path / "private"
    outside.write_text("never read this")
    (project / "RULES.md").symlink_to(outside)
    nodes = destination.snapshot(destination.review_folder(str(project)), ["RULES.md"])
    assert nodes["RULES.md"].kind == "symlink"
    assert nodes["RULES.md"].target == str(outside)
    assert nodes["RULES.md"].data is None


def test_a_leaf_link_target_that_is_not_utf8_is_refused(project):
    os.symlink(b"\xff-target", bytes(project / "RULES.md"))
    with pytest.raises(FsError) as refused:
        destination.snapshot(destination.review_folder(str(project)), ["RULES.md"])
    assert refused.value.status == 409


@pytest.mark.parametrize("kind", ["fifo", "hardlink", "directory"])
def test_material_leaf_refuses_nonregular_or_shared_inodes(project, kind):
    path = project / "notes"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "hardlink":
        (project / "original").write_text("shared")
        path.hardlink_to(project / "original")
    else:
        path.mkdir()
    with pytest.raises(FsError, match="single-link regular"):
        destination.snapshot(destination.review_folder(str(project)), ["notes"])


@pytest.mark.parametrize("path", ["../escape", ".git/config", ".GIT/config", "HEAD", "a//b"])
def test_material_paths_use_the_existing_node_and_git_metadata_policy(project, path):
    with pytest.raises((FsError, PlaybookFormatError)):
        destination.snapshot(destination.review_folder(str(project)), [path])


@pytest.mark.parametrize("nested", [False, True])
def test_bare_git_metadata_is_refused_including_a_nested_destination(project, nested):
    (project / "HEAD").write_text("ref: refs/heads/main\n")
    (project / "objects").mkdir()
    (project / "refs").mkdir()
    target = project
    if nested:
        target = project / "hooks"
        target.mkdir()
    with pytest.raises(FsError, match="git"):
        destination.snapshot(destination.review_folder(str(target)), ["pre-commit"])


def test_same_size_in_place_edit_during_read_refuses_even_when_mtime_is_restored(
    project, monkeypatch
):
    path = project / "notes.md"
    path.write_bytes(b"before")
    stamp = path.stat()
    original = os.read
    edited = False

    def edit_after_read(fd, count):
        nonlocal edited
        data = original(fd, count)
        if data == b"before" and not edited:
            edited = True
            path.write_bytes(b"after!")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        return data

    monkeypatch.setattr(destination.os, "read", edit_after_read)
    with pytest.raises(FsError, match="changed during review"):
        destination.snapshot(destination.review_folder(str(project)), ["notes.md"])
    assert edited and path.read_bytes() == b"after!"


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
def test_destination_content_is_bounded(project, monkeypatch, limit):
    (project / "notes.md").write_bytes(b"123456")
    monkeypatch.setattr(schema, limit, 5)
    with pytest.raises(FsError, match="review size limit"):
        destination.snapshot(destination.review_folder(str(project)), ["notes.md"])


@pytest.mark.parametrize("path", [None, 42, "relative", "/invalid\x00path"])
def test_folder_input_is_rejected_before_filesystem_access(project, path):
    with pytest.raises(FsError, match="absolute destination"):
        destination.review_folder(path)


def test_a_symlinked_root_is_refused(project, tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(project, target_is_directory=True)
    with pytest.raises(FsError, match="symlink"):
        destination.review_folder(str(link))


def test_parent_moved_during_read_cannot_supply_the_review_snapshot(project, monkeypatch):
    parent = project / "docs"
    parent.mkdir()
    (parent / "notes.md").write_bytes(b"before")
    original = os.read
    moved = False

    def move_after_read(fd, count):
        nonlocal moved
        data = original(fd, count)
        if data == b"before" and not moved:
            moved = True
            parent.rename(project / "moved-docs")
            parent.mkdir()
            (parent / "notes.md").write_bytes(b"after!")
        return data

    monkeypatch.setattr(destination.os, "read", move_after_read)
    with pytest.raises(FsError, match="moved during review"):
        destination.snapshot(destination.review_folder(str(project)), ["docs/notes.md"])
    assert moved


def test_an_alias_replaced_during_read_is_refused(project, monkeypatch):
    link = project / "RULES.md"
    link.symlink_to("original.md")
    original = os.readlink

    def replace_after_read(name, **kwargs):
        result = original(name, **kwargs)
        if name == "RULES.md":
            link.rename(project / "kept-link")
            link.symlink_to("new.md")
        return result

    monkeypatch.setattr(destination.os, "readlink", replace_after_read)
    with pytest.raises(FsError, match="link changed during review"):
        destination.snapshot(destination.review_folder(str(project)), ["RULES.md"])
