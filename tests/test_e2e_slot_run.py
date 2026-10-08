"""The host-wide e2e slot cap, under concurrency (#1151).

`scripts/e2e_slot_run` is what every Playwright job wraps its browser run in, so that the shared
runner host never runs more than N browser suites at once however many PRs are iterating. Its
contract: at most N commands hold a slot at any instant; everyone else waits and then runs; the
command's own exit status comes back untouched; a slot frees itself when its holder dies (kernel
flock, no pid files); and a child that OUTLIVES the command — the leaked `vite preview` class —
never keeps the slot. These tests pin each property against the real script.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_slot_run"


def _env(slot_dir: Path, slots: int, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        E2E_SLOT_DIR=str(slot_dir),
        E2E_SLOTS=str(slots),
        E2E_SLOT_POLL_S="0.05",
        E2E_SLOT_WAIT_S="60",
    )
    env.update(extra)
    return env


def _run(slot_dir: Path, slots: int, *cmd: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *cmd],
        env=_env(slot_dir, slots, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def slot_dir(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def test_never_more_than_N_commands_hold_a_slot_at_once(slot_dir: Path, tmp_path: Path) -> None:
    """Six contenders, two slots: every one runs, and the overlap never exceeds two.

    Each command appends `+` on entry and `-` on exit to one shared log (O_APPEND keeps lines
    whole); replaying the log gives the concurrency at every instant.
    """
    log = tmp_path / "log"
    body = f'echo + >> "{log}"; sleep 0.4; echo - >> "{log}"'
    procs = [
        subprocess.Popen(
            [str(SCRIPT), "bash", "-c", body],
            env=_env(slot_dir, 2),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(6)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0] * 6

    depth = peak = 0
    for mark in log.read_text().split():
        depth += 1 if mark == "+" else -1
        peak = max(peak, depth)
    assert log.read_text().split().count("+") == 6, "a contender never ran"
    assert peak == 2, f"the cap was not held (peak concurrency {peak}, cap 2)"


def test_the_command_exit_status_comes_back_untouched(slot_dir: Path) -> None:
    assert _run(slot_dir, 1, "true").returncode == 0
    assert _run(slot_dir, 1, "bash", "-c", "exit 3").returncode == 3


def test_a_command_exiting_75_is_not_mistaken_for_a_busy_slot(
    slot_dir: Path, tmp_path: Path
) -> None:
    """75 is flock's 'conflict' code here. Passed through, it would make the caller loop think
    every slot was busy and run the command AGAIN — a second test run nobody asked for."""
    counter = tmp_path / "runs"
    r = _run(slot_dir, 2, "bash", "-c", f'echo x >> "{counter}"; exit 75')
    assert r.returncode == 1
    assert counter.read_text().split() == ["x"], "the command was re-run after exiting 75"


def test_a_killed_holder_frees_its_slot(slot_dir: Path, tmp_path: Path) -> None:
    """No pid files, no reclamation: the kernel drops the flock when its holder dies."""
    started = tmp_path / "started"
    holder = subprocess.Popen(
        [str(SCRIPT), "bash", "-c", f'touch "{started}"; sleep 60'],
        env=_env(slot_dir, 1),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for(started)
        assert (
            _run(slot_dir, 1, "true", E2E_SLOT_WAIT_S="0").returncode == 124
        ), "the single slot should be held"
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        assert (
            _run(slot_dir, 1, "true", E2E_SLOT_WAIT_S="5").returncode == 0
        ), "the slot stayed locked after its holder was killed"
    finally:
        _reap_group(holder)


def test_a_child_that_outlives_the_command_does_not_keep_the_slot(
    slot_dir: Path, tmp_path: Path
) -> None:
    """The leaked-`vite preview` shape: the command backgrounds a long-lived child and returns.

    Without `flock -o` that child inherits the lock descriptor and holds the slot for as long as
    it lives — one leak per killed shard would quietly shrink the cap to zero.
    """
    pidfile = tmp_path / "child.pid"
    r = _run(
        slot_dir,
        1,
        "bash",
        "-c",
        f'setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > "{pidfile}"',
    )
    assert r.returncode == 0
    child = int(pidfile.read_text())
    try:
        assert _alive(child), "the fixture's leaked child should still be running"
        assert (
            _run(slot_dir, 1, "true", E2E_SLOT_WAIT_S="0").returncode == 0
        ), "a child that outlived the command kept the slot"
    finally:
        _kill(child)


def test_no_free_slot_fails_closed_without_running_the_command(
    slot_dir: Path, tmp_path: Path
) -> None:
    started = tmp_path / "started"
    ran = tmp_path / "ran"
    holder = subprocess.Popen(
        [str(SCRIPT), "bash", "-c", f'touch "{started}"; sleep 60'],
        env=_env(slot_dir, 1),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for(started)
        r = _run(slot_dir, 1, "touch", str(ran), E2E_SLOT_WAIT_S="1")
        assert r.returncode == 124
        assert "failing closed" in r.stderr
        assert not ran.exists(), "the command ran without a slot"
    finally:
        _reap_group(holder)


@pytest.mark.parametrize("bad", ["0", "x", "-1"])
def test_a_nonsense_slot_count_is_refused(slot_dir: Path, bad: str) -> None:
    r = _run(slot_dir, 1, "true", E2E_SLOTS=bad)
    assert r.returncode == 2
    assert "E2E_SLOTS" in r.stderr


def test_every_playwright_job_runs_its_browsers_under_the_cap() -> None:
    """The cap only means something if every browser-heavy job takes a slot."""
    root = SCRIPT.parent.parent / ".forgejo" / "workflows"
    web_ci = (root / "web-ci.yml").read_text()
    appmode = (root / "appmode-e2e.yml").read_text()
    assert "e2e_slot_run npm run test:e2e" in web_ci
    assert "e2e_slot_run npx playwright test" in appmode


# helpers ---------------------------------------------------------------------------------------


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _reap_group(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


def test_every_slot_caller_reads_the_same_cap_knob() -> None:
    """The cap only means anything if every caller agrees on it (see the script header).

    Both workflows that wrap a browser run in e2e_slot_run must pass E2E_SLOTS from the one repo
    variable with the same fallback, so the cap is a single, revertable knob. The fallback is 4:
    one PR's full shard set, sized for example-host once the host had headroom again.
    """
    root = Path(__file__).resolve().parent.parent / ".forgejo" / "workflows"
    knob = "E2E_SLOTS: ${{ vars.E2E_SLOTS || '4' }}"
    callers = [p for p in sorted(root.glob("*.yml")) if "e2e_slot_run" in p.read_text()]
    assert {p.name for p in callers} >= {"web-ci.yml", "appmode-e2e.yml", "pr-visual.yml"}, callers
    for p in callers:
        assert knob in p.read_text(), f"{p.name} does not pass the shared E2E_SLOTS knob"


def test_the_command_runs_niced(slot_dir: Path) -> None:
    """Browsers share the org runners with every repo's validations; they run at nice 10."""
    r = _run(slot_dir, 1, "sh", "-c", "nice")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "10", r.stdout
    r = _run(slot_dir, 1, "sh", "-c", "nice", E2E_NICE="0")
    assert r.stdout.strip().splitlines()[-1] == "0", r.stdout


def test_a_bad_nice_value_fails_closed(slot_dir: Path) -> None:
    r = _run(slot_dir, 1, "true", E2E_NICE="ten")
    assert r.returncode == 2
    assert "E2E_NICE" in r.stderr
