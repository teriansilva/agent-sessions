"""Read Claude Code session history off disk.

Source of truth: ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`` (live) and
``~/.claude/projects-archive/<encoded-cwd>/<uuid>.jsonl`` (archived).

Each JSONL is one session. The directory name is a **lossy** encoding of the
cwd — Claude Code replaces ``/`` *and* ``.`` (and other non-alphanumerics)
with ``-``, so ``demoapp.io`` and ``demoapp/io`` both become
``...-demoapp-io`` and can't be told apart. The authoritative cwd is the
``cwd`` field recorded inside the JSONL; we read that and only fall back to
decoding the directory name when a file has no ``cwd`` field. This matters
because the cwd feeds the open-session allowlist + the ws launch dir.

This scanner is Claude-Code-only by design — it reads ``~/.claude/projects``.
opencode is a separate engine read by ``engines.OpenCodeProvider`` (a read-only
SQLite reader), not here. See agent-sessions#10/#11/#12.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import project_dirs
from .metadata import title_candidate

# Session UUIDs that Claude Code writes are RFC4122-shaped.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# --- Session creation time (#506) ------------------------------------------------------
# The sidebar can sort by *creation* time (a stable order) rather than the update mtime.
# No engine recorded a creation time before, so we derive one per engine: the first
# record's timestamp for the JSONL engines (claude/codex/gemini/antigravity), opencode's
# ``time_created`` column, with a filesystem fallback. These helpers are shared by every
# file-based provider so the derivation (and its "read only the first record, never the
# whole file" guarantee) stays in one place.
_CREATED_AT_MAX_LINES = 20  # scan at most this many leading records for a timestamp


def _parse_epoch(value: object) -> float | None:
    """Parse a timestamp to epoch *seconds*: an epoch number (seconds or milliseconds), or an
    ISO-8601 string. Returns ``None`` for anything unparseable so the caller can fall back."""
    if isinstance(value, bool):  # bool is an int subclass — never a timestamp
        return None
    if isinstance(value, int | float):
        if value <= 0:
            return None
        return value / 1000.0 if value > 1e12 else float(value)  # heuristic: ms vs s
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def first_record_created_at(path: Path, max_lines: int = _CREATED_AT_MAX_LINES) -> float | None:
    """Creation time from the earliest record's timestamp in a JSONL session file.

    Scans at most ``max_lines`` leading records (NOT the whole file — a malformed/empty head
    just yields ``None`` so the caller falls back to the filesystem time) and returns the first
    usable timestamp found, looking at the common top-level (``timestamp``/``ts``/``time``/
    ``created_at``) and nested ``payload`` shapes the various engines use. ``None`` when
    unreadable or no timestamp is present."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                for key in ("timestamp", "ts", "time", "created_at"):
                    ts = _parse_epoch(rec.get(key))
                    if ts is not None:
                        return ts
                payload = rec.get("payload")
                if isinstance(payload, dict):
                    for key in ("timestamp", "ts", "time"):
                        ts = _parse_epoch(payload.get(key))
                        if ts is not None:
                            return ts
    except OSError:
        return None
    return None


def fs_created_at(st: os.stat_result) -> float:
    """Filesystem creation time: ``st_birthtime`` where the platform records it (macOS/BSD),
    else ``st_ctime`` (on Linux ext4 birthtime is usually unavailable; ctime is the inode
    change time — an imperfect but stable fallback used only when no record timestamp exists)."""
    bt = getattr(st, "st_birthtime", None)
    return float(bt) if bt else float(st.st_ctime)


def derive_created_at(content_path: Path | None, st: os.stat_result) -> float:
    """Creation time for a file-based session: the first-record timestamp when available,
    else the filesystem fallback. ``content_path`` is the JSONL/transcript to peek at (``None``
    for engines whose content isn't a readable JSONL — straight to the fs fallback)."""
    if content_path is not None:
        ts = first_record_created_at(content_path)
        if ts is not None:
            return ts
    return fs_created_at(st)


# --- Session last-activity time (#525) -------------------------------------------------
# The Update-order sort key (and the reaper / pulse activity signal) must be the time of the last
# *real* conversation turn, NOT the JSONL's filesystem mtime. A bare ``claude --resume`` rewrites
# app-state records — ``permission-mode`` / ``mode`` / ``ai-title`` / ``last-prompt`` /
# ``file-history-snapshot``, all timestamp-less — which bump ``st.st_mtime`` with zero conversation
# activity, so sorting on the raw mtime floats a merely-*opened* session to the top of the sidebar
# on the next poll. We instead read the newest ``timestamp`` among genuine conversation records
# (``user`` / ``assistant``). The type allowlist matters: ``system`` records are timestamped but can
# fire at resume (e.g. ``scheduled_task_fire``), and a future app-state record that gains a
# ``timestamp`` must not reintroduce false activity.
_ACTIVITY_TYPES = frozenset({"user", "assistant"})  # Claude conversation turns
_ACTIVITY_TAIL_BYTES = 64 * 1024  # initial tail window read from the end
_ACTIVITY_MAX_BYTES = 8 * 1024 * 1024  # cap: stop expanding + fall back to fs mtime beyond this


def last_activity_at(
    path: Path,
    *,
    types: frozenset[str] = _ACTIVITY_TYPES,
    tail_bytes: int = _ACTIVITY_TAIL_BYTES,
    max_bytes: int = _ACTIVITY_MAX_BYTES,
) -> float | None:
    """Newest ``timestamp`` among genuine conversation records in a JSONL session file, or ``None``.

    Reads the file **tail** backward in growing windows (never the whole file in the common case)
    and returns the max timestamp of records whose ``type`` is in ``types`` — the last real turn.
    Trailing timestamp-less app-state records (written by a bare resume, #525) are skipped, and the
    growing window guarantees that however many of them pile up over repeated idle opens, they can
    never hide the last conversation record and cause a silent fall-back to the (bumped) file mtime.
    ``None`` when unreadable, empty, or no conversation record exists within ``max_bytes`` — the
    caller then falls back to the filesystem mtime.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    window = tail_bytes
    try:
        with path.open("rb") as fh:
            while True:
                start = max(0, size - window)
                fh.seek(start)
                text = fh.read().decode("utf-8", errors="replace")
                lines = text.split("\n")
                if start > 0 and lines:
                    lines = lines[1:]  # first line is probably partial (window cut it) — drop it
                best: float | None = None
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("type") not in types:
                        continue
                    ts = _parse_epoch(rec.get("timestamp"))
                    if ts is not None and (best is None or ts > best):
                        best = ts
                if best is not None:
                    return best
                if start == 0 or window >= max_bytes:
                    return None  # whole file (or the cap) held no conversation record
                window = min(window * 4, max_bytes)
    except OSError:
        return None


def derive_last_activity(content_path: Path | None, st: os.stat_result) -> float:
    """Last-activity time for a file-based session: the newest conversation-record timestamp when
    available, else the filesystem mtime. This is the sidebar's Update-order key and the reaper /
    pulse activity signal — deliberately NOT the raw ``st.st_mtime``, which a bare ``claude
    --resume`` bumps by writing timestamp-less app-state records (#525). ``content_path`` is
    ``None`` for engines whose content isn't a readable JSONL — straight to the fs fallback."""
    if content_path is not None:
        ts = last_activity_at(content_path)
        if ts is not None:
            return ts
    return float(st.st_mtime)


# Claude Code records local-command scaffolding as ordinary ``type:"user"`` messages; as a
# title such a record is noise ("<local-command-caveat>Caveat: the messages below…"). Skip
# them — possibly several in a row — when picking the fallback-title candidate (#670).
_CLAUDE_WRAPPER_PREFIXES = ("<local-command-caveat", "<command-name", "<local-command-stdout")


@dataclass(frozen=True)
class Session:
    """One Claude Code session as the sidebar sees it."""

    engine: str  # "claude" (this scanner) or "opencode" (engines.OpenCodeProvider)
    uuid: str
    cwd: str
    # Last real activity — the newest ``user``/``assistant`` record timestamp (#525), NOT the raw
    # JSONL file mtime, which a bare ``claude --resume`` bumps by writing timestamp-less app-state
    # records. This is the Update-order sort key + the reaper/pulse activity signal. Falls back to
    # the fs mtime only when the file has no conversation record. (Kept named ``last_mtime`` — a
    # rename across the API + frontend is a mechanical follow-up.)
    last_mtime: float
    first_user_message: str
    archived: bool
    # When the session was created (#506), derived per engine (first-record timestamp / opencode
    # time_created / fs fallback). Defaults to 0.0 so back-compat constructors (and the rare row
    # with no derivable time) stay valid; such rows sort last in creation-date order.
    created_at: float = 0.0

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


def _decode_cwd(dirname: str) -> str:
    """Fallback cwd derivation from the encoded dir name.

    Claude Code's directory encoding replaces BOTH ``/`` and ``.`` (and any
    other non-alphanumeric) with ``-``, so the dir name is **lossy** — e.g.
    ``demoapp.io`` and ``demoapp/io`` both encode to ``...-demoapp-io``.
    We can't reverse it reliably. This is therefore only a fallback for when the
    JSONL itself has no ``cwd`` field; ``_read_session_meta`` prefers the real
    cwd recorded inside the session file.
    """
    if not dirname.startswith("-"):
        return dirname
    return "/" + dirname[1:].replace("-", "/")


def _read_session_meta(jsonl_path: Path) -> tuple[str | None, str]:
    """Single pass over a JSONL: return (real_cwd_or_None, first_user_message).

    Claude Code records carry the true ``cwd`` (e.g. ``/home/u/claude/demoapp.io``),
    which is authoritative — unlike the lossy directory name. We grab the first
    ``cwd`` we see and the first user message text, then stop. Local-command wrapper
    records (``_CLAUDE_WRAPPER_PREFIXES``) are skipped as title candidates (#670).
    """
    cwd: str | None = None
    first_msg = ""
    try:
        with jsonl_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                    cwd = rec["cwd"]
                if not first_msg and rec.get("type") == "user":
                    content = rec.get("message", {}).get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                t = (part.get("text") or "").strip()
                                if t:
                                    text = t
                                    break
                    if text and not text.startswith(_CLAUDE_WRAPPER_PREFIXES):
                        first_msg = title_candidate(text)
                if cwd is not None and first_msg:
                    break
    except OSError:
        return None, ""
    return cwd, first_msg


def _walk(root: Path, archived: bool) -> Iterable[Session]:
    if not root.is_dir():
        return
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        decoded = _decode_cwd(project_dir.name)
        for jsonl in project_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            if not _UUID_RE.match(uuid):
                continue
            try:
                st = jsonl.stat()
            except OSError:
                continue
            real_cwd, first_msg = _read_session_meta(jsonl)
            cwd = real_cwd or decoded
            yield Session(
                engine="claude",
                uuid=uuid,
                cwd=cwd,
                last_mtime=derive_last_activity(jsonl, st),
                first_user_message=first_msg,
                archived=archived,
                created_at=derive_created_at(jsonl, st),
            )


def scan(home: Path | None = None) -> list[Session]:
    """Return every Claude Code session on disk, live + archived.

    A uuid present in BOTH trees means an archived session whose live JSONL was
    recreated under ``projects/`` by a still-running ``claude`` process (#194). The
    archived copy wins, so the session stays archived instead of bouncing back into
    the active list (and the same id never appears in both scopes).
    """
    home = home or Path.home()
    live = list(_walk(home / ".claude" / "projects", archived=False))
    archive = list(_walk(home / ".claude" / "projects-archive", archived=True))
    archived_uuids = {s.uuid for s in archive}
    live = [s for s in live if s.uuid not in archived_uuids]
    return live + archive


def scanned_cwds(sessions: Iterable[Session]) -> set[str]:
    """The set of cwds that have at least one session.

    Used by the ws resume path to refuse arbitrary attacker-chosen cwds.
    """
    return {s.cwd for s in sessions}


# Ephemeral CI-runner working directories (#452). An opencode session spawned
# inside a Forgejo Actions job (nektos/act) records a throwaway ``act`` workdir
# (``<cache>/act/<hash>/hostexecutor``) as its ``directory``. That dir is deleted
# when the job ends, so the session can never be resumed — it only clutters the
# sidebar / resume allowlist / picker. Engines drop such rows at scan time.
_EPHEMERAL_CACHE_DIRS = ("act",)  # cache subdirs that hold CI scratch, not projects


def is_ephemeral_cwd(cwd: str | None, home: Path | None = None) -> bool:
    """True if ``cwd`` is an ephemeral CI-runner working directory (#452).

    Pure, **filesystem-free** path test: the rows we filter point at *already
    deleted* dirs, so ``exists`` / ``resolve(strict=True)`` / ``stat`` would be
    wrong. We normalize lexically (``os.path.normpath``) and compare whole path
    **components** — never a substring, so a real project like ``~/x/react``
    (contains the letters "act") is never hidden. ``None`` / empty → ``False``.

    A cwd is ephemeral when it equals, or sits under, an ``act`` cache root.
    Matched two ways so a path recorded by a CI process whose ``XDG_CACHE_HOME``
    / home differed from the organizer's at scan time is still caught:

    * the ``.cache``→``act`` component sequence anywhere in the path — catches a
      literal ``<home>/.cache/act/…`` regardless of the runtime cache env, and
    * the env-derived root ``$XDG_CACHE_HOME/act`` (honors a relocated cache,
      which need not contain a literal ``.cache`` segment).
    """
    if not cwd:
        return False
    home = home or Path.home()
    parts = Path(os.path.normpath(cwd)).parts

    # (1) ``.cache``/<ci-dir> as adjacent components, anywhere in the path.
    for first, second in zip(parts, parts[1:], strict=False):
        if first == ".cache" and second in _EPHEMERAL_CACHE_DIRS:
            return True

    # (2) the env-derived cache root (a relocated $XDG_CACHE_HOME need not
    #     contain a literal ``.cache`` segment, so (1) would miss it).
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    for ci_dir in _EPHEMERAL_CACHE_DIRS:
        root = (cache_root / ci_dir).parts
        if parts[: len(root)] == root:
            return True

    return False


def _scan_root_subdirs(root: Path, out: set[str]) -> None:
    """Add ``root``'s immediate sub-dirs to ``out`` (#465), keeping only children whose
    ``os.path.realpath`` resolves to a directory still under ``root`` — rejecting hidden dirs
    (``.git``, ``.claude``), symlink-out, and traversal. Generalises the historical ``~/claude``
    special-case so a fresh (session-less) folder under a configured root still surfaces."""
    if not root.is_dir():
        return
    for child in root.iterdir():
        # Skip hidden dirs (e.g. .claude, .git) — not real projects.
        if child.name.startswith("."):
            continue
        try:
            real = child.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not real.is_dir():
            continue
        # real path must remain under the root (reject symlink-out / traversal)
        if real == root or root in real.parents:
            out.add(str(real))


def pickable_projects(
    home: Path | None = None,
    sessions: Iterable[Session] | None = None,
    *,
    roots: list[str] | None = None,
    exclusions: list[str] | None = None,
) -> list[str]:
    """Folders offered by the new-session picker / Settings folder manager.

    Default (no ``roots``): scanned session cwds ∪ the immediate subdirectories of ``~/claude`` —
    the pre-#465 behaviour, unchanged. Each ``~/claude/*`` candidate is included only when
    ``os.path.realpath`` resolves to a directory whose **real path is still under ``~/claude``** —
    this rejects symlinks that point outside the tree and any traversal.

    Root-scoped (#465): when ``roots`` is a non-empty list, the ``~/claude`` special-case
    generalises to *the immediate sub-dirs of EACH root* (same hidden-dir skip + realpath-under-root
    guard), and the result is restricted to cwds that are **under a root** (boundary-aware, via
    ``project_dirs.in_scope``) — so out-of-root session cwds drop out. This is a HARD scope.

    In BOTH cases ``exclusions`` (boundary-aware path prefixes) are dropped, in addition to the
    existing ephemeral ``~/.cache/act`` filter applied at scan time.
    """
    home = home or Path.home()
    roots = roots or []
    exclusions = exclusions or []
    out: set[str] = set()
    if sessions is not None:
        out |= {s.cwd for s in sessions}
    else:
        out |= {s.cwd for s in scan(home)}

    if roots:
        # Root-scoped: surface each root's immediate sub-dirs, then keep only in-scope cwds.
        for r in roots:
            _scan_root_subdirs(Path(r), out)
        out = {c for c in out if project_dirs.in_scope(c, roots=roots, exclusions=exclusions)}
    else:
        # Unscoped (today): session cwds ∪ ~/claude subdirs, then drop the exclusions below.
        _scan_root_subdirs((home / "claude").resolve(), out)
        if exclusions:
            out = {c for c in out if not any(project_dirs.path_within(c, e) for e in exclusions)}
    return sorted(out)
