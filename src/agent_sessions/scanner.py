"""Read Claude Code session history off disk.

Source of truth: ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`` (live) and
``~/.claude/projects-archive/<encoded-cwd>/<uuid>.jsonl`` (archived).

Each JSONL is one session. The directory name encodes the cwd by replacing
``/`` with ``-`` (Claude Code's convention). We decode back when surfacing
the cwd to the API.

Opencode is a planned second engine source (operator-docs#61); this
scanner is Claude-Code-only by design.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Session UUIDs that Claude Code writes are RFC4122-shaped.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Session:
    """One Claude Code session as the sidebar sees it."""

    engine: str  # "claude" for now; "opencode" once #61 lands
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str
    archived: bool

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


def _decode_cwd(dirname: str) -> str:
    """Claude Code encodes cwd by replacing ``/`` with ``-`` in the dir name.

    The first ``-`` represents the leading ``/`` of an absolute path.
    """
    if not dirname.startswith("-"):
        return dirname
    return "/" + dirname[1:].replace("-", "/")


def _first_user_message(jsonl_path: Path) -> str:
    """Best-effort: scan the JSONL for the first user message text."""
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
                # Claude Code writes records with shape: {"type": "user", "message": {...}}
                if rec.get("type") != "user":
                    continue
                msg = rec.get("message", {})
                content = msg.get("content")
                if isinstance(content, str):
                    return content.strip().splitlines()[0][:120]
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "").strip()
                            if text:
                                return text.splitlines()[0][:120]
                return ""
    except OSError:
        return ""
    return ""


def _walk(root: Path, archived: bool) -> Iterable[Session]:
    if not root.is_dir():
        return
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        cwd = _decode_cwd(project_dir.name)
        for jsonl in project_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            if not _UUID_RE.match(uuid):
                continue
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            yield Session(
                engine="claude",
                uuid=uuid,
                cwd=cwd,
                last_mtime=mtime,
                first_user_message=_first_user_message(jsonl),
                archived=archived,
            )


def scan(home: Path | None = None) -> list[Session]:
    """Return every Claude Code session on disk, live + archived."""
    home = home or Path.home()
    live = list(_walk(home / ".claude" / "projects", archived=False))
    archive = list(_walk(home / ".claude" / "projects-archive", archived=True))
    return live + archive


def scanned_cwds(sessions: Iterable[Session]) -> set[str]:
    """The set of cwds that have at least one session.

    Used by ``zellij.open_or_switch`` to refuse arbitrary attacker-chosen cwds.
    """
    return {s.cwd for s in sessions}
