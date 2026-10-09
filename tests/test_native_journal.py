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
from concurrent.futures import ThreadPoolExecutor
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
            actor="operator",
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


def test_queue_is_durable_and_requires_prewrite_transition(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    fresh, receipt = journal.claim(root, claim, queued=True)
    assert fresh and receipt["handoff"] == "queued"
    journal._CACHE.clear()  # a reload must preserve the explicit non-handoff state
    assert journal.read(root, sid).operations[receipt["operation_id"]].handoff == "queued"
    assert journal.claim(root, claim, queued=True) == (False, receipt)
    with pytest.raises(journal.JournalError):
        journal.record_handoff(root, binding, receipt["operation_id"], "sent")
    uncertain = journal.record_handoff(root, binding, receipt["operation_id"], "uncertain")
    journal._CACHE.clear()
    assert journal.claim(root, claim, queued=True) == (False, uncertain)
    with pytest.raises(journal.JournalError):
        journal.record_handoff(root, binding, receipt["operation_id"], "queued")
    with pytest.raises(journal.JournalError):
        journal.record_handoff(
            root, replace(binding, worker_id=str(uuid.uuid4())), receipt["operation_id"], "sent"
        )


@pytest.mark.parametrize("bound", ["MAX_QUEUED", "MAX_QUEUE_BYTES"])
def test_full_queue_refuses_without_claim_and_still_replays(conversation, monkeypatch, bound):
    root, sid, binding = conversation
    first = request(binding)
    _, receipt = journal.claim(root, first, queued=True)
    monkeypatch.setattr(journal, bound, 1)
    next_request = request(binding, revision=2)
    with pytest.raises(journal.JournalError, match="queue is full"):
        journal.claim(root, next_request, queued=True)
    assert next_request["params"]["operation_id"] not in journal.read(root, sid).operations
    assert journal.claim(root, first, queued=True) == (False, receipt)


def test_only_submits_can_wait_in_queue(conversation):
    root, _, binding = conversation
    with pytest.raises(journal.JournalError, match="only operator"):
        journal.claim(root, request(binding, action="interrupt"), queued=True)


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


def test_unchanged_observation_skips_bytes_but_still_checks_privacy_and_fsync(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    journal.claim(root, request(binding))
    journal.read(root, sid)
    inode = path_of(conversation).stat().st_ino
    pread, fsync = os.pread, os.fsync
    synced = []

    def read(fd, *args):
        assert os.fstat(fd).st_ino != inode, "unchanged observation reread the journal"
        return pread(fd, *args)

    def sync(fd):
        synced.append(os.fstat(fd).st_ino)
        return fsync(fd)

    monkeypatch.setattr(os, "pread", read)
    monkeypatch.setattr(os, "fsync", sync)
    assert journal.read(root, sid).revision == 2
    assert inode in synced
    path_of(conversation).chmod(0o644)
    with pytest.raises(journal.JournalError):
        journal.read(root, sid)


def test_restored_mtime_cannot_hide_rewritten_claim_from_read_or_replay(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    journal.read(root, sid)
    path = path_of(conversation)
    st = path.stat()
    old = claim["params"]["text"]
    path.write_bytes(path.read_bytes().replace(old.encode(), b"x" * len(old)))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (
        journal.read(root, sid)
        .operations[claim["params"]["operation_id"]]
        .request["params"]["text"]
        != old
    )
    with pytest.raises(journal.JournalError, match="already binds"):
        journal.claim(root, claim)


def test_cached_observation_never_skips_effect_time_content_verification(conversation, monkeypatch):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    journal.read(root, sid)
    inode = path_of(conversation).stat().st_ino
    original = os.pread
    reads = []

    def read(fd, size, offset):
        if os.fstat(fd).st_ino == inode:
            reads.append((size, offset))
        return original(fd, size, offset)

    monkeypatch.setattr(os, "pread", read)
    assert journal.claim(root, claim)[0] is False
    assert reads and reads[0][1] == 0


def test_event_payloads_spill_privately_and_old_snapshot_remains_immutable(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    operation = claim["params"]["operation_id"]
    journal.append_events(root, binding, [event(operation, "large " * 1000)] * 100)
    old = journal.read(root, sid)
    assert old.events.memory_bytes < path_of(conversation).stat().st_size // 10
    st = os.fstat(old.events._spool.file.fileno())
    assert st.st_mode & 0o777 == 0o600 and st.st_nlink == 0
    assert st.st_dev == root.stat().st_dev
    page = old.page(0, 1)
    journal.append_events(root, binding, [event(operation, "new")])
    new = journal.read(root, sid)
    assert len(old.events) == 100 and len(new.events) == 101
    assert old.page(0, 1) == page
    assert new.page(old.revision, 1)["events"][0]["data"]["text"] == "new"


@pytest.mark.parametrize("budget", ["_CACHE_BYTES", "_CACHE_DISK_BYTES"])
def test_byte_budget_eviction_preserves_old_event_pages_and_exact_replay(
    conversation, monkeypatch, budget
):
    root, sid, binding = conversation
    claim = request(binding)
    _, receipt = journal.claim(root, claim)
    journal.append_events(root, binding, [event(claim["params"]["operation_id"])])
    expected = journal.read(root, sid).page(0)
    monkeypatch.setattr(journal, budget, 1)
    assert journal.read(root, sid).page(0) == expected
    assert sid not in journal._CACHE
    assert journal.claim(root, claim) == (False, receipt)


def test_failed_preview_does_not_publish_events_to_cached_snapshot(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    before = journal.read(root, sid)
    with pytest.raises(journal.JournalError):
        journal.append_events(
            root, binding, [event(claim["params"]["operation_id"]), event(str(uuid.uuid4()))]
        )
    assert len(before.events) == 0 and len(journal.read(root, sid).events) == 0


def test_spilling_unicode_does_not_amplify_the_journals_byte_bound(conversation):
    from agent_sessions import native_events

    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    operation = claim["params"]["operation_id"]
    text = "é漢🙂" * 6000
    journal.append_events(root, binding, [event(operation, text)] * 20)
    view = journal.read(root, sid)
    assert view.events.disk_bytes < path_of(conversation).stat().st_size
    assert view.page(0, 1)["events"][0]["data"]["text"] == text
    # A legacy JSON escape may decode to an unpaired surrogate; the derived cache
    # preserves even that value rather than replacing it or failing a valid fold.
    events = native_events.Events(root)
    item = {"cursor": 1, "event": event(operation, "escaped \ud800")}
    events.append(item)
    assert events[0] == item


def test_cached_read_observes_replacement_and_refuses_truncation(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    journal.read(root, sid)
    path = path_of(conversation)
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(
        path.read_bytes().replace(b"Inspect the project", b"Inspect new project")
    )
    replacement.chmod(0o600)
    replacement.replace(path)
    operation = claim["params"]["operation_id"]
    assert journal.read(root, sid).operations[operation].request["params"]["text"] == (
        "Inspect new project"
    )
    with pytest.raises(journal.JournalError):
        journal.claim(root, claim)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(journal.JournalError):
        journal.read(root, sid)


def test_concurrent_observers_and_appends_keep_complete_immutable_pages(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    operation = claim["params"]["operation_id"]
    old = journal.read(root, sid)

    def append():
        for i in range(30):
            journal.append_events(root, binding, [event(operation, str(i))])

    def observe():
        revision = 0
        for _ in range(30):
            view = journal.read(root, sid)
            assert view.revision >= revision
            revision = view.revision
            rows = view.page(0)["events"]
            assert [row["data"]["text"] for row in rows] == [str(i) for i in range(len(rows))]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(append), *(pool.submit(observe) for _ in range(3))]
        for future in futures:
            future.result(timeout=30)
    assert not old.page(0)["events"]
    assert len(journal.read(root, sid).page(0)["events"]) == 30


def test_spill_failure_never_commits_a_claim_and_recovery_preserves_history(
    conversation, monkeypatch
):
    from agent_sessions import native_events

    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    journal.append_events(root, binding, [event(claim["params"]["operation_id"])])
    before = path_of(conversation).read_bytes()
    expected = journal.read(root, sid).page(0)
    journal._CACHE.clear()

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(native_events.tempfile, "TemporaryFile", fail)
        with pytest.raises(journal.JournalError):
            journal.read(root, sid)
        with pytest.raises(journal.JournalError):
            journal.claim(root, request(binding, revision=expected["revision"]))
    assert path_of(conversation).read_bytes() == before
    assert journal.read(root, sid).page(0) == expected
    assert journal.claim(root, claim)[0] is False


def test_full_derived_spool_rebuilds_without_losing_history(conversation):
    root, sid, binding = conversation
    claim = request(binding)
    journal.claim(root, claim)
    operation = claim["params"]["operation_id"]
    journal.append_events(root, binding, [event(operation, "old")])
    old = journal.read(root, sid)
    old.events._spool.size = 128 * 1024 * 1024
    journal.append_events(root, binding, [event(operation, "new")])
    assert [row["data"]["text"] for row in journal.read(root, sid).page(0)["events"]] == [
        "old",
        "new",
    ]
    assert old.page(0)["events"][0]["data"]["text"] == "old"


def test_bounded_projection_reads_recent_payloads_and_preserves_global_facts(
    conversation, monkeypatch
):
    from agent_sessions import native_events, native_runtime, structured_runtime

    root, sid, binding = conversation
    turns = []
    for i in range(55):
        claim = request(binding, revision=journal.read(root, sid).revision)
        journal.claim(root, claim)
        turns.append(claim["params"]["operation_id"])
        journal.append_events(root, binding, [event(turns[-1], f"answer {i}")])
    journal.append_events(
        root,
        binding,
        [
            {
                "kind": "session",
                "data": {
                    "native_id": "native-history",
                    "model_configured": None,
                    "model_effective": None,
                },
            },
            {
                "kind": "model",
                "data": {
                    "operation_id": turns[0],
                    "native_turn_id": "old-turn",
                    "model_effective": "effective-model",
                },
            },
            {
                "kind": "turn_completed",
                "data": {
                    "operation_id": turns[0],
                    "native_turn_id": "old-turn",
                    "state": "completed",
                    "error": "",
                    "background_active": True,
                },
            },
            {
                "kind": "approval",
                "data": {
                    "operation_id": turns[-1],
                    "native_turn_id": "current-turn",
                    "request_id": "s:current",
                    "item_id": "current-item",
                    "tool": "Read",
                    "summary": "Read file",
                    "choices": ["approve", "reject"],
                    "payload_digest": "a" * 64,
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "complete": True,
                },
            },
        ],
    )
    record = {"request": {"model": None}, "adapter": binding.adapter}
    folded = journal.read(root, sid)
    full = native_runtime.project(folded, record, binding.worker_id)
    read_item = native_events.Events.__getitem__
    read_turns = []

    def counted(self, index):
        item = read_item(self, index)
        if item["event"]["kind"] == "text":
            read_turns.append(item["event"]["data"].get("operation_id"))
        return item

    monkeypatch.setattr(native_events.Events, "__getitem__", counted)
    limited = native_runtime.project(folded, record, binding.worker_id, max_turns=50)
    assert limited["turns"] == full["turns"][-50:]
    assert limited["omitted_turns"] == 5
    assert limited["native"] == full["native"]
    assert limited["native"]["background_active"] is True
    assert limited["model_effective"] == full["model_effective"] == "effective-model"
    assert limited["pending_requests"] == full["pending_requests"]
    assert limited["pending_requests"][0]["request_id"] == "s:current"
    assert limited["in_flight"] == full["in_flight"] == turns[-1]
    assert not set(turns[:5]) & set(read_turns)
    assert structured_runtime._snapshot(binding.session_key, limited)["omitted_turns"] == 5


def test_a_decision_written_before_actor_binding_still_folds(conversation):
    """Hermes on #1278: requiring `actor` on stored records made pre-upgrade journals unreadable."""
    root, session_id, binding = conversation
    claim = request(binding, action="decide", decision="reject")
    journal.claim(root, claim)
    path = path_of(conversation)
    lines = path.read_bytes().split(b"\n")
    legacy = []
    for line in lines:
        if b'"native_operation"' in line:
            record = json.loads(line)
            record["request"]["params"].pop("actor")
            line = json.dumps(record).encode()
        legacy.append(line)
    path.write_bytes(b"\n".join(legacy))
    journal._CACHE.clear()
    folded = journal.read(root, session_id)
    op = folded.operations[claim["params"]["operation_id"]]
    assert op.request["action"] == "decide" and "actor" not in op.request["params"]
