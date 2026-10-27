"""GitHub Copilot CLI engine provider (#1565).

Copilot keeps one directory per session under ``~/.copilot/session-state/<uuid>/`` with a
``workspace.yaml`` (id, cwd, created/updated stamps) written at session creation and an
``events.jsonl`` event log (``session.start``, ``user.message``, ``assistant.message``,
``tool.execution_*``, …) once the session runs. Archiving MOVES the directory to the sibling
``session-state-archive/<uuid>/`` tree — verified against Copilot 1.0.68/1.0.70: its
``session/list`` and ``--resume`` enumerate ``session-state/`` directly off the filesystem, so a
moved session is hidden from the vendor and restored by the move back. The auxiliary
``session-store.db`` FTS index is left untouched.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session
from . import base


def _iso_ts(value) -> float:
    """An ISO-8601 ``…Z`` timestamp as epoch seconds, or 0.0 for anything unexpected."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _workspace_meta(path: Path, *, checked: bool = False) -> dict[str, str]:
    """The scalar top-level keys of a session's ``workspace.yaml`` (``cwd``, ``created_at``,
    ``updated_at``, …).

    Deliberately NOT a YAML parse: PyYAML is not a dependency, and the keys we need are
    single-line ``key: value`` scalars. A block scalar (``name: |-`` followed by indented
    lines) is skipped by the same rule — its header line carries no inline value.

    A genuinely ABSENT file is a partial artifact (``{}``); any other read failure propagates
    under ``checked`` so a measurement never reads an unreadable workspace as a missing row.
    """
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line[:1] in (" ", "\t", "#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if not key or not value or value in ("|-", "|", ">-", ">"):
                    continue
                if len(value) <= 4096:
                    out[key] = value
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        if checked:
            raise
    return out


def human_user_text(record: dict) -> str:
    """The text of a HUMAN ``user.message`` event, else "".

    Skill/command invocations and autopilot continuations also emit ``user.message`` records —
    they carry ``data.source`` (``"skill-…"``, ``"command-…"``, ``"jit-instruction"``) or
    ``data.isAutopilotContinuation`` — and a subagent's turns carry a top-level ``agentId``.
    None of those are the operator's own words, so none may become a title or a human turn.
    """
    if not isinstance(record, dict) or record.get("type") != "user.message":
        return ""
    if record.get("agentId"):
        return ""
    data = record.get("data")
    if not isinstance(data, dict):
        return ""
    if data.get("source") or data.get("isAutopilotContinuation"):
        return ""
    content = data.get("content")
    return content.strip() if isinstance(content, str) else ""


def is_detached_subagent(record: dict) -> bool:
    """Is this ``session.start`` record a DETACHED subagent's (a
    ``detachedFromSpawningParentSessionId``)? Those are listed under their parent by Copilot
    itself, never as top-level sessions."""
    if not isinstance(record, dict) or record.get("type") != "session.start":
        return False
    data = record.get("data")
    return isinstance(data, dict) and bool(data.get("detachedFromSpawningParentSessionId"))


def _events_head(path: Path, *, checked: bool = False) -> tuple[str, bool, float]:
    """``(first human user text, is-detached-subagent, last activity ts)`` from one
    ``events.jsonl``, best-effort and in a single pass. A missing file is all-empty; any other
    read failure propagates under ``checked``, like ``_workspace_meta``."""
    title = ""
    detached = False
    last = 0.0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if not detached and is_detached_subagent(rec):
                    detached = True
                if not title:
                    title = human_user_text(rec)
                ts = _iso_ts(rec.get("timestamp"))
                if ts > last:
                    last = ts
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        if checked:
            raise
    return title, detached, last


def _dir_mtime(path: Path) -> float:
    """Newest mtime among the session dir's own files (the dir mtime bumps on archive moves)."""
    newest = 0.0
    try:
        for entry in path.iterdir():
            try:
                st = entry.stat()
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
    except OSError:
        return 0.0
    return newest


def _present(path: Path) -> bool:
    """Is there ANYTHING at ``path``? ``stat``-authoritative, like ``archive_state``: ENOENT
    alone means absent; an unreadable path counts as present so a conflicting move is refused
    rather than attempted blind."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


class CopilotProvider:
    """Copilot CLI: per-session directories under ``~/.copilot/session-state/`` (#1565).

    Read-only and fail-soft like the other file-based kinds: a malformed session directory
    costs its own row, never the whole list. Archive is a real directory MOVE (the vendor's
    ``--resume``/``session/list`` enumerate the live tree off the filesystem) plus the
    engine-agnostic sidecar flag, mirroring Claude.
    """

    engine_id = "copilot"
    id_pattern = base._COPILOT_UUID_RE

    def store_present(self) -> bool:
        """Does this engine's store exist? Presence of the BINARY is the provider's question
        (its provenance-checked entrypoint, #853 §2b) — never a PATH lookup here."""
        return base._copilot_sessions_dir().is_dir()

    def is_present(self) -> bool:
        """Kind-level presence is the STORE only. Whether the binary is there is the owning
        provider's question, answered through provenance — never a PATH lookup (#853 §2b)."""
        return self.store_present()

    def _session_dirs(self, root: Path) -> list[Path]:
        try:
            return [
                Path(e.path)
                for e in os.scandir(root)
                if e.is_dir(follow_symlinks=False) and self.id_pattern.match(e.name)
            ]
        except OSError:
            return []

    def _row(self, path: Path, *, archived: bool, checked: bool = False) -> Session | None:
        """One row from one session directory, or ``None`` when it is not listable. Shared by
        ``scan`` and ``scan_checked`` so neither can disagree about what a row contains;
        ``checked`` propagates a failed READ instead of turning it into a missing row."""
        try:
            st = path.stat()
        except OSError:
            if checked:
                raise
            return None
        meta = _workspace_meta(path / "workspace.yaml", checked=checked)
        if not meta.get("id"):
            # workspace.yaml is written at session creation; without it this is not a session
            # (a partial crash artifact), never a row.
            return None
        title, detached, last_event = _events_head(path / "events.jsonl", checked=checked)
        if detached:
            return None
        cwd = meta.get("cwd", "")
        if not cwd:
            return None
        created = _iso_ts(meta.get("created_at"))
        last = last_event or _iso_ts(meta.get("updated_at")) or _dir_mtime(path) or st.st_mtime
        return Session(
            engine=self.engine_id,
            uuid=path.name,
            cwd=cwd,
            last_mtime=last,
            first_user_message=title,
            archived=archived,
            created_at=created or st.st_mtime,
        )

    def scan(self) -> list[Session]:
        out: list[Session] = []
        for root, archived in (
            (base._copilot_sessions_dir(), False),
            (base._copilot_archive_dir(), True),
        ):
            for path in self._session_dirs(root):
                row = self._row(path, archived=archived)
                if row is not None:
                    out.append(row)
        return out

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows, plus a line for each session directory that could not be READ (#993):
        a measurement authorising deletion must not read an unreadable directory as absent."""
        rows: list[Session] = []
        problems: list[str] = []
        for root, archived in (
            (base._copilot_sessions_dir(), False),
            (base._copilot_archive_dir(), True),
        ):
            entries = [
                Path(e.path)
                for e in base.scandir_checked(root)
                if e.is_dir(follow_symlinks=False) and self.id_pattern.match(e.name)
            ]
            got, trouble = base.checked_rows(
                sorted(entries),
                lambda p, archived=archived: self._row(p, archived=archived, checked=True),
                engine_id=self.engine_id,
            )
            rows.extend(got)
            problems.extend(trouble)
        return rows, problems

    def lookup(self, native_id: str) -> Session | None:
        """This one session, read fresh (#991) from either tree, or ``None``."""
        if not self.id_pattern.match(native_id or ""):
            return None
        for root, archived in (
            (base._copilot_sessions_dir(), False),
            (base._copilot_archive_dir(), True),
        ):
            row = self._row(root / native_id, archived=archived)
            if row is not None:
                return row
        return None

    def archive_state(self, native_id: str) -> str:
        """`archived` · `not-archived` · `unreadable` — does the ARCHIVE tree hold this id?

        Deliberately NOT an existence check (mirrors Claude's): it asks only whether the
        archive tree holds a directory for this id. `stat`, not `exists`: only `ENOENT` means
        "not here"; any other failure is "unreadable", never "not-archived".
        """
        if not self.id_pattern.match(native_id or ""):
            return "not-archived"
        target = base._copilot_archive_dir() / native_id
        try:
            target.stat()
        except (FileNotFoundError, NotADirectoryError):
            return "not-archived"
        except OSError:
            return "unreadable"
        return "archived"

    def _move_session_tree(self, native_id: str, src: Path, dst: Path) -> None:
        """Collision-safe MOVE of one session directory into the other tree.

        The destination is CLAIMED by an exclusive ``mkdir`` before anything moves, and the
        move is a single ``os.rename`` onto that empty claim (both trees hang off the same
        store root, so always one filesystem; rename replaces an empty directory atomically).
        Check and move therefore cannot interleave into ``shutil.move``'s silent
        ``<id>/<id>/`` nest: a destination that appears first — a repeat transition, or a
        still-running Copilot recreating its live directory — is ``FileExistsError`` and
        refused, and anything written into the claim before the rename fails the rename with
        ``ENOTEMPTY`` instead of being replaced. Only an actually MISSING source is the
        idempotent no-op; every other failure raises with the sidecar left untouched.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.mkdir()
        except FileExistsError:
            if _present(src):
                raise base.EngineError(
                    f"{self.engine_id}:{native_id} exists in both the live and archive trees; "
                    "refusing to nest one inside the other"
                ) from None
            return  # already moved earlier — the caller's sidecar patch is the idempotent part
        except OSError as e:
            raise base.EngineError(
                f"{self.engine_id}:{native_id} could not claim its destination ({e})"
            ) from e
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            # The source vanished (or was never on disk): undo our empty claim; the move is
            # the idempotent no-op.
            dst.rmdir()
            return
        except OSError as e:
            try:
                dst.rmdir()  # undo only OUR empty claim; a filled one is left for a human
            except OSError:
                pass
            raise base.EngineError(f"{self.engine_id}:{native_id} could not be moved ({e})") from e

    def archive(self, native_id):
        # MOVE the session directory into the archive tree AND record the flag in the
        # engine-agnostic sidecar, mirroring Claude: the sidecar keeps the session archived
        # even if a still-running process recreates the live directory on its next write.
        if not self.id_pattern.match(native_id or ""):
            raise base.EngineError(f"bad {self.engine_id} id: {native_id!r}")
        self._move_session_tree(
            native_id,
            base._copilot_sessions_dir() / native_id,
            base._copilot_archive_dir() / native_id,
        )
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        if not self.id_pattern.match(native_id or ""):
            raise base.EngineError(f"bad {self.engine_id} id: {native_id!r}")
        self._move_session_tree(
            native_id,
            base._copilot_archive_dir() / native_id,
            base._copilot_sessions_dir() / native_id,
        )
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
