"""Engine registry + the engine-qualified-id key functions (split out of the single-file
``engines.py``, #265 S1).

A small registry merges the **present** providers so ``/api/sessions`` is engine-agnostic,
and ``parse_key`` is the single gate that resolves an id to its provider and validates the
native shape before any dispatch. See the package ``__init__`` docstring for the identity model.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..scanner import Session
from . import base
from .antigravity import AntigravityProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .kimi import KimiProvider
from .opencode import OpenCodeProvider
from .shell import ShellProvider

# Order is scan/display order; a provider only surfaces when present. Shell is last — the agent
# engines lead, and the always-present plain terminal (#636) trails them.
_PROVIDERS: list[base.EngineProvider] = [
    ClaudeProvider(),
    OpenCodeProvider(),
    CodexProvider(),
    GeminiProvider(),
    AntigravityProvider(),
    KimiProvider(),
    ShellProvider(),
]
_BY_ID: dict[str, base.EngineProvider] = {p.engine_id: p for p in _PROVIDERS}


def all_providers() -> list[base.EngineProvider]:
    return list(_PROVIDERS)


def present_providers() -> list[base.EngineProvider]:
    """Providers usable on this host (binary and/or data store present)."""
    return [p for p in _PROVIDERS if p.is_present()]


def get(engine_id: str) -> base.EngineProvider | None:
    return _BY_ID.get(engine_id)


def scan_all() -> list[Session]:
    """Every session from every present provider, merged."""
    out: list[Session] = []
    for p in present_providers():
        out.extend(p.scan())
    return out


# Short-lived scan-snapshot cache (#561). A single `/api/sessions` request re-walks the whole
# ``~/.claude/projects`` tree with three reads per JSONL, and a keystroke burst (no debounce),
# the 15 s poll, and "load more" pagination each trigger a full walk. This memoises the parsed
# ``scan_all()`` snapshot for a short TTL so that burst collapses to a single real disk walk. Only
# the DISK WALK is cached — routes still build rows from fresh ``metadata.load()`` / ``webterm``
# state every request, so the live "working" signal, favorites, renames, etc. never go stale.
#
# Keyed on ``str(Path.home())`` (re-resolved per call) because the Claude scanner walks
# ``Path.home()/.claude/projects`` — tests monkeypatch ``$HOME`` to a ``mktemp -d`` home, so a
# global singleton would leak one test's sessions into another. A single lock makes the miss
# single-flight: two concurrent requests within the TTL yield at most one real walk (the second
# blocks on the lock, then reads the just-populated entry).
#
# TTL (#652 L1): at 1.5 s the snapshot was warm for only ~1.5 s of each 15 s poll window, so a
# deliberate search keystroke-settle or a project switch between polls almost always landed on a
# COLD walk of the whole live+archive tree (~3 reads per JSONL). Raised to 10 s so those actions
# hit a warm snapshot. Safe because every in-app write that changes what the scanner sees already
# calls ``invalidate_scan_cache()`` (archive/unarchive/new-session), so the only staleness this
# guards is a session created OUTSIDE the app (a CLI launch / a running agent writing a fresh
# JSONL) — already bounded by the 15 s poll, which re-walks on cache expiry (10 s < 15 s).
_SCAN_CACHE_TTL_S = 10.0
_scan_cache_lock = threading.Lock()
_scan_cache: dict[str, tuple[float, list[Session]]] = {}


def set_scan_cache_ttl(seconds: float) -> None:
    """Set the scan-snapshot TTL (seconds). ``0`` disables caching — the test suite sets this so
    each request re-walks (mutation-then-rescan tests stay deterministic); the dedicated cache
    tests opt back in."""
    global _SCAN_CACHE_TTL_S
    _SCAN_CACHE_TTL_S = max(0.0, float(seconds))


def invalidate_scan_cache() -> None:
    """Drop every cached scan snapshot. Called after any write that changes what the scanner sees
    (archive/unarchive — Claude moves the JSONL; a new-session launch writes a fresh JSONL) so the
    next list request re-walks instead of serving the just-mutated tree stale."""
    with _scan_cache_lock:
        _scan_cache.clear()


def scan_all_cached() -> list[Session]:
    """``scan_all()`` behind the short TTL + single-flight cache (#561), keyed on the effective
    home. Read path for the sidebar list; falls straight through when the TTL is 0.

    Resolves ``scan_all`` through the package namespace so a ``monkeypatch.setattr(engines,
    "scan_all", …)`` (the established test seam) is honoured here too."""
    from .. import engines as _pkg

    if _SCAN_CACHE_TTL_S <= 0:
        return _pkg.scan_all()
    key = str(Path.home())
    with _scan_cache_lock:
        hit = _scan_cache.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < _SCAN_CACHE_TTL_S:
            return hit[1]
        sessions = _pkg.scan_all()
        _scan_cache[key] = (time.monotonic(), sessions)
        return sessions


def session_key(s: Session) -> str:
    """The engine-qualified identity for a scanned session."""
    return f"{s.engine}:{s.uuid}"


def is_new_session_placeholder(raw: str) -> bool:
    """True if ``raw`` is an ``<engine>:new-<uuid>`` new-session placeholder for an engine that
    mints its own id and reconciles (opencode, codex — #127/#315). Engine-agnostic: gated on the
    provider's ``new_session_reconciles`` flag, not a hard-coded engine id."""
    if ":" not in raw:
        return False
    engine_id, _, native = raw.partition(":")
    prov = _BY_ID.get(engine_id)
    return bool(getattr(prov, "new_session_reconciles", False)) and bool(
        base._NEW_PLACEHOLDER_RE.match(native)
    )


def is_opencode_new_placeholder(raw: str) -> bool:
    """Deprecated back-compat alias of :func:`is_new_session_placeholder`."""
    return is_new_session_placeholder(raw)


def parse_key(raw: str, *, allow_new_placeholder: bool = False) -> tuple[base.EngineProvider, str]:
    """Resolve an engine-qualified id (``engine:native_id``) to (provider, native_id).

    Back-compat: a bare value matching Claude's UUID shape is treated as a Claude
    id, so pre-multi-engine clients / bookmarks keep working. Raises ``EngineError``
    on an unknown engine or a native id that fails the provider's pattern — this is
    the validation gate before any dispatch.

    ``allow_new_placeholder`` (set ONLY by the ws ``new=1`` launch path, #127/#315) also
    accepts the ``new-<uuid>`` placeholder for any engine whose ``new_session_reconciles``
    flag is set (opencode, codex — they mint their own id). It is NOT accepted on the
    resume/attach path, so a placeholder can never be used to attach to or resume an
    arbitrary session.
    """
    if ":" in raw:
        engine_id, _, native = raw.partition(":")
        prov = _BY_ID.get(engine_id)
        if prov is None:
            raise base.EngineError(f"unknown engine: {engine_id!r}")
    else:
        prov = _BY_ID["claude"]
        native = raw
    if (
        allow_new_placeholder
        and getattr(prov, "new_session_reconciles", False)
        and base._NEW_PLACEHOLDER_RE.match(native)
    ):
        return prov, native
    if not prov.id_pattern.match(native):
        raise base.EngineError(f"bad {prov.engine_id} id: {native!r}")
    return prov, native


def physical_key(key: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve an engine-qualified ``key`` to the PHYSICAL key its live resources are
    under (#127 alias layer).

    For opencode new-session, the dtach socket / single-writer lock / scrollback buffer /
    metadata are all keyed by the ``new-<uuid>`` placeholder the master was launched
    under. Once reconciled, an alias ``placeholder → real`` is persisted; an attach by the
    *real* id must therefore resolve back to the placeholder. So this maps a real id to
    its placeholder (the inverse of the stored map) and leaves everything else unchanged.

    Pass ``aliases`` (``metadata.load_aliases()``) to avoid re-reading the sidecar; omit
    to read it. Idempotent and safe for non-opencode keys (returns ``key``).
    """
    if aliases is None:
        from .. import metadata as _md

        aliases = _md.load_aliases()
    # stored map is placeholder→real; we need real→placeholder for resource lookup.
    for placeholder, real in aliases.items():
        if real == key:
            return placeholder
    return key


def logical_key(key: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve an engine-qualified ``key`` to the LOGICAL key its *engine* knows it by —
    the inverse of :func:`physical_key` (#611).

    A session launched on a mint-its-own-id engine (codex / opencode / antigravity) keeps the
    ``new-<uuid>`` placeholder as its physical key for life: that's what the dtach socket, the
    lock, the ring and the sidecar are keyed by. But the engine's own transcript store is keyed
    by the REAL id it minted. Anything that wants to read that store — the AI reviewer and its
    recap — must map placeholder → real, or ``parse_key`` rejects the placeholder shape and the
    transcript silently reads as empty.

    The stored alias map is already ``placeholder → real``, so this is a direct lookup. Pass
    ``aliases`` (``metadata.load_aliases()``) to avoid re-reading the sidecar; omit to read it.
    Idempotent, and a no-op for pinned-id engines and unreconciled placeholders (returns
    ``key``).
    """
    if aliases is None:
        from .. import metadata as _md

        aliases = _md.load_aliases()
    return aliases.get(key, key)


def canonical_key(raw: str) -> str:
    """Normalize a raw/back-compat id to its canonical ``engine:native_id`` form."""
    prov, native = parse_key(raw)
    return f"{prov.engine_id}:{native}"
