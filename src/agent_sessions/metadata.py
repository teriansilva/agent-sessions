"""Sidecar JSON for the bits Claude Code doesn't store: title, sticky, sort_key, project_alias.

Backed by ``~/.config/agent-sessions/metadata.json``. Keyed by the engine-qualified
session id ``<engine>:<native_id>`` (e.g. ``claude:<uuid>``). Pre-multi-engine
bare-UUID keys (#11) are normalized to ``claude:<uuid>`` on read, and rewritten in
canonical form after a one-time ``.bak`` backup so a botched migration is reversible.

Concurrent writers serialize on ``fcntl.flock``. Reads tolerate a write in
progress; writes take an exclusive lock for the read-modify-write window.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_CLAUDE_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Reserved top-level key in the sidecar JSON for the placeholder→real session-id alias
# map (#127). It's NOT a session row: ``load`` skips it, so it never leaks into the
# list. The value is ``{placeholder_key: real_key}`` where each side is an
# engine-qualified id (``opencode:new-<uuid>`` → ``opencode:ses_…``). Persisting it in
# the sidecar is what lets an alias survive an app restart: after a restart the dtach
# socket / lock still live under the *placeholder* key, so an attach by the real id must
# resolve back to the placeholder, and a freshly-loaded app reads the alias to do so.
_ALIAS_KEY = "__aliases__"


def _normalize_keys(data: dict) -> tuple[dict, bool]:
    """Map pre-multi-engine bare-UUID keys to ``claude:<uuid>``.

    Returns ``(normalized, changed)``. Already-qualified keys (containing ``:``)
    and non-UUID keys are left untouched, so this is a no-op for current data. The
    reserved ``__aliases__`` key (#127) is passed through verbatim.
    """
    out: dict = {}
    changed = False
    for k, v in data.items():
        if k == _ALIAS_KEY:
            out[k] = v
            continue
        nk = f"claude:{k}" if (":" not in k and _CLAUDE_UUID_RE.match(k)) else k
        changed = changed or nk != k
        out[nk] = v
    return out, changed


@dataclass
class SessionMeta:
    title: str = ""
    sticky: bool = False
    sort_key: int = 0
    # Legacy per-session display-name override for a cwd. RETIRED from the write path
    # by #361 (project entities supersede it); still read one release as the folder-ref
    # name fallback for sessions the one-shot alias→entity migration never saw.
    project_alias: str = ""
    # Explicit project assignment (#361): the id of a project entity in projects.json.
    # "" = unassigned (resolution falls back to adopted-folder matching, then to the
    # implicit folder group). A dangling id (deleted project) is ignored on read.
    project_id: str = ""
    # App-side archive override for engines whose store we treat as read-only
    # (opencode.db, codex rollouts). Tri-state: None = no override (use the engine's
    # native archived state); True/False = explicit override in *both* directions —
    # so a row already archived natively (opencode.db time_archived) can be unarchived.
    # Claude archives by moving its JSONL and never writes this, so it stays None.
    archived: bool | None = None
    # AI session review (#356). `ai_title` is a SEPARATE field from `title` on purpose:
    # the reviewer never overwrites a user's manual rename — display precedence is
    # resolved by `display_title` (user title → ai_title → first user message).
    ai_summary: str = ""
    ai_title: str = ""
    intervention_required: bool = False
    intervention_reason: str = ""
    # Wall-clock of the last SUCCESSFUL review. A failed review never touches these
    # fields, so the last good result stays — visibly stale via this timestamp — rather
    # than masquerading as fresh (#356 staleness semantics).
    reviewed_at: float | None = None
    # Input fingerprint captured at review time; the scheduler (#356 Phase 2) re-reviews
    # only when the current fingerprint differs.
    review_fingerprint: str = ""
    review_excluded: bool = False


def display_title(meta: SessionMeta, first_user_message: str) -> str:
    """THE display-title precedence (#356, mitigates #284): a manual rename always wins,
    the AI title fills the gap, the first user message is the legacy fallback. Single
    helper so every row-shaping / search / filter path agrees."""
    return meta.title or meta.ai_title or first_user_message


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
    raw, _ = _normalize_keys(raw)
    out: dict[str, SessionMeta] = {}
    for key, val in raw.items():
        if key == _ALIAS_KEY:
            continue  # the alias map is not a session row — never surface it
        if not isinstance(val, dict):
            continue
        out[key] = SessionMeta(
            title=str(val.get("title", "")),
            sticky=bool(val.get("sticky", False)),
            sort_key=int(val.get("sort_key", 0)),
            project_alias=str(val.get("project_alias", "")),
            project_id=str(val.get("project_id", "") or ""),
            archived=(val["archived"] if isinstance(val.get("archived"), bool) else None),
            ai_summary=str(val.get("ai_summary", "") or ""),
            ai_title=str(val.get("ai_title", "") or ""),
            intervention_required=bool(val.get("intervention_required", False)),
            intervention_reason=str(val.get("intervention_reason", "") or ""),
            reviewed_at=(
                float(val["reviewed_at"])
                if isinstance(val.get("reviewed_at"), int | float)
                and not isinstance(val.get("reviewed_at"), bool)
                else None
            ),
            review_fingerprint=str(val.get("review_fingerprint", "") or ""),
            review_excluded=bool(val.get("review_excluded", False)),
        )
    return out


def patch(
    key: str,
    **fields,
) -> SessionMeta:
    """Read-modify-write a single session's metadata under an exclusive flock.

    ``key`` is the engine-qualified id (``<engine>:<native_id>``). Returns the new
    SessionMeta.
    """
    path = _default_path()
    allowed = {
        "title",
        "sticky",
        "sort_key",
        # "project_alias" is deliberately ABSENT: write path retired by #361 (the
        # alias→entity migration); existing values are preserved on rewrite below.
        "project_id",
        "archived",
        # AI review fields (#356) — written by review.py / the exclude toggle, never by
        # the rename path, so a review can't clobber a user's title.
        "ai_summary",
        "ai_title",
        "intervention_required",
        "intervention_reason",
        "reviewed_at",
        "review_fingerprint",
        "review_excluded",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown metadata fields: {sorted(bad)}")

    with _exclusive(path) as fh:
        try:
            text = fh.read()
            data = json.loads(text) if text.strip() else {}
            if not isinstance(data, dict):
                text, data = "", {}
        except json.JSONDecodeError:
            text, data = "", {}

        # One-time migration of legacy bare-UUID keys → claude:<uuid>, backing the
        # original file up once before the first canonical rewrite.
        data, migrated = _normalize_keys(data)
        if migrated:
            bak = path.with_name(path.name + ".bak")
            if not bak.exists():
                bak.write_text(text)

        existing = data.get(key, {})
        if not isinstance(existing, dict):
            existing = {}
        meta_dict = {
            "title": existing.get("title", ""),
            "sticky": existing.get("sticky", False),
            "sort_key": existing.get("sort_key", 0),
            "project_alias": existing.get("project_alias", ""),
            "project_id": existing.get("project_id", ""),
            "archived": existing.get("archived"),
            "ai_summary": existing.get("ai_summary", ""),
            "ai_title": existing.get("ai_title", ""),
            "intervention_required": existing.get("intervention_required", False),
            "intervention_reason": existing.get("intervention_reason", ""),
            "reviewed_at": existing.get("reviewed_at"),
            "review_fingerprint": existing.get("review_fingerprint", ""),
            "review_excluded": existing.get("review_excluded", False),
        }
        meta_dict.update(fields)
        data[key] = meta_dict
        _rewrite_in_place(fh, data)
        return SessionMeta(**meta_dict)


def get(key: str, path: Path | None = None) -> SessionMeta:
    return load(path).get(key, SessionMeta())


def resolve_key(key: str, path: Path | None = None) -> str:
    """The sidecar key a metadata write/read for ``key`` should target (Hermes on PR #367).

    For a reconciled opencode/codex session the row id is the LOGICAL real id, while
    metadata set before reconcile (title/sticky/archive) lives under the PLACEHOLDER
    physical key (#127). The list read path prefers the logical entry
    (``meta_index.get(key) or meta_index.get(phys)``), so a write that blindly creates a
    sparse logical-key sidecar would SHADOW the physical one — hiding the existing
    title/sticky/archive state. Resolution rule (single source of truth, mirroring the
    read precedence): an existing logical entry wins; else an existing physical entry;
    else the logical key (fresh sidecar).
    """
    index = load(path)
    if key in index:
        return key
    # Lazy import: the engines package imports this module at init, so a top-level
    # import here would be circular. By call time both modules are loaded.
    from . import engines

    phys = engines.physical_key(key, load_aliases(path))
    if phys != key and phys in index:
        return phys
    return key


def load_aliases(path: Path | None = None) -> dict[str, str]:
    """The persisted ``placeholder_key → real_key`` alias map (#127).

    Both sides are engine-qualified ids. Fail-soft: a missing / corrupt sidecar or a
    malformed alias section yields ``{}`` (no aliasing) rather than an error — the worst
    case is a reconciled session momentarily showing under its placeholder again, never
    a wrong attach. Only well-formed ``str → str`` entries are returned.
    """
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
    aliases = raw.get(_ALIAS_KEY)
    if not isinstance(aliases, dict):
        return {}
    return {k: v for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}


def set_alias(placeholder_key: str, real_key: str) -> None:
    """Record ``placeholder_key → real_key`` in the sidecar under an exclusive flock (#127).

    Idempotent. Stored in the same file as session metadata so it survives an app
    restart; the session-row read path skips the reserved alias section, so it never
    pollutes the list.
    """
    if not placeholder_key or not real_key:
        raise ValueError("empty alias key")
    path = _default_path()
    with _exclusive(path) as fh:
        try:
            text = fh.read()
            data = json.loads(text) if text.strip() else {}
            if not isinstance(data, dict):
                text, data = "", {}
        except json.JSONDecodeError:
            text, data = "", {}
        data, migrated = _normalize_keys(data)
        if migrated:
            bak = path.with_name(path.name + ".bak")
            if not bak.exists():
                bak.write_text(text)
        aliases = data.get(_ALIAS_KEY)
        if not isinstance(aliases, dict):
            aliases = {}
        aliases[placeholder_key] = real_key
        data[_ALIAS_KEY] = aliases
        _rewrite_in_place(fh, data)


__all__ = [
    "SessionMeta",
    "display_title",
    "load",
    "patch",
    "get",
    "resolve_key",
    "load_aliases",
    "set_alias",
]
