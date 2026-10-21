"""One admitted material effect through held parents, retaining every displaced entry (#1191).

The lifecycle supplies the frozen change, reviewed parent identities, a private per-effect
directory on the destination filesystem, a durable progress callback and live admission. This
module owns no project state and grants no review authority. Recovery must inspect its retained
entries before retrying; an occupied effect directory is never overwritten or automatically
pruned. No operation here executes a probe, an agent, a shell or an outbound request.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Callable, Iterator

from .. import fileedit, filewrite, renameat
from ..fsbrowse import FsError
from . import destination, materials, mutation_plan


@contextlib.contextmanager
def _parent(
    folder: destination.Folder,
    path: str,
    parents: dict[str, destination.Node],
    admit: Callable[[], None],
) -> Iterator[tuple[int, str, Callable[[], None]]]:
    parts = filewrite.validate_relpath(path)
    destination._guard(folder, parts)
    with destination.open_folder(folder) as root, contextlib.ExitStack() as stack:
        held = [(root, folder.path)]
        fd = root
        prefix = []
        for segment in parts[:-1]:
            prefix.append(segment)
            rel = "/".join(prefix)
            expected = parents.get(rel)
            if expected is None or expected.kind != "directory":
                raise FsError("a material parent has no recorded directory identity", status=409)
            fd = stack.enter_context(destination._open(segment, destination._DIR_FLAGS, dir_fd=fd))
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != expected.identity[:2]:
                raise FsError("a material parent was replaced", status=409)
            held.append((fd, os.path.join(folder.path, *prefix)))

        def guard():
            admit()
            for parent_fd, absolute in held:
                destination._verify(parent_fd, absolute, folder)
            destination._guard(folder, parts)

        guard()
        yield fd, parts[-1], guard
        guard()


def _entry(fd: int, parent: int) -> None:
    st = os.fstat(fd)
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) != 0o700
        or st.st_dev != os.fstat(parent).st_dev
    ):
        raise FsError(
            "material recovery needs a private directory on the same filesystem", status=409
        )


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _move(source: int, name: str, target: int, retained: str) -> None:
    renameat.renameat2(source, name, target, retained, renameat.RENAME_NOREPLACE)
    os.fsync(target)
    os.fsync(source)


def _withdraw(parent: int, name: str, entry: int, expected: os.stat_result) -> None:
    """Retain the tentative name; if a writer substituted it, put that entry back unchanged."""
    try:
        _move(parent, name, entry, "withdrawn")
    except FileNotFoundError:
        return
    current = os.stat("withdrawn", dir_fd=entry, follow_symlinks=False)
    if not _same_inode(current, expected):
        fileedit._put_back(entry, "withdrawn", parent, name)


def create(
    folder: destination.Folder,
    change: mutation_plan.Change,
    parents: dict[str, destination.Node],
    entry: int,
    *,
    progress: Callable[[dict], None],
    admit: Callable[[], None],
) -> None:
    """Publish a prepared inode by exclusive link, then checkpoint under its live guard.

    The effect's entry and intent must already be durable. A file candidate is leased from its
    creation descriptor through post-link settlement. A typed instruction symlink is immutable
    and its linked inode and target are checked instead. Neither path can clobber a claimant.
    """
    if change.action != "create" or change.before.kind != "absent":
        raise FsError("material creation requires an absent reviewed destination", status=409)
    after = change.after
    if after.kind not in {"file", "symlink"}:
        raise FsError("unsupported material creation", status=422)
    with _parent(folder, change.path, parents, admit) as (parent, name, guard):
        _entry(entry, parent)
        progress({"phase": "intent"})
        with contextlib.ExitStack() as stack:
            if after.kind == "file":
                if not isinstance(after.data, bytes) or len(after.data) > fileedit.MAX_EDIT_BYTES:
                    raise FsError("material bytes exceed the file limit", status=422)
                fd = os.open(
                    "candidate",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=entry,
                )
                stack.callback(os.close, fd)
                fileedit._take_lease(fd)
                stack.callback(fileedit._release_lease, fd)
                fileedit._write_all(fd, after.data)
                os.fsync(fd)
                candidate = os.fstat(fd)
            else:
                mutation_plan.ownership(
                    {
                        change.path: {
                            "kind": "symlink",
                            "disposition": "managed",
                            "target": after.target,
                        }
                    }
                )
                os.symlink(after.target, "candidate", dir_fd=entry)
                fd = os.open("candidate", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=entry)
                stack.callback(os.close, fd)
                candidate = os.fstat(fd)
                if (
                    not stat.S_ISLNK(candidate.st_mode)
                    or os.readlink("", dir_fd=fd) != after.target
                ):
                    raise FsError(
                        "the instruction candidate changed before publication", status=409
                    )
            os.fsync(entry)
            progress({"phase": "staged", "inode": [candidate.st_dev, candidate.st_ino]})
            guard()
            linked = False
            try:
                if after.kind == "file":
                    if not fileedit._lease_intact(fd):
                        raise FsError(
                            "the material candidate was opened before publication", status=409
                        )
                # linkat follows the proc descriptor to the pinned inode, including an O_PATH
                # descriptor for a symlink. A substituted staging name is never a source.
                os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=parent, follow_symlinks=True)
                linked = True
                os.fsync(parent)
                guard()
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not _same_inode(named, candidate):
                    raise FsError("the created material was replaced before settlement", status=409)
                if after.kind == "file":
                    digest, _, truncated = fileedit._sha256_fd(fd, fileedit.MAX_EDIT_BYTES)
                    if (
                        truncated
                        or digest != materials.digest(after.data)
                        or not fileedit._lease_intact(fd)
                    ):
                        raise FsError(
                            "the material candidate changed before settlement", status=409
                        )
                elif os.readlink(name, dir_fd=parent) != after.target:
                    raise FsError("the instruction alias changed before settlement", status=409)
                progress({"phase": "done", "inode": [candidate.st_dev, candidate.st_ino]})
            except BaseException:
                if linked:
                    _withdraw(parent, name, entry, candidate)
                raise
            # Only our settled candidate pin is dropped. Displaced/withdrawn names never are.
            pin = os.stat("candidate", dir_fd=entry, follow_symlinks=False)
            if not _same_inode(pin, candidate):
                raise FsError("the material recovery pin changed", status=409)
            os.unlink("candidate", dir_fd=entry)
            os.fsync(entry)


def remove(
    folder: destination.Folder,
    change: mutation_plan.Change,
    parents: dict[str, destination.Node],
    entry: int,
    *,
    progress: Callable[[dict], None],
    admit: Callable[[], None],
) -> None:
    """Move the exact reviewed inode into durable retention; never unlink displaced bytes."""
    if change.action != "remove" or change.after.kind != "absent":
        raise FsError("material removal requires a reviewed removal", status=409)
    before = change.before
    if before.kind not in {"file", "symlink"}:
        raise FsError("unsupported material removal", status=422)
    with _parent(folder, change.path, parents, admit) as (parent, name, guard):
        _entry(entry, parent)
        if destination._leaf(parent, name) != before:
            raise FsError("the material changed after review", status=409)
        with contextlib.ExitStack() as stack:
            if before.kind == "file":
                fd = stack.enter_context(
                    destination._open(name, destination._FILE_FLAGS, dir_fd=parent)
                )
                if destination._identity(os.fstat(fd)) != before.identity:
                    raise FsError("the material changed before its lease", status=409)
                fileedit._take_lease(fd)
                stack.callback(fileedit._release_lease, fd)
            else:
                fd = None
            progress({"phase": "intent", "inode": list(before.identity[:2])})
            guard()
            moved = False
            try:
                renameat.renameat2(parent, name, entry, "removed", renameat.RENAME_NOREPLACE)
                moved = True
                os.fsync(entry)
                os.fsync(parent)
                kept = os.stat("removed", dir_fd=entry, follow_symlinks=False)
                if (kept.st_dev, kept.st_ino) != before.identity[:2]:
                    raise FsError("the material was replaced during removal", status=409)
                if fd is not None:
                    digest, _, truncated = fileedit._sha256_fd(fd, fileedit.MAX_EDIT_BYTES)
                    if (
                        truncated
                        or digest != materials.digest(before.data)
                        or not fileedit._lease_intact(fd)
                    ):
                        raise FsError("the material was edited during removal", status=409)
                elif os.readlink("removed", dir_fd=entry) != before.target:
                    raise FsError("the instruction alias changed during removal", status=409)
                guard()
                progress({"phase": "done", "inode": list(before.identity[:2])})
            except BaseException:
                if moved:
                    fileedit._put_back(entry, "removed", parent, name)
                raise
