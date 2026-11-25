"""A turn that failed on the agent's login offers Sign in (`remedy: signin`).

Claude's stream-json marks it with a structured field on the synthetic assistant message it writes
instead of a reply: `"error": "authentication_failed"`. See `fixtures/claude_auth_failed.jsonl`
(provenance in `fixtures/claude_auth_failed.PROVENANCE.md`). The remedy is read from that field
only, never from the message's words; it rides `turn_completed` through the closed IPC schema and
the journal to the turn the SPA reads.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from agent_sessions import chat_store, native_ipc, native_runtime, structured_runtime
from agent_sessions import native_journal as journal
from agent_sessions.native_protocol import TURN_REMEDIES, ClaudeCodec
from test_native_journal import request

FIXTURE = Path(__file__).parent / "fixtures" / "claude_auth_failed.jsonl"


def _rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def _replay(rows: list[dict]) -> tuple[ClaudeCodec, list, str]:
    session = next(r["frame"]["session_id"] for r in rows if r["frame"].get("session_id"))
    codec = ClaudeCodec(session)
    events = []
    operation = None
    for row in rows:
        frame = row["frame"]
        if row["dir"] == "out":
            operation = frame["uuid"]
            codec.submit(frame["message"]["content"], operation)
            continue
        events += codec.feed(frame)
    return codec, events, operation


def _completed(events) -> dict:
    (done,) = [e for e in events if e.kind == "turn_completed"]
    return done.data


def _without_error_field(rows: list[dict]) -> list[dict]:
    rows = copy.deepcopy(rows)
    for row in rows:
        row["frame"].pop("error", None)
    return rows


def test_the_captured_login_failure_fails_the_turn_with_the_signin_remedy():
    _, events, operation = _replay(_rows())
    done = _completed(events)
    assert done["operation_id"] == operation
    assert done["state"] == "failed"
    assert done["remedy"] == "signin"
    # The closed schema the worker journals through accepts it as it is.
    assert native_ipc.normalize_event({"kind": "turn_completed", "data": done})["data"] == done


def test_the_same_words_without_the_field_offer_no_remedy():
    """Negative control: the message's text ("Not logged in · Please run /login") is no signal."""
    _, events, _ = _replay(_without_error_field(_rows()))
    done = _completed(events)
    assert done["state"] == "failed"
    assert "remedy" not in done


def test_another_error_value_offers_no_remedy():
    rows = _rows()
    for row in rows:
        if "error" in row["frame"]:
            row["frame"]["error"] = "rate_limit"
    done = _completed(_replay(rows)[1])
    assert done["state"] == "failed" and "remedy" not in done


def test_the_remedy_belongs_to_its_own_turn_only():
    codec, _, _ = _replay(_rows())
    nxt = str(uuid.uuid4())
    codec.submit("again", nxt)
    for state in ("queued", "started"):
        codec.feed({"type": "command_lifecycle", "command_uuid": nxt, "state": state})
    (done,) = codec.feed(
        {
            "type": "result",
            "uuid": str(uuid.uuid4()),
            "subtype": "success",
            "is_error": False,
            "result": "Answer",
            "user_message_uuid": nxt,
        }
    )
    assert done.kind == "turn_completed" and done.data["state"] == "completed"
    assert "remedy" not in done.data


def test_a_login_failure_followed_by_a_completed_result_offers_no_remedy():
    """Only a FAILED end carries a remedy (a retry inside the turn that then succeeded)."""
    rows = [r for r in _rows() if r["frame"].get("type") != "result"]
    codec, _, operation = _replay(rows[:-1])  # without the lifecycle end
    (done,) = codec.feed(
        {
            "type": "result",
            "uuid": str(uuid.uuid4()),
            "subtype": "success",
            "is_error": False,
            "result": "Answer",
            "user_message_uuid": operation,
        }
    )
    assert done.data["state"] == "completed" and "remedy" not in done.data


def test_a_subagents_login_failure_is_not_the_operator_turns():
    rows = _rows()
    for row in rows:
        if row["frame"].get("type") == "assistant":
            row["frame"]["parent_tool_use_id"] = "toolu_1"
    done = _completed(_replay(rows)[1])
    assert "remedy" not in done


def test_a_turn_ended_by_its_lifecycle_alone_keeps_the_remedy():
    """The CLI ends the input without the result that names it: still failed, still Sign in."""
    rows = [r for r in _rows() if r["frame"].get("type") != "result"]
    for row in rows:
        if row["frame"].get("state") == "cancelled":
            row["frame"]["state"] = "completed"
    codec, events, operation = _replay(rows)
    events += codec.feed(
        {"type": "system", "subtype": "status", "status": None, "session_id": codec.native_id}
    )
    done = _completed(events)
    assert done["operation_id"] == operation
    assert done["state"] == "failed" and done["remedy"] == "signin"


# --- the closed IPC schema --------------------------------------------------------------------


def _event(**extra) -> dict:
    return {
        "kind": "turn_completed",
        "data": {
            "operation_id": str(uuid.uuid4()),
            "native_turn_id": "n",
            "state": "failed",
            "error": "",
            **extra,
        },
    }


def test_the_schema_accepts_every_remedy_on_a_failed_turn():
    assert TURN_REMEDIES == {"signin"}
    for remedy in TURN_REMEDIES:
        assert native_ipc.normalize_event(_event(remedy=remedy))["data"]["remedy"] == remedy
    assert "remedy" not in native_ipc.normalize_event(_event())["data"]


@pytest.mark.parametrize(
    "extra",
    [
        {"remedy": "login"},
        {"remedy": ""},
        {"remedy": None},
        {"remedy": ["signin"]},
        {"remedy": "signin", "state": "completed"},
        {"remedy": "signin", "state": "interrupted"},
    ],
)
def test_the_schema_refuses_any_other_remedy(extra):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event(_event(**extra))


# --- journal → the turn the SPA reads ---------------------------------------------------------


@pytest.fixture
def conversation(tmp_path):
    session_id = str(uuid.uuid4())
    root = tmp_path / "chat"
    binding = native_ipc.Binding(
        session_key=f"test-api:{session_id}",
        worker_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        adapter="claude-stream-json",
    )
    chat_store.create(
        root,
        session_id,
        cwd=str(tmp_path),
        request={"runtime": "api", "session_key": binding.session_key},
        model="requested-model",
    )
    return root, session_id, binding


def _turn(root, sid, binding, **extra) -> str:
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation, "sent")
    data = {"operation_id": operation, "native_turn_id": "n", "error": "", **extra}
    journal.append_events(root, binding, [{"kind": "turn_completed", "data": data}])
    return operation


def test_the_snapshot_turn_carries_the_remedy_of_its_failure_only(conversation):
    root, sid, binding = conversation
    failed = _turn(root, sid, binding, state="failed", remedy="signin")
    plain = _turn(root, sid, binding, state="failed", error="something else")
    done = _turn(root, sid, binding, state="completed")
    view = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )
    public = {
        t["turn_id"]: t for t in structured_runtime._snapshot(binding.session_key, view)["turns"]
    }
    assert public[failed]["state"] == "failed" and public[failed]["remedy"] == "signin"
    assert "remedy" not in public[plain] and public[plain]["state"] == "failed"
    assert "remedy" not in public[done]
