"""Bell dedupe + clearing (#752).

The bug these pin: the orchestrator's only dedupe is "at most one LIVE action per session", so
an escalation nobody acts on expires, the session reads as free, and the next pass re-escalates
the identical situation — a new alert and a new push, forever. Measured on the live store: 200
rows carrying 54 distinct titles.
"""

import json

import pytest

from agent_sessions import notifications, orchestrator, prefs


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(tmp_path / "s.json"))


IDLE = 1_700_000_000.0  # a session waiting on the operator emits nothing; its clock stands still


def _add(
    title="needs you",
    session="claude:aaa",
    action_id="a1",
    escalation=True,
    activity_at=IDLE,
    reason="",
):
    return notifications.add(
        title=title,
        project="p",
        reason=reason,
        session_id=session,
        engine="claude",
        action_id=action_id,
        escalation=escalation,
        activity_at=activity_at,
    )


def _rows():
    return notifications.listing()["notifications"]


def test_an_unresolved_escalation_is_announced_once_not_once_per_ttl():
    assert _add(action_id="a1") is not None
    # Same session, same situation, a fresh ledger record after the previous one expired.
    assert _add(action_id="a2") is None
    assert len(_rows()) == 1


def test_a_changed_situation_still_gets_through():
    _add(title="waiting on a menu choice")
    assert _add(title="the build failed") is not None
    assert len(_rows()) == 2


def test_the_same_title_in_a_different_session_is_a_different_alert():
    _add(session="claude:aaa")
    assert _add(session="codex:bbb") is not None
    assert len(_rows()) == 2


def test_clearing_lets_a_later_re_escalation_speak_again():
    """Clearing is 'forget this', not 'mute forever' — the operator's own action decides."""
    _add()
    notifications.dismiss()
    assert _rows() == []
    assert _add() is not None


def test_dismiss_removes_only_the_named_rows():
    a = _add(title="one")
    _add(title="two")
    c = _add(title="three")
    assert notifications.dismiss([a["id"], c["id"]]) == 2
    assert [r["title"] for r in _rows()] == ["two"]


def test_dismiss_all_empties_the_ring_and_reports_the_count():
    for i in range(4):
        _add(title=f"t{i}")
    assert notifications.dismiss() == 4
    assert notifications.listing() == {"notifications": [], "unread": 0}


def test_dismiss_for_action_retires_that_alert_and_leaves_the_others():
    _add(title="one", action_id="act-1")
    _add(title="two", action_id="act-2")
    assert notifications.dismiss_for_action("act-1") == 1
    assert [r["title"] for r in _rows()] == ["two"]
    # An id nobody raised must not quietly wipe anything.
    assert notifications.dismiss_for_action("act-nope") == 0
    assert notifications.dismiss_for_action("") == 0
    assert len(_rows()) == 1


def test_a_suppressed_alert_does_not_re_send_the_push(monkeypatch, tmp_path):
    """The push is the half that can wake someone at 3am — it must be suppressed too.

    Guards the actual mistake available here: `add` returning the pre-existing row so callers
    "stay uniform" would leave `fanout(note)` firing on every pass while the bell looked calm.
    """
    sent: list[dict] = []
    monkeypatch.setattr(notifications, "fanout", lambda note: sent.append(note))
    monkeypatch.setattr(prefs, "get_orchestrator", lambda: {"notify": "escalations"})
    monkeypatch.setattr(
        orchestrator.ledger,
        "append_batch_for_free_sessions",
        lambda records, *a, **k: (records, []),
    )
    monkeypatch.setattr(orchestrator.ledger, "compact_if_needed", lambda *a, **k: None)

    rec = {
        "id": "act-1",
        "state": "escalated",
        "title": "Awaiting a decision",
        "project": "p",
        "rationale": "why",
        "session_id": "claude:aaa",
        "engine": "claude",
        # The production shape carries the session's clock; an escalated session is waiting on
        # the operator, so it stands still between passes.
        "last_activity": IDLE,
    }
    orchestrator._persist([rec])
    orchestrator._persist([{**rec, "id": "act-2"}])  # same situation, next pass

    assert len(sent) == 1, "the second pass re-sent a push for an alert already in the bell"
    assert len(_rows()) == 1


def test_the_store_stays_valid_json_after_a_dismiss(tmp_path):
    """A half-written store would take the whole bell down on the next read."""
    _add(title="one")
    b = _add(title="two")
    notifications.dismiss([b["id"]])
    raw = json.loads((tmp_path / "n.json").read_text())
    assert [r["title"] for r in raw] == ["one"]


def test_autonomous_notices_are_never_collapsed():
    """`notify=all` exists so the operator sees what was done for them.

    Deduping those the way escalations are deduped would show one `continue` and hide every
    later one in the same session — the opposite of what that mode is for.
    """
    a = _add(action_id="a1", escalation=False)
    b = _add(action_id="a2", escalation=False)
    assert a is not None and b is not None
    assert len(_rows()) == 2


def test_a_suppressed_re_proposal_relinks_the_row_to_the_current_action():
    """A → expired → B suppressed → rejecting B must still find the row.

    Without the re-link the stored `action_id` stays pointed at A, so deciding B in Pulse
    dismisses nothing and the alert is stranded in the bell forever.
    """
    first = _add(action_id="act-A")
    assert _add(action_id="act-B") is None
    row = _rows()[0]
    assert row["id"] == first["id"], "the row itself must not be replaced"
    assert row["action_id"] == "act-B"
    assert notifications.dismiss_for_action("act-B") == 1
    assert _rows() == []


def test_re_linking_does_not_make_a_read_row_look_new():
    """Only the pointer moves — a re-proposal must not resurface as unread or jump the sort."""
    _add(action_id="act-A")
    notifications.mark_read()
    before = _rows()[0]
    _add(action_id="act-B")
    after = _rows()[0]
    assert after["read"] is True
    assert after["ts"] == before["ts"]
    assert notifications.listing()["unread"] == 0


def test_two_equivalent_concurrent_adds_yield_exactly_one_row():
    """The check and the append share one lock hold; a racing pass must lose cleanly."""
    import threading

    out: list = []
    barrier = threading.Barrier(6)

    def go(i):
        barrier.wait()
        out.append(_add(action_id=f"act-{i}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in out if r is not None) == 1, "more than one pass announced"
    assert len(_rows()) == 1


# --- provenance: an escalation is never swallowed by something that was not one --------------


def test_an_autonomous_notice_never_suppresses_a_later_escalation():
    """Reproduced by the review: under `notify=all` an autonomous row with the same session
    title swallowed a later escalation — returning `None`, sending no push, and leaving the
    operator with the autonomous row's reason. An escalation silently lost is the one failure
    the bell exists to prevent."""
    auto = _add(action_id="auto-1", escalation=False)
    notifications.mark_read()
    esc = _add(action_id="esc-1", escalation=True)

    assert esc is not None, "an escalation was suppressed by an autonomous notice"
    assert len(_rows()) == 2
    assert notifications.listing()["unread"] == 1
    assert auto["id"] != esc["id"]


def test_an_escalation_never_suppresses_a_later_autonomous_notice():
    """The mirror: `notify=all` exists to show what was DONE, and an earlier escalation must
    not hide it."""
    _add(action_id="esc-1", escalation=True)
    assert _add(action_id="auto-1", escalation=False) is not None
    assert len(_rows()) == 2


def test_a_legacy_row_without_provenance_fails_toward_announcing():
    """Rows written before this field cannot prove they were escalations. Suppressing on an
    unprovable match would lose an escalation; announcing twice merely repeats one."""
    p = notifications._notifications_path()
    notifications._write(
        p,
        [
            {
                "id": "legacy",
                "ts": 1,
                "read": False,
                "title": "needs you",
                "project": "p",
                "reason": "old",
                "session_id": "claude:aaa",
                "engine": "claude",
                "action_id": "old-1",
            }
        ],
    )
    assert _add(action_id="new-1", escalation=True) is not None
    assert len(_rows()) == 2


def test_escalation_provenance_is_recorded_on_the_row():
    assert _add(action_id="a1", escalation=True)["escalation"] is True
    assert _add(title="other", action_id="a2", escalation=False)["escalation"] is False


# --- the discriminator: has the session done anything since we told you? ---------------------


def test_a_different_escalation_in_the_same_session_is_announced():
    """The review's reproducer, and the case `(session_id, title)` alone could not see.

    Same session, same session TITLE, different situation. For the second thing to happen the
    session had to produce output, which moves its clock — so the two are distinguishable
    without any model text.
    """
    first = _add(action_id="a1", reason="Choose an auth method", activity_at=IDLE)
    second = _add(action_id="a2", reason="Production deploy failed", activity_at=IDLE + 900)

    assert second is not None, "a new escalation was hidden behind the old one's text"
    assert len(_rows()) == 2
    assert {r["reason"] for r in _rows()} == {"Choose an auth method", "Production deploy failed"}
    assert first["id"] != second["id"]
    assert notifications.listing()["unread"] == 2


def test_the_same_situation_re_proposed_is_still_suppressed():
    """The flood itself: an escalation nobody acted on, re-proposed every TTL. The session is
    waiting on the operator, so it emitted nothing and its clock has not moved."""
    _add(action_id="a1", reason="Choose an auth method", activity_at=IDLE)
    assert (
        _add(action_id="a2", reason="Choose an auth method (rephrased)", activity_at=IDLE) is None
    )
    assert len(_rows()) == 1
    assert _rows()[0]["action_id"] == "a2"  # relinked


def test_an_unprovable_stamp_fails_toward_announcing():
    """Either side missing means we cannot show the situation is unchanged. Suppressing on a
    guess loses an escalation; announcing twice merely repeats one."""
    _add(action_id="a1", activity_at=None)
    assert _add(action_id="a2", activity_at=IDLE) is not None
    assert len(_rows()) == 2

    notifications.dismiss()
    _add(action_id="b1", activity_at=IDLE)
    assert _add(action_id="b2", activity_at=None) is not None
    assert len(_rows()) == 2
