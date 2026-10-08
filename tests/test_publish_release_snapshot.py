"""`scripts/publish-release-snapshot`: deterministic, signed, idempotent publish (#832 Phase 1).

Extracted from `publish-public.yml` precisely so it can be tested. These run against a real
local bare "mirror" with real keys and real `git archive` filtering — everything the workflow
does except pointing at infrastructure that serves users.

**Why not test the real thing:** `deploy-prod.yml` triggers on `push: tags: ["v*"]`, and `v*` is
a glob, so *any* test tag starting with `v` is a production deploy — now gated on a trusted
signature, but a tag signed by the real key would deploy for real. There is no disposable release
tag on this repo; a scratch remote is the only honest harness. (`create-release.yml` used to
dispatch the prod deploy and the mirror publish as well; both dispatches were removed in #832 —
the second was destroying the signed mirror tag.)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/publish-release-snapshot"


def _git(repo: Path, *args, check=True, **env):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **env},
    )


def _keygen(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path), "-q"], check=True
    )
    return path


@pytest.fixture
def world(tmp_path):
    """A source repo with a release tag, a bare mirror, a trusted key and an untrusted one."""
    key = _keygen(tmp_path / "key")
    other = _keygen(tmp_path / "other")
    signers = tmp_path / "signers"
    signers.write_text(f"release@agent-sessions {(tmp_path / 'key.pub').read_text().strip()}\n")

    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)

    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    _git_init = subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    assert _git_init.returncode == 0
    _git(src, "config", "user.name", "t")
    _git(src, "config", "user.email", "t@e")
    (src / "app.py").write_text("app\n")
    (src / "INTERNAL.md").write_text("internal\n")
    (src / ".gitattributes").write_text("INTERNAL.md export-ignore\n")
    (src / "scripts/publish-release-snapshot").write_bytes(SCRIPT.read_bytes())
    (src / "scripts/publish-release-snapshot").chmod(0o755)
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "c")
    _git(src, "tag", "-a", "v0.20.0", "-m", "r")
    return {"src": src, "mirror": mirror, "signers": signers, "key": key, "other": other}


def _publish(w, ref="v0.20.0", key=None, signers=None, expect_ok=True):
    r = subprocess.run(
        [
            "sh",
            str(w["src"] / "scripts/publish-release-snapshot"),
            ref,
            str(w["mirror"]),
            "--signers",
            str(signers or w["signers"]),
            "--key",
            str(key or w["key"]),
        ],
        cwd=w["src"],
        capture_output=True,
        text=True,
    )
    if expect_ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def _refs(mirror: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-remote", str(mirror)], capture_output=True, text=True, check=True
    ).stdout
    return {ln.split("\t")[1]: ln.split("\t")[0] for ln in out.splitlines() if "\t" in ln}


# ---- the happy path, and the idempotency that makes resume verifiable ----------


def test_publishes_an_annotated_signed_tag(world):
    _publish(world)
    refs = _refs(world["mirror"])
    assert "refs/tags/v0.20.0" in refs
    assert (
        "refs/tags/v0.20.0^{}" in refs
    ), "no peeled line — the tag is lightweight, so there is no object to sign or verify"


def test_a_repeat_dispatch_changes_nothing(world):
    """The property Hermes asked for — and the assertion is guarded against being vacuous.

    An earlier prototype of this reported "UNCHANGED" while comparing two empty strings, because
    the publish had silently failed. Two nothings compare equal forever, so the non-empty guard
    is the part that makes this test mean anything.
    """
    _publish(world)
    before = _refs(world["mirror"])
    assert before.get("refs/tags/v0.20.0"), "nothing was published; the comparison would be vacuous"
    assert before.get("refs/tags/v0.20.0^{}")

    r = _publish(world)
    assert "reuse" in r.stdout

    after = _refs(world["mirror"])
    assert after["refs/tags/v0.20.0"] == before["refs/tags/v0.20.0"], "tag OBJECT changed"
    assert after["refs/tags/v0.20.0^{}"] == before["refs/tags/v0.20.0^{}"], "peeled commit changed"


def test_the_snapshot_is_filtered(world):
    """`export-ignore` must still be honoured — determinism must not have bypassed the filter."""
    _publish(world)
    subprocess.run(
        [
            "git",
            "-C",
            str(world["src"]),
            "fetch",
            "-q",
            "--no-tags",
            str(world["mirror"]),
            "refs/tags/v0.20.0:refs/tags/pub",
        ],
        check=True,
        capture_output=True,
    )
    files = subprocess.run(
        ["git", "-C", str(world["src"]), "ls-tree", "-r", "--name-only", "pub"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "app.py" in files
    assert "INTERNAL.md" not in files, "an export-ignored file reached the mirror"


def test_the_same_release_recomputes_to_the_same_commit(world, tmp_path):
    """Determinism itself, independent of the mirror — this is what makes resume a *check*.

    Without it, "reuse whatever is already published" is indistinguishable from "trust whatever
    is already published", because there is nothing to compare the published identity against.
    """
    first = _publish(world).stdout.strip().splitlines()[-1]
    subprocess.run(["rm", "-rf", str(world["mirror"])], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(world["mirror"])], check=True)
    second = _publish(world).stdout.strip().splitlines()[-1]
    assert first and second
    assert first == second, "the same release produced two different snapshot commits"


# ---- refusals, all of which must happen BEFORE anything is pushed --------------


def test_refuses_an_already_published_tag_that_is_unsigned(world):
    """Today's mirror shape: a lightweight/unsigned tag must not be silently accepted."""
    _git(world["src"], "push", "-q", str(world["mirror"]), "main")
    _git(world["src"], "push", "-q", str(world["mirror"]), "refs/tags/v0.20.0")
    before = _refs(world["mirror"])

    r = _publish(world, expect_ok=False)
    assert r.returncode != 0
    assert "NOT signed by a trusted key" in (r.stdout + r.stderr)
    assert _refs(world["mirror"]) == before, "the mirror was modified despite the refusal"


def test_refuses_an_already_published_tag_signed_by_an_untrusted_key(world):
    r = _publish(world, key=world["other"], expect_ok=False)
    # The freshly signed tag must not verify against the trust root, and nothing is pushed.
    assert r.returncode != 0
    assert "does not verify" in (r.stdout + r.stderr)
    assert "refs/tags/v0.20.0" not in _refs(world["mirror"])


def test_refuses_when_the_published_release_is_a_different_snapshot(world, tmp_path):
    """A VALID signature over the wrong identity — the case a signature alone cannot catch.

    Measured while designing this: a forged snapshot signed with the genuine key verifies
    happily. What refuses is recomputing the snapshot and comparing, which is only possible
    because the rebuild is deterministic.
    """
    _publish(world)
    before = _refs(world["mirror"])

    # Same trusted key, different content, forced onto the mirror's tag.
    forged = tmp_path / "forged"
    forged.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(forged)], check=True)
    _git(forged, "config", "user.name", "x")
    _git(forged, "config", "user.email", "x@e")
    _git(forged, "config", "gpg.format", "ssh")
    _git(forged, "config", "user.signingkey", str(world["key"]))
    (forged / "app.py").write_text("tampered\n")
    _git(forged, "add", "-A")
    _git(forged, "commit", "-qm", "publish: snapshot of v0.20.0")
    _git(forged, "tag", "-s", "v0.20.0", "-m", "release v0.20.0")
    _git(forged, "push", "-q", "-f", str(world["mirror"]), "refs/tags/v0.20.0")
    poisoned = _refs(world["mirror"])
    assert poisoned["refs/tags/v0.20.0"] != before["refs/tags/v0.20.0"], "the forge did not land"

    r = _publish(world, expect_ok=False)
    assert r.returncode != 0
    blob = r.stdout + r.stderr
    assert "recomputes to" in blob, blob
    assert "Refusing to overwrite" in blob
    # The refusal must not "fix" the mirror by force-pushing over the forgery either.
    assert _refs(world["mirror"])["refs/tags/v0.20.0"] == poisoned["refs/tags/v0.20.0"]


def test_refuses_a_missing_or_empty_trust_root(world, tmp_path):
    """Absent evidence is not a pass — and unlike the installer's remote lookup, this one is
    unambiguous: the file either shipped in the checkout or it did not."""
    empty = tmp_path / "empty-signers"
    empty.write_text("")
    r = _publish(world, signers=empty, expect_ok=False)
    assert r.returncode != 0
    assert "trust root missing or empty" in (r.stdout + r.stderr)
    assert "refs/tags/v0.20.0" not in _refs(world["mirror"])

    r = _publish(world, signers=tmp_path / "nope", expect_ok=False)
    assert r.returncode != 0
    assert "trust root missing or empty" in (r.stdout + r.stderr)


def test_refuses_when_the_signing_key_is_unavailable(world, tmp_path):
    """A release that cannot be signed must not be published unsigned."""
    r = _publish(world, key=tmp_path / "absent", expect_ok=False)
    assert r.returncode != 0
    assert "not readable" in (r.stdout + r.stderr)
    assert "refs/tags/v0.20.0" not in _refs(world["mirror"]), "an unsigned release was published"


# ---- CI must hold no signing authority at all (#832, operator-signed model) ----
#
# The earlier version of this file asserted that the workflows signed correctly. They no longer
# sign at all: the key is passphrase-protected and only a human can unlock it. So what is worth
# guarding is the inverse — that no signing authority has crept back into CI.

WORKFLOWS = REPO / ".forgejo/workflows"


def test_no_workflow_holds_or_references_signing_key_material():
    """CI signing is the hole this design closed; a test is what stops it reopening quietly."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        body = wf.read_text()
        assert "RELEASE_SIGNING_KEY" not in body, f"{wf.name} references a signing secret"
        assert "signingkey" not in body, f"{wf.name} configures a signing key"
        assert "tag -s" not in body, f"{wf.name} signs a tag — signing must be operator-only"


def test_the_release_workflow_does_not_mint_the_tag_itself():
    """The operator signs and pushes the tag; CI reacts to it.

    If create-release.yml went back to creating the tag, releases would be unsigned again while
    everything downstream still claimed they were verified.

    The first version of this test read ``"cut-signed-release" in wf or "git tag -a" not in wf``
    and was worthless: the workflow names ``cut-signed-release`` in an error message, so the left
    operand is permanently true, the right is never evaluated, and restoring ``git tag -a`` would
    not have reddened it. An ``or`` between a check and an incidental string is not a check.
    """
    wf = (WORKFLOWS / "create-release.yml").read_text()
    assert "git tag -a" not in wf, "create-release.yml creates its own unsigned tag again"
    assert "verify-release-tag" in wf, (
        "create-release.yml no longer verifies the tag it publishes. It must call the SHARED "
        "scripts/verify-release-tag — deploy-prod.yml gates on the same script, and two "
        "hand-written copies of a fail-closed check drift apart."
    )


def test_the_release_workflow_cannot_verify_a_tag_against_that_tags_own_trust_root():
    """The root must not come from the artefact. Here two separate things make that true.

    ``actions/checkout@v4`` is deliberately unpinned (no ``ref:``), so the tree is the DISPATCHED
    ref — reviewed main in normal use — rather than the tag's tree. That alone is not enough,
    because a release tag legitimately points at main's HEAD: what stops a *planted* tag is the
    duplicate-tag guard, which refuses unless the tag's commit equals the checkout's HEAD.

    Together: a forged tag has to already point at reviewed main HEAD, leaving only the signature
    to forge — which is what the passphrase withholds. Drop either leg and the workflow verifies
    a tag against a trust root the tag itself could carry, which is self-vouching.

    Pinned as a test rather than a comment because a comment cannot fail. Both legs are asserted
    separately so a removal names which property was lost.
    """
    wf = (WORKFLOWS / "create-release.yml").read_text()
    assert '[ "${TAG_SHA}" = "${HEAD_SHA}" ]' in wf, (
        "the guard tying the tag to the checked-out commit is gone — create-release.yml would "
        "now verify a tag that points anywhere, against a trust root that tag could supply"
    )
    assert not re.search(r"uses:\s*actions/checkout@v4\s*\n\s*with:(?:.*\n)*?\s*ref:", wf), (
        "the release checkout now pins a ref; if that ref is the tag, the trust root comes from "
        "the artefact being verified"
    )


# ---- --only-if-tip: the rolling `main` never moves backwards (#1250) -------------


def _publish_branch(w, source, *extra):
    return subprocess.run(
        [
            "sh",
            str(w["src"] / "scripts/publish-release-snapshot"),
            "main",
            str(w["mirror"]),
            "--source",
            source,
            *extra,
        ],
        cwd=w["src"],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def forge(world, tmp_path):
    """A forge remote whose main has moved past the first commit: A, then B on top."""
    forge = tmp_path / "forge.git"
    subprocess.run(["git", "init", "-q", "--bare", str(forge)], check=True)
    src = world["src"]
    _git(src, "remote", "add", "forge", str(forge))
    a = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "app.py").write_text("app v2\n")
    _git(src, "commit", "-qam", "b")
    b = _git(src, "rev-parse", "HEAD").stdout.strip()
    _git(src, "push", "-q", "forge", "main")
    return {"a": a, "b": b}


def test_an_older_run_finishing_last_does_not_rewind_the_mirror(world, forge):
    """Hermes' reproduction on #1250: B publishes, then A's delayed run force-pushed and the
    mirror's main went back to A. With the guard, A's run sees B is the tip and pushes nothing."""
    r = _publish_branch(world, forge["b"], "--only-if-tip", "forge")
    assert r.returncode == 0, r.stdout + r.stderr
    after_b = _refs(world["mirror"])["refs/heads/main"]
    assert after_b, "B's publish produced no main — the comparison below would be vacuous"

    r = _publish_branch(world, forge["a"], "--only-if-tip", "forge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "superseded" in r.stdout
    assert _refs(world["mirror"])["refs/heads/main"] == after_b, "the older run rewound main"


def test_without_the_guard_an_older_tree_can_still_be_appended(world, forge):
    """Fast-forward ancestry alone cannot prevent reverting content to an older source tree."""
    _publish_branch(world, forge["b"])
    after_b = _refs(world["mirror"])["refs/heads/main"]
    _publish_branch(world, forge["a"])
    assert _refs(world["mirror"])["refs/heads/main"] != after_b
    assert _git(world["mirror"], "show", "main:app.py").stdout == "app\n"


def test_an_unreadable_forge_fails_closed(world, forge, tmp_path):
    r = _publish_branch(world, forge["b"], "--only-if-tip", str(tmp_path / "nowhere.git"))
    assert r.returncode != 0
    assert "refs/heads/main" not in _refs(world["mirror"]), "published blind"


def _publish_release(w, *extra):
    return subprocess.run(
        [
            "sh",
            str(w["src"] / "scripts/publish-release-snapshot"),
            "v0.20.0",
            str(w["mirror"]),
            "--signers",
            str(w["signers"]),
            "--key",
            str(w["key"]),
            *extra,
        ],
        cwd=w["src"],
        capture_output=True,
        text=True,
    )


def test_a_release_never_writes_main(world, forge):
    """Hermes round 2 on #1250: the cut runs outside the workflow's concurrency group, so its
    tip check could not be atomic — it read A, B merged and was published, then the cut pushed A.
    The fix is structural: a release publishes its tag and nothing else, so the workflow is the
    mirror main's only writer. Holds with and without --only-if-tip."""
    _publish_branch(world, forge["b"], "--only-if-tip", "forge")
    after_b = _refs(world["mirror"])["refs/heads/main"]
    for extra in ((), ("--only-if-tip", "forge")):
        r = _publish_release(world, *extra)
        assert r.returncode == 0, r.stdout + r.stderr
        refs = _refs(world["mirror"])
        assert "refs/tags/v0.20.0" in refs
        assert refs["refs/heads/main"] == after_b, f"a release moved main ({extra})"


def test_a_release_on_an_empty_mirror_leaves_it_without_main(world):
    r = _publish_release(world)
    assert r.returncode == 0, r.stdout + r.stderr
    refs = _refs(world["mirror"])
    assert "refs/tags/v0.20.0" in refs
    assert "refs/heads/main" not in refs


def test_a_merge_landing_after_the_tip_read_is_published_by_its_own_run(world, forge, tmp_path):
    """The interleaving the single writer leaves: run A reads the tip (A), merge C lands, A
    pushes A. The mirror is briefly behind, and C's run — queued behind A by `concurrency` —
    carries it forward. Driven through a git wrapper that lands C on the forge right after A's
    tip read, i.e. between the read and the push."""
    src = world["src"]
    # rewind the forge to A so A is the tip when its run reads it
    _git(src, "push", "-q", "-f", "forge", f"{forge['a']}:refs/heads/main")
    (src / "app.py").write_text("app v3\n")
    _git(src, "commit", "-qam", "c")
    c = _git(src, "rev-parse", "HEAD").stdout.strip()

    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "git").write_text(
        "#!/bin/sh\n"
        f'"{real_git}" "$@"; rc=$?\n'
        'case "$*" in *ls-remote*) '
        f'"{real_git}" -C "{src}" push -q -f forge {c}:refs/heads/main >/dev/null 2>&1;; esac\n'
        "exit $rc\n"
    )
    (shim / "git").chmod(0o755)
    env = {**__import__("os").environ, "PATH": f"{shim}:{__import__('os').environ['PATH']}"}
    r = subprocess.run(
        [
            "sh",
            str(src / "scripts/publish-release-snapshot"),
            "main",
            str(world["mirror"]),
            "--source",
            forge["a"],
            "--only-if-tip",
            "forge",
        ],
        cwd=src,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        _git(src, "ls-remote", "forge", "refs/heads/main").stdout.split()[0] == c
    ), "the wrapper never landed C mid-run — this test would be vacuous"
    after_a = _refs(world["mirror"])["refs/heads/main"]

    r = _publish_branch(world, c, "--only-if-tip", "forge")
    assert r.returncode == 0, r.stdout + r.stderr
    after_c = _refs(world["mirror"])["refs/heads/main"]
    assert after_c != after_a, "C's run did not carry the mirror forward"
    snap_c = _publish_branch(world, c, "--dry-run").stdout.strip().splitlines()[-1]
    assert after_c == snap_c


# ---- --message-file (#1326): branch publishes carry the public message; releases refuse it ----


def _mirror_main_message(w) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(w["mirror"]), "log", "-1", "--format=%B", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _with_scanner(w):
    """The final-message scan runs the source checkout's own gate; the fixture has none."""
    dst = w["src"] / "scripts/check-public-snapshot"
    dst.write_bytes((REPO / "scripts/check-public-snapshot").read_bytes())
    dst.chmod(0o755)


def test_a_branch_publish_uses_the_message_file(world, tmp_path):
    _with_scanner(world)
    msg = tmp_path / "msg"
    msg.write_text("feat(web): a panel\n\n- one\n#### a heading survives\n")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mirror_main_message(world) == "feat(web): a panel\n\n- one\n#### a heading survives\n\n"


def test_a_branch_publish_without_a_message_file_keeps_the_generic_message(world):
    r = _publish_branch(world, "main")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mirror_main_message(world) == "publish: snapshot of main\n\n"


def test_a_release_refuses_a_message_file(world, tmp_path):
    msg = tmp_path / "msg"
    msg.write_text("anything\n")
    r = _publish_release(world, "--message-file", str(msg))
    assert r.returncode != 0
    assert "--message-file is for branch publishes" in r.stderr
    assert "refs/tags/v0.20.0" not in _refs(world["mirror"]), "refused, but pushed anyway"


def test_an_empty_message_file_refuses_before_pushing(world, tmp_path):
    msg = tmp_path / "msg"
    msg.write_text("")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode != 0
    assert "refs/heads/main" not in _refs(world["mirror"])


def test_a_committed_message_that_hits_the_denylist_publishes_the_generic_one(world, tmp_path):
    """Hermes on #1326: scan the message as COMMITTED, not only the file it came from."""
    _with_scanner(world)
    msg = tmp_path / "msg"
    msg.write_text("feat: x\n\n- checked on " + "mb-" + "infrabot\n")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fallback" in r.stdout
    assert _mirror_main_message(world) == "publish: snapshot of main\n\n"


@pytest.mark.parametrize(
    "loc",
    [
        "ftp://vault.example.invalid/private/report",
        "www.vault.example.invalid/private",
        "[details](https://vault.example.invalid/p_(v1)?key=k)",
        "ops@vault.corp.lan",
        "10.20.30.40",
        "//[fd00::1]/private/report",
        "fe80::1",
        '<a href="/private">x</a>',
        "/srv/private/report",
        "back\\slash",
    ],
)
def test_a_committed_message_with_a_location_publishes_the_generic_one(world, tmp_path, loc):
    """Hermes on #1327: the tripwire on the COMMITTED message, for a builder that was bypassed."""
    _with_scanner(world)
    msg = tmp_path / "msg"
    msg.write_text(f"feat: x\n\n- see {loc}\n")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mirror_main_message(world) == "publish: snapshot of main\n\n"


def test_a_message_the_builder_passes_is_not_tripped_by_the_publisher(world, tmp_path):
    """The tripwire must stay a subset of the builder's frame, or every real summary would be
    replaced by the generic message."""
    _with_scanner(world)
    text = (
        "feat(web): a panel\n\n"
        "- `Open` fills `--accent`/`--on-accent` on hover; ✕ sits apart; ≥44px at ≤800px.\n"
        "- Two-tier header: `NOTIFICATIONS ● N unread`, then `Clear all? Yes / Cancel`.\n"
        "- `web/src/app/origins.test.ts`, `docs/design.md` and `install.sh`.\n"
    )
    msg = tmp_path / "msg"
    msg.write_text(text)
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fallback" not in r.stdout
    assert _mirror_main_message(world) == text + "\n"


def test_a_missing_scanner_publishes_the_generic_message(world, tmp_path):
    msg = tmp_path / "msg"
    msg.write_text("feat: a clean message\n")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mirror_main_message(world) == "publish: snapshot of main\n\n"


def test_the_fallback_is_the_same_commit_as_a_generic_publish(world, tmp_path):
    """The fallback must not cost determinism: it recomputes to the generic publish's SHA."""
    r = _publish_branch(world, "main", "--dry-run")
    generic = r.stdout.strip().splitlines()[-1]
    msg = tmp_path / "msg"
    msg.write_text("feat: x " + "infrastructure" + "-docs\n")
    _with_scanner(world)
    r = _publish_branch(world, "main", "--message-file", str(msg), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == generic


@pytest.mark.parametrize(
    "body",
    [
        "## Summary\n- real\n\n## Test plan\n```md\n## Summary\n- PRIVATE-PLAN-SENTINEL\n```\n",
        "## Summary\n- real\n\n   ## Test plan\n- PRIVATE-PLAN-SENTINEL\n",  # round 4
        "<!--\n## Summary\n- PRIVATE-PLAN-SENTINEL\n-->\n## Test plan\n- test\n",  # round 5
    ],
)
def test_builder_to_mirror_only_the_real_summary_publishes(world, tmp_path, body):
    """Hermes on #1327, rounds 3 to 5, end to end: no other section's text reaches the mirror.
    Where the body's shape is not the template's, the builder falls back to the title alone."""
    _with_scanner(world)
    src = world["src"]
    builder = src / "scripts/public-commit-message"
    builder.write_bytes((REPO / "scripts/public-commit-message").read_bytes())
    builder.chmod(0o755)
    _git(src, "commit", "-q", "--allow-empty", "-m", "feat(web): a panel (#5)")
    sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    pr = tmp_path / "pr.json"
    pr.write_text(
        __import__("json").dumps(
            {"merged": True, "merge_commit_sha": sha, "title": "feat(web): a panel", "body": body}
        )
    )
    msg = tmp_path / "msg"
    built = subprocess.run(
        [str(builder), sha, "--pr-json", str(pr)], cwd=src, capture_output=True, text=True
    )
    assert built.returncode == 0, built.stderr
    msg.write_text(built.stdout)
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stdout + r.stderr
    published = _mirror_main_message(world)
    assert "SENTINEL" not in published
    assert published == "feat(web): a panel\n\n"  # not the template's shape: title alone


# ---- continuous public ancestry (#1352), starting with the existing root ----------


def test_successive_publishes_keep_history_filtered_and_exact(world, forge):
    src, mirror = world["src"], world["mirror"]
    roots = []
    for source in (forge["a"], forge["b"]):
        r = _publish_branch(world, source)
        assert r.returncode == 0, r.stdout + r.stderr
        roots.append(_refs(mirror)["refs/heads/main"])
    _git(src, "rm", "app.py")
    (src / "new.py").write_text("new app\n")
    _git(src, "add", "new.py")
    _git(src, "commit", "-qm", "private source description must not escape")
    r = _publish_branch(world, "main")
    assert r.returncode == 0, r.stdout + r.stderr
    roots.append(_refs(mirror)["refs/heads/main"])
    assert _git(mirror, "rev-list", "--reverse", "main").stdout.splitlines() == roots
    assert _git(mirror, "show", "main:new.py").stdout == "new app\n"
    assert _git(mirror, "show", "main~1:app.py").stdout == "app v2\n"
    assert _git(mirror, "show", "main~2:app.py").stdout == "app\n"
    assert _git(mirror, "cat-file", "-e", "main:app.py", check=False).returncode != 0
    for sha in roots:
        assert "INTERNAL.md" not in _git(mirror, "ls-tree", "-r", "--name-only", sha).stdout
    private = set(_git(src, "rev-list", "--all").stdout.splitlines())
    assert not private.intersection(roots), "private commits reached the mirror"
    metadata = _git(mirror, "log", "--format=%an <%ae> %cn <%ce>%n%B", "main").stdout
    assert "private source description" not in metadata
    assert "t@e" not in metadata
    assert "agent-sessions-publish@users.noreply.github.com" in metadata


def test_same_tree_retry_ignores_changed_source_date_and_message(world, tmp_path):
    _with_scanner(world)
    msg = tmp_path / "message"
    msg.write_text("feat: original description\n")
    r = _publish_branch(world, "main", "--message-file", str(msg))
    assert r.returncode == 0, r.stderr
    before = _refs(world["mirror"])
    _git(world["src"], "commit", "--allow-empty", "-qm", "another private-only change")
    msg.write_text("feat: changed description\n")
    for extra in ((), ("--dry-run",)):
        r = _publish_branch(world, "main", "--message-file", str(msg), *extra)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "reuse" in r.stdout
        assert _refs(world["mirror"]) == before
        assert r.stdout.splitlines()[-1] == before["refs/heads/main"]
    assert _mirror_main_message(world) == "feat: original description\n\n"


def test_dry_run_computes_the_append_without_mutating_refs(world, forge):
    _publish_branch(world, forge["a"])
    before = _refs(world["mirror"])
    dry = _publish_branch(world, forge["b"], "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert _refs(world["mirror"]) == before
    result = _publish_branch(world, forge["b"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == dry.stdout.splitlines()[-1]


def test_unreadable_public_remote_fails_even_for_a_dry_run(world, tmp_path):
    world["mirror"] = tmp_path / "unreachable.git"
    result = _publish_branch(world, "main", "--dry-run")
    assert result.returncode != 0
    assert "refusing to publish blind" in result.stderr


def test_concurrent_public_update_after_fetch_is_not_overwritten(
    world, forge, tmp_path, monkeypatch
):
    _publish_branch(world, forge["a"])
    before = _refs(world["mirror"])["refs/heads/main"]
    # An independent writer has a valid successor ready but publishes it only after our fetch.
    other = tmp_path / "other-writer"
    _git(tmp_path, "clone", "-q", "-b", "main", str(world["mirror"]), str(other))
    _git(other, "config", "user.name", "public writer")
    _git(other, "config", "user.email", "public@example.com")
    (other / "concurrent.txt").write_text("preserve this published change\n")
    _git(other, "add", "concurrent.txt")
    _git(other, "commit", "-qm", "concurrent public change")
    competing = _git(other, "rev-parse", "HEAD").stdout.strip()
    real_git = __import__("shutil").which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(
        '#!/bin/sh\n"$REAL_GIT" "$@"; rc=$?\n'
        'if [ "$1" = fetch ] && [ "$rc" -eq 0 ]; then\n'
        '  "$REAL_GIT" -C "$OTHER_WRITER" push -q origin main || exit 90\n'
        'fi\nexit "$rc"\n'
    )
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("REAL_GIT", real_git)
    monkeypatch.setenv("OTHER_WRITER", str(other))
    monkeypatch.setenv("PATH", f"{shim}:{__import__('os').environ['PATH']}")
    result = _publish_branch(world, forge["b"])
    assert result.returncode != 0
    assert "could not fast-forward" in result.stderr
    assert _refs(world["mirror"])["refs/heads/main"] == competing != before


def test_existing_signed_tag_identity_survives_branch_history(world, forge):
    _publish_release(world)
    tags = _refs(world["mirror"])
    for source in (forge["a"], forge["b"]):
        result = _publish_branch(world, source)
        assert result.returncode == 0, result.stderr
    before = _refs(world["mirror"])
    result = _publish_release(world)
    assert result.returncode == 0, result.stderr
    assert _refs(world["mirror"]) == before
    for ref, sha in tags.items():
        assert before[ref] == sha
    assert _git(world["mirror"], "rev-list", "--count", "main").stdout.strip() == "2"
