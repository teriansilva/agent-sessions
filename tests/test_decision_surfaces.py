"""A decision nobody can act on is not counted, and not pushed (#1057, operator's call on #1054).

Since #1049 removed the session pane's decision strip, the mission console is the only surface
that renders Approve / Reject, and it renders a decision exactly when a mission holds the
decision's session. So:

* **Held** — counted in the unread badge and pushed, exactly as before.
* **Mission-less** — still LISTED in the bell (visibility is never traded away; the Open link
  still leads to the session), but neither counted as actionable nor as `uncertain`, and not
  pushed: nothing the operator could do would clear it.
* **Membership unreadable** — an unestablishable answer, resolved the way #852 rule 5 resolves
  the others: `uncertain` in the bell, and the push fails toward announcing.

The real mission store is used throughout (the unreadable case breaks the STORE — its path is a
directory — rather than patching the reader), and this file deliberately does NOT use the
`every_session_held` fixture the older bell tests use.
"""

from __future__ import annotations

import pytest

from agent_sessions import missions, notifications, orchestrator, prefs
from agent_sessions import orchestrator_ledger as ledger

HELD = "claude:11111111-1111-1111-1111-111111111111"
LOOSE = "claude:22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(tmp_path / "s.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))


@pytest.fixture
def held(fake_jsonl, tmp_path):
    m = missions.create_mission("ship it", title="Held mission", cwd=str(tmp_path))
    missions.adopt(m["id"], HELD)
    return m


@pytest.fixture
def unreadable_store(monkeypatch, tmp_path):
    broken = tmp_path / "missions-db-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(broken))
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


def _escalate(action_id: str, session: str) -> None:
    """A live escalation and its bell row — the order production writes them in."""
    ledger.append(
        {"id": action_id, "state": "escalated", "verb": "continue", "session_id": session}
    )
    notifications.add(
        title=f"needs you ({action_id})",
        project="p",
        session_id=session,
        engine="claude",
        action_id=action_id,
        escalation=True,
        activity_at=1_700_000_000.0,
    )


def test_a_held_decision_counts_and_a_mission_less_one_is_listed_but_not_counted(held):
    _escalate("act-held", HELD)
    _escalate("act-loose", LOOSE)

    out = notifications.listing()
    listed = {r["action_id"] for r in out["notifications"]}
    assert listed == {"act-held", "act-loose"}, "a decision with no surface must stay VISIBLE"
    assert out["unread"] == 1, "only the decision the mission console can act on is counted"
    assert (
        out["uncertain"] == 0
    ), "a mission-less decision is KNOWN to have no surface — that is not an uncertain state"


def test_detaching_the_session_takes_its_decision_out_of_the_count(held):
    """The count follows membership at READ time, the same fact the console renders from."""
    _escalate("act-held", HELD)
    assert notifications.listing()["unread"] == 1
    missions.detach(held["id"], HELD)
    out = notifications.listing()
    assert out["unread"] == 0
    assert [r["action_id"] for r in out["notifications"]] == ["act-held"]


def test_an_unreadable_membership_store_is_uncertain_never_actionable_never_hidden(
    fake_jsonl, unreadable_store
):
    _escalate("act-loose", LOOSE)
    out = notifications.listing()
    assert [r["action_id"] for r in out["notifications"]] == ["act-loose"]
    assert out["unread"] == 0
    assert out["uncertain"] == 1


def _persist_escalation(action_id: str, session: str, monkeypatch) -> list[dict]:
    prefs.set_orchestrator({"enabled": True, "notify": "escalations"})
    sent: list[dict] = []
    monkeypatch.setattr(notifications, "fanout", lambda note: sent.append(note))
    # `_persist` consults the fence first; this test is about the push, not the fence.
    monkeypatch.setattr(orchestrator, "_barred_sessions", lambda: set())
    orchestrator._persist(
        [
            {
                "id": action_id,
                "state": "escalated",
                "session_id": session,
                "engine": "claude",
                "title": "needs a decision",
                "project": "agent-sessions",
            }
        ]
    )
    return sent


def test_a_held_escalation_is_pushed(held, monkeypatch):
    assert [n["action_id"] for n in _persist_escalation("act-held", HELD, monkeypatch)] == [
        "act-held"
    ]


def test_a_mission_less_escalation_is_listed_but_not_pushed(held, monkeypatch):
    assert (
        _persist_escalation("act-loose", LOOSE, monkeypatch) == []
    ), "a push must not wake the operator for a decision no surface can take"
    assert [r["action_id"] for r in notifications.listing()["notifications"]] == ["act-loose"]


def test_an_unreadable_membership_store_fails_toward_pushing(
    fake_jsonl, unreadable_store, monkeypatch
):
    assert [n["action_id"] for n in _persist_escalation("act-loose", LOOSE, monkeypatch)] == [
        "act-loose"
    ]
