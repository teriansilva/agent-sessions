"""The long-turn browser fixture (#1643) is real server output, not hand-written JSON.

`web/e2e/fixtures/long-turn.json` feeds the Playwright fold spec. This test rebuilds it from a
journal through `native_runtime.project` and `structured_runtime._turn`, exactly as a snapshot is
built, and fails if the committed file differs: the browser test can never pass against a shape
the server does not send. Regenerate deliberately with `AGENT_SESSIONS_WRITE_FIXTURES=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import test_native_agent_turns as agent_tests
from agent_sessions import chat_store, native_ipc, native_runtime, structured_runtime
from agent_sessions import native_journal as journal
from test_reply_fold import streamed, text, tool, turn_with

FIXTURE = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/long-turn.json"
TS = 1_792_800_000.0


def _note(i: int) -> str:
    # ~1,250 characters: 200 of them overrun the 200k kept window, so the long turn is cut twice.
    return f"Update {i:03}: " + " ".join(["progress"] * 137) + "."


def _shown(root, sid, binding, turn_id, budget=None):
    view = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )
    (raw,) = [t for t in view["turns"] if t["turn_id"] == turn_id]
    shown = structured_runtime._turn(raw, budget)
    # Stable across runs: the ids and times the fixture's spec does not read.
    for key in ("operation_id", "turn_id"):
        if key in shown:
            shown[key] = f"fixture-{key}"
    shown["ts"], shown["reply_ts"] = TS, TS + 1
    for i, call in enumerate(shown["tools"]):
        call["id"] = f"call-{i:03}"
    return shown


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
        model="requested-model",
    )
    return tmp / "chat", session_id, binding


def build(tmp_path_factory, monkeypatch) -> dict:
    agent_tests.at(monkeypatch, TS)
    out = {}
    for name in (
        "long",
        "clipped",
        "agent",
        "starved",
        "starved_agent",
        "starved_single",
        "starved_single_agent",
    ):
        root, sid, binding = _conversation(tmp_path_factory.mktemp(name))
        if name == "long":
            events = []
            for i in range(200):
                events += [lambda o, i=i: text(o, _note(i)), lambda o, i=i: tool(o, f"t{i}")]
            turn_id = turn_with(root, sid, binding, events)
        elif name == "starved":
            # Newer turns spent the shared snapshot budget (#1643): this one gets no text at all.
            turn_id = turn_with(
                root, sid, binding, [lambda o: text(o, "a" * 30), lambda o: text(o, "b" * 30)]
            )
            out[name] = _shown(root, sid, binding, turn_id, budget=[len("Inspect the project")])
            continue
        elif name == "clipped":
            newest = "Started the full report. " + "detail " * 3200 + "End of report."
            turn_id = turn_with(
                root,
                sid,
                binding,
                [
                    lambda o: text(o, "Short note first.", partial=True, item="a"),
                    *streamed(newest, "b"),
                ],
            )
        elif name == "starved_single":
            # One message, no budget left (Hermes on #1651): still counted, never vanished.
            turn_id = turn_with(root, sid, binding, [lambda o: text(o, "Only answer.")])
            out[name] = _shown(root, sid, binding, turn_id, budget=[len("Inspect the project")])
            continue
        elif name == "starved_single_agent":
            target = "55555555-4444-4555-8666-777777777777"
            journal.append_events(
                root,
                binding,
                [
                    agent_tests.background(target),
                    agent_tests.own(target, "Own answer."),
                    agent_tests.background(None, "completed"),
                ],
            )
            out[name] = _shown(root, sid, binding, target, budget=[0])
            continue
        elif name == "starved_agent":
            target = "44444444-4444-4555-8666-777777777777"
            journal.append_events(
                root,
                binding,
                [
                    agent_tests.background(target),
                    agent_tests.own(target, "c" * 30),
                    agent_tests.own(target, "d" * 30),
                    agent_tests.background(None, "completed"),
                ],
            )
            out[name] = _shown(root, sid, binding, target, budget=[0])
            continue
        else:
            target = "33333333-4444-4555-8666-777777777777"
            events = [agent_tests.background(target)]
            for i in range(12):
                events += [
                    agent_tests.own(target, _note(i)),
                    agent_tests.own(
                        target,
                        tool=(
                            f"toolu_{i}",
                            "Bash",
                            json.dumps({"command": f"step {i}"}),
                            "completed",
                        ),
                    ),
                ]
            events.append(agent_tests.background(None, "completed"))
            journal.append_events(root, binding, events)
            turn_id = target
        out[name] = _shown(root, sid, binding, turn_id)
    return out


def test_the_browser_fixture_is_what_the_server_sends(tmp_path_factory, monkeypatch):
    built = build(tmp_path_factory, monkeypatch)
    long = built["long"]
    assert long["reply_omitted"]["messages"] > 100 and len(long["reply_breaks"]) > 8
    assert built["clipped"]["first_message_partial"] is True
    assert built["agent"]["kind"] == "agent" and len(built["agent"]["reply_breaks"]) == 12
    for name, count in (
        ("starved", 2),
        ("starved_agent", 2),
        ("starved_single", 1),
        ("starved_single_agent", 1),
    ):
        assert built[name]["reply"] == "" and built[name]["reply_omitted"]["messages"] == count
    rendered = json.dumps(built, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("AGENT_SESSIONS_WRITE_FIXTURES") == "1":
        FIXTURE.write_text(rendered)
    assert FIXTURE.read_text() == rendered, "regenerate with AGENT_SESSIONS_WRITE_FIXTURES=1"
