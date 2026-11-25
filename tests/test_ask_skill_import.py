"""HQ imports a skill from a repository link (#2145), end to end through the real app.

A typed HQ turn reads a repository at one commit (`inspect_skill_repo`), stages the EXACT fetched
bytes as a `skill.import` card (`propose_library`), and the operator's confirm saves them through
the Library's own route with a display-only, unverified `repo_origin`. Pinned here:

* byte-for-byte: a SKILL.md over 8,000 characters (more than HQ ever reads of a file) is saved
  identical to the repository's bytes, so the lossy transcription path is really replaced;
* the link must be typed in THIS turn's message; a source from no inspect is refused; a call turn
  never fetches;
* a folder the Library can't store stages a REFUSED card that offers nothing and names why;
* the fetched repository is gone when the turn ends; Save never needs it (the draft is durable);
* the store writes `repo_origin` with the bytes in one rename: it survives a reload, a failed save
  leaves no skill, it is never a gallery origin, and a later upstream push changes nothing saved.

The repository is a bare one on disk standing in for the https link (`served`): mechanics only.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_sessions import ask_library, ask_orchestrator, gitwrite, skill_repo
from agent_sessions.connectors import hq as connector_hq
from agent_sessions.gallery import contract, repo_origin, skills
from agent_sessions.playbooks import store
from test_ask_operations import _confirm, _result, _settled, live  # noqa: F401
from test_ask_orchestrator import ai, answer, call  # noqa: F401
from test_skill_repo import SKILL, _git, make_repo

LINK = "https://github.com/acme/agent-skills"
QUESTION = f"Import the skill from {LINK} — the pdf-forms one"
LONG = SKILL + "".join(
    f"Step {i}: fill field {i} exactly as the form prints it.\n" for i in range(400)
)


@pytest.fixture
def served(tmp_path, monkeypatch):
    """`LINK` is served from a bare repository on disk. MECHANICS ONLY: the https check and the
    admission are replaced by the `file` transport, and the clone is pointed at the bare path."""
    srv = make_repo(
        tmp_path / "forge",
        {
            "skills/pdf-forms/SKILL.md": LONG,
            "skills/pdf-forms/scripts/fill.py": "#!/usr/bin/env python3\nprint('fill')\n",
            "skills/legacy/SKILL.md": SKILL,
            "skills/legacy/assets/form.pdf": b"%PDF\x00\x01binary",
            "README.md": "# acme skills\n",
        },
    )
    monkeypatch.setattr(skill_repo, "check_url", lambda u: u)
    monkeypatch.setattr(
        skill_repo, "_admit", lambda u: gitwrite.Admission([], allow_protocol="file")
    )
    real = skill_repo._fetch_into

    def into(work, exe, url, ref, admission, cancel):
        assert url == LINK
        snap = real(work, exe, f"file://{srv}", ref, admission, cancel)
        snap.url = url
        return snap

    monkeypatch.setattr(skill_repo, "_fetch_into", into)
    return srv


def _turn(a, ai, *steps, question: str = QUESTION):
    script = ai(*steps, answer("Read it; confirm the card."))
    cid = a.new()
    r = a.ask(cid, question)
    assert r.status_code == 200, r.text
    return cid, script


def _cards(a, cid):
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _inspect(url=LINK):
    return call("inspect_skill_repo", {"url": url}, cid="i")


def _import(path="skills/pdf-forms", new_id="pdf-forms", source="skillsrc:1"):
    return call(
        "propose_library",
        {"group": "skill", "action": "import", "source": source, "path": path, "new_id": new_id},
        cid="p",
    )


def test_hq_imports_the_exact_bytes_and_records_an_unverified_source(live, ai, served):
    cid, script = _turn(live, ai, _inspect(), _import())
    listed = _result(script, 1)
    assert listed["source"] == "skillsrc:1"
    assert (
        "untrusted content from the repository github.com/acme/agent-skills" in listed["provenance"]
    )
    rows = listed["untrusted_repository_content"]["skills"]
    assert [r["path"] for r in rows] == ["skills/legacy", "skills/pdf-forms"]
    commit = _git(served, "rev-parse", "HEAD").strip()
    assert listed["commit"] == commit[:12]

    (card,) = _cards(live, cid)
    assert card["op"] == "skill.import" and card["state"] == "pending" and card["can_confirm"]
    assert card["verb"] == "Save skill" and card["label"] == "Import skill"
    shown = live.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=live.hdr).json()
    assert shown["kind"] == "import" and shown["refused"] == []
    assert shown["source"] == f"github.com/acme/agent-skills @ {commit[:7]} · skills/pdf-forms/"
    assert {r["path"]: r["flag"] for r in shown["rows"]} == {
        "SKILL.md": "",
        "scripts/fill.py": "script",
    }
    # The repository is gone once the turn ended: Save must not need it.
    assert not any(skill_repo.root().iterdir())

    assert _confirm(live, card["action_id"]).status_code == 202
    done = _settled(live, cid)
    assert done["state"] == "done", done
    saved = live.c.get("/api/library/skills/pdf-forms", headers=live.hdr).json()
    # BYTE-FOR-BYTE: longer than anything HQ reads of a file, saved exactly as the repository
    # has it.
    assert len(LONG) > max(ask_library.FILE_CHARS_MAX, connector_hq.FILE_TEXT_MAX)
    want = _git(served, "cat-file", "blob", "HEAD:skills/pdf-forms/SKILL.md")
    assert saved["files"]["SKILL.md"].encode() == want.encode() == LONG.encode()
    assert saved["repo_origin"] == {
        "url": LINK,
        "commit": commit,
        "path": "skills/pdf-forms",
        "unverified": True,
    }
    assert saved["gallery_origin"] is None  # never the gallery's verified provenance


def test_a_later_upstream_push_changes_nothing_saved(live, ai, served):
    cid, _ = _turn(live, ai, _inspect(), _import())
    (card,) = _cards(live, cid)
    work = served.parent / "work"
    (work / "skills/pdf-forms/SKILL.md").write_text("---\nname: pdf-forms\n---\nhijacked\n")
    _git(work, "commit", "-qam", "later")
    _git(work, "push", "-q", str(served), "main")
    assert _confirm(live, card["action_id"]).status_code == 202
    assert _settled(live, cid)["state"] == "done"
    saved = live.c.get("/api/library/skills/pdf-forms", headers=live.hdr).json()
    assert saved["files"]["SKILL.md"] == LONG


def test_a_link_not_typed_in_this_message_is_never_fetched(live, ai, served, monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(skill_repo, "fetch", lambda *a, **k: fetched.append(a[0]))
    cid, script = _turn(live, ai, _inspect(), question="Import the pdf-forms skill from that repo")
    assert "typed" in json.dumps(_result(script, 1))
    cid, script = _turn(live, ai, _inspect(LINK + "/tree/main"), question=QUESTION)
    assert "typed" in json.dumps(_result(script, 1))
    assert fetched == [] and _cards(live, cid) == []


def test_a_source_no_inspect_minted_this_turn_is_refused(live, ai, served):
    cid, script = _turn(live, ai, _import())
    assert "inspect_skill_repo returned in this turn" in json.dumps(_result(script, 1))
    assert _cards(live, cid) == []


def test_a_folder_the_library_cant_store_stages_a_refused_card(live, ai, served):
    cid, script = _turn(live, ai, _inspect(), _import(path="skills/legacy", new_id="legacy"))
    told = _result(script, 2)
    assert told["proposed"] is False and told["refused"] == ["assets/form.pdf is binary"]
    (card,) = _cards(live, cid)
    assert card["state"] == "refused"
    assert card["can_confirm"] is False and card["can_dismiss"] is False
    assert card["summary"] == "legacy can't be saved: assets/form.pdf is binary. Nothing was saved."
    shown = live.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=live.hdr).json()
    assert shown["files"] == [] and shown["refused"] == ["assets/form.pdf is binary"]
    assert {r["path"]: r["flag"] for r in shown["rows"]}["assets/form.pdf"] == "binary"
    # A confirm of a settled card answers with its state and runs nothing.
    r = _confirm(live, card["action_id"])
    assert r.status_code in (200, 409, 422) and _settled(live, cid)["state"] == "refused"
    assert live.c.get("/api/library/skills/legacy", headers=live.hdr).status_code == 404


def test_a_call_turn_never_fetches(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(skill_repo, "fetch", lambda *a, **k: fetched.append(a[0]))
    turn = ask_orchestrator._Turn(
        cid="c", turn_id="t", question=QUESTION, working_keys=set(), progress=lambda e: None,
        via="call",
    )  # fmt: skip
    with pytest.raises(ask_orchestrator._ToolRefused) as e:
        asyncio.run(ask_orchestrator._inspect_skill_repo(turn, {"url": LINK}))
    assert e.value.code == "not_typed" and fetched == []


# ---- the store ----------------------------------------------------------------------------------

ORIGIN = {"url": LINK, "commit": "a" * 40, "path": "skills/pdf-forms"}


def test_repo_origin_is_saved_with_the_bytes_and_survives_a_reload():
    skills.save("pdf-forms", {"SKILL.md": SKILL}, repo=ORIGIN)
    again = skills.get("pdf-forms")
    assert again["repo_origin"] == {**ORIGIN, "unverified": True}
    assert again["gallery_origin"] is None
    assert skills.listing()["skills"][0]["repo_origin"]["commit"] == "a" * 40


def test_a_failed_save_leaves_no_partial_skill(monkeypatch):
    real = os.setxattr

    def refuse(fd, name, value, flags=0):
        if name == repo_origin.XATTR:
            raise OSError(95, "Operation not supported")
        return real(fd, name, value, flags)

    monkeypatch.setattr(os, "setxattr", refuse)
    with pytest.raises(contract.GalleryError, match="nothing was saved"):
        skills.save("pdf-forms", {"SKILL.md": SKILL}, repo=ORIGIN)
    with pytest.raises(store.NotFound):
        skills.get("pdf-forms")
    assert [s["id"] for s in skills.listing()["skills"]] == []


@pytest.mark.parametrize(
    "bad",
    [
        {**ORIGIN, "url": "http://github.com/acme/x"},
        {**ORIGIN, "url": "https://u:p@github.com/acme/x"},
        {**ORIGIN, "commit": "abc"},
        {**ORIGIN, "path": "../etc"},
        {**ORIGIN, "unverified": False},
        {**ORIGIN, "verified": True},
    ],
)
def test_only_the_one_unverified_shape_is_accepted(bad):
    with pytest.raises(contract.GalleryError):
        skills.save("pdf-forms", {"SKILL.md": SKILL}, repo=bad)
    with pytest.raises(store.NotFound):
        skills.get("pdf-forms")


def test_the_create_route_takes_an_optional_repo_origin(live):
    r = live.c.post(
        "/api/library/skills",
        json={"new_id": "pdf-forms", "files": {"SKILL.md": SKILL}, "repo_origin": ORIGIN},
        headers=live.hdr,
    )
    assert r.status_code == 201, r.text
    assert r.json()["repo_origin"] == {**ORIGIN, "unverified": True}
    r = live.c.post(
        "/api/library/skills",
        json={"new_id": "other", "files": {"SKILL.md": SKILL}, "bogus": 1},
        headers=live.hdr,
    )
    assert r.status_code >= 400
