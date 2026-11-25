"""Mission refusal and rework notices close on devices; one push per plan edit (#2154).

A mission notification shows under its own never-reused device tag (`mission-ep:<action id>`).
Its retraction persists the device close (`closing:` for the next shown push, `closed:` for the
app and the service worker) in the same write that removes its bell row: no close-only push, and
no close without the row going. The runner's retraction reads what is OPEN, never the refusal
history, and a plan edit while approved is one notice per approval and revision.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_rework, mission_runner, missions, notifications
from agent_sessions.mission_runner import notice_key, plan_notice_key
from test_mission_runner import (  # noqa: F401 — shared isolated fixtures
    _approve,
    _archive_specialist,
    _cv_ready,
    _fresh_runner,
    _new_revision,
    _ungrade,
    host,
    ident,
    project,
)
from test_mission_steps import seam  # noqa: F401

TAG = notifications.mission_episode_tag


def _row(action_id: str) -> dict:
    rec = notifications.add(
        title="Mission step not started: t",
        project="",
        session_id="",
        engine="",
        reason="s: why",
        action_id=action_id,
        tag=TAG(action_id),
    )
    assert rec is not None
    return rec


def _open(prefix: str = "mission-") -> list[str]:
    return sorted(
        str(r.get("action_id"))
        for r in notifications.listing()["notifications"]
        if str(r.get("action_id") or "").startswith(prefix)
    )


def _pending() -> list[str]:
    return notifications._owed(
        notifications._load_doc_strict(notifications._notifications_path())[2]
    )


@pytest.fixture
def pushes(monkeypatch):
    """Every push a producer sends, as `(tag, close)`; one subscribed device that accepts all."""
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(notifications, "list_subscriptions", lambda path=None: [{"id": "d"}])

    def fanout(rec, base_url="", path=None, close=()):
        sent.append((str(rec.get("tag") or ""), list(close)))
        return {"sent": 1, "pruned": 0, "failed": 0}

    monkeypatch.setattr(notifications, "fanout", fanout)
    return sent


# ---- the store: one shared episode-close operation ---------------------------------------------


def test_the_tag_family_is_mission_episodes_only():
    assert TAG("mission-refusal:a:b") == "mission-ep:mission-refusal:a:b"
    assert TAG("mission-rework:m:e:1") == "mission-ep:mission-rework:m:e:1"
    assert TAG("needs-you:x") == "" and TAG("") == ""
    rec = notifications.add(
        title="t", project="", session_id="", engine="", action_id="x", tag="/s/claude/x"
    )
    assert "tag" not in rec, "a reused URL tag is never stored as an episode tag"


def test_a_retraction_removes_the_row_and_persists_the_device_close_in_one_write():
    _row("mission-refusal:a1:aaaa")
    assert notifications.retract_episode("mission-refusal:a1:aaaa") == 1
    assert _open() == []
    tag = TAG("mission-refusal:a1:aaaa")
    assert notifications.owed_close_tags() == [tag]  # the app and the worker close it
    assert _pending() == [tag]  # the next shown push carries it
    assert notifications.listing()["close_tags"] == [tag]
    assert notifications.retract_episode("mission-refusal:a1:aaaa") == 0  # nothing left: no-op


def test_a_row_without_an_episode_tag_owes_no_device_close():
    notifications.add(title="t", project="", session_id="", engine="", action_id="mission-x:1")
    assert notifications.retract_episode("mission-x:1") == 1
    assert notifications.owed_close_tags() == [] and _pending() == []


def test_a_newer_episode_stays_open_when_an_older_one_closes():
    _row("mission-refusal:a1:aaaa")
    _row("mission-refusal:a1:bbbb")
    notifications.retract_episode("mission-refusal:a1:aaaa")
    assert _open() == ["mission-refusal:a1:bbbb"]
    assert notifications.owed_close_tags() == [TAG("mission-refusal:a1:aaaa")]


def test_the_close_rides_on_the_next_shown_push_and_only_the_sent_batch_is_acknowledged(pushes):
    _row("mission-refusal:a1:aaaa")
    notifications.retract_episode("mission-refusal:a1:aaaa")
    newer = _row("mission-refusal:a1:bbbb")
    notifications.push(newer)
    assert pushes == [(TAG("mission-refusal:a1:bbbb"), [TAG("mission-refusal:a1:aaaa")])]
    assert _pending() == [], "the delivered close is acknowledged"
    # The closed ledger keeps answering the app and the worker after delivery.
    assert notifications.owed_close_tags() == [TAG("mission-refusal:a1:aaaa")]


def test_a_failed_device_leaves_the_close_owed(monkeypatch):
    monkeypatch.setattr(notifications, "list_subscriptions", lambda path=None: [{"id": "d"}])
    monkeypatch.setattr(
        notifications,
        "fanout",
        lambda rec, base_url="", path=None, close=(): {"sent": 0, "pruned": 0, "failed": 1},
    )
    _row("mission-refusal:a1:aaaa")
    notifications.retract_episode("mission-refusal:a1:aaaa")
    notifications.push(_row("mission-refusal:a1:bbbb"))
    assert _pending() == [TAG("mission-refusal:a1:aaaa")]


def test_a_failed_write_changes_nothing_and_a_retry_persists_the_close(monkeypatch):
    _row("mission-refusal:a1:aaaa")
    real = notifications._write
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real(*a, **k)

    monkeypatch.setattr(notifications, "_write", flaky)
    with pytest.raises(OSError):
        notifications.retract_episode("mission-refusal:a1:aaaa")
    assert _open() == ["mission-refusal:a1:aaaa"] and notifications.owed_close_tags() == []
    assert notifications.retract_episode("mission-refusal:a1:aaaa") == 1
    assert notifications.owed_close_tags() == [TAG("mission-refusal:a1:aaaa")]


def test_a_rework_round_is_marked_closed_only_after_its_close_is_persisted(monkeypatch):
    """Recovery order: a notifications write that fails leaves the round open, so the next pass
    retries the whole retraction; a retry persists the close and then retires the round."""
    key = mission_rework.notice_key("msn_x", "o1.cv", 1)
    _row(key)
    marked: list[bool] = []
    monkeypatch.setattr(
        missions, "mark_rework_notice", lambda m, e, r, closed=False: marked.append(closed)
    )
    real = notifications.retract_episode

    def unreadable(action_id, path=None, *, now=None):
        raise notifications.StoreUnreadable("not JSON")

    monkeypatch.setattr(notifications, "retract_episode", unreadable)
    with pytest.raises(notifications.StoreUnreadable):
        mission_rework._retract("msn_x", {"edge_from": "o1.cv", "round": 1})
    assert marked == [] and _open() == [key]
    monkeypatch.setattr(notifications, "retract_episode", real)
    mission_rework._retract("msn_x", {"edge_from": "o1.cv", "round": 1})
    assert marked == [True]
    assert notifications.owed_close_tags() == [TAG(key)]


@pytest.mark.parametrize("how", ["dismiss", "clear_all", "dismiss_for_action", "evict"])
def test_a_row_leaving_by_any_route_closes_on_devices(monkeypatch, how):
    """Not only the producer's retraction: the operator's dismiss or clear-all, an action's
    dismissal and the ring's eviction remove the row too, and the device close rides with it."""
    rec = _row("mission-refusal:a1:aaaa")
    other = _row("mission-refusal:a1:bbbb")
    if how == "dismiss":
        notifications.dismiss([rec["id"]])
    elif how == "clear_all":
        notifications.dismiss(None)
    elif how == "dismiss_for_action":
        notifications.dismiss_for_action("mission-refusal:a1:aaaa")
    else:
        monkeypatch.setattr(notifications, "NOTIFY_MAX", 2)
        _row("mission-refusal:a1:cccc")  # the oldest goes
    closed = set(notifications.owed_close_tags())
    assert TAG("mission-refusal:a1:aaaa") in closed
    assert (TAG(other["action_id"]) in closed) == (how == "clear_all")
    # The producer's later retraction finds nothing left and owes nothing more.
    assert notifications.retract_episode("mission-refusal:a1:aaaa") == 0


def test_a_mission_close_rides_on_a_needs_you_push_and_the_other_way_round(pushes):
    """One ledger, two producers: each one's push carries the other's owed closes, and a
    needs-you sync keeps the mission entries it does not own."""
    from agent_sessions import needs_you_notify

    _row("mission-refusal:a1:aaaa")
    notifications.retract_episode("mission-refusal:a1:aaaa")
    res = notifications.sync_needs_you({}, push=False)  # a needs-you write of the shared map
    assert TAG("mission-refusal:a1:aaaa") in res["pending"]
    ny = {"title": "A session needs you", "project": "", "tag": "needs-you:claude:x:1"}
    needs_you_notify._deliver({"opened": [ny], "pending": res["pending"]}, push=True)
    assert pushes == [("needs-you:claude:x:1", [TAG("mission-refusal:a1:aaaa")])]
    # …and a needs-you close rides on a mission push.
    p = notifications._notifications_path()
    with notifications._locked(p):
        rows, _a, eps = notifications._load_doc_strict(p)
        eps["closing:needs-you:claude:y:1"] = {"tag": "needs-you:claude:y:1", "ts": 2e9}
        notifications._write(p, rows, episodes=eps)
    notifications.push(_row("mission-refusal:a1:bbbb"))
    assert pushes[-1] == (TAG("mission-refusal:a1:bbbb"), ["needs-you:claude:y:1"])


def test_the_ledger_stays_bounded_whichever_producer_writes_it(monkeypatch):
    monkeypatch.setattr(notifications, "CLOSED_MAX", 3)
    for i in range(6):
        _row(f"mission-refusal:a1:{i:04d}")
        notifications.retract_episode(f"mission-refusal:a1:{i:04d}", now=1000.0 + i)
    assert notifications.owed_close_tags() == [
        TAG(f"mission-refusal:a1:{i:04d}") for i in (3, 4, 5)
    ]


# ---- the runner: tags, coalescing, the same step's older notices --------------------------------


@pytest.mark.anyio
async def test_a_plan_edit_while_approved_pushes_once_per_revision(seam, pushes):  # noqa: F811
    first = _approve(seam["mid"])
    _cv_ready(seam)
    _new_revision(seam)  # o1.cv and o1.cover_letter are both refused: the plan moved to r2
    await mission_runner.advance(seam["mid"], now=0.0)
    await mission_runner.advance(seam["mid"], now=10_000.0)  # a repeated pass
    mission_runner.reset_for_test()  # a restart, on the same revision
    await mission_runner.advance(seam["mid"], now=20_000.0)
    moved = [r for r in missions.refusals(seam["mid"]) if r["approval_id"] == first["id"]]
    assert {r["step"] for r in moved} >= {"o1.cv", "o1.cover_letter"}, "per-step records kept"
    assert pushes == [(TAG(plan_notice_key(first["id"], 2)), [])], pushes
    assert _open("mission-refusal:") == [plan_notice_key(first["id"], 2)]
    # A second edit: exactly one more push, under its own tag, carrying the older revision's
    # close (the latest revision is the notice that stands).
    _new_revision(seam)
    await mission_runner.advance(seam["mid"], now=30_000.0)
    assert pushes[1:] == [
        (TAG(plan_notice_key(first["id"], 3)), [TAG(plan_notice_key(first["id"], 2))])
    ], pushes
    assert _open("mission-refusal:") == [plan_notice_key(first["id"], 3)]


@pytest.mark.anyio
async def test_an_unrelated_refusal_class_still_notifies_on_its_own(seam, pushes):  # noqa: F811
    first = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    out = await mission_runner.advance(seam["mid"], now=0.0)
    assert out == {"o1.posting": "refused"}, out
    [row] = _open("mission-refusal:")
    assert row.startswith(f"mission-refusal:{first['id']}:") and not row.endswith(":r2")
    assert pushes == [(TAG(row), [])]
    seam["team"]["ctx"] = good


@pytest.mark.anyio
async def test_a_new_refusal_of_a_step_retracts_its_older_notice_and_closes_it(
    seam,  # noqa: F811
    pushes,
):
    first = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    await mission_runner.advance(seam["mid"], now=0.0)
    [older] = _open("mission-refusal:")
    seam["team"]["ctx"] = good
    _archive_specialist(seam)  # a different change refuses the same step under the same approval
    await mission_runner.advance(seam["mid"], now=100_000.0)
    [newer] = _open("mission-refusal:")
    assert newer != older and newer.startswith(f"mission-refusal:{first['id']}:")
    assert notifications.owed_close_tags() == [TAG(older)]
    # The older close rode on the newer push.
    assert pushes[-1] == (TAG(newer), [TAG(older)])


def test_a_steps_new_refusal_leaves_another_steps_notice_open(seam):  # noqa: F811
    """Same step only: a newer refusal of step A never retracts step B's standing notice."""
    first = _approve(seam["mid"])
    ch_a1, ch_a2, ch_b = ("sha256:" + c * 64 for c in "abc")
    for step, ch in (("o1.posting", ch_a1), ("o1.cv", ch_b), ("o1.posting", ch_a2)):
        missions.record_refusal(
            seam["mid"],
            approval_id=first["id"],
            step=step,
            change_digest=ch,
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
    _row(notice_key(first["id"], ch_a1))
    _row(notice_key(first["id"], ch_b))
    newest = notice_key(first["id"], ch_a2)
    _row(newest)
    assert mission_runner._retract_older(seam["mid"], first["id"], "o1.posting", newest) == 1
    assert _open("mission-refusal:") == sorted([notice_key(first["id"], ch_b), newest])


@pytest.mark.anyio
async def test_the_approvals_end_closes_its_notices_on_devices(seam, pushes):  # noqa: F811
    first = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    await mission_runner.advance(seam["mid"], now=0.0)
    [row] = _open("mission-refusal:")
    missions.revoke_approval(seam["mid"], first["id"])
    assert mission_runner.retract_sync(seam["mid"]) == 1
    assert _open("mission-refusal:") == [] and notifications.owed_close_tags() == [TAG(row)]
    seam["team"]["ctx"] = good


# ---- bounded retraction: work proportional to what is open -------------------------------------


def test_retract_sync_never_reads_the_live_approvals_retired_history(seam, monkeypatch):  # noqa: F811
    """Hermes on #2223: thousands of refusals under the approval IN FORCE whose notices are long
    gone (dismissed or replaced) — every healthy pass reads only the open notices' refusals,
    through the by-change index, never the approval's whole history."""
    first = _approve(seam["mid"])
    for i in range(2000):
        missions.record_refusal(
            seam["mid"],
            approval_id=first["id"],
            step=f"o1.s{i % 7}",
            change_digest=f"sha256:{i:064x}",
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
    for step, ch in (("o1.posting", "e" * 64), ("o1.cv", "f" * 64)):
        missions.record_refusal(
            seam["mid"],
            approval_id=first["id"],
            step=step,
            change_digest=f"sha256:{ch}",
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
        _row(notice_key(first["id"], f"sha256:{ch}"))
    fetched = {"rows": 0, "calls": 0}
    real_steps = missions.refusal_steps

    def steps(mid, approval_id, changes, **k):
        out = real_steps(mid, approval_id, changes, **k)
        fetched["rows"] += len(out)
        fetched["calls"] += 1
        return out

    monkeypatch.setattr(missions, "refusal_steps", steps)
    for _ in range(3):  # three healthy passes: two open notices, two different steps
        assert mission_runner.retract_sync(seam["mid"], step_id="o1.other") == 0
    assert fetched == {"rows": 6, "calls": 3}, fetched  # 2 per pass, never the 2,000
    # And the lookup SEEKS: the planner uses the by-change index, not a scan of the approval.
    con = missions._ready()
    try:
        plan = " ".join(
            str(tuple(r))
            for r in con.execute(
                "EXPLAIN QUERY PLAN SELECT step FROM mission_approval_refusals "
                "WHERE mission_id=? AND approval_id=? AND change_digest >= ? "
                "AND change_digest < ?",
                (seam["mid"], first["id"], "sha256:ee", "sha256:eeg"),
            )
        )
    finally:
        con.close()
    assert "mission_approval_refusals_by_change" in plan and "change_digest>" in plan, plan


def test_refusal_steps_matches_only_the_named_changes():  # noqa: F811
    """The lookup's contract: only refusals whose digest begins with a named key part; a part
    that is not hex never reaches SQL."""
    mid = missions.create_mission("x", title="refusal lookup")["id"]
    for step, ch in (
        ("o1.a", "sha256:" + "a" * 64),
        ("o1.b", "sha256:" + "ab" * 32),
        ("o1.c", "c" * 64),
    ):
        missions.record_refusal(
            mid,
            approval_id="ap",
            step=step,
            change_digest=ch,
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
    assert missions.refusal_steps(mid, "ap", ["a" * 24]) == [
        {"step": "o1.a", "change_digest": "sha256:" + "a" * 64}
    ]
    assert [r["step"] for r in missions.refusal_steps(mid, "ap", ["ab" * 12, "c" * 24])] == [
        "o1.b",
        "o1.c",
    ]
    assert missions.refusal_steps(mid, "ap", ["%", "' OR 1=1 --", ""]) == []
    assert missions.refusal_steps(mid, "other", ["a" * 24]) == []


def test_retract_sync_reads_what_is_open_not_the_refusal_history(seam, monkeypatch):  # noqa: F811
    first = _approve(seam["mid"])
    # A large RETIRED history: thousands of refusals under approvals that ended long ago, none of
    # them on the bell any more.
    for a in range(20):
        for i in range(100):
            missions.record_refusal(
                seam["mid"],
                approval_id=f"old-{a}",
                step="o1.posting",
                change_digest=f"sha256:{a:04d}{i:060d}",
                boundary="launch",
                reasons=[{"code": "grade_below_min", "text": "x"}],
            )
    # A small ACTIVE set: two open notices under the approval in force, one of them the step's.
    for step, ch in (("o1.posting", "1" * 64), ("o1.cv", "2" * 64)):
        missions.record_refusal(
            seam["mid"],
            approval_id=first["id"],
            step=step,
            change_digest=f"sha256:{ch}",
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
        _row(notice_key(first["id"], f"sha256:{ch}"))
    fetched = {"rows": 0}

    def history(*a, **k):
        raise AssertionError("retract_sync read the full refusal history")

    real_steps = missions.refusal_steps

    def steps(mid, approval_id, changes, **k):
        out = real_steps(mid, approval_id, changes, **k)
        fetched["rows"] += len(out)
        return out

    monkeypatch.setattr(missions, "refusals", history)
    monkeypatch.setattr(missions, "refusal_steps", steps)
    assert mission_runner.retract_sync(seam["mid"], step_id="o1.posting") == 1
    assert fetched["rows"] == 2, fetched  # the live approval's own two, never the 2,000 retired
    assert _open("mission-refusal:") == [notice_key(first["id"], "sha256:" + "2" * 64)]
    # Without a step: nothing under the approval in force goes, and no refusal row is read.
    fetched["rows"] = 0
    assert mission_runner.retract_sync(seam["mid"]) == 0
    assert fetched["rows"] == 0


def test_retract_sync_leaves_another_missions_notices_alone(seam):  # noqa: F811
    _approve(seam["mid"])
    _row("mission-refusal:someone-elses-approval:" + "3" * 24)
    assert mission_runner.retract_sync(seam["mid"]) == 0
    assert _open("mission-refusal:") == ["mission-refusal:someone-elses-approval:" + "3" * 24]


def test_a_late_plan_notice_never_retracts_a_newer_revisions(seam):  # noqa: F811
    first = _approve(seam["mid"])
    _row(plan_notice_key(first["id"], 3))
    _row(plan_notice_key(first["id"], 2))  # arrived late
    assert (
        mission_runner._retract_older(
            seam["mid"], first["id"], None, plan_notice_key(first["id"], 2)
        )
        == 0
    )
    assert _open("mission-refusal:") == sorted(
        [plan_notice_key(first["id"], 2), plan_notice_key(first["id"], 3)]
    )


# ---- a failed replacement cleanup is retried by the next pass (Hermes on #2223) -----------------


def _fail_once_for(monkeypatch, victim: str) -> list[str]:
    """`retract_episode` fails ONCE for `victim` (a transient store error), then works again."""
    real = notifications.retract_episode
    failed: list[str] = []

    def flaky(action_id, path=None, *, now=None):
        if action_id == victim and not failed:
            failed.append(action_id)
            raise OSError("disk full, once")
        return real(action_id, path, now=now)

    monkeypatch.setattr(notifications, "retract_episode", flaky)
    return failed


@pytest.mark.anyio
async def test_a_failed_plan_notice_replacement_is_retried_by_the_next_pass(
    seam,  # noqa: F811
    pushes,
    monkeypatch,
):
    first = _approve(seam["mid"])
    _cv_ready(seam)
    _new_revision(seam)
    await mission_runner.advance(seam["mid"], now=0.0)
    r2, r3 = plan_notice_key(first["id"], 2), plan_notice_key(first["id"], 3)
    assert _open("mission-refusal:") == [r2]
    failed = _fail_once_for(monkeypatch, r2)
    _new_revision(seam)
    await mission_runner.advance(seam["mid"], now=10_000.0)
    assert failed == [r2], "the replacement's cleanup did not run"
    assert _open("mission-refusal:") == sorted([r2, r3]), "the failure was not transient-shaped"
    assert TAG(r2) not in notifications.owed_close_tags()
    # A NORMAL later pass: nothing new is announced, yet the cleanup is retried and converges.
    await mission_runner.advance(seam["mid"], now=20_000.0)
    assert _open("mission-refusal:") == [r3]
    assert TAG(r2) in notifications.owed_close_tags()
    assert [t for t, _ in pushes].count(TAG(r3)) == 1, "the newer notice was pushed twice"


@pytest.mark.anyio
async def test_a_failed_same_step_replacement_is_retried_by_the_next_pass(
    seam,  # noqa: F811
    pushes,
    monkeypatch,
):
    first = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    await mission_runner.advance(seam["mid"], now=0.0)
    [older] = _open("mission-refusal:")
    failed = _fail_once_for(monkeypatch, older)
    seam["team"]["ctx"] = good
    _archive_specialist(seam)
    await mission_runner.advance(seam["mid"], now=100_000.0)
    assert failed == [older] and len(_open("mission-refusal:")) == 2
    newer = next(a for a in _open("mission-refusal:") if a != older)
    assert newer.startswith(f"mission-refusal:{first['id']}:")
    assert mission_runner.retract_sync(seam["mid"]) == 1  # what every pass runs
    assert _open("mission-refusal:") == [newer]
    assert TAG(older) in notifications.owed_close_tags()
    assert [t for t, _ in pushes].count(TAG(newer)) == 1


def test_the_backstop_never_leaves_a_refused_step_without_its_open_notice(seam):  # noqa: F811
    """Refused for A, then B (B's notice open, A's gone), then A AGAIN (a repeat: no new notice).
    B is the step's only open notice: it stays, though A is now the latest refusal."""
    first = _approve(seam["mid"])
    a, b = ("sha256:" + c * 64 for c in "ab")
    for ch in (a, b):
        missions.record_refusal(
            seam["mid"],
            approval_id=first["id"],
            step="o1.posting",
            change_digest=ch,
            boundary="launch",
            reasons=[{"code": "grade_below_min", "text": "x"}],
        )
    _row(notice_key(first["id"], b))
    missions.record_refusal(
        seam["mid"],
        approval_id=first["id"],
        step="o1.posting",
        change_digest=a,
        boundary="launch",
        reasons=[{"code": "grade_below_min", "text": "x"}],
        now=4e9,
    )
    _row("mission-refusal:" + first["id"] + ":" + "c" * 24)  # another step's, unrecorded: kept
    assert mission_runner.retract_sync(seam["mid"]) == 0
    assert notice_key(first["id"], b) in _open("mission-refusal:")


def test_a_v43_store_gains_the_by_change_index_and_rolls_back(tmp_path):
    """v44 is additive: a v43 store gains the index on open, rolls back to 43 by its header
    alone, and converges again on the next open."""
    import sqlite3

    db = tmp_path / "missions.sqlite3"
    missions._ready(db).close()
    con = sqlite3.connect(db)
    con.execute("DROP INDEX mission_approval_refusals_by_change")
    con.execute("PRAGMA user_version=43")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions._ready(db).close()
    con = sqlite3.connect(db)
    q = "SELECT 1 FROM sqlite_master WHERE name='mission_approval_refusals_by_change'"
    assert con.execute(q).fetchone() and missions.SCHEMA_VERSION == 44
    assert con.execute("PRAGMA user_version").fetchone()[0] == 44
    con.close()
    assert missions.rollback_schema(43, path=db) == {"from": 44, "to": 43}
    missions.reset_schema_cache_for_test()
    missions._ready(db).close()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 44
    con.close()
