"""HQ writes the Library (#1611): playbooks, skills and automations, proposed by the model, shown
verbatim on a card, saved by the Library's own route when the operator confirms.

Pinned here: an invalid draft never reaches a card; the card's content is the draft it is bound
to, and a draft changed on disk after staging runs nothing; an edit binds the revision HQ read, so
a concurrent save makes it stale; a new automation is saved off, enabling is its own consent card,
and a widening edit of an enabled one consents to the NEW scope in one PATCH; HQ never stages
permission bypass or auto-choose autonomy; targets are this turn's; and secrets are guarded whole,
before any cut, both when HQ reads a file and when it drafts one.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_sessions import ask_library, ask_store, template_secrets
from agent_sessions import automations_store as astore
from agent_sessions.routes import automations as aroutes
from test_ask_operations import _confirm, _project, _result, _settled, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

BUNDLED = Path(ask_library.__file__).parent / "playbooks" / "bundled" / "research-brief"
CANARY = "zq9-canary-Vh3kP0wLx7"


def _bundle(pid: str = "release-train", name: str = "Release train") -> dict[str, str]:
    files = {str(p.relative_to(BUNDLED)): p.read_text() for p in BUNDLED.rglob("*") if p.is_file()}
    toml = files["playbook.toml"]
    head, sep, rest = toml.partition("[identity]")
    ident, _, after = rest.partition("\n\n")
    lines = []
    for ln in ident.split("\n"):
        if ln.startswith("id ="):
            ln = f'id = "{pid}"'
        elif ln.startswith("name ="):
            ln = f'name = "{name}"'
        lines.append(ln)
    files["playbook.toml"] = head + sep + "\n".join(lines) + "\n\n" + after
    return files


def _ask(a, ai, *steps, question: str = "do it") -> tuple[str, object]:
    script = ai(*steps, answer("Proposed; confirm it on the card."))
    cid = a.new()
    r = a.ask(cid, question)
    assert r.status_code == 200, r.text
    return cid, script


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _content(a, card: dict):
    return a.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=a.hdr)


def _local_playbook(a, files: dict) -> dict:
    r = a.c.post("/api/playbooks", json={"files": files}, headers=a.hdr)
    assert r.status_code == 201, r.text
    return r.json()


# ---- playbooks ----------------------------------------------------------------------------------


def test_create_shows_the_files_and_the_confirm_saves_them_through_the_library_route(live, ai):
    files = _bundle()
    cid, script = _ask(
        live, ai, call("propose_library", {"group": "playbook", "action": "create", "files": files})
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "playbook.create" and card["state"] == "pending"
    assert card["content"] is True and "doesn't run" in card["text"]
    shown = _content(live, card).json()
    assert shown["kind"] == "files"
    assert {f["path"]: f["text"] for f in shown["files"]} == files
    assert live.c.get("/api/playbooks/release-train", headers=live.hdr).status_code == 404

    assert _confirm(live, card["action_id"]).status_code == 202
    done = _settled(live, cid)
    assert done["state"] == "done", done
    assert done["library"] == {"kind": "playbook", "id": "release-train"}
    saved = live.c.get("/api/playbooks/release-train", headers=live.hdr).json()
    assert saved["files"] == files and saved["editable"] is True


def test_an_invalid_playbook_never_reaches_a_card(live, ai):
    files = _bundle()
    files["playbook.toml"] = files["playbook.toml"].replace("[identity]", "[identity]\nbogus = 1")
    cid, script = _ask(
        live, ai, call("propose_library", {"group": "playbook", "action": "create", "files": files})
    )
    assert _cards(live, cid) == []
    assert "not valid" in json.dumps(_result(script, 1))


def _propose_edit(a, ai, pid: str, changes: dict, **extra) -> str:
    cid, _ = _ask(
        a,
        ai,
        calls(("list_playbooks", {}), ("playbook_detail", {"id": pid})),
        call(
            "propose_library",
            {"group": "playbook", "action": "edit", "id": pid, "files": changes, **extra},
            cid="p",
        ),
    )
    return cid


def test_an_edit_shows_a_diff_and_saves_the_merged_tree(live, ai):
    files = _bundle()
    _local_playbook(live, files)
    cid = _propose_edit(live, ai, "release-train", {"README.md": "# Release train\n\nNew.\n"})
    (card,) = _cards(live, cid)
    assert card["op"] == "playbook.edit"
    (d,) = _content(live, card).json()["diff"]
    assert d["path"] == "README.md" and d["status"] == "changed" and "+New." in d["diff"]
    assert _confirm(live, card["action_id"]).status_code == 202
    assert _settled(live, cid)["state"] == "done"
    saved = live.c.get("/api/playbooks/release-train", headers=live.hdr).json()["files"]
    assert saved["README.md"] == "# Release train\n\nNew.\n"
    # Untouched files are carried over exactly.
    assert {k: v for k, v in saved.items() if k != "README.md"} == {
        k: v for k, v in files.items() if k != "README.md"
    }


def test_an_edit_after_a_concurrent_save_is_stale_and_writes_nothing(live, ai):
    files = _bundle()
    first = _local_playbook(live, files)
    cid = _propose_edit(live, ai, "release-train", {"README.md": "HQ's version\n"})
    (card,) = _cards(live, cid)
    elsewhere = {**files, "README.md": "the Library's version\n"}
    r = live.c.put(
        "/api/playbooks/release-train",
        json={"revision": first["revision"], "files": elsewhere},
        headers=live.hdr,
    )
    assert r.status_code == 200, r.text
    _confirm(live, card["action_id"])
    out = _settled(live, cid)
    assert out["state"] == "stale" and "saved elsewhere" in out["reason"]
    saved = live.c.get("/api/playbooks/release-train", headers=live.hdr).json()["files"]
    assert saved["README.md"] == "the Library's version\n"


def test_a_bundled_playbook_is_never_edited_in_place(live, ai):
    cid = _propose_edit(live, ai, "research-brief", {"README.md": "x\n"})
    assert _cards(live, cid) == []


def test_an_id_no_tool_returned_this_turn_is_refused(live, ai):
    _local_playbook(live, _bundle())
    cid, script = _ask(
        live,
        ai,
        call(
            "propose_library",
            {"group": "playbook", "action": "edit", "id": "release-train", "files": {"a": "b"}},
        ),
    )
    assert _cards(live, cid) == []
    assert "returned in this turn" in json.dumps(_result(script, 1))


def test_a_draft_changed_on_disk_after_staging_runs_nothing(live, ai):
    cid, _ = _ask(
        live,
        ai,
        call("propose_library", {"group": "playbook", "action": "create", "files": _bundle()}),
    )
    (card,) = _cards(live, cid)
    path = ask_store._draft_path(cid, card["action_id"])
    data = json.loads(path.read_text())
    data["files"]["README.md"] = "swapped after the card was shown\n"
    path.write_text(json.dumps(data, sort_keys=True))
    assert _content(live, card).status_code == 409
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "stale"
    assert live.c.get("/api/playbooks/release-train", headers=live.hdr).status_code == 404


def test_the_draft_file_is_private(live, ai):
    cid, _ = _ask(
        live,
        ai,
        call("propose_library", {"group": "playbook", "action": "create", "files": _bundle()}),
    )
    (card,) = _cards(live, cid)
    st = os.stat(ask_store._draft_path(cid, card["action_id"]))
    assert st.st_mode & 0o777 == 0o600


# ---- secrets ------------------------------------------------------------------------------------


def test_a_secret_across_the_cut_is_guarded_before_the_cut(live, ai, monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [CANARY])
    files = _bundle()
    # The secret STRADDLES the per-file cut: cut first and its head would be sent; guarded whole
    # first, nothing of it survives.
    files["README.md"] = "x" * (ask_library.FILE_CHARS_MAX - 10) + f"{CANARY}\n" + "y" * 200
    _local_playbook(live, files)
    cid, script = _ask(
        live,
        ai,
        calls(
            ("list_playbooks", {}),
        ),
        call("playbook_detail", {"id": "release-train", "path": "README.md"}, cid="d"),
    )
    shown = _result(script, 2)
    assert shown["cut"] == ["README.md"]
    sent = json.dumps(script.bodies)
    assert CANARY[:10] not in sent and CANARY not in sent


def test_a_draft_holding_a_secret_is_refused_never_stored_altered(live, ai, monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [CANARY])
    files = _bundle()
    files["README.md"] = f"token: {CANARY}\n"
    cid, script = _ask(
        live, ai, call("propose_library", {"group": "playbook", "action": "create", "files": files})
    )
    assert _cards(live, cid) == []
    assert "secret" in json.dumps(_result(script, 1))
    assert not (ask_store.root() / "drafts" / cid).exists() or not any(
        (ask_store.root() / "drafts" / cid).iterdir()
    )


# ---- skills -------------------------------------------------------------------------------------

SKILL = {"SKILL.md": "---\nname: changelog-writer\ndescription: Write changelogs.\n---\n\nDo it.\n"}


def test_the_skill_create_route_is_noreplace_and_csrf_guarded(live):
    body = {"new_id": "changelog-writer", "files": SKILL}
    assert live.c.post("/api/library/skills", json=body).status_code in (401, 403)
    assert live.c.post("/api/library/skills", json=body, headers=live.hdr).status_code == 201
    again = live.c.post("/api/library/skills", json=body, headers=live.hdr)
    assert again.status_code == 409, again.text
    bad = live.c.post(
        "/api/library/skills", json={"new_id": "x", "files": {"../a": "b"}}, headers=live.hdr
    )
    assert bad.status_code == 422


def test_hq_creates_a_skill_through_the_new_route(live, ai):
    cid, _ = _ask(
        live,
        ai,
        call(
            "propose_library",
            {"group": "skill", "action": "create", "new_id": "changelog-writer", "files": SKILL},
        ),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "skill.create" and card["title"] == "changelog-writer"
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    got = live.c.get("/api/library/skills/changelog-writer", headers=live.hdr).json()
    assert got["files"] == SKILL


def test_a_skill_edit_is_saved_as_a_new_copy_and_the_original_stays(live, ai):
    live.c.post(
        "/api/library/skills", json={"new_id": "changelog-writer", "files": SKILL}, headers=live.hdr
    )
    changed = {"SKILL.md": SKILL["SKILL.md"] + "\nAlso link the PRs.\n"}
    cid, _ = _ask(
        live,
        ai,
        calls(("list_skills", {}), ("skill_detail", {"id": "changelog-writer"})),
        call(
            "propose_library",
            {
                "group": "skill",
                "action": "edit",
                "id": "changelog-writer",
                "new_id": "changelog-writer-2",
                "files": changed,
            },
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "skill.edit" and "original stays" in card["text"]
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    original = live.c.get("/api/library/skills/changelog-writer", headers=live.hdr).json()
    copy = live.c.get("/api/library/skills/changelog-writer-2", headers=live.hdr).json()
    assert original["files"] == SKILL and copy["files"] == changed


# ---- automations --------------------------------------------------------------------------------


@pytest.fixture
def mission_cwd(monkeypatch, tmp_path):
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes, "_resolve_cwd", lambda pid, **kw: (pid, str(tmp_path)))


def _mission(**over) -> dict:
    return {
        "group": "automation",
        "action": "create",
        "name": "nightly audit",
        "trigger": {"kind": "manual"},
        "action_kind": "start_mission",
        "project_index": 0,
        "message": "Audit the dependencies and open one issue.",
        **over,
    }


def _stored(name: str = "nightly audit", *, enabled: bool) -> str:
    cfg = {
        "name": name,
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_mission",
            "project_id": "p1",
            "instruction": {"text": "Audit the dependencies."},
            "autonomy": "propose",
        },
    }
    aid = aroutes._create(cfg)["id"]
    if enabled:
        pub = aroutes.public(astore.get(aid))
        aroutes._enable(
            aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
        )
    return aid


def test_a_new_automation_is_saved_off_from_this_turns_project(live, ai, mission_cwd):
    _project(live, "web")
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_projects", {}),
        ),
        call("propose_library", _mission(), cid="p"),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "automation.create" and "switched off" in card["text"]
    shown = _content(live, card).json()
    assert shown["kind"] == "automation" and shown["status"] == "off"
    assert shown["instruction"] == "Audit the dependencies and open one issue."
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    (row,) = live.c.get("/api/automations", headers=live.hdr).json()["automations"]
    assert row["enabled"] is False and row["action"]["autonomy"] == "propose"


@pytest.mark.parametrize(
    "over",
    [
        {"autonomy": "dispatch_auto_choose"},
        {"action_kind": "start_session", "engine_index": 0, "bypass": True},
    ],
)
def test_hq_never_stages_bypass_or_auto_choose(live, ai, mission_cwd, over):
    _project(live, "web")
    cid, script = _ask(
        live,
        ai,
        calls(("list_projects", {}), ("list_agents", {})),
        call("propose_library", _mission(**over), cid="p"),
    )
    assert _cards(live, cid) == []
    assert "never" in json.dumps(_result(script, 2))


def test_bypass_on_is_refused_even_on_a_stored_config():
    with pytest.raises(ask_library.Refused):
        ask_library._fence({"action": {"kind": "start_session", "bypass": True}})


def test_enable_is_a_consent_card_bound_to_the_scope_it_showed(live, ai, mission_cwd):
    aid = _stored(enabled=False)
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call("propose_library", {"group": "automation", "action": "enable", "id": aid}, cid="p"),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "automation.enable" and card["risk"] == "external"
    shown = _content(live, card).json()
    assert shown["scope_lines"] and shown["instruction"] == "Audit the dependencies."
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    assert astore.get(aid)["enabled"]


def test_enable_after_the_scope_changed_is_stale_and_stays_off(live, ai, mission_cwd):
    aid = _stored(enabled=False)
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call("propose_library", {"group": "automation", "action": "enable", "id": aid}, cid="p"),
    )
    (card,) = _cards(live, cid)
    rev = astore.get(aid)["revision"]
    aroutes._patch(
        aid,
        {
            "revision": rev,
            "action": {
                "kind": "start_mission",
                "project_id": "p1",
                "instruction": {"text": "Something else entirely."},
                "autonomy": "propose",
            },
        },
    )
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "stale"
    assert not astore.get(aid)["enabled"]


def test_a_widening_edit_of_an_enabled_automation_consents_to_the_new_scope(live, ai, mission_cwd):
    aid = _stored(enabled=True)
    daily = {"kind": "schedule", "cadence": {"kind": "daily", "time": "02:00"}, "tz": "UTC"}
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call(
            "propose_library",
            {"group": "automation", "action": "edit", "id": aid, "trigger": daily},
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "automation.widen" and card["verb"] == "Save and consent"
    shown = _content(live, card).json()
    assert shown["status"] == "enabled" and shown["widened"]
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    row = astore.get(aid)
    assert row["enabled"] and row["config"]["trigger"]["kind"] == "schedule"


def test_a_narrowing_edit_of_an_enabled_automation_says_it_remains_enabled(live, ai, mission_cwd):
    aid = _stored(enabled=True)
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call(
            "propose_library",
            {
                "group": "automation",
                "action": "edit",
                "id": aid,
                "policy": {"pause_after_failures": 1},
            },
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "automation.edit" and "remains enabled" in card["text"].lower()
    assert "doesn't widen" in card["text"]
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    assert astore.get(aid)["config"]["policy"]["pause_after_failures"] == 1


def test_an_edit_whose_automation_was_switched_off_since_is_stale(live, ai, mission_cwd):
    aid = _stored(enabled=True)
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call(
            "propose_library",
            {"group": "automation", "action": "edit", "id": aid, "name": "renamed"},
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    rev = astore.get(aid)["revision"]
    live.c.post(f"/api/automations/{aid}/disable", json={"revision": rev}, headers=live.hdr)
    _confirm(live, card["action_id"])
    out = _settled(live, cid)
    assert out["state"] == "stale" and "switched off" in out["reason"]
    assert astore.get(aid)["name"] == "nightly audit"


# ---- review round (#1611): what HQ read is what it edits ----------------------------------------


def test_a_model_file_named_like_a_trusted_key_is_still_guarded(live, ai, monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [CANARY])
    files = {**SKILL, "revision": f"{CANARY}\n"}
    cid, script = _ask(
        live,
        ai,
        call(
            "propose_library",
            {"group": "skill", "action": "create", "new_id": "leaky", "files": files},
        ),
    )
    assert _cards(live, cid) == []
    assert "secret" in json.dumps(_result(script, 1))


def test_an_edit_is_refused_when_the_library_saved_after_hq_read_it(live, ai, monkeypatch):
    import httpx

    from agent_sessions import review

    files = _bundle()
    first = _local_playbook(live, files)
    script = ai(
        calls(("list_playbooks", {}), ("playbook_detail", {"id": "release-train"})),
        call(
            "propose_library",
            {
                "group": "playbook",
                "action": "edit",
                "id": "release-train",
                "files": {"README.md": "HQ's rewrite of what it read\n"},
            },
            cid="p",
        ),
        answer("Proposed."),
    )
    inner = script.handler

    async def save_between(request):
        # After HQ's read (the 2nd request carries its result), the operator saves in the Library.
        if len(script.bodies) == 1:
            from agent_sessions.playbooks import store

            store.update_playbook(
                "release-train", first["revision"], {**files, "README.md": "mine\n"}
            )
        return await inner(request)

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(save_between))
    cid = live.new()
    assert live.ask(cid, "edit it").status_code == 200
    assert _cards(live, cid) == []
    assert "saved elsewhere after you read it" in json.dumps(_result(script, 2))
    saved = live.c.get("/api/playbooks/release-train", headers=live.hdr).json()["files"]
    assert saved["README.md"] == "mine\n"


def test_an_edit_without_a_detail_read_is_refused(live, ai):
    _local_playbook(live, _bundle())
    cid, script = _ask(
        live,
        ai,
        calls(
            ("list_playbooks", {}),
        ),
        call(
            "propose_library",
            {
                "group": "playbook",
                "action": "edit",
                "id": "release-train",
                "files": {"README.md": "x\n"},
            },
            cid="p",
        ),
    )
    assert _cards(live, cid) == []
    assert "playbook_detail" in json.dumps(_result(script, 2))


def test_a_file_hq_saw_only_in_part_is_never_rewritten(live, ai):
    files = _bundle()
    files["README.md"] = "x" * (ask_library.FILE_CHARS_MAX + 100)
    _local_playbook(live, files)
    cid, script = _ask(
        live,
        ai,
        calls(("list_playbooks", {}), ("playbook_detail", {"id": "release-train"})),
        call(
            "propose_library",
            {
                "group": "playbook",
                "action": "edit",
                "id": "release-train",
                "files": {"README.md": "short\n"},
            },
            cid="p",
        ),
    )
    assert _cards(live, cid) == []
    assert "read README.md whole first" in json.dumps(_result(script, 2))


# ---- review round 2 (#1611) ---------------------------------------------------------------------


def test_a_file_name_carrying_a_secret_never_reaches_the_model(live, ai, monkeypatch):
    live.c.post(
        "/api/library/skills",
        json={"new_id": "named", "files": {**SKILL, f"notes-{CANARY}.md": "x\n"}},
        headers=live.hdr,
    )
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [CANARY])
    cid, script = _ask(
        live,
        ai,
        calls(
            ("list_skills", {}),
            ("skill_detail", {"id": "named"}),
        ),
    )
    sent = json.dumps(script.bodies)
    assert CANARY not in sent
    assert "file names contains a secret" in sent


def _draft_of(live, ai) -> tuple[str, Path]:
    cid, _ = _ask(
        live,
        ai,
        call("propose_library", {"group": "playbook", "action": "create", "files": _bundle()}),
    )
    (card,) = _cards(live, cid)
    _confirm(live, card["action_id"], "dismiss")
    return cid, ask_store._draft_path(cid, card["action_id"])


def test_deleting_a_conversation_deletes_its_drafts(live, ai):
    cid, draft = _draft_of(live, ai)
    assert draft.exists()
    r = live.c.delete(f"/api/ask/conversations/{cid}", headers=live.hdr)
    assert r.status_code in (200, 204), r.text
    assert not draft.exists() and not draft.parent.exists()


def test_the_sweep_removes_drafts_whose_conversation_is_gone(live, ai):
    cid, draft = _draft_of(live, ai)
    ask_store._path(cid).unlink()  # a delete that stopped before its drafts went
    ask_store.sweep()
    assert not draft.exists()


# ---- review round 3 (#1611): the card discloses every change ------------------------------------


def _skill_edit_card(live, ai, files: dict) -> dict:
    live.c.post(
        "/api/library/skills", json={"new_id": "changelog-writer", "files": SKILL}, headers=live.hdr
    )
    cid, _ = _ask(
        live,
        ai,
        calls(("list_skills", {}), ("skill_detail", {"id": "changelog-writer"})),
        call(
            "propose_library",
            {
                "group": "skill",
                "action": "edit",
                "id": "changelog-writer",
                "new_id": "changelog-writer-2",
                "files": files,
            },
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    return card


def test_an_edit_that_adds_a_file_shows_it_on_the_card(live, ai):
    card = _skill_edit_card(live, ai, {"notes.md": "Remember the PR links.\n"})
    (d,) = _content(live, card).json()["diff"]
    assert d["path"] == "notes.md" and d["status"] == "added" and "+Remember" in d["diff"]


def test_a_newline_only_edit_shows_the_changed_ending(live, ai):
    card = _skill_edit_card(live, ai, {"SKILL.md": SKILL["SKILL.md"].rstrip("\n")})
    (d,) = _content(live, card).json()["diff"]
    assert ask_library.NO_NEWLINE in d["diff"] and d["added"] == 1 and d["removed"] == 1


def test_an_automation_rename_is_shown_before_confirm(live, ai, mission_cwd):
    aid = _stored(enabled=True)
    cid, _ = _ask(
        live,
        ai,
        calls(
            ("list_automations", {}),
        ),
        call(
            "propose_library",
            {"group": "automation", "action": "edit", "id": aid, "name": "weekly audit"},
            cid="p",
        ),
    )
    (card,) = _cards(live, cid)
    shown = _content(live, card).json()
    assert shown["name"] == "weekly audit" and shown["changed"] == ["name"]


@pytest.mark.parametrize("flag", ["durable", "state_durable", "recovery_durable"])
def test_a_save_whose_flush_failed_is_never_reported_as_confirmed(flag):
    from agent_sessions import ask_operations

    for op_id in ("playbook.create", "playbook.edit", "skill.create", "skill.edit"):
        op = ask_operations.CATALOG[op_id]
        assert op.outcome(201, {flag: False}, {}) == (
            ask_operations.UNCONFIRMED,
            ask_library.NOT_DURABLE,
        )
        assert op.outcome(201, {flag: True}, {})[0] == ask_operations.DONE
