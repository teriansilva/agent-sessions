"""Native API source validation and joint admission (#1275).

An API manifest selects a reviewed protocol and references a console installation; it grants
no executable or structured-runtime capability. Both providers are captured from ONE roster
and rechecked together against a fresh roster before an adapter may act. Resolving a source
does not launch it, select a model, inherit bypass flags, or claim an adapter is implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..engine_errors import EngineError
from . import kinds, provenance
from .manifest import Manifest

if TYPE_CHECKING:
    from ..engines.registry import Roster
    from .provider import PluginProvider


class SourceError(EngineError):
    """The declared source cannot be used for this native API client."""


def validate_provider(prov: PluginProvider, by_id: Mapping[str, PluginProvider]) -> PluginProvider:
    """Validate a complete candidate roster without consulting another generation or doing I/O.

    Requiring a PTY source rejects API chains and cycles in one pass. Missing/disabled providers
    are absent from the active roster; a retiring provider is refused even if a caller supplies it.
    Binary availability/provenance remains a later execution-time check by the real adapter.
    """
    m = getattr(prov, "manifest", None)
    if not isinstance(m, Manifest) or m.runtime != "api" or m.api is None:
        raise SourceError("the provider is not a native API client")
    if prov.engine_id != m.id or getattr(prov, "retiring", False):
        raise SourceError("the native API client is not active")
    if m.api.source == m.id:
        raise SourceError("api.source must not reference itself")
    source = by_id.get(m.api.source)
    if source is None or getattr(source, "retiring", False):
        raise SourceError(f"api.source {m.api.source!r} is missing, disabled or retiring")
    sm = getattr(source, "manifest", None)
    if (
        not isinstance(sm, Manifest)
        or sm.id != m.api.source
        or source.engine_id != sm.id
        or sm.runtime != "pty"
        or sm.identity.kind != "agent"
        or sm.binary is None
        or sm.api is not None
    ):
        raise SourceError("api.source must be an active console agent, never another API client")
    shape = (sm.store.layout if sm.store else None, sm.transcript_kind)
    if shape != kinds.API_SOURCE_KINDS.get(m.api.kind):
        raise SourceError(f"api.source is incompatible with protocol {m.api.kind!r}")
    return source


def _stamp(prov) -> tuple:
    m = getattr(prov, "manifest", None)
    # Include the digest explicitly: Manifest equality intentionally ignores its loaded bytes.
    # A managed installation replacement must also change this binding even with equal metadata.
    return (
        m,
        getattr(m, "digest", None),
        getattr(prov, "root", None),
        getattr(prov, "_record", None),
    )


@dataclass(frozen=True)
class SourceBinding:
    provider: PluginProvider
    source: PluginProvider
    generation: int
    _provider_stamp: tuple
    _source_stamp: tuple


def resolve(prov: PluginProvider, *, roster: Roster | None = None) -> SourceBinding:
    """Capture a source and its API provider from one admitted roster, without launching either."""
    from ..engines import registry

    roster = registry.current() if roster is None else roster
    current = roster.by_id.get(prov.engine_id)
    if current is None or _stamp(current) != _stamp(prov):
        raise SourceError("the native API client changed or is no longer active")
    source = validate_provider(current, roster.by_id)
    return SourceBinding(current, source, roster.generation, _stamp(current), _stamp(source))


def admits(binding: SourceBinding) -> bool:
    """Recheck BOTH captured providers in a single fresh view; unrelated reloads need not revoke."""
    from ..engines import registry

    try:
        with registry.snapshot_scope(fresh=True, require_current=True) as roster:
            prov = roster.by_id.get(binding.provider.engine_id)
            if prov is None or _stamp(prov) != binding._provider_stamp:
                return False
            source = validate_provider(prov, roster.by_id)
            return _stamp(source) == binding._source_stamp
    except (OSError, ValueError, EngineError, provenance.ProvenanceError):
        return False
