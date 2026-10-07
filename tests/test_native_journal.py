"""Durable native claims survive contention and uncertain writes without replay (#1278).

These are real private tmp-path journals. The journal is not an authorization or
native-effect executor: worker/generation admission belongs to its caller.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from agent_sessions import chat_store, native_ipc
from agent_sessions import native_journal as journal


@pytest.fixture
def conversation(tmp_path):
    session_id = str(uuid.uuid4())
    root = tmp_path / "chat"
    binding = native_ipc.Binding(
        session_key=f"test-api:{session_id}",
        worker_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        adapter="codex-app-server",
    )
    chat_store.create(
        root,
        session_id,
        cwd=str(tmp_path),
        request={"runtime": "api", "session_key": binding.session_key},
        model="requested-model",
    )
    return root, session_id, binding


def request(binding, *, action="submit", operation_id=None, revision=1, **changes):
    params = {
        "operation_id": operation_id or str(uuid.uuid4()),
        "expected_revision": revision,
    }
    if action == "submit":
        params.update(
            text="Inspect the project",
            context={
                "mission_id": "mission-1",
                "flow_revision": "flow-2",
                "step_id": "inspect",
                "episode": 3,
            },
        )
    elif action == "decide":
        params.update(
            turn_id=str(uuid.uuid4()),
            request_id="s:approval-1",
            item_id="item-1",
            payload_digest="a" * 64,
            decision="approve",
            approval_worker_id=binding.worker_id,
            approval_connection_id=binding.connection_id,
        )
    elif action == "interrupt":
        params["turn_id"] = str(uuid.uuid4())
    elif action == "stop":
        params["target_worker_id"] = binding.worker_id
    params.update(changes)
    return {
        **binding.envelope("request"),
        "request_id": str(uuid.uuid4()),
        "action": action,
        "params": params,
    }


def event(operation_id, text="Answer"):
    return {
        "kind": "text",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "item_id": "item-1",
            "text": text,
            "partial": False,
            "truncated": False,
        },
    }


def path_of(conversation):
    root, session_id, _ = conversation
    return root / f"{session_id}.jsonl"


def records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def test_claim_is_private_and_fsynced_before_caller_can_handoff(conversation, monkeypatch):
    root, session_id, binding = conversation
    path = path_of(conversation)
    claim = request(binding)
    observed = []
    fsync = os.fsync
    inode = path.stat().st_ino

    def sync(fd):
        fsync(fd)
        if os.fstat(fd).st_ino == inode:
            observed.append(("fsynced", records(path)[-1]))

    monkeypatch.setattr(chat_store.os, "fsync", sync)
    newly_claimed, receipt = journal.claim(root, claim)
    observed.append(("caller_can_write", receipt))
    assert newly_claimed is True
    assert [item[0] for item in observed] == ["fsynced", "caller_can_write"]
    assert observed[0][1]["type"] == "native_operation"
    assert observed[0][1]["operation_id"] == claim["params"]["operation_id"]
    assert receipt["handoff"] == "uncertain"
    assert receipt["recorded_revision"] == 2
    assert path.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert journal.read(root, session_id).revision == 2


def test_exact_replay_precedes_stale_revision_and_new_current_envelope(conversation):
    root, session_id, binding = conversation
    claim = request(binding)
    _, original = journal.claim(root, claim)
    before = path_of(conversation).read_bytes()
    reconnect = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    repeated = {**claim, **reconnect.envelope("request"), "request_id": str(uuid.uuid4())}
    repeated["params"] = {**claim["params"], "expected_revision": 0}
    fresh, receipt = journal.claim(root, repeated)
    assert fresh is False and receipt == original
    assert path_of(conversation).read_bytes() == before
    stored = journal.read(root, session_id).operations[original["operation_id"]]
    assert (stored.worker_id, stored.connection_id) == (binding.worker_id, binding.connection_id)
    assert stored.handoff == "uncertain"


@pytest.mark.parametrize(
    "field,value", [("text", "Changed instruction"), ("context", {"episode": 4})]
)
def test_existing_operation_cannot_be_rebound_to_changed_work(conversation, field, value):
    root, _, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    changed = copy.deepcopy(claim)
    changed["params"][field] = value
    changed["params"]["expected_revision"] = 2
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, changed)
    assert refused.value.code == "conflict"
    assert path_of(conversation).read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "turn_id",
        "request_id",
        "item_id",
        "payload_digest",
        "decision",
        "approval_worker_id",
        "approval_connection_id",
    ],
)
def test_decision_operation_preserves_exact_original_target(conversation, field):
    root, _, binding = conversation
    claim = request(binding, action="decide")
    journal.claim(root, claim)
    changed = copy.deepcopy(claim)
    changed["params"][field] = (
        "reject"
        if field == "decision"
        else "b" * 64
        if field == "payload_digest"
        else str(uuid.uuid4())
        if field in {"turn_id", "approval_worker_id", "approval_connection_id"}
        else "different-native-target"
    )
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, changed)
    assert refused.value.code == "conflict"
    assert len(records(path_of(conversation))) == 2


def test_stale_new_work_is_refused_without_a_durable_claim(conversation):
    root, _, binding = conversation
    journal.claim(root, request(binding))
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, request(binding, revision=1))
    assert refused.value.code == "conflict"
    assert path_of(conversation).read_bytes() == before


def test_independent_processes_can_claim_same_operation_only_once(conversation, tmp_path):
    root, _, binding = conversation
    claim = request(binding)
    payload = json.dumps({"root": str(root), "request": claim})
    script = """
import json, sys
from pathlib import Path
from agent_sessions.native_journal import claim
payload = json.loads(sys.stdin.readline())
new, receipt = claim(Path(payload['root']), payload['request'])
print(json.dumps({'new': new, 'receipt': receipt}), flush=True)
"""
    child_home = tmp_path / "child-home"
    child_home.mkdir(mode=0o700)
    env = dict(os.environ, HOME=str(child_home), PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(4)
    ]
    try:
        # All processes exist before any is released to contend for the real flock.
        for child in children:
            child.stdin.write(payload + "\n")
            child.stdin.flush()
            child.stdin.close()
            child.stdin = None
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert sum(outcome["new"] for outcome in outcomes) == 1
    assert all(outcome["receipt"] == outcomes[0]["receipt"] for outcome in outcomes)
    assert [record["type"] for record in records(path_of(conversation))] == [
        "session",
        "native_operation",
    ]


def test_complete_bytes_after_failed_fsync_do_not_allow_another_handoff(conversation, monkeypatch):
    root, _, binding = conversation
    claim = request(binding)
    path = path_of(conversation)
    inode = path.stat().st_ino
    fsync = os.fsync
    allowed = False
    synced = []
    simulated_native_writes = []

    def sync(fd):
        if os.fstat(fd).st_ino == inode:
            if not allowed:
                raise OSError("synthetic disk durability failure")
            synced.append(fd)
        return fsync(fd)

    monkeypatch.setattr(chat_store.os, "fsync", sync)
    for _ in range(2):
        with pytest.raises(journal.JournalError) as failed:
            new, receipt = journal.claim(root, claim)
            if new:
                simulated_native_writes.append(receipt)
        assert failed.value.code == "unavailable"
    assert len(records(path)) == 2  # One complete but not yet acknowledged claim remains occupied.
    allowed = True
    new, receipt = journal.claim(root, claim)
    if new:
        simulated_native_writes.append(receipt)
    assert synced and new is False and receipt["handoff"] == "uncertain"
    assert simulated_native_writes == []
    assert len(records(path)) == 2


def test_partial_write_never_gets_repaired_into_permission_to_retry(conversation, monkeypatch):
    root, session_id, binding = conversation
    claim = request(binding)
    path = path_of(conversation)
    original_write = os.write
    inode = path.stat().st_ino
    partial = False

    def write(fd, data):
        nonlocal partial
        if os.fstat(fd).st_ino == inode:
            if partial:
                raise OSError("synthetic interrupted append")
            partial = True
            return original_write(fd, data[: len(data) // 2])
        return original_write(fd, data)

    monkeypatch.setattr(chat_store.os, "write", write)
    with pytest.raises(journal.JournalError):
        journal.claim(root, claim)
    torn = path.read_bytes()
    assert not torn.endswith(b"\n")
    monkeypatch.setattr(chat_store.os, "write", original_write)
    for operation in [
        lambda: journal.read(root, session_id),
        lambda: journal.claim(root, claim),
        lambda: journal.claim(root, request(binding, revision=2)),
    ]:
        with pytest.raises(journal.JournalError) as refused:
            operation()
        assert refused.value.code == "unavailable"
        assert path.read_bytes() == torn


def test_replay_and_observation_both_cross_durability_barrier(conversation, monkeypatch):
    root, session_id, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    inode = path_of(conversation).stat().st_ino
    fsync = os.fsync
    count = 0

    def sync(fd):
        nonlocal count
        fsync(fd)
        if os.fstat(fd).st_ino == inode:
            count += 1

    monkeypatch.setattr(chat_store.os, "fsync", sync)
    assert journal.claim(root, claim)[0] is False
    assert journal.read(root, session_id).revision == 2
    assert count == 2


def test_sent_then_uncertain_can_never_become_not_sent_after_reopen(conversation):
    root, session_id, binding = conversation
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    assert journal.record_handoff(root, binding, operation_id, "sent")["handoff"] == "sent"
    assert (
        journal.record_handoff(root, binding, operation_id, "uncertain")["handoff"] == "uncertain"
    )
    assert journal.read(root, session_id).operations[operation_id].ever_sent is True
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.record_handoff(root, binding, operation_id, "not_sent")
    assert refused.value.code == "conflict"
    assert path_of(conversation).read_bytes() == before
    assert journal.claim(root, claim)[0] is False


def test_proved_not_sent_is_final_and_still_requires_a_new_operation_uuid(conversation):
    root, _, binding = conversation
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    receipt = journal.record_handoff(root, binding, operation_id, "not_sent")
    assert journal.claim(root, claim) == (False, receipt)
    before = path_of(conversation).read_bytes()
    assert journal.record_handoff(root, binding, operation_id, "not_sent") == receipt
    assert path_of(conversation).read_bytes() == before
    for status in ["sent", "uncertain"]:
        with pytest.raises(journal.JournalError) as refused:
            journal.record_handoff(root, binding, operation_id, status)
        assert refused.value.code == "conflict"


@pytest.mark.parametrize("field", ["worker_id", "connection_id"])
def test_handoff_receipt_requires_original_worker_connection(conversation, field):
    root, _, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    other = replace(binding, **{field: str(uuid.uuid4())})
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.record_handoff(root, other, claim["params"]["operation_id"], "sent")
    assert refused.value.code == "conflict"
    assert path_of(conversation).read_bytes() == before


def test_normalized_observations_have_durable_monotonic_cursors(conversation):
    root, session_id, binding = conversation
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    assert (
        journal.append_events(
            root, binding, [event(operation_id, "one"), event(operation_id, "two")]
        )
        == 5
    )
    journal.record_handoff(root, binding, operation_id, "uncertain")
    assert journal.append_events(root, binding, [event(operation_id, "three")]) == 7
    reopened = journal.read(root, session_id)
    page = reopened.page(0, limit=2)
    assert page["revision"] == 7 and page["next_cursor"] == 5
    assert [item["cursor"] for item in page["events"]] == [4, 5]
    assert [item["data"]["text"] for item in page["events"]] == ["one", "two"]
    rest = reopened.page(page["next_cursor"], limit=2)
    assert [item["cursor"] for item in rest["events"]] == [7]
    assert rest["next_cursor"] == 7
    assert reopened.page(rest["next_cursor"])["events"] == []
    assert all(set(item) == {"cursor", "kind", "data"} for item in page["events"])


@pytest.mark.parametrize("after,limit", [(8, 1), (-1, 1), (False, 1), (0, 0), (0, 101), (0, True)])
def test_event_cursor_and_page_size_are_strict(conversation, after, limit):
    root, session_id, _ = conversation
    with pytest.raises(journal.JournalError):
        journal.read(root, session_id).page(after, limit)


def test_event_for_wrong_worker_or_nonexistent_operation_is_not_persisted(conversation):
    root, _, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    other = replace(binding, worker_id=str(uuid.uuid4()))
    before = path_of(conversation).read_bytes()
    for owner, observed in [
        (other, event(claim["params"]["operation_id"])),
        (binding, event(str(uuid.uuid4()))),
    ]:
        with pytest.raises(journal.JournalError):
            journal.append_events(root, owner, [observed])
        assert path_of(conversation).read_bytes() == before


def test_private_frames_capabilities_and_unbounded_observations_never_enter_journal(conversation):
    root, _, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    operation_id = claim["params"]["operation_id"]
    observed = event(operation_id)
    hidden = copy.deepcopy(observed)
    hidden["data"]["capability"] = "a" * 64
    before = path_of(conversation).read_bytes()
    for invalid in [
        {"kind": "send", "data": {"frame": {"result": {"decision": "accept"}}}},
        hidden,
        event(operation_id, "x" * (native_ipc.MAX_EVENT_TEXT + 1)),
    ]:
        with pytest.raises(ValueError):
            journal.append_events(root, binding, [invalid])
        assert path_of(conversation).read_bytes() == before
    invalid_request = copy.deepcopy(claim)
    invalid_request["capability"] = "a" * 64
    with pytest.raises(native_ipc.IPCError):
        journal.claim(root, invalid_request)
    assert path_of(conversation).read_bytes() == before


def test_same_uuid_under_other_client_cannot_replay_or_append(conversation):
    root, session_id, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    other = replace(binding, session_key=f"other-api:{session_id}")
    replay = {**claim, **other.envelope("request")}
    before = path_of(conversation).read_bytes()
    for attempt in [
        lambda: journal.claim(root, replay),
        lambda: journal.record_handoff(root, other, claim["params"]["operation_id"], "sent"),
        lambda: journal.append_events(root, other, [event(claim["params"]["operation_id"])]),
    ]:
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "conflict"
        assert path_of(conversation).read_bytes() == before


@pytest.mark.parametrize("fault", ["world-readable", "hardlink", "symlink"])
def test_nonprivate_or_replaced_journal_cannot_acquire_claim(conversation, tmp_path, fault):
    root, _, binding = conversation
    path = path_of(conversation)
    if fault == "world-readable":
        path.chmod(0o644)
    elif fault == "hardlink":
        os.link(path, tmp_path / "other-link")
    else:
        original = tmp_path / "original.jsonl"
        path.rename(original)
        path.symlink_to(original)
    before = path.read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, request(binding))
    assert refused.value.code == "unavailable"
    assert path.read_bytes() == before


def test_journal_cannot_exceed_capacity_mid_claim(conversation, monkeypatch):
    root, _, binding = conversation
    path = path_of(conversation)
    before = path.read_bytes()
    monkeypatch.setattr(journal, "MAX_BYTES", len(before) + 8)
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, request(binding))
    assert refused.value.code == "unavailable"
    assert path.read_bytes() == before


def test_short_kernel_writes_are_finished_before_returning_receipt(conversation, monkeypatch):
    root, session_id, binding = conversation
    original = os.write
    inode = path_of(conversation).stat().st_ino
    writes = 0

    def write(fd, data):
        nonlocal writes
        if os.fstat(fd).st_ino == inode:
            writes += 1
            return original(fd, data[:31])
        return original(fd, data)

    monkeypatch.setattr(chat_store.os, "write", write)
    new, receipt = journal.claim(root, request(binding))
    assert new is True and writes > 1
    assert journal.read(root, session_id).operations[receipt["operation_id"]].handoff == "uncertain"
    assert path_of(conversation).read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "fault", ["duplicate-claim", "private-event", "unknown-record", "duplicate-field"]
)
def test_corrupt_complete_record_stays_occupied_instead_of_being_skipped(conversation, fault):
    root, session_id, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    path = path_of(conversation)
    stored = records(path)
    if fault == "duplicate-claim":
        extra = json.dumps(stored[-1]).encode() + b"\n"
    elif fault == "private-event":
        extra = (
            json.dumps(
                {
                    "type": "native_event",
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "ts": 1.0,
                    "event": {"kind": "send", "data": {"frame": {"result": "private"}}},
                }
            ).encode()
            + b"\n"
        )
    elif fault == "unknown-record":
        extra = (
            json.dumps(
                {
                    "type": "native_future_effect",
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "ts": 1.0,
                }
            ).encode()
            + b"\n"
        )
    else:
        extra = b'{"type":"native_operation","type":"native_handoff"}\n'
    with path.open("ab") as file:
        file.write(extra)
    occupied = path.read_bytes()
    for attempt in [lambda: journal.read(root, session_id), lambda: journal.claim(root, claim)]:
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "unavailable"
        assert path.read_bytes() == occupied


def test_overflowing_timestamp_is_a_typed_unavailable_journal(conversation):
    root, session_id, binding = conversation
    path = path_of(conversation)
    header = records(path)[0]
    header["created_at"] = 10**400
    path.write_bytes(json.dumps(header).encode() + b"\n")
    occupied = path.read_bytes()
    for attempt in [
        lambda: journal.read(root, session_id),
        lambda: journal.claim(root, request(binding)),
    ]:
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "unavailable"
        assert path.read_bytes() == occupied


def test_approval_observation_must_match_its_recorded_worker_generation(conversation):
    root, _, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    approval = {
        "kind": "approval",
        "data": {
            "operation_id": claim["params"]["operation_id"],
            "native_turn_id": "native-turn-1",
            "request_id": "s:approval-1",
            "item_id": "item-1",
            "tool": "Read",
            "summary": "Read file",
            "choices": ["approve", "reject"],
            "payload_digest": "a" * 64,
            "worker_id": binding.worker_id,
            "connection_id": str(uuid.uuid4()),
        },
    }
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.append_events(root, binding, [approval])
    assert refused.value.code == "unavailable"
    assert path_of(conversation).read_bytes() == before
    approval["data"]["connection_id"] = binding.connection_id
    journal.append_events(root, binding, [approval])
    reopened = journal.read(root, binding.session_key.partition(":")[2])
    stored = reopened.page(0)["events"][0]["data"]
    assert stored["worker_id"] == binding.worker_id
    assert stored["connection_id"] == binding.connection_id
    assert stored["payload_digest"] == "a" * 64


def test_record_limit_refuses_new_claim_without_consuming_operation(conversation, monkeypatch):
    root, _, binding = conversation
    journal.claim(root, request(binding))
    before = path_of(conversation).read_bytes()
    monkeypatch.setattr(journal, "MAX_RECORDS", 2)
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, request(binding, revision=2))
    assert refused.value.code == "unavailable"
    assert path_of(conversation).read_bytes() == before


def test_no_progress_append_never_returns_a_claim_receipt(conversation, monkeypatch):
    root, _, binding = conversation
    path = path_of(conversation)
    before = path.read_bytes()
    original = os.write
    inode = path.stat().st_ino

    def write(fd, data):
        return 0 if os.fstat(fd).st_ino == inode else original(fd, data)

    monkeypatch.setattr(chat_store.os, "write", write)
    with pytest.raises(journal.JournalError) as refused:
        journal.claim(root, request(binding))
    assert refused.value.code == "unavailable"
    assert path.read_bytes() == before


@pytest.mark.parametrize("private_field", ["capability", "frame"])
def test_native_header_refuses_private_or_raw_extra_fields(conversation, private_field):
    root, session_id, binding = conversation
    path = path_of(conversation)
    header = records(path)[0]
    header["request"][private_field] = (
        "a" * 64 if private_field == "capability" else {"method": "turn/start", "params": {}}
    )
    path.write_bytes(json.dumps(header).encode() + b"\n")
    occupied = path.read_bytes()
    for attempt in [
        lambda: journal.read(root, session_id),
        lambda: journal.claim(root, request(binding)),
    ]:
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "unavailable"
        assert path.read_bytes() == occupied


@pytest.mark.parametrize("action", ["decide", "interrupt", "stop"])
def test_turn_observation_cannot_bind_to_non_submit_operation(conversation, action):
    root, session_id, binding = conversation
    claim = request(binding, action=action)
    journal.claim(root, claim)
    operation_id = claim["params"]["operation_id"]
    before = path_of(conversation).read_bytes()
    with pytest.raises(journal.JournalError) as refused:
        journal.append_events(root, binding, [event(operation_id)])
    assert refused.value.code == "unavailable"
    assert path_of(conversation).read_bytes() == before
    reopened = journal.read(root, session_id)
    assert reopened.operations[operation_id].request["action"] == action
    assert reopened.events == []


def test_incremental_fold_matches_a_full_fold_and_stays_cheap(conversation):
    """Review of #1278: every transaction re-parsed the whole file (≈0.5 s at 10k records)."""
    import time

    root, session_id, binding = conversation
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    for _ in range(100):
        journal.append_events(root, binding, [event(operation_id)] * 100)
    started = time.monotonic()
    for _ in range(20):
        journal.append_events(root, binding, [event(operation_id)])
    assert (time.monotonic() - started) / 20 < 0.1  # incremental: not O(10k records) each
    incremental = journal.read(root, session_id)
    journal._CACHE.clear()
    full = journal.read(root, session_id)
    assert incremental.revision == full.revision == 10_022
    assert incremental.events == full.events and incremental.operations == full.operations
    assert full.page(10_000, 5) == incremental.page(10_000, 5)


def test_a_rewritten_journal_is_never_served_from_the_fold_cache(conversation):
    root, session_id, binding = conversation
    journal.claim(root, request(binding))
    assert journal.read(root, session_id).revision == 2
    path = path_of(conversation)
    header = path.read_bytes().split(b"\n")[0] + b"\n"
    path.write_bytes(header)  # same file, shorter content: the cached prefix must not apply
    assert journal.read(root, session_id).revision == 1


def test_a_same_length_interior_rewrite_is_never_served_from_the_fold_cache(conversation):
    """Hermes on #1278: the cache compared only length and a tail sample."""
    root, session_id, binding = conversation
    claim = request(binding)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.append_events(root, binding, [event(operation_id)] * 20)
    original = claim["params"]["text"]
    assert (
        journal.read(root, session_id).operations[operation_id].request["params"]["text"]
        == original
    )
    path = path_of(conversation)
    raw = path.read_bytes()
    changed = "Z" * len(original)
    path.write_bytes(raw.replace(json.dumps(original).encode(), json.dumps(changed).encode(), 1))
    assert len(path.read_bytes()) == len(raw)
    folded = journal.read(root, session_id)
    assert folded.operations[operation_id].request["params"]["text"] == changed
