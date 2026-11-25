"""How a native worker's unit ended (#1643): systemd's `ExecStopPost=` hook records it for the
pinned invocation, and a turn its generation left unsettled names the memory limit — while staying
`uncertain`, and nothing is ever resent because of it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import test_native_agent_turns as agent_tests
import test_native_journal as journal_tests
from agent_sessions import native_containment as containment
from agent_sessions import (
    native_exit_record,
    native_memory,
    native_runtime,
    native_state,
    structured_runtime,
)
from agent_sessions import native_journal as journal
from test_native_journal import event, request

conversation = journal_tests.conversation
WORKER = containment.WorkerIdentity("8f14e45f-ceea-467a-9575-7a6f2e8f0b2a")
INVOCATION = "d4d3d996ed7949e38104d854a4a3e2cb"
OOM = "killed: this session hit its 8 GB memory limit (Settings → Resource limits)"
OUTSIDE = "killed: out of memory outside this session's own limit (the host or a group it runs in)"
KILLED = {"result": "oom-kill", "invocation_id": INVOCATION}


# --- the launch argv ---------------------------------------------------------------------------


def _property(argv: list[str], name: str) -> str:
    (value,) = [
        a.removeprefix(f"--property={name}=") for a in argv if a.startswith(f"--property={name}=")
    ]
    return value


@pytest.mark.parametrize(("gib", "mib"), [(8, 7372), (1, 921), (32, 29491)])
def test_the_soft_limit_sits_at_ninety_percent_of_the_hard_one(gib, mib):
    argv = containment.launch_argv(
        WORKER, python="/bin/python", state_dir="/state", home="/home", memory_gib=gib
    )
    assert _property(argv, "MemoryMax") == f"{gib}G"
    assert _property(argv, "MemoryHigh") == f"{mib}M"


def test_the_exit_hook_is_one_literal_quoted_command_that_cannot_change_the_result():
    argv = containment.launch_argv(
        WORKER,
        python="/releases/build one/venv/bin/python",
        state_dir='/private/native "state"',
        home="/home/operator",
    )
    hook = _property(argv, "ExecStopPost")
    # `-`: its failure never changes how the unit ended; `:`: no `$VAR` expansion of its words.
    assert hook == (
        '-:"/releases/build one/venv/bin/python" "-I" "-m" "agent_sessions.native_exit_record"'
        f' "--worker-id" "{WORKER.worker_id}" "--state-dir" "/private/native \\"state\\""'
    )
    assert "sh" not in hook.split('"')  # never a shell (CLAUDE.md shell-free guarantee)


@pytest.mark.parametrize("field", ["python", "state_dir"])
def test_the_exit_hook_refuses_specifiers_like_the_rest_of_the_launch(field):
    kwargs = {"python": "/bin/python", "state_dir": "/state", "home": "/home"}
    kwargs[field] = "/a/%n"
    with pytest.raises(containment.ContainmentError):
        containment.launch_argv(WORKER, **kwargs)


# --- the recorder ------------------------------------------------------------------------------


@pytest.fixture
def pinned(monkeypatch):
    worker_id = str(uuid.uuid4())
    native_state.update_lifecycle(worker_id, phase="ready", invocation_id=INVOCATION, memory_gib=8)
    for name in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS", "INVOCATION_ID"):
        monkeypatch.delenv(name, raising=False)
    return worker_id


def _ended(monkeypatch, result="oom-kill", code="killed", status="KILL", invocation=INVOCATION):
    for name, value in {
        "SERVICE_RESULT": result,
        "EXIT_CODE": code,
        "EXIT_STATUS": status,
        "INVOCATION_ID": invocation,
    }.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_the_recorder_writes_how_the_pinned_invocation_ended_and_when(pinned, monkeypatch):
    _ended(monkeypatch)
    before = time.time()
    assert native_exit_record.record(pinned) is True
    exit_ = native_state.read_lifecycle(pinned)["exit"]
    assert before <= exit_.pop("at") <= time.time()
    assert exit_ == {
        "result": "oom-kill",
        "code": "killed",
        "status": "KILL",
        "invocation_id": INVOCATION,
    }
    assert native_state.read_lifecycle(pinned)["phase"] == "ready"  # nothing else is touched


@pytest.mark.parametrize(
    "changes",
    [
        {"invocation": "0" * 31 + "1"},  # another invocation of the same name
        {"invocation": None},
        {"invocation": "D4D3D996ED7949E38104D854A4A3E2CB"},  # not systemd's form
        {"result": None},
        {"result": "oom kill; rm"},
        {"result": "x" * 40},
    ],
)
def test_the_recorder_writes_nothing_it_cannot_tie_to_the_pin(pinned, monkeypatch, changes):
    _ended(monkeypatch, **changes)
    assert native_exit_record.record(pinned) is False
    assert "exit" not in native_state.read_lifecycle(pinned)


def test_the_recorder_writes_nothing_before_the_runtime_pinned_the_invocation(monkeypatch):
    worker_id = str(uuid.uuid4())
    native_state.update_lifecycle(worker_id, phase="intent")
    _ended(monkeypatch)
    assert native_exit_record.record(worker_id) is False
    assert "exit" not in native_state.read_lifecycle(worker_id)


def test_malformed_code_and_status_are_dropped_not_stored(pinned, monkeypatch):
    _ended(monkeypatch, code="killed\nnow", status="$(id)")
    assert native_exit_record.record(pinned) is True
    exit_ = native_state.read_lifecycle(pinned)["exit"]
    assert exit_["code"] is None and exit_["status"] is None


def _main(monkeypatch, argv):
    """`main` adopts the worker's environment into THIS process (`HOME`, the plugin-state dir), as
    the real hook does in its own. Registered with monkeypatch first, so both are restored after:
    a leaked `HOME` made a later test on the same xdist worker read the tmp dir as the home."""
    monkeypatch.setenv("HOME", os.environ["HOME"])
    monkeypatch.setenv(
        "AGENT_SESSIONS_PLUGIN_STATE_DIR", os.environ["AGENT_SESSIONS_PLUGIN_STATE_DIR"]
    )
    return native_exit_record.main(argv)


def _config(worker_id: str, home: Path) -> None:
    native_state.write_config(
        worker_id,
        {
            "worker_id": worker_id,
            "home": str(home),
            "server_env": {
                "AGENT_SESSIONS_PLUGIN_STATE_DIR": os.environ["AGENT_SESSIONS_PLUGIN_STATE_DIR"]
            },
        },
    )


def test_main_adopts_the_workers_own_config_then_records(pinned, monkeypatch, tmp_path):
    _config(pinned, tmp_path)
    _ended(monkeypatch)
    assert _main(monkeypatch, ["--worker-id", pinned, "--state-dir", str(native_state.root())]) == 0
    assert native_state.read_lifecycle(pinned)["exit"]["result"] == "oom-kill"


def test_main_refuses_another_generations_config(pinned, monkeypatch, tmp_path):
    other = str(uuid.uuid4())
    _config(other, tmp_path)
    (native_state.worker_dir(pinned)).mkdir(parents=True, exist_ok=True)
    shutil.copy(native_state.worker_dir(other) / "config.json", native_state.worker_dir(pinned))
    _ended(monkeypatch)
    assert _main(monkeypatch, ["--worker-id", pinned, "--state-dir", str(native_state.root())]) == 1
    assert "exit" not in native_state.read_lifecycle(pinned)


# --- the reason ----------------------------------------------------------------------------------


LIMIT_ONLY = "killed: out of memory (this session's limit is 8 GB — Settings → Resource limits)"


@pytest.mark.parametrize(
    ("exit_", "life", "reason"),
    [
        (KILLED, {"memory_gib": 8}, LIMIT_ONLY),
        (KILLED, {}, "killed: out of memory"),
        ({"result": "success", "invocation_id": INVOCATION}, {"memory_gib": 8}, None),
        ({"result": "exit-code", "invocation_id": INVOCATION}, {"memory_gib": 8}, None),
        ({"result": "signal", "invocation_id": INVOCATION}, {"memory_gib": 8}, None),
        ({"result": "oom-kill", "invocation_id": "f" * 32}, {"memory_gib": 8}, None),
        (None, {"memory_gib": 8}, None),
        ("oom-kill", {"memory_gib": 8}, None),
    ],
)
def test_only_an_oom_kill_of_the_pinned_invocation_is_named(exit_, life, reason):
    worker_id = str(uuid.uuid4())
    native_state.update_lifecycle(worker_id, invocation_id=INVOCATION, exit=exit_, **life)
    assert native_runtime._ended_reason(worker_id) == reason


def _reading(worker_id, *, ended_at, measured_at, scope="session", used=7.6, invocation=INVOCATION):
    native_state.update_lifecycle(
        worker_id,
        invocation_id=INVOCATION,
        memory_gib=8,
        exit={**KILLED, "at": ended_at},
        memory={
            "level": "critical",
            "working_set": int(used * 1024**3),
            "max": 8 * 1024**3,
            "scope": scope,
            "at": measured_at,
            "invocation_id": invocation,
        },
    )


def test_a_reading_just_before_the_end_is_stated_as_where_it_stood():
    worker_id = str(uuid.uuid4())
    _reading(worker_id, ended_at=1000.0, measured_at=992.0)
    assert native_runtime._ended_reason(worker_id) == (
        "killed: out of memory — last measured at 7.6 of its 8 GB limit "
        "(Settings → Resource limits)"
    )


def test_a_shared_groups_reading_names_no_setting_that_would_not_help():
    worker_id = str(uuid.uuid4())
    _reading(worker_id, ended_at=1000.0, measured_at=995.0, scope="shared", used=1.9)
    reason = native_runtime._ended_reason(worker_id)
    assert reason == (
        "killed: out of memory — last measured at 1.9 of 8 GB in a group this session shares, "
        "limited outside BattleLab"
    )
    assert "Settings" not in reason


@pytest.mark.parametrize(
    "stale",
    [
        {"measured_at": 1000.0 - native_memory.FRESH - 1},  # long before the end
        {"measured_at": 1005.0},  # after the recorded end: not a reading of this ending
        {"measured_at": 995.0, "invocation": "f" * 32},  # another invocation's reading
    ],
)
def test_a_reading_that_is_not_of_this_ending_is_not_stated(stale):
    worker_id = str(uuid.uuid4())
    _reading(worker_id, ended_at=1000.0, **stale)
    assert native_runtime._ended_reason(worker_id) == LIMIT_ONLY


def test_earlier_pressure_never_becomes_the_cause_of_a_later_kill():
    # Hermes on #1651: own-limit pressure long ago (a recovered allocation failure, a critical
    # reading), then a kill the parent's limit caused. Nothing the hook or the reader sees ties
    # the kill to a limit, so no reason ever says which one did it.
    worker_id = str(uuid.uuid4())
    _reading(worker_id, ended_at=5000.0, measured_at=1000.0)
    reason = native_runtime._ended_reason(worker_id)
    assert reason == LIMIT_ONLY and "hit" not in reason


def test_an_unknown_worker_reads_generic():
    assert native_runtime._ended_reason(str(uuid.uuid4())) is None


def _operator_turn(root, sid, binding):
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    journal.append_events(root, binding, [event(operation_id, "Working on it")])
    return operation_id


def _turn(view, turn_id):
    (turn,) = [t for t in view["turns"] if t["turn_id"] == turn_id]
    return turn


def _project(root, sid, binding, reason):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        None,  # its worker is gone
        ended_reason=lambda worker_id: reason if worker_id == binding.worker_id else None,
    )


def test_an_operator_turn_its_worker_left_names_the_limit_and_stays_uncertain(conversation):
    root, sid, binding = conversation
    turn_id = _operator_turn(root, sid, binding)
    generic = _turn(_project(root, sid, binding, None), turn_id)
    named = _turn(_project(root, sid, binding, OOM), turn_id)
    assert generic["reason"] == "the native worker ended before this turn settled"
    # A record that lands after the worker was first seen gone upgrades the words, nothing else.
    assert named["reason"] == OOM
    assert (
        (named["status"], named["code"])
        == (generic["status"], generic["code"])
        == (
            "failed",
            "uncertain",
        )
    )
    public = structured_runtime._snapshot(binding.session_key, _project(root, sid, binding, OOM))
    (shown,) = [t for t in public["turns"] if t["turn_id"] == turn_id]
    assert shown["state"] == "uncertain" and shown["reason"] == OOM


def test_an_agent_run_turn_its_worker_left_is_uncertain_and_names_the_limit(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    target = agent_tests.agent(
        root, sid, binding, monkeypatch, agent_tests.T0, "Watching CI", end=None
    )
    generic = _turn(_project(root, sid, binding, None), target)
    named = _turn(_project(root, sid, binding, OOM), target)
    assert generic["reason"] == "the native worker ended during this turn"
    assert named["reason"] == OOM
    assert (
        (named["status"], named["code"])
        == (generic["status"], generic["code"])
        == (
            "failed",
            "uncertain",
        )
    )
    public = structured_runtime._snapshot(binding.session_key, _project(root, sid, binding, OOM))
    (shown,) = [t for t in public["turns"] if t["turn_id"] == target]
    assert shown["state"] == "uncertain" and shown["reason"] == OOM


def test_a_live_workers_turn_is_never_given_an_ended_reason(conversation):
    root, sid, binding = conversation
    turn_id = _operator_turn(root, sid, binding)
    view = native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        ended_reason=lambda worker_id: OOM,
    )
    assert _turn(view, turn_id)["status"] == "pending"
    assert not _turn(view, turn_id).get("reason")


# --- the real hook, under the real systemd-run shape (skips where the user manager can't) ------


def _user_manager() -> bool:
    if not shutil.which("systemd-run") or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    probe = subprocess.run(
        ["systemd-run", "--user", "--quiet", "--collect", "--wait", "--", "/bin/true"],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="systemd only")
def test_a_real_memory_kill_reaches_the_lifecycle_through_the_hook(tmp_path):
    if not _user_manager():
        pytest.skip("no usable `systemd-run --user` here")
    worker = containment.WorkerIdentity.mint()
    state_dir = str(native_state.root())
    _config(worker.worker_id, tmp_path)
    argv = containment.launch_argv(
        worker, python=sys.executable, state_dir=state_dir, home=str(tmp_path), memory_gib=1
    )
    hook = _property(argv, "ExecStopPost")
    # The launch's containment as it was before #1732 (`OOMPolicy=kill`: a whole-unit `oom-kill`,
    # which a generation launched then can still end with), a 64 MB cap and a child that outgrows
    # it once pinned. Since #1732 the policy is `continue`: see test_native_oom_systemd.py.
    hog = "import time,sys;time.sleep(2);b=[]\nwhile True: b.append(bytearray(10**7))"
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--service-type=exec",
            f"--unit={worker.unit}",
            "--property=ExitType=main",
            "--property=KillMode=control-group",
            "--property=MemoryMax=64M",
            "--property=MemorySwapMax=0",
            "--property=OOMPolicy=kill",
            "--property=TimeoutStopSec=15",
            f"--property=ExecStopPost={hook}",
            "--",
            sys.executable,
            "-I",
            "-c",
            hog,
        ],
        check=True,
        timeout=30,
    )
    shown = subprocess.run(
        ["systemctl", "--user", "show", "--property=InvocationID", "--value", worker.unit],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    group = subprocess.run(
        ["systemctl", "--user", "show", "--property=ControlGroup", "--value", worker.unit],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    native_state.update_lifecycle(
        worker.worker_id, invocation_id=shown, control_group=group, memory_gib=1
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "exit" not in native_state.read_lifecycle(
        worker.worker_id
    ):
        time.sleep(0.25)
    life = native_state.read_lifecycle(worker.worker_id)
    assert life.get("exit", {}).get("result") == "oom-kill", json.dumps(life)
    assert life["exit"]["invocation_id"] == shown
    assert native_runtime._ended_reason(worker.worker_id) == (
        "killed: out of memory (this session's limit is 1 GB — Settings → Resource limits)"
    )
