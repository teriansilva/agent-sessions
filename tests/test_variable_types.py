"""Shared-library metadata cannot weaken playbook value or secret definitions."""

import pytest

from agent_sessions import variable_types as types


@pytest.mark.parametrize(
    ("definition", "value", "stored"),
    [
        ({}, "first\nsecond", "first\nsecond"),
        ({"value_type": "url"}, "https://example.test", "https://example.test"),
        ({"value_type": "path"}, "~/work/project", "~/work/project"),
        ({"value_type": "enum", "choices": ["a", "b"]}, "b", "b"),
        ({"value_type": "int"}, 12, "12"),
        ({"value_type": "bool"}, False, "false"),
    ],
)
def test_native_and_legacy_values_share_the_same_type(definition, value, stored):
    metadata = types.metadata("input", "text", definition)
    assert types.typed_value("input", metadata, value, max_len=2000) == value
    assert types.text(value) == stored
    parsed = types.stored_value("input", metadata, stored, max_len=2000)
    assert type(parsed) is type(value)
    assert parsed == value
    assert types.metadata("input", "text", metadata) == metadata


@pytest.mark.parametrize("raw", [True, "1", 1.5, None])
def test_native_integer_does_not_coerce(raw):
    with pytest.raises(types.DefinitionError):
        types.typed_value("count", {"value_type": "int"}, raw, max_len=2000)


@pytest.mark.parametrize("raw", ["01", "-0", "+1", " 1", "1.0", "true", "١"])
def test_legacy_integer_requires_canonical_ascii(raw):
    with pytest.raises(types.DefinitionError):
        types.stored_value("count", {"value_type": "int"}, raw, max_len=2000)


@pytest.mark.parametrize(
    "raw", [{"default": "hidden"}, {"example": "hidden"}, {"value_type": "url"}]
)
def test_secret_metadata_cannot_hold_values_or_other_types(raw):
    with pytest.raises(types.DefinitionError):
        types.metadata("credential", "secret", raw)


@pytest.mark.parametrize("raw", [{"value": "hidden"}, {"secret": {}}, {"id": "replacement"}])
def test_metadata_cannot_smuggle_value_envelope_or_identity(raw):
    with pytest.raises(types.DefinitionError):
        types.metadata("input", "text", raw)


def test_missing_defaults_stay_absent_and_supplied_defaults_stay_typed():
    assert "default" not in types.metadata("input", "text", {})
    assert (
        types.metadata("enabled", "text", {"value_type": "bool", "default": False})["default"]
        is False
    )


@pytest.mark.parametrize("value", ["../escape", "path/*", "/path//empty"])
def test_paths_preserve_the_playbook_segment_boundary(value):
    with pytest.raises(types.DefinitionError):
        types.typed_value("path", {"value_type": "path"}, value, max_len=2000)
