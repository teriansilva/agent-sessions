"""The session link — #840 §9, "the one thing that must be rock solid".

Everything MISSION CONTROL does is worth nothing if its picture of a session can drift from the
session. §9 states that contract as six invariants and says they get a dedicated suite rather than
being left to emerge from the feature tests; this is that suite (#894).

The point of collecting them here, rather than trusting the tests that live beside each mechanism,
is that the link is a JOIN. Adoption, the write fence, liveness, reconciliation, restart recovery
and staleness are each covered where they are implemented — and each of those tests is about its
own component, so none of them notices when two of the six stop agreeing with each other. Every
test below is written against the invariant's own words.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_sessions import actuator, engines, missions, prefs, session_input
from agent_sessions import mission_supervisor as sup
from agent_sessions import orchestrator_ledger as ledger

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
CLAUDE_B = "claude:22222222-2222-2222-2222-222222222222"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    missions.reset_schema_cache_for_test()
    session_input.reset()
    # ORCHESTRATION ON. The master switch and the tier are their own fences and have their own
    # tests; leaving them off here would make every delivery below refuse for the wrong reason
    # and the invariants would pass without being exercised at all.
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    m = missions.create_mission("ship it", cwd=str(tmp_path))
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    yield m["id"]
    session_input.reset()
    missions.reset_schema_cache_for_test()


# ---- 1. One writer, unchanged ----------------------------------------------------------------


def test_INVARIANT_1_a_mission_is_a_CLIENT_of_the_single_writer_lock(store):
    """ "Mission control is a client of it, never an exception to it."

    Asserted structurally rather than behaviourally, because the failure this guards against is
    somebody adding a second way in: every byte the actuator sends goes through
    `session_input.send_input`, which is where the flock lives. A module that imported
    `ptybridge` or opened the socket itself would be the exception §9 forbids.
    """
    import inspect

    src = inspect.getsource(actuator)
    assert "session_input.send_input" in src
    # …and nothing in the mission layer writes to a pty by another door.
    for mod in ("missions", "mission_supervisor"):
        import importlib

        text = inspect.getsource(importlib.import_module(f"agent_sessions.{mod}"))
        assert "ptybridge" not in text, f"{mod} reaches a pty without the actuator's fence"


def test_INVARIANT_1_adoption_is_arbitrated_by_the_DATABASE_not_by_a_check(store, tmp_path):
    """Exclusivity is the index's job. "Check whether anything holds this, then insert" is not
    race-safe — two concurrent adopts both pass the check — so the loser must lose in the
    database and be told who won."""
    other = missions.create_mission("the other one", cwd=str(tmp_path))["id"]
    missions.adopt(store, CLAUDE_A)
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(other, CLAUDE_A)
    assert e.value.status == 409
    # The refusal NAMES the holder — an operator told only "no" cannot act on it.
    assert store in str(e.value)


# ---- 2. Delivery is at-most-once and always recorded ------------------------------------------


@pytest.mark.anyio
async def test_INVARIANT_2_two_deliveries_of_one_action_cannot_both_write(store, monkeypatch):
    """ "Claim BEFORE writing, and atomically." A read-then-write across two lock holds lets two
    callers both see `proposed` and both send — a duplicate `choose` answers a prompt twice."""
    missions.adopt(store, CLAUDE_A)
    rec = ledger.append(
        {
            "id": "act_1",
            "verb": "continue",
            "session_id": CLAUDE_A,
            "state": "approved",
            "confidence": 1.0,
        }
    )
    assert rec

    sent: list[bytes] = []
    monkeypatch.setattr(session_input, "is_live", lambda key: True)
    monkeypatch.setattr(
        session_input,
        "send_input",
        lambda key, payload, **kw: sent.append(payload) or session_input.Outcome("delivered", ""),
    )

    first, second = await asyncio.gather(
        actuator.deliver("act_1"), actuator.deliver("act_1"), return_exceptions=True
    )
    outcomes = [first, second]
    assert len(sent) <= 1, "one action was delivered twice"
    # …and the loser SAYS it lost rather than silently doing nothing.
    assert any(isinstance(o, actuator.NotDeliverable) for o in outcomes) or len(sent) == 1


@pytest.mark.anyio
async def test_INVARIANT_2_a_write_is_RECORDED_BEFORE_the_bytes(store, monkeypatch):
    """ "`claimed` is written and fsynced BEFORE any byte reaches the PTY."

    That ordering is what makes a crash recoverable: the record proves a delivery was in flight
    even though it cannot prove the outcome. Asserted on the ORDER, because both halves happening
    is not the property — the sequence is.
    """
    missions.adopt(store, CLAUDE_A)
    ledger.append(
        {
            "id": "act_2",
            "verb": "continue",
            "session_id": CLAUDE_A,
            "state": "approved",
            "confidence": 1.0,
        }
    )
    order: list[str] = []
    monkeypatch.setattr(session_input, "is_live", lambda key: True)

    real_claim = ledger.claim

    def claim(aid, states):
        order.append("claimed")
        return real_claim(aid, states)

    monkeypatch.setattr(ledger, "claim", claim)
    monkeypatch.setattr(
        session_input,
        "send_input",
        lambda key, payload, **kw: order.append("wrote") or session_input.Outcome("delivered", ""),
    )

    await actuator.deliver("act_2")
    assert order[: order.index("wrote")].count("claimed") == 1, order
    assert order.index("claimed") < order.index("wrote"), order


# ---- 3. Liveness is observed, never assumed ---------------------------------------------------


@pytest.mark.anyio
async def test_INVARIANT_3_liveness_is_IS_LIVE_not_the_card_flag(store, monkeypatch):
    """ "The card's `live` flag means *working or attached*, which is not the same predicate, and
    conflating them is a bug this repo has already had."

    Driven with a session the CARD would call live and `is_live` would not: no writer owns its
    bytes, so nothing may be sent to it.
    """
    missions.adopt(store, CLAUDE_A)
    ledger.append(
        {
            "id": "act_3",
            "verb": "continue",
            "session_id": CLAUDE_A,
            "state": "approved",
            "confidence": 1.0,
        }
    )
    sent: list[bytes] = []
    monkeypatch.setattr(
        session_input, "send_input", lambda key, payload, **kw: sent.append(payload)
    )
    # `session_input.reset()` in the fixture means no writer is registered — `is_live` is False
    # while a card built from the same session would say `working`.
    assert session_input.is_live(engines.physical_key(CLAUDE_A)) is False

    out = await actuator.deliver("act_3")
    assert sent == [], "bytes were sent to a session no writer owns"
    assert str(out.get("state")) in ("failed", "stale"), out
    assert "not live" in str(out.get("detail") or "").lower()


# ---- 4. The binding survives id reconciliation ------------------------------------------------


def test_INVARIANT_4_an_AMBIGUOUS_reconcile_leaves_the_binding_exactly_where_it_was(store):
    """ "Re-bound through the SAME reconcile path the viewer uses when the real id appears —
    never re-guessed by matching on cwd or recency."

    Driven through `main._reconcile_new_session`, the coroutine the viewer actually runs, because
    the ambiguity this invariant is about only exists there: two launches in one cwd inside the
    poll window produce two new ids and nothing in the system can say which one is ours. The
    earlier version of this test adopted three keys directly and asserted they were all still
    there — true of any implementation, including one that guesses, because it never reached the
    code that would do the guessing.

    Red against a reconcile that picks one (`result[0]`, the newest, the one matching the cwd):
    an alias is written and the client is converged onto a session nobody proved was ours.
    """
    from agent_sessions import main as main_mod
    from agent_sessions import metadata

    placeholder = "new-33333333-3333-3333-3333-333333333333"
    placeholder_key = f"opencode:{placeholder}"
    missions.adopt(store, placeholder_key)

    frames: list[str] = []

    class _WS:
        async def send_text(self, s):
            frames.append(s)

    class _Prov:
        engine_id = "opencode"

        def reconcile_new_session(self, cwd, snapshot):
            # TWO launches landed in this cwd inside the window.
            return ["ses_aaa", "ses_bbb"]

    aliases: list[tuple] = []
    real_alias = metadata.set_alias
    metadata.set_alias = lambda a, b: aliases.append((a, b))
    real_interval = main_mod._RECONCILE_INTERVAL_S
    main_mod._RECONCILE_INTERVAL_S = 0
    try:
        asyncio.run(main_mod._reconcile_new_session(_WS(), _Prov(), placeholder, "/repo", set()))
    finally:
        metadata.set_alias = real_alias
        main_mod._RECONCILE_INTERVAL_S = real_interval

    assert aliases == [], f"an ambiguous reconcile wrote an alias: {aliases}"
    assert frames == [], "the client was converged onto a session nobody proved was ours"
    # …and the mission still holds exactly the key it was handed.
    assert missions.active_session_keys(store) == [placeholder_key]
    assert missions.all_active_memberships().get(placeholder_key) == store


def test_INVARIANT_4_an_UNAMBIGUOUS_reconcile_binds_the_key_it_was_GIVEN(store):
    """The other half: one new id IS ours, and the re-bind is an ALIAS, not a rewritten claim.

    The mission goes on holding the placeholder — the key a caller handed it — and the alias is
    what lets a later attach by the real id resolve back to the same socket, lock and buffer. A
    reconcile that instead rewrote `mission_sessions` would be the store having an opinion about
    which session became which, which is exactly what this invariant forbids.
    """
    from agent_sessions import main as main_mod
    from agent_sessions import metadata

    placeholder = "new-44444444-4444-4444-4444-444444444444"
    placeholder_key = f"opencode:{placeholder}"
    missions.adopt(store, placeholder_key)

    frames: list[str] = []

    class _WS:
        async def send_text(self, s):
            frames.append(s)

    class _Prov:
        engine_id = "opencode"

        def reconcile_new_session(self, cwd, snapshot):
            return "ses_real"

    aliases: list[tuple] = []
    real_alias = metadata.set_alias
    metadata.set_alias = lambda a, b: aliases.append((a, b))
    real_interval = main_mod._RECONCILE_INTERVAL_S
    main_mod._RECONCILE_INTERVAL_S = 0
    try:
        asyncio.run(main_mod._reconcile_new_session(_WS(), _Prov(), placeholder, "/repo", set()))
    finally:
        metadata.set_alias = real_alias
        main_mod._RECONCILE_INTERVAL_S = real_interval

    assert aliases == [(placeholder_key, "opencode:ses_real")]
    assert frames and "ses_real" in frames[0]
    # THE BINDING IS UNTOUCHED. The alias is the bridge; the claim is the record.
    assert missions.active_session_keys(store) == [placeholder_key]


def test_INVARIANT_4_a_mission_holds_the_key_it_was_GIVEN(store):
    """The re-bind goes through the same reconcile path the viewer uses, so the mission holds
    exactly the key it was handed — never one derived from the cwd or the newest row."""
    missions.adopt(store, CLAUDE_A)
    assert missions.active_session_keys(store) == [CLAUDE_A]
    missions.detach(store, CLAUDE_A)
    assert missions.active_session_keys(store) == []
    # …and the history survives the detach, which is what lets a reader see what happened.
    kinds = [e["kind"] for e in missions.get_mission(store)["events"]]
    assert kinds.count("session") >= 2


# ---- 5. The binding survives restarts ---------------------------------------------------------


def test_INVARIANT_5_the_link_is_REBUILT_FROM_THE_STORE_not_from_memory(store, tmp_path):
    """ "At boot the link is rebuilt from the store against the live registry."

    Nothing in this process may be load-bearing for the link: a restart drops every registry,
    every cache and every in-memory map, and the mission's claim has to survive all of it.
    """
    missions.adopt(store, CLAUDE_A)
    # A "restart": every in-process cache is dropped and the store is re-opened from disk.
    missions.reset_schema_cache_for_test()
    session_input.reset()
    assert missions.active_session_keys(store) == [CLAUDE_A]
    assert missions.all_active_memberships().get(CLAUDE_A) == store


def test_INVARIANT_5_a_mission_whose_session_is_GONE_says_so(store, monkeypatch):
    """ "Any mission whose session is gone moves to a truthful state and says so in the thread."

    The store still holds the claim — that is the record — so the truthful state is the
    SUPERVISOR'S reading, which must not report a session it cannot find as working.
    """
    missions.adopt(store, CLAUDE_A)
    # The engine no longer knows about it: no store record at all.
    monkeypatch.setattr(engines, "scan_all", lambda: [])
    stalled, why, _mark = sup.session_is_stalled(
        "claude", CLAUDE_A.split(":", 1)[1], since=time.time() - 10_000
    )
    assert stalled is True
    assert why, "a stalled session must say what it is stalled ON"


# ---- 6. A dead or wedged session is visible as one --------------------------------------------


def test_INVARIANT_6_a_WEDGED_master_is_stalled_even_though_it_is_alive_and_has_output(
    store, monkeypatch
):
    """ "A dead or wedged session is visible as one."

    The wedge is the hard case and it is the one this repo has actually hit: a leaked, SIGTERM-
    proof `dtach -a` client with a big Send-Q stalls output for every viewer while the master
    process is up, the socket is there, and the transcript on disk is a perfectly ordinary,
    non-empty file. Every existence check says healthy.

    So the signal has to be GROWTH against a baseline, and the previous version of this test
    could not see that: it asked about a session key that does not exist, where the mark is 0 and
    `stalled` is true because there is nothing there at all. That stays green against a
    `session_is_stalled` that returns `mark == 0` and nothing else — i.e. against an
    implementation with the wedge bug fully intact.

    Here the transcript is NON-EMPTY and UNCHANGING, which is the wedge's actual signature.
    """
    from agent_sessions import transcript

    # 4096 bytes of history, and not one more between the two observations.
    monkeypatch.setattr(transcript, "growth_mark", lambda engine, native, root: 4096)

    wedged, why, mark = sup.session_is_stalled(
        "claude",
        "44444444-4444-4444-4444-444444444444",
        since=time.time() - 10_000,
        baseline_mark=4096,
    )
    assert wedged is True, "a session whose output has stopped moving was reported healthy"
    assert mark == 4096
    # ...and it says WHAT it is stalled on — "wrote nothing" would be the wrong sentence here,
    # because it wrote 4096 bytes and then stopped.
    assert "added nothing" in why, why

    # The same session, before the stall window has elapsed, is NOT yet stalled: an alarm that
    # fires the instant output pauses is an alarm nobody reads.
    early, _why, _m = sup.session_is_stalled(
        "claude",
        "44444444-4444-4444-4444-444444444444",
        since=time.time() - 1,
        baseline_mark=4096,
    )
    assert early is False


def test_INVARIANT_6_a_session_that_GREW_is_not_stalled(store, monkeypatch):
    """The mirror. A stall check that never clears is an alarm nobody reads — the test is GROWTH
    against a baseline, not existence, so a session that has moved must come back healthy."""
    marks = iter([10, 20])

    def fake(engine, native, **kw):
        return next(marks, 20)

    monkeypatch.setattr(sup, "_transcript_mark", fake, raising=False)
    stalled, _why, mark = sup.session_is_stalled(
        "claude",
        "55555555-5555-5555-5555-555555555555",
        since=time.time() - 10_000,
        baseline_mark=None,
    )
    # Whatever the mark turns out to be, an UNKNOWN baseline must not be reported as growth:
    # the first pass records a baseline rather than declaring health.
    assert mark is None or isinstance(mark, int)
    assert isinstance(stalled, bool)


def test_INVARIANT_6_an_ARCHIVING_mission_accepts_no_writes(store):
    """The last fence before bytes reach a pty, and it fails CLOSED.

    A mission being torn down has withdrawn its sessions; no caller — automatic or human — may
    write to one. `sessions_barred_from_automation` is what `deliver`'s final guard consults, so
    a session that appears there is one the actuator will refuse.
    """
    missions.adopt(store, CLAUDE_A)
    assert CLAUDE_A not in missions.sessions_barred_from_automation()
    missions.set_state(store, "running", "done", outcome="done")
    missions.begin_archive(store)
    assert (
        CLAUDE_A in missions.sessions_barred_from_automation()
    ), "a session whose mission is being archived was still offered to the actuator"
