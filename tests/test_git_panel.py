"""GIT-tab backend tests (#784).

The interesting ones are adversarial and were written against measured behaviour: repo-configured
programs that a "read-only" subcommand will happily execute, metadata that reaches outside the root
through four different indirections, and a parser fed the filenames real repositories actually have.
Everything runs against throwaway repos under ``AGENT_SESSIONS_FS_ROOT`` — the real ``~`` is never
touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, gitpanel


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
    (p / "a.txt").write_text("one\ntwo\n")
    _git(p, "add", "a.txt")
    _git(p, "commit", "-qm", "init")
    return p


# --------------------------------------------------------------------------- helper sentinels


def _marker(repo, name):
    """A script that records the fact it ran. If a 'read-only' command executes it, we have lost."""
    script = repo / f"{name}.sh"
    hit = repo / f"{name}.ran"
    script.write_text(f'#!/bin/sh\necho ran > "{hit}"\ncat "$1" 2>/dev/null || cat\n')
    script.chmod(0o755)
    return script, hit


@pytest.mark.parametrize("driver", ["clean", "process"])
@pytest.mark.parametrize("attr_source", ["gitattributes", "info_attributes"])
def test_no_repo_configured_helper_ever_runs(repo, driver, attr_source):
    """textconv, external diff, fsmonitor AND a conversion filter — none may execute.

    Two earlier versions of this test passed against code that DID run the filter, and both
    failure modes are baked in here deliberately:

    * committing `.gitattributes` *after* the baseline (so the attribute never applied), and
    * changing the file's LENGTH, which lets git decide the file differs from stat alone and
      never hash it — the hash is where conversion happens.

    The third, found in review: only `.gitattributes` was covered. `$GIT_DIR/info/attributes`
    binds the same driver, is not a tracked file, and `--attr-source` does not redirect it — so
    the fix cannot live at the binding site. Both sources are parametrized, and so are both
    driver forms (`process` takes precedence over `clean` when both exist, so testing them
    together tests only one of them).
    """
    tc, tc_hit = _marker(repo, "textconv")
    ext, ext_hit = _marker(repo, "extdiff")
    fsm, fsm_hit = _marker(repo, "fsmonitor")
    conv, conv_hit = _marker(repo, "conv")

    attr_line = "a.txt diff=mytc filter=evil\n"
    if attr_source == "gitattributes":
        # Committed FIRST so it is in force for the tracked file.
        (repo / ".gitattributes").write_text(attr_line)
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-qm", "attrs")
    else:
        info = repo / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text(attr_line)

    _git(repo, "config", "diff.mytc.textconv", str(tc))
    _git(repo, "config", "diff.external", str(ext))
    _git(repo, "config", "core.fsmonitor", str(fsm))
    _git(repo, "config", f"filter.evil.{driver}", str(conv))

    # SAME LENGTH ("two" -> "TWO"), so git cannot shortcut on size and must hash the worktree
    # file — which is where the filter would run.
    (repo / "a.txt").write_text("one\nTWO\n")
    os.utime(repo / "a.txt", (2_000_000_000, 2_000_000_000))
    for hit in (tc_hit, ext_hit, fsm_hit, conv_hit):
        hit.unlink(missing_ok=True)

    gitpanel.git_status(str(repo))
    gitpanel.git_diff(str(repo / "a.txt"), staged=False)

    for hit, what in (
        (tc_hit, "textconv"),
        (ext_hit, "external diff"),
        (fsm_hit, "fsmonitor"),
        (conv_hit, f"{driver} filter (bound via {attr_source})"),
    ):
        assert not hit.exists(), f"the repository's {what} program was executed"


def test_git_never_reads_the_repository_config(repo, tmp_path):
    """The mechanism, pinned directly: git runs against a gitdir this module assembles.

    An earlier fix enumerated the repo's conversion drivers and disabled each by name. It worked,
    but it consulted repository-controlled config — which meant an enumerate-then-use race and an
    unbounded preflight. Nothing is enumerated now, so the assertion is stronger and simpler:
    config the repository writes has no effect at all.
    """
    outside = tmp_path / "marker"
    _git(repo, "config", "core.fsmonitor", "/bin/false")
    _git(repo, "config", "filter.evil.clean", "/bin/false")
    _git(repo, "config", "user.name", str(outside))

    r = gitpanel.discover_repo(str(repo))
    with gitpanel.sanitized_gitdir(r) as (gitdir, _branch):
        # `info/` is empty, so `info/attributes` cannot bind a path to a driver...
        assert os.listdir(os.path.join(gitdir, "info")) == []
        # ...and the config is the one written here, carrying no driver of any kind.
        cfg = open(os.path.join(gitdir, "config")).read()
        assert "filter." not in cfg
        assert "fsmonitor" not in cfg
        assert str(outside) not in cfg
        # The object store is shared by reference rather than copied.
        assert os.path.islink(os.path.join(gitdir, "objects"))
        # Writes during a refresh land on the copy, never the repository.
        assert os.path.realpath(os.path.join(gitdir, "index")) != os.path.realpath(
            os.path.join(repo, ".git", "index")
        )


def test_a_driver_written_mid_request_still_cannot_run(repo):
    """The race the previous design could not close.

    Enumerating drivers and disabling them by name leaves a gap: a config write landing between
    the enumeration and the command introduces a driver the generated flags never covered. Here
    the write happens *after* discovery and *during* the request, and it still cannot matter —
    the config it lands in is never opened.
    """
    conv, conv_hit = _marker(repo, "conv")
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("a.txt filter=evil\n")
    (repo / "a.txt").write_text("one\nTWO\n")
    os.utime(repo / "a.txt", (2_000_000_000, 2_000_000_000))

    real = gitpanel.sanitized_gitdir

    @contextmanager
    def racing(r):
        with real(r) as prepared:
            # Written after the metadata snapshot exists, before git is spawned.
            _git(repo, "config", "filter.evil.clean", str(conv))
            _git(repo, "config", "filter.evil.required", "true")
            yield prepared

    conv_hit.unlink(missing_ok=True)
    with mock.patch.object(gitpanel, "sanitized_gitdir", racing):
        status = gitpanel.git_status(str(repo))

    assert not conv_hit.exists(), "a driver written mid-request executed"
    assert any(e["path"] == "a.txt" for e in status["entries"])


def test_required_filter_does_not_break_status(repo):
    """Emptying a driver on a `required` filter is a hard failure, not a no-op.

    Measured: `-c filter.evil.clean=` alone gives `fatal: a.txt: clean filter 'evil' failed`,
    exit 128. git-lfs sets `filter.lfs.required = true` in the operator's global config, so the
    first version of this fix would have turned every LFS repo into an error state — a regression
    introduced *by* the security fix, which is exactly the kind that ships unnoticed.
    """
    conv, conv_hit = _marker(repo, "conv")
    # Bound via `.git/info/attributes`, NOT `.gitattributes`: `--attr-source` already redirects
    # the latter, so the driver is never consulted and the `required` flag never bites. The first
    # version of this test used `.gitattributes` and passed against the unfixed code.
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("a.txt filter=evil\n")
    _git(repo, "config", "filter.evil.clean", str(conv))
    _git(repo, "config", "filter.evil.required", "true")
    (repo / "a.txt").write_text("one\nTWO\n")
    os.utime(repo / "a.txt", (2_000_000_000, 2_000_000_000))
    conv_hit.unlink(missing_ok=True)

    status = gitpanel.git_status(str(repo))

    assert not conv_hit.exists(), "the required filter still ran"
    assert any(e["path"] == "a.txt" for e in status["entries"]), "the change went unreported"


# --------------------------------------------------------------------------- child liveness


def _fake_git(tmp_path, name, body):
    """A stand-in for the git binary. `config --list` short-circuits so `_neutralising_flags`
    (which runs first) does not itself trip the behaviour under test."""
    exe = tmp_path / name
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time, os\n"
        "if 'config' in sys.argv[1:]:\n"
        "    sys.exit(0)\n" + body
    )
    exe.chmod(0o755)
    return str(exe)


def test_silent_child_hits_the_deadline(repo, tmp_path, monkeypatch):
    """A child that produces nothing must still be killed at the deadline.

    The bug this pins: the timeout was checked *around* `proc.stdout.read()`, which blocks until
    EOF. Measured against the old code, a child sleeping 1s returned successfully under a 50ms
    budget — the deadline could not fire, because nothing came back to check it between.
    """
    monkeypatch.setattr(gitpanel, "git_bin", lambda: _fake_git(tmp_path, "git", "time.sleep(30)\n"))
    monkeypatch.setattr(gitpanel, "GIT_TIMEOUT_S", 0.5)
    started = time.monotonic()
    with pytest.raises(gitpanel.GitError) as e:
        gitpanel._run_git(str(repo), ["status"], cwd=str(repo))
    elapsed = time.monotonic() - started
    assert e.value.status == 504
    # Generous, but far below the child's 30s: the point is that the deadline fired at all.
    assert elapsed < 10, f"the deadline did not interrupt a silent child ({elapsed:.1f}s)"


def test_stderr_flood_does_not_deadlock(repo, tmp_path, monkeypatch):
    """Draining stdout to EOF before touching stderr deadlocks on a chatty child.

    stderr's pipe buffer is ~64 KiB. A child that writes past it blocks; a parent blocked on
    stdout never drains it; neither side moves again. So both pipes are selected over together.

    The assertion runs on a worker thread: on a regression this call never returns, and a plain
    call would hang the whole suite rather than fail it.
    """
    body = (
        "sys.stderr.write('x' * (4 * 1024 * 1024))\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('# branch.head main\\n')\n"
    )
    monkeypatch.setattr(gitpanel, "git_bin", lambda: _fake_git(tmp_path, "git", body))
    monkeypatch.setattr(gitpanel, "GIT_TIMEOUT_S", 20.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(gitpanel._run_git, str(repo), ["status"], cwd=str(repo))
        try:
            out = fut.result(timeout=30)
        except FuturesTimeout:
            pytest.fail("stdout-first draining deadlocked against a child flooding stderr")
    assert b"branch.head" in out


def test_only_read_only_subcommands_are_invoked(repo, monkeypatch):
    """No `git diff`, ever — and nothing that writes."""
    seen: list[list[str]] = []
    real = gitpanel._run_git

    def spy(root, args, *, cwd, **kw):
        seen.append(args)
        return real(root, args, cwd=cwd, **kw)

    monkeypatch.setattr(gitpanel, "_run_git", spy)
    (repo / "a.txt").write_text("one\nCHANGED\n")
    gitpanel.git_status(str(repo))
    gitpanel.git_diff(str(repo / "a.txt"), staged=False)

    subcommands = {a[0] for a in seen}
    assert subcommands <= {"rev-parse", "status", "cat-file"}, subcommands
    assert "diff" not in subcommands


# --------------------------------------------------------------------------- diff fidelity


def test_invalid_utf8_worktree_file_is_reported_binary(repo):
    """`read_file`'s display decoding cannot be undone, so the diff path reads raw bytes.

    `read_file` decodes with `errors="replace"`. Feeding its *string* back through `.encode()`
    always yields valid UTF-8 — U+FFFD is a legal character — so the strict `_decodable()` check
    downstream could never fail, and a file full of undecodable bytes came back as a diff full of
    replacement characters presented as if it were the file's content.

    No NUL byte here on purpose: a NUL would be caught by the binary sniff and never reach the
    decode. `\xff\xfe` is simply not valid UTF-8.
    """
    (repo / "a.txt").write_bytes(b"one\n\xff\xfe bad\n")
    out = gitpanel.git_diff(str(repo / "a.txt"), staged=False)
    assert out["binary"] is True, "undecodable bytes were rendered as text"
    assert out["diff"] == ""
    assert "\ufffd" not in out["diff"]


def test_conflict_diff_compares_ours_against_theirs(repo):
    """A conflict's DIFF is ours-vs-theirs, from the stage-2/stage-3 blobs.

    Dropping those OIDs left the comparison running against two empty blobs, so the panel showed
    the merge markers as if they were the change — the one rendering of a conflict that carries no
    information about what actually disagrees.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("one\nTHEIRS\n")
    _git(repo, "commit", "-qam", "theirs")
    _git(repo, "checkout", "-q", "-")
    (repo / "a.txt").write_text("one\nOURS\n")
    _git(repo, "commit", "-qam", "ours")
    subprocess.run(  # conflicts, so a non-zero exit is the expected outcome
        ["git", "merge", "side"], cwd=repo, capture_output=True, check=False
    )

    status = gitpanel.git_status(str(repo))
    row = next(e for e in status["entries"] if e["kind"] == "unmerged")
    assert row["oid_ours"] and row["oid_theirs"]
    assert row["oid_ours"] != row["oid_theirs"]

    out = gitpanel.git_diff(str(repo / "a.txt"), staged=False)
    assert out["conflict"] is True
    assert "-OURS" in out["diff"], out["diff"]
    assert "+THEIRS" in out["diff"], out["diff"]
    # The worktree copy is markers + both sides; diffing it would describe the markers.
    assert "<<<<<<<" not in out["diff"]


def _git_diff_body(repo, rel, *args):
    """Native git's own hunk text, for equality rather than a hand-written expectation."""
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", *args, "--", rel],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.split("@@", 1)[-1].strip()


@pytest.mark.parametrize(
    ("before", "after"),
    [(b"a\n", b"a"), (b"a", b"a\n")],
    ids=["remove-final-newline", "add-final-newline"],
)
def test_final_newline_change_matches_git(root, before, after):
    """The terminator is part of the line, and its loss is not cosmetic.

    Splitting on "\n" and dropping the trailing empty element made `b"a\n"` and `b"a"` compare as
    the same single line, so the diff was either empty or (with the empty element kept) a phantom
    blank-line deletion at `@@ -1,2 +1 @@`. Git reports `-a` / `+a` / the no-newline marker. The
    assertion is equality with git's own output rather than a transcription of it.
    """
    repo = root / "nl"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_bytes(before)
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.txt").write_bytes(after)

    out = gitpanel.git_diff(str(repo / "a.txt"), staged=False)

    assert gitpanel.NO_NEWLINE_MARKER in out["diff"]
    assert out["added"] == 1 and out["removed"] == 1
    body = out["diff"].split("@@", 1)[-1].strip()
    assert body == _git_diff_body(repo, "a.txt")


def test_upstream_divergence_survives_the_sanitized_gitdir(root):
    """The sanitized config carries the upstream ref names, so ahead/behind still works.

    Not copying repository config is the point of the design, but `branch.<n>.remote` / `.merge`
    are ref *names* rather than commands — they are re-emitted into the config this module writes
    (after a shape check), which is what keeps the divergence counters real instead of null.
    """
    repo = root / "up"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    bare = root / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-qam", "ahead")

    status = gitpanel.git_status(str(repo))

    assert status["branch"]
    assert status["upstream"] and status["upstream"].startswith("origin/")
    assert status["ahead"] == 1
    assert status["behind"] == 0


def test_linked_worktree_reads_the_shared_object_store(root):
    """A linked worktree keeps HEAD and the index in its own gitdir while objects and refs live in
    the commondir — so the snapshot has to draw from both."""
    repo = root / "wtmain"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    wt = root / "wtlinked"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    (wt / "b.txt").write_text("new\n")

    status = gitpanel.git_status(str(wt))

    assert status["branch"] == "side"
    assert any(e["path"] == "b.txt" for e in status["entries"])


def _old_git_body(real):
    """A git that rejects `--attr-source` the way 2.39 does, then execs the real one."""
    return (
        "args = sys.argv[1:]\n"
        "if any(a.startswith('--attr-source') for a in args):\n"
        "    sys.stderr.write('error: unknown option `attr-source=...`\\n')\n"
        "    sys.exit(129)\n"
        f"os.execv({real!r}, [{real!r}] + args)\n"
    )


def test_byte_capped_diff_truncates_on_a_line_boundary(root):
    """A blind slice at the byte cap cuts a line in half, and half a line renders as a whole one.

    The reader has no way to see that: the truncation flag says the diff is incomplete, not that
    the last visible line is a fragment of its real content. So the cut lands on a newline.
    """
    repo = root / "big"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    old = "\n".join("x" * 90 for _ in range(9000)) + "\n"
    new_text = "\n".join("y" * 90 for _ in range(9000)) + "\n"
    (repo / "a.txt").write_text(old)
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.txt").write_text(new_text)

    out = gitpanel.git_diff(str(repo / "a.txt"), staged=False)

    assert out["truncated"] is True
    # Counts from a truncated prefix are not totals, so they are withheld.
    assert out["added"] is None and out["removed"] is None
    body = [
        ln
        for ln in out["diff"].splitlines()
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]
    assert body, "nothing was emitted"
    # Every content line is a WHOLE line: one prefix character plus the full 90-char payload.
    assert all(len(ln) == 91 for ln in body), f"last line is a fragment: {body[-1]!r}"


def test_old_git_without_attr_source_still_works(repo, tmp_path, monkeypatch):
    """`--attr-source` is defence in depth now, not the mechanism — so an old git is supported.

    An earlier version failed CLOSED on git < 2.40, because safety genuinely depended on the flag.
    Once the sanitized gitdir removed that dependency, refusing to run became a hard error for no
    security gain. The flag is dropped after the first refusal and not offered again.
    """
    gitpanel.reset_attr_source_for_test()
    real = gitpanel.git_bin()
    body = _old_git_body(real)
    fake = _fake_git(tmp_path, "git", body)
    monkeypatch.setattr(gitpanel, "git_bin", lambda: fake)
    try:
        status = gitpanel.git_status(str(repo))
        assert status["repo"] == str(repo)
        assert gitpanel._ATTR_SOURCE is False, "the unsupported flag was offered again"
    finally:
        gitpanel.reset_attr_source_for_test()


def test_old_git_still_cannot_run_a_repository_filter(repo, tmp_path, monkeypatch):
    """And dropping the flag does not reopen the hole it used to cover.

    This is the assertion that makes the fallback safe rather than merely convenient: with
    `--attr-source` gone, a `.gitattributes` binding is live again — but the sanitized gitdir
    defines no driver for it to name, so there is nothing to run.
    """
    gitpanel.reset_attr_source_for_test()
    conv, conv_hit = _marker(repo, "conv")
    (repo / ".gitattributes").write_text("a.txt filter=evil\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    _git(repo, "config", "filter.evil.clean", str(conv))
    (repo / "a.txt").write_text("one\nTWO\n")
    os.utime(repo / "a.txt", (2_000_000_000, 2_000_000_000))
    conv_hit.unlink(missing_ok=True)

    real = gitpanel.git_bin()
    body = _old_git_body(real)
    monkeypatch.setattr(gitpanel, "git_bin", lambda: _fake_git(tmp_path, "git", body))
    try:
        status = gitpanel.git_status(str(repo))
        assert not conv_hit.exists(), "the repository's filter ran once --attr-source was dropped"
        assert any(e["path"] == "a.txt" for e in status["entries"])
    finally:
        gitpanel.reset_attr_source_for_test()


# --------------------------------------------------------------------------- containment


def test_gitdir_file_pointing_outside_is_refused(root, tmp_path):
    """`GIT_CEILING_DIRECTORIES` does NOT stop this: the ceiling bounds upward discovery, not where
    a `.git` file points. Measured — `--show-toplevel` stayed contained while the gitdir was not."""
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = root / "linked"
    proj.mkdir()
    _git(proj, "init", "-q", f"--separate-git-dir={outside}/gitdir")
    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(proj))
    assert e.value.status == 403


@pytest.mark.parametrize("absolute", [True, False])
def test_config_include_pointing_outside_is_refused(repo, root, tmp_path, absolute):
    """A contained `.git/config` can pull in an outside file via `include.path`, and git honours
    it — measured: an outside file's `user.name` came back from inside the contained repo."""
    outside = tmp_path / "evil.cfg"
    outside.write_text("[user]\n\tname = pwned\n")
    target = str(outside) if absolute else os.path.relpath(outside, repo / ".git")
    with open(repo / ".git" / "config", "a") as fh:
        fh.write(f"[include]\n\tpath = {target}\n")
    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(repo))
    assert e.value.status == 403


def test_worktree_alternate_pointing_outside_is_refused(repo, root, tmp_path):
    """A linked worktree's own gitdir has no `objects` and no `config` — those live in the
    commondir. Validating only the gitdir therefore passed vacuously for every linked worktree,
    which is the one case where the check had to look somewhere else to mean anything."""
    donor = tmp_path / "donor.git"
    (donor / "objects").mkdir(parents=True)
    wt = root / "linkedwt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{donor}/objects\n")

    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(wt))
    assert e.value.status == 403


def test_teardown_never_follows_the_object_symlink(repo):
    """The snapshot shares the object store by REFERENCE, so its teardown deletes a symlink that
    points into the live repository. `shutil.rmtree` does not follow it — verified rather than
    assumed, because the failure mode is destroying the repository's objects."""
    r = gitpanel.discover_repo(str(repo))
    with gitpanel.sanitized_gitdir(r) as (gitdir, _branch):
        assert os.path.islink(os.path.join(gitdir, "objects"))
    assert not os.path.exists(gitdir)
    assert os.path.isdir(repo / ".git" / "objects")
    # And the repository is still readable afterwards.
    assert gitpanel.git_status(str(repo))["repo"] == str(repo)


@pytest.mark.parametrize("child", ["objects", "refs"])
def test_symlinked_metadata_directory_is_refused(repo, tmp_path, child):
    """A contained gitdir says nothing about where its CHILDREN resolve.

    `.git/objects` can be a symlink to an external object store; the snapshot linked straight
    through to it and `cat-file` returned blobs from outside the root — a working DIFF over files
    the browser is not allowed to see. Same class of escape as `objects/info/alternates`, which
    was already refused; the same door with a different handle.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = repo / ".git" / child
    shutil.move(str(src), str(outside / child))
    os.symlink(outside / child, src)
    (repo / "a.txt").write_text("one\nTWO\n")

    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(repo))
    assert e.value.status == 403


@pytest.mark.parametrize("child", ["HEAD", "index"])
def test_symlinked_metadata_file_is_refused(repo, tmp_path, child):
    """Metadata FILES are taken through `O_NOFOLLOW`, so a link is refused at the syscall.

    Skipping silently would be worse than refusing for the index in particular: git would then
    compare the worktree against an empty index and report a plausible but wrong set of changes
    (measured — one path listed twice, as both a staged delete and an addition).
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = repo / ".git" / child
    shutil.move(str(src), str(outside / child))
    os.symlink(outside / child, src)

    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(repo))
    assert e.value.status == 400


def test_symlinked_packed_refs_is_not_followed(repo, tmp_path):
    """`packed-refs` is the ref store once refs are packed, so following a link to one outside the
    root would resolve HEAD against foreign refs."""
    _git(repo, "pack-refs", "--all")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = repo / ".git" / "packed-refs"
    shutil.move(str(src), str(outside / "packed-refs"))
    os.symlink(outside / "packed-refs", src)

    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(repo))
    assert e.value.status == 400


def test_symlinked_config_is_not_honoured(repo, tmp_path):
    """A symlinked `config` is declined rather than read, so nothing it names takes effect.

    Status still works — config is not required to read a working tree — but the upstream it
    declares is absent rather than honoured, which is the observable half of "not followed".
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = repo / ".git" / "config"
    shutil.move(str(src), str(outside / "config"))
    (outside / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n[branch "master"]\n'
        "\tremote = origin\n\tmerge = refs/heads/master\n"
    )
    os.symlink(outside / "config", src)
    (repo / "a.txt").write_text("one\nTWO\n")

    status = gitpanel.git_status(str(repo))

    assert status["upstream"] is None
    assert any(e["path"] == "a.txt" for e in status["entries"])


def test_object_alternate_pointing_outside_is_refused(repo, tmp_path):
    """The gitdir itself is in-root here — `--absolute-git-dir` alone would pass."""
    donor = tmp_path / "donor.git"
    donor.mkdir()
    (donor / "objects").mkdir()
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{donor}/objects\n")
    with pytest.raises(gitpanel.GitError) as e:
        gitpanel.git_status(str(repo))
    assert e.value.status == 403


def test_ordinary_in_root_indirection_still_works(root):
    """The refusals above must not break a normal separate-gitdir layout INSIDE the root."""
    proj = root / "ok"
    proj.mkdir()
    (root / "gitdirs").mkdir()
    _git(proj, "init", "-q", f"--separate-git-dir={root}/gitdirs/ok")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    (proj / "f.txt").write_text("x\n")
    _git(proj, "add", "f.txt")
    _git(proj, "commit", "-qm", "c")
    st = gitpanel.git_status(str(proj))
    assert st["repo"] == os.path.realpath(str(proj))


def test_not_a_repository_is_a_state_not_an_error(root):
    plain = root / "plain"
    plain.mkdir()
    st = gitpanel.git_status(str(plain))
    assert st["repo"] is None
    assert st["entries"] == []


# --------------------------------------------------------------------------- parsing


def test_status_groups_and_both_staged_and_unstaged(repo):
    (repo / "a.txt").write_text("one\nSTAGED\n")
    _git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("one\nSTAGED\nTHEN-UNSTAGED\n")
    (repo / "new.txt").write_text("n\n")
    st = gitpanel.git_status(str(repo))
    rows = [(e["kind"], e["path"]) for e in st["entries"]]
    # MM is real git state: it belongs in BOTH groups, each with its own diff side.
    assert ("staged", "a.txt") in rows
    assert ("changed", "a.txt") in rows
    assert ("untracked", "new.txt") in rows


def test_untracked_files_are_listed_per_file_not_as_a_directory(repo):
    """`--untracked-files=normal` collapses to `? new/`, which cannot carry filename + parent nor
    feed per-file status into the FILES tree."""
    (repo / "new").mkdir()
    (repo / "new" / "deep").mkdir()
    (repo / "new" / "b.txt").write_text("b\n")
    (repo / "new" / "deep" / "a.txt").write_text("a\n")
    paths = {e["path"] for e in gitpanel.git_status(str(repo))["entries"]}
    assert "new/b.txt" in paths and "new/deep/a.txt" in paths
    assert "new/" not in paths


@pytest.mark.parametrize("name", ["with space.txt", "-leading-dash.txt", "café.txt", "quo'te.txt"])
def test_awkward_filenames_parse(repo, name):
    """`-z` is why this works: v1 output quote-escapes these and a parser built on it is wrong."""
    (repo / name).write_text("x\n")
    paths = {e["path"] for e in gitpanel.git_status(str(repo))["entries"]}
    assert name in paths


def test_rename_carries_both_paths(repo):
    _git(repo, "mv", "a.txt", "renamed.txt")
    entries = gitpanel.git_status(str(repo))["entries"]
    ren = [e for e in entries if e["path"] == "renamed.txt"]
    assert ren and ren[0].get("orig_path") == "a.txt"


def test_detached_and_no_upstream_are_absent_not_zero(repo):
    """Ahead/behind must be absent rather than 0 — "level with upstream" and "no upstream" are
    different facts and the UI has to be able to tell them apart."""
    st = gitpanel.git_status(str(repo))
    assert st["upstream"] is None
    assert st["ahead"] is None and st["behind"] is None

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(repo, "checkout", "-q", head)
    # The 1s single-flight window would otherwise serve the pre-checkout answer — correct
    # behaviour for the panel, wrong for a test that just changed the repo.
    gitpanel.reset_flights_for_test()
    assert gitpanel.git_status(str(repo))["branch"] is None


def test_unborn_repo_has_no_branch_oid_but_still_lists(root):
    proj = root / "empty"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / "f.txt").write_text("x\n")
    st = gitpanel.git_status(str(proj))
    assert st["repo"] is not None
    assert {e["path"] for e in st["entries"]} == {"f.txt"}


def test_unmerged_paths_are_their_own_kind(root):
    p = root / "conflict"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "f.txt").write_text("base\n")
    _git(p, "add", "f.txt")
    _git(p, "commit", "-qm", "base")
    _git(p, "checkout", "-q", "-b", "other")
    (p / "f.txt").write_text("theirs\n")
    _git(p, "commit", "-qam", "theirs")
    _git(p, "checkout", "-q", "-")
    (p / "f.txt").write_text("ours\n")
    _git(p, "commit", "-qam", "ours")
    subprocess.run(["git", "-C", str(p), "merge", "other"], capture_output=True)
    kinds = {e["kind"] for e in gitpanel.git_status(str(p))["entries"] if e["path"] == "f.txt"}
    assert "unmerged" in kinds


# --------------------------------------------------------------------------- diff


def test_diff_of_an_unstaged_change(repo):
    (repo / "a.txt").write_text("one\nTWO\n")
    d = gitpanel.git_diff(str(repo / "a.txt"), staged=False)
    assert d["added"] == 1 and d["removed"] == 1
    assert "-two" in d["diff"] and "+TWO" in d["diff"]
    assert d["truncated"] is False


def test_a_same_second_edit_is_still_seen_a_second_later(repo):
    """The index snapshot must not defeat git's racily-clean guard (#797).

    git decides "unchanged" from `stat` alone when the worktree file matches the stat cached in
    the index. For an edit made in the same timestamp granule as the index write — same size,
    same mtime, different content — that shortcut is unsound, so git re-hashes any entry whose
    mtime is >= the INDEX FILE's own mtime instead of trusting it.

    `sanitized_gitdir` copies the index, and a fresh copy has a fresh mtime — which makes every
    entry look safely older than the index and switches the guard off. git then reports **no
    change for a file that changed**: reproduced 6/6 by editing `a.txt` in the same second as
    the commit and asking a second later (within the same second it passed, which is why this
    surfaced as a load-dependent flake rather than a steady failure).

    The three tests that flaked in CI all share the same-size shape this asserts; the mechanism
    is a real panel defect, so this is a correctness regression rather than test hygiene.
    """
    # Same LENGTH as the committed content, so size cannot betray the change — and written
    # immediately after the `repo` fixture's commit, so it lands in the same whole second.
    (repo / "a.txt").write_text("one\nTWO\n")
    assert int((repo / "a.txt").stat().st_mtime) == int((repo / ".git" / "index").stat().st_mtime)

    # Cross the one-second boundary: this is what a loaded CI run does for free between the
    # fixture and the assertion, and it is the only thing that separates green from red.
    time.sleep(1.2)
    gitpanel.reset_flights_for_test()

    d = gitpanel.git_diff(str(repo / "a.txt"), staged=False)
    assert d["added"] == 1 and d["removed"] == 1, d


def test_untracked_offers_no_diff(repo):
    (repo / "u.txt").write_text("x\n")
    d = gitpanel.git_diff(str(repo / "u.txt"), staged=False)
    assert d["diff"] == ""


def test_binary_is_refused_rather_than_mangled(repo):
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    _git(repo, "add", "bin.dat")
    _git(repo, "commit", "-qm", "bin")
    (repo / "bin.dat").write_bytes(b"\x00\x09\x09")
    d = gitpanel.git_diff(str(repo / "bin.dat"), staged=False)
    assert d["binary"] is True
    assert d["diff"] == ""


def test_crlf_is_not_reported_as_every_line_changed(repo):
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    _git(repo, "add", "crlf.txt")
    _git(repo, "commit", "-qm", "crlf")
    (repo / "crlf.txt").write_bytes(b"a\r\nB\r\n")
    d = gitpanel.git_diff(str(repo / "crlf.txt"), staged=False)
    assert d["added"] == 1 and d["removed"] == 1, d["diff"]


def test_oversized_sources_are_refused_before_comparison(repo, monkeypatch):
    """A response cap applied after difflib has run bounds nothing."""
    monkeypatch.setattr(gitpanel, "DIFF_SOURCE_MAX_BYTES", 64)
    (repo / "big.txt").write_text("x" * 4096 + "\n")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-qm", "big")
    (repo / "big.txt").write_text("y" * 4096 + "\n")
    d = gitpanel.git_diff(str(repo / "big.txt"), staged=False)
    assert d["too_large"] is True
    assert d["added"] is None and d["removed"] is None, "a prefix count is not a total"


def test_truncated_diff_withholds_the_counts(repo, monkeypatch):
    monkeypatch.setattr(gitpanel, "DIFF_MAX_LINES", 5)
    (repo / "many.txt").write_text("\n".join(str(i) for i in range(200)) + "\n")
    _git(repo, "add", "many.txt")
    _git(repo, "commit", "-qm", "many")
    (repo / "many.txt").write_text("\n".join(str(i * 2) for i in range(200)) + "\n")
    d = gitpanel.git_diff(str(repo / "many.txt"), staged=False)
    assert d["truncated"] is True
    assert d["added"] is None and d["removed"] is None


# --------------------------------------------------------------------------- concurrency


def test_concurrent_cold_callers_produce_one_status_run(repo, monkeypatch):
    """A TTL cache alone does not coalesce COLD misses: N pollers each miss, each spawn git."""
    runs: list[int] = []
    real = gitpanel._run_git

    def counting(root, args, *, cwd, **kw):
        if args and args[0] == "status":
            runs.append(1)
        return real(root, args, cwd=cwd, **kw)

    monkeypatch.setattr(gitpanel, "_run_git", counting)
    gitpanel.reset_flights_for_test()

    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(gitpanel.git_status(str(repo))))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 6
    assert len(runs) == 1, f"{len(runs)} status runs for 6 concurrent cold callers"


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def test_git_routes_are_no_store(client, repo):
    (repo / "a.txt").write_text("one\nTWO\n")
    st = client.get("/api/git/status", params={"path": str(repo)})
    assert st.status_code == 200
    assert st.headers["cache-control"] == "no-store"
    assert st.json()["repo"] is not None

    df = client.get("/api/git/diff", params={"path": str(repo / "a.txt")})
    assert df.status_code == 200
    assert df.headers["cache-control"] == "no-store", "a diff carries file bytes"

    bad = client.get("/api/git/status", params={"path": "/etc"})
    assert bad.status_code == 403
    assert bad.headers["cache-control"] == "no-store"


def test_status_route_reports_not_a_repo_as_200(client, root):
    plain = root / "plain"
    plain.mkdir()
    r = client.get("/api/git/status", params={"path": str(plain)})
    assert r.status_code == 200
    assert r.json()["repo"] is None


def test_git_missing_is_stated_not_500(client, repo, monkeypatch):
    monkeypatch.setattr(gitpanel, "_GIT_BIN", None)
    monkeypatch.setattr(gitpanel, "_GIT_BIN_RESOLVED", True)
    r = client.get("/api/git/status", params={"path": str(repo)})
    assert r.status_code == 501
    assert "not installed" in r.text


# --------------------------------------------------------------------------- staged diffs


def test_staged_modification_diffs_index_against_head(repo):
    """A staged row must compare INDEX to HEAD.

    It compared HEAD with itself, because the row carried only one oid — so every staged diff came
    back empty while `git diff --cached` showed real changes.
    """
    (repo / "a.txt").write_text("one\nSTAGED\n")
    _git(repo, "add", "a.txt")
    d = gitpanel.git_diff(str(repo / "a.txt"), staged=True)
    assert d["added"] == 1 and d["removed"] == 1, d
    assert "-two" in d["diff"] and "+STAGED" in d["diff"]


def test_staged_add_and_delete_diff(repo):
    (repo / "added.txt").write_text("brand new\n")
    _git(repo, "add", "added.txt")
    d = gitpanel.git_diff(str(repo / "added.txt"), staged=True)
    assert d["added"] == 1 and d["removed"] == 0, d
    assert "+brand new" in d["diff"]

    _git(repo, "rm", "-q", "a.txt")
    # The status single-flight reuses its answer for 1s, so a change made microseconds later is
    # invisible to it. That is correct for a polling panel and wrong for a test that just staged
    # a deletion, so the window is cleared explicitly.
    gitpanel.reset_flights_for_test()
    d2 = gitpanel.git_diff(str(repo / "a.txt"), staged=True)
    assert d2["removed"] == 2 and d2["added"] == 0, d2


def test_an_MM_path_gives_a_different_diff_per_row(repo):
    """The two rows of an `MM` path are different questions and must not return the same answer."""
    (repo / "a.txt").write_text("one\nSTAGED\n")
    _git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("one\nSTAGED\nUNSTAGED\n")
    staged = gitpanel.git_diff(str(repo / "a.txt"), staged=True)
    unstaged = gitpanel.git_diff(str(repo / "a.txt"), staged=False)
    assert "+STAGED" in staged["diff"]
    assert "+UNSTAGED" in unstaged["diff"]
    assert staged["diff"] != unstaged["diff"]


def test_staged_rename_diffs_against_the_original(repo):
    _git(repo, "mv", "a.txt", "moved.txt")
    d = gitpanel.git_diff(str(repo / "moved.txt"), staged=True)
    # A pure rename has identical content on both sides: no line changes, and definitely no crash.
    assert d["added"] == 0 and d["removed"] == 0, d


# --------------------------------------------------------------------------- comparison bound


def test_a_pathological_pair_is_refused_before_the_expensive_matching(repo, monkeypatch):
    """The old wall-clock check ran AFTER difflib yielded its first line — but difflib does its
    matching before yielding, so the check could only label a slow comparison, never stop one.

    The bound is now on the work itself: trim the common prefix/suffix, then refuse if the
    remaining rectangle is too big. This asserts the refusal happens *without* calling difflib.
    """
    monkeypatch.setattr(gitpanel, "DIFF_MAX_CELLS", 100)
    called = []
    import difflib as _d

    real = _d.unified_diff
    monkeypatch.setattr(_d, "unified_diff", lambda *a, **k: called.append(1) or real(*a, **k))

    (repo / "big.txt").write_text("\n".join(f"old line {i}" for i in range(200)) + "\n")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-qm", "big")
    (repo / "big.txt").write_text("\n".join(f"new line {i}" for i in range(200)) + "\n")

    d = gitpanel.git_diff(str(repo / "big.txt"), staged=False)
    assert d["coarse"] is True, d
    assert not called, "difflib must not be entered once the pair is over budget"
    assert "-old line 0" in d["diff"] and "+new line 0" in d["diff"]


def test_a_normal_edit_stays_a_real_line_diff(repo):
    """The bound must not make ordinary edits coarse — prefix/suffix trimming keeps them tiny."""
    body = "\n".join(f"line {i}" for i in range(5000))
    (repo / "long.txt").write_text(body + "\n")
    _git(repo, "add", "long.txt")
    _git(repo, "commit", "-qm", "long")
    (repo / "long.txt").write_text(body.replace("line 2500", "CHANGED") + "\n")
    d = gitpanel.git_diff(str(repo / "long.txt"), staged=False)
    assert d["coarse"] is False
    assert d["added"] == 1 and d["removed"] == 1, d
