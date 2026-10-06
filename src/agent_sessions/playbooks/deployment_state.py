"""Durable deployment records beneath the authoring root, with one lock per project (#1191).

Every lifecycle effect holds the authoring root shared and its project lock exclusively. The
authoring delete's exclusive root lock therefore sees either a committed deployment or none of
its effects. Records never contain plaintext secrets. Invalid or newer records refuse rather
than report that the project has no deployment.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import atomicjson, template_vars
from . import deployments, schema, store

DIRECTORY = ".deployments"
RECORD = "state.json"
VERSION = 1
MAX_RECORD_BYTES = 8 * schema.MAX_TOTAL_BYTES
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_STATES = frozenset({"bound", "binding_intent", "applied", "removed"})
#: Operation journals. Completed history lives in the `*_history` maps, not here; the remove
#: coordinator drops settled journals when it publishes `removed`.
_OPERATIONS = ("binding_operation", "apply_operation", "removal_operation")


def _unsettled(record: dict) -> bool:
    """Fail closed: ANY journal on a removed record keeps it holding, however it is marked.

    A journal's "complete" marker is not proof by itself (#1290 review): a damaged or partial
    payload could carry it. Only a removal that settled and dropped every journal releases.
    """
    return any(name in record for name in _OPERATIONS)


def holds(record: dict) -> bool:
    """A record holds its project and source playbook unless it is cleanly removed."""
    return record["state"] != "removed" or _unsettled(record)


def _directory(parent: int, name: str, create: bool) -> int | None:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
        # Also repair the durability of a directory created before an interrupted fsync.
        os.fsync(parent)
    try:
        fd = os.open(name, _FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if create:
            raise store.StoreError("the deployment directory disappeared", status=503) from None
        return None
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise store.StoreError(
            "deployment directories must be private and operator-owned", status=409
        )
    return fd


def _verify(parent: int, name: str, fd: int) -> None:
    named, held = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(fd)
    if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
        raise store.Conflict("the deployment state directory changed")


def _read(fd: int, pid: str) -> dict | None:
    try:
        source = os.open(
            RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd
        )
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(source)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_size > MAX_RECORD_BYTES
        ):
            raise store.StoreError(
                "the deployment record is not a bounded private file", status=409
            )
        chunks, size = [], 0
        while part := os.read(source, min(65536, MAX_RECORD_BYTES + 1 - size)):
            chunks.append(part)
            size += len(part)
            if size > MAX_RECORD_BYTES:
                raise store.StoreError("the deployment record is too large", status=409)
        after = os.stat(RECORD, dir_fd=fd, follow_symlinks=False)
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise store.Conflict("the deployment record changed while reading")
        value = json.loads(b"".join(chunks))
    except (UnicodeError, ValueError):
        raise store.StoreError("the deployment record is damaged", status=409) from None
    finally:
        os.close(source)
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VERSION
        or value.get("project_id") != pid
        or not isinstance(value.get("playbook_id"), str)
        or value.get("state") not in _STATES
    ):
        raise store.StoreError(
            "the deployment record is damaged or needs a newer BattleLab", status=409
        )
    store.playbook_id(value["playbook_id"])
    return value


@dataclass
class Locked:
    root_fd: int
    directory_fd: int
    project_fd: int
    project_id: str

    def verify(self) -> None:
        expected = str(store.local_root().resolve())
        if os.readlink(f"/proc/self/fd/{self.root_fd}") != expected:
            raise store.Conflict("the playbook root moved")
        named, held = store.local_root().lstat(), os.fstat(self.root_fd)
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
            held.st_dev,
            held.st_ino,
        ):
            raise store.Conflict("the playbook root was replaced")
        _verify(self.root_fd, DIRECTORY, self.directory_fd)
        _verify(self.directory_fd, self.project_id, self.project_fd)

    def read(self) -> dict | None:
        self.verify()
        value = _read(self.project_fd, self.project_id)
        self.verify()
        return value

    def write(self, value: dict) -> None:
        self.verify()
        if (
            value.get("project_id") != self.project_id
            or type(value.get("version")) is not int
            or value["version"] != VERSION
            or value.get("state") not in _STATES
        ):
            raise store.StoreError("invalid deployment record")
        store.playbook_id(value.get("playbook_id"))
        if len(json.dumps(value, ensure_ascii=True).encode()) > MAX_RECORD_BYTES:
            raise store.StoreError("the deployment record is too large")
        # The proc path names our held directory, so atomicjson's temp and rename cannot follow
        # a replaced project-state path. It writes 0600 before content and syncs file + parent.
        atomicjson.atomic_write_json(Path(f"/proc/self/fd/{self.project_fd}") / RECORD, value)
        self.verify()


@contextlib.contextmanager
def locked(project_id: str, *, create: bool = False) -> Iterator[Locked | None]:
    pid = template_vars.project_id(project_id)
    if create:
        root = store._open_root(create=True)
        assert root is not None
        os.close(root)
        # First use may have created the authoring root and its ancestors. Re-sync on retries:
        # merely observing a directory does not prove an earlier creation survived fsync.
        current = store.local_root().resolve()
        while current != Path.home() and current.stat().st_uid == os.geteuid():
            parent = os.open(current.parent, _FLAGS)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            if current == current.parent:
                break
            current = current.parent
    with store.root_lock(exclusive=False) as root:
        if root is None:
            yield None
            return
        with contextlib.ExitStack() as stack:
            directory = _directory(root, DIRECTORY, create)
            if directory is None:
                yield None
                return
            stack.callback(os.close, directory)
            project = _directory(directory, pid, create)
            if project is None:
                yield None
                return
            stack.callback(os.close, project)
            with store._flock(project, exclusive=True, wait=store.LOCK_WAIT_S):
                record = Locked(root, directory, project, pid)
                record.verify()
                yield record
                record.verify()


def active(project_id: str) -> dict | None:
    """The project's deployment while it holds the project; damaged records raise (fail closed).

    The project store calls this under its own index flock, which every lifecycle operation takes
    outermost, so no bind/apply/remove can commit between this read and that caller's write.
    """
    with locked(project_id) as held:
        record = held.read() if held is not None else None
    return record if record is not None and holds(record) else None


class Registry:
    """Called only inside the authoring store's exclusive root lock; never reacquire it."""

    def projects_running(self, playbook_id: str) -> list[dict]:
        root = store._open_root(create=False)
        if root is None:
            return []
        try:
            directory = _directory(root, DIRECTORY, False)
            if directory is None:
                return []
            try:
                result = []
                for name in os.listdir(directory):
                    pid = template_vars.project_id(name)
                    project = _directory(directory, pid, False)
                    if project is None:
                        raise deployments.DeploymentsUnavailable("a deployment disappeared")
                    try:
                        record = _read(project, pid)
                        if (
                            record is not None
                            and record["playbook_id"] == playbook_id
                            and holds(record)
                        ):
                            result.append({"project_id": pid})
                        _verify(directory, pid, project)
                    finally:
                        os.close(project)
                _verify(root, DIRECTORY, directory)
                return result
            finally:
                os.close(directory)
        finally:
            os.close(root)
