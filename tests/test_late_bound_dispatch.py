"""A mission can start an engine whose session id arrives after the brief (#989).

The launch is stubbed exactly as `test_headless_dispatch.py` stubs it — the real spawn is proved in
`test_headless_launch.py` — and the engine is a fake LATE-ID provider, so these tests pin the
orchestration: which session a launch binds, what it refuses to bind, and what the mission store,
the fences, recovery and the alias projection do with the answer. They say nothing about whether a
real engine is safe to enable; that is measured per engine against the real CLI (#989 Phase 2).

The engine id is `kimi` so that the REAL registry's placeholder rules, id pattern and runtime parser
apply wherever the code under test consults them. Nothing here starts kimi.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
import tempfile
import threading
import time
import types
import uuid

import pytest

from agent_sessions import (
    engines,
    handoff,
    headless_dispatch,
    headless_seed,
    launch_binding,
    metadata,
    mission_dispatch,
    mission_dispatch_recover,
    mission_fence,
    mission_plan,
    missions,
    ptybridge,
    runtime_cleanup,
    session_input,
    sessionlock,
    transcript,
)
from agent_sessions.engines import base

OURS = "session_11111111-2222-3333-4444-555555555555"
OTHER = "session_22222222-3333-4444-5555-666666666666"
THIRD = "session_33333333-4444-5555-6666-777777777777"
BRIEF = "review the open bugs and report back"
#: A pid above any `pid_max`, so its lease is provably dead.
DEAD_OWNER = "4194305:1"


class FakeLate:
    """A late-id engine that declares every capability, backed by in-memory state."""

    engine_id = "kimi"
    id_pattern = base._KIMI_SESSION_RE
    supports_seed_start = True
    new_session_reconciles = True

    def __init__(self):
        #: native id -> first user text (None: the session exists and has no turn yet)
        self.sessions: dict[str, str | None] = {}
        self.unreadable: set[str] = set()
        self.store_readable = True
        self.evidence = (base.EVIDENCE_FOUND, "")
        self.preflight = (base.PREFLIGHT_OK, "")
        self.preflight_saw_fence_free: bool | None = None
        self.on_evidence = None
        self.on_bind = None
        self.bind_polls = 0

    def is_present(self):
        return True

    def new_launch_argv(self, native, *, cwd, bypass):
        return ["/bin/true"]

    def unattended_preflight(self, *, cwd, probe, gate):
        # A READ, which must not run under the global launch-policy fence (#921). Taking the fence
        # here from a worker thread succeeds only if nothing is holding it across this call.
        try:
            with session_input.launch_fence(timeout=0.5):
                self.preflight_saw_fence_free = True
        except session_input.AuthorityFenceBusy:
            self.preflight_saw_fence_free = False
        return self.preflight

    def start_evidence(self, launch):
        if self.on_evidence is not None:
            self.on_evidence(launch)
        return self.evidence

    def snapshot_session_ids(self, cwd):
        return set(self.sessions) if self.store_readable else None

    def bind_session(self, launch):
        self.bind_polls += 1
        if self.on_bind is not None:
            self.on_bind(self.bind_polls)
        return launch_binding.bind_by_nonce(self, launch)


@pytest.fixture
def work(tmp_path, monkeypatch):
    sockdir = tempfile.mkdtemp(prefix="as-lb-")
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", sockdir)
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "metadata.json"))
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    handoff.reset_for_tests()
    d = tmp_path / "work"
    d.mkdir()
    yield d
    handoff.reset_for_tests()
    missions.reset_schema_cache_for_test()
    shutil.rmtree(sockdir, ignore_errors=True)


@pytest.fixture
def prov(monkeypatch, work):
    p = FakeLate()
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: p if e == "kimi" else None)

    def adapter(native, home):
        if native in p.unreadable:
            raise OSError("torn transcript")
        text = p.sessions.get(native)
        if text is None:
            return []
        return [types.SimpleNamespace(role="user", kind="text", text=text)]

    monkeypatch.setitem(transcript._ADAPTERS, "kimi", adapter)
    monkeypatch.setattr(headless_dispatch, "START_POLL_S", 0.01)
    return p


@pytest.fixture
def spawned(monkeypatch):
    calls: list = []

    class P:
        returncode = 0

        def wait(self):
            return 0

    def fake(argv, **kw):
        calls.append(argv)
        i = list(argv).index("-n") + 1
        open(argv[i], "w").close()
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", fake)
    return calls


@pytest.fixture
def typed(monkeypatch, prov):
    """Delivery succeeds and records the text; `prov.on_type` lets a test make the engine react."""
    sent: list[str] = []
    prov.on_type = None

    async def deliver(key, seed_key, **kw):
        text = handoff.claim_seed(seed_key)
        handoff.ack_seed(seed_key, "delivered")
        sent.append(text)
        if prov.on_type is not None:
            prov.on_type(text)
        return True, ""

    monkeypatch.setattr(headless_seed, "deliver", deliver)
    return sent


@pytest.fixture
def torn_down(monkeypatch):
    """The lowest teardown seam. Everything above it — `mission_fence`, the runtime parser, the
    store's physical mapping — runs for real."""
    calls: list[str] = []

    async def fake(engine, native, **kw):
        calls.append(f"{engine}:{native}")
        return "term"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", fake)
    return calls


class Reg:
    async def ensure_headless(self, engine, sid):
        return None


def _dispatch(work, **kw):
    return headless_dispatch.dispatch(
        registry=Reg(),
        engine="kimi",
        cwd=str(work),
        brief=kw.pop("brief", BRIEF),
        start_timeout=kw.pop("start_timeout", 0.3),
        **kw,
    )


def _line(text: str) -> str:
    return text.rsplit("\n", 1)[-1]


# ---- binding -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_binds_the_session_that_carries_THIS_attempts_nonce(
    work, prov, spawned, typed, torn_down
):
    prov.on_type = lambda text: prov.sessions.__setitem__(OURS, text)
    out = await _dispatch(work)
    assert out.ok, out.reason
    assert out.key.startswith("kimi:new-"), "a late-id engine must launch under a placeholder"
    assert out.bound is True and out.bound_key == f"kimi:{OURS}"
    assert out.bound_proof == base.PROOF_NONCE
    assert out.adopt_key == f"kimi:{OURS}"
    assert typed[0].startswith(BRIEF)
    assert _line(typed[0]) == launch_binding.nonce_line(out.nonce)
    assert torn_down == [], "a bound launch is not torn down"


@pytest.mark.anyio
async def test_the_SAME_BRIEF_without_the_nonce_is_never_bound_and_ours_binds_when_it_appears(
    work, prov, spawned, typed, torn_down
):
    """Correlation is not attribution: another session given the same text, first."""
    prov.on_evidence = lambda launch: prov.sessions.__setitem__(OTHER, BRIEF)

    def ours_later(n):
        if n == 3:
            prov.sessions[OURS] = typed[0]

    prov.on_bind = ours_later
    out = await _dispatch(work, start_timeout=2.0)
    assert out.ok, out.reason
    assert out.bound_key == f"kimi:{OURS}", "the session that did not receive this paste was bound"


@pytest.mark.anyio
async def test_the_SAME_BRIEF_without_the_nonce_and_ours_NEVER_appears_fails_and_tears_down(
    work, prov, spawned, typed, torn_down
):
    prov.on_evidence = lambda launch: prov.sessions.__setitem__(OTHER, BRIEF)
    out = await _dispatch(work)
    assert out.ok is False
    assert out.bound is False and out.bound_key == ""
    assert out.state == "unbound"
    assert "never appeared" in out.reason
    assert torn_down == [out.key], "an unbound launch must be torn down by its placeholder"


@pytest.mark.anyio
async def test_an_interactive_session_with_a_DIFFERENT_first_turn_is_never_bound(
    work, prov, spawned, typed, torn_down
):
    prov.on_evidence = lambda launch: prov.sessions.__setitem__(OTHER, "something else entirely")
    out = await _dispatch(work)
    assert out.ok is False and out.bound_key == ""
    assert "never appeared" in out.reason


@pytest.mark.anyio
async def test_TWO_sessions_carrying_the_nonce_is_ambiguous_and_nothing_is_picked(
    work, prov, spawned, typed, torn_down
):
    def both(text):
        prov.sessions[OURS] = text
        prov.sessions[OTHER] = text

    prov.on_type = both
    out = await _dispatch(work)
    assert out.ok is False and out.bound_key == ""
    assert "none was picked" in out.reason
    assert torn_down == [out.key]


@pytest.mark.anyio
async def test_a_BOUND_answer_without_a_proof_is_not_a_binding(
    work, prov, spawned, typed, torn_down, monkeypatch
):
    monkeypatch.setattr(
        prov, "bind_session", lambda launch: base.Binding(base.BIND_BOUND, native=OURS)
    )
    out = await _dispatch(work)
    assert out.ok is False and out.bound_key == ""
    assert "without a proof" in out.reason


@pytest.mark.anyio
async def test_UNREADABLE_until_the_deadline_says_could_not_tell_never_that_it_did_not_appear(
    work, prov, spawned, typed, torn_down
):
    def torn(text):
        prov.sessions[OURS] = text
        prov.unreadable.add(OURS)

    prov.on_type = torn
    out = await _dispatch(work)
    assert out.ok is False and out.bound_key == ""
    assert "could not tell which kimi session" in out.reason
    assert "never appeared" not in out.reason


# ---- nothing is typed or spawned when it must not be ----------------------------------------


@pytest.mark.anyio
async def test_a_brief_AT_THE_CAP_is_refused_before_spawn_because_its_nonce_would_not_fit(
    work, prov, spawned, typed
):
    with pytest.raises(headless_dispatch.DispatchError) as e:
        await _dispatch(work, brief="x" * handoff.SEED_CAP_BYTES)
    assert "no room" in str(e.value)
    assert spawned == [] and typed == []


@pytest.mark.anyio
async def test_NO_START_EVIDENCE_means_nothing_is_typed(work, prov, spawned, typed, torn_down):
    prov.evidence = (base.EVIDENCE_ABSENT, "a trust screen is up")
    out = await _dispatch(work)
    assert out.launched is True and out.started is False and out.briefed is False
    assert typed == [], "the brief was typed into a launch with no start evidence"
    assert torn_down == [out.key]


@pytest.mark.anyio
async def test_an_engine_MISSING_a_capability_is_refused_naming_what_is_missing(
    work, monkeypatch, spawned
):
    class NoBinder(FakeLate):
        bind_session = None

    p = NoBinder()
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: p)
    with pytest.raises(headless_dispatch.DispatchError) as e:
        await _dispatch(work)
    assert "bind_session" in str(e.value)
    assert "does not reveal its session id" in str(e.value)
    assert spawned == []


@pytest.mark.anyio
async def test_a_second_unattended_launch_in_the_same_folder_is_refused_before_spawn(
    work, prov, spawned, typed
):
    held = sessionlock.acquire(headless_dispatch._admission_key("kimi", str(work)))
    assert held is not None
    try:
        out = await _dispatch(work)
    finally:
        held.release()
    assert out.launched is False
    assert "another unattended kimi launch" in out.reason
    assert spawned == []


@pytest.mark.anyio
async def test_an_UNREADABLE_store_before_the_launch_refuses_rather_than_reading_as_empty(
    work, prov, spawned, typed
):
    prov.store_readable = False
    out = await _dispatch(work)
    assert out.launched is False
    assert "could not be read before the launch" in out.reason
    assert spawned == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer,expected",
    [
        ((base.PREFLIGHT_REFUSED, "logged out"), "refused an unattended start"),
        ((base.PREFLIGHT_UNKNOWN, "probe timed out"), "could not confirm"),
    ],
)
async def test_a_preflight_that_is_not_OK_refuses_before_spawn(
    work, prov, spawned, typed, answer, expected
):
    prov.preflight = answer
    out = await _dispatch(work)
    assert out.launched is False
    assert expected in out.reason
    assert spawned == []


@pytest.mark.anyio
async def test_the_preflight_READ_never_runs_under_the_launch_policy_fence(
    work, prov, spawned, typed, torn_down
):
    prov.on_type = lambda text: prov.sessions.__setitem__(OURS, text)
    await _dispatch(work)
    assert prov.preflight_saw_fence_free is True


@pytest.mark.anyio
async def test_CANCELLED_while_binding_publishes_nothing_and_tears_down(
    work, prov, spawned, typed, torn_down
):
    """The cancel lands while the binder is INSIDE a poll, so its answer can never be used.

    The barrier is what makes that deterministic. Arming the session and *then* cancelling races
    the poll — under load (measured at load average 59) the bind wins and the dispatch completes,
    so the test failed for a reason that had nothing to do with the code. Parking the provider
    mid-poll removes the race instead of hoping to win it. Both waits are bounded, so a wedge
    fails this test rather than hanging the run (#970).
    """
    parked = threading.Event()
    release = threading.Event()

    def park(_poll):
        prov.sessions[OURS] = typed[0]  # from here a poll WOULD bind; none of them may land
        parked.set()
        release.wait(10)

    prov.on_bind = park
    task = asyncio.ensure_future(_dispatch(work, start_timeout=30))
    for _ in range(1000):
        if parked.is_set():
            break
        await asyncio.sleep(0.01)
    assert parked.is_set(), "the dispatch never reached binding"

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert metadata.load_aliases() == {}
    assert len(torn_down) == 1 and torn_down[0].startswith("kimi:new-")


# ---- the mission ---------------------------------------------------------------------------


def _claimed(work, *, owner=None):
    m = missions.create_mission("ship it", cwd=str(work))
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd=str(work), engine="kimi", brief=BRIEF)
    kw = {"owner": owner} if owner else {}
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"], **kw)


def test_a_MISSION_adopts_the_real_key_records_where_it_runs_and_publishes_the_alias(
    work, prov, spawned, typed, torn_down
):
    seen: dict = {}

    def react(text):
        seen["record"] = missions.get_dispatch(mid)
        prov.sessions[OURS] = text

    prov.on_type = react
    mid, claimed = _claimed(work)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=Reg()))
    assert out["state"] == "running" and out["outcome"] == "started", out
    real = f"kimi:{OURS}"
    assert out["session_key"] == real
    placeholder = seen["record"]["session_key"]
    assert placeholder.startswith("kimi:new-")
    assert seen["record"]["attempt_nonce"] and _line(typed[0]).endswith(
        f"{seen['record']['attempt_nonce']}]"
    ), "the record's nonce is not the one that was typed"
    assert missions.active_session_keys(mid) == [real]
    assert missions.physical_key_of(real) == placeholder
    assert metadata.load_aliases().get(placeholder) == real
    assert missions.get_dispatch(mid) is None


def test_DELIVERED_but_UNBOUND_fails_and_Start_again_stays_refused(
    work, prov, spawned, typed, torn_down
):
    mid, claimed = _claimed(work)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=Reg()))
    assert out["state"] == "failed" and out["outcome"] == "failed", out
    assert "never appeared" in out["reason"]
    assert missions.active_session_keys(mid) == []
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "failed", "planned")
    assert e.value.status == 409, "a launch that typed its brief became eligible to start again"


def test_a_FAILED_publication_is_repaired_at_startup_and_repair_is_idempotent(
    work, prov, spawned, typed, torn_down, monkeypatch
):
    prov.on_type = lambda text: prov.sessions.__setitem__(OURS, text)
    real_set_alias = metadata.set_alias

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(metadata, "set_alias", broken)
    mid, claimed = _claimed(work)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=Reg()))
    assert out["state"] == "running", "a failed projection must never undo the adoption"
    assert metadata.load_aliases() == {}
    placeholder = missions.physical_key_of(f"kimi:{OURS}")
    assert placeholder

    monkeypatch.setattr(metadata, "set_alias", real_set_alias)
    assert asyncio.run(mission_dispatch_recover.repair_projections()) == 1
    assert metadata.load_aliases()[placeholder] == f"kimi:{OURS}"
    assert asyncio.run(mission_dispatch_recover.repair_projections()) == 0


def test_a_CRASH_between_the_adopting_commit_and_publication_is_repaired(work, torn_down):
    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    verdict = mission_dispatch.fenced_settle(
        mid,
        to="running",
        detail="bound",
        session_key=f"kimi:{OURS}",
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
    )
    assert verdict["settled"], verdict
    # …and the process died here, before `publish_binding` ran.
    assert metadata.load_aliases() == {}
    assert asyncio.run(mission_dispatch_recover.repair_projections()) == 1
    assert metadata.load_aliases()[placeholder] == f"kimi:{OURS}"


def test_RECOVERY_tears_down_an_unbound_placeholder_through_the_real_fence_and_adopts_nothing(
    work, torn_down
):
    mid, claimed = _claimed(work, owner=DEAD_OWNER)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    assert missions.note_dispatch_session(
        mid, placeholder, expect_plan=claimed["plan_id"], nonce="a" * 32
    )
    moved = asyncio.run(mission_dispatch_recover.recover_once())
    assert moved == 1
    m = missions.get_mission(mid)
    assert m["state"] == "failed"
    assert any("before the session id was bound" in (e.get("text") or "") for e in m["events"])
    assert torn_down == [placeholder], "the teardown did not go through the placeholder"
    assert missions.active_session_keys(mid) == []


def test_ownership_is_found_BY_THE_PLACEHOLDER_so_a_teardown_spares_an_adopted_session(
    work, torn_down
):
    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    verdict = mission_dispatch.fenced_settle(
        mid,
        to="running",
        detail="bound",
        session_key=real,
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
        keep_record=True,  # the window before `clear_dispatch`
    )
    assert verdict["settled"], verdict
    assert missions.holder_of(placeholder) == mid
    rows = [r for r in missions.unsettled_dispatches() if r["mission_id"] == mid]
    assert rows and rows[0]["held"] is True, "recovery would treat the adopted session as orphaned"
    assert asyncio.run(mission_fence.abandon(placeholder)) == "spared"
    assert torn_down == []


def test_the_FIRST_adoption_locks_the_recorded_placeholder_and_later_ones_read_the_store(
    work, monkeypatch
):
    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    # Before the mapping exists, resolving the real key cannot find the runtime.
    assert mission_fence.adoption_keys(mid, real)[1] == real
    assert mission_fence.adoption_keys(mid, real, physical_key=placeholder)[1] == placeholder

    locked: list[list[str]] = []
    real_tx = session_input.sessions_transaction

    @contextlib.contextmanager
    def spy(keys):
        locked.append(list(keys))
        with real_tx(keys):
            yield

    monkeypatch.setattr(session_input, "sessions_transaction", spy)
    verdict = mission_dispatch.fenced_settle(
        mid,
        to="running",
        detail="bound",
        session_key=real,
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
    )
    assert verdict["settled"], verdict
    assert locked == [[mission_fence.roster_key(mid), placeholder]]
    # After the commit, the store answers — with no alias published at all.
    assert metadata.load_aliases() == {}
    assert mission_fence.physical_of(real) == placeholder
    assert mission_fence.held_keys(mid) == [placeholder]


def test_a_TEARDOWN_holding_the_placeholder_serialises_with_the_adopting_settlement(work):
    """The pre-commit interval, through the real fence: while a teardown holds the placeholder's
    fence, the settlement that would adopt it cannot commit."""
    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    holding = threading.Event()
    release = threading.Event()

    def teardown():
        with session_input.sessions_transaction([placeholder]):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=teardown)
    t.start()
    assert holding.wait(5)
    result: dict = {}

    def settle():
        try:
            result["verdict"] = mission_dispatch.fenced_settle(
                mid,
                to="running",
                detail="bound",
                session_key=f"kimi:{OURS}",
                physical_key=placeholder,
                expect_plan=claimed["plan_id"],
            )
        except session_input.AuthorityFenceBusy as e:
            result["busy"] = e

    s = threading.Thread(target=settle)
    s.start()
    s.join(0.3)
    assert missions.active_session_keys(mid) == [], "the adoption committed inside the teardown"
    release.set()
    t.join(5)
    s.join(10)
    assert "busy" in result or result["verdict"]["settled"]


# ---- the gates stay strict -----------------------------------------------------------------


def test_the_RUNTIME_parser_accepts_a_placeholder_and_every_public_parser_still_refuses_it():
    placeholder = f"kimi:new-{uuid.uuid4()}"
    with pytest.raises(engines.EngineError):
        engines.parse_key(placeholder)
    with pytest.raises(engines.EngineError):
        engines.canonical_key(placeholder)
    prov, native = engines.parse_runtime_key(placeholder)
    assert prov.engine_id == "kimi" and native == placeholder.split(":", 1)[1]
    with pytest.raises(engines.EngineError):
        engines.parse_runtime_key(f"claude:new-{uuid.uuid4()}"), "claude pins its id"


def test_the_MISSION_picker_drops_an_incapable_late_id_engine_and_the_HANDOFF_picker_does_not(
    monkeypatch,
):
    class Late:
        engine_id = "kimi"
        supports_seed_start = True
        new_session_reconciles = True

        def is_present(self):
            return True

    monkeypatch.setattr(engines, "all_providers", lambda: [Late()])
    assert mission_plan.engine_options() == []
    assert handoff.seed_start_state(Late(), present=True) == (True, None)


def test_every_REAL_late_id_engine_is_still_refused_with_the_capability_reason():
    late = [p for p in engines.all_providers() if getattr(p, "new_session_reconciles", False)]
    assert {p.engine_id for p in late} >= {"codex", "opencode", "kimi", "antigravity"}
    for p in late:
        ok, why = engines.unattended_start_state(p)
        assert ok is False, f"{p.engine_id} became dispatchable without a measured adapter"
        assert "no unattended start check yet" in why


def _columns(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_v28_step_adds_both_columns_to_a_v27_shape_and_converges_when_rerun():
    """A synthetic v26 shape, not a real store with the columns dropped: SQLite's DROP COLUMN
    rewrites the table's own DDL text and cannot parse the commented schema this store carries."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE mission_sessions (mission_id TEXT, session_key TEXT)")
    con.execute("CREATE TABLE mission_dispatches (mission_id TEXT PRIMARY KEY)")
    try:
        missions._migrate_27_to_28(con)
        missions._migrate_27_to_28(con)  # a partially upgraded file converges
        assert _tables(con) >= {"session_runtime_bindings"}
        assert _columns(con, "session_runtime_bindings") == {
            "logical_key",
            "physical_key",
            "bound_at",
        }
        assert "attempt_nonce" in _columns(con, "mission_dispatches")
    finally:
        con.close()


def test_a_FRESH_store_has_both_columns_and_the_ladder_reaches_the_current_version(work):
    """Fresh installs are built from the base DDL, not the ladder — so both have to carry them.

    The version this lands on is deliberately NOT a literal. #983 P4 added v29 after this suite was
    written, so pinning 28 asserted "no later migration exists" — which is not what this test is
    about, and is a tripwire every future migration would have to come back and edit. Spelled the
    way the other schema pins are (`test_missions.py`, `test_mission_plan_intent.py`,
    `test_mission_directions.py`): the subject is that the ladder CONVERGES, columns and all.
    """
    missions.list_missions()
    path = work.parent / "m.db"
    con = sqlite3.connect(path)
    try:
        assert "session_runtime_bindings" in _tables(con)
        assert "attempt_nonce" in _columns(con, "mission_dispatches")
        # An upgraded file whose columns already exist must still converge, not fail the step.
        con.execute("PRAGMA user_version=27")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()
    missions.list_missions()
    con = sqlite3.connect(path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    assert version == missions.SCHEMA_VERSION


# ---- #994 review 1 ---------------------------------------------------------------------------

PARENT = "claude:0a0a0a0a-1111-1111-1111-111111111111"


def _running_with_parent(work):
    mid, claimed = _claimed(work)
    assert missions.note_dispatch_session(mid, PARENT, expect_plan=claimed["plan_id"])
    verdict = missions.settle_dispatch(
        mid, to="running", detail="up", session_key=PARENT, expect_plan=claimed["plan_id"]
    )
    assert verdict["settled"], verdict
    return mid


def test_the_REAPER_probes_a_late_bound_childs_PLACEHOLDER_and_keeps_its_slot_charged(
    work, monkeypatch
):
    """Finding 1. The ledger names the real key; the master lives under the placeholder."""
    from agent_sessions.routes import missions as routes

    mid = _running_with_parent(work)
    missions.claim_spawn(mid, parent_key=PARENT, engine="kimi", cwd=str(work), brief="child")
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    verdict = missions.settle_dispatch(
        mid, to="running", detail="bound", session_key=real, physical_key=placeholder
    )
    assert verdict["settled"], verdict
    assert missions.open_spawn_count(mid) == 1

    live_socket = ptybridge.socket_path("kimi", placeholder.split(":", 1)[1])
    alive = {"placeholder": True}

    def probe(path):
        if str(path) == str(live_socket) and alive["placeholder"]:
            return "alive"
        return ptybridge.DEAD  # the real key's socket does not exist

    monkeypatch.setattr(ptybridge, "probe_master", probe)
    assert routes._reap_dead_spawns(mid) == 0
    assert missions.open_spawn_count(mid) == 1, "a running late-bound child's slot was freed"

    alive["placeholder"] = False
    assert routes._reap_dead_spawns(mid) == 1
    assert missions.open_spawn_count(mid) == 0


def test_an_UNPUBLISHED_alias_still_resolves_to_the_placeholder_so_no_second_writer_launches(
    work, monkeypatch
):
    """Finding 2. A failed publication, then the returned real key is opened straight away."""
    from agent_sessions import sessions
    from agent_sessions.routes import terminal

    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    verdict = mission_dispatch.fenced_settle(
        mid,
        to="running",
        detail="bound",
        session_key=real,
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
    )
    assert verdict["settled"], verdict
    assert metadata.load_aliases() == {}, "this test is about the window with no alias"

    kimi = engines.get("kimi")
    phys = asyncio.run(terminal.resolve_physical_key(kimi, OURS, is_new=False))
    assert phys == placeholder

    master_lock = sessionlock.acquire(placeholder)  # the running master holds its own flock
    assert master_lock is not None
    try:
        action, lock = sessions.open_action("kimi", phys.split(":", 1)[1])
        assert action != sessions.LAUNCH, "the placeholder's writer would have been joined"
        if lock is not None:
            lock.release()
        # The control: resolving the real key to itself is the duplicate writer this prevents.
        action, lock = sessions.open_action("kimi", OURS)
        assert action == sessions.LAUNCH
        lock.release()
    finally:
        master_lock.release()

    def unreadable(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(missions, "physical_key_of", unreadable)
    other = "session_44444444-5555-6666-7777-888888888888"
    assert asyncio.run(terminal.resolve_physical_key(kimi, other, is_new=False)) is None
    # A pinned-id engine never has a mapping, so it never reads the store at all.
    claude = engines.get("claude")
    native = "12121212-3434-5656-7878-909090909090"
    assert (
        asyncio.run(terminal.resolve_physical_key(claude, native, is_new=False))
        == f"claude:{native}"
    )


def test_a_session_RELEASED_by_one_mission_and_ADOPTED_by_another_keeps_its_physical_mapping(
    work, torn_down
):
    """Finding 3. Ownership asked by the placeholder must follow the session across missions."""
    mid_a, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    assert missions.note_dispatch_session(mid_a, placeholder, expect_plan=claimed["plan_id"])
    verdict = mission_dispatch.fenced_settle(
        mid_a,
        to="running",
        detail="bound",
        session_key=real,
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
    )
    assert verdict["settled"], verdict
    missions.detach(mid_a, real)
    assert missions.holder_of(placeholder) is None

    mid_b = missions.create_mission("take it over", cwd=str(work))["id"]
    mission_dispatch.fenced_adopt(mid_b, real)
    assert missions.holder_of(real) == mid_b
    assert missions.holder_of(placeholder) == mid_b, "the new owner is invisible by placeholder"
    assert mission_fence.held_keys(mid_b) == [placeholder]
    assert asyncio.run(mission_fence.abandon(placeholder)) == "spared"
    assert torn_down == [], "a teardown by placeholder stopped a session mission B holds"


@pytest.mark.anyio
async def test_a_STALLED_binder_cannot_hold_the_dispatch_past_its_budget(
    work, prov, spawned, typed, torn_down, monkeypatch
):
    """Finding 4. The budget bounds each call, a late answer never binds, and the folder's
    admission flock is released on time."""
    import time as _time

    def stalled(launch):
        _time.sleep(3.0)  # reads nothing: the thread left running after the test is harmless
        return base.Binding(base.BIND_BOUND, native=OURS, proof=base.PROOF_NONCE)

    monkeypatch.setattr(prov, "bind_session", stalled)
    t0 = _time.monotonic()
    out = await _dispatch(work, start_timeout=0.3)
    elapsed = _time.monotonic() - t0
    assert out.ok is False and out.bound_key == "", "a late answer was bound"
    assert "did not answer within the binding budget" in out.reason
    assert elapsed < 2.0, f"the dispatch waited {elapsed:.2f}s on a stalled binder"
    assert torn_down == [out.key]
    freed = sessionlock.acquire(headless_dispatch._admission_key("kimi", str(work)))
    assert freed is not None, "the folder's admission flock outlived the dispatch"
    freed.release()


# ---- #994 review 2 ---------------------------------------------------------------------------


def _adopted_without_alias(work, *, close=False):
    """A mission that adopted a late-bound session whose alias was never published."""
    mid, claimed = _claimed(work)
    placeholder = f"kimi:new-{uuid.uuid4()}"
    real = f"kimi:{OURS}"
    assert missions.note_dispatch_session(mid, placeholder, expect_plan=claimed["plan_id"])
    verdict = mission_dispatch.fenced_settle(
        mid,
        to="running",
        detail="bound",
        session_key=real,
        physical_key=placeholder,
        expect_plan=claimed["plan_id"],
    )
    assert verdict["settled"], verdict
    assert metadata.load_aliases() == {}, "these tests are about the window with no alias"
    if close:
        missions.set_state(mid, "running", "done", outcome="done")
    return mid, placeholder, real


class Stops(list):
    """The keys termination was asked to stop, plus the answer it gives (`outcome`)."""

    outcome = "term"


@pytest.fixture
def stops(monkeypatch, work):
    """Only process termination is stubbed. Resolution, archive and settlement run for real."""
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(work.parent / "kimi-store"))
    calls = Stops()

    async def terminate(engine, native, *, key=None, spare_if=None):
        calls.append(key)
        return calls.outcome

    monkeypatch.setattr(runtime_cleanup.reaper, "terminate_master", terminate)
    return calls


def test_the_RESOLVER_reads_the_store_only_for_an_unaliased_late_id_key(work, monkeypatch):
    mid, placeholder, real = _adopted_without_alias(work)
    assert asyncio.run(runtime_cleanup.resolve_runtime_key("kimi", OURS)) == placeholder
    native = placeholder.split(":", 1)[1]
    assert asyncio.run(runtime_cleanup.resolve_runtime_key("kimi", native)) == placeholder

    def unreadable(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(missions, "physical_key_of", unreadable)
    with pytest.raises(runtime_cleanup.UnresolvableRuntime):
        asyncio.run(runtime_cleanup.resolve_runtime_key("kimi", OURS))
    claude = "12121212-3434-5656-7878-909090909090"
    assert asyncio.run(runtime_cleanup.resolve_runtime_key("claude", claude)) == f"claude:{claude}"


def test_ARCHIVING_a_late_bound_session_with_NO_ALIAS_stops_its_placeholder_runtime(work, stops):
    """Finding 1, through the real mission archive: the mapped master is the termination target."""
    from agent_sessions import mission_archive

    mid, placeholder, real = _adopted_without_alias(work)
    out = asyncio.run(mission_archive.archive_mission(mid, abandon=True))
    assert stops == [placeholder], f"archive terminated {stops}, not the placeholder runtime"
    assert out["archived"] is True


def test_ARCHIVING_refuses_a_session_whose_mapping_cannot_be_read_and_stops_nothing(
    work, stops, monkeypatch
):
    from agent_sessions import mission_archive

    mid, placeholder, real = _adopted_without_alias(work, close=True)

    def unreadable(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(missions, "physical_key_of", unreadable)
    out = asyncio.run(mission_archive.archive_mission(mid))
    assert stops == [], "a runtime nobody could locate was signalled"
    assert [f["session_key"] for f in out["not_archived"]] == [real]
    rows = missions.archive_sessions_for(mid)
    assert rows[0]["archive_state"] == "failed"
    assert "could not tell where" in rows[0]["archive_error"]


@pytest.mark.anyio
async def test_a_CANCELLATION_while_the_admission_lock_is_being_taken_still_releases_it(
    work, prov, spawned, typed, torn_down, monkeypatch
):
    """Finding 2. The flock is taken on a worker; the dispatch is cancelled before it is bound."""
    real_acquire = sessionlock.acquire
    taken = threading.Event()
    go = threading.Event()

    def barrier(key):
        lock = real_acquire(key)
        if key.startswith("unattended-admit-"):
            taken.set()
            go.wait(5)
        return lock

    monkeypatch.setattr(headless_dispatch.sessionlock, "acquire", barrier)
    task = asyncio.ensure_future(_dispatch(work, start_timeout=5))
    for _ in range(500):
        if taken.is_set():
            break
        await asyncio.sleep(0.01)
    assert taken.is_set(), "the dispatch never reached the admission lock"
    task.cancel()
    await asyncio.sleep(0.05)
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    again = real_acquire(headless_dispatch._admission_key("kimi", str(work)))
    assert again is not None, "the cancelled acquisition stranded the folder's admission flock"
    again.release()
    assert spawned == [], "nothing may be spawned by a dispatch cancelled before its launch"


# ---- #994 review 3 ---------------------------------------------------------------------------


def test_RETENTION_deleting_a_closed_mission_keeps_the_runtime_BINDING(work, monkeypatch):
    """The blocker: the mapping used to live on mission_sessions, which cascades on deletion.

    Failed publication -> the mission closes while its agent runs -> retention deletes the closed
    mission. The binding is a fact about the RUNTIME, so it must survive all three, or the terminal
    launches a second writer beside a live agent and no repair can ever put it back.
    """
    from agent_sessions.routes import terminal

    mid, placeholder, real = _adopted_without_alias(work, close=True)
    with contextlib.suppress(Exception):
        missions.clear_dispatch(mid, stopped=True)

    deleted = missions.retention_pass(days=1, now=time.time() + 5 * 86400)
    assert deleted >= 1, "retention deleted nothing, so this test proves nothing"
    assert missions.get_mission(mid) is None

    assert missions.physical_key_of(real) == placeholder, "the mapping went with mission history"
    assert missions.physical_bindings() == [(real, placeholder)]
    assert asyncio.run(runtime_cleanup.resolve_runtime_key("kimi", OURS)) == placeholder
    assert (
        asyncio.run(terminal.resolve_physical_key(engines.get("kimi"), OURS, is_new=False))
        == placeholder
    )
    # …and the alias repair can still publish it, which is what the earlier design lost for good.
    assert asyncio.run(mission_dispatch_recover.repair_projections()) == 1
    assert metadata.load_aliases()[placeholder] == real


def test_a_TEARDOWN_keeps_the_binding_even_when_it_PROVED_THE_BOUNDARY_EMPTY(work, stops):
    """Append-only (#994 review 5, #1017): a proved stop is not proof the mapping is retirable.

    `cleanup_runtime` leaves the socket alone while the physical key's launch lock is HELD — a NEW
    generation owns that path — and still answers with the OLD master's result. So `stopped` can
    describe a boundary a replacement already took, and retiring the mapping on it strands that
    replacement behind a logical key that resolves to itself.
    """
    from agent_sessions import sessionlock
    from agent_sessions.routes import terminal

    mid, placeholder, real = _adopted_without_alias(work)

    # Held by the mission: spared before anything is signalled, and the mapping stays.
    assert asyncio.run(mission_fence.abandon(placeholder)) == "spared"
    assert stops == []
    assert missions.physical_key_of(real) == placeholder

    # Nobody holds it now, and the teardown could not prove the boundary empty.
    missions.detach(mid, real)
    stops.outcome = "leaked"
    assert asyncio.run(mission_fence.abandon(placeholder)) == "leaked"
    assert missions.physical_key_of(real) == placeholder, "a leak dropped the only mapping"

    # A REPLACEMENT GENERATION holds the placeholder's writer lock while the old master is
    # reaped, which is exactly when cleanup spares its socket and still reports the old stop.
    replacement = sessionlock.acquire(placeholder)
    assert replacement is not None, "a replacement needs the lock this test has it hold"
    try:
        stops.outcome = "term"
        assert asyncio.run(mission_fence.abandon(placeholder)) == "stopped"
        assert (
            missions.physical_key_of(real) == placeholder
        ), "a proved stop retired the mapping of a runtime a replacement already owns"
        assert missions.physical_bindings() == [(real, placeholder)]
        # …so the next reader still lands on the replacement, not beside it as a second writer.
        assert (
            asyncio.run(terminal.resolve_physical_key(engines.get("kimi"), OURS, is_new=False))
            == placeholder
        )
    finally:
        replacement.release()


def test_an_ARCHIVE_keeps_the_binding_when_TERMINATION_LEAKED(work, stops):
    """`leaked` is a process group that survived SIGKILL, so the mapping still names something."""
    from agent_sessions import mission_archive

    mid, placeholder, real = _adopted_without_alias(work)
    stops.outcome = "leaked"
    out = asyncio.run(mission_archive.archive_mission(mid, abandon=True))
    assert out["archived"] is True
    assert stops == [placeholder], "the archive must still target the MAPPED master"
    assert (
        missions.physical_key_of(real) == placeholder
    ), "the archive dropped the only locator of an agent that survived SIGKILL"
    assert missions.physical_bindings() == [(real, placeholder)]


def test_an_ARCHIVE_keeps_the_binding_when_TERMINATION_RAISED(work, monkeypatch):
    """The teardown is exception-suppressed, so a raise must not read as a proved stop either."""
    from agent_sessions import mission_archive

    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(work.parent / "kimi-store"))
    mid, placeholder, real = _adopted_without_alias(work)

    async def boom(*_a, **_k):
        raise OSError("no such process")

    monkeypatch.setattr(runtime_cleanup.reaper, "terminate_master", boom)
    out = asyncio.run(mission_archive.archive_mission(mid, abandon=True))
    assert out["archived"] is True
    assert (
        missions.physical_key_of(real) == placeholder
    ), "a raised termination proved nothing, yet the mapping went"
    assert missions.physical_bindings() == [(real, placeholder)]
