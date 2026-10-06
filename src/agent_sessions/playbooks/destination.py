"""Read a deployment destination through held descriptors (#1191).

The destination identity and complete pre-state belong in the review digest. This module never
creates a folder or writes a file. Applying a reviewed plan must re-read these facts under the
project lock, and still fence each individual mutation against external editors.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass

from .. import fileedit, files, filewrite, prefs, project_dirs
from ..fsbrowse import FsError
from . import schema
from .tree import check_segment

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class Folder:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class Node:
    kind: str
    identity: tuple[int, ...] = ()
    data: bytes | None = None
    target: str | None = None


def _scope(path: str) -> None:
    roots = project_dirs.effective_roots()
    if not roots or not project_dirs.in_scope(
        path, roots=roots, exclusions=prefs.get_folder_exclusions()
    ):
        raise FsError("the destination is outside the configured project roots", status=403)


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_nlink,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_uid,
        st.st_gid,
    )


def _verify(fd: int, path: str, root: Folder) -> None:
    _scope(path)
    if files._fd_still_contained(fd, root.path) != path:
        raise FsError("the destination moved during review", status=409)
    held = os.fstat(fd)
    named = os.stat(path, follow_symlinks=False)
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        raise FsError("the destination changed during review", status=409)


def review_folder(path: str) -> Folder:
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise FsError("an absolute destination folder is required", status=422)
    files._refuse_if_symlink(path)
    resolved = os.path.realpath(path)
    _scope(resolved)
    with _open(resolved, _DIR_FLAGS) as fd:
        st = os.fstat(fd)
        folder = Folder(resolved, st.st_dev, st.st_ino)
        _verify(fd, resolved, folder)
        return folder


@contextlib.contextmanager
def _open(path: str, flags: int, *, dir_fd: int | None = None) -> Iterator[int]:
    fd = os.open(path, flags, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_folder(folder: Folder) -> Iterator[int]:
    _scope(folder.path)
    with _open(folder.path, _DIR_FLAGS) as fd:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (folder.device, folder.inode):
            raise FsError("the reviewed destination folder was replaced", status=409)
        _verify(fd, folder.path, folder)
        yield fd
        _verify(fd, folder.path, folder)


def _guard(root: Folder, parts: list[str]) -> None:
    filewrite.refuse_git_metadata(root.path, parts)
    fileedit.refuse_recovery_store(root.path, parts)
    # Project roots may be outside the Files panel's home boundary. Reuse its gitdir-shape
    # predicate all the way up that path too, including a destination nested in a bare repo.
    ancestor = os.path.join(root.path, *parts[:-1])
    while True:
        if filewrite._looks_like_gitdir(ancestor):
            raise FsError("playbook materials may not touch git metadata", status=403)
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent


def _leaf(parent_fd: int, name: str) -> Node:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return Node("absent")
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(name, dir_fd=parent_fd)
        try:
            # A non-UTF-8 target decodes to surrogates that no JSON response can carry.
            target.encode("utf-8")
        except UnicodeEncodeError:
            raise FsError("an instruction link target is not valid UTF-8", status=409) from None
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(after):
            raise FsError("an instruction link changed during review", status=409)
        return Node("symlink", _identity(before), target=target)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise FsError("a material destination is not a single-link regular file", status=409)
    with _open(name, _FILE_FLAGS, dir_fd=parent_fd) as fd:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise FsError("a material destination is not a single-link regular file", status=409)
        if _identity(before) != _identity(opened):
            raise FsError("a material destination changed during review", status=409)
        if opened.st_size > schema.MAX_FILE_BYTES:
            raise FsError("a material destination exceeds the review size limit", status=413)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(65536, schema.MAX_FILE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > schema.MAX_FILE_BYTES:
                raise FsError("a material destination exceeds the review size limit", status=413)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(opened) != _identity(os.fstat(fd)) or _identity(opened) != _identity(named):
            raise FsError("a material destination changed during review", status=409)
        return Node("file", _identity(opened), data=b"".join(chunks))


def snapshot(folder: Folder, paths: list[str]) -> dict[str, Node]:
    """Read leaves plus each intermediate directory identity; never follow a component symlink.

    Symlinks at a leaf are reported without following them, so the planner can compare an exact
    recorded instruction alias. Every other material action must refuse that leaf classification.
    """
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise FsError("material destinations must be a list of relative paths", status=422)
    if len(paths) > schema.MAX_MATERIALS:
        raise FsError("too many material destinations", status=422)
    result: dict[str, Node] = {}
    total = 0
    with open_folder(folder) as root_fd:
        for path in sorted(set(paths)):
            parts = filewrite.validate_relpath(path)
            for segment in parts:
                check_segment(segment, path)
            _guard(folder, parts)
            with contextlib.ExitStack() as stack:
                parent_fd = root_fd
                prefix: list[str] = []
                missing = False
                for segment in parts[:-1]:
                    prefix.append(segment)
                    rel = "/".join(prefix)
                    if not missing:
                        try:
                            parent_fd = stack.enter_context(
                                _open(segment, _DIR_FLAGS, dir_fd=parent_fd)
                            )
                        except FileNotFoundError:
                            missing = True
                    node = (
                        Node("absent")
                        if missing
                        else Node("directory", _identity(os.fstat(parent_fd)))
                    )
                    if rel in result and result[rel] != node:
                        raise FsError("a material parent changed during review", status=409)
                    result[rel] = node
                    if not missing:
                        _verify(parent_fd, os.path.join(folder.path, *prefix), folder)
                leaf = Node("absent") if missing else _leaf(parent_fd, parts[-1])
                result[path] = leaf
                total += len(leaf.data or b"")
                if total > schema.MAX_TOTAL_BYTES:
                    raise FsError("material destinations exceed the review size limit", status=413)
                if not missing:
                    _verify(parent_fd, os.path.join(folder.path, *parts[:-1]), folder)
                    _guard(folder, parts)
    return result
