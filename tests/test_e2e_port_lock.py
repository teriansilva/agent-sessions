"""The e2e preview-port reservation, under concurrency (#1151).

`scripts/e2e_port.sh` is what the web-ci shards source to pick their `vite preview --strictPort`
port on the SHARED runner host. Its contract is a reservation, not a formula: any number of
concurrent jobs — even with IDENTICAL candidate seeds, the exact collision the previous
run-id/job-pid derivations died on — must come away with distinct, held ports; a port already
serving a non-participant must be skipped; and the leak of a run killed before its release trap
fired must be reclaimable. These tests pin all three properties against the REAL script (it is
sourced into a long-lived bash, the same shape the workflow step uses — a sourced shell's pid
is the reservation's liveness anchor).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_port.sh"

# These tests pick REAL ports, so they need a base range no other port protocol on this host
# touches: clear of the reservation's default band (16000-31996), the legacy run-id range
# (46000-61996), the playwright config fallback (41001-45000) and the kernel ephemeral range
# (32768+). 12000 sits alone in that gap.
TEST_BASE = 12000

# The sourced-shell probe. The bash stays alive holding the reservation, so the test can read
# what it picked and assert against it while the lock is LIVE. stdout line 1 = the port.
_ACQUIRE_HOLD = r"""
source {script}
export E2E_PORT_LOCK_ROOT={lock_root}
export E2E_PORT_BASE={base}
export E2E_PORT_SEED={seed}
E2E_PORT="$(e2e_port_acquire {shard})"
echo "$E2E_PORT"
kill -STOP $$
"""


def _start_hold(seed: int, shard: int, lock_root: Path) -> subprocess.Popen:
    """Start a bash that will acquire a port and FREEZE holding it. Returns immediately."""
    script = _ACQUIRE_HOLD.format(
        script=SCRIPT, lock_root=lock_root, base=TEST_BASE, seed=seed, shard=shard
    )
    return subprocess.Popen(
        ["bash", "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )


def _read_port(proc: subprocess.Popen) -> int:
    """Collect one holder's port (a failed acquire prints nothing and exits non-zero)."""
    port = int(proc.stdout.readline().strip())
    proc.port = port  # type: ignore[attr-defined]
    return port


def _hold(seed: int, shard: int, lock_root: Path) -> subprocess.Popen:
    proc = _start_hold(seed, shard, lock_root)
    _read_port(proc)
    return proc


def _release(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGCONT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def lock_root(tmp_path: Path) -> Path:
    root = tmp_path / "ports"
    root.mkdir()
    return root


def test_concurrent_jobs_with_identical_seeds_get_distinct_held_ports(lock_root: Path) -> None:
    # THE regression the previous derivations failed: four jobs, SAME seed, SAME shard — every
    # candidate identical. All four are RUNNING before any port is read: simultaneous
    # acquisition, not one holder after another (review 5313's test-quality note).
    holders = [_start_hold(seed=42, shard=1, lock_root=lock_root) for _ in range(4)]
    try:
        for p in holders:
            _read_port(p)
        ports = {p.port for p in holders}  # type: ignore[attr-defined]
        assert len(ports) == 4, f"collision under identical seeds: {ports}"
        for port in ports:
            # Held: the lock dir exists and names a LIVE holder.
            pid = int((lock_root / str(port) / "pid").read_text())
            assert Path(f"/proc/{pid}").exists()
    finally:
        for p in holders:
            _release(p)


def test_a_held_port_is_not_handed_out_again(lock_root: Path) -> None:
    holder = _hold(seed=7, shard=2, lock_root=lock_root)
    try:
        taken = holder.port  # type: ignore[attr-defined]
        # A second job with the SAME seed and shard must come away with a DIFFERENT port.
        second = _hold(seed=7, shard=2, lock_root=lock_root)
        try:
            assert second.port != taken  # type: ignore[attr-defined]
        finally:
            _release(second)
    finally:
        _release(holder)


def _squat_scenario(lock_root: Path) -> int:
    """The busy-check scenario: a foreign listener sits on the acquire's FIRST candidate.

    The listener binds port 0 (kernel-assigned) and that assigned port becomes
    E2E_PORT_BASE with seed 0 / shard 1 — so candidate 1 IS the squatted port by
    construction, no fixed port anywhere, and two overlapping invocations (separate roots,
    shared network namespace) cannot collide with each other or with anything else. The
    candidate math is asserted before the acquire runs, and the base+seek ceiling is checked
    against the 65535 port limit.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    squatted = server.getsockname()[1]
    try:
        seek = int(os.environ.get("E2E_PORT_SEEK", "50"))
        assert squatted + 4 * (seek - 1) < 65536, "fixture: candidates would cross the port ceiling"
        # seed 0, shard 1: candidate 1 = base + 0 — asserted, not assumed.
        assert squatted + (0 % 4000) * 4 + 1 - 1 == squatted
        acquire_cmd = (
            f"source {SCRIPT}; export E2E_PORT_LOCK_ROOT={lock_root};"
            f" export E2E_PORT_BASE={squatted}; export E2E_PORT_SEED=0;"
            " e2e_port_acquire 1"
        )
        proc = subprocess.run(["bash", "-c", acquire_cmd], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        picked = int(proc.stdout.strip())
        assert picked != squatted, "a serving port was handed out"
        # The busy candidate must not be left held (it was dropped, not kept).
        assert not (lock_root / str(squatted) / "pid").exists()
        assert (lock_root / str(picked) / "pid").exists()
        return picked
    finally:
        server.close()


def test_a_port_serving_a_non_participant_is_skipped(lock_root: Path) -> None:
    # Even a RESERVED port that a foreign process already serves must be skipped — the
    # reservation alone cannot evict a listener.
    _squat_scenario(lock_root)


def test_overlapping_fixture_invocations_with_independent_roots(lock_root: Path) -> None:
    # Review 5320's reproduced failure: the old fixture pinned ONE fixed port, so two
    # concurrent pytest invocations (separate lock roots, shared network namespace) fought
    # over the bind. With the port-0 fixture, overlapping invocations must both succeed.
    results: dict[str, int] = {}
    errors: dict[str, BaseException] = {}

    def run(name: str) -> None:
        root = lock_root / name
        root.mkdir()
        try:
            results[name] = _squat_scenario(root)
        except BaseException as exc:  # pragma: no cover - reported via the assert below
            errors[name] = exc

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"overlapping fixture invocations failed: {errors}"
    assert set(results) == {"a", "b"}


def test_a_dead_run_leak_is_reclaimed(lock_root: Path) -> None:
    # A run killed before its release trap fired leaves the lock behind. A later job must be
    # able to take the port back — the whole point of the pid-file liveness anchor.
    stale = lock_root / str(TEST_BASE)
    stale.mkdir()
    (stale / "pid").write_text("999999")  # no such process
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"source {SCRIPT}; export E2E_PORT_LOCK_ROOT={lock_root}; "
            f"export E2E_PORT_BASE={TEST_BASE}; E2E_PORT_SEED=0 e2e_port_acquire 1",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) == TEST_BASE
    # And the reclaimed lock now names the reclaiming shell.
    holder = int((stale / "pid").read_text())
    assert holder != 999999


def test_release_drops_the_reservation(lock_root: Path) -> None:
    holder = _hold(seed=11, shard=3, lock_root=lock_root)
    port = holder.port  # type: ignore[attr-defined]
    _release(holder)  # the frozen bash exits → but release is the TRAP's job; do it explicitly
    release_cmd = (
        f"source {SCRIPT}; export E2E_PORT_LOCK_ROOT={lock_root};" f" e2e_port_release {port}"
    )
    subprocess.run(["bash", "-c", release_cmd], check=True)
    assert not (lock_root / str(port)).exists()
    # The port is hand-out-able again.
    again = _hold(seed=11, shard=3, lock_root=lock_root)
    try:
        assert again.port == port  # type: ignore[attr-defined]
    finally:
        _release(again)


def test_concurrent_stale_reclaim_hands_the_port_to_exactly_one_job(lock_root: Path) -> None:
    # Review 5313's reproduced race: a stale (dead-pid) lock on the shared first candidate,
    # and several reclaimers arriving together. Unsynchronized, both pass the dead check and
    # both end up owning the port. The acquire lock must serialize the reclaim: one winner,
    # the others walk away with DIFFERENT ports.
    first = TEST_BASE  # seed 0, shard 1
    stale = lock_root / str(first)
    stale.mkdir()
    (stale / "pid").write_text("999999")  # no such process
    procs = [_start_hold(seed=0, shard=1, lock_root=lock_root) for _ in range(4)]
    try:
        ports = [_read_port(p) for p in procs]
        assert len(set(ports)) == 4, f"stale reclaim double-handed a port: {ports}"
        assert ports.count(first) == 1, "the reclaimed port went to more than one job"
        # And the winner's lock names a LIVE holder.
        holder = int((stale / "pid").read_text())
        assert Path(f"/proc/{holder}").exists()
    finally:
        for p in procs:
            _release(p)


def test_workflow_shaped_exhaustion_never_reaches_the_test_command(lock_root: Path) -> None:
    # The integration failure review 5313 found: `export E2E_PORT="$(acquire)"` masks the
    # failure (export's status), and the shards run with an EMPTY port. The workflow's shape —
    # bare assignment under set -e, export separately — must abort the step and never reach
    # anything downstream.
    for offset in (0, 4):
        port = TEST_BASE + offset
        d = lock_root / str(port)
        d.mkdir()
        (d / "pid").write_text(str(os.getpid()))  # this test process is alive
    step = (
        "set -euo pipefail\n"
        f"source {SCRIPT}\n"
        f"export E2E_PORT_LOCK_ROOT={lock_root}\n"
        f"export E2E_PORT_BASE={TEST_BASE}\n"
        "export E2E_PORT_SEEK=2\n"
        'E2E_PORT="$(E2E_PORT_SEED=0 e2e_port_acquire 1)"\n'
        "export E2E_PORT\n"
        "echo SHOULD-NOT-PRINT\n"
    )
    proc = subprocess.run(["bash", "-c", step], capture_output=True, text=True)
    assert proc.returncode != 0, "exhaustion must abort the step, not export an empty port"
    assert "no free preview port" in proc.stderr
    assert proc.stdout.strip() == "", "the downstream command must never run"


def test_acquire_fails_loudly_when_nothing_is_free(lock_root: Path, monkeypatch) -> None:
    # Fail-closed: an exhausted search must exit non-zero with a message, never guess a port.
    monkeypatch.setenv("E2E_PORT_SEEK", "2")
    env = {**os.environ, "E2E_PORT_LOCK_ROOT": str(lock_root), "E2E_PORT_SEEK": "2"}
    # Fill both candidate locks with LIVE holders.
    for offset in (0, 4):
        port = TEST_BASE + offset
        d = lock_root / str(port)
        d.mkdir()
        (d / "pid").write_text(str(os.getpid()))  # this test process is alive
    acquire_cmd = (
        f"source {SCRIPT}; export E2E_PORT_LOCK_ROOT={lock_root};"
        f" export E2E_PORT_BASE={TEST_BASE}; E2E_PORT_SEED=0 e2e_port_acquire 1"
    )
    proc = subprocess.run(
        ["bash", "-c", acquire_cmd],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode != 0
    assert "no free preview port" in proc.stderr
    assert proc.stdout.strip() == ""
