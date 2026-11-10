"""The mission shelf (#1740 P1a): everything a mission produced, laid out for review.

A **shelf item** is a server-owned row in the missions store (`mission_shelf_items`, schema v35),
deleted with its mission by the store's own cascade and physical scrub. It is one of three
shapes (`ref_type`):

* ``path`` — a regular file inside the mission's project folders, the configured roots and
  outside `folder_exclusions`, with the sha256 (and a stat fingerprint) taken from the verified
  descriptor at delivery;
* ``url`` — `http`/`https` only, no userinfo, never fetched by the server (no outbound request
  exists anywhere in this module);
* ``text`` — inline text, at most `TEXT_MAX` bytes of UTF-8.

**Delivering is the only way in for an agent** (`deliver`, called by `shelf_broker` with the
identity the broker mapped from a per-launch capability — never one the agent named). Everything is
judged here, synchronously, and a refusal carries a code the agent's tool turns into words
(`REFUSALS`): a path outside the mission's folders / the roots / under an exclusion, a final
symlink or a symlinked escape, an oversized file or text, content whose bytes do not match its
extension, a URL with credentials, credential-shaped text (withheld, never redacted, as memory
ingestion does). Titles and whys are one line, control characters stripped, length-bounded.

**Reading a file is mission-bound and stricter than `/api/files/raw`** (`open_item`): the route
takes no path, and every read re-proves membership (the item is this mission's), the folders,
the roots, the exclusions, no symlink, and a regular file on the opened descriptor. Media is
served inline only when its first bytes match its extension (`sniff_ok`); SVG is never served as
markup (a document, download-only); a text document is served as ``text/plain``. Everything else
is download-only.

**Integrity is checked when the shelf is read** (`integrity`): a file whose bytes no longer hash to
the delivered sha256 is *changed since delivered*, a gone one *missing*, one now outside the
boundary *inaccessible* — all three stay on the shelf. A matching stat fingerprint (device, inode,
size, mtime, ctime) skips the hash; a fingerprint is only recorded once the file has been quiet
for `SETTLE_S`, so a write in the same timestamp tick cannot hide behind it.

**The revision** rises on every add and every verdict, so the page re-reads only when it moved, and
a verdict carries the revision it was made against (a stale one is a 409).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import files, missions, prefs, project_dirs, shelf_mcp
from .fsbrowse import FsError, contained_path

# ---------------------------------------------------------------- vocabulary + bounds

KINDS: tuple[str, ...] = ("image", "video", "audio", "document", "code", "link", "text")
SOURCES: tuple[str, ...] = ("delivered", "found")
VERDICTS: tuple[str, ...] = ("good", "needs_work")
REF_TYPES: tuple[str, ...] = ("path", "url", "text", "code")
#: What an agent may name in `shelf_add`. `code` items are found in git (P1b), never delivered.
DELIVERABLE_KINDS: frozenset[str] = frozenset(
    {"image", "video", "audio", "document", "link", "text"}
)

ITEM_ID_RE = re.compile(r"^shf_[0-9a-f]{32}$")
TITLE_MAX = 120
WHY_MAX = 300
NOTE_MAX = 2000
#: Inline text, in UTF-8 bytes.
TEXT_MAX = 64 * 1024
URL_MAX = 2048
PATH_MAX = 4096
#: A delivered file is hashed in full from its descriptor; this bounds that work.
FILE_MAX = 1024 * 1024 * 1024
#: The largest text document the viewer shows in place (the file viewer's own read cap).
TEXT_DOC_MAX = files.FILES_MAX_READ
MISSION_ITEMS_MAX = 2000
PAGE_MAX = 100
#: A file must be this quiet (ctime age) before its stat fingerprint may stand in for a hash.
SETTLE_S = 2.0
_HASH_CHUNK = 1024 * 1024

#: Text documents the viewer shows in place, as plain text — never rendered as HTML.
TEXT_DOC_EXTS: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".diff",
        ".patch",
        ".rst",
        ".adoc",
    }
)
TEXT_MIME = "text/plain; charset=utf-8"

#: Refusal codes → what the agent is told (`shelf_mcp.MEANING`, the one table, import-light there).
#: Nothing else a refusal says; never echoes input.
REFUSALS: dict[str, str] = shelf_mcp.MEANING

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LINE_BREAKS = re.compile(r"[\r\n\t  ]+")
_GIT_OID_SEGMENT = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-f]{40}|[0-9a-f]{64})(?![0-9A-Za-z])")


class ShelfRefused(Exception):
    """A delivery or a read the shelf will not do. ``code`` is one of `REFUSALS`."""

    def __init__(self, code: str, status: int = 422) -> None:
        assert code in REFUSALS, code
        super().__init__(REFUSALS[code])
        self.code = code
        self.status = status


class ShelfConflict(missions.MissionError):
    """A verdict made against a revision that has moved on. 409, carrying the current one."""

    def __init__(self, revision: int) -> None:
        super().__init__("the shelf changed since it was read; reload it", status=409)
        self.revision = revision


# ---------------------------------------------------------------- schema (v35)

#: The tables live in `missions.SHELF_DDL` (the store owns its schema and its migration ladder).
SHELF_DDL = missions.SHELF_DDL


# ---------------------------------------------------------------- small helpers


def new_item_id() -> str:
    return "shf_" + uuid.uuid4().hex


def validate_item_id(item_id: object) -> str:
    if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
        raise missions.MissionError("unknown shelf item", status=404)
    return item_id


def clean_line(value: object, cap: int) -> str:
    """One line of agent-written text: control characters gone, breaks folded to a space."""
    if not isinstance(value, str):
        return ""
    text = _LINE_BREAKS.sub(" ", value)
    text = _CONTROL.sub("", text)
    return " ".join(text.split())[: cap + 1]


def clean_text(value: object) -> str:
    """Multi-line agent text: tabs and newlines kept, every other control character dropped."""
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


def _mask_oids(url: str) -> str:
    """``url`` with every git object id in its PATH masked: a commit page names an id, not a
    credential. The query and fragment are left exactly as they are, so a token there is judged."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=_GIT_OID_SEGMENT.sub("oid", parts.path)))


def _guard(text: str, *, url: bool = False) -> None:
    """The store boundary every agent string passes (`ask_store.guard_text`): a stored secret or a
    credential shape refuses the whole item — withheld, never redacted. A URL is shape-judged with
    its git ids masked (`_mask_oids`), and its real text is still judged for a STORED secret."""
    from . import ask_store, template_secrets

    if not text:
        return
    probe = _mask_oids(text) if url else text
    try:
        if ask_store.guard_text(probe, len(probe)) != probe:
            raise ShelfRefused("credential")
        if probe != text and template_secrets.redact_text(text) != text:
            raise ShelfRefused("credential")
    except ShelfRefused:
        raise
    except Exception:  # noqa: BLE001 — redaction that cannot run authorizes nothing
        raise ShelfRefused("store_unavailable", status=503) from None


def classify(path: str) -> tuple[str, str | None]:
    """``(kind, inline mime | None)`` for a file, by extension: the viewer's own allowlist
    (`files.PREVIEW_TYPES`) for media, except SVG, which is never served as markup (a document,
    download-only); text documents as plain text; anything else a download-only document."""
    ext = os.path.splitext(path)[1].lower()
    hit = files.PREVIEW_TYPES.get(ext)
    if hit and ext != ".svg":
        return hit[1], hit[0]
    if ext in TEXT_DOC_EXTS:
        return "document", TEXT_MIME
    return "document", None


def _ftyp(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")


#: Magic-byte checks per extension. A media item is served inline only when its first bytes say
#: what its extension says: an agent cannot rename a page or a script to `.png` and have it shown.
_SNIFF = {
    ".png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda h: h.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    ".gif": lambda h: h[:6] in (b"GIF87a", b"GIF89a"),
    ".webp": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP",
    ".avif": lambda h: len(h) >= 12 and h[4:8] == b"ftyp" and h[8:12] in (b"avif", b"avis"),
    ".bmp": lambda h: h[:2] == b"BM",
    ".ico": lambda h: h[:4] == b"\x00\x00\x01\x00",
    ".mp4": _ftyp,
    ".m4v": _ftyp,
    ".mov": _ftyp,
    ".m4a": _ftyp,
    ".webm": lambda h: h[:4] == b"\x1a\x45\xdf\xa3",
    ".weba": lambda h: h[:4] == b"\x1a\x45\xdf\xa3",
    ".ogv": lambda h: h[:4] == b"OggS",
    ".ogg": lambda h: h[:4] == b"OggS",
    ".oga": lambda h: h[:4] == b"OggS",
    ".opus": lambda h: h[:4] == b"OggS",
    ".mp3": lambda h: h[:3] == b"ID3" or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0),
    ".wav": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE",
    ".flac": lambda h: h[:4] == b"fLaC",
    ".aac": lambda h: h[:4] == b"ADIF" or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF6) == 0xF0),
}
SNIFF_BYTES = 64


def sniff_ok(path: str, head: bytes) -> bool:
    """Do the first bytes of a media file match its extension? False for any other extension."""
    check = _SNIFF.get(os.path.splitext(path)[1].lower())
    return bool(check and check(head))


def text_ok(head: bytes) -> bool:
    """A text document's first bytes: no NUL (the file viewer's own binary sniff)."""
    return b"\x00" not in head


def fingerprint(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"


def _settled(st: os.stat_result, now: float | None = None) -> bool:
    return ((time.time() if now is None else now) - st.st_ctime) >= SETTLE_S


def _hash_fd(fd: int, size: int) -> str:
    h = hashlib.sha256()
    pos = 0
    while pos < size:
        chunk = os.pread(fd, min(_HASH_CHUNK, size - pos), pos)
        if not chunk:
            break
        h.update(chunk)
        pos += len(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- the boundary


def mission_folders(mission: dict, *, index: dict | None = None) -> list[str]:
    """The folders a mission's files may live in: its own `cwd`, its project's folders and the
    folders of every project its objectives name (#1797), each realpath-resolved. An unreadable
    projects store contributes nothing (fail closed to the cwd alone)."""
    from . import projects

    out: list[str] = []
    seen: set[str] = set()

    def add(folder: object) -> None:
        if not isinstance(folder, str) or not folder.strip():
            return
        real = os.path.realpath(os.path.expanduser(folder))
        if real not in seen:
            seen.add(real)
            out.append(real)

    add(mission.get("cwd"))
    pids = {mission.get("project_id")} | {
        o.get("project_id") for o in (mission.get("objectives") or []) if isinstance(o, dict)
    }
    pids.discard(None)
    pids.discard("")
    if pids:
        try:
            idx = projects.load() if index is None else index
        except Exception:  # noqa: BLE001 — unreadable: the cwd alone
            idx = {}
        for pid in sorted(p for p in pids if isinstance(p, str)):
            entity = idx.get(pid)
            if entity is None or getattr(entity, "archived", False):
                continue
            for folder in getattr(entity, "folders", ()) or ():
                add(folder)
    return out


@dataclass(frozen=True)
class Boundary:
    """Where a mission's files may be, read ONCE per request: the mission's folders, the
    configured roots and `folder_exclusions`. Every entry is compared realpath-resolved (a file's
    own path is resolved before it is judged, so an exclusion typed through a symlinked alias must
    be too) and also as written (the session list's form), so neither spelling escapes it."""

    folders: tuple[str, ...]
    roots: tuple[str, ...]
    exclusions: tuple[str, ...]


def _both(paths: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        for v in (os.path.normpath(os.path.expanduser(p)), os.path.realpath(os.path.expanduser(p))):
            if v not in out:
                out.append(v)
    return tuple(out)


def boundary(mission: dict, *, index: dict | None = None) -> Boundary:
    return Boundary(
        folders=tuple(mission_folders(mission, index=index)),
        roots=tuple(project_dirs.effective_roots()),
        exclusions=_both(prefs.get_folder_exclusions()),
    )


def path_allowed(path: str, b: Boundary) -> bool:
    """Inside one of the mission's folders AND the terminal's hard scope (roots +
    `folder_exclusions`, `project_dirs.in_scope` with no curation). Pure string work on an
    already realpath-resolved path."""
    if not b.folders or not any(project_dirs.path_within(path, f) for f in b.folders):
        return False
    return project_dirs.in_scope(path, roots=list(b.roots), exclusions=list(b.exclusions))


def _scope_check(b: Boundary):
    def check(real: str) -> None:
        if not path_allowed(real, b):
            raise FsError("outside the mission's project folders", status=403)

    return check


def open_verified(path: str, b: Boundary) -> tuple[int, os.stat_result, str]:
    """Open ``path`` for a shelf read or delivery, proving every rule on the way: no final
    symlink, inside home, inside the mission's folders / roots / outside exclusions (checked on
    the resolved name BEFORE the open, so an excluded file is never even opened, and again on the
    descriptor), a regular file. Raises `FsError` with the file panel's statuses (404 missing, 403
    outside, 422 symlink / not a regular file)."""
    files._refuse_if_symlink(path)
    resolved = contained_path(path)
    check = _scope_check(b)
    check(resolved)
    fd, st, verified = files._open_verified(resolved, directory=False)
    try:
        check(verified)
    except BaseException:
        os.close(fd)
        raise
    return fd, st, verified


def _fs_refusal(e: FsError) -> ShelfRefused:
    if e.status == 404:
        return ShelfRefused("not_found")
    if e.status == 403:
        return ShelfRefused("outside_project")
    if "symlink" in str(e):
        return ShelfRefused("symlink")
    return ShelfRefused("not_a_file")


# ---------------------------------------------------------------- delivering


def _validate(item: object) -> dict:
    """Judge an agent's `shelf_add` arguments WITHOUT touching the filesystem or the store."""
    if not isinstance(item, dict):
        raise ShelfRefused("bad_request")
    allowed = {"kind", "title", "why", "path", "url", "text"}
    if set(item) - allowed:
        raise ShelfRefused("bad_request")
    given = [k for k in ("path", "url", "text") if item.get(k) not in (None, "")]
    if len(given) != 1:
        raise ShelfRefused("bad_request")
    kind = item.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in DELIVERABLE_KINDS):
        raise ShelfRefused("bad_request")
    if not isinstance(item.get("title", ""), str) or not isinstance(item.get("why", ""), str):
        raise ShelfRefused("bad_request")
    title = clean_line(item.get("title"), TITLE_MAX)
    why = clean_line(item.get("why"), WHY_MAX)
    if not title:
        raise ShelfRefused("title_required")
    if len(title) > TITLE_MAX or len(why) > WHY_MAX:
        raise ShelfRefused("too_long")
    ref_type = given[0]
    raw = item[ref_type]
    if not isinstance(raw, str):
        raise ShelfRefused("bad_request")
    out = {"title": title, "why": why, "ref_type": ref_type, "kind": kind}
    if ref_type == "text":
        text = clean_text(raw)
        if len(text.encode("utf-8")) > TEXT_MAX:
            raise ShelfRefused("too_long")
        if not text.strip():
            raise ShelfRefused("bad_request")
        if kind not in (None, "text"):
            raise ShelfRefused("kind_mismatch")
        out.update(kind="text", ref=text, size=len(text.encode("utf-8")))
    elif ref_type == "url":
        url = raw.strip()
        if len(url) > URL_MAX or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
            raise ShelfRefused("url_invalid")
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            raise ShelfRefused("url_invalid") from None
        if parts.scheme not in ("http", "https") or not host:
            raise ShelfRefused("url_invalid")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ShelfRefused("url_credentials")
        if kind not in (None, "link"):
            raise ShelfRefused("kind_mismatch")
        out.update(kind="link", ref=url, size=None)
    else:
        path = raw
        if "\x00" in path or len(path) > PATH_MAX or not os.path.isabs(path):
            raise ShelfRefused("bad_request")
        out.update(ref=os.path.normpath(path))
    _guard(title)
    _guard(why)
    if ref_type == "text":
        _guard(out["ref"])
    elif ref_type == "url":
        _guard(out["ref"], url=True)
    return out


def _read_file(item: dict, b: Boundary) -> dict:
    """The file half of a delivery: open with every rule, hash and sniff through the descriptor."""
    try:
        fd, st, verified = open_verified(item["ref"], b)
    except FsError as e:
        raise _fs_refusal(e) from None
    try:
        if st.st_size > FILE_MAX:
            raise ShelfRefused("too_large")
        kind, mime = classify(verified)
        if item["kind"] is not None and item["kind"] != kind:
            raise ShelfRefused("kind_mismatch")
        head = os.pread(fd, SNIFF_BYTES, 0)
        if kind in ("image", "video", "audio") and not sniff_ok(verified, head):
            raise ShelfRefused("content_mismatch")
        before = os.fstat(fd)
        sha = _hash_fd(fd, st.st_size)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    fp = (
        fingerprint(after)
        if fingerprint(before) == fingerprint(after) and _settled(after)
        else None
    )
    return {
        **item,
        "ref": verified,
        "kind": kind,
        "mime": mime,
        "size": after.st_size,
        "sha256": sha,
        "fingerprint": fp,
    }


def _member_tx(con, mission_id: str, session_key: str, phys_key: str) -> bool:
    """Is this launch's session on the mission's roster right now — by its own key, or by the
    late-bound key its runtime was adopted under (`session_runtime_bindings`)?"""
    row = con.execute(
        "SELECT 1 FROM mission_sessions WHERE mission_id=? AND removed_at IS NULL AND ("
        " session_key IN (?, ?) OR session_key IN ("
        "  SELECT logical_key FROM session_runtime_bindings WHERE physical_key IN (?, ?)))"
        " LIMIT 1",
        (mission_id, session_key, phys_key, session_key, phys_key),
    ).fetchone()
    return row is not None


def _bump_tx(con, mission_id: str) -> tuple[int, int]:
    """Take the next seq and raise the revision. Returns ``(seq, revision)``."""
    con.execute(
        "INSERT INTO mission_shelf_state (mission_id, revision, next_seq) VALUES (?,0,1) "
        "ON CONFLICT(mission_id) DO NOTHING",
        (mission_id,),
    )
    row = con.execute(
        "SELECT revision, next_seq FROM mission_shelf_state WHERE mission_id=?", (mission_id,)
    ).fetchone()
    seq, revision = int(row["next_seq"]), int(row["revision"]) + 1
    con.execute(
        "UPDATE mission_shelf_state SET revision=?, next_seq=? WHERE mission_id=?",
        (revision, seq + 1, mission_id),
    )
    return seq, revision


def deliver(
    mission_id: str,
    item: object,
    *,
    session_key: str,
    phys_key: str,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Put one agent-delivered item on a mission's shelf, or raise `ShelfRefused`.

    The identity (``mission_id``, ``session_key``, ``phys_key``) comes from the broker's
    capability row, never from the agent. Membership is checked twice: before the file is read
    (so a refused launch reads nothing) and again inside the write transaction, so a session
    detached meanwhile commits nothing."""
    missions.validate_id(mission_id)
    judged = _validate(item)
    m = missions.get_mission(mission_id, events_limit=1, path=path)
    if m is None or m.get("archived_at") is not None:
        raise ShelfRefused("not_in_mission", status=409)
    con = missions._ready(path)
    try:
        if not _member_tx(con, mission_id, session_key, phys_key):
            raise ShelfRefused("not_in_mission", status=409)
    finally:
        con.close()
    if judged["ref_type"] == "path":
        judged = _read_file(judged, boundary(m))
    ts = time.time() if now is None else now
    item_id = new_item_id()
    with missions._write_lock:
        con = missions._ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archived_at, archiving_at FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if (
                row is None
                or row["archived_at"] is not None
                or row["archiving_at"] is not None
                or not _member_tx(con, mission_id, session_key, phys_key)
            ):
                raise ShelfRefused("not_in_mission", status=409)
            count = con.execute(
                "SELECT COUNT(*) FROM mission_shelf_items WHERE mission_id=?", (mission_id,)
            ).fetchone()[0]
            if count >= MISSION_ITEMS_MAX:
                raise ShelfRefused("shelf_full", status=409)
            seq, revision = _bump_tx(con, mission_id)
            con.execute(
                "INSERT INTO mission_shelf_items (id, mission_id, seq, source, kind, title, why,"
                " session_key, ref_type, ref, mime, size, sha256, fingerprint, created_at) "
                "VALUES (?,?,?,'delivered',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    mission_id,
                    seq,
                    judged["kind"],
                    judged["title"],
                    judged["why"],
                    session_key,
                    judged["ref_type"],
                    judged["ref"],
                    judged.get("mime"),
                    judged.get("size"),
                    judged.get("sha256"),
                    judged.get("fingerprint"),
                    ts,
                ),
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"id": item_id, "kind": judged["kind"], "revision": revision}


# ---------------------------------------------------------------- integrity

#: ``(item id, fingerprint) → sha256`` of what is on disk now, so a changed file is hashed once
#: per change rather than on every read. Bounded; process-local; never authoritative.
_DISK_SHA: OrderedDict[tuple[str, str], str] = OrderedDict()
_DISK_SHA_MAX = 4096
_disk_lock = threading.Lock()


def reset_cache_for_test() -> None:
    with _disk_lock:
        _DISK_SHA.clear()
    with _states_lock:
        _STATES.clear()


def integrity(row: dict, b: Boundary) -> dict:
    """The file state of a path item right now: ``ok``, ``changed`` (with the on-disk sha256 and
    mtime), ``missing`` or ``inaccessible``. Never raises; never follows a link. An ``ok`` that had
    to hash a quiet file carries ``settled_fp``: the fingerprint the row may now keep, so the next
    read (in this process or after a restart) skips the hash."""
    try:
        fd, st, _verified = open_verified(row["ref"], b)
    except FsError as e:
        return {"state": "missing" if e.status == 404 else "inaccessible"}
    except Exception:  # noqa: BLE001 — unknowable is not "ok"
        return {"state": "inaccessible"}
    try:
        fp = fingerprint(st)
        if row.get("fingerprint") and fp == row["fingerprint"]:
            return {"state": "ok"}
        with _disk_lock:
            sha = _DISK_SHA.get((row["id"], fp))
        quiet = False
        if sha is None:
            if st.st_size > FILE_MAX:
                return {"state": "changed", "disk_sha256": None, "disk_mtime": st.st_mtime}
            sha = _hash_fd(fd, st.st_size)
            quiet = fingerprint(os.fstat(fd)) == fp and _settled(st)
            if quiet:
                with _disk_lock:
                    _DISK_SHA[(row["id"], fp)] = sha
                    while len(_DISK_SHA) > _DISK_SHA_MAX:
                        _DISK_SHA.popitem(last=False)
        if sha == row.get("sha256"):
            return {"state": "ok", "settled_fp": fp} if quiet else {"state": "ok"}
        return {"state": "changed", "disk_sha256": sha, "disk_mtime": st.st_mtime}
    except OSError:
        return {"state": "inaccessible"}
    finally:
        os.close(fd)


#: The file states of one mission's shelf, shared by the pages of ONE read: ``mission id →
#: (revision, monotonic time, states)``. A full read is up to 20 page requests, and checking
#: every file on every page made it quadratic (review on #1904). Short-lived on purpose — file
#: states are re-checked on the next read — and dropped the moment the revision moves.
_STATES: dict[str, tuple[int, float, dict[str, dict]]] = {}
STATES_TTL_S = 5.0
_states_lock = threading.Lock()


def _file_states(
    mission_id: str, revision: int, rows: list[dict], b: Boundary, *, fresh: bool
) -> tuple[dict[str, dict], list[tuple[str, str]]]:
    """``(states, settled)``: every path item's state, and the ``(fingerprint, item id)`` pairs a
    FRESH check found quiet and unchanged (to write back). Only the states are shared between the
    pages of a read, and they are never mutated afterwards — two requests may hold the same dict."""
    now = time.monotonic()
    if not fresh:
        with _states_lock:
            hit = _STATES.get(mission_id)
        if hit and hit[0] == revision and now - hit[1] < STATES_TTL_S:
            return hit[2], []
    raw = {r["id"]: integrity(r, b) for r in rows if r["ref_type"] == "path"}
    settled = [(st["settled_fp"], item_id) for item_id, st in raw.items() if "settled_fp" in st]
    states = {k: {f: v for f, v in st.items() if f != "settled_fp"} for k, st in raw.items()}
    with _states_lock:
        _STATES[mission_id] = (revision, now, states)
        for k in [k for k, v in _STATES.items() if now - v[1] >= STATES_TTL_S]:
            _STATES.pop(k, None)
    return states, settled


def _keep_fingerprints(settled: list[tuple[str, str]], path: Path | None) -> None:
    """Write back the fingerprint of every file a fresh check hashed and found unchanged and
    quiet. Best-effort: a busy store just means the next read hashes again."""
    if not settled:
        return
    try:
        with missions._write_lock:
            con = missions._ready(path)
            try:
                con.executemany("UPDATE mission_shelf_items SET fingerprint=? WHERE id=?", settled)
            finally:
                con.close()
    except Exception:  # noqa: BLE001 — a cache, never a requirement
        return


# ---------------------------------------------------------------- reading


def _row(r) -> dict:
    d = dict(r)
    d["meta"] = missions._loads(d.get("meta")) if d.get("meta") else None
    return d


def _public(row: dict, state: dict | None) -> dict:
    """What the API returns for one item. A path is shown by its name relative to the mission's
    folder where it can be (the client never sends one back: reads go by item id)."""
    out = {
        "id": row["id"],
        "seq": row["seq"],
        "source": row["source"],
        "kind": row["kind"],
        "title": row["title"],
        "why": row["why"],
        "session_key": row["session_key"],
        "step_id": row.get("step_id"),
        "ref_type": row["ref_type"],
        "size": row.get("size"),
        "sha256": row.get("sha256"),
        "created_at": row["created_at"],
        "verdict": row.get("verdict"),
        "note": row.get("note") or "",
        "verdict_at": row.get("verdict_at"),
    }
    if row["ref_type"] == "path":
        out["path"] = row["ref"]
        out["name"] = os.path.basename(row["ref"])
        out["mime"] = row.get("mime")
        out["viewable"] = row.get("mime") is not None
        out["file"] = state or {"state": "ok"}
    elif row["ref_type"] == "url":
        parts = urlsplit(row["ref"])
        out["url"] = row["ref"]
        out["host"] = parts.hostname or ""
        out["url_path"] = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    elif row["ref_type"] == "text":
        out["text"] = row["ref"]
    else:
        out["meta"] = row.get("meta")
    return out


def _counts(rows: list[dict], states: dict[str, dict]) -> dict:
    by_kind = dict.fromkeys(KINDS, 0)
    by_source = dict.fromkeys(SOURCES, 0)
    unreviewed = 0
    checks = {"changed": 0, "missing": 0, "inaccessible": 0}
    for r in rows:
        by_kind[r["kind"]] += 1
        by_source[r["source"]] += 1
        if r.get("verdict") is None:
            unreviewed += 1
        state = states.get(r["id"], {}).get("state")
        if state in checks:
            checks[state] += 1
    return {
        "total": len(rows),
        "by_kind": by_kind,
        "by_source": by_source,
        "unreviewed": unreviewed,
        "file_checks": checks,
    }


def _parse_cursor(cursor: object) -> int:
    if cursor in (None, ""):
        return 0
    if not isinstance(cursor, str) or not re.fullmatch(r"s[0-9]{1,9}", cursor):
        raise missions.MissionError("malformed cursor", status=422)
    return int(cursor[1:])


def _verdict_matches(have: object, want: object) -> bool:
    return have is None if want == "unreviewed" else have == want


def manifest(
    mission_id: str,
    *,
    cursor: object = None,
    kind: object = None,
    source: object = None,
    verdict: object = None,
    limit: int = PAGE_MAX,
    path: Path | None = None,
) -> dict:
    """One page of a mission's shelf. Counts and file checks describe the WHOLE shelf, computed
    before the filters (the session list's facet rule); the filters narrow before the page."""
    missions.validate_id(mission_id)
    after = _parse_cursor(cursor)
    if kind not in (None, "") and kind not in KINDS:
        raise missions.MissionError("unknown kind", status=422)
    if source not in (None, "") and source not in SOURCES:
        raise missions.MissionError("unknown source", status=422)
    if verdict not in (None, "") and verdict not in (*VERDICTS, "unreviewed"):
        raise missions.MissionError("unknown verdict filter", status=422)
    limit = max(1, min(PAGE_MAX, int(limit or PAGE_MAX)))
    m = missions.get_mission(mission_id, events_limit=1, path=path)
    if m is None:
        raise missions.MissionError("unknown mission", status=404)
    con = missions._ready(path)
    try:
        con.execute("BEGIN DEFERRED")
        rev = con.execute(
            "SELECT revision FROM mission_shelf_state WHERE mission_id=?", (mission_id,)
        ).fetchone()
        rows = [
            _row(r)
            for r in con.execute(
                "SELECT * FROM mission_shelf_items WHERE mission_id=? ORDER BY seq ASC LIMIT ?",
                (mission_id, MISSION_ITEMS_MAX),
            ).fetchall()
        ]
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    revision = int(rev["revision"]) if rev else 0
    # The first page of a read checks every file; the pages after it share that answer.
    states, settled = _file_states(mission_id, revision, rows, boundary(m), fresh=after == 0)
    _keep_fingerprints(settled, path)
    counts = _counts(rows, states)
    picked = [
        r
        for r in rows
        if r["seq"] > after
        and (not kind or r["kind"] == kind)
        and (not source or r["source"] == source)
        and (
            not verdict
            or (
                r.get("verdict") is None if verdict == "unreviewed" else r.get("verdict") == verdict
            )
        )
    ]
    page = picked[:limit]
    return {
        "mission_id": mission_id,
        "revision": revision,
        "counts": counts,
        "items": [_public(r, states.get(r["id"])) for r in page],
        "next_cursor": f"s{page[-1]['seq']}" if len(picked) > limit else None,
    }


def get_item(mission_id: str, item_id: str, *, path: Path | None = None) -> dict | None:
    """The stored row of one item, ONLY if it is this mission's (else None — the route's 404)."""
    missions.validate_id(mission_id)
    validate_item_id(item_id)
    con = missions._ready(path)
    try:
        r = con.execute(
            "SELECT * FROM mission_shelf_items WHERE id=? AND mission_id=?", (item_id, mission_id)
        ).fetchone()
    finally:
        con.close()
    return _row(r) if r else None


def set_verdict(
    mission_id: str,
    item_id: str,
    *,
    expected_revision: object,
    verdict: object,
    note: object = "",
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Record the operator's verdict on one item. ``verdict`` None clears it (and its note)."""
    missions.validate_id(mission_id)
    validate_item_id(item_id)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise missions.MissionError("expected_revision must be an integer", status=422)
    if verdict is not None and verdict not in VERDICTS:
        raise missions.MissionError("verdict is good, needs_work or null", status=422)
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise missions.MissionError("note must be text", status=422)
    note = clean_text(note).strip()
    if len(note) > NOTE_MAX:
        raise missions.MissionError(f"a note is at most {NOTE_MAX} characters", status=422)
    if verdict is None:
        note = ""
    ts = time.time() if now is None else now
    with missions._write_lock:
        con = missions._ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            m = con.execute(
                "SELECT archived_at, archiving_at FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if m is None:
                raise missions.MissionError("unknown mission", status=404)
            if m["archived_at"] is not None or m["archiving_at"] is not None:
                raise missions.MissionError("an archived mission is read-only", status=409)
            r = con.execute(
                "SELECT * FROM mission_shelf_items WHERE id=? AND mission_id=?",
                (item_id, mission_id),
            ).fetchone()
            if r is None:
                raise missions.MissionError("unknown shelf item", status=404)
            rev_row = con.execute(
                "SELECT revision FROM mission_shelf_state WHERE mission_id=?", (mission_id,)
            ).fetchone()
            current = int(rev_row["revision"]) if rev_row else 0
            if expected_revision != current:
                raise ShelfConflict(current)
            revision = current + 1
            con.execute(
                "UPDATE mission_shelf_state SET revision=? WHERE mission_id=?",
                (revision, mission_id),
            )
            con.execute(
                "UPDATE mission_shelf_items SET verdict=?, note=?, verdict_at=? WHERE id=?",
                (verdict, note, ts if verdict is not None else None, item_id),
            )
            row = con.execute("SELECT * FROM mission_shelf_items WHERE id=?", (item_id,)).fetchone()
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"revision": revision, "item": _public(_row(row), None)}


# ---------------------------------------------------------------- the read path


def read_plan(mission_id: str, item_id: str, *, path: Path | None = None) -> tuple[dict, Boundary]:
    """The stored row of a file item and the mission's boundary, or `MissionError` 404 when the
    item is not this mission's or is not a file. The route opens it (`open_verified`)."""
    row = get_item(mission_id, item_id, path=path)
    if row is None or row["ref_type"] != "path":
        raise missions.MissionError("unknown shelf item", status=404)
    m = missions.get_mission(mission_id, events_limit=1, path=path)
    if m is None:
        raise missions.MissionError("unknown shelf item", status=404)
    return row, boundary(m)


def inline_type(row: dict, verified: str, head: bytes, size: int) -> str | None:
    """The Content-Type an item's bytes may be served INLINE as, or None (download only).

    Media: the extension's allowlisted MIME, and only when the first bytes match it. A text
    document: plain text, when it is small enough and has no NUL. SVG and everything else: never.
    The decision is made on the bytes being served now, not on what was delivered."""
    kind, mime = classify(verified)
    if mime is None or kind != row["kind"]:
        return None
    if kind in ("image", "video", "audio"):
        return mime if sniff_ok(verified, head) else None
    if mime == TEXT_MIME:
        return TEXT_MIME if size <= TEXT_DOC_MAX and text_ok(head) else None
    return None


__all__ = [
    "DELIVERABLE_KINDS",
    "KINDS",
    "REFUSALS",
    "ShelfConflict",
    "ShelfRefused",
    "classify",
    "deliver",
    "get_item",
    "inline_type",
    "integrity",
    "manifest",
    "mission_folders",
    "open_verified",
    "Boundary",
    "boundary",
    "path_allowed",
    "read_plan",
    "set_verdict",
    "sniff_ok",
]
