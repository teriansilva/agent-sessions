"""A tool that runs out of memory is killed alone, not the whole API session (#1732).

The unit runs `OOMPolicy=continue`, so an overrun kills ONE process the kernel selects. The agent
starts through an exec trampoline (`native_exec`) that ranks it and everything it runs above the
worker. The worker counts kills from its unit's hierarchical `memory.events:oom_kill` and journals
each rise as one `memory_oom_kill` notice, on the turn that was in flight at both reads or else
between turns. When the agent itself died by SIGKILL while the count rose, the turn states those
facts — never "the agent ran out of memory".

Every launch, trampoline, event-schema and reap-ordering test runs for each `kinds.API_KINDS`
client, so a client added later is covered without a new test. The real-systemd cases live in
`test_native_oom_systemd.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from agent_sessions import (
    chat_store,
    native_containment,
    native_exec,
    native_ipc,
    native_runtime,
    native_state,
    native_worker,
    structured_runtime,
)
from agent_sessions import native_journal as journal
from agent_sessions.plugins import kinds
from test_native_journal import request

API_KINDS = sorted(kinds.API_KINDS)
INVOCATION = "d4d3d996ed7949e38104d854a4a3e2cb"
OTHER_INVOCATION = "0123456789abcdef0123456789abcdef"
WORKER = native_containment.WorkerIdentity("8f14e45f-ceea-467a-9575-7a6f2e8f0b2a")
SIGPIPE_BIT = 1 << (signal.SIGPIPE - 1)


def ident() -> str:
    return str(uuid.uuid4())


def _property(argv: list[str], name: str) -> str:
    (value,) = [
        a.removeprefix(f"--property={name}=") for a in argv if a.startswith(f"--property={name}=")
    ]
    return value


def _worker(tmp_path: Path, kind: str) -> native_worker.Worker:
    return native_worker.Worker(
        {
            "worker_id": ident(),
            "session_key": f"test-api:{ident()}",
            "adapter": kind,
            "journal_root": str(tmp_path / "journal"),
            "capability": native_ipc.Capability.create()._value,
            "connection_id": ident(),
            "mode": "create",
            "binary": "/opt/agent/bin/agent",
            "cwd": str(tmp_path),
            # Claude's history id is minted before launch; the others bind theirs at create.
            **({"native_id": ident()} if kind == "claude-stream-json" else {}),
        },
        tmp_path,
    )


# --- the unit: one victim, not the group ---------------------------------------------------------


@pytest.mark.parametrize("kind", API_KINDS)
def test_the_unit_kills_one_selected_process_and_keeps_its_other_limits(kind, tmp_path):
    argv = native_containment.launch_argv(
        WORKER, python="/bin/python", state_dir="/state", home="/home", memory_gib=8
    )
    assert _property(argv, "OOMPolicy") == "continue"
    # Everything else that bounds the group is as before (#1643/#1651): the cap, the soft limit,
    # the task bound, and Stop / idle teardown killing the WHOLE group.
    assert _property(argv, "MemoryMax") == "8G"
    assert _property(argv, "MemoryHigh") == "7372M"
    assert _property(argv, "KillMode") == "control-group"
    assert _property(argv, "TasksMax") == "4096"
    assert _property(argv, "NoNewPrivileges") == "yes"
    # This client's agent starts behind the trampoline, from the worker's own interpreter.
    worker = _worker(tmp_path, kind)
    own = native_exec.own_value()
    assert worker.spawn_argv() == [
        sys.executable,
        "-I",
        "-m",
        "agent_sessions.native_exec",
        "--oom-score-adj",
        str(min(1000, own + native_exec.RAISE)),
        "--",
        *worker.argv(),
    ]


@pytest.mark.parametrize("kind", API_KINDS)
def test_an_unreadable_own_value_starts_the_agent_directly(kind, tmp_path, monkeypatch):
    monkeypatch.setattr(native_exec, "own_value", lambda path=None: None)
    worker = _worker(tmp_path, kind)
    assert worker.spawn_argv() == worker.argv()


# --- the trampoline ------------------------------------------------------------------------------


@pytest.mark.parametrize(("own", "agent"), [(0, 100), (200, 300), (950, 1000), (1000, 1000)])
def test_the_agent_ranks_above_the_worker_never_past_the_kernels_bound(own, agent):
    assert native_exec.raised(own) == agent


@pytest.mark.parametrize(
    "args",
    [
        ["--oom-score-adj", "abc", "--", "/bin/true"],
        ["--oom-score-adj", "1001", "--", "/bin/true"],
        ["--oom-score-adj", "-1001", "--", "/bin/true"],
        ["--oom-score-adj", "1e3", "--", "/bin/true"],
        ["--oom-score-adj", "+300", "--", "/bin/true"],
        ["--oom-score-adj", " 300", "--", "/bin/true"],
        ["--oom-score-adj", "", "--", "/bin/true"],
        ["--oom-score-adj", "199", "--", "/bin/true"],  # below the inherited 200: a LOWER
        ["--oom-score-adj", "300", "/bin/true"],  # no `--`
        ["--oom-score-adj", "300", "--"],  # no command
        ["--oom-score-adj", "300", "--", ""],
        ["--adj", "300", "--", "/bin/true"],
        [],
    ],
)
def test_the_trampoline_refuses_anything_but_a_raise_and_a_command(args):
    with pytest.raises(native_exec.Refused):
        native_exec.parse(args, own=200)


def test_the_trampoline_refuses_when_it_cannot_tell_a_raise():
    with pytest.raises(native_exec.Refused):
        native_exec.parse(["--oom-score-adj", "300", "--", "/bin/true"], own=None)


def test_a_refused_trampoline_never_executes(monkeypatch, capsys):
    monkeypatch.setattr(native_exec, "own_value", lambda path=None: 200)
    monkeypatch.setattr(native_exec.os, "execvp", lambda *a: pytest.fail("executed"))
    assert native_exec.main(["--oom-score-adj", "nope", "--", "/bin/true"]) == 2
    assert "refused" in capsys.readouterr().err


def test_the_trampoline_argv_is_literal_and_bounded():
    assert native_exec.argv("/venv/bin/python", 300, ["/bin/agent", "a b", "$(id)"]) == [
        "/venv/bin/python",
        "-I",
        "-m",
        "agent_sessions.native_exec",
        "--oom-score-adj",
        "300",
        "--",
        "/bin/agent",
        "a b",
        "$(id)",
    ]
    for bad in (1001, -1001, True, "300"):
        with pytest.raises(native_exec.Refused):
            native_exec.argv("/venv/bin/python", bad, ["/bin/agent"])
    with pytest.raises(native_exec.Refused):
        native_exec.argv("python", 300, ["/bin/agent"])


# A fake agent that forks first thing: its child reports what it inherited.
_FORKER = """
import json, os, sys
pid = os.fork()
if pid == 0:
    status = open(f"/proc/{os.getppid()}/status").read()
    ignored = next(l for l in status.splitlines() if l.startswith("SigIgn:")).split()[1]
    print(json.dumps({"child": int(open("/proc/self/oom_score_adj").read()),
                      "agent": int(open(f"/proc/{os.getppid()}/oom_score_adj").read()),
                      "sig_ignored": ignored, "env": dict(os.environ), "cwd": os.getcwd(),
                      "argv": sys.argv[1:]}), flush=True)
    os._exit(0)
os.waitpid(pid, 0)
"""


def _trampoline(tmp_path, value, *command, env=None):
    """The real spawn, under the worker's launch shape: `env -i` and `python -I`."""
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8", "MARK": "a b"}
    return subprocess.run(
        [
            "/usr/bin/env",
            "-i",
            *(f"{k}={v}" for k, v in (env or base).items()),
            *native_exec.argv(sys.executable, value, list(command)),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


def test_a_real_spawn_raises_the_agent_before_its_first_fork_and_keeps_the_rest(tmp_path):
    own = native_exec.own_value()
    value = native_exec.raised(own)
    done = _trampoline(tmp_path, value, sys.executable, "-c", _FORKER, "--flag", "a b")
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen["agent"] == seen["child"] == value  # set before the agent could fork anything
    assert native_exec.own_value() == own  # the caller's (the worker's) own value is untouched
    # Exactly the agent's argv, environment and directory; a default SIGPIPE, as before (#1732).
    assert seen["argv"] == ["--flag", "a b"]
    assert seen["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "MARK": "a b",
    }
    assert seen["cwd"] == str(tmp_path)
    # A default SIGPIPE, as when the worker started the agent directly (an agent that is not
    # Python itself: Python ignores SIGPIPE at its own start).
    status = _trampoline(tmp_path, value, "/bin/cat", "/proc/self/status")
    (ignored,) = [
        line.split()[1] for line in status.stdout.splitlines() if line.startswith("SigIgn")
    ]
    assert int(ignored, 16) & SIGPIPE_BIT == 0


def test_a_real_spawn_with_a_bad_value_never_runs_the_agent(tmp_path):
    marker = tmp_path / "ran"
    for bad in ("-5", "1001", "x"):
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "agent_sessions.native_exec",
                "--oom-score-adj",
                bad,
                "--",
                "/usr/bin/touch",
                str(marker),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == native_exec.EXIT_REFUSED, done.stderr
        assert not marker.exists()


def test_an_agent_that_cannot_start_is_reported_not_hidden(tmp_path):
    done = _trampoline(tmp_path, native_exec.raised(native_exec.own_value()), "/nonexistent/agent")
    assert done.returncode == native_exec.EXIT_EXEC_FAILED
    assert "could not start" in done.stderr


# --- the event: closed, bounded, anonymous -------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"count": 1, "invocation_id": INVOCATION},
        {"count": 2, "invocation_id": INVOCATION, "operation_id": str(uuid.UUID(int=1))},
        {"count": 7, "invocation_id": INVOCATION, "agent_turn": str(uuid.UUID(int=2))},
    ],
)
def test_an_oom_notice_passes_the_ipc_boundary(data):
    assert native_ipc.normalize_event({"kind": "memory_oom_kill", "data": data})["data"] == data


@pytest.mark.parametrize(
    "data",
    [
        {"count": 0, "invocation_id": INVOCATION},
        {"count": -1, "invocation_id": INVOCATION},
        {"count": True, "invocation_id": INVOCATION},
        {"count": "1", "invocation_id": INVOCATION},
        {"count": 10**7, "invocation_id": INVOCATION},
        {"count": 1, "invocation_id": INVOCATION.upper()},
        {"count": 1, "invocation_id": "x"},
        {"count": 1},
        {"invocation_id": INVOCATION},
        # Never a process: no name, argv, pid or free text.
        {"count": 1, "invocation_id": INVOCATION, "process": "chrome"},
        {"count": 1, "invocation_id": INVOCATION, "argv": ["node"]},
        {"count": 1, "invocation_id": INVOCATION, "pid": 4},
        {"count": 1, "invocation_id": INVOCATION, "operation_id": "turn"},
        {
            "count": 1,
            "invocation_id": INVOCATION,
            "operation_id": str(uuid.UUID(int=1)),
            "agent_turn": str(uuid.UUID(int=2)),
        },
    ],
)
def test_anything_else_in_an_oom_notice_is_refused(data):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "memory_oom_kill", "data": data})


@pytest.fixture
def conversations(tmp_path):
    """One journal per client kind."""

    def make(kind: str):
        session_id = ident()
        root = tmp_path / kind / "chat"
        binding = native_ipc.Binding(
            session_key=f"test-api:{session_id}",
            worker_id=ident(),
            connection_id=ident(),
            adapter=kind,
        )
        chat_store.create(
            root,
            session_id,
            cwd=str(tmp_path),
            request={"runtime": "api", "session_key": binding.session_key},
            model="requested-model",
        )
        return root, session_id, binding

    return make


def _claim(root, sid, binding, *, queued=False, sent=True) -> str:
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim, queued=queued)
    if sent:
        journal.record_handoff(root, binding, operation_id, "sent")
    return operation_id


def _done(root, binding, operation_id):
    journal.append_events(
        root,
        binding,
        [
            {
                "kind": "turn_completed",
                "data": {
                    "operation_id": operation_id,
                    "native_turn_id": "n",
                    "state": "completed",
                    "error": "",
                },
            }
        ],
    )


def _oom(root, binding, count, *, invocation=INVOCATION, **turn):
    data = {"count": count, "invocation_id": invocation, **turn}
    journal.append_events(root, binding, [{"kind": "memory_oom_kill", "data": data}])


def _project(root, sid, binding, **kwargs):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        **kwargs,
    )


def _by_id(view):
    return {t["turn_id"]: t for t in view["turns"]}


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_turns_notice_is_counted_once_per_invocation_and_count(conversations, kind):
    root, sid, binding = conversations(kind)
    turn = _claim(root, sid, binding)
    _oom(root, binding, 1, operation_id=turn)
    _oom(root, binding, 1, operation_id=turn)  # the same reading again (a re-read, a replay)
    _oom(root, binding, 3, operation_id=turn)  # two more between reads: one notice of 2
    _oom(root, binding, 2, operation_id=turn)  # older than shown: nothing new
    _oom(root, binding, 1, invocation=OTHER_INVOCATION, operation_id=turn)  # another invocation
    _done(root, binding, turn)
    for _ in range(2):  # a reload reads the same
        view = _project(root, sid, binding)
        shown = _by_id(view)[turn]
        assert shown["oom_kills"] == 4 and "oom_kills_after" not in shown
        assert shown["status"] == "done"  # a notice never changes a turn's status
    public = structured_runtime._snapshot(binding.session_key, view)
    (turn_view,) = public["turns"]
    assert turn_view["oom_kills"] == 4 and turn_view["state"] == "completed"
    assert "oom_kills_after" not in turn_view and "oom_kills_start" not in public


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_session_level_notice_sits_after_the_turn_that_started_last(conversations, kind):
    root, sid, binding = conversations(kind)
    first = _claim(root, sid, binding)
    _done(root, binding, first)
    second = _claim(root, sid, binding)
    queued = _claim(root, sid, binding, queued=True, sent=False)  # claimed, never started
    _oom(root, binding, 1)
    _done(root, binding, second)
    view = _project(root, sid, binding)
    turns = _by_id(view)
    assert turns[second]["oom_kills_after"] == 1
    assert not any(t.get("oom_kills") for t in turns.values())  # never pinned to a turn
    assert "oom_kills_after" not in turns[first] and "oom_kills_after" not in turns[queued]
    public = structured_runtime._snapshot(binding.session_key, view)
    after = {t["turn_id"]: t.get("oom_kills_after") for t in public["turns"]}
    assert after == {first: None, second: 1, queued: None}


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_notice_before_any_turn_is_shown_only_with_the_first_turn(conversations, kind):
    root, sid, binding = conversations(kind)
    _oom(root, binding, 2)
    first = _claim(root, sid, binding)
    _done(root, binding, first)
    second = _claim(root, sid, binding)
    view = _project(root, sid, binding)
    assert view["oom_kills_start"] == 2
    assert structured_runtime._snapshot(binding.session_key, view)["oom_kills_start"] == 2
    newest = _project(root, sid, binding, max_turns=1)  # the first turn is not in this window
    assert "oom_kills_start" not in newest and set(_by_id(newest)) == {second}


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_windowed_read_still_sees_its_turns_notice(conversations, kind):
    root, sid, binding = conversations(kind)
    turns = []
    for _ in range(3):
        turns.append(_claim(root, sid, binding))
        if len(turns) == 1:
            _oom(root, binding, 1, operation_id=turns[0])
        _done(root, binding, turns[-1])
    _oom(root, binding, 2, operation_id=turns[-1])  # a notice can land after its turn's end
    assert _by_id(_project(root, sid, binding, max_turns=1))[turns[-1]]["oom_kills"] == 1
    assert _by_id(_project(root, sid, binding))[turns[0]]["oom_kills"] == 1


# --- attribution in the worker -------------------------------------------------------------------


class Counter:
    """The worker's counter read, scripted: a value per read, None for an unreadable one."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        value = self.values.pop(0)
        return None if value is None else (value, INVOCATION)


def _attributing(tmp_path, kind, *reads):
    worker = _worker(tmp_path, kind)
    worker.oom_count = Counter(*reads)
    return worker


def _notices(worker):
    return [i["data"] for i in worker.pending if i["kind"] == "memory_oom_kill"]


def _start(worker, operation_id):
    worker.codec.operation_id = operation_id
    return worker.turn_transition()


def _settle(worker):
    worker.codec.operation_id = None
    return worker.turn_transition()


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_kill_within_one_turn_names_that_turn(tmp_path, kind):
    worker = _attributing(tmp_path, kind, 0, 0, 1)
    turn = ident()
    _start(worker, turn)  # read 0 at the start
    worker.oom_read(worker._turn_key, worker._turn_key)  # a periodic read mid-turn: still 0
    assert _settle(worker) is True  # the settle's read sees the kill: both reads in the turn
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION, "operation_id": turn}]


@pytest.mark.parametrize("kind", API_KINDS)
def test_an_own_turns_kill_names_the_own_turn(tmp_path, kind):
    worker = _attributing(tmp_path, kind, 0, 1)
    own = ident()
    worker.codec.background_turn = own
    worker.turn_transition()
    worker.codec.background_turn = None
    worker.turn_transition()
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION, "agent_turn": own}]


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_turn_transition_between_the_kill_and_the_read_is_session_level(tmp_path, kind):
    worker = _attributing(tmp_path, kind, 0, 0, 1)
    first, second = ident(), ident()
    _start(worker, first)
    _settle(worker)  # read 0: the first turn ends clean
    _start(worker, second)  # read 1: the kill happened between turns
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION}]


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_turn_that_started_and_settled_between_two_reads_is_never_named(tmp_path, kind):
    worker = _attributing(tmp_path, kind, 0, 1)
    worker.oom_read(None, None)  # read 0 before the turn
    worker.codec.operation_id = ident()  # the turn starts and ends inside one batch
    worker.codec.operation_id = None
    worker.oom_read(worker._turn_key, worker._turn_key)  # the next periodic read: 1
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION}]


@pytest.mark.parametrize("kind", API_KINDS)
def test_an_unreadable_read_at_the_start_makes_the_kill_session_level(tmp_path, kind):
    worker = _attributing(tmp_path, kind, None, 1)
    turn = ident()
    _start(worker, turn)  # unreadable: where the window began is unknown
    _settle(worker)
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION}]


@pytest.mark.parametrize("kind", API_KINDS)
def test_two_kills_between_reads_are_one_notice_and_no_rise_is_none(tmp_path, kind):
    worker = _attributing(tmp_path, kind, 0, 2, 2, 2)
    turn = ident()
    _start(worker, turn)
    _settle(worker)
    worker.oom_read(None, None)
    worker.oom_read(None, None)
    assert _notices(worker) == [{"count": 2, "invocation_id": INVOCATION, "operation_id": turn}]


def test_the_counter_is_read_only_from_the_pinned_own_unit(tmp_path, monkeypatch):
    worker = _worker(tmp_path, "codex-app-server")
    unit = native_containment.WorkerIdentity(worker.worker_id).unit
    group = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}"
    directory = tmp_path / "cg" / group.lstrip("/")
    directory.mkdir(parents=True)
    events = directory / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\noom_group_kill 0\n")
    from agent_sessions import native_memory

    monkeypatch.setattr(native_memory, "CGROUP_ROOT", tmp_path / "cg")
    monkeypatch.setattr(worker, "_own_group", lambda: group)
    worker.oom_baseline()  # 2 kills before this worker: not its own
    assert worker.oom_count() is None  # not pinned yet: nothing is read
    native_state.update_lifecycle(worker.worker_id, control_group=group + "x")
    assert worker.oom_count() is None  # pinned elsewhere
    native_state.update_lifecycle(worker.worker_id, control_group=group, invocation_id=INVOCATION)
    assert worker.oom_count() == (0, INVOCATION)
    events.write_text("oom 5\noom_kill 5\n")  # three kills, nested groups included
    assert worker.oom_count() == (3, INVOCATION)
    events.write_text("oom 5\n")
    assert worker.oom_count() is None


# --- reap ordering: the agent's own end, never one a stop gave it --------------------------------


async def _reap(tmp_path, kind, script, *, reads, stop_after=None, seen=0):
    """Run the real reader against a real child: (worker, reports, seconds, the turn)."""
    worker = _attributing(tmp_path, kind, *reads)
    worker._oom_seen = seen  # the count last journaled before the agent's end
    reports = []
    worker.report = lambda **fields: reports.append(fields)
    turn = ident()
    worker.codec.operation_id = turn
    worker._turn_key = worker._oom_turn = ("operation_id", turn)  # in flight at the last read
    worker.proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if stop_after is not None:

        async def stop():
            while not stop_after.exists():  # the child is past its start
                await asyncio.sleep(0.01)
            worker.stop_child()

        stopping = asyncio.create_task(stop())
    began = time.monotonic()
    await asyncio.wait_for(worker.reader(), 30)
    seconds = time.monotonic() - began
    await asyncio.wait_for(worker.proc.wait(), 30)
    if stop_after is not None:
        await stopping
    return worker, reports, seconds, turn


def _kinds(worker):
    return [i["kind"] for i in worker.pending]


SIGKILL_SELF = "import os, signal, time; time.sleep(0.2); os.kill(os.getpid(), signal.SIGKILL)"
CLOSE_THEN_SIGKILL = (
    "import os, signal, time; os.close(1); time.sleep(0.5); os.kill(os.getpid(), signal.SIGKILL)"
)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
@pytest.mark.parametrize("script", [SIGKILL_SELF, CLOSE_THEN_SIGKILL])
async def test_an_agent_sigkilled_while_the_count_rose_is_recorded_from_its_own_code(
    tmp_path, kind, script
):
    worker, reports, _, turn = await _reap(tmp_path, kind, script, reads=[1])
    assert worker.proc.returncode == -signal.SIGKILL  # its own end, not the stop's SIGTERM
    # The counter's last read is journaled before the disconnect, on the turn in flight.
    assert _kinds(worker) == ["memory_oom_kill", "disconnected"]
    assert _notices(worker) == [{"count": 1, "invocation_id": INVOCATION, "operation_id": turn}]
    (report,) = reports
    agent_exit = report["agent_exit"]
    assert time.time() - agent_exit.pop("at") < 60
    assert agent_exit == {
        "reason": "sigkill_during_oom",
        "returncode": -signal.SIGKILL,
        "oom_kills": 1,
        "invocation_id": INVOCATION,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_a_sigkill_after_an_already_journaled_kill_stays_generic(tmp_path, kind):
    # A tool's kill was journaled (count 1) before an unrelated SIGKILL hit the agent.
    worker, reports, _, _ = await _reap(tmp_path, kind, SIGKILL_SELF, reads=[1], seen=1)
    assert worker.proc.returncode == -signal.SIGKILL
    assert reports == [] and _kinds(worker) == ["disconnected"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_a_sigkill_without_a_rise_and_an_ordinary_exit_with_one_stay_generic(tmp_path, kind):
    worker, reports, _, _ = await _reap(tmp_path, kind, SIGKILL_SELF, reads=[0])
    assert worker.proc.returncode == -signal.SIGKILL
    assert reports == [] and _kinds(worker) == ["disconnected"]
    exits = "import sys, time; time.sleep(0.2); sys.exit(3)"
    worker, reports, _, _ = await _reap(tmp_path / "b", kind, exits, reads=[1])
    assert worker.proc.returncode == 3
    # The kill is still journaled (it happened), but the agent's end is not tied to it.
    assert reports == [] and _kinds(worker) == ["memory_oom_kill", "disconnected"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_a_protocol_error_while_the_agent_lives_takes_the_stop_path(tmp_path, kind):
    script = (
        "import sys, time; sys.stdout.write('{not a frame\\n'); sys.stdout.flush(); time.sleep(30)"
    )
    worker, reports, seconds, _ = await _reap(tmp_path, kind, script, reads=[])
    assert worker.reason.startswith("native protocol violation")
    assert worker.proc.returncode == -signal.SIGTERM  # stopped at once: no reap window
    assert seconds < native_worker.REAP_WINDOW and reports == []
    assert "memory_oom_kill" not in _kinds(worker)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_an_agent_alive_after_its_output_ends_is_stopped_after_the_window(tmp_path, kind):
    script = "import os, time; os.close(1); time.sleep(30)"
    worker, reports, seconds, _ = await _reap(tmp_path, kind, script, reads=[1])
    assert worker.proc.returncode == -signal.SIGTERM
    assert native_worker.REAP_WINDOW <= seconds < native_worker.REAP_WINDOW + 5
    # Never derived from a process BattleLab stopped: no read, no reason.
    assert reports == [] and _kinds(worker) == ["disconnected"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_an_agent_battlelab_stopped_gets_no_oom_reason(tmp_path, kind):
    ready = tmp_path / "ready"
    script = f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    worker, reports, _, _ = await _reap(tmp_path, kind, script, reads=[1], stop_after=ready)
    assert worker.proc.returncode == -signal.SIGTERM
    assert reports == [] and _kinds(worker) == ["disconnected"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_nothing_new_is_admitted_while_the_agent_is_reaped(tmp_path, kind):
    script = "import os, time; os.close(1); time.sleep(1); os._exit(5)"
    worker = _attributing(tmp_path, kind, 0)
    worker.report = lambda **fields: None
    worker.proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reading = asyncio.create_task(worker.reader())
    for _ in range(200):
        if worker.closing:
            break
        await asyncio.sleep(0.01)
    assert worker.closing and not worker.done.is_set()  # in the window: effects refuse
    await asyncio.wait_for(reading, 30)
    assert worker.proc.returncode == 5


# --- the reason ----------------------------------------------------------------------------------


AGENT_KILLED = "the agent was killed (SIGKILL) while a process in this session ran out of memory"
OOM_EXIT = {"reason": "sigkill_during_oom", "invocation_id": INVOCATION}


@pytest.mark.parametrize(
    ("life", "reason"),
    [
        (
            {"agent_exit": {"reason": "sigkill_during_oom", "invocation_id": INVOCATION}},
            AGENT_KILLED,
        ),
        ({"agent_exit": {"reason": "sigkill_during_oom", "invocation_id": OTHER_INVOCATION}}, None),
        ({"agent_exit": {"reason": "sigkill_during_oom"}}, None),
        ({"agent_exit": {"reason": "oom", "invocation_id": INVOCATION}}, None),
        ({"agent_exit": "sigkill_during_oom"}, None),
        # A whole-unit `oom-kill` keeps #1651's words, whatever the worker recorded.
        (
            {
                "exit": {"result": "oom-kill", "invocation_id": INVOCATION},
                "agent_exit": {"reason": "sigkill_during_oom", "invocation_id": INVOCATION},
            },
            "killed: out of memory (this session's limit is 8 GB — Settings → Resource limits)",
        ),
        (
            {
                "exit": {"result": "success", "invocation_id": INVOCATION},
                "agent_exit": {"reason": "sigkill_during_oom", "invocation_id": INVOCATION},
            },
            AGENT_KILLED,
        ),
    ],
)
def test_the_agents_end_is_stated_as_facts_for_the_pinned_invocation_only(life, reason):
    worker_id = ident()
    native_state.update_lifecycle(worker_id, invocation_id=INVOCATION, memory_gib=8, **life)
    assert native_runtime._ended_reason(worker_id) == reason
    assert "agent ran out of memory" not in (reason or "")


@pytest.mark.parametrize("kind", API_KINDS)
def test_a_disconnected_turn_of_a_gone_worker_states_the_agents_end(conversations, kind):
    root, sid, binding = conversations(kind)
    turn = _claim(root, sid, binding)
    _oom(root, binding, 1, operation_id=turn)
    journal.append_events(
        root,
        binding,
        [
            {
                "kind": "disconnected",
                "data": {
                    "state": "uncertain",
                    "native_id": None,
                    "native_turn_id": None,
                    "operation_id": turn,
                },
            }
        ],
    )

    def project(live, reason):
        return _by_id(
            native_runtime.project(
                journal.read(root, sid),
                {"request": {"model": None}, "adapter": kind},
                live,
                ended_reason=lambda worker_id: reason,
            )
        )[turn]

    gone = project(None, AGENT_KILLED)
    assert (gone["status"], gone["code"], gone["reason"]) == ("failed", "uncertain", AGENT_KILLED)
    assert gone["oom_kills"] == 1
    # While its worker still runs, or with nothing recorded, the words stay generic.
    assert project(binding.worker_id, AGENT_KILLED)["reason"] == "the native connection closed"
    assert project(None, None)["reason"] == "the native connection closed"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_every_api_kind_is_covered():
    # A new client is covered by every parametrized test above without editing this file.
    assert set(API_KINDS) == kinds.API_KINDS and len(API_KINDS) >= 4
    assert os.path.basename(native_exec.__file__) == "native_exec.py"
