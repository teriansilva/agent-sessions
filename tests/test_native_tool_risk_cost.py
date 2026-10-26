"""A long turn's snapshot classifies only the calls it can show.

A 19,000-call codex turn (codex-api 9471d32e) took ~20 s per snapshot read: every call's command
was re-tokenized for its advisory risk mark on every read, and every call event scanned every
earlier call. Only the newest `TOOLS_SHOWN` calls of a turn are ever sent, so only they are
classified, once per read, with the same result as classifying every event on arrival.
"""

import json

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_tool_order import done, submit, tool

conversation = journal_tests.conversation
SHOWN = native_runtime.TOOLS_SHOWN


def project(root, sid, binding):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )


def risky(op, item_id):
    data = {"operation_id": op, "native_turn_id": "n1", "item_id": item_id, "state": "running"}
    data.update(tool="Bash", summary=json.dumps({"command": "git push --force origin main"}))
    return {"kind": "tool", "data": data}


def test_only_the_calls_a_snapshot_shows_are_classified(conversation, monkeypatch):
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = []
    for i in range(SHOWN * 3):
        events += [tool(op, f"t{i}"), tool(op, f"t{i}", "completed")]
    for i in range(0, len(events), 50):
        journal.append_events(root, binding, events[i : i + 50])
    asked = []
    real = native_runtime._tool_risk
    monkeypatch.setattr(native_runtime, "_tool_risk", lambda *a: asked.append(a) or real(*a))
    (turn,) = project(root, sid, binding)["turns"]
    assert len(turn["tools"]) == SHOWN * 3
    assert len(asked) == SHOWN  # one per shown call, none for the older two thirds
    shown = structured_runtime._turn(turn)["tools"]
    assert [t["id"] for t in shown] == [f"t{i}" for i in range(SHOWN * 2, SHOWN * 3)]
    assert all("risk" in t for t in shown)
    assert not any("_risk_sources" in t for t in turn["tools"]) and "_tool_index" not in turn


def test_a_shown_calls_risk_is_what_classifying_every_event_gave(conversation):
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    journal.append_events(
        root,
        binding,
        [
            risky(op, "push"),
            # Its result names only the call (no tool, no summary): the mark stays.
            tool(op, "push", "completed"),
            tool(op, "echo"),
            done(op),
        ],
    )
    (turn,) = project(root, sid, binding)["turns"]
    marks = {t["id"]: t.get("risk", {}).get("level") for t in turn["tools"]}
    assert marks["push"] == "risky" and marks["echo"] != "risky"


def test_a_late_result_for_a_call_out_of_view_still_finds_it(conversation, monkeypatch):
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    events = [tool(op, f"t{i}") for i in range(SHOWN + 5)]
    events.append(tool(op, "t0", "completed"))  # the oldest call's result, long after
    for i in range(0, len(events), 50):
        journal.append_events(root, binding, events[i : i + 50])
    asked = []
    real = native_runtime._tool_risk
    monkeypatch.setattr(native_runtime, "_tool_risk", lambda *a: asked.append(a[1]) or real(*a))
    (turn,) = project(root, sid, binding)["turns"]
    first = turn["tools"][0]
    assert first["id"] == "t0" and first["outcome"] == "completed"  # the index found it
    assert len(turn["tools"]) == SHOWN + 5  # no duplicate row
    assert len(asked) == SHOWN and not any("echo t0" in summary for summary in asked)


def test_a_call_reported_later_under_another_kind_keeps_its_risk_mark(conversation):
    # Hermes on #1711: an ACP call starts as `execute` with a risky command, then a later event
    # for the same item reports another kind. Each event is classified under the name it had.
    root, sid, binding = conversation
    op = submit(root, sid, binding)
    first = {
        "operation_id": op,
        "native_turn_id": "n1",
        "item_id": "x",
        "state": "running",
        "tool": "execute",
        "summary": json.dumps({"command": "git push --force origin main"}),
    }
    later = {
        **first,
        "tool": "read",
        "summary": json.dumps({"path": "README.md"}),
        "state": "completed",
    }
    journal.append_events(
        root, binding, [{"kind": "tool", "data": first}, {"kind": "tool", "data": later}]
    )
    (turn,) = project(root, sid, binding)["turns"]
    (row,) = turn["tools"]
    assert row["name"] == "read" and row["risk"]["level"] == "risky"
