"""Crash-safe JSON stores — one helper, so the next store inherits the behaviour (#728).

The repo keeps several small JSON documents (``prefs.json``, ``metadata.json``,
``pulse-cache.json``, the orchestrator ledger) and, before this module, wrote them three
different ways. The unsafe shape is the obvious one:

.. code-block:: python

    with path.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        data = json.load(fh)
        fh.seek(0); fh.truncate()     # <-- the document is now GONE from disk
        json.dump(data, fh)           # <-- and only now does it come back

Two things go wrong there, and only the second one is obvious:

1. **A failure between ``truncate()`` and a completed ``dump`` erases the document.**
   ``prefs.json`` holds the AI-review API key, so that window loses the operator's endpoint
   credential — not merely a UI preference.
2. **An unlocked reader inside that window sees an empty or half-written file**, hits its
   ``JSONDecodeError`` fallback, and reports *defaults*. The caller then acts on a document
   that says the feature is off and no endpoint is configured.

``atomic_write_json`` closes both: the document is serialized in full **before** anything on
disk is touched, written to a temp file, ``fsync``'d, and then ``os.replace``d into place.
``os.replace`` is atomic, so a reader observes either the whole old document or the whole new
one and never needs to take a lock to be safe. The parent directory is ``fsync``'d afterwards
because the *rename* is a namespace change: without it the new bytes are durable but the link
to them need not be, and after power loss the file can still name the old inode — or, on first
creation, not exist at all.

The exclusive lock stops being load-bearing for reader safety, but it is still required for
**read-modify-write ordering** (two writers must not each read the same base and clobber each
other) — that is ``json_write_lock``, and it deliberately locks a *sidecar*. See its docstring.

Two details of the temp file are load-bearing rather than incidental, and are argued where they
are implemented: it is **unique and 0600 before it holds any content** (a fixed name opened with
``O_CREAT`` inherits a stale file's permissions and is shared state between concurrent writers),
and a failed parent-directory ``fsync`` is **not** swallowed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

#: Suffix of the same-directory temp file. The NAME is unique per write (``mkstemp``) — a fixed
#: one is both a concurrency bug and a permissions bug (see ``atomic_write_json``).
TMP_SUFFIX = ".tmp"
#: Suffix of the lock sidecar (see ``json_write_lock``).
LOCK_SUFFIX = ".lock"


def fsync_dir(d: Path) -> None:
    """``fsync`` a directory so a rename/creation in it is durable, not just the file's bytes.

    Errors propagate. There is no honest "best effort" here: the caller's contract is durability,
    and swallowing the failure reports a crash-safe save that isn't one.
    """
    fd = os.open(str(d), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(
    path: Path | str,
    doc: object,
    *,
    mode: int = 0o600,
    indent: int | None = 2,
    sort_keys: bool = True,
) -> None:
    """Replace ``path`` with ``doc``, atomically and durably.

    Serialize → temp file in the SAME directory → ``fsync`` → ``os.replace`` → ``fsync`` the
    parent directory. Same-directory matters twice: ``os.replace`` is only atomic within a
    filesystem, and the directory whose rename must be made durable is the one being synced.

    Serializing *first* is the point of the ordering: a document that cannot be encoded raises
    before a single byte of the old one is at risk, where the truncate-in-place shape would
    already have destroyed it.

    The temp file is created by ``mkstemp`` — **unique, ``O_EXCL``, and 0600 from the moment it
    exists** — and ``mode`` is asserted on the descriptor *before* the first byte of the document
    is written. Both halves matter, and a fixed ``<name>.tmp`` opened with ``O_CREAT`` failed both
    (Hermes on #811):

    * ``O_CREAT`` keeps an existing file's permissions, so a stale 0644 temp left by a crash was
      still 0644 **while the API key was being written into it** — a chmod afterwards is too late,
      because the window it is closing has already passed.
    * A fixed name is shared state. Two concurrent writers raced on one inode: one raised
      ``FileNotFoundError`` when the other's ``os.replace`` moved the file out from under it, and
      the one that reported success had not written the document that ended up on disk.

    A failed parent-directory ``fsync`` **propagates**. The rename is what makes the new document
    reachable, so a caller told "saved" when that flush failed cannot tell a durable commit from
    one that may vanish on power loss — which is the whole guarantee this function sells.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, indent=indent, sort_keys=sort_keys).encode("utf-8")
    # Leading dot: a crash mid-write leaves a hidden sibling rather than something that looks
    # like a store. `mkstemp` is O_EXCL + 0600, so the file is private before it has content.
    fd, tmpname = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=TMP_SUFFIX)
    closed = False
    try:
        # BEFORE the payload — the point is that no byte of the document ever exists at a mode
        # looser than the caller asked for.
        os.fchmod(fd, mode)
        # POSIX permits a short write; a single `os.write` that returns fewer bytes would leave a
        # truncated document that the next `os.replace` would publish as the real one.
        written = 0
        while written < len(payload):
            n = os.write(fd, payload[written:])
            if n <= 0:
                raise OSError(f"short write to {tmpname} made no progress")
            written += n
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        closed = True
        # The temp is never a partial *published* document, but leaving a truncated one behind
        # invites a future reader to find it. Drop it; the old document is untouched.
        with contextlib.suppress(OSError):
            os.unlink(tmpname)
        raise
    finally:
        if not closed:
            os.close(fd)
    try:
        os.replace(tmpname, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmpname)
        raise
    fsync_dir(p.parent)


def read_json_doc(path: Path | str) -> dict:
    """The stored mapping, or ``{}`` for missing / unreadable / non-mapping content.

    Deliberately lock-free: with every write going through ``atomic_write_json`` there is no
    torn state to observe, so a reader that took the lock would only serialize itself against
    writers for no gain.
    """
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


@contextlib.contextmanager
def json_write_lock(path: Path | str) -> Iterator[None]:
    """Serialize read-modify-write cycles against ``path``.

    The lock is taken on a **sidecar** (``<name>.lock``), never on the document itself, and that
    is forced by the atomic write rather than a matter of taste: ``fcntl.flock`` is per **inode**
    and ``os.replace`` installs a *new* inode. A writer holding a lock on the document it is
    about to replace is, one instant later, holding a lock on an unlinked inode that no arriving
    writer can even find — so the mutex silently stops excluding anyone. The sidecar is never
    renamed, so it stays the one thing every writer agrees on.

    (``metadata.py`` reaches the same conclusion from the other side: it keeps the lock on the
    file and therefore must *not* ``os.replace`` it, and says so in its own comment.)
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.with_name(p.name + LOCK_SUFFIX)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "LOCK_SUFFIX",
    "TMP_SUFFIX",
    "atomic_write_json",
    "fsync_dir",
    "json_write_lock",
    "read_json_doc",
]
