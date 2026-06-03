"""codex engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session
from . import base

# rollout-<iso-ts>-<uuid>.jsonl  →  capture the trailing uuid
_CODEX_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _codex_text(content) -> str:
    """First text chunk of a codex message ``content`` (str or list of parts)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"]).strip()
    return ""


class CodexProvider:
    """codex: JSONL rollout files under ``~/.codex/sessions/YYYY/MM/DD/
    rollout-<ts>-<uuid>.jsonl``, resumed via ``codex resume <uuid>``.

    File-based like Claude (not a DB like opencode), so discovery mirrors the Claude
    reader: walk the rollout files, take the uuid from the filename, read ``cwd`` +
    the first user message from the records, mtime from the file. **Read-only +
    fail-soft**: a parse/IO error skips that file, never the whole list. Archive is
    not a codex concept, so ``archive``/``unarchive`` raise (surfaced as a 4xx).
    """

    engine_id = "codex"
    id_pattern = base._CODEX_UUID_RE
    supports_new = False  # codex resume-only (no pinned new-session yet)

    def is_present(self) -> bool:
        return base._codex_sessions_dir().is_dir() or shutil.which("codex") is not None

    def _meta(self, path: Path) -> tuple[str, str] | None:
        """``(cwd, first_user_message)`` from one rollout file. Single pass, best-effort."""
        cwd = first_user = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if not cwd and payload.get("cwd"):
                        cwd = str(payload["cwd"])
                    if not first_user and payload.get("role") == "user":
                        first_user = _codex_text(payload.get("content"))
                    if cwd and first_user:
                        break
        except OSError:
            return None
        # cwd is the one required field: it's the launch dir + the open-path
        # allowlist key. A rollout with no usable cwd (corrupt-only, or not a real
        # session) yields no row rather than a bogus empty-cwd session.
        return (cwd, first_user) if cwd else None

    def scan(self) -> list[Session]:
        root = base._codex_sessions_dir()
        out: list[Session] = []
        try:
            files = list(root.rglob("rollout-*.jsonl"))
        except OSError:
            return out
        for path in files:
            m = _CODEX_ROLLOUT_RE.search(path.name)
            if not m:
                continue
            meta = self._meta(path)
            if meta is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cwd, first_user = meta
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=m.group(1),
                    cwd=cwd,
                    last_mtime=mtime,
                    first_user_message=first_user,
                    archived=False,
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # codex resumes by uuid; cwd is set by the launcher. No documented per-launch
        # bypass flag (sandbox/approvals are config / -c driven), so none is added.
        return [base.CODEX_BIN, "resume", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        raise NotImplementedError("codex ws new-session not supported yet")

    def archive(self, native_id):
        # codex rollouts stay read-only; archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
