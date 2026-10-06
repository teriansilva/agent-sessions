"""Ownership, not matching names, decides what deployment update/remove may touch."""

import pytest

from agent_sessions.playbooks import destination, materials
from agent_sessions.playbooks import mutation_plan as plan


def material(data=b"Instructions\n", disposition="managed", path="RULES.md"):
    return materials.Material(path, disposition, data=data)


def build(wanted, current, owned=None):
    return plan.build(wanted, current, owned or {}, "deployment-one")


def test_first_apply_update_and_remove_own_exact_whole_file_bytes():
    [created], conflicts = build([material()], {"RULES.md": destination.Node("absent")})
    assert not conflicts and created.action == "create"
    [updated], conflicts = build(
        [material(b"Next\n")], {created.path: created.after}, {created.path: created.ownership}
    )
    assert not conflicts and updated.after.data == b"Next\n"
    [removed], conflicts = build(
        [], {updated.path: updated.after}, {updated.path: updated.ownership}
    )
    assert not conflicts and removed.after.kind == "absent"


@pytest.mark.parametrize("wanted", [[], [material(b"Update")]])
def test_operator_edit_blocks_whole_file_update_and_remove_with_review_content(wanted):
    [created], _ = build([material()], {"RULES.md": destination.Node("absent")})
    changes, conflicts = build(
        wanted,
        {created.path: destination.Node("file", data=b"my edit")},
        {created.path: created.ownership},
    )
    assert changes == []
    assert conflicts[0]["before"]["text"] == "my edit"
    assert "operator edits" in conflicts[0]["reason"]


@pytest.mark.parametrize("path", ["RULES.md", "workflow.yml", "config.toml"])
def test_region_uses_file_comment_syntax_and_preserves_unrelated_operator_edits(path):
    [created], conflicts = build(
        [material(path=path)], {path: destination.Node("file", data=b"Original\n")}
    )
    assert not conflicts and created.ownership["kind"] == "region"
    marker = b"<!-- BattleLab" if path.endswith(".md") else b"# BattleLab"
    assert marker in created.after.data
    edited = destination.Node(
        "file", data=b"Added prefix\n" + created.after.data + b"Added suffix\n"
    )
    [updated], conflicts = build(
        [material(b"Next", path=path)], {path: edited}, {path: created.ownership}
    )
    assert not conflicts
    assert updated.after.data.startswith(b"Added prefix\nOriginal\n")
    [removed], conflicts = build([], {path: updated.after}, {path: updated.ownership})
    assert not conflicts
    assert removed.after.data == b"Added prefix\nOriginal\n\nAdded suffix\n"


@pytest.mark.parametrize("mutation", ["inside", "missing", "duplicate", "reordered"])
def test_region_damage_blocks_update_and_remove(mutation):
    [created], _ = build([material()], {"RULES.md": destination.Node("file", data=b"Original\n")})
    data = created.after.data
    begin, end = plan._markers("RULES.md", "deployment-one")
    damaged = {
        "inside": data.replace(b"Instructions", b"Edited"),
        "missing": data.replace(end, b""),
        "duplicate": data + begin,
        "reordered": end + b"\nInstructions\n" + begin,
    }[mutation]
    for wanted in ([], [material(b"Next")]):
        changes, conflicts = build(
            wanted,
            {"RULES.md": destination.Node("file", data=damaged)},
            {"RULES.md": created.ownership},
        )
        assert not changes and conflicts


def test_reference_adoption_requires_identical_regular_file_and_then_becomes_owned():
    previous = {"RULES.md": {"kind": "reference", "disposition": "reference"}}
    identical = destination.Node("file", data=b"Instructions\n")
    [adopted], conflicts = build([material()], {"RULES.md": identical}, previous)
    assert not conflicts and adopted.action == "keep"
    assert adopted.ownership["kind"] == "file"
    [removed], conflicts = build([], {"RULES.md": identical}, {"RULES.md": adopted.ownership})
    assert not conflicts and removed.action == "remove"
    for invalid in (
        destination.Node("absent"),
        destination.Node("symlink", target="OTHER.md"),
        destination.Node("file", data=b"Changed"),
    ):
        changes, conflicts = build([material()], {"RULES.md": invalid}, previous)
        assert not changes and conflicts


def test_seed_survives_operator_edits_update_and_remove():
    seed = material(disposition="seed")
    [created], _ = build([seed], {seed.path: destination.Node("absent")})
    edited = destination.Node("file", data=b"Project owns me")
    for wanted in ([], [material(b"New seed", "seed")]):
        [kept], conflicts = build(wanted, {seed.path: edited}, {seed.path: created.ownership})
        assert not conflicts and kept.action == "keep" and kept.after == edited


def test_preexisting_matching_instruction_alias_never_becomes_owned():
    alias = materials.Material("AGENTS.md", "managed", target="RULES.md")
    node = destination.Node("symlink", target="RULES.md")
    [kept], conflicts = build([alias], {alias.path: node})
    assert not conflicts and kept.ownership["disposition"] == "reference"
    [removed], conflicts = build([], {alias.path: node}, {alias.path: kept.ownership})
    assert not conflicts and removed.action == "keep"


def test_removed_seed_is_not_recreated_by_an_update():
    seed = material(disposition="seed")
    [created], _ = build([seed], {seed.path: destination.Node("absent")})
    [kept], conflicts = build(
        [seed], {seed.path: destination.Node("absent")}, {seed.path: created.ownership}
    )
    assert not conflicts and kept.action == "keep" and kept.after.kind == "absent"


def test_created_alias_can_update_but_retargeted_alias_blocks_remove():
    alias = materials.Material("AGENTS.md", "managed", target="RULES.md")
    [created], _ = build([alias], {alias.path: destination.Node("absent")})
    changed = destination.Node("symlink", target="OPERATOR.md")
    changes, conflicts = build([], {alias.path: changed}, {alias.path: created.ownership})
    assert not changes and conflicts


def test_binary_whole_file_update_is_exact_but_overlay_cannot_invent_comment_syntax():
    binary = material(b"\x00\xff", path="asset.bin")
    [created], conflicts = build([binary], {binary.path: destination.Node("absent")})
    assert not conflicts
    public = plan.public(created)
    assert public["after"]["base64"] == "AP8=" and public["diff"] is None
    updated = material(b"\x00\xfe", path="asset.bin")
    [change], conflicts = build(
        [updated], {binary.path: created.after}, {binary.path: created.ownership}
    )
    assert not conflicts and change.after.data == b"\x00\xfe"
    changes, conflicts = build([updated], {binary.path: created.after})
    assert not changes and conflicts


def test_expanded_region_is_bounded_after_operator_content_is_combined(monkeypatch):
    monkeypatch.setattr(plan.schema, "MAX_FILE_BYTES", 100)
    changes, conflicts = build(
        [material(b"a" * 50)], {"RULES.md": destination.Node("file", data=b"b" * 50)}
    )
    assert not changes and "size limit" in conflicts[0]["reason"]


def test_an_owned_alias_replaced_by_a_regular_file_is_ownership_loss_not_an_overlay():
    alias = materials.Material("AGENTS.md", "managed", target="RULES.md")
    [created], _ = build([alias], {alias.path: destination.Node("absent")})
    substituted = destination.Node("file", data=b"operator text\n")
    as_file = material(b"Instructions\n", path="AGENTS.md")
    changes, conflicts = build(
        [as_file], {alias.path: substituted}, {alias.path: created.ownership}
    )
    assert changes == [] and "operator edits" in conflicts[0]["reason"]
    # An intact owned alias still cannot change kind in place.
    changes, conflicts = build(
        [as_file], {alias.path: created.after}, {alias.path: created.ownership}
    )
    assert changes == [] and "cannot become a file" in conflicts[0]["reason"]


def test_a_binary_operator_file_never_receives_a_text_region():
    current = destination.Node("file", data=b"key = 1\0binary\n")
    changes, conflicts = build([material(path="config.txt")], {"config.txt": current})
    assert changes == [] and "binary" in conflicts[0]["reason"]


def test_an_owned_file_the_operator_deleted_is_not_recreated_by_an_update():
    [created], _ = build([material()], {"RULES.md": destination.Node("absent")})
    changes, conflicts = build(
        [material(b"Next\n")],
        {"RULES.md": destination.Node("absent")},
        {"RULES.md": created.ownership},
    )
    assert changes == [] and "deleted" in conflicts[0]["reason"]


def test_an_owned_region_whose_file_was_deleted_is_not_recreated():
    current = destination.Node("file", data=b"Operator\n")
    [created], _ = build([material()], {"RULES.md": current})
    assert created.ownership["kind"] == "region"
    changes, conflicts = build(
        [material()], {"RULES.md": destination.Node("absent")}, {"RULES.md": created.ownership}
    )
    assert changes == [] and "deleted" in conflicts[0]["reason"]


def test_an_owned_alias_the_operator_deleted_is_not_recreated():
    alias = materials.Material("AGENTS.md", "managed", target="RULES.md")
    [created], _ = build([alias], {alias.path: destination.Node("absent")})
    changes, conflicts = build(
        [alias], {alias.path: destination.Node("absent")}, {alias.path: created.ownership}
    )
    assert changes == [] and "deleted" in conflicts[0]["reason"]


def test_removing_an_already_deleted_owned_file_is_a_no_op():
    [created], _ = build([material()], {"RULES.md": destination.Node("absent")})
    [change], conflicts = build(
        [], {"RULES.md": destination.Node("absent")}, {"RULES.md": created.ownership}
    )
    assert not conflicts and change.action == "keep"


def test_a_recorded_inode_makes_a_same_byte_substitute_ownership_loss():
    [created], _ = build([material()], {"RULES.md": destination.Node("absent")})
    owned = {**created.ownership, "inode": [1, 42]}
    ours = destination.Node("file", (1, 42, 0, 0), data=b"Instructions\n")
    [kept], conflicts = build([material()], {"RULES.md": ours}, {"RULES.md": owned})
    assert not conflicts and kept.action == "keep"
    theirs = destination.Node("file", (1, 99, 0, 0), data=b"Instructions\n")
    changes, conflicts = build([material()], {"RULES.md": theirs}, {"RULES.md": owned})
    assert changes == [] and "operator edits" in conflicts[0]["reason"]
