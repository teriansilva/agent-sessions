"""Found in git (#1740 P1b): what a mission's sessions changed, collected exactly from git.

Every limit the issue names is pinned against a real repository: a file already dirty at baseline
counts only if its content changed; ignored files never count; a deleted file is listed with no
viewer; a branch switch is followed by reachability; two sessions overlapping in one repository
share one item that names both; a change under an excluded folder is never reported; and the
diff route serves the patches mission-bound. A session that joined without a baseline gets a late
one on the first read, and its changes are collected once it leaves or the mission is in review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    files,
    gitpanel,
    mission_shelf,
    mission_shelf_git,
    missions,
    prefs,
    secrets_broker,
    shelf_broker,
)
from agent_sessions.main import create_app
from test_mission_shelf import SESSION, png

OTHER = "codex:bbbbbbbb-0000-4000-8000-000000000002"


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    missions.reset_schema_cache_for_test()
    files.reset_capabilities_for_test()
    gitpanel.reset_flights_for_test()
    mission_shelf.reset_cache_for_test()
    monkeypatch.setattr(mission_shelf, "SETTLE_S", -1.0)
    yield
    missions.reset_schema_cache_for_test()


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "home" / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text("*.log\n")
    (r / "a.txt").write_text("one\n")
    (r / "pre.md").write_text("first\n")
    (r / "steady.txt").write_text("v1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    # Dirty BEFORE the session joins: one will change again, one will not.
    (r / "pre.md").write_text("first\nsecond\n")
    (r / "steady.txt").write_text("v2\n")
    return r


@pytest.fixture
def mission(repo) -> str:
    m = missions.create_mission("assets", cwd=str(repo))["id"]
    missions.adopt(m, SESSION)
    assert mission_shelf_git.take_baselines(m, SESSION) == 1
    return m


def found(mid: str) -> list[dict]:
    return [i for i in mission_shelf.manifest(mid)["items"] if i["source"] == "found"]


def code_item(mid: str) -> dict:
    items = [i for i in found(mid) if i["kind"] == "code"]
    assert len(items) == 1, items
    return items[0]


def test_found_is_exactly_what_changed_since_the_baseline(mission, repo):
    (repo / "c.py").write_text("print('hi')\n")
    git(repo, "add", "c.py")
    git(repo, "commit", "-qm", "add c")
    (repo / "pre.md").write_text("first\nsecond\nthird\n")  # dirty at baseline AND changed again
    (repo / "shot.png").write_bytes(png())  # new, untracked
    (repo / "a.txt").unlink()  # deleted
    (repo / "noise.log").write_text("ignored\n")  # ignored: never counts
    # steady.txt was dirty at baseline and is unchanged since: never counts.
    assert found(mission) == []  # still on the roster, mission running: nothing collected yet
    missions.detach(mission, SESSION)
    item = code_item(mission)
    files_ = {f["path"]: f for f in item["meta"]["files"]}
    assert set(files_) == {"c.py", "pre.md", "shot.png", "a.txt"}
    assert files_["c.py"]["status"] == "A" and files_["c.py"]["from"] == "commit"
    assert files_["pre.md"]["status"] == "M"
    assert files_["shot.png"]["status"] == "A"
    assert files_["a.txt"]["status"] == "D" and "sha" not in files_["a.txt"]
    assert [c["subject"] for c in item["meta"]["commits"]] == ["add c"]
    assert item["title"] == "add c" and item["meta"]["branch"] == "main"
    assert item["meta"]["sessions"] == [SESSION]
    # The viewable files are tiles of their own; source code and deletions are not.
    tiles = {i["title"]: i for i in found(mission) if i["kind"] != "code"}
    assert set(tiles) == {"shot.png", "pre.md"}
    assert tiles["shot.png"]["kind"] == "image" and tiles["shot.png"]["file"] == {"state": "ok"}
    # Collected once.
    mission_shelf_git.collect_due(mission)
    assert len([i for i in found(mission) if i["kind"] == "code"]) == 1


def test_a_branch_switch_is_followed_by_reachability(mission, repo):
    git(repo, "stash", "-q", "-u")
    git(repo, "checkout", "-qb", "feature")
    (repo / "f.txt").write_text("f\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "feature work")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert item["meta"]["branch"] == "feature"
    assert [c["subject"] for c in item["meta"]["commits"]] == ["feature work"]
    paths = {f["path"] for f in item["meta"]["files"]}
    assert "f.txt" in paths


def test_overlapping_sessions_share_one_item_that_names_both(mission, repo):
    missions.adopt(mission, OTHER)
    assert mission_shelf_git.take_baselines(mission, OTHER) == 1
    (repo / "shot.png").write_bytes(png())
    missions.set_state(mission, "draft", "abandoned")  # closed: everything is collected
    items = found(mission)
    tiles = [i for i in items if i["kind"] == "image"]
    assert len(tiles) == 1 and sorted(tiles[0]["sessions"]) == sorted([SESSION, OTHER])
    codes = [i for i in items if i["kind"] == "code"]
    assert len(codes) == 1 and sorted(codes[0]["meta"]["sessions"]) == sorted([SESSION, OTHER])


def test_a_change_under_an_exclusion_is_never_found(mission, repo):
    (repo / "private").mkdir()
    (repo / "private" / "secret.md").write_text("do not show\n")
    (repo / "ok.md").write_text("fine\n")
    prefs.set_folder_exclusions([str(repo / "private")])
    missions.detach(mission, SESSION)
    paths = {f["path"] for f in code_item(mission)["meta"]["files"]}
    assert "private/secret.md" not in paths and "ok.md" in paths
    assert not [i for i in found(mission) if i["title"] == "secret.md"]


def test_a_non_git_folder_finds_nothing(tmp_path):
    plain = tmp_path / "home" / "plain"
    plain.mkdir()
    m = missions.create_mission("x", cwd=str(plain))["id"]
    missions.adopt(m, SESSION)
    mission_shelf_git.take_baselines(m, SESSION)
    (plain / "shot.png").write_bytes(png())
    missions.detach(m, SESSION)
    assert found(m) == []
    assert mission_shelf.manifest(m)["found_pending"] == []


def test_a_session_without_a_baseline_gets_a_late_one_and_is_pending(repo):
    m = missions.create_mission("x", cwd=str(repo))["id"]
    missions.adopt(m, SESSION)  # joined by a path that recorded nothing
    page = mission_shelf.manifest(m)
    assert page["found_pending"] == [SESSION]
    (repo / "late.md").write_text("after the late baseline\n")
    missions.set_state(m, "draft", "abandoned")
    item = code_item(m)
    assert item["meta"]["late"] is True
    assert {f["path"] for f in item["meta"]["files"]} == {"late.md"}
    assert mission_shelf.manifest(m)["found_pending"] == []


def test_the_diff_is_mission_bound_and_carries_each_files_patch(mission, repo, auth_cfg):
    (repo / "c.py").write_text("print('hi')\n")
    git(repo, "add", "c.py")
    git(repo, "commit", "-qm", "add c")
    (repo / "new.md").write_text("# New\n")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    d = mission_shelf_git.diff(mission, item["id"])
    patches = {f["path"]: f["patch"] for f in d["files"]}
    assert "+print('hi')" in patches["c.py"]
    assert "+# New" in patches["new.md"]
    other = missions.create_mission("other", cwd=str(repo))["id"]
    with pytest.raises(missions.MissionError) as e:
        mission_shelf_git.diff(other, item["id"])
    assert e.value.status == 404
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get(f"/api/missions/{mission}/shelf/{item['id']}/diff").status_code == 401
    # The manifest never carries the patches.
    assert "patch" not in json.dumps(mission_shelf.manifest(mission)["items"])


def test_a_repo_defined_filter_or_textconv_never_runs(mission, repo, tmp_path):
    """git reads a sanitized private gitdir: a driver the repository configures is never read."""
    marker = tmp_path / "ran"
    git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    git(repo, "config", "diff.evil.textconv", f"touch {marker}; cat")
    (repo / ".gitattributes").write_text("*.md filter=evil diff=evil\n")
    (repo / "x.md").write_text("x\n")
    missions.detach(mission, SESSION)
    assert code_item(mission)
    mission_shelf_git.diff(mission, code_item(mission)["id"])
    assert not marker.exists()


def test_the_cap_reports_the_overflow(mission, repo, monkeypatch):
    monkeypatch.setattr(mission_shelf_git, "FOUND_FILES_MAX", 2)
    for n in range(4):
        (repo / f"f{n}.py").write_text(f"{n}\n")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert len(item["meta"]["files"]) == 2 and item["meta"]["files_more"] == 2


def test_detach_and_archive_end_the_sessions_shelf_capability(tmp_path, monkeypatch, repo):
    from types import SimpleNamespace

    from agent_sessions import agent_tools

    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda p: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)
    manifest = SimpleNamespace(tools_mcp=agent_tools.CLAUDE_FLAG)
    m = missions.create_mission("x", cwd=str(repo))["id"]
    missions.adopt(m, SESSION)
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=manifest)
    token_file = Path(env[shelf_broker.ENV_TOKEN_FILE])
    token = token_file.read_text()
    # The real token is recognised (its request is judged, not refused as unknown)…
    assert shelf_broker.handle(token, "add", {}).get("error") != "unknown_capability"
    shelf_broker.revoke_session(m, SESSION)
    assert not token_file.exists()
    # …and refused as unknown once the session has left.
    assert shelf_broker.handle(token, "add", {})["error"] == "unknown_capability"
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=manifest)
    token_file = Path(env[shelf_broker.ENV_TOKEN_FILE])
    token = token_file.read_text()
    assert shelf_broker.handle(token, "add", {}).get("error") != "unknown_capability"
    shelf_broker.revoke_launch(SESSION)
    assert not token_file.exists()
    assert shelf_broker.handle(token, "add", {})["error"] == "unknown_capability"
    con = missions._ready()
    assert (
        con.execute("SELECT COUNT(*) FROM mission_shelf_caps WHERE revoked_at IS NULL").fetchone()[
            0
        ]
        == 0
    )
    con.close()


# ---- review of #1923: windows, keys, and what git is asked ------------------------------------

PLACEHOLDER = "claude:new-0000aaaa"


def baseline_rows(mid: str) -> list[dict]:
    con = missions._ready()
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM mission_shelf_baselines WHERE mission_id=? ORDER BY taken_at",
                (mid,),
            ).fetchall()
        ]
    finally:
        con.close()


def bind(logical: str, physical: str) -> None:
    import time as _t

    with missions._write_lock:
        con = missions._ready()
        try:
            missions._bind_runtime_tx(con, logical, physical, _t.time())
            con.commit()
        finally:
            con.close()


def test_a_glob_named_file_never_widens_a_patch_into_an_exclusion(mission, repo):
    (repo / "private").mkdir()
    (repo / "private" / "secret.md").write_text("do not show\n")
    (repo / "*").write_text("star\n")
    git(repo, "add", "--", ":(literal)*", "private/secret.md")
    git(repo, "commit", "-qm", "both")
    prefs.set_folder_exclusions([str(repo / "private")])
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert {f["path"] for f in item["meta"]["files"]} == {"*"}
    d = mission_shelf_git.diff(mission, item["id"])
    blob = json.dumps(d)
    assert "+star" in blob
    assert "do not show" not in blob and "secret.md" not in blob


def test_a_launch_baseline_waits_for_its_session_to_join(repo):
    """A dispatch records the baseline under its key BEFORE the session is adopted: a read in that
    window is not the session leaving, and must not close it."""
    m = missions.create_mission("x", cwd=str(repo))["id"]
    snaps = mission_shelf_git.prepare_baselines(m)
    assert mission_shelf_git.record_baselines(m, SESSION, snaps) == 1
    assert found(m) == [] and mission_shelf.manifest(m)["found_pending"] == []
    assert baseline_rows(m)[0]["collected_at"] is None  # still open
    missions.adopt(m, SESSION)
    (repo / "work.md").write_text("the session's work\n")
    assert found(m) == []
    assert mission_shelf.manifest(m)["found_pending"] == [SESSION]
    missions.detach(m, SESSION)
    assert {f["path"] for f in code_item(m)["meta"]["files"]} == {"work.md"}


def test_a_launch_that_never_joined_is_closed_with_nothing(repo):
    m = missions.create_mission("x", cwd=str(repo))["id"]
    mission_shelf_git.record_baselines(m, SESSION, mission_shelf_git.prepare_baselines(m))
    (repo / "stray.md").write_text("not the mission's\n")
    missions.set_state(m, "draft", "abandoned")
    assert found(m) == []
    (row,) = baseline_rows(m)
    assert row["outcome"] == "never joined" and row["collected_at"] is not None


def test_a_late_bound_session_has_one_window_under_both_keys(repo):
    """The launch records under its placeholder; the roster adopts the real id, bound to it. The
    shelf must not open a second (late) window for the real id."""
    m = missions.create_mission("x", cwd=str(repo))["id"]
    mission_shelf_git.record_baselines(m, PLACEHOLDER, mission_shelf_git.prepare_baselines(m))
    missions.adopt(m, SESSION)
    bind(SESSION, PLACEHOLDER)
    (repo / "first.md").write_text("before the first read\n")
    page = mission_shelf.manifest(m)
    assert page["found_pending"] == [SESSION]
    assert len(baseline_rows(m)) == 1
    assert mission_shelf_git.take_baselines(m, SESSION) == 0  # the adopt route: already open
    (repo / "second.md").write_text("after it\n")
    missions.detach(m, SESSION)
    item = code_item(m)
    assert {f["path"] for f in item["meta"]["files"]} == {"first.md", "second.md"}
    assert item["meta"]["sessions"] == [SESSION] and item["meta"]["late"] is False


def test_a_repository_with_no_commit_at_baseline_lists_its_commits(tmp_path):
    r = tmp_path / "home" / "empty"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    m = missions.create_mission("x", cwd=str(r))["id"]
    missions.adopt(m, SESSION)
    assert mission_shelf_git.take_baselines(m, SESSION) == 1
    (r / "first.py").write_text("print(1)\n")
    git(r, "add", "first.py")
    git(r, "commit", "-qm", "the first commit")
    missions.detach(m, SESSION)
    item = code_item(m)
    assert [c["subject"] for c in item["meta"]["commits"]] == ["the first commit"]
    (f,) = item["meta"]["files"]
    assert f["path"] == "first.py" and f["status"] == "A" and f["from"] == "commit"
    assert "+print(1)" in mission_shelf_git.diff(m, item["id"])["files"][0]["patch"]


def test_a_staged_new_file_is_added_and_a_staged_rename_lists_its_source(mission, repo):
    (repo / "staged.md").write_text("# Staged\n")
    git(repo, "add", "staged.md")
    git(repo, "mv", "a.txt", "moved.txt")
    missions.detach(mission, SESSION)
    files_ = {f["path"]: f for f in code_item(mission)["meta"]["files"]}
    assert files_["staged.md"]["status"] == "A"
    assert files_["moved.txt"]["status"] == "A"
    assert files_["a.txt"]["status"] == "D"


def test_a_commit_that_only_touched_an_exclusion_is_not_on_the_shelf(mission, repo):
    (repo / "private").mkdir()
    (repo / "private" / "plan.md").write_text("x\n")
    git(repo, "add", "private/plan.md")
    git(repo, "commit", "-qm", "the secret plan")
    prefs.set_folder_exclusions([str(repo / "private")])
    missions.detach(mission, SESSION)
    assert found(mission) == []  # nothing else changed: no code item at all
    assert "secret plan" not in json.dumps(mission_shelf.manifest(mission))


def test_a_listed_change_never_carries_an_excluded_commits_subject(mission, repo):
    (repo / "private").mkdir()
    (repo / "private" / "plan.md").write_text("x\n")
    git(repo, "add", "private/plan.md")
    git(repo, "commit", "-qm", "the secret plan")
    (repo / "ok.md").write_text("fine\n")
    prefs.set_folder_exclusions([str(repo / "private")])
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert item["meta"]["commits"] == [] and item["title"] == "Uncommitted changes in proj"
    assert "secret plan" not in json.dumps(mission_shelf.manifest(mission))


def test_the_diff_judges_the_boundary_as_it_is_now(mission, repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("later excluded\n")
    (repo / "ok.md").write_text("fine\n")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert "docs/x.md" in {f["path"] for f in mission_shelf_git.diff(mission, item["id"])["files"]}
    prefs.set_folder_exclusions([str(repo / "docs")])
    d = mission_shelf_git.diff(mission, item["id"])
    assert {f["path"] for f in d["files"]} == {"ok.md"}
    assert "later excluded" not in json.dumps(d)


def test_a_comparison_that_keeps_failing_is_given_up_on(mission, repo, monkeypatch):
    def broken(*_a, **_k):
        raise gitpanel.GitError("boom", status=400)

    monkeypatch.setattr(mission_shelf_git, "_compare", broken)
    missions.detach(mission, SESSION)
    for _ in range(mission_shelf_git.COLLECT_ATTEMPTS):
        assert mission_shelf.manifest(mission, found=False)["found_pending"] == [SESSION]
        assert mission_shelf_git.collect_due(mission) == 0
    (row,) = baseline_rows(mission)
    assert row["outcome"] == "failed" and row["attempts"] == mission_shelf_git.COLLECT_ATTEMPTS
    assert row["collected_at"] is not None
    assert mission_shelf.manifest(mission)["found_pending"] == []


def test_a_claimed_window_is_collected_once(mission, repo):
    (repo / "x.md").write_text("x\n")
    con = missions._ready()
    con.execute("UPDATE mission_shelf_baselines SET claimed_at=?", (__import__("time").time(),))
    con.commit()
    con.close()
    missions.detach(mission, SESSION)
    assert mission_shelf_git.collect_due(mission) == 0 and found(mission) == []
    con = missions._ready()
    con.execute("UPDATE mission_shelf_baselines SET claimed_at=1")  # abandoned long ago
    con.commit()
    con.close()
    assert mission_shelf_git.collect_due(mission) == 1
    assert code_item(mission)


def test_a_session_back_on_the_roster_gets_a_new_window(mission, repo):
    (repo / "one.md").write_text("first window\n")
    missions.detach(mission, SESSION)
    first = code_item(mission)
    missions.adopt(mission, SESSION)
    assert mission_shelf_git.take_baselines(mission, SESSION) == 1  # the adopt route
    (repo / "two.md").write_text("second window\n")
    missions.detach(mission, SESSION)
    codes = [i for i in found(mission) if i["kind"] == "code"]
    assert len(codes) == 2
    second = next(i for i in codes if i["id"] != first["id"])
    assert {f["path"] for f in second["meta"]["files"]} == {"two.md"}


def test_a_reopened_mission_opens_new_windows_for_its_roster(mission, repo):
    missions.set_state(mission, "draft", "planned")
    missions.set_state(mission, "planned", "running")
    (repo / "one.md").write_text("before review\n")
    missions.set_state(mission, "running", "review")
    assert len([i for i in found(mission) if i["kind"] == "code"]) == 1
    (closed,) = baseline_rows(mission)
    mission_shelf.manifest(mission)
    assert baseline_rows(mission) == [closed]  # in review: no new window is opened
    missions.set_state(mission, "review", "running")
    mission_shelf.manifest(mission)  # the first read after reopening: a late window
    (row,) = baseline_rows(mission)
    assert row["collected_at"] is None and row["late"] == 1
    (repo / "two.md").write_text("after reopening\n")
    missions.set_state(mission, "running", "review")
    codes = [i for i in found(mission) if i["kind"] == "code"]
    assert len(codes) == 2
    assert any({f["path"] for f in c["meta"]["files"]} == {"two.md"} for c in codes)


def test_worktree_too_means_the_tree_is_dirty_now(mission, repo):
    (repo / "pre.md").write_text("first\nsecond\ncommitted\n")  # dirty at baseline, then committed
    (repo / "c.py").write_text("1\n")
    git(repo, "add", "pre.md", "c.py")
    git(repo, "commit", "-qm", "commit both")
    (repo / "c.py").write_text("2\n")  # c.py changes again after its commit
    missions.detach(mission, SESSION)
    files_ = {f["path"]: f for f in code_item(mission)["meta"]["files"]}
    assert files_["c.py"].get("worktree_too") is True
    assert "worktree_too" not in files_["pre.md"]


def test_a_shelf_read_never_waits_for_a_refresh_in_flight(mission, repo):
    lock = mission_shelf_git._mission_lock(mission)
    (repo / "x.md").write_text("x\n")
    missions.detach(mission, SESSION)
    with lock:
        t0 = __import__("time").monotonic()
        mission_shelf_git.refresh(mission, wait=False)  # returns at once, collects nothing
        assert __import__("time").monotonic() - t0 < 5
        assert mission_shelf.manifest(mission, found=False)["found_pending"] == [SESSION]
    mission_shelf_git.refresh(mission, wait=False)
    assert code_item(mission)


def _clean_repo(tmp_path, name: str, n: int) -> Path:
    r = tmp_path / "home" / name
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    for i in range(n):
        (r / f"f{i:02}.txt").write_text(f"{i}\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "twenty files")
    return r


def test_an_orphan_branch_never_invents_deletions(tmp_path):
    """Hermes 7938: unrelated histories have no merge base; a diff across the two trees reported a
    phantom deletion for every file of the old branch."""
    r = _clean_repo(tmp_path, "site", 20)
    m = missions.create_mission("x", cwd=str(r))["id"]
    missions.adopt(m, SESSION)
    assert mission_shelf_git.take_baselines(m, SESSION) == 1
    git(r, "checkout", "-q", "--orphan", "gh-pages")
    git(r, "rm", "-rqf", ".")
    (r / "index.html").write_text("<h1>site</h1>\n")
    git(r, "add", "index.html")
    git(r, "commit", "-qm", "publish")
    missions.detach(m, SESSION)
    item = code_item(m)
    assert [(f["path"], f["status"]) for f in item["meta"]["files"]] == [("index.html", "A")]
    assert [c["subject"] for c in item["meta"]["commits"]] == ["publish"]
    assert item["meta"]["history"] == "unrelated"
    d = json.dumps(mission_shelf_git.diff(m, item["id"]))
    assert "+<h1>site</h1>" in d and "f00.txt" not in d


def test_a_baseline_commit_gone_from_the_store_keeps_the_working_tree_changes(mission, repo):
    con = missions._ready()
    con.execute("UPDATE mission_shelf_baselines SET head=?", ("d" * 40,))
    con.commit()
    con.close()
    (repo / "kept.md").write_text("exact, from the tree\n")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert "kept.md" in {f["path"] for f in item["meta"]["files"]}
    assert item["meta"]["commits"] == [] and item["meta"]["history"] == "base_missing"
    assert baseline_rows(mission)[0]["outcome"] == "collected"


def test_a_commit_subject_with_the_separator_keeps_its_paths(mission, repo):
    (repo / "x.md").write_text("x\n")
    git(repo, "add", "x.md")
    git(repo, "commit", "-qm", "odd\x1esubject\x1fhere")
    missions.detach(mission, SESSION)
    item = code_item(mission)
    assert len(item["meta"]["commits"]) == 1
    assert item["meta"]["commits"][0]["subject"].startswith("odd")


def test_a_symlink_is_never_shelved_as_a_file_tile(mission, repo):
    (repo / "target.png").write_bytes(png())
    git(repo, "add", "target.png")
    git(repo, "commit", "-qm", "image")
    (repo / "link.png").symlink_to(repo / "target.png")
    missions.detach(mission, SESSION)
    tiles = {i["title"] for i in found(mission) if i["kind"] != "code"}
    assert "link.png" not in tiles and "target.png" in tiles


def test_a_file_past_the_hash_cap_still_gets_its_tile(mission, repo, monkeypatch):
    monkeypatch.setattr(mission_shelf_git, "HASH_MAX", 10)
    (repo / "render.png").write_bytes(png())  # "large": keyed by size and mtime, not hashed
    missions.detach(mission, SESSION)
    tiles = {i["title"]: i for i in found(mission) if i["kind"] != "code"}
    assert tiles["render.png"]["kind"] == "image"
