"""The template variables library (#1090, Phase 1) — values defined once, used by every template.

A variable is a named value (``staging_host``, ``test_cmd``, …) that a template field declared with
``source: "library"`` takes instead of a default of its own, so one edit reaches every template
that uses it at its next send. Names share ``templates.FIELD_NAME_RE``: a library field IS the
variable, by name, and ``{{name}}`` in the body stays the one token syntax.

Phase 1 holds **text** values only. Secrets (Phase 2) live in their own encrypted store and never
in this file — nothing here is ever sensitive enough to withhold from the browser.

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

from . import templates as tstore
from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger(__name__)

STORE_VERSION = 1

VARIABLES_MAX = 100
VALUE_MAX = 2_000

EDITABLE_KEYS = frozenset({"name", "value"})

LIMITS = {
    "variables_max": VARIABLES_MAX,
    "value_max": VALUE_MAX,
    "name_max": 32,
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


def validate(payload: object) -> dict:
    """A new variable's fields, or ``VariableError``. Unknown keys are refused, not ignored."""
    if not isinstance(payload, dict):
        raise VariableError("expected a JSON object")
    unknown = sorted(set(payload) - EDITABLE_KEYS)
    if unknown:
        raise VariableError(f"unknown fields: {unknown}")
    return {"name": _name(payload.get("name")), "value": _value(payload)}


def _coerce_record(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise VariableError("record is not an object")
    fields = validate({k: raw.get(k) for k in EDITABLE_KEYS if k in raw})
    return {
        **fields,
        "created_at": tstore._number(raw.get("created_at"), "created_at"),
        "updated_at": tstore._number(raw.get("updated_at"), "updated_at"),
    }


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
    with json_write_lock(path):
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
        {**rec, "used_by": usage.get(rec["name"], [])}
        for rec in sorted(_read_leniently(), key=lambda r: r["name"])
    ]


def values() -> dict[str, str]:
    """``{name: value}`` — what a send resolves ``library`` fields against."""
    return {r["name"]: r["value"] for r in _read_leniently()}


def create_variable(payload: object) -> dict:
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        if any(r["name"] == fields["name"] for r in records):
            raise VariableError(f"a variable named {fields['name']} already exists")
        if len(records) >= VARIABLES_MAX:
            raise VariableError(f"too many variables (max {VARIABLES_MAX})")
        now = time.time()
        rec = {**fields, "created_at": now, "updated_at": now}
        records.append(rec)
        return dict(rec)

    return _mutate(fn)  # type: ignore[return-value]


def update_variable(name: str, payload: object, expected_updated_at: float) -> dict:
    """Replace the value. The name is the identity and is never changed here (no rename)."""
    if not isinstance(payload, dict):
        raise VariableError("expected a JSON object")
    unknown = sorted(set(payload) - {"value"})
    if unknown:
        raise VariableError(f"unknown fields: {unknown} (a variable cannot be renamed)")
    value = _value(payload)

    def fn(records: list[dict]) -> dict:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(dict(rec))
        rec["value"] = value
        rec["updated_at"] = _bumped(rec["updated_at"])
        return dict(rec)

    return _mutate(fn)  # type: ignore[return-value]


def delete_variable(name: str, expected_updated_at: float) -> None:
    def fn(records: list[dict]) -> None:
        rec = _find(records, name)
        if rec["updated_at"] != expected_updated_at:
            raise VariableConflict(dict(rec))
        # Fail-closed: an inventory that could not be read in full is not "no dependants".
        dependants = tstore.library_references_checked(name)
        if dependants:
            raise VariableInUse(name, dependants)
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
    "store_path",
    "update_variable",
    "validate",
    "values",
]
