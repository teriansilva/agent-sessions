"""A tool that runs out of memory is killed alone (#1732): the real worker, end to end.

Two layers, both with `fake_native.py` speaking each client's real protocol:

* The real worker process under the substituted service manager (`FakeHost`, every
  `kinds.API_KINDS` client): the agent really starts through the exec trampoline under the
  worker's launch environment (`env -i`, `python -I`), and the reap keeps the agent's own return
  code.
* The real `systemd-run --user` unit with a tiny `MemoryMax` (skipped, with the reason, where no
  user manager can run one): a child that overruns is killed alone, the agent as the victim and
  the same-window two-cause case read as facts, and the negative controls stay generic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import test_native_runtime as runtime_tests
from agent_sessions import native_exec, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_native_runtime import host, project  # noqa: F401 — fixtures

ident = runtime_tests.ident
API_KINDS = sorted(kinds.API_KINDS)
# Each client kind's scripted stand-in. A kind without one fails here, never silently skipped.
SOURCE = {kind: source for source, kind in runtime_tests.KINDS.items()}
AGENT_KILLED = native_runtime.AGENT_KILLED_DURING_OOM
GENERIC = "the native connection closed"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_every_api_kind_has_a_scripted_client():
    assert set(SOURCE) == kinds.API_KINDS


async def _session(project_dir: Path, kind: str) -> tuple[str, str]:
    engine = runtime_tests.ENGINES[SOURCE[kind]]
    key = (await runtime.create_session(engine, str(project_dir), operation_id=ident()))[
        "session_key"
    ]
    worker = native_state.read_session(key.partition(":")[2])["current_worker"]
    return key, worker


async def _until(check, seconds=60.0, what="a condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _exited(worker: str) -> dict:
    async def check():
        life = native_state.read_lifecycle(worker)
        return life if life.get("phase") == "exited" else None

    return await _until(check, what="the worker's exit")


async def _turn(key: str, turn_id: str, *, states=("completed", "failed", "uncertain")):
    async def check():
        snap = await runtime.snapshot(key)
        turn = next((t for t in snap["turns"] if t["turn_id"] == turn_id), None)
        return (snap, turn) if turn is not None and turn["state"] in states else None

    return await _until(check, what=f"turn {turn_id} in {states}")


async def _reason(key: str, turn_id: str, reason: str):
    """The turn's words once its worker is gone (a reason is never given to a live worker's)."""

    async def check():
        snap = await runtime.snapshot(key)
        turn = next((t for t in snap["turns"] if t["turn_id"] == turn_id), None)
        return turn if turn is not None and turn.get("reason") == reason else None

    return await _until(check, what=f"reason {reason!r}")


# --- the real worker under the substituted service manager, every client ----------------------


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_the_real_worker_starts_its_agent_through_the_trampoline(host, project, kind):  # noqa: F811
    (project / "report-oom-adj").touch()
    key, worker = await _session(project, kind)
    try:
        life = native_state.read_lifecycle(worker)
        entries = [
            json.loads(line) for line in (project / "oom-adj.jsonl").read_text().splitlines()
        ]
        (agent,) = [e for e in entries if e["pid"] == life["native_pid"]]
        with open(f"/proc/{life['pid']}/oom_score_adj") as fh:
            worker_value = int(fh.read())
        # The worker's own value is the one it was started with: never changed by it.
        assert worker_value == native_exec.own_value()
        # The agent ranks above it before its first fork, and what it forks inherits that.
        assert agent["agent"] == agent["child"] == native_exec.raised(worker_value)
        assert agent["agent"] > worker_value or worker_value == native_exec.MAX
        # Its environment is the child environment alone: nothing the interpreter added.
        assert not any(k.startswith("PYTHON") or k in {"LC_CTYPE", "LC_ALL"} for k in agent["env"])
        assert agent["env"]["BATTLELAB_SESSION"] == key and agent["cwd"] == str(project)
    finally:
        await runtime.stop(key)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
@pytest.mark.parametrize(("text", "code"), [("CLOSE_THEN_EXIT", 7), ("CLOSE_THEN_KILLSELF", -9)])
async def test_an_agent_that_ends_by_itself_is_reaped_with_its_own_code(
    host,  # noqa: F811
    project,  # noqa: F811
    kind,
    text,
    code,
):
    """Its output ends, then it exits (or is SIGKILLed) half a second later: the worker waits
    for that end instead of stopping it, so the recorded code is the agent's own. Red before
    #1732: the reader's EOF stopped it first and recorded the stop's SIGTERM."""
    key, worker = await _session(project, kind)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=text)
    life = await _exited(worker)
    assert life["native_returncode"] == code
    assert "agent_exit" not in life  # no counter rose: nothing is said about memory
    await _reason(key, turn, GENERIC)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_a_protocol_error_while_the_agent_lives_stops_it_as_before(host, project, kind):  # noqa: F811
    key, worker = await _session(project, kind)
    started = time.monotonic()
    # The generation ends right after the bad line, so this call's own receipt may be lost.
    with contextlib.suppress(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="BADFRAME")
    life = await _exited(worker)
    assert life["native_returncode"] == -15  # the stop path, at once: no reap window
    assert life["reason"].startswith("native protocol violation")
    assert "agent_exit" not in life
    assert time.monotonic() - started < 30


# --- the real unit, with a tiny memory cap ---------------------------------------------------


class TinyHost(native_runtime.Host):
    """The production service manager, with the launch's hard cap scaled down to a test's size and
    no swap, so an overrun is a kill, not a page-out. Its soft limit is lifted: without swap the
    kernel's throttle above `MemoryHigh` (#1643) makes ONE allocating process crawl for minutes
    before it reaches the cap. Everything else is the launch's own argv — `OOMPolicy` included,
    unless a test asks for the old one."""

    def __init__(self, policy: str | None = None):
        self.policy = policy
        self.launched: list[list[str]] = []

    def launch(self, argv: list[str]) -> bool:
        argv = [
            a for a in argv if not a.startswith(("--property=MemoryMax=", "--property=MemoryHigh="))
        ]
        if self.policy is not None:
            argv = [
                f"--property=OOMPolicy={self.policy}"
                if a.startswith("--property=OOMPolicy=")
                else a
                for a in argv
            ]
        at = argv.index("--")
        argv[at:at] = [
            "--property=MemoryMax=256M",
            "--property=MemoryHigh=infinity",
            "--property=MemorySwapMax=0",
        ]
        self.launched.append(argv)
        return super().launch(argv)


def _user_manager() -> str | None:
    """Why a real unit can't run here, or None."""
    if os.environ.get("CI") == "true":
        return "CI runners have no systemd user manager for transient units"
    problem = native_runtime.Host().available()
    if problem is not None:
        return problem
    try:
        probe = native_runtime.Host().run(
            ["/usr/bin/systemd-run", "--user", "--quiet", "--collect", "--wait", "--", "/bin/true"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"systemd-run --user failed: {exc}"
    return None if probe.returncode == 0 else "systemd-run --user cannot start a unit here"


SYSTEMD = _user_manager()
needs_systemd = pytest.mark.skipif(
    SYSTEMD is not None, reason=f"needs a real systemd user manager: {SYSTEMD}"
)


@pytest.fixture
def tiny(host, monkeypatch):  # noqa: F811
    real = TinyHost()
    monkeypatch.setattr(native_runtime, "HOST", real)
    return real


async def _stop(key):
    try:
        await runtime.stop(key)
    except runtime.StructuredError:
        pass


def _unit_file(worker: str, name: str) -> str:
    group = native_state.read_lifecycle(worker)["control_group"]
    return (Path("/sys/fs/cgroup") / group.lstrip("/") / name).read_text()


@needs_systemd
@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_a_child_that_overruns_is_killed_alone(tiny, project, kind):  # noqa: F811
    """Red on `OOMPolicy=kill`: the kernel killed the worker and the agent with the child."""
    key, worker = await _session(project, kind)
    try:
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="OOMCHILD:1")
        _, done = await _turn(key, turn)
        assert done["state"] == "completed", done
        assert done["reply"] == "echo:children:-9"  # the agent saw a failed command
        assert done["oom_kills"] == 1 and "oom_kills_after" not in done
        life = native_state.read_lifecycle(worker)
        assert life["phase"] == "ready"
        os.kill(life["pid"], 0)  # the worker
        os.kill(life["native_pid"], 0)  # and the agent: both alive
        again = ident()
        await runtime.submit_turn(key, operation_id=again, text="hello")
        _, after = await _turn(key, again)
        assert after["reply"] == "echo:hello" and not after.get("oom_kills")
        assert _unit_file(worker, "memory.oom.group").strip() == "0"  # one victim, not the group
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
@pytest.mark.parametrize("kind", API_KINDS)
async def test_the_agent_as_the_victim_is_stated_as_facts(tiny, project, kind):  # noqa: F811
    key, worker = await _session(project, kind)
    try:
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="OOMSELF")
        life = await _exited(worker)
        assert life["native_returncode"] == -9  # its own end, recorded before any stop
        assert life["agent_exit"]["reason"] == "sigkill_during_oom"
        assert life["agent_exit"]["invocation_id"] == life["invocation_id"]
        shown = await _reason(key, turn, AGENT_KILLED)
        assert shown["state"] == "uncertain" and shown["oom_kills"] == 1
        assert "ran out of memory" in shown["reason"] and "agent ran out" not in shown["reason"]
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
async def test_a_tool_oom_and_an_unrelated_sigkill_in_one_window_read_the_same(tiny, project):  # noqa: F811
    key, worker = await _session(project, "codex-app-server")
    try:
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="OOM_THEN_KILLSELF")
        life = await _exited(worker)
        assert life["native_returncode"] == -9
        shown = await _reason(key, turn, AGENT_KILLED)  # never "the agent ran out of memory"
        assert shown["oom_kills"] == 1
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
@pytest.mark.parametrize("end", ["DIE", "KILLSELF"])
async def test_an_earlier_tool_oom_never_labels_a_later_agent_end(tiny, project, end):  # noqa: F811
    """Negative controls: a reconciled tool OOM, then an ordinary exit or an unrelated SIGKILL."""
    key, worker = await _session(project, "codex-app-server")
    try:
        first = ident()
        await runtime.submit_turn(key, operation_id=first, text="OOMCHILD:1")
        _, done = await _turn(key, first)
        assert done["state"] == "completed" and done["oom_kills"] == 1
        second = ident()
        await runtime.submit_turn(key, operation_id=second, text=end)
        life = await _exited(worker)
        assert life["native_returncode"] == (9 if end == "DIE" else -9)
        assert "agent_exit" not in life
        shown = await _reason(key, second, GENERIC)
        assert not shown.get("oom_kills")
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
async def test_two_kills_in_one_turn_are_one_notice_of_two(tiny, project):  # noqa: F811
    key, _ = await _session(project, "codex-app-server")
    try:
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="OOMCHILD:2")
        _, done = await _turn(key, turn)
        assert done["reply"] == "echo:children:-9,-9" and done["oom_kills"] == 2
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
async def test_a_kill_between_turns_is_a_session_level_notice(tiny, project):  # noqa: F811
    key, _ = await _session(project, "codex-app-server")
    try:
        first = ident()
        await runtime.submit_turn(key, operation_id=first, text="OOMLATER")
        await _turn(key, first)
        (project / "release-hog").touch()  # the agent's child overruns after the turn ended

        async def killed():
            codes = project / "hog-codes.txt"
            return codes.exists() and codes.read_text().split() == ["-9"]

        await _until(killed, what="the late child's kill")
        second = ident()
        await runtime.submit_turn(key, operation_id=second, text="hello")
        snap, _ = await _turn(key, second)
        turns = {t["turn_id"]: t for t in snap["turns"]}
        assert turns[first].get("oom_kills_after") == 1
        assert not turns[first].get("oom_kills") and not turns[second].get("oom_kills")
    finally:
        await _stop(key)


@needs_systemd
@pytest.mark.anyio
async def test_the_trampoline_ranks_the_agent_above_the_worker_in_a_real_unit(tiny, project):  # noqa: F811
    (project / "report-oom-adj").touch()
    key, worker = await _session(project, "codex-app-server")
    try:
        life = native_state.read_lifecycle(worker)
        entries = [
            json.loads(line) for line in (project / "oom-adj.jsonl").read_text().splitlines()
        ]
        (agent,) = [e for e in entries if e["pid"] == life["native_pid"]]
        with open(f"/proc/{life['pid']}/oom_score_adj") as fh:
            worker_value = int(fh.read())
        probe = native_runtime.Host().run(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--wait",
                "--pipe",
                "--",
                "/bin/cat",
                "/proc/self/oom_score_adj",
            ]
        )
        assert worker_value == int(probe.stdout)  # what any process of a user unit starts with
        assert agent["agent"] == agent["child"] == native_exec.raised(worker_value) > worker_value
    finally:
        await _stop(key)
