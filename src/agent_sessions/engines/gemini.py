"""gemini-cli engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, derive_created_at
from . import base


def _gemini_text(content) -> str:
    """First text chunk of a gemini message ``content`` (list of ``{"text": …}`` parts,
    or a bare string)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"]).strip()
    return ""


class GeminiProvider:
    """gemini-cli: chat logs under ``~/.gemini/tmp/<project>/chats/
    session-<iso-ts>-<short>.jsonl``, resumed via ``gemini --resume <uuid>``.

    File-based like Claude/codex (not a DB). Each chat file opens with a ``kind:"main"``
    header record carrying the full ``sessionId`` (a UUID) + ``projectHash``; subsequent
    records are messages (``type:"user"`` with ``content:[{"text":…}]``). The launch
    cwd is resolved from ``projectHash`` via ``tmp/project-map.json`` — that's also gemini's
    own resume scoping (``--resume`` searches the cwd's project chats dir), so launching in
    the session's real cwd makes ``--resume <uuid>`` find it. **Read-only + fail-soft**: a
    parse/IO error or an unmappable project skips that file, never the whole list. Archive
    is not a gemini concept, so it rides the engine-agnostic sidecar (like codex/opencode).
    """

    engine_id = "gemini"
    id_pattern = base._GEMINI_UUID_RE
    supports_new = True  # new session with a pinned id via `gemini --session-id <uuid>`
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    expects_raw_tty = True  # ratatui/Ink TUI: its PTY must stay raw (#804)

    def is_present(self) -> bool:
        return base._gemini_tmp_dir().is_dir() or shutil.which("gemini") is not None

    def _project_map(self) -> dict[str, str]:
        """``projectHash -> cwd`` from ``tmp/project-map.json`` (best-effort, fail-soft)."""
        try:
            data = json.loads((base._gemini_tmp_dir() / "project-map.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _read(self, path: Path) -> tuple[str, str, str] | None:
        """``(session_id, project_hash, first_user_message)`` from one chat file, single
        pass, best-effort. None if the header carries no usable ``sessionId``."""
        sid = phash = first_user = ""
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
                    if not sid and isinstance(rec.get("sessionId"), str):
                        sid = rec["sessionId"]
                        phash = rec.get("projectHash") or phash
                    if not first_user and rec.get("type") == "user":
                        first_user = _gemini_text(rec.get("content"))
                    if sid and first_user:
                        break
        except OSError:
            return None
        return (sid, phash, first_user) if self.id_pattern.match(sid) else None

    def scan(self) -> list[Session]:
        root = base._gemini_tmp_dir()
        out: list[Session] = []
        try:
            files = list(root.glob("*/chats/session-*.jsonl"))
        except OSError:
            return out
        pmap = self._project_map()
        for path in files:
            meta = self._read(path)
            if meta is None:
                continue
            sid, phash, first_user = meta
            cwd = pmap.get(phash, "")
            # cwd is the launch dir + open-path allowlist key + gemini's own resume
            # scope. No mapping -> no usable cwd -> skip (like codex), never a bogus row.
            if not cwd:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=sid,
                    cwd=cwd,
                    last_mtime=st.st_mtime,
                    first_user_message=first_user,
                    archived=False,
                    created_at=derive_created_at(path, st),
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # gemini resumes by uuid, scoped to the cwd's project chats dir (set by the
        # launcher). `bypass` maps to gemini's "open straight in" flags: --yolo
        # (auto-approve tools, mirrors claude's --dangerously-skip-permissions) and
        # --skip-trust (skip the workspace-trust prompt).
        argv = [base.GEMINI_BIN, "--resume", native_id]
        if bypass:
            argv += ["--yolo", "--skip-trust"]
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *new* gemini session with our pre-generated id so the bridge can key it
        # before gemini has written its chat file.
        argv = [base.GEMINI_BIN, "--session-id", native_id]
        if bypass:
            argv += ["--yolo", "--skip-trust"]
        return argv

    def archive(self, native_id):
        # gemini chat logs stay read-only; archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
