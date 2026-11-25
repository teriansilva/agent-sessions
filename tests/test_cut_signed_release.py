"""`scripts/cut-signed-release` — the operator-run release cut (#832 Phase 1).

Runs against scratch remotes with real keys and real `git archive` filtering. It cannot be
rehearsed against the real repository: `deploy-prod.yml` triggers on `push: tags: ["v*"]`, and
`v*` is a glob, so *any* test tag starting with `v` is a production deploy.

Three of these tests exist because the bug they cover was found by running the script rather
than reading it, and in every case the run **reported success at each step**:

* the snapshot was built from the tag, which does not exist yet at that point;
* `cmd | tail -1` returned *tail's* exit status, so a failed publish looked fine;
* `git fetch` auto-followed tags, so verifying the mirror silently created a local tag holding
  the *mirror's* identity — and the release then shipped the wrong commit, signed and verified.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CUT = REPO / "scripts/cut-signed-release"
PUBLISH = REPO / "scripts/publish-release-snapshot"


def _run(*args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} failed:\n{r.stdout}{r.stderr}")
    return r


def _git(repo: Path, *a, check=True):
    return _run("git", "-C", str(repo), *a, check=check)


def _refs(remote: Path) -> dict[str, str]:
    out = _run("git", "ls-remote", str(remote)).stdout
    return {ln.split("\t")[1]: ln.split("\t")[0] for ln in out.splitlines() if "\t" in ln}


@pytest.fixture
def world(tmp_path):
    """A source repo on main with an origin and a mirror, plus a signing key file.

    Tests sign with `--key` (a passphrase-less file) rather than an agent: the agent path is
    what an operator uses, but it is git's concern, not this script's, and a passphrase prompt
    is not something a test should be simulating.
    """
    key = tmp_path / "key"
    _run("ssh-keygen", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(key), "-q")
    other = tmp_path / "other"
    _run("ssh-keygen", "-t", "ed25519", "-N", "", "-C", "o", "-f", str(other), "-q")

    origin, mirror = tmp_path / "origin.git", tmp_path / "mirror.git"
    for r in (origin, mirror):
        _run("git", "init", "-q", "--bare", "-b", "main", str(r))

    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    _run("git", "init", "-q", "-b", "main", str(src))
    _git(src, "config", "user.name", "t")
    _git(src, "config", "user.email", "t@e")
    (src / "app.py").write_text("app\n")
    (src / "INTERNAL.md").write_text("internal\n")
    (src / ".gitattributes").write_text("INTERNAL.md export-ignore\n")
    for f in (CUT, PUBLISH):
        dst = src / "scripts" / f.name
        dst.write_bytes(f.read_bytes())
        dst.chmod(0o755)
    (src / "scripts/release-signers").write_text(
        f"release@agent-sessions {(tmp_path / 'key.pub').read_text().strip()}\n"
    )
    gate = src / "scripts/check-public-snapshot"
    gate.write_text("#!/bin/sh\nexit 0\n")  # the real gate has its own tests
    gate.chmod(0o755)
    # The installer smoke is a multi-minute pristine-container install, so it is stubbed here the
    # same way — scripts/smoke-install has its own coverage. What these tests care about is that
    # the cut CONSULTS it and aborts when it fails, which
    # test_a_red_installer_smoke_aborts_before_anything_is_published proves by making it fail.
    smoke = src / "scripts/smoke-install"
    smoke.write_text("#!/bin/sh\nexit 0\n")
    smoke.chmod(0o755)
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "c")
    _git(src, "remote", "add", "origin", str(origin))
    _git(src, "push", "-q", "-u", "origin", "main")
    return {
        "src": src,
        "origin": origin,
        "mirror": mirror,
        "key": key,
        "other": other,
        "head": _git(src, "rev-parse", "HEAD").stdout.strip(),
    }


def _cut(w, version="0.20.0", key=None, expect_ok=True, script=None):
    r = subprocess.run(
        [
            "sh",
            str(w["src"] / "scripts" / (script or "cut-signed-release")),
            version,
            "--mirror",
            str(w["mirror"]),
            "--key",
            str(key or w["key"]),
        ],
        cwd=w["src"],
        capture_output=True,
        text=True,
        env={**os.environ, "SSH_AUTH_SOCK": ""},
    )
    if expect_ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


# ---- the three bugs found by running it ----------------------------------------


def test_the_release_tag_names_the_source_commit_not_the_mirror_snapshot(world):
    """THE dangerous one. Every step reported success and the release was still wrong.

    `git fetch` auto-follows tags. Fetching the mirror's tag in order to verify it also created
    a local `refs/tags/<TAG>` holding the MIRROR's identity — a flat snapshot commit. The next
    step found that tag, reported "existing tag verifies — reusing it" (it *did* verify; it was
    signed by the right key), and pushed the mirror's snapshot into the private repo as the
    release.

    A valid signature over the wrong identity is precisely what a signature check cannot catch,
    which is why this asserts the commit rather than the signature.
    """
    _cut(world)
    origin_commit = _refs(world["origin"])["refs/tags/v0.20.0^{}"]
    mirror_commit = _refs(world["mirror"])["refs/tags/v0.20.0^{}"]

    assert origin_commit == world["head"], "the release tag does not name the commit released"
    assert (
        mirror_commit != origin_commit
    ), "the two remotes hold the same object — the mirror is a flat snapshot and must differ"


def test_a_failed_mirror_publish_aborts_instead_of_continuing_empty(world):
    """`cmd | tail -1` yields *tail's* exit status, so a failed publish looked like success.

    The empty identity then flowed onward and the run continued past a step that had not
    happened. Forced here by pointing the mirror at a path that cannot be pushed to.
    """
    r = subprocess.run(
        [
            "sh",
            str(world["src"] / "scripts/cut-signed-release"),
            "0.20.0",
            "--mirror",
            str(world["src"] / "no-such-remote.git"),
            "--key",
            str(world["key"]),
        ],
        cwd=world["src"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "mirror publish failed" in out or "no usable commit id" in out
    assert "refs/tags/v0.20.0" not in _refs(
        world["origin"]
    ), "a release tag was pushed even though the mirror step failed"


def test_the_cut_works_although_the_tag_does_not_exist_yet(world):
    """The snapshot is built from the COMMIT.

    Signing the tag is deliberately the last step — it is what triggers the deploy — so at the
    moment the mirror is built the tag does not exist. Building from the tag failed outright
    with "ref not found", which is the benign version of getting this wrong.
    """
    assert "refs/tags/v0.20.0" not in _refs(world["origin"])
    r = _cut(world)
    assert "released v0.20.0" in r.stdout
    assert _refs(world["mirror"])["refs/tags/v0.20.0^{}"], "no mirror snapshot was produced"


# ---- ordering, which is the design rather than a check -------------------------


def test_nothing_reaches_the_deploy_triggering_remote_until_the_mirror_verifies(world):
    """The Forgejo tag push is what starts a production deploy, so it goes last.

    Deployment therefore cannot begin from a half-published cut: the half that would trigger it
    is the half that happens after everything else is verified. Asserted by breaking the mirror
    step and observing that the deploy-triggering remote is untouched.
    """
    (world["mirror"] / "HEAD").write_text("ref: refs/heads/main\n")  # keep it a valid repo
    r = subprocess.run(
        [
            "sh",
            str(world["src"] / "scripts/cut-signed-release"),
            "0.20.0",
            "--mirror",
            "/nonexistent/mirror.git",
            "--key",
            str(world["key"]),
        ],
        cwd=world["src"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert _refs(world["origin"]).get("refs/tags/v0.20.0") is None


# ---- refusals ------------------------------------------------------------------


def test_refuses_a_tag_that_verifies_but_names_other_content(world):
    """A signature proves who made the tag, never that it describes this release."""
    _git(world["src"], "tag", "-a", "--no-sign", "-m", "x", "v0.20.0", "HEAD~0")
    _git(world["src"], "tag", "-d", "v0.20.0")
    # Sign a tag over a DIFFERENT commit with the genuine key.
    (world["src"] / "other.txt").write_text("other\n")
    _git(world["src"], "add", "-A")
    _git(world["src"], "commit", "-qm", "other")
    other_commit = _git(world["src"], "rev-parse", "HEAD").stdout.strip()
    _git(
        world["src"],
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={world['key']}",
        "tag",
        "-s",
        "v0.20.0",
        "-m",
        "release v0.20.0",
    )
    _git(world["src"], "reset", "-q", "--hard", world["head"])

    r = _cut(world, expect_ok=False)
    assert r.returncode != 0
    blob = r.stdout + r.stderr
    # Two guards can catch this — the precondition check refuses before the cut even starts,
    # and step 3 refuses again if it gets that far. Asserting one exact message would pin the
    # guard rather than the property. What matters is that it refuses, says which commit it
    # objected to, and pushes nothing.
    assert "refusing" in blob.lower(), blob
    assert other_commit[:12] in blob, "the refusal does not say which commit it objected to"
    assert _refs(world["origin"]).get("refs/tags/v0.20.0") is None, "a wrong tag was pushed"


def test_refuses_an_unclean_tree_and_a_non_main_branch(world):
    (world["src"] / "dirty.txt").write_text("x\n")
    r = _cut(world, expect_ok=False)
    assert r.returncode != 0 and "not clean" in (r.stdout + r.stderr)
    (world["src"] / "dirty.txt").unlink()

    _git(world["src"], "checkout", "-q", "-b", "sidebranch")
    r = _cut(world, expect_ok=False)
    assert r.returncode != 0 and "cut from main" in (r.stdout + r.stderr)


def test_refuses_a_bad_version_string(world):
    for bad in ("v0.20.0", "0.20", "latest"):
        r = _cut(world, version=bad, expect_ok=False)
        assert r.returncode != 0, f"accepted {bad!r}"


def test_an_unsignable_key_leaves_no_tag_behind(world):
    """`git tag -s` exits 0 and produces an UNSIGNED tag when the key cannot be used.

    Measured on git 2.43. So the script verifies the tag it just made and, failing that, deletes
    it — otherwise a later run would find an unsigned tag sitting at the right commit and have
    to decide what it meant.
    """
    junk = world["src"] / "not-a-key"
    junk.write_text("definitely not a key\n")
    r = _cut(world, key=junk, expect_ok=False)
    assert r.returncode != 0
    local = _git(world["src"], "tag", "-l", "v0.20.0").stdout.strip()
    assert local == "", "an unsigned or unverifiable tag was left in the repository"


# ---- idempotency ---------------------------------------------------------------


def test_cutting_the_same_release_twice_changes_nothing(world):
    _cut(world)
    before_o, before_m = _refs(world["origin"]), _refs(world["mirror"])
    assert before_o.get("refs/tags/v0.20.0"), "nothing published; the comparison would be vacuous"
    assert before_m.get("refs/tags/v0.20.0")

    _cut(world)
    assert _refs(world["origin"])["refs/tags/v0.20.0"] == before_o["refs/tags/v0.20.0"]
    assert _refs(world["mirror"])["refs/tags/v0.20.0"] == before_m["refs/tags/v0.20.0"]


def test_both_published_identities_are_signed_by_the_trusted_key(world):
    _cut(world)
    signers = world["src"] / "scripts/release-signers"
    for remote in ("origin", "mirror"):
        _git(
            world["src"],
            "fetch",
            "-q",
            "--no-tags",
            str(world[remote]),
            "refs/tags/v0.20.0:refs/tags/vfy",
            "--force",
        )
        out = _git(
            world["src"],
            "-c",
            f"gpg.ssh.allowedSignersFile={signers}",
            "verify-tag",
            "vfy",
            check=False,
        )
        assert out.returncode == 0, f"{remote} tag does not verify"
        assert "release@agent-sessions" in (
            out.stdout + out.stderr
        ), f"{remote}: signature is valid but from an untrusted key"
        _git(world["src"], "tag", "-d", "vfy")


def test_the_mirror_snapshot_stays_filtered(world):
    _cut(world)
    _git(
        world["src"],
        "fetch",
        "-q",
        "--no-tags",
        str(world["mirror"]),
        "refs/tags/v0.20.0:refs/tags/pub",
        "--force",
    )
    files = _git(world["src"], "ls-tree", "-r", "--name-only", "pub").stdout.split()
    assert "app.py" in files
    assert "INTERNAL.md" not in files, "an export-ignored file reached the public mirror"


def test_a_red_installer_smoke_aborts_before_anything_is_published(world):
    """The gate that moved. It used to block the tag from create-release.yml; now the operator's
    tag push IS the deploy trigger, so the smoke has to run inside the cut, ahead of every
    mutation — and it has to actually stop it.

    Asserts on the WORLD, not on the exit code: a non-zero exit proves the script gave up, not
    that it gave up in time. What matters is that a failing smoke leaves no signed mirror tag and
    no deploy-triggering Forgejo tag — i.e. nothing that could start a production rollout.
    """
    smoke = world["src"] / "scripts/smoke-install"
    smoke.write_text("#!/bin/sh\necho 'install failed on a clean box' >&2\nexit 1\n")
    smoke.chmod(0o755)
    # Commit AND push it: the cut refuses a dirty tree, and refuses a HEAD that is not
    # origin/main. Skipping this reached the earlier precondition instead of the smoke, and the
    # test still "failed the cut" — passing for entirely the wrong reason.
    _git(world["src"], "add", "-A")
    _git(world["src"], "commit", "-qm", "red smoke")
    _git(world["src"], "push", "-q", "origin", "main")

    r = _cut(world, "0.20.0", expect_ok=False)

    assert r.returncode != 0
    assert "installer smoke failed" in (r.stdout + r.stderr)
    assert "refs/tags/v0.20.0" not in _refs(world["origin"]), (
        "a red smoke still pushed the deploy-triggering tag — production would roll out an "
        "installer that does not install"
    )
    assert "refs/tags/v0.20.0" not in _refs(
        world["mirror"]
    ), "a red smoke still published the mirror identity"
