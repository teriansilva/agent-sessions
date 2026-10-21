"""Shared-library definitions use the existing playbook type contract (#1390).

Metadata contains no stored value. Legacy library values remain strings for existing
consumers; canonical conversion prevents a boolean from being accepted as an integer.
Imports of the playbook validator are deferred because its schema also uses the library.
"""

from __future__ import annotations

import re

METADATA_KEYS = frozenset(
    {"value_type", "label", "help", "required", "pattern", "choices", "default", "example"}
)


class DefinitionError(ValueError):
    """A definition or its value does not satisfy the shared type contract."""


def metadata(name: str, kind: str, raw: object) -> dict:
    """Validate portable metadata without accepting a secret default or example."""
    from .playbooks import validate
    from .plugins.manifest import ManifestError, _Reader

    if not isinstance(raw, dict) or set(raw) - METADATA_KEYS:
        raise DefinitionError("expected variable metadata with known fields only")
    portable = {key: value for key, value in raw.items() if key != "value_type"}
    portable["type"] = raw.get("value_type", "text")
    try:
        result = validate._variables(
            _Reader({"variables": [{"name": name, "kind": kind, **portable}]}, "")
        )[name]
    except ManifestError as e:
        raise DefinitionError(str(e)) from None
    # The normalized playbook reader includes absent optional defaults as None;
    # absence must remain absence when this definition is written/exported.
    result["value_type"] = result.pop("type")
    return {
        key: value
        for key, value in result.items()
        if key in METADATA_KEYS
        and (key not in {"pattern", "default", "example"} or key in raw)
        and (key != "choices" or result["value_type"] == "enum")
    }


def typed_value(name: str, definition: dict, value: object, *, max_len: int) -> object:
    """Validate a native JSON value. Strings never stand in for booleans/integers."""
    from .playbooks import validate
    from .plugins.manifest import ManifestError

    var = {
        "name": name,
        "pattern": None,
        "choices": [],
        **definition,
        "type": definition.get("value_type", "text"),
    }
    try:
        return validate._variable_default(
            var, value, f"variable {name}", max_len=max_len, multiline=var["type"] == "text"
        )
    except ManifestError as e:
        raise DefinitionError(str(e)) from None


def stored_value(name: str, definition: dict, value: str, *, max_len: int) -> object:
    """Parse the legacy string representation, refusing noncanonical coercions."""
    raw: object = value
    if definition.get("value_type", "text") == "int":
        if not re.fullmatch(r"(?:0|-?[1-9][0-9]{0,15})", value, flags=re.ASCII):
            raise DefinitionError(f"{name}: needs an integer")
        raw = int(value)
    elif definition.get("value_type") == "bool":
        if value not in ("true", "false"):
            raise DefinitionError(f"{name}: needs true or false")
        raw = value == "true"
    return typed_value(name, definition, raw, max_len=max_len)


def text(value: object) -> str:
    """The compatible text representation, after typed validation."""
    return ("true" if value else "false") if type(value) is bool else str(value)
