"""The operator's avatar (#1931): one image, shown beside the operator's messages in API sessions
and to the visitors of an API share link.

**One file, private, replaced atomically.** It lives at ``<root>/avatar`` (``root()``: env
``AGENT_SESSIONS_PROFILE_DIR``, default ``~/.config/agent-sessions/profile``). The directory is
``0700`` and the file ``0600``, the mode contract ``routes/upload.py`` argues. A write goes to a
temp name created ``O_EXCL`` in the bound directory, is pinned to ``0600`` before any byte, fsynced,
then ``rename``d over the old one, so a reader sees the old image or the new one, never half of
either.

**The bytes decide the type, never the client.** Only PNG, JPEG and WebP are accepted, by magic
number (``sniff``). SVG and GIF are refused: an SVG can carry script, and a share link serves this
image to strangers. Nothing re-encodes it (no image library is a dependency): the browser crops and
scales before upload, and the cap here holds whatever a client sends.

**Bound, never followed.** The directory is opened with ``O_NOFOLLOW`` on its last component and
must be a real directory owned by this user; the file is opened ``O_NOFOLLOW`` relative to that
descriptor and must be a regular file. A symlink at either reads as "no avatar" (``read`` never
raises: it feeds ``/api/config``); a write into a refused folder fails loudly.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV = "AGENT_SESSIONS_PROFILE_DIR"
FILE_NAME = "avatar"
DIR_MODE = 0o700
FILE_MODE = 0o600
#: The browser uploads a 256 px square; generous for that, small enough to hold in memory.
MAX_BYTES = 512 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_write_lock = threading.Lock()
log = logging.getLogger(__name__)


class AvatarError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Avatar:
    data: bytes
    mime: str
    version: str


def root() -> Path:
    raw = os.environ.get(ROOT_ENV)
    return Path(raw) if raw else Path.home() / ".config" / "agent-sessions" / "profile"


def sniff(data: bytes) -> str | None:
    """The image type ``data`` IS, by its magic number, or None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _open_dir(create: bool) -> int | None:
    """A descriptor on the profile directory, or None when it doesn't exist (and ``create`` is
    off). Refuses a symlink, a non-directory, another owner's directory or a loose mode."""
    d = root()
    if create:
        d.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.mkdir(d, DIR_MODE)
    try:
        fd = os.open(d, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AvatarError("the profile folder is not a plain directory", 500) from e
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise AvatarError("the profile folder belongs to another user", 500)
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            # Ours, but loose (an umask, a hand-made folder): tighten it before anything lands.
            os.fchmod(fd, DIR_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


#: The last read, keyed by the file's identity (inode, size, mtime): `/api/config` and every
#: visitor's projection ask for the version, and the bytes rarely change. A replace is a new inode.
_cache: tuple[tuple[int, int, int], Avatar] | None = None
_cache_lock = threading.Lock()


def read() -> Avatar | None:
    """The stored avatar, or None when there is none (or what is there is not one).

    **Never raises.** `/api/config` (the SPA's bootstrap, which carries the CSRF token) and every
    visitor's projection ask for the version, so a profile folder this module refuses — a symlink,
    another user's, not a directory — must read as "no avatar", not take the app down with a 500
    the operator could not even recover from in the UI. Writes still refuse it loudly."""
    try:
        return _read()
    except (AvatarError, OSError):
        log.warning("operator avatar unreadable; treated as none", exc_info=True)
        return None


def _read() -> Avatar | None:
    global _cache
    dir_fd = _open_dir(create=False)
    if dir_fd is None:
        return None
    try:
        try:
            fd = os.open(FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except OSError:
            return None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        with _cache_lock:
            if _cache is not None and _cache[0] == key:
                return _cache[1]
        data = fh.read(MAX_BYTES + 1)
    mime = sniff(data)
    if mime is None or len(data) > MAX_BYTES:
        return None
    avatar = Avatar(data=data, mime=mime, version=version_of(data))
    with _cache_lock:
        _cache = (key, avatar)
    return avatar


def version() -> str | None:
    """The stored avatar's version (for ``?v=`` cache-busting), or None."""
    a = read()
    return a.version if a else None


def write(data: bytes) -> str:
    """Store ``data`` as the avatar, replacing any; return its version."""
    if not data:
        raise AvatarError("empty image", 422)
    if len(data) > MAX_BYTES:
        raise AvatarError(f"image too large (max {MAX_BYTES // 1024} KiB)", 413)
    if sniff(data) is None:
        raise AvatarError("only PNG, JPEG or WebP images", 415)
    with _write_lock:
        dir_fd = _open_dir(create=True)
        assert dir_fd is not None
        tmp = f".avatar-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                FILE_MODE,
                dir_fd=dir_fd,
            )
            try:
                os.fchmod(fd, FILE_MODE)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, FILE_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dir_fd)
                raise
            with contextlib.suppress(OSError):
                os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return version_of(data)


def remove() -> None:
    """Remove the avatar. Idempotent."""
    with _write_lock:
        dir_fd = _open_dir(create=False)
        if dir_fd is None:
            return
        try:
            os.unlink(FILE_NAME, dir_fd=dir_fd)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        finally:
            os.close(dir_fd)


#: Every avatar response: the type is the sniffed one, never a guess; the bytes can't be rendered
#: as a document; and nothing keeps a copy (the URL carries ``?v=`` for freshness).
def response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": "inline",
    }
