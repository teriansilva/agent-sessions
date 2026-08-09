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
  recency without touching the transcript.

**Read-only + fail-soft**, like every non-Claude engine: a parse/IO error skips one row and never
the whole list, and nothing here ever writes Kimi's store — archive rides the engine-agnostic
metadata sidecar. Kimi mints its own session id (there is no ``--session-id`` flag), so new
sessions launch under a placeholder and reconcile afterwards, the codex/antigravity dance.
"""

from __future__ import annotations

import json
import shutil
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


def _meta(session_dir: Path) -> tuple[str, str, float, float] | None:
    """``(work_dir, title, updated_at, created_at)`` from one session's ``state.json``.

    Returns ``None`` when the session has no usable ``workDir``: cwd is both the launch dir and the
    open-path allowlist key, so a session we can't place yields **no row** rather than a bogus
    empty-cwd one (the rule codex/gemini/antigravity already follow).

    Timestamps come from Kimi's own ``createdAt``/``updatedAt`` rather than file mtimes — they
    survive a copy of the store and don't get bumped by unrelated writes. Both degrade to the
    filesystem when absent or malformed.
    """
    state_path = session_dir / "state.json"
    try:
        with state_path.open(encoding="utf-8", errors="replace") as fh:
            state = json.load(fh)
        st = state_path.stat()
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    work = state.get("workDir")
    if not isinstance(work, str) or not work:
        return None
    raw_title = state.get("title")
    # Kimi seeds every session with "New Session" and only replaces it once it has something to
    # name; surfacing that verbatim would fill the sidebar with identical rows.
    title = (
        raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() != _UNTITLED else ""
    )
    updated = _iso_to_epoch(state.get("updatedAt")) or st.st_mtime
    created = _iso_to_epoch(state.get("createdAt")) or fs_created_at(st)
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
    supports_new = True
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    expects_raw_tty = True  # ratatui/Ink TUI: its PTY must stay raw (#804)
    # Kimi mints its own ``session_<uuid>`` at launch — ``-S/--session`` only *resumes*, there is
    # no caller-supplied id flag — so new-session launches under a ``new-<uuid>`` placeholder and
    # reconciles to the real id afterwards (same shape as codex/antigravity).
    new_session_reconciles = True
    # Cross-engine handoff target (#720 Phase 3): a fresh Kimi TUI accepts the seed as a bracketed
    # paste on its PTY input (never argv). PROVEN against a real authenticated session — replicating
    # webterm's readiness gate (DECSET 2004 armed + a ≥2KB first paint + a quiet window), a
    # multi-line ``ESC[200~ … ESC[201~ CR`` paste was consumed as ONE submitted ``turn.prompt``
    # with newlines intact. So the codex "arms 2004 then eats stdin" race (which the gate exists to
    # catch) can't strand a Kimi seed. Delivery + timing stay the engine-agnostic webterm.py gate.
    supports_seed_start = True

    def is_present(self) -> bool:
        return base._kimi_dir().is_dir() or shutil.which("kimi") is not None

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
            meta = _meta(session_dir)
            if meta is None:
                continue
            work, title, updated, created = meta
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=sid,
                    cwd=work,
                    last_mtime=updated,
                    first_user_message=title,
                    archived=False,
                    created_at=created,
                )
            )
        return out

    # --- launch -----------------------------------------------------------------------------

    def launch_argv(self, native_id, *, cwd, bypass):
        # ``-S <id>`` resumes that session; cwd is applied by the pty bridge as the child's working
        # dir, never interpolated into argv (shell-free contract).
        argv = [base.KIMI_BIN, "-S", native_id]
        if bypass:
            argv.append("-y")  # --yolo: auto-approve every action
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # A *fresh* session in ``cwd``. Kimi mints its own ``session_<uuid>`` (no ``--session-id``
        # equivalent), discovered by the reconcile diff afterwards. ``native_id`` is the client's
        # ``new-<uuid>`` placeholder that keys the socket/lock — Kimi never sees it.
        argv = [base.KIMI_BIN]
        if bypass:
            argv.append("-y")
        return argv

    # --- new-session reconciliation ---------------------------------------------------------

    def _session_ids_in_cwd(self, cwd: str) -> set[str] | None:
        """Session ids whose ``workDir`` == ``cwd``, or ``None`` if the store read FAILED.

        A missing store is a valid empty baseline (fresh Kimi) → ``set()``, not a failure. Scoped
        by cwd so a session created concurrently in another project can't be adopted as ours. The
        index is authoritative for ``workDir`` here; dirs found only by the walk are resolved via
        ``state.json``, and any session whose workDir isn't readable yet is excluded so it stays
        *pending* rather than being misattributed.
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
