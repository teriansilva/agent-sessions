"""`own_basetemp.py` (#2045): every pr-validate run removes its own pytest basetemp.

Each run's basetemps were left in the CI runners' TMPDIR (`/ci`, a 24 GB tmpfs), 2-4 GB apiece,
until `/ci` filled up and every CI job failed with ENOSPC. Pinned here:

- the basetemp goes when pytest exits, pytest's status kept, and the run's leftover processes are
  ended first;
- it goes when the run is killed (forgejo-runner runs no later step after a cancel): by the
  janitor, only once the killed group is gone and nothing holds it, without holding the step's
  log open, also when the outer `sibling_watch` is what was killed;
- a held dir is kept, never removed;
- nothing but a run's own dirs is ever removed;
- every pytest invocation in pr-validate goes through it.
"""

from __future__ import annotations

import importlib.util
import os
import pwd
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo" / "scripts"
SCRIPT = SCRIPTS / "own_basetemp.py"
SELF = "pr-validate / python (pull_request)"

_spec = importlib.util.spec_from_file_location("own_basetemp", SCRIPT)
ob = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(ob)

# A stand-in for pytest. It takes `--basetemp=DIR` like pytest and fills it (with a read-only dir,
# as permission tests leave), then does what FAKE_MODE says.
FAKE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    base = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--basetemp="))
    blob = os.path.join(base, "popen-gw0", "test_x0", "blob")
    os.makedirs(os.path.dirname(blob))
    with open(blob, "wb") as f:
        f.write(b"x" * 4096)
    ro = os.path.join(base, "popen-gw0", "ro")
    os.makedirs(ro)
    open(os.path.join(ro, "f"), "w").close()
    os.chmod(ro, 0o500)
    mode = os.environ.get("FAKE_MODE", "exit")
    env = os.environ

    def spawn(code, *args):
        # In a session of its own, as a daemon a test starts: no group kill reaches it.
        return subprocess.Popen([sys.executable, "-c", code, *args], cwd="/",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True).pid

    if mode == "exit":
        sys.exit(int(env.get("FAKE_RC", "0")))
    if mode == "leak":  # a test's daemon outlives the run, holding nothing
        open(env["FAKE_READY"], "w").write(str(spawn("import time; time.sleep(300)")))
        sys.exit(0)
    if mode == "wait":  # exit once the test says so
        open(env["FAKE_READY"], "w").write("1")
        while not os.path.exists(env["FAKE_GO"]):
            time.sleep(0.05)
        sys.exit(0)
    # "hang": a worker that takes FAKE_HOLD s to die keeps an fd in the basetemp, then notes when
    # it let go. A detached daemon holds NOTHING, then after FAKE_IDLE s reopens the basetemp by
    # name and notes whether it could. And this hangs like a run being cancelled.
    holder = spawn(
        "import sys, time; f = open(sys.argv[1], 'rb'); time.sleep(float(sys.argv[2])); "
        "f.close(); open(sys.argv[3], 'w').write(repr(time.time()))",
        blob, env["FAKE_HOLD"], env["FAKE_CLOSED"])
    idle = spawn(
        "import os, sys, time; time.sleep(float(sys.argv[2])); ok = os.path.exists(sys.argv[1]); "
        "open(sys.argv[3], 'w').write(f'{ok} {time.time()!r}')",
        blob, env.get("FAKE_IDLE", "300"), env.get("FAKE_IDLE_DONE", "/dev/null"))
    open(env["FAKE_READY"], "w").write(f"{holder} {idle}")
    time.sleep(300)
    """
)

FAST = {
    "OWN_BASETEMP_WRAPPER_GRACE_S": "1",
    "OWN_BASETEMP_QUIET_S": "0.5",
    "OWN_BASETEMP_TRACK_S": "0.2",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    t = tmp_path / "t"
    t.mkdir()
    fake = tmp_path / "fake_pytest.py"
    fake.write_text(FAKE)
    monkeypatch.setenv("TMPDIR", str(t))
    monkeypatch.setenv("OWN_BASETEMP_SYSLOG", "0")  # never into the runner's journal
    monkeypatch.setattr(ob, "QUIET_S", 0.2)
    monkeypatch.setattr(ob, "WRAPPER_GRACE_S", 1.0)
    run = t / "pytest-pr7-abc123"
    sibling = t / "pytest-pr7-def456" / "full"  # another run of the same PR, still going
    sibling.mkdir(parents=True)
    (sibling / "keep").write_text("mine")
    started: list[int] = []
    yield t, run, fake, started, sibling
    for pid in started:  # only processes this test started
        if pid > 1 and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return st.rsplit(")", 1)[1].split()[0] != "Z"


def _wait_for(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _janitors(base: Path) -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"--janitor" in argv and str(base).encode() in argv:
            found.append(int(name))
    return found


def _wrapper(base: Path, fake: Path) -> list[str]:
    return [sys.executable, str(SCRIPT), str(base), "--", sys.executable, str(fake)]


def test_a_finished_run_removes_its_basetemp_and_keeps_pytests_status(world):
    _t, run, fake, _started, sibling = world
    base = run / "full"
    r = subprocess.run(
        _wrapper(base, fake),
        env={**os.environ, **FAST, "FAKE_RC": "3"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 3, r.stdout + r.stderr  # pytest's verdict, not the cleanup's
    assert not base.exists()  # its read-only dir included
    assert f"own_basetemp: removed {base}" in r.stdout
    assert run.is_dir(), "the run dir stays: the later pytest steps put their basetemps in it"
    assert (sibling / "keep").read_text() == "mine"
    # The janitor heard "done" and left: a dir made at the same path now is not removed.
    _wait_for(lambda: not _janitors(base), 10, "the janitor did not leave on done")
    base.mkdir()
    time.sleep(1.5)
    assert base.is_dir()


def test_a_daemon_the_run_left_is_ended_before_the_basetemp_goes(world, tmp_path):
    _t, run, fake, started, _sibling = world
    base, ready = run / "full", tmp_path / "ready"
    r = subprocess.run(
        _wrapper(base, fake),
        env={**os.environ, **FAST, "FAKE_MODE": "leak", "FAKE_READY": str(ready)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    daemon = int(ready.read_text())
    started.append(daemon)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "left processes behind; ending them" in r.stdout and str(daemon) in r.stdout
    assert not _alive(daemon), "the run's leftover outlived its step"
    assert not base.exists()


def test_a_basetemp_held_after_the_run_is_kept_then_removed_once_let_go(world, tmp_path):
    """Held by a process outside the run (here: this test). The wrapper keeps it; the janitor
    removes it only after it is let go."""
    _t, run, fake, _started, _sibling = world
    base, ready, go = run / "full", tmp_path / "ready", tmp_path / "go"
    env = {
        **os.environ,
        **FAST,
        "OWN_BASETEMP_JANITOR_GRACE_S": "30",
        # Shorter than the hold below: the janitor's grace is not cut short by its daemon pass.
        "OWN_BASETEMP_END_TRACKED_S": "1",
        "FAKE_MODE": "wait",
        "FAKE_READY": str(ready),
        "FAKE_GO": str(go),
    }
    # A group of its own, as a step has: the janitor also waits for the wrapper's group to end.
    wrapper = subprocess.Popen(
        _wrapper(base, fake), env=env, stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        _wait_for(ready.exists, 30, "the fake pytest never got going")
        with open(base / "popen-gw0" / "test_x0" / "blob", "rb"):
            go.touch()
            out, _ = wrapper.communicate(timeout=60)
            assert wrapper.returncode == 0
            assert f"KEPT {base}: held by {os.getpid()}" in out
            time.sleep(4.0)  # well past END_TRACKED_S, well inside JANITOR_GRACE_S
            assert base.is_dir(), "removed while held"
        _wait_for(lambda: not base.exists(), 30, "the janitor never retried once it was let go")
        time.sleep(1.0)
        assert run.is_dir(), "a normal end's janitor took the run dir from the next pytest step"
    finally:
        if wrapper.poll() is None:
            wrapper.kill()


def _hang_env(tmp_path: Path, hold: float, idle: float = 300) -> dict:
    return {
        **os.environ,
        **FAST,
        "FAKE_MODE": "hang",
        "FAKE_HOLD": str(hold),
        "FAKE_IDLE": str(idle),
        "FAKE_READY": str(tmp_path / "ready"),
        "FAKE_CLOSED": str(tmp_path / "closed"),
        "FAKE_IDLE_DONE": str(tmp_path / "idle_done"),
        "OWN_BASETEMP_TRACK_ACK": str(tmp_path / "tracked"),
    }


def _killed_run_is_cleaned(cmd: list[str], env: dict, world, tmp_path: Path) -> None:
    _t, run, _fake, started, sibling = world
    base, ready, closed = run / "full", tmp_path / "ready", tmp_path / "closed"
    idle_done = tmp_path / "idle_done"
    step = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    )
    try:
        _wait_for(lambda: ready.exists() and " " in ready.read_text(), 30, "never got going")
        holder, idle = (int(p) for p in ready.read_text().split())
        started += [holder, idle]
        acks = tmp_path / "tracked"
        _wait_for(
            lambda: acks.exists() and str(idle) in acks.read_text().split(),
            30,
            "the wrapper never reported the detached daemon",
        )
        os.killpg(step.pid, signal.SIGKILL)  # what a cancel does to the step's group
        step.wait(timeout=10)
        t0 = time.monotonic()
        assert step.stdout is not None
        step.stdout.read()  # EOF only once nothing holds the step's log open
        assert time.monotonic() - t0 < 5, "something outlived the kill holding the step's log"
        _wait_for(lambda: not base.exists(), 60, "the janitor never removed the killed run's dir")
        removed_at = time.time()
        assert closed.exists(), "removed while a worker still held it"
        assert removed_at >= float(closed.read_text())
        # The detached daemon held nothing past the quiet interval, survived the group kill, and
        # then reopened the path: it must still have been there, and gone only after it exited.
        assert idle_done.exists(), "removed while the run's detached daemon still ran"
        reopened, idle_exit = idle_done.read_text().split()
        assert reopened == "True", "the basetemp was gone under the run's detached daemon"
        assert removed_at >= float(idle_exit)
        _wait_for(lambda: not run.exists(), 30, "the killed job's now-empty run dir stayed")
        time.sleep(1.0)
        assert not base.exists(), "something wrote into it after the cleanup"
        assert (sibling / "keep").read_text() == "mine", "another run's dir was touched"
    finally:
        if step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)


def test_a_killed_wrapper_is_cleaned_up_by_its_janitor(world, tmp_path):
    _t, run, fake, _s, _sib = world
    env = _hang_env(tmp_path, hold=1, idle=8)
    _killed_run_is_cleaned(_wrapper(run / "full", fake), env, world, tmp_path)


def test_a_killed_sibling_watch_around_it_is_cleaned_up_too(world, tmp_path):
    """The real nesting: the step runs `sibling_watch.py -- own_basetemp.py -- pytest`, and a
    cancel SIGKILLs the watcher's group. Its guard then stops the command group (#1454), which
    takes the wrapper with it, and the janitor takes over."""
    _t, run, fake, _s, _sib = world
    env = {
        **_hang_env(tmp_path, hold=1, idle=8),
        # Credentials set, so it really watches; the address refuses at once and a failed read
        # keeps the command running.
        "API": "https://127.0.0.1:9",
        "HEAD_SHA": "sha",
        "TOKEN": "tok",
        "SIBLING_WATCH_INTERVAL": "600",
    }
    cmd = [sys.executable, str(SCRIPTS / "sibling_watch.py"), "--self", SELF, "--"]
    _killed_run_is_cleaned(cmd + _wrapper(run / "full", fake), env, world, tmp_path)


def test_a_killed_runs_dir_still_held_at_the_deadline_is_kept(world, tmp_path):
    _t, run, fake, started, _sibling = world
    base = run / "full"
    env = {**_hang_env(tmp_path, hold=120), "OWN_BASETEMP_JANITOR_GRACE_S": "3"}
    step = subprocess.Popen(
        _wrapper(base, fake),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready = tmp_path / "ready"
        _wait_for(lambda: ready.exists() and " " in ready.read_text(), 30, "never got going")
        started += [int(p) for p in ready.read_text().split()]
        janitors = _janitors(base)
        assert janitors
        os.killpg(step.pid, signal.SIGKILL)
        step.wait(timeout=10)
        _wait_for(lambda: not any(_alive(j) for j in janitors), 30, "the janitor never gave up")
        assert base.is_dir(), "removed while a worker still held it"
    finally:
        if step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "rel",
    [
        "pytest-pr7-abc123",  # a run dir is not a basetemp: --basetemp would wipe its siblings
        "",  # TMPDIR itself
        "elsewhere/full",  # not in a run dir
        "pytest-pr/full",
        "pytest-prx-1/full",
        "pytest-pr7-abc123/Full",
        "pytest-pr7-abc123/full/deeper",
        "pytest-pr7-abc123/../pytest-pr7-abc123/full",
    ],
)
def test_only_a_step_dir_of_a_run_dir_is_taken_as_a_basetemp(world, rel):
    t, _run, _fake, _started, sibling = world
    path = f"{t}/{rel}" if rel else str(t)
    assert ob.main([path, "--", sys.executable, "-c", "pass"]) == 2
    assert t.is_dir() and (sibling / "keep").exists()


def test_a_relative_path_a_symlink_and_a_command_with_its_own_basetemp_are_refused(world, tmp_path):
    t, run, _fake, _started, _sibling = world
    run.mkdir()
    assert ob.main(["pytest-pr7-abc123/full", "--", "true"]) == 2
    target = tmp_path / "precious"
    target.mkdir()
    (run / "link").symlink_to(target)
    assert ob.main([str(run / "link"), "--", "true"]) == 2
    (t / "pytest-pr9-link").symlink_to(target)
    assert ob.main([str(t / "pytest-pr9-link" / "full"), "--", "true"]) == 2
    assert ob.main(["--remove", str(t / "pytest-pr9-link")]) == 2
    assert target.is_dir()
    assert ob.main([str(run / "full"), "--", "pytest", "--basetemp", "/x"]) == 2
    assert ob.main([str(run / "full"), "--", "pytest", "--basetemp=/x"]) == 2


def test_remove_takes_a_run_dir_and_nothing_else(world, tmp_path):
    t, run, _fake, _started, sibling = world
    (run / "full" / "a").mkdir(parents=True)
    outside = tmp_path / "pytest-pr7-outside"
    outside.mkdir()
    assert ob.main(["--remove", str(t)]) == 2
    assert ob.main(["--remove", str(outside)]) == 2  # the right name in the wrong place
    assert ob.main(["--remove", str(run / "full")]) == 2  # a basetemp is not a run dir
    assert outside.is_dir() and (run / "full").is_dir()
    assert ob.main(["--remove", str(run)]) == 0
    assert not run.exists()
    assert (sibling / "keep").exists()
    assert ob.main(["--remove", str(run)]) == 0  # already gone: nothing to do
    assert ob.main(["--remove", ""]) == 0  # the setup step never ran


def test_remove_keeps_a_held_run_dir(world, tmp_path):
    _t, run, _fake, started, _sibling = world
    (run / "full").mkdir(parents=True)
    (run / "full" / "f").write_text("")
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; f = open(sys.argv[1]); open(sys.argv[2], 'w').close(); "
            "time.sleep(60)",
            str(run / "full" / "f"),
            str(ready),
        ]
    )
    started.append(holder.pid)
    try:
        _wait_for(ready.exists, 30, "the holder never opened it")
        assert ob.main(["--remove", str(run)]) == 0  # never fails the job over it
        assert run.is_dir()
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_new_run_dirs_are_fresh_short_and_not_created(world, capsys):
    t, _run, _fake, _started, _sibling = world
    assert ob.main(["--new-run-dir", "2045"]) == 0
    a = capsys.readouterr().out.strip()
    assert ob.main(["--new-run-dir", "2045"]) == 0
    b = capsys.readouterr().out.strip()
    assert a != b and Path(a).parent == t and Path(a).name.startswith("pytest-pr2045-")
    assert not Path(a).exists()
    assert ob.run_dir(a) == a
    # No longer than pytest's own default for the runner's user, so no AF_UNIX socket path grows.
    assert len(f"{a}/scoped") <= len(f"{t}/pytest-of-{pwd.getpwuid(os.getuid()).pw_name}/pytest-1")
    assert ob.main(["--new-run-dir", "1; rm -rf /"]) == 2


def test_leftovers_lists_only_this_prs_run_dirs(world, capsys):
    t, run, _fake, _started, _sibling = world
    run.mkdir()
    (t / "pytest-pr70-zzz").mkdir()
    (t / "pytest-of-someone").mkdir()
    assert ob.main(["--leftovers", "7"]) == 0
    # Only the list after the prefix: TMPDIR itself may sit under pytest's own pytest-of-<user>.
    listed = capsys.readouterr().out.strip().split(f"left in {t}: ", 1)[1]
    assert listed == "pytest-pr7-abc123, pytest-pr7-def456"


def _job_steps() -> list[dict]:
    wf = yaml.safe_load((ROOT / ".forgejo" / "workflows" / "pr-validate.yml").read_text())
    return wf["jobs"]["python"]["steps"]


def test_every_pytest_in_pr_validate_runs_under_its_own_basetemp():
    steps = _job_steps()
    runs = [s.get("run", "") for s in steps]
    setup = next(i for i, r in enumerate(runs) if "PYTEST_RUN_TMP=" in r)
    assert "own_basetemp.py --new-run-dir" in runs[setup]
    pytest_steps = [i for i, r in enumerate(runs) if "/bin/pytest" in r]
    assert len(pytest_steps) == 3  # full, scoped, wheel
    leaves = set()
    for i in pytest_steps:
        assert i > setup
        name = steps[i]["name"]
        assert "own_basetemp.py" in runs[i], name
        assert "--basetemp" not in runs[i], name  # the wrapper adds it
        leaf = runs[i].split('"$PYTEST_RUN_TMP/', 1)[1].split('"', 1)[0]
        assert ob.STEP_NAME.match(leaf), name
        leaves.add(leaf)
    assert len(leaves) == 3, "each pytest step needs its own basetemp: --basetemp wipes it"


def test_the_job_ends_with_an_always_step_that_removes_the_run_dir():
    last = _job_steps()[-1]
    assert "always()" in str(last.get("if", ""))
    assert "own_basetemp.py --remove" in last["run"] and "PYTEST_RUN_TMP" in last["run"]


def test_a_daemon_detached_too_late_to_be_reported_is_not_waited_for(world, tmp_path):
    """The stated gap: a descendant that detached within the last TRACK_S before the kill was not
    reported, so the janitor cannot wait for it. Pinned so the limit is a known one: with no
    report at all, the dir goes once the group is gone and nothing holds it, daemon or not."""
    _t, run, fake, started, _sibling = world
    base = run / "full"
    env = {**_hang_env(tmp_path, hold=0.1, idle=60), "OWN_BASETEMP_TRACK_S": "600"}
    step = subprocess.Popen(
        _wrapper(base, fake),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready = tmp_path / "ready"
        _wait_for(lambda: ready.exists() and " " in ready.read_text(), 30, "never got going")
        holder, idle = (int(p) for p in ready.read_text().split())
        started += [holder, idle]
        _wait_for((tmp_path / "closed").exists, 30, "the holder never let go")
        os.killpg(step.pid, signal.SIGKILL)
        step.wait(timeout=10)
        _wait_for(lambda: not base.exists(), 60, "the janitor never removed the killed run's dir")
        assert _alive(idle) and not (tmp_path / "tracked").exists()
    finally:
        if step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)


def test_the_last_step_leaves_a_killed_runs_dir_to_its_janitor(world, tmp_path):
    """A sibling_watch abort is not a cancel: the step exits 3 and the `if: always()` step runs at
    once. It must not remove the run dir while the janitor still waits for a detached daemon."""
    t, run, fake, started, _sibling = world
    base = run / "full"
    env = _hang_env(tmp_path, hold=0.1, idle=8)
    step = subprocess.Popen(
        _wrapper(base, fake),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready, acks = tmp_path / "ready", tmp_path / "tracked"
        _wait_for(lambda: ready.exists() and " " in ready.read_text(), 30, "never got going")
        holder, idle = (int(p) for p in ready.read_text().split())
        started += [holder, idle]
        _wait_for(lambda: acks.exists() and str(idle) in acks.read_text().split(), 30, "no ack")
        os.killpg(step.pid, signal.SIGKILL)
        step.wait(timeout=10)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--remove", str(run)],
            env={**os.environ, **FAST},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0 and f"KEPT {run}" in r.stdout, r.stdout + r.stderr
        idle_done = tmp_path / "idle_done"
        _wait_for(idle_done.exists, 30, "the detached daemon never finished")
        assert idle_done.read_text().split()[0] == "True", "removed under the run's daemon"
        _wait_for(lambda: not run.exists(), 60, "the janitor never finished the job")
    finally:
        if step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)


def test_a_killed_runs_daemon_that_never_exits_is_ended_by_the_janitor(world, tmp_path):
    _t, run, fake, started, _sibling = world
    base = run / "full"
    env = {**_hang_env(tmp_path, hold=0.1, idle=300), "OWN_BASETEMP_END_TRACKED_S": "2"}
    step = subprocess.Popen(
        _wrapper(base, fake),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready, acks = tmp_path / "ready", tmp_path / "tracked"
        _wait_for(lambda: ready.exists() and " " in ready.read_text(), 30, "never got going")
        holder, idle = (int(p) for p in ready.read_text().split())
        started += [holder, idle]
        _wait_for(lambda: acks.exists() and str(idle) in acks.read_text().split(), 30, "no ack")
        os.killpg(step.pid, signal.SIGKILL)
        step.wait(timeout=10)
        _wait_for(lambda: not _alive(idle), 60, "the janitor never ended the run's daemon")
        _wait_for(lambda: not run.exists(), 60, "the janitor never removed the dir after it")
    finally:
        if step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)


def _sleeper(started: list[int]) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    started.append(p.pid)
    _wait_for(lambda: ob._stat(p.pid) is not None, 10, "the sleeper never started")
    return p


def test_ending_tracked_processes_never_signals_a_reused_pid(world):
    """A tracked (pid, start time) whose pid now belongs to another process: never signalled."""
    _t, _run, _fake, started, _sibling = world
    p = _sleeper(started)
    try:
        start = ob._stat(p.pid)[2]
        assert ob._end_tracked({(p.pid, start + 1)}, signal.SIGKILL) == []
        time.sleep(0.3)
        assert p.poll() is None, "a process with a different start time was signalled"
        assert ob._end_tracked({(p.pid, start)}, signal.SIGKILL) == [str(p.pid)]
        assert p.wait(timeout=10) == -signal.SIGKILL
    finally:
        if p.poll() is None:
            p.kill()


def test_without_a_pidfd_nothing_is_signalled(world, monkeypatch):
    _t, _run, _fake, started, _sibling = world
    p = _sleeper(started)

    def no_pidfd(pid):
        raise OSError(38, "pidfd_open")  # ENOSYS: a kernel or Python without pidfds

    monkeypatch.setattr(ob, "_pidfd_open", no_pidfd)
    try:
        assert ob._end_tracked({(p.pid, ob._stat(p.pid)[2])}, signal.SIGKILL) == []
        time.sleep(0.3)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait(timeout=10)
