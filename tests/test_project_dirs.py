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


# ---- path_within: boundary-aware, filesystem-free (#465) ----------------------


def test_path_within_equal_and_nested():
    assert project_dirs.path_within("/a/b", "/a/b") is True
    assert project_dirs.path_within("/a/b/c", "/a/b") is True
    assert project_dirs.path_within("/a/b/c/d", "/a") is True


def test_path_within_boundary_not_a_substring():
    # /a must NOT contain /a-foo (boundary-aware, not a raw prefix).
    assert project_dirs.path_within("/a-foo", "/a") is False
    assert project_dirs.path_within("/a/b", "/a/bc") is False


def test_path_within_normalizes_and_tolerates_trailing_sep():
    assert project_dirs.path_within("/a/b/../b/c", "/a/b") is True
    assert project_dirs.path_within("/a/b/c", "/a/b/") is True


def test_path_within_empty_is_false():
    assert project_dirs.path_within("", "/a") is False
    assert project_dirs.path_within("/a", "") is False
    assert project_dirs.path_within("", "") is False


def test_path_within_is_filesystem_free():
    # Both paths point at non-existent dirs — a pure lexical test, no stat.
    assert project_dirs.path_within("/nope/gone/child", "/nope/gone") is True


# ---- in_scope (#465) ----------------------------------------------------------


def test_in_scope_no_roots_is_open_but_honors_exclusions():
    # No roots ⇒ feature off ⇒ everything in scope unless excluded.
    assert project_dirs.in_scope("/anywhere", roots=[], exclusions=[]) is True
    assert project_dirs.in_scope("/x/tmp/y", roots=[], exclusions=["/x/tmp"]) is False


def test_in_scope_requires_under_a_root():
    roots = ["/home/u/code"]
    assert project_dirs.in_scope("/home/u/code/proj", roots=roots, exclusions=[]) is True
    assert project_dirs.in_scope("/home/u/code", roots=roots, exclusions=[]) is True
    assert project_dirs.in_scope("/home/u/other", roots=roots, exclusions=[]) is False


def test_in_scope_exclusion_drops_even_under_root():
    roots = ["/home/u/code"]
    excl = ["/home/u/code/scratch"]
    assert project_dirs.in_scope("/home/u/code/scratch/x", roots=roots, exclusions=excl) is False
    assert project_dirs.in_scope("/home/u/code/keep", roots=roots, exclusions=excl) is True


def test_in_scope_curation_beats_roots():
    # #520: explicit curation keeps a cwd that sits outside every root; without it the same cwd
    # is dropped. (curated=False is the pre-#520 behaviour.)
    roots = ["/home/u/code"]
    assert project_dirs.in_scope("/tmp/other", roots=roots, exclusions=[], curated=True) is True
    assert project_dirs.in_scope("/tmp/other", roots=roots, exclusions=[], curated=False) is False


def test_in_scope_exclusion_beats_curation():
    # #520 precedence rule 1: an exclusion wins even over explicit curation.
    roots = ["/home/u/code"]
    assert (
        project_dirs.in_scope("/tmp/other", roots=roots, exclusions=["/tmp/other"], curated=True)
        is False
    )


# ---- effective_roots: prefs ↔ env merge (#465) --------------------------------


def test_effective_roots_prefs_take_precedence_over_env(tmp_path, monkeypatch):
    pref_dir = tmp_path / "pref"
    env_dir = tmp_path / "env"
    pref_dir.mkdir()
    env_dir.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(env_dir))
    monkeypatch.setattr(project_dirs.prefs, "get_project_roots", lambda path=None: [str(pref_dir)])
    assert project_dirs.effective_roots() == [os.path.realpath(pref_dir)]


def test_effective_roots_falls_back_to_env_when_pref_empty(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(env_dir))
    monkeypatch.setattr(project_dirs.prefs, "get_project_roots", lambda path=None: [])
    assert project_dirs.effective_roots() == [os.path.realpath(env_dir)]


def test_effective_roots_drops_missing_dirs_and_dedups(tmp_path, monkeypatch):
    real = tmp_path / "a"
    real.mkdir()
    monkeypatch.delenv("AGENT_SESSIONS_PROJECT_ROOTS", raising=False)
    monkeypatch.setattr(
        project_dirs.prefs,
        "get_project_roots",
        lambda path=None: [str(real), str(tmp_path / "missing"), str(real), ""],
    )
    assert project_dirs.effective_roots() == [os.path.realpath(real)]


def test_effective_roots_set_but_all_missing_does_not_fall_back_to_env(tmp_path, monkeypatch):
    # A SET-but-stale pref (non-empty raw list, all dirs missing) must NOT silently widen scope
    # back to the env roots (Hermes #467). Branching on the RAW pref presence: it wins and
    # normalizes to [] (scope stays off), never the env list.
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_PROJECT_ROOTS", str(env_dir))
    monkeypatch.setattr(
        project_dirs.prefs, "get_project_roots", lambda path=None: [str(tmp_path / "gone")]
    )
    assert project_dirs.effective_roots() == []


def test_project_roots_is_effective_roots(tmp_path, monkeypatch):
    # project_roots() now reads the merged source so the mkdir boundary + discovery share it.
    real = tmp_path / "code"
    real.mkdir()
    monkeypatch.delenv("AGENT_SESSIONS_PROJECT_ROOTS", raising=False)
    monkeypatch.setattr(project_dirs.prefs, "get_project_roots", lambda path=None: [str(real)])
    assert project_roots() == [os.path.realpath(real)]
    # And create_project_dir keeps working against the prefs-sourced root.
    cwd = create_project_dir(str(real), "from-pref")
    assert cwd == os.path.realpath(real / "from-pref")
