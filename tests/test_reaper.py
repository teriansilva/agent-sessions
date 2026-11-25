"""Idle-session reaper (#279): selection logic + safety posture.

The load-bearing guarantee is that the reaper only ever targets STALE sessions (detached + idle past
the TTL) and NEVER an active one (attached, or recently-active), is opt-in, and defaults to dry-run
(observes, kills nothing). These pin that without spawning real PTYs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile

import pytest

from agent_sessions import reaper


def _row(key, *, attached=False, last_output_at=None, engine="claude", sid=None):
    return {
        "id": key,
        "engine": engine,
        "sid": sid or key.split(":", 1)[-1],
        "attached": attached,
        "last_output_at": last_output_at,
    }


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", raising=False)
    assert reaper.enabled() is False
    assert reaper.idle_ttl() == 0


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_DRY_RUN", raising=False)
    assert reaper.dry_run() is True
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    assert reaper.dry_run() is False


def test_is_stale_only_detached_and_idle():
    now = 1000.0
    ttl = 600
    assert reaper.is_stale(True, 0.0, now, ttl) is False  # attached → never stale, ever
    assert reaper.is_stale(False, now - 10, now, ttl) is False  # recently active → not stale
    assert reaper.is_stale(False, None, now, ttl) is False  # no signal at all → don't risk it
    assert reaper.is_stale(False, now - 999, now, ttl) is True  # detached + idle past TTL → STALE
    assert reaper.is_stale(False, now - ttl, now, ttl) is True  # exactly at threshold (>=)


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def snapshot(self):
        return list(self._rows)


def test_sweep_selects_only_stale_and_honors_exempt(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "600")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")  # observe only
    monkeypatch.setenv("AGENT_SESSIONS_REAP_EXEMPT", "claude:pinned")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])  # hermetic: no transcript mtimes
    now = 10_000.0
    reg = _FakeRegistry(
        [
            _row("claude:active", attached=True, last_output_at=0.0),  # attached → skip
            _row("claude:fresh", last_output_at=now - 5),  # recent → skip
            _row("claude:pinned", last_output_at=now - 99_999),  # exempt → skip
            _row("claude:stale1", last_output_at=now - 5000),  # STALE
            _row("claude:stale2", last_output_at=now - 700),  # STALE
            _row("claude:noidle", last_output_at=None),  # no signal → skip
        ]
    )

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert set(selected) == {"claude:stale1", "claude:stale2"}


def test_sweep_kills_nothing_in_dry_run(monkeypatch):
    # Dry-run must not invoke the teardown path at all (no PID lookup, no kill, no mirror teardown).
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    calls = {"find": 0, "signal": 0}
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: calls.__setitem__("find", 1))
    monkeypatch.setattr(reaper, "_signal_tree", lambda p, s: calls.__setitem__("signal", 1))
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stale", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert selected == ["claude:stale"]  # still SELECTED + logged...
    assert calls == {"find": 0, "signal": 0}  # ...but nothing was torn down


def test_transcript_mtime_is_the_restart_proof_activity_floor(monkeypatch):
    # The accumulation case: a session silent since before this process started has last_output_at
    # None, but its transcript mtime is old → it IS stale and gets reaped. Conversely, an OLD
    # last_output_at but a RECENT transcript mtime (active again post-restart) is NOT stale.
    from agent_sessions.scanner import Session

    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "600")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")
    now = 100_000.0
    scanned = [
        Session("claude", "silentold", "/x", now - 5000, "hi", False),  # idle 5000s → stale
        Session("claude", "activenow", "/x", now - 5, "hi", False),  # active 5s ago → NOT stale
    ]
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: scanned)
    reg = _FakeRegistry(
        [
            _row("claude:silentold", last_output_at=None),  # never observed live this process
            _row("claude:activenow", last_output_at=now - 9000),  # stale by live signal alone...
        ]
    )

    async def go():
        return await reaper.sweep(reg, now=now)

    # silentold reaped via mtime; activenow saved because its transcript mtime is recent.
    assert asyncio.run(go()) == ["claude:silentold"]


def test_sweep_disabled_returns_empty(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", raising=False)
    reg = _FakeRegistry([_row("claude:x", last_output_at=0.0)])

    async def go():
        return await reaper.sweep(reg, now=1.0)

    assert asyncio.run(go()) == []


def test_real_reap_signals_tree_and_frees_mirror(monkeypatch):
    # Flag off dry-run: the stale session's master PID is looked up, its process group SIGTERM'd,
    # and the VT mirror freed. History (transcript/ring) is untouched — the teardown calls neither
    # clear_scrollback nor _drop_buffer. Exits on SIGTERM (no SIGKILL escalation).
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    monkeypatch.setattr(reaper, "_REAP_GRACE_S", 0)  # don't actually sleep in the test
    sigs = []
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: 4242)
    # The boundary is a captured TREE now, not one group — see `_containment`. Stubbed at that
    # seam so the sweep's own wiring is what this test exercises.
    monkeypatch.setattr(reaper, "_containment", lambda pid: ({pid}, {pid}))
    monkeypatch.setattr(
        reaper,
        "_signal_boundary",
        lambda pids, pgids, sig, cgroup=None: sigs.append((sorted(pids)[0], sig)),
    )
    monkeypatch.setattr(
        reaper, "_boundary_alive", lambda pids, cgroup=None: False
    )  # died on SIGTERM
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stale", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert selected == ["claude:stale"]
    assert sigs == [(4242, reaper.signal.SIGTERM)]  # SIGTERM only — it exited


def test_real_reap_escalates_to_sigkill_when_surviving(monkeypatch):
    # A master that ignores SIGTERM (stays alive past the grace) is escalated to SIGKILL so a reap
    # always frees the session rather than re-logging the same survivor every sweep.
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    monkeypatch.setattr(reaper, "_REAP_GRACE_S", 0)
    sigs = []
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: 99)
    monkeypatch.setattr(reaper, "_containment", lambda pid: ({pid}, {pid}))
    monkeypatch.setattr(
        reaper,
        "_signal_boundary",
        lambda pids, pgids, sig, cgroup=None: sigs.append((sorted(pids)[0], sig)),
    )
    # Survives SIGTERM, and then survives SIGKILL too — so the outcome is `leaked`, which is the
    # answer a caller must not read as a clean teardown.
    monkeypatch.setattr(reaper, "_boundary_alive", lambda pids, cgroup=None: True)
    monkeypatch.setattr(reaper, "_KILL_CONFIRM_S", 0)
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stubborn", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    asyncio.run(go())
    assert sigs == [(99, reaper.signal.SIGTERM), (99, reaper.signal.SIGKILL)]


def test_a_REAL_DTACH_TARGET_that_ignores_signals_is_still_contained(tmp_path):
    """[security] #898 reviews 4 and 5, finding 1 — with REAL `dtach`, because that is where it
    lives and a synthetic stand-in got it wrong twice.

    The first version checked the master PID: a master that exits while its child ignores SIGHUP
    and SIGTERM answered `term` over a live agent. The second checked the master's process GROUP
    and was rebuilt against a leader and child that deliberately shared one — which `dtach` does
    NOT do. It calls `setsid()` on its target so the pty gets its own controlling terminal, so the
    master and the agent are in different groups AND different sessions. Measured on this host
    while writing this: master pgid 1173666, agent pgid 1173667. A group-wide signal aimed at the
    master therefore contains the master and nothing else.

    So the boundary is the process TREE, captured before anything is signalled — and the only
    honest way to test it is through the launcher the feature actually uses.
    """
    import os
    import subprocess
    import sys
    import time

    if shutil.which("dtach") is None:  # pragma: no cover - dtach is a hard dependency here
        pytest.skip("dtach is not installed")

    ready = tmp_path / "ready"
    # SHORT, because AF_UNIX paths are capped at 108 bytes and a pytest tmp_path can exceed it —
    # the socket FILE appears either way and only `connect()` fails, which reads like a slow host.
    sockdir = tempfile.mkdtemp(prefix="as-pty-")
    sock = os.path.join(sockdir, "s")
    target = (
        "import signal,sys,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(300)\n"
    )
    subprocess.run(
        ["dtach", "-n", sock, sys.executable, "-c", target, str(ready)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the target never installed its handlers"

    master = None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            cmd = open(f"/proc/{name}/cmdline", "rb").read()
        except OSError:
            continue
        if b"dtach" in cmd and sock.encode() in cmd:
            master = int(name)
            break
    assert master is not None, "the dtach master was not found"

    pids, _pgids = reaper._containment(master)
    kids = pids - {master}
    assert kids, "the target was not inside the captured boundary"
    # THE PREMISE, asserted rather than assumed: the topology this test exists for is real.
    child = next(iter(kids))
    assert os.getpgid(child) != os.getpgid(master), (
        "dtach did not setsid its target here — the group-only check would have been enough, "
        "and this test is no longer testing what it claims"
    )

    real = reaper._find_master_pid
    reaper._find_master_pid = lambda e, s: master
    try:
        outcome = asyncio.run(reaper.terminate_master("claude", "x", grace_s=0.4))
    finally:
        reaper._find_master_pid = real

    assert outcome == "kill", outcome
    for p in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p, 0)
            raise AssertionError(f"{p} survived the teardown")
    with contextlib.suppress(Exception):
        shutil.rmtree(sockdir)


def test_a_FOREIGN_process_group_is_never_signalled(tmp_path):
    """[security] #898 review 5, finding 3. The validation was thrown away at the signal site.

    `_containment` decides which groups are ours — the leader must be inside the captured tree,
    and it must not be our own — and the old signal path then recomputed `os.getpgid(pid)` and
    `killpg`'d whatever it got, including the group it had just been told to leave alone.
    """
    sent: list[tuple[str, int, int]] = []

    class FakeOs:
        @staticmethod
        def killpg(pg, sig):
            sent.append(("killpg", pg, sig))

        @staticmethod
        def kill(pid, sig):
            sent.append(("kill", pid, sig))

    real_killpg, real_kill = reaper.os.killpg, reaper.os.kill
    reaper.os.killpg, reaper.os.kill = FakeOs.killpg, FakeOs.kill
    try:
        # A REAL pid — this process — whose group was NOT validated as ours to touch. It has to
        # be real: a pid that does not exist makes `os.getpgid` raise, so the recomputation the
        # old path performed would be suppressed and the test would pass against it.
        reaper._signal_boundary({os.getpid()}, set(), reaper.signal.SIGTERM)
    finally:
        reaper.os.killpg, reaper.os.kill = real_killpg, real_kill

    assert sent == [("kill", os.getpid(), reaper.signal.SIGTERM)], sent
    assert not any(s[0] == "killpg" for s in sent), "a group we do not own was signalled"


def test_OUR_OWN_process_group_is_never_a_target():
    """The other disqualifier, and the one that already cost a test runner its life: a boundary
    whose group is OURS must not be offered as a killpg target, or the escalation is a SIGKILL to
    this process."""
    _pids, pgids = reaper._containment(os.getpid())
    assert os.getpgid(0) not in pgids


def test_a_ZOMBIE_leader_is_not_a_live_boundary(tmp_path):
    """The other half, and the one that makes the check usable rather than a permanent alarm.

    The master is spawned by this app, so when it exits it stays in the process table as an
    unreaped child — and a zombie is still a group member. Asked with `killpg(pgid, 0)` alone, a
    perfectly clean teardown reports the group as populated for ever. Measured: the first version
    of this returned `leaked` for a group whose only remaining member was the exited leader.
    """
    import signal as sig
    import subprocess
    import sys
    import time

    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        assert reaper._boundary_alive({leader.pid}) is True
        leader.send_signal(sig.SIGKILL)
        # NOT reaped — no `wait()` — so it is a zombie in its own group, which is the state a
        # torn-down master is in the instant after it dies.
        deadline = time.monotonic() + 10
        while not reaper._alive(leader.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert reaper._boundary_alive({leader.pid}) is False, "a zombie was counted as alive"
    finally:
        with contextlib.suppress(Exception):
            leader.wait(timeout=5)


def test_a_TARGET_that_FORKS_a_survivor_during_teardown_is_still_contained(tmp_path):
    """[security] #898 review 6 — the boundary must survive REPARENTING, not just a signal.

    A pid snapshot answers "who existed when I looked". A target that handles SIGTERM by forking
    a child and then exiting hands that child to init: the tree rooted at the master no longer
    reaches it, the re-capture walks a dead root, and the teardown reports `term` over a live,
    permission-bypassed agent. That is the one answer this function must never give.

    Cgroup membership is inherited across `fork()` and does not change when a process is
    reparented, so the transient scope still contains the survivor. This drives the real thing:
    real `dtach`, a real scope, a target that really forks and really exits.
    """
    import os
    import subprocess
    import sys
    import time

    from agent_sessions import scopedspawn

    if shutil.which("dtach") is None:  # pragma: no cover - dtach is a hard dependency here
        pytest.skip("dtach is not installed")
    scopedspawn.reset_cache_for_tests()
    if not (scopedspawn.enabled() and scopedspawn.available()):  # pragma: no cover
        pytest.skip("transient scopes are unavailable on this host")

    ready = tmp_path / "ready"
    forked = tmp_path / "forked"
    sockdir = tempfile.mkdtemp(prefix="as-pty-")
    sock = os.path.join(sockdir, "s")
    # On SIGTERM: fork a child that ignores everything but SIGKILL, then EXIT. The child is
    # reparented to init and leaves the tree entirely.
    target = (
        "import os,signal,sys,time\n"
        "def onterm(*a):\n"
        "    if os.fork() == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "        open(sys.argv[2], 'w').close()\n"
        "        time.sleep(300)\n"
        "        os._exit(0)\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, onterm)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(300)\n"
    )
    argv, unit = scopedspawn.wrap(
        ["dtach", "-n", sock, sys.executable, "-c", target, str(ready), str(forked)],
        engine="claude",
        session_id="00000000-0000-0000-0000-0000000000ff",
    )
    assert unit is not None, "the launch was not scoped, so there is no durable boundary to test"
    subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the target never installed its handler"

    master = None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            cmd = open(f"/proc/{name}/cmdline", "rb").read()
        except OSError:
            continue
        if b"dtach" in cmd and sock.encode() in cmd:
            master = int(name)
            break
    assert master is not None, "the dtach master was not found"

    # THE PREMISE: the scope really is a boundary of its own, not this process's cgroup.
    cg = reaper._containment_cgroup(master)
    assert cg is not None, "the master shares this app's cgroup; there is no boundary to prove"

    real = reaper._find_master_pid
    reaper._find_master_pid = lambda e, s: master
    try:
        outcome = asyncio.run(reaper.terminate_master("claude", "x", grace_s=1.0))
    finally:
        reaper._find_master_pid = real

    # It forked — the case is real, not hypothetical.
    assert forked.exists(), "the target never forked; this test proved nothing"
    # …and nothing is left in the scope. `term` would be the false success.
    left = reaper._cgroup_members(cg)
    live = [p for p in (left or ()) if p != os.getpid()]
    assert not live, f"{live} survived the teardown inside the scope"
    assert outcome in ("term", "kill"), outcome
    with contextlib.suppress(Exception):
        shutil.rmtree(sockdir)
