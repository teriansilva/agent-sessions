"""Write-side git tests (#806).

The valuable ones here are adversarial, and each was written against a **measured** behaviour
rather than an assumption — three of the obvious hardening flags do not do what they look like they
do, and the tests below are what keep the working ones in place:

* a repo-configured ``core.hooksPath`` hook runs on a plain ``switch``;
* a repo whose own config sets ``protocol.ext.allow=always`` makes ``fetch`` execute an
  ``ext::<cmd>`` remote — and ``-c protocol.allow=never`` does **not** stop it;
* a repo-configured ``remote.<n>.uploadpack`` executes locally — and ``-c`` does **not** override
  it, only ``--upload-pack`` on the command line does;
* ``--`` does **not** disable pathspec magic: ``git restore -- ':(glob)*.txt'`` hit both files.

Everything runs against throwaway repos under ``AGENT_SESSIONS_FS_ROOT``; the real ``~`` is never
touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from agent_sessions import files, gitpanel, gitwrite, ptybridge
from agent_sessions.files import FsError


def _fps(repo, paths):
    """The fingerprints the panel would have shown for these rows — what a real client echoes."""
    st = gitwrite.git_status(str(repo))
    want = set(paths)
    return {e["path"]: e["fp"] for e in st["entries"] if e["path"] in want}


def _staged_fp(repo):
    return gitwrite.git_status(str(repo))["staged_fp"]


def _dirty_fp(repo):
    return gitwrite.git_status(str(repo))["dirty_fp"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _reset():
    files._inflight_total = 0
    files._inflight_by_root.clear()
    gitpanel.reset_flights_for_test()
    gitpanel.reset_git_bin_for_test()
    yield
    gitpanel.reset_flights_for_test()
    files._inflight_total = 0
    files._inflight_by_root.clear()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "home"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(r))
    files.reset_capabilities_for_test()
    yield r
    files.reset_capabilities_for_test()


@pytest.fixture()
def repo(root):
    p = root / "proj"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "a.txt").write_text("one\n")
    (p / "b.txt").write_text("one\n")
    _git(p, "add", "a.txt", "b.txt")
    _git(p, "commit", "-qm", "init")
    _git(p, "branch", "other")
    return p


@pytest.fixture()
def local_transport(monkeypatch):
    """Add `file` back to the protocol allowlist FOR THIS TEST ONLY.

    The shipped allowlist is network-only (pinned by `test_the_protocol_allowlist_is_network_only`)
    and that is the containment guarantee — but it also means the only kind of remote a hermetic
    test can build, a bare repo on disk, is unreachable. Two different things need proving and
    neither can stand in for the other:

    * that a local remote is REFUSED — tested against the real shipped value, never this fixture;
    * that the fetch/push MECHANICS are right (upstream set on a first push, `--upload-pack` beats
      the repo's own value, the refspec is the one this module built) — impossible to exercise
      without a reachable remote, so `file` goes back for the duration.

    Any test taking this fixture is therefore testing mechanics, not containment. It is a fixture
    rather than an inline monkeypatch so that reading the argument list tells you which is which.
    """
    monkeypatch.setattr(gitwrite, "GIT_ALLOW_PROTOCOL", gitwrite.GIT_ALLOW_PROTOCOL + ":file")


#: A syntactically valid expectation for tests whose refusal happens before the digest is
#: consulted — an ambiguous target, a detached HEAD, a remote with several push URLs. Those cannot
#: obtain a real token because the preflight itself refuses, which is the point of each of them.
#: Well-FORMED but wrong — the point is to reach the drift refusal (409), not the shape
#: rejection (422). It carries a source oid too, since the token now pins what would be sent
#: as well as where it would go.
PLACEHOLDER_EXPECT = "origin/master@0123456789abcdef:" + "0" * 40


def _expect(repo, remote=None) -> str:
    """The expectation token the panel would send, obtained the way the panel obtains it.

    Hard-coding `origin/master` here would defeat the point of the binding: the token pins the
    destination the preflight RESOLVED, so a test that writes the label by hand is asserting
    against a value the server never issued.
    """
    out = gitwrite.push_target(str(repo), remote)
    assert out["ok"], f"preflight refused: {out['reason']}"
    return out["expect"]


def _branches(repo) -> set[str]:
    """The repository's local branches, read straight off disk rather than through the module
    under test -- a test that asks the code whether the code worked proves nothing."""
    out = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _sentinel(root, name):
    """A script that records that it ran. If a hardened command executes it, we have lost."""
    script = root / f"{name}.sh"
    hit = root / f"{name}.ran"
    script.write_text(f'#!/bin/sh\ntouch "{hit}"\nexit 1\n')
    script.chmod(0o755)
    return script, hit


# --------------------------------------------------------------------------- execution vectors


def test_repo_configured_hook_does_not_run_on_switch(repo, root):
    """MEASURED: a `core.hooksPath` hook runs on a plain `git switch`.

    An empty `core.hooksPath` stops it — also measured.
    """
    hooks = root / "evilhooks"
    hooks.mkdir()
    hit = root / "hook.ran"
    hook = hooks / "post-checkout"
    hook.write_text(f'#!/bin/sh\ntouch "{hit}"\n')
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))

    # Baseline: prove the vector is real in THIS environment, so a passing test below means the
    # mitigation worked rather than that the vector never existed.
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-q", "other"], check=True, capture_output=True
    )
    assert hit.exists(), "vector not reproduced — the sentinel below would prove nothing"
    hit.unlink()
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-q", "master"], check=False, capture_output=True
    )
    hit.unlink(missing_ok=True)

    gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    assert not hit.exists(), "a repository-configured hook executed during a panel switch"


def test_ext_remote_does_not_execute_even_when_the_repo_lifts_the_protocol_policy(repo, root):
    """MEASURED: repo-local `protocol.ext.allow=always` makes a plain fetch run `ext::<cmd>`.

    Also measured: `-c protocol.allow=never` does NOT stop it (the per-protocol key wins), which is
    why the mitigation is the `GIT_ALLOW_PROTOCOL` allowlist instead.
    """
    script, hit = _sentinel(root, "ext")
    _git(repo, "remote", "add", "evil", f"ext::{script}")
    _git(repo, "config", "protocol.ext.allow", "always")

    baseline = subprocess.run(
        ["git", "-C", str(repo), "fetch", "evil"], capture_output=True, check=False
    )
    assert hit.exists(), f"vector not reproduced ({baseline.stderr!r})"
    hit.unlink()

    with pytest.raises((gitpanel.GitError, FsError)):
        gitwrite.git_fetch(str(repo), "evil")
    assert not hit.exists(), "an ext:: remote helper executed from repository config"


def test_repo_configured_uploadpack_does_not_execute(repo, root, local_transport):
    """MEASURED: `remote.<n>.uploadpack` runs locally on fetch, and `-c` does NOT override it.

    Takes `local_transport` deliberately: this remote is a path on disk, so with the shipped
    allowlist the fetch would be refused by protocol before `--upload-pack` ever mattered and the
    assertion below would hold for the wrong reason — a green test proving nothing.
    """
    other = root / "otherrepo"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    (other / "x").write_text("x")
    _git(other, "add", "x")
    _git(other, "commit", "-qm", "o")

    script, hit = _sentinel(root, "up")
    _git(repo, "remote", "add", "loc", str(other))
    _git(repo, "config", "remote.loc.uploadpack", str(script))

    subprocess.run(["git", "-C", str(repo), "fetch", "loc"], capture_output=True, check=False)
    assert hit.exists(), "vector not reproduced"
    hit.unlink()

    gitwrite.git_fetch(str(repo), "loc")
    assert not hit.exists(), "a repository-configured uploadpack executed"


# --------------------------------------------------------------------------- input boundary


def test_pathspec_magic_is_refused_not_expanded(repo):
    """MEASURED: `git restore -- ':(glob)*.txt'` restored BOTH files. It must never reach git."""
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("changed\n")
    with pytest.raises(FsError) as e:
        gitwrite.validate_paths(gitwrite.resolve_repo(str(repo)), [":(glob)*.txt"])
    assert e.value.status == 409  # not in the fresh status set, so unreachable


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside", "a/../../x", "-rf", "", "a\x00b", "./a.txt"],
)
def test_paths_that_are_not_plain_relative_names_are_refused(repo, bad):
    r = gitwrite.resolve_repo(str(repo))
    with pytest.raises(FsError):
        gitwrite.validate_paths(r, [bad])


def test_a_path_the_server_does_not_currently_report_is_refused(repo):
    """The bound on a destructive op is a FRESH status read, not the client's word."""
    r = gitwrite.resolve_repo(str(repo))
    with pytest.raises(FsError) as e:
        gitwrite.validate_paths(r, ["a.txt"])  # unmodified: not a reported change
    assert e.value.status == 409
    (repo / "a.txt").write_text("changed\n")
    gitpanel.reset_flights_for_test()
    assert gitwrite.validate_paths(r, ["a.txt"]) == ["a.txt"]


def test_too_many_paths_is_a_refusal(repo):
    (repo / "a.txt").write_text("changed\n")
    r = gitwrite.resolve_repo(str(repo))
    with pytest.raises(FsError):
        gitwrite.validate_paths(r, ["a.txt"] * (gitwrite.MAX_PATHS + 1))


@pytest.mark.parametrize("bad", ["-x", "a..b", "a b", "with\nnewline", "", "x.lock", "@{-1}"])
def test_ref_names_that_are_not_names_are_refused(bad):
    with pytest.raises(FsError):
        gitwrite.validate_ref(bad)


# --------------------------------------------------------------------------- refusals


def test_switch_refuses_a_dirty_tree(repo):
    """MEASURED: `git switch` silently carries uncommitted work onto the other branch."""
    (repo / "a.txt").write_text("uncommitted\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    assert e.value.status == 409
    assert "uncommitted" in str(e.value)
    # And it really did not move.
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "master"


def test_switch_moves_a_clean_tree(repo):
    out = gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    assert out["branch"] == "other"
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "other"


def test_deleting_the_current_branch_is_refused(repo):
    with pytest.raises(FsError) as e:
        gitwrite.git_branch_delete(str(repo), "master")
    assert e.value.status == 409


def test_an_unmerged_branch_is_refused_never_force_deleted(repo):
    """`-D` is not reachable from this module — an unmerged branch is a refusal, and it survives."""
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "c.txt").write_text("new\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "unmerged work")
    _git(repo, "switch", "-q", "master")

    with pytest.raises(gitpanel.GitError) as e:
        gitwrite.git_branch_delete(str(repo), "feature")
    assert "not fully merged" in str(e.value)
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "feature" in branches, "an unmerged branch was destroyed"


def test_pull_without_an_upstream_is_a_named_refusal(repo):
    with pytest.raises(FsError) as e:
        gitwrite.git_pull(str(repo))
    assert e.value.status == 409
    assert "upstream" in str(e.value)


def test_fetch_on_a_repo_with_no_remotes_is_a_named_refusal(repo):
    with pytest.raises(FsError) as e:
        gitwrite.git_fetch(str(repo))
    assert e.value.status == 409


def test_a_path_outside_the_root_is_refused(root):
    with pytest.raises(FsError):
        gitwrite.resolve_repo("/etc")


def test_not_a_repository_is_a_named_404(root):
    plain = root / "plain"
    plain.mkdir()
    with pytest.raises(FsError) as e:
        gitwrite.resolve_repo(str(plain))
    assert e.value.status == 404


# --------------------------------------------------------------------------- hygiene


def test_credentials_are_redacted_out_of_git_output():
    assert gitwrite.redact("fatal: https://user:tok@example.com/x.git not found") == (
        "fatal: https://<redacted>@example.com/x.git not found"
    )
    assert "tok" not in gitwrite.redact("remote: https://u:tok@h/x")


def test_hooks_void_is_an_empty_directory():
    void = gitwrite.hooks_void()
    assert os.path.isdir(void)
    assert os.listdir(void) == []


def test_hooks_void_is_not_in_shared_temp():
    """RED against the pre-#1006 tree, which used `tempfile.mkdtemp()` — i.e. shared `/tmp`.

    A name in a world-writable directory is not ours: delete it and any local account may recreate
    it holding executable hooks, which a peer then hands to git as `core.hooksPath`. That is the
    hazard #993's prune category was cut for, and it is removed by where the directory now lives.
    """
    void = Path(gitwrite.hooks_void())
    shared = {Path(p).resolve() for p in ("/tmp", "/var/tmp", "/dev/shm", tempfile.gettempdir())}
    assert void.parent.resolve() not in shared, f"{void} sits directly in shared temp"
    assert void.parent.resolve() == Path(os.environ["AGENT_SESSIONS_RUNTIME_DIR"]).resolve()


def test_hooks_void_is_one_stable_directory_across_calls_and_a_restart(monkeypatch):
    """RED against the pre-#1006 tree: one `mkdtemp` per PROCESS, so every restart left another
    directory behind — which is how 34,282 of them accumulated on this host.

    `raising=False` because the pre-fix tree has a `_hooks_void_dir` module global and this tree
    deliberately has none; clearing whatever memo exists is what makes this a restart rather than
    a second call.
    """
    first = gitwrite.hooks_void()
    assert gitwrite.hooks_void() == first, "two calls in one process disagreed"
    inode = os.stat(first).st_ino

    monkeypatch.setattr(gitwrite, "_hooks_void_dir", None, raising=False)
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)

    again = gitwrite.hooks_void()
    assert again == first, "a restart produced a second directory instead of reusing the one"
    assert os.stat(again).st_ino == inode, "the directory was recreated rather than reused"


def test_hooks_void_survives_concurrent_first_use():
    """Creation is idempotent, so the loser of the race verifies the winner's directory."""
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def call():
        try:
            barrier.wait(timeout=10)
            results.append(gitwrite.hooks_void())
        except Exception as exc:  # noqa: BLE001 — reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"concurrent first use raised: {errors}"
    assert len(results) == 8
    assert len(set(results)) == 1, f"concurrent first use produced {len(set(results))} directories"


def test_hooks_void_refuses_instead_of_falling_back_when_the_subtree_is_not_private():
    """Fail closed. The refusal is the whole point: a permissive failure branch here would put
    the directory straight back into shared temp, which is the defect being removed."""
    runtime = Path(os.environ["AGENT_SESSIONS_RUNTIME_DIR"])
    runtime.chmod(0o777)
    try:
        with pytest.raises(gitpanel.GitError, match="other-writable"):
            gitwrite.hooks_void()
    finally:
        runtime.chmod(0o700)


def test_hooks_void_refuses_a_symlinked_runtime_dir(tmp_path, monkeypatch):
    """`lstat`, not `stat`: resolving first would answer about the target and say nothing about
    the link, which is the component an attacker controls."""
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked-runtime"
    link.symlink_to(real)
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(link))
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)
    with pytest.raises(gitpanel.GitError, match="not a real directory"):
        gitwrite.hooks_void()


def test_hooks_void_reports_unexpected_contents_and_never_deletes_them():
    """Refuse, never "repair". Deleting the entry would destroy the evidence an operator needs
    and leave whatever produced it in place."""
    void = Path(gitwrite.hooks_void())
    void.chmod(0o700)  # 0500 refuses even our own write, which is the point of 0500
    planted = void / "post-checkout"
    planted.write_text("#!/bin/sh\necho pwned\n")
    void.chmod(0o500)

    with pytest.raises(gitpanel.GitError, match="not empty"):
        gitwrite.hooks_void()
    assert planted.exists(), "the refusal deleted the evidence instead of reporting it"


def test_a_hook_does_not_run_on_the_network_path(repo, root, tmp_path, monkeypatch):
    """The SECOND caller — `gitwrite.py`'s isolated network command, not just the write path.

    MEASURED on git 2.43.0, and the baseline below re-measures it on every run: the scratch gitdir
    a network command runs against carries none of the repository's own config, but `$HOME`'s
    global config still applies — and a global `core.hooksPath` runs a `reference-transaction`
    hook during a plain `fetch`, because a fetch updates refs. `-c core.hooksPath=<void>` stops it.

    The remote is a local path, reached through `run_git_net`'s own `allow_protocol` parameter.
    The production allowlist is `https:ssh` precisely so a repository cannot name a path on this
    machine, which is also why a faithful end-to-end fetch cannot otherwise be exercised offline;
    using the code's own seam keeps the argv under test the real one.
    """
    fake_home = tmp_path / "githome"
    fake_home.mkdir()
    hooks = tmp_path / "evilhooks"
    hooks.mkdir()
    hit = tmp_path / "net-hook.ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f'#!/bin/sh\ntouch "{hit}"\n')
    hook.chmod(0o755)
    (fake_home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("HOME", str(fake_home))

    source = root / "remote-src"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "f.txt").write_text("x\n")
    _git(source, "add", "f.txt")
    _git(source, "commit", "-qm", "one")

    rp = gitwrite.resolve_repo(str(repo))
    fetch = ["fetch", "--no-tags", "--", str(source), "+refs/heads/*:refs/remotes/probe/*"]

    # Baseline: prove the vector is real in THIS environment, so a clean sentinel below means the
    # mitigation worked rather than that nothing would have fired anyway.
    with gitwrite._isolated_gitdir(rp) as gitdir:
        subprocess.run(
            ["git", f"--git-dir={gitdir}", "-c", "protocol.file.allow=always", *fetch],
            check=True,
            capture_output=True,
            env={
                "HOME": str(fake_home),
                "PATH": os.environ["PATH"],
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_OBJECT_DIRECTORY": os.path.join(rp.common(), "objects"),
            },
        )
    assert hit.exists(), "vector not reproduced — the sentinel below would prove nothing"
    hit.unlink()

    with gitwrite._isolated_gitdir(rp) as gitdir:
        gitwrite.run_git_net(
            rp,
            gitdir,
            fetch,
            # `extra_config` is spliced into the argv RAW, so it carries its own `-c` — the same
            # shape `_fetch_isolated` passes its TLS pins in.
            extra_config=["-c", "protocol.file.allow=always"],
            allow_protocol="file",
        )
    assert not hit.exists(), "a repository-reachable hook executed during a panel network command"


def test_every_write_invocation_is_a_literal_argv_list(repo, monkeypatch):
    """The shell-free guarantee is load-bearing; `pr-validate` greps for it and so does this."""
    seen = {}

    class _P:
        returncode = 0
        stdout = None
        stderr = None

        def __init__(self, argv, **kw):
            seen["argv"] = argv
            seen["shell"] = kw.get("shell", False)
            raise RuntimeError("stop here — the argv is what this test is about")

    monkeypatch.setattr(gitwrite.subprocess, "Popen", _P)
    with pytest.raises(RuntimeError):
        gitwrite.run_git_write(gitwrite.resolve_repo(str(repo)), ["status"])
    assert isinstance(seen["argv"], list)
    assert all(isinstance(a, str) for a in seen["argv"])
    assert seen["shell"] is False
    assert "--literal-pathspecs" in seen["argv"]
    assert any(a.startswith("core.hooksPath=") for a in seen["argv"])


def test_a_write_invalidates_the_cached_status(repo):
    """A finished write is exactly the moment the cached read is known to be wrong."""
    before = gitpanel.git_status(str(repo))
    assert before["branch"] == "master"
    gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    after = gitpanel.git_status(str(repo))
    assert after["branch"] == "other", "the panel would have shown a pre-write branch"


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from fastapi.testclient import TestClient

    from agent_sessions import main

    return TestClient(main.create_app())


def _hdr(c, cfg):
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_a_write_route_without_csrf_is_refused(client, repo, auth_cfg):
    """These are the first state-changing routes in this surface; a write reachable without CSRF
    would be reachable from any page the operator happens to open."""
    r = client.post(
        "/api/git/switch", json={"path": str(repo), "branch": "other", "expect": _dirty_fp(repo)}
    )
    assert r.status_code == 403
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "master", "the refused request still moved the branch"


def test_no_git_write_is_reachable_by_GET(client, repo):
    """A GET that mutates is a GET an ``<img>`` tag can fire.

    The assertion is about the *effect*, not the status: an unmatched GET here falls through to the
    SPA catch-all and answers 404 rather than 405, which is fine — what must never happen is a
    branch moving. Asserting 405 would have been asserting the router's shape instead of the
    security property.
    """
    for route in ("/api/git/switch", "/api/git/fetch", "/api/git/pull", "/api/git/branch/delete"):
        r = client.get(route, params={"path": str(repo), "branch": "other"})
        assert r.status_code in (404, 405), f"{route} answered a GET with {r.status_code}"
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "master", "a GET mutated the repository"


def test_switch_route_moves_the_branch_and_says_so(client, repo, auth_cfg):
    r = client.post(
        "/api/git/switch",
        json={"path": str(repo), "branch": "other", "expect": _dirty_fp(repo)},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "other"
    assert r.headers["cache-control"] == "no-store"


def test_a_refusal_reaches_the_client_as_the_reason_it_is(client, repo, auth_cfg):
    """409 for a dirty tree, not a generic 500 — the distinction IS the feature."""
    (repo / "a.txt").write_text("uncommitted\n")
    r = client.post(
        "/api/git/switch",
        json={"path": str(repo), "branch": "other", "expect": _dirty_fp(repo)},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 409, r.text
    assert "uncommitted" in r.json()["detail"]


def test_branches_route_lists_local_and_remote(client, repo):
    r = client.get("/api/git/branches", params={"path": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"] == "master"
    assert sorted(body["local"]) == ["master", "other"]
    assert body["remote"] == []


def test_branches_route_reports_not_a_repo_without_failing(client, root):
    plain = root / "plain"
    plain.mkdir()
    r = client.get("/api/git/branches", params={"path": str(plain)})
    assert r.status_code == 200
    assert r.json()["repo"] is None


# ------------------------------------------------------- phase 2: the working tree


def test_staging_runs_the_repos_clean_filter_and_that_is_the_stated_contract(repo, root):
    """MEASURED (git 2.43.0): ``git add -- a.txt`` executes a repo-configured ``filter.*.clean``.

    This test asserts the vector **fires**, which is unusual and deliberate. It is an accepted
    residual — converting worktree bytes into an index entry *is* the requested operation, and it
    is exactly what the agent's own ``git add`` does in that worktree. Pinning it means the day
    someone believes the write path is helper-proof, this test says otherwise in writing.
    """
    script, hit = _sentinel(root, "clean")
    # `exit 1` in the sentinel would fail the add; this one has to succeed AND pass content on.
    script.write_text(f'#!/bin/sh\ntouch "{hit}"\ncat\n')
    script.chmod(0o755)
    _git(repo, "config", "filter.evil.clean", str(script))
    (repo / ".gitattributes").write_text("a.txt filter=evil\n")
    (repo / "a.txt").write_text("two\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    assert (
        hit.exists()
    ), "the documented residual stopped being true — update the contract, not this test"


def test_discard_cannot_be_widened_by_pathspec_magic(repo):
    """MEASURED: ``git restore --worktree -- ':(glob)*.txt'`` restored BOTH files.

    ``--`` did not stop it, so ``--literal-pathspecs`` plus an intersection against a freshly
    re-read status does: the magic string is not a name the server just reported, so it never
    reaches git at all — and b.txt keeps its edit.
    """
    (repo / "a.txt").write_text("edited\n")
    (repo / "b.txt").write_text("edited\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), [":(glob)*.txt"], _fps(repo, [":(glob)*.txt"]))
    assert e.value.status == 409
    assert (repo / "a.txt").read_text() == "edited\n"
    assert (repo / "b.txt").read_text() == "edited\n", "pathspec magic widened a destructive op"


@pytest.mark.parametrize(
    "magic", [":(top)a.txt", ":!b.txt", ":/a.txt", "*.txt", ":(glob)**/*", "-a.txt"]
)
def test_discard_refuses_every_pathspec_magic_shape(repo, magic):
    """Each of these is a *name* to this API, and none of them is a name git just reported."""
    (repo / "a.txt").write_text("edited\n")
    with pytest.raises(FsError):
        gitwrite.git_discard(str(repo), [magic], _fps(repo, [magic]))
    assert (repo / "a.txt").read_text() == "edited\n"


@pytest.mark.parametrize("name", ["with space.txt", "quo'te.txt", 'dq"ote.txt', "ünïcodé.txt"])
def test_odd_but_legitimate_names_still_work(repo, name):
    """The defence must not be "refuse anything unusual" — these are real filenames."""
    (repo / name).write_text("hello\n")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-qm", "add odd name")
    (repo / name).write_text("edited\n")
    gitwrite.git_discard(str(repo), [name], _fps(repo, [name]))
    assert (repo / name).read_text() == "hello\n"


def test_discard_refuses_an_untracked_file_rather_than_deleting_it(repo):
    """``restore`` recovers from the index or HEAD; an untracked file has neither, so a
    "discard" there is an unrecoverable delete wearing the same button."""
    (repo / "new.txt").write_text("not in git\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["new.txt"], _fps(repo, ["new.txt"]))
    assert e.value.status == 409
    assert (repo / "new.txt").exists(), "the panel deleted a file git has no copy of"


def test_discard_of_a_path_the_tree_no_longer_reports_is_refused(repo):
    """The window between render and click: the row is gone, so the request is too."""
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], _fps(repo, ["a.txt"]))
    assert e.value.status == 409
    assert "refresh" in str(e.value)


def test_discard_restores_from_the_index_for_a_staged_and_modified_path(repo):
    """A staged edit plus a later unstaged one: discarding drops the unstaged half only, which is
    what the row the operator clicked actually showed."""
    (repo / "a.txt").write_text("staged\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    (repo / "a.txt").write_text("and then some\n")
    gitwrite.git_discard(str(repo), ["a.txt"], _fps(repo, ["a.txt"]))
    assert (repo / "a.txt").read_text() == "staged\n"


def test_stage_then_unstage_round_trips(repo):
    (repo / "a.txt").write_text("edited\n")
    out = gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    assert any(e["kind"] == "staged" for e in out["status"]["entries"])
    out = gitwrite.git_stage(str(repo), ["a.txt"], False, _fps(repo, ["a.txt"]))
    kinds = {e["kind"] for e in out["status"]["entries"] if e["path"] == "a.txt"}
    assert kinds == {"changed"}


def test_stage_stages_a_deletion(repo):
    """`add` rather than `update-index`, so one call covers a removed path too (measured)."""
    (repo / "a.txt").unlink()
    out = gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    row = [e for e in out["status"]["entries"] if e["path"] == "a.txt"]
    assert row and row[0]["kind"] == "staged" and row[0]["index"] == "D"


def test_unstage_on_an_unborn_branch_uses_rm_cached(root):
    """MEASURED: ``restore --staged`` dies with "could not resolve HEAD" before the first commit."""
    p = root / "fresh"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "f.txt").write_text("x\n")
    _git(p, "add", "f.txt")
    out = gitwrite.git_stage(str(p), ["f.txt"], False, _fps(p, ["f.txt"]))
    kinds = {e["kind"] for e in out["status"]["entries"] if e["path"] == "f.txt"}
    assert kinds == {"untracked"}
    assert (p / "f.txt").exists(), "unstaging removed the file from the worktree"


def test_stage_refuses_a_path_from_the_wrong_side(repo):
    """Staging reads the not-staged side; a already-staged path is not offered there."""
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    with pytest.raises(FsError):
        gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))


def test_commit_records_what_was_staged(repo):
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    out = gitwrite.git_commit(str(repo), "feat: a real message", _staged_fp(repo))
    assert out["files"] == 1
    assert out["commit"]
    assert out["status"]["entries"] == []
    subject = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True
    ).stdout.strip()
    assert subject == "feat: a real message"


def test_commit_with_a_leading_dash_message_is_a_message_not_an_option(repo):
    """`--message=` rather than `-m <msg>`: the value can never be re-read as a flag."""
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    gitwrite.git_commit(str(repo), "--amend is not happening here", _staged_fp(repo))
    subject = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True
    ).stdout.strip()
    assert subject == "--amend is not happening here"
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "2"
    ), "the message was parsed as --amend and rewrote history"


def test_commit_refuses_an_empty_index(repo):
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), "nothing here", _staged_fp(repo))
    assert e.value.status == 409
    assert "nothing is staged" in str(e.value)


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_commit_refuses_a_message_that_is_not_one(repo, bad):
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), bad, _staged_fp(repo))
    assert e.value.status == 422


def test_commit_refuses_on_a_detached_head(repo):
    """The commit would succeed and then be one clean-tree `switch` away from unreachable."""
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), "on a detached head", _staged_fp(repo))
    assert e.value.status == 409
    assert "detached" in str(e.value)


def test_unmerged_paths_block_stage_and_commit(root):
    p = root / "conflict"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "f.txt").write_text("base\n")
    _git(p, "add", "f.txt")
    _git(p, "commit", "-qm", "base")
    _git(p, "checkout", "-qb", "side")
    (p / "f.txt").write_text("side\n")
    _git(p, "commit", "-qam", "side")
    _git(p, "checkout", "-q", "master")
    (p / "f.txt").write_text("main\n")
    _git(p, "commit", "-qam", "main")
    subprocess.run(["git", "-C", str(p), "merge", "side"], capture_output=True)
    with pytest.raises(FsError) as e:
        gitwrite.git_stage(str(p), ["f.txt"], True, _fps(p, ["f.txt"]))
    assert e.value.status == 409
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(p), "resolve", _staged_fp(p))
    assert e.value.status == 409


# ------------------------------------------------------- phase 3: push


@pytest.fixture()
def remote_repo(root, repo, local_transport):
    """A real (bare) remote on disk, so push is exercised end to end rather than stubbed.

    Depends on `local_transport`: a bare repo on disk is only reachable with `file` added back.
    Everything reached through this fixture is push MECHANICS; the protocol refusal has its own
    tests against the shipped allowlist.
    """
    bare = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    return bare


def test_push_without_an_expected_target_is_refused(repo, remote_repo):
    """The binding cannot be optional, or it is not a binding.

    `expect` carries the destination the panel actually rendered. Honouring it when present but
    accepting its absence means a stale client -- or anyone posting the body #806 originally
    documented -- silently gets the old re-resolve-and-hope behaviour, which is the exact path
    the binding exists to close. Omission is a refusal.
    """
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo))
    assert e.value.status == 422
    assert (
        subprocess.run(
            ["git", "-C", str(remote_repo), "for-each-ref"], capture_output=True, text=True
        ).stdout.strip()
        == ""
    ), "a push without a bound target still reached the remote"


def test_push_with_a_malformed_expected_target_is_refused(repo, remote_repo):
    """A present-but-unparseable `expect` must not degrade into "no expectation"."""
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect="not a target!")
    assert e.value.status in (409, 422)


def test_push_route_refuses_a_body_without_expect(client, repo, remote_repo, auth_cfg):
    """Pinned at the public mutation boundary, not just in the helper -- the route is what a
    client can actually reach."""
    r = client.post("/api/git/push", json={"path": str(repo)}, headers=_hdr(client, auth_cfg))
    assert r.status_code == 422, r.text


def test_push_sets_upstream_on_the_first_push(repo, remote_repo):
    out = gitwrite.git_push(str(repo), expect=_expect(repo))
    assert out["set_upstream"] is True
    assert out["target"] == "origin/master"
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "master@{upstream}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "origin/master"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(remote_repo), "rev-parse", "--verify", "refs/heads/master"],
            capture_output=True,
        ).returncode
        == 0
    )


def test_push_never_forces_and_never_takes_a_client_refspec(repo, remote_repo, monkeypatch):
    seen: dict = {}
    real = gitwrite.run_git_net

    def spy(r, gitdir, args, **kw):
        if args and args[0] == "push":
            seen["args"] = list(args)
        return real(r, gitdir, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_net", spy)
    gitwrite.git_push(str(repo), expect=_expect(repo))
    args = seen["args"]
    assert not any(a in ("--force", "-f", "--force-with-lease") for a in args)
    assert "--receive-pack" in args and args[args.index("--receive-pack") + 1] == "git-receive-pack"
    # The SOURCE is the verified oid, not the branch name: that is what makes the expectation
    # binding rather than merely checked next to the write. `push.default` gets no say either way.
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert args[-1] == f"{head}:refs/heads/master"


def test_push_refuses_an_ambiguous_target_naming_the_candidates(repo, remote_repo, root):
    """Two remotes, no upstream, no pushDefault: refuse, don't guess."""
    other = root / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "backup", str(other))
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect=PLACEHOLDER_EXPECT)
    assert e.value.status == 409
    assert "origin" in str(e.value) and "backup" in str(e.value)


def test_push_honours_remote_pushdefault_before_falling_back(repo, remote_repo, root):
    other = root / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "backup", str(other))
    _git(repo, "config", "remote.pushDefault", "backup")
    out = gitwrite.git_push(str(repo), expect=_expect(repo))
    assert out["remote"] == "backup"


def test_push_prefers_a_configured_upstream_over_everything_else(repo, remote_repo, root):
    other = root / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "backup", str(other))
    gitwrite.git_push(
        str(repo), "origin", expect=_expect(repo, "origin")
    )  # sets upstream to origin
    _git(repo, "config", "remote.pushDefault", "backup")
    out = gitwrite.git_push(str(repo), expect=_expect(repo))
    assert out["remote"] == "origin", "pushDefault overrode an existing upstream"
    assert out["set_upstream"] is False


def test_push_refuses_an_unknown_remote(repo, remote_repo):
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), "nope", expect="nope/master@0123456789abcdef")
    assert e.value.status == 422


def test_push_refuses_a_detached_head(repo, remote_repo):
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect=PLACEHOLDER_EXPECT)
    assert e.value.status == 409


def test_push_target_preflight_reports_the_resolved_name(repo, remote_repo):
    out = gitwrite.push_target(str(repo))
    assert out["ok"] is True
    assert out["target"] == "origin/master"
    assert out["set_upstream"] is True


def test_push_target_preflight_renders_ambiguity_rather_than_failing(repo, remote_repo, root):
    """The control has to DRAW the refusal and its candidate list, so the preflight answers with
    a state. Enforcement still lives on the POST."""
    other = root / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "backup", str(other))
    out = gitwrite.push_target(str(repo))
    assert out["ok"] is False
    assert sorted(out["candidates"]) == ["backup", "origin"]
    assert out["target"] is None


def test_push_target_changes_nothing(repo, remote_repo):
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    gitwrite.push_target(str(repo))
    assert (
        subprocess.run(
            ["git", "-C", str(remote_repo), "rev-parse", "--verify", "refs/heads/master"],
            capture_output=True,
        ).returncode
        != 0
    ), "a preflight pushed"
    after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    assert before == after


# ------------------------------------------------------- phase 2/3 routes


def test_every_new_write_route_needs_csrf(client, repo, auth_cfg):
    (repo / "a.txt").write_text("edited\n")
    for route, body in (
        ("/api/git/stage", {"paths": ["a.txt"], "staged": True}),
        ("/api/git/discard", {"paths": ["a.txt"]}),
        ("/api/git/commit", {"message": "nope"}),
        ("/api/git/push", {}),
    ):
        r = client.post(route, json={"path": str(repo), **body})
        assert r.status_code == 403, f"{route} accepted a request with no CSRF token"
    assert (repo / "a.txt").read_text() == "edited\n"


def test_no_phase_two_or_three_write_is_reachable_by_GET(client, repo):
    (repo / "a.txt").write_text("edited\n")
    for route in ("/api/git/stage", "/api/git/discard", "/api/git/commit", "/api/git/push"):
        r = client.get(route, params={"path": str(repo), "paths": "a.txt", "message": "x"})
        assert r.status_code in (404, 405), f"{route} answered a GET with {r.status_code}"
    assert (repo / "a.txt").read_text() == "edited\n", "a GET discarded a change"
    assert not subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"], capture_output=True
    ).returncode, "a GET staged a change"


def test_push_target_route_is_a_read(client, repo, remote_repo):
    r = client.get("/api/git/push-target", params={"path": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "origin/master"
    assert r.headers["cache-control"] == "no-store"


def test_stage_and_commit_through_the_routes(client, repo, auth_cfg):
    (repo / "a.txt").write_text("edited\n")
    h = _hdr(client, auth_cfg)
    r = client.post(
        "/api/git/stage",
        json={
            "path": str(repo),
            "paths": ["a.txt"],
            "staged": True,
            "expect": _fps(repo, ["a.txt"]),
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/git/commit",
        json={"path": str(repo), "message": "via the panel", "expect": _staged_fp(repo)},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 1


def test_discard_route_names_exactly_what_it_threw_away(client, repo, auth_cfg):
    (repo / "a.txt").write_text("edited\n")
    r = client.post(
        "/api/git/discard",
        json={"path": str(repo), "paths": ["a.txt"], "expect": _fps(repo, ["a.txt"])},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 200, r.text
    assert r.json()["discarded"] == ["a.txt"]
    assert (repo / "a.txt").read_text() == "one\n"


# ---------------------------------------------- review round 2: helper-execution vectors (#825)
#
# Each of these was REPRODUCED against the previous head before it was closed. They are not the
# accepted clean/smudge residual — none of them is inherent to fetching, committing or pushing.


def test_repo_configured_ssh_command_does_not_run_on_fetch(repo, root):
    """MEASURED: a repo-local `core.sshCommand` EXECUTES during fetch."""
    script, hit = _sentinel(root, "sshcmd")
    _git(repo, "config", "core.sshCommand", str(script))
    _git(repo, "remote", "add", "ssh1", "ssh://example.invalid/x.git")
    with pytest.raises((FsError, gitpanel.GitError)):
        gitwrite.git_fetch(str(repo), "ssh1")
    assert not hit.exists(), "the repository chose the ssh binary"


def test_repo_configured_gpg_program_does_not_run_on_commit(repo, root):
    """MEASURED: `commit.gpgSign=true` + `gpg.program=<helper>` executes that helper on commit."""
    script, hit = _sentinel(root, "gpg")
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "gpg.program", str(script))
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    gitwrite.git_commit(str(repo), "unsigned on purpose", _staged_fp(repo))
    assert not hit.exists(), "the repository chose a signing program"


def test_credential_helper_is_reset_on_the_command_line(repo):
    """An empty `credential.helper` RESETS the list (measured). Asserted on the argv, because a
    helper only fires against a server that actually challenges — a test that needs a live 401 to
    fail would pass for the wrong reason on an offline runner."""
    seen: dict = {}

    class _P:
        returncode = 0
        stdout = None
        stderr = None

        def __init__(self, argv, **kw):
            seen["argv"] = argv
            raise RuntimeError("stop")

    import unittest.mock as _m

    with _m.patch.object(gitwrite.subprocess, "Popen", _P), pytest.raises(RuntimeError):
        gitwrite.run_git_write(gitwrite.resolve_repo(str(repo)), ["status"])
    argv = seen["argv"]
    for pin in (
        "credential.helper=",
        "core.sshCommand=ssh",
        "core.gitProxy=",
        "gpg.program=false",
        "commit.gpgsign=false",
    ):
        assert pin in argv, f"{pin} is not pinned on the command line"


def test_the_protocol_allowlist_is_network_only():
    """`git://` is unauthenticated, unencrypted, and the ONLY protocol `core.gitProxy` applies to.
    `file` is out by operator decision (#806): a local-path remote was the root of every
    containment finding on this PR, and no amount of URL parsing closes a check-then-use race
    against config the repository owns. Network-only makes containment structural."""
    assert set(gitwrite.GIT_ALLOW_PROTOCOL.split(":")) == {"https", "ssh"}


def test_ssh_command_is_pinned_in_the_environment_too(repo):
    env = gitwrite._write_env(str(repo))
    assert env["GIT_SSH_COMMAND"] == "ssh"


# ---------------------------------------------- local transports are not reachable AT ALL
#
# Operator decision (#806): the panel speaks network transports only. These tests run against the
# REAL shipped allowlist -- none of them takes `local_transport`, and that is the point.


def test_a_file_remote_is_refused_by_protocol(repo, root, tmp_path):
    """The escape that used to need containment logic is now refused by git itself.

    This is the exact scenario earlier rounds reproduced: an outside repository reachable as a
    `file://` remote, whose objects a fetch would pull in and one `switch --create` would
    materialise inside the browsable tree. Now the transport never opens.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init", "-q")
    _git(outside, "config", "user.email", "t@t")
    _git(outside, "config", "user.name", "t")
    (outside / "secret.txt").write_text("SECRET FROM OUTSIDE\n")
    _git(outside, "add", "secret.txt")
    _git(outside, "commit", "-qm", "outside")
    _git(repo, "remote", "add", "ext", f"file://{outside}")

    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403
    msg = str(e.value)
    assert "https and ssh only" in msg
    # The refusal has to say where the operation IS still possible, or it reads as a bug.
    assert "terminal" in msg
    # ...and nothing came across.
    assert not (repo / "secret.txt").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/remotes/ext/master"],
            capture_output=True,
        ).returncode
        != 0
    ), "a remote-tracking ref was created, so objects were fetched"


@pytest.mark.parametrize(
    "spelling",
    [
        "file://{p}",
        "file://localhost{p}",
        "file://127.0.0.1{p}",
        "{p}",  # git takes a bare absolute path as a remote
        "{rel}",  # ...and a bare relative one, resolved from the repository
    ],
)
def test_every_local_remote_spelling_is_refused(repo, root, tmp_path, spelling):
    """One rule replaces the URL parser, and it covers the spellings that kept slipping past it.

    Each of these was, at some point in this PR's review history, a way through a containment
    check that had been written to handle the previous one. They are all the same thing to git:
    the `file` transport. MEASURED -- git names it `transport 'file' not allowed` for all five.
    """
    outside = tmp_path / "out.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    url = spelling.format(p=outside, rel=os.path.relpath(outside, repo))
    _git(repo, "remote", "add", "ext", url)
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403, f"{url} was not refused"
    # The MECHANISM still matters — the DELETED containment check also answered 403, so a
    # status-only assertion could not tell "refused by a real gate" from "refused by inspection
    # that crept back". But there are now two real gates, and which one fires first depends on
    # the spelling: `file://localhost/...` names a host, so the loopback gate (option C) catches
    # it before git's protocol allowlist ever runs. Both are intended; anything else is not.
    msg = str(e.value)
    assert (
        "https and ssh only" in msg or "points back at this machine" in msg
    ), f"{url} was refused by neither the protocol gate nor the loopback gate: {msg}"


def test_a_local_push_url_is_refused_and_writes_nothing(repo, root, tmp_path):
    """`remote.<n>.pushurl` shadowing an innocent `url` was its own finding. Asserted by EFFECT --
    the outside repository must have no refs -- because a refusal that still wrote would look
    identical from the exception alone."""
    inside = root / "in.git"
    subprocess.run(["git", "init", "-q", "--bare", str(inside)], check=True)
    outside = tmp_path / "out.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{inside}")
    _git(repo, "config", "remote.origin.pushurl", f"file://{outside}")

    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_push(str(repo), expect=_expect(repo))
    assert e.value.status == 403
    assert "https and ssh only" in str(e.value), "refused, but not by the protocol gate"
    assert (
        subprocess.run(
            ["git", "-C", str(outside), "for-each-ref"], capture_output=True, text=True
        ).stdout.strip()
        == ""
    ), "the push reached the outside repository"


def test_a_local_remote_INSIDE_the_root_is_refused_too(repo, root):
    """The cost of the decision, pinned so it cannot be walked back by accident.

    A sibling clone under `$HOME` was previously allowed -- containment was satisfied, and it
    worked. It does not any more: the rule is now the transport, not the location. This is the
    one test here that fails if someone "helpfully" re-adds `file` for the in-root case, which is
    exactly the shape the old check had and exactly what re-opens the race.
    """
    other = root / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "local", f"file://{other}")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "local")
    assert e.value.status == 403
    assert "terminal" in str(e.value), "the refusal does not say where this IS still possible"


def test_the_repository_cannot_lift_the_protocol_allowlist(repo, root, tmp_path):
    """The load-bearing measurement, pinned.

    `protocol.allow=never` on the command line loses to a repo's per-protocol key -- that is why
    the allowlist is an ENV allowlist and not a config pin. This asserts the direction that makes
    the whole decision work: repo-local `protocol.file.allow=always` (and the generic
    `protocol.allow=always` with it) does NOT beat `GIT_ALLOW_PROTOCOL`. If a git upgrade ever
    reversed that precedence, containment would silently come undone, and this test is what
    would notice.
    """
    outside = tmp_path / "out.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    _git(repo, "remote", "add", "ext", f"file://{outside}")
    _git(repo, "config", "protocol.file.allow", "always")
    _git(repo, "config", "protocol.allow", "always")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403
    assert "https and ssh only" in str(e.value), "the repository lifted the protocol allowlist"


def test_an_ssh_remote_is_not_refused_by_the_protocol_gate(repo, root):
    """The negative control: `ssh` IS allowed, so a refusal here must NOT be the protocol gate.

    Without this, every test above would still pass if the allowlist were empty -- and the panel
    would be unable to talk to any remote at all, which is a different bug wearing the same green.
    """
    _git(repo, "remote", "add", "ssh1", "ssh://git@example.invalid/x.git")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ssh1")
    assert "https and ssh only" not in str(e.value), "ssh was turned away by the protocol gate"


# ---------------------------------------------- a network URL can still mean THIS machine


#: Every spelling of "this machine" that a remote URL can carry. `ssh` is an allowed transport,
#: so each of these is a *network* URL by every syntactic test and a local read by effect —
#: which is precisely why the `file:`-removal's premise ("a network remote cannot reach the local
#: filesystem") was false, and why refusing them is a separate control.
LOCAL_HOSTS = [
    "localhost",
    "LOCALHOST",  # case
    "localhost.",  # trailing root dot
    "anything.localhost",  # RFC 6761 reserves the whole TLD
    "127.0.0.1",
    "127.1.2.3",  # the whole /8, not just .0.1
    "0.0.0.0",
    "[::1]",
    "[::]",
    "[::ffff:127.0.0.1]",  # IPv4-mapped — `is_loopback` is False for this, measured
]


@pytest.mark.parametrize("host", LOCAL_HOSTS)
def test_an_ssh_remote_pointing_at_this_machine_is_refused(repo, root, tmp_path, host):
    """REPRODUCED before this was written: `ssh://localhost/<outside path>` fetched, and created
    the outside remote-tracking ref. Each spelling below reaches the same place."""
    outside = tmp_path / "out.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    _git(repo, "remote", "add", "ext", f"ssh://{host}{outside}")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403
    assert "points back at this machine" in str(e.value)
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/remotes/ext/master"],
            capture_output=True,
        ).returncode
        != 0
    ), "the fetch reached the outside repository anyway"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_a_scp_like_remote_pointing_at_this_machine_is_refused(repo, root, host):
    """`user@host:path` is the spelling most people actually type, and it has no `://` to parse."""
    _git(repo, "remote", "add", "ext", f"git@{host}:/srv/out.git")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403


def test_a_name_that_RESOLVES_to_loopback_is_refused(repo, root, monkeypatch):
    """A literal-only check is sidestepped by any name pointing at 127.0.0.1, so names are
    resolved and every address checked. The resolver is stubbed rather than depending on a
    hosts-file entry existing on the runner."""
    real = socket.getaddrinfo

    def fake(host, *a, **kw):
        if host == "sneaky.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real(host, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert gitwrite.host_is_local("sneaky.test") is True
    _git(repo, "remote", "add", "ext", "ssh://sneaky.test/srv/out.git")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403


@pytest.mark.parametrize(
    "host", ["example.invalid", "192.0.2.10", "[2001:db8::1]", "git.example.test"]
)
def test_a_genuinely_remote_host_is_not_refused_as_local(repo, root, host):
    """The negative control, and it is load-bearing: without it, a classifier that called
    EVERYTHING local would pass every test above while disabling the feature outright."""
    _git(repo, "remote", "add", "ext", f"ssh://{host}/srv/out.git")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert "points back at this machine" not in str(e.value), f"{host} was called local"


def test_the_url_that_is_CHECKED_is_the_url_that_is_RUN(repo, root):
    """The property that makes this option C and not option A.

    Option A would inspect `remote.<n>.url` and then run `git fetch <name>`, leaving the config
    free to move in between. Here the resolved URL is what reaches the argv, so there is no second
    resolution to race. Asserted on the actual command rather than by reading the code.
    """
    other = root / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "origin", str(other))
    seen: dict = {}
    real = gitwrite.run_git_net

    def spy(r, gitdir, args, **kw):
        if args and args[0] == "fetch":
            seen["args"] = list(args)
        return real(r, gitdir, args, **kw)

    monkeypatch_setattr = pytest.MonkeyPatch()
    monkeypatch_setattr.setattr(
        gitwrite, "GIT_ALLOW_PROTOCOL", gitwrite.GIT_ALLOW_PROTOCOL + ":file"
    )
    monkeypatch_setattr.setattr(gitwrite, "run_git_net", spy)
    try:
        gitwrite.git_fetch(str(repo), "origin")
    finally:
        monkeypatch_setattr.undo()

    args = seen["args"]
    assert str(other) in args, "git was not handed the resolved URL"
    assert "origin" not in args, "git was handed the mutable remote NAME, so the check can be raced"


def test_a_remote_with_several_push_urls_is_refused_not_narrowed(repo, root):
    """git pushes to EVERY pushurl. Pinning the first would report success while silently
    dropping the rest, so this is a refusal rather than a narrowing."""
    a = root / "a.git"
    b = root / "b.git"
    for d in (a, b):
        subprocess.run(["git", "init", "-q", "--bare", str(d)], check=True)
    _git(repo, "remote", "add", "origin", str(a))
    _git(repo, "config", "--add", "remote.origin.pushurl", str(a))
    _git(repo, "config", "--add", "remote.origin.pushurl", str(b))
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect=PLACEHOLDER_EXPECT)
    assert e.value.status == 409
    assert "2 URLs" in str(e.value)


# ------------------------------------- round 8: "this machine" is more than loopback


def _own_addresses() -> list[str]:
    """Addresses actually assigned to this host, discovered rather than assumed.

    A hard-coded LAN address would pass on the machine it was written on and silently skip
    everywhere else — which is the same "table that was never executed" failure the loopback list
    was written to avoid.
    """
    found = []
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as sock:
                # No packet is sent by a UDP connect; it just picks the source address the kernel
                # would use for that route.
                sock.connect(
                    ("2001:4860:4860::8888", 80) if fam == socket.AF_INET6 else ("8.8.8.8", 80)
                )
                found.append(sock.getsockname()[0])
        except OSError:
            continue
    return [a for a in found if a and not a.startswith("127.")]


def test_a_non_loopback_address_of_THIS_host_is_refused(repo, root, tmp_path):
    """REPRODUCED in review: this host's own LAN address is not loopback, yet reaches this same
    filesystem — so a loopback-only predicate admitted it.

    The address is discovered at runtime, so this test means the same thing on any host.
    """
    mine = _own_addresses()
    if not mine:
        pytest.skip("this host has no non-loopback address to test with")
    addr = mine[0]
    assert gitwrite.host_is_local(addr) is True, f"{addr} is this host's own address"
    outside = tmp_path / "out.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    host = f"[{addr}]" if ":" in addr else addr
    _git(repo, "remote", "add", "ext", f"ssh://{host}{outside}")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "ext")
    assert e.value.status == 403
    assert "points back at this machine" in str(e.value)


def test_the_bind_probe_does_not_call_every_address_local(repo, root):
    """The negative control for the widened predicate. Without it, a `bind()` that always
    succeeded would make every test above pass while disabling all remotes."""
    for addr in ("192.0.2.10", "198.51.100.7", "8.8.8.8", "2001:db8::1"):
        assert gitwrite._addr_is_local(addr) is False, f"{addr} was called local"


# ------------------------------------- round 8: the expectation pins the DESTINATION


def test_a_pushurl_that_moves_after_the_preflight_is_refused(repo, root, remote_repo):
    """REPRODUCED: the preflight displayed `origin/master`, `remote.origin.pushurl` was repointed,
    and the push wrote the NEW destination while the label-only expectation still matched.

    Asserted by effect on both repositories, because "it refused" and "it refused but had already
    written" look identical from the exception.
    """
    token = _expect(repo)
    moved = root / "moved.git"
    subprocess.run(["git", "init", "-q", "--bare", str(moved)], check=True)
    _git(repo, "config", "remote.origin.pushurl", str(moved))

    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect=token)
    assert e.value.status == 409
    assert "changed after the panel showed it" in str(e.value)
    for name, d in (("moved", moved), ("original", remote_repo)):
        assert (
            subprocess.run(
                ["git", "-C", str(d), "for-each-ref"], capture_output=True, text=True
            ).stdout.strip()
            == ""
        ), f"the push reached the {name} repository"


def test_a_bare_label_is_no_longer_an_acceptable_expectation(repo, remote_repo):
    """The old spelling has to be REJECTED, not tolerated: a stale client sending `origin/master`
    would otherwise keep the label-only binding that finding 2 is about."""
    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect="origin/master")
    assert e.value.status == 422


def test_the_preflight_issues_an_expectation_that_the_push_accepts(repo, remote_repo):
    """The positive control: the token the preflight issues must actually work, or the two halves
    have drifted and every push is a 409."""
    out = gitwrite.git_push(str(repo), expect=_expect(repo))
    assert out["target"] == "origin/master"
    assert (
        subprocess.run(
            ["git", "-C", str(remote_repo), "rev-parse", "--verify", "refs/heads/master"],
            capture_output=True,
        ).returncode
        == 0
    )


# ------------------------------------- round 8: TLS is pinned at the url's own specificity


def test_tls_is_pinned_at_the_urls_own_specificity(repo, root):
    """MEASURED against a real self-signed endpoint: a generic `-c http.sslVerify=true` LOSES to
    the repository's url-specific key (the request went through), while `-c
    http.<that exact url>.sslVerify=true` made git refuse the certificate.

    Only expressible because the effective URL is pinned — before that there was no exact URL to
    name, which is why the earlier conclusion was that only a refusal could work.
    """
    url = "https://example.invalid/x.git"
    pin = gitwrite.tls_pin_for(url)
    # The helper pins TRANSPORT, not only verification: the proxy is reset at the same
    # specificity, because admitting the host says nothing about where the connection is routed.
    assert f"http.{url}.sslVerify=true" in pin
    assert f"http.{url}.proxy=" in pin
    assert "http.proxy=" in pin
    # git's own resolution of the key it would use, with the repo trying to turn it off.
    _git(repo, "config", f"http.{url}.sslVerify", "false")
    got = subprocess.run(
        ["git", "-C", str(repo), *pin, "config", "--get-urlmatch", "http.sslVerify", url],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert got == "true", "the repository's url-specific key beat the pinned one"


def test_the_tls_pin_is_only_applied_to_https(repo):
    """ssh and local paths have no `http.<url>` namespace; emitting one would be noise on every
    command and could collide with a real key."""
    assert gitwrite.tls_pin_for("ssh://example.test/x.git") == []
    assert gitwrite.tls_pin_for("/srv/x.git") == []
    assert gitwrite.tls_pin_for("") == []


def test_every_network_command_carries_the_tls_pin(repo, root, remote_repo):
    """Pinning that is not actually on the command line is not pinning. Asserted on the argv."""
    seen: list = []
    real = gitwrite.run_git_net

    def spy(r, gitdir, args, **kw):
        if args and args[0] in ("fetch", "push"):
            seen.append((args[0], kw.get("extra_config")))
        return real(r, gitdir, args, **kw)

    mp = pytest.MonkeyPatch()
    mp.setattr(gitwrite, "run_git_net", spy)
    try:
        gitwrite.git_push(str(repo), expect=_expect(repo))
    finally:
        mp.undo()
    assert seen, "no network command was observed"
    for verb, extra in seen:
        # A local bare repo is not https, so the pin is correctly empty — what matters is that the
        # parameter is threaded through at all rather than dropped on the floor.
        assert extra is not None, f"{verb} did not pass extra_config through"


# ------------------------------------- round 8: https connects to the address we checked


def test_an_https_destination_pins_the_addresses_it_was_admitted_on(monkeypatch):
    """DNS rebinding: we resolve a name, admit it, and git resolves it again — possibly to
    loopback. For https the answer is to hand git the addresses instead of the name.

    MEASURED: with `http.curloptResolve` set, git's failure becomes "Failed to connect to … port
    443" instead of "Could not resolve host", i.e. DNS was skipped and the pinned address used.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7", "203.0.113.8"])
    pins = gitwrite.admit_destination("https://git.example.test/x.git", "origin").pins
    assert "-c" in pins
    joined = " ".join(pins)
    assert "http.https://git.example.test/x.git.sslVerify=true" in joined
    # Both addresses, so DNS failover still works — pinning only the first would be an
    # availability regression traded for nothing.
    assert "http.curloptResolve=git.example.test:443:203.0.113.7,203.0.113.8" in joined


def test_the_address_pin_uses_the_url_s_own_port(monkeypatch):
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    pins = gitwrite.admit_destination("https://git.example.test:8443/x.git", "origin").pins
    assert "http.curloptResolve=git.example.test:8443:203.0.113.7" in " ".join(pins)


def test_the_addresses_pinned_are_the_addresses_CHECKED(monkeypatch):
    """The property that keeps this from being another check-then-use: one resolution feeds both
    the refusal and the pin. If admission ever resolved separately from pinning, a name could be
    admitted on one answer and connected on another — the very bug being fixed."""
    calls = []

    def counting(host):
        calls.append(host)
        return ["203.0.113.7"]

    monkeypatch.setattr(gitwrite, "resolve_addresses", counting)
    pins = gitwrite.admit_destination("https://git.example.test/x.git", "origin").pins
    assert calls == ["git.example.test"], f"resolved {len(calls)} times: {calls}"
    assert "203.0.113.7" in " ".join(pins)


def test_a_literal_https_address_needs_no_dns_pin(monkeypatch):
    """Nothing to rebind when the URL already names an address, so no pin is emitted."""
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    pins = gitwrite.admit_destination("https://203.0.113.7/x.git", "origin").pins
    assert "curloptResolve" not in " ".join(pins)
    assert "sslVerify=true" in " ".join(pins)


def test_an_ssh_destination_pins_its_ADDRESS_not_http_config(monkeypatch):
    """ssh has no `http.<url>` namespace, so it emits no `-c` — but it is no longer unpinned.

    The address ssh connects to is fixed to the one just checked, carried in `GIT_SSH_COMMAND`
    (the env wins over `core.sshCommand`, so a `-c` pin here would be silently ignored), and
    `HostKeyAlias` keeps host-key checking against the NAME so the operator's `known_hosts` entry
    still matches.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    got = gitwrite.admit_destination("ssh://git.example.test/x.git", "origin")
    assert got.pins == [], "no http config for a non-http transport"
    assert got.ssh_command == (
        "ssh -o HostName=203.0.113.7 -o HostKeyAlias=git.example.test"
    ), got.ssh_command


def test_the_ssh_pin_actually_reaches_GIT_SSH_COMMAND(repo, monkeypatch):
    """The plumbing, not just the string — because this is where it would silently do nothing.

    `_write_env` sets `GIT_SSH_COMMAND=ssh`, and this module already MEASURED that the environment
    beats `core.sshCommand`. So a pin delivered as `-c core.sshCommand=...` would be overridden by
    that plain "ssh" and quietly have no effect: the check would pass, the config would look
    right, and every connection would still resolve the name itself. This asserts the value that
    actually lands in the child's environment.
    """
    seen = {}

    class FakeProc:
        def __init__(self, *a, **k):
            seen.update(k.get("env") or {})
            self.stdout, self.stderr, self.returncode, self.pid = None, None, 0, 1

        def communicate(self, *a, **k):
            return ("", "")

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(gitwrite.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(gitwrite, "_read_bounded", lambda *a, **k: ("", False), raising=False)
    r = gitwrite.discover_repo(str(repo))
    try:
        gitwrite.run_git_write(r, ["status"], ssh_command="ssh -o HostName=203.0.113.7")
    except Exception:  # noqa: BLE001 - the fake process is not a real git; the env is the assertion
        pass
    assert seen.get("GIT_SSH_COMMAND") == "ssh -o HostName=203.0.113.7", seen.get("GIT_SSH_COMMAND")


@pytest.mark.parametrize(
    "host",
    [
        "evil.test; touch /tmp/pwned",
        "evil.test$(id)",
        "evil.test`id`",
        "evil.test|id",
        "-oProxyCommand=id",
        "evil test",
        'evil"test',
        "evil'test",
        "evil\ttest",
    ],
)
def test_a_hostname_that_is_not_a_hostname_is_REFUSED_not_quoted(host):
    """The pin is built by interpolation, so the control has to be an allowlist.

    Every one of these would be a shell metacharacter, an option, or a separator inside
    `GIT_SSH_COMMAND`. None is a legal hostname, so none is escaped or quoted — each is refused,
    which is the only version of this that cannot be got wrong by a later edit to the quoting.
    """
    with pytest.raises(FsError) as e:
        gitwrite._ssh_safe_host(host)
    assert e.value.status == 403


def test_the_pin_follows_the_operator_s_ssh_config_rewrite(monkeypatch):
    """`~/.ssh/config` may rewrite the hostname, and the pin must follow it.

    An earlier revision argued from this case that ssh could not be pinned at all. It can: `ssh -G`
    prints the fully resolved config and connects to nothing, so the EFFECTIVE name is knowable
    before any connection. Pinning an address resolved for the URL's name instead would send the
    connection somewhere the operator never configured.
    """

    class Out:
        returncode = 0
        stdout = "host alias-demo\nhostname real-target.example.test\nport 2222\n"

    monkeypatch.setattr(gitwrite.shutil, "which", lambda _n: "/usr/bin/ssh")
    monkeypatch.setattr(gitwrite.subprocess, "run", lambda *a, **k: Out())
    monkeypatch.setattr(
        gitwrite,
        "resolve_addresses",
        lambda h: ["198.51.100.9"] if "real-target" in h else ["1.2.3.4"],
    )
    got = gitwrite.admit_destination("ssh://alias-demo/x.git", "origin")
    assert got.ssh_command == (
        "ssh -o HostName=198.51.100.9 -o HostKeyAlias=real-target.example.test"
    ), got.ssh_command


def test_an_https_url_is_never_mistaken_for_scp_like_ssh(monkeypatch):
    """`https://h/x` reads as scp-like `host:path` if the scp pattern is tried first — `https` as
    the host, `//h/x` as the path. That silently dropped the TLS and DNS pins from every https
    remote, so scheme precedence is pinned here rather than left to the pin tests to notice."""
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    got = gitwrite.admit_destination("https://git.example.test/x.git", "origin")
    assert got.ssh_command is None
    assert "sslVerify=true" in " ".join(got.pins)


@pytest.mark.parametrize("url", ["ssh://git.example.test/x.git", "https://git.example.test/x.git"])
def test_a_named_destination_that_resolves_to_NOTHING_is_refused(url, monkeypatch):
    """Fail closed, on both network transports.

    `resolve_addresses` reports every lookup failure as an empty list, and an empty list used to
    mean "admitted, but unpinned" — no `curloptResolve` for https, no `-o HostName` for ssh. A
    DNS-controlled host could therefore answer NXDOMAIN while admission looked, and a LOCAL
    address when git looked a moment later: the whole escape restored through the one path that
    skipped the pin. An address set of nothing is not a reason to trust a name.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: [])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    with pytest.raises(FsError) as e:
        gitwrite.admit_destination(url, "origin")
    assert e.value.status == 403
    assert "could not be resolved" in str(e.value)


def test_a_literal_address_still_needs_no_resolution(monkeypatch):
    """The fail-closed rule must not refuse a URL that already names an address — there is no
    lookup to fail, and nothing for DNS to answer differently later."""
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: [h])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    assert gitwrite.admit_destination("https://203.0.113.7/x.git", "origin").pins
    assert gitwrite.admit_destination("ssh://203.0.113.7/x.git", "origin").ssh_command == (
        "ssh -o HostName=203.0.113.7 -o HostKeyAlias=203.0.113.7"
    )


def test_the_public_fetch_route_never_reaches_the_transport_for_an_unresolved_host(
    repo, root, monkeypatch
):
    """The whole point of failing closed: git must not get the chance to look the name up itself.

    Refusing inside `admit_destination` is only worth anything if no `fetch` is issued afterwards
    — otherwise the resolver gets a second answer, which is the rebinding this closes. So this
    asserts the absence of the transport call, not just the exception.
    """
    _git(repo, "remote", "add", "origin", "ssh://git.example.test/x.git")
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: [])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)

    ran: list[list[str]] = []
    real = gitwrite.run_git_write

    def spy(r, args, **kw):
        ran.append(list(args))
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    with pytest.raises(FsError) as e:
        gitwrite.git_fetch(str(repo), "origin")
    assert e.value.status == 403
    assert not any("fetch" in a for a in ran), f"the transport ran anyway: {ran}"


def test_a_repo_configured_http_proxy_is_never_CONTACTED(repo, monkeypatch):
    """Admitting the HOST says nothing about where the CONNECTION goes.

    With a repo-local `http.proxy` pointing at a loopback listener, git connected to that proxy
    even though the remote had been admitted as a public address — the local-service SSRF class
    restored behind a destination check that passed. Both spellings are set here because the
    url-specific one outbids a generic reset, exactly as with `sslVerify`: clearing only
    `http.proxy` would look right and do nothing.

    The assertion is that the listener accepts NOTHING. Checking the emitted `-c` list instead
    would only prove the flags were spelled, not that git honoured them.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    hits: list[int] = []
    stop = threading.Event()

    def accept_loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                hits.append(1)
                conn.close()
            except (TimeoutError, OSError):
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        url = "https://public.example.test/x.git"
        _git(repo, "remote", "add", "origin", url)
        _git(repo, "config", "http.proxy", f"http://127.0.0.1:{port}")
        _git(repo, "config", f"http.{url}.proxy", f"http://127.0.0.1:{port}")
        # A routable-looking but unreachable address (TEST-NET-3), so the only way a connection
        # completes at all is through the proxy — which is the thing under test.
        monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
        monkeypatch.setattr(gitwrite, "NET_TIMEOUT_S", 6.0)
        with contextlib.suppress(Exception):
            gitwrite.git_fetch(str(repo), "origin")
    finally:
        stop.set()
        t.join(timeout=3)
        srv.close()
    assert not hits, "git connected to the repository's own proxy despite admission"


def test_redirects_are_switched_OFF_for_the_admitted_url(monkeypatch):
    """A pin binds the URL that was CHECKED, not wherever a 30x sends the next hop.

    Measured by the reviewer: with the TLS/proxy/DNS pins in place, a redirect to a loopback HTTPS
    service was still followed and the connection accepted — so admission constrained nothing
    after the first response, and an attacker-controlled remote could bounce an operator's fetch
    onto a local service. git's default is `initial`, which still follows one hop, so the panel's
    own transports turn redirects off entirely; there is no legitimate reason for them to change
    destination mid-flight.
    """
    url = "https://git.example.test/x.git"
    joined = " ".join(gitwrite.tls_pin_for(url))
    assert "http.followRedirects=false" in joined
    assert f"http.{url}.followRedirects=false" in joined, "the url-specific key outbids a generic"


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://user:s3cr3tvalue@git.example.test/x.git", "s3cr3tvalue"),
        ("https://ghp_secrettoken@git.example.test/x.git", "ghp_secrettoken"),
        ("ssh://git:hunter2pass@git.example.test/x.git", "hunter2pass"),
        # The other spelling git accepts, and it reaches argv just as surely. The key families
        # are shared with `redact()`, so a secret this module would strip from a message can no
        # longer sail into a command line.
        ("https://git.example.test/x.git?access_token=qu3rys3cret", "qu3rys3cret"),
        ("https://git.example.test/x.git?oauth_token=oa2s3cret", "oa2s3cret"),
        ("https://git.example.test/x.git?client_secret=cl1s3cret", "cl1s3cret"),
    ],
)
def test_a_credential_in_the_url_is_refused_before_git_runs(url, secret):
    """`redact()` protects the stderr this module RETURNS; it cannot protect the command line.

    The resolved URL goes straight into fetch/pull/push argv and into `-c` arguments, so an inline
    token is published in `/proc/<pid>/cmdline` for the life of the operation — readable by any
    other local user on this shared host. Refused rather than stripped: removing the userinfo
    would silently change WHICH credential git uses and turn a visible failure into a puzzling
    one. A bare `https://<token>@host` counts too — the secret sits in the username position.
    """
    with pytest.raises(FsError) as e:
        gitwrite.admit_destination(url, "origin")
    assert e.value.status == 403
    assert "credential helper" in str(e.value)
    # The refusal names the problem without repeating the secret — an error message is one of the
    # places a credential most easily ends up copied into a log or a screenshot.
    assert secret not in str(e.value)
    assert url not in str(e.value)


def test_an_ordinary_url_without_userinfo_is_unaffected(monkeypatch):
    """The refusal keys on userinfo, so a normal remote must pass straight through."""
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    got = gitwrite.admit_destination("https://git.example.test/x.git", "origin")
    assert "sslVerify=true" in " ".join(got.pins)


def test_no_credential_reaches_the_ACTUAL_argv(repo, monkeypatch):
    """Asserted against the constructed command line, not against the refusal.

    `redact()` protects the stderr this module returns; `/proc/<pid>/cmdline` is readable by any
    other local user on this shared host for the whole life of the operation. The refusal is the
    mechanism, but the property is "the marker never appears in argv or the environment" — so
    that is what this checks, by capturing what would actually have been executed.
    """
    marker = "MARKERs3cret0000"
    seen: dict[str, object] = {}

    class FakeProc:
        def __init__(self, argv, *a, **k):
            seen["argv"] = list(argv)
            seen["env"] = dict(k.get("env") or {})
            self.args = list(argv)
            self.stdout = self.stderr = None
            self.returncode, self.pid = 0, 1

        def communicate(self, *a, **k):
            return ("", "")

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0

        def kill(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gitwrite.subprocess, "Popen", FakeProc)
    _git(repo, "remote", "add", "origin", "https://placeholder.example.test/x.git")
    for url in (
        f"https://u:{marker}@git.example.test/x.git",
        f"https://git.example.test/x.git?access_token={marker}",
    ):
        _git(repo, "remote", "set-url", "origin", url)
        seen.clear()
        with contextlib.suppress(Exception):
            gitwrite.git_fetch(str(repo), "origin")
        blob = (
            " ".join(str(x) for x in seen.get("argv", []))
            + " "
            + " ".join(f"{k}={v}" for k, v in (seen.get("env") or {}).items())
        )
        assert marker not in blob, f"the secret reached the command line for {url!r}"


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        # A legacy `;` separator: the benign `ok=1` used to consume the rest of the string, so the
        # later assignment was never a candidate for `_is_credential_key` at all.
        ("https://git.example.test/x.git?ok=1;client_secret=semis3cret", "semis3cret"),
        # A nested URL: the outer value ran through the inner query for the same reason.
        (
            "https://git.example.test/x.git?next=https://o/y?access_token=nesteds3cret",
            "nesteds3cret",
        ),
        ("https://git.example.test/x.git#code=fragments3cret", "fragments3cret"),
    ],
)
def test_a_benign_pair_cannot_hide_a_later_credential(url, secret):
    """One blind spot produced two failures, because admission and redaction share the matcher.

    `_QUERY_PAIR` ended a value only at `&` or whitespace, so `?ok=1;client_secret=<secret>` was a
    single pair named `ok` — benign, therefore admitted, and therefore left intact in any error
    git returned. Ending a value at `;`, `?` and `#` as well splits the assignments apart. Erring
    toward splitting is safe: it can only offer MORE names for `_is_credential_key` to judge.

    Asserted on both surfaces, since one matcher feeds both.
    """
    with pytest.raises(FsError) as e:
        gitwrite.admit_destination(url, "origin")
    assert e.value.status == 403
    assert secret not in str(e.value)
    assert secret not in gitwrite.redact(f"fatal: could not read from {url}")


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@github.example/x.git",
        "git@github.example:org/x.git",
        "ssh://myuser@git.example.test/x.git",
    ],
)
def test_an_ordinary_ssh_LOGIN_NAME_is_not_treated_as_a_credential(url, monkeypatch):
    """`git@host` is how every git host spells an ssh remote.

    An earlier round refused ALL userinfo to keep tokens out of argv, which would have broken
    essentially every real ssh remote this panel exists to drive. It survived its own tests
    because they all used `https://`; it was caught by probing an ordinary remote. A login name
    is not a secret — a PASSWORD is, on any transport, and over https a username is either
    useless or a token wearing one.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    got = gitwrite.admit_destination(url, "origin")
    assert got.ssh_command, "an admitted ssh remote must still be address-pinned"


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        # Bracketed and percent-encoded nesting are why key CLASSIFICATION was abandoned: each
        # fix was right and each round produced another spelling. The whole component goes now.
        ("https://git.example.test/x.git?oauth[client_secret]=brackets3cret", "brackets3cret"),
        ("https://git.example.test/x.git?next=%68ttps://o?access_token=enc0ded", "enc0ded"),
        ("https://git.example.test/x.git?harmless=1", "harmless"),
    ],
)
def test_a_query_string_is_refused_WHOLESALE_not_classified(url, secret):
    """No lexer competes with every way a secret can be written, so none is asked to.

    A git remote does not need a query string, so the whole component is refused and redacted as
    one unit. The third case carries nothing secret at all and is still refused — that is the
    point: the rule is structural, not a judgement about the name.
    """
    with pytest.raises(FsError) as e:
        gitwrite.admit_destination(url, "origin")
    assert e.value.status == 403
    assert "query string" in str(e.value)
    assert secret not in gitwrite.redact(f"fatal: could not read from {url}")


# ------------------------------------- bound to what the operator saw, not to a name


def test_discard_refuses_a_row_that_CHANGED_after_it_was_confirmed(repo):
    """The confirmation names a path; the file is what gets destroyed.

    "Discard a.txt" used to mean "discard whatever a.txt contains when the command runs". The
    session agent shares this worktree and never takes the panel's lock, so the gap between the
    operator reading the row and confirming it is reachable without any concurrent PANEL use —
    which is why a lock alone cannot close it and the request has to carry what was shown.
    """
    (repo / "a.txt").write_text("original\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("the edit the operator looked at\n")
    shown = gitwrite.git_status(str(repo))
    fp = {e["path"]: e["fp"] for e in shown["entries"] if e["path"] == "a.txt"}
    assert fp, "the row must carry a fingerprint"

    # The agent writes something else while the confirmation is on screen.
    (repo / "a.txt").write_text("work the operator never saw\n")

    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], fp)
    assert e.value.status == 409
    assert "changed after you confirmed it" in str(e.value)
    assert (repo / "a.txt").read_text() == "work the operator never saw\n", "it was destroyed"


def test_discard_still_works_when_nothing_moved(repo):
    """The binding must not break the ordinary case — a confirmation acted on promptly."""
    (repo / "a.txt").write_text("original\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("edit\n")
    shown = gitwrite.git_status(str(repo))
    fp = {e["path"]: e["fp"] for e in shown["entries"] if e["path"] == "a.txt"}
    gitwrite.git_discard(str(repo), ["a.txt"], fp)
    assert (repo / "a.txt").read_text() == "original\n"


def test_commit_refuses_an_index_that_gained_a_file_after_review(repo):
    """`git commit` records the whole INDEX, not the rows that were ticked.

    So binding a commit to individual paths would not catch this: the danger is a file being
    ADDED to the index between the panel's read and the commit, which no per-row check sees. The
    whole staged set is fingerprinted for that reason.
    """
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    shown = gitwrite.git_status(str(repo))
    staged_fp = shown["staged_fp"]

    # The agent stages something the operator was never shown.
    (repo / "secret.txt").write_text("not reviewed\n")
    _git(repo, "add", "secret.txt")

    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), "msg", staged_fp)
    assert e.value.status == 409
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True
    )
    assert "msg" not in out.stdout, "it committed the unreviewed index anyway"


def test_switch_refuses_a_tree_that_went_dirty_after_the_check(repo, monkeypatch):
    """`switch` silently CARRIES uncommitted work across, so a stale cleanliness check drags
    edits onto another branch.

    The mutation has to land BETWEEN the early check and the locked section — dirtying the tree
    before the call just trips the early check and proves nothing (measured: that version passed
    with both in-lock checks deleted). `_guarded` is where the lock is taken, so writing the file
    as it is entered is exactly the interleaving, made deterministic.
    """
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    clean_fp = gitwrite.git_status(str(repo))["dirty_fp"]

    real_guarded = gitwrite._guarded

    def dirty_then_guard(r, fn):
        (repo / "a.txt").write_text("the agent wrote this after the check\n")
        return real_guarded(r, fn)

    monkeypatch.setattr(gitwrite, "_guarded", dirty_then_guard)
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "other", None, None, clean_fp)
    assert e.value.status == 409, str(e.value)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() != "other", "it switched with a dirty tree"


def test_stage_refuses_a_row_that_changed_under_it(repo):
    """Same rule for staging: the row described content, and that is what must be staged."""
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("shown\n")
    fp = {
        e["path"]: e["fp"]
        for e in gitwrite.git_status(str(repo))["entries"]
        if e["path"] == "a.txt"
    }
    (repo / "a.txt").write_text("never shown\n")
    with pytest.raises(FsError) as e:
        gitwrite.git_stage(str(repo), ["a.txt"], True, fp)
    assert e.value.status == 409


def test_a_write_that_names_no_expectation_is_REFUSED(repo):
    """Optional was the hole: the safety contract held only for clients that opted into it.

    A guarantee any caller can decline by omitting a field is not a guarantee — and the probe
    that found this simply called `git_discard(path, ["a.txt"])` with no expectation and watched
    the current bytes be destroyed. Every operation whose contract depends on a binding now
    refuses without one.
    """
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("edited\n")

    for call in (
        lambda: gitwrite.git_discard(str(repo), ["a.txt"]),
        lambda: gitwrite.git_stage(str(repo), ["a.txt"], True),
        lambda: gitwrite.git_commit(str(repo), "msg"),
    ):
        with pytest.raises(FsError) as e:
            call()
        assert e.value.status == 422, str(e.value)
    assert (repo / "a.txt").read_text() == "edited\n", "an unbound call still mutated"

    # `switch` needs a CLEAN tree to reach the binding check at all — with a dirty one the
    # (legitimate) dirty-tree refusal fires first, and asserting 422 there would be asserting the
    # wrong thing.
    _git(repo, "checkout", "--", "a.txt")
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "other")
    assert e.value.status == 422, str(e.value)


def test_an_expectation_must_cover_EXACTLY_the_paths_being_changed(repo):
    """A partial expectation would bless the paths it omits.

    Two files, an expectation naming one: the other would ride along unverified, which is the
    same hole as omitting the field entirely, just quieter.
    """
    for n in ("a.txt", "b.txt"):
        (repo / n).write_text("base\n")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-qm", "base")
    for n in ("a.txt", "b.txt"):
        (repo / n).write_text("edited\n")

    partial = _fps(repo, ["a.txt"])
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt", "b.txt"], partial)
    assert e.value.status == 422
    assert (repo / "b.txt").read_text() == "edited\n"


def test_push_refuses_a_branch_that_MOVED_after_the_preflight(repo, remote_repo):
    """The preflight answered "where does this go" and never "what goes there".

    A token that pins only the destination stays valid while the branch advances, so an agent
    committing after the operator saw the preflight had that commit pushed under an expectation
    that still matched. Reproduced by the reviewer: take a token, commit, push, and the remote
    received the post-preflight commit.
    """
    token = _expect(repo)
    (repo / "sneaked.txt").write_text("never shown\n")
    _git(repo, "add", "sneaked.txt")
    _git(repo, "commit", "-qm", "after the preflight")

    with pytest.raises(FsError) as e:
        gitwrite.git_push(str(repo), expect=token)
    assert e.value.status == 409
    assert "moved after the panel showed you" in str(e.value)
    out = subprocess.run(
        ["git", "-C", str(remote_repo), "log", "--oneline", "--all"],
        capture_output=True,
        text=True,
    )
    assert "after the preflight" not in out.stdout, "the unseen commit reached the remote"


def test_push_SENDS_the_verified_oid_not_the_branch_name(repo, remote_repo, monkeypatch):
    """Checking next to the write is not binding; sending the oid is.

    Even if the branch moves between the check and the command, git is handed the commit that was
    verified — the name is never re-resolved by git itself.
    """
    token = _expect(repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    gitwrite.git_push(str(repo), expect=token)
    out = subprocess.run(
        ["git", "-C", str(remote_repo), "rev-parse", "--verify", "refs/heads/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out == head, "the remote did not receive exactly the verified commit"


def test_pull_refuses_a_tree_dirtied_while_the_lock_was_taken(repo, remote_repo, monkeypatch):
    """The dirty-tree refusal has to survive the gap between the check and the merge.

    A probe dirtied a tracked file while `_guarded` was being entered and pull fast-forwarded
    anyway, leaving that edit on the new base — the opposite of what the named refusal promises.
    Mutating as the lock is taken is the interleaving; doing it before the call only trips the
    early check and proves nothing.
    """
    _git(repo, "push", "-q", "-u", "origin", "master")
    real = gitwrite._guarded

    def dirty_then_guard(r, fn):
        (repo / "a.txt").write_text("the agent wrote this after the check\n")
        return real(r, fn)

    monkeypatch.setattr(gitwrite, "_guarded", dirty_then_guard)
    with pytest.raises(FsError) as e:
        gitwrite.git_pull(str(repo))
    assert e.value.status == 409, str(e.value)
    assert "commit or discard" in str(e.value)


def test_branch_creation_uses_the_OID_it_validated(repo):
    """`git switch -c <new> <start>` resolves that mutable name when it runs.

    So a start point that moved between the membership check and the command created a branch at
    a commit the operator never selected. The check resolves to an object and the command is
    given that object.
    """
    _git(repo, "checkout", "-q", "other")
    (repo / "on-other.txt").write_text("x\n")
    _git(repo, "add", "on-other.txt")
    _git(repo, "commit", "-qm", "the commit the operator selected")
    selected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "other"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", "master")

    real = gitwrite._guarded

    def move_then_guard(r, fn):
        # `other` advances after validation, before the command runs.
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "other"], check=True)
        (repo / "moved.txt").write_text("never selected\n")
        subprocess.run(["git", "-C", str(repo), "add", "moved.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "moved after validation"], check=True
        )
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "master"], check=True)
        return real(r, fn)

    monkeypatch_target = gitwrite
    orig = monkeypatch_target._guarded
    monkeypatch_target._guarded = move_then_guard
    try:
        gitwrite.git_switch(str(repo), "fresh", True, "other", _dirty_fp(repo))
    finally:
        monkeypatch_target._guarded = orig
    created = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "fresh"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert created == selected, "the branch was created at a commit that was never selected"


def test_a_push_that_reached_the_remote_is_never_reported_as_a_failure(
    repo, remote_repo, monkeypatch
):
    """The remote advanced; only the local tidying failed. Those are different facts.

    Local settlement runs AFTER the remote has changed, and an injected `update-ref` failure made
    the whole call raise — so the API reported a failure for a push that had succeeded. The
    operator acts on that differently: a failure invites a retry, and a retry of a completed push
    is at best noise. Each settlement step is idempotent, so re-running is a safe repair.
    """
    token = _expect(repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    real = gitwrite.run_git_write

    def fail_update_ref(r, args, **kw):
        if args and args[0] == "update-ref":
            raise gitwrite.GitError("simulated: could not lock ref", status=400)
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", fail_update_ref)
    out = gitwrite.git_push(str(repo), expect=token)

    assert out["pushed"] == head
    assert out["settled"] is False
    assert out["settle_error"], "the operator is told WHAT did not finish"
    remote_head = subprocess.run(
        ["git", "-C", str(remote_repo), "rev-parse", "--verify", "refs/heads/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == head, "the remote really did advance"


def test_discard_preserves_the_bytes_it_replaces(repo):
    """The window cannot be closed, so the loss is made reversible instead.

    There is no compare-and-swap between the panel and the session agent: they share a worktree,
    the agent takes no lock, and git has no "restore only if this still hashes to X". Narrowing
    the race is not closing it — but every byte about to be replaced is written to the object
    database first, so an edit that lands inside the window is recoverable by oid rather than
    gone. That is the property the operator actually needs.
    """
    (repo / "a.txt").write_text("committed\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("work in progress\n")

    out = gitwrite.git_discard(str(repo), ["a.txt"], _fps(repo, ["a.txt"]))
    assert (repo / "a.txt").read_text() == "committed\n", "the discard did not happen"

    oids = out["recoverable"]["a.txt"]
    assert oids, "the replaced bytes were not preserved at all"
    oid = oids[0]
    restored = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", oid], capture_output=True, text=True
    )
    assert restored.stdout == "work in progress\n", "the replaced bytes are not recoverable"


def test_a_stage_landing_MID_COMMIT_is_not_in_the_commit(repo, monkeypatch):
    """The commit records the tree that was VERIFIED, so an unreviewed file cannot get in.

    `git commit` writes whatever the index holds when it runs, so the old shape had to let the
    race happen and undo it afterwards — commit, compare trees, `reset --soft` on a mismatch. That
    worked, but it meant the defence was a rollback, and a rollback is only as good as its own
    correctness (it ate a concurrent commit twice during review).

    Building the object from the verified tree removes the race instead of compensating for it:
    `commit-tree` is handed the exact tree, so a stage landing at the boundary changes the INDEX
    and not the commit — which is what it should do, and it shows up as a staged row afterwards.
    """
    (repo / "a.txt").write_text("reviewed\n")
    _git(repo, "add", "a.txt")
    staged_fp = _staged_fp(repo)
    real = gitwrite.run_git_write
    fired: list[int] = []

    def stage_extra_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "write-tree" and not fired:
            fired.append(1)
            (repo / "sneaked.txt").write_text("never reviewed\n")
            subprocess.run(["git", "-C", str(repo), "add", "sneaked.txt"], check=True)
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", stage_extra_at_the_boundary)
    out = gitwrite.git_commit(str(repo), "msg", staged_fp)
    assert fired, "the boundary was never reached — the test proves nothing"

    files = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert files == ["a.txt"], f"the commit carries files nobody reviewed: {files}"
    assert out["commit"], "no commit was reported"
    # And the late stage is not lost — it is simply still staged, where it belongs.
    still = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True
    ).stdout.split()
    assert "sneaked.txt" in still, "the late stage was discarded instead of left staged"


def test_a_commit_landing_MID_COMMIT_is_never_overwritten(repo, monkeypatch):
    """Someone else's commit on this branch must make ours refuse, not rewind theirs.

    The old shape resolved `HEAD^` and `HEAD` separately to decide whether a rollback was safe;
    two reads of a moving ref could agree with each other and both be wrong, and the swap then
    discarded both commits. There is no rollback now — the branch is moved once, by name, with an
    expected-old value — so a branch that moved underneath simply fails the swap.
    """
    (repo / "a.txt").write_text("reviewed\n")
    _git(repo, "add", "a.txt")
    staged_fp = _staged_fp(repo)
    real = gitwrite.run_git_write
    fired: list[int] = []

    def commit_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "commit-tree" and not fired:
            fired.append(1)
            (repo / "theirs.txt").write_text("someone else's work\n")
            subprocess.run(["git", "-C", str(repo), "add", "theirs.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "THEIR COMMIT"], check=True)
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", commit_at_the_boundary)
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), "mine", staged_fp)
    assert e.value.status == 409
    assert fired, "the boundary was never reached — the test proves nothing"

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "--all"], capture_output=True, text=True
    ).stdout
    assert "THEIR COMMIT" in log, "the commit path discarded a commit it did not make"


def test_a_branch_switch_MID_COMMIT_does_not_move_the_commit_to_the_other_branch(repo, monkeypatch):
    """The commit lands on the branch that was CHOSEN, not on whatever HEAD names later.

    `git commit` commits to HEAD. The branch name was read before the lock and reported back
    afterwards, so a switch in between put the commit on another branch while the response named
    the original — the operator is told one thing and the history says another.

    `update-ref refs/heads/<branch>` names the branch explicitly, so a wandering HEAD cannot
    redirect it.
    """
    (repo / "a.txt").write_text("reviewed\n")
    _git(repo, "add", "a.txt")
    staged_fp = _staged_fp(repo)
    before_other = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/other"], capture_output=True, text=True
    ).stdout.strip()
    real = gitwrite.run_git_write
    fired: list[int] = []

    def switch_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "commit-tree" and not fired:
            fired.append(1)
            subprocess.run(
                ["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/other"], check=True
            )
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", switch_at_the_boundary)
    try:
        out = gitwrite.git_commit(str(repo), "mine", staged_fp)
    except FsError:
        out = None
    assert fired, "the boundary was never reached — the test proves nothing"

    after_other = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/other"], capture_output=True, text=True
    ).stdout.strip()
    assert after_other == before_other, "the commit landed on the branch nobody chose"
    if out is not None:
        assert out["branch"] == "master", f"reported {out['branch']!r}, committed to master"


def test_a_repo_that_REWRITES_the_admitted_url_is_refused(repo, monkeypatch):
    """`url.<base>.insteadOf` rewrites a URL inside git — including one given on the argv.

    The reviewer's stated repro does NOT reproduce: `_effective_url` resolves through
    `remote get-url`, which APPLIES the rewrite (measured), so admission already sees
    `ssh://127.0.0.1/...` and the local-destination gate refuses it. This asserts that refusal so
    it cannot silently regress.

    The TOCTOU variant IS real — a rewrite added after admission still rewrites the pinned
    string — and is closed separately by narrowing `GIT_ALLOW_PROTOCOL` to the admitted scheme,
    which the next test covers.
    """
    _git(repo, "remote", "add", "origin", "https://good.invalid/repo.git")
    _git(repo, "config", "url.ssh://127.0.0.1/outside/.insteadOf", "https://good.invalid/")
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])

    ran: list[list[str]] = []
    real = gitwrite.run_git_write

    def spy(r, args, **kw):
        ran.append(list(args))
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", spy)
    with pytest.raises(FsError) as e:
        gitwrite.git_fetch(str(repo), "origin")
    assert e.value.status == 403
    # ANY rewrite key refuses now, and that check runs before admission — so this is the rewrite
    # refusal rather than the local-destination one. Both would be correct; the rewrite message
    # is the more useful, because it names what the operator has to change.
    assert "rewrites remote URLs" in str(e.value)
    assert not any("fetch" in a for a in ran), "git ran despite the rewrite"


def test_the_transport_is_pinned_to_the_scheme_that_was_admitted(monkeypatch):
    """A rewrite added AFTER admission cannot connect, because the transport is refused.

    Pinning the URL string is not enough — `insteadOf` applies to command-line URLs too, so a
    rewrite landing after the check turns the pinned `https://` into `ssh://` and every https pin
    stays attached to a URL git no longer uses. Narrowing `GIT_ALLOW_PROTOCOL` to the admitted
    scheme makes the rewrite inert whenever it arrives, which a config check at admission time
    cannot do.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    assert (
        gitwrite.admit_destination("https://git.example.test/x.git", "origin").allow_protocol
        == "https"
    )
    assert (
        gitwrite.admit_destination("ssh://git.example.test/x.git", "origin").allow_protocol == "ssh"
    )


@pytest.mark.parametrize("url", ["http://h/x.git", "git://h/x.git", "ftp://h/x.git"])
def test_the_transport_pin_can_never_WIDEN_the_allowlist(url, monkeypatch):
    """A pin that derives its value from its input is not a pin.

    Narrowing `GIT_ALLOW_PROTOCOL` to the admitted scheme is what makes a `url.insteadOf` rewrite
    inert — but deriving that scheme from the URL alone inverted it: an `http://` remote set the
    variable to `http` and thereby ENABLED a plaintext transport the allowlist exists to forbid.
    The approved set is the authority; the URL only ever selects from it.
    """
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    with pytest.raises(FsError) as e:
        gitwrite.admit_destination(url, "origin")
    assert e.value.status == 403
    assert "does not drive" in str(e.value)


def test_the_scp_like_ssh_form_is_still_admitted(monkeypatch):
    """`git@host:path` carries no scheme and IS ssh — reading the scheme literally refused the
    most common ssh remote there is."""
    monkeypatch.setattr(gitwrite, "resolve_addresses", lambda h: ["203.0.113.7"])
    monkeypatch.setattr(gitwrite, "ssh_effective_host", lambda h: h)
    assert (
        gitwrite.admit_destination("git@github.example:org/x.git", "origin").allow_protocol == "ssh"
    )


# ---------------------------------------------- TLS


@pytest.mark.parametrize("key", ["http.sslVerify", "http.https://example.invalid/.sslVerify"])
def test_a_repository_that_disables_tls_verification_is_refused(repo, root, key):
    """MEASURED: `-c http.sslVerify=true` LOSES to the url-specific key, which is more specific —
    the same shadowing that makes `-c remote.<n>.uploadpack` ineffective. There is no env var that
    force-ENABLES verification, so the honest control is a refusal, not a claimed pin."""
    other = root / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{other}")
    _git(repo, "config", key, "false")
    with pytest.raises(FsError) as e:
        gitwrite.git_fetch(str(repo), "origin")
    assert e.value.status == 403
    assert "TLS" in str(e.value)


# ---------------------------------------------- redaction


@pytest.mark.parametrize(
    "text",
    [
        "fatal: could not read https://user:s3cr3t@host/x.git",
        "fatal: unable to access 'https://host/x.git?access_token=s3cr3t'",
        "remote: denied https://host/x.git?private_token=s3cr3t&ref=main",
        "fatal: https://host/x?api_key=s3cr3t",
    ],
)
def test_a_secret_never_survives_redaction(text):
    out = gitwrite.redact(text)
    # No `, out` message: a failing redaction assertion must not print the thing it failed to
    # redact -- into pytest output, into CI logs, into the review comment quoting them.
    assert "s3cr3t" not in out
    assert "<redacted>" in out


@pytest.mark.parametrize(
    "key",
    [
        "to%6Ben",  # -> token
        "pass%77ord",  # -> password
        "sec%72et",  # -> secret
        "%61uth",  # -> auth
        "%74%6F%6B%65%6E",  # -> token, every character encoded
        "acce%73s_token",  # encoded OUTSIDE the stem -- this one already worked
        "client%5Fsecret",
    ],
)
def test_a_percent_encoded_credential_key_still_redacts(key):
    """REPRODUCED: `to%6Ben=` leaked, `acce%73s_token=` did not.

    The old matcher read the key name literally, so encoding a character *inside* the stem hid it
    while encoding one *outside* the stem left the stem intact and matched. That is precisely the
    spelling someone hiding a secret would reach for, and the difference between the two cases is
    why "add the encoded spellings to the list" is not the fix -- there are unboundedly many.
    Decode the name, then classify it.

    Absence only, and the marker is never interpolated into an assertion message.
    """
    out = gitwrite.redact(f"fatal: unable to access 'https://host/x.git?{key}=s3cr3t'")
    assert "s3cr3t" not in out
    assert "<redacted>" in out


def test_a_real_git_failure_does_not_echo_an_encoded_credential(repo, root):
    """End to end through real git rather than through `redact()` alone: the string a client
    actually receives is git's stderr after this module has handled it, and only a real run
    proves the two are wired together."""
    _git(repo, "remote", "add", "origin", "https://host.invalid/x.git?to%6Ben=s3cr3t")
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "origin")
    assert "s3cr3t" not in str(e.value)


def _encoded_at(level: int) -> str:
    """A credential key name hidden under `level` rounds of percent-encoding.

    Level 1 hides a character INSIDE the stem (`to%6Ben`); each further level escapes the `%` of
    the one before. Built rather than hard-coded so the construction is visible, and asserted to
    decode back to `token` in the test below — a case that did not actually decode to a credential
    name would make the whole parametrisation vacuous, which is how the first version of this
    probe fooled me.
    """
    k = "to%6Ben"
    for _ in range(level - 1):
        k = k.replace("%", "%25", 1)
    return k


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 8])
def test_redaction_decodes_to_a_fixed_point_not_a_round_limit(level):
    """REPRODUCED: stopping after three decode rounds leaked at four.

    A fixed round count is a number an attacker can simply exceed. Decoding runs to a fixed point
    instead, which terminates because every successful decode strictly shortens the string.
    """
    key = _encoded_at(level)
    decoded = key
    for _ in range(level):
        decoded = urllib.parse.unquote(decoded)
    assert decoded == "token", f"bad case: {key!r} decodes to {decoded!r}, not a credential name"

    out = gitwrite.redact(f"fatal: unable to access 'https://h/x.git?{key}=s3cr3t'")
    assert "s3cr3t" not in out
    assert "<redacted>" in out


@pytest.mark.parametrize("length", [64, 65, 200, 1000])
def test_redaction_has_no_key_length_blind_spot(length):
    """REPRODUCED: the key-name pattern was capped at 64 characters, so a 65-character name ending
    in `token` was not classified at all and its value came back intact. A length cap on the NAME
    is a blind spot, not a bound — the useful bound is the 8 KiB stderr cap that already applies
    to the whole message."""
    key = "z" * (length - 5) + "token"
    assert len(key) == length
    out = gitwrite.redact(f"fatal: unable to access 'https://h/x.git?{key}=s3cr3t'")
    assert "s3cr3t" not in out


def test_redaction_leaves_the_useful_part_alone():
    """The message IS the value of this surface — redaction must not reduce it to nothing."""
    out = gitwrite.redact("fatal: repository 'https://host/x.git' not found")
    assert "not found" in out and "host/x.git" in out


# ---------------------------------------------- process teardown


def test_a_timeout_reaps_the_whole_process_group(repo, root, monkeypatch):
    """`proc.kill()` signals git and NOTHING it spawned: a transport helper's child outlived the
    route after the operator was told the operation had been stopped."""
    marker = root / "descendant.pid"
    # `git` is replaced by a script that backgrounds a long-lived child and then sleeps, which is
    # the shape of git-plus-helper without needing a real remote.
    fake = root / "fakegit.sh"
    fake.write_text(
        "#!/bin/sh\n" f"sh -c 'sleep 60 & echo $! > \"{marker}\"; wait' &\n" "sleep 60\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(gitwrite, "git_bin", lambda: str(fake))
    with pytest.raises(gitpanel.GitError):
        gitwrite.run_git_write(gitwrite.resolve_repo(str(repo)), ["status"], timeout=1.0)
    time.sleep(0.3)
    assert marker.exists(), "the harness never started a descendant — the test proves nothing"
    pid = int(marker.read_text().strip())
    with pytest.raises(OSError):
        os.kill(pid, 0)  # ESRCH: the descendant went with the group


# ---------------------------------------------- JSON types cannot invert an operation


def test_a_string_false_does_not_create_a_branch(client, repo, auth_cfg):
    """REPRODUCED: `bool("false")` is True, so JSON `"false"` for `create` made a branch instead
    of switching to one. A truthiness coercion that can INVERT the operation is the one thing a
    write API must not do quietly."""
    r = client.post(
        "/api/git/switch",
        json={"path": str(repo), "branch": "other", "create": "false", "expect": _dirty_fp(repo)},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 422, r.text
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(branches) == ["master", "other"]


def test_a_string_false_does_not_stage(client, repo, auth_cfg):
    (repo / "a.txt").write_text("edited\n")
    r = client.post(
        "/api/git/stage",
        json={"path": str(repo), "paths": ["a.txt"], "staged": "false"},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 422, r.text
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"], capture_output=True
        ).returncode
        == 0
    ), "a string 'false' staged the file"


def test_a_start_point_must_be_a_branch_the_server_just_listed(repo):
    """Shape-validation is not enough: `start` reaches git as a REVISION, so an arbitrary object
    id would otherwise be accepted despite the issue's contract."""
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "fromsha", True, sha, _dirty_fp(repo))
    assert e.value.status == 422
    # A real branch still works.
    gitwrite.git_switch(str(repo), "fromother", True, "other", _dirty_fp(repo))


# ---------------------------------------------- a commit cannot exceed what was shown


def test_commit_is_refused_when_the_status_was_truncated(repo, root, monkeypatch):
    """`git commit` commits the INDEX, but the panel only ever showed a truncated list of it.

    The status caps at `GIT_MAX_ENTRIES`; past that it sets `truncated` and stops listing. The
    commit control then advertises the number of rows it can see, while the commit itself takes
    everything staged — so past the cap the operator commits files they were never shown, which is
    the one direction this surface must not fail in.

    The cap is lowered here rather than staging 5,001 real files: the property under test is
    "truncated status ⇒ refusal", and the constant is what defines truncated. Stated plainly so
    nobody reads this as a 5,001-file test — it is not one.
    """
    monkeypatch.setattr(gitpanel, "GIT_MAX_ENTRIES", 2)
    for n in ("f1.txt", "f2.txt", "f3.txt", "f4.txt"):
        (repo / n).write_text("new\n")
    _git(repo, "add", "f1.txt", "f2.txt", "f3.txt", "f4.txt")

    st = gitpanel.git_status(str(repo))
    assert st["truncated"] is True, "cap not reached — the test would prove nothing"
    assert len(st["entries"]) < 4, "every staged file was listed, so nothing was hidden"

    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(FsError) as e:
        gitwrite.git_commit(str(repo), "commits more than was shown", _staged_fp(repo))
    assert e.value.status == 409
    assert "truncated" in str(e.value).lower() or "too many" in str(e.value).lower()
    after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert before == after, "the commit went through anyway"


def test_commit_still_works_when_the_status_is_complete(repo, root):
    """The negative control: the refusal must be about truncation, not about staging in general."""
    (repo / "a.txt").write_text("edited\n")
    gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    out = gitwrite.git_commit(str(repo), "a normal commit", _staged_fp(repo))
    assert out["files"] == 1


# ---------------------------------------------- switch cannot create when it was not asked to


def test_switch_without_create_cannot_create_a_branch_through_dwim(repo, root):
    """MEASURED: `git switch -- topic`, with only `origin/topic` present, CREATES a local branch
    and reports success. `--` does not stop it -- DWIM is not option parsing -- so a `create:false`
    request could still bring a branch into existence, which is the one thing the flag means.

    Two conditions have to hold for the vector at all, and both are reproduced below rather than
    assumed: a configured `remote.<n>.fetch` refspec, and exactly one matching remote-tracking
    ref. Without the refspec git refuses with `invalid reference` and the test would pass while
    proving nothing.
    """
    _git(repo, "remote", "add", "origin", "https://example.invalid/x.git")
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(repo, "update-ref", "refs/remotes/origin/topic", "HEAD")

    # --- reproduce it on plain git, exactly as the hardening table's sentinels do ---
    baseline = subprocess.run(
        ["git", "-C", str(repo), "switch", "--", "topic"], capture_output=True, text=True
    )
    assert baseline.returncode == 0, f"vector not reproduced: {baseline.stderr!r}"
    assert _branches(repo) == {"master", "other", "topic"}, "vector not reproduced"
    _git(repo, "switch", "-q", "master")
    _git(repo, "branch", "-q", "-D", "topic")
    assert _branches(repo) == {"master", "other"}

    # --- the hardened path refuses, and creates nothing ---
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "topic", None, None, _dirty_fp(repo))
    assert e.value.status == 422
    assert _branches(repo) == {"master", "other"}, "a create:false switch created a branch"
    assert _head(repo) == "master", "HEAD moved on a refused switch"


def test_switch_passes_no_guess_so_the_refusal_is_structural_too(repo, root):
    """The membership check gives the operator a readable 422; `--no-guess` is what makes the
    refusal hold even if a branch is deleted between the check and the command. Both, not either:
    the first is the message, the second is the guarantee."""
    seen: dict = {}
    real = gitwrite.run_git_write

    def spy(r, args, **kw):
        if args and args[0] == "switch":
            seen["args"] = list(args)
        return real(r, args, **kw)

    monkeypatch_target = gitwrite
    old = monkeypatch_target.run_git_write
    monkeypatch_target.run_git_write = spy
    try:
        gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    finally:
        monkeypatch_target.run_git_write = old
    assert "--no-guess" in seen["args"], "switch can still guess a branch into existence"


def test_switch_to_a_branch_that_does_not_exist_anywhere_is_a_named_refusal(repo):
    """No remote-tracking ref, so no DWIM -- but the operator still deserves the same 422 rather
    than git's `invalid reference`."""
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "nosuchbranch", None, None, _dirty_fp(repo))
    assert e.value.status == 422
    assert "nosuchbranch" in str(e.value)


# ---------------------------------------------- pull leaves the base alone under live work


def test_pull_refuses_a_dirty_tree(repo, root):
    """MEASURED: a fast-forward with an UNRELATED dirty file succeeds and moves HEAD. git is right
    that nothing was clobbered, but this panel is docked into a session an agent is editing, and
    changing the base under live work is the same hazard as `switch` carrying edits across."""
    bare = root / "up.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{bare}")
    _git(repo, "push", "-q", "-u", "origin", "master")
    (repo / "b.txt").write_text("UNRELATED LOCAL EDIT\n")
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    with pytest.raises(FsError) as e:
        gitwrite.git_pull(str(repo))
    assert e.value.status == 409
    assert "commit or discard" in str(e.value)
    after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    assert before == after
    assert (repo / "b.txt").read_text() == "UNRELATED LOCAL EDIT\n"


# ---------------------------------------------- the epoch guard


def test_a_status_begun_before_a_write_is_never_its_answer(repo, root, monkeypatch):
    """MEASURED on the previous head: a read already IN FLIGHT when the write landed was rejoined,
    and its pre-write value returned as the write's own result — `invalidate_status` only dropped
    SETTLED entries. Dropping the cache is not enough; the epoch is what makes it impossible."""
    started = threading.Event()
    release = threading.Event()
    real = gitpanel.git_status

    def slow(path, min_epoch=None):
        if min_epoch is None:  # only the "poller" read is parked
            started.set()
            release.wait(5)
        return real(path, min_epoch=min_epoch)

    monkeypatch.setattr(gitpanel, "git_status", slow)
    monkeypatch.setattr(gitwrite, "git_status", slow)

    poller = threading.Thread(target=lambda: slow(str(repo)))
    poller.start()
    assert started.wait(5)
    # The repository changes while that read is parked mid-flight.
    (repo / "a.txt").write_text("edited while a read was in flight\n")
    out = gitwrite.git_stage(str(repo), ["a.txt"], True, _fps(repo, ["a.txt"]))
    release.set()
    poller.join(5)
    kinds = {e["kind"] for e in out["status"]["entries"] if e["path"] == "a.txt"}
    assert kinds == {"staged"}, f"the write returned a pre-write status: {out['status']}"


# ---------------------------------------------- the write side THROUGH the relay (#579)
#
# #806 names an app-mode branch switch through the tunnel as acceptance evidence, because
# CSRF/Origin rewriting across the relay is the boundary under test. This drives that boundary
# for real: a browser-side `Mux` opens an HTTP stream, the agent's `AppProxyTarget` rewrites the
# headers exactly as it does in production, and the request is served by the REAL app — real
# `csrf_guard`, real Origin check, real `gitwrite.git_switch`, real repository on disk.
#
# What it is not: a browser. The remaining gap is the SPA's own `tunnel.fetch` adapter, which is
# covered by `web/src/homefree/tunnel.test.ts` against a real mux pair. Said plainly rather than
# claimed as the full app-mode E2E.


async def _relay_request(proxy, browser_mux, method, path, headers, body=b""):
    import struct as _struct

    s = browser_mux.open(
        json.dumps({"k": "http", "method": method, "path": path, "headers": headers}).encode()
    )
    if body:
        await s.write(body)
    await s.end()
    buf = bytearray()
    while len(buf) < 4:
        part = await s.read(4 - len(buf))
        if not part:
            break
        buf += part
    n = _struct.unpack(">I", bytes(buf))[0]
    meta_raw = bytearray()
    while len(meta_raw) < n:
        part = await s.read(n - len(meta_raw))
        if not part:
            break
        meta_raw += part
    payload = bytearray()
    while True:
        part = await s.read()
        if not part:
            break
        payload += part
    return json.loads(bytes(meta_raw)), bytes(payload)


def test_a_branch_switch_survives_the_relay_with_its_csrf_and_origin(
    repo, root, monkeypatch, auth_cfg
):
    """The write side rides the existing tunnel unchanged — proven, not argued."""
    import asyncio

    import httpx

    from agent_sessions import main
    from agent_sessions.homefree.appproxy import AppProxyTarget
    from agent_sessions.homefree.mux import Mux

    # `auth_cfg` is what makes this hermetic: `create_app` reads the auth env directly, so
    # without it the test passes only on a machine that happens to export a secret key — which is
    # exactly how it passed locally and failed in CI.
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "http://127.0.0.1:8765")
    app = main.create_app()

    async def go():
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        proxy = AppProxyTarget(app_port=8765, client=client)
        muxes: dict = {}
        muxes["browser"] = Mux(is_initiator=True, on_send=lambda f: muxes["agent"].feed(f))
        muxes["agent"] = Mux(
            is_initiator=False,
            on_send=lambda f: muxes["browser"].feed(f),
            on_stream=lambda s: asyncio.ensure_future(proxy.serve(s)),
        )
        b = muxes["browser"]

        # 1. Pick up a session cookie + CSRF token the way the connect page does.
        meta, body = await _relay_request(proxy, b, "GET", "/api/config", {})
        assert meta["status"] == 200, body
        csrf = json.loads(body)["csrf"]
        cookies = "; ".join(
            v.split(";", 1)[0] for k, v in meta["headers"] if k.lower() == "set-cookie"
        )

        # 2. The actual branch switch, over the tunnel.
        meta, body = await _relay_request(
            proxy,
            b,
            "POST",
            "/api/git/switch",
            {
                # The browser's own origin — the proxy must REWRITE this to the app's, which is
                # the whole reason a relayed POST is not rejected by the Origin check.
                "Origin": "https://battlelab.superstatus.io",
                "Cookie": cookies,
                "X-CSRF-Token": csrf,
                "Content-Type": "application/json",
            },
            json.dumps({"path": str(repo), "branch": "other", "expect": _dirty_fp(repo)}).encode(),
        )
        return meta, body

    meta, body = asyncio.run(go())
    assert meta["status"] == 200, body
    assert json.loads(body)["branch"] == "other"
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "other", "the relayed switch did not reach the repository"


def test_a_relayed_write_without_csrf_is_still_refused(repo, root, monkeypatch, auth_cfg):
    """The relay must not become a way around the guard it is supposed to carry."""
    import asyncio

    import httpx

    from agent_sessions import main
    from agent_sessions.homefree.appproxy import AppProxyTarget
    from agent_sessions.homefree.mux import Mux

    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "http://127.0.0.1:8765")
    app = main.create_app()

    async def go():
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        proxy = AppProxyTarget(app_port=8765, client=client)
        muxes: dict = {}
        muxes["browser"] = Mux(is_initiator=True, on_send=lambda f: muxes["agent"].feed(f))
        muxes["agent"] = Mux(
            is_initiator=False,
            on_send=lambda f: muxes["browser"].feed(f),
            on_stream=lambda s: asyncio.ensure_future(proxy.serve(s)),
        )
        return await _relay_request(
            proxy,
            muxes["browser"],
            "POST",
            "/api/git/switch",
            {"Content-Type": "application/json"},
            json.dumps({"path": str(repo), "branch": "other"}).encode(),
        )

    meta, _ = asyncio.run(go())
    assert meta["status"] == 403
    head = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "master"


# ------------------------------------- review round 3 (#825): config-driven redirection


def test_core_worktree_cannot_redirect_a_write_outside_the_root(repo, root, tmp_path):
    """MEASURED: a repo-local `core.worktree` pointing outside `$HOME` makes `switch` write THERE
    — the containment boundary the whole panel rests on, walked around by one config key.
    `--work-tree` on the command line wins (also measured), which is the structural fix."""
    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    # The branches must DIFFER, or a checkout writes nothing and the test proves nothing —
    # `other` is created at `master` by the fixture, which made the first version vacuous.
    _git(repo, "switch", "-q", "other")
    (repo / "only-on-other.txt").write_text("two\n")
    _git(repo, "add", "only-on-other.txt")
    _git(repo, "commit", "-qm", "second")
    _git(repo, "switch", "-q", "master")
    _git(repo, "config", "core.worktree", str(outside))
    gitwrite.git_switch(str(repo), "other", None, None, _dirty_fp(repo))
    assert list(outside.iterdir()) == [], "the checkout escaped to the configured worktree"
    assert (repo / "only-on-other.txt").exists(), "the contained worktree was not the one updated"


def test_the_tls_refusal_fails_CLOSED_when_the_config_is_too_large(repo, root):
    """`git config --list` was silently truncated at 1 MiB, so a url-specific `sslVerify=false`
    past the cap was never seen and the refusal returned "fine" — a fail-OPEN on a security
    decision. Truncation is now a refusal in its own right."""
    other = root / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{other}")
    # >1 MiB of harmless config, then the dangerous key behind it.
    filler = "x" * 200
    with (repo / ".git" / "config").open("a") as fh:
        fh.write("\n[pad]\n")
        for i in range(6000):
            fh.write(f"\tk{i} = {filler}\n")
        fh.write('[http "https://example.invalid/"]\n\tsslVerify = false\n')
    with pytest.raises((FsError, gitpanel.GitError)) as e:
        gitwrite.git_fetch(str(repo), "origin")
    assert e.value.status in (403, 413), f"fetch was allowed with status {e.value.status}"


@pytest.mark.parametrize(
    "text",
    [
        "fatal: https://h/x?client_secret=s3cr3t",
        "fatal: https://h/x?oauth_token=s3cr3t",
        "fatal: https://h/x?code=s3cr3t",
        "fatal: https://h/x?X-Amz-Signature=s3cr3t",
        "fatal: https://h/x?apikey=s3cr3t",
    ],
)
def test_redaction_covers_the_credential_key_families(text):
    assert "s3cr3t" not in gitwrite.redact(text), gitwrite.redact(text)


def test_the_documented_from_field_is_honoured(client, repo, auth_cfg):
    """#806 documents the start point as `from`; the route read only `start`, so a caller
    following the published contract silently created from HEAD instead of the named ref."""
    # `other` must diverge from HEAD, or "created from HEAD" and "created from other" produce the
    # same SHA and the assertion is vacuous — which is exactly how the first version passed
    # against the unfixed code.
    _git(repo, "switch", "-q", "other")
    (repo / "only-on-other.txt").write_text("two\n")
    _git(repo, "add", "only-on-other.txt")
    _git(repo, "commit", "-qm", "second")
    _git(repo, "switch", "-q", "master")
    r = client.post(
        "/api/git/switch",
        json={
            "path": str(repo),
            "branch": "from-other",
            "create": True,
            "from": "other",
            "expect": _dirty_fp(repo),
        },
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 200, r.text
    shas = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "from-other", "other", "master"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert shas[0] == shas[1], "the branch was created from HEAD, not from the named start point"
    assert shas[0] != shas[2], "the fixture did not diverge — the assertion would be vacuous"


# ------------------------------- the races, bound rather than narrowed
#
# Every test below writes at the exact boundary a reviewer's probes wrote at. They are the point
# of the whole "bind the observation through the mutation" change: each one FAILS against the
# check-then-use shape it replaced, where the comment said the window could only be narrowed.


def _cat(repo, oid: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", oid], capture_output=True, text=True
    ).stdout


def test_a_write_landing_MID_DISCARD_is_still_recoverable(repo, monkeypatch):
    """The pre-image must cover every version, including one that arrives during the replacement.

    The old shape hashed the file and then ran `git restore` over it, and said in a comment that
    the gap between the two could not be closed. An edit landing in that gap was overwritten by
    `restore` AND missing from every returned oid — neither prevented nor recoverable, which is
    the one combination the pre-image existed to rule out.

    The replacement never reads before it writes: it displaces the current bytes with `rename`,
    and creates the new file with `O_EXCL` so a writer that claims the freed name is displaced in
    turn instead of being overwritten. Whatever occupied the name, at any point, comes back.
    """
    (repo / "a.txt").write_text("committed\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("work in progress\n")
    fps = _fps(repo, ["a.txt"])

    real = gitwrite.run_git_write
    fired: list[int] = []

    def write_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "hash-object" and not fired:
            fired.append(1)
            (repo / "a.txt").write_text("LATE EDIT\n")
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", write_at_the_boundary)
    out = gitwrite.git_discard(str(repo), ["a.txt"], fps)

    assert fired, "the boundary was never reached — the test proves nothing"
    assert (repo / "a.txt").read_text() == "committed\n", "the discard did not happen"
    bodies = {_cat(repo, oid) for oid in out["recoverable"]["a.txt"]}
    assert "LATE EDIT\n" in bodies, "the edit that landed during the discard is gone"
    assert "work in progress\n" in bodies, "the reviewed pre-image is gone"


def test_a_write_landing_MID_STAGE_is_not_the_one_that_gets_staged(repo, monkeypatch):
    """`git add` re-reads the file when it runs; the index must hold what was VERIFIED.

    "Stage a.txt" used to mean "stage whatever a.txt holds by the time git gets there", so bytes
    that arrived after the last check were staged under a row describing the old ones — content
    nobody reviewed riding into the index. Hashing first and installing that blob id makes the
    staged object the verified one; a later edit changes the FILE, which is what it should do and
    which shows up as a fresh unstaged row.
    """
    (repo / "a.txt").write_text("reviewed\n")
    fps = _fps(repo, ["a.txt"])

    real = gitwrite.run_git_write
    seen = {"verify": 0}
    real_verify = gitwrite.verify_rows

    def count_verify(r, want, verb):
        out = real_verify(r, want, verb)
        seen["verify"] += 1
        if seen["verify"] == 2:
            # The instant before the staging command itself — where the old code re-checked and
            # then handed the PATH to git anyway.
            (repo / "a.txt").write_text("NEVER REVIEWED\n")
        return out

    monkeypatch.setattr(gitwrite, "verify_rows", count_verify)
    monkeypatch.setattr(gitwrite, "run_git_write", real)
    gitwrite.git_stage(str(repo), ["a.txt"], True, fps)

    assert seen["verify"] >= 2, "the boundary was never reached — the test proves nothing"
    staged = subprocess.run(
        ["git", "-C", str(repo), "show", ":a.txt"], capture_output=True, text=True
    ).stdout
    assert staged == "reviewed\n", f"the index holds bytes nobody was shown: {staged!r}"


def test_a_write_landing_MID_SWITCH_is_carried_back_not_left_on_the_other_branch(repo, monkeypatch):
    """`switch` cannot be bound, so it is made reversible — and it must actually reverse.

    git has no conditional checkout: the cleanliness that makes a switch safe is a whole-worktree
    property only the command could observe atomically, so checking one syscall earlier changes
    nothing and a probe writing at that boundary had its edit carried onto the other branch.

    What rescues it is that `switch` CARRIES uncommitted work rather than dropping it, so
    switching back is a complete undo rather than a second guess.
    """
    dirty_fp = _dirty_fp(repo)
    real = gitwrite.run_git_write
    fired: list[int] = []

    def dirty_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "switch" and not fired:
            fired.append(1)
            (repo / "a.txt").write_text("WRITTEN WHILE SWITCHING\n")
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", dirty_at_the_boundary)
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "other", None, None, dirty_fp)
    assert e.value.status == 409
    assert fired, "the boundary was never reached — the test proves nothing"

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "master", f"the panel left the operator on {head!r} after refusing the switch"
    assert (repo / "a.txt").read_text() == "WRITTEN WHILE SWITCHING\n", "the late edit was lost"


def test_a_branch_switch_MID_PULL_never_fast_forwards_the_branch_nobody_chose(
    repo, root, monkeypatch, local_transport
):
    """Pull must move the branch it was asked for, not whatever HEAD names when `merge` runs.

    `git merge --ff-only` advances HEAD, so checking that HEAD was still on the intended branch
    and then calling merge is check-then-use with git on the far side of the gap — a probe
    switched branches at exactly that point and the pull fast-forwarded a branch nobody selected.

    Naming the ref and giving `update-ref` the value it must still hold removes both halves: the
    wrong branch cannot be the one that moves, and a branch that moved underneath fails the swap.
    """
    bare = root / "up.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{bare}")
    _git(repo, "push", "-q", "-u", "origin", "master")
    # `other` (from the fixture) is well behind, and nothing in this pull may touch it.
    other_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/other"], capture_output=True, text=True
    ).stdout.strip()
    # Someone else advances the remote.
    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    (clone / "upstream.txt").write_text("from the remote\n")
    _git(clone, "add", "upstream.txt")
    _git(clone, "commit", "-qm", "upstream work")
    _git(clone, "push", "-q", "origin", "HEAD:master")

    real = gitwrite.run_git_write
    fired: list[int] = []

    def switch_away_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "rev-parse" and "origin/master" in args and not fired:
            fired.append(1)
            # Exactly where the old code had already finished checking which branch HEAD named.
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "other"], check=True)
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", switch_away_at_the_boundary)
    try:
        gitwrite.git_pull(str(repo))
    except (FsError, gitwrite.GitError):
        pass  # refusing is a fine outcome; fast-forwarding `other` is not
    assert fired, "the boundary was never reached — the test proves nothing"

    other_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/other"], capture_output=True, text=True
    ).stdout.strip()
    assert other_after == other_before, "the pull fast-forwarded a branch nobody selected"


def test_push_does_not_retarget_an_upstream_someone_configured_meanwhile(
    repo, remote_repo, monkeypatch
):
    """`will_set` is a preflight verdict; by settlement time it may describe a branch that has
    since been given a deliberate upstream. Overwriting that would replace a choice with a guess
    derived from which button was pressed."""
    real = gitwrite.run_git_net
    fired: list[int] = []

    def configure_upstream_mid_push(r, gitdir, args, **kw):
        out = real(r, gitdir, args, **kw)
        if args and args[0] == "push" and not fired:
            fired.append(1)
            subprocess.run(
                ["git", "-C", str(repo), "config", "branch.master.remote", "somewhere-else"],
                check=True,
            )
        return out

    monkeypatch.setattr(gitwrite, "run_git_net", configure_upstream_mid_push)
    gitwrite.git_push(str(repo), expect=_expect(repo))
    assert fired, "the boundary was never reached — the test proves nothing"
    got = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "branch.master.remote"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert got == "somewhere-else", "the push overwrote an upstream it did not set"


def test_bytes_that_cannot_be_hashed_are_never_deleted(repo, monkeypatch):
    """The preserve step must not become a delete step when git fails.

    Written the obvious way — hash inside `try`, unlink inside `finally` — a `hash-object` failure
    swallows the error and removes the only copy of the bytes, which is a worse version of the
    defect the displacement exists to fix. It is unreachable in normal operation and catastrophic
    when reached, which is exactly the shape worth pinning.

    So on failure the file stays on disk under its `.battlelab-displaced-` name, the discard
    refuses, and the refusal says where the bytes are.
    """
    (repo / "a.txt").write_text("committed\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("PRECIOUS\n")
    fps = _fps(repo, ["a.txt"])

    real = gitwrite.run_git_write

    def break_hash_object(r, args, **kw):
        if args and args[0] == "hash-object":
            raise gitwrite.GitError("hash-object exploded")
        return real(r, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_write", break_hash_object)
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], fps)
    assert e.value.status == 409

    survivors = [p for p in repo.iterdir() if p.name.startswith(gitwrite.DISPLACED_PREFIX)]
    assert survivors, "the bytes were deleted by the step that was supposed to preserve them"
    assert survivors[0].read_text() == "PRECIOUS\n"
    assert survivors[0].name in str(e.value), "the refusal does not say where the bytes went"


def test_discard_rebuilds_a_directory_the_operator_deleted(repo):
    """`rm -rf sub/` then discarding `sub/a.txt` must put the file back, directory and all.

    `git restore` recreated the path on the way to the file. Walking to the parent by descriptor
    does not, unless it is told to — and a discard that cannot restore a deleted file is broken in
    precisely the case the control exists for. Measured against the descriptor walk before this:
    it refused with "a directory on the way to it is missing".

    Creating it costs nothing in containment: the `O_NOFOLLOW` open is still what vets the result,
    so a symlink swapped into the gap is refused rather than followed.
    """
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("committed\n")
    _git(repo, "add", "sub/a.txt")
    _git(repo, "commit", "-qm", "with a subdir")
    shutil.rmtree(repo / "sub")
    fps = _fps(repo, ["sub/a.txt"])  # AFTER the delete: that deletion is the row being discarded

    out = gitwrite.git_discard(str(repo), ["sub/a.txt"], fps)
    assert out["discarded"] == ["sub/a.txt"]
    assert (repo / "sub" / "a.txt").read_text() == "committed\n"


def test_discard_refuses_a_parent_that_became_a_symlink(repo, root):
    """Recreating a missing directory must not become "follow whatever is there now".

    The containment claim is that a path resolves inside the worktree at the moment bytes move,
    not at the moment it was validated. A component that is a symlink fails the `O_NOFOLLOW` open,
    and that has to stay true on the branch that creates directories.
    """
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("committed\n")
    _git(repo, "add", "sub/a.txt")
    _git(repo, "commit", "-qm", "with a subdir")
    shutil.rmtree(repo / "sub")
    outside = root / "elsewhere"
    outside.mkdir()
    (repo / "sub").symlink_to(outside, target_is_directory=True)
    fps = _fps(repo, ["sub/a.txt"])

    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["sub/a.txt"], fps)
    assert e.value.status == 409
    assert not (outside / "a.txt").exists(), "the discard wrote through a symlink"


def test_discard_restores_a_file_LARGER_than_the_stdout_cap(repo):
    """A blob read through the ordinary runner is silently cut off at `_MAX_STDOUT`.

    The runner caps stdout at 1 MiB and only turns that cap into an error when the caller asks
    for `require_complete`. Reading blob content through it therefore restored any larger tracked
    file **truncated, while reporting success** — corruption that looks exactly like a working
    discard until someone opens the file.

    Handing git the descriptor `O_EXCL` just created removes the size question: the bytes never
    pass through this process at all.
    """
    big = ("x" * 63 + "\n") * 40_000  # ~2.5 MiB, comfortably past the cap
    (repo / "big.txt").write_text(big)
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-qm", "a big tracked file")
    assert len(big.encode()) > gitwrite._MAX_STDOUT, "the fixture no longer exceeds the cap"

    (repo / "big.txt").write_text("clobbered\n")
    fps = _fps(repo, ["big.txt"])
    gitwrite.git_discard(str(repo), ["big.txt"], fps)

    back = (repo / "big.txt").read_text()
    assert len(back) == len(big), f"restored {len(back)} bytes of {len(big)} — truncated"
    assert back == big


def test_staging_a_path_containing_a_NEWLINE_stages_that_path(repo):
    """The batch form must carry paths as argv, not through `update-index --index-info`.

    `--index-info` is the natural way to write many index entries at once, and it is the wrong one
    here. Its records are newline-terminated, and `validate_paths` refuses only NUL — as it must,
    since a name has to match a real status row, and a newline is legal in a Linux filename.
    Measured: feeding one through that format dies with `fatal: malformed index info`, and the
    entry is simply not staged.

    (A TAB is fine in both forms — `--index-info` takes everything after the FIRST tab as the
    path — so the newline is the case that actually distinguishes them, and this test says so
    rather than pinning a character that proves nothing.)

    Repeated `--cacheinfo` passes the path as its own argument, where no delimiter exists to
    confuse. This test is what stops the batch being "simplified" back.
    """
    name = "line\nbreak.txt"
    (repo / name).write_text("across a newline\n")
    fps = _fps(repo, [name])
    gitwrite.git_stage(str(repo), [name], True, fps)

    staged = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--stage", "-z"], capture_output=True, text=True
    ).stdout
    assert f"\t{name}\0" in staged + "\0", f"the newline path is not in the index: {staged!r}"
    body = subprocess.run(
        ["git", "-C", str(repo), "show", f":{name}"], capture_output=True, text=True
    ).stdout
    assert body == "across a newline\n"


def test_staging_many_files_is_not_one_git_call_per_file(repo):
    """A guarantee that costs a hundredfold slowdown under the repository lock is not free.

    Hashing and installing per path measured 1.24s for 200 files against 0.01s for a plain
    `git add`. The binding does not require per-path invocations — `hash-object` takes many paths
    and prints their ids in order, and `--cacheinfo` repeats — so this pins the call count rather
    than a wall-clock number, which would be flaky on a loaded runner.
    """
    names = []
    for i in range(60):
        n = f"f{i:03d}.txt"
        (repo / n).write_text(f"content {i}\n")
        names.append(n)
    fps = _fps(repo, names)

    real = gitwrite.run_git_write
    calls: list[str] = []

    def count(r, args, **kw):
        if args:
            calls.append(args[0])
        return real(r, args, **kw)

    monkeypatch_calls = calls
    import unittest.mock as _mock

    with _mock.patch.object(gitwrite, "run_git_write", count):
        gitwrite.git_stage(str(repo), names, True, fps)

    hashing = monkeypatch_calls.count("hash-object")
    installing = monkeypatch_calls.count("update-index")
    assert hashing <= 2, f"{hashing} hash-object calls for {len(names)} files"
    assert installing <= 2, f"{installing} update-index calls for {len(names)} files"
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True
    ).stdout.split()
    assert sorted(staged) == sorted(names), "the batch did not stage every file"


def test_unstaging_many_files_is_not_one_git_call_per_file(repo):
    """Unstage looks each path up in HEAD; that lookup batches like every other.

    Measured at one `ls-tree` per path: 0.66s for 200 files, under the repository lock. What makes
    unstaging safe is that HEAD's objects are immutable, not the number of round trips used to
    find them, so batching costs the guarantee nothing.
    """
    names = []
    for i in range(60):
        n = f"f{i:03d}.txt"
        (repo / n).write_text(f"content {i}\n")
        names.append(n)
    _git(repo, "add", *names)
    _git(repo, "commit", "-qm", "committed once")
    for i, n in enumerate(names):
        (repo / n).write_text(f"changed {i}\n")
    _git(repo, "add", *names)
    fps = _fps(repo, names)

    real = gitwrite.run_git_write
    calls: list[str] = []

    def count(r, args, **kw):
        if args:
            calls.append(args[0])
        return real(r, args, **kw)

    import unittest.mock as _mock

    with _mock.patch.object(gitwrite, "run_git_write", count):
        gitwrite.git_stage(str(repo), names, False, fps)

    assert calls.count("ls-tree") <= 2, f"{calls.count('ls-tree')} ls-tree calls for {len(names)}"
    assert calls.count("update-index") <= 2, f"{calls.count('update-index')} update-index calls"
    still = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True
    ).stdout.split()
    assert still == [], f"files are still staged: {still[:3]}"


def test_a_refused_CREATE_switch_leaves_no_branch_behind(repo, monkeypatch):
    """The compensating undo must undo the branch too, not just the checkout.

    `switch -c` does two things — it creates a branch and it moves onto it — so going back to the
    original branch only reverses half of them. A refused create that left the new branch sitting
    in the list would be a side effect of an operation the panel reported as having not happened,
    and the operator would have to clean it up by hand.

    It is deleted with `-d`, never `-D`: the branch points at the commit it was made from and so
    is fully merged. If git disagrees, something else has happened and the branch stays.
    """
    dirty_fp = _dirty_fp(repo)
    real = gitwrite.run_git_write
    fired: list[int] = []

    def dirty_at_the_boundary(r, args, **kw):
        out = real(r, args, **kw)
        if args and args[0] == "switch" and "--create" in args and not fired:
            fired.append(1)
            (repo / "a.txt").write_text("WRITTEN WHILE CREATING\n")
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", dirty_at_the_boundary)
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "brand-new", True, None, dirty_fp)
    assert e.value.status == 409
    assert fired, "the boundary was never reached — the test proves nothing"

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "master", f"left the operator on {head!r} after refusing"
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "brand-new" not in branches, "a refused create left its branch behind"
    assert (repo / "a.txt").read_text() == "WRITTEN WHILE CREATING\n", "the late edit was lost"


def test_a_rewrite_installed_AFTER_the_preflight_cannot_redirect_the_fetch(
    repo, root, monkeypatch, local_transport
):
    """`refuse_url_rewrites` is a preflight, and a preflight loses to a writer.

    `url.<base>.insteadOf` rewrites a URL inside git, including one given on the argv. Scanning
    the config first and then invoking git leaves a window: a rewrite installed in it still
    applies, and `GIT_ALLOW_PROTOCOL` does not stop a same-scheme redirect — so the pinned
    request goes somewhere else with the original host's TLS/DNS pins still attached.

    The window is not narrowed here, it is removed: the network command runs against a scratch
    `$GIT_DIR` holding a config this module wrote, so `url.*` in the repository's own config is
    in a file git never opens. Measured both ways — an ordinary fetch follows such a rewrite, and
    this one does not.
    """
    real_remote = root / "real.git"
    evil_remote = root / "evil.git"
    for bare in (real_remote, evil_remote):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", f"file://{real_remote}")
    # A commit that exists ONLY on the attacker's remote, so its arrival is unambiguous.
    decoy = root / "decoy"
    subprocess.run(["git", "clone", "-q", str(evil_remote), str(decoy)], check=True)
    _git(decoy, "config", "user.email", "e@e")
    _git(decoy, "config", "user.name", "e")
    (decoy / "pwned.txt").write_text("attacker content\n")
    _git(decoy, "add", "pwned.txt")
    _git(decoy, "commit", "-qm", "ATTACKER COMMIT")
    _git(decoy, "push", "-q", "origin", "HEAD:refs/heads/master")

    real = gitwrite.run_git_net
    fired: list[int] = []

    def rewrite_after_the_preflight(r, gitdir, args, **kw):
        # Installed at the last possible moment: the preflight has already scanned and passed.
        if args and args[0] == "fetch" and not fired:
            fired.append(1)
            _git(
                repo,
                "config",
                f"url.file://{evil_remote}.insteadOf",
                f"file://{real_remote}",
            )
        return real(r, gitdir, args, **kw)

    monkeypatch.setattr(gitwrite, "run_git_net", rewrite_after_the_preflight)
    try:
        gitwrite.git_fetch(str(repo), "origin")
    except (FsError, gitwrite.GitError):
        pass  # a refusal is fine; fetching the attacker's commit is not
    assert fired, "the boundary was never reached — the test proves nothing"

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--all", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "ATTACKER COMMIT" not in log, "the late rewrite redirected the fetch"


def test_push_settlement_never_rewinds_a_tracking_ref_that_moved(repo, remote_repo, monkeypatch):
    """The expected-old side of a swap has to be a SNAPSHOT, not a fresh read.

    Settlement read `refs/remotes/<remote>/<branch>` after the push and handed that value to
    `update-ref` as the expected-old. That is not a compare-and-swap — it is "whatever is there
    now, make it what I want", which succeeds precisely when it should refuse. A probe that
    advanced the tracking ref during the push had it rewound by the bookkeeping.

    Read before the network call, the swap refuses and leaves the newer value alone.
    """
    # A commit that is NOT the one being pushed — otherwise "rewound to want_oid" and "left
    # alone" are the same value and the assertion cannot tell them apart. (The first version of
    # this test made exactly that mistake and passed against the unfixed code.) `commit-tree`
    # mints it without moving any branch, so the push's own expectation still holds.
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True
    ).stdout.strip()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    ahead = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", head],
        input="a concurrent fetch landed this\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ahead and ahead != head, "the fixture did not mint a distinct commit"
    real = gitwrite.run_git_net
    fired: list[int] = []

    def advance_tracking_during_push(r, gitdir, args, **kw):
        out = real(r, gitdir, args, **kw)
        if args and args[0] == "push" and not fired:
            fired.append(1)
            # A concurrent fetch lands a newer tracking value while we are on the network.
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", ahead],
                check=True,
            )
        return out

    monkeypatch.setattr(gitwrite, "run_git_net", advance_tracking_during_push)
    out = gitwrite.git_push(str(repo), expect=_expect(repo))
    assert fired, "the boundary was never reached — the test proves nothing"
    assert out["pushed"], "the remote update is not reported"
    now = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert now == ahead, "settlement rewound a tracking ref that had moved on"


def test_a_stage_landing_MID_DISCARD_check_refuses_rather_than_restoring_it(repo, monkeypatch):
    """Discard must put back the version that was on screen, not merely *a* version.

    `verify_rows` binds the worktree bytes the operator confirmed. It says nothing about which
    blob the INDEX holds, and the index is what discard restores from — so a stage landing between
    the row check and the index read swapped the replacement for content that was never displayed.
    It looks harmless because what lands is a real version of the file, which is exactly why it
    needs pinning rather than eyeballing.
    """
    (repo / "a.txt").write_text("committed\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("work in progress\n")
    fps = _fps(repo, ["a.txt"])

    real = gitwrite.verify_rows
    fired: list[int] = []

    def stage_a_different_version(r, want, verb):
        out = real(r, want, verb)
        if not fired:
            fired.append(1)
            # A different blob becomes the index version, after the row was verified.
            (repo / "a.txt").write_text("A THIRD VERSION\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            (repo / "a.txt").write_text("work in progress\n")
        return out

    monkeypatch.setattr(gitwrite, "verify_rows", stage_a_different_version)
    with pytest.raises(FsError) as e:
        gitwrite.git_discard(str(repo), ["a.txt"], fps)
    assert e.value.status == 409
    assert fired, "the boundary was never reached — the test proves nothing"
    assert (repo / "a.txt").read_text() == "work in progress\n", "it discarded anyway"


def test_a_failed_switch_undo_is_reported_not_claimed_as_restored(repo, monkeypatch):
    """The compensation must be believed only when it worked.

    `_undo_switch` swallowed a failed switch-back while the caller said unconditionally that it
    "went back" — so a reproduced late-edit case left HEAD on the target branch, with the edit
    there, and handed the operator recovery advice for a state they were not in. Wrong location
    plus confident wording is worse than the original failure.
    """
    dirty_fp = _dirty_fp(repo)
    real = gitwrite.run_git_write
    state = {"switched": 0}

    def dirty_then_block_the_way_back(r, args, **kw):
        if args and args[0] == "switch":
            state["switched"] += 1
            if state["switched"] == 2:
                # The compensating switch-back itself fails.
                raise gitwrite.GitError("switch back refused")
        out = real(r, args, **kw)
        if args and args[0] == "switch" and state["switched"] == 1:
            (repo / "a.txt").write_text("WRITTEN WHILE SWITCHING\n")
        return out

    monkeypatch.setattr(gitwrite, "run_git_write", dirty_then_block_the_way_back)
    with pytest.raises(FsError) as e:
        gitwrite.git_switch(str(repo), "other", None, None, dirty_fp)
    msg = str(e.value)
    assert e.value.status == 409
    assert state["switched"] >= 2, "the undo was never attempted — the test proves nothing"
    assert "did not work" in msg, f"the failure was swallowed: {msg}"
    assert "went back to" not in msg, f"it claimed a restoration that did not happen: {msg}"


def test_the_FIRST_commit_in_a_fresh_repository_works(root):
    """An unborn branch has no tip, and committing onto one is the ordinary first commit.

    Building the commit object instead of asking `git commit` for one means resolving the branch
    tip to use as the parent — and a fresh repository has none. Resolving it unconditionally broke
    the very first commit with `fatal: Needed a single revision`, and nothing caught it because no
    test had ever committed into an empty repository. A root commit simply takes no `-p`.
    """
    fresh = root / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q")
    _git(fresh, "config", "user.email", "t@t")
    _git(fresh, "config", "user.name", "t")
    (fresh / "first.txt").write_text("first commit ever\n")
    _git(fresh, "add", "first.txt")

    out = gitwrite.git_commit(str(fresh), "the very first commit", _staged_fp(fresh))
    assert out["commit"], "no commit was reported"

    log = subprocess.run(
        ["git", "-C", str(fresh), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "the very first commit" in log
    parents = subprocess.run(
        ["git", "-C", str(fresh), "rev-list", "--parents", "-n", "1", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(parents) == 1, f"the root commit was given a parent: {parents}"
    files = subprocess.run(
        ["git", "-C", str(fresh), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert files == ["first.txt"]


def test_network_writes_work_inside_a_LINKED_WORKTREE(root, monkeypatch):
    """A linked worktree keeps HEAD and the index of its own, and shares objects and refs.

    That distinction is why `Repo.common()` exists, and the scratch-gitdir network path has to
    respect it: `GIT_OBJECT_DIRECTORY` must name the SHARED object store. Pointed at
    `<gitdir>/objects` — which does not exist for a linked worktree — git rejects the scratch
    directory outright (`fatal: not a git repository`), so fetch, pull and push are broken for
    anyone working in a worktree at all. Measured against the pre-fix code, not reasoned about.
    """
    monkeypatch.setattr(gitwrite, "GIT_ALLOW_PROTOCOL", gitwrite.GIT_ALLOW_PROTOCOL + ":file")
    main = root / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "a.txt").write_text("one\n")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-qm", "init")
    bare = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(main, "remote", "add", "origin", f"file://{bare}")
    _git(main, "push", "-q", "-u", "origin", "master")
    wt = root / "wt"
    _git(main, "worktree", "add", "-q", "-b", "side", str(wt))

    gitwrite.git_fetch(str(wt), "origin")

    oid = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "refs/remotes/origin/master"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert oid, "the tracking ref did not land in the shared store"
    kind = subprocess.run(
        ["git", "-C", str(wt), "cat-file", "-t", oid], capture_output=True, text=True
    ).stdout.strip()
    assert kind == "commit", f"the fetched object is not readable from the worktree: {kind!r}"
    per_worktree = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-dir"], capture_output=True, text=True
    ).stdout.strip()
    assert not os.path.isdir(
        os.path.join(per_worktree, "objects")
    ), "a second object store was built beside the real one"
