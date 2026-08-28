"""#801 Phase 2 — the matrix: which engines, in which states, actually submit a nudge?

**This file produces a table, not a fix.** The original byte-shape hypothesis was tested and
refuted, so the honest next step is measurement across every actuable engine and every state a
real nudge meets — and only then a fix, scoped to whatever is red.

Opt-in twice over: the engine must be installed, and `AGENT_SESSIONS_REAL_AGENT=1` must be set.
These launch real agents and spend real tokens, so CI never runs them.

    AGENT_SESSIONS_REAL_AGENT=1 uv run --extra dev pytest -q tests/test_nudge_submit_real.py \
        -m real_agent -s

Read `tests/nudge_harness.py` first — its traps each produced a confident false negative,
and every one of them looks exactly like the bug under test.
"""

from __future__ import annotations

import pytest

import nudge_harness as H
from agent_sessions import actuator, prefs

pytestmark = pytest.mark.real_agent

# ── What this file deliberately does NOT measure ─────────────────────────────────────────
#
# #801 lists four conditions. Only the two below are here, and the omission is a finding
# rather than an oversight.
#
# **busy / mid-turn** and **mid-render** both require proving that the write landed while the
# ENGINE was still producing output. Three separate attempts at that predicate were each shown
# wrong by review, and each counter-example was correct:
#
#   * "output within the last 350 ms"  — a resize can emit its final byte 50 ms before the
#     write and stop dead; recency is not activity.
#   * "output after the write"         — the nudge's own echo and the turn it starts produce
#     exactly that, so it is satisfied even when the repaint ended first.
#   * "output in 2 of 4 sampled buckets" — the producer can stop in bucket 2 and the helper
#     still waits out buckets 3 and 4 before the caller writes.
#
# The common root: from a PTY byte stream you can observe *that* output happened, never *who
# is still working*. Distinguishing engine-caused output from input-caused echo, at the
# instant of a write, needs an engine-state signal this instrument does not have.
#
# So those cells are not shipped rather than shipped unsound. An unmeasurable condition is not
# a passing condition — the same rule #801 applies to untested engines. Closing them wants a
# different mechanism (an engine that reports its own busy state, or a PTY-level generation
# marker), which is Phase 3's problem and not this PR's.
# ─────────────────────────────────────────────────────────────────────────────────────────

#: Every engine declaring `supports_orchestrator_input`. `shell` is absent by design — a nudge
#: into a login shell is a *command*, which is the whole reason that gate is default-deny.
ACTUABLE = ("claude", "opencode", "codex", "gemini", "antigravity", "kimi")


def _nudge_payload() -> bytes:
    """The bytes a real `continue` sends — rendered by the server, never authored here.

    Taken from `actuator.render` so the matrix measures what the orchestrator actually emits.
    A payload hand-written in a test would make the whole exercise measure the test.
    """
    cfg = prefs.get_orchestrator()
    return actuator.render({"verb": "continue"}, cfg)


def _session(engine: str, tmp_path) -> H.RealSession:
    """A session for an engine that is genuinely testable, else skip with the reason.

    Skips are reserved for **prerequisites** — binary absent, not actuable, no transcript
    adapter, opt-in not armed. They must never absorb a launch or readiness failure: an engine
    that passed availability and then failed to paint is a red result, and turning it into a
    green skip is how a broken harness reports success (review on #858).
    """
    ok, reason = H.engine_available(engine)
    if not ok:
        pytest.skip(reason)
    return H.RealSession(engine=engine, cwd=str(tmp_path))


def _ready(s: H.RealSession) -> None:
    """Wait for readiness, and classify a missing LOGIN as a prerequisite rather than a red cell.

    **The auth check runs whether or not readiness succeeded, and the ordering is the whole
    point.** `wait_ready()` accepts any first paint followed by a quiet window — and a login
    prompt paints once and then waits, so a logged-out engine looks *ready*. Checking auth only
    on the failure path therefore never fires for the most common logged-out shape, and the
    nudge goes on to fail as a red matrix result: a confident false negative about submission,
    caused by a missing login (review on #858; the earlier version had exactly this bug).

    `present_providers()` answers "is the binary installed", never "is it logged in", and
    `service_env()` strips provider credential families on purpose — so this cannot be inferred
    from availability either. #801 asks for a skip when the binary **or its auth** is absent.

    Anything else stays a failure: an engine that is installed, authenticated and still will not
    start is a real result, not something to skip past.
    """
    ready = s.wait_ready()
    if s.looks_unauthenticated():
        pytest.skip(
            f"{s.engine} is installed but not authenticated in a service-like environment, so "
            "it cannot be measured here — recorded as UNTESTED, not as failing"
        )
    if ready:
        return
    raise AssertionError(
        f"{s.engine} passed availability and appears authenticated but never became ready — "
        "a launch/readiness failure, which is a real result and not something to skip past"
    )


@pytest.mark.parametrize("engine", ACTUABLE)
def test_nudge_submits_when_quiescent(engine, tmp_path):
    """Baseline: an idle session at an empty prompt.

    This is the ONLY cell the refuted Phase 1 run covered, and it passed for claude. It is here
    as the control — a red here means something much more basic than the reported defect.
    """
    with _session(engine, tmp_path) as s:
        _ready(s)
        submitted, detail = H.submits(s, _nudge_payload())
        print(
            f"\n[matrix] {engine:12s} quiescent      : {'PASS' if submitted else 'FAIL'} — {detail}"
        )
        assert submitted, f"{engine} did not submit a quiescent nudge: {detail}"


@pytest.mark.parametrize("engine", ACTUABLE)
def test_nudge_submits_without_the_quiet_gate(engine, tmp_path):
    """Isolates the quiet gate itself as the variable.

    If mid-render is red and this is green (or vice versa), the gate is implicated directly
    rather than by inference — which is the difference between a fix and another guess.
    """
    with _session(engine, tmp_path) as s:
        _ready(s)
        submitted, detail = H.submits(s, _nudge_payload(), require_quiet=False)
        print(
            f"\n[matrix] {engine:12s} no-quiet-gate  : {'PASS' if submitted else 'FAIL'} — {detail}"
        )
        assert submitted, f"{engine} did not submit with the quiet gate off: {detail}"
