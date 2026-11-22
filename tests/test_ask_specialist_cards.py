"""HQ proposes specialists as Library cards (#1938): create, edit, archive and unarchive.

Pinned here:

* a draft `specialists.check_draft` refuses never reaches a card, and HQ adds no limit of its own
  (the store's capacity range is the only one; six specialists in one turn are six cards);
* the agent, model, effort and skills are INDEXES into this turn's own listings, resolved before
  validation: a valid choice the turn did not list is refused, an invented id is refused, and an
  index keeps its row when a list is fetched again or comes back in another order;
* the tap re-runs the route's own checks, and anything that changed since staging is ``stale``
  with the validator's reason (a concurrent edit, a pinned skill's revision, a model the agent no
  longer offers), never invalid; nothing is written;
* archive and unarchive are revision-checked and never go through `check_draft`; a refusal by the
  store that changed nothing is ``refused``;
* a playbook names the specialists it relies on, their state is derived at every read, and
  saving the playbook creates none of them.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json

import pytest

from agent_sessions import (
    ask_library,
    ask_orchestrator,
    ask_specialists,
    ask_store,
    engines,
    prefs,
    specialists,
    voice_calls,
)
from agent_sessions import specialists_store as store
from test_ask_library import _bundle
from test_ask_operations import _confirm, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401
from test_hq_steps import _current_sid, _model, _sid, _wait

AGENT = "claude"
MODEL = "claude-opus-5"
LISTS = (("list_agents", {}), ("list_agent_models", {"engine_index": 0}), ("list_skills", {}))


@pytest.fixture
def pool(monkeypatch, live):
    """Two agents that can start, a three-skill Library whose revisions a test may move, and the
    real specialists store behind the real routes."""
    state = {
        "agents": [
            {"index": 0, "id": AGENT, "name": "Claude Code", "can_start": True},
            {"index": 1, "id": "codex", "name": "Codex", "can_start": True},
        ],
        "lib": {"tdd": "r-tdd-1", "git-hygiene": "r-gh-1", "review": "r-rv-1"},
        "order": ["tdd", "git-hygiene", "review"],
    }
    monkeypatch.setattr(engines.registry, "can_start", lambda prov: prov is not None)
    monkeypatch.setattr(ask_orchestrator, "_agent_rows", lambda: [dict(a) for a in state["agents"]])
    monkeypatch.setattr(specialists, "library_revisions", lambda: dict(state["lib"]))
    real_get = ask_library._get

    async def get(app, operator, path, what):
        if path == "/api/library/skills":
            rows = [s for s in state["order"] if s in state["lib"]]
            return {"skills": [{"id": s, "files": {"SKILL.md": f"# {s}\n"}} for s in rows]}
        return await real_get(app, operator, path, what)

    monkeypatch.setattr(ask_library, "_get", get)
    return state


def _model_index(model: str = MODEL, engine: str = AGENT) -> int:
    return [m["id"] for m in ask_specialists.agent_models(engine)].index(model)


def _draft(**over) -> dict:
    d = {
        "group": "specialist",
        "action": "create",
        "title": "Release Notes Writer",
        "summary": "Writes the release notes from the merged pull requests.",
        "engine_index": 0,
        "model_index": _model_index(),
        "skill_indexes": [0, 1],
        "capacity": 3,
    }
    d.update(over)
    return d


def _ask(a, ai, *steps, question: str = "set it up") -> tuple[str, object]:
    script = ai(*steps, answer("Proposed; confirm it on the card."))
    cid = a.new()
    r = a.ask(cid, question)
    assert r.status_code == 200, r.text
    return cid, script


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _results(script, n: int) -> list[dict]:
    return [json.loads(m["content"]) for m in script.tool_results(n)]


def _content(a, card: dict) -> dict:
    r = a.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=a.hdr)
    assert r.status_code == 200, r.text
    return r.json()


def _settle(a, cid: str, pid: str, timeout: float = 10.0) -> dict:
    import time

    end = time.monotonic() + timeout
    while True:
        card = next(c for c in _cards(a, cid) if c["action_id"] == pid)
        if card["state"] != "deciding" or time.monotonic() > end:
            return card
        time.sleep(0.05)


def _decide(a, cid: str, card: dict, decision: str = "confirm") -> dict:
    r = _confirm(a, card["action_id"], decision)
    assert r.status_code in (200, 202), r.text
    return _settle(a, cid, card["action_id"])


def _propose(a, ai, *proposals: dict, lists=LISTS) -> tuple[str, object]:
    return _ask(
        a,
        ai,
        calls(*lists),
        calls(*[("propose_library", p) for p in proposals]),
    )


def _refusal(script) -> str:
    return json.dumps(_results(script, 2)[len(LISTS) :], ensure_ascii=False)


def _have(sid: str | None = None, **over) -> dict:
    """A specialist in the pool, written through the store's own validator."""
    d = {
        "title": "Code Reviewer",
        "summary": "Reviews a pull request before it merges.",
        "skills": [{"id": "tdd", "revision": specialists.library_revisions()["tdd"]}],
        "engine": AGENT,
        "model": MODEL,
        "effort": None,
        "capacity": 2,
    }
    d.update(over)
    return store.create(specialists.check_draft(d), sid=sid)


# ---- create --------------------------------------------------------------------------------------


def test_create_shows_the_whole_specialist_and_the_tap_creates_it(pool, live, ai):
    cid, script = _propose(live, ai, _draft())
    (card,) = _cards(live, cid)
    assert card["op"] == "specialist.create" and card["state"] == "pending"
    assert card["label"] == "New specialist" and card["title"] == "Release Notes Writer"
    assert card["verb"] == "Create specialist" and card["risk"] == "reversible"
    assert card["text"] == "Creates the specialist Release Notes Writer in your Library."
    shown = _content(live, card)
    assert shown["kind"] == "specialist" and shown["action"] == "create"
    assert shown["before"] is None
    assert shown["after"] == {
        "title": "Release Notes Writer",
        "summary": "Writes the release notes from the merged pull requests.",
        "engine": AGENT,
        "agent": shown["after"]["agent"],
        "model": MODEL,
        "effort": None,
        "grade": None,
        "skills": [
            {"id": "tdd", "revision": "r-tdd-1"},
            {"id": "git-hygiene", "revision": "r-gh-1"},
        ],
        "capacity": 3,
    }
    assert shown["skills_now"] == {"tdd": "r-tdd-1", "git-hygiene": "r-gh-1"}
    assert store.list_all() == []  # nothing exists until the tap

    done = _decide(live, cid, card)
    assert done["state"] == "done", done
    assert done["library"] == {"kind": "specialist", "id": ""}
    (made,) = store.list_all()
    assert made["current"]["title"] == "Release Notes Writer"
    assert made["current"]["model"] == MODEL and made["current"]["capacity"] == 3
    assert made["current"]["skills"] == shown["after"]["skills"]


def test_the_grade_on_the_card_follows_the_model_and_is_never_an_argument(pool, live, ai):
    from agent_sessions import model_grades

    model_grades.apply_patch({AGENT: {MODEL: "frontier"}})
    cid, script = _propose(live, ai, _draft(grade="fast"))
    (card,) = _cards(live, cid)
    assert _content(live, card)["after"]["grade"] == "frontier"
    assert "grade" not in ask_store.read_draft(cid, card["action_id"])[0]["body"]
    properties = ask_orchestrator.TOOLS["propose_library"].spec["function"]["parameters"]
    assert "grade" not in properties["properties"]


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"title": ""}, "title is required"),
        ({"title": "x" * 61}, "longer than 60"),
        ({"skill_indexes": [0, 0]}, "listed twice"),
        ({"capacity": 0}, "capacity must be 1…8"),
        ({"capacity": 9}, "capacity must be 1…8"),
    ],
)
def test_a_draft_the_validator_refuses_never_reaches_a_card(pool, live, ai, over, needle):
    cid, script = _propose(live, ai, _draft(**over))
    assert _cards(live, cid) == []
    assert needle in _refusal(script)
    assert store.list_all() == []


def test_the_stores_whole_capacity_range_stages_with_no_narrower_limit_of_hqs(pool, live, ai):
    low, high = specialists.CAPACITY_MIN, specialists.CAPACITY_MAX
    cid, _ = _propose(
        live, ai, _draft(title="Low", capacity=low), _draft(title="High", capacity=high)
    )
    cards = _cards(live, cid)
    assert [_content(live, c)["after"]["capacity"] for c in cards] == [low, high]


def test_six_specialists_in_one_turn_are_six_cards(pool, live, ai):
    cid, _ = _propose(live, ai, *[_draft(title=f"Specialist {i}") for i in range(6)])
    cards = _cards(live, cid)
    assert [c["title"] for c in cards] == [f"Specialist {i}" for i in range(6)]
    assert {c["state"] for c in cards} == {"pending"}
    for card in cards:
        assert _decide(live, cid, card)["state"] == "done"
    assert len(store.list_all()) == 6


def test_with_no_room_left_the_conversations_own_refusal_is_reported(pool, live, ai, monkeypatch):
    monkeypatch.setattr(ask_store, "MAX_SESSION_PROPOSALS", 2)
    cid, script = _propose(live, ai, *[_draft(title=f"Specialist {i}") for i in range(3)])
    assert [c["title"] for c in _cards(live, cid)] == ["Specialist 0", "Specialist 1"]
    refused = _results(script, 2)[-1]
    assert "holds as many proposals as it can" in refused["error"]
    assert "new conversation" in refused["error"]


def test_a_title_carrying_a_credential_is_refused(pool, live, ai):
    cid, script = _propose(live, ai, _draft(title="sk-ant-api03-AbCdEf0123456789xyz"))
    assert _cards(live, cid) == []
    assert "credential" in _refusal(script)


# ---- indexes, resolved against this turn's listings ----------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"engine": AGENT, "engine_index": None},
        {"engine_index": AGENT},
        {"model": MODEL, "model_index": None},
        {"model_index": MODEL},
        {"skills": [{"id": "tdd", "revision": "r-tdd-1"}], "skill_indexes": None},
        {"skill_indexes": ["tdd"]},
        {"engine_index": 7},
        {"model_index": 999},
        {"skill_indexes": [9]},
        {"effort_index": 0},
    ],
)
def test_an_id_the_model_wrote_or_an_index_nobody_listed_stages_nothing(pool, live, ai, over):
    args = {k: v for k, v in _draft(**over).items() if v is not None}
    cid, script = _propose(live, ai, args)
    assert _cards(live, cid) == []
    assert "refused" in _refusal(script) or "must be" in _refusal(script)
    assert store.list_all() == []


@pytest.mark.parametrize(
    "missing, needle",
    [
        ("list_agents", "call list_agents in this turn first"),
        ("list_agent_models", "call list_agent_models for that agent in this turn first"),
        ("list_skills", "call list_skills in this turn first"),
    ],
)
def test_a_valid_choice_this_turn_did_not_list_is_refused(pool, live, ai, missing, needle):
    """`check_draft` would accept each of these: the agent is registered, the model is offered and
    the skill is in the Library. They are refused because THIS turn never listed them."""
    if missing == "list_agents":
        lists: tuple = (("list_skills", {}),)
    else:
        lists = tuple(step for step in LISTS if step[0] != missing)
    cid, script = _ask(live, ai, calls(*lists), call("propose_library", _draft(), cid="p"))
    assert _cards(live, cid) == []
    assert needle in json.dumps(_results(script, 2)[-1])
    # …and the very same arguments stage once the turn has listed them.
    cid, _ = _propose(live, ai, _draft())
    assert len(_cards(live, cid)) == 1


def test_a_model_listed_for_another_agent_is_not_this_agents(pool, live, ai):
    lists = (("list_agents", {}), ("list_agent_models", {"engine_index": 1}), ("list_skills", {}))
    cid, script = _propose(live, ai, _draft(), lists=lists)
    assert _cards(live, cid) == []
    assert "list_agent_models for that agent" in json.dumps(_results(script, 2)[-1])


def test_an_index_keeps_its_row_when_a_list_is_repeated_or_reordered(pool, live, ai):
    """The first `list_skills` gives tdd 0, git-hygiene 1, review 2. The Library then gains a
    skill and lists in another order; the second read keeps every index and gives the new skill
    the next one, so index 0 is still tdd whichever listing HQ took it from."""

    real = ask_orchestrator.TOOLS["list_skills"].run
    seen = {"n": 0}

    async def list_skills(turn, args):
        seen["n"] += 1
        if seen["n"] == 2:
            pool["lib"]["changelog"] = "r-cl-1"
            pool["order"] = ["changelog", "review", "git-hygiene", "tdd"]
        return await real(turn, args)

    tool = ask_orchestrator.TOOLS["list_skills"]
    patched = type(tool)(tool.name, tool.kind, tool.spec, list_skills)
    try:
        ask_orchestrator.TOOLS["list_skills"] = patched
        cid, script = _ask(
            live,
            ai,
            calls(*LISTS),
            calls(("list_skills", {}), ("list_agents", {})),
            call("propose_library", _draft(skill_indexes=[0, 3]), cid="p"),
        )
    finally:
        ask_orchestrator.TOOLS["list_skills"] = tool
    second = _results(script, 2)[len(LISTS)]
    assert {r["id"]: r["index"] for r in second["skills"]} == {
        "changelog": 3,
        "review": 2,
        "git-hygiene": 1,
        "tdd": 0,
    }
    (card,) = _cards(live, cid)
    assert [k["id"] for k in _content(live, card)["after"]["skills"]] == ["tdd", "changelog"]


def test_a_row_a_later_listing_no_longer_returns_can_no_longer_be_chosen(pool, live, ai):
    real = ask_orchestrator.TOOLS["list_skills"].run
    seen = {"n": 0}

    async def list_skills(turn, args):
        seen["n"] += 1
        if seen["n"] == 2:
            del pool["lib"]["tdd"]
        return await real(turn, args)

    tool = ask_orchestrator.TOOLS["list_skills"]
    try:
        ask_orchestrator.TOOLS["list_skills"] = type(tool)(
            tool.name, tool.kind, tool.spec, list_skills
        )
        cid, script = _ask(
            live,
            ai,
            calls(*LISTS),
            calls(("list_skills", {})),
            call("propose_library", _draft(skill_indexes=[0]), cid="p"),
        )
    finally:
        ask_orchestrator.TOOLS["list_skills"] = tool
    assert _cards(live, cid) == []
    assert "no longer returns" in json.dumps(_results(script, 3)[-1])


def test_a_valid_choice_no_listing_of_a_repeated_or_reordered_turn_returned_is_refused(
    pool, live, ai, monkeypatch
):
    """The agents and the models come back reversed on their second read, and the Library gains
    a skill after the LAST `list_skills`: every index still names the row it was first given,
    and the new skill (valid to `check_draft`, never listed this turn) has no index to name."""
    first_models = ask_specialists.agent_models(AGENT)
    reads = {"agents": 0, "models": 0}
    real_models = ask_specialists.agent_models

    def models(engine):
        reads["models"] += 1
        rows = real_models(engine)
        return rows[::-1] if reads["models"] > 1 else rows

    real_agents = ask_orchestrator._agent_rows

    def agents():
        reads["agents"] += 1
        if reads["agents"] == 2:
            pool["agents"].reverse()
            pool["lib"]["changelog"] = "r-cl-1"  # after the last list_skills of this turn
            pool["order"].append("changelog")
        return real_agents()

    kept, unlisted = _draft(title="Kept"), _draft(title="Never listed", skill_indexes=[0, 3])
    monkeypatch.setattr(ask_specialists, "agent_models", models)
    monkeypatch.setattr(ask_orchestrator, "_agent_rows", agents)
    cid, script = _ask(
        live,
        ai,
        calls(*LISTS),
        calls(("list_skills", {}), ("list_agents", {}), ("list_agent_models", {"engine_index": 0})),
        calls(("propose_library", kept), ("propose_library", unlisted)),
    )
    assert reads == {"agents": 2, "models": 2}
    again = _results(script, 2)[len(LISTS) :]
    assert [s["index"] for s in again[0]["skills"]] == [0, 1, 2]
    assert {a["name"]: a["index"] for a in again[1]["agents"]} == {"Claude Code": 0, "Codex": 1}
    assert {m["model"]: m["index"] for m in again[2]["models"]} == {
        r["id"]: i for i, r in enumerate(first_models)
    }
    (card,) = _cards(live, cid)
    assert card["title"] == "Kept"
    shown = _content(live, card)["after"]
    assert (shown["engine"], shown["model"]) == (AGENT, MODEL)
    refused = json.dumps(_results(script, 3)[-1])
    assert "skill_index must be an index list_skills returned in this turn" in refused
    # The store itself would take the skill: only this turn's listings stand between.
    specialists.check_draft(
        {
            "title": "Valid to the store",
            "summary": "",
            "skills": [{"id": "changelog", "revision": "r-cl-1"}],
            "engine": AGENT,
            "model": MODEL,
            "effort": None,
            "capacity": 1,
        }
    )


def test_an_automation_never_takes_an_agent_a_later_list_agents_dropped():
    """`stable` keeps a dropped row in place (its index is never reused); every reader of the
    turn's agents refuses it, an automation's `start_session` too."""
    agents = ask_specialists.stable(None, [{"id": AGENT}, {"id": "codex"}])
    agents = ask_specialists.stable(agents, [{"id": AGENT}])
    args = {"action_kind": "start_session", "message": "Run it.", "project_index": 0}
    kw = {"projects": [{"id": "p", "cwd": "/w"}], "agents": agents, "keys": None}
    with pytest.raises(ask_library.Refused, match="no longer returns"):
        ask_library.build_action({**args, "engine_index": 1}, folder_of=lambda p: "/w", **kw)
    built = ask_library.build_action({**args, "engine_index": 0}, folder_of=lambda p: "/w", **kw)
    assert built["engine"] == AGENT


def test_an_effort_is_an_index_into_the_selected_models_own_levels(pool, live, ai, monkeypatch):
    """An API client's model lists its effort levels; the level is resolved from THAT model's
    list as this turn read it, and one the model does not list is refused before validation."""
    api = next(
        p.engine_id
        for p in engines.all_providers()
        if p.manifest and p.manifest.identity.kind == "agent" and p.manifest.runtime == "api"
    )
    pool["agents"][1] = {"index": 1, "id": api, "name": "API client", "can_start": True}
    base = _draft(engine_index=1, skill_indexes=[])
    rows = [
        {"id": "default", "label": "the agent's default", "efforts": []},
        {"id": "big-model", "label": "Big", "efforts": ["low", "high"], "grade": None},
        {"id": "small-model", "label": "Small", "efforts": [], "grade": None},
    ]
    monkeypatch.setattr(ask_specialists, "agent_models", lambda engine: [dict(r) for r in rows])
    checked: list[tuple] = []

    def select_api_choice(prov, model, effort):
        checked.append((model, effort))
        return model, effort

    from agent_sessions import model_choice

    monkeypatch.setattr(model_choice, "select_api_choice", select_api_choice)
    lists = (("list_agents", {}), ("list_agent_models", {"engine_index": 1}), ("list_skills", {}))
    cid, script = _propose(
        live,
        ai,
        {**base, "title": "High", "model_index": 1, "effort_index": 1},
        {**base, "title": "None offered", "model_index": 2, "effort_index": 0},
        {**base, "title": "Past the end", "model_index": 1, "effort_index": 2},
        {**base, "title": "Written", "model_index": 1, "effort": "high"},
        lists=lists,
    )
    listed = _results(script, 1)[1]["models"][1]
    assert listed["efforts"] == [{"index": 0, "level": "low"}, {"index": 1, "level": "high"}]
    cards = _cards(live, cid)
    assert [c["title"] for c in cards] == ["High", "Written"]
    assert _content(live, cards[0])["after"]["effort"] == "high"
    assert _content(live, cards[1])["after"]["effort"] is None  # a written level is not read
    assert ("big-model", "high") in checked and ("small-model", "low") not in checked
    refused = json.dumps(_results(script, 2)[len(lists) :])
    assert refused.count("effort_index must be an index from that model's efforts") == 2


# ---- stale at the tap ----------------------------------------------------------------------------


def test_a_pinned_skill_that_moved_before_the_tap_is_stale_with_the_validators_reason(
    pool, live, ai
):
    cid, _ = _propose(live, ai, _draft())
    (card,) = _cards(live, cid)
    pool["lib"]["tdd"] = "r-tdd-2"
    out = _decide(live, cid, card)
    assert out["state"] == "stale", out
    assert out["reason"] == (
        "Skill tdd changed since you loaded it: reload and save again. Nothing was created."
    )
    assert store.list_all() == []
    # The card keeps what it was made with, and marks the pin that moved.
    shown = _content(live, card)
    assert shown["after"]["skills"][0] == {"id": "tdd", "revision": "r-tdd-1"}
    assert shown["skills_now"]["tdd"] == "r-tdd-2"


def test_a_model_the_agent_no_longer_offers_is_stale_with_the_validators_reason(pool, live, ai):
    prefs.set_agent_defaults({"models": {AGENT: ["house-model-1"]}})
    cid, _ = _propose(live, ai, _draft(model_index=_model_index("house-model-1")))
    (card,) = _cards(live, cid)
    assert _content(live, card)["after"]["model"] == "house-model-1"
    prefs.set_agent_defaults({"models": {AGENT: []}})
    out = _decide(live, cid, card)
    assert out["state"] == "stale", out
    assert "house-model-1" in out["reason"] and "is not offered" in out["reason"]
    assert out["reason"].endswith("Nothing was created.")
    assert store.list_all() == []


def _edit(a, ai, index: int = 0, **changes) -> tuple[str, object]:
    return _ask(
        a,
        ai,
        calls(*LISTS, ("list_specialists", {})),
        call("specialist_detail", {"specialist_index": index}, cid="d"),
        call(
            "propose_library",
            {"group": "specialist", "action": "edit", "specialist_index": index, **changes},
            cid="p",
        ),
    )


def test_a_concurrent_edit_before_the_tap_is_stale_and_saves_nothing(pool, live, ai):
    made = _have()
    cid, _ = _edit(live, ai, capacity=6)
    (card,) = _cards(live, cid)
    elsewhere = specialists.check_draft({**_body(made), "summary": "The operator's own edit."})
    store.update(made["id"], elsewhere, expect_revision=made["revision"])
    out = _decide(live, cid, card)
    assert out["state"] == "stale", out
    assert out["reason"] == (
        "Code Reviewer was edited since this card was made. Nothing was saved."
    )
    now = store.get(made["id"])["current"]
    assert now["version"] == 2 and now["capacity"] == 2
    assert now["summary"] == "The operator's own edit."


def _body(spec: dict) -> dict:
    return {k: spec["current"][k] for k in specialists.FIELDS}


def test_the_routes_own_conflict_is_stale_too(pool, live, ai, monkeypatch):
    """The re-read at the tap catches a moved revision first; a write that lands between that
    re-read and the route is the route's 409, and reads the same."""
    made = _have()
    cid, _ = _edit(live, ai, capacity=6)
    (card,) = _cards(live, cid)

    def conflict(sid, body, *, expect_revision, now=None):
        raise store.Conflict()

    monkeypatch.setattr(store, "update", conflict)
    out = _decide(live, cid, card)
    assert out["state"] == "stale", out
    assert out["reason"] == "It was edited since this card was made. Nothing was saved."
    assert store.get(made["id"])["current"]["version"] == 1


# ---- edit ----------------------------------------------------------------------------------------


def test_specialist_detail_reads_every_field_and_the_current_version(pool, live, ai):
    _have()
    cid, script = _ask(
        live,
        ai,
        calls(("list_agents", {}), ("list_specialists", {})),
        call("specialist_detail", {"specialist_index": 0}, cid="d"),
    )
    shown = _results(script, 2)[-1]
    assert {k: shown[k] for k in ("title", "summary", "agent", "model", "effort", "capacity")} == {
        "title": "Code Reviewer",
        "summary": "Reviews a pull request before it merges.",
        "agent": "Claude Code",
        "model": MODEL,
        "effort": None,
        "capacity": 2,
    }
    assert shown["skills"] == [{"skill": "tdd", "pinned": "current"}]
    assert shown["version"] == 1 and shown["archived"] is False and shown["grade"] == "ungraded"
    assert ask_orchestrator.TOOLS["specialist_detail"].kind == "read"
    assert _cards(live, cid) == []


def test_an_edit_needs_the_specialist_read_in_full_this_turn(pool, live, ai):
    _have()
    cid, script = _ask(
        live,
        ai,
        calls(*LISTS, ("list_specialists", {})),
        call(
            "propose_library",
            {"group": "specialist", "action": "edit", "specialist_index": 0, "capacity": 6},
            cid="p",
        ),
    )
    assert _cards(live, cid) == []
    assert "specialist_detail in this turn first" in json.dumps(_results(script, 2)[-1])


def test_an_edit_shows_both_versions_and_the_tap_saves_a_new_one(pool, live, ai):
    made = _have()
    cid, _ = _edit(live, ai, capacity=6, skill_indexes=[0, 2], model_index=_model_index("default"))
    (card,) = _cards(live, cid)
    assert card["op"] == "specialist.edit" and card["label"] == "Edit specialist"
    assert card["title"] == "Code Reviewer · version 1 → 2"
    assert card["verb"] == "Save as version 2"
    assert card["text"] == (
        "Saves version 2 of Code Reviewer. Missions that pinned version 1 keep it."
    )
    shown = _content(live, card)
    assert shown["action"] == "edit" and shown["version"] == {"from": 1, "to": 2}
    assert shown["before"]["capacity"] == 2 and shown["after"]["capacity"] == 6
    assert shown["before"]["model"] == MODEL and shown["after"]["model"] == "default"
    assert [k["id"] for k in shown["after"]["skills"]] == ["tdd", "review"]
    assert shown["after"]["title"] == shown["before"]["title"] == "Code Reviewer"

    out = _decide(live, cid, card)
    assert out["state"] == "done" and out["summary"] == "Saved as version 2."
    assert out["library"] == {"kind": "specialist", "id": made["id"]}
    now = store.get(made["id"])["current"]
    assert now["version"] == 2 and now["capacity"] == 6 and now["model"] == "default"
    assert store.version(made["id"], 1)["capacity"] == 2  # the pinned version is kept


def test_an_edit_that_changes_nothing_or_breaks_the_validator_stages_nothing(pool, live, ai):
    _have()
    cid, script = _edit(live, ai, capacity=2)
    assert _cards(live, cid) == [] and "changes nothing" in json.dumps(_results(script, 3)[-1])
    cid, script = _edit(live, ai, capacity=40)
    assert _cards(live, cid) == [] and "capacity must be" in json.dumps(_results(script, 3)[-1])


# ---- archive and unarchive -----------------------------------------------------------------------


def _archive(a, ai, action: str = "archive", index: int = 0) -> tuple[str, object]:
    return _ask(
        a,
        ai,
        calls(("list_agents", {}), ("list_specialists", {})),
        call(
            "propose_library",
            {"group": "specialist", "action": action, "specialist_index": index},
            cid="p",
        ),
    )


def test_archive_and_unarchive_are_cards_that_change_no_version(pool, live, ai):
    made = _have()
    cid, _ = _archive(live, ai)
    (card,) = _cards(live, cid)
    assert card["op"] == "specialist.archive" and card["label"] == "Archive specialist"
    assert card["title"] == "Code Reviewer · version 1" and card["verb"] == "Archive"
    assert card["text"] == "No new launches. Versions that missions pinned stay readable."
    assert card["risk"] == "reversible" and "content" not in card
    assert store.get(made["id"])["archived"] is False
    assert _decide(live, cid, card)["state"] == "done"
    assert store.get(made["id"])["archived"] is True

    # An archived specialist is found through list_specialists, and unarchived by its index.
    cid, script = _archive(live, ai, "unarchive")
    (listed,) = _results(script, 1)[1]["specialists"]
    assert listed["archived"] is True and listed["can_start"] is False
    (card,) = _cards(live, cid)
    assert card["op"] == "specialist.unarchive" and card["verb"] == "Unarchive"
    assert card["title"] == "Code Reviewer · version 1 · archived"
    assert card["text"] == (
        "It can be launched again. Nothing about it changes: it comes back as version 1."
    )
    assert _decide(live, cid, card)["state"] == "done"
    now = store.get(made["id"])
    assert now["archived"] is False and now["current"]["version"] == 1


def test_archiving_what_is_archived_or_unarchiving_what_is_not_stages_nothing(pool, live, ai):
    _have()
    cid, script = _archive(live, ai, "unarchive")
    assert _cards(live, cid) == [] and "it is not archived" in json.dumps(_results(script, 2))


@pytest.mark.parametrize("action", ["archive", "unarchive"])
def test_a_revision_that_moved_before_the_tap_is_a_stale_archive_card(pool, live, ai, action):
    made = _have()
    if action == "unarchive":
        made = store.set_archived(made["id"], True, expect_revision=made["revision"])
    cid, _ = _archive(live, ai, action)
    (card,) = _cards(live, cid)
    # Flipped there and back elsewhere: the same archive state, another revision.
    other = store.set_archived(made["id"], action == "archive", expect_revision=made["revision"])
    store.set_archived(made["id"], action != "archive", expect_revision=other["revision"])
    out = _decide(live, cid, card)
    assert out["state"] == "stale", out
    assert out["reason"] == "Code Reviewer was edited since this card was made. Nothing changed."
    assert store.get(made["id"])["archived"] is (action == "unarchive")


def _save_before_staging_reads(monkeypatch, write) -> list:
    """A competing save that lands AFTER HQ's draft was prepared from its read and BEFORE staging
    reads the facts the card binds (#1938 review): `write()` runs once, at that first facts read."""
    real, done = ask_specialists.facts, []

    async def facts(op, target, app, operator):
        if not done:
            done.append(write())
        return await real(op, target, app, operator)

    monkeypatch.setattr(ask_specialists, "facts", facts)
    return done


def _no_card_and_said_why(a, cid, script, n: int) -> None:
    assert _cards(a, cid) == []
    refused = json.dumps(_results(script, n)[-1])
    assert "saved, archived or unarchived elsewhere while the card was prepared" in refused


def test_a_save_between_the_edits_read_and_its_staging_never_binds_the_older_draft(
    pool, live, ai, monkeypatch
):
    """Version 1 is read and HQ's capacity-only edit is drafted from it; the operator's summary
    edit lands (version 2) before staging reads the facts. The card must not bind version 2's
    revision while it carries version 1's replacement: nothing stages, and the summary survives."""
    made = _have()
    _save_before_staging_reads(
        monkeypatch,
        lambda: store.update(
            made["id"],
            specialists.check_draft({**_body(made), "summary": "The operator's own edit."}),
            expect_revision=made["revision"],
        ),
    )
    cid, script = _edit(live, ai, capacity=6)
    _no_card_and_said_why(live, cid, script, 3)
    now = store.get(made["id"])["current"]
    assert now["version"] == 2 and now["capacity"] == 2
    assert now["summary"] == "The operator's own edit."


@pytest.mark.parametrize("action", ["archive", "unarchive"])
def test_a_save_between_an_archive_cards_read_and_its_staging_refuses_the_card(
    pool, live, ai, monkeypatch, action
):
    """The version an archive or unarchive card shows and the revision it binds come from ONE
    read: a save in between (here: flipped elsewhere and back) refuses the card."""
    made = _have()
    if action == "unarchive":
        made = store.set_archived(made["id"], True, expect_revision=made["revision"])

    def flip_and_back():
        other = store.set_archived(
            made["id"], action == "archive", expect_revision=made["revision"]
        )
        return store.set_archived(
            made["id"], action != "archive", expect_revision=other["revision"]
        )

    saved = _save_before_staging_reads(monkeypatch, flip_and_back)
    cid, script = _archive(live, ai, action)
    _no_card_and_said_why(live, cid, script, 2)
    assert store.get(made["id"])["revision"] == saved[0]["revision"]
    assert store.get(made["id"])["archived"] is (action == "unarchive")


def test_archive_works_for_a_specialist_whose_pins_no_longer_validate(pool, live, ai):
    """Its skill moved and its model is gone: `check_draft` would refuse this specialist today.
    Archive and unarchive never ask it."""
    prefs.set_agent_defaults({"models": {AGENT: ["house-model-1"]}})
    made = _have(model="house-model-1")
    prefs.set_agent_defaults({"models": {AGENT: []}})
    pool["lib"]["tdd"] = "r-tdd-9"
    with pytest.raises(specialists.DraftRefused):
        specialists.check_draft(_body(made))
    cid, _ = _archive(live, ai)
    assert _decide(live, cid, _cards(live, cid)[0])["state"] == "done"
    assert store.get(made["id"])["archived"] is True
    cid, _ = _archive(live, ai, "unarchive")
    assert _decide(live, cid, _cards(live, cid)[0])["state"] == "done"
    assert store.get(made["id"])["archived"] is False


def test_a_refusal_by_the_store_reads_refused_in_its_own_words(pool, live, ai, monkeypatch):
    """HQ follows the STORE's policy (#1935 refuses archiving built-in staff there): whatever the
    store refuses without changing anything is ``refused``, never ``failed`` or ``stale``."""
    made = _have()
    cid, _ = _archive(live, ai)
    (card,) = _cards(live, cid)

    def refuse(sid, archived, *, expect_revision, now=None):
        raise store.StoreError("built-in staff can't be archived", status=409)

    monkeypatch.setattr(store, "set_archived", refuse)
    out = _decide(live, cid, card)
    assert out["state"] == "refused", out
    assert out["summary"] == "Built-in staff can't be archived. Nothing changed."
    assert out["can_confirm"] is False and out["can_dismiss"] is False
    assert store.get(made["id"])["archived"] is False


# ---- specialists for a playbook ------------------------------------------------------------------


def _playbook(**extra) -> dict:
    return {
        "group": "playbook",
        "action": "create",
        "files": _bundle(),
        "specialists": ["Release Notes Writer", "Code Reviewer", "QA Runner"],
        **extra,
    }


def _states(a, card: dict) -> dict:
    return {r["title"]: r["state"] for r in _content(a, card)["relies"]}


#: #2251 D9: a playbook and the specialists it needs are TWO steps, the specialists first.
TWO_STEPS = {
    "title": "Release train",
    "steps": [
        {"title": "The specialists it needs"},
        {"title": "The playbook", "needs": [0]},
    ],
}


def _two_steps(a, ai, monkeypatch, *specialists_args: dict, then=None):
    """Sort first: the step list, then ONLY step 1's specialist card(s). Once step 1 is done, the
    server's continuation stages step 2's playbook card (``then``: what that turn replies
    instead)."""
    ai()  # the endpoint's config; the fake model below answers
    replies = [
        calls(*LISTS),
        call("propose_steps", TWO_STEPS, cid="ps"),
        lambda b: calls(
            *[("propose_library", {**x, "for_step": _sid(b, 1)}) for x in specialists_args]
        ),
        answer("Two steps: the specialist first, then the playbook."),
        *(
            then
            or (
                lambda b: call(
                    "propose_library", {**_playbook(), "for_step": _current_sid(b)}, cid="pb"
                ),
                answer("Step 1 is done; now the playbook."),
            )
        ),
    ]
    model = _model(monkeypatch, *replies)
    cid = a.new()
    assert a.ask(cid, "a release-train playbook with the specialists it needs").status_code == 200
    return cid, model


def _after_tap(a, cid: str, turns: int) -> dict:
    return _wait(a, cid, lambda c: len(c["turns"]) == turns and _turns_settled(c))


def _turns_settled(conv) -> bool:
    cards = [p for t in conv["turns"] for p in t["proposals"]]
    return all(t["status"] not in ("pending", "queued") for t in conv["turns"]) and all(
        c["state"] != "deciding" for c in cards
    )


def test_a_playbook_and_the_specialists_it_needs_are_two_steps_and_its_card_names_them(
    pool, live, ai, monkeypatch
):
    have = _have()
    cid, model = _two_steps(live, ai, monkeypatch, _draft())
    conv = live.get(cid)
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["now", "next"]
    (specialist,) = _cards(live, cid)  # ONE card on screen: step 1's
    assert specialist["op"] == "specialist.create"
    assert lst["steps"][0]["cards"] == [specialist["action_id"]]
    assert [s["id"] for s in store.list_all()] == [have["id"]]

    assert _decide(live, cid, specialist)["state"] == "done"
    conv = _after_tap(live, cid, 2)
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["done", "now"]
    (playbook,) = conv["turns"][1]["proposals"]
    assert playbook["op"] == "playbook.create"
    assert lst["steps"][1]["cards"] == [playbook["action_id"]]
    relies = _content(live, playbook)["relies"]
    assert relies == [
        {
            "title": "Release Notes Writer",
            "state": "exists",
            "version": 1,
            "engine": AGENT,
            "agent": relies[0]["agent"],
            "model": MODEL,
        },
        {
            "title": "Code Reviewer",
            "state": "exists",
            "version": 1,
            "engine": AGENT,
            "agent": relies[1]["agent"],
            "model": MODEL,
        },
        {"title": "QA Runner", "state": "missing"},
    ]
    assert _content(live, playbook)["kind"] == "files"  # the files are shown as before
    assert playbook["can_confirm"] is True  # a missing specialist never blocks it
    assert _decide(live, cid, playbook)["state"] == "done"
    assert len(store.list_all()) == 2  # the playbook created nothing


def test_a_playbook_and_its_specialists_without_a_step_list_are_refused(pool, live, ai):
    """#2251's one-kind rule holds for them: this branch never special-cases it."""
    cid, script = _propose(live, ai, _draft(), _playbook())
    (card,) = _cards(live, cid)
    assert card["op"] == "specialist.create"
    refused = _results(script, 2)[-1]["error"]
    assert refused.startswith("refused: this turn already proposed a different kind")
    assert "propose_steps" in refused
    assert live.get(cid)["step_lists"] == []


def test_six_specialists_are_one_step_a_batch_before_the_playbook(pool, live, ai, monkeypatch):
    six = [_draft(title=f"Specialist {i}") for i in range(6)]
    cid, _ = _two_steps(live, ai, monkeypatch, *six)
    cards = _cards(live, cid)
    assert [c["title"] for c in cards] == [f"Specialist {i}" for i in range(6)]
    (lst,) = live.get(cid)["step_lists"]
    assert lst["steps"][0]["cards"] == [c["action_id"] for c in cards]
    for card in cards[:-1]:
        assert _decide(live, cid, card)["state"] == "done"
    assert len(live.get(cid)["turns"]) == 1  # the step is done only when the whole batch is
    assert _decide(live, cid, cards[-1])["state"] == "done"
    conv = _after_tap(live, cid, 2)
    (playbook,) = conv["turns"][1]["proposals"]
    assert playbook["op"] == "playbook.create"


def test_a_declined_specialist_step_holds_and_no_playbook_is_staged(pool, live, ai, monkeypatch):
    cid, _ = _two_steps(live, ai, monkeypatch, _draft(), then=(answer("Change, skip or end?"),))
    (specialist,) = _cards(live, cid)
    assert _decide(live, cid, specialist, "dismiss")["state"] == "dismissed"
    conv = _wait(live, cid, lambda c: _turns_settled(c) and c["step_lists"][0]["status"] == "held")
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["held", "next"]
    assert [p["op"] for t in conv["turns"] for p in t["proposals"]] == ["specialist.create"]
    assert store.list_all() == []


def _two_turns(a, ai, first: dict, second: dict) -> str:
    """Two requests, one card kind each: a specialist asked for, then a playbook asked for."""
    ai(
        calls(*LISTS),
        call("propose_library", first, cid="p1"),
        answer("Proposed."),
        call("propose_library", second, cid="p2"),
        answer("Proposed."),
    )
    cid = a.new()
    assert a.ask(cid, "a release notes writer").status_code == 200
    assert a.ask(cid, "and a release-train playbook").status_code == 200
    return cid


def test_saving_the_playbook_first_creates_no_specialist(pool, live, ai):
    """Cards of separate requests may be confirmed in either order: the playbook's list stays
    truthful and saving it creates nothing."""
    cid = _two_turns(live, ai, _draft(), _playbook())
    specialist, playbook = _cards(live, cid)
    assert _states(live, playbook)["Release Notes Writer"] == "proposed"
    assert _content(live, playbook)["relies"][0]["where"] == "above"
    assert _decide(live, cid, playbook)["state"] == "done"
    assert store.list_all() == []
    assert _states(live, playbook)["Release Notes Writer"] == "proposed"
    assert _decide(live, cid, specialist)["state"] == "done"
    assert _states(live, playbook) == {
        "Release Notes Writer": "exists",
        "Code Reviewer": "missing",
        "QA Runner": "missing",
    }


def test_the_specialist_first_then_the_playbook(pool, live, ai):
    cid = _two_turns(live, ai, _draft(), _playbook())
    specialist, playbook = _cards(live, cid)
    assert _decide(live, cid, specialist)["state"] == "done"
    assert _states(live, playbook)["Release Notes Writer"] == "exists"
    assert live.c.get("/api/playbooks/release-train", headers=live.hdr).status_code == 404
    assert _decide(live, cid, playbook)["state"] == "done"
    assert len(store.list_all()) == 1


def test_a_declined_or_failed_companion_reads_missing_never_proposed(pool, live, ai):
    cid = _two_turns(live, ai, _draft(), _playbook())
    specialist, playbook = _cards(live, cid)
    assert _decide(live, cid, specialist, "dismiss")["state"] == "dismissed"
    assert _states(live, playbook)["Release Notes Writer"] == "missing"
    assert store.list_all() == []

    cid = _two_turns(live, ai, _draft(), _playbook())
    specialist, playbook = _cards(live, cid)
    pool["lib"]["tdd"] = "r-tdd-2"  # the companion goes stale at its tap
    assert _decide(live, cid, specialist)["state"] == "stale"
    assert _states(live, playbook)["Release Notes Writer"] == "missing"
    assert _decide(live, cid, playbook)["state"] == "done"
    assert store.list_all() == []


def test_a_playbook_with_no_specialists_named_shows_no_list(pool, live, ai):
    args = _playbook()
    del args["specialists"]
    cid, _ = _propose(live, ai, args)
    assert "relies" not in _content(live, _cards(live, cid)[0])


# ---- the catalog, the tools and a call -----------------------------------------------------------


def test_the_group_and_its_four_operations_are_in_the_catalog():
    from agent_sessions import ask_operations

    assert "specialist" in ask_library.GROUPS
    ops = {o.action: o for o in ask_operations.CATALOG.values() if o.group == "specialist"}
    assert set(ops) == set(ask_specialists.ACTIONS)
    assert {o.risk for o in ops.values()} == {"reversible"}
    assert {a for a, o in ops.items() if o.draft} == {"create", "edit"}
    assert "refused" in ask_operations.STATES and "refused" in ask_store.OPERATION_STATES


def test_the_reads_are_draft_readable_and_the_prompt_says_hq_may_propose_specialists():
    from agent_sessions import prompts

    assert {"specialist_detail", "list_agent_models"} <= ask_orchestrator.DRAFT_READ_TOOLS
    text = " ".join(prompts.get("ask_orchestrator").default.split())
    for needle in (
        "Specialists (the Commander's pool) are Library cards too, as many as asked",
        "specialist_detail before an edit",
        "only by this turn's list_agents, list_agent_models and list_skills indexes",
        "the grade follows the model",
        "they are the step before it; saving it creates none",
    ):
        assert needle in text, needle


@pytest.mark.parametrize("action", ask_specialists.ACTIONS)
def test_a_specialist_card_is_a_screen_decision_on_a_call(action):
    """A Library card offers Confirm only once its content is on screen (#1611): a voice never
    confirms a specialist card, and the call says why, as it does for every Library change."""
    op_id = f"specialist.{action}"
    assert voice_calls._voice_operation(op_id) is False
    assert op_id not in voice_calls.VOICE_DRAFTS
    view = {"proposals": [{"kind": "operation", "action_id": "a" * 32, "op": op_id, "needs": []}]}
    assert voice_calls.screen_reasons(view) == {
        "a" * 32: "Library changes are confirmed on screen, where their content is."
    }


def test_stable_keeps_every_index_and_marks_what_a_later_read_dropped():
    first = ask_specialists.stable(None, [{"id": "a"}, {"id": "b"}])
    again = ask_specialists.stable(first, [{"id": "c"}, {"id": "a", "name": "new"}])
    assert [(r["id"], r["index"], r["gone"]) for r in again] == [
        ("a", 0, False),
        ("b", 1, True),
        ("c", 2, False),
    ]
    assert again[0]["name"] == "new"
    with pytest.raises(ask_library.Refused, match="no longer returns"):
        ask_specialists.pick({"i": 1}, "i", again, "list_things")
    with pytest.raises(ask_library.Refused, match="in this turn first"):
        ask_specialists.pick({"i": 0}, "i", None, "list_things")
