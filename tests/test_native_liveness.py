"""An API session stays alive until it is idle (#1724).

Background work counts on every client: engine-reported tasks (Claude's task list, Codex's
background terminals) and the process floor (any process in the worker's unit the agent
started). The background set is reconciled, journaled as a generation-fenced inventory and
settled when a generation ends with work still running; an operator Stop ends only the work it
was confirmed for. Real-systemd integration lives in ``test_native_liveness_systemd.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import signal
import time
import uuid
from pathlib import Path

import pytest

import test_native_journal as journal_tests
from agent_sessions import (
    native_containment,
    native_ipc,
    native_journal,
    native_processes,
    native_protocol,
    native_runtime,
    native_state,
    native_worker,
    share_api,
    structured_runtime,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.native_processes import Floor, Process, Scan
from agent_sessions.native_protocol import ClaudeCodec, CodexCodec, OpencodeAcpCodec
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401

conversation = journal_tests.conversation
FIXTURES = Path(__file__).parent / "fixtures"
SESSION = str(uuid.uuid4())
UNIT = "agent-sessions-native-0123.service"
GROUP = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- the process floor: a fake cgroup tree and /proc ---------------------------------------


def stat_line(pid: int, comm: str, start: int, state: str = "S", ppid: int = 1) -> str:
    fields = [state, str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 30
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


def fake_tree(tmp_path: Path, groups: dict[str, list[tuple]], *, own: str | None = GROUP):
    """``groups``: cgroup path → [(pid, comm, start[, state])]. Returns (cgroup root, proc)."""
    root, proc = tmp_path / "cgroup", tmp_path / "proc"
    for group, procs in groups.items():
        directory = root / group.lstrip("/")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cgroup.procs").write_text("".join(f"{p[0]}\n" for p in procs))
        for pid, comm, start, *state in procs:
            (proc / str(pid)).mkdir(parents=True, exist_ok=True)
            (proc / str(pid) / "stat").write_text(stat_line(pid, comm, start, *state))
    (proc / "self").mkdir(parents=True, exist_ok=True)
    if own is not None:
        (proc / "self" / "cgroup").write_text(f"0::{own}\n")
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1790000000\n")
    (proc / "uptime").write_text("100000.00 1.00\n")
    return root, proc


def test_a_scan_lists_the_group_and_its_descendant_groups(tmp_path):
    root, proc = fake_tree(
        tmp_path,
        {
            GROUP: [(10, "python3", 100), (11, "codex", 101), (12, "sleep", 500, "Z")],
            f"{GROUP}/tools": [(20, "node", 700)],
        },
    )
    observed = native_processes.scan(GROUP, root=root, proc=proc)
    assert observed.state == "ok"
    # A zombie runs nothing any more; a nested group (#1732's tools) is walked too.
    assert {(p.pid, p.comm) for p in observed.processes} == {
        (10, "python3"),
        (11, "codex"),
        (20, "node"),
    }


def test_a_process_that_vanishes_mid_scan_is_gone_not_unknown(tmp_path):
    root, proc = fake_tree(tmp_path, {GROUP: [(10, "python3", 100)]})
    (root / GROUP.lstrip("/") / "cgroup.procs").write_text("10\n99\n")  # 99 exited meanwhile
    observed = native_processes.scan(GROUP, root=root, proc=proc)
    assert observed.state == "ok" and {p.pid for p in observed.processes} == {10}


@pytest.mark.parametrize(
    "damage",
    ["unreadable-procs", "malformed-procs", "malformed-stat", "unreadable-stat", "no-group"],
)
def test_a_failed_or_partial_scan_is_unknown_and_unknown_is_busy(tmp_path, damage):
    root, proc = fake_tree(tmp_path, {GROUP: [(10, "python3", 100), (11, "node", 300)]})
    procs = root / GROUP.lstrip("/") / "cgroup.procs"
    if damage == "unreadable-procs":
        procs.unlink()
        procs.mkdir()  # reading a directory fails with an OSError that is not "gone"
    elif damage == "malformed-procs":
        procs.write_text("10\nnot-a-pid\n")
    elif damage == "malformed-stat":
        (proc / "11" / "stat").write_text("11 (node S\n")
    elif damage == "unreadable-stat":
        (proc / "11" / "stat").unlink()
        (proc / "11" / "stat").mkdir()
    else:
        group = "/user.slice/gone.service"
        observed = native_processes.scan(group, root=root, proc=proc)
        assert observed.state == "unknown"
        return
    observed = native_processes.scan(GROUP, root=root, proc=proc)
    assert observed == Scan("unknown")
    floor = native_processes.floor(observed, frozenset({(10, 100)}))
    assert floor.state == "unknown" and floor.busy  # never proof of idle


def test_a_scan_above_its_bound_is_unknown_never_partial(tmp_path, monkeypatch):
    root, proc = fake_tree(tmp_path, {GROUP: [(10 + i, "sleep", 100 + i) for i in range(5)]})
    monkeypatch.setattr(native_processes, "MAX_PROCESSES", 4)
    assert native_processes.scan(GROUP, root=root, proc=proc).state == "unknown"


def test_comm_with_spaces_and_parentheses_is_parsed_after_the_last_paren():
    assert native_processes.parse_stat(stat_line(7, "a) (b c", 4242)) == ("a) (b c", "S", 1, 4242)


def test_the_floor_counts_every_process_not_in_the_baseline_and_guards_pid_reuse(tmp_path):
    baseline = frozenset({(10, 100), (11, 101), (12, 102)})
    root, proc = fake_tree(
        tmp_path,
        {
            GROUP: [
                (10, "python3", 100),  # the worker
                (11, "codex", 101),  # the native CLI
                (12, "node", 900),  # pid 12 was an MCP server at readiness; reused since
                (40, "sleep", 950),  # a detached job
            ]
        },
    )
    floor = native_processes.floor(native_processes.scan(GROUP, root=root, proc=proc), baseline)
    assert floor.state == "ok" and floor.busy
    assert [(p.pid, p.start) for p in floor.processes] == [(12, 900), (40, 950)]
    # Only the baseline: idle.
    root2, proc2 = fake_tree(tmp_path / "b", {GROUP: [(10, "python3", 100), (11, "codex", 101)]})
    quiet = native_processes.floor(native_processes.scan(GROUP, root=root2, proc=proc2), baseline)
    assert quiet.state == "ok" and quiet.processes == () and not quiet.busy
    # No baseline (it could not be taken at readiness): unknown, busy.
    assert native_processes.floor(Scan("ok"), None).busy


def test_the_worker_reads_only_its_own_pinned_unit_group(tmp_path):
    root, proc = fake_tree(tmp_path, {GROUP: [(10, "python3", 100)]})
    assert native_processes.observe(UNIT, GROUP, proc=proc, root=root).state == "ok"
    # In its unit but the pin is missing or another group's: unknown, never a guess.
    assert native_processes.observe(UNIT, None, proc=proc, root=root).state == "unknown"
    assert native_processes.observe(UNIT, "/other", proc=proc, root=root).state == "unknown"
    # Not in a unit of its own at all (a test harness's substituted service manager).
    _, harness = fake_tree(tmp_path / "h", {}, own="/user.slice/session-3.scope")
    assert native_processes.observe(UNIT, GROUP, proc=harness, root=root).state == "uncontained"
    # No cgroup v2 line, or an unreadable /proc/self/cgroup: unknown.
    _, none = fake_tree(tmp_path / "n", {}, own=None)
    assert native_processes.observe(UNIT, GROUP, proc=none, root=root).state == "unknown"


def test_started_at_is_boot_time_plus_start_ticks(tmp_path):
    _, proc = fake_tree(tmp_path, {})
    ticks = __import__("os").sysconf("SC_CLK_TCK")
    assert native_processes.started_at(Process(ticks * 60, 5, "x"), proc) == 1790000060


# --- the Stop digest and the closed event schema -----------------------------------------


def test_the_stop_digest_binds_generation_tasks_processes_and_scan():
    worker = str(uuid.uuid4())
    base = native_ipc.stop_digest(worker, ["a", "b"], [(5, 50)], "ok")
    assert base == native_ipc.stop_digest(worker, ["b", "a"], [(5, 50)], "ok")
    assert base != native_ipc.stop_digest(str(uuid.uuid4()), ["a", "b"], [(5, 50)], "ok")
    assert base != native_ipc.stop_digest(worker, ["a"], [(5, 50)], "ok")
    assert base != native_ipc.stop_digest(worker, ["a", "b"], [(5, 50), (6, 60)], "ok")
    assert base != native_ipc.stop_digest(worker, ["a", "b"], [(5, 51)], "ok")  # pid reuse
    unknown = native_ipc.stop_digest(worker, ["a", "b"], (), "unknown")
    # An unreadable scan is never an empty one.
    assert unknown not in {base, native_ipc.stop_digest(worker, ["a", "b"], (), "ok")}
    assert native_ipc.stop_digest(worker, [], (), "uncontained") == native_ipc.stop_digest(
        worker, [], (), "ok"
    )


def inventory(*, engine=(), processes=(), scan="ok", worker=None):
    procs = [
        {"pid": pid, "start": start, "comm": comm, "started_at": 1790000000 + start}
        for pid, start, comm in processes
    ]
    return {
        "engine": list(engine),
        "processes": procs,
        "more": 0,
        "scan": scan,
        "digest": native_ipc.stop_digest(
            worker or str(uuid.uuid4()), engine, [(p, s) for p, s, _ in processes], scan
        ),
    }


def test_inventory_and_settlement_events_are_closed_and_bounded():
    good = inventory(engine=["t1"], processes=[(40, 950, "sleep")])
    assert native_ipc.normalize_event({"kind": "background_inventory", "data": good})
    settled = {k: good[k] for k in ("engine", "processes", "more", "scan")}
    settled.update(reason="idle", idle_for=1800)
    assert native_ipc.normalize_event({"kind": "background_settled", "data": settled})
    bad = [
        ("background_inventory", {**good, "argv": ["npm", "run", "dev"]}),
        ("background_inventory", {**good, "processes": [{**good["processes"][0], "env": "X"}]}),
        ("background_inventory", {**good, "scan": "unknown"}),  # an unreadable scan lists none
        ("background_inventory", {**good, "engine": ["t"] * 2}),
        ("background_inventory", {**good, "engine": [f"t{i}" for i in range(129)]}),
        ("background_inventory", {**good, "processes": good["processes"] * 33}),
        (
            "background_inventory",
            {**good, "processes": [{**good["processes"][0], "comm": "x" * 17}]},
        ),
        ("background_settled", {**settled, "reason": "oom"}),  # only the reader derives oom
        ("background_settled", {**settled, "reason": "stop"}),  # idle_for is idle's alone
    ]
    for kind, data in bad:
        with pytest.raises(native_ipc.IPCError):
            native_ipc.normalize_event({"kind": kind, "data": data})


def test_a_stop_confirmation_is_part_of_the_stop_request_only():
    binding = native_ipc.Binding(f"x-api:{SESSION}", ident(), ident(), "codex-app-server")
    request = journal_tests.request(binding, action="stop", confirm_digest="a" * 64)
    assert native_ipc.validate_request(request)["params"]["confirm_digest"] == "a" * 64
    with pytest.raises(native_ipc.IPCError):
        native_ipc.validate_request(journal_tests.request(binding, confirm_digest="a" * 64))
    with pytest.raises(native_ipc.IPCError):
        native_ipc.validate_request(
            journal_tests.request(binding, action="stop", confirm_digest="nope")
        )


# --- Claude: the full task list reconciles the set ----------------------------------------


def _task(subtype, task_id, **fields):
    return {"type": "system", "subtype": subtype, "task_id": task_id, **fields}


def _tasks_changed(*tasks):
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [
            {"task_id": t, "run_id": "r", "task_type": "local_bash", "description": f"run {t}"}
            for t in tasks
        ],
    }


def test_claudes_full_task_list_ends_a_ghost_and_adds_a_missed_start():
    codec = ClaudeCodec(SESSION)
    codec.feed(_task("task_started", "a", description="A"))
    codec.feed(_task("task_started", "b", description="B"))
    assert codec._background == {"a", "b"}
    # `b`'s task_notification was missed; `c` started without a task_started we saw.
    events = codec.feed(_tasks_changed("a", "c"))
    assert codec._background == {"a", "c"}
    assert [(e.data["task_id"], e.data["task_state"]) for e in events] == [
        ("b", "ended"),
        ("c", "running"),
    ]
    assert events[1].data["description"] == "run c" and events[1].data["task_type"] == "local_bash"
    assert all(e.data["active"] is True for e in events)
    # The same list again records nothing: no byte-identical background events (#1733).
    assert codec.feed(_tasks_changed("a", "c")) == []
    (gone,) = [e for e in codec.feed(_tasks_changed()) if e.data["task_id"] == "a"][:1] or [None]
    assert gone is not None and codec._background == set()
    for event in events:
        native_ipc.normalize_event({"kind": event.kind, "data": event.data})


def test_a_ghost_task_cannot_keep_the_worker_alive(conversation):
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.floor = Floor("ok", ())
    worker.codec.feed(_task("task_started", "ghost", description="never reported ended"))
    assert worker.busy()
    worker.codec.feed(_tasks_changed())  # the CLI's own list says nothing runs
    assert not worker.busy()


@pytest.mark.parametrize("bad", [{"tasks": "x"}, {"tasks": [1]}, {"tasks": [{}]}])
def test_a_malformed_task_list_is_a_protocol_error(bad):
    with pytest.raises(native_protocol.ProtocolError):
        ClaudeCodec(SESSION).feed({"type": "system", "subtype": "background_tasks_changed", **bad})
    with pytest.raises(native_protocol.ProtocolError):
        ClaudeCodec(SESSION).feed(_tasks_changed(*[f"t{i}" for i in range(129)]))


def test_the_task_list_between_completed_and_the_result_does_not_lose_the_result():
    """Regression (#1724): `background_tasks_changed` arriving between an input's lifecycle
    `completed` and its result used to end the turn "without its result"."""
    from test_claude_send_now_captured import OPERATION, _ended, _result

    codec = _ended()
    session = codec.native_id
    changed = {**_tasks_changed("bg"), "session_id": session}
    events = codec.feed(changed)
    assert all(e.kind != "turn_completed" for e in events) and codec.operation_id == OPERATION
    assert codec._background == {"bg"}
    (done,) = [
        e
        for e in codec.feed(_result(origin={"kind": "human"}, user_message_uuid=OPERATION))
        if e.kind == "turn_completed"
    ]
    assert done.data["state"] == "completed" and done.data["text"] == "done"


# --- Codex and opencode: the measured frames (Phase 1 fixtures) -------------------------------


def replay(codec, name, *, stop_at=None):
    """Feed a recorded fixture's inbound frames; the thread id is taken from thread/start."""
    out = []
    for line in (FIXTURES / name).read_text().splitlines():
        record = json.loads(line)
        if record["dir"] != "in":
            continue
        frame = record["frame"]
        if "result" in frame and isinstance(frame["result"], dict):
            thread = frame["result"].get("thread")
            if isinstance(thread, dict) and isinstance(codec, CodexCodec):
                codec.native_id = thread["id"]
            continue
        if frame.get("method") == "turn/started" and codec.operation_id is None:
            codec.operation_id = "11111111-1111-4111-8111-111111111111"
            codec.native_turn_id = frame["params"]["turn"]["id"]
        out.append((frame.get("method"), codec.feed(frame)))
        if stop_at and frame.get("method") == stop_at:
            break
    return out


def test_codex_reports_a_terminal_that_outlives_its_turn_until_its_late_completion():
    codec = CodexCodec()
    seen = replay(codec, "codex_background_terminal.jsonl", stop_at="turn/completed")
    method, events = seen[-1]
    assert method == "turn/completed"
    (bg,) = [e for e in events if e.kind == "background"]
    assert bg.data["task_state"] == "running" and bg.data["task_type"] == "background_terminal"
    assert bg.data["description"] == "/bin/bash -lc 'sleep 20; echo bg-done'"
    assert codec.terminals and codec.operation_id is None
    native_ipc.normalize_event({"kind": bg.kind, "data": bg.data})
    # The rest: output and `item/completed` ~19 s later, under the ENDED turn's id.
    codec2 = CodexCodec()
    seen = replay(codec2, "codex_background_terminal.jsonl")
    late = [events for method, events in seen if method == "item/completed"][-1]
    (ended,) = late
    assert ended.kind == "background" and ended.data["task_state"] == "completed"
    assert not codec2.terminals


def test_codex_reports_nothing_for_a_detached_job_the_process_floor_must_see():
    codec = CodexCodec()
    seen = replay(codec, "codex_detached_job.jsonl")
    assert not codec.terminals
    assert not [e for _, events in seen for e in events if e.data.get("task_id")]


def test_codex_terminals_do_not_hold_the_queue_but_keep_the_worker_alive(conversation):
    codec = CodexCodec()
    replay(codec, "codex_background_terminal.jsonl", stop_at="turn/completed")
    worker = _worker(conversation, codec)
    worker.floor = Floor("ok", (Process(900, 4121, "bash"),))
    assert not worker.dispatch_busy()  # an operator message still goes
    assert worker.busy()


def test_a_codex_terminal_no_process_can_run_is_retired(conversation):
    codec = CodexCodec()
    replay(codec, "codex_background_terminal.jsonl", stop_at="turn/completed")
    worker = _worker(conversation, codec)
    before = set(codec.terminals)
    worker.floor = Floor("ok", ())
    worker.after_floor(set())  # a terminal registered after the scan began is never retired
    assert codec.terminals
    worker.after_floor(before)
    assert not codec.terminals and not worker.busy()
    assert any(
        p["kind"] == "background" and p["data"]["task_state"] == "ended" for p in worker.pending
    )


def test_opencode_reports_nothing_for_a_backgrounded_command():
    """The ACP filters are unchanged (#1724): the measured `cmd &` frames carry no background
    observation at all; only the process floor keeps such a worker alive."""
    codec = OpencodeAcpCodec(
        "ses_e82db8eb4ffel4sNkGk3NTwzlb", agent="battlelab-api-0123456789abcdef"
    )
    codec.operation_id, codec.native_turn_id = ident(), "turn"
    out = []
    for line in (FIXTURES / "opencode_background_job.jsonl").read_text().splitlines():
        record = json.loads(line)
        frame = record["frame"]
        if record["dir"] == "in" and frame.get("method") == "session/update":
            with contextlib.suppress(native_protocol.ProtocolError):
                out += codec.feed(frame)
    assert not [e for e in out if e.kind == "background"]
    assert not getattr(codec, "_background", None) and not getattr(codec, "terminals", None)


# --- the worker: busy, inventory, settlement and the Stop recheck ---------------------------


def _worker(conversation, codec):
    root, _sid, binding = conversation
    worker = native_worker.Worker(
        {
            "worker_id": binding.worker_id,
            "session_key": binding.session_key,
            "adapter": binding.adapter,
            "journal_root": str(root),
            "capability": native_ipc.Capability.create()._value,
            "connection_id": binding.connection_id,
        },
        root,
    )
    worker.codec = codec
    worker.ready.set()
    # The freeze's seams (#1724): these pids are FAKE, so never a real signal. Signals are
    # recorded; a (pid, start) recheck answers from the scan the test serves.
    worker.sent = []
    worker._signal = lambda pid, sig: worker.sent.append((pid, sig))

    def recheck(pid):
        scan = native_processes.observe("unit", None)
        found = next((p for p in scan.processes if p.pid == pid), None)
        if found is None:
            return None
        # A pid whose last recorded signal was SIGSTOP reads as stopped, as the kernel shows it.
        last = next((sig for p, sig in reversed(worker.sent) if p == pid), None)
        return dataclasses.replace(found, state="T" if last == signal.SIGSTOP else "S")

    worker._recheck = recheck
    worker._members = lambda: {p.pid for p in native_processes.observe("unit", None).processes}
    return worker


@pytest.mark.parametrize("make", [CodexCodec, lambda: ClaudeCodec(SESSION)])
def test_busy_counts_the_process_floor_on_every_client(conversation, make):
    worker = _worker(conversation, make())
    worker.floor = Floor("ok", ())
    assert not worker.busy()
    worker.floor = Floor("ok", (Process(950, 40, "sleep"),))
    assert worker.busy() and not worker.dispatch_busy()
    worker.floor = Floor("unknown")
    assert worker.busy()
    worker.floor = Floor("uncontained")
    assert not worker.busy()


def test_busy_counts_the_process_floor_for_the_acp_clients(conversation):
    from agent_sessions.native_protocol import CopilotAcpCodec

    for codec in (
        OpencodeAcpCodec(None, agent="battlelab-api-0123456789abcdef"),
        CopilotAcpCodec(None),
    ):
        worker = _worker(conversation, codec)
        worker.floor = Floor("ok", (Process(950, 40, "node"),))
        assert worker.busy() and not worker.dispatch_busy()
        worker.floor = Floor("ok", ())
        assert not worker.busy()


def _events(root, sid, kind):
    return [
        e["event"]["data"]
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == kind
    ]


def _stop_everything(worker, binding, root, sid):
    request = journal_tests.request(
        binding,
        action="stop",
        revision=native_journal.read(root, sid).revision,
        confirm_everything=True,
    )
    return asyncio.run(worker.effect(native_ipc.validate_request(request)))


def _stop(worker, binding, root, sid, digest=None):
    params = {"confirm_digest": digest} if digest else {}
    request = journal_tests.request(
        binding, action="stop", revision=native_journal.read(root, sid).revision, **params
    )
    return asyncio.run(worker.effect(native_ipc.validate_request(request)))


def test_a_changed_set_refuses_the_stop_with_the_fresh_inventory_journaled(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    scans = iter([Scan("ok", frozenset({Process(100, 10, "python3"), Process(950, 40, "node")}))])
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: next(scans))
    worker.baseline = frozenset({(10, 100)})
    shown = native_ipc.stop_digest(binding.worker_id, (), (), "ok")  # the pane saw nothing
    response = _stop(worker, binding, root, sid, shown)
    assert response["error"]["reason"] == "inventory_changed"
    assert not worker.terminated  # nothing stopped
    assert not [o for o in native_journal.read(root, sid).operations.values()]  # nothing claimed
    (fresh,) = _events(root, sid, "background_inventory")
    assert [p["pid"] for p in fresh["processes"]] == [40] and fresh["processes"][0][
        "comm"
    ] == "node"
    assert not _events(root, sid, "background_settled")


def test_an_orderly_stop_rescans_and_settles_before_the_child_is_stopped(conversation, monkeypatch):
    """The process started after the last 30 s inventory is in the settlement: Stop rescans."""
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.baseline = frozenset({(10, 100)})
    worker.floor = Floor("ok", ())
    worker.record_inventory(worker.inventory())  # the last tick saw nothing
    late = Scan("ok", frozenset({Process(100, 10, "python3"), Process(990, 41, "make")}))
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: late)
    digest = native_ipc.stop_digest(binding.worker_id, (), [(41, 990)], "ok")
    response = _stop(worker, binding, root, sid, digest)
    assert "result" in response and worker.terminated
    (settled,) = _events(root, sid, "background_settled")
    assert settled["reason"] == "stop" and [p["pid"] for p in settled["processes"]] == [41]


def test_an_unreadable_scan_is_never_confirmed_exactly_only_by_stop_everything(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.baseline = frozenset({(10, 100)})
    worker.codec._background = {"task-1"}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: Scan("unknown"))
    # Even the matching digest of the unknown state is not an exact confirmation.
    unknown = native_ipc.stop_digest(binding.worker_id, ["task-1"], (), "unknown")
    assert _stop(worker, binding, root, sid, unknown)["error"]["reason"] == "inventory_changed"
    assert not worker.terminated
    # Stop everything goes, and the settlement says the scan was unreadable.
    response = _stop_everything(worker, binding, root, sid)
    assert "result" in response and worker.terminated
    (settled,) = _events(root, sid, "background_settled")
    assert settled["scan"] == "unknown" and settled["engine"] == ["task-1"]
    assert settled["processes"] == []


def test_an_unconfirmed_stop_still_settles_from_a_fresh_scan(conversation, monkeypatch):
    """Archive's Stop (no confirmation) keeps its containment path, and records what it ended."""
    root, sid, binding = conversation
    worker = _worker(conversation, CodexCodec())
    worker.baseline = frozenset()
    monkeypatch.setattr(
        native_processes, "observe", lambda *a, **k: Scan("ok", frozenset({Process(5, 7, "vite")}))
    )
    assert "result" in _stop(worker, binding, root, sid)
    (settled,) = _events(root, sid, "background_settled")
    assert settled["reason"] == "stop" and settled["processes"][0]["comm"] == "vite"


def test_engine_changes_are_journaled_with_their_tasks_and_nothing_twice(conversation):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.floor = Floor("ok", ())
    worker.record_inventory(worker.inventory())  # empty at readiness: nothing written
    asyncio.run(worker.handle_events(worker.codec.feed(_task("task_started", "t1"))))
    asyncio.run(worker.handle_events(worker.codec.feed(_task("task_progress", "t1"))))
    worker.flush()
    assert [d["engine"] for d in _events(root, sid, "background_inventory")] == [["t1"]]
    worker.after_floor(set())  # an idle tick with nothing changed writes nothing
    worker.flush()
    assert len(_events(root, sid, "background_inventory")) == 1


# --- settlement: what the projection derives for ended generations -------------------------


def _binding(binding, worker=None):
    return native_ipc.Binding(binding.session_key, worker or ident(), ident(), binding.adapter)


def _append(root, binding, kind, data, monkeypatch=None, ts=None):
    if ts is not None:
        monkeypatch.setattr(native_journal.time, "time", lambda: ts)
    native_journal.append_events(root, binding, [{"kind": kind, "data": data}])


def _settled(data, reason, **extra):
    return {
        **{k: data[k] for k in ("engine", "processes", "more", "scan")},
        "reason": reason,
        **extra,
    }


def _project(root, sid, live, ended=lambda w: None):
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": "claude-stream-json"},
        live,
        max_turns=50,
        ended_reason=ended,
    )
    return raw["native"]


def test_orderly_abrupt_and_idle_ends_project_as_rows_with_their_reasons(conversation):
    root, sid, binding = conversation
    stopped, crashed, idled = _binding(binding), _binding(binding), _binding(binding)
    work = inventory(engine=["t1"], processes=[(40, 950, "node")], worker=stopped.worker_id)
    _append(
        root,
        stopped,
        "background",
        {"task_id": "t1", "state": "running", "description": "Watch CI"},
    )
    _append(root, stopped, "background_inventory", work)
    _append(root, stopped, "background_settled", _settled(work, "stop"))
    crash = inventory(processes=[(41, 990, "vite")], worker=crashed.worker_id)
    _append(root, crashed, "background_inventory", crash)  # SIGKILL: no settlement
    idle = inventory(worker=idled.worker_id)
    _append(root, idled, "background_settled", _settled(idle, "idle", idle_for=1800))
    rows = _project(root, sid, None)["worker_ends"]
    assert [r["reason"] for r in rows] == ["stop", "worker_ended", "idle"]
    assert rows[0]["tasks"][0]["description"] == "Watch CI"
    assert rows[0]["tasks"][0]["state"] == rows[0]["processes"][0]["state"] == "ended_with_worker"
    assert rows[1]["processes"][0]["comm"] == "vite" and rows[1]["settled"] is False
    assert rows[2] == {**rows[2], "reason": "idle", "idle_for": 1800}
    # Idempotent: settling the same journal again yields the same rows, and writes nothing.
    revision = native_journal.read(root, sid).revision
    assert _project(root, sid, None)["worker_ends"] == rows
    assert native_journal.read(root, sid).revision == revision
    # Never argv or environment, only pid, start time and the executable name.
    assert set(rows[1]["processes"][0]) == {"pid", "start", "comm", "started_at", "source", "state"}


def test_reason_precedence_oom_over_stop_over_protocol_over_worker_ended(conversation):
    root, sid, binding = conversation
    gens = [_binding(binding) for _ in range(4)]
    for gen, reason in zip(gens, ["stop", "protocol", None, "stop"], strict=True):
        work = inventory(processes=[(40, 950, "node")], worker=gen.worker_id)
        _append(root, gen, "background_inventory", work)
        if reason:
            _append(root, gen, "background_settled", _settled(work, reason))
    oomed = {gens[3].worker_id}
    rows = _project(root, sid, None, lambda w: "killed: out of memory" if w in oomed else None)[
        "worker_ends"
    ]
    assert [r["reason"] for r in rows] == ["stop", "protocol", "worker_ended", "oom"]
    assert native_runtime.END_REASONS == ("oom", "stop", "protocol", "worker_ended")


def test_a_crash_between_inventory_and_settlement_settles_from_the_last_inventory(conversation):
    root, sid, binding = conversation
    gen = _binding(binding)
    first = inventory(processes=[(40, 950, "node")], worker=gen.worker_id)
    second = inventory(processes=[(40, 950, "node"), (41, 990, "esbuild")], worker=gen.worker_id)
    _append(root, gen, "background_inventory", first)
    _append(root, gen, "background_inventory", second)
    # ...and the worker was SIGKILLed before it could write `background_settled`.
    (row,) = _project(root, sid, None)["worker_ends"]
    assert row["reason"] == "worker_ended" and [p["pid"] for p in row["processes"]] == [40, 41]


def test_the_abrupt_end_gap_work_between_two_observations_is_never_claimed(conversation):
    """A process born after the last 30 s inventory and killed with the worker before the next
    was never recorded: nothing claims it ended with the worker (#1724, documented)."""
    root, sid, binding = conversation
    gen = _binding(binding)
    _append(
        root,
        gen,
        "background_inventory",
        inventory(processes=[(40, 950, "node")], worker=gen.worker_id),
    )
    # (pid 77 `cc1` started here, and the OOM kill took it before the next tick)
    (row,) = _project(root, sid, None, lambda w: "killed: out of memory")["worker_ends"]
    assert row["reason"] == "oom" and [p["pid"] for p in row["processes"]] == [40]
    assert "cc1" not in json.dumps(row)


def test_the_live_worker_projects_processes_and_its_stop_inventory(conversation):
    root, sid, binding = conversation
    work = inventory(engine=["t1"], processes=[(40, 950, "node")], worker=binding.worker_id)
    _append(
        root,
        binding,
        "background",
        {"task_id": "t1", "state": "running", "description": "Watch", "active": True},
    )
    _append(root, binding, "background_inventory", work)
    native = _project(root, sid, binding.worker_id)
    sources = [(t["source"], t["task_id"]) for t in native["background_tasks"]]
    assert sources == [("engine", "t1"), ("process", "pid:40:950")]
    process = native["background_tasks"][1]
    assert process["comm"] == "node" and process["pid"] == 40 and "argv" not in process
    stop = native["stop_inventory"]
    assert stop["worker"] == binding.worker_id and stop["digest"] == work["digest"]
    assert stop["tasks"][0]["description"] == "Watch" and stop["processes"][0]["pid"] == 40
    assert native["worker_ends"] == []  # the live generation has not ended
    # Before any inventory: the empty set's digest, which the worker's own matches.
    empty = _project(root, sid, ident())["stop_inventory"]
    assert empty["digest"] == native_ipc.stop_digest(empty["worker"], (), (), "ok")


def test_a_public_share_never_sees_processes_or_inventories(conversation):
    root, sid, binding = conversation
    work = inventory(processes=[(40, 950, "node")], worker=binding.worker_id)
    _append(root, binding, "background_inventory", work)
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": "claude-stream-json"},
        binding.worker_id,
        max_turns=50,
    )
    snap = structured_runtime._snapshot(binding.session_key, raw)
    assert snap["native"]["background_tasks"][0]["comm"] == "node"  # the operator sees it
    public = json.dumps(share_api.project(snap, {}, share_api._Redactor([]), "link"))
    assert "node" not in public and "stop_inventory" not in public and "pid" not in public


# --- the real Stop boundary: route → runtime → worker (FakeHost, the scripted Claude) ----------


async def _with_task(work) -> str:
    (work / "emit-lifecycle").write_text("")
    key = (await runtime.create_session(ENGINES["claude"], str(work), operation_id=ident()))[
        "session_key"
    ]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="BGTASK")
    await settle(key, first)
    for _ in range(200):
        native = (await runtime.snapshot(key))["native"]
        if native["stop_inventory"]["tasks"]:
            return key
        await asyncio.sleep(0.05)
    raise AssertionError("the background task never reached the inventory")


def _running(fake, key) -> bool:
    worker = native_state.read_session(key.partition(":")[2])["current_worker"]
    return fake._alive(native_containment.WorkerIdentity(worker).unit)


@pytest.mark.anyio
async def test_a_stale_stop_refuses_with_the_inventory_and_stops_nothing_until_reconfirmed(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key = await _with_task(project)
    sid = key.partition(":")[2]
    worker = native_state.read_session(sid)["current_worker"]
    # An older page confirms "nothing runs" (no body), and a pane that saw an empty set.
    for confirm in ({}, {"worker": worker, "digest": native_ipc.stop_digest(worker, (), (), "ok")}):
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.operator_stop(key, **confirm)
        assert refused.value.status == 409 and refused.value.reason == "inventory_changed"
        inventory_now = refused.value.inventory
        assert [t["task_id"] for t in inventory_now["tasks"]] == ["bg1"]
        assert _running(host, key)  # the unit was never stopped
        assert native_state.read_session(sid)["closed"] is False  # nor the session closed
    # Another generation's confirmation is refused the same way.
    with pytest.raises(runtime.StructuredError):
        await runtime.operator_stop(key, worker=ident(), digest=inventory_now["digest"])
    assert _running(host, key)
    # The refreshed inventory, confirmed: it stops, and the task ended with its worker.
    stopped = await runtime.operator_stop(
        key, worker=inventory_now["worker"], digest=inventory_now["digest"]
    )
    assert stopped["containment"] == "gone"
    (row,) = (await runtime.snapshot(key))["native"]["worker_ends"]
    assert row["reason"] == "stop" and [t["task_id"] for t in row["tasks"]] == ["bg1"]
    (project / "release-bg-task").write_text("")


def _wedged(monkeypatch, real_call):
    """The worker is alive but cannot answer a stop (its effect lock held, its loop stuck)."""

    async def failing(gen, action, params, **kwargs):
        if action == "stop":
            silent = native_runtime.NativeError(503, "the native worker did not answer")
            silent.transport = True  # as `_call` raises a timeout: no answer at all
            raise silent
        return await real_call(gen, action, params, **kwargs)

    monkeypatch.setattr(native_runtime, "_call", failing)


@pytest.mark.anyio
async def test_a_worker_call_error_during_the_check_refuses_and_never_falls_through(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    key = await _with_task(project)
    sid = key.partition(":")[2]
    called = []
    real_call, real_stop_sync = native_runtime._call, native_runtime._stop_sync
    _wedged(monkeypatch, real_call)
    monkeypatch.setattr(native_runtime, "_stop_sync", lambda *a, **k: called.append(a) or "gone")
    inventory_now = (await runtime.snapshot(key))["native"]["stop_inventory"]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.operator_stop(
            key, worker=inventory_now["worker"], digest=inventory_now["digest"]
        )
    # A refusal with the escape hatch: nothing stopped, the session not closed, and the
    # generation's last known work offered for an explicit Stop anyway.
    assert refused.value.status == 503 and refused.value.reason == "worker_unreachable"
    hatch = refused.value.inventory
    assert hatch["reach"] == "unreachable" and hatch["worker"] == inventory_now["worker"]
    assert [t["task_id"] for t in hatch["tasks"]] == ["bg1"]
    assert not called and _running(host, key)
    assert native_state.read_session(sid)["closed"] is False
    monkeypatch.setattr(native_runtime, "_call", real_call)
    monkeypatch.setattr(native_runtime, "_stop_sync", real_stop_sync)
    (project / "release-bg-task").write_text("")
    await runtime.stop(key)


@pytest.mark.anyio
async def test_stop_anyway_for_a_wedged_worker_stops_it_and_settles_its_work_as_stopped(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    key = await _with_task(project)
    sid = key.partition(":")[2]
    worker = native_state.read_session(sid)["current_worker"]
    _wedged(monkeypatch, native_runtime._call)
    # The unit stop SIGKILLs it: a wedged worker settles nothing itself, the runtime does.
    monkeypatch.setattr(host, "stop", lambda argv: host.kill(worker) or True)
    stopped = await runtime.operator_stop(key, worker=worker, everything=True)
    assert stopped["containment"] == "gone" and not _running(host, key)
    (row,) = (await runtime.snapshot(key))["native"]["worker_ends"]
    assert row["reason"] == "stop" and row["settled"] is True
    assert [t["task_id"] for t in row["tasks"]] == ["bg1"]
    root = native_runtime._journal_root(native_runtime._api_provider(ENGINES["claude"]))
    settled = [
        e
        for e in native_journal.read(root, sid).events
        if e["event"]["kind"] == "background_settled" and e["worker_id"] == worker
    ]
    assert len(settled) == 1


@pytest.mark.anyio
async def test_stop_everything_is_refused_for_another_generation_and_stops_its_own(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    key = await _with_task(project)
    sid = key.partition(":")[2]
    worker = native_state.read_session(sid)["current_worker"]
    called = []
    real_stop_sync = native_runtime._stop_sync
    monkeypatch.setattr(native_runtime, "_stop_sync", lambda *a, **k: called.append(a) or "gone")
    # Offered for a generation that is not the one running now: refused, nothing stopped.
    with pytest.raises(runtime.StructuredError) as stale:
        await runtime.operator_stop(key, worker=ident(), everything=True)
    assert stale.value.status == 409 and stale.value.reason == "inventory_changed"
    assert not called and _running(host, key)
    assert native_state.read_session(sid)["closed"] is False
    monkeypatch.setattr(native_runtime, "_stop_sync", real_stop_sync)
    # For its own generation, a reachable worker accepts it, settles and stops.
    stopped = await runtime.operator_stop(key, worker=worker, everything=True)
    assert stopped["containment"] == "gone"
    (row,) = (await runtime.snapshot(key))["native"]["worker_ends"]
    assert row["reason"] == "stop" and row["tasks"][0]["task_id"] == "bg1"
    (project / "release-bg-task").write_text("")


@pytest.mark.anyio
async def test_archive_keeps_its_own_unconfirmed_containment_path(host, project):  # noqa: F811
    key = await _with_task(project)
    assert (await runtime.stop(key))["containment"] == "gone"  # runtime_cleanup's call
    (row,) = (await runtime.snapshot(key))["native"]["worker_ends"]
    assert row["reason"] == "stop" and row["tasks"][0]["task_id"] == "bg1"


def test_the_stop_route_carries_the_confirmation_and_the_refreshed_inventory(
    host,  # noqa: F811
    project,  # noqa: F811
    auth_cfg,
):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    (project / "emit-lifecycle").write_text("")
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
        turn = ident()
        c.post(
            f"/api/structured/sessions/{key}/turns", json={"operation_id": turn, "text": "BGTASK"}
        )
        for _ in range(400):
            snap = c.get(f"/api/structured/sessions/{key}").json()
            if snap["native"]["stop_inventory"]["tasks"]:
                break
            time.sleep(0.05)
        stop = f"/api/structured/sessions/{key}/stop"
        for bad in (
            {"worker": "x"},
            {"worker": ident(), "digest": "nothex"},
            {"extra": 1},
            {"worker": ident(), "everything": False},
            {"worker": ident(), "everything": "yes"},
            {"worker": ident(), "digest": "a" * 64, "everything": True},
            {"everything": True},
            {"worker": ident(), "unreachable": True},
        ):
            assert c.post(stop, json=bad).status_code == 422
        refused = c.post(stop)  # an older page: confirms that nothing runs
        assert refused.status_code == 409, refused.text
        payload = refused.json()
        assert payload["reason"] == "inventory_changed"
        assert [t["task_id"] for t in payload["inventory"]["tasks"]] == ["bg1"]
        assert c.get(f"/api/structured/sessions/{key}/containment").json()["containment"] == "live"
        confirmed = {
            "worker": payload["inventory"]["worker"],
            "digest": payload["inventory"]["digest"],
        }
        done = c.post(stop, json=confirmed)
        assert done.status_code == 200 and done.json()["containment"] == "gone"
    (project / "release-bg-task").write_text("")


def test_the_stop_route_offers_stop_everything_for_a_worker_it_cannot_reach(
    host,  # noqa: F811
    project,  # noqa: F811
    auth_cfg,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    (project / "emit-lifecycle").write_text("")
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        csrf = c.get("/api/config").json()["csrf"]
        c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
        body = {"engine": ENGINES["claude"], "cwd": str(project), "operation_id": ident()}
        key = c.post("/api/structured/sessions", json=body).json()["session_key"]
        c.post(
            f"/api/structured/sessions/{key}/turns",
            json={"operation_id": ident(), "text": "BGTASK"},
        )
        for _ in range(400):
            snap = c.get(f"/api/structured/sessions/{key}").json()
            if snap["native"]["stop_inventory"]["tasks"]:
                break
            time.sleep(0.05)
        shown = snap["native"]["stop_inventory"]
        _wedged(monkeypatch, native_runtime._call)
        monkeypatch.setattr(host, "stop", lambda argv: host.kill(shown["worker"]) or True)
        stop = f"/api/structured/sessions/{key}/stop"
        refused = c.post(stop, json={"worker": shown["worker"], "digest": shown["digest"]})
        assert refused.status_code == 503, refused.text
        payload = refused.json()
        assert payload["reason"] == "worker_unreachable"
        assert payload["inventory"]["reach"] == "unreachable"
        assert payload["inventory"]["worker"] == shown["worker"]
        live = c.get(f"/api/structured/sessions/{key}/containment").json()["containment"]
        assert live == "live"  # refused: nothing stopped
        anyway = c.post(stop, json={"worker": shown["worker"], "everything": True})
        assert anyway.status_code == 200 and anyway.json()["containment"] == "gone"
        (row,) = c.get(f"/api/structured/sessions/{key}").json()["native"]["worker_ends"]
        assert row["reason"] == "stop"


# --- review of PR #1757: churn, a lost baseline, journal growth, a double Stop ---------------

TCK = __import__("os").sysconf("SC_CLK_TCK")


def _scan(*processes, observed):
    return Scan("ok", frozenset({Process(100, 10, "python3"), *processes}), observed)


def _served(worker, *processes, observed):
    """The worker served an inventory of ``processes`` observed at tick ``observed``."""
    worker.baseline = frozenset({(10, 100)})
    worker.floor = native_processes.floor(_scan(*processes, observed=observed), worker.baseline)
    data = worker.inventory()
    worker.record_inventory(data, force=True)
    return data["digest"]


def test_an_exact_stop_refuses_any_change_and_stop_everything_ends_it(conversation, monkeypatch):
    """No churn heuristic (operator decision on PR #1757): a shown `sh` that starts a long-lived
    `node` after the list is a change, refused exactly; Stop everything, disclosed as ending
    work not listed, ends it."""
    root, sid, binding = conversation
    shell, server = Process(900, 41, "sh"), Process(8990, 47, "node", ppid=41)
    readings = {}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    readings["scan"] = _scan(shell, server, observed=9000)
    assert _stop(worker, binding, root, sid, shown)["error"]["reason"] == "inventory_changed"
    assert not worker.terminated
    (fresh,) = _events(root, sid, "background_inventory")[-1:]
    assert [p["pid"] for p in fresh["processes"]] == [41, 47]  # the refreshed, complete list
    # A loop's young child, an ended process, a new agent task: each a change, refused.
    for now, engine in (
        ((shell, Process(8999, 48, "sleep", ppid=41)), None),
        ((), None),
        ((shell,), ["t9"]),
    ):
        again = _worker(conversation, ClaudeCodec(SESSION))
        digest = _served(again, shell, observed=1000)
        if engine:
            again.codec._background = set(engine)
        readings["scan"] = _scan(*now, observed=9000)
        assert _stop(again, binding, root, sid, digest)["error"]["reason"] == "inventory_changed"
    # Stop everything: the server is ended with the worker, and the settlement lists it.
    readings["scan"] = _scan(shell, server, observed=9000)
    assert "result" in _stop_everything(worker, binding, root, sid) and worker.terminated
    settled = _events(root, sid, "background_settled")[-1]
    assert settled["reason"] == "stop" and [p["pid"] for p in settled["processes"]] == [41, 47]


def test_an_inventory_over_the_display_limits_is_never_confirmed_exactly(conversation, monkeypatch):
    """33 processes (one past the 32 listed) or 129 tasks (one past 128): the dialog cannot name
    them all, so even the matching digest is refused; only Stop everything ends them."""
    root, sid, binding = conversation
    many = [Process(500 + i, 100 + i, "worker") for i in range(33)]
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: _scan(*many, observed=9000))
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.baseline = frozenset({(10, 100)})
    worker.observe_floor()
    over = worker.inventory()
    assert len(over["processes"]) == 32 and over["more"] == 1
    assert (
        _stop(worker, binding, root, sid, over["digest"])["error"]["reason"] == "inventory_changed"
    )
    assert "result" in _stop_everything(worker, binding, root, sid)
    # 129 tasks: 128 listed, the overflow counted.
    tasks = _worker(conversation, ClaudeCodec(SESSION))
    tasks.baseline = frozenset({(10, 100)})
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: _scan(observed=9000))
    tasks.observe_floor()
    tasks.codec._background = {f"t{i:03d}" for i in range(129)}
    data = tasks.inventory()
    assert len(data["engine"]) == 128 and data["engine_more"] == 1
    native_ipc.normalize_event({"kind": "background_inventory", "data": data})
    assert (
        _stop(tasks, binding, root, sid, data["digest"])["error"]["reason"] == "inventory_changed"
    )
    assert "result" in _stop_everything(tasks, binding, root, sid)


def test_a_second_stop_while_the_first_is_under_way_joins_it(conversation, monkeypatch):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, Process(900, 41, "sh"), observed=1000)
    monkeypatch.setattr(
        native_processes, "observe", lambda *a, **k: _scan(Process(900, 41, "sh"), observed=2000)
    )
    assert "result" in _stop(worker, binding, root, sid, shown)
    # The second tab's confirmation is stale (its process is being killed): no false "changed".
    monkeypatch.setattr(
        native_processes, "observe", lambda *a, **k: _scan(Process(1999, 77, "x"), observed=2100)
    )
    assert "result" in _stop(worker, binding, root, sid, "f" * 64)
    assert len(_events(root, sid, "background_settled")) == 1


@pytest.mark.anyio
async def test_a_double_stop_through_the_runtime_never_answers_changed(
    host,  # noqa: F811
    project,  # noqa: F811
):
    key = await _with_task(project)
    shown = (await runtime.snapshot(key))["native"]["stop_inventory"]
    first, second = await asyncio.gather(
        runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"]),
        runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"]),
        return_exceptions=True,
    )
    assert first["containment"] == "gone", first
    assert not isinstance(second, Exception), second
    assert second["containment"] == "gone"
    (project / "release-bg-task").write_text("")


def test_a_lost_baseline_is_retried_until_the_first_turn_and_never_after(conversation, monkeypatch):
    readings = iter(
        [Scan("unknown")] * native_worker.BASELINE_TRIES
        + [_scan(observed=1000), _scan(Process(1500, 41, "node"), observed=2000)]
    )
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: next(readings))
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.take_baseline()
    assert worker.floor.state == "unknown" and worker.busy()
    worker.observe_floor()  # an idle tick, no turn yet: retried
    assert worker.baseline == frozenset({(10, 100)}) and not worker.busy()
    worker.observe_floor()
    assert [p.pid for p in worker.floor.processes] == [41]
    # After a turn, a missing baseline stays missing: it could swallow the agent's own work.
    later = _worker(conversation, ClaudeCodec(SESSION))
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: Scan("unknown"))
    later.take_baseline()
    asyncio.run(later.handle_events(later.codec.feed(_task("task_started", "t1"))))
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: _scan(observed=1000))
    later.observe_floor()
    assert later.baseline is None and later.floor.state == "unknown" and later.busy()


def test_journal_growth_is_bounded_under_process_churn(conversation, monkeypatch):
    """Production cadence (tick 30 s, interval 5 min): an hour of a rotating set of long-lived
    processes writes at most 13 inventories; a loop's short-lived children write none."""
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    assert worker.tick == 30.0 and worker.inventory_interval == 300.0
    worker.baseline = frozenset({(10, 100)})
    clock = {"now": 10_000.0}
    monkeypatch.setattr(native_worker.time, "monotonic", lambda: clock["now"])
    span = 30 * TCK
    for tick in range(120):  # one hour of idle ticks
        clock["now"] += 30
        now = 10**7 + tick * span
        rotating = Process(now - 2 * span, 1000 + tick, "build")  # aged: lived two ticks
        children = [Process(now - 5, 5000 + tick, "sleep")]  # a loop's young child
        worker.floor = native_processes.floor(
            _scan(rotating, *children, observed=now), worker.baseline
        )
        worker.after_floor(set())
        assert worker.busy()
    worker.flush()
    written = _events(root, sid, "background_inventory")
    assert 1 <= len(written) <= 13
    assert all(p["comm"] == "build" for d in written for p in d["processes"])
    # Only young children churn: nothing to journal after the first quiet inventory.
    quiet = _worker(conversation, ClaudeCodec(SESSION))
    quiet.baseline = frozenset({(10, 100)})
    before = len(_events(root, sid, "background_inventory"))
    for tick in range(120):
        clock["now"] += 30
        now = 2 * 10**7 + tick * span
        quiet.floor = native_processes.floor(
            _scan(Process(now - 5, 9000 + tick, "sleep"), observed=now), quiet.baseline
        )
        quiet.after_floor(set())
        assert quiet.busy()
    quiet.flush()
    assert len(_events(root, sid, "background_inventory")) == before


def test_a_flapping_scan_is_bounded_like_any_process_change(conversation, monkeypatch):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.baseline = frozenset({(10, 100)})
    clock = {"now": 10_000.0}
    monkeypatch.setattr(native_worker.time, "monotonic", lambda: clock["now"])
    for tick in range(120):  # an hour, the scan readable one tick and not the next
        clock["now"] += 30
        worker.floor = (
            Floor("unknown")
            if tick % 2
            else native_processes.floor(_scan(observed=10**7), worker.baseline)
        )
        worker.after_floor(set())
    worker.flush()
    assert len(_events(root, sid, "background_inventory")) <= 13


# --- Hermes on PR #1757: a worker's answer never authorizes Stop anyway; the stop boundary ------


@pytest.mark.anyio
async def test_stop_everything_never_stops_a_worker_that_answered_with_a_refusal(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    key = await _with_task(project)
    sid = key.partition(":")[2]
    worker = native_state.read_session(sid)["current_worker"]
    real_call = native_runtime._call

    async def answering(gen, action, params, **kwargs):
        if action == "stop":
            # Reached the worker, which refused (a journal-revision race): not "no answer".
            raise native_runtime.NativeError(
                409, "native conversation revision changed", "conflict"
            )
        return await real_call(gen, action, params, **kwargs)

    monkeypatch.setattr(native_runtime, "_call", answering)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.operator_stop(key, worker=worker, everything=True)
    assert refused.value.status == 409 and refused.value.reason is None  # its own refusal
    assert _running(host, key) and native_state.read_session(sid)["closed"] is False
    # The first check too: a worker that answered offers no Stop anyway, only its refusal.
    shown = (await runtime.snapshot(key))["native"]["stop_inventory"]
    with pytest.raises(runtime.StructuredError) as answered:
        await runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"])
    assert answered.value.status == 409 and answered.value.reason is None
    assert _running(host, key) and native_state.read_session(sid)["closed"] is False
    monkeypatch.setattr(native_runtime, "_call", real_call)
    (project / "release-bg-task").write_text("")
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_successor_started_before_the_stop_boundary_is_never_stopped(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """The confirmed worker accepts and exits; a send launches a successor before the Stop
    reaches its lifecycle-locked close. Nothing of the successor was confirmed: 409, and the
    successor and the session stay live (Hermes on PR #1757)."""
    key = await _with_task(project)
    sid = key.partition(":")[2]
    old = native_state.read_session(sid)["current_worker"]
    old_unit = native_containment.WorkerIdentity(old).unit
    real_confirm = native_runtime._confirm_stop
    successor = {}

    async def then_a_successor(engine_id, session_id, gen, confirm):
        accepted = await real_confirm(engine_id, session_id, gen, confirm)
        for _ in range(400):  # the accepted worker stops its child and exits
            if not host._alive(old_unit):
                break
            await asyncio.sleep(0.05)
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="after the stop")
        await settle(key, turn)
        successor["worker"] = native_state.read_session(sid)["current_worker"]
        return accepted

    monkeypatch.setattr(native_runtime, "_confirm_stop", then_a_successor)
    shown = (await runtime.snapshot(key))["native"]["stop_inventory"]
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"])
    assert refused.value.status == 409 and refused.value.reason == "inventory_changed"
    assert refused.value.inventory["worker"] == successor["worker"] != old
    assert _running(host, key)  # the successor's unit was never stopped
    record = native_state.read_session(sid)
    assert record["closed"] is False and record["current_worker"] == successor["worker"]
    monkeypatch.setattr(native_runtime, "_confirm_stop", real_confirm)
    await runtime.stop(key)


def test_a_stop_claims_at_the_current_revision_while_the_agent_keeps_writing(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    worker = _worker(conversation, ClaudeCodec(SESSION))
    worker.floor = Floor("ok", ())
    worker.baseline = frozenset()
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: Scan("ok", observed=5))
    stale = native_journal.read(root, sid).revision
    _append(
        root, binding, "background", {"task_id": "t0", "state": "completed"}
    )  # output meanwhile
    request = journal_tests.request(binding, action="stop", revision=stale)
    response = asyncio.run(worker.effect(native_ipc.validate_request(request)))
    assert "result" in response and worker.terminated


# --- Hermes on PR #1757 head a75fd9ad --------------------------------------------------------


def test_a_refused_stop_claim_publishes_no_acceptance(conversation, monkeypatch):
    """A Stop whose claim is refused (or whose revision retries run out) stopped nothing: no
    `stop_accepted_at`, and a later Stop still needs a valid confirmation."""
    root, sid, binding = conversation
    real_claim = native_journal.claim
    for failure in ("busy", "exhausted"):
        worker = _worker(conversation, ClaudeCodec(SESSION))
        shown = _served(worker, Process(900, 41, "sh"), observed=1000)
        monkeypatch.setattr(
            native_processes,
            "observe",
            lambda *a, **k: _scan(Process(900, 41, "sh"), observed=2000),
        )
        calls = []

        def refuse(*args, failure=failure, calls=calls, **kwargs):
            calls.append(1)
            if failure == "busy":
                raise native_journal.JournalError("busy", "the journal is busy")
            raise native_journal.JournalError("conflict", native_journal.REVISION_CHANGED)

        monkeypatch.setattr(native_journal, "claim", refuse)
        response = _stop(worker, binding, root, sid, shown)
        assert "error" in response and not worker.terminated
        if failure == "exhausted":
            assert len(calls) == native_worker.STOP_CLAIM_TRIES
        assert worker.stop_accepted is False
        life = native_state.read_lifecycle(binding.worker_id)
        assert not life.get("stop_accepted_at")
        monkeypatch.setattr(native_journal, "claim", real_claim)
        # A later Stop with a stale confirmation is still checked, never waved through.
        assert _stop(worker, binding, root, sid, "e" * 64)["error"]["reason"] == "inventory_changed"


@pytest.mark.anyio
async def test_a_repeated_stop_never_reaches_a_successor(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """First Stop accepted, its worker exits, a send starts a successor, then the second Stop
    (the same confirmation, a double click or another tab): the successor is untouched."""
    key = await _with_task(project)
    sid = key.partition(":")[2]
    old = native_state.read_session(sid)["current_worker"]
    shown = (await runtime.snapshot(key))["native"]["stop_inventory"]
    gen = native_runtime._generation(sid)
    # The first Stop's worker half: accepted, and the old worker exits.
    assert await native_runtime._confirm_stop(
        ENGINES["claude"], sid, gen, {"worker": shown["worker"], "digest": shown["digest"]}
    )
    assert native_state.read_lifecycle(old).get("stop_accepted_at")
    for _ in range(400):
        if not host._alive(native_containment.WorkerIdentity(old).unit):
            break
        await asyncio.sleep(0.05)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="after the first stop")
    await settle(key, turn)
    successor = native_state.read_session(sid)["current_worker"]
    assert successor != old
    # The second Stop read its generation BEFORE the successor took over (the gap Hermes found):
    # the old, already-accepted one. Only the lifecycle-locked close can still tell.
    real_generation = native_runtime._generation
    monkeypatch.setattr(native_runtime, "_generation", lambda session_id: gen)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"])
    monkeypatch.setattr(native_runtime, "_generation", real_generation)
    assert refused.value.status == 409 and refused.value.reason == "inventory_changed"
    assert _running(host, key)
    record = native_state.read_session(sid)
    assert record["closed"] is False and record["current_worker"] == successor
    await runtime.stop(key)


# --- Hermes on PR #1757 head 95e7885f: a fork while the Stop's claim fsyncs ------------------


def test_a_child_forked_during_the_claim_is_settled_as_started_while_stopping(
    conversation, monkeypatch
):
    """The exact confirmation names the shell (41); while the claim awaits, the shell forks a
    long-lived child (42). The Stop goes (the dialog said anything these jobs start also ends),
    and the settlement is accurate: both, 42 marked as started during the stop."""
    root, sid, binding = conversation
    shell, child = Process(900, 41, "sh"), Process(8990, 42, "node", ppid=41)
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    real_claim = native_journal.claim

    def forking(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        readings["scan"] = _scan(shell, child, observed=9001)  # the fork, mid-claim
        return result

    monkeypatch.setattr(native_journal, "claim", forking)
    assert "result" in _stop(worker, binding, root, sid, shown) and worker.terminated
    (settled,) = _events(root, sid, "background_settled")
    assert [(p["pid"], p.get("started_during_stop")) for p in settled["processes"]] == [
        (41, None),
        (42, True),
    ]
    # The ended-with-worker rows carry it, for the pane's "started while stopping".
    (row,) = _project(root, sid, None)["worker_ends"]
    assert [p.get("started_during_stop") for p in row["processes"]] == [None, True]


def test_an_unreadable_scan_during_the_freeze_refuses_the_exact_stop(conversation, monkeypatch):
    """Fail closed: an exact Stop never goes ahead blind. Stop everything still settles
    `unknown`, never a list it cannot vouch for."""
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    real_claim = native_journal.claim

    def unreadable_after(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        readings["scan"] = Scan("unknown")
        return result

    monkeypatch.setattr(native_journal, "claim", unreadable_after)
    assert _stop(worker, binding, root, sid, shown)["error"]["reason"] == "inventory_changed"
    assert not worker.terminated and not _events(root, sid, "background_settled")
    (aborted,) = _events(root, sid, "stop_aborted")
    assert aborted["reason"] == "unstable"
    monkeypatch.setattr(native_journal, "claim", real_claim)
    assert "result" in _stop_everything(worker, binding, root, sid)
    (settled,) = _events(root, sid, "background_settled")
    assert settled["scan"] == "unknown" and settled["processes"] == []


def test_started_during_stop_is_a_settlement_fact_only():
    good = inventory(processes=[(42, 8990, "node")])
    flagged = {**good["processes"][0], "started_during_stop": True}
    settled = {"engine": [], "processes": [flagged], "more": 0, "scan": "ok", "reason": "stop"}
    assert native_ipc.normalize_event({"kind": "background_settled", "data": settled})
    for kind, data in (
        ("background_inventory", {**good, "processes": [flagged]}),
        (
            "background_settled",
            {**settled, "processes": [{**flagged, "started_during_stop": False}]},
        ),
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc.normalize_event({"kind": kind, "data": data})


# --- Hermes on PR #1757 head 1df12060: freeze, then revalidate ---------------------------------


def _claim_then(monkeypatch, readings, scan):
    real_claim = native_journal.claim

    def claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        readings["scan"] = scan
        return result

    monkeypatch.setattr(native_journal, "claim", claim)
    return real_claim


def test_an_independent_process_born_during_the_claim_refuses_the_exact_stop(
    conversation, monkeypatch
):
    """Hermes' probe: the dialog showed `sh` (41); during the claim the CLI (10, baseline)
    starts an independent `node` (42). The frozen set is not what was confirmed: 409, every
    stopped process resumed, `stop_aborted` journaled, the session open — and a later Stop of
    the refreshed list, or Stop everything, still works."""
    root, sid, binding = conversation
    shell, node = Process(900, 41, "sh"), Process(8990, 42, "node", ppid=10)
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    real_claim = _claim_then(monkeypatch, readings, _scan(shell, node, observed=9001))
    response = _stop(worker, binding, root, sid, shown)
    assert response["error"]["reason"] == "inventory_changed"
    assert not worker.terminated and not worker.stop_accepted
    assert not native_state.read_lifecycle(binding.worker_id).get("stop_accepted_at")
    stopped = sorted(pid for pid, sig in worker.sent if sig == signal.SIGSTOP)
    resumed = sorted(pid for pid, sig in worker.sent if sig == signal.SIGCONT)
    assert stopped == resumed == [10, 41, 42]  # frozen, then every one resumed
    assert not any(sig == signal.SIGKILL for _, sig in worker.sent)
    (aborted,) = _events(root, sid, "stop_aborted")
    assert aborted["reason"] == "changed"
    claim = next(o for o in native_journal.read(root, sid).operations.values())
    assert claim.request["action"] == "stop" and claim.handoff == "not_sent"
    fresh = _events(root, sid, "background_inventory")[-1]
    assert [p["pid"] for p in fresh["processes"]] == [41, 42]  # the dialog's next list
    assert not _events(root, sid, "background_settled")
    # The refreshed, complete list is confirmed exactly, and the Stop goes.
    monkeypatch.setattr(native_journal, "claim", real_claim)
    worker.sent.clear()
    assert "result" in _stop(worker, binding, root, sid, fresh["digest"]) and worker.terminated
    assert sorted(pid for pid, sig in worker.sent if sig == signal.SIGKILL) == [10, 41, 42]
    # Stop everything, from the aborted state, works too.
    again = _worker(conversation, ClaudeCodec(SESSION))
    digest = _served(again, shell, observed=1000)
    readings["scan"] = _scan(shell, observed=9000)
    _claim_then(monkeypatch, readings, _scan(shell, node, observed=9001))
    assert "error" in _stop(again, binding, root, sid, digest)
    assert "result" in _stop_everything(again, binding, root, sid)


def test_a_listed_jobs_child_born_during_the_claim_is_disclosed_and_stopped(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    shell, child = Process(900, 41, "sh"), Process(8990, 42, "node", ppid=41)
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    _claim_then(monkeypatch, readings, _scan(shell, child, observed=9001))
    assert "result" in _stop(worker, binding, root, sid, shown) and worker.terminated
    (settled,) = _events(root, sid, "background_settled")
    assert [(p["pid"], p.get("started_during_stop")) for p in settled["processes"]] == [
        (41, None),
        (42, True),
    ]
    signals = [(pid, sig) for pid, sig in worker.sent]
    assert sorted(pid for pid, sig in signals if sig == signal.SIGSTOP) == [10, 41, 42]
    assert sorted(pid for pid, sig in signals if sig == signal.SIGKILL) == [10, 41, 42]
    assert not _events(root, sid, "stop_aborted")


def test_a_fork_storm_that_never_stabilises_is_a_change_and_everything_resumes(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    readings = {"scan": _scan(shell, observed=9000)}
    born = iter(range(1000, 2000))

    def storming(*a, **k):
        # Only the worker's own scans see a new birth; the test seam's rechecks just read.
        if readings.get("storm") and a[:1] != ("unit",):
            pid = next(born)
            readings["scan"] = Scan(
                "ok",
                readings["scan"].processes | {Process(8000 + pid, pid, "sh", ppid=10)},
                9001,
            )
        return readings["scan"]

    monkeypatch.setattr(native_processes, "observe", storming)
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    real_claim = native_journal.claim

    def claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        readings["storm"] = True
        return result

    monkeypatch.setattr(native_journal, "claim", claim)
    assert _stop(worker, binding, root, sid, shown)["error"]["reason"] == "inventory_changed"
    stopped = sorted(pid for pid, sig in worker.sent if sig == signal.SIGSTOP)
    resumed = sorted(pid for pid, sig in worker.sent if sig == signal.SIGCONT)
    assert stopped and stopped == resumed  # all resumed
    assert _events(root, sid, "stop_aborted")[0]["reason"] == "unstable"
    assert not worker.terminated


def test_the_freeze_never_signals_pid_1_or_the_worker_itself(conversation, monkeypatch):
    import os

    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    me = Process(5, os.getpid(), "python3")
    init = Process(1, 1, "systemd")
    readings["scan"] = Scan("ok", readings["scan"].processes | {me, init}, 9000)
    worker.baseline = frozenset({(10, 100), me.identity, init.identity})
    _stop(worker, binding, root, sid, shown)
    assert {pid for pid, _ in worker.sent}.isdisjoint({1, os.getpid(), os.getpgid(0)})


# --- Hermes on PR #1757 head b8c12a7a: confirmed freezing, a guaranteed resume ----------------


def _signals(worker, sig):
    return sorted({pid for pid, s in worker.sent if s == sig})


@pytest.mark.parametrize("failure", ["signal", "recheck"])
def test_a_process_that_cannot_be_confirmed_stopped_fails_the_exact_stop_closed(
    conversation, monkeypatch, failure
):
    """The baseline CLI (10) refuses SIGSTOP: it is never confirmed stopped, so the freeze is
    `unstable` and the exact Stop refuses — never accepted with a live CLI that could still
    start a job before the unit stop. Nothing is killed; what was stopped is resumed."""
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    record, reading = worker._signal, worker._recheck

    def refusing(pid, sig):
        if pid == 10 and sig == signal.SIGSTOP:
            raise PermissionError(1, "Operation not permitted")
        record(pid, sig)

    def unreadable(pid):
        if pid == 10:
            raise native_processes.ScanError("EIO")  # Hermes' probe: the CLI's recheck fails
        return reading(pid)

    if failure == "signal":
        worker._signal = refusing
    else:
        worker._recheck = unreadable
    assert _stop(worker, binding, root, sid, shown)["error"]["reason"] == "inventory_changed"
    assert not worker.terminated and not worker.stop_accepted
    assert _signals(worker, signal.SIGKILL) == []
    assert _signals(worker, signal.SIGSTOP) == _signals(worker, signal.SIGCONT) == [41]
    assert _events(root, sid, "stop_aborted")[-1]["reason"] == "unstable"
    assert worker.paused == {}


def test_a_transient_stop_failure_is_retried_within_the_bounds(conversation, monkeypatch):
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    record, failed = worker._signal, []

    def once(pid, sig):
        if pid == 10 and sig == signal.SIGSTOP and not failed:
            failed.append(pid)
            raise PermissionError(1, "Operation not permitted")
        record(pid, sig)

    worker._signal = once
    assert "result" in _stop(worker, binding, root, sid, shown)
    assert _signals(worker, signal.SIGKILL) == [10, 41] and worker.paused == {}
    (paused,) = _events(root, sid, "stop_paused")
    assert sorted(p["pid"] for p in paused["processes"]) == [10, 41]  # the ledger, journaled


def test_an_unreadable_identity_on_abort_is_still_resumed_and_retried_until_confirmed(
    conversation, monkeypatch
):
    """Resume is guaranteed: the job's stat cannot be read when the abort resumes it, so it is
    resumed as a member of the unit's cgroup and kept in the ledger, shown as unresumed, and
    an idle tick confirms it later and clears it."""
    root, sid, binding = conversation
    shell, node = Process(900, 41, "sh"), Process(8990, 42, "node", ppid=10)
    readings = {"scan": _scan(shell, observed=9000)}
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: readings["scan"])
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    _claim_then(monkeypatch, readings, _scan(shell, node, observed=9001))
    reading, broken = worker._recheck, {"on": False}

    def flaky(pid):
        if broken["on"] and pid == 41:
            raise native_processes.ScanError("EIO")
        return reading(pid)

    def members():
        broken["on"] = True  # read only on resume: from here 41's stat cannot be read
        return {10, 41, 42}

    worker._recheck, worker._members = flaky, members
    assert _stop(worker, binding, root, sid, shown)["error"]["reason"] == "inventory_changed"
    assert 41 in _signals(worker, signal.SIGCONT)  # resumed though unreadable
    assert set(worker.paused) == {(41, 900)}  # unconfirmed: kept
    assert _events(root, sid, "stop_aborted")[-1]["unresumed"] == 1
    worker.flush()
    assert _project(root, sid, binding.worker_id)["paused_unresumed"] == 1
    # The next idle tick reads it again, confirms the resume, clears the ledger.
    broken["on"], worker._members = False, lambda: {10, 41, 42}
    worker._recheck = reading
    asyncio.run(worker.retry_resume())
    worker.flush()
    assert worker.paused == {}
    assert _events(root, sid, "stop_resumed")[-1]["unresumed"] == 0
    assert _project(root, sid, binding.worker_id)["paused_unresumed"] == 0


def test_an_exception_inside_the_freeze_resumes_everything_it_stopped(conversation, monkeypatch):
    root, sid, binding = conversation
    shell, node = Process(900, 41, "sh"), Process(8990, 42, "node", ppid=41)
    readings = {"scan": _scan(shell, observed=9000), "calls": 0}

    def scanning(*a, **k):
        if readings.get("explode") and a[:1] != ("unit",):
            readings["calls"] += 1
            if readings["calls"] > 1:
                raise RuntimeError("the scan blew up")
        return readings["scan"]

    monkeypatch.setattr(native_processes, "observe", scanning)
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    real_claim = native_journal.claim

    def claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        readings["scan"], readings["explode"] = _scan(shell, node, observed=9001), True
        return result

    monkeypatch.setattr(native_journal, "claim", claim)
    response = _stop(worker, binding, root, sid, shown)
    assert response["error"]["code"] == "unavailable" and not worker.terminated
    stopped = _signals(worker, signal.SIGSTOP)
    assert stopped and _signals(worker, signal.SIGCONT) == stopped
    assert _signals(worker, signal.SIGKILL) == [] and worker.paused == {}
    claimed = next(o for o in native_journal.read(root, sid).operations.values())
    assert claimed.handoff == "not_sent"
    # A cancellation (any BaseException) inside the freeze resumes too, then propagates.
    again = _worker(conversation, ClaudeCodec(SESSION))
    again.baseline = frozenset({(10, 100)})
    readings.update(scan=_scan(shell, node, observed=9001), calls=0, explode=False)

    class Cancelled(BaseException):
        pass

    def cancelling(*a, **k):
        if a[:1] != ("unit",) and again.paused:
            raise Cancelled
        return readings["scan"]

    monkeypatch.setattr(native_processes, "observe", cancelling)
    with pytest.raises(Cancelled):
        again.freeze_and_check({"engine": [], "processes": [{"pid": 41, "start": 900}]})
    assert again.paused == {} and _signals(again, signal.SIGCONT) == _signals(again, signal.SIGSTOP)


# --- Hermes on PR #1757 head a19fc823: one owned Stop, checked journal writes -----------------


def test_an_idle_tick_at_any_await_of_an_accepted_exact_stop_resumes_nothing(
    conversation, monkeypatch
):
    """Hermes' probe: an idle tick fired between the freeze and the kill (after the freeze,
    during the claim, at the settlement, at the handoff, before the kill) SIGCONTed the frozen
    `sh` before the unit stop, and a resumed CLI could start work nobody confirmed. The
    accepted Stop owns its ledger until the frozen set is killed: no tick resumes any of it."""
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: _scan(shell, observed=9000))
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    seams: list[str] = []

    async def run():
        loop = asyncio.get_running_loop()
        ticks: list[asyncio.Future] = []

        def tick(seam):
            seams.append(seam)
            ticks.append(asyncio.ensure_future(worker.retry_resume()))

        def from_thread(seam):
            done = __import__("threading").Event()

            def fire():
                tick(seam)
                done.set()

            loop.call_soon_threadsafe(fire)
            done.wait(5)
            time.sleep(0.05)  # the tick runs while the Stop is suspended here

        real_claim, real_freeze = native_journal.claim, worker.freeze_and_check
        real_settle, real_handoff, real_kill = worker.settle, worker.handoff, worker.kill_frozen

        def claim(*args, **kwargs):
            result = real_claim(*args, **kwargs)
            from_thread("claim")
            return result

        def freeze(confirmed):
            verdict = real_freeze(confirmed)
            from_thread("freeze")
            return verdict

        def settle(*args, **kwargs):
            tick("settlement")
            return real_settle(*args, **kwargs)

        async def handoff(*args, **kwargs):
            tick("handoff")
            for _ in range(20):
                await asyncio.sleep(0.005)
            return await real_handoff(*args, **kwargs)

        def kill(frozen):
            from_thread("kill")
            return real_kill(frozen)

        monkeypatch.setattr(native_journal, "claim", claim)
        worker.freeze_and_check, worker.settle = freeze, settle
        worker.handoff, worker.kill_frozen = handoff, kill
        response = await worker.effect(
            native_ipc.validate_request(_stop_request(binding, root, sid, shown))
        )
        await asyncio.wait_for(asyncio.gather(*ticks), 5)
        return response

    response = asyncio.run(run())
    assert _signals(worker, signal.SIGCONT) == []  # nothing frozen was ever resumed
    assert "result" in response and worker.stop_accepted
    assert seams == ["claim", "freeze", "settlement", "handoff", "kill"]
    assert _signals(worker, signal.SIGSTOP) == _signals(worker, signal.SIGKILL) == [10, 41]
    assert worker.paused == {} and worker.stop_owner is None


def _stop_request(binding, root, sid, digest):
    return journal_tests.request(
        binding,
        action="stop",
        revision=native_journal.read(root, sid).revision,
        confirm_digest=digest,
    )


@pytest.mark.parametrize("record", ["stop_paused", "background_settled"])
def test_an_unwritable_stop_record_refuses_the_exact_stop_and_resumes_everything(
    conversation, monkeypatch, record
):
    """The ledger (`stop_paused`) and the settlement are checked preconditions of an exact
    Stop: one that cannot be written resumes every frozen process, refuses the Stop (never
    accepted, nothing killed), settles its claim `not_sent`, and leaves the unit running — a
    later Stop still works."""
    root, sid, binding = conversation
    shell = Process(900, 41, "sh")
    monkeypatch.setattr(native_processes, "observe", lambda *a, **k: _scan(shell, observed=9000))
    worker = _worker(conversation, ClaudeCodec(SESSION))
    shown = _served(worker, shell, observed=1000)
    worker.flush()
    real_append = native_journal.append_events

    def append(root_, binding_, events):
        if any(e["kind"] == record for e in events):
            raise native_journal.JournalError("unwritable", "the disk is full", "ENOSPC")
        return real_append(root_, binding_, events)

    monkeypatch.setattr(native_journal, "append_events", append)
    response = _stop(worker, binding, root, sid, shown)
    assert response["error"]["reason"] == native_ipc.JOURNAL_UNWRITABLE
    assert not worker.stop_accepted and not worker.terminated
    assert not native_state.read_lifecycle(binding.worker_id).get("stop_accepted_at")
    assert _signals(worker, signal.SIGSTOP) == _signals(worker, signal.SIGCONT) == [10, 41]
    assert _signals(worker, signal.SIGKILL) == [] and worker.paused == {}
    claim = next(o for o in native_journal.read(root, sid).operations.values())
    assert claim.handoff == "not_sent"
    assert not _events(root, sid, "background_settled")
    assert _events(root, sid, "stop_aborted")[-1]["reason"] == "journal"
    assert worker.stop_owner is None
    # The journal recovers: the same list, confirmed again, stops.
    monkeypatch.setattr(native_journal, "append_events", real_append)
    worker.sent.clear()
    assert "result" in _stop(worker, binding, root, sid, shown) and worker.terminated
    assert _signals(worker, signal.SIGKILL) == [10, 41]


def test_a_background_inventory_never_refuses_a_send_at_the_seen_revision(tmp_path):
    """#1801's rule extended (#1724): the worker journals an inventory while work runs (at most
    every 5 min, more on agent task changes); a send at the revision the operator saw is not
    refused for it — but a generation's settlement or an aborted Stop still is activity."""
    from agent_sessions import native_events

    worker = ident()

    def journal(*events):
        j = native_journal.Journal({}, revision=10, events=native_events.Events(tmp_path))
        for cursor, (kind, data) in enumerate(events, 11):
            j.events.append(
                {
                    "cursor": cursor,
                    "worker_id": worker,
                    "connection_id": "c",
                    "ts": 0.0,
                    "event": {"kind": kind, "data": data},
                }
            )
            j.revision = cursor
        return j

    ok = native_runtime._nothing_to_review
    assert ok(
        journal(("background_inventory", inventory()), ("stop_resumed", {})), 10, worker, worker
    )
    assert not ok(
        journal(
            ("background_settled", {"reason": "idle"}),
        ),
        10,
        worker,
        worker,
    )
    assert not ok(
        journal(
            ("stop_aborted", {"reason": "changed"}),
        ),
        10,
        worker,
        worker,
    )
