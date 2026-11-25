"""#950 Phase 2b — RECENT COMMITS and revert a commit.

Every test runs against a real repository and real git, as Phase 2a's do. The race tests inject the
competing change INSIDE the operation (a spy on the call that sits in the gap), because a race whose
window the test removes cannot be caught by the test.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, gitpanel, gitwrite
from agent_sessions.files import FsError


@pytest.fixture(autouse=True)
def _reset():
    gitpanel.reset_flights_for_test()
    gitpanel.reset_git_bin_for_test()
    files._inflight_total = 0
    files._inflight_by_root.clear()
    yield
    gitpanel.reset_flights_for_test()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "home"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(r))
    files.reset_capabilities_for_test()
    yield r
    files.reset_capabilities_for_test()


def _git(repo, *args, check=True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def repo(root):
    p = root / "proj"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "a.txt").write_text("one\n")
    (p / "b.txt").write_text("one\n")
    _git(p, "add", "a.txt", "b.txt")
    _git(p, "commit", "-q", "-m", "init")
    return p


def head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def commit_all(repo, message) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return head(repo)


def status(repo) -> dict:
    gitpanel.reset_flights_for_test()
    return gitwrite.git_status(str(repo))


def log(repo, limit=None) -> dict:
    gitpanel.reset_flights_for_test()
    return gitpanel.git_log(str(repo), limit)


def revert(repo, commit, **kw) -> dict:
    gitpanel.reset_flights_for_test()
    return gitwrite.git_revert(
        str(repo),
        commit,
        kw.get("head", head(repo)),
        kw.get(
            "branch", _git(repo, "symbolic-ref", "HEAD", check=False).strip() or "refs/heads/main"
        ),
    )


@pytest.fixture()
def shaped(repo) -> str:
    """A commit that MODIFIES a.txt, DELETES b.txt and ADDS c.txt, with an unrelated one on top."""
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").unlink()
    (repo / "c.txt").write_text("new\n")
    target = commit_all(repo, "reshape")
    (repo / "z.txt").write_text("later\n")
    commit_all(repo, "unrelated")
    return target


# --------------------------------------------------------------------------- RECENT COMMITS


def test_log_lists_first_parent_history_newest_first(repo, shaped):
    lg = log(repo)
    assert lg["head"] == head(repo)
    assert [c["subject"] for c in lg["commits"]] == ["unrelated", "reshape", "init"]
    assert lg["commits"][1]["sha"] == shaped
    assert [c["parents"] for c in lg["commits"]] == [1, 1, 0]
    # No upstream: `pushed` is ABSENT — "not known", never "not pushed".
    assert all("pushed" not in c for c in lg["commits"])


def test_log_marks_what_the_upstream_already_has(repo, shaped):
    _git(repo, "update-ref", "refs/remotes/origin/main", shaped)
    _git(repo, "config", "branch.main.remote", "origin")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")
    pushed = {c["subject"]: c.get("pushed") for c in log(repo)["commits"]}
    assert pushed == {"unrelated": False, "reshape": True, "init": True}


def test_log_limit_is_clamped_to_the_panel_window(repo):
    for i in range(gitpanel.LOG_LIMIT + 3):
        (repo / "a.txt").write_text(f"{i}\n")
        commit_all(repo, f"c{i}")
    assert len(log(repo, "5")["commits"]) == 5
    assert len(log(repo, "999")["commits"]) == gitpanel.LOG_LIMIT
    assert len(log(repo)["commits"]) == gitpanel.LOG_LIMIT


def test_log_of_a_branch_with_no_commit_yet_is_empty(root):
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    lg = log(p)
    assert lg["head"] is None and lg["commits"] == []


# --------------------------------------------------------------------------- revert


def test_revert_undoes_a_modify_delete_add_commit_and_keeps_every_displaced_version(repo, shaped):
    before = head(repo)
    r = revert(repo, shaped)
    assert r["index"] == "settled" and r["worktree"] == "settled", r
    assert r["sha"] == head(repo) and r["reverted_commit"] == shaped
    assert _git(repo, "rev-parse", "HEAD^").strip() == before
    assert (repo / "a.txt").read_text() == "one\n"
    assert (repo / "b.txt").read_text() == "one\n"
    assert not (repo / "c.txt").exists()
    assert (repo / "z.txt").read_text() == "later\n"
    # Index and worktree both match the new commit: nothing is left to show.
    assert status(repo)["entries"] == []
    # The deleted file was displaced into git, never unlinked unpreserved.
    assert _git(repo, "cat-file", "-p", r["recoverable"]["c.txt"][0]) == "new\n"
    assert _git(repo, "cat-file", "-p", r["recoverable"]["a.txt"][0]) == "two\n"
    body = _git(repo, "log", "-1", "--format=%B")
    assert 'Revert "reshape"' in body and shaped in body


def test_head_moving_before_publication_publishes_nothing(repo, shaped, monkeypatch):
    seen = head(repo)
    real = gitwrite.revert_tree

    def agent_commits_meanwhile(r, commit, h):
        out = real(r, commit, h)
        (repo / "z.txt").write_text("agent\n")
        _git(repo, "commit", "-qam", "agent")
        return out

    monkeypatch.setattr(gitwrite, "revert_tree", agent_commits_meanwhile)
    with pytest.raises(FsError) as e:
        revert(repo, shaped, head=seen)
    assert e.value.status == 409 and "moved" in str(e.value)
    assert _git(repo, "log", "-1", "--format=%s").strip() == "agent"
    assert (repo / "a.txt").read_text() == "two\n" and (repo / "c.txt").exists()


def test_a_head_the_panel_did_not_show_is_refused_before_anything_is_computed(repo, shaped):
    with pytest.raises(FsError) as e:
        revert(repo, shaped, head=shaped)  # a real commit, just not the tip
    assert e.value.status == 409 and "moved" in str(e.value)


def test_a_worktree_edit_before_materialisation_is_left_and_reported(repo, shaped, monkeypatch):
    real = gitwrite._index_cas

    def agent_edits_then_settle(r, changes, *rest, **kw):
        (repo / "a.txt").write_text("agent edit\n")
        return real(r, changes, *rest, **kw)

    monkeypatch.setattr(gitwrite, "_index_cas", agent_edits_then_settle)
    r = revert(repo, shaped)
    assert r["sha"] == head(repo)  # the commit exists
    assert r["worktree"] == "pending" and r["worktree_left"] == ["a.txt"]
    assert (repo / "a.txt").read_text() == "agent edit\n"  # left exactly as the agent left it
    assert (repo / "b.txt").read_text() == "one\n" and not (repo / "c.txt").exists()


def test_a_failure_after_publication_is_reported_not_raised(repo, shaped, monkeypatch):
    def broken(r, changes, *rest, **kw):
        raise OSError(5, "index went away")

    monkeypatch.setattr(gitwrite, "_index_cas", broken)
    r = revert(repo, shaped)
    assert r["sha"] == head(repo)
    assert r["index"] == "pending" and set(r["index_left"]) == {"a.txt", "b.txt", "c.txt"}


def test_a_conflicting_revert_writes_no_ref_index_or_worktree(repo, shaped):
    (repo / "a.txt").write_text("three\n")
    before = commit_all(repo, "builds on reshape")
    index = _git(repo, "ls-files", "-s")
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "conflicts" in str(e.value) and "a.txt" in str(e.value)
    assert head(repo) == before
    assert _git(repo, "ls-files", "-s") == index
    assert (repo / "a.txt").read_text() == "three\n" and (repo / "c.txt").exists()


def test_a_dirty_tree_is_refused_with_the_count(repo, shaped):
    (repo / "z.txt").write_text("dirty\n")
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "1 uncommitted change" in str(e.value)


def test_the_clean_check_binds_inside_the_lock(repo, shaped, monkeypatch):
    before = head(repo)
    (repo / "z.txt").write_text("dirty\n")
    # The early check is a cached fast path; pretend it saw a clean tree.
    monkeypatch.setattr(gitwrite, "_is_clean", lambda r: (True, 0))
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "uncommitted" in str(e.value)
    assert head(repo) == before and (repo / "z.txt").read_text() == "dirty\n"


def test_only_a_listed_recent_commit_can_be_reverted(repo):
    _git(repo, "checkout", "-qb", "side")
    (repo / "a.txt").write_text("side\n")
    foreign = commit_all(repo, "on side")
    _git(repo, "checkout", "-q", "main")
    with pytest.raises(FsError) as e:
        revert(repo, foreign)
    assert e.value.status == 409 and "recent commits" in str(e.value)

    for i in range(gitpanel.LOG_LIMIT):
        (repo / "b.txt").write_text(f"{i}\n")
        commit_all(repo, f"c{i}")
    oldest = _git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    with pytest.raises(FsError) as e:
        revert(repo, oldest)
    assert e.value.status == 409 and "recent commits" in str(e.value)


@pytest.mark.parametrize(("commit", "head_value"), [("nope", None), ("a" * 40, ""), (None, None)])
def test_malformed_requests_are_422(repo, commit, head_value):
    with pytest.raises(FsError) as e:
        gitwrite.git_revert(str(repo), commit, head(repo) if head_value is None else head_value)
    assert e.value.status == 422


def test_the_first_commit_and_a_merge_are_refused(repo):
    with pytest.raises(FsError) as e:
        revert(repo, head(repo))
    assert e.value.status == 409 and "first commit" in str(e.value)

    _git(repo, "checkout", "-qb", "side")
    (repo / "b.txt").write_text("side\n")
    commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    commit_all(repo, "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    with pytest.raises(FsError) as e:
        revert(repo, head(repo))
    assert e.value.status == 409 and "merge" in str(e.value)


@pytest.mark.parametrize("shape", ["symlink", "mode", "gitlink"])
def test_symlink_submodule_and_mode_changes_are_refused(repo, shape):
    if shape == "symlink":
        os.symlink("a.txt", repo / "link")
        _git(repo, "add", "link")
        why = "symbolic link"
    elif shape == "mode":
        os.chmod(repo / "a.txt", 0o755)
        _git(repo, "add", "a.txt")
        why = "file mode"
    else:
        (repo / "sub").mkdir()
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head(repo)},sub")
        why = "submodule"
    _git(repo, "commit", "-q", "-m", shape)
    before = head(repo)
    with pytest.raises(FsError) as e:
        revert(repo, before)
    assert e.value.status == 409 and why in str(e.value)
    assert head(repo) == before


@pytest.mark.parametrize("bound_via", ["gitattributes", "info_attributes"])
def test_a_repository_merge_driver_never_runs_during_a_revert(repo, root, bound_via):
    """The measured vector: a repo-configured merge driver RAN under `merge-tree` in the real gitdir
    and resolved a conflict. Here it must neither run nor resolve anything."""
    sentinel = root / "DRIVER_RAN"
    _git(repo, "config", "merge.boom.driver", f"touch {sentinel}")
    if bound_via == "gitattributes":
        (repo / ".gitattributes").write_text("a.txt merge=boom\n")
        commit_all(repo, "attributes")
    else:
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("a.txt merge=boom\n")
    (repo / "a.txt").write_text("two\n")
    target = commit_all(repo, "change a")
    (repo / "a.txt").write_text("three\n")
    commit_all(repo, "change a again")
    with pytest.raises(FsError) as e:
        revert(repo, target)
    assert e.value.status == 409 and "conflicts" in str(e.value)
    assert not sentinel.exists()


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def _hdr(c, cfg):
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_the_revert_route_needs_csrf(client, repo, shaped):
    r = client.post("/api/git/revert", json={"path": str(repo), "commit": shaped})
    assert r.status_code == 403


def test_log_and_revert_through_the_server(client, repo, shaped, auth_cfg):
    h = _hdr(client, auth_cfg)
    lg = client.get("/api/git/log", params={"path": str(repo)}).json()
    target = next(c for c in lg["commits"] if c["subject"] == "reshape")
    r = client.post(
        "/api/git/revert",
        json={
            "path": str(repo),
            "commit": target["sha"],
            "head": lg["head"],
            "branch": "refs/heads/" + lg["branch"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["reverted_commit"] == target["sha"] and body["index"] == body["worktree"] == "settled"
    )

    # The same request again is now bound to a head that has moved.
    r = client.post(
        "/api/git/revert",
        json={
            "path": str(repo),
            "commit": target["sha"],
            "head": lg["head"],
            "branch": "refs/heads/" + lg["branch"],
        },
        headers=h,
    )
    assert r.status_code == 409 and "moved" in r.json()["detail"]


# --------------------------------------------------------------------------- worktree settlement


def _revert_leaving(repo, monkeypatch, shaped, name="a.txt"):
    """A revert that could not WRITE one file — committed and indexed, the file still pre-revert."""
    real = gitwrite._restore_from_index

    def refuse(r, n, entry, saved=None):
        if n == name:
            raise FsError("the disk said no", status=409)
        return real(r, n, entry, saved)

    with monkeypatch.context() as m:
        m.setattr(gitwrite, "_restore_from_index", refuse)
        r = revert(repo, shaped)
    assert r["worktree"] == "pending" and r["worktree_left"] == [name]
    return r


def test_a_file_a_revert_could_not_write_is_reported_and_settle_finishes_it(
    repo, shaped, monkeypatch
):
    _revert_leaving(repo, monkeypatch, shaped)
    st = status(repo)
    assert st["unsettled_worktree"] == ["a.txt"]
    assert st["unsettled"] == []  # the index is at the commit; only the file is behind
    s = gitwrite.git_settle(str(repo), head(repo))
    assert s["worktree"] == "settled" and s["worktree_paths"] == ["a.txt"], s
    assert (repo / "a.txt").read_text() == "one\n"
    assert _git(repo, "cat-file", "-p", s["recoverable"]["a.txt"][0]) == "two\n"
    after = status(repo)
    assert after["unsettled_worktree"] == [] and after["entries"] == []


def test_an_edit_is_not_unsettled_and_settle_leaves_it_alone(repo, shaped, monkeypatch):
    _revert_leaving(repo, monkeypatch, shaped)
    (repo / "a.txt").write_text("my own edit\n")
    assert status(repo)["unsettled_worktree"] == []
    s = gitwrite.git_settle(str(repo), head(repo))
    assert s["worktree_paths"] == []
    assert (repo / "a.txt").read_text() == "my own edit\n"


def test_a_reversal_staged_on_purpose_keeps_its_worktree_on_settle(repo):
    (repo / "a.txt").write_text("two\n")
    tip = commit_all(repo, "two")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    st = status(repo)
    assert st["unsettled"] == ["a.txt"] and st["unsettled_worktree"] == []
    s = gitwrite.git_settle(str(repo), tip)
    assert s["worktree_paths"] == []
    assert (repo / "a.txt").read_text() == "one\n"  # only the index moved


def test_the_worktree_check_hashes_bytes_without_filters_and_writes_nothing(
    repo, shaped, monkeypatch, root
):
    sentinel = root / "FILTER_RAN"
    _git(repo, "config", "filter.boom.clean", f"touch {sentinel}; cat")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("a.txt filter=boom\n")
    _revert_leaving(repo, monkeypatch, shaped)
    sentinel.unlink(missing_ok=True)  # the revert's own staging may run the repo's clean filter
    seen: list[list[str]] = []
    real = gitpanel._run_git

    def spy(r, args, *, cwd, **kw):
        seen.append(args)
        return real(r, args, cwd=cwd, **kw)

    monkeypatch.setattr(gitpanel, "_run_git", spy)
    objects_before = sorted(os.listdir(repo / ".git" / "objects"))
    assert status(repo)["unsettled_worktree"] == ["a.txt"]
    hashes = [a for a in seen if a[0] == "hash-object"]
    assert hashes and all("--no-filters" in a and "-w" not in a for a in hashes)
    assert not sentinel.exists()
    assert sorted(os.listdir(repo / ".git" / "objects")) == objects_before


def test_the_worktree_check_answers_none_past_its_bounds(repo, shaped, monkeypatch):
    _revert_leaving(repo, monkeypatch, shaped)
    monkeypatch.setattr(gitpanel, "UNSETTLED_WORKTREE_MAX", 0)
    assert status(repo)["unsettled_worktree"] is None
    monkeypatch.setattr(gitpanel, "UNSETTLED_WORKTREE_MAX", 50)
    monkeypatch.setattr(gitpanel, "UNSETTLED_WORKTREE_BYTES", 1)
    assert status(repo)["unsettled_worktree"] is None


#: git's lock file, relative to a test repository.
LOCK = os.path.join(".git", "index.lock")


def _try_git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _at_update_ref(monkeypatch, act, *, after: bool = False) -> list:
    """Run `act` at the real publication boundary: just before (or just after) `update-ref`."""
    real = gitwrite.run_git_write
    fired: list = []

    def spy(r, args, *a, **kw):
        boundary = bool(args) and args[0] == "update-ref" and not fired
        if boundary and not after:
            fired.append(act())
        out = real(r, args, *a, **kw)
        if boundary and after:
            fired.append(act())
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    return fired


# ------------------------------------------------------------------ review 4829, in 2b
#
# Hermes found these in Phase 2a's writes (#964, review 4829); each is asked of the revert and of
# SETTLE's worktree half here, with the competing change injected inside the operation.


def _make_unmerged(repo, name: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """A real conflict in the index for ``name``: stages 1-3, no stage 0, as a merge leaves.

    ``check=False`` returns git's answer instead of asserting it: the lock tests want to see
    `update-index` refused because this process holds index.lock."""
    oids = [
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=text,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for text in ("base\n", "ours\n", "theirs\n")
    ]
    info = f"0 {'0' * 40}\t{name}\n" + "".join(
        f"100644 {oid} {stage}\t{name}\n" for stage, oid in enumerate(oids, 1)
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), "update-index", "--index-info"],
        input=info,
        capture_output=True,
        text=True,
    )
    if check:
        assert proc.returncode == 0, proc.stderr
        assert _git(repo, "ls-files", "-u", "--", name).strip()
    return proc


def _empty_merge_in_progress(repo) -> None:
    """MERGE_HEAD with a clean tree: an empty commit merged with --no-commit."""
    tip = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo, "switch", "-q", "-c", "empty-side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    _git(repo, "switch", "-q", tip)
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "empty-side")
    assert (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_a_merge_in_progress_refuses_a_revert_at_admission(repo, shaped):
    _empty_merge_in_progress(repo)
    before = head(repo)
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "merge" in str(e.value)
    assert head(repo) == before and (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "a.txt").read_text() == "two\n"


def test_a_merge_starting_just_before_publication_publishes_nothing(repo, shaped, monkeypatch):
    """The merge lands at `commit-tree`, BEFORE the revert takes index.lock; the operation state
    read under the lock sees it, so nothing is published."""
    before = head(repo)
    real = gitwrite._commit_tree

    def merge_meanwhile(*a, **kw):
        out = real(*a, **kw)
        _empty_merge_in_progress(repo)
        return out

    monkeypatch.setattr(gitwrite, "_commit_tree", merge_meanwhile)
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "merge" in str(e.value)
    assert head(repo) == before and (repo / "a.txt").read_text() == "two\n"


def test_a_branch_switch_just_before_publication_publishes_nothing(repo, shaped, monkeypatch):
    """The switch lands at `commit-tree`, BEFORE the revert takes index.lock; the branch read under
    the lock sees it, so nothing is published. A switch that lands later is refused by git itself
    (the `update-ref` tests below)."""
    before = head(repo)
    _git(repo, "branch", "side")
    real = gitwrite._commit_tree

    def switch_meanwhile(*a, **kw):
        out = real(*a, **kw)
        _git(repo, "switch", "-q", "side")
        return out

    monkeypatch.setattr(gitwrite, "_commit_tree", switch_meanwhile)
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "switched" in str(e.value)
    assert _git(repo, "rev-parse", "main").strip() == before
    assert _git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/side"
    assert (repo / "a.txt").read_text() == "two\n" and (repo / "c.txt").exists()


def test_a_ref_only_branch_change_after_publication_leaves_the_other_checkout_alone(
    repo, shaped, monkeypatch
):
    """Was: a `git switch side` right after `update-ref`. The revert holds index.lock there now, so
    that switch is refused by git (next test). What the lock cannot stop is a ref-only write:
    `symbolic-ref` points HEAD at `side` without touching the index. The index compare-and-swap and
    the worktree half both re-check the branch, so `side` gets nothing."""
    before = head(repo)
    _git(repo, "branch", "side")
    _at_update_ref(
        monkeypatch, lambda: _git(repo, "symbolic-ref", "HEAD", "refs/heads/side"), after=True
    )
    r = revert(repo, shaped)
    assert _git(repo, "rev-parse", "main").strip() == r["sha"]  # published on main
    assert _git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/side"
    assert r["index"] == "pending" and r["worktree"] == "pending", r
    assert "no longer on `main`" in r["worktree_reason"]
    # side's checkout is exactly as it was: its index and files still match side's commit.
    assert _git(repo, "rev-parse", "side").strip() == before
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    assert (repo / "a.txt").read_text() == "two\n" and (repo / "c.txt").exists()
    assert not (repo / LOCK).exists()


def test_a_branch_switch_at_update_ref_is_refused_by_git_and_the_revert_settles_on_its_branch(
    repo, shaped, monkeypatch
):
    """The gap Hermes found in 2a's commit-paths (#964, review 4833, 3), in the revert: a `git
    switch` right before `update-ref`, after every last-moment read, still published to the old
    branch. The revert holds index.lock from its final checks on, so the switch itself fails."""
    before = head(repo)
    _git(repo, "branch", "side")
    fired = _at_update_ref(monkeypatch, lambda: _try_git(repo, "switch", "-q", "side"))
    r = revert(repo, shaped)
    (switch,) = fired
    assert switch.returncode != 0 and "index.lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "main").strip() == r["sha"] != before
    assert _git(repo, "rev-parse", "side").strip() == before
    assert r["index"] == "settled" and r["worktree"] == "settled", r
    assert (repo / "a.txt").read_text() == "one\n" and not (repo / "c.txt").exists()
    assert not (repo / LOCK).exists()


def test_a_conflicting_merge_at_update_ref_never_reaches_the_reverted_checkout(
    repo, shaped, monkeypatch
):
    """Review 4833, 3, in the revert: a conflicting merge run right before `update-ref` wrote
    unmerged stages and conflict markers into the checkout the revert then materialised into. With
    index.lock held, git cannot write the index, so no stage and no marker reaches the checkout."""
    _git(repo, "switch", "-q", "-c", "side", shaped)
    (repo / "z.txt").write_text("side\n")
    commit_all(repo, "side adds z")
    _git(repo, "switch", "-q", "main")
    before = head(repo)
    fired = _at_update_ref(monkeypatch, lambda: _try_git(repo, "merge", "side"))
    r = revert(repo, shaped)
    (merge,) = fired
    assert merge.returncode != 0
    assert _git(repo, "ls-files", "-u") == "" and (repo / "z.txt").read_text() == "later\n"
    assert r["sha"] == head(repo) != before
    # Measured on git 2.43: the merge fails with "Unable to write index" but has already written
    # MERGE_HEAD — the one effect the lock does not stop — so the revert's index and files are left
    # pending rather than settled over a merge in progress.
    assert "Unable to write index" in merge.stderr
    assert (repo / ".git" / "MERGE_HEAD").exists()
    assert r["index"] == "pending" and "merge" in r["index_reason"], r
    assert r["worktree"] == "pending" and (repo / "a.txt").read_text() == "two\n", r
    assert not (repo / LOCK).exists()


def test_an_agent_commit_right_after_the_revert_is_published_is_refused_by_git(
    repo, shaped, monkeypatch
):
    """Review 4833, 2b, in the revert: right after `update-ref` the index still holds the pre-revert
    entries, so an ordinary `git commit` recorded them as a newer commit that silently undid the
    revert — and settlement then installed the revert's entries over it."""
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "commit", "-q", "-m", "agent"), after=True
    )
    r = revert(repo, shaped)
    (commit,) = fired
    assert commit.returncode != 0 and "index.lock" in commit.stderr
    assert head(repo) == r["sha"] and r["index"] == "settled" and r["worktree"] == "settled", r
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    assert (repo / "a.txt").read_text() == "one\n"
    assert not (repo / LOCK).exists()


def test_a_conflict_written_right_after_publication_is_refused_by_git(repo, shaped, monkeypatch):
    """Was: stages 1-3 written right after `update-ref`, checking the materialisation stepped around
    them. The lock is held there now, so git refuses the write itself and the revert settles."""
    fired = _at_update_ref(
        monkeypatch, lambda: _make_unmerged(repo, "a.txt", check=False), after=True
    )
    r = revert(repo, shaped)
    (proc,) = fired
    assert proc.returncode != 0 and "index.lock" in proc.stderr
    assert _git(repo, "ls-files", "-u") == ""
    assert r["index"] == "settled" and r["worktree"] == "settled", r
    assert (repo / "a.txt").read_text() == "one\n"
    assert not (repo / LOCK).exists()


def test_a_conflict_landing_as_the_index_is_published_is_never_written_over(
    repo, shaped, monkeypatch
):
    """Publishing the index consumes git's lock — the lock file becomes the index — so there is an
    instant before the revert takes it again for the working tree. A conflict written in exactly
    that instant is seen under the retaken lock, and the file is left as it is."""
    real = gitwrite._index_cas
    done: list = []

    def cas_then_conflict(r, changes, *rest, **kw):
        out = real(r, changes, *rest, **kw)
        if not done:
            done.append(_make_unmerged(repo, "a.txt"))
        return out

    monkeypatch.setattr(gitwrite, "_index_cas", cas_then_conflict)
    r = revert(repo, shaped)
    assert r["sha"] == head(repo)
    assert r["worktree"] == "pending" and "a.txt" in r["worktree_left"], r
    assert (repo / "a.txt").read_text() == "two\n"  # the conflicted file was not replaced
    assert _git(repo, "ls-files", "-u", "--", "a.txt").strip()  # and its stages are intact
    assert not (repo / LOCK).exists()


def test_a_switch_while_the_revert_writes_its_files_is_refused_by_git(repo, shaped, monkeypatch):
    """The lock is taken again after the index is published, and held while files are written: a
    switch attempted then fails instead of moving the checkout underneath the writes."""
    _git(repo, "branch", "side")
    real = gitwrite._snapshot_for_index
    fired: list = []

    def switch_during_worktree(r, names):
        if not fired:
            fired.append(_try_git(repo, "switch", "-q", "side"))
        return real(r, names)

    monkeypatch.setattr(gitwrite, "_snapshot_for_index", switch_during_worktree)
    r = revert(repo, shaped)
    (switch,) = fired
    assert switch.returncode != 0 and "index.lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert r["worktree"] == "settled" and (repo / "a.txt").read_text() == "one\n", r
    assert not (repo / LOCK).exists()


def test_a_revert_whose_index_lands_but_cannot_be_flushed_says_so_and_keeps_every_id(
    repo, shaped, monkeypatch
):
    """Review 4833, 4, in the revert: the directory fsync after the real index rename failed. The
    index DID move, so the result is settled with `index_durable: false` — not "not updated" — and
    every displaced id is still returned."""
    real_rename, real_fsync = os.rename, os.fsync
    seen = {"renamed": False, "failed": False}

    def rename(src, dst, *args, **kw):
        out = real_rename(src, dst, *args, **kw)
        if dst == "index" and kw.get("dst_dir_fd") is not None:
            seen["renamed"] = True
        return out

    def fsync(fd):
        if seen["renamed"] and not seen["failed"] and _stat.S_ISDIR(os.fstat(fd).st_mode):
            seen["failed"] = True
            raise OSError(errno.EIO, "injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "fsync", fsync)
    r = revert(repo, shaped)
    assert seen["failed"]
    assert r["index"] == "settled" and r["index_durable"] is False, r
    assert "confirmed on disk" in r["index_reason"]
    assert r["worktree"] == "settled" and (repo / "a.txt").read_text() == "one\n"
    assert _git(repo, "cat-file", "-p", r["recoverable"]["a.txt"][0]) == "two\n"
    assert not (repo / LOCK).exists()


def test_an_index_lock_held_by_another_git_publishes_no_revert_and_is_left_alone(
    repo, shaped, monkeypatch
):
    before = head(repo)
    (repo / LOCK).write_bytes(b"someone else's\n")
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    with pytest.raises(FsError) as e:
        revert(repo, shaped)
    assert e.value.status == 409 and "busy" in str(e.value)
    assert head(repo) == before
    assert (repo / LOCK).read_bytes() == b"someone else's\n"
    assert (repo / "a.txt").read_text() == "two\n"


def test_a_commit_changing_a_name_that_is_not_utf8_is_never_reverted(repo):
    """2a decodes diff paths strictly (#964, review 4833, 1); the revert parses its commit's shape
    with the same parser, so a path git cannot name in UTF-8 is a 409 before anything is
    computed."""
    bad = os.path.join(os.fsencode(str(repo)), b"bad\xff")
    with open(bad, "wb") as f:
        f.write(b"one\n")
    commit_all(repo, "add bad")
    with open(bad, "wb") as f:
        f.write(b"two\n")
    target = commit_all(repo, "touch bad")
    with pytest.raises(FsError) as e:
        revert(repo, target)
    assert e.value.status == 409 and "UTF-8" in str(e.value)
    assert head(repo) == target
    with open(bad, "rb") as f:
        assert f.read() == b"two\n"


def test_an_unexpected_error_after_publication_is_a_partial_result_with_every_id(
    repo, shaped, monkeypatch
):
    real = gitwrite._restore_from_index

    def breaks_on_b(r, n, entry, saved=None):
        if n == "b.txt":
            raise ValueError("something nobody expected")
        return real(r, n, entry, saved)

    monkeypatch.setattr(gitwrite, "_restore_from_index", breaks_on_b)
    r = revert(repo, shaped)
    assert r["sha"] == head(repo) and r["index"] == "settled", r
    assert r["worktree"] == "pending" and "nobody expected" in r["worktree_reason"]
    assert set(r["worktree_left"]) == {"b.txt", "c.txt"}
    assert (repo / "a.txt").read_text() == "one\n"
    assert _git(repo, "cat-file", "-p", r["recoverable"]["a.txt"][0]) == "two\n"


def test_a_status_read_failing_after_publication_still_returns_the_revert(
    repo, shaped, monkeypatch
):
    published: list[bool] = []
    real_run = gitwrite.run_git_write
    real_status = gitwrite._fresh_status

    def mark(r, args, *a, **kw):
        out = real_run(r, args, *a, **kw)
        if args[:1] == ["update-ref"]:
            published.append(True)
        return out

    def status_after(r):
        if published:
            raise gitwrite.GitError("status went away", status=500)
        return real_status(r)

    monkeypatch.setattr(gitwrite, "run_git_write", mark)
    monkeypatch.setattr(gitwrite, "_fresh_status", status_after)
    r = revert(repo, shaped)
    assert r["sha"] == head(repo) and r["worktree"] == "settled", r
    assert "status" not in r and "status went away" in r["status_error"]


def test_settle_holds_the_index_lock_so_a_switch_before_its_index_write_is_refused(
    repo, shaped, monkeypatch
):
    """Was: a `git switch side` injected just before SETTLE's compare-and-swap, asserting the
    worktree half then left side's files alone. SETTLE holds index.lock from validating HEAD
    (#964, review 4833), so that switch is refused by git and SETTLE finishes on its own branch."""
    _revert_leaving(repo, monkeypatch, shaped)
    tip = head(repo)
    _git(repo, "branch", "side")
    real = gitwrite._index_cas
    fired: list = []

    def switch_then_cas(r, changes, *rest, **kw):
        if not fired:
            fired.append(_try_git(repo, "switch", "-q", "side"))
        return real(r, changes, *rest, **kw)

    monkeypatch.setattr(gitwrite, "_index_cas", switch_then_cas)
    s = gitwrite.git_settle(str(repo), tip)
    (switch,) = fired
    assert switch.returncode != 0 and "index.lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert s["worktree"] == "settled" and s["worktree_paths"] == ["a.txt"], s
    assert (repo / "a.txt").read_text() == "one\n"
    assert not (repo / LOCK).exists()


def test_settle_holds_head_so_a_ref_only_write_before_its_index_write_is_refused(
    repo, shaped, monkeypatch
):
    """Was: `symbolic-ref` pointing HEAD at `side` just before SETTLE's compare-and-swap — the one
    branch change index.lock alone could not stop, so the worktree half had to leave side's files
    alone. SETTLE now holds HEAD.lock and the branch's ref lock from validating HEAD (#964, review
    4847), so git refuses that write and SETTLE finishes on its own branch."""
    _revert_leaving(repo, monkeypatch, shaped)
    tip = head(repo)
    _git(repo, "branch", "side")
    real = gitwrite._index_cas
    fired: list = []

    def ref_write_then_cas(r, changes, *rest, **kw):
        if not fired:
            fired.append(_try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side"))
        return real(r, changes, *rest, **kw)

    monkeypatch.setattr(gitwrite, "_index_cas", ref_write_then_cas)
    s = gitwrite.git_settle(str(repo), tip)
    (proc,) = fired
    assert proc.returncode != 0 and ".lock" in proc.stderr, proc
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert s["worktree"] == "settled" and s["worktree_paths"] == ["a.txt"], s
    assert (repo / "a.txt").read_text() == "one\n"
    assert _no_locks(repo) == []


def _settle_publishing_the_index(repo, monkeypatch, shaped) -> str:
    """A revert that left a.txt's FILE behind, plus b.txt's index moved back to the parent's entry
    (absent), so SETTLE both publishes an index change and then writes a file."""
    _revert_leaving(repo, monkeypatch, shaped)
    _git(repo, "rm", "-q", "--cached", "b.txt")
    return head(repo)


def test_settle_never_writes_over_a_conflict_that_lands_as_its_index_is_published(
    repo, shaped, monkeypatch
):
    """Was: a conflict written right after SETTLE's compare-and-swap. Publishing the index consumes
    git's lock (the lock file becomes the index), so in that instant a conflict can still land;
    SETTLE takes the lock again before writing files, and the UNMERGED fence under it leaves the
    file exactly as it is."""
    tip = _settle_publishing_the_index(repo, monkeypatch, shaped)
    real = gitwrite._index_cas
    done: list = []

    def cas_then_conflict(r, changes, *rest, **kw):
        out = real(r, changes, *rest, **kw)
        if not done:
            done.append(_make_unmerged(repo, "a.txt"))
        return out

    monkeypatch.setattr(gitwrite, "_index_cas", cas_then_conflict)
    s = gitwrite.git_settle(str(repo), tip)
    assert done and "b.txt" in s["paths"], s
    assert s["worktree"] == "pending" and "a.txt" in s["worktree_left"], s
    assert (repo / "a.txt").read_text() == "two\n"
    assert _git(repo, "ls-files", "-u", "--", "a.txt").strip()
    assert not (repo / LOCK).exists()


def test_settle_takes_the_lock_again_so_a_conflict_attempted_while_it_writes_files_is_refused(
    repo, shaped, monkeypatch
):
    """After publishing the index, SETTLE re-takes index.lock and holds it while the worktree half
    writes: a conflict attempted then is refused by git, and the file is settled."""
    tip = _settle_publishing_the_index(repo, monkeypatch, shaped)
    real = gitwrite._worktree_entries_raw
    fired: list = []

    def conflict_while_writing(r, names):
        if not fired:
            fired.append(_make_unmerged(repo, "a.txt", check=False))
        return real(r, names)

    monkeypatch.setattr(gitwrite, "_worktree_entries_raw", conflict_while_writing)
    s = gitwrite.git_settle(str(repo), tip)
    (proc,) = fired
    assert proc.returncode != 0 and "index.lock" in proc.stderr
    assert s["index"] == "settled" and "b.txt" in s["paths"], s
    assert s["worktree"] == "settled" and s["worktree_paths"] == ["a.txt"], s
    assert (repo / "a.txt").read_text() == "one\n" and _git(repo, "ls-files", "-u") == ""
    assert not (repo / LOCK).exists()


# ------------------------------------------------------------------ review 4847, in 2b
#
# index.lock does not stop a writer that moves only refs (`git reset --soft`, `symbolic-ref`).
# After its own `update-ref` the revert takes git's HEAD.lock and the branch's ref lock and holds
# them through the index and the worktree; SETTLE holds them from validating HEAD through its
# worktree half.


def _no_locks(repo) -> list[str]:
    """Every git lock file left anywhere in the repository's git directory — must be none."""
    left = []
    for dirpath, _dirs, filenames in os.walk(repo / ".git"):
        left += [os.path.join(dirpath, f) for f in filenames if f.endswith(".lock")]
    return left


def _at_scratch_index(monkeypatch, act) -> list:
    """Run `act` while the rewritten entries are installed into the scratch copy of the index."""
    real = gitwrite._install_index_entries
    fired: list = []

    def spy(r, entries, index_file=None):
        if index_file and ".battlelab-index-" in index_file and not fired:
            fired.append(act())
        return real(r, entries, index_file=index_file)

    monkeypatch.setattr(gitwrite, "_install_index_entries", spy)
    return fired


def _ref_writer(repo, how: str, target: str):
    """A git command that moves only refs: a soft reset to `target`, or HEAD pointed at `side`."""
    if how == "reset":
        return lambda: _try_git(repo, "reset", "--soft", target)
    return lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side")


@pytest.mark.parametrize("how", ["reset", "symbolic-ref"])
def test_a_ref_writer_while_the_revert_rewrites_its_index_is_refused_by_git(
    repo, shaped, monkeypatch, how
):
    """Hermes on #964 (review 4847), asked of the revert: index.lock does not exclude `git reset
    --soft` or `symbolic-ref`. One landing while the revert rewrote its index moved HEAD, and the
    revert still settled. It now holds HEAD.lock and the branch's ref lock from right after its own
    `update-ref`, so git refuses the ref writer."""
    before = head(repo)
    _git(repo, "branch", "side")
    fired = _at_scratch_index(monkeypatch, _ref_writer(repo, how, before))
    r = revert(repo, shaped)
    (proc,) = fired
    assert proc.returncode != 0 and ".lock" in proc.stderr, proc
    assert head(repo) == r["sha"] != before
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert r["index"] == "settled" and r["worktree"] == "settled", r
    assert _no_locks(repo) == []


@pytest.mark.parametrize("how", ["reset", "symbolic-ref"])
def test_a_ref_writer_while_the_revert_writes_its_files_is_refused_by_git(
    repo, shaped, monkeypatch, how
):
    """Publishing the index consumes index.lock, never the ref locks: they are still held while the
    revert writes its files, so a soft reset or `symbolic-ref` attempted then is refused by git."""
    before = head(repo)
    _git(repo, "branch", "side")
    act = _ref_writer(repo, how, before)
    real = gitwrite._snapshot_for_index
    fired: list = []

    def during_worktree(r, names):
        if not fired:
            fired.append(act())
        return real(r, names)

    monkeypatch.setattr(gitwrite, "_snapshot_for_index", during_worktree)
    r = revert(repo, shaped)
    (proc,) = fired
    assert proc.returncode != 0 and ".lock" in proc.stderr, proc
    assert head(repo) == r["sha"] != before
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert r["worktree"] == "settled" and (repo / "a.txt").read_text() == "one\n", r
    assert _no_locks(repo) == []


def test_a_ref_lock_another_git_holds_after_the_revert_is_published_leaves_both_halves_pending(
    repo, shaped, monkeypatch
):
    """The revert's own `update-ref` needs HEAD.lock, so it takes the ref locks only after it. If
    another git holds one then, the commit stands, the index and the working tree are left pending
    — never settled without the fence — and that lock is left exactly as it is."""
    before = head(repo)
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    branch_lock = repo / ".git" / "refs" / "heads" / "main.lock"
    _at_update_ref(monkeypatch, lambda: branch_lock.write_bytes(b"someone else's\n"), after=True)
    r = revert(repo, shaped)
    assert head(repo) == r["sha"] != before
    assert r["index"] == "pending" and r["worktree"] == "pending", r
    assert "locked" in r["worktree_reason"], r
    assert (repo / "a.txt").read_text() == "two\n"
    assert branch_lock.read_bytes() == b"someone else's\n"
    branch_lock.unlink()
    assert _no_locks(repo) == []


@pytest.mark.parametrize("how", ["reset", "symbolic-ref"])
def test_settle_keeps_head_and_the_branch_locked_while_it_writes_files(
    repo, shaped, monkeypatch, how
):
    """SETTLE takes HEAD.lock and the branch's ref lock before validating HEAD. The index rename
    consumes index.lock, never those, so they are still held while its worktree half writes: a
    soft reset or `symbolic-ref` then is refused by git, and the file is settled on the commit
    validated."""
    tip = _settle_publishing_the_index(repo, monkeypatch, shaped)
    _git(repo, "branch", "side")
    parent = _git(repo, "rev-parse", f"{tip}^").strip()
    act = _ref_writer(repo, how, parent)
    real = gitwrite._worktree_entries_raw
    fired: list = []

    def while_writing(r, names):
        if not fired:
            fired.append(act())
        return real(r, names)

    monkeypatch.setattr(gitwrite, "_worktree_entries_raw", while_writing)
    s = gitwrite.git_settle(str(repo), tip)
    (proc,) = fired
    assert proc.returncode != 0 and ".lock" in proc.stderr, proc
    assert head(repo) == tip
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert s["worktree"] == "settled" and s["worktree_paths"] == ["a.txt"], s
    assert (repo / "a.txt").read_text() == "one\n"
    assert _no_locks(repo) == []


# --------------------------------------------------------------------------- review 4856 fence


def test_a_ref_only_checkout_change_right_before_the_revert_keeps_it_and_writes_nothing(
    repo, shaped, monkeypatch
):
    """Review 4856, 3, in the revert: index.lock does not stop `git symbolic-ref HEAD
    refs/heads/side`, and the revert publishes with `update-ref` exactly as commit-paths does. Run
    immediately before that `update-ref`, it advances `main` while HEAD names `side`. The accepted
    no-compensation policy keeps the published commit, reports its SHA and preserves the index
    and worktree. A rollback could replace another writer's symbolic ref."""

    before = head(repo)
    _git(repo, "branch", "side")
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side")
    )
    index_before = (repo / ".git" / "index").read_bytes()
    r = revert(repo, shaped, head=before)
    (flip,) = fired
    assert flip.returncode == 0, flip
    assert r["index"] == r["worktree"] == "pending", r
    assert r["sha"] in r["index_reason"] and "left there" in r["index_reason"], r
    assert "reset --hard" not in r["index_reason"]
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == r["sha"] != before
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert _git(repo, "rev-parse", "refs/heads/side").strip() == before
    assert _git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/side"
    assert (repo / "a.txt").read_text() == "two\n"
    assert not (repo / "b.txt").exists() and (repo / "c.txt").exists()
    assert _no_locks(repo) == []


def test_a_checkout_that_moves_right_after_the_revert_is_published_keeps_it_and_writes_nothing(
    repo, shaped, monkeypatch
):
    """The other side of the fence: HEAD named `main` when the revert was published and moved only
    afterwards (the revert IS in HEAD's reflog), so the revert stands on `main` — and nothing is
    materialised into the checkout that now names `side`."""
    before = head(repo)
    _git(repo, "branch", "side")
    fired = _at_update_ref(
        monkeypatch,
        lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side"),
        after=True,
    )
    r = revert(repo, shaped, head=before)
    (flip,) = fired
    assert flip.returncode == 0, flip
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == r["sha"] != before
    assert r["index"] == "pending" and "no longer on" in r["index_reason"], r
    assert r["worktree"] == "pending", r
    assert (repo / "a.txt").read_text() == "two\n" and not (repo / "b.txt").exists()
    assert _no_locks(repo) == []


def test_a_revert_whose_checkout_moved_first_is_reported_not_rewound_if_the_branch_moved_again(
    repo, shaped, monkeypatch
):
    """Another commit on top of the published revert belongs to that writer. No compensation
    may move it back; the response still carries the revert SHA and leaves both halves pending."""
    before = head(repo)
    _git(repo, "branch", "side")
    _at_update_ref(monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side"))
    real = gitwrite._lock_head
    moved: list = []

    def someone_moves_main_first(r, locks, wait_s=0.0):
        if not moved:
            tree = _git(repo, "rev-parse", "refs/heads/main^{tree}").strip()
            other = _git(repo, "commit-tree", tree, "-p", "refs/heads/main", "-m", "agent").strip()
            _git(repo, "update-ref", "refs/heads/main", other)
            moved.append(other)
        return real(r, locks, wait_s)

    monkeypatch.setattr(gitwrite, "_lock_head", someone_moves_main_first)
    r = revert(repo, shaped, head=before)
    assert moved and _git(repo, "rev-parse", "refs/heads/main").strip() == moved[0]
    assert r["index"] == "pending" and r["sha"] in r["index_reason"], r
    assert _git(repo, "rev-parse", f"{moved[0]}^").strip() == r["sha"]
    assert r["worktree"] == "pending", r
    assert (repo / "a.txt").read_text() == "two\n"
    assert _no_locks(repo) == []


def test_a_revert_publishes_to_the_branch_when_a_tag_shares_its_name(repo, shaped):
    """Review 4856, 1, in the revert: with a tag `main`, `symbolic-ref --short HEAD` says
    `heads/main`. The revert's own `update-ref refs/heads/<branch>` and its locks take the branch
    from the full ref, so the revert lands on `refs/heads/main` and settles."""
    _git(repo, "tag", "main")
    before = head(repo)
    r = revert(repo, shaped, head=before)
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == r["sha"] != before
    assert r["index"] == "settled" and r["worktree"] == "settled", r
    assert (repo / "a.txt").read_text() == "one\n" and (repo / "b.txt").exists()
    assert _no_locks(repo) == []


def test_revert_refuses_reflogs_off_before_publication(repo, shaped, monkeypatch):
    before = head(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    _git(repo, "config", "core.logAllRefUpdates", "false")
    updates = _at_update_ref(monkeypatch, lambda: True)
    with pytest.raises(FsError) as exc:
        revert(repo, shaped, head=before)
    assert exc.value.status == 409 and "core.logAllRefUpdates" in str(exc.value)
    assert updates == []
    assert head(repo) == before
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / "a.txt").read_text() == "two\n"
    assert _no_locks(repo) == []


def test_revert_reports_a_converted_branch_without_rewinding_its_target(repo, shaped, monkeypatch):
    before = head(repo)
    _git(repo, "branch", "side")
    index_before = (repo / ".git" / "index").read_bytes()
    updates = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/side")
    )
    r = revert(repo, shaped, head=before)
    assert len(updates) == 1 and updates[0].returncode == 0
    assert _git(repo, "symbolic-ref", "refs/heads/main").strip() == "refs/heads/side"
    assert _git(repo, "rev-parse", "refs/heads/side").strip() == r["sha"] != before
    assert r["index"] == r["worktree"] == "pending", r
    assert "symbolic ref" in r["index_reason"] and r["sha"] in r["index_reason"], r
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / "a.txt").read_text() == "two\n"
    assert _no_locks(repo) == []


# ------------------------------------------------------------------ review 4957


def test_confirmed_branch_is_required_even_when_two_branches_share_a_tip(
    client, repo, shaped, auth_cfg
):
    seen = head(repo)
    _git(repo, "checkout", "-qb", "other")
    index = (repo / ".git" / "index").read_bytes()
    response = client.post(
        "/api/git/revert",
        json={"path": str(repo), "commit": shaped, "head": seen, "branch": "refs/heads/main"},
        headers=_hdr(client, auth_cfg),
    )
    assert response.status_code == 409, response.text
    assert "branch" in response.json()["detail"]
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == seen
    assert _git(repo, "rev-parse", "refs/heads/other").strip() == seen
    assert (repo / ".git" / "index").read_bytes() == index
    assert (repo / "a.txt").read_text() == "two\n"


@pytest.mark.parametrize("branch", [None, "", "main", "refs/tags/main", "refs/heads/../main", True])
def test_revert_requires_the_full_confirmed_branch(client, repo, shaped, auth_cfg, branch):
    seen = head(repo)
    response = client.post(
        "/api/git/revert",
        json={"path": str(repo), "commit": shaped, "head": seen, "branch": branch},
        headers=_hdr(client, auth_cfg),
    )
    assert response.status_code == 422, response.text
    assert head(repo) == seen


def test_settle_failure_returns_the_displaced_concurrent_bytes(repo, shaped, monkeypatch):
    _revert_leaving(repo, monkeypatch, shaped)
    restore = gitwrite._restore_from_index
    concurrent = "uncommitted concurrent bytes preserved through a failed restore\n"

    def race(r, name, entry, saved=None):
        if name == "a.txt":
            (repo / name).write_text(concurrent)
        return restore(r, name, entry, saved)

    def fail_blob(*args):
        raise gitpanel.GitError("injected blob read failure")

    monkeypatch.setattr(gitwrite, "_restore_from_index", race)
    monkeypatch.setattr(gitwrite, "_cat_blob_into", fail_blob)
    out = gitwrite.git_settle(str(repo), head(repo))
    assert out["worktree"] == "pending" and out["worktree_left"] == ["a.txt"]
    assert not (repo / "a.txt").exists()
    ids = out["recoverable"].get("a.txt", [])
    assert ids, out
    assert concurrent in [_git(repo, "cat-file", "-p", oid) for oid in ids]


def test_settle_releases_both_index_directory_descriptors(repo):
    (repo / "a.txt").write_text("two\n")
    tip = commit_all(repo, "two")
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(6):
        (repo / "a.txt").write_text("one\n")
        _git(repo, "add", "a.txt")
        out = gitwrite.git_settle(str(repo), tip)
        assert out["index"] == "settled"
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("failure_at", ["operation-check", "retake"])
def test_settle_post_index_failure_retains_the_applied_result(repo, monkeypatch, failure_at):
    for name in ("a.txt", "b.txt"):
        (repo / name).write_text("two\n")
    tip = commit_all(repo, "two files")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    (repo / "b.txt").write_text("one\n")
    retake = gitwrite._retake_after_publish

    def broken(*args):
        raise gitpanel.GitError("injected post-index read failure")

    def after_index(r, held):
        assert _git(repo, "show", ":a.txt") == "two\n"
        if failure_at == "retake":
            return broken()
        monkeypatch.setattr(gitwrite, "_unfinished_operation", broken)
        return retake(r, held)

    monkeypatch.setattr(gitwrite, "_retake_after_publish", after_index)
    out = gitwrite.git_settle(str(repo), tip)
    assert out["index"] == "settled" and "a.txt" in out["paths"]
    assert out["sha"] == tip
    assert out["worktree"] == "pending" and out["worktree_left"] == ["b.txt"]
    assert "injected" in out["worktree_reason"]
    assert _git(repo, "show", ":a.txt") == "two\n"
    assert (repo / "b.txt").read_text() == "one\n"
    assert _no_locks(repo) == []


@pytest.mark.parametrize("direction", ["file-to-directory", "directory-to-file"])
def test_revert_refuses_file_directory_transitions_before_publication(repo, direction):
    (repo / "a.txt").unlink()
    (repo / "a.txt").mkdir()
    (repo / "a.txt" / "child").write_text("nested\n")
    target = commit_all(repo, "file to directory")
    if direction == "directory-to-file":
        (repo / "a.txt" / "child").unlink()
        (repo / "a.txt").rmdir()
        (repo / "a.txt").write_text("file again\n")
        target = commit_all(repo, "directory to file")
    index = (repo / ".git" / "index").read_bytes()
    with pytest.raises(FsError, match="file and a directory") as exc:
        revert(repo, target)
    assert exc.value.status == 409
    assert head(repo) == target
    assert (repo / ".git" / "index").read_bytes() == index
    assert _git(repo, "status", "--porcelain") == ""


def test_settle_does_not_call_a_directory_blocking_a_file_settled(repo):
    # A previous interrupted operation (or an external writer) left a directory at an added file.
    (repo / "new.txt").write_text("committed\n")
    tip = commit_all(repo, "add file")
    (repo / "new.txt").unlink()
    (repo / "new.txt").mkdir()
    out = gitwrite.git_settle(str(repo), tip)
    assert out["worktree"] == "pending" and out["worktree_left"] == ["new.txt"], out
    assert (repo / "new.txt").is_dir()
    assert status(repo)["unsettled_worktree"] == ["new.txt"]


def test_history_after_revert_uses_the_new_epoch(repo, shaped, monkeypatch):
    monkeypatch.setattr(gitpanel, "_STATUS_TTL_S", 60)
    old = gitpanel.git_log(str(repo))
    out = gitwrite.git_revert(str(repo), shaped, old["head"], "refs/heads/main")
    fresh = gitpanel.git_log(str(repo))
    assert fresh["head"] == out["sha"] != old["head"]
    assert fresh["commits"][0]["sha"] == out["sha"]


def test_history_after_write_neither_joins_nor_reuses_a_prewrite_flight(repo, shaped, monkeypatch):
    ready, finish = Event(), Event()
    snapshot = gitpanel.sanitized_gitdir
    calls = []

    @contextmanager
    def parked(r):
        with snapshot(r) as value:
            first = not calls
            calls.append(True)
            if first:
                ready.set()
                assert finish.wait(10), "test did not release the old history snapshot"
            yield value

    monkeypatch.setattr(gitpanel, "sanitized_gitdir", parked)
    monkeypatch.setattr(gitpanel, "_STATUS_TTL_S", 60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(gitpanel.git_log, str(repo))
        assert ready.wait(5)
        try:
            (repo / "z.txt").write_text("after snapshot\n")
            tip = commit_all(repo, "new history")
            gitpanel.invalidate_status(str(repo))
            fresh = pool.submit(gitpanel.git_log, str(repo))
            assert fresh.result(timeout=5)["head"] == tip
        finally:
            finish.set()
        assert old.result(timeout=5)["head"] != tip
        assert gitpanel.git_log(str(repo))["head"] == tip


def test_settle_preserves_a_reversal_staged_during_index_lock_reacquisition(
    repo, shaped, monkeypatch
):
    tip = _settle_publishing_the_index(repo, monkeypatch, shaped)
    cas = gitwrite._index_cas
    staged = []

    def stage_after_publish(r, changes, *args, **kw):
        result = cas(r, changes, *args, **kw)
        # Publication consumed index.lock. An ordinary writer can now stage the pending bytes,
        # making them an intentional reversal, before SETTLE takes the lock for worktree writes.
        staged.append(_try_git(repo, "add", "a.txt"))
        return result

    monkeypatch.setattr(gitwrite, "_index_cas", stage_after_publish)
    out = gitwrite.git_settle(str(repo), tip)
    assert len(staged) == 1 and staged[0].returncode == 0, staged
    assert out["index"] == "settled" and "b.txt" in out["paths"]
    assert out["worktree"] == "pending" and out["worktree_left"] == ["a.txt"], out
    assert out["worktree_paths"] == []
    assert "index entry changed" in out["worktree_reason"]
    assert _git(repo, "show", ":a.txt") == "two\n"
    assert (repo / "a.txt").read_text() == "two\n"
    assert _no_locks(repo) == []


def test_revert_preserves_a_reversal_staged_during_index_lock_reacquisition(
    repo, shaped, monkeypatch
):
    cas = gitwrite._index_cas
    staged = []

    def stage_after_publish(r, changes, *args, **kw):
        result = cas(r, changes, *args, **kw)
        # Initial REVERT has the same publication/reacquisition interval as explicit SETTLE.
        # This successful stage makes the pending worktree bytes an intentional reversal.
        staged.append(_try_git(repo, "add", "a.txt"))
        return result

    monkeypatch.setattr(gitwrite, "_index_cas", stage_after_publish)
    out = revert(repo, shaped)
    assert len(staged) == 1 and staged[0].returncode == 0, staged
    assert out["sha"] == head(repo) and _git(repo, "show", "HEAD:a.txt") == "one\n"
    assert out["index"] == "settled"
    assert out["worktree"] == "pending" and out["worktree_left"] == ["a.txt"], out
    assert "index entry changed" in out["worktree_reason"]
    assert _git(repo, "show", ":a.txt") == "two\n"
    assert (repo / "a.txt").read_text() == "two\n"
    assert (repo / "b.txt").read_text() == "one\n"
    assert not (repo / "c.txt").exists()
    assert _git(repo, "status", "--porcelain") == "M  a.txt\n"
    assert _no_locks(repo) == []
