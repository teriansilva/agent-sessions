"""Uploading files and whole folders **into** the browsed directory (#807).

The write half of the file panel, and — like :mod:`agent_sessions.gitwrite` — a deliberately
separate module from the read path it narrows. :mod:`agent_sessions.files` reads through
*open-then-verify*; a write needs the same shape plus one addition, because it **creates** path
components and creation is where traversal bugs live.

Three properties carry this module, each argued where it is enforced:

**Containment is descriptor-relative, component by component.** ``dir`` goes through
``contained_path`` and is then opened ``O_DIRECTORY | O_NOFOLLOW`` and re-verified through
``/proc/self/fd``. Each level of ``relpath`` is created and opened *relative to the descriptor
above it*, so a component that already exists as a symlink fails the ``O_NOFOLLOW`` open and the
upload is refused — never followed, and never checked-then-followed. The file itself is created
``O_EXCL``, which is what makes "never overwrite silently" structural rather than an ``exists()``
call a race can outrun.

**No upload may write into git metadata, and a name check does not close that.**
``hooks/pre-commit`` is code the agent's *next* commit runs and ``config`` names programs git
executes (#806 measured the list), so an upload that can write either turns "put a file in my
project" into code execution in the session. Refusing a component spelled ``.git`` misses a bare
repository, a ``.git``-*file* pointing elsewhere, and a linked worktree's common-dir — all of
which #782 explicitly supports. So
the refusal is against **discovered** metadata roots (:func:`gitpanel.discover_repo`) plus a
gitdir *shape* check, with the name check kept only as the cheap first filter.

**The byte bound binds at ingress, not in the route.** FastAPI hands a route its ``UploadFile``
only *after* Starlette has parsed the whole multipart request, and in the pinned Starlette a file
part is spooled to a ``SpooledTemporaryFile`` until EOF — so a chunked or false-``Content-Length``
request writes unbounded bytes to temporary disk before the route's first line runs, and the 413
arrives after the damage. This module therefore drives the parser itself from ``request.stream()``
and counts bytes **as they arrive**; nothing is ever spooled.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import threading
import time

from .files import FsError, _fd_still_contained, _require_capabilities
from .fsbrowse import contained_path, home_root
from .gitpanel import discover_repo

#: One file may not exceed this. Matches the compose box's existing `/api/upload` cap, and the
#: relay buffers a body roughly three times over (browser `arrayBuffer`, mux, agent).
MAX_FILE_BYTES = 25 * 1024 * 1024
#: A folder drop of a `node_modules` is a mis-drop, not a request.
MAX_BATCH_FILES = 500
MAX_BATCH_BYTES = 250 * 1024 * 1024
#: Idle expiry, not wall-clock from creation: a slow 400-file batch must not die mid-flight.
BATCH_IDLE_TTL_S = 15 * 60
MAX_LIVE_BATCHES = 8
#: How long a fully settled batch stays addressable so a retried Skip can be replayed rather than
#: meeting "unknown or expired". It counts against no LIVE cap while it lingers.
SETTLED_GRACE_S = 120
#: …but "counts against no cap" cannot mean "unbounded". Settled batches are excluded from the
#: live ceilings, so without their own bound a client could create-and-settle in a loop and grow
#: the registry without limit inside one grace window. The oldest lingering record is evicted
#: past this many — losing only the ability to replay a very stale retry.
MAX_SETTLED_LINGER = 32
#: Process-wide ceiling behind the per-owner cap. Rotating session identities defeats a per-owner
#: limit entirely — a probe admitted 800 live batches across 100 owners — so the registry needs a
#: bound that does not depend on who is asking.
MAX_LIVE_BATCHES_TOTAL = 64
#: `relpath` depth and component length. 255 is the common filesystem NAME_MAX.
MAX_DEPTH = 32
MAX_COMPONENT = 255
_CHUNK = 256 * 1024
#: Mode every uploaded file gets. The executable bit is NEVER set, whatever the client says:
#: uploading `+x` into a tree an agent runs commands in is not a convenience worth having.
FILE_MODE = 0o644


# --------------------------------------------------------------------------- batches


class BatchError(FsError):
    """An unknown, expired, or exhausted batch. Never a silent degrade to "unbounded"."""

    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg, status=status)


class Batch:
    """A server-side reservation, not a tally kept after the fact.

    Each upload is an independent, concurrent request, so a limit checked as "count what has
    landed so far" is not a limit: two requests can both read 499 and both proceed. Everything
    here is therefore an **atomic compare-and-set under one lock**, and the accounting is
    incremented *before* the bytes are accepted — which is what makes
    ``reserved + committed + in-flight <= cap`` true at every instant rather than only after a
    reconciliation that runs too late to stop anything.
    """

    def __init__(self, bid: str, manifest: dict[str, int], owner: str = ""):
        self.id = bid
        #: Who may use this batch, and whose allowance it counts against. #807 asks for a
        #: per-session cap; a process-global one let one client's drops lock out every other.
        self.owner = owner
        #: Immutable per-file manifest from batch creation: path identity -> declared size. A
        #: client claim, so it is an ADMISSION affordance (fail an over-budget folder before a
        #: byte moves) and never the enforcement.
        self.manifest = manifest
        self.files_used = 0
        self.bytes_used = 0
        #: Manifest entry -> how it ended: "landed", "skipped" or "failed". A MAPPING, not a
        #: set: membership alone cannot tell an idempotent Skip retry from a Skip arriving LATE,
        #: after the operator already chose Replace and the file landed. The first must replay;
        #: the second must be refused, not reported as a skip that never happened.
        self._outcome: dict[str, str] = {}
        #: When the batch became fully settled. It stays addressable for a grace period after
        #: that, so a retried Skip can be replayed instead of meeting "unknown or expired".
        self.settled_at: float | None = None
        #: Entries whose collision the operator has been asked about and has not yet answered.
        #: Only these may be settled by an explicit Skip.
        self._pending: set[str] = set()
        self.touched = time.monotonic()
        self.lock = threading.Lock()
        self._per_file: dict[str, int] = {}

    # -- files ---------------------------------------------------------------

    def take_file_slot(self, relpath: str) -> None:
        with self.lock:
            self._expire_guard()
            # A settled batch lingers ONLY so a retried Skip can be replayed. It must not still
            # accept writes: every manifest entry already reached a terminal outcome, so a new
            # upload against it would spend an allowance that was accounted for and closed, and
            # would land bytes for a drop the operator has been told is finished.
            if self.settled:
                raise BatchError("this upload batch already finished — start a new one")
            if self.files_used >= MAX_BATCH_FILES:
                raise BatchError(f"this batch is full (max {MAX_BATCH_FILES} files)")
            if self.manifest and relpath not in self.manifest:
                raise BatchError(f"{relpath!r} is not in this batch's manifest", status=422)
            self.files_used += 1
            self.touched = time.monotonic()

    def release_file_slot(self, relpath: str, streamed: int) -> None:
        """Roll a failed / refused / cancelled upload back.

        One bad file must not poison a batch — a 26 MB file in a 40-file drop is one red row.
        """
        with self.lock:
            self.files_used = max(0, self.files_used - 1)
            self.bytes_used = max(0, self.bytes_used - streamed)
            self._per_file.pop(relpath, None)
            self.touched = time.monotonic()

    # -- bytes ---------------------------------------------------------------

    def take_bytes(self, relpath: str, n: int) -> None:
        """Top the reservation up **before** accepting a chunk. Raises the moment it cannot.

        This is the load-bearing half. It never consults the declared size as an authority: a
        request that declares 1 MiB and streams 25 MiB gains nothing, because every chunk is
        charged here first. That also removes any field-ordering trap — a ``declared_size``
        arriving after the file part cannot matter to a bound that never reads it.
        """
        with self.lock:
            self._expire_guard()
            used = self._per_file.get(relpath, 0)
            # A file may stream no more than its own manifest entry, and never more than the
            # absolute per-file ceiling regardless of what the manifest claimed.
            ceiling = min(self.manifest.get(relpath, MAX_FILE_BYTES), MAX_FILE_BYTES)
            if used + n > ceiling:
                raise FsError(
                    f"that file is larger than the limit ({MAX_FILE_BYTES // (1024 * 1024)} MB)",
                    status=413,
                )
            if self.bytes_used + n > MAX_BATCH_BYTES:
                raise BatchError(
                    f"this batch is over its size limit "
                    f"({MAX_BATCH_BYTES // (1024 * 1024)} MB total)",
                    status=413,
                )
            self._per_file[relpath] = used + n
            self.bytes_used += n
            self.touched = time.monotonic()

    def _expire_guard(self) -> None:
        if time.monotonic() - self.touched > BATCH_IDLE_TTL_S:
            raise BatchError("this upload batch expired — start it again")

    @property
    def settled(self) -> bool:
        """Every file in the manifest has reached a terminal outcome.

        Without this a batch was only ever removed by IDLE EXPIRY, so eight completed one-file
        drops locked the ninth out for the full TTL — reproduced. A finished batch is finished.
        """
        return bool(self.manifest) and len(self._outcome) >= len(self.manifest)

    def _settle(self, relpath: str, outcome: str) -> None:
        """Mark ONE manifest entry terminal, recording HOW. Caller holds ``self.lock``.

        The FIRST outcome wins: a Skip that arrives later must not overwrite the fact that the
        file actually landed.
        """
        self._pending.discard(relpath)
        self._outcome.setdefault(relpath, outcome)
        self.touched = time.monotonic()
        if self.settled_at is None and self.manifest and len(self._outcome) >= len(self.manifest):
            self.settled_at = time.monotonic()

    def finish_file(self, relpath: str, outcome: str = "failed") -> None:
        """Record a terminal outcome for one entry — ``landed``, ``skipped`` or ``failed``.

        Idempotent per relpath, because settlement is keyed by entry rather than counted:
        settling the same entry twice can no longer retire a sibling that is still streaming.
        """
        with self.lock:
            self._settle(relpath, outcome)

    def mark_pending_collision(self, relpath: str) -> None:
        """The operator has been asked about this entry's collision and has not answered yet.

        This is what makes Skip *bindable*: an entry with no outstanding question cannot be
        settled by a Skip request, so the endpoint cannot be used to retire arbitrary entries.
        """
        with self.lock:
            if relpath not in self._outcome:
                self._pending.add(relpath)
            self.touched = time.monotonic()

    def skip_file(self, relpath: str) -> None:
        """Settle one entry because the operator chose Skip on its collision.

        Bound three ways: the entry must be in the manifest, it must have a collision actually
        awaiting an answer, and answering the same one twice is a no-op rather than a second
        settlement. Unbound, this endpoint settled *any* entry *any* number of times — enough to
        retire a batch whose real sibling was still in flight, which then failed as
        ``unknown or expired`` and freed the live-batch allowance early.
        """
        with self.lock:
            self._expire_guard()
            if not relpath:
                raise BatchError("a relpath is required", status=422)
            if self.manifest and relpath not in self.manifest:
                raise BatchError(f"{relpath!r} is not in this batch's manifest", status=422)
            prior = self._outcome.get(relpath)
            if prior == "skipped":
                # The same Skip answered twice — a client retry after a dropped response.
                return
            if prior is not None:
                # A LATE Skip: the operator already answered differently and the file reached a
                # real outcome. Replying "skipped" would report that nothing happened when the
                # file in fact landed, so this is refused rather than replayed.
                raise BatchError(
                    f"{relpath!r} already finished as {prior} — this Skip is stale", status=409
                )
            if relpath not in self._pending:
                raise BatchError(f"{relpath!r} has no collision awaiting an answer", status=409)
            self._settle(relpath, "skipped")

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "batch_id": self.id,
                "files_used": self.files_used,
                "bytes_used": self.bytes_used,
                "files_limit": MAX_BATCH_FILES,
                "bytes_limit": MAX_BATCH_BYTES,
            }


_batches: dict[str, Batch] = {}
_batches_lock = threading.Lock()

# NOTE: there was a per-directory `threading.Lock` here. It is gone, and its removal is the fix
# for two findings at once. It never closed the sibling race (both requests had already created
# their components before either reached the fence), and holding a *thread* lock on the event-loop
# thread across `await`ed executor jobs deadlocked the loop outright: a second upload finishing in
# the same directory blocked in `acquire()`, so the first coroutine could never resume to release
# it. The rollback above resolves the race without serialising anything.


def _reap() -> None:
    """Drop batches that went idle, and settled ones once their replay grace has passed.

    A settled batch used to be dropped the instant it settled, which freed the allowance but made
    the idempotent-Skip replay unreachable for a ONE-FILE batch: the Skip settled it, the reap
    deleted it, and the client's retry met "unknown or expired". Settled batches now linger
    briefly — long enough to answer a retry — while being excluded from every cap, so the lockout
    that motivated the immediate drop stays fixed.
    """
    now = time.monotonic()
    for bid, b in list(_batches.items()):
        idle = now - b.touched > BATCH_IDLE_TTL_S
        spent = b.settled and b.settled_at is not None and now - b.settled_at > SETTLED_GRACE_S
        if idle or spent:
            _batches.pop(bid, None)
    # Bound the lingering records themselves. They are exempt from the live ceilings, so without
    # this a create-and-settle loop grows `_batches` without limit inside one grace window —
    # the exemption becoming a hole in the very ceiling it was carved out of. Oldest goes first:
    # the older a settled record, the less likely a retry is still coming for it.
    lingering = sorted(
        ((b.settled_at or 0.0, bid) for bid, b in _batches.items() if b.settled),
        reverse=True,
    )
    for _, bid in lingering[MAX_SETTLED_LINGER:]:
        _batches.pop(bid, None)


def reset_batches_for_test() -> None:
    with _batches_lock:
        _batches.clear()


def create_batch(files: object, owner: str = "") -> dict:
    """Mint a batch from an immutable manifest, refusing an over-budget one before a byte moves.

    Admission only. The manifest is a client claim, so it buys a fast, honest failure for a
    folder drop that was never going to fit — it is never what bounds the stream.
    """
    if not isinstance(files, list) or not files:
        raise FsError("a batch needs at least one file", status=422)
    if len(files) > MAX_BATCH_FILES:
        raise FsError(
            f"that is {len(files)} files — this panel takes at most {MAX_BATCH_FILES} in one drop",
            status=413,
        )
    manifest: dict[str, int] = {}
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise FsError("each manifest entry must be an object", status=422)
        rel = item.get("relpath")
        size = item.get("size")
        if not isinstance(rel, str) or not rel:
            raise FsError("each manifest entry needs a relpath", status=422)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise FsError("each manifest entry needs a byte size", status=422)
        # Validated here too, so a batch can never be admitted naming a path the upload route
        # would go on to refuse.
        validate_relpath(rel)
        if size > MAX_FILE_BYTES:
            raise FsError(
                f"{rel!r} is {size // (1024 * 1024)} MB — the limit is "
                f"{MAX_FILE_BYTES // (1024 * 1024)} MB",
                status=413,
            )
        if rel in manifest:
            # Silently collapsing them let two sources race one destination: the first completion
            # settled a one-entry batch and the sibling lost its retry.
            raise FsError(f"{rel!r} appears twice in that drop", status=422)
        manifest[rel] = size
        total += size
    if total > MAX_BATCH_BYTES:
        raise FsError(
            f"that drop is {total // (1024 * 1024)} MB — the limit is "
            f"{MAX_BATCH_BYTES // (1024 * 1024)} MB",
            status=413,
        )
    with _batches_lock:
        _reap()
        # Counted PER OWNER: the cap is meant to bound one client's in-flight drops, and a global
        # count let one session's history lock every other session out.
        # Settled batches are excluded from BOTH caps: they linger only to replay a retry, and
        # counting them would recreate the very lockout `_reap` exists to prevent.
        mine = sum(1 for b in _batches.values() if b.owner == owner and not b.settled)
        if mine >= MAX_LIVE_BATCHES:
            raise BatchError("too many uploads are already in flight — finish one first")
        if sum(1 for b in _batches.values() if not b.settled) >= MAX_LIVE_BATCHES_TOTAL:
            # Fails closed under owner rotation, which the per-owner cap alone does not.
            raise BatchError("this box has too many uploads in flight — try again shortly")
        bid = secrets.token_urlsafe(18)
        _batches[bid] = Batch(bid, manifest, owner)
    return {
        "batch_id": bid,
        "files": len(manifest),
        "bytes": total,
        "files_limit": MAX_BATCH_FILES,
        "bytes_limit": MAX_BATCH_BYTES,
        "file_limit": MAX_FILE_BYTES,
    }


def get_batch(bid: object, owner: str = "") -> Batch:
    """The named batch, or a 409 naming the state. Never a quiet fallback to unbounded.

    A batch belongs to the session that minted it: an id alone must not let another session
    spend someone else's allowance.
    """
    if not isinstance(bid, str) or not bid:
        raise BatchError("that upload batch is unknown — start it again")
    with _batches_lock:
        _reap()
        b = _batches.get(bid)
    if b is None:
        raise BatchError("that upload batch is unknown or expired — start it again")
    if b.owner != owner:
        raise BatchError("that upload batch belongs to another session", status=403)
    return b


def implicit_batch() -> Batch:
    """A one-shot batch for a single-file upload, so the bound is applied on the same code path.

    Not registered in ``_batches``: nothing else will ever reference it, and leaving one-shot
    objects in the registry would burn the live-batch budget for no reason.
    """
    return Batch("implicit", {})


# --------------------------------------------------------------------------- path validation


_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def validate_relpath(rel: str) -> list[str]:
    """``relpath`` as **components**, never as a joined string.

    This is the zip-slip surface, and it is refused by construction rather than by a prefix check
    on the joined result — a check that is only as good as the normalisation in front of it.
    """
    if not isinstance(rel, str) or not rel:
        raise FsError("a relative path is required", status=422)
    if rel.startswith("/") or rel.startswith("\\"):
        raise FsError("an upload path must be relative", status=422)
    if _BAD_CHARS.search(rel):
        raise FsError("an upload path must not contain control characters", status=422)
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH:
        raise FsError(f"that path is nested deeper than {MAX_DEPTH} folders", status=422)
    for p in parts:
        if p in ("", ".", ".."):
            raise FsError("an upload path must not contain traversal segments", status=422)
        if "\\" in p:
            raise FsError("an upload path must not contain backslashes", status=422)
        if len(p.encode("utf-8")) > MAX_COMPONENT:
            raise FsError("one of those names is too long", status=422)
        if p == ".git":
            raise FsError("uploads may not write into git metadata", status=403)
    # A directory is a git repository to git — and to `_looks_like_gitdir` — only if it holds a
    # **HEAD file**. Refusing to ever create one is therefore not a heuristic: it makes the
    # gitdir shape *uncompletable by upload*, structurally, with no directory state consulted
    # and so nothing to race. That is what lets the serialising fence go entirely; a lock that
    # has to win a race is strictly weaker than an operation that cannot start one.
    #
    # Deliberately the LAST component only, and exact-case. A *directory* named HEAD blocks
    # `isfile(HEAD)` rather than enabling it, and `HEAD.md` or `head` are ordinary files.
    if parts[-1] == "HEAD":
        raise FsError(
            "uploads may not create a file named HEAD — it would make this folder look like a "
            "git repository",
            status=403,
        )
    return parts


#: The two DIRECTORY parts of a gitdir. `HEAD` is the third and can never be uploaded at all
#: (`validate_relpath`), so these are the names that matter once a HEAD file already exists.
_GITDIR_DIRS = ("objects", "refs")


def _looks_like_gitdir(path: str) -> bool:
    """A directory carrying a gitdir's own shape, whatever it happens to be called.

    A **bare** repository (`repo.git/`, or any directory that simply *is* a gitdir) holds `hooks/`
    and `config` under a name no `.git` comparison will ever see.
    """
    try:
        return (
            os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects"))
            and os.path.isdir(os.path.join(path, "refs"))
        )
    except OSError:
        return False


def _inside(child: str, parent: str) -> bool:
    if not parent:
        return False
    parent = parent.rstrip(os.sep)
    return child == parent or child.startswith(parent + os.sep)


def refuse_completed_gitdir(dest_dir: str) -> None:
    """Refuse an upload that has just turned an ordinary directory INTO git metadata.

    :func:`refuse_git_metadata` runs *before* the write and can only see what already exists, so
    it is blind to the shape a client assembles one file at a time. Reproduced on the public
    route: upload ``HEAD``, then ``objects/placeholder``, then a dangerous ``config`` and
    ``hooks/pre-commit``, then ``refs/heads/main`` — every request is individually innocent, and
    the last one completes a gitdir wrapped around a config that was already sitting there.

    So the check runs again *after* the bytes land and *before* they are published: the upload
    that completes the shape is the one refused, and its file never becomes visible. Callers hold
    the destination's :func:`dir_lock` across write-and-publish, so two uploads cannot each supply
    the other's missing component and both pass.
    """
    cur = dest_dir
    root = home_root()
    while True:
        if _looks_like_gitdir(cur):
            raise FsError(
                "that upload would complete a git repository's metadata directory, so it was "
                "refused and nothing was written",
                status=403,
            )
        if cur == root or not _inside(cur, root):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent


def refuse_git_metadata(target_dir: str, parts: list[str]) -> None:
    """Refuse a target that lands inside git metadata — **discovered**, not spelled.

    Three shapes, each with its own adversarial test: a plain `.git/` (already caught by the name
    filter, kept for defence in depth), a gitdir reached through a `.git` *file*, and a bare
    repository. The linked-worktree common-dir is covered by testing both `gitdir` and `common()`.

    If a later decision narrows this, the security claim narrows with it in writing — it is not
    left to a comparison that quietly stopped matching.
    """
    # The deepest directory this upload would write into, as a path (it need not exist yet).
    dest_dir = os.path.join(target_dir, *parts[:-1]) if len(parts) > 1 else target_dir
    repo = discover_repo(target_dir)
    if repo is not None:
        for meta in (repo.gitdir, repo.common()):
            if meta and (_inside(dest_dir, meta) or _inside(target_dir, meta)):
                raise FsError("uploads may not write into git metadata", status=403)
    # Walk from the destination up to the contained root looking for a gitdir SHAPE — this is
    # what catches a bare repository that no worktree references, so `discover_repo` never saw it.
    root = home_root()
    cur = dest_dir
    while True:
        if _looks_like_gitdir(cur):
            raise FsError("uploads may not write into a git repository's metadata", status=403)
        if cur == root or not _inside(cur, root):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    # A directory that already holds a HEAD file gains no further gitdir part from an upload.
    #
    # Refusing the HEAD *leaf* stops one being uploaded, but it does not stop a shape completing
    # where HEAD already exists by other means: two CONCURRENT uploads of `refs/heads/main` and
    # `refs/tags/x` both create the shared `refs/`, both are then refused by the completion check,
    # and neither rollback can remove `refs/` — the first does not own it once the sibling's leaf
    # is inside, and the second never created it. `refs/` survives, the shape is complete, and any
    # pre-planted `config`/`hooks/` become live. Reproduced by the reviewer with a synchronised
    # two-thread probe.
    #
    # This refuses BEFORE any component is created, and reads only state that neither request
    # mutates — the presence of HEAD. Both racers therefore observe the same fact and both refuse,
    # so there is no interleaving to serialise and nothing is left behind to clean up. That is why
    # it is a check rather than the lock this module deleted, which lost three review rounds.
    prospective = target_dir
    for comp in parts[:-1]:
        if comp in _GITDIR_DIRS and os.path.isfile(os.path.join(prospective, "HEAD")):
            raise FsError(
                f"that folder already contains a git HEAD file, so uploading {comp!r} into it "
                "would turn it into a git repository",
                status=403,
            )
        prospective = os.path.join(prospective, comp)


# --------------------------------------------------------------------------- the write


def _open_dir(path: str) -> int:
    """A directory descriptor, proven to still be the contained path we resolved."""
    _require_capabilities()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FsError(
                "that folder is a symlink — uploads do not follow one", status=422
            ) from None
        if e.errno == errno.ENOENT:
            raise FsError("no such folder", status=404) from None
        if e.errno == errno.ENOTDIR:
            raise FsError("not a directory", status=422) from None
        if e.errno in (errno.EACCES, errno.EPERM):
            raise FsError("permission denied", status=403) from None
        raise FsError(f"could not open the folder: {e.strerror or e}", status=400) from None
    try:
        _fd_still_contained(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_symlink(dir_fd: int, name: str) -> bool:
    """Wording only — called after an open has already failed, never as a gate before one."""
    try:
        import stat as _stat

        return _stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def _descend(
    parent_fd: int, names: list[str]
) -> tuple[int, list[int], list[tuple[int, str, bool]]]:
    """Create and open each intermediate component **relative to the descriptor above it**.

    ``O_NOFOLLOW`` on every level is the whole mechanism: a component that already exists as a
    symlink fails the open, so the write is refused rather than redirected. There is no window
    between a check and a use because there is no check — only the open.
    """
    opened: list[int] = []
    #: (parent_fd, name, mine) for every intermediate component, innermost last. `mine` records
    #: whether THIS request created it — an ordinary rollback removes only its own, while a
    #: hostile-shape rollback may also retire a shared empty one (see `Destination.abandon`).
    created: list[tuple[int, str, bool]] = []
    cur = parent_fd
    try:
        for name in names:
            mine = True
            try:
                os.mkdir(name, 0o755, dir_fd=cur)
            except FileExistsError:
                mine = False
            except OSError as e:
                raise FsError(f"could not create {name!r}: {e.strerror or e}", status=400) from None
            created.append((cur, name, mine))
            try:
                nfd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=cur,
                )
            except OSError as e:
                # MEASURED: `O_DIRECTORY | O_NOFOLLOW` on a symlink-to-a-directory raises
                # **ENOTDIR** on Linux, not ELOOP — so reporting ENOTDIR as "already exists and
                # is not a folder" told the operator the wrong thing about the one refusal that
                # matters most here. The open has already failed either way (containment never
                # depended on this branch); an `lstat` only decides the wording.
                if e.errno in (errno.ELOOP, errno.ENOTDIR) and _is_symlink(cur, name):
                    raise FsError(
                        f"{name!r} is a symlink — an upload never follows one", status=422
                    ) from None
                if e.errno == errno.ELOOP:
                    raise FsError(
                        f"{name!r} is a symlink — an upload never follows one", status=422
                    ) from None
                if e.errno == errno.ENOTDIR:
                    raise FsError(
                        f"{name!r} already exists and is not a folder", status=409
                    ) from None
                raise FsError(f"could not open {name!r}: {e.strerror or e}", status=400) from None
            _fd_still_contained(nfd)
            opened.append(nfd)
            cur = nfd
        return cur, opened, created
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise


#: Ceiling on a file this panel is willing to REPLACE. Beyond it, identity cannot be established
#: cheaply enough to be safe, so replace is refused rather than approximated: hashing a prefix and
#: trusting size+mtime for the tail let an in-place rewrite past the prefix through untouched
#: (measured, on an 8 MiB + 8 byte file). Refusing is the honest end of that trade — the operator
#: can replace it in the session, where nothing is pretending to have checked.
_REPLACE_MAX = 64 * 1024 * 1024


def _identify(dir_fd: int, name: str) -> tuple | None:
    """A fingerprint of the file at ``name``, or ``None`` if it is not there.

    Inode, size, mtime **and a content digest**. The digest is the part that matters: an in-place
    rewrite preserving size and timestamp compares equal on the metadata alone, and the operator's
    "replace" decision would then be applied to a file they never saw.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if st.st_size > _REPLACE_MAX:
            raise FsError(
                f"{name!r} is too large for the panel to replace safely "
                f"({st.st_size // (1024 * 1024)} MB) — replace it in the session instead",
                status=413,
            )
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            h.update(chunk)
        # The WHOLE file. A prefix plus size+mtime is not an identity: an in-place rewrite past
        # the prefix preserves all three and compares equal.
        return (st.st_ino, st.st_size, st.st_mtime_ns, h.hexdigest())
    except OSError:
        return None
    finally:
        os.close(fd)


def _unique_name(dir_fd: int, name: str) -> str:
    """`report.pdf` -> `report (2).pdf`. Bounded, and every attempt is still an `O_EXCL` create."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(2, 1000):
        cand = f"{stem} ({n}){'.' + ext if ext else ''}"
        try:
            os.stat(cand, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return cand
    raise FsError("could not find a free name for that file", status=409)


class Destination:
    """An open destination descriptor plus what it takes to publish or abandon it."""

    def __init__(
        self,
        fd: int,
        dir_fd: int,
        name: str,
        final: str,
        dirs: list[int],
        base_fd: int,
        path: str = "",
        expect: tuple | None = None,
        created: list[tuple[int, str, bool]] | None = None,
    ):
        self.fd = fd
        self.dir_fd = dir_fd
        self.name = name
        self.final = final
        #: Absolute path the bytes will be visible at once published — built from the CONTAINED
        #: base plus validated components, never echoed back from the client's own `dir` string.
        self.path = path
        self._dirs = dirs
        self._base_fd = base_fd
        #: (inode, mtime_ns, size) of the file this upload is REPLACING, as it was when the
        #: collision was detected. Re-checked immediately before the rename.
        self.expect = expect
        #: The directory the bytes land in — what the post-write gitdir check walks up from.
        self.dest_dir = os.path.dirname(path)
        #: Every intermediate component this upload traversed, innermost last, each flagged with
        #: whether this request created it.
        self._created = created or []
        self.closed = False

    def write(self, data: bytes) -> None:
        """Write **all** of `data`, or fail.

        `os.write()` is permitted to accept fewer bytes than it was given, and ignoring the
        returned count is how a partial write becomes a TRUNCATED FILE PUBLISHED AS SUCCESS —
        reproduced with a short first write, which published three bytes of an eight-byte upload
        and reported 200. A loop over a memoryview is the fix; zero progress is a failure rather
        than an infinite loop.
        """
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            if n <= 0:
                raise OSError(errno.EIO, "the file system accepted no bytes")
            view = view[n:]

    def publish(self) -> None:
        """Make the bytes visible under their final name, atomically for a replace.

        A `replace` **revalidates the target it is replacing**. The operator chose "replace"
        against a specific file, but streaming takes time and this panel is docked into a session
        an agent is actively working in — so the agent can rewrite that file while the bytes are
        still arriving, and an unconditional rename would silently destroy work that appeared
        *after* the decision. The identity captured at collision time is re-checked here, and a
        target that moved on is a refusal rather than a stale choice applied anyway.
        """
        if self.name != self.final:
            if self.expect is not None:
                now = _identify(self.dir_fd, self.final)
                if now is None:
                    raise FsError(
                        f"{self.final!r} disappeared while it was being replaced — nothing was "
                        "overwritten",
                        status=409,
                    )
                if now != self.expect:
                    raise FsError(
                        f"{self.final!r} changed while it was being uploaded, so it was NOT "
                        "replaced. Check what changed and upload again if you still mean to.",
                        status=409,
                    )
            # `replace` never REOPENS the path: the bytes went to a temp name in the SAME
            # directory through the same descriptor, and this rename publishes them. There is no
            # moment between writing and publishing where the target can be redirected.
            os.rename(self.name, self.final, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        self._close()

    def abandon(self) -> None:
        """Remove the partial file **through the parent descriptor**, never by path.

        Order is load-bearing and was wrong once: closing first shut `dir_fd` (which for a
        top-level upload IS the base descriptor), so the `unlink` that followed ran against a
        closed fd, failed EBADF, and was swallowed — leaving a refused upload's bytes sitting at
        the destination. Unlink first, close after.
        """
        if not self.closed:
            try:
                os.close(self.fd)
            except OSError:
                pass
            try:
                os.unlink(self.name, dir_fd=self.dir_fd)
            except OSError:
                pass
        # Undo the directories, innermost first — BEFORE `_close`, because `_close` shuts exactly
        # the descriptors these `rmdir`s are relative to. Doing it after meant every call failed
        # EBADF and broke out on the first iteration. (Same ordering mistake as the unlink above,
        # made twice in this file: a parent descriptor has to outlive everything relative to it.)
        #
        # ONLY what this request created. An earlier version also retired *shared* empty
        # components on the gitdir-refusal path, to stop two siblings leaving a completed shape
        # between them — but that deleted the operator's own pre-existing empty directories in a
        # reachable interleaving (a pre-existing `refs/heads` was removed by a refusal that never
        # made it). Ownership-blind cleanup is the wrong tool; the interleaving is prevented
        # instead, by the fence in `metadata_fence_key`.
        for parent_fd, name, mine in reversed(self._created):
            if not mine:
                break
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                break  # non-empty or gone: stop, the rest are its ancestors
        self._close(file_fd_already_closed=True)

    def _close(self, *, file_fd_already_closed: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        fds = list(reversed(self._dirs))
        if not file_fd_already_closed:
            fds.insert(0, self.fd)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if self._base_fd not in self._dirs:
            try:
                os.close(self._base_fd)
            except OSError:
                pass


def open_destination(dir_path: object, relpath: object, on_collision: object) -> Destination:
    """Resolve, contain, refuse, create. Nothing is written until this returns."""
    if not isinstance(dir_path, str) or not dir_path.strip():
        raise FsError("a target folder is required", status=422)
    mode = on_collision if isinstance(on_collision, str) else "fail"
    if mode not in ("fail", "keep_both", "replace"):
        raise FsError("unknown collision mode", status=422)
    base = contained_path(dir_path)
    parts = validate_relpath(relpath if isinstance(relpath, str) else "")
    refuse_git_metadata(base, parts)
    # The editor's recovery store holds journals its save path acts on (#950), so an upload must
    # never create one. Imported here because `fileedit` imports this module.
    from . import fileedit

    fileedit.refuse_recovery_store(base, parts)

    base_fd = _open_dir(base)
    dirs: list[int] = []
    created: list[tuple[int, str, bool]] = []
    expect: tuple | None = None
    try:
        dir_fd, dirs, created = _descend(base_fd, parts[:-1])
        leaf = parts[-1]
        final = leaf
        name = leaf
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
        except FileExistsError:
            if mode == "fail":
                raise FsError(f"{leaf!r} already exists here", status=409) from None
            if mode == "keep_both":
                final = name = _unique_name(dir_fd, leaf)
                fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
            else:
                # `replace`: a temp sibling in the same directory, published by `rename` below.
                # The target's identity is captured NOW, so `publish` can tell whether the file
                # the operator decided about is still the file it is about to overwrite.
                # (inode, mtime_ns, size) is not enough: an agent rewriting the file IN PLACE
                # can preserve all three where timestamps are coarse or deliberately restored,
                # and the stale choice would then overwrite newer work anyway. The content digest
                # is what actually answers "is this still the file they decided about".
                expect = _identify(dir_fd, leaf)
                if expect is None:
                    # FAIL CLOSED. `None` previously meant both "no identity available" and "no
                    # expectation to check", so `publish()` skipped its check entirely — and the
                    # target can genuinely vanish between the `O_EXCL` collision and this call,
                    # after which a newer file created while bytes stream would be silently
                    # renamed over. A replace that cannot establish what it is replacing is not a
                    # replace.
                    raise FsError(
                        f"{leaf!r} changed while the panel was reading it, so it was NOT "
                        "replaced. Try again.",
                        status=409,
                    ) from None
                name = f".{leaf}.upload-{secrets.token_hex(6)}"
                fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise FsError(
                    f"{leaf!r} already exists as a symlink — an upload never follows one",
                    status=422,
                ) from None
            if e.errno in (errno.EACCES, errno.EPERM):
                raise FsError("permission denied", status=403) from None
            if e.errno == errno.EISDIR:
                raise FsError(f"{leaf!r} already exists as a folder", status=409) from None
            raise FsError(f"could not create the file: {e.strerror or e}", status=400) from None
        try:
            # The mode argument is masked by the umask, so it can only ever land TIGHTER than
            # asked. `fchmod` pins it before a byte is written — and never with an executable bit.
            os.fchmod(fd, FILE_MODE & ~_umask())
        except OSError:
            pass
        return Destination(
            fd,
            dir_fd,
            name,
            final,
            dirs,
            base_fd,
            os.path.join(base, *parts[:-1], final),
            expect,
            created,
        )
    except BaseException:
        for fd in dirs:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(base_fd)
        except OSError:
            pass
        raise


_umask_cache: int | None = None
_umask_lock = threading.Lock()


def _umask() -> int:
    """The process umask, read WITHOUT changing it.

    The usual `os.umask(x)` then `os.umask(back)` dance is a process-global mutation with a real
    window: any other thread creating a file in between gets the wrong mode. This app is
    thread-pooled and the file panel is one of the pools, so that window is reachable. Linux
    exposes the value directly in `/proc/self/status`; the swap is only the fallback for a kernel
    that does not, and it is cached either way so it happens at most once.
    """
    global _umask_cache
    with _umask_lock:
        if _umask_cache is None:
            try:
                with open("/proc/self/status") as fh:
                    for line in fh:
                        if line.startswith("Umask:"):
                            _umask_cache = int(line.split()[1], 8)
                            break
            except OSError:
                pass
        if _umask_cache is None:  # pragma: no cover - non-Linux fallback
            cur = os.umask(0o022)
            os.umask(cur)
            _umask_cache = cur
        return _umask_cache


def disk_full(e: OSError) -> bool:
    return e.errno in (errno.ENOSPC, errno.EDQUOT)


__all__ = [
    "BATCH_IDLE_TTL_S",
    "FILE_MODE",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_FILES",
    "MAX_DEPTH",
    "MAX_FILE_BYTES",
    "MAX_LIVE_BATCHES",
    "MAX_LIVE_BATCHES_TOTAL",
    "Batch",
    "BatchError",
    "Destination",
    "create_batch",
    "disk_full",
    "get_batch",
    "implicit_batch",
    "open_destination",
    "refuse_completed_gitdir",
    "refuse_git_metadata",
    "reset_batches_for_test",
    "validate_relpath",
]
