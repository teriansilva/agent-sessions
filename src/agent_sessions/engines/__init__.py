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

This used to be one ~670-line module; #265 S1 split it into ``base`` (contract +
patterns + binaries), one module per provider, and ``registry`` (the key functions).
This ``__init__`` re-exports the same public surface, so ``from . import engines`` and
``engines.<name>`` keep working unchanged for callers and tests.
"""

from __future__ import annotations

import shutil  # noqa: F401 — re-exported so `engines.shutil` stays patchable

from .. import metadata as _metadata  # noqa: F401 — exposed for `engines._metadata` in tests
from . import base as base  # noqa: F401 — exposed for `engines.base` (patch target)
from .antigravity import AntigravityProvider
from .base import (  # noqa: F401 — public re-exports
    AGY_BIN,
    BASH_BIN,
    CLAUDE_BIN,
    CODEX_BIN,
    GEMINI_BIN,
    KIMI_BIN,
    OPENCODE_BIN,
    EngineError,
    EngineProvider,
)
from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .kimi import KimiProvider
from .opencode import OPENCODE_SCHEMA, OpenCodeProvider
from .registry import (  # noqa: F401 — public re-exports
    all_providers,
    canonical_key,
    expects_raw_tty,
    get,
    invalidate_scan_cache,
    is_new_session_placeholder,
    is_opencode_new_placeholder,
    logical_key,
    orchestrator_input_engines,
    parse_key,
    physical_key,
    present_providers,
    scan_all,
    scan_all_cached,
    session_key,
    set_scan_cache_ttl,
    supports_orchestrator_input,
)
from .shell import ShellProvider

__all__ = [
    "EngineError",
    "EngineProvider",
    "ClaudeProvider",
    "OpenCodeProvider",
    "CodexProvider",
    "GeminiProvider",
    "AntigravityProvider",
    "KimiProvider",
    "all_providers",
    "present_providers",
    "get",
    "scan_all",
    "scan_all_cached",
    "supports_orchestrator_input",
    "expects_raw_tty",
    "orchestrator_input_engines",
    "invalidate_scan_cache",
    "set_scan_cache_ttl",
    "session_key",
    "parse_key",
    "canonical_key",
    "physical_key",
    "logical_key",
    "is_new_session_placeholder",
    "is_opencode_new_placeholder",
    "CLAUDE_BIN",
    "OPENCODE_BIN",
    "CODEX_BIN",
    "GEMINI_BIN",
    "AGY_BIN",
    "BASH_BIN",
    "KIMI_BIN",
    "ShellProvider",
    "OPENCODE_SCHEMA",
]
