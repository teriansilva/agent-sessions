"""Crash/replay, consent and source-deletion boundaries of optional HQ capture (#2036)."""

import asyncio
import contextlib
import json
import uuid

import pytest

from agent_sessions import (
    ai_providers,
    ask_store,
    memory,
    memory_capture,
    memory_service,
    review,
    template_secrets,
)
from test_hq_models import wire  # noqa: F401


@pytest.fixture(autouse=True)
def inventory(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


def source(text="Research answers should include primary sources.", *, capture=True, status="done"):
    if capture:
        memory_service.set_capture(True)
    cid, tid = ask_store.create(), str(uuid.uuid4())
    rec = {"type": "status", "turn_id": tid, "status": status}
    if capture and status == "done":
        rec["memory_capture"] = memory_capture.intent(text)
    ask_store.append(
        cid,
        {"type": "user", "turn_id": tid, "text": text, "qd": ask_store.question_digest(text)},
        rec,
    )
    return cid, tid, text


def extraction(monkeypatch, quote):
    calls = []

    async def extract(messages):
        calls.append(messages)
        return {
            "memories": [
                {
                    "title": "Research sources",
                    "body": "Include primary sources.",
                    "quote": quote,
                    "type": "decision",
                }
            ]
        }

    monkeypatch.setattr(review, "complete_json", extract)
    return calls


def run():
    return asyncio.run(memory_capture.step())


def test_default_off_and_historical_turns_are_not_backfilled(monkeypatch):
    assert memory_capture.intent() is None
    source(capture=False)
    memory_service.set_capture(True)
    extraction(monkeypatch, "not in the source")
    assert not run()
    assert not memory.notes(memory.HQ_SCOPE)


def test_commit_before_enqueue_is_reconciled_and_source_receipt_is_idempotent(monkeypatch):
    cid, tid, quote = source()
    calls = extraction(monkeypatch, quote)
    assert run()
    assert not run()
    assert len(calls) == 1
    note = memory.notes(memory.HQ_SCOPE)[0]
    assert (
        memory_service.read(memory_service.operator(), memory.HQ_SCOPE, note["slug"])["source"][
            "source"
        ]
        == f"{cid}/{tid}"
    )
    assert all(m["role"] != "assistant" for m in calls[0])


def test_admission_before_ack_replays_without_duplication_and_forget_wins(monkeypatch):
    _, _, quote = source()
    calls = extraction(monkeypatch, quote)
    real = memory_capture._state

    def stop_after_admission(job, state, **kw):
        if state == "done":
            raise RuntimeError("simulated crash after durable admission")
        return real(job, state, **kw)

    monkeypatch.setattr(memory_capture, "_state", stop_after_admission)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run()
    note = memory.notes(memory.HQ_SCOPE)[0]
    memory.delete_note(memory.HQ_SCOPE, note["slug"])
    monkeypatch.setattr(memory_capture, "_state", real)
    assert run()
    assert len(calls) == 1 and not memory.notes(memory.HQ_SCOPE)


@pytest.mark.parametrize("change", ["off", "clear", "source-delete"])
def test_policy_and_source_changes_during_extraction_cancel_admission(monkeypatch, change):
    cid, _, quote = source()

    async def extract(_):
        if change == "off":
            memory_service.set_capture(False)
            memory_service.set_capture(True)
        elif change == "clear":
            memory.clear(memory.HQ_SCOPE)
        else:
            ask_store.delete_conversation(cid)
        return {
            "memories": [
                {"title": "Sources", "body": "Primary sources.", "quote": quote, "type": "decision"}
            ]
        }

    monkeypatch.setattr(review, "complete_json", extract)
    assert run()
    assert not memory.notes(memory.HQ_SCOPE)


def test_source_deletion_forgets_explicit_and_captured_notes(monkeypatch):
    cid, tid, quote = source()
    extraction(monkeypatch, quote)
    run()
    memory_service.submit(
        memory_service.operator(),
        memory.HQ_SCOPE,
        title="Explicit source",
        body="A durable preference.",
        origin=f"hq:{cid}:{tid}:explicit:1",
        source=f"{cid}/{tid}",
        quote=quote,
    )
    assert len(memory.notes(memory.HQ_SCOPE)) == 2
    ask_store.delete_conversation(cid)
    assert not memory.notes(memory.HQ_SCOPE)


def test_new_secret_candidate_is_withheld_before_persistence(monkeypatch):
    _, _, quote = source()

    async def extract(_):
        monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["orchard-harbor-linen"])
        return {
            "memories": [
                {
                    "title": "Sources",
                    "body": "orchard-harbor-linen",
                    "quote": quote,
                    "type": "finding",
                }
            ]
        }

    monkeypatch.setattr(review, "complete_json", extract)
    assert run()
    with contextlib.closing(memory._connect(memory.HQ_SCOPE)) as conn:
        assert all(
            "orchard-harbor-linen" not in r[0]
            for r in conn.execute("SELECT candidates FROM memory_capture_jobs")
        )
    assert not memory.notes(memory.HQ_SCOPE)


def test_fabricated_quote_and_failed_turn_cannot_capture(monkeypatch):
    _, _, quote = source(status="failed")
    calls = extraction(monkeypatch, quote)
    assert not run() and not calls
    source()
    extraction(monkeypatch, "words never said")
    assert run() and not memory.notes(memory.HQ_SCOPE)


def test_capture_intent_is_shape_checked_at_the_durable_boundary():
    cid, tid, _ = source()
    with pytest.raises(ValueError, match="capture intent"):
        ask_store.append(
            cid,
            {
                "type": "status",
                "status": "failed",
                "turn_id": tid,
                "memory_capture": {"generation": 0, "revision": 0},
            },
        )
    assert "memory_capture" in json.dumps(ask_store.read(cid))


def test_off_at_turn_start_cannot_capture_after_operator_enables_it():
    before = memory_service.capture_settings()
    memory_service.set_capture(True)
    assert memory_capture.intent("Use primary sources.", expected=before) is None
    enabled = memory_service.capture_settings()
    memory.clear(memory.HQ_SCOPE)
    assert memory_capture.intent("Use primary sources.", expected=enabled) is None


def test_redacted_source_does_not_match_durable_intent(monkeypatch):
    cid, _, quote = source()
    calls = extraction(monkeypatch, quote)
    path = ask_store._path(cid)
    data = path.read_text()
    path.write_text(data.replace(quote, "[withheld]"))
    assert run()
    assert not calls and not memory.notes(memory.HQ_SCOPE)


def test_admitted_memories_survive_routine_expiry_but_explicit_source_delete_wins(monkeypatch):
    import os
    import time

    cid, _, quote = source()
    extraction(monkeypatch, quote)
    run()
    old = time.time() - 40 * 86400
    os.utime(ask_store._path(cid), (old, old))
    assert memory_capture.retains_source(cid)
    assert cid not in ask_store.sweep() and ask_store.exists(cid)
    assert memory.notes(memory.HQ_SCOPE)
    ask_store.delete_conversation(cid)
    assert not memory.notes(memory.HQ_SCOPE) and not ask_store.exists(cid)


def test_forgetting_last_note_releases_source_retention_pin(monkeypatch):
    import os
    import time

    cid, _, quote = source()
    extraction(monkeypatch, quote)
    run()
    memory.delete_note(memory.HQ_SCOPE, memory.notes(memory.HQ_SCOPE)[0]["slug"])
    assert not memory_capture.retains_source(cid)
    old = time.time() - 40 * 86400
    os.utime(ask_store._path(cid), (old, old))
    assert cid in ask_store.sweep()


def test_committed_handoff_does_not_wait_for_the_conversation_scan(monkeypatch):
    for _ in range(60):
        ask_store.create()
    cid, tid, quote = source()
    calls = extraction(monkeypatch, quote)
    memory_capture.enqueue_committed(cid, tid)
    # Even a reconciler still traversing old conversations cannot delay the already queued job.
    monkeypatch.setattr(memory_capture, "reconcile", lambda: None)
    assert run() and calls and memory.notes(memory.HQ_SCOPE)


def test_handoff_cannot_enqueue_a_failed_or_uncommitted_turn():
    cid, tid, _ = source(status="failed")
    memory_capture.enqueue_committed(cid, tid)
    with contextlib.closing(memory._connect(memory.HQ_SCOPE)) as conn:
        assert not conn.execute("SELECT 1 FROM memory_capture_jobs").fetchone()


def test_unreadable_capture_metadata_does_not_break_an_unrelated_hq_turn(monkeypatch):
    from agent_sessions import ask_memory

    def broken():
        raise ValueError("corrupt metadata")

    monkeypatch.setattr(memory_service, "capture_settings", broken)
    assert ask_memory.snapshot() is None
    assert memory_capture.intent("Hello.", expected=None) is None
    assert memory_capture.intent("Hello.") is None


@pytest.mark.anyio
async def test_capture_outliving_a_turn_uses_its_own_model_context(monkeypatch, wire):  # noqa: F811
    _, _, quote = source()
    _, bodies, send = wire
    entered, release = asyncio.Event(), asyncio.Event()

    async def extract(_):
        entered.set()
        await release.wait()
        await send("background memory capture")
        return {
            "memories": [{"title": "Sources", "body": quote, "quote": quote, "type": "decision"}]
        }

    monkeypatch.setattr(review, "complete_json", extract)
    with ai_providers.spoken_turn("high", "voice"):
        with ai_providers.model_override(model="typed", thinking="low"):
            task = asyncio.create_task(memory_capture.step())
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert ai_providers.override_model() == "typed"
    release.set()
    assert await asyncio.wait_for(task, timeout=10)
    assert len(memory.notes(memory.HQ_SCOPE)) == 1
    assert (bodies[0]["model"], bodies[0]["reasoning_effort"]) == ("endpoint", "medium")
