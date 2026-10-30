"""Shared Variables library: stable IDs, typed definitions and explicit collections (#1390).

This is an adapter over the existing encrypted variable store, never a second value store.
All mutations use its outer authority fence before its one JSON lock. A collection groups
explicit IDs; changing it never changes a saved consumer's reference set. Legacy names and
secret encryption identities are immutable, while display labels and collection names may
change. Type changes refuse while a template or project depends on the definition.
"""

from __future__ import annotations

import re
import time
from copy import deepcopy

from . import template_secrets, variable_document, variable_types, vault_files
from . import template_vars as store
from . import templates as templates


class LibraryNotFound(LookupError):
    """No live global variable or collection has this ID."""


def _bound(records: list[dict], name: str) -> list[str]:
    """The projects that bind the global variable ``name`` by explicit ``ref: global`` (#1786):
    the projects whose agents a secret with agent access is offered to."""
    return sorted(
        r["project_id"]
        for r in records
        if r["scope"] == store.SCOPE_PROJECT
        and r["name"] == name
        and r.get("ref") == store.REF_GLOBAL
    )


def _public(rec: dict, records: list[dict] | None = None) -> dict:
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
    if rec["kind"] in store.SECRET_KINDS:
        # Agent access (#1786): whether agents may request it. A secret file also shows its
        # display name and size — never content, digest or revision.
        result["agent_access"] = rec.get("agent_access", False)
    if rec["kind"] == "secret_file" and "file" in rec:
        result["file"] = {"name": rec["file"]["name"], "size": rec["file"]["size"]}
    if rec["kind"] in store.SECRET_KINDS and records is not None:
        result["projects"] = _bound(records, rec["name"])
    return result


def _snapshot(records: list[dict], collections: list[dict]) -> dict:
    return {
        "variables": [
            _public(r, records) for r in sorted(records, key=store._key) if store._is_global(r)
        ],
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
    if kind == "secret_file" and "value" in payload:
        raise store.VariableError("create the secret file without a value, then upload the file")
    if "value" in payload:
        fields["value"] = _value(name, kind, definition, payload["value"])

    def fn(records: list[dict]) -> dict:
        _withhold_metadata(name, definition)
        if kind == "secret" and "value" in fields:
            store.refuse_secret_in_labels(
                records,
                fields["value"],
                store._exposed_labels([{"name": name, "definition": definition}]),
            )
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
        return _public(record, records)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def update(vid: str, payload: object, expected: float) -> dict:
    if (
        not isinstance(payload, dict)
        or not payload
        or set(payload) - {"definition", "value", "clear_value", "agent_access"}
    ):
        raise store.VariableError(
            "update accepts definition, value, clear_value or agent_access; name and kind are "
            "immutable"
        )
    if "agent_access" in payload and type(payload["agent_access"]) is not bool:
        raise store.VariableError("agent_access is true or false")
    if "clear_value" in payload and (payload["clear_value"] is not True or "value" in payload):
        raise store.VariableError("clear_value must be true and cannot accompany a value")

    def fn(records: list[dict]) -> dict:
        rec = _find(records, vid)
        if rec["updated_at"] != expected:
            raise store.VariableConflict(_public(rec))
        old = _definition(rec["name"], rec["kind"], rec["definition"])
        definition = _definition(rec["name"], rec["kind"], payload.get("definition", old))
        if definition != old:
            _withhold_metadata(rec["name"], definition, old)

        # Labels/help are descriptive only. Every contract-affecting edit needs an unused
        # definition; dependants must be explicitly unbound and reviewed first.
        def contract(d: dict) -> dict:
            return {k: v for k, v in d.items() if k not in {"label", "help"}}

        if contract(old) != contract(definition):
            dependants = templates.library_references_checked(rec["name"])
            projects = store._project_dependants(records, rec["name"])
            if dependants or projects:
                raise store.VariableInUse(rec["name"], dependants, projects)
        if "agent_access" in payload:
            if rec["kind"] not in store.SECRET_KINDS:
                raise store.VariableError("agent access applies to secrets and secret files only")
            if payload["agent_access"]:
                rec["agent_access"] = True
            else:
                rec.pop("agent_access", None)
        if rec["kind"] == "secret_file" and "value" in payload:
            raise store.VariableError("a secret file is replaced by uploading a file")
        if (
            rec["kind"] in store.SECRET_KINDS
            and contract(old) != contract(definition)
            and "value" not in payload
            and not rec.get("unset")
            and not payload.get("clear_value")
        ):
            raise store.VariableError("re-enter the secret when changing its constraints")
        if payload.get("clear_value"):
            store._retire(rec)
            store._drop_file(rec)
            rec.pop("value", None)
            rec.pop("secret", None)
            rec.pop("file", None)
            rec["unset"] = True
        elif "value" in payload:
            value = _value(rec["name"], rec["kind"], definition, payload["value"])
            if rec["kind"] == "secret":
                store.refuse_secret_in_labels(
                    records,
                    value,
                    store._exposed_labels([{"name": rec["name"], "definition": definition}]),
                )
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
        return _public(rec, records)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def _withhold_metadata(name: str, definition: dict, old: dict | None = None) -> None:
    """Every label an agent or the access log shows — the name, the label, the help text —
    passes the withholding check (#1786). Called INSIDE the store's transaction, so it judges
    the secret inventory as of the commit (Hermes 7394, 2 and 5)."""
    if old is None:
        _withhold_label(name, "name")
    for key, what in (("label", "label"), ("help", "description")):
        # On an edit only a field that CHANGED is judged: an old label never blocks a new one.
        if definition.get(key) and (old is None or old.get(key) != definition[key]):
            _withhold_label(str(definition[key]), what)


#: Value shapes a label must not hold (#1786). Deliberately NOT `credential_shaped`, which leaves
#: whole chat messages out and fires on ordinary help text ("API token: create it under …",
#: "Bearer token sent as the Authorization header"): a label is refused only for a stored secret
#: or for something that IS a credential value — PEM, URL userinfo, a known token prefix, or a long
#: random run.
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})"
)
LABEL_RANDOM_RUN_MIN = 32


def label_value_shaped(text: str) -> bool:
    from . import template_suggest as ts

    if ts._PEM_RE.search(text) or ts._USERINFO_RE.search(text) or _TOKEN_PREFIX_RE.search(text):
        return True
    return any(
        len(m.group(0)) >= LABEL_RANDOM_RUN_MIN and ts._looks_random(m.group(0))
        for m in ts._RUN_RE.finditer(text)
    )


def _withhold_label(label: str, what: str) -> None:
    """A label agents and the access log see must not carry a secret (#1786): refused, never
    rewritten — a stored value, or a credential VALUE shape. Judged against every secret whose
    plaintext is known (`redaction_values_for_labels`): an unreadable file is unknowable and must
    not block a repair or an unrelated write."""
    secrets = template_secrets.redaction_values_for_labels()  # unreadable store: refuse
    if template_secrets.redact_text(label, secrets) != label or label_value_shaped(label):
        raise store.VariableError(f"the {what} looks like a credential; choose another")


def put_file(vid: str, data: bytes, name: object, expected: float) -> dict:
    """Store (or replace) a secret file's content (#1786). The new blob is published BEFORE the
    record that refers to it commits; the old one is retired, then destroyed after the commit.
    The response is the masked row: never the content."""
    file_name = store.file_name(name)
    if not 0 < len(data) <= vault_files.MAX_BYTES:
        raise store.VariableError("a secret file is 1 byte to 256 KiB")

    def fn(records: list[dict]) -> dict:
        rec = _find(records, vid)
        if rec["kind"] != "secret_file":
            raise store.VariableError("only a secret file takes an upload")
        if rec["updated_at"] != expected:
            raise store.VariableConflict(_public(rec))
        # Under the registration fence: no secret can be stored between this check and the commit.
        _withhold_label(file_name, "file name")
        text = vault_files.text(data)
        if text is not None:
            store.refuse_secret_in_labels(records, text, [(file_name, "file name")])
        # Before every file write: settle what an interrupted one left (its revision may be ours).
        store._sweep_locked(store.store_path())
        rid = rec["id"]
        rev = rec["file"]["rev"] + 1 if "file" in rec else 1
        if rev > vault_files.REV_MAX:
            raise store.VariableError("this secret file's revision counter is exhausted")
        vault_files.publish(rid, rev, store.file_aad(rec, rev), data)
        store.on_abort(lambda: store.remove_uncommitted_file(rid, rev))
        store._retire(rec)
        store._drop_file(rec)
        rec.pop("unset", None)
        rec["file"] = {"name": file_name, "size": len(data), "rev": rev}
        rec["updated_at"] = store._bumped(rec["updated_at"])
        return _public(rec, records)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def set_projects(vid: str, project_ids: object, expected: float) -> dict:
    """Bind a global secret to exactly these projects (#1786 P3) by explicit ``ref: global``
    records — the store's own rule for which projects' agents a secret may reach. Adds and
    removes refs only: a project's own value under the name refuses, and a binding a playbook
    deployment still holds is never removed here (it is that deployment's)."""
    from . import projects as project_store
    from . import variable_references

    if not isinstance(project_ids, list) or len(project_ids) > 200:
        raise store.VariableError("project_ids is a list of project ids")
    wanted = {store.project_id(p) for p in project_ids}

    def fn(records: list[dict]) -> dict:
        rec = _find(records, vid)
        if rec["kind"] not in store.SECRET_KINDS:
            raise store.VariableError("only a secret or a secret file is bound to projects here")
        if rec["updated_at"] != expected:
            raise store.VariableConflict(_public(rec, records))
        name = rec["name"]
        current = set(_bound(records, name))
        # Only ADDED ids must name a project that exists now (checked inside the fence). A
        # binding whose project was since deleted stays removable, and keeping it is no change.
        known = project_store.load()
        unknown = sorted(p for p in wanted - current if p not in known)
        if unknown:
            raise store.VariableError(f"unknown projects: {unknown}")
        for pid in sorted(wanted - current):
            own = next(
                (
                    r
                    for r in records
                    if r["scope"] == store.SCOPE_PROJECT
                    and r["project_id"] == pid
                    and r["name"] == name
                ),
                None,
            )
            if own is not None:
                raise store.VariableError(
                    f"project {pid} has its own value for {name}; change it there first"
                )
            if sum(1 for r in records if r["project_id"] == pid) >= store.PROJECT_VARIABLES_MAX:
                raise store.VariableError(
                    f"project {pid} holds too many bindings (max {store.PROJECT_VARIABLES_MAX})"
                )
            now = time.time()
            records.append(
                {
                    "name": name,
                    "kind": rec["kind"],
                    "scope": store.SCOPE_PROJECT,
                    "project_id": pid,
                    "ref": store.REF_GLOBAL,
                    "id": variable_document.new_id(),
                    "definition": {"value_type": "text"},
                    "created_at": now,
                    "updated_at": now,
                }
            )
        removing = current - wanted
        if removing:
            held = sorted(removing & set(variable_references.deployment_projects(name)))
            if held:
                raise store.VariableError(
                    f"a playbook deployment in {', '.join(held)} uses this binding; remove it there"
                )
            records[:] = [
                r
                for r in records
                if not (
                    r["scope"] == store.SCOPE_PROJECT
                    and r["name"] == name
                    and r.get("ref") == store.REF_GLOBAL
                    and r["project_id"] in removing
                )
            ]
        if wanted != current:
            # The record's revision moves, so a second writer holding the old one conflicts
            # instead of silently replacing this list. Copies are keyed on the VALUE's
            # revision (`secrets_broker._marker`), so this bump never destroys one.
            rec["updated_at"] = store._bumped(rec["updated_at"])
        return _public(rec, records)

    return store._mutate_document(lambda records, collections: fn(records))  # type: ignore[return-value]


def access_log(vid: str, limit: int = 50) -> list[dict]:
    """The newest agent requests for one secret (#1786): when, which project, engine and session,
    and the outcome. Never a value or a path."""
    from . import secrets_broker

    records = store._read_strictly_full()
    rec = _find(records, vid)
    if rec["kind"] not in store.SECRET_KINDS:
        raise store.VariableError("only a secret or a secret file has an access log")
    if not secrets_broker.db_path().exists():
        return []
    return secrets_broker.access_log(rec["id"], limit)


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
