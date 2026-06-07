"""Engine registry + the engine-qualified-id key functions (split out of the single-file
``engines.py``, #265 S1).

A small registry merges the **present** providers so ``/api/sessions`` is engine-agnostic,
and ``parse_key`` is the single gate that resolves an id to its provider and validates the
native shape before any dispatch. See the package ``__init__`` docstring for the identity model.
"""

from __future__ import annotations

from ..scanner import Session
from . import base
from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import OpenCodeProvider

# Order is scan/display order; a provider only surfaces when present.
_PROVIDERS: list[base.EngineProvider] = [
    ClaudeProvider(),
    OpenCodeProvider(),
    CodexProvider(),
    GeminiProvider(),
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


def canonical_key(raw: str) -> str:
    """Normalize a raw/back-compat id to its canonical ``engine:native_id`` form."""
    prov, native = parse_key(raw)
    return f"{prov.engine_id}:{native}"
