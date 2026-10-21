"""#979: commit the verified index while real Git writers race the operation."""

import subprocess

import pytest

from agent_sessions import files, gitpanel, gitwrite
from agent_sessions.files import FsError


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(tmp_path))
    files.reset_capabilities_for_test()
    gitpanel.reset_flights_for_test()
    p = tmp_path / "repo"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.name", "Test")
    git(p, "config", "user.email", "test@example.invalid")
    yield p
    files.reset_capabilities_for_test()
    gitpanel.reset_flights_for_test()


def staged(repo):
    gitpanel.reset_flights_for_test()
    return gitwrite.git_status(str(repo))["staged_fp"]


def prepare(repo, root=False):
    (repo / "a").write_text("base\n")
    git(repo, "add", "a")
    if not root:
        git(repo, "commit", "-qm", "base")
        (repo / "a").write_text("reviewed\n")
        git(repo, "add", "a")
    return staged(repo)


@pytest.mark.parametrize("root", [False, True])
@pytest.mark.parametrize("same_path", [False, True])
def test_external_stage_immediately_before_write_tree_is_refused(
    repo, monkeypatch, root, same_path
):
    token = prepare(repo, root)
    oid = git(repo, "rev-parse", ":a").stdout.strip()
    before_index = (repo / ".git/index").read_bytes()
    real = gitwrite.run_git_write
    attempts = []

    def racing(r, args, **kw):
        if args[0] == "write-tree":
            name = "a" if same_path else "b"
            (repo / name).write_text("not reviewed\n")
            attempts.append(git(repo, "add", name, check=False))
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", racing)
    out = gitwrite.git_commit(str(repo), "panel", token)
    assert len(attempts) == 1
    assert attempts[0].returncode != 0 and "index.lock" in attempts[0].stderr
    assert git(repo, "rev-parse", "HEAD:a").stdout.strip() == oid
    assert git(repo, "ls-tree", "--name-only", "HEAD").stdout.splitlines() == ["a"]
    assert out["paths"] == ["a"] and out["files"] == 1
    assert out["sha"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert (repo / ".git/index").read_bytes() == before_index
    assert not (repo / ".git/index.lock").exists()
    assert not list((repo / ".git").glob(".battlelab-staged-*"))


def test_soft_reset_before_publication_refuses_without_rewinding(repo, monkeypatch):
    prepare(repo)
    git(repo, "commit", "-qm", "second")
    parent = git(repo, "rev-parse", "HEAD^").stdout.strip()
    (repo / "a").write_text("third\n")
    git(repo, "add", "a")
    token = staged(repo)
    index = (repo / ".git/index").read_bytes()
    real = gitwrite.run_git_write
    fired = []

    def racing(r, args, **kw):
        if args[0] == "update-ref":
            git(repo, "reset", "--soft", parent)
            fired.append(True)
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", racing)
    with pytest.raises(FsError) as err:
        gitwrite.git_commit(str(repo), "panel", token)
    assert err.value.status == 409 and fired
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == parent
    assert (repo / ".git/index").read_bytes() == index
    assert not (repo / ".git/index.lock").exists()


def test_foreign_lock_is_preserved_and_nothing_is_published(repo, monkeypatch):
    token = prepare(repo)
    before = git(repo, "rev-parse", "HEAD").stdout
    index = (repo / ".git/index").read_bytes()
    lock = repo / ".git/index.lock"
    lock.write_bytes(b"another git owns this")
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0)
    with pytest.raises(FsError) as err:
        gitwrite.git_commit(str(repo), "panel", token)
    assert err.value.status == 409
    assert lock.read_bytes() == b"another git owns this"
    assert (repo / ".git/index").read_bytes() == index
    assert git(repo, "rev-parse", "HEAD").stdout == before


def test_delta_failure_prevents_publication_and_releases_lock(repo, monkeypatch):
    token = prepare(repo)
    before = git(repo, "rev-parse", "HEAD").stdout
    real = gitwrite.run_git_bytes
    fired = []

    def broken(r, args, **kw):
        if args[0] == "diff-tree":
            fired.append(True)
            raise gitpanel.GitError("delta unavailable", status=409)
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_bytes", broken)
    with pytest.raises(FsError) as err:
        gitwrite.git_commit(str(repo), "panel", token)
    assert err.value.status == 409 and fired
    assert git(repo, "rev-parse", "HEAD").stdout == before
    assert not (repo / ".git/index.lock").exists()


def test_failed_status_after_publication_keeps_sha(repo, monkeypatch):
    token = prepare(repo)
    real = gitwrite.run_git_write

    def publish(r, args, **kw):
        out = real(r, args, **kw)
        if args[0] == "update-ref":
            monkeypatch.setattr(gitwrite, "_fresh_status", lambda _: 1 / 0)
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", publish)
    out = gitwrite.git_commit(str(repo), "panel", token)
    assert out["sha"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert out["status_error"] and "status" not in out
    assert not (repo / ".git/index.lock").exists()


def test_result_counts_rename_once_and_excludes_unchanged_paths(repo):
    prepare(repo)
    (repo / "unchanged").write_text("not part of the rename\n")
    git(repo, "add", "unchanged")
    git(repo, "commit", "-qm", "second")
    git(repo, "mv", "a", "renamed\nfile")
    out = gitwrite.git_commit(str(repo), "rename", staged(repo))
    assert out["paths"] == ["renamed\nfile"] and out["files"] == 1


@pytest.mark.parametrize("mode_only", [False, True])
def test_stale_fingerprint_refuses_after_taking_lock_and_releases_it(repo, mode_only):
    token = prepare(repo)
    before = git(repo, "rev-parse", "HEAD").stdout
    if mode_only:
        git(repo, "update-index", "--chmod=+x", "a")
    else:
        (repo / "a").write_text("restaged\n")
        git(repo, "add", "a")
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FsError) as err:
        gitwrite.git_commit(str(repo), "panel", token)
    assert err.value.status == 409
    assert git(repo, "rev-parse", "HEAD").stdout == before
    assert (repo / ".git/index").read_bytes() == index
    assert not (repo / ".git/index.lock").exists()


def test_unstaged_only_edit_keeps_the_token_and_stays_out_of_the_commit(repo):
    token = prepare(repo)
    oid = git(repo, "rev-parse", ":a").stdout.strip()
    (repo / "a").write_text("a later worktree version\n")
    assert staged(repo) == token
    out = gitwrite.git_commit(str(repo), "panel", token)
    assert git(repo, "rev-parse", "HEAD:a").stdout.strip() == oid
    assert (repo / "a").read_text() == "a later worktree version\n"
    assert out["paths"] == ["a"] and out["files"] == 1
