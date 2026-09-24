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
import pathlib
import threading
import time

import pytest

from agent_sessions import (
    engines,
    handoff,
    headless_dispatch,
    headless_seed,
    ptybridge,
    sessionlock,
    start_evidence,
)


class FakeProv:
    engine_id = "claude"
    supports_seed_start = True
    new_session_reconciles = False
    # #853 P3: every capability answer comes from the manifest, so a fake stands in for the real
    # engine by carrying ITS engine's real manifest (subclasses below switch `engine_id`).

    @property
    def manifest(self):
        return engines.registry._BY_ID[self.engine_id].manifest  # tests patch `engines.get`

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


@pytest.mark.anyio
async def test_an_INELIGIBLE_engine_is_refused_WITHOUT_being_probed(env, prov, reg, monkeypatch):
    """The preflight LAUNCHES A PROCESS, so it belongs behind the eligibility fences (review 1,
    finding 1).

    I first placed it at the top of `dispatch()` and reasoned about it as a cheap credential read.
    It is not: it runs `claude -p` in the project directory, possibly metered. Sitting there it
    probed engines that were about to be refused as ineligible anyway, and — worse — ran before
    `on_key` and `authorize`, so a superseded dispatch or a withdrawn policy could still start
    agent work that the later fence then rejected.

    A mint-own-id engine is the cheapest way to assert the ordering: it is refused unconditionally,
    so if the probe still runs, the probe is in front of the fence.
    """
    probed = []
    monkeypatch.setattr(
        headless_dispatch.engine_auth,
        "check",
        lambda *a, **k: (probed.append(a), (headless_dispatch.engine_auth.AUTHENTICATED, ""))[1],
    )
    prov.new_session_reconciles = True  # a mint-own-id engine
    with pytest.raises(headless_dispatch.DispatchError) as e:
        await headless_dispatch.dispatch(
            registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
        )
    assert "first turn" in str(e.value)
    assert probed == [], "an ineligible engine was probed before it was refused"


@pytest.mark.anyio
async def test_an_UNAUTHENTICATED_host_never_spawns(env, prov, reg, monkeypatch):
    """A host whose agent cannot log in must not be handed an operator's brief (#916).

    Start evidence cannot catch this: a session with absent credentials registers in 0.80s and one
    with expired credentials in 1.35s, showing no authentication signal at all. Both would pass the
    readiness gate, take the brief at a prompt that cannot act on it, and report `running`. So the
    refusal has to happen before the spawn, and nothing may be spawned when it fires.
    """
    spawned = _stub_spawn(monkeypatch)
    monkeypatch.setattr(
        headless_dispatch.engine_auth,
        "check",
        lambda *a, **k: (headless_dispatch.engine_auth.UNAUTHENTICATED, "not logged in"),
    )
    # NOT a `DispatchError` any more, and that is the reordering working (review 2, finding 1):
    # the probe now runs after `on_key`, inside the single-writer lock and behind `authorize`. Past
    # that point a key has been minted, so a refusal is a settled OUTCOME the caller reconciles,
    # not an exception thrown before anything was recorded.
    #
    # `start_timeout` stays short so a REGRESSION fails fast rather than waiting out the readiness
    # budget — a red-proof that takes 90s to fail is one nobody runs.
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
    )
    assert out.ok is False
    assert "cannot authenticate" in (out.reason or "")
    assert not spawned, "an agent was spawned on a host that cannot authenticate"


@pytest.mark.anyio
async def test_an_UNKNOWN_auth_answer_never_spawns(env, prov, reg, monkeypatch):
    """ "We could not tell" refuses too, and does NOT claim the login is broken.

    A `!= UNAUTHENTICATED` comparison would have let this through, which is the whole reason the
    check is tri-state and the gate is `may_dispatch`. The message matters as much as the refusal:
    telling an operator they are logged out because a probe timed out sends them to fix something
    that was never wrong.
    """
    spawned = _stub_spawn(monkeypatch)
    monkeypatch.setattr(
        headless_dispatch.engine_auth,
        "check",
        lambda *a, **k: (headless_dispatch.engine_auth.UNKNOWN, "the probe timed out"),
    )
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
    )
    assert out.ok is False
    msg = out.reason or ""
    assert "could not confirm" in msg
    assert "not logged in" not in msg.lower(), "an unknown was reported as a confirmed logout"
    assert not spawned


@pytest.fixture(autouse=True)
def _auth_ok(monkeypatch):
    """Every dispatch now runs an authentication preflight (#916) before it spawns.

    Left unstubbed these tests would shell out to the real `claude` on every case — slow, and
    dependent on whoever is logged in on the machine running them. Stubbed to AUTHENTICATED so the
    suite keeps testing what it is about.

    **That makes this fixture a blind spot on purpose**, so the gate itself is covered explicitly
    by `test_an_UNAUTHENTICATED_host_never_spawns` and `test_an_UNKNOWN_auth_answer_never_spawns`
    below, which opt out. A test that stubs a gate can never prove the gate is there.
    """
    monkeypatch.setattr(
        headless_dispatch.engine_auth,
        "check",
        lambda *a, **k: (headless_dispatch.engine_auth.AUTHENTICATED, "stubbed"),
    )


def _stub_started(monkeypatch, started=True, *, on_read=None):
    """Say whether the ENGINE reports the session as started, at the seam actually consulted.

    #916 moved that seam for `claude`. The transcript store cannot answer "did it start" — the
    JSONL is written on the first turn, which is the brief the gate withholds — so claude is read
    through a start-evidence adapter instead, and `_has_store_record` is no longer on its path.
    Tests that stubbed only the old function were patching a door production no longer opens: the
    stub returned True and the dispatch still never reached `briefed`.

    Both are stubbed here, so a test states the FACT ("the engine says it started") rather than the
    mechanism, and neither seam moving again silently disarms it.
    """

    def _store(*_a, **_kw):
        if on_read is not None:
            on_read()
        return started

    def _adapter(_native, _cwd, **_kw):
        if on_read is not None:
            on_read()
        return (start_evidence.FOUND, "") if started else (start_evidence.ABSENT, "no entry")

    monkeypatch.setattr(headless_dispatch, "_has_store_record", _store)
    monkeypatch.setitem(headless_dispatch._START_EVIDENCE, "claude-sessions", _adapter)


def _stub_spawn(monkeypatch, *, returncode=0, make_socket=True):
    """Stand in for the dtach spawn. The REAL spawn is proved in `test_headless_launch.py`.

    `headless_dispatch._popen`, not `asyncio.create_subprocess_exec` (#904 rev 7, finding 2): the
    launch fence has to be held ACROSS the spawn, and holding a `threading.Lock` across an
    `await` on the event loop is the #888 deadlock — the viewer attach path calls `bump_epoch()`
    synchronously on that same loop. So the lock and the spawn run together on a worker thread,
    and the door this stubs is the one production now uses.
    """

    class P:
        def __init__(self):
            self.returncode = returncode

        def wait(self):
            return returncode

    def fake(argv, **kw):
        if make_socket:
            # dtach creates the socket; the dispatcher waits for it.
            i = list(argv).index("-n") + 1
            open(argv[i], "w").close()
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", fake)


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
    # The reason has to be ACTIONABLE, and it must not assert a cause it did not check (#916).
    # This used to require the literal word `trust`, which pinned a guess: the shipped message
    # named a first-run prompt as "the usual cause", and the dispatch that actually mattered —
    # into an already-trusted folder — failed identically and was told the same story. The
    # message now reports what was observed and offers the screens as a likelihood, so the
    # assertion pins the observation and the honest hedge rather than the guess.
    assert "did not register a live session" in out.reason
    assert "most likely" in out.reason, "a guess is being stated as a fact again"


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
    _stub_started(monkeypatch)
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
    _stub_started(monkeypatch)

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
    monkeypatch.setattr(headless_dispatch.subprocess, "Popen", lambda a, **k: spawned.append(a))
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
    _stub_started(monkeypatch)
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
    _stub_started(monkeypatch, on_read=lambda: order.append("evidence"))

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
    _stub_started(monkeypatch)

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
    # THE COMPOSITION IS THE GUARANTEE, not the wording. This asserted `"own store"`, which was
    # the transcript-store era's phrasing; #916 reads a different artifact for claude and says so.
    # What must not change is that BOTH facts reach the operator: why the dispatch failed, and
    # that the agent it may have left behind could not be stopped. Losing either to the other is
    # the failure this test exists for.
    assert "did not register a live session" in out.reason, "the original reason survives"
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
    monkeypatch.setattr(headless_dispatch.subprocess, "Popen", lambda a, **k: spawned.append(a))
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
    _stub_started(monkeypatch)

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
    _stub_started(monkeypatch)

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
    _stub_started(monkeypatch)

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
    # The launched process stays alive until the teardown stops it, as the real `dtach` does. A
    # flat sleep kept the worker thread busy for 30 s after the assertions had already passed.
    killed = threading.Event()

    class SlowProc:
        returncode = 0

        def wait(self):
            order.append("waiting")
            killed.wait(30)  # cancelled here
            return 0

    def fake(argv, **kw):
        i = list(argv).index("-n") + 1
        open(argv[i], "w").close()
        return SlowProc()

    monkeypatch.setattr(headless_dispatch, "_popen", fake)

    async def cleanup(engine, native, **kwargs):
        order.append("abandon")
        killed.set()
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

        def wait(self):
            return 0

    def fake(argv, **kw):
        spawned.append(tuple(argv))
        open(argv[list(argv).index("-n") + 1], "w").close()
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", fake)
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="x", start_timeout=0.3
    )
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
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="x", start_timeout=0.3
    )
    assert out.state != "failed", out.reason


@pytest.mark.anyio
async def test_the_LAUNCH_lands_in_the_directory_that_was_APPROVED_not_the_name(
    env, prov, reg, monkeypatch, tmp_path
):
    """#904 review 3, finding 4. Every check on the path is a check on a NAME.

    The route compares the resolved cwd, `authorize` compares it again inside the launch fence,
    and then the kernel resolves `cwd=` after all of that — so a project repointed in the last
    window still launches somewhere nobody approved. There is no earlier place to move the check
    to; the gap is between the last possible check and the syscall.

    An open descriptor is not a name. It refers to the inode that was there when the approved
    path was resolved, and no later rename or symlink swap moves it.

    Red against a spawn that passes the path through.
    """
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / "marker").write_text("this is the one the operator saw")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "marker").write_text("somewhere else entirely")

    seen: dict[str, str] = {}

    class P:
        returncode = 0

        def wait(self):
            return 0

    def fake(argv, **kw):
        # THE RENAME HAPPENS HERE — after every check, at the moment of the spawn, which is the
        # only window this finding is about.
        approved.rename(tmp_path / "moved-away")
        decoy.rename(approved)
        seen["cwd"] = str(kw.get("cwd"))
        # READ IT HERE, while the descriptor is open — which is exactly when the CHILD resolves
        # it. Reading after the spawn returns would be reading a closed fd, and would fail for a
        # reason that has nothing to do with the property under test.
        seen["marker"] = (pathlib.Path(seen["cwd"]) / "marker").read_text()
        i = list(argv).index("-n") + 1
        open(argv[i], "w").close()
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", fake)
    await headless_dispatch.dispatch(
        engine="claude", cwd=str(approved), brief="go", registry=reg, start_timeout=0.3
    )

    # The cwd handed to the spawn still resolves to the directory the operator approved, even
    # though its NAME now belongs to something else.
    assert seen["marker"] == "this is the one the operator saw"


@pytest.mark.anyio
async def test_the_LAUNCH_FENCE_does_not_block_the_EVENT_LOOP(env, prov, reg, monkeypatch):
    """#904 review 7, finding 2. The fence must be held across the spawn — a check released before
    it is a check again — but the first version held `session_input._lock`, a `threading.Lock`,
    across an `await` ON THE LOOP.

    The viewer attach path calls `bump_epoch()` synchronously on that same loop, so an attach
    arriving while the spawn was suspended blocked the loop: the spawn could never complete, the
    lock was never released, and neither side could make progress — #888's deadlock in a new
    place.

    So the ordering is driven for real: the launch enters the fence, and the attach happens while
    it is inside. What must not hang is the attach.

    Red against a fence held across an event-loop await.
    """
    import uuid as _uuid

    from agent_sessions import session_input

    key = f"claude:{_uuid.uuid4()}"
    inside = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_spawn(argv, **kw):
        # Inside the fence, on a worker thread. The loop must still be live: this sleep is what a
        # real `create_subprocess_exec` suspension looked like, and it is exactly the window an
        # attach used to arrive in.
        loop.call_soon_threadsafe(inside.set)
        time.sleep(0.4)
        i = list(argv).index("-n") + 1
        open(argv[i], "w").close()

        class P:
            returncode = 0

            def wait(self):
                return 0

        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", slow_spawn)
    _stub_started(monkeypatch)

    async def delivered(k, seed_key, **kw):
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", delivered)

    launch = asyncio.ensure_future(
        headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    )
    await asyncio.wait_for(inside.wait(), timeout=10)

    # THE QUESTION, ASKED FROM THE LOOP: while a launch is inside its fence, can this thread
    # still take `session_input._lock`? That is precisely what `session_stream`'s attach path
    # needs — it calls `bump_epoch()` synchronously, from here.
    #
    # Asked with a TIMEOUT rather than by calling `bump_epoch` outright, because the failure
    # being guarded against is a hang: a test that reproduces it by hanging is a test that hangs.
    # `acquire(timeout=...)` gives the same answer and always returns.
    #
    # Under a fence held across an event-loop await the holder IS this thread, so the lock can
    # never be acquired and the launch can never progress to release it — deadlock. Under the
    # fence held on a worker, the worker finishes its spawn and releases, and this succeeds.
    got = session_input._lock.acquire(timeout=5)
    if got:
        session_input._lock.release()
    assert got, "the launch fence was held across an event-loop await"

    out = await asyncio.wait_for(launch, timeout=20)
    # …and the launch still happened, so this does not pass by refusing to launch.
    assert out.launched, out.reason
    # …nor by never entering the fence: `bump_epoch` is the real call the attach path makes.
    session_input.bump_epoch(key)


def _late_spawn(monkeypatch, order, *, delay=0.4, entered=None):
    """A `_popen` that takes its time — the window a deadline or a cancellation lands in.

    Records `popen` when it actually spawns, which is the event the caller must not be able to
    outrun: whatever `dispatch()` said, if this line runs afterwards there is an unattended,
    permission-bypassed agent nobody was ever told about.
    """

    class P:
        returncode = 0

        def wait(self):
            return 0

    def slow(argv, **kw):
        if entered is not None:
            entered.set()
        time.sleep(delay)
        i = list(argv).index("-n") + 1
        open(argv[i], "w").close()
        order.append("popen")
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", slow)


@pytest.mark.anyio
async def test_a_SPAWN_TIMEOUT_does_not_leave_a_worker_that_can_still_LAUNCH(
    env, prov, reg, monkeypatch
):
    """#904 review 8, finding 2. Cancelling an await cancels nothing on a thread.

    `wait_for(to_thread(_fenced_spawn), timeout=…)` reads like a bounded spawn and is not one. When
    the deadline fired, `dispatch()` returned "the launch could not be spawned", the cleanup tore
    down a session that did not exist yet and released the single-writer lock and the directory
    handle — and the worker, still inside the fence, then went on to `_popen()`. The result is the
    exact outcome this whole module exists to prevent: an unattended agent nobody wrote down,
    outside the teardown that would have stopped it, with the lock free for a retry to start a
    second one beside it.

    The property is an ORDER: the spawn cannot happen after the answer. Red against a deadline
    that abandons the worker — the launch lands after `dispatch()` has already reported that
    nothing was launched.
    """
    order: list[str] = []
    _late_spawn(monkeypatch, order, delay=0.4)
    monkeypatch.setattr(headless_dispatch, "SPAWN_TIMEOUT_S", 0.1)
    _stub_started(monkeypatch)

    out = await asyncio.wait_for(
        headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go"),
        timeout=20,
    )
    order.append("returned")
    # EVERY CHANCE TO SPAWN BEHIND OUR BACK. "It has not launched yet" is the failure state, not
    # the passing one — an abandoned worker is still out there, and asserting only on what has
    # happened by the time the answer lands would call that clean.
    await asyncio.sleep(0.8)

    assert not out.launched, "the timeout branch is not the one under test"
    # EITHER it never spawned — the worker saw the caller had given up — OR it spawned while the
    # caller was still holding it. What must never happen is a spawn after the answer.
    assert "popen" not in order or order.index("popen") < order.index("returned"), order


@pytest.mark.anyio
async def test_a_CANCELLED_REQUEST_does_not_leave_a_worker_that_can_still_LAUNCH(
    env, prov, reg, monkeypatch
):
    """#904 review 8, finding 2, the other trigger: the request goes away mid-spawn.

    `mission_dispatch.run` handles `CancelledError` explicitly — it settles the mission `failed`
    and KEEPS the durable record precisely because a cancellation can land after the spawn. That
    honesty is worth nothing if the spawn happens after the frame that could tear it down has
    already unwound: recovery then looks for a session the engine's store does not know yet, and
    the agent that appears a moment later belongs to nobody.

    Red against a cancellation that abandons the worker.
    """
    import threading

    order: list[str] = []
    entered = threading.Event()
    _late_spawn(monkeypatch, order, delay=0.4, entered=entered)
    _stub_started(monkeypatch)

    task = asyncio.ensure_future(
        headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 10)
    assert entered.is_set(), "the spawn never started, so nothing was cancelled mid-spawn"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=20)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    order.append("cancelled")
    await asyncio.sleep(0.8)

    # The spawn is INSIDE the cancelled frame, not after it — so the teardown in `finally` is a
    # teardown of something that exists, rather than of nothing while the agent starts behind it.
    assert "popen" in order, "the worker had not spawned yet, so it was still out there"
    assert order.index("popen") < order.index("cancelled"), order


@pytest.mark.anyio
async def test_a_REFUSAL_BEFORE_THE_DIRECTORY_HANDLE_still_frees_the_session(
    env, prov, reg, monkeypatch
):
    """#904 review 8, found by CI. The cleanup could itself fail, and then nothing was cleaned.

    `dirfd` was declared partway down the try block and read by the `finally`, so anything that
    raised above that line — here an engine whose launch binary is not an absolute path, which is
    a refusal `ptybridge` is right to make — reached the teardown with the name unbound. The
    `UnboundLocalError` replaced the launcher's real reason with a crash AND aborted the rest of
    the `finally`, so `lock.release()` never ran: the session was BUSY for the life of the
    process, and every retry was refused by a lock whose holder had already gone.

    Two assertions, because either alone would pass against half a fix: the operator gets the
    reason, and the next attempt is a LAUNCH rather than a BUSY.

    Red against a `dirfd` bound inside the block the `finally` guards.
    """
    seen: list[str] = []

    def _not_absolute(native, **kw):
        seen.append(native)
        return ["claude"]  # not an absolute path — `ptybridge.launch_argv` refuses it

    monkeypatch.setattr(prov, "new_launch_argv", _not_absolute)

    # THE LAUNCHER'S OWN REFUSAL, reaching the caller as the kind of error it is: nothing was
    # spawned, so this is a dispatch that did not happen and the operator can fix it and retry.
    with pytest.raises(headless_dispatch.DispatchError) as e:
        await asyncio.wait_for(
            headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go"),
            timeout=20,
        )
    assert "absolute path" in str(e.value), str(e.value)

    # …AND THE LOCK IS BACK. Nothing was ever spawned, so a session left BUSY here is one nothing
    # can ever reach again — and the lock is keyed on the id the refused launch minted.
    assert seen, "the provider was never asked, so this proves nothing about the refusal"
    lock = sessionlock.acquire(f"claude:{seen[0]}")
    assert lock is not None, "the refused launch left the session locked with nothing running"
    lock.release()


@pytest.mark.anyio
async def test_the_AUTH_PREFLIGHT_takes_its_FENCE_OFF_the_event_loop(env, prov, reg, monkeypatch):
    """#916 review 3, findings 1 and 4 — one test, because they are one mistake.

    The previous version took `session_input.launch_fence` **in the coroutine**, read `authorize`,
    released it, and only then scheduled the probes on a worker. Both halves were wrong:

    * the fence is a `threading.Lock`, so acquiring it on the loop stalls every other task —
      measured at a 50 ms heartbeat arriving 401 ms late — which is the #888 deadlock shape the
      main spawn already had to be moved off the loop to avoid;
    * and the probe processes were then created *outside* the transaction that decision belonged
      to, so a policy withdrawn in the gap still got a real `claude -p` started.

    Both go away by handing the fence to the worker as a context manager entered around the spawn.
    This asserts the property directly rather than by timing: **no fence acquisition during a
    dispatch happens on the loop thread**, and the preflight is the one that proves it, since it
    is handed a gate it must enter.

    Red against a fence taken in the coroutine (the loop's ident appears in `threads`), and red
    against a preflight given no gate at all (`gate is None`).
    """
    import threading

    from agent_sessions import session_input

    loop_thread = threading.get_ident()
    threads: list[int] = []
    real_fence = session_input.launch_fence

    @contextlib.contextmanager
    def spy_fence(*a, **kw):
        threads.append(threading.get_ident())
        with real_fence(*a, **kw) as epoch:
            yield epoch

    monkeypatch.setattr(session_input, "launch_fence", spy_fence)

    def fake_check(binary, *, cwd=None, env=None, probe=None, gate=None):
        # Stands in for `engine_auth.check` WITHOUT spawning a real agent, but enters the gate
        # exactly where `_run` does — around the spawn — so the seam under test is the real one.
        assert gate is not None, "the preflight was handed no policy gate to spawn inside"
        assert threading.get_ident() != loop_thread, "the preflight itself ran on the loop"
        with gate() as why_not:
            assert not why_not
        return headless_dispatch.engine_auth.AUTHENTICATED, "gated"

    monkeypatch.setattr(headless_dispatch.engine_auth, "check", fake_check)
    _stub_spawn(monkeypatch)
    _stub_started(monkeypatch)

    async def delivered(key, seed_key, **kw):
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", delivered)

    # `authorize` MUST BE PASSED, or this test is vacuous against the code it names: the reviewed
    # version's coroutine-side fence sat behind `if authorize is not None`, so with the default
    # `None` the branch never runs and "no fence on the loop" is true for the wrong reason.
    authorized_on: list[int] = []

    def authorize(epoch):
        authorized_on.append(threading.get_ident())
        return ""

    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", authorize=authorize
    )
    assert out.ok is True, out.reason
    # Two acquisitions: the probe's and the spawn's. Both belong on a worker.
    assert len(threads) >= 2, f"the fence was entered {len(threads)} time(s), expected the probe's"
    assert (
        authorized_on and loop_thread not in authorized_on
    ), "the policy decision was made on the event loop, so the lock it is made under was too"
    assert loop_thread not in threads, (
        "a launch fence was acquired on the event loop — a blocking lock there stalls every other "
        "task, including the attach path that must be able to take it"
    )


@pytest.mark.anyio
async def test_a_POLICY_WITHDRAWN_at_the_probe_refuses_WITHOUT_a_login_verdict(
    env, prov, reg, monkeypatch
):
    """A refusing gate aborts the dispatch; it does not become a claim about the agent (#916 r3).

    `Refused` is deliberately not `UNAUTHENTICATED` or `UNKNOWN`. Reporting "this host cannot
    authenticate" because orchestration was switched off mid-probe would send an operator to
    re-authenticate a login that was never broken — the same category error the tri-state exists
    to prevent, one layer out.
    """
    spawned = _stub_spawn(monkeypatch)

    def refusing_check(binary, *, cwd=None, env=None, probe=None, gate=None):
        raise headless_dispatch.engine_auth.Refused("orchestration was switched off")

    monkeypatch.setattr(headless_dispatch.engine_auth, "check", refusing_check)
    out = await headless_dispatch.dispatch(
        registry=reg, engine="claude", cwd=str(env), brief="go", start_timeout=0.3
    )
    assert out.ok is False
    assert "switched off" in (out.reason or "")
    assert "authenticate" not in (out.reason or ""), "a policy refusal was reported as a logout"
    assert not spawned


@pytest.mark.anyio
async def test_an_AUTHORIZE_that_RAISES_is_a_refusal_not_an_escaping_exception(
    env, prov, reg, monkeypatch
):
    """`dispatch()` must always return an outcome carrying a reason (#916 review 3, follow-up).

    Moving the policy check into the probe's gate dropped the `except` that used to wrap it, and
    the enclosing `try` has only a `finally` — so an `authorize` that raised escaped `dispatch()`
    entirely and left the settlement with no `DispatchOut` to write about the attempt. `authorize`
    is operator-supplied and reads a project store off disk, so "it threw" is a reachable state,
    not a hypothetical.
    """
    spawned = _stub_spawn(monkeypatch)

    def exploding_check(binary, *, cwd=None, env=None, probe=None, gate=None):
        with gate():
            pass  # the gate calls `authorize`, which raises

    monkeypatch.setattr(headless_dispatch.engine_auth, "check", exploding_check)

    def authorize(epoch):
        raise RuntimeError("the project store is unreadable")

    out = await headless_dispatch.dispatch(
        registry=reg,
        engine="claude",
        cwd=str(env),
        brief="go",
        authorize=authorize,
        start_timeout=0.3,
    )
    assert out.ok is False
    assert "could not be authorized" in (out.reason or "")
    assert "RuntimeError" in (out.reason or ""), "the reason names nothing the operator can act on"
    assert not spawned, "an agent was spawned after the authorization crashed"


@pytest.mark.anyio
async def test_CANCELLATION_abandons_the_probe_OFF_the_event_loop(env, prov, reg, monkeypatch):
    """#916 review 4, finding 2. Moving the launch fence off the loop did not cover teardown.

    `Probe.abandon()` takes the ownership lock the worker holds across `Popen`, then signals a
    process group and waits on it — all blocking. Called straight from the coroutine's `finally`
    it stalled every other task for as long as a spawn took: measured at a 30 ms heartbeat
    arriving 599 ms late against a 600 ms `Popen`.

    The serialization is deliberate and is kept — it is what stops a cancellation missing a
    process created a moment later — so what moves is the WAIT, not the lock. Asserted on thread
    identity rather than on a duration, because a timing assertion on a loaded CI host is the
    flake this repo has just spent a day removing (#918).
    """
    import threading

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    abandoned_on: list[int] = []
    real_abandon = headless_dispatch.engine_auth.Probe.abandon

    def spy(self):
        abandoned_on.append(threading.get_ident())
        return real_abandon(self)

    monkeypatch.setattr(headless_dispatch.engine_auth.Probe, "abandon", spy)

    started = asyncio.Event()

    def slow_check(binary, *, cwd=None, env=None, probe=None, gate=None):
        # Inside the preflight, on its worker — the window a cancellation lands in.
        loop.call_soon_threadsafe(started.set)
        time.sleep(1.5)
        return headless_dispatch.engine_auth.AUTHENTICATED, "stubbed"

    monkeypatch.setattr(headless_dispatch.engine_auth, "check", slow_check)
    _stub_spawn(monkeypatch)
    _stub_started(monkeypatch)

    task = asyncio.ensure_future(
        headless_dispatch.dispatch(registry=reg, engine="claude", cwd=str(env), brief="go")
    )
    await asyncio.wait_for(started.wait(), timeout=15)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert abandoned_on, "the probe was never abandoned on the cancellation path"
    assert loop_thread not in abandoned_on, (
        "abandonment ran on the event loop, where its lock acquisition and its kill/wait block "
        "every other task for the length of a spawn"
    )


@pytest.mark.anyio
async def test_opencode_maintenance_refusal_precedes_auth_and_real_spawn(
    env, prov, reg, monkeypatch
):
    from agent_sessions import opencode_admission

    prov.engine_id = "opencode"
    # A capable OpenCode provider must pass the existing capability fence before reaching ours.
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda _: prov)
    spawned = _stub_spawn(monkeypatch)
    monkeypatch.setattr(
        headless_dispatch.engine_auth, "check", lambda *a, **k: pytest.fail("auth ran")
    )
    with opencode_admission.acquire(exclusive=True):
        out = await headless_dispatch.dispatch(
            registry=reg, engine="opencode", cwd=str(env), brief="test"
        )
    assert out.refusal == "maintenance" and not out.launched
    assert "retry" in out.reason and not spawned


@pytest.mark.anyio
async def test_opencode_admission_covers_cancelled_auth_worker_until_exit(
    env, prov, reg, monkeypatch
):
    import threading

    from agent_sessions import opencode_admission

    entered, finish = threading.Event(), threading.Event()
    prov.engine_id = "opencode"
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda _: prov)

    def probe(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return headless_dispatch.engine_auth.UNAUTHENTICATED, "test"

    monkeypatch.setattr(headless_dispatch.engine_auth, "check", probe)
    _stub_spawn(monkeypatch)
    task = asyncio.create_task(
        headless_dispatch.dispatch(registry=reg, engine="opencode", cwd=str(env), brief="test")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert opencode_admission.acquire(exclusive=True) is None
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with opencode_admission.acquire(exclusive=True):
        pass
