"""Release-signing trust root (#832 Phase 0).

Phase 0 ships the key, the trust root, and the record — and *no enforcement*. What can still
go wrong at this stage is drift: the copy of the trust root embedded in `install.sh` silently
diverging from `scripts/release-signers`, so the file everyone reads and the value actually
used disagree. That is the failure these tests exist to make impossible.

They also pin the verifier's observable behaviour, because Phase 2's fail-closed logic is built
on top of it and `git verify-tag` offers no machine-readable status for SSH signatures.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
SIGNERS = REPO / "scripts/release-signers"
TRUST = REPO / "scripts/release-trust.json"


def _signer_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _embedded(name: str) -> str:
    m = re.search(rf"^{name}='([^']*)'$", INSTALL_SH.read_text(), re.M)
    assert m, f"{name} not found in install.sh"
    return m.group(1)


# ---- the trust root itself ------------------------------------------------------


#: The trust root, by fingerprint. Pinned here so a swapped or edited key line is a red build, not a
#: silent change of who can sign releases. Custody of both: docs/release-signing.md.
PRIMARY_FPR = "SHA256:2aBGF8oP1PEvJFiD2/GWVLnl8sC3IJ2lhNahZjm/reY"
RECOVERY_FPR = "SHA256:kUrh/5H71d27nPuIZan3Uv+//Ny+Bxy2IExdOAbQvVo"


def _fingerprint(line: str) -> str:
    _principal, keytype, b64, *_ = line.split()
    out = subprocess.run(
        ["ssh-keygen", "-lf", "-"], input=f"{keytype} {b64}\n", capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.split()[1]


def test_signers_file_holds_the_primary_then_the_recovery_key():
    """Two entries, one principal. The recovery key (Recovery §2) signs only the bridge release
    that replaces a lost primary; sharing the principal keeps every `Good "git" signature for
    release@agent-sessions` check valid for it without a second rule."""
    lines = _signer_lines(SIGNERS.read_text())
    assert len(lines) == 2, f"expected primary + recovery, got {len(lines)}: {lines}"
    for line in lines:
        principal, keytype, b64, *_ = line.split()
        assert principal == "release@agent-sessions"
        assert keytype == "ssh-ed25519"
        assert len(b64) > 40
    assert [_fingerprint(line) for line in lines] == [PRIMARY_FPR, RECOVERY_FPR]


def test_the_trust_root_never_contains_private_key_material():
    """Paranoia, and cheap. A private key here would be published to the mirror."""
    for p in (SIGNERS, TRUST, INSTALL_SH):
        body = p.read_text()
        assert "PRIVATE KEY" not in body, f"private key material in {p.name}"


def test_embedded_root_matches_the_signers_file_byte_for_byte():
    """The whole point of Phase 0's test: the file and the value in use cannot drift.

    `install.sh` deliberately does NOT read `scripts/release-signers` from the clone it is
    verifying — a signer list taken from the candidate authenticates nothing. So the embedded
    copy is the one that matters operationally, and the committed file is what humans read and
    edit. Two copies of a trust root is a correctness hazard unless something forces them equal.
    """
    assert _embedded("RELEASE_SIGNERS") == "\n".join(_signer_lines(SIGNERS.read_text()))


def test_embedded_cutover_matches_the_trust_record():
    doc = json.loads(TRUST.read_text())
    assert _embedded("RELEASE_LAST_UNSIGNED") == doc["last_unsigned_release"]
    assert doc["signers_file"] == "scripts/release-signers"
    assert doc["namespace"] == "git"


def test_the_recorded_cutover_is_well_formed_and_real():
    """A cutover naming a version nobody cut would enforce on everything, or on nothing.

    Split into two assertions on purpose, because they need different things to be true of the
    *checkout* rather than of the code. Shape is checkable anywhere. Existence needs tags — and
    CI clones shallow without them, which is how the first version of this test failed in CI
    while passing locally: it was asserting a property of the checkout, not of the repository.

    The skip is narrow by design. It fires only when the checkout has **no tags at all**, which
    is unambiguous evidence that existence is not checkable here. A recorded tag missing while
    other tags are present is a real error and is never skipped — that is the case worth
    catching, and a blanket skip would have hidden it.
    """
    doc = json.loads(TRUST.read_text())
    tag = doc["last_unsigned_release"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", tag), f"not a release version: {tag}"

    tags = subprocess.run(
        ["git", "-C", str(REPO), "tag", "-l", "v*"], capture_output=True, text=True
    ).stdout.split()
    if not tags:
        pytest.skip("no tags in this checkout (shallow clone) — existence is not checkable here")
    assert tag in tags, f"cutover {tag} is recorded but absent from this repo's {len(tags)} tags"


def test_the_custody_record_exists_and_is_kept_out_of_the_public_snapshot():
    doc = REPO / "docs/release-signing.md"
    assert doc.exists(), "Phase 0 requires the custody/rotation/recovery record"
    attrs = (REPO / ".gitattributes").read_text()
    assert re.search(
        r"^docs/release-signing\.md\s+export-ignore$", attrs, re.M
    ), "the custody record names the release host and key paths; it must not reach the mirror"


def test_the_docs_do_not_claim_a_control_that_is_not_wired_up_yet():
    """Docs must track what the code actually does — in BOTH directions.

    Phase 0 is inert: `install.sh` carries the trust root as a constant and has no `verify-tag`
    gate. Documentation written in the present tense about a control that does not exist is
    worse than no documentation, because it stops operators looking for the gap. Caught in
    review on this PR, where INSTALL.md said the installer "verifies before building anything".

    The coupling runs the other way too, which is the point of testing it rather than fixing the
    prose once: when Phase 2 adds the gate, this test fails until the marker is removed.
    Neither direction can drift silently.
    """
    install_sh = INSTALL_SH.read_text()
    docs = (REPO / "INSTALL.md").read_text()
    enforcing = "verify-tag" in install_sh

    # Keyed on an explicit marker, not on prose. The first version matched the banner's wording
    # and went red the moment that wording was legitimately updated for Phase 1 — a guard that
    # fires on rephrasing teaches people to edit the guard instead of the claim.
    marker = "<!-- signing-enforcement: off -->"
    if not enforcing:
        assert (
            marker in docs
        ), "install.sh has no verify-tag gate, so INSTALL.md must carry the not-enforced marker"
    else:
        assert (
            marker not in docs
        ), "install.sh now verifies signatures — drop the not-enforced marker and its banner"


def test_the_custody_record_does_not_claim_the_installer_protects_anyone_yet():
    """The over-claim direction, on the file an incident responder reads first.

    A sibling test pins INSTALL.md against `install.sh`. This pins the custody record, which makes
    the same class of claim to a different audience: INSTALL.md is read by someone installing,
    this file by someone deciding whether a release can be trusted. Phase 1 gave CI a real
    `verify-tag` gate, and the tempting next edit is to summarise that as "releases are verified"
    — which an installing user would read as protection they do not have.

    So while `install.sh` carries no gate, this file must say so in as many words. When Phase 2
    lands the gate, this test fails until the sentence is removed — the same bidirectional
    coupling as the INSTALL.md marker, and for the same reason: a claim nothing can falsify is
    not a claim.
    """
    body = (REPO / "docs/release-signing.md").read_text()
    disclaimer = "**`install.sh` does not verify anything.**"
    if "verify-tag" in INSTALL_SH.read_text():
        assert disclaimer not in body, (
            "install.sh now verifies; the custody record still says it does not — "
            "drop the disclaimer and move Phase 2 to landed"
        )
    else:
        assert disclaimer in body, (
            "install.sh has no verify-tag gate, so the custody record must say plainly that an "
            "installing user is not protected yet"
        )


def test_the_custody_record_names_durable_identities():
    """The record exists so nobody has to remember where the secret is.

    A key whose location is lost is worse than no key — that was the operator's stated reason
    for choosing this arrangement. It must therefore name the key file, the passphrase's home,
    and the fingerprint that identifies the right key, plus what to do when any of them is lost.
    The file is export-ignored, so naming internal infrastructure here is safe; a separate test
    pins that exclusion.
    """
    body = (REPO / "docs/release-signing.md").read_text()
    for needed, why in [
        ("**Private key (encrypted)**", "where the key file lives"),
        ("**Passphrase**", "where the thing that actually protects it lives"),
        ("Fingerprint", "how to confirm you are looking at the right key"),
        ("Why the passphrase is the whole security model", "why location alone protects nothing"),
        ("## Recovery", "what to do when it is lost"),
        ("Accepted risk — signed off, and by whom", "who accepted this model, and what exactly"),
    ]:
        assert needed in body, f"custody record does not record {why}"
    assert "PRIVATE KEY" not in body, "custody record must describe the key, never contain it"


def test_the_documented_fingerprint_is_derived_from_the_committed_key():
    """Bind every recorded fingerprint to an actual key, not merely to the word "Fingerprint".

    An earlier version only asserted the heading existed. That stays green through a key
    rotation, leaving the custody record pointing at a fingerprint nobody can match — which is
    the one thing an operator uses to confirm they are holding the right key in an incident.
    """
    doc = (REPO / "docs/release-signing.md").read_text()
    for line in _signer_lines(SIGNERS.read_text()):
        derived = _fingerprint(line)
        assert derived in doc, (
            f"custody record does not carry the committed key's fingerprint ({derived}) — "
            "it is stale, which makes it useless for confirming the right key"
        )


# ---- the verifier's behaviour, which Phase 2 will depend on ---------------------


def _keygen(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path), "-q"],
        check=True,
    )
    return path


def _repo_with_signed_tag(tmp: Path, key: Path, tag: str = "v9.9.9") -> Path:
    r = tmp / "repo"
    r.mkdir(parents=True)

    def git(*a, **kw):
        subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, **kw)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@e")
    git("config", "gpg.format", "ssh")
    git("config", "user.signingkey", str(key))
    (r / "f").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "c")
    git("tag", "-s", tag, "-m", tag)
    return r


def _verify(repo: Path, root: Path, tag: str = "v9.9.9"):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", f"gpg.ssh.allowedSignersFile={root}", "verify-tag", tag],
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
    reason="ssh-keygen absent",
)
def test_verifier_accepts_a_trusted_signer_and_refuses_an_untrusted_one(tmp_path):
    """Positive control plus the case that matters, on the real toolchain.

    Without the positive control, a harness that could never succeed would make every
    fail-closed assertion below pass for the wrong reason.
    """
    good, bad = _keygen(tmp_path / "good"), _keygen(tmp_path / "bad")
    root = tmp_path / "signers"
    root.write_text(f"release@agent-sessions {(tmp_path / 'good.pub').read_text().strip()}\n")

    assert _verify(_repo_with_signed_tag(tmp_path / "a", good), root).returncode == 0

    r = _verify(_repo_with_signed_tag(tmp_path / "b", bad), root)
    assert r.returncode != 0
    assert "No principal matched" in (r.stdout + r.stderr)


@pytest.mark.skipif(
    subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
    reason="ssh-keygen absent",
)
def test_a_missing_trust_root_is_not_distinguishable_from_an_unknown_signer(tmp_path):
    """Pins the trap that Phase 4's step-down rule must not fall into.

    Both an unknown signer and a MISSING trust root emit `No principal matched`. A rotation
    walk keyed on that string alone would treat "the signers file was deleted" as "step down
    to an older release" — turning a deleted file into a downgrade attack. Phase 2/4 must
    therefore prove the root is present *before* verifying, and this test exists so that
    requirement is anchored in observed behaviour rather than a comment someone can delete.

    It also fails loudly if a git/openssh upgrade changes the wording, which is the other way
    the classification could silently break.
    """
    good = _keygen(tmp_path / "good")
    root = tmp_path / "signers"
    root.write_text(f"release@agent-sessions {(tmp_path / 'good.pub').read_text().strip()}\n")
    repo = _repo_with_signed_tag(tmp_path / "a", good)

    assert _verify(repo, root).returncode == 0  # control: this root works

    missing = _verify(repo, tmp_path / "does-not-exist")
    assert missing.returncode != 0
    blob = missing.stdout + missing.stderr
    assert (
        "No principal matched" in blob
    ), "the ambiguity this test documents has changed — re-check Phase 2/4's classification"
    assert "Unable to open allowed keys file" in blob


@pytest.mark.skipif(
    subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
    reason="ssh-keygen absent",
)
def test_an_unsigned_tag_refuses_which_is_why_the_cutover_exists(tmp_path):
    """Every release up to the recorded cutover is unsigned; enforcement must not be retroactive."""
    good = _keygen(tmp_path / "good")
    root = tmp_path / "signers"
    root.write_text(f"release@agent-sessions {(tmp_path / 'good.pub').read_text().strip()}\n")
    repo = _repo_with_signed_tag(tmp_path / "a", good)
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-a", "v9.9.8", "-m", "unsigned"],
        check=True,
        capture_output=True,
    )
    r = _verify(repo, root, tag="v9.9.8")
    assert r.returncode != 0
    assert "no signature" in (r.stdout + r.stderr)
