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

from agent_sessions import actuator
from agent_sessions import orchestrator_ledger as ledger

# state -> (projection, can_approve, can_reject)
TABLE = {
    "proposed": ("actionable", True, True),
    "escalated": ("actionable", False, True),
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
    ("verb", "why"),
    [
        ("escalate", "the model asking a question — there is no verb to deliver"),
        ("continue", "a yolo action below confidence_min, keeping its delivering verb"),
    ],
)
def test_escalated_is_reject_only_whatever_put_it_there(verb, why):
    """Both roads into `escalated` land on the same controls.

    They are genuinely different situations — `orchestrator._decide_state` reaches `escalated`
    from `verb == "escalate"` and, separately, from `confidence < confidence_min` in yolo — and
    the second keeps a real delivering verb, which is why the client rendered Approve for it.
    Neither is claimable, so neither may offer Approve.
    """
    out = ledger.project_for_operator("escalated")
    assert out["projection"] == "actionable", f"{verb}: still wants the operator ({why})"
    assert out["can_approve"] is False, f"{verb}: Approve is a 409 ({why})"
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
