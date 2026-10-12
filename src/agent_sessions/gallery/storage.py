"""Private gallery evidence and stable sidecar locks outside editable libraries."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from ..plugins import storage as private
from .contract import GalleryError


def root() -> Path:
    from ..playbooks import store

    path = (
        Path(os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions").expanduser()
        / "gallery-state"
    )
    for editable in (store.local_root(), skills_root()):
        resolved = Path(os.path.realpath(editable))
        if Path(os.path.realpath(path)).is_relative_to(resolved):
            raise GalleryError("Gallery evidence must be outside editable content.")
    return path


def skills_root() -> Path:
    return (
        Path(os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions").expanduser()
        / "skills-library"
    )


@dataclass(frozen=True)
class _Directory:
    path: Path
    fd: int

    def check(self):
        try:
            held = os.fstat(self.fd)
            private.provenance._check_dir_st(held, self.fd, str(self.path), last=True)
            named = os.stat(self.path, follow_symlinks=False)
            same = (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
        except OSError:
            same = False
        if not same:
            raise GalleryError("Gallery evidence directory changed; inspect retained state.")


_HELD: ContextVar[_Directory | None] = ContextVar("gallery_evidence_directory", default=None)
_LIMIT = 8 * 1024 * 1024


@contextlib.contextmanager
def _directory(path: Path):
    path = path.absolute()
    held = _HELD.get()
    if held is not None:
        if path != held.path:
            raise GalleryError("Gallery evidence directory changed during an operation.")
        held.check()
        yield held
        held.check()
        return
    with private.directory(path) as fd:
        held = _Directory(path, fd)
        token = _HELD.set(held)
        try:
            held.check()
            yield held
            held.check()
        finally:
            _HELD.reset(token)


@contextlib.contextmanager
def locked(name: str):
    if not re.fullmatch(r"[a-z0-9-]{1,80}", name):
        raise GalleryError("Invalid gallery state name.")
    # Nested publication/index/origin/operation locks share this exact directory. A
    # pathname replacement must never switch a caller to evidence under another lock.
    with _directory(root()) as held:
        fd = os.open(
            name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=held.fd,
        )
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_nlink != 1
                or st.st_mode & 0o077
            ):
                raise GalleryError("Gallery state lock is not private.")
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GalleryError("Another gallery operation is busy.") from None
                    time.sleep(0.02)
            held.check()
            yield held.path / (name + ".json")
            held.check()
        finally:
            os.close(fd)


def read(path: Path) -> dict | None:
    if _HELD.get() is None:
        return private.read(path, max_bytes=_LIMIT)
    with _directory(path.parent) as held:
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=held.fd,
            )
        except FileNotFoundError:
            return None
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_nlink > 1
                or st.st_mode & 0o077
            ):
                raise GalleryError("Gallery evidence is not a private regular file.")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(_LIMIT + 1)
            if len(data) > _LIMIT:
                raise GalleryError("Gallery evidence is too large.")
            try:
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise GalleryError(
                    "Gallery evidence is unreadable; it was left untouched."
                ) from None
            return value
        finally:
            os.close(fd)


def write(path: Path, value: dict) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    if len(payload) > _LIMIT:
        raise GalleryError("Gallery evidence is too large.")
    with _directory(path.parent) as held:
        name = ".state-" + uuid.uuid4().hex + ".tmp"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=held.fd,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("Gallery evidence write made no progress.")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        held.check()
        os.replace(name, path.name, src_dir_fd=held.fd, dst_dir_fd=held.fd)
        os.fsync(held.fd)
        # Failed writes retain their private temporary evidence. Never re-resolve an
        # absolute temp/destination name after a move, even for cleanup or fsync.
