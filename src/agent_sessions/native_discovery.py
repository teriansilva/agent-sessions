"""Keep API-owned native histories out of console discovery (#1277).

Ownership is read AFTER the store reader or cache returns. Neither a file parse cache nor a
paging snapshot freezes it. A pending creation can still list unrelated history, but cannot
answer a late-id reconciliation: its not-yet-bound native id could otherwise be adopted.
"""

from __future__ import annotations

from functools import wraps

from . import native_ownership


def _snapshot(prov):
    source = native_ownership.source_identity(prov)
    return native_ownership.source_snapshot(source) if source is not None else None


def filter_rows(prov, rows, *, strict: bool = False):
    try:
        owned = _snapshot(prov)
    except native_ownership.OwnershipError:
        if strict:
            raise
        return []
    if owned is None or not owned.bound_native_ids:
        return rows
    return [row for row in rows if row.uuid not in owned.bound_native_ids]


def filter_cached(rows):
    """Recheck each represented provider once, preserving row order and the cached list."""
    from . import engines

    hidden = set()
    for engine in {row.engine for row in rows}:
        prov = engines.get_any(engine)
        if prov is None:
            # A row whose provider disappeared is no longer discoverable. In particular its
            # old store binding cannot be reinterpreted using some other provider's identity.
            hidden.update((engine, row.uuid) for row in rows if row.engine == engine)
            continue
        own_rows = [row for row in rows if row.engine == engine]
        visible = {row.uuid for row in filter_rows(prov, own_rows)}
        hidden.update((engine, row.uuid) for row in own_rows if row.uuid not in visible)
    return [row for row in rows if (row.engine, row.uuid) not in hidden] if hidden else rows


def guard_hook(prov, name, fn):
    """Wrap only hooks the store kind actually implements; never add a capability."""
    from .plugins import kinds

    manifest = prov.manifest
    layouts = {shape[0] for shape in kinds.API_SOURCE_KINDS.values()}
    if (
        manifest.runtime != "pty"
        or manifest.store is None
        or manifest.store.layout not in layouts
        or name
        not in {
            "scan_checked",
            "lookup",
            "snapshot_session_ids",
            "reconcile_new_session",
            "bind_session",
        }
    ):
        return fn

    @wraps(fn)
    def guarded(*args, **kwargs):
        result = fn(*args, **kwargs)
        if name == "scan_checked":
            rows, problems = result
            try:
                return filter_rows(prov, rows, strict=True), problems
            except native_ownership.OwnershipError:
                return [], [*problems, f"{prov.engine_id}: native ownership could not be read"]
        if name == "lookup":
            return result if result is not None and filter_rows(prov, [result]) else None
        try:
            owned = _snapshot(prov)
            if owned is None:
                return result
            if owned.pending:
                raise native_ownership.OwnershipError(
                    "pending", "native session creation is awaiting ownership reconciliation"
                )
            if name == "snapshot_session_ids":
                return None if result is None else set(result) - owned.bound_native_ids
            if name == "reconcile_new_session":
                candidates = result if isinstance(result, list) else [result] if result else []
                candidates = [
                    native for native in candidates if native not in owned.bound_native_ids
                ]
                return candidates[0] if len(candidates) == 1 else candidates or None
            if result.native and result.native in owned.bound_native_ids:
                raise native_ownership.OwnershipError(
                    "owned", "native history belongs to an API client"
                )
            return result
        except native_ownership.OwnershipError:
            if name == "snapshot_session_ids":
                raise  # an unreadable snapshot is not an empty pre-launch store
            if name == "reconcile_new_session":
                return None
            from .engines import base

            return base.Binding(base.BIND_UNREADABLE, detail="native ownership is unavailable")

    return guarded
