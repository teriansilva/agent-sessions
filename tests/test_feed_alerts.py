# ruff: noqa: F811 — the imported fixtures are the parameters below
"""Publisher alerts (#1730 P2): real signed cuts and statements, the thresholds, the opt-in, and
one announcement per condition."""

from __future__ import annotations

import pytest

from agent_sessions import feed_alerts, prefs
from agent_sessions.plugins import feed, storage
from test_feed_freshness import DAY, NOW, bundle, cut, gallery, keys, stmt  # noqa: F401

#: The real reader, kept before the autouse fixture swaps in an in-memory record.
_REAL_GET_ANNOUNCED = prefs.get_feed_alerts_announced
_REAL_RECORDS = (
    prefs.set_feed_alert_records,
    prefs.set_feed_alerts_pushed,
    prefs.get_feed_alerts_pushed,
    prefs.get_feed_alerts_retracting,
)


@pytest.fixture(autouse=True)
def _quick_retry_and_private_ledger(monkeypatch):
    """No 5 s retry sleeps in tests, and an in-memory announced-keys record."""
    monkeypatch.setattr(feed_alerts, "RETRY_AFTER", 0)
    ledger, pushed = [], []
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts_announced", lambda: list(ledger))
    monkeypatch.setattr(
        feed_alerts.prefs, "set_feed_alerts_announced", lambda keys: ledger.__init__(keys)
    )
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts_pushed", lambda: list(pushed))
    monkeypatch.setattr(
        feed_alerts.prefs, "set_feed_alerts_pushed", lambda keys: pushed.__init__(keys)
    )
    retracting = []
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts_retracting", lambda: list(retracting))
    monkeypatch.setattr(
        feed_alerts.prefs, "set_feed_alerts_retracting", lambda keys: retracting.__init__(keys)
    )

    def records(*, announced=None, pushed=None, retracting=None):  # one write: copy first
        new = [list(v) if v is not None else None for v in (announced, pushed, retracting)]
        for store, value in zip((ledger, pushed_, retracting_), new, strict=True):
            if value is not None:
                store.__init__(value)

    pushed_, retracting_ = pushed, retracting
    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alert_records", records)
    return ledger


def keys_of(alerts):
    prefix = "feed-alert:agents"
    return sorted(a["key"].split(":", 2)[2] for a in alerts if a["key"].startswith(prefix))


def test_nothing_held_means_nothing_to_say(keys):
    assert feed_alerts.evaluate(now=NOW) == []


def test_a_seven_day_cut_warns_two_days_ahead(keys):
    data, doc = cut(long=False)  # expires NOW + 1 day
    feed.accept(data, keys.content(data), now=NOW)
    assert keys_of(feed_alerts.evaluate(now=NOW)) == [f"{doc['sequence']}:content"]
    assert feed_alerts.evaluate(now=doc["expires_at"] - 3 * DAY) == []


def test_a_long_cut_warns_two_weeks_ahead_and_needs_a_current_statement(keys):
    data, doc = cut()  # 60-day window
    statement = stmt(data, doc, expires=NOW + 4 * DAY)
    feed.accept(data, keys.content(data), statement=bundle(keys, statement), now=NOW)
    assert feed_alerts.evaluate(now=NOW) == []  # statement good for 4 days, content for 60
    seq = doc["sequence"]
    # Statement inside its last 48 h.
    assert keys_of(feed_alerts.evaluate(now=NOW + 3 * DAY))[0].startswith(f"{seq}:statement:")
    # Statement expired: no current statement.
    assert keys_of(feed_alerts.evaluate(now=NOW + 5 * DAY)) == [f"{seq}:no-statement"]
    # Two weeks before the content ends (and still no statement).
    assert keys_of(feed_alerts.evaluate(now=doc["expires_at"] - 13 * DAY)) == [
        f"{seq}:content",
        f"{seq}:no-statement",
    ]


def test_an_expired_cut_says_so_once(keys):
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    expired = feed_alerts.evaluate(now=doc["expires_at"] + 1)
    assert keys_of(expired) == [f"{doc['sequence']}:expired"]


def test_damaged_trust_records_are_reported(keys):
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    storage.write(storage.root() / "feed-floor.json", {"damaged": True})
    assert [a["key"] for a in feed_alerts.evaluate(now=NOW)] == ["feed-alert:agents:unavailable"]


def test_the_gallery_is_watched_too(keys, gallery):
    index, accept, _clock, _trust, _contract = gallery
    raw, doc, sig = index(long=False)
    accept(raw, doc, sig)
    alerts = [a["key"] for a in feed_alerts.evaluate(now=doc["expires_at"] - DAY)]
    assert alerts == [f"feed-alert:gallery:{doc['sequence']}:content"]


def test_sweep_is_off_by_default_and_announces_each_condition_once(keys, monkeypatch):
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    bell = []
    monkeypatch.setattr(feed_alerts, "_refresh_feeds", lambda: None)
    monkeypatch.setattr(feed_alerts.notifications, "listing", lambda: {"notifications": bell})
    monkeypatch.setattr(feed_alerts.notifications, "list_subscriptions", lambda: [])

    def add(**fields):
        row = {**fields}
        bell.append(row)
        return row

    monkeypatch.setattr(feed_alerts.notifications, "add", add)
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts", lambda: False)
    assert feed_alerts.sweep(now=NOW) == [] and bell == []
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts", lambda: True)
    feed_alerts.sweep(now=NOW)
    feed_alerts.sweep(now=NOW + 60)  # the same condition again
    assert [row["action_id"] for row in bell] == [f"feed-alert:agents:{doc['sequence']}:content"]
    assert bell[0]["session_id"] == "" and bell[0]["title"].startswith("The agent catalog ends")


def test_sweep_refreshes_both_feeds_first(keys, monkeypatch):
    calls = []
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts", lambda: True)
    monkeypatch.setattr(feed_alerts, "_refresh_feeds", lambda: calls.append("refresh"))
    monkeypatch.setattr(feed_alerts, "evaluate", lambda now=None: calls.append("evaluate") or [])
    feed_alerts.sweep(now=NOW)
    assert calls == ["refresh", "evaluate"]


@pytest.mark.parametrize("value", [True, False])
def test_the_preference_round_trips(value, tmp_path):
    from agent_sessions import prefs

    path = tmp_path / "prefs.json"
    assert prefs.get_feed_alerts(path) is False
    prefs.set_feed_alerts(value, path)
    assert prefs.get_feed_alerts(path) is value


# ---- independent review of 88ef92d0 --------------------------------------------------------------


@pytest.fixture
def bell(monkeypatch):
    rows, pushed = [], []
    monkeypatch.setattr(feed_alerts, "_refresh_feeds", lambda: None)
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts", lambda: True)
    monkeypatch.setattr(feed_alerts.notifications, "listing", lambda: {"notifications": rows})
    monkeypatch.setattr(feed_alerts.notifications, "list_subscriptions", lambda: [{"id": "phone"}])
    monkeypatch.setattr(
        feed_alerts.notifications,
        "fanout",
        lambda rec: pushed.append(rec) or {"sent": 1, "pruned": 0, "failed": 0},
    )

    def add(**fields):
        rows.append(dict(fields))
        return rows[-1]

    monkeypatch.setattr(feed_alerts.notifications, "add", add)
    return rows, pushed


def test_a_dismissed_alert_is_never_pushed_again(keys, bell):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    rows.clear()  # the operator dismissed it (or the bell evicted it)
    feed_alerts.sweep(now=doc["expires_at"] + 7 * 3600)
    assert len(pushed) == 1 and rows == []


def test_the_push_is_sent_to_subscribed_devices(keys, bell):
    _rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    assert [p["action_id"] for p in pushed] == [f"feed-alert:agents:{doc['sequence']}:expired"]


def test_a_statement_capped_by_its_cuts_end_is_no_stall(keys):
    """In a long cut's last days, a statement renewed today expires with the cut: the content
    alert speaks; no 'renewal has stalled'."""
    data, doc = cut()
    end = doc["expires_at"]
    capped = stmt(data, doc, issued=end - 3 * DAY, expires=end)
    feed.accept(data, keys.content(data), statement=bundle(keys, capped), now=end - 3 * DAY)
    assert keys_of(feed_alerts.evaluate(now=end - DAY)) == [f"{doc['sequence']}:content"]


def test_the_thresholds_are_inclusive_at_their_exact_edges(keys):
    data, doc = cut()
    seq, end = doc["sequence"], doc["expires_at"]
    statement = stmt(data, doc, expires=NOW + 4 * DAY)
    feed.accept(data, keys.content(data), statement=bundle(keys, statement), now=NOW)
    edge = NOW + 4 * DAY - feed_alerts.STATEMENT_WARN
    assert keys_of(feed_alerts.evaluate(now=edge))[0].startswith(f"{seq}:statement:")
    assert feed_alerts.evaluate(now=edge - 1) == []
    assert f"{seq}:content" in keys_of(
        feed_alerts.evaluate(now=end - feed_alerts.LONG_CONTENT_WARN)
    )
    assert f"{seq}:content" not in keys_of(
        feed_alerts.evaluate(now=end - feed_alerts.LONG_CONTENT_WARN - 1)
    )
    assert keys_of(feed_alerts.evaluate(now=end)) == [f"{seq}:expired"]


def test_a_brief_lock_collision_is_retried_not_reported(keys, monkeypatch):
    data, cut_doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real, calls = feed.historical, []

    def busy_once():
        calls.append(1)
        if len(calls) == 1:
            raise storage.LockBusy("held by a refresh")
        return real()

    monkeypatch.setattr(feed, "historical", busy_once)
    alerts = keys_of(feed_alerts.evaluate(now=NOW + DAY // 2))  # one sweep: busy, then retried
    assert alerts == [f"{cut_doc['sequence']}:content"] and len(calls) == 2


def test_an_unsafe_trust_file_is_reported_and_the_gallery_still_checked(keys, gallery, monkeypatch):
    from agent_sessions.plugins.provenance import ProvenanceError

    index, accept, _clock, _trust, _contract = gallery
    raw, doc, sig = index(long=False)
    accept(raw, doc, sig)

    def unsafe():
        raise ProvenanceError("feed.json is a symlink")

    monkeypatch.setattr(feed, "historical", unsafe)
    alerts = [a["key"] for a in feed_alerts.evaluate(now=doc["expires_at"] - DAY)]
    assert alerts == [
        "feed-alert:agents:unavailable",
        f"feed-alert:gallery:{doc['sequence']}:content",
    ]


def test_only_a_real_true_enables_alerts(tmp_path):
    from agent_sessions import prefs

    path = tmp_path / "prefs.json"
    path.write_text('{"feed_publisher_alerts": "yes"}')
    assert prefs.get_feed_alerts(path) is False


# ---- independent review, round 2 --------------------------------------------------------------


def test_two_sweeps_never_overlap(keys, bell, monkeypatch):
    import threading
    import time as clock

    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    monkeypatch.setattr(feed_alerts, "_refresh_feeds", lambda: clock.sleep(0.3))
    slow_add = feed_alerts.notifications.add

    def slow(**fields):  # a real store write takes time: both sweeps pass the checks first
        clock.sleep(0.3)
        return slow_add(**fields)

    monkeypatch.setattr(feed_alerts.notifications, "add", slow)
    threads = [
        threading.Thread(target=feed_alerts.sweep, kwargs={"now": doc["expires_at"] + 1})
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rows) == 1 and len(pushed) == 1


def test_a_cleared_condition_is_announced_again_next_time(keys, bell, monkeypatch):
    """Once per episode: an outage that recovered and recurs weeks later is announced again,
    while a dismissed alert that is still due stays quiet."""
    rows, pushed = bell
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)
    rows.clear()
    feed_alerts.sweep(now=NOW + 120)  # still damaged, dismissed: quiet
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 180)  # recovered: the key is forgotten
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 240)  # a new outage
    outages = [p for p in pushed if p["action_id"] == "feed-alert:agents:unavailable"]
    assert len(outages) == 2


def test_a_failed_record_write_never_loses_the_push(keys, bell, monkeypatch):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)

    def full(_keys):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alerts_announced", full)
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    feed_alerts.sweep(now=doc["expires_at"] + 60)  # the delivery record survives the failed write
    assert len(pushed) == 1


def test_a_crash_between_the_bell_row_and_the_push_still_pushes_once(
    keys, bell, _quick_retry_and_private_ledger
):
    """Hermes 7266: a bell row is no proof of delivery. A row left by a crash (no records)
    gets its push on the next sweep, and only once."""
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    key = f"feed-alert:agents:{doc['sequence']}:expired"
    rows.append({"action_id": key, "title": "t"})  # written just before a crash
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    feed_alerts.sweep(now=doc["expires_at"] + 60)
    assert len(rows) == 1 and [p["action_id"] for p in pushed] == [key]
    assert _quick_retry_and_private_ledger == [key]


def test_the_record_is_capped(_quick_retry_and_private_ledger):
    for i in range(feed_alerts.ANNOUNCED_MAX + 5):
        feed_alerts._remember(f"feed-alert:agents:{i}:content")
    record = _quick_retry_and_private_ledger
    assert len(record) == feed_alerts.ANNOUNCED_MAX
    assert record[-1].endswith(f"{feed_alerts.ANNOUNCED_MAX + 4}:content")


def test_both_feeds_are_refreshed(monkeypatch):
    from agent_sessions import agent_catalog_refresh
    from agent_sessions.gallery import gallery_client

    called = []

    async def agents(**_):
        called.append("agents")

    async def gallery():
        called.append("gallery")

    monkeypatch.setattr(agent_catalog_refresh, "refresh", agents)
    monkeypatch.setattr(gallery_client, "refresh", gallery)
    feed_alerts._refresh_feeds()
    assert called == ["agents", "gallery"]


def test_a_damaged_statement_floor_doesnt_trouble_a_short_cut(keys):
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    storage.write(storage.root() / f"{feed.STATEMENT}-floor.json", {"damaged": True})
    assert not any(a["key"].endswith(":unavailable") for a in feed_alerts.evaluate(now=NOW))


def test_the_record_drops_non_strings(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text('{"feed_alerts_announced": ["a", 3, null, "b", {"x": 1}]}')
    assert _REAL_GET_ANNOUNCED(path) == ["a", "b"]


# ---- independent review, round 3 --------------------------------------------------------------


def test_an_unreadable_feed_keeps_its_remembered_alerts(keys, bell, monkeypatch):
    """A read that fails is unknown, not cleared: a dismissed alert isn't re-pushed after it."""
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    after = doc["expires_at"] + 1
    feed_alerts.sweep(now=after)  # expired: pushed
    rows.clear()  # dismissed
    real = feed.historical

    def busy():
        raise storage.LockBusy("held")

    monkeypatch.setattr(feed, "historical", busy)
    feed_alerts.sweep(now=after + 60)  # unreadable twice: unavailable, pushed
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=after + 120)  # readable again: the dismissed expiry stays quiet
    assert [p["action_id"].rsplit(":", 1)[-1] for p in pushed] == ["expired", "unavailable"]


def test_a_recurrence_is_announced_even_while_the_old_row_is_in_the_bell(keys, bell, monkeypatch):
    """The cleared condition's bell row is retracted when its key is forgotten, so the next
    episode pushes even if the operator never dismissed the first."""
    rows, pushed = bell
    retracted = []

    def dismiss(key):
        retracted.append(key)
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)  # recovered, not dismissed: the row is retracted
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 180)
    outages = [p for p in pushed if p["action_id"] == "feed-alert:agents:unavailable"]
    assert len(outages) == 2 and retracted == ["feed-alert:agents:unavailable"]


def test_a_failed_push_is_retried_until_it_is_delivered(keys, bell, monkeypatch):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    outcomes = [{"sent": 0, "failed": 1}, {"sent": 1, "failed": 0}]

    def flaky(rec):
        pushed.append(rec)
        return outcomes.pop(0) if outcomes else {"sent": 1, "failed": 0}

    monkeypatch.setattr(feed_alerts.notifications, "fanout", flaky)
    after = doc["expires_at"] + 1
    for i in range(4):
        feed_alerts.sweep(now=after + i * 60)
    assert len(rows) == 1 and len(pushed) == 2  # failed, then delivered; never again


def test_with_no_device_subscribed_there_is_nothing_to_retry(keys, bell, monkeypatch):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    calls = []

    def nobody(rec):
        calls.append(rec)
        return {"sent": 0, "pruned": 0, "failed": 0}

    monkeypatch.setattr(feed_alerts.notifications, "fanout", nobody)
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    feed_alerts.sweep(now=doc["expires_at"] + 60)
    assert len(rows) == 1 and len(calls) == 1 and pushed == []


# ---- independent review, round 5 --------------------------------------------------------------


def test_an_unreadable_subscription_list_is_retried_not_taken_as_nobody(keys, bell, monkeypatch):
    """fanout reads the subscriptions strictly and raises when it can't: that is a retry, never
    'delivered to nobody' (Hermes 5265)."""
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    broken = {"on": True}

    def strict(rec):
        if broken["on"]:
            raise feed_alerts.notifications.StoreUnreadable("push-subscriptions.json")
        pushed.append(rec)
        return {"sent": 1, "pruned": 0, "failed": 0}

    monkeypatch.setattr(feed_alerts.notifications, "fanout", strict)
    after = doc["expires_at"] + 1
    feed_alerts.sweep(now=after)
    broken["on"] = False  # the store is repaired
    feed_alerts.sweep(now=after + 60)
    feed_alerts.sweep(now=after + 120)
    assert len(pushed) == 1


def test_one_broken_subscription_does_not_re_push_the_healthy_ones(keys, bell, monkeypatch):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)

    def partly(rec):
        pushed.append(rec)
        return {"sent": 1, "pruned": 0, "failed": 1}  # one phone fine, one subscription broken

    monkeypatch.setattr(feed_alerts.notifications, "fanout", partly)
    for i in range(4):
        feed_alerts.sweep(now=doc["expires_at"] + 1 + i * 60)
    assert len(pushed) == 1


def test_an_unreadable_feed_keeps_only_its_own_keys(keys, gallery, bell, monkeypatch):
    """The unreadable-keep is per feed: the other feed's cleared conditions are still forgotten."""
    index, accept, _clock, _trust, _contract = gallery
    raw, gdoc, sig = index(long=False)
    accept(raw, gdoc, sig)
    after = gdoc["expires_at"] + 1
    feed_alerts.sweep(now=after)  # the gallery has expired
    gallery_key = f"feed-alert:gallery:{gdoc['sequence']}:expired"
    assert gallery_key in feed_alerts._announced()

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)  # agents unreadable
    raw2, gdoc2, sig2 = index(2, long=False)
    accept(raw2, gdoc2, sig2)  # a new gallery cut: its old expiry has cleared
    feed_alerts.sweep(now=after + 60)
    assert gallery_key not in feed_alerts._announced()


# ---- independent review, round 6: pin the round-5 fixes against the REAL bell and fan-out --------

import json as _json  # noqa: E402

from agent_sessions import notifications  # noqa: E402


def _real_push(monkeypatch, tmp_path, subs):
    """The real notifications store and fan-out (subscriptions read strictly), with only the
    network send faked."""
    from agent_sessions import webpush

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    subs_path = tmp_path / "subs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(subs_path))
    subs_path.write_text(subs if isinstance(subs, str) else _json.dumps(subs))
    monkeypatch.setattr(feed_alerts, "_refresh_feeds", lambda: None)
    monkeypatch.setattr(feed_alerts.prefs, "get_feed_alerts", lambda: True)
    sent = []
    monkeypatch.setattr(webpush, "send", lambda row, payload: sent.append(row["endpoint"]))
    return subs_path, sent


def _sub(n):
    return {
        "id": n,
        "endpoint": f"https://fcm.googleapis.com/{n}",
        "keys": {"p256dh": "x", "auth": "y"},
    }


def _expired_now(keys):
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    return doc["expires_at"] + 1


def test_a_corrupt_subscription_store_is_never_recorded_as_delivered(keys, monkeypatch, tmp_path):
    subs_path, sent = _real_push(monkeypatch, tmp_path, "{not json")
    t = _expired_now(keys)
    feed_alerts.sweep(now=t)
    assert feed_alerts._pushed() == [] and sent == []
    subs_path.write_text(_json.dumps([_sub("a")]))  # the store is repaired
    feed_alerts.sweep(now=t + 6 * 3600)
    feed_alerts.sweep(now=t + 12 * 3600)
    assert sent == ["https://fcm.googleapis.com/a"]


def test_a_failed_announced_write_never_pushes_twice(keys, monkeypatch, tmp_path):
    _, sent = _real_push(monkeypatch, tmp_path, [_sub("a")])
    t = _expired_now(keys)
    real = feed_alerts.prefs.set_feed_alerts_announced
    calls = {"n": 0}

    def flaky(keys_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real(keys_)

    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alerts_announced", flaky)
    feed_alerts.sweep(now=t)
    feed_alerts.sweep(now=t + 6 * 3600)
    assert len(sent) == 1


def test_another_producers_row_is_never_retracted(keys, monkeypatch, tmp_path):
    _real_push(monkeypatch, tmp_path, [])
    notifications.add(
        title="auto", project="", session_id="", engine="", action_id="automation:abc:1"
    )
    feed_alerts.prefs.set_feed_alerts_announced(["automation:abc:1"])
    feed_alerts.prefs.set_feed_alerts_pushed(["automation:abc:1"])
    feed_alerts.sweep(now=NOW)
    ids = {r["action_id"] for r in notifications.listing()["notifications"]}
    assert "automation:abc:1" in ids
    assert feed_alerts._announced() == [] and feed_alerts._pushed() == []


# ---- Hermes 7276 --------------------------------------------------------------------------------


def test_a_failed_push_is_retried_even_after_its_row_is_dismissed(keys, bell, monkeypatch):
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    outcomes = [{"sent": 0, "failed": 1}]

    def flaky(rec):
        if outcomes:
            return outcomes.pop(0)
        pushed.append(rec)
        return {"sent": 1, "pruned": 0, "failed": 0}

    monkeypatch.setattr(feed_alerts.notifications, "fanout", flaky)
    after = doc["expires_at"] + 1
    feed_alerts.sweep(now=after)  # the push fails
    rows.clear()  # the operator dismisses the row (or the bell evicts it)
    feed_alerts.sweep(now=after + 60)  # the push service recovered
    feed_alerts.sweep(now=after + 120)
    assert rows == [] and [p["action_id"] for p in pushed] == [
        f"feed-alert:agents:{doc['sequence']}:expired"
    ]


def test_a_failed_retraction_is_retried_so_the_next_episode_gets_a_fresh_row(
    keys, bell, monkeypatch
):
    rows, pushed = bell
    failures = {"left": 1}

    def dismiss(key):
        if failures["left"]:
            failures["left"] -= 1
            raise feed_alerts.notifications.StoreUnreadable("bell write failed")
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)  # outage 1: a row and a push
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)  # recovered: the retraction fails, it stays pending
    assert feed_alerts._retracting() == ["feed-alert:agents:unavailable"]
    feed_alerts.sweep(now=NOW + 180)  # still recovered: the retraction is retried
    assert feed_alerts._retracting() == []
    assert not [r for r in rows if r.get("action_id") == "feed-alert:agents:unavailable"]
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 240)  # outage 2: a fresh row and a push
    outages = [r for r in rows if r.get("action_id") == "feed-alert:agents:unavailable"]
    assert (
        len(outages) == 1
        and len([p for p in pushed if p["action_id"] == "feed-alert:agents:unavailable"]) == 2
    )


def test_an_episode_that_recurs_before_its_retraction_lands_is_still_pushed(
    keys, bell, monkeypatch
):
    """The delivery record belongs to one episode: once its condition clears it is forgotten at
    once, even while the bell row's retraction is still pending (independent review r7)."""
    rows, pushed = bell
    failures = {"left": 1}

    def dismiss(key):
        if failures["left"]:
            failures["left"] -= 1
            raise feed_alerts.notifications.StoreUnreadable("bell write failed")
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)  # outage 1: a row and a push
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)  # recovered: the retraction fails
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 180)  # outage 2, before any retry: it must reach the phone
    assert len([p for p in pushed if p["action_id"] == "feed-alert:agents:unavailable"]) == 2


def test_a_recurrence_before_the_retraction_retry_gets_a_fresh_unread_row(keys, bell, monkeypatch):
    """Hermes 7281: the previous episode's row (already read) is retracted, not adopted, and the
    bell gets a new row for the new episode."""
    rows, pushed = bell
    failures = {"left": 1}

    def dismiss(key):
        if failures["left"]:
            failures["left"] -= 1
            raise feed_alerts.notifications.StoreUnreadable("bell write failed")
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)  # outage 1: a row and a push
    first = next(r for r in rows if r.get("action_id") == "feed-alert:agents:unavailable")
    first["read"] = True  # the operator read it
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)  # recovered: the retraction fails
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 180)  # outage 2, before any retry
    outages = [r for r in rows if r.get("action_id") == "feed-alert:agents:unavailable"]
    assert len(outages) == 1 and outages[0] is not first and not outages[0].get("read")
    assert feed_alerts._retracting() == []


def test_while_the_stale_row_cant_be_retracted_the_recurrence_still_pushes(keys, bell, monkeypatch):
    """The bell refuses every write: no stale row is adopted, no duplicate row is added, and the
    phone is still told; the row follows once the bell recovers."""
    rows, pushed = bell
    broken_bell = {"on": False}

    def dismiss(key):
        if broken_bell["on"]:
            raise feed_alerts.notifications.StoreUnreadable("bell write failed")
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    key = "feed-alert:agents:unavailable"
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)
    first = rows[-1]
    broken_bell["on"] = True
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 180)
    assert [r for r in rows if r.get("action_id") == key] == [first]  # nothing new, nothing adopted
    assert len([p for p in pushed if p["action_id"] == key]) == 2
    assert key not in feed_alerts._announced()
    broken_bell["on"] = False
    feed_alerts.sweep(now=NOW + 240)  # the bell is back: the stale row goes, a fresh one lands
    outages = [r for r in rows if r.get("action_id") == key]
    assert len(outages) == 1 and outages[0] is not first
    assert len([p for p in pushed if p["action_id"] == key]) == 2  # the phone was already told


def test_a_failed_trim_after_the_retraction_never_silences_the_next_episode(
    keys, bell, monkeypatch
):
    """Independent review r8: the records die mid-update (a full disk) after the row is gone.
    The pending record is written first, so the recurrence still gets a row and a push."""
    rows, pushed = bell

    def dismiss(key):
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    key = "feed-alert:agents:unavailable"
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)
    real_set = feed_alerts.prefs.set_feed_alerts_retracting

    def full_disk(_keys):
        raise OSError(28, "No space left on device")

    # Cleared: the episode ends and the row is retracted, then the disk fills before the pending
    # entry is dropped. The recurrence must still get its own row and its own push.
    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alerts_retracting", full_disk)
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)
    assert not [r for r in rows if r.get("action_id") == key]
    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alerts_retracting", real_set)
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 180)  # it recurs
    assert len([r for r in rows if r.get("action_id") == key]) == 1
    assert len([p for p in pushed if p["action_id"] == key]) == 2
    assert feed_alerts._retracting() == []


def test_a_failed_episode_end_leaves_the_row_and_its_records_together(keys, bell, monkeypatch):
    """The atomic write ending the episode fails (a full disk): nothing is retracted, so there is
    never a row gone with its records left behind (a recurrence would be silent)."""
    rows, pushed = bell

    def dismiss(key):
        rows[:] = [r for r in rows if r.get("action_id") != key]
        return 1

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    data, _ = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    real = feed.historical

    def broken():
        raise feed.FeedError("damaged")

    def full_disk(**_records):
        raise OSError(28, "No space left on device")

    key = "feed-alert:agents:unavailable"
    monkeypatch.setattr(feed, "historical", broken)
    feed_alerts.sweep(now=NOW + 60)
    monkeypatch.setattr(feed_alerts.prefs, "set_feed_alert_records", full_disk)
    monkeypatch.setattr(feed, "historical", real)
    feed_alerts.sweep(now=NOW + 120)
    assert len([r for r in rows if r.get("action_id") == key]) == 1
    assert key in feed_alerts._announced() and key in feed_alerts._pushed()


def test_the_pending_retractions_are_capped(keys, bell, monkeypatch):
    def dismiss(_key):
        raise feed_alerts.notifications.StoreUnreadable("bell write failed")

    monkeypatch.setattr(feed_alerts.notifications, "dismiss_for_action", dismiss)
    stale = [f"feed-alert:agents:{n}:expired" for n in range(feed_alerts.ANNOUNCED_MAX + 30)]
    feed_alerts.prefs.set_feed_alerts_announced(stale)
    feed_alerts._forget_cleared([])
    pending = feed_alerts._retracting()
    assert len(pending) == feed_alerts.ANNOUNCED_MAX and pending[-1] == stale[-1]


def test_the_records_writer_changes_only_what_it_is_given(tmp_path):
    records, set_pushed, get_pushed, get_retracting = _REAL_RECORDS
    path = tmp_path / "prefs.json"
    set_pushed(["feed-alert:agents:1:expired"], path=path)
    records(announced=["a"], retracting=["b", 3], path=path)
    assert get_pushed(path=path) == ["feed-alert:agents:1:expired"]
    assert _REAL_GET_ANNOUNCED(path=path) == ["a"]
    assert get_retracting(path=path) == ["b"]


def test_a_bell_that_cant_take_the_row_still_lets_the_push_out(keys, bell, monkeypatch):
    """Hermes 7298: the bell write fails (a full disk); the phone is warned anyway, and the row
    lands on the next sweep without a second push."""
    rows, pushed = bell
    data, doc = cut(long=False)
    feed.accept(data, keys.content(data), now=NOW)
    key = f"feed-alert:agents:{doc['sequence']}:expired"
    real_add = feed_alerts.notifications.add

    def full_disk(**_fields):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(feed_alerts.notifications, "add", full_disk)
    feed_alerts.sweep(now=doc["expires_at"] + 1)
    assert [p["action_id"] for p in pushed] == [key] and rows == []
    assert key not in feed_alerts._announced()
    monkeypatch.setattr(feed_alerts.notifications, "add", real_add)
    feed_alerts.sweep(now=doc["expires_at"] + 7 * 3600)
    assert [r["action_id"] for r in rows] == [key]
    assert [p["action_id"] for p in pushed] == [key]  # no second push
    assert key in feed_alerts._announced()
