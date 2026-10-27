"""HQ conversation attachments (#1707) — the blobs behind files shared in the chat.

The conversation's JSONL stays text-only (:mod:`ask_store`); a shared file is a **blob** on disk
beside it, and a `user` record carries only its descriptor (``id``, ``name``, ``kind``, ``size``).

    <ask-root>/attachments/<cid>/<aid>       one blob, 0600, <aid> a server-made UUID hex
    <ask-root>/attachments/<cid>/.lock       the attachments FENCE (below)
    <ask-root>/attachments/<cid>/…partial    never visible: a cancelled upload reaps it

**Kinds — decided from the bytes, never the client's name or MIME** (``classify``): ``image``
(sniffed png/jpeg/gif/webp signature — the only kind rendered inline), ``text`` (decodes as UTF-8
with no NULs and is not an HTML-markup head; shown as a text excerpt and served ``text/plain`` —
an SVG lands here and is only ever shown *as text*, never rendered), and ``other`` (everything
else, HTML markup included; served strictly as a download). A fake ``image/x.png`` carrying
HTML classifies ``other`` and can never execute.

**The fence.** Upload publish, conversation delete and retention's reap share ONE per-conversation
lock (``attachments_fence``). An upload holds it across the publish step and re-checks, **inside
the fence**, that the conversation still exists immediately before publishing — so a delete
interleaved with an in-flight upload can never leave an orphan directory published after deletion.
Delete takes the same fence while removing the whole directory.

**Bounds.** 10 MiB per file, at most 4 staged at once, 32 MiB aggregate per conversation (staged +
sent); every refusal names the cap. A staged blob no user record claims is reaped after
:data:`STAGED_TTL` (24 h). Blobs die with their conversation — there is no second lifecycle.

**Prompt boundary — fail-closed.** Blob bytes and file names reach a model request ONLY through
:func:`prompt_block` / :func:`prompt_note` (:mod:`ask_orchestrator`'s ``_user_content`` and
``_history`` both call it). That sanitizer redacts known template-secret values and withholds a
credential-shaped name or body **whole**; if the sanitizer cannot run, the model gets a fixed
count/kind/size note with **no raw filename and no bytes** — never a partial or unscrubbed echo.
The JSONL admission step never sees blob bytes and is never relied on for them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import ask_store, template_secrets, template_suggest

log = logging.getLogger(__name__)

#: Per-file cap. Refused before the bytes are accepted (the ingest charges each chunk first).
MAX_FILE_BYTES = 10 * 1024 * 1024
#: Staged blobs a conversation may hold waiting for their message.
MAX_STAGED = 4
#: Aggregate per conversation, staged + sent. Refusals name it.
MAX_TOTAL_BYTES = 32 * 1024 * 1024
#: How long a staged blob no message claimed stays around.
STAGED_TTL = 24 * 3600
#: The stored NAME is metadata: capped like a title, never a path component.
NAME_MAX = 200
#: Bytes of a text blob read for the model, per file.
PROMPT_TEXT_MAX = 6 * 1024

AID_RE = re.compile(r"^[0-9a-f]{32}$")
KINDS = frozenset({"image", "text", "other"})

#: Sniffed signatures for the inline-preview kind. SUFFIXES AND CLIENT MIME ARE NEVER ASKED.
_IMAGE_SIGS: tuple[tuple[bytes, bytes], ...] = (
    (b"\x89PNG\r\n\x1a\n", b""),
    (b"\xff\xd8\xff", b""),
    (b"GIF87a", b""),
    (b"GIF89a", b""),
    (b"RIFF", b"WEBP"),  # RIFF....WEBP
)


class AttachmentRefused(Exception):
    """The upload or claim is refused; ``detail`` is the operator-facing reason (names the cap)."""

    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def valid_aid(value: object) -> bool:
    return isinstance(value, str) and bool(AID_RE.match(value))


def new_aid() -> str:
    return uuid.uuid4().hex


def clean_name(value: object) -> str:
    """The display name as stored: never a path, never empty, capped. Path separators and control
    bytes are stripped so a name can only ever be a NAME (ids carry the location)."""
    if not isinstance(value, str):
        return "file"
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in "\r\n\t")
    name = name.strip().lstrip(".")
    return name[:NAME_MAX] or "file"


def classify(data: bytes) -> str:
    """`image` / `text` / `other`, from ALL the BYTES alone — and ONLY here.

    This runs exactly once per blob, at publish, on the complete file; the result is persisted in
    the blob's sidecar and read back by claim, prompt and retrieval. Re-classifying from a prefix
    (16 bytes at GET, 8 KiB at claim) disagreed with the full-file verdict — a binary whose first
    8 KiB is ASCII read `text` when sent and reached the model as text — so it is never done.

    An SVG decodes as UTF-8 text and classifies `text` — it is shown as a text excerpt and served
    ``text/plain``, so it can never execute. A file named ``.png`` that carries HTML is `other`:
    HTML-markup heads are the one thing never offered as a viewable text, so the only disposition
    they can ever earn is a download. A NUL **anywhere** means `other`, whatever the name.
    """
    head = data[:16]
    for sig, tail in _IMAGE_SIGS:
        if head.startswith(sig) and (not tail or data[8:12] == tail):
            return "image"
    if b"\x00" in data:
        return "other"
    # ONE decode of the whole bounded file. A prefix decode (`data[:256].decode`) rejected a
    # multibyte character split at byte 256 that the full decode accepts — valid UTF-8 lost its
    # View/text-plain path (#1720 round 3).
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "other"
    stripped = text.lstrip().lower()
    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        return "other"
    return "text"


def attachments_dir(cid: str) -> Path:
    return ask_store.root() / "attachments" / cid


def _ensure_dir(path: Path) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    # Correct a permissive umask on the way in: the store is single-admin and its blobs too.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def ensure_root() -> Path:
    """``<ask-root>/attachments`` as a 0700 directory, created or corrected."""
    d = ask_store.root() / "attachments"
    _ensure_dir(ask_store.root())
    _ensure_dir(d)
    return d


def _lock_path(cid: str) -> Path:
    # OUTSIDE the blob dir it protects: if the lock lived inside, taking the fence would
    # re-create a directory a concurrent delete had just removed (a refused publish would then
    # "resurrect" the conversation's attachment dir). The dir exists only while it holds blobs.
    return ensure_root() / ".locks" / f"{cid}.lock"


@contextlib.contextmanager
def attachments_fence(cid: str) -> Iterator[None]:
    """THE shared fence: publish, delete and reap run inside it. Blocking, short."""
    if not ask_store.valid_cid(cid):
        raise ask_store.ConversationNotFound(cid)
    locks = ensure_root() / ".locks"
    _ensure_dir(locks)
    fd = os.open(_lock_path(cid), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _blob_path(cid: str, aid: str) -> Path:
    if not (ask_store.valid_cid(cid) and valid_aid(aid)):
        raise AttachmentRefused("malformed attachment id")
    return attachments_dir(cid) / aid


def list_blobs(cid: str) -> list[tuple[str, int, float]]:
    """``(aid, size, mtime)`` for each LIVE blob in the conversation's dir.

    Two kinds of on-disk file are NOT live and never count toward quota or claims (#1720
    round 9): a **tombstoned** blob (removed — awaiting its unlink) and a sidecar-less blob
    (an INCOMPLETE write whose metadata never landed). The fenced sweeps finish both."""
    out: list[tuple[str, int, float]] = []
    d = attachments_dir(cid)
    with contextlib.suppress(FileNotFoundError):
        for e in os.scandir(d):
            if not e.is_file(follow_symlinks=False) or not valid_aid(e.name):
                continue
            if is_taken_back(cid, e.name):
                continue
            if not (d / f"{e.name}.meta").exists():
                continue
            st = e.stat(follow_symlinks=False)
            out.append((e.name, st.st_size, st.st_mtime))
    return out


def total_bytes(cid: str) -> int:
    """EVERY physical byte in the conversation's attachment dir — including tombstoned and
    sidecar-less files still awaiting their unlink (#1720 round 10).

    `list_blobs` is the claimable/staged view (logically removed files do not hold a STAGED
    SLOT); this is the DISK bound. The two must not collapse into one: a removal whose unlink
    keeps failing releases the slot but its bytes stay on disk, and without counting them the
    32 MiB cap stops bounding storage at all — every failed-removal cycle would grow the dir
    forever. New uploads are refused while stale bytes still hold the cap."""
    d = attachments_dir(cid)
    total = 0
    with contextlib.suppress(FileNotFoundError):
        for e in os.scandir(d):
            if e.is_file(follow_symlinks=False):
                total += e.stat(follow_symlinks=False).st_size
    return total


def claimed_ids(records: list[dict]) -> set[str]:
    """Attachment ids some `user` record already carries — an id is claimed by exactly one turn."""
    out: set[str] = set()
    for r in records:
        for a in r.get("attachments") or []:
            if isinstance(a, dict) and valid_aid(a.get("id")):
                out.add(a["id"])
    return out


def publish(cid: str, aid: str, name: str, data: bytes) -> dict:
    """Store one blob and answer its descriptor. Blocking.

    The conversation-existence check runs **inside** the fence, immediately before the publish —
    never after the fence is released — so a concurrent ``delete_conversation`` either removes the
    blob afterwards or is refused because the upload holds the fence. A refusal leaves no file.
    """
    if not valid_aid(aid):
        raise AttachmentRefused("malformed attachment id")
    if not data:
        raise AttachmentRefused("an empty file cannot be shared")
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentRefused(
            f"that file is larger than the limit ({MAX_FILE_BYTES // (1024 * 1024)} MB)",
            status=413,
        )
    kind = classify(data)
    name = clean_name(name)
    with attachments_fence(cid):
        # INSIDE the fence: the conversation must still exist right before the publish.
        if not ask_store.exists(cid):
            raise ask_store.ConversationNotFound(cid)
        # The staged cap is enforced HERE, not at claim: a fifth file no message has taken is
        # refused before it lands (the operator sends first). Sent blobs do not count.
        try:
            staged = [b for b in list_blobs(cid) if b[0] not in claimed_ids(ask_store.read(cid))]
        except (ask_store.StoreRefused, ask_store.ConversationNotFound):
            staged = list_blobs(cid)  # unreadable records: the conservative count
        if len(staged) >= MAX_STAGED:
            raise AttachmentRefused(
                f"at most {MAX_STAGED} files can wait for a message — send them first",
                status=413,
            )
        if total_bytes(cid) + len(data) > MAX_TOTAL_BYTES:
            raise AttachmentRefused(
                f"this conversation would pass its {MAX_TOTAL_BYTES // (1024 * 1024)} MB "
                "of shared files",
                status=413,
            )
        # The dir is created only now — every refusal above leaves no directory behind.
        _ensure_dir(attachments_dir(cid))
        dest = _blob_path(cid, aid)
        tmp = attachments_dir(cid) / f"{aid}.partial"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if not n:
                    raise OSError("attachment write made no progress")
                view = view[n:]
            os.fsync(fd)
        except BaseException:
            # A refused or failed write leaves nothing behind — the partial goes first.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        os.replace(tmp, dest)
        try:
            write_meta(cid, aid, name, kind)
        except OSError:
            # Roll BOTH artifacts back (#1720 round 8): a blob whose sidecar never landed is an
            # invisible staged slot — four of them eat the whole quota while the composer only
            # reports failed uploads. The sidecar-less blob is never left behind.
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            if dest.exists():
                # The rollback's own unlink failed too: the tombstone is the fallback, so the
                # file is at least logically gone and reaped later (#1720 round 9).
                with contextlib.suppress(OSError):
                    fd = os.open(
                        _tombstone(cid, aid),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                    )
                    os.close(fd)
            with contextlib.suppress(OSError):
                (attachments_dir(cid) / f"{aid}.meta").unlink(missing_ok=True)
            raise
    return {"id": aid, "name": name, "kind": kind, "size": len(data)}


def claim(cid: str, aids: list[str], records: list[dict]) -> list[dict]:
    """Validate that every id is an unclaimed blob of THIS conversation; answer its descriptors.

    The CALLER holds the attachments fence across this AND the append that records the claim
    (``ask_orchestrator.begin`` wraps both) — that is what makes a discard unable to slip
    between the check and the record (#1720 rounds 3 and 6). Under the turn lock by the same
    caller, so an id is claimed by exactly one turn or stays staged. Raises
    :class:`AttachmentRefused` for an unknown, foreign, taken-back or already-claimed id, and
    when the staged count or aggregate would be crossed.
    """
    if len(aids) > MAX_STAGED:
        raise AttachmentRefused(f"at most {MAX_STAGED} files per message")
    claimed = claimed_ids(records)
    out: list[dict] = []
    seen: set[str] = set()
    for aid in aids:
        if not valid_aid(aid):
            raise AttachmentRefused("malformed attachment id")
        if aid in seen:
            continue
        seen.add(aid)
        if aid in claimed:
            raise AttachmentRefused("that file was already sent in another message")
        if is_taken_back(cid, aid):
            raise AttachmentRefused("that file was taken back")
        path = _blob_path(cid, aid)
        try:
            st = path.stat()
        except FileNotFoundError:
            raise AttachmentRefused("that file is not in this conversation") from None
        if not stat.S_ISREG(st.st_mode):
            raise AttachmentRefused("that file is not in this conversation")
        # Name and kind are what PUBLISH persisted beside the blob — the one full-file
        # classification. Claiming from a prefix disagreed with it (a binary whose first 8 KiB is
        # ASCII read `text` when sent); that is never done again.
        if not (attachments_dir(cid) / f"{aid}.meta").exists():
            raise AttachmentRefused("that upload did not finish")
        name, kind = read_meta(cid, aid)
        out.append({"id": aid, "name": name, "kind": kind, "size": st.st_size})
    return out


def write_meta(cid: str, aid: str, name: str, kind: str) -> None:
    """Persist the blob's display name and its ONE classification beside it (publish-time; 0600,
    never on the blob's path). Everything downstream reads this and never re-classifies a
    prefix — see :func:`classify`."""
    meta = attachments_dir(cid) / f"{aid}.meta"
    fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps({"name": clean_name(name), "kind": kind}).encode("utf-8"))
    finally:
        os.close(fd)


def read_meta(cid: str, aid: str) -> tuple[str, str]:
    """``(name, kind)`` as publish persisted them. A sidecar that cannot be read is the SAFE
    fallback: `a shared file`, `other` — a blob with no proven classification is never offered
    as an image or a text preview."""
    meta = attachments_dir(cid) / f"{aid}.meta"
    try:
        raw = json.loads(meta.read_bytes()[: 4 * 1024])
    except (OSError, ValueError):
        return "file", "other"
    name = clean_name(raw.get("name")) if isinstance(raw, dict) else "file"
    kind = raw.get("kind") if isinstance(raw, dict) else None
    return (name if name != "file" else "file"), (kind if kind in KINDS else "other")


def read_text_bodies(cid: str, descriptors: list[dict]) -> dict[str, bytes]:
    """The TEXT bodies for a turn's attachments (id → bytes) — the WHOLE file, bounded by the
    store's own :data:`MAX_FILE_BYTES`.

    The whole file is load-bearing: `prompt_block` sanitizes before it cuts, and a body cut
    upstream let a secret spanning the read boundary keep its prefix in the prompt (#1720 round
    3). A missing or unreadable blob contributes nothing — the descriptor still stands."""
    out: dict[str, bytes] = {}
    for a in descriptors:
        if a.get("kind") != "text" or not valid_aid(a.get("id")):
            continue
        with contextlib.suppress(OSError, AttachmentRefused):
            fd, _size = open_blob(cid, a["id"])
            try:
                chunks: list[bytes] = []
                got = 0
                while got <= MAX_FILE_BYTES:
                    chunk = os.read(fd, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                out[a["id"]] = b"".join(chunks)[:MAX_FILE_BYTES]
            finally:
                os.close(fd)
    return out


def open_blob(cid: str, aid: str) -> tuple[int, int]:
    """``(fd, size)`` on the blob, opened under the conversation's dir without following links.

    The conversation must still EXIST: a blob left behind by a failed cleanup (or a crash between
    the JSONL unlink and the blob removal) is never retrievable through the old URL — deletion
    holds even when the cleanup does not finish."""
    if not (ask_store.valid_cid(cid) and valid_aid(aid)):
        raise AttachmentRefused("malformed attachment id")
    if not ask_store.exists(cid):
        raise AttachmentRefused("that file is gone", status=404)
    if is_taken_back(cid, aid):
        # Removed — and the read must refuse even while the bytes await their unlink
        # (#1720 round 9): acknowledge-then-still-downloadable is a retention breach.
        raise AttachmentRefused("that file is gone", status=404)
    try:
        dir_fd = os.open(attachments_dir(cid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError, OSError):
        raise AttachmentRefused("that file is gone", status=404) from None
    try:
        fd = os.open(aid, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        raise AttachmentRefused("that file is gone", status=404) from None
    except OSError:
        raise AttachmentRefused("that file is gone", status=404) from None
    finally:
        os.close(dir_fd)
    return fd, os.fstat(fd).st_size


def delete_for_conversation(cid: str) -> None:
    """Remove a conversation's whole attachment directory — called INSIDE the fence by delete and
    sweep, together with the records. Best effort: a blob that will not go now is an orphan the
    next sweep removes. The fence's own lock file goes last (safe while it is held: the lock is on
    the inode; a later opener makes a fresh one and re-checks existence), so an explicit delete
    leaves NOTHING named after the conversation."""
    d = attachments_dir(cid)
    with contextlib.suppress(OSError):
        for e in os.scandir(d):
            with contextlib.suppress(OSError):
                Path(e.path).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        d.rmdir()
    with contextlib.suppress(OSError):
        _lock_path(cid).unlink(missing_ok=True)


def _tombstone(cid: str, aid: str) -> Path:
    return attachments_dir(cid) / f"{aid}.gone"


def is_taken_back(cid: str, aid: str) -> bool:
    """The operator removed this staged file: it is logically GONE even if the bytes are still
    on disk waiting for a free fence (#1720 round 6)."""
    return _tombstone(cid, aid).exists()


def complete_tombstones(cid: str) -> int:
    """Physically remove every taken-back blob. Called only under the attachments fence —
    which also encloses every claim+append, so an unlink can never land inside one. A reload or
    a closed tab can never strand a removal: the next thing anyone does in this conversation
    finishes it.

    FAIL CLOSED (#1720 round 7): the marker is retired only after the blob is CONFIRMED gone.
    A failed blob unlink keeps the marker (a later pass retries) — never a state where the
    bytes are still there and no obligation to remove them remains."""
    gone = 0
    for t in list(attachments_dir(cid).glob("*.gone")):
        aid = t.name[: -len(".gone")]
        blob = _blob_path(cid, aid)
        meta = attachments_dir(cid) / f"{aid}.meta"
        try:
            blob.unlink(missing_ok=True)
            # The SIDECAR carries the filename — it must go too before the marker retires
            # (#1720 round 8): a transient meta-unlink failure used to drop the marker with
            # the name still on disk and no obligation left to remove it.
            meta.unlink(missing_ok=True)
        except OSError:
            continue  # the marker stays: the removal is still owed
        with contextlib.suppress(OSError):
            t.unlink(missing_ok=True)
        gone += 1
    # Incomplete writes (a blob whose sidecar never landed) are garbage: swept here on every
    # fenced pass so a rollback that could not unlink either artifact is still finished later.
    with contextlib.suppress(FileNotFoundError):
        for e in os.scandir(attachments_dir(cid)):
            if not e.is_file(follow_symlinks=False) or not valid_aid(e.name):
                continue
            if is_taken_back(cid, e.name) or (attachments_dir(cid) / f"{e.name}.meta").exists():
                continue
            with contextlib.suppress(OSError):
                Path(e.path).unlink(missing_ok=True)
                gone += 1
    # Orphan SIDECARS (#1720 round 11): a `.meta` whose blob is gone — e.g. a TTL reap whose
    # sidecar unlink failed once — still holds the private filename. The reap discovers
    # candidates through `list_blobs`, which cannot see it any more, so nothing would ever
    # retry: this sweep is that retry.
    with contextlib.suppress(FileNotFoundError):
        for e in os.scandir(attachments_dir(cid)):
            if not (e.is_file(follow_symlinks=False) and e.name.endswith(".meta")):
                continue
            aid = e.name[: -len(".meta")]
            if valid_aid(aid) and (attachments_dir(cid) / aid).exists():
                continue
            with contextlib.suppress(OSError):
                Path(e.path).unlink(missing_ok=True)
                gone += 1
    return gone


def discard(cid: str, aid: str) -> str:
    """Take back a STAGED blob (Remove on a pill, or an upload whose pill went away).

    Returns the caller's answer (#1720 round 11): ``"discarded"`` (the removal is durable),
    ``"gone"`` (already taken back — nothing to do), ``"claimed"`` (sent history keeps it), or
    ``"retry"`` (the durable marker could NOT be written — the file is still staged and still
    counts, and the caller must keep the cleanup pending). A ``bool`` cannot say retryable
    failure apart from an already-settled one; the UI used to stop on both.

    Durable means a **tombstone** (``<aid>.gone``) written under the fence, not the unlink: a
    turn can hold the turn lock for minutes, and a removal that only lived in the page's retry
    loop stranded the blob's quota slot until the 24 h reap (#1720 round 6). With the tombstone
    down, the file is logically gone at once — `claim` refuses it and the staged count frees —
    and the bytes go whenever any later fence hold completes the tombstones. The tombstone and
    the claim check share the FENCE, so neither can slip between the other; a claimed id is
    never tombstoned."""
    if not (ask_store.valid_cid(cid) and valid_aid(aid)):
        return "gone"
    with attachments_fence(cid):
        if not ask_store.exists(cid):
            return "gone"
        if aid in claimed_ids(ask_store.read(cid)):
            return "claimed"
        if not _blob_path(cid, aid).exists() and not is_taken_back(cid, aid):
            return "gone"
        if not is_taken_back(cid, aid):
            # FAIL CLOSED (#1720 round 7): the removal is acknowledged only once its durable
            # marker is on disk. If the marker cannot be written the caller is told so and the
            # pill stays — never a silent drop of a file that is still staged and still counts.
            try:
                fd = os.open(
                    _tombstone(cid, aid),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(fd)
            except OSError:
                return "retry"
        complete_tombstones(cid)  # best case: the bytes go right now
        return "discarded"


def reap_staged(now: float | None = None) -> int:
    """Retention's attachment pass: orphan directories (conversation gone) and staged blobs past
    :data:`STAGED_TTL` that no user record claims. Returns how many blobs went."""
    now = time.time() if now is None else now
    ensure_root()
    root = ask_store.root() / "attachments"
    removed = 0
    with contextlib.suppress(FileNotFoundError):
        for e in os.scandir(root):
            cid = e.name
            if not e.is_dir(follow_symlinks=False) or not ask_store.valid_cid(cid):
                continue
            try:
                # Lock order matches delete: turn first, then the attachments fence. Holding the
                # TURN lock is what serializes this decision against `begin`'s claim+append —
                # without it a blob past its TTL could be deleted between a claim and the append
                # that makes it durable, leaving a settled turn pointing at a missing file.
                with ask_store.turn_lock(cid) as free:
                    if not free:
                        continue  # a turn is claiming/sending right now
                    with attachments_fence(cid):
                        complete_tombstones(cid)
                        if not ask_store.exists(cid):
                            delete_for_conversation(cid)
                            continue
                        try:
                            records = ask_store.read(cid)
                        except (ask_store.ConversationNotFound, ask_store.StoreRefused):
                            continue
                        claimed = claimed_ids(records)
                        for aid, _size, mtime in list_blobs(cid):
                            if aid in claimed or now - mtime < STAGED_TTL:
                                continue
                            with contextlib.suppress(OSError):
                                (_blob_path(cid, aid)).unlink(missing_ok=True)
                                (attachments_dir(cid) / f"{aid}.meta").unlink(missing_ok=True)
                                removed += 1
            except OSError as e2:
                log.warning("ask attachment reap failed (%s)", type(e2).__name__)
    return removed


# ---- the prompt boundary (fail-closed) ---------------------------------------------------------


def _withhold_name() -> str:
    return "a shared file"


def prompt_note(attachments: list[dict]) -> str:
    """The note a turn carries about its shared files.

    With a working sanitizer: names, kinds and sizes. On a sanitizer failure: a **fixed**
    count/kind/size note with no raw filename and no bytes — the fail-closed shape.
    """
    if not attachments:
        return ""
    try:
        with template_secrets.registration_fence():
            secrets = template_secrets.redaction_values()
            parts = []
            for a in attachments:
                name = template_secrets.redact_text(str(a.get("name") or "file"), secrets)
                if template_suggest.credential_shaped(name):
                    name = _withhold_name()
                parts.append(f"{name} ({a.get('kind')}, {_human(int(a.get('size') or 0))})")
        return (
            f"[Shared {len(attachments)} file{'s' if len(attachments) != 1 else ''}: "
            + "; ".join(parts)
            + "]"
        )
    except Exception:
        # The ONLY fallback: counts, kinds and sizes. No name the sanitizer did not clear.
        kinds: dict[str, int] = {}
        total = 0
        for a in attachments:
            k = a.get("kind") if a.get("kind") in KINDS else "other"
            kinds[k] = kinds.get(k, 0) + 1
            total += int(a.get("size") or 0)
        listed = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(kinds.items()))
        return f"[Shared {len(attachments)} files: {listed}, {_human(total)}]"


def prompt_block(attachment: dict, body: bytes | None) -> str:
    """One attachment as the model reads it: the note line, and for a TEXT blob a bounded fenced
    excerpt — both through the same fail-closed sanitizer. An image or `other` body is never
    included (no vision; nothing unrenderable is quoted).

    The sanitizer runs on the COMPLETE body BEFORE any truncation. Truncating first let a secret
    spanning the excerpt boundary keep its prefix in the prompt (its whole no longer matched the
    redactor, and the rest looked harmless) — a known-secret fragment reached the model. The
    order here is load-bearing: redact the full text, withhold on the full text, cut only after.
    """
    if body is None or attachment.get("kind") != "text":
        return prompt_note([attachment])
    try:
        with template_secrets.registration_fence():
            secrets = template_secrets.redaction_values()
            name = template_secrets.redact_text(str(attachment.get("name") or "file"), secrets)
            if template_suggest.credential_shaped(name):
                name = _withhold_name()
            full = template_secrets.redact_text(body.decode("utf-8", "replace"), secrets)
            if template_suggest.credential_shaped(full):
                return (
                    f"{prompt_note([attachment])} (contents withheld — they look like a credential)"
                )
        # Only the already-scrubbed text is cut: a secret can no longer span the cut.
        truncated = len(full) > PROMPT_TEXT_MAX
        text = full[:PROMPT_TEXT_MAX]
        if truncated:
            text += "\n…[truncated]"
        return f"{prompt_note([attachment])}\n```\n{text}\n```"
    except Exception:
        return prompt_note([attachment])


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"
