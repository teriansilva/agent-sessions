"""Every present engine reads the playbook's instructions (§11), driven by a fixture roster."""

import os

import pytest

from agent_sessions import projects
from agent_sessions.playbooks import apply, instructions, lifecycle, remove, review, store
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup
#: Made-up engines: the derivation names none in code, only what a manifest declares.
ROSTER = {"alpha": ["AGENTS.md"], "beta": ["CLAUDE.md", "GEMINI.md"]}


def _instr_files(extra_material=None):
    contents = files()
    contents["playbook.toml"] = (
        contents["playbook.toml"]
        .replace('id = "review-demo"', 'id = "instr-demo"')
        .replace('path = "RULES.md"', 'path = "CLAUDE.md"')
    )
    contents["template/CLAUDE.md"] = contents.pop("template/RULES.md")
    if extra_material:
        contents["playbook.toml"] += extra_material
    return contents


def _install(contents, pid="instr-demo"):
    tree = store.tree_from_files(contents)
    for relative, data in tree.files.items():
        path = store.local_root() / pid / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree.digest()


@pytest.fixture
def roster(monkeypatch):
    current = {k: list(v) for k, v in ROSTER.items()}
    monkeypatch.setattr(instructions, "present", lambda: current)
    return current


@pytest.fixture
def deploy(setup, roster):
    folder, body = setup
    revision = _install(_instr_files())
    entity = projects.create("Instr project", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        **body,
        "revision": revision,
        "destination": str(folder),
        "bindings": [],
        "project_id": entity.id,
    }

    def bind_and_apply(op_suffix="1"):
        plan = review.build("instr-demo", inputs, key=KEY)
        receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
        bound = lifecycle.bind(
            entity.id, "instr-demo", inputs, receipt["receipt"], f"bind-op-{op_suffix}0000", key=KEY
        )
        return apply.apply(entity.id, f"apply-op-{op_suffix}0000", bound["receipt"], key=KEY)

    return folder, entity, inputs, bind_and_apply


def test_each_present_engines_file_is_an_alias_to_the_one_instruction_material(deploy):
    folder, entity, inputs, bind_and_apply = deploy
    plan = review.build("instr-demo", inputs, key=KEY).public
    aliases = {m["path"]: m["after"]["target"] for m in plan["materials"] if "target" in m["after"]}
    assert aliases == {"AGENTS.md": "CLAUDE.md", "GEMINI.md": "CLAUDE.md"}
    bind_and_apply()
    assert (folder / "CLAUDE.md").read_text().startswith("Check ")
    for name in ("AGENTS.md", "GEMINI.md"):
        assert os.readlink(folder / name) == "CLAUDE.md"
    assert apply.status(entity.id)["instructions_missing"] == []


def test_a_roster_change_after_review_invalidates_the_review(deploy, roster):
    folder, entity, inputs, _ = deploy
    plan = review.build("instr-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    roster["gamma"] = ["GEMINI.md", "AGENTS.md"]  # same files, a new engine: still changes facts
    with pytest.raises(store.StoreError) as refused:
        lifecycle.bind(
            entity.id, "instr-demo", inputs, receipt["receipt"], "bind-op-10000", key=KEY
        )
    assert "review" in str(refused.value) or "changed" in str(refused.value), refused.value


def test_an_engine_installed_later_is_reported_and_reapply_fixes_it(deploy, roster):
    folder, entity, inputs, bind_and_apply = deploy
    roster.clear()
    roster["alpha"] = ["AGENTS.md"]
    bind_and_apply()
    assert not (folder / "GEMINI.md").exists()
    roster["beta"] = ["CLAUDE.md", "GEMINI.md"]  # installed after apply
    assert apply.status(entity.id)["instructions_missing"] == ["beta"]
    bind_and_apply("2")
    assert os.readlink(folder / "GEMINI.md") == "CLAUDE.md"
    assert apply.status(entity.id)["instructions_missing"] == []


def test_removal_takes_the_derived_aliases_with_it(deploy):
    folder, entity, _, bind_and_apply = deploy
    bind_and_apply()
    plan = remove.plan(entity.id, key=KEY)
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert not any((folder / n).is_symlink() for n in ("AGENTS.md", "GEMINI.md"))
    assert not (folder / "CLAUDE.md").exists()


def test_a_preexisting_matching_alias_is_used_but_never_owned(deploy):
    folder, entity, _, bind_and_apply = deploy
    os.symlink("CLAUDE.md", folder / "AGENTS.md")
    bind_and_apply()
    assert record(entity.id)["files"]["AGENTS.md"]["disposition"] == "reference"
    plan = remove.plan(entity.id, key=KEY)
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert os.readlink(folder / "AGENTS.md") == "CLAUDE.md"  # the operator's alias stays


def test_no_or_ambiguous_instruction_material_derives_nothing(setup, roster):
    # review-demo: its one material, RULES.md, is not an instruction file.
    plan = review.build("review-demo", setup[1], key=KEY).public
    assert not any("target" in m["after"] for m in plan["materials"])
    two = {
        "materials": [
            {"path": "CLAUDE.md", "kind": "file", "disposition": "managed"},
            {"path": "AGENTS.md", "kind": "file", "disposition": "managed"},
        ]
    }
    assert instructions.material(two) is None
    assert instructions.include([], two, roster) == []


def test_a_declared_file_or_alias_is_never_replaced_by_a_derived_one(roster):
    from agent_sessions.playbooks import materials

    bundle = {
        "materials": [
            {"path": "CLAUDE.md", "kind": "file", "disposition": "managed"},
            {"path": "AGENTS.md", "kind": "instruction-alias", "disposition": "managed"},
        ]
    }
    declared = [
        materials.Material("CLAUDE.md", "managed", data=b"x"),
        materials.Material("AGENTS.md", "managed", target="CLAUDE.md"),
    ]
    out = instructions.include(declared, bundle, roster)
    assert [m.path for m in out] == ["CLAUDE.md", "AGENTS.md", "GEMINI.md"]
    assert out[2].target == "CLAUDE.md"


def _maximal_files():
    from agent_sessions.playbooks import schema

    contents = _instr_files()
    contents.pop("flows/check.toml")  # no flows: no generated flow document
    extra = schema.MAX_MATERIALS - 1  # plus CLAUDE.md = exactly the limit
    for i in range(extra):
        contents["playbook.toml"] += (
            f'\n[[materials]]\npath = "notes/n{i:03d}.md"\ndisposition = "managed"\n'
        )
        contents[f"template/notes/n{i:03d}.md"] = f"note {i}\n"
    return contents


def test_a_maximal_bundle_refuses_clearly_when_an_engine_needs_one_more_alias(setup, monkeypatch):
    folder, body = setup
    revision = _install(_maximal_files())
    inputs = {**body, "revision": revision, "destination": str(folder), "bindings": []}
    monkeypatch.setattr(instructions, "present", lambda: {})
    assert len(review.build("instr-demo", inputs, key=KEY).public["materials"]) == 200
    monkeypatch.setattr(instructions, "present", lambda: {"alpha": ["AGENTS.md"]})
    with pytest.raises(store.StoreError, match="200-material limit") as refused:
        review.build("instr-demo", inputs, key=KEY)
    assert "AGENTS.md" in str(refused.value) and refused.value.status == 422


@pytest.mark.parametrize(
    "agents, expected",
    [
        (None, ["alpha"]),  # absent
        ("file", ["alpha"]),  # an unrelated operator file
        ("OTHER.md", ["alpha"]),  # a link to something else
        ("CLAUDE.md", []),  # a real alias to the instruction material
    ],
)
def test_missing_counts_only_instruction_files_verified_on_disk(setup, agents, expected):
    from dataclasses import asdict

    from agent_sessions.playbooks import destination, materials

    folder, _ = setup
    (folder / "CLAUDE.md").write_text("instructions\n")
    if agents == "file":
        (folder / "AGENTS.md").write_text("the operator's own notes\n")
    elif agents is not None:
        os.symlink(agents, folder / "AGENTS.md")
    record_ = {
        "destination": asdict(destination.review_folder(str(folder))),
        "files": {
            "CLAUDE.md": {
                "kind": "file",
                "disposition": "managed",
                "digest": materials.digest(b"instructions\n"),
            },
            "AGENTS.md": {"kind": "reference", "disposition": "reference"},
        },
    }
    assert instructions.missing(record_, {"alpha": ["AGENTS.md"]}) == expected


def test_an_unreadable_destination_reports_every_present_engine(setup):
    folder, _ = setup
    record_ = {
        "destination": {"path": str(folder / "gone"), "device": 0, "inode": 0},
        "files": {"CLAUDE.md": {"kind": "file", "disposition": "managed", "digest": "0" * 64}},
    }
    assert instructions.missing(record_, ROSTER) == ["alpha", "beta"]


@pytest.mark.parametrize("change", ["replaced", "deleted"])
def test_a_replaced_or_deleted_source_covers_no_engine(deploy, change):
    folder, entity, _, bind_and_apply = deploy
    bind_and_apply()
    assert apply.status(entity.id)["instructions_missing"] == []
    if change == "replaced":
        (folder / "CLAUDE.md").write_text("the operator's unrelated text\n")
    else:
        os.unlink(folder / "CLAUDE.md")
    # The links still point at CLAUDE.md, but it is no longer the deployment's instructions.
    assert apply.status(entity.id)["instructions_missing"] == ["alpha", "beta"]


def test_an_alias_rotation_past_the_material_limit_is_refused_clearly(setup, roster):
    """One hard limit over the full union (rendered + previously owned), never worked around."""
    folder, body = setup
    contents = _maximal_files()
    # 198 notes + CLAUDE.md = 199 declared; one derived alias makes 200 rendered.
    contents["playbook.toml"] = contents["playbook.toml"].replace(
        '\n[[materials]]\npath = "notes/n197.md"\ndisposition = "managed"\n', ""
    )
    contents.pop("template/notes/n197.md")
    revision = _install(contents)
    entity = projects.create("Full project", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        **body,
        "revision": revision,
        "destination": str(folder),
        "bindings": [],
        "project_id": entity.id,
    }
    roster.clear()
    roster["alpha"] = ["AGENTS.md"]
    plan = review.build("instr-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)
    bound = lifecycle.bind(
        entity.id, "instr-demo", inputs, receipt["receipt"], "bind-op-10000", key=KEY
    )
    apply.apply(entity.id, "apply-op-10000", bound["receipt"], key=KEY)
    roster.clear()
    roster["gamma"] = ["GEMINI.md"]  # 200 rendered + 200 owned = 201 distinct paths
    with pytest.raises(store.StoreError, match="would touch 201 paths") as refused:
        review.build("instr-demo", inputs, key=KEY)
    assert refused.value.status == 422 and "remove a material" in str(refused.value)
    assert os.readlink(folder / "AGENTS.md") == "CLAUDE.md"  # nothing changed
    assert apply.status(entity.id)["instructions_missing"] == ["gamma"]


def test_a_replaced_managed_alias_with_the_same_target_is_not_coverage(deploy):
    folder, entity, _, bind_and_apply = deploy
    bind_and_apply()
    assert record(entity.id)["files"]["AGENTS.md"]["inode"]  # an owned, inode-proven alias
    staged = folder / "AGENTS.tmp"
    os.symlink("CLAUDE.md", staged)
    os.replace(staged, folder / "AGENTS.md")  # same target, a different inode
    assert apply.status(entity.id)["instructions_missing"] == ["alpha"]
