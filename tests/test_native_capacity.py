"""Capacity edges end a turn, not a generation (#1725): the real worker against the scripted
native (a real child, a real journal, the real IPC), and in process where a full ledger or an
unreadable scan is set up directly. The safety table's codec rows are in
`test_native_safety_table.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path

import pytest

import test_native_journal as journal_tests
from agent_sessions import (
    native_ipc,
    native_journal,
    native_protocol,
    native_runtime,
    native_state,
    native_worker,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.native_processes import Floor, Process
from agent_sessions.native_protocol import ClaudeCodec
from test_native_liveness import _task, _worker
from test_native_runtime import ENGINES, frames, host, ident, project, settle  # noqa: F401

conversation = journal_tests.conversation
SESSION = "b6bf1a94-f584-4d7a-b2a2-850de2ab17f5"
#: Results that take a connection to its high-water mark.
HIGH_WATER = native_protocol.MAX_RESULTS - native_protocol.RESULTS_RESERVE


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _claude(project):  # noqa: F811
    # The current CLI's command lifecycle (#1427): once proved, a running task holds no message.
    (project / "emit-lifecycle").write_text("")
    created = await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident())
    return created["session_key"]


async def _until(predicate, what: str, seconds: float = 60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await predicate()
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"never: {what}")


async def _fill(key: str, *, text: str | None = None) -> None:
    """A turn whose sub-agent's results fill the ledger; its own result ends it normally."""
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=text or f"FLOOD:{HIGH_WATER}")
    _, settled = await settle_long(key, turn)
    assert settled["state"] == "completed"
    assert settled["reply"] == "flooded"  # not cut: it finished as itself


async def settle_long(key: str, turn: str):
    async def done():
        snap = await runtime.snapshot(key)
        found = next((t for t in snap["turns"] if t["turn_id"] == turn), None)
        return (snap, found) if found and found["state"] != "running" else None

    return await _until(done, "the turn settled", seconds=240.0)


def _worker_count(host) -> int:  # noqa: F811
    return len(host.launches)


# --- the real worker --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_full_connection_rolls_over_and_resumes(host, project, monkeypatch):  # noqa: F811
    """Full ledger, nothing running: it retires between turns, says why, and the next Send
    resumes the history, exactly like an idle resume."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)  # a 0.5 s tick
    key = await _claude(project)
    await _fill(key)
    sid = key.partition(":")[2]

    async def gone():
        snap = await runtime.snapshot(key)
        return snap if not snap["native"]["worker"] else None

    await _until(gone, "the connection retired")
    journal = native_journal.read(
        native_runtime._journal_root(native_runtime._api_provider(ENGINES["claude"])), sid
    )
    settled = [e for e in journal.events if e["event"]["kind"] == "background_settled"]
    assert [e["event"]["data"]["reason"] for e in settled] == ["rollover"]
    # Not a failure of any kind: the worker ended clean, and no turn was cut.
    life = native_state.read_lifecycle(settled[0]["worker_id"])
    assert life.get("rollover_at") and not life.get("reason")
    assert not any(
        e["event"]["kind"] == "disconnected" and e["event"]["data"]["operation_id"]
        for e in journal.events
    )

    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="again")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:again" and _worker_count(host) == 2
    second = [f for f in frames(project) if f["frame"].get("uuid") == nxt]
    assert any(a.startswith("--resume=") for a in second[0]["argv"])  # the same history


@pytest.mark.anyio
async def test_a_send_racing_a_full_idle_connection_resumes_at_once(host, project, monkeypatch):  # noqa: F811
    """No wait for a tick: with nothing running the send's own admission retires it, and the
    send goes to a successor, the answer an idle exit gives a request that raced it."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 3600.0)  # the tick won't come
    key = await _claude(project)
    await _fill(key)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="after")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:after" and _worker_count(host) == 2
    assert sum(1 for f in frames(project) if f["frame"].get("uuid") == nxt) == 1


@pytest.mark.anyio
async def test_a_full_connection_rolls_over_once_its_task_settles(host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _claude(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="proves the lifecycle")
    await settle(key, first)
    started = ident()
    await runtime.submit_turn(key, operation_id=started, text="BGTASK")
    await settle(key, started)
    await _fill(key)
    assert (await runtime.snapshot(key))["native"]["worker"]  # held by the task
    (project / "release-bg-task").write_text("")

    async def gone():
        snap = await runtime.snapshot(key)
        return snap if not snap["native"]["worker"] else None

    snap = await _until(gone, "the connection retired once the task settled")
    assert not snap["native"].get("rollover_held")
    assert not snap["native"]["worker_ends"]  # nothing ran on, so nothing ended with it
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="resumed")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:resumed" and _worker_count(host) == 2


@pytest.mark.anyio
async def test_a_full_connection_with_a_running_task_is_held(host, project, monkeypatch):  # noqa: F811
    """Never retired under running work. Send is refused with its remedy, nothing claimed, and
    Stop and resume is the operator's: the confirmed Stop, then the same operation id."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _claude(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="proves the lifecycle")
    await settle(key, first)
    started = ident()
    await runtime.submit_turn(key, operation_id=started, text="BGTASK")
    await settle(key, started)
    await _fill(key)

    async def held():
        snap = await runtime.snapshot(key)
        return snap if snap["native"].get("rollover_held") else None

    snap = await _until(held, "the session said it is held")
    await asyncio.sleep(1.5)  # several ticks: it stays, the task is still running
    snap = await runtime.snapshot(key)
    assert snap["native"]["worker"] and snap["native"]["rollover_held"] and _worker_count(host) == 1
    assert [t["task_id"] for t in snap["native"]["stop_inventory"]["tasks"]] == ["bg1"]

    blocked = ident()
    before = snap["revision"]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(key, operation_id=blocked, text="waits", expected_revision=before)
    assert refused.value.status == 409
    assert refused.value.reason == native_ipc.CONVERSATION_FULL
    assert refused.value.remedy == "stop_resume"
    assert blocked not in {t["turn_id"] for t in (await runtime.snapshot(key))["turns"]}

    inventory = (await runtime.snapshot(key))["native"]["stop_inventory"]
    stopped = await runtime.operator_stop(
        key, worker=inventory["worker"], digest=inventory["digest"]
    )
    assert stopped["containment"] == "gone"
    (await runtime.snapshot(key))["native"]["worker_ends"]
    # The same operation id, as the pane's Stop and resume sends it: it was never claimed.
    await runtime.submit_turn(key, operation_id=blocked, text="waits")
    _, turn = await settle(key, blocked)
    assert turn["reply"] == "echo:waits" and _worker_count(host) == 2
    (project / "release-bg-task").write_text("")


@pytest.mark.anyio
async def test_a_stale_digest_on_a_held_session_refreshes_then_stops(host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _claude(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="proves the lifecycle")
    await settle(key, first)
    started = ident()
    await runtime.submit_turn(key, operation_id=started, text="BGTASK")
    await settle(key, started)
    await _fill(key)
    live = (await runtime.snapshot(key))["native"]["stop_inventory"]
    with pytest.raises(runtime.StructuredError) as stale:
        await runtime.operator_stop(key, worker=live["worker"], digest="0" * 64)
    assert stale.value.status == 409 and stale.value.reason == "inventory_changed"
    assert stale.value.inventory["tasks"][0]["task_id"] == "bg1"
    assert (await runtime.snapshot(key))["native"]["worker"]  # nothing stopped
    stopped = await runtime.operator_stop(
        key, worker=live["worker"], digest=stale.value.inventory["digest"]
    )
    assert stopped["containment"] == "gone"
    (project / "release-bg-task").write_text("")


@pytest.mark.anyio
async def test_a_full_connection_with_overflowed_tasks_recovers_only_on_stop_everything(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """More tasks than are tracked: never ended by the 129th, never listed as complete. An exact
    confirmation is refused (the list isn't all of it), Stop everything is the way out."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _claude(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="proves the lifecycle")
    await settle(key, first)
    many = ident()
    await runtime.submit_turn(key, operation_id=many, text="MANYTASKS:140")
    _, settled = await settle(key, many)
    assert settled["reply"] == "started" and _worker_count(host) == 1  # the 129th ended nothing
    await _fill(key)
    snap = await runtime.snapshot(key)
    inventory = snap["native"]["stop_inventory"]
    assert inventory["overflow"] is True and len(inventory["tasks"]) == 128
    assert snap["native"]["rollover_held"] is True
    with pytest.raises(runtime.StructuredError) as exact:
        await runtime.operator_stop(key, worker=inventory["worker"], digest=inventory["digest"])
    assert exact.value.reason == "inventory_changed"
    assert (await runtime.snapshot(key))["native"]["worker"]
    stopped = await runtime.operator_stop(key, worker=inventory["worker"], everything=True)
    assert stopped["containment"] == "gone"
    (row,) = (await runtime.snapshot(key))["native"]["worker_ends"]
    assert row["reason"] == "stop" and row.get("overflow") is True


@pytest.mark.anyio
async def test_the_129th_task_ends_nothing_and_is_said(host, project):  # noqa: F811
    key = await _claude(project)
    many = ident()
    await runtime.submit_turn(key, operation_id=many, text="MANYTASKS:129")
    await settle(key, many)
    snap = await runtime.snapshot(key)
    assert snap["native"]["stop_inventory"]["overflow"] is True
    # The same generation, alive: it said so instead of ending.
    assert snap["native"]["worker"] and _worker_count(host) == 1
    assert len(snap["native"]["background_tasks"]) == native_protocol.MAX_PENDING


@pytest.mark.anyio
async def test_a_tool_output_frame_too_large_is_a_row_and_the_turn_succeeds(host, project):  # noqa: F811
    key = await _claude(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIGFRAME:output")
    _, settled = await settle(key, turn)
    assert settled["state"] == "completed" and settled["reply"] == "echo:BIGFRAME:output"
    (row,) = [t for t in settled["tools"] if t["name"] == "battlelab"]
    assert row["summary"] == native_protocol.FRAME_SKIPPED
    assert _worker_count(host) == 1


@pytest.mark.anyio
async def test_a_result_too_large_ends_the_turn_uncertain_not_a_success(host, project):  # noqa: F811
    key = await _claude(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIGFRAME:result")
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["state"] == "uncertain"
    assert settled["reason"] == native_protocol.LOST_RESULT
    # The generation lives: the next message runs on it.
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="next")
    _, turn_after = await settle(key, nxt)
    assert turn_after["reply"] == "echo:next" and _worker_count(host) == 1


@pytest.mark.anyio
async def test_a_request_too_large_is_refused_and_the_turn_is_never_a_success(host, project):  # noqa: F811
    key = await _claude(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIGFRAME:request")
    _, settled = await settle(key, turn, state=("uncertain",))
    # The agent's own result said success, with the refusal it read: not what the operator gets.
    assert settled["state"] == "uncertain" and settled["reason"] == native_protocol.LOST_REQUEST
    assert settled["reply"] == "refused:error"
    answers = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("type") == "control_response"
        and f["frame"]["response"].get("request_id") == "perm-big"
    ]
    assert len(answers) == 1 and answers[0]["response"]["subtype"] == "error"
    assert not (await runtime.snapshot(key))["pending_requests"]
    assert _worker_count(host) == 1


@pytest.mark.anyio
async def test_a_request_too_large_with_its_id_after_its_payload_is_answered(host, project):  # noqa: F811
    """The real worker, the real bytes: the id is past everything the frame's start can show."""
    key = await _claude(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIGFRAME:request-last")
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["reply"] == "refused:error"
    answers = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("type") == "control_response"
        and f["frame"]["response"].get("request_id") == "perm-big"
    ]
    assert len(answers) == 1 and answers[0]["response"]["subtype"] == "error"
    assert _worker_count(host) == 1


@pytest.mark.anyio
async def test_a_request_that_cant_be_answered_ends_the_connection_and_says_so(host, project):  # noqa: F811
    """Left unanswered, the agent would wait for ever. It fails closed: the generation ends, the
    turn reads uncertain with why, and the next Send resumes the history."""
    key = await _claude(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIGFRAME:unreadable")
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["reason"] == native_protocol.LOST_UNREADABLE

    async def gone():
        snap = await runtime.snapshot(key)
        return snap if not snap["native"]["worker"] else None

    await _until(gone, "the connection ended")
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="resumed")
    _, after = await settle(key, nxt)
    assert after["reply"] == "echo:resumed" and _worker_count(host) == 2


@pytest.mark.anyio
async def test_a_frame_too_large_outside_any_turn_is_counted_for_the_pane(host, project):  # noqa: F811
    key = await _claude(project)
    sid = key.partition(":")[2]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="plain")
    await settle(key, turn)
    gen = native_runtime._generation(sid)
    # What a dropped frame leaves on the lifecycle, read back through the snapshot.
    native_state.update_lifecycle(gen.worker_id, frames_skipped=2)
    assert (await runtime.snapshot(key))["native"]["frames_skipped"] == 2


@pytest.mark.anyio
async def test_capacity_an_unterminated_frame_is_discarded_and_the_turn_is_uncertain(host, project):  # noqa: F811
    """The row's test is `test_a_partial_frame…` in test_native_runtime.py; this is its claim."""
    created = await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())
    key = created["session_key"]
    turn = ident()
    try:
        await runtime.submit_turn(key, operation_id=turn, text="PARTIAL")
    except runtime.StructuredError as exc:
        assert exc.status == 503
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["state"] == "uncertain"
    page = await runtime.events(key, after=0)
    assert any("mid-frame" in e["data"].get("message", "") for e in page["events"])


@pytest.mark.anyio
async def test_a_persistently_unreadable_scan_keeps_a_way_out_through_the_api(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """The journal says the scan can't be read, for as long as it can't. An exact confirmation of
    that state is refused by the worker's own rescan, the inventory is never read as empty, and
    Stop everything, disclosed in the pane, recovers the session."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _claude(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="proves the lifecycle")
    await settle(key, first)
    started = ident()
    await runtime.submit_turn(key, operation_id=started, text="BGTASK")
    await settle(key, started)
    await _fill(key)
    sid = key.partition(":")[2]
    gen = native_runtime._generation(sid)
    binding = native_ipc.Binding(
        key, gen.worker_id, gen.config["connection_id"], gen.config["adapter"]
    )
    root = native_runtime._journal_root(native_runtime._api_provider(ENGINES["claude"]))
    unknown = native_ipc.stop_digest(gen.worker_id, ["bg1"], (), "unknown")
    native_journal.append_events(
        root,
        binding,
        [
            {
                "kind": "background_inventory",
                "data": {
                    "engine": ["bg1"],
                    "processes": [],
                    "more": 0,
                    "scan": "unknown",
                    "digest": unknown,
                },
            }
        ],
    )
    snap = await runtime.snapshot(key)
    inventory = snap["native"]["stop_inventory"]
    assert inventory["scan"] == "unknown" and inventory["tasks"][0]["task_id"] == "bg1"
    with pytest.raises(runtime.StructuredError) as exact:
        await runtime.operator_stop(key, worker=inventory["worker"], digest=inventory["digest"])
    assert exact.value.reason == "inventory_changed"
    assert (await runtime.snapshot(key))["native"]["worker"]
    stopped = await runtime.operator_stop(key, worker=inventory["worker"], everything=True)
    assert stopped["containment"] == "gone"
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="resumed")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:resumed"
    (project / "release-bg-task").write_text("")


def test_the_turns_route_carries_a_refusals_reason_and_remedy(host, project, auth_cfg, monkeypatch):  # noqa: F811
    """Closed values beside the words, for the pane to act on: no new route, no bypass."""
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        c.headers.update(
            {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
        )
        body = {"engine": ENGINES["claude"], "cwd": str(project), "operation_id": ident()}
        key = c.post("/api/structured/sessions", json=body).json()["session_key"]
        turns = f"/api/structured/sessions/{key}/turns"
        first = ident()
        assert c.post(turns, json={"operation_id": first, "text": "one"}).status_code < 300
        for _ in range(400):
            snap = c.get(f"/api/structured/sessions/{key}").json()
            if snap["turns"] and snap["turns"][0]["state"] == "completed":
                break
            time.sleep(0.05)
        # A real conversation change: the expectation is older than what the pane must see.
        stale = c.post(
            turns,
            json={"operation_id": ident(), "text": "two", "expected_revision": 1},
        )
        assert stale.status_code == 409
        assert stale.json() == {
            "detail": native_journal.REVISION_CHANGED,
            "reason": native_ipc.REVISION_MOVED,
            "remedy": "retry",
        }

        async def full(*_args, **_kwargs):
            raise native_runtime.NativeError(
                409, "full", reason=native_ipc.CONVERSATION_FULL, remedy="stop_resume"
            )

        monkeypatch.setattr(native_runtime, "submit", full)
        held = c.post(turns, json={"operation_id": ident(), "text": "three"})
        assert held.status_code == 409 and held.json()["remedy"] == "stop_resume"

        async def plain(*_args, **_kwargs):
            raise native_runtime.NativeError(409, "this turn was not sent")

        monkeypatch.setattr(native_runtime, "submit", plain)
        refused = c.post(turns, json={"operation_id": ident(), "text": "four"})
        assert refused.status_code == 409 and "remedy" not in refused.json()


@pytest.mark.parametrize(
    ("detail", "remedy"),
    [
        ("a previous native worker's state is unknown; stop the session first", "stop_resume"),
        ("this session's native history was never created; stop it", "stop_resume"),
        ("the native worker could not be started; stop the session", "stop_resume"),
        ("to attach documents, stop and resume this session", "resume_with_attachments"),
        ("the conversation kept changing; retry", "retry"),
        ("permissions changed while sending; review the session and send again", "retry"),
        ("the native worker is unreachable", "retry"),
        ("the native worker is unavailable", "retry"),
    ],
)
def test_every_dead_end_that_names_an_action_carries_its_remedy(detail, remedy):
    """The refusals the pane shows that tell the operator what to do: each names it as a button."""
    source = (Path(native_runtime.__file__)).read_text()
    at = source.index(detail)
    window = source[at : at + 240]
    assert f'remedy="{remedy}"' in window, detail
    assert remedy in native_ipc.REMEDIES


def _reading(snap: dict) -> str:
    """What the pane's `outbox.sameConversation` compares, the same way: the JSON of it."""
    return json.dumps(
        [
            snap["turns"],
            snap["pending_requests"],
            snap["active_turn"],
            snap["native"].get("background_turn"),
            {
                key: value
                for key, value in (snap["native"].get("plan") or {}).items()
                if key in {"entries", "explanation", "document"}
            },
        ],
        sort_keys=True,
    )


async def _gone(key: str) -> dict:
    async def check():
        snap = await runtime.snapshot(key)
        return snap if not snap["native"]["worker"] else None

    return await _until(check, "the worker ended")


async def _settled_revision(key: str) -> dict:
    """The revision a worker's very last record leaves: it stops moving."""
    previous = None
    for _ in range(100):
        snap = await runtime.snapshot(key)
        if previous is not None and snap["revision"] == previous:
            return snap
        previous = snap["revision"]
        await asyncio.sleep(0.2)
    raise AssertionError("the revision never settled")


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode", "copilot"])
@pytest.mark.parametrize("end", ["idle", "crash"])
async def test_the_conversation_reads_the_same_across_a_real_worker_exit(  # noqa: F811
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    source,
    end,
):
    """#1420, against the real server: the pane's recovery fires only if what it compares stays
    byte-identical across a worker's exit while the revision moves. The browser test mocks this;
    this is the proof the mock is what the server does: the stale send is refused with the closed
    reason, and the same operation id at the fresh revision is delivered once."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 300.0 if end == "crash" else 2.0)
    created = await runtime.create_session(ENGINES[source], str(project), operation_id=ident())
    key = created["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="one")
    await settle(key, first)
    seen = await runtime.snapshot(key)
    assert seen["native"]["worker"]
    if end == "crash":
        pid = next(f["pid"] for f in frames(project) if f.get("pid"))
        assert pid > 1
        os.kill(pid, signal.SIGKILL)  # the scripted agent this test started, by its own pid
    await _gone(key)
    # The successor below keeps the real 30 min idle: only the first generation's exit is the test.
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 1800.0)
    now = await _settled_revision(key)
    assert now["revision"] > seen["revision"]
    assert _reading(now) == _reading(seen)  # what `sameConversation` reads

    nxt = ident()
    with pytest.raises(runtime.StructuredError) as stale:
        await runtime.submit_turn(
            key, operation_id=nxt, text="two", expected_revision=seen["revision"]
        )
    assert stale.value.status == 409 and stale.value.reason == native_ipc.REVISION_MOVED
    # As the pane goes on: it re-reads, compares, and sends the same id at what it just read. That
    # refused send has launched the successor (its startup records are in this read).
    fresh = await _settled_revision(key)
    assert nxt not in {t["turn_id"] for t in fresh["turns"]}
    assert _reading(fresh) == _reading(seen)
    await runtime.submit_turn(
        key, operation_id=nxt, text="two", expected_revision=fresh["revision"]
    )
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two"
    delivered = [
        f
        for f in frames(project)
        if nxt in json.dumps(f["frame"]) or "two" in json.dumps(f["frame"].get("params", {}))
    ]
    assert len(delivered) == 1


# --- in process: the states no scripted child sets up cheaply --------------------------------


def _full(worker) -> None:
    worker.codec._results = {
        str(i): "x" for i in range(native_protocol.MAX_RESULTS - native_protocol.RESULTS_RESERVE)
    }


def _submit(worker, binding, root, sid, operation_id=None):
    request = journal_tests.request(
        binding,
        operation_id=operation_id,
        revision=native_journal.read(root, sid).revision,
    )
    return asyncio.run(worker.effect(native_ipc.validate_request(request)))


def _claude_worker(conversation):  # noqa: F811
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.floor = Floor("ok", ())
    worker.stop_child = lambda: worker.__dict__.setdefault("stopped", True)
    worker.gate_open = lambda: True
    worker._observe_real = worker.observe_floor
    worker.observe_floor = lambda: worker.floor
    return root, sid, binding, worker


def test_a_full_connection_refuses_a_new_turn_with_its_reason_and_claims_nothing(conversation):
    root, sid, binding, worker = _claude_worker(conversation)
    worker.codec.feed(_task("task_started", "t1", description="suite"))
    _full(worker)
    before = native_journal.read(root, sid).revision
    answer = _submit(worker, binding, root, sid)
    assert answer["error"]["reason"] == native_ipc.CONVERSATION_FULL
    assert answer["error"]["code"] == "conflict"
    assert native_journal.read(root, sid).revision == before
    assert not worker.closing and "stopped" not in worker.__dict__


def test_a_full_idle_connection_is_not_held_and_a_busy_one_says_so(conversation):
    _, _, _, worker = _claude_worker(conversation)
    worker.report = lambda **_fields: None
    _full(worker)
    asyncio.run(worker.report_rollover())
    assert not worker.reported_held  # about to retire: nothing holds it
    worker.codec.feed(_task("task_started", "t1"))
    asyncio.run(worker.report_rollover())
    assert worker.reported_held


def test_a_full_connection_with_nothing_running_retires_on_the_send(conversation):
    root, sid, binding, worker = _claude_worker(conversation)
    _full(worker)
    answer = _submit(worker, binding, root, sid)
    # The same answer an idle exit gives a racing request: the runtime resumes in a successor.
    assert answer["error"]["message"] == native_worker.SHUTTING_DOWN
    assert worker.closing and worker.stopped
    (settled,) = [
        e["event"]["data"]
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == "background_settled"
    ]
    assert settled["reason"] == "rollover"


def test_a_send_rescans_before_a_full_connection_retires(conversation):
    """The floor is up to a tick old. A server the agent just left running (`nohup server &`) must
    not be killed by the send that finds the connection full, and settled as if nothing ran."""
    root, sid, binding, worker = _claude_worker(conversation)
    _full(worker)
    worker.floor = Floor("ok", ())  # as last read, a tick ago
    server = Process(950, 40, "node")

    def scan():
        worker.floor = Floor("ok", (server,))
        return worker.floor

    worker.observe_floor = scan
    answer = _submit(worker, binding, root, sid)
    assert answer["error"]["reason"] == native_ipc.CONVERSATION_FULL
    assert not worker.closing and "stopped" not in worker.__dict__
    assert not [
        e
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == "background_settled"
    ]


def test_a_failed_rescan_refuses_and_never_retires(conversation):
    root, sid, binding, worker = _claude_worker(conversation)
    _full(worker)
    worker.floor = Floor("ok", ())

    def scan():
        raise OSError("can't read the group")

    worker.observe_floor = scan
    answer = _submit(worker, binding, root, sid)
    assert answer["error"]["reason"] == native_ipc.CONVERSATION_FULL
    assert not worker.closing and "stopped" not in worker.__dict__


def test_idle_watch_rescans_under_the_effect_lock_before_it_retires(conversation):
    """The tick's own scan can be a lock-wait old: what started meanwhile is not retired over."""
    _, _, _, worker = _claude_worker(conversation)
    worker.tick = 0.01
    _full(worker)
    scans = []

    def scan():
        scans.append(1)
        if len(scans) == 2:  # the rescan under the lock, right after the tick's own
            worker.floor = Floor("ok", (Process(950, 40, "node"),))
        return worker.floor

    worker.observe_floor = scan

    async def run():
        watch = asyncio.create_task(worker.idle_watch())
        await asyncio.sleep(0.05)
        assert not worker.closing
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(run())


def test_a_send_path_retire_settles_once_with_idle_watch_still_running(conversation):
    """The retirement a send starts doesn't stop the watch: its next tick must not settle again,
    or the pane would read two ends for one generation."""
    root, sid, binding, worker = _claude_worker(conversation)
    worker.tick = 0.01
    _full(worker)

    async def run():
        watch = asyncio.create_task(worker.idle_watch())
        request = journal_tests.request(binding, revision=native_journal.read(root, sid).revision)
        await worker.effect(native_ipc.validate_request(request))
        await asyncio.sleep(0.05)
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(run())
    settled = [
        e
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == "background_settled"
    ]
    assert len(settled) == 1


def test_settle_writes_one_settlement_whoever_asks_twice(conversation):
    root, sid, _, worker = _claude_worker(conversation)
    worker.settle("rollover", worker.inventory())
    worker.settle("idle", worker.inventory(), idle_for=1)
    settled = [
        e["event"]["data"]["reason"]
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == "background_settled"
    ]
    assert settled == ["rollover"]


def test_a_replay_on_a_full_connection_still_observes_its_receipt(conversation):
    root, sid, binding, worker = _claude_worker(conversation)
    worker.codec.feed(_task("task_started", "t1"))
    operation = ident()
    worker.codec.submit = lambda *a, **k: {"type": "user"}  # nothing to write to
    worker.write = _no_write
    first = _submit(worker, binding, root, sid, operation)
    assert "result" in first
    _full(worker)
    again = _submit(worker, binding, root, sid, operation)
    assert again["result"]["operation_id"] == first["result"]["operation_id"]


async def _no_write(_frame):
    return None


@pytest.mark.parametrize(
    ("floor", "running", "held"),
    [
        (Floor("ok", ()), False, False),
        (Floor("ok", (Process(950, 40, "cargo"),)), False, True),
        (Floor("unknown"), False, True),
        (Floor("ok", ()), True, True),
    ],
    ids=["settled", "a-process", "unreadable-scan", "a-task"],
)
def test_held_while_anything_runs_and_only_then(conversation, floor, running, held):
    _, _, _, worker = _claude_worker(conversation)
    worker.floor = floor
    if running:
        worker.codec.feed(_task("task_started", "t1"))
    _full(worker)
    assert worker.rollover_due() and worker.busy() is held


def test_idle_watch_retires_a_full_connection_only_once_nothing_runs(conversation, monkeypatch):
    _, _, _, worker = _claude_worker(conversation)
    worker.tick = 0.01
    worker.codec.feed(_task("task_started", "t1"))
    _full(worker)

    async def run():
        watch = asyncio.create_task(worker.idle_watch())
        await asyncio.sleep(0.1)
        assert not worker.closing and worker.reported_held
        worker.codec.feed(_task("task_notification", "t1", status="completed"))
        await asyncio.wait_for(watch, 5)

    asyncio.run(run())
    assert worker.closing and worker.stopped


def test_an_overflowed_inventory_is_never_confirmed_exactly(conversation):
    root, sid, binding, worker = _claude_worker(conversation)
    for n in range(native_protocol.MAX_PENDING + 1):
        worker.codec.feed(_task("task_started", f"t{n}"))
    inventory = worker.inventory()
    assert inventory["overflow"] is True and not worker.complete(inventory)
    # Its digest differs from the same ids without the overflow: a confirmation of a complete list
    # never matches a set that stopped being complete.
    assert inventory["digest"] != native_ipc.stop_digest(
        worker.worker_id, worker.engine_ids(), (), "ok"
    )
    request = journal_tests.request(
        binding,
        action="stop",
        revision=native_journal.read(root, sid).revision,
        confirm_digest=inventory["digest"],
    )
    answer = asyncio.run(worker.effect(native_ipc.validate_request(request)))
    assert answer["error"]["reason"] == "inventory_changed"
    assert not worker.stop_accepted


def test_a_persistently_unreadable_scan_on_a_full_session_stops_only_by_stop_everything(
    conversation,
):
    """Held by what can't be listed, for as long as it can't: the inventory is never read as
    empty, an exact confirmation is refused, and Stop everything is the way out."""
    root, sid, binding, worker = _claude_worker(conversation)
    worker.floor = Floor("unknown")
    _full(worker)
    assert worker.busy() and worker.rollover_due()
    inventory = worker.inventory()
    assert inventory["scan"] == "unknown" and not worker.complete(inventory)
    request = journal_tests.request(
        binding,
        action="stop",
        revision=native_journal.read(root, sid).revision,
        confirm_digest=inventory["digest"],
    )
    answer = asyncio.run(worker.effect(native_ipc.validate_request(request)))
    assert answer["error"]["reason"] == "inventory_changed" and not worker.stop_accepted
    request = journal_tests.request(
        binding,
        action="stop",
        revision=native_journal.read(root, sid).revision,
        confirm_everything=True,
    )
    answer = asyncio.run(worker.effect(native_ipc.validate_request(request)))
    assert "result" in answer and worker.stop_accepted


def test_the_rollover_settlement_and_the_overflow_are_closed_in_the_schema():
    event = {
        "kind": "background_settled",
        "data": {
            "reason": "rollover",
            "engine": [],
            "processes": [],
            "more": 0,
            "scan": "ok",
            "overflow": True,
        },
    }
    assert native_ipc.normalize_event(event)["data"]["reason"] == "rollover"
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({**event, "data": {**event["data"], "overflow": False}})


@pytest.mark.anyio
async def test_a_write_the_agent_never_took_ends_it(conversation, monkeypatch):
    """The safety row beside the capacity ones: a half-written frame is never resent."""
    root, sid, binding, worker = _claude_worker(conversation)

    async def broken(_frame):
        raise ConnectionError("gone")

    worker.write = broken
    request = journal_tests.request(binding, revision=native_journal.read(root, sid).revision)
    answer = await worker.effect(native_ipc.validate_request(request))
    assert answer["result"]["handoff"] == "uncertain" and worker.stopped


@pytest.mark.anyio
async def test_a_queue_failure_ends_the_generation(conversation):
    """A queued message failing after admission can't permit further native writes."""
    _, _, _, worker = _claude_worker(conversation)
    worker.queued.append({"operation_id": ident()})
    worker.queue_wake.set()

    async def answered(_params):
        return True

    async def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    @contextlib.contextmanager
    def admitted(_params=None):
        yield

    worker._answer_current = answered
    worker.queued_admission = admitted
    worker.handoff = boom
    await asyncio.wait_for(worker.dispatch_queue(), 5)
    assert worker.reason.startswith("queued message dispatch failed") and worker.stopped


def test_the_measured_bound_and_the_reserve_are_what_the_docs_say():
    doc = (Path(__file__).parent.parent / "docs/invariants/native-runtime.md").read_text()
    assert f"`RESULTS_RESERVE` ({native_protocol.RESULTS_RESERVE})" in doc
    assert json.dumps(native_protocol.MAX_RESULTS) in doc
