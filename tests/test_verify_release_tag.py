"""The release gate: `scripts/verify-release-tag`, and the two workflows that must use it (#832).

Phase 1 signs releases. Signing is worth nothing unless something REFUSES an unsigned one, and
review found the refusal missing from the only path that mutates production: `deploy-prod.yml`
ran on every `v*` push and went straight to `install.sh`. These tests pin the gate's behaviour
and its placement, because either one alone is insufficient — a correct check wired in after the
mutation is not a check.

Behaviour is exercised against real `git` + `ssh-keygen` rather than mocked. The failure modes
here (a valid signature from an untrusted key; a MISSING trust root, which git reports with the
same "No principal matched" text as an unknown signer) are properties of the toolchain, and a
mock would happily assert whatever this file claimed they were.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts/verify-release-tag"
WORKFLOWS = REPO / ".forgejo/workflows"

needs_ssh_keygen = pytest.mark.skipif(
    subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
    reason="ssh-keygen absent",
)


def _keygen(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path), "-q"], check=True
    )
    return path


def _repo(tmp: Path, signer_pub: Path, cutover: str = "v0.19.2") -> Path:
    r = tmp / "repo"
    r.mkdir(parents=True)

    def git(*a):
        subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@e")
    git("config", "gpg.format", "ssh")
    (r / "scripts").mkdir()
    (r / "scripts/release-signers").write_text(
        f"release@agent-sessions {signer_pub.read_text().strip()}\n"
    )
    (r / "f").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "c")
    head = subprocess.run(
        ["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # The exemption is MEMBERSHIP in a commit-pinned map, not an ordering comparison — so the
    # record has to name the real historical releases and where they point.
    (r / "scripts/release-trust.json").write_text(
        json.dumps(
            {
                "version": 1,
                "namespace": "git",
                "last_unsigned_release": cutover,
                "unsigned_releases": {"v0.19.0": head, cutover: head},
            }
        )
    )
    return r


def _tag(repo: Path, tag: str, key: Path | None) -> None:
    cmd = ["git", "-C", str(repo)]
    if key is not None:
        cmd += ["-c", f"user.signingkey={key}"]
        cmd += ["tag", "-s", tag, "-m", tag]
    else:
        cmd += ["tag", "-a", tag, "-m", tag]
    subprocess.run(cmd, check=True, capture_output=True)


def _run(repo: Path, tag: str, **kw) -> subprocess.CompletedProcess:
    args = ["sh", str(VERIFY), tag]
    for flag, val in kw.items():
        args += [f"--{flag.replace('_', '-')}", str(val)]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True)


# ---- the gate's behaviour -------------------------------------------------------


@needs_ssh_keygen
def test_a_tag_signed_by_the_trusted_key_is_accepted(tmp_path):
    """Positive control. Without it every refusal below could pass on a broken harness."""
    good = _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.20.0", good)
    assert _run(repo, "v0.20.0").returncode == 0


@needs_ssh_keygen
@pytest.mark.parametrize(
    "tag,key_name,why",
    [
        ("v0.20.0", "bad", "a valid signature from a key the trust root does not name"),
        ("v0.20.0", None, "no signature at all"),
    ],
)
def test_an_untrusted_or_unsigned_tag_is_refused(tmp_path, tag, key_name, why):
    """The two ways a tag can be untrustworthy. The first is the dangerous one.

    A wrong-key tag still produces `Good "git" signature` on stdout — just without a principal —
    so any check grepping for that phrase accepts an attacker. The gate never greps; it uses the
    exit status of a verify against a root it has already proven present.
    """
    _keygen(tmp_path / "good")
    key = _keygen(tmp_path / "bad") if key_name else None
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, tag, key)
    r = _run(repo, tag)
    assert r.returncode == 1, f"{why} was accepted"
    assert "REFUSED" in r.stderr


@needs_ssh_keygen
def test_a_missing_trust_root_refuses_rather_than_looking_like_an_unknown_signer(tmp_path):
    """The trap this gate exists to avoid.

    `git verify-tag` prints "No principal matched" for BOTH an unknown signer and a trust root
    that does not exist, so a check keyed on git's output cannot tell "untrusted key" from
    "somebody deleted the signers file". The gate proves the root is present first, and says so
    distinctly — a deleted root must never read as a routine refusal that someone works around.
    """
    good = _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.20.0", good)

    assert _run(repo, "v0.20.0").returncode == 0  # control: this root works

    r = _run(repo, "v0.20.0", signers=tmp_path / "does-not-exist")
    assert r.returncode == 1
    assert (
        "trust root missing or empty" in r.stderr
    ), "a missing trust root must be named as such, not blurred into an untrusted-signer refusal"


@needs_ssh_keygen
def test_a_trust_root_with_no_signer_entry_refuses(tmp_path):
    """An empty root verifies nothing, so it would refuse every release — which reads as "all
    releases are forged" rather than "the root is broken". Name the real condition."""
    good = _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.20.0", good)
    empty = tmp_path / "empty-signers"
    empty.write_text("# only a comment\n")
    r = _run(repo, "v0.20.0", signers=empty)
    assert r.returncode == 1
    assert "no signer entry" in r.stderr


@needs_ssh_keygen
@pytest.mark.parametrize("tag", ["v0.19.0", "v0.19.2"])
def test_a_recorded_pre_signing_release_does_not_need_a_signature(tmp_path, tag):
    """Enforcement must not be retroactive: every existing release is unsigned, and a rollback to
    one has to keep working."""
    _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, tag, None)
    assert _run(repo, tag).returncode == 0


@needs_ssh_keygen
@pytest.mark.parametrize("tag", ["v", "v0", "v0.0.0", "v00.00.00", "v0.0.1", "v0.18.99"])
def test_an_old_looking_tag_that_is_not_a_real_release_is_refused(tmp_path, tag):
    """The bypass this exemption used to have, and the reason it is a list rather than a compare.

    The first version asked whether the tag NAME sorted at or below `last_unsigned_release` with
    `sort -V`. Every name here sorts below `v0.19.2`, every one matches deploy-prod's `v*`
    trigger, and none is a real release — so anyone able to push a tag could deploy arbitrary
    content unsigned, which is the whole control defeated. A regex would not have fixed it
    either: `v0.0.0` is well-formed semver and still sorts low. Only membership does.

    Reproduced in review on the real `release-trust.json` before the fix.
    """
    _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, tag, None)
    r = _run(repo, tag)
    assert r.returncode == 1, f"{tag} took the pre-cutover exemption and would deploy unsigned"
    assert "not a recorded pre-signing release" in r.stderr


@needs_ssh_keygen
def test_a_recorded_release_moved_onto_other_content_loses_the_exemption(tmp_path):
    """Why the allowlist pins commits and not just names.

    A name-only list lets a historical tag be force-pushed onto attacker content and redeployed:
    the name still matches, so the exemption is inherited by content that never shipped. The pin
    makes the exemption belong to the release, not to the string.
    """
    _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.19.0", None)
    assert _run(repo, "v0.19.0").returncode == 0  # control: it is exempt where it belongs

    (repo / "f").write_text("evil")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "evil"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-a", "-f", "v0.19.0", "-m", "moved"],
        check=True,
        capture_output=True,
    )

    r = _run(repo, "v0.19.0")
    assert r.returncode == 1, "a moved historical tag kept its exemption"
    assert "has been moved" in r.stderr


@needs_ssh_keygen
def test_an_unparseable_trust_record_is_an_error_not_a_free_pass(tmp_path):
    """The cutover decides whether a signature is required, so a broken record must stop the
    release — not silently take the branch that requires nothing."""
    good = _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.20.0", good)
    broken = tmp_path / "broken.json"
    broken.write_text('{"version": 1}')
    assert _run(repo, "v0.20.0", trust=broken).returncode == 2


# ---- the gate's PLACEMENT, which is the half review found missing ---------------


def _uncommented(body: str) -> str:
    """Comments describing a removed hazard must not be mistaken for the hazard itself."""
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))


def _steps(wf: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / wf).read_text())
    return [s for job in doc["jobs"].values() for s in job["steps"]]


def test_deploy_prod_verifies_the_tag_before_install_sh_ever_runs():
    """The negative regression: an unsigned tag must not reach `install.sh`.

    A workflow cannot be executed here, so this asserts the property that makes the outcome
    inevitable — the gate exists, it is unconditional, and it is ORDERED BEFORE the step that
    mutates production. Ordering is the whole finding: the same check placed after the install
    would pass a test that only asked "is there a verify step?".
    """
    steps = _steps("deploy-prod.yml")
    gate = next(
        (i for i, s in enumerate(steps) if "verify-release-tag" in (s.get("run") or "")), None
    )
    install = next((i for i, s in enumerate(steps) if "install.sh" in (s.get("run") or "")), None)
    assert gate is not None, "deploy-prod.yml does not verify the release tag at all"
    assert install is not None, "deploy-prod.yml no longer installs — re-check this test"
    assert gate < install, "the signature gate runs AFTER install.sh; production is already updated"
    assert not steps[gate].get(
        "if"
    ), "the gate is conditional — a fail-closed check must not be skippable by trigger type"


def test_deploy_prod_takes_the_gate_from_main_not_from_the_tag_being_verified():
    """On a `push: tags` trigger the checkout IS the tag's tree, so its `scripts/release-signers`
    is supplied by whoever made the tag. Verifying against that is self-vouching. The verifier,
    the root and the cutover must all be read from `origin/main`, which is branch-protected."""
    run = next(
        s["run"] for s in _steps("deploy-prod.yml") if "verify-release-tag" in (s.get("run") or "")
    )
    for artefact in ("verify-release-tag", "release-signers", "release-trust.json"):
        assert (
            f"origin/main:scripts/{artefact}" in run
        ), f"{artefact} is not read from origin/main — the tag's author supplies it"


def test_deploy_prod_fetches_main_into_a_remote_tracking_ref_not_fetch_head():
    """`git show origin/main:…` needs refs/remotes/origin/main to exist, and on a tag-triggered
    run it does not.

    A tag checkout carries a narrow configured refspec (`+refs/tags/<TAG>:refs/tags/<TAG>`), and
    passing any refspec on the command line suppresses the configured one — so a bare `main`
    fetches into FETCH_HEAD and never creates the remote-tracking ref. The fetch still reports
    success; the gate then dies on "invalid object name". Failing closed, but on an error that
    names nothing relevant, and on every single release deploy.

    Measured on a real shallow tag clone: bare `main` left origin/main absent, the explicit
    destination created it.
    """
    run = next(
        s["run"] for s in _steps("deploy-prod.yml") if "verify-release-tag" in (s.get("run") or "")
    )
    assert "refs/heads/main:refs/remotes/origin/main" in run, (
        "main is not fetched into a remote-tracking ref; origin/main will not resolve and the "
        "gate will fail on every deploy"
    )


def test_the_release_workflows_do_not_dispatch_anything():
    """Two blockers, one cause. `create-release.yml` dispatched `deploy-prod.yml` (a SECOND deploy,
    since the operator's own tag push already triggers it) and `publish-public.yml` — which
    force-pushes a lightweight `git tag -f` and so replaced the freshly signed mirror tag with an
    unsigned one, destroying the signature the cut had just produced."""
    body = (WORKFLOWS / "create-release.yml").read_text()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "dispatches" not in stripped, f"create-release.yml dispatches again: {stripped!r}"


def test_publish_public_refuses_to_republish_a_release_tag():
    """The force-push is the landmine, not the dispatch. Removing the caller leaves a workflow that
    still destroys a signature when someone dispatches it by hand, so the refusal lives at the
    point of damage."""
    body = (WORKFLOWS / "publish-public.yml").read_text()
    code = _uncommented(body)
    assert (
        "git tag -f" not in code
    ), "publish-public.yml still force-tags — it will unsign the mirror"
    assert (
        "Refusing" in code and "v[0-9]*" in code
    ), "publish-public.yml must refuse a v* ref outright, not merely omit the tagging"


def test_the_installer_smoke_gates_the_cut_rather_than_trailing_it():
    """The smoke used to block the tag from `create-release.yml`. Now the operator's tag push IS
    the deploy trigger, so a gate in a manually-dispatched workflow runs after production has
    started updating. It has to sit in the cut, ahead of the push."""
    cut = (REPO / "scripts/cut-signed-release").read_text()
    assert "smoke-install" in cut, "the installer smoke no longer gates the release cut"
    assert cut.index("smoke-install") < cut.index(
        'git push "$ORIGIN"'
    ), "the smoke runs after the deploy-triggering push — it gates nothing"
    assert "smoke-install" not in _uncommented(
        (WORKFLOWS / "create-release.yml").read_text()
    ), "create-release.yml still runs a smoke that cannot block anything"


# ---- the assembled gate, executed ----------------------------------------------


def _bare_world(tmp: Path):
    """An origin whose `main` carries the gate, plus a shallow tag checkout — the real shape."""
    good = _keygen(tmp / "key")
    bad = _keygen(tmp / "bad")
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp / "work"
    work.mkdir()

    def git(*a, **kw):
        subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True, **kw)

    git("init", "-q", "-b", "main", ".")
    git("config", "user.name", "t")
    git("config", "user.email", "t@e")
    git("config", "gpg.format", "ssh")
    (work / "scripts").mkdir()
    (work / "scripts/verify-release-tag").write_bytes(VERIFY.read_bytes())
    (work / "scripts/release-signers").write_text(
        f"release@agent-sessions {(tmp / 'key.pub').read_text().strip()}\n"
    )
    (work / "f").write_text("app")
    git("add", "-A")
    git("commit", "-qm", "c")
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (work / "scripts/release-trust.json").write_text(
        json.dumps(
            {
                "version": 1,
                "namespace": "git",
                "last_unsigned_release": "v0.19.2",
                "unsigned_releases": {"v0.19.0": head, "v0.19.2": head},
            }
        )
    )
    git("add", "-A")
    git("commit", "-qm", "trust")
    # Tag the FIRST commit explicitly. Committing the trust record moves HEAD, so pinning "HEAD"
    # and then tagging would pin one commit and tag another — the exemption would never match.
    # Real historical releases point at old commits anyway, so this is the honest shape too.
    git("tag", "-a", "v0.19.0", head, "-m", "old")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "-u", "origin", "main")
    return work, origin, good, bad


def _gate_script() -> str:
    """The gate as the workflow actually runs it — extracted, never re-typed."""
    steps = _steps("deploy-prod.yml")
    return next(s["run"] for s in steps if "verify-release-tag" in (s.get("run") or ""))


def _deploys(tmp: Path, origin: Path, tag: str) -> subprocess.CompletedProcess:
    co = tmp / "co"
    if co.exists():
        subprocess.run(["rm", "-rf", str(co)], check=True)
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", tag, f"file://{origin}", str(co)],
        check=True,
        capture_output=True,
    )
    gate = tmp / "gate.sh"
    gate.write_text(_gate_script())
    # bash, not sh: Forgejo runs `run:` blocks with bash, and the block uses `set -o pipefail`.
    # Running it under dash fails on line 1 and refuses EVERYTHING — which looks exactly like a
    # working fail-closed gate until you read the message.
    return subprocess.run(
        ["bash", str(gate)],
        cwd=co,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_REF": f"refs/tags/{tag}", "GITHUB_REF_NAME": tag},
    )


@needs_ssh_keygen
def test_the_assembled_deploy_gate_refuses_a_tag_that_supplies_its_own_trust_root(tmp_path):
    """The composition, not the parts — and the one case the whole design turns on.

    Every other test here checks a piece: the verifier's logic, the step's position, the fetch
    refspec. This executes the workflow's actual `run:` block against a real shallow tag checkout,
    which is the only way to catch a break in how those pieces fit — an extraction that silently
    yields nothing, a fetch that reports success without creating `origin/main`, a shell builtin
    the runner's shell lacks.

    The attacker here does the obvious thing: puts THEIR OWN public key in the tag's
    `scripts/release-signers` and signs the tag with the matching private key. Everything in that
    tag's tree is internally consistent, and a gate reading its root from the checkout would
    verify it happily. It must be refused, because the root comes from `origin/main`.
    """
    work, origin, good, bad = _bare_world(tmp_path)

    def git(*a):
        subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True)

    git("-c", f"user.signingkey={good}", "tag", "-s", "v0.20.0", "-m", "v0.20.0")
    git("push", "-q", "origin", "refs/tags/v0.20.0")
    assert _deploys(tmp_path, origin, "v0.20.0").returncode == 0, (
        "positive control failed: a properly signed tag must deploy, or every refusal below "
        "passes for the wrong reason"
    )

    # The attack: swap the trust root inside the tag's own tree, sign with the matching key.
    (work / "scripts/release-signers").write_text(
        f"release@agent-sessions {(tmp_path / 'bad.pub').read_text().strip()}\n"
    )
    git("add", "-A")
    git("commit", "-qm", "attacker swaps the trust root")
    git("-c", f"user.signingkey={bad}", "tag", "-s", "v0.88.8", "-m", "v0.88.8")
    git("push", "-q", "origin", "refs/tags/v0.88.8")

    r = _deploys(tmp_path, origin, "v0.88.8")
    assert r.returncode != 0, (
        "a tag carrying its own trust root was accepted — the gate is verifying the artefact "
        "against itself"
    )
    assert "REFUSED" in (r.stdout + r.stderr)


@needs_ssh_keygen
def test_the_assembled_deploy_gate_lets_a_pre_cutover_tag_through(tmp_path):
    """Rollback to a shipped release must keep working; every existing release is unsigned."""
    work, origin, _good, _bad = _bare_world(tmp_path)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "refs/tags/v0.19.0"],
        check=True,
        capture_output=True,
    )
    assert _deploys(tmp_path, origin, "v0.19.0").returncode == 0


# ---- Hermes on #1206: the same two bypasses the installer had -------------------------


@needs_ssh_keygen
def test_a_signed_release_relabelled_under_a_higher_name_is_refused(tmp_path):
    good = _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    _tag(repo, "v0.20.0", good)
    obj = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/tags/v0.20.0"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/tags/v0.99.0", obj], check=True)
    assert _run(repo, "v0.20.0").returncode == 0
    r = _run(repo, "v0.99.0")
    assert r.returncode == 1
    assert "relabelled" in r.stderr


@needs_ssh_keygen
@pytest.mark.skipif(
    subprocess.run(["which", "gpg"], capture_output=True).returncode != 0, reason="gpg absent"
)
def test_a_pgp_signed_tag_is_refused_even_with_its_key_in_the_keyring(tmp_path):
    _keygen(tmp_path / "good")
    repo = _repo(tmp_path, tmp_path / "good.pub")
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(gnupg)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            "pgp@example.invalid",
            "ed25519",
            "sign",
            "never",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "gpg.format=openpgp",
            "-c",
            "user.signingkey=pgp@example.invalid",
            "tag",
            "-s",
            "v0.20.0",
            "-m",
            "v0.20.0",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    r = subprocess.run(
        ["sh", str(VERIFY), "v0.20.0"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SSH" in r.stderr


@needs_ssh_keygen
def test_the_assembled_deploy_gate_refuses_a_checkout_the_tag_moved_away_from(tmp_path):
    """Hermes on #1206: the job checks out the tag, then the gate re-fetches and verifies it.
    Point the tag at unsigned B for the checkout, restore the genuine signed A before the gate's
    fetch: A verifies, and the next step would run B's install.sh. The gate must refuse."""
    work, origin, good, _bad = _bare_world(tmp_path)

    def git(repo, *a):
        return subprocess.run(
            ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git(work, "-c", f"user.signingkey={good}", "tag", "-s", "v0.20.0", "-m", "v0.20.0")
    git(work, "push", "-q", "origin", "refs/tags/v0.20.0")
    signed_obj = git(origin, "rev-parse", "refs/tags/v0.20.0")
    assert _deploys(tmp_path, origin, "v0.20.0").returncode == 0  # positive control

    (work / "f").write_text("attacker's app\n")
    git(work, "commit", "-qam", "B — never signed")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/attacker")  # B's objects reach the origin
    git(origin, "update-ref", "refs/tags/v0.20.0", git(work, "rev-parse", "HEAD"))  # moved to B
    co = tmp_path / "co-race"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "v0.20.0", f"file://{origin}", str(co)],
        check=True,
        capture_output=True,
    )
    git(origin, "update-ref", "refs/tags/v0.20.0", signed_obj)  # restored before the gate fetches
    gate = tmp_path / "gate-race.sh"
    gate.write_text(_gate_script())
    r = subprocess.run(
        ["bash", str(gate)],
        cwd=co,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_REF": "refs/tags/v0.20.0", "GITHUB_REF_NAME": "v0.20.0"},
    )
    assert r.returncode != 0, "the gate verified A and let B's checkout through to install.sh"
    assert "the tag moved during the job" in (r.stdout + r.stderr)
