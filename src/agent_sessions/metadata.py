"""Sidecar JSON for the bits Claude Code doesn't store: title, sticky, project_alias.

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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
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

# Shared color validator (#571). Lives in ``metadata`` so the SAME rule governs the
# project-color write/read path AND the per-session-color write path; the rule lives
# in exactly one place. ``validate_color(value, fail_soft=...)`` is consumed by
# ``projects._validate_color`` (write path, raises ValueError that's re-raised as
# ``ProjectError``), ``projects._from_raw`` (read path, fail-soft → ""), and the
# session-color endpoint (write path, raises ValueError → 422).
_COLOR_RE_HELP = "color must be #rgb or #rrggbb"


def validate_color(color: object, *, fail_soft: bool = False) -> str:
    """Validate + normalize a hex color. ``""`` is the canonical clear form.

    Empty/None → ``""``. A valid ``#rgb`` or ``#rrggbb`` (lower-case letters allowed)
    is normalized to lower-case hex. Anything else either raises
    ``ValueError(_COLOR_RE_HELP)`` (``fail_soft=False``, write paths) or silently
    degrades to ``""`` (``fail_soft=True``, read paths — hand-edited sidecars /
    project stores must NEVER crash the sidebar).
    """
    if color is None:
        return ""
    if not isinstance(color, str):
        if fail_soft:
            return ""
        raise ValueError(_COLOR_RE_HELP)
    c = color.strip()
    if not c:
        return ""
    if (
        c.startswith("#")
        and len(c) in (4, 7)
        and all(ch in "0123456789abcdefABCDEF" for ch in c[1:])
    ):
        return c.lower()
    if fail_soft:
        return ""
    raise ValueError(_COLOR_RE_HELP)


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
    # Custom per-session tag (#551): a short user label (free text / emoji) rendered before the
    # AI summary in the sidebar row. A SEPARATE field from the AI review output, written only by
    # the tag route, so re-review never clobbers it (same discipline as user `title` vs `ai_title`).
    tag: str = ""
    # NOTE (#520): `sort_key` (a manual ordering tiebreaker) was removed — no product flow ever
    # wrote it, so the list sort reduced to sticky-then-recency regardless. Old sidecars may still
    # carry a `sort_key` key; it is simply ignored on read. Since #571 introduced general
    # unknown-key preservation in ``patch()``, ``sort_key`` is also preserved on rewrite (no
    # drop) — the ``list_sessions`` reducer just ignores it. No migration is needed.
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
    # Chronological "what happened in this session" recap (#481), generated by the review
    # pass over the WHOLE-session transcript (not the tail the summary uses) and shown in the
    # session-brief modal. Independent of the summary fields above: its own
    # `recap_fingerprint` gates regeneration, and a failed recap call leaves the last good
    # value untouched (never rolls back the summary/intervention write, and vice-versa).
    ai_recap: str = ""
    recap_fingerprint: str = ""
    # Server-side compose draft (#477): the unsent text + pasted-image attachment pills
    # for this session's compose box, so a draft survives refresh / session switch and is
    # available cross-device (one server, one sidecar). None = no draft. Shape when set:
    # ``{"text": str, "attachments": [{"name","path"}], "updated_at": float}``. Only the
    # server-issued upload PATHS are stored — never image blobs (the route validates that
    # each path lives inside the upload namespace).
    draft: dict | None = None
    # Per-session color override (#571): a ``#rgb``/``#rrggbb`` hex string. ``""`` when unset
    # (= "no override", fall through to project / engine). The picker reads RAW ``m.color``
    # to know whether the user explicitly set a color (preserves the round-trip discipline
    # that PATCH ``""`` → row.color = ``""``); rendering surfaces consume the resolver
    # (``resolveSessionColor()`` in the SPA) which returns ``{color, source}``. Engine-agnostic
    # — rides the same sidecar as ``title``/``sticky``/``tag``, so opencode/codex/gemini get
    # it for free. Validated by ``metadata.validate_color`` (write path raises, read path
    # fail-soft normalizes invalid stored values to ``""`` so a hand-edited sidecar can
    # never 500 the sidebar).
    color: str = ""


# Schema-known field names frozen at module-import time — used by ``patch()`` to
# filter the rebuilt ``meta_dict`` to fields the dataclass accepts when constructing
# the returned ``SessionMeta``. The persisted sidecar dict is allowed to carry MORE
# fields than this set (the unknown-key preservation contract above); the dataclass
# instance is not.
_SESSIONMETA_FIELDS = tuple(fields(SessionMeta))


def has_draft(meta: SessionMeta) -> bool:
    """True when this session carries a non-empty compose draft (#477) — drives the blue
    status-dot in the sidebar. Empty text AND no attachments ⇒ no draft."""
    d = meta.draft
    return bool(isinstance(d, dict) and (str(d.get("text", "")).strip() or d.get("attachments")))


def _is_meaningful(candidate: str) -> bool:
    """Is an AUTO-DERIVED title candidate worth showing (#284)? True iff, after
    ``strip()``, it is at least 2 chars long AND carries at least one alphanumeric.
    So a stray keystroke (``"a"``), punctuation-only (``"."`` / ``".."`` / ``"--"``)
    and whitespace-only all fail, while real short prompts (``"go"`` / ``"ok"`` /
    ``"hi"``) pass. Applied ONLY to the first-user-message fallback — never to a
    user's manual rename, which is authoritative even at one char."""
    s = candidate.strip()
    return len(s) >= 2 and any(c.isalnum() for c in s)


def display_title(meta: SessionMeta, first_user_message: str) -> str:
    """THE display-title precedence (#356, fixes #284): a manual rename always wins,
    the AI title fills the gap, the first user message is the legacy fallback — but the
    auto-derived first message only counts when it's meaningful (``_is_meaningful``), so
    a freshly-created session whose first record is a stray ``"a"`` / ``"."`` resolves to
    ``""`` (an empty display title) instead of leaking that character as the name. A
    user-set ``meta.title`` is kept verbatim even at one char. Single helper so every
    row-shaping / search / filter path agrees."""
    if meta.title:
        return meta.title
    if meta.ai_title:
        return meta.ai_title
    return first_user_message if _is_meaningful(first_user_message) else ""


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


# Parsed-sidecar cache (#652 L2). The list route reads the sidecar on every keystroke-settle,
# filter switch, and 15 s poll — and used to open + ``json.load`` the whole (multi-MB) file TWICE
# per request: once for the session rows (``load``) and once for the alias map (``load_aliases``).
# Memoize the parsed dict behind an (mtime_ns, size) signature so those two calls share one parse
# and repeated requests between edits skip the read entirely. Every write goes through the flocked
# read-modify-write below and ends in an atomic ``os.replace`` (new mtime), so a stale cache is
# impossible; ``patch`` still reads the authoritative on-disk bytes under flock, never this cache.
# Keyed on ``str(path)`` (tests use a per-case tmp path, prod has one file), single entry per path.
_raw_cache_lock = threading.Lock()
_raw_cache: dict[str, tuple[int, int, dict]] = {}


def _load_raw(path: Path) -> dict:
    """The sidecar's raw parsed JSON dict, memoized on (mtime_ns, size). ``{}`` (never raises) for
    a missing / empty / corrupt / non-dict file. Callers treat the result as READ-ONLY — they
    build fresh views (``_normalize_keys`` → new dict, the alias comprehension → new dict) and
    never mutate it, so the one cached object is safe to share across threads and requests."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    sig = (st.st_mtime_ns, st.st_size)
    with _raw_cache_lock:
        hit = _raw_cache.get(key)
        if hit is not None and hit[0] == sig[0] and hit[1] == sig[1]:
            return hit[2]
    # Parse OUTSIDE the lock (a cold cache right after a write may parse twice concurrently —
    # harmless, same bytes) so a multi-MB parse never serializes concurrent list requests.
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    with _raw_cache_lock:
        _raw_cache[key] = (sig[0], sig[1], raw)
    return raw


def invalidate_raw_cache() -> None:
    """Drop the parsed-sidecar cache. Not needed for correctness (the mtime signature invalidates
    on every write), but exposed for tests and defensive callers."""
    with _raw_cache_lock:
        _raw_cache.clear()


def load(path: Path | None = None) -> dict[str, SessionMeta]:
    """Read sidecar; tolerate missing/empty/corrupt files by returning empty dict."""
    path = path or _default_path()
    raw = _load_raw(path)
    if not raw:
        return {}
    raw, _ = _normalize_keys(raw)
    out: dict[str, SessionMeta] = {}
    for key, val in raw.items():
        if key == _ALIAS_KEY:
            continue  # the alias map is not a session row — never surface it
        if not isinstance(val, dict):
            continue
        # Per-session color override (#571): read-time fail-soft normalizes any hand-edited
        # invalid value to ``""`` so a corrupted entry can never raise into the sidebar —
        # same discipline as ``draft`` (None when shape-wrong) and ``archived`` (None when
        # non-bool). The write path enforces the regex; this is the safety net.
        # ``validate_color`` accepts any scalar (None / bool / int / list → ""), so the
        # call is uniform and shape-agnostic.
        color = validate_color(val.get("color", ""), fail_soft=True)
        out[key] = SessionMeta(
            title=str(val.get("title", "")),
            sticky=bool(val.get("sticky", False)),
            tag=str(val.get("tag", "") or ""),
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
            ai_recap=str(val.get("ai_recap", "") or ""),
            recap_fingerprint=str(val.get("recap_fingerprint", "") or ""),
            draft=(val["draft"] if isinstance(val.get("draft"), dict) else None),
            color=color,
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
        # Custom per-session tag (#551) — written by the tag route, never by the review path.
        "tag",
        # "sort_key" was removed in #520 (never written by any product flow); patching it now
        # raises "unknown metadata fields", same as any other retired key.
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
        # Chronological recap (#481) — written by the review pass, never by the rename path.
        "ai_recap",
        "recap_fingerprint",
        # Compose draft (#477) — written by the draft route; a dict or None.
        "draft",
        # Per-session color override (#571) — written by the color route. Validated by
        # ``metadata.validate_color`` upstream; the route layer translates
        # ``ValueError`` → 422 with the helper string.
        "color",
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
            "tag": existing.get("tag", ""),
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
            "ai_recap": existing.get("ai_recap", ""),
            "recap_fingerprint": existing.get("recap_fingerprint", ""),
            "draft": existing.get("draft"),
            # Per-session color override (#571): persisted only when non-empty on write,
            # so a freshly-untouched row reads back as ``""`` (no override), not ``"#fff"``.
            # The ``color != ""`` truthiness gate is owned by the color route — this layer
            # just passes through whatever it received (validated upstream).
            "color": existing.get("color", ""),
        }
        # General unknown-key preservation (#571): a hand-edited or future-added field on
        # a row that the schema doesn't recognize must NOT be silently dropped by
        # ``patch()`` rewriting that row's dict. We rebuild from the schema baseline
        # (which applies defaults + type coercion + the validated write via ``fields``),
        # then carry every non-schema key forward. ``project_alias`` is schema-known
        # (it's persisted in the baseline above) but is NOT in ``allowed``; treating it
        # as schema-known here is correct — we don't want the rewrite to surface a
        # shadow row by surprise, only to preserve any other unmodeled keys.
        known = set(meta_dict)
        meta_dict.update({k: v for k, v in existing.items() if k not in known})
        meta_dict.update(fields)
        # Per-session color normalization (#571): route callers pre-validate via
        # ``metadata.validate_color``, but ``patch()`` is also called from tests /
        # internal paths that may not. Re-run the shared validator AFTER applying
        # ``fields`` so storage is always canonical (``#5FD7FF`` → ``#5fd7ff``) —
        # defense-in-depth that costs one cheap regex and avoids surprises in
        # read-back assertions. If the caller passed an invalid value, this re-raises
        # the same ``ValueError`` the route layer would have raised.
        if "color" in fields:
            meta_dict["color"] = validate_color(fields["color"])
        data[key] = meta_dict
        _rewrite_in_place(fh, data)
        # The persisted sidecar dict MAY carry unmodeled keys (the preservation
        # contract above), but the SessionMeta dataclass only knows schema fields.
        # Construct it from the schema-known subset — the unmodeled keys survive on
        # disk via the persisted ``data[key]`` above, never in the returned object.
        schema_field_names = {f.name for f in _SESSIONMETA_FIELDS}
        return SessionMeta(**{k: v for k, v in meta_dict.items() if k in schema_field_names})


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
    # Shares the #652 L2 parse cache with ``load`` — within one list request the second call is a
    # cache hit, so the sidecar is parsed once, not twice.
    raw = _load_raw(path)
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
    "has_draft",
    "display_title",
    "load",
    "patch",
    "get",
    "resolve_key",
    "load_aliases",
    "set_alias",
]
