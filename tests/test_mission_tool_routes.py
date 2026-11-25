"""Every tool route of a mission-ready API client is accounted for (#1903).

The bar (#1581 / #1613): "no bypass" is not enough. Each route an adapter has is exactly one of

* **ask**: a permission request BattleLab surfaces as a pending approval. Its input binding is the
  adapter's own (`tests/test_api_permissions_opencode.py`: an empty ACP request borrows only the
  correlated running tool input, otherwise the approval is incomplete and cannot be approved);
* **enforced by config**: the adapter's own permission block removes it (`task: deny`);
* **refused with a shown reason**: a named route and a test.

The opencode rows are MEASURED on the installed CLI by `fixtures/opencode_tool_routes_probe.py`
(offline: a throwaway HOME, a loopback scripted model, no credentials) and read here from
`fixtures/opencode_tool_routes.json`. The fixture records the permission block it was measured
under, so a changed block fails here until the probe is re-run."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from agent_sessions import native_protocol
from agent_sessions.engines import opencode as oc
from agent_sessions.plugins import kinds

FIXTURES = Path(__file__).parent / "fixtures"
MEASURED = json.loads((FIXTURES / "opencode_tool_routes.json").read_text())

#: Kinds whose route inventory lives in this file. A kind turns `API_MISSION_READY` true only with
#: an entry here (or its own end-to-end proof, which Claude's predates this inventory: #1900).
INVENTORIED = {"opencode-acp"}  # kind-data
PROVED_BY_1900 = {"claude-stream-json"}  # kind-data


#: CLI builds the fixture may have been measured on: a bump is a new measurement, never a pass.
MEASURED_BUILDS = {"1.18.35"}


def classify(tool: str, row: dict, permission: dict) -> str:
    """One observed route as `ask`, `enforced` (the config refuses it) or raise. The row is what
    the real CLI did; the config only confirms it, so an unlisted route cannot pass by omission."""
    if row["ran"]:
        raise AssertionError(f"{tool} had its effect with every request cancelled")
    if row["offered"] and row["asked"] >= 1:
        return "ask"
    if not row["offered"] and row["invalid"] and permission.get(tool) == "deny":
        return "enforced"
    raise AssertionError(f"{tool} is an unclassified route: {row}")


def test_every_observed_route_is_classified_against_the_effective_config():
    kinds_seen = {
        classify(t, r, oc.API_GUARDED_PERMISSION)
        for t, r in MEASURED["tools"].items()
        if r["offered"] or t == "task"
    }
    assert kinds_seen == {"ask", "enforced"}


def test_NEGATIVE_CONTROL_a_route_that_neither_asks_nor_is_refused_fails_the_inventory():
    planted = {"asked": 0, "ran": False, "offered": True, "invalid": False}
    with pytest.raises(AssertionError, match="unclassified"):
        classify("newtool", planted, oc.API_GUARDED_PERMISSION)
    with pytest.raises(AssertionError, match="had its effect"):
        classify("bash", {**planted, "ran": True, "asked": 1}, oc.API_GUARDED_PERMISSION)
    # A route absent from the offer and not refused by the config is no pass either.
    gone = {"asked": 0, "ran": False, "offered": False, "invalid": True}
    with pytest.raises(AssertionError, match="unclassified"):
        classify("task", gone, {**oc.API_GUARDED_PERMISSION, "task": "ask"})


def test_the_fixture_was_measured_on_a_build_this_test_knows():
    assert (
        MEASURED["version"] in MEASURED_BUILDS
    ), f"measured on {MEASURED['version']}: re-run the probe and review the new build's routes"


def test_a_mission_ready_kind_has_an_inventory_or_the_1900_proof():
    ready = {k for k, v in kinds.API_MISSION_READY.items() if v}
    assert ready <= INVENTORIED | PROVED_BY_1900, ready - INVENTORIED - PROVED_BY_1900
    assert INVENTORIED <= set(kinds.API_KINDS)


def test_the_measurement_is_of_the_permission_block_in_the_code():
    block = json.loads(oc.api_config_content(oc.mint_api_agent()))
    guarded = next(iter(block["agent"].values()))["permission"]
    digest = hashlib.sha256(json.dumps(guarded, sort_keys=True).encode()).hexdigest()
    assert (
        MEASURED["guarded_permission_sha256"] == digest
    ), "the guarded permission block changed: re-run tests/fixtures/opencode_tool_routes_probe.py"
    assert MEASURED["version"], "the fixture names the CLI build it measured"


def test_the_guarded_block_asks_for_everything_but_the_one_refused_route():
    assert oc.API_GUARDED_PERMISSION["*"] == "ask"
    refused = {k for k, v in oc.API_GUARDED_PERMISSION.items() if v != "ask"}
    assert refused == {"task"}, refused
    assert oc.API_GUARDED_PERMISSION["task"] == "deny"
    # The bypass block is the only one that allows anything, and it is separate.
    assert "allow" not in oc.API_PERMISSION.values()


def test_every_tool_the_model_is_offered_asks_and_none_ran_unasked():
    offered = MEASURED["offered"]
    assert offered, "the probe measured nothing"
    for tool in offered:
        row = MEASURED["tools"][tool]
        assert row["asked"] >= 1, f"{tool} ran without a permission request: {row}"
        assert not row["ran"], f"{tool} had its effect with every request cancelled: {row}"
        assert not row["invalid"], f"{tool}: offered but invalid: {row}"
    # The classes the issue names, each present or proved absent below.
    classes = {
        "shell": {"bash"},
        "file edit/write": {"edit", "write"},
        "read/search": {"read", "glob", "grep"},
        "network": {"webfetch"},
        "MCP / plugin": {"fixture_labels"},
        "skill": {"skill"},
        "todo": {"todowrite"},
    }
    for name, tools in classes.items():
        assert tools <= set(offered), f"{name} not measured: {tools - set(offered)}"


def test_every_tool_not_offered_is_refused_by_the_config_or_unreachable():
    for tool, row in MEASURED["tools"].items():
        if row["offered"]:
            continue
        assert row["invalid"], f"{tool}: not offered yet the model's call did not fail: {row}"
        assert not row["ran"] and row["asked"] == 0, f"{tool}: {row}"
    # `task` is the sub-agent tool: denied by OUR block (not merely absent), so no operator or
    # project config can offer it back.
    assert MEASURED["tools"]["task"]["offered"] is False
    assert oc.API_GUARDED_PERMISSION["task"] == "deny"


def test_the_slash_command_door_to_a_child_session_is_measured_and_closed():
    """A prompt `/name` runs a command by itself, and `subtask: true` (or a command routed to an
    agent) runs it in a CHILD session under another agent: on the measured build that child ran
    its tool with no request reaching BattleLab. The model cannot type a slash command into its
    own prompt, but a mission text can begin with one, so a guarded text that does is refused
    before it is written. #1892: an operator's own turn in a no-bypass session is refused the
    same doors, built-in or declared by the operator or the project."""
    doors = {c: r for c, r in MEASURED["commands"].items() if r["subagent_started"]}
    assert doors, "the probe found no sub-agent door to close"
    assert any(r["child_ran"] for r in doors.values()), "no door was proved open"
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    for command in doors:
        for text in (command, f"  {command} now", f"\n{command}"):
            assert native_protocol.guarded_text_refusal(codec, text), text
        assert native_protocol.guarded_text_refusal(codec, f"run {command}") is None
    # `/review` is opencode's built-in `subtask: true` command; the rest are declared in a config
    # (the probe's source has `/probe-sub` as `subtask: true, agent: general`), so a resolved
    # command table covers them too (both triggers).
    assert native_protocol.operator_subtask_refusal(codec, "/review")
    declared = frozenset(c.lstrip("/") for c in doors)
    for command in doors:
        for text in (command, f"  {command} now", f"\n{command}"):
            assert native_protocol.operator_subtask_refusal(codec, text, declared), text
        assert native_protocol.operator_subtask_refusal(codec, f"run {command}", declared) is None


@pytest.mark.parametrize("prefix", ["\ufeff", "\u200b", "\u00a0", " \ufeff\n", "\u2028", "\x00"])
def test_a_invisible_prefix_does_not_hide_a_slash_command(prefix):
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    assert native_protocol.guarded_text_refusal(codec, f"{prefix}/probe-sub")


@pytest.mark.parametrize("text", ["Fix the login flow", "", "a / b", "path/to/file", "10/20"])
def test_a_guarded_text_that_does_not_start_with_a_slash_is_untouched(text):
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    assert native_protocol.guarded_text_refusal(codec, text) is None


def test_a_client_without_slash_commands_is_not_touched():
    assert (
        native_protocol.guarded_text_refusal(
            native_protocol.ClaudeCodec(str(uuid.uuid4())), "/review"
        )
        is None
    )


def _operator_worker(codec, **overrides):
    import types

    from agent_sessions import native_worker

    fields = {"config": {"cwd": "/w", "bypass": False}, "subtask_commands": frozenset()}
    fields.update(overrides)
    namespace = types.SimpleNamespace(codec=codec, **fields)
    namespace.frame = native_worker.Worker.frame.__get__(namespace)
    return namespace


def test_an_operator_subtask_command_is_refused_in_a_guarded_session():
    """#1892: an operator's own `/review` (a built-in `subtask: true` command) or a project
    command routed to an agent starts a child session whose first ask never reaches ACP. A
    guarded (no bypass) session refuses both before anything is written."""
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    guarded = _operator_worker(codec, subtask_commands=frozenset({"probe-sub", "my.command"}))
    for text in ("/review", "  /review the change", "\ufeff/review", "/probe-sub", "/my.command"):
        with pytest.raises(native_protocol.ProtocolError, match="subagent"):
            guarded.frame("submit", {"text": text, "operation_id": str(uuid.uuid4())})
        with pytest.raises(native_protocol.ProtocolError, match="subagent"):
            guarded.frame("steer", {"text": text, "operation_id": str(uuid.uuid4())})


def test_an_operator_command_without_a_child_and_a_plain_message_are_untouched():
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    guarded = _operator_worker(codec, subtask_commands=frozenset({"probe-sub"}))
    for text in ("/init", "/probe-plain", "please run /review yourself", "Fix the login flow"):
        with pytest.raises(native_protocol.ProtocolError) as exc:
            guarded.frame("submit", {"text": text, "operation_id": str(uuid.uuid4())})
        assert "subagent" not in str(exc.value)


def test_a_bypass_operator_keeps_every_command():
    codec = native_protocol.OpencodeAcpCodec(agent=oc.mint_api_agent())
    bypass = _operator_worker(
        codec,
        config={"cwd": "/w", "bypass": True},
        subtask_commands=frozenset({"probe-sub"}),
    )
    for text in ("/review", "/probe-sub"):
        with pytest.raises(native_protocol.ProtocolError) as exc:
            bypass.frame("submit", {"text": text, "operation_id": str(uuid.uuid4())})
        assert "subagent" not in str(exc.value)


def test_the_resolved_command_table_marks_only_child_opening_commands():
    """opencode 1.18.35's real condition: `subtask == true`, or — when `subtask` is not false —
    the routed agent's effective mode is `subagent` (built-in modes from the installed binary:
    build/plan are primary, general/explore subagent; `debug config` reports operator agents).
    A primary-routed command is ordinary and must not be refused; an unknown agent fails closed."""
    resolved = {
        "agent": {
            "lint": {"mode": "primary"},
            "custom-sub": {"mode": "subagent"},
            "default-agent": {},  # opencode's default mode is "all", not subagent
            # A configured BUILT-IN without a mode keeps its built-in mode (review 8789).
            "general": {"description": "customized built-in"},
            "build": {"description": "customized built-in"},
        },
        "command": {
            # `subtask: true` always opens a child, whatever the routed agent.
            "probe-sub": {"subtask": True, "agent": "general", "template": "child"},
            "true-primary": {"subtask": True, "agent": "lint", "template": "child"},
            # `subtask: false` never opens one, whatever the routed agent.
            "not-subtask": {"subtask": False, "template": "text"},
            "agent-but-off": {"agent": "general", "subtask": False, "template": "text"},
            # An unset subtask: only a subagent mode opens a child.
            "agent-only": {"agent": "explore", "template": "child"},
            "custom-sub": {"agent": "custom-sub", "template": "child"},
            "primary-custom": {"agent": "lint", "template": "text"},
            "primary-builtin": {"agent": "build", "template": "text"},
            "default-mode": {"agent": "default-agent", "template": "text"},
            "audit": {"agent": "general", "template": "child"},  # customized built-in subagent
            "tidy": {"agent": "build", "template": "text"},  # customized built-in primary
            # Unknown agent: fail closed.
            "mystery": {"agent": "mystery", "template": "child"},
            "plain": {"template": "text"},
        },
    }
    commands = oc.subtask_commands(resolved)
    assert commands == frozenset(
        {"probe-sub", "true-primary", "agent-only", "custom-sub", "mystery", "audit"}
    )
    for malformed in ({}, {"command": []}, {"command": {"x": "not a table"}}):
        assert oc.subtask_commands(malformed) == frozenset()


def test_the_adapter_lookup_refuses_subtask_commands_by_kind():
    """The app's own layer (#1892): it knows the CLI's built-ins, so an older worker never
    writes `/review`; a client without slash commands and an unnamed adapter are untouched."""
    for commands in (frozenset(), frozenset({"review"})):
        assert native_protocol.operator_adapter_subtask_refusal(
            "opencode-acp", "\ufeff/review", commands
        )
        assert (
            native_protocol.operator_adapter_subtask_refusal(
                "opencode-acp", "please run /review", commands
            )
            is None
        )
    for adapter in (None, "", "claude-stream-json", "codex-app-server"):
        assert native_protocol.operator_adapter_subtask_refusal(adapter, "/review") is None
    # The named config trigger is the caller's table, never invented here.
    assert native_protocol.operator_adapter_subtask_refusal(
        "opencode-acp", "/probe-sub", frozenset({"probe-sub"})
    )


def test_a_worker_without_a_command_inventory_refuses_every_slash_command():
    """#1892: a worker started before the inventory has neither the field nor its own refusal.
    A child-opening built-in keeps the subtask reason; EVERY other slash command waits for a stop
    and resume, even a built-in name like `/init`, because a project can redefine it as a
    child-opening command (review 8789). Plain text goes."""
    subtask = native_protocol.operator_adapter_without_inventory_refusal("opencode-acp", "/review")
    assert subtask and "subagent" in subtask
    inventory = native_protocol.operator_adapter_without_inventory_refusal(
        "opencode-acp", "/probe-sub"
    )
    assert inventory and "stop and resume" in inventory
    for text in ("/init", "/customize-opencode"):
        refused = native_protocol.operator_adapter_without_inventory_refusal("opencode-acp", text)
        assert refused and "stop and resume" in refused, text
    for text in ("please run /review", "", "fix this"):
        assert (
            native_protocol.operator_adapter_without_inventory_refusal("opencode-acp", text) is None
        )
    # Kinds that report no inventory at all (opencode2) and kinds without slash commands are
    # untouched, and only opencode's worker reports one.
    assert native_protocol.operator_adapter_reports_inventory("opencode-acp")
    for adapter in (None, "", "opencode2-acp", "claude-stream-json", "copilot-acp"):
        assert not native_protocol.operator_adapter_reports_inventory(adapter)
        assert (
            native_protocol.operator_adapter_without_inventory_refusal(adapter, "/probe-sub")
            is None
        )
