"""The template variables library (#1090, Phase 1) — values defined once, used by every template.

A variable is a named value (``staging_host``, ``test_cmd``, …) that a template field declared with
``source: "library"`` takes instead of a default of its own, so one edit reaches every template
that uses it at its next send. Names share ``templates.FIELD_NAME_RE``: a library field IS the
variable, by name, and ``{{name}}`` in the body stays the one token syntax.

**Two kinds (store v2, Phase 2).** A ``text`` variable carries its ``value``. A ``secret`` variable
carries only an AES-GCM envelope (``template_secrets``) — never plaintext, and no response ever
includes the envelope: a secret row says ``set`` / ``needs_reentry`` and nothing else. Both kinds
live in this ONE list under ONE lock, which is what makes a name unique across kinds by
construction — a text field can never resolve a secret's name through a second store, or the
reverse. A v1 file reads as all-text; the first accepted write publishes it as v2.

**Same store discipline as ``templates.py``, deliberately:** its own file
(``~/.config/agent-sessions/template-variables.json``, override ``AGENT_SESSIONS_TEMPLATE_VARS``),
mode 0600, every write through ``atomicjson`` under the sidecar lock; lenient read, strict write; a
damaged file is hard-linked aside before the first accepted write replaces it; a newer store
version is refused, never rewritten; edits and deletes are fenced by ``updated_at``. The helpers
are ``templates``' own, imported rather than restated, so the two stores cannot drift apart.

**No rename.** A variable's name is its identity — the key every referencing field matches on — so
renaming is delete + recreate, and DELETE refuses while any template still references the name
(``VariableInUse``, carrying the dependants). That check is FAIL-CLOSED: if the template library
cannot be read in full (unreadable, damaged, a newer version) the delete is refused
(``VariableRefsUnknown``) — "I could not see every template" is never "no template uses it".
It reads the template store without holding its lock: a template saved a moment later may name a
variable that no longer exists, which is already a first-class state (the picker marks it
*missing library variable* and never sends the token literally), so there is nothing the extra
lock would protect.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from . import template_secrets
from . import templates as tstore
from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger(__name__)

STORE_VERSION = 2

VARIABLES_MAX = 100
VALUE_MAX = 2_000

EDITABLE_KEYS = frozenset({"name", "value", "kind"})
KINDS = ("text", "secret")

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
    """DELETE refused: these templates still reference the variable (``[{id, name}]``)."""

    def __init__(self, name: str, dependants: list[dict]) -> None:
        super().__init__(f"variable {name!r} is still used by {len(dependants)} template(s)")
        self.name = name
        self.dependants = dependants


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


def _coerce_record(raw: object) -> dict:
    """A stored record, re-validated on read. v1 records carry no ``kind`` and are text."""
    if not isinstance(raw, dict):
        raise VariableError("record is not an object")
    kind = _kind(raw.get("kind"))
    name = _name(raw.get("name"))
    if kind == "secret":
        unknown = sorted(set(raw) - {"name", "kind", "secret", "created_at", "updated_at"})
        env = raw.get("secret")
        if unknown or not template_secrets.valid_envelope(env):
            raise VariableError("a secret record holds a name, an envelope and timestamps only")
        body: dict = {"name": name, "kind": kind, "secret": dict(env)}  # type: ignore[arg-type]
    else:
        unknown = sorted(set(raw) - {"name", "kind", "value", "created_at", "updated_at"})
        if unknown:
            raise VariableError(f"unknown record keys: {unknown}")
        body = {"name": name, "kind": kind, "value": _value(raw)}
    return {
        **body,
        "created_at": tstore._number(raw.get("created_at"), "created_at"),
        "updated_at": tstore._number(raw.get("updated_at"), "updated_at"),
    }


def public(rec: dict, used_by: list[dict] | None = None) -> dict:
    """What a response may carry for ``rec``: a text variable's value; for a secret, only whether
    one is stored and whether it still decrypts. The envelope never leaves the server."""
    out = {
        "name": rec["name"],
        "kind": rec["kind"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }
    if rec["kind"] == "secret":
        out["set"] = True
        out["needs_reentry"] = template_secrets.decrypt(rec["name"], rec["secret"]) is None
    else:
        out["value"] = rec["value"]
    if used_by is not None:
        out["used_by"] = used_by
    return out


# ---- store I/O -------------------------------------------------------------------------------


def _read(path: Path) -> tuple[list[dict], bool]:
    """The trustworthy records plus whether the file needs quarantining before the next write."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], False
    except (OSError, ValueError) as e:
        log.warning("template vars: %s is unreadable (%s); serving an empty library", path, e)
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
    seen: set[str] = set()
    damaged = False
    for item in raw["variables"]:
        try:
            rec = _coerce_record(item)
        except VariableError as e:
            log.warning("template vars: skipping an unreadable record in %s: %s", path, e)
            damaged = True
            continue
        if rec["name"] in seen:
            damaged = True
            continue
        seen.add(rec["name"])
        records.append(rec)
    return records, damaged


def _mutate(fn: Callable[[list[dict]], dict | None]) -> dict | None:
    """Read-modify-write under the sidecar lock; a refused ``fn`` touches nothing (see
    ``templates._mutate``, whose quarantine-before-publish order this shares)."""
    path = store_path()
    # Under the write seam's cross-process fence: a template send's fingerprint re-reads these
    # revisions inside it right before byte one (#1090, Hermes on #1105).
    from . import session_input

    with session_input.mutation_fence(), json_write_lock(path):
        records, damaged = _read(path)
        result = fn(records)
        if damaged and path.exists():
            tstore._quarantine(path)
        records.sort(key=lambda r: r["name"])
        atomic_write_json(path, {"version": STORE_VERSION, "variables": records}, mode=0o600)
    return result


def _read_leniently() -> list[dict]:
    try:
        records, _ = _read(store_path())
    except VariableStoreUnsupported as e:
        log.warning("template vars: %s; serving an empty library", e)
        return []
    return records


def _find(records: list[dict], name: str) -> dict:
    for rec in records:
        if rec["name"] == name:
            return rec
    raise VariableNotFound(name)


def _bumped(previous: float) -> float:
    try:
        return tstore._bumped(previous)
    except tstore.TemplateError:
        raise VariableError(
            "this variable's revision counter is exhausted; delete and recreate it"
        ) from None


# ---- public API ------------------------------------------------------------------------------


def list_variables() -> list[dict]:
    """Every readable variable, by name, each with the templates that use it (``used_by``)."""
    usage = tstore.library_usage()  # one read of the template store, not one per variable
    return [
        public(rec, usage.get(rec["name"], []))
        for rec in sorted(_read_leniently(), key=lambda r: r["name"])
    ]


def values() -> dict[str, str]:
    """``{name: value}`` of the TEXT variables — what a ``library`` text field resolves against.
    A secret's name is deliberately absent: a text field naming it is a missing variable."""
    return {r["name"]: r["value"] for r in _read_leniently() if r["kind"] == "text"}


def secret_state() -> dict[str, str]:
    """``{name: "ok" | "reentry"}`` for every SECRET variable — what a library secret field
    resolves against, without its value."""
    return {
        r["name"]: "ok"
        if template_secrets.decrypt(r["name"], r["secret"]) is not None
        else "reentry"
        for r in _read_leniently()
        if r["kind"] == "secret"
    }


def secret_values_checked() -> dict[str, str]:
    """``secret_values`` for REDACTION: fail-closed where the lenient read would fail open.

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
    out: dict[str, str] = {}
    for item in raw["variables"]:
        try:
            rec = _coerce_record(item)
        except VariableError:
            # A record that fails validation may still carry a decryptable secret (damaged
            # METADATA — a bad timestamp — does not touch the authenticated envelope). Recover it
            # from the envelope alone; if it looks like a secret and cannot be recovered, the
            # inventory is incomplete and redaction must refuse (Hermes on #1105).
            name = item.get("name") if isinstance(item, dict) else None
            env = item.get("secret") if isinstance(item, dict) else None
            if isinstance(name, str) and template_secrets.valid_envelope(env):
                v = template_secrets.decrypt_checked(name, env)  # type: ignore[arg-type]
                if v is not None:
                    out[name] = v
                continue
            if isinstance(item, dict) and (item.get("kind") == "secret" or "secret" in item):
                raise VariableError(
                    "a secret record in the variables store is unreadable"
                ) from None
            continue
        if rec["kind"] == "secret":
            v = template_secrets.decrypt_checked(rec["name"], rec["secret"])
            if v is not None:
                out[rec["name"]] = v
    return out


def revisions() -> dict[str, float]:
    """``{name: updated_at}`` — what a send binds its library values to (Hermes on #1105)."""
    return {r["name"]: r["updated_at"] for r in _read_leniently()}


def secret_values() -> dict[str, str]:
    """``{name: plaintext}`` for every secret that decrypts. SERVER-ONLY: the send renders with
    it, and the redaction set is built from it. Nothing that answers the browser may call it."""
    out: dict[str, str] = {}
    for r in _read_leniently():
        if r["kind"] == "secret":
            v = template_secrets.decrypt(r["name"], r["secret"])
            if v is not None:
                out[r["name"]] = v
    return out


def _stored(fields: dict, now_created: float, now_updated: float) -> dict:
    """The record to store for validated ``fields``: a secret's plaintext becomes its envelope."""
    body: dict = {"name": fields["name"], "kind": fields["kind"]}
    if fields["kind"] == "secret":
        body["secret"] = template_secrets.encrypt(fields["name"], fields["value"])
    else:
        body["value"] = fields["value"]
    return {**body, "created_at": now_created, "updated_at": now_updated}


def create_variable(payload: object) -> dict:
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        # One list, one lock: a name is unique across BOTH kinds by construction.
        if any(r["name"] == fields["name"] for r in records):
            raise VariableError(f"a variable named {fields['name']} already exists")
        if len(records) >= VARIABLES_MAX:
            raise VariableError(f"too many variables (max {VARIABLES_MAX})")
        now = time.time()
        rec = _stored(fields, now, now)
        records.append(rec)
        return public(rec)

    return _mutate(fn)  # type: ignore[return-value]


def update_variable(name: str, payload: object, expected_updated_at: float) -> dict:
    """Replace the value. The name and the kind are the identity and never change here (no rename,
    no conversion — delete and recreate). A secret's value is write-only: replaced, never read."""
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
            old = template_secrets.decrypt(name, rec["secret"])
            if old is not None:
                template_secrets.retire([old])  # still in transcripts: keep redacting it
            rec["secret"] = template_secrets.encrypt(name, value)
        else:
            rec["value"] = _value(payload)
        rec["updated_at"] = _bumped(rec["updated_at"])
        return public(rec)

    return _mutate(fn)  # type: ignore[return-value]


def delete_variable(name: str, expected_updated_at: float) -> None:
    def fn(records: list[dict]) -> None:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(public(rec))
        # Fail-closed: an inventory that could not be read in full is not "no dependants".
        dependants = tstore.library_references_checked(name)
        if dependants:
            raise VariableInUse(name, dependants)
        if rec["kind"] == "secret":
            old = template_secrets.decrypt(name, rec["secret"])
            if old is not None:
                template_secrets.retire([old])  # still in transcripts: keep redacting it
        records.remove(rec)
        return None

    _mutate(fn)


__all__ = [
    "LIMITS",
    "STORE_VERSION",
    "VALUE_MAX",
    "VARIABLES_MAX",
    "VariableConflict",
    "VariableError",
    "VariableInUse",
    "VariableNotFound",
    "VariableRefsUnknown",
    "VariableStoreUnsupported",
    "create_variable",
    "delete_variable",
    "list_variables",
    "public",
    "revisions",
    "secret_state",
    "secret_values",
    "secret_values_checked",
    "store_path",
    "update_variable",
    "validate",
    "values",
]
