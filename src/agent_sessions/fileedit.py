"""Editing an existing text file in place from the file viewer (#950 Phase 1).

The write half of :mod:`agent_sessions.files`, and a separate module for the same reason
:mod:`agent_sessions.filewrite` and :mod:`agent_sessions.gitwrite` are: the read path's guarantees
are narrower and simpler, and a write must not quietly borrow them. This module is a **security
and data-integrity boundary**; treat every change here as one.

The agent in the session's terminal writes to the same tree the operator is editing. So the
contract is not "write the operator's bytes" but:

    A save replaces exactly the version the operator was served, or it is refused and the file is
    left as the other writer left it. Bytes that the save cannot prove nobody else wrote are
    **retained**, never discarded.

Four mechanisms carry that, each argued where it is enforced:

**Editable is decided on the bytes as stored.** :func:`read_file` answers the viewer with the same
payload as :func:`files.read_file` plus ``version`` (sha256 of the complete bytes), ``editable``,
``readonly_reason``, ``eol`` and ``bom``. A file is editable only when the read was complete (an
editor over the first 1 MiB would save a truncated file), the bytes decode as **strict** UTF-8
(the display decode uses ``errors="replace"``, and a U+FFFD round trip corrupts the file), it uses
one line-ending style, it has exactly one name, the operator owns it, it is outside git metadata
(including a `.git` *file*, which is what points git at a linked worktree's or submodule's
repository) and the recovery store, it shares the recovery store's filesystem, and its filesystem
grants leases.

**"Is anyone else holding this file open" is the kernel's answer, not a scan.** A ``/proc`` scan
cannot settle it: measured on the author's host, 10 of the operator's 420 processes (``gpg-agent``,
``sshd``, ``sftp-server`` …) refuse ``/proc/<pid>/fd`` with ``EACCES``. A save therefore holds a
**write lease** (``F_SETLEASE F_WRLCK``) on the target for the whole replacement. The kernel refuses
the lease (``EAGAIN``) while any other process has the file open — a writer, a reader, or a
non-dumpable process alike — and while it is held, another ``open`` blocks and breaks it, which
:func:`_lease_intact` sees before the new bytes are installed. ``/proc`` is only used to *name* the
holder in a refusal, best-effort.

The lease is delivered with ``SIGIO``, whose default action terminates the process. So
:func:`install_lease_signal_handler` installs a **no-op Python handler** from the main thread at
startup — a handler, not ``SIG_IGN``, because an ignored disposition is inherited across ``exec``
by every agent this app launches, while a caught one is reset. Every lease acquisition first checks
the handler is still ours and refuses otherwise.

**The replacement never has a window in which bytes can vanish.** Six steps, each followed by the
``fsync`` calls that make it durable across a power loss (as far as the filesystem honours them):

1. *Intent* — a recovery record is written into a new entry in the store (record, entry, store).
2. *Stage* — the new bytes go to an ``O_EXCL`` temporary beside the target (temp, target dir),
   carrying the original's mode, group and access ACL; a group or ACL that cannot be kept is a
   refusal here, before anything is displaced.
3. *Displace* — one ``rename`` moves the current file **into the store**; at every instant its
   bytes are either at the name or in the store (entry dir, then target dir).
4. *Check* — the displaced file must be the leased inode, the lease unbroken, the bytes ``expect``.
   Otherwise whatever was displaced is put back — a regular file with ``link``, and the store keeps
   its own name for it (so the file reads as two names, read-only, until the operator deletes that
   copy); anything else (a directory or symlink swapped in meanwhile) with a no-clobber ``rename``
   — and the save is a 409.
5. *Install* — ``link`` the temporary into the name. ``link`` fails on an existing name, so a writer
   that claimed the name meanwhile is kept, not overwritten (target dir).
6. *Finish* — unlink the temporary and mark the record complete (target dir, record, entry).

A root-bound chat save pins its candidate from the creation descriptor, then checks its inode
and proposed hash under a separate write lease through installation and settlement. Installation
links that descriptor, so replacing a temporary path cannot substitute unapproved bytes. The
post-link inode, hash, lease, boundary and policy checks must all pass before settlement. Its
durable recovery-store hard link survives a parent rename and worker death. Recovery restores only
that app-created candidate's bytes under a write lease and
content comparison when its original parent cannot be found; it never overwrites the displaced
original. Refusal preserves a raced writer's bytes. A crash before settlement triggers rollback,
never inferred approval; bytes ambiguous after a recovery crash are retained for inspection.
The checks define settlement, not a filesystem lock against subsequent directory renames.

A record left incomplete by a crash is resolved by :func:`resolve_pending` the next time the store
is opened. Every resolution action checks what exists before acting, so a resolution that is itself
interrupted converges on the next run, and **no resolution removes a name displaced bytes have** —
not their last one, and not one an earlier ``link`` seemed to make redundant, because another
process can rename over that other name at any moment.

**The store is input, not state.** It lives under the operator's home, so a record is parsed as
untrusted data: every field is checked against exactly what :func:`_replace` writes, every
descriptor-relative operand is re-derived from ``name`` rather than trusted, and a record that does
not match is set aside unread (``.invalid-<id>``) — never acted on, and never able to fail another
save, however it fails to parse. No client write route may create anything under the store
(:func:`refuse_recovery_store`). Its directories are made durable in their parents before it is
relied on, and again on every use until that has once succeeded.

**Retained copies are kept until the operator deletes them.** Nothing in this module removes one.
Automatic pruning was built and then removed (Hermes on #955): its last step has to be a check that
nobody holds the copy open followed by an ``unlink``, and no check excludes an ``open`` that arrives
after it — a writer that opened in that gap wrote into an inode with no name left, and a removed
name cannot be given back (``linkat`` via ``/proc/self/fd`` is ``ENOENT``). The store grows with
every save; its size is the operator's to manage.

The boundary that remains, stated rather than implied: an ``open`` whose path lookup finished
before the displacement but which reaches the kernel's lease check only after release opens the
displaced inode, and its writes land in the retained copy. They are recoverable, not merged.

No shell, ever. No outbound network, ever.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from . import files, filewrite, renameat
from .files import FsError
from .fsbrowse import contained_path, home_root

#: Python only names these on some builds; the Linux values are stable ABI.
F_SETLEASE = getattr(fcntl, "F_SETLEASE", 1024)
F_GETLEASE = getattr(fcntl, "F_GETLEASE", 1025)

#: The largest file the editor will open for editing — the read path's own cap, on purpose: a file
#: the viewer could not read completely is a file the editor must not save.
MAX_EDIT_BYTES = files.FILES_MAX_READ
#: Bound on the request body. JSON escaping can grow text up to six-fold (a control character
#: becomes ``\\u00XX``); anything beyond that is not a 1 MiB file.
MAX_BODY_BYTES = 6 * MAX_EDIT_BYTES + 4096
#: How long a save may hold its lease. Far below the kernel's ``fs.lease-break-time`` (45 s by
#: default), so a process blocked on our lease waits for our answer, not for the kernel's timeout.
LEASE_BUDGET_S = 10.0
#: How long a save waits for another save (in this or another app process) to leave the store.
STORE_LOCK_WAIT_S = 5.0
#: Bound on the best-effort ``/proc`` walk that names a lease holder.
HOLDER_SCAN_BUDGET_S = 0.3

_BOM = b"\xef\xbb\xbf"
_EXPECT = re.compile(r"\A[0-9a-f]{64}\Z")


class SaveRefused(FsError):
    """A refusal that carries structured fields for the viewer (holder, version, retained copy)."""

    def __init__(self, message: str, *, status: int = 409, **fields: object) -> None:
        super().__init__(message, status=status)
        self.fields = fields


# --------------------------------------------------------------------------- test seams

#: Called with a step name after each step completes. Production never sets it; the kill-after-
#: every-step tests do, which is the only way to prove recovery from each intermediate state.
_HOOK: Callable[[str], None] | None = None


def _step(name: str) -> None:
    if _HOOK is not None:
        _HOOK(name)


def _fsync(fd: int, label: str) -> None:
    """One seam for every durability call, so a test can assert their ORDER."""
    os.fsync(fd)


# --------------------------------------------------------------------------- lease signal


def _on_lease_break(signum: int, frame: object) -> None:  # pragma: no cover - signal context
    """Deliberately nothing. The break is observed with ``F_GETLEASE``; this only replaces the
    default action, which would terminate the process."""


def install_lease_signal_handler() -> bool:
    """Install the no-op ``SIGIO`` handler. Must run on the main thread (``create_app`` does).

    Returns whether the handler is in place. When it is not, :func:`edit_capabilities` reports it
    and every file stays read-only — taking a lease without it could let a lease break kill the app.
    """
    if threading.current_thread() is not threading.main_thread():
        return _lease_signal_ok()
    try:
        signal.signal(signal.SIGIO, _on_lease_break)
    except (ValueError, OSError, AttributeError):
        return False
    return True


def _lease_signal_ok() -> bool:
    try:
        return signal.getsignal(signal.SIGIO) is _on_lease_break
    except (ValueError, AttributeError):
        return False


# --------------------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class EditCaps:
    ok: bool
    reason: str = ""


def _leases_enabled() -> bool:
    try:
        with open("/proc/sys/fs/leases-enable") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def edit_capabilities() -> EditCaps:
    """Not cached: the signal half can change after startup, and every other check is cheap."""
    base = files.capabilities()
    if not base.ok:
        return EditCaps(False, base.reason)
    missing = [
        name
        for name, fn in (("rename(dir_fd)", os.rename), ("link(dir_fd)", os.link))
        if fn not in os.supports_dir_fd
    ]
    if missing:
        return EditCaps(False, "editing is unsupported here: missing " + ", ".join(missing))
    if not _leases_enabled():
        return EditCaps(False, "editing is off: this system does not grant file leases")
    if not _lease_signal_ok():
        return EditCaps(False, "editing is off: lease notifications are not set up in this process")
    return EditCaps(True)


# --------------------------------------------------------------------------- the store


def recovery_dir() -> str:
    """Where displaced files are retained; ``AGENT_SESSIONS_EDIT_RECOVERY`` overrides it.

    **Resolved**, and that is load-bearing. Every gate compares it against a path the containment
    proof already resolved, and the retained path a save reports is built from it. Returning the
    configured spelling let a symlinked store miss the "is a retained copy" refusal — a retained
    original read as editable and a save replaced it (Hermes on #955). ``realpath`` of a store that
    does not exist yet resolves the prefix that does, so first use is unaffected.
    """
    raw = os.environ.get("AGENT_SESSIONS_EDIT_RECOVERY") or "~/.agent-sessions/edit-recovery"
    return os.path.realpath(os.path.expanduser(raw))


#: Written into the store once every directory on its path has been synced into its parent. Until
#: it exists, every use of the store redoes those syncs.
_STORE_MARKER = ".durable"


def _ensure_store() -> str:
    """Create the store, and make every directory on its path durable in its PARENT before use.

    ``makedirs`` alone left a first-use store whose own name was never synced: the store is the only
    other name displaced bytes have, so its entry in its parent has to survive a power loss before
    anything is displaced into it (Hermes on #955, confirmed with a recording probe).

    Syncing only what THIS call created was not enough either. When ``mkdir`` succeeded and the
    ``fsync`` after it failed, the retry found the directory, synced nothing, and used the store
    (Hermes on #955, review 4807, fault-injected). No call can know what an earlier one created, so
    until :data:`_STORE_MARKER` exists every use syncs each directory on the path that this user
    owns — every one a first use could have created, including one a concurrent process made — into
    its parent, top down, and only then writes the marker. The marker needs no ``fsync`` of its own:
    it is written only after those syncs returned, so losing it costs one redundant pass and can
    never skip one.
    """
    path = recovery_dir()
    with contextlib.suppress(OSError):
        os.stat(os.path.join(path, _STORE_MARKER), follow_symlinks=False)
        return path
    missing: list[str] = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(d, 0o700 if d == path else 0o777)
    # Never the operator's home itself: no first use creates it, and syncing it meant opening ITS
    # parent, which a user may not be able to read (`/home` at 0711) — every save would then fail.
    owned: list[str] = []
    cur, uid, home = path, os.geteuid(), os.path.realpath(os.path.expanduser("~"))
    while cur != home and os.stat(cur).st_uid == uid:
        owned.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(owned):
        pfd = os.open(os.path.dirname(d), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            _fsync(pfd, "store-parent")
        finally:
            os.close(pfd)
    os.close(
        os.open(
            os.path.join(path, _STORE_MARKER),
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    )
    return path


def _open_store() -> int:
    return os.open(_ensure_store(), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def refuse_recovery_store(target_dir: str, parts: list[str]) -> None:
    """Refuse a client write that would land in — or in place of — the editor's recovery store.

    Every record there is a journal the save path acts on. Reproduced on #955: an upload created
    ``recovery/<entry>/record.json`` with an absolute ``tmp``, and the next unrelated save deleted a
    file outside the root. Validation in :func:`_valid_record` is the second line, this the first,
    and it covers the store's directories before the first save has created them, and a file
    uploaded AT one of its ancestors (which would make the store impossible to create). Compared on
    resolved paths, so a symlinked store or target folder cannot slip past a prefix check.
    """
    dest = os.path.realpath(os.path.join(target_dir, *parts))
    store = os.path.realpath(recovery_dir())
    if _inside(dest, store) or _inside(store, dest):
        raise FsError("uploads may not write into the editor's recovery store", status=403)


@contextlib.contextmanager
def _store_locked() -> Iterator[int]:
    """Exclusive use of the store across threads AND app processes (``flock`` on ``.lock``)."""
    try:
        store_fd = _open_store()
    except OSError as e:
        raise FsError(f"the recovery store is unavailable: {e.strerror or e}", status=503) from None
    lock_fd = -1
    try:
        lock_fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600, dir_fd=store_fd)
        deadline = time.monotonic() + STORE_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise FsError("another save is still running; try again", status=503) from None
                time.sleep(0.02)
        yield store_fd
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)  # closing the description releases the flock
        os.close(store_fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _write_record(entry_fd: int, rec: dict) -> None:
    data = json.dumps(rec, sort_keys=True).encode("utf-8")
    with contextlib.suppress(FileNotFoundError):
        os.unlink("record.json.tmp", dir_fd=entry_fd)
    fd = os.open(
        "record.json.tmp",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=entry_fd,
    )
    try:
        _write_all(fd, data)
        _fsync(fd, "record")
    finally:
        os.close(fd)
    os.rename("record.json.tmp", "record.json", src_dir_fd=entry_fd, dst_dir_fd=entry_fd)
    _fsync(entry_fd, "entry")


#: A record :func:`_replace` writes is a few kilobytes at most; anything larger is not one.
_MAX_RECORD_BYTES = 64 * 1024


def _read_record(entry_fd: int) -> dict | None:
    """The entry's record. ``None`` when there is none to read right now — the entry is left as it
    is and looked at again next pass; ``{}``, which no validation accepts, when what is there is not
    a record.

    Opened non-blocking and required to be a regular file before any ``read``: a FIFO named
    ``record.json`` blocked the ``open`` for ever, under the store lock, and a directory made
    ``read`` raise. Nesting past the parser's recursion limit is malformed too, not an error.
    """
    try:
        fd = os.open(
            "record.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=entry_fd,
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        raw = b""
        while len(raw) <= _MAX_RECORD_BYTES and (chunk := os.read(fd, 65536)):
            raw += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > _MAX_RECORD_BYTES:
        return {}
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _retained_name(name: str) -> str:
    # Keep the original name (and so its extension) so a retained copy opens with the right
    # highlighting in the viewer. Bounded so the entry path stays well under NAME_MAX.
    return "previous-" + name[-200:]


def _tmp_name(name: str, entry_id: str) -> str:
    return f".{name[-180:]}.battlelab-save-{entry_id}"


_ENTRY_ID = re.compile(r"\A[0-9]{1,20}-[0-9a-f]{12}\Z")
#: Every state a record written by this module can be in.
_STATES = frozenset({"intent", "noop", "reverted", "kept_both", "complete", "orphaned"})


def _is_leaf(value: object) -> bool:
    """One directory entry name — what every descriptor-relative operand must be."""
    if not isinstance(value, str) or value in ("", ".", "..") or "/" in value or "\x00" in value:
        return False
    try:
        return len(os.fsencode(value)) <= 255
    except (UnicodeError, ValueError):
        return False


def _valid_record(entry_id: str, rec: dict) -> bool:
    """Is this exactly a record :func:`_replace` writes?

    A record is input: the store sits under the operator's home. So nothing it carries is used as
    written — ``tmp`` and ``retained`` must equal what ``name`` derives, ``name`` must be a single
    entry name, ``parent`` an absolute normalised path, ``target`` their join. An absolute ``tmp``
    would make ``unlink(tmp, dir_fd=…)`` ignore the descriptor entirely; ``../`` would walk out
    of it.
    """
    name, parent = rec.get("name"), rec.get("parent")
    if not _ENTRY_ID.match(entry_id) or rec.get("id") != entry_id:
        return False
    if not _is_leaf(name) or not isinstance(parent, str) or not os.path.isabs(parent):
        return False
    assert isinstance(name, str)
    if os.path.normpath(parent) != parent or rec.get("target") != os.path.join(parent, name):
        return False
    if rec.get("tmp") != _tmp_name(name, entry_id) or rec.get("retained") != _retained_name(name):
        return False
    if "guarded" in rec and rec["guarded"] is not True:
        return False
    for field in ("parent_inode", "candidate_inode"):
        if field in rec and (
            not isinstance(rec[field], list)
            or len(rec[field]) != 2
            or any(type(v) is not int or v < 0 for v in rec[field])
        ):
            return False
    if "rollback_started" in rec and rec["rollback_started"] is not True:
        return False
    if "candidate_inode" in rec and not rec.get("guarded"):
        return False
    if "rollback_started" in rec and "candidate_inode" not in rec:
        return False
    if rec.get("guarded") and "parent_inode" not in rec:
        return False
    for key in ("expect", "new_version"):
        v = rec.get(key)
        if not isinstance(v, str) or not _EXPECT.match(v):
            return False
    state = rec.get("state")
    # The type first: a list or dict is unhashable, and the membership test raised (Hermes on #955).
    if not isinstance(state, str) or state not in _STATES:
        return False
    for key in ("created", "settled"):
        v = rec.get(key)
        if v is None and key == "settled":
            continue
        if isinstance(v, bool) or not isinstance(v, int | float):
            return False
    return True


def _rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """``renameat2(RENAME_NOREPLACE)``: move an entry of any type, never replacing one.

    ``FileExistsError`` when the destination is claimed; ``OSError`` (``ENOSYS`` / ``EINVAL``) where
    the kernel or filesystem does not offer it — the caller then keeps the entry where it is.
    """
    renameat.renameat2(src_dir_fd, src, dst_dir_fd, dst, renameat.RENAME_NOREPLACE)


# --------------------------------------------------------------------------- small fs helpers


def _sha256_fd(fd: int, limit: int) -> tuple[str, bytes, bool]:
    """``(hex, bytes, truncated)`` of one descriptor read with ``pread`` — never moves a shared
    offset and never re-opens the path."""
    chunks: list[bytes] = []
    off = 0
    while off < limit:
        chunk = os.pread(fd, min(65536, limit - off), off)
        if not chunk:
            break
        chunks.append(chunk)
        off += len(chunk)
    truncated = bool(os.pread(fd, 1, off)) if off >= limit else False
    data = b"".join(chunks)
    return hashlib.sha256(data).hexdigest(), data, truncated


def _take_lease(fd: int) -> None:
    if not _lease_signal_ok():
        raise FsError(
            "editing is off: lease notifications are not set up in this process", status=501
        )
    fcntl.fcntl(fd, F_SETLEASE, fcntl.F_WRLCK)


def _release_lease(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, F_SETLEASE, fcntl.F_UNLCK)


def _lease_intact(fd: int) -> bool:
    """A broken (or breaking) lease reads back as something other than ``F_WRLCK``."""
    try:
        return fcntl.fcntl(fd, F_GETLEASE) == fcntl.F_WRLCK
    except OSError:
        return False


def _check_candidate(fd: int, dir_fd: int, name: str, rec: dict) -> None:
    try:
        st = os.fstat(fd)
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        digest, _, truncated = _sha256_fd(fd, MAX_EDIT_BYTES)
    except OSError:
        raise FsError("the staged edit cannot be verified; nothing was saved", status=409) from None
    if (
        not stat.S_ISREG(st.st_mode)
        or [st.st_dev, st.st_ino] != rec["candidate_inode"]
        or (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino)
        or not _lease_intact(fd)
        or truncated
        or digest != rec["new_version"]
    ):
        raise FsError("the staged edit changed; the approved bytes were not saved", status=409)


@contextlib.contextmanager
def _candidate_lease(dir_fd: int, tmp: str, rec: dict):
    if not rec.get("guarded"):
        yield None
        return
    try:
        fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        raise FsError("the staged edit cannot be opened; nothing was saved", status=409) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != rec["candidate_inode"]:
            raise FsError("the staged edit was replaced; nothing was saved", status=409)
        try:
            _take_lease(fd)
        except OSError:
            raise FsError(
                "the staged edit is open elsewhere; nothing was saved", status=409
            ) from None
        _check_candidate(fd, dir_fd, tmp, rec)
        yield fd
    finally:
        _release_lease(fd)
        os.close(fd)


def lease_holder(dev: int, ino: int) -> dict | None:
    """Best-effort name of a process holding ``(dev, ino)`` open. Decorates a refusal; never
    decides one — an unreadable process is simply not named."""
    deadline = time.monotonic() + HOLDER_SCAN_BUDGET_S
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        if time.monotonic() > deadline:
            return None
        if int(pid) == me:
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for n in fds:
            try:
                st = os.stat(f"/proc/{pid}/fd/{n}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (dev, ino):
                try:
                    with open(f"/proc/{pid}/comm") as f:
                        comm = f.read().strip()
                except OSError:
                    comm = ""
                return {"pid": int(pid), "comm": comm}
    return None


def _inside(child: str, parent: str) -> bool:
    parent = parent.rstrip(os.sep)
    return child == parent or child.startswith(parent + os.sep)


# --------------------------------------------------------------------------- editability


def _text_shape(data: bytes) -> tuple[str | None, str, bool]:
    """``(reason_or_None, eol, bom)`` for bytes that are already known complete and not binary."""
    bom = data.startswith(_BOM)
    body = data[len(_BOM) :] if bom else data
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        offset = e.start + (len(_BOM) if bom else 0)
        return f"is not valid UTF-8 (byte 0x{body[e.start]:02x} at offset {offset})", "\n", bom
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if lone_cr:
        return "uses bare CR line endings", "\n", bom
    if crlf and lf:
        return "mixes CRLF and LF line endings", "\n", bom
    return None, ("\r\n" if crlf else "\n"), bom


def _placement_refusal(verified: str, st: os.stat_result) -> str | None:
    """Reasons that depend on WHERE the file is and WHAT it is, not on its bytes."""
    if st.st_nlink != 1:
        return (
            f"has {st.st_nlink} names (hard links), and a save would split them — if a save of "
            "it was refused or interrupted, the other name is the original the editor's recovery "
            "store keeps, and deleting that copy makes the file editable again"
        )
    if st.st_uid != os.geteuid():
        return "is owned by another user"
    store = recovery_dir()
    if _inside(verified, store):
        return "is a retained copy in the editor's recovery store"
    # Spelled, and not only discovered. A linked worktree's or submodule's `.git` is a regular FILE
    # whose `gitdir:` line tells git where the repository is; it is not inside any gitdir, so the
    # discovery below never sees it (Hermes on #955: a real `git worktree` pointer was saveable).
    if ".git" in verified.split(os.sep):
        return "is git metadata — a `.git` entry tells git where a repository is"
    try:
        filewrite.refuse_git_metadata(os.path.dirname(verified), [os.path.basename(verified)])
    except FsError:
        return "is inside git metadata — code the agent's next git command runs"
    try:
        _ensure_store()
        if os.stat(store).st_dev != st.st_dev:
            return "is on a different filesystem from the editor's recovery store"
    except OSError as e:
        return f"cannot be retained: the recovery store is unavailable ({e.strerror or e})"
    return None


def _lease_probe(fd: int) -> str | None:
    """Can this file take a lease at all? ``EAGAIN`` (someone has it open right now) is a moment,
    not a property — the save will answer it. ``EINVAL`` / ``ENOLCK`` is the filesystem."""
    if not _lease_signal_ok():
        return None  # reported through edit_capabilities
    try:
        fcntl.fcntl(fd, F_SETLEASE, fcntl.F_WRLCK)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EBUSY):
            return None
        if e.errno in (errno.EINVAL, errno.ENOLCK, errno.EOPNOTSUPP):
            return "is on a filesystem that does not grant file leases"
        if e.errno in (errno.EACCES, errno.EPERM):
            return "is owned by another user"
        return f"cannot be leased ({e.strerror or e})"
    _release_lease(fd)
    return None


def read_file(path: str) -> dict:
    """:func:`files.read_file`'s payload plus what a save needs. Same containment proof."""
    files._refuse_if_symlink(path)
    resolved = contained_path(path)
    fd, st, verified = files._open_verified(resolved, directory=False)
    try:
        digest, data, truncated = _sha256_fd(fd, MAX_EDIT_BYTES)
        binary = b"\x00" in data[: files._BINARY_SNIFF_BYTES]
        lease_reason = None if (binary or truncated) else _lease_probe(fd)
    finally:
        os.close(fd)
    if binary:
        return {
            "path": verified,
            "size": st.st_size,
            "binary": True,
            "mime": files._guess_mime(verified),
            "editable": False,
            "readonly_reason": "is binary",
        }
    payload: dict = {
        "path": verified,
        "size": st.st_size,
        "binary": False,
        # The BOM is reported as `bom`, not rendered: left in, the editor would send U+FEFF back
        # as text and the save would restore a second BOM in front of it.
        "content": (data[len(_BOM) :] if data.startswith(_BOM) else data).decode(
            "utf-8", errors="replace"
        ),
        "truncated": truncated,
        "version": None if truncated else digest,
        "eol": "\n",
        "bom": data.startswith(_BOM),
    }
    reason: str | None
    caps = edit_capabilities()
    if not caps.ok:
        reason = caps.reason
    elif truncated:
        reason = (
            "is larger than 1 MiB — only the first 1 MiB was loaded, so a save would cut it short"
        )
    else:
        shape, eol, bom = _text_shape(data)
        payload["eol"], payload["bom"] = eol, bom
        reason = shape or _placement_refusal(verified, st) or lease_reason
    payload["editable"] = reason is None
    payload["readonly_reason"] = reason
    return payload


# --------------------------------------------------------------------------- save


def _encode(content: object, eol: str, bom: bool) -> bytes:
    if not isinstance(content, str):
        raise FsError("content must be text", status=422)
    if "\r" in content:
        raise FsError(
            "send line breaks as \\n; the file's own line endings are restored", status=422
        )
    try:
        body = content.replace("\n", eol).encode("utf-8")
    except UnicodeEncodeError:
        raise FsError("content is not valid Unicode text", status=422) from None
    data = (_BOM if bom else b"") + body
    if len(data) > MAX_EDIT_BYTES:
        raise FsError("the edited file would be larger than 1 MiB", status=422)
    return data


def _open_target(dir_fd: int, name: str) -> tuple[int, os.stat_result, str]:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FsError("symlinks are display-only", status=422) from None
        if e.errno == errno.ENOENT:
            raise FsError("the file no longer exists", status=404) from None
        if e.errno in (errno.EACCES, errno.EPERM):
            raise FsError("permission denied", status=403) from None
        raise FsError(f"could not open the file: {e.strerror or e}", status=400) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FsError("not a regular file", status=422)
        verified = files._fd_still_contained(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd, st, verified


def _budget(started: float) -> None:
    if time.monotonic() - started > LEASE_BUDGET_S:
        raise FsError("the save took too long and was abandoned; nothing was replaced", status=503)


_ACL_ACCESS = "system.posix_acl_access"


def _copy_access(src_fd: int, dst_fd: int, st: os.stat_result) -> None:
    """Give the temporary exactly the access the original had: mode, group and access ACL.

    ``fchmod`` alone is not that. A new file takes the creating process's group (or the directory's,
    under setgid) and inherits the parent's DEFAULT ACL, and neither follows the mode bits. Measured
    on #955: a restrictive ``user:nobody:---`` entry vanished, a file moved from its own group
    to the process group, and a parent default ACL added a named-user read grant the original never
    had. So the group is set explicitly and the access ACL is copied — or removed when the original
    has none. The group goes first because ``fchown`` clears set-id bits and ``fchmod`` restores
    them; the ACL goes last because it rewrites the group-class bits from its mask, which is the
    original's. Anything that cannot be kept refuses HERE, before anything is displaced.
    """
    try:
        if os.fstat(dst_fd).st_gid != st.st_gid:
            os.fchown(dst_fd, -1, st.st_gid)
    except PermissionError:
        raise SaveRefused(
            "this file's group could not be kept, so it was not saved — your edits are kept",
            reason="not_editable",
        ) from None
    os.fchmod(dst_fd, stat.S_IMODE(st.st_mode))
    try:
        try:
            acl: bytes | None = os.getxattr(src_fd, _ACL_ACCESS)
        except OSError as e:
            if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                return  # no POSIX ACLs on this filesystem: the mode bits are the whole story
            if e.errno != errno.ENODATA:
                raise
            acl = None
        if acl is not None:
            os.setxattr(dst_fd, _ACL_ACCESS, acl)
        else:
            try:
                os.removexattr(dst_fd, _ACL_ACCESS)
            except OSError as e:
                if e.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise
    except OSError as e:
        raise SaveRefused(
            f"this file's access list could not be kept ({e.strerror or e}), so it was not saved "
            "— your edits are kept",
            reason="not_editable",
        ) from None


def _put_back(entry_fd: int, retained: str, dir_fd: int, name: str) -> list[str] | None:
    """Return the displaced entry to its name. ``None`` on success; the two surviving names when
    the name was claimed meanwhile (both are kept, and the caller reports both).

    A regular file goes back by ``link``, and **the store keeps its own name for it**. That name is
    never removed — not by ``unlink``, not by renaming anything over it — because the restored name
    proves nothing: another process can rename over it at any moment, and one that opened the file
    before that goes on writing into the inode. The first version unlinked the store's name and
    lost the original (Hermes on #955, review 4807); the second renamed a copy over it, which kept
    the bytes of that moment and lost what an open writer added afterwards (review 4815, reproduced
    with a real writer at ``st_nlink == 0``). The cost is stated, not hidden: the file now has two
    names, so it opens read-only — :func:`_placement_refusal` says why — until the operator deletes
    the kept copy. :func:`_resolve_one` recognises a put-back that linked and then crashed.

    Anything else — a directory or symlink an agent put at the name during the save — goes back by
    a no-clobber ``rename``: ``link`` refuses a directory, and on a symlink it linked the symlink's
    TARGET into the worktree (Hermes on #955, both reproduced). Where that rename is unavailable
    this raises and the entry stays in the store with its record pending, for the next resolution
    to retry.
    """
    if not stat.S_ISREG(os.stat(retained, dir_fd=entry_fd, follow_symlinks=False).st_mode):
        try:
            _rename_noreplace(entry_fd, retained, dir_fd, name)
        except FileExistsError:
            return [name, retained]
        _fsync(dir_fd, "target")
        _fsync(entry_fd, "entry")
        return None
    try:
        os.link(retained, name, src_dir_fd=entry_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        return [name, retained]
    _fsync(dir_fd, "target")
    _step("put_back:linked")
    return None


def _second_name_note(entry_fd: int, retained: str, dir_fd: int, name: str, path: str) -> str:
    """What a refusal adds when its put-back left the store naming the file too (see _put_back)."""
    if not _same_file(entry_fd, retained, dir_fd, name):
        return ""
    return (
        f"; the recovery store still names it too ({path}), so it opens read-only until you "
        "delete that copy"
    )


def save(
    path: str,
    content: object,
    expect: object,
    *,
    root: str | None = None,
    admit: Callable[[str], None] | None = None,
) -> dict:
    """Replace the loaded version. Optional root/policy narrow the same save for chat proposals.

    The policy sees descriptor-proven paths, before any mutation and again before displacement
    and installation. A policy failure after displacement uses the ordinary recovery path.
    """
    caps = edit_capabilities()
    if not caps.ok:
        raise FsError(caps.reason, status=501)
    if not isinstance(expect, str) or not _EXPECT.match(expect):
        raise FsError("expect must be the version the file was loaded at", status=422)
    if not isinstance(content, str):
        raise FsError("content must be text", status=422)
    if not isinstance(path, str) or not path.strip():
        raise FsError("path is required", status=422)
    files._refuse_if_symlink(path)
    resolved = contained_path(path, root)
    parent, name = os.path.split(resolved)
    with _store_locked() as store_fd:
        resolve_pending(store_fd)
        dir_fd, _dst, verified_parent = files._open_verified(parent, directory=True, root=root)
        try:

            def guard() -> None:
                current_parent = files._fd_still_contained(dir_fd, root)
                if current_parent != verified_parent:
                    raise FsError("the parent directory moved during the save", status=409)
                if admit is not None:
                    admit(os.path.join(current_parent, name))

            result = _save_locked(
                store_fd,
                dir_fd,
                verified_parent,
                name,
                content,
                expect,
                root=root,
                guard=guard,
            )
        finally:
            os.close(dir_fd)
    with contextlib.suppress(Exception):
        from .gitpanel import discover_repo, invalidate_status

        repo = discover_repo(verified_parent)
        if repo is not None:
            invalidate_status(repo.toplevel)
    return result


def _save_locked(
    store_fd: int,
    dir_fd: int,
    parent: str,
    name: str,
    content: str,
    expect: str,
    *,
    root: str | None = None,
    guard: Callable[[], None] | None = None,
) -> dict:
    fd, st, verified = _open_target(dir_fd, name)
    try:
        files._fd_still_contained(fd, root)
        if guard is not None:
            guard()
        reason = _placement_refusal(verified, st)
        if reason:
            raise SaveRefused(f"this file {reason}", reason="not_editable")
        if root is not None and not st.st_mode & stat.S_IWUSR:
            # Ordinary editor replacement can replace a read-only inode. A guarded candidate
            # additionally needs owner write access for relocation rollback after process death.
            raise SaveRefused(
                "this file's owner permissions do not permit crash recovery of a chat edit",
                reason="not_editable",
            )
        try:
            _take_lease(fd)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EBUSY):
                raise SaveRefused(
                    "the file is open in another process, so it was not saved — "
                    "your edits are kept",
                    reason="open_elsewhere",
                    holder=lease_holder(st.st_dev, st.st_ino),
                ) from None
            raise FsError(f"this file cannot be leased ({e.strerror or e})", status=409) from None
        started = time.monotonic()
        try:
            return _replace(
                store_fd,
                dir_fd,
                fd,
                st,
                verified,
                parent,
                name,
                content,
                expect,
                started,
                guard=guard,
                guarded=root is not None,
            )
        finally:
            _release_lease(fd)
    finally:
        os.close(fd)


def _replace(
    store_fd: int,
    dir_fd: int,
    fd: int,
    st: os.stat_result,
    verified: str,
    parent: str,
    name: str,
    content: str,
    expect: str,
    started: float,
    *,
    guard: Callable[[], None] | None = None,
    guarded: bool = False,
) -> dict:
    current, data, truncated = _sha256_fd(fd, MAX_EDIT_BYTES)
    if truncated:
        raise SaveRefused("the file is now larger than 1 MiB and was not saved", reason="too_large")
    if current != expect:
        raise SaveRefused(
            "the file changed on disk while you were editing — nothing was saved and your "
            "edits are kept",
            reason="changed",
            version=current,
        )
    shape, eol, bom = _text_shape(data)
    if shape:
        raise SaveRefused(f"the file {shape} and was not saved", reason="not_editable")
    new = _encode(content, eol, bom)
    new_version = hashlib.sha256(new).hexdigest()
    if new_version == current:
        return {"path": verified, "version": current, "size": len(new), "retained": None}

    entry_id = f"{int(time.time())}-{secrets.token_hex(6)}"
    tmp = _tmp_name(name, entry_id)
    retained = _retained_name(name)
    store = recovery_dir()

    # 1. Intent.
    os.mkdir(entry_id, mode=0o700, dir_fd=store_fd)
    entry_fd = os.open(entry_id, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=store_fd)
    try:
        rec = {
            "id": entry_id,
            "target": verified,
            "parent": parent,
            "name": name,
            "tmp": tmp,
            "retained": retained,
            "expect": expect,
            "new_version": new_version,
            "created": time.time(),
            "state": "intent",
        }
        if guarded:
            parent_st = os.fstat(dir_fd)
            rec.update(guarded=True, parent_inode=[parent_st.st_dev, parent_st.st_ino])
        _write_record(entry_fd, rec)
        _fsync(store_fd, "store")
        _step("intent")

        # 2. Stage.
        _budget(started)
        tfd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            _copy_access(fd, tfd, st)
            _write_all(tfd, new)
            _fsync(tfd, "temp")
        except BaseException:
            os.close(tfd)
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            with contextlib.suppress(OSError):
                _settle(entry_fd, rec, "noop")  # nothing was displaced
            raise
        try:
            _fsync(dir_fd, "target")
            if guarded:
                # A pathname cannot locate a renamed parent after process death. Pin our own
                # tentative inode in the durable store BEFORE displacement, with hash-bound byte
                # snapshots for rollback. The original displaced inode is never overwritten.
                for leaf, payload in (("guarded-before", data), ("guarded-after", new)):
                    copy_fd = os.open(
                        leaf,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=entry_fd,
                    )
                    try:
                        _write_all(copy_fd, payload)
                        _fsync(copy_fd, "guarded-copy")
                    finally:
                        os.close(copy_fd)
                os.link(
                    f"/proc/self/fd/{tfd}",
                    "guarded-candidate",
                    dst_dir_fd=entry_fd,
                    follow_symlinks=True,
                )
                candidate = os.fstat(tfd)
                rec["candidate_inode"] = [candidate.st_dev, candidate.st_ino]
                _write_record(entry_fd, rec)
        finally:
            os.close(tfd)
        _step("stage")

        # 3. Displace.
        _budget(started)
        if guard is not None:
            try:
                guard()
            except BaseException:
                _discard_tmp(dir_fd, tmp)
                _settle(entry_fd, rec, "noop")
                raise
        try:
            os.rename(name, retained, src_dir_fd=dir_fd, dst_dir_fd=entry_fd)
        except FileNotFoundError:
            _discard_tmp(dir_fd, tmp)
            _settle(entry_fd, rec, "noop")
            raise SaveRefused(
                "the file was removed while it was being saved — nothing was written",
                reason="changed",
            ) from None
        _fsync(entry_fd, "entry")
        _fsync(dir_fd, "target")
        _step("displace")

        # 4. Check — and from here until install settles, any failure puts the file back.
        linked = False
        try:
            dst = os.stat(retained, dir_fd=entry_fd, follow_symlinks=False)
            same_inode = (dst.st_dev, dst.st_ino) == (st.st_dev, st.st_ino)
            intact = _lease_intact(fd)
            unchanged = same_inode and _sha256_fd(fd, MAX_EDIT_BYTES)[0] == expect
            if not (same_inode and intact and unchanged):
                try:
                    both = _put_back(entry_fd, retained, dir_fd, name)
                except OSError:
                    with contextlib.suppress(OSError):
                        _discard_tmp(dir_fd, tmp)
                    raise FsError(
                        "another process replaced the file with something that could not be moved "
                        "back yet — it is kept in the editor's recovery store, and nothing was "
                        "saved",
                        status=503,
                    ) from None
                _discard_tmp(dir_fd, tmp)
                _settle(entry_fd, rec, "kept_both" if both else "reverted")
                kept_path = os.path.join(store, entry_id, retained)
                raise SaveRefused(
                    (
                        "another process touched the file during the save — the original is back "
                        "in place and nothing was saved"
                        if same_inode
                        else "another process replaced the file during the save — what it put "
                        "there is back in place and nothing was saved"
                    )
                    + _second_name_note(entry_fd, retained, dir_fd, name, kept_path)
                    if not both
                    else "another process replaced the file during the save — both versions are "
                    "kept and nothing was overwritten",
                    reason="changed" if intact else "opened_during_save",
                    both=[os.path.join(parent, name), os.path.join(store, entry_id, retained)]
                    if both
                    else None,
                )
            _step("check")

            # 5. Install.
            _budget(started)
            if guard is not None:
                guard()
            if not _lease_intact(fd):
                both = _put_back(entry_fd, retained, dir_fd, name)
                _discard_tmp(dir_fd, tmp)
                _settle(entry_fd, rec, "kept_both" if both else "reverted")
                raise SaveRefused(
                    "another process opened the file during the save — the original is back in "
                    "place and nothing was saved"
                    + _second_name_note(
                        entry_fd, retained, dir_fd, name, os.path.join(store, entry_id, retained)
                    ),
                    reason="opened_during_save",
                )
            with _candidate_lease(dir_fd, tmp, rec) as candidate_fd:
                try:
                    if candidate_fd is None:
                        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    else:
                        os.link(
                            f"/proc/self/fd/{candidate_fd}",
                            name,
                            dst_dir_fd=dir_fd,
                            follow_symlinks=True,
                        )
                    linked = True
                except FileExistsError:
                    _discard_tmp(dir_fd, tmp)
                    _settle(entry_fd, rec, "kept_both")
                    raise SaveRefused(
                        "another process created the file during the save — its version was kept, "
                        "and the one you loaded is retained",
                        reason="changed",
                        both=[os.path.join(parent, name), os.path.join(store, entry_id, retained)],
                    ) from None
                _fsync(dir_fd, "target")
                _step("install")
                # linkat cannot lock the directory's placement. Installation remains tentative
                # until the descriptor/policy checks pass AFTER the link. A boundary change
                # withdraws the tentative name without deleting either the original or a raced
                # replacement. The intent record also makes a crash before settlement a rollback,
                # never inferred approval from finding the proposed bytes on disk.
                if guarded and guard is not None:
                    guard()
                if candidate_fd is not None:
                    _check_candidate(candidate_fd, dir_fd, name, rec)
                    _settle(entry_fd, rec, "complete")
        except SaveRefused:
            if guarded and linked and rec["state"] == "intent":
                _withdraw_unsettled(entry_fd, rec, dir_fd)
            raise
        except BaseException:
            if guarded and linked and rec["state"] == "intent":
                # Includes fsync/settlement errors, not just a refused policy check. If rollback
                # itself fails, keep its journal and temporary identity intact for recovery.
                _withdraw_unsettled(entry_fd, rec, dir_fd)
            # Anything unexpected between displacement and install: give the name back first.
            with contextlib.suppress(OSError):
                if not _name_exists(dir_fd, name):
                    _put_back(entry_fd, retained, dir_fd, name)
            with contextlib.suppress(OSError):
                _discard_tmp(dir_fd, tmp)
            raise

        # 6. Finish. The save has happened; a failure here leaves work for resolve_pending only.
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
            _fsync(dir_fd, "target")
            _settle(entry_fd, rec, "complete")
            if guarded:
                _discard_tmp(entry_fd, "guarded-candidate")
        _step("finish")
    finally:
        os.close(entry_fd)
    return {
        "path": verified,
        "version": new_version,
        "size": len(new),
        "retained": {"path": os.path.join(store, entry_id, retained), "version": expect},
    }


def _name_exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _discard_tmp(dir_fd: int, tmp: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp, dir_fd=dir_fd)
        _fsync(dir_fd, "target")


def _settle(entry_fd: int, rec: dict, state: str) -> None:
    rec["state"] = state
    rec["settled"] = time.time()
    _write_record(entry_fd, rec)


def _withdraw_unsettled(entry_fd: int, rec: dict, dir_fd: int) -> None:
    """Recover a guarded install without a check-then-unlink of somebody else's name.

    Move the tentative destination into a new retained name, then identify what actually moved.
    If another writer won that race, restore its entry instead. All displaced regular files
    keep a recovery-store name; no path or bytes from the record select the quarantine name.
    """
    name, tmp, retained = rec["name"], rec["tmp"], rec["retained"]
    identity_fd, identity = (
        (entry_fd, "guarded-candidate") if "candidate_inode" in rec else (dir_fd, tmp)
    )
    unsettled = "unsettled-" + name[-200:]
    if _same_file(identity_fd, identity, dir_fd, name) and not _name_exists(entry_fd, unsettled):
        _rename_noreplace(dir_fd, name, entry_fd, unsettled)
        _fsync(entry_fd, "entry")
        _fsync(dir_fd, "target")
        _step("withdraw:displaced")
    if _name_exists(entry_fd, unsettled):
        chosen = retained if _same_file(identity_fd, identity, entry_fd, unsettled) else unsettled
        _put_back(entry_fd, chosen, dir_fd, name)
    elif not _name_exists(dir_fd, name):
        _put_back(entry_fd, retained, dir_fd, name)
    _settle(
        entry_fd, rec, "reverted" if _same_file(entry_fd, retained, dir_fd, name) else "kept_both"
    )
    _discard_tmp(dir_fd, tmp)


# --------------------------------------------------------------------------- recovery


def resolve_pending(store_fd: int) -> None:
    """Resolve every incomplete record. Caller holds the store lock. Idempotent: every action checks
    the state it acts on first. Nothing here removes a retained copy (see the module doc)."""
    try:
        names = sorted(os.listdir(store_fd))
    except OSError:
        return
    for entry_id in names:
        if entry_id.startswith("."):
            continue
        try:
            entry_fd = os.open(
                entry_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=store_fd,
            )
        except OSError:
            continue
        try:
            # This runs at the start of every save, so one entry must never fail another save —
            # whether its record cannot be read, does not parse, does not validate, or cannot be
            # resolved right now (a put-back the filesystem refuses, a parent that is unreadable).
            # Reading and validating are inside this boundary too: `state: []` raised in validation
            # while only resolution was (Hermes on #955, review 4807).
            try:
                rec = _read_record(entry_fd)
                if rec is None:
                    continue  # unreadable right now: left exactly as it is, and retried
                if not _valid_record(entry_id, rec):
                    _set_aside(store_fd, entry_id)
                    continue
                if rec["state"] == "intent":
                    _resolve_one(entry_fd, rec)
                elif rec.get("guarded") and rec["state"] == "complete":
                    _discard_tmp(entry_fd, "guarded-candidate")
            except Exception:  # noqa: S112 - isolation is the point; the record stays as it was
                continue
        finally:
            os.close(entry_fd)


def _set_aside(store_fd: int, entry_id: str) -> None:
    """Move an entry whose record is not one this module wrote out of the resolver's way, unread."""
    with contextlib.suppress(OSError):
        _rename_noreplace(store_fd, entry_id, store_fd, f".invalid-{entry_id}")
        _fsync(store_fd, "store")


def _resolve_one(entry_fd: int, rec: dict) -> None:
    parent, name, tmp, retained = rec["parent"], rec["name"], rec["tmp"], rec["retained"]
    try:
        dir_fd, _st, _v = files._open_verified(parent, directory=True)
    except FsError:
        if rec.get("guarded") and "candidate_inode" in rec:
            _restore_relocated_candidate(entry_fd, rec)
            return
        _settle(entry_fd, rec, "orphaned")  # the directory is gone; the retained copy stays
        return
    try:
        if rec.get("guarded"):
            parent_st = os.fstat(dir_fd)
            if [parent_st.st_dev, parent_st.st_ino] != rec["parent_inode"]:
                if "candidate_inode" in rec:
                    _restore_relocated_candidate(entry_fd, rec)
                else:
                    _settle(entry_fd, rec, "orphaned")
                return
            if _name_exists(entry_fd, retained):
                _withdraw_unsettled(entry_fd, rec, dir_fd)
            else:
                _discard_tmp(dir_fd, tmp)
                _settle(entry_fd, rec, "noop")
            return
        have_tmp = _name_exists(dir_fd, tmp)
        have_name = _name_exists(dir_fd, name)
        have_retained = _name_exists(entry_fd, retained)
        installed = have_tmp and have_name and _same_file(dir_fd, tmp, dir_fd, name)
        if have_tmp:
            _discard_tmp(dir_fd, tmp)
            _step("resolve:tmp")
        if not have_retained:
            # Nothing was displaced (a crash before step 3), or a put-back already finished.
            _settle(entry_fd, rec, "noop")
            return
        if not have_name:
            _put_back(entry_fd, retained, dir_fd, name)
            _settle(entry_fd, rec, "reverted")
            return
        if _same_file(entry_fd, retained, dir_fd, name):
            # A put-back that linked the name back — which is all a put-back does, so it is done.
            # The store's name is kept: the restored name may have been renamed over since the
            # comparison above, and a writer may hold the inode (Hermes on #955, reviews 4807 and
            # 4815).
            _settle(entry_fd, rec, "reverted")
            return
        if installed or _file_version(dir_fd, name) == rec.get("new_version"):
            _settle(entry_fd, rec, "complete")
        else:
            _settle(entry_fd, rec, "kept_both")
    finally:
        os.close(dir_fd)


def _restore_relocated_candidate(entry_fd: int, rec: dict) -> None:
    """Retract proposed bytes through our durable inode pin when its parent cannot be found.

    This restores only the app-created candidate, under a kernel write lease and a content CAS.
    The displaced original and a different writer's inode are never overwritten. An open file
    leaves the intent pending; modified bytes are retained as kept_both. Snapshots and the pin
    remain named, so a crash during this recovery can itself be retried without a path search.
    """
    snapshots = []
    for leaf, digest in (("guarded-before", rec["expect"]), ("guarded-after", rec["new_version"])):
        source = os.open(
            leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=entry_fd
        )
        try:
            st = os.fstat(source)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise OSError("invalid rollback snapshot")
            actual, data, truncated = _sha256_fd(source, MAX_EDIT_BYTES)
            if truncated or actual != digest:
                raise OSError("rollback snapshot changed")
            snapshots.append(data)
        finally:
            os.close(source)
    before, after = snapshots
    candidate = os.open(
        "guarded-candidate",
        os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=entry_fd,
    )
    try:
        st = os.fstat(candidate)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or [st.st_dev, st.st_ino] != rec["candidate_inode"]
        ):
            raise OSError("rollback candidate changed")
        _take_lease(candidate)
        try:
            started = time.monotonic()
            _, current, truncated = _sha256_fd(candidate, MAX_EDIT_BYTES)
            # A lease cannot prove ownership across a process death. Even a byte-exact prefix
            # of our restoration may be another writer's edit between recovery attempts.
            if truncated or current not in (before, after):
                _settle(entry_fd, rec, "kept_both")
                return
            if current != before:
                rec["rollback_started"] = True
                _write_record(entry_fd, rec)
                offset = 0
                while offset < len(before):
                    _budget(started)
                    if not _lease_intact(candidate):
                        raise OSError("rollback lease was broken")
                    written = os.pwrite(candidate, before[offset : offset + 65536], offset)
                    if written <= 0:
                        raise OSError("rollback write made no progress")
                    offset += written
                    _step("relocated:chunk")
                _fsync(candidate, "candidate")
                _step("relocated:written")
                if not _lease_intact(candidate):
                    raise OSError("rollback lease was broken")
                os.ftruncate(candidate, len(before))
                _fsync(candidate, "candidate")
                _step("relocated:truncated")
            _settle(entry_fd, rec, "reverted")
        finally:
            _release_lease(candidate)
    finally:
        os.close(candidate)


def _same_file(a_fd: int, a: str, b_fd: int, b: str) -> bool:
    try:
        sa = os.stat(a, dir_fd=a_fd, follow_symlinks=False)
        sb = os.stat(b, dir_fd=b_fd, follow_symlinks=False)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _file_version(dir_fd: int, name: str) -> str | None:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError:
        return None
    try:
        return _sha256_fd(fd, MAX_EDIT_BYTES + 1)[0]
    finally:
        os.close(fd)


def startup_resolve() -> None:
    """Resolve crash leftovers when the app starts. Best-effort: a store that cannot be opened is
    reported by the next save instead."""
    with contextlib.suppress(FsError, OSError), _store_locked() as store_fd:
        resolve_pending(store_fd)


__all__ = [
    "EditCaps",
    "LEASE_BUDGET_S",
    "MAX_BODY_BYTES",
    "MAX_EDIT_BYTES",
    "SaveRefused",
    "edit_capabilities",
    "home_root",
    "install_lease_signal_handler",
    "lease_holder",
    "read_file",
    "recovery_dir",
    "refuse_recovery_store",
    "resolve_pending",
    "save",
    "startup_resolve",
]
