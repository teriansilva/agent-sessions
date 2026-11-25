"""An API session's journal is a chain of segment files under one stable lock (#1733).

Segment 0 is the session's original ``<root>/<id>.jsonl``; later segments are
``<root>/<id>.<n>.jsonl`` (n = 1, 2, …). Neither name matches the session listing (``<uuid>.jsonl``
exactly), so a segment never lists as a session. The ACTIVE segment is the highest index; a
lower one is frozen once its successor exists and is never written again.

Segment n ≥ 1 opens with a header record ``{"type": "segment", "index", "base_revision", "prev",
"carried"}``. ``prev`` pins the predecessor's exact bytes (size + sha256) and ``base_revision``
its last cursor, so cursors stay monotonic across the chain. A missing segment, a gap, a header
that names another index, or a predecessor that no longer matches its pin is corruption: the
whole chain is unreadable (fail closed), never silently merged or skipped.

Locking. A rename cannot move a flock from one inode to its successor, so every native journal
operation locks ``<root>/<id>.lock`` (never renamed or replaced): ``LOCK_EX`` to mutate,
``LOCK_SH`` to observe, and only then resolves and opens the active segment. While segment 0 is
the active one, its own descriptor is flocked too, in the same mode: a worker started by a
release from before the chain locks only that file, and must still be excluded. A frozen
segment is made read-only (0400), so such a worker's next append fails instead of writing
behind its successor. Ordinary (non-native) chat stores never come here: they keep
``chat_store``'s per-file lock and tolerant torn-tail reader unchanged.

Rollover (`rollover`) runs under the exclusive lock: sidecar temp → fsync → successor temp →
fsync → rename sidecar → rename successor → directory fsync → reopen and validate. A crash
leaves either no successor (the old segment stays active; an orphaned sidecar is ignored and
replaced by the next rollover) or a complete one. Any I/O failure here is `Unwritable`, with the
error class, and nothing was appended to the journal.

This module is storage only: what a header carries and what a sidecar indexes are
`native_journal`'s decisions.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SEGMENT_RE = re.compile(r"\.([1-9][0-9]{0,8})\.jsonl")
#: A successor's header holds the live control state it carries (`native_journal.CARRY_MAX_BYTES`)
#: plus the session header; reading one never needs more than this.
HEADER_MAX_BYTES = 9 * 1024 * 1024
#: A sidecar indexes one segment's operations; generous, and still bounded.
SIDECAR_MAX_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


class Unwritable(Exception):
    """The chain could not be extended (disk full, I/O error, fsync or rename failure).

    Raised only where nothing was appended to the journal, so the caller may report a known
    refusal. ``cause`` is one of `CAUSES`."""

    def __init__(self, cause: str, detail: str):
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


#: The error classes the operator sees, closed.
CAUSES = ("disk_full", "io_error", "write_failed")


def cause_of(exc: BaseException) -> str:
    code = getattr(exc, "errno", None)
    if code in (errno.ENOSPC, errno.EDQUOT):
        return "disk_full"
    if code == errno.EIO:
        return "io_error"
    return "write_failed"


def _check_id(session_id: str) -> str:
    if not _UUID_RE.fullmatch(session_id or ""):
        raise ValueError("malformed native session id")
    return session_id


def segment_path(root: Path, session_id: str, index: int) -> Path:
    _check_id(session_id)
    return root / (f"{session_id}.jsonl" if index == 0 else f"{session_id}.{index}.jsonl")


def sidecar_path(root: Path, session_id: str, index: int) -> Path:
    _check_id(session_id)
    return root / f"{session_id}.{index}.ops"


def lock_path(root: Path, session_id: str) -> Path:
    _check_id(session_id)
    return root / f"{session_id}.lock"


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _listed(root: Path, session_id: str) -> list[int]:
    found = []
    with os.scandir(root) as entries:
        for entry in entries:
            name = entry.name
            if not name.startswith(session_id):
                continue
            match = _SEGMENT_RE.fullmatch(name[len(session_id) :])
            if match is not None:
                found.append(int(match.group(1)))
    return sorted(found)


def indexes(root: Path, session_id: str, *, exhaustive: bool = True) -> list[int]:
    """The segment indexes n ≥ 1 present, ascending, found by probing ``<id>.<n>.jsonl`` upward:
    a session that never rolled over costs ONE stat, never a listing of the store directory
    (the session-list walk stays linear; the 100 ms events wake stamp stays microseconds).

    ``exhaustive``: also detect a GAP. A missing segment stops the probe on a FROZEN segment,
    which has its own sidecar (renamed in before the freeze) or is read-only; only then is the
    directory listed, and `resolve` refuses the non-contiguous result. The wake stamp and the
    growth signal are hints and never list."""
    _check_id(session_id)
    found = []
    index = 1
    while _exists(segment_path(root, session_id, index)):
        found.append(index)
        index += 1
    if not exhaustive:
        return found
    top = found[-1] if found else 0
    suspicious = _exists(sidecar_path(root, session_id, top))
    if not suspicious:
        try:
            suspicious = not os.lstat(segment_path(root, session_id, top)).st_mode & 0o200
        except FileNotFoundError:
            return found  # no segment 0: `resolve` fails on it anyway
    return _listed(root, session_id) if suspicious else found


def private(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & 0o077
        and st.st_nlink == 1
    )


def stamp_of(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def decode(line: bytes, limit: int) -> dict:
    """Strict JSON object: no duplicate keys, no NaN/Infinity, bounded. ValueError otherwise."""

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def invalid(_):
        raise ValueError("invalid constant")

    if len(line) > limit:
        raise ValueError("record exceeds its bound")
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("unreadable record") from exc
    if type(value) is not dict:
        raise ValueError("record is not an object")
    return value


def encode(record: dict) -> bytes:
    # Same encoding as every journal record (chat_store._records_bytes).
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8", "replace")


# --- verified identities ----------------------------------------------------------------------
# A frozen segment's (sha256, line count), memoized by its exact stat stamp. Any write to it —
# even a same-length rewrite with its mtime restored — moves ctime, misses here and is hashed
# again, so a tampered predecessor fails its pin on the next resolution.
_IDENTITY: OrderedDict[tuple, tuple[str, int]] = OrderedDict()
_HEADERS: OrderedDict[tuple, dict] = OrderedDict()
_MEMO = 512
_MEMO_LOCK = threading.Lock()


def _memo(table: OrderedDict, key, value=None):
    with _MEMO_LOCK:
        if value is None:
            hit = table.get(key)
            if hit is not None:
                table.move_to_end(key)
            return hit
        table[key] = value
        table.move_to_end(key)
        while len(table) > _MEMO:
            table.popitem(last=False)
        return value


def remember_identity(st: os.stat_result, sha256: str, lines: int) -> None:
    _memo(_IDENTITY, stamp_of(st), (sha256, lines))


def identity(path: Path, st: os.stat_result) -> tuple[str, int]:
    """(sha256 hex, number of LF-terminated lines) of the file exactly as ``st`` describes it."""
    hit = _memo(_IDENTITY, stamp_of(st))
    if hit is not None:
        return hit
    return _memo(_IDENTITY, stamp_of(st), hash_file(path, st))


def hash_file(path: Path, st: os.stat_result) -> tuple[str, int]:
    """Hash a frozen segment exactly as ``st`` describes it (the expensive part of `identity`)."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if stamp_of(before) != stamp_of(st):
            raise ValueError("a native journal segment changed while it was verified")
        digest = hashlib.sha256()
        lines = 0
        offset = 0
        while True:
            chunk = os.pread(fd, _READ_CHUNK, offset)
            if not chunk:
                break
            digest.update(chunk)
            lines += chunk.count(b"\n")
            offset += len(chunk)
        if offset != st.st_size or stamp_of(os.fstat(fd)) != stamp_of(st):
            raise ValueError("a native journal segment changed while it was verified")
    finally:
        os.close(fd)
    return digest.hexdigest(), lines


def read_file(path: Path, st: os.stat_result, limit: int) -> bytes:
    """A frozen segment's complete bytes, exactly as ``st`` describes them."""
    if st.st_size > limit:
        raise ValueError("a native journal segment exceeds its bound")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if stamp_of(os.fstat(fd)) != stamp_of(st):
            raise ValueError("a native journal segment changed while it was read")
        raw = os.pread(fd, limit + 1, 0)
        if len(raw) != st.st_size:
            raise ValueError("a native journal segment changed while it was read")
        return raw
    finally:
        os.close(fd)


def _first_line(path: Path, st: os.stat_result) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if stamp_of(os.fstat(fd)) != stamp_of(st):
            raise ValueError("a native journal segment changed while it was read")
        data = b""
        while b"\n" not in data:
            if len(data) > HEADER_MAX_BYTES:
                raise ValueError("a native journal segment header exceeds its bound")
            chunk = os.pread(fd, 64 * 1024, len(data))
            if not chunk:
                raise ValueError("a native journal segment has no complete header")
            data += chunk
        return data[: data.index(b"\n")]
    finally:
        os.close(fd)


def check_header(value: dict, index: int) -> dict:
    """The shape of segment ``index``'s header; its carried content is `native_journal`'s."""
    if type(value) is not dict or set(value) != {
        "type",
        "index",
        "base_revision",
        "prev",
        "carried",
    }:
        raise ValueError("invalid native journal segment header")
    prev = value["prev"]
    if (
        value["type"] != "segment"
        or type(value["index"]) is not int
        or value["index"] != index
        or index < 1
        or type(value["base_revision"]) is not int
        or not 1 <= value["base_revision"] <= 2**53 - 1
        or type(prev) is not dict
        or set(prev) != {"size", "sha256"}
        or type(prev["size"]) is not int
        or prev["size"] < 1
        or type(prev["sha256"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", prev["sha256"])
        or type(value["carried"]) is not dict
    ):
        raise ValueError("invalid native journal segment header")
    return value


def header(path: Path, st: os.stat_result, index: int) -> dict:
    """Segment n ≥ 1's header, read from its first line and shape-checked (memoized by stamp)."""
    key = (*stamp_of(st), index)
    hit = _memo(_HEADERS, key)
    if hit is not None:
        return hit
    return _memo(
        _HEADERS, key, check_header(decode(_first_line(path, st), HEADER_MAX_BYTES), index)
    )


@dataclass(frozen=True)
class Segment:
    index: int
    path: Path
    stat: os.stat_result
    header: dict | None  # segment n ≥ 1 only

    @property
    def stamp(self) -> tuple:
        return stamp_of(self.stat)

    @property
    def base(self) -> int:
        """The cursor just before this segment's first record (0 for segment 0)."""
        return 0 if self.header is None else self.header["base_revision"]


def last_revision(segment: Segment, lines: int) -> int:
    """The cursor of a segment's last record, from its line count: segment 0's line i has
    cursor i; segment n's header is line 1 and its record k (k ≥ 1) has cursor base + k."""
    return lines if segment.index == 0 else segment.base + lines - 1


def resolve(root: Path, session_id: str) -> list[Segment]:
    """Every segment of the chain, verified: contiguous from 0, each header naming its own
    index, each predecessor matching its successor's pin and revision. ValueError otherwise."""
    found = indexes(root, session_id)
    if found != list(range(1, len(found) + 1)):
        raise ValueError("a native journal segment is missing")
    chain = []
    for index in [0, *found]:
        path = segment_path(root, session_id, index)
        st = os.stat(path, follow_symlinks=False)
        if not private(st):
            raise ValueError("a native journal segment is not a private file")
        chain.append(Segment(index, path, st, header(path, st, index) if index else None))
    for previous, segment in zip(chain, chain[1:], strict=False):
        sha256, lines = identity(previous.path, previous.stat)
        pin = segment.header
        if pin["prev"] != {"size": previous.stat.st_size, "sha256": sha256} or pin[
            "base_revision"
        ] != last_revision(previous, lines):
            raise ValueError("a native journal segment no longer matches its successor's pin")
    return chain


def total_size(root: Path, session_id: str) -> int:
    """Bytes across every segment: the journal's monotonic, uncapped growth signal."""
    return sum(
        os.stat(segment_path(root, session_id, index), follow_symlinks=False).st_size
        for index in [0, *indexes(root, session_id, exhaustive=False)]
    )


def active_stamp(root: Path, session_id: str) -> tuple:
    """A cheap identity of the active segment (index + stat), with no lock: a wake hint only."""
    index = max([0, *indexes(root, session_id, exhaustive=False)])
    st = os.stat(segment_path(root, session_id, index), follow_symlinks=False)
    return (index, *stamp_of(st))


# --- locking ----------------------------------------------------------------------------------


class View:
    """The chain as one locked observation: the resolved segments and the active one's open fd.

    Valid only inside `locked`. ``st`` is the active descriptor's own fstat after locking."""

    def __init__(self, root: Path, session_id: str, exclusive: bool):
        self.root = root
        self.session_id = session_id
        self.exclusive = exclusive
        self.lock_fd: int | None = None
        self.fd: int | None = None
        self.chain: list[Segment] = []
        self.st: os.stat_result | None = None

    @property
    def active(self) -> Segment:
        return self.chain[-1]

    def _close_active(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def open_active(self) -> None:
        """(Re)resolve the chain and open its active segment, checking it is the resolved one."""
        self._close_active()
        self.chain = resolve(self.root, self.session_id)
        active = self.active
        if self.exclusive:
            healed = False
            if not active.stat.st_mode & 0o200:
                healed |= _thaw_interrupted(self.root, self.session_id, active)
            for frozen in self.chain[:-1]:
                if frozen.stat.st_mode & 0o222:
                    _refreeze(frozen)
                    healed = True
            if healed:  # modes moved ctimes: observe the chain again
                self.chain = resolve(self.root, self.session_id)
                active = self.active
        flags = os.O_RDWR | os.O_APPEND if self.exclusive else os.O_RDONLY
        fd = os.open(active.path, flags | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.fd = fd
        if active.index == 0:
            # A pre-chain release's worker locks only this file: exclude it the same way.
            fcntl.flock(fd, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        st = os.fstat(fd)
        if not private(st) or (st.st_dev, st.st_ino) != (active.stat.st_dev, active.stat.st_ino):
            raise ValueError("the active native journal segment changed while it was opened")
        self.st = st

    def close(self) -> None:
        self._close_active()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def _thaw_interrupted(root: Path, session_id: str, active: Segment) -> bool:
    """A read-only ACTIVE segment with a sidecar of its own is a rollover that froze it and then
    died before publishing its successor (`rollover` renames the sidecar, freezes, then publishes).
    Nothing follows it, so under the exclusive lock it becomes writable again. Without that proof
    a read-only active segment is left alone: writers then fail, as for any unwritable record."""
    if not os.path.lexists(sidecar_path(root, session_id, active.index)):
        return False
    fd = os.open(active.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (active.stat.st_dev, active.stat.st_ino):
            os.fchmod(fd, 0o600)
            return True
        return False
    finally:
        os.close(fd)


def _refreeze(segment: Segment) -> None:
    """A frozen segment that is writable again (a crash lost its freeze, or someone changed it)
    is made read-only under the exclusive lock, durably. Its bytes are what its successor pinned
    (`resolve` checked), so nothing is lost; it only stops anything appending behind the pin."""
    fd = os.open(segment.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == (segment.stat.st_dev, segment.stat.st_ino):
            os.fchmod(fd, 0o400)
            os.fsync(fd)
    finally:
        os.close(fd)


def _open_lock(root: Path, session_id: str, create: bool) -> int | None:
    path = lock_path(root, session_id)
    try:
        fd = os.open(
            path,
            (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as exc:
        if create and exc.errno not in (errno.ELOOP, errno.EACCES):
            raise Unwritable(
                cause_of(exc), "the session's lock file could not be created"
            ) from None
        raise
    try:
        st = os.fstat(fd)
        if not (
            stat.S_ISREG(st.st_mode)
            and st.st_uid == os.geteuid()
            and not st.st_mode & 0o077
            and st.st_nlink == 1
        ):
            raise ValueError("the native journal lock is not a private file")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def locked(root: Path, session_id: str, *, exclusive: bool):
    """Hold the session's stable lock and yield a `View` of its resolved, opened chain.

    A writer creates ``<id>.lock`` if needed (failing as `Unwritable` when it cannot). A reader
    never creates it: a journal that has no lock file has never rolled over, so locking its only
    segment is enough — and if a chain appears meanwhile, the reader retries under the lock."""
    _check_id(session_id)
    view = View(root, session_id, exclusive)
    try:
        for _ in range(3):
            view.close()
            view.lock_fd = _open_lock(root, session_id, create=exclusive)
            if view.lock_fd is not None:
                fcntl.flock(view.lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            view.open_active()
            if view.lock_fd is None and len(view.chain) > 1:
                continue  # rolled over between the probe and our lock: take the stable lock
            # Without the lock file, segment 0's own shared lock (taken in `open_active`) still
            # excludes every writer: one holds it exclusively while segment 0 is active.
            yield view
            return
        raise ValueError("the native journal kept changing while it was locked")
    finally:
        view.close()


# --- appends and rollover ---------------------------------------------------------------------


def append(fd: int, data: bytes, size: int) -> None:
    """Append ``data`` at ``size`` and fsync, all or nothing as far as the file can say.

    A failed write is truncated back to ``size`` under the exclusive lock (those bytes were never
    a record, and nothing acknowledged them), so a full disk refuses cleanly instead of leaving a
    torn tail. If even the truncation fails the tail stays, and the strict reader refuses it.
    A failed fsync AFTER a complete write keeps the bytes: they may already be durable, so the
    claim they hold stays occupied (#1278) — that is reported as unavailable, not a refusal."""
    view = memoryview(data)
    try:
        while view:
            written = os.write(fd, view)
            if not written:
                raise OSError(errno.EIO, "native journal append made no progress")
            view = view[written:]
    except OSError as exc:
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        except OSError:
            raise OSError(exc.errno, "append failed and could not be undone") from exc
        raise Unwritable(cause_of(exc), "the session's record could not be written") from None
    os.fsync(fd)


def _write_temporary(root: Path, session_id: str, data: bytes) -> Path:
    path = root / f".{session_id}-{uuid.uuid4().hex}.new"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if not written:
                raise OSError(errno.EIO, "temporary write made no progress")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return path


def sync_directory(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rollover(view: View, raw: bytes, successor: bytes, sidecar: bytes) -> None:
    """Freeze the active segment (whose complete bytes are ``raw``) behind ``successor``.

    Order: sidecar temp → fsync → successor temp → fsync → rename sidecar → FREEZE the active
    segment (0400) and fsync it → rename successor → directory fsync; then the view reopens and
    validates the
    new active segment. The predecessor is read-only before any successor is visible, so nothing
    — a worker of an older release included — can append behind its pin, whatever fails later.
    A failure before the successor is published thaws it again and leaves the chain as it was (an
    orphaned sidecar is ignored: it indexes a segment that is still active); a crash in that window
    is thawed by the next writer (`_thaw_interrupted`). `Unwritable` reports any I/O failure."""
    root, session_id = view.root, view.session_id
    index = view.active.index
    target = segment_path(root, session_id, index + 1)
    staged: list[Path] = []
    frozen = False
    try:
        try:
            staged.append(_write_temporary(root, session_id, sidecar))
            staged.append(_write_temporary(root, session_id, successor))
            os.rename(staged[0], sidecar_path(root, session_id, index))
            staged.pop(0)
            if os.path.lexists(target):
                raise ValueError("a native journal successor already exists")
            os.fchmod(view.fd, 0o400)
            frozen = True
            # The freeze is durable BEFORE the successor can be: a crash never keeps a published
            # successor behind a predecessor whose read-only mode was lost.
            os.fsync(view.fd)
            os.rename(staged[0], target)
            staged.pop(0)
        except OSError as exc:
            if frozen:
                with contextlib.suppress(OSError):
                    os.fchmod(view.fd, 0o600)  # not published: still the active segment
            raise Unwritable(cause_of(exc), "a new segment could not be written") from None
        # The successor is published: whatever happens next, it is the active segment, and its
        # predecessor is already read-only.
        try:
            sync_directory(root)
        except OSError as exc:
            raise Unwritable(cause_of(exc), "the new segment could not be made durable") from None
    finally:
        for path in staged:
            with contextlib.suppress(OSError):
                path.unlink()
    with contextlib.suppress(OSError):
        st = os.fstat(view.fd)
        if st.st_size == len(raw):
            remember_identity(st, hashlib.sha256(raw).hexdigest(), raw.count(b"\n"))
    view.open_active()
    if view.active.index != index + 1:
        raise ValueError("the new native journal segment did not become active")


def write_sidecar(root: Path, session_id: str, index: int, data: bytes) -> None:
    """Replace a rebuilt sidecar (best effort: it is a verifiable cache, never the authority)."""
    path = _write_temporary(root, session_id, data)
    try:
        os.rename(path, sidecar_path(root, session_id, index))
        sync_directory(root)
    finally:
        path.unlink(missing_ok=True)


def read_sidecar(root: Path, session_id: str, index: int) -> tuple[bytes, tuple] | None:
    """A sidecar's bytes and stamp, or None when it is absent or not a private file."""
    path = sidecar_path(root, session_id, index)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not private(st) or st.st_size > SIDECAR_MAX_BYTES:
            return None
        raw = os.pread(fd, SIDECAR_MAX_BYTES + 1, 0)
        if len(raw) != st.st_size:
            return None
        return raw, stamp_of(st)
    finally:
        os.close(fd)


def read_line(path: Path, offset: int, limit: int) -> bytes:
    """The complete LF-terminated line starting at ``offset`` (without its LF), which must be
    a line start. ValueError otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if offset and os.pread(fd, 1, offset - 1) != b"\n":
            raise ValueError("offset is not a record boundary")
        data = os.pread(fd, limit + 2, offset)
    finally:
        os.close(fd)
    end = data.find(b"\n")
    if end < 0:
        raise ValueError("no complete record at offset")
    return data[:end]
