"""The streaming-reply fixture (#1727) is real server output, not hand-written JSON.

`web/src/components/structured/streamDeltas.fixture.json` feeds `streamDeltas.test.ts`: for each
case it holds a snapshot taken mid-stream, the operator's events page after that snapshot's
cursor, and the snapshot taken after those events. The pane's `applyTextDeltas` must turn the
first two into the third's reply exactly (text, message breaks, the item the stream continues) —
or refuse the page, where the projection would show something deltas cannot build. This test
rebuilds the file from journals through `native_runtime.project` and `structured_runtime._snapshot`
and fails if the committed file differs, so the browser code can never pass against a shape the
server does not send. Regenerate deliberately with `AGENT_SESSIONS_WRITE_FIXTURES=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from agent_sessions import chat_store, native_ipc, native_runtime, structured_runtime
from agent_sessions import native_journal as journal

FIXTURE = (
    Path(__file__).resolve().parents[1] / "web/src/components/structured/streamDeltas.fixture.json"
)
TURN = "11111111-2222-4333-8444-555555555555"
RETRY = "66666666-2222-4333-8444-555555555555"
QUEUED = "77777777-2222-4333-8444-555555555555"


def _conversation(tmp: Path):
    session_id = str(uuid.uuid4())
    binding = native_ipc.Binding(
        session_key=f"test-api:{session_id}",
        worker_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        adapter="codex-app-server",
    )
    chat_store.create(
        tmp / "chat",
        session_id,
        cwd=str(tmp),
        request={"runtime": "api", "session_key": binding.session_key},
        model=None,
    )
    return tmp / "chat", session_id, binding


def _claim(root, sid, binding, operation_id):
    revision = journal.read(root, sid).revision
    journal.claim(
        root,
        {
            **binding.envelope("request"),
            "request_id": str(uuid.uuid4()),
            "action": "submit",
            "params": {
                "operation_id": operation_id,
                "expected_revision": revision,
                "text": "Explain the change",
                "context": {},
            },
        },
    )
    journal.record_handoff(root, binding, operation_id, "sent")


def delta(text, item=None, op=TURN):
    data = {"operation_id": op, "native_turn_id": "n", "text": text, "partial": True}
    named = {"item_id": item} if item else {}
    return {"kind": "text", "data": {**data, "truncated": False, **named}}


def settled(text, item="msg-1"):
    data = {"operation_id": TURN, "native_turn_id": "n", "item_id": item, "text": text}
    return {"kind": "text", "data": {**data, "partial": False, "truncated": False}}


def tool():
    data = {"operation_id": TURN, "native_turn_id": "n", "item_id": "t1", "tool": "command"}
    return {"kind": "tool", "data": {**data, "state": "inProgress", "summary": "ls"}}


def _snapshot(root, sid, binding) -> dict:
    raw = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=50,
        live_text=True,  # the operator's own snapshot, the one the pane extends (#1727)
    )
    snap = structured_runtime._snapshot(binding.session_key, raw)
    # What `applyTextDeltas` reads; the ids and times it does not read stay out of the file.
    return {
        "revision": snap["revision"],
        "event_cursor": snap["event_cursor"],
        "turns": [
            {
                key: t[key]
                for key in ("turn_id", "state", "reply", "reply_breaks", "stream")
                if key in t
            }
            for t in snap["turns"]
        ],
    }


def _page(root, sid, after) -> dict:
    page = journal.read(root, sid).page(after, 100)
    return {
        "revision": page["revision"],
        "next_cursor": page["next_cursor"],
        "events": [
            {"cursor": e["cursor"], "kind": e["kind"], "data": dict(e["data"])}
            for e in page["events"]
        ],
    }


# Each case: batches of events (one journal append each, as one worker flush), the base taken
# after `base` batches, and whether deltas can build the rest. A batch may be a callable (a
# private record: a claim) instead.
CASES = {
    # Codex / opencode / copilot: every delta names its message; a new message is a paragraph.
    "messages": {
        "batches": [
            [delta("Looking", "msg-1"), delta(" at the", "msg-1")],
            [delta(" code.", "msg-1")],
            [delta("Next", "msg-2"), delta(" step", "msg-2")],
            [delta("   ", "msg-3")],  # a blank new message adds nothing, but is the item now
            [delta("still msg-3", "msg-3")],
            [delta("\n\nfourth", "msg-4")],
        ],
        "bases": [0, 1, 2, 3, 4],
        "applies": True,
    },
    # Claude: deltas name no message; they continue one text.
    "unnamed": {
        "batches": [
            [delta("Sure")],
            [delta(", here"), delta(" it is:\n\n- one")],
            [delta("\n- two")],
        ],
        "bases": [0, 1, 2],
        "applies": True,
    },
    # A settled text block replaces the streamed text: only a snapshot shows that.
    "settled": {
        "batches": [[delta("Part", "msg-1")], [settled("Part one.")]],
        "bases": [1],
        "applies": False,
    },
    # A tool row is structural: a snapshot's.
    "tool": {
        "batches": [[delta("Running", "msg-1")], [tool(), delta(" it", "msg-1")]],
        "bases": [1],
        "applies": False,
    },
    # Another operation's text (a retry is a new operation id) never splices into this turn.
    "retry": {
        "batches": [[delta("First", "msg-1")], "claim-retry", [delta("Again", "msg-9", RETRY)]],
        "bases": [2],
        "applies": False,
    },
    # #1727 part 2 — Codex: message one settled, a command, message two streaming: the snapshot
    # shows both and the events extend the second.
    "after-settled": {
        "batches": [
            [settled("One.", "m1")],
            [tool()],
            [delta("Two", "m2")],
            [delta(" so", "m2"), delta(" far", "m2")],
        ],
        "bases": [3],
        "applies": True,
    },
    # …taken before the second message's first words: nothing streams yet, so a snapshot shows it.
    "after-settled-unopened": {
        "batches": [[settled("One.", "m1")], [tool()], [delta("Two", "m2")]],
        "bases": [2],
        "applies": False,
    },
    # A new message in the stream without a settled block between: a paragraph, as the server.
    "after-settled-switch": {
        "batches": [[settled("One.", "m1")], [delta("Two", "m2")], [delta("Three", "m3")]],
        "bases": [2],
        "applies": True,
    },
    # The streaming message settles on the page: the snapshot's, never the deltas'.
    "after-settled-settles": {
        "batches": [
            [settled("One.", "m1")],
            [delta("Two", "m2")],
            [settled("Two.", "m2"), delta("Three", "m3")],
        ],
        "bases": [2],
        "applies": False,
    },
    # Claude: deltas name no message; each settled block names the message id.
    "after-settled-unnamed": {
        "batches": [[delta("A")], [settled("A.", "msg_1")], [delta("B")], [delta(" c")]],
        "bases": [3],
        "applies": True,
    },
    # Hermes on #1760: message one is cut at its end (one message over the snapshot cap, here
    # lowered to 40 so the fixture stays small: the same cut code), and message two streams after
    # it as its own part, the cut's marker in the text. The pane extends message two at the end.
    "after-a-cut-message": {
        "batches": [
            [settled("a" * 41, "m1")],
            [tool()],
            [delta("Two", "m2")],
            [delta(" more", "m2")],
        ],
        "bases": [3],
        "applies": True,
        "cap": 40,
    },
    # A private record (a queued claim) between the snapshot and the text: a gap in the cursors.
    "gap": {
        "batches": [[delta("One", "msg-1")], "claim-queued", [delta(" two", "msg-1")]],
        "bases": [1],
        "applies": False,
    },
}


def build(tmp_path_factory) -> dict:
    out = {}
    for name, case in CASES.items():
        cap, structured_runtime.SNAPSHOT_TEXT = (
            structured_runtime.SNAPSHOT_TEXT,
            case.get("cap", structured_runtime.SNAPSHOT_TEXT),
        )
        try:
            out.update(_build_case(tmp_path_factory, name, case))
        finally:
            structured_runtime.SNAPSHOT_TEXT = cap
    return out


def _build_case(tmp_path_factory, name, case) -> dict:
    out = {}
    for base in case["bases"]:
        root, sid, binding = _conversation(tmp_path_factory.mktemp(f"{name}-{base}"))
        _claim(root, sid, binding, TURN)
        snaps = []
        for i, batch in enumerate([*case["batches"], None]):
            if i == base:
                snaps.append(_snapshot(root, sid, binding))
            if batch is None:
                break
            if batch == "claim-retry":
                _claim(root, sid, binding, RETRY)
            elif batch == "claim-queued":
                journal.claim(
                    root,
                    {
                        **binding.envelope("request"),
                        "request_id": str(uuid.uuid4()),
                        "action": "submit",
                        "params": {
                            "operation_id": QUEUED,
                            "expected_revision": journal.read(root, sid).revision,
                            "text": "later",
                            "context": {},
                        },
                    },
                    queued=True,
                )
            else:
                journal.append_events(root, binding, batch)
        first = snaps[0]
        out[f"{name}@{base}"] = {
            "applies": case["applies"],
            "base": first,
            "page": _page(root, sid, first["event_cursor"]),
            "next": _snapshot(root, sid, binding),
        }
    return out


def test_the_stream_fixture_is_what_the_server_sends(tmp_path_factory):
    built = build(tmp_path_factory)
    for name, case in built.items():
        shown = {t["turn_id"]: t for t in case["next"]["turns"]}
        if case["applies"]:
            # The running turn is marked streaming before AND after: deltas may extend it.
            assert next(t for t in case["base"]["turns"] if t["turn_id"] == TURN)["stream"]
            assert shown[TURN]["stream"], name
    text = json.dumps(built, indent=1, sort_keys=True) + "\n"
    if os.environ.get("AGENT_SESSIONS_WRITE_FIXTURES") == "1":
        FIXTURE.write_text(text)
    assert FIXTURE.read_text() == text, "regenerate with AGENT_SESSIONS_WRITE_FIXTURES=1"


def test_a_settled_or_ended_turn_is_never_marked_streaming(tmp_path):
    root, sid, binding = _conversation(tmp_path)
    _claim(root, sid, binding, TURN)
    journal.append_events(root, binding, [delta("Part", "msg-1"), settled("Part one.")])
    (turn,) = _snapshot(root, sid, binding)["turns"]
    assert turn["state"] == "running" and "stream" not in turn  # shown from settled text
    done = {"operation_id": TURN, "native_turn_id": "n", "state": "completed", "error": ""}
    journal.append_events(root, binding, [{"kind": "turn_completed", "data": done}])
    (turn,) = _snapshot(root, sid, binding)["turns"]
    assert turn["state"] == "completed" and "stream" not in turn
