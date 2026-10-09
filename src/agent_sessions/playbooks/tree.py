"""Read a bundle tree into memory under the material node policy (#863 §2, #1096 §2/§11).

The bundle is walked ONCE, descriptor-relative, and every later check reads the snapshot this
returns — never the path again — so the bytes validated are the bytes that were read.

**Node policy: regular files and directories only.** A symbolic link, a hard link (a regular file
with more than one name), a device node, a FIFO or a socket anywhere in the tree refuses the whole
bundle at load. It is not skipped: a skipped node is a node a later reader might follow. The one
link a playbook can ask for — an instruction alias such as `AGENTS.md` — is a DECLARATION in
`playbook.toml` (`kind = "instruction-alias"`), never a link on disk; deploy creates it (P2).

Every open is `O_NOFOLLOW` relative to its parent's descriptor, so a component swapped for a link
after it was listed fails the open instead of being followed.

The installed-source reader alone may supply a code-pinned complete file inventory. Those
release bytes can come from package-manager cache hard links; all other node and size rules
still apply, and a hash/inventory mismatch refuses before validation or disclosure.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field

from . import schema
from .errors import PlaybookFormatError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass
class Tree:
    """A bundle's content: every regular file's bytes and every directory, by relative path."""

    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)

    def listdir(self, rel: str) -> list[str]:
        """The direct children (files and directories) of `rel`, sorted."""
        prefix = f"{rel}/" if rel else ""
        out = {
            p[len(prefix) :].split("/", 1)[0]
            for p in (*self.files, *self.dirs)
            if p.startswith(prefix) and p != rel
        }
        return sorted(out)

    def digest(self) -> str:
        """The content revision of this snapshot: sha256 over every directory and every file's
        path, length and bytes, in a framed, sorted encoding (#1191). Two snapshots have the same
        digest exactly when they hold the same directories and the same files byte for byte, so it
        is what a stale edit, a stale delete and a deployment's pin compare."""
        h = hashlib.sha256(b"battlelab-playbook-tree/1\0")
        for d in sorted(self.dirs):
            h.update(b"d\0" + d.encode("utf-8") + b"\0")
        for p in sorted(self.files):
            data = self.files[p]
            h.update(b"f\0" + p.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
            h.update(data)
        return h.hexdigest()


def _lstat_at(name: str, dir_fd: int) -> os.stat_result:
    """`lstat` relative to a directory descriptor. A seam, so the device-node case — which cannot be
    created without privileges — can be tested against the real walk."""
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def node_problem(mode: int, nlink: int) -> str | None:
    """Why a node is refused, or None for a regular file / directory."""
    if stat.S_ISLNK(mode):
        return "is a symbolic link (bundles may not contain links; declare an instruction alias)"
    if stat.S_ISDIR(mode):
        return None
    if stat.S_ISREG(mode):
        return "is a hard link (a file with more than one name)" if nlink > 1 else None
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "is a device node"
    if stat.S_ISFIFO(mode):
        return "is a FIFO"
    if stat.S_ISSOCK(mode):
        return "is a socket"
    return "is not a regular file or directory"


def check_segment(seg: str, where: str) -> None:
    if not schema.PATH_SEG_RE.fullmatch(seg) or seg in (".", ".."):
        raise PlaybookFormatError(where, f"path segment {seg!r} is not allowed")
    if seg.lower() == ".git":
        raise PlaybookFormatError(where, "nothing in a bundle may be named .git")


class _Walk:
    def __init__(self, installed_files: dict[str, str] | None = None) -> None:
        self.tree = Tree()
        self.entries = 0
        self.total = 0
        self.installed_files = installed_files

    def node_problem(self, st: os.stat_result, rel: str) -> str | None:
        # uv may hard-link package data to its cache. Only an exact release inventory supplied
        # by the installed-source reader can admit those files; their bytes are checked below.
        nlink = (
            1 if self.installed_files is not None and rel in self.installed_files else st.st_nlink
        )
        return node_problem(st.st_mode, nlink)

    def read_file(self, dir_fd: int, name: str, rel: str) -> None:
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as e:
            raise PlaybookFormatError(rel, f"cannot be opened ({e.strerror})") from None
        try:
            st = os.fstat(fd)
            problem = self.node_problem(st, rel)
            if problem:
                raise PlaybookFormatError(rel, problem)
            if not stat.S_ISREG(st.st_mode):
                # Listed as a regular file, opened as something else (a directory swapped in
                # between): the descriptor is what is read, so the descriptor decides.
                raise PlaybookFormatError(rel, "changed while the bundle was being read")
            if st.st_size > schema.MAX_FILE_BYTES:
                raise PlaybookFormatError(rel, f"is larger than {schema.MAX_FILE_BYTES} bytes")
            chunks: list[bytes] = []
            size = 0
            while size <= schema.MAX_FILE_BYTES:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as e:
                    raise PlaybookFormatError(rel, f"cannot be read ({e.strerror})") from None
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > schema.MAX_FILE_BYTES:
                raise PlaybookFormatError(rel, f"is larger than {schema.MAX_FILE_BYTES} bytes")
        finally:
            os.close(fd)
        self.total += size
        if self.total > schema.MAX_TOTAL_BYTES:
            raise PlaybookFormatError("", f"bundle is larger than {schema.MAX_TOTAL_BYTES} bytes")
        data = b"".join(chunks)
        if self.installed_files is not None:
            if hashlib.sha256(data).hexdigest() != self.installed_files.get(rel):
                raise PlaybookFormatError(rel, "does not match the installed release resource")
        self.tree.files[rel] = data

    def walk(self, dir_fd: int, rel: str, depth: int) -> None:
        if depth > schema.MAX_TREE_DEPTH:
            raise PlaybookFormatError(rel, f"is nested deeper than {schema.MAX_TREE_DEPTH}")
        with os.scandir(dir_fd) as it:
            names = sorted(e.name for e in it)
        for name in names:
            child = f"{rel}/{name}" if rel else name
            check_segment(name, child)
            if len(child) > schema.PATH_MAX:
                raise PlaybookFormatError(child, f"path is longer than {schema.PATH_MAX}")
            self.entries += 1
            if self.entries > schema.MAX_TREE_ENTRIES:
                raise PlaybookFormatError(
                    "", f"bundle has more than {schema.MAX_TREE_ENTRIES} entries"
                )
            st = _lstat_at(name, dir_fd)
            problem = self.node_problem(st, child)
            if problem:
                raise PlaybookFormatError(child, problem)
            if stat.S_ISDIR(st.st_mode):
                try:
                    sub = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as e:
                    raise PlaybookFormatError(child, f"cannot be opened ({e.strerror})") from None
                try:
                    self.tree.dirs.add(child)
                    self.walk(sub, child, depth + 1)
                finally:
                    os.close(sub)
            else:
                self.read_file(dir_fd, name, child)


def read_tree(root: str | os.PathLike) -> Tree:
    """Snapshot the bundle at `root`, or raise `PlaybookFormatError`. The root itself must be a real
    directory: a symlinked bundle root is refused like any other link."""
    try:
        fd = os.open(os.fspath(root), _DIR_FLAGS)
    except OSError as e:
        raise PlaybookFormatError("", f"bundle root cannot be opened ({e.strerror})") from None
    try:
        w = _Walk()
        w.walk(fd, "", 0)
        return w.tree
    finally:
        os.close(fd)


def read_tree_at(dir_fd: int, name: str, *, installed_files: dict[str, str] | None = None) -> Tree:
    """``read_tree`` of the bundle directory ``name`` RELATIVE to ``dir_fd`` (``O_NOFOLLOW``): the
    local playbook store reads its own root through the descriptor it holds, never a path string.

    Only the installed-source reader supplies `installed_files`: code-pinned hashes of every
    file. Local/catalog callers omit it and always retain the single-link node policy."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        raise PlaybookFormatError("", f"bundle root cannot be opened ({e.strerror})") from None
    try:
        w = _Walk(installed_files)
        w.walk(fd, "", 0)
        if installed_files is not None:
            dirs = {p.rpartition("/")[0] for p in installed_files if "/" in p}
            dirs = {
                "/".join(p.split("/")[:i]) for p in dirs for i in range(1, len(p.split("/")) + 1)
            }
            if set(w.tree.files) != set(installed_files) or w.tree.dirs != dirs:
                raise PlaybookFormatError("", "does not match the installed release inventory")
        return w.tree
    finally:
        os.close(fd)
