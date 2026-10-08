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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from . import assessment


def _legacy_bare_engine():
    """``(engine_id, id_pattern)`` of the ONE engine whose manifest claims `legacy_bare_id` — the
    engine every pre-multi-engine bare key belongs to (#853 P3) — or None when no loaded manifest
    claims it, in which case bare keys are left exactly as they are rather than guessed at."""
    from .engines import registry

    eid = registry._BARE_ID_ENGINE
    prov = registry.get_any(eid) if eid else None
    return (eid, prov.id_pattern) if prov is not None else None


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
    """Map pre-multi-engine bare-UUID keys to ``<legacy engine>:<uuid>`` (``claude:<uuid>``).

    Returns ``(normalized, changed)``. Already-qualified keys (containing ``:``)
    and non-UUID keys are left untouched, so this is a no-op for current data. The
    reserved ``__aliases__`` key (#127) is passed through verbatim.
    """
    out: dict = {}
    changed = False
    legacy = _legacy_bare_engine()
    for k, v in data.items():
        if k == _ALIAS_KEY:
            out[k] = v
            continue
        nk = (
            f"{legacy[0]}:{k}"
            if (legacy is not None and ":" not in k and legacy[1].fullmatch(k))
            else k
        )
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
    # Pulse orchestrator opt-out (#726). Managed-by-default: every session is in the
    # orchestrator's world unless this is set. DELIBERATELY SEPARATE from
    # ``review_excluded``, which is broader and wrong for this — ``pulse.build_cards``
    # drops review-excluded sessions from the card set entirely, so reusing it would also
    # remove the session from Pulse and Ask. This flag is narrower: the session stays
    # visible, stays summarised, stays flagged ``needs_you``; it only stops being something
    # the orchestrator may propose against or write to. (``review_excluded`` still implies
    # orchestrator-excluded transitively, since such a session never reaches the card set
    # the digest is built from.)
    orchestrator_excluded: bool = False
    # Chronological "what happened in this session" recap (#481), generated by the review
    # pass over the WHOLE-session transcript (not the tail the summary uses) and shown in the
    # session-brief modal. Independent of the summary fields above: its own
    # `recap_fingerprint` gates regeneration, and a failed recap call leaves the last good
    # value untouched (never rolls back the summary/intervention write, and vice-versa).
    ai_recap: str = ""
    recap_fingerprint: str = ""
    # Structured assessment (#1020): the bounded, versioned record `assessment.record` builds from
    # the tail review's reply — current state, blocker, decision needed, task, constraints, with
    # the source fingerprint and read time it describes. None = no record (unknown). Validated by
    # `assessment.from_stored` on BOTH write (`patch`) and read, so a stored row can never carry an
    # unbounded or misshapen record. Read through `assessment.project`, which decides freshness.
    ai_assessment: dict | None = None
    # When the last review attempt FAILED after reading its input (#1020); cleared by a success.
    # The one field a failed review writes: the last good results stay, and the assessment
    # projection uses this to say they were not refreshed.
    review_failed_at: float | None = None
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
    # Cross-engine handoff provenance (#597) — engine-qualified ids + ISO-8601 timestamp,
    # written by ``handoff.py`` only AFTER the target spawn passes the aliveness gate.
    # PROVENANCE ONLY: the handoff seed text itself is never persisted anywhere. A stale
    # half (target archived, source deleted) is tolerated at read time — the row just
    # carries the string; nothing dereferences it blindly.
    handoff_from: str = ""
    handoff_to: str = ""
    handoff_mode: str = ""
    handoff_at: str = ""
    # The model a launch ASKED for (#1189): the canonical id `model_choice` resolved, written only
    # when a launch requested a non-default model. "" = default or never recorded, and "" is never
    # taken to match anything. Keyed like every sidecar field, so a late-id session's placeholder
    # entry carries it through adoption (`requested_model`). A REQUEST, never evidence of what ran:
    # that is `transcript.effective_model`.
    model_requested: str = ""


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


# Fallback-title normalization (#670): ONE rule for every engine's first-user-message
# fallback — a single bounded line. Applied HERE, at display time, never to the stored
# ``Session.first_user_message``, which stays raw because it doubles as the ``/api/sessions``
# search haystack and diagnostic value (Hermes on PR #672): normalizing it at scan time made
# any term after the first line / 120th character unsearchable.
TITLE_FALLBACK_MAX = 120


def title_candidate(text: str) -> str:
    """Normalize a raw first-user-message into a fallback-title candidate: strip, keep the
    first line, cap at ``TITLE_FALLBACK_MAX`` chars. ``""`` stays ``""`` (→ ``(untitled)``)."""
    s = (text or "").strip()
    return s.splitlines()[0][:TITLE_FALLBACK_MAX] if s else ""


def display_title(meta: SessionMeta, first_user_message: str) -> str:
    """THE display-title precedence (#356, fixes #284): a manual rename always wins,
    the AI title fills the gap, the first user message is the legacy fallback — but the
    auto-derived first message only counts when it's meaningful (``_is_meaningful``), so
    a freshly-created session whose first record is a stray ``"a"`` / ``"."`` resolves to
    ``""`` (an empty display title) instead of leaking that character as the name. A
    user-set ``meta.title`` is kept verbatim even at one char. Single helper so every
    row-shaping / search / filter path agrees. The fallback is title-normalized here
    (``title_candidate``, #670) — the raw message stays searchable upstream."""
    if meta.title:
        return meta.title
    if meta.ai_title:
        return meta.ai_title
    candidate = title_candidate(first_user_message)
    return candidate if _is_meaningful(candidate) else ""


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


def archive_override_under_lock(key: str, path: Path | None = None) -> str:
    """What the SIDECAR says about `key`'s archive state — `unset`/`active`/`archived`/`unreadable`.

    `load()` deliberately collapses missing, empty and corrupt into `{}`, because for the list
    surfaces "no metadata" and "unreadable metadata" produce the same rows, and failing them would
    take the sidebar down over a sidecar. That is the right trade there and the wrong one at an
    authorization boundary (#896 reviews 16 and 17): the ADOPT gate refuses an ARCHIVED session,
    and for the engines whose archive lives only in the sidecar an unreadable one answers "not
    archived" — so the gate fails OPEN exactly when it cannot see.

    Two states have to be told apart from "empty", and neither is visible to a lock-free read:

    * a **corrupt** file — bytes that do not parse;
    * an **ordinary concurrent write**. `patch()` truncates in place and then serializes, so every
      edit has a window in which the file exists and is ZERO BYTES. A reader that reads "empty"
      there concludes there is no archive override, and an archived session becomes adoptable for
      as long as somebody else's write takes.

    So this takes the same exclusive flock every writer takes — the read equivalent of `patch()`'s
    own "read the authoritative on-disk bytes under flock, never this cache". Deliberately NOT on
    the list path, which must stay lock-free and fail soft.

    **FOUR answers, because the sidecar can say four different things** (review 29). It used to
    say two, and the pair it conflated was the damaging one: "nobody has recorded an override"
    and "the operator explicitly set this session ACTIVE" both came back `False`, so the caller
    could not tell them apart and went on to ask the engine's own store in both cases. For an
    engine whose store is READ-ONLY — opencode — unarchiving deliberately writes only the sidecar
    and leaves the native `time_archived` alone, so the engine still reports archived and an
    explicitly unarchived session could never be adopted.

    * ``unset``      — read, and silent about this session. The engine's own view decides.
    * ``active``     — an explicit `archived: false`. The operator has SAID so, and the override
      is the app's own precedence, so nothing else gets a vote.
    * ``archived``   — an explicit `archived: true`.
    * ``unreadable`` — the file could not be read, did not parse, or the row EXISTS and is
      damaged (not an object, or an `archived` that is not a boolean). Not "not archived"
      (review 28, finding 2): it is a session whose archive state nobody can determine.
    """
    path = path or _default_path()
    try:
        if not path.exists():
            return "unset"  # no sidecar is an empty sidecar, and that is a real answer
        with _exclusive(path) as fh:
            body = fh.read()
    except OSError:
        return "unreadable"
    return _state_from_body(body, key)


def _state_from_body(body: str, key: str) -> str:
    """``unset``/``active``/``archived``/``unreadable`` for ``key`` from the sidecar's raw bytes.

    Shared by every reader that takes the lock, so they cannot drift in how they read one.
    """
    if not body.strip():
        # Zero bytes UNDER THE LOCK is not a write in progress — a writer would still be holding
        # it — so this is a genuinely empty file, which is what `touch` leaves behind.
        return "unset"
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return "unreadable"
    if not isinstance(raw, dict):
        return "unreadable"
    raw, _ = _normalize_keys(raw)
    # ABSENT AND MALFORMED ARE OPPOSITE FACTS (#896 review 28, finding 2), and this collapsed them
    # into `False` — the one answer the adoption gate reads as permission to continue. A row that
    # is not a row, or an `archived` that is not a boolean, is damage: the sidecar was read and
    # this session's archive state could not be determined from it. Answering "definitively not
    # archived" there let a sidecar-only archived session with a surviving live writer be adopted
    # into a running mission. Absence is still a real answer, because a sidecar that has never
    # been told about a session is not asserting anything about it.
    return archive_state_of_row(raw, key)


def archive_state_of_row(raw: dict, key: str) -> str:
    """``unset``/``active``/``archived``/``unreadable`` for one key of already-parsed sidecar JSON.

    The archive-relevant shape check, kept **separate from the display decoder** and applied to the
    RAW row. ``_index_from_raw`` is fail-soft by design — it drops a row that is not an object and
    turns a non-boolean ``archived`` into ``None`` — and both of those reach a destructive caller as
    "nobody recorded an override", which then defers to the engine's own flag. For an unarchived
    opencode session that flag still says archived, so a DAMAGED override became permission to
    delete (Hermes on PR #1000, review 4898). Damage is not absence.
    """
    if key not in raw:
        return "unset"  # no row at all: read, and silent about this session
    row = raw[key]
    if not isinstance(row, dict):
        return "unreadable"  # there IS a row and it is not one
    if "archived" not in row:
        return "unset"  # a row with no override recorded, which is the ordinary shape
    flag = row["archived"]
    if flag is None:
        # `null` is how the app itself records "no override": `patch()` persists the whole
        # `SessionMeta`, whose `archived` is `None` for any session merely renamed, coloured or
        # reviewed. Treating it as damage swept 146 of 919 rows on the author's own sidecar into
        # fail-closed and flooded every preview with problems. Absent key and explicit null are
        # the same statement — nobody has said anything about this session.
        return "unset"
    if not isinstance(flag, bool):
        return "unreadable"  # a value nobody can interpret is not "not archived"
    return "archived" if flag else "active"


class MetadataUnreadable(RuntimeError):
    """The sidecar EXISTS but what it says could not be established — it would not open, did not
    parse, or is not an object.

    Distinct from an ABSENT sidecar, which is a real answer ("nothing is overridden"). Raised only
    by :func:`load_checked`, for callers whose decision is destructive; every display surface keeps
    the fail-soft readers.
    """


@contextmanager
def archive_state_held(*keys: str, path: Path | None = None):
    """Yield the archive state for ``keys`` **while still holding** the sidecar's exclusive lock.

    :func:`archive_override_under_lock` answers and then lets go, which is enough to *check* and
    not enough to *act*: an archive or unarchive can commit in the gap between the answer and the
    deletion it authorises (Hermes on PR #1000, review 4898). Every writer goes through ``patch()``
    under this same flock, so a caller that deletes inside this block sees any transition that has
    already committed and blocks any that has not.

    **Several keys, in precedence order, because a session has more than one identity.** The
    override for an aliased OpenCode session is written under the LOGICAL (real) id by
    ``unarchive``, while the scrollback mirror is keyed by the PHYSICAL id — so a fence that knew
    only the physical key read `unset` and deleted an active session's cache (review 4915/4919,
    finding 1). The first key with an explicit state wins, matching discovery's own
    `overrides.get(logical) or overrides.get(physical)` precedence; `unreadable` is explicit and
    therefore blocks.

    **The lock is taken even when the sidecar does not exist yet.** Yielding `unset` on absence
    without locking left first-creation unfenced: a natively-archived session legitimately reaches
    that branch before BattleLab has ever written a sidecar, and another thread could create the
    file and commit an unarchive inside the yielded block (finding 2). ``_exclusive`` creates the
    file, so a prune may leave an empty ``metadata.json`` on a fresh install — a far better trade
    than a race, and an empty sidecar reads as `unset` exactly as absence did.

    Hold it for one session and one act. Holding it across a whole sweep would stall every archive,
    unarchive and rename in the app for the length of that sweep.

    The lock is entered manually rather than with ``with``, so that an ``OSError`` raised by the
    CALLER's body can never be mistaken for a failure to read the sidecar.
    """
    path = path or _default_path()
    cm = _exclusive(path)
    try:
        fh = cm.__enter__()
    except OSError:
        yield "unreadable"
        return
    try:
        try:
            body = fh.read()
        except OSError:
            state = "unreadable"
        else:
            state = "unset"
            for key in keys:
                candidate = _state_from_body(body, key)
                if candidate != "unset":
                    state = candidate  # first explicit answer wins; `unreadable` is explicit
                    break
        yield state
    finally:
        cm.__exit__(None, None, None)


def load_checked(
    path: Path | None = None,
) -> tuple[dict[str, SessionMeta], dict[str, str], dict[str, str]]:
    """``(index, aliases, overrides)`` from ONE locked read, raising :class:`MetadataUnreadable`
    rather than answering "nothing is overridden" for a sidecar it could not read.

    ``overrides`` maps key → ``unset``/``active``/``archived``/``unreadable``, computed from the
    RAW rows by :func:`archive_state_of_row`. It exists because ``index`` is built by the display
    decoder, which is fail-soft about exactly the shapes that matter here: it drops a row that is
    not an object and turns a non-boolean ``archived`` into ``None``, both of which read downstream
    as "no override" and defer to the engine's own flag. A destructive caller must use
    ``overrides``; ``index`` remains for display-shaped callers.

    ``load()`` / ``load_aliases()`` collapse missing, corrupt and mid-write-empty into ``{}``. That
    is right for the list surfaces — a sidecar should never take the sidebar down — and wrong
    wherever the answer authorizes something irreversible (#896 reviews 16/17/28/29 drew the same
    line for the ADOPT gate; this is the same line for #993's prune).

    Two properties it does NOT share with ``load()``, both load-bearing:

    * it reads **under the exclusive flock every writer takes**, because ``patch()`` truncates in
      place before it serializes — so a lock-free reader has a window where the file exists and is
      ZERO BYTES, and reads "no overrides" while an override is being written;
    * it returns the session index and the alias map **together**, from that one read, so the two
      halves of an eligibility decision cannot come from different moments.

    An absent sidecar still returns empties: nothing has been overridden, which is knowable.
    """
    path = path or _default_path()
    # `_exclusive` touches the file into existence, so ask FIRST — a read-only preview must not
    # create the operator's sidecar as a side effect.
    if not path.exists():
        return {}, {}, {}
    try:
        with _exclusive(path) as fh:
            body = fh.read()
    except OSError as e:
        raise MetadataUnreadable(f"{type(e).__name__}: {e.strerror or e}") from e
    if not body.strip():
        # Zero bytes UNDER THE LOCK is not a write in progress — a writer would still hold it.
        return {}, {}, {}
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        raise MetadataUnreadable(f"the sidecar did not parse ({e})") from e
    if not isinstance(raw, dict):
        raise MetadataUnreadable("the sidecar is not an object")
    raw, _ = _normalize_keys(raw)
    overrides = {key: archive_state_of_row(raw, key) for key in raw if key != _ALIAS_KEY}
    return _index_from_raw(raw, normalized=True), _aliases_from_raw(raw), overrides


def _aliases_from_raw(raw: dict) -> dict[str, str]:
    """The well-formed ``str → str`` entries of the reserved alias section, or ``{}``."""
    aliases = raw.get(_ALIAS_KEY)
    if not isinstance(aliases, dict):
        return {}
    return {k: v for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}


def load(path: Path | None = None) -> dict[str, SessionMeta]:
    """Read sidecar; tolerate missing/empty/corrupt files by returning empty dict."""
    path = path or _default_path()
    raw = _load_raw(path)
    if not raw:
        return {}
    return _index_from_raw(raw)


def _index_from_raw(raw: dict, *, normalized: bool = False) -> dict[str, SessionMeta]:
    """Build the session index from already-parsed sidecar JSON. Shared by ``load`` and
    ``load_checked`` so the two can never drift in how a row is interpreted."""
    if not normalized:
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
            orchestrator_excluded=bool(val.get("orchestrator_excluded", False)),
            ai_recap=str(val.get("ai_recap", "") or ""),
            recap_fingerprint=str(val.get("recap_fingerprint", "") or ""),
            ai_assessment=assessment.from_stored(val.get("ai_assessment")),
            review_failed_at=assessment.finite_number(val.get("review_failed_at")),
            draft=(val["draft"] if isinstance(val.get("draft"), dict) else None),
            color=color,
            # Handoff provenance (#597) — plain strings; read-time fail-soft like the
            # rest (a hand-edited non-string normalizes to "" rather than raising).
            handoff_from=str(val.get("handoff_from", "") or ""),
            handoff_to=str(val.get("handoff_to", "") or ""),
            handoff_mode=str(val.get("handoff_mode", "") or ""),
            handoff_at=str(val.get("handoff_at", "") or ""),
            model_requested=_model_or_blank(val.get("model_requested")),
        )
    return out


def _model_or_blank(value: object) -> str:
    """Read-time fail-soft: a hand-edited value that is not model-shaped reads as "" (unrecorded),
    which matches nothing — never as a model id a resume could be judged against."""
    from .plugins.manifest import _MODEL_ID_RE

    return value if isinstance(value, str) and _MODEL_ID_RE.fullmatch(value) else ""


def requested_model(key: str, path: Path | None = None) -> str:
    """The recorded requested model for ``key``, following a late-id session's placeholder alias
    (#127): the logical entry first, then the physical one. "" when none is recorded.

    The `default` marker (an explicit default resume replaced the record, #1189) reads as "" —
    and, on the logical entry, stops the fall-through, so a placeholder's older model never
    resurfaces."""
    return requested_model_in(load(path), load_aliases(path), key)


def requested_model_in(index: dict[str, SessionMeta], aliases: dict[str, str], key: str) -> str:
    """`requested_model` over an index + alias map the caller already loaded — THE one precedence
    (logical entry first, then the physical one; the `default` marker reads as "") for every
    reader, so a session row and a resume can never disagree about the request. A row must not
    read `model_requested` off whichever entry it took as a whole: a rename creates the logical
    entry WITHOUT the field, and the placeholder's record lives on the physical one."""
    from .plugins.manifest import MODEL_DEFAULT

    m = index.get(key)
    if m is not None and m.model_requested:
        return "" if m.model_requested == MODEL_DEFAULT else m.model_requested
    from . import engines

    phys = engines.physical_key(key, aliases)
    pm = index.get(phys) if phys != key else None
    got = pm.model_requested if pm is not None else ""
    return "" if got == MODEL_DEFAULT else got


def patch(
    key: str,
    *,
    failed_review_started_at: float | None = None,
    **fields,
) -> SessionMeta:
    """Read-modify-write a single session's metadata under an exclusive flock.

    ``key`` is the engine-qualified id (``<engine>:<native_id>``). Returns the new
    SessionMeta. A failure-only write may supply ``failed_review_started_at``: a success
    committed since that attempt began wins, checked under this same exclusive lock.
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
        # Pulse orchestrator opt-out (#726) — written by the orchestrator-exclude toggle.
        "orchestrator_excluded",
        # Chronological recap (#481) — written by the review pass, never by the rename path.
        "ai_recap",
        "recap_fingerprint",
        # Structured assessment + failed-refresh stamp (#1020) — written by the review pass only.
        "ai_assessment",
        "review_failed_at",
        # Compose draft (#477) — written by the draft route; a dict or None.
        "draft",
        # Per-session color override (#571) — written by the color route. Validated by
        # ``metadata.validate_color`` upstream; the route layer translates
        # ``ValueError`` → 422 with the helper string.
        "color",
        # Cross-engine handoff provenance (#597) — written by handoff.py's post-aliveness
        # transitions only, never by any user-facing rename/tag path.
        "handoff_from",
        "handoff_to",
        "handoff_mode",
        "handoff_at",
        # The requested model (#1189) — written by `model_choice.record` only, after the launch
        # path validated it; never by a user-facing rename/tag path.
        "model_requested",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown metadata fields: {sorted(bad)}")
    if failed_review_started_at is not None and (
        set(fields) != {"review_failed_at"}
        or assessment.finite_number(failed_review_started_at) is None
    ):
        raise ValueError("failed_review_started_at is only valid for a failed review timestamp")
    if fields.get("review_failed_at") is not None:
        stamp = assessment.finite_number(fields["review_failed_at"])
        if stamp is None:
            raise ValueError("review_failed_at must be a finite number or null")
        fields["review_failed_at"] = stamp

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
        reviewed_at = assessment.finite_number(existing.get("reviewed_at"))
        if (
            failed_review_started_at is not None
            and reviewed_at is not None
            and reviewed_at >= failed_review_started_at
        ):
            return _index_from_raw({key: existing}, normalized=True)[key]
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
            "orchestrator_excluded": existing.get("orchestrator_excluded", False),
            "ai_recap": existing.get("ai_recap", ""),
            "recap_fingerprint": existing.get("recap_fingerprint", ""),
            "ai_assessment": assessment.from_stored(existing.get("ai_assessment")),
            "review_failed_at": assessment.finite_number(existing.get("review_failed_at")),
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
        # The assessment is bounded on WRITE as well as on read (#1020): whatever a caller hands
        # in is re-validated, and something that is not a record refuses the write.
        if fields.get("ai_assessment") is not None:
            rec = assessment.from_stored(fields["ai_assessment"])
            if rec is None:
                raise ValueError("ai_assessment is not a valid assessment record")
            meta_dict["ai_assessment"] = rec
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
    return _aliases_from_raw(_load_raw(path))


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
        # The requested model (#1189) follows the session through adoption. With no logical entry
        # the reads fall back to the placeholder's (and a sparse logical row is never CREATED
        # here: it would shadow the placeholder's title/sticky, `resolve_key`). A logical entry
        # that already exists would shadow the record instead, so it gets a copy — never an
        # overwrite of a model it recorded itself.
        src_row = data.get(placeholder_key)
        dst = data.get(real_key)
        requested = _model_or_blank(
            src_row.get("model_requested") if isinstance(src_row, dict) else None
        )
        if requested and isinstance(dst, dict) and not _model_or_blank(dst.get("model_requested")):
            data[real_key] = {**dst, "model_requested": requested}
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
