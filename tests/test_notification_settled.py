"""The bell's settled history and its badge (#852, #840 §16).

Since #800 a decided row is RETIRED, not deleted, because the row is doing two jobs: it is the
operator's alert, and it is the "already told you" memo that stops one unresolved situation being
announced every TTL (#760). Everything here turns on keeping those two jobs separable — the
window is a *projection*, and trimming the store instead is #800's bug through the front door.
"""

from __future__ import annotations

import os
import time

import pytest

from agent_sessions import notifications

# Every decision here has a surface: these tests pin other axes of the bell (#1057).
pytestmark = pytest.mark.usefixtures("every_session_held")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    return tmp_path / "n.json"


def _add(action_id="a1", session="claude:s1", escalation=True, activity_at=None):
    return notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id=session,
        engine="claude",
        action_id=action_id,
        escalation=escalation,
        activity_at=activity_at,
    )


def test_retiring_stamps_settled_at_and_the_row_survives(store):
    """The stamp is the row's OWN, because the 24h window must not depend on a ledger record
    compaction may already have removed."""
    _add()
    assert notifications.retire_for_actions(["a1"]) == 1
    rows = notifications._read(store)
    assert len(rows) == 1, "retire must not delete — the dedupe memo lives here"
    assert isinstance(rows[0]["settled_at"], int | float)
    assert rows[0]["retired"] is True


def test_a_settled_row_is_projected_back_as_history_without_controls(store):
    _add()
    notifications.retire_for_actions(["a1"])
    settled = notifications.settled()
    assert [r["action_id"] for r in settled] == ["a1"]
    assert settled[0]["can_approve"] is False and settled[0]["can_reject"] is False
    # The action is not in any ledger here, so it is historical — no outcome asserted.
    assert settled[0]["projection"] == "historical"


def _shown_ids() -> list[str]:
    """The ids a client would have on screen — what it must send back to Clear."""
    return [r["id"] for r in notifications.settled()]


def _clear_what_is_shown() -> int:
    """Clear exactly the current projection, i.e. an operator clicking Clear on a fresh view."""
    return notifications.clear_settled(_shown_ids())


def test_clear_settled_hides_and_does_not_delete(store):
    """Deleting destroys the memo and restarts the re-announce loop for exactly the actions the
    operator already dealt with."""
    _add()
    notifications.retire_for_actions(["a1"])
    assert _clear_what_is_shown() == 1
    assert notifications.settled() == []
    assert len(notifications._read(store)) == 1, "clear must be a hide, not a delete"


def test_clear_settled_cannot_reach_a_live_row(store):
    """It may only touch rows already in the settled projection — otherwise it clears an alert
    nobody decided."""
    live = _add(action_id="live")
    # Ask for the live row BY ID — an empty request would return 0 without proving anything.
    assert notifications.clear_settled([live["id"]]) == 0
    assert len(notifications.listing()["notifications"]) == 1


def test_a_row_outside_the_window_still_suppresses_a_re_announce(store):
    """The bound is on the PROJECTION, not the store. A row that ages out stops being drawn and
    keeps doing its #760 job."""
    _add()
    notifications.retire_for_actions(["a1"])
    rows = notifications._read(store)
    rows[0]["settled_at"] = time.time() - notifications.SETTLED_WINDOW_S - 60
    notifications._write(store, rows)

    assert notifications.settled() == [], "aged out of the window"
    assert len(notifications._read(store)) == 1, "…but still in the store as the memo"


def test_reviving_a_row_clears_settled_at_in_the_same_write(store):
    """A stale `settled_at` on a live alert files an unresolved situation in the operator's
    decision history and starts a retention clock on something nobody settled."""
    # A REAL activity stamp: equivalence is unprovable without one, and unprovable deliberately
    # fails toward announcing, so a None-vs-None comparison would not exercise the revive path.
    rec = _add(action_id="a1", activity_at=1234.5)
    notifications.retire_for_actions(["a1"])
    assert notifications._read(store)[0].get("settled_at") is not None

    # The same situation, re-proposed under a new action id, with unchanged activity.
    notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id="claude:s1",
        engine="claude",
        action_id="a2",
        escalation=True,
        activity_at=rec.get("activity_at"),
    )
    rows = notifications._read(store)
    assert len(rows) == 1, "one situation must produce ONE alert, not two"
    assert rows[0]["action_id"] == "a2"
    assert rows[0]["retired"] is False
    assert "settled_at" not in rows[0]


def test_the_badge_counts_only_what_can_still_be_acted_on(store, tmp_path, monkeypatch):
    """A number the operator cannot clear by acting is a number they learn to ignore."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    _add(action_id="act-pending", session="claude:s1")
    _add(action_id="act-claimed", session="claude:s2")
    ledger.append(
        {"id": "act-pending", "state": "proposed", "verb": "continue", "session_id": "claude:s1"}
    )
    ledger.append(
        {"id": "act-claimed", "state": "claimed", "verb": "continue", "session_id": "claude:s2"}
    )

    out = notifications.listing()
    assert len(out["notifications"]) == 2, "both rows are still shown"
    assert out["unread"] == 1, "…but only the actionable one is counted"


def test_an_unreadable_ledger_loses_nothing_but_claims_nothing(store, monkeypatch):
    """Fails toward SHOWING — which is not the same as counting as actionable.

    This test previously asserted `unread == 2`, on the reasoning that uncertainty must inflate
    the badge rather than deflate it. The first half of that is right and the second is not:
    hiding an escalation the operator never saw is indeed the outcome this module exists to
    prevent, but those rows project `unknown` and offer no control, so counting them as
    ACTIONABLE hands the operator a number they cannot clear by acting.

    Both rows stay visible, and `uncertain` carries them.
    """
    monkeypatch.setattr(notifications, "_action_states", lambda rows: None)
    _add(action_id="a1")
    _add(action_id="a2", session="claude:s2")
    out = notifications.listing()
    assert len(out["notifications"]) == 2, "an outage hid an escalation"
    assert out["uncertain"] == 2
    assert out["unread"] == 0


def test_informational_rows_do_not_inflate_the_decision_badge(store):
    """They are not uncertain — they are KNOWN not to be decisions.

    Nothing about an informational notice can be approved or rejected, so no operator action can
    ever clear it from the count. Including it made the badge un-clearable by acting, which is
    the property that teaches an operator to stop reading it. It stays fully visible; only the
    COUNT is bounded.
    """
    _add(action_id="", session="claude:s1", escalation=False)
    out = notifications.listing()
    assert len(out["notifications"]) == 1, "informational rows are still shown"
    assert out["unread"] == 0, "…but they are not decisions, so they are not counted"


def test_an_escalation_with_no_action_id_is_uncertain_not_actionable(store):
    """The other unestablishable row — same answer as an outage, for the same reason.

    An escalation carrying no `action_id` cannot be looked up, so nothing can be approved or
    rejected on it. It used to count as actionable on the "unknown fails toward counting"
    reasoning; it now projects `unknown`, stays visible, and is counted as uncertain. Excluding
    informational rows still must not quietly drop this one, which is what the original test was
    really guarding — asserted below by its presence and its `uncertain` count.
    """
    _add(action_id="", session="claude:s2", escalation=True)
    out = notifications.listing()
    assert len(out["notifications"]) == 1, "a decision with no id was dropped"
    assert out["notifications"][0]["projection"] == "unknown"
    assert out["uncertain"] == 1
    assert out["unread"] == 0


def test_an_outage_does_not_turn_a_log_entry_into_a_decision(store, monkeypatch):
    """Unknown fails toward counting; informational is not unknown.

    An unreadable ledger says nothing about whether a row is a decision — an informational notice
    is known not to be one whatever the ledger's state. Checking the outage first let log entries
    back into the badge during an outage, re-creating exactly the un-clearable count this rule
    removes, at the moment the operator can do least about it.
    """
    monkeypatch.setattr(notifications, "_action_states", lambda rows: None)
    _add(action_id="", session="claude:info", escalation=False)
    _add(action_id="a1", session="claude:real", escalation=True)
    out = notifications.listing()
    assert len(out["notifications"]) == 2
    # The informational row is in NEITHER count: it is not actionable and it is not uncertain —
    # it is known not to be a decision, whatever the ledger can or cannot say.
    assert out["unread"] == 0
    assert out["uncertain"] == 1, "only the escalation is uncertain, outage or not"


def test_a_cleared_incident_that_comes_back_can_settle_visibly_again(store):
    """`clear_settled` hides; reviving must un-hide, or the flag outlives what it cleared.

    Settle → clear → the same situation returns → settle again: the second settlement was
    filtered out by a decision the operator made about the FIRST one, and nothing ever undid it.
    """
    rec = _add(action_id="a1", activity_at=1234.5)
    notifications.retire_for_actions(["a1"])
    assert _clear_what_is_shown() == 1
    assert notifications.settled() == []

    # The same unresolved situation, re-proposed under a new action id.
    notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id="claude:s1",
        engine="claude",
        action_id="a2",
        escalation=True,
        activity_at=rec.get("activity_at"),
    )
    notifications.retire_for_actions(["a2"])
    assert [r["action_id"] for r in notifications.settled()] == [
        "a2"
    ], "a cleared incident stayed invisible after coming back"


def test_settled_history_orders_by_decision_time_not_repair_time(store):
    """A self-heal runs long after the fact, so stamping repair time orders the operator's
    decision history by when we NOTICED rather than when they decided."""
    _add(action_id="early", session="claude:s1", activity_at=1.0)
    _add(action_id="late", session="claude:s2", activity_at=2.0)

    # Realistic stamps: the window is 24h wide, so epoch-1970 values would simply be filtered
    # out and the test would "pass" on an empty list.
    now = time.time()
    # `late` decided a minute ago; `early` decided two minutes ago but only healed now.
    notifications.retire_for_actions(["late"], decided_at={"late": now - 60})
    notifications.retire_for_actions(["early"], decided_at={"early": now - 120})

    order = [r["action_id"] for r in notifications.settled()]
    assert order == ["late", "early"], f"ordered by repair time, not decision time: {order}"


def test_a_live_bell_row_carries_the_shared_projection(store, tmp_path, monkeypatch):
    """The bell is a producer of decisions like any other surface, and #852 requires all of them
    to consume the one helper rather than each deriving controls from a state field."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    _add(action_id="act-approved", session="claude:s1")
    ledger.append(
        {"id": "act-approved", "state": "approved", "verb": "continue", "session_id": "claude:s1"}
    )

    row = notifications.listing()["notifications"][0]
    for field in ("projection", "can_approve", "can_reject", "state"):
        assert field in row, f"missing {field}"
    # `approved` is reject-only: in flight, and approving again is a no-op.
    assert row["projection"] == "in_flight_revocable"
    assert row["can_approve"] is False and row["can_reject"] is True


def test_clearing_hides_only_what_the_operator_could_see(store):
    """Eleven recent rows, ten visible: clearing must hide ten, not eleven.

    `clear_settled` re-stated the membership predicate and omitted the bounds, so the unseen
    eleventh row was hidden for ever — although it would have become visible as newer entries
    aged out. Hiding what was never shown is history loss, not a dismissal.
    """
    now = time.time()
    for i in range(notifications.SETTLED_MAX + 1):
        _add(action_id=f"a{i}", session=f"claude:s{i}")
        notifications.retire_for_actions([f"a{i}"], decided_at={f"a{i}": now - i})

    assert len(notifications.settled()) == notifications.SETTLED_MAX
    # Send every id, including the one the projection withheld: the window still bounds it.
    every = [r["id"] for r in notifications._read(store)]
    assert notifications.clear_settled(every) == notifications.SETTLED_MAX

    rows = {r["action_id"]: r for r in notifications._read(store)}
    oldest = f"a{notifications.SETTLED_MAX}"
    assert not rows[oldest].get("settled_hidden"), "a row never shown was hidden anyway"
    # …and it surfaces now that the newer ten are gone from the projection.
    assert [r["action_id"] for r in notifications.settled()] == [oldest]


def test_clearing_does_not_touch_a_row_that_has_aged_out(store):
    """Outside the 24h window is outside the projection, so clear must not reach it either."""
    now = time.time()
    _add(action_id="fresh", session="claude:s1")
    _add(action_id="ancient", session="claude:s2")
    notifications.retire_for_actions(["fresh"], decided_at={"fresh": now - 10})
    notifications.retire_for_actions(
        ["ancient"], decided_at={"ancient": now - notifications.SETTLED_WINDOW_S - 60}
    )

    assert [r["action_id"] for r in notifications.settled()] == ["fresh"]
    both = [r["id"] for r in notifications._read(store)]
    assert notifications.clear_settled(both) == 1
    rows = {r["action_id"]: r for r in notifications._read(store)}
    assert rows["fresh"].get("settled_hidden") is True
    assert not rows["ancient"].get("settled_hidden"), "an aged-out row was hidden by a clear"


def test_clearing_cannot_hide_a_decision_that_settled_after_the_view(store):
    """GET → something settles → Clear. The new row was never on screen and must survive.

    The window is recomputed when the POST runs, so a clear that says only "clear the settled
    window" hides whatever slipped in between the two requests — and hidden is permanent, since
    it is exactly the flag that keeps a row out of every later projection. A lock inside the POST
    cannot help: the two moments are different moments. The client therefore sends what it drew.
    """
    now = time.time()
    _add(action_id="seen", session="claude:s1")
    notifications.retire_for_actions(["seen"], decided_at={"seen": now - 10})

    shown = _shown_ids()  # ← the GET the operator is looking at
    assert [r["action_id"] for r in notifications.settled()] == ["seen"]

    # …and now, before the click lands, a second action settles.
    _add(action_id="unseen", session="claude:s2")
    notifications.retire_for_actions(["unseen"], decided_at={"unseen": now})

    assert notifications.clear_settled(shown) == 1, "cleared more than was displayed"
    rows = {r["action_id"]: r for r in notifications._read(store)}
    assert rows["seen"].get("settled_hidden") is True
    assert not rows["unseen"].get("settled_hidden"), "hid a decision the operator never saw"
    # It is still there to be seen, which is the whole point.
    assert [r["action_id"] for r in notifications.settled()] == ["unseen"]


def test_an_unreadable_ledger_does_not_demote_a_live_escalation(store, monkeypatch, tmp_path):
    """A ledger that will not READ is not a ledger that is EMPTY.

    Exercised through the production reader — `orchestrator_ledger` resolving a real path — not
    by mocking `_action_states`, because the defect lived precisely in the reader: `latest_by_id`
    turned an `OSError` into `{}`, which the caller could only read as "every action absent", and
    absent projects as `historical`: no controls, no badge, no outcome asserted.
    """
    from agent_sessions import orchestrator_ledger

    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(led))
    orchestrator_ledger.append({"id": "a1", "state": "escalated", "verb": "continue"})

    _add(action_id="a1", escalation=True)
    ok = notifications.listing()
    assert ok["notifications"][0]["projection"] == "actionable"
    assert ok["unread"] == 1

    # Same store, same rows — only the ledger becomes unreadable.
    led.chmod(0o000)
    try:
        if os.access(led, os.R_OK):  # running as root: the mode is not enforced
            pytest.skip("cannot make a file unreadable as this user")
        out = notifications.listing()
    finally:
        led.chmod(0o644)

    row = out["notifications"][0]
    # NOT `historical` — that is the demotion this guards. `unknown` says the store did not
    # answer, which is the truth: the row stays visible and is reported as uncertain rather than
    # either vanishing or being overstated as actionable.
    assert row["projection"] == "unknown", "an unreadable ledger demoted a live escalation"
    # NO controls — the reject route reads through the same `_read_all_at`, so a Reject offered
    # here is answered 404 "unknown action". Advertising it is the very defect this PR fixed one
    # commit earlier, pointed at my own fix.
    assert row["can_reject"] is False
    assert row["can_approve"] is False
    assert out["uncertain"] == 1, "an unreadable ledger lost the decision"
    assert out["unread"] == 0, "an unreadable row cannot be actionable — it has no control"

    # …and it recovers on its own once the file reads again, with no operator action.
    back = notifications.listing()["notifications"][0]
    assert back["projection"] == "actionable"


def test_a_settled_row_keeps_no_controls_when_the_ledger_cannot_be_read(
    store, monkeypatch, tmp_path
):
    """History is history whether or not the ledger answers.

    `settled()` promises a window "with NO controls", and a settled row proves its own finality:
    `retired` plus the `settled_at` this window filters on are durable fields ON THE ROW. The
    ledger is consulted only to LABEL the outcome. Passing the unreadable flag straight through
    handed a decided row a Reject — a control on history, for something the operator already
    settled, which the backend then answers 404.
    """
    from agent_sessions import orchestrator_ledger

    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(led))
    orchestrator_ledger.append({"id": "a1", "state": "rejected", "verb": "continue"})

    _add(action_id="a1", escalation=True)
    notifications.retire_for_actions(["a1"])
    ok = notifications.settled()
    assert [r["action_id"] for r in ok] == ["a1"]
    assert ok[0]["can_approve"] is False and ok[0]["can_reject"] is False

    led.chmod(0o000)
    try:
        if os.access(led, os.R_OK):
            pytest.skip("cannot make a file unreadable as this user")
        out = notifications.settled()
    finally:
        led.chmod(0o644)

    assert [r["action_id"] for r in out] == ["a1"], "an outage dropped settled history"
    assert out[0]["can_reject"] is False, "an outage handed history a Reject"
    assert out[0]["can_approve"] is False


def test_an_outage_row_is_uncertain_not_actionable(store, monkeypatch, tmp_path):
    """A row with no control may not inflate the ACTIONABLE count — but it must not vanish.

    An earlier revision counted it, reasoning that an outage is transient so the number stays
    true. That defends visibility, which is not what the badge means: it counts what can be
    **acted on**, and this row offers nothing to tap, so the operator cannot clear it by acting —
    rule 5's exact property. It also contradicted this module's own claim that one projection
    decides both questions.

    So the row leaves `unread` and appears in `uncertain`, which states what is actually true:
    something is outstanding and its state cannot currently be read. Both are asserted together,
    with the row still present in the listing, so a later change cannot satisfy one by losing
    another.
    """
    from agent_sessions import orchestrator_ledger

    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(led))
    orchestrator_ledger.append({"id": "a1", "state": "escalated", "verb": "continue"})
    _add(action_id="a1", escalation=True)

    led.chmod(0o000)
    try:
        if os.access(led, os.R_OK):
            pytest.skip("cannot make a file unreadable as this user")
        out = notifications.listing()
    finally:
        led.chmod(0o644)

    row = out["notifications"][0]
    assert row["can_approve"] is False and row["can_reject"] is False, "an outage offered a 404"
    assert row["projection"] == "unknown"
    assert out["unread"] == 0, "a row with no control counted as actionable"
    assert out["uncertain"] == 1, "an outage lost the decision entirely"
    assert len(out["notifications"]) == 1, "the row must stay visible"


def test_settled_strips_controls_whatever_the_projection_says(store, monkeypatch):
    """The `settled()` guard, tested directly rather than through a path another fix masks.

    Today no projection a settled row can reach offers a control, so removing this guard changes
    no observable behaviour — which makes it exactly the kind of "protection" that is really an
    untested assumption. It is kept because the projection is a growing table (#877 adds a
    claimable escalation state) and this window must not inherit a control from a future row, so
    it is asserted against a projection that DOES offer controls rather than against today's.
    """
    from agent_sessions import orchestrator_ledger

    _add(action_id="a1", escalation=True)
    notifications.retire_for_actions(["a1"])

    monkeypatch.setattr(
        orchestrator_ledger,
        "project_for_operator",
        lambda state, known=True: {
            "projection": "actionable",
            "can_approve": True,
            "can_reject": True,
            "state": state,
        },
    )
    out = notifications.settled()
    assert [r["action_id"] for r in out] == ["a1"]
    assert out[0]["can_approve"] is False, "history offered Approve"
    assert out[0]["can_reject"] is False, "history offered Reject"
