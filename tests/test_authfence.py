"""The cross-process authorization fence (#887, landed inside #888).

The fence exists because a *shared read* cannot close the window between "authority verified" and
`os.write()`: reading and comparing is not mutual exclusion, so a sibling instance can always land
between them however late the read happens. Only a fence both sides take can order them.

These tests use REAL PROCESSES and a real `flock`. A same-process test cannot prove anything here —
the thing under test is precisely what `threading.Lock` does not do.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import textwrap
import time

import pytest

from agent_sessions import authfence, session_input


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def _child(script: str, lockdir, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["AGENT_SESSIONS_LOCK_DIR"] = str(lockdir)
    env["PYTHONPATH"] = "src"
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_SIBLING_PROCESS_is_excluded_while_the_fence_is_held(lockdir):
    """The property the whole issue is about: two processes, one fence, no overlap."""
    holder = _child(
        """
        import sys, time
        from agent_sessions import authfence
        with authfence.hold():
            print("held", flush=True)
            time.sleep(1.5)
        """,
        lockdir,
    )
    assert holder.stdout.readline().strip() == "held"

    # The sibling must NOT get in while the child holds it.
    t0 = time.monotonic()
    with pytest.raises(authfence.FenceBusy):
        with authfence.hold(timeout=0.3):
            pass
    waited = time.monotonic() - t0
    assert waited >= 0.25, "the acquisition did not actually wait for its budget"

    holder.wait(timeout=10)
    # …and once the child is gone the fence is free.
    with authfence.hold(timeout=2.0):
        pass


def test_the_fence_is_RELEASED_when_a_holder_DIES(lockdir):
    """`flock` release-on-close is the kernel's guarantee, but it is the reason a crash cannot
    wedge the fleet — so it is asserted, not assumed."""
    holder = _child(
        """
        import time
        from agent_sessions import authfence
        with authfence.hold():
            print("held", flush=True)
            time.sleep(30)
        """,
        lockdir,
    )
    assert holder.stdout.readline().strip() == "held"
    with pytest.raises(authfence.FenceBusy):
        with authfence.hold(timeout=0.2):
            pass

    holder.kill()  # SIGKILL — no unwinding, no finally, nothing but the fd closing
    holder.wait(timeout=10)

    with authfence.hold(timeout=5.0):
        pass  # must not raise


def test_the_contention_budget_is_BOUNDED_and_fails_closed(lockdir):
    """An unbounded wait would turn a stuck sibling into a hung delivery."""
    holder = _child(
        """
        import time
        from agent_sessions import authfence
        with authfence.hold():
            print("held", flush=True)
            time.sleep(5)
        """,
        lockdir,
    )
    assert holder.stdout.readline().strip() == "held"
    t0 = time.monotonic()
    with pytest.raises(authfence.FenceBusy):
        with authfence.hold(timeout=0.4):
            pass
    elapsed = time.monotonic() - t0
    assert 0.35 <= elapsed < 3.0, f"the budget was not honoured ({elapsed:.2f}s)"
    holder.kill()
    holder.wait(timeout=10)


def test_the_fence_lives_in_the_SHARED_lock_directory(lockdir):
    """A fence in a per-instance location would silently not be shared by the instances it is
    meant to order — the failure mode would be invisible and total."""
    from agent_sessions import sessionlock

    assert authfence.fence_path().parent == sessionlock.lock_dir()
    with authfence.hold():
        assert authfence.fence_path().exists()


def test_the_fence_is_REENTRANT_ACROSS_acquisitions_not_within_one(lockdir):
    """A fresh fd per acquisition is what makes the cross-process semantics exact.

    Sequential acquisitions must both succeed; that is what the writer does on every chunk-one
    retry, and a leaked fd would wedge the second one.
    """
    for _ in range(5):
        with authfence.hold(timeout=1.0):
            pass


def test_acquiring_the_fence_is_CHEAP_when_uncontended(lockdir):
    """#887 asks for the cost of an ordinary delivery to be measured, not assumed.

    This is the uncontended path, which is every delivery on a single-instance install.
    """
    n = 200
    t0 = time.monotonic()
    for _ in range(n):
        with authfence.hold(timeout=1.0):
            pass
    per_call_ms = (time.monotonic() - t0) / n * 1000
    print(f"\n[authfence] uncontended acquire+release: {per_call_ms:.3f} ms/call")
    assert per_call_ms < 5.0, f"the fence costs {per_call_ms:.2f} ms on an uncontended delivery"


# ==============================================================================================
# The ordering proof, through the production `send_input` boundary.
# ==============================================================================================


@pytest.fixture
def pty_pair():
    import pty

    master, slave = pty.openpty()
    try:
        yield master, slave
    finally:
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_a_SIBLING_withdrawal_in_the_write_WINDOW_sends_ZERO_bytes(pty_pair, lockdir):
    """The exact ordering #887 exists for, with two real processes.

    A sibling instance commits an authority withdrawal in the window between this writer's
    verification and its first byte. Before the fence that window was open: the compare happened
    before the loop, the sibling committed, and the bytes went out under authority that had already
    been withdrawn. A shared *read* cannot close it — reading is not mutual exclusion.

    Now the writer performs its shared-store re-read INSIDE the fence, and the sibling must take
    the same fence to commit. So the writer either goes first (and the withdrawal waits), or the
    sibling goes first and the re-read SEES it. This drives the second ordering and asserts what
    the operator actually cares about: **nothing reached the terminal**.
    """
    import threading

    master, slave = pty_pair
    key = "claude:11111111-1111-1111-1111-111111111111"
    session_input.register_writer(key, master, threading.Lock(), "headless")

    # The shared "authority" both instances agree on — a file, standing in for the missions store.
    authority = lockdir.parent / "authority.txt"
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_text("A")

    # The sibling: takes the fence, withdraws authority, releases. It holds the fence BEFORE the
    # writer starts, so the writer is guaranteed to arrive second — the interleaving is forced,
    # not hoped for (a test that merely races proves nothing when it passes).
    sibling = _child(
        """
        import sys, time
        from pathlib import Path
        from agent_sessions import authfence
        target = Path(sys.argv[1])
        with authfence.hold():
            print("held", flush=True)
            time.sleep(0.6)          # the writer is now blocked on the fence
            target.write_text("B")   # …and the withdrawal commits inside its window
        print("released", flush=True)
        """,
        lockdir,
        str(authority),
    )
    assert sibling.stdout.readline().strip() == "held"

    out = session_input.send_input(
        key,
        b"continue\r",
        require_quiet=False,
        policy_fingerprint=lambda: authority.read_text(),
    )
    sibling.wait(timeout=10)

    assert out.state == "stale", f"expected a refusal, got {out.state}: {out.detail}"
    # THE ASSERTION THAT MATTERS: not the returned verdict, the terminal.
    import fcntl as _f

    _f.fcntl(slave, _f.F_SETFL, os.O_NONBLOCK)
    try:
        got = os.read(slave, 4096)
    except BlockingIOError:
        got = b""
    assert got == b"", f"bytes reached the pty under withdrawn authority: {got!r}"


def test_an_UNCONTENDED_delivery_still_reaches_the_pty(pty_pair, lockdir):
    """The control, and the one that stops the fence from being a way to break delivery.

    Every assertion above is about refusing; this is the ordinary path on a single-instance
    install, which must still write.
    """
    import threading

    master, slave = pty_pair
    key = "claude:22222222-2222-2222-2222-222222222222"
    session_input.register_writer(key, master, threading.Lock(), "headless")

    out = session_input.send_input(
        key, b"hello\r", require_quiet=False, policy_fingerprint=lambda: "unchanged"
    )
    assert out.ok, f"the fence broke an ordinary delivery: {out.state} {out.detail}"
    assert b"hello" in os.read(slave, 1024)


# ==============================================================================================
# Round 7 of the #888 review — the fence must have NO fail-open path.
# ==============================================================================================


def test_a_MUTATION_REFUSES_rather_than_committing_un_ordered(lockdir):
    """An earlier version logged and proceeded on a busy fence. That breaks the guarantee.

    "No byte reaches a pty under a withdrawn authorization" cannot survive a withdrawal that
    commits outside the fence — quietly, in exactly the case where contention says something is
    already unusual. A withdrawal that cannot be ordered must fail and be retried.
    """
    holder = _child(
        """
        import time
        from agent_sessions import authfence
        with authfence.hold():
            print("held", flush=True)
            time.sleep(6)
        """,
        lockdir,
    )
    assert holder.stdout.readline().strip() == "held"

    import agent_sessions.session_input as si

    committed = []
    try:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(si, "MUTATION_FENCE_BUDGET_S", 0.3)
            with pytest.raises(si.AuthorityFenceBusy):
                with si.sessions_transaction(["claude:a"]):
                    committed.append(True)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert committed == [], "the mutation body ran without the cross-process fence"


def test_EVERY_authority_transaction_takes_the_fence(lockdir):
    """Not just the mission one. A per-session opt-out and a global policy change withdraw
    authority exactly as a detach does, and each was process-local."""
    import agent_sessions.session_input as si

    holder = _child(
        """
        import time
        from agent_sessions import authfence
        with authfence.hold():
            print("held", flush=True)
            time.sleep(6)
        """,
        lockdir,
    )
    assert holder.stdout.readline().strip() == "held"
    try:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(si, "MUTATION_FENCE_BUDGET_S", 0.3)
            for name, call in (
                ("sessions_transaction", lambda: si.sessions_transaction(["claude:a"])),
                ("session_transaction", lambda: si.session_transaction("claude:a")),
                ("policy_transaction", lambda: si.policy_transaction()),
            ):
                ran = []
                with pytest.raises(si.AuthorityFenceBusy), call():
                    ran.append(True)
                assert ran == [], f"{name} committed without the cross-process fence"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_PER_SESSION_OPT_OUT_is_in_the_in_fence_fingerprint(tmp_path, monkeypatch):
    """It is checked in `check_precondition`, which runs BEFORE the fence.

    So a sibling flipping `orchestrator_excluded` after that check had its withdrawal land before
    byte one with nothing to catch it. The fingerprint is what the fence re-reads, so it has to
    carry the opt-out too.
    """
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    from agent_sessions import actuator, metadata

    key = "claude:33333333-3333-3333-3333-333333333333"
    mkey = metadata.resolve_key(key)
    fp = actuator._authority_fingerprint(key)

    # FLIP whatever it currently is, rather than assuming it starts False. The suite shares a home
    # between tests, so asserting on an absolute starting value makes this pass or fail on which
    # tests ran before it — which is not the property under test.
    start = bool(metadata.get(mkey).orchestrator_excluded)
    before = fp()
    metadata.patch(mkey, orchestrator_excluded=not start)
    assert fp() != before, "opting a session out did not move the authority fingerprint"

    # …and flipping back returns it, so the field is genuinely part of the tuple rather than the
    # tuple merely being unstable.
    metadata.patch(mkey, orchestrator_excluded=start)
    assert fp() == before


def test_the_OPT_OUT_ROUTE_enters_the_fence_OFF_the_event_loop(auth_cfg):
    """Asserts WHERE the route runs the fence, by asking whether a loop is running inside it.

    Two earlier versions of this proved nothing. The first drove the blocking work through
    `asyncio.to_thread` itself — the fixed pattern — so it passed against the unfixed route. The
    second measured a concurrent request's latency and passed because the request never reached
    the fence. The property that actually differs is whether the transaction is entered on the
    loop thread, and there is an exact test for that: on the loop thread
    `asyncio.get_running_loop()` succeeds; on a worker thread it raises.

    `session_transaction` polls a cross-process `flock` for up to `MUTATION_FENCE_BUDGET_S`.
    Entered on the loop it stalls every other request for the whole budget.
    """
    import asyncio

    from fastapi.testclient import TestClient

    import agent_sessions.routes.pulse as pulse_routes
    import agent_sessions.session_input as si
    from agent_sessions.main import create_app

    on_loop: list[bool] = []
    real = si.session_transaction

    @contextlib.contextmanager
    def _watch(key):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        with real(key):
            yield

    with pytest.MonkeyPatch.context() as m:
        m.setattr(si, "session_transaction", _watch)
        m.setattr(pulse_routes.session_input, "session_transaction", _watch)
        c = TestClient(create_app(auth_cfg), base_url="https://testserver")
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        assert r.status_code == 303, f"login failed: {r.status_code}"
        csrf = c.get("/api/config").json()["csrf"]
        r = c.post(
            "/api/sessions/claude:11111111-1111-1111-1111-111111111111/orchestrator-exclude",
            json={"excluded": True},
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        )

    assert r.status_code == 200, f"the route did not reach the fence ({r.status_code}): {r.text}"
    assert on_loop, "the opt-out route never entered the authorization fence"
    assert not any(on_loop), (
        "the fence was entered on the event-loop thread; a contended flock there stalls every "
        "other request for the whole mutation budget"
    )
