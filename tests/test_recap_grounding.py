"""Recap grounding (#755).

The failure this pins is real and was shipped: a `CONTINUE` proposal carried the recap line

    Session ended with a crash (OS can't spawn worker thread: Resource temporarily
    unavailable) during a subsequent Explain-this-codebase request, unrelated to the
    config change.

for `codex:019f980f-…`, whose transcript's final event is `task_complete` and whose 22KB screen
contains no instance of `crash`, `error`, `unavailable` or `spawn`. Both inputs were verifiably
clean; the model invented the failure whole.

That field is what an operator reads before authorising the actuator to type into a live
session, so a fabricated failure can induce an approval they would otherwise decline.
"""

import pytest

from agent_sessions.review import ReviewError, _recap_shape_guard

# The five true lines, verbatim from the stored recap.
REAL_LINES = [
    "Inspected ~/.config/opencode/opencode.json and found default model set to "
    "local/minimax-m3-row with laguna-s-2.1 already registered under the local provider.",
    "Edited opencode.json to change the model field from local/minimax-m3-row to "
    "local/laguna-s-2.1.",
    "Validated the JSON parses cleanly and the model is registered in the provider catalog.",
    "Ran opencode debug config to confirm the effective model resolves to local/laguna-s-2.1.",
    "OpenCode default model is now local/laguna-s-2.1; new sessions will use Laguna by default.",
]
FABRICATED = (
    "Session ended with a crash (OS can't spawn worker thread: Resource temporarily "
    "unavailable) during a subsequent Explain-this-codebase request, unrelated to the "
    "config change."
)

# Stands in for the session's evidence: the config work happened, the crash did not.
EVIDENCE = """
$ cat ~/.config/opencode/opencode.json
{"model": "local/minimax-m3-row"}
$ opencode debug config
model: local/laguna-s-2.1
OpenCode default model is now local/laguna-s-2.1.
task_complete
"""


def test_the_fabricated_crash_line_is_dropped_and_the_true_lines_survive():
    obj = {"recap": "\n".join([*REAL_LINES, FABRICATED])}
    out = _recap_shape_guard(obj, EVIDENCE).splitlines()
    assert len(out) == 5, out
    assert all("crash" not in ln for ln in out)
    assert out[0].startswith("Inspected ~/.config/opencode/opencode.json")
    assert out[-1].startswith("OpenCode default model is now")


def test_a_quote_that_really_is_in_the_evidence_survives():
    """The guard must not punish a recap for doing its job — quoting real output."""
    line = 'Ran the check and it reported ("model: local/laguna-s-2.1") as effective.'
    out = _recap_shape_guard({"recap": line}, EVIDENCE)
    assert out == line


def test_a_re_wrapped_quote_still_counts_as_grounded():
    """Whitespace and case must not decide whether a true quote is called a lie.

    The guard collapses intra-line whitespace by contract, so the comparison is against the
    collapsed form — what matters is that the line SURVIVES, not that it is byte-identical.
    """
    line = "Config showed (MODEL:   local/laguna-s-2.1) after the edit was applied cleanly."
    out = _recap_shape_guard({"recap": line}, EVIDENCE)
    assert out == " ".join(line.split())
    assert "local/laguna-s-2.1" in out


def test_short_parentheticals_are_never_treated_as_quotations():
    """`(PR #741)` and `(3 files)` are asides, not claims about machine output."""
    line = "Merged the change (PR #741) after review and confirmed it landed (3 files)."
    assert _recap_shape_guard({"recap": line}, EVIDENCE) == line


def test_an_entirely_fabricated_recap_keeps_the_last_good_one():
    """Degrade, never drop the record: the caller swallows ReviewError and leaves ai_recap be."""
    obj = {"recap": FABRICATED}
    with pytest.raises(ReviewError):
        _recap_shape_guard(obj, EVIDENCE)


def test_without_a_source_the_guard_is_shape_only():
    """Callers that have no evidence to check against must keep working unchanged."""
    obj = {"recap": "\n".join([*REAL_LINES, FABRICATED])}
    assert len(_recap_shape_guard(obj).splitlines()) == 6


def test_grounding_runs_after_the_existing_cleanup_not_instead_of_it():
    """Bullets are still stripped and blanks still dropped — the new rule composes."""
    obj = {"recap": f"- {REAL_LINES[1]}\n\n  \n1. {REAL_LINES[2]}\n{FABRICATED}"}
    out = _recap_shape_guard(obj, EVIDENCE).splitlines()
    assert out == [REAL_LINES[1], REAL_LINES[2]]


# --- a nudge nobody is waiting for (#755, second defect) ------------------------------------

from agent_sessions.orchestrator import _validate_actions  # noqa: E402


def _sent(age_hours: float, sid: str = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"):
    return {sid: {"id": sid, "age_hours": age_hours}}


def test_continue_is_not_delivered_into_work_that_finished_days_ago():
    """The real case: `continue` at confidence 0.8 on a session finished six days earlier."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.8}],
        },
        _sent(144.0),
    )
    assert actions[0]["verb"] == "escalate", "a stale session still got an unattended nudge"
    # Degraded, never dropped — the operator must still see the session.
    assert actions[0]["session_id"] == sid


def test_a_recently_idle_session_still_gets_its_nudge():
    """The gate must not swallow the normal case it was never aimed at."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.8}],
        },
        _sent(3.0),
    )
    assert actions[0]["verb"] == "continue"


def test_a_stale_choose_loses_its_option_when_it_degrades():
    """A leftover `option` on an `escalate` would be a delivery payload with no delivery."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "choose", "option": 2, "confidence": 0.9}],
        },
        _sent(200.0),
    )
    assert actions[0]["verb"] == "escalate"
    assert "option" not in actions[0]


def test_escalate_on_a_stale_session_is_untouched():
    """Escalation is already 'the operator looks' — staleness changes nothing about it."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "escalate", "confidence": 0.4}],
        },
        _sent(500.0),
    )
    assert actions[0]["verb"] == "escalate"


def test_the_gate_fires_on_the_shape_production_actually_passes():
    """The regression Hermes caught: `run_pass` builds `sent` from RAW CARDS.

    Raw cards carry `last_activity`; `age_hours` exists only on the trimmed `_digest_entry`
    copy sent to the model. Reading `age_hours` alone meant the gate found `None` on every real
    call and never fired — and the earlier tests could not see it, because they built their own
    `sent` with `age_hours` already in it. This one passes what the callers pass.
    """
    import time

    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    now = time.time()
    raw_card = {  # the shape `pulse.build_cards` yields, six days idle
        "id": sid,
        "engine": "codex",
        "title": "Switch OpenCode default model to laguna-s-2.1",
        "last_activity": now - 144 * 3600,
    }
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.8}],
        },
        {sid: raw_card},
        now=now,
    )
    assert actions[0]["verb"] == "escalate", "the gate did not fire on a real card"


def test_a_recent_raw_card_keeps_its_nudge():
    import time

    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    now = time.time()
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.8}],
        },
        {sid: {"id": sid, "last_activity": now - 2 * 3600}},
        now=now,
    )
    assert actions[0]["verb"] == "continue"


def test_a_card_with_no_activity_stamp_is_not_treated_as_stale():
    """No timestamp means unknown, and unknown must not silently block every delivery."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.8}],
        },
        {sid: {"id": sid}},
    )
    assert actions[0]["verb"] == "continue"


def test_the_chat_path_measures_age_against_its_own_pass():
    """The sibling call site Hermes named. `orchestrator_chat.ask` builds `sent` from raw cards
    too, so it needs the same age handling — and the same `now`, not wall-clock."""
    import time

    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    # A `now` well in the future proves the passed value is what's used: measured against
    # wall-clock this card is fresh, measured against the pass's `now` it is 100h stale.
    then = time.time()
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [{"session_id": sid, "verb": "continue", "confidence": 0.9}],
        },
        {sid: {"id": sid, "last_activity": then}},
        now=then + 100 * 3600,
    )
    assert actions[0]["verb"] == "escalate"
