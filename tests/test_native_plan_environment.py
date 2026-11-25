"""The agent's plan and the tools it works with (#1726): normalized `plan` / `environment` events
from the three clients' REAL frames, the bounded IPC schema, the snapshot fields and their
lifetimes, the derived call counter, and the public link that shows none of it.

Fixtures: `tests/fixtures/native_plan_environment.PROVENANCE.md`."""

import json
import uuid
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path

import pytest

import test_native_journal as journal_tests
from agent_sessions import (
    native_events,
    native_ipc,
    native_protocol,
    native_runtime,
    share_api,
    structured_runtime,
)
from agent_sessions import native_journal as journal
from agent_sessions.engines import opencode
from agent_sessions.native_protocol import (
    ClaudeCodec,
    CodexCodec,
    CopilotAcpCodec,
    OpencodeAcpCodec,
)
from test_native_agent_turns import T0, at, operator

conversation = journal_tests.conversation
FIXTURES = Path(__file__).parent / "fixtures"
AGENT = "battlelab-api-0123456789abcdef"


def frames(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def normalized(events):
    """Every observation a codec returns must pass the closed journal schema unchanged."""
    out = []
    for event in events:
        if event.kind == "send":
            continue
        data = dict(event.data)
        if event.kind == "approval":  # the worker stamps its generation on these
            data.update(worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
        item = native_ipc.normalize_event({"kind": event.kind, "data": data})
        assert item["data"] == data, event
        out.append(item)
    return out


def of(events, kind):
    return [e for e in events if e["kind"] == kind]


# --- the real frames, per client ------------------------------------------------------------


def replay_codex(name):
    """Drive a CodexCodec through a recorded app-server log: our requests are re-issued by the
    codec (its own ids), the CLI's replies are mapped onto them by method, in order."""
    codec, ids, issued, out = CodexCodec(), {}, defaultdict(deque), []
    log = frames(name)
    texts = [r["frame"]["params"] for r in log if r["frame"].get("method") == "turn/start"]
    for record in log:
        frame = record["frame"]
        if record["dir"] == "out":
            method = frame.get("method")
            if method == "initialize":
                issued[method].append(codec.initialize()["id"])
            elif method == "thread/start":
                issued[method].append(codec.create(frame["params"]["cwd"])["id"])
            elif method == "turn/start":
                text = texts.pop(0)["input"][0]["text"]
                issued[method].append(codec.submit(text, str(uuid.uuid4()))["id"])
            if "id" in frame and method:
                ids[frame["id"]] = issued[method].popleft()
            continue
        if "id" in frame and "method" not in frame:
            frame = {**frame, "id": ids[frame["id"]]}
        events = codec.feed(frame)
        for event in events:
            sent = event.data.get("frame", {}) if event.kind == "send" else {}
            if "method" in sent:
                issued[sent["method"]].append(sent["id"])
        out.extend(normalized(events))
    return codec, out


def test_codex_plan_and_environment_from_real_frames():
    _, events = replay_codex("codex_plan_environment.jsonl")
    (plan,) = of(events, "plan")
    assert plan["data"] == {
        "entries": [
            {"text": "Read the codec", "status": "completed"},
            {"text": "Add the plan event", "status": "in_progress"},
            {"text": "Write the tests", "status": "pending"},
        ],
        "explanation": "Two steps, then verify.",
    }
    final = of(events, "environment")[-1]["data"]
    assert final["skill_use"] is False  # Codex reports no use of a skill
    assert final["mcp"] == [
        {"name": "broken", "status": "failed"},
        {"name": "fixture", "status": "connected"},
    ]
    assert {"name": "release-notes"} in final["skills"]
    assert "commands" not in final  # never reported: absent, not an empty list
    (call,) = [e for e in of(events, "tool") if e["data"].get("tool") == "mcpToolCall"][:1]
    assert call["data"]["call"] is True and call["data"]["mcp_server"] == "fixture"
    # Startup reports are folded into ONE state; an unchanged report writes nothing.
    assert len(of(events, "environment")) <= 6
    # Names and status only: no command line, path or error text reaches the event.
    text = json.dumps(of(events, "environment"))
    assert "no-such-binary" not in text and "/tmp/fixture" not in text and "SKILL.md" not in text


def test_codex_plan_document_comes_from_the_settled_plan_item():
    _, events = replay_codex("codex_plan_item.jsonl")
    (plan,) = of(events, "plan")
    assert plan["data"] == {"document": "# Add the rail\n\n1. Codec events\n2. Snapshot fields\n"}


def test_codex_discovery_errors_leave_the_list_unreported():
    codec = CodexCodec()
    codec.initialize()
    start = codec.create("/w")
    events = codec.feed({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
    sends = [e.data["frame"] for e in events if e.kind == "send"]
    assert [f["method"] for f in sends] == ["mcpServerStatus/list", "skills/list"]
    assert sends[0]["params"] == {"threadId": "thread-1", "detail": "toolsAndAuthOnly", "limit": 1}
    for frame in sends:
        assert codec.feed({"id": frame["id"], "error": {"code": -32601, "message": "no"}}) == []


def replay_claude(name):
    log = frames(name)
    init = next(r["frame"] for r in log if r["frame"].get("subtype") == "init")
    codec, out = ClaudeCodec(init["session_id"]), []
    for record in log:
        frame = record["frame"]
        if record["dir"] == "out":
            if frame.get("type") == "control_request":
                codec.initialize()
            elif frame.get("type") == "user":
                codec.submit(frame["message"]["content"], frame["uuid"])
            continue
        out.extend(normalized(codec.feed(frame)))
    return codec, out


def test_claude_plan_and_environment_from_real_frames():
    _, events = replay_claude("claude_plan_environment.jsonl")
    env = of(events, "environment")
    # `system/init` opens every turn with the same lists: reported ONCE.
    assert len(env) == 1
    data = env[0]["data"]
    assert data["skill_use"] is True
    assert data["mcp"] == [
        {"name": "fixture", "status": "connected"},
        {"name": "broken", "status": "failed"},
    ]
    assert {"name": "release-notes"} in data["skills"]
    assert {"name": "triage"} in data["commands"]
    plans = of(events, "plan")
    # Task #1 of a fresh list: the connection sees the whole list from its first task on.
    assert [p["data"]["entries"] for p in plans if "entries" in p["data"]][-1] == [
        {"text": "Read the codec", "status": "completed"},
        {"text": "Add the plan event", "status": "in_progress"},
    ]
    assert [p["data"] for p in plans if "document" in p["data"]] == [
        {"document": "# Add the rail\n\n1. Codec events\n2. Snapshot fields\n"}
    ]
    calls = [e["data"] for e in of(events, "tool") if e["data"].get("call")]
    skill = next(c for c in calls if c["tool"] == "Skill")
    assert skill["skill"] == "release-notes"
    mcp = next(c for c in calls if c["tool"].startswith("mcp__"))
    assert mcp["mcp_server"] == "fixture"
    # Only a REPORTED use is a skill use: TaskCreate & co. carry none.
    assert sum(1 for c in calls if "skill" in c) == 1


def test_claude_todowrite_input_is_the_checklist_where_the_cli_still_offers_it():
    """2.1.312 disables TodoWrite (`claude_todowrite_disabled.jsonl`): a call it refuses is no
    plan at all — it must never replace the real checklist (review on #1811)."""
    _, events = replay_claude("claude_todowrite_disabled.jsonl")
    assert of(events, "plan") == []
    # Where the CLI accepts it, the input is the checklist — once its result came back clean.
    codec = ClaudeCodec(str(uuid.uuid4()))
    codec.submit("go", str(uuid.uuid4()))
    todos = {"todos": [{"content": "A", "status": "in_progress"}]}
    call = {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": todos}
    assert of(_claude_turn(codec, [call]), "plan") == []  # proposed, not yet accepted
    events = normalized(
        codec.feed(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                },
            }
        )
    )
    (plan,) = of(events, "plan")
    assert plan["data"] == {"entries": [{"text": "A", "status": "in_progress"}]}


def _claude_turn(codec, blocks, result=None, tool_id="t1"):
    events = codec.feed(
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": blocks}}
    )
    if result is not None:
        events += codec.feed(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
                },
                "tool_use_result": result,
            }
        )
    return normalized(events)


def test_claude_task_list_is_shown_only_while_this_connection_knows_all_of_it():
    sid = str(uuid.uuid4())
    codec = ClaudeCodec(sid)
    codec.submit("go", str(uuid.uuid4()))
    # A resumed generation: task 3 was created by an earlier one. Its update proves nothing,
    # and says the list shown may be out of date (review on #1811).
    update = {"type": "tool_use", "id": "t1", "name": "TaskUpdate"}
    update["input"] = {"taskId": "3", "status": "completed"}
    plans = of(_claude_turn(codec, [update], {"success": True, "taskId": "3"}), "plan")
    assert [p["data"] for p in plans] == [{"stale": True}]
    created = {"type": "tool_use", "id": "t2", "name": "TaskCreate", "input": {"subject": "B"}}
    events = _claude_turn(codec, [created], {"task": {"id": "4", "subject": "B"}}, "t2")
    assert not of(events, "plan")  # not task #1: earlier tasks may exist
    listed = {"type": "tool_use", "id": "t3", "name": "TaskList", "input": {}}
    tasks = [
        {"id": "3", "subject": "A", "status": "completed"},
        {"id": "4", "subject": "B", "status": "pending"},
    ]
    (plan,) = of(_claude_turn(codec, [listed], {"tasks": tasks}, "t3"), "plan")
    assert plan["data"]["entries"] == [
        {"text": "A", "status": "completed"},
        {"text": "B", "status": "pending"},
    ]
    gone = {"type": "tool_use", "id": "t4", "name": "TaskUpdate"}
    gone["input"] = {"taskId": "3", "status": "deleted"}
    (plan,) = of(_claude_turn(codec, [gone], {"success": True}, "t4"), "plan")
    assert plan["data"]["entries"] == [{"text": "B", "status": "pending"}]


def test_claude_mcp_call_no_known_server_prefixes_is_unattributed():
    codec = ClaudeCodec(str(uuid.uuid4()))
    codec.feed(
        {
            "type": "system",
            "subtype": "init",
            "session_id": codec.native_id,
            "mcp_servers": [{"name": "git hub", "status": "needs-auth"}, {"name": "git"}],
        }
    )
    codec.submit("go", str(uuid.uuid4()))
    blocks = [
        {"type": "tool_use", "id": "a", "name": "mcp__git_hub__issues", "input": {}},
        {"type": "tool_use", "id": "b", "name": "mcp__git__log", "input": {}},
        {"type": "tool_use", "id": "c", "name": "mcp__other__x", "input": {}},
        {"type": "tool_use", "id": "d", "name": "Bash", "input": {"command": "ls"}},
    ]
    tools = [e["data"] for e in of(_claude_turn(codec, blocks), "tool")]
    assert [t.get("mcp_server") for t in tools] == ["git hub", "git", "", None]


def test_claude_environment_status_words_never_read_healthy_when_unknown():
    codec = ClaudeCodec(str(uuid.uuid4()))
    (_, env) = codec.feed(
        {
            "type": "system",
            "subtype": "init",
            "session_id": codec.native_id,
            "mcp_servers": [
                {"name": "a", "status": "needs-auth"},
                {"name": "b", "status": "something-new"},
                {"name": "c", "status": "disabled", "url": "https://secret.example/x"},
            ],
            "skills": ["s"] * 3,
        }
    )
    assert env.data["mcp"] == [
        {"name": "a", "status": "needs_auth"},
        {"name": "b", "status": "pending"},
        {"name": "c", "status": "disabled"},
    ]
    assert env.data["skills"] == [{"name": "s"}] and "commands" not in env.data
    assert "secret.example" not in json.dumps(env.data)


def replay_opencode(name, probe=None):
    codec, out = OpencodeAcpCodec(agent=AGENT), []
    if probe is not None:
        codec.configured_mcp(opencode.mcp_server_states(probe))
    for record in frames(name):
        frame = record["frame"]
        if record["dir"] == "out":
            method = frame["method"] if "method" in frame else None
            if method == "initialize":
                codec.initialize()
            elif method == "session/new":
                codec.create(frame["params"]["cwd"])
            elif method == "session/set_config_option":
                codec.pin_mode()
            elif method == "session/prompt":
                codec.submit(frame["params"]["prompt"][0]["text"], str(uuid.uuid4()))
            continue
        out.extend(normalized(codec.feed(frame)))
    return codec, out


PROBE = {
    "mcp": {
        "fixture": {"type": "local", "command": ["python3", "/x/mcp.py"], "environment": {}},
        "remote-off": {"type": "remote", "url": "http://127.0.0.1:9/mcp", "enabled": False},
    }
}


def test_opencode_plan_commands_and_tools_from_real_frames():
    _, events = replay_opencode("opencode_plan_environment.jsonl", PROBE)
    env = of(events, "environment")
    final = env[-1]["data"]
    # MCP names come from the configuration probe: `configured` (unmeasured), never connected.
    assert final["mcp"] == [
        {"name": "fixture", "status": "configured"},
        {"name": "remote-off", "status": "disabled"},
    ]
    assert [c["name"] for c in final["commands"]] == [
        "customize-opencode",
        "init",
        "release-notes",
        "review",
        "triage",
    ]
    assert final["skill_use"] is True and "skills" not in final  # opencode lists no skills
    assert "127.0.0.1" not in json.dumps(env) and "mcp.py" not in json.dumps(env)
    # 1.18.35: the todowrite input, journaled ONCE, when the call completed.
    (plan,) = of(events, "plan")
    assert plan["data"]["entries"] == [
        {"text": "Read the codec", "status": "completed"},
        {"text": "Add the plan event", "status": "in_progress"},
        {"text": "Write the tests", "status": "pending"},
    ]
    calls = [e["data"] for e in of(events, "tool") if e["data"].get("call")]
    assert len(calls) == 3  # one per tool call, however many updates each got
    skills = [e["data"]["skill"] for e in of(events, "tool") if "skill" in e["data"]]
    assert skills == ["release-notes"]
    assert [c.get("mcp_server") for c in calls] == [None, None, "fixture"]


def test_opencode_without_a_probe_reports_no_mcp():
    _, events = replay_opencode("opencode_plan_environment.jsonl")
    assert all("mcp" not in e["data"] for e in of(events, "environment"))


def _acp(codec, update):
    return codec.feed(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": codec.native_id, "update": update},
        }
    )


@pytest.mark.parametrize("client", ["opencode", "copilot"])
def test_acp_out_of_turn_relaxation_is_only_for_the_plan_and_the_commands(client):
    if client == "opencode":
        codec = OpencodeAcpCodec("ses_abc", agent=AGENT)
    else:
        codec = CopilotAcpCodec(str(uuid.uuid4()))
    codec.native_id = codec.native_id or "ses_abc"
    # Outside a turn: a tool call, its update, text and thoughts are still dropped.
    for update in (
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "execute", "title": "bash"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}},
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old"}},
    ):
        assert _acp(codec, update) == []
    (commands,) = _acp(
        codec,
        {"sessionUpdate": "available_commands_update", "availableCommands": [{"name": "init"}]},
    )
    assert commands.kind == "environment" and commands.data["commands"] == [{"name": "init"}]
    entries = [{"content": "Ship it", "status": "in_progress", "priority": "high"}]
    (plan,) = _acp(codec, {"sessionUpdate": "plan", "entries": entries})
    assert plan.kind == "plan"
    assert plan.data == {"entries": [{"text": "Ship it", "status": "in_progress"}]}
    # A plan replayed while `session/load` is pending is history already journaled.
    codec.load(codec.native_id, "/w")
    assert _acp(codec, {"sessionUpdate": "plan", "entries": entries}) == []
    normalized([commands, plan])


# --- the closed IPC schema ------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"explanation": "why"},
        {"entries": [{"text": "x", "status": "blocked"}]},
        {"entries": [{"text": "", "status": "pending"}]},
        {"entries": [{"text": "x" * 501, "status": "pending"}]},
        {"entries": [{"text": "x", "status": "pending"}] * 101},
        {"entries": [{"text": "x", "status": "pending", "id": "1"}]},
        {"document": "x" * (64 * 1024 + 1)},
        {"entries": [], "explanation": "x" * 2001},
    ],
)
def test_ipc_refuses_a_plan_outside_its_bounds(data):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "plan", "data": data})


@pytest.mark.parametrize(
    "data",
    [
        {"skill_use": True},
        {"mcp": [{"name": "a", "status": "healthy"}]},
        {"mcp": [{"name": "a", "status": "connected", "url": "https://x"}]},
        {"mcp": [{"name": "a", "status": "connected"}] * 2},
        {"mcp": [{"name": f"s{i}", "status": "connected"} for i in range(65)]},
        {"skills": [{"name": "x" * 129}]},
        {"skills": [{"name": f"s{i}"} for i in range(257)]},
        {"commands": [{"name": " "}]},
        {"commands": ["init"]},
    ],
)
def test_ipc_refuses_an_environment_outside_its_bounds(data):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "environment", "data": data})


def test_ipc_tool_call_fields_are_bounded():
    base = {"item_id": "i", "operation_id": str(uuid.uuid4()), "native_turn_id": "t"}
    ok = {**base, "tool": "Bash", "call": True, "mcp_server": "", "skill": "notes"}
    assert native_ipc.normalize_event({"kind": "tool", "data": ok})["data"] == ok
    for bad in ({"call": "yes"}, {"mcp_server": "x" * 129}, {"skill": ""}):
        with pytest.raises(native_ipc.IPCError):
            native_ipc.normalize_event({"kind": "tool", "data": {**base, **bad}})


# --- the snapshot: lifetimes, pagination, counts --------------------------------------------


def plan_event(*texts, document=None):
    data = {}
    if texts:
        data["entries"] = [{"text": t, "status": "pending"} for t in texts]
    if document is not None:
        data["document"] = document
    return {"kind": "plan", "data": data}


def env_event(*servers):
    return {
        "kind": "environment",
        "data": {"skill_use": True, "mcp": [{"name": s, "status": "connected"} for s in servers]},
    }


def view(root, sid, binding, live="self", **kwargs):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id if live == "self" else live,
        **kwargs,
    )
    return structured_runtime._snapshot(binding.session_key, raw)


def test_empty_states_say_nothing_was_shared(conversation):
    root, sid, binding = conversation
    native = view(root, sid, binding)["native"]
    assert native["plan"] is None and native["environment"] is None
    assert native["counts"] == native_events.Counts().view()


def test_plan_survives_worker_replacement_and_every_page_of_history(conversation, monkeypatch):
    root, sid, binding = conversation
    first = [operator(root, sid, binding, monkeypatch, T0 + i, f"op {i}") for i in range(3)]
    at(monkeypatch, T0 + 10)
    journal.append_events(root, binding, [plan_event("old step")])
    journal.append_events(root, binding, [plan_event("Read", "Write"), plan_event(document="# P")])
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    later = [operator(root, sid, successor, monkeypatch, T0 + 20 + i, f"new {i}") for i in range(3)]
    expected = {
        "entries": [{"text": "Read", "status": "pending"}, {"text": "Write", "status": "pending"}],
        "entries_at": T0 + 10,
        "document": "# P",
        "document_at": T0 + 10,
        # Written by the replaced generation: the live one has not re-confirmed it (#1811).
        "stale": True,
    }
    # The live successor, a one-turn window, a page of old history; and no live worker at all.
    assert view(root, sid, successor)["native"]["plan"] == expected
    assert view(root, sid, successor, max_turns=1)["native"]["plan"] == expected
    older = view(root, sid, successor, max_turns=2, before=later[0])
    assert [t["turn_id"] for t in older["turns"]] == first[1:]
    assert older["native"]["plan"] == expected
    assert view(root, sid, successor, live=None)["native"]["plan"] == {**expected, "stale": False}
    # A newer checklist replaces the checklist only; the document stays the newest document.
    journal.append_events(root, successor, [plan_event("Ship")])
    plan = view(root, sid, successor)["native"]["plan"]
    assert plan["entries"] == [{"text": "Ship", "status": "pending"}] and plan["document"] == "# P"


def test_environment_belongs_to_the_live_generation_only(conversation):
    root, sid, binding = conversation
    journal.append_events(root, binding, [env_event("old"), env_event("github")])
    assert view(root, sid, binding)["native"]["environment"]["mcp"] == [
        {"name": "github", "status": "connected"}
    ]
    # The generation ended (no live worker) or was replaced (a successor that has not reported).
    assert view(root, sid, binding, live=None)["native"]["environment"] is None
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    assert view(root, sid, successor)["native"]["environment"] is None
    journal.append_events(root, successor, [env_event("linear")])
    assert view(root, sid, successor)["native"]["environment"]["mcp"][0]["name"] == "linear"


def call(operation_id, item, tool, **extra):
    return {
        "kind": "tool",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "item_id": item,
            "tool": tool,
            "state": "running",
            "summary": "{}",
            "call": True,
            **extra,
        },
    }


def counted_session(root, sid, binding, monkeypatch, turns, calls):
    """``turns`` operator turns, each with ``calls`` Bash calls (+ their results, which never
    count again), one MCP call to `github`, one unattributed MCP call and one skill use."""
    for t in range(turns):
        at(monkeypatch, T0 + t)
        claim = journal_tests.request(binding, revision=journal.read(root, sid).revision)
        op = claim["params"]["operation_id"]
        journal.claim(root, claim)
        journal.record_handoff(root, binding, op, "sent")
        batch = []
        for i in range(calls):
            batch.append(call(op, f"b{i}", "Bash"))
            result = call(op, f"b{i}", "Bash")
            del result["data"]["call"]
            batch.append(result)
        batch += [
            call(op, "m1", "mcp__github__issues", mcp_server="github"),
            call(op, "m2", "mcp__mystery__x", mcp_server=""),
            call(op, "s1", "Skill", skill="release-notes"),
        ]
        for start in range(0, len(batch), native_ipc.MAX_EVENTS):
            journal.append_events(root, binding, batch[start : start + native_ipc.MAX_EVENTS])


def test_counts_are_exact_past_the_turn_window_and_the_per_turn_truncation(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    turns = structured_runtime.SNAPSHOT_TURNS + 5
    calls = native_runtime.TOOLS_SHOWN + 6
    counted_session(root, sid, binding, monkeypatch, turns, calls)
    snap = view(root, sid, binding, max_turns=structured_runtime.SNAPSHOT_TURNS)
    assert snap["omitted_turns"] > 0 and snap["turns"][-1]["tools_truncated"] is True
    counts = snap["native"]["counts"]
    assert counts["tools"] == [
        {"name": "Bash", "calls": turns * calls},
        {"name": "Skill", "calls": turns},
    ]
    assert counts["servers"] == [{"name": "github", "calls": turns}]
    assert counts["unattributed"] == turns
    assert counts["skills"] == [{"name": "release-notes", "calls": turns}]
    assert counts["tools_other"] == counts["servers_other"] == counts["skills_other"] == 0


def test_counter_overflows_into_other_and_rebuilds_with_the_same_totals(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(native_events, "MAX_COUNTED_TOOLS", 3)
    monkeypatch.setattr(native_events, "MAX_COUNTED_SERVERS", 1)
    monkeypatch.setattr(native_events, "MAX_COUNTED_SKILLS", 1)
    at(monkeypatch, T0)
    claim = journal_tests.request(binding, revision=journal.read(root, sid).revision)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    batch = [call(op, f"t{i}", f"Tool{i}") for i in range(5)]
    batch += [
        call(op, "m1", "mcp__a__x", mcp_server="a"),
        call(op, "m2", "mcp__b__y", mcp_server="b"),
    ]
    batch += [call(op, "s1", "Skill", skill="one"), call(op, "s2", "Skill", skill="two")]
    journal.append_events(root, binding, batch)
    first = view(root, sid, binding)["native"]["counts"]
    assert [r["name"] for r in first["tools"]] == ["Tool0", "Tool1", "Tool2"]
    assert first["tools_other"] == 4  # Tool3, Tool4 and both Skill calls
    assert first["servers"] == [{"name": "a", "calls": 1}] and first["servers_other"] == 1
    assert first["skills"] == [{"name": "one", "calls": 1}] and first["skills_other"] == 1
    # The index is derived: drop every cached fold and read the journal bytes again.
    journal._CACHE.clear()
    journal._CHAIN.clear()
    journal._FROZEN.clear()
    assert view(root, sid, binding)["native"]["counts"] == first
    # Stale: a cached prefix extended by newer records counts them once, like a full fold.
    journal.append_events(root, binding, [call(op, "t9", "Tool0")])
    extended = view(root, sid, binding)["native"]["counts"]
    journal._CACHE.clear()
    assert view(root, sid, binding)["native"]["counts"] == extended
    assert extended["tools"][0] == {"name": "Tool0", "calls": 2}


# --- the public link ------------------------------------------------------------------------


def test_public_snapshot_omits_plan_and_environment_and_keeps_todays_tool_row(conversation):
    root, sid, binding = conversation
    claim = journal_tests.request(binding)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    todos = {"todos": [{"content": "Rotate the PRIVATE-PLAN key", "status": "pending"}]}
    summary = json.dumps(todos, separators=(",", ":"))
    journal.append_events(
        root,
        binding,
        [
            call(op, "toolu_1", "TodoWrite", summary=summary),
            plan_event("Rotate the PRIVATE-PLAN key", document="PRIVATE-DOC"),
            env_event("PRIVATE-SERVER"),
        ],
    )
    operator_view = view(root, sid, binding)
    assert operator_view["native"]["plan"] and operator_view["native"]["environment"]
    public = share_api.project(operator_view, {}, share_api._Redactor([]), "link")
    text = json.dumps(public)
    assert "native" not in public and "plan" not in public and "environment" not in public
    assert "PRIVATE-DOC" not in text and "PRIVATE-SERVER" not in text and "counts" not in text
    # Pinned on purpose: the plan-bearing tool row is exactly today's redacted name/summary.
    (turn,) = public["turns"]
    assert turn["tools"] == [{"name": "TodoWrite", "outcome": "running", "summary": summary}]
    page = {"revision": 9, "next_cursor": 9, "events": [{"cursor": 8, **plan_event("x")}]}
    assert share_api.project_events(page, "link")["events"] == [{"cursor": 8, "kind": "plan"}]


def test_startup_reports_of_a_new_generation_are_no_missed_activity(conversation):
    root, sid, binding = conversation
    before = journal.read(root, sid).revision
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    journal.append_events(root, successor, [env_event("git"), plan_event("replayed")])
    after = journal.read(root, sid)
    assert native_runtime._nothing_to_review(after, before, binding.worker_id, successor.worker_id)


def test_protocol_bounds_are_the_ipc_bounds():
    assert native_protocol.PLAN_ENTRIES == 100 and native_protocol.PLAN_TEXT == 500
    assert native_protocol.ENV_MCP == 64 and native_protocol.ENV_SKILLS == 256
    assert native_protocol.ENV_COMMANDS == 256 and native_protocol.ENV_NAME == 128
    entries = [{"content": f"step {i}", "status": "pending"} for i in range(150)]
    long = native_protocol._plan_entries([{"content": "x" * 900, "status": "done"}], "content")
    assert len(native_protocol._plan_entries(entries, "content")) == 100
    assert long == [{"text": "x" * 499 + "…", "status": "completed"}]


# --- review on #1811 ------------------------------------------------------------------------


def _opencode_in_turn():
    codec = OpencodeAcpCodec("ses_abc", agent=AGENT)
    codec.submit("go", str(uuid.uuid4()))
    return codec


def test_an_overlong_opencode_server_name_is_cut_once_for_listing_and_counting():
    """A probe name past ENV_NAME (with stray spaces) is cut and stripped ONCE, so the listed
    name, the counted name and the journal bound always agree."""
    codec = OpencodeAcpCodec(agent=AGENT)
    codec.configured_mcp([("  " + "s" * 200 + "  ", "configured")])
    new = codec.create("/w")
    shown = native_protocol._cut("s" * 200, native_protocol.ENV_NAME)
    env = codec.feed(
        {"jsonrpc": "2.0", "id": new["id"], "result": {"sessionId": "ses_abc", "configOptions": []}}
    )
    (listed,) = [e for e in normalized(env) if e["kind"] == "environment"]
    assert listed["data"]["mcp"] == [{"name": shown, "status": "configured"}]
    codec._pinned = True
    codec.submit("go", str(uuid.uuid4()))
    title = f"{'s' * 200}_labels"
    update = {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "other", "title": title}
    (tool,) = of(normalized(_acp(codec, update)), "tool")
    assert tool["data"]["mcp_server"] == shown


def test_a_refused_observation_never_loses_the_batch_or_stops_flushing(conversation):
    from types import SimpleNamespace

    from agent_sessions import native_worker

    root, sid, binding = conversation
    bad = {"kind": "plan", "data": {"entries": [{"text": "x" * 900, "status": "pending"}]}}
    good = plan_event("kept")
    fake = SimpleNamespace(
        pending=[bad, good],
        journal_root=root,
        binding=binding,
        reason=None,
        journal_failed=lambda exc: pytest.fail(f"journal failed: {exc}"),
        stop_child=lambda: pytest.fail("the child was stopped"),
    )
    native_worker.Worker.flush(fake)
    assert fake.pending == []
    kinds = [e["event"]["kind"] for e in journal.read(root, sid).events]
    assert kinds == ["error", "plan"]
    assert view(root, sid, binding)["native"]["plan"]["entries"][0]["text"] == "kept"


def test_claude_task_list_it_cannot_reconcile_is_marked_stale_not_left_live(conversation):
    codec = ClaudeCodec(str(uuid.uuid4()))
    codec.submit("go", str(uuid.uuid4()))
    create = {"type": "tool_use", "id": "t1", "name": "TaskCreate", "input": {"subject": "A"}}
    (plan,) = of(_claude_turn(codec, [create], {"task": {"id": "1", "subject": "A"}}), "plan")
    assert plan["data"]["entries"] == [{"text": "A", "status": "pending"}]
    # An update for a task this connection never saw: the list is unknown — stale, once.
    update = {"type": "tool_use", "id": "t2", "name": "TaskUpdate"}
    update["input"] = {"taskId": "9", "status": "completed"}
    (stale,) = of(_claude_turn(codec, [update], {"success": True}, "t2"), "plan")
    assert stale["data"] == {"stale": True}
    again = {"type": "tool_use", "id": "t3", "name": "TaskUpdate"}
    again["input"] = {"taskId": "1", "status": "completed"}
    assert of(_claude_turn(codec, [again], {"success": True}, "t3"), "plan") == []
    # A TaskList answer makes it whole again.
    listed = {"type": "tool_use", "id": "t4", "name": "TaskList", "input": {}}
    (whole,) = of(
        _claude_turn(codec, [listed], {"tasks": [{"id": "1", "subject": "A"}]}, "t4"), "plan"
    )
    assert "stale" not in whole["data"]


def test_a_task_result_that_is_lost_or_refused_makes_the_list_stale():
    codec = ClaudeCodec(str(uuid.uuid4()))
    codec.submit("go", str(uuid.uuid4()))
    create = {"type": "tool_use", "id": "t1", "name": "TaskCreate", "input": {"subject": "A"}}
    _claude_turn(codec, [create], {"task": {"id": "1", "subject": "A"}})
    update = {"type": "tool_use", "id": "t2", "name": "TaskUpdate"}
    update["input"] = {"taskId": "1", "status": "completed"}
    _claude_turn(codec, [update])
    events = normalized(
        codec.feed(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": "x",
                            "is_error": True,
                        }
                    ],
                },
            }
        )
    )
    assert [e["data"] for e in of(events, "plan")] == [{"stale": True}]


def test_a_subagents_task_call_makes_the_main_list_stale():
    codec = ClaudeCodec(str(uuid.uuid4()))
    codec.submit("go", str(uuid.uuid4()))
    create = {"type": "tool_use", "id": "t1", "name": "TaskCreate", "input": {"subject": "A"}}
    _claude_turn(codec, [create], {"task": {"id": "1", "subject": "A"}})
    sub = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_agent",
        "message": {
            "id": "m9",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "s1", "name": "TaskUpdate", "input": {"taskId": "1"}}
            ],
        },
    }
    assert [e["data"] for e in of(normalized(codec.feed(sub)), "plan")] == [{"stale": True}]


def test_a_plan_from_an_earlier_generation_or_marked_stale_is_not_live(conversation):
    root, sid, binding = conversation
    journal.append_events(root, binding, [plan_event("A")])
    assert view(root, sid, binding)["native"]["plan"]["stale"] is False
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    assert view(root, sid, successor)["native"]["plan"]["stale"] is True
    journal.append_events(root, successor, [plan_event("B")])
    assert view(root, sid, successor)["native"]["plan"]["stale"] is False
    journal.append_events(root, successor, [{"kind": "plan", "data": {"stale": True}}])
    plan = view(root, sid, successor)["native"]["plan"]
    assert plan["stale"] is True and plan["entries"][0]["text"] == "B"


def test_opencode_counts_tools_by_name_not_by_acp_kind():
    _, events = replay_opencode("opencode_plan_environment.jsonl", PROBE)
    counts = native_events.Counts()
    for e in of(events, "tool"):
        counts.observe(e["data"])
    view_ = counts.view()
    assert {r["name"] for r in view_["tools"]} == {"todowrite", "skill"}
    assert view_["servers"] == [{"name": "fixture", "calls": 1}]


def test_codex_with_no_mcp_servers_reports_none_configured():
    codec = CodexCodec()
    codec.initialize()
    start = codec.create("/w")
    sends = [
        e.data["frame"]
        for e in codec.feed({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
        if e.kind == "send"
    ]
    page = next(f for f in sends if f["method"] == "mcpServerStatus/list")
    (env,) = codec.feed({"id": page["id"], "result": {"data": [], "nextCursor": None}})
    assert env.kind == "environment" and env.data["mcp"] == []


def test_codex_skills_changed_keeps_one_request_in_flight():
    codec = CodexCodec()
    codec.initialize()
    start = codec.create("/w")
    sends = [
        e.data["frame"]
        for e in codec.feed({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
        if e.kind == "send"
    ]
    first = next(f for f in sends if f["method"] == "skills/list")
    for _ in range(300):  # never a request per change, never a capacity error
        assert codec.feed({"method": "skills/changed", "params": {}}) == []
    again = [e for e in codec.feed({"id": first["id"], "result": {"data": []}}) if e.kind == "send"]
    assert [f.data["frame"]["method"] for f in again] == ["skills/list"]
    # A refused discovery is harmless too.
    assert (
        codec.feed({"id": again[0].data["frame"]["id"], "error": {"code": 1, "message": "x"}}) == []
    )


# --- Hermes on #1811 ------------------------------------------------------------------------


def test_opencode_skill_use_reaches_the_folded_counts_once_per_call(conversation):
    """opencode reports the skill a call loads on a LATER update than the one that starts the
    call: the folded snapshot counts it as one use of that skill, and never as a second call."""
    root, sid, binding = conversation
    claim = journal_tests.request(binding)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    _, events = replay_opencode("opencode_plan_environment.jsonl", PROBE)
    observed = []
    for e in events:
        data = dict(e["data"])
        if "operation_id" in data:
            data["operation_id"] = op
        observed.append({"kind": e["kind"], "data": data})
    for start in range(0, len(observed), native_ipc.MAX_EVENTS):
        journal.append_events(root, binding, observed[start : start + native_ipc.MAX_EVENTS])
    # The same reported use journaled again (a replayed update) is still one use.
    skill = next(o for o in observed if o["data"].get("skill"))
    journal.append_events(root, binding, [skill])
    counts = view(root, sid, binding)["native"]["counts"]
    assert counts["skills"] == [{"name": "release-notes", "calls": 1}]
    assert {r["name"]: r["calls"] for r in counts["tools"]} == {"todowrite": 1, "skill": 1}


def test_a_clients_status_reports_never_refuse_a_send_at_the_seen_revision(conversation):
    """#1801's rule for #1726's events: the live worker's `environment` report and `stale` marker
    are status, whatever generation; a plan's own checklist or document keeps the refusal. #1724's
    inventory keeps its handling."""
    root, sid, binding = conversation
    seen = journal.read(root, sid).revision
    worker = binding.worker_id
    journal.append_events(
        root, binding, [env_event("git"), {"kind": "plan", "data": {"stale": True}}]
    )
    assert native_runtime._nothing_to_review(journal.read(root, sid), seen, worker, worker)
    # Reported by an old generation too: status, whoever wrote it.
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    journal.append_events(root, successor, [env_event("linear")])
    assert native_runtime._nothing_to_review(journal.read(root, sid), seen, worker, worker)
    assert native_runtime._status_record({"kind": "background_inventory", "data": {}})
    # What the agent says it will do is reviewable once the generation is past its startup...
    journal.append_events(root, binding, [plan_event("Ship it")])
    assert not native_runtime._nothing_to_review(journal.read(root, sid), seen, worker, worker)
    # ...and a new generation's startup report of it stays no missed activity.
    other = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    before = journal.read(root, sid).revision
    journal.append_events(root, other, [plan_event("Replayed")])
    assert native_runtime._nothing_to_review(
        journal.read(root, sid), before, worker, other.worker_id
    )
