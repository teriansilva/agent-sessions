"""Alive is not started, and started is not briefed (#739).

#732 was closed because its headless launch could not have worked. This file pins the part its
tests could not have caught even if it had: **evidence**. #840 recorded the hazard as an
observation rather than a hypothesis — on 2026-08-25 a `claude` launch in a fresh checkout
produced a live dtach master, a live process and a bound socket, and *no transcript, indefinitely*,
because the workspace trust dialog was waiting. Every liveness signal said healthy.

So the tests here drive the three facts apart and assert that each one is reported for what it is.
The launch itself is proved against a real `dtach` in `test_headless_launch.py`; here the spawn is
stubbed so the STATE MACHINE can be exercised, which is the opposite trade-off and is why both
files exist.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agent_sessions import handoff, headless_dispatch, headless_seed, ptybridge, sessionlock


class FakeProv:
    engine_id = "claude"
    supports_seed_start = True
    new_session_reconciles = False

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.launched_with = None

    def is_present(self):
        return True

    def scan(self):
        return self.rows

    def new_launch_argv(self, native, *, cwd, bypass):
        self.launched_with = (native, cwd, bypass)
        return ["/bin/true"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import tempfile

    # A SHORT socket dir: `sockaddr_un` is 108 bytes and pytest's tmp_path plus a uuid overruns it.
    sockdir = tempfile.mkdtemp(prefix="as-hd-")
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", sockdir)
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    handoff.reset_for_test() if hasattr(handoff, "reset_for_test") else None
    yield tmp_path
    import shutil

    shutil.rmtree(sockdir, ignore_errors=True)


def _stub_spawn(monkeypatch, *, returncode=0, make_socket=True):
    """Stand in for the dtach spawn. The REAL spawn is proved in `test_headless_launch.py`."""

    class P:
        def __init__(self):
            self.returncode = returncode

        async def wait(self):
            return returncode

    async def fake(*argv, **kw):
        if make_socket:
            # dtach creates the socket; the dispatcher waits for it.
            i = argv.index("-n") + 1
            open(argv[i], "w").close()
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


class FakeRegistry:
    """Stands in for `SessionRegistry`. Records that the reader was started, and for WHICH id."""

    def __init__(self):
        self.ensured: list[tuple[str, str]] = []

    async def ensure_headless(self, engine: str, sid: str) -> None:
        self.ensured.append((engine, sid))


@pytest.fixture
def reg():
    return FakeRegistry()


@pytest.fixture(autouse=True)
def _delivers(monkeypatch):
    """Delivery succeeds by default.

    The brief is now sent BEFORE the store-evidence check (a mint-own-ID engine writes its id only
    after its first turn, so awaiting reconciliation first deadlocked — #898 review 2). That makes
    delivery a precondition of almost every case here rather than the last step, so it is stubbed
    once. The cases that are ABOUT delivery override it, and the real thing is exercised end to
    end against a live `dtach` in `test_headless_launch.py`.
    """
    from agent_sessions import headless_seed

    async def ok(key, seed_key, **kw):
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", ok)


@pytest.fixture(autouse=True)
def _no_real_teardown(monkeypatch):
    """A failed dispatch tears the launch down for real. Stubbed by default so the state-machine
    tests do not signal process groups; the abandon behaviour has its own tests below."""
    from agent_sessions import runtime_cleanup

    calls: list[tuple[str, str]] = []

    async def fake(engine, native, **kw):
        calls.append((engine, native))
        return "term"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", fake)
    return calls


@pytest.fixture
def prov(monkeypatch):
    p = FakeProv()
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: p if e == "claude" else None)
    return p


@pytest.mark.anyio
async def test_ALIVE_IS_NOT_STARTED_a_live_process_with_no_store_record_fails(
    env, prov, reg, monkeypatch
):
    """THE regression #840 asks for by name.

    The launch works, the socket is bound, the master is up — and the engine's store never shows
    the session. That is the trust-dialog case, and it must land in a NAMED failure rather than in
    `running`.
    """
    _stub_spawn(monkeypatch)
    prov.rows = []  # the store never learns about it
    sent: list[str] = []

    async def record(key, seed_key, **kw):
        sent.append(key)
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", record)
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="do the thing", start_timeout=0.4
    )
    assert out.launched is True, "the master DID come up — that is the trap"
    assert out.started is False
    # AND NOTHING WAS TYPED (#898 review 3, finding 1). This is a PINNED-ID engine, so its id is
    # known before the launch and there is nothing to wait on — which means the store record can
    # be required BEFORE the brief. A trust dialog is armed, painted and quiet, so it sails
    # through the readiness gate; the store is the only thing that tells it apart from a running
    # agent, and briefing first would type the operator's words plus a submit into a consent
    # prompt with nobody watching.
    assert out.briefed is False
    assert sent == [], "the brief was delivered to a session the engine has no record of"
    assert out.state == "launched"
    assert out.ok is False
    # The reason has to be actionable: "failed" alone sends the operator to the logs.
    assert "own store" in out.reason and "trust" in out.reason


@pytest.mark.anyio
async def test_STARTED_IS_NOT_BRIEFED_a_session_that_never_takes_the_brief_fails(
    env, prov, reg, monkeypatch
):
    """A store record says the engine woke up. It does not say the agent accepted the brief."""
    _stub_spawn(monkeypatch)
    prov.rows = [{"id": "claude:x", "native": None}]

    async def never_ready(key, seed_key, **kw):
        return False, "the session never became ready (bracketed-paste never true)"

    monkeypatch.setattr(headless_seed, "deliver", never_ready)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda prov, native, cwd: True)
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="do the thing", start_timeout=0.4
    )
    # A pinned-id engine reaches `started` first — its store record is what makes the delivery
    # safe — and then the brief is refused, so the dispatch stops there. `ok` requires BOTH, so
    # a store record on its own is not success.
    assert out.briefed is False
    assert out.started is True
    assert out.ok is False
    assert out.state == "started"
    assert "ready" in out.reason
    # …and the launch is not left running.
    assert "abandoned" in out.events


@pytest.mark.anyio
async def test_the_happy_path_reports_BRIEFED_and_only_then_is_ok(env, prov, reg, monkeypatch):
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)

    seen = {}

    async def delivered(key, seed_key, **kw):
        seen["key"] = key
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", delivered)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    assert out.state == "briefed" and out.ok is True
    # A PINNED-ID engine keeps `launched → started → briefed`: its id is known before the launch,
    # so there is nothing to wait for and the store record can gate the delivery (#898 review 3,
    # finding 1). Only a mint-own-id engine has to brief before it can be identified.
    assert out.events == ["launched", "started", "briefed"]
    # The seed is redeemed under the SAME key the lock and the socket are named for.
    assert seen["key"] == out.key


@pytest.mark.anyio
async def test_shell_is_never_a_dispatch_target(env, reg, monkeypatch):
    """Seeding a brief into a bare `bash -l` EXECUTES it. Gated through the handoff picker's own
    capability check, so the two sets cannot drift apart."""

    class Shell(FakeProv):
        engine_id = "shell"
        supports_seed_start = False

    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: Shell())
    with pytest.raises(headless_dispatch.DispatchError):
        await headless_dispatch.dispatch(
            registry=reg, engine="shell", cwd=str(env), brief="rm -rf /"
        )


@pytest.mark.anyio
async def test_an_engine_that_cannot_be_seeded_is_refused_before_anything_spawns(
    env, reg, monkeypatch
):
    class NoSeed(FakeProv):
        supports_seed_start = False

    spawned = []
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: NoSeed())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a))
    with pytest.raises(headless_dispatch.DispatchError):
        await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="x")
    # Refused BEFORE the spawn: a session nobody can brief is a session nobody asked for.
    assert spawned == []


@pytest.mark.anyio
async def test_a_held_lock_means_do_nothing(env, prov, reg, monkeypatch):
    """The single-writer arbiter is the flock, never a check-then-act."""
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(sessionlock, "acquire", lambda key: None)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="x")
    assert out.state == "failed"
    assert "already holds" in out.reason


@pytest.mark.anyio
async def test_a_launch_that_never_hands_off_the_lock_RELEASES_it(env, prov, reg, monkeypatch):
    """Otherwise the session is BUSY for ever with nothing running — the worst of both."""
    _stub_spawn(monkeypatch, returncode=1, make_socket=False)
    released = []

    class L:
        fd = 3

        def transfer(self):
            raise AssertionError("must not transfer a lock the master never took")

        def release(self):
            released.append(True)

    monkeypatch.setattr(sessionlock, "acquire", lambda key: L())
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="x")
    assert out.state == "failed"
    assert released == [True]


@pytest.mark.anyio
async def test_the_brief_is_SANITISED_before_it_is_stored(env, prov, reg, monkeypatch):
    """An `ESC` in a brief could terminate the bracketed paste early and smuggle raw key input
    into the new session. `sanitize_seed` is the one gate, and it runs before the store."""
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)
    captured = {}

    async def deliver(key, seed_key, **kw):
        captured["seed"] = handoff.claim_seed(seed_key)
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", deliver)
    await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="do it\x1b[201~ and this too"
    )
    assert "\x1b" not in (captured.get("seed") or "")


@pytest.mark.anyio
async def test_an_empty_brief_is_refused(env, prov, reg):
    with pytest.raises(headless_dispatch.DispatchError):
        await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="   ")


# ---- #898 review ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_READER_is_started_before_anything_waits_on_output(env, prov, reg, monkeypatch):
    """Finding 1, and the one my own happy-path test could not have caught.

    A `dtach -n` master has nobody attached, so nothing drains it: no scrollback, no first-paint
    observation and no registered writer for the seed to borrow. The registry keeps one
    server-owned reader per live session, but it only sweeps at STARTUP — before this socket
    existed. Without starting it here every real dispatch waits out the readiness timeout.

    Asserted on the ORDER, because starting it after the readiness wait is the same bug.
    """
    order: list[str] = []
    _stub_spawn(monkeypatch)

    async def ensure(engine, sid):
        order.append(f"reader:{sid}")

    monkeypatch.setattr(reg, "ensure_headless", ensure)
    monkeypatch.setattr(
        headless_dispatch,
        "_has_store_record",
        lambda p, n, c: order.append("evidence") or True,
    )

    async def deliver(key, seed_key, **kw):
        order.append("deliver")
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", deliver)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    assert out.ok is True
    assert order[0].startswith("reader:"), order
    assert order.index("deliver") > 0


@pytest.mark.anyio
async def test_a_FAILED_dispatch_does_not_leave_the_agent_running(
    env, prov, reg, monkeypatch, _no_real_teardown
):
    """Finding 2. Past `transfer()` the master owns the lock, so returning a failure without
    tearing down leaves a permission-bypassed agent alive that nobody is watching and nothing will
    brief — and the next retry launches a SECOND one while the first still holds its lock."""
    _stub_spawn(monkeypatch)
    prov.rows = []  # never appears in the engine's store
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
    )
    assert out.ok is False and out.started is False
    assert "abandoned" in out.events
    assert _no_real_teardown, "the launch must actually be torn down"
    assert _no_real_teardown[0][0] == "claude"


@pytest.mark.anyio
async def test_a_BRIEFED_dispatch_is_never_torn_down(
    env, prov, reg, monkeypatch, _no_real_teardown
):
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)

    async def deliver(key, seed_key, **kw):
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", deliver)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    assert out.ok is True
    assert _no_real_teardown == []


@pytest.mark.anyio
async def test_a_failed_TEARDOWN_is_reported_beside_the_real_reason(env, prov, reg, monkeypatch):
    """The dispatch already failed; a failed cleanup is additional bad news, not a replacement for
    the reason the operator needs."""
    from agent_sessions import runtime_cleanup

    _stub_spawn(monkeypatch)
    prov.rows = []

    async def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", boom)
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
    )
    assert "own store" in out.reason, "the original reason survives"
    assert "could not be stopped" in out.reason
    assert any(e.startswith("abandon-failed") for e in out.events)


@pytest.mark.anyio
async def test_a_MINT_OWN_ID_engine_is_REFUSED_before_the_spawn(env, reg, monkeypatch):
    """[security] #898 review 4, finding 2 — and the scope of this feature, stated in a test.

    The whole safety property here is condition 5: the ENGINE'S OWN STORE is what tells a running
    agent apart from a first-run or consent screen, because that screen is armed, painted and
    quiet and satisfies every signal the readiness gate can see. An engine that does not reveal
    its id until after its first turn cannot be asked before the brief is typed — so either the
    dispatch deadlocks, or the operator's words plus a submit land on a modal with nobody
    watching. Refused, before anything is spawned, with a reason that says which.

    A noninteractive bootstrap proof may well exist per engine — a rollout written at startup
    rather than at first turn — but it is a different fact for each and has to be established
    against the real engine. Assuming one is what this test exists to prevent.
    """

    class Minter(FakeProv):
        engine_id = "opencode"
        new_session_reconciles = True

    spawned: list = []
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: Minter())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a))
    with pytest.raises(headless_dispatch.DispatchError) as e:
        await headless_dispatch.dispatch(registry=reg, engine="opencode", cwd=str(env), brief="x")
    assert "does not reveal its session id" in str(e.value)
    assert "refused" in str(e.value)
    assert spawned == [], "nothing may be launched"


@pytest.mark.anyio
async def test_PERMISSION_BYPASS_IS_NOT_THE_DEFAULT(env, prov, reg, monkeypatch):
    """Finding 4, and a policy boundary rather than a bug.

    Every interactive path launches with bypass, but UNATTENDED bypass is a different grant: an
    agent with tool prompts suppressed and nobody watching what it does. #739's own security
    section says it must stay gated by the autonomy tier and approval-required until exercised in
    anger — so the module that implements the mechanism must not be the thing that decides it.
    """
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)

    async def deliver(key, seed_key, **kw):
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", deliver)
    await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    assert prov.launched_with[2] is False, "bypass must be opt-in, not the default"

    await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", bypass=True
    )
    assert prov.launched_with[2] is True, "…and available when a caller states it explicitly"


# ---- #898 review 2 ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_LEAKED_process_group_is_NOT_reported_as_abandoned(env, prov, reg, monkeypatch):
    """[security] #898 review 4, finding 1, at the layer the operator reads.

    Teardown now answers about the process GROUP, and `leaked` means something in it survived
    SIGKILL. `_abandon` used to treat any non-raising cleanup as success, so the record said
    "abandoned" — the operator reads that as "the unattended, permission-bypassed agent is gone"
    while it is still running. A teardown may report only what it achieved.
    """
    from agent_sessions import runtime_cleanup

    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)

    async def never_ready(key, seed_key, **kw):
        return False, "the session never became ready"

    async def leaked(engine, native, **kw):
        return "leaked"

    monkeypatch.setattr(headless_seed, "deliver", never_ready)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaked)

    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.4
    )
    assert out.ok is False
    assert "abandoned" not in out.events, "a surviving process was reported as stopped"
    assert "abandon-leaked" in out.events
    assert "STILL RUNNING" in out.reason
    # …and the ORIGINAL reason survives beside it: the operator needs to know why the dispatch
    # failed as well as that the cleanup did not finish the job.
    assert "never became ready" in out.reason


@pytest.mark.anyio
async def test_a_CLEAN_teardown_still_reports_abandoned(env, prov, reg, monkeypatch):
    """The green half, so the leak check is a fence rather than a blanket refusal to say
    anything."""
    from agent_sessions import runtime_cleanup

    _stub_spawn(monkeypatch)
    monkeypatch.setattr(headless_dispatch, "_has_store_record", lambda *a: True)

    async def never_ready(key, seed_key, **kw):
        return False, "the session never became ready"

    async def clean(engine, native, **kw):
        return "term"

    monkeypatch.setattr(headless_seed, "deliver", never_ready)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", clean)

    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.4
    )
    assert "abandoned" in out.events and "STILL RUNNING" not in out.reason


@pytest.mark.anyio
async def test_CANCELLATION_after_the_spawn_tears_down_before_it_unlocks(
    env, prov, reg, monkeypatch
):
    """[security] #898 review 5, finding 2. Ownership starts when the fd can have been inherited.

    The lock fd is passed to `dtach`, and everything between the spawn and `lock.transfer()` is
    awaited: the spawn itself, `proc.wait()`, the socket poll. A `CancelledError` in that window
    took the "never handed off" branch and called `lock.release()` — which frees the flock for the
    whole shared open file description, the master's copy included — while the master keeps
    running. The next retry then starts a SECOND permission-bypassed agent beside the first with
    no single-writer fence between them.

    So the question is not "did we finish handing over" but "could a process have inherited this",
    and the teardown runs BEFORE the unlock. Shielded, because this path is itself unwinding a
    cancellation and an unshielded `await` in that `finally` is cancelled at its first suspension
    point — precisely when it matters.
    """
    from agent_sessions import runtime_cleanup

    order: list[str] = []

    class SlowProc:
        returncode = 0

        async def wait(self):
            order.append("waiting")
            await asyncio.sleep(30)  # cancelled here
            return 0

    async def fake(*argv, **kw):
        i = argv.index("-n") + 1
        open(argv[i], "w").close()
        return SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def cleanup(engine, native, **kwargs):
        order.append("abandon")
        return "term"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", cleanup)

    real_release = sessionlock.SessionLock.release

    def spy_release(self):
        order.append("release")
        return real_release(self)

    monkeypatch.setattr(sessionlock.SessionLock, "release", spy_release)

    task = asyncio.create_task(
        headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert "abandon" in order, "a cancelled launch was left running"
    assert order.index("abandon") < order.index(
        "release"
    ), "the lock was released before the launch it may have been inherited by was stopped"


@pytest.mark.anyio
async def test_the_launch_is_wrapped_in_a_TRANSIENT_SCOPE(env, prov, reg, monkeypatch):
    """The scope is the containment boundary, not only a resource one (#898 review 6).

    A pid snapshot cannot see a process that did not exist when it was taken, so an agent that
    answers SIGTERM by forking a child and exiting walks out of a tree-rooted teardown. Cgroup
    membership is inherited across fork and survives reparenting, so the scope still holds it —
    which is what lets `terminate_master` PROVE the boundary empty instead of inferring it.
    """
    from agent_sessions import scopedspawn

    seen: list[tuple] = []

    def fake_wrap(argv, *, engine, session_id):
        seen.append((tuple(argv), engine, session_id))
        return ["/usr/bin/systemd-run", "--scope", "--", *argv], "as-claude-test.scope"

    monkeypatch.setattr(scopedspawn, "wrap", fake_wrap)
    spawned: list[tuple] = []

    class P:
        returncode = 0

        async def wait(self):
            return 0

    async def fake(*argv, **kw):
        spawned.append(argv)
        open(argv[argv.index("-n") + 1], "w").close()
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="x")
    assert out.state != "failed", out.reason
    assert len(seen) == 1, "the dispatch launch was not offered to the scope wrapper"
    # It wrapped the DTACH argv, not the agent's own — the master and everything it forks have to
    # be inside, and the master is what dtach forks.
    assert seen[0][0][0].endswith("dtach")
    # …and the wrapped form is what was actually spawned.
    assert spawned and spawned[0][0].endswith("systemd-run")


@pytest.mark.anyio
async def test_a_host_with_NO_SCOPES_still_dispatches(env, prov, reg, monkeypatch):
    """Never refuse a session because isolation is unavailable — the ladder `scopedspawn` already
    documents. The teardown then reports the reduced boundary rather than claiming a clean one."""
    from agent_sessions import scopedspawn

    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    _stub_spawn(monkeypatch)
    out = await headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="x")
    assert out.state != "failed", out.reason
