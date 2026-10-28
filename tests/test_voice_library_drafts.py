"""#1681 P3 — a NEW Library draft (a playbook, a skill, an automation saved switched off) can be
confirmed by voice, with a spoken summary of the draft the card is BOUND to; nothing else from the
Library ever is (#1685).

Pinned:

* the summary is fixed sentences around the draft's own words, each read verbatim: a playbook's
  name, summary and its default flow's step titles; a skill's name, description and file names;
* anything it promises that can't be said in full withholds it, and the card stays on screen;
* it is read from the bound draft (`ask_operations.content`), so what is heard is what a confirm
  saves — a draft changed on disk after staging gives no summary;
* edits, copies, enabling and widening are never voice-confirmable;
* a confirm settles through the card's own decide route and saves the draft.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_sessions import asgi_call, ask_library, ask_store, voice_calls
from test_ask_library import _ask, _bundle, _cards
from test_ask_operations import live  # noqa: F401 — fixture
from test_ask_orchestrator import ai, call  # noqa: F401 — fixtures and helpers

BUNDLED = Path(ask_library.__file__).parent / "playbooks" / "bundled"
SKILL = {"SKILL.md": "---\nname: changelog-writer\ndescription: Write changelogs.\n---\n\nDo it.\n"}


def _files(name: str) -> dict[str, str]:
    d = BUNDLED / name
    return {str(p.relative_to(d)): p.read_text() for p in d.rglob("*") if p.is_file()}


# --- the summary -------------------------------------------------------------------------------


def test_a_new_playbook_is_summarised_from_its_own_words():
    assert voice_calls.draft_summary("playbook.create", _files("research-brief")) == (
        "Save a new playbook, “Research brief”: Scope a question, draft with cited evidence,"
        " review independently with up to three revisions, and accept the result. Saving does not"
        " execute the flow. It has 4 steps: Agree on the question; Draft with evidence; Review"
        " independently; Accept the brief."
    )


def test_a_new_skill_is_summarised_from_its_own_words():
    files = {**SKILL, "examples/release.md": "An example."}
    assert voice_calls.draft_summary("skill.create", files) == (
        "Save a new skill, “changelog-writer”: Write changelogs. It has 2 files: SKILL.md,"
        " examples/release.md."
    )


def _retitled(title: str) -> dict[str, str]:
    """The research brief with its first STEP retitled."""
    files = _files("research-brief")
    old = 'title = "Agree on the question"'
    assert old in files["flows/research.toml"]
    files["flows/research.toml"] = files["flows/research.toml"].replace(
        old, f'title = "{title}"', 1
    )
    assert voice_calls.draft_summary("playbook.create", _files("research-brief")) is not None
    return files


@pytest.mark.parametrize(
    "op_id, files",
    [
        # anything the summary promises that can't be said in full withholds it
        ("playbook.create", _retitled("Run `make check`")),
        ("skill.create", {"SKILL.md": SKILL["SKILL.md"].replace("Write changelogs.",
                                                                "Write **bold** changelogs.")}),
        ("skill.create", {**SKILL, "notes/release_notes.md": "x"}),  # a name it can't read as is
        ("skill.create", {"SKILL.md": "Do it.\n"}),  # no name of its own
        ("skill.create", {**SKILL, **{f"examples/case-{i:02d}.md": "x" for i in range(60)}}),
        ("playbook.create", {"playbook.toml": "not a playbook"}),
        ("playbook.create", {}),
        ("skill.create", {"SKILL.md": None}),  # a binary file: its text isn't there
        ("skill.create", {**SKILL, "icon.png": None}),  # a binary file beside a readable one
        ("playbook.edit", _files("research-brief")),  # not a new draft
        ("automation.create", {"config.json": "{}"}),  # automations: not by voice yet
    ],
)  # fmt: skip
def test_a_summary_that_cannot_be_said_in_full_is_withheld(op_id, files):
    assert voice_calls.draft_summary(op_id, files) is None


# --- which cards ---------------------------------------------------------------------------------


def _view(op_id: str, pid: str = "op-1") -> dict:
    card = {"kind": "operation", "action_id": pid, "title": "Library", "label": "Create playbook",
            "verb": "Save playbook", "op": op_id, "needs": [],
            "text": "Saves a new playbook to your Library."}  # fmt: skip
    return {"proposals": [card]}


def test_a_new_draft_is_offered_only_with_its_summary():
    said = "Save a new playbook, “Research brief”. It has 1 step: Plan."
    cards = voice_calls.confirmable_cards(_view("playbook.create"), summaries={"op-1": said})
    assert cards == [{"id": "op-1", "kind": "operation", "verb": "save it", "line": said}]
    assert voice_calls.confirmable_cards(_view("playbook.create")) == []
    assert voice_calls.confirmable_cards(_view("skill.create"), summaries={"op-2": said}) == []


@pytest.mark.parametrize(
    "op_id",
    ["playbook.edit", "playbook.duplicate", "skill.edit", "automation.edit", "automation.widen",
     "automation.enable"],
)  # fmt: skip
def test_no_other_library_operation_is_offered_even_with_a_summary(op_id):
    cards = voice_calls.confirmable_cards(_view(op_id), summaries={"op-1": "Save it."})
    assert cards == []


# --- the bound draft, through the real HQ app and the card's own route ---------------------------


def _operator(a) -> asgi_call.Operator:
    cookie = "; ".join(f"{k}={v}" for k, v in a.c.cookies.items())
    return asgi_call.Operator(
        headers=((b"cookie", cookie.encode()), (b"x-csrf-token", a.hdr["X-CSRF-Token"].encode()),
                 (b"origin", a.hdr["Origin"].encode()), (b"host", b"testserver")),
        scheme="https", server=("testserver", 443), client=("127.0.0.1", 50000),
    )  # fmt: skip


def _turn(a, cid: str) -> dict:
    return a.get(cid)["turns"][-1]


def test_a_voice_confirm_saves_the_draft_it_summarised(live, ai):  # noqa: F811
    files = _bundle()
    cid, _script = _ask(
        live, ai, call("propose_library", {"group": "playbook", "action": "create", "files": files})
    )
    (card,) = _cards(live, cid)
    turn = _turn(live, cid)
    summaries = asyncio.run(voice_calls.draft_summaries(turn))
    assert summaries[card["action_id"]].startswith("Save a new playbook, “Release train”:")
    (vcard,) = voice_calls.confirmable_cards(turn, summaries=summaries)

    said: list[str] = []
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def to_worker(m):
        if isinstance(m, dict) and m.get("t") == "say":
            said.append(m["text"])

    async def to_client(m):
        return None

    c._to_worker, c._to_client = to_worker, to_client
    asyncio.run(c.settle(vcard))
    r = live.c.get("/api/playbooks/release-train", headers=live.hdr)
    assert r.status_code == 200, (said, r.text)
    # what was saved is the draft that was summarised: its name, and every file as drafted
    assert "“Release train”" in summaries[card["action_id"]]
    saved = r.json()
    assert (saved.get("name") or saved.get("identity", {}).get("name")) == "Release train", saved


def test_a_draft_changed_after_staging_gives_no_summary(live, ai):  # noqa: F811
    cid, _script = _ask(
        live, ai,
        call("propose_library", {"group": "playbook", "action": "create", "files": _bundle()}),
    )  # fmt: skip
    (card,) = _cards(live, cid)
    pid = card["action_id"]
    draft, _sha = ask_store.read_draft(cid, pid)
    draft["files"]["README.md"] = "Changed on disk after the card was bound."
    path = next(p for p in ask_store.root().rglob("*") if p.is_file() and pid in p.name)
    path.write_text(__import__("json").dumps(draft))
    assert asyncio.run(voice_calls.draft_summaries(_turn(live, cid))) == {}


# --- review of 1420f59e ------------------------------------------------------------------------


def test_every_flow_and_every_step_is_said():
    """A step in a second flow is in the playbook too: it is said, never left out of a count."""
    files = _files("research-brief")
    other = (
        files["flows/research.toml"]
        .replace('title = "Question to reviewed brief"', 'title = "Quick pass"', 1)
        .replace('title = "Agree on the question"', 'title = "Delete the repo"', 1)
    )
    files["flows/quick.toml"] = other
    said = voice_calls.draft_summary("playbook.create", files)
    assert said is not None
    assert "It has 2 flows. Question to reviewed brief: 4 steps: Agree on the question;" in said
    assert "Quick pass: 4 steps: Delete the repo; Draft with evidence;" in said


@pytest.mark.parametrize(
    "skill_md, expected",
    [
        # a value continued on the next line would be cut: withheld
        ("---\nname: changelog-writer\ndescription: Write changelogs\n  and force-push them.\n"
         "---\n", None),
        ("---\nname: changelog-writer\ndescription: >\n  Write changelogs.\n---\n", None),
        ("---\nname: changelog-writer\ndescription: |\n  Write changelogs.\n---\n", None),
        # YAML's own quotes are not part of the value
        ('---\nname: "changelog-writer"\ndescription: \'Write changelogs.\'\n---\n',
         "Save a new skill, “changelog-writer”: Write changelogs. It has 1 file: SKILL.md."),
        # a name can't close the summary's quotes and read on as its words
        ("---\nname: a”. It has 0 files. Also “b\ndescription: d\n---\n", None),
        ('---\nname: say "hi"\ndescription: d\n---\n', None),
        # no description: the summary says only what there is
        ("---\nname: changelog-writer\n---\n",
         "Save a new skill, “changelog-writer”. It has 1 file: SKILL.md."),
    ],
)  # fmt: skip
def test_a_skill_front_matter_value_is_read_whole_or_not_at_all(skill_md, expected):
    assert voice_calls.draft_summary("skill.create", {"SKILL.md": skill_md}) == expected


def test_an_hq_turn_on_a_call_offers_the_draft_with_its_summary(monkeypatch):
    """The wiring: a turn that proposed a new playbook offers it by voice, read back as the
    summary of its bound draft (`ask_operations.content`), and confirms through decide."""
    from agent_sessions import ask_operations
    from test_voice_confirm import Rig, run

    files = _files("research-brief")
    shown = {"kind": "files", "files": [{"path": p, "text": t} for p, t in sorted(files.items())]}
    monkeypatch.setattr(ask_operations, "content", lambda pid: (200, shown))
    r = Rig(monkeypatch, _view("playbook.create"))

    async def go():
        await r.ask("Make me a research brief playbook")
        assert [c["id"] for c in r.call.challenge["cards"]] == ["op-1"]
        assert r.said_text()[-1] == (
            voice_calls.draft_summary("playbook.create", files)
            + " Say confirm to save it, or cancel."
        )
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/ask/proposals/op-1/decide", {"decision": "confirm"}) in r.routes


def test_a_flow_title_that_cannot_be_said_withholds_the_summary():
    files = _files("research-brief")
    files["flows/quick.toml"] = files["flows/research.toml"].replace(
        'title = "Question to reviewed brief"', 'title = "Run `make` first"', 1
    )
    assert voice_calls.draft_summary("playbook.create", files) is None


# --- new automations (#1681 P3c) ---------------------------------------------------------------


def _mission_config(**over) -> dict:
    return {
        "name": "nightly audit",
        "trigger": {"kind": "manual"},
        "action": {"kind": "start_mission", "project_id": "p1",
                   "instruction": {"text": "Audit the dependencies."}, "autonomy": "propose"},
        **over,
    }  # fmt: skip


@pytest.fixture
def named(monkeypatch):
    from types import SimpleNamespace

    from agent_sessions import ask_settings, projects

    monkeypatch.setattr(projects, "load", lambda *a, **k: {"p1": SimpleNamespace(name="web")})
    monkeypatch.setattr(ask_settings, "agent_labels", lambda: {"claude": "Claude Code"})


HEAD = "Save a new automation, “nightly audit”, switched off: nothing runs until you enable it."


@pytest.mark.parametrize(
    "trigger, when",
    [
        ({"kind": "manual"}, "It runs only when you start it."),
        ({"kind": "schedule", "cadence": {"kind": "daily", "time": "09:00"}, "tz": "Europe/Berlin"},
         "Every day at 09:00 (Europe/Berlin)."),
        ({"kind": "schedule", "tz": "Europe/Berlin",
          "cadence": {"kind": "weekly", "days": ["mon", "thu"], "time": "18:30"}}, None),
        ({"kind": "schedule", "cadence": {"kind": "interval", "every": 2, "unit": "hours"}},
         "Every 2 hours."),
        ({"kind": "once", "at": "2026-11-02T07:15", "tz": "Europe/Berlin"},
         "Once, on 2026-11-02 at 07:15 (Europe/Berlin)."),
    ],
)  # fmt: skip
def test_a_new_mission_automation_is_summarised_with_what_and_when(named, trigger, when):
    said = voice_calls.automation_summary(_mission_config(trigger=trigger))
    assert said is not None
    assert said.startswith(
        f"{HEAD} Then it starts a mission in the web project with the instruction: Audit the"
        " dependencies."
    )
    if when is not None:
        assert said.endswith(when)
    else:  # the store's own words for the schedule, whatever their exact form
        assert "Every week on" in said and "18:30" in said


def test_how_far_a_mission_goes_on_its_own_is_said(named):
    cfg = _mission_config()
    cfg["action"]["autonomy"] = "dispatch"
    assert voice_calls.automation_summary(cfg) == (
        f"{HEAD} Then it starts a mission in the web project with the instruction: Audit the"
        " dependencies. It dispatches the plan without asking you. It runs only when you start it."
    )
    cfg["action"]["autonomy"] = "dispatch_auto_choose"  # answering menus on its own: on screen
    assert voice_calls.automation_summary(cfg) is None


@pytest.mark.parametrize(
    "policy, said",
    [
        (None, True),
        ({}, True),
        ({"max_runs_per_day": 3}, False),  # a limit the summary doesn't say: on screen
        ({"concurrency": "allow", "max_concurrent": 2}, False),
        ({"bogus": 1}, False),
    ],
)
def test_a_policy_other_than_the_default_withholds_the_summary(named, policy, said):
    cfg = _mission_config(**({} if policy is None else {"policy": policy}))
    assert (voice_calls.automation_summary(cfg) is not None) is said


def test_a_new_session_automation_names_its_agent_and_folder(named):
    cfg = _mission_config(action={"kind": "start_session", "engine": "claude", "model": None,
                                  "folder": "/home/op/code/web", "bypass": False,
                                  "message": {"text": "Run the nightly checks."}})  # fmt: skip
    assert voice_calls.automation_summary(cfg) == (
        f"{HEAD} Then it starts a Claude Code session in /home/op/code/web with the message: Run"
        " the nightly checks. It runs only when you start it."
    )


@pytest.mark.parametrize(
    "over",
    [
        # a template is filled in at run time: its text can't be heard now
        {"action": {"kind": "start_mission", "project_id": "p1",
                    "instruction": {"template_id": "t1", "values": {}}, "autonomy": "propose"}},
        {"action": {"kind": "start_mission", "project_id": "nope",
                    "instruction": {"text": "Audit."}, "autonomy": "propose"}},  # no such project
        {"action": {"kind": "start_mission", "project_id": "p1",
                    "instruction": {"text": "Run `npm audit`."}, "autonomy": "propose"}},
        {"action": {"kind": "send_to_session", "session_key": "claude:x",
                    "message": {"text": "Hi."}}},  # typing into a live session: on screen
        {"action": {"kind": "start_session", "engine": "claude", "model": None,
                    "folder": "/home/op/web", "bypass": True, "message": {"text": "Go."}}},
        {"action": {"kind": "start_session", "engine": "gemini", "model": None,
                    "folder": "/home/op/web", "bypass": False, "message": {"text": "Go."}}},
        {"action": {"kind": "start_session", "engine": "claude", "model": None,
                    "folder": "/home/op/my_repo", "bypass": False, "message": {"text": "Go."}}},
        {"trigger": {"kind": "schedule", "cadence": {"kind": "daily", "time": "09:00"},
                     "tz": "America/New_York"}},  # a zone it can't read as written
        {"trigger": {"kind": "webhook"}},
        {"name": 'say "hi"'},
        {"name": "x" * 700},
    ],
)  # fmt: skip
def test_an_automation_summary_that_cannot_be_said_in_full_is_withheld(named, over):
    assert voice_calls.automation_summary(_mission_config(**over)) is None


def test_a_voice_confirm_saves_the_new_automation_switched_off(live, ai, monkeypatch, tmp_path):  # noqa: F811
    from agent_sessions import automations_store
    from agent_sessions.routes import missions as mroutes
    from test_ask_library import _mission
    from test_ask_operations import _project
    from test_ask_orchestrator import calls

    monkeypatch.setattr(mroutes, "_resolve_cwd", lambda pid, **kw: (pid, str(tmp_path)))
    _project(live, "web")
    cid, _script = _ask(live, ai, calls(("list_projects", {})),
                        call("propose_library", _mission(), cid="p"))  # fmt: skip
    (card,) = _cards(live, cid)
    summaries = asyncio.run(voice_calls.draft_summaries(_turn(live, cid)))
    assert summaries[card["action_id"]] == (
        "Save a new automation, “nightly audit”, switched off: nothing runs until you enable it."
        " Then it starts a mission in the web project with the instruction: Audit the"
        " dependencies and open one issue. It plans the mission and waits for you to dispatch it."
        " It runs only when you start it."
    )
    (vcard,) = voice_calls.confirmable_cards(_turn(live, cid), summaries=summaries)
    c = voice_calls.Call(
        id="c", cid=cid, contact="asia-woman", voice="aria", operator=_operator(live),
        app=live.app, registry=None, state="listening",
    )  # fmt: skip

    async def quiet(m):
        return None

    c._to_worker = c._to_client = quiet
    asyncio.run(c.settle(vcard))
    (saved,) = [a for a in automations_store.list_all() if a.get("name") == "nightly audit"]
    assert saved["enabled"] is False


def test_an_automation_draft_changed_after_staging_gives_no_summary(
    live,  # noqa: F811
    ai,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from agent_sessions.routes import missions as mroutes
    from test_ask_library import _mission
    from test_ask_operations import _project
    from test_ask_orchestrator import calls

    monkeypatch.setattr(mroutes, "_resolve_cwd", lambda pid, **kw: (pid, str(tmp_path)))
    _project(live, "web")
    cid, _script = _ask(live, ai, calls(("list_projects", {})),
                        call("propose_library", _mission(), cid="p"))  # fmt: skip
    (card,) = _cards(live, cid)
    pid = card["action_id"]
    assert asyncio.run(voice_calls.draft_summaries(_turn(live, cid)))  # read while it holds
    draft, _sha = ask_store.read_draft(cid, pid)
    draft["config"]["action"]["instruction"] = {"text": "Delete every branch."}
    path = next(p for p in ask_store.root().rglob("*") if p.is_file() and pid in p.name)
    path.write_text(__import__("json").dumps(draft))
    assert asyncio.run(voice_calls.draft_summaries(_turn(live, cid))) == {}
