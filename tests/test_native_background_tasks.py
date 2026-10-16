"""Background task observations explain the worker without authorizing or attributing a turn."""

import json
import uuid
from dataclasses import replace

import pytest

import test_native_journal as journal_tests
from agent_sessions import (
    native_ipc,
    native_protocol,
    native_runtime,
    share_api,
    structured_runtime,
)
from agent_sessions import native_journal as journal
from agent_sessions.native_protocol import ClaudeCodec

conversation = journal_tests.conversation
T0 = 1_791_790_000.125
SESSION = str(uuid.uuid4())


def task(state="running", *, task_id="build-monitor", **metadata):
    return {
        "kind": "background",
        "data": {"task_id": task_id, "state": state, "active": True, **metadata},
    }


def append(root, binding, monkeypatch, t, observation):
    monkeypatch.setattr(journal.time, "time", lambda: t)
    journal.append_events(root, binding, [observation])


def snapshot(root, sid, binding, *, live="self", max_turns=None):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id if live == "self" else live,
        max_turns=max_turns,
    )
    return structured_runtime._snapshot(binding.session_key, raw)


def test_claude_preserves_reported_activity_without_raw_task_payloads():
    codec = ClaudeCodec(SESSION)
    operation = str(uuid.uuid4())
    codec.submit("Check the deployment", operation)
    (started,) = codec.feed(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "build-monitor",
            "description": "Monitor RTM build 39991",
            "task_type": "local_bash",
            "output_file": "/private/task-output",
            "tool_use_id": "toolu-monitor",
            "native_secret": "never copy this",
        }
    )
    assert started.data == {
        "task_id": "build-monitor",
        "state": "running",
        "active": True,
        "task_state": "running",
        "description": "Monitor RTM build 39991",
        "task_type": "local_bash",
    }
    (progress,) = codec.feed(
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": "build-monitor",
            "description": "Waiting for RTM build 39991",
            "last_tool_name": "Bash",
            "usage": {"total_tokens": 100, "duration_ms": 5000},
        }
    )
    assert progress.data["description"] == "Waiting for RTM build 39991"
    assert progress.data["last_tool_name"] == "Bash"
    assert progress.data["task_state"] == ""
    assert "usage" not in progress.data
    (paused,) = codec.feed(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "build-monitor",
            "patch": {"status": "paused", "summary": "Waiting for a deployment slot"},
        }
    )
    assert paused.data["task_state"] == "paused"
    assert paused.data["summary"] == "Waiting for a deployment slot"
    (sparse,) = codec.feed(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "build-monitor",
            "patch": {"description": "Monitor RTM deployment", "end_time": 1234},
        }
    )
    assert sparse.data["task_state"] == "" and sparse.data["state"] == "running"
    assert sparse.data["description"] == "Monitor RTM deployment"
    assert "end_time" not in sparse.data
    assert codec.operation_id == operation and codec.background_turn is None


@pytest.mark.parametrize("field,limit", [("description", 1000), ("summary", 2000)])
def test_metadata_truncation_is_visible_and_optional_malformed_metadata_is_ignored(field, limit):
    codec = ClaudeCodec(SESSION)
    (event,) = codec.feed(
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": "monitor",
            field: "x" * (limit + 20),
            "task_type": {"bad": "type"},
            "last_tool_name": False,
        }
    )
    assert event.data[field] == "x" * (limit - 1) + "…"
    assert "task_type" not in event.data and "last_tool_name" not in event.data
    assert native_ipc.normalize_event({"kind": event.kind, "data": event.data})["data"] == (
        event.data
    )


def test_task_ipc_accepts_only_bounded_allowlisted_metadata():
    observation = task(
        task_state="paused",
        description="Monitor build",
        task_type="local_bash",
        last_tool_name="Bash",
        summary="Waiting for a deployment slot",
    )
    assert native_ipc.normalize_event(observation) == observation
    for field, value in (
        ("description", "x" * 1001),
        ("summary", "x" * 2001),
        ("task_type", "x" * 129),
        ("last_tool_name", "x" * 129),
        ("output_file", "/private/output"),
        ("patch", {"status": "running"}),
        ("operation_id", str(uuid.uuid4())),
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc.normalize_event(
                {**observation, "data": {**observation["data"], field: value}}
            )


def test_task_snapshot_merges_sparse_progress_and_survives_bounded_reads_and_restarts(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    append(
        root,
        binding,
        monkeypatch,
        T0,
        task(description="Monitor RTM build 39991", task_type="local_bash", task_state="running"),
    )
    append(root, binding, monkeypatch, T0 + 30, task("paused", task_state="paused"))
    append(
        root,
        binding,
        monkeypatch,
        T0 + 40,
        task(task_state="", last_tool_name="Bash", summary="Waiting for deployment"),
    )
    expected = [
        {
            "task_id": "build-monitor",
            "state": "paused",
            "description": "Monitor RTM build 39991",
            "task_type": "local_bash",
            "last_tool_name": "Bash",
            "summary": "Waiting for deployment",
            "started_at": T0,
            "updated_at": T0 + 40,
        }
    ]
    for max_turns in (None, 0, 1):
        view = snapshot(root, sid, binding, max_turns=max_turns)
        assert view["native"]["background_tasks"] == expected
        assert view["native"]["background_active"] is True
        assert view["native"]["background_turn"] is None
        assert view["state"] == "idle" and view["turns"] == []
        journal._CACHE.clear()
        assert snapshot(root, sid, binding, max_turns=max_turns) == view


@pytest.mark.parametrize("terminal", ["completed", "failed", "stopped", "killed"])
def test_terminal_tasks_disappear_and_late_progress_never_revives_them(
    conversation, monkeypatch, terminal
):
    root, sid, binding = conversation
    append(root, binding, monkeypatch, T0, task(description="Monitor build"))
    append(root, binding, monkeypatch, T0 + 20, task(terminal, task_state=terminal))
    append(root, binding, monkeypatch, T0 + 30, task(task_state="", summary="late progress"))
    append(root, binding, monkeypatch, T0 + 40, task(task_id="another", description="Run tests"))
    (remaining,) = snapshot(root, sid, binding)["native"]["background_tasks"]
    assert remaining["task_id"] == "another"
    assert remaining["description"] == "Run tests"
    assert "late progress" not in json.dumps(remaining)


def test_task_ids_are_scoped_to_live_generation_and_old_workers_cannot_update_them(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    append(root, binding, monkeypatch, T0, task(description="Old worker's build"))
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    append(root, successor, monkeypatch, T0 + 10, task(description="New worker's tests"))
    append(root, binding, monkeypatch, T0 + 20, task("completed", task_state="completed"))
    (current,) = snapshot(root, sid, successor)["native"]["background_tasks"]
    assert current["description"] == "New worker's tests" and current["started_at"] == T0 + 10
    stopped = snapshot(root, sid, successor, live=None)
    assert stopped["native"]["background_tasks"] == []
    assert stopped["native"]["background_active"] is False


@pytest.mark.parametrize("current_active", [False, True])
@pytest.mark.parametrize("old_kind", ["background", "turn_completed"])
@pytest.mark.parametrize("max_turns", [None, 0])
def test_projected_activity_ignores_old_generations_even_when_their_report_arrives_last(
    conversation, monkeypatch, current_active, old_kind, max_turns
):
    root, sid, binding = conversation
    claim = journal_tests.request(binding)
    operation = claim["params"]["operation_id"]
    journal.claim(root, claim)
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    current = (
        task(description="Monitor current build")
        if current_active
        else {"kind": "background", "data": {"state": "idle", "active": False}}
    )
    append(root, successor, monkeypatch, T0, current)
    old = (
        {"kind": "background", "data": {"state": "activity", "active": not current_active}}
        if old_kind == "background"
        else {
            "kind": "turn_completed",
            "data": {
                "operation_id": operation,
                "native_turn_id": "old-turn",
                "state": "completed",
                "error": "",
                "background_active": not current_active,
            },
        }
    )
    append(root, binding, monkeypatch, T0 + 20, old)
    view = snapshot(root, sid, successor, max_turns=max_turns)
    assert view["native"]["background_active"] is current_active
    assert bool(view["native"]["background_tasks"]) is current_active
    public = share_api.project(view, {}, share_api._Redactor([]), "link")
    assert public["background"] is current_active and "native" not in public


def test_old_metadata_free_task_events_remain_readable_without_inventing_activity(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    append(root, binding, monkeypatch, T0, task())
    (current,) = snapshot(root, sid, binding)["native"]["background_tasks"]
    assert current == {
        "task_id": "build-monitor",
        "state": "running",
        "started_at": T0,
        "updated_at": T0,
    }


def test_active_task_projection_has_a_fixed_bound(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(native_protocol, "MAX_PENDING", 2)
    for i in range(3):
        append(root, binding, monkeypatch, T0 + i, task(task_id=f"task-{i}"))
    assert len(snapshot(root, sid, binding)["native"]["background_tasks"]) == 2


def test_duplicate_task_observations_keep_original_start_and_other_tasks_separate(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    append(root, binding, monkeypatch, T0, task(description="Monitor build"))
    append(
        root,
        binding,
        monkeypatch,
        T0 + 5,
        task(task_id="tests", description="Run integration tests"),
    )
    append(root, binding, monkeypatch, T0 + 10, task(description="Monitor build"))
    append(root, binding, monkeypatch, T0 + 15, task(task_state="", last_tool_name="Bash"))
    monitor, tests = snapshot(root, sid, binding)["native"]["background_tasks"]
    assert monitor["task_id"] == "build-monitor" and monitor["started_at"] == T0
    assert monitor["updated_at"] == T0 + 15 and monitor["last_tool_name"] == "Bash"
    assert tests["task_id"] == "tests" and tests["started_at"] == tests["updated_at"] == T0 + 5
    assert tests["description"] == "Run integration tests" and "last_tool_name" not in tests


def test_an_uncorrelated_background_report_retains_only_the_generic_activity_signal(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    append(
        root,
        binding,
        monkeypatch,
        T0,
        {"kind": "background", "data": {"state": "activity", "active": True}},
    )
    view = snapshot(root, sid, binding)
    assert view["native"]["background_tasks"] == []
    assert view["native"]["background_active"] is True and view["turns"] == []


def test_background_metadata_stays_out_of_public_links(conversation, monkeypatch):
    root, sid, binding = conversation
    append(root, binding, monkeypatch, T0, task(description="Private RTM build description"))
    operator = snapshot(root, sid, binding)
    assert operator["native"]["background_tasks"]
    public = share_api.project(operator, {}, share_api._Redactor([]), "link")
    assert "native" not in public and "Private RTM" not in json.dumps(public)
    event_page = {"revision": 4, "next_cursor": 4, "events": [{"cursor": 4, **task()}]}
    public_events = share_api.project_events(event_page, "link")
    assert public_events["events"] == [{"cursor": 4, "kind": "background"}]
