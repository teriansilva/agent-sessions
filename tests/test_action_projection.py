"""One action state, one projection of it (#852, #840 §16).

Three sets in this codebase already disagree about what "pending" means —
`OPERATOR_PENDING_STATES` and `REJECTABLE_STATES` are identical, `actuator.CLAIMABLE_STATES` is
neither — so any surface deriving controls from one of them alone gets `approved` wrong (it is
reject-only yet still deliverable) and `claimed` wrong (live, not terminal).

The table below is the contract. It is driven over EVERY ledger state, plus the absent case,
because a projection that is only checked on the states someone remembered is the same accident
one layer up.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import actuator, session_input
from agent_sessions import orchestrator_ledger as ledger


@pytest.fixture(autouse=True)
def _sessions_are_live(monkeypatch):
    """This file is about how a live action is PROJECTED, not whether its session still exists.

    Since #969 every retiring read also withdraws an action whose session has no writer, which is
    what a dead session looks like in production (startup `discover()` streams every live master).
    The fixtures here seed actions for sessions nobody registered, so without this they would be
    withdrawn before the projection under test ever saw them.
    """
    monkeypatch.setattr(session_input, "is_live", lambda key: True)


# state -> (projection, can_approve, can_reject)
TABLE = {
    "proposed": ("actionable", True, True),
    # The two roads into an escalation, and the ONE thing that differs between them (#877):
    # a model QUESTION has nothing to run, so it is reject-only; a low-confidence action kept a
    # real delivering verb, so "yes" is a meaningful answer and Approve is offered.
    "escalated": ("actionable", False, True),
    "escalated_low_confidence": ("actionable", True, True),
    "approved": ("in_flight_revocable", False, True),
    "claimed": ("in_flight_locked", False, False),
    "delivered": ("settled", False, False),
    "failed": ("settled", False, False),
    "rejected": ("settled", False, False),
    "stale": ("settled", False, False),
    "expired": ("settled", False, False),
    "indeterminate": ("settled", False, False),
    "observed": ("settled", False, False),
    None: ("historical", False, False),
}


@pytest.mark.parametrize("state", list(TABLE))
def test_every_ledger_state_projects_exactly_once(state):
    want_proj, want_approve, want_reject = TABLE[state]
    got = ledger.project_for_operator(state)
    assert got == {
        "projection": want_proj,
        "can_approve": want_approve,
        "can_reject": want_reject,
        "state": state,
    }


def test_the_table_covers_every_state_the_ledger_can_hold():
    """A projection checked only on remembered states is the original accident, one layer up."""
    covered = {s for s in TABLE if s is not None}
    assert ledger.ALL_STATES <= covered, ledger.ALL_STATES - covered


def test_approve_is_never_offered_outside_actionable():
    """`can_approve` is the narrowest control and the easiest to widen by accident."""
    for state in TABLE:
        p = ledger.project_for_operator(state)
        if p["can_approve"]:
            assert p["projection"] == "actionable", state


def test_the_projection_agrees_with_the_sets_that_actually_gate_the_server():
    """The projection must not merely be self-consistent — it has to match what the server will
    honour, or it offers a control that is refused on tap."""
    for state in TABLE:
        if state is None:
            continue
        p = ledger.project_for_operator(state)
        # Reject is refused server-side outside REJECTABLE_STATES…
        assert p["can_reject"] == (state in ledger.REJECTABLE_STATES), state
        # …and an action that delivery may still claim must never be shown as settled.
        if state in actuator.CLAIMABLE_STATES:
            assert p["projection"] != "settled", state


def test_an_absent_action_asserts_no_outcome():
    """`historical` is deliberately distinct from `settled`: the ledger no longer holds the row,
    so claiming an outcome for it would invent a fact."""
    p = ledger.project_for_operator(None)
    assert p["projection"] == "historical"
    assert p["state"] is None


def test_both_pulse_producers_emit_the_projection(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """The producer-boundary regression #852 asks for.

    `_attach_pending` has TWO producers — a card that already exists, and a card synthesized for
    an action whose session the cache missed. Projecting in the first branch only left the second
    storing the raw ledger row, so the same action carried controls on one path and none of the
    contract on the other. That is the "one contract, two derivations" drift the projection exists
    to end, reproduced inside the function that introduced it.
    """
    from fastapi.testclient import TestClient

    from agent_sessions import pulse
    from agent_sessions.main import create_app

    ledger.append(
        {"id": "act-1", "state": "proposed", "verb": "continue", "session_id": "claude:known"}
    )
    ledger.append(
        {"id": "act-2", "state": "proposed", "verb": "continue", "session_id": "claude:missing"}
    )
    monkeypatch.setattr(
        pulse, "load_cache", lambda *a, **k: {"cards": [{"id": "claude:known", "state": "idle"}]}
    )

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    cards = {x["id"]: x for x in c.get("/api/pulse").json()["cards"]}

    assert set(cards) == {"claude:known", "claude:missing"}
    for cid, card in cards.items():
        a = card.get("pending_action")
        assert a is not None, cid
        for field in ("projection", "can_approve", "can_reject", "announced"):
            assert field in a, f"{cid} is missing {field}"
        assert a["projection"] == "actionable", cid


# --- the producer-boundary matrix (#852) ------------------------------------------------------
#
# The helper being right is necessary and not sufficient: the contract is that every PRODUCER
# emits it, for every state. Exercising the boundaries with one representative state each is how
# a producer that special-cases `proposed` — or drops the fields on a branch nobody sampled —
# stays green. These drive the full state set through each boundary that survives in this PR.


def _pulse_cards(auth_cfg, state, *, cached):
    from fastapi.testclient import TestClient

    from agent_sessions import pulse
    from agent_sessions.main import create_app

    ledger.append({"id": "act-1", "state": state, "verb": "continue", "session_id": "claude:s1"})
    cards = [{"id": "claude:s1", "state": "idle"}] if cached else []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pulse, "load_cache", lambda *a, **k: {"cards": cards})
        c = TestClient(create_app(auth_cfg), base_url="https://testserver")
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        return {x["id"]: x for x in c.get("/api/pulse").json()["cards"]}


@pytest.mark.parametrize("state", sorted(ledger.LIVE_STATES))
@pytest.mark.parametrize("cached", [True, False], ids=["existing-card", "synthesized-card"])
def test_the_pulse_producers_project_every_live_state(auth_cfg, fake_jsonl, state, cached):  # noqa: ARG001
    """Both branches of `_attach_pending`, every state that can reach them.

    Only LIVE states reach a card — a settled action is history, not an errand — so the matrix
    here is `LIVE_STATES`, driven through both the existing-card and synthesized-card producers.
    """
    cards = _pulse_cards(auth_cfg, state, cached=cached)
    card = cards.get("claude:s1")
    if state not in ledger.OPERATOR_PENDING_STATES:
        # `claimed` is deliberately kept off this surface (#777); it must not appear at all
        # rather than appear without the contract.
        assert card is None or "pending_action" not in card, state
        return
    assert card is not None, state
    a = card["pending_action"]
    want = ledger.project_for_operator(state)
    for field, value in want.items():
        assert a[field] == value, f"{state}/{field}"
    assert "announced" in a, state


@pytest.mark.parametrize("state", sorted(ledger.ALL_STATES))
def test_notification_hydration_projects_every_state(tmp_path, monkeypatch, state):
    """The bell is a producer too, across the whole state set rather than one sample."""
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import notifications

    notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id="claude:s1",
        engine="claude",
        action_id="act-1",
        escalation=True,
        activity_at=1.0,
    )
    ledger.append({"id": "act-1", "state": state, "verb": "continue", "session_id": "claude:s1"})
    out = notifications.listing()
    rows = out["notifications"] + out["settled"]
    assert rows, state
    row = rows[0]
    want = ledger.project_for_operator(state)
    for field, value in want.items():
        assert row[field] == value, f"{state}/{field}"


def test_notification_hydration_projects_an_absent_action(tmp_path, monkeypatch):
    """The absent case has to be driven too: it asserts NO outcome, which is a different answer
    from any state and the one most easily lost by sampling only live rows."""
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import notifications

    notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id="claude:s1",
        engine="claude",
        action_id="gone",
        escalation=True,
        activity_at=1.0,
    )
    row = notifications.listing()["notifications"][0]
    assert row["projection"] == "historical"
    assert row["state"] is None
    assert row["can_approve"] is False and row["can_reject"] is False


# --------------------------------------------------------------------------------------------
# `escalated` is ACTIONABLE but NOT approvable — the projection may never advertise a control the
# delivery path refuses. #840 §16 tables `escalated` as "Approve + Reject" while the same
# paragraph states `CLAIMABLE_STATES` is `{proposed, approved}`; the issue contradicts itself and
# the backend is the authority.
# --------------------------------------------------------------------------------------------


def test_can_approve_never_exceeds_what_delivery_will_claim():
    """The anti-drift pin: every state the projection offers Approve for must be claimable.

    Asserted against `actuator.CLAIMABLE_STATES` itself rather than a restated literal — a copy
    is what let these disagree in the first place. If the delivery state machine ever widens or
    narrows, this test moves with it instead of going quietly stale.
    """
    for state in ledger.ALL_STATES:
        if ledger.project_for_operator(state)["can_approve"]:
            assert state in actuator.CLAIMABLE_STATES, (
                f"projection offers Approve for {state!r}, which actuator.deliver refuses with "
                "409 NotDeliverable"
            )


def test_the_two_sets_are_one_object_not_two_equal_ones():
    """Same object, so they cannot drift apart by an edit to one side."""
    assert actuator.CLAIMABLE_STATES is ledger.CLAIMABLE_STATES


@pytest.mark.parametrize(
    ("verb", "want_state", "want_approve", "why"),
    [
        ("escalate", "escalated", False, "the model asking a question — no verb to deliver"),
        (
            "continue",
            "escalated_low_confidence",
            True,
            "the model wanting to ACT and not being sure — a real verb, so yes is an answer",
        ),
    ],
)
def test_the_two_roads_into_an_escalation_DIVERGE(verb, want_state, want_approve, why):
    """The contract #877 changed, driven through the PRODUCER rather than asserted about a
    constant.

    The version of this test that stood here was parametrized on `verb` and then never used it:
    it called `project_for_operator("escalated")` twice and asserted both roads converge on
    reject-only. That was the pre-#877 contract, and because the test never called `_decide` it
    stayed GREEN while documenting the opposite of what the code now does — a test that passes
    against code it contradicts, which is worse than no test because it reads as coverage.

    So the verb is now actually used: `_decide` maps it to a state, and the projection of THAT
    state is what is asserted.
    """
    from agent_sessions import orchestrator, prefs

    cfg = dict(prefs.get_orchestrator())
    cfg.update(enabled=True, autonomy="yolo", allowed_verbs=["continue"], confidence_min=0.75)
    state, _reason = orchestrator._decide({"verb": verb, "confidence": 0.1}, cfg)
    assert state == want_state, f"{verb}: {why}"

    out = ledger.project_for_operator(state)
    assert out["projection"] == "actionable", f"{verb}: still wants the operator ({why})"
    assert out["can_approve"] is want_approve, f"{verb}: {why}"
    assert out["can_reject"] is True


def test_an_escalated_delivering_verb_is_refused_by_the_real_delivery_path(tmp_path, monkeypatch):
    """The backend half of the pin, through the production path rather than a restated constant.

    This is the case the projection used to advertise: `escalated` carrying `continue`, which
    looks deliverable and is not.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "l.jsonl"))
    ledger.append(
        {
            "id": "a1",
            "state": "escalated",
            "verb": "continue",
            "session_id": "claude:s1",
            "confidence": 0.4,
        }
    )
    with pytest.raises(actuator.NotDeliverable) as e:
        asyncio.run(actuator.deliver("a1", registry=None))
    assert "escalated" in str(e.value)


# --------------------------------------------------------------------------------------------
# `unknown` — the store did not answer. #840 §16 has no row for it, because it assumes the ledger
# always answers. Collapsing it into `historical` is what disarmed a live escalation on a
# transient read error.
# --------------------------------------------------------------------------------------------


def test_an_unreadable_store_is_not_an_absent_action():
    unknown = ledger.project_for_operator(None, known=False)
    absent = ledger.project_for_operator(None)
    assert unknown["projection"] == "unknown"
    assert absent["projection"] == "historical"
    assert unknown != absent, "an unreadable store and a compacted action are different answers"


def test_unknown_offers_no_control_because_no_mutation_can_be_arbitrated():
    """NO controls, including Reject.

    An earlier revision offered Reject on the reasoning that the row still wants the operator.
    It does — but `compare_and_set` and `get` read through the same `_read_all_at`, which turns
    the same `OSError` into an empty ledger, so the route answers **404 "unknown action"**. The
    rule this module established one commit earlier applies to its own fix: the projection may
    not advertise a control the backend refuses.
    """
    p = ledger.project_for_operator(None, known=False)
    assert p["can_approve"] is False
    assert p["can_reject"] is False
    assert p["state"] is None


def test_no_projection_anywhere_offers_a_control_the_reject_route_cannot_honour():
    """The generalisation, so the next projection added cannot repeat this.

    `can_reject` is only ever true for a state the reject CAS will actually accept; and the
    unreadable case offers nothing, because no state can be established for the CAS to match.
    """
    for state in ledger.ALL_STATES:
        if ledger.project_for_operator(state)["can_reject"]:
            assert state in ledger.REJECTABLE_STATES, state
    assert ledger.project_for_operator(None, known=False)["can_reject"] is False


@pytest.mark.parametrize("state", sorted(ledger.ALL_STATES))
def test_unknown_wins_over_any_stale_state_handed_alongside_it(state):
    """`known=False` is not advisory: whatever state a caller happens to pass, an unreadable
    store cannot have produced it, so the projection must not be computed from it."""
    assert ledger.project_for_operator(state, known=False)["projection"] == "unknown"


# ==============================================================================================
# THE TWO ROADS INTO AN ESCALATION (#877)
#
# `escalated` used to be one state carrying two meanings, and that conflation WAS the bug: the
# operator was shown a real, runnable `continue`, asked to look at it, and given no way to say
# yes — the console's Approve button 409'd because `escalated` is not claimable.
#
# The fix is one state per meaning, so the projection stays a pure state → controls table. The
# risk it introduces is different and is what these tests are for: a new state has to be in
# EVERY set that decides something, and each one it misses fails silently, in a different
# surface, in a different way.
# ==============================================================================================

LOW_CONF = "escalated_low_confidence"


@pytest.mark.parametrize(
    "set_name",
    [
        # Miss this and the row is not live — compaction may drop it while it waits.
        "LIVE_STATES",
        # Miss this and it never carries decision controls on a card at all.
        "OPERATOR_PENDING_STATES",
        # Miss this and the operator can see it but cannot decline it.
        "REJECTABLE_STATES",
        # Miss this and it NEVER EXPIRES — it stays live indefinitely, which is worse than the
        # gap being fixed. This was the set missing from the first draft of the issue.
        "EXPIRABLE_STATES",
        # …and this is the one the whole issue is about.
        "CLAIMABLE_STATES",
    ],
)
def test_the_new_state_is_in_every_set_that_decides_something(set_name):
    assert LOW_CONF in getattr(ledger, set_name), set_name


def test_the_model_question_stays_out_of_claimable():
    """The other direction, and the safety half. A `verb == "escalate"` action has nothing to
    run, so widening delivery must not admit it."""
    assert "escalated" not in ledger.CLAIMABLE_STATES
    assert "escalated" in ledger.REJECTABLE_STATES


def test_escalation_states_is_the_named_meaning_rather_than_a_seventh_comparison():
    """Seven exact `== "escalated"` comparisons decided things before this — announcement,
    counting, tone, ARIA, whether the reason shows. The set exists so the eighth reader asks a
    name instead of repeating a string."""
    assert ledger.ESCALATION_STATES == {"escalated", LOW_CONF}
    assert ledger.ESCALATION_STATES <= ledger.LIVE_STATES
    assert ledger.ESCALATION_STATES <= ledger.OPERATOR_PENDING_STATES
    assert ledger.ESCALATION_STATES <= ledger.REJECTABLE_STATES
    assert ledger.ESCALATION_STATES <= ledger.EXPIRABLE_STATES


def test_the_low_confidence_row_is_approvable_and_the_question_is_not():
    """The projection, which is the single authority every surface reads."""
    low = ledger.project_for_operator(LOW_CONF)
    q = ledger.project_for_operator("escalated")
    assert low["projection"] == q["projection"] == ledger.ACTIONABLE
    assert (low["can_approve"], low["can_reject"]) == (True, True)
    assert (q["can_approve"], q["can_reject"]) == (False, True)


def test_can_approve_still_equals_membership_of_the_delivery_set():
    """The identity that makes "the console never advertises a control the backend refuses" a
    structural property rather than a convention. If this ever drifts, an Approve button appears
    for something `actuator.deliver` answers with a 409 — the exact defect #862 removed."""
    from agent_sessions import actuator

    assert actuator.CLAIMABLE_STATES is ledger.CLAIMABLE_STATES
    for state in ledger.ALL_STATES:
        p = ledger.project_for_operator(state)
        if p["projection"] == ledger.ACTIONABLE:
            assert p["can_approve"] == (state in ledger.CLAIMABLE_STATES), state
        else:
            assert p["can_approve"] is False, state


def test_no_source_file_decides_anything_from_a_BARE_escalated_comparison():
    """The recurrence guard, and it exists because the count kept being wrong.

    `escalated` was compared by hand in eight places across the server and the client — the
    issue's inventory found seven, and the eighth shipped a low-confidence action labelled as
    already "in flight". Each miss fails SILENTLY and differently: never announced, announced
    but never counted, the wrong tone, the wrong ARIA label, the wrong sentence.

    So the literal is banned outside the places that legitimately own it: the set that defines
    the vocabulary, the projection that reads it, and tests. Anything else asking "is this an
    escalation?" asks `ESCALATION_STATES` (or the client's `isEscalation`), which cannot fall
    behind a new member.

    A source scan is an early warning, not a boundary — the same standing as the AST checker in
    `test_prompts_registry.py`, and for the same reason: a static check of string literals can
    always be spelled around. What it CAN do is catch the accident, which is what every one of
    the eight was.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    # Where the literal is the DEFINITION rather than a decision derived from it.
    allowed = {
        root / "src" / "agent_sessions" / "orchestrator_ledger.py",  # the sets themselves
        root / "web" / "src" / "lib" / "orchestratorAction.ts",  # the client mirror
        root / "web" / "src" / "types" / "api.ts",  # the state union
    }
    pattern = re.compile(r'[=!]==?\s*"escalated"|case\s+"escalated"|"escalated"\s*[=!]=')

    offenders = []
    for sub, globs in (
        (root / "src", ("**/*.py",)),
        (root / "web" / "src", ("**/*.ts", "**/*.tsx")),
    ):
        for g in globs:
            for f in sub.glob(g):
                if f in allowed or ".test." in f.name:
                    continue
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    stripped = line.lstrip()
                    if stripped.startswith(("#", "*", "//", '"""', "/**")):
                        continue  # prose about the rule is not the rule
                    if pattern.search(line):
                        offenders.append(f"{f.relative_to(root)}:{i}: {stripped[:90]}")
    assert not offenders, (
        "these decide something from a bare `escalated` comparison; use ESCALATION_STATES / "
        "isEscalation / the projection instead:\n  " + "\n  ".join(offenders)
    )


# --- the ownership stamp (#878) ---------------------------------------------------------------
#
# Which mission holds a session is a SERVER fact. The console's first version derived it by
# scanning the mission rows it happened to have in memory — and that rail is paged, so a session
# held by mission 101 read as held by nobody and was offered an ADOPT the server then refused
# with 409. Ownership that depends on how far the operator has scrolled is not ownership.


def _cards_with_missions(auth_cfg, *, cached_ids, membership):
    from fastapi.testclient import TestClient

    from agent_sessions import pulse
    from agent_sessions.main import create_app
    from agent_sessions.routes import pulse as pulse_routes

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            pulse,
            "load_cache",
            lambda *a, **k: {"cards": [{"id": i, "state": "idle"} for i in cached_ids]},
        )
        if isinstance(membership, Exception):

            def boom(*a, **k):
                raise membership

            mp.setattr(pulse_routes.missions, "all_active_memberships", boom)
        else:
            mp.setattr(
                pulse_routes.missions, "all_active_memberships", lambda *a, **k: dict(membership)
            )
        c = TestClient(create_app(auth_cfg), base_url="https://testserver")
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        return {x["id"]: x for x in c.get("/api/pulse").json()["cards"]}


def test_a_card_carries_the_mission_that_holds_it(auth_cfg, fake_jsonl):  # noqa: ARG001
    """The stamp covers EVERY mission, not the page the rail happens to show.

    `msn_page3` stands in for a mission far past the first page: the server has never been told
    what the client has loaded, so it cannot answer differently for one — which is exactly the
    property that makes the client's paged derivation unnecessary.
    """
    cards = _cards_with_missions(
        auth_cfg,
        cached_ids=["claude:held", "claude:free"],
        membership={"claude:held": "msn_page3"},
    )
    assert cards["claude:held"]["mission_id"] == "msn_page3"
    # `None`, not absent: "no mission holds this" is a real answer and the client adopts on it.
    assert cards["claude:free"]["mission_id"] is None


def test_a_synthesized_card_is_stamped_too(auth_cfg, fake_jsonl):  # noqa: ARG001
    """The second producer. A card conjured for an action the pulse cache never saw is still a
    session some mission may hold — and it is the one most likely to be, since it is carrying a
    live decision."""
    ledger.append(
        {"id": "act-1", "state": "proposed", "verb": "continue", "session_id": "claude:ghost"}
    )
    cards = _cards_with_missions(auth_cfg, cached_ids=[], membership={"claude:ghost": "msn_x"})
    assert cards["claude:ghost"]["mission_id"] == "msn_x"


def test_an_unreadable_membership_store_omits_the_field_rather_than_lying(auth_cfg, fake_jsonl):  # noqa: ARG001
    """ABSENT, never `None`.

    `None` means "no mission holds this", which the client acts on by offering ADOPT. If an
    unreadable store answered `None` the console would offer that adoption for every session on
    the page — mutations the server refuses — which is the precise defect the stamp removes.
    There is no sentinel string available either: any value could be a real mission id.
    """
    cards = _cards_with_missions(
        auth_cfg, cached_ids=["claude:s1"], membership=RuntimeError("db is gone")
    )
    # The card still SHOWS — hiding the operator's work is the worse failure — it just makes no
    # ownership claim.
    assert "claude:s1" in cards
    assert "mission_id" not in cards["claude:s1"]


def test_a_stale_cached_stamp_never_survives_a_store_that_will_not_answer(auth_cfg, fake_jsonl):  # noqa: ARG001
    """The unconditional strip, tested where it is load-bearing.

    When the store answers, the assignment overwrites whatever the cache held, so a test with a
    readable store proves nothing about the strip — it would pass with the strip deleted. The
    case that needs it is the one where nothing overwrites: the pulse cache outlives the
    memberships it saw, so a `mission_id` an earlier scan wrote into it would be served as
    current ownership precisely when the server cannot establish ownership at all. Absent, not
    stale.
    """
    from fastapi.testclient import TestClient

    from agent_sessions import pulse
    from agent_sessions.main import create_app
    from agent_sessions.routes import pulse as pulse_routes

    def boom(*a, **k):
        raise RuntimeError("db is gone")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            pulse,
            "load_cache",
            lambda *a, **k: {
                "cards": [{"id": "claude:s1", "state": "idle", "mission_id": "msn_stale"}]
            },
        )
        mp.setattr(pulse_routes.missions, "all_active_memberships", boom)
        c = TestClient(create_app(auth_cfg), base_url="https://testserver")
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        card = {x["id"]: x for x in c.get("/api/pulse").json()["cards"]}["claude:s1"]
    assert "mission_id" not in card
