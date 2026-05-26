"""Engine providers — one interface over the agent CLIs whose sessions the
sidebar organizes.

Each provider knows how to discover its sessions, validate its native id shape,
build the launch argv (the ws PTY bridge spawns it via dtach), and (optionally)
archive. A small registry merges the **present** providers so ``/api/sessions``
is engine-agnostic.

**Engine-qualified identity.** A session's app-facing id is ``<engine>:<native_id>``
(e.g. ``claude:<uuid>``). Threading that through the API routes, the metadata
sidecar keys, and per-engine id validation means ids from different engines can
never collide or hit the wrong validator. ``parse_key`` is the single gate that
resolves an id to its provider and validates the native shape before any dispatch.

Providers expose ``launch_argv`` / ``new_launch_argv`` — a raw argv the ws bridge
runs under dtach — rather than a Zellij dispatch layer. See #10/#11/#12, #49, #64.

**Shell-free:** providers build argv lists and delegate the actual ``subprocess``
exec to the PTY bridge / ``archive``; no provider invokes a shell.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import archive as _archive
from . import metadata as _metadata
from . import scanner
from .scanner import Session

_CLAUDE_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SES_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
# codex session ids are UUIDs (UUIDv7), same shape as Claude's.
_CODEX_UUID_RE = _CLAUDE_UUID_RE

CLAUDE_BIN = os.environ.get("AGENT_SESSIONS_CLAUDE_BIN") or shutil.which("claude") or "claude"
OPENCODE_BIN = (
    os.environ.get("AGENT_SESSIONS_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
)
# Engine binaries are commonly off the login PATH (npm-global, ~/.codex, …), so an
# explicit env override is the reliable launch mechanism; PATH lookup is a fallback.
CODEX_BIN = os.environ.get("AGENT_SESSIONS_CODEX_BIN") or shutil.which("codex") or "codex"
GEMINI_BIN = os.environ.get("AGENT_SESSIONS_GEMINI_BIN") or shutil.which("gemini") or "gemini"
# gemini session ids are UUIDs, same shape as Claude/codex.
_GEMINI_UUID_RE = _CLAUDE_UUID_RE


def _gemini_tmp_dir() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_GEMINI_TMP_DIR") or (Path.home() / ".gemini" / "tmp")
    )


def _codex_sessions_dir() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_CODEX_SESSIONS_DIR") or (Path.home() / ".codex" / "sessions")
    )


# rollout-<iso-ts>-<uuid>.jsonl  →  capture the trailing uuid
_CODEX_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)
# Columns the opencode reader depends on, pinned so a schema rename fails the
# fixture test (loud) rather than silently dropping rows in prod (it would just
# fail-soft to no opencode rows).
OPENCODE_SCHEMA = ("id", "parent_id", "directory", "title", "time_updated", "time_archived")


def _opencode_db() -> str:
    return os.environ.get("AGENT_SESSIONS_OPENCODE_DB") or str(
        Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    )


class EngineError(RuntimeError):
    """Unknown engine, malformed native id, or an operation the engine refuses."""


@runtime_checkable
class EngineProvider(Protocol):
    """The contract every engine implements. Claude is the reference impl."""

    engine_id: str
    id_pattern: re.Pattern

    def is_present(self) -> bool: ...
    def scan(self) -> list[Session]: ...
    def launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]: ...
    def new_launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        """Argv to start a *fresh* session with a caller-chosen id (ws new-session,
        #49). Engines that can't pin a new session id raise NotImplementedError."""
        ...

    def archive(self, native_id: str) -> None: ...
    def unarchive(self, native_id: str) -> None: ...


class ClaudeProvider:
    """Claude Code: sessions under ``~/.claude/projects``, resumed via ``claude --resume``.

    Delegates to the existing ``scanner`` / ``archive`` modules — this provider is a
    thin adapter, so the Claude behavior is byte-for-byte what it was before the
    abstraction.
    """

    engine_id = "claude"
    id_pattern = _CLAUDE_UUID_RE
    supports_new = True  # ws new-session via new_launch_argv

    def is_present(self) -> bool:
        return (Path.home() / ".claude" / "projects").is_dir() or shutil.which("claude") is not None

    def scan(self) -> list[Session]:
        # scanner is Claude-only today; filter defensively so this stays correct
        # if a future scanner ever yields more than one engine.
        return [s for s in scanner.scan() if s.engine == self.engine_id]

    def launch_argv(self, native_id, *, cwd, bypass):
        # Resume command for the per-session PTY bridge (issue #49); cwd is set by the
        # launcher, not an argv arg here.
        argv = [CLAUDE_BIN, "--resume", native_id]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *new* claude session with our pre-generated id (`--session-id`),
        # so the bridge can key it before claude has written its JSONL.
        argv = [CLAUDE_BIN, "--session-id", native_id]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        return argv

    def archive(self, native_id):
        _archive.archive(native_id)

    def unarchive(self, native_id):
        _archive.unarchive(native_id)


class OpenCodeProvider:
    """opencode: sessions live in a SQLite DB (``~/.local/share/opencode/opencode.db``),
    resumed via ``opencode <dir> --session <id>``.

    **Read-only to opencode.db:** the sidebar never writes opencode's DB.
    ``archive``/``unarchive`` raise ``NotImplementedError`` (surfaced as a 4xx) —
    our archive moves the Claude JSONL, which opencode has no equivalent for.
    Rename/sticky *do* work for opencode: they write the engine-agnostic sidecar
    (``metadata.json``), never ``opencode.db``. All DB access is read-only and
    **fail-soft**: any sqlite error (missing / locked / corrupt / schema drift)
    yields no opencode rows rather than taking down the Claude list.
    """

    engine_id = "opencode"
    id_pattern = _SES_RE
    # No ws new-session path yet: new_launch_argv raises NotImplementedError, so we must
    # NOT advertise opencode in /api/config's new_session_engines (else the picker offers
    # it and /ws/term/opencode:…?new=1 closes 4404). Flip to True once a pinned opencode
    # new-launch is implemented (#64 review).
    supports_new = False

    def _query(self) -> list:
        db = _opencode_db()
        if not os.path.exists(db):
            return []
        cols = ", ".join(OPENCODE_SCHEMA)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("PRAGMA busy_timeout=500")
                return con.execute(
                    f"SELECT {cols} FROM session WHERE parent_id IS NULL"  # noqa: S608 fixed cols
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    def is_present(self) -> bool:
        # Present only when the DB is actually *readable* (not merely that the file
        # or binary exists) — so a half-installed / locked opencode stays silent.
        db = _opencode_db()
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

    def scan(self) -> list[Session]:
        out: list[Session] = []
        for sid, _parent, directory, title, time_updated, time_archived in self._query():
            if not isinstance(sid, str) or not self.id_pattern.match(sid):
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
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # opencode resumes a session by id within its project dir. `bypass` is
        # accepted only for interface parity (permissions are config-side).
        return [OPENCODE_BIN, cwd, "--session", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        raise NotImplementedError("opencode ws new-session not supported yet")

    def archive(self, native_id):
        # opencode.db stays read-only; record the archive flag in the engine-agnostic
        # sidecar (same place rename/sticky live). list_sessions ORs it into the row.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)


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
    id_pattern = _CODEX_UUID_RE
    supports_new = False  # codex resume-only (no pinned new-session yet)

    def is_present(self) -> bool:
        return _codex_sessions_dir().is_dir() or shutil.which("codex") is not None

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
        root = _codex_sessions_dir()
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
        return [CODEX_BIN, "resume", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        raise NotImplementedError("codex ws new-session not supported yet")

    def archive(self, native_id):
        # codex rollouts stay read-only; archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)


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
    id_pattern = _GEMINI_UUID_RE
    supports_new = True  # new session with a pinned id via `gemini --session-id <uuid>`

    def is_present(self) -> bool:
        return _gemini_tmp_dir().is_dir() or shutil.which("gemini") is not None

    def _project_map(self) -> dict[str, str]:
        """``projectHash -> cwd`` from ``tmp/project-map.json`` (best-effort, fail-soft)."""
        try:
            data = json.loads((_gemini_tmp_dir() / "project-map.json").read_text())
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
        root = _gemini_tmp_dir()
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
                mtime = path.stat().st_mtime
            except OSError:
                continue
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=sid,
                    cwd=cwd,
                    last_mtime=mtime,
                    first_user_message=first_user,
                    archived=False,
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # gemini resumes by uuid, scoped to the cwd's project chats dir (set by the
        # launcher). `bypass` maps to gemini's "open straight in" flags: --yolo
        # (auto-approve tools, mirrors claude's --dangerously-skip-permissions) and
        # --skip-trust (skip the workspace-trust prompt).
        argv = [GEMINI_BIN, "--resume", native_id]
        if bypass:
            argv += ["--yolo", "--skip-trust"]
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *new* gemini session with our pre-generated id so the bridge can key it
        # before gemini has written its chat file.
        argv = [GEMINI_BIN, "--session-id", native_id]
        if bypass:
            argv += ["--yolo", "--skip-trust"]
        return argv

    def archive(self, native_id):
        # gemini chat logs stay read-only; archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)


# Registry. Order is scan/display order; a provider only surfaces when present.
_PROVIDERS: list[EngineProvider] = [
    ClaudeProvider(),
    OpenCodeProvider(),
    CodexProvider(),
    GeminiProvider(),
]
_BY_ID: dict[str, EngineProvider] = {p.engine_id: p for p in _PROVIDERS}


def all_providers() -> list[EngineProvider]:
    return list(_PROVIDERS)


def present_providers() -> list[EngineProvider]:
    """Providers usable on this host (binary and/or data store present)."""
    return [p for p in _PROVIDERS if p.is_present()]


def get(engine_id: str) -> EngineProvider | None:
    return _BY_ID.get(engine_id)


def scan_all() -> list[Session]:
    """Every session from every present provider, merged."""
    out: list[Session] = []
    for p in present_providers():
        out.extend(p.scan())
    return out


def session_key(s: Session) -> str:
    """The engine-qualified identity for a scanned session."""
    return f"{s.engine}:{s.uuid}"


def parse_key(raw: str) -> tuple[EngineProvider, str]:
    """Resolve an engine-qualified id (``engine:native_id``) to (provider, native_id).

    Back-compat: a bare value matching Claude's UUID shape is treated as a Claude
    id, so pre-multi-engine clients / bookmarks keep working. Raises ``EngineError``
    on an unknown engine or a native id that fails the provider's pattern — this is
    the validation gate before any dispatch.
    """
    if ":" in raw:
        engine_id, _, native = raw.partition(":")
        prov = _BY_ID.get(engine_id)
        if prov is None:
            raise EngineError(f"unknown engine: {engine_id!r}")
    else:
        prov = _BY_ID["claude"]
        native = raw
    if not prov.id_pattern.match(native):
        raise EngineError(f"bad {prov.engine_id} id: {native!r}")
    return prov, native


def canonical_key(raw: str) -> str:
    """Normalize a raw/back-compat id to its canonical ``engine:native_id`` form."""
    prov, native = parse_key(raw)
    return f"{prov.engine_id}:{native}"


__all__ = [
    "EngineError",
    "EngineProvider",
    "ClaudeProvider",
    "OpenCodeProvider",
    "CodexProvider",
    "GeminiProvider",
    "all_providers",
    "present_providers",
    "get",
    "scan_all",
    "session_key",
    "parse_key",
    "canonical_key",
]
