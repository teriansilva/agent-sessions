"""The bytes reviewed for deployment, with no probe, secret lookup or filesystem mutation."""

from __future__ import annotations

import pytest

from agent_sessions.playbooks import materials, schema
from agent_sessions.playbooks.tree import Tree


def _bundle(*, template=True, kind="text", disposition="managed"):
    return {
        "variables": [{"name": "name", "kind": kind}],
        "materials": [
            {"path": "notes.md", "kind": "file", "disposition": disposition, "template": template}
        ],
    }


@pytest.mark.parametrize("disposition", ["managed", "seed", "reference"])
def test_review_renders_all_dispositions_once_without_interpreting_variable_values(disposition):
    tree = Tree(files={"template/notes.md": b"Hello {{name}}; ${{ secrets.VALUE }}"})
    [item] = materials.render(_bundle(disposition=disposition), tree, {"name": "{{other}}"})
    assert item == materials.Material(
        "notes.md", disposition, data=b"Hello {{other}}; ${{ secrets.VALUE }}"
    )
    assert tree.files["template/notes.md"].startswith(b"Hello {{name}}")


@pytest.mark.parametrize("data", [b"verbatim {{name}}", b"\x00\xff\xfe"])
def test_verbatim_materials_keep_every_byte(data):
    [item] = materials.render(_bundle(template=False), Tree(files={"template/notes.md": data}), {})
    assert item.data == data


@pytest.mark.parametrize("values", [{}, {"name": None}, {"name": 3}, {"name": True}])
def test_unresolved_or_unconverted_text_never_reaches_a_material(values):
    with pytest.raises(materials.MaterialError, match="needs a resolved text value"):
        materials.render(_bundle(), Tree(files={"template/notes.md": b"{{name}}"}), values)


def test_secret_tokens_are_refused_defensively_without_echoing_the_value():
    with pytest.raises(materials.MaterialError, match="not a declared text variable") as error:
        materials.render(
            _bundle(kind="secret"),
            Tree(files={"template/notes.md": b"{{name}}"}),
            {"name": "a-private-value"},
        )
    assert "a-private-value" not in str(error.value)


def test_alias_is_a_declaration_and_needs_no_source_file():
    bundle = {
        "variables": [],
        "materials": [
            {
                "path": "AGENTS.md",
                "kind": "instruction-alias",
                "disposition": "managed",
                "target": "RULES.md",
            }
        ],
    }
    assert materials.render(bundle, Tree(), {}) == [
        materials.Material("AGENTS.md", "managed", target="RULES.md")
    ]


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
def test_render_expansion_cannot_escape_bundle_bounds(monkeypatch, limit):
    monkeypatch.setattr(schema, limit, 5)
    with pytest.raises(materials.MaterialError, match="size limit"):
        materials.render(
            _bundle(), Tree(files={"template/notes.md": b"{{name}}"}), {"name": "abcdef"}
        )


def test_region_update_and_remove_keep_unrelated_operator_edits_byte_for_byte():
    initial, original_digest = materials.append_region(
        b"Operator preface\n", "deployment-one", b"A"
    )
    edited_outside = b"new preface\n" + initial + b"new tail\n"
    updated, new_digest = materials.change_region(
        edited_outside, "deployment-one", original_digest, b"B"
    )
    assert b"\nB\n" in updated and new_digest != original_digest
    removed, no_digest = materials.change_region(updated, "deployment-one", new_digest, None)
    assert removed == b"new preface\nOperator preface\n\nnew tail\n"
    assert no_digest is None


@pytest.mark.parametrize(
    "change",
    ["missing_begin", "missing_end", "duplicate_begin", "duplicate_end", "reordered", "edited"],
)
def test_region_refuses_marker_damage_and_operator_edits(change):
    block, expected = materials.region_block("deployment-one", b"original")
    begin, end = materials._markers("deployment-one")
    damaged = {
        "missing_begin": block.replace(begin, b""),
        "missing_end": block.replace(end, b""),
        "duplicate_begin": begin + block,
        "duplicate_end": block + end,
        "reordered": end + b"\noriginal\n" + begin,
        "edited": block.replace(b"original", b"operator addition"),
    }[change]
    with pytest.raises(materials.MaterialError):
        materials.change_region(damaged, "deployment-one", expected, b"replacement")
    with pytest.raises(materials.MaterialError):
        materials.change_region(damaged, "deployment-one", expected, None)


def test_new_region_never_adopts_markers_and_cannot_render_its_own_markers():
    block, _ = materials.region_block("deployment-one", b"body")
    with pytest.raises(materials.MaterialError, match="already contains"):
        materials.append_region(block, "deployment-one", b"different")
    with pytest.raises(materials.MaterialError, match="contains its managed region markers"):
        materials.region_block("deployment-one", block)


@pytest.mark.parametrize("current,regular", [(None, False), (b"same", False), (b"different", True)])
def test_reference_adoption_refuses_missing_links_and_different_bytes(current, regular):
    with pytest.raises(materials.MaterialError):
        materials.require_adoption(current, b"same", regular_file=regular)


def test_reference_adoption_records_identical_regular_file_bytes():
    assert materials.require_adoption(b"same", b"same", regular_file=True) == materials.digest(
        b"same"
    )
