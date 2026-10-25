"""The model may write WHAT done looks like, never HOW it is checked (#883, #1088, #1653).

`http_status` and `http_revision` make a request, so if model output could reach a URL then
untrusted text — the instruction, an issue body, a transcript the planner read — could choose an
address the server fetches on a schedule. Server-side request forgery with a cadence attached.

Checklists are gone (#1653), so every mission takes the path a mission that declined one used to:
with an AI endpoint the model's outcomes become `supervisor_judged` gates (source `instruction`),
without one they are notes. Neither can carry a probe or a target. These assert that on the HTTP
client rather than on a stored row — a test that only checked the row would pass on a design that
fetched first and stored badly.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent_sessions import mission_objectives as mo
from agent_sessions import missions


def _pending_ids(**kw) -> list[str]:
    """Just the ids from the recovery worklist, which pages on `(objectives_at, id)`."""
    return [mid for _, mid in missions.missions_awaiting_objectives(**kw)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    missions.reset_schema_cache_for_test()
    yield tmp_path / "m.db"
    missions.reset_schema_cache_for_test()


def _mission():
    return missions.create_mission("ship the thing")["id"]


#: An ordinary reply in the one contract there is: outcomes, as notes.
_NOTES = {"notes": [{"title": "A PR is open"}, {"title": "It is live"}]}


def _unconfigured(monkeypatch):
    """No AI endpoint — explicit, so the test does not depend on what the tmp prefs file holds."""
    from agent_sessions import review

    def _raise():
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(mo.review, "_require_config", _raise)


def _texts(mid) -> list[str]:
    return [e.get("text") or "" for e in missions.get_mission(mid)["events"]]


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
@pytest.mark.parametrize("ai_built", [True, False])
async def test_a_planner_that_asks_for_a_metadata_url_gets_NOTHING_executable(
    store, no_requests, monkeypatch, ai_built
):
    """The instruction and the model both try to aim a probe at cloud metadata and loopback.

    There is no field for it, so the attempt cannot even be expressed — which is the point. It
    is asserted anyway, because "the schema has no field" is a claim about today's schema and
    this is a claim about the behaviour. Both producer paths: judged gates and plain notes.
    """
    if ai_built:
        _configured(monkeypatch)
    else:
        _unconfigured(monkeypatch)
    _reply(
        monkeypatch,
        {
            # The removed template-selection contract, carrying targets: writes nothing.
            "objectives": [
                {
                    "template_index": 1,
                    "gate": True,
                    "probe": "http_status",
                    "probe_args": {"url": "http://169.254.169.254/latest/meta-data/"},
                },
                {"template_index": 0, "gate": True},
            ],
            "notes": [
                # A note that tries to author a target REFUSES its whole row…
                {"title": "Healthy", "probe": "http_status"},
                {"title": "Healthy", "probe_args": {"url": "http://127.0.0.1:9200/"}},
                # …and a clean one alongside still lands: it can carry the text, never act on it.
                {"title": "check http://169.254.169.254/ manually"},
            ],
        },
    )
    mid = _mission()
    out = await mo.propose(mid)

    assert out["dropped"] == 4, out
    [row] = out["objectives"]
    assert row["title"] == "check http://169.254.169.254/ manually"
    assert row["probe_args"] is None
    if ai_built:
        assert (row["probe"], row["gate"], row["source"]) == (
            "supervisor_judged",
            True,
            "instruction",
        )
    else:
        assert (row["probe"], row["gate"], row["source"]) == ("none", False, "model")
    for o in missions.objectives(mid):
        assert o.get("probe_args") is None
        assert o["probe"] in ("supervisor_judged", "none")
    # …and the fixture asserts no request was issued at any point.
    assert no_requests == []


# ---- every mission is an instruction mission now (#1653) -------------------------------------


@pytest.mark.anyio
async def test_a_mission_with_NO_checklist_gets_instruction_objectives(
    store, no_requests, monkeypatch
):
    """The regression for #1653: a plain mission — no `playbook_id` at all, which used to mean
    "the operator's default checklist" — gets the outcomes the model wrote as judged gates. Before,
    only a mission that declined a checklist (`":none"`) took this path."""
    _configured(monkeypatch)
    _reply(monkeypatch, _NOTES)
    mid = _mission()
    assert missions.get_mission(mid).get("playbook_id") in (None, "")
    out = await mo.propose(mid)
    rows = missions.objectives(mid)
    assert [(o["key"], o["title"]) for o in rows] == [
        (f"{missions.GOAL_KEY_PREFIX}1", "A PR is open"),
        (f"{missions.GOAL_KEY_PREFIX}2", "It is live"),
    ]
    for o in rows:
        assert (o["probe"], o["gate"], o["source"]) == ("supervisor_judged", True, "instruction")
        assert o["probe_args"] is None and o.get("direction") is None
    assert out["dropped"] == 0 and out["parameterised"] == 0
    # Gates exist, so the "nothing gates" line is not written.
    assert not any("no objective gates completion" in t for t in _texts(mid))


@pytest.mark.anyio
async def test_instruction_objectives_are_CAPPED(store, no_requests, monkeypatch):
    """At most `INSTRUCTION_GATES_MAX` judged gates, however many the model writes; the rest are
    counted as dropped and said once on the timeline."""
    cap = missions.INSTRUCTION_GATES_MAX
    _configured(monkeypatch)
    _reply(monkeypatch, {"notes": [{"title": f"Outcome {i}"} for i in range(cap + 3)]})
    mid = _mission()
    out = await mo.propose(mid)
    assert len(missions.objectives(mid)) == cap
    assert out["dropped"] == 3
    assert missions.get_mission(mid)["objectives_fit"] == {"dropped": 3, "parameterised": 0}


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [{}, {"notes": []}, {"notes": [{"title": ""}]}, "not json"])
async def test_a_reply_with_NO_usable_outcome_gets_the_fallback_gate(
    store, no_requests, monkeypatch, reply
):
    """A mission with an AI endpoint is never unmeasured: one judged gate whose criterion is the
    instruction itself."""
    _configured(monkeypatch)
    _reply(monkeypatch, reply)
    mid = _mission()
    await mo.propose(mid)
    [row] = missions.objectives(mid)
    assert (row["key"], row["title"]) == (
        missions.DONE_AS_INSTRUCTED_KEY,
        missions.DONE_AS_INSTRUCTED_TITLE,
    )
    assert (row["probe"], row["gate"], row["source"]) == ("supervisor_judged", True, "instruction")


@pytest.mark.anyio
async def test_a_FAILED_model_call_still_gets_the_fallback_gate_and_says_why(
    store, no_requests, monkeypatch
):
    """The AI-endpoint fallback: a `ReviewError` from the model call is not a mission without
    objectives — it gets the judged fallback gate, and the timeline says the model wrote none."""
    _configured(monkeypatch)

    async def boom(_messages, **_kw):
        raise mo.review.ReviewError("endpoint returned HTTP 502")

    monkeypatch.setattr(mo.review, "complete_json", boom)
    mid = _mission()
    await mo.propose(mid)
    assert [o["key"] for o in missions.objectives(mid)] == [missions.DONE_AS_INSTRUCTED_KEY]
    assert any(
        "could not write the objectives" in t and "HTTP 502" in t for t in _texts(mid)
    ), _texts(mid)


@pytest.mark.anyio
async def test_WITHOUT_an_endpoint_the_outcomes_are_notes_and_nothing_gates(
    store, no_requests, monkeypatch
):
    """No endpoint means nobody can judge a gate, so a direct caller gets notes — `model` rows,
    no probe, never a gate — and the timeline says, once, that nothing gates completion (#1063)."""
    _unconfigured(monkeypatch)
    _reply(monkeypatch, _NOTES)
    mid = _mission()
    await mo.propose(mid)
    rows = missions.objectives(mid)
    assert [o["key"] for o in rows] == [
        f"{missions.NOTE_KEY_PREFIX}1",
        f"{missions.NOTE_KEY_PREFIX}2",
    ]
    for o in rows:
        assert (o["probe"], o["gate"], o["source"]) == ("none", False, "model")
    said = [t for t in _texts(mid) if "no objective gates completion" in t]
    assert len(said) == 1, _texts(mid)


@pytest.mark.anyio
async def test_a_double_submitted_create_instantiates_ONCE(store, no_requests, monkeypatch):
    """Single-flight per mission: the second producer finds the first in flight and declines,
    rather than racing it into a second batch."""
    from agent_sessions import aitasks

    aitasks.reset()
    _configured(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def slow(_messages, **_kw):
        calls.append(1)
        entered.set()
        await release.wait()
        return _NOTES

    monkeypatch.setattr(mo.review, "complete_json", slow)
    mid = _mission()
    first = asyncio.ensure_future(mo.propose_for_new_mission(mid))
    await asyncio.wait_for(entered.wait(), 10)
    second = await mo.propose_for_new_mission(mid)
    release.set()
    out = await asyncio.wait_for(first, 10)

    assert second["templates"] == "already_running"
    assert calls == [1], "the model was asked twice"
    assert len(out["objectives"]) == 2
    assert [o["key"] for o in missions.objectives(mid)] == [
        f"{missions.GOAL_KEY_PREFIX}1",
        f"{missions.GOAL_KEY_PREFIX}2",
    ]
    assert _pending_ids() == [], "the winner did not settle the intent"


@pytest.mark.anyio
@pytest.mark.parametrize("ai_built", [True, False])
async def test_the_OLD_template_selection_contract_writes_NOTHING(
    store, no_requests, monkeypatch, ai_built
):
    """A model (or a cached prompt) still answering `{"objectives": [{"template_index": …}]}` gets
    nothing written from those selections: there are no templates to select from, so each one is
    counted as dropped. With an endpoint the mission still gets its fallback gate."""
    if ai_built:
        _configured(monkeypatch)
    else:
        _unconfigured(monkeypatch)
    _reply(
        monkeypatch,
        {
            "objectives": [
                {"template_index": 0, "gate": True},
                {"template_index": 1, "probe_args": {"branch": "main"}},
                {"template_index": 0, "title": "A PR is open against main"},
            ]
        },
    )
    mid = _mission()
    out = await mo.propose(mid)
    assert out["dropped"] == 3, out
    keys = [o["key"] for o in missions.objectives(mid)]
    assert keys == ([missions.DONE_AS_INSTRUCTED_KEY] if ai_built else []), keys
    assert all(o["source"] != "playbook" for o in missions.objectives(mid))


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
    """The regression for the whole finding: POST a mission, read back its objectives.

    It goes through the ROUTE rather than calling `propose()`, because the defect was not in
    `propose()` — that worked — but in nothing ever calling it. A test of the producer alone
    stays green through exactly this bug, which is why this one starts at the HTTP boundary.
    """
    _configured(monkeypatch)
    _reply(
        monkeypatch,
        {
            # A stale template selection beside the outcomes: the selection writes nothing.
            "objectives": [{"template_index": 0, "gate": True}],
            "notes": [{"title": "A PR is open"}, {"title": "The rollback plan is written"}],
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
    assert [o["key"] for o in rows] == ["goal_1", "goal_2"], rows
    # Authority per row, which is the reason instantiation is its own boundary: the model wrote
    # the criterion, the supervisor judges it, and nothing the model wrote is a probe target.
    assert [o["source"] for o in rows] == ["instruction", "instruction"]
    for o in rows:
        assert (o["probe"], o["gate"], o["probe_args"]) == ("supervisor_judged", True, None)


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
                "key": f"{missions.GOAL_KEY_PREFIX}1",
                "title": "A PR is open",
                "probe": "supervisor_judged",
                "gate": True,
                "source": "instruction",
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
# `propose()` awaits a model for many seconds and then writes. The checklist binding fence that
# used to wrap the write went with checklists (#1653) and is NOT replaced: the write is fenced only
# by `instantiate_objectives`' in-transaction checks — a closed mission, a key already written.


@pytest.mark.anyio
async def test_a_mission_CLOSED_mid_proposal_gets_no_late_gate(store, no_requests, monkeypatch):
    """A `done` mission carrying a fresh PENDING gate is an objective list contradicting its own
    outcome — and `_finish_objective_write` cannot repair it, because it reopens `review` and
    nothing else. Refused rather than reopened: a late background task does not get to reopen a
    mission the operator closed."""
    _configured(monkeypatch)
    mid = _mission()

    async def close_then_answer(_messages, **_kw):
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "abandoned", outcome="abandoned")
        return _NOTES

    monkeypatch.setattr(mo.review, "complete_json", close_then_answer)
    out = await mo.propose(mid)  # does not raise
    assert out["objectives"] == [] and out["templates"] == "stale"
    assert missions.objectives(mid) == [], "a gate was added to a closed mission"
    assert missions.get_mission(mid)["state"] == "abandoned", "a closed mission was reopened"
    assert any(t.startswith("no objectives proposed: ") for t in _texts(mid)), _texts(mid)


@pytest.mark.anyio
async def test_a_mission_DISPATCHED_mid_proposal_gets_no_late_objectives(
    store, no_requests, monkeypatch, tmp_path
):
    """Dispatch started while the model was thinking: what done means is fixed until the agent
    starts, so the late batch is refused in the write transaction."""
    _configured(monkeypatch)
    mid = missions.create_mission("ship the thing", cwd=str(tmp_path))["id"]

    async def dispatch_then_answer(_messages, **_kw):
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        return _NOTES

    monkeypatch.setattr(mo.review, "complete_json", dispatch_then_answer)
    out = await mo.propose(mid)  # does not raise
    assert out["objectives"] == [] and out["templates"] == "stale"
    assert missions.objectives(mid) == []
    assert missions.get_mission(mid)["state"] == "dispatching"
    assert any(t.startswith("no objectives proposed: ") for t in _texts(mid)), _texts(mid)


@pytest.mark.anyio
async def test_an_objective_the_OPERATOR_added_mid_proposal_is_not_added_to(
    store, no_requests, monkeypatch
):
    """The operator wrote their own objective — under a key the model's batch does not use, so
    no key collision can save it — while the model was thinking. A mission that already has
    objectives is not appended to: nothing from the proposal lands, the call does not raise, and
    the timeline says why."""
    _configured(monkeypatch)
    mid = _mission()

    async def operator_adds_then_answer(_messages, **_kw):
        missions.patch_objectives(
            mid, [{"op": "add", "key": "mine", "title": "Mine", "gate": False}]
        )
        return _NOTES

    monkeypatch.setattr(mo.review, "complete_json", operator_adds_then_answer)
    out = await mo.propose(mid)  # does not raise
    assert out["objectives"] == [] and out["templates"] == "stale"
    assert [(o["key"], o["source"]) for o in missions.objectives(mid)] == [("mine", "operator")]
    assert any(t.startswith("no objectives proposed: ") for t in _texts(mid)), _texts(mid)


@pytest.mark.anyio
async def test_objectives_another_PRODUCER_wrote_mid_proposal_are_not_added_to(
    store, no_requests, monkeypatch
):
    """…and the same for a second producer's batch landing first: the late one writes nothing."""
    _configured(monkeypatch)
    mid = _mission()
    theirs = [
        {
            "key": f"{missions.GOAL_KEY_PREFIX}1",
            "title": "Theirs",
            "probe": "supervisor_judged",
            "gate": True,
            "source": "instruction",
        }
    ]

    async def write_then_answer(_messages, **_kw):
        missions.instantiate_objectives(mid, theirs)
        return _NOTES

    monkeypatch.setattr(mo.review, "complete_json", write_then_answer)
    out = await mo.propose(mid)  # does not raise
    assert out["objectives"] == [] and out["templates"] == "stale"
    assert [(o["key"], o["title"]) for o in missions.objectives(mid)] == [("goal_1", "Theirs")]
    assert any(t.startswith("no objectives proposed: ") for t in _texts(mid)), _texts(mid)


@pytest.mark.parametrize("source", ["operator", "bogus"])
def test_instantiation_refuses_any_source_but_model_and_instruction(store, source):
    """Only the producer's own sources (and the trusted `playbook` template row) pass."""
    mid = _mission()
    row = {"key": "k", "title": "T", "probe": "supervisor_judged", "gate": True, "source": source}
    with pytest.raises(missions.MissionError):
        missions.instantiate_objectives(mid, [row])
    assert missions.objectives(mid) == []


# ---- the reply contract: drift DROPS, never repairs -----------------------------------------

BOTH = pytest.mark.parametrize("ai_built", [True, False])


@BOTH
@pytest.mark.parametrize(
    "reply",
    [
        {"notes": {"title": "x"}},  # an object where a list belongs
        {"notes": 7},
        {"notes": "nope"},
    ],
)
def test_malformed_CONTAINERS_drop_rather_than_raise(store, reply, ai_built):
    """The stated contract is that drift DROPS rows. Slicing a dict raises `TypeError`, which is
    a parse failure escaping as a crash — the opposite."""
    rows, dropped = mo._rows_from_reply(reply, ai_built=ai_built)
    assert rows == [] and dropped >= 1


@BOTH
@pytest.mark.parametrize("objectives", [{"template_index": 0}, "nope", 7])
def test_a_malformed_OLD_objectives_container_writes_nothing_and_does_not_raise(
    store, objectives, ai_built
):
    rows, _ = mo._rows_from_reply(
        {"objectives": objectives, "notes": [{"title": "ok"}]}, ai_built=ai_built
    )
    assert [r["title"] for r in rows] == ["ok"]


@BOTH
@pytest.mark.parametrize("gate", ["false", "true", True, False, 1, 0, {}, [], None])
def test_a_note_carrying_a_GATE_is_refused_whatever_its_value(store, gate, ai_built):
    """Drift drops a row; it never degrades it into something with a different meaning. Whether
    an outcome gates is decided by the producer path, never by the model — `bool("false")` is
    `True`, and forcing a value would change what the outcome means (review on #884)."""
    rows, dropped = mo._rows_from_reply(
        {"notes": [{"title": "T", "gate": gate}]}, ai_built=ai_built
    )
    assert rows == [], f"{gate!r} was repaired into a row instead of dropped"
    assert dropped == 1, "the refusal was not counted"


@BOTH
@pytest.mark.parametrize(
    "field",
    [
        # The four a denylist would also have caught…
        "probe",
        "probe_args",
        "source",
        "key",
        # …and the ones only an ALLOWLIST catches: a typo, a hallucinated key, a future field.
        "direction",
        "foo",
        "template_index",
        "state",
        "met_at",
    ],
)
def test_a_note_carrying_a_FORBIDDEN_field_is_refused(store, field, ai_built):
    """A note is `{"title": ...}` and nothing else. A note that TRIED to author a target produces
    nothing at all, not a sanitised objective — the weaker SSRF answer would keep the row."""
    item = {"title": "T", field: "http_status" if field == "probe" else {"url": "http://x"}}
    # (the value never matters — the FIELD is what refuses the row)
    rows, dropped = mo._rows_from_reply({"notes": [item]}, ai_built=ai_built)
    assert rows == [] and dropped == 1


@BOTH
def test_a_note_carrying_anything_but_a_title_is_refused_and_the_rest_survive(store, ai_built):
    rows, dropped = mo._rows_from_reply(
        {"notes": [{"title": "ok"}, {"title": "x", "probe": "http_status"}]}, ai_built=ai_built
    )
    assert [r["title"] for r in rows] == ["ok"]
    assert dropped == 1


def test_a_clean_note_still_GATES_on_the_judged_path(store):
    """The control — the fences above must not make gating impossible."""
    rows, dropped = mo._rows_from_reply({"notes": [{"title": "T"}]}, ai_built=True)
    assert dropped == 0
    assert [(r["key"], r["probe"], r["gate"], r["source"]) for r in rows] == [
        ("goal_1", "supervisor_judged", True, "instruction")
    ]


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
    """The regression for the whole finding: the mission exists, its objectives do not, and
    nothing in this process is going to fix it — until recovery runs."""
    _configured_direct(monkeypatch)
    mid = _mission()  # created, intent pending, producer never ran (the crash)
    assert missions.objectives(mid) == []

    _reply(monkeypatch, _NOTES)
    out = await mo.recover_pending(older_than=0.0)

    assert out["recovered"] == [mid], out
    assert [o["key"] for o in missions.objectives(mid)] == ["goal_1", "goal_2"]
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


@BOTH
@pytest.mark.parametrize("title", [7, 0, True, None, {"t": "x"}, ["x"], 1.5, "", "   "])
def test_a_NON_STRING_or_EMPTY_title_DROPS_the_note(store, title, ai_built):
    """`title: 7` is not repaired into some default title: a reply that does not match the
    contract drops the row (review on #884)."""
    rows, dropped = mo._rows_from_reply({"notes": [{"title": title}]}, ai_built=ai_built)
    assert rows == [], f"title={title!r} was repaired into a row"
    assert dropped == 1


@BOTH
def test_an_ABSENT_title_DROPS_the_note(store, ai_built):
    """There is no template to fall back on any more: a note without a title is nothing."""
    rows, dropped = mo._rows_from_reply({"notes": [{}]}, ai_built=ai_built)
    assert rows == [] and dropped == 1


@pytest.mark.parametrize(
    "title", ["x" * (missions.OBJECTIVE_TITLE_MAX + 1), "line\nbreak", "tab\there", "del\x7f"]
)
def test_a_judged_gate_title_breaking_the_title_rules_is_DROPPED_not_truncated(store, title):
    """A judged gate's title IS its criterion, so one the store would refuse drops that row here,
    rather than failing the whole atomic batch or being cut into a different criterion."""
    rows, dropped = mo._rows_from_reply(
        {"notes": [{"title": title}, {"title": "ok"}]}, ai_built=True
    )
    assert [r["title"] for r in rows] == ["ok"] and dropped == 1


@pytest.mark.anyio
async def test_boot_recovery_takes_a_RECENT_pre_restart_intent(store, no_requests, monkeypatch):
    """The window a crash is most likely to land in was the one recovery excluded.

    `older_than=120` skipped a mission created 30 seconds before the crash, and since boot is the
    only caller nothing ever came back for it. At boot there are no producers running in this
    process, so every pending intent is by definition pre-restart and ours to finish.
    """
    _configured_direct(monkeypatch)
    mid = _mission()  # stamped `pending` just now — a crash could have landed right here
    _reply(monkeypatch, _NOTES)

    out = await mo.recover_pending()  # the boot call, with its production defaults
    assert out["recovered"] == [mid], out
    assert _pending_ids() == []


@pytest.mark.anyio
async def test_boot_recovery_DRAINS_past_one_batch(store, no_requests, monkeypatch):
    """One batch of 20 left the 21st mission pending for ever."""
    _configured_direct(monkeypatch)
    ids = [_mission() for _ in range(25)]
    assert len(_pending_ids(limit=200)) == 25
    _reply(monkeypatch, _NOTES)

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
    _reply(monkeypatch, _NOTES)
    # The production attempt runs, but the intent never closes — the row stays on the worklist.
    monkeypatch.setattr(missions, "settle_objectives_state", lambda *a, **k: False)

    out = await mo.recover_pending(limit=5)
    assert out["recovered"] == [mid], "the stuck mission was not attempted exactly once"
    assert _pending_ids() == [mid], "it should still be pending"


@pytest.mark.anyio
async def test_recovery_has_NO_fixed_total_cap(store, no_requests, monkeypatch):
    """A `max_batches` ceiling left the 1,001st intent pending for ever.

    Driven above the old 20x50 ceiling with a small batch size, so the run needs far more
    iterations than any fixed count would have allowed. Termination does not need a counter:
    `attempted` grows every iteration that continues, and the worklist is finite.
    """
    _configured_direct(monkeypatch)
    ids = [_mission() for _ in range(120)]
    _reply(monkeypatch, _NOTES)

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
    _reply(monkeypatch, _NOTES)

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


# ---- a mission created while checklists existed (#1653) -------------------------------------
#
# The `playbook_id` column stays and old rows keep their value, unread. Such a mission keeps the
# objectives its checklist gave it; the producer never re-resolves the checklist, never fails on
# the stale id, and never piles instruction objectives on top.

_OLD_ROWS = [
    {"key": "pr_open", "title": "A PR is open", "probe": "forge_pr", "gate": True},
    {"key": "pr_open--2", "title": "A second PR is open", "probe": "forge_pr", "gate": True},
]


def _legacy_mission(db, *, with_objectives: bool, state: str | None) -> str:
    """A mission as an old store row has it: `playbook_id` naming a checklist that no longer
    exists anywhere, its checklist's objectives (if any), and an `objectives_state`."""
    mid = _mission()
    if with_objectives:
        # Written the way the store can write rows today, then relabelled to what the checklist
        # producer stored: `source = 'playbook'`, which instantiation no longer accepts.
        missions.patch_objectives(mid, [{"op": "add", **r} for r in _OLD_ROWS])
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE missions SET playbook_id='ship' WHERE id=?", (mid,))
        con.execute("UPDATE mission_objectives SET source='playbook' WHERE mission_id=?", (mid,))
        con.commit()
    finally:
        con.close()
    if state is not None:
        assert missions.settle_objectives_state(mid, state) is True
    return mid


def _objectives_state(db, mid) -> str | None:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT objectives_state FROM missions WHERE id=?", (mid,)).fetchone()[0]
    finally:
        con.close()


def _snapshot(mid) -> list[tuple]:
    return [
        (o["key"], o["title"], o["probe"], o["gate"], o["source"]) for o in missions.objectives(mid)
    ]


@pytest.mark.anyio
async def test_a_SETTLED_mission_with_an_old_checklist_id_keeps_its_objectives(
    store, no_requests, monkeypatch
):
    _configured(monkeypatch)
    _reply(monkeypatch, _NOTES)
    mid = _legacy_mission(store, with_objectives=True, state="done")
    before = _snapshot(mid)
    assert missions.get_mission(mid)["playbook_id"] == "ship"

    out = await mo.recover_pending()
    assert out == {"recovered": [], "failed": []}
    assert _snapshot(mid) == before
    assert missions.get_mission(mid)["state"] == "draft", "the mission was held or moved"
    assert _objectives_state(store, mid) == "done"
    assert not any(t.startswith("no objectives proposed") for t in _texts(mid)), _texts(mid)


@pytest.mark.anyio
async def test_boot_recovery_of_an_old_checklist_mission_with_PENDING_objectives_adds_none(
    store, no_requests, monkeypatch
):
    """The crash landed between the old producer's write and its settle: the checklist's rows are
    there and the intent is still `pending`. Recovery must finish the intent without failing on the
    stale `playbook_id` and without adding instruction objectives beside the checklist's own —
    "a mission that already has objectives gets no new ones"."""
    _configured(monkeypatch)
    _reply(monkeypatch, _NOTES)
    mid = _legacy_mission(store, with_objectives=True, state=None)
    before = _snapshot(mid)
    assert _pending_ids() == [mid]

    out = await mo.recover_pending()
    assert out["failed"] == [], out
    assert _snapshot(mid) == before, "the producer added objectives to a mission that had some"
    assert _objectives_state(store, mid) != "failed"
    assert _pending_ids() == []
    assert missions.get_mission(mid)["state"] == "draft"


@pytest.mark.anyio
async def test_an_old_checklist_mission_with_NO_objectives_yet_gets_instruction_ones(
    store, no_requests, monkeypatch
):
    """…and one whose producer never ran is not re-resolved against the checklist it names: it
    gets instruction objectives like any other mission, and settles."""
    _configured(monkeypatch)
    _reply(monkeypatch, _NOTES)
    mid = _legacy_mission(store, with_objectives=False, state=None)

    out = await mo.recover_pending()
    assert out["recovered"] == [mid], out
    assert [(o["key"], o["source"]) for o in missions.objectives(mid)] == [
        ("goal_1", "instruction"),
        ("goal_2", "instruction"),
    ]
    assert _objectives_state(store, mid) == "done"
    assert missions.get_mission(mid)["playbook_id"] == "ship", "the stored column was rewritten"
