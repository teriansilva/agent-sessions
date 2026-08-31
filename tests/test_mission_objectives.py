"""The planner may choose WHICH objective, never WHAT IT DOES (#883).

`http_status` and `http_revision` make a request, so if model output could reach a URL then
untrusted text — the instruction, an issue body, a transcript the planner read — could choose an
address the server fetches on a schedule. Server-side request forgery with a cadence attached.

These assert the property that makes that impossible: there is no code path from model text to a
probe target, so the acceptance test can be made **on the HTTP client** rather than on a stored
row. A test that only checked the row would pass on a design that fetched first and stored badly.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import mission_objectives as mo
from agent_sessions import missions, prefs


def _pending_ids(**kw) -> list[str]:
    """Just the ids from the recovery worklist, which pages on `(objectives_at, id)`."""
    return [mid for _, mid in missions.missions_awaiting_objectives(**kw)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_mission_playbooks(
        {
            "default_id": "ship",
            "playbooks": [
                {
                    "id": "ship",
                    "label": "Ship",
                    "objectives": [
                        {
                            "key": "pr_open",
                            "title": "A PR is open",
                            "probe": "forge_pr",
                            "gate": True,
                        },
                        {
                            "key": "live",
                            "title": "It is live",
                            "probe": "http_status",
                            "probe_args": {"url": "https://app.example.com/healthz"},
                            "gate": True,
                        },
                    ],
                }
            ],
        }
    )
    return tmp_path / "m.db"


def _mission():
    return missions.create_mission("ship the thing")["id"]


@pytest.fixture
def no_requests(monkeypatch):
    """Fail the test if ANYTHING issues an HTTP request during it.

    This is the assertion the issue asks for. Checking the stored row would pass on a design that
    fetched a model-chosen URL and then declined to persist it — the request is the harm, not the
    row.
    """
    calls: list[str] = []

    def boom(*a, **k):  # noqa: ANN002, ANN003
        calls.append(str(a[:2]))
        raise AssertionError(f"an HTTP request was issued: {a[:2]}")

    import httpx

    monkeypatch.setattr(httpx.Client, "request", boom, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "request", boom, raising=False)
    return calls


def _reply(monkeypatch, obj):
    async def fake(_messages, **_kw):
        return obj

    monkeypatch.setattr(mo.review, "complete_json", fake)


@pytest.mark.anyio
async def test_a_planner_that_asks_for_a_metadata_url_gets_NOTHING_executable(
    store, no_requests, monkeypatch
):
    """The instruction and the model both try to aim a probe at cloud metadata and loopback.

    There is no field for it, so the attempt cannot even be expressed — which is the point. It
    is asserted anyway, because "the schema has no field" is a claim about today's schema and
    this is a claim about the behaviour.
    """
    _reply(
        monkeypatch,
        {
            "objectives": [
                # Every one of these is an attempt to author a target, and each one now REFUSES
                # its whole row — the prompt's own promise ("any you add is ignored and the row
                # is refused"). Stronger than substituting the template's probe: a selection that
                # tried to author a target produces nothing at all (review on #884).
                {
                    "template_index": 1,
                    "gate": True,
                    "probe": "http_status",
                    "probe_args": {"url": "http://169.254.169.254/latest/meta-data/"},
                },
                {
                    "template_index": 0,
                    "gate": True,
                    "probe_args": {"url": "http://127.0.0.1:9200/"},
                },
                # …and a CLEAN selection alongside them still works, so the refusal is aimed at
                # the attempt and not at the batch.
                {"template_index": 0, "gate": True},
            ],
            "notes": [{"title": "check http://169.254.169.254/ manually"}],
        },
    )
    mid = _mission()
    out = await mo.propose(mid)

    rows = {o["key"]: o for o in out["objectives"]}
    # BOTH target-authoring selections were refused outright — `live` (index 1) is absent
    # entirely, not present with a substituted probe.
    assert "live" not in rows, "a selection that tried to author a target produced a row"
    assert out["dropped"] == 2, out
    # The clean selection still landed, and it took its probe from the TEMPLATE.
    assert rows["pr_open"]["probe"] == "forge_pr"
    assert rows["pr_open"]["probe_args"] is None
    # Nothing anywhere points at what the model asked for.
    for o in out["objectives"]:
        assert "169.254" not in str(o.get("probe_args") or "")
        assert "127.0.0.1" not in str(o.get("probe_args") or "")
    # The note is a note: it can carry the text, and it can never act on it.
    note = next(o for o in out["objectives"] if o["key"].startswith(missions.NOTE_KEY_PREFIX))
    assert note["probe"] == "none"
    assert note["gate"] is False
    assert note["probe_args"] is None
    # …and the fixture asserts no request was issued at any point.
    assert no_requests == []


@pytest.mark.anyio
async def test_an_invalid_template_index_is_dropped_never_invented(store, no_requests, monkeypatch):
    """Out of range, negative, non-integer, and `True` — which is an `int` in Python and would
    otherwise index element 1."""
    _reply(
        monkeypatch,
        {
            "objectives": [
                {"template_index": 99},
                {"template_index": -1},
                {"template_index": "0"},
                {"template_index": True},
                {"gate": True},
            ]
        },
    )
    out = await mo.propose(_mission())
    assert out["objectives"] == []
    assert out["dropped"] == 5


@pytest.mark.anyio
async def test_a_repeated_selection_is_deduplicated_not_refused(store, no_requests, monkeypatch):
    _reply(monkeypatch, {"objectives": [{"template_index": 0}, {"template_index": 0}]})
    out = await mo.propose(_mission())
    assert [o["key"] for o in out["objectives"]] == ["pr_open"]
    assert out["dropped"] == 0, "a restatement is not a refusal"


@pytest.mark.anyio
async def test_a_model_title_is_adapted_but_carries_no_authority(store, no_requests, monkeypatch):
    _reply(
        monkeypatch,
        {"objectives": [{"template_index": 0, "title": "A PR is open against main"}]},
    )
    out = await mo.propose(_mission())
    row = out["objectives"][0]
    assert row["title"] == "A PR is open against main"
    assert row["probe"] == "forge_pr", "the title changed; the probe did not"


@pytest.mark.anyio
async def test_a_gate_on_a_NON_PROBING_template_cannot_gate(store, no_requests, monkeypatch):
    """A gating objective with no probe can never be met, so the mission could never complete.
    Degrading a security problem into a liveness one is not a fix (#883)."""
    prefs.set_mission_playbooks(
        {
            "default_id": "p",
            "playbooks": [
                {
                    "id": "p",
                    "label": "P",
                    "objectives": [
                        {"key": "manual", "title": "Manual", "probe": "none", "gate": False}
                    ],
                }
            ],
        }
    )
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})
    out = await mo.propose(_mission())
    assert out["objectives"][0]["gate"] is False


@pytest.mark.anyio
async def test_an_unknown_playbook_yields_notes_only_and_says_so(store, no_requests, monkeypatch):
    _reply(monkeypatch, {"objectives": [{"template_index": 0}], "notes": [{"title": "n"}]})
    mid = missions.create_mission("x", playbook_id="gone")["id"]
    out = await mo.propose(mid)
    assert out["templates"] == "unknown_playbook"
    # The selection had no templates to select FROM, so it is dropped; the note survives.
    assert [o["key"] for o in out["objectives"]] == [f"{missions.NOTE_KEY_PREFIX}1"]
    kinds = [e["kind"] for e in missions.get_mission(mid)["events"]]
    assert "objective" in kinds, "the missing playbook id was not recorded"


# ---- the producer's LIFECYCLE call site (#883 review) -------------------------------
#
# The prompt, the parser and the instantiator were all reachable only from their own unit tests:
# creating a mission persisted `playbook_id` and stopped, so no ordinary mission ever received an
# objective. Every test below drives the boundary a real mission goes through.


@pytest.fixture
def no_outbound(monkeypatch):
    """Like `no_requests`, but usable from a ROUTE test.

    `TestClient` drives the app THROUGH `httpx.Client.request`, so the blanket version fails the
    test's own login. This one lets the loopback transport through and fails on anything that
    leaves the process — which is the assertion that actually matters here.
    """
    seen: list[str] = []
    real = __import__("httpx").Client.request

    def guard(self, method, url, *a, **k):  # noqa: ANN001, ANN002, ANN003
        if not str(url).startswith("https://testserver"):
            seen.append(str(url))
            raise AssertionError(f"an outbound HTTP request was issued: {method} {url}")
        return real(self, method, url, *a, **k)

    import httpx

    monkeypatch.setattr(httpx.Client, "request", guard)
    monkeypatch.setattr(
        httpx.AsyncClient,
        "request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"outbound request: {a[1:3]}")),
        raising=False,
    )
    return seen


def _api(auth_cfg, tmp_path):
    from fastapi.testclient import TestClient

    from agent_sessions import projects
    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {
        "X-CSRF-Token": c.get("/api/config").json()["csrf"],
        "Origin": auth_cfg.origin,
    }
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    proj = projects.create("repo", folders=[str(repo)], default_folder=str(repo))
    return c, hdr, proj


def _configured(monkeypatch):
    """An AI endpoint exists. Nothing is fetched — `complete_json` is stubbed by `_reply`."""
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})


def test_CREATING_a_mission_instantiates_its_objectives(
    store, no_outbound, auth_cfg, tmp_home, tmp_path, monkeypatch
):
    """The regression for the whole finding: POST a mission, read back a filled checklist.

    It goes through the ROUTE rather than calling `propose()`, because the defect was not in
    `propose()` — that worked — but in nothing ever calling it. A test of the producer alone
    stays green through exactly this bug, which is why this one starts at the HTTP boundary.
    """
    _configured(monkeypatch)
    _reply(
        monkeypatch,
        {
            "objectives": [{"template_index": 0, "gate": True}],
            "notes": [{"title": "ask about the rollback plan"}],
        },
    )
    c, hdr, proj = _api(auth_cfg, tmp_path)
    r = c.post(
        "/api/missions",
        json={"instruction": "ship the thing", "project_id": proj.id},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    rows = c.get(f"/api/missions/{r.json()['id']}/objectives", headers=hdr).json()["objectives"]
    assert [o["key"] for o in rows] == ["pr_open", "note_1"], rows
    # Authority per row, which is the reason instantiation is its own boundary.
    assert [o["source"] for o in rows] == ["playbook", "model"]
    # The template's probe came from the TEMPLATE; the note carries none and cannot gate.
    assert (rows[0]["probe"], rows[0]["gate"]) == ("forge_pr", True)
    assert (rows[1]["probe"], rows[1]["gate"]) == ("none", False)


def test_a_mission_is_still_created_when_the_planner_FAILS(
    store, no_outbound, auth_cfg, tmp_home, tmp_path, monkeypatch
):
    """A dead model endpoint must not cost the operator their mission — and must not be silent.

    Both halves matter. Swallowing the failure would leave an empty checklist indistinguishable
    from a feature that does not work, which is precisely what the operator reported about the
    orchestrator (#772).
    """
    _configured(monkeypatch)

    async def boom(_messages, **_kw):
        raise RuntimeError("endpoint returned HTTP 500")

    monkeypatch.setattr(mo.review, "complete_json", boom)
    c, hdr, proj = _api(auth_cfg, tmp_path)
    r = c.post(
        "/api/missions",
        json={"instruction": "ship the thing", "project_id": proj.id},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    assert c.get(f"/api/missions/{mid}/objectives", headers=hdr).json()["objectives"] == []
    events = c.get(f"/api/missions/{mid}", headers=hdr).json()["events"]
    said = [e.get("text") or "" for e in events]
    assert any("no objectives proposed" in t and "HTTP 500" in t for t in said), said


def test_an_install_with_NO_AI_ENDPOINT_creates_missions_without_a_red_task(
    store, no_outbound, auth_cfg, tmp_home, tmp_path, monkeypatch
):
    """Not configured is an unmade choice, not a fault. A fresh install still gets its mission,
    and the timeline says why the checklist is empty rather than leaving it a mystery."""
    from agent_sessions import aitasks, review

    monkeypatch.setattr(
        mo.review,
        "_require_config",
        lambda: (_ for _ in ()).throw(review.NotConfiguredError("no endpoint")),
    )
    aitasks.reset()
    c, hdr, proj = _api(auth_cfg, tmp_path)
    r = c.post(
        "/api/missions",
        json={"instruction": "ship the thing", "project_id": proj.id},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    events = c.get(f"/api/missions/{mid}", headers=hdr).json()["events"]
    assert any("no AI endpoint is configured" in (e.get("text") or "") for e in events)
    # …and the DURABLE intent is closed, not merely explained. Leaving it pending made every
    # mission on a fresh install a permanent recovery-worklist entry, reconsidered on every boot
    # for ever — and the timeline event alone cannot show that (review on #884).
    assert _pending_ids() == [], "an unconfigured install left the production intent pending"
    # Not recorded as a failing AI task: a red counter here reports a fault that is not one.
    assert aitasks.snapshot()["last"].get("mission-objectives") is None


@pytest.mark.anyio
async def test_instantiating_an_unmet_GATE_reopens_a_mission_in_review(
    store, no_requests, tmp_path
):
    """The instantiator kept `patch_objectives`' rows and dropped its INVARIANTS.

    A proposal against a mission already in `review` could add a pending gate while the mission
    stayed review-ready — an objective list saying "not done" beside a mission saying "ready to
    close" — and left `updated_at` and the timeline untouched, so nothing downstream could see it
    had happened.
    """
    mid = missions.create_mission("ship the thing", cwd=str(tmp_path))["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.set_state(mid, "running", "review")
    before = missions.get_mission(mid)
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": "pr_open",
                "title": "A PR is open",
                "probe": "forge_pr",
                "gate": True,
                "source": "playbook",
            }
        ],
    )
    after = missions.get_mission(mid)
    assert after["state"] == "running", "an unmet gate was added and the mission stayed in review"
    assert after["updated_at"] > before["updated_at"]
    kinds = [
        (e["kind"], (e.get("meta") or {}).get("by")) for e in missions.get_mission(mid)["events"]
    ]
    assert ("objective", "instantiation") in kinds, kinds
    assert any(k == "state" for k, _ in kinds), kinds


# ---- what can change while the model is thinking (review on #884) --------------------------
#
# `propose()` resolves templates, awaits a model for many seconds, and then writes. Three things
# the operator can do inside that window were unfenced: revoke the playbook, close the mission,
# or send output the parser coerces instead of dropping.


@pytest.mark.anyio
async def test_a_playbook_REVOKED_mid_proposal_arms_nothing(store, no_requests, monkeypatch):
    """The revocation race, which is the security-relevant one.

    The operator deletes the playbook while the model is thinking. Instantiating from the
    snapshot would persist a probe target they have just removed — and Phase 5 would then
    schedule requests to it on a cadence.
    """
    mid = _mission()

    async def revoke_then_answer(_messages, **_kw):
        prefs.set_mission_playbooks({"default_id": "", "playbooks": []})
        return {"objectives": [{"template_index": 0, "gate": True}]}

    monkeypatch.setattr(mo.review, "complete_json", revoke_then_answer)
    out = await mo.propose(mid)
    assert out["objectives"] == [], "a revoked playbook's target was instantiated anyway"
    assert missions.objectives(mid) == []
    events = missions.get_mission(mid)["events"]
    assert any("playbook changed" in (e.get("text") or "") for e in events), events


@pytest.mark.anyio
async def test_a_playbook_RETARGETED_mid_proposal_arms_nothing(store, no_requests, monkeypatch):
    """Same fence, for the edit that keeps the id and moves the URL — the case a version counter
    on the playbook id alone would miss."""
    mid = _mission()

    async def retarget_then_answer(_messages, **_kw):
        pb = prefs.get_mission_playbooks()
        pb["playbooks"][0]["objectives"][1]["probe_args"] = {"url": "https://new.example/healthz"}
        prefs.set_mission_playbooks(pb)
        return {"objectives": [{"template_index": 1, "gate": True}]}

    monkeypatch.setattr(mo.review, "complete_json", retarget_then_answer)
    out = await mo.propose(mid)
    assert out["objectives"] == []
    stored = [o.get("probe_args") for o in missions.objectives(mid)]
    assert stored == [], f"a stale target was persisted: {stored}"


@pytest.mark.anyio
async def test_a_mission_CLOSED_mid_proposal_gets_no_late_gate(store, no_requests, monkeypatch):
    """A `done` mission carrying a fresh PENDING gate is a checklist contradicting its own
    outcome — and `_finish_objective_write` cannot repair it, because it reopens `review` and
    nothing else. Refused rather than reopened: a late background task does not get to reopen a
    mission the operator closed."""
    mid = _mission()

    async def close_then_answer(_messages, **_kw):
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "abandoned", outcome="abandoned")
        return {"objectives": [{"template_index": 0, "gate": True}]}

    monkeypatch.setattr(mo.review, "complete_json", close_then_answer)
    out = await mo.propose(mid)
    assert out["objectives"] == []
    assert missions.objectives(mid) == [], "a gate was added to a closed mission"
    assert missions.get_mission(mid)["state"] == "abandoned", "a closed mission was reopened"


@pytest.mark.parametrize(
    "reply",
    [
        {"objectives": {"template_index": 0}},  # an object where a list belongs
        {"objectives": "nope"},
        {"notes": {"title": "x"}},
        {"notes": 7},
    ],
)
def test_malformed_CONTAINERS_drop_rather_than_raise(store, reply):
    """The stated contract is that drift DROPS rows. Slicing a dict raises `TypeError`, which is
    a parse failure escaping as a crash — the opposite."""
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, dropped = mo._rows_from_reply(reply, t)
    assert rows == [] and dropped >= 1


@pytest.mark.parametrize("gate", ["false", "true", 1, 0, {}, {"a": 1}, [], None, "yes"])
def test_a_NON_BOOLEAN_gate_DROPS_the_whole_selection(store, gate):
    """Drift drops a row; it never degrades it into something with a different meaning.

    `bool("false")` is `True`, so coercing could arm a gate nobody asked for. But the earlier fix
    — keep the row and force `gate=False` — is wrong in the other direction: it turns an intended
    REQUIRED outcome into an optional one, and the mission can then complete without it. #883's
    contract is drop-and-count, and this is the only reading that cannot silently change what an
    objective means (review on #884).
    """
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, dropped = mo._rows_from_reply({"objectives": [{"template_index": 0, "gate": gate}]}, t)
    assert rows == [], f"{gate!r} was repaired into a row instead of dropped"
    assert dropped == 1, "the refusal was not counted"


@pytest.mark.parametrize(
    "field",
    [
        # The four a denylist would also have caught…
        "probe",
        "probe_args",
        "source",
        "key",
        # …and the ones only an ALLOWLIST catches: a typo, a hallucinated key, a future field.
        # A denylist left every one of these silently accepted, so the row survived while nobody
        # had checked what it said (review on #884).
        "foo",
        "template_idx",
        "gate_",
        "state",
        "met_at",
    ],
)
def test_a_selection_carrying_a_FORBIDDEN_field_is_refused(store, field):
    """The prompt says "any you add is ignored and the row is refused". It has to be true.

    Keeping such a row (with the template's own probe substituted) made the prompt a promise the
    operator could not check, and it is the weaker SSRF answer: a selection that TRIED to author a
    target should produce nothing at all, not a sanitised objective.
    """
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    item = {"template_index": 0, field: "http_status" if field == "probe" else {"url": "http://x"}}
    # (the value never matters — the FIELD is what refuses the row)
    rows, dropped = mo._rows_from_reply({"objectives": [item]}, t)
    assert rows == [] and dropped == 1


def test_a_note_carrying_anything_but_a_title_is_refused(store):
    """A note is `{"title": ...}` and nothing else — the same rule, so a note cannot smuggle a
    field a selection may not carry."""
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, dropped = mo._rows_from_reply(
        {"notes": [{"title": "ok"}, {"title": "x", "probe": "http_status"}]}, t
    )
    assert [r["title"] for r in rows] == ["ok"]
    assert dropped == 1


def test_a_TRUE_gate_still_works(store):
    """The control — the fence above must not make gating impossible."""
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, _ = mo._rows_from_reply({"objectives": [{"template_index": 0, "gate": True}]}, t)
    assert rows[0]["gate"] is True


def test_the_activity_kind_is_BOUNDED_not_one_per_mission(store):
    """`aitasks._last` is keyed on kind and never expires, and `/api/ai/activity` serializes it
    whole — so a per-mission kind grows memory and every activity response with the total
    historical mission count."""
    from agent_sessions import aitasks

    aitasks.reset()

    async def _drive():
        for i in range(5):
            async with aitasks.single_flight("mission-objectives", detail=f"m{i}", scope=f"m{i}"):
                pass

    asyncio.run(_drive())
    assert list(aitasks.snapshot()["last"]) == [
        "mission-objectives"
    ], "the activity map grew one entry per mission"


def test_the_per_mission_single_flight_still_excludes(store):
    """…and the scope still does the job the per-mission kind was doing."""
    from agent_sessions import aitasks

    aitasks.reset()

    async def _drive():
        async with aitasks.single_flight("mission-objectives", scope="m1"):
            # Same scope: refused.
            with pytest.raises(aitasks.AlreadyRunning):
                async with aitasks.single_flight("mission-objectives", scope="m1"):
                    pass
            # Different scope: allowed, because two missions may propose at once.
            async with aitasks.single_flight("mission-objectives", scope="m2"):
                pass

    asyncio.run(_drive())


def test_the_binding_is_digested_from_the_SAME_read_as_the_templates(store):
    """A fence with a race inside it is not a fence.

    Resolving templates and then digesting them in a SECOND read lets prefs change in between:
    the digest describes the new config, the templates are the old ones, and the write-boundary
    check compares new against new, passes, and writes the stale targets anyway. Caught in
    self-review of the first version of this fix, not by the tests above — all of which pass
    against the broken version, because they mutate prefs during the MODEL call rather than
    during the resolve.
    """
    calls: list[int] = []
    real = prefs.get_mission_playbooks

    def counting_and_mutating():
        calls.append(1)
        out = real()
        if len(calls) == 1:
            # Whatever a second read would see is different from what the first one did.
            prefs.set_mission_playbooks({"default_id": "", "playbooks": []})
        return out

    mid = _mission()
    import agent_sessions.prefs as prefs_mod

    prefs_mod.get_mission_playbooks = counting_and_mutating
    try:
        status, templates, binding = missions.templates_and_binding(mid)
    finally:
        prefs_mod.get_mission_playbooks = real

    assert calls == [1], "templates and binding were resolved by two separate reads"
    assert binding == missions._binding_digest(status, templates)


def test_a_revocation_CANNOT_land_between_the_check_and_the_insert(store, tmp_path, monkeypatch):
    """The check-then-write race, driven DETERMINISTICALLY at the vulnerable instant.

    A first version started the revoker before instantiation and accepted either outcome, so
    timing could put the revocation wholly before or after the check — and the review showed the
    old vulnerable implementation still passing it. A concurrency test that does not force the
    interleaving proves nothing about the interleaving.

    This one fires the revocation from INSIDE `playbook_binding`, i.e. immediately after the
    check the old code performed and before any insert, then observes — at the moment of the
    insert — whether the revocation had completed:

    * held lock (fixed): the revoker blocks on the prefs flock until this transaction commits,
      so at insert time it has NOT completed. The write is of a target that was still configured.
    * unheld lock (the bug): the revoker takes the flock immediately, completes, and the insert
      then happens anyway — writing a target the operator has already removed.
    """
    import threading

    mid = _mission()
    status, templates, binding = missions.templates_and_binding(mid)
    assert status == "ok"
    rows = [
        {
            "key": templates[1]["key"],
            "title": templates[1]["title"],
            "probe": templates[1]["probe"],
            "probe_args": templates[1]["probe_args"],
            "gate": True,
            "source": "playbook",
        }
    ]

    revoked = threading.Event()
    revoker: list[threading.Thread] = []
    real_binding = missions.playbook_binding

    def binding_then_revoke(mission_id, *, path=None):
        """Compute the digest, then start the revocation at the exact vulnerable instant."""
        out = real_binding(mission_id, path=path)
        if not revoker:

            def _go():
                prefs.set_mission_playbooks({"default_id": "", "playbooks": []})
                revoked.set()

            t = threading.Thread(target=_go, daemon=True)
            revoker.append(t)
            t.start()
        return out

    monkeypatch.setattr(missions, "playbook_binding", binding_then_revoke)

    seen_at_insert: list[bool] = []
    real_op_add = missions._op_add

    def spy_op_add(con, mission_id, op, source, ts):
        """Hold the insert open and GIVE the revocation time to land.

        This is what makes the test discriminate rather than race. If the prefs lock is held
        across the transaction (fixed), the revoker is blocked for this whole wait and cannot
        complete. If it was released after the check (the bug), the revoker takes the flock
        immediately and finishes well inside it — and the insert then proceeds anyway, which is
        exactly the defect. Without this wait the vulnerable window is sub-millisecond and the
        observation misses it: the first version of this test passed against the old code.
        """
        revoked.wait(timeout=2.0)
        seen_at_insert.append(revoked.is_set())
        return real_op_add(con, mission_id, op, source, ts)

    monkeypatch.setattr(missions, "_op_add", spy_op_add)

    outcome = "committed"
    try:
        missions.instantiate_objectives(mid, rows, expect_binding=binding)
    except missions.MissionError as e:
        assert e.status == 409
        outcome = "refused"

    for t in revoker:
        t.join(timeout=5)
    assert revoked.is_set(), "the revoker never ran, so nothing was interleaved at all"

    if outcome == "committed":
        assert seen_at_insert and seen_at_insert[0] is False, (
            "the revocation completed BEFORE the insert and the target was written anyway — "
            "the check ran outside the lock that was supposed to hold it"
        )
    else:
        assert missions.objectives(mid) == [], "a refused instantiation still wrote rows"


# ---- objective production is a DURABLE intent, not an in-process hope (review on #884) ------


def test_creating_a_mission_stamps_a_PENDING_production_intent(store):
    """A `BackgroundTask` lives only in the process that served the create.

    A crash between the mission's commit and the model call left a permanent empty checklist,
    with no timeline event saying why and nobody to retry it. The intent is stamped in the SAME
    transaction as the mission, so recovery can find it.
    """
    mid = _mission()
    assert _pending_ids() == [mid]


@pytest.mark.parametrize("state", ["done", "failed", "skipped"])
def test_every_terminal_outcome_closes_the_intent(store, state):
    """`done`, `failed` and `skipped` all take the mission off the retry list.

    `failed` and `skipped` deliberately do NOT stay pending: a model that answers badly, or an
    install with no endpoint, would otherwise be retried on every boot for ever.
    """
    mid = _mission()
    assert missions.settle_objectives_state(mid, state) is True
    assert _pending_ids() == []
    # …and the CAS means a late second caller cannot overwrite the recorded outcome.
    assert missions.settle_objectives_state(mid, "done") is False


@pytest.mark.anyio
async def test_a_CRASHED_producer_is_recovered_at_boot(store, no_requests, monkeypatch):
    """The regression for the whole finding: the mission exists, the checklist does not, and
    nothing in this process is going to fix it — until recovery runs."""
    _configured_direct(monkeypatch)
    mid = _mission()  # created, intent pending, producer never ran (the crash)
    assert missions.objectives(mid) == []

    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})
    out = await mo.recover_pending(older_than=0.0)

    assert out["recovered"] == [mid], out
    assert [o["key"] for o in missions.objectives(mid)] == ["pr_open"]
    assert _pending_ids() == [], "the intent was not closed"


@pytest.mark.anyio
async def test_recovery_SKIPS_producers_still_in_flight(store, no_requests, monkeypatch):
    """`older_than` keeps boot recovery off a producer that started seconds ago in this process.

    Correctness does not depend on it — `propose_for_new_mission` is single-flighted per mission
    — but racing one would put a second entry in the activity log for no reason.
    """
    _configured_direct(monkeypatch)
    _mission()
    assert await mo.recover_pending(older_than=3600.0) == {"recovered": [], "failed": []}


@pytest.mark.anyio
async def test_a_mission_that_PREDATES_the_producer_is_never_retried(store, monkeypatch, tmp_path):
    """NULL is not `pending`. Backfilling would propose objectives for the entire history at
    once, on the first boot after an upgrade."""
    mid = _mission()
    con = missions._ready(None)
    try:
        con.execute("UPDATE missions SET objectives_state=NULL WHERE id=?", (mid,))
        con.commit()
    finally:
        con.close()
    assert _pending_ids() == []


def _configured_direct(monkeypatch):
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})


@pytest.mark.parametrize("title", [7, 0, True, None, {"t": "x"}, ["x"], 1.5])
def test_a_NON_STRING_title_DROPS_the_selection(store, title):
    """`title: 7` was being repaired into the template's own title.

    That produces an executable row from a reply that did not match the contract — the same
    "degrade into a different meaning" the gate rule forbids, and #883 says drift DROPS
    (review on #884).
    """
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, dropped = mo._rows_from_reply({"objectives": [{"template_index": 0, "title": title}]}, t)
    assert rows == [], f"title={title!r} was repaired into a row"
    assert dropped == 1


def test_an_ABSENT_title_still_uses_the_template(store):
    """The control: `title` is OPTIONAL, and omitting it is not drift."""
    t = [{"key": "k", "title": "T", "probe": "forge_pr", "probe_args": None}]
    rows, dropped = mo._rows_from_reply({"objectives": [{"template_index": 0}]}, t)
    assert [r["title"] for r in rows] == ["T"] and dropped == 0


@pytest.mark.anyio
async def test_boot_recovery_takes_a_RECENT_pre_restart_intent(store, no_requests, monkeypatch):
    """The window a crash is most likely to land in was the one recovery excluded.

    `older_than=120` skipped a mission created 30 seconds before the crash, and since boot is the
    only caller nothing ever came back for it. At boot there are no producers running in this
    process, so every pending intent is by definition pre-restart and ours to finish.
    """
    _configured_direct(monkeypatch)
    mid = _mission()  # stamped `pending` just now — a crash could have landed right here
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})

    out = await mo.recover_pending()  # the boot call, with its production defaults
    assert out["recovered"] == [mid], out
    assert _pending_ids() == []


@pytest.mark.anyio
async def test_boot_recovery_DRAINS_past_one_batch(store, no_requests, monkeypatch):
    """One batch of 20 left the 21st mission pending for ever."""
    _configured_direct(monkeypatch)
    ids = [_mission() for _ in range(25)]
    assert len(_pending_ids(limit=200)) == 25
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})

    out = await mo.recover_pending(limit=10)
    assert sorted(out["recovered"]) == sorted(ids), out
    assert _pending_ids(limit=200) == [], "work was left behind"


@pytest.mark.anyio
async def test_recovery_STOPS_rather_than_spinning_on_an_unsettleable_mission(
    store, no_requests, monkeypatch
):
    """A mission that cannot settle must not turn the drain into an infinite loop.

    The worklist would keep returning it, so the loop stops as soon as a batch yields nothing it
    has not already tried.
    """
    _configured_direct(monkeypatch)
    mid = _mission()
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})
    # The production attempt runs, but the intent never closes — the row stays on the worklist.
    monkeypatch.setattr(missions, "settle_objectives_state", lambda *a, **k: False)

    out = await mo.recover_pending(limit=5)
    assert out["recovered"] == [mid], "the stuck mission was not attempted exactly once"
    assert _pending_ids() == [mid], "it should still be pending"


def test_a_stored_NULL_playbook_block_fails_CLOSED(tmp_path):
    """Absence and an explicit `null` are different facts, and `.get()` collapses them.

    A hand-edited `{"mission_playbooks": null}` read back as the SHIPPED templates — server
    probe targets armed by a malformed value — instead of degrading to none. That is a fail-open
    on operator policy: the rule is that only ABSENCE (the install never configured playbooks)
    gets the defaults (review on #884).
    """
    from agent_sessions.atomicjson import atomic_write_json

    p = tmp_path / "p.json"
    atomic_write_json(p, {"mission_playbooks": None})
    assert prefs.get_mission_playbooks(p) == {"default_id": "", "playbooks": []}

    # …and the control: the key genuinely absent still gets the shipped defaults.
    q = tmp_path / "q.json"
    atomic_write_json(q, {"theme": "dark"})
    assert [x["id"] for x in prefs.get_mission_playbooks(q)["playbooks"]] == [
        "ship_a_change",
        "investigate",
    ]


@pytest.mark.anyio
async def test_recovery_has_NO_fixed_total_cap(store, no_requests, monkeypatch):
    """A `max_batches` ceiling left the 1,001st intent pending for ever.

    Driven above the old 20x50 ceiling with a small batch size, so the run needs far more
    iterations than any fixed count would have allowed. Termination does not need a counter:
    `attempted` grows every iteration that continues, and the worklist is finite.
    """
    _configured_direct(monkeypatch)
    ids = [_mission() for _ in range(120)]
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})

    out = await mo.recover_pending(limit=2)  # 60 batches — more than the old ceiling of 50
    assert len(out["recovered"]) == len(
        ids
    ), f"recovery stopped early: {len(out['recovered'])} of {len(ids)}"
    assert _pending_ids(limit=500) == [], "work was left pending"


@pytest.mark.anyio
async def test_a_full_page_of_STUCK_intents_does_not_starve_the_ones_behind_it(
    store, no_requests, monkeypatch
):
    """The combination my two earlier regressions each covered only half of.

    One asserted that an unsettleable mission does not spin the loop — with nothing behind it.
    The other asserted a long drain completes — with everything settling. Neither drove a FULL
    PAGE of stuck rows followed by recoverable work, which is where remembering "what I have
    attempted" starved everything after the page: the query kept serving the same oldest rows,
    the set said nothing was new, and the loop stopped with real work pending (review on #884).

    A forward-only cursor answers both at once: each row is visited at most once per pass, so
    nothing spins, and the pass ends only when a page comes back EMPTY, so nothing is skipped.
    """
    _configured_direct(monkeypatch)
    stuck = [_mission() for _ in range(20)]  # a full page at the production default
    later = _mission()
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})

    real_settle = missions.settle_objectives_state

    def settle_all_but_the_stuck(mission_id, state, **kw):
        if mission_id in stuck:
            return False  # the attempt runs; the intent refuses to close
        return real_settle(mission_id, state, **kw)

    monkeypatch.setattr(missions, "settle_objectives_state", settle_all_but_the_stuck)

    out = await mo.recover_pending(limit=20)

    assert (
        later in out["recovered"]
    ), f"the mission behind a full stuck page was never reached: {out}"
    assert (
        _pending_ids(limit=200) == stuck
    ), "the stuck page should still be pending, and nothing else"
