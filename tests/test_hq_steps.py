"""HQ's step lists (#2251): a multi-part request sorted into ordered steps, one card at a time.

Pinned here:
- derivation: a step's state comes from its cards (a replacement takes its slot and never holds;
  a batch is done when every card succeeded, held with counts otherwise; a step needing a skipped
  step is held); display numbers come from the order, never stored;
- identity: edit / add / move / skip keep every sid, need and card; a forward dependency, a cycle
  or a move past a need is refused; a stale revision is refused with the list as it stands;
- only work that has not begun moves: a running card refuses move / skip / end, a pending one is
  withdrawn through its own decide route;
- the fence: with a list open only the current step gets a card; with none, a second KIND of card
  in one turn is refused, while a same-kind batch is not;
- THE REPLAY (the operator's conversation that motivated #2251): project → session → playbook.
  The session card is staged only after the project card is done, from fresh reads, so it
  targets the NEW project's folder;
- exactly once: an owed advance or hold is begun by one turn whose user record carries its key,
  under the turn lock — after a duplicate settlement, a crash before anything was written, two
  kicks at once; a Commander message takes it up; a state HQ caused itself asks nothing;
- Stop (#2050): a card confirmed before a later Stop continues nothing on its own;
- End steps: dismisses the waiting card, refuses while one runs;
- a card that replaced another carries what changed (the Changed line).
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import time

import httpx
import pytest

from agent_sessions import (
    ask_orchestrator,
    ask_proposals,
    ask_steps,
    ask_store,
    mission_targets,
    projects,
    review,
)
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    _login,
    ai,
    answer,
    app,
    call,
    calls,
)
from test_ask_sessions import Live, world  # noqa: F401 — fixtures are used by name

# ---- the fake endpoint ---------------------------------------------------------------------------


class Model:
    """The i-th request gets ``replies[i]`` — a dict, or a callable of the request body."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        reply = self.replies[min(len(self.bodies) - 1, len(self.replies) - 1)]
        got = reply(body) if callable(reply) else reply
        if inspect.isawaitable(got):
            got = await got
        return httpx.Response(
            200, json={"choices": [{"message": dict(got), "finish_reason": "stop"}]}
        )

    def questions(self) -> list[str]:
        """Each request's own question (its last user message)."""
        out = []
        for b in self.bodies:
            m = [x for x in b["messages"] if x["role"] == "user"][-1]["content"]
            out.append(m if isinstance(m, str) else json.dumps(m))
        return out


def _model(monkeypatch, *replies) -> Model:
    m = Model(*replies)
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(m.handler))
    return m


def _last_tool(body: dict) -> dict:
    return json.loads([m for m in body["messages"] if m["role"] == "tool"][-1]["content"])


def _tool_with(body: dict, key: str) -> dict:
    """The newest tool result carrying ``key``."""
    for m in reversed([m for m in body["messages"] if m["role"] == "tool"]):
        got = json.loads(m["content"])
        if key in got:
            return got
    raise AssertionError(f"no tool result with {key!r}")


def _index_of(body: dict, name: str) -> int:
    return next(p["index"] for p in _tool_with(body, "projects")["projects"] if p["name"] == name)


def _sid(body: dict, n: int) -> str:
    """The sid of step ``n`` (1-based) from the last tool result that carried the steps."""
    for m in reversed([m for m in body["messages"] if m["role"] == "tool"]):
        got = json.loads(m["content"])
        if isinstance(got.get("steps"), dict):
            return got["steps"]["steps"][n - 1]["sid"]
    raise AssertionError("no step list in the tool results")


def _current_sid(body: dict) -> str:
    text = [x for x in body["messages"] if x["role"] == "user"][-1]["content"]
    text = text if isinstance(text, str) else json.dumps(text)
    i = text.index("{")
    steps, _ = json.JSONDecoder().raw_decode(text[i:])
    return steps["current_step"]


# ---- fixtures ------------------------------------------------------------------------------------


@pytest.fixture
def hq(world, ai, auth_cfg, monkeypatch):
    """The live app, with the project list read from the REAL project store: a project created
    by a card is a startable project for the next step, as in the running app."""
    home = world["proj"].parent

    def options():
        rows = []
        for e in projects.load().values():
            folder = e.default_folder or (list(e.folders)[0] if e.folders else "")
            if folder and not e.archived:
                rows.append({"id": e.id, "name": e.name, "cwd": folder})
        return rows

    def resolve(pid, *, index=None):
        for p in options():
            if p["id"] == pid:
                return dict(p), ""
        return None, "the chosen project no longer exists"

    monkeypatch.setattr(mission_targets, "project_options", options)
    monkeypatch.setattr(mission_targets, "resolve_chosen_project", resolve)
    ai()  # the endpoint's config; each test installs its own fake model
    a = Live(auth_cfg)
    a.home = home
    try:
        yield a
    finally:
        a.close()


def _wait(a, cid, pred, timeout=20.0):
    end = time.monotonic() + timeout
    while True:
        conv = a.get(cid)
        if pred(conv) or time.monotonic() > end:
            return conv
        time.sleep(0.05)


def _settled(conv) -> bool:
    cards = [p for t in conv["turns"] for p in t["proposals"]]
    return all(t["status"] not in ("pending", "queued") for t in conv["turns"]) and all(
        c["state"] != "deciding" for c in cards
    )


def _cards(conv) -> dict[str, dict]:
    return {p["action_id"]: p for t in conv["turns"] for p in t["proposals"]}


STEPS = {
    "title": "Set up Rentals",
    "steps": [
        {"title": "Projekt „Rentals“ anlegen"},
        {"title": "Forgejo-Repo anlegen und auschecken", "needs": [0]},
        {"title": "Playbook „Rentals“", "needs": [0]},
    ],
}


def _first_turn():
    """Sort first, then ONLY step 1's card."""
    return (
        call("propose_steps", STEPS, cid="ps"),
        lambda b: call(
            "propose_project_change",
            {"action": "create", "name": "Rentals", "for_step": _sid(b, 1)},
            cid="pp",
        ),
        answer("Drei Schritte; vor dir liegt Schritt 1."),
    )


def _confirm_project(a, cid, folder) -> dict:
    conv = a.get(cid)
    (card,) = [p for p in _cards(conv).values() if p.get("op") == "project.create"]
    r = a.c.post(
        f"/api/ask/proposals/{card['action_id']}/decide",
        json={"decision": "confirm", "inputs": {"folder": str(folder)}},
        headers=a.hdr,
    )
    assert r.status_code == 202, r.text
    return card


# ---- THE REPLAY ---------------------------------------------------------------------------------


def test_the_replay_one_card_at_a_time_and_the_session_targets_the_new_project(
    hq, world, monkeypatch
):
    folder = hq.home / "rentals"
    folder.mkdir()
    model = _model(
        monkeypatch,
        *_first_turn(),
        # The advance continuation: fresh reads, then ONLY step 2's card.
        calls(("list_projects", {}), ("list_agents", {})),
        lambda b: call(
            "propose_session",
            {
                "project_index": _index_of(b, "Rentals"),
                "engine_index": 0,
                "brief": "Lege das Repo Rentals an und checke es hier aus.",
                "for_step": _current_sid(b),
            },
            cid="s",
        ),
        answer("Schritt 1 ist erledigt; jetzt die Session."),
    )
    cid = hq.new()
    r = hq.ask(cid, "Projekt Rentals, ein Forgejo-Repo darin, dann ein Playbook")
    assert r.status_code == 200, r.text
    conv = hq.get(cid)
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["now", "next", "next"]
    assert [s["n"] for s in lst["steps"]] == [1, 2, 3]
    assert lst["drawn_at_turn"] == conv["turns"][0]["turn_id"]
    # ONE card on screen: step 1's.
    cards = _cards(conv)
    assert len(cards) == 1 and lst["steps"][0]["cards"] == list(cards)
    assert lst["owed"] is False

    _confirm_project(hq, cid, folder)
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    first, cont = conv["turns"]
    assert cont["via"] == "card", "the server's continuation, never the Commander's words"
    assert "Projekt „Rentals“ anlegen" in cont["question"]
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["done", "now", "next"]
    (session,) = cont["proposals"]
    assert session["kind"] == "session"
    # THE BUG THIS FIXES: the session card targets the project step 1 just created.
    (made,) = [p for p in projects.load().values() if p.name == "Rentals"]
    row = ask_proposals._row(cid, session["action_id"])
    assert row["project_id"] == made.id and row["cwd"] == str(folder)
    assert lst["steps"][1]["cards"] == [session["action_id"]]
    assert lst["drawn_at_turn"] == cont["turn_id"], "drawn once, under the newest turn"
    assert lst["created_turn"] == first["turn_id"], "where it was: the quiet line"
    # Exactly once: the key is on the continuation's own record, and nothing more is owed.
    records = ask_store.read(cid)
    keys = [r["steps_key"] for r in records if r.get("type") == "user" and "steps_key" in r]
    assert len(keys) == 1 and keys[0].startswith("next:")
    assert lst["owed"] is False
    assert len(model.bodies) == 6, "three requests a turn, two turns: nothing ran twice"


def test_a_started_session_step_says_started_never_done_work(hq, world, monkeypatch):
    folder = hq.home / "rentals"
    folder.mkdir()
    model = _model(
        monkeypatch,
        *_first_turn(),
        calls(("list_projects", {}), ("list_agents", {})),
        lambda b: call(
            "propose_session",
            {
                "project_index": _index_of(b, "Rentals"),
                "engine_index": 0,
                "brief": "Set up the repo.",
                "for_step": _current_sid(b),
            },  # fmt: skip
            cid="s",
        ),
        answer("Now the session."),
        answer("Step 3 next."),
    )
    cid = hq.new()
    assert hq.ask(cid, "set it all up").status_code == 200
    _confirm_project(hq, cid, folder)
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    (session,) = conv["turns"][1]["proposals"]
    r = hq.decide(session["action_id"], {"decision": "start", "bypass": False})
    assert r.status_code == 202, r.text
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 3 and _settled(c))
    (lst,) = conv["step_lists"]
    assert [s["state"] for s in lst["steps"]] == ["done", "done", "now"]
    assert lst["steps"][1]["started"] is True
    note = conv["turns"][2]["question"]
    assert "STARTED" in note and "its work is not tracked here" in note
    assert len(model.bodies) == 7


# ---- the fence ----------------------------------------------------------------------------------


def test_two_kinds_of_card_in_one_turn_without_steps_the_second_is_refused(hq, monkeypatch):
    model = _model(
        monkeypatch,
        call("propose_project_change", {"action": "create", "name": "X"}, cid="a"),
        call("propose_library", {"group": "playbook", "action": "create"}, cid="b"),
        answer("ok"),
    )
    cid = hq.new()
    assert hq.ask(cid, "make a project and a playbook").status_code == 200
    got = json.loads([m for m in model.bodies[2]["messages"] if m["role"] == "tool"][-1]["content"])
    assert got["error"].startswith("refused: this turn already proposed a different kind")
    assert "propose_steps" in got["error"]
    assert len(_cards(hq.get(cid))) == 1


def test_same_kind_cards_in_one_turn_are_a_batch_and_need_no_steps(hq, monkeypatch):
    _model(
        monkeypatch,
        call("propose_project_change", {"action": "create", "name": "A"}, cid="a"),
        call("propose_project_change", {"action": "create", "name": "B"}, cid="b"),
        answer("two"),
    )
    cid = hq.new()
    assert hq.ask(cid, "two projects").status_code == 200
    assert len(_cards(hq.get(cid))) == 2


def test_with_a_list_open_only_the_current_step_gets_a_card(hq, monkeypatch):
    model = _model(
        monkeypatch,
        call("propose_steps", STEPS, cid="ps"),
        lambda b: call(
            "propose_project_change",
            {"action": "create", "name": "Rentals", "for_step": _sid(b, 2)},
            cid="wrong",
        ),
        lambda b: call(
            "propose_project_change", {"action": "create", "name": "Rentals"}, cid="none"
        ),
        lambda b: call(
            "propose_project_change",
            {"action": "create", "name": "Rentals", "for_step": _sid(b, 1)},
            cid="right",
        ),
        answer("ok"),
    )
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    errs = [_last_tool(model.bodies[i]) for i in (2, 3)]
    for e in errs:
        assert "only that step can have a card now" in e["error"].lower(), e
    ok = _last_tool(model.bodies[4])
    assert ok["proposed"] is True and ok["step"] == _sid(model.bodies[4], 1)
    conv = hq.get(cid)
    assert len(_cards(conv)) == 1
    assert conv["step_lists"][0]["steps"][0]["cards"] == list(_cards(conv))


def test_a_second_open_list_is_refused_with_the_list_as_it_stands(hq, monkeypatch):
    model = _model(
        monkeypatch,
        call("propose_steps", STEPS, cid="a"),
        call("propose_steps", STEPS, cid="b"),
        answer("ok"),
    )
    cid = hq.new()
    assert hq.ask(cid, "x").status_code == 200
    got = _last_tool(model.bodies[2])
    assert "still open" in got["error"] and got["steps"]["status"] == "active"
    assert len(hq.get(cid)["step_lists"]) == 1


# ---- holds, exactly once, Stop ------------------------------------------------------------------


def test_a_tap_dismiss_holds_the_step_and_hq_asks_once(hq, monkeypatch):
    model = _model(
        monkeypatch, *_first_turn(), answer("Ändern, überspringen oder aufhören?"), answer("x")
    )
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    (card,) = _cards(hq.get(cid)).values()
    r = hq.decide(card["action_id"], {"decision": "dismiss"})
    assert r.status_code == 200
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    (lst,) = conv["step_lists"]
    assert lst["status"] == "held" and lst["steps"][0]["state"] == "held"
    hold = conv["turns"][1]
    assert hold["via"] == "card" and "on hold" in hold["question"]
    assert hold["proposals"] == []
    # Exactly once: another kick finds the key taken up.
    ok = asyncio.run(ask_orchestrator.kick_steps(cid, operator=None, app=None))
    assert ok is False
    assert len(hq.get(cid)["turns"]) == 2
    assert len(model.bodies) == 4


def test_hqs_own_withdrawal_asks_nothing_afterwards(hq, monkeypatch):
    model = _model(
        monkeypatch,
        *_first_turn(),
        calls(("list_cards", {})),
        lambda b: call("withdraw_card", {"card": _last_tool(b)["cards"][0]["card"]}, cid="w"),
        answer("Weg damit."),
        answer("SHOULD NOT BE ASKED"),
    )
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    assert hq.ask(cid, "lass das Projekt weg").status_code == 200
    time.sleep(1.0)  # any continuation would have begun by now
    conv = _wait(hq, cid, _settled)
    assert len(conv["turns"]) == 2, "the state HQ caused itself is taken up by its own turn"
    (lst,) = conv["step_lists"]
    assert lst["steps"][0]["state"] == "held" and lst["owed"] is False
    assert len(model.bodies) == 6


def test_a_crash_before_anything_was_written_is_reconstructed_once(hq, monkeypatch):
    """Card settled, server gone before any continuation: the next kick derives the same owed
    key and begins it exactly once; a second kick begins nothing."""
    folder = hq.home / "rentals"
    folder.mkdir()
    model = _model(monkeypatch, *_first_turn(), answer("weiter"), answer("NEVER"))
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    (card,) = _cards(hq.get(cid)).values()
    # As if the run settled and the process died before the tap's follow-up ran.
    ask_proposals._update(cid, card["action_id"], state="done", settled_at=time.time())
    assert hq.get(cid)["step_lists"][0]["owed"] is True
    client_app = hq.app

    async def twice():
        op = object()
        a, b = await asyncio.gather(
            ask_orchestrator.kick_steps(cid, operator=op, app=client_app),
            ask_orchestrator.kick_steps(cid, operator=op, app=client_app),
        )
        return a, b

    started = hq.c.portal.call(twice)
    assert sorted(started) == [False, True]
    conv = _wait(hq, cid, lambda c: len(c["turns"]) == 2 and _settled(c))
    assert conv["turns"][1]["via"] == "card"
    assert conv["step_lists"][0]["owed"] is False
    assert len(model.bodies) == 4


def test_a_key_already_taken_up_is_never_begun_again():
    cid = ask_store.create()
    key = "next:" + "a" * 16 + ":s" + "b" * 8 + ":" + "c" * 16
    ask_store.append(
        cid,
        {
            "type": "user",
            "turn_id": "11111111-1111-1111-1111-111111111111",
            "text": "x",
            "qd": "0" * 64,
            "steps_key": key,
        },  # fmt: skip
    )
    with pytest.raises(ask_orchestrator.TurnError) as e:
        asyncio.run(ask_orchestrator._steps_key(cid, ask_store.read(cid), key, "card"))
    assert e.value.status == 409


def test_a_commander_message_takes_up_what_is_owed(hq, monkeypatch):
    model = _model(monkeypatch, *_first_turn(), answer("Projekt steht; jetzt Schritt 2?"))
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    (card,) = _cards(hq.get(cid)).values()
    ask_proposals._update(cid, card["action_id"], state="done", settled_at=time.time())
    assert hq.ask(cid, "wie geht es weiter?").status_code == 200
    records = ask_store.read(cid)
    mine = [r for r in records if r.get("type") == "user"][-1]
    assert mine.get("steps_key", "").startswith("next:")
    assert "via" not in mine
    # The Commander's turn saw the steps as data.
    assert '"current_step"' in model.questions()[-1]
    conv = _wait(hq, cid, _settled)
    assert len(conv["turns"]) == 2 and conv["step_lists"][0]["owed"] is False


def test_a_card_confirmed_before_a_later_stop_continues_nothing():
    rec = [{"type": "status", "status": "stopped", "ts": 200.0, "turn_id": "x"}]
    assert ask_orchestrator._stopped_after(rec, 100.0) is True
    assert ask_orchestrator._stopped_after(rec, 300.0) is False
    assert ask_orchestrator._stopped_after(rec, None) is False


# ---- End steps ----------------------------------------------------------------------------------


def test_end_steps_dismisses_the_waiting_card_and_ends_the_list(hq, monkeypatch):
    _model(monkeypatch, *_first_turn())
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    conv = hq.get(cid)
    lst = conv["step_lists"][0]
    r = hq.c.post(
        f"/api/ask/conversations/{cid}/steps/end", json={"list_id": lst["id"]}, headers=hq.hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["steps"]["status"] == "ended"
    conv = hq.get(cid)
    (card,) = _cards(conv).values()
    assert card["state"] == "dismissed" and not card.get("replaced_by")
    assert [s["state"] for s in conv["step_lists"][0]["steps"]] == ["ended"] * 3
    assert projects.load() == {} or all(p.name != "Rentals" for p in projects.load().values())


def test_end_steps_needs_csrf_and_a_list_id(hq, monkeypatch):
    _model(monkeypatch, *_first_turn())
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    lid = hq.get(cid)["step_lists"][0]["id"]
    r = hq.c.post(f"/api/ask/conversations/{cid}/steps/end", json={"list_id": lid})
    assert r.status_code == 403
    r = hq.c.post(f"/api/ask/conversations/{cid}/steps/end", json={}, headers=hq.hdr)
    assert r.status_code == 422


def test_end_steps_refuses_while_a_card_runs(hq, monkeypatch):
    _model(monkeypatch, *_first_turn())
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    conv = hq.get(cid)
    (card,) = _cards(conv).values()
    lid = conv["step_lists"][0]["id"]
    # A launch running: its decide lock held and its card `deciding`.
    fd = ask_store.try_decide_lock(cid)
    try:
        ask_proposals._update(cid, card["action_id"], state="deciding")
        r = hq.c.post(
            f"/api/ask/conversations/{cid}/steps/end", json={"list_id": lid}, headers=hq.hdr
        )
        assert r.status_code == 409 and "never cancelled" in r.json()["detail"]
        assert ask_store.read_steps(cid)[0]["ended"] is False
    finally:
        os.close(fd)


# ---- the Changed line ---------------------------------------------------------------------------


def test_a_replacement_carries_what_changed_and_the_old_card_is_hidden_data(hq, monkeypatch):
    model = _model(
        monkeypatch,
        *_first_turn(),
        calls(("list_cards", {})),
        lambda b: call(
            "propose_project_change",
            {
                "action": "create",
                "name": "Housing Rentals",
                "replaces": _last_tool(b)["cards"][0]["card"],
            },  # fmt: skip
            cid="r",
        ),
        answer("Umbenannt."),
    )
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    assert hq.ask(cid, "nenne es Housing Rentals").status_code == 200
    conv = hq.get(cid)
    cards = _cards(conv)
    (new,) = [c for c in cards.values() if c.get("replaces")]
    assert new["changes"] == [{"field": "name", "from": "Rentals", "to": "Housing Rentals"}]
    # The replacement holds the step's slot; the old card is never a hold.
    (lst,) = conv["step_lists"]
    assert lst["steps"][0]["cards"] == [new["action_id"]] and lst["status"] == "active"
    # Drawn once, under the newest turn that changed it: the replacement's (G6).
    assert lst["drawn_at_turn"] == conv["turns"][1]["turn_id"]
    assert lst["created_turn"] == conv["turns"][0]["turn_id"]
    assert len(model.bodies) == 6


# ---- derivation and identity (pure) -------------------------------------------------------------


def _row(pid, state, **kw):
    return {"id": pid, "state": state, **kw}


def _list(*steps, ended=False):
    return {"id": "f" * 16, "revision": 1, "title": "t", "ended": ended,
            "drawn_at_turn": "x", "steps": list(steps)}  # fmt: skip


def _st(sid, cards=(), needs=(), skipped=False):
    return {"sid": sid, "title": sid, "cards": list(cards), "needs": list(needs),
            "skipped": skipped}  # fmt: skip


A, B, C = "a" * 32, "b" * 32, "c" * 32


def test_a_replacement_takes_its_slot_and_never_holds():
    rows = {A: _row(A, "dismissed", replaced_by=B), B: _row(B, "pending", replaces=A)}
    v = ask_steps.derive(_list(_st("s00000001", [A]), _st("s00000002")), rows)
    assert v["steps"][0]["state"] == "now" and v["steps"][0]["cards"] == [B]
    rows[B]["state"] = "done"
    v = ask_steps.derive(_list(_st("s00000001", [A]), _st("s00000002")), rows)
    assert v["steps"][0]["state"] == "done" and v["current"] == "s00000002"


def test_a_batch_is_done_only_when_every_card_succeeded():
    rows = {A: _row(A, "done"), B: _row(B, "failed", outcome="no"), C: _row(C, "done")}
    v = ask_steps.derive(_list(_st("s00000001", [A, B, C])), rows)
    s = v["steps"][0]
    assert s["state"] == "held" and s["outcome"] == "2 of 3 done; 1 did not"
    assert v["status"] == "held"


def test_a_step_needing_a_skipped_step_is_held():
    v = ask_steps.derive(
        _list(_st("s00000001", skipped=True), _st("s00000002", needs=["s00000001"])), {}
    )
    assert v["steps"][1]["state"] == "held" and v["steps"][1]["reason"] == "needs_skipped"
    assert ask_steps.owed(v)[1] == "hold"


def test_owed_keys():
    rows = {A: _row(A, "done"), B: _row(B, "pending")}
    lst = _list(_st("s00000001", [A]), _st("s00000002"))
    got = ask_steps.owed(ask_steps.derive(lst, rows))
    assert got is not None and got[1] == "next" and ask_store.STEPS_KEY_RE.match(got[0])
    lst["steps"][1]["cards"] = [B]
    assert ask_steps.owed(ask_steps.derive(lst, rows)) is None, "a card in flight owes nothing"
    rows[B]["state"] = "done"
    got = ask_steps.owed(ask_steps.derive(lst, rows))
    assert got[1] == "done"
    first = ask_steps.owed(ask_steps.derive(_list(_st("s00000001"), _st("s00000002")), {}))
    assert first is None, "a fresh list's first card is the turn's own business"
    assert ask_steps.owed(ask_steps.derive(_list(_st("s00000001", [A]), ended=True), rows)) is None


def _stored(cid, lst):
    with ask_store.proposals_lock(cid):
        ask_store.write_steps(cid, [lst])


def _turn():
    return "22222222-2222-2222-2222-222222222222"


def _stored_list(cid):
    lst = {
        "id": "e" * 16,
        "revision": 3,
        "title": "t",
        "ended": False,
        "drawn_at_turn": _turn(),
        "created_turn": _turn(),
        "steps": [_st("s00000001"), _st("s00000002", needs=["s00000001"]), _st("s00000003")],
    }
    _stored(cid, lst)  # fmt: skip
    return lst


def test_identity_survives_move_add_skip_and_numbers_re_derive():
    cid = ask_store.create()
    _stored_list(cid)
    new, pend = ask_steps.plan_revision(
        cid, "e" * 16, 3,
        [{"op": "move", "sid": "s00000003", "after": ""},
         {"op": "add", "title": "new", "after": "s00000001"},
         {"op": "skip", "sid": "s00000003"}],
    )  # fmt: skip
    assert pend == []
    view = ask_steps.apply_revision(cid, new, 3, _turn())
    sids = [s["sid"] for s in view["steps"]]
    assert sids[0] == "s00000003" and sids[1] == "s00000001" and sids[3] == "s00000002"
    assert [s["n"] for s in view["steps"]] == [1, 2, 3, 4]
    assert view["steps"][3]["needs"] == ["s00000001"] and view["revision"] == 4
    assert view["steps"][0]["state"] == "skipped"


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "move", "sid": "s00000002", "after": ""}],  # past what it needs
        [{"op": "edit", "sid": "s00000001", "needs": ["s00000002"]}],  # forward / cycle
        [{"op": "add", "title": "x", "after": "", "needs": ["s00000003"]}],
    ],
)
def test_a_forward_dependency_or_a_move_past_a_need_is_refused(ops):
    cid = ask_store.create()
    _stored_list(cid)
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.plan_revision(cid, "e" * 16, 3, ops)
    assert e.value.code == "needs"


def test_a_stale_revision_is_refused_with_the_list_as_it_stands():
    cid = ask_store.create()
    _stored_list(cid)
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.plan_revision(cid, "e" * 16, 2, [{"op": "end"}])
    assert e.value.code == "stale" and e.value.lst["revision"] == 3


def test_only_work_that_has_not_begun_moves():
    cid = ask_store.create()
    lst = _stored_list(cid)
    lst["steps"][0]["cards"] = [A]
    _stored(cid, lst)
    with ask_store.proposals_lock(cid):
        ask_store.write_proposals(cid, [])  # no sidecar rows: A reads "gone"
    # A running card refuses skip, move and end; nothing is written.
    rows = [{"id": A, "turn_id": _turn(), "kind": "operation", "state": "deciding"}]
    import agent_sessions.ask_steps as st

    orig = st._rows
    st._rows = lambda _cid: {A: rows[0]}
    try:
        for ops in ([{"op": "skip", "sid": "s00000001"}], [{"op": "end"}]):
            with pytest.raises(ask_steps.StepsRefused) as e:
                ask_steps.plan_revision(cid, "e" * 16, 3, ops)
            assert e.value.code == "busy" and "never cancelled" in e.value.text
        rows[0]["state"] = "pending"
        _new, pend = ask_steps.plan_revision(cid, "e" * 16, 3, [{"op": "end"}])
        assert pend == [A]
    finally:
        st._rows = orig
    assert ask_store.read_steps(cid)[0]["revision"] == 3


def test_the_store_refuses_a_malformed_list_or_key():
    cid = ask_store.create()
    bad = copy.deepcopy(_stored_list(cid))
    bad["steps"][0]["needs"] = ["s00000002"]
    with pytest.raises(ValueError), ask_store.proposals_lock(cid):
        ask_store.write_steps(cid, [bad])
    with pytest.raises(ValueError):
        ask_store.append(
            cid,
            {
                "type": "user",
                "turn_id": _turn(),
                "text": "x",
                "qd": "0" * 64,
                "steps_key": "next:../../etc",
            },  # fmt: skip
        )


def test_retention_removes_the_steps_with_the_conversation():
    cid = ask_store.create()
    _stored_list(cid)
    ask_store.delete(cid)
    assert not (ask_store.root() / "steps" / f"{cid}.json").exists()


# ---- Hermes on #2251: displacement by the resulting order, End vs. a staged card ---------------


@pytest.fixture
def rows(monkeypatch):
    """The proposal sidecar as `ask_steps` reads it, by id (a dict the test edits)."""
    table: dict[str, dict] = {}
    monkeypatch.setattr(ask_steps, "_rows", lambda _cid: table)
    return table


def _current_holds(cid, rows, state):
    lst = _stored_list(cid)
    lst["steps"][0]["cards"] = [A]  # s00000001 is current, with A
    _stored(cid, lst)
    rows[A] = {"id": A, "turn_id": _turn(), "kind": "operation", "state": state}


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "move", "sid": "s00000003", "after": ""}],  # a cardless step moved ahead
        [{"op": "add", "title": "first", "after": ""}],  # a step added ahead
    ],
    ids=["move-ahead", "add-ahead"],
)
def test_a_step_put_ahead_of_the_current_one_withdraws_its_pending_card(rows, ops):
    cid = ask_store.create()
    _current_holds(cid, rows, "pending")
    new, pend = ask_steps.plan_revision(cid, "e" * 16, 3, ops)
    assert pend == [A], "the displaced card is withdrawn, never left live but hidden"
    a = next(st for st in new["steps"] if st["sid"] == "s00000001")
    assert a["cards"] == [] and a["retired"] == [A]
    rows[A]["state"] = "dismissed"  # as the withdrawal leaves it
    view = ask_steps.apply_revision(cid, new, 3, _turn())
    assert view["steps"][0]["state"] == "now" and view["steps"][0]["cards"] == []
    assert next(x for x in view["steps"] if x["sid"] == "s00000001")["state"] == "next"


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "move", "sid": "s00000003", "after": ""}],
        [{"op": "add", "title": "first", "after": ""}],
        [{"op": "skip", "sid": "s00000001"}],
        [{"op": "end"}],
    ],
    ids=["move-ahead", "add-ahead", "skip", "end"],
)
def test_a_running_current_card_refuses_anything_that_takes_it_off_screen(rows, ops):
    cid = ask_store.create()
    _current_holds(cid, rows, "deciding")
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.plan_revision(cid, "e" * 16, 3, ops)
    assert e.value.code == "busy" and "never cancelled" in e.value.text


def test_a_step_moved_behind_the_current_one_withdraws_nothing(rows):
    cid = ask_store.create()
    _current_holds(cid, rows, "pending")
    _new, pend = ask_steps.plan_revision(
        cid, "e" * 16, 3, [{"op": "move", "sid": "s00000003", "after": "s00000001"}]
    )
    assert pend == []


def test_an_attach_between_plan_and_write_makes_end_stale(rows):
    """End plans with no card on the current step; HQ attaches one; End's write must fail."""
    cid = ask_store.create()
    _stored_list(cid)
    new, pend = ask_steps.end_plan(cid, "e" * 16)
    assert pend == []
    rows[A] = {"id": A, "turn_id": _turn(), "kind": "operation", "state": "pending"}
    assert ask_steps.attach(cid, "e" * 16, "s00000001", A, "propose_project_change") == 4
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.apply_revision(cid, new, new["revision"])
    assert e.value.code == "stale"
    stored = ask_store.read_steps(cid)[0]
    assert stored["ended"] is False and stored["steps"][0]["cards"] == [A]


def test_staging_waits_for_the_steps_fence_and_end_waits_for_staging(hq, monkeypatch):
    """The fence spans check → stage → attach and End's plan → dismiss → write: while one holds
    it, the other is refused as busy, so neither can write over the other."""
    monkeypatch.setattr(ask_steps, "FENCE_WAIT_S", 0.3)
    model = _model(
        monkeypatch,
        call("propose_steps", STEPS, cid="ps"),
        answer("Laid out."),
        lambda b: call(
            "propose_project_change",
            {"action": "create", "name": "Rentals", "for_step": _current_sid(b)},
            cid="pp",
        ),
        answer("Card?"),
    )
    cid = hq.new()
    assert hq.ask(cid, "set it up").status_code == 200
    lid = hq.get(cid)["step_lists"][0]["id"]
    fd = ask_store.try_steps_lock(cid)  # another transition holds the fence
    try:
        assert hq.ask(cid, "now the project card").status_code == 200
        got = _last_tool(model.bodies[3])
        assert got["error"].startswith("refused: the steps are being changed right now")
        assert _cards(hq.get(cid)) == {}
        r = hq.c.post(
            f"/api/ask/conversations/{cid}/steps/end", json={"list_id": lid}, headers=hq.hdr
        )
        assert r.status_code == 409 and "being changed" in r.json()["detail"]
    finally:
        os.close(fd)
    assert ask_store.read_steps(cid)[0]["ended"] is False


def _pending(conv) -> list[str]:
    return [pid for pid, c in _cards(conv).items() if c["state"] == "pending"]


def test_a_refused_second_kind_sorts_into_steps_and_leaves_no_card_outside_them(hq, monkeypatch):
    """Hermes on #2251: refusal → sorting → staging must never leave the first card waiting outside
    the wizard. A list refuses while a card waits outside it; adopting that card puts it in the
    first step; End steps then leaves nothing pending."""
    model = _model(
        monkeypatch,
        call("propose_project_change", {"action": "create", "name": "Rentals"}, cid="a"),
        call("propose_library", {"group": "playbook", "action": "create"}, cid="b"),
        call("propose_steps", STEPS, cid="c"),  # without adopt: refused, the card is named
        lambda b: call(
            "propose_steps",
            {**STEPS, "adopt": [_tool_with(b, "card")["card"]]},
            cid="d",
        ),
        answer("Sorted; step 1 is the project you have."),
    )
    cid = hq.new()
    assert hq.ask(cid, "make a project and a playbook").status_code == 200
    assert "propose_steps with adopt" in _last_tool(model.bodies[2])["error"]
    refused = _last_tool(model.bodies[3])
    assert "still wait outside any step" in refused["error"]
    adopted = _last_tool(model.bodies[4])
    assert "do not propose it again" in adopted["note"]
    conv = hq.get(cid)
    (card,) = _pending(conv)
    (lst,) = conv["step_lists"]
    assert lst["steps"][0]["cards"] == [card] and lst["steps"][0]["state"] == "now"
    assert len(_cards(conv)) == 1, "one card, in the first step, and no other"
    r = hq.c.post(
        f"/api/ask/conversations/{cid}/steps/end", json={"list_id": lst["id"]}, headers=hq.hdr
    )
    assert r.status_code == 200, r.text
    assert _pending(hq.get(cid)) == [], "End steps leaves no orphan waiting"


def test_a_list_refuses_a_card_left_waiting_outside_it_until_it_is_withdrawn(rows):
    cid = ask_store.create()
    rows[A] = {"id": A, "turn_id": _turn(), "kind": "operation", "op": "project.create",
               "state": "pending"}  # fmt: skip
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.create(cid, _turn(), "t", [{"title": "a"}, {"title": "b"}])
    assert e.value.code == "loose_cards" and A in e.value.text
    with pytest.raises(ask_steps.StepsRefused) as e:
        ask_steps.create(cid, _turn(), "t", [{"title": "a"}, {"title": "b"}], adopt=[B])
    assert e.value.code == "adopt"
    rows[A]["state"] = "dismissed"  # withdrawn
    view = ask_steps.create(cid, _turn(), "t", [{"title": "a"}, {"title": "b"}])
    assert view["steps"][0]["cards"] == []
