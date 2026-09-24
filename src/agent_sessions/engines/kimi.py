"""Kimi Code engine provider (#714).

Kimi Code (Moonshot, ``kimi``) keeps a **nested session directory tree** under ``~/.kimi-code`` —
a third storage shape next to the flat JSONL engines (claude/codex/gemini) and the SQLite ones
(opencode/antigravity):

    ~/.kimi-code/session_index.jsonl                      {sessionId, sessionDir, workDir}
    ~/.kimi-code/sessions/wd_<slug>_<hash>/session_<uuid>/ state.json
                                                          agents/<agent>/wire.jsonl

Two consequences shape this provider:

- **The index is a fast path, not the truth.** ``scan`` reads ``session_index.jsonl`` when it's
  there and falls back to walking ``sessions/*/session_*/`` when it's missing or corrupt, so a
  truncated index degrades to a slower scan rather than an empty sidebar. Rows are merged by id
  with the walk, because an index row can outlive the dir it points at (and vice versa). The one
  exact-session resolution seam is :func:`session_dir_for` — the provider *and* the transcript
  adapter/locator (``transcript.kimi_wire_path``) go through it, so there is never a second,
  subtly-different resolver (#720).
- **Transcript lives in ``agents/main/wire.jsonl``** — a loop-event stream parsed by
  ``transcript._kimi_turns_from_wire`` (#720). ``state.json`` still supplies the sidebar title /
  recency without touching the transcript. ``state.json`` itself comes in two schemas that both
  exist in a live store (#1030): v1 (kimi ≤0.42) with ``workDir`` + ISO timestamps, and v2
  (kimi 0.43.1+, which self-updates in place) with ``cwd`` + epoch-millisecond timestamps.
  :func:`_meta` reads both; a session we can't place yields no row, as before.

**Read-only + fail-soft**, like every non-Claude engine: a parse/IO error skips one row and never
the whole list, and nothing here ever writes Kimi's store — archive rides the engine-agnostic
metadata sidecar. Kimi mints its own session id (there is no ``--session-id`` flag), so new
sessions launch under a placeholder and reconcile afterwards, the codex/antigravity dance.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, fs_created_at
from . import base

# Kimi's placeholder title for a session it hasn't auto-titled yet. Treated as "no title" so the
# sidebar falls back to its own derivation instead of showing a wall of identical "New Session".
_UNTITLED = "New Session"


def _iso_to_epoch(value: object) -> float:
    """ISO-8601 (``2026-07-19T14:19:03.061Z``) → epoch seconds, or ``0.0`` if unparseable.

    ``state.json`` stores Z-suffixed UTC; ``fromisoformat`` only learned to accept ``Z`` in 3.11,
    so the suffix is normalized explicitly rather than relying on the interpreter version.
    """
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _state_ts_to_epoch(value: object) -> float:
    """One ``state.json`` timestamp → epoch seconds, or ``0.0`` (→ the caller's filesystem
    fallback).

    Kimi writes two shapes here and BOTH exist in a live store (#1030):

    - v1 (≤0.42): ISO-8601 strings — see :func:`_iso_to_epoch`.
    - v2 (0.43.1+): finite epoch **milliseconds** as a JSON number.

    Booleans are rejected before the numeric branch even though ``bool`` subclasses ``int`` —
    ``True`` is not a timestamp. Anything malformed (wrong type, non-finite, ≤0) returns
    ``0.0`` so the ``or st.st_mtime`` / ``or fs_created_at`` fallback in :func:`_meta` stays
    in charge rather than a bogus value sneaking through.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        if not math.isfinite(value) or value <= 0:
            return 0.0
        return value / 1000.0
    return _iso_to_epoch(value)


# --- store reading (module-level + home-injectable) -----------------------------------------
#
# These are module functions, not provider methods, so the transcript adapter/locator resolve the
# SAME store under a test home without instantiating the provider — one path contract (#720). Every
# one takes ``home`` and threads it into ``base._kimi_dir(home)`` (env override still wins there).


def _index_rows(home: Path | None = None) -> dict[str, tuple[str, Path]]:
    """``{session_id: (work_dir, session_dir)}`` from ``session_index.jsonl``.

    Fail-soft per line: a truncated tail or a junk row is skipped, the rest still load. A
    missing/unreadable index is an empty mapping, not an error — the caller falls back to walking
    the session dirs.
    """
    out: dict[str, tuple[str, Path]] = {}
    path = base._kimi_dir(home) / "session_index.jsonl"
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                sid, work, sdir = row.get("sessionId"), row.get("workDir"), row.get("sessionDir")
                # An id we can't validate must never key a session or build a path.
                if not isinstance(sid, str) or not base._KIMI_SESSION_RE.match(sid):
                    continue
                if not isinstance(work, str) or not work:
                    continue
                if not isinstance(sdir, str) or not sdir:
                    continue
                out[sid] = (work, Path(sdir))
    except OSError:
        return out
    return out


def _walk_session_dirs(home: Path | None = None) -> dict[str, Path]:
    """``{session_id: session_dir}`` by walking ``sessions/wd_*/session_*``.

    The fallback when the index is missing or lost rows, and the ground truth for whether a dir
    still exists on disk. Kept to the known two-level shape rather than an unbounded ``rglob`` so a
    large store stays cheap to scan.
    """
    out: dict[str, Path] = {}
    root = base._kimi_dir(home) / "sessions"
    try:
        buckets = list(root.iterdir())
    except OSError:
        return out
    for bucket in buckets:
        try:
            if not bucket.is_dir():
                continue
            entries = list(bucket.iterdir())
        except OSError:
            continue
        for entry in entries:
            if base._KIMI_SESSION_RE.match(entry.name):
                out[entry.name] = entry
    return out


def _meta_checked(session_dir: Path) -> tuple[str, str, float, float] | None:
    """:func:`_meta`, but RAISING when ``state.json`` cannot be READ.

    `_meta` swallows `OSError` into `None`, which a measurement authorising deletion cannot tell
    apart from "this session has no usable working dir" — so an unreadable store counted as
    nothing to remove (review 4915/4919, finding 4).

    **One read, and that is the fix.** The first cut of this probed the file here and then called
    `_meta`, which OPENED IT AGAIN through its own fail-soft `except OSError`. A file that answers
    the probe and then fails the authoritative read — a failing disk, a revoked network mount, an
    unlucky moment — came back as `None` with no problem reported, i.e. a structurally valid zero
    that authorises deleting contents nobody counted (review 4951, P2). The read that produces the
    row is now the only read there is.
    """
    return _meta(session_dir, checked=True)


def _meta(session_dir: Path, *, checked: bool = False) -> tuple[str, str, float, float] | None:
    """``(work_dir, title, updated_at, created_at)`` from one session's ``state.json``.

    Returns ``None`` when the session has no usable working dir: cwd is both the launch dir and
    the open-path allowlist key, so a session we can't place yields **no row** rather than a bogus
    empty-cwd one (the rule codex/gemini/antigravity already follow).

    ``checked`` selects the failure policy, never the parse: fail-soft ``None`` for the sidebar,
    RAISE for a measurement that authorises deletion. Absence is not a failure under either — a
    missing ``state.json`` is a session we cannot place. A malformed one also stays ``None``: it
    read fine, and "this record does not decode" is a listability question, not an unreadable
    store.

    Two ``state.json`` schemas are read here and BOTH exist in a live store (#1030) — v1
    (kimi ≤0.42) and v2 (kimi 0.43.1+, which renamed the working-dir field and switched the
    timestamps to epoch milliseconds):

    - working dir: v2 ``cwd`` wins when it is a non-empty string, v1 ``workDir`` is the
      fallback (and the only field for old sessions). Precedence is defined (the field the
      current writer maintains) and pinned by test, so a transitional writer carrying both
      can never flip the row between scans.
    - timestamps: :func:`_state_ts_to_epoch` handles both shapes; anything malformed falls
      back to the filesystem (``st_mtime`` / ``fs_created_at``) as before.

    Timestamps come from Kimi's own ``createdAt``/``updatedAt`` rather than file mtimes — they
    survive a copy of the store and don't get bumped by unrelated writes. Both degrade to the
    filesystem when absent or malformed.
    """
    state_path = session_dir / "state.json"
    try:
        raw = state_path.read_bytes()
        st = state_path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        if checked:
            raise
        return None
    try:
        state = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None

    def _work_dir(*keys: str) -> str | None:
        for key in keys:
            val = state.get(key)
            if isinstance(val, str) and val:
                return val
        return None

    work = _work_dir("cwd", "workDir")
    if work is None:
        return None
    raw_title = state.get("title")
    # Kimi seeds every session with "New Session" and only replaces it once it has something to
    # name; surfacing that verbatim would fill the sidebar with identical rows.
    title = (
        raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() != _UNTITLED else ""
    )
    updated = _state_ts_to_epoch(state.get("updatedAt")) or st.st_mtime
    created = _state_ts_to_epoch(state.get("createdAt")) or fs_created_at(st)
    return work, title, updated, created


def session_dir_for(native_id: str, home: Path | None = None) -> Path | None:
    """The on-disk session dir for exactly ``native_id``, or ``None`` — the single resolution seam
    shared by the provider and the transcript locator (#720).

    - Validates the ``session_<uuid>`` shape first, so a path is never built from junk and a
      same-prefix neighbour (``session_<A>`` vs ``session_<A>x``) can never match (exact dict-key
      lookup, no globbing).
    - **The walk wins over a stale index path**: the walk enumerates dirs that actually exist, so
      if it has the id its path is real; only when the walk lacks it do we trust the index, and
      then only if that path is still a directory (a stale index row → ``None``).
    """
    if not base._KIMI_SESSION_RE.match(native_id):
        return None
    walk = _walk_session_dirs(home)
    if native_id in walk:
        return walk[native_id]
    idx = _index_rows(home)
    if native_id in idx:
        sdir = idx[native_id][1]
        try:
            if sdir.is_dir():
                return sdir
        except OSError:
            return None
    return None


class KimiProvider:
    """Kimi Code: nested session dirs under ``~/.kimi-code``, resumed via ``kimi -S <id>``.

    Native ids carry a literal ``session_`` prefix (``session_<uuid>``), not a bare UUID — see
    ``base._KIMI_SESSION_RE``. Read-only, fail-soft, sidecar archive, reconciling new-session.
    """

    engine_id = "kimi"
    id_pattern = base._KIMI_SESSION_RE

    def store_present(self) -> bool:
        """Does this engine's store exist? Presence of the BINARY is the provider's question
        (its provenance-checked entrypoint, #853 §2b) — never a PATH lookup here."""
        return base._kimi_dir().is_dir()

    def is_present(self) -> bool:
        """Kind-level presence is the STORE only. Whether the binary is there is the owning
        provider's question, answered through provenance — never a PATH lookup (#853 §2b)."""
        return self.store_present()

    # --- store reading ----------------------------------------------------------------------
    # Resolution lives in the module-level `_index_rows` / `_walk_session_dirs` / `_meta` /
    # `session_dir_for` helpers so the transcript adapter shares the exact-session seam (#720).

    def scan(self) -> list[Session]:
        # Index first, then the walk — union by id so neither a stale index row nor a dir the
        # index forgot can drop a session. The walk wins on path, since it is the ground truth.
        dirs: dict[str, Path] = {sid: sdir for sid, (_, sdir) in _index_rows().items()}
        dirs.update(_walk_session_dirs())
        out: list[Session] = []
        for sid, session_dir in dirs.items():
            row = self._row(sid, session_dir)
            if row is not None:
                out.append(row)
        return out

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows, plus a line for each session dir that could not be READ (#993).

        BOTH halves of ``scan``'s union are fail-soft: ``_index_rows`` treats an unreadable index
        as an empty mapping, and ``_walk_session_dirs`` swallows ``OSError`` per bucket — so an
        unreadable store answers "no kimi sessions", which must never be what authorises a
        deletion. The checked walk raises on a bucket it cannot list and names a session dir it
        cannot read, while keeping every session that read cleanly. The walk alone is the ground
        truth for what exists on disk, so the index is deliberately not consulted here.
        """
        dirs: dict[str, Path] = {}
        for bucket in base.scandir_checked(base._kimi_dir() / "sessions"):
            if not bucket.is_dir(follow_symlinks=False):
                continue
            for entry in base.scandir_checked(Path(bucket.path)):
                if base._KIMI_SESSION_RE.match(entry.name):
                    dirs[entry.name] = Path(entry.path)
        paths = [dirs[sid] for sid in sorted(dirs)]
        return base.checked_rows(
            paths,
            lambda p: self._row(p.name, p, read=_meta_checked),
            engine_id=self.engine_id,
        )

    def _row(self, sid: str, session_dir: Path, *, read=_meta) -> Session | None:
        """One row from one session dir's ``state.json`` (v1 or v2), or ``None`` when it has no
        usable working dir. Shared by ``scan`` and ``lookup`` (#991).

        ``read`` is the reader: fail-soft :func:`_meta` for display, :func:`_meta_checked` for
        maintenance. Injecting it keeps ONE row builder, so the two paths cannot disagree about
        what a record means — they differ only in how a failed read is reported."""
        meta = read(session_dir)
        if meta is None:
            return None
        work, title, updated, created = meta
        return Session(
            engine=self.engine_id,
            uuid=sid,
            cwd=work,
            last_mtime=updated,
            first_user_message=title,
            archived=False,
            created_at=created,
        )

    def lookup(self, native_id: str) -> Session | None:
        """This one session, read fresh (#991), or ``None`` — resolved through
        :func:`session_dir_for`, the provider's one exact-session seam (walk wins over a stale
        index path, as in ``scan``), then that dir's ``state.json`` only."""
        session_dir = session_dir_for(native_id or "")
        return self._row(native_id, session_dir) if session_dir is not None else None

    # --- launch -----------------------------------------------------------------------------

    # --- new-session reconciliation ---------------------------------------------------------

    def _session_ids_in_cwd(self, cwd: str) -> set[str] | None:
        """Session ids whose working dir == ``cwd``, or ``None`` if the store read FAILED.

        A missing store is a valid empty baseline (fresh Kimi) → ``set()``, not a failure. Scoped
        by cwd so a session created concurrently in another project can't be adopted as ours. The
        index is authoritative for ``workDir`` here (v2 kept the field in ``session_index.jsonl``
        even though ``state.json`` renamed it to ``cwd``); dirs found only by the walk are
        resolved via :func:`_meta` (v1 or v2), and any session whose working dir isn't readable
        yet is excluded so it stays *pending* rather than being misattributed.
        """
        root = base._kimi_dir()
        if not root.is_dir():
            return set()
        out: set[str] = set()
        index = _index_rows()
        for sid, (work, _sdir) in index.items():
            if work == cwd:
                out.add(sid)
        for sid, sdir in _walk_session_dirs().items():
            if sid in index:
                continue  # already classified by the index
            meta = _meta(sdir)
            if meta is None:
                continue  # workDir not written yet → excluded (stays pending)
            if meta[0] == cwd:
                out.add(sid)
        return out

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """Kimi session ids already present in ``cwd`` BEFORE launch. ``None`` on a read failure,
        so the caller skips reconciliation rather than adopting a pre-existing session."""
        return self._session_ids_in_cwd(cwd)

    def reconcile_new_session(self, cwd: str, snapshot: set[str]) -> str | list[str] | None:
        """The Kimi session created in ``cwd`` since ``snapshot``. Returns:

        * the single new id — ours (unambiguous), or
        * a ``list`` of ≥2 — AMBIGUOUS (two new same-cwd sessions inside the poll window): the
          caller must NOT guess, or
        * ``None`` — Kimi hasn't written the session yet: keep serving under the placeholder.

        Read-only to Kimi's store; never mutates it.
        """
        current = self._session_ids_in_cwd(cwd)
        if current is None:
            return None  # transient read failure → stay on the placeholder
        new_ids = sorted(current - snapshot)
        if not new_ids:
            return None
        if len(new_ids) > 1:
            return new_ids  # ambiguous → caller fails safe
        return new_ids[0]

    # --- archive ----------------------------------------------------------------------------

    def archive(self, native_id):
        # Kimi's store stays read-only; the archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
