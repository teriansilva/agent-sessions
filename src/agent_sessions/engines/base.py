"""Shared engine contract: the :class:`EngineProvider` protocol, :class:`EngineError`, the
native-id patterns, and the store-location helpers (split out of the old single-file
``engines.py`` in #265 S1).

There are no binary constants here any more (#853 P3): every exec — launch, resume, usage probe —
resolves its binary from the engine's manifest through ``plugins.provenance``. The store helpers
(``_codex_sessions_dir`` & co) are thin, patchable names over one resolver that reads the
manifest, and inside a :func:`store_scope` they resolve the REQUESTING engine's own store.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# Checked enumeration for maintenance (#993) lives in the leaf module `agent_sessions.checked_scan`
# so `scanner.py` can use it without importing the engines package, which would be circular
# (`engines/__init__` → `registry` → `scanner`). Re-exported here because providers already hold
# `base`.
from ..checked_scan import checked_rows, scandir_checked  # noqa: F401

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
# shell (terminal-as-agent, #636): a plain login shell has no engine store and no native id of its
# own, so we mint our own UUID (same shape as Claude's) as the permanent bookkeeping key.
_SHELL_UUID_RE = _CLAUDE_UUID_RE
# Kimi Code (#714) ids are UUIDs with a literal ``session_`` prefix — verified against the live
# store (``session_index.jsonl`` rows + the on-disk session dir names), NOT bare UUIDs. Reusing
# ``_CLAUDE_UUID_RE`` here would make ``parse_key`` reject every real Kimi session.
_KIMI_SESSION_RE = re.compile(
    r"^session_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# --- per-engine store locations (env-overridable) -------------------------------------------
#
# Each is a thin, patchable name over the ONE resolver (#853 P3): the location comes from the
# manifest whose `store.layout` this is — its root, `store.env_override` and `store.path_env` —
# never from a second copy kept here. Keyed by LAYOUT (kind vocabulary), not by engine id.
# ``home`` defaults to ``Path.home()``; it is injectable so the transcript adapters can resolve the
# SAME store under a test home, while an env override — when set — still wins for both.


#: The engine a reusable reader KIND is currently reading for (#853 P3, Hermes on PR #1127). A
#: transcript adapter or usage reporter is shaped by a store FORMAT, and more than one engine may
#: select it; the store it reads must be the requesting engine's, never whichever engine happens to
#: own the layout. The dispatch points (`transcript.adapter_for` & co, `agent_usage.REPORTERS`)
#: set this around the call; outside a scope, a store kind reading its own layout resolves as
#: before.
_STORE_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "store_scope", default=None
)


@contextlib.contextmanager
def store_scope(engine_id: str | None):
    token = _STORE_SCOPE.set(engine_id)
    try:
        yield
    finally:
        _STORE_SCOPE.reset(token)


def _store(layout: str, name: str | None = None, home: Path | None = None) -> Path:
    from . import registry

    scoped = _STORE_SCOPE.get()
    if scoped is not None:
        return registry.store_for_engine(scoped, name, home)
    return registry.store_for_layout(layout, name, home)


def _gemini_tmp_dir(home: Path | None = None) -> Path:
    return _store("gemini-tmp", home=home)


# agy (Antigravity CLI) state lives under ``~/.gemini/antigravity-cli/`` — NOT ``~/.antigravity/``
# (verified against agy 1.0.8). Conversations live in ``conversations/<uuid>.db`` (SQLite) and
# transcripts in ``brain/<uuid>/**/transcript.jsonl``; the kind + adapter derive those from here.
def _antigravity_dir(home: Path | None = None) -> Path:
    return _store("antigravity-cli", home=home)


def _kimi_dir(home: Path | None = None) -> Path:
    """Kimi Code's state root (#714). Everything the kind reads hangs off it: the
    ``session_index.jsonl`` fast path, and ``sessions/wd_<slug>_<hash>/session_<uuid>/``."""
    return _store("kimi-code", home=home)


def _codex_sessions_dir(home: Path | None = None) -> Path:
    return _store("codex-rollouts", home=home)


def _opencode_db(home: Path | None = None) -> str:
    return str(_store("opencode-sqlite", "db", home))


def _opencode_log(home: Path | None = None) -> Path:
    """opencode's structured log — the START artifact for an unattended launch (#1050).

    Its SQLite store cannot answer "did an agent start": measured on 2026-09-21, a launched,
    painted, ready opencode wrote **no** ``session`` row for 45 s with nothing typed (#916's
    deadlock). This file gets a ``creating instance`` line carrying the launch directory ~1.8 s in.
    Env-overridable (`store.path_env.log`) so tests never read the operator's real log.
    """
    return _store("opencode-sqlite", "log", home)


def _shell_dir(home: Path | None = None) -> Path:
    """Per-session record store for the shell engine (#636): no native store to scan, so the kind
    keeps one JSON record per session here. Env-overridable so tests never touch real state."""
    return _store("shell-records", home=home)


# --- contract -------------------------------------------------------------------------------


# Defined in a leaf module so `plugins` can raise it without importing this package (#853 P2).
from ..engine_errors import EngineError  # noqa: E402, F401 — re-exported


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


# --- unattended launch contract (#989) ------------------------------------------------------
#
# An engine that mints its own session id can be dispatched unattended only if it answers three
# questions the dispatcher cannot answer for it, each as an OPTIONAL provider method:
#
#   unattended_preflight(*, cwd, probe, gate) -> (PREFLIGHT_*, why)
#       Before spawn, inside the session's single-writer lock, with the pinned cwd. Reads run
#       OUTSIDE the global launch-policy fence; `gate` is a context manager the provider enters
#       around each process it spawns and nothing else (#921). `probe` owns those processes.
#   start_evidence(launch: LaunchContext) -> (EVIDENCE_*, detail)
#       After spawn and BEFORE any byte is typed. Keyed on the launch, never on the id.
#   bind_session(launch: LaunchContext) -> Binding
#       Polled after delivery until the binding deadline. `bound` carries a proof or is not one.
#       It MUST be a bounded, side-effect-free read: the dispatcher stops waiting on a call at the
#       deadline and cannot cancel the worker thread it ran on, so an implementation that blocks
#       on I/O it does not bound itself outlives the dispatch it was asked about.
#
# `snapshot_session_ids(cwd)` (the reconcile adapters every late-id engine already has) is the
# fourth. An engine missing any of them is refused (`registry.unattended_start_state`). A pinned-id
# engine keeps the existing dispatch path: its id is known before the launch, so it is bound then.

PREFLIGHT_OK = "ok"
PREFLIGHT_REFUSED = "refused"
PREFLIGHT_UNKNOWN = "unknown"

#: The same three words `start_evidence` uses for claude, so one reason formatter serves both.
EVIDENCE_FOUND = "found"
EVIDENCE_ABSENT = "absent"
EVIDENCE_UNREADABLE = "unreadable"

BIND_BOUND = "bound"
BIND_PENDING = "pending"
BIND_AMBIGUOUS = "ambiguous"
#: NOT a flavour of `pending`: "not there yet" and "could not look" are opposite facts, and only the
#: first may be reported as the id never appearing.
BIND_UNREADABLE = "unreadable"

#: The attempt nonce appears in the session's first user turn. Only a session that received this
#: launch's paste can carry it.
PROOF_NONCE = "nonce"
#: A provider-specific fact tying the session to this launch's own process.
PROOF_LINKAGE = "linkage"
BIND_PROOFS = frozenset({PROOF_NONCE, PROOF_LINKAGE})


@dataclass(frozen=True)
class LaunchContext:
    """One unattended launch, as the provider capabilities see it (#989).

    `key` is the PHYSICAL key the master, lock and ring live under — a `<engine>:new-<uuid>`
    placeholder for a late-id engine. `snapshot` is the engine's own id set for `cwd` taken before
    the spawn; `None` means that read failed, and nothing may be bound against it.
    """

    engine: str
    key: str
    native: str
    cwd: str
    nonce: str
    snapshot: frozenset[str] | None = None
    launched_at: float = 0.0


@dataclass(frozen=True)
class Binding:
    """What `bind_session` found. `native` and `proof` are meaningful only when `state` is bound."""

    state: str
    native: str = ""
    proof: str = ""
    detail: str = ""
