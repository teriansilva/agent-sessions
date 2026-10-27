"""Private share-bundle state (#1714): previews, staged bundles, import journals, provenance.

Everything lives in one app-owned directory (`<AGENT_SESSIONS_HOME>/share-state`, 0700) outside
every editable library, opened component by component without following a link. Each file is a
private (0600, single-link, owned) regular file read and written relative to that directory's
descriptor; a write is a new temporary file, fsynced, renamed over the old name, then the
directory is fsynced. One `flock` serializes every preview write and every confirm.

The journal holds ids and digests only, never content or a secret value.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from ..plugins import storage as private
from ..plugins.provenance import ProvenanceError

NAME_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,95}")
LOCK_WAIT_S = 5.0
MAX_JSON_BYTES = 4 * 1024 * 1024


class StateError(RuntimeError):
    """The share state directory or one of its files is unusable."""


class Busy(StateError):
    """Another import holds the share lock."""


def root() -> Path:
    from ..gallery import storage as gallery_storage
    from ..playbooks import store

    path = (
        Path(os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions").expanduser()
        / "share-state"
    )
    resolved = Path(os.path.realpath(path))
    for editable in (store.local_root(), gallery_storage.skills_root()):
        if resolved.is_relative_to(Path(os.path.realpath(editable))):
            raise StateError("share state must be outside editable content")
    return path.absolute()


@contextlib.contextmanager
def _dir() -> Iterator[int]:
    try:
        with private.directory(root()) as fd:
            yield fd
    except (private.StateError, ProvenanceError) as exc:
        raise StateError(str(exc)) from None


def _name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise StateError("invalid share state name")
    return name


def _private(fd: int) -> None:
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.geteuid()
        or st.st_nlink != 1
        or st.st_mode & 0o077
    ):
        raise StateError("share state is not a private regular file")


@contextlib.contextmanager
def lock() -> Iterator[None]:
    """The one share lock: previews are recorded and confirms run under it."""
    with _dir() as dfd:
        fd = os.open(
            "share.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=dfd,
        )
        try:
            _private(fd)
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy("another import is running; try again") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)


def read_bytes(name: str, limit: int) -> bytes | None:
    with _dir() as dfd:
        try:
            fd = os.open(
                _name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd
            )
        except FileNotFoundError:
            return None
        try:
            _private(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
        finally:
            os.close(fd)
    if len(data) > limit:
        raise StateError("share state file is too large")
    return data


def write_bytes(name: str, data: bytes) -> None:
    with _dir() as dfd:
        tmp = f".tmp-{uuid.uuid4().hex}"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dfd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise StateError("share state write made no progress")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        os.close(fd)
        os.replace(tmp, _name(name), src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)


def read_json(name: str) -> dict | None:
    data = read_bytes(name, MAX_JSON_BYTES)
    if data is None:
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise StateError("share state is unreadable; it was left untouched") from None
    if not isinstance(value, dict):
        raise StateError("share state is unreadable; it was left untouched")
    return value


def write_json(name: str, value: dict) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise StateError("share state is too large")
    write_bytes(name, data)


def touch(name: str) -> None:
    """Refresh a state file's modification time (a re-staged bundle stays alive)."""
    with _dir() as dfd:
        os.utime(_name(name), dir_fd=dfd, follow_symlinks=False)


def remove(name: str) -> None:
    with _dir() as dfd, contextlib.suppress(FileNotFoundError):
        os.unlink(_name(name), dir_fd=dfd)


def entries() -> list[tuple[str, float, int]]:
    """Every state file's name, modification time and size (temporary files included)."""
    with _dir() as dfd:
        out = []
        for name in os.listdir(dfd):
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((name, st.st_mtime, st.st_size))
        return out


def remove_any(name: str) -> None:
    """Remove a listed file, including a leftover temporary file (pruning only)."""
    if not (NAME_RE.fullmatch(name) or re.fullmatch(r"\.tmp-[0-9a-f]{32}", name)):
        return
    with _dir() as dfd, contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dfd)
