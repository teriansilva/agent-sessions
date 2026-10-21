"""Message times come from the persisted journal, not a browser poll or worker restart."""

from dataclasses import replace

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_journal import event, request

conversation = journal_tests.conversation
ADAPTERS = ["codex-app-server", "claude-stream-json", "opencode-acp"]
FIRST_TEXT = 1_791_790_242.125


def snapshot(root, sid, binding, *, max_turns=None):
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=max_turns,
    )
    return structured_runtime._snapshot(binding.session_key, raw)


def completed(operation_id, text=""):
    return {
        "kind": "turn_completed",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "state": "completed",
            "error": "",
            "background_active": False,
            "text": text,
        },
    }


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("partial", [False, True])
def test_first_response_time_survives_streaming_completion_and_reopen(
    conversation, monkeypatch, adapter, partial
):
    root, sid, binding = conversation
    binding = replace(binding, adapter=adapter)
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    assert snapshot(root, sid, binding)["turns"][0]["reply_ts"] is None

    # Empty text and tool activity must not give a response a fictitious start time.
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT - 30)
    journal.append_events(
        root,
        binding,
        [
            event(operation_id, ""),
            {
                "kind": "tool",
                "data": {
                    "operation_id": operation_id,
                    "native_turn_id": "native-turn-1",
                    "item_id": "tool-1",
                    "tool": "command",
                    "summary": "pwd",
                    "completed": True,
                },
            },
        ],
    )
    assert snapshot(root, sid, binding)["turns"][0]["reply_ts"] is None
    first = event(operation_id, "Initial response")
    first["data"]["partial"] = partial
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT)
    journal.append_events(root, binding, [first])
    before = snapshot(root, sid, binding)
    assert before["turns"][0]["reply_ts"] == FIRST_TEXT

    # A backwards host-clock adjustment must not replace the first event in journal order.
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT - 1)
    journal.append_events(root, binding, [event(operation_id, "Final response")])
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT + 90)
    journal.append_events(root, binding, [completed(operation_id)])
    warm = snapshot(root, sid, binding)
    assert warm["turns"][0]["reply_ts"] == FIRST_TEXT
    assert warm["turns"][0]["state"] == "completed"
    assert "Final response" in warm["turns"][0]["reply"]
    journal._CACHE.clear()
    assert snapshot(root, sid, binding) == warm
    assert before["turns"][0]["reply_ts"] == FIRST_TEXT


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("text", ["Completion-only response", ""])
def test_completion_only_has_a_time_only_when_it_contains_a_response(
    conversation, monkeypatch, adapter, text
):
    root, sid, binding = conversation
    binding = replace(binding, adapter=adapter)
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT)
    journal.append_events(root, binding, [completed(operation_id, text)])
    journal._CACHE.clear()
    turn = snapshot(root, sid, binding)["turns"][0]
    assert turn["reply"] == text
    assert turn["reply_ts"] == (FIRST_TEXT if text else None)


def test_bounded_snapshot_keeps_each_displayed_turns_own_time(conversation, monkeypatch):
    root, sid, binding = conversation
    for i in range(3):
        claim = request(binding, revision=journal.read(root, sid).revision)
        operation_id = claim["params"]["operation_id"]
        journal.claim(root, claim)
        monkeypatch.setattr(journal.time, "time", lambda i=i: FIRST_TEXT + i)
        journal.append_events(root, binding, [event(operation_id, f"Response {i}")])
    journal._CACHE.clear()
    view = snapshot(root, sid, binding, max_turns=2)
    assert view["omitted_turns"] == 1
    assert [t["reply_ts"] for t in view["turns"]] == [FIRST_TEXT + 1, FIRST_TEXT + 2]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("queued", [False, True])
def test_submission_time_survives_handoff_replay_and_cold_reads(
    conversation, monkeypatch, adapter, queued
):
    root, sid, binding = conversation
    binding = replace(binding, adapter=adapter)
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    submitted = FIRST_TEXT - 60
    monkeypatch.setattr(journal.time, "time", lambda: submitted)
    assert journal.claim(root, claim, queued=queued)[0]
    first = snapshot(root, sid, binding)["turns"][0]
    assert first["ts"] == submitted
    assert first["reply_ts"] is None
    if queued:
        assert first["state"] == "queued"

    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT - 1)
    if queued:
        journal.record_handoff(root, binding, operation_id, "uncertain")
    journal.record_handoff(root, binding, operation_id, "sent")
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT)
    journal.append_events(root, binding, [event(operation_id, "Response")])
    monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT + 90)
    journal.append_events(root, binding, [completed(operation_id)])
    assert not journal.claim(root, claim, queued=queued)[0]
    warm = snapshot(root, sid, binding)
    assert warm["turns"][0]["ts"] == submitted
    assert warm["turns"][0]["reply_ts"] == FIRST_TEXT
    journal._CACHE.clear()
    assert snapshot(root, sid, binding) == warm
    assert first["ts"] == submitted


def test_bounded_snapshot_preserves_unsent_queued_submission_times(conversation, monkeypatch):
    root, sid, binding = conversation
    for i in range(3):
        claim = request(binding, revision=journal.read(root, sid).revision)
        monkeypatch.setattr(journal.time, "time", lambda i=i: FIRST_TEXT + i)
        journal.claim(root, claim, queued=True)
        monkeypatch.setattr(journal.time, "time", lambda: FIRST_TEXT + 90)
        journal.record_handoff(root, binding, claim["params"]["operation_id"], "not_sent")
    journal._CACHE.clear()
    view = snapshot(root, sid, binding, max_turns=2)
    assert view["omitted_turns"] == 1
    assert [t["ts"] for t in view["turns"]] == [FIRST_TEXT + 1, FIRST_TEXT + 2]
    assert all(t["state"] == "failed" and t["reply_ts"] is None for t in view["turns"])
