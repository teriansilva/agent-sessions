"""HQ proposes a session FROM a specialist; the tap starts that pinned version (#1910).

* **list_specialists** reads the pool the way the Specialists page does (`GET /api/specialists`,
  in-process) and answers by index, unarchived only, with `can_start` / `why_not`.
* **propose_session(specialist_index=…)** takes the agent from the specialist; an `engine_index`
  naming another agent, a specialist that can't start, or an index from a list this turn never
  fetched is refused and nothing is written. The card's agent line names the specialist and the
  version; the sidecar entry pins ``{id, version}``.
* **The tap** dispatches with ``specialist=(id, version)`` and that version's model, and is out of
  date (nothing starts) when the specialist was edited, archived or removed since.

The dispatcher is `test_ask_sessions.world`'s stand-in, wrapped to record the specialist kwargs.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import json
import uuid

import pytest

from agent_sessions import ask_library, ask_store, headless_dispatch, specialists_store
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    ai,
    answer,
    app,
    call,
    calls,
)
from test_ask_sessions import BRIEF, live, world  # noqa: F401 — fixtures are used by name


def _spec(sid="researcher", version=3, *, engine="claude", model="default", **extra) -> dict:
    current = {
        "version": version,
        "title": "Researcher",
        "summary": "Researches on the web and cites every fact.",
        "skills": [{"id": "research-with-citations", "revision": "r1"}],
        "engine": engine,
        "model": model,
        "effort": None,
        "capacity": 2,
        "created_at": 1.0,
    }
    return {
        "id": sid,
        "revision": version,
        "archived": False,
        "current": current,
        "grade": None,
        "live": 0,
        "skills_missing": [],
        "skills_outdated": [],
        **extra,
    }


@pytest.fixture
def pool(monkeypatch, world):
    """The pool `GET /api/specialists` reports and the store the tap re-reads, one list."""
    state = {"specialists": [_spec()], "world": world}
    real_get = ask_library._get

    async def get(app, operator, path, what):
        if path == "/api/specialists":
            return {"specialists": state["specialists"], "templates": []}
        return await real_get(app, operator, path, what)

    def store_get(sid):
        for s in state["specialists"]:
            if s["id"] == sid:
                return s
        raise specialists_store.NotFound()

    def store_version(sid, n):
        return store_get(sid)["current"]

    # The Library as `specialists.library_revisions` reads it: every pinned skill at its revision
    # unless a test moves one (`moved`), or the library can't be read (`library_down`).
    from agent_sessions import specialists

    state["moved"] = None
    state["library_down"] = False

    def library_revisions():
        if state["library_down"]:
            raise OSError("the skills library is busy")
        revs = {"research-with-citations": "r1"}
        if state["moved"]:
            revs[state["moved"]] = "r2"
        return revs

    monkeypatch.setattr(specialists, "library_revisions", library_revisions)
    monkeypatch.setattr(ask_library, "_get", get)
    monkeypatch.setattr(specialists_store, "get", store_get)
    monkeypatch.setattr(specialists_store, "version", store_version)

    # Record what the tap hands the dispatcher beyond the stand-in's own arguments.
    stand_in = headless_dispatch.dispatch
    state["extra"] = []

    async def dispatch(*, specialist=None, model=None, **kw):
        state["extra"].append({"specialist": specialist, "model": model})
        return await stand_in(**kw)

    monkeypatch.setattr(headless_dispatch, "dispatch", dispatch)
    return state


def _turn(ai, args: dict, *, lists=("list_projects", "list_specialists", "list_agents")):
    return ai(
        calls(*[(name, {}) for name in lists]),
        call("propose_session", args, cid="s"),
        answer("I proposed a research session; start it from the card."),
    )


def _result(script) -> dict:
    return json.loads(script.tool_results(2)[-1]["content"])


def _ask(a, ai, args, **kw):
    script = _turn(ai, args, **kw)
    cid = a.new()
    turn = a.ask(cid, "research which speech models run in real time").json()["turn"]
    return cid, turn, script


# ---- listing and proposing ----------------------------------------------------------------------


def test_list_specialists_answers_by_index_and_leaves_archived_ones_out(app, ai, pool):
    pool["specialists"].append(_spec("old", 1, archived=True))
    script = ai(calls(("list_specialists", {})), answer("ok"))
    app.ask(app.new(), "who can research?")
    (listed,) = [json.loads(m["content"]) for m in script.tool_results(1)]
    assert listed == {
        "specialists": [
            {
                "index": 0,
                "title": "Researcher",
                "summary": "Researches on the web and cites every fact.",
                "agent": "Claude Code",
                "model": "default",
                "skills": ["research-with-citations"],
                "version": 3,
                "can_start": True,
            }
        ]
    }


def test_a_specialist_proposal_pins_its_version_and_names_it_on_the_card(app, ai, pool):
    cid, turn, script = _ask(app, ai, {"project_index": 0, "specialist_index": 0, "brief": BRIEF})
    assert _result(script)["proposed"] is True
    (card,) = turn["proposals"]
    assert card["agent"] == "Claude Code · Researcher v3" and card["text"] == BRIEF
    (row,) = ask_store.read_proposals(cid)
    assert row["engine"] == "claude"
    assert row["specialist"] == {"id": "researcher", "version": 3}
    assert pool["extra"] == []  # nothing starts


def test_both_indexes_naming_the_same_agent_is_accepted(app, ai, pool):
    """The four combinations (Hermes on #1910): specialist alone and engine alone are covered
    above and in test_ask_sessions; neither is refused below; both agreeing is accepted here."""
    cid, turn, script = _ask(
        app, ai, {"project_index": 0, "specialist_index": 0, "engine_index": 0, "brief": BRIEF}
    )
    assert _result(script)["proposed"] is True
    (row,) = ask_store.read_proposals(cid)
    assert row["engine"] == "claude" and row["specialist"] == {"id": "researcher", "version": 3}


def test_a_plain_proposal_writes_no_specialist_field(app, ai, pool):
    cid, _turn, _ = _ask(app, ai, {"project_index": 0, "engine_index": 0, "brief": BRIEF})
    (row,) = ask_store.read_proposals(cid)
    assert "specialist" not in row


@pytest.mark.parametrize(
    "setup, args, needle",
    [
        (None, {"project_index": 0, "brief": BRIEF}, "name an agent"),
        (None, {"project_index": 0, "specialist_index": 4, "brief": BRIEF}, "must be an index"),
        (
            lambda p: p["specialists"].__setitem__(
                0, _spec(skills_missing=["research-with-citations"])
            ),
            {"project_index": 0, "specialist_index": 0, "brief": BRIEF},
            "missing from the Library",
        ),
        (
            lambda p: p["specialists"].__setitem__(
                0, _spec(skills_outdated=["research-with-citations"])
            ),
            {"project_index": 0, "specialist_index": 0, "brief": BRIEF},
            "changed in the Library",
        ),
        (
            lambda p: (
                p["world"]["agents"].append(
                    {"index": 1, "id": "codex", "name": "Codex", "can_start": True}
                ),
                p["specialists"].__setitem__(0, _spec(engine="codex")),
            ),
            {"project_index": 0, "specialist_index": 0, "engine_index": 0, "brief": BRIEF},
            "runs on Codex, not Claude Code",
        ),
        (
            # An agent that is not installed is never named by its id, and can't start.
            lambda p: p["specialists"].__setitem__(0, _spec(engine="gemini")),
            {"project_index": 0, "specialist_index": 0, "brief": BRIEF},
            "the agent it runs on is not installed here",
        ),
    ],
    ids=[
        "no-agent",
        "forged-index",
        "skill-missing",
        "skill-outdated",
        "agent-mismatch",
        "agent-not-installed",
    ],
)
def test_a_specialist_proposal_that_cannot_start_writes_nothing(app, ai, pool, setup, args, needle):
    if setup is not None:
        setup(pool)
    cid, turn, script = _ask(app, ai, args)
    assert needle in _result(script)["error"]
    assert turn["proposals"] == [] and ask_store.read_proposals(cid) == []


def test_a_specialist_index_from_a_list_this_turn_never_fetched_is_refused(app, ai, pool):
    cid, _turn, script = _ask(
        app,
        ai,
        {"project_index": 0, "specialist_index": 0, "brief": BRIEF},
        lists=("list_projects", "list_agents"),
    )
    assert "call list_specialists in this turn first" in _result(script)["error"]
    assert ask_store.read_proposals(cid) == []


def test_list_specialists_is_a_draft_read():
    from agent_sessions import ask_orchestrator

    assert "list_specialists" in ask_orchestrator.DRAFT_READ_TOOLS
    assert ask_orchestrator.TOOLS["list_specialists"].kind == "read"


# ---- the tap -------------------------------------------------------------------------------------


@pytest.mark.parametrize("model, sent", [("default", None), ("claude-opus-5-5", "claude-opus-5-5")])
def test_the_tap_dispatches_the_pinned_version_with_its_model(live, ai, pool, model, sent):
    pool["specialists"][0] = _spec(model=model)
    cid, _turn, _ = _ask(live, ai, {"project_index": 0, "specialist_index": 0, "brief": BRIEF})
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    assert live.settled(cid)["state"] == "started"
    assert pool["extra"] == [{"specialist": ("researcher", 3), "model": sent}]


@pytest.mark.parametrize(
    "change, why",
    [
        (lambda p: p["specialists"].__setitem__(0, _spec(version=4)), "edited since"),
        (lambda p: p["specialists"].__setitem__(0, _spec(engine="codex")), "edited since"),
        (lambda p: p["specialists"].__setitem__(0, _spec(archived=True)), "archived"),
        (lambda p: p["specialists"].clear(), "no longer exists"),
        (
            lambda p: p.__setitem__("moved", "research-with-citations"),
            "(research-with-citations) changed in the Library since this was proposed",
        ),
    ],
    ids=["new-version", "other-agent", "archived", "removed", "skill-moved"],
)
def test_a_specialist_changed_before_the_tap_starts_nothing(live, ai, pool, change, why):
    cid, _turn, _ = _ask(live, ai, {"project_index": 0, "specialist_index": 0, "brief": BRIEF})
    change(pool)
    live.decide(live.card(cid)["action_id"])
    card = live.settled(cid)
    assert card["state"] == "stale" and why in card["reason"]
    assert pool["extra"] == []


def test_the_store_refuses_a_malformed_specialist_pin():
    entry = {
        "id": "a" * 32,
        "turn_id": str(uuid.uuid4()),
        "kind": "session",
        "state": "pending",
        "engine": "claude",
        "project_id": "p1",
        "cwd": "/home/x/api",
        "real": "/home/x/api",
        "bypass_default": False,
        "brief": BRIEF,
    }
    for bad in ({"id": "Researcher", "version": 1}, {"id": "r", "version": True}, {"id": "r"}):
        with pytest.raises(ValueError, match="malformed specialist"):
            ask_store._admit_entry({**entry, "specialist": bad}, [], fresh=False)
    assert ask_store._admit_entry(
        {**entry, "specialist": {"id": "researcher", "version": 3}}, [], fresh=False
    )["specialist"] == {"id": "researcher", "version": 3}


def test_a_specialist_with_NO_skills_starts_from_the_card(live, ai, pool):
    """Hermes on #1912: an empty skill list is a valid specialist, not a moved skill. 4 of the
    5 starters suggest none; such a card must start, never read out of date."""
    pool["specialists"][0] = _spec()
    pool["specialists"][0]["current"]["skills"] = []
    cid, _turn, _ = _ask(live, ai, {"project_index": 0, "specialist_index": 0, "brief": BRIEF})
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "started", card.get("reason")
    assert pool["extra"] == [{"specialist": ("researcher", 3), "model": None}]


def test_a_library_that_cannot_be_read_at_the_tap_does_not_spend_the_card(live, ai, pool):
    """ "Busy, try again" is not "moved": the precheck flags nothing, as the Specialists page
    does, and the launch's own bundle build stays the last word."""
    cid, _turn, _ = _ask(live, ai, {"project_index": 0, "specialist_index": 0, "brief": BRIEF})
    pool["library_down"] = True
    live.decide(live.card(cid)["action_id"])
    assert live.settled(cid)["state"] == "started"
