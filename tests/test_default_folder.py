"""Per-project default launch folder (#448): create/update auto-adopt the default, releasing the
current default folder is rejected unless a new one is set in the same request, and a legacy entity
with folders but no stored default falls back deterministically to its first (sorted) folder."""

from __future__ import annotations

import pytest

from agent_sessions import projects


@pytest.fixture
def store(tmp_path):
    return tmp_path / "projects.json"


def test_create_adopts_the_default_folder(store):
    p = projects.create("P", default_folder="/home/u/work", path=store)
    assert p.default_folder == "/home/u/work"
    assert "/home/u/work" in p.folders  # the default is always an adopted folder


def test_create_merges_default_into_given_folders(store):
    p = projects.create("P", folders=["/a"], default_folder="/b", path=store)
    assert p.default_folder == "/b"
    assert set(p.folders) == {"/a", "/b"}


def test_create_folderless_has_no_default(store):
    p = projects.create("P", path=store)
    assert p.default_folder == "" and p.folders == ()


def test_create_without_default_falls_back_to_first_folder_on_read(store):
    p = projects.create("P", folders=["/z", "/a"], path=store)  # stored default ""
    assert p.default_folder == "/a"  # read-time fallback to the first (sorted) folder
    assert projects.load(store)[p.id].default_folder == "/a"  # deterministic across reload


def test_update_set_default_adopts_it(store):
    p = projects.create("P", folders=["/a"], default_folder="/a", path=store)
    p2 = projects.update(p.id, default_folder="/b", path=store)
    assert p2.default_folder == "/b" and "/b" in p2.folders


def test_update_releasing_the_default_folder_is_rejected(store):
    p = projects.create("P", default_folder="/a", path=store)  # folders=[/a], default=/a
    with pytest.raises(projects.ProjectError) as e:
        projects.update(p.id, folders=[], path=store)  # drops the default with no replacement
    assert e.value.status == 409


def test_update_release_default_ok_when_new_default_in_same_request(store):
    p = projects.create("P", folders=["/a"], default_folder="/a", path=store)
    p2 = projects.update(p.id, folders=["/b"], default_folder="/b", path=store)
    assert p2.default_folder == "/b" and set(p2.folders) == {"/b"}


def test_update_clear_default_to_empty(store):
    p = projects.create("P", default_folder="/a", path=store)
    p2 = projects.update(p.id, default_folder="", path=store)
    # cleared default → legacy fallback to first folder on read (folders still [/a])
    assert p2.default_folder == "/a" and p2.folders == ("/a",)
