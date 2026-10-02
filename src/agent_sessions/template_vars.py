"""The template variables library (#1090) — values defined once, used by every template — and,
since store v3 (#1191), the per-project bindings a deployed playbook resolves against.

A variable is a named value (``staging_host``, ``test_cmd``, …) that a template field declared with
``source: "library"`` takes instead of a default of its own, so one edit reaches every template
that uses it at its next send. Names share ``templates.FIELD_NAME_RE``: a library field IS the
variable, by name, and ``{{name}}`` in the body stays the one token syntax.

**Two kinds (store v2, Phase 2).** A ``text`` variable carries its ``value``. A ``secret`` variable
carries only an AES-GCM envelope (``template_secrets``) — never plaintext, and no response ever
includes the envelope: a secret row says ``set`` / ``needs_reentry`` and nothing else. Both kinds
live in this ONE list under ONE lock, which is what makes a name unique across kinds (per scope)
by construction.

**Two scopes (store v3, #1191).** Every record carries ``scope`` (``global`` | ``project``) and
``project_id`` (the ``projects.py`` entity id, ``null`` for global). Identity is
``(scope, project_id, name)``: ``global:<name>`` or ``project:<entity_id>:<name>``, so the same
name in two projects is two independent values. A v1/v2 file reads as all-global, and its secret
envelopes as ``aad: "legacy"`` (bare-name AAD) — decided by the FILE's version, never guessed per
envelope; the first accepted write publishes v3 with those markers kept, and a legacy envelope is
re-encrypted under its scoped AAD on that variable's next write. An older build refuses a v3 file
(read-empty, no write) by the newer-store rule below. A project record is one of three things: its
own ``value``, its own secret ``envelope``, or ``ref: "global"`` — the recorded, explicit choice to
use the global variable of the same name and kind. The global library views (``values``,
``secret_state``, ``list_variables``, ``revisions``, ``secret_values``) are GLOBAL ONLY; redaction
(``secret_values_checked``) covers every scope.

**Resolution (``resolver``).** Text: project binding → global library → field default; secrets:
the project's own secret, or its recorded ``ref`` to the global one — a secret NEVER falls back to
another scope by itself. A project binding that exists but cannot be used (needs re-entry, the other
kind, a ref whose global is gone or unusable) REFUSES (``BindingUnusable``) and never falls through,
because a silent swap to another value is worse than a refusal. Resolution reads the store STRICTLY:
an unreadable, damaged or newer store raises instead of resolving against the part it could read.

**Same store discipline as ``templates.py``, deliberately:** its own file
(``~/.config/agent-sessions/template-variables.json``, override ``AGENT_SESSIONS_TEMPLATE_VARS``),
mode 0600, every write through ``atomicjson`` under the sidecar lock; lenient read, strict write; a
damaged file is hard-linked aside before the first accepted write replaces it; a newer store
version is refused, never rewritten; edits and deletes are fenced by ``updated_at``. The helpers
are ``templates``' own, imported rather than restated, so the two stores cannot drift apart.

**No rename, and dependency-aware deletion under ONE fence.** A variable's name is its identity, so
renaming is delete + recreate. Deleting a GLOBAL variable refuses while any template references the
name OR any project binding records ``ref: "global"`` to it (``VariableInUse``, carrying both
lists). The sidecar lock is the one binding/deletion fence: ``bind_project`` checks the global it
refers to and ``delete_variable`` checks its dependants inside the same ``_mutate``, so a bind
racing a delete ends with exactly one of them refused. The template half is FAIL-CLOSED: if the
template library cannot be read in full the delete is refused (``VariableRefsUnknown``). It reads
the template store without holding its lock: a template saved a moment later may name a variable
that no longer exists, which is already a first-class state (the picker marks it *missing library
variable* and never sends the token literally). Removing a project's bindings
(``remove_project_bindings``) touches that project's records only — never a global, never another
project's.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from . import template_secrets
from . import templates as tstore
from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger(__name__)

STORE_VERSION = 3

VARIABLES_MAX = 100
VALUE_MAX = 2_000
#: Bindings one project may hold (a playbook declares at most 32 variables; room for two).
PROJECT_VARIABLES_MAX = 64
#: Every record across every scope: the file stays small enough to read on every send.
RECORDS_MAX = 4_000

EDITABLE_KEYS = frozenset({"name", "value", "kind"})
KINDS = ("text", "secret")
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
        raise VariableError("kind must be 'text' or 'secret'")
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
    body: dict = {"name": name, "kind": kind, "scope": scope, "project_id": pid}
    if "ref" in raw:
        if version < 3 or scope != SCOPE_PROJECT or raw["ref"] != REF_GLOBAL:
            raise VariableError("only a project binding may refer to the global variable")
        unknown = sorted(set(raw) - base - {"ref"})
        if unknown:
            raise VariableError(f"unknown record keys: {unknown}")
        body["ref"] = REF_GLOBAL
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
    return {
        **body,
        "created_at": tstore._number(raw.get("created_at"), "created_at"),
        "updated_at": tstore._number(raw.get("updated_at"), "updated_at"),
    }


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
        out["set"] = True
        out["needs_reentry"] = _decrypt(rec) is None
    else:
        out["value"] = rec["value"]
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
    else:
        out["value"] = rec["value"]
    return out


# ---- store I/O -------------------------------------------------------------------------------


def _read(path: Path) -> tuple[list[dict], bool]:
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
    return records, damaged


def _mutate(fn: Callable[[list[dict]], dict | list | None]) -> dict | list | None:
    """Read-modify-write under the sidecar lock; a refused ``fn`` touches nothing (see
    ``templates._mutate``, whose quarantine-before-publish order this shares). The sidecar lock is
    also THE binding/deletion fence (#1191): every bind and every delete decides inside it."""
    path = store_path()
    # Under the write seam's cross-process fence: a template send's fingerprint re-reads these
    # revisions inside it right before byte one (#1090, Hermes on #1105).
    from . import session_input

    with session_input.mutation_fence(), json_write_lock(path):
        records, damaged = _read(path)
        result = fn(records)
        if len(records) > RECORDS_MAX:
            raise VariableError(f"the variables store is full (max {RECORDS_MAX} records)")
        if damaged and path.exists():
            tstore._quarantine(path)
        records.sort(key=_key)
        atomic_write_json(path, {"version": STORE_VERSION, "variables": records}, mode=0o600)
    return result


def _read_leniently() -> list[dict]:
    try:
        records, _ = _read(store_path())
    except VariableStoreUnsupported as e:
        log.warning("template vars: %s; serving an empty library", e)
        return []
    return records


def _globals() -> list[dict]:
    return [r for r in _read_leniently() if _is_global(r)]


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
    return records


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
    return [public(rec, usage.get(rec["name"], [])) for rec in sorted(_globals(), key=_key)]


def values() -> dict[str, str]:
    """``{name: value}`` of the global TEXT variables — what a ``library`` text field resolves
    against. A secret's name is deliberately absent: a text field naming it is a missing
    variable."""
    return {r["name"]: r["value"] for r in _globals() if r["kind"] == "text"}


def secret_state() -> dict[str, str]:
    """``{name: "ok" | "reentry"}`` for every global SECRET variable — what a library secret field
    resolves against, without its value."""
    return {
        r["name"]: "ok" if _decrypt(r) is not None else "reentry"
        for r in _globals()
        if r["kind"] == "secret"
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


def secret_values_checked() -> list[str]:
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
            if isinstance(item, dict) and (item.get("kind") == "secret" or "secret" in item):
                raise VariableError(
                    "a secret record in the variables store is unreadable"
                ) from None
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
    return {r["name"]: r["updated_at"] for r in _globals()}


def secret_values() -> dict[str, str]:
    """``{name: plaintext}`` for every GLOBAL secret that decrypts. SERVER-ONLY: the send renders
    with it. Nothing that answers the browser may call it."""
    out: dict[str, str] = {}
    for r in _globals():
        if r["kind"] == "secret":
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


def create_variable(payload: object) -> dict:
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        # One list, one lock: a name is unique across BOTH kinds (per scope) by construction.
        globals_ = [r for r in records if _is_global(r)]
        if any(r["name"] == fields["name"] for r in globals_):
            raise VariableError(f"a variable named {fields['name']} already exists")
        projects = _project_dependants(records, fields["name"])
        if projects:
            raise VariableInUse(fields["name"], [], projects)
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
        if rec["kind"] == "secret":
            value = _secret_value(payload)
            _retire(rec)
            rec["secret"] = template_secrets.encrypt_scoped(SCOPE_GLOBAL, None, name, value)
        else:
            rec["value"] = _value(payload)
        rec["updated_at"] = _bumped(rec["updated_at"])
        return public(rec)

    return _mutate(fn)  # type: ignore[return-value]


def _project_dependants(records: list[dict], name: str) -> list[str]:
    return sorted(
        {
            r["project_id"]
            for r in records
            if r["scope"] == SCOPE_PROJECT and r["name"] == name and r.get("ref") == REF_GLOBAL
        }
    )


def delete_variable(name: str, expected_updated_at: float) -> None:
    """Delete a GLOBAL variable — refused while a template references it or a project binding
    records ``ref: "global"`` to it, both decided under the one sidecar lock."""

    def fn(records: list[dict]) -> None:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(public(rec))
        projects = _project_dependants(records, name)
        # Fail-closed: an inventory that could not be read in full is not "no dependants".
        dependants = tstore.library_references_checked(name)
        if dependants or projects:
            raise VariableInUse(name, dependants, projects)
        _retire(rec)
        records.remove(rec)
        return None

    _mutate(fn)


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
        now = time.time()
        for b in wanted:
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

    return _mutate(fn)  # type: ignore[return-value]


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

    return _mutate(fn)  # type: ignore[return-value]


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
        if rec is None or rec["kind"] != kind:
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
            raise BindingUnusable(name, "the project binds a text value under this name")
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
