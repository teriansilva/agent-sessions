"""Ask follows the sessions it started (#1443 Phase 4a; following nudged sessions is #1517).

* A pass is DETERMINISTIC: no model call, no notification. ``finished`` is an edge in the
  session's own visible-output clock since the follow began (it survives a restart through the
  scrollback's `.visible` stamp), a newly flagged card is ``needs_you``, and a session missing
  from two COMPLETE catalog reads in a row is ``ended`` (and unfollowed).
* An update is a KIND plus the session's title through the store boundary — never model text.
* Started sessions are followed once per start; Unfollow sticks.
* ``seen`` against the last update is the unread marker; follows are bounded.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import time
import uuid

import pytest

from agent_sessions import (
    ask_follow,
    ask_store,
    notifications,
    orchestrator_ledger,
    review,
    template_secrets,
)
from test_ask_orchestrator import App, app  # noqa: F401 — fixtures are used by name

S1 = "claude:00000000-0000-4000-8000-000000000001"
S2 = "claude:00000000-0000-4000-8000-000000000002"


def _card(key=S1, title="api rebase", needs=False) -> dict:
    return {"id": key, "title": title, "intervention_required": needs, "state": "idle"}


_CLOCK: dict = {"now": 0.0, "out": {}}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """One clock for the passes AND the follower's own `time.time()` (a follow's `since`, the
    `seen` cap), so simulated minutes stay consistent with what the follower believes is now."""
    from types import SimpleNamespace

    _CLOCK.update(now=time.time(), out={})
    monkeypatch.setattr(ask_follow, "time", SimpleNamespace(time=lambda: _CLOCK["now"]))


def _run(catalog, working=()) -> int:
    """One pass, a minute after the last. A session in ``working`` produced visible output in
    that minute (its output clock advances); every other session's clock stands still."""
    _CLOCK["now"] += 60.0
    for k in working:
        _CLOCK["out"][k] = _CLOCK["now"]
    return ask_follow.pass_once(dict(_CLOCK["out"]), catalog=catalog, now=_CLOCK["now"])


def _updates(cid: str) -> list[dict]:
    return [r for r in ask_store.read(cid) if r["type"] == "update"]


@pytest.fixture
def quiet(monkeypatch):
    """No model call and no notification may happen during a pass."""

    def forbidden(*a, **k):
        raise AssertionError("the follower must not call a model or notify")

    monkeypatch.setattr(review, "post_chat_response", forbidden)
    monkeypatch.setattr(notifications, "sync_needs_you", forbidden)


def test_the_first_look_is_a_baseline_then_finishing_a_turn_is_one_update(quiet):
    cid = ask_store.create()
    assert ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    assert _run([_card()], working=(S1,)) == 0  # baseline: working
    assert _run([_card()], working=(S1,)) == 0  # still working: nothing
    assert _run([_card()]) == 1  # stopped
    assert _run([_card()]) == 0  # a no-op pass writes nothing
    (u,) = _updates(cid)
    assert u["kind"] == "finished" and u["key"] == S1 and u["title"] == "api rebase"
    assert "text" not in u  # a kind and a title, never model text


def test_a_newly_flagged_card_is_needs_you_once(quiet):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    assert _run([_card(needs=True)]) == 1
    assert _run([_card(needs=True)]) == 0
    assert [u["kind"] for u in _updates(cid)] == ["needs_you"]


def test_a_session_gone_from_the_catalog_twice_ends_its_follow(quiet):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    assert _run([]) == 0  # one miss is not an archive
    assert _run([_card()]) == 0  # and resets
    _run([])
    assert _run([]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["ended"]
    assert ask_follow.state(cid)["following"] == []


def test_a_turn_that_finished_before_a_restart_is_still_reported(quiet):
    """The in-memory output clock is empty after a restart; the stamp on disk still knows the
    turn that finished between the last pass and the restart (Hermes on #1500)."""
    from agent_sessions import scrollback

    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    _CLOCK["now"] += 20
    scrollback._persist_visible(S1, _CLOCK["now"])  # it printed, then went quiet
    # …and the server restarted (the in-memory clock is not passed in: `{}` below)
    _CLOCK["now"] += 60
    assert ask_follow.pass_once({}, catalog=[_card()], now=_CLOCK["now"]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["finished"]
    _CLOCK["now"] += 60
    assert ask_follow.pass_once({}, catalog=[_card()], now=_CLOCK["now"]) == 0  # once


def test_a_short_turn_just_after_the_follow_survives_a_restart(quiet, monkeypatch):
    """Hermes on #1500: output stamped just BEFORE the follow, then a short turn within the
    same second after it, then a restart. The real append path records the turn on disk."""
    from agent_sessions import scrollback

    base = _CLOCK["now"]
    monkeypatch.setattr(scrollback.time, "time", lambda: _CLOCK["now"])
    scrollback._buffer_append(S1, b"earlier output\r\n")  # at base, before the follow
    _CLOCK["now"] = base + 0.2
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _CLOCK["now"] = base + 0.5
    scrollback._buffer_append(S1, b"the short turn\r\n")  # after the follow, same second
    _CLOCK["now"] = base + 61.0  # restart, then the first pass: no in-memory clock
    assert ask_follow.pass_once({}, catalog=[_card()], now=_CLOCK["now"]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_a_disk_stamp_from_before_the_follow_reports_nothing(quiet):
    from agent_sessions import scrollback

    scrollback._persist_visible(S1, _CLOCK["now"] - 300)  # output long before the start
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _CLOCK["now"] += 60
    assert ask_follow.pass_once({}, catalog=[_card()], now=_CLOCK["now"]) == 0
    assert _updates(cid) == []


def test_an_unreadable_follow_state_is_unknown_not_empty(monkeypatch):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)

    def unreadable(_cid):
        raise ask_store.SidecarUnreadable("disk")

    monkeypatch.setattr(ask_store, "read_follows", unreadable)
    got = ask_follow.state(cid)
    assert got["incomplete"] is True and got["following"] == []


def test_an_update_stored_but_not_appended_is_pending(quiet, monkeypatch):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()], working=(S1,))
    real = ask_store.append

    def fails(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ask_store, "append", fails)
    _run([_card()])  # finished is stored in the outbox, its append fails
    assert ask_follow.state(cid)["pending"] is True
    monkeypatch.setattr(ask_store, "append", real)
    _run([_card()])  # the next pass flushes it
    assert ask_follow.state(cid)["pending"] is False
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_an_intervention_before_the_first_pass_is_reported_once(quiet):
    """A started session can need the operator inside the follower's first minute (Hermes on
    #1500): the follow began before it, so the first pass reports it."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    assert _run([_card(needs=True)]) == 1
    assert _run([_card(needs=True)]) == 0  # unchanged: once
    assert [u["kind"] for u in _updates(cid)] == ["needs_you"]


def test_a_started_session_that_never_needs_anyone_reports_nothing_until_it_does(quiet):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    assert _run([_card()]) == 0
    assert _run([_card()]) == 0
    assert _run([_card(needs=True)]) == 1
    assert _run([_card(needs=False)]) == 0  # cleared: no update for that
    assert _run([_card(needs=True)]) == 1  # a new episode reports again
    assert [u["kind"] for u in _updates(cid)] == ["needs_you", "needs_you"]


def test_an_incomplete_read_never_ends_a_follow_and_recovery_keeps_it(quiet):
    """Absence from a read that could not see every session proves nothing (Hermes on #1500)."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    for _ in range(ask_follow.MISSES_TO_END + 2):
        _CLOCK["now"] += 60.0
        assert ask_follow.pass_once({}, catalog=[], complete=False, now=_CLOCK["now"]) == 0
    assert ask_follow.state(cid)["following"] == [S1]
    # The read recovers and the session is still there: nothing ended.
    assert _run([_card()]) == 0
    assert _updates(cid) == []
    # A COMPLETE read that misses it twice still ends it, so archiving still works.
    _run([])
    assert _run([]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["ended"]


def test_the_default_read_is_strict_and_an_incomplete_one_ends_nothing(quiet, monkeypatch):
    from agent_sessions import pulse, pulse_chat

    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    strict_calls: list[bool] = []

    def build_cards(*, strict=False, **_k):
        strict_calls.append(strict)
        if strict:
            raise pulse.CardsIncomplete("codex store unreadable")
        return []

    monkeypatch.setattr(pulse, "build_cards", build_cards)
    monkeypatch.setattr(pulse_chat, "build_catalog", lambda **_k: [])
    monkeypatch.setattr("agent_sessions.routes.pulse.in_scope_cards", lambda cards: cards)
    for _ in range(ask_follow.MISSES_TO_END + 1):
        _CLOCK["now"] += 60.0
        ask_follow.pass_once({}, now=_CLOCK["now"])
    assert strict_calls and strict_calls[0] is True
    assert ask_follow.state(cid)["following"] == [S1]
    assert _updates(cid) == []


def test_an_update_title_passes_the_store_boundary(quiet):
    template_secrets.register_typed(["purple-monkey-dishwasher-tag"])
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card(title="deploy purple-monkey-dishwasher-tag")], working=(S1,))
    _run([_card(title="deploy purple-monkey-dishwasher-tag")])
    raw = (ask_store.root() / f"{cid}.jsonl").read_text()
    raw += (ask_store.root() / "follows" / f"{cid}.json").read_text()
    assert "purple-monkey-dishwasher-tag" not in raw


def test_follows_are_bounded():
    cid = ask_store.create()
    keys = [f"claude:{uuid.uuid4()}" for _ in range(ask_store.MAX_FOLLOWS + 1)]
    taken = [
        ask_follow.follow(cid, k, title="x", via="start", reason=uuid.uuid4().hex) for k in keys
    ]
    assert taken == [True] * ask_store.MAX_FOLLOWS + [False]
    assert len(ask_follow.state(cid)["following"]) == ask_store.MAX_FOLLOWS


def test_past_the_update_cap_a_conversation_stops_following(quiet, monkeypatch):
    monkeypatch.setattr(ask_store, "MAX_UPDATES", 2)
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    for _ in range(4):
        _run([_card()], working=(S1,))
        _run([_card()])
    assert len(_updates(cid)) == 2
    assert ask_follow.state(cid)["following"] == []
    assert ask_follow.follow(cid, S2, title="x", via="start", reason=uuid.uuid4().hex) is False


def test_unread_is_the_last_update_against_seen(quiet):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    assert ask_follow.state(cid)["unread"] is False
    _run([_card()], working=(S1,))
    _run([_card()])
    assert ask_follow.state(cid)["unread"] is True
    assert ask_follow.unread_conversations() == [cid]
    ask_follow.mark_seen(cid, _CLOCK["now"])
    assert ask_follow.state(cid)["unread"] is False and ask_follow.unread_conversations() == []


def test_seen_never_covers_an_update_the_client_had_not_shown(quiet):
    """The client says how far it has SHOWN; an update written after its read stays unread."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()], working=(S1,))
    _run([_card()])
    shown = _updates(cid)[-1]["ts"]
    _run([_card(needs=True)])  # written after the client's read
    assert ask_follow.mark_seen(cid, shown) is True
    assert ask_follow.state(cid)["unread"] is True
    assert ask_follow.mark_seen(cid, _updates(cid)[-1]["ts"]) is False


def test_a_followed_session_now_excluded_counts_as_gone(quiet, monkeypatch, tmp_path):
    """Roots + exclusions are a boundary: a session excluded after it was followed stops surfacing
    and its follow ends."""
    from agent_sessions import prefs, pulse

    work = tmp_path / "work"
    work.mkdir()
    card = {**_card(), "cwd": str(work), "project": {"kind": "folder"}}
    # The follower's own read is the STRICT card read (an incomplete one ends nothing).
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [card])
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run(None)
    assert ask_follow.state(cid)["following"] == [S1]
    prefs.set_folder_exclusions([str(work)])
    _run(None)
    assert _run(None) == 1
    assert [u["kind"] for u in _updates(cid)] == ["ended"]
    assert ask_follow.state(cid)["following"] == []


def test_retention_removes_the_follows_with_the_conversation():
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    ask_store.delete(cid)
    assert not (ask_store.root() / "follows" / f"{cid}.json").exists()
    assert not ask_store._follows_lock_path(cid).exists()


def test_an_unknown_update_kind_is_refused_by_the_store():
    cid = ask_store.create()
    with pytest.raises(ValueError):
        ask_store.append(
            cid,
            {"type": "update", "key": S1, "kind": "rm -rf", "title": "x", "event_id": "a" * 32},
        )


# ---- routes ------------------------------------------------------------------------------------


def test_the_conversation_carries_its_updates_and_follows_and_seen_clears_unread(app, quiet):
    cid = app.new()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()], working=(S1,))
    _run([_card()])
    body = app.get(cid)
    assert body["following"] == [S1] and body["unread"] is True
    (u,) = body["updates"]
    assert u["kind"] == "finished" and u["key"] == S1 and u["following"] is True
    assert app.c.get("/api/ask/unread").json() == {"unread": True, "conversations": 1}
    (row,) = [
        r for r in app.c.get("/api/ask/conversations").json()["conversations"] if r["id"] == cid
    ]
    assert row["unread"] is True
    assert (
        app.c.post(f"/api/ask/conversations/{cid}/seen", json={}, headers=app.hdr).status_code
        == 422
    )
    r = app.c.post(f"/api/ask/conversations/{cid}/seen", json={"through": u["ts"]}, headers=app.hdr)
    assert r.status_code == 200 and r.json() == {"unread": False}
    assert app.get(cid)["unread"] is False
    assert app.c.get("/api/ask/unread").json()["unread"] is False


@pytest.mark.parametrize(
    "raw",
    [
        '{"through": ' + "9" * 400 + "}",  # overflowed float(): an unhandled 500 (Hermes on #1500)
        '{"through": -1}',
        '{"through": 4102444801}',
        '{"through": Infinity}',
        '{"through": NaN}',
        '{"through": true}',
        '{"through": "1"}',
    ],
)
def test_seen_refuses_anything_but_a_time_in_range(app, raw):
    cid = app.new()
    r = app.c.post(
        f"/api/ask/conversations/{cid}/seen",
        content=raw,
        headers={**app.hdr, "Content-Type": "application/json"},
    )
    assert r.status_code == 422, r.text


def test_a_follow_with_no_update_yet_is_listed_with_its_title(app):
    """Unfollow must not wait for the first update (Hermes on #1500)."""
    cid = app.new()
    ask_follow.follow(cid, S1, title="api rebase", via="start", reason=uuid.uuid4().hex)
    body = app.get(cid)
    assert body["updates"] == []
    assert body["follows"] == [{"key": S1, "title": "api rebase"}]
    app.c.delete(f"/api/ask/conversations/{cid}/follow/{S1}", headers=app.hdr)
    assert app.get(cid)["follows"] == []


def test_every_remembered_follow_says_when_it_last_began_so_old_notices_stay_old(app):
    """#1755: an inactive follow keeps its `since`, and following again moves it forward, so the
    client can tell a superseded `ended` notice from the current follow's."""
    cid = app.new()
    ask_follow.follow(
        cid, S1, title="api rebase", via="start", reason=uuid.uuid4().hex, since=100.0
    )
    assert app.get(cid)["follow_since"] == {S1: 100.0}
    app.c.delete(f"/api/ask/conversations/{cid}/follow/{S1}", headers=app.hdr)
    body = app.get(cid)
    assert body["follows"] == [] and body["follow_since"] == {S1: 100.0}
    ask_follow.follow(
        cid, S1, title="api rebase", via="start", reason=uuid.uuid4().hex, since=200.0
    )
    assert app.get(cid)["follow_since"] == {S1: 200.0}


def test_the_conversation_says_when_follows_are_unknown_or_an_update_is_pending(app, monkeypatch):
    cid = app.new()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    body = app.get(cid)
    assert body["follows_incomplete"] is False and body["updates_pending"] is False

    def unreadable(_cid):
        raise ask_store.SidecarUnreadable("disk")

    monkeypatch.setattr(ask_store, "read_follows", unreadable)
    assert app.get(cid)["follows_incomplete"] is True


def test_unfollow_route_needs_csrf_and_stops_following(app):
    cid = app.new()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    r = app.c.delete(f"/api/ask/conversations/{cid}/follow/{S1}")
    assert r.status_code == 403 and ask_follow.state(cid)["following"] == [S1]
    r = app.c.delete(f"/api/ask/conversations/{cid}/follow/{S1}", headers=app.hdr)
    assert r.status_code == 200 and ask_follow.state(cid)["following"] == []
    assert (
        app.c.delete(f"/api/ask/conversations/{cid}/follow/{S1}", headers=app.hdr).status_code
        == 404
    )


def test_one_pass_never_writes_past_the_update_cap(quiet, monkeypatch):
    """A single pass with two changes and room for one writes one."""
    monkeypatch.setattr(ask_store, "MAX_UPDATES", 1)
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()], working=(S1,))
    assert _run([_card(needs=True)]) == 1  # finished AND needs you
    assert len(_updates(cid)) == 1


def test_a_conversation_removed_by_retention_gets_no_follow_sidecar_back(monkeypatch):
    """Removed between the first look and the lock (retention runs concurrently): checked again
    under the lock, so no sidecar is written for a conversation that is gone."""
    cid = ask_store.create()
    looks = {"n": 0}
    real = ask_store.exists

    def exists(c):
        looks["n"] += 1
        if looks["n"] == 1:
            return True  # the first look, before retention got to it
        ask_store.delete(c)
        return real(c)

    monkeypatch.setattr(ask_store, "exists", exists)
    assert ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex) is False
    assert not (ask_store.root() / "follows" / f"{cid}.json").exists()


# ---- Hermes round 1 on #1500: durability ---------------------------------------------------------


def _transition(cid: str) -> None:
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()], working=(S1,))


def test_an_append_that_fails_after_the_state_is_stored_is_flushed_once_next_pass(
    quiet, monkeypatch
):
    cid = ask_store.create()
    _transition(cid)
    real = ask_store.append
    calls = {"n": 0}

    def failing(c, *recs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(c, *recs)

    monkeypatch.setattr(ask_store, "append", failing)
    _run([_card()])  # the transition; its append fails
    assert _updates(cid) == []
    assert ask_follow.state(cid)["unread"] is True  # the marker is already there
    _run([_card()])  # flushed, not re-detected
    _run([_card()])
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_a_clear_that_fails_after_the_append_never_doubles_the_update(quiet, monkeypatch):
    cid = ask_store.create()
    _transition(cid)
    real = ask_store.write_follows
    calls = {"n": 0}

    def failing(c, doc):
        calls["n"] += 1
        if calls["n"] == 2:  # 1: state + outbox; 2: clearing the outbox after the append
            raise OSError("disk hiccup")
        return real(c, doc)

    monkeypatch.setattr(ask_store, "write_follows", failing)
    _run([_card()])
    assert len(_updates(cid)) == 1
    _run([_card()])  # the outbox is still there: found by id
    _run([_card()])
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_a_started_session_whose_follow_was_not_recorded_is_followed_by_the_pass(quiet):
    """The launch's own follow failed (a stop, a write): the pass reconciles it from the proposal
    sidecar, once — and an Unfollow after that is never undone."""
    cid = ask_store.create()
    pid = uuid.uuid4().hex
    with ask_store.proposals_lock(cid):
        ask_store.write_proposals(
            cid,
            [
                {
                    "id": pid,
                    "turn_id": str(uuid.uuid4()),
                    "kind": "session",
                    "state": "started",
                    "engine": "claude",
                    "project_id": "p1",
                    "project": "api",
                    "agent": "Claude Code",
                    "cwd": "/work/api",
                    "real": "/work/api",
                    "brief": "rebase",
                    "bypass_default": False,
                    "bypass": False,
                    "session_key": S1,
                }
            ],
        )
    _run([_card()])
    assert ask_follow.state(cid)["following"] == [S1]
    ask_follow.unfollow(cid, S1)
    _run([_card()])
    assert ask_follow.state(cid)["following"] == []


def test_an_ended_update_whose_append_failed_is_still_flushed(quiet, monkeypatch):
    """The follow ENDED in the failing pass, so the conversation has no active follow left — its
    outbox alone must bring it back into the next pass."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    _run([_card()])
    _run([])  # one miss
    real = ask_store.append
    calls = {"n": 0}

    def failing(c, *recs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(c, *recs)

    monkeypatch.setattr(ask_store, "append", failing)
    _run([])  # ended; its append fails
    assert ask_follow.state(cid)["following"] == [] and _updates(cid) == []
    _run([])
    assert [u["kind"] for u in _updates(cid)] == ["ended"]


# ---- Hermes round 2 on #1500 ----------------------------------------------------------------


def test_a_short_turn_between_two_passes_is_still_finished(quiet):
    """Output at 10 s, quiet again well before the next pass: never sampled busy, still an edge in
    the session's own output clock (Hermes: samples at 0/60/120 s, output at 10 s)."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    t0 = time.time() + 1
    assert ask_follow.pass_once({}, catalog=[_card()], now=t0) == 0  # baseline, no output yet
    assert ask_follow.pass_once({S1: t0 + 10}, catalog=[_card()], now=t0 + 60) == 1
    assert ask_follow.pass_once({S1: t0 + 10}, catalog=[_card()], now=t0 + 120) == 0
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_a_turn_already_running_at_the_first_look_reports_when_it_ends(quiet):
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    t0 = time.time() + 1
    assert ask_follow.pass_once({S1: t0 - 0.5}, catalog=[_card()], now=t0) == 0  # working now
    assert ask_follow.pass_once({S1: t0 - 0.5}, catalog=[_card()], now=t0 + 60) == 1


def test_output_from_before_the_follow_never_reports(quiet):
    """The baseline is when the follow began: a session whose last output predates it — and a
    restarted server with no output time for it — reports nothing."""
    cid = ask_store.create()
    t0 = time.time() + 1
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex, since=t0 - 100)
    assert ask_follow.pass_once({S1: t0 - 300}, catalog=[_card()], now=t0) == 0
    assert ask_follow.pass_once({S1: t0 - 300}, catalog=[_card()], now=t0 + 60) == 0
    assert ask_follow.pass_once({}, catalog=[_card()], now=t0 + 120) == 0
    assert _updates(cid) == []


def test_a_short_turn_before_the_first_pass_is_still_finished(quiet):
    """Hermes round 3: followed at T, output at T+10, first pass at T+60 — the baseline is the
    follow's own start, so that turn reports."""
    cid = ask_store.create()
    t = time.time() + 1
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex, since=t)
    assert ask_follow.pass_once({S1: t + 10}, catalog=[_card()], now=t + 60) == 1
    assert ask_follow.pass_once({S1: t + 10}, catalog=[_card()], now=t + 120) == 0
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


# ---- #1517: sessions a delivered Ask nudge reached -----------------------------------------


def _key(i: int) -> str:
    return f"claude:00000000-0000-4000-8000-{i:012d}"


def _nudge(cid: str, key: str = S1, state: str = "delivered", ts: float | None = None) -> str:
    aid = uuid.uuid4().hex
    orchestrator_ledger.append_batch_for_free_sessions(
        [
            {
                "id": aid,
                "state": state,
                "ts": _CLOCK["now"] if ts is None else ts,
                "source": "ask",
                "session_id": key,
                "title": "docs pass",
                "ask_conversation": cid,
            }
        ]
    )
    return aid


@pytest.mark.parametrize(
    ("state", "follows"),
    [
        ("delivered", True),
        ("indeterminate", True),
        ("rejected", False),
        ("expired", False),
        ("stale", False),
        ("failed", False),
        ("proposed", False),
    ],
)
def test_only_a_nudge_that_reached_the_session_follows_it(quiet, state, follows):
    cid = ask_store.create()
    _nudge(cid, state=state)
    _run([_card()])
    assert ask_follow.state(cid)["following"] == ([S1] if follows else [])
    if follows:
        (f,) = ask_store.read_follows(cid)["follows"]
        assert f["via"] == "nudge"


def test_a_nudged_session_already_needing_you_is_a_baseline_not_an_update(quiet):
    """Unlike a started one, a nudged session may have needed someone before the nudge: the
    first look only sets the baseline; a NEW intervention after it is reported."""
    cid = ask_store.create()
    _nudge(cid)
    assert _run([_card(needs=True)]) == 0
    assert _run([_card(needs=False)]) == 0
    assert _run([_card(needs=True)]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["needs_you"]


def test_a_nudge_follows_from_its_settlement_time(quiet):
    cid = ask_store.create()
    _nudge(cid, ts=_CLOCK["now"] - 30)
    _run([_card()])
    (f,) = ask_store.read_follows(cid)["follows"]
    assert f["since"] == pytest.approx(_CLOCK["now"] - 60 - 30, abs=1e-3)  # epoch: rel is ±30 min


def test_a_replayed_nudge_never_follows_twice_and_unfollow_sticks(quiet):
    cid = ask_store.create()
    _nudge(cid)
    _run([_card()])
    assert ask_follow.unfollow(cid, S1) is True
    for _ in range(3):  # the row is still in the ledger; every pass sees it again
        _run([_card()])
    assert ask_follow.state(cid)["following"] == []


def test_compaction_admits_a_nudge_before_deleting_its_row(quiet):
    cid = ask_store.create()
    aid = _nudge(cid)
    orchestrator_ledger.compact(history_max=0)  # before any follower pass
    assert aid not in orchestrator_ledger.latest_by_id()
    assert ask_follow.state(cid)["following"] == [S1]


def test_a_failed_admission_declines_compaction_and_the_next_pass_follows(quiet, monkeypatch):
    cid = ask_store.create()
    aid = _nudge(cid)
    real = ask_store.write_follows

    def fails(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ask_store, "write_follows", fails)
    assert orchestrator_ledger.compact(history_max=0) == 0  # declined
    assert aid in orchestrator_ledger.latest_by_id()  # the row survives
    monkeypatch.setattr(ask_store, "write_follows", real)
    _run([_card()])
    assert ask_follow.state(cid)["following"] == [S1]


def test_a_deleted_conversation_makes_its_nudge_inert(quiet):
    cid = ask_store.create()
    aid = _nudge(cid)
    ask_store.delete(cid)
    assert orchestrator_ledger.compact(history_max=0) > 0 or aid not in (
        orchestrator_ledger.latest_by_id()
    )
    assert aid not in orchestrator_ledger.latest_by_id()  # compacted, not declined


def test_a_conversation_deleted_while_admission_waits_gets_no_sidecar_back(quiet, monkeypatch):
    """Retention deletes the conversation between admission's first look and its lock: the look
    UNDER the lock says gone, so no orphan follows sidecar is written back."""
    cid = ask_store.create()
    aid = _nudge(cid)
    ask_store.delete(cid)
    real, looks = ask_store.exists, []

    def stale_first_look(c: str) -> bool:
        looks.append(c)
        return len(looks) == 1 or real(c)  # the first look predates the delete

    monkeypatch.setattr(ask_store, "exists", stale_first_look)
    assert ask_follow.admit_nudge(cid, S1, title="t", action_id=aid, since=1.0) == "gone"
    assert not ask_store._follows_path(cid).exists()


def test_at_the_follow_cap_a_nudge_is_a_visible_non_follow_once(quiet):
    cid = ask_store.create()
    for i in range(ask_store.MAX_FOLLOWS):
        ask_follow.follow(cid, _key(100 + i), title=f"s{i}", via="start", reason=uuid.uuid4().hex)
    aid = _nudge(cid, key=S1)
    assert ask_follow.admit_nudge(cid, S1, title="docs pass", action_id=aid, since=1.0) == (
        "not_followed"
    )
    assert ask_follow.state(cid)["pending"] is True  # queued, not appended inside the admission
    _run([_card()] + [_card(_key(100 + i)) for i in range(ask_store.MAX_FOLLOWS)])
    rows = [u for u in _updates(cid) if u["kind"] == "not_followed"]
    assert len(rows) == 1 and rows[0]["key"] == S1 and rows[0]["title"] == "docs pass"
    _run([_card()] + [_card(_key(100 + i)) for i in range(ask_store.MAX_FOLLOWS)])
    assert len([u for u in _updates(cid) if u["kind"] == "not_followed"]) == 1  # never again


def test_the_update_budget_is_one_counter_bumped_at_queue_time(quiet):
    """Hermes on #1517: a queued entry uses ONE slot, not two."""
    cid = ask_store.create()
    for i in range(ask_store.MAX_FOLLOWS):
        ask_follow.follow(cid, _key(100 + i), title=f"s{i}", via="start", reason=uuid.uuid4().hex)
    with ask_store.follows_lock(cid):
        doc = ask_store.read_follows(cid)
        doc["updates"] = ask_store.MAX_UPDATES - 2
        ask_store.write_follows(cid, doc)
    one, two, three = (_nudge(cid, key=_key(i)) for i in (201, 202, 203))
    assert ask_follow.admit_nudge(cid, _key(201), title="a", action_id=one, since=1.0) == (
        "not_followed"
    )
    assert ask_store.read_follows(cid)["updates"] == ask_store.MAX_UPDATES - 1  # one slot
    assert ask_follow.admit_nudge(cid, _key(202), title="b", action_id=two, since=1.0) == (
        "not_followed"
    )
    assert ask_follow.admit_nudge(cid, _key(203), title="c", action_id=three, since=1.0) == (
        "exhausted"
    )
    assert ask_follow.state(cid)["exhausted"] is True
    assert three in ask_store.read_follows(cid)["consumed"]  # consumed, with no row


def test_queued_refusals_and_an_update_survive_an_interrupted_flush_once_each(quiet, monkeypatch):
    """Hermes on #1517: an ordinary update minted by a pass whose flush crashes, then two refused
    nudges admitted on top of it before any replay. Nothing is overwritten (writers append to the
    outbox), and the replay delivers each exactly once, in order."""
    cid = ask_store.create()
    ask_follow.follow(cid, S1, title="api", via="start", reason=uuid.uuid4().hex)
    for i in range(ask_store.MAX_FOLLOWS - 1):
        ask_follow.follow(cid, _key(100 + i), title=f"s{i}", via="start", reason=uuid.uuid4().hex)
    cards = [_card()] + [_card(_key(100 + i)) for i in range(ask_store.MAX_FOLLOWS - 1)]
    _run(cards, working=(S1,))
    # `context()`, never `undo()`: undo would also revert the fixtures' store and clock pins.
    with monkeypatch.context() as m:
        m.setattr(ask_store, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        _run(cards)  # mints `finished` for S1; its flush crashes
    a1, a2 = _nudge(cid, key=_key(201)), _nudge(cid, key=_key(202))
    ask_follow.admit_nudge(cid, _key(201), title="one", action_id=a1, since=1.0)
    ask_follow.admit_nudge(cid, _key(202), title="two", action_id=a2, since=1.0)
    assert [o["kind"] for o in ask_store.read_follows(cid)["outbox"]] == [
        "finished",
        "not_followed",
        "not_followed",
    ]
    _run(cards)  # the replay flushes all three
    _run(cards)
    kinds = [(u["kind"], u.get("title")) for u in _updates(cid)]
    assert kinds == [("finished", "api rebase"), ("not_followed", "one"), ("not_followed", "two")]


def test_retention_waits_for_a_follower_write_that_already_passed_its_check():
    """Hermes round 2: a follower inside the follows lock (existence checked, write pending) and a
    sweep: the sweep waits for the lock, then deletes everything — no orphan sidecar or lock."""
    import threading

    cid = ask_store.create()
    t = time.time() - (ask_store.RETENTION_DAYS + 1) * 86400
    import os as _os

    _os.utime(ask_store.root() / f"{cid}.jsonl", (t, t))
    inside, release = threading.Event(), threading.Event()
    real_read = ask_store.read_follows

    def slow_read(c):
        doc = real_read(c)
        inside.set()
        release.wait(5)
        return doc

    ask_store_read = ask_store.read_follows
    ask_store.read_follows = slow_read
    try:
        writer = threading.Thread(
            target=ask_follow.follow,
            args=(cid, S1),
            kwargs={"title": "x", "via": "start", "reason": "a" * 32},
        )
        writer.start()
        assert inside.wait(5)
        swept: list = []
        sweeper = threading.Thread(target=lambda: swept.append(ask_store.sweep()))
        sweeper.start()
        sweeper.join(0.3)
        assert sweeper.is_alive()  # it waits for the follower's lock
        release.set()
        writer.join(5)
        sweeper.join(5)
    finally:
        ask_store.read_follows = ask_store_read
    assert swept == [[cid]]
    assert not (ask_store.root() / "follows" / f"{cid}.json").exists()
    assert not ask_store._follows_lock_path(cid).exists()


def test_a_reconciled_start_counts_output_from_the_start_decision(quiet):
    """The launch's own follow failed; the pass reconciles it later. Output that came between the
    start decision and that pass is the session's first turn — it reports."""
    cid = ask_store.create()
    decided = _CLOCK["now"] - 30
    with ask_store.proposals_lock(cid):
        ask_store.write_proposals(
            cid,
            [
                {
                    "id": uuid.uuid4().hex,
                    "turn_id": str(uuid.uuid4()),
                    "kind": "session",
                    "state": "started",
                    "engine": "claude",
                    "project_id": "p1",
                    "project": "api",
                    "agent": "Claude Code",
                    "cwd": "/work/api",
                    "real": "/work/api",
                    "brief": "rebase",
                    "bypass_default": False,
                    "bypass": False,
                    "session_key": S1,
                    "decided_at": decided,
                }
            ],
        )
    _CLOCK["out"][S1] = decided + 5  # its first turn, before any pass
    assert _run([_card()]) == 1
    assert [u["kind"] for u in _updates(cid)] == ["finished"]


def test_a_not_followed_row_is_unread_until_seen(quiet):
    """A refused follow is an update like any other: it marks the conversation unread, and a seen
    mark through it clears that."""
    cid = ask_store.create()
    for i in range(ask_store.MAX_FOLLOWS):
        ask_follow.follow(cid, _key(100 + i), title=f"s{i}", via="start", reason=uuid.uuid4().hex)
    cards = [_card(_key(100 + i)) for i in range(ask_store.MAX_FOLLOWS)]
    _run(cards)
    assert ask_follow.state(cid)["unread"] is False
    one = _nudge(cid, key=_key(201))
    assert ask_follow.admit_nudge(cid, _key(201), title="a", action_id=one, since=1.0) == (
        "not_followed"
    )
    assert ask_follow.state(cid)["unread"] is True
    _run(cards)
    shown = _updates(cid)[-1]
    assert shown["kind"] == "not_followed"
    ask_follow.mark_seen(cid, shown["ts"])
    assert ask_follow.state(cid)["unread"] is False


def test_a_crashed_flush_of_a_queued_refusal_never_counts_it_again(quiet, monkeypatch):
    """The budget boundary across a crash: the refusal queued at ``MAX_UPDATES - 2`` uses one slot,
    and a flush that crashes, then its replay, leave the counter where the queueing put it."""
    cid = ask_store.create()
    for i in range(ask_store.MAX_FOLLOWS):
        ask_follow.follow(cid, _key(100 + i), title=f"s{i}", via="start", reason=uuid.uuid4().hex)
    cards = [_card(_key(100 + i)) for i in range(ask_store.MAX_FOLLOWS)]
    with ask_store.follows_lock(cid):
        doc = ask_store.read_follows(cid)
        doc["updates"] = ask_store.MAX_UPDATES - 2
        ask_store.write_follows(cid, doc)
    one = _nudge(cid, key=_key(201))
    assert ask_follow.admit_nudge(cid, _key(201), title="a", action_id=one, since=1.0) == (
        "not_followed"
    )
    with monkeypatch.context() as m:
        m.setattr(ask_store, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        _run(cards)  # the flush crashes
    assert ask_store.read_follows(cid)["updates"] == ask_store.MAX_UPDATES - 1
    _run(cards)  # the replay delivers it
    _run(cards)
    doc = ask_store.read_follows(cid)
    assert doc["updates"] == ask_store.MAX_UPDATES - 1 and doc["outbox"] == []
    assert [u["kind"] for u in _updates(cid)] == ["not_followed"]
