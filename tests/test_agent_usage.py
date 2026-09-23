"""Per-agent usage, asked of the agent (#839).

Every parser test runs against **output captured from the real CLI on this host**
(`tests/fixtures/`), not against a hand-written approximation of it — the whole premise of this
feature is that the agents answer, so a fixture invented to match the parser would prove nothing
about whether the parser matches the agent.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_sessions import agent_usage as au

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- claude -----------------------------------------------------------------------------------


def test_claude_usage_reads_every_window_from_real_output():
    rep = au.parse_claude_usage(_fixture("claude-usage.txt"))
    assert rep.source == au.SOURCE_PLAN
    got = {w.label: w.used_pct for w in rep.windows}
    # Three distinct percentages, so a parser that latched onto the first match, the last match,
    # or a fixed line would disagree with at least one of them.
    assert got == {"session": 5.0, "week (all models)": 31.0, "week (Fable)": 0.0}


def test_claude_usage_keeps_a_window_that_states_no_reset():
    """`week (Fable): 0% used` has no `resets` clause. It is still a window."""
    rep = au.parse_claude_usage(_fixture("claude-usage.txt"))
    by_label = {w.label: w for w in rep.windows}
    assert by_label["week (Fable)"].resets_at is None
    assert by_label["session"].resets_at is not None


#: A moment just before the earliest reset clause in `claude-usage.txt` ("Aug 26, 1:10pm",
#: Europe/Bucharest). Pinned rather than read from the clock — see the test below.
_CLAUDE_FIXTURE_NOW = 1787724000.0  # 2026-08-26T09:00:00+03:00


def test_claude_usage_reset_times_are_in_the_future():
    """The reset clause carries no year (`Aug 26, 1:10pm`), so it is resolved against *now* —
    and a naive parse lands it in 1900.

    **`now` is pinned, and that is a fix rather than a convenience.** Reading the real clock made
    this a time bomb: it passed for a few weeks after the fixture was captured and then failed on
    every PR in the repo, for a reason that had nothing to do with any diff. Pinning keeps the
    assertion pointed at what it is actually about — inferring the right year for a year-less
    date — instead of at what today happens to be. The bound stays tight, because a loose one
    would stop catching the 1900 case this exists to catch.

    What the clock exposed was also a real defect, and it is fixed rather than merely pinned
    away: the parser used to roll ANY apparently-past date into the next year, so a reset printed
    yesterday came back ~365 days out and would have been shown to the operator that way. It now
    resolves to the NEAREST adjacent year — see `test_a_reset_a_day_stale_does_not_jump_a_YEAR`
    below, which pins exactly that.
    """
    now = _CLAUDE_FIXTURE_NOW
    rep = au.parse_claude_usage(_fixture("claude-usage.txt"), now=now)
    resets = [w.resets_at for w in rep.windows if w.resets_at]
    assert resets, "fixture has reset clauses"
    for r in resets:
        assert now - 86400 < r < now + 40 * 86400, r


def test_a_reset_a_day_stale_does_not_jump_a_YEAR():
    """The year is a guess, and it has to be the guess NEAREST to now.

    Pinning the test's clock stops the suite aging, but it does not touch what the operator sees.
    Rolling forward whenever the stamp looked past was written for a December reading opened in
    January, and it fired on anything more than 24h old: a reset printed `Aug 26` and read on
    Aug 27 resolved to Aug 26 of the FOLLOWING year — rendered as a limit that resets next
    summer. That is not the clock being awkward, it is a wrong answer.

    Picking the nearest candidate year handles the December case in both directions and cannot
    produce the jump.
    """
    # Read one day AFTER the printed reset: the nearest year is this one, in the recent past.
    now = time.mktime((2026, 8, 27, 12, 0, 0, 0, 0, -1))
    got = au._parse_human_reset("Aug 26, 1:10pm", now)
    assert got is not None
    assert abs(got - now) < 2 * 86400, f"a day-stale reset jumped to {got}"

    # And the case the roll-forward existed for still works: printed in December, read in January.
    jan = time.mktime((2027, 1, 3, 9, 0, 0, 0, 0, -1))
    dec = au._parse_human_reset("Dec 31, 5pm", jan)
    assert dec is not None
    assert abs(dec - jan) < 10 * 86400, f"a December reading resolved to {dec}"


def test_claude_usage_survives_output_that_says_nothing():
    rep = au.parse_claude_usage("Login required.\n")
    assert rep.windows == []
    assert rep.error


# --- antigravity ------------------------------------------------------------------------------


def test_agy_usage_parses_the_real_tab_separated_output():
    rep = au.parse_agy_usage(_fixture("agy-usage.txt"))
    assert rep.source == au.SOURCE_PLAN
    assert len(rep.windows) == 4
    # The captured account is untouched: 100% REMAINING is 0% USED. A parser that forwarded the
    # number as-is would put a fresh account at the top of the alarm list.
    assert {w.used_pct for w in rep.windows} == {0.0}


def test_agy_usage_inverts_each_row_independently():
    """The real capture is uniform (100% across the board), so it cannot show that each row is
    converted from its own number. This variant carries four distinct values."""
    text = (
        "Gemini Models\tWeekly Limit Remaining\t40%\t2026-09-01T10:29:43Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t75%\t2026-08-25T17:20:26Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t8%\t2026-09-01T12:20:26Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-08-25T17:20:26Z\n"
    )
    rep = au.parse_agy_usage(text)
    assert [w.used_pct for w in rep.windows] == [60.0, 25.0, 92.0, 0.0]
    assert rep.windows[0].label == "Gemini Models · Weekly Limit"


# --- codex ------------------------------------------------------------------------------------


def _rollout(tmp_path: Path, *lines: dict, name: str = "rollout-a.jsonl") -> Path:
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "08" / "26"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / name
    p.write_text("".join(json.dumps(rec) + "\n" for rec in lines))
    return p


def test_codex_reads_rate_limits_from_its_own_rollout(tmp_path, monkeypatch):
    payload = json.loads("{" + _fixture("codex-ratelimits.json").strip() + "}")
    _rollout(tmp_path, {"type": "event_msg", "payload": {"type": "token_count", **payload}})
    # A moment BEFORE the fixture's stated reset. Unpinned, this test was a time bomb:
    # `resets_at` is a real epoch, the reader drops windows that have already reset, and it duly
    # started failing for everyone once that moment passed. The assertion is about PARSING.
    rep = au.read_codex_rate_limits(home=tmp_path, now=1788137880 - 60)
    assert rep.source == au.SOURCE_PLAN
    assert [(w.label, w.used_pct) for w in rep.windows] == [("week", 21.0)]
    assert rep.plan == "pro"
    assert rep.windows[0].resets_at == 1788137880


def test_codex_ignores_rate_limits_nested_under_info(tmp_path):
    """`rate_limits` is a SIBLING of `payload.info`, not a child of it.

    This is the mistake the first implementation actually made, and it is invisible in
    production: the reader simply never found a quota and every codex row read "not asked yet".
    """
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"rate_limits": {"primary": {"used_percent": 99.0}}},
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert rep.windows == []


def test_codex_takes_the_last_rate_limits_in_the_file(tmp_path):
    """A rollout accumulates one `token_count` per turn; the quota is whatever the newest says."""
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 3.0, "window_minutes": 10080}},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 44.0, "window_minutes": 10080}},
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [w.used_pct for w in rep.windows] == [44.0]


def test_codex_prefers_the_newest_rollout(tmp_path):
    old = _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 300}},
            },
        },
        name="rollout-old.jsonl",
    )
    new = _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 61.0, "window_minutes": 300}},
            },
        },
        name="rollout-new.jsonl",
    )
    import os

    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [w.used_pct for w in rep.windows] == [61.0]


def test_codex_finds_rollouts_at_any_depth(tmp_path):
    """The date nesting is codex's business. A glob pinned to `*/*/*/` would fail SILENTLY if it
    changed — every codex row would read "not asked yet" forever, with nothing in a log."""
    from agent_sessions.engines import base

    flat = tmp_path / ".codex" / "sessions"
    flat.mkdir(parents=True)
    (flat / "rollout-flat.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {"primary": {"used_percent": 37.0, "window_minutes": 10080}},
                },
            }
        )
        + "\n"
    )
    assert base._codex_sessions_dir(tmp_path) == flat
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [w.used_pct for w in rep.windows] == [37.0]


def test_derive_pct_does_not_need_a_fully_populated_config():
    """A missing key here would be a 500 on an authenticated read, not a wrong number."""
    row = {"source": au.SOURCE_MANUAL, "tokens": None}
    assert au.derive_pct(row, {"limit_tokens": 1000}) == 0.0
    assert au.derive_pct(row, {}) is None


def test_codex_with_no_rollouts_reports_an_error_not_zero(tmp_path):
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert rep.windows == []
    assert rep.error
    # The distinction that matters: "nothing to read" must never render as 0% used.
    assert au.derive_pct(rep.as_dict(), {"limit_tokens": 0, "manual_used": 0}) is None


# --- opencode ---------------------------------------------------------------------------------


def _opencode_db(tmp_path: Path, rows: list[tuple[float, dict]]) -> Path:
    share = tmp_path / ".local" / "share" / "opencode"
    share.mkdir(parents=True, exist_ok=True)
    db = share / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("create table part (id text primary key, time_created integer, data text)")
    for i, (ts, data) in enumerate(rows):
        con.execute("insert into part values (?,?,?)", (f"p{i}", int(ts * 1000), json.dumps(data)))
    con.commit()
    con.close()
    return db


def _step(**tokens) -> dict:
    return {"type": "step-finish", "tokens": tokens}


def test_opencode_sums_step_finish_tokens(tmp_path):
    now = time.time()
    _opencode_db(
        tmp_path,
        [
            (
                now - 3600,
                _step(input=1000, output=10, reasoning=0, cache={"read": 500, "write": 7}),
            ),
            (
                now - 7200,
                _step(input=2000, output=20, reasoning=0, cache={"read": 600, "write": 3}),
            ),
        ],
    )
    rep = au.read_opencode_tokens(home=tmp_path, days=7)
    assert rep.source == au.SOURCE_TOKENS
    assert rep.tokens == {"in": 3000, "out": 30, "cache_read": 1100, "cache_write": 10}
    assert rep.window_days == 7


def test_opencode_counts_reasoning_as_output(tmp_path):
    """Reasoning tokens are billed as output. Dropping them undercounts an engine on a
    thinking model by most of what it actually spent."""
    now = time.time()
    _opencode_db(tmp_path, [(now - 60, _step(input=100, output=10, reasoning=900))])
    rep = au.read_opencode_tokens(home=tmp_path, days=7)
    assert rep.tokens["out"] == 910


def test_opencode_excludes_rows_outside_the_window(tmp_path):
    now = time.time()
    _opencode_db(
        tmp_path,
        [
            (now - 3600, _step(input=1000, output=10)),
            (now - 30 * 86400, _step(input=999_000, output=999)),
        ],
    )
    rep = au.read_opencode_tokens(home=tmp_path, days=7)
    assert rep.tokens["in"] == 1000


def test_opencode_ignores_parts_that_are_not_step_finish(tmp_path):
    """`part` has no `type` column — the type lives INSIDE the JSON blob.

    The SQL `like '%step-finish%'` is a *prefilter*, not the test: a text part that merely
    mentions the string passes it, and only the decoded `type` can reject it. A reader that
    trusted the prefilter would count this row's numbers as tokens the agent spent.
    """
    now = time.time()
    _opencode_db(
        tmp_path,
        [
            (now - 60, _step(input=1000, output=10)),
            (
                now - 60,
                {
                    "type": "text",
                    "text": "the tokens are recorded on the step-finish part",
                    "tokens": {"input": 500_000, "output": 500_000},
                },
            ),
        ],
    )
    rep = au.read_opencode_tokens(home=tmp_path, days=7)
    assert rep.tokens == {"in": 1000, "out": 10, "cache_read": 0, "cache_write": 0}


def test_opencode_missing_database_is_an_error_not_a_zero(tmp_path):
    rep = au.read_opencode_tokens(home=tmp_path)
    assert rep.error
    assert rep.tokens is None


# --- what a number means ------------------------------------------------------------------------


def test_billable_excludes_cache_reads():
    """Cache reads dominate the raw total (measured: 104M input against 6.4M cache-read for a
    week of opencode) and are the cheapest tokens billed. Folding them in would make a limit
    read as breached for a reason the operator cannot act on."""
    assert au.billable({"in": 100, "out": 5, "cache_read": 10_000, "cache_write": 40}) == 105


def test_plan_percent_is_the_worst_window_not_the_first():
    row = {
        "source": au.SOURCE_PLAN,
        "windows": [
            {"label": "session", "used_pct": 5.0},
            {"label": "week", "used_pct": 96.0},
        ],
    }
    # A session at 5% while the week sits at 96% is not at 5%.
    assert au.derive_pct(row, {"limit_tokens": 0, "manual_used": 0}) == 96.0


def test_token_count_without_a_limit_has_no_percentage():
    row = {"source": au.SOURCE_TOKENS, "tokens": {"in": 5_000_000, "out": 1}}
    assert au.derive_pct(row, {"limit_tokens": 0, "manual_used": 0}) is None
    assert au.derive_pct(row, {"limit_tokens": 10_000_000, "manual_used": 0}) == 50.0


def test_manual_source_counts_the_operators_number_not_the_engines():
    row = {"source": au.SOURCE_MANUAL, "tokens": {"in": 9_000_000, "out": 0}}
    assert au.derive_pct(row, {"limit_tokens": 1_000_000, "manual_used": 250_000}) == 25.0


# --- rows -----------------------------------------------------------------------------------


BUDGETS = {"threshold_pct": 90, "notify": True, "engines": {}}


def test_unreported_unconfigured_engine_is_none_not_zero():
    rows = {r["engine"]: r for r in au.build_rows({}, BUDGETS, time.time())}
    assert rows["kimi"]["source"] == au.SOURCE_NONE
    assert rows["kimi"]["used_pct"] is None


def test_configuring_a_counter_turns_an_unreported_engine_manual():
    budgets = {**BUDGETS, "engines": {"kimi": {"limit_tokens": 1000, "manual_used": 950}}}
    rows = {r["engine"]: r for r in au.build_rows({}, budgets, time.time())}
    assert rows["kimi"]["source"] == au.SOURCE_MANUAL
    assert rows["kimi"]["used_pct"] == 95.0


def test_shell_never_gets_a_usage_row():
    """`shell` is a login shell with no agent behind it (#636); it has no usage to have an
    opinion about, and a row saying "0%" would imply otherwise."""
    assert "shell" not in au.ENGINES
    assert "shell" not in {r["engine"] for r in au.build_rows({}, BUDGETS, time.time())}


def test_a_report_older_than_the_stale_window_is_labelled_stale():
    now = time.time()
    reports = {
        "claude": {
            "engine": "claude",
            "source": au.SOURCE_PLAN,
            "at": now - au.STALE_AFTER_S - 60,
            "windows": [{"label": "week", "used_pct": 40.0, "resets_at": None}],
        },
        "codex": {
            "engine": "codex",
            "source": au.SOURCE_PLAN,
            "at": now - 60,
            "windows": [{"label": "week", "used_pct": 40.0, "resets_at": None}],
        },
    }
    rows = {r["engine"]: r for r in au.build_rows(reports, BUDGETS, now)}
    assert rows["claude"]["stale"] is True
    assert rows["codex"]["stale"] is False
    # Stale is labelled, never blanked: yesterday's percentage beats an empty panel.
    assert rows["claude"]["used_pct"] == 40.0


# --- alerts ---------------------------------------------------------------------------------


def _deliver(fresh, state):
    """Simulate the delivery step between two sweeps.

    `evaluate_alerts` returns crossings to announce and the state to persist; it deliberately
    does NOT record them as announced, because nothing has reached the operator yet — that is
    `mark_announced`'s job, after the bell write. A test that threads only `state` is modelling
    "noticed" as "delivered", which is precisely the confusion that let an undelivered sibling
    window consume a failed alert.
    """
    return sorted(set(state) | {a["key"] for a in fresh})


def _row(engine="opencode", pct=0.0, limit=10_000_000, windows=None):
    return {
        "engine": engine,
        "source": au.SOURCE_TOKENS if windows is None else au.SOURCE_PLAN,
        "used_pct": pct,
        "limit_tokens": limit,
        "windows": windows or [],
    }


def test_a_crossing_announces_once_not_once_per_sweep():
    state = []
    fresh, state = au.evaluate_alerts([_row(pct=91.0)], BUDGETS, state)
    assert [a["level"] for a in fresh] == [90]
    state = _deliver(fresh, state)
    for _ in range(3):
        fresh, state = au.evaluate_alerts([_row(pct=93.0)], BUDGETS, state)
        assert fresh == []


def test_exhaustion_is_a_second_announcement_not_a_repeat_of_the_first():
    fresh, state = au.evaluate_alerts([_row(pct=91.0)], BUDGETS, [])
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row(pct=100.0)], BUDGETS, state)
    assert [a["level"] for a in fresh] == [au.EXHAUST_PCT]


def test_easing_off_exhaustion_does_not_re_announce_the_threshold():
    """The 90 → 100 → 97 regression.

    One stored "highest level reached" cannot express this: at 97 it would clear (below its own
    re-arm band), and with the state gone the 90 threshold would be armed again and announce a
    second time — even though usage never went near 90.
    """
    fresh, state = au.evaluate_alerts([_row(pct=91.0)], BUDGETS, [])
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row(pct=100.0)], BUDGETS, state)
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row(pct=97.0)], BUDGETS, state)
    assert fresh == []
    # And it stays quiet on the way back up to 99 — that level is still held.
    fresh, state = au.evaluate_alerts([_row(pct=99.0)], BUDGETS, state)
    assert fresh == []


def test_a_real_retreat_re_arms_the_threshold():
    fresh, state = au.evaluate_alerts([_row(pct=91.0)], BUDGETS, [])
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row(pct=60.0)], BUDGETS, state)
    assert fresh == []
    fresh, state = au.evaluate_alerts([_row(pct=91.0)], BUDGETS, state)
    assert [a["level"] for a in fresh] == [90]


def test_hovering_inside_the_band_does_not_re_announce():
    """opencode's total is a rolling 7-day sum, so it genuinely falls as old turns age out.
    Without hysteresis a figure oscillating around the line announces on every sweep."""
    fresh, state = au.evaluate_alerts([_row(pct=90.5)], BUDGETS, [])
    assert len(fresh) == 1
    state = _deliver(fresh, state)
    for pct in (89.0, 90.2, 88.5, 91.0):
        fresh, state = au.evaluate_alerts([_row(pct=pct)], BUDGETS, state)
        assert fresh == [], pct


def test_editing_the_limit_re_evaluates_against_the_new_shape():
    """A crossing measured against a 10M limit says nothing about the same count under 5M — and
    halving a limit creates a crossing with no drop-below-the-band transition to re-arm on."""
    fresh, state = au.evaluate_alerts([_row(pct=91.0, limit=10_000_000)], BUDGETS, [])
    assert len(fresh) == 1
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row(pct=95.0, limit=5_000_000)], BUDGETS, state)
    assert [a["level"] for a in fresh] == [90]


def test_a_plan_window_that_rolls_is_a_new_crossing():
    week1 = [{"label": "week", "used_pct": 95.0, "resets_at": 1_800_000_000}]
    week2 = [{"label": "week", "used_pct": 95.0, "resets_at": 1_800_604_800}]
    fresh, state = au.evaluate_alerts([_row("claude", 95.0, windows=week1)], BUDGETS, [])
    assert len(fresh) == 1
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts([_row("claude", 95.0, windows=week1)], BUDGETS, state)
    assert fresh == []
    fresh, state = au.evaluate_alerts([_row("claude", 95.0, windows=week2)], BUDGETS, state)
    assert len(fresh) == 1, "next week's 95% is a different crossing"


def test_a_threshold_of_100_announces_once_not_twice():
    b = {**BUDGETS, "threshold_pct": 100}
    fresh, _ = au.evaluate_alerts([_row(pct=100.0)], b, [])
    assert len(fresh) == 1


def test_an_unmeasurable_agent_never_alerts():
    rows = au.build_rows({}, BUDGETS, time.time())
    fresh, state = au.evaluate_alerts(rows, BUDGETS, [])
    assert fresh == []
    assert state == []


# --- the sweep -------------------------------------------------------------------------------


def test_a_failed_probe_keeps_the_last_good_figures(tmp_path, monkeypatch):
    store = tmp_path / "usage.json"
    good = au.Report(
        engine="claude",
        source=au.SOURCE_PLAN,
        windows=[au.Window("week", 42.0, None)],
        at=time.time(),
    )
    monkeypatch.setitem(au.REPORTERS, "claude", lambda: good)
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)

    bad = au.Report(engine="claude", source=au.SOURCE_PLAN, at=time.time() + 1, error="boom")
    monkeypatch.setitem(au.REPORTERS, "claude", lambda: bad)
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)

    stored = au.load(store)["reports"]["claude"]
    # The figures survive; the failure is recorded beside them rather than replacing them.
    assert [w["used_pct"] for w in stored["windows"]] == [42.0]
    assert stored["error"] == "boom"
    assert stored["checked_at"] > stored["at"]


def test_a_probe_that_raises_does_not_stop_the_other_engines(tmp_path, monkeypatch):
    def explode():
        raise RuntimeError("the CLI segfaulted")

    monkeypatch.setitem(au.REPORTERS, "claude", explode)
    monkeypatch.setitem(
        au.REPORTERS,
        "codex",
        lambda: au.Report(
            engine="codex",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 7.0, None)],
            at=time.time(),
        ),
    )
    store = tmp_path / "usage.json"
    au.refresh(path=store, engines=["claude", "codex"], budgets=BUDGETS)
    reports = au.load(store)["reports"]
    assert reports["claude"]["error"]
    assert [w["used_pct"] for w in reports["codex"]["windows"]] == [7.0]


def test_the_sweep_returns_the_crossings_it_found(tmp_path, monkeypatch):
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 93.0, 1_800_000_000)],
            at=time.time(),
        ),
    )
    store = tmp_path / "usage.json"
    first = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert [a["engine"] for a in first["alerts"]] == ["claude"]
    # A crossing is consumed by DELIVERY, not by being noticed — `refresh` hands it over and
    # `mark_announced` records that it landed. Once it has, the next sweep is silent.
    au.mark_announced([a["key"] for a in first["alerts"]], path=store)
    again = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert again["alerts"] == []


def test_notify_off_suppresses_the_announcement_but_not_the_measurement(tmp_path, monkeypatch):
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 93.0, None)],
            at=time.time(),
        ),
    )
    store = tmp_path / "usage.json"
    out = au.refresh(path=store, engines=["claude"], budgets={**BUDGETS, "notify": False})
    assert out["alerts"] == []
    assert au.load(store)["reports"]["claude"]["windows"]


def test_a_corrupt_store_does_not_take_the_sweep_down(tmp_path, monkeypatch):
    store = tmp_path / "usage.json"
    store.write_text("{not json at all")
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 12.0, None)],
            at=time.time(),
        ),
    )
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert [w["used_pct"] for w in au.load(store)["reports"]["claude"]["windows"]] == [12.0]


# --- the shell-free guarantee ------------------------------------------------------------------


def test_probes_run_a_literal_argv_never_a_shell(monkeypatch):
    """The launchers' load-bearing rule, extended to the probes: a binary as argv[0] and a list
    of arguments, never a command string, never `shell=`."""
    seen = {}

    class FakeProc:
        returncode = 0

        def __init__(self):
            import io

            self.stdout = io.BytesIO(b"Current session: 1% used\n")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(au.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(au.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(au.select, "select", lambda r, w, x, t: (r, [], []))
    au.probe_claude(binary="/usr/local/bin/claude")
    assert seen["argv"] == ["/usr/local/bin/claude", "--no-session-persistence", "-p", "/usage"]
    assert "shell" not in seen["kw"]
    # stdin closed: `claude -p` otherwise blocks waiting for piped input that never comes.
    assert seen["kw"]["stdin"] is au.subprocess.DEVNULL


def test_the_claude_probe_leaves_no_transcript(monkeypatch):
    """`--no-session-persistence` is the difference between a probe and ~35,000 JSONL files a
    year, each of which the scanner would then stat and open on every pass just to hide it.

    Asserted on the argv because that is where the guarantee lives — the alternative was
    accumulating artifacts and filtering them forever.
    """
    seen = {}
    monkeypatch.setattr(au, "_run", lambda argv, **kw: (seen.update(argv=argv), (0, ""))[1])
    au.probe_claude(binary="/usr/local/bin/claude")
    assert "--no-session-persistence" in seen["argv"]
    # It only works with --print, so the two travel together.
    assert "-p" in seen["argv"]


def test_a_probe_that_floods_stdout_is_killed_not_buffered(monkeypatch):
    """`capture_output=True` bounds the TIME but not the BYTES.

    The process on the other end is a vendor CLI whose output we do not control; a malfunctioning
    one can emit gigabytes inside the 90-second window and the parent buffers all of it. That is
    an availability hole in the server, not a parsing problem.
    """
    import sys as _sys

    code, out = au._run(
        [_sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"]
    )
    assert code == 125
    assert "exceeded" in out
    assert len(out) < au.MAX_PROBE_BYTES


def test_a_bounded_probe_still_returns_a_normal_answer(monkeypatch):
    """The cap must not truncate real output — every genuine answer is a few hundred bytes."""
    import sys as _sys

    code, out = au._run([_sys.executable, "-c", "print('Current session: 7% used')"])
    assert code == 0
    assert au.parse_claude_usage(out).windows[0].used_pct == 7.0


@pytest.mark.parametrize("engine", ["kimi", "gemini"])
def test_manual_only_engines_are_never_probed(engine):
    """These two have nothing to ask. Registering a probe for them would spawn a process per
    sweep to learn nothing."""
    assert engine not in au.REPORTERS
    assert engine in au.MANUAL_ONLY


# --- the episode key is per SOURCE ---------------------------------------------------------------
# Only `plan` rows have a reset time to key on. A single universal key shape containing "the
# window's reset time" would be incomplete for the other two, so the shape is asserted per source.


def test_a_plan_key_names_the_window_and_its_reset():
    row = _row(
        "claude", 95.0, windows=[{"label": "week", "used_pct": 95.0, "resets_at": 1_800_000_000}]
    )
    key = au.alert_key("claude", row, 90.0)
    assert key == "claude:week:1800000000:90"


def test_a_token_key_names_the_limit_it_was_measured_against():
    """No window, so nothing to roll — the limit is what makes one episode different from the
    next, and it has to be IN the key for an edit to end the episode."""
    assert au.alert_key("opencode", _row(pct=95.0, limit=10_000_000), 90.0) == (
        "opencode:tokens:10000000:90"
    )


def test_a_manual_key_has_the_same_shape_as_a_token_key():
    """A count the operator typed and a count the engine reported are judged the same way once a
    limit exists; only where the number came from differs, and that is not part of the episode."""
    row = _row("kimi", 95.0, limit=5_000_000)
    row["source"] = au.SOURCE_MANUAL
    assert au.alert_key("kimi", row, 90.0) == "kimi:tokens:5000000:90"


def test_a_manual_counter_reset_re_arms_the_alert():
    """The operator's own reset is a real retreat: they zero the counter at the start of a period,
    and the next crossing must announce rather than being suppressed by the last one."""
    manual = dict(_row("kimi", 95.0, limit=5_000_000), source=au.SOURCE_MANUAL)
    fresh, state = au.evaluate_alerts([manual], BUDGETS, [])
    assert len(fresh) == 1
    state = _deliver(fresh, state)
    reset = dict(manual, used_pct=0.0)
    fresh, state = au.evaluate_alerts([reset], BUDGETS, state)
    assert fresh == []
    fresh, state = au.evaluate_alerts([manual], BUDGETS, state)
    assert [a["level"] for a in fresh] == [90]


def test_a_restart_does_not_re_announce_what_it_already_announced(tmp_path, monkeypatch):
    """The held levels live in the store beside the reports, not in memory.

    An in-memory latch passes every test above and then announces every crossing again on the
    next deploy — which is when an operator is least inclined to trust the feature.

    The restart is a **real second interpreter**, not `importlib.reload`. Reloading rebinds
    `sys.modules` for the rest of the session while every module that already did
    `from . import agent_usage` keeps the old object, so the two disagree about `REPORTERS` and
    about which module a later `monkeypatch` touches — an order-dependent failure planted in
    whatever runs next. A subprocess is both a truer restart and inert.
    """
    store = tmp_path / "usage.json"
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 94.0, 1_800_000_000)],
            at=time.time(),
        ),
    )
    out = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert len(out["alerts"]) == 1
    au.mark_announced([a["key"] for a in out["alerts"]], path=store)

    src = str(Path(__file__).resolve().parents[1] / "src")
    script = f"""
import json, sys, time
sys.path.insert(0, {src!r})
from pathlib import Path
from agent_sessions import agent_usage as au
au.REPORTERS["claude"] = lambda: au.Report(
    engine="claude", source=au.SOURCE_PLAN,
    windows=[au.Window("week", 94.0, 1_800_000_000)], at=time.time())
out = au.refresh(path=Path({str(store)!r}), engines=["claude"],
                 budgets={{"threshold_pct": 90, "notify": True, "engines": {{}}}})
print(json.dumps([a["key"] for a in out["alerts"]]))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [], "a fresh process must not re-announce"


# --- delivery is committed only once it has landed ------------------------------------------------


def test_a_bell_failure_does_not_consume_the_crossing(tmp_path, monkeypatch):
    """A transient notification-store failure must not silently make the alert permanent.

    `refresh` used to persist the announced key in the same write as the reports, before
    anything reached the operator. One failed bell write then looked identical to a delivered
    one forever: the next sweep saw the key as already announced and never retried.
    """
    store = tmp_path / "usage.json"
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 95.0, 1_800_000_000)],
            at=time.time(),
        ),
    )
    out = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert len(out["alerts"]) == 1
    # The bell write failed, so nothing was marked announced.
    assert au.load(store).get("alerted") == []

    # The very next sweep offers the same crossing again.
    again = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert [a["key"] for a in again["alerts"]] == [a["key"] for a in out["alerts"]]

    # Only once it lands is it consumed.
    au.mark_announced([a["key"] for a in again["alerts"]], path=store)
    third = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert third["alerts"] == []


def test_re_arm_bookkeeping_is_still_persisted_immediately(tmp_path, monkeypatch):
    """Holding back the NEW keys must not also hold back the ones already announced.

    `refresh` writes the levels that were already delivered and drops the ones that fell below
    their band — that is what stops a held crossing re-announcing every sweep.
    """
    store = tmp_path / "usage.json"
    pct = {"v": 95.0}
    monkeypatch.setitem(
        au.REPORTERS,
        "claude",
        lambda: au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", pct["v"], 1_800_000_000)],
            at=time.time(),
        ),
    )
    out = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    au.mark_announced([a["key"] for a in out["alerts"]], path=store)
    assert au.load(store)["alerted"]

    # Still over the line: held, not re-announced.
    assert au.refresh(path=store, engines=["claude"], budgets=BUDGETS)["alerts"] == []
    assert au.load(store)["alerted"]

    # A real retreat clears it in the very same write.
    pct["v"] = 40.0
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert au.load(store)["alerted"] == []


# --- a quota reading has its own age --------------------------------------------------------------


def test_codex_reports_when_the_quota_was_MEASURED_not_when_it_was_read(tmp_path):
    """codex writes a quota only while it runs, so the newest rollout on a host can be days old.

    Stamping the read time made a day-old figure render as `stale: false`, freshly checked.
    """
    now = time.time()
    long_ago = now - 5 * 86400
    _rollout(
        tmp_path,
        {
            "timestamp": "2020-01-02T03:04:05.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": 44.0,
                        "window_minutes": 10080,
                        "resets_at": now + 86400,
                    }
                },
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [w.used_pct for w in rep.windows] == [44.0]
    # `at` is codex's own timestamp (2020), NOT now.
    assert rep.at < long_ago
    assert rep.checked_at is not None and rep.checked_at >= now
    rows = {r["engine"]: r for r in au.build_rows({"codex": rep.as_dict()}, BUDGETS, now)}
    assert rows["codex"]["stale"] is True


def test_a_quota_window_that_has_already_reset_is_not_current_usage(tmp_path):
    """An expired window's percentage belongs to a period that is over.

    Carrying it forward shows a full plan that is actually empty — and fires an alert for a
    crossing that has already reset.
    """
    now = time.time()
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": 95.0,
                        "window_minutes": 10080,
                        "resets_at": now - 86400,
                    }
                },
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert rep.windows == []
    assert "reset" in (rep.error or "")
    # And therefore nothing to alert on.
    rows = au.build_rows({"codex": rep.as_dict()}, BUDGETS, now)
    assert au.evaluate_alerts(rows, BUDGETS, [])[0] == []


def test_a_live_window_beside_an_expired_one_still_reports(tmp_path):
    """Only the dead window is dropped — a rollout carrying both must not lose the live one."""
    now = time.time()
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": 95.0,
                        "window_minutes": 10080,
                        "resets_at": now - 86400,
                    },
                    "secondary": {
                        "used_percent": 12.0,
                        "window_minutes": 300,
                        "resets_at": now + 3600,
                    },
                },
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [(w.label, w.used_pct) for w in rep.windows] == [("5h", 12.0)]


def test_a_window_with_no_stated_reset_is_kept(tmp_path):
    """Absent is not expired. A quota codex states without a reset time is still this period's."""
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 31.0, "window_minutes": 10080}},
            },
        },
    )
    assert [w.used_pct for w in au.read_codex_rate_limits(home=tmp_path).windows] == [31.0]


# --- one malformed external record must not take the panel down ---------------------------------


def test_a_four_hundred_digit_percentage_is_not_infinity():
    """`\\d+(\\.\\d+)?` matches it and `float()` turns it into `inf`, which then poisons every
    comparison it reaches — and serialises to a 500."""
    text = f"Current session: {'9' * 400}% used\nCurrent week: 12% used\n"
    rep = au.parse_claude_usage(text)
    assert [w.used_pct for w in rep.windows] == [12.0]
    json.dumps(rep.as_dict())


def test_a_codex_NaN_percentage_never_reaches_the_response(tmp_path):
    """A JSON `NaN` reaches Starlette's `JSONResponse`, which raises
    `ValueError: Out of range float values are not JSON compliant` — an authenticated GET
    turning into a 500 because another program wrote a bad record."""
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-nan.jsonl").write_text(
        '{"type":"event_msg","payload":{"type":"token_count","rate_limits":'
        '{"primary":{"used_percent":NaN,"window_minutes":10080}}}}\n'
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert rep.windows == []
    json.dumps(rep.as_dict(), allow_nan=False)


def test_an_out_of_range_reset_time_is_dropped(tmp_path):
    _rollout(
        tmp_path,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 1e309}
                },
            },
        },
    )
    rep = au.read_codex_rate_limits(home=tmp_path)
    assert [w.resets_at for w in rep.windows] == [None]
    json.dumps(rep.as_dict(), allow_nan=False)


def test_a_poisoned_stored_report_is_normalized_on_read():
    """The document on disk was written by an earlier build, or by hand. Serving it unchecked
    puts the same non-finite value straight into the response."""
    now = time.time()
    stored = {
        "claude": {
            "engine": "claude",
            "source": au.SOURCE_PLAN,
            "at": now,
            "windows": [
                {"label": "week", "used_pct": float("inf"), "resets_at": None},
                {"label": "session", "used_pct": 4.0, "resets_at": None},
            ],
        }
    }
    rows = {r["engine"]: r for r in au.build_rows(stored, BUDGETS, now)}
    assert [w["used_pct"] for w in rows["claude"]["windows"]] == [4.0]
    json.dumps(rows, allow_nan=False)


# --- an expired period must not come back through RETENTION --------------------------------------


def test_a_retained_window_that_has_since_expired_is_not_served(tmp_path, monkeypatch):
    """The reporter-side filter is not enough, and this is the gap it left.

    A reporter that finds only expired windows returns an **error-only** report — and `refresh`'s
    last-good retention then keeps the PREVIOUS document, expired windows and all. The dead
    period came straight back through the retention path, was served, and alerted.

    The stored document here is what a live window looks like once its period has rolled: exactly
    what retention would be holding.
    """
    store = tmp_path / "usage.json"
    now = time.time()
    store.write_text(
        json.dumps(
            {
                "reports": {
                    "codex": {
                        "engine": "codex",
                        "source": au.SOURCE_PLAN,
                        "at": now - 600,
                        "windows": [{"label": "week", "used_pct": 95.0, "resets_at": now - 60}],
                    }
                }
            }
        )
    )
    # The probe can only report that everything has reset, so retention keeps the figures above.
    monkeypatch.setitem(
        au.REPORTERS,
        "codex",
        lambda: au.Report(
            engine="codex",
            source=au.SOURCE_PLAN,
            at=time.time(),
            error="every rate_limits window has already reset",
        ),
    )
    out = au.refresh(path=store, engines=["codex"], budgets=BUDGETS)

    stored = au.load(store)["reports"]["codex"]
    assert stored["windows"], "retention still keeps the last good document"
    rows = {r["engine"]: r for r in au.snapshot(path=store, budgets=BUDGETS)}
    assert rows["codex"]["windows"] == [], "but an expired period is not current usage"
    assert rows["codex"]["used_pct"] is None
    assert out["alerts"] == [], "and nothing may alert on it"


def test_a_window_with_no_reset_survives_the_row_boundary():
    """Absent is not expired — the same rule the reporter applies, applied to retained data."""
    now = time.time()
    stored = {
        "claude": {
            "engine": "claude",
            "source": au.SOURCE_PLAN,
            "at": now,
            "windows": [{"label": "week (Fable)", "used_pct": 0.0, "resets_at": None}],
        }
    }
    rows = {r["engine"]: r for r in au.build_rows(stored, BUDGETS, now)}
    assert [w["label"] for w in rows["claude"]["windows"]] == ["week (Fable)"]


# --- the policy in force when the decision is made -----------------------------------------------


def test_the_threshold_is_read_after_the_probes_not_before(tmp_path, monkeypatch):
    """A sweep spends up to 90 s per engine inside a vendor CLI. A policy snapshot taken before
    that and trusted after is a decision made under a setting the operator has withdrawn."""
    from agent_sessions import prefs

    store = tmp_path / "usage.json"
    prefs.set_agent_budgets({"threshold_pct": 90})

    def slow_reporter():
        # The operator raises the bar while the probe is still running.
        prefs.set_agent_budgets({"threshold_pct": 99})
        return au.Report(
            engine="claude",
            source=au.SOURCE_PLAN,
            windows=[au.Window("week", 95.0, 1_800_000_000)],
            at=time.time(),
        )

    monkeypatch.setattr(au, "REPORTERS", {"claude": slow_reporter})
    # No `budgets` argument: the policy is whatever is in force when the decision is made.
    out = au.refresh(path=store, engines=["claude"])
    assert out["alerts"] == [], "95% is under the 99% threshold the operator just set"


def test_a_probe_timeout_kills_the_whole_process_group(monkeypatch):
    """`proc.kill()` signals ONE pid. A CLI that forks a helper (a runtime, an auth broker)
    leaves it running after we walk away, still holding CPU and sockets — the bound would be
    advertised rather than enforced."""
    import os as _os
    import sys as _sys

    marker = Path(tempfile.mkdtemp()) / "helper-alive"
    # The parent spawns a child that outlives it, then hangs. Both are in the probe's group.
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f'"import time, pathlib; p = pathlib.Path({str(marker)!r});\\n'
        f'[ (p.write_text(str(i)), time.sleep(0.2)) for i in range(200) ]"])\n'
        "time.sleep(600)\n"
    )
    monkeypatch.setattr(au, "PROBE_TIMEOUT_S", 2.0)
    code, out = au._run([_sys.executable, "-c", script])
    assert code == 124
    # Give the helper a moment to prove it is gone: its heartbeat must stop advancing.
    time.sleep(1.0)
    first = marker.read_text() if marker.exists() else ""
    time.sleep(1.0)
    second = marker.read_text() if marker.exists() else ""
    assert first == second, f"a descendant of the probe outlived it ({first} → {second})"
    assert _os.getpgid(_os.getpid()) != 0  # sanity: we did not signal our own group


def test_a_non_finite_timestamp_does_not_500_the_panel():
    """The percentages were normalized and the timestamps were not — the same
    `Out of range float values are not JSON compliant` on an authenticated GET, one field over."""
    now = time.time()
    stored = {
        "claude": {
            "engine": "claude",
            "source": au.SOURCE_PLAN,
            "at": float("nan"),
            "checked_at": float("inf"),
            "windows": [{"label": "week", "used_pct": 40.0, "resets_at": None}],
        }
    }
    rows = au.build_rows(stored, BUDGETS, now)
    json.dumps(rows, allow_nan=False)
    assert rows[0]["at"] == 0.0
    assert rows[0]["checked_at"] is None
    # And a report with no usable timestamp is "not asked yet", never "fresh".
    assert rows[0]["stale"] is False


def test_the_probes_honour_the_pinned_binary_not_PATH(monkeypatch):
    """`AGENT_SESSIONS_CLAUDE_BIN` / `AGENT_SESSIONS_AGY_BIN` decide which binary this host runs.

    An operator who pins the launcher away from a stale or untrusted PATH entry must not find
    the probe executing that entry every 15 minutes instead — `shutil.which` in here would
    quietly reintroduce exactly the binary they excluded.
    """
    from agent_sessions.engines import base

    seen = {}
    monkeypatch.setattr(au, "_run", lambda argv, **kw: (seen.update(argv=argv), (0, ""))[1])
    monkeypatch.setattr(base, "CLAUDE_BIN", "/opt/pinned/claude")
    monkeypatch.setattr(base, "AGY_BIN", "/opt/pinned/agy")
    # `agent_usage` no longer imports `shutil` at all — that absence IS the fix, and the two
    # constants above are the only source of a probe's binary.
    assert not hasattr(au, "shutil"), "a PATH lookup here would bypass the pin"

    au.probe_claude()
    assert seen["argv"][0] == "/opt/pinned/claude"
    au.probe_agy()
    assert seen["argv"][0] == "/opt/pinned/agy"


def test_a_stored_token_payload_cannot_500_the_panel():
    """`_int` sanitised the arithmetic and left the raw dict to be serialised, so a stored
    non-finite `in`/cache produced a perfectly finite percentage and still raised on the way
    out. The response row is normalized, not only the number derived from it."""
    now = time.time()
    stored = {
        "opencode": {
            "engine": "opencode",
            "source": au.SOURCE_TOKENS,
            "at": now,
            "window_days": 7,
            "tokens": {
                "in": float("inf"),
                "out": 5,
                "cache_read": float("nan"),
                "cache_write": 10**40,
            },
        }
    }
    budgets = {**BUDGETS, "engines": {"opencode": {"limit_tokens": 100}}}
    rows = {r["engine"]: r for r in au.build_rows(stored, budgets, now)}
    json.dumps(rows, allow_nan=False)
    assert rows["opencode"]["tokens"] == {"in": 0, "out": 5, "cache_read": 0, "cache_write": 0}


def test_a_probe_descendant_does_not_outlive_a_SUCCESSFUL_probe(tmp_path):
    """`_kill` reaps the group on timeout and overflow; the normal path just returned.

    A probe that forks a helper, detaches its stdio and exits 0 left that helper running — the
    parent's clean exit says nothing about its descendants, so the bound held only for probes
    that misbehave in the ways already handled.
    """
    import os as _os
    import sys as _sys

    pidfile = tmp_path / "helper.pid"
    # The helper PUBLISHES its pid atomically: write a sibling temporary, close it, rename it
    # into place. `Path.write_text` creates the file before writing it, so a parent polling for
    # existence could see an EMPTY pidfile and exit, the helper was reaped, and `int('')` failed
    # (Hermes on #1114). The 0.25 s pause between creating the temporary and publishing it is
    # that create-before-write window, exercised on every run: the parent must not proceed
    # through it.
    child = (
        "import os, time\n"
        f"tmp = {str(pidfile) + '.tmp'!r}\n"
        "fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "time.sleep(0.25)\n"
        "os.write(fd, str(os.getpid()).encode())\n"
        "os.close(fd)\n"
        f"os.replace(tmp, {str(pidfile)!r})\n"
        "time.sleep(120)\n"
    )
    # The parent waits until the helper has PROVABLY started (its pidfile is published), bounded.
    # A fixed 0.5 s head start was a race on a loaded runner: Python start-up alone can take
    # longer, the parent exited first, and the helper was reaped before writing its pidfile —
    # "the helper must have started" (#1107). The property under test is unchanged: the helper
    # is still running when the parent exits cleanly.
    parent = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "deadline = time.time() + 30\n"
        f"while not os.path.exists({str(pidfile)!r}) and time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "print('parent done')\n"
    )
    code, out = au._run([_sys.executable, "-c", parent])
    assert code == 0 and "parent done" in out
    assert pidfile.exists(), "the helper must have started, or this proves nothing"
    helper = int(pidfile.read_text())  # published whole, never an empty file
    time.sleep(0.5)
    with pytest.raises(OSError):
        # Signal 0 only checks existence. A live helper makes this succeed.
        _os.kill(helper, 0)


def test_no_stored_field_can_reach_the_response_unnormalized():
    """The row is built from an allowlist, not copied from disk and patched.

    Four variants of one bug shipped before this — non-finite percentages, timestamps, token
    counts, then `window_days` — because the default was "forward whatever is stored" and each
    fix only narrowed it. Every field is named and normalized, so an unknown or malformed key
    cannot reach `JSONResponse` at all.
    """
    now = time.time()
    stored = {
        "opencode": {
            "engine": "opencode",
            "source": au.SOURCE_TOKENS,
            "at": now,
            "window_days": float("inf"),
            "tokens": {"in": 10, "out": 1, "cache_read": 0, "cache_write": 0},
            # A key no build ever wrote, carrying a value no encoder will take.
            "surprise": float("nan"),
            "plan": {"not": "a string"},
        }
    }
    budgets = {**BUDGETS, "engines": {"opencode": {"limit_tokens": 100}}}
    rows = {r["engine"]: r for r in au.build_rows(stored, budgets, now)}
    json.dumps(rows, allow_nan=False)
    assert rows["opencode"]["window_days"] is None
    assert rows["opencode"]["plan"] is None
    assert "surprise" not in rows["opencode"], "an unknown stored key is not forwarded"


def test_a_ranking_swap_between_windows_is_not_a_new_crossing():
    """session/week = 95/94 → 94/95 → 95/94, with both windows over 90 throughout.

    The announcement is about the worst window, but "worst" is a ranking and rankings swap.
    Holding only the announced window's key re-armed whichever window fell to second place, so
    the pair re-announced each other indefinitely without anything retreating.
    """
    budgets = {"threshold_pct": 90, "notify": True, "engines": {}}

    def rows(session_pct, week_pct):
        return [
            {
                "engine": "claude",
                "source": au.SOURCE_PLAN,
                "used_pct": max(session_pct, week_pct),
                "limit_tokens": 0,
                "windows": [
                    {"label": "session", "used_pct": session_pct, "resets_at": 1_800_000_000},
                    {"label": "week", "used_pct": week_pct, "resets_at": 1_800_604_800},
                ],
            }
        ]

    fresh, state = au.evaluate_alerts(rows(95, 94), budgets, [])
    assert len(fresh) == 1, "the first crossing announces"
    state = _deliver(fresh, state)
    for session_pct, week_pct in ((94, 95), (95, 94), (95, 96), (96, 95)):
        fresh, state = au.evaluate_alerts(rows(session_pct, week_pct), budgets, state)
        assert fresh == [], f"a swap is not a crossing ({session_pct}/{week_pct})"


def test_a_sibling_joining_a_LIVE_episode_is_not_a_second_announcement():
    """An episode is per (engine, level), not per window.

    Once the operator has been told "claude is past 90%", a second quota of the same agent
    crossing the same line is not news worth its own bell row — the alert already says the agent
    is over. This is the deliberate other side of the anti-oscillation rule: suppression lasts as
    long as ANY delivered window of that episode is still live, which is what makes a ranking
    swap silent without needing to know which window is currently worst.
    """
    budgets = {"threshold_pct": 90, "notify": True, "engines": {}}

    def rows(session_pct, week_pct):
        return [
            {
                "engine": "claude",
                "source": au.SOURCE_PLAN,
                "used_pct": max(session_pct, week_pct),
                "limit_tokens": 0,
                "windows": [
                    {"label": "session", "used_pct": session_pct, "resets_at": 1_800_000_000},
                    {"label": "week", "used_pct": week_pct, "resets_at": 1_800_604_800},
                ],
            }
        ]

    fresh, state = au.evaluate_alerts(rows(95, 10), budgets, [])
    assert len(fresh) == 1
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts(rows(95, 97), budgets, state)
    assert fresh == [], "the same episode, already announced"


def test_the_episode_re_arms_once_every_delivered_window_retreats():
    """The suppression above is bounded by the episode actually ending — otherwise one crossing
    would silence an agent forever. Every delivered window must fall past its band first."""
    budgets = {"threshold_pct": 90, "notify": True, "engines": {}}

    def rows(session_pct, week_pct):
        return [
            {
                "engine": "claude",
                "source": au.SOURCE_PLAN,
                "used_pct": max(session_pct, week_pct),
                "limit_tokens": 0,
                "windows": [
                    {"label": "session", "used_pct": session_pct, "resets_at": 1_800_000_000},
                    {"label": "week", "used_pct": week_pct, "resets_at": 1_800_604_800},
                ],
            }
        ]

    fresh, state = au.evaluate_alerts(rows(95, 10), budgets, [])
    state = _deliver(fresh, state)
    # The session resets — the one delivered window is gone, so the episode is over.
    fresh, state = au.evaluate_alerts(rows(2, 10), budgets, state)
    assert fresh == []
    assert state == [], "nothing left held"
    # The week now crosses on its own: a new episode, and it announces.
    fresh, state = au.evaluate_alerts(rows(2, 97), budgets, state)
    assert len(fresh) == 1
    assert fresh[0]["key"].split(":")[1] == "week"


def test_a_failed_delivery_is_not_consumed_by_an_unannounced_sibling():
    """The bug the delivered-only rule exists to prevent.

    Persisting every over-threshold sibling recorded windows as seen that were never delivered.
    So: session/week = 95/94 announces `session`; the bell write fails, so nothing is marked
    announced; the ranking swaps to 94/95 — and because `week` had been persisted anyway, the
    next evaluation found it already seen and offered nothing. **The operator got neither
    crossing.**
    """
    budgets = {"threshold_pct": 90, "notify": True, "engines": {}}

    def rows(session_pct, week_pct):
        return [
            {
                "engine": "claude",
                "source": au.SOURCE_PLAN,
                "used_pct": max(session_pct, week_pct),
                "limit_tokens": 0,
                "windows": [
                    {"label": "session", "used_pct": session_pct, "resets_at": 1_800_000_000},
                    {"label": "week", "used_pct": week_pct, "resets_at": 1_800_604_800},
                ],
            }
        ]

    fresh, state = au.evaluate_alerts(rows(95, 94), budgets, [])
    assert [a["key"].split(":")[1] for a in fresh] == ["session"]
    # Delivery FAILS — `_deliver` is deliberately not called, which is what a failed bell write
    # leaves behind: the crossing was offered and nothing was recorded as announced.
    assert state == [], "nothing may be persisted that was not delivered"

    fresh, state = au.evaluate_alerts(rows(94, 95), budgets, state)
    assert fresh, "the crossing must still be offered after a failed delivery"
    assert [a["key"].split(":")[1] for a in fresh] == ["week"]


def test_a_plan_window_announces_at_both_levels():
    """The two-level rule on the WINDOW key path, not just the token one.

    `alert_key` has two spellings — one per window for a `plan` row, one per limit for a
    `tokens` row — and the level has to be in both. The exhaustion test above only exercised
    the token spelling, so a level dropped from the window key went unnoticed.
    """
    budgets = {"threshold_pct": 90, "notify": True, "engines": {}}

    def rows(pct):
        return [
            {
                "engine": "claude",
                "source": au.SOURCE_PLAN,
                "used_pct": pct,
                "limit_tokens": 0,
                "windows": [{"label": "week", "used_pct": pct, "resets_at": 1_800_000_000}],
            }
        ]

    fresh, state = au.evaluate_alerts(rows(93.0), budgets, [])
    assert [a["level"] for a in fresh] == [90]
    state = _deliver(fresh, state)
    fresh, state = au.evaluate_alerts(rows(100.0), budgets, state)
    assert [a["level"] for a in fresh] == [au.EXHAUST_PCT], "exhaustion is its own crossing"
    state = _deliver(fresh, state)
    # And neither repeats.
    fresh, state = au.evaluate_alerts(rows(100.0), budgets, state)
    assert fresh == []
