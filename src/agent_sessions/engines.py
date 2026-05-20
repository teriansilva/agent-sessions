"""Engine providers — one interface over the agent CLIs whose sessions the
sidebar organizes.

Each provider knows how to discover its sessions, validate its native id shape,
dispatch open/new into Zellij, and (optionally) archive. A small registry merges
the **present** providers so ``/api/sessions`` is engine-agnostic.

**Engine-qualified identity.** A session's app-facing id is ``<engine>:<native_id>``
(e.g. ``claude:<uuid>``). Threading that through the API routes, the metadata
sidecar keys, and per-engine id validation means ids from different engines can
never collide or hit the wrong validator. ``parse_key`` is the single gate that
resolves an id to its provider and validates the native shape before any dispatch.

Claude and opencode are the two providers today. The seam is kept deliberately
small — providers expose ``open_or_switch`` / ``new_session`` rather than a raw
argv plugin layer; Zellij tab naming is engine-prefixed (Claude bare ``<short>:``,
opencode ``o:<short>:``). See #10/#11/#12.

**Shell-free:** providers build argv lists and delegate the actual ``subprocess``
exec to ``zellij`` / ``archive``; no provider invokes a shell.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import archive as _archive
from . import scanner, zellij
from .scanner import Session

_CLAUDE_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SES_RE = re.compile(r"^ses_[A-Za-z0-9]+$")

OPENCODE_BIN = (
    os.environ.get("AGENT_SESSIONS_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
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
    def open_or_switch(
        self, native_id: str, *, cwd: str, title: str, allowed_cwds: Iterable[str], bypass: bool
    ) -> str: ...
    def new_session(
        self, *, cwd: str, title: str, allowed_cwds: Iterable[str], bypass: bool
    ) -> str: ...
    def archive(self, native_id: str) -> None: ...
    def unarchive(self, native_id: str) -> None: ...


class ClaudeProvider:
    """Claude Code: sessions under ``~/.claude/projects``, resumed via ``claude --resume``.

    Delegates to the existing ``scanner`` / ``zellij`` / ``archive`` modules — this
    provider is a thin adapter, so the Claude behavior is byte-for-byte what it was
    before the abstraction.
    """

    engine_id = "claude"
    id_pattern = _CLAUDE_UUID_RE

    def is_present(self) -> bool:
        return (Path.home() / ".claude" / "projects").is_dir() or shutil.which("claude") is not None

    def scan(self) -> list[Session]:
        # scanner is Claude-only today; filter defensively so this stays correct
        # if a future scanner ever yields more than one engine.
        return [s for s in scanner.scan() if s.engine == self.engine_id]

    def open_or_switch(self, native_id, *, cwd, title, allowed_cwds, bypass):
        return zellij.open_or_switch(
            uuid=native_id, cwd=cwd, title=title, allowed_cwds=allowed_cwds, bypass=bypass
        )

    def new_session(self, *, cwd, title, allowed_cwds, bypass):
        return zellij.new_session(cwd=cwd, title=title, allowed_cwds=allowed_cwds, bypass=bypass)

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

    def open_or_switch(self, native_id, *, cwd, title, allowed_cwds, bypass):
        # opencode's permission model is config-side (opencode.json) — there's no
        # per-launch bypass flag; `bypass` is accepted only for interface parity.
        return zellij.open_engine(
            engine_prefix="o",
            short=native_id,
            cwd=cwd,
            title=title,
            allowed_cwds=allowed_cwds,
            argv=[OPENCODE_BIN, cwd, "--session", native_id],
        )

    def new_session(self, *, cwd, title, allowed_cwds, bypass):
        return zellij.new_engine(
            engine_prefix="o",
            cwd=cwd,
            title=title,
            allowed_cwds=allowed_cwds,
            argv=[OPENCODE_BIN, cwd],
        )

    def archive(self, native_id):
        raise NotImplementedError("opencode sessions are read-only in the sidebar")

    def unarchive(self, native_id):
        raise NotImplementedError("opencode sessions are read-only in the sidebar")


# Registry. Order is scan/display order; a provider only surfaces when present.
_PROVIDERS: list[EngineProvider] = [ClaudeProvider(), OpenCodeProvider()]
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
    "all_providers",
    "present_providers",
    "get",
    "scan_all",
    "session_key",
    "parse_key",
    "canonical_key",
]
