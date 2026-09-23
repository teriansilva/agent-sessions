"""Executable provenance — what binds `argv[0]` to the artifact that was checked (#853 §2b).

`manifest.py` closes the *value* space: every flag and kind a manifest names is from a fixed set.
That does not close the *executable* space. `binary.name`, `binary.env_var` and
`binary.search_paths` are manifest data, so a perfectly literal, kind-assembled argv is only as safe
as the file its `argv[0]` turns out to be. This module decides that file, and re-decides it at
every invocation.

**Two states, never confused.**

- **managed** — the plugin was installed into its own root (`<plugins>/<id>/`, P5) and the install
  record names the entrypoint and its sha256. The entrypoint must resolve inside that root with no
  symlink on the way, and its digest must match — re-checked before every run, not once at
  download. A digest verified at install and then forgotten is a check that expired before it
  mattered.
- **adopted** — a CLI the operator installed themselves, found through `binary.env_var` or
  `binary.search_paths`. There is no feed artifact to bind it to. For a **plugin-supplied** manifest
  it runs only after the operator has confirmed that exact file (path + sha256); a changed file
  needs confirming again. An **in-tree** (first-party) manifest's adopted binary is confirmed by
  provenance — the manifest is reviewed code, exactly as today's hardcoded `*_BIN` constants are —
  so the seven existing engines keep working with no new prompt. It is still recorded and surfaced
  as adopted, never as managed.

An env override pointing outside the plugin root **flips a managed plugin to adopted** rather than
quietly widening it.

**No PATH lookup.** `argv[0]` is always an absolute path this module resolved; a bare name never
reaches the launcher, so a directory earlier on `PATH` cannot shadow the entrypoint.

**Who can swap the file.** The entrypoint, and every directory above it, must be owned by root or
by the operator and writable by nobody else: not by others, and by its group only when that group
is the operator's **private** group (their primary group, with no other member and no other user's
primary group) — the default `umask 002` layout of a user-private-group system, where `~/.local/bin`
is `775` and nobody else is in the group. A root-owned sticky ancestor such as `/tmp` is tolerated,
since nobody else can rename our entries inside it; the directory that *contains* the entrypoint
never is. That rule is what makes the check-then-exec window safe: after validation only
root or the operator — who already runs every agent — can replace the bytes. On top of it, each
invocation re-opens the resolved path with `O_NOFOLLOW` and compares device, inode, size, mtime and
ctime to what was validated, re-hashing when any of them moved, so a retargeted symlink or a
replaced file is refused rather than launched. ctime cannot be set from user space, which is why a
matching stat tuple is allowed to stand in for a re-hash.

**What the digest attests — and what it does not.** Provenance covers the entrypoint FILE: its
bytes, its location and who can replace it. When that file is a script, the kernel then runs the
interpreter its `#!` line names — `#!/usr/bin/env node` is a PATH lookup made by `env`, not by
BattleLab — and the script loads whatever it imports. Neither the interpreter nor the imported
payload is hashed per exec. "No PATH lookup" therefore means BattleLab never resolves `argv[0]`
through PATH; it is not a claim about the whole execution chain. For a managed plugin the payload
lives under the same operator-owned root, so the ownership rule still bounds who can change it; P5
verifies the whole artifact at install. No surface may say more than this.

**Trust comes from where a manifest was loaded, never from what it says.** The loader passes
`first-party` only for files under the in-tree package directory; nothing in a manifest (not its
id, not its publisher) can raise it. A local manifest reusing an in-tree id is refused, never merged
or preferred.

**Closed entrypoint vocabulary.** A plugin-supplied manifest may not name, alias, or resolve to a
shell, interpreter or privilege tool (`kinds.FORBIDDEN_ENTRYPOINTS`). The in-tree `shell` engine is
exempt by provenance, never by name.
"""

from __future__ import annotations

import errno
import grp
import hashlib
import os
import pwd
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import kinds
from .manifest import Manifest

FIRST_PARTY = "first-party"
LOCAL = "local"
TRUST_LEVELS = frozenset({FIRST_PARTY, LOCAL})

MANAGED = "managed"
ADOPTED = "adopted"

_HASH_CHUNK = 1 << 20


class ProvenanceError(Exception):
    """The entrypoint cannot be run. The message says why, in operator-facing terms."""


@dataclass(frozen=True)
class Record:
    """What BattleLab has on file for one plugin: the install and/or the operator's confirmation.

    `install_entrypoint` is relative to the plugin root; `install_sha256` is its digest at install.
    `confirmed_path` / `confirmed_sha256` bind an adopted binary to the exact bytes the operator
    approved.
    """

    install_entrypoint: str | None = None
    install_sha256: str | None = None
    confirmed_path: str | None = None
    confirmed_sha256: str | None = None
    #: The digest of the manifest this record was written for. A record for any other manifest
    #: (edited, replaced, or a different plugin claiming the id) makes nothing managed or confirmed.
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class Entrypoint:
    path: str  # absolute, symlink-free — this is argv[0]
    state: str  # MANAGED | ADOPTED
    via: str  # "install" | "env" | "search_paths"
    stat_key: tuple[int, int, int, int, int]
    sha256: str  # "" when never hashed (first-party adopted)
    note: str = ""  # e.g. why a managed plugin was flipped to adopted


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _trusted_owner(st: os.stat_result) -> bool:
    return st.st_uid in (0, os.getuid())


def _group_is_private(gid: int) -> bool:
    """Is `gid` the operator's own user-private group — nobody else can write through it?

    Deliberately NOT cached: this is an authorization fact, and a group that gains a member must
    stop being trusted on the very next check, not at the next app restart (Hermes on PR #1112).
    """
    if gid != os.getgid():
        return False
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        # No passwd/group entry (common in containers): privacy cannot be established.
        return False
    if members - {me}:
        return False
    return not any(p.pw_gid == gid and p.pw_uid != os.getuid() for p in pwd.getpwall())


_ACL_XATTR = "system.posix_acl_access"
_ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER = 0x02, 0x04, 0x08, 0x10, 0x20
_ACL_WRITE = 0x2


def _acl_of(src: int | str) -> bytes | None:
    """The POSIX access ACL of an fd or path (never followed), or None when there is none.

    Any read failure other than "no ACL" / "not supported" is answered with an unparseable blob,
    which `_writable_by_others` treats as writable: an ACL we cannot read is not one we may trust.
    """
    try:
        if isinstance(src, int):
            return os.getxattr(src, _ACL_XATTR)
        return os.getxattr(src, _ACL_XATTR, follow_symlinks=False)
    except OSError as e:
        if e.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return None
        return b"\x00"


def _acl_grants_others_write(acl: bytes, st: os.stat_result) -> bool:
    """With an extended ACL the mode's group bits are only the MASK; the real writers are the
    entries. Named users and groups, the owning group and `other` are each checked against the
    mask. Anything malformed fails closed."""
    if len(acl) < 4 or (len(acl) - 4) % 8 or int.from_bytes(acl[:4], "little") != 2:
        return True
    entries = [
        (
            int.from_bytes(acl[i : i + 2], "little"),
            int.from_bytes(acl[i + 2 : i + 4], "little"),
            int.from_bytes(acl[i + 4 : i + 8], "little"),
        )
        for i in range(4, len(acl), 8)
    ]
    mask = next((perm for tag, perm, _ in entries if tag == _ACL_MASK), 0x7)
    uid = os.getuid()
    for tag, perm, qid in entries:
        if tag == _ACL_OTHER and perm & _ACL_WRITE:
            return True
        if not perm & mask & _ACL_WRITE:
            continue
        if tag == _ACL_USER and qid != uid:
            return True
        if tag == _ACL_GROUP_OBJ and not _group_is_private(st.st_gid):
            return True
        if tag == _ACL_GROUP and not _group_is_private(qid):
            return True
    return False


def _writable_by_others(st: os.stat_result, src: int | str | None = None) -> bool:
    """Can anyone but the operator (or root) write this? `src` is the fd or path the stat came
    from, so an extended ACL is read from the SAME object (Hermes on PR #1112: with an ACL, the
    group bits are the mask and a named user's write never shows in the mode)."""
    if st.st_mode & 0o002:
        return True
    acl = _acl_of(src) if src is not None else None
    if acl is not None:
        return _acl_grants_others_write(acl, st)
    return bool(st.st_mode & 0o020) and not _group_is_private(st.st_gid)


def _check_dir_st(st: os.stat_result, src: int | str, where: str, *, last: bool) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise ProvenanceError(f"{where} is not a plain directory")
    if not _trusted_owner(st):
        raise ProvenanceError(f"{where} is owned by another user (uid {st.st_uid})")
    if _writable_by_others(st, src):
        sticky_root = st.st_uid == 0 and st.st_mode & stat.S_ISVTX
        if last or not sticky_root:
            raise ProvenanceError(f"{where} is writable by other users")


def open_verified(
    path: str, *, executable: bool = False, canonicalize: bool = True
) -> tuple[int, os.stat_result]:
    """Open `path` so that WHAT WAS CHECKED IS WHAT IS READ (Hermes on PR #1112).

    The canonical path is walked one component at a time: each directory is opened `O_NOFOLLOW`
    relative to its parent's descriptor and checked ON THAT DESCRIPTOR — owner, mode, ACL — and the
    file is opened the same way (`O_NONBLOCK`, so a FIFO cannot hang the open) and checked on its
    own descriptor. A component swapped for a symlink after canonicalisation fails the walk
    (`ELOOP`) instead of redirecting it, and the caller reads or hashes the returned descriptor, so
    no path is ever re-resolved between the check and the use.

    Every directory must be owned by root or the operator and writable by nobody else, except a
    root-owned sticky ancestor (never the immediate parent: anyone could have planted a file there
    first). Returns `(fd, stat)`; the caller owns the fd.

    `canonicalize=False` walks `path` EXACTLY as given — it must already be absolute and
    symlink-free, and any symlink found on it is refused rather than resolved. Executable
    provenance uses that mode, so the path that is checked is byte-for-byte the path returned as
    `argv[0]`: resolving a second time let the walk check one path while the caller kept another
    (Hermes review 5136).
    """
    if canonicalize:
        real = os.path.realpath(path)
    else:
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            raise ProvenanceError(f"{path} is not an absolute, normalised path")
        real = path
    parts = [p for p in real.split("/") if p]
    if not parts:
        raise ProvenanceError(f"{path} is not a file")
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    dfd = os.open("/", flags_dir)
    try:
        _check_dir_st(os.fstat(dfd), dfd, "/", last=len(parts) == 1)
        where = ""
        for i, name in enumerate(parts[:-1]):
            where += "/" + name
            try:
                nfd = os.open(name, flags_dir, dir_fd=dfd)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    raise FileNotFoundError(e.errno, e.strerror, where) from None
                raise ProvenanceError(f"cannot open {where}: {e.strerror}") from None
            os.close(dfd)
            dfd = nfd
            _check_dir_st(os.fstat(dfd), dfd, where, last=i == len(parts) - 2)
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileNotFoundError(e.errno, e.strerror, real) from None
            raise ProvenanceError(f"cannot open {real}: {e.strerror}") from None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    problem = None
    if not stat.S_ISREG(st.st_mode):
        problem = f"{real} is not a regular file"
    elif not _trusted_owner(st):
        problem = f"{real} is owned by another user (uid {st.st_uid})"
    elif _writable_by_others(st, fd):
        problem = f"{real} is writable by other users"
    elif executable and not st.st_mode & 0o111:
        problem = f"{real} is not executable"
    if problem:
        os.close(fd)
        raise ProvenanceError(problem)
    return fd, st


def _check_dirs(real: str) -> None:
    """The ancestor half of `open_verified`, for callers that only need the verdict."""
    fd, _ = _open_checked(real)
    os.close(fd)


def _open_checked(real: str) -> tuple[int, os.stat_result]:
    """Open an executable at EXACTLY `real` — no second canonicalisation (see `open_verified`)."""
    try:
        return open_verified(real, executable=True, canonicalize=False)
    except FileNotFoundError:
        raise ProvenanceError(f"{real} does not exist") from None


def _sha256_fd(fd: int) -> str:
    h = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _HASH_CHUNK)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _expand(p: str, home: Path) -> str:
    return str(home / p[2:]) if p.startswith("~/") else p


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_vocabulary(m: Manifest, trust: str) -> None:
    """The closed entrypoint vocabulary, applied to the manifest's own names at load time."""
    if trust == FIRST_PARTY:
        return
    for n in (m.binary.name, *m.binary.aliases):
        if kinds.is_forbidden_entrypoint(n):
            raise ProvenanceError(
                f"binary {n!r} is a shell, interpreter or privilege tool; "
                "a plugin may not launch one"
            )


def resolve(
    m: Manifest,
    *,
    trust: str,
    root: Path,
    record: Record | None,
    env: Mapping[str, str],
    home: Path,
) -> Entrypoint | None:
    """Decide the entrypoint, or None when nothing is installed where the manifest looks.

    Raises `ProvenanceError` when a candidate EXISTS but may not run — a refusal is a finding the
    operator needs to see, never a silent "not present".
    """
    if trust not in TRUST_LEVELS:
        raise ProvenanceError(f"unknown trust level {trust!r}")
    check_vocabulary(m, trust)
    record = record or Record()
    if record != Record() and record.manifest_sha256 != m.digest:
        raise ProvenanceError(
            "the install/confirmation record was written for a different manifest; "
            "the operator has to confirm this one"
        )
    root_real = os.path.realpath(root)

    cand: str | None = None
    via = ""
    note = ""
    managed = False
    override = env.get(m.binary.env_var) if m.binary.env_var else None
    if override:
        if not os.path.isabs(override):
            raise ProvenanceError(f"{m.binary.env_var} must be an absolute path")
        cand, via = override, "env"
        # Managed-or-adopted is decided below, on the ONE canonical path that is also walked and
        # returned — never on a separate resolution of the override (Hermes review 5136).
        managed = bool(record.install_entrypoint)
    elif record.install_entrypoint:
        cand, via, managed = str(Path(root_real) / record.install_entrypoint), "install", True
    else:
        for d in m.binary.search_paths:
            p = os.path.join(_expand(d, home), m.binary.name)
            if os.path.lexists(p):
                cand, via = p, "search_paths"
                break
    if cand is None:
        return None
    if not os.path.lexists(cand):
        if via == "search_paths":
            return None
        raise ProvenanceError(f"{cand} does not exist")

    # The ONLY canonicalisation. From here on `real` is walked exactly as it is (every component
    # `O_NOFOLLOW`, a symlink refused rather than followed) and returned as argv[0] unchanged, so
    # the path that was checked and the path that is executed cannot diverge.
    real = os.path.realpath(cand)
    if via == "env" and managed and not _inside(real, root_real):
        managed = False
        note = (
            f"{m.binary.env_var} points outside the plugin root — treated as adopted, not managed"
        )
    if trust != FIRST_PARTY and kinds.is_forbidden_entrypoint(os.path.basename(real)):
        raise ProvenanceError(
            f"{cand} resolves to {real}, a shell, interpreter or privilege tool; "
            "a plugin may not launch one"
        )
    if managed:
        expected = os.path.normpath(os.path.join(root_real, record.install_entrypoint or ""))
        if via == "install" and real != expected:
            raise ProvenanceError(
                "the installed entrypoint is reached through a symlink; refusing it"
            )
        if not _inside(real, root_real):
            raise ProvenanceError("the installed entrypoint resolves outside the plugin root")
    fd, st = _open_checked(real)
    try:
        digest = ""
        if managed:
            digest = _sha256_fd(fd)
            if not record.install_sha256 or digest != record.install_sha256:
                raise ProvenanceError(
                    "the installed entrypoint no longer matches its install digest"
                )
        elif trust != FIRST_PARTY:
            digest = _sha256_fd(fd)
            if record.confirmed_path != real or record.confirmed_sha256 != digest:
                raise ProvenanceError(
                    f"{real} is an adopted binary for a plugin-supplied manifest and needs the "
                    "operator's confirmation of this exact file"
                )
    finally:
        os.close(fd)
    return Entrypoint(
        path=real,
        state=MANAGED if managed else ADOPTED,
        via=via,
        stat_key=_stat_key(st),
        sha256=digest,
        note=note,
    )


def reverify(ep: Entrypoint, record: Record | None, *, trust: str) -> Entrypoint:
    """Re-check a resolved entrypoint immediately before it is run.

    Same file (device, inode, size, mtime, ctime unchanged) → returned as is. Anything moved →
    re-hashed and held to the same digest rule as at resolution; a first-party adopted binary that
    moved (the vendor's own auto-update) must be re-resolved by the caller instead.
    """
    record = record or Record()
    fd, st = _open_checked(ep.path)
    try:
        key = _stat_key(st)
        if key == ep.stat_key:
            return ep
        if ep.state == ADOPTED and trust == FIRST_PARTY:
            raise ProvenanceError("the binary changed since it was resolved; resolve it again")
        digest = _sha256_fd(fd)
        want = record.install_sha256 if ep.state == MANAGED else record.confirmed_sha256
        if not want or digest != want:
            raise ProvenanceError(
                f"{ep.path} changed since it was checked and no longer matches its digest"
            )
        return Entrypoint(ep.path, ep.state, ep.via, key, digest, ep.note)
    finally:
        os.close(fd)
