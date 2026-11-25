"""Shell engine — "terminal as agent" (#636).

A plain interactive **login shell** with no agent behind it. It is a first-class engine so it
shows up in the new-session picker, the sidebar, the engine filter and the overview, and — the
whole point — it is AI-reviewed like every agent session. There is no transcript to read, so the
reviewer runs on the live terminal *screen* alone: ``review.gather_input`` already builds its
payload from the transcript AND ``scrollback.live_tail_text`` and only fails when both are empty,
and ``_plain_transcript`` fail-softs to "" when an engine registers no transcript adapter. Shell
registers none, so it reviews on screen — that IS "terminal as agent".

Two things make this a thin engine rather than a subsystem:

- **Pinned id.** A shell has no store that mints an id, so the client mints a UUID and we launch
  under ``shell:<uuid>`` — that key is final at launch (``new_session_reconciles`` is absent →
  falsey), so no reconcile dance.
- **Own record store.** With no native JSONL/SQLite to scan, the provider persists one tiny JSON
  record per session under ``base._shell_dir()`` (``on_new_session``) and ``scan`` reads them
  back. Archive rides the engine-agnostic metadata sidecar, exactly like gemini/codex/opencode.

Shell-free launcher contract (the repo's load-bearing security property): ``launch_argv`` returns
a **literal argv list** — the bash *binary* as argv[0] with a literal ``-l`` flag, never a command
string handed to an interpreter. cwd is applied by the pty bridge as the child's working dir, not
interpolated here. So none of the forbidden shell-layer patterns appear.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, fs_created_at
from . import base


class ShellProvider:
    """A bare ``bash -l`` login shell as a reviewable session (#636). No agent; no transcript;
    reviewed on its live screen. Pinned id, own record store, sidecar archive."""

    engine_id = "shell"
    id_pattern = base._SHELL_UUID_RE

    def store_present(self) -> bool:
        """Does this engine's store exist? Presence of the BINARY is the provider's question
        (its provenance-checked entrypoint, #853 §2b) — never a PATH lookup here."""
        return base._shell_dir().is_dir()

    def is_present(self) -> bool:
        """Kind-level presence is the STORE only. Whether the binary is there is the owning
        provider's question, answered through provenance — never a PATH lookup (#853 §2b)."""
        return self.store_present()

    # --- record store ----------------------------------------------------------------------

    def _record_path(self, native_id: str) -> Path | None:
        # Guard the filename against anything but our own UUID shape (defense in depth — callers
        # pass a parse_key-validated id, but the store must never build a path from junk).
        if not self.id_pattern.match(native_id):
            return None
        return base._shell_dir() / f"{native_id}.json"

    def on_new_session(self, native_id: str, *, cwd: str) -> None:
        """Persist a record for a freshly-launched shell so ``scan`` lists it. Called from the
        pinned-id new-session path AFTER cwd validation. The caller wraps this best-effort (a
        sidecar write must never block the terminal); the write itself is atomic so a crash can't
        leave a half-written record."""
        path = self._record_path(native_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"id": native_id, "cwd": cwd, "created_at": time.time()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(path)

    def on_new_session_failed(self, native_id: str) -> None:
        """Drop the record written by ``on_new_session`` when the launch that followed it was
        rejected — so a failed new session leaves no phantom row. Best-effort."""
        path = self._record_path(native_id)
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink()

    def _row_from_path(self, path: Path, *, checked: bool = False) -> Session | None:
        """One row from one record file, or ``None`` — fail-soft per record: a bad file drops its
        row, never the whole list. Shared by ``scan`` and ``lookup`` (#991).

        ``checked`` splits the two failures this used to catch together: a record that cannot be
        READ propagates, so a measurement authorising deletion names it instead of counting it as
        absent (review 4951, P2), while a record that reads and does not DECODE stays a dropped
        row under both policies — that is a listability fact, not an unreadable store."""
        try:
            raw = path.read_text(encoding="utf-8")
            st = path.stat()
        except OSError:
            if checked:
                raise
            return None
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return self._row_from_record(rec, st)

    def _row_from_record(self, rec, st) -> Session | None:
        """One row from an ALREADY-READ record + stat, or ``None`` when the record is not LISTABLE
        (its shape is wrong). No I/O here on purpose: classifying a read *failure* belongs to the
        caller, which is what lets ``scan_checked`` treat one as unreadable rather than absent."""
        if not isinstance(rec, dict):
            return None
        sid, cwd = rec.get("id"), rec.get("cwd")
        if not (isinstance(sid, str) and self.id_pattern.match(sid)):
            return None
        if not (isinstance(cwd, str) and cwd):
            return None
        created = rec.get("created_at")
        usable = isinstance(created, int | float) and not isinstance(created, bool) and created > 0
        created_at = float(created) if usable else fs_created_at(st)
        return Session(
            engine=self.engine_id,
            uuid=sid,
            cwd=cwd,
            last_mtime=st.st_mtime,
            # No transcript, so no first message; the title comes from the AI review's
            # ai_title (from the screen) or a manual rename via the sidecar.
            first_user_message="",
            # Sidecar override in the row builder decides the effective archive state.
            archived=False,
            created_at=created_at,
        )

    def scan(self) -> list[Session]:
        root = base._shell_dir()
        out: list[Session] = []
        try:
            files = sorted(root.glob("*.json"))
        except OSError:
            return out
        for path in files:
            row = self._row_from_path(path)
            if row is not None:
                out.append(row)
        return out

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows, plus a line for each record that could not be READ (#993).

        Partial by design: a record that fails to read costs only itself. "This shell session has
        no record" and "its record would not open" must not arrive as the same empty answer when
        something is about to be deleted — but nor may one bad record hide the healthy ones
        (review 4898). An absent store is legitimately empty; a shape-invalid record was read fine
        and is simply not listable, so it is skipped exactly as ``scan`` skips it.
        """
        paths = [
            Path(e.path)
            for e in base.scandir_checked(base._shell_dir())
            if e.name.endswith(".json") and e.is_file(follow_symlinks=False)
        ]
        return base.checked_rows(
            sorted(paths),
            lambda p: self._row_from_path(p, checked=True),
            engine_id=self.engine_id,
        )

    def lookup(self, native_id: str) -> Session | None:
        """This one shell session's record, read fresh (#991), or ``None``. ``on_new_session``
        names each record after its id, so this is one file read."""
        path = self._record_path(native_id or "")
        if path is None:
            return None
        row = self._row_from_path(path)
        return row if row is not None and row.uuid == native_id else None

    # --- launch ----------------------------------------------------------------------------

    # --- archive (engine-agnostic sidecar, like gemini/codex/opencode) ---------------------

    def archive(self, native_id: str) -> None:
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id: str) -> None:
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
