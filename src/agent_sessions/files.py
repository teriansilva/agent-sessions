"""Bounded, read-only filesystem access for the session file panel (#783).

This module is a **security boundary**; treat every change here as one. It reads directory
listings and file bytes under ``$HOME`` and hands them to the SPA, which is strictly more than
:mod:`agent_sessions.fsbrowse` (the folder picker) ever did — that one only listed directory
*names*. Three properties are load-bearing and each exists because the obvious version is wrong:

**Open-then-verify, not check-then-open.** ``fsbrowse.contained_path()`` realpath-resolves and
rejects escapes, but ``realpath()`` followed by a later ``open()`` is check-then-use: a component
can be repointed in between. So containment is proved on the **descriptor**: acquire, ``fstat``
for the type, re-read ``/proc/self/fd/<n>`` to confirm the thing actually opened is still inside
the root, then read only *through that fd*. A listing walks with ``scandir(fd)`` /
``stat(dir_fd=fd)`` so the directory can't be swapped mid-walk. (The residual threat is a
same-UID racer — i.e. the agent this panel is displaying, which can already read whatever the app
can. Winning the race buys it nothing. This is implemented because it's cheap, not because it
contains a peer process.)

**Acquisition must be non-blocking.** ``os.open(p, O_RDONLY | O_NOFOLLOW)`` on a FIFO with no
writer blocks *inside open()* — the ``fstat`` that would reject it never runs, so "special files
are refused before any read" is false without ``O_NONBLOCK``. Measured, not assumed.

**Abandoned work is bounded.** ``asyncio.to_thread`` cannot be cancelled: cancelling the await
abandons the coroutine, never the thread. So every worker runs under a semaphore budget and every
loop carries a cooperative deadline. A cancelled request may still leave *one* bounded worker
finishing; it cannot leave a growing pool.

No shell, ever. No writes, ever.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .fsbrowse import FsError, contained_path, home_root

# Response/work bounds. The entry cap bounds the *response*; the scan budget bounds the *I/O*,
# which is the part a cap alone never did (an exact `total` implies a completed scan).
FILES_MAX_ENTRIES = 2000
FILES_SCAN_BUDGET_MS = 1500
FILES_MAX_READ = 1024 * 1024  # 1 MiB
_BINARY_SNIFF_BYTES = 8192
_READ_CHUNK = 64 * 1024

# Worker budget. Global cap plus a per-root cap so one pathological directory cannot starve
# every other session's panel. `to_thread` can't be cancelled, so this is the containment.
FILES_MAX_WORKERS = 8
FILES_MAX_WORKERS_PER_ROOT = 3


class FilesBusy(FsError):
    """Worker budget saturated. 503 — an honest 'try again', never a silent queue."""

    def __init__(self, message: str = "the file panel is busy; try again") -> None:
        super().__init__(message, status=503)


# --------------------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class Capabilities:
    """What the platform must provide for the containment contract to hold."""

    ok: bool
    reason: str = ""


_CAPS: Capabilities | None = None


def _detect_capabilities() -> Capabilities:
    missing: list[str] = []
    for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY", "O_CLOEXEC"):
        if not hasattr(os, flag):
            missing.append(flag)
    if os.scandir not in os.supports_fd:
        missing.append("scandir(dir_fd)")
    if os.stat not in os.supports_dir_fd:
        missing.append("stat(dir_fd)")
    if not os.path.isdir("/proc/self/fd"):
        missing.append("/proc/self/fd")
    if missing:
        return Capabilities(False, "unsupported platform: missing " + ", ".join(missing))
    return Capabilities(True)


def capabilities() -> Capabilities:
    """Detected once. **Fails closed**: without the full set the panel is disabled with a stated
    reason rather than quietly downgrading to a weaker check (a device+inode comparison is not an
    equivalent substitute, so none is offered)."""
    global _CAPS
    if _CAPS is None:
        _CAPS = _detect_capabilities()
    return _CAPS


def reset_capabilities_for_test() -> None:
    global _CAPS
    _CAPS = None


def _require_capabilities() -> None:
    caps = capabilities()
    if not caps.ok:
        raise FsError(caps.reason, status=501)


# --------------------------------------------------------------------------- worker budget

_budget_lock = threading.Lock()
_inflight_total = 0
_inflight_by_root: dict[str, int] = {}


_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def executor() -> ThreadPoolExecutor:
    """A **dedicated** pool, sized to exactly :data:`FILES_MAX_WORKERS`.

    The admission counter is only an honest bound if the budget and the thread pool are the same
    thing. ``asyncio.to_thread`` dispatches to ``run_in_executor(None, …)`` — the interpreter's
    default pool: up to ``min(32, cpu+4)`` threads, an unbounded queue, and shared with every
    other blocking call in the process. Against that pool the counter here bounded *admission*
    while execution was governed by something else entirely, so a flood could still put far more
    scans on threads than the budget names, and an unrelated flood could starve the panel without
    ever raising :class:`FilesBusy`.

    With this pool the two coincide: admission caps in-flight work at N, the pool has exactly N
    threads, so nothing ever queues behind them and "refuses rather than queues" is finally a
    property of the system rather than of one counter.
    """
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=FILES_MAX_WORKERS, thread_name_prefix="files"
            )
        return _EXECUTOR


def shutdown_executor_for_test() -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = None


def fairness_key(raw: str | None) -> str:
    """The per-root admission key, canonicalised **without touching the filesystem.**

    Keying on the raw request path let lexical aliases for one directory each take their own
    counter: ``root``, ``root/.``, ``root//`` and ``root/sub/..`` were admitted as four distinct
    roots, so one pathological directory could occupy the whole pool and hand every unrelated root
    a 503 — ``FILES_MAX_WORKERS_PER_ROOT`` stopped meaning anything.

    ``normpath`` + ``expanduser`` collapse all of those purely as string operations, which is what
    makes this safe to call on the event loop. What it deliberately does **not** do is resolve
    symlinks: that is `realpath`, i.e. filesystem work, and doing it here would put I/O back on the
    loop. So two *symlinked* spellings of one directory still get separate counters. The global cap
    remains the hard bound; this is fairness, and its limits are stated rather than implied.
    """
    # An omitted/empty path resolves to the configured root, so it must map to the SAME key as an
    # explicit request for that root — otherwise the two spellings each take a full per-root
    # allotment and one canonical directory gets 2N workers.
    #
    # Deliberately the root's LEXICAL identity, not `home_root()`: that one calls `realpath`,
    # which is filesystem I/O and must not run on the event loop. The residual is the same one
    # documented above — a root reached through a symlink still keys differently.
    raw = (raw or "").strip() or (os.environ.get("AGENT_SESSIONS_FS_ROOT") or "~")
    return os.path.normpath(os.path.expanduser(raw))


def acquire(key: str) -> None:
    """Take a slot or raise :class:`FilesBusy`. Called by the ROUTE, **before** submission.

    Admission has to happen above the executor: submitting first would bound what *runs* while a
    flood piles up in the queue behind it.

    ``key`` should come from :func:`fairness_key`, not from a raw request path.
    """
    global _inflight_total
    with _budget_lock:
        if _inflight_total >= FILES_MAX_WORKERS:
            raise FilesBusy()
        if _inflight_by_root.get(key, 0) >= FILES_MAX_WORKERS_PER_ROOT:
            raise FilesBusy()
        _inflight_total += 1
        _inflight_by_root[key] = _inflight_by_root.get(key, 0) + 1


def release(key: str) -> None:
    """Give the slot back. Owned by the WORKER — see :func:`guarded`."""
    global _inflight_total
    with _budget_lock:
        if _inflight_total > 0:
            _inflight_total -= 1
        n = _inflight_by_root.get(key, 1) - 1
        if n <= 0:
            _inflight_by_root.pop(key, None)
        else:
            _inflight_by_root[key] = n


class Slot:
    """One admitted request's slot, releasable **exactly once** by whoever ends up owning it.

    Ownership is genuinely ambiguous and that is the whole difficulty:

    * If the worker starts, the worker owns it — releasing from the request task would hand the
      slot back while the thread is still running (measured: occupancy 1 → 0 with the worker
      blocked, then eight further admissions).
    * If the request is cancelled *before* the worker starts, the queued callable is dropped and
      the worker never runs — so nothing in the worker can ever release, and the slot leaks
      permanently (measured: `prestart_cancel (1, {'queued-key': 1})`, still held after the pool
      drained; repeat it eight times and the panel is dead until restart).

    Neither owner can be chosen up front, so both are allowed to try and the lock makes the
    second attempt a no-op.
    """

    __slots__ = ("key", "_lock", "_done")

    def __init__(self, key: str) -> None:
        self.key = key
        self._lock = threading.Lock()
        self._done = False

    def release(self) -> bool:
        """Release if nobody has yet. Returns True if THIS call did it."""
        with self._lock:
            if self._done:
                return False
            self._done = True
        release(self.key)
        return True


def acquire_slot(key: str) -> Slot:
    """Admit and hand back the releasable token. Raises :class:`FilesBusy` when saturated.

    ``key`` is canonicalised here so no caller can accidentally reintroduce alias-splitting.
    """
    canon = fairness_key(key)
    acquire(canon)
    return Slot(canon)


def run_slot(slot: Slot, fn, *args):
    """Worker entry point: run ``fn`` and release the slot when the callable actually exits."""
    try:
        return fn(*args)
    finally:
        slot.release()


def guarded(key: str, fn, *args):
    """Back-compat shim over :func:`run_slot` for callers holding a bare key."""
    return run_slot(Slot(key), fn, *args)


def inflight_workers() -> tuple[int, dict[str, int]]:
    """(total, per-root) — for the concurrency tests."""
    with _budget_lock:
        return _inflight_total, dict(_inflight_by_root)


# --------------------------------------------------------------------------- descriptor gate


def _fd_still_contained(fd: int) -> str:
    """Absolute path the descriptor *actually* refers to, verified against the root.

    This is the proof the pre-check isn't. ``/proc/self/fd/<n>`` resolves what was really opened,
    so a component repointed between the ``realpath`` and the ``open`` is caught here rather than
    slipping through with the pre-check's blessing.
    """
    try:
        real = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as e:
        raise FsError(f"could not verify the open file: {e}", status=400) from None
    root = home_root()
    # A deleted file reads back as "<path> (deleted)"; treat it as gone rather than parsing it.
    if real.endswith(" (deleted)"):
        raise FsError("the file disappeared while it was being read", status=404)
    if real != root and not real.startswith(root + os.sep):
        raise FsError("path escapes the home root", status=403)
    return real


def _refuse_if_symlink(raw: str | None) -> None:
    """Refuse a *final component* that is itself a symlink — phase 1 is display-only.

    This has to be checked on the RAW path, before ``contained_path``: ``realpath`` resolves the
    whole chain, so by the time we hold the resolved path the link is already gone and
    ``O_NOFOLLOW`` would never see it. (Caught by a test that asserted the opposite and failed —
    the pre-check was quietly following exactly what the contract says it must not.) An
    *intermediate* symlink is fine: realpath collapses it and containment still judges the result.
    """
    if not raw or not raw.strip():
        return
    try:
        if stat.S_ISLNK(os.lstat(os.path.expanduser(raw)).st_mode):
            raise FsError("symlinks are display-only", status=422)
    except OSError:
        return  # missing / unreadable: let the open below produce the real error


def _open_verified(path: str, *, directory: bool) -> tuple[int, os.stat_result, str]:
    """Acquire a descriptor and prove it. Returns ``(fd, fstat, verified_abspath)``.

    ``O_NONBLOCK`` is load-bearing: without it a FIFO with no writer blocks in ``open()`` and the
    type check below never runs. ``O_NOFOLLOW`` refuses a symlinked final component at acquisition
    time — which is also why phase 1 treats symlinks as display-only rather than pretending it can
    safely follow the contained ones.
    """
    _require_capabilities()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            # O_NOFOLLOW on a symlink. Phase 1 never follows one.
            raise FsError("symlinks are display-only", status=422) from None
        if e.errno == errno.ENOTDIR:
            raise FsError("not a directory", status=404 if directory else 422) from None
        if e.errno in (errno.ENXIO, errno.EOPNOTSUPP):
            # A socket, or a device with no driver behind it. A kind we refuse, not an OS fault.
            raise FsError("not a regular file", status=422) from None
        if e.errno == errno.ENOENT:
            raise FsError("no such file or directory", status=404) from None
        if e.errno in (errno.EACCES, errno.EPERM):
            raise FsError("permission denied", status=403) from None
        raise FsError(f"could not open the path: {e.strerror or e}", status=400) from None
    try:
        st = os.fstat(fd)
        if directory:
            if not stat.S_ISDIR(st.st_mode):
                raise FsError("not a directory", status=404)
        elif not stat.S_ISREG(st.st_mode):
            # FIFOs, sockets, devices, directories. Refused on the descriptor, before any read —
            # which only works because acquisition was non-blocking.
            raise FsError("not a regular file", status=422)
        verified = _fd_still_contained(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd, st, verified


# --------------------------------------------------------------------------- listing


def _json_safe(name: str) -> bool:
    """Can this filename survive the JSON encoder? Undecodable bytes arrive as lone surrogates."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _entry_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file"


def _link_fields(dir_fd: int, name: str, base: str) -> dict:
    """Display-only link metadata. The shape is pinned by #783 so the backend and the tree types
    cannot drift: ``link_target`` always, ``link_contained`` always, ``link_kind`` **only** when
    contained (an uncontained target's kind is not something we resolve, let alone report)."""
    out: dict = {"link_target": None, "link_contained": False}
    try:
        target = os.readlink(name, dir_fd=dir_fd)
    except OSError:
        return out
    if not _json_safe(target):
        # The link is real and its name is fine, but the TARGET is not representable. Report the
        # link with a null target rather than letting one entry take down the listing (and, via
        # an unhandled 500, drop the no-store headers with it).
        out["link_unencodable_target"] = True
        return out
    out["link_target"] = target
    resolved = os.path.realpath(target if os.path.isabs(target) else os.path.join(base, target))
    root = home_root()
    if resolved == root or resolved.startswith(root + os.sep):
        out["link_contained"] = True
        try:
            out["link_kind"] = "dir" if stat.S_ISDIR(os.stat(resolved).st_mode) else "file"
        except OSError:
            out["link_kind"] = None
    return out


def list_dir(path: str | None) -> dict:
    """One directory, bounded by an entry cap **and** a wall-clock budget.

    ``total`` is an integer **iff** ``complete`` is true. A scan stopped by either budget reports
    ``complete: false`` / ``total: null`` / ``truncated: true`` rather than a count it never
    finished — and the dirs-first ordering then describes only *the entries actually returned*,
    since the rest were never seen.
    """
    _refuse_if_symlink(path)
    base = contained_path(path or "")
    fd, _st, verified = _open_verified(base, directory=True)
    try:
        entries: list[dict] = []
        unencodable = 0
        complete = True
        deadline = time.monotonic() + (FILES_SCAN_BUDGET_MS / 1000.0)
        with os.scandir(fd) as it:
            for de in it:
                if len(entries) >= FILES_MAX_ENTRIES or time.monotonic() > deadline:
                    complete = False
                    break
                if not _json_safe(de.name):
                    # A POSIX filename is bytes, not text: os.scandir surfaces undecodable ones
                    # with lone surrogates, which JSONResponse cannot encode. One such file used
                    # to take down the entire listing, so they are counted out rather than
                    # allowed to brick the directory — and the count is reported, not swallowed.
                    unencodable += 1
                    continue
                try:
                    st = os.stat(de.name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue  # vanished mid-walk; skip rather than fail the whole listing
                kind = _entry_kind(st.st_mode)
                row: dict = {
                    "name": de.name,
                    "path": os.path.join(verified, de.name),
                    "kind": kind,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
                if kind == "link":
                    row.update(_link_fields(fd, de.name, verified))
                entries.append(row)
    finally:
        os.close(fd)

    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    root = home_root()
    parent = os.path.dirname(verified) if verified != root else None
    return {
        "path": verified,
        "parent": parent,
        "root": root,
        "entries": entries,
        "total": len(entries) if complete else None,
        "complete": complete,
        "truncated": not complete,
        # Honest about what was left out, rather than pretending the directory is smaller.
        "unencodable": unencodable,
    }


# --------------------------------------------------------------------------- read


def _guess_mime(path: str) -> str:
    import mimetypes

    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def read_file_bytes(path: str, *, limit: int = FILES_MAX_READ) -> tuple[str, bytes, int, bool]:
    """Raw bytes of one regular file — the same containment proof as :func:`read_file`, no decoding.

    :func:`read_file` is a *display* surface: it decodes with ``errors="replace"``, so every
    invalid byte becomes U+FFFD. Re-encoding that string cannot distinguish a file that was always
    valid UTF-8 from one that was mangled on the way in — the replacement character is a legal
    character, and the original bytes are gone. A caller that has to *decide* whether the content
    is text (#784's diff path, which refuses rather than render a mangled diff) therefore needs the
    bytes as stored.

    Returns ``(verified_path, data, size, truncated)``. Bounded while reading, so a huge file is
    never materialised.
    """
    _refuse_if_symlink(path)
    resolved = contained_path(path)
    fd, st, verified = _open_verified(resolved, directory=False)
    try:
        chunks: list[bytes] = []
        read_total = 0
        truncated = False
        while read_total < limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit - read_total))
            if not chunk:
                break
            chunks.append(chunk)
            read_total += len(chunk)
        else:
            truncated = bool(os.read(fd, 1))
    finally:
        os.close(fd)
    return verified, b"".join(chunks), st.st_size, truncated


def read_file(path: str) -> dict:
    """One regular file, capped **while reading** so a 3 GB file is never materialised.

    Binary is decided by a NUL in the first sniff window; a binary file returns metadata only.
    Text decodes with ``errors="replace"`` — this is a *display* surface, so a mixed-encoding
    source file should render rather than 500. (#784's diff path decodes strictly instead: a
    silently mangled diff is worse than an honest refusal.)
    """
    _refuse_if_symlink(path)
    resolved = contained_path(path)
    fd, st, verified = _open_verified(resolved, directory=False)
    try:
        size = st.st_size
        chunks: list[bytes] = []
        read_total = 0
        truncated = False
        first = b""
        while read_total < FILES_MAX_READ:
            chunk = os.read(fd, min(_READ_CHUNK, FILES_MAX_READ - read_total))
            if not chunk:
                break
            if not first:
                first = chunk[:_BINARY_SNIFF_BYTES]
                if b"\x00" in first:
                    return {
                        "path": verified,
                        "size": size,
                        "binary": True,
                        "mime": _guess_mime(verified),
                    }
            chunks.append(chunk)
            read_total += len(chunk)
        else:
            # Hit the cap: is there more behind it?
            truncated = bool(os.read(fd, 1))
    finally:
        os.close(fd)

    data = b"".join(chunks)
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return {"path": verified, "size": size, "binary": True, "mime": _guess_mime(verified)}
    return {
        "path": verified,
        "size": size,
        "binary": False,
        "content": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


__all__ = [
    "FILES_MAX_ENTRIES",
    "FILES_MAX_READ",
    "FILES_SCAN_BUDGET_MS",
    "Capabilities",
    "FilesBusy",
    "FsError",
    "acquire",
    "Slot",
    "acquire_slot",
    "capabilities",
    "executor",
    "fairness_key",
    "guarded",
    "inflight_workers",
    "list_dir",
    "release",
    "run_slot",
    "shutdown_executor_for_test",
    "read_file",
    "read_file_bytes",
    "reset_capabilities_for_test",
]
