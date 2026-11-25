"""An API session's journal rolls over to a new segment instead of bricking at its bound (#1733).

Real private tmp-path journals, real flocks and renames. Segments are small here (the soft
triggers are lowered per test) except where a test pins the production shape: the legacy file
observed in the field (67,108,716 bytes / 27,475 records) and a claude-shaped journal that
reaches the RECORD trigger first. Those are generated at test time, never committed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from agent_sessions import chat_store, native_ipc, native_segments
from agent_sessions import native_journal as journal
from test_native_journal import event, request


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


@pytest.fixture
def small(monkeypatch):
    """Segments of a few dozen records, so a test rolls over in milliseconds."""
    monkeypatch.setattr(journal, "SOFT_RECORDS", 40)
    monkeypatch.setattr(journal, "SOFT_BYTES", 256 * 1024)


def seg(root, sid, index):
    return native_segments.segment_path(root, sid, index)


def segments(root, sid):
    return [0, *native_segments.indexes(root, sid)]


def lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def turn(root, binding, revision=None, text="Inspect the project", **kw):
    """Claim a submit at the journal's current revision; return its operation id."""
    sid = binding.session_key.partition(":")[2]
    current = journal.read(root, sid).revision if revision is None else revision
    claim = request(binding, revision=current, text=text, **kw)
    assert journal.claim(root, claim)[0] is True
    return claim


def completed(operation_id, text="done"):
    return {
        "kind": "turn_completed",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "state": "completed",
            "error": "",
            "text": text,
        },
    }


def fill(root, binding, operation_id, count, text="Answer"):
    for start in range(0, count, 50):
        journal.append_events(root, binding, [event(operation_id, text)] * min(50, count - start))


# --- segment naming, header, pins --------------------------------------------------------------


def test_rollover_keeps_one_continuous_history_behind_a_pinned_successor(conversation, small):
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    fill(root, binding, op, 30)
    before = seg(root, sid, 0).read_bytes()
    fill(root, binding, op, 30)
    assert segments(root, sid) == [0, 1]
    assert seg(root, sid, 0).read_bytes() == before  # never rewritten
    assert seg(root, sid, 0).stat().st_mode & 0o777 == 0o400  # frozen: read-only
    head = lines(seg(root, sid, 1))[0]
    assert head["type"] == "segment" and head["index"] == 1
    assert head["prev"] == {"size": len(before), "sha256": hashlib.sha256(before).hexdigest()}
    assert head["base_revision"] == before.count(b"\n")
    folded = journal.read(root, sid)
    assert folded.revision == 2 + 60  # header + claim + 60 events: cursors never restart
    cursors = [item["cursor"] for item in folded.events]
    assert cursors == list(range(3, 63))
    # Neither a segment, its sidecar nor the lock file is a session.
    assert {p.name for p in root.iterdir() if not p.name.startswith(".")} == {
        f"{sid}.jsonl",
        f"{sid}.1.jsonl",
        f"{sid}.0.ops",
        f"{sid}.lock",
    }
    assert chat_store.session_ids(root) == [sid]
    for name in (f"{sid}.1.jsonl", f"{sid}.0.ops", f"{sid}.lock"):
        assert (root / name).stat().st_mode & 0o777 in (0o600, 0o400)


def test_a_cursor_in_an_earlier_segment_pages_forward_across_the_boundary(conversation, small):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    fill(root, binding, op, 100)
    assert segments(root, sid) == [0, 1, 2]
    folded = journal.read(root, sid)
    seen, after = [], 0
    while True:
        page = folded.page(after, 7)
        seen.extend(item["cursor"] for item in page["events"])
        if page["next_cursor"] == after or not page["events"]:
            break
        after = page["next_cursor"]
    assert seen == list(range(3, 103))
    # A cursor just before a boundary pages straight across it.
    boundary = lines(seg(root, sid, 2))[0]["base_revision"]
    page = folded.page(boundary - 2, 4)
    assert [e["cursor"] for e in page["events"]] == list(range(boundary - 1, boundary + 3))


def test_one_turn_spanning_three_segments_renders_settles_and_pages(conversation, small):
    from agent_sessions import native_runtime

    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    for part in ("first", "second", "third"):
        fill(root, binding, op, 45, text=part)
    journal.append_events(root, binding, [completed(op)])
    assert len(segments(root, sid)) >= 3
    view = native_runtime.project(
        journal.read(root, sid), {"request": {"model": None}, "adapter": binding.adapter}, None
    )
    (shown,) = view["turns"]
    assert shown["status"] == "done"
    assert all(part in shown["reply"] for part in ("first", "second", "third"))
    assert shown["reply"].count("first") == 45 and shown["reply"].count("third") == 45
    # The carry never copied that turn's content: every successor header is small.
    for index in segments(root, sid)[1:]:
        assert len(seg(root, sid, index).read_bytes().split(b"\n", 1)[0]) < 4096


# --- replay across segments: sidecars, the many-segment fixture --------------------------------


def test_replay_of_an_old_operation_id_after_rollover(conversation, small):
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    fill(root, binding, op, 80)
    assert len(segments(root, sid)) >= 3
    fresh, receipt = journal.claim(root, claim)  # same request: the original receipt
    assert fresh is False and receipt["handoff"] == "sent" and receipt["operation_id"] == op
    changed = dict(claim, params={**claim["params"], "text": "Something else"})
    with pytest.raises(journal.JournalError, match="already binds"):
        journal.claim(root, changed)
    assert (
        sum(
            1
            for i in segments(root, sid)
            for r in lines(seg(root, sid, i))
            if r.get("type") == "native_operation" and r["operation_id"] == op
        )
        == 1
    )


def test_many_segments_of_operations_replay_and_the_carry_stays_flat(conversation, small):
    """More settled operations than the old 4 MiB carry ceiling could ever hold (#1733): they
    replay from their frozen segments' sidecars, and no successor carries them."""
    root, sid, binding = conversation
    claims = []
    big = "x" * 60_000
    for _ in range(80):  # 80 × 60 kB of request text ≈ 4.6 MiB of settled operations
        claim = turn(root, binding, text=big + str(len(claims)))
        op = claim["params"]["operation_id"]
        journal.record_handoff(root, binding, op, "sent")
        journal.append_events(root, binding, [completed(op)])
        claims.append(claim)
    indexes = segments(root, sid)
    assert len(indexes) > 10
    assert sum(len(c["params"]["text"]) for c in claims) > 4 * 1024 * 1024
    headers = [len(seg(root, sid, i).read_bytes().split(b"\n", 1)[0]) for i in indexes[1:]]
    assert max(headers) < 2048  # flat: settled operations are never carried
    journal._SIDECARS.clear()
    journal._INDEX.clear()
    for claim in (claims[0], claims[37], claims[-2]):
        fresh, receipt = journal.claim(root, claim)
        assert fresh is False and receipt["handoff"] == "sent"
        reused = dict(claim, params={**claim["params"], "text": "another request"})
        with pytest.raises(journal.JournalError, match="already binds"):
            journal.claim(root, reused)
    assert len(journal.read(root, sid).operations) == 80


# --- the legacy file and the soft triggers -----------------------------------------------------


def _legacy(root, sid, binding, *, size, records):
    """Write segment 0 at EXACTLY ``size`` bytes / ``records`` lines, no marker, as the field
    journal was: a header, one claim, its sent handoff, then text events."""
    path = seg(root, sid, 0)
    head = path.read_bytes()
    claim = request(binding, revision=1)
    op = claim["params"]["operation_id"]
    immutable = native_ipc.immutable_request(claim)
    out = [
        {
            "type": "native_operation",
            "operation_id": op,
            "request": immutable,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": 1.0,
        },
        {
            "type": "native_handoff",
            "operation_id": op,
            "handoff": "sent",
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": 1.0,
        },
    ]

    def text_event(text):
        return {
            "type": "native_event",
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": 2.0,
            "event": {
                "kind": "text",
                "data": {
                    "operation_id": op,
                    "native_turn_id": "native-turn-1",
                    "item_id": "item-1",
                    "text": text,
                    "partial": False,
                    "truncated": False,
                },
            },
        }

    fixed = len(head) + len(chat_store._records_bytes(out))
    count = records - 1 - len(out)
    empty = len(chat_store._records_bytes([text_event("")]))
    budget = size - fixed - count * empty  # characters of text to spread over the events
    assert budget >= 0
    each, rest = divmod(budget, count)
    body = [text_event("a" * (each + (1 if i < rest else 0))) for i in range(count)]
    data = head + chat_store._records_bytes(out) + chat_store._records_bytes(body)
    path.write_bytes(data)
    assert len(data) == size and data.count(b"\n") == records
    return op


def test_the_bricked_field_journal_rolls_over_on_its_next_send(conversation):
    """codex-api:9471d32e… stopped at 67,108,716 bytes / 27,475 records: every append failed."""
    root, sid, binding = conversation
    _legacy(root, sid, binding, size=67_108_716, records=27_475)
    original = hashlib.sha256(seg(root, sid, 0).read_bytes()).hexdigest()
    assert journal.read(root, sid).revision == 27_475
    fresh, receipt = journal.claim(root, request(binding, revision=27_475))
    assert fresh is True and receipt["recorded_revision"] == 27_476
    assert segments(root, sid) == [0, 1]
    assert hashlib.sha256(seg(root, sid, 0).read_bytes()).hexdigest() == original
    journal._CACHE.clear()
    journal._CHAIN.clear()
    assert journal.read(root, sid).revision == 27_476


def test_the_byte_trigger_rolls_over_exactly_past_its_bound(conversation, monkeypatch):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    one = len(
        chat_store._records_bytes(
            [
                {
                    "type": "native_event",
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "ts": time.time(),
                    "event": native_ipc.normalize_event(event(op)),
                }
            ]
        )
    )
    size = seg(root, sid, 0).stat().st_size
    # One more record fits (a `ts` float varies by a few bytes: slack well under one record).
    monkeypatch.setattr(journal, "SOFT_BYTES", size + one + 16)
    assert one > 64
    monkeypatch.setattr(journal, "SOFT_RECORDS", 10_000)
    journal.append_events(root, binding, [event(op)])
    assert segments(root, sid) == [0]
    journal.append_events(root, binding, [event(op)])
    assert segments(root, sid) == [0, 1]


def test_the_record_trigger_rolls_over_exactly_past_its_bound(conversation, monkeypatch):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    monkeypatch.setattr(journal, "SOFT_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(journal, "SOFT_RECORDS", 5)  # 2 lines now: three more fit
    journal.append_events(root, binding, [event(op)] * 3)
    assert segments(root, sid) == [0]
    journal.append_events(root, binding, [event(op)])
    assert segments(root, sid) == [0, 1]


def test_a_claude_shaped_journal_reaches_the_record_trigger_first(conversation, monkeypatch):
    """claude-api writes ~7 small records per tool call, most of them `background` reports."""
    root, sid, binding = conversation
    path = seg(root, sid, 0)
    op = turn(root, binding)["params"]["operation_id"]
    w, c = binding.worker_id, binding.connection_id

    def record(kind, data):
        return {
            "type": "native_event",
            "worker_id": w,
            "connection_id": c,
            "ts": time.time(),
            "event": {"kind": kind, "data": data},
        }

    tool = {"operation_id": op, "native_turn_id": "t1", "item_id": "call-1"}
    call = [
        record("tool", {**tool, "tool": "Bash", "state": "running", "summary": "ls -la src"}),
        *[record("background", {"state": "running", "active": True, "task_id": "task-1"})] * 5,
        record("tool", {**tool, "state": "completed", "output": "ok", "completed": True}),
    ]
    per_record = len(chat_store._records_bytes(call)) / len(call)
    # At the PRODUCTION triggers this shape reaches 90k records at well under 56 MiB.
    assert per_record * journal.SOFT_RECORDS < journal.SOFT_BYTES / 2
    monkeypatch.setattr(journal, "SOFT_RECORDS", 9_000)  # the same shape, a tenth of the size
    have = path.read_bytes().count(b"\n")
    calls = (journal.SOFT_RECORDS - have) // len(call)
    with path.open("ab") as fh:
        fh.write(chat_store._records_bytes(call * calls))
    spare = journal.SOFT_RECORDS - path.read_bytes().count(b"\n")
    journal.append_events(root, binding, [event(op)] * spare)  # exactly at the trigger: no
    assert segments(root, sid) == [0]
    journal.append_events(root, binding, [event(op)])
    assert segments(root, sid) == [0, 1]
    head = lines(seg(root, sid, 1))[0]["carried"]
    # The live generation's latest background report per task rides along — and no tool event.
    kinds = [r["event"]["kind"] for _, r in head["records"] if r["type"] == "native_event"]
    assert kinds == ["background"]


# --- crash points and fault injection ------------------------------------------------------------


def _temporary(path):
    return Path(path).name.startswith(".") and Path(path).name.endswith(".new")


@pytest.mark.parametrize(
    ("name", "error", "cause"),
    [
        ("successor-enospc", errno.ENOSPC, "disk_full"),
        ("fsync-eio", errno.EIO, "io_error"),
        ("rename-failure", errno.EXDEV, "write_failed"),
    ],
)
def test_a_rollover_that_cannot_be_written_is_a_refusal_that_wrote_nothing(
    conversation, monkeypatch, name, error, cause
):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    monkeypatch.setattr(journal, "SOFT_RECORDS", 6)
    fill(root, binding, op, 3)
    before = seg(root, sid, 0).read_bytes()
    original_open, original_fsync, original_rename = os.open, os.fsync, os.rename
    temporaries: list[int] = []

    def opener(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if _temporary(path):
            temporaries.append(fd)
        return fd

    def write_fault(fd, data):
        if name == "successor-enospc" and fd in temporaries[1:]:
            raise OSError(error, os.strerror(error))
        return original_write(fd, data)

    def fsync_fault(fd):
        if name == "fsync-eio" and fd in temporaries:
            raise OSError(error, os.strerror(error))
        return original_fsync(fd)

    def rename_fault(src, dst, *args, **kwargs):
        if name == "rename-failure" and str(dst).endswith(".1.jsonl"):
            raise OSError(error, os.strerror(error))
        return original_rename(src, dst, *args, **kwargs)

    original_write = os.write
    with monkeypatch.context() as faults:
        faults.setattr(os, "open", opener)
        faults.setattr(os, "write", write_fault)
        faults.setattr(os, "fsync", fsync_fault)
        faults.setattr(os, "rename", rename_fault)
        with pytest.raises(journal.JournalError) as refused:
            journal.append_events(root, binding, [event(op)] * 3)
    assert (refused.value.code, refused.value.cause) == ("unwritable", cause)
    assert segments(root, sid) == [0]
    assert seg(root, sid, 0).read_bytes() == before  # nothing appended anywhere
    assert not [p for p in root.iterdir() if _temporary(p)]  # nothing staged left behind
    assert journal.read(root, sid).revision == before.count(b"\n")
    journal.append_events(root, binding, [event(op)] * 3)  # space is back: it rolls over
    assert segments(root, sid) == [0, 1]


def test_a_crash_between_sidecar_and_successor_leaves_an_orphan_nothing_trusts(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    monkeypatch.setattr(journal, "SOFT_RECORDS", 8)
    original = os.rename

    def crash(src, dst, *args, **kwargs):
        if str(dst).endswith(".1.jsonl"):
            raise OSError(errno.EIO, "power lost")  # the process dies right here
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", crash)
    with pytest.raises(journal.JournalError):
        fill(root, binding, op, 10)
    monkeypatch.setattr(os, "rename", original)
    orphan = native_segments.sidecar_path(root, sid, 0)
    assert orphan.exists() and segments(root, sid) == [0]
    stale = json.loads(orphan.read_bytes())
    # Segment 0 stays active and keeps taking appends: the orphan's identity goes stale.
    monkeypatch.setattr(journal, "SOFT_RECORDS", 10_000)
    fill(root, binding, op, 3)
    monkeypatch.setattr(journal, "SOFT_RECORDS", 8)
    journal.append_events(root, binding, [event(op)] * 2)  # 7 lines + 2 > 8: rolls over now
    assert segments(root, sid) == [0, 1]
    fresh = json.loads(orphan.read_bytes())
    assert fresh["size"] == seg(root, sid, 0).stat().st_size != stale["size"]
    assert journal.claim(root, claim)[0] is False


@pytest.mark.parametrize("damage", ["missing", "tampered-entry", "tampered-identity", "garbage"])
def test_a_missing_or_tampered_sidecar_is_rebuilt_from_its_pinned_segment(
    conversation, small, damage
):
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    fill(root, binding, op, 60)
    sidecar = native_segments.sidecar_path(root, sid, 0)
    doc = json.loads(sidecar.read_bytes())
    if damage == "missing":
        sidecar.unlink()
    elif damage == "tampered-entry":
        doc["entries"][op]["digest"] = "0" * 64
        sidecar.write_text(json.dumps(doc) + "\n")
    elif damage == "tampered-identity":
        doc["size"] += 1
        sidecar.write_text(json.dumps(doc) + "\n")
    else:
        sidecar.write_bytes(b"{not json\n")
    journal._SIDECARS.clear()
    journal._INDEX.clear()
    fresh, receipt = journal.claim(root, claim)
    assert fresh is False and receipt["handoff"] == "sent"
    with pytest.raises(journal.JournalError, match="already binds"):
        journal.claim(root, dict(claim, params={**claim["params"], "text": "other"}))
    assert json.loads(sidecar.read_bytes())["entries"][op]["digest"] != "0" * 64


@pytest.mark.parametrize("damage", ["tampered", "appended", "missing-middle"])
def test_a_changed_or_missing_frozen_segment_makes_the_whole_chain_unavailable(
    conversation, small, damage
):
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    fill(root, binding, op, 100)
    assert len(segments(root, sid)) >= 3
    frozen = seg(root, sid, 0)
    frozen.chmod(0o600)
    if damage == "tampered":
        raw = frozen.read_bytes()
        frozen.write_bytes(raw.replace(b"Inspect the project", b"Inspect the PROJECT", 1))
    elif damage == "appended":
        with frozen.open("ab") as fh:
            fh.write(b"\n")
    else:
        seg(root, sid, 1).unlink()
    journal._SIDECARS.clear()
    journal._INDEX.clear()
    for attempt in (
        lambda: journal.read(root, sid),
        lambda: journal.claim(root, claim),  # never re-runs an old id
        lambda: journal.claim(root, request(binding, revision=10**6)),
        lambda: journal.append_events(root, binding, [event(op)]),
    ):
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "unavailable"


def test_a_torn_or_oversize_active_segment_is_unavailable_not_full(conversation, small):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    fill(root, binding, op, 60)
    chain = segments(root, sid)
    active = seg(root, sid, chain[-1])
    with active.open("ab") as fh:
        fh.write(b'{"type": "native_event"')
    for attempt in (
        lambda: journal.append_events(root, binding, [event(op)] * 50),
        lambda: journal.read(root, sid),
        lambda: journal.prepare(root, sid),
    ):
        with pytest.raises(journal.JournalError) as refused:
            attempt()
        assert refused.value.code == "unavailable"
    assert segments(root, sid) == chain  # a corrupt journal never masquerades as full


def test_two_processes_claim_concurrently_across_a_rollover(conversation, tmp_path):
    """The stable lock (#1733): six OS processes claim — three the same operation, three their
    own — while the first claim rolls the journal over. Exactly one successor results, every
    distinct claim is recorded exactly once, the duplicates replay the one receipt."""
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    fill(root, binding, op, 10)
    trigger = seg(root, sid, 0).read_bytes().count(b"\n") + 1
    shared = request(binding)
    own = [request(binding) for _ in range(3)]
    script = """
import json, sys
from pathlib import Path
from agent_sessions import native_journal as journal
payload = json.loads(sys.stdin.readline())
journal.SOFT_RECORDS = payload['trigger']
root, sid, claim = Path(payload['root']), payload['sid'], payload['request']
for _ in range(200):
    claim['params']['expected_revision'] = journal.read(root, sid).revision
    try:
        new, receipt = journal.claim(root, claim)
        break
    except journal.JournalError as exc:
        if 'revision changed' not in exc.detail:
            raise
print(json.dumps({'new': new, 'receipt': receipt}), flush=True)
"""
    child_home = tmp_path / "child-home"
    child_home.mkdir(mode=0o700)
    env = dict(os.environ, HOME=str(child_home), PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    claims = [shared, shared, shared, *own]
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in claims
    ]
    outcomes = []
    try:
        for child, claim in zip(children, claims, strict=True):
            child.stdin.write(
                json.dumps({"root": str(root), "sid": sid, "request": claim, "trigger": trigger})
                + "\n"
            )
            child.stdin.flush()
            child.stdin.close()
            child.stdin = None
        for child in children:
            stdout, stderr = child.communicate(timeout=60)
            assert child.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert segments(root, sid) == [0, 1]  # one rollover, one active segment
    shared_id = shared["params"]["operation_id"]
    assert sum(o["new"] for o in outcomes[:3]) == 1
    assert len({json.dumps(o["receipt"], sort_keys=True) for o in outcomes[:3]}) == 1
    assert all(o["new"] for o in outcomes[3:])
    recorded = [
        r["operation_id"]
        for i in segments(root, sid)
        for r in lines(seg(root, sid, i))[(1 if i else 0) :]
        if r["type"] in {"native_operation", "native_queued"}
    ]
    expected = {shared_id, *(c["params"]["operation_id"] for c in own)}
    assert sorted(r for r in recorded if r in expected) == sorted(expected)
    assert len(journal.read(root, sid).operations) == 5


# --- the stable lock, discovery, growth and the wake stamp ---------------------------------------


def test_only_writers_create_the_lock_and_a_frozen_segment_refuses_a_pre_chain_writer(
    conversation, small
):
    root, sid, binding = conversation
    journal.read(root, sid)
    assert not native_segments.lock_path(root, sid).exists()  # reads never write
    op = turn(root, binding)["params"]["operation_id"]
    assert native_segments.lock_path(root, sid).stat().st_mode & 0o777 == 0o600
    fill(root, binding, op, 60)
    # A worker of the previous release appends through chat_store's per-file lock: it fails
    # instead of writing behind the successor.
    with pytest.raises(PermissionError):
        chat_store.transaction(root, sid, lambda raw: ([{"type": "x"}], None), max_bytes=1 << 26)


def test_discovery_growth_and_wake_stamp_follow_the_active_segment(conversation, small):
    from test_native_discovery_rows import kind

    root, sid, binding = conversation
    op = turn(root, binding, text="First actual prompt")["params"]["operation_id"]
    fill(root, binding, op, 60)
    last = segments(root, sid)[-1]
    assert last >= 1
    store = kind(root, engine="test-api")
    row = store.lookup(sid)
    assert row.first_user_message == "First actual prompt"  # from segment 0
    assert row.last_mtime == seg(root, sid, last).stat().st_mtime  # the active segment's
    before = native_segments.active_stamp(root, sid)
    size = native_segments.total_size(root, sid)
    journal.append_events(root, binding, [event(op)])
    assert native_segments.active_stamp(root, sid) != before
    assert native_segments.active_stamp(root, sid)[0] == segments(root, sid)[-1]
    assert native_segments.total_size(root, sid) > size
    assert native_segments.total_size(root, sid) == sum(
        seg(root, sid, i).stat().st_size for i in segments(root, sid)
    )
    assert [r.uuid for r in store.scan()] == [sid]


def test_transcript_consumers_read_the_whole_chain(conversation, small):
    """Recap, Hand off, AI review and exports (`transcript._native_turns`) see every turn."""
    from agent_sessions import transcript

    root, sid, binding = conversation
    texts = []
    for i in range(3):
        claim = turn(root, binding, text=f"Message {i}")
        op = claim["params"]["operation_id"]
        journal.record_handoff(root, binding, op, "sent")
        fill(root, binding, op, 45, text=f"reply {i}")
        journal.append_events(root, binding, [completed(op)])
        texts.append(f"Message {i}")
    assert len(segments(root, sid)) >= 3
    turns = transcript._native_turns(root, sid)
    assert [t.text for t in turns if t.role == "user"] == texts
    assert all(f"reply {i}" in turns[2 * i + 1].text for i in range(3))


# --- Hermes on #1771: freeze before publish, sidecar completeness -------------------------------


def test_a_failed_directory_fsync_after_publishing_leaves_the_predecessor_frozen(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    monkeypatch.setattr(journal, "SOFT_RECORDS", 6)
    fill(root, binding, op, 3)
    before = seg(root, sid, 0).read_bytes()
    with monkeypatch.context() as fault:
        fault.setattr(
            native_segments,
            "sync_directory",
            lambda root: (_ for _ in ()).throw(OSError(errno.EIO, "fsync failed")),
        )
        with pytest.raises(journal.JournalError) as refused:
            journal.append_events(root, binding, [event(op)] * 3)
    assert (refused.value.code, refused.value.cause) == ("unwritable", "io_error")
    assert segments(root, sid) == [0, 1]  # published, nothing appended to it
    assert seg(root, sid, 0).stat().st_mode & 0o777 == 0o400  # frozen BEFORE it was published
    # A worker of the previous release appends through segment 0's own lock: it cannot.
    with pytest.raises(PermissionError):
        chat_store.transaction(root, sid, lambda raw: ([{"type": "x"}], None), max_bytes=1 << 26)
    assert seg(root, sid, 0).read_bytes() == before
    journal.append_events(root, binding, [event(op)])  # the chain is intact and goes on
    assert journal.read(root, sid).revision == before.count(b"\n") + 1


def test_a_rollover_that_is_not_published_thaws_its_segment_again(conversation, monkeypatch):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    monkeypatch.setattr(journal, "SOFT_RECORDS", 6)
    fill(root, binding, op, 3)
    original = os.rename

    def refuse(src, dst, *args, **kwargs):
        if str(dst).endswith(".1.jsonl"):
            raise OSError(errno.EIO, "rename failed")
        return original(src, dst, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "rename", refuse)
        with pytest.raises(journal.JournalError):
            journal.append_events(root, binding, [event(op)] * 3)
    assert segments(root, sid) == [0]
    assert seg(root, sid, 0).stat().st_mode & 0o777 == 0o600  # still the active segment


def test_a_crash_between_freezing_and_publishing_is_thawed_by_the_next_writer(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    # The state such a crash leaves: an orphan sidecar of the ACTIVE segment, frozen, no successor.
    native_segments.sidecar_path(root, sid, 0).write_text("{}\n")
    native_segments.sidecar_path(root, sid, 0).chmod(0o600)
    seg(root, sid, 0).chmod(0o400)
    journal.append_events(root, binding, [event(op)])
    assert seg(root, sid, 0).stat().st_mode & 0o777 == 0o600
    assert segments(root, sid) == [0]
    # Without that proof a read-only active segment stays read-only: writers fail.
    native_segments.sidecar_path(root, sid, 0).unlink()
    seg(root, sid, 0).chmod(0o400)
    with pytest.raises(journal.JournalError):
        journal.append_events(root, binding, [event(op)])
    seg(root, sid, 0).chmod(0o600)


@pytest.mark.parametrize("damage", ["omitted-entry", "earlier-last-record"])
def test_a_sidecar_that_omits_or_understates_an_operation_is_never_trusted(
    conversation, small, damage
):
    """Hermes on #1771: a sidecar with a recomputed checksum but a missing entry let a used
    operation id be claimed again for different work. Completeness is checked against the
    pinned segment."""
    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    fill(root, binding, op, 60)
    sidecar = native_segments.sidecar_path(root, sid, 0)
    doc = json.loads(sidecar.read_bytes())
    if damage == "omitted-entry":
        del doc["entries"][op]
    else:  # point it at the claim, as if it had never been sent
        entry = doc["entries"][op]
        entry["last"] = entry["claim"]
        entry["receipt"] = {**entry["receipt"], "recorded_revision": entry["claim"][0]}
        entry["receipt"]["handoff"] = "uncertain"
    doc["checksum"] = journal._checksum(doc["entries"])
    sidecar.write_text(json.dumps(doc) + "\n")
    journal._SIDECARS.clear()
    journal._INDEX.clear()
    with pytest.raises(journal.JournalError, match="already binds"):
        journal.claim(root, dict(claim, params={**claim["params"], "text": "other work"}))
    fresh, receipt = journal.claim(root, claim)
    assert fresh is False and receipt["handoff"] == "sent"
    journal._CACHE.clear()
    journal._CHAIN.clear()
    assert len(journal.read(root, sid).operations) == 1


# --- Hermes on #1771, round 2: durable freeze, crash recovery, long chains -----------------------


def test_the_freeze_is_durable_before_the_successor_is_published(conversation, monkeypatch):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    monkeypatch.setattr(journal, "SOFT_RECORDS", 6)
    fill(root, binding, op, 3)
    inode = seg(root, sid, 0).stat().st_ino
    steps = []
    fchmod, fsync, rename = os.fchmod, os.fsync, os.rename

    def chmod_seen(fd, mode):
        if os.fstat(fd).st_ino == inode:
            steps.append(("freeze", mode))
        return fchmod(fd, mode)

    def fsync_seen(fd):
        if os.fstat(fd).st_ino == inode and steps and steps[-1][0] == "freeze":
            steps.append(("fsync-frozen",))
        return fsync(fd)

    def rename_seen(src, dst, *args, **kwargs):
        steps.append(("rename", Path(dst).name.split(".", 1)[1]))
        return rename(src, dst, *args, **kwargs)

    with monkeypatch.context() as watch:
        watch.setattr(os, "fchmod", chmod_seen)
        watch.setattr(os, "fsync", fsync_seen)
        watch.setattr(os, "rename", rename_seen)
        journal.append_events(root, binding, [event(op)] * 3)
    assert steps == [
        ("rename", "0.ops"),
        ("freeze", 0o400),
        ("fsync-frozen",),
        ("rename", "1.jsonl"),
    ]


def test_a_crash_that_kept_the_successor_but_lost_the_freeze_is_refrozen(conversation, small):
    """The state a crash may leave if the freeze did not reach the disk: a published successor
    behind a writable predecessor. The next writer freezes it again before anything appends."""
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    fill(root, binding, op, 60)
    assert len(segments(root, sid)) >= 2
    seg(root, sid, 0).chmod(0o600)  # the lost freeze; its bytes still match the pin
    journal.append_events(root, binding, [event(op)])
    assert seg(root, sid, 0).stat().st_mode & 0o777 == 0o400
    with pytest.raises(PermissionError):
        chat_store.transaction(root, sid, lambda raw: ([{"type": "x"}], None), max_bytes=1 << 26)


def test_a_new_operation_id_never_rereads_a_chain_longer_than_the_sidecar_cache(
    conversation, monkeypatch
):
    """Hermes on #1771: every new id asked every predecessor; past the 64-sidecar cache each
    asked revalidated its whole segment. The op-id index answers a new id with no segment read."""
    root, sid, binding = conversation
    monkeypatch.setattr(journal, "SOFT_RECORDS", 4)
    claims = []
    for _ in range(70):
        claim = turn(root, binding)
        op = claim["params"]["operation_id"]
        journal.record_handoff(root, binding, op, "sent")
        journal.append_events(root, binding, [completed(op)])
        claims.append(claim)
    assert len(segments(root, sid)) > journal._SIDECARS_MAX
    monkeypatch.setattr(journal, "SOFT_RECORDS", 10_000)
    journal._SIDECARS.clear()
    journal._INDEX.clear()
    reads = []
    recorded = journal._recorded_operations
    monkeypatch.setattr(
        journal,
        "_recorded_operations",
        lambda segment: reads.append(segment.index) or recorded(segment),
    )
    turn(root, binding)  # builds the index once: one validation per frozen segment
    first = len(reads)
    assert first <= len(segments(root, sid))
    reads.clear()
    for _ in range(2):
        turn(root, binding)  # each new id: no segment read at all
    assert reads == []
    fresh, receipt = journal.claim(root, claims[3])  # an old id still replays (one segment)
    assert fresh is False and receipt["handoff"] == "sent"
    assert len(reads) <= 1


# --- pre-merge review of #1771: a REAL full disk, O(1) discovery, no refold under the lock ------


def _caches_cold():
    for name in ("_CACHE", "_CHAIN", "_FROZEN", "_FIRST", "_SIDECARS", "_INDEX"):
        getattr(journal, name, {}).clear()
    native_segments._IDENTITY.clear()
    native_segments._HEADERS.clear()


def _count_listings(monkeypatch):
    listings = []
    scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda *a: listings.append(a) or scandir(*a))
    return listings


def _full_disk(monkeypatch, root):
    """EVERY write under the store fails ENOSPC: creating a file (O_CREAT, O_TMPFILE) and writing
    to any descriptor of one. Fault injection only — never fill a real filesystem."""
    store = str(root)
    real_open, real_write, real_pwrite = os.open, os.write, os.pwrite

    def under(fd):
        try:
            return os.readlink(f"/proc/self/fd/{fd}").startswith(store)
        except OSError:
            return False

    def full():
        return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def opener(path, flags, *args, **kwargs):
        creates = flags & os.O_CREAT or flags & getattr(os, "O_TMPFILE", 0) == getattr(
            os, "O_TMPFILE", -1
        )
        if creates and str(path).startswith(store) and not os.path.exists(path):
            raise full()
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", -1) and str(
            path
        ).startswith(store):
            raise full()
        return real_open(path, flags, *args, **kwargs)

    def writer(fd, data):
        if under(fd):
            raise full()
        return real_write(fd, data)

    def pwriter(fd, data, offset):
        if under(fd):
            raise full()
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "open", opener)
    monkeypatch.setattr(os, "write", writer)
    monkeypatch.setattr(os, "pwrite", pwriter)


def test_on_a_full_disk_a_claim_is_a_known_refusal_and_the_session_stays_readable(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]  # the lock file exists
    fill(root, binding, op, 30)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    _caches_cold()  # every fold must spool again
    with monkeypatch.context() as disk:
        _full_disk(disk, root)
        # The worker's claim: nothing appended, so a KNOWN refusal, never "unavailable".
        with pytest.raises(journal.JournalError) as refused:
            journal.claim(root, request(binding, revision=32))
        assert (refused.value.code, refused.value.cause) == ("unwritable", "disk_full")
        # And the session is still readable (its snapshot shows what happened).
        _caches_cold()
        assert journal.read(root, sid).revision == 32
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_no_store_listing_for_a_session_that_never_rolled_over(conversation, small, monkeypatch):
    root, sid, binding = conversation
    listings = _count_listings(monkeypatch)
    op = turn(root, binding)["params"]["operation_id"]
    journal.read(root, sid)
    native_segments.active_stamp(root, sid)
    native_segments.total_size(root, sid)
    assert listings == []  # one stat, never a listing of the whole store
    fill(root, binding, op, 60)  # a healthy chain: still none
    journal.read(root, sid)
    native_segments.active_stamp(root, sid)
    assert listings == []


def test_the_session_walk_stays_linear(tmp_path, monkeypatch):
    """Review of #1771: listing the store per session made the walk O(N²) (2000 sessions: 8-12 s
    vs 0.35 s). A chainless walk lists nothing per session."""
    from test_native_discovery_rows import conversation as make
    from test_native_discovery_rows import kind

    root = tmp_path / "conversations"
    for _ in range(300):
        make(root)
    listings = _count_listings(monkeypatch)
    started = time.monotonic()
    rows = kind(root).scan()
    elapsed = time.monotonic() - started
    assert len(rows) == 300 and listings == []
    assert elapsed / 300 < 0.05  # per session, generous: listing would grow with the store


def test_a_rollover_extends_the_cached_fold_instead_of_refolding(conversation, small, monkeypatch):
    root, sid, binding = conversation
    op = turn(root, binding)["params"]["operation_id"]
    fill(root, binding, op, 30)
    journal.read(root, sid)  # this process has a fold of the chain
    read = []
    real = native_segments.read_file
    monkeypatch.setattr(
        native_segments,
        "read_file",
        lambda path, *a: read.append(Path(path).name) or real(path, *a),
    )
    for _ in range(3):
        before = segments(root, sid)
        fill(root, binding, op, 30)  # rolls over at least once
        assert len(segments(root, sid)) > len(before)
        read.clear()
        result = journal.read(root, sid)
        # Only the segment the last read ended in (now frozen) and newer ones are read: the
        # history before it is never refolded.
        older = {seg(root, sid, i).name for i in before[:-1]}
        assert not older & set(read), (read, before)
    _caches_cold()
    cold = journal.read(root, sid)  # and the result is exactly a cold fold's
    assert cold.revision == result.revision and cold.events == result.events


def test_frozen_segments_are_never_read_or_hashed_under_the_journal_lock(
    conversation, small, monkeypatch
):
    """The lock-hold guard (review of #1771): a cold read and a cold claim of a long chain do the
    O(history) work — hashing pins, folding frozen segments, validating sidecars — BEFORE taking
    the journal lock, so an append never waits behind it."""
    import threading

    root, sid, binding = conversation
    claim = turn(root, binding)
    op = claim["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    journal.append_events(root, binding, [completed(op)])
    fill(root, binding, op, 200)
    assert len(segments(root, sid)) >= 4
    held = threading.local()
    under_lock = []
    locked = native_segments.locked

    @__import__("contextlib").contextmanager
    def watched(*args, **kwargs):
        with locked(*args, **kwargs) as view:
            held.on = True
            try:
                yield view
            finally:
                held.on = False

    def spy(name, fn):
        def wrapper(*args, **kwargs):
            if getattr(held, "on", False):
                under_lock.append(name)
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(native_segments, "locked", watched)
    monkeypatch.setattr(native_segments, "read_file", spy("read_file", native_segments.read_file))
    if hasattr(native_segments, "hash_file"):
        monkeypatch.setattr(
            native_segments, "hash_file", spy("hash_file", native_segments.hash_file)
        )
    _caches_cold()
    assert journal.read(root, sid).revision > 200
    _caches_cold()
    assert journal.claim(root, claim)[0] is False  # replay through the cold op-id index
    _caches_cold()
    journal.claim(root, request(binding, revision=journal.read(root, sid).revision))
    assert under_lock == [], under_lock


def test_a_discovery_row_reads_segment_zero_only(conversation, small, monkeypatch):
    from test_native_discovery_rows import kind

    root, sid, binding = conversation
    op = turn(root, binding, text="First actual prompt")["params"]["operation_id"]
    fill(root, binding, op, 150)
    assert len(segments(root, sid)) >= 3
    _caches_cold()
    reads = []
    real = native_segments.read_file
    monkeypatch.setattr(
        native_segments,
        "read_file",
        lambda path, *a: reads.append(Path(path).name) or real(path, *a),
    )
    row = kind(root, engine="test-api").lookup(sid)
    assert row.first_user_message == "First actual prompt"
    assert reads == []  # no history folded
    assert not getattr(journal, "_FROZEN", {}) and not journal._CHAIN


def test_discovery_finds_a_first_message_sent_after_a_pre_send_rollover(conversation, monkeypatch):
    """Hermes on #1771: the record rolled over before the first operator message; that message
    lands in a later segment and must still be the row's first message, never a cached empty."""
    from test_native_discovery_rows import kind

    root, sid, binding = conversation
    monkeypatch.setattr(journal, "SOFT_RECORDS", 4)
    pressure = {"kind": "memory_pressure", "data": {"level": "normal"}}
    for _ in range(6):
        journal.append_events(root, binding, [pressure] * 2)  # rolls over, no message yet
    assert len(segments(root, sid)) >= 2
    store = kind(root, engine="test-api")
    assert store.lookup(sid).first_user_message == ""
    turn(root, binding, text="first-message-after-rollover")
    assert lines(seg(root, sid, 0))[1:] and all(
        r["type"] == "native_event" for r in lines(seg(root, sid, 0))[1:]
    )
    assert store.lookup(sid).first_user_message == "first-message-after-rollover"
    turn(root, binding, text="a later message")
    _caches_cold()
    assert store.lookup(sid).first_user_message == "first-message-after-rollover"
