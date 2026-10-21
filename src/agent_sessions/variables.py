"""Shared Variables library: stable IDs, typed definitions and explicit collections (#1390).

This is an adapter over the existing encrypted variable store, never a second value store.
All mutations use its outer authority fence before its one JSON lock. A collection groups
explicit IDs; changing it never changes a saved consumer's reference set. Legacy names and
secret encryption identities are immutable, while display labels and collection names may
change. Type changes refuse while a template or project depends on the definition.
"""

from __future__ import annotations

import time
from copy import deepcopy

from . import template_secrets, variable_document, variable_types
from . import template_vars as store
from . import templates as templates


class LibraryNotFound(LookupError):
    """No live global variable or collection has this ID."""


def _public(rec: dict) -> dict:
    result = store.public(rec)
    result["id"] = rec.get("id")
    definition = variable_types.metadata(rec["name"], rec["kind"], rec.get("definition", {}))
    result["definition"] = definition
    result["set"] = not rec.get("unset", False)
    if rec.get("unset"):
        result.pop("value", None)
    elif rec["kind"] == "text":
        result["value"] = variable_types.stored_value(
            rec["name"], definition, rec["value"], max_len=store.VALUE_MAX
        )
    return result


def _snapshot(records: list[dict], collections: list[dict]) -> dict:
    return {
        "variables": [_public(r) for r in sorted(records, key=store._key) if store._is_global(r)],
        "collections": deepcopy(sorted(collections, key=lambda c: (c["name"].casefold(), c["id"]))),
    }


def library() -> dict:
    """One strict, side-effect-free read; old stores remain readable before their first write."""
    raw: dict = {}
    try:
        records, damaged = store._read(store.store_path(), document=raw)
    except store.VariableStoreUnsupported as exc:
        raise store.ResolutionUnavailable(str(exc)) from None
    if damaged:
        raise store.ResolutionUnavailable("the variables store could not be read in full")
    return {
        **_snapshot(records, raw.get("collections", [])),
        "migration_required": bool(raw) and raw.get("version", 0) < 4,
        "limits": {**store.LIMITS, "collections_max": variable_document.COLLECTIONS_MAX},
    }


def migrate() -> dict:
    """Explicitly enable shared references; publish IDs and retain the old file atomically.

    It is also safe to skip this call: the first accepted legacy or shared write performs
    the same upgrade. Reads never mutate files. Repeating this request keeps existing IDs.
    """
    return store._mutate_document(_snapshot)  # type: ignore[return-value]


def _find(records: list[dict], vid: str) -> dict:
    try:
        variable_document.validate_id(vid)
    except variable_document.IdentityError:
        raise LibraryNotFound(vid) from None
    for record in records:
        if record["id"] == vid and store._is_global(record):
            return record
    raise LibraryNotFound(vid)


def _definition(name: str, kind: str, raw: object) -> dict:
    try:
        return variable_types.metadata(name, kind, raw)
    except variable_types.DefinitionError as exc:
        raise store.VariableError(str(exc)) from None


def _value(name: str, kind: str, definition: dict, raw: object) -> str:
    if kind == "secret":
        raw = store._secret_value({"value": raw})
    try:
        native = variable_types.typed_value(name, definition, raw, max_len=store.VALUE_MAX)
    except variable_types.DefinitionError as exc:
        raise store.VariableError(str(exc)) from None
    # Apply the existing terminal-control/length checks to the canonical text as well.
    return store._value({"value": variable_types.text(native)})


def create(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"name", "kind", "definition", "value"}:
        raise store.VariableError("a variable accepts name, kind, definition and value only")
    name = store._name(payload.get("name"))
    kind = store._kind(payload.get("kind"))
    definition = _definition(name, kind, payload.get("definition", {}))
    fields = {"name": name, "kind": kind}
    if "value" in payload:
        fields["value"] = _value(name, kind, definition, payload["value"])

    def fn(records: list[dict]) -> dict:
        globals_ = [r for r in records if store._is_global(r)]
        if any(r["name"] == name for r in globals_):
            raise store.VariableError(f"a variable named {name} already exists")
        dependants = templates.library_references_checked(name)
        projects = store._project_dependants(records, name)
        if dependants or projects:
            raise store.VariableInUse(name, dependants, projects)
        if len(globals_) >= store.VARIABLES_MAX:
            raise store.VariableError("the variables library is full")
        now = time.time()
        record = (
            store._stored(fields, store.SCOPE_GLOBAL, None, now, now)
            if "value" in fields
            else {
                "name": name,
                "kind": kind,
                "scope": store.SCOPE_GLOBAL,
                "project_id": None,
                "unset": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        record["id"] = variable_document.new_id()
        record["definition"] = definition
        records.append(record)
        return _public(record)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def update(vid: str, payload: object, expected: float) -> dict:
    if (
        not isinstance(payload, dict)
        or not payload
        or set(payload) - {"definition", "value", "clear_value"}
    ):
        raise store.VariableError(
            "update accepts definition, value or clear_value; name and kind are immutable"
        )
    if "clear_value" in payload and (payload["clear_value"] is not True or "value" in payload):
        raise store.VariableError("clear_value must be true and cannot accompany a value")

    def fn(records: list[dict]) -> dict:
        rec = _find(records, vid)
        if rec["updated_at"] != expected:
            raise store.VariableConflict(_public(rec))
        old = _definition(rec["name"], rec["kind"], rec["definition"])
        definition = _definition(rec["name"], rec["kind"], payload.get("definition", old))

        # Labels/help are descriptive only. Every contract-affecting edit needs an unused
        # definition; dependants must be explicitly unbound and reviewed first.
        def contract(d: dict) -> dict:
            return {k: v for k, v in d.items() if k not in {"label", "help"}}

        if contract(old) != contract(definition):
            dependants = templates.library_references_checked(rec["name"])
            projects = store._project_dependants(records, rec["name"])
            if dependants or projects:
                raise store.VariableInUse(rec["name"], dependants, projects)
        if (
            rec["kind"] == "secret"
            and contract(old) != contract(definition)
            and "value" not in payload
            and not rec.get("unset")
            and not payload.get("clear_value")
        ):
            raise store.VariableError("re-enter the secret when changing its constraints")
        if payload.get("clear_value"):
            store._retire(rec)
            rec.pop("value", None)
            rec.pop("secret", None)
            rec["unset"] = True
        elif "value" in payload:
            value = _value(rec["name"], rec["kind"], definition, payload["value"])
            if rec["kind"] == "secret":
                replacement = template_secrets.encrypt_scoped(
                    store.SCOPE_GLOBAL, None, rec["name"], value
                )
                store._retire(rec)
                rec["secret"] = replacement
            else:
                rec["value"] = value
            rec.pop("unset", None)
        elif rec["kind"] == "text" and not rec.get("unset"):
            try:
                variable_types.stored_value(
                    rec["name"], definition, rec["value"], max_len=store.VALUE_MAX
                )
            except variable_types.DefinitionError as exc:
                raise store.VariableError(
                    f"provide a compatible value with this definition: {exc}"
                ) from None
        rec["definition"] = definition
        rec["updated_at"] = store._bumped(rec["updated_at"])
        return _public(rec)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def delete(vid: str, expected: float) -> None:
    def fn(records: list[dict], collections: list[dict]) -> None:
        rec = _find(records, vid)
        if rec["updated_at"] != expected:
            raise store.VariableConflict(_public(rec))
        store._delete_record(records, collections, rec)

    store._mutate_document(fn)


def _collection_fields(payload: object, records: list[dict]) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"name", "variable_ids"}:
        raise store.VariableError("a collection accepts name and variable_ids only")
    name = templates._text(payload, "name", max_len=80, required=True)
    members = payload["variable_ids"]
    if not isinstance(members, list) or len(members) > store.VARIABLES_MAX:
        raise store.VariableError("variable_ids must be a list of shared variable IDs")
    if any(not isinstance(v, str) for v in members) or len(set(members)) != len(members):
        raise store.VariableError("a collection includes each variable ID once")
    for vid in members:
        _find(records, vid)  # Must exist as a global in this same locked read.
    return {"name": name, "variable_ids": sorted(members)}


def save_collection(payload: object, cid: str | None = None, expected: float | None = None) -> dict:
    def fn(records: list[dict], collections: list[dict]) -> dict:
        previous = None
        if cid is not None:
            previous = next((c for c in collections if c["id"] == cid), None)
            if previous is None:
                raise LibraryNotFound(cid)
            if previous["updated_at"] != expected:
                raise store.VariableConflict(deepcopy(previous))
        fields = _collection_fields(payload, records)
        if any(
            c["id"] != cid and c["name"].casefold() == fields["name"].casefold()
            for c in collections
        ):
            raise store.VariableError("a collection with this name already exists")
        if previous is None and len(collections) >= variable_document.COLLECTIONS_MAX:
            raise store.VariableError("the collections library is full")
        now = time.time()
        record = {
            **fields,
            "id": cid or variable_document.new_id(),
            "created_at": previous["created_at"] if previous else now,
            "updated_at": store._bumped(previous["updated_at"]) if previous else now,
        }
        if previous is not None:
            collections.remove(previous)
        collections.append(record)
        return deepcopy(record)

    return store._mutate_document(fn)  # type: ignore[return-value]


def delete_collection(cid: str, expected: float) -> None:
    def fn(records: list[dict], collections: list[dict]) -> None:
        rec = next((c for c in collections if c["id"] == cid), None)
        if rec is None:
            raise LibraryNotFound(cid)
        if rec["updated_at"] != expected:
            raise store.VariableConflict(deepcopy(rec))
        collections.remove(rec)

    store._mutate_document(fn)
