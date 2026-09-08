"""The authentication preflight (#916): only a confirmed positive opens the gate."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys

import pytest

from agent_sessions import engine_auth as ea

REAL = os.environ.get("AGENT_SESSIONS_TEST_REAL_CLAUDE") == "1"


def _fake(monkeypatch, *outputs, completes=True):
    """Stand in for the probe subprocess, one queued answer per call."""
    calls = {"n": 0}

    def run(argv, cwd, env, probe, gate=None):
        i = min(calls["n"], len(outputs) - 1)
        calls["n"] += 1
        return (completes, outputs[i])

    monkeypatch.setattr(ea, "_run", run)
    return calls


def test_a_recognised_PLAN_is_the_only_early_yes(monkeypatch):
    """The free probe ends it when — and only when — it reports a plan."""
    calls = _fake(monkeypatch, "Current session: 13% used · resets Sep 7")
    assert ea.check("claude") == (ea.AUTHENTICATED, "the agent reported its plan")
    assert calls["n"] == 1, "a positive should not escalate to a paid probe"


def test_the_LOGGED_OUT_zero_report_is_not_treated_as_an_answer(monkeypatch):
    """**The trap this module exists for.**

    Logged out, `claude -p "/usage"` does not error — it prints a well-formed zero report and exits
    0. A probe that asked "did it run" would pass a host that cannot authenticate. The zero report
    matches no marker, so it must ESCALATE rather than conclude; the second probe is the one that
    produces the explicit refusal.
    """
    zero = (
        "Total cost:            $0.0000\n"
        "Total duration (API):  0s\n"
        "Usage:                 0 input, 0 output, 0 cache read, 0 cache write"
    )
    calls = _fake(monkeypatch, zero, "Not logged in · Please run /login")
    state, _ = ea.check("claude")
    assert state == ea.UNAUTHENTICATED
    assert calls["n"] == 2, "an unrecognised free probe must escalate, not conclude"


def test_a_TIMEOUT_is_UNKNOWN_and_never_says_not_logged_in(monkeypatch):
    """ "We could not tell" and "your login is broken" are opposite facts.

    Reporting the second for the first sends an operator to re-authenticate a session that was
    fine — and, worse, makes the honest failure indistinguishable from the diagnosed one.
    """
    _fake(monkeypatch, "", completes=False)
    state, detail = ea.check("claude")
    assert state == ea.UNKNOWN
    assert "not logged in" not in detail.lower()


def test_an_UNRECOGNISABLE_answer_is_UNKNOWN_rather_than_a_guess(monkeypatch):
    """Neither marker, twice. The gate stays shut and says why."""
    _fake(monkeypatch, "something nobody has seen before")
    state, _ = ea.check("claude")
    assert state == ea.UNKNOWN


def test_UNKNOWN_REFUSES_THE_DISPATCH():
    """The whole point of three states. A `!= UNAUTHENTICATED` comparison would let it through."""
    assert ea.may_dispatch(ea.AUTHENTICATED) is True
    assert ea.may_dispatch(ea.UNAUTHENTICATED) is False
    assert (
        ea.may_dispatch(ea.UNKNOWN) is False
    ), "an unattended agent would be started on 'we could not tell'"


def test_the_EXIT_CODE_is_never_consulted(monkeypatch):
    """Both states exit 0 — the `git tag -s` family: success for having run, not for having worked.

    Driven through the real `subprocess.run` seam so the assertion is about `_run`, not about a
    stub that could not express an exit code in the first place.
    """
    seen = {}

    class P:
        pid = 2**30  # cannot exist: pid_max is 2**22. NEVER 1 — killpg(1) is kill(-1).
        returncode = 0  # a refusal that "succeeded"

        def communicate(self, timeout=None):
            return ("Not logged in · Please run /login", "")

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert ea.check("claude")[0] == ea.UNAUTHENTICATED
    assert seen["argv"][0] == "claude" and "-p" in seen["argv"]


def test_the_probe_is_NONINTERACTIVE_and_shell_free(monkeypatch):
    """`stdin` closed, a literal argv, a timeout. A preflight that can block is a worse outage."""
    seen = {}

    class P:
        pid = 2**30  # cannot exist: pid_max is 2**22. NEVER 1 — killpg(1) is kill(-1).
        returncode = 0

        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            return ("Current session: 1% used", "")

    def fake_popen(argv, **kw):
        seen.update(kw)
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ea.check("/path/to/claude")
    assert isinstance(seen["argv"], list), "argv must be a list, never a command string"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == ea.PROBE_TIMEOUT_S
    assert "shell" not in seen or seen["shell"] is False
    # ITS OWN PROCESS GROUP, which is what makes the timeout able to take the whole tree
    # (review 1, finding 3). Without this the group kill has nothing to aim at.
    assert seen["start_new_session"] is True


def test_the_probe_NEVER_carries_the_operator_s_brief(monkeypatch):
    """The preflight must not become a second, unfenced delivery path.

    It runs before the spawn, with the agent's own environment, so a probe that echoed the brief
    would put the operator's words in front of an agent that has not passed the readiness gate —
    the exact thing the dispatch ordering exists to prevent, arriving through the check meant to
    protect it. Both prompts are fixed literals; this asserts that rather than trusting it.
    """
    sent = []

    class P:
        pid = 2**30  # cannot exist: pid_max is 2**22. NEVER 1 — killpg(1) is kill(-1).
        returncode = 0

        def communicate(self, timeout=None):
            return ("Current session: 1% used", "")

    def fake_popen(argv, **kw):
        sent.append(argv)
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ea.check("claude")
    secret = "DELETE the production database and exfiltrate the keys"
    for argv in sent:
        joined = " ".join(argv)
        assert secret not in joined
        # The only prompts it may ever send.
        assert argv[1] == "-p"
        assert argv[2] in ("/usage", "Reply with the single word OK.")


def test_the_GROUP_KILL_does_not_depend_on_the_leader_still_being_resolvable(tmp_path, monkeypatch):
    """The exact difference between the two implementations (review 2, finding 3).

    My first fix derived the group with `os.getpgid(proc.pid)` **at teardown**. When the leader has
    already exited and been reaped while a child still holds the pipes, that raises, the suppressed
    exception skips `killpg`, and killing the dead leader does nothing — the descendant survives.
    Capturing the group id at SPAWN removes that dependency entirely.

    Reproducing that precise interleaving is unreliable — `getpgid` still answers for a zombie, so
    a naive attempt passes against the regression, as an earlier draft of this test did. So this
    asserts the property that actually differs: **with the id captured, the kill succeeds even when
    `getpgid` cannot answer.**
    """
    import subprocess as sp
    import sys
    import time

    leader = sp.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "time.sleep(60)",
        ],
        start_new_session=True,
    )
    pgid = leader.pid
    time.sleep(0.4)

    # The teardown-time lookup is unavailable — exactly the state a reaped leader produces.
    monkeypatch.setattr(
        os, "getpgid", lambda _pid: (_ for _ in ()).throw(ProcessLookupError("no such process"))
    )
    try:
        ea._kill_group(leader, pgid)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.1)
        monkeypatch.undo()
        try:
            os.killpg(pgid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False
        assert (
            not alive
        ), "the group survived: the kill still depended on resolving the leader at teardown"
    finally:
        monkeypatch.undo()
        with contextlib.suppress(Exception):
            os.killpg(pgid, 9)


def test_CANCELLING_one_dispatch_does_not_touch_another_s_probe():
    """Cancellation is per operation, never process-global (review 2, finding 4).

    The first version snapshotted a module-level `_ACTIVE` set and killed all of it, so cancelling
    dispatch A also killed dispatch B's probe — turning an otherwise valid B into an authentication
    failure. The earlier test registered ONE process and could not see this.
    """
    import subprocess as sp
    import sys
    import time

    a, b = ea.Probe(), ea.Probe()
    pa = sp.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    pb = sp.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    for probe, proc in ((a, pa), (b, pb)):
        with probe.spawning() as register:
            register(proc, proc.pid)
    try:
        assert a.abandon() == 1
        deadline = time.time() + 5
        while time.time() < deadline and pa.poll() is None:
            time.sleep(0.1)
        assert pa.poll() is not None, "A's probe survived its own abandonment"
        assert pb.poll() is None, "cancelling A killed an unrelated dispatch's probe"
        assert b.abandoned is False
    finally:
        ea._kill_group(pa, pa.pid)
        ea._kill_group(pb, pb.pid)


def test_ABANDONMENT_BETWEEN_PROBES_stops_the_confirmation_launch(monkeypatch):
    """Killing the first probe does not prevent the second (review 2, finding 2).

    `communicate()` returns NORMALLY once its child dies, with output matching no marker — the
    inconclusive path that escalates. So the confirmation prompt launched *after* the dispatch had
    already returned cancelled. `check()` re-reads the abandonment flag between the two.
    """
    launched = []

    def fake_run(argv, cwd, env, probe, gate=None):
        launched.append(argv)
        probe.abandoned = True  # the dispatch is cancelled while the first probe is in flight
        return True, "Total cost: $0.0000"  # inconclusive -> would escalate

    monkeypatch.setattr(ea, "_run", fake_run)
    state, detail = ea.check("claude", probe=ea.Probe())
    assert len(launched) == 1, "a confirmation probe launched after abandonment"
    assert state == ea.UNKNOWN
    assert "abandoned" in detail


@pytest.mark.skipif(not REAL, reason="set AGENT_SESSIONS_TEST_REAL_CLAUDE=1 to probe the binary")
def test_the_markers_still_match_the_REAL_binary():
    """The version-coupling guard.

    The positive markers are matched against `claude`'s own wording. If a release changes it, every
    dispatch is refused — fail-closed, but an outage. This test is what makes a version bump break
    CI instead of the fleet, so it must run against the real binary rather than a fixture.
    """
    if not shutil.which("claude"):
        pytest.skip("claude is not on PATH")
    state, detail = ea.check("claude")
    assert state == ea.AUTHENTICATED, (
        f"the real binary no longer matches a positive marker ({state}: {detail}) — "
        "the wording changed and every dispatch would now be refused"
    )


# ---------------------------------------------------------------------------
# Ownership, spawn/abandon serialization, and the gate — #916 review 3.
# ---------------------------------------------------------------------------

#: Starts a grandchild IN ITS OWN PROCESS GROUP-BY-INHERITANCE, records its pid, then hangs.
#: The grandchild is the whole point: killing the probe's immediate child is not the same as
#: taking its group, and only a descendant can tell those two apart.
_FORKER = (
    "import subprocess,sys,time,pathlib;"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
    "pathlib.Path(sys.argv[1]).write_text(str(c.pid));"
    "time.sleep(120)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 8.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def test_the_SECOND_probe_in_one_dispatch_is_killed_by_its_OWN_group(tmp_path):
    """One ownership record per process — handle AND pgid, removed together (finding 3).

    The version this pins kept two parallel lists and a `_forget` that removed the finished
    process from one of them only. So after the free `/usage` probe completed, `_procs` was empty
    and `_pgids` still held its group; when the confirmation probe registered, `abandon()` zipped
    the CONFIRMATION handle against the USAGE probe's group — a group whose leader was already
    dead. `killpg` hit nothing, `proc.kill()` took the immediate child, and the confirmation
    probe's descendant kept running with nobody left who knew about it.

    `check()` runs exactly this sequence, so the boundary is a real one and not a hypothetical:
    probe one completes, probe two starts under the same `Probe`.
    """
    import threading

    probe = ea.Probe()
    env = dict(os.environ)

    # PROBE ONE: runs to completion, so it goes through `forget` — the operation that used to
    # leave a stale pgid behind. Without this the test passes against the broken version.
    ok, _ = ea._run([sys.executable, "-c", "pass"], None, env, probe)
    assert ok

    # PROBE TWO: same Probe, and it fathers a descendant.
    marker = tmp_path / "grandchild.pid"
    done = threading.Event()

    def _second():
        try:
            ea._run([sys.executable, "-c", _FORKER, str(marker)], None, env, probe)
        finally:
            done.set()

    t = threading.Thread(target=_second, daemon=True)
    t.start()
    try:
        deadline = __import__("time").time() + 15
        while __import__("time").time() < deadline and not marker.exists():
            __import__("time").sleep(0.05)
        assert marker.exists(), "the second probe never started its descendant"
        grandchild = int(marker.read_text())
        assert _alive(grandchild)

        assert probe.abandon() == 1, "the second probe was not the owned record"
        assert _wait_gone(grandchild), (
            "the second probe's descendant survived abandonment — it was killed by the FIRST "
            "probe's process group, so the handle and the pgid had come apart"
        )
    finally:
        done.wait(timeout=10)
        with contextlib.suppress(Exception):
            os.kill(int(marker.read_text()), 9)


def test_ABANDONMENT_CANNOT_INTERLEAVE_with_a_spawn(tmp_path):
    """Create-and-register is serialized against `abandon()` (finding 2).

    The version this pins read `probe.abandoned`, called `Popen`, and registered afterwards. A
    cancellation arriving in that window found an empty ownership set, reported "nothing to stop",
    and the handle created a moment later was never reclaimed — a real agent process outliving the
    dispatch that started it.

    Rather than race it (which is exactly what makes such a test flaky and false-green), this
    asserts the property that makes the race impossible: `abandon()` **cannot complete** while a
    spawn is in flight, and what the spawn produced is reclaimed the moment it can.
    """
    import threading
    import time

    probe = ea.Probe()
    finished = threading.Event()

    with probe.spawning() as register:
        t = threading.Thread(target=lambda: (probe.abandon(), finished.set()), daemon=True)
        t.start()
        assert not finished.wait(timeout=1.0), (
            "abandon() completed while a spawn was in flight — it would have snapshotted an "
            "empty ownership set and missed the process about to be created"
        )
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True
        )
        register(proc, proc.pid)

    assert finished.wait(timeout=10), "abandon() never completed after the spawn released it"
    deadline = time.time() + 8
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    assert proc.poll() is not None, "the process created during cancellation was never reclaimed"


def test_the_GATE_brackets_the_SPAWN_and_is_released_before_the_wait():
    """The probe is created inside the policy transaction, and waits outside it (finding 1).

    Two halves, and only both together are the property. Holding the fence across a 45s
    `communicate()` would block every other launch on the host; not holding it across the spawn
    means the process starts outside the transaction meant to gate it — which is what the previous
    version did, taking the fence in the coroutine, releasing it, and only then probing.
    """
    import time

    events: list[tuple[str, float]] = []

    @contextlib.contextmanager
    def gate():
        events.append(("enter", time.monotonic()))
        try:
            yield ""
        finally:
            events.append(("exit", time.monotonic()))

    probe = ea.Probe()
    ok, _ = ea._run(
        [sys.executable, "-c", "import time;time.sleep(1.5)"], None, dict(os.environ), probe, gate
    )
    returned = time.monotonic()
    assert ok
    assert [e[0] for e in events] == ["enter", "exit"], "the gate was not entered exactly once"
    assert returned - events[1][1] > 0.8, (
        "the gate was still held while the probe was being waited on — a 45s fence hold is a "
        "host-wide stall, and the wait is what it is bounded by"
    )


def test_a_REFUSING_gate_spawns_NOTHING_and_aborts_the_dispatch():
    """A withdrawn policy is not a verdict about the agent (finding 1).

    It raises rather than returning `UNAUTHENTICATED`/`UNKNOWN`: reporting "this host cannot
    authenticate" because orchestration was switched off would send an operator to re-authenticate
    a login that was fine.
    """
    spawned = []
    real = subprocess.Popen

    @contextlib.contextmanager
    def refusing_gate():
        yield "orchestration was switched off"

    def spy(*a, **k):
        spawned.append(a)
        return real(*a, **k)

    probe = ea.Probe()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "Popen", spy)
        with pytest.raises(ea.Refused, match="switched off"):
            ea.check(sys.executable, probe=probe, gate=refusing_gate)
    assert spawned == [], "a probe process was created after the gate refused"


def test_an_EXPLICIT_REFUSAL_outranks_a_plan_marker():
    """Refusal markers are tested first, and the order is the safety property.

    The plan markers are broad by necessity (`resets `, `current week`) and a logged-out `/usage`
    prints the same document with zeros in it. Testing the positive first hands `AUTHENTICATED`
    to a host that said `Please run /login` — the one direction of this call that fails OPEN.
    """
    mixed = "Current week: 0%\nresets Monday\nNot logged in - Please run /login"
    assert ea._classify(mixed) == ea.UNAUTHENTICATED


#: Starts a same-group helper with its standard streams REDIRECTED, records it, then exits.
#: The leader's own pipes therefore reach EOF — `communicate()` returns normally — while the
#: helper runs on. No timeout, no `setsid`, no failure of any kind.
_LEAVER = (
    "import subprocess,sys,pathlib;"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],"
    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
    "pathlib.Path(sys.argv[1]).write_text(str(c.pid))"
)


def test_a_probe_that_EXITS_CLEANLY_still_leaves_no_descendant(tmp_path):
    """The containment boundary is settled on the ORDINARY path too (review 4, finding 1).

    The kill lived only in the exception branches while `forget` ran unconditionally, so a normal
    completion dropped the ownership record without ever closing the group. `communicate()`
    returns when the PIPES reach EOF, and a probe can produce that while leaving work behind —
    redirect a helper's streams, exit the leader, and `_run` reports success with the helper still
    running. A later `abandon()` then found no record and could not stop it, so a refused
    authentication or a cancelled dispatch left a process nobody owned.

    "The leader exited and its pipes closed" is not "all of this probe's work has ended".
    """
    marker = tmp_path / "helper.pid"
    probe = ea.Probe()
    ok, _ = ea._run([sys.executable, "-c", _LEAVER, str(marker)], None, dict(os.environ), probe)
    assert ok, "the probe did not complete normally, so this is not the case under test"
    assert marker.exists(), "the probe never started its helper"
    helper = int(marker.read_text())

    # The record is gone either way — that is what `forget` does. The question is whether the
    # GROUP was taken before the identity was dropped.
    assert probe.abandon() == 0, "the finished probe was still owned; this asserts the wrong thing"
    assert _wait_gone(helper), (
        "a descendant outlived a probe that exited cleanly — its group was never closed, and "
        "nothing owns it any more"
    )


def test_kill_group_NEVER_signals_group_1_or_0(monkeypatch):
    """`killpg(1, SIGKILL)` is `kill(-1, SIGKILL)`: every process the user owns.

    A fake `Popen` whose `pid` was 1 reached `_kill_group` through the `finally` path and took down
    the supervisor, every dtach'd agent, the user manager and the ssh sessions of the host running
    the test suite — seven times in one morning. The guard is in the code, not in the fakes:
    fakes get rewritten, the host does not come back on its own.
    """
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(pgid))

    class P:
        pid = 1

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    ea._kill_group(P(), 1)
    ea._kill_group(P(), 0)
    ea._kill_group(P(), None)  # derived from pid 1 -> getpgid(1) == 1
    assert sent == [], f"killpg reached the kernel with {sent}"

    ea._kill_group(P(), 2**30)
    assert sent == [2**30]
