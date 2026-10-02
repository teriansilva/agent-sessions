"""The recent-work window (#1086): 1–3 days, default 1, read-lenient / write-strict.

Driven by tests/fixtures/pulse_window_days_cases.json, which web/src/lib/recentWindow.test.ts reads
too — one table, both languages, so the clamp and the boolean rule cannot drift between them.
"""

import json
from pathlib import Path

from agent_sessions import prefs, pulse

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "pulse_window_days_cases.json").read_text()
)


def test_the_fixture_describes_both_modules_constants():
    assert (_CASES["min"], _CASES["max"], _CASES["default"]) == (
        prefs.PULSE_WINDOW_MIN,
        prefs.PULSE_WINDOW_MAX,
        prefs.PULSE_WINDOW_DEFAULT,
    )
    assert (pulse.WINDOW_DAYS_MIN, pulse.WINDOW_DAYS_MAX, pulse.WINDOW_DAYS_DEFAULT) == (
        prefs.PULSE_WINDOW_MIN,
        prefs.PULSE_WINDOW_MAX,
        prefs.PULSE_WINDOW_DEFAULT,
    )


def test_shared_read_table_prefs_and_pulse_agree():
    for case in _CASES["read"]:
        got = prefs.coerce_pulse_window_days(case["in"])
        assert got == case["out"], f"{case['in']!r} → {got}: {case['why']}"
        assert pulse.coerce_window_days(case["in"]) == got, case["why"]


def test_read_edges_json_cannot_carry():
    for v in (float("nan"), float("inf"), float("-inf")):
        assert prefs.coerce_pulse_window_days(v) == prefs.PULSE_WINDOW_DEFAULT


def test_shared_write_table():
    for v in _CASES["write_accepted"]:
        assert prefs.is_valid_pulse_window_days(v) is True, v
        assert prefs.validate_pulse_patch({"window_days": v}) is None, v
    for v in _CASES["write_rejected"]:
        assert prefs.is_valid_pulse_window_days(v) is False, v
        assert prefs.validate_pulse_patch({"window_days": v}) is not None, v


def test_a_stored_long_window_CLAMPS_on_read_instead_of_resetting(tmp_path):
    """The behaviour change #1086 pins: before, an out-of-range stored value fell back to the
    default. A stored 7 (valid under the old 1–30 range) now reads as the longest window still
    offered, so an operator who chose a long window keeps the longest one."""
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps({"pulse": {"window_days": 7}}))
    assert prefs.get_pulse(path)["window_days"] == 3


def test_unset_window_is_one_day(tmp_path):
    assert prefs.get_pulse(tmp_path / "none.json")["window_days"] == 1
