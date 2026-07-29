"""opencode engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import os
import sqlite3

from .. import discover
from .. import metadata as _metadata
from ..scanner import Session, is_ephemeral_cwd
from . import base

# Columns the opencode reader depends on, pinned so a schema rename fails the
# fixture test (loud) rather than silently dropping rows in prod (it would just
# fail-soft to no opencode rows).
OPENCODE_SCHEMA = (
    "id",
    "parent_id",
    "directory",
    "title",
    "time_created",
    "time_updated",
    "time_archived",
)


class OpenCodeProvider:
    """opencode: sessions live in a SQLite DB (``~/.local/share/opencode/opencode.db``),
    resumed via ``opencode <dir> --session <id>``.

    **Read-only to opencode.db:** the sidebar never writes opencode's DB.
    ``archive``/``unarchive`` flip the engine-agnostic sidecar flag (``metadata.json``,
    OR'd into the row by ``list_sessions``), never ``opencode.db`` — opencode has no JSONL
    to move, so archive is a pure sidecar toggle. Rename/sticky work the same way (sidecar
    only). All DB access is read-only and
    **fail-soft**: any sqlite error (missing / locked / corrupt / schema drift)
    yields no opencode rows rather than taking down the Claude list.
    """

    engine_id = "opencode"
    id_pattern = base._SES_RE
    # opencode can't pin a new-session id (``opencode --session`` only *continues*; there
    # is no create-returning-id). So new-session uses launch-then-reconcile (#127): launch
    # ``opencode <dir>`` (mints its own ``ses_…``) under a client-minted ``new-<uuid>``
    # placeholder, then diff opencode.db to find the new ``ses_…`` for that cwd and record
    # a persisted placeholder→real alias. The ws route + alias layer do the reconcile; the
    # provider only supplies the snapshot/diff primitives and the new-launch argv.
    supports_new = True
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    new_session_reconciles = True  # mints its own id → placeholder/reconcile flow (#127/#315)
    # Cross-engine handoff target (#597): the fresh opencode TUI accepts the seed as a
    # bracketed paste on its PTY input (never argv).
    supports_seed_start = True

    def _query_rows(self) -> list:
        """Read top-level opencode sessions, RAISING ``sqlite3.Error`` on a real read
        failure (locked / corrupt / schema drift). A genuinely absent DB file returns ``[]``
        — that's a valid empty (fresh opencode, no sessions yet), not a failure. Callers that
        must not confuse "read failed" with "empty" (the new-session baseline snapshot) use
        this directly; ``_query`` wraps it fail-soft for scan / is_present."""
        db = base._opencode_db()
        if not os.path.exists(db):
            return []
        cols = ", ".join(OPENCODE_SCHEMA)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA busy_timeout=500")
            return con.execute(
                f"SELECT {cols} FROM session WHERE parent_id IS NULL"  # noqa: S608 fixed cols
            ).fetchall()
        finally:
            con.close()

    def _query(self) -> list:
        # Fail-soft wrapper: any sqlite error yields no opencode rows rather than taking
        # down the Claude list. (The baseline snapshot can't use this — see _query_rows.)
        try:
            return self._query_rows()
        except sqlite3.Error:
            return []

    def _db_readable(self) -> bool:
        db = base._opencode_db()
        if not os.path.exists(db):
            return False
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("SELECT 1 FROM session LIMIT 1")
                return True
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def _bin(self) -> str:
        # The service may have started before opencode was installed/doctor rewrote env.
        # Resolve dynamically so ~/.opencode/bin/opencode still launches as an absolute argv[0].
        return discover.resolve(self.engine_id) or base.OPENCODE_BIN

    def is_present(self) -> bool:
        # A fresh opencode install has a CLI before it has an opencode.db. Treat either a
        # launchable binary or a readable DB as enough for the provider to participate.
        return discover.resolve(self.engine_id) is not None or self._db_readable()

    def scan(self) -> list[Session]:
        out: list[Session] = []
        for (
            sid,
            _parent,
            directory,
            title,
            time_created,
            time_updated,
            time_archived,
        ) in self._query():
            if not isinstance(sid, str) or not self.id_pattern.match(sid):
                continue
            # Drop ephemeral CI-runner sessions (#452): their cwd is a throwaway
            # ``act`` workdir that's already deleted, so they can never be resumed
            # and only clutter the list / resume allowlist / picker.
            if is_ephemeral_cwd(directory or ""):
                continue
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=sid,
                    cwd=directory or "",
                    # opencode stores epoch *milliseconds*; Claude uses seconds.
                    last_mtime=(time_updated or 0) / 1000.0,
                    first_user_message=title or "",  # opencode maintains a real title
                    archived=time_archived is not None,
                    # Real creation time from the DB (#506), ms → s; fall back to the update
                    # time if a row somehow lacks time_created.
                    created_at=(time_created or time_updated or 0) / 1000.0,
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # opencode resumes a session by id within its project dir. `bypass` is
        # accepted only for interface parity (permissions are config-side).
        return [self._bin(), cwd, "--session", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *fresh* opencode session in `cwd`. We deliberately pass NO `--session`:
        # ``opencode <dir>`` mints its own ``ses_…`` id, which the reconcile step (DB-diff)
        # discovers afterwards. `native_id` here is the client-minted ``new-<uuid>``
        # placeholder the bridge keys the socket/lock by; opencode never sees it. `bypass`
        # is config-side for opencode, so it doesn't change the argv (interface parity).
        return [self._bin(), cwd]

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """The set of top-level opencode ``ses_…`` ids currently in ``cwd`` (#127), or
        ``None`` if the DB read FAILED.

        Taken *before* launch so the post-launch diff can attribute the one new id to our
        placeholder. A failed read MUST NOT be confused with a genuinely empty one: if a
        transient sqlite lock/corrupt/schema error yielded an empty baseline while the cwd
        already had a ``ses_…`` row, the next successful poll would see that pre-existing row
        as "new" and misattribute it — a wrong attach, exactly what #127 must never do. So on
        read failure we return ``None`` and the caller skips reconciliation (stays on the
        placeholder). A missing DB file is a valid empty baseline (fresh opencode), not a
        failure. cwd-scoped so an unrelated new session elsewhere can't be mistaken for ours.
        """
        try:
            rows = self._query_rows()
        except sqlite3.Error:
            return None
        return {
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in rows
            if isinstance(sid, str) and self.id_pattern.match(sid) and (directory or "") == cwd
        }

    def reconcile_new_session(self, cwd: str, snapshot: set[str]) -> str | list[str] | None:
        """Find the opencode session id created in ``cwd`` since ``snapshot`` (#127).

        Returns:
          * the single new ``ses_…`` id — our session (unambiguous attribution), or
          * a ``list`` of ≥2 new ids — AMBIGUOUS (two new same-cwd sessions in the poll
            window): the caller must NOT guess (fail-safe — never attach to the wrong
            one), or
          * ``None`` — opencode hasn't written a new row yet (it may not until the first
            message): the caller keeps serving under the placeholder and polls again.

        Read-only to opencode.db; never mutates it.
        """
        new_ids = [
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in self._query()
            if isinstance(sid, str)
            and self.id_pattern.match(sid)
            and (directory or "") == cwd
            and sid not in snapshot
        ]
        if not new_ids:
            return None
        if len(new_ids) > 1:
            return new_ids  # ambiguous → caller fails safe
        return new_ids[0]

    def archive(self, native_id):
        # opencode.db stays read-only; record the archive flag in the engine-agnostic
        # sidecar (same place rename/sticky live). list_sessions ORs it into the row.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
