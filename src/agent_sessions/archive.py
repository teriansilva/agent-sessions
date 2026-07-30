"""Archive / unarchive a session by moving its JSONL between
``~/.claude/projects/`` and ``~/.claude/projects-archive/``.

The scanner already reads both trees and tags archived sessions, so archiving
is purely a file move that preserves the ``<encoded-cwd>/<uuid>.jsonl`` layout.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ArchiveError(RuntimeError):
    pass


def _roots(home: Path | None) -> tuple[Path, Path]:
    home = home or Path.home()
    return home / ".claude" / "projects", home / ".claude" / "projects-archive"


def _find(root: Path, uuid: str) -> Path | None:
    """Locate ``<root>/<encoded-cwd>/<uuid>.jsonl``. Returns None if absent."""
    if not root.is_dir():
        return None
    matches = list(root.glob(f"*/{uuid}.jsonl"))
    return matches[0] if matches else None


def _move(uuid: str, src_root: Path, dst_root: Path, *, what: str) -> str:
    if not _UUID_RE.match(uuid):
        raise ArchiveError(f"bad uuid: {uuid!r}")
    src = _find(src_root, uuid)
    if src is None:
        raise ArchiveError(f"session {uuid} not found to {what}")
    # Preserve the encoded-cwd parent dir name in the destination.
    dst_dir = dst_root / src.parent.name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    return str(dst)


def archive(uuid: str, home: Path | None = None) -> str:
    """Move a live session into the archive tree. Returns the new path."""
    projects, archive_root = _roots(home)
    return _move(uuid, projects, archive_root, what="archive")


def unarchive(uuid: str, home: Path | None = None) -> str:
    """Move an archived session back into the live tree. Returns the new path."""
    projects, archive_root = _roots(home)
    return _move(uuid, archive_root, projects, what="unarchive")


__all__ = ["archive", "unarchive", "ArchiveError"]
