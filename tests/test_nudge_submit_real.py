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

import uuid

import pytest

import nudge_harness as H
from agent_sessions import actuator, prefs
from agent_sessions.engines import registry

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

#: Engines whose first-run flow cannot be ANSWERED under a pinned session id, so the prompt has
#: to be avoided at launch. Maps to the single flag that suppresses that prompt — never to the
#: provider's `bypass`, which is a wider thing. See `_session`.
_SUPPRESS_TRUST_FLAG = {"gemini": "--skip-trust"}


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
    # `bypass` stays FALSE for every engine, and the gemini case is why that matters.
    #
    # Gemini needs its workspace-trust dialog suppressed: ACCEPTING it makes gemini re-exec, and
    # the re-exec collides with the pinned `--session-id` ("Session ID … already exists") and
    # kills the session — so the prompt has to be avoided rather than answered.
    #
    # But the provider's `bypass` does not mean "suppress that prompt". It expands to
    # `--yolo --skip-trust`, and `--yolo` AUTO-APPROVES TOOL CALLS — a broad permission grant on
    # an authenticated agent. A throwaway cwd is not a sandbox; it confines nothing about
    # filesystem, process or network access. That grant would need explicit human approval and
    # has none, so it is not taken (review on #880).
    #
    # So: the production argv with `bypass=False`, plus the ONE flag that suppresses the prompt.
    # `--skip-trust` skips a question; `--yolo` skips a safety boundary. This harness needs only
    # the first, and the two must not be reached for together just because a provider bundles
    # them behind one parameter.
    prov = next(p for p in registry.present_providers() if p.engine_id == engine)
    native = str(uuid.uuid4())
    argv = prov.new_launch_argv(native, cwd=str(tmp_path), bypass=False)
    flag = _SUPPRESS_TRUST_FLAG.get(engine)
    if flag:
        argv = [*argv, flag]
    return H.RealSession(engine=engine, cwd=str(tmp_path), native_id=native, argv_override=argv)


def _classify(s: H.RealSession, submitted: bool, detail: str) -> None:
    """Decide what a cell MEANS. The whole contested question of this PR lives here.

    **A PASS needs no prerequisite, and that is what makes the rest tractable.** A session parked
    on a trust dialog or a Terms notice cannot commit a turn, so a committed turn is
    self-certifying: the engine was at a prompt and it submitted. Nothing has to be established
    beforehand.

    The ambiguity exists only on a FAILURE, where "the engine dropped it" and "the engine was
    never at a prompt" are indistinguishable from the turn count. #801's contract is that the
    second must not be published as the first. So a failure is a RED cell only when the engine
    has a DEMONSTRATED prompt-ready signal that said it was ready — and today none does
    (`H.PROMPT_READY` is empty, with the measurements that emptied it recorded there).

    That is deliberately conservative: it means no engine can produce a red until someone
    contributes a readiness signal for it. Which is the honest reading of "we observed no
    submission and cannot establish the engine was ever at a prompt", and it names the missing
    prerequisite instead of guessing past it.
    """
    if submitted:
        return
    if detail.startswith("not delivered:"):
        # The WRITE did not land. Still a failure, but a claim about the delivery path — not
        # about how the engine handles a payload that DID arrive.
        pytest.fail(
            f"{s.engine}: the write itself did not land, so this says nothing about whether the "
            f"engine submits a delivered payload. {detail}"
        )
    ready, why = H.prompt_ready(s)
    if ready is True:
        pytest.fail(
            f"{s.engine}: bytes were DELIVERED to a session demonstrated to be at a prompt "
            f"({why}), and no user turn committed. This is the defect #801 exists to measure. "
            f"{detail}"
        )
    pytest.skip(
        f"{s.engine}: delivered, and no turn committed — but {why}, so this cannot be published "
        f"as an engine defect. UNTESTED, not failing (#801). {detail}"
    )


def _ready(s: H.RealSession) -> None:
    """Wait for the session to paint and go quiet. **No longer a gate.**

    Every version of a gate here was wrong, in four different ways, and the last two were refuted
    by the installed stores rather than by argument — see `H.PROMPT_READY` for the measurements.
    The gate moved to `_classify`, where the question actually arises: only a FAILING cell is
    ambiguous, because a PASS certifies its own prerequisite.
    """
    s.wait_ready()


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
        _classify(s, submitted, detail)
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
        _classify(s, submitted, detail)
        assert submitted, f"{engine} did not submit with the quiet gate off: {detail}"
