"""One scoped value/secret store, with shared definitions and stable IDs (#1090/#1191/#1390).

Store v4 adds opaque IDs, typed definition metadata, a retained legacy identity ledger
and explicit collections to the existing template-variables.json. It keeps the same
AES-GCM key, immutable scope/name encryption identity and legacy/scoped AAD markers.
The first accepted write retains the exact old file, then atomically publishes all v4
metadata and values together. Reads never upgrade; damaged/newer inventories refuse
writes. No consumer file is rewritten or encryption envelope moved during migration.

The legacy public API retains name-based string values and write-only secret status.
_read_strictly and _mutate adapt the v3 value-record shape used by existing deployment
journals and keyed review fingerprints; _mutate_document owns the full v4 transaction.
Old writers preserve IDs/definitions and validate changed values against their types.
The generic shared API lives in variables.py. New builds refuse unknown metadata;
old builds refuse version 4 rather than dropping its identities or collections.

Resolution keeps project → global → default for text; project secrets require their
own value or an explicit global reference. Wrong-kind/unusable bindings never fall
through. Every scope's ciphertext participates in fail-closed transport redaction.
Public responses contain no secret value or envelope, including conflicts.

All writers take session_input.mutation_fence before this store's sidecar lock. Global
create/delete/retype checks templates, live project references and retained deployment
binding snapshots under that fence, using strict inventories without nested store locks.
A new reference cannot race a deletion; a retained legacy reference cannot silently
adopt a newly created same-name value. Display labels and collection names may change;
storage names/kinds remain immutable. Removing a collection never removes its values.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import template_secrets, variable_document, variable_types, vault_files
from . import templates as tstore
from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger(__name__)

STORE_VERSION = 4
# Deployment journals and review fingerprints retain their exact pre-ID record shape.
BINDING_SNAPSHOT_VERSION = 3

VARIABLES_MAX = 100
VALUE_MAX = 2_000
#: Bindings one project may hold (a playbook declares at most 32 variables; room for two).
PROJECT_VARIABLES_MAX = 64
#: Every record across every scope: the file stays small enough to read on every send.
RECORDS_MAX = 4_000

EDITABLE_KEYS = frozenset({"name", "value", "kind"})
KINDS = ("text", "secret", "secret_file")
#: Kinds whose value is never readable through any response (#1090, #1786).
SECRET_KINDS = frozenset({"secret", "secret_file"})
SCOPE_GLOBAL = template_secrets.SCOPE_GLOBAL
SCOPE_PROJECT = template_secrets.SCOPE_PROJECT
SCOPES = (SCOPE_GLOBAL, SCOPE_PROJECT)
#: A project binding's recorded, explicit choice to use the global variable of the same name.
REF_GLOBAL = "global"
#: The ``projects.py`` entity id shape a binding is keyed by: ASCII, no ``:`` (the AAD separator),
#: and never the synthetic ``__default__`` project, which owns nothing.
PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

LIMITS = {
    "variables_max": VARIABLES_MAX,
    "value_max": VALUE_MAX,
    "name_max": 32,
    "secret_min": 8,
}

# The errors are templates' own, so the routes answer both stores with one mapping.
VariableError = tstore.TemplateError
VariableConflict = tstore.TemplateConflict


class VariableNotFound(LookupError):
    """No variable with that name."""


class VariableInUse(Exception):
    """DELETE refused: these templates (``[{id, name}]``) and/or these projects
    (``[project_id]``, each holding ``ref: "global"`` to it) still depend on the variable."""

    def __init__(
        self, name: str, dependants: list[dict], projects: list[str] | None = None
    ) -> None:
        projects = list(projects or [])
        parts = []
        if dependants:
            parts.append(f"{len(dependants)} template(s)")
        if projects:
            parts.append(f"{len(projects)} project(s)")
        super().__init__(f"variable {name!r} is still used by {' and '.join(parts)}")
        self.name = name
        self.dependants = dependants
        self.projects = projects


#: Raised by ``delete_variable`` when the template library cannot be read in full.
VariableRefsUnknown = tstore.TemplateInventoryIncomplete


class VariableStoreUnsupported(RuntimeError):
    """The file on disk is a newer store version than this build writes."""

    def __init__(self, version: object) -> None:
        super().__init__(
            f"template-variables.json is store version {version!r}, newer than this build "
            f"(version {STORE_VERSION}) — upgrade BattleLab; nothing was written"
        )
        self.version = version


class ResolutionUnavailable(RuntimeError):
    """The store could not be read COMPLETELY, so no value may be resolved from it: resolving
    against the records that happened to parse could silently swap in a fallback."""


class BindingUnusable(Exception):
    """A project binding exists but cannot be used — refuse (409), never fall back (#1096 §3)."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason


class BindingMissing(LookupError):
    """Nothing resolves this name for this project (for a secret: no project binding at all —
    a secret never falls back to the global scope without a recorded ``ref``)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def store_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_TEMPLATE_VARS",
            str(Path.home() / ".config" / "agent-sessions" / "template-variables.json"),
        )
    )


# ---- validation -----------------------------------------------------------------------------


def _name(raw: object) -> str:
    if not isinstance(raw, str) or not tstore.FIELD_NAME_RE.fullmatch(raw):
        raise VariableError(
            "a variable name is a lowercase letter followed by letters, digits or '_' "
            "(max 32 characters)"
        )
    return raw


def project_id(raw: object) -> str:
    """A valid project entity id, or ``VariableError``. Checked before anything is stored."""
    if not isinstance(raw, str) or not raw.isascii() or not PROJECT_ID_RE.fullmatch(raw):
        raise VariableError("not a project id")
    return raw


def _value(payload: dict) -> str:
    # Same control-character rule as a template body (ESC inside a bracketed paste, #618).
    return tstore._text(payload, "value", max_len=VALUE_MAX, required=True, multiline=True)


def _kind(raw: object) -> str:
    kind = "text" if raw is None else raw
    if kind not in KINDS:
        raise VariableError("kind must be 'text', 'secret' or 'secret_file'")
    return kind  # type: ignore[return-value]


def _secret_value(payload: dict) -> str:
    value = _value(payload)
    if value != value.strip():
        # A message is trimmed when it is assembled, so a secret at its edge would be sent
        # WITHOUT the whitespace — a form the redaction set would not recognize (Hermes on #1105).
        raise VariableError("a secret cannot start or end with whitespace")
    if len(value) < template_secrets.SECRET_MIN:
        raise VariableError(
            f"a secret is at least {template_secrets.SECRET_MIN} characters — a shorter one "
            "would be redacted out of ordinary text"
        )
    return value


def validate(payload: object) -> dict:
    """A new variable's fields, or ``VariableError``. Unknown keys are refused, not ignored.

    Returns the PLAINTEXT value for either kind; ``create_variable`` encrypts a secret's before
    anything is stored."""
    if not isinstance(payload, dict):
        raise VariableError("expected a JSON object")
    unknown = sorted(set(payload) - EDITABLE_KEYS)
    if unknown:
        raise VariableError(f"unknown fields: {unknown}")
    kind = _kind(payload.get("kind"))
    if kind == "secret_file":
        raise VariableError("a secret file is created in the shared library and uploaded there")
    value = _secret_value(payload) if kind == "secret" else _value(payload)
    return {"name": _name(payload.get("name")), "kind": kind, "value": value}


_BASE_KEYS = frozenset({"name", "kind", "created_at", "updated_at"})
_SCOPE_KEYS = frozenset({"scope", "project_id"})


def _coerce_scope(raw: dict, version: int) -> tuple[str, str | None]:
    if version < 3:
        # A v1/v2 file predates scopes: every record in it is global, by the FILE's version.
        return SCOPE_GLOBAL, None
    scope = raw.get("scope")
    pid = raw.get("project_id")
    if scope == SCOPE_GLOBAL and pid is None:
        return SCOPE_GLOBAL, None
    if scope == SCOPE_PROJECT:
        return SCOPE_PROJECT, project_id(pid)
    raise VariableError("a record's scope is 'global' (no project) or 'project' (with its id)")


def _coerce_record(raw: object, version: int = STORE_VERSION) -> dict:
    """A stored record, re-validated on read. v1 records carry no ``kind`` and are text; v1/v2
    records carry no scope and are global, and a v2 envelope is a ``legacy`` (bare-name AAD) one."""
    if not isinstance(raw, dict):
        raise VariableError("record is not an object")
    kind = _kind(raw.get("kind"))
    name = _name(raw.get("name"))
    scope, pid = _coerce_scope(raw, version)
    base = _BASE_KEYS | (_SCOPE_KEYS if version >= 3 else frozenset())
    if version >= 4:
        base |= {"id", "definition"}
    if kind == "secret_file" and version < 4:
        raise VariableError("a secret file exists only in a version-4 store")
    body: dict = {"name": name, "kind": kind, "scope": scope, "project_id": pid}
    # Agent access (#1786) belongs to the record that HOLDS a secret's value: a project's `ref`
    # follows the global record's flag and carries none of its own. Stored only when on.
    if "agent_access" in raw:
        if raw["agent_access"] is not True or kind not in SECRET_KINDS or "ref" in raw:
            raise VariableError("agent_access is true on a secret's own record, or absent")
        if version < 4:
            raise VariableError("agent_access exists only in a version-4 store")
        base = base | {"agent_access"}
        body["agent_access"] = True
    if version >= 4 and raw.get("unset") is True:
        if scope != SCOPE_GLOBAL or set(raw) - base - {"unset"}:
            raise VariableError("only a global definition may have no stored value")
        body["unset"] = True
    elif "ref" in raw:
        if version < 3 or scope != SCOPE_PROJECT or raw["ref"] != REF_GLOBAL:
            raise VariableError("only a project binding may refer to the global variable")
        unknown = sorted(set(raw) - base - {"ref"})
        if unknown:
            raise VariableError(f"unknown record keys: {unknown}")
        body["ref"] = REF_GLOBAL
    elif kind == "secret_file":
        unknown = sorted(set(raw) - base - {"file"})
        if unknown:
            raise VariableError(f"unknown record keys: {unknown}")
        body["file"] = _file_meta(raw.get("file"))
    elif kind == "secret":
        unknown = sorted(set(raw) - base - {"secret"})
        env = raw.get("secret")
        if version < 3:
            ok = template_secrets.valid_envelope(env)
            env = {**env, "aad": template_secrets.AAD_LEGACY} if ok else env  # type: ignore[dict-item]
        else:
            ok = template_secrets.valid_store_envelope(env)
        if unknown or not ok:
            raise VariableError("a secret record holds a name, an envelope and timestamps only")
        if env["aad"] == template_secrets.AAD_LEGACY and scope != SCOPE_GLOBAL:  # type: ignore[index]
            raise VariableError("a legacy envelope is only ever a global one")
        body["secret"] = dict(env)  # type: ignore[arg-type]
    else:
        unknown = sorted(set(raw) - base - {"value"})
        if unknown:
            raise VariableError(f"unknown record keys: {unknown}")
        body["value"] = _value(raw)
    if version >= 4:
        try:
            body["id"] = variable_document.validate_id(raw.get("id"))
            body["definition"] = variable_types.metadata(name, kind, raw.get("definition"))
            if kind == "text" and "value" in body:
                variable_types.stored_value(
                    name, body["definition"], body["value"], max_len=VALUE_MAX
                )
        except (variable_document.IdentityError, variable_types.DefinitionError) as exc:
            raise VariableError(str(exc)) from None
    return {
        **body,
        "created_at": tstore._number(raw.get("created_at"), "created_at"),
        "updated_at": tstore._number(raw.get("updated_at"), "updated_at"),
    }


def file_name(raw: object) -> str:
    """A secret file's display name: printable, no path separator, at most ``FILE_NAME_MAX``.
    It is metadata only — never a path component anywhere."""
    if not isinstance(raw, str):
        raise VariableError("a file name is a string")
    name = raw.strip()
    if (
        not name
        or len(name) > vault_files.FILE_NAME_MAX
        or not name.isprintable()
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise VariableError("a file name is 1–255 printable characters with no path separator")
    return name


def _file_meta(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"name", "size", "rev"}:
        raise VariableError("a secret file record holds its file name, size and revision")
    size, rev = raw["size"], raw["rev"]
    if type(size) is not int or not 0 < size <= vault_files.MAX_BYTES:
        raise VariableError("a secret file's size is 1 byte to 256 KiB")
    if type(rev) is not int or not 0 < rev <= vault_files.REV_MAX:
        raise VariableError("a secret file's revision is a positive integer")
    return {"name": file_name(raw["name"]), "size": size, "rev": rev}


def file_aad(rec: dict, rev: int | None = None) -> str:
    return vault_files.aad(
        rec["scope"], rec["project_id"], rec["name"], rec["id"], rev or rec["file"]["rev"]
    )


def file_bytes(rec: dict, *, checked: bool = False) -> bytes | None:
    """A secret file record's plaintext, or ``None`` (missing, tampered, no key). SERVER-ONLY."""
    return vault_files.read(rec["id"], rec["file"]["rev"], file_aad(rec), checked=checked)


def _key(rec: dict) -> tuple[str, str, str]:
    return (rec["scope"], rec["project_id"] or "", rec["name"])


def _is_global(rec: dict) -> bool:
    return rec["scope"] == SCOPE_GLOBAL


def _decrypt(rec: dict) -> str | None:
    return template_secrets.decrypt_record(
        rec["scope"], rec["project_id"], rec["name"], rec["secret"]
    )


def public(rec: dict, used_by: list[dict] | None = None) -> dict:
    """What a response may carry for a GLOBAL ``rec``: a text variable's value; for a secret, only
    whether one is stored and whether it still decrypts. The envelope never leaves the server."""
    out = {
        "name": rec["name"],
        "kind": rec["kind"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }
    if rec["kind"] == "secret":
        out["set"] = not rec.get("unset", False)
        out["needs_reentry"] = not rec.get("unset") and _decrypt(rec) is None
    elif rec["kind"] == "secret_file":
        out["set"] = not rec.get("unset", False)
        out["needs_reentry"] = not rec.get("unset") and file_bytes(rec) is None
    else:
        # Legacy clients keep their string-shaped response; resolution excludes unset
        # definitions, so an empty display value can never turn into a stored binding.
        out["value"] = rec.get("value", "")
    if used_by is not None:
        out["used_by"] = used_by
    return out


def public_binding(rec: dict) -> dict:
    """A PROJECT binding as a response may carry it: ``source`` says whose value it is. A secret is
    ``{set, needs_reentry}`` only; a ``ref`` binding carries no value of its own at all."""
    out: dict = {
        "name": rec["name"],
        "kind": rec["kind"],
        "project_id": rec["project_id"],
        "source": "global" if rec.get("ref") == REF_GLOBAL else "project",
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }
    if "ref" in rec:
        return out
    if rec["kind"] == "secret":
        out["set"] = True
        out["needs_reentry"] = _decrypt(rec) is None
    elif rec["kind"] == "secret_file":
        out["set"] = True
        out["needs_reentry"] = file_bytes(rec) is None
    else:
        out["value"] = rec["value"]
    return out


# ---- store I/O -------------------------------------------------------------------------------


def _read(path: Path, *, document: dict | None = None) -> tuple[list[dict], bool]:
    """The trustworthy records plus whether the file needs quarantining before the next write."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], False
    except (OSError, ValueError, RecursionError) as e:
        log.warning(
            "template vars: %s is unreadable (%s); serving an empty library", path, type(e).__name__
        )
        return [], True
    if not isinstance(raw, dict):
        return [], True
    if document is not None:
        document.update(raw)
    # Version BEFORE shape, for the reason templates._read gives.
    version = raw.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > STORE_VERSION:
        raise VariableStoreUnsupported(version)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return [], True
    if not isinstance(raw.get("variables"), list):
        return [], True
    records: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    damaged = False
    for item in raw["variables"]:
        try:
            rec = _coerce_record(item, version)
        except VariableError as e:
            log.warning("template vars: skipping an unreadable record in %s: %s", path, e)
            damaged = True
            continue
        if _key(rec) in seen:
            damaged = True
            continue
        seen.add(_key(rec))
        records.append(rec)
    globals_by_name = {r["name"]: r for r in records if _is_global(r)}
    for rec in records:
        if rec.get("ref") == REF_GLOBAL:
            target = globals_by_name.get(rec["name"])
            if target is None or target["kind"] != rec["kind"]:
                # Keep the reference in the deletion inventory, but never resolve a store
                # with a dangling or incompatible binding (including via a fallback).
                damaged = True
    if version >= 4:
        try:
            variable_document.validate(raw, records)
        except variable_document.IdentityError:
            damaged = True
    return records, damaged


def legacy_record(rec: dict) -> dict:
    """The v3 value snapshot used by deployed journals and legacy name-based consumers."""
    return {key: value for key, value in rec.items() if key not in {"id", "definition"}}


def _mutate(
    fn: Callable[[list[dict]], dict | list | None], *, repair_references: bool = False
) -> dict | list | None:
    """Adapt existing v3 record writers without migrating their journals or fingerprints.

    The complete v4 document is still validated under the one authority/store fence.
    Existing identities/definitions survive each legacy write; a new identity gets a new
    ID. Validation against retained definitions prevents an old client bypassing types.
    """

    def adapt(records: list[dict], collections: list[dict]) -> dict | list | None:
        previous = {_key(rec): rec for rec in records}
        compatible = [legacy_record(rec) for rec in records]
        result = fn(compatible)
        merged = []
        for rec in compatible:
            old = previous.get(_key(rec))
            if old is not None and old["kind"] == rec["kind"]:
                metadata = {"id": old["id"], "definition": old["definition"]}
            else:
                metadata = {"id": variable_document.new_id(), "definition": {"value_type": "text"}}
            upgraded = {**rec, **metadata}
            # Plaintext was already validated at legacy entry. Recheck new encrypted
            # values against the retained secret definition before publishing as well.
            if (
                rec["kind"] == "secret"
                and "secret" in rec
                and (old is None or rec["secret"] != old.get("secret"))
            ):
                value = _decrypt(upgraded)
                if value is None:
                    raise VariableError(
                        "the replacement secret could not be read; nothing was written"
                    )
                try:
                    variable_types.typed_value(
                        rec["name"], metadata["definition"], value, max_len=VALUE_MAX
                    )
                except variable_types.DefinitionError as exc:
                    raise VariableError(str(exc)) from None
            merged.append(upgraded)
        records[:] = merged
        return result

    return _mutate_document(adapt, repair_references=repair_references)


def _repairable_references(original: dict, records: list[dict]) -> bool:
    """All records/identities parsed; only dangling project targets may be repaired."""
    version = original.get("version")
    if type(version) is not int or version not in (1, 2, 3, 4):
        return False
    raw = original.get("variables")
    if not isinstance(raw, list) or len(raw) != len(records):
        return False
    if version >= 4:
        try:
            variable_document.validate(original, records)
        except variable_document.IdentityError:
            return False
    elif set(original) != {"version", "variables"}:
        return False
    globals_ = {r["name"]: r for r in records if _is_global(r)}
    return any(
        r.get("ref") == REF_GLOBAL
        and (r["name"] not in globals_ or globals_[r["name"]]["kind"] != r["kind"])
        for r in records
    )


_hooks = threading.local()


def after_commit(action: Callable[[], None]) -> None:
    """Run ``action`` once THIS transaction's document is published, still under its locks (a
    replaced or deleted secret file's old blob is destroyed only then, #1786). Failures are
    logged: the record is already committed, and the sweep settles what is left."""
    _hooks.current[0].append(action)


def on_abort(action: Callable[[], None]) -> None:
    """Run ``action`` if THIS transaction does not publish (a blob published for it is removed)."""
    _hooks.current[1].append(action)


def _run_hooks(actions: list[Callable[[], None]], what: str) -> None:
    for action in actions:
        try:
            action()
        except Exception as e:  # noqa: BLE001 — the sweep settles anything left behind
            log.warning("template vars: a secret-file %s step failed (%s)", what, type(e).__name__)


def _mutate_document(
    fn: Callable[[list[dict], list[dict]], dict | list | None],
    *,
    repair_references: bool = False,
) -> dict | list | None:
    """One v4 transaction under authority → store locks, with a retained upgrade backup.

    Refused callbacks never publish. Corrupt metadata refuses rather than discarding any
    unreadable record; legacy journal adaptation happens in _mutate, above this boundary.
    """
    path = store_path()
    # Under the write seam's cross-process fence: a template send's fingerprint re-reads these
    # revisions inside it right before byte one (#1090, Hermes on #1105).
    from . import session_input

    # The registration fence too (#1443): a secret stored here becomes part of the redaction set at
    # one instant relative to every fenced redacting writer, never in the middle of one.
    with (
        session_input.mutation_fence(),
        template_secrets.registration_fence(),
        json_write_lock(path),
    ):
        hooks: tuple[list, list] = ([], [])
        outer = getattr(_hooks, "current", None)
        _hooks.current = hooks
        try:
            result = _transaction(fn, path, repair_references)
        except BaseException:
            _run_hooks(hooks[1], "rollback")
            raise
        finally:
            _hooks.current = outer
        _run_hooks(hooks[0], "cleanup")
    # Agents' copies (#1786) are re-judged against what was just committed — AFTER this write's
    # fences are released, so the broker's fence never extends the authorization fence terminal
    # input waits on (independent review of #1796). Policy only: no process is probed here.
    from . import secrets_broker

    secrets_broker.after_store_commit()
    return result


def _transaction(
    fn: Callable[[list[dict], list[dict]], dict | list | None],
    path: Path,
    repair_references: bool,
) -> dict | list | None:
    original: dict = {}
    records, damaged = _read(path, document=original)
    if damaged and not (repair_references and _repairable_references(original, records)):
        raise VariableError(
            "the variables store could not be read in full; repair it before writing "
            "or upgrading — nothing was written"
        )
    try:
        ledger = variable_document.prepare(records, original)
    except variable_document.IdentityError as exc:
        raise VariableError(str(exc)) from None
    collections = original.get("collections", [])
    before = {_key(r): _labels_and_value(r) for r in records}
    result = fn(records, collections)
    if len(records) > RECORDS_MAX:
        raise VariableError(f"the variables store is full (max {RECORDS_MAX} records)")
    records[:] = [_coerce_record(rec, STORE_VERSION) for rec in records]
    _guard_changed_labels(records, before)
    globals_ = {r["name"]: r for r in records if _is_global(r)}
    if any(
        r.get("ref") == REF_GLOBAL
        and (r["name"] not in globals_ or globals_[r["name"]]["kind"] != r["kind"])
        for r in records
    ):
        raise VariableError("a project reference is still unresolved; nothing was written")
    records.sort(key=_key)
    try:
        document = variable_document.publish_document(records, ledger, STORE_VERSION, collections)
        if path.exists() and original.get("version", 0) < 4:
            variable_document.retain_upgrade_backup(path)
    except variable_document.IdentityError as exc:
        raise VariableError(str(exc)) from None
    # Shared project memory (#1441): every agent-readable view is re-checked against the new
    # secret inventory BEFORE this fence is released. The scopes are enumerated BEFORE the
    # write, so a failure to enumerate them refuses the write instead of letting a stored
    # secret settle beside views nobody checked.
    from . import memory

    with memory.secrets_changing():
        atomic_write_json(path, document, mode=0o600)
    return result


def _sweep_locked(path: Path) -> int:
    """Settle the secret-file directory against the store as committed (``vault_files.sweep``).
    The caller holds the store lock, so no publish is in flight."""
    # Only a store that reads COMPLETELY — every record valid, the identity ledger consistent —
    # decides what is an orphan (Hermes 7406, 2). A damaged record could otherwise hide the very
    # blob it still refers to, and the sweep would destroy it; on any doubt nothing is moved.
    document: dict = {}
    try:
        records, damaged = _read(path, document=document)
    except VariableStoreUnsupported:
        return 0
    if damaged or (path.exists() and not isinstance(document.get("identities"), list)):
        log.warning("secret files: the variables store is not readable in full; nothing swept")
        return 0
    current = {
        (r["id"], r["file"]["rev"]) for r in records if r["kind"] == "secret_file" and "file" in r
    }
    ledger = [e for e in document.get("identities", []) if isinstance(e, dict)]
    return vault_files.sweep(ledger, current)


def sweep_secret_files() -> int:
    """App start: settle orphans an interrupted write left (#1786). Same lock order as a write's
    inner part: the registration fence, then the store lock."""
    path = store_path()
    with template_secrets.registration_fence(), json_write_lock(path):
        return _sweep_locked(path)


def request_sweep() -> None:
    """A transaction that touched a secret file settles the directory once it has committed."""
    path = store_path()
    after_commit(lambda: _sweep_locked(path))


def _read_leniently() -> list[dict]:
    try:
        records, _ = _read(store_path())
    except VariableStoreUnsupported as e:
        log.warning("template vars: %s; serving an empty library", e)
        return []
    return records


def _globals(*, for_resolution: bool = False) -> list[dict]:
    records = _read_strictly() if for_resolution else _read_leniently()
    return [r for r in records if _is_global(r)]


def _read_strictly() -> list[dict]:
    """Every record, or ``ResolutionUnavailable``. A missing file is a complete answer (nothing is
    stored); anything the lenient read would degrade — unreadable, unparsable, a newer version, a
    damaged record — raises instead."""
    try:
        records, damaged = _read(store_path())
    except VariableStoreUnsupported as e:
        raise ResolutionUnavailable(str(e)) from None
    if damaged:
        raise ResolutionUnavailable("the variables store could not be read in full")
    return [legacy_record(rec) for rec in records]


def _find(
    records: list[dict], name: str, scope: str = SCOPE_GLOBAL, pid: str | None = None
) -> dict:
    for rec in records:
        if rec["name"] == name and rec["scope"] == scope and rec["project_id"] == pid:
            return rec
    raise VariableNotFound(name)


def _bumped(previous: float) -> float:
    try:
        return tstore._bumped(previous)
    except tstore.TemplateError:
        raise VariableError(
            "this variable's revision counter is exhausted; delete and recreate it"
        ) from None


# ---- public API: the global library ----------------------------------------------------------


def list_variables() -> list[dict]:
    """Every readable GLOBAL variable, by name, with the templates that use it (``used_by``)."""
    usage = tstore.library_usage()  # one read of the template store, not one per variable
    # A secret file has no place in the name-based template library (#1786): it is never a field.
    return [
        public(rec, usage.get(rec["name"], []))
        for rec in sorted(_globals(), key=_key)
        if rec["kind"] != "secret_file"
    ]


def values() -> dict[str, str]:
    """``{name: value}`` of the global TEXT variables — what a ``library`` text field resolves
    against. A secret's name is deliberately absent: a text field naming it is a missing
    variable. Execution readers refuse any incomplete inventory, including its identity ledger."""
    return {
        r["name"]: r["value"]
        for r in _globals(for_resolution=True)
        if r["kind"] == "text" and not r.get("unset")
    }


def secret_state() -> dict[str, str]:
    """``{name: "ok" | "reentry"}`` for every global SECRET variable — what a library secret field
    resolves against, without its value."""
    return {
        r["name"]: "ok" if _decrypt(r) is not None else "reentry"
        for r in _globals(for_resolution=True)
        if r["kind"] == "secret" and not r.get("unset")
    }


def _raw_identity(item: object, version: int) -> tuple[str, str | None, str] | None:
    """The identity a damaged record's envelope was bound to, if its identity fields still parse."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not tstore.FIELD_NAME_RE.fullmatch(name):
        return None
    try:
        scope, pid = _coerce_scope(item, version)
    except VariableError:
        return None
    return scope, pid, name


def secret_values_checked(*, lenient_files: bool = False) -> list[str]:
    """See `_secret_values_checked`. A blob destroyed by a write that committed meanwhile (its
    record already names the next revision) is read again once, never reported as damage."""
    path = store_path()

    def stamp():
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    before = stamp()
    try:
        return _secret_values_checked(lenient_files=lenient_files)
    except _FileUnreadable:
        if stamp() == before:
            raise VariableError("a secret file in the variables store is unreadable") from None
    try:
        return _secret_values_checked(lenient_files=lenient_files)
    except _FileUnreadable:
        raise VariableError("a secret file in the variables store is unreadable") from None


class _FileUnreadable(Exception):
    pass


def _secret_values_checked(*, lenient_files: bool = False) -> list[str]:
    """Every stored secret's plaintext, for REDACTION — EVERY scope, global and project, and EVERY
    record (two records claiming one identity are both included). Fail-closed where the lenient
    read would fail open.

    A missing file is a complete answer (no secrets). An unreadable file, one that does not parse,
    or a newer store version RAISES — "I could not read the store" must never become "there is
    nothing to redact" (Hermes on #1105). A single record that does not validate is skipped: its
    plaintext is unknowable whichever way this answers.
    """
    path = store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("variables"), list):
        raise VariableError("the variables store is not readable as a store")
    version = raw.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > STORE_VERSION:
        raise VariableStoreUnsupported(version)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise VariableError("the variables store is not readable as a store")
    if version >= 4 and set(raw) != variable_document.DOCUMENT_KEYS:
        raise VariableError("the variables store has unknown or missing metadata")
    # A LIST, not a map keyed by identity: two records that claim the same identity (a damaged or
    # hand-edited file) both carry a plaintext, and keying by identity let the later one hide the
    # earlier from redaction (Hermes 5512). Use is refused for duplicates elsewhere; redaction
    # covers every value.
    out: list[str] = []
    for item in raw["variables"]:
        try:
            rec = _coerce_record(item, version)
        except VariableError:
            # A record that fails validation may still carry a decryptable secret (damaged
            # METADATA — a bad timestamp — does not touch the authenticated envelope). Recover it
            # from the envelope alone; if it looks like a secret and cannot be recovered, the
            # inventory is incomplete and redaction must refuse (Hermes on #1105).
            ident = _raw_identity(item, version)
            env = item.get("secret") if isinstance(item, dict) else None
            if isinstance(env, dict) and version < 3 and template_secrets.valid_envelope(env):
                env = {**env, "aad": template_secrets.AAD_LEGACY}
            if ident is not None and template_secrets.valid_store_envelope(env):
                v = template_secrets.decrypt_record_checked(*ident, env)  # type: ignore[arg-type]
                if v is not None:
                    out.append(v)
                continue
            if isinstance(item, dict) and (
                item.get("kind") in SECRET_KINDS or "secret" in item or "file" in item
            ):
                raise VariableError(
                    "a secret record in the variables store is unreadable"
                ) from None
            continue
        if rec["kind"] == "secret_file" and "file" in rec:
            data = file_bytes(rec, checked=True)
            if data is None:
                if lenient_files:
                    continue  # a label check: the unknowable must not block a repair (review)
                # Missing, tampered, or its key lost/replaced: the transport refuses until the
                # operator re-uploads or deletes it (#1786 P1 "fail closed on key loss"; Hermes
                # 7416). Label checks stay lenient, so that repair is always possible.
                raise _FileUnreadable
            value = vault_files.text(data)
            if value is not None:
                out.append(value)  # binary: no form redaction could match (stated residual)
            continue
        if rec["kind"] == "secret" and "secret" in rec:
            v = template_secrets.decrypt_record_checked(
                rec["scope"], rec["project_id"], rec["name"], rec["secret"]
            )
            if v is not None:
                out.append(v)
    return out


def revisions() -> dict[str, float]:
    """``{name: updated_at}`` of the GLOBAL variables — what a send binds its library values to
    (Hermes on #1105)."""
    return {r["name"]: r["updated_at"] for r in _globals(for_resolution=True)}


def secret_values() -> dict[str, str]:
    """``{name: plaintext}`` for every GLOBAL secret that decrypts. SERVER-ONLY: the send renders
    with it. Nothing that answers the browser may call it."""
    out: dict[str, str] = {}
    for r in _globals(for_resolution=True):
        if r["kind"] == "secret" and not r.get("unset"):
            v = _decrypt(r)
            if v is not None:
                out[r["name"]] = v
    return out


def _stored(fields: dict, scope: str, pid: str | None, created: float, updated: float) -> dict:
    """The record to store for validated ``fields``: a secret's plaintext becomes its envelope,
    bound to the scope-qualified identity."""
    body: dict = {"name": fields["name"], "kind": fields["kind"], "scope": scope, "project_id": pid}
    if fields["kind"] == "secret":
        body["secret"] = template_secrets.encrypt_scoped(
            scope, pid, fields["name"], fields["value"]
        )
    else:
        body["value"] = fields["value"]
    return {**body, "created_at": created, "updated_at": updated}


def _retire(rec: dict) -> None:
    """A stored secret is about to be replaced or removed: keep redacting it (it is still in the
    transcripts it was pasted into)."""
    if rec["kind"] == "secret" and "secret" in rec:
        old = _decrypt(rec)
        if old is not None:
            template_secrets.retire([old])
    elif rec["kind"] == "secret_file" and "file" in rec:
        data = file_bytes(rec)
        if data is not None:
            vault_files.retire(data)


def file_committed(vid: str, rev: int) -> bool:
    """True when the store as it is NOW on disk refers to blob ``(vid, rev)``. A transaction can
    fail AFTER its document replaced the old one (a failed directory sync): the record is then
    visible, and its blob must not be removed (Hermes 7394, 1). Unreadable reads as referenced."""
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    rows = raw.get("variables") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return True
    return any(
        isinstance(r, dict)
        and r.get("id") == vid
        and isinstance(r.get("file"), dict)
        and r["file"].get("rev") == rev
        for r in rows
    )


def remove_uncommitted_file(vid: str, rev: int) -> None:
    """An aborted transaction's blob is removed — unless the document did become visible."""
    if not file_committed(vid, rev):
        vault_files.remove(vid, rev)


def _labels_and_value(rec: dict) -> tuple:
    """What the backstop below compares: a record's agent-visible labels, and its envelope."""
    definition = rec.get("definition") or {}
    file = rec.get("file") if isinstance(rec.get("file"), dict) else {}
    return (
        (rec["name"], definition.get("label"), definition.get("help"), file.get("name")),
        repr(rec.get("secret")),
    )


def _guard_changed_labels(records: list[dict], before: dict) -> None:
    """The backstop for EVERY writer of the store — the library, the legacy API, project bindings,
    a playbook's apply and restore (Hermes 7402): inside the one transaction, a record whose labels
    changed has them checked, and a secret whose value changed is refused if a label already shows
    it. Unchanged records are not re-judged, so an old label never blocks an unrelated write."""
    from . import variables

    for rec in records:
        old = before.get(_key(rec))
        now = _labels_and_value(rec)
        if old is not None and old == now:
            continue
        labels, envelope = now
        whats = ("name", "label", "description", "file name")
        for i, (text, what) in enumerate(zip(labels, whats, strict=True)):
            # Only a field that CHANGED to a non-empty text is judged: removing or keeping a label
            # can expose nothing, so it never blocks a write (independent review of #1788).
            if text and (old is None or old[0][i] != text):
                variables._withhold_label(str(text), what)
        if rec["kind"] == "secret" and "secret" in rec and (old is None or old[1] != envelope):
            value = _decrypt(rec)
            if value is not None:
                refuse_secret_in_labels(records, value)


def _exposed_labels(records: list[dict]) -> list[tuple[str, str]]:
    """Every label agents and the access log are shown (#1786): name, label, help, file name."""
    out: list[tuple[str, str]] = []
    for r in records:
        out.append((r["name"], "name"))
        definition = r.get("definition") or {}
        for key, what in (("label", "label"), ("help", "description")):
            if definition.get(key):
                out.append((str(definition[key]), what))
        if r.get("kind") == "secret_file" and isinstance(r.get("file"), dict):
            out.append((str(r["file"]["name"]), "file name"))
    return out


def refuse_secret_in_labels(
    records: list[dict], value: str, extra: list[tuple[str, str]] | None = None
) -> None:
    """Refuse a secret write whose value — or any line or run of it that redaction would match —
    already shows in a label (Hermes 7398, 2): the label was checked when it was saved, so this
    is the other order. Called inside the store's transaction; the message names the field only."""
    needles = [
        v
        for v in {value, *template_secrets.fragments(value)}
        if len(v) >= template_secrets.SECRET_MIN
    ]
    if not needles:
        return
    # The caller's records may be the legacy shape (no definitions, `_mutate`): the committed
    # store's labels are read too — under the store lock, so they are the current ones.
    try:
        committed, _ = _read(store_path())
    except VariableStoreUnsupported:
        committed = []
    for label, what in [*_exposed_labels(records), *_exposed_labels(committed), *(extra or [])]:
        if template_secrets.redact_text(label, needles) != label:
            raise VariableError(
                f"this secret appears in a variable's {what}; change that {what} first — "
                "nothing was saved"
            )


def _drop_file(rec: dict) -> None:
    """``rec``'s blob is destroyed once the transaction that stops referring to it commits."""
    if rec["kind"] == "secret_file" and "file" in rec:
        vid, rev = rec["id"], rec["file"]["rev"]
        after_commit(lambda: vault_files.remove(vid, rev))
        request_sweep()


def create_variable(payload: object) -> dict:
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        from . import variables  # the shared library's label check, under the same fences

        variables._withhold_label(fields["name"], "name")
        if fields["kind"] == "secret":
            refuse_secret_in_labels(records, fields["value"], [(fields["name"], "name")])
        # One list, one lock: a name is unique across BOTH kinds (per scope) by construction.
        globals_ = [r for r in records if _is_global(r)]
        if any(r["name"] == fields["name"] for r in globals_):
            raise VariableError(f"a variable named {fields['name']} already exists")
        projects = _project_dependants(records, fields["name"])
        dependants = tstore.library_references_checked(fields["name"])
        if projects or dependants:
            raise VariableInUse(fields["name"], dependants, projects)
        if len(globals_) >= VARIABLES_MAX:
            raise VariableError(f"too many variables (max {VARIABLES_MAX})")
        now = time.time()
        rec = _stored(fields, SCOPE_GLOBAL, None, now, now)
        records.append(rec)
        return public(rec)

    return _mutate(fn)  # type: ignore[return-value]


def update_variable(name: str, payload: object, expected_updated_at: float) -> dict:
    """Replace a GLOBAL variable's value. The name and the kind are the identity and never change
    here (no rename, no conversion — delete and recreate). A secret's value is write-only: replaced,
    never read, and always re-encrypted under its scoped AAD (a ``legacy`` envelope ends here)."""
    if not isinstance(payload, dict):
        raise VariableError("expected a JSON object")
    unknown = sorted(set(payload) - {"value"})
    if unknown:
        raise VariableError(f"unknown fields: {unknown} (a variable cannot be renamed)")

    def fn(records: list[dict]) -> dict:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(public(rec))
        if rec["kind"] == "secret_file":
            raise VariableError("a secret file is replaced by uploading it in the shared library")
        if rec["kind"] == "secret":
            value = _secret_value(payload)
            refuse_secret_in_labels(records, value)
            _retire(rec)
            rec["secret"] = template_secrets.encrypt_scoped(SCOPE_GLOBAL, None, name, value)
        else:
            rec["value"] = _value(payload)
        rec.pop("unset", None)
        rec["updated_at"] = _bumped(rec["updated_at"])
        return public(rec)

    return _mutate(fn)  # type: ignore[return-value]


def _project_dependants(records: list[dict], name: str) -> list[str]:
    from . import variable_references

    active = {
        r["project_id"]
        for r in records
        if r["scope"] == SCOPE_PROJECT and r["name"] == name and r.get("ref") == REF_GLOBAL
    }
    return sorted(active | set(variable_references.deployment_projects(name)))


def _delete_record(records: list[dict], collections: list[dict], rec: dict) -> None:
    name = rec["name"]
    projects = _project_dependants(records, name)
    dependants = tstore.library_references_checked(name)
    if dependants or projects:
        raise VariableInUse(name, dependants, projects)
    _retire(rec)
    _drop_file(rec)
    records.remove(rec)
    for collection in collections:
        if rec["id"] in collection["variable_ids"]:
            collection["variable_ids"].remove(rec["id"])
            collection["updated_at"] = _bumped(collection["updated_at"])


def delete_variable(name: str, expected_updated_at: float) -> None:
    """Delete a global value only after checking its complete consumer inventory."""

    def fn(records: list[dict], collections: list[dict]) -> None:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(public(rec))
        _delete_record(records, collections, rec)

    _mutate_document(fn)


# ---- public API: project bindings (#1191) ------------------------------------------------------


def _binding(raw: object, pid: str) -> dict:
    """One requested binding, validated: ``{name, kind, value}`` or
    ``{name, kind, ref: "global"}``."""
    if not isinstance(raw, dict):
        raise VariableError("a binding is an object")
    unknown = sorted(set(raw) - {"name", "kind", "value", "ref"})
    if unknown:
        raise VariableError(f"unknown binding fields: {unknown}")
    name = _name(raw.get("name"))
    kind = _kind(raw.get("kind"))
    if ("value" in raw) == ("ref" in raw):
        raise VariableError(f"{name}: a binding carries either its own value or ref 'global'")
    if "ref" in raw:
        if raw["ref"] != REF_GLOBAL:
            raise VariableError(f"{name}: ref must be 'global'")
        return {"name": name, "kind": kind, "ref": REF_GLOBAL}
    if kind == "secret_file":
        raise VariableError(f"{name}: a secret file is bound by ref 'global' only")
    value = _secret_value(raw) if kind == "secret" else _value(raw)
    return {"name": name, "kind": kind, "value": value}


def bind_project(project: object, bindings: object) -> list[dict]:
    """Set some of a project's bindings, all or nothing, under the binding/deletion fence.

    Each binding replaces the project's record of that name (whatever its kind). A ``ref: "global"``
    binding is refused unless the global variable of that name AND kind exists in the same locked
    read — so a concurrent ``delete_variable`` either sees this ref (and refuses) or has already
    removed the global (and this bind refuses). Returns the project's bindings afterwards."""
    pid = project_id(project)
    if not isinstance(bindings, list) or not bindings:
        raise VariableError("bindings is a non-empty list")
    if len(bindings) > PROJECT_VARIABLES_MAX:
        raise VariableError(f"too many bindings (max {PROJECT_VARIABLES_MAX})")
    wanted = [_binding(b, pid) for b in bindings]
    names = [b["name"] for b in wanted]
    if len(set(names)) != len(names):
        raise VariableError("a binding names each variable once")

    def fn(records: list[dict]) -> list[dict]:
        from . import variables  # the shared library's label check, under the same fences

        now = time.time()
        for b in wanted:
            # A binding's name is a label too (Hermes 7402): checked here, inside the fences.
            variables._withhold_label(b["name"], "name")
            if b["kind"] == "secret" and "value" in b:
                refuse_secret_in_labels(records, b["value"], [(b["name"], "name")])
            if "ref" in b:
                target = next(
                    (r for r in records if _is_global(r) and r["name"] == b["name"]), None
                )
                if target is None or target["kind"] != b["kind"]:
                    raise VariableError(
                        f"{b['name']}: there is no global {b['kind']} variable of that name to "
                        "refer to"
                    )
            try:
                old = _find(records, b["name"], SCOPE_PROJECT, pid)
            except VariableNotFound:
                old = None
            if old is not None:
                _retire(old)
                records.remove(old)
            created = old["created_at"] if old is not None else now
            updated = _bumped(old["updated_at"]) if old is not None else now
            if "ref" in b:
                rec = {
                    "name": b["name"],
                    "kind": b["kind"],
                    "scope": SCOPE_PROJECT,
                    "project_id": pid,
                    "ref": REF_GLOBAL,
                    "created_at": created,
                    "updated_at": updated,
                }
            else:
                rec = _stored(b, SCOPE_PROJECT, pid, created, updated)
            records.append(rec)
        mine = [r for r in records if r["project_id"] == pid]
        if len(mine) > PROJECT_VARIABLES_MAX:
            raise VariableError(f"too many bindings (max {PROJECT_VARIABLES_MAX})")
        return [public_binding(r) for r in sorted(mine, key=_key)]

    return _mutate(fn, repair_references=True)  # type: ignore[return-value]


def remove_project_bindings(project: object, names: object = None) -> list[str]:
    """Remove this project's bindings (all of them, or only ``names``) and nothing else — never a
    global variable, never another project's binding. Returns the names removed."""
    pid = project_id(project)
    only: set[str] | None = None
    if names is not None:
        if not isinstance(names, list):
            raise VariableError("names is a list")
        only = {_name(n) for n in names}

    def fn(records: list[dict]) -> list[str]:
        gone = [
            r
            for r in records
            if r["scope"] == SCOPE_PROJECT
            and r["project_id"] == pid
            and (only is None or r["name"] in only)
        ]
        for r in gone:
            _retire(r)
            records.remove(r)
        return sorted(r["name"] for r in gone)

    return _mutate(fn, repair_references=True)  # type: ignore[return-value]


def project_bindings(project: object) -> list[dict]:
    """This project's bindings, public shape (no secret value, no envelope)."""
    pid = project_id(project)
    return [
        public_binding(r)
        for r in sorted(_read_leniently(), key=_key)
        if r["scope"] == SCOPE_PROJECT and r["project_id"] == pid
    ]


class Resolver:
    """Resolution for ONE project over ONE strict read of the store (#1096 §3).

    ``text``: project binding → global → default. ``secret`` / ``secret_state``: the project's own
    secret or its recorded ``ref`` to the global one; never an implicit fallback. Every refusal
    names the variable; nothing here ever returns a value from a scope it was not entitled to."""

    def __init__(self, pid: str, records: list[dict]) -> None:
        self.project_id = pid
        self._project = {
            r["name"]: r for r in records if r["scope"] == SCOPE_PROJECT and r["project_id"] == pid
        }
        self._global = {r["name"]: r for r in records if _is_global(r)}

    def _global_of(self, name: str, kind: str, *, why: str) -> dict:
        rec = self._global.get(name)
        if rec is None or rec["kind"] != kind or rec.get("unset"):
            raise BindingUnusable(name, why)
        return rec

    def text(self, name: str, default: str | None = None) -> dict:
        """``{value, source: "project" | "global" | "default", revision}``, or ``BindingMissing``.
        A record of the OTHER kind at a level refuses rather than falling past it."""
        _name(name)
        rec = self._project.get(name)
        if rec is not None:
            if rec["kind"] != "text":
                raise BindingUnusable(name, "the project binds a secret under this name")
            if rec.get("ref") == REF_GLOBAL:
                g = self._global_of(
                    name, "text", why="the global variable this project refers to is gone"
                )
                return {"value": g["value"], "source": "global", "revision": g["updated_at"]}
            return {"value": rec["value"], "source": "project", "revision": rec["updated_at"]}
        g = self._global.get(name)
        if g is not None:
            if g["kind"] != "text":
                raise BindingUnusable(name, "the global variable of this name is a secret")
            if not g.get("unset"):
                return {"value": g["value"], "source": "global", "revision": g["updated_at"]}
        if default is not None:
            return {"value": default, "source": "default", "revision": None}
        raise BindingMissing(name)

    def _secret_record(self, name: str) -> tuple[dict, str]:
        _name(name)
        rec = self._project.get(name)
        if rec is None:
            raise BindingMissing(name)  # never an implicit fallback to the global secret
        if rec["kind"] != "secret":
            raise BindingUnusable(name, f"the project binds a {rec['kind']} under this name")
        if rec.get("ref") == REF_GLOBAL:
            g = self._global_of(
                name, "secret", why="the global secret this project refers to is gone"
            )
            return g, "global"
        return rec, "project"

    def secret_state(self, name: str) -> dict:
        """``{state: "ok" | "reentry", source, revision}`` without the value, or
        ``BindingMissing``."""
        rec, source = self._secret_record(name)
        ok = _decrypt(rec) is not None
        return {"state": "ok" if ok else "reentry", "source": source, "revision": rec["updated_at"]}

    def secret(self, name: str) -> str:
        """The plaintext. SERVER-ONLY. A binding that needs re-entry REFUSES — it never falls
        through to another scope's value."""
        rec, source = self._secret_record(name)
        v = _decrypt(rec)
        if v is None:
            raise BindingUnusable(
                name,
                "needs re-entry"
                if source == "project"
                else "the global secret this project refers to needs re-entry",
            )
        return v


def resolver(project: object) -> Resolver:
    """A ``Resolver`` for ``project`` over one STRICT read (``ResolutionUnavailable`` otherwise)."""
    pid = project_id(project)
    return Resolver(pid, _read_strictly())


__all__ = [
    "LIMITS",
    "PROJECT_VARIABLES_MAX",
    "REF_GLOBAL",
    "SCOPES",
    "STORE_VERSION",
    "VALUE_MAX",
    "VARIABLES_MAX",
    "BindingMissing",
    "BindingUnusable",
    "ResolutionUnavailable",
    "Resolver",
    "VariableConflict",
    "VariableError",
    "VariableInUse",
    "VariableNotFound",
    "VariableRefsUnknown",
    "VariableStoreUnsupported",
    "bind_project",
    "create_variable",
    "delete_variable",
    "list_variables",
    "project_bindings",
    "project_id",
    "public",
    "public_binding",
    "remove_project_bindings",
    "resolver",
    "revisions",
    "secret_state",
    "secret_values",
    "secret_values_checked",
    "store_path",
    "update_variable",
    "validate",
    "values",
]
