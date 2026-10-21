"""The probe runner and the settlement it writes (#891, Phase 5b of #840).

Three things are pinned here, and they are the three the feature is worthless without:

1. **`met` is only ever written from an observation.** The operator's edit path refuses
   `state`/`met_at`/`observed` outright; this is the other side of that rule, and a test asserts
   the model half cannot reach it either.
2. **`unknown` settles nothing and is visibly stale.** A forge outage must not look like the work
   going backwards, and a probe that could not run must not re-serve its last answer as current.
3. **The stale-200.** `service_live` and `change_live` are two questions, and a 200 answers only
   the first. A target that answered 200 before a deploy and a stale instance still answering 200
   after it both leave a revision objective unsettled.
"""

from __future__ import annotations

import httpx
import pytest

from agent_sessions import forge, gitpanel, mission_probes, missions, prefs

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    missions.reset_schema_cache_for_test()
    yield tmp_path / "missions.db"
    missions.reset_schema_cache_for_test()


@pytest.fixture(autouse=True)
def _no_network():
    yield
    forge.set_transport_for_test(None)
    mission_probes.set_transport_for_test(None)


def _mission(**kw):
    m = missions.create_mission(kw.pop("instruction", "do the thing"), cwd=kw.pop("cwd", "/repo"))
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    return m


def _add(mid, key, probe, args=None, gate=True):
    missions.patch_objectives(
        mid,
        [{"op": "add", "key": key, "title": key, "probe": probe, "probe_args": args, "gate": gate}],
    )


# ---- the settlement writer ------------------------------------------------------


def test_an_observation_settles_and_records_what_settled_it(store):
    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    row = missions.observe_objective(
        m["id"], "pr_open", observed=True, value=True, detail="PR #7 is open", extra={"number": 7}
    )
    assert row["state"] == "met" and row["met_at"]
    # The FACT is kept on the row, not just the flag — an operator reading "met" months later
    # should be able to see what was seen.
    assert row["observed"]["detail"] == "PR #7 is open"
    assert row["observed"]["number"] == 7


def test_could_not_look_settles_NOTHING_and_marks_the_row_stale(store):
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    row = missions.observe_objective(
        m["id"], "checks", observed=False, value=False, detail="forge unreachable"
    )
    assert row["state"] == "pending"
    assert row["met_at"] is None
    # `stale` + `reason` are exactly the two fields the console's degraded rendering reads.
    assert row["observed"]["stale"] is True
    assert row["observed"]["reason"] == "forge unreachable"


def test_observed_false_records_the_fact_and_leaves_the_objective_unmet(store):
    m = _mission()
    _add(m["id"], "merged", "forge_merged")
    row = missions.observe_objective(
        m["id"], "merged", observed=True, value=False, detail="not merged yet"
    )
    assert row["state"] == "pending" and row["met_at"] is None
    # NOT stale: we looked. The distinction is what the supervisor's nudge acts on.
    assert "stale" not in row["observed"]
    assert row["observed"]["value"] is False


def test_a_failed_probe_never_UN_meets_a_settled_objective(store):
    """Silently re-opening a gate would let the supervisor resume nudging a mission it had already
    proposed for completion. The observation is recorded; the state is the operator's to move."""
    m = _mission()
    _add(m["id"], "merged", "forge_merged")
    missions.observe_objective(m["id"], "merged", observed=True, value=True, detail="merged")
    row = missions.observe_objective(
        m["id"], "merged", observed=True, value=False, detail="not merged"
    )
    assert row["state"] == "met"
    assert row["met_at"] is not None  # the settlement keeps its own stamp
    assert row["observed"]["detail"] == "not merged"


def test_settling_advances_the_episode_so_the_budget_starts_again(store):
    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    before, _ = missions.objective_episode(m["id"], "pr_open")
    missions.observe_objective(m["id"], "pr_open", observed=True, value=True, detail="open")
    after, stood = missions.objective_episode(m["id"], "pr_open")
    assert after > before and stood is False


def test_an_unknown_objective_is_None_not_an_upsert(store):
    m = _mission()
    assert missions.observe_objective(m["id"], "nope", observed=True, value=True) is None


def test_the_OPERATOR_path_still_cannot_write_a_settlement(store):
    """The two paths stay asymmetric: this is the whole authority split."""
    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    for field in ("state", "met_at", "observed"):
        with pytest.raises(missions.MissionError):
            missions.patch_objectives(
                m["id"], [{"op": "retitle", "key": "pr_open", "title": "x", field: "met"}]
            )


# ---- the runner -----------------------------------------------------------------


def test_an_unconfigured_forge_makes_every_forge_probe_UNKNOWN(store, monkeypatch, tmp_path):
    """ "We were not told where to look" is not evidence about the work."""
    monkeypatch.setattr(prefs, "get_forge", lambda *a, **k: dict(prefs._FORGE_DEFAULTS))
    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    out = mission_probes.run_for_mission(m["id"])
    assert out["probed"] == 1 and out["settled"] == 0 and out["unknown"] == 1
    row = missions.objectives(m["id"])[0]
    assert row["state"] == "pending"
    assert row["observed"]["stale"] is True
    assert "no forge is configured" in row["observed"]["reason"]


def test_a_settled_IMMUTABLE_objective_is_not_re_probed(store, monkeypatch):
    """`forge_merged` does not un-merge. Re-asking spends a request to learn what is recorded."""
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(True),
    )
    m = _mission()
    _add(m["id"], "merged", "forge_merged")
    _add(m["id"], "branch", "git_local")
    missions.observe_objective(m["id"], "merged", observed=True, value=True, detail="x")
    mission_probes.run_for_mission(m["id"])
    assert calls == ["branch"]


def test_a_settled_MUTABLE_objective_IS_re_probed(store, monkeypatch):
    """Checks go red when the head advances and an approval can be dismissed.

    A gate that stops being asked the moment it first passes lets a completion proposal quote
    evidence about a commit that is no longer the head. The settlement is not moved backwards
    automatically — that is the operator's call — but the OBSERVATION is refreshed so the
    regression is visible (#897 review).
    """
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(False, "checks are red"),
    )
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    mission_probes.run_for_mission(m["id"])
    assert calls == ["checks"]
    row = missions.objectives(m["id"])[0]
    # Still met — a probe never un-meets — but the row now SAYS the latest look disagreed.
    assert row["state"] == "met"
    assert row["observed"]["detail"] == "checks are red"


def test_a_WAIVED_objective_is_never_probed(store, monkeypatch):
    """The operator has said it is not required; asking anyway spends a request to contradict."""
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(True),
    )
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    mission_probes.run_for_mission(m["id"])
    assert calls == []


def test_supervisor_judged_and_none_are_never_run_by_the_probe_runner(store, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(True),
    )
    m = _mission()
    _add(m["id"], "claimed", "supervisor_judged", gate=False)
    _add(m["id"], "byhand", "none", gate=False)
    mission_probes.run_for_mission(m["id"])
    assert calls == []


def test_one_unwritable_objective_does_not_stop_the_rest(store, monkeypatch):
    m = _mission()
    _add(m["id"], "a", "forge_pr")
    _add(m["id"], "b", "forge_merged")
    monkeypatch.setattr(mission_probes, "probe_one", lambda mi, o, **_: forge.Fact.seen(True, "ok"))
    real = missions.observe_objective
    seen: list[str] = []

    def flaky(mid, key, **kw):
        seen.append(key)
        if key == "a":
            raise RuntimeError("disk")
        return real(mid, key, **kw)

    monkeypatch.setattr(missions, "observe_objective", flaky)
    out = mission_probes.run_for_mission(m["id"])
    assert seen == ["a", "b"]
    assert out["probed"] == 2


# ---- the two HTTP probes, and the stale-200 -------------------------------------


def _http(handler):
    mission_probes.set_transport_for_test(httpx.MockTransport(handler))


def test_http_status_answers_the_LIVENESS_question(store):
    _http(lambda r: httpx.Response(200, text="hello"))
    f = mission_probes._probe_http("http_status", {"url": "https://x/health"}, {})
    assert f.observed and f.value is True
    _http(lambda r: httpx.Response(503, text=""))
    f = mission_probes._probe_http("http_status", {"url": "https://x/health"}, {})
    assert f.observed and f.value is False


def test_an_unreachable_target_is_UNKNOWN_not_down(store):
    def boom(req):
        raise httpx.ConnectError("no", request=req)

    _http(boom)
    f = mission_probes._probe_http("http_status", {"url": "https://x/health"}, {})
    assert f.observed is False and "unreachable" in f.detail


def test_THE_STALE_200_a_revision_objective_does_not_settle_on_a_status_code(store):
    """The regression #840 names by name.

    A target answered 200 **before** the deploy, and a stale instance still answers 200 **after**
    it. Both look identical on status alone — which is why `service_live` may go green and
    `change_live` may not. The revision probe looks for the marker the server knows about, so the
    stale instance is `observed=True, value=False`: we looked, and this is not the new revision.
    """
    body_before = "<html>build 111</html>"

    # BEFORE the deploy: 200, old build.
    _http(lambda r: httpx.Response(200, text=body_before))
    live = mission_probes._probe_http("http_status", {"url": "https://x/"}, {})
    change = mission_probes._probe_http(
        "http_revision", {"url": "https://x/", "expect": "222deadbeef"}, {}
    )
    assert live.observed and live.value is True, "service_live MAY go green"
    assert change.observed and change.value is False, "change_live may NOT"

    # AFTER the deploy, but a stale instance is still answering — same 200, same old body.
    change2 = mission_probes._probe_http(
        "http_revision", {"url": "https://x/", "expect": "222deadbeef"}, {}
    )
    assert change2.value is False

    # …and only when the marker actually appears does it settle.
    _http(lambda r: httpx.Response(200, text="<html>build 222deadbeef</html>"))
    change3 = mission_probes._probe_http(
        "http_revision", {"url": "https://x/", "expect": "222deadbeef"}, {}
    )
    assert change3.observed and change3.value is True


def test_a_revision_probe_with_NO_marker_is_unknown_rather_than_green(store):
    """There is nothing it could check, and answering "live" from a status code is the exact claim
    this probe exists to refuse."""
    _http(lambda r: httpx.Response(200, text="anything"))
    f = mission_probes._probe_http("http_revision", {"url": "https://x/"}, {})
    assert f.observed is False


def test_NO_REQUEST_is_issued_for_a_probe_kind_that_does_not_fetch(store, monkeypatch):
    """The #883 authority regression, re-asserted against THIS runner.

    #883 proved the planner cannot author a probe target. The remaining question is whether the
    RUNNER can be induced to fetch one, and the answer has to be asserted on the HTTP client — a
    check on the stored row proves only that the row is clean.
    """

    def boom(req):
        raise AssertionError(f"the runner issued a request to {req.url}")

    mission_probes.set_transport_for_test(httpx.MockTransport(boom))
    forge.set_transport_for_test(httpx.MockTransport(boom))
    m = _mission()
    # A model can only ever produce a NOTE — no probe, never a gate (#883). Its title is attacker-
    # controlled text; the runner must not read a target out of it.
    _add(m["id"], "note_x", "none", gate=False)
    _add(m["id"], "claimed", "supervisor_judged", gate=False)
    out = mission_probes.run_for_mission(m["id"])
    assert out["probed"] == 0


# ---- the supervisor integration -------------------------------------------------


@pytest.mark.anyio
async def test_the_probes_run_BEFORE_the_assessment_reads_the_rows(store, monkeypatch):
    """Order is the point, and it is the difference between acting now and acting a sweep late.

    The probes are what MAKE a gate met. Running them after `assess()` means the pass that could
    have noticed a mission finishing instead nudges about an objective the probe it had not yet run
    was about to satisfy — so the completion proposal arrives a whole interval later.
    """
    from agent_sessions import mission_supervisor

    order: list[str] = []

    def fake_probes(mission_id, *, path=None):
        order.append("probe")
        missions.observe_objective(mission_id, "pr_open", observed=True, value=True, detail="open")
        return {"probed": 1, "settled": 1, "unknown": 0}

    real_assess = mission_supervisor.assess

    def spy_assess(mission_id, **kw):
        order.append("assess")
        return real_assess(mission_id, **kw)

    monkeypatch.setattr(mission_probes, "run_for_mission", fake_probes)
    monkeypatch.setattr(mission_supervisor, "assess", spy_assess)

    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    out = await mission_supervisor.run_pass(m["id"])
    assert order[:2] == ["probe", "assess"]
    # …and the assessment therefore SEES the settlement in the same pass.
    assert out["objectives"][0]["met"] is True
    assert out["probes"] == {"probed": 1, "settled": 1, "unknown": 0}


@pytest.mark.anyio
async def test_a_probe_failure_never_fails_the_supervisor_pass(store, monkeypatch):
    """A forge outage leaves objectives unsettled and visibly stale — a state the console renders.
    It must not be a pass that raises, or one bad forge stops follow-through for the whole fleet."""
    from agent_sessions import mission_supervisor

    def boom(mission_id, *, path=None):
        raise RuntimeError("forge exploded")

    monkeypatch.setattr(mission_probes, "run_for_mission", boom)
    m = _mission()
    _add(m["id"], "pr_open", "forge_pr")
    out = await mission_supervisor.run_pass(m["id"])
    assert "objectives" in out
    assert out["probes"] is None


# ---- #897 review: fairness, identity, and the last observation -----------------------------


def test_THE_THIRTEENTH_OBJECTIVE_is_eventually_probed(store, monkeypatch):
    """The starvation family, third instance.

    `[:PROBES_PER_MISSION]` of a stable order is a permanent PREFIX: with more unsettled
    objectives than the cap, the tail is never reached — objective 13 was asked about zero times,
    for ever. The cap is a ceiling on WORK per pass, never on the set that work is drawn from.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(False, "not yet"),
    )
    m = _mission()
    keys = [f"o{i:02d}" for i in range(13)]
    for k in keys:
        _add(m["id"], k, "forge_merged")

    mission_probes.run_for_mission(m["id"])
    assert len(calls) == mission_probes.PROBES_PER_MISSION
    mission_probes.run_for_mission(m["id"])
    # The window ADVANCED rather than restarting.
    assert set(calls) == set(keys), f"never reached: {sorted(set(keys) - set(calls))}"


def test_the_cursor_is_DURABLE_across_a_restart(store, monkeypatch):
    """In process memory the cursor resets on every restart, and a service that restarts before
    completing a revolution re-selects the same prefix for ever — fairness a restart erases is not
    fairness (#888's own finding, same shape)."""
    calls: list[str] = []
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda m, o, **_: calls.append(o["key"]) or forge.Fact.seen(False, "not yet"),
    )
    m = _mission()
    for i in range(13):
        _add(m["id"], f"o{i:02d}", "forge_merged")
    mission_probes.run_for_mission(m["id"])
    first = list(calls)
    # A "restart": nothing in this process is reused — the cursor has to come off disk.
    missions.reset_schema_cache_for_test()
    calls.clear()
    mission_probes.run_for_mission(m["id"])
    assert calls != first
    assert set(first) | set(calls) == {f"o{i:02d}" for i in range(13)}


def test_a_LATE_probe_cannot_settle_a_DIFFERENT_objective_in_the_same_slot(store):
    """`(mission_id, key)` is a SLOT, not an identity.

    Drop and re-add the same key pointing at a different target while a request is in flight and
    the old answer would settle the new question. The identity is compared inside the transaction
    that writes, because outside it this is check-then-act.
    """
    m = _mission()
    _add(m["id"], "live", "http_status", {"url": "https://old.example"})
    # …the operator repoints it while a probe for the OLD target is in flight.
    missions.patch_objectives(m["id"], [{"op": "drop", "key": "live"}])
    _add(m["id"], "live", "http_revision", {"url": "https://new.example", "expect": "abc"})

    # The late answer, carrying the identity it was ASKED about.
    row = missions.observe_objective(
        m["id"],
        "live",
        observed=True,
        value=True,
        detail="200 from the old target",
        expect_probe="http_status",
        expect_args={"url": "https://old.example"},
    )
    assert row is None, "the stale answer must not be applied at all"
    assert missions.objectives(m["id"])[0]["state"] == "pending"


def test_the_PROBE_KIND_alone_is_enough_to_refuse_a_late_answer(store):
    """Isolates the kind half of the identity.

    The first version of this file only exercised a case where the ARGS also differed, so
    disabling the probe-kind check left it green — the test proved the args comparison twice and
    the kind comparison not at all. Same args, different question.
    """
    m = _mission()
    _add(m["id"], "live", "http_status", {"url": "https://x.example"})
    missions.patch_objectives(m["id"], [{"op": "drop", "key": "live"}])
    _add(m["id"], "live", "http_revision", {"url": "https://x.example", "expect": "v2"})
    row = missions.observe_objective(
        m["id"],
        "live",
        observed=True,
        value=True,
        detail="200",
        expect_probe="http_status",
        expect_args={"url": "https://x.example"},
    )
    assert row is None
    assert missions.objectives(m["id"])[0]["state"] == "pending"


def test_the_PROBE_ARGS_alone_are_enough_to_refuse_a_late_answer(store):
    """…and the mirror image: same question, repointed target."""
    m = _mission()
    _add(m["id"], "live", "http_status", {"url": "https://old.example"})
    missions.patch_objectives(m["id"], [{"op": "drop", "key": "live"}])
    _add(m["id"], "live", "http_status", {"url": "https://new.example"})
    row = missions.observe_objective(
        m["id"],
        "live",
        observed=True,
        value=True,
        detail="200 from the old target",
        expect_probe="http_status",
        expect_args={"url": "https://old.example"},
    )
    assert row is None


def test_the_identity_check_lets_the_MATCHING_answer_through(store):
    m = _mission()
    _add(m["id"], "live", "http_status", {"url": "https://x.example"})
    row = missions.observe_objective(
        m["id"],
        "live",
        observed=True,
        value=True,
        detail="200",
        expect_probe="http_status",
        expect_args={"url": "https://x.example"},
    )
    assert row is not None and row["state"] == "met"


def test_an_UNKNOWN_keeps_the_last_successful_observation(store):
    """ "It was green an hour ago and the forge is down" and "we have never seen this" are
    different, and the console's "last seen … · stale" needs the first to have something to name.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(
        m["id"], "checks", observed=True, value=False, detail="checks pending"
    )
    row = missions.observe_objective(
        m["id"], "checks", observed=False, value=False, detail="forge unreachable"
    )
    assert row["observed"]["stale"] is True
    assert row["observed"]["reason"] == "forge unreachable"
    # …and the prior fact survives.
    assert row["observed"]["last"]["detail"] == "checks pending"
    assert row["observed"]["last"]["value"] is False


def test_forge_run_does_not_need_a_pull_request(store, monkeypatch):
    """A workflow run is a branch-and-workflow fact. Forcing it through the PR prerequisite made
    an answerable question unanswerable whenever no PR existed yet."""
    asked: list[str] = []

    class C:
        def pull_request(self, *a, **k):
            asked.append("pr")
            return forge.Fact.seen(False, "no PR")

        def run(self, repo, workflow, branch):
            asked.append("run")
            return forge.Fact.seen(True, "the last run succeeded")

    monkeypatch.setattr(mission_probes, "_client_for", lambda t: C())
    monkeypatch.setattr(mission_probes, "_repo_for", lambda m, a: "acme/app")
    monkeypatch.setattr(mission_probes, "_branch_for", lambda m, a: "topic")
    f = mission_probes.probe_one({}, {"probe": "forge_run", "probe_args": {"workflow": "ci"}})
    assert f.observed and f.value is True
    assert asked == ["run"], "it must not ask for a pull request first"


# ---- #897 re-review -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_MET_gate_whose_latest_look_says_RED_does_not_allow_likely_done(store):
    """The completion proposal is a claim about NOW.

    The probe records a regression without moving the settlement backwards — deliberately, since
    un-meeting a gate silently would restart nudging on a mission already proposed for completion.
    But computing `likely_done` from the stored state alone let a mission whose checks had just
    gone red still propose completion with `unmet_gates: 0`.
    """
    from agent_sessions import mission_supervisor

    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    a = mission_supervisor.assess(m["id"])
    assert a["likely_done"] is True and a["unmet_gates"] == 0

    missions.observe_objective(m["id"], "checks", observed=True, value=False, detail="red")
    a = mission_supervisor.assess(m["id"])
    assert a["likely_done"] is False
    assert a["unmet_gates"] == 1
    # The stored settlement is untouched; only the CLAIM ABOUT NOW changed.
    assert a["objectives"][0]["met"] is True
    assert a["objectives"][0]["current"] is False


@pytest.mark.anyio
async def test_a_MET_gate_that_has_gone_STALE_does_not_allow_likely_done(store):
    """ "We could not check" is not evidence that a gate still holds — the stale-200 claim, one
    layer up."""
    from agent_sessions import mission_supervisor

    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    missions.observe_objective(m["id"], "checks", observed=False, value=False, detail="forge down")
    a = mission_supervisor.assess(m["id"])
    assert a["likely_done"] is False and a["unmet_gates"] == 1


@pytest.mark.anyio
async def test_a_WAIVED_gate_does_not_go_stale(store):
    """A waiver is the operator saying it was not required. That does not expire."""
    from agent_sessions import mission_supervisor

    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    a = mission_supervisor.assess(m["id"])
    assert a["likely_done"] is True and a["unmet_gates"] == 0


def test_EVERY_probe_whose_fact_can_change_is_mutable(store):
    """Named individually, because the list is the thing that decides whether a gate is ever
    re-checked and a missing entry is silent."""
    for kind in (
        "forge_checks",
        "forge_review",
        "forge_pr",
        "http_status",
        "http_revision",
        "forge_run",
        "git_local",
    ):
        assert kind in mission_probes.MUTABLE, kind
    # A merge does not un-merge.
    assert "forge_merged" not in mission_probes.MUTABLE


def test_a_probe_whose_FORGE_moved_mid_flight_is_discarded(store, monkeypatch):
    """The forge config is part of WHERE an answer came from, and none of it is in `probe_args` —
    so the stored-field comparison alone let an answer from the old authority settle a row now
    pointing at a new one (#897 review).

    Moved by repointing the REAL config while the request is notionally in flight, not by
    monkeypatching the digest: a test that patches the comparison proves the comparison is called,
    which is not the claim.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    cfg = {**prefs._FORGE_DEFAULTS, "enabled": True, "base_url": "https://git.a/", "owner": "acme"}
    monkeypatch.setattr(prefs, "get_forge", lambda *a, **k: dict(cfg))

    def probe(mi, o, **_):
        cfg["base_url"] = "https://git.b/"  # the operator repoints the forge, mid-request
        return forge.Fact.seen(True, "green")

    monkeypatch.setattr(mission_probes, "probe_one", probe)
    out = mission_probes.run_for_mission(m["id"])
    assert out["probed"] == 1
    assert out["settled"] == 0, "an answer about somewhere else must not settle the row"
    assert missions.objectives(m["id"])[0]["state"] == "pending"


def test_a_probe_whose_DERIVED_target_moved_mid_flight_is_discarded(store, monkeypatch):
    """…and the DERIVED half too (#897 re-review, finding 2).

    `probe_args` names neither the repository nor the branch for the ordinary objective that omits
    them: they are read off the checkout's `origin` remote and its current branch. Change the
    remote while a request is in flight and every stored field is identical while the destination
    is not — which is precisely the case the first version of the fence could not see.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    monkeypatch.setattr(
        prefs, "get_forge", lambda *a, **k: {**prefs._FORGE_DEFAULTS, "enabled": True}
    )
    remote = {"url": "https://git.example/acme/app.git"}
    monkeypatch.setattr(gitpanel, "remote_url", lambda cwd: remote["url"])
    monkeypatch.setattr(gitpanel, "head_sha", lambda cwd, branch=None: "abc123")

    def probe(mi, o, **_):
        remote["url"] = "https://git.example/someone-else/app.git"
        return forge.Fact.seen(True, "green")

    monkeypatch.setattr(mission_probes, "probe_one", probe)
    out = mission_probes.run_for_mission(m["id"])
    assert out["probed"] == 1
    assert out["settled"] == 0, "a different repository is a different question"
    assert missions.objectives(m["id"])[0]["state"] == "pending"


def test_a_SUPERSEDED_probe_cannot_settle_the_row_it_no_longer_holds(store, monkeypatch):
    """The generation, which is what makes the fence atomic rather than a pair of comparisons.

    Two runners can select the same objective, and a restart loses anything held in one runner's
    locals. `bind_probe_target` stamps the row; a second bind moves the stamp; the first answer is
    then adjudicated by the transaction that would otherwise have committed it.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    o = missions.objectives(m["id"])[0]
    tgt = mission_probes.resolve_target(m, o).digest

    first = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    second = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    assert second == first + 1

    # The FIRST runner's answer comes back last. It is about the right place, and it is stale.
    late = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=first,
    )
    assert late is None
    assert missions.objectives(m["id"])[0]["state"] == "pending"

    # …and the holder of the current generation still settles it.
    now = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=second,
    )
    assert now is not None and now["state"] == "met"


def test_a_probe_whose_target_HELD_STILL_is_applied(store, monkeypatch):
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    monkeypatch.setattr(
        mission_probes, "probe_one", lambda mi, o, **_: forge.Fact.seen(True, "green")
    )
    out = mission_probes.run_for_mission(m["id"])
    assert out["settled"] == 1
    row = missions.objectives(m["id"])[0]
    assert row["state"] == "met"
    # …and the row records WHAT it was measured against, so the next probe can compare.
    assert row["observed"]["target"]
    # …while the BINDING itself stays internal: it is a fence, not mission state, and publishing
    # a digest the client could send back would make it one.
    assert "probe_gen" not in row and "probe_target" not in row


def test_http_revision_ABANDONS_an_over_cap_body(store):
    """The second outbound client. The first pass streamed the forge adapter and left this one
    buffering — which is why the inventory counts call SITES rather than modules."""
    sent = {"n": 0}

    def handler(req):
        def gen():
            chunk = b"x" * 65536
            for _ in range(40):  # 2.5 MiB if anyone lets it run
                sent["n"] += len(chunk)
                yield chunk

        return httpx.Response(200, content=gen())

    _http(handler)
    f = mission_probes._probe_http("http_revision", {"url": "https://x/", "expect": "deadbeef"}, {})
    # UNKNOWN, not false: the cap stopped the download partway, so "the marker is not in what we
    # read" is not "the marker is not there" (#897 re-review, finding 5). The BYTES are still
    # bounded, which is what this test was originally about.
    assert f.observed is False and "not read" in f.detail
    assert sent["n"] <= mission_probes.REVISION_BODY_MAX + 65536, f"downloaded {sent['n']}"


# ---- #897 re-review, finding 1: one predicate, asked in both places -------------------------


def _propose(mid):
    return missions.propose_completion(mid, from_state="running", render=lambda rows: ("t", {}))


def test_a_gate_that_went_RED_after_settling_BLOCKS_the_completion_transaction(store):
    """The board and the committing transaction were asking two different questions.

    `assess()` asks whether the LATEST observation still supports the settlement, because a check
    can be re-run red and a deploy can be rolled back. `propose_completion` asked only whether
    `state` was `met` — and a probe that sees a settled gate go red records the fact WITHOUT
    un-meeting the row, deliberately (un-meeting is an operator decision). So the board reported
    "not done" while the transaction happily carried the mission into review and posted a
    completion proposal over a gate that was red at the moment it committed.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    assert missions.objectives(m["id"])[0]["state"] == "met"

    missions.observe_objective(m["id"], "checks", observed=True, value=False, detail="re-ran red")
    row = missions.objectives(m["id"])[0]
    assert row["state"] == "met" and row["observed"]["value"] is False, "premise: still `met`"

    assert _propose(m["id"]) is False, "a completion was proposed over a gate that had gone red"
    got = missions.get_mission(m["id"])
    assert got["state"] == "running"
    assert not [e for e in got["events"] if e["kind"] == "completion"]


def test_a_gate_we_COULD_NOT_LOOK_AT_also_blocks_the_completion_transaction(store):
    """ "We could not check" is not evidence that a gate still holds — it is the stale-200 claim
    one layer up, and proposing a completion on it is the loudest possible place to make it."""
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    missions.observe_objective(m["id"], "checks", observed=False, value=False, detail="forge down")
    assert missions.objectives(m["id"])[0]["observed"]["stale"] is True
    assert _propose(m["id"]) is False
    assert missions.get_mission(m["id"])["state"] == "running"


def test_a_WAIVED_objective_is_exempt_because_a_decision_does_not_go_STALE(store):
    """The operator said it was not required. That does not stop being true when a forge is down,
    and a fence that blocked on it would make waiving useless — which is the over-correction this
    pins against."""
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    assert _propose(m["id"]) is True
    assert missions.get_mission(m["id"])["state"] == "review"


def test_a_gate_still_OBSERVED_GREEN_proposes_normally(store):
    """The green half, so the fence is pinned in both directions rather than by refusing."""
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    missions.observe_objective(m["id"], "checks", observed=True, value=True, detail="green")
    assert _propose(m["id"]) is True
    assert missions.get_mission(m["id"])["state"] == "review"


def test_an_UPGRADED_store_gets_the_probe_binding_columns(tmp_path):
    """v15, applied to a v14 file rather than only present in a fresh `CREATE TABLE`.

    The asymmetry that makes an unversioned column dangerous: a fresh install is fine, an
    upgraded one raises `no such column: probe_gen` on the first settlement — which is the one
    path that matters, since every existing install is the upgraded case.
    """
    import sqlite3

    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row  # what `_ready` hands a migration in production
    con.executescript(
        "CREATE TABLE mission_objectives (mission_id TEXT NOT NULL, key TEXT NOT NULL,"
        " ord INTEGER NOT NULL, title TEXT NOT NULL, probe TEXT NOT NULL, probe_args TEXT,"
        " gate INTEGER NOT NULL, state TEXT NOT NULL, met_at REAL, observed TEXT,"
        " source TEXT NOT NULL, PRIMARY KEY (mission_id, key));"
    )
    con.commit()

    missions._migrate_14_to_15(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(mission_objectives)").fetchall()}
    assert {"probe_target", "probe_gen"} <= cols
    # …and re-running is a no-op rather than a duplicate-column error.
    missions._migrate_14_to_15(con)
    con.close()


# ---- #897 re-review round 4 -------------------------------------------------------------------


def test_an_A_to_B_to_A_config_change_cannot_settle_from_the_WRONG_AUTHORITY(store, monkeypatch):
    """Finding 1. A digest cannot see an ABA; not re-reading the config can.

    The target is resolved from forge A and the row is bound to it. If the client is built from
    LIVE preferences at request time, an operator who repoints A→B and back again during the
    request sends it to B while both the bound digest and the one taken afterwards read A — every
    comparison agrees and an answer from an authority nobody approved settles the row.

    Asserted on the URL the client is actually constructed for, because that is the claim: the
    request goes to the BOUND authority, not to whatever the config says at the moment the client
    is made.
    """
    cfg = {
        **prefs._FORGE_DEFAULTS,
        "enabled": True,
        "base_url": "https://a.example/",
        "kind": "forgejo",
        "owner": "acme",
    }
    monkeypatch.setattr(prefs, "get_forge", lambda *a, **k: dict(cfg))
    mission = {"cwd": "", "merge_sha": ""}
    obj = {"probe": "forge_checks", "probe_args": {"repo": "acme/app", "branch": "topic"}}
    target = mission_probes.resolve_target(mission, obj)

    # …the operator repoints the forge while the request is in flight.
    cfg["base_url"] = "https://b.example/"
    client = mission_probes._client_for(target)
    assert client is not None
    assert "a.example" in client.base, f"the request would go to {client.base}"
    # …and the credential does NOT ride along to an authority that is no longer configured. A
    # token minted for one host must not be sent to another; the private name is read because
    # the client deliberately does not expose it.
    assert client._token == ""


def test_DROPPING_and_RE_ADDING_an_objective_cannot_inherit_an_old_answer(store):
    """Finding 2. A per-row generation restarts at zero when the row is replaced.

    `(mission, key)` is a SLOT, not an identity — the same slot can hold a different question a
    second later. A generation counted from the row's own history is an index into that slot, so
    the second incarnation handed out `1` again and an answer issued for the first passed every
    check: same probe, same args, same target, same number.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    o = missions.objectives(m["id"])[0]
    tgt = mission_probes.resolve_target(m, o).digest
    stale = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    assert stale is not None

    missions.patch_objectives(m["id"], [{"op": "drop", "key": "checks"}])
    _add(m["id"], "checks", "forge_checks")

    # The OLD answer, arriving now, about a row that no longer exists in the form it asked about.
    late = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=stale,
    )
    assert late is None, "an answer for the previous incarnation settled the new one"
    assert missions.objectives(m["id"])[0]["state"] == "pending"

    # …and the new incarnation gets a generation that was never handed out before.
    fresh = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    assert fresh is not None and fresh > stale


def test_a_MARKER_BEYOND_THE_CAP_is_unknown_rather_than_not_live(store):
    """Finding 5. The cap stops the download partway, so a miss in the prefix is not a miss.

    Reporting "the expected revision is not live" because the page is large is the stale-200 lie
    pointing the other way: a claim about the deploy made from something that is not evidence
    about the deploy.
    """
    tail = b"x" * (mission_probes.REVISION_BODY_MAX + 4096) + b"build-deadbeef"
    _http(lambda r: httpx.Response(200, content=tail))
    f = mission_probes._probe_http(
        "http_revision", {"url": "https://x/", "expect": "build-deadbeef"}, {}
    )
    assert f.observed is False, f
    assert "not read" in f.detail

    # …and a marker INSIDE the cap still settles, so the guard is the truncation and not the size.
    _http(lambda r: httpx.Response(200, text="build-deadbeef then " + "x" * 1000))
    g = mission_probes._probe_http(
        "http_revision", {"url": "https://x/", "expect": "build-deadbeef"}, {}
    )
    assert g.observed and g.value is True


def test_http_revision_binds_to_the_MERGE_SHA_when_no_expect_is_configured(store):
    """#891's `change_live` contract, with the producer it was missing (#897 re-review 5).

    "Is THIS revision live" needs a revision, and a static playbook cannot name a SHA that does
    not exist when it is written. So the marker is the operator's `expect` when they typed one and
    the mission's own merge SHA otherwise — recorded by the one thing that knows it, an
    observation of the merge. With neither, the probe says which is missing rather than reporting
    "live" from a status code.
    """
    m = _mission()
    _add(m["id"], "live", "http_revision", {"url": "https://x.example/"})

    # NEITHER: unknown, and it names both fixes.
    _http(lambda r: httpx.Response(200, text="<html>build 111</html>"))
    f = mission_probes._probe_http(
        "http_revision", {"url": "https://x/"}, missions.get_mission(m["id"])
    )
    assert f.observed is False
    assert "expect" in f.detail and "merge" in f.detail

    # …then a merge is OBSERVED, and the mission has a revision to look for.
    assert missions.note_merge_sha(m["id"], "deadbeefcafe") is True
    row = missions.get_mission(m["id"])
    assert row["merge_sha"] == "deadbeefcafe"

    _http(lambda r: httpx.Response(200, text="<html>build 111</html>"))
    stale = mission_probes._probe_http("http_revision", {"url": "https://x/"}, row)
    assert stale.observed and stale.value is False, "the stale instance is not the new revision"

    _http(lambda r: httpx.Response(200, text="<html>build deadbeefcafe</html>"))
    live = mission_probes._probe_http("http_revision", {"url": "https://x/"}, row)
    assert live.observed and live.value is True


def test_a_MERGE_SHA_is_write_once(store):
    """A merge commit does not change. A second, different value would mean the row is about a
    different merge — and would repoint every revision objective on the mission at it with
    nothing on screen saying so."""
    m = _mission()
    assert missions.note_merge_sha(m["id"], "aaaa1111") is True
    assert missions.note_merge_sha(m["id"], "bbbb2222") is False
    assert missions.get_mission(m["id"])["merge_sha"] == "aaaa1111"
    # …and a value that is not a commit is refused rather than stored.
    m2 = _mission()
    assert missions.note_merge_sha(m2["id"], "not a sha") is False
    assert missions.note_merge_sha(m2["id"], "") is False
    assert missions.get_mission(m2["id"])["merge_sha"] in (None, "")


def test_observing_a_MERGE_records_its_sha_on_the_mission(store, monkeypatch):
    """The producer, through the RUNNER rather than by calling the store directly — which is the
    difference between proving the writer works and proving it is used."""
    m = _mission()
    _add(m["id"], "merged", "forge_merged")
    monkeypatch.setattr(
        mission_probes,
        "probe_one",
        lambda mi, o, **_: forge.Fact.seen(True, "merged", sha="feedface99"),
    )
    out = mission_probes.run_for_mission(m["id"])
    assert out["settled"] == 1
    assert missions.get_mission(m["id"])["merge_sha"] == "feedface99"


def test_a_FORGE_CHANGE_DURING_THE_WRITE_cannot_settle_the_row(store, monkeypatch):
    """[security] #897 re-review 5, finding 1 — the window a pre-write comparison cannot close.

    Every other fence compares two values the CALLER supplied, so all of them answer about a
    moment before the settling transaction. An operator who repoints the forge between the
    caller's last resolution and the write leaves each one agreeing while the evidence came from
    an authority nobody is configured for any more.

    The revision counter lives in this store, so the transaction reads it for itself. Driven by
    advancing it inside the write — the exact interleaving the digest cannot see.
    """
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    o = missions.objectives(m["id"])[0]
    tgt = mission_probes.resolve_target(m, o).digest
    gen = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    assert gen is not None

    # …the operator saves a new forge endpoint while the answer is on its way back.
    missions.bump_forge_revision()

    late = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=gen,
    )
    assert late is None, "an answer from the previous forge settled the row"
    assert missions.objectives(m["id"])[0]["state"] == "pending"

    # …and a probe bound AFTER the change settles normally, so this is a fence and not a wedge.
    gen2 = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    ok = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=gen2,
    )
    assert ok is not None and ok["state"] == "met"


def test_SAVING_THE_FORGE_ADVANCES_THE_REVISION(store, tmp_path, monkeypatch):
    """The counter is advanced by the config write itself, not by anything remembering to.

    Asserted through `prefs.set_forge` rather than by calling the bump directly: the difference
    between proving the counter works and proving the thing that has to move it does. Asserted as
    MONOTONIC rather than as `+1`, because the write advances it on both sides (#897 re-review 6,
    finding 2) — the exact count is an implementation detail and pinning it would make the
    ordering fix look like a regression.
    """
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    before = missions.forge_revision()
    prefs.set_forge({"enabled": True, "base_url": "https://git.a.example/", "kind": "forgejo"})
    after_one = missions.forge_revision()
    assert after_one > before
    prefs.set_forge({"base_url": "https://git.b.example/"})
    assert missions.forge_revision() > after_one


def test_a_LATE_PROBE_cannot_edit_a_TERMINAL_mission(store):
    """#897 re-review 5, finding 3. A probe issued while the mission was running can resolve after
    the operator has failed or abandoned it, and marking an objective `met` on a finished record
    edits history — the settlement is about a mission that no longer exists in that form."""
    m = _mission()
    _add(m["id"], "checks", "forge_checks")
    o = missions.objectives(m["id"])[0]
    tgt = mission_probes.resolve_target(m, o).digest
    gen = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )

    missions.set_state(m["id"], "running", "failed", outcome="failed")

    late = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=gen,
    )
    assert late is None, "a finished mission's checklist was edited by a late probe"
    assert missions.objectives(m["id"])[0]["state"] == "pending"


def test_a_v16_store_UPGRADES_with_the_column_a_probe_needs(tmp_path, monkeypatch):
    """#897 re-review 6, finding 1 — and the asymmetry that makes this class of bug dangerous.

    `probe_rev` was appended to the v14→v15 step after v16 already existed, so a database that
    had already reached 16 walks straight past it and lands on 17 without the column. The first
    probe then fails with `no such column: probe_rev`. A FRESH install is fine, because the base
    `CREATE TABLE` carries it — so the failure only ever reaches operators who already had the
    app, which is exactly who a migration exists for.

    Driven from a synthetic v16 file through a real bind and settle, rather than by asserting on
    `PRAGMA table_info`: the column being present is the mechanism, and the probe working is the
    claim.
    """
    import sqlite3

    db = tmp_path / "v16.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    # Build it as v16 did: every table, and `mission_objectives` WITHOUT `probe_rev`.
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(missions._SCHEMA)
    con.execute("ALTER TABLE mission_objectives DROP COLUMN probe_rev")
    con.execute("ALTER TABLE missions DROP COLUMN merge_sha")
    con.execute("PRAGMA user_version=16")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    m = _mission()  # opening the store runs the migration
    _add(m["id"], "checks", "forge_checks")
    o = missions.objectives(m["id"])[0]
    tgt = mission_probes.resolve_target(m, o).digest
    gen = missions.bind_probe_target(
        m["id"], "checks", target=tgt, expect_probe="forge_checks", expect_args=o.get("probe_args")
    )
    assert gen is not None
    row = missions.observe_objective(
        m["id"],
        "checks",
        observed=True,
        value=True,
        detail="green",
        expect_probe="forge_checks",
        expect_args=o.get("probe_args"),
        expect_target=tgt,
        expect_gen=gen,
    )
    assert row is not None and row["state"] == "met"
    # …and the merge-SHA producer works on the upgraded file too.
    assert missions.note_merge_sha(m["id"], "abc123") is True


def test_the_revision_LEADS_the_config_write(tmp_path, monkeypatch):
    """#897 re-review 6, finding 2 — the ORDER, which is the whole fence.

    The two stores are different files and cannot be written atomically together, so the order
    decides which way the gap fails. Bumping only afterwards leaves a window where the config is
    already B and the revision still says A, and a probe settling inside it sees its bound
    revision match — landing an answer from A under B, durably. Bumping FIRST inverts the window
    to "revision says B, config still A", where a settling probe is refused: it costs a discarded
    probe and the next pass re-binds.

    Asserted from INSIDE the config write, because "the revision moved at some point" is true of
    both orders and only one of them is safe.
    """
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    before = missions.forge_revision()
    seen: list[int] = []
    real = prefs._mutate

    def watched(block, merge, path=None):
        # The revision as it stands at the moment the forge block is actually written.
        seen.append(missions.forge_revision())
        return real(block, merge, path)

    monkeypatch.setattr(prefs, "_mutate", watched)
    prefs.set_forge({"enabled": True, "base_url": "https://git.a.example/", "kind": "forgejo"})

    assert seen, "the config write never happened"
    assert seen[0] > before, (
        "the config was written while the revision still named the previous authority — a probe "
        "settling in that window lands an answer from the old forge under the new one"
    )
