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
from pathlib import Path

from . import project_dirs

# Session UUIDs that Claude Code writes are RFC4122-shaped.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Session:
    """One Claude Code session as the sidebar sees it."""

    engine: str  # "claude" (this scanner) or "opencode" (engines.OpenCodeProvider)
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str
    archived: bool

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
    ``cwd`` we see and the first user message text, then stop.
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
                    if isinstance(content, str):
                        first_msg = content.strip().splitlines()[0][:120] if content.strip() else ""
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text", "").strip()
                                if text:
                                    first_msg = text.splitlines()[0][:120]
                                    break
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
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            real_cwd, first_msg = _read_session_meta(jsonl)
            cwd = real_cwd or decoded
            yield Session(
                engine="claude",
                uuid=uuid,
                cwd=cwd,
                last_mtime=mtime,
                first_user_message=first_msg,
                archived=archived,
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
