"""Operator-owned plugin state outside all artifact trees (#1259).

Directory traversal is descriptor-relative and refuses symlinks and foreign write access.
Stable sidecar locks serialize mutations; atomicjson supplies fsync + atomic replacement.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path

from ..atomicjson import atomic_write_json
from . import plugin_state_home, plugins_home, provenance

MAX_STATE_BYTES = 8 * 1024 * 1024


class StateError(ValueError):
    pass


@contextlib.contextmanager
def directory(path: Path) -> Iterator[int]:
    path = path.absolute()
    if ".." in path.parts:
        raise StateError("plugin directories must be normalized")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parts = path.parts[1:]
        provenance._check_dir_st(os.fstat(fd), fd, "/", last=not parts)
        for index, part in enumerate(parts):
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
            provenance._check_dir_st(os.fstat(fd), fd, str(path), last=index == len(parts) - 1)
        yield fd
    finally:
        os.close(fd)


def root() -> Path:
    state, artifacts = plugin_state_home().absolute(), plugins_home().absolute()
    if os.path.commonpath(
        [os.path.realpath(state), os.path.realpath(artifacts)]
    ) == os.path.realpath(artifacts):
        raise StateError("plugin state must be outside the artifact directory")
    return state


@contextlib.contextmanager
def locked(name: str, *, wait: float = 2) -> Iterator[Path]:
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in name):
        raise StateError("invalid state name")
    path = root() / name
    with directory(path.parent) as dfd:
        fd = os.open(
            name + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd
        )
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_nlink != 1
                or st.st_mode & 0o077
            ):
                raise StateError("plugin lock is not private")
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateError("another plugin operation is busy") from None
                    time.sleep(0.02)
            yield path
        finally:
            os.close(fd)


def read(path: Path) -> dict | None:
    try:
        fd, _ = provenance.open_verified(str(path), canonicalize=False)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_nlink != 1 or st.st_mode & 0o077:
            raise StateError("plugin state is not private")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES:
            raise StateError("plugin state is too large")
        doc = json.loads(data)
        if not isinstance(doc, dict):
            raise ValueError
        return doc
    except (ValueError, UnicodeError):
        raise StateError("plugin state is unreadable; it was left untouched") from None
    finally:
        os.close(fd)


def write(path: Path, doc: dict) -> None:
    # Called only while locked() owns the already verified parent and stable sidecar.
    if len(json.dumps(doc).encode()) > MAX_STATE_BYTES:
        raise StateError("plugin state is too large")
    atomic_write_json(path, doc)
