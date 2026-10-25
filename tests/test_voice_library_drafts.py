"""#1681 P3 — a NEW Library draft (a playbook or a skill) can be confirmed by voice, with a spoken
summary of the draft the card is BOUND to; nothing else from the Library ever is (#1685).

Pinned:

* the summary is fixed sentences around the draft's own words, each read verbatim: a playbook's
  name, summary and its default flow's step titles; a skill's name, description and file names;
* anything it promises that can't be said in full withholds it, and the card stays on screen;
* it is read from the bound draft (`ask_operations.content`), so what is heard is what a confirm
  saves — a draft changed on disk after staging gives no summary;
* edits, copies and every automation operation are never voice-confirmable;
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
    ["playbook.edit", "playbook.duplicate", "skill.edit", "automation.create", "automation.edit",
     "automation.widen", "automation.enable"],
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
