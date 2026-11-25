"""#1891: opencode API subagents — no unanswerable ask, a background-strip row, and Send now
never cancels one.

opencode 1.18.35's `task` tool runs a subagent in a child session inside the parent turn. Over ACP
the client sees only the parent's tool call (pending → in_progress with `description`,
`subagent_type` → completed/failed), never the child's own frames, and never a child's permission
request: an ask there waits for ever (measured on #1891). So:

* bypass gives the subagents the parent's all-allow (the config-level block); guarded refuses
  `task` outright;
* each running `task` call is a background task (`native.background_tasks`, the #1726 strip);
* while one runs, Send now — a `session/cancel` here — is refused `would_stop_background`, the
  message keeps its place, and only Stop all and send (#1644) cuts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

import test_native_journal as journal_tests
import test_native_runtime as rt
from agent_sessions import native_events, native_ipc, native_journal, native_protocol, native_worker
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import opencode as opencode_engine
from test_native_journal import conversation  # noqa: F401
from test_native_opencode import AGENT, SES, _codec, _create

host, project = rt.host, rt.project
frames, ident, settle = rt.frames, rt.ident, rt.settle

CALL = "call-task"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- the permission policy ---------------------------------------------------------------------


def test_bypass_reaches_the_subagents_through_the_config_level_block():
    config = json.loads(opencode_engine.api_config_content(AGENT, bypass=True))
    own = config["agent"][AGENT]["permission"]
    assert set(own.values()) == {"allow"}
    # What a `general` / `explore` subagent runs under: no longer `{"*": "ask"}`.
    assert config["permission"] == own
    assert next(iter(config["permission"])) == "*"


def test_guarded_refuses_task_and_asks_everything_else():
    config = json.loads(opencode_engine.api_config_content(AGENT))
    own = config["agent"][AGENT]["permission"]
    assert own["task"] == "deny"
    assert {k for k, v in own.items() if v != "ask"} == {"task"}
    assert next(iter(own)) == "*" and own["*"] == "ask"
    assert config["permission"] == {"*": "ask"}  # unchanged: nothing else may allow
    # The shared template is not mutated by either build.
    assert opencode_engine.API_PERMISSION["task"] == "ask"


def _merged(operator, ours):
    """opencode's key-by-key deep merge of OPENCODE_CONFIG_CONTENT over the operator's block:
    THEIR keys keep their position, ours replace values (a pattern map merges pattern-wise)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in operator.items()}
    for key, value in ours.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def _last_match(block, permission, pattern="x"):
    """opencode's evaluation: the LAST rule whose permission (and pattern) matches."""
    import fnmatch

    action = None
    for key, value in block.items():
        if not fnmatch.fnmatchcase(permission, key):
            continue
        if isinstance(value, dict):
            for pat, act in value.items():
                if fnmatch.fnmatchcase(pattern, pat):
                    action = act
        else:
            action = value
    return action


def test_bypass_restates_every_operator_config_level_key_as_allow():
    """Review on #1891: the operator's `{"*": "ask", "github_*": "ask"}` keeps its order under the
    merge, so `github_*` would follow our `*` and win, and a subagent's MCP call would hang."""
    operator = {
        "*": "ask",
        "github_*": "ask",
        "read": {"*": "allow", "*.env": "ask"},
        "bash": "deny",
    }
    unfixed = json.loads(opencode_engine.api_config_content(AGENT, bypass=True))["permission"]
    assert _last_match(_merged(operator, unfixed), "github_create_issue") == "ask"  # the hole
    config = json.loads(
        opencode_engine.api_config_content(AGENT, bypass=True, operator_permission=operator)
    )
    merged = _merged(operator, config["permission"])
    for permission, pattern in [
        ("github_create_issue", "x"),
        ("read", "secrets.env"),
        ("bash", "rm -rf x"),
        ("webfetch", "x"),
        ("some_mcp_tool", "x"),
    ]:
        assert _last_match(merged, permission, pattern) == "allow", permission
    # Guarded never reads it: the config-level block stays all-ask.
    guarded = opencode_engine.api_config_content(AGENT, operator_permission=operator)
    assert json.loads(guarded)["permission"] == {"*": "ask"}


@pytest.mark.parametrize("block", [None, "ask", ["*"], {1: "ask"}])
def test_an_unusable_operator_block_adds_nothing(block):
    config = json.loads(
        opencode_engine.api_config_content(AGENT, bypass=True, operator_permission=block)
    )
    assert set(config["permission"].values()) == {"allow"}


def test_the_worker_passes_the_probed_operator_block_to_the_child(tmp_path):
    from test_native_opencode import _config

    config = _config(tmp_path, bypass=True)
    worker = native_worker.Worker(config, tmp_path)
    operator = {"*": "ask", "github_*": "ask"}
    env = native_worker.child_environment(
        config, worker._server_password, worker.agent, operator_permission=operator
    )
    forced = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert forced["permission"]["github_*"] == "allow"


# --- the codec ---------------------------------------------------------------------------------


def _running(codec, op=None):
    op = op or ident()
    codec.submit("research", op)
    return op


def _tool(codec, update_kind="tool_call_update", /, **update):
    return codec.feed(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SES, "update": {"sessionUpdate": update_kind, **update}},
        }
    )


START = {"toolCallId": CALL, "kind": "think", "title": "task", "status": "pending", "rawInput": {}}
RUNNING = {
    "toolCallId": CALL,
    "kind": "think",
    "title": "Research flights",
    "status": "in_progress",
    "rawInput": {"description": "Research flights", "prompt": "p", "subagent_type": "general"},
}


def _background(events):
    return [e.data for e in events if e.kind == "background"]


def test_a_task_call_is_a_background_task_from_its_start_to_its_end():
    codec = _codec()
    _running(codec)
    # Pending, it names no subagent yet: nothing to show, nothing a Send now would end.
    assert _background(_tool(codec, "tool_call", **START)) == []
    assert not codec.send_now_would_stop_background()
    [started] = _background(_tool(codec, **RUNNING))
    assert started == {
        "task_id": CALL,
        "state": "running",
        "task_state": "running",
        "active": True,
        "description": "Research flights",
        "task_type": "general",
    }
    assert codec.send_now_would_stop_background()
    assert _background(_tool(codec, **RUNNING)) == []  # the repeated update says nothing new
    [ended] = _background(_tool(codec, **{**RUNNING, "status": "completed"}))
    assert ended == {
        "task_id": CALL,
        "state": "completed",
        "task_state": "completed",
        "active": False,
    }
    assert not codec.send_now_would_stop_background()
    assert codec._background == set()


def test_a_failed_task_call_ends_failed():
    codec = _codec()
    _running(codec)
    _tool(codec, "tool_call", **START)
    _tool(codec, **RUNNING)
    [ended] = _background(_tool(codec, **{**RUNNING, "status": "failed"}))
    assert ended["state"] == "failed" and not codec._background


def test_other_tools_are_never_background_tasks():
    codec = _codec()
    _running(codec)
    bash = {"toolCallId": "call-b", "kind": "execute", "title": "bash", "status": "pending"}
    assert _background(_tool(codec, "tool_call", **bash)) == []
    # A later title of "task", or a subagent-shaped input, on another tool's call: still not one.
    later = {**bash, "title": "task", "status": "in_progress", "rawInput": RUNNING["rawInput"]}
    assert _background(_tool(codec, **later)) == []
    assert not codec.send_now_would_stop_background()


def _recorded(name):
    """The real CLI's `task` call frames (`fixtures/opencode_subagent.PROVENANCE.md`), rebound to
    the codec's session."""
    out = []
    for line in (FIXTURES / name).read_text().splitlines():
        frame = json.loads(line)["frame"]
        if frame.get("method") == "session/update" and frame["params"]["update"].get(
            "sessionUpdate"
        ) in {"tool_call", "tool_call_update"}:
            out.append({**frame, "params": {**frame["params"], "sessionId": SES}})
    return out


def test_the_recorded_bypass_subagent_is_one_row_from_its_input_to_its_completion():
    codec = _codec()
    _running(codec)
    seen = [_background(codec.feed(frame)) for frame in _recorded("opencode_subagent_bypass.jsonl")]
    assert [len(events) for events in seen] == [0, 1, 0, 1]
    started, ended = seen[1][0], seen[3][0]
    assert started["description"] == "Probe subagent" and started["task_type"] == "general"
    assert ended["state"] == "completed" and ended["task_id"] == started["task_id"]
    assert not codec._background


def test_a_call_to_the_denied_task_tool_is_never_a_row():
    """Guarded (`task: deny`): opencode offers no `task`, and a call the model makes anyway is
    retitled `invalid` (recorded)."""
    codec = _codec()
    _running(codec)
    for frame in _recorded("opencode_subagent_guarded.jsonl"):
        assert _background(codec.feed(frame)) == []
        assert not codec.send_now_would_stop_background()


@pytest.mark.parametrize("reason", ["cancelled", "end_turn", "max_tokens"])
def test_no_subagent_outlives_its_turn(reason):
    codec = _codec()
    op = _running(codec)
    prompt = next(rid for rid, (action, o) in codec._requests.items() if o == op)
    _tool(codec, "tool_call", **START)
    _tool(codec, **RUNNING)
    events = codec.feed({"jsonrpc": "2.0", "id": prompt, "result": {"stopReason": reason}})
    kinds = [e.kind for e in events]
    assert kinds == ["background", "turn_completed"]  # ended BEFORE the turn's end
    assert events[0].data == {
        "task_id": CALL,
        "state": "stopped",
        "task_state": "stopped",
        "active": False,
    }
    assert not codec.send_now_would_stop_background()


def test_a_lost_prompt_reply_ends_the_subagents_with_the_uncertain_turn():
    codec = _codec()
    op = _running(codec)
    prompt = next(rid for rid, (action, o) in codec._requests.items() if o == op)
    _tool(codec, "tool_call", **START)
    _tool(codec, **RUNNING)
    head = ('{"jsonrpc":"2.0","id":"' + prompt + '","result":{"stopReason":"').encode()
    events = codec.frame_lost(head, b'end_turn"}}')
    kinds = [e.kind for e in events]
    assert "turn_completed" in kinds
    stopped = [e.data for e in events if e.kind == "background"]
    assert stopped and stopped[0]["task_id"] == CALL and stopped[0]["state"] == "stopped"
    assert kinds.index("background") < kinds.index("turn_completed")
    assert not codec._background and not codec.send_now_would_stop_background()


def test_more_subagents_than_tracked_say_so_once_and_the_turn_end_clears_it():
    codec = _codec()
    op = _running(codec)
    prompt = next(rid for rid, (action, o) in codec._requests.items() if o == op)
    for i in range(native_protocol.MAX_PENDING):
        call = {**RUNNING, "toolCallId": f"call-{i}"}
        _tool(codec, "tool_call", **{**call, "title": "task", "status": "pending", "rawInput": {}})
        _tool(codec, **call)
    assert len(codec._background) == native_protocol.MAX_PENDING and not codec.overflow
    extra = {**RUNNING, "toolCallId": "call-extra"}
    _tool(codec, "tool_call", **{**extra, "title": "task", "status": "pending", "rawInput": {}})
    [notice] = _background(_tool(codec, **extra))
    assert notice == {"state": "activity", "active": True} and codec.overflow
    assert _background(_tool(codec, **extra)) == []  # said once
    assert codec.send_now_would_stop_background()
    codec.feed({"jsonrpc": "2.0", "id": prompt, "result": {"stopReason": "end_turn"}})
    assert not codec.overflow and not codec._background
    assert not codec.send_now_would_stop_background()


def test_an_update_after_its_turn_ended_never_tracks_the_subagent_again():
    codec = _codec()
    op = _running(codec)
    prompt = next(rid for rid, (action, o) in codec._requests.items() if o == op)
    _tool(codec, "tool_call", **START)
    _tool(codec, **RUNNING)
    codec.feed({"jsonrpc": "2.0", "id": prompt, "result": {"stopReason": "cancelled"}})
    _running(codec)  # the next turn
    assert _background(_tool(codec, **RUNNING)) == []
    assert not codec.send_now_would_stop_background()


def test_a_replayed_task_call_is_not_tracked():
    codec = _codec()  # no turn: `session/load` history and idle chatter
    assert _tool(codec, "tool_call", **START) == []
    assert not codec._background


def test_the_journal_fold_shows_the_row_while_it_runs_and_drops_it_after():
    codec = _codec()
    _running(codec)
    tasks, ended = {}, set()
    for event in [*_tool(codec, "tool_call", **START), *_tool(codec, **RUNNING)]:
        if event.kind == "background":
            native_events.observe_task(tasks, ended, event.data, 1.0)
    assert tasks[CALL]["description"] == "Research flights"
    assert tasks[CALL]["task_type"] == "general" and tasks[CALL]["state"] == "running"
    for event in _tool(codec, **{**RUNNING, "status": "completed"}):
        if event.kind == "background":
            native_events.observe_task(tasks, ended, event.data, 2.0)
    assert tasks == {} and CALL in ended


# --- the worker: Send now keeps the message, Stop all and send cuts it -------------------------


def _subagent_worker(conversation, monkeypatch):  # noqa: F811
    root, sid, binding = conversation
    worker = native_worker.Worker(
        {
            "worker_id": binding.worker_id,
            "session_key": binding.session_key,
            "adapter": binding.adapter,
            "journal_root": str(root),
            "capability": native_ipc.Capability.create()._value,
            "connection_id": binding.connection_id,
        },
        root,
    )
    codec = _codec()
    worker.codec = codec
    worker.ready.set()
    monkeypatch.setattr(worker, "gate_open", lambda: True)
    monkeypatch.setattr(worker, "queued_admission", lambda *_a: contextlib.nullcontext())
    active = journal_tests.request(binding)
    native_journal.claim(root, active)
    codec.submit("research", active["params"]["operation_id"])
    _tool(codec, "tool_call", **START)
    _tool(codec, **RUNNING)
    queued = journal_tests.request(binding, revision=native_journal.read(root, sid).revision)
    native_journal.claim(root, queued, queued=True)
    worker.queued.append(queued["params"])
    control = journal_tests.request(
        binding,
        action="send_now",
        turn_id=codec.operation_id,
        queued_turn_id=queued["params"]["operation_id"],
        mode="interrupt",
        revision=native_journal.read(root, sid).revision,
    )
    writes = []

    async def write(frame):
        writes.append(frame)

    monkeypatch.setattr(worker, "write", write)
    return worker, queued, control, writes


@pytest.mark.anyio
async def test_send_now_while_a_subagent_runs_keeps_the_message_and_cancels_nothing(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, queued, control, writes = _subagent_worker(conversation, monkeypatch)
    assert worker.send_now_state() == "would_stop_background"
    result = await worker.effect(control)
    assert result["error"]["reason"] == "would_stop_background"
    assert writes == []  # no session/cancel
    assert worker.queued == [queued["params"]]
    assert worker.engine_ids() == [CALL]  # what Stop all and send would confirm


@pytest.mark.anyio
async def test_stop_all_and_send_with_the_subagent_confirmed_cancels_the_turn(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, queued, control, writes = _subagent_worker(conversation, monkeypatch)
    params = dict(control["params"])
    params.update(
        mode="stop_all",
        confirm_worker_id=worker.worker_id,
        confirm_tasks=native_ipc.tasks_digest([CALL]),
    )
    result = await worker.effect({**control, "params": params})
    assert result["result"]["handoff"] == "sent"
    (frame,) = writes
    assert frame["method"] == "session/cancel"
    assert worker.queued == [queued["params"]]  # sent once the cancelled turn ends


# --- end to end: the real worker against the scripted opencode ---------------------------------


async def _until(key, check, *, tries=300):
    for _ in range(tries):
        snap = await runtime.snapshot(key)
        if check(snap):
            return snap
        await asyncio.sleep(0.02)
    raise AssertionError(f"never reached: {snap['native']}")


def _rows(snap):
    return [t for t in snap["native"].get("background_tasks", []) if t.get("source") == "engine"]


@pytest.mark.anyio
async def test_a_running_subagent_is_a_strip_row_and_send_now_never_cuts_it(host, project):
    key = await _create(project)
    active, urgent = ident(), ident()
    await runtime.submit_turn(key, operation_id=active, text="SUBAGENT")
    snap = await _until(key, lambda s: any(t.get("description") for t in _rows(s)))
    [row] = _rows(snap)
    assert row["task_id"] == CALL and row["state"] == "running"
    assert row["description"] == "Research flights" and row["task_type"] == "general"
    snap = await _until(key, lambda s: s["native"]["send_now_state"] == "would_stop_background")
    await runtime.submit_turn(key, operation_id=urgent, text="Done?")
    with pytest.raises(runtime.StructuredError):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    sent = [f["frame"] for f in frames(project) if "frame" in f]
    assert not any(f.get("method") == "session/cancel" for f in sent)
    snap = await runtime.snapshot(key)
    assert next(t for t in snap["turns"] if t["turn_id"] == urgent)["state"] == "queued"
    assert [t["task_id"] for t in _rows(snap)] == [CALL]  # still running
    # The operator's own Stop ends it, and the row with it.
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    snap, _ = await settle(key, active, state=("interrupted",))
    snap = await _until(key, lambda s: _rows(s) == [])
    assert snap["native"]["send_now_state"] != "would_stop_background"


@pytest.mark.anyio
async def test_a_finished_subagent_leaves_the_strip(host, project):
    key = await _create(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="SUBAGENT_DONE")
    snap, settled = await settle(key, turn)
    assert settled["reply"] == "done"
    assert _rows(snap) == []
    events = (await runtime.events(key, after=0))["events"]
    seen = [e["data"] for e in events if e["kind"] == "background" and e["data"].get("task_id")]
    assert seen[0]["state"] == "running" and seen[-1]["state"] == "completed"
