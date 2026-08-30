"""Hermetic regressions for the #801 real-agent harness itself.

The harness is an instrument, and an instrument nobody checks reports whatever it likes. Every
test here runs **without launching an agent or spending a token** — the two contracts that
matter (reaping the master, and never missing a capability query) are about `dtach` and byte
streams, not about any particular engine, so `sleep` and a pipe are enough to pin them.

Each of these is a regression for a defect that was actually present and was found in review on
#858, not a hypothetical.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import struct
import termios
import time
import uuid
from pathlib import Path

import pytest

import nudge_harness as H
from agent_sessions import scopedspawn


def _pids_running(*argv: str) -> list[str]:
    """PIDs whose cmdline is exactly ``argv``, read from /proc.

    Deliberately not `pgrep -f`: the pattern lands in pgrep's own cmdline, so it matches itself
    and reports a process that is only the probe.
    """
    want = "\0".join(argv)
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", encoding="utf-8", errors="replace") as fh:
                if fh.read().rstrip("\0") == want:
                    out.append(entry)
        except OSError:
            continue
    return out


def _reply_probe():
    """A RealSession stub whose 'master' is a pipe, for parser tests."""
    r, w = os.pipe()
    s = H.RealSession.__new__(H.RealSession)
    s._master = w
    s._qbuf = bytearray()
    return s, r, w


@pytest.mark.parametrize("query", [q for q, _ in H._QUERY_REPLIES])
def test_capability_query_is_answered_at_every_split_boundary(query):
    """A query split across two `os.read()` chunks must still be answered.

    PTY read boundaries are arbitrary, so `ESC[` can end one read and `>q` begin the next.
    Matching per chunk misses it, the TUI waits forever for a reply that never comes, and the
    harness reports "delivered but never submitted" — the exact false negative the responder
    exists to prevent. Found in review with a probe splitting `ESC[` from `>q`.
    """
    for cut in range(1, len(query)):
        s, r, w = _reply_probe()
        s._answer_capability_queries(query[:cut])
        s._answer_capability_queries(query[cut:])
        os.close(w)
        out = os.read(r, 4096)
        os.close(r)
        assert out, f"no reply for {query!r} split at byte {cut}"


def test_capability_query_answered_when_it_arrives_whole():
    s, r, w = _reply_probe()
    s._answer_capability_queries(b"noise\x1b[>q trailing")
    os.close(w)
    out = os.read(r, 4096)
    os.close(r)
    assert out.startswith(b"\x1bP>|")


def test_query_buffer_stays_bounded():
    """The rolling buffer must not grow with the session — it keeps only a match-length tail."""
    s, r, w = _reply_probe()
    for _ in range(200):
        s._answer_capability_queries(b"x" * 4096)
    os.close(w)
    os.close(r)
    assert len(s._qbuf) <= max(len(q) for q, _ in H._QUERY_REPLIES)


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_teardown_reaps_the_master_not_just_the_client(tmp_path):
    """Exiting the context must leave **no** dtach master and no child running.

    This is the contract that costs real money when it is wrong. `dtach -c` runs in the
    foreground as a *client*; terminating it only detaches, and this repo guarantees the master
    and its child survive the spawner's death (`test_dtach_master_survives_spawner_death`). The
    original teardown killed the client and unlinked the socket — leaving a live agent and
    removing the only handle on it. Across a matrix run that was one abandoned agent per cell.

    `sleep` stands in for the agent: the contract is about dtach, not about any engine, so this
    proves it for free.
    """
    sleep = shutil.which("sleep")
    assert sleep, "sleep required"
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sleep, "300"],
    )
    with s:
        deadline = time.time() + 10
        while time.time() < deadline and not s.master_alive():
            time.sleep(0.1)
        assert s.master_alive(), "the dtach master never came up — nothing to test"
        # Scanned from /proc rather than `pgrep -f`: the pattern would appear in pgrep's own
        # cmdline and match itself, which is a documented way to "find" a process that is
        # really just your own probe.
        child = _pids_running(sleep, "300")
        assert child, "the sleep child never started"

    assert not s.master_alive(), f"master survived teardown (reap outcome: {s.reap_outcome!r})"
    # And the child is gone with it — a reaped master that orphans its child is not a reap.
    still = list(child)
    deadline = time.time() + 10
    while time.time() < deadline and still:
        still = [p for p in child if os.path.exists(f"/proc/{p}")]
        if still:
            time.sleep(0.25)
    assert not still, f"the agent process survived teardown: {still}"


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_pty_is_never_launched_at_zero_by_zero(tmp_path):
    """The engine must inherit a usable geometry on its first paint.

    A fresh `openpty()` is 0×0, and production floors both axes for exactly this reason: a 0×0
    controlling tty makes Ink-style agents render into nothing (`webterm._set_winsize`,
    #292/#293). An engine that cannot paint looks exactly like an engine that cannot be nudged.
    """
    sleep = shutil.which("sleep")
    s = H.RealSession(
        engine="claude", cwd=str(tmp_path), native_id=str(uuid.uuid4()), argv_override=[sleep, "5"]
    )
    with s:
        packed = fcntl.ioctl(s._master, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (H._PTY_ROWS, H._PTY_COLS)
        assert rows > 0 and cols > 0


def test_real_agent_tests_are_gated_by_an_explicit_marker_selection():
    """The second opt-in arm is a real gate, not a registered name.

    Registering a `real_agent` marker does not deselect anything — a plain pytest run collects
    and runs those cells, and they were skipping only because the environment variable happened
    to be unset. Inherit that variable and an ordinary run starts launching agents. The gate now
    lives in `conftest.pytest_collection_modifyitems`; this asserts it is still wired.
    """
    conftest = (H.Path(__file__).resolve().parent / "conftest.py").read_text()
    assert "pytest_collection_modifyitems" in conftest
    assert "real_agent" in conftest
    assert "config.option.markexpr" in conftest


def test_a_query_is_answered_exactly_once_across_following_noise():
    """A matched query must be consumed, not replayed.

    Retaining a whole matched query in the rolling buffer makes it match again on the next
    ordinary output chunk, writing an unsolicited second terminal response into the agent's
    input stream. A probe with the longest query plus one noise byte produced two replies
    (review on #858). Perturbing the TUI invalidates the matrix exactly as a missed reply does.
    """
    longest = max((q for q, _ in H._QUERY_REPLIES), key=len)
    s, r, w = _reply_probe()
    s._answer_capability_queries(longest)
    for _ in range(5):
        s._answer_capability_queries(b"x")  # ordinary output afterwards
    os.close(w)
    out = os.read(r, 8192)
    os.close(r)
    reply = dict(H._QUERY_REPLIES)[longest]
    assert out.count(reply) == 1, f"replied {out.count(reply)}x to one query"


def test_consecutive_queries_each_get_exactly_one_reply():
    # Pick two queries with DISTINCT replies, or the counts below are vacuous — several
    # queries deliberately share reply bytes (noted in review).
    by_reply: dict[bytes, bytes] = {}
    for query, reply in H._QUERY_REPLIES:
        by_reply.setdefault(reply, query)
    (r1, q1), (r2, q2) = list(by_reply.items())[:2]
    s, r, w = _reply_probe()
    s._answer_capability_queries(q1 + b"noise" + q2)
    s._answer_capability_queries(b"more noise")
    os.close(w)
    out = os.read(r, 8192)
    os.close(r)
    assert r1 != r2, "test needs two queries with different replies"
    assert out.count(r1) == 1, f"replied {out.count(r1)}x to the first query"
    assert out.count(r2) == 1, f"replied {out.count(r2)}x to the second query"


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_startup_failure_after_spawn_still_reaps_the_master(tmp_path, monkeypatch):
    """An exception during post-spawn setup must not leak the agent.

    `__exit__` runs only if `__enter__` *returns*. Before the rollback, a failure in
    `register_writer` or `Thread.start()` left the dtach master and a live authenticated agent
    running with nothing scheduled to reap them — the original leak reached through a different
    door (review on #858). Injecting the failure is the only way to see it.
    """
    sleep = shutil.which("sleep")
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sleep, "300"],
    )

    boom = RuntimeError("injected: drain thread failed to start")

    def explode(self_):
        raise boom

    monkeypatch.setattr(H.threading.Thread, "start", explode)
    with pytest.raises(RuntimeError):
        s.__enter__()
    monkeypatch.undo()

    assert not s.master_alive(), (
        f"master survived a failed __enter__ (reap outcome: {s.reap_outcome!r}) — a live agent "
        "was left running with nothing scheduled to reap it"
    )
    assert not _pids_running(sleep, "300"), "the child survived a failed __enter__"


def _RETIRED_test_stale_output():
    pass


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_teardown_reaps_a_child_that_escaped_the_group_and_carries_no_session_id(tmp_path):
    """The case the id-and-group sweep cannot see after the fact.

    `cleanup_runtime` kills the master's process group first. A child that has left that group
    and carries neither the session id nor the socket path in its argv is then undiscoverable —
    a probe reported `escaped_child_survived_teardown=True` with `straggler_pids=[]` (review on
    #858). It is reapable only because descendants are sampled *while the runtime is alive*.
    """
    sh = shutil.which("sh")
    sleep = shutil.which("sleep")
    # setsid puts the grandchild in its OWN process group, and its argv carries no session id.
    setsid = shutil.which("setsid")
    if not setsid:
        pytest.skip("setsid required")
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sh, "-c", f"{setsid} {sleep} 297 & exec {sleep} 298"],
    )
    with s:
        deadline = time.time() + 15
        while time.time() < deadline and not _pids_running(sleep, "297"):
            time.sleep(0.2)
        escaped = _pids_running(sleep, "297")
        assert escaped, "the escaped grandchild never started"
        # It really is out of the master's group and carries no id.
        assert s.native_id not in open(f"/proc/{escaped[0]}/cmdline").read()
        time.sleep(1.5)  # let the descendant sampler observe it

    deadline = time.time() + 10
    while time.time() < deadline and _pids_running(sleep, "297"):
        time.sleep(0.25)
    assert not _pids_running(sleep, "297"), "the escaped child survived teardown"


def test_a_recycled_pid_is_never_signalled():
    """A remembered PID whose start time no longer matches must not be signalled.

    PIDs are not durable identities — Linux reuses numbers, and a cell can run for minutes. A
    remembered number naively trusted would let teardown kill unrelated work, and then its
    whole process group (review on #858). Identity is `(pid, start-time)`.
    """
    s = H.RealSession.__new__(H.RealSession)
    s.native_id = "no-such-session-id-" + str(uuid.uuid4())
    s._seen_descendants = {os.getpid(): 1}  # our own pid, with a WRONG start time
    s.straggler_pids = []
    victims = s._reap_stragglers(grace=0.1)
    assert os.getpid() not in victims, "would have signalled a process whose identity changed"


def test_starttime_identifies_a_live_process_and_rejects_a_dead_one():
    s = H.RealSession.__new__(H.RealSession)
    assert s._starttime(os.getpid()) is not None
    assert s._starttime(999_999_999) is None


@pytest.mark.skipif(
    not (shutil.which("dtach") and shutil.which("setsid")), reason="dtach + setsid required"
)
def test_teardown_reaps_an_escaped_child_with_no_grace_period(tmp_path):
    """Tear down the instant the escaped child appears — no sleep to let the sampler catch it.

    The previous version slept 1.5s before exiting, which guaranteed the 0.5s watcher had
    already sampled the child and so masked the real boundary: a child appearing between the
    watcher's last scan and teardown was never remembered, and once the root is gone the tree
    walk has no root to start from, so it could never be discovered afterwards either (review
    on #858). Teardown now takes a final forced scan while the root is still alive, and this
    exercises exactly that window.
    """
    sh, sleep, setsid = shutil.which("sh"), shutil.which("sleep"), shutil.which("setsid")
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sh, "-c", f"printf x; sleep 2; {setsid} {sleep} 291 & exec {sleep} 292"],
    )
    with s:
        deadline = time.time() + 25
        while time.time() < deadline and not _pids_running(sleep, "291"):
            time.sleep(0.05)
        assert _pids_running(sleep, "291"), "the escaped child never started"
        # NO grace period: exit immediately, so the child may never have been sampled.

    deadline = time.time() + 10
    while time.time() < deadline and _pids_running(sleep, "291"):
        time.sleep(0.25)
    assert not _pids_running(sleep, "291"), "the escaped child survived an immediate teardown"


def test_a_dropped_nudge_is_ALWAYS_a_red_cell_now(monkeypatch):
    """The suppression is gone, and this is the test that says so.

    Five rounds of review could not make "the engine was blocked, not broken" derivable from
    what this harness sees, and the two errors are not symmetric: a false red costs an
    investigation that starts with the screen tail printed beside it, while a false `UNTESTED`
    deletes the finding. So a non-submitting engine is reported, with its evidence, every time —
    even when the screen is showing a modal we would previously have skipped on.
    """
    s = _real(_GEMINI_MODAL, engine="gemini")
    # Nothing about this SCREEN changes the answer. What decides is the session record.
    assert H.prompt_ready(s)[0] is None


#: The REAL gemini modal, from a screen tail this harness captured on a live run. Kept as a
#: FIXTURE — nothing classifies on it any more; it is the realistic thing to put on a screen when
#: a test needs one.
_GEMINI_MODAL = (
    "│ Terms of Services and Privacy Notice for Gemini CLI │\n"
    "│ https://geminicli.com/docs/resources/tos-privacy/    │\n"
    "╰──────────────────────────────────────────────────────╯"
)


def test_no_result_is_derived_from_the_RAW_SCREEN_BUFFER_anywhere():
    """The classifier is gone, and this is what stands where its tests did.

    Six review rounds each defeated a narrower version of it — free-form substrings, then
    strong-vs-weak, then line anchoring, then a length bound, then an engine-bound two-marker
    signature — and the sixth showed why: `screen_text()` is a rolling BYTE buffer, and a TUI
    repaints with cursor-home plus erase-line, so a modal overwritten on screen still has every
    byte in it. Tail-slicing cannot establish what is displayed, and no rule built on it can.

    So no engine state is inferred from that buffer any more. The buffer is diagnostics: it
    rides on a failure so a person can read it. This asserts the absence, because "we removed
    it" is a claim about today and this is a claim about tomorrow.
    """
    src = (Path(H.__file__)).read_text()
    for gone in ("_MODAL_SIGNATURES", "modal_may_be_showing", "looks_unauthenticated"):
        assert gone not in src, f"{gone} came back — see `PROMPT_READY` before re-adding"
    # …and the one remaining reader of the buffer is the TRUST handler, where over-matching
    # costs a keystroke and under-matching costs a dialog left up — both ending in a red cell
    # that carries this same screen.
    assert "_TRUST_MARKERS" in src


def _real(text: str = "", engine: str = "claude", ready: bool = True) -> H.RealSession:
    """A REAL `RealSession`, not a duck-typed stand-in.

    The stand-ins are what let a rename ship broken: they implemented the OLD method name, so
    `_ready`'s call to a method that no longer existed passed every caller test and would have
    crashed every real-agent cell with `AttributeError` (review on #880). A fake that implements
    the API the caller *used to* use tests nothing about the API it uses now.
    """
    s = H.RealSession.__new__(H.RealSession)
    s.engine = engine
    # A real id, because a readiness signal may resolve one against the engine store — and a
    # bypassed `__init__` is exactly how a field the code depends on goes missing.
    s.native_id = str(uuid.uuid4())
    s._seen = bytearray(text.encode())
    s.wait_ready = lambda *a, **k: ready  # type: ignore[method-assign]
    return s


def test_a_PASS_needs_no_prerequisite_at_all(monkeypatch):
    """A committed turn is self-certifying, and that is what makes the rest tractable.

    A session parked on a trust dialog or a Terms notice cannot commit a turn — so if one
    committed, the engine was at a prompt and it submitted. No readiness signal is required, and
    demanding one is what would have skipped claude, the only engine that passes.
    """
    import test_nudge_submit_real as suite

    monkeypatch.setattr(H, "PROMPT_READY", {})
    # Asserted as "raises NOTHING", not by simply calling it: a `_classify` that SKIPPED on a pass
    # would make this test skip rather than fail, and a skipped test is not a red — it would sail
    # through a mutation that costs the matrix its only passing engine.
    try:
        suite._classify(_real("anything at all"), submitted=True, detail="user turns 0 -> 1")
    except BaseException as e:  # noqa: BLE001 — including pytest's Skipped
        raise AssertionError(
            f"a PASS was not accepted on its own: {type(e).__name__}: {e}"
        ) from None


def test_a_FAILURE_with_NO_demonstrated_signal_is_UNTESTED_not_red(monkeypatch):
    """#801's contract, and the state every engine is in today."""
    import test_nudge_submit_real as suite

    monkeypatch.setattr(H, "PROMPT_READY", {})
    with pytest.raises(BaseException) as e:
        suite._classify(
            _real(_GEMINI_MODAL, engine="gemini"),
            submitted=False,
            detail="delivered, but user turns stayed at 0 after 120s",
        )
    assert "Skipped" in type(e.value).__name__
    assert "UNTESTED, not failing" in str(e.value)
    assert "no demonstrated prompt-ready signal" in str(e.value)


def test_a_FAILURE_with_a_signal_that_says_READY_is_a_RED_CELL(monkeypatch):
    """The contract is not vacuous: contribute a signal and the defect becomes reportable.

    The caller-level "ready" regression — demonstrated at a prompt, write landed, no turn.
    """
    import test_nudge_submit_real as suite

    monkeypatch.setattr(
        H, "PROMPT_READY", {"claude": lambda s: (True, "the prompt marker was observed")}
    )
    with pytest.raises(BaseException) as e:
        suite._classify(
            _real("ready > "),
            submitted=False,
            detail="delivered, but user turns stayed at 0 after 120s",
        )
    assert "Skipped" not in type(e.value).__name__, "the defect under test was skipped"
    assert "Failed" in type(e.value).__name__
    assert "defect #801 exists to measure" in str(e.value)


def test_a_signal_that_says_NOT_READY_is_UNTESTED(monkeypatch):
    """The caller-level "blocked" regression. `False` and `None` reach the same cell by different
    routes, and both are honest; only `True` licenses a red."""
    import test_nudge_submit_real as suite

    monkeypatch.setattr(
        H, "PROMPT_READY", {"claude": lambda s: (False, "the onboarding gate was observed")}
    )
    with pytest.raises(BaseException) as e:
        suite._classify(
            _real("ready > "), submitted=False, detail="delivered, but user turns stayed at 0"
        )
    assert "Skipped" in type(e.value).__name__
    assert "onboarding gate was observed" in str(e.value)


def test_a_DELIVERY_failure_is_red_but_says_something_DIFFERENT(monkeypatch):
    """A write that never landed says nothing about how the engine handles one that did."""
    import test_nudge_submit_real as suite

    monkeypatch.setattr(H, "PROMPT_READY", {})
    with pytest.raises(BaseException) as e:
        suite._classify(_real(), submitted=False, detail="not delivered: refused (viewer busy)")
    assert "Failed" in type(e.value).__name__
    assert "the write itself did not land" in str(e.value)


def test_the_registry_is_EMPTY_and_that_is_the_finding():
    """Five general rules were tried and refuted; the registry records that rather than hiding it.

    If an entry appears here, its author owes the demonstration — which is the contract this test
    exists to make visible.
    """
    assert H.PROMPT_READY == {}, (
        "a prompt-ready signal was added: demonstrate the engine's real store/state boundary in "
        "the PR, the way the gemini/claude measurements in `PROMPT_READY` are demonstrated"
    )


def test_prompt_ready_distinguishes_NO_SIGNAL_from_NOT_READY(monkeypatch):
    """`None` is not `False`. "Nobody has established how to tell" and "it is demonstrably not at
    a prompt" are different facts; collapsing them makes a missing prerequisite read as an
    observation."""
    monkeypatch.setattr(H, "PROMPT_READY", {})
    ready, why = H.prompt_ready(_real(engine="gemini"))
    assert ready is None and "no demonstrated prompt-ready signal" in why

    monkeypatch.setattr(H, "PROMPT_READY", {"gemini": lambda s: (False, "modal observed")})
    ready, why = H.prompt_ready(_real(engine="gemini"))
    assert ready is False and why == "modal observed"


def test_ready_calls_only_methods_a_REAL_session_has():
    """The stale-rename guard, stated as a property rather than trusted to a fake.

    `_ready` is driven against an actual `RealSession`, so a method it calls that no longer
    exists is an `AttributeError` here instead of in the opt-in matrix nobody runs in CI — which
    is how a rename shipped broken once already (review on #880).
    """
    import test_nudge_submit_real as suite

    suite._ready(_real("ready > "))


@pytest.mark.skipif(
    not (shutil.which("dtach") and shutil.which("setsid")), reason="dtach + setsid required"
)
def test_scope_reaps_a_child_the_inventory_never_saw(tmp_path, monkeypatch):
    """The race no snapshot can close: a child forked AFTER the final inventory scan.

    A forced scan is only a snapshot — it does not stop the master forking after it, and once
    `_proc` is terminated the tree walk has no root, so such a child is never remembered and
    the straggler sweep cannot find it (review on #858).

    The fix is containment rather than inventory: the session launches inside a transient
    systemd scope, and stopping that scope kills the whole cgroup — every descendant, whenever
    it was forked. This test proves the cgroup is what does the work by **disabling the
    inventory entirely**: `_sample_descendants` is a no-op, so `_seen_descendants` stays empty
    and nothing can be reaped by pid or by session id.
    """
    if not scopedspawn.available():
        pytest.skip("systemd-run --user scopes unavailable on this host")
    sh, sleep, setsid = shutil.which("sh"), shutil.which("sleep"), shutil.which("setsid")
    monkeypatch.setattr(H.RealSession, "_sample_descendants", lambda self, force=False: None)

    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sh, "-c", f"{setsid} {sleep} 287 & exec {sleep} 288"],
    )
    with s:
        deadline = time.time() + 20
        while time.time() < deadline and not _pids_running(sleep, "287"):
            time.sleep(0.1)
        assert _pids_running(sleep, "287"), "the escaped child never started"

    assert s.scope_unit, "no transient scope was created — nothing would contain the subtree"
    assert s.scope_stopped, "the scope was never stopped"
    assert not s._seen_descendants, "inventory should be disabled for this test"
    assert not s.straggler_pids, "the sweep must not be what reaped it"

    deadline = time.time() + 10
    while time.time() < deadline and _pids_running(sleep, "287"):
        time.sleep(0.25)
    assert not _pids_running(sleep, "287"), "the cgroup did not reap the escaped child"


def test_a_real_agent_is_refused_when_no_transient_scope_is_available(monkeypatch):
    """Without a usable scope the harness must not launch a real agent at all.

    The fallback (descendant inventory) is racy **by construction**: a child forked after the
    final scan and detached with `setsid` carries neither the session id nor the socket path,
    and once the root is reaped the tree walk has no root — so it is both unrememberable and
    undiscoverable. Three rounds of scan-timing fixes could not close that, because no snapshot
    can (review on #858).

    So containment became a prerequisite rather than a best effort: if the session cannot be
    guaranteed reapable, an authenticated token-spending agent is not launched. The cell is
    recorded UNTESTED, which is the same rule this suite already applies to engines it cannot
    measure — never a pass, never a red.
    """
    monkeypatch.setenv(H.OPT_IN_ENV, "1")
    monkeypatch.setattr(H.scopedspawn, "available", lambda: False)
    ok, reason = H.engine_available("claude")
    assert ok is False
    assert "could not be guaranteed reapable" in reason
    assert "refusing to launch" in reason


def test_stop_scope_reports_failure_when_systemctl_fails(monkeypatch):
    """A failed `systemctl stop` must NOT report success.

    Reporting success would silently drop the session back onto the inventory path — the racy
    one the scope exists to replace — while the caller believes the cgroup was killed.
    """
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"

    class _Fail:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _Fail())
    assert s._stop_scope(timeout=1) is False


def test_stop_scope_requires_the_unit_to_actually_be_gone(monkeypatch):
    """`stop` exiting 0 is not proof the cgroup is empty — the unit state is checked too."""
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"

    class _StillActive:
        returncode = 0
        stdout = "active\n"

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _StillActive())
    assert s._stop_scope(timeout=1) is False

    class _Gone:
        returncode = 0
        stdout = "inactive\n"

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _Gone())
    assert s._stop_scope(timeout=1) is True


def test_config_disabled_scopes_are_refused_too(monkeypatch):
    """`available()` is not the whole scope contract — `wrap()` also honours `enabled()`.

    With `AGENT_SESSIONS_SESSION_SCOPES=0`, `scopedspawn.wrap()` returns the bare argv and no
    unit even though the probe says scopes are *available*. Gating only on `available()` waved
    that straight through onto the racy inventory fallback and launched a real agent unscoped
    (review on #858).
    """
    monkeypatch.setenv(H.OPT_IN_ENV, "1")
    monkeypatch.setattr(H.scopedspawn, "available", lambda: True)
    monkeypatch.setattr(H.scopedspawn, "enabled", lambda: False)
    ok, reason = H.engine_available("claude")
    assert ok is False
    assert "disabled by config" in reason


def test_launch_fails_closed_when_scopes_are_unusable(monkeypatch):
    """A real engine is never spawned without containment — checked before any launch work.

    The gate deliberately runs at the very top of `__enter__`, before the argv is built: it
    does not depend on the launch command, and placing it after argv construction made it
    unreachable wherever the engine binary is absent, because `ptybridge.launch_argv` rejects
    a bare name first. That is precisely the CI case, so the guard existed only on machines
    that happened to have the engine installed — which is not a guard at all.

    The `sleep` stand-ins used by this suite stay exempt: hermetic, and they spend nothing.
    """
    monkeypatch.setattr(H.scopedspawn, "enabled", lambda: False)
    monkeypatch.setattr(H.scopedspawn, "available", lambda: True)
    spawned = []
    monkeypatch.setattr(H.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    s = H.RealSession(engine="claude", cwd="/tmp", native_id=str(uuid.uuid4()))
    with pytest.raises(RuntimeError, match="refusing to launch a real agent"):
        s.__enter__()
    assert not spawned, "a real agent was spawned without a transient scope"


def test_the_sleep_stand_ins_are_exempt_from_the_scope_gate(monkeypatch, tmp_path):
    """`argv_override` sessions must still run with scopes unusable — they spend nothing.

    Without this the harness's own hermetic regressions would become unrunnable on any host
    without systemd scopes, which would quietly reduce coverage rather than protect anything.
    """
    monkeypatch.setattr(H.scopedspawn, "enabled", lambda: False)
    sleep = shutil.which("sleep")
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sleep, "1"],
    )
    with s:
        pass  # must not raise


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_teardown_raises_when_containment_cannot_be_verified(tmp_path, monkeypatch):
    """An unverifiable scope must FAIL the cell, not return a quiet False.

    Detecting a failed stop and then continuing leaves a real agent possibly running while the
    caller sees a normal teardown — and the sweeps cannot find an id-less child forked after
    the final snapshot. Containment is part of the teardown contract (review on #858).
    """
    sleep = shutil.which("sleep")
    s = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sleep, "5"],
    )
    with s:
        pass_through = s.scope_unit
    assert pass_through, "this host must create a scope for the test to mean anything"

    # Now a session whose scope can never be verified gone.
    s2 = H.RealSession(
        engine="claude",
        cwd=str(tmp_path),
        native_id=str(uuid.uuid4()),
        argv_override=[sleep, "5"],
    )
    monkeypatch.setattr(H.RealSession, "_stop_scope", lambda self, **k: False)
    monkeypatch.setattr(H.RealSession, "_kill_scope", lambda self, **k: False)
    with pytest.raises(RuntimeError, match="containment NOT verified"):
        with s2:
            pass
    # Clean up the scope the failed teardown deliberately left alone.
    monkeypatch.undo()
    s2._kill_scope()


def test_readiness_is_not_satisfied_by_a_live_trust_dialog():
    """A modal that painted and then went quiet is not readiness.

    Same shape as the logged-out prompt: `wait_ready` accepts first-paint-then-quiet, and a
    dialog waiting for a keypress is perfectly quiet. Delivering into it produces no turn, which
    the matrix would publish as the engine dropping the nudge.
    """
    s = H.RealSession.__new__(H.RealSession)
    s._seen = bytearray(b"Quick safety check: Is this a project you created or one you trust?")
    assert s.trust_prompt_showing()
    s._seen = bytearray(b"ready > try 'refactor foo.py'")
    assert not s.trust_prompt_showing()


def test_scope_is_not_declared_dead_on_a_query_error(monkeypatch):
    """A failed state query proves nothing — it must not read as "the unit is gone".

    `is-active` was the original probe, and it exits non-zero for ordinary states, so its
    status could not be consulted; "stdout is not `active`" was used instead. That accepted a
    D-Bus / user-manager failure (returncode 1, empty stdout) as proof of containment, which is
    exactly backwards: the one moment the harness cannot see the unit is the moment it must not
    claim the cgroup was reaped (review on #858).
    """
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"

    class _ManagerError:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _ManagerError())
    assert s._scope_inactive(timeout=1) is False
    assert s._stop_scope(timeout=1) is False, "the caller must inherit the refusal"


def test_a_deactivating_scope_is_not_yet_terminal(monkeypatch):
    """`deactivating` still holds processes; only `inactive`/`failed` end the wait."""
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"

    class _Deactivating:
        returncode = 0
        stdout = "deactivating\n"

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _Deactivating())
    assert s._scope_inactive(timeout=1) is False

    class _Failed:
        returncode = 0
        stdout = "failed\n"

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _Failed())
    assert s._scope_inactive(timeout=1) is True, "`failed` is terminal — the cgroup is empty"


def test_a_failed_kill_is_recorded_but_the_unit_state_decides(monkeypatch):
    """The kill's status is consulted; the *provable* unit state is what settles containment.

    Measured on real systemd: on the ordinary success path `stop` has already reaped the scope,
    so `kill` exits 1 with "Unit ... not loaded". An earlier attempt at this fix refused on that
    status and reported three correctly-reaped cgroups as uncontained. A failed kill over a
    verifiably terminal unit is therefore success — and the error is still recorded.
    """
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"
    s.kill_error = None

    def _run(argv, *a, **k):
        if "kill" in argv:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "Unit not loaded."})()
        return type("R", (), {"returncode": 0, "stdout": "inactive\n", "stderr": ""})()

    monkeypatch.setattr(H.subprocess, "run", _run)
    assert s._kill_scope(timeout=1) is True, "already-dead is contained, not a failure"
    assert s.kill_error == "Unit not loaded.", "the failure is still recorded, not swallowed"


def test_kill_refuses_when_the_state_cannot_be_proved(monkeypatch):
    """A broken manager fails BOTH commands — and that path must fail closed.

    This is the caller path Hermes asked to cover: kill errors, verification errors, and the
    teardown contract must surface rather than report a clean reap.
    """
    s = H.RealSession.__new__(H.RealSession)
    s.scope_unit = "as-claude-deadbeef.scope"
    s.kill_error = None

    class _AllFail:
        returncode = 1
        stdout = ""
        stderr = "Failed to connect to bus"

    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _AllFail())
    assert s._kill_scope(timeout=1) is False
    assert s._stop_scope(timeout=1) is False


# Real layouts, captured from the engines rather than imagined. claude 2.1.247 draws a BLANK
# LINE between its two options and puts the cursor on "No, exit" — both measured off a live PTY.
_CLAUDE_DIALOG = (
    "Quick safety check: Is this a project you created or one you trust?\n"
    "\n"
    "\u276f No, exit\n"
    "\n"
    "  Yes, I trust this folder\n"
)
_INVERSE_DIALOG = (
    "Do you trust the files in this folder?\n"
    "\n"
    "\u276f Yes, I trust this folder\n"
    "\n"
    "  No, exit\n"
)


def _dialog_session(screen: str, proc=None):
    """A session showing `screen`, whose PTY write swaps in whatever the engine paints next."""
    s = H.RealSession.__new__(H.RealSession)
    s._trusted = False
    s._proc = proc
    s._master = 1
    s._out_bytes = 0
    s._seen = bytearray(screen.encode())
    return s


def test_the_affirmative_option_is_selected_from_the_real_claude_layout():
    """One deterministic move onto "Yes", read off the drawn list — not a guessed keystroke."""
    s = _dialog_session(_CLAUDE_DIALOG)
    assert s._trust_options() == [(False, True), (True, False)]
    assert s._trust_selection_keys() == b"\x1b[B\r", "cursor sits on No; Yes is one Down away"


def test_inverse_option_order_never_sends_the_destructive_keystroke(monkeypatch):
    """THE regression: on a Yes-first dialog, Down would select "No" and kill the engine.

    The old design always tried Down+CR first and relied on an Up+CR fallback — but selecting
    "No" exits the engine, so the liveness check returns before the fallback is ever reachable
    and the matrix records a harness-caused exit as an engine failure (review on #858). The
    affirmative option is now located before anything is sent.
    """
    alive = _Alive()
    s = _dialog_session(_INVERSE_DIALOG, proc=alive)
    assert s._trust_selection_keys() == b"\r", "cursor already on Yes — do not move"

    written = []

    def _write(fd, b):
        written.append(b)
        if b"\x1b[B" in b:  # a Down here would have selected "No, exit"
            alive.exited = True
        s._out_bytes += 64
        s._seen = bytearray(b"ready for input")
        return len(b)

    monkeypatch.setattr(H.os, "write", _write)
    assert s._answer_trust_prompt() is True
    assert written == [b"\r"], f"exactly one, non-destructive selection; got {written!r}"
    assert not alive.exited, "the engine must still be alive and able to reach readiness"


class _Alive:
    exited = False

    def poll(self):
        return 1 if self.exited else None


@pytest.mark.parametrize(
    "screen",
    [
        "\u276f No, exit\n\n  Maybe later\n",  # no affirmative option at all
        "  No, exit\n\n  Yes, I trust this folder\n",  # no cursor: a move cannot be aimed
        "Do you trust the files in this folder?\n",  # marker, but no options drawn yet
    ],
)
def test_an_unreadable_dialog_sends_nothing_at_all(screen, monkeypatch):
    """When the layout cannot be read, refusing beats guessing — a wrong guess is destructive."""
    s = _dialog_session(screen)
    written = []
    monkeypatch.setattr(H.os, "write", lambda fd, b: written.append(b) or len(b))
    assert s._trust_selection_keys() is None
    assert s._answer_trust_prompt() is False
    assert written == [], "nothing may be sent when the affirmative option is not identified"


def test_the_last_repainted_frame_decides_the_cursor_position():
    """The buffer holds every frame; a stale cursor would aim the move at the wrong option."""
    s = _dialog_session(_CLAUDE_DIALOG + "\n" + _INVERSE_DIALOG)
    assert s._trust_options() == [(True, True), (False, False)], "last frame wins"
    assert s._trust_selection_keys() == b"\r"


def test_trust_answer_needs_fresh_output_not_an_empty_buffer(monkeypatch):
    """`_seen` is cleared before the keystroke, so "no marker" is trivially true at first.

    Without requiring new bytes, the very first 250 ms poll saw an empty buffer, found no trust
    marker in it, and declared the dialog answered — proving the accepted state from the
    absence of any output at all (review on #858).
    """
    s = _dialog_session(_CLAUDE_DIALOG, proc=_Alive())
    # The keystroke lands, `_seen` is cleared, and the engine then paints NOTHING back.
    monkeypatch.setattr(H.os, "write", lambda fd, b: len(b))
    assert s._answer_trust_prompt() is False, "silence is not an answered dialog"
    assert s._trusted is False


def test_an_exit_during_the_trust_answer_is_caught_inside_the_loop(monkeypatch):
    """A dialog that clears because the ENGINE DIED must not read as success.

    The liveness check sat *after* the poll loop, so the "marker is gone" branch returned first
    and reported success for an engine that had just quit.
    """
    alive = _Alive()
    s = _dialog_session(_CLAUDE_DIALOG, proc=alive)

    def _write(fd, b):
        alive.exited = True  # the engine quits...
        s._out_bytes += 64  # ...and its farewell IS fresh output
        s._seen = bytearray()
        return len(b)

    monkeypatch.setattr(H.os, "write", _write)
    assert s._answer_trust_prompt() is False
    assert s._trusted is False


@pytest.mark.parametrize(
    "markexpr",
    [
        "real_agent_extra",
        "not real_agent_extra",
        "xreal_agent",
        "real_agentology or slow",
        "not e2e_install",
        "",
    ],
)
def test_lookalike_marker_names_do_not_open_the_real_agent_gate(markexpr):
    """The gate matches the IDENTIFIER `real_agent`, never a substring of a longer name.

    `-m "not real_agent_extra"` contains the text "real_agent" while *selecting* the real-agent
    cells. Under the substring check the hook returned early, added no skip, and a routine run
    launched the token-spending matrix whenever `AGENT_SESSIONS_REAL_AGENT` was inherited
    (review on #858).
    """
    assert _skips_added(markexpr) == 1, f"{markexpr!r} must not satisfy the gate"


@pytest.mark.parametrize(
    "markexpr",
    ["real_agent", "not real_agent", "real_agent and not slow", "slow or real_agent"],
)
def test_the_gate_opens_for_expressions_that_name_the_marker(markexpr):
    """Compound expressions that genuinely name `real_agent` still select it."""
    assert _skips_added(markexpr) == 0, f"{markexpr!r} names the marker and must pass the gate"


def _skips_added(markexpr: str) -> int:
    """Run the real collection hook over one `real_agent` item; count the skips it applied."""
    import conftest

    class _Opt:
        pass

    class _Config:
        option = _Opt()

    cfg = _Config()
    cfg.option.markexpr = markexpr

    class _Item:
        keywords = {"real_agent": True}

        def __init__(self):
            self.marks = []

        def add_marker(self, m):
            self.marks.append(m)

    item = _Item()
    conftest.pytest_collection_modifyitems(cfg, [item])
    return len(item.marks)


# ==================================================================================================
# The numbered trust parser, and the post-write classifier (#801, review on #880).
#
# Both are hermetic: a stub screen, no engine, no tokens. They exist because the previous version
# of this file did not exercise either — so a parser regression could have selected the BROADER
# `Trust parent folder`, or a classifier regression could have manufactured a green, while the
# suite this PR cites stayed entirely passing. That is the exact defect class #801 is about.
# ==================================================================================================


class _Screen(H.RealSession):
    """A session whose screen is whatever the test says it is."""

    def __init__(self, text: str, engine: str = "gemini"):
        super().__init__(engine=engine, cwd="/tmp")
        self._text = text

    def screen_text(self) -> str:  # type: ignore[override]
        return self._text


#: Gemini's dialog as it is actually drawn — box-framed, `●` on the selected option, numbered.
#: Captured from a live session; the parser is written against this rather than against a guess.
GEMINI_TRUST = """
 │ Trusting a folder allows Gemini CLI to load its local configurations, including custom       │
 │                                                                                              │
 │ ● 1. Trust folder (work-dir)                                                                 │
 │   2. Trust parent folder (tmp)                                                               │
 │   3. Don't trust                                                                             │
 ╰──────────────────────────────────────────────────────────────────────────────────────────────╯
"""


def test_numbered_trust_dialog_selects_the_exact_folder():
    assert _Screen(GEMINI_TRUST)._numbered_trust_key() == b"1\r"


def test_the_dialog_is_recognised_as_a_trust_prompt_at_all():
    # Readiness depends on this too: `wait_ready` must not call a session ready while it is up.
    assert _Screen(GEMINI_TRUST).trust_prompt_showing() is True


def test_parent_only_fails_closed_rather_than_widening_the_grant():
    """`Trust parent folder` is strictly broader — it would trust the parent for every future
    run. With no exact-folder option the parser refuses instead of reaching for it."""
    screen = GEMINI_TRUST.replace("● 1. Trust folder (work-dir)", "● 1. Trust parent folder (tmp)")
    screen = screen.replace("  2. Trust parent folder (tmp)", "  2. Don't trust")
    assert _Screen(screen)._numbered_trust_key() is None


def test_two_exact_folder_options_are_ambiguous_and_refused():
    screen = GEMINI_TRUST.replace("  2. Trust parent folder (tmp)", "  2. Trust folder (other)")
    assert _Screen(screen)._numbered_trust_key() is None


#: The dialog REPAINTED with its options renumbered — the exact-folder choice is now `2`.
#:
#: The frames must MATERIALLY DISAGREE or the test is vacuous. An earlier version only moved the
#: selection glyph, leaving `1. Trust folder` in both frames: a parser that always read the first
#: frame returned `b"1\r"` and passed, which is precisely the bug the test claims to exclude
#: (review on #880). Answering `1` here would now choose "Don't trust" — a wrong answer that
#: exits the engine and publishes the exit as a dropped nudge.
GEMINI_TRUST_RENUMBERED = """
 │ Trusting a folder allows Gemini CLI to load its local configurations, including custom       │
 │                                                                                              │
 │ ● 1. Don't trust                                                                             │
 │   2. Trust folder (work-dir)                                                                 │
 │   3. Trust parent folder (tmp)                                                               │
 ╰──────────────────────────────────────────────────────────────────────────────────────────────╯
"""


def test_a_repaint_is_read_from_the_CURRENT_frame_not_a_stale_one():
    """The dialog repaints and the buffer keeps every earlier frame. Reading a stale frame would
    answer a menu that is no longer on screen — the same hazard `_trust_options` guards for the
    cursor path, and it applies to the numbered one too."""
    # Old frame first, current frame second. The stale answer is `1`; the current one is `2`.
    assert _Screen(GEMINI_TRUST + GEMINI_TRUST_RENUMBERED)._numbered_trust_key() == b"2\r"


def test_a_repaint_that_removes_the_exact_folder_option_fails_closed():
    """The other direction, and the one a stale read is most dangerous in: the current frame no
    longer offers the exact folder at all, so the answer is to refuse — never the digit that
    happened to be right one frame ago."""
    narrowed = GEMINI_TRUST.replace(
        "● 1. Trust folder (work-dir)", "● 1. Trust parent folder (tmp)"
    ).replace("  2. Trust parent folder (tmp)", "  2. Don't trust")
    assert _Screen(GEMINI_TRUST + narrowed)._numbered_trust_key() is None


def test_a_screen_with_no_numbered_menu_is_not_a_numbered_dialog():
    assert _Screen("❯ No, exit\n  Yes, I trust this folder")._numbered_trust_key() is None


# --------------------------------------------------------------------------------------------
# `PROMPT_READY` — the difference between "this engine drops nudges" and "this engine was never
# at a prompt". This issue has already published the first when the second was true, on a live
# run, so it is tested here rather than in the real-agent module where the opt-in gate would skip
# every case (review on #880).
# --------------------------------------------------------------------------------------------

#: The REAL modal, copied from a screen tail this harness captured on a live gemini run — not a
#: paraphrase. The heading alone is deliberately NOT enough to classify (see the negative
#: controls above); what makes this frame a modal is the notice's own link beside it.
_TOS = (
    "│ Terms of Services and Privacy Notice for Gemini CLI │\n"
    "│ https://geminicli.com/docs/resources/tos-privacy/    │\n"
    "╰──────────────────────────────────────────────────────╯"
)


def test_TRANSCRIPT_EXISTENCE_is_not_a_readiness_signal(tmp_path, monkeypatch):
    """The measurement that refuted attempt 5, pinned so it cannot be quietly re-adopted.

    The installed stores disagree about WHEN a session file appears, in opposite directions:
    gemini writes a header before the prompt (so a blocked session would pass such a check and
    publish a false red), and claude writes nothing until a turn commits (so the same check would
    skip the one engine that passes). This reproduces the shape locally — a resolvable session
    record whose session has committed nothing.
    """
    from agent_sessions import transcript

    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-tmp-x"
    proj.mkdir(parents=True)
    native = "11111111-2222-3333-4444-555555555555"
    # No user turn — exactly the shape gemini's 317 header-only files have.
    (proj / f"{native}.jsonl").write_text('{"type":"session_meta","cwd":"/tmp/x"}\n')
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert transcript.locator_for("claude")(native, home), "the locator resolves it"
    assert H.user_turns("claude", native, home) == 0, "...and it has committed nothing"
    # So "the locator resolves it" cannot mean "the engine was at a prompt".
