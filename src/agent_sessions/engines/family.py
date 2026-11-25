"""Client families and the default client for a purpose — API first (#1728).

A **family** is every client of one agent: today a console (`pty`) client and, where one exists, its
native API client. Membership is derived from what the manifests already declare
(`Manifest.family`): a native API client belongs to the agent its `api.source` names; every other
manifest is its own family. It is never derived by trimming an engine id, and no manifest can claim
a family: a local console manifest stays its own family whatever its display order, so it can never
become another agent's console fallback.

`default_client(family, purpose)` is THE resolver for which client a new piece of work starts on
when nothing else was chosen. It names no engine: it reads the manifest's `runtime` and the same
readiness answer the new-session picker and `/api/engines` already share (`registry.can_start`).

* ``interactive``: the family's first READY API client (`api_ready`); otherwise its first console
  client that can start, with `api_unready` (it has an API client that cannot start now) or
  `api_absent` (it has none). A family whose every client is retiring, or that is not in the
  roster at all, answers `(None, "family_retired")`.
* ``unattended`` (#1728 Phase 3 consumes it): only API clients the structured facade admits for
  missions (`ClientDescriptor.mission_ready`). There is NO console fallback.

The server computes only this BASE default. A choice the operator made and kept on a device
(`remembered_explicit` / `remembered_stale`) is the SPA's, applied on top; a create then says
whether it is the `automatic` default (`stale_automatic` refuses one the base has since moved
from) or an `explicit` choice (honoured while that client can start).

Every function here may walk directories or run a cached local probe: call it OFF the loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from . import registry

PURPOSES = ("interactive", "unattended")
#: The typed base reasons. The SPA adds `remembered_explicit` / `remembered_stale` on top.
REASONS = ("api_ready", "api_unready", "api_absent", "family_retired")
#: The two provenances a create may state for the client it names.
CHOICES = ("automatic", "explicit")


def family_of(ref) -> str | None:
    """The family key of an engine id, a session key or a provider — or None when unknown."""
    m = registry.manifest_of(ref)
    return getattr(m, "family", None) if m is not None else None


def _members(family: str, providers: Iterable) -> list:
    return [p for p in providers if getattr(p.manifest, "family", None) == family]


def _resolve(
    clients: list,
    purpose: str,
    ready: Callable[[object], bool],
    mission_ready: Callable[[object], bool],
) -> tuple[str | None, str]:
    """The base default among ACTIVE ``clients`` (display order). Pure over its two answers."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")  # a bug, never a default
    if not clients:
        return None, "family_retired"
    api = [p for p in clients if p.manifest.runtime == "api"]
    if purpose == "unattended":
        admitted = [p for p in api if ready(p) and mission_ready(p)]
        if admitted:
            return admitted[0].engine_id, "api_ready"
        return None, "api_unready" if api else "api_absent"
    for p in api:
        if ready(p):
            return p.engine_id, "api_ready"
    console = next((p for p in clients if p.manifest.runtime != "api" and ready(p)), None)
    return (console.engine_id if console else None), ("api_unready" if api else "api_absent")


def _mission_ready(prov) -> bool:
    """`ClientDescriptor.mission_ready` (#1273 P3 sets it); never a readiness probe here."""
    if prov.manifest.runtime != "api":
        return False
    from .. import structured_runtime

    try:
        return bool(structured_runtime.describe(prov.engine_id, check_ready=False).mission_ready)
    except Exception:  # noqa: BLE001 — a client that cannot answer is not admitted
        return False


def default_client(family: str, purpose: str) -> tuple[str | None, str]:
    """``(engine_id | None, reason)`` — the base default client of ``family`` for ``purpose``."""
    clients = [p for p in _members(family, registry.all_providers()) if not registry.is_retiring(p)]
    return _resolve(clients, purpose, registry.can_start, _mission_ready)


def _why_not(prov) -> str:
    """Why a client cannot start a new session right now, in the operator's words."""
    if registry.is_retiring(prov):
        return registry.REMOVED_REASON
    if prov.manifest.runtime == "api":
        return registry.api_unavailable_reason(prov) or "the client is not ready"
    if not getattr(prov, "supports_new", False):
        return "this agent can't start new sessions"
    return "this agent isn't installed on this host"


def families_block(ready_by_id: Mapping[str, bool] | None = None) -> list[dict]:
    """The roster's `families` block (`/api/engines`): one row per family, in display order.

    ``{family, clients, interactive_default: {engine, reason}, unattended_admitted, reasons}`` —
    ``reasons`` maps each client that cannot start now to why. ``ready_by_id`` reuses the
    `can_start` answers the caller already computed for the same request, so one roster read
    never probes a client twice; a missing id is asked."""
    ready_by_id = dict(ready_by_id or {})

    def ready(p) -> bool:
        if p.engine_id not in ready_by_id:
            ready_by_id[p.engine_id] = registry.can_start(p)
        return ready_by_id[p.engine_id]

    active = registry.all_providers()
    retiring = registry.retiring_providers()
    order: list[str] = []
    for p in [*active, *retiring]:
        fam = p.manifest.family
        if fam not in order:
            order.append(fam)
    out = []
    for fam in order:
        live = [p for p in _members(fam, active) if not registry.is_retiring(p)]
        gone = [p for p in [*_members(fam, active), *_members(fam, retiring)] if p not in live]
        engine, reason = _resolve(live, "interactive", ready, _mission_ready)
        admitted = [
            p.engine_id
            for p in live
            if p.manifest.runtime == "api" and ready(p) and _mission_ready(p)
        ]
        reasons = {p.engine_id: _why_not(p) for p in [*live, *gone] if p in gone or not ready(p)}
        out.append(
            {
                "family": fam,
                "clients": [p.engine_id for p in [*live, *gone]],
                "interactive_default": {"engine": engine, "reason": reason},
                "unattended_admitted": admitted,
                "reasons": reasons,
            }
        )
    return out


def stale_automatic(engine_id: str) -> dict | None:
    """For a create that named ``engine_id`` as the AUTOMATIC default: ``None`` while it still is
    its family's interactive default, else the new default ``{family, engine, reason}`` — the
    create is then refused (409 / ws 4412) and nothing launches. An explicit choice never comes
    here. An unknown engine is not this check's to refuse: the create path refuses it."""
    fam = family_of(engine_id)
    if fam is None:
        return None
    engine, reason = default_client(fam, "interactive")
    if engine == engine_id:
        return None
    return {"family": fam, "engine": engine, "reason": reason}


def stale_words(stale: dict) -> str:
    """The refusal's words. Names the new default by its manifest label, never by a literal."""
    m = registry.manifest_of(stale["engine"]) if stale.get("engine") else None
    label = m.identity.label if m is not None else None
    if label is None:
        return "the default for this agent changed and none of its clients can start now"
    return f"the default changed to {label} since this was shown — nothing was started"
