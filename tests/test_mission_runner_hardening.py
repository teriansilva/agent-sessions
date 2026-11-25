# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""Plan-runner hardening (#2023, follow-up of #1951 slice B / #2019).

* **Consent at the gate** (Phase 1): a gate in a step session started under a standing consent
  checks the consent is still in force, through the one decide door and the one boundary
  (`mission_approval.admit(…, "gate", consent_id=…)`); a consent-authority run without its
  consent id fails closed.
* **Bounded back-off** (Phase 3): `_retry_at`, `_failures` and `_changes` are pruned together.
* **Runner correctness** (Phase 4): the board, the bell rows, the fence, shutdown and the words.
"""

from __future__ import annotations

import copy
import time
import types
import uuid

import pytest

from agent_sessions import mission_approval as appr
from agent_sessions import mission_runner, mission_steps, missions, notifications
from agent_sessions import structured_runtime as runtime
from mission_team_fixture import GRADES, store_plan
from mission_team_fixture import context as team_context
from mission_team_fixture import spec as team_spec
from test_mission_runner import (  # noqa: F401 — the shared `app` fixture
    _approve,
    _edit_specialist,
    _stop_all,
    _ungrade,
    app,
)
from test_mission_steps import (  # noqa: F401 — shared fixtures
    anyio_backend,
    seam,
)
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    host,
    ident,
    project,
)


@pytest.fixture(autouse=True)
def _fresh_runner():
    mission_runner.reset_for_test()
    yield
    mission_runner.reset_for_test()


# ---- Phase 1: consent at the gate ----------------------------------------------------------------


def _consent(mid: str) -> str:
    """A standing consent on the approval in force, the way the route writes it."""
    approval = missions.approval(mid)
    offer = appr.consent_scope(approval, "Apply")
    scope = {k: v for k, v in offer.items() if k != "digest"}
    return missions.commit_consent(
        mid,
        approval_id=approval["id"],
        approval_digest=approval["digest"],
        scope=scope,
        scope_digest=offer["digest"],
        expires_at=time.time() + 86400,
        operation_id=str(uuid.uuid4()),
        request_digest="r",
    )["id"]


def _whole_plan_qualifies(seam) -> None:
    """A consent covers the WHOLE plan: grade sonnet `frontier`, so every step of the seam's plan
    (review needs Senior) re-checks clean, not only the step it launches."""
    graded = copy.deepcopy(GRADES)
    for engine in graded:
        graded[engine]["sonnet"] = "frontier"
    seam["team"]["ctx"] = team_context(
        specs=[team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2)],
        grades=graded,
    )


async def _start_under_consent(seam) -> tuple[dict, str]:
    _whole_plan_qualifies(seam)
    _approve(seam["mid"])
    cid = _consent(seam["mid"])
    out = await mission_steps.start_step(
        seam["mid"],
        "o1.posting",
        revision=1,
        authority=mission_steps.Authority("consent", cid),
        admit=appr.admit,
    )
    assert out["outcome"] == "started", out
    (run,) = missions.step_runs(seam["mid"])
    assert run["authority"] == "consent" and run["consent_id"] == cid
    return run, cid


def _fake_adapter(m, asked: list[str]) -> None:
    async def adapter_decide(*a, **kw):
        asked.append(a[4])
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    m.setattr(runtime, "_adapter", lambda engine, op: Fake())


async def _decide(key: str, decision: str):
    return await runtime.decide(
        key,
        turn_id="t",
        request_id="r",
        decision_id=str(uuid.uuid4()),
        decision=decision,
        user="u",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("consent", ["valid", "revoked", "expired", "replaced"])
async def test_a_gate_in_a_consent_session_checks_the_consent_is_still_in_force(
    seam, monkeypatch, consent
):
    """Through the actual decide door: with the approval itself still clean, a revoked, expired or
    replaced consent refuses `approve` / `always` / `answer` before the adapter is called, and is
    recorded as a gate refusal; a decline always goes through."""
    run, cid = await _start_under_consent(seam)
    if consent == "revoked":
        missions.revoke_consent(seam["mid"], cid)
    elif consent == "replaced":
        assert _consent(seam["mid"]) != cid
    elif consent == "expired":
        # The consent's clock only: everything else reads the real time.
        monkeypatch.setattr(appr, "time", types.SimpleNamespace(time=lambda: 9e12))
    asked: list[str] = []
    with monkeypatch.context() as m:
        _fake_adapter(m, asked)
        for decision in ("approve", "always", "answer"):
            if consent == "valid":
                await _decide(run["session_key"], decision)
                continue
            with pytest.raises(runtime.StructuredError) as e:
                await _decide(run["session_key"], decision)
            assert e.value.status == 409 and e.value.reason == "approval_refused", e.value
            want = "expired" if consent == "expired" else "no longer covers it"
            assert f"your standing consent {want}" in str(e.value.detail), e.value.detail
        if consent == "valid":
            assert asked == ["approve", "always", "answer"]
            assert missions.refusals(seam["mid"]) == []
        else:
            assert asked == [], "an allow reached the client although the consent is not in force"
            rec = missions.refusals(seam["mid"])
            assert rec and {r["boundary"] for r in rec} == {"gate"}
        await _decide(run["session_key"], "reject")
        assert asked[-1] == "reject", "a decline was held up by the consent check"
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_consent_run_without_its_consent_id_fails_closed(monkeypatch):
    """A run whose authority is `consent` but names no consent is never admitted on the approval
    alone: the boundary is asked as a consent-backed gate and refuses."""
    run = {
        "mission_id": "m-1",
        "step_id": "o1.cv",
        "authority": "consent",
        "consent_id": None,
        "use_kind": "primary",
        "alternate_index": None,
    }
    monkeypatch.setattr(missions, "step_run_of_session", lambda key: run)
    monkeypatch.setattr(missions, "get_mission", lambda *a, **k: {"state": "running"})
    seen: list[dict] = []

    def admit(mid, step, boundary, **kw):
        seen.append({"boundary": boundary, **kw})
        if "consent_id" in kw and not kw["consent_id"]:
            return {"ok": False, "reasons": [{"code": "approval_revoked", "text": "no consent"}]}
        return {"ok": True, "reasons": []}

    monkeypatch.setattr(appr, "admit", admit)
    with pytest.raises(runtime.StructuredError) as e:
        await runtime._admit_gate("claude-api:x")
    assert e.value.status == 409
    assert seen == [{"boundary": "gate", "consent_id": ""}]


def test_admit_refuses_a_consent_backed_gate_that_names_no_consent(monkeypatch):
    """The boundary itself: a gate asked with an empty consent id is consent-backed, and no
    consent covers it, even while the approval re-checks clean."""
    approval = {"id": "apr-1", "digest": "sha256:" + "0" * 64, "pins": {}, "revoked_at": None}
    monkeypatch.setattr(missions, "approval", lambda mid: approval)
    monkeypatch.setattr(missions, "plan_revision", lambda mid: {"document": {}, "revision": 1})
    monkeypatch.setattr(appr, "context_for", lambda doc: None)
    monkeypatch.setattr(appr, "read_facts", lambda doc: None)
    monkeypatch.setattr(appr, "recheck", lambda *a, **k: {"ok": True, "reasons": []})
    monkeypatch.setattr(missions, "consent", lambda mid, cid=None: None)
    monkeypatch.setattr(missions, "step_runs", lambda mid: [])
    monkeypatch.setattr(missions, "record_refusal", lambda *a, **k: True)
    assert appr.admit("m-1", "o1.cv", "gate")["ok"], "an approval-only gate needs no consent"
    out = appr.admit("m-1", "o1.cv", "gate", consent_id="")
    assert not out["ok"] and out["reasons"][0]["code"] == "approval_revoked"
    assert "standing consent" in out["reasons"][0]["text"]


def _consent_admit(monkeypatch, whole_plan: list[dict]) -> None:
    """`admit` over a clean approval and a consent in force, with the alternate's own pin clean
    and `whole_plan` as the reasons of the whole-plan re-check."""
    approval = {"id": "apr-1", "digest": "sha256:" + "0" * 64, "pins": {}, "revoked_at": None}
    consent = {"id": "c-1", "approval_id": "apr-1", "revoked_at": None, "expires_at": 9e12}
    monkeypatch.setattr(missions, "approval", lambda mid: approval)
    monkeypatch.setattr(missions, "plan_revision", lambda mid: {"document": {}, "revision": 1})
    monkeypatch.setattr(appr, "context_for", lambda doc: None)
    monkeypatch.setattr(appr, "read_facts", lambda doc: None)
    monkeypatch.setattr(missions, "consent", lambda mid, cid=None: consent)
    monkeypatch.setattr(missions, "step_runs", lambda mid: [])
    monkeypatch.setattr(missions, "record_refusal", lambda *a, **k: True)

    def recheck(a, doc, rev, ctx, facts, *, step=None, alternate=None):
        if step is not None:
            return {"ok": True, "reasons": []}  # the alternate's own pin is clean
        return {"ok": not whole_plan, "reasons": list(whole_plan)}

    monkeypatch.setattr(appr, "recheck", recheck)


_SPENT = {"code": "model_unavailable", "cause": "usage_exhausted", "step": "o1.cv", "text": "x"}


def test_a_consent_backed_alternate_gate_is_checked_on_its_own_pin(monkeypatch):
    """The whole-plan check that voids a consent on any change never refuses an alternate's GATE
    for its own pick's CLIENT (out of usage): the alternate's own pin decides that step. The
    pick's own session still sees the cause."""
    _consent_admit(monkeypatch, [_SPENT])
    assert appr.admit("m-1", "o1.cv", "gate", consent_id="c-1", alternate=0)["ok"]
    out = appr.admit("m-1", "o1.cv", "gate", consent_id="c-1")
    assert not out["ok"], "the pick's own session must still see its client's cause"


@pytest.mark.parametrize(
    "reason",
    [
        # A CHANGE on the alternate's own step (no client cause) is a change: it voids.
        {"code": "specialist_version_changed", "step": "o1.cv", "text": "v1 to v2"},
        # Another step's client is out of usage / not mission-ready: not this alternate's pick.
        {"code": "model_unavailable", "cause": "usage_exhausted", "step": "o1.review", "text": "x"},
        {
            "code": "model_unavailable",
            "cause": "engine_not_mission_ready",
            "step": "o1.review",
            "text": "x",
        },
        # Any other step's change.
        {"code": "specialist_version_changed", "step": "o1.review", "text": "v1 to v2"},
    ],
    ids=["own-step-change", "other-step-usage", "other-step-readiness", "other-step-change"],
)
def test_the_alternate_exemption_is_only_its_own_picks_client(monkeypatch, reason):
    _consent_admit(monkeypatch, [_SPENT, reason])
    assert not appr.admit("m-1", "o1.cv", "gate", consent_id="c-1", alternate=0)["ok"]


def test_a_consent_launch_of_an_alternate_is_unchanged(monkeypatch):
    """The exemption is the GATE's only: a consent LAUNCH of an alternate is still refused while
    the pick's client is out of usage (standing consent has no consumer yet, and its scope lines
    say nothing of alternates)."""
    _consent_admit(monkeypatch, [_SPENT])
    out = appr.admit("m-1", "o1.cv", "consent", consent_id="c-1", alternate=0)
    assert not out["ok"] and out["reasons"][0].get("cause") == "usage_exhausted"


@pytest.mark.anyio
async def test_a_malformed_alternate_index_fails_closed_at_the_gate(monkeypatch):
    run = {
        "mission_id": "m-1",
        "step_id": "o1.cv",
        "authority": "approval",
        "use_kind": "alternate",
        "alternate_index": "not-an-index",
    }
    monkeypatch.setattr(missions, "step_run_of_session", lambda key: run)
    monkeypatch.setattr(appr, "admit", lambda *a, **k: {"ok": True, "reasons": []})
    with pytest.raises(runtime.StructuredError) as e:
        await runtime._admit_gate("claude-api:x")
    assert e.value.status == 503


# ---- Phase 3: the back-off is bounded ------------------------------------------------------------


def _seed(*keys: tuple[str, str, str]) -> None:
    """A back-off entry in all three maps, as a refused offer leaves it."""
    for n, key in enumerate(keys, 1):
        mission_runner._retry_at[key] = 1e9 + n
        mission_runner._failures[key] = n
        mission_runner._changes[key] = f"sha256:{n}"


def _kept(key: tuple[str, str, str]) -> bool:
    there = [
        key in mission_runner._retry_at,
        key in mission_runner._failures,
        key in mission_runner._changes,
    ]
    assert len(set(there)) == 1, f"{key} was pruned from only some of the maps: {there}"
    return there[0]


@pytest.mark.anyio
async def test_a_pass_prunes_gone_steps_and_old_approvals_and_keeps_held_and_busy_ones(seam):
    """`advance` drops the back-off of a step that left the plan and of an approval that is no
    longer the one in force, in all three maps together. A held step and a step that is only not
    startable now (waiting) keep their deadline and failure count; another mission's are not this
    pass's to judge."""
    mid = seam["mid"]
    first = _approve(mid)
    store_plan(mid, seam["doc"])  # revision 2: the first approval is superseded
    live = _approve(mid)
    assert live["id"] != first["id"]
    assert missions.set_step_hold(mid, "o1.posting", hold=True) is True
    held = (mid, "o1.posting", live["id"])
    waiting = (mid, "o1.cv", live["id"])
    gone = (mid, "o1.gone", live["id"])
    stale = (mid, "o1.cv", first["id"])
    other = ("m-other", "o1.cv", "apr-x")
    _seed(held, waiting, gone, stale, other)
    before = (mission_runner._retry_at[held], mission_runner._failures[held])
    assert await mission_runner.advance(mid) == {}
    assert _kept(held) and _kept(waiting) and _kept(other)
    assert (mission_runner._retry_at[held], mission_runner._failures[held]) == before
    assert not _kept(gone), "a step that left the plan kept its back-off"
    assert not _kept(stale), "an approval no longer in force kept its back-off"
    # Revoked: no approval is in force, so none of the mission's keys stands.
    missions.revoke_approval(mid, live["id"])
    assert await mission_runner.advance(mid) == {}
    assert not _kept(held) and not _kept(waiting) and _kept(other)


@pytest.mark.anyio
async def test_a_failed_read_of_the_steps_prunes_nothing(seam, monkeypatch):
    mid = seam["mid"]
    approval = _approve(mid)
    gone = (mid, "o1.gone", approval["id"])
    _seed(gone)

    def boom(mission_id):
        raise RuntimeError("the store is unreadable")

    monkeypatch.setattr(mission_runner, "offers_sync", boom)
    assert await mission_runner.advance(mid) == {}
    assert _kept(gone), "an unreadable plan was taken for an empty one"


@pytest.mark.anyio
async def test_the_sweep_prunes_missions_that_left_the_worklist_unless_the_read_failed(
    seam, monkeypatch
):
    mid = seam["mid"]
    approval = _approve(mid)
    mine = (mid, "o1.cv", approval["id"])
    left = ("m-left", "o1.cv", "apr-x")
    _seed(mine, left)

    async def advance(m, registry=None, **kw):
        return {}

    monkeypatch.setattr(mission_runner, "advance", advance)
    real = missions.approved_missions

    def unreadable():
        raise RuntimeError("the store is unreadable")

    monkeypatch.setattr(missions, "approved_missions", unreadable)
    assert await mission_runner.sweep() == 0
    assert _kept(mine) and _kept(left), "a failed worklist read was taken for an empty one"
    monkeypatch.setattr(missions, "approved_missions", real)
    assert await mission_runner.sweep() == 1
    for t in list(mission_runner._inflight.values()):
        await t
    assert _kept(mine)
    assert not _kept(left), "a mission that left approved_missions() kept its back-off"


# ---- Phase 4: runner correctness -----------------------------------------------------------------


def _board(mid: str, step: str = "o1.posting") -> dict:
    return next(s for s in mission_steps.view_sync(mid)["steps"] if s["id"] == step)


@pytest.mark.anyio
async def test_a_running_step_never_shows_refused_and_an_earlier_run_never_hides_a_new_one(seam):
    """The race (re-review of #2019 at 53ff936d): the runner read the step ready, the operator's
    Start won, then the runner's admission refused. The refusal was written AFTER the run, and
    the board must not call a running step `refused`, nor the run once it ended after the
    refusal. Episode-aware: a refusal recorded after that episode ended is the step's standing
    one again (the rework reopen and `bound_reached` cases of `test_mission_rework.py`)."""
    mid = seam["mid"]
    _approve(mid)
    out = await mission_steps.start_step(mid, "o1.posting", revision=1)  # the operator's Start
    assert out["outcome"] == "started", out
    before = _board(mid)
    assert before["refusal"] is None
    _ungrade(seam)
    assert not appr.admit(mid, "o1.posting", "launch")["ok"]  # the runner's, after the run
    after = _board(mid)
    assert [r["state"] for r in after["runs"]] == ["running"]
    assert after["refusal"] is None, f"a running step shows refused: {after['refusal']}"
    await mission_steps.stop_step(mid, "o1.posting", 1)
    ended = _board(mid)
    assert [r["state"] for r in ended["runs"]] == ["stopped"]
    assert ended["refusal"] is None, "a run that ended after the refusal no longer hides it"
    _edit_specialist(seam)
    assert not appr.admit(mid, "o1.posting", "launch")["ok"]  # after the episode ended
    reopened = _board(mid)
    assert reopened["refusal"] is not None, "an earlier episode's run hid a newer refusal"
    assert reopened["refusal"]["codes"] == ["specialist_version_changed"]


@pytest.mark.anyio
@pytest.mark.parametrize("state, retracts", [("failed", True), ("refused", False)])
async def test_a_written_run_retracts_the_refusal_notice_in_the_runner(
    monkeypatch, state, retracts
):
    """A launch admitted after the refusal whose run was WRITTEN and then failed answers the
    refusal too, not only a `started` one; a run refused at its effects started nothing."""

    async def start(mid, sid, revision, use, registry):
        return {"outcome": state, "state": state, "run": {"state": state}}

    retracted: list = []
    monkeypatch.setattr(mission_runner, "_start", start)
    monkeypatch.setattr(
        mission_runner, "retract_sync", lambda m, **kw: retracted.append((m, kw)) or 0
    )
    got = await mission_runner._offer("m-1", "T", {"id": "o1.cv", "alternates": 0}, 1, None)
    assert got == ("refused", None)
    assert retracted == ([("m-1", {"step_id": "o1.cv"})] if retracts else [])


@pytest.mark.parametrize(
    "outcome, state, retracts",
    [("started", "running", True), ("failed", "failed", True), ("refused", "refused", False)],
)
def test_a_written_run_retracts_the_refusal_notice_on_the_start_route(
    app, monkeypatch, outcome, state, retracts
):
    from test_mission_steps import _mission

    mid, _doc_ = _mission()

    async def start_step(*a, **kw):
        return {
            "outcome": outcome,
            "reason": "",
            "state": state,
            "session_key": None,
            "run": {"state": state},
        }

    retracted: list = []
    monkeypatch.setattr(mission_steps, "start_step", start_step)
    monkeypatch.setattr(
        mission_runner, "retract_sync", lambda m, **kw: retracted.append((m, kw)) or 0
    )
    r = app["c"].post(
        f"/api/missions/{mid}/steps/o1.cv/start",
        json={"revision": 1, "expect_cwd": "/tmp"},
        headers=app["hdr"],
    )
    assert r.status_code == 200, r.text
    assert retracted == ([(mid, {"step_id": "o1.cv"})] if retracts else [])


@pytest.mark.anyio
async def test_the_fence_reads_the_runs_own_approval(seam, monkeypatch):
    """`fence_verdict` re-reads the pins of the approval the run was CLAIMED under, never
    whichever approval is in force now."""
    mid = seam["mid"]
    first = _approve(mid)
    assert await mission_runner.advance(mid) == {"o1.posting": "started"}
    (run,) = missions.step_runs(mid)
    assert run["approval_id"] == first["id"]
    # Another approval in force now (a running mission is never approved again through the
    # route, so written as the store would: the first revoked, a second one in force).
    import json

    missions.revoke_approval(mid, first["id"])
    con = missions._ready(None)
    try:
        con.execute(
            "INSERT INTO mission_plan_approvals (id, mission_id, revision, digest, pins_json, "
            "operation_id, request_digest, created_at) VALUES "
            "('apr-other', ?, 1, 'd', ?, 'op-other', 'r', ?)",
            (mid, json.dumps(first["pins"]), time.time()),
        )
        con.commit()
    finally:
        con.close()
    assert missions.approval(mid)["id"] == "apr-other"
    seen: list[str] = []
    monkeypatch.setattr(missions, "step_launch_verdict", lambda plan_id: None)
    monkeypatch.setattr(
        appr, "effect_reasons", lambda approval, doc, step: seen.append(approval["id"]) or []
    )
    mission_steps.fence_verdict(run["plan_id"])
    assert seen == [first["id"]], "the fence compared against the approval in force"
    await _stop_all(seam)


@pytest.mark.anyio
async def test_the_alternates_loop_stops_when_shutdown_begins(monkeypatch):
    calls: list[tuple[str, int | None]] = []

    async def start(mid, sid, revision, use, registry):
        calls.append((use.kind, use.index))
        if use.kind == "primary":
            reasons = [{"code": "model_unavailable", "cause": "usage_exhausted", "text": "spent"}]
            raise missions.StepRefused("admission_refused", "x", reasons=reasons, new=False)
        mission_runner._closing = True  # shutdown began while alternate 0 was offered
        raise missions.StepRefused("cap", "the cap")

    monkeypatch.setattr(mission_runner, "_start", start)
    monkeypatch.setattr(mission_runner, "_notify_unless_started", lambda *a, **k: None)
    await mission_runner._offer("m-1", "T", {"id": "o1.cv", "alternates": 3}, 1, None)
    assert calls == [("primary", None), ("alternate", 0)], "an alternate was offered after drain"


@pytest.mark.anyio
async def test_the_runner_is_restartable_after_a_cancelled_drain():
    import asyncio

    hang = asyncio.ensure_future(asyncio.sleep(3600))
    mission_runner._inflight["m-1"] = hang
    draining = asyncio.ensure_future(mission_runner.drain(timeout=3600))
    await asyncio.sleep(0)
    assert mission_runner._closing
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert not mission_runner._closing, "a cancelled shutdown left the runner off"
    hang.cancel()


@pytest.mark.parametrize(
    "code, words",
    [
        ("specialist_version_changed", "specialist changed"),
        ("specialist_archived", "specialist was archived"),
        ("model_changed", "model changed"),
        ("skill_pin_stale", "skill"),
        ("playbook_revision_changed", "playbook changed"),
        ("deployment_changed", "deployment changed"),
        ("equipment_changed", "equipment changed"),
        ("plan_revision_superseded", "the plan changed"),
    ],
)
def test_class_text_words_each_code_by_its_class(code, words):
    text = appr.class_text([{"code": code, "text": "x"}])
    assert words in text, (code, text)
    if code != "plan_revision_superseded":
        assert "the plan changed" not in text, (code, text)


@pytest.mark.parametrize(
    "cause, words",
    [("usage_exhausted", "out of usage"), ("engine_not_mission_ready", "not mission-ready")],
)
@pytest.mark.anyio
async def test_the_gate_keeps_refusing_a_client_cause_and_names_it(monkeypatch, cause, words):
    """Decided on #2023 (reversible, the conservative option): the gate still refuses an allow
    while the pick's client is out of usage or not mission-ready, and says that, not "the plan
    changed" or "approve again"."""
    run = {
        "mission_id": "m-1",
        "step_id": "o1.cv",
        "authority": "approval",
        "use_kind": "primary",
        "alternate_index": None,
    }
    monkeypatch.setattr(missions, "step_run_of_session", lambda key: run)
    monkeypatch.setattr(missions, "get_mission", lambda *a, **k: {"state": "running"})
    reason = {"code": "model_unavailable", "cause": cause, "step": "o1.cv", "text": "x"}
    monkeypatch.setattr(appr, "admit", lambda *a, **k: {"ok": False, "reasons": [reason]})
    with pytest.raises(runtime.StructuredError) as e:
        await runtime._admit_gate("claude-api:x")
    assert e.value.status == 409
    assert words in str(e.value.detail), e.value.detail
    assert "approve" not in str(e.value.detail).lower().replace("approval", "")


@pytest.mark.anyio
async def test_start_step_admits_an_alternate_on_its_own_index(seam):
    """Spy (re-review of #2019 at b8e28895): the seam asks the admission with
    `alternate=use.index`, never the pick's pin."""
    seen: list[tuple[str, dict]] = []

    def admit(mid, step, boundary, **kw):
        seen.append((boundary, kw))
        return {"ok": False, "reasons": [{"code": "approval_revoked", "text": "spy"}]}

    with pytest.raises(missions.StepRefused):
        await mission_steps.start_step(
            seam["mid"],
            "o1.posting",
            revision=1,
            authority=mission_runner.APPROVAL,
            admit=admit,
            use=mission_steps.Use.alternate(1, "out of usage"),
        )
    assert seen == [("launch", {"alternate": 1})]


@pytest.mark.anyio
async def test_each_pass_retracts_notices_of_an_approval_no_longer_in_force(seam):
    """The per-pass `retract_sync` backstop in `advance`: a refusal notice raised under an
    approval that was superseded by a path that retracted nothing goes at the next pass, even
    when the pass offers no step."""
    mid = seam["mid"]
    first = _approve(mid)
    change = "sha256:" + "a" * 64
    reasons = [{"code": "grade_below_min", "text": "graded fast", "step": "o1.posting"}]
    assert missions.record_refusal(
        mid,
        approval_id=first["id"],
        step="o1.posting",
        change_digest=change,
        boundary="launch",
        reasons=reasons,
    )
    extra = {"new": True, "approval_id": first["id"], "change": change, "reasons": reasons}
    mission_runner._notify(mid, "Apply", "o1.posting", extra)
    key = mission_runner.notice_key(first["id"], change)

    def rows() -> list[str]:
        return [
            r["action_id"]
            for r in notifications.listing()["notifications"]
            if r.get("action_id") == key
        ]

    assert rows() == [key]
    store_plan(mid, seam["doc"])
    _approve(mid)  # superseded; nothing retracted here
    assert missions.set_step_hold(mid, "o1.posting", hold=True) is True
    assert rows() == [key]
    assert await mission_runner.advance(mid) == {}  # nothing offered: only the backstop ran
    assert rows() == [], "the per-pass backstop left a notice of a superseded approval"


def test_a_refused_run_never_hides_a_refusal():
    """A run refused at its effects started nothing: written after the admission's refusal, it
    still leaves that refusal standing."""
    refusal = {
        "boundary": "launch",
        "created_at": 10.0,
        "reasons": [{"code": "grade_below_min", "text": "x"}],
    }
    run = {"state": "refused", "created_at": 20.0, "ended_at": 20.0}
    assert mission_steps._refusal_view([refusal], [run]) is not None
    assert mission_steps._refusal_view([refusal], [{**run, "state": "failed"}]) is None


@pytest.mark.parametrize(
    "reason, words",
    [
        ({"code": "model_unavailable", "cause": "usage_exhausted", "text": "x"}, "out of usage"),
        (
            {"code": "approval_revoked", "consent": True, "text": "x"},
            "your standing consent no longer covers it",
        ),
    ],
)
def test_the_board_words_a_refusal_like_the_bell_and_the_gate(reason, words):
    """The board's refusal text uses the same class as the bell row and the gate: a client cause
    or the consent is named, not "its model is not available" or the approval."""
    refusal = {"boundary": "launch", "created_at": 10.0, "reasons": [reason]}
    view = mission_steps._refusal_view([refusal], [])
    assert words in view["text"], view["text"]
    assert appr.class_text([reason]) in view["text"]
