"""The playbook bundle format, flow schema and validator (#1190, #1096 P1).

Every rule in the issue has its own negative here, each mutating ONE thing in a copy of a valid
fixture — and `_edit` refuses an edit whose anchor is missing, so a test can never pass because
the mutation silently did not happen.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from agent_sessions import missions, playbooks
from agent_sessions.playbooks import schema, tree, validate
from agent_sessions.playbooks.errors import PlaybookFormatError

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks"
DEV = "forge-workflow"
NONDEV = "inbox-triage"


@pytest.fixture
def dev(tmp_path: Path) -> Path:
    dst = tmp_path / "src" / DEV
    shutil.copytree(FIXTURES / DEV, dst)
    return dst


@pytest.fixture
def nondev(tmp_path: Path) -> Path:
    dst = tmp_path / "src" / NONDEV
    shutil.copytree(FIXTURES / NONDEV, dst)
    return dst


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"test anchor {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


def _refused(bundle: Path, *needles: str) -> str:
    with pytest.raises(PlaybookFormatError) as e:
        playbooks.load_bundle(bundle)
    msg = str(e.value)
    for needle in needles:
        assert needle in msg, msg
    return msg


def _flow(b: Path) -> Path:
    return b / "flows" / "dev.toml"


def _manifest(b: Path) -> Path:
    return b / "playbook.toml"


# --- the valid fixtures --------------------------------------------------------------------------


def test_dev_fixture_loads_with_rework_and_outputs():
    pb = playbooks.load_bundle(FIXTURES / DEV)
    flow = pb["flows"]["dev"]
    steps = {s["id"]: s for s in flow["steps"]}
    assert list(steps) == ["plan", "implement", "review", "merge", "deploy", "retro"]
    assert steps["review"]["rework"] == {
        "to": "implement",
        "when": "review_approved",
        "max_rounds": 5,
    }
    assert steps["implement"]["outputs"] == ["pr_number", "head_branch", "head_sha"]
    # The producer -> consumer bridge: review's forge_review reads implement's observed branch.
    review_args = steps["review"]["checklist"][0]["probe_args"]
    assert review_args["branch"] == "{{steps.implement.head_branch}}"
    assert steps["review"]["distinct_from"] == [{"step": "implement", "constraint": "session"}]
    assert steps["review"]["actor"] == {"kind": "external", "label": "Reviewer bot"}
    assert steps["implement"]["actor"] == {"kind": "agent", "engine": "claude", "model": "default"}
    assert validate.is_note(steps["retro"]) and not validate.is_note(steps["deploy"])
    assert pb["default_flow"] == "dev"
    alias = next(m for m in pb["materials"] if m["path"] == "AGENTS.md")
    assert alias == {
        "path": "AGENTS.md",
        "kind": "instruction-alias",
        "disposition": "managed",
        "target": "CLAUDE.md",
    }
    assert pb["runbooks"]["audit"]["caps"] == {"iterations": 3, "wall_clock_minutes": 30}
    assert pb["rituals"] == [{"name": "nightly-audit", "runbook": "audit", "schedule": "daily"}]


def test_nondev_fixture_has_no_connections_and_only_judged_items():
    pb = playbooks.load_bundle(FIXTURES / NONDEV)
    assert pb["connections"] == []
    assert pb["identity"]["domain"] == "mail"
    probes = {i["probe"] for s in pb["flows"]["triage"]["steps"] for i in s["checklist"]}
    assert probes == {"supervisor_judged"}


def test_every_fixture_variable_type_round_trips():
    pb = playbooks.load_bundle(FIXTURES / DEV)
    by = {v["name"]: v for v in pb["variables"]}
    assert by["healthy_status"]["default"] == 200 and by["healthy_status"]["type"] == "int"
    assert by["forge_token"]["kind"] == "secret" and by["forge_token"]["default"] is None
    assert pb["connections"][0]["credential"] == "forge_token"


# --- listing: fail-soft per bundle ---------------------------------------------------------------


def test_one_bad_bundle_disables_itself_and_the_listing_returns_the_rest(dev, nondev):
    src = dev.parent
    broken = src / "broken-one"
    shutil.copytree(FIXTURES / DEV, broken)
    _edit(_manifest(broken), 'id = "forge-workflow"', 'id = "broken-one"')
    _edit(_flow(broken), 'after = ["plan"]', 'after = ["nope"]')
    cards = {c["id"]: c for c in playbooks.list_bundles(src)}
    assert set(cards) == {"broken-one", DEV, NONDEV}
    assert cards["broken-one"]["ok"] is False
    assert "unknown step 'nope'" in cards["broken-one"]["error"]
    assert cards[DEV]["ok"] is True and cards[NONDEV]["ok"] is True
    assert cards[DEV]["flows"][0]["steps"][2]["actor"]["kind"] == "external"


def test_listing_survives_a_symlink_and_a_stray_file_at_the_source_root(dev):
    src = dev.parent
    os.symlink(dev, src / "linked")
    (src / "stray.txt").write_text("x")
    cards = {c["id"]: c for c in playbooks.list_bundles(src)}
    assert cards[DEV]["ok"] is True
    assert cards["linked"]["ok"] is False
    assert cards["stray.txt"]["ok"] is False


def test_missing_source_lists_nothing(tmp_path):
    assert playbooks.list_bundles(tmp_path / "absent") == []


def test_bundled_source_is_the_package_dir():
    assert playbooks.BUNDLED_ROOT.name == "bundled"
    assert isinstance(playbooks.list_bundles(), list)


def test_directory_name_must_equal_identity_id(tmp_path):
    dst = tmp_path / "other-name"
    shutil.copytree(FIXTURES / DEV, dst)
    _refused(dst, "identity.id", "directory name")


# --- unknown-field policy and format -------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "old", "new", "needle"),
    [
        ("playbook.toml", "format = 1\n", "format = 1\nextra = 1\n", "extra"),
        ("playbook.toml", 'domain = "development"', 'domain = "development"\nlogo = "x"', "logo"),
        ("playbook.toml", 'name = "repo"\n', 'name = "repo"\nhint = "x"\n', "hint"),
        (
            "playbook.toml",
            "orchestrator_input = true",
            "orchestrator_input = true\nroot = true",
            "root",
        ),
        ("flows/dev.toml", 'id = "plan"', 'id = "plan"\nowner = "x"', "owner"),
        ("flows/dev.toml", 'key = "issue_ready"', 'key = "issue_ready"\ngate = true', "gate"),
        (
            "flows/dev.toml",
            'engine = "claude", model',
            'engine = "claude", flags = "-x", model',
            "flags",
        ),
        ("flows/dev.toml", "max_rounds = 5", "max_rounds = 5\nforever = true", "forever"),
        (
            "runbooks/audit.md",
            'trigger = "ritual"',
            'trigger = "ritual"\ncron = "* * * * *"',
            "cron",
        ),
        ("templates/start.toml", 'tags = ["dev"]', 'tags = ["dev"]\nid = "x"', "id"),
    ],
)
def test_unknown_fields_are_rejected_at_every_level(dev, rel, old, new, needle):
    _edit(dev / rel, old, new)
    _refused(dev, needle, "unknown field")


@pytest.mark.parametrize(
    ("value", "needle"),
    [("2", "needs a newer BattleLab"), ("0", "not supported"), ("true", "must be the integer")],
)
def test_manifest_format_policy(dev, value, needle):
    _edit(_manifest(dev), "format = 1\n", f"format = {value}\n")
    _refused(dev, "playbook.toml: format", needle)


def test_flow_format_is_required(dev):
    _edit(_flow(dev), "format = 1\n", "")
    _refused(dev, "flows/dev.toml: format")


def test_unknown_root_entry_is_refused(dev):
    (dev / "scripts").mkdir()
    _refused(dev, "scripts", "not part of the bundle format")


def test_a_non_toml_file_in_flows_is_refused(dev):
    (dev / "flows" / "notes.txt").write_text("x")
    _refused(dev, "flows/notes.txt")


# --- the flow graph ------------------------------------------------------------------------------


def test_cycle_is_refused(dev):
    _edit(_flow(dev), 'id = "plan"\n', 'id = "plan"\nafter = ["merge"]\n')
    _refused(dev, "cycle")


def test_self_prerequisite_is_refused(dev):
    _edit(_flow(dev), 'after = ["plan"]', 'after = ["implement"]')
    _refused(dev, "after itself")


def test_rework_to_a_non_ancestor_is_refused(dev):
    _edit(_flow(dev), 'to = "implement"', 'to = "deploy"')
    _refused(dev, "rework.to", "not an ancestor")


def test_rework_to_an_unknown_step_is_refused(dev):
    _edit(_flow(dev), 'to = "implement"', 'to = "ghost"')
    _refused(dev, "rework.to", "unknown step")


def test_rework_when_must_name_an_item_of_its_own_step(dev):
    _edit(_flow(dev), 'when = "review_approved"', 'when = "pr_open"')
    _refused(dev, "rework.when")


def test_rework_is_the_only_back_reference_so_after_cannot_point_down(dev):
    # `after` pointing at a descendant is a cycle, not a second kind of back-edge.
    _edit(_flow(dev), 'after = ["review"]', 'after = ["review", "deploy"]')
    _refused(dev, "cycle")


def test_note_cannot_be_a_prerequisite(dev):
    _append(
        _flow(dev),
        '\n[[steps]]\nid = "after_note"\ntitle = "x"\nactor = { kind = "operator" }\n'
        'after = ["retro"]\n',
    )
    _refused(dev, "'retro' is a note and cannot be a prerequisite")


def test_note_cannot_be_a_rework_target(dev):
    _edit(_flow(dev), 'to = "implement"', 'to = "retro"')
    _refused(dev, "rework.to", "is a note")


def test_duplicate_step_id_is_refused(dev):
    _edit(_flow(dev), 'id = "retro"', 'id = "deploy"')
    _refused(dev, "duplicate step id 'deploy'")


def test_duplicate_item_key_is_refused(dev):
    _edit(_flow(dev), 'key = "merged"', 'key = "pr_open"')
    _refused(dev, "duplicate item key 'pr_open'")


@pytest.mark.parametrize("key", ["note_x", "goal_x", "done_as_instructed", "a--2"])
def test_reserved_item_keys_are_refused(dev, key):
    _edit(_flow(dev), 'key = "merged"', f'key = "{key}"')
    _refused(dev, "reserved")


def test_duplicate_variable_and_material_are_refused(dev):
    _edit(_manifest(dev), 'name = "repo_name"', 'name = "repo"')
    _refused(dev, "duplicate variable 'repo'")


def test_duplicate_material_path_is_refused(dev):
    _edit(_manifest(dev), 'path = "docs/mission.md"', 'path = "CLAUDE.md"')
    _refused(dev, "duplicate material 'CLAUDE.md'")


def test_unknown_probe_kind_is_refused(dev):
    _edit(_flow(dev), 'probe = "forge_merged"', 'probe = "shell"')
    _refused(dev, "unknown probe kind 'shell'")


def test_actor_kind_is_closed(dev):
    _edit(_flow(dev), 'actor = { kind = "operator" }', 'actor = { kind = "script" }')
    _refused(dev, "actor.kind")


def test_engine_and_model_are_shape_checked_but_not_resolved(dev):
    # A well-formed engine this host does not have is UNRESOLVED (P2/P5), not an error.
    _edit(
        _flow(dev), 'engine = "claude", model = "default"', 'engine = "future-agent", model = "m-9"'
    )
    pb = playbooks.load_bundle(dev)
    assert pb["flows"]["dev"]["steps"][0]["actor"]["engine"] == "future-agent"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('engine = "claude", model = "default"', 'engine = "claude; rm", model = "default"'),
        ('engine = "claude", model = "default"', 'engine = "claude", model = "--dangerous flag"'),
    ],
)
def test_malformed_engine_or_model_reference_is_refused(dev, old, new):
    _edit(_flow(dev), old, new)
    _refused(dev, "actor")


def test_memory_mode_is_closed(dev):
    _edit(_flow(dev), 'memory = "read-write"', 'memory = "everything"')
    _refused(dev, "memory")


def test_skills_are_advisory_references(dev):
    _edit(
        _flow(dev),
        'memory = "read-write"',
        'memory = "read-write"\n'
        'skills = [{ id = "code-review", revision = "1.2", required = true }]',
    )
    pb = playbooks.load_bundle(dev)
    implement = pb["flows"]["dev"]["steps"][1]
    assert implement["skills"] == [{"id": "code-review", "revision": "1.2", "required": True}]


# --- probe arguments: symbolic checks at load ----------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('probe_args = { repo = "{{repo}}" }', 'probe_args = { repo = "acme/widgets" }'),
        (
            'probe_args = { url = "{{deploy_url}}", expect_status',
            'probe_args = { url = "https://internal.example.com/", expect_status',
        ),
        (
            'branch = "{{steps.implement.head_branch}}" }\n\n[steps.rework]',
            'branch = "main" }\n\n[steps.rework]',
        ),
    ],
    ids=["repo", "url", "branch"],
)
def test_literal_in_a_target_bearing_argument_is_refused(dev, old, new):
    _edit(_flow(dev), old, new)
    _refused(dev, "target-bearing", "never supplies a literal probe target")


def test_literal_workflow_is_target_bearing_too(dev):
    _append(
        _flow(dev),
        '\n[[steps]]\nid = "ci"\ntitle = "x"\nactor = { kind = "none" }\nafter = ["merge"]\n'
        '[[steps.checklist]]\nkey = "ci_green"\ntitle = "x"\nprobe = "forge_run"\n'
        'probe_args = { workflow = "deploy.yml" }\n',
    )
    _refused(dev, "workflow", "target-bearing")


def test_partial_interpolation_is_refused(dev):
    _edit(
        _flow(dev), 'probe_args = { repo = "{{repo}}" }', 'probe_args = { repo = "{{repo}}-fork" }'
    )
    _refused(dev, "exactly one reference")


def test_literal_expectation_is_checked_by_the_probe_arguments_own_contract(dev):
    _edit(_flow(dev), 'expect_status = "{{healthy_status}}"', "expect_status = 204")
    playbooks.load_bundle(dev)  # a literal expectation chooses no target: admitted
    _edit(_flow(dev), "expect_status = 204", "expect_status = 999")
    _refused(dev, "expect_status", "HTTP status 100-599")


def test_bool_literal_is_not_an_int_argument(dev):
    _edit(_flow(dev), 'expect_status = "{{healthy_status}}"', "expect_status = true")
    _refused(dev, "expect_status", "must be an integer")


def test_unknown_variable_in_a_probe_argument_is_refused(dev):
    _edit(
        _flow(dev), 'probe_args = { repo = "{{repo}}" }', 'probe_args = { repo = "{{repository}}" }'
    )
    _refused(dev, "undeclared variable {{repository}}")


def test_missing_required_argument_is_refused(dev):
    _edit(
        _flow(dev),
        'probe_args = { url = "{{deploy_url}}", expect_status',
        "probe_args = { expect_status",
    )
    _refused(dev, "probe http_status requires url")


def test_unknown_argument_key_is_refused(dev):
    _edit(
        _flow(dev),
        'probe_args = { repo = "{{repo}}" }',
        'probe_args = { repo = "{{repo}}", host = "{{repo}}" }',
    )
    _refused(dev, "does not take host")


def test_optional_arguments_may_be_absent(dev):
    # forge_pr's `repo` is optional in PROBE_ARG_SCHEMA, so an item without it is valid — the
    # symbolic check treats optional keys exactly as `missions.validate_probe_args` does.
    assert missions.PROBE_ARG_SCHEMA["forge_pr"]["repo"][0] is False
    _edit(
        _flow(dev), 'probe = "forge_pr"\nprobe_args = { repo = "{{repo}}" }', 'probe = "forge_pr"'
    )
    playbooks.load_bundle(dev)


@pytest.mark.parametrize(
    ("variable", "arg_line", "needle"),
    [
        # A text variable where an int is expected.
        ("repo", 'expect_status = "{{repo}}"', "takes a int value; {{repo}} is a text variable"),
        # bool-as-int: a bool variable is never an integer argument.
        ("flag", 'expect_status = "{{flag}}"', "{{flag}} is a bool variable"),
        # An int variable where text is expected.
        ("healthy_status", 'expect = "{{healthy_status}}"', "{{healthy_status}} is a int variable"),
        # A url variable is not a repository; a text variable is not a url.
        ("deploy_url", 'expect = "{{deploy_url}}"', "{{deploy_url}} is a url variable"),
        (
            "repo",
            'expect = "x", url = "{{repo}}"',
            "takes a url value; {{repo}} is a text variable",
        ),
    ],
)
def test_variable_type_must_fit_the_argument(dev, variable, arg_line, needle):
    _append(_manifest(dev), '\n[[variables]]\nname = "flag"\ntype = "bool"\ndefault = true\n')
    # `[[variables]]` after `[flows]` would be a flows sub-table in TOML; re-home the flows block.
    _edit(_manifest(dev), '[flows]\ndefault = "dev"\n', "")
    _append(_manifest(dev), '\n[flows]\ndefault = "dev"\n')
    if "expect_status" in arg_line:
        _edit(_flow(dev), 'expect_status = "{{healthy_status}}"', arg_line)
    elif "url =" in arg_line:
        _edit(
            _flow(dev), 'url = "{{deploy_url}}", expect = "{{steps.implement.head_sha}}"', arg_line
        )
    else:
        _edit(_flow(dev), 'expect = "{{steps.implement.head_sha}}"', arg_line)
    _refused(dev, needle)


def test_int_variable_default_is_never_a_bool(dev):
    _edit(_manifest(dev), "default = 200", "default = true")
    _refused(dev, "variables[5].default", "must be an integer (not a boolean)")


def test_max_rounds_bool_is_not_an_int(dev):
    _edit(_flow(dev), "max_rounds = 5", "max_rounds = true")
    _refused(dev, "max_rounds", "must be an integer")


@pytest.mark.parametrize("value", ["0", "11"])
def test_max_rounds_is_bounded(dev, value):
    _edit(_flow(dev), "max_rounds = 5", f"max_rounds = {value}")
    _refused(dev, "max_rounds", "[1, 10]")


def test_runbook_cap_bool_is_not_an_int(dev):
    _edit(dev / "runbooks" / "audit.md", "iterations = 3", "iterations = true")
    _refused(dev, "runbooks/audit.md", "iterations")


# --- step outputs and {{steps.<id>.<slot>}} ------------------------------------------------------


def test_reference_to_an_unknown_producer_is_refused(dev):
    _edit(
        _flow(dev),
        'branch = "{{steps.implement.head_branch}}" }\n\n[steps.rework]',
        'branch = "{{steps.build.head_branch}}" }\n\n[steps.rework]',
    )
    _refused(dev, "unknown step 'build'")


def test_reference_to_an_undeclared_slot_is_refused(dev):
    _edit(
        _flow(dev),
        'outputs = ["pr_number", "head_branch", "head_sha"]',
        'outputs = ["pr_number", "head_branch"]',
    )
    _refused(dev, "does not declare the output 'head_sha'")


def test_a_slot_the_producers_kinds_cannot_yield_is_refused(dev):
    # plan's checklist is supervisor_judged only: it observes nothing a slot could hold.
    _edit(_flow(dev), 'id = "plan"\n', 'id = "plan"\noutputs = ["pr_number"]\n')
    _refused(dev, "outputs", "'pr_number' is not produced by any probe kind")


def test_git_local_yields_branch_but_forge_pr_does_not(dev):
    _edit(_flow(dev), 'outputs = ["pr_number", "head_branch", "head_sha"]', 'outputs = ["branch"]')
    _refused(dev, "'branch' is not produced")


def test_a_producer_that_is_not_an_ancestor_is_refused(dev):
    # plan comes BEFORE implement; it cannot read implement's output.
    _edit(
        _flow(dev),
        'probe = "supervisor_judged"\n\n[[steps]]\nid = "implement"',
        'probe = "supervisor_judged"\n\n[[steps.checklist]]\nkey = "early"\ntitle = "x"\n'
        'probe = "forge_checks"\nprobe_args = { branch = "{{steps.implement.head_branch}}" }\n\n'
        '[[steps]]\nid = "implement"',
    )
    _refused(dev, "step 'implement' is not an ancestor of 'plan'")


def test_a_step_cannot_read_its_own_output(dev):
    _edit(
        _flow(dev),
        'probe = "forge_checks"\nprobe_args = { repo = "{{repo}}" }',
        'probe = "forge_checks"\nprobe_args = { branch = "{{steps.implement.head_branch}}" }',
    )
    _refused(dev, "not an ancestor of 'implement'")


@pytest.mark.parametrize(
    ("ref", "needle"),
    [
        ("{{steps.implement.pr_number}}", "branch does not take a pr_number output"),
        ("{{steps.implement.head_sha}}", "branch does not take a sha output"),
    ],
)
def test_slot_type_must_fit_the_argument(dev, ref, needle):
    _edit(
        _flow(dev),
        'branch = "{{steps.implement.head_branch}}" }\n\n[steps.rework]',
        f'branch = "{ref}" }}\n\n[steps.rework]',
    )
    _refused(dev, needle)


def test_a_step_output_never_fills_the_authority_arguments(dev):
    _edit(
        _flow(dev),
        'probe_args = { repo = "{{repo}}", branch = "{{steps.implement.head_branch}}" }\n\n'
        "[steps.rework]",
        'probe_args = { repo = "{{steps.implement.head_branch}}" }\n\n[steps.rework]',
    )
    _refused(dev, "repo does not take a branch output")


def test_a_step_output_is_not_prose(dev):
    _edit(
        _flow(dev),
        'title = "Merge"\n',
        'title = "Merge"\nbrief = "Merge {{steps.implement.pr_number}}"\n',
    )
    _refused(dev, "brief", "only be referenced from a probe argument")


# --- distinct_from -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ('step = "implement"\nconstraint', 'step = "ghost"\nconstraint', "unknown step 'ghost'"),
        ('step = "implement"\nconstraint', 'step = "review"\nconstraint', "distinct from itself"),
        ('constraint = "session"', 'constraint = "machine"', "constraint"),
    ],
    ids=["unknown", "self", "bad-constraint"],
)
def test_distinct_from_shape(dev, old, new, needle):
    _edit(_flow(dev), old, new)
    _refused(dev, "distinct_from", needle)


def test_distinct_from_duplicate_is_refused(dev):
    _edit(
        _flow(dev),
        'constraint = "session"\n',
        'constraint = "session"\n\n[[steps.distinct_from]]\n'
        'step = "implement"\nconstraint = "session"\n',
    )
    _refused(dev, "duplicate distinct_from")


# --- bounded counts ------------------------------------------------------------------------------


def _steps(n: int) -> str:
    out = ['format = 1\ntitle = "big"\n']
    for i in range(n):
        out.append(f'\n[[steps]]\nid = "s{i}"\ntitle = "x"\nactor = {{ kind = "operator" }}\n')
    return "".join(out)


def test_more_than_30_steps_is_refused(dev):
    _flow(dev).write_text(_steps(schema.MAX_STEPS + 1))
    _edit(_manifest(dev), '[flows]\ndefault = "dev"\n', "")
    _refused(dev, "steps", "more than 30")


def test_exactly_30_steps_is_accepted(dev):
    _flow(dev).write_text(_steps(schema.MAX_STEPS))
    playbooks.load_bundle(dev)


def test_more_than_12_items_is_refused(dev):
    items = "".join(
        f'\n[[steps.checklist]]\nkey = "k{i}"\ntitle = "x"\nprobe = "supervisor_judged"\n'
        for i in range(schema.MAX_ITEMS_PER_STEP + 1)
    )
    _flow(dev).write_text(
        'format = 1\ntitle = "x"\n[[steps]]\nid = "a"\ntitle = "x"\nactor = { kind = "operator" }\n'
        + items
    )
    _refused(dev, "checklist", "more than 12")


def test_more_than_20_flows_is_refused(dev):
    for i in range(schema.MAX_FLOWS):
        (dev / "flows" / f"f{i}.toml").write_text(_steps(1))
    _refused(dev, "flows", "more than 20 files")


# --- secrets -------------------------------------------------------------------------------------


def test_secret_interpolated_in_material_is_rejected(dev):
    _append(dev / "template" / "CLAUDE.md", "\nThe forge token is {{forge_token}}.\n")
    _refused(dev, "template/CLAUDE.md", "interpolates the secret variable {{forge_token}}")


def test_secret_token_is_refused_even_in_a_verbatim_material(dev):
    _append(dev / "template" / ".forgejo" / "workflows" / "pr-validate.yml", "# {{forge_token}}\n")
    _refused(dev, "pr-validate.yml", "secret variable {{forge_token}}")


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("runbooks/audit.md", "on {{repo}}.", "on {{repo}} with {{forge_token}}."),
        ("templates/start.toml", "in {{repo}}", "in {{repo}} with {{forge_token}}"),
        ("flows/dev.toml", "sections.", "sections, using {{forge_token}}."),
        ("README.md", "verify live.", "verify live with {{forge_token}}."),
    ],
    ids=["runbook", "template", "brief", "readme"],
)
def test_secret_is_never_rendered_into_any_bundle_text(dev, rel, old, new):
    _edit(dev / rel, old, new)
    _refused(dev, "secret variable {{forge_token}}")


def test_secret_is_never_a_probe_argument(dev):
    _edit(
        _flow(dev),
        'probe_args = { repo = "{{repo}}" }',
        'probe_args = { repo = "{{forge_token}}" }',
    )
    _refused(dev, "a secret variable may never be a probe argument")


@pytest.mark.parametrize("key", ["default", "example"])
def test_secret_variable_carries_no_value(dev, key):
    _edit(_manifest(dev), 'kind = "secret"\n', f'kind = "secret"\n{key} = "hunter2"\n')
    _refused(dev, "variables[3]", f"a secret variable has no {key}")


def test_secret_variable_takes_no_other_type(dev):
    _edit(_manifest(dev), 'kind = "secret"\n', 'kind = "secret"\ntype = "url"\n')
    _refused(dev, "a secret variable is text")


def test_undeclared_variable_in_a_rendered_material_is_refused(dev):
    _append(dev / "template" / "CLAUDE.md", "{{nobody}}\n")
    _refused(dev, "template/CLAUDE.md", "undeclared variable {{nobody}}")


def test_verbatim_material_keeps_foreign_template_syntax(dev):
    # `${{ secrets.CI_TOKEN }}` is the CI system's syntax, not a BattleLab reference.
    text = (dev / "template" / ".forgejo" / "workflows" / "pr-validate.yml").read_text()
    assert "${{ secrets.CI_TOKEN }}" in text
    playbooks.load_bundle(dev)


# --- connections ---------------------------------------------------------------------------------


def test_connection_endpoint_literal_is_refused(dev):
    _edit(_manifest(dev), 'url = "{{forge_url}}"', 'url = "https://forge.example.com"')
    _refused(dev, "connections[0].url", "never supplies a literal endpoint")


def test_connection_endpoint_must_reference_the_right_type(dev):
    _edit(_manifest(dev), 'url = "{{forge_url}}"', 'url = "{{repo}}"')
    _refused(dev, "must reference a url text variable")


def test_connection_credential_must_be_a_secret(dev):
    _edit(_manifest(dev), 'credential = "forge_token"', 'credential = "repo"')
    _refused(dev, "credential", "declared secret variable")


def test_connection_verify_is_from_its_kinds_fixed_set(dev):
    _edit(_manifest(dev), 'verify = "api"', 'verify = "status"')
    _refused(dev, "verify")


def test_requires_connections_must_be_declared(dev):
    _edit(_manifest(dev), 'connections = ["forge"]', 'connections = ["mail"]')
    _refused(dev, "requires.connections")


def test_capabilities_are_a_closed_request_vocabulary(dev):
    _edit(_manifest(dev), "orchestrator_input = true", "orchestrator_input = true\nsudo = true")
    _refused(dev, "capabilities.sudo", "unknown field")


# --- variables -----------------------------------------------------------------------------------


def test_variable_pattern_uses_the_safe_grammar(dev):
    _edit(_manifest(dev), "pattern = '^[a-z0-9\\-]{1,64}$'", "pattern = '^(a+)+$'")
    _refused(dev, "pattern")


def test_variable_default_must_match_its_pattern(dev):
    _edit(
        _manifest(dev), 'default = "make test"', "default = \"make test\"\npattern = '^[a-z]{1,4}$'"
    )
    _refused(dev, "does not match the variable's pattern")


def test_enum_needs_choices_and_a_default_among_them(nondev):
    _edit(nondev / "playbook.toml", 'default = "today"', 'default = "never"')
    _refused(nondev, "declared choices")


def test_url_variable_default_is_a_url(dev):
    _edit(_manifest(dev), 'example = "https://forge.example.com"', 'example = "file:///etc/passwd"')
    _refused(dev, "http:// or https://")


def test_variable_name_follows_field_name_re(dev):
    _edit(_manifest(dev), 'name = "repo_name"', 'name = "Repo-Name"')
    _refused(dev, "variables[0].name")


# --- templates -----------------------------------------------------------------------------------


def test_bundled_template_images_are_refused(dev):
    _append(dev / "templates" / "start.toml", 'images = [{ name = "x", path = "x.png" }]\n')
    _refused(dev, "templates/start.toml", "images are not supported")


def test_template_goes_through_the_template_validator(dev):
    _edit(dev / "templates" / "start.toml", 'tags = ["dev"]', 'tags = ["Not A Tag"]')
    _refused(dev, "templates/start.toml", "a tag is lowercase")


def test_template_fields_are_derived_from_variables_not_declared_in_the_template(dev):
    pb = playbooks.load_bundle(dev)
    fields = pb["templates"]["start"]["fields"]
    assert [f["name"] for f in fields] == ["repo", "test_cmd"]
    assert fields[1] == {
        "name": "test_cmd",
        "label": "Test command",
        "default": "make test",
        "required": False,
        "source": "template",
        "kind": "text",
    }


def test_the_same_bundle_validates_on_a_host_with_an_empty_uploads_folder(
    dev, tmp_path, monkeypatch
):
    home = tmp_path / "fresh-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    from agent_sessions.routes.upload import uploads_dir

    assert not uploads_dir().exists()
    pb = playbooks.load_bundle(dev)
    assert pb["templates"]["start"]["images"] == []
    assert not uploads_dir().exists()  # nothing was created on the way


# --- material node policy ------------------------------------------------------------------------


def test_symlink_file_in_the_bundle_is_refused(dev):
    os.symlink("CLAUDE.md", dev / "template" / "LINK.md")
    _refused(dev, "template/LINK.md", "symbolic link")


def test_symlinked_directory_in_the_bundle_is_refused(dev):
    os.symlink("docs", dev / "template" / "more")
    _refused(dev, "template/more", "symbolic link")


def test_symlink_pointing_outside_the_bundle_is_refused(dev, tmp_path):
    # A DECLARED material swapped for a link to a file outside the bundle: the only thing wrong
    # is the link, so nothing but the node policy can refuse it.
    outside = tmp_path / "outside.md"
    outside.write_text("anything\n")
    mission = dev / "template" / "docs" / "mission.md"
    mission.unlink()
    os.symlink(outside, mission)
    _refused(dev, "template/docs/mission.md", "symbolic link")


def test_symlinked_bundle_root_is_refused(dev, tmp_path):
    link = tmp_path / "linkroot"
    os.symlink(dev, link)
    with pytest.raises(PlaybookFormatError, match="bundle root cannot be opened"):
        tree.read_tree(link)


def test_hardlink_in_the_bundle_is_refused(dev, tmp_path):
    # The second name is OUTSIDE the bundle, so the declared file itself is the only thing wrong:
    # its bytes are shared with a path the bundle does not own.
    os.link(dev / "template" / "CLAUDE.md", tmp_path / "elsewhere.md")
    _refused(dev, "template/CLAUDE.md", "hard link")


def test_fifo_in_the_bundle_is_refused(dev):
    os.mkfifo(dev / "template" / "pipe")
    _refused(dev, "template/pipe", "FIFO")


def test_socket_in_the_bundle_is_refused(dev, monkeypatch):
    monkeypatch.chdir(dev / "template")  # AF_UNIX paths are short; bind relative to the cwd
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind("sock")
        _refused(dev, "template/sock", "socket")
    finally:
        s.close()


def test_device_node_in_the_bundle_is_refused(dev, monkeypatch):
    # A device node cannot be created unprivileged, so the walk's lstat seam reports one for a
    # real file and the real walk has to refuse it.
    real = tree._lstat_at

    def fake(name, dir_fd):
        st = real(name, dir_fd)
        if name == "mission.md":
            fields = list(st)
            fields[0] = stat.S_IFCHR | 0o600
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(tree, "_lstat_at", fake)
    _refused(dev, "template/docs/mission.md", "device node")


@pytest.mark.parametrize(
    "mode,expected",
    [
        (stat.S_IFBLK | 0o600, "device node"),
        (stat.S_IFCHR | 0o600, "device node"),
        (stat.S_IFIFO | 0o600, "FIFO"),
        (stat.S_IFSOCK | 0o600, "socket"),
        (stat.S_IFLNK | 0o777, "symbolic link"),
    ],
)
def test_node_policy_table(mode, expected):
    assert expected in tree.node_problem(mode, 1)
    assert tree.node_problem(stat.S_IFREG | 0o644, 1) is None
    assert tree.node_problem(stat.S_IFDIR | 0o755, 2) is None


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "/etc/passwd",
        "docs/../../x.md",
        "docs//x.md",
        "./CLAUDE.md",
        ".git/config",
        "docs/*.md",
    ],
)
def test_material_path_must_be_relative_and_contained(dev, path):
    _edit(_manifest(dev), 'path = "docs/mission.md"', f'path = "{path}"')
    _refused(dev, "materials[3].path")


def test_git_directory_in_the_bundle_is_refused(dev):
    (dev / "template" / ".git").mkdir()
    _refused(dev, ".git")


def test_undeclared_file_in_template_is_refused(dev):
    (dev / "template" / "docs" / "extra.md").write_text("x")
    _refused(dev, "template/docs/extra.md", "not declared")


def test_declared_material_must_exist(dev):
    (dev / "template" / "docs" / "mission.md").unlink()
    _refused(dev, "materials[docs/mission.md]", "is not a file")


def test_oversized_material_is_refused(dev):
    (dev / "template" / "docs" / "mission.md").write_bytes(b"x" * (schema.MAX_FILE_BYTES + 1))
    _refused(dev, "larger than")


# --- instruction aliases (#1096 §11) -------------------------------------------------------------


def test_same_directory_declared_alias_is_accepted():
    pb = playbooks.load_bundle(FIXTURES / DEV)
    assert any(
        m["kind"] == "instruction-alias" and m["target"] == "CLAUDE.md" for m in pb["materials"]
    )


@pytest.mark.parametrize(
    ("new_target", "needle"),
    [
        ("../CLAUDE.md", "one path component"),
        ("/etc/passwd", "one path component"),
        ("docs/agent-workflow.md", "one path component"),
        ("GEMINI.md", "not a declared material"),
    ],
    ids=["traversal", "absolute", "target-in-subdirectory", "undeclared"],
)
def test_alias_target_rules(dev, new_target, needle):
    _edit(_manifest(dev), 'target = "CLAUDE.md"', f'target = "{new_target}"')
    _refused(dev, needle)


def test_alias_of_an_alias_is_refused(dev):
    _append(
        _manifest(dev),
        '\n[[materials]]\npath = "GEMINI.md"\nkind = "instruction-alias"\ntarget = "AGENTS.md"\n',
    )
    _edit(_manifest(dev), '[flows]\ndefault = "dev"\n', "")
    _refused(dev, "may not target another alias")


def test_alias_in_a_subdirectory_is_refused(dev):
    _edit(_manifest(dev), 'path = "AGENTS.md"', 'path = "docs/AGENTS.md"')
    _refused(dev, "sits at the bundle's root")


def test_alias_shipped_as_a_file_is_refused(dev):
    (dev / "template" / "AGENTS.md").write_text("x")
    _refused(dev, "declared, never shipped as a file")


# --- rituals, runbooks ---------------------------------------------------------------------------


def test_ritual_must_name_a_runbook_and_a_fixed_cadence(dev):
    _edit(_manifest(dev), 'schedule = "daily"', 'schedule = "0 3 * * *"')
    _refused(dev, "schedule")


def test_ritual_runbook_must_exist(dev):
    _edit(_manifest(dev), 'runbook = "audit"', 'runbook = "cleanup"')
    _refused(dev, "rituals[0].runbook")


def test_runbook_needs_frontmatter(dev):
    (dev / "runbooks" / "audit.md").write_text("no frontmatter\n")
    _refused(dev, "runbooks/audit.md", "frontmatter")


def test_runbook_trigger_is_closed(dev):
    _edit(dev / "runbooks" / "audit.md", 'trigger = "ritual"', 'trigger = "webhook"')
    _refused(dev, "trigger")


# --- requires executes nothing -------------------------------------------------------------------


def test_requires_binaries_executes_nothing(dev, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("a playbook load or requires check tried to execute something")

    for mod, names in (
        (subprocess, ("Popen", "run", "call", "check_call", "check_output", "getoutput")),
        (
            os,
            (
                "system",
                "popen",
                "execv",
                "execve",
                "execvp",
                "execvpe",
                "execl",
                "execlp",
                "spawnv",
                "spawnve",
                "spawnvp",
                "posix_spawn",
                "posix_spawnp",
                "fork",
            ),
        ),
    ):
        for name in names:
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, forbidden)
    looked: list[str] = []

    def which(name, *a, **k):
        looked.append(name)
        return None

    monkeypatch.setattr(shutil, "which", which)
    pb = playbooks.load_bundle(dev)
    assert looked == []  # loading does not even look
    assert playbooks.list_bundles(dev.parent)[0]["ok"] is True
    assert playbooks.requires_status(pb) == {"binaries": {"git": False}}
    assert looked == ["git"]


def test_requires_binary_name_is_a_name_not_a_command(dev):
    _edit(_manifest(dev), 'binaries = ["git"]', 'binaries = ["git status"]')
    _refused(dev, "requires.binaries")


# --- review round 1 (PR #1248): a bundle never chooses a target through a variable --------------


def _manifest_tail(b: Path, text: str) -> None:
    """Append top-level tables AFTER re-homing `[flows]`, so nothing lands inside that table."""
    _edit(_manifest(b), '[flows]\ndefault = "dev"\n', "")
    _append(_manifest(b), text + '\n[flows]\ndefault = "dev"\n')


def test_target_variable_default_is_refused_for_a_forge_repo(dev):
    _edit(_manifest(dev), 'name = "repo"\n', 'name = "repo"\ndefault = "attacker/evil"\n')
    _refused(dev, "variables[1].default", "must be typed by the operator")


def test_target_variable_enum_choices_are_refused(dev):
    _edit(
        _manifest(dev),
        'name = "repo"\n',
        'name = "repo"\ntype = "enum"\nchoices = ["attacker/evil"]\n',
    )
    _refused(dev, "variables[1].choices", "must be typed by the operator")


def test_target_variable_default_is_refused_for_an_http_url(dev):
    _edit(
        _manifest(dev),
        'name = "deploy_url"\n',
        'name = "deploy_url"\ndefault = "http://169.254.169.254/latest"\n',
    )
    _refused(dev, "variables[4].default", "must be typed by the operator")


def test_target_variable_default_is_refused_for_a_connection_endpoint(dev):
    _edit(
        _manifest(dev),
        'example = "https://forge.example.com"',
        'example = "https://forge.example.com"\ndefault = "https://attacker.example"',
    )
    _refused(dev, "variables[2].default", "connection 'forge' url")


def _root_connection(nondev: Path, default_line: str) -> None:
    m = nondev / "playbook.toml"
    _edit(m, "connections = []\n", "")
    _edit(m, '[flows]\ndefault = "triage"\n', "")
    _append(
        m,
        f'\n[[variables]]\nname = "root"\ntype = "path"\n{default_line}\n'
        '\n[[connections]]\nname = "files"\nkind = "filesystem-root"\npath = "{{root}}"\n'
        '\n[flows]\ndefault = "triage"\n',
    )


def test_glob_path_default_is_refused(nondev):
    _root_connection(nondev, 'default = "/../**/*"')
    _refused(nondev, "variables[2].default", "no glob characters")


def test_target_path_variable_default_is_refused_even_when_well_formed(nondev):
    _root_connection(nondev, 'default = "/srv/data"')
    _refused(nondev, "variables[2].default", "connection 'files' path")


def test_target_path_variable_without_default_is_accepted(nondev):
    _root_connection(nondev, 'example = "~/mail"')
    pb = playbooks.load_bundle(nondev)
    assert pb["connections"][0]["params"] == {"path": "root"}


@pytest.mark.parametrize("value", ["docs/*.md", "../up", "a//b", "/etc/./x", "~/../x"])
def test_path_example_follows_the_manifest_path_rules(dev, value):
    _manifest_tail(dev, f'\n[[variables]]\nname = "where"\ntype = "path"\nexample = "{value}"\n')
    _refused(dev, "no glob characters")


def test_target_variable_example_stays_allowed(dev):
    pb = playbooks.load_bundle(dev)
    forge_url = next(v for v in pb["variables"] if v["name"] == "forge_url")
    assert forge_url["example"] == "https://forge.example.com" and forge_url["default"] is None


def test_expectation_variable_keeps_its_default(dev):
    # `healthy_status` fills `expect_status` — an expectation, never a target — so its default
    # stays; `LITERAL_ARGS` is exactly the line between the two.
    pb = playbooks.load_bundle(dev)
    assert next(v for v in pb["variables"] if v["name"] == "healthy_status")["default"] == 200


# --- review round 1: every free-text field is scanned ---------------------------------------------


@pytest.mark.parametrize(
    ("rel", "old", "new", "where"),
    [
        ("flows/dev.toml", 'title = "Merge"', 'title = "Merge {{TOKEN}}"', "steps[3].title"),
        (
            "flows/dev.toml",
            'title = "The pull request is merged"',
            'title = "Merged {{TOKEN}}"',
            "checklist[0].title",
        ),
        (
            "flows/dev.toml",
            'description = "The development',
            'description = "{{TOKEN}}',
            "description",
        ),
        ("flows/dev.toml", 'label = "Reviewer bot"', 'label = "{{TOKEN}}"', "actor.label"),
        ("runbooks/audit.md", 'title = "Nightly audit"', 'title = "Audit {{TOKEN}}"', "title"),
        ("runbooks/audit.md", "same cause", "same cause {{TOKEN}}", "bail[0]"),
        ("playbook.toml", 'help = "The repository', 'help = "{{TOKEN}} The repository', "help"),
        (
            "playbook.toml",
            'summary = "Plan an issue',
            'summary = "{{TOKEN}} Plan an issue',
            "summary",
        ),
        (
            "templates/start.toml",
            'description = "Pick',
            'description = "{{TOKEN}} Pick',
            "description",
        ),
        ("flows/dev.toml", 'title = "Issue to live"', 'title = "{{TOKEN}}"', "dev.toml: title"),
        (
            "playbook.toml",
            'verify = "api"',
            'verify = "api"\nlabel = "{{TOKEN}}"',
            "connections[0].label",
        ),
        ("playbook.toml", 'publisher = "Example"', 'publisher = "{{TOKEN}}"', "identity.publisher"),
    ],
    ids=[
        "step-title",
        "item-title",
        "flow-desc",
        "actor-label",
        "rb-title",
        "bail",
        "help",
        "summary",
        "tpl-desc",
        "flow-title",
        "conn-label",
        "publisher",
    ],
)
@pytest.mark.parametrize("token", ["forge_token", "nobody"], ids=["secret", "undeclared"])
def test_every_free_text_field_is_scanned(dev, rel, old, new, where, token):
    _edit(dev / rel, old, new.replace("TOKEN", token))
    needle = "secret variable" if token == "forge_token" else "undeclared variable"
    _refused(dev, where, needle)


# --- review round 1: surviving mutants ------------------------------------------------------------


def test_uppercase_git_component_is_refused(dev):
    (dev / "template" / ".GIT").mkdir()
    _refused(dev, "template/.GIT", "named .git")


def _pretend_regular(monkeypatch, target: str, like: Path) -> None:
    """The listing sees `target` with `like`'s regular stat; the open sees the truth."""
    real = tree._lstat_at

    def fake(name, dir_fd):
        return os.stat(like) if name == target else real(name, dir_fd)

    monkeypatch.setattr(tree, "_lstat_at", fake)


def test_a_file_hardlinked_between_listing_and_reading_is_refused(dev, tmp_path, monkeypatch):
    mission = dev / "template" / "docs" / "mission.md"
    os.link(mission, tmp_path / "second-name.md")
    single = tmp_path / "single.md"
    single.write_text("x")
    _pretend_regular(monkeypatch, "mission.md", single)
    _refused(dev, "template/docs/mission.md", "hard link")


def test_a_fifo_swapped_in_after_listing_is_refused(dev, tmp_path, monkeypatch):
    mission = dev / "template" / "docs" / "mission.md"
    mission.unlink()
    os.mkfifo(mission)
    single = tmp_path / "single.md"
    single.write_text("x")
    _pretend_regular(monkeypatch, "mission.md", single)
    _refused(dev, "template/docs/mission.md", "FIFO")


def test_a_directory_swapped_in_after_listing_is_a_format_error(dev, tmp_path, monkeypatch):
    mission = dev / "template" / "docs" / "mission.md"
    mission.unlink()
    mission.mkdir()
    single = tmp_path / "single.md"
    single.write_text("x")
    _pretend_regular(monkeypatch, "mission.md", single)
    _refused(dev, "template/docs/mission.md", "changed while the bundle was being read")


@pytest.mark.parametrize(
    ("limit", "value", "needle"),
    [
        ("MAX_TREE_DEPTH", 2, "nested deeper than 2"),
        ("MAX_TREE_ENTRIES", 5, "more than 5 entries"),
        ("MAX_TOTAL_BYTES", 200, "larger than 200 bytes"),
        ("PATH_MAX", 20, "path is longer than 20"),
    ],
)
def test_tree_walk_limits(dev, monkeypatch, limit, value, needle):
    playbooks.load_bundle(dev)  # green at the real limits
    monkeypatch.setattr(schema, limit, value)
    _refused(dev, needle)


def test_listing_survives_an_os_error_from_one_bundle(dev, nondev, monkeypatch):
    real = playbooks.loader.load_bundle

    def flaky(path):
        if Path(path).name == NONDEV:
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(playbooks.loader, "load_bundle", flaky)
    cards = {c["id"]: c for c in playbooks.list_bundles(dev.parent)}
    assert cards[DEV]["ok"] is True
    assert cards[NONDEV] == {
        "id": NONDEV,
        "ok": False,
        "error": "could not be read (PermissionError)",
    }


def test_listing_overflow_gets_an_error_card_not_a_silent_drop(dev, nondev, monkeypatch):
    monkeypatch.setattr(schema, "MAX_BUNDLES", 1)
    cards = playbooks.list_bundles(dev.parent)
    assert [c["id"] for c in cards] == [DEV, NONDEV]
    assert cards[0]["ok"] is True
    assert cards[1]["ok"] is False and "more than 1 entries" in cards[1]["error"]


def test_runbook_requires_connections_must_be_declared(dev):
    _edit(
        dev / "runbooks" / "audit.md", 'connections = ["forge"]\n+++', 'connections = ["mail"]\n+++'
    )
    _refused(
        dev, "runbooks/audit.md", "requires.connections", "'mail' is not a declared connection"
    )


def test_enum_choice_with_a_control_character_is_refused(nondev):
    _edit(nondev / "playbook.toml", '"this-week"]', '"this\\u0001week"]')
    _refused(nondev, "choices[1]", "not a valid choice")


def test_pattern_only_on_text_or_path_variables(dev):
    _edit(
        _manifest(dev),
        'example = "https://forge.example.com"',
        "example = \"https://forge.example.com\"\npattern = '^[a-z]{1,4}$'",
    )
    _refused(dev, "variables[2].pattern", "only a text or path variable takes a pattern")


def test_flows_default_must_name_an_existing_flow(dev):
    _edit(_manifest(dev), 'default = "dev"', 'default = "release"')
    _refused(dev, "flows.default", "'release' is not a flow")


def test_readme_must_be_a_file(dev):
    (dev / "README.md").unlink()
    (dev / "README.md").mkdir()
    _refused(dev, "README.md", "must be a file")


def test_flows_must_be_a_directory(dev):
    shutil.rmtree(dev / "flows")
    (dev / "flows").write_text("x")
    _refused(dev, "flows", "must be a directory")


def test_deeply_nested_runbook_frontmatter_is_a_format_error(dev):
    deep = "x = " + "[" * 5000 + "]" * 5000 + "\n"
    (dev / "runbooks" / "audit.md").write_text(f"+++\n{deep}+++\nbody\n")
    _refused(dev, "runbooks/audit.md", "nested too deeply")
