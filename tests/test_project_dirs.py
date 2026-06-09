"""Scoped project-directory creation (#335 Phase 3) — the security boundary.

These tests pin the containment guarantees: a folder can only be created directly under a configured
root, and no `..` / absolute / symlink name can escape it. The feature is OFF unless roots are set.
"""

from __future__ import annotations

import os

import pytest

from agent_sessions import project_dirs
from agent_sessions.project_dirs import ProjectDirError, create_project_dir, project_roots


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "code"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(r))
    return r


def test_disabled_without_roots(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_PROJECT_ROOTS", raising=False)
    assert project_roots() == []
    with pytest.raises(ProjectDirError) as e:
        create_project_dir("/anything", "x")
    assert e.value.status == 404


def test_roots_are_realpathed_and_existing_only(tmp_path, monkeypatch):
    real = tmp_path / "a"
    real.mkdir()
    monkeypatch.setenv(
        "AGENT_SESSIONS_PROJECT_ROOTS", os.pathsep.join([str(real), str(tmp_path / "missing"), ""])
    )
    assert project_roots() == [os.path.realpath(real)]  # missing dir dropped, blanks ignored


def test_create_under_root(root):
    cwd = create_project_dir(str(root), "proj-a")
    assert cwd == os.path.realpath(root / "proj-a")
    assert os.path.isdir(cwd)


def test_create_is_idempotent(root):
    (root / "existing").mkdir()
    assert create_project_dir(str(root), "existing") == os.path.realpath(root / "existing")


def test_root_not_allowed_403(root, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(ProjectDirError) as e:
        create_project_dir(str(other), "x")
    assert e.value.status == 403


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "../escape", "x\x00y", "x" * 256])
def test_bad_names_rejected_422(root, name):
    with pytest.raises(ProjectDirError) as e:
        create_project_dir(str(root), name)
    assert e.value.status == 422


def test_symlink_name_cannot_escape_403(root, tmp_path):
    # A single-component name that IS a symlink pointing outside the root must be refused —
    # realpath resolves it out of the root, and the containment check catches it.
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(ProjectDirError) as e:
        create_project_dir(str(root), "link")
    assert e.value.status == 403
    assert project_dirs._valid_name("ok-name") is True
