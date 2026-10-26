"""Per-launch skill bundles (#1693 S2): a specialist's skills, at their pinned revisions, as one
read-only directory the skills tool (`skills_mcp`) serves to one session.

* **Location** ``<AGENT_SESSIONS_HOME>/skill-bundles/<bundle_id>/<skill_id>/…`` (default home
  ``~/.local/share/agent-sessions``). The root is opened through `plugins.storage.directory` (no
  symlink anywhere on the path, ownership and mode checked); every directory below it is created
  ``0700`` and every file ``0400``, all relative to a directory descriptor. The id is the launch's
  own (S4 passes its capacity reservation id), so a bundle lives exactly as long as the launch
  that holds it and `remove` deletes it on release.
* **The pin is proved, never assumed.** Each skill's tree is read and its revision computed from
  the very bytes read, under the skills library's SHARED lock (the lock its writers take
  exclusively), through the store's descriptor-relative, no-follow reads (`gallery.files.read`).
  A revision other than the pinned one refuses the whole bundle — never served from newer content
  — and an edit can't land between "revision matches" and "tree copied": both happen under that
  one lock. An unreadable tree, a lock that can't be had, or a write that fails refuses too, and
  every refusal is a `BundleRefused` that names its phase.
* **All or nothing, never replacing.** The bundle is built in a dot-named staging directory and
  published with ``renameat2(RENAME_NOREPLACE)`` (an existing name, even an empty directory, is
  never replaced; a filesystem without it refuses the bundle, never a check-then-rename); a
  refusal leaves nothing behind, and staging left by a crash is swept.
* **Only a bundle can be served.** `verify` is what the launch seams call before they hand a path
  to the skills tool: an existing, private directory of this user, directly under the root.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path

from .gallery import contract, files
from .gallery import skills as library
from .memory_mcp import _RENAMEAT2, RENAME_NOREPLACE
from .playbooks import store
from .playbooks.schema import SKILL_REF_RE
from .plugins import storage as plugin_storage
from .plugins.provenance import ProvenanceError

BUNDLE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_REVISION_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_STAGING = ".staging-"
_REMOVING = ".removing-"
#: Staging or removal leftovers older than this are a crash's, never a live build's.
STALE_S = 3600
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
#: What opening the root or the library can raise beyond `OSError`.
_DIR_ERRORS = (OSError, plugin_storage.StateError, ProvenanceError)
_WRITE_FAILED = "the skill bundle could not be written"
ATOMIC_UNAVAILABLE = "this filesystem cannot publish a skill bundle atomically; nothing was built"


class BundleRefused(RuntimeError):
    """The bundle can't be proved to hold exactly the pinned skills; the launch is refused."""


def root() -> Path:
    home = os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    return Path(home).expanduser().absolute() / "skill-bundles"


def path_of(bundle_id: str) -> Path:
    return root() / _bundle_id(bundle_id)


def _bundle_id(bundle_id: object) -> str:
    if not isinstance(bundle_id, str) or not BUNDLE_ID_RE.fullmatch(bundle_id):
        raise BundleRefused("invalid bundle id")
    return bundle_id


@contextlib.contextmanager
def _root_fd():
    try:
        cm = plugin_storage.directory(root())
        fd = cm.__enter__()
    except _DIR_ERRORS:
        raise BundleRefused("the skill bundle directory is unavailable or unsafe") from None
    try:
        yield fd
    finally:
        cm.__exit__(None, None, None)


def _check(skills: object) -> list[tuple[str, str]]:
    if not isinstance(skills, list) or not skills:
        raise BundleRefused("a bundle holds at least one skill")
    out: list[tuple[str, str]] = []
    for ref in skills:
        if not isinstance(ref, dict) or set(ref) != {"id", "revision"}:
            raise BundleRefused("each skill is {id, revision}")
        sid, rev = ref["id"], ref["revision"]
        if not isinstance(sid, str) or not SKILL_REF_RE.fullmatch(sid):
            raise BundleRefused("a skill id is malformed")
        if not isinstance(rev, str) or not _REVISION_RE.fullmatch(rev):
            raise BundleRefused(f"skill {sid}: the revision is malformed")
        if any(sid == s for s, _ in out):
            raise BundleRefused(f"skill {sid} is listed twice")
        out.append((sid, rev))
    return out


def _snapshot(lib_fd: int, sid: str, pinned: str) -> dict[str, bytes]:
    """``sid``'s files, read under the caller's shared lock, only if they ARE the pinned
    revision."""
    try:
        dfd = os.open(sid, files.DIR, dir_fd=lib_fd)
    except FileNotFoundError:
        raise BundleRefused(f"skill {sid} is not in your skills library") from None
    except OSError:
        raise BundleRefused(f"skill {sid} can't be read") from None
    try:
        tree = files.read(dfd)
    except (OSError, ValueError, contract.GalleryError):
        raise BundleRefused(f"skill {sid} can't be read") from None
    finally:
        os.close(dfd)
    if tree.digest() != pinned:
        raise BundleRefused(f"skill {sid} changed since this version was saved: save a new version")
    return dict(tree.files)


def _mkdir_at(parent: int, name: str) -> int:
    """``name`` under ``parent`` as a fresh ``0700`` directory, opened."""
    os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, _DIR, dir_fd=parent)
    os.fchmod(fd, 0o700)  # never wider than 0700, whatever the umask
    return fd


def _write_tree(staging_fd: int, sid: str, tree: dict[str, bytes]) -> None:
    skill_fd = _mkdir_at(staging_fd, sid)
    opened: dict[str, int] = {"": skill_fd}
    try:
        for rel, data in sorted(tree.items()):
            parts = rel.split("/")
            if any(p in ("", ".", "..") or "\0" in p for p in parts):
                raise BundleRefused("a skill path is malformed")
            prefix = ""
            for part in parts[:-1]:
                key = f"{prefix}{part}/"
                if key not in opened:
                    opened[key] = _mkdir_at(opened[prefix], part)
                prefix = key
            fd = os.open(parts[-1], _CREATE, 0o400, dir_fd=opened[prefix])
            try:
                os.fchmod(fd, 0o400)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
    finally:
        for fd in opened.values():
            os.close(fd)


def _exists_at(dfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _publish(root_fd: int, staging: str, name: str) -> None:
    """Rename ``staging`` to ``name`` under ``root_fd`` with ``RENAME_NOREPLACE``. Where that is
    unavailable the bundle is refused (as the memory store refuses a note): a check-then-rename
    could replace a name that appeared in between, and a bundle is never published that way."""
    if _RENAMEAT2 is None:
        raise BundleRefused(ATOMIC_UNAVAILABLE)
    rc = _RENAMEAT2(root_fd, staging.encode(), root_fd, name.encode(), RENAME_NOREPLACE)
    if rc == 0:
        return
    err = ctypes.get_errno()
    if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        raise BundleRefused(ATOMIC_UNAVAILABLE)
    if err in (errno.EEXIST, errno.ENOTEMPTY):
        raise BundleRefused("this launch already has a bundle")
    raise BundleRefused(_WRITE_FAILED)


def _rmtree_at(root_fd: int, name: str) -> None:
    """Delete ``name`` under ``root_fd`` whole, never following a link, even where a directory
    inside has lost its write bit (an agent runs as the same user)."""
    try:
        st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=root_fd)
        return
    for _top, _dirs, _names, dfd in os.fwalk(name, dir_fd=root_fd, follow_symlinks=False):
        with contextlib.suppress(OSError):
            os.fchmod(dfd, 0o700)
    shutil.rmtree(name, dir_fd=root_fd)


def _sweep(root_fd: int) -> None:
    """Remove staging and removal leftovers a crash left behind (never a live build's)."""
    now = time.time()
    for name in os.listdir(root_fd):
        if not name.startswith((_STAGING, _REMOVING)):
            continue
        try:
            st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            continue
        if now - st.st_mtime > STALE_S:
            with contextlib.suppress(OSError):
                _rmtree_at(root_fd, name)


def _copy_pinned(staging_fd: int, refs: list[tuple[str, str]]) -> None:
    try:
        # ONE shared lock over every check and copy: no library edit lands in between.
        with library.locked(exclusive=False) as lib_fd:
            for sid, pinned in refs:
                tree = _snapshot(lib_fd, sid, pinned)
                try:
                    _write_tree(staging_fd, sid, tree)
                except OSError:
                    raise BundleRefused(_WRITE_FAILED) from None
    except (store.StoreError, *_DIR_ERRORS):
        raise BundleRefused("the skills library is busy or unavailable; try again") from None


def build(bundle_id: str, skills: list[dict]) -> Path:
    """The bundle ``bundle_id`` holding exactly ``skills`` (``[{id, revision}]``), or
    `BundleRefused`. Never replaces an existing bundle."""
    name = _bundle_id(bundle_id)
    refs = _check(skills)
    with _root_fd() as root_fd:
        with contextlib.suppress(OSError):
            _sweep(root_fd)
        if _exists_at(root_fd, name):
            raise BundleRefused("this launch already has a bundle")
        staging = f"{_STAGING}{name}-{secrets.token_hex(6)}"
        try:
            staging_fd = _mkdir_at(root_fd, staging)
        except OSError:
            raise BundleRefused(_WRITE_FAILED) from None
        try:
            try:
                _copy_pinned(staging_fd, refs)
            finally:
                os.close(staging_fd)
            try:
                _publish(root_fd, staging, name)
            except OSError:
                raise BundleRefused(_WRITE_FAILED) from None
        except BaseException:
            with contextlib.suppress(OSError):
                _rmtree_at(root_fd, staging)
            raise
    return root() / name


def remove(bundle_id: str) -> bool:
    """Delete ``bundle_id``'s bundle; True when there was one. It is renamed out of its name
    first, so the skills tool stops serving it at once even if the deletion is slow or fails."""
    name = _bundle_id(bundle_id)
    with _root_fd() as root_fd:
        if not _exists_at(root_fd, name):
            return False
        gone = f"{_REMOVING}{name}-{secrets.token_hex(6)}"
        os.rename(name, gone, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        _rmtree_at(root_fd, gone)
    return True


def verify(path: str) -> str:
    """``path`` if it is a bundle this module built: an existing directory of this user, not
    group- or world-writable, directly under the root and named by a bundle id. The launch seams
    call this before handing a path to the skills tool, so it can never be pointed elsewhere."""
    if not isinstance(path, str) or os.path.dirname(path) != str(root()):
        raise BundleRefused("not a skill bundle")
    name = _bundle_id(os.path.basename(path))
    with _root_fd() as root_fd:
        try:
            fd = os.open(name, _DIR, dir_fd=root_fd)
        except OSError:
            raise BundleRefused("the skill bundle is missing") from None
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    if st.st_uid != os.geteuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BundleRefused("the skill bundle is not private")
    return path
