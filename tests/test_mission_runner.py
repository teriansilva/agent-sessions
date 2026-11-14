# ruff: noqa: F811 — `host` / `project` / `seam` are shared fixtures imported below
"""HQ starts the approved plan's steps (#1951 slice B): the runner under an approval in force.

On the real path (`seam`: the fake native child, a real specialist, a real project, every store
under `tmp_path`): the runner offers each ready step to #1581's one seam with
`Authority("approval")` and `mission_approval.admit`, so a stale binding or a grade drift is
REFUSED AT LAUNCH (nothing reserved, nothing claimed, no session), recorded once, shown on the
board and notified once. Plus the operator's per-step hold, the gate boundary in the one decide
door, an approved alternate's own pin, and the v42 store.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import mission_approval as appr
from agent_sessions import (
    mission_runner,
    mission_steps,
    missions,
    notifications,
)
from agent_sessions import specialists_store as sstore
from agent_sessions import structured_runtime as runtime
from agent_sessions.main import create_app
from mission_team_fixture import GRADES, store_plan
from mission_team_fixture import context as team_context
from mission_team_fixture import spec as team_spec
from test_mission_steps import (  # noqa: F401 — shared fixtures
    _assignment,
    _doc,
    _mission,
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


def _approve(mid: str) -> dict:
    """Approve the latest revision the way the route does: the server's own pins and digest."""
    latest = missions.plan_revision(mid)
    doc = latest["document"]
    p = appr.pins(doc, latest["revision"], appr.context_for(doc), appr.read_facts(doc))
    return missions.commit_approval(
        mid,
        revision=latest["revision"],
        digest=appr.digest(p),
        pins=p,
        operation_id=str(uuid.uuid4()),
        request_digest="r",
    )


def _bell(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    real = notifications.add

    def add(**kw):
        rec = real(**kw)
        if rec is not None:
            sent.append(kw)
        return rec

    monkeypatch.setattr(notifications, "add", add)
    return sent


def _no_reservation(seam) -> bool:
    keys = [r["session_key"] for r in missions.step_runs(seam["mid"]) if r.get("session_key")]
    return not keys or sstore.last_reservation(keys) is None


async def _stop_all(seam) -> None:
    import contextlib

    for r in missions.step_runs(seam["mid"]):
        if r.get("session_key"):
            with contextlib.suppress(runtime.StructuredError):
                await runtime.stop(r["session_key"])


# ---- the runner on the real path -----------------------------------------------------------------


@pytest.mark.anyio
async def test_under_an_approval_the_runner_starts_the_ready_step_through_the_seam(seam):
    approval = _approve(seam["mid"])
    out = await mission_runner.advance(seam["mid"])
    assert out == {"o1.posting": "started"}, out
    (run,) = missions.step_runs(seam["mid"])
    assert run["authority"] == "approval" and run["approval_id"] == approval["id"]
    assert run["state"] == "running" and run["use_kind"] == "primary"
    view = mission_steps.view_sync(seam["mid"])
    assert view["approval"] == {"id": approval["id"], "revision": 1}
    assert "HQ starts each agent step of revision 1" in view["runner"]
    posting = next(s for s in view["steps"] if s["id"] == "o1.posting")
    assert posting["runs"][0]["started_by"] == "started by HQ under your approval"
    # The next step waits for its evidence: the runner offers nothing that is not ready.
    assert await mission_runner.advance(seam["mid"]) == {}
    await _stop_all(seam)


@pytest.mark.anyio
async def test_nothing_starts_without_an_approval_in_force(seam):
    assert await mission_runner.advance(seam["mid"]) == {}
    approval = _approve(seam["mid"])
    missions.revoke_approval(seam["mid"], approval["id"])
    assert await mission_runner.advance(seam["mid"]) == {}
    assert missions.step_runs(seam["mid"]) == []
    assert missions.approved_missions() == []


def _edit_specialist(seam) -> None:
    cur = sstore.get(seam["spec"]["id"])
    body = {k: v for k, v in cur["current"].items() if k not in ("version", "created_at")}
    sstore.update(seam["spec"]["id"], {**body, "summary": "edited"}, expect_revision=1)
    # P5's context reads the pool: the specialist's new current version.
    specs = [team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2, version=2)]
    old = team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2)["current"]
    ctx = team_context(specs=specs)
    seam["team"]["ctx"] = type(ctx)(**{**ctx.__dict__, "pinned": {(seam["spec"]["id"], 1): old}})


def _archive_specialist(seam) -> None:
    cur = sstore.get(seam["spec"]["id"])
    sstore.set_archived(seam["spec"]["id"], True, expect_revision=cur["revision"])
    seam["team"]["ctx"] = team_context(
        specs=[team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2, archived=True)]
    )


def _grade_drift(seam) -> None:
    """#1951's acceptance: claude-api sonnet re-mapped from strong to fast after approval."""
    drifted = copy.deepcopy(GRADES)
    for engine in drifted:
        if "sonnet" in drifted[engine]:
            drifted[engine]["sonnet"] = "fast"
    seam["team"]["ctx"] = team_context(
        specs=[team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2)],
        grades=drifted,
    )


def _ungrade(seam) -> None:
    drifted = copy.deepcopy(GRADES)
    for engine in drifted:
        drifted[engine].pop("sonnet", None)
    seam["team"]["ctx"] = team_context(
        specs=[team_spec(seam["spec"]["id"], "sonnet", seam["skills"], capacity=2)],
        grades=drifted,
    )


def _cv_ready(seam) -> None:
    """o1.posting done: o1.cv and o1.cover_letter (both `strong`) are ready."""
    from test_mission_steps import _settle_evidence

    _settle_evidence(seam["mid"], seam["doc"], "o1.posting")


def _equipment(seam, monkeypatch) -> None:
    real = appr.equipment_of

    def more(pid):
        items = real(pid)
        return (
            None
            if items is None
            else [*items, {"kind": "secret", "id": "v-new", "name": "NEW", "scope": "project"}]
        )

    monkeypatch.setattr(appr, "equipment_of", more)


def _new_revision(seam) -> None:
    store_plan(seam["mid"], seam["doc"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change, code",
    [
        ("version", "specialist_version_changed"),
        ("archived", "specialist_archived"),
        ("grade", "grade_below_min"),
        ("equipment", "equipment_changed"),
        ("revision", "plan_revision_superseded"),
    ],
)
async def test_a_stale_binding_is_refused_at_launch_with_nothing_reserved_or_started(
    seam, monkeypatch, change, code
):
    """REGRESSION 1 (and 2, grade drift) AT LAUNCH: not just "stale" on the card. The refusal is
    the seam's preflight: no run, no reservation, no session; recorded once, shown on the board,
    notified once."""
    bell = _bell(monkeypatch)
    _cv_ready(seam)
    approval = _approve(seam["mid"])
    if change == "version":
        _edit_specialist(seam)
    elif change == "archived":
        _archive_specialist(seam)
    elif change == "grade":
        _grade_drift(seam)
    elif change == "equipment":
        _equipment(seam, monkeypatch)
    else:
        _new_revision(seam)
    out = await mission_runner.advance(seam["mid"], now=0.0)
    assert out.get("o1.cv") == "refused", out
    assert "started" not in out.values(), out
    assert missions.step_runs(seam["mid"]) == [], "a refused launch claimed a run"
    assert _no_reservation(seam)
    assert _users_none(seam)
    rec = [r for r in missions.refusals(seam["mid"]) if r["step"] == "o1.cv"]
    assert len(rec) == 1 and rec[0]["approval_id"] == approval["id"]
    assert rec[0]["boundary"] == "launch"
    assert code in {r["code"] for r in rec[0]["reasons"]}
    view = mission_steps.view_sync(seam["mid"])
    cv = next(s for s in view["steps"] if s["id"] == "o1.cv")
    assert cv["refusal"] is not None and code in cv["refusal"]["codes"]
    assert "Nothing was reserved, given or started" in cv["refusal"]["text"]
    mine = [b for b in bell if b["reason"].startswith("o1.cv: ")]
    assert len(mine) == 1 and mine[0]["action_id"].startswith(f"mission-refusal:{approval['id']}:")
    # Offered again after the back-off, against the same change: asked once, nothing new.
    again = await mission_runner.advance(seam["mid"], now=mission_runner.RETRY_S + 1)
    assert again.get("o1.cv") == "refused"
    assert len([r for r in missions.refusals(seam["mid"]) if r["step"] == "o1.cv"]) == 1
    assert len([b for b in bell if b["reason"].startswith("o1.cv: ")]) == 1
    assert missions.step_runs(seam["mid"]) == []


def _users_none(seam) -> bool:
    from test_mission_steps import _users

    return _users(seam["project"]) == []


@pytest.mark.anyio
async def test_grade_drift_refuses_and_the_remap_back_launches_the_same_approval(seam, monkeypatch):
    """REGRESSION 2: the grade is never pinned, so remapping it back lets the SAME approval
    launch, and the refusal's notification is retracted once the step starts."""
    bell = _bell(monkeypatch)
    _cv_ready(seam)
    approval = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _grade_drift(seam)
    out = await mission_runner.advance(seam["mid"], now=0.0)
    assert out == {"o1.cv": "refused", "o1.cover_letter": "refused"}, out
    codes = {r["code"] for x in missions.refusals(seam["mid"]) for r in x["reasons"]}
    assert codes == {"grade_below_min"}
    assert len(bell) == 2
    seam["team"]["ctx"] = good
    out = await mission_runner.advance(seam["mid"], now=mission_runner.RETRY_S + 1)
    assert out["o1.cv"] == "started", out
    run = next(r for r in missions.step_runs(seam["mid"]) if r["step_id"] == "o1.cv")
    assert run["approval_id"] == approval["id"]
    view = mission_steps.view_sync(seam["mid"])
    assert next(s for s in view["steps"] if s["id"] == "o1.cv")["refusal"] is None
    left = [
        str(r.get("action_id") or "")
        for r in notifications.listing()["notifications"]
        if str(r.get("action_id") or "").startswith("mission-refusal:")
    ]
    assert len(left) == 1, "o1.cv's refusal notification outlived its launch"
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_held_step_waits_for_the_operators_start(seam):
    _approve(seam["mid"])
    assert missions.set_step_hold(seam["mid"], "o1.posting", hold=True) is True
    assert await mission_runner.advance(seam["mid"]) == {}
    assert missions.step_runs(seam["mid"]) == []
    posting = next(
        s for s in mission_steps.view_sync(seam["mid"])["steps"] if s["id"] == "o1.posting"
    )
    assert posting["held"] and posting["can_start"]
    assert posting["hold_text"].startswith("Ready, and waiting for you")
    # Lifting it starts nothing by itself; the runner's next pass does.
    assert missions.set_step_hold(seam["mid"], "o1.posting", hold=False) is False
    assert missions.step_runs(seam["mid"]) == []
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_pause_beats_the_runner(seam):
    """#1581's fence order holds under the runner: an operator's pause refuses every start."""
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    await mission_steps.pause(seam["mid"])
    await _stop_all(seam)
    run = missions.step_runs(seam["mid"])[0]
    missions.finish_step_run(
        seam["mid"], "o1.posting", 1, to="stopped", detail="t", expect="running"
    )
    from test_mission_steps import _settle_evidence

    _settle_evidence(seam["mid"], seam["doc"], "o1.posting")
    assert run["state"] == "running"
    assert await mission_runner.advance(seam["mid"]) == {}, "a paused mission started a step"
    assert len(missions.step_runs(seam["mid"])) == 1
    await mission_steps.resume(seam["mid"])
    out = await mission_runner.advance(seam["mid"])
    assert "started" in out.values(), out
    assert len(missions.step_runs(seam["mid"])) == 2
    await _stop_all(seam)


@pytest.mark.anyio
async def test_the_sweep_runs_one_task_per_approved_mission(seam, monkeypatch):
    _approve(seam["mid"])
    seen: list[str] = []

    async def advance(mid, registry=None, **kw):
        seen.append(mid)
        return {}

    monkeypatch.setattr(mission_runner, "advance", advance)
    assert await mission_runner.sweep() == 1
    for t in list(mission_runner._inflight.values()):
        await t
    assert seen == [seam["mid"]]
    monkeypatch.setenv("AGENT_SESSIONS_MISSION_RUNNER", "0")
    assert await mission_runner.sweep() == 0


# ---- alternates (D6): offered only on a client or usage refusal, each on its own pin -----------


@pytest.mark.anyio
async def test_an_alternate_is_offered_only_when_the_pick_cannot_run_on_its_client(monkeypatch):
    calls: list[tuple[str, int | None]] = []

    async def start(mid, sid, revision, use, registry):
        calls.append((use.kind, use.index))
        if use.kind == "primary":
            raise missions.StepRefused(
                "admission_refused",
                "x",
                reasons=[
                    {"code": "model_unavailable", "cause": "usage_exhausted", "text": "spent"}
                ],
                new=True,
            )
        if use.index == 0:
            raise missions.StepRefused(
                "admission_refused",
                "y",
                reasons=[{"code": "specialist_version_changed", "text": "v1 → v2"}],
                new=True,
                change="sha256:" + "a" * 64,
                approval_id="apr-" + "1" * 20,
            )
        return {"outcome": "started"}

    monkeypatch.setattr(mission_runner, "_start", start)
    notified: list = []
    monkeypatch.setattr(mission_runner, "_notify", lambda *a, **k: notified.append(a))
    monkeypatch.setattr(mission_runner, "retract_sync", lambda *a, **k: 0)
    got = await mission_runner._offer("m", "T", {"id": "o1.cv", "alternates": 2}, 1, None)
    assert got == ("started", None)
    assert calls == [("primary", None), ("alternate", 0), ("alternate", 1)]
    assert notified == [], "a refusal was notified although an alternate ran in its place"

    calls.clear()

    async def full(mid, sid, revision, use, registry):
        calls.append((use.kind, use.index))
        raise missions.StepRefused(
            "admission_refused",
            "x",
            reasons=[{"code": "grade_below_min", "text": "graded fast"}],
            new=True,
        )

    monkeypatch.setattr(mission_runner, "_start", full)
    # Refused with nothing claimed: what it was refused for keys the back-off (no change digest
    # here, so its code).
    assert await mission_runner._offer("m", "T", {"id": "o1.cv", "alternates": 2}, 1, None) == (
        "refused",
        "admission_refused",
    )
    assert calls == [("primary", None)], "an alternate replaced a pick that was not blocked"
    assert len(notified) == 1


def test_an_alternate_is_rechecked_on_its_own_pin():
    """`admit(…, alternate=i)` re-checks that approved alternate, never the pick, and fails
    closed for an alternate the approval did not enumerate."""
    from mission_team_fixture import context, pool, spec
    from test_mission_approval import approved, assembled, bump, codes, facts, with_old

    lite = spec("cv-writer-lite", "sonnet", ["cv-writing"], capacity=1)
    specs = [*pool(), lite]
    doc = assembled(context(specs=specs))
    cv = next(s for s in doc["steps"] if s["id"] == "o1.cv")
    cv["envelope"]["assignment"]["alternates"] = [
        {"specialist": {"id": "cv-writer-lite", "version": 1}, "reason": "t"}
    ]
    ctx = context(specs=specs)
    a = approved(doc=doc, ctx=ctx)
    assert appr.recheck(a, doc, 1, ctx, facts(), step="o1.cv", alternate=0)["ok"]
    old = bump(specs, "cv-writer-lite", summary="edited")
    moved = with_old(context(specs=specs), "cv-writer-lite", old)
    out = appr.recheck(a, doc, 1, moved, facts(), step="o1.cv", alternate=0)
    assert codes(out) == ["specialist_version_changed"]
    assert appr.recheck(a, doc, 1, moved, facts(), step="o1.cv")["ok"], "the pick did not change"
    assert codes(appr.recheck(a, doc, 1, ctx, facts(), step="o1.cv", alternate=3)) == [
        "approval_revoked"
    ]


# ---- the gate boundary in the one decide door ----------------------------------------------------


@pytest.mark.anyio
async def test_a_gate_in_a_step_session_under_a_stale_approval_is_refused_and_a_decline_is_not(
    seam, monkeypatch
):
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    (run,) = missions.step_runs(seam["mid"])
    key = run["session_key"]
    asked = []

    async def adapter_decide(*a, **kw):
        asked.append(a[4])
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    with monkeypatch.context() as m:
        m.setattr(runtime, "_adapter", lambda engine, op: Fake())
        _ungrade(seam)
        with pytest.raises(runtime.StructuredError) as e:
            await runtime.decide(
                key,
                turn_id="t",
                request_id="r",
                decision_id=str(uuid.uuid4()),
                decision="approve",
                user="u",
            )
        assert e.value.status == 409 and asked == []
        rec = missions.refusals(seam["mid"])
        assert rec and rec[-1]["boundary"] == "gate"
        await runtime.decide(
            key,
            turn_id="t",
            request_id="r",
            decision_id=str(uuid.uuid4()),
            decision="reject",
            user="u",
        )
        assert asked == ["reject"], "a decline was held up by the gate"
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_session_that_runs_no_approved_step_is_not_gated(seam, monkeypatch):
    out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)  # the operator's
    key = out["run"]["session_key"]
    called = []
    monkeypatch.setattr(appr, "admit", lambda *a, **k: called.append(a) or {"ok": False})

    async def adapter_decide(*a, **kw):
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    with monkeypatch.context() as m:
        m.setattr(runtime, "_adapter", lambda engine, op: Fake())
        await runtime.decide(
            key,
            turn_id="t",
            request_id="r",
            decision_id=str(uuid.uuid4()),
            decision="approve",
            user="u",
        )
    assert called == []
    await _stop_all(seam)


@pytest.mark.anyio
@pytest.mark.parametrize("use_kind", ["primary", "alternate"])
async def test_a_gate_in_an_alternates_session_checks_the_alternates_own_pin(monkeypatch, use_kind):
    """Independent review of #2019: an alternate's session is gated on ITS pin. Both directions:
    the pick out of quota (its pin refuses) must not refuse the alternate's gate, and a stale
    alternate (its own pin refuses) must refuse even while the pick is fine."""
    run = {
        "mission_id": "m-1",
        "step_id": "o1.cv",
        "authority": "approval",
        "use_kind": use_kind,
        "alternate_index": 0 if use_kind == "alternate" else None,
    }
    monkeypatch.setattr(missions, "step_run_of_session", lambda key: run)
    monkeypatch.setattr(missions, "get_mission", lambda *a, **k: {"state": "running"})
    seen: list[dict] = []

    def admit(mid, step, boundary, **kw):
        seen.append(kw)
        alt = kw.get("alternate")
        # The pick is out of quota; the alternate is fine.
        ok = alt == 0
        reasons = [] if ok else [{"code": "model_unavailable", "text": "out of quota"}]
        return {"ok": ok, "reasons": reasons}

    monkeypatch.setattr(appr, "admit", admit)
    if use_kind == "alternate":
        await runtime._admit_gate("claude-api:x")  # admitted on its own pin
        assert seen == [{"alternate": 0}]

        def stale_alt(mid, step, boundary, **kw):
            seen.append(kw)
            alt = kw.get("alternate")
            ok = alt is None  # now the pick is fine and the alternate is stale
            return {
                "ok": ok,
                "reasons": []
                if ok
                else [{"code": "specialist_version_changed", "text": "v1 to v2"}],
            }

        monkeypatch.setattr(appr, "admit", stale_alt)
        with pytest.raises(runtime.StructuredError) as e:
            await runtime._admit_gate("claude-api:x")
        assert e.value.status == 409 and seen[-1] == {"alternate": 0}
        assert "can't be approved again" in str(e.value.detail)
    else:
        with pytest.raises(runtime.StructuredError):
            await runtime._admit_gate("claude-api:x")
        assert seen == [{}]


# ---- the hold route and the v42 store ------------------------------------------------------------


@pytest.fixture
def app(auth_cfg):
    missions.reset_schema_cache_for_test()
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    yield {"c": c, "hdr": {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}}
    missions.reset_schema_cache_for_test()


def test_the_hold_route_is_the_operators_with_csrf_and_exact_keys(app):
    mid, _doc_ = _mission()
    url = f"/api/missions/{mid}/steps/o1.cv/hold"
    assert app["c"].post(url, json={"hold": True}).status_code == 403  # no CSRF / Origin
    r = app["c"].post(url, json={"hold": True}, headers=app["hdr"])
    assert r.status_code == 200 and r.json() == {"step_id": "o1.cv", "held": True}
    assert r.headers["cache-control"].startswith("no-store")
    assert missions.step_holds(mid) == {"o1.cv"}
    assert app["c"].post(url, json={"hold": 1}, headers=app["hdr"]).status_code == 422
    assert app["c"].post(url, json={"hold": True, "x": 1}, headers=app["hdr"]).status_code == 422
    bad = f"/api/missions/{mid}/steps/o9.nope/hold"
    assert app["c"].post(bad, json={"hold": True}, headers=app["hdr"]).status_code == 404
    r = app["c"].post(url, json={"hold": False}, headers=app["hdr"])
    assert r.json()["held"] is False and missions.step_holds(mid) == set()


def test_the_schema_is_v42_and_rolls_back_to_41(tmp_path):
    import sqlite3

    db = tmp_path / "missions.sqlite3"
    missions._ready(db).close()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 42
    assert con.execute("SELECT name FROM sqlite_master WHERE name='mission_step_holds'").fetchone()
    con.close()
    assert missions.rollback_schema(41, path=db) == {"from": 42, "to": 41}
    missions.reset_schema_cache_for_test()
    missions._ready(db).close()  # re-opening converges
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 42
    con.close()


def test_holds_leave_with_their_mission():
    mid, _doc_ = _mission()
    missions.set_step_hold(mid, "o1.cv", hold=True)
    assert missions.delete_mission(mid)
    con = missions._ready(None)
    try:
        assert con.execute("SELECT COUNT(*) FROM mission_step_holds").fetchone()[0] == 0
    finally:
        con.close()


# ---- the preflight → effect window (Hermes 128628) ----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("at", ["create", "brief"])
async def test_an_equipment_change_after_the_admission_refuses_the_effect(seam, monkeypatch, at):
    """A pin outside the specialist that moves AFTER `admit` passed (the project's equipment) is
    re-read inside the write fence at the create and at the brief: nothing starts, or the
    session started for the create is stopped before any brief."""
    from agent_sessions import mission_structured
    from test_mission_steps import _users

    _approve(seam["mid"])
    real_create = mission_structured.runtime.create_session

    async def create(*a, **kw):
        if at == "create":
            _equipment(seam, monkeypatch)
            return await real_create(*a, **kw)
        snap = await real_create(*a, **kw)
        _equipment(seam, monkeypatch)
        return snap

    monkeypatch.setattr(mission_structured.runtime, "create_session", create)
    out = await mission_runner.advance(seam["mid"])
    assert out == {"o1.posting": "refused"}, out
    (run,) = missions.step_runs(seam["mid"])
    assert run["state"] in (("refused",) if at == "create" else ("failed", "unknown"))
    assert _users(seam["project"]) == [], "a brief went out after the equipment changed"
    if at == "brief":
        assert (await runtime.probe(run["session_key"]))["containment"] == "gone"


def test_effect_reasons_cover_the_deployment_and_the_project():
    """The fence's half of the pins outside the specialist: a re-deployed playbook, a moved
    deployment and an unusable project all refuse the effect."""
    from test_mission_approval import _dep, _deployed, approved, assembled, codes, facts

    doc = _deployed(assembled())
    a = approved(doc=doc, f=facts(deployment=_dep()))
    seen = {}

    def read(d):
        return seen["facts"]

    import agent_sessions.mission_approval as m

    real = m.read_facts
    try:
        m.read_facts = read
        seen["facts"] = facts(deployment=_dep())
        assert m.effect_reasons(a, doc, "o1.cv") == []
        seen["facts"] = facts(deployment=_dep(revision="f" * 40))
        assert "playbook_revision_changed" in codes({"reasons": m.effect_reasons(a, doc, "o1.cv")})
        seen["facts"] = facts(deployment=_dep(generation=9))
        assert codes({"reasons": m.effect_reasons(a, doc, "o1.cv")}) == ["deployment_changed"]
        seen["facts"] = facts(usable=False, deployment=_dep())
        assert codes({"reasons": m.effect_reasons(a, doc, "o1.cv")}) == ["project_unavailable"]
    finally:
        m.read_facts = real


@pytest.mark.anyio
async def test_a_hold_after_the_runner_read_the_step_still_stops_its_start(seam, monkeypatch):
    """The per-step hold has the pause's fence: one landing after the runner read the step as
    ready is refused by the claim, and one landing after the claim is refused at the effects."""
    from agent_sessions import mission_structured
    from test_mission_steps import _users

    _approve(seam["mid"])
    real_offers = mission_runner.offers_sync

    def offers_then_hold(mid):
        out = real_offers(mid)
        missions.set_step_hold(mid, "o1.posting", hold=True)  # lands after the read
        return out

    monkeypatch.setattr(mission_runner, "offers_sync", offers_then_hold)
    out = await mission_runner.advance(seam["mid"])
    assert out == {"o1.posting": "refused"}, out
    assert missions.step_runs(seam["mid"]) == [], "a held step was claimed"
    monkeypatch.setattr(mission_runner, "offers_sync", real_offers)
    missions.set_step_hold(seam["mid"], "o1.posting", hold=False)
    mission_runner.reset_for_test()
    real_create = mission_structured.runtime.create_session

    async def hold_then_create(*a, **kw):
        missions.set_step_hold(seam["mid"], "o1.posting", hold=True)  # lands after the claim
        return await real_create(*a, **kw)

    monkeypatch.setattr(mission_structured.runtime, "create_session", hold_then_create)
    out = await mission_runner.advance(seam["mid"])
    assert out == {"o1.posting": "refused"}, out
    (run,) = missions.step_runs(seam["mid"])
    assert run["state"] == "refused" and _users(seam["project"]) == []
    # The operator's own Start is never held.
    missions.set_step_hold(seam["mid"], "o1.posting", hold=False)
    monkeypatch.setattr(mission_structured.runtime, "create_session", real_create)
    missions.set_step_hold(seam["mid"], "o1.posting", hold=True)
    started = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert started["outcome"] == "started", started
    await _stop_all(seam)


def test_the_operators_own_start_retracts_that_steps_refusal_notice(app, monkeypatch):
    """Independent review of #2019: "Start step myself" ends the refusal's notification too."""
    mid, _doc_ = _mission()

    async def started(*a, **kw):
        return {"outcome": "started", "reason": "", "state": "running", "session_key": None}

    retracted: list = []
    monkeypatch.setattr(mission_steps, "start_step", started)
    monkeypatch.setattr(
        mission_runner, "retract_sync", lambda m, **kw: retracted.append((m, kw)) or 0
    )
    r = app["c"].post(
        f"/api/missions/{mid}/steps/o1.cv/start",
        json={"revision": 1, "expect_cwd": "/tmp"},
        headers=app["hdr"],
    )
    assert r.status_code == 200, r.text
    assert retracted == [(mid, {"step_id": "o1.cv"})]


@pytest.mark.anyio
async def test_a_gate_refusal_never_swallows_the_next_launchs_refusal(seam, monkeypatch):
    """Independent re-review of #2019: the refusal key carries the boundary and the episode. A
    gate refusal never swallows a launch refusal of the SAME change: each is its own decision."""
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"], now=0.0) == {"o1.posting": "started"}
    (run,) = missions.step_runs(seam["mid"])

    async def adapter_decide(*a, **kw):
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    _ungrade(seam)
    with monkeypatch.context() as m:
        m.setattr(runtime, "_adapter", lambda engine, op: Fake())
        with pytest.raises(runtime.StructuredError):
            await runtime.decide(
                run["session_key"],
                turn_id="t",
                request_id="r",
                decision_id=str(uuid.uuid4()),
                decision="approve",
                user="u",
            )
    # The same change at a LAUNCH boundary is its own decision (the key carries the boundary).
    launch = appr.admit(seam["mid"], "o1.posting", "launch")
    assert not launch["ok"] and launch["new"], launch
    rec = missions.refusals(seam["mid"])
    assert [r["boundary"] for r in rec] == ["gate", "launch"]
    await _stop_all(seam)


@pytest.mark.anyio
async def test_the_runner_never_turns_a_stop_into_a_restart(seam):
    """Independent re-review of #2019: HQ starts each step ONCE under an approval. A step the
    operator stopped stays stopped; the board says to start it again yourself."""
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    stopped = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert stopped["state"] == "stopped", stopped
    assert await mission_runner.advance(seam["mid"], now=1e9) == {}
    assert len(missions.step_runs(seam["mid"])) == 1
    posting = next(
        s for s in mission_steps.view_sync(seam["mid"])["steps"] if s["id"] == "o1.posting"
    )
    assert posting["runner_text"] == RAN_TEXT
    assert posting["can_start"]  # the operator's own Start still works
    out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert out["outcome"] == "started"
    await _stop_all(seam)


RAN_TEXT = "This step has run already, and HQ starts a step only once; start it again yourself."


async def _runner_start(seam, step: str = "o1.posting") -> dict:
    """A launch nobody tapped for, straight at the seam: what a second app instance's runner
    would send, past any offer filter of this one."""
    return await mission_steps.start_step(
        seam["mid"], step, revision=1, authority=mission_runner.APPROVAL, admit=appr.admit
    )


@pytest.mark.anyio
async def test_a_step_the_operator_started_and_stopped_is_never_restarted_by_hq(seam):
    """Independent re-review of #2019 (blocking): "each step once" counts the operator's own
    runs too. The offer filter skips the step, and the claim itself refuses (`already_ran`)."""
    _approve(seam["mid"])
    missions.set_step_hold(seam["mid"], "o1.posting", hold=True)  # the runner leaves it to me
    out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert out["outcome"] == "started" and out["run"]["authority"] == "operator"
    missions.set_step_hold(seam["mid"], "o1.posting", hold=False)
    stopped = await mission_steps.stop_step(seam["mid"], "o1.posting", 1)
    assert stopped["state"] == "stopped", stopped
    assert await mission_runner.advance(seam["mid"], now=1e9) == {}
    with pytest.raises(missions.StepRefused) as e:
        await _runner_start(seam)
    assert e.value.code == "already_ran"
    assert [r["state"] for r in missions.step_runs(seam["mid"])] == ["stopped"]
    posting = next(
        s for s in mission_steps.view_sync(seam["mid"])["steps"] if s["id"] == "o1.posting"
    )
    assert posting["runner_done"] and posting["runner_text"] == RAN_TEXT
    # The operator's own Start is not fenced: their tap is its authority.
    again = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert again["outcome"] == "started" and again["run"]["episode"] == 2
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_step_whose_operator_launch_failed_is_never_restarted_by_hq(seam, monkeypatch):
    from agent_sessions import mission_structured

    _approve(seam["mid"])
    missions.set_step_hold(seam["mid"], "o1.posting", hold=True)

    async def broken(*a, **kw):
        raise RuntimeError("the client fell over")

    with monkeypatch.context() as m:  # never `monkeypatch.undo()`: it drops the seam's pins
        m.setattr(mission_structured.runtime, "create_session", broken)
        out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert out["outcome"] == "failed", out
    (run,) = missions.step_runs(seam["mid"])
    assert run["authority"] == "operator", run
    if run["state"] == "unknown":  # its record was kept: a proved stop answers it, as `failed`
        assert missions.clear_dispatch(seam["mid"], expect_plan=run["plan_id"], stopped=True)
    (run,) = missions.step_runs(seam["mid"])
    assert run["state"] == "failed", run
    missions.set_step_hold(seam["mid"], "o1.posting", hold=False)
    assert await mission_runner.advance(seam["mid"], now=1e9) == {}
    with pytest.raises(missions.StepRefused) as e:
        await _runner_start(seam)
    assert e.value.code == "already_ran"
    assert len(missions.step_runs(seam["mid"])) == 1


@pytest.mark.anyio
async def test_the_runner_line_says_nothing_starts_while_orchestration_is_off(seam):
    from agent_sessions import prefs

    _approve(seam["mid"])
    prefs.set_orchestrator({"enabled": False})
    view = mission_steps.view_sync(seam["mid"])
    assert view["runner"].startswith("HQ starts no step while orchestration")


@pytest.mark.anyio
async def test_a_step_that_keeps_being_refused_backs_off(monkeypatch, seam):
    calls: list[float] = []

    async def offer(mid, title, step, revision, registry):
        calls.append(1.0)
        return "refused", None  # refused at its effects

    approval = _approve(seam["mid"])
    monkeypatch.setattr(mission_runner, "_offer", offer)
    t = 0.0
    waits = []
    for _ in range(9):
        await mission_runner.advance(seam["mid"], now=t)
        nxt = mission_runner._retry_at[(seam["mid"], "o1.posting", approval["id"])]
        waits.append(nxt - t)
        t = nxt + 0.1
    assert waits[:3] == pytest.approx([60.0, 120.0, 240.0])
    assert max(waits) == pytest.approx(mission_runner.RETRY_MAX_S)


@pytest.mark.anyio
async def test_an_admission_refusal_backs_off_minutes_and_a_new_change_starts_again(
    monkeypatch, seam
):
    """Independent re-review of #2019: a refusal that claimed nothing backs off at most
    `ADMISSION_RETRY_MAX_S`, so a binding remapped back launches within minutes; a refusal for a
    different change starts the back-off again."""
    change = {"what": "sha256:a"}

    async def offer(mid, title, step, revision, registry):
        return "refused", change["what"]

    approval = _approve(seam["mid"])
    monkeypatch.setattr(mission_runner, "_offer", offer)
    key = (seam["mid"], "o1.posting", approval["id"])
    t, waits = 0.0, []
    for _ in range(6):
        await mission_runner.advance(seam["mid"], now=t)
        waits.append(mission_runner._retry_at[key] - t)
        t = mission_runner._retry_at[key] + 0.1
    assert waits == pytest.approx([60.0, 120.0, 240.0, 300.0, 300.0, 300.0])
    change["what"] = "sha256:b"
    await mission_runner.advance(seam["mid"], now=t)
    assert mission_runner._retry_at[key] - t == pytest.approx(mission_runner.RETRY_S)


@pytest.mark.anyio
async def test_a_new_approval_starts_a_fresh_back_off(monkeypatch, seam):
    async def offer(mid, title, step, revision, registry):
        return "refused", None

    first = _approve(seam["mid"])
    monkeypatch.setattr(mission_runner, "_offer", offer)
    t = 0.0
    for _ in range(4):
        await mission_runner.advance(seam["mid"], now=t)
        t = mission_runner._retry_at[(seam["mid"], "o1.posting", first["id"])] + 0.1
    missions.revoke_approval(seam["mid"], first["id"])
    second = _approve(seam["mid"])
    assert second["id"] != first["id"]
    out = await mission_runner.advance(seam["mid"], now=t)
    assert out == {"o1.posting": "refused"}, out
    key = (seam["mid"], "o1.posting", second["id"])
    assert mission_runner._retry_at[key] - t == pytest.approx(mission_runner.RETRY_S)


@pytest.mark.anyio
async def test_a_drain_that_begins_while_the_worklist_is_read_starts_no_pass(seam, monkeypatch):
    """The in-loop `_closing` re-check: shutdown that begins while `approved_missions` is being
    read (off the loop) starts no `advance` for anything that read returned."""
    import asyncio

    _approve(seam["mid"])
    real = missions.approved_missions

    def read_then_drain(*a, **kw):
        out = real(*a, **kw)
        asyncio.run(mission_runner.drain())  # this worker thread: shutdown begins mid-read
        return out

    monkeypatch.setattr(missions, "approved_missions", read_then_drain)
    assert await mission_runner.sweep() == 0
    assert mission_runner._inflight == {}
    assert missions.step_runs(seam["mid"]) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "switch", ["AGENT_SESSIONS_MISSION_RUNNER", "AGENT_SESSIONS_MISSION_SUPERVISOR"]
)
async def test_the_runner_line_says_so_when_a_host_switch_turns_the_runner_off(
    seam, monkeypatch, switch
):
    _approve(seam["mid"])
    monkeypatch.setenv(switch, "0")
    view = mission_steps.view_sync(seam["mid"])
    assert view["runner"].startswith("HQ starts no step: starting steps by itself is switched off")
    # Only the runner is off: the operator's Start is not.
    posting = next(s for s in view["steps"] if s["id"] == "o1.posting")
    assert posting["can_start"]


@pytest.mark.anyio
async def test_every_decision_but_a_decline_is_gated(seam, monkeypatch):
    """The gate door is a deny list (independent re-review of #2019): a decision kind it does not
    know is gated like `approve`; only `reject` and `cancel` skip the re-check."""
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    (run,) = missions.step_runs(seam["mid"])
    gated: list[str] = []
    asked: list[str] = []

    async def gate(key):
        gated.append(key)
        raise runtime.StructuredError(409, "refused", reason="approval_refused")

    async def adapter_decide(*a, **kw):
        asked.append(a[4])
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    with monkeypatch.context() as m:
        m.setattr(runtime, "_admit_gate", gate)
        m.setattr(runtime, "_adapter", lambda engine, op: Fake())
        for decision in ("approve", "always", "answer", "something-new", "reject", "cancel"):
            call = runtime.decide(
                run["session_key"],
                turn_id="t",
                request_id="r",
                decision_id=str(uuid.uuid4()),
                decision=decision,
                user="u",
            )
            if decision in ("reject", "cancel"):
                await call
            else:
                with pytest.raises(runtime.StructuredError):
                    await call
    assert len(gated) == 4 and asked == ["reject", "cancel"]
    await _stop_all(seam)


@pytest.mark.anyio
async def test_no_pass_starts_after_drain(seam):
    _approve(seam["mid"])
    await mission_runner.drain()
    assert await mission_runner.sweep() == 0


# ---- the fifth independent review of #2019 ------------------------------------------------------


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
@pytest.mark.parametrize("broken", ["membership", "recheck"])
async def test_the_gate_fails_closed_when_it_cannot_read_or_recheck(seam, monkeypatch, broken):
    """An allow is refused with a 503, and never reaches the client, when the step's run can't be
    read (any API session) or the approval can't be re-checked (an approved step's session). A
    decline still goes through."""
    _approve(seam["mid"])
    assert await mission_runner.advance(seam["mid"]) == {"o1.posting": "started"}
    (run,) = missions.step_runs(seam["mid"])
    asked: list[str] = []

    async def adapter_decide(*a, **kw):
        asked.append(a[4])
        return {"ok": True}

    class Fake:
        decide = staticmethod(adapter_decide)

    def boom(*a, **kw):
        raise RuntimeError("the store is unreadable")

    with monkeypatch.context() as m:
        m.setattr(runtime, "_adapter", lambda engine, op: Fake())
        if broken == "membership":
            m.setattr(missions, "step_run_of_session", boom)
        else:
            m.setattr(appr, "admit", boom)
        for decision in ("approve", "always", "answer"):
            with pytest.raises(runtime.StructuredError) as e:
                await _decide(run["session_key"], decision)
            assert e.value.status == 503, (decision, e.value)
        assert asked == [], "an allow reached the client although the gate could not decide"
        await _decide(run["session_key"], "reject")
        await _decide(run["session_key"], "cancel")
        assert asked == ["reject", "cancel"]
    await _stop_all(seam)


@pytest.mark.anyio
async def test_the_board_shows_the_refusal_made_last_when_a_change_comes_back(seam):
    """drift → ungrade → drift: the board names the grade drift again, not the ungraded step
    (a repeat moves the record's time, and is still not a new decision)."""
    _cv_ready(seam)  # o1.cv's minimum grade is `strong`: a drift to `fast` refuses it
    _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _grade_drift(seam)
    assert appr.admit(seam["mid"], "o1.cv", "launch", now=1000.0)["new"]
    _ungrade(seam)
    assert appr.admit(seam["mid"], "o1.cv", "launch", now=2000.0)["new"]
    _grade_drift(seam)
    again = appr.admit(seam["mid"], "o1.cv", "launch", now=3000.0)
    assert not again["ok"] and not again["new"], again
    cv = next(s for s in mission_steps.view_sync(seam["mid"])["steps"] if s["id"] == "o1.cv")
    assert cv["refusal"]["codes"] == ["grade_below_min"], cv["refusal"]
    assert cv["refusal"]["at"] == 3000.0
    assert len(missions.refusals(seam["mid"])) == 2
    seam["team"]["ctx"] = good


@pytest.mark.anyio
async def test_a_change_that_returns_after_the_step_ran_again_is_a_new_decision(seam):
    """The episode half of the refusal key: the same stale binding, refused before and after the
    step ran once more, is two decisions (and two notices), never one swallowing the other."""
    _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    first = appr.admit(seam["mid"], "o1.posting", "launch")
    assert first["new"]
    assert not appr.admit(seam["mid"], "o1.posting", "launch")["new"]  # same episode: one decision
    seam["team"]["ctx"] = good
    out = await mission_steps.start_step(seam["mid"], "o1.posting", revision=1)
    assert out["outcome"] == "started"
    assert (await mission_steps.stop_step(seam["mid"], "o1.posting", 1))["state"] == "stopped"
    _ungrade(seam)
    after = appr.admit(seam["mid"], "o1.posting", "launch")
    assert after["new"] and after["change"] != first["change"], after
    seam["team"]["ctx"] = good
    await _stop_all(seam)


@pytest.mark.anyio
async def test_a_drain_during_a_pass_offers_no_further_step(seam, monkeypatch):
    _cv_ready(seam)
    _approve(seam["mid"])
    offered: list[str] = []

    async def offer(mid, title, step, revision, registry):
        offered.append(step["id"])
        mission_runner._closing = True  # what `drain` sets first, mid-pass
        return "started", None

    monkeypatch.setattr(mission_runner, "_offer", offer)
    out = await mission_runner.advance(seam["mid"])
    assert offered == ["o1.cv"] and out == {"o1.cv": "started"}, (offered, out)


def _operator_claims(seam, step: str = "o1.posting") -> None:
    """The operator's Start, at the store: its run exists from its claim on."""
    from test_mission_steps import _claim

    _claim(seam["mid"], step, cwd=str(seam["cwd"]))


@pytest.mark.anyio
@pytest.mark.parametrize("when", ["before", "after"])
async def test_a_refusal_raced_by_the_operators_start_leaves_no_notice(seam, monkeypatch, when):
    """The runner read the step ready; the operator started it; then the runner's admission was
    refused. Nothing is left to answer: no notice is raised (`before` the check), or the one
    raised is retracted (`after`: the Start landed while the notice was being raised)."""
    bell = _bell(monkeypatch)
    _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)
    real_start, real_notify = mission_runner._start, mission_runner._notify

    async def start(mid, sid, revision, use, registry):
        if when == "before":
            _operator_claims(seam, sid)
        return await real_start(mid, sid, revision, use, registry)

    def notify(mid, title, sid, extra):
        real_notify(mid, title, sid, extra)
        if when == "after":
            _operator_claims(seam, sid)

    monkeypatch.setattr(mission_runner, "_start", start)
    monkeypatch.setattr(mission_runner, "_notify", notify)
    out = await mission_runner.advance(seam["mid"], now=0.0)
    assert out == {"o1.posting": "refused"}, out
    assert missions.refusals(seam["mid"]), "the admission did not refuse"
    left = [
        r
        for r in notifications.listing()["notifications"]
        if str(r.get("action_id") or "").startswith("mission-refusal:")
    ]
    assert left == [], "a refusal notice outlived the operator's own Start"
    assert len(bell) == (0 if when == "before" else 1)
    seam["team"]["ctx"] = good


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["revoke", "supersede"])
@pytest.mark.parametrize("when", ["before", "racing"])
async def test_a_refusal_notice_never_outlives_the_approval_it_was_recorded_under(
    seam, monkeypatch, how, when
):
    """Hermes 8155: the refusal was recorded, then the approval was revoked or superseded and its
    route retracted what existed, all before the queued notice was raised. The notice is not
    raised (`before`: the change landed before the check), or it is retracted (`racing`: the
    route's retraction ran between the check and the raise)."""
    bell = _bell(monkeypatch)
    first = _approve(seam["mid"])
    good = seam["team"]["ctx"]
    _ungrade(seam)

    def end_the_approval() -> None:
        if how == "revoke":
            missions.revoke_approval(seam["mid"], first["id"])
        else:
            _new_revision(seam)
            assert _approve(seam["mid"])["id"] != first["id"]
        mission_runner.retract_sync(seam["mid"])  # what both routes do after their commit

    real_start, real_notify = mission_runner._start, mission_runner._notify

    async def start(mid, sid, revision, use, registry):
        try:
            return await real_start(mid, sid, revision, use, registry)
        except missions.StepRefused:
            if when == "before":
                end_the_approval()
            raise

    def notify(mid, title, sid, extra):
        if when == "racing":
            end_the_approval()
        real_notify(mid, title, sid, extra)

    monkeypatch.setattr(mission_runner, "_start", start)
    monkeypatch.setattr(mission_runner, "_notify", notify)
    out = await mission_runner.advance(seam["mid"], now=0.0)
    assert out == {"o1.posting": "refused"}, out
    assert any(r["approval_id"] == first["id"] for r in missions.refusals(seam["mid"]))
    left = [
        r
        for r in notifications.listing()["notifications"]
        if str(r.get("action_id") or "").startswith("mission-refusal:")
    ]
    assert left == [], "a refusal notice outlived the approval it was recorded under"
    assert len(bell) == (0 if when == "before" else 1)
    seam["team"]["ctx"] = good
