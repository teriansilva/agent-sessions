"""Read Claude Code session history off disk.

Source of truth: ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`` (live) and
``~/.claude/projects-archive/<encoded-cwd>/<uuid>.jsonl`` (archived).

Each JSONL is one session. The directory name is a **lossy** encoding of the
cwd — Claude Code replaces ``/`` *and* ``.`` (and other non-alphanumerics)
with ``-``, so ``example-app`` and ``example-app/io`` both become
``...-example-app-io`` and can't be told apart. The authoritative cwd is the
``cwd`` field recorded inside the JSONL; we read that and only fall back to
decoding the directory name when a file has no ``cwd`` field. This matters
because the cwd feeds the open-session allowlist + ``zellij --cwd``.

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
    """Fallback cwd derivation from the encoded dir name.

    Claude Code's directory encoding replaces BOTH ``/`` and ``.`` (and any
    other non-alphanumeric) with ``-``, so the dir name is **lossy** — e.g.
    ``example-app`` and ``example-app/io`` both encode to ``...-example-app-io``.
    We can't reverse it reliably. This is therefore only a fallback for when the
    JSONL itself has no ``cwd`` field; ``_read_session_meta`` prefers the real
    cwd recorded inside the session file.
    """
    if not dirname.startswith("-"):
        return dirname
    return "/" + dirname[1:].replace("-", "/")


def _read_session_meta(jsonl_path: Path) -> tuple[str | None, str]:
    """Single pass over a JSONL: return (real_cwd_or_None, first_user_message).

    Claude Code records carry the true ``cwd`` (e.g. ``/home/u/claude/example-app``),
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
