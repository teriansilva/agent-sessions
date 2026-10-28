"""#1681 P3 — an operation that needs a folder on screen can take it by voice, and the model never
sees or names a folder:

* `project.remove_folder` / `project.default_folder` pick from the project's own folders: the call
  reads the card's OWN choices on the server (`ask_operations.folder_choices`), says what the
  operation does and each choice verbatim, and a number settles the card's decide route with that
  folder as its input — the payload a tap on the picker sends;
* `project.create` / `project.add_folder` need a folder browsed for: the call SUGGESTS one on the
  server from the project's name (the operator's decision, option A) — the configured project root
  plus the name as one folder name, only if that folder EXISTS — and settles the decide route with
  it and `existing`, so the store re-checks it is still a real directory under its own lock.

Pinned: offered only with readable choices or a readable suggestion; a number names one of the
choices, anything else voids; a pick is offered alone after the turn's other cards; nothing is
decided with a folder made anywhere else; through the real app, the folder lands on the project.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import ask_operations, projects, voice_calls
from test_ask_operations import _card, _project, _propose_project, _settled, live  # noqa: F401
from test_ask_orchestrator import ai  # noqa: F401 — fixture
from test_voice_confirm import Rig, run
from test_voice_library_drafts import _operator

FOLDERS = ["/home/op/code/web", "/home/op/code/web-docs"]


def _view(op: str = "project.default_folder", needs=("folder",), **extra) -> dict:
    card = {"kind": "operation", "action_id": "op-1", "title": "web",
            "label": "Default launch folder", "verb": "Set default folder", "op": op,
            "needs": list(needs), "text": "New sessions start in the folder you pick."}  # fmt: skip
    return {"proposals": [card, *extra.get("more", [])]}


PICK_LINE = (
    "Default launch folder, for web: New sessions start in the folder you pick. Which folder? The"
    " options are: one, /home/op/code/web; two, /home/op/code/web-docs."
)


def test_a_pick_is_offered_with_the_cards_own_choices():
    (card,) = voice_calls.confirmable_cards(_view(), picks={"op-1": FOLDERS})
    assert card["kind"] == "pick" and card["line"] == PICK_LINE
    assert [o["label"] for o in card["options"]] == FOLDERS


@pytest.mark.parametrize(
    "view, picks",
    [
        (_view(), {}),  # no choices read: on screen
        (_view(), {"op-1": [f"/home/op/p{i}" for i in range(6)]}),  # too many to read out
        (_view(), {"op-1": ["/home/op/my_repo", "/home/op/web"]}),  # one can't be read as is
        # a name carrying the list's own punctuation could read as more options than there are
        (_view(), {"op-1": ["/home/op/web; two, /home/op/important", "/home/op/other"]}),
        (_view(), {"op-1": ["/home/op/a, b", "/home/op/c"]}),
        (_view(op="project.create"), {"op-1": FOLDERS}),  # not a catalog pick (hand-built)
        (_view(op="playbook.create"), {"op-1": FOLDERS}),  # a Library op: never by voice
        (_view(needs=("folder", "name")), {"op-1": FOLDERS}),  # another input it can't take
        # what the operation does can't be read in full: the pick stays on screen (Hermes 7047)
        (
            {"proposals": [{**_view()["proposals"][0], "text": "Sets `default_folder`."}]},
            {"op-1": FOLDERS},
        ),
    ],
)
def test_no_pick_without_readable_choices_of_its_own(view, picks):
    cards = voice_calls.confirmable_cards(view, picks=picks)
    assert not [c for c in cards if c["kind"] == "pick"]


def _rig(monkeypatch, view: dict) -> Rig:
    monkeypatch.setattr(
        ask_operations, "folder_choices", lambda pid: FOLDERS if pid == "op-1" else []
    )
    return Rig(monkeypatch, view)


def test_a_folder_is_picked_by_number_and_settled_through_decide(monkeypatch):
    r = _rig(monkeypatch, _view())

    async def go():
        await r.ask("Make web-docs the default folder for web")
        assert r.said_text()[-1] == f"{PICK_LINE} Say the folder's number, or cancel."
        await r.readback_played()
        await r.ask("The second one.")

    run(go())
    body = {"decision": "confirm", "inputs": {"folder": "/home/op/code/web-docs"}}
    assert ("POST", "/api/ask/proposals/op-1/decide", body) in r.routes


@pytest.mark.parametrize("said", ["Confirm.", "The third one.", "web docs", "Yes."])
def test_anything_but_one_of_its_numbers_decides_nothing(said, monkeypatch):
    r = _rig(monkeypatch, _view())

    async def go():
        await r.ask("Make web-docs the default folder for web")
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert not any(p.endswith("/decide") for _m, p, _b in r.routes)


NUDGE = {"kind": "nudge", "action_id": "act-9", "title": "api", "text": "Rerun the tests."}
NUDGE2 = {"kind": "nudge", "action_id": "act-8", "title": "docs", "text": "Rebuild the site."}


def test_a_pick_is_offered_alone_after_the_turns_other_cards(monkeypatch):
    """The other cards are offered together, as always ("confirm two" still works); the pick
    follows on its own, answered by its own numbers (#1692 review)."""
    r = _rig(monkeypatch, _view(more=[NUDGE, NUDGE2]))

    async def go():
        await r.ask("Set the default folder and nudge api and docs")
        assert [c["id"] for c in r.call.challenge["cards"]] == ["act-9", "act-8"]
        await r.readback_played()
        await r.ask("Confirm two.")
        assert [c["id"] for c in r.call.challenge["cards"]] == ["op-1"]
        await r.readback_played()
        await r.ask("The first one.")

    run(go())
    posts = [(p, b) for m, p, b in r.routes if m == "POST"]
    assert posts == [
        ("/api/pulse/actions/act-8/approve", None),
        ("/api/ask/proposals/op-1/decide",
         {"decision": "confirm", "inputs": {"folder": "/home/op/code/web"}}),
    ]  # fmt: skip


def test_a_cancel_with_cards_still_queued_says_they_all_stay(monkeypatch):
    r = _rig(monkeypatch, _view(more=[NUDGE]))

    async def go():
        await r.ask("Set the default folder and nudge api")
        await r.readback_played()
        await r.ask("Cancel.")

    run(go())
    assert r.said_text()[-1] == "Okay. They stay on your screen."
    assert r.call.challenge is None


def test_on_a_call_in_another_language_every_card_is_read_for_the_screen(monkeypatch):
    r = _rig(monkeypatch, _view(more=[NUDGE]))
    r.call.language = "de"

    async def go():
        await r.ask("Setz den Standardordner und schick api eine Nachricht")

    run(go())
    said = r.said_text()[-1]
    assert said.startswith(PICK_LINE) and "A message to api: Rerun the tests." in said
    assert said.endswith("Confirm it on your screen.")
    assert r.call.challenge is None


def test_a_pick_without_a_choice_never_settles(monkeypatch):
    r = _rig(monkeypatch, _view())
    (card,) = voice_calls.confirmable_cards(_view(), picks={"op-1": FOLDERS})
    run(r.call.settle(card))
    assert r.routes == []


# --- through the real app ----------------------------------------------------------------------


def test_the_picked_folder_becomes_the_default_through_the_real_route(live, ai):  # noqa: F811
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/web", "/srv/web-docs"])
    cid, _ = _propose_project(live, ai, "default_folder")
    turn = live.get(cid)["turns"][-1]
    picks = asyncio.run(voice_calls.folder_picks(turn))
    assert list(picks.values()) == [["/srv/web", "/srv/web-docs"]]
    (card,) = voice_calls.confirmable_cards(turn, picks=picks)
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def quiet(m):
        return None

    c._to_worker = c._to_client = quiet
    asyncio.run(c.settle({**card, "choice": card["options"][1]}))
    assert _settled(live, cid)["state"] == "done"
    assert projects.load()[pid].default_folder == "/srv/web-docs"
    # a decided card offers nothing more to pick
    assert ask_operations.folder_choices(card["id"]) == []


def test_a_folder_that_must_be_browsed_for_is_never_offered(live, ai):  # noqa: F811
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/web", "/srv/web-docs"])
    cid, _ = _propose_project(live, ai, "add_folder")
    (card,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    assert card["op"] == "project.add_folder" and card["needs"] == ["folder"]
    assert ask_operations.folder_choices(card["action_id"]) == []  # not the project's own list
    assert asyncio.run(voice_calls.folder_picks(live.get(cid)["turns"][-1])) == {}


@pytest.mark.parametrize(
    "action, effect",
    [
        ("remove_folder", "Releases the folder you choose below from web; its sessions go back to"
                          " folder grouping."),
        ("default_folder", "New sessions in web start in the folder you choose below."),
    ],
)  # fmt: skip
def test_the_real_cards_effect_is_read_before_the_pick(live, ai, action, effect):  # noqa: F811
    """What the operation DOES, in the card's own words, is in the read-back that arms it."""
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/web", "/srv/web-docs"])
    cid, _ = _propose_project(live, ai, action)
    turn = live.get(cid)["turns"][-1]
    (card,) = voice_calls.confirmable_cards(turn, picks=asyncio.run(voice_calls.folder_picks(turn)))
    label = turn["proposals"][0]["label"]
    assert card["line"] == (
        f"{label}, for web: {effect} Which folder? The options are: one, /srv/web; two,"
        " /srv/web-docs."
    )


# --- a browsed-for folder, suggested from the project's name (#1681, the operator's decision) ---


@pytest.mark.parametrize(
    "name, slug",
    [("web", "web"), ("Web App", "web-app"), ("  docs_site  ", "docs-site"),
     ("v2.0 api", "v2.0-api"), ("--", ""), ("../etc", "etc"), ("über", "ber"), (None, "")],
)  # fmt: skip
def test_a_project_name_becomes_one_folder_name(name, slug):
    assert ask_operations.folder_slug(name) == slug


@pytest.fixture
def root(tmp_path):
    """A project root whose path reads aloud as written: pytest's own temp dir carries the test's
    name, underscores and all, which no read-back can say verbatim. Removed afterwards."""
    import shutil
    from pathlib import Path

    base = Path(f"/tmp/voiceroot-{tmp_path.name.replace('_', '-')}")
    (base / "code").mkdir(parents=True, exist_ok=True)
    try:
        yield base / "code"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _create(live, ai, name: str = "Web App") -> str:  # noqa: F811
    from test_ask_operations import _propose_create

    return _propose_create(live, ai, name)


def test_a_new_project_adopts_the_existing_folder_suggested_from_its_name(live, ai, root):  # noqa: F811
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web-app").mkdir()  # an existing folder: voice never makes one (option A)
    cid = _create(live, ai)
    turn = live.get(cid)["turns"][-1]
    suggestions = asyncio.run(voice_calls.folder_suggestions(turn))
    (s,) = suggestions.values()
    assert s == {"root": str(root), "name": "web-app", "folder": str(root / "web-app")}
    (card,) = voice_calls.confirmable_cards(turn, suggestions=suggestions)
    p = turn["proposals"][0]
    assert card["kind"] == "suggest" and card["line"] == (
        "Create project: Creates the project “Web App” in the folder you choose below. I suggest"
        f" the existing folder {root / 'web-app'}."
    )
    assert p["title"] == ""  # a new project's name is in its effect, not a title
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def quiet(m):
        return None

    c._to_worker = c._to_client = quiet
    asyncio.run(c.settle(card))
    assert _settled(live, cid)["state"] == "done"
    (made,) = [pr for pr in projects.load().values() if pr.name == "Web App"]
    assert list(made.folders) == [str(root / "web-app")] and (root / "web-app").is_dir()
    assert ask_operations.suggested_folder(card["id"]) is None  # a decided card: nothing more


def test_no_root_configured_suggests_nothing(live, ai):  # noqa: F811
    from agent_sessions import prefs

    prefs.set_project_roots([])
    monkey_env = __import__("os").environ.pop("AGENT_SESSIONS_PROJECT_ROOTS", None)
    try:
        cid = _create(live, ai)
        assert asyncio.run(voice_calls.folder_suggestions(live.get(cid)["turns"][-1])) == {}
    finally:
        if monkey_env is not None:
            __import__("os").environ["AGENT_SESSIONS_PROJECT_ROOTS"] = monkey_env


def test_adding_a_folder_suggests_one_the_project_does_not_have(live, ai, root):  # noqa: F811
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web").mkdir()
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/elsewhere"])
    cid, _ = _propose_project(live, ai, "add_folder")
    (card,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    assert ask_operations.suggested_folder(card["action_id"])["folder"] == str(root / "web")
    projects.update(pid, folders=[str(root / "web")])  # it has that one already
    assert ask_operations.suggested_folder(card["action_id"]) is None
    cid, _ = _propose_project(live, ai, "default_folder")  # picks from its own list instead
    (card,) = [p for p in live.get(cid)["turns"][-1]["proposals"]]
    assert ask_operations.suggested_folder(card["action_id"]) is None


def test_a_suggested_folder_that_could_read_as_a_list_stays_on_screen():
    p = {**_view(op="project.create")["proposals"][0], "title": "", "label": "Create project"}
    ok = {"root": "/home/op/code", "name": "web", "folder": "/home/op/code/web"}
    assert voice_calls._suggest_card(p, ok)["line"].endswith(
        "I suggest the existing folder /home/op/code/web."
    )
    bad = {**ok, "folder": "/home/op/a, two, b/web"}
    assert voice_calls._suggest_card(p, bad) is None


def _decide(a, pid: str, inputs: dict):
    body = {"decision": "confirm", "inputs": inputs}
    return a.c.post(f"/api/ask/proposals/{pid}/decide", json=body, headers=a.hdr)


def test_a_folder_that_is_not_there_is_never_suggested(live, ai, root):  # noqa: F811
    """Option A: a new folder is never made by voice — it is picked on screen."""
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    cid = _create(live, ai)
    pid = live.get(cid)["turns"][-1]["proposals"][0]["action_id"]
    assert ask_operations.suggested_folder(pid) is None
    (root / "web-app").mkdir()
    assert ask_operations.suggested_folder(pid)["folder"] == str(root / "web-app")


def test_no_card_takes_a_folder_creation_input(live, ai, root):  # noqa: F811
    """Nothing on the decide route makes a folder: an extra input is refused as ever."""
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    cid = _create(live, ai)
    pid = live.get(cid)["turns"][-1]["proposals"][0]["action_id"]
    r = _decide(live, pid, {"folder": str(root / "web-app"), "create_folder": True})
    assert r.status_code == 422 and "no such input" in r.text
    assert not (root / "web-app").exists()


SUGGESTED = {"root": "/home/op/code", "name": "web", "folder": "/home/op/code/web"}


def _suggest_rig(monkeypatch, seq):
    view = _view(op="project.create")
    view["proposals"][0].update(
        label="Create project", title="", text="Creates the project web in the folder you choose."
    )
    r = Rig(monkeypatch, view)
    answers = iter(seq)
    monkeypatch.setattr(ask_operations, "suggested_folder", lambda pid: next(answers))
    return r


def test_the_existing_folder_is_the_decisions_input(monkeypatch):
    r = _suggest_rig(monkeypatch, [SUGGESTED, SUGGESTED])

    async def go():
        await r.ask("Make a project called web")
        assert r.said_text()[-1].endswith(
            "I suggest the existing folder /home/op/code/web. Say confirm to use it, or cancel."
        )
        await r.readback_played()
        await r.ask("Confirm.")

    run(go())
    posts = [(p, b) for m, p, b in r.routes if m == "POST"]
    body = {"decision": "confirm", "inputs": {"folder": "/home/op/code/web", "existing": True}}
    assert posts == [("/api/ask/proposals/op-1/decide", body)]


@pytest.mark.parametrize("now", [None, {**SUGGESTED, "folder": "/home/op/code/other"}],
                         ids=["gone", "other-folder"])  # fmt: skip
def test_a_suggestion_that_changed_since_it_was_read_does_nothing(now, monkeypatch):
    r = _suggest_rig(monkeypatch, [SUGGESTED, now])

    async def go():
        await r.ask("Make a project called web")
        await r.readback_played()
        await r.ask("Confirm.")

    run(go())
    assert not any(m == "POST" for m, _p, _b in r.routes)
    assert r.said_text()[-1] == "That card changed, so I did nothing. It's on your screen."


def test_a_folder_another_project_owns_or_an_excluded_one_is_never_suggested(live, ai, root):  # noqa: F811
    """The store refuses a folder another project owns, exactly, nested or above (#361), so it
    is never suggested; nor one under an excluded prefix, nor a symlink."""
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web-app").mkdir()
    cid = _create(live, ai)
    pid = live.get(cid)["turns"][-1]["proposals"][0]["action_id"]
    assert ask_operations.suggested_folder(pid) is not None
    other = _project(live, "other")
    for owned in (str(root / "web-app"), str(root), str(root / "web-app" / "sub")):
        projects.update(other, folders=[owned])
        assert ask_operations.suggested_folder(pid) is None, owned
    projects.update(other, folders=["/srv/unrelated"])
    prefs.set_folder_exclusions([str(root / "web-app")])
    assert ask_operations.suggested_folder(pid) is None
    prefs.set_folder_exclusions([])
    (root / "web-app").rmdir()
    (root / "elsewhere").mkdir()
    (root / "web-app").symlink_to(root / "elsewhere")
    assert ask_operations.suggested_folder(pid) is None


def test_a_card_dismissed_between_the_check_and_the_decide_changes_nothing(
    live,  # noqa: F811
    ai,  # noqa: F811
    root,
    monkeypatch,
):
    """The card is dismissed AFTER the confirm's re-check passed: the decide route refuses it."""
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web-app").mkdir()
    cid = _create(live, ai)
    turn = live.get(cid)["turns"][-1]
    (card,) = voice_calls.confirmable_cards(
        turn, suggestions=asyncio.run(voice_calls.folder_suggestions(turn))
    )
    real = ask_operations.suggested_folder

    def checked_then_dismissed(pid):
        got = real(pid)
        r = live.c.post(f"/api/ask/proposals/{pid}/decide", json={"decision": "dismiss"},
                        headers=live.hdr)  # fmt: skip
        assert r.status_code in (200, 202), r.text
        return got

    monkeypatch.setattr(ask_operations, "suggested_folder", checked_then_dismissed)
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def quiet(m):
        return None

    c._to_worker = c._to_client = quiet
    asyncio.run(c.settle(card))
    assert not [p for p in projects.load().values() if p.name == "Web App"]


def _settle_real(live, cid: str, card: dict) -> None:  # noqa: F811
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def quiet(m):
        return None

    c._to_worker = c._to_client = quiet
    asyncio.run(c.settle(card))


def _after_the_recheck(monkeypatch, change) -> None:
    """The folder changes AFTER the call's own re-check and before the store saves (Hermes 7110)."""
    real = ask_operations.suggested_folder

    def checked(pid):
        out = real(pid)
        change()
        return out

    monkeypatch.setattr(ask_operations, "suggested_folder", checked)


def _gone(root, name: str, how: str):
    """Remove ``root/name``, or replace it with a symlink to another directory."""
    import os
    import shutil

    def change():
        shutil.rmtree(root / name)
        if how == "link":
            (root / "elsewhere").mkdir()
            os.symlink(root / "elsewhere", root / name)

    return change


@pytest.mark.parametrize("how", ["removed", "link"])
def test_a_suggested_folder_gone_before_the_save_is_never_the_new_projects(
    live,  # noqa: F811
    ai,  # noqa: F811
    root,
    monkeypatch,
    how,
):
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web-app").mkdir()
    cid = _create(live, ai)
    turn = live.get(cid)["turns"][-1]
    (card,) = voice_calls.confirmable_cards(
        turn, suggestions=asyncio.run(voice_calls.folder_suggestions(turn))
    )
    _after_the_recheck(monkeypatch, _gone(root, "web-app", how))
    _settle_real(live, cid, card)
    assert _settled(live, cid)["state"] == "failed"
    assert not [pr for pr in projects.load().values() if pr.name == "Web App"]


@pytest.mark.parametrize("how", ["removed", "link"])
def test_a_suggested_folder_gone_before_the_save_is_never_added(
    live,  # noqa: F811
    ai,  # noqa: F811
    root,
    monkeypatch,
    how,
):
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web").mkdir()
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/elsewhere"])
    cid, _ = _propose_project(live, ai, "add_folder")
    turn = live.get(cid)["turns"][-1]
    (card,) = voice_calls.confirmable_cards(
        turn, suggestions=asyncio.run(voice_calls.folder_suggestions(turn))
    )
    assert card["kind"] == "suggest"
    _after_the_recheck(monkeypatch, _gone(root, "web", how))
    _settle_real(live, cid, card)
    assert _settled(live, cid)["state"] == "failed"
    assert list(projects.load()[pid].folders) == ["/srv/elsewhere"]


@pytest.mark.parametrize("existing", [False, None, "yes", 1])
def test_existing_is_only_ever_true(live, ai, root, existing):  # noqa: F811
    from agent_sessions import prefs

    prefs.set_project_roots([str(root)])
    (root / "web-app").mkdir()
    cid = _create(live, ai)
    pid = live.get(cid)["turns"][-1]["proposals"][0]["action_id"]
    r = _decide(live, pid, {"folder": str(root / "web-app"), "existing": existing})
    assert r.status_code == 422
    assert not [pr for pr in projects.load().values() if pr.name == "Web App"]


@pytest.mark.parametrize("action", ["default_folder", "remove_folder"])
def test_existing_only_rides_where_it_is_acted_on(live, ai, action):  # noqa: F811
    """Review of #1692: picking from the project's own folders takes no `existing` — it would be
    accepted and silently ignored there."""
    pid = _project(live, "web")
    projects.update(pid, folders=["/srv/a", "/srv/b"])
    cid, _ = _propose_project(live, ai, action)
    (card,) = [p for p in live.get(cid)["turns"][-1]["proposals"]]
    r = _decide(live, card["action_id"], {"folder": "/srv/a", "existing": True})
    assert r.status_code == 422 and "no such input" in r.text


def test_existing_never_rides_without_a_folder(live, ai):  # noqa: F811
    _project(live, "web")
    cid, _ = _propose_project(live, ai, "archive")
    (card,) = [p for p in live.get(cid)["turns"][-1]["proposals"]]
    r = _decide(live, card["action_id"], {"existing": True})
    assert r.status_code == 422 and "no such input" in r.text


def test_a_screen_pick_is_saved_without_the_existence_check(live, ai, root):  # noqa: F811
    """Unchanged on screen: the picker lists symlinked folders, and a tap without `existing` is
    saved as before."""
    import os

    (root / "real").mkdir()
    os.symlink(root / "real", root / "web-app")
    cid = _create(live, ai)
    pid = live.get(cid)["turns"][-1]["proposals"][0]["action_id"]
    assert _decide(live, pid, {"folder": str(root / "web-app")}).status_code == 202
    assert _settled(live, cid)["state"] == "done"
