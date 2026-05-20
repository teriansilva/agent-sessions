"""Sidecar JSON for the bits Claude Code doesn't store: title, sticky, sort_key, project_alias.

Backed by ``~/.config/agent-sessions/metadata.json``. Keyed by session UUID for
v1 (Claude-only); migrates to ``<engine>:<id>`` form when the opencode follow-up
in operator-docs#61 lands.

Concurrent writers serialize on ``fcntl.flock``. Reads tolerate a write in
progress; writes take an exclusive lock for the read-modify-write window.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionMeta:
    title: str = ""
    sticky: bool = False
    sort_key: int = 0
    project_alias: str = ""


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_METADATA",
            str(Path.home() / ".config" / "agent-sessions" / "metadata.json"),
        )
    )


@contextmanager
def _exclusive(path: Path):
    """Open the file (creating it + parents if needed) with an exclusive flock.

    The yielded handle is opened in r+ mode so callers can read-then-write in
    place under the lock. **Don't** ``os.replace`` the file inside this block —
    ``fcntl.flock`` is per-inode, so a replace would break the mutex for any
    waiting writer (its handle is bound to the old inode).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    fh = path.open("r+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _rewrite_in_place(fh, data: dict) -> None:
    """Truncate + write under an already-held flock. Caller is responsible for the lock."""
    fh.seek(0)
    fh.truncate()
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.flush()
    os.fsync(fh.fileno())


def load(path: Path | None = None) -> dict[str, SessionMeta]:
    """Read sidecar; tolerate missing/empty/corrupt files by returning empty dict."""
    path = path or _default_path()
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, SessionMeta] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        out[key] = SessionMeta(
            title=str(val.get("title", "")),
            sticky=bool(val.get("sticky", False)),
            sort_key=int(val.get("sort_key", 0)),
            project_alias=str(val.get("project_alias", "")),
        )
    return out


def patch(
    uuid: str,
    **fields,
) -> SessionMeta:
    """Read-modify-write a single session's metadata under an exclusive flock.

    Returns the new SessionMeta.
    """
    path = _default_path()
    allowed = {"title", "sticky", "sort_key", "project_alias"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown metadata fields: {sorted(bad)}")

    with _exclusive(path) as fh:
        try:
            text = fh.read()
            data = json.loads(text) if text.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except json.JSONDecodeError:
            data = {}

        existing = data.get(uuid, {})
        if not isinstance(existing, dict):
            existing = {}
        meta_dict = {
            "title": existing.get("title", ""),
            "sticky": existing.get("sticky", False),
            "sort_key": existing.get("sort_key", 0),
            "project_alias": existing.get("project_alias", ""),
        }
        meta_dict.update(fields)
        data[uuid] = meta_dict
        _rewrite_in_place(fh, data)
        return SessionMeta(**meta_dict)


def get(uuid: str, path: Path | None = None) -> SessionMeta:
    return load(path).get(uuid, SessionMeta())


__all__ = ["SessionMeta", "load", "patch", "get"]
