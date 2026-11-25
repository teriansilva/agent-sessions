"""Secret files (#1786 P1): the sealed blobs behind ``kind: "secret_file"`` variables.

A secret file's ciphertext lives in its own blob beside the variables store, never in
``template-variables.json`` (which every send reads): ``<store dir>/secret-files/<id>.<rev>.bin``,
the directory 0700 and every blob 0600. The record keeps ``file: {name, size, rev}`` only. A blob is
sealed by `template_secrets.seal_bytes` under the store key with the AAD
``secret_file:<scoped identity>:<id>:<rev>``, so a blob copied onto another id, scope or revision
does not open there.

**Write order** (a JSON commit and a blob rename are not one atomic step):

1. publish the NEW blob ``<id>.<rev+1>.bin`` — a dot-named temp file, fsynced, moved into place with
   ``renameat2(RENAME_NOREPLACE)``, the directory fsynced. Nothing refers to it yet;
2. the caller commits the record (rev+1) in the store's one transaction, having retired the old
   value into the redaction set first;
3. after the commit, the OLD blob is truncated and unlinked.

A crash between 1 and 2 leaves an orphan new blob; between 2 and 3, an orphan old one. ``sweep``
(run at app start and inside every file write, under the store lock) finds both: a blob that is not
a current ``(id, rev)`` is opened under the identity the v4 LEDGER recorded for its id — never a
scope guessed from its name — retired into redaction, truncated and unlinked. A blob whose id is not
in the ledger, or that does not open under it, is moved to ``.quarantine/`` and counted: its
plaintext is unknown, so it is never deleted unseen. Only the operator removes it.

A CURRENT record whose blob is missing or does not open reads as needs re-entry, and redaction
refuses (`template_vars.secret_values_checked`), exactly like an unrecoverable secret record.

Every file is opened relative to the directory descriptor with ``O_NOFOLLOW | O_NONBLOCK`` and
judged on its own descriptor: regular, single-link, this user's, within the size bound.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from collections.abc import Iterable
from pathlib import Path

from . import renameat, template_secrets

#: The largest secret file, in plaintext bytes.
MAX_BYTES = 256 * 1024
#: The longest stored file name (display metadata only; never a path component).
FILE_NAME_MAX = 255
#: The largest revision a record may carry.
REV_MAX = 10**9
QUARANTINE = ".quarantine"
_TMP_PREFIX = ".tmp-"
_BLOB_RE = re.compile(r"([0-9a-f]{32})\.([1-9][0-9]{0,9})\.bin")
#: A sealed blob is the plaintext plus the header and the GCM tag.
_BLOB_MAX = MAX_BYTES + 64
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SecretFileUnavailable(OSError):
    """The secret-file directory is not ours or not private; nothing is read or written."""


def root() -> Path:
    from . import template_vars

    return template_vars.store_path().parent / "secret-files"


def aad(scope: str, project_id: str | None, name: str, vid: str, rev: int) -> str:
    return f"secret_file:{template_secrets.scoped_aad(scope, project_id, name)}:{vid}:{rev}"


def blob_name(vid: str, rev: int) -> str:
    name = f"{vid}.{rev}.bin"
    if not _BLOB_RE.fullmatch(name):
        raise ValueError("not a secret-file identity")
    return name


def _private(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise SecretFileUnavailable("the secret-file directory is not private to this user")


def _open_dir(path: Path, *, create: bool) -> int | None:
    """A descriptor on ``path``, verified private; ``None`` when it does not exist and ``create``
    is false."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    try:
        fd = os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        if create:
            raise
        return None
    try:
        _private(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd(fd: int) -> bytes | None:
    """The bytes behind ``fd`` if it is a regular, single-link file of ours within the bound."""
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_nlink != 1
        or st.st_uid != os.geteuid()
        or st.st_size > _BLOB_MAX
    ):
        return None
    chunks = []
    left = _BLOB_MAX + 1
    while left > 0:
        chunk = os.read(fd, min(left, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    data = b"".join(chunks)
    return None if len(data) > _BLOB_MAX else data


def publish(vid: str, rev: int, aad_: str, data: bytes) -> None:
    """Seal ``data`` and publish it as ``<vid>.<rev>.bin``. Never replaces a name: an existing blob
    under it raises ``FileExistsError`` and nothing is left behind."""
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("a secret file is 1 byte to 256 KiB")
    sealed = template_secrets.seal_bytes(aad_, data)
    final = blob_name(vid, rev)
    top = root()
    top.parent.mkdir(parents=True, exist_ok=True)
    dfd = _open_dir(top, create=True)
    assert dfd is not None
    try:
        tmp = f"{_TMP_PREFIX}{secrets.token_hex(16)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=dfd)
        try:
            view = memoryview(sealed)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            renameat.renameat2(dfd, tmp, dfd, final, renameat.RENAME_NOREPLACE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read(vid: str, rev: int, aad_: str, *, checked: bool = False) -> bytes | None:
    """The plaintext of ``<vid>.<rev>.bin``, or ``None``: missing, not a plain file of ours, or it
    does not open under ``aad_``. ``checked`` (REDACTION) raises on an unreadable key file."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return None
    try:
        try:
            fd = os.open(blob_name(vid, rev), os.O_RDONLY | _FILE_FLAGS, dir_fd=dfd)
        except (FileNotFoundError, OSError):
            return None
        try:
            sealed = _read_fd(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    if sealed is None:
        return None
    return template_secrets.open_bytes(aad_, sealed, checked=checked)


def _plain_file(dfd: int, name: str) -> bool:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid()


def _destroy(dfd: int, name: str) -> bool:
    """Truncate and unlink ``name`` when it is a plain file of ours; leave anything else alone
    (a directory, a FIFO, a link: never opened for writing). True when it was destroyed."""
    if not _plain_file(dfd, name):
        return False
    try:
        fd = os.open(name, os.O_WRONLY | _FILE_FLAGS, dir_fd=dfd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            return False
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dfd)
    os.fsync(dfd)
    return True


def identity(vid: str, rev: int) -> str:
    """One stored blob's identity (inode + modification time), or ``"absent"``. A blob published
    again under the same name (a re-upload after a clear restarts at revision 1) differs, so a
    copy keyed on it is never mistaken for the new bytes. A directory that is not ours raises."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return "absent"
    try:
        st = os.stat(blob_name(vid, rev), dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    finally:
        os.close(dfd)
    return f"{st.st_ino}.{st.st_mtime_ns}"


def remove(vid: str, rev: int) -> None:
    """Truncate and unlink one blob. A missing blob or directory is already removed."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return
    try:
        _destroy(dfd, blob_name(vid, rev))
    finally:
        os.close(dfd)


def _quarantine(dfd: int, name: str) -> None:
    qfd = None
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(QUARANTINE, 0o700, dir_fd=dfd)
        qfd = os.open(QUARANTINE, _DIR_FLAGS, dir_fd=dfd)
        _private(qfd)
        target = name
        for n in range(2, 1000):
            try:
                renameat.renameat2(dfd, name, qfd, target, renameat.RENAME_NOREPLACE)
                break
            except FileExistsError:
                target = f"{name}.{n}"
        os.fsync(qfd)
        os.fsync(dfd)
    finally:
        if qfd is not None:
            os.close(qfd)


def sweep(ledger: Iterable[dict], current: set[tuple[str, int]]) -> int:
    """Settle every blob that is not a current ``(id, rev)``; returns how many were quarantined.

    Runs under the variables store lock (no writer is mid-publish), so a leftover temp file is from
    an interrupted write and is destroyed. An orphan whose id the ledger knows as a secret file and
    that opens under that identity is retired into redaction, then truncated and unlinked; anything
    else is quarantined."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return 0
    by_id = {e["id"]: e for e in ledger if e.get("kind") == "secret_file"}
    moved = 0
    try:
        for name in sorted(os.listdir(dfd)):
            if name == QUARANTINE:
                continue
            if name.startswith(_TMP_PREFIX):
                # A plain leftover of an interrupted write is destroyed; anything else under a
                # temp name (a directory, a FIFO) is moved aside, never opened (independent review).
                if not _destroy(dfd, name):
                    _quarantine(dfd, name)
                    moved += 1
                continue
            m = _BLOB_RE.fullmatch(name)
            if m and (m.group(1), int(m.group(2))) in current:
                continue  # a current blob is never touched, even an emptied one (needs re-entry)
            if _plain_file(dfd, name) and os.stat(name, dir_fd=dfd).st_size == 0:
                _destroy(dfd, name)  # emptied by an interrupted destroy: nothing left to review
                continue
            entry = by_id.get(m.group(1)) if m else None
            if m and entry is not None:
                vid, rev = m.group(1), int(m.group(2))
                data = read(
                    vid, rev, aad(entry["scope"], entry["project_id"], entry["name"], vid, rev)
                )
                if data is not None:
                    retire(data)
                    _destroy(dfd, name)
                    continue
            _quarantine(dfd, name)
            moved += 1
    finally:
        os.close(dfd)
    return moved


def signature() -> tuple:
    """What the redaction cache keys on (#1786, Hermes 7398): every blob's name, inode, size and
    times. A blob removed, replaced or re-sealed under the same name changes it, so a warm cache
    never vouches for bytes it did not read. A directory that is not ours raises (fail closed)."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return ("absent",)
    try:
        out = []
        for name in sorted(os.listdir(dfd)):
            if name == QUARANTINE or name.startswith(_TMP_PREFIX):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # destroyed between the listing and the stat: simply not there
            out.append((name, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        return tuple(out)
    finally:
        os.close(dfd)


def quarantined() -> int:
    """How many unrecognized blobs wait in quarantine for the operator."""
    dfd = _open_dir(root(), create=False)
    if dfd is None:
        return 0
    try:
        try:
            qfd = os.open(QUARANTINE, _DIR_FLAGS, dir_fd=dfd)
        except FileNotFoundError:
            return 0
        try:
            return len(os.listdir(qfd))
        finally:
            os.close(qfd)
    finally:
        os.close(dfd)


def text(data: bytes) -> str | None:
    """The file as text when it is UTF-8 — the only form redaction can match."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def retire(data: bytes) -> None:
    """Keep redacting a file that is being replaced or removed (it may be in transcripts)."""
    value = text(data)
    if value is not None:
        template_secrets.retire([value])
