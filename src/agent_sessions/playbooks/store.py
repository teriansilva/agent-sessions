"""Where playbooks come from, and the versioned authoring store for `local` ones (#1191, #1192).

**Sources** (#1096 §8). `bundled` ships in-tree (`loader.BUNDLED_ROOT`), `catalog` is P10's signed
index (`catalog_root()`, none until then), and `local` is the operator's own tree under
`~/.config/agent-sessions/playbooks/<id>/` (override `AGENT_SESSIONS_PLAYBOOKS_DIR`). Ids are
unique across sources: the first source that holds an id owns it (bundled, then catalog, then
local), and a local directory shadowed by another source is listed as an error card and cannot be
addressed. Listing is fail-soft per bundle, exactly like `loader.list_bundles`.

**Only `local` is writable.** `bundled` and `catalog` are read-only (`ReadOnly`, a 403): the way
to change one is *Duplicate to local*.

**A revision is the content.** A playbook's `revision` is `Tree.digest()` of its snapshot — sha256
over every directory and file — so an edit made by hand on disk moves it exactly like an edit made
through the API. Edit, delete and set-default carry the revision the operator read and are refused
(`Conflict`, a 409 carrying the current card) when it moved. Saving never touches a deployment:
projects stay pinned to the revision they deployed and see *update available* (PR 2 compares).

**Writes are descriptor-relative, validated, staged, then published in one rename.** The whole
bundle is validated by the P1 validator (`loader.validate_named`) in memory first. Then, under the
root's exclusive lock:

1. the root is opened `O_DIRECTORY | O_NOFOLLOW` (a symlinked root is refused) and what an
   interrupted write left is SETTLED, never deleted: a `.staging-<id>-*` tree is retained in the
   recovery area, a `.trash-<id>-*` is retained (deletion committed) or restored under its id (not
   committed); state candidates and displaced state are retained too;
2. the tree is written into a fresh `.staging-<id>-<random>` directory: every directory `mkdir`ed
   and opened `O_NOFOLLOW` relative to its parent's descriptor, every file created
   `O_EXCL | O_NOFOLLOW` relative to its directory's descriptor, 0600/0700, written in full and
   `fsync`ed, and every directory `fsync`ed — no path string is ever resolved below the root;
3. the staged tree is READ BACK through the same node-policy walk and must equal, byte for byte,
   the snapshot that was validated;
4. it is published: a new playbook by `renameat2(RENAME_NOREPLACE)` (never over an existing entry,
   not even an empty directory), an edit by `renameat2(RENAME_EXCHANGE)` so the id names either the
   old tree or the new one at every instant, and the root is `fsync`ed;
5. whatever is displaced (the old tree, a deleted one) is MOVED into `.recovery/` — displace,
   never drop. Online operations never purge recovery copies.

A crash anywhere leaves either the old tree or the new one under the id, plus a dot-named leftover
the next write settles (retains or restores). Only directories implied by the files are ever
created (#1248 note 3: a stray empty directory is never mirrored).

**One lock, taken on the worker thread.** `root_lock` is an `flock` on `<root>/.lock`, opened
`O_CLOEXEC | O_NOFOLLOW` so no agent BattleLab starts inherits it: exclusive for every write,
shared for every read of the local tree. Every public function here is BLOCKING and acquires and
releases the lock inside itself, so the routes run them with `asyncio.to_thread`: a cancelled
request cannot strand the lock, because the thread that took it is the thread that releases it,
in a `finally`. A lock another writer holds past `LOCK_WAIT_S` is a 503, never a stolen lock.

**The default** (`set_default`) is the playbook the New project wizard preselects. It lives in the
root's `.state.json` under the same lock, so deleting the default playbook clears the default in the
same critical section (#1096 §5 gallery rules). Duplicating never changes it.

**Deleting a local playbook that projects still run is refused** with the list of those projects
(`InUse`), asked of `deployments.projects_running` inside the exclusive lock (see that module for
the contract the lifecycle keeps).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import fileedit, renameat
from . import deployments, loader, schema, tomlw
from .errors import PlaybookFormatError
from .tree import Tree, check_segment, read_tree_at

ENV = "AGENT_SESSIONS_PLAYBOOKS_DIR"
SOURCE_BUNDLED = "bundled"
SOURCE_CATALOG = "catalog"
SOURCE_LOCAL = "local"
#: Lookup precedence, and the order the gallery lists sources in.
SOURCES = (SOURCE_BUNDLED, SOURCE_CATALOG, SOURCE_LOCAL)
READ_ONLY_SOURCES = frozenset({SOURCE_BUNDLED, SOURCE_CATALOG})

LOCK_NAME = ".lock"
STATE_NAME = ".state.json"
STATE_PENDING = ".pending-state.json"
STAGING_PREFIX = ".staging-"
TRASH_PREFIX = ".trash-"
STATE_TMP_PREFIX = ".state-"
STAGING_RE = re.compile(r"\.staging-([a-z][a-z0-9-]{1,47})-([0-9a-f]{16})")
#: How many retained copies a gallery or detail read lists (newest first); `recovery_total` counts.
RECOVERY_LIST_MAX = 100
#: Every tree a change DISPLACES (the old tree of a save, a deleted playbook, a tree a crashed
#: write left behind) is moved here, never deleted — `fileedit`'s "displace, never drop". Nothing
#: prunes it: pruning is exactly the check-then-unlink race, even with a reviewed revision.
#: `recovery_entries` lists copies; permanent cleanup requires an offline operator procedure.
RECOVERY_DIR = ".recovery"
RECOVERY_NAME_RE = re.compile(r"([a-z][a-z0-9-]{1,47})-(\d{8}T\d{6})-([0-9a-f]{8})")
TRASH_RE = re.compile(r"\.trash-([a-z][a-z0-9-]{1,47})-([0-9a-f]{16})")
LEFTOVER_PREFIXES = (STAGING_PREFIX, TRASH_PREFIX, STATE_TMP_PREFIX)  # settled by `_sweep`
STATE_MAX_BYTES = 64 * 1024
LOCK_WAIT_S = 5.0
_POLL_S = 0.02

REVISION_RE = re.compile(r"[0-9a-f]{64}")
FILE_MODE = 0o600
DIR_MODE = 0o700
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

COPY_SUFFIX = " (copy)"


class StoreError(Exception):
    """A refusal with the HTTP status the route answers and extra body fields."""

    status = 422

    def __init__(self, detail: str, *, status: int | None = None, **extra: object) -> None:
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status
        self.extra = extra


class NotFound(StoreError):
    status = 404


class ReadOnly(StoreError):
    status = 403


class Conflict(StoreError):
    status = 409


class InUse(Conflict):
    """Delete refused: `projects` still run the playbook."""


class Busy(StoreError):
    status = 503


def local_root() -> Path:
    return Path(os.environ.get(ENV, str(Path.home() / ".config" / "agent-sessions" / "playbooks")))


def bundled_root() -> Path:
    return loader.BUNDLED_ROOT  # read at call time, so a test can point it at a fixture tree


def catalog_root() -> Path | None:
    """The staged catalog tree (P10, #1199). None: no catalog source exists yet."""
    return None


# ---- validation of client input ------------------------------------------------------------------


def playbook_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw.isascii() or not schema.PLAYBOOK_ID_RE.fullmatch(raw):
        raise StoreError("not a playbook id")
    return raw


def revision(raw: object) -> str:
    if not isinstance(raw, str) or not raw.isascii() or not REVISION_RE.fullmatch(raw):
        raise StoreError("revision is required and is the one you loaded")
    return raw


def _file_path(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw or len(raw) > schema.PATH_MAX:
        raise StoreError(f"a file path is a relative path of at most {schema.PATH_MAX} characters")
    parts = raw.split("/")
    if len(parts) > schema.MAX_TREE_DEPTH + 1:
        raise StoreError(f"{raw!r} is nested deeper than {schema.MAX_TREE_DEPTH}")
    for seg in parts:
        try:
            check_segment(seg, raw)
        except PlaybookFormatError as e:
            raise StoreError(str(e)) from None
    return parts


def _file_bytes(path: str, raw: object) -> bytes:
    """A file's bytes from one of three spellings: UTF-8 text (a string), `{"base64": …}`, or —
    for a `.toml` file only — `{"toml": {…}}`, a document BattleLab writes for the caller."""
    if isinstance(raw, str):
        if len(raw) > schema.MAX_FILE_BYTES:
            raise StoreError(f"{path} is larger than {schema.MAX_FILE_BYTES} bytes")
        try:
            return raw.encode("utf-8")
        except UnicodeEncodeError:
            raise StoreError(f"{path} is not valid Unicode text") from None
    if isinstance(raw, dict) and set(raw) == {"base64"}:
        b64 = raw["base64"]
        if not isinstance(b64, str) or len(b64) > (schema.MAX_FILE_BYTES * 4) // 3 + 4:
            raise StoreError(f"{path}: base64 is a string of at most {schema.MAX_FILE_BYTES} bytes")
        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise StoreError(f"{path}: base64 does not decode") from None
    if isinstance(raw, dict) and set(raw) == {"toml"}:
        if not path.endswith(".toml"):
            raise StoreError(f"{path}: only a .toml file may be sent as a document")
        try:
            return tomlw.dumps(raw["toml"]).encode("utf-8")
        except tomlw.TomlWriteError as e:
            raise StoreError(f"{path}: {e}") from None
    raise StoreError(f'{path}: a file is text, {{"base64": …}} or (for .toml) {{"toml": {{…}}}}')


def tree_from_files(raw: object) -> Tree:
    """The bundle a client sent (`{path: file}`), as an in-memory snapshot — bounded like the walk
    bounds a bundle on disk. Directories are exactly the files' parents."""
    if not isinstance(raw, dict) or not raw:
        raise StoreError("files is a non-empty object of path → file")
    if len(raw) > schema.MAX_TREE_ENTRIES:
        raise StoreError(f"a bundle has at most {schema.MAX_TREE_ENTRIES} entries")
    tree = Tree()
    total = 0
    for path, value in raw.items():
        parts = _file_path(path)
        data = _file_bytes(path, value)
        if len(data) > schema.MAX_FILE_BYTES:
            raise StoreError(f"{path} is larger than {schema.MAX_FILE_BYTES} bytes")
        total += len(data)
        if total > schema.MAX_TOTAL_BYTES:
            raise StoreError(f"the bundle is larger than {schema.MAX_TOTAL_BYTES} bytes")
        tree.files[path] = data
        for i in range(1, len(parts)):
            tree.dirs.add("/".join(parts[:i]))
    clash = sorted(set(tree.files) & tree.dirs)
    if clash:
        raise StoreError(f"{clash[0]} is both a file and a directory")
    if len(tree.files) + len(tree.dirs) > schema.MAX_TREE_ENTRIES:
        raise StoreError(f"a bundle has at most {schema.MAX_TREE_ENTRIES} entries")
    return tree


def _validate(tree: Tree, name: str) -> dict:
    """The P1 validator, the one authority on what a bundle may say."""
    try:
        return loader.validate_named(tree, name)
    except PlaybookFormatError as e:
        raise StoreError(str(e), field=e.field) from None
    except RecursionError:
        raise StoreError("the bundle is nested too deeply") from None


def _identity_id(tree: Tree) -> str:
    """The id a new bundle names itself by (validated in full by `_validate` right after)."""
    try:
        doc = tomllib.loads(tree.files.get(schema.MANIFEST_NAME, b"").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        doc = {}
    ident = doc.get("identity") if isinstance(doc, dict) else None
    raw = ident.get("id") if isinstance(ident, dict) else None
    if not isinstance(raw, str) or not schema.PLAYBOOK_ID_RE.fullmatch(raw):
        _validate(tree, "")  # the validator's own words for what is wrong
        raise StoreError("playbook.toml: identity.id is not a playbook id")
    return raw


# ---- the local root: descriptor, lock, state -----------------------------------------------------


def _open_root(*, create: bool) -> int | None:
    root = local_root()
    if create:
        root.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        with contextlib.suppress(FileExistsError):
            os.mkdir(root, DIR_MODE)
    try:
        return os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        if create:
            raise StoreError("the local playbook folder disappeared", status=503) from None
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StoreError(
                "the local playbook folder is not a real directory (a symlink is never followed)",
                status=409,
            ) from None
        raise StoreError(
            f"the local playbook folder cannot be opened ({e.strerror})", status=503
        ) from None


@contextlib.contextmanager
def _flock(root_fd: int, exclusive: bool, wait: float) -> Iterator[None]:
    try:
        # O_NONBLOCK: a FIFO planted at the lock's name must not hang the open.
        fd = os.open(
            LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as e:
        raise StoreError(f"the playbook lock cannot be opened ({e.strerror})", status=503) from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise StoreError("the playbook lock is not a regular file", status=503)
    try:
        op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, op)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("another playbook change is in progress; try again") from None
                time.sleep(_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def root_lock(*, exclusive: bool, wait: float = LOCK_WAIT_S) -> Iterator[int | None]:
    """BLOCKING. The local root's descriptor under its lock (None when there is no local root and
    `exclusive` is False). Exclusive creates the root. PR 2 records a deployment under it
    (shared)."""
    root_fd = _open_root(create=exclusive)
    if root_fd is None:
        yield None
        return
    try:
        with _flock(root_fd, exclusive, wait):
            if exclusive:
                _sweep(root_fd)
                _heal_default(root_fd)
            yield root_fd
    finally:
        os.close(root_fd)


def _recovery_fd(root_fd: int) -> int:
    with contextlib.suppress(FileExistsError):
        os.mkdir(RECOVERY_DIR, DIR_MODE, dir_fd=root_fd)
        _fsync_dir(root_fd)
    return os.open(RECOVERY_DIR, _DIR_FLAGS, dir_fd=root_fd)


_NOT_RETAINED_YET = (
    "the displaced copy could not be moved into the recovery area yet; it stays in the local "
    "playbook folder under a dot-name and the next write retains it"
)


class Retained(str):
    """The recovery name a tree was moved to; `.durable` is False (with `.reason`) when the move
    landed but could not be flushed to disk."""

    durable: bool = True
    reason: str = ""


def _retain(root_fd: int, name: str, pid: str) -> Retained | None:
    """Move the displaced tree `name` into the recovery area as `<id>-<time>-<rand>` (NOREPLACE).
    Never deletes, and NEVER RAISES: before the rename nothing has moved (None — the tree stays
    under its dot-name and the next sweep retains it); after it the tree IS retained, and a failed
    flush is reported as `durable = False`, not raised (the publication split)."""
    try:
        rfd = _recovery_fd(root_fd)
    except OSError:
        return None
    try:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        for _ in range(8):
            target = f"{pid}-{stamp}-{secrets.token_hex(4)}"
            try:
                renameat.renameat2(root_fd, name, rfd, target, renameat.RENAME_NOREPLACE)
            except FileExistsError:
                continue
            except OSError:
                return None
            out = Retained(target)
            try:
                _fsync_dir(rfd)
                _fsync_dir(root_fd)
            except OSError as e:
                out.durable = False
                out.reason = (
                    f"the retained copy could not be flushed to disk "
                    f"({e.strerror or type(e).__name__})"
                )
            return out
        return None
    finally:
        os.close(rfd)


def _state_for_write(root_fd: int) -> dict:
    """The state a WRITE starts from, read STRICTLY: publishing a new state built from a lenient
    read of a damaged file would publish `committed: []` and let the next sweep resurrect a
    committed delete. A damaged state refuses the write (503) and nothing is written."""
    state = _read_state_strict(root_fd)
    if state is None:
        raise StoreError(
            f"the playbook store state is damaged or unsettled; inspect {STATE_NAME}, "
            f"{STATE_PENDING} and retained state copies in the local playbook folder "
            "before repairing it (nothing was changed)",
            status=503,
        )
    return state


_ABSENT = object()
_UNREADABLE = object()


def _state_bytes(root_fd: int):
    """The state file's bytes from ONE open (`_ABSENT`, `_UNREADABLE` or the bytes). Every reader
    parses exactly these bytes — no second open a swap could slip in between."""
    try:
        os.stat(STATE_PENDING, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        return _UNREADABLE
    else:
        return _UNREADABLE  # interrupted/uncertain publication: never guess commitment evidence
    try:
        # O_NONBLOCK, then fstat: a FIFO at this name must never block a reader holding the lock.
        fd = os.open(
            STATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd
        )
    except FileNotFoundError:
        return _ABSENT
    except OSError:
        return _UNREADABLE
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _UNREADABLE
        return os.read(fd, STATE_MAX_BYTES + 1)
    except OSError:
        return _UNREADABLE
    finally:
        os.close(fd)


def _parse_state(data: object, *, strict: bool) -> dict | None:
    """Normalise the state from `_state_bytes`. Lenient (`strict=False`): anything damaged reads
    as "no default, nothing committed". Strict: an absent file is the empty state, and anything
    that EXISTS but is unreadable, unparsable or the wrong shape is None."""
    empty = {"version": 1, "default": None, "committed": []}
    if data is _ABSENT:
        return empty
    if data is _UNREADABLE or not isinstance(data, bytes) or len(data) > STATE_MAX_BYTES:
        return None if strict else empty
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        return None if strict else empty
    ok = isinstance(doc, dict) and type(doc.get("version")) is int and doc["version"] == 1
    raw = doc.get("committed", []) if ok else None  # type: ignore[union-attr]
    if strict and (
        not ok
        or not isinstance(raw, list)
        or not all(isinstance(c, str) and c.isascii() and TRASH_RE.fullmatch(c) for c in raw)
    ):
        return None
    default = doc.get("default") if ok else None  # type: ignore[union-attr]
    if not isinstance(default, str) or not schema.PLAYBOOK_ID_RE.fullmatch(default):
        default = None
    committed = (
        [c for c in raw if isinstance(c, str) and TRASH_RE.fullmatch(c)]
        if isinstance(raw, list)
        else []
    )
    return {"version": 1, "default": default, "committed": committed}


def _read_state_strict(root_fd: int) -> dict | None:
    """The state, or None when it EXISTS but cannot be read or parsed — the sweep then leaves
    every trash entry exactly where it is (a damaged state must not resurrect a committed
    delete). An absent file is the empty state. One open, one read, one parse."""
    return _parse_state(_state_bytes(root_fd), strict=True)


def _withdraw_restored(root_fd: int, pid: str, trash: str, restored_fd: int) -> list[str]:
    """Withdraw only the tree this sweep restored. A replacement keeps its live name.

    The pre-check avoids moving an already replaced name; verification AFTER displacement
    closes the check-to-rename interval. Use a fresh staging name, never committed trash, until
    identity is proved. If putting an intervening editor's tree back meets another writer,
    preserve the newest live tree and retain the displaced one, reporting its recovery name.
    """
    if not _same_inode(root_fd, pid, restored_fd):
        return []
    aside = f"{STAGING_PREFIX}{pid}-{secrets.token_hex(8)}"
    try:
        renameat.renameat2(root_fd, pid, root_fd, aside, renameat.RENAME_NOREPLACE)
    except OSError:
        return []  # no rename happened; never retain an unchecked live name as a fallback
    target = trash if _same_inode(root_fd, aside, restored_fd) else pid
    try:
        renameat.renameat2(root_fd, aside, root_fd, target, renameat.RENAME_NOREPLACE)
    except OSError:
        kept = _retain(root_fd, aside, pid)
        return [str(kept or aside)]
    return []


def _sweep(root_fd: int) -> None:
    """Settle what an interrupted write left behind (exclusive lock held). Nothing a playbook ever
    was is deleted: a `.trash-*` whose deletion the published state records as COMMITTED moves to
    the recovery area; an UNCOMMITTED one (the state write never landed) goes back under its id;
    a `.staging-*` (possibly an old tree an exchange displaced) is retained. State
    temporaries are retained too; a substituted name is never unlinked."""
    with os.scandir(root_fd) as it:
        names = [e.name for e in it]
    state = _read_state_strict(root_fd)
    committed = set((state or {}).get("committed", []))
    settled: set[str] = set()
    for name in names:
        if name.startswith(STATE_TMP_PREFIX):
            _keep_state(root_fd, name)
            continue
        m = TRASH_RE.fullmatch(name)
        if m is not None and state is None:
            continue  # the state cannot be read: whether this delete was committed is unknown
        if m is not None:
            if _read_state_strict(root_fd) != state:
                return  # commitment evidence moved: leave the remaining trash untouched
            pid = m.group(1)
            if name in committed:
                if _retain(root_fd, name, pid) is not None:
                    settled.add(name)
                continue
            try:
                restored_fd = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
            except OSError:
                _retain(root_fd, name, pid)
                continue
            try:
                try:
                    renameat.renameat2(root_fd, name, root_fd, pid, renameat.RENAME_NOREPLACE)
                except OSError:
                    _retain(root_fd, name, pid)  # its name is taken: kept in recovery instead
                    continue
                if _read_state_strict(root_fd) != state:
                    kept = _withdraw_restored(root_fd, pid, name, restored_fd)
                    _fsync_after(root_fd)
                    raise StoreError(
                        "the playbook state changed during recovery; try again",
                        status=503,
                        kept=kept,
                    )
                _fsync_after(root_fd)
            finally:
                os.close(restored_fd)
            continue
        if name.startswith(STAGING_PREFIX):
            sm = STAGING_RE.fullmatch(name)
            _retain(root_fd, name, sm.group(1) if sm else "unsaved")
    if state is not None and (settled or (committed - set(names))):
        with contextlib.suppress(StoreError):
            _write_state(
                root_fd,
                {**state, "committed": sorted(committed & set(names) - settled)},
                expected=state,
            )


def _heal_default(root_fd: int) -> None:
    """A delete interrupted between its rename-aside and its state write leaves a default naming a
    playbook that is gone. Reads already show no default (`_default_of`); the next write, holding
    the exclusive lock, makes the file agree. Only an id NO source holds is cleared — an invalid
    bundled playbook keeps its place — and only when every source was listed SUCCESSFULLY: a
    transiently unreadable bundled or catalog root never clears a default."""
    state = _read_state_strict(root_fd)
    if state is None or state["default"] is None:
        return  # a damaged state is never rewritten here: its `committed` list may be lost
    try:
        entries = [
            *_entries_at_path(bundled_root(), SOURCE_BUNDLED, strict=True),
            *_entries_at_path(catalog_root(), SOURCE_CATALOG, strict=True),
            *_entries_in(root_fd, SOURCE_LOCAL, skip_dot=True),
        ]
    except (_Incomplete, OSError):
        return  # a source could not be read: never clear a default on an incomplete listing
    if not any(e.id == state["default"] for e in entries):  # held anywhere, valid or not: kept
        _write_state(root_fd, {**state, "default": None}, expected=state)


def _fsync_after(fd: int) -> str | None:
    """A directory fsync AFTER a rename that already published something: never raises (the
    change happened); returns the reason it could not be flushed, for the caller to report."""
    try:
        _fsync_dir(fd)
    except OSError as e:
        return f"the change is saved but could not be flushed to disk ({e.strerror or e})"
    return None


def _fsync_dir(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as e:  # pragma: no cover — a filesystem without directory fsync
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


def _read_state(root_fd: int | None) -> dict:
    """Lenient: an absent or damaged state file is "no default"."""
    if root_fd is None:
        return {"version": 1, "default": None, "committed": []}
    out = _parse_state(_state_bytes(root_fd), strict=False)
    assert out is not None
    return out


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[n:]


def _keep_state(root_fd: int, name: str) -> bool:
    """Retain state candidates/displaced files, including unverified substitutions and late
    descriptor writes. Neither failure cleanup nor the sweep ever unlinks their bytes."""
    try:
        renameat.renameat2(
            root_fd,
            name,
            root_fd,
            f".retained-state-{secrets.token_hex(16)}",
            renameat.RENAME_NOREPLACE,
        )
    except OSError:
        return False
    return True


def _same_inode(root_fd: int, name: str, fd: int) -> bool:
    try:
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        held = os.fstat(fd)
        return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)
    except OSError:
        return False


def _write_state(root_fd: int, state: dict, *, expected: dict) -> dict:
    """Publish only over the expected state. Kernel write leases exclude in-place writers;
    EXCHANGE followed by identity verification catches a path replacement during publication.
    Displaced state is retained, never unlinked. A failed post-publication flush is reported as
    saved but not durable, exactly like a playbook publication."""
    data = json.dumps(
        {"version": 1, "default": state["default"], "committed": state.get("committed", [])}
    ).encode("utf-8")
    tmp = f"{STATE_TMP_PREFIX}{secrets.token_hex(8)}"
    old_fd = new_fd = None
    exchanged = False
    pending = False
    settled = False
    marker_cleared = True
    try:
        try:
            old_fd = os.open(
                STATE_NAME,
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            if expected != {"version": 1, "default": None, "committed": []}:
                raise StoreError(
                    "the playbook store state changed; try again", status=503
                ) from None
        if old_fd is not None:
            st = os.fstat(old_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise StoreError("the playbook store state cannot be fenced", status=503)
            fileedit._take_lease(old_fd)
            before = os.pread(old_fd, STATE_MAX_BYTES + 1, 0)
            if _parse_state(before, strict=True) != expected:
                raise StoreError("the playbook store state changed; try again", status=503)
        new_fd = os.open(
            tmp,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            FILE_MODE,
            dir_fd=root_fd,
        )
        fileedit._take_lease(new_fd)
        _write_all(new_fd, data)
        os.fsync(new_fd)
        if not _same_inode(root_fd, tmp, new_fd) or not fileedit._lease_intact(new_fd):
            raise StoreError("the staged playbook state changed; try again", status=503)
        # Durable intent survives a crash between exchange and verification. An unresolved
        # marker makes state reads fail closed, so a later sweep cannot resurrect a deletion.
        marker = os.open(
            STATE_PENDING,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            FILE_MODE,
            dir_fd=root_fd,
        )
        pending = True
        try:
            _write_all(marker, json.dumps({"expected": expected, "proposed": state}).encode())
            os.fsync(marker)
        finally:
            os.close(marker)
        _fsync_dir(root_fd)
        if old_fd is not None:
            if not fileedit._lease_intact(old_fd):
                settled = True
                raise StoreError("the playbook store state is being edited; try again", status=503)
            renameat.renameat2(root_fd, tmp, root_fd, STATE_NAME, renameat.RENAME_EXCHANGE)
            exchanged = True
            if (
                not _same_inode(root_fd, tmp, old_fd)
                or not fileedit._lease_intact(old_fd)
                or not _same_inode(root_fd, STATE_NAME, new_fd)
                or not fileedit._lease_intact(new_fd)
            ):
                # Put the state actually displaced back. The temporary now holds whatever
                # was at the live name, so even a second intervening edit is retained.
                displaced = os.stat(tmp, dir_fd=root_fd, follow_symlinks=False)
                renameat.renameat2(root_fd, tmp, root_fd, STATE_NAME, renameat.RENAME_EXCHANGE)
                restored = os.stat(STATE_NAME, dir_fd=root_fd, follow_symlinks=False)
                if (
                    (restored.st_dev, restored.st_ino) != (displaced.st_dev, displaced.st_ino)
                    or not _same_inode(root_fd, tmp, new_fd)
                    or not fileedit._lease_intact(new_fd)
                ):
                    raise StoreError(
                        "the state changed during rollback; inspect retained state copies",
                        status=503,
                        state_unsettled=True,
                    )
                exchanged = False
                settled = True
                raise StoreError("the playbook store state changed; try again", status=503)
        else:
            renameat.renameat2(root_fd, tmp, root_fd, STATE_NAME, renameat.RENAME_NOREPLACE)
            if not _same_inode(root_fd, STATE_NAME, new_fd) or not fileedit._lease_intact(new_fd):
                raise StoreError(
                    "the published playbook state changed; inspect the local state before retrying",
                    status=503,
                    state_unsettled=True,
                )
        settled = True
        # Publication happened. A later path edit belongs to that writer; it must never be
        # overwritten by cleanup. Keep the displaced inode even after the lease is released.
    except (OSError, fileedit.FsError) as e:
        if exchanged:
            # A failed rollback leaves the published state uncertain. Preserve its commitment
            # evidence rather than claiming the deletion did not apply.
            raise StoreError(
                "the playbook state needs recovery; retained state copies are in the local folder",
                status=503,
                state_unsettled=True,
            ) from None
        settled = True  # the failed atomic syscall published nothing
        raise StoreError(
            f"the playbook state cannot be saved ({e}); check {STATE_NAME} in the local folder",
            status=503,
        ) from None
    finally:
        _keep_state(root_fd, tmp)
        if pending and settled:
            marker_cleared = _keep_state(root_fd, STATE_PENDING)
        for fd in (old_fd, new_fd):
            if fd is not None:
                fileedit._release_lease(fd)
                os.close(fd)
    if not marker_cleared:
        return {
            "state_durable": False,
            "state_reason": "the state was saved but its completion marker could not be "
            "settled; inspect the local state and retained copies before retrying",
        }
    try:
        _fsync_dir(root_fd)
    except OSError as e:
        return {
            "state_durable": False,
            "state_reason": f"the change is saved but could not be flushed to disk "
            f"({e.strerror or type(e).__name__})",
        }
    return {"state_durable": True}


# ---- reading the sources -------------------------------------------------------------------------


@dataclass
class Entry:
    id: str
    source: str
    tree: Tree | None = None
    pb: dict | None = None
    error: str | None = None
    revision: str | None = None
    #: False for a local directory shadowed by another source's id: listed, never addressed.
    addressable: bool = True
    #: The entry was refused for a reason that is not about its format: a failure while walking
    #: its tree that the walk does not turn into a format error (a `RecursionError`, a
    #: `ValueError`, an `OSError` from below the bundle root) or a place past the source's entry
    #: bound. A strict inventory refuses on these. (A bundle whose root cannot be OPENED — e.g.
    #: `chmod 000` — is a format error card: it still owns its id, so a local copy it shadows
    #: stays unaddressable and a write to that id is the read-only 403.)
    unreadable: bool = False


def _entry_at(dir_fd: int, name: str, source: str) -> Entry:
    if not schema.PLAYBOOK_ID_RE.fullmatch(name):
        return Entry(name, source, error="the folder name is not a playbook id", addressable=False)
    try:
        tree = read_tree_at(dir_fd, name)
    except PlaybookFormatError as e:
        return Entry(name, source, error=str(e))
    except (OSError, RecursionError, ValueError) as e:
        return Entry(name, source, error=f"could not be read ({type(e).__name__})", unreadable=True)
    entry = Entry(name, source, tree=tree, revision=tree.digest())
    try:
        entry.pb = loader.validate_named(tree, name)
    except PlaybookFormatError as e:
        entry.error = str(e)
    except (RecursionError, ValueError) as e:
        entry.error = f"could not be read ({type(e).__name__})"
    return entry


def _entries_in(dir_fd: int, source: str, *, skip_dot: bool) -> list[Entry]:
    with os.scandir(dir_fd) as it:
        names = sorted(e.name for e in it if not (skip_dot and e.name.startswith(".")))
    out = [_entry_at(dir_fd, n, source) for n in names[: schema.MAX_BUNDLES]]
    for n in names[schema.MAX_BUNDLES :]:
        out.append(
            Entry(
                n,
                source,
                error=f"not read: the source holds more than {schema.MAX_BUNDLES} entries",
                addressable=False,
                unreadable=True,
            )
        )
    return out


class _Incomplete(Exception):
    """A source could not be listed (anything but "it does not exist")."""


def _entries_at_path(path: Path | None, source: str, *, strict: bool = False) -> list[Entry]:
    """A source's entries. Lenient for the gallery (an unreadable source lists nothing); with
    `strict` only a source that is absent or configured away counts as empty, and any other
    failure raises `_Incomplete` — "I could not read it" is never "it holds nothing"."""
    if path is None:
        return []
    try:
        fd = os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise _Incomplete(source) from None
        return []
    try:
        return _entries_in(fd, source, skip_dot=False)
    except OSError:
        if strict:
            raise _Incomplete(source) from None
        return []
    finally:
        os.close(fd)


def _all_entries(root_fd: int | None, *, strict: bool = False) -> list[Entry]:
    """Every source's entries; an id is owned by the first source that holds it.

    Lenient (the gallery): a source that cannot be read lists as nothing. STRICT (every
    ownership-dependent write): a bundled or catalog root, or a bundle in one, that cannot be read
    refuses with a 503 — otherwise a local copy shadowed by an unreadable bundled id would look
    like the owner, and a write could land on it."""
    try:
        higher = [
            *_entries_at_path(bundled_root(), SOURCE_BUNDLED, strict=strict),
            *_entries_at_path(catalog_root(), SOURCE_CATALOG, strict=strict),
        ]
    except _Incomplete:
        raise StoreError("a playbook source could not be read; try again", status=503) from None
    if strict and any(e.unreadable for e in higher):
        raise StoreError("a playbook source could not be read; try again", status=503)
    entries = [
        *higher,
        *(_entries_in(root_fd, SOURCE_LOCAL, skip_dot=True) if root_fd is not None else []),
    ]
    owner: dict[str, str] = {}
    for e in entries:
        if not e.addressable:
            continue
        if e.id in owner:
            e.addressable = False
            e.error = f"the id {e.id!r} is already used by a {owner[e.id]} playbook"
            e.pb = None
        else:
            owner[e.id] = e.source
    return entries


def _find(entries: list[Entry], pid: str) -> Entry:
    for e in entries:
        if e.addressable and e.id == pid:
            return e
    raise NotFound("unknown playbook")


def _card(e: Entry, default: str | None) -> dict:
    base = loader.card(e.pb) if e.pb is not None else {"id": e.id, "ok": False, "error": e.error}
    return {
        **base,
        "source": e.source,
        "editable": e.source == SOURCE_LOCAL and e.addressable,
        "revision": e.revision,
        "default": e.addressable and e.id == default,
    }


def _default_of(entries: list[Entry], state: dict) -> str | None:
    d = state["default"]
    return d if any(e.addressable and e.pb is not None and e.id == d for e in entries) else None


def _json_safe(v: object) -> object:
    """A parsed TOML value as JSON, or ValueError (a date has no JSON spelling)."""
    if isinstance(v, bool | int | str):
        return v
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    raise ValueError(type(v).__name__)


def _files_of(tree: Tree) -> tuple[dict, dict]:
    files: dict[str, object] = {}
    documents: dict[str, object] = {}
    for path in sorted(tree.files):
        data = tree.files[path]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            files[path] = {"base64": base64.b64encode(data).decode("ascii")}
            continue
        files[path] = text
        if path.endswith(".toml"):
            try:
                documents[path] = _json_safe(tomllib.loads(text))
            except (tomllib.TOMLDecodeError, ValueError, RecursionError):
                pass
    return files, documents


def _detail(e: Entry, default: str | None) -> dict:
    out = _card(e, default)
    if e.tree is not None:
        out["files"], out["documents"] = _files_of(e.tree)
    if e.pb is not None:
        out["readme"] = e.pb["readme"]
        out["requires_present"] = loader.requires_status(e.pb)
    return out


# ---- public API (every function BLOCKING; run it off the event loop) -----------------------------


def _recovery_list(root_fd: int | None) -> list[dict]:
    if root_fd is None:
        return []
    try:
        rfd = os.open(RECOVERY_DIR, _DIR_FLAGS, dir_fd=root_fd)
    except OSError:
        return []
    try:
        with os.scandir(rfd) as it:
            names = sorted(e.name for e in it)
    finally:
        os.close(rfd)
    out = []
    for n in names:
        m = RECOVERY_NAME_RE.fullmatch(n) if n.isascii() else None
        if m:
            out.append({"name": n, "playbook_id": m.group(1), "at": m.group(2)})
    out.sort(key=lambda r: (r["at"], r["name"]), reverse=True)  # newest first
    return out


def _recovery_revision(root_fd: int, name: str) -> str | None:
    """Content AND directory identity, read through the same held descriptor. A byte-identical
    replacement is a different retained copy. Unreadable copies have no usable revision."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            return None
        digest = read_tree_at(fd, ".").digest()
        return hashlib.sha256(f"recovery/1:{st.st_dev}:{st.st_ino}:{digest}".encode()).hexdigest()
    except (OSError, PlaybookFormatError):
        return None
    finally:
        os.close(fd)


def _with_revisions(root_fd: int | None, entries: list[dict]) -> list[dict]:
    """Each listed entry with a content/identity `revision`, or None when unreadable."""
    if root_fd is None or not entries:
        return entries
    try:
        rfd = os.open(RECOVERY_DIR, _DIR_FLAGS, dir_fd=root_fd)
    except OSError:
        return [{**e, "revision": None} for e in entries]
    try:
        return [{**e, "revision": _recovery_revision(rfd, e["name"])} for e in entries]
    finally:
        os.close(rfd)


def recovery_entries() -> list[dict]:
    """Every retained copy: `[{name, playbook_id, at, revision}]` (see `RECOVERY_DIR`)."""
    with root_lock(exclusive=False) as root_fd:
        return _with_revisions(root_fd, _recovery_list(root_fd))


def list_playbooks() -> dict:
    """The gallery: one card per entry of every source, fail-soft per bundle, plus the retained
    copies in the recovery area."""
    with root_lock(exclusive=False) as root_fd:
        entries = _all_entries(root_fd)
        state = _read_state(root_fd)
        recovery = _recovery_list(root_fd)
        listed = _with_revisions(root_fd, recovery[:RECOVERY_LIST_MAX])
    default = _default_of(entries, state)
    return {
        "playbooks": [_card(e, default) for e in entries],
        "default": default,
        "recovery": listed,
        "recovery_total": len(recovery),
    }


def get_playbook(pid: str) -> dict:
    """One playbook's detail: its card, its files (text, or `{"base64"}`), its TOML documents
    parsed, its README and whether its required binaries are present."""
    playbook_id(pid)
    with root_lock(exclusive=False) as root_fd:
        entries = _all_entries(root_fd)
        state = _read_state(root_fd)
        mine = [r for r in _recovery_list(root_fd) if r["playbook_id"] == pid]
        listed = _with_revisions(root_fd, mine[:RECOVERY_LIST_MAX])
    out = _detail(_find(entries, pid), _default_of(entries, state))
    out["recovery"] = listed
    out["recovery_total"] = len(mine)
    return out


def _stage(root_fd: int, tree: Tree, pid: str = "unsaved") -> str:
    """Write `tree` into a fresh staging directory under the root, descriptor-relative, and prove
    by reading it back that it holds exactly `tree`. Returns the staging name."""
    name = f"{STAGING_PREFIX}{pid}-{secrets.token_hex(8)}"
    os.mkdir(name, DIR_MODE, dir_fd=root_fd)
    fds: dict[str, int] = {}
    try:
        fds[""] = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
        for d in sorted(tree.dirs, key=lambda p: (p.count("/"), p)):
            parent, _, leaf = d.rpartition("/")
            os.mkdir(leaf, DIR_MODE, dir_fd=fds[parent])
            fds[d] = os.open(leaf, _DIR_FLAGS, dir_fd=fds[parent])
        for path in sorted(tree.files):
            parent, _, leaf = path.rpartition("/")
            fd = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                FILE_MODE,
                dir_fd=fds[parent],
            )
            try:
                _write_all(fd, tree.files[path])
                os.fsync(fd)
            finally:
                os.close(fd)
        for fd in fds.values():  # every directory's entries are durable before publication
            _fsync_dir(fd)
        back = read_tree_at(root_fd, name)
        if back.files != tree.files or back.dirs != tree.dirs:
            raise Conflict("the playbook changed while it was being written; nothing was saved")
        return name
    except BaseException:
        # Even an unpublished name can have been replaced by an on-disk editor. Retain
        # whatever is there; an identity check followed by recursive removal is still racy.
        _retain(root_fd, name, pid)
        raise
    finally:
        for fd in fds.values():
            os.close(fd)


def _digest_at(root_fd: int, name: str) -> str | None:
    """The revision of the tree at `name` under the root, or None when it cannot be read."""
    try:
        return read_tree_at(root_fd, name).digest()
    except (PlaybookFormatError, OSError, RecursionError, ValueError):
        return None


def _put_back(
    root_fd: int, displaced: str, pid: str, *, exchange: bool, staged: str | None = None
) -> None:
    """The displaced tree is not the checked revision (it was edited on disk meanwhile): restore
    it under `pid` and refuse.

    After an EXCHANGE the id named the new tree for a moment, so a path-based edit could have
    landed in it too. The exchange-back therefore checks what it displaces a second time: only a
    tree that is still exactly what was staged (`staged`, its digest) is the store's own; anything
    else is an edit, and both are RETAINED in the recovery area — the edit's entry is named in the
    refusal (`kept`). After a rename-aside (delete) the edited tree simply goes back. Whatever
    cannot go back is retained too — never swept, never dropped."""
    try:
        if exchange:
            renameat.renameat2(root_fd, displaced, root_fd, pid, renameat.RENAME_EXCHANGE)
        else:
            renameat.renameat2(root_fd, displaced, root_fd, pid, renameat.RENAME_NOREPLACE)
    except OSError:
        kept = _retain(root_fd, displaced, pid)
        where = f"recovery entry {kept}" if kept else f"{displaced} in the local playbook folder"
        raise Conflict(
            f"this playbook was edited on disk during the change; the edited copy is kept as "
            f"{where}",
            kept=[str(kept or displaced)],
        ) from None
    with contextlib.suppress(OSError):
        _fsync_dir(root_fd)  # the exchange-back happened: the 409 below must still go out
    kept_names: list[str] = []
    if exchange:
        if staged is not None and _digest_at(root_fd, displaced) == staged:
            _retain(root_fd, displaced, pid)  # our staged tree; kept all the same (never dropped)
        else:
            kept_names.append(str(_retain(root_fd, displaced, pid) or displaced))
    detail = "this playbook was edited on disk during the change; nothing was replaced"
    if kept_names:
        detail += (
            f" — an edit made during the change is kept as recovery entry {', '.join(kept_names)}"
        )
    raise Conflict(detail, revision=_digest_at(root_fd, pid), kept=kept_names)


def _publish_new(root_fd: int, staging: str, pid: str) -> str | None:
    try:
        renameat.renameat2(root_fd, staging, root_fd, pid, renameat.RENAME_NOREPLACE)
    except FileExistsError:
        raise Conflict(f"a playbook named {pid} already exists") from None
    except OSError as e:
        raise StoreError(
            f"this filesystem cannot publish a playbook safely ({e.strerror})", status=503
        ) from None
    return _fsync_after(root_fd)


def _exists_locally(root_fd: int, pid: str) -> bool:
    try:
        os.stat(pid, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _ensure_free(root_fd: int, entries: list[Entry], pid: str) -> None:
    if any(e.id == pid for e in entries) or _exists_locally(root_fd, pid):
        raise Conflict(f"a playbook named {pid} already exists")


def _writable(entries: list[Entry], pid: str) -> Entry:
    """The local entry `pid`, or `ReadOnly` (403) for bundled/catalog — decided before anything
    about the request's body is looked at."""
    e = _find(entries, pid)
    if e.source in READ_ONLY_SOURCES:
        raise ReadOnly(f"a {e.source} playbook is read-only — duplicate it to local to change it")
    return e


def require_writable(pid: str) -> None:
    """The route's first step for an edit or a delete: 404 / 403 before the body is read."""
    playbook_id(pid)
    with root_lock(exclusive=False) as root_fd:
        _writable(_all_entries(root_fd, strict=True), pid)


def _local_for_write(entries: list[Entry], pid: str, expect: str) -> Entry:
    e = _writable(entries, pid)
    if e.revision is None:
        raise Conflict("this playbook cannot be read from disk; fix or remove it there")
    if e.revision != expect:
        raise Conflict(
            "this playbook changed since you loaded it", current=_card(e, None), revision=e.revision
        )
    return e


def create_playbook(files: object) -> dict:
    """A new `local` playbook from `files`; its id is its `identity.id`."""
    tree = tree_from_files(files)
    pid = _identity_id(tree)
    _validate(tree, pid)
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        _ensure_free(root_fd, entries, pid)
        not_durable = _publish_new(root_fd, _stage(root_fd, tree, pid), pid)
        entries = _all_entries(root_fd)
        state = _read_state(root_fd)
    out = _detail(_find(entries, pid), _default_of(entries, state))
    if not_durable:
        out.update(durable=False, durable_reason=not_durable)
    return out


def update_playbook(pid: str, expect: str, files: object) -> dict:
    """Replace a `local` playbook's whole tree, fenced by the revision the operator loaded. The id
    is the identity: `identity.id` must stay `pid` (no rename — duplicate, then delete)."""
    playbook_id(pid)
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        # Source first: a read-only playbook is a 403 whatever the body says — then the revision,
        # then the body.
        _writable(entries, pid)
        _local_for_write(entries, pid, revision(expect))
        tree = tree_from_files(files)
        _validate(tree, pid)
        if tree.digest() != expect:
            staging = _stage(root_fd, tree, pid)
            try:
                renameat.renameat2(root_fd, staging, root_fd, pid, renameat.RENAME_EXCHANGE)
            except OSError as e:
                _retain(root_fd, staging, pid)
                raise StoreError(
                    f"this filesystem cannot replace a playbook safely ({e.strerror})", status=503
                ) from None
            not_durable = _fsync_after(root_fd)
            # DISPLACE, NEVER DROP: the lock does not stop a hand edit on disk, so the tree the
            # exchange displaced must be the revision that was checked. Anything else is put back.
            if _digest_at(root_fd, staging) != expect:
                _put_back(root_fd, staging, pid, exchange=True, staged=tree.digest())
            kept = _retain(root_fd, staging, pid)  # the OLD tree, proven the checked revision
        else:
            kept = None
            not_durable = None
        displaced = kept is None and tree.digest() != expect
        entries = _all_entries(root_fd)
        state = _read_state(root_fd)
    out = _detail(_find(entries, pid), _default_of(entries, state))
    if not_durable:
        out.update(durable=False, durable_reason=not_durable)
    if kept is not None:
        out["retained"] = str(kept)
        if not kept.durable:
            out.update(recovery_durable=False, recovery_reason=kept.reason)
    elif displaced:
        out.update(recovery_durable=False, recovery_reason=_NOT_RETAINED_YET)
    return out


def delete_playbook(pid: str, expect: str) -> dict:
    """Delete a `local` playbook: refused while projects run it; clears the default if it was it."""
    playbook_id(pid)
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        _writable(entries, pid)  # 403 before the revision is even parsed
        _local_for_write(entries, pid, revision(expect))
        try:
            running = deployments.projects_running(pid)
        except deployments.DeploymentsUnavailable as e:
            raise StoreError(f"{e}; nothing was deleted", status=503) from None
        if running:
            raise InUse(
                f"{len(running)} project(s) still run this playbook — remove those deployments "
                "first",
                projects=running,
            )
        # The state this delete will publish is read STRICTLY before anything moves: a damaged
        # state is a 503 with the playbook still under its id (nothing was changed means nothing).
        state = _state_for_write(root_fd)
        trash = f"{TRASH_PREFIX}{pid}-{secrets.token_hex(8)}"
        try:
            renameat.renameat2(root_fd, pid, root_fd, trash, renameat.RENAME_NOREPLACE)
        except OSError as e:
            raise StoreError(
                f"this filesystem cannot remove a playbook safely ({e.strerror})", status=503
            ) from None
        _fsync_after(root_fd)  # the state commit below flushes the directory again
        # DISPLACE, NEVER DROP: what was moved aside must be the revision that was checked.
        if _digest_at(root_fd, trash) != expect:
            _put_back(root_fd, trash, pid, exchange=False)
        # COMMIT the deletion in the published state (and let the default follow) BEFORE the tree
        # leaves the trash name. If that write fails the delete did not happen: the tree goes back.
        new_state = {
            **state,
            "default": None if state["default"] == pid else state["default"],
            "committed": sorted({*state.get("committed", []), trash}),
        }
        try:
            # A hand edit to the state during displacement must refuse inside the rollback
            # boundary too. Never publish an older snapshot over changed commitment evidence.
            if _state_for_write(root_fd) != state:
                raise StoreError("the playbook store state changed; try again", status=503)
            durability = _write_state(root_fd, new_state, expected=state)
        except StoreError as error:
            if error.extra.get("state_unsettled"):
                _retain(root_fd, trash, pid)
                raise
            try:
                renameat.renameat2(root_fd, trash, root_fd, pid, renameat.RENAME_NOREPLACE)
            except OSError:
                _retain(root_fd, trash, pid)  # cannot go back: kept in recovery, never swept
            else:
                _fsync_after(root_fd)
            raise
        # The deletion is committed (the state was published, durable or not): it is FINISHED —
        # the tree is RETAINED in recovery, never removed — and reported, never raised.
        kept = _retain(root_fd, trash, pid)
        if kept is not None:
            with contextlib.suppress(StoreError):
                _write_state(
                    root_fd,
                    {**new_state, "committed": [c for c in new_state["committed"] if c != trash]},
                    expected=new_state,
                )
            durability = {**durability, "retained": str(kept)}
            if not kept.durable:
                durability.update(recovery_durable=False, recovery_reason=kept.reason)
        else:
            durability.update(recovery_durable=False, recovery_reason=_NOT_RETAINED_YET)
    return durability


def set_default(pid: str, expect: str, expect_default: object) -> dict:
    """Make `pid` (any source, valid) the default, fenced by its revision AND by the default the
    operator saw (`expect_default`, an id or None)."""
    playbook_id(pid)
    revision(expect)
    if expect_default is not None:
        playbook_id(expect_default)
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        e = _find(entries, pid)
        if e.pb is None:
            raise Conflict("an invalid playbook cannot be the default", current=_card(e, None))
        if e.revision != expect:
            raise Conflict(
                "this playbook changed since you loaded it",
                current=_card(e, None),
                revision=e.revision,
            )
        state = _state_for_write(root_fd)
        current = _default_of(entries, state)
        if current != expect_default:
            raise Conflict("the default playbook changed since you loaded it", default=current)
        durability = _write_state(root_fd, {**state, "default": pid}, expected=state)
    return {"default": pid, **durability}


def clear_default(pid: str) -> dict:
    """Clear the default, only if it is `pid` (otherwise a 409 naming the current default)."""
    playbook_id(pid)
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        state = _state_for_write(root_fd)
        current = _default_of(entries, state)
        if current != pid:
            raise Conflict(f"{pid} is not the default playbook", default=current)
        durability = _write_state(root_fd, {**state, "default": None}, expected=state)
    return {"default": None, **durability}


# ---- duplicate ---------------------------------------------------------------------------------


def _fresh(base: str, pattern: re.Pattern[str], max_len: int, taken: set[str]) -> str:
    """A fresh id `<base>-<4 hex>`, truncated to fit `pattern` / `max_len`, never one in `taken`."""
    for _ in range(64):
        suffix = f"-{secrets.token_hex(2)}"
        cand = base[: max_len - len(suffix)].rstrip("-_") + suffix
        if pattern.fullmatch(cand) and cand not in taken:
            taken.add(cand)
            return cand
    raise Conflict("could not find a fresh id; try again")  # pragma: no cover


def _remap_ref(value: object, steps: dict[str, str]) -> object:
    if not isinstance(value, str):
        return value
    return schema.STEP_TOKEN_RE.sub(
        lambda m: f"{{{{steps.{steps.get(m.group(1), m.group(1))}.{m.group(2)}}}}}", value
    )


def remap_flow(doc: dict, steps: dict[str, str]) -> dict:
    """A flow document with every step id — and every place one is named: `after`, `rework.to`,
    `distinct_from[].step` and `{{steps.<id>.<slot>}}` in probe arguments — mapped through
    `steps`. Anything else is copied unchanged."""
    out = dict(doc)
    new_steps = []
    for raw in doc.get("steps", []):
        s = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(s, dict):
            if isinstance(s.get("id"), str):
                s["id"] = steps.get(s["id"], s["id"])
            if isinstance(s.get("after"), list):
                s["after"] = [steps.get(a, a) if isinstance(a, str) else a for a in s["after"]]
            rw = s.get("rework")
            if isinstance(rw, dict) and isinstance(rw.get("to"), str):
                s["rework"] = {**rw, "to": steps.get(rw["to"], rw["to"])}
            if isinstance(s.get("distinct_from"), list):
                s["distinct_from"] = [
                    {**d, "step": steps.get(d["step"], d["step"])}
                    if isinstance(d, dict) and isinstance(d.get("step"), str)
                    else d
                    for d in s["distinct_from"]
                ]
            if isinstance(s.get("checklist"), list):
                items = []
                for it in s["checklist"]:
                    if isinstance(it, dict) and isinstance(it.get("probe_args"), dict):
                        it = {
                            **it,
                            "probe_args": {
                                k: _remap_ref(v, steps) for k, v in it["probe_args"].items()
                            },
                        }
                    items.append(it)
                s["checklist"] = items
        new_steps.append(s)
    if "steps" in doc:
        out["steps"] = new_steps
    return out


def _duplicate_tree(e: Entry, new_id: str, name: str) -> Tree:
    """The duplicate's snapshot: fresh playbook, flow and step ids, a new name, every reference to
    a renamed id remapped, and every other file byte for byte. (Rewritten TOML loses comments.)"""
    assert e.tree is not None and e.pb is not None
    src = e.tree
    out = Tree(files=dict(src.files), dirs=set(src.dirs))
    manifest = tomllib.loads(src.files[schema.MANIFEST_NAME].decode("utf-8"))
    manifest["identity"] = {**manifest["identity"], "id": new_id, "name": name}
    flow_ids: dict[str, str] = {}
    # FRESH means distinct from every ORIGINAL id too, not only from the ids minted so far: a
    # minted `a-0000` beside an original `a-0000` would otherwise have its file overwritten.
    taken: set[str] = set(e.pb["flows"])
    for fid in sorted(e.pb["flows"]):
        flow_ids[fid] = _fresh(fid, schema.FILE_ID_RE, 48, taken)
    # Every original flow file leaves first, then every new one is added: no name is ever both.
    for fid in flow_ids:
        del out.files[f"{schema.FLOWS_DIR}/{fid}.toml"]
    for fid, new_fid in flow_ids.items():
        rel = f"{schema.FLOWS_DIR}/{fid}.toml"
        doc = tomllib.loads(src.files[rel].decode("utf-8"))
        step_taken: set[str] = {s["id"] for s in e.pb["flows"][fid]["steps"]}
        steps = {
            s["id"]: _fresh(s["id"], schema.STEP_ID_RE, 32, step_taken)
            for s in e.pb["flows"][fid]["steps"]
        }
        out.files[f"{schema.FLOWS_DIR}/{new_fid}.toml"] = tomlw.dumps(
            remap_flow(doc, steps)
        ).encode("utf-8")
    flows_table = manifest.get("flows")
    if isinstance(flows_table, dict) and isinstance(flows_table.get("default"), str):
        manifest["flows"] = {
            **flows_table,
            "default": flow_ids.get(flows_table["default"], flows_table["default"]),
        }
    out.files[schema.MANIFEST_NAME] = tomlw.dumps(manifest).encode("utf-8")
    return out


def duplicate_playbook(pid: str, body: dict) -> dict:
    """Copy any valid playbook (bundled, catalog or local) to a new `local` one. `body` may carry
    `revision` (refused when stale), `id` and `name`; the default is never changed."""
    playbook_id(pid)
    unknown = sorted(set(body) - {"revision", "id", "name"})
    if unknown:
        raise StoreError(f"duplicate does not take {', '.join(unknown)}")
    expect = revision(body["revision"]) if "revision" in body else None
    want_id = playbook_id(body["id"]) if "id" in body else None
    want_name = body.get("name")
    if want_name is not None and (not isinstance(want_name, str) or not want_name.strip()):
        raise StoreError("name is a non-empty string")
    with root_lock(exclusive=True) as root_fd:
        assert root_fd is not None
        entries = _all_entries(root_fd, strict=True)
        e = _find(entries, pid)
        if e.pb is None or e.tree is None:
            raise Conflict("an invalid playbook cannot be duplicated; fix it first")
        if expect is not None and e.revision != expect:
            raise Conflict(
                "this playbook changed since you loaded it",
                current=_card(e, None),
                revision=e.revision,
            )
        taken = {x.id for x in entries}
        new_id = want_id or _fresh(pid, schema.PLAYBOOK_ID_RE, 48, taken)
        _ensure_free(root_fd, entries, new_id)
        name = want_name if want_name is not None else e.pb["identity"]["name"] + COPY_SUFFIX
        if len(name) > schema.NAME_MAX:
            name = name[: schema.NAME_MAX]
        try:
            tree = _duplicate_tree(e, new_id, name)
        except tomlw.TomlWriteError as err:
            raise Conflict(f"this playbook cannot be duplicated: {err}") from None
        _validate(tree, new_id)
        not_durable = _publish_new(root_fd, _stage(root_fd, tree, new_id), new_id)
        entries = _all_entries(root_fd)
        state = _read_state(root_fd)
    out = _detail(_find(entries, new_id), _default_of(entries, state))
    if not_durable:
        out.update(durable=False, durable_reason=not_durable)
    return out


def copy_draft(files: object) -> dict:
    """Save the supplied, validated draft with fresh identities, never a stored substitute.

    Read-only sources are untouched. Publication uses the ordinary create boundary, including
    its strict source inventory, full validation, read-back and NOREPLACE rename.
    """
    tree = tree_from_files(files)
    pid = _identity_id(tree)
    pb = _validate(tree, pid)
    with root_lock(exclusive=False) as root_fd:
        taken = {e.id for e in _all_entries(root_fd, strict=True)} | {pid}
    new_id = _fresh(pid, schema.PLAYBOOK_ID_RE, 48, taken)
    name = (pb["identity"]["name"] + COPY_SUFFIX)[: schema.NAME_MAX]
    try:
        copied = _duplicate_tree(Entry(pid, "local", tree=tree, pb=pb), new_id, name)
    except tomlw.TomlWriteError as err:
        raise StoreError(f"this draft cannot be copied: {err}") from None
    # The read lock above is only an id hint; create's own exclusive NOREPLACE check wins races.
    raw, _documents = _files_of(copied)
    return create_playbook(raw)
