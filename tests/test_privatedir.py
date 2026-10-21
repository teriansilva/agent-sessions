"""The trust-anchor policy behind `gitwrite.hooks_void()` (#1006).

The rule under test is two-part and the split is the whole design: **above** the anchor an
ancestor must be a real directory, owned by a trusted owner (root or us), and not writable by
any other account; **at and below** it every component must be ours outright. An over-literal
"every ancestor is ours" rule would reject an ordinary install, because `/` and `/home` are
root-owned — hence the positive default-home case here alongside the negative ones.

Two constraints shape how these are written. The suite cannot `chown`, so the ownership matrix is
asserted against the predicate with synthetic `stat` results (creating a directory owned by an
unrelated uid needs privileges this process does not have and must never acquire); everything else
runs against real directories. And `tmp_path` lives under `/tmp`, which the policy refuses by
design — so the harness declares a test root, which is exactly what the policy test below pins as
having no production spelling.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from agent_sessions import privatedir
from agent_sessions.privatedir import PrivateDirError, TrustPolicy


def _fake_stat(*, uid: int, mode: int, gid: int = 0) -> os.stat_result:
    """A `stat_result` with a chosen owner, group and mode — see the module docstring for why."""
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def _stranger(euid: int) -> int:
    """A uid that is neither root nor us."""
    return euid + 4242


# --------------------------------------------------------------------------- the positive case


def test_a_default_home_install_passes_with_root_owned_ancestors():
    """The case an over-literal ancestor rule breaks, asserted against the REAL ancestry.

    `$HOME` on an ordinary install sits under a root-owned `/` and `/home`. This runs the
    PRODUCTION policy (the harness's `/tmp` test root does not apply, because `$HOME` is not
    inside it), reads only — `lstat`, no creation, nothing written — and must pass.
    """
    home = Path.home()
    euid = os.geteuid()
    st = os.lstat(home)
    # The guard matches the POLICY exactly: any foreign-write bit disqualifies, ACLs included (an
    # ACL mask surfaces as the group bit). A group-writable home is genuinely unsafe, so skipping
    # there is honest rather than a hollowed-out assertion.
    if st.st_uid != euid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        pytest.skip(f"{home} is not a private home on this host (uid={st.st_uid})")

    assert privatedir.verify_private_dir(home, policy=privatedir.PRODUCTION) == str(home)
    # And the ancestry really is the root-owned shape this test exists to cover.
    assert any(os.lstat(p).st_uid == 0 for p in home.parents)


# --------------------------------------------------------------------------- the ownership matrix


def test_a_root_owned_ancestor_is_trusted_and_an_unrelated_owner_is_not():
    euid = os.geteuid()
    privatedir._check_above_anchor(Path("/x"), _fake_stat(uid=0, mode=0o755), euid)
    privatedir._check_above_anchor(Path("/x"), _fake_stat(uid=euid, mode=0o755), euid)
    foreign = _fake_stat(uid=_stranger(euid), mode=0o755)
    with pytest.raises(PrivateDirError, match="neither root nor this process"):
        privatedir._check_above_anchor(Path("/x"), foreign, euid)


def test_mode_alone_is_not_enough_above_the_anchor():
    """Trusted ownership **and** mode, as a correction to the issue's earlier mode-only wording.

    A `0755` directory owned by a stranger passes every mode check and is still one `chmod` away
    from `0777` — a change we would never observe. The mode is a fact about this instant; the
    ownership is the fact about who may revoke it.
    """
    euid = os.geteuid()
    tidy_but_foreign = _fake_stat(uid=_stranger(euid), mode=0o755)
    assert not tidy_but_foreign.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    with pytest.raises(PrivateDirError, match="untrusted owner"):
        privatedir._check_above_anchor(Path("/x"), tidy_but_foreign, euid)


def test_an_other_writable_ancestor_is_refused():
    """`other` write is fatal outright — every local account qualifies, no lookup needed."""
    euid = os.geteuid()
    for mode in (0o757, 0o777, 0o707):
        with pytest.raises(PrivateDirError, match="other-writable"):
            privatedir._check_above_anchor(Path("/x"), _fake_stat(uid=0, mode=mode), euid)


def test_at_and_below_the_anchor_root_owned_is_not_good_enough():
    euid = os.geteuid()
    privatedir._check_at_or_below_anchor(Path("/x"), _fake_stat(uid=euid, mode=0o700), euid)
    with pytest.raises(PrivateDirError, match="not by this process"):
        privatedir._check_at_or_below_anchor(Path("/x"), _fake_stat(uid=0, mode=0o755), euid)
    with pytest.raises(PrivateDirError, match="other-writable"):
        privatedir._check_at_or_below_anchor(Path("/x"), _fake_stat(uid=euid, mode=0o702), euid)


def test_a_wrong_owner_directory_at_the_anchor_is_refused():
    """The same rule against a real directory: `/usr` is root's, and at the anchor that is not
    us."""
    if os.geteuid() == 0:
        pytest.skip("running as root — /usr is then owned by the effective uid")
    with pytest.raises(PrivateDirError, match="not by this process"):
        privatedir.verify_private_dir("/usr", policy=TrustPolicy(test_roots=("/usr",)))


# --------------------------------------------------------------- what "writable by another" means


def test_group_write_is_refused_outright():
    """There is no private-group exception, and the attempt to build one is why.

    An earlier cut admitted group-write when the owning group looked exclusive. It could not be
    made sound (PR #1013 review): the mode bits can be an ACL **mask** rather than the owning
    group's permissions, and NSS enumeration cannot prove a group has no other members. Refusing
    the bit removes both failure modes at once, and removes the code that asked.
    """
    euid = os.geteuid()
    for mode in (0o770, 0o775):
        with pytest.raises(PrivateDirError, match="group-writable"):
            privatedir._check_at_or_below_anchor(
                Path("/x"), _fake_stat(uid=euid, mode=mode, gid=euid), euid
            )
        with pytest.raises(PrivateDirError, match="group-writable"):
            privatedir._check_above_anchor(Path("/x"), _fake_stat(uid=0, mode=mode, gid=0), euid)


@pytest.mark.skipif(not shutil.which("setfacl"), reason="setfacl required")
def test_a_named_user_acl_cannot_smuggle_write_past_the_mode_check(tmp_path):
    """MEASURED on a real filesystem, and the reason the private-group exception had to go.

    With an extended POSIX ACL the stat group bits are the ACL **mask**, not the owning group's
    permissions — so a directory can have an exclusive owning group while a named unrelated account
    holds effective `rwx`. The mask bounds every named entry, though, and the mask *is* what the
    group bits report. Both directions are asserted, because that pair is what makes refusing
    group-write **faithful** rather than merely strict: a named user with effective write forces
    the bit on, and a reduced mask that clears the bit also strips that user's write.
    """
    wide = tmp_path / "acl-wide"
    wide.mkdir(mode=0o700)
    subprocess.run(["setfacl", "-m", "u:65534:rwx", str(wide)], check=True)
    assert os.lstat(wide).st_mode & stat.S_IWGRP, "the ACL mask should surface as the group bit"
    with pytest.raises(PrivateDirError, match="group-writable"):
        privatedir.verify_private_dir(wide)

    masked = tmp_path / "acl-masked"
    masked.mkdir(mode=0o700)
    subprocess.run(["setfacl", "-m", "u:65534:rwx", "-m", "m::r-x", str(masked)], check=True)
    assert not os.lstat(masked).st_mode & stat.S_IWGRP
    # The named user's write is masked away, so no other account can write: this must PASS.
    assert privatedir.verify_private_dir(masked) == str(masked)


def test_the_boundary_never_consults_nss_enumeration():
    """`pwd.getpwall()` cannot prove a group is exclusive, so the boundary must not ask it.

    Directory-backed NSS resolves individual users while omitting them from enumeration — sssd's
    `enumerate` defaults to false — and an incomplete answer raises nothing to catch, so absence
    from the list is not evidence. Pinned over the AST, so a mention in prose can neither satisfy
    nor break it.
    """
    tree = ast.parse(Path(privatedir.__file__).read_text(encoding="utf-8"))
    reads = {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    for forbidden in ("pwd.getpwall", "pwd.getpwuid", "grp.getgrgid"):
        assert forbidden not in reads, f"the boundary consults {forbidden}"
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "pwd" not in imported and "grp" not in imported


def test_a_real_0775_directory_is_refused(tmp_path):
    """The `umask 002` shape this host actually produces — refused, not admitted."""
    d = tmp_path / "umask002"
    d.mkdir()
    d.chmod(0o775)
    with pytest.raises(PrivateDirError, match="group-writable"):
        privatedir.verify_private_dir(d)


# --------------------------------------------------------------------------- real-directory cases


def test_a_symlink_component_is_refused(tmp_path):
    """`lstat`, not `stat`: resolving first would answer about the target and say nothing about
    the link, which is the component an attacker controls."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)

    assert privatedir.verify_private_dir(real) == str(real)
    with pytest.raises(PrivateDirError, match="not a real directory"):
        privatedir.verify_private_dir(link)
    # ...and as an intermediate component, not merely as the leaf.
    with pytest.raises(PrivateDirError, match="not a real directory"):
        privatedir.verify_private_dir(link / "under")


def test_a_foreign_writable_component_below_the_anchor_is_refused(tmp_path):
    boundary = tmp_path / "boundary"
    boundary.mkdir(mode=0o700)
    inner = boundary / "inner"
    inner.mkdir(mode=0o700)
    assert privatedir.verify_private_dir(inner) == str(inner)

    boundary.chmod(0o777)
    try:
        with pytest.raises(PrivateDirError, match="other-writable"):
            privatedir.verify_private_dir(inner)
    finally:
        boundary.chmod(0o700)


def test_a_component_that_cannot_be_read_refuses(tmp_path, monkeypatch):
    """PR #1000's P1 in this module's shape: a read failure at a boundary that decides "is this
    ours" must refuse. That PR shipped a fail-SOFT read at a destructive eligibility boundary and
    it let prune delete an active session's scrollback."""
    target = tmp_path / "runtime"
    real_lstat = os.lstat

    def boom(path, *a, **kw):
        if str(path) == str(target):
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(privatedir.os, "lstat", boom)
    with pytest.raises(PrivateDirError, match="cannot be read"):
        privatedir.verify_private_dir(target)


# --------------------------------------------------------------------------- the /tmp policy


def test_tmp_is_refused_as_an_anchor_by_identity_not_merely_by_mode():
    """ "Sticky or not." The refusal never consults the mode, so a `/tmp` somehow at `0755` is
    refused just the same — the sticky bit stops a local user deleting another's entry, never
    creating a name we would then trust."""
    for path in ("/tmp", "/var/tmp", "/dev/shm"):
        with pytest.raises(PrivateDirError, match="shared temp directory is never a trust anchor"):
            privatedir._refuse_shared_temp(Path(path))


def test_a_subtree_under_tmp_is_refused_by_the_production_policy(tmp_path):
    """Why the test root must be declared: the suite's own `tmp_path` is refused in production."""
    with pytest.raises(PrivateDirError):
        privatedir.verify_private_dir(tmp_path, policy=privatedir.PRODUCTION)


def test_the_tmp_test_root_exception_has_no_production_spelling():
    """The exception is injected by the harness and reachable no other way.

    In particular not by an environment variable, which would put it back inside production's
    reach — the one thing #1006 rules out explicitly. Checked over the AST rather than the text so
    a mention in a docstring or comment cannot satisfy or break it.
    """
    assert privatedir.PRODUCTION.test_roots == ()

    tree = ast.parse(Path(privatedir.__file__).read_text(encoding="utf-8"))
    reads = {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert "os.environ" not in reads, "privatedir reads the environment"
    assert "os.getenv" not in reads, "privatedir reads the environment"

    # The policy in force during the suite is the harness's; clearing it restores production.
    before = privatedir.active_policy()
    assert before.test_roots
    privatedir.set_policy_for_test(None)
    try:
        assert privatedir.active_policy() is privatedir.PRODUCTION
    finally:
        privatedir.set_policy_for_test(before)


# --------------------------------------------------------------------------- creation + reuse


def test_ensure_private_dir_creates_once_at_the_requested_mode_and_reuses_it(tmp_path):
    (tmp_path / "runtime").mkdir(mode=0o700)
    target = tmp_path / "runtime" / "hooks-void"
    first = privatedir.ensure_private_dir(target, mode=0o500)
    assert stat.S_IMODE(os.lstat(first).st_mode) == 0o500
    inode = os.stat(first).st_ino

    second = privatedir.ensure_private_dir(target, mode=0o500)
    assert second == first
    assert os.stat(second).st_ino == inode, "the directory was recreated rather than reused"


def test_ensure_private_dir_is_safe_under_concurrent_first_use(tmp_path):
    """The loser of the `mkdir` race takes `FileExistsError` and verifies the winner's directory,
    so first use needs no lock and cannot produce two answers."""
    (tmp_path / "runtime").mkdir(mode=0o700)
    target = tmp_path / "runtime" / "concurrent-void"
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def call():
        try:
            barrier.wait(timeout=10)
            results.append(privatedir.ensure_private_dir(target, mode=0o500))
        except Exception as exc:  # noqa: BLE001 — reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"concurrent first use raised: {errors}"
    assert results == [str(target)] * 8


def test_ensure_private_dir_refuses_a_wrong_mode_instead_of_repairing_it(tmp_path):
    """ "Repairing" a directory this call did not create is indistinguishable from adopting one
    somebody else prepared."""
    (tmp_path / "runtime").mkdir(mode=0o700)
    target = tmp_path / "runtime" / "loose"
    target.mkdir(mode=0o700)
    with pytest.raises(PrivateDirError, match="refusing rather than changing the mode"):
        privatedir.ensure_private_dir(target, mode=0o500)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700, "the mode was 'repaired'"


def test_ensure_private_dir_refuses_before_creating_anything_under_a_bad_parent(tmp_path):
    parent = tmp_path / "loose-parent"
    parent.mkdir()
    # `chmod`, never `mkdir(mode=...)`: the umask masks mkdir's mode, so on this host (`umask 002`)
    # 0o777 lands as 0o775. The policy refuses that too — but as *group*-writable, so the match
    # below would miss. The explicit chmod is what selects the `other-writable` diagnostic this
    # test is about. Same umask behaviour the production bug came from.
    parent.chmod(0o777)
    target = parent / "void"
    with pytest.raises(PrivateDirError, match="other-writable"):
        privatedir.ensure_private_dir(target, mode=0o500)
    assert not target.exists(), "created a directory inside a subtree it had already refused"
