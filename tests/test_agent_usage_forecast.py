"""Where an agent's usage is heading at its recent pace — the dashboard's "runs out" line.

The pace is taken from the sweep's own readings of the agent's answer (kept beside the reports),
never from a transcript: the forecast is arithmetic on figures the agent already reported.
"""

from __future__ import annotations

import time

from agent_sessions import agent_usage as au

BUDGETS = {"threshold_pct": 90, "notify": True, "engines": {}}
H = 3600.0
NOW = 1_900_000_000.0


def _plan_hist(points, label="week", resets_at=NOW + 48 * H):
    return [{"t": t, "w": [[label, pct, resets_at]]} for t, pct in points]


def _plan_row(pct, label="week", resets_at=NOW + 48 * H):
    return {
        "source": au.SOURCE_PLAN,
        "windows": [{"label": label, "used_pct": pct, "resets_at": resets_at}],
    }


def test_a_pace_that_outruns_the_reset_says_when_it_runs_out():
    # 10 pp in 5 h = 2 pp/h; 30 pp left → 15 h, well before the reset 48 h out.
    hist = _plan_hist([(NOW - 5 * H, 60.0), (NOW - 2 * H, 66.0), (NOW, 70.0)])
    f = au.forecast(_plan_row(70.0), hist, NOW)
    assert f["state"] == "exhausts"
    assert f["runs_out_at"] == round(NOW + 15 * H)
    assert f["window"] == "week"


def test_a_pace_that_lasts_until_the_reset_projects_the_level_at_reset():
    # 1 pp over 10 h = 0.1 pp/h; 48 h to the reset → 20 + 4.8.
    hist = _plan_hist([(NOW - 10 * H, 19.0), (NOW, 20.0)])
    f = au.forecast(_plan_row(20.0), hist, NOW)
    assert f["state"] == "ok"
    assert f["runs_out_at"] is None
    assert f["pct_at_reset"] == 24.8


def test_too_few_readings_is_learning_never_ok():
    """One reading, or readings a few minutes apart, cannot say what a pace is — and "ok" would
    be a claim that nothing is heading for the limit."""
    assert au.forecast(_plan_row(50.0), [], NOW)["state"] == "learning"
    close = _plan_hist([(NOW - 600, 49.0), (NOW, 50.0)])
    assert au.forecast(_plan_row(50.0), close, NOW)["state"] == "learning"


def test_the_previous_period_does_not_set_the_pace():
    """Readings from before a reset (a different stated reset, or a fall in the percentage) are
    not the current period's pace."""
    old = _plan_hist([(NOW - 20 * H, 10.0), (NOW - 12 * H, 90.0)], resets_at=NOW - 11 * H)
    new = _plan_hist([(NOW - 2 * H, 1.0), (NOW, 2.0)])
    f = au.forecast(_plan_row(2.0), old + new, NOW)
    assert f["rate_per_h"] == 0.5
    # Same reset stamp, but the percentage fell: only the readings after the fall count.
    fell = _plan_hist([(NOW - 6 * H, 80.0), (NOW - 2 * H, 1.0), (NOW, 2.0)])
    assert au.forecast(_plan_row(2.0), fell, NOW)["rate_per_h"] == 0.5


def test_readings_outside_the_lookback_are_ignored():
    hist = _plan_hist([(NOW - 30 * H, 0.0), (NOW - H, 50.0), (NOW, 50.0)])
    f = au.forecast(_plan_row(50.0), hist, NOW)
    assert f["rate_per_h"] == 0.0
    assert f["state"] == "ok"


def test_the_row_reports_the_window_that_runs_out_first():
    session_reset, week_reset = NOW + 3 * H, NOW + 72 * H
    hist = [
        {"t": NOW - 2 * H, "w": [["session", 10.0, session_reset], ["week", 40.0, week_reset]]},
        {"t": NOW, "w": [["session", 20.0, session_reset], ["week", 50.0, week_reset]]},
    ]
    row = {
        "source": au.SOURCE_PLAN,
        "windows": [
            {"label": "session", "used_pct": 20.0, "resets_at": session_reset},
            {"label": "week", "used_pct": 50.0, "resets_at": week_reset},
        ],
    }
    f = au.forecast(row, hist, NOW)
    # session: 5 pp/h, 80 left → 16 h, after its reset in 3 h → ok. week: 5 pp/h → 10 h → out.
    assert f["window"] == "week"
    assert f["state"] == "exhausts"


def test_an_exhausted_window_is_out():
    assert au.forecast(_plan_row(100.0), [], NOW)["state"] == "out"


def test_tokens_forecast_only_against_an_operator_limit():
    hist = [{"t": NOW - 10 * H, "b": 1_000_000}, {"t": NOW, "b": 2_000_000}]
    row = {
        "source": au.SOURCE_TOKENS,
        "tokens": {"in": 1_900_000, "out": 100_000},
        "window_days": 7,
        "limit_tokens": 0,
    }
    assert au.forecast(row, hist, NOW) is None, "no limit, nothing to run out of"
    # 100k/h, 3M left → 30 h, within the 7-day window.
    f = au.forecast({**row, "limit_tokens": 5_000_000}, hist, NOW)
    assert f["state"] == "exhausts"
    assert f["runs_out_at"] == round(NOW + 30 * H)
    assert f["rate_per_h"] == 2.0  # percent of the limit per hour


def test_tokens_beyond_the_rolling_window_is_not_claimed():
    hist = [{"t": NOW - 10 * H, "b": 1_000_000}, {"t": NOW, "b": 1_001_000}]
    row = {
        "source": au.SOURCE_TOKENS,
        "tokens": {"in": 1_001_000, "out": 0},
        "window_days": 7,
        "limit_tokens": 100_000_000,
    }
    assert au.forecast(row, hist, NOW)["state"] == "ok"


def test_an_idle_rolling_sum_is_on_pace_not_learning():
    """opencode's count is a rolling 7-day sum: idle, it FALLS every sweep as old turns age out.
    That fall is the trend, not a reset — it must read as on pace, never as "learning" forever."""
    hist = [{"t": NOW - (4 - i) * H, "b": 2_000_000 - i * 10_000} for i in range(5)]
    row = {
        "source": au.SOURCE_TOKENS,
        "tokens": {"in": 1_960_000, "out": 0},
        "window_days": 7,
        "limit_tokens": 5_000_000,
    }
    f = au.forecast(row, hist, NOW)
    assert f["state"] == "ok"
    assert f["rate_per_h"] < 0


def test_manual_counters_have_no_forecast():
    row = {"source": au.SOURCE_MANUAL, "limit_tokens": 10, "manual_used": 5}
    assert au.forecast(row, [], NOW) is None


# --- the history the pace is read from ------------------------------------------------------


def _report(pct, at, error=None):
    return {
        "engine": "claude",
        "source": au.SOURCE_PLAN,
        "windows": [{"label": "week", "used_pct": pct, "resets_at": None}],
        "at": at,
        "error": error,
    }


def test_history_appends_only_newer_observations():
    """A failed probe keeps the previous report (same `at`); recording it again would draw a
    flat line through the outage and read as 'no usage'."""
    h = au.record_history({}, {"claude": _report(10.0, NOW - H)}, ["claude"], NOW)
    h = au.record_history(h, {"claude": _report(10.0, NOW - H, "boom")}, ["claude"], NOW)
    assert len(h["claude"]) == 1
    h = au.record_history(h, {"claude": _report(12.0, NOW)}, ["claude"], NOW)
    assert [s["w"][0][1] for s in h["claude"]] == [10.0, 12.0]


def test_history_is_bounded_by_age_and_count():
    h = {"claude": [{"t": NOW - 3 * 86400, "w": [["week", 1.0, None]]}]}
    h = au.record_history(h, {"claude": _report(2.0, NOW)}, ["claude"], NOW)
    assert len(h["claude"]) == 1
    many = {"claude": [{"t": NOW - i, "w": [["week", 1.0, None]]} for i in range(500, 0, -1)]}
    h = au.record_history(many, {}, [], NOW)
    assert len(h["claude"]) == au.HISTORY_MAX


def test_history_tolerates_a_malformed_store():
    h = au.record_history({"claude": "junk", 3: [], "codex": [None, {"t": "x"}]}, {}, [], NOW)
    assert h == {}


def test_the_sweep_records_readings_and_the_row_carries_a_forecast(tmp_path, monkeypatch):
    store = tmp_path / "usage.json"
    t0 = time.time()
    for i, pct in enumerate((40.0, 45.0, 50.0)):
        rep = au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", pct, None)],
            at=t0 - (2 - i) * H,
        )
        monkeypatch.setitem(au.REPORTERS, "claude", lambda rep=rep: rep)
        au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert len(au.load(store)["history"]["claude"]) == 3
    rows = {r["engine"]: r for r in au.snapshot(path=store, budgets=BUDGETS)}
    f = rows["claude"]["forecast"]
    assert f["state"] == "exhausts"
    assert f["rate_per_h"] == 5.0
