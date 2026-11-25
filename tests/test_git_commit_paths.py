"""#950 Phase 2a — commit selected / all, index settlement, SETTLE, and revert-file.

Every test runs against a real repository and real git. The race tests inject the conflicting
change INSIDE the operation (a spy on the git call that sits in the gap), because a race whose
window the test removes cannot be caught by the test.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat as _stat
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, gitpanel, gitwrite
from agent_sessions.files import FsError


@pytest.fixture(autouse=True)
def _reset():
    gitpanel.reset_flights_for_test()
    gitpanel.reset_git_bin_for_test()
    # No hooks-void reset: #1006 replaced the per-process temp directory with one stable home that
    # re-establishes its existence and mode on EVERY call rather than caching them, so there is no
    # module state left to clear — and `reset_hooks_void_for_test` went with it.
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


def status(repo) -> dict:
    gitpanel.reset_flights_for_test()
    return gitwrite.git_status(str(repo))


def fps(repo, names) -> dict:
    return {e["path"]: e["fp"] for e in status(repo)["entries"] if e["path"] in names}


def head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def blob(repo, rev_path) -> str:
    return _git(repo, "show", rev_path)


def index_blob(repo, name) -> str:
    return _git(repo, "show", f":{name}")


def commit_paths(repo, names, message="panel commit", **kw):
    return gitwrite.git_commit_paths(
        str(repo), message, names, kw.get("expect", fps(repo, names)), kw.get("head", head(repo))
    )


# --------------------------------------------------------------------------- status additions


def test_status_names_the_head_commit_and_keeps_index_modes(repo):
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")
    st = status(repo)
    assert st["head"] == head(repo)
    (row,) = [e for e in st["entries"] if e["kind"] == "staged"]
    assert row["mode_index"] == "100644"
    assert st["unsettled"] == []


def test_an_unborn_branch_has_no_head(root):
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    (p / "x.txt").write_text("x\n")
    st = status(p)
    assert st["head"] is None and st["unsettled"] == []


# --------------------------------------------------------------------------- commit selected / all


def test_commit_selected_commits_exactly_those_paths_and_leaves_the_rest_of_the_index(repo):
    (repo / "a.txt").write_text("a two\n")  # changed, unstaged
    (repo / "b.txt").write_text("b two\n")
    _git(repo, "add", "b.txt")  # staged, NOT selected
    (repo / "c.txt").write_text("new\n")  # untracked, selected
    before = head(repo)

    out = commit_paths(repo, ["a.txt", "c.txt"])

    assert out["index"] == "settled" and out["index_left"] == []
    assert _git(repo, "rev-parse", "HEAD^").strip() == before
    assert blob(repo, "HEAD:a.txt") == "a two\n"
    assert blob(repo, "HEAD:c.txt") == "new\n"
    assert blob(repo, "HEAD:b.txt") == "one\n"  # the staged, unselected change is not in it
    assert index_blob(repo, "b.txt") == "b two\n"  # …and is still staged
    st = status(repo)
    assert {(e["path"], e["kind"]) for e in st["entries"]} == {("b.txt", "staged")}
    assert st["unsettled"] == []
    assert out["sha"] == head(repo)


def test_commit_all_commits_every_staged_and_changed_row(repo):
    (repo / "a.txt").write_text("a two\n")
    (repo / "b.txt").write_text("b two\n")
    _git(repo, "add", "b.txt")
    commit_paths(repo, ["a.txt", "b.txt"])
    assert blob(repo, "HEAD:a.txt") == "a two\n" and blob(repo, "HEAD:b.txt") == "b two\n"
    assert status(repo)["entries"] == []


def test_a_staged_version_that_is_neither_head_nor_worktree_is_refused_before_publication(
    repo, monkeypatch
):
    """Hermes on #964 (review 4829, 1): parent H, staged S, worktree W. Publishing W and failing
    settlement left an index at S, which `unsettled` (index still at the PARENT's entry) cannot
    see — so SETTLE could not recover and a later STAGED commit silently recorded S over W. Refused
    before publication now, with settlement broken to prove nothing is published either way."""
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    before = head(repo)
    with monkeypatch.context() as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        with pytest.raises(FsError) as e:
            commit_paths(repo, ["b.txt"])
    assert e.value.status == 409 and "staged version" in str(e.value)
    assert head(repo) == before and index_blob(repo, "b.txt") == "staged\n"
    # Staged first — the index now equals the worktree — the same file commits and settles.
    _git(repo, "add", "b.txt")
    out = commit_paths(repo, ["b.txt"])
    assert blob(repo, "HEAD:b.txt") == "worktree\n" and out["index"] == "settled"
    assert status(repo)["entries"] == []


def test_a_deleted_file_is_committed_as_a_deletion(repo):
    (repo / "a.txt").unlink()
    commit_paths(repo, ["a.txt"])
    assert "a.txt" not in _git(repo, "ls-tree", "--name-only", "HEAD")
    assert "a.txt" not in _git(repo, "ls-files")


def test_the_first_commit_on_an_unborn_branch(root):
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "x.txt").write_text("x\n")
    out = gitwrite.git_commit_paths(str(p), "first", ["x.txt"], fps(p, ["x.txt"]), "")
    assert out["index"] == "settled"
    assert _git(p, "rev-list", "--count", "HEAD").strip() == "1"


def test_a_head_the_panel_did_not_show_is_refused_and_nothing_is_published(repo):
    (repo / "a.txt").write_text("two\n")
    stale = head(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "agent")
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["a.txt"], head=stale)
    assert e.value.status == 409
    assert _git(repo, "log", "-1", "--format=%s").strip() == "agent"


def test_an_agent_commit_landing_before_the_critical_section_publishes_nothing(repo, monkeypatch):
    """A commit that finishes before the panel takes index.lock is seen under it: the branch no
    longer holds the head the operator was shown. (One attempted INSIDE the lock is refused by git
    itself — see the review 4833 tests.)"""
    (repo / "a.txt").write_text("two\n")
    real = gitwrite.run_git_write
    fired = []

    def spy(r, args, **kw):
        if args and args[0] == "commit-tree" and not fired:
            fired.append(True)
            _git(repo, "commit", "-q", "--allow-empty", "-m", "agent")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["a.txt"])
    assert fired and e.value.status == 409
    assert _git(repo, "log", "-1", "--format=%s").strip() == "agent"
    assert blob(repo, "HEAD:a.txt") == "one\n"


def test_an_edit_landing_while_the_snapshot_is_taken_is_refused(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    real = gitwrite._snapshot_for_index

    def edit_after(r, names):
        out = real(r, names)
        (repo / "a.txt").write_text("two, and the agent's edit\n")
        return out

    monkeypatch.setattr(gitwrite, "_snapshot_for_index", edit_after)
    before = head(repo)
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["a.txt"])
    assert e.value.status == 409 and head(repo) == before


def test_a_truncated_status_and_a_detached_head_are_refused(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("two\n")
    names = ["a.txt"]
    expect, tip = fps(repo, names), head(repo)
    # Scoped, not `monkeypatch.undo()`: undo reverts EVERY patch in the test, including the
    # file-root environment the fixture set, and the next call then escapes the root.
    with monkeypatch.context() as m:
        m.setattr(gitpanel, "GIT_MAX_ENTRIES", 1)
        with pytest.raises(FsError) as e:
            gitwrite.git_commit_paths(str(repo), "m", names, expect, tip)
        assert e.value.status == 409
    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(FsError) as e:
        commit_paths(repo, names)
    assert e.value.status == 409


# --------------------------------------------------------------------------- settlement


def _boom(repo, changes, *_args, **_kw):
    raise OSError("injected failure after update-ref")


def _broken_settlement(monkeypatch):
    """Settlement fails right after the commit is published — scoped to a `with` block."""
    ctx = monkeypatch.context()
    return ctx


def test_a_failure_right_after_publication_returns_the_sha_and_pending_then_settle_fixes_it(
    repo, monkeypatch
):
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])

    assert out["index"] == "pending" and out["index_left"] == ["a.txt"]
    assert out["sha"] == head(repo) and blob(repo, "HEAD:a.txt") == "two\n"
    # The real index still holds the parent's blob: a staged REVERSAL of the commit.
    assert index_blob(repo, "a.txt") == "one\n"
    assert status(repo)["unsettled"] == ["a.txt"]

    settled = gitwrite.git_settle(str(repo), out["sha"])
    assert settled["index"] == "settled"
    assert index_blob(repo, "a.txt") == "two\n"
    st = status(repo)
    assert st["unsettled"] == [] and st["entries"] == []
    # Idempotent.
    assert gitwrite.git_settle(str(repo), out["sha"])["index"] == "settled"


def test_an_external_stage_before_settlement_is_left_as_staged_and_reported(repo, monkeypatch):
    """Staged after the snapshot and before the critical section (inside it, git refuses the add):
    the index no longer holds the entry the commit replaced, so settlement leaves it."""
    (repo / "a.txt").write_text("two\n")
    real = gitwrite.run_git_write
    fired = []

    def agent_stages_first(r, args, **kw):
        if args and args[0] == "commit-tree" and not fired:
            fired.append(True)
            (repo / "a.txt").write_text("agent\n")
            _git(repo, "add", "a.txt")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", agent_stages_first)
    out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending" and out["index_left"] == ["a.txt"]
    assert blob(repo, "HEAD:a.txt") == "two\n"
    assert index_blob(repo, "a.txt") == "agent\n"  # not overwritten
    assert status(repo)["unsettled"] == []  # the index is not the parent's version


def test_settle_never_consumes_staging_that_arrived_after_the_commit(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt", "b.txt"])
    (repo / "a.txt").write_text("newer\n")
    _git(repo, "add", "a.txt")
    res = gitwrite.git_settle(str(repo), out["sha"])
    assert res["index"] == "pending" and res["index_left"] == ["a.txt"]
    assert index_blob(repo, "a.txt") == "newer\n"
    assert index_blob(repo, "b.txt") == "two\n"


def test_an_index_lock_held_by_another_process_publishes_nothing_and_is_left_alone(
    repo, monkeypatch
):
    """The panel publishes only while it holds index.lock itself, so a lock another git holds
    past the bounded wait is a 409 with nothing published — and that lock is never removed."""
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    lock = repo / ".git" / "index.lock"
    real = gitwrite.run_git_write

    def lock_taken_by_another_git(r, args, **kw):
        if args and args[0] == "commit-tree" and not lock.exists():
            lock.write_text("held by another git\n")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", lock_taken_by_another_git)
    with pytest.raises(FsError) as e:
        gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert e.value.status == 409 and "busy" in str(e.value)
    assert head(repo) == tip and blob(repo, "HEAD:a.txt") == "one\n"
    assert lock.read_text() == "held by another git\n"
    lock.unlink()


def test_a_split_index_is_refused_rather_than_rewritten(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / ".git" / "sharedindex.0123456789abcdef0123456789abcdef01234567").write_bytes(b"x")
    out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending" and "split index" in out["index_reason"]


def test_settle_refuses_a_moved_head_and_a_merge_head(repo):
    (repo / "a.txt").write_text("two\n")
    out = commit_paths(repo, ["a.txt"])
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), out["sha"])
    assert e.value.status == 409

    _git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), head(repo))
    assert e.value.status == 409
    assert status(repo)["unsettled"] == []  # a merge HEAD reports nothing


def test_unsettled_covers_an_addition_a_deletion_and_a_mode_only_change(repo, monkeypatch):
    (repo / "new.txt").write_text("added\n")
    (repo / "b.txt").unlink()
    os.chmod(repo / "a.txt", 0o755)
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["new.txt", "b.txt", "a.txt"])
    assert status(repo)["unsettled"] == ["a.txt", "b.txt", "new.txt"]
    gitwrite.git_settle(str(repo), out["sha"])
    assert status(repo)["unsettled"] == []
    assert _git(repo, "ls-files", "-s", "a.txt").split()[0] == "100755"
    assert "b.txt" not in _git(repo, "ls-files")


def test_unsettled_on_a_root_commit(root, monkeypatch):
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "x.txt").write_text("x\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = gitwrite.git_commit_paths(str(p), "first", ["x.txt"], fps(p, ["x.txt"]), "")
    assert status(p)["unsettled"] == ["x.txt"]
    assert gitwrite.git_settle(str(p), out["sha"])["index"] == "settled"
    assert status(p)["unsettled"] == []


def test_parse_raw_diff():
    a, b, z = b"a" * 40, b"b" * 40, b"0" * 40
    blob_ = (
        b":100644 100755 "
        + a
        + b" "
        + a
        + b" M"
        + bytes(1)
        + b"a.txt"
        + bytes(1)
        + b":000000 100644 "
        + z
        + b" "
        + b
        + b" A"
        + bytes(1)
        + b"with space.txt"
        + bytes(1)
    )
    out = gitwrite.parse_raw_diff(blob_)
    assert out["a.txt"] == (("100644", "a" * 40), ("100755", "a" * 40))
    assert out["with space.txt"] == (None, ("100644", "b" * 40))


# --------------------------------------------------------------------------- revert file


def test_revert_file_puts_index_and_worktree_back_to_the_last_commit(repo):
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    staged_oid = _git(repo, "rev-parse", ":b.txt").strip()
    out = gitwrite.git_discard(str(repo), ["b.txt"], fps(repo, ["b.txt"]), "head", head(repo))
    assert (repo / "b.txt").read_text() == "one\n"
    assert index_blob(repo, "b.txt") == "one\n"
    assert out["index"] == "settled"
    assert out["staged_recoverable"]["b.txt"] == staged_oid
    assert out["recoverable"]["b.txt"]  # the worktree bytes were displaced, not destroyed
    assert _git(repo, "cat-file", "-p", out["recoverable"]["b.txt"][0]) == "worktree\n"
    assert status(repo)["entries"] == []


def test_revert_file_refuses_a_file_the_last_commit_does_not_have(repo):
    (repo / "new.txt").write_text("x\n")
    _git(repo, "add", "new.txt")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["new.txt"], fps(repo, ["new.txt"]), "head", head(repo))
    assert e.value.status == 409
    assert (repo / "new.txt").exists()


def test_revert_file_is_bound_to_the_commit_the_panel_showed(repo):
    (repo / "a.txt").write_text("two\n")
    stale = head(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "agent")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], fps(repo, ["a.txt"]), "head", stale)
    assert e.value.status == 409
    assert (repo / "a.txt").read_text() == "two\n"


def test_discard_rejects_an_unknown_source(repo):
    (repo / "a.txt").write_text("two\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], fps(repo, ["a.txt"]), "elsewhere")
    assert e.value.status == 422


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def _hdr(c, cfg):
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


@pytest.mark.parametrize("route", ["/api/git/commit-paths", "/api/git/settle"])
def test_new_write_routes_need_csrf(client, repo, route):
    r = client.post(route, json={"path": str(repo)})
    assert r.status_code == 403


def test_commit_paths_settle_and_revert_file_through_the_server(client, repo, auth_cfg):
    h = _hdr(client, auth_cfg)
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    st = client.get("/api/git/status", params={"path": str(repo)}).json()
    fp = {e["path"]: e["fp"] for e in st["entries"]}
    r = client.post(
        "/api/git/commit-paths",
        json={
            "path": str(repo),
            "message": "from the panel",
            "paths": ["a.txt"],
            "expect": {"a.txt": fp["a.txt"]},
            "head": st["head"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["index"] == "settled" and body["sha"] == head(repo)

    r = client.post("/api/git/settle", json={"path": str(repo), "commit": body["sha"]}, headers=h)
    assert r.status_code == 200 and r.json()["index"] == "settled"

    st = client.get("/api/git/status", params={"path": str(repo)}).json()
    fp = {e["path"]: e["fp"] for e in st["entries"]}
    r = client.post(
        "/api/git/discard",
        json={
            "path": str(repo),
            "paths": ["b.txt"],
            "expect": {"b.txt": fp["b.txt"]},
            "from": "head",
            "head": st["head"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (repo / "b.txt").read_text() == "one\n"


# --------------------------------------------------------------------------- review 4829 (#964)


def test_settle_refuses_an_unresolved_conflict_and_leaves_it_intact(repo):
    """Hermes on #964 (review 4829, 2): after the panel commits a new file, an add/add merge
    conflicts without moving HEAD. SETTLE read the conflict as an absent entry, installed the
    commit's blob, dropped every unmerged stage and reported settled with MERGE_HEAD present."""
    parent = head(repo)
    (repo / "new.txt").write_text("mine\n")
    out = commit_paths(repo, ["new.txt"])
    _git(repo, "checkout", "-q", "-b", "side", parent)
    (repo / "new.txt").write_text("theirs\n")
    _git(repo, "add", "new.txt")
    _git(repo, "commit", "-q", "-m", "side adds new.txt")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "side", check=False)
    assert head(repo) == out["sha"] and (repo / ".git" / "MERGE_HEAD").exists()
    assert "new.txt" in _git(repo, "ls-files", "-u")

    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), out["sha"])
    assert e.value.status == 409
    assert "new.txt" in _git(repo, "ls-files", "-u")
    assert (repo / ".git" / "MERGE_HEAD").exists()

    # The compare-and-swap itself refuses a conflicted path, whatever state file is present.
    mine = ("100644", _git(repo, "rev-parse", f"{out['sha']}:new.txt").strip())
    done, left, reason = gitwrite._index_cas(
        gitwrite.resolve_repo(str(repo)), {"new.txt": (None, mine)}
    )
    assert done == [] and left == ["new.txt"] and "conflict" in reason
    assert "new.txt" in _git(repo, "ls-files", "-u")


def test_a_branch_switch_just_before_publication_publishes_nothing(repo, monkeypatch):
    """Hermes on #964 (review 4829, 3): the named-ref CAS protected main's OID, not the checkout.
    A `git switch side` that finishes BEFORE the panel takes index.lock (injected at `commit-tree`)
    is seen by the branch check under it. A switch attempted at `update-ref`, inside the lock, is
    the review 4833 test below."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    before = head(repo)
    real = gitwrite.run_git_write
    fired = []

    def switch_first(r, args, **kw):
        if args and args[0] == "commit-tree" and not fired:
            fired.append(True)
            _git(repo, "switch", "-q", "side")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", switch_first)
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["a.txt"])
    assert fired and e.value.status == 409
    assert _git(repo, "rev-parse", "main").strip() == before
    assert _git(repo, "rev-parse", "side").strip() == before
    assert index_blob(repo, "a.txt") == "one\n"  # side's index was not given the commit


def test_a_branch_switch_right_after_publication_never_settles_into_the_other_branch(
    repo, monkeypatch
):
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    before = head(repo)
    real = gitwrite.run_git_write
    fired = []

    def switch_after(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "update-ref" and not fired:
            fired.append(True)
            # What a switch does to the checkout's identity, without git refusing over the dirty
            # file: HEAD now names side, and the index is side's to keep.
            _git(repo, "symbolic-ref", "HEAD", "refs/heads/side")
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", switch_after)
    out = commit_paths(repo, ["a.txt"])
    assert fired and out["index"] == "pending" and "no longer on `main`" in out["index_reason"]
    assert _git(repo, "rev-parse", "main").strip() == out["sha"]
    assert _git(repo, "rev-parse", "side").strip() == before
    assert index_blob(repo, "a.txt") == "one\n"


def test_revert_file_returns_the_displaced_ids_when_the_index_update_fails(repo, monkeypatch):
    """Hermes on #964 (review 4829, 4): the worktree was already replaced when `_index_cas` raised,
    and the response was only the exception — the saved object ids were gone with it."""
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")

    def io_error(*_a, **_kw):
        raise OSError("injected index I/O failure")

    monkeypatch.setattr(gitwrite, "_index_cas", io_error)
    out = gitwrite.git_discard(str(repo), ["b.txt"], fps(repo, ["b.txt"]), "head", head(repo))
    assert out["index"] == "pending" and "injected" in out["index_reason"]
    assert out["worktree"] == "settled" and (repo / "b.txt").read_text() == "one\n"
    (saved,) = out["recoverable"]["b.txt"]
    assert _git(repo, "cat-file", "-p", saved) == "worktree\n"


def test_revert_file_failing_part_way_returns_every_id_already_displaced(repo, monkeypatch):
    (repo / "a.txt").write_text("a work\n")
    (repo / "b.txt").write_text("b work\n")
    names = ["a.txt", "b.txt"]
    expect, tip = fps(repo, names), head(repo)
    real = gitwrite._cat_blob_into
    calls = []

    def fail_second(r, oid, fd):
        calls.append(oid)
        if len(calls) == 2:
            raise gitwrite.GitError("injected failure writing the second file")
        return real(r, oid, fd)

    monkeypatch.setattr(gitwrite, "_cat_blob_into", fail_second)
    out = gitwrite.git_discard(str(repo), names, expect, "head", tip)
    assert out["worktree"] == "pending" and out["worktree_left"] == ["b.txt"]
    assert out["discarded"] == ["a.txt"] and out["index"] == "pending"
    assert (repo / "a.txt").read_text() == "one\n"
    got = {n: _git(repo, "cat-file", "-p", ids[0]) for n, ids in out["recoverable"].items()}
    assert got == {"a.txt": "a work\n", "b.txt": "b work\n"}


def test_a_staged_rename_commits_as_a_rename_only_with_both_names(repo):
    """Hermes on #964 (review 4829, 5): porcelain shows `git mv a b` as ONE staged row, path b with
    orig_path a. The panel sent only b, the HEAD-seeded private index kept a, and the commit held
    both — a copy, reported settled, with a's deletion still staged."""
    _git(repo, "mv", "a.txt", "moved.txt")
    before = head(repo)
    fp = fps(repo, ["moved.txt"])["moved.txt"]
    with pytest.raises(FsError) as e:
        gitwrite.git_commit_paths(str(repo), "m", ["moved.txt"], {"moved.txt": fp}, before)
    assert e.value.status == 409 and "rename" in str(e.value) and head(repo) == before

    out = gitwrite.git_commit_paths(
        str(repo), "m", ["moved.txt", "a.txt"], {"moved.txt": fp, "a.txt": fp}, before
    )
    tree = _git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert "moved.txt" in tree and "a.txt" not in tree
    assert out["index"] == "settled" and status(repo)["entries"] == []


def test_a_conflict_appearing_after_admission_refuses_the_commit(repo, monkeypatch):
    """Hermes on #964 (review 4829, 6): the repository-wide conflict check ran outside the lock and
    the row checks only cover the selected paths, so a merge that conflicted on an unselected file
    let a single-parent commit publish over MERGE_HEAD."""
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side b")
    _git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main b")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    real = gitwrite.verify_rows
    fired = []

    def merge_first(r, want, verb, **kw):
        if not fired:
            fired.append(True)
            _git(repo, "merge", "-q", "side", check=False)
        return real(r, want, verb, **kw)

    monkeypatch.setattr(gitwrite, "verify_rows", merge_first)
    with pytest.raises(FsError) as e:
        gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert fired and e.value.status == 409
    assert head(repo) == tip and (repo / ".git" / "MERGE_HEAD").exists()


def test_a_status_read_failing_after_publication_still_returns_the_commit(repo, monkeypatch):
    """Hermes on #964 (review 4829, 7): the final `_fresh_status` sat outside every catch, so a
    failed read after a real publication raised and the response carried no SHA."""
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    real_run, real_status = gitwrite.run_git_write, gitwrite._fresh_status
    published = []

    def run(r, args, **kw):
        out = real_run(r, args, **kw)
        if args and args[0] == "update-ref":
            published.append(True)
        return out

    def status_after_publication(r):
        if published:
            raise gitwrite.GitError("injected status failure")
        return real_status(r)

    monkeypatch.setattr(gitwrite, "run_git_write", run)
    monkeypatch.setattr(gitwrite, "_fresh_status", status_after_publication)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert out["sha"] == head(repo) != tip and out["index"] == "settled"
    assert "status" not in out and "injected" in out["status_error"]


def test_revert_file_status_read_failing_afterwards_still_returns_the_ids(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    real_cas, real_status = gitwrite._index_cas, gitwrite._fresh_status
    settled = []

    def cas(*a, **kw):
        out = real_cas(*a, **kw)
        settled.append(True)
        return out

    def status_after(r):
        if settled:
            raise gitwrite.GitError("injected status failure")
        return real_status(r)

    monkeypatch.setattr(gitwrite, "_index_cas", cas)
    monkeypatch.setattr(gitwrite, "_fresh_status", status_after)
    out = gitwrite.git_discard(str(repo), ["a.txt"], expect, "head", tip)
    assert out["index"] == "settled" and out["recoverable"]["a.txt"]
    assert "status" not in out and "injected" in out["status_error"]


# --------------------------------------------------------------------------- review 4833 (#964)

LOCK = os.path.join(".git", "index.lock")
#: git's bytes for a name that is not UTF-8, and a REAL name that `replace` decodes it into.
BAD = b"bad\xff"
LOOKALIKE = "bad\ufffd".encode()


def _named(repo, name: bytes, text: str) -> None:
    with open(os.path.join(os.fsencode(str(repo)), name), "wb") as f:
        f.write(text.encode())


def _git_b(repo, *args: bytes) -> bytes:
    return subprocess.run(
        [b"git", b"-C", os.fsencode(str(repo)), *args], check=True, capture_output=True
    ).stdout


def _try_git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _at_update_ref(monkeypatch, act, *, after: bool = False) -> list:
    """Run `act` at the real publication boundary: just before (or just after) `update-ref`."""
    real = gitwrite.run_git_write
    fired: list = []

    def spy(r, args, **kw):
        boundary = bool(args) and args[0] == "update-ref" and not fired
        if boundary and not after:
            fired.append(act())
        out = real(r, args, **kw)
        if boundary and after:
            fired.append(act())
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    return fired


def test_settle_refuses_a_path_git_cannot_name_in_utf8_and_changes_nothing(repo):
    """Hermes on #964 (review 4833, 1): `parse_raw_diff` decoded `bad\\xff` as `bad\\ufffd`, the
    name of a different real file, and SETTLE installed the commit's blob into that file's entry."""
    _named(repo, BAD, "h\n")
    _named(repo, LOOKALIKE, "h\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two names")
    parent = head(repo)
    _named(repo, BAD, "w\n")
    (repo / "a.txt").write_text("w\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change the undecodable one")
    tip = head(repo)
    _git(repo, "read-tree", parent)  # the index still holds the parent: SETTLE is on offer
    before = _git_b(repo, b"ls-files", b"-s", b"-z")
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), tip)
    assert e.value.status == 409 and "UTF-8" in str(e.value)
    assert _git_b(repo, b"ls-files", b"-s", b"-z") == before
    assert not (repo / LOCK).exists()


def test_a_row_whose_name_is_not_utf8_is_never_a_write_target(repo):
    """The same decoding fed `validate_paths`: two rows read `bad\\ufffd`, and a commit of that name
    committed whichever real file carried it."""
    _named(repo, BAD, "h\n")
    _named(repo, LOOKALIKE, "h\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two names")
    _named(repo, BAD, "undecodable\n")
    _named(repo, LOOKALIKE, "lookalike\n")
    before = head(repo)
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["bad\ufffd"])
    assert e.value.status == 409 and "UTF-8" in str(e.value)
    assert head(repo) == before
    rows = [r for r in status(repo)["entries"] if r["path"] == "bad\ufffd"]
    assert len(rows) == 2 and sum(bool(r.get("undecodable")) for r in rows) == 1


def test_a_branch_whose_name_is_not_utf8_is_never_published_to(repo):
    """`_head_branch` decoded the checkout's branch the same way, and `update-ref` then moved the
    real branch spelled with U+FFFD."""
    _git_b(repo, b"branch", LOOKALIKE)
    _git_b(repo, b"checkout", b"-q", b"-b", BAD)
    (repo / "a.txt").write_text("two\n")
    tip = head(repo)
    with pytest.raises(FsError) as e:
        commit_paths(repo, ["a.txt"])
    assert e.value.status == 409 and "UTF-8" in str(e.value)
    assert _git_b(repo, b"rev-parse", b"refs/heads/" + LOOKALIKE).decode().strip() == tip
    assert _git_b(repo, b"rev-parse", b"refs/heads/" + BAD).decode().strip() == tip


def _conflicting_side(repo) -> None:
    """`side` and `main` both edit b.txt, so merging side into main conflicts."""
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side edits b")
    _git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main edits b")


def test_a_conflicting_merge_at_update_ref_is_refused_by_git_and_never_reaches_the_index(
    repo, monkeypatch
):
    """Hermes on #964 (review 4833, 3): a conflicting merge run right before `update-ref` — after
    every last-moment check — still let the commit publish and settle over MERGE_HEAD and unmerged
    stages. The panel holds index.lock there now: git cannot save the tree's state to start the
    merge, so no conflict reaches the index or the worktree, and the commit settles cleanly."""
    _conflicting_side(repo)
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_update_ref(monkeypatch, lambda: _try_git(repo, "merge", "side"))
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (merge,) = fired
    assert merge.returncode != 0
    assert _git(repo, "ls-files", "-u") == "" and (repo / "b.txt").read_text() == "main\n"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert out["sha"] == head(repo) != tip
    assert out["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert not (repo / LOCK).exists()


def test_a_merge_state_appearing_under_the_lock_leaves_settlement_pending(repo, monkeypatch):
    """What index.lock cannot stop: measured on git 2.43, a conflicting `git merge` on a clean tree
    fails with "Unable to write index" but has ALREADY written MERGE_HEAD. So the operation state is
    re-read under the lock immediately before the index is published, and a merge that appeared
    leaves the commit's settlement pending rather than settled over it. MERGE_HEAD is written with
    `update-ref` here — the one effect of that merge the lock does not prevent."""
    _conflicting_side(repo)
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    _at_update_ref(monkeypatch, lambda: _git(repo, "update-ref", "MERGE_HEAD", "side"), after=True)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert out["sha"] == head(repo) != tip
    assert out["index"] == "pending" and "merge is in progress" in out["index_reason"]
    assert index_blob(repo, "a.txt") == "one\n"
    assert not (repo / LOCK).exists()


def test_a_branch_switch_at_update_ref_is_refused_by_git_and_the_commit_settles_on_its_branch(
    repo, monkeypatch
):
    """Hermes on #964 (review 4833, 3): the same gap with `git switch` still advanced the old
    branch. The panel holds index.lock there now, so the switch itself fails."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_update_ref(monkeypatch, lambda: _try_git(repo, "switch", "side"))
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (switch,) = fired
    assert switch.returncode != 0 and "index.lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "main").strip() == out["sha"] != tip
    assert _git(repo, "rev-parse", "side").strip() == tip
    assert out["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert not (repo / LOCK).exists()


def test_an_agent_commit_right_after_publication_is_refused_by_git_and_never_settled_over(
    repo, monkeypatch
):
    """Hermes on #964 (review 4833, 2b): an ordinary `git commit` right after publication recorded
    the still-unsettled index as a newer commit, and settlement then installed the panel's entries
    over it, reporting settled."""
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "commit", "-q", "-m", "agent"), after=True
    )
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (commit,) = fired
    assert commit.returncode != 0 and "index.lock" in commit.stderr
    assert head(repo) == out["sha"] and out["index"] == "settled"
    assert index_blob(repo, "a.txt") == "two\n"
    assert not (repo / LOCK).exists()


def test_a_head_moved_by_a_ref_write_after_publication_is_never_settled_over(repo, monkeypatch):
    """A ref write landing right after our `update-ref` — BEFORE settlement can take HEAD's and the
    branch's ref locks, because `update-ref` itself needs them — is refused by `expect_head` under
    those locks: settlement is bound to the commit it was made for. The later interval, after that
    HEAD check and inside the index rewrite, is where review 4847 found the gap:
    `test_a_soft_reset_while_the_index_is_rewritten_is_refused_by_git_and_never_settled_over`."""
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)

    def agent_moves_the_branch():
        tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
        other = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "agent").strip()
        _git(repo, "update-ref", "refs/heads/main", other)
        return other

    fired = _at_update_ref(monkeypatch, agent_moves_the_branch, after=True)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (other,) = fired
    assert head(repo) == other != out["sha"]
    assert out["index"] == "pending" and "last commit changed" in out["index_reason"]
    assert index_blob(repo, "a.txt") == "one\n"
    assert not (repo / LOCK).exists()


def test_settle_holds_the_index_lock_so_a_switch_after_validating_head_is_refused(
    repo, monkeypatch
):
    """Hermes on #964 (review 4833, 2a): after SETTLE validated HEAD, a `git switch side` before
    the compare-and-swap put the commit's entries into side's index, reported settled."""
    parent = head(repo)
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending" and index_blob(repo, "a.txt") == "one\n"
    real = gitwrite.run_git_write
    fired: list = []

    def switch_after_validation(r, args, **kw):
        if args[:2] == ["rev-list", "--parents"] and not fired:
            fired.append(_try_git(repo, "switch", "side"))
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", switch_after_validation)
    res = gitwrite.git_settle(str(repo), out["sha"])
    (switch,) = fired
    assert switch.returncode != 0 and "index.lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert res["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert _git(repo, "rev-parse", "side").strip() == parent
    assert not (repo / LOCK).exists()


def test_revert_file_keeps_the_staged_id_when_the_index_lands_but_cannot_be_flushed(
    repo, monkeypatch
):
    """Hermes on #964 (review 4833, 4): the directory fsync after the real index rename raised, the
    caller treated the whole update as not applied, and the displaced staged version's id vanished
    from `staged_recoverable` — although the index no longer held it."""
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    staged_oid = _git(repo, "rev-parse", ":b.txt").strip()
    expect, tip = fps(repo, ["b.txt"]), head(repo)
    real_rename, real_fsync = os.rename, os.fsync
    renamed: list = []

    def rename(src, dst, *args, **kw):
        out = real_rename(src, dst, *args, **kw)
        # The index is published relative to the lock's directory descriptor (review 4863).
        if dst == "index" and kw.get("dst_dir_fd") is not None:
            renamed.append(True)
        return out

    def fsync(fd):
        if renamed and _stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "fsync", fsync)
    out = gitwrite.git_discard(str(repo), ["b.txt"], expect, "head", tip)
    assert renamed
    assert out["staged_recoverable"] == {"b.txt": staged_oid}
    assert out["index_durable"] is False and "confirmed on disk" in out["index_reason"]
    assert out["index"] == "settled" and out["reverted"] == ["b.txt"]
    assert index_blob(repo, "b.txt") == "one\n" and (repo / "b.txt").read_text() == "one\n"
    assert _git(repo, "cat-file", "-p", staged_oid) == "staged\n"
    assert not (repo / LOCK).exists()


# --------------------------------------------------------------------------- review 4847 (#964)


def _no_locks(repo) -> list[str]:
    """Every git lock file left anywhere in the repository's git directory — must be none."""
    left = []
    for dirpath, _dirs, filenames in os.walk(repo / ".git"):
        left += [os.path.join(dirpath, f) for f in filenames if f.endswith(".lock")]
    return left


def _at_scratch_index(monkeypatch, act) -> list:
    """Run `act` at the interval review 4847 named: inside settlement, AFTER its branch and HEAD
    checks, while the rewritten entries are installed into the scratch copy of the index."""
    real = gitwrite._install_index_entries
    fired: list = []

    def spy(r, entries, index_file=None):
        if index_file and ".battlelab-index-" in index_file and not fired:
            fired.append(act())
        return real(r, entries, index_file=index_file)

    monkeypatch.setattr(gitwrite, "_install_index_entries", spy)
    return fired


def test_a_soft_reset_while_the_index_is_rewritten_is_refused_by_git_and_never_settled_over(
    repo, monkeypatch
):
    """Hermes on #964 (review 4847, 1): index.lock does not exclude `git reset --soft`, which moves
    the branch through its own ref lock. A reset landing after settlement's HEAD check received the
    superseded commit's entries, reported settled. Settlement now holds HEAD.lock and the branch's
    ref lock as well, so git refuses the reset for that window."""
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_scratch_index(monkeypatch, lambda: _try_git(repo, "reset", "--soft", tip))
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (reset,) = fired
    assert reset.returncode != 0 and ".lock" in reset.stderr
    assert head(repo) == out["sha"] != tip
    assert out["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert _no_locks(repo) == []


def test_settle_holds_head_and_the_branch_so_a_soft_reset_while_it_rewrites_is_refused(
    repo, monkeypatch
):
    """The same interval in SETTLE: after it validated HEAD == the commit, a `git reset --soft` to
    the parent succeeded and SETTLE staged the superseded commit into the reset checkout."""
    parent = head(repo)
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending" and index_blob(repo, "a.txt") == "one\n"
    fired = _at_scratch_index(monkeypatch, lambda: _try_git(repo, "reset", "--soft", parent))
    res = gitwrite.git_settle(str(repo), out["sha"])
    (reset,) = fired
    assert reset.returncode != 0 and ".lock" in reset.stderr
    assert head(repo) == out["sha"]
    assert res["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert _no_locks(repo) == []


def test_a_head_lock_another_git_holds_after_publication_leaves_settlement_pending(
    repo, monkeypatch
):
    """The commit exists once `update-ref` returns; if HEAD's lock is then held by another git past
    the wait, the index is left for SETTLE — reported pending, never settled unguarded — and that
    lock is not touched."""
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    lock = repo / ".git" / "HEAD.lock"
    _at_update_ref(monkeypatch, lambda: lock.write_text("held by another git\n"), after=True)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert out["sha"] == head(repo) != tip
    assert out["index"] == "pending" and "locked" in out["index_reason"]
    assert index_blob(repo, "a.txt") == "one\n"
    assert lock.read_text() == "held by another git\n"
    lock.unlink()
    assert _no_locks(repo) == []


def test_settle_refuses_while_another_git_holds_the_branch_ref_lock_and_leaves_it_alone(
    repo, monkeypatch
):
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    lock = repo / ".git" / "refs" / "heads" / "main.lock"
    lock.write_text("held by another git\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), out["sha"])
    assert e.value.status == 409 and "locked" in str(e.value)
    assert index_blob(repo, "a.txt") == "one\n"
    assert lock.read_text() == "held by another git\n"
    lock.unlink()
    assert _no_locks(repo) == []


def _staged_side_and_worktree(repo) -> str:
    """Hermes' layout: main HEAD H, side HEAD S; on main the index holds S and the worktree W."""
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side S")
    side_tip = head(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("side\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    return side_tip


def test_revert_file_holds_head_and_the_branch_so_a_switch_before_restoring_is_refused(
    repo, monkeypatch
):
    """Hermes on #964 (review 4847, 2): REVERT FILE checked HEAD once, then restored the worktree
    and settled the index with no checkout fence. An ordinary `git switch side` just before the
    restoration succeeded (side's tree matches the staged version), and main's H was written into
    side's worktree and index, reported settled. The locks are now held across both halves."""
    side_tip = _staged_side_and_worktree(repo)
    expect, tip = fps(repo, ["b.txt"]), head(repo)
    real = gitwrite._restore_from_index
    fired: list = []

    def switch_first(r, name, entry, saved=None):
        if not fired:
            fired.append(_try_git(repo, "switch", "side"))
        return real(r, name, entry, saved)

    monkeypatch.setattr(gitwrite, "_restore_from_index", switch_first)
    out = gitwrite.git_discard(str(repo), ["b.txt"], expect, "head", tip)
    (switch,) = fired
    assert switch.returncode != 0 and ".lock" in switch.stderr
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "side").strip() == side_tip
    assert out["index"] == "settled" and out["worktree"] == "settled"
    assert index_blob(repo, "b.txt") == "one\n" and (repo / "b.txt").read_text() == "one\n"
    assert _no_locks(repo) == []


def test_revert_file_refuses_a_head_moved_before_its_locks_and_displaces_nothing(repo, monkeypatch):
    """HEAD is re-validated UNDER the locks, before any file is displaced: a ref write that moved
    the branch just before they were taken is a 409 with the worktree and index untouched."""
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    expect, tip = fps(repo, ["b.txt"]), head(repo)
    real = gitwrite._acquire_index_lock
    fired: list = []

    def branch_moves_first(r, *args, **kw):
        if not fired:
            tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
            other = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "agent").strip()
            _git(repo, "update-ref", "refs/heads/main", other)
            fired.append(other)
        return real(r, *args, **kw)

    monkeypatch.setattr(gitwrite, "_acquire_index_lock", branch_moves_first)
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["b.txt"], expect, "head", tip)
    assert fired and e.value.status == 409
    assert (repo / "b.txt").read_text() == "worktree\n" and index_blob(repo, "b.txt") == "staged\n"
    assert _no_locks(repo) == []


def test_revert_file_refuses_while_another_git_holds_head_and_displaces_nothing(repo, monkeypatch):
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("worktree\n")
    expect, tip = fps(repo, ["b.txt"]), head(repo)
    monkeypatch.setattr(gitwrite, "INDEX_LOCK_WAIT_S", 0.2)
    lock = repo / ".git" / "HEAD.lock"
    lock.write_text("held by another git\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["b.txt"], expect, "head", tip)
    assert e.value.status == 409 and "locked" in str(e.value)
    assert (repo / "b.txt").read_text() == "worktree\n" and index_blob(repo, "b.txt") == "staged\n"
    assert lock.read_text() == "held by another git\n"
    lock.unlink()
    assert _no_locks(repo) == []


def test_a_linked_worktree_locks_its_own_head_and_the_shared_branch_ref(root, repo, monkeypatch):
    """A linked worktree keeps HEAD in its own gitdir and the branch ref in the shared one: both
    are locked where git looks for them, so a soft reset in the worktree is refused there too."""
    wt = root / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    (wt / "a.txt").write_text("two\n")
    expect, tip = fps(wt, ["a.txt"]), head(wt)
    seen: list = []

    def reset_and_look():
        seen.append(
            (
                (repo / ".git" / "worktrees" / "wt" / "HEAD.lock").exists(),
                (repo / ".git" / "refs" / "heads" / "wt.lock").exists(),
            )
        )
        return _try_git(wt, "reset", "--soft", tip)

    fired = _at_scratch_index(monkeypatch, reset_and_look)
    out = gitwrite.git_commit_paths(str(wt), "m", ["a.txt"], expect, tip)
    (reset,) = fired
    assert seen == [(True, True)]
    assert reset.returncode != 0 and ".lock" in reset.stderr
    assert head(wt) == out["sha"] != tip and head(repo) == tip
    assert out["index"] == "settled" and index_blob(wt, "a.txt") == "two\n"
    assert _no_locks(repo) == []


# --------------------------------------------------------------------------- review 4856


def _worktree_on_main_with_pending_settle(root, repo, *, with_tag: bool):
    """Hermes' layout: the primary checkout on another branch, a linked worktree on `main` whose
    HEAD is W while its index still holds the parent H, so SETTLE is offered. With `with_tag`, a
    tag named `main` makes `symbolic-ref --short HEAD` answer `heads/main`."""
    _git(repo, "switch", "-q", "-c", "other")
    wt = root / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "main")
    if with_tag:
        _git(repo, "tag", "main", "main")
    parent = head(wt)
    (wt / "a.txt").write_text("two\n")
    _git(wt, "commit", "-q", "-am", "W")
    _git(wt, "read-tree", "HEAD~1")  # the index back at H; the worktree keeps W's bytes
    return wt, parent


@pytest.mark.parametrize("with_tag", [True, False], ids=["same-name-tag", "no-tag-control"])
def test_settle_locks_the_real_branch_ref_even_when_a_tag_shares_its_name(
    root, repo, monkeypatch, with_tag
):
    """Hermes on #964 (review 4856, 1): with a tag `main`, `symbolic-ref --short HEAD` answers
    `heads/main`, so SETTLE locked `refs/heads/heads/main.lock`. An `update-ref refs/heads/main`
    from the primary checkout after SETTLE's HEAD check then succeeded, and SETTLE staged W over the
    reset worktree's H index, reporting settled. The branch now comes from the full symbolic ref."""
    wt, parent = _worktree_on_main_with_pending_settle(root, repo, with_tag=with_tag)
    w = head(wt)
    assert status(wt)["unsettled"] == ["a.txt"]
    fired = _at_scratch_index(
        monkeypatch, lambda: _try_git(repo, "update-ref", "refs/heads/main", parent)
    )
    res = gitwrite.git_settle(str(wt), w)
    (write,) = fired
    assert write.returncode != 0 and ".lock" in write.stderr, write
    assert head(wt) == w
    assert res["index"] == "settled" and index_blob(wt, "a.txt") == "two\n"
    assert not (repo / ".git" / "refs" / "heads" / "heads").exists()
    assert _no_locks(repo) == []


def test_commit_paths_publishes_to_the_branch_when_a_tag_shares_its_name(repo):
    """The same collision in commit-paths: every ref name was derived from `heads/main`, so the
    branch looked moved (`refs/heads/heads/main` does not exist) and the commit was refused."""
    _git(repo, "tag", "main")
    tip = head(repo)
    (repo / "a.txt").write_text("two\n")
    out = commit_paths(repo, ["a.txt"])
    assert out["branch"] == "main"
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == out["sha"] != tip
    assert _git(repo, "rev-parse", "refs/tags/main").strip() == tip
    assert out["index"] == "settled" and index_blob(repo, "a.txt") == "two\n"
    assert not (repo / ".git" / "refs" / "heads" / "heads").exists()
    assert _no_locks(repo) == []


def test_settle_refuses_a_ref_directory_that_is_a_symlink_and_creates_nothing_outside(
    repo, monkeypatch, tmp_path
):
    """Hermes on #964 (review 4856, 2): O_NOFOLLOW covered only the final `.lock` name. For a
    packed `topic/deep/main` branch whose `refs/heads/topic` is a symlink out of the filesystem
    root, SETTLE created `deep/` and `main.lock` outside it and settled. The lock is now created
    by descriptor, one directory at a time, and a symlinked ancestor is refused."""
    _git(repo, "switch", "-q", "-c", "topic/deep/main")
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending"
    _git(repo, "pack-refs", "--all", "--prune")
    heads = repo / ".git" / "refs" / "heads"
    shutil.rmtree(heads / "topic", ignore_errors=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (heads / "topic").symlink_to(outside, target_is_directory=True)
    assert _git(repo, "rev-parse", "refs/heads/topic/deep/main").strip() == out["sha"]
    seen: list = []
    real = gitwrite._index_cas

    def spy(*args, **kw):
        seen.append(sorted(p.name for p in outside.rglob("*")))
        return real(*args, **kw)

    monkeypatch.setattr(gitwrite, "_index_cas", spy)
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), out["sha"])
    assert e.value.status == 409
    assert seen == [] and list(outside.iterdir()) == []
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_releasing_ref_locks_removes_only_our_own_lock_even_when_its_parent_is_swapped(
    repo, monkeypatch, tmp_path
):
    """Hermes on #964 (review 4856, 2): release unlinked by path, so swapping `refs/heads/topic`
    for a symlink after the compare-and-set made it delete an unrelated `main.lock` outside the
    repository and leave its own lock behind. Release is now bound to the directory descriptor the
    lock was created in, and removes a file only if it is still the one this process made."""
    _git(repo, "switch", "-q", "-c", "topic/main")
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    heads = repo / ".git" / "refs" / "heads"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "main.lock").write_text("another process's lock\n")
    real = gitwrite._index_cas
    swapped: list = []

    def cas_then_swap(*args, **kw):
        res = real(*args, **kw)
        (heads / "topic").rename(heads / "topic-moved")
        (heads / "topic").symlink_to(elsewhere, target_is_directory=True)
        swapped.append(True)
        return res

    monkeypatch.setattr(gitwrite, "_index_cas", cas_then_swap)
    gitwrite.git_settle(str(repo), out["sha"])
    assert swapped
    assert (elsewhere / "main.lock").read_text() == "another process's lock\n"
    assert not (heads / "topic-moved" / "main.lock").exists()
    assert _no_locks(repo) == []


def test_a_ref_only_checkout_change_right_before_publication_is_reported_not_withdrawn(
    repo, monkeypatch
):
    """Hermes on #964 (review 4856, 3; then 4920, 1): index.lock does not stop `git symbolic-ref
    HEAD refs/heads/side`. Run immediately before the real `update-ref`, it lets commit-paths
    advance `main` while HEAD names `side`. The panel cannot hold HEAD.lock across its own
    publication — measured on git 2.43, every form of `update-ref` of the branch HEAD names needs
    HEAD.lock — so straight after publishing it re-reads HEAD under HEAD.lock and finds the checkout
    moved.

    It used to put `main` back by compare-and-swap and refuse. It no longer does (review 4920, the
    operator's option B applied to this path too): that rollback is an `update-ref` whose ref
    identity cannot be bound through the mutation, and a conversion landing between the pre-read and
    the command replaced another writer's symbolic ref while the OID swap still passed. So the
    commit is LEFT on `main`, reported with its SHA, and the index is untouched."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side")
    )
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (flip,) = fired
    assert flip.returncode == 0, flip
    # The commit exists and the response says where, rather than claiming it was undone.
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert out["index"] == "pending"
    assert "left there" in out["index_reason"] and out["sha"] in out["index_reason"]
    # main holds the commit git wrote — not rewound to `tip`.
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == out["sha"]
    assert _git(repo, "rev-parse", "refs/heads/side").strip() == tip
    assert _git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/side"
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_a_checkout_that_moved_before_publication_is_reported_not_rewound_if_the_branch_moved_again(
    repo, monkeypatch
):
    """Another writer advances `main` right after the panel published to it, while the checkout has
    already moved away. Nothing of that writer's is touched: the panel takes no compensating ref
    write at all (review 4920), the commit is reported where git put it, and the index is left."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    _at_update_ref(monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side"))
    real = gitwrite._lock_head
    moved: list = []

    def someone_moves_main_first(r, locks, wait_s=0.0):
        if not moved:
            tree = _git(repo, "rev-parse", "main^{tree}").strip()
            other = _git(repo, "commit-tree", tree, "-p", "main", "-m", "agent").strip()
            _git(repo, "update-ref", "refs/heads/main", other)
            moved.append(other)
        return real(r, locks, wait_s)

    monkeypatch.setattr(gitwrite, "_lock_head", someone_moves_main_first)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    # The other writer's commit is still on main — the panel did not rewind over it.
    assert moved and _git(repo, "rev-parse", "refs/heads/main").strip() == moved[0]
    assert out["index"] == "pending" and re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert "left there" in out["index_reason"] or "no longer on" in out["index_reason"]
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_a_pending_result_never_prescribes_destructive_recovery(repo, monkeypatch):
    """Hermes on #964 (reviews 4922, 4930, 4934): the pending reason told the operator to run
    `git reset --hard <old head>` on the branch — or `git branch -D` on an unborn one — and
    `GitTab` renders `index_reason` verbatim. Reproduced with real Git: commit `a` while unrelated
    `b` is staged, switch back, follow that advice, and `b` is erased from both index and worktree.

    A string the panel displays is part of the product. It must name what was published and send
    the operator to look, never prescribe a command that destroys work the panel never saw."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    _at_update_ref(monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side"))
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    reason = out["index_reason"]
    assert out["index"] == "pending" and out["sha"] in reason
    for cmd in ("reset --hard", "branch -D", "checkout -f", "clean -fd", "reset --merge"):
        assert cmd not in reason, f"pending reason prescribes `{cmd}`: {reason}"
    # It must still say where the commit is, and make inspection the first step.
    assert f"git log -1 {out['sha'][:7]}" in reason, reason
    assert "git status" in reason, reason


def test_the_moved_checkout_path_takes_no_compensating_ref_write_at_all(repo, monkeypatch):
    """Hermes on #964 (review 4920, 1), reproduced through public COMMIT PATHS: the checkout moves
    off `main` just before publication, so the panel publishes W to `main`. Another process then
    converts `main` into a symbolic ref to `third` — the instant the old rollback
    (`update-ref --no-deref main H W`) ran in. That command replaced the other writer's brand-new
    symbolic ref with a direct one, exited 0 because the OID compare-and-swap still resolved through
    the conversion, and the request reported a clean withdrawal.

    The compensation is gone, so there is no command left to race. This pins that directly: the
    panel issues **exactly one** `update-ref` for the whole request — its own publication — and the
    commit is reported on `main` rather than rewound.

    No conversion is injected here, deliberately. Converting `main` after publication is caught by
    `_fence_branch_conversion`, which runs BEFORE the HEAD check, so the withdrawal path never
    executes and this assertion passes whatever that path does — measured: an earlier version of
    this test did exactly that and survived both mutations that restored the rollback. The
    conversion case has its own regression; this one owns the moved-checkout path, and the reason
    text is asserted so a future routing change fails loudly instead of quietly passing."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    real = gitwrite.run_git_write
    updates: list = []

    def spy(r, args, **kw):
        if args and args[0] == "update-ref":
            updates.append(list(args))
            if len(updates) == 1:
                _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side")  # moved BEFORE publishing
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert len(updates) == 1, f"a compensating ref write was made: {updates}"
    # This is the withdrawal path, not the conversion fence — the two report differently.
    assert "this checkout moved to" in out["index_reason"], out["index_reason"]
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"]) and out["index"] == "pending"
    assert "left there" in out["index_reason"] and out["sha"] in out["index_reason"]
    # main holds the commit git wrote — not rewound, and not deleted.
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == out["sha"]
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_an_unborn_branch_whose_checkout_moved_keeps_the_commit_instead_of_deleting_the_ref(
    root, monkeypatch
):
    """The root-commit variant review 4920 asked for: with no parent the old rollback was
    `update-ref --no-deref -d <ref> <mine>`, deleting the branch outright. A deletion is the same
    unbindable write as an update — more destructive, since it removes the ref another process may
    have just claimed. It is gone too: the first commit stays on the branch and is reported."""
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "x.txt").write_text("x\n")
    real = gitwrite.run_git_write
    updates: list = []

    def spy(r, args, **kw):
        if args and args[0] == "update-ref":
            updates.append(list(args))
            if len(updates) == 1:
                _try_git(p, "symbolic-ref", "HEAD", "refs/heads/side")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    out = gitwrite.git_commit_paths(str(p), "first", ["x.txt"], fps(p, ["x.txt"]), "")
    assert len(updates) == 1, f"a compensating ref write was made: {updates}"
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert _git(p, "rev-parse", "refs/heads/main").strip() == out["sha"], "the branch still exists"
    assert out["index"] == "pending"


# --------------------------------------------------------------------------- review 4863


def _chain_head_to_main(repo):
    """HEAD → refs/heads/alias → refs/heads/main: `symbolic-ref HEAD` resolves the whole chain."""
    _git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/alias")


def test_settle_refuses_a_head_that_reaches_its_branch_through_another_symbolic_ref(
    repo, monkeypatch
):
    """Hermes on #964 (review 4863, 1): with HEAD → alias → main, SETTLE locked HEAD and main but
    not alias. `git symbolic-ref refs/heads/alias refs/heads/side` during the private-index
    installation exited 0 with every lock held, and SETTLE installed W while HEAD now resolved to
    side's H. A HEAD that reaches its branch through another symbolic ref is refused before
    anything is locked or written."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "W")
    w = head(repo)
    _git(repo, "read-tree", "HEAD~1")  # the index back at H; the worktree keeps W's bytes
    _chain_head_to_main(repo)
    fired = _at_scratch_index(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/side")
    )
    with pytest.raises(FsError) as e:
        gitwrite.git_settle(str(repo), w)
    assert e.value.status == 409 and "symbolic ref" in str(e.value)
    assert fired == []
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


@pytest.mark.parametrize("op", ["commit-paths", "revert-file"])
def test_a_head_that_goes_through_another_symbolic_ref_refuses_every_write(repo, op):
    """The same chain refuses commit-paths and REVERT FILE too, with nothing written: which branch
    a write moves is decided by HEAD's IMMEDIATE target, and a chain makes that ambiguous."""
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    (repo / "a.txt").write_text("two\n")
    tip = head(repo)
    expect = fps(repo, ["a.txt"] if op == "commit-paths" else ["b.txt"])
    before_index = _git(repo, "ls-files", "-s")
    _chain_head_to_main(repo)
    with pytest.raises(FsError) as e:
        if op == "commit-paths":
            gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
        else:
            gitwrite.git_discard(str(repo), ["b.txt"], expect, "head", tip)
    assert e.value.status == 409 and "symbolic ref" in str(e.value)
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == tip
    assert _git(repo, "ls-files", "-s") == before_index
    assert (repo / "a.txt").read_text() == "two\n" and (repo / "b.txt").read_text() == "staged\n"
    assert _no_locks(repo) == []


def _swap_gitdir_for_symlink(repo, outside):
    """What a concurrent filesystem writer can do: move `.git` aside, put a symlink in its place."""
    (repo / ".git").rename(repo / ".git-moved")
    (repo / ".git").symlink_to(outside, target_is_directory=True)


def test_index_lock_release_after_a_gitdir_swap_removes_only_our_own_lock(
    repo, monkeypatch, tmp_path
):
    """Hermes on #964 (review 4863, 2): `_IndexLock` kept a pathname. After an idempotent SETTLE's
    compare-and-set, `.git` was renamed and replaced by a symlink to a directory outside the root;
    release deleted that directory's unrelated `index.lock` and left its own in `.git-moved`. The
    index lock is now bound to the directory descriptor it was created in, and release removes a
    file only while it is still the one this process made."""
    (repo / "a.txt").write_text("two\n")
    out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "settled"  # so SETTLE below has nothing to rewrite
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.lock").write_text("another process's lock\n")
    real = gitwrite._index_cas
    swapped: list = []

    def cas_then_swap(*args, **kw):
        res = real(*args, **kw)
        _swap_gitdir_for_symlink(repo, outside)
        swapped.append(True)
        return res

    monkeypatch.setattr(gitwrite, "_index_cas", cas_then_swap)
    gitwrite.git_settle(str(repo), out["sha"])
    assert swapped
    assert (outside / "index.lock").read_text() == "another process's lock\n"
    assert list((repo / ".git-moved").rglob("*.lock")) == []


def test_index_publication_after_a_gitdir_swap_lands_in_the_locked_directory(
    repo, monkeypatch, tmp_path
):
    """The publication half of the same swap: the rewritten index is renamed over `index` inside the
    directory the lock was created in, never through a path that now leads outside the root."""
    (repo / "a.txt").write_text("two\n")
    with _broken_settlement(monkeypatch) as m:
        m.setattr(gitwrite, "_index_cas", _boom)
        out = commit_paths(repo, ["a.txt"])
    assert out["index"] == "pending"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.lock").write_text("another process's lock\n")
    real = gitwrite._write_all_fd
    swapped: list = []

    def write_then_swap(fd, data):
        real(fd, data)
        if not swapped:
            _swap_gitdir_for_symlink(repo, outside)
            swapped.append(True)

    monkeypatch.setattr(gitwrite, "_write_all_fd", write_then_swap)
    gitwrite.git_settle(str(repo), out["sha"])
    assert swapped
    assert (outside / "index.lock").read_text() == "another process's lock\n"
    assert not (outside / "index").exists()
    moved = repo / ".git-moved"
    assert list(moved.rglob("*.lock")) == []
    shown = subprocess.run(
        ["git", f"--git-dir={moved}", f"--work-tree={repo}", "show", ":a.txt"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "two\n"


def test_a_head_read_that_fails_after_publication_still_returns_the_commit(repo, monkeypatch):
    """Hermes on #964 (review 4863, 3): the strict HEAD read after `update-ref` ran outside the
    post-publication handler. With HEAD pointed at a branch whose name is not UTF-8 right after the
    real `update-ref`, commit-paths answered 409 "Nothing was changed" without the SHA, while `main`
    had advanced. Every inspection after publication now reports into the result instead."""
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    bad = b"refs/heads/bad\xffname"
    fired = _at_update_ref(
        monkeypatch,
        lambda: subprocess.run(
            [b"git", b"-C", os.fsencode(repo), b"symbolic-ref", b"HEAD", bad],
            capture_output=True,
        ),
        after=True,
    )
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (flip,) = fired
    assert flip.returncode == 0, flip
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert out["index"] == "pending" and out["index_reason"]
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == out["sha"] != tip
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_a_same_oid_retry_is_judged_by_its_own_publication_not_by_reflog_history(repo, monkeypatch):
    """Hermes on #964 (review 4863, 4): HEAD's reflog was searched for the commit anywhere in its
    last 50 entries. Commit W through the panel, soft-reset to H and retry the same content and
    message in the same second: `commit-tree` returns the same W. A
    `symbolic-ref HEAD refs/heads/side` just before the retry's `update-ref` was then read as a move
    AFTER publication (the first attempt had logged W) and left `main` advanced. Only entries
    appended during THIS publication count. The retry's commit object is pinned to W instead of
    racing the clock for the same second."""
    (repo / "a.txt").write_text("two\n")
    first = commit_paths(repo, ["a.txt"], message="same")
    w = first["sha"]
    parent = _git(repo, "rev-parse", f"{w}~1").strip()
    _git(repo, "reset", "-q", "--soft", parent)
    _git(repo, "branch", "side")
    real_tree = gitwrite._commit_tree

    def same_commit(r, tree, par, message):
        real_tree(r, tree, par, message)
        assert tree == _git(repo, "rev-parse", f"{w}^{{tree}}").strip() and par == parent
        return w

    monkeypatch.setattr(gitwrite, "_commit_tree", same_commit)
    expect = fps(repo, ["a.txt"])
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "HEAD", "refs/heads/side")
    )
    out = gitwrite.git_commit_paths(str(repo), "same", ["a.txt"], expect, parent)
    (flip,) = fired
    assert flip.returncode == 0, flip
    # The evidence question is unchanged: only entries from THIS publication count, so the move is
    # judged as BEFORE. What follows it changed — the commit is reported, never rewound (4920).
    assert out["sha"] == w and out["index"] == "pending"
    assert w in out["index_reason"] and "left there" in out["index_reason"]
    assert _git(repo, "rev-parse", "refs/heads/main").strip() == w
    assert _no_locks(repo) == []


def test_a_branch_turned_symbolic_right_before_publication_leaves_the_commit_on_the_target(
    repo, monkeypatch
):
    """Hermes on #964 (review 4868, 2; then review 4893, 1): `git symbolic-ref refs/heads/main
    refs/heads/side` run immediately before the panel's `update-ref refs/heads/main <W> <H>` makes
    the dereferencing update advance `side`. No form of `update-ref` refuses that on git 2.43
    without following or overwriting the other actor's symref (measured: `--no-deref` replaces it
    with W), so the panel reads where its own update landed from the TARGET's reflog — its unique
    message is there only when the update went through the conversion.

    It then used to put `side` back by compare-and-swap. **It no longer does** (operator decision,
    option B): rewinding `side` means writing to a ref this process never published to, and a
    second conversion landing in the instant before that rollback replaced the other writer's
    symbolic ref with a direct one while the OID compare-and-swap still passed. Holding the
    target's lock cannot close that — git's own `update-ref` needs the same lock — and writing the
    ref by hand appends no reflog entry. So the commit is LEFT where git wrote it, reported with
    its SHA, and nothing belonging to the other process is touched."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    fired = _at_update_ref(
        monkeypatch, lambda: _try_git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/side")
    )
    out = gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    (flip,) = fired
    assert flip.returncode == 0, flip
    # The commit exists and the response says where it is rather than claiming it was undone.
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert out["index"] == "pending"
    assert "left there" in out["index_reason"] and out["sha"] in out["index_reason"]
    # side holds the commit git actually wrote — not rewound to where it was.
    assert _git(repo, "rev-parse", "refs/heads/side").strip() == out["sha"]
    assert _git(repo, "rev-parse", "refs/heads/side").strip() != tip
    # main is still the symref the other process made — not followed, not overwritten, not rewound.
    assert (
        _git(repo, "symbolic-ref", "--no-recurse", "refs/heads/main").strip() == "refs/heads/side"
    )
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_a_repository_with_reflogs_off_is_refused_before_anything_is_published(repo):
    """Hermes on #964 (review 4893, 2), by the operator's decision: with `core.logAllRefUpdates`
    off git records no reflog, and the reflog is the only evidence that tells a publication which
    went through HEAD from one that went through a branch another process converted underneath it.
    Without it, a commit redirected onto the wrong branch stands there permanently and the panel
    cannot even report that it happened. So the refusal comes BEFORE `update-ref`: that
    configuration publishes nothing at all rather than publishing and reporting uncertainty."""
    _git(repo, "config", "core.logAllRefUpdates", "false")
    (repo / "a.txt").write_text("two\n")
    expect, tip = fps(repo, ["a.txt"]), head(repo)
    with pytest.raises(FsError) as e:
        gitwrite.git_commit_paths(str(repo), "m", ["a.txt"], expect, tip)
    assert e.value.status == 409 and "logAllRefUpdates" in str(e.value)
    assert head(repo) == tip, "nothing was published"
    assert index_blob(repo, "a.txt") == "one\n"
    assert _no_locks(repo) == []


def test_the_first_commit_in_a_new_repository_is_not_mistaken_for_reflogs_being_off(repo):
    """The refusal above is keyed on the CONFIG, never on whether `logs/HEAD` exists yet. A
    repository that has had no ref update has no reflog file, so keying on the file refused the
    first commit in every new repository (measured: two root-commit regressions). Reflogs on, no
    reflog file yet — the commit goes through."""
    _git(repo, "config", "core.logAllRefUpdates", "true")
    os.remove(repo / ".git" / "logs" / "HEAD")
    (repo / "a.txt").write_text("two\n")
    out = commit_paths(repo, ["a.txt"], message="first")
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])
    assert head(repo) == out["sha"]


def test_an_identical_publication_after_the_reflog_mark_is_not_taken_for_the_panels_own(
    repo, monkeypatch
):
    """Hermes on #964 (review 4868, 3): every HEAD reflog entry appended after the mark was taken
    as the panel's evidence. Another actor, after the mark, published an identical commit W through
    `main`, soft-reset it to H and pointed HEAD at `side` — all before the panel's own `update-ref`.
    The panel then found that actor's `H → W` entry and left `main` advanced instead of withdrawing
    its own publication made after the checkout had moved. The evidence is now bound to THIS update
    by a message unique to the request. No timestamp is overridden: the other actor's commit is made
    by `git commit-tree` with the same inputs in the same second, and the attempt is retried if the
    clock ticked between the two (asserted equal before anything else)."""
    _git(repo, "branch", "side")
    (repo / "a.txt").write_text("two\n")
    tip = head(repo)
    real_tree, real_write = gitwrite._commit_tree, gitwrite.run_git_write
    for _attempt in range(5):
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(repo, "update-ref", "refs/heads/main", tip)
        _git(repo, "read-tree", tip)
        expect = fps(repo, ["a.txt"])
        theirs: list[str] = []
        mine: list[str] = []

        def commit_twice(r, tree, parent, message, _theirs=theirs):
            made = real_tree(r, tree, parent, message)
            # The same tree, parent, message bytes and identity, within the same second.
            other = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-p", parent],
                input=message,
                capture_output=True,
                text=True,
                check=True,
            )
            _theirs.append(other.stdout.strip())
            return made

        def other_actor_first(r, args, _theirs=theirs, _mine=mine, **kw):
            if args and args[0] == "update-ref" and not _mine:
                _mine.append(next(a for a in args if re.fullmatch(r"[0-9a-f]{40}", a)))
                if _theirs == _mine:
                    # After the mark, before the panel's update: the same W published through
                    # main (HEAD's reflog gains `H W`), reset away, and the checkout moved to side.
                    _git(repo, "update-ref", "refs/heads/main", _mine[0], tip)
                    _git(repo, "reset", "-q", "--soft", tip)
                    _git(repo, "symbolic-ref", "HEAD", "refs/heads/side")
            return real_write(r, args, **kw)

        monkeypatch.setattr(gitwrite, "_commit_tree", commit_twice)
        monkeypatch.setattr(gitwrite, "run_git_write", other_actor_first)
        out = refused = None
        try:
            out = gitwrite.git_commit_paths(str(repo), "same", ["a.txt"], expect, tip)
        except FsError as err:
            refused = err
        monkeypatch.setattr(gitwrite, "_commit_tree", real_tree)
        monkeypatch.setattr(gitwrite, "run_git_write", real_write)
        assert mine, f"the panel never reached its update-ref: {refused}"
        if theirs[0] != mine[0]:
            continue  # the clock ticked between the two commit-trees: set up and try again
        assert theirs[0] == mine[0], "the other actor's commit is the panel's W"
        # The evidence question is what this test exists for, and it is unchanged: the panel must
        # judge the move by ITS OWN stamped entry, not by the other actor's identical `H → W`. It
        # therefore reads the checkout as having moved BEFORE its publication. What follows that
        # verdict changed in review 4920 — the commit is reported where git wrote it, never rewound
        # — so the request returns a result instead of refusing.
        assert refused is None, f"the publication was turned back into a refusal: {refused}"
        assert out is not None and out["sha"] == mine[0] and out["index"] == "pending"
        assert mine[0] in out["index_reason"] and "left there" in out["index_reason"]
        assert _git(repo, "rev-parse", "refs/heads/main").strip() == mine[0]
        assert _no_locks(repo) == []
        return
    pytest.fail("no two identical commits within one second in 5 attempts")
