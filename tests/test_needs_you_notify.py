"""Needs-you notifications (#1086 Phase 4): one per EPISODE, persisted, and retracted — bell row
and device notification — the moment the session stops needing the operator.

Driven through the real `routes.pulse.build_needs_you` (the Ask page's own read) over a faked
world: cards, mission membership and the live screen. What is pinned is what the operator relies
on: a notification disappears when the session is answered in the terminal, when its decision
settles, when it is dismissed and when it is archived; a close push closes only its own tag; a
restart does not re-announce an open episode; an unreadable list retracts nothing."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from agent_sessions import (
    metadata,
    missions,
    needs_you_dismissals,
    needs_you_notify,
    notifications,
    orchestrator,
    orchestrator_ledger,
    prefs,
    pulse,
    review,
    webpush,
)
from automation_helpers import append_current_action, current_action

A = "claude:aaaaaaaa-0000-0000-0000-00000000000a"
B = "claude:aaaaaaaa-0000-0000-0000-00000000000b"
C = "claude:aaaaaaaa-0000-0000-0000-00000000000c"


def _card(sid, *, flagged=True, cwd="/work/a"):
    return {
        "id": sid,
        "engine": "claude",
        "title": f"title {sid[-1]}",
        "cwd": cwd,
        "project": {"kind": "folder", "id": "p1", "name": "Alpha"},
        "last_activity": time.time() - 60,
        "intervention_required": flagged,
        # Model-authored: must never reach the bell row or a push through this path.
        "intervention_reason": "MODEL TEXT the agent asked about secrets",
        "ai_summary": "s",
    }


@pytest.fixture(autouse=True)
def _own_stores(tmp_path, monkeypatch):
    """The dismissal store and prefs are pinned per SESSION by conftest, not per test: a dismissal
    written by one test would otherwise suppress a row in the next."""
    monkeypatch.setenv("AGENT_SESSIONS_NEEDS_YOU_DISMISSED", str(tmp_path / "dismissed.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))


@pytest.fixture
def world(monkeypatch):
    state = {
        "cards": [_card(A)],
        "held": set(),
        "held_error": False,
        "screen": {"prompt_class": "question", "menu": None, "fingerprint": "fp-1", "screen": "?"},
    }
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [dict(c) for c in state["cards"]])

    def held(**_k):
        if state["held_error"]:
            raise OSError("membership store unreadable")
        return set(state["held"])

    monkeypatch.setattr(missions, "all_active_memberships", held)
    monkeypatch.setattr(
        orchestrator, "observed_screen", lambda key, strict=False: dict(state["screen"])
    )
    monkeypatch.setattr(review, "last_words", lambda key, n=1500: "")
    return state


@pytest.fixture
def pushes(monkeypatch):
    """A subscribed device, and every payload sent to it."""
    sent: list[dict] = []
    subs = notifications._subs_path()
    notifications._write(
        subs,
        [{"id": "s1", "endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}}],
    )
    monkeypatch.setattr(webpush, "send", lambda sub, payload, **k: sent.append(json.loads(payload)))
    return sent


def _bell():
    return notifications.listing()


def _pending():
    _rows, _a, eps = notifications._load_doc(notifications._notifications_path())
    return sorted(v["tag"] for k, v in eps.items() if k.startswith(notifications.CLOSING))


def _needs_you_rows():
    return [n for n in _bell()["notifications"] if n.get("needs_you")]


def test_entering_needs_you_announces_ONCE_with_an_episode_tag_and_no_model_text(world, pushes):
    assert needs_you_notify.sync_once() == {"opened": 1, "closed": 0}
    rows = _needs_you_rows()
    assert len(rows) == 1 and rows[0]["session_id"] == A
    tag = rows[0]["tag"]
    assert tag.startswith(f"needs-you:{A}:")
    # The reason is the SERVER's kind label, never the review's model-authored text.
    assert rows[0]["reason"] == "Asks you a question"
    assert "MODEL TEXT" not in json.dumps(rows[0])
    assert _bell()["unread"] == 1
    assert len(pushes) == 1 and pushes[0]["tag"] == tag
    assert set(pushes[0]) == {"title", "body", "url", "tag"}
    # Still needing you is the SAME episode: nothing more is announced or pushed.
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    assert len(_needs_you_rows()) == 1 and len(pushes) == 1


def test_a_restart_or_a_bell_dismissal_does_not_re_announce_an_open_episode(world, pushes):
    needs_you_notify.sync_once()
    # The episode is on disk beside the rows — a fresh process reads the same file.
    _rows, _announced, episodes = notifications._load_doc(notifications._notifications_path())
    assert set(episodes) == {A}
    # The operator clears the bell (a DELETE of every row): the episode survives it.
    notifications.dismiss(None)
    notifications.mark_read(None)
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    assert _needs_you_rows() == [] and len(pushes) == 1


def test_answered_in_the_terminal_retracts_the_row_and_closes_ONLY_its_own_tag(world, pushes):
    world["cards"] = [_card(A), _card(B)]
    needs_you_notify.sync_once()
    tags = {r["session_id"]: r["tag"] for r in _needs_you_rows()}
    assert set(tags) == {A, B}
    # A is answered in the terminal: its review no longer flags it.
    world["cards"] = [_card(A, flagged=False), _card(B)]
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 1}
    assert [r["session_id"] for r in _needs_you_rows()] == [B]
    assert _bell()["unread"] == 1
    # No push whose only job is to retract (Hermes 5239, finding 7): A's retraction is OWED, rides
    # on the next shown push, and the app closes it from the bell's `close_tags` meanwhile.
    assert len(pushes) == 2
    assert _pending() == [tags[A]]
    assert _bell()["close_tags"] == [tags[A]]


def test_a_settled_decision_retracts_its_notification(world, pushes):
    # A decision-only session: not flagged by its review, but holding a decision you can act on.
    world["cards"] = [_card(A, flagged=False)]
    append_current_action(
        {
            "id": "act-1",
            "state": "escalated",
            "session_id": A,
            "verb": "answer",
            "answer": "x",
            "engine": "claude",
            "title": "t",
            "expires_at": time.time() + 3600,
        }
    )
    assert needs_you_notify.sync_once()["opened"] == 1
    tag = _needs_you_rows()[0]["tag"]
    orchestrator_ledger.transition("act-1", "rejected")
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 1}
    assert _needs_you_rows() == []
    # No push is sent for the retraction alone; it stays owed on disk, and the app closes the
    # device notification when it is opened.
    assert len(pushes) == 1
    assert _pending() == [tag]
    assert _bell()["close_tags"] == [tag]


def test_dismissing_on_Ask_retracts_it(world, pushes):
    needs_you_notify.sync_once()
    needs_you_dismissals.dismiss(A, "fp-1")
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 1}
    assert _needs_you_rows() == []


def test_archiving_or_joining_a_mission_retracts_it(world, pushes):
    world["cards"] = [_card(A), _card(B)]
    needs_you_notify.sync_once()
    world["cards"] = [_card(B)]  # A archived: gone from the cards
    world["held"] = {B}  # B taken into a mission: decided in its console now
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 2}
    assert _needs_you_rows() == [] and _bell()["unread"] == 0


def test_an_UNREADABLE_list_retracts_nothing(world, pushes):
    needs_you_notify.sync_once()
    world["held_error"] = True
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert len(_needs_you_rows()) == 1
    assert not [p for p in pushes if p.get("type") == "close"]


def test_switching_notify_off_closes_every_open_episode_and_opens_none(world, pushes):
    needs_you_notify.sync_once()
    prefs.set_session_review({"notify": False})
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 1}
    world["cards"] = [_card(A), _card(B)]
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    assert _needs_you_rows() == []


def test_needing_you_AGAIN_is_a_new_episode_with_a_new_tag(world, pushes):
    needs_you_notify.sync_once()
    first = _needs_you_rows()[0]["tag"]
    world["cards"] = [_card(A, flagged=False)]
    needs_you_notify.sync_once()
    time.sleep(0.002)  # episode ids are millisecond starts
    world["cards"] = [_card(A)]
    assert needs_you_notify.sync_once()["opened"] == 1
    second = _needs_you_rows()[0]["tag"]
    assert second != first


def test_other_writers_never_drop_the_open_episodes(world, pushes):
    needs_you_notify.sync_once()
    # Every other mutation of this store rewrites the whole document.
    notifications.add(title="t", project="p", session_id=B, engine="claude", escalation=True)
    notifications.mark_read(None)
    notifications.dismiss(None)
    _rows, _a, episodes = notifications._load_doc(notifications._notifications_path())
    assert set(episodes) == {A}
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}


def test_no_subscription_means_no_push_and_no_close_push_later(world, monkeypatch):
    sent: list = []
    monkeypatch.setattr(webpush, "send", lambda *a, **k: sent.append(a))
    needs_you_notify.sync_once()
    world["cards"] = []
    needs_you_notify.sync_once()
    assert sent == []


def test_the_payload_carries_titles_links_and_tags_and_nothing_else():
    body = json.loads(
        webpush.build_payload(
            title="T", project="P", url="/u", tag="needs-you:x:1", close=["needs-you:y:1"]
        )
    )
    assert body == {
        "title": "T",
        "body": "P",
        "url": "/u",
        "tag": "needs-you:x:1",
        "close": ["needs-you:y:1"],
    }
    # A payload without a tag is byte-for-byte what it was.
    assert set(json.loads(webpush.build_payload(title="T", project="P", url="/u"))) == {
        "title",
        "body",
        "url",
    }


def test_the_notify_pref_is_write_strict_and_read_lenient():
    assert prefs.validate_session_review_patch({"notify": "yes"}) is not None
    assert prefs.validate_session_review_patch({"notify": False}) is None
    assert prefs.get_session_review()["notify"] is True


# ---- one producer: the orchestrator no longer announces standalone sessions ----------------


def _rec(sid, aid):
    return current_action(
        {
            "id": aid,
            "state": "escalated",
            "session_id": sid,
            "verb": "answer",
            "answer": "x",
            "engine": "claude",
            "title": "needs a call",
            "last_activity": time.time(),
        }
    )


def test_the_orchestrator_announces_ONLY_mission_held_sessions(monkeypatch, pushes):
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {B})
    orchestrator._persist([_rec(A, "a-1"), _rec(B, "b-1")])
    rows = notifications.listing()["notifications"]
    # A (no mission) is the needs-you sync's to announce; B (mission-held) is announced here.
    assert [r["session_id"] for r in rows] == [B]
    assert len(pushes) == 1


def test_unreadable_membership_still_announces_rather_than_losing_the_escalation(
    monkeypatch, pushes
):
    def boom(**_k):
        raise OSError("unreadable")

    monkeypatch.setattr(missions, "all_active_memberships", boom)
    orchestrator._persist([_rec(A, "a-1")])
    assert [r["session_id"] for r in notifications.listing()["notifications"]] == [A]


# ---- Hermes 5231: retraction is owed until delivered, and only rides on something visible ----


def test_a_pending_retraction_rides_on_the_next_SHOWN_push_and_is_then_acknowledged(world, pushes):
    needs_you_notify.sync_once()
    tag_a = _needs_you_rows()[0]["tag"]
    world["cards"] = [_card(A, flagged=False)]
    needs_you_notify.sync_once()
    assert _pending() == [tag_a] and not [p for p in pushes if p.get("type") == "close"]
    # A new episode shows — and carries A's retraction, applied before it is shown.
    world["cards"] = [_card(A, flagged=False), _card(B)]
    needs_you_notify.sync_once()
    shown = [p for p in pushes if p.get("type") != "close"]
    assert shown[-1]["close"] == [tag_a]
    assert _pending() == []


def test_a_failed_retraction_is_retried_not_forgotten(world, pushes, monkeypatch):
    needs_you_notify.sync_once()
    tag_a = _needs_you_rows()[0]["tag"]
    world["cards"] = [_card(A, flagged=False)]
    needs_you_notify.sync_once()
    monkeypatch.setattr(
        webpush, "send", lambda *a, **k: (_ for _ in ()).throw(webpush.PushError("down"))
    )
    world["cards"] = [_card(A, flagged=False), _card(B)]
    needs_you_notify.sync_once()  # B's push carried A's retraction, and failed
    assert _pending() == [tag_a]  # still owed, on disk
    monkeypatch.setattr(
        webpush, "send", lambda sub, payload, **k: pushes.append(json.loads(payload))
    )
    world["cards"] = [_card(A, flagged=False), _card(B), _card(C)]
    needs_you_notify.sync_once()
    assert pushes[-1]["close"] == [tag_a]
    assert _pending() == []


def test_more_than_one_batch_of_retractions_drains_and_acknowledges_ONLY_what_was_sent(
    world, pushes
):
    many = [f"claude:aaaaaaaa-0000-0000-0000-0000000001{i:02d}" for i in range(22)]
    world["cards"] = [_card(sid) for sid in many]
    needs_you_notify.sync_once()
    world["cards"] = [_card(sid, flagged=False) for sid in many]
    needs_you_notify.sync_once()
    owed = _pending()
    assert len(owed) == 22
    world["cards"] = [_card(sid, flagged=False) for sid in many] + [_card(B)]
    needs_you_notify.sync_once()
    sent = pushes[-1]["close"]
    assert len(sent) == webpush.CLOSE_MAX
    # Exactly the batch on the wire is acknowledged; the rest stays owed for the next shown push.
    assert sorted(_pending()) == sorted(set(owed) - set(sent))
    world["cards"] = [_card(sid, flagged=False) for sid in many] + [_card(B), _card(C)]
    needs_you_notify.sync_once()
    assert _pending() == []


def test_the_bell_read_lists_owed_retractions_and_omits_them_when_unreadable(world, pushes):
    needs_you_notify.sync_once()
    tag = _needs_you_rows()[0]["tag"]
    assert _bell()["close_tags"] == []
    world["cards"] = []
    needs_you_notify.sync_once()
    assert _bell()["close_tags"] == [tag]
    notifications._notifications_path().write_text("{ not json")
    # Absent — never an empty list the app would read as "nothing owed".
    assert "close_tags" not in _bell()


# ---- Hermes 5231: incomplete reads retract nothing and rewrite nothing -----------------------


def test_an_unreadable_notifications_store_is_never_rewritten(world, pushes):
    needs_you_notify.sync_once()
    path = notifications._notifications_path()
    path.write_text("{ torn")
    assert needs_you_notify.sync_once() == {"skipped": "store unreadable"}
    assert path.read_text() == "{ torn"
    # Every other writer refuses too, rather than replacing it with an invented empty document.
    with pytest.raises(notifications.StoreUnreadable):
        notifications.add(title="t", project="p", session_id=B, engine="claude", escalation=True)
    assert path.read_text() == "{ torn"


def test_an_unreadable_dismissal_record_retracts_nothing_and_re_announces_nothing(world, pushes):
    needs_you_notify.sync_once()
    needs_you_dismissals.dismiss(A, "fp-1")
    assert needs_you_notify.sync_once()["closed"] == 1
    needs_you_dismissals._path().write_text("{ torn")
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert _needs_you_rows() == [] and len([p for p in pushes if p.get("type") != "close"]) == 1


def test_an_incomplete_engine_scan_retracts_nothing(world, pushes, monkeypatch):
    needs_you_notify.sync_once()
    from agent_sessions import engines

    monkeypatch.setattr(engines, "scan_all_checked", lambda: ([], ["claude: unreadable"]))
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert len(_needs_you_rows()) == 1


# The REAL card read (no `world`): the metadata sidecar is read under the writers' lock.


@pytest.fixture
def real_cards(fake_jsonl, monkeypatch):
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: set())
    monkeypatch.setattr(
        orchestrator,
        "observed_screen",
        lambda key, strict=False: {
            "prompt_class": "question",
            "menu": None,
            "fingerprint": "fp",
            "screen": "?",
        },
    )
    sid = "claude:11111111-1111-1111-1111-111111111111"
    metadata.patch(sid, intervention_required=True, intervention_reason="asked")
    return sid


def test_a_metadata_write_in_progress_does_not_end_the_episode(real_cards, pushes):
    assert needs_you_notify.sync_once()["opened"] >= 1
    path = metadata._default_path()
    with metadata._exclusive(path) as fh:
        body = fh.read()
        fh.seek(0)
        fh.truncate()  # a writer's in-place rewrite, mid-flight: the file is ZERO BYTES
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(needs_you_notify.sync_once()))
        t.start()
        time.sleep(0.3)
        assert t.is_alive(), "the strict read must wait for the writer's lock"
        fh.write(body)
        fh.flush()
    t.join(10)
    assert result == {"opened": 0, "closed": 0}


def test_an_unreadable_metadata_sidecar_retracts_nothing(real_cards, pushes, monkeypatch):
    needs_you_notify.sync_once()
    before = len(_needs_you_rows())

    def denied(*_a, **_k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(metadata, "_exclusive", denied)
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert len(_needs_you_rows()) == before


# ---- Hermes 5231 finding 4: an escalation announced while membership was unreadable ----------


def test_a_transient_first_read_never_erases_rows(world, pushes, monkeypatch):
    needs_you_notify.sync_once()
    notifications.add(title="t", project="p", session_id=B, engine="claude", escalation=True)
    path = notifications._notifications_path()
    before = path.read_text()
    real = type(path).read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        if self == path and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(13, "transient")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", flaky)
    with pytest.raises(notifications.StoreUnreadable):
        notifications.add(title="u", project="p", session_id=C, engine="claude", escalation=True)
    assert path.read_text() == before


def test_a_refused_write_leaks_no_file_descriptor(world, pushes):
    needs_you_notify.sync_once()
    notifications._notifications_path().write_text("{ torn")
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        with pytest.raises(notifications.StoreUnreadable):
            notifications.add(title="t", project="p", session_id=B, engine="claude")
    assert len(os.listdir("/proc/self/fd")) <= before


# ---- Hermes 5265 ---------------------------------------------------------------------------


def test_an_unreadable_subscription_list_acknowledges_nothing(world, pushes, monkeypatch):
    needs_you_notify.sync_once()
    tag_a = _needs_you_rows()[0]["tag"]
    world["cards"] = []
    needs_you_notify.sync_once()
    assert _pending() == [tag_a]
    # The list read that decided to push succeeded; the file tore before the send read it.
    subs = notifications.list_subscriptions()
    monkeypatch.setattr(notifications, "list_subscriptions", lambda path=None: subs)
    notifications._subs_path().write_text("{ torn")
    world["cards"] = [_card(B)]
    needs_you_notify.sync_once()  # B opens; its push cannot even find its recipients
    assert _pending() == [tag_a]  # never "sent to nobody, so done"


def test_retractions_ride_within_the_ENCODED_push_budget_and_only_those_are_acknowledged(
    world, pushes
):
    many = [f"claude:aaaaaaaa-0000-0000-0000-0000000002{i:02d}" for i in range(20)]
    world["cards"] = [_card(sid) for sid in many]
    needs_you_notify.sync_once()
    world["cards"] = []
    needs_you_notify.sync_once()
    owed = _pending()
    heavy = _card(B)
    heavy["title"] = "🙂" * 120
    heavy["project"] = {"kind": "folder", "id": "p", "name": "漢" * 240}
    world["cards"] = [heavy]
    needs_you_notify.sync_once()
    sent = pushes[-1]["close"]
    assert 0 < len(sent) < len(owed)  # the budget, not the count, bounded it
    assert len(json.dumps(pushes[-1], separators=(",", ":")).encode()) <= webpush.PAYLOAD_MAX_BYTES
    assert sorted(_pending()) == sorted(set(owed) - set(sent))


def test_a_dismissed_session_whose_screen_cannot_be_read_is_not_re_announced(
    world, pushes, monkeypatch
):
    needs_you_notify.sync_once()
    needs_you_dismissals.dismiss(A, "fp-1")
    assert needs_you_notify.sync_once()["closed"] == 1

    def unreadable(key, strict=False):
        if strict:
            raise orchestrator.ScreenUnreadable("OSError")
        return {"prompt_class": "open", "menu": None, "fingerprint": "", "screen": ""}

    monkeypatch.setattr(orchestrator, "observed_screen", unreadable)
    assert needs_you_notify.sync_once() == {"skipped": "unavailable"}
    assert len([p for p in pushes if "close" not in p]) == 1


def test_the_app_can_still_retract_after_the_PUSH_path_gave_up(world, pushes):
    needs_you_notify.sync_once()
    tag = _needs_you_rows()[0]["tag"]
    world["cards"] = []
    needs_you_notify.sync_once()
    later = time.time() + notifications.CLOSING_KEEP_S + 60
    notifications.sync_needs_you({}, now=later)
    assert _pending() == []  # no longer carried by pushes…
    assert tag in _bell()["close_tags"]  # …but still closed by the app
    notifications.sync_needs_you({}, now=time.time() + notifications.CLOSED_KEEP_S + 60)
    assert tag not in _bell()["close_tags"]  # past the supported horizon


# ---- Hermes 5275 findings 1-2: an episode DEFERS to a live escalation, never adopts it --------

URL_TAG_A = f"/s/claude/{A.split(':', 1)[1]}"


def _escalation_rows(sid):
    return [
        r
        for r in _bell()["notifications"]
        if r["session_id"] == sid and r.get("escalation") is True and not r.get("needs_you")
    ]


def test_a_fallback_announcement_is_deferred_to_announced_once_and_never_retracted_by_the_episode(
    world, pushes, monkeypatch
):
    def unreadable(**_k):
        raise OSError("membership unreadable")

    monkeypatch.setattr(missions, "all_active_memberships", unreadable)
    orchestrator._persist([_rec(A, "a-1")])  # fails toward announcing: a URL-tagged push
    assert [p.get("tag") for p in pushes] == [None]
    # Membership recovers and says A is standalone: the SAME situation — no second announcement,
    # and the escalation row is left exactly as it was.
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: set())
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    assert len(pushes) == 1
    assert _needs_you_rows() == [] and len(_escalation_rows(A)) == 1
    # The episode ends: it announced nothing, so it owes nothing, and the escalation's reusable
    # URL tag is never put on a close list (its own decision settles that row).
    world["cards"] = []
    assert needs_you_notify.sync_once()["closed"] == 1
    assert _pending() == [] and _bell()["close_tags"] == []
    assert len(_escalation_rows(A)) == 1
    # Operator decision on #1136 (option 1): the degraded fallback is a mission-style escalation.
    # Its decision settling retires the BELL row; its device notification is not retracted.
    orchestrator_ledger.transition("a-1", "rejected")
    assert _escalation_rows(A) == []
    assert _pending() == [] and _bell()["close_tags"] == []


def test_an_OPEN_episode_is_not_announced_again_and_a_later_escalation_is_never_consumed(
    world, pushes, monkeypatch
):
    needs_you_notify.sync_once()  # A's episode is open and pushed under its own tag
    tag = _needs_you_rows()[0]["tag"]

    def unreadable(**_k):
        raise OSError("membership unreadable")

    monkeypatch.setattr(missions, "all_active_memberships", unreadable)
    orchestrator._persist([_rec(A, "a-1")])
    assert len(pushes) == 1  # the open episode already covers it: no second announcement
    # An escalation that got in anyway keeps its own row and lifecycle.
    notifications.add(
        title="dup", project="p", session_id=A, engine="claude", action_id="x", escalation=True
    )
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: set())
    world["cards"] = []
    needs_you_notify.sync_once()
    assert _pending() == [tag]  # only the episode's own, never-reused tag
    assert len(_escalation_rows(A)) == 1


def test_a_retraction_can_never_close_a_NEW_escalation_under_a_reused_url_tag(
    world, pushes, monkeypatch
):
    def unreadable(**_k):
        raise OSError("membership unreadable")

    monkeypatch.setattr(missions, "all_active_memberships", unreadable)
    orchestrator._persist([_rec(A, "a-1")])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: set())
    needs_you_notify.sync_once()  # deferred
    world["cards"] = []
    orchestrator_ledger.transition("a-1", "rejected")
    needs_you_notify.sync_once()
    # A joins a mission and escalates again under the SAME URL tag; B then opens and pushes.
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {A})
    orchestrator._persist([_rec(A, "a-2")])
    world["cards"] = [_card(B)]
    needs_you_notify.sync_once()
    assert URL_TAG_A not in _bell()["close_tags"]
    assert all(URL_TAG_A not in (p.get("close") or []) for p in pushes)


def test_joining_a_mission_closes_the_standalone_episode_and_KEEPS_the_missions_escalation(
    world, pushes, monkeypatch
):
    needs_you_notify.sync_once()  # standalone episode for A, pushed
    tag = _needs_you_rows()[0]["tag"]
    # A joins a mission; the orchestrator announces the mission's own escalation before the sync.
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {A})
    orchestrator._persist([_rec(A, "m-1")])
    world["cards"] = []  # mission-held: no longer on the needs-you list
    needs_you_notify.sync_once()
    assert _pending() == [tag]
    rows = _escalation_rows(A)
    assert len(rows) == 1 and not rows[0].get("retired")
    assert URL_TAG_A not in _bell()["close_tags"]
