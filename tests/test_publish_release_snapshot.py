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
        ["git", "-C", str(world["src"]), "fetch", "-q", str(world["mirror"]), "main:pub"],
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
