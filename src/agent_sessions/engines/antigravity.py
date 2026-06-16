"""Antigravity CLI (``agy``) engine provider (#422).

``agy`` is Google's Go-rewrite successor to ``gemini-cli`` (Gemini CLI retires for free/Pro/Ultra
tiers on 2026-06-18). It is a **separate engine** that coexists with ``gemini`` — operators
mid-transition have both CLIs installed and both surface their own sessions.

Despite the lineage, ``agy``'s on-disk layout is NOT gemini's (verified against ``agy`` 1.0.8, not
the issue's guesses): state lives under ``~/.gemini/antigravity-cli/`` (not ``~/.antigravity/``),
and a conversation is

* ``conversations/<uuid>.db`` — a **SQLite** database of protobuf-encoded steps, and
* ``brain/<uuid>/.system_generated/logs/transcript.jsonl`` — a plaintext JSONL transcript.

Like Claude/codex/gemini this is **file-based + read-only + fail-soft**: a parse/IO error or a
conversation with no resolvable cwd skips that row, never the whole list. The conversation cwd
(launch dir + open-path allowlist key) comes from agy's ``cache/last_conversations.json`` when the
conversation is the most-recent in its workspace, else from the ``file://`` workspace URI embedded
in the SQLite ``trajectory_metadata_blob``. Resume is ``agy --conversation <uuid>`` (a global id, no
cwd scoping). ``agy`` exposes no flag to pin a new conversation id, so ``supports_new`` is False —
this provider is read / attach / resume only. Archive is not an agy concept, so it rides the
engine-agnostic sidecar (like codex/gemini/opencode).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import metadata as _metadata
from ..scanner import Session
from . import base

# An agy ``USER_INPUT`` step wraps the human text in ``<USER_REQUEST>…</USER_REQUEST>``, alongside
# ``<ADDITIONAL_METADATA>`` / ``<USER_SETTINGS_CHANGE>`` blocks the model gets but we drop.
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def _user_request_text(content: object) -> str:
    """Human text of an agy ``USER_INPUT`` content string — the ``<USER_REQUEST>`` body, or the raw
    (stripped) text if the wrapper is absent."""
    if not isinstance(content, str):
        return ""
    m = _USER_REQUEST_RE.search(content)
    return (m.group(1) if m else content).strip()


def _transcript_path(root: Path, native_id: str) -> Path | None:
    """``brain/<uuid>/.system_generated/logs/transcript.jsonl`` (agy 1.0.8's canonical location),
    with a recursive glob fallback in case a future agy nests the logs differently."""
    canonical = root / "brain" / native_id / ".system_generated" / "logs" / "transcript.jsonl"
    if canonical.is_file():
        return canonical
    try:
        matches = sorted((root / "brain" / native_id).glob("**/transcript.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _first_user_message(root: Path, native_id: str) -> str:
    """First human turn of a conversation, from its JSONL transcript (best-effort, fail-soft)."""
    path = _transcript_path(root, native_id)
    if path is None:
        return ""
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
                if isinstance(rec, dict) and rec.get("type") == "USER_INPUT":
                    text = _user_request_text(rec.get("content"))
                    if text:
                        return text
    except OSError:
        return ""
    return ""


def _file_uri_path(blob: bytes) -> str:
    """The workspace cwd from agy's ``trajectory_metadata_blob`` protobuf. The workspace folder is
    a length-delimited ``file://<abs-path>`` string; the protobuf varint immediately before
    ``file://`` is its byte length, which delimits the path from the following protobuf tag (a bare
    "scan until non-path byte" would over-read, since the next tag byte can be a valid path char).
    Best-effort: "" if absent or the length doesn't frame a valid ``file://`` URI (row skipped)."""
    marker = blob.find(b"file://")
    if marker < 1:
        return ""
    # Read the length varint backwards from just before the URI. A protobuf varint is little-endian
    # base-128; every byte but the last carries a 0x80 continuation bit, so the run of varint bytes
    # is [<0x80-set>…]<0x80-clear> ending at marker-1.
    start = marker - 1
    while start > 0 and blob[start - 1] & 0x80:
        start -= 1
    length = 0
    for shift, byte in enumerate(blob[start:marker]):
        length |= (byte & 0x7F) << (7 * shift)
    uri = blob[marker : marker + length]
    if len(uri) != length:
        return ""
    try:
        text = uri.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if not text.startswith("file://"):
        return ""
    path = unquote(urlparse(text).path)
    return path if path.startswith("/") else ""


def _db_cwd(db_path: Path) -> str:
    """Conversation cwd from its SQLite ``trajectory_metadata_blob`` (read-only, fail-soft)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    if not row or not isinstance(row[0], bytes | bytearray):
        return ""
    return _file_uri_path(bytes(row[0]))


def _cwd_by_id(root: Path) -> dict[str, str]:
    """uuid → cwd from agy's ``cache/last_conversations.json`` (a cwd → latest-uuid map, reversed).
    Only the most-recent conversation per cwd appears there, so it's a robust JSON fast-path;
    everything else falls back to the SQLite blob scrape. Best-effort, fail-soft."""
    path = root / "cache" / "last_conversations.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {v: k for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


class AntigravityProvider:
    """Antigravity CLI (``agy``): SQLite conversations under ``~/.gemini/antigravity-cli/`` resumed
    via ``agy --conversation <uuid>``. Separate engine from ``gemini``; read/attach/resume only."""

    engine_id = "antigravity"
    id_pattern = base._ANTIGRAVITY_UUID_RE
    supports_new = False  # agy mints its own conversation id; no flag to pin one (verified 1.0.8)

    def is_present(self) -> bool:
        return base._antigravity_dir().is_dir() or shutil.which("agy") is not None

    def scan(self) -> list[Session]:
        root = base._antigravity_dir()
        out: list[Session] = []
        try:
            dbs = list((root / "conversations").glob("*.db"))
        except OSError:
            return out
        cache_cwd = _cwd_by_id(root)
        for db in dbs:
            native_id = db.stem
            if not self.id_pattern.match(native_id):
                continue
            try:
                mtime = db.stat().st_mtime
            except OSError:
                continue
            # cwd is the launch dir + open-path allowlist key. No usable cwd -> skip the row
            # (fail-soft, like gemini's unmapped-project skip), never a bogus empty-cwd entry.
            cwd = cache_cwd.get(native_id) or _db_cwd(db)
            if not cwd:
                continue
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=native_id,
                    cwd=cwd,
                    last_mtime=mtime,
                    first_user_message=_first_user_message(root, native_id),
                    archived=False,
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # agy resumes a conversation by its global UUID (verified: `agy --conversation <uuid>`).
        # `bypass` maps to `--dangerously-skip-permissions` (auto-approve tool calls); agy has no
        # separate workspace-trust flag, unlike gemini's `--skip-trust`.
        argv = [base.AGY_BIN, "--conversation", native_id]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # agy mints its own conversation id and exposes no flag to pin one (verified against
        # `agy --help`, 1.0.8), so a new session can't be keyed before agy writes it. Read/attach/
        # resume only (`supports_new = False`); the ws new-session path never calls this.
        raise NotImplementedError(
            "antigravity (agy) cannot start a session with a caller-chosen id"
        )

    def archive(self, native_id):
        # agy conversations stay read-only; the archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
