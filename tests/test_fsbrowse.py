"""HOME-sandboxed folder browse + create (#448). The security boundary for the folder picker:
realpath containment under $HOME (rejects `..`/symlink escape), single-component mkdir names,
dotfiles skipped. ``AGENT_SESSIONS_FS_ROOT`` overrides the root for the test home."""

from __future__ import annotations

import os

import pytest

from agent_sessions import fsbrowse


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(root))
    return root


def _outside(tmp_path, name):
    d = tmp_path / name  # sibling of the home root → NOT contained
    d.mkdir(exist_ok=True)
    return d


def test_list_dirs_lists_subdirs_skips_dotfiles_and_files(home):
    (home / "alpha").mkdir()
    (home / "Beta").mkdir()
    (home / ".hidden").mkdir()
    (home / "afile.txt").write_text("x")
    resolved, dirs = fsbrowse.list_dirs(None)  # default = home
    assert resolved == os.path.realpath(str(home))
    assert [d["name"] for d in dirs] == ["alpha", "Beta"]  # case-insensitive sort; dotfile/file out
    assert all(d["path"] == os.path.join(resolved, d["name"]) for d in dirs)


def test_list_dirs_default_is_home_empty(home):
    assert fsbrowse.list_dirs("") == (os.path.realpath(str(home)), [])


def test_list_dirs_rejects_dotdot_escape(home):
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.list_dirs(str(home / ".." / ".."))
    assert e.value.status == 403


def test_list_dirs_rejects_symlink_escape(home, tmp_path):
    (home / "link").symlink_to(_outside(tmp_path, "outside-ls"))
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.list_dirs(str(home / "link"))  # realpath → outside home → 403
    assert e.value.status == 403


def test_list_dirs_404_when_not_a_directory(home):
    (home / "f.txt").write_text("x")
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.list_dirs(str(home / "f.txt"))
    assert e.value.status == 404


def test_make_dir_creates_and_is_idempotent(home):
    p = fsbrowse.make_dir(str(home), "proj")
    assert p == os.path.join(os.path.realpath(str(home)), "proj")
    assert os.path.isdir(p)
    assert fsbrowse.make_dir(str(home), "proj") == p  # mkdir -p semantics → idempotent


def test_make_dir_nested_parent(home):
    (home / "a").mkdir()
    p = fsbrowse.make_dir(str(home / "a"), "b")
    assert os.path.isdir(p) and p.endswith(f"a{os.sep}b")


@pytest.mark.parametrize("name", ["..", ".", "a/b", "", "   ", "x" * 256, "a\x00b"])
def test_make_dir_rejects_bad_names(home, name):
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.make_dir(str(home), name)
    assert e.value.status == 422


def test_make_dir_rejects_parent_outside_home(home, tmp_path):
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.make_dir(str(_outside(tmp_path, "outside-mk")), "x")
    assert e.value.status == 403


def test_make_dir_rejects_symlinked_parent_escape(home, tmp_path):
    (home / "plink").symlink_to(_outside(tmp_path, "outside-mk2"))
    with pytest.raises(fsbrowse.FsError) as e:
        fsbrowse.make_dir(str(home / "plink"), "x")  # parent realpath escapes home → 403
    assert e.value.status == 403
