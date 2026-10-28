"""Every exactly-once fence holds across a journal rollover, for every API client (#1733).

Parametrized over `kinds.API_KINDS`, so a new client cannot ship without them. Each client gets
its own fixture journal in the shape it writes: Codex's turn/item events, Claude's
`background` reports (with the agent's own turn as a Send now target), and the ACP clients'
permission requests. A rollover is forced in the middle of each scenario; then the fence must
behave exactly as it does without one — and the successor must have carried nothing but live
control state.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agent_sessions import chat_store, native_ipc, native_runtime, native_segments
from agent_sessions import native_journal as journal
from agent_sessions.plugins import kinds
from test_native_journal import request

ENGINES = {
    "codex-app-server": "codex-api",
    "claude-stream-json": "claude-api",
    "opencode-acp": "opencode-api",
    "copilot-acp": "copilot-api",
}


def test_every_api_client_has_a_fixture_journal():
    assert set(ENGINES) == set(kinds.API_KINDS)


@pytest.fixture(params=sorted(kinds.API_KINDS))
def client(request, tmp_path, monkeypatch):
    kind = request.param
    session_id = str(uuid.uuid4())
    root = tmp_path / "chat"
    binding = native_ipc.Binding(
        session_key=f"{ENGINES[kind]}:{session_id}",
        worker_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        adapter=kind,
    )
    chat_store.create(
        root,
        session_id,
        cwd=str(tmp_path),
        request={"runtime": "api", "session_key": binding.session_key},
        model=None,
    )
    monkeypatch.setattr(journal, "SOFT_RECORDS", 10_000)
    return kind, root, session_id, binding


def ev(kind, **data):
    return {"kind": kind, "data": data}


def started(op, native="native-turn-1"):
    return ev("turn_started", operation_id=op, native_turn_id=native)


def approval(binding, op, request_id, tool="execute"):
    return ev(
        "approval",
        operation_id=op,
        native_turn_id="native-turn-1",
        request_id=request_id,
        item_id=f"item-{request_id}",
        tool=tool,
        summary=json.dumps({"command": "rm -rf build"}),
        choices=["approve", "reject"],
        payload_digest="b" * 64,
        worker_id=binding.worker_id,
        connection_id=binding.connection_id,
        complete=True,
    )


def work(kind, op, n, label):
    """``n`` tool calls of one operator turn, in this client's shape."""
    out = []
    for i in range(n):
        item = f"{label}-{i}"
        if kind == "codex-app-server":
            out += [
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    tool="execute",
                    state="running",
                    summary=json.dumps({"command": f"ls {i}"}),
                ),
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    state="completed",
                    output="ok",
                    completed=True,
                ),
            ]
        elif kind == "claude-stream-json":
            out += [
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    tool="Bash",
                    state="running",
                    summary=f"ls {i}",
                ),
                ev("background", state="running", active=True, task_id="task-1"),
                ev("background", state="running", active=True, task_id="task-1"),
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    state="completed",
                    output="ok",
                    completed=True,
                ),
            ]
        else:  # ACP: tool_call / tool_call_update
            out += [
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    tool="read",
                    state="pending",
                    summary=f"read {i}",
                ),
                ev(
                    "tool",
                    operation_id=op,
                    native_turn_id="native-turn-1",
                    item_id=item,
                    state="completed",
                    completed=True,
                ),
            ]
        out.append(
            ev(
                "text",
                operation_id=op,
                native_turn_id="native-turn-1",
                item_id=f"msg-{label}",
                text=f"{label} {i}.",
                partial=False,
                truncated=False,
            )
        )
    return out


def completed(op, text=""):
    return ev(
        "turn_completed",
        operation_id=op,
        native_turn_id="native-turn-1",
        state="completed",
        error="",
        text=text,
    )


def append(root, binding, events):
    for start in range(0, len(events), native_ipc.MAX_EVENTS):
        journal.append_events(root, binding, events[start : start + native_ipc.MAX_EVENTS])


def claim(root, binding, *, queued=False, **kw):
    sid = binding.session_key.partition(":")[2]
    req = request(binding, revision=journal.read(root, sid).revision, **kw)
    if req["action"] == "decide":
        req["params"].pop("grant", None)
    fresh, receipt = journal.claim(root, req, queued=queued)
    assert fresh is True
    return req, receipt


def roll(root, sid, binding, monkeypatch):
    """Force exactly one rollover with a session-wide observation, then stop rolling."""
    before = native_segments.indexes(root, sid)
    active = native_segments.segment_path(root, sid, max([0, *before]))
    monkeypatch.setattr(journal, "SOFT_RECORDS", active.read_bytes().count(b"\n") + 1)
    journal.append_events(root, binding, [ev("memory_pressure", level="normal")] * 2)
    monkeypatch.setattr(journal, "SOFT_RECORDS", 10_000)
    after = native_segments.indexes(root, sid)
    assert after == [*before, len(before) + 1]
    return native_segments.segment_path(root, sid, after[-1])


def carried(path):
    head = json.loads(path.read_bytes().split(b"\n", 1)[0])
    return head["carried"]


def view(kind, root, sid, binding, live=True):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": kind},
        binding.worker_id if live else None,
    )


def no_content_carried(path):
    for _, record in carried(path)["records"]:
        if record["type"] == "native_event":
            assert record["event"]["kind"] not in {"tool", "text", "turn_completed"}


# --- the fences ---------------------------------------------------------------------------------


def test_replay_of_an_old_operation_id_same_and_different_request(client, monkeypatch):
    kind, root, sid, binding = client
    req, receipt = claim(root, binding)
    op = req["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    append(root, binding, [started(op), *work(kind, op, 3, "a"), completed(op)])
    successor = roll(root, sid, binding, monkeypatch)
    # Settled: nothing about it is carried (a live background task report may be).
    assert [r["event"]["kind"] for _, r in carried(successor)["records"]] in (
        [],
        ["background"],
    )
    fresh, again = journal.claim(root, req)
    assert fresh is False and again["operation_id"] == op and again["handoff"] == "sent"
    other = dict(req, params={**req["params"], "text": "a different request"})
    with pytest.raises(journal.JournalError, match="already binds"):
        journal.claim(root, other)


def test_an_in_flight_turn_continues_and_settles_in_the_successor(client, monkeypatch):
    kind, root, sid, binding = client
    req, _ = claim(root, binding)
    op = req["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    append(root, binding, [started(op), *work(kind, op, 4, "before")])
    successor = roll(root, sid, binding, monkeypatch)
    no_content_carried(successor)
    assert view(kind, root, sid, binding)["in_flight"] == op
    append(root, binding, [*work(kind, op, 4, "after"), completed(op, "all done")])
    (turn,) = view(kind, root, sid, binding)["turns"]
    assert turn["status"] == "done"
    assert "before 0." in turn["reply"] and "after 3." in turn["reply"]
    names = [t["id"] for t in turn["tools"]]
    assert len(names) == len(set(names)) == 8  # nothing carried twice


def test_a_queued_message_still_sends_exactly_once(client, monkeypatch):
    kind, root, sid, binding = client
    running, _ = claim(root, binding)
    journal.record_handoff(root, binding, running["params"]["operation_id"], "sent")
    append(root, binding, [started(running["params"]["operation_id"])])
    queued, receipt = claim(root, binding, queued=True, text="then this")
    qid = queued["params"]["operation_id"]
    assert receipt["handoff"] == "queued"
    roll(root, sid, binding, monkeypatch)
    # The worker dispatches it after the rollover: both transitions are accepted once.
    assert journal.record_handoff(root, binding, qid, "uncertain")["handoff"] == "uncertain"
    assert journal.record_handoff(root, binding, qid, "sent")["handoff"] == "sent"
    fresh, again = journal.claim(root, queued, queued=True)
    assert fresh is False and again["handoff"] == "sent"
    claims = [
        r
        for i in [0, *native_segments.indexes(root, sid)]
        for r in (
            json.loads(line)
            for line in native_segments.segment_path(root, sid, i).read_bytes().splitlines()[1:]
        )
        if r.get("operation_id") == qid and r["type"] == "native_queued"
    ]
    assert len(claims) == 1  # a carried copy is state, never a second record
    shown = [t for t in view(kind, root, sid, binding)["turns"] if t["turn_id"] == qid]
    assert len(shown) == 1


def test_a_pending_approval_is_still_answerable_with_the_same_digest(client, monkeypatch):
    kind, root, sid, binding = client
    req, _ = claim(root, binding)
    op = req["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    append(root, binding, [started(op), *work(kind, op, 2, "a"), approval(binding, op, "r-7")])
    before = view(kind, root, sid, binding)["pending_requests"]
    roll(root, sid, binding, monkeypatch)
    after = view(kind, root, sid, binding)["pending_requests"]
    assert [(r["request_id"], r["payload_digest"]) for r in after] == [("r-7", "b" * 64)]
    assert after == before
    decision, _ = claim(
        root,
        binding,
        action="decide",
        turn_id=op,
        request_id="r-7",
        item_id="item-r-7",
        payload_digest="b" * 64,
    )
    journal.record_handoff(root, binding, decision["params"]["operation_id"], "sent")
    assert view(kind, root, sid, binding)["pending_requests"] == []


def test_send_now_target_survives(client, monkeypatch):
    kind, root, sid, binding = client
    req, _ = claim(root, binding)
    op = req["params"]["operation_id"]
    journal.record_handoff(root, binding, op, "sent")
    events = [started(op, "native-turn-9"), *work(kind, op, 2, "a")]
    own = str(uuid.uuid4())
    if kind == "claude-stream-json":  # the agent's own turn is a Send now target too (#1427)
        events.append(ev("background", state="running", active=True, background_turn=own))
    append(root, binding, events)
    queued, _ = claim(root, binding, queued=True, text="now please")
    qid = queued["params"]["operation_id"]
    before = view(kind, root, sid, binding)
    successor = roll(root, sid, binding, monkeypatch)
    starts = [
        r
        for _, r in carried(successor)["records"]
        if r.get("event", {}).get("kind") == "turn_started"
    ]
    assert [r["event"]["data"]["native_turn_id"] for r in starts] == ["native-turn-9"]
    after = view(kind, root, sid, binding)
    assert after["in_flight"] == before["in_flight"] == op
    assert after["native"]["background_turn"] == before["native"]["background_turn"]
    if kind == "claude-stream-json":
        assert after["native"]["background_turn"] == own
    control, _ = claim(
        root, binding, action="send_now", turn_id=op, queued_turn_id=qid, mode="steer"
    )
    journal.record_handoff(root, binding, control["params"]["operation_id"], "sent")
    queued_turn = next(t for t in view(kind, root, sid, binding)["turns"] if t["turn_id"] == qid)
    assert queued_turn["status"] in {"queued", "delivering"}


def test_permission_change_state_survives(client, monkeypatch):
    """Enabling Skip permissions stops the generation through a `stop` control; the record's
    permission change lives outside the journal and the control's claim is carried."""
    kind, root, sid, binding = client
    record = {
        "request": {"model": None},
        "adapter": kind,
        "permission_change": {
            "operation_id": str(uuid.uuid4()),
            "state": "stopping",
            "actor": "operator",
        },
    }
    stop, _ = claim(root, binding, action="stop")
    sop = stop["params"]["operation_id"]
    before = native_runtime.project(journal.read(root, sid), record, binding.worker_id)
    successor = roll(root, sid, binding, monkeypatch)
    assert [r["operation_id"] for _, r in carried(successor)["records"]] == [sop]
    assert journal.record_handoff(root, binding, sop, "sent")["handoff"] == "sent"
    assert journal.claim(root, stop)[0] is False
    after = native_runtime.project(journal.read(root, sid), record, binding.worker_id)
    assert after["permission_change"] == before["permission_change"]
