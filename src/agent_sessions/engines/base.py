"""Shared engine contract: the :class:`EngineProvider` protocol, :class:`EngineError`,
the native-id patterns, and the engine binary paths (split out of the old single-file
``engines.py`` in #265 S1).

Provider modules (``claude``/``opencode``/``codex``/``gemini``) read the ``*_BIN`` constants
from **this module at call time** (e.g. ``base.CLAUDE_BIN``), so a test can override a binary
with ``monkeypatch.setattr(engines.base, "CLAUDE_BIN", ...)`` and the provider's launch argv
picks it up. The package ``__init__`` re-exports them as ``engines.CLAUDE_BIN`` for reads.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

# --- native-id patterns ---------------------------------------------------------------------

_CLAUDE_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SES_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
# Client-minted placeholder id for an opencode new-session (#127). opencode mints its
# own ``ses_…`` id (we can't pin one), so the ws/dtach bridge launches under this
# placeholder and later reconciles it to the real id via the persisted alias. Accepted
# ONLY in the ``new=1`` launch path (see ``parse_key(allow_new_placeholder=True)``);
# ``_SES_RE`` stays the validator for resume/attach so a placeholder can never be used
# to attach to or resume a session that isn't ours.
_NEW_PLACEHOLDER_RE = re.compile(
    r"^new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# codex + gemini + antigravity (agy) session ids are UUIDs (UUIDv7), same shape as Claude's.
_CODEX_UUID_RE = _CLAUDE_UUID_RE
_GEMINI_UUID_RE = _CLAUDE_UUID_RE
_ANTIGRAVITY_UUID_RE = _CLAUDE_UUID_RE

# --- engine binaries ------------------------------------------------------------------------
# Engine binaries are commonly off the login PATH (npm-global, ~/.codex, …), so an
# explicit env override is the reliable launch mechanism; PATH lookup is a fallback.

CLAUDE_BIN = os.environ.get("AGENT_SESSIONS_CLAUDE_BIN") or shutil.which("claude") or "claude"
OPENCODE_BIN = (
    os.environ.get("AGENT_SESSIONS_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
)
CODEX_BIN = os.environ.get("AGENT_SESSIONS_CODEX_BIN") or shutil.which("codex") or "codex"
GEMINI_BIN = os.environ.get("AGENT_SESSIONS_GEMINI_BIN") or shutil.which("gemini") or "gemini"
# Antigravity's binary is ``agy`` (not ``antigravity``), so the env knob is keyed on the binary
# name — ``AGENT_SESSIONS_AGY_BIN`` — to match what operators type and what ``doctor`` writes.
AGY_BIN = os.environ.get("AGENT_SESSIONS_AGY_BIN") or shutil.which("agy") or "agy"


# --- per-engine store locations (env-overridable) -------------------------------------------


# ``home`` defaults to ``Path.home()`` (providers call these with no args); it's injectable so the
# transcript adapters can resolve the SAME store under a test home, while the env override — when
# set — still wins for both providers and adapters (single source of truth for the path contract).
def _gemini_tmp_dir(home: Path | None = None) -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_GEMINI_TMP_DIR")
        or ((home or Path.home()) / ".gemini" / "tmp")
    )


# agy (Antigravity CLI) state lives under ``~/.gemini/antigravity-cli/`` — NOT ``~/.antigravity/``
# (verified against agy 1.0.8; the issue's guessed path was wrong). Conversations live in
# ``conversations/<uuid>.db`` (SQLite) and transcripts in ``brain/<uuid>/**/transcript.jsonl``;
# the provider + transcript adapter derive those subpaths from this single root.
def _antigravity_dir(home: Path | None = None) -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_ANTIGRAVITY_DIR")
        or ((home or Path.home()) / ".gemini" / "antigravity-cli")
    )


def _codex_sessions_dir(home: Path | None = None) -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_CODEX_SESSIONS_DIR")
        or ((home or Path.home()) / ".codex" / "sessions")
    )


def _opencode_db(home: Path | None = None) -> str:
    return os.environ.get("AGENT_SESSIONS_OPENCODE_DB") or str(
        (home or Path.home()) / ".local" / "share" / "opencode" / "opencode.db"
    )


# --- contract -------------------------------------------------------------------------------


class EngineError(RuntimeError):
    """Unknown engine, malformed native id, or an operation the engine refuses."""


@runtime_checkable
class EngineProvider(Protocol):
    """The contract every engine implements. Claude is the reference impl."""

    engine_id: str
    id_pattern: re.Pattern

    def is_present(self) -> bool: ...
    def scan(self) -> list: ...
    def launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]: ...
    def new_launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        """Argv to start a *fresh* session with a caller-chosen id (ws new-session,
        #49). Engines that can't pin a new session id raise NotImplementedError."""
        ...

    def archive(self, native_id: str) -> None: ...
    def unarchive(self, native_id: str) -> None: ...
