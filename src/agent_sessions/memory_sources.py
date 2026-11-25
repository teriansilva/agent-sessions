"""Agents' OWN memory stores, read so a project's shared memory can import them (#1471).

Which agent keeps one is a manifest fact (`memory.kind`, a member of `kinds.MEMORY_SOURCE_KINDS`);
the reader is registered here under that KIND, never under an engine id, and finds the store
through the declaring manifest's own paths (`store_for_engine(..., "sessions")`), never a literal.

**Read only, by construction.** Nothing in this module writes, renames or unlinks: it opens
directories and files read-only, relative to a checked descriptor, and returns bytes.

**What is read, and how.** A memory directory is opened `O_DIRECTORY|O_NOFOLLOW` and checked ON
THAT DESCRIPTOR: a directory, owned by this user, writable by nobody else (`provenance`'s rule, so
a user-private group is fine and an ACL is honoured). Each file is opened relative to it with
`O_NOFOLLOW|O_NONBLOCK` and checked on its own descriptor — a regular, single-link file of this
user — then read to the cap plus one byte, so an over-size file is refused without trusting its
size. A request never names a path: callers pass the opaque `Source.id` of a freshly discovered
list, and anything that changed since is simply a different list.

**Which folder a store belongs to** is what its transcripts RECORD (`cwd`), never the directory
name: Claude encodes `/` and `.` alike as `-`, so a name cannot tell `agent-sessions` from
`agent-sessions-web`. A store whose transcripts record no folder is reported `unavailable` with
that reason — never silently attributed, never silently dropped.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: An imported note may be this large (agent inbox notes stay at `memory.ENTRY_MAX_BYTES`).
IMPORT_MAX_BYTES = 32 * 1024
#: Files looked at per source; the excess is reported, never read.
MAX_FILES = 1000
#: Session directories looked at per store; transcripts checked per candidate directory (its
#: archive included); and how far into one transcript its first `cwd` is looked for (a session's
#: first message precedes it — 1 MiB in the largest seen here). Past a bound a store is
#: reported unavailable, never attributed from a sample.
MAX_DIRS = 4000
MAX_TRANSCRIPTS = 4000
TRANSCRIPT_MAX = 16 * 1024 * 1024

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass
class Source:
    """One agent memory store found for a project's folders."""

    id: str  # opaque; what a client sends back
    key: str  # stable identity of the store: kind + its directory (the `imports` table key)
    kind: str
    engine: str
    label: str
    path: str  # for display only (`~` abbreviated); never accepted back
    relation: str  # "this folder" | "a subfolder session" | "" (unavailable before it was known)
    files: int = 0
    over_agent_size: int = 0  # files larger than an agent note, still importable
    over_cap: int = 0  # files larger than IMPORT_MAX_BYTES: refused
    beyond_max_files: int = 0
    state: str = "ok"  # "ok" | "unavailable"
    reason: str = ""
    dir: Path = field(default=Path(), repr=False)
    #: Re-establish this store's attribution NOW against the CURRENT project index and session
    #: assignments: "" while every session is still that project's (with the same relation),
    #: else why not. The import calls it at commit — after the read, under the project and
    #: metadata locks — see `memory.import_notes`.
    recheck: Callable[..., str] = field(default=lambda index, meta=None: "", repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "engine": self.engine,
            "label": self.label,
            "path": self.path,
            "relation": self.relation,
            "files": self.files,
            "over_agent_size": self.over_agent_size,
            "over_cap": self.over_cap,
            "beyond_max_files": self.beyond_max_files,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass
class Item:
    """One file of a source: its bytes, or why it was not read."""

    name: str
    data: bytes | None
    reason: str = ""
    #: For a file that was not read: what it looked like (size, mtime), so an unchanged refusal is
    #: recognised on the next import instead of being refused — and listed — again.
    stamp: str = ""


def _source_id(key: str) -> str:
    return "s" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _display(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home) :] if s == home or s.startswith(home + "/") else s


def _check_dir(fd: int) -> str:
    """Why this directory may not be read, or ``""``."""
    from .plugins import provenance

    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        return "not a directory"
    if st.st_uid != os.geteuid():
        return "owned by another user"
    if provenance.writable_by_others(st, fd):
        return "writable by other users"
    return ""


@contextlib.contextmanager
def _opened(path: Path):
    fd = os.open(path, _DIR)
    try:
        yield fd
    finally:
        os.close(fd)


def _candidates(fd: int) -> list[str]:
    """The note files of a memory directory, by name: `*.md` except the `MEMORY.md` index."""
    return sorted(n for n in os.listdir(fd) if n.endswith(".md") and n != "MEMORY.md")


def _name(raw: str) -> str:
    """A file name as text that is safe to store and show: an undecodable byte (kept by
    `os.listdir` as a lone surrogate) is written as its escape, so it never reaches a database or
    a response as invalid text."""
    return raw.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _read_at(dfd: int, name: str) -> Item:
    if _name(name) != name:
        return Item(_name(name), None, "its file name is not UTF-8 text")
    try:
        fd = os.open(name, _FILE, dir_fd=dfd)
    except OSError:
        return Item(name, None, "not a regular file")
    stamp = ""
    try:
        st = os.fstat(fd)
        stamp = f"{st.st_mode}:{st.st_size}:{st.st_mtime_ns}"
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            return Item(name, None, "not a regular single-link file of this user", stamp)
        data = os.read(fd, IMPORT_MAX_BYTES + 1)
    except OSError as exc:
        return Item(name, None, f"could not be read ({exc.strerror or exc})", stamp)
    finally:
        os.close(fd)
    if len(data) > IMPORT_MAX_BYTES:
        return Item(name, None, f"larger than {IMPORT_MAX_BYTES // 1024} KiB", stamp)
    return Item(name, data)


def _sized(source: Source, agent_max: int) -> None:
    """Fill in a source's counts (from `fstat`, for the preview; the read re-checks each file)."""
    try:
        with _opened(source.dir) as fd:
            why = _check_dir(fd)
            if why:
                source.state, source.reason = "unavailable", why
                return
            names = _candidates(fd)
            source.beyond_max_files = max(0, len(names) - MAX_FILES)
            for name in names[:MAX_FILES]:
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                source.files += 1
                if st.st_size > IMPORT_MAX_BYTES:
                    source.over_cap += 1
                elif st.st_size > agent_max:
                    source.over_agent_size += 1
    except OSError as exc:
        source.state, source.reason = "unavailable", f"could not be opened ({exc.strerror or exc})"


# ---- the `claude-auto-memory` kind --------------------------------------------------------------
#
# Claude names a session directory after the folder the session ran in (every non-alphanumeric
# character becomes `-`), so only a directory named like one of the project's folders — or like
# a folder under one (`<name>-…`) — can hold this project's sessions. That name is only a
# CANDIDATE: it is lossy (`/a/b-c` and `/a/b/c` share it). Attribution is decided by EVERY
# transcript of the directory — current and archived alike — each giving the folder its session
# was LAUNCHED in (its first top-level `cwd`, parsed, matching the directory's name), and fails
# closed: one session whose folder cannot be established, or that ran outside the project, and
# the whole store is reported, never imported.


class _Unattributable(Exception):
    pass


#: Claude's session MESSAGE records. A transcript with neither never ran a turn, so it cannot have
#: written memory: it is metadata only (a title, a permission mode) and carries no folder.
_MESSAGE_TYPES = frozenset({"user", "assistant"})


def _session_folder(dfd: int, name: str, dirname: str) -> str | None:
    """The folder a transcript's session was launched in: its FIRST top-level ``cwd``, parsed
    record by record (a ``cwd`` nested in a message is never read). It must name this very
    directory (Claude derives the directory name from it), and no message may come before it.
    None for a metadata-only transcript. Raises `_Unattributable` for anything else."""
    try:
        fd = os.open(name, _FILE, dir_fd=dfd)
    except OSError as exc:
        raise _Unattributable(f"a session could not be read ({exc.strerror or exc})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unattributable("a session is not a regular file")
        with os.fdopen(os.dup(fd), "rb") as fh:
            read = 0
            while True:
                raw = fh.readline(TRANSCRIPT_MAX - read + 1)
                if not raw:
                    return None
                read += len(raw)
                if read > TRANSCRIPT_MAX:
                    raise _Unattributable("a session is too long to find its folder")
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    raise _Unattributable("a session has an unreadable record") from None
                if not isinstance(rec, dict):
                    raise _Unattributable("a session has an unreadable record")
                cwd = rec.get("cwd")
                if cwd is not None:
                    if not isinstance(cwd, str) or not cwd.startswith("/"):
                        raise _Unattributable("a session records an unreadable folder")
                    cwd = os.path.normpath(cwd)
                    if _encoded(cwd) != dirname:
                        raise _Unattributable(
                            "a session's recorded folder does not match its store"
                        )
                    return cwd
                if rec.get("type") in _MESSAGE_TYPES:
                    raise _Unattributable("a session's message records no folder")
    except OSError as exc:
        raise _Unattributable(f"a session could not be read ({exc.strerror or exc})") from None
    finally:
        os.close(fd)


def _sessions(paths: list[Path]) -> list[tuple[str, str]]:
    """``(session id, launch folder)`` for every transcript in these directories (the session dir
    and its archived twin, which share one name). Raises `_Unattributable` rather than answer from
    a sample."""
    found: list[tuple[str, str]] = []
    seen = 0
    for path in paths:
        try:
            dfd = os.open(path, _DIR)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _Unattributable(
                f"its sessions could not be listed ({exc.strerror or exc})"
            ) from None
        try:
            for name in os.listdir(dfd):
                if not name.endswith(".jsonl"):
                    continue
                seen += 1
                if seen > MAX_TRANSCRIPTS:
                    raise _Unattributable("too many sessions to check which folders they ran in")
                cwd = _session_folder(dfd, name, path.name)
                if cwd is not None:
                    found.append((name[: -len(".jsonl")], cwd))
        finally:
            os.close(dfd)
    return found


def _encoded(folder: str) -> str:
    """Claude's directory name for a folder (ASCII letters and digits kept, the rest `-`)."""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in folder)


def _candidate(name: str, folders: list[str]) -> bool:
    return any(name == _encoded(f) or name.startswith(_encoded(f) + "-") for f in folders)


def _attribute(
    dirs: list[Path], engine: str, pid: str, index: dict, meta: dict | None = None
) -> tuple[str, str]:
    """``(relation, "")`` for a store EVERY session of which belongs to project ``pid``;
    ``("", reason)`` for one that is mixed or cannot be attributed; ``("", "")`` for one none of
    whose sessions is ``pid``'s (not this project's to mention).

    Which project a session belongs to is decided by THE resolver (`projects.resolve`): its
    explicit assignment wins, then the most specific project folder owning its launch folder — so
    a session assigned elsewhere, or launched in a sub-folder another project owns, is not this
    project's, whatever folder it ran in."""
    from . import metadata, projects

    try:
        sessions = _sessions(dirs)
    except _Unattributable as exc:
        return "", str(exc)
    if not sessions:
        return "", "no session records which folder it belongs to"
    if meta is None:
        meta = metadata.load()
    entity = index.get(pid)
    own = {os.path.normpath(f) for f in (entity.folders if entity else ())}
    owners: set[str] = set()
    here = False
    for sid, cwd in sessions:
        m = meta.get(f"{engine}:{sid}")
        ref = projects.resolve(cwd, m.project_id if m else "", index)
        owners.add(ref.id if ref.kind == "project" else "")
        here = here or cwd in own
    if pid not in owners:
        return "", ""
    if owners != {pid}:
        return "", "not all of its sessions belong to this project"
    return ("this folder" if here else "a subfolder session"), ""


def _still(
    dirs: list[Path], engine: str, pid: str, index: dict, rel: str, meta: dict | None = None
) -> str:
    got, why = _attribute(dirs, engine, pid, index, meta)
    if why:
        return why
    if got != rel:
        return "its sessions changed while it was being read"
    return ""


def _claude_auto_memory(kind: str, prov, pid: str, index: dict, agent_max: int) -> list[Source]:
    from .engines import base, registry

    try:
        store = registry.store_for_engine(prov.engine_id, "sessions")
    except base.EngineError:
        return []
    try:
        archive: Path | None = registry.store_for_engine(prov.engine_id, "archive")
    except base.EngineError:
        archive = None
    try:
        names = sorted(os.listdir(store))[:MAX_DIRS]
    except FileNotFoundError:
        return []  # the agent never ran here: genuinely nothing to import
    except OSError as exc:
        return [_unavailable(kind, prov, store, f"could not be opened ({exc.strerror or exc})")]
    entity = index.get(pid)
    folders = _norm(entity.folders if entity else ())
    out: list[Source] = []
    for name in names:
        if not _candidate(name, folders):
            continue
        mdir = store / name / "memory"
        try:
            mst = os.stat(mdir, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        dirs = [store / name] + ([archive / name] if archive else [])
        rel, why = _attribute(dirs, prov.engine_id, pid, index)
        if not rel and not why:
            continue  # a sibling folder whose name merely starts like ours: not this project's
        if why:
            out.append(_unavailable(kind, prov, mdir, why))
            continue
        key = f"{kind}:{mdir}"
        src = Source(
            id=_source_id(key),
            key=key,
            kind=kind,
            engine=prov.engine_id,
            label=prov.manifest.identity.label,
            path=_display(mdir),
            relation=rel,
            dir=mdir,
            recheck=lambda now, meta=None, dirs=dirs, rel=rel: _still(
                dirs, prov.engine_id, pid, now, rel, meta
            ),
        )
        if not stat.S_ISDIR(mst.st_mode):
            src.state, src.reason = "unavailable", "its memory is not a directory"
        else:
            _sized(src, agent_max)
        out.append(src)
    return out


def _unavailable(kind: str, prov, path: Path, reason: str) -> Source:
    key = f"{kind}:{path}"
    return Source(
        id=_source_id(key),
        key=key,
        kind=kind,
        engine=prov.engine_id,
        label=prov.manifest.identity.label,
        path=_display(path),
        relation="",
        state="unavailable",
        reason=reason,
        dir=path,
    )


#: kind → reader. A kind declared in `kinds.MEMORY_SOURCE_KINDS` with no reader here finds nothing.
READERS: dict[str, Callable[[str, object, str, dict, int], list[Source]]] = {
    "claude-auto-memory": _claude_auto_memory,
}

#: kind → {the source's own note type: a shared-memory type}. An unmapped type is the default.
TYPE_MAPS: dict[str, dict[str, str]] = {
    # Claude's auto-memory types (`metadata.type`): user, feedback, project, reference.
    "claude-auto-memory": {"reference": "reference", "feedback": "decision"},
}
_FRONT_TYPE = re.compile(r"^\s*type:\s*['\"]?([A-Za-z_-]+)", re.MULTILINE)


def note_type(kind: str, text: str) -> str | None:
    """The shared-memory type a source note maps to, read from its frontmatter, or None."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    m = _FRONT_TYPE.search(text[4:end] if end != -1 else "")
    return TYPE_MAPS.get(kind, {}).get(m.group(1).lower()) if m else None


def _norm(folders) -> list[str]:
    return [os.path.normpath(os.path.expanduser(f)) for f in folders if f]


def discover(pid: str, index: dict, *, agent_max: int) -> list[Source]:
    """Every agent memory store found for project ``pid`` (in the project ``index``), across the
    roster — ordered "this folder" first. A reader that fails is reported as one unavailable
    source, never raised."""
    from .engines import registry

    out: list[Source] = []
    for prov in registry.all_providers():
        kind = getattr(prov.manifest, "memory_kind", "none")
        reader = READERS.get(kind)
        if reader is None:
            continue
        try:
            out.extend(reader(kind, prov, pid, index, agent_max))
        except Exception as exc:  # noqa: BLE001 — one unreadable store never fails the page
            out.append(_unavailable(kind, prov, Path("?"), f"could not be read ({exc})"))
    order = {"this folder": 0, "a subfolder session": 1, "": 2}
    return sorted(out, key=lambda s: (order.get(s.relation, 3), s.path))


def read(source: Source) -> list[Item]:
    """The files of an `ok` source, each read through its own checked descriptor. Raises
    `PermissionError` (with the reason) when the directory may not be read. This is NOT the
    authorization — `Source.recheck`, run at commit, is (see `memory.import_notes`)."""
    with _opened(source.dir) as fd:
        why = _check_dir(fd)
        if why:
            raise PermissionError(why)
        return [_read_at(fd, name) for name in _candidates(fd)[:MAX_FILES]]
