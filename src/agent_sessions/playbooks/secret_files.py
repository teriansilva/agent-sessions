"""Secret reference files (#1096 §3, #1191): the one place a deployment writes plaintext.

A format-3 template material may name `{{secret_path:<name>}}` for a declared secret variable. It
renders to the absolute path `<root>/<project_id>/<name>`, where the root is
`~/.config/agent-sessions/playbook-secrets` (override `AGENT_SESSIONS_PLAYBOOK_SECRETS`). The PATH
is what review shows and what lands in the project, never the value. This is the explicit exception
to #1090's "a secret is stored only encrypted": apply writes the project's bound value there at
0600, and remove deletes it with the deployment.

**The one rule: a name in this store is ours only while it holds an inode this deployment proved.**
The proven inodes are the one its record owns and the one its apply journal created. Nothing is
deleted, published over or adopted on any other evidence, a name included.

* The root (and any ancestor this module creates) and the per-project directory are 0700 and owned
  by this user, the root and project directory opened `O_DIRECTORY | O_NOFOLLOW`; anything else
  refuses. Each new directory entry is fsynced into its parent. No path component comes from the
  bundle: the project id and the variable name are validated identifiers. The root a deployment
  wrote under is recorded with it, so a changed root setting never strands a file (review and
  apply refuse; remove uses the recorded root). The project directory's identity is journaled
  before the first write and recorded with the files: a moved, replaced or deleted directory
  refuses apply and remove, which never conclude "absent" from a replacement. The root must
  land outside the destination and every project folder (`check_outside`).
* A write creates an anonymous `O_TMPFILE` inode, fills and fsyncs it, and journals that inode
  (with the staging name it is about to get) BEFORE any name refers to it: a crash can never leave
  a name holding unproven plaintext. It is then linked under the staging name, checked, and
  published atomically: `RENAME_NOREPLACE` onto an absent name, or `RENAME_EXCHANGE` with the
  recorded inode. Any other file under the name refuses; it is never replaced or adopted, and the
  staged inode is reaped. The directories must still be the ones the descriptors hold, before and
  after publication; settlement then re-reads every name by path.
* Nothing is ever unlinked: no unlink can be bound to an inode. A name is retired by moving it
  into the project's private `.reap/` and judging what moved through a descriptor: a proven inode
  is truncated through it (its plaintext gone), anything else goes back or stays, reported. A
  foreign file is never deleted, and an interrupted retirement is found again by inode
  (`_sweep`) while the record and journal still prove it.
* The value never enters a log, response, record or journal. Only names, paths and inodes do, and
  the store's revision of the secret, which must be the one the review accepted.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

from .. import renameat, template_vars
from . import schema, store

ROOT_ENV = "AGENT_SESSIONS_PLAYBOOK_SECRETS"
_STAGING_RE = re.compile(r"\.staging-[0-9a-f]{32}")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def root() -> str:
    default = Path.home() / ".config" / "agent-sessions" / "playbook-secrets"
    return os.path.normpath(os.path.abspath(os.environ.get(ROOT_ENV) or str(default)))


def _name(name: object) -> str:
    if not isinstance(name, str) or not schema.VARIABLE_NAME_RE.fullmatch(name):
        raise store.Conflict("invalid secret reference name")
    return name


def path(pid: str, name: str, top: str | None = None) -> str:
    """The absolute path `{{secret_path:<name>}}` renders to for project `pid`."""
    return os.path.join(top or root(), template_vars.project_id(pid), _name(name))


def staging_name(name: object) -> str:
    if not isinstance(name, str) or not _STAGING_RE.fullmatch(name):
        raise ValueError("invalid staging name")
    return name


def check_root(record: dict | None) -> None:
    """Refuse when the root setting moved away from the files this deployment owns."""
    if record and record.get("secret_files") and record.get("secret_root") != root():
        raise store.Conflict(
            f"this deployment's secret files are under {record.get('secret_root')}, but the "
            f"secrets root is now {root()}: restore {ROOT_ENV}, or remove the deployment and "
            "deploy it again"
        )


def _inside_any(real: str, folders) -> str | None:
    for folder in folders:
        base = os.path.realpath(folder)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return folder
    return None


def _containment(folder: str) -> store.Conflict:
    return store.Conflict(
        f"the secret files for this project would be inside the project folder {folder}; "
        f"set {ROOT_ENV} to a directory outside every project"
    )


def _placed(top: str, pid: str, dirs: tuple[int, int], folders) -> str | None:
    """Why the held project directory may not receive or keep plaintext, or None.

    Containment is judged on the directory ACTUALLY held (its kernel path, `/proc/self/fd`), so
    a symlinked ancestor retargeted after review cannot carry the file into a project; and the
    configured path must still name that very directory.
    """
    inside = _inside_any(os.readlink(f"/proc/self/fd/{dirs[1]}"), folders)
    if inside is not None:
        return str(_containment(inside))
    if not _bound(top, pid, dirs):
        return "the playbook secrets directory moved; apply again"
    return None


def check_outside(folders, pid: str) -> None:
    """Refuse when the project's secret DIRECTORY (`<root>/<pid>`, where the files actually land)
    is inside, or equal to, any project folder or the destination: the value must live outside
    every workspace, where a copy, archive or `git add` cannot pick it up. That covers a root
    inside a project and a root above a project folder named like the project id. Compared on
    resolved paths, so a symlinked ancestor cannot slip past."""
    real = os.path.realpath(os.path.join(root(), template_vars.project_id(pid)))
    inside = _inside_any(real, folders)
    if inside is not None:
        raise _containment(inside)


# --- directories ---------------------------------------------------------------------------------


def _check_private(fd: int) -> None:
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise store.Conflict("the playbook secrets directory is not private (0700, owned by you)")


def _mkdir_synced(name: str, parent: int) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        return
    os.fsync(parent)  # the new entry is durable before anything inside it is relied on


def _open_root(top: str, *, create: bool) -> int | None:
    """The root's descriptor; created (0700, every new entry synced) only when `create`."""
    parts = Path(top).parts
    fd = os.open(parts[0], _DIR_FLAGS)
    try:
        for i, part in enumerate(parts[1:], 1):
            last = i == len(parts) - 1
            if create:
                _mkdir_synced(part, fd)
            # Ancestors may be symlinks the operator configured; the root itself may not.
            flags = _DIR_FLAGS if last else os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if create:
                    raise
                return None
            os.close(fd)
            fd = child
        _check_private(fd)
        out, fd = fd, -1
        return out
    finally:
        if fd >= 0:
            os.close(fd)


@contextlib.contextmanager
def _project(top: str, pid: str, *, create: bool) -> Iterator[tuple[int, int] | None]:
    """(root fd, project fd), or None when absent and not `create`."""
    base = _open_root(top, create=create)
    if base is None:
        yield None
        return
    fd = -1
    try:
        name = template_vars.project_id(pid)
        if create:
            _mkdir_synced(name, base)
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=base)
        except FileNotFoundError:
            if create:
                raise
        if fd < 0:
            yield None
            return
        _check_private(fd)
        yield base, fd
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(base)


def directory(top: str, pid: str) -> list[int]:
    """Create the project's directory if needed; its identity, journaled before any write."""
    with _project(top, pid, create=True) as dirs:
        assert dirs is not None
        st = os.fstat(dirs[1])
        return [st.st_dev, st.st_ino]


def _same_directory(top: str, pid: str, dirs: tuple[int, int] | None, expected) -> None:
    """The project directory must be the one this deployment wrote into, never a replacement."""
    if expected is None:
        return
    if dirs is not None:
        st = os.fstat(dirs[1])
        if [st.st_dev, st.st_ino] == list(expected):
            return
    raise store.Conflict(
        f"the secrets directory {os.path.join(top, pid)} is not the one this deployment wrote "
        "its secret files into (moved, replaced or deleted); move the original back, then retry"
    )


def _bound(top: str, pid: str, dirs: tuple[int, int]) -> bool:
    """Whether the root and project paths still name the directories the descriptors hold."""
    base, fd = dirs
    for where, held in ((top, base), (os.path.join(top, pid), fd)):
        try:
            st = os.stat(where, follow_symlinks=False)
        except OSError:
            return False
        live = os.fstat(held)
        if (st.st_dev, st.st_ino) != (live.st_dev, live.st_ino):
            return False
    return True


# --- names ---------------------------------------------------------------------------------------


def _inode(fd: int, name: str) -> list[int] | None:
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return [st.st_dev, st.st_ino]


REAP_DIR = ".reap"


def _scrub(fd: int, name: str, proven: list[list[int]]) -> bool:
    """Truncate `name` through a descriptor whose `fstat` proves it holds a proven inode.

    The proof and the truncation bind to the same inode, never to a name: the file is held with
    `O_PATH | O_NOFOLLOW`, proven by `fstat`, and only then made writable and truncated through
    that descriptor's own `/proc/self/fd` link (which names the held inode, not a path), so a
    read-only (0400, 0000) owned file is scrubbed too. False only when the inode is NOT proven;
    a proven file that cannot be scrubbed raises, so nothing is ever recorded as gone that is not.
    """
    try:
        held = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return False
    try:
        st = os.fstat(held)
        if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] not in proven:
            return False
        if st.st_size:
            bound = f"/proc/self/fd/{held}"
            _writable(bound)
            out = os.open(bound, os.O_WRONLY | os.O_CLOEXEC)
            try:
                if [os.fstat(out).st_dev, os.fstat(out).st_ino] != [st.st_dev, st.st_ino]:
                    raise OSError("the reopened secret file is not the proven inode")
                os.ftruncate(out, 0)
                os.fsync(out)
            finally:
                os.close(out)
        return True
    finally:
        os.close(held)


def _writable(bound: str) -> None:
    """Make the HELD inode owner-writable (a `/proc/self/fd` link names that inode, not a path)."""
    os.chmod(bound, 0o600)


@contextlib.contextmanager
def _quarantine(fd: int) -> Iterator[int]:
    """The project's private `.reap/` directory: where names go instead of being unlinked."""
    _mkdir_synced(REAP_DIR, fd)
    q = os.open(REAP_DIR, _DIR_FLAGS, dir_fd=fd)
    try:
        _check_private(q)
        yield q
    finally:
        os.close(q)


def _sweep(fd: int, proven: list[list[int]]) -> None:
    """Scrub any proven inode an interrupted reap left in `.reap/` (found by inode, not name)."""
    with _quarantine(fd) as q:
        for entry in os.listdir(q):
            _scrub(q, entry, proven)


def _reap_one(fd: int, name: str, proven: list[list[int]]) -> str:
    """Retire `name` if it holds a proven inode: `removed`, `absent` or `kept`.

    **Nothing is ever unlinked**, because no unlink can be bound to an inode. The name is moved
    (`RENAME_NOREPLACE`) into the private `.reap/` directory, and what moved is judged through a
    descriptor: a proven inode is truncated through it (its plaintext is gone, an empty file
    remains in `.reap/`), anything else is moved back under its name, or, if the name was taken
    meanwhile, kept in `.reap/` and reported. A foreign file is therefore never deleted, and a
    crash between the move and the truncation leaves the plaintext where `_sweep` finds it by
    inode on the next pass, while the record and journal still prove it.
    """
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    if [st.st_dev, st.st_ino] not in proven:
        return "kept"
    with _quarantine(fd) as q:
        aside = "r-" + secrets.token_hex(16)
        try:
            renameat.renameat2(fd, name, q, aside, renameat.RENAME_NOREPLACE)
        except FileNotFoundError:
            return "absent"
        os.fsync(fd)
        os.fsync(q)
        if _scrub(q, aside, proven):
            return "removed"
        # Not ours after all (replaced between the lookup and the move): give the name back.
        try:
            renameat.renameat2(q, aside, fd, name, renameat.RENAME_NOREPLACE)
            os.fsync(fd)
        except FileExistsError:
            pass  # the name was taken again; the moved file stays in .reap/, untouched
        return "kept"


def reap(
    top: str, pid: str, names: list[str], proven: list[list[int]], expected: list[int] | None
) -> dict[str, str]:
    """Delete each of `names` that holds a proven inode; report each name's outcome.

    `expected` is the directory this deployment wrote into. A missing or replaced directory
    refuses: a name absent from a REPLACEMENT proves nothing about the displaced plaintext.
    """
    for name in names:
        if not _STAGING_RE.fullmatch(name):
            _name(name)
    proven = [list(i) for i in proven]
    with _project(top, pid, create=False) as dirs:
        _same_directory(top, pid, dirs, expected)
        if dirs is None:
            return {name: "absent" for name in names}
        _sweep(dirs[1], proven)
        return {name: _reap_one(dirs[1], name, proven) for name in names}


# --- write ---------------------------------------------------------------------------------------


def write(
    top: str,
    pid: str,
    name: str,
    value: str,
    effect: dict,
    recorded: list[int] | None,
    progress: Callable[[], None],
    expected: list[int],
    folders: list[str],
) -> None:
    """Publish `value` as `name`'s reference file, recoverably; `effect` is its journal entry.

    `recorded` is the inode this deployment's record owns under `name`, if any: the only file a
    write may replace. `expected` is the journaled project directory. `progress` makes the
    journal durable.
    """
    _name(name)
    with _project(top, pid, create=False) as dirs:
        _same_directory(top, pid, dirs, expected)
        assert dirs is not None
        fd = dirs[1]
        reason = _placed(top, pid, dirs, folders)
        if reason is not None:
            raise store.Conflict(reason)  # judged before any plaintext exists in it
        if effect["phase"] == "staged":
            if _inode(fd, name) == effect["inode"]:
                _finish(fd, effect, recorded, progress)  # published before an interruption
                return
            if _inode(fd, effect["staging"]) != effect["inode"]:
                # The staged name is gone (the link never happened) or is not ours: leave any
                # foreign entry alone and stage afresh. An unlinked O_TMPFILE inode is already gone.
                effect.update(phase="pending", staging=None, inode=None)
                progress()
        if effect["phase"] == "pending":
            _stage(fd, value, effect, progress)
        reason = _placed(top, pid, dirs, folders)
        if reason is not None:
            _refuse(fd, effect, progress, reason)
        staging, mine = effect["staging"], effect["inode"]
        if _inode(fd, staging) != mine:
            _refuse(fd, effect, progress, "a staged secret file was replaced; apply again")
        current = _inode(fd, name)
        if current is None:
            renameat.renameat2(fd, staging, fd, name, renameat.RENAME_NOREPLACE)
        elif recorded is not None and current == list(recorded):
            renameat.renameat2(fd, staging, fd, name, renameat.RENAME_EXCHANGE)
            if _inode(fd, staging) != list(recorded):
                # What came out is not the recorded file: swap it straight back and refuse.
                renameat.renameat2(fd, staging, fd, name, renameat.RENAME_EXCHANGE)
                _refuse(fd, effect, progress, _foreign(top, pid, name))
        else:
            _refuse(fd, effect, progress, _foreign(top, pid, name))
        os.fsync(fd)
        reason = _placed(top, pid, dirs, folders)
        if reason is not None:
            # Published into a directory that is no longer acceptable: retire it at once.
            _reap_one(fd, name, [mine])
            _refuse(fd, effect, progress, reason)
        _finish(fd, effect, recorded, progress)


def _stage(fd: int, value: str, effect: dict, progress: Callable[[], None]) -> None:
    try:
        tmp = os.open(".", os.O_TMPFILE | os.O_WRONLY | os.O_CLOEXEC, 0o600, dir_fd=fd)
    except OSError as e:
        raise store.StoreError(
            f"the secrets directory cannot hold an anonymous file: {e.strerror or e}", status=409
        ) from None
    try:
        os.fchmod(tmp, 0o600)
        data = memoryview(value.encode("utf-8"))
        while data:
            data = data[os.write(tmp, data) :]
        os.fsync(tmp)
        st = os.fstat(tmp)
        staging = ".staging-" + secrets.token_hex(16)
        # Journaled while NO name refers to the inode: every later name holding it is ours.
        effect.update(phase="staged", staging=staging, inode=[st.st_dev, st.st_ino])
        progress()
        _link(tmp, staging, fd)
        os.fsync(fd)
    finally:
        os.close(tmp)


def _link(tmp: int, staging: str, fd: int) -> None:
    """Give the anonymous inode its first name (fails if the name exists)."""
    os.link(f"/proc/self/fd/{tmp}", staging, dst_dir_fd=fd)


def _finish(fd: int, effect: dict, recorded: list[int] | None, progress) -> None:
    # The staging name now holds nothing (a NOREPLACE moved it) or, after an exchange, the
    # recorded file: both proven. Anything else under it stays.
    proven = [effect["inode"]] + ([list(recorded)] if recorded is not None else [])
    _reap_one(fd, effect["staging"], proven)
    effect["phase"] = "done"
    progress()


def _foreign(top: str, pid: str, name: str) -> str:
    return (
        f"{path(pid, name, top)} is not the secret file this deployment wrote; "
        "move it away, then apply again"
    )


def _refuse(fd: int, effect: dict, progress: Callable[[], None], reason: str) -> None:
    if effect["staging"] is not None and effect["inode"] is not None:
        _reap_one(fd, effect["staging"], [effect["inode"]])  # never leave staged plaintext
    effect.update(phase="pending", staging=None, inode=None)
    progress()
    raise store.Conflict(reason)


def settle(top: str, pid: str, owned: dict[str, list[int]], folders: list[str]) -> None:
    """Every written name, looked up afresh by PATH, must still hold the inode apply wrote, in
    a directory that is still outside every project."""
    if not owned:
        return
    with _project(top, pid, create=False) as dirs:
        reason = None if dirs is None else _placed(top, pid, dirs, folders)
        if reason is not None:
            raise store.Conflict(reason)
        for name, inode in sorted(owned.items()):
            if dirs is None or _inode(dirs[1], _name(name)) != list(inode):
                raise store.Conflict(
                    f"{path(pid, name, top)}: changed before the apply record settled"
                )


def present(top: str, pid: str, name: str, inode: list[int]) -> bool:
    """Whether `name` is still the recorded file (verify's check; nothing is read)."""
    with _project(top, pid, create=False) as dirs:
        return dirs is not None and _inode(dirs[1], _name(name)) == list(inode)


# --- shapes --------------------------------------------------------------------------------------


def validate_effects(effects: object) -> None:
    """The journal shape of an apply's secret-file effects; ValueError when damaged."""
    if not isinstance(effects, dict) or len(effects) > 2 * schema.MAX_VARIABLES:
        raise ValueError
    for name, effect in effects.items():
        _name(name)
        if (
            not isinstance(effect, dict)
            or set(effect) != {"action", "phase", "staging", "inode"}
            or effect["action"] not in {"write", "delete"}
            or effect["phase"] not in {"pending", "staged", "done"}
            or (effect["staging"] is None) != (effect["inode"] is None)
        ):
            raise ValueError
        if effect["staging"] is not None:
            staging_name(effect["staging"])
        _identity(effect["inode"])


def _identity(inode: object) -> None:
    if inode is not None and (
        not isinstance(inode, list) or len(inode) != 2 or not all(type(i) is int for i in inode)
    ):
        raise ValueError


def validate_owned(owned: object) -> dict:
    """A record's `secret_files`: name → the inode this deployment wrote."""
    if not isinstance(owned, dict) or len(owned) > schema.MAX_VARIABLES:
        raise ValueError
    for name, inode in owned.items():
        _name(name)
        if inode is None:
            raise ValueError
        _identity(inode)
    return owned


def validate_root(top: object) -> str:
    if not isinstance(top, str) or not os.path.isabs(top) or os.path.normpath(top) != top:
        raise ValueError
    return top
