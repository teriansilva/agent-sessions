"""Plain background heartbeats are folded at append, never re-read by a snapshot (#1805).

An idle agent's running subagent journals a `background` progress or activity report every few
seconds; a long session held 20k of them, and every snapshot decoded all of them twice. The
projection now takes the live worker's background flag and tasks from a fold kept on the event
index. Its output is checked revision by revision against the projector as it was before
(`fixtures/native_project_before_1805.py.txt`), run over the same journal with the heartbeats
put back into the session-wide index.
"""

import dataclasses
import uuid
from array import array
from pathlib import Path

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_events, native_runtime
from agent_sessions import native_journal as journal

conversation = journal_tests.conversation
T0 = 1_791_790_000.0
RECORD = {"request": {"model": None}, "adapter": "codex-app-server"}
OWN_1, OWN_2 = str(uuid.uuid4()), str(uuid.uuid4())
ORACLE = Path(__file__).parent / "fixtures" / "native_project_before_1805.py.txt"


def _oracle():
    namespace = dict(vars(native_runtime))
    exec(compile(ORACLE.read_text(), str(ORACLE), "exec"), namespace)  # noqa: S102
    return namespace["project"]


before_1805 = _oracle()


def legacy(current: journal.Journal) -> journal.Journal:
    """The same journal with every plain heartbeat back in the session-wide index, as it was
    indexed before #1805: what the oracle reads."""
    old = current.copy()
    beats = {i for i in range(len(old.events)) if native_events.heartbeat(old.events[i]["event"])}
    old.events._global = array("Q", sorted({*old.events._global, *beats}))
    return old


def beat(state="activity", **data):
    return {"kind": "background", "data": {"state": state, "active": True, **data}}


def task(task_id, state="running", **data):
    return beat(state, task_id=task_id, task_state=state, **data)


def agent(target, state="running", **data):
    return {
        "kind": "background",
        "data": {"state": state, "active": target is not None, "background_turn": target, **data},
    }


def completed(operation_id, background_active):
    return {
        "kind": "turn_completed",
        "data": {
            "operation_id": operation_id,
            "native_turn_id": "native-turn-1",
            "state": "completed",
            "error": "",
            "background_active": background_active,
        },
    }


def claim(root, binding):
    sid = binding.session_key.partition(":")[2]
    request = journal_tests.request(binding, revision=journal.read(root, sid).revision)
    assert journal.claim(root, request)[0] is True
    return request["params"]["operation_id"]


def scenario(root, binding, other, monkeypatch, check):
    """Append the matrix of #1805 (issue + Hermes' second review) one record at a time, calling
    ``check`` after each: heartbeats on both sides of a `turn_completed`, an own turn with and
    without a task id, a terminal task then late progress, session/model, memory and OOM
    notices, an approval and its cancellation, then a replacement worker."""
    clock = iter(T0 + n for n in range(10_000))
    monkeypatch.setattr(journal.time, "time", lambda: next(clock))

    def add(who, *events):
        for event in events:
            journal.append_events(root, who, [event])
            check()

    add(binding, beat(), beat(active=False), beat("running"))
    add(binding, task("t1", description="Monitor build"), task("t1", "", last_tool_name="Bash"))
    op = claim(root, binding)
    check()
    add(binding, journal_tests.event(op), beat())
    approval = {
        "kind": "approval",
        "data": {
            "operation_id": op,
            "native_turn_id": "native-turn-1",
            "request_id": "s:approval-1",
            "item_id": "item-2",
            "tool": "Bash",
            "summary": "Run ls",
            "choices": ["approve", "reject"],
            "payload_digest": "a" * 64,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "complete": True,
        },
    }
    add(binding, approval, {"kind": "approval_cancelled", "data": {"request_id": "s:approval-1"}})
    # A heartbeat before and after the completion that also reports the flag.
    add(binding, beat(active=True), completed(op, False), beat(active=True), beat(active=False))
    # An own turn, with a task on its start record, then plain progress and its end.
    add(binding, agent(OWN_1, task_id="t2", task_state="running"), beat(), task("t2", ""))
    add(
        binding,
        {
            "kind": "text",
            "data": {
                "agent_turn": OWN_1,
                "text": "Checked.",
                "partial": False,
                "truncated": False,
            },
        },
        agent(None, "completed"),
        beat(active=False),
    )
    # A terminal task, then late progress that must not revive it.
    add(binding, task("t1", "completed"), task("t1", "running", summary="late"))
    add(
        binding,
        {
            "kind": "session",
            "data": {"native_id": "n-1", "model_configured": "m-2", "model_effective": None},
        },
        {
            "kind": "model",
            "data": {"operation_id": op, "native_turn_id": "x", "model_effective": "m-3"},
        },
        {"kind": "memory_pressure", "data": {"level": "high"}},
        {"kind": "memory_oom_kill", "data": {"count": 1, "invocation_id": "0" * 32}},
        beat(),
    )
    # The replacement generation: its own heartbeats and tasks; the old one's last reports.
    add(other, beat(active=False), task("t3"), beat())
    add(binding, beat(active=True), task("t4"))
    add(other, agent(OWN_2), beat(active=False))


def views(current, live_workers):
    for live in live_workers:
        for max_turns in (None, 0, 1, 2, 20):
            yield live, max_turns, current


@pytest.mark.parametrize("roll", [False, True], ids=["one-segment", "rolled-over"])
def test_the_projection_is_unchanged_at_every_revision(conversation, monkeypatch, roll):
    root, sid, binding = conversation
    other = dataclasses.replace(binding, worker_id=str(uuid.uuid4()))
    if roll:
        # Segments of a few dozen records: the scenario crosses several rollovers (#1733), and a
        # successor's carry rebuilds the fold from what it actually holds.
        monkeypatch.setattr(journal, "SOFT_RECORDS", 12)
        monkeypatch.setattr(journal, "SOFT_BYTES", 256 * 1024)
    checked = []

    def check():
        current = journal.read(root, sid)
        old = legacy(current)
        for live, max_turns, j in views(current, (binding.worker_id, other.worker_id, None)):
            new_view = native_runtime.project(j, RECORD, live, max_turns=max_turns)
            old_view = before_1805(old, RECORD, live, max_turns=max_turns)
            assert new_view == old_view, (current.revision, live, max_turns)
        checked.append(current.revision)

    scenario(root, binding, other, monkeypatch, check)
    assert len(checked) > 30
    # Not vacuous: the scenario reaches a non-trivial flag and task list both ways.
    current = journal.read(root, sid)
    view = native_runtime.project(current, RECORD, binding.worker_id, max_turns=2)
    assert view["native"]["background_active"] is True
    assert [t["task_id"] for t in view["native"]["background_tasks"]] == ["t2", "t4"]
    view = native_runtime.project(current, RECORD, other.worker_id, max_turns=2)
    assert view["native"]["background_turn"] == OWN_2
    assert [t["task_id"] for t in view["native"]["background_tasks"]] == ["t3"]
    if roll:
        assert journal.read(root, sid).base > 0  # it did roll over


def test_a_warm_snapshot_decodes_no_plain_heartbeat(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(journal.time, "time", lambda: T0)
    for n in range(300):
        journal.append_events(root, binding, [beat(active=n % 2 == 0)])
        if n % 50 == 0:
            journal.append_events(root, binding, [task(f"t{n}")])
    op = claim(root, binding)
    journal.append_events(root, binding, [journal_tests.event(op), completed(op, False)])
    journal.append_events(root, binding, [beat(active=True)])
    current = journal.read(root, sid)
    decoded = []
    getitem = native_events.Events.__getitem__

    def counting(self, index):
        item = getitem(self, index)
        if not isinstance(index, slice):
            decoded.append(item["event"])
        return item

    monkeypatch.setattr(native_events.Events, "__getitem__", counting)
    view = native_runtime.project(current, RECORD, binding.worker_id, max_turns=20)
    assert not [event for event in decoded if native_events.heartbeat(event)]
    assert len(decoded) < 10  # the turn's own records and the session-wide ones, nothing else
    assert view["native"]["background_active"] is True  # the newest heartbeat still decides
    assert len(view["native"]["background_tasks"]) == 6


def test_a_copy_folds_on_its_own_and_a_cache_miss_rebuilds_the_fold(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(journal.time, "time", lambda: T0)
    journal.append_events(root, binding, [task("t1"), beat(active=True)])
    cached = journal.read(root, sid)
    copy = cached.events.copy()
    copy.append(
        {
            "cursor": cached.revision + 1,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": T0 + 1,
            "event": task("t1", "completed"),
        }
    )
    copy.append(
        {
            "cursor": cached.revision + 2,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": T0 + 2,
            "event": beat(active=False),
        }
    )
    assert copy.tasks_of(binding.worker_id) == []
    assert copy.heartbeat_of(binding.worker_id)[1] is False
    # The cached fold (and every later read of it) is untouched by the copy's records.
    again = journal.read(root, sid)
    assert [t["task_id"] for t in again.events.tasks_of(binding.worker_id)] == ["t1"]
    assert again.events.heartbeat_of(binding.worker_id)[1] is True
    # Handing out a task never lets a caller edit the fold.
    again.events.tasks_of(binding.worker_id)[0]["state"] = "edited"
    assert again.events.tasks_of(binding.worker_id)[0]["state"] == "running"
    view = native_runtime.project(journal.read(root, sid), RECORD, binding.worker_id)
    journal._CACHE.clear()
    assert native_runtime.project(journal.read(root, sid), RECORD, binding.worker_id) == view


def test_the_fold_is_counted_in_the_cache_weight(conversation, monkeypatch):
    root, sid, binding = conversation
    monkeypatch.setattr(journal.time, "time", lambda: T0)
    empty = journal.read(root, sid).events.memory_bytes
    journal.append_events(root, binding, [task(f"t{n}", summary="x" * 200) for n in range(20)])
    assert journal.read(root, sid).events.memory_bytes > empty + 20 * 200
