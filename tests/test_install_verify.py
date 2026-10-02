"""Release signature verification in the installer and the updater (#832 Phases 2-4).

Everything here runs the REAL `install.sh` code against REAL signed tags in scratch
repositories, with throwaway keys. The only thing changed in the installer under test is its
embedded trust root (`RELEASE_SIGNERS` / `RELEASE_UNSIGNED_PINS`), swapped by rewriting a copy
of the script — the production script has no way to take a trust root from its environment,
and these tests must not need one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_sessions import update

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("ssh-keygen")), reason="git + ssh-keygen required"
)

_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**_base_env(repo.parent), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *_ID, *args], capture_output=True, text=True, check=check, env=env
    )


def _base_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C"}


def _keygen(d: Path, name: str) -> Path:
    key = d / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True
    )
    return key


def _signer_line(key: Path) -> str:
    return "release@agent-sessions " + key.with_suffix(".pub").read_text().strip()


class World:
    """A scratch remote plus keys. Tags are created with the real git signing path."""

    def __init__(self, root: Path):
        self.root = root
        self.remote = root / "remote"
        self.remote.mkdir()
        _git(self.remote, "init", "-q", "-b", "main")
        self.k_old = _keygen(root, "old")
        self.k_new = _keygen(root, "new")
        self.k_evil = _keygen(root, "evil")
        self.commit("first")

    def commit(self, msg: str, files: dict[str, str] | None = None) -> str:
        for rel, body in (files or {}).items():
            p = self.remote / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            _git(self.remote, "add", rel)
        _git(self.remote, "commit", "-q", "--allow-empty", "-m", msg)
        return _git(self.remote, "rev-parse", "HEAD").stdout.strip()

    def tag(self, name: str, key: Path | None = None, *, lightweight: bool = False) -> None:
        if lightweight:
            _git(self.remote, "tag", name)
        elif key is None:
            _git(self.remote, "tag", "-a", name, "-m", name)
        else:
            _git(
                self.remote,
                "-c",
                "gpg.format=ssh",
                "-c",
                f"user.signingkey={key}",
                "tag",
                "-s",
                name,
                "-m",
                name,
            )
            # `git tag -s` exits 0 even when signing failed — never trust it (release-signing.md)
            assert "SSH SIGNATURE" in _git(self.remote, "cat-file", "tag", name).stdout

    def tamper(self, name: str) -> None:
        """Rewrite a signed tag's message while keeping its signature block."""
        body = _git(self.remote, "cat-file", "tag", name).stdout.replace(f"\n{name}\n", "\nevil\n")
        obj = subprocess.run(
            ["git", "-C", str(self.remote), "hash-object", "-t", "tag", "-w", "--stdin"],
            input=body,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _git(self.remote, "update-ref", f"refs/tags/{name}", obj)


def make_installer(
    dst: Path, signers: list[Path] | None, pins: list[str] = (), *, raw_signers: str | None = None
) -> Path:
    text = INSTALL_SH.read_text()
    root = raw_signers if raw_signers is not None else "\n".join(map(_signer_line, signers or []))
    text, n1 = re.subn(
        r"^RELEASE_SIGNERS='[^']*'$", lambda _m: f"RELEASE_SIGNERS='{root}'", text, flags=re.M
    )
    text, n2 = re.subn(
        r"^RELEASE_UNSIGNED_PINS='[^']*'$",
        lambda _m: "RELEASE_UNSIGNED_PINS='" + "\n".join(pins) + "'",
        text,
        flags=re.M,
    )
    assert n1 == 1 and n2 == 1, "installer trust-root constants not found"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    return dst


def verify(world: World, installer: Path, tag: str, **env_extra: str) -> tuple[int, str]:
    env = {**_base_env(world.root), "AGENT_SESSIONS_REPO": str(world.remote), **env_extra}
    r = subprocess.run(
        ["sh", str(installer), "--verify-release", tag],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---- install.sh --verify-release ---------------------------------------------------


def test_a_tag_signed_by_a_trusted_key_verifies(world, tmp_path):
    world.tag("v1.0.0", world.k_old)
    rc, out = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0")
    assert rc == 0, out
    assert "signed by a trusted release key" in out


def test_either_key_of_a_two_key_root_verifies(world, tmp_path):
    world.tag("v1.0.0", world.k_new)
    inst = make_installer(tmp_path / "i.sh", [world.k_old, world.k_new])
    assert verify(world, inst, "v1.0.0")[0] == 0


def test_an_unknown_signer_is_the_only_steppable_answer(world, tmp_path):
    world.tag("v1.0.0", world.k_new)
    rc, out = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0")
    assert rc == 3, out
    assert "does not trust" in out


@pytest.mark.parametrize("shape", ["tampered", "unsigned", "lightweight"])
def test_missing_or_invalid_signatures_refuse_outright(world, tmp_path, shape):
    if shape == "tampered":
        world.tag("v1.0.0", world.k_old)
        world.tamper("v1.0.0")
    elif shape == "unsigned":
        world.tag("v1.0.0")
    else:
        world.tag("v1.0.0", lightweight=True)
    rc, out = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0")
    assert rc == 1, out  # refused — never 3, which the updater would step past


@pytest.mark.parametrize("root", ["", "# only a comment\n"])
def test_an_empty_trust_root_refuses_and_is_never_steppable(world, tmp_path, root):
    # The trap: git prints "No principal matched" for an empty or missing root too. Were that read
    # as unknown-signer, emptying the root would turn every update into a downgrade walk.
    world.tag("v1.0.0", world.k_old)
    rc, out = verify(world, make_installer(tmp_path / "i.sh", None, raw_signers=root), "v1.0.0")
    assert rc == 1, out
    assert "no release signer" in out


def test_the_trust_root_never_comes_from_the_candidate(world, tmp_path):
    # The candidate ships its own signer list naming the attacker's key, and is signed by it.
    world.commit(
        "attacker",
        {
            "scripts/release-signers": _signer_line(world.k_evil) + "\n",
            "install.sh": "RELEASE_SIGNERS='" + _signer_line(world.k_evil) + "'\n",
        },
    )
    world.tag("v1.0.0", world.k_evil)
    rc, out = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0")
    assert rc != 0, out


def test_the_operators_git_config_cannot_change_the_verdict(world, tmp_path):
    # A global gpg.ssh.program that always succeeds would otherwise "verify" anything.
    world.tag("v1.0.0", world.k_evil)
    (tmp_path / ".gitconfig").write_text(
        '[gpg "ssh"]\n\tprogram = /bin/true\n\tallowedSignersFile = /dev/null\n'
    )
    rc, out = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0")
    assert rc == 3, out


def test_a_pinned_pre_signing_release_needs_no_signature(world, tmp_path):
    world.tag("v0.9.0")  # annotated, unsigned — like every release before the cutover
    sha = _git(world.remote, "rev-parse", "v0.9.0^{commit}").stdout.strip()
    inst = make_installer(tmp_path / "i.sh", [world.k_old], [f"v0.9.0 {sha}"])
    rc, out = verify(world, inst, "v0.9.0")
    assert rc == 0, out
    assert "pre-signing release" in out


def test_a_moved_pre_signing_release_refuses(world, tmp_path):
    first = _git(world.remote, "rev-parse", "HEAD").stdout.strip()
    world.commit("moved")
    world.tag("v0.9.0")
    inst = make_installer(tmp_path / "i.sh", [world.k_old], [f"v0.9.0 {first}"])
    rc, out = verify(world, inst, "v0.9.0")
    assert rc == 1, out
    assert "has been moved" in out


def test_a_pin_for_another_tag_does_not_exempt(world, tmp_path):
    # The pair is the record: the right commit under a different release name is not exempt.
    world.tag("v0.9.1")
    sha = _git(world.remote, "rev-parse", "v0.9.1^{commit}").stdout.strip()
    inst = make_installer(tmp_path / "i.sh", [world.k_old], [f"v0.9.0 {sha}"])
    assert verify(world, inst, "v0.9.1")[0] == 1


def test_either_remotes_commit_for_a_pinned_release_is_exempt(world, tmp_path):
    # A release has one commit per remote; the installer lists both, and either is that release.
    world.tag("v0.9.0")
    sha = _git(world.remote, "rev-parse", "v0.9.0^{commit}").stdout.strip()
    pins = ["v0.9.0 " + "0" * 40, f"v0.9.0 {sha}"]
    assert verify(world, make_installer(tmp_path / "i.sh", [world.k_old], pins), "v0.9.0")[0] == 0


def test_a_version_below_the_cutover_is_not_exempt_by_ordering(world, tmp_path):
    # `v0.0.0` sorts below every real release. Exemption is membership, never ordering.
    world.tag("v0.0.0")
    inst = make_installer(tmp_path / "i.sh", [world.k_old], [])
    assert verify(world, inst, "v0.0.0")[0] == 1


def test_a_missing_ssh_keygen_refuses_naming_the_package(world, tmp_path):
    world.tag("v1.0.0", world.k_old)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for tool in ("git", "sed", "awk", "grep", "mktemp", "rm", "tr", "sh", "cat"):
        path = shutil.which(tool)
        if path:
            (fakebin / tool).symlink_to(path)
    rc, out = verify(
        world, make_installer(tmp_path / "i.sh", [world.k_old]), "v1.0.0", PATH=str(fakebin)
    )
    assert rc == 1, out
    assert "openssh-client" in out


def test_verify_mode_rejects_a_non_release_ref(world, tmp_path):
    rc, _ = verify(world, make_installer(tmp_path / "i.sh", [world.k_old]), "main")
    assert rc == 2


def test_verify_mode_touches_no_install_state(world, tmp_path):
    world.tag("v1.0.0", world.k_old)
    prefix = tmp_path / "prefix"
    verify(
        world,
        make_installer(tmp_path / "i.sh", [world.k_old]),
        "v1.0.0",
        AGENT_SESSIONS_HOME=str(prefix),
    )
    assert not prefix.exists()


# ---- the build-time gate ------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 required")
def test_the_installer_refuses_an_unsigned_release_before_building(world, tmp_path):
    world.tag("v1.0.0")  # annotated but unsigned
    prefix = tmp_path / "prefix"
    inst = make_installer(tmp_path / "i.sh", [world.k_old])
    env = {
        **_base_env(tmp_path),
        "AGENT_SESSIONS_REPO": str(world.remote),
        "AGENT_SESSIONS_REF": "v1.0.0",
        "AGENT_SESSIONS_HOME": str(prefix),
        "AGENT_SESSIONS_NO_SERVICE": "1",
        "AGENT_SESSIONS_SKIP_WEB_BUILD": "1",
        "AGENT_SESSIONS_ASSUME_YES": "1",
        "AGENT_SESSIONS_HOST": "127.0.0.1",
    }
    r = subprocess.run(
        ["sh", str(inst)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert r.returncode != 0
    assert "refusing to build v1.0.0" in r.stderr, r.stderr[-2000:]
    assert not (prefix / "releases").exists() or not any((prefix / "releases").iterdir())
    assert not (prefix / "current").exists()


# ---- the updater: pre-spawn check, blocked, and the rotation walk ---------------------


@pytest.fixture
def installed(world, tmp_path, monkeypatch):
    """An install whose `current` release carries a given trust root."""
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env-file"))
    monkeypatch.setenv("AGENT_SESSIONS_CHANNEL", "stable")

    def install(signers: list[Path], version: str) -> None:
        make_installer(home / "current" / "src" / "install.sh", signers)
        monkeypatch.setattr(update, "get_version", lambda: version)

    return install


def test_verify_signature_maps_the_installer_answers(world, installed):
    world.tag("v1.0.0", world.k_old)
    world.tag("v1.1.0", world.k_new)
    world.tag("v1.2.0")
    installed([world.k_old], "0.9.0")
    url = str(world.remote)
    assert update.verify_signature("v1.0.0", url)[0] == update.SIG_OK
    assert update.verify_signature("v1.1.0", url)[0] == update.SIG_UNKNOWN_SIGNER
    assert update.verify_signature("v1.2.0", url)[0] == update.SIG_REFUSED


def test_an_installer_without_the_check_fails_closed(world, installed, tmp_path):
    world.tag("v1.0.0", world.k_old)
    installed([world.k_old], "0.9.0")
    inst = tmp_path / "home" / "current" / "src" / "install.sh"
    inst.write_text(inst.read_text().replace("verify_release_only", "something_else"))
    status, why = update.verify_signature("v1.0.0", str(world.remote))
    assert status == update.SIG_REFUSED
    assert "predates" in why


def test_a_dormant_install_crosses_a_rotation_via_the_bridge(world, installed):
    # v1.0.0 old key · v1.1.0 BRIDGE (old key signs; its installer trusts both) · v1.2.0 new key
    world.tag("v1.0.0", world.k_old)
    world.commit("bridge")
    world.tag("v1.1.0", world.k_old)
    world.commit("after rotation")
    world.tag("v1.2.0", world.k_new)
    url = str(world.remote)

    installed([world.k_old], "1.0.0")  # switched off across the whole transition
    tag, commit, why = update.select_signed_target(url)
    assert tag == "v1.1.0", why
    assert commit == _git(world.remote, "rev-parse", "v1.1.0^{commit}").stdout.strip()

    installed([world.k_old, world.k_new], "1.1.0")  # the bridge is now installed
    tag, _commit, why = update.select_signed_target(url)
    assert tag == "v1.2.0", why


def test_no_bridge_is_a_hard_refusal_never_a_reinstall_or_downgrade(world, installed):
    world.tag("v0.9.0", world.k_old)  # verifiable, but OLDER than what runs
    world.commit("x")
    world.tag("v1.0.0", world.k_old)  # verifiable, but what already runs
    world.commit("y")
    world.tag("v1.2.0", world.k_new)  # the newest, by a key this install does not trust
    installed([world.k_old], "1.0.0")
    tag, commit, why = update.select_signed_target(str(world.remote))
    assert (tag, commit) == (None, None)
    assert "no newer release this installation can verify" in why


def test_an_invalid_newest_signature_refuses_without_walking(world, installed):
    world.tag("v1.1.0", world.k_old)  # a perfectly good lower release exists…
    world.commit("x")
    world.tag("v1.2.0", world.k_old)
    world.tamper("v1.2.0")  # …but the newest is forged: that is an attack, not a rotation
    installed([world.k_old], "1.0.0")
    tag, _c, why = update.select_signed_target(str(world.remote))
    assert tag is None
    assert "refusing to update" in why


def test_the_walk_stops_at_an_invalid_candidate(world, installed):
    world.tag("v1.0.5", world.k_old)
    world.commit("x")
    world.tag("v1.1.0")  # unsigned — not a rotation, not steppable
    world.commit("y")
    world.tag("v1.2.0", world.k_new)
    installed([world.k_old], "1.0.0")
    tag, _c, why = update.select_signed_target(str(world.remote))
    assert tag is None  # never walks past v1.1.0 to v1.0.5
    assert "refusing to update" in why


def _spy_spawn(monkeypatch, record):
    """Intercept ONLY the detached installer spawn (`[sh, install.sh]`, no arguments). The
    signature check runs the same installer through subprocess.run, which uses Popen too."""
    real = update.subprocess.Popen

    def popen(argv, *a, **k):
        if len(argv) == 2 and str(argv[1]).endswith("install.sh"):
            record(argv, k.get("env"))
            return object()
        return real(argv, *a, **k)

    monkeypatch.setattr(update.subprocess, "Popen", popen)


def test_apply_surfaces_a_signature_refusal_as_blocked(world, installed, monkeypatch):
    world.tag("v1.2.0")  # unsigned
    installed([world.k_old], "1.0.0")
    monkeypatch.setattr(update, "_repo_url", lambda: str(world.remote))
    spawned = []
    _spy_spawn(monkeypatch, lambda argv, env: spawned.append(argv))
    assert update.apply() is False
    assert spawned == []
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v1.2.0")
    assert "not signed with an SSH release key" in update.check()["blocked"]


def test_apply_spawns_the_verified_target_pinned(world, installed, monkeypatch):
    world.tag("v1.2.0", world.k_old)
    installed([world.k_old], "1.0.0")
    monkeypatch.setattr(update, "_repo_url", lambda: str(world.remote))
    monkeypatch.setattr(update, "_seed_progress", lambda _t: None)
    captured = {}
    _spy_spawn(monkeypatch, lambda argv, env: captured.update(env))
    assert update.apply() is True
    assert captured["AGENT_SESSIONS_REF"] == "v1.2.0"
    assert (
        captured["AGENT_SESSIONS_EXPECT_COMMIT"]
        == _git(world.remote, "rev-parse", "v1.2.0^{commit}").stdout.strip()
    )


# ---- the failure classifier, against git's real output ---------------------------------


def _classify(rc: int, out: str) -> str:
    body = INSTALL_SH.read_text()
    fn = re.search(r"^_classify_verify\(\) \{\n.*?^\}\n", body, flags=re.M | re.S).group(0)
    r = subprocess.run(
        ["sh", "-c", fn + '_classify_verify "$1" "$2"', "sh", str(rc), out],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _real_verify(world: World, tag: str, signers: Path) -> tuple[int, str]:
    r = subprocess.run(
        ["git", "-C", str(world.remote), "-c", f"gpg.ssh.allowedSignersFile={signers}"]
        + ["-c", "gpg.ssh.program=ssh-keygen", "verify-tag", tag],
        capture_output=True,
        text=True,
        env={**_base_env(world.root), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def test_the_classifier_on_real_git_output(world, tmp_path):
    """Pins git's English strings AND the verdict for each: a tooling upgrade that rewords one
    fails here instead of silently reclassifying. The dangerous row is the missing root, which
    ALSO prints "No principal matched" — it must never read as a steppable unknown signer."""
    world.tag("vgood", world.k_old)
    world.tag("vother", world.k_new)
    world.tag("vtamper", world.k_old)
    world.tamper("vtamper")
    world.tag("vunsigned")
    world.tag("vlight", lightweight=True)
    root = tmp_path / "signers"
    root.write_text(_signer_line(world.k_old) + "\n")
    empty = tmp_path / "empty"
    empty.write_text("")
    cases = {
        ("vgood", root): "ok",
        ("vother", root): "unknown-signer",
        ("vtamper", root): "refused",
        ("vunsigned", root): "refused",
        ("vlight", root): "refused",
        ("vgood", tmp_path / "no-such-root"): "refused",  # missing root: never steppable
    }
    for (tag, signers), want in cases.items():
        rc, out = _real_verify(world, tag, signers)
        assert _classify(rc, out) == want, (tag, signers.name, rc, out)
    # An empty root is refused before verify-tag ever runs (the installer checks it), but the
    # classifier alone would call it unknown-signer — which is why that earlier check exists.
    rc, out = _real_verify(world, "vgood", empty)
    assert out.splitlines() == [out.splitlines()[0], "No principal matched."]


def test_the_classifier_refuses_anything_unrecognised():
    unknown = 'Good "git" signature with ED25519 key SHA256:x\nNo principal matched.'
    assert _classify(1, unknown) == "unknown-signer"
    assert _classify(1, unknown + "\nsomething new") == "refused"
    assert _classify(1, "No principal matched.") == "refused"
    assert _classify(1, "") == "refused"
    assert _classify(0, "") == "ok"


# ---- Hermes on #1206: four bypasses, each reproduced here before its fix ------------------


def _fn(name: str) -> str:
    body = INSTALL_SH.read_text()
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", body, flags=re.M | re.S).group(0)


def test_stable_resolves_the_full_tag_name_never_a_branch(world):
    # A tag `v999/z` used to resolve to its last path segment, `z` — a BRANCH, built unverified.
    world.tag("v1.0.0", world.k_old)
    world.commit("unsigned branch")
    _git(world.remote, "branch", "z")
    _git(world.remote, "tag", "v999/z")
    script = _fn("resolve_ref") + 'REF=""; CHANNEL=stable; REPO_URL="$1"; resolve_ref'
    r = subprocess.run(
        ["sh", "-c", script, "sh", str(world.remote)], capture_output=True, text=True
    )
    assert r.stdout.strip() == "v1.0.0"
    out = _git(world.remote, "ls-remote", "--tags", "--refs", str(world.remote), "v*").stdout
    assert update._release_tags(out) == ["v1.0.0"]


def test_stable_with_no_release_tag_resolves_nothing(world):
    _git(world.remote, "branch", "z")
    _git(world.remote, "tag", "v999/z")
    script = _fn("resolve_ref") + 'REF=""; CHANNEL=stable; REPO_URL="$1"; resolve_ref'
    r = subprocess.run(
        ["sh", "-c", script, "sh", str(world.remote)], capture_output=True, text=True
    )
    assert r.stdout.strip() == ""  # and main() refuses on an empty stable ref
    assert 'die "no release tag (vX.Y.Z) found on' in INSTALL_SH.read_text()


def test_a_branch_named_like_the_release_is_never_built(world, tmp_path):
    # Signed tag v1.0.0 on commit A, and a BRANCH v1.0.0 on unsigned commit B: `clone --branch`
    # picks the branch. The tag verifies; what would be built is B.
    world.tag("v1.0.0", world.k_old)
    world.commit("B — not what was signed", {"marker": "B\n"})
    _git(world.remote, "branch", "v1.0.0")
    src = tmp_path / "src.sh"
    src.write_text(INSTALL_SH.read_text().replace('\nmain "$@"\n', "\n"))
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    r = subprocess.run(
        [
            "sh",
            "-c",
            f'. "{src}"; RELEASE_SIGNERS="$SIGNERS"; PY=/nonexistent; build_release v1.0.0',
        ],
        capture_output=True,
        text=True,
        env={
            **_base_env(tmp_path),
            "AGENT_SESSIONS_HOME": str(prefix),
            "AGENT_SESSIONS_REPO": str(world.remote),
            "AGENT_SESSIONS_NO_SERVICE": "1",
            "SIGNERS": _signer_line(world.k_old),
        },
    )
    assert r.returncode != 0
    assert "refusing to build v1.0.0" in r.stderr and "verified tag" in r.stderr, r.stderr
    assert not (prefix / "releases").exists() or not any((prefix / "releases").iterdir())


def test_an_old_signed_release_relabelled_as_newer_refuses(world, tmp_path):
    # refs/tags/v9.0.0 → the genuine, trusted-signed v1.0.0 OBJECT: a valid signature, replayed.
    world.tag("v1.0.0", world.k_old)
    obj = _git(world.remote, "rev-parse", "refs/tags/v1.0.0").stdout.strip()
    _git(world.remote, "update-ref", "refs/tags/v9.0.0", obj)
    inst = make_installer(tmp_path / "i.sh", [world.k_old])
    assert verify(world, inst, "v1.0.0")[0] == 0
    rc, out = verify(world, inst, "v9.0.0")
    assert rc == 1, out
    assert "relabelled" in out


@pytest.mark.skipif(not shutil.which("gpg"), reason="gpg required")
def test_a_pgp_signature_never_substitutes_for_the_ssh_root(world, tmp_path):
    # git picks the verifier from the signature format: a PGP-signed tag was checked against the
    # operator's GPG keyring — here one holding ONLY the (untrusted) signer's public key.
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700)
    genv = {**_base_env(tmp_path), "GNUPGHOME": str(gnupg)}
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
        env=genv,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(world.remote),
            *_ID,
            "-c",
            "gpg.format=openpgp",
            "-c",
            "user.signingkey=pgp@example.invalid",
            "tag",
            "-s",
            "v1.0.0",
            "-m",
            "v1.0.0",
        ],
        env={**genv, "GIT_CONFIG_NOSYSTEM": "1"},
        check=True,
        capture_output=True,
    )
    inst = make_installer(tmp_path / "i.sh", [world.k_old])
    rc, out = verify(world, inst, "v1.0.0", GNUPGHOME=str(gnupg))
    assert rc == 1, out
    assert "SSH" in out
