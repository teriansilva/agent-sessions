"""The per-agent usage endpoints and the prefs block behind them (#839)."""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import agent_usage as au
from agent_sessions import prefs, usage_loop
from agent_sessions.main import create_app


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _hdr(c, cfg):
    return {"X-CSRF-Token": _login(c, cfg), "Origin": cfg.origin}


# --- prefs ------------------------------------------------------------------------------------


def test_defaults_when_nothing_is_stored(tmp_path):
    assert prefs.get_agent_budgets(tmp_path / "prefs.json") == {
        "threshold_pct": prefs.BUDGET_THRESHOLD_DEFAULT,
        "notify": True,
        "engines": {},
    }
    assert prefs.BUDGET_THRESHOLD_DEFAULT == 90, "the issue's default"


def test_saving_one_agents_limit_does_not_erase_another(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_agent_budgets({"engines": {"opencode": {"limit_tokens": 10}}}, p)
    prefs.set_agent_budgets({"engines": {"kimi": {"limit_tokens": 20}}}, p)
    engines = prefs.get_agent_budgets(p)["engines"]
    assert engines == {"opencode": {"limit_tokens": 10}, "kimi": {"limit_tokens": 20}}


def test_saving_one_field_does_not_erase_the_other_on_the_same_agent(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_agent_budgets({"engines": {"kimi": {"limit_tokens": 100}}}, p)
    prefs.set_agent_budgets({"engines": {"kimi": {"manual_used": 40}}}, p)
    assert prefs.get_agent_budgets(p)["engines"]["kimi"] == {
        "limit_tokens": 100,
        "manual_used": 40,
    }


def test_zero_clears_a_limit(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_agent_budgets({"engines": {"kimi": {"limit_tokens": 100}}}, p)
    prefs.set_agent_budgets({"engines": {"kimi": {"limit_tokens": 0}}}, p)
    assert prefs.get_agent_budgets(p)["engines"] == {}


def test_a_write_preserves_unrelated_prefs(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_agent_budgets({"threshold_pct": 75}, p)
    assert prefs.get_theme(p) == "dark"


def test_a_hand_edited_file_cannot_poison_the_numbers(tmp_path):
    """A count that is `inf`, a 400-digit integer, `true`, or a string must not reach a
    division — an authenticated GET returning 500 because prefs.json was edited by hand is a
    denial of service on the whole panel."""
    p = tmp_path / "prefs.json"
    p.write_text(
        json.dumps(
            {
                "agent_budgets": {
                    "threshold_pct": 10**400,
                    "notify": "yes",
                    "engines": {
                        "kimi": {"limit_tokens": 10**400, "manual_used": True},
                        "opencode": {"limit_tokens": "lots"},
                        "codex": {"limit_tokens": float("inf")},
                        "gemini": "not even an object",
                    },
                }
            }
        )
    )
    cfg = prefs.get_agent_budgets(p)
    assert cfg["threshold_pct"] == prefs.BUDGET_THRESHOLD_DEFAULT
    assert cfg["notify"] is True
    assert cfg["engines"] == {}
    # And the rows built from it are finite and serialisable.
    rows = au.build_rows({}, cfg, time.time())
    json.dumps(rows)
    assert all(r["used_pct"] is None for r in rows)


def test_validation_rejects_what_coercion_would_have_swallowed():
    v = prefs.validate_agent_budgets_patch
    assert v({"threshold_pct": 0})
    assert v({"threshold_pct": 101})
    assert v({"threshold_pct": True})
    assert v({"notify": "yes"})
    assert v({"engines": {"kimi": {"limit_tokens": -1}}})
    assert v({"engines": {"kimi": {"limit_tokens": 1.5}}})
    assert v({"engines": {"kimi": {"bogus": 1}}})
    assert v({"typo_pct": 90}), "an unknown field must not silently no-op"
    assert (
        v({"threshold_pct": 90, "notify": False, "engines": {"kimi": {"limit_tokens": 5}}}) is None
    )


# --- routes -----------------------------------------------------------------------------------


def test_get_serves_a_row_per_agent_without_probing(auth_cfg, monkeypatch):
    called = []
    monkeypatch.setitem(au.REPORTERS, "claude", lambda: called.append(1))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/agents/usage")
    assert r.status_code == 200
    d = r.json()
    assert [a["engine"] for a in d["agents"]] == list(au.ENGINES)
    assert d["budgets"]["threshold_pct"] == 90
    # Opening Settings must not spawn six CLIs.
    assert called == []


def test_get_requires_a_session(auth_cfg):
    r = _client(auth_cfg).get("/api/agents/usage")
    assert r.status_code == 401


def test_patch_saves_and_returns_the_recomputed_rows(auth_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    (tmp_path / "usage.json").write_text(
        json.dumps(
            {
                "reports": {
                    "opencode": {
                        "engine": "opencode",
                        "source": "tokens",
                        "at": time.time(),
                        "tokens": {"in": 900_000, "out": 100_000, "cache_read": 9_000_000},
                        "window_days": 7,
                    }
                }
            }
        )
    )
    c = _client(auth_cfg)
    h = _hdr(c, auth_cfg)
    r = c.patch(
        "/api/agents/budgets",
        json={"engines": {"opencode": {"limit_tokens": 2_000_000}}},
        headers=h,
    )
    assert r.status_code == 200
    rows = {a["engine"]: a for a in r.json()["agents"]}
    # 1.0M billable against a 2M limit — cache reads excluded, as server-side.
    assert rows["opencode"]["used_pct"] == 50.0


def test_patch_rejects_an_invalid_budget_with_the_reason(auth_cfg):
    c = _client(auth_cfg)
    h = _hdr(c, auth_cfg)
    r = c.patch("/api/agents/budgets", json={"threshold_pct": 0}, headers=h)
    assert r.status_code == 422
    assert "threshold_pct" in r.json()["detail"]


def test_patch_needs_the_csrf_token(auth_cfg):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.patch(
        "/api/agents/budgets",
        json={"threshold_pct": 50},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_refresh_asks_the_agents(auth_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 12.0, None)],
                at=time.time(),
            )
        },
    )
    c = _client(auth_cfg)
    h = _hdr(c, auth_cfg)
    r = c.post("/api/agents/usage/refresh", headers=h)
    assert r.status_code == 200
    rows = {a["engine"]: a for a in r.json()["agents"]}
    assert rows["claude"]["used_pct"] == 12.0


def test_refresh_while_one_is_running_is_a_409_not_a_second_fanout(auth_cfg, monkeypatch):
    monkeypatch.setattr(usage_loop, "refresh_once", lambda: {"skipped": "busy"})
    c = _client(auth_cfg)
    h = _hdr(c, auth_cfg)
    r = c.post("/api/agents/usage/refresh", headers=h)
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


def test_a_sweep_that_starts_in_the_gap_still_gets_a_409(auth_cfg, monkeypatch):
    """The route must not pre-check `is_running()` and then trust it.

    Another sweep can take the lock between the check and the worker starting. The route would
    then report **200, refreshed** for figures it never refreshed — the worst kind of wrong,
    because the operator has no way to tell. Only `refresh_once` holds the lock that settles
    this, so only its answer may decide the status.
    """
    # Free at the moment a pre-check would run...
    monkeypatch.setattr(usage_loop, "is_running", lambda: False)
    # ...and taken by the time the work is actually attempted.
    monkeypatch.setattr(usage_loop, "refresh_once", lambda: {"skipped": "busy"})
    c = _client(auth_cfg)
    r = c.post("/api/agents/usage/refresh", headers=_hdr(c, auth_cfg))
    assert r.status_code == 409


def test_refresh_needs_the_csrf_token(auth_cfg):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/agents/usage/refresh", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


# --- the loop ---------------------------------------------------------------------------------


def test_the_loop_is_single_flight(monkeypatch, tmp_path):
    """The sweep spawns CLIs. Two of them overlapping doubles that for nothing, and the manual
    Refresh button shares the flag so a click during a sweep cannot fan out a second one."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    inner = {}

    def reporter():
        inner["nested"] = usage_loop.refresh_once()
        return au.Report(engine="claude", source=au.SOURCE_PLAN, at=time.time())

    monkeypatch.setattr(au, "REPORTERS", {"claude": reporter})
    usage_loop.refresh_once()
    assert inner["nested"] == {"skipped": "busy"}
    # And the flag is released afterwards, so the next sweep is not locked out forever.
    assert usage_loop.is_running() is False


def test_the_flag_is_released_even_when_the_sweep_explodes(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))

    def boom(**kw):
        raise RuntimeError("store on fire")

    monkeypatch.setattr(au, "refresh", boom)
    try:
        usage_loop.refresh_once()
    except RuntimeError:
        pass
    assert usage_loop.is_running() is False


def test_the_env_kill_switch_stops_the_loop(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_USAGE_LOOP", "0")
    assert usage_loop.loop_enabled() is False
    monkeypatch.setenv("AGENT_SESSIONS_USAGE_LOOP", "1")
    assert usage_loop.loop_enabled() is True
    monkeypatch.delenv("AGENT_SESSIONS_USAGE_LOOP", raising=False)
    assert usage_loop.loop_enabled() is True


def test_a_crossing_reaches_the_bell(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 97.0, 1_800_000_000)],
                at=time.time(),
            )
        },
    )
    from agent_sessions import notifications

    usage_loop.refresh_once()
    rows = notifications.listing(tmp_path / "notifications.json")["notifications"]
    assert len(rows) == 1
    assert rows[0]["engine"] == "claude"
    assert "97" in rows[0]["title"]
    # Nothing about what the agent was DOING — the bell's no-session-content rule.
    assert rows[0]["session_id"] == ""

    # A second sweep at the same figure announces nothing.
    usage_loop.refresh_once()
    assert len(notifications.listing(tmp_path / "notifications.json")["notifications"]) == 1


def test_a_crossing_is_pushed_as_well_as_belled(monkeypatch, tmp_path):
    """The bell is the record; the push is the one channel that can reach someone who is away.

    `notifications.add` alone is not the alert path — the established caller in
    `orchestrator.py` fans out too, and a budget alert that only lands in a bell nobody is
    looking at does not do the job the issue asked for.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 97.0, 1_800_000_000)],
                at=time.time(),
            )
        },
    )
    from agent_sessions import notifications

    sent = []
    monkeypatch.setattr(notifications, "fanout", lambda rec, *a, **k: sent.append(rec))
    usage_loop.refresh_once()
    assert len(sent) == 1
    assert sent[0]["engine"] == "claude"


def test_a_dead_push_service_does_not_lose_the_crossing(monkeypatch, tmp_path):
    """Fanout is best-effort. If it throws, the bell row still exists and the crossing is still
    consumed — retrying it would re-ring the bell for an alert already recorded."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 97.0, 1_800_000_000)],
                at=time.time(),
            )
        },
    )
    from agent_sessions import notifications

    def boom(*a, **k):
        raise RuntimeError("push service down")

    monkeypatch.setattr(notifications, "fanout", boom)
    out = usage_loop.refresh_once()
    assert out["delivered"], "the bell write succeeded, so the crossing is delivered"
    assert len(notifications.listing(tmp_path / "notifications.json")["notifications"]) == 1
    assert usage_loop.refresh_once()["alerts"] == []


def test_a_failed_bell_write_is_retried_on_the_next_sweep(monkeypatch, tmp_path):
    """The end-to-end half of the durability fix: nothing is marked delivered until the bell
    write succeeds, so a transient store failure costs a sweep rather than the whole alert."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 97.0, 1_800_000_000)],
                at=time.time(),
            )
        },
    )
    from agent_sessions import notifications

    real_add = notifications.add
    fail = {"on": True}

    def flaky(**kw):
        if fail["on"]:
            raise OSError("no space left on device")
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", flaky)
    assert usage_loop.refresh_once()["delivered"] == []
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []

    fail["on"] = False
    out = usage_loop.refresh_once()
    assert out["delivered"], "the crossing survived the failure and was offered again"
    assert len(notifications.listing(tmp_path / "notifications.json")["notifications"]) == 1


def test_notify_off_reaches_neither_the_bell_nor_the_push(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": lambda: au.Report(
                engine="claude",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 97.0, None)],
                at=time.time(),
            )
        },
    )
    prefs.set_agent_budgets({"notify": False})
    from agent_sessions import notifications

    usage_loop.refresh_once()
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []


def _claude_at(pct: float):
    return lambda: au.Report(
        engine="claude",
        source=au.SOURCE_PLAN,
        windows=[au.Window("week", pct, 1_800_000_000)],
        at=time.time(),
    )


def test_a_crash_after_the_bell_write_does_not_duplicate_the_alert(monkeypatch, tmp_path):
    """The other half of the durability problem, settled by the outbox.

    Committing the key *before* the bell write loses the alert on a failure; committing it
    *after* duplicates it on a crash in between. Two independent file transactions cannot be
    made atomic, so an **outbox** records the attempt first: a key still sitting there on the
    next sweep means this process died mid-delivery, and #839's contract ("nothing re-announced
    after a crash") settles the tie as *attempted counts as announced*.

    The bell cannot be the arbiter here, which is why the outbox exists: the operator can
    dismiss the row and the ring evicts at `NOTIFY_MAX`, so its ABSENCE proves nothing.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    from agent_sessions import notifications

    # Crash: the bell write lands, the commit never does.
    monkeypatch.setattr(
        au, "mark_announced", lambda *a, **k: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        usage_loop.refresh_once()
    doc = au.load(tmp_path / "usage.json")
    assert len(notifications.listing(tmp_path / "notifications.json")["notifications"]) == 1
    assert doc.get("alerted") == [], "the key never got committed"
    assert doc.get("pending"), "but the attempt is on record"

    # The operator dismisses the row they already saw — so the bell can no longer answer.
    notifications.dismiss(None, tmp_path / "notifications.json")
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []

    # Recovery: the outbox settles it, with no second bell entry and no second push.
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    pushed = []
    monkeypatch.setattr(notifications, "fanout", lambda rec, *a, **k: pushed.append(rec))
    out = usage_loop.refresh_once()
    assert out["alerts"] == [], "recovered as announced, not offered again"
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []
    assert pushed == []
    doc = au.load(tmp_path / "usage.json")
    assert doc.get("alerted"), "the crossing is settled"
    assert doc.get("pending") == []
    # And it stays settled.
    assert usage_loop.refresh_once()["alerts"] == []


def test_a_known_delivery_failure_leaves_the_outbox_clean_and_retries(monkeypatch, tmp_path):
    """The distinction the outbox turns on.

    A crossing whose delivery we can PROVE failed is retried; one whose fate is unknown (the
    process vanished) is assumed delivered. Without the first half, a transient bell-store
    failure would be recovered as "announced" and lost forever — the round-2 bug arriving by
    the other door.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    from agent_sessions import notifications

    real_add = notifications.add
    fail = {"on": True}

    def flaky(**kw):
        if fail["on"]:
            raise OSError("no space left on device")
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", flaky)
    assert usage_loop.refresh_once()["delivered"] == []
    doc = au.load(tmp_path / "usage.json")
    assert doc.get("pending") == [], "a known failure is taken back out of the outbox"
    assert doc.get("alerted") == []

    fail["on"] = False
    out = usage_loop.refresh_once()
    assert out["delivered"], "so the crossing is offered again"
    assert len(notifications.listing(tmp_path / "notifications.json")["notifications"]) == 1


def test_the_crossing_key_is_what_makes_the_alert_idempotent(monkeypatch, tmp_path):
    """The bell row carries the crossing key, which is what recovery matches on."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    from agent_sessions import notifications

    out = usage_loop.refresh_once()
    row = notifications.listing(tmp_path / "notifications.json")["notifications"][0]
    assert row["action_id"] == out["delivered"][0]
    assert row["action_id"].startswith("claude:week:")


def test_turning_alerts_off_during_a_probe_stops_the_notification(monkeypatch, tmp_path):
    """A sweep spends up to 90 s per engine inside a vendor CLI, and the operator can switch
    alerts off in that window. Deciding under a snapshot taken beforehand delivers a
    notification under a setting that has already been withdrawn."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    prefs.set_agent_budgets({"threshold_pct": 90, "notify": True})

    def reporter_that_outlives_the_setting():
        prefs.set_agent_budgets({"notify": False})
        return _claude_at(97.0)()

    monkeypatch.setattr(au, "REPORTERS", {"claude": reporter_that_outlives_the_setting})
    from agent_sessions import notifications

    out = usage_loop.refresh_once()
    assert out["delivered"] == []
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []


def test_an_unreadable_bell_announces_rather_than_staying_silent(monkeypatch, tmp_path):
    """The idempotency check fails OPEN.

    Guessing "already announced" from a read that failed would recreate the lost-alert bug the
    whole delivery path exists to prevent. An unreadable bell means we announce — and may
    duplicate — rather than go quiet, the same direction `notifications.add` fails for
    escalations.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    from agent_sessions import notifications

    real_listing = notifications.listing
    calls = {"n": 0}

    def flaky_listing(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # the idempotency probe
            raise OSError("bell unreadable")
        return real_listing(*a, **k)

    monkeypatch.setattr(notifications, "listing", flaky_listing)
    out = usage_loop.refresh_once()
    assert out["delivered"], "an unreadable bell must not silence the alert"
    assert len(real_listing(tmp_path / "notifications.json")["notifications"]) == 1


def test_alerts_switched_off_between_the_write_and_the_delivery(monkeypatch, tmp_path):
    """The settlement fence, isolated from the one inside `refresh`.

    `refresh` already decides under the policy in force at write time; this covers the window
    *after* that — the operator switches alerts off while the loop is between the store write
    and the bell write. Without the second read the notification still goes out under a setting
    that has been withdrawn.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    prefs.set_agent_budgets({"threshold_pct": 90, "notify": True})

    real_refresh = au.refresh

    def refresh_then_withdraw(**kw):
        out = real_refresh(**kw)
        prefs.set_agent_budgets({"notify": False})  # after the write, before the delivery
        return out

    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    monkeypatch.setattr(au, "refresh", refresh_then_withdraw)
    from agent_sessions import notifications

    out = usage_loop.refresh_once()
    assert out["alerts"] == [] and out["delivered"] == []
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []


def test_raising_the_threshold_after_the_sweep_withdraws_the_alert(monkeypatch, tmp_path):
    """The settlement fence has to re-evaluate ELIGIBILITY, not just the notify toggle.

    A crossing computed at 90% must not be delivered under a policy that now says 99%. Checking
    only the boolean left that case delivering under a withdrawn setting.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    prefs.set_agent_budgets({"threshold_pct": 90, "notify": True})

    real_refresh = au.refresh

    def refresh_then_raise_the_bar(**kw):
        out = real_refresh(**kw)
        prefs.set_agent_budgets({"threshold_pct": 99})  # after the write, before the delivery
        return out

    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(95.0)})
    monkeypatch.setattr(au, "refresh", refresh_then_raise_the_bar)
    from agent_sessions import notifications

    out = usage_loop.refresh_once()
    assert out["delivered"] == []
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []
    # Withdrawn, not consumed: lowering the bar again announces it properly.
    assert au.load(tmp_path / "usage.json").get("alerted") == []
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(95.0)})
    prefs.set_agent_budgets({"threshold_pct": 90})
    assert usage_loop.refresh_once()["delivered"]


def test_a_short_bell_write_does_not_consume_the_crossing(monkeypatch, tmp_path):
    """`os.write` may write FEWER bytes than it is given and just return the count.

    Unchecked, that installs a truncated document: the reader can't parse it and returns `[]`,
    so the bell silently empties while every caller was told the write succeeded — and this
    feature then marks the crossing announced and never mentions it again.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(au, "REPORTERS", {"claude": _claude_at(97.0)})
    from agent_sessions import notifications

    real_write = os.write

    def short_write(fd, data):
        # The kernel is permitted to do exactly this.
        return real_write(fd, data[: len(data) // 2]) if len(data) > 40 else real_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    usage_loop.refresh_once()
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))

    rows = notifications.listing(tmp_path / "notifications.json")["notifications"]
    assert len(rows) == 1, "the record must be complete and readable, or not claimed as written"
    assert rows[0]["engine"] == "claude"


def test_a_crash_mid_batch_does_not_silence_the_alerts_it_never_reached(monkeypatch, tmp_path):
    """The at-most-once crash trade must not be contagious.

    Claiming the whole batch up front meant dying during the FIRST delivery left every key
    pending — and recovery then committed all of them, silencing crossings that were never even
    attempted. The trade is only defensible for the crossing actually in flight.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": _claude_at(97.0),
            "codex": lambda: au.Report(
                engine="codex",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 96.0, 1_800_000_000)],
                at=time.time(),
            ),
        },
    )
    from agent_sessions import notifications

    real_add = notifications.add
    seen = {"n": 0}

    def die_on_the_first(**kw):
        seen["n"] += 1
        if seen["n"] == 1:
            raise SystemExit("process died mid-delivery")
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", die_on_the_first)
    with pytest.raises(SystemExit):
        usage_loop.refresh_once()

    doc = au.load(tmp_path / "usage.json")
    assert len(doc.get("pending") or []) == 1, "only the crossing in flight was claimed"

    # Recovery settles that ONE, and the others are still offered.
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {
            "claude": _claude_at(97.0),
            "codex": lambda: au.Report(
                engine="codex",
                source=au.SOURCE_PLAN,
                windows=[au.Window("week", 96.0, 1_800_000_000)],
                at=time.time(),
            ),
        },
    )
    out = usage_loop.refresh_once()
    engines_alerted = {a["engine"] for a in out["alerts"]}
    assert engines_alerted, "the untouched crossings survive the crash"
    rows = notifications.listing(tmp_path / "notifications.json")["notifications"]
    assert rows, "and they reach the bell"


def test_raising_a_per_engine_LIMIT_after_the_sweep_withdraws_the_alert(monkeypatch, tmp_path):
    """The percentage itself is derived from the limit, so re-checking only the threshold was
    half a fence: doubling a limit turns 95% into 47.5% without the threshold moving at all."""
    monkeypatch.setenv("AGENT_SESSIONS_AGENT_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "notifications.json"))
    prefs.set_agent_budgets(
        {
            "threshold_pct": 90,
            "notify": True,
            "engines": {"kimi": {"limit_tokens": 100, "manual_used": 95}},
        }
    )
    real_refresh = au.refresh

    def refresh_then_raise_the_limit(**kw):
        out = real_refresh(**kw)
        prefs.set_agent_budgets({"engines": {"kimi": {"limit_tokens": 200}}})
        return out

    monkeypatch.setattr(au, "REPORTERS", {})
    monkeypatch.setattr(au, "refresh", refresh_then_raise_the_limit)
    from agent_sessions import notifications

    out = usage_loop.refresh_once()
    assert out["delivered"] == [], "47.5% is under the threshold"
    assert notifications.listing(tmp_path / "notifications.json")["notifications"] == []
    assert au.load(tmp_path / "usage.json").get("alerted") == [], "withdrawn, not consumed"
