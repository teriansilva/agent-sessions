"""#1196 — playbooks and git: clone, seed, forge remote creation, update-as-commit and PR.

Every git test runs real git against real repositories. A local bare repository stands in for
the forge's git side (the `file` transport is added back FOR MECHANICS TESTS ONLY, exactly like
`tests/test_gitwrite.py::local_transport`); refusals of local remotes are tested against the
shipped allowlist. The forge API is an `httpx.MockTransport` — CI never reaches a network.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import httpx
import pytest

from agent_sessions import (
    fileedit,
    files,
    forge_write,
    gitpanel,
    gitwrite,
    prefs,
    projects,
    template_vars,
)
from agent_sessions.playbooks import (
    apply,
    lifecycle,
    loader,
    publication,
    repo_git,
    review,
    store,
)

KEY = "playbook-git-tests-signing-key-32-characters"
REAL_REMOTE_REPOSITORY = publication._remote_repository
TOKEN = "tok-secret-1196"


def bundle_files(
    version="1.0.0",
    rules="Check {{forge_url}}.\n",
    forge_default=False,
    guide="A guide.\n",
    extra_files=None,
):
    default = '\ndefault = "https://forge.test"' if forge_default else ""
    extra = "".join(
        f'[[materials]]\npath = "{path}"\ndisposition = "{disposition}"\n'
        for path, (disposition, _) in (extra_files or {}).items()
    )
    files = {
        "playbook.toml": f"""format = 2
[identity]
id = "git-demo"
name = "Git demo"
publisher = "Example"
version = "{version}"
domain = "development"
summary = "Exercise the git half of deploy."
[[variables]]
name = "forge_url"
type = "url"{default}
[[variables]]
name = "token"
kind = "secret"
[[connections]]
name = "forge"
kind = "forge"
url = "{{{{forge_url}}}}"
credential = "token"
verify = "none"
[[materials]]
path = "RULES.md"
disposition = "managed"
template = true
[[materials]]
path = "docs/guide.md"
disposition = "managed"
""",
        "template/RULES.md": rules,
        "template/docs/guide.md": guide,
    }
    files["playbook.toml"] += extra
    for path, (_, text) in (extra_files or {}).items():
        files[f"template/{path}"] = text
    return files


def _git(repo, *args, check=True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    ).stdout


def _write_bundle(**kw) -> str:
    source = store.local_root() / "git-demo"
    contents = store.tree_from_files(bundle_files(**kw))
    loader.validate_named(contents, "git-demo")
    for relative, data in contents.files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return contents.digest()


@pytest.fixture
def env(tmp_home, tmp_path, monkeypatch):
    assert fileedit.install_lease_signal_handler()
    monkeypatch.setattr(loader, "BUNDLED_ROOT", tmp_path / "no-bundled")
    monkeypatch.setattr(prefs, "get_project_roots", lambda: [str(tmp_path)])
    monkeypatch.delenv("AGENT_SESSIONS_FS_ROOT", raising=False)
    (tmp_path / ".gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.test\n")
    files.reset_capabilities_for_test()
    gitpanel.reset_flights_for_test()
    forge = FakeForge()
    forge_write.set_transport_for_test(httpx.MockTransport(forge.handler))
    yield tmp_path, forge
    forge_write.set_transport_for_test(None)
    gitpanel.reset_flights_for_test()
    files.reset_capabilities_for_test()


@pytest.fixture
def local_transport(monkeypatch):
    """MECHANICS ONLY: a bare repository on disk is the only remote a hermetic test can build."""
    monkeypatch.setattr(gitwrite, "GIT_ALLOW_PROTOCOL", gitwrite.GIT_ALLOW_PROTOCOL + ":file")
    # The bare path stands in for `https://forge.test/acme/proj.git`.
    monkeypatch.setattr(publication, "_remote_repository", lambda *a: ("acme", "proj"))


class FakeForge:
    """A Forgejo-shaped API with exactly the endpoints a publication may use."""

    def __init__(self):
        self.repos: dict[str, dict] = {}
        self.pulls: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_pull_create = 0
        self.lose_create_response = False
        self.refuse_create = False
        #: How this forge writes an ssh address; every repository answer carries it.
        self.ssh_prefix = "git@forge.test:"
        self.visible = [{"full_name": "acme/existing"}]
        self.created_ssh_url: str | None = None

    def _json(self, status, body):
        return httpx.Response(status, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        assert request.headers.get("authorization") == f"token {TOKEN}"
        assert TOKEN not in str(request.url)
        if path == "/api/v1/user":
            return self._json(200, {"login": "me"})
        if path == "/api/v1/user/repos" and method == "GET":
            rows = [
                {**r, "ssh_url": f"{self.ssh_prefix}{r['full_name']}.git"} for r in self.visible
            ]
            return self._json(200, rows)
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)", path)
        if m and method == "GET":
            repo = self.repos.get(f"{m[1]}/{m[2]}")
            return self._json(200, repo) if repo else self._json(404, {})
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)/pulls", path)
        if m and method == "GET":
            return self._json(200, [p for p in self.pulls if p["state"] == "open"])
        if m and method == "POST":
            if self.fail_pull_create:
                self.fail_pull_create -= 1
                return self._json(500, {})
            body = json.loads(request.content)
            pr = {
                "number": len(self.pulls) + 1,
                "state": "open",
                "html_url": f"https://forge.test/{m[1]}/{m[2]}/pulls/{len(self.pulls) + 1}",
                "head": {"ref": body["head"], "repo": {"full_name": f"{m[1]}/{m[2]}"}},
                "base": {"ref": body["base"]},
                "title": body["title"],
            }
            self.pulls.append(pr)
            return self._json(201, pr)
        m = re.fullmatch(r"/api/v1/(?:orgs/([^/]+)|user)/repos", path)
        if m and method == "POST":
            if self.refuse_create:
                self.refuse_create = False
                return self._json(503, {})
            owner = m[1] or "me"
            body = json.loads(request.content)
            full = f"{owner}/{body['name']}"
            if full in self.repos:
                return self._json(409, {})
            self.repos[full] = {
                "full_name": full,
                "description": body["description"],
                "private": body["private"],
                "clone_url": f"https://forge.test/{full}.git",
                "ssh_url": self.created_ssh_url or f"{self.ssh_prefix}{full}.git",
                "html_url": f"https://forge.test/{full}",
            }
            if self.lose_create_response:
                self.lose_create_response = False
                return self._json(502, {})
            return self._json(201, self.repos[full])
        return self._json(405, {})


def deploy(folder, *, forge_default=False, bind_url=True, op="1", **bundle):
    revision = _write_bundle(forge_default=forge_default, **bundle)
    entity = projects.create("Git project", folders=[str(folder)], default_folder=str(folder))
    bindings = [{"name": "token", "kind": "secret", "value": TOKEN}]
    if bind_url:
        bindings.append({"name": "forge_url", "kind": "text", "value": "https://forge.test"})
    body = {
        "destination": str(folder),
        "revision": revision,
        "project_id": entity.id,
        "bindings": bindings,
    }
    plan = review.build("git-demo", body, key=KEY)
    targets = [t["id"] for t in plan.public["targets"] if t["requires_confirmation"]]
    receipt = review.confirm(plan, plan.public["digest"], targets, key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, "git-demo", body, receipt, f"bind-op-0000{op}", key=KEY)
    apply.apply(entity.id, f"apply-op-0000{op}", bound["receipt"], key=KEY)
    return entity


def redeploy(entity, op="2", **bundle):
    """Bind and apply a (possibly new) revision of the playbook to the same project."""
    revision = _write_bundle(**bundle)
    with apply.state.locked(entity.id) as locked:
        stored = locked.read()["inputs"]
    body = {**stored, "revision": revision}
    planned = review.build("git-demo", body, key=KEY)
    targets = [t["id"] for t in planned.public["targets"] if t["requires_confirmation"]]
    receipt = review.confirm(planned, planned.public["digest"], targets, key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, "git-demo", body, receipt, f"bind-op-0000{op}", key=KEY)
    apply.apply(entity.id, f"apply-op-0000{op}", bound["receipt"], key=KEY)


def _merge(folder, bare, commit):
    """The PR merged: the remote default branch and the local one move to the commit."""
    _git(folder, "push", "-q", "origin", f"{commit}:refs/heads/main")
    _git(folder, "branch", "-f", "main", commit)
    _git(folder, "switch", "-q", "main")


@pytest.fixture
def repo_project(env, local_transport):
    return _repo_project(env)


def _repo_project(env, **bundle):
    """A project folder that is a clone of a bare remote, with the playbook applied over it."""
    tmp, forge = env
    bare = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    folder = tmp / "proj"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "main")
    (folder / "README.md").write_text("hello\n")
    (folder / "other.txt").write_text("operator work\n")
    _git(folder, "add", "README.md")
    _git(folder, "commit", "-q", "-m", "init")
    _git(folder, "remote", "add", "origin", str(bare))
    _git(folder, "push", "-q", "origin", "main")
    entity = deploy(folder, **bundle)
    return entity, folder, bare, forge


def plan(entity, **body):
    return publication.plan_update(entity.id, body, key=KEY)


def publish(entity, planned, op="publish-op-1", **body):
    return publication.publish_update(
        entity.id, {**body, "digest": planned["digest"], "operation_id": op}, key=KEY
    )


def _branch_commit(bare, branch="playbook/git-demo-v1.0.0"):
    return _git(
        bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    ).strip()


# --- update as commit + PR ---------------------------------------------------------------------


def test_publication_commits_only_the_playbook_paths_on_the_approved_base_and_opens_a_pr(
    repo_project,
):
    entity, folder, bare, forge = repo_project
    _git(folder, "add", "other.txt")  # the operator's own staged work
    base = _git(folder, "rev-parse", "HEAD").strip()
    planned = plan(entity)
    assert planned["base"] == base and planned["default_branch"] == "main"
    assert planned["branch"] == "playbook/git-demo-v1.0.0"
    assert sorted(planned["paths"]) == ["RULES.md", "docs/guide.md"]
    assert planned["forge"]["url"] == "https://forge.test" and TOKEN not in json.dumps(planned)
    out = publish(entity, planned)
    assert out["pushed"] is True and out["pull_request"]["number"] == 1
    commit = out["commit"]
    assert _branch_commit(bare) == commit
    assert _git(folder, "rev-parse", f"{commit}^").strip() == base
    changed = _git(folder, "diff-tree", "-r", "--name-only", "--no-commit-id", commit).split()
    assert sorted(changed) == ["RULES.md", "docs/guide.md"]
    # The operator's staged file stayed staged and out of the commit.
    assert "other.txt" in _git(folder, "diff", "--cached", "--name-only")
    assert forge.pulls[0]["head"]["ref"] == "playbook/git-demo-v1.0.0"
    assert forge.pulls[0]["base"]["ref"] == "main"
    assert TOKEN not in json.dumps(out)
    assert TOKEN not in _git(folder, "config", "--list")
    # A same-id retry of a completed publication changes nothing and duplicates nothing.
    assert publish(entity, planned) == out
    assert len(forge.pulls) == 1
    assert publication.operation(entity.id, "publish-op-1")["commit"] == commit


def test_a_concurrent_git_add_never_enters_the_publication_commit(repo_project, monkeypatch):
    """The path-commit invariant: another writer staging work mid-commit stays out of it."""
    entity, folder, bare, _ = repo_project
    planned = plan(entity)
    real = gitwrite._snapshot_for_index

    def racing(repo, names):
        subprocess.run(["git", "-C", str(folder), "add", "other.txt"], check=True)
        return real(repo, names)

    monkeypatch.setattr(gitwrite, "_snapshot_for_index", racing)
    out = publish(entity, planned)
    changed = _git(folder, "diff-tree", "-r", "--name-only", "--no-commit-id", out["commit"])
    assert sorted(changed.split()) == ["RULES.md", "docs/guide.md"]
    assert "other.txt" in _git(folder, "diff", "--cached", "--name-only")


def test_an_edit_after_apply_refuses_and_asks_for_a_new_review(repo_project):
    entity, folder, bare, forge = repo_project
    (folder / "RULES.md").write_text("operator edit\n")
    with pytest.raises(store.Conflict, match="changed after the playbook applied") as e:
        plan(entity)
    assert e.value.extra["paths"] == ["RULES.md"]


def test_an_edit_between_plan_and_publish_refuses_before_anything_is_written(repo_project):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    (folder / "docs" / "guide.md").write_text("changed\n")
    with pytest.raises(store.Conflict):
        publish(entity, planned)
    assert _branch_commit(bare) == "" and forge.pulls == []
    assert _git(folder, "branch", "--list", "playbook/*").strip() == ""


def test_a_same_size_same_mtime_rewrite_is_caught_by_the_content_binding(repo_project, monkeypatch):
    """A row fingerprint is (size, mtime): this rewrite keeps both. The bound blob refuses it."""
    entity, folder, bare, _ = repo_project
    planned = plan(entity)
    target = folder / "docs" / "guide.md"
    real = gitwrite._snapshot_for_index

    def rewrite(repo, names):
        st = target.stat()
        target.write_text("B guide.\n")  # same length as "A guide.\n"
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        return real(repo, names)

    monkeypatch.setattr(gitwrite, "_snapshot_for_index", rewrite)
    with pytest.raises(store.StoreError, match="no longer the content that was reviewed") as e:
        publish(entity, planned)
    assert e.value.status == 409
    assert _branch_commit(bare) == ""
    # Nothing landed, so the checkout is back where it was and the branch this op made is gone.
    assert _git(folder, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert _git(folder, "branch", "--list", "playbook/*").strip() == ""
    assert e.value.extra["checkout"] == {"branch": "main", "head": planned["base"]}


def test_a_failed_commit_puts_the_checkout_back_and_a_retry_publishes(repo_project, monkeypatch):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    real = gitwrite.git_commit_paths
    monkeypatch.setattr(
        gitwrite,
        "git_commit_paths",
        lambda *a, **k: (_ for _ in ()).throw(gitpanel.GitError("index busy", status=409)),
    )
    with pytest.raises(store.StoreError, match="index busy") as e:
        publish(entity, planned)
    assert _git(folder, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert _git(folder, "branch", "--list", "playbook/*").strip() == ""
    assert e.value.extra["checkout"]["branch"] == "main"
    assert publication.operation(entity.id, "publish-op-1")["commit"] is None
    monkeypatch.setattr(gitwrite, "git_commit_paths", real)
    out = publish(entity, planned)
    assert _branch_commit(bare) == out["commit"] and len(forge.pulls) == 1


def test_after_a_publish_the_checkout_is_stated_and_the_operators_branch_is_untouched(
    repo_project,
):
    entity, folder, bare, _ = repo_project
    before = _git(folder, "config", "--get-regexp", r"^branch\.main\.", check=False)
    out = publish(entity, plan(entity))
    assert out["checkout"] == {
        "branch": "playbook/git-demo-v1.0.0",
        "head": out["commit"],
        "previous_branch": "main",
    }
    assert _git(folder, "symbolic-ref", "HEAD").strip() == "refs/heads/playbook/git-demo-v1.0.0"
    assert _git(folder, "config", "--get-regexp", r"^branch\.main\.", check=False) == before
    assert _git(folder, "config", "branch.playbook/git-demo-v1.0.0.merge").strip() == (
        "refs/heads/playbook/git-demo-v1.0.0"
    )


SEED = {"NOTES.md": ("seed", "Your notes.\n")}
REGION = {"README.md": ("managed", "Managed text.\n")}


def test_an_edited_seed_never_blocks_an_update_and_is_never_committed(env, local_transport):
    """Reproduced: a seed the operator edits used to refuse every publication forever."""
    entity, folder, bare, _ = _repo_project(env, extra_files=SEED)
    assert (folder / "NOTES.md").read_text() == "Your notes.\n"
    (folder / "NOTES.md").write_text("my own notes now\n")  # the seed belongs to the project
    planned = plan(entity)
    assert sorted(planned["paths"]) == ["RULES.md", "docs/guide.md"]
    out = publish(entity, planned)
    changed = _git(folder, "diff-tree", "-r", "--name-only", "--no-commit-id", out["commit"])
    assert sorted(changed.split()) == ["RULES.md", "docs/guide.md"]
    assert (folder / "NOTES.md").read_text() == "my own notes now\n"


def test_an_edit_outside_the_region_of_a_file_not_being_committed_blocks_nothing(
    env, local_transport
):
    entity, folder, bare, _ = _repo_project(env, extra_files=REGION)
    first = publish(entity, plan(entity))
    assert "README.md" in _git(
        folder, "diff-tree", "-r", "--name-only", "--no-commit-id", first["commit"]
    )
    _merge(folder, bare, first["commit"])
    redeploy(entity, version="1.1.0", guide="A better guide.\n", extra_files=REGION)
    readme = folder / "README.md"
    readme.write_text(readme.read_text().replace("hello\n", "hello, edited\n", 1))
    planned = plan(entity)
    assert sorted(planned["paths"]) == ["docs/guide.md"]
    out = publish(entity, planned, op="publish-op-2")
    changed = _git(folder, "diff-tree", "-r", "--name-only", "--no-commit-id", out["commit"])
    assert changed.split() == ["docs/guide.md"]
    assert "hello, edited" in readme.read_text()


def test_a_reapply_that_keeps_a_path_refreshes_what_it_binds(env, local_transport):
    """An out-of-region edit to a file being committed refuses; re-applying (the region is
    unchanged, so the path is a `keep`) makes the reviewed bytes the new baseline."""
    entity, folder, bare, _ = _repo_project(env, extra_files=REGION)
    readme = folder / "README.md"
    readme.write_text(readme.read_text().replace("hello\n", "hello, edited\n", 1))
    with pytest.raises(store.Conflict, match="changed after the playbook applied") as e:
        plan(entity)
    assert e.value.extra["paths"] == ["README.md"]
    redeploy(entity, extra_files=REGION)
    planned = plan(entity)
    assert sorted(planned["paths"]) == ["README.md", "RULES.md", "docs/guide.md"]
    out = publish(entity, planned)
    shown = _git(folder, "show", f"{out['commit']}:README.md")
    assert "hello, edited" in shown and "Managed text." in shown


def test_unpushed_commits_or_a_moved_head_refuse_publication(repo_project):
    entity, folder, bare, _ = repo_project
    (folder / "x.txt").write_text("x\n")
    _git(folder, "add", "x.txt")
    _git(folder, "commit", "-q", "-m", "unpushed")
    with pytest.raises(store.Conflict, match="sync it first"):
        plan(entity)
    _git(folder, "push", "-q", "origin", "main")
    planned = plan(entity)
    (folder / "y.txt").write_text("y\n")
    _git(folder, "add", "y.txt")
    _git(folder, "commit", "-q", "-m", "after the plan")
    with pytest.raises(store.Conflict, match="sync it first"):
        publish(entity, planned)  # the plan is recomputed at the effect, against the moved HEAD
    assert _branch_commit(bare) == ""


def test_an_existing_local_branch_with_other_content_is_refused(repo_project):
    entity, folder, bare, _ = repo_project
    planned = plan(entity)
    _git(folder, "branch", "playbook/git-demo-v1.0.0")
    _git(folder, "switch", "-q", "playbook/git-demo-v1.0.0")
    (folder / "z.txt").write_text("z\n")
    _git(folder, "add", "z.txt")
    _git(folder, "commit", "-q", "-m", "someone else's")
    _git(folder, "switch", "-q", "main")
    with pytest.raises(store.Conflict, match="already exists with other content"):
        publish(entity, planned)


def test_a_remote_branch_with_other_content_is_refused_never_force_pushed(repo_project):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    other = folder.parent / "elsewhere"
    _git(folder.parent, "clone", "-q", str(bare), str(other))
    (other / "w.txt").write_text("w\n")
    _git(other, "add", "w.txt")
    _git(other, "-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "theirs")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/playbook/git-demo-v1.0.0")
    theirs = _branch_commit(bare)
    with pytest.raises(store.Conflict, match="never force-pushed") as e:
        publish(entity, planned)
    assert e.value.extra["commit"]  # the local commit stands and is reported
    assert _branch_commit(bare) == theirs and forge.pulls == []


def test_a_failed_push_leaves_the_commit_and_a_retry_only_pushes(repo_project, monkeypatch):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    real = gitwrite.git_push
    calls = []

    def failing(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise gitpanel.GitError("the remote hung up", status=502)
        return real(*a, **k)

    monkeypatch.setattr(gitwrite, "git_push", failing)
    with pytest.raises(store.StoreError) as e:
        publish(entity, planned)
    commit = e.value.extra["commit"]
    assert e.value.extra["operation_id"] == "publish-op-1"
    assert _git(folder, "rev-parse", "HEAD").strip() == commit
    assert _branch_commit(bare) == "" and forge.pulls == []
    assert publication.operation(entity.id, "publish-op-1")["commit"] == commit
    out = publish(entity, planned)
    assert out["commit"] == commit and _branch_commit(bare) == commit
    assert len(calls) == 2 and len(forge.pulls) == 1


def test_push_landed_but_pr_failed_a_retry_creates_only_the_pr(repo_project, monkeypatch):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    forge.fail_pull_create = 1
    pushes = []
    real = gitwrite.git_push
    monkeypatch.setattr(gitwrite, "git_push", lambda *a, **k: pushes.append(1) or real(*a, **k))
    with pytest.raises(store.StoreError) as e:
        publish(entity, planned)
    assert e.value.status == 502 and _branch_commit(bare) == e.value.extra["commit"]
    out = publish(entity, planned)
    assert out["pull_request"]["number"] == 1 and len(forge.pulls) == 1
    assert len(pushes) == 1


def test_an_existing_pr_for_the_branch_is_adopted(repo_project):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    forge.pulls.append(
        {
            "number": 7,
            "state": "open",
            "html_url": "https://forge.test/acme/proj/pulls/7",
            "head": {"ref": "playbook/git-demo-v1.0.0", "repo": {"full_name": "acme/proj"}},
            "base": {"ref": "main"},
        }
    )
    out = publish(entity, planned)
    assert out["pull_request"] == {
        "number": 7,
        "url": "https://forge.test/acme/proj/pulls/7",
        "adopted": True,
    }
    assert not any(m == "POST" and p.endswith("/pulls") for m, p in forge.calls)


def test_a_commit_whose_record_was_lost_is_adopted_not_duplicated(repo_project, monkeypatch):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    real = repo_git.commit_bound

    def crash_after(*a, **k):
        real(*a, **k)
        raise RuntimeError("lost before the record")

    monkeypatch.setattr(repo_git, "commit_bound", crash_after)
    with pytest.raises(RuntimeError):
        publish(entity, planned)
    first = _git(folder, "rev-parse", "refs/heads/playbook/git-demo-v1.0.0").strip()
    monkeypatch.setattr(repo_git, "commit_bound", real)
    out = publish(entity, planned)
    assert out["commit"] == first
    assert _git(folder, "rev-list", "--count", f"{planned['base']}..{first}").strip() == "1"


def test_an_operation_id_names_one_request(repo_project):
    entity, folder, bare, _ = repo_project
    planned = plan(entity)
    publish(entity, planned)
    with pytest.raises(store.Conflict, match="another publication"):
        publication.publish_update(
            entity.id, {"digest": "0" * 64, "operation_id": "publish-op-1"}, key=KEY
        )


def test_a_forge_url_from_the_bundle_default_is_refused(env, local_transport):
    tmp, _ = env
    folder = tmp / "proj"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "main")
    entity = deploy(folder, forge_default=True, bind_url=False)
    with pytest.raises(store.Conflict, match="playbook's default"):
        plan(entity)


def test_the_remote_must_be_on_the_forge_connections_host():
    assert publication._remote_repository("https://forge.test/acme/proj.git", "forge.test") == (
        "acme",
        "proj",
    )
    assert publication._remote_repository("git@forge.test:acme/proj.git", "forge.test") == (
        "acme",
        "proj",
    )
    with pytest.raises(store.Conflict, match="not an address the forge"):
        publication._remote_repository("https://evil.test/acme/proj.git", "forge.test")
    with pytest.raises(store.Conflict, match="not an address the forge"):
        publication._remote_repository("https://forge.test/a/b/c.git", "forge.test")
    with pytest.raises(store.Conflict, match="one owner/repository"):
        publication._remote_repository("https://forge.test/proj.git", "forge.test")


def test_the_push_remote_host_binding_through_the_real_parser(repo_project, monkeypatch):
    """The fixture stands the bare path in for the forge; this restores the real binding."""
    entity, folder, bare, forge = repo_project
    monkeypatch.setattr(publication, "_remote_repository", REAL_REMOTE_REPOSITORY)
    with pytest.raises(store.Conflict, match="not on the forge"):
        plan(entity)  # the bare path names no host
    _git(folder, "remote", "set-url", "--push", "origin", "https://evil.test/acme/proj.git")
    with pytest.raises(store.Conflict, match="not an address the forge"):
        plan(entity)
    assert ("GET", "/api/v1/repos/acme/proj") in forge.calls  # asked, and the forge said no


def test_a_remote_off_the_web_host_is_accepted_only_as_the_forge_advertises_it():
    known = {"ssh://git@forge.test:2222/acme/proj.git", "git@ssh.forge.test:acme/proj.git"}

    def confirm(owner, name):
        return known if (owner, name) == ("acme", "proj") else set()

    parse = REAL_REMOTE_REPOSITORY
    assert parse("https://forge.test/acme/proj.git", "forge.test") == ("acme", "proj")
    assert parse("git@forge.test:acme/proj.git", "forge.test") == ("acme", "proj")
    for url in known:
        assert parse(url, "forge.test", confirm) == ("acme", "proj")
        with pytest.raises(store.Conflict, match="not an address the forge"):
            parse(url, "forge.test")  # never without the forge's word
    for url in (
        "ssh://git@forge.test:2222/acme/other.git",
        "https://evil.test/acme/proj.git",
        "https://forge.test/x/acme/proj.git",
    ):
        with pytest.raises(store.Conflict, match="not an address the forge"):
            parse(url, "forge.test", confirm)


def test_a_same_named_branch_on_a_fork_is_not_adopted_as_the_pr(repo_project):
    entity, folder, bare, forge = repo_project
    forge.pulls.append(
        {
            "number": 1,
            "state": "open",
            "html_url": "https://forge.test/someone/proj/pulls/1",
            "head": {"ref": "playbook/git-demo-v1.0.0", "repo": {"full_name": "someone/proj"}},
            "base": {"ref": "main"},
        }
    )
    out = publish(entity, plan(entity))
    assert out["pull_request"]["adopted"] is False and out["pull_request"]["number"] == 2
    assert forge.pulls[1]["head"]["repo"]["full_name"] == "acme/proj"


def test_a_forge_url_rebound_after_the_plan_never_receives_the_token(repo_project):
    entity, folder, bare, forge = repo_project
    planned = plan(entity)
    template_vars.bind_project(
        entity.id, [{"name": "forge_url", "kind": "text", "value": "https://other.test"}]
    )
    with pytest.raises(store.Conflict, match="different address") as e:
        publish(entity, planned)
    assert e.value.extra["variable"] == "forge_url"
    assert forge.calls == [] and forge.pulls == []


def test_progress_reads_while_a_publish_holds_the_lock_and_never_creates(repo_project, monkeypatch):
    entity, folder, bare, _ = repo_project
    root = store.local_root()
    with pytest.raises(store.NotFound):
        publication.operation(entity.id, "publish-op-1")
    assert not (root / publication.DIRECTORY).exists()  # a read created nothing
    out = publish(entity, plan(entity))
    monkeypatch.setattr(store, "LOCK_WAIT_S", 0.2)
    with publication._locked(entity.id):
        assert publication.operation(entity.id, "publish-op-1")["commit"] == out["commit"]


# --- seed and remote creation ------------------------------------------------------------------


@pytest.fixture
def seeded_project(env):
    tmp, forge = env
    folder = tmp / "fresh"
    folder.mkdir()
    entity = deploy(folder)
    (folder / "unrelated.txt").write_text("not the playbook's\n")
    return entity, folder, forge


def test_seed_initialises_and_commits_exactly_the_playbook_paths(seeded_project):
    entity, folder, _ = seeded_project
    out = repo_git.seed(entity.id, {})
    assert out["seeded"] is True
    tracked = _git(folder, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert sorted(tracked) == ["RULES.md", "docs/guide.md"]
    assert _git(folder, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert not (folder / ".git" / "hooks").exists()  # no template copied in
    again = repo_git.seed(entity.id, {})
    assert again["seeded"] is False and again["sha"] == out["sha"]


def test_seed_refuses_a_folder_with_other_history(seeded_project):
    entity, folder, _ = seeded_project
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "add", "unrelated.txt")
    _git(folder, "commit", "-q", "-m", "theirs")
    with pytest.raises(store.Conflict, match="other history"):
        repo_git.seed(entity.id, {})


@pytest.fixture
def resolvable(monkeypatch):
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)


def _remote_body(**kw):
    return {"owner": "acme", "name": "fresh", **kw}


def test_remote_creation_shows_the_target_creates_once_and_never_puts_a_token_in_the_url(
    seeded_project, resolvable, monkeypatch
):
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    pushed = []
    monkeypatch.setattr(publication, "_push", lambda *a: pushed.append(a) or {})
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    assert planned["repository"] == "acme/fresh" and planned["private"] is True
    assert planned["url"] == "git@forge.test:acme/fresh.git"
    assert ("POST", "/api/v1/orgs/acme/repos") not in forge.calls  # the plan only looks
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    out = publication.create_remote(entity.id, body, key=KEY)
    assert out["repository"]["created_by_operation"] is True
    assert _git(folder, "remote", "get-url", "origin").strip() == "git@forge.test:acme/fresh.git"
    assert TOKEN not in _git(folder, "config", "--list") and TOKEN not in json.dumps(out)
    assert len(pushed) == 1
    assert publication.create_remote(entity.id, body, key=KEY) == out
    creates = [c for c in forge.calls if c == ("POST", "/api/v1/orgs/acme/repos")]
    assert len(creates) == 1


def test_remote_creation_uses_the_forges_own_ssh_port_before_creating(
    seeded_project, resolvable, monkeypatch
):
    """Hermes 5944 / finding 3: a forge whose ssh runs on 2222 advertises `ssh://…:2222/`. The
    plan must show THAT address before anything is created, and the create must use it."""
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    forge.ssh_prefix = "ssh://git@forge.test:2222/"
    monkeypatch.setattr(publication, "_push", lambda *a: {})
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    assert planned["url"] == "ssh://git@forge.test:2222/acme/fresh.git"
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    out = publication.create_remote(entity.id, body, key=KEY)
    assert out["repository"]["url"] == planned["url"]
    assert _git(folder, "remote", "get-url", "origin").strip() == planned["url"]


def test_remote_creation_refuses_before_creating_when_the_ssh_address_is_unknown(
    seeded_project, resolvable
):
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    forge.visible = []
    with pytest.raises(store.StoreError, match="ssh address cannot be determined"):
        publication.plan_remote(entity.id, _remote_body(), key=KEY)
    with pytest.raises(store.StoreError, match="does not take transport"):
        publication.plan_remote(entity.id, _remote_body(transport="https"), key=KEY)
    assert not [c for c in forge.calls if c[0] == "POST"]


def test_a_forge_answer_unlike_the_plan_names_the_repository_it_left(
    seeded_project, resolvable, monkeypatch
):
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    monkeypatch.setattr(publication, "_push", lambda *a: {})
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    forge.created_ssh_url = "git@elsewhere.test:acme/fresh.git"
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    with pytest.raises(store.Conflict, match="left alone") as e:
        publication.create_remote(entity.id, body, key=KEY)
    assert e.value.extra["html_url"] == "https://forge.test/acme/fresh"
    assert "origin" not in _git(folder, "remote").split()


def test_remote_creation_pushes_through_the_real_push_path(
    seeded_project, local_transport, monkeypatch
):
    """MECHANICS: a bare repository stands in for the forge's git side, so the real `_push`
    (destination check, `ls-remote`, `git_push`) runs end to end."""
    entity, folder, forge = seeded_project
    tmp = folder.parent
    seeded = repo_git.seed(entity.id, {})
    bare = tmp / "acme" / "fresh.git"
    bare.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    forge.ssh_prefix = f"{tmp}/"
    # The stand-in is a path, which the ssh-shape check refuses by design.
    monkeypatch.setattr(publication, "_check_ssh_url", lambda *a: None)
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    assert planned["url"] == str(bare)
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    out = publication.create_remote(entity.id, body, key=KEY)
    assert out["pushed"] is True
    assert _branch_commit(bare, "main") == seeded["sha"]


def test_an_existing_repository_name_is_refused(seeded_project, resolvable):
    entity, _, forge = seeded_project
    repo_git.seed(entity.id, {})
    forge.repos["acme/fresh"] = {"full_name": "acme/fresh", "description": ""}
    with pytest.raises(store.Conflict, match="already exists"):
        publication.plan_remote(entity.id, _remote_body(), key=KEY)


def test_a_lost_create_response_is_adopted_only_by_the_operations_own_marker(
    seeded_project, resolvable, monkeypatch
):
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    monkeypatch.setattr(publication, "_push", lambda *a: {})
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    forge.lose_create_response = True
    with pytest.raises(store.StoreError):
        publication.create_remote(entity.id, body, key=KEY)
    out = publication.create_remote(entity.id, body, key=KEY)
    assert out["repository"]["created_by_operation"] is True
    assert len([c for c in forge.calls if c[0] == "POST"]) == 1


def test_a_name_collision_this_operation_did_not_create_is_refused(
    seeded_project, resolvable, monkeypatch
):
    entity, folder, forge = seeded_project
    repo_git.seed(entity.id, {})
    monkeypatch.setattr(publication, "_push", lambda *a: {})
    planned = publication.plan_remote(entity.id, _remote_body(), key=KEY)
    body = {**_remote_body(), "digest": planned["digest"], "operation_id": "remote-op-1"}
    forge.refuse_create = True  # the create request fails without creating anything
    with pytest.raises(store.StoreError):
        publication.create_remote(entity.id, body, key=KEY)
    # Somebody else takes the name before the retry: a matching name is not ownership.
    forge.repos["acme/fresh"] = {
        "full_name": "acme/fresh",
        "description": "theirs",
        "ssh_url": "git@forge.test:acme/fresh.git",
    }
    with pytest.raises(store.Conflict, match="did not create it"):
        publication.create_remote(entity.id, body, key=KEY)
    assert "origin" not in _git(folder, "remote").split()


def test_the_forge_writer_has_no_delete_path():
    names = {n for n, _ in inspect.getmembers(forge_write.ForgeWriter) if not n.startswith("__")}
    assert not {n for n in names if re.search(r"delete|remove|destroy|patch|put|merge", n, re.I)}
    w = forge_write.ForgeWriter(kind="forgejo", base_url="https://forge.test", token="t")
    for method in ("DELETE", "PATCH", "PUT"):
        with pytest.raises(forge_write.ForgeWriteError, match="not a request"):
            w._send(method, "/repos/acme/fresh")
    with pytest.raises(forge_write.ForgeWriteError, match="https"):
        forge_write.ForgeWriter(kind="forgejo", base_url="http://forge.test", token="t")


# --- clone ------------------------------------------------------------------------------------


@pytest.mark.parametrize("config_scope", ["global", "system", "xdg"])
def test_clone_does_not_rewrite_the_admitted_https_destination(
    env, resolvable, monkeypatch, config_scope
):
    """Real Git must hand its HTTPS helper the admitted URL, never a same-scheme rewrite.

    The helper records the effective destination and fails without opening a socket. The
    production protocol allowlist and admission pins remain in place throughout the clone.
    """
    tmp, _ = env
    requested = "https://forge.test/approved.git"
    redirected = "https://127.0.0.1/unapproved.git"
    config = {
        "global": tmp / ".gitconfig",
        "system": tmp / "system-gitconfig",
        "xdg": tmp / ".config" / "git" / "config",
    }[config_scope]
    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open("a") as f:
        f.write(f'[url "{redirected}"]\n\tinsteadOf = {requested}\n')
    helpers = tmp / "git-helpers"
    helpers.mkdir()
    contacted = tmp / "effective-url"
    helper = helpers / "git-remote-https"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(contacted)!r}).write_text(sys.argv[2])\n"
        "sys.exit(1)\n"
    )
    helper.chmod(0o700)
    real = gitwrite._run_argv

    def recording_transport(argv, child_env, cwd, timeout, **kw):
        # Replace only the transport, after Git has processed its real config and URL rewrites.
        if "clone" in argv:
            assert child_env["GIT_ALLOW_PROTOCOL"] == "https"
            assert "http.curloptResolve=forge.test:443:203.0.113.7" in argv
            child_env = {**child_env, "GIT_EXEC_PATH": str(helpers)}
            if config_scope == "system":
                child_env["GIT_CONFIG_SYSTEM"] = str(config)
        return real(argv, child_env, cwd, timeout, **kw)

    monkeypatch.setattr(gitwrite, "_run_argv", recording_transport)
    with pytest.raises(store.StoreError):
        repo_git.clone({"url": requested, "parent": str(tmp), "name": "cloned"})
    assert contacted.read_text() == requested
    assert not (tmp / "cloned").exists() and _staged(tmp) == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc",
        "/srv/somewhere.git",
        "ssh://localhost/home/x.git",
        "https://127.0.0.1/x.git",
        "https://user:pw@forge.test/x.git",
        "https://forge.test/x.git?token=abc",
    ],
)
def test_clone_refuses_local_remotes_and_credentials_with_the_shipped_allowlist(env, url):
    tmp, _ = env
    with pytest.raises(store.StoreError) as e:
        repo_git.clone({"url": url, "parent": str(tmp), "name": "cloned"})
    assert e.value.status in (403, 409)
    assert not any((tmp / "cloned").iterdir()) if (tmp / "cloned").exists() else True


def test_clone_refuses_a_non_empty_destination(env, resolvable):
    tmp, _ = env
    (tmp / "busy").mkdir()
    (tmp / "busy" / "keep.txt").write_text("mine\n")
    with pytest.raises(store.Conflict, match="not empty"):
        repo_git.clone({"url": "https://forge.test/x.git", "parent": str(tmp), "name": "busy"})
    assert (tmp / "busy" / "keep.txt").read_text() == "mine\n"


def test_clone_refuses_a_destination_outside_the_project_roots(env, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(prefs, "get_project_roots", lambda: [str(tmp_path / "roots")])
    (tmp_path / "roots").mkdir()
    with pytest.raises(store.StoreError) as e:
        repo_git.clone({"url": "https://forge.test/x.git", "parent": str(elsewhere), "name": "c"})
    assert e.value.status == 403 and not (elsewhere / "c").exists()


def test_clone_never_fetches_a_submodule_url(env, local_transport):
    """MECHANICS: the clone works, and `.gitmodules` pointing at a local repo is not followed."""
    tmp, _ = env
    inner = tmp / "inner"
    _git(tmp, "init", "-q", "-b", "main", str(inner))
    (inner / "secret.txt").write_text("outside\n")
    _git(inner, "add", "secret.txt")
    _git(inner, "commit", "-q", "-m", "inner")
    outer = tmp / "outer"
    _git(tmp, "init", "-q", "-b", "main", str(outer))
    (outer / "a.txt").write_text("a\n")
    _git(outer, "add", "a.txt")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    _git(outer, "commit", "-q", "-m", "outer")
    out = repo_git.clone({"url": str(outer), "parent": str(tmp), "name": "cloned"})
    cloned = tmp / "cloned"
    assert out["path"] == str(cloned) and out["branch"] == "main"
    assert (cloned / "a.txt").read_text() == "a\n"
    assert not (cloned / "sub" / "secret.txt").exists()
    assert not (cloned / ".git" / "modules").exists()
    assert not (cloned / ".git" / "hooks").exists()


def _filtered_source(tmp, files: dict[str, str]):
    """A source repository committed with every filter off, so setup itself runs no driver."""
    src = tmp / "src"
    _git(tmp, "init", "-q", "-b", "main", str(src))
    for rel, text in files.items():
        (src / rel).write_text(text)
    off = []
    for drv in ("lfs", "probe", "probeproc"):
        off += ["-c", f"filter.{drv}.clean=", "-c", f"filter.{drv}.process="]
        off += ["-c", f"filter.{drv}.required=false"]
    _git(src, *off, "add", "-A")
    _git(src, *off, "commit", "-q", "-m", "content")
    return src


def test_clone_never_runs_a_configured_smudge_driver_the_repository_selects(env, local_transport):
    """Repository content picks the driver; the clone's checkout must run none of them."""
    tmp, _ = env
    src = _filtered_source(
        tmp,
        {
            ".gitattributes": "a.txt filter=probe\nb.txt filter=probeproc\n",
            "a.txt": "raw a\n",
            "b.txt": "raw b\n",
        },
    )
    marker = tmp / "driver-ran"
    with (tmp / ".gitconfig").open("a") as f:
        f.write(
            f'[filter "probe"]\n\tsmudge = "echo hit >> {marker}; cat"\n\trequired = true\n'
            f'[filter "probeproc"]\n\tprocess = "echo hit >> {marker}"\n\trequired = true\n'
        )
    out = repo_git.clone({"url": str(src), "parent": str(tmp), "name": "cloned"})
    assert not marker.exists()
    cloned = tmp / "cloned"
    assert out["branch"] == "main" and out["head"]
    assert (cloned / "a.txt").read_text() == "raw a\n"
    assert (cloned / "b.txt").read_text() == "raw b\n"


@pytest.mark.skipif(not shutil.which("git-lfs"), reason="git-lfs is not installed")
def test_clone_never_lets_an_lfsconfig_reach_the_network(env, local_transport):
    """The reproduced finding: `* filter=lfs` + a pointer + `.lfsconfig` made git-lfs POST to a
    loopback listener during checkout. The listener must receive nothing."""
    tmp, _ = env
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824\n"
        "size 5\n"
    )
    src = _filtered_source(
        tmp,
        {
            ".gitattributes": "* filter=lfs diff=lfs merge=lfs -text\n",
            ".lfsconfig": f"[lfs]\n\turl = http://127.0.0.1:{port}/x\n",
            "big.bin": pointer,
        },
    )
    with (tmp / ".gitconfig").open("a") as f:
        f.write(
            '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
            "\tprocess = git-lfs filter-process\n\trequired = true\n"
        )
    got: list[bytes] = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1)
                with contextlib.suppress(OSError):
                    got.append(conn.recv(200))

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        out = repo_git.clone({"url": str(src), "parent": str(tmp), "name": "cloned"})
    finally:
        stop.set()
        t.join(5)
        listener.close()
    assert got == []
    assert out["head"] and (tmp / "cloned" / "big.bin").read_text() == pointer


def test_a_driver_declared_after_a_huge_config_value_still_never_runs(env, local_transport):
    """Hermes 5947: past the 1 MiB stdout cap `git config --list` was silently a prefix, so a
    driver declared after a big value was never voided and the repository could select it."""
    tmp, _ = env
    src = _filtered_source(tmp, {".gitattributes": "a.txt filter=probe\n", "a.txt": "raw a\n"})
    marker = tmp / "driver-ran"
    with (tmp / ".gitconfig").open("a") as f:
        f.write(f"[padding]\n\tbig = {'x' * (1100 * 1024)}\n")
        f.write(f'[filter "probe"]\n\tsmudge = "echo hit >> {marker}; cat"\n\trequired = true\n')
    out = repo_git.clone({"url": str(src), "parent": str(tmp), "name": "cloned"})
    assert not marker.exists()
    assert out["head"] and (tmp / "cloned" / "a.txt").read_text() == "raw a\n"


def _staged(tmp):
    return sorted(p.name for p in tmp.iterdir() if p.name.startswith(".battlelab-clone-"))


def _during_checkout(monkeypatch, then):
    """Run `then()` right after the clone's checkout step, whatever the step does."""
    real = repo_git._run_unbound

    def hooked(args, cwd, **kw):
        out = real(args, cwd, **kw) if "checkout" not in args or then.passthrough else None
        if "checkout" in args:
            then()
        return out

    monkeypatch.setattr(repo_git, "_run_unbound", hooked)


def test_a_failed_clone_never_deletes_what_another_writer_put_in_the_destination(
    env, local_transport, monkeypatch
):
    """Hermes 5947: undoing a failed clone emptied a pre-existing folder, other writers' files
    included. The clone now writes only into its own staging folder and removes only that."""
    tmp, _ = env
    src = _filtered_source(tmp, {"a.txt": "a\n"})
    (tmp / "mine").mkdir()

    def theirs_then_fail():
        (tmp / "mine" / "theirs.txt").write_text("another writer\n")
        raise gitpanel.GitError("checkout failed", status=409)

    theirs_then_fail.passthrough = False
    _during_checkout(monkeypatch, theirs_then_fail)
    with pytest.raises(store.StoreError, match="checkout failed"):
        repo_git.clone({"url": str(src), "parent": str(tmp), "name": "mine"})
    assert [p.name for p in (tmp / "mine").iterdir()] == ["theirs.txt"]
    assert (tmp / "mine" / "theirs.txt").read_text() == "another writer\n"
    assert _staged(tmp) == []
    with pytest.raises(store.StoreError):
        repo_git.clone({"url": str(src), "parent": str(tmp), "name": "absent"})
    assert not (tmp / "absent").exists() and _staged(tmp) == []


def test_a_file_appearing_in_the_destination_refuses_a_complete_clone(
    env, local_transport, monkeypatch
):
    tmp, _ = env
    src = _filtered_source(tmp, {"a.txt": "a\n"})
    (tmp / "mine").mkdir()

    def theirs():
        (tmp / "mine" / "theirs.txt").write_text("another writer\n")

    theirs.passthrough = True
    real = repo_git._run_unbound
    _during_checkout(monkeypatch, theirs)
    with pytest.raises(store.Conflict, match="left untouched"):
        repo_git.clone({"url": str(src), "parent": str(tmp), "name": "mine"})
    assert [p.name for p in (tmp / "mine").iterdir()] == ["theirs.txt"]  # no partial clone
    assert _staged(tmp) == []
    monkeypatch.setattr(repo_git, "_run_unbound", real)
    (tmp / "mine" / "theirs.txt").unlink()
    out = repo_git.clone({"url": str(src), "parent": str(tmp), "name": "mine"})  # retry works
    assert (tmp / "mine" / "a.txt").read_text() == "a\n" and out["branch"] == "main"
    assert oct((tmp / "mine").stat().st_mode & 0o777) == "0o755" and _staged(tmp) == []


# --- routes -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    ["/git/seed", "/git/remote/plan", "/git/remote", "/git/publish/plan", "/git/publish"],
)
def test_every_git_write_needs_a_session_csrf_and_the_origin(seeded_project, auth_cfg, tail):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app
    from agent_sessions.routes import playbooks as routes

    entity, folder, _ = seeded_project
    c = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    url = routes.DEPLOY.replace("{project}", entity.id) + tail
    assert c.post(url, json={}).status_code == 401
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    assert c.post(url, json={}).status_code == 403
    assert c.post(url, json={}, headers={**hdr, "Origin": "https://other.example"}).status_code == (
        403
    )
    assert c.post(routes.PREFIX + "/clone", json={}).status_code == 403
    if tail == "/git/seed":
        r = c.post(url, json={}, headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        assert (folder / ".git").is_dir()
        got = c.get(routes.DEPLOY.replace("{project}", entity.id) + "/git/nope-nope-1")
        assert got.status_code == 404
    else:
        r = c.post(url, json={"bogus": 1}, headers=hdr)
        assert r.status_code == 422 and r.headers["cache-control"] == "no-store"


def test_the_applied_path_record_holds_digests_never_content(seeded_project):
    entity, folder, _ = seeded_project
    with apply.state.locked(entity.id) as locked:
        record = locked.read()
    paths = record["applied_paths"]["paths"]
    assert set(paths) == {"RULES.md", "docs/guide.md"}
    assert paths["docs/guide.md"]["kind"] == "file"
    assert "A guide" not in json.dumps(record["applied_paths"])
    assert template_vars.resolver(entity.id).secret("token") == TOKEN
