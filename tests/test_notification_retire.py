"""The bell holds only what still needs resolving (#800).

#757 cleared a notification when the operator pressed Approve or Reject. But those two routes
are not where most actions settle — expiry, actuator outcomes and startup recovery all end an
action without anyone pressing anything — so an escalation that ended any other way sat in the
bell forever pointing at something nobody could act on. Measured on the live store when this was
written: 23 of 33 rows pointed at an already-expired action.

What these pin, in the order the failures actually matter:

* **Retire, never delete.** The row doubles as the "already told you" memo behind #760. Deleting
  on settlement would re-announce every unresolved situation once per TTL — the exact regression
  #760 fixed.
* **One boundary.** Every terminal state, reached through its real settlement path.
* **Self-healing.** A notifications-store failure, or a row predating this, is reconciled on the
  next read; an unreadable or unknown ledger record fails toward SHOWING.
* **`notify: all` history survives.** Those rows are a log, not a queue.
"""

import json
from unittest import mock

import pytest

from agent_sessions import notifications
from agent_sessions import orchestrator_ledger as ledger

IDLE = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(tmp_path / "s.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))


def _alert(action_id="a1", session="claude:aaa", escalation=True, title="needs you"):
    return notifications.add(
        title=title,
        project="p",
        reason="",
        session_id=session,
        engine="claude",
        action_id=action_id,
        escalation=escalation,
        activity_at=IDLE,
    )


def _action(action_id="a1", state="escalated", **extra):
    return ledger.append({"id": action_id, "state": state, "session_id": "claude:aaa", **extra})


def _bell():
    return notifications.listing()


def _stored():
    return notifications._read(notifications._notifications_path())


# --- the view ---------------------------------------------------------------------------


def test_a_retired_row_leaves_the_list_and_the_badge_together():
    """The badge is computed from the same filtered set as the list — a count that disagrees
    with the rows under it is worse than no count."""
    _alert(action_id="a1", title="gone")
    _alert(action_id="a2", session="codex:bbb", title="stays")
    _action("a1", "escalated")
    _action("a2", "escalated")

    assert _bell()["unread"] == 2

    notifications.retire_for_actions(["a1"])
    view = _bell()

    assert [r["title"] for r in view["notifications"]] == ["stays"]
    assert view["unread"] == 1


def test_retiring_keeps_the_row_in_the_store():
    """The whole safety of this change: the row leaves the VIEW and stays as the #760 memo."""
    _alert(action_id="a1")
    notifications.retire_for_actions(["a1"])

    assert _bell()["notifications"] == []
    assert len(_stored()) == 1, "the row must survive as the dedupe memo"
    assert _stored()[0]["retired"] is True


def test_a_live_action_is_never_retired_by_reconciliation():
    for state in sorted(ledger.LIVE_STATES):
        notifications.dismiss()
        ledger._path().unlink(missing_ok=True)
        _alert(action_id="live")
        _action("live", state)
        assert len(_bell()["notifications"]) == 1, f"{state} is live and must stay visible"


# --- one settlement boundary ------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(ledger.TERMINAL_STATES - {"observed"}))
def test_every_terminal_transition_retires_the_alert(state):
    """Hooked at the two ledger mutation entry points, so every settlement path is covered —
    including the ones that do not exist yet."""
    _alert(action_id="act")
    _action("act", "escalated")

    assert ledger.transition("act", state) is not None

    assert _bell()["notifications"] == [], f"{state} left the alert in the bell"
    assert _bell()["unread"] == 0


def test_the_expiry_sweep_retires_through_its_real_path():
    """`expire_due` is the single biggest source of stranded rows: nobody presses anything."""
    _alert(action_id="act")
    _action("act", "escalated", expires_at=1.0)

    assert ledger.expire_due() == ["act"]
    assert _bell()["notifications"] == []


def test_startup_recovery_retires_through_its_real_path():
    _alert(action_id="act")
    _action("act", "claimed")

    assert ledger.recover_claimed() == ["act"]
    assert _bell()["notifications"] == []


def test_a_notifications_failure_never_undoes_a_settled_transition():
    """The ledger write is the record of truth and has already succeeded. Best-effort means the
    transition stands — and `listing` reconciles the row on the next read."""
    _alert(action_id="act")
    _action("act", "escalated")

    with mock.patch.object(notifications, "retire_for_actions", side_effect=OSError("disk full")):
        assert ledger.transition("act", "expired") is not None

    assert ledger.get("act")["state"] == "expired", "the ledger write must stand"
    assert _bell()["notifications"] == [], "the next read must heal the stranded row"


# --- reconcile on read, and its failure direction ---------------------------------------


def test_a_row_settled_before_this_existed_is_healed_on_read():
    """No migration: the rows already stranded on the live store drain on the next GET."""
    _alert(action_id="act")
    _action("act", "escalated")
    # Settled without the hook, exactly as the rows already on the live store were.
    ledger.append({"id": "act", "state": "expired"})

    assert _bell()["notifications"] == []


@pytest.mark.parametrize(
    "action_id, why",
    [("", "a row with no action id"), ("never-existed", "an id the ledger never saw")],
)
def test_an_unprovable_row_stays_visible(action_id, why):
    """Hiding an escalation the operator never saw is the one outcome worth failing loudly to
    avoid, so anything unprovable fails toward SHOWING."""
    _alert(action_id=action_id)

    assert len(_bell()["notifications"]) == 1, f"{why} must not be retired"


def test_an_unreadable_ledger_never_empties_the_bell():
    _alert(action_id="act")
    with mock.patch.object(ledger, "latest_by_id", side_effect=OSError("corrupt")):
        assert len(_bell()["notifications"]) == 1


# --- what must NOT be retired -----------------------------------------------------------


def test_an_autonomous_notice_survives_its_action_settling():
    """`notify: all` rows record what was done for the operator. Nothing waits on them, and
    erasing them would delete the only account of an autonomous action."""
    _alert(action_id="act", escalation=False)
    _action("act", "approved")

    assert ledger.transition("act", "delivered") is not None
    assert len(_bell()["notifications"]) == 1
    assert _stored()[0].get("retired") is not True


# --- the #760 guard ---------------------------------------------------------------------


def test_an_unchanged_situation_is_not_re_announced_after_its_row_retires():
    """The regression this design exists to avoid. A → expired → the same situation re-escalates:
    the memo must still suppress it, even though the row is no longer in the bell."""
    first = _alert(action_id="act-A")
    assert first is not None
    _action("act-A", "escalated")
    ledger.transition("act-A", "expired")
    assert _bell()["notifications"] == []

    assert _alert(action_id="act-B") is None, "the same unchanged situation announced twice"
    assert len(_stored()) == 1


def test_a_re_linked_row_comes_back_into_the_bell():
    """Suppressed-and-re-linked means the situation is live again, so the alert is live again —
    otherwise it is suppressed by a memo the operator can no longer see."""
    _alert(action_id="act-A")
    _action("act-A", "escalated")
    ledger.transition("act-A", "expired")
    assert _bell()["notifications"] == []

    assert _alert(action_id="act-B") is None  # suppressed, and re-linked to act-B
    row = _stored()[0]
    assert row["action_id"] == "act-B"
    assert row.get("retired") is not True
    assert len(_bell()["notifications"]) == 1


# --- the ring ---------------------------------------------------------------------------


def test_eviction_drops_retired_rows_before_live_ones(monkeypatch):
    """A settled row the operator cannot act on must never push out one that is still waiting."""
    monkeypatch.setattr(notifications, "NOTIFY_MAX", 3)
    _alert(action_id="old-1", session="claude:s1")
    _alert(action_id="old-2", session="claude:s2")
    notifications.retire_for_actions(["old-1", "old-2"])
    _alert(action_id="live-1", session="claude:s3")
    _alert(action_id="live-2", session="claude:s4")
    _alert(action_id="live-3", session="claude:s5")

    kept = {r["action_id"] for r in _stored()}
    assert kept == {"live-1", "live-2", "live-3"}


def test_the_store_stays_valid_json_after_a_retire(tmp_path):
    _alert(action_id="act")
    notifications.retire_for_actions(["act"])
    assert isinstance(json.loads((tmp_path / "n.json").read_text()), list)
