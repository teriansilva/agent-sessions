"""Private shared state for native API sessions and their worker generations (#1278).

Everything lives under ``<plugin state>/native/``, which every app instance sharing the source
stores must share (with the lock and runtime directories, see ``native-ownership.md``). Files
are private JSON written atomically; a per-session flock serializes every lifecycle change.

* ``<uuid>.json`` — the session record: adapter, source, cwd, requested model, the private
  creation owner token, the bound native id, the CURRENT worker generation and the closed gate.
* ``workers/<worker_id>/config.json`` — the one private document a worker reads at start: its
  capability and the reviewed launch inputs. Nothing in it is accepted from a browser.
* ``workers/<worker_id>/lifecycle.json`` — launch intent, the pinned systemd invocation and the
  worker's own phase reports.

A generation is launched at most once. The session record names exactly one current generation;
a worker that starts and finds itself not current (or the gate closed) exits before it creates
any native child, so a late start of an old generation can never produce a second writer.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from .plugins import storage

MAX_RECORD_BYTES = 256 * 1024
_LOCK_WAIT = 10.0


class StateError(RuntimeError):
    pass


class LockBusy(StateError):
    """Transient lifecycle contention; no admission decision has been made yet."""


def _uuid(value: object, name: str = "identity") -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise StateError(f"invalid native {name}") from None
    if str(parsed) != value:
        raise StateError(f"invalid native {name}")
    return value


def root() -> Path:
    return storage.root() / "native"


def session_path(session_id: str) -> Path:
    return root() / f"{_uuid(session_id, 'session id')}.json"


def worker_dir(worker_id: str) -> Path:
    return root() / "workers" / _uuid(worker_id, "worker id")


def ensure_dir(path: Path) -> None:
    """Create/verify a private directory chain (descriptor-relative, no symlinks)."""
    try:
        with storage.directory(path):
            pass
    except (OSError, ValueError, storage.StateError) as exc:
        raise StateError(f"native state directory is unavailable: {exc}") from None


@contextlib.contextmanager
def session_lock(session_id: str, *, wait: float = _LOCK_WAIT) -> Iterator[None]:
    """The per-session lifecycle fence: launch, close and recovery all hold it.

    Holding it while a launch runs is what makes "closed AND drained" a single observation:
    a closer that acquired this lock knows no launch of that session is outstanding.
    """
    _uuid(session_id, "session id")
    directory = root() / "locks"
    ensure_dir(directory)
    fd = os.open(
        directory / f"{session_id}.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink != 1
            or st.st_mode & 0o077
        ):
            raise StateError("native session lock is not private")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy("another native lifecycle operation is busy") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def read(path: Path) -> dict | None:
    try:
        return storage.read(path, max_bytes=MAX_RECORD_BYTES)
    except (OSError, storage.StateError) as exc:
        raise StateError(f"native state is unreadable: {exc}") from None


def write(path: Path, doc: dict) -> None:
    ensure_dir(path.parent)
    try:
        storage.write(path, doc, max_bytes=MAX_RECORD_BYTES)
    except (OSError, ValueError, storage.StateError) as exc:
        raise StateError(f"native state could not be written: {exc}") from None


def read_session(session_id: str) -> dict | None:
    return read(session_path(session_id))


def write_session(session_id: str, doc: dict) -> None:
    write(session_path(session_id), doc)


def read_config(worker_id: str) -> dict | None:
    return read(worker_dir(worker_id) / "config.json")


def write_config(worker_id: str, doc: dict) -> None:
    write(worker_dir(worker_id) / "config.json", doc)


def read_lifecycle(worker_id: str) -> dict:
    return read(worker_dir(worker_id) / "lifecycle.json") or {}


def update_lifecycle(worker_id: str, **fields) -> dict:
    """Merge fields under the worker's own sidecar lock (worker and web both report here)."""
    path = worker_dir(worker_id) / "lifecycle.json"
    ensure_dir(path.parent)
    fd = os.open(
        path.parent / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = read(path) or {}
        doc.update(fields)
        write(path, doc)
        return doc
    finally:
        os.close(fd)


def admitted(session_id: str, worker_id: str) -> bool:
    """Is this generation the session's current, open one? Read under the caller's lock."""
    record = read_session(session_id)
    return (
        record is not None
        and record.get("current_worker") == worker_id
        and record.get("closed") is not True
    )


LEASES = "native-leases"


def lease(release: Path | None, worker_id: str, *, remove: bool = False) -> None:
    """Hold (or drop) a generation's release lease under the installer's prune lock (#1278).

    `install.sh` prunes under ``<prefix>/.release-prune.lock`` and never removes a release
    with a lease, so a worker's code cannot disappear while it runs. Shared with the worker,
    which drops its own lease as its last act.
    """
    if release is None:
        return
    _uuid(worker_id, "worker id")
    lock = release.parent.parent / ".release-prune.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        leases = release / LEASES
        if remove:
            with contextlib.suppress(FileNotFoundError):
                (leases / worker_id).unlink()
            return
        if not release.is_dir():
            raise StateError("the installed release is no longer present")
        leases.mkdir(mode=0o700, exist_ok=True)
        entry = os.open(leases / worker_id, os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, 0o600)
        os.fsync(entry)
        os.close(entry)
        dfd = os.open(leases, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(fd)


def forget_config(worker_id: str) -> None:
    """A proved-gone generation's config (it holds the capability) has no further use."""
    with contextlib.suppress(FileNotFoundError):
        (worker_dir(worker_id) / "config.json").unlink()
