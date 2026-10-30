"""HQ's confirmed operations (#1558 P1, #1559): proposed by the model, confirmed on a card, run
through the UI's own route as the operator.

Pinned here: the catalog names only routes the app registers; a card and its entry are bound, so a
tampered sidecar or a target that changed since the card was shown runs nothing; the confirm is a
server task (202) whose outcome is the route's own fact; the decide route's own guards (CSRF)
hold; a crash mid-run is `interrupted`, never replayed; forged indexes and bad fields stage
nothing; and no method, path or body is ever stored.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json
import os
import time

import pytest
from starlette.routing import Match

from agent_sessions import ask_operations, ask_store, prefs, projects
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    _login,
    ai,
    answer,
    app,
    call,
    calls,
)
from test_ask_sessions import Live

# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture
def live(auth_cfg):
    a = Live(auth_cfg)
    try:
        yield a
    finally:
        a.close()


def _project(a, name: str) -> str:
    r = a.c.post("/api/projects", json={"name": name}, headers=a.hdr)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"] if "id" in r.json() else r.json()["project"]["id"]


def _propose_project(a, ai, action: str, **fields) -> tuple[str, dict]:
    script = ai(
        calls(("list_projects", {})),
        call("propose_project_change", {"action": action, "project_index": 0, **fields}, cid="p"),
        answer("Proposed; confirm it on the card."),
    )
    cid = a.new()
    r = a.ask(cid, f"{action} the project")
    assert r.status_code == 200, r.text
    return cid, {"turn": r.json()["turn"], "script": script}


def _card(a, cid: str) -> dict:
    (card,) = [p for t in a.get(cid)["turns"] for p in t["proposals"]]
    return card


def _settled(a, cid: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while True:
        card = _card(a, cid)
        if card["state"] != "deciding" or time.monotonic() > end:
            return card
        time.sleep(0.05)


def _confirm(a, pid: str, decision: str = "confirm"):
    return a.c.post(f"/api/ask/proposals/{pid}/decide", json={"decision": decision}, headers=a.hdr)


def _result(script, n: int = 2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


# ---- the catalog --------------------------------------------------------------------------------


def _resolves(app, method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in app.router.routes:
        matched, _child = route.matches(scope)
        if matched == Match.FULL:
            return True
    return False


def test_every_catalog_operation_names_a_route_the_app_registers(app, monkeypatch):
    monkeypatch.setattr(ask_operations, "_folder_patch", lambda pid, facts, **kw: {})
    facts = {"runtime": "terminal", "name": "p", "sessions": 1, "matched": 1, "revision": 2}
    fields = {"title": "t", "tag": "t", "color": "", "name": "n", "days": 3, "folder": "/x"}
    fields.update(project_id="p1", project_name="p", new_id="copy", enabled=True)
    # A secret (#1786): the create's card input, and the revision an access card reads at the tap.
    fields.update(secret_name="asc_api_key", description="", value="card-typed-value-1786")
    from agent_sessions import ask_secrets

    monkeypatch.setattr(ask_secrets, "_current", lambda target, facts: 1.0)
    # A library save (#1611) builds its body from its bound draft, injected at the tap.
    fields["_content"] = {
        "files": {"playbook.toml": ""},
        "revision": "r",
        "origin": None,
        "new_id": "copy",
        "operation": "0" * 32,
        "config": {},
        "body": {"name": "n"},
        "scope_digest": "d",
    }
    for op in ask_operations.CATALOG.values():
        target = {
            "session": "claude:00000000-0000-4000-8000-0000000000a1",
            "project": "p1",
            "playbook": "release-train",
            "skill": "changelog-writer",
            "automation": "aut1",
            "secret": "0" * 32,
            "attachment": "0" * 32,
        }.get(op.target, "")
        for runtime in ("terminal", "api"):
            method, path, _body = op.route(target, fields, dict(facts, runtime=runtime))
            assert _resolves(app.app, method, path), (op.id, method, path)


def test_close_stops_an_api_session_and_archives_a_terminal_one():
    op = ask_operations.CATALOG["session.close"]
    key = "codex:abc"
    assert op.route(key, {}, {"runtime": "api"})[1] == f"/api/structured/sessions/{key}/stop"
    assert op.route(key, {}, {"runtime": "terminal"})[1] == f"/api/sessions/{key}/archive"


@pytest.mark.parametrize(
    "op_id, raw",
    [
        ("session.rename", {"title": ""}),
        ("session.rename", {"title": "x" * 121}),
        ("session.rename", {"title": "two\nlines"}),
        ("session.color", {"color": "red"}),
        ("sessions.archive_older", {"days": 0}),
        ("sessions.archive_older", {"days": 4000}),
        ("sessions.archive_older", {"days": True}),
        ("sessions.archive_older", {"days": "7"}),
        ("session.rename", {}),
    ],
)
def test_fields_outside_their_gate_are_refused(op_id, raw):
    with pytest.raises(ask_operations.Refused):
        ask_operations.gate_fields(ask_operations.CATALOG[op_id], raw)


def test_a_project_archive_with_a_failed_member_is_partly_done():
    body = {
        "counts": {"archived": 2, "failed": 1},
        "sessions": [
            {"id": "a", "result": "archived"},
            {"id": "b", "result": "archived"},
            {"id": "c", "result": "failed", "reason": "a mission holds this session"},
        ],
    }
    state, said = ask_operations._bulk_project(200, body, "archived")
    assert state == ask_operations.PARTLY_DONE
    assert said.startswith("2 of 3 sessions archived; 1 not") and "a mission holds" in said


def test_archive_older_with_skipped_sessions_is_partly_done():
    assert ask_operations._older(200, {"archived": 3, "skipped": 2})[0] == "partly_done"
    assert ask_operations._older(200, {"archived": 3, "skipped": 0})[0] == "done"


def test_a_failed_session_archive_never_claims_nothing_changed():
    op = ask_operations.CATALOG["session.archive"]
    said = op.failure(409, "a mission holds this session", {})
    assert "may already have been stopped" in said and "Nothing changed" not in said


# ---- proposing ----------------------------------------------------------------------------------


def test_a_project_rename_is_a_bound_card_and_a_pending_entry_and_nothing_runs(app, ai):
    pid = _project(app, "scratch")
    cid, got = _propose_project(app, ai, "rename", name="scratch-2")
    assert _result(got["script"])["proposed"] is True
    (card,) = got["turn"]["proposals"]
    assert card["kind"] == "operation" and card["state"] == "pending"
    assert card["op"] == "project.rename" and card["title"] == "scratch"
    assert card["text"] == "Renames scratch to “scratch-2”." and card["verb"] == "Rename"
    assert card["can_confirm"] and card["can_dismiss"]
    (row,) = ask_store.read_proposals(cid)
    assert row["target"] == pid and row["fields"] == {"name": "scratch-2"}
    assert row["facts"]["name"] == "scratch"
    assert projects.load()[pid].name == "scratch"


def test_no_method_path_or_body_is_ever_stored(app, ai):
    _project(app, "scratch")
    cid, _ = _propose_project(app, ai, "rename", name="scratch-2")
    raw = (ask_store.root() / "proposals" / f"{cid}.json").read_text()
    assert "/api/" not in raw and '"method"' not in raw and '"path"' not in raw


@pytest.mark.parametrize("index", [3, "0", False])
def test_a_forged_project_index_stages_nothing(app, ai, index):
    _project(app, "scratch")
    script = ai(
        calls(("list_projects", {})),
        call(
            "propose_project_change",
            {"action": "rename", "project_index": index, "name": "x"},
            cid="p",
        ),
        answer("ok"),
    )
    cid = app.new()
    turn = app.ask(cid, "rename it").json()["turn"]
    assert "refused" in _result(script)["error"]
    assert turn["proposals"] == [] and ask_store.read_proposals(cid) == []


def test_an_index_from_a_list_this_turn_never_fetched_is_refused(app, ai):
    _project(app, "scratch")
    script = ai(
        call("propose_project_change", {"action": "archive", "project_index": 0}, cid="p"),
        answer("ok"),
    )
    cid = app.new()
    app.ask(cid, "archive it")
    assert "list_projects" in _result(script, 1)["error"]
    assert ask_store.read_proposals(cid) == []


def test_a_name_carrying_a_credential_is_refused(app, ai):
    _project(app, "scratch")
    cid, got = _propose_project(
        app, ai, "rename", name="AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX"
    )
    assert "credential" in _result(got["script"])["error"]
    assert ask_store.read_proposals(cid) == []


# ---- the tap ------------------------------------------------------------------------------------


def test_confirm_runs_the_route_as_a_task_and_the_card_reads_done(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    card = _card(live, cid)
    r = _confirm(live, card["action_id"])
    assert r.status_code == 202 and r.json()["proposal"]["state"] == "deciding"
    card = _settled(live, cid)
    assert card["state"] == "done", card
    assert card["summary"] == "Renamed."
    assert projects.load()[pid].name == "scratch-2"


def test_a_second_confirm_runs_nothing_more(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    aid = _card(live, cid)["action_id"]
    assert _confirm(live, aid).status_code == 202
    _settled(live, cid)
    projects.update(pid, name="renamed-by-hand")
    again = _confirm(live, aid)
    assert again.status_code == 200 and again.json()["proposal"]["state"] == "done"
    assert projects.load()[pid].name == "renamed-by-hand"


def test_dismiss_runs_nothing(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    r = _confirm(live, _card(live, cid)["action_id"], "dismiss")
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "dismissed"
    assert projects.load()[pid].name == "scratch"


def test_a_project_renamed_since_the_card_was_shown_is_stale_and_nothing_runs(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "color", color="#336699")
    projects.update(pid, name="renamed-meanwhile")
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "renamed" in card["reason"]
    assert projects.load()[pid].color in ("", None)


def test_a_tampered_sidecar_target_runs_nothing(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "delete")
    other = _project(live, "keep-me")
    path = ask_store.root() / "proposals" / f"{cid}.json"
    data = json.loads(path.read_text())
    data["proposals"][0]["target"] = other
    path.write_text(json.dumps(data))
    r = _confirm(live, _card(live, cid)["action_id"])
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "stale"
    assert {pid, other} <= set(projects.load())


def test_a_tampered_operation_id_runs_nothing(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    path = ask_store.root() / "proposals" / f"{cid}.json"
    data = json.loads(path.read_text())
    data["proposals"][0]["op"] = "project.delete"
    path.write_text(json.dumps(data))
    r = _confirm(live, _card(live, cid)["action_id"])
    assert r.json()["proposal"]["state"] == "stale"
    assert pid in projects.load()


def test_a_confirm_without_the_csrf_token_is_refused_and_runs_nothing(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    aid = _card(live, cid)["action_id"]
    r = live.c.post(
        f"/api/ask/proposals/{aid}/decide",
        json={"decision": "confirm"},
        headers={"Origin": live.cfg.origin},
    )
    assert r.status_code == 403
    assert _card(live, cid)["state"] == "pending"
    assert projects.load()[pid].name == "scratch"


def test_a_session_proposal_cannot_be_confirmed_and_an_operation_cannot_be_started(live, ai):
    _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    r = _confirm(live, _card(live, cid)["action_id"], "start")
    assert r.status_code == 422


def test_a_crash_mid_run_reads_interrupted_and_is_never_replayed(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    aid = _card(live, cid)["action_id"]
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        rows[0]["state"] = "deciding"
        ask_store.write_proposals(cid, rows)
    card = _card(live, cid)  # the decide lock is free: nobody is running it
    assert card["state"] == "interrupted" and "not retried" in card["reason"]
    assert _confirm(live, aid).json()["proposal"]["state"] == "interrupted"
    assert projects.load()[pid].name == "scratch"


def test_the_delete_card_says_the_sessions_are_kept_and_reads_destructive(app, ai):
    _project(app, "scratch")
    cid, got = _propose_project(app, ai, "delete")
    (card,) = got["turn"]["proposals"]
    assert card["risk"] == "destructive" and card["verb"] == "Delete project"
    assert "kept" in card["text"] and "lost" in card["text"]


def test_the_entry_file_is_private(app, ai):
    _project(app, "scratch")
    cid, _ = _propose_project(app, ai, "rename", name="scratch-2")
    mode = os.stat(ask_store.root() / "proposals" / f"{cid}.json").st_mode & 0o777
    assert mode == 0o600


# ---- operator inputs: the folder card ------------------------------------------------------------


def _propose_create(a, ai, name: str = "web") -> str:
    ai(
        call("propose_project_change", {"action": "create", "name": name}, cid="p"),
        answer("Pick the folder on the card."),
    )
    cid = a.new()
    assert a.ask(cid, "make a project").status_code == 200
    return cid


def test_create_asks_the_operator_for_the_folder_and_the_model_never_names_one(live, ai, tmp_path):
    cid = _propose_create(live, ai)
    card = _card(live, cid)
    assert card["op"] == "project.create" and card["needs"] == ["folder"]
    folder = tmp_path / "web"
    folder.mkdir()
    r = live.c.post(
        f"/api/ask/proposals/{card['action_id']}/decide",
        json={"decision": "confirm", "inputs": {"folder": str(folder)}},
        headers=live.hdr,
    )
    assert r.status_code == 202, r.text
    assert _settled(live, cid)["state"] == "done"
    (made,) = [p for p in projects.load().values() if p.name == "web"]
    assert list(made.folders) == [str(folder)] and made.default_folder == str(folder)


@pytest.mark.parametrize(
    "inputs",
    [None, {}, {"folder": "relative/path"}, {"folder": "/a/../etc"}, {"folder": 3}],
    ids=["none", "empty", "relative", "dotdot", "not-a-string"],
)
def test_a_missing_or_bad_folder_is_refused_and_the_card_stays_pending(live, ai, inputs):
    cid = _propose_create(live, ai)
    aid = _card(live, cid)["action_id"]
    body = {"decision": "confirm", **({"inputs": inputs} if inputs is not None else {})}
    r = live.c.post(f"/api/ask/proposals/{aid}/decide", json=body, headers=live.hdr)
    assert r.status_code == 422
    assert _card(live, cid)["state"] == "pending"
    assert not [p for p in projects.load().values() if p.name == "web"]


def test_an_input_on_an_operation_that_takes_none_is_refused(live, ai):
    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "rename", name="scratch-2")
    r = live.c.post(
        f"/api/ask/proposals/{_card(live, cid)['action_id']}/decide",
        json={"decision": "confirm", "inputs": {"folder": "/tmp"}},
        headers=live.hdr,
    )
    assert r.status_code == 422
    assert projects.load()[pid].name == "scratch"


def test_add_and_release_a_folder_through_the_card(live, ai, tmp_path):
    pid = _project(live, "scratch")
    folder = tmp_path / "extra"
    folder.mkdir()
    cid, _ = _propose_project(live, ai, "add_folder")
    aid = _card(live, cid)["action_id"]
    r = live.c.post(
        f"/api/ask/proposals/{aid}/decide",
        json={"decision": "confirm", "inputs": {"folder": str(folder)}},
        headers=live.hdr,
    )
    assert r.status_code == 202 and _settled(live, cid)["state"] == "done"
    assert str(folder) in projects.load()[pid].folders
    cid, _ = _propose_project(live, ai, "remove_folder")
    r = live.c.post(
        f"/api/ask/proposals/{_card(live, cid)['action_id']}/decide",
        json={"decision": "confirm", "inputs": {"folder": str(folder)}},
        headers=live.hdr,
    )
    assert r.status_code == 202 and _settled(live, cid)["state"] == "done"
    assert str(folder) not in projects.load()[pid].folders


# ---- sessions: the reads and an end-to-end session operation ------------------------------------

S_A = "claude:11111111-1111-1111-1111-111111111111"
S_B = "claude:22222222-2222-2222-2222-222222222222"


def _listed(a, ai, list_args: dict | None = None, then: tuple | None = None):
    script = ai(
        call("list_sessions", list_args or {}, cid="l"),
        *([call(then[0], then[1], cid="p")] if then else []),
        answer("done"),
    )
    cid = a.new()
    r = a.ask(cid, "list my sessions")
    assert r.status_code == 200, r.text
    return cid, script


def test_list_sessions_is_the_sidebar_list_and_never_a_path(live, ai, fake_jsonl):
    _cid, script = _listed(live, ai)
    raw = script.tool_results(1)[0]["content"]
    rows = json.loads(raw)["sessions"]
    keys = {r["key"] for r in rows}
    assert {S_A, S_B} <= keys
    assert "/home/user" not in raw and "/tmp/other" not in raw
    assert all(r["archived"] is False for r in rows)


def test_list_sessions_archived_lists_the_archive(live, ai, fake_jsonl):
    _cid, script = _listed(live, ai, {"archived": True})
    rows = json.loads(script.tool_results(1)[0]["content"])["sessions"]
    assert [r["archived"] for r in rows] and all(r["archived"] for r in rows)


def test_a_session_rename_from_the_list_runs_the_rename_route(live, ai, fake_jsonl):
    from agent_sessions import metadata

    cid, script = _listed(
        live,
        ai,
        then=("propose_session_change", {"action": "rename", "key": S_A, "title": "renamed"}),
    )
    assert _result(script)["proposed"] is True
    card = _card(live, cid)
    assert card["op"] == "session.rename" and card["text"] == "Renames it to “renamed”."
    assert _confirm(live, card["action_id"]).status_code == 202
    assert _settled(live, cid)["state"] == "done"
    assert metadata.get(S_A).title == "renamed"


def test_a_session_archive_moves_it_to_the_archive(live, ai, fake_jsonl):
    cid, _ = _listed(live, ai, then=("propose_session_change", {"action": "archive", "key": S_B}))
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "done", card
    r = live.c.get(f"/api/sessions/{S_B}")
    assert r.status_code == 200 and r.json()["archived"] is True


def test_a_key_no_tool_returned_this_turn_is_refused(live, ai, fake_jsonl):
    script = ai(
        call("propose_session_change", {"action": "archive", "key": S_A}, cid="p"),
        answer("ok"),
    )
    cid = live.new()
    live.ask(cid, "archive it")
    assert "refused" in _result(script, 1)["error"]
    assert ask_store.read_proposals(cid) == []


def test_session_brief_reads_the_session(live, ai, fake_jsonl):
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    assert brief["key"] == S_A and brief["archived"] is False


def test_list_folders_names_folders_and_never_paths(live, ai, fake_jsonl):
    script = ai(call("list_folders", {}, cid="f"), answer("ok"))
    live.ask(live.new(), "folders?")
    raw = script.tool_results(1)[0]["content"]
    names = {f["name"] for f in json.loads(raw)["folders"]}
    assert "demoapp.io" in names
    assert "/" not in raw


# ---- #1571 review: the card previews and binds exactly what the route will act on --------------


def _confirm_with(a, cid: str, folder: str):
    return a.c.post(
        f"/api/ask/proposals/{_card(a, cid)['action_id']}/decide",
        json={"decision": "confirm", "inputs": {"folder": folder}},
        headers=a.hdr,
    )


def test_project_archive_previews_and_walks_one_population(live, ai, fake_jsonl, monkeypatch):
    """The card's count is the route's own membership (a fresh, unscoped resolve), not the
    sidebar's scoped, cached `session_count` — both go through `project_members`."""
    from agent_sessions.routes import sessions as session_routes

    pid = _project(live, "scratch")
    projects.update(pid, folders=["/home/user/claude/repo/a"])
    seen: list[list[str]] = []
    real = session_routes.project_members

    def recorded(p, index):
        out = real(p, index)
        seen.append(sorted(k for _s, k, _m in out))
        return out

    monkeypatch.setattr(session_routes, "project_members", recorded)
    # Out of the sidebar's scope (an excluded folder): its count says 0, the route still walks 2.
    prefs.set_folder_exclusions(["/home/user/claude/repo/a"])
    (row,) = live.c.get("/api/projects").json()["projects"]
    assert row["session_count"] == 0
    cid, _ = _propose_project(live, ai, "archive")
    assert "its 2 sessions" in _card(live, cid)["text"]
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "done", card
    # Preview, the tap's re-read and the route itself all walked the same members.
    assert len(seen) >= 3 and all(s == seen[0] for s in seen) and seen[0] == sorted([S_A, S_B])


def test_an_equal_size_member_swap_makes_the_archive_card_stale(live, ai, monkeypatch):
    from agent_sessions.routes import sessions as session_routes

    pid = _project(live, "scratch")
    members = [(None, S_A, None)]
    monkeypatch.setattr(session_routes, "project_members", lambda p, index: list(members))
    cid, _ = _propose_project(live, ai, "archive")
    members[:] = [(None, S_B, None)]  # A left, B joined: still one member
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "sessions it covers changed" in card["reason"]
    assert not projects.load()[pid].archived  # nothing ran


def test_archive_older_binds_the_routes_candidates_not_a_count(live, ai, fake_jsonl, monkeypatch):
    from agent_sessions import engines
    from agent_sessions.routes import sessions as session_routes

    first, second = [
        s for s in engines.scan_all() if engines.session_key(s) in (S_A, S_B)
    ]  # fmt: skip
    candidates = [first]
    monkeypatch.setattr(session_routes, "archive_older_candidates", lambda cutoff: candidates)
    script = ai(
        call("propose_session_change", {"action": "archive_older", "days": 3}, cid="p"),
        answer("Proposed."),
    )
    cid = live.new()
    assert live.ask(cid, "archive old sessions").status_code == 200
    assert "(1 now)" in _card(live, cid)["text"], _result(script, 1)
    candidates[:] = [second]  # one session, but not the one the card counted
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "sessions it covers changed" in card["reason"]
    assert not any(engines.session_key(s) == S_B and s.archived for s in engines.scan_all())


def test_archive_older_route_sweeps_exactly_the_shared_candidates(live, fake_jsonl, monkeypatch):
    from agent_sessions.routes import sessions as session_routes

    monkeypatch.setattr(session_routes, "archive_older_candidates", lambda cutoff: [])
    r = live.c.post("/api/sessions/archive-older", json={"hours": 1}, headers=live.hdr)
    assert r.status_code == 200 and r.json()["archived"] == 0


def test_the_folder_precondition_refuses_a_stale_list_and_writes_nothing(live):
    pid = _project(live, "scratch")
    projects.update(pid, folders=["/srv/a"])
    r = live.c.patch(
        f"/api/projects/{pid}",
        json={"folders": ["/srv/b"], "if_folders": []},
        headers=live.hdr,
    )
    assert r.status_code == 409 and "folders changed" in r.json()["detail"]
    assert list(projects.load()[pid].folders) == ["/srv/a"]
    r = live.c.patch(
        f"/api/projects/{pid}",
        json={"folders": ["/srv/a", "/srv/b"], "if_folders": ["/srv/a/"]},
        headers=live.hdr,
    )
    assert r.status_code == 200 and list(projects.load()[pid].folders) == ["/srv/a", "/srv/b"]


def test_a_folder_change_racing_the_patch_is_refused_never_lost(live, ai, monkeypatch):
    """Two confirms both read [A]; the other's write lands between this one's read and its PATCH.
    The PATCH's `if_folders` makes the route refuse: the other's folder is never dropped."""
    from agent_sessions import asgi_call

    pid = _project(live, "scratch")
    projects.update(pid, folders=["/srv/a"])
    cid, _ = _propose_project(live, ai, "add_folder")
    real = asgi_call.call

    async def racing(app, operator, method, path, body, **kw):
        if method == "PATCH":
            projects.update(pid, folders=["/srv/a", "/srv/c"])  # the other confirm's write
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", racing)
    assert _confirm_with(live, cid, "/srv/b").status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "failed" and "folders changed" in card["summary"], card
    assert list(projects.load()[pid].folders) == ["/srv/a", "/srv/c"]


def test_a_same_count_folder_replacement_makes_the_default_card_stale(live, ai):
    pid = _project(live, "scratch")
    projects.update(pid, folders=["/srv/a", "/srv/x"])
    cid, _ = _propose_project(live, ai, "default_folder")
    assert _card(live, cid)["choices"]["folder"] == ["/srv/a", "/srv/x"]
    projects.update(pid, folders=["/srv/b", "/srv/x"])  # A deliberately replaced by B
    assert _confirm_with(live, cid, "/srv/a").status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "folders changed" in card["reason"]
    assert "/srv/a" not in projects.load()[pid].folders  # never re-adopted


def test_a_picked_folder_the_project_no_longer_has_is_stale_not_released(live, ai, monkeypatch):
    """Even past the set check (simulated), a folder that is not one of the project's is never
    'released' (a no-op called Done) nor set as default (the store would re-adopt it)."""
    pid = _project(live, "scratch")
    projects.update(pid, folders=["/srv/a"])
    for action in ("remove_folder", "default_folder"):
        cid, _ = _propose_project(live, ai, action)
        assert _confirm_with(live, cid, "/srv/gone").status_code == 202
        card = _settled(live, cid)
        assert card["state"] == "stale" and "no longer one of its folders" in card["reason"]
    assert list(projects.load()[pid].folders) == ["/srv/a"]


def test_a_success_too_large_to_read_back_is_unconfirmed_never_failed(live, ai, monkeypatch):
    from agent_sessions import asgi_call

    pid = _project(live, "scratch")
    cid, _ = _propose_project(live, ai, "archive")
    real = asgi_call.call

    async def oversized(app, operator, method, path, body, **kw):
        r = await real(app, operator, method, path, body, **kw)
        if method == "POST" and path.endswith("/archive"):
            return asgi_call.Response(status=r.status, body=None, truncated=True)
        return r

    monkeypatch.setattr(asgi_call, "call", oversized)
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "unconfirmed" and "can't say what changed" in card["summary"], card
    assert projects.load()[pid].archived  # it did run


# ---- #1571 review 6742 -----------------------------------------------------------------------


def test_the_set_digest_is_framed_so_no_two_folder_sets_collide():
    assert ask_operations._digest(["/a", "/b\n/c"]) != ask_operations._digest(["/a\n/b", "/c"])
    assert ask_operations._digest(["/b", "/a"]) == ask_operations._digest(["/a", "/b"])


def test_a_newline_reframed_folder_set_is_stale_through_the_confirm(live, ai):
    pid = _project(live, "scratch")
    projects.update(pid, folders=["/srv/a", "/srv/b\n/srv/c"])
    cid, _ = _propose_project(live, ai, "default_folder")
    projects.update(pid, folders=["/srv/a\n/srv/b", "/srv/c"])  # same joined text, other set
    assert _confirm_with(live, cid, "/srv/c").status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "folders changed" in card["reason"]
    assert projects.load()[pid].default_folder != "/srv/c"  # the stale card's pick never ran


def test_the_delete_card_discloses_the_shared_memory_and_its_failure_never_says_nothing_changed(
    app, ai
):
    _project(app, "scratch")
    cid, _ = _propose_project(app, ai, "delete")
    assert "shared memory" in _card(app, cid)["text"]
    said = ask_operations.CATALOG["project.delete"].failure(500, "disk full", {})
    assert "shared memory may already have been removed" in said and "Nothing changed" not in said


def _propose_move(a, ai, *, project_index: int = 0):
    ai(
        calls(("list_sessions", {}), ("list_projects", {})),
        call(
            "propose_session_change",
            {"action": "move", "key": S_A, "project_index": project_index},
            cid="p",
        ),
        answer("Proposed."),
    )
    cid = a.new()
    assert a.ask(cid, "move it").status_code == 200
    return cid


def test_a_move_whose_destination_was_renamed_is_stale(live, ai, fake_jsonl):
    from agent_sessions import metadata

    pid = _project(live, "web")
    cid = _propose_move(live, ai)
    assert "Moves it to web" in _card(live, cid)["text"]
    projects.update(pid, name="renamed-meanwhile")
    assert _confirm(live, _card(live, cid)["action_id"]).status_code == 202
    card = _settled(live, cid)
    assert card["state"] == "stale" and "destination project changed" in card["reason"]
    assert (metadata.load().get(S_A) or metadata.SessionMeta()).project_id != pid


def test_list_sessions_filters_by_agent_and_mission_through_the_route(
    live, ai, fake_jsonl, monkeypatch
):
    from agent_sessions import asgi_call

    queries: list[str] = []
    real = asgi_call.call

    async def recorded(app, operator, method, path, body, **kw):
        if path == "/api/sessions":
            queries.append(kw.get("query", ""))
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", recorded)
    script = ai(
        calls(("list_agents", {})),
        calls(
            ("list_sessions", {"agent_index": 0, "mission_id": "none"}),
            ("list_sessions", {"mission_id": "m-not-returned"}),
            ("list_sessions", {"agent_index": 99}),
        ),
        answer("done"),
    )
    live.ask(live.new(), "list them")
    agents = json.loads(script.tool_results(1)[0]["content"])["agents"]
    from agent_sessions.engines import registry

    first = next(p for p in registry.present_providers() if registry.is_agent(p))
    assert agents[0]["index"] == 0 and len(queries) == 1
    assert f"engine={first.engine_id}" in queries[0] and "mission=none" in queries[0]
    # The request after round 2 carries round 1's result too: the three list_sessions are last.
    results = [json.loads(m["content"]) for m in script.tool_results(2)[-3:]]
    assert "sessions" in results[0]
    assert "refused" in json.dumps(results[1]) and "refused" in json.dumps(results[2])


def test_session_brief_adds_bounded_history_as_untrusted_data(live, ai, fake_jsonl):
    (path,) = fake_jsonl.rglob("11111111-1111-1111-1111-111111111111.jsonl")
    reply = {"type": "assistant", "message": {"role": "assistant", "content": "M001 \x1b[31mred"}}
    with path.open("a") as f:
        f.write(json.dumps(reply) + "\n")
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 50}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    assert "M001" in brief["untrusted_history"]
    assert "\x1b" not in brief["untrusted_history"]
    assert brief["history_has_more"] in (True, False)
    for bad in (0, 201, True, "10"):
        _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": bad}))
        assert (
            "refused" in script.tool_results(2)[-1]["content"]
            or "history_lines" in (script.tool_results(2)[-1]["content"])
        )


def test_list_sessions_names_agents_in_authored_words_only(live, ai, fake_jsonl, monkeypatch):
    """#1569 review 6739 applies here too: a loaded manifest's display name never reaches the
    model — `list_sessions` names an agent by `ask_settings.agent_labels()` alone."""
    from test_ask_reads import SEGMENTED, _display_names

    _display_names(monkeypatch, {"claude": SEGMENTED + "Name"})
    script = ai(call("list_sessions", {}, cid="l"), answer("done"))
    live.ask(live.new(), "list my sessions")
    sent = json.dumps(script.bodies)
    assert SEGMENTED not in sent
    rows = json.loads(script.tool_results(1)[0]["content"])["sessions"]
    assert rows and {r["agent"] for r in rows} == {"Claude Code"}


def _history_with(fake_jsonl, text: str) -> None:
    (path,) = fake_jsonl.rglob("11111111-1111-1111-1111-111111111111.jsonl")
    reply = {"type": "assistant", "message": {"role": "assistant", "content": text}}
    with path.open("a") as f:
        f.write(json.dumps(reply) + "\n")


def test_history_that_looks_like_a_credential_never_reaches_the_endpoint(live, ai, fake_jsonl):
    """#1571 review 6745: older terminal lines are where a typed password sits. Route to model:
    the whole page is withheld, and the canary is in no request the AI endpoint was sent."""
    _history_with(fake_jsonl, "M001 export DB_PASSWORD=hunter2-CANARY-7731")
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 50}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    # The turn that looks like a credential is withheld WHOLE; the rest of the history stays.
    assert ask_store.WITHHELD in brief["untrusted_history"]
    assert "first message on repo-a" in brief["untrusted_history"]
    assert "hunter2-CANARY-7731" not in json.dumps(script.bodies)


def test_a_known_secret_in_history_is_redacted_before_the_endpoint(live, ai, fake_jsonl):
    """Two nets hold here: `guard_text` on the page and `review._post_chat`'s own redaction at the
    transport. This pins the end result, not either net alone (the credential test above does)."""
    from agent_sessions import template_secrets

    template_secrets.register_typed(["plainwords-canary-value"])
    _history_with(fake_jsonl, "M001 the deploy note says plainwords-canary-value here")
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 50}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    assert "M001" in brief["untrusted_history"]
    assert "plainwords-canary-value" not in json.dumps(script.bodies)


def test_history_is_refused_when_the_secret_store_cannot_redact(live, ai, fake_jsonl, monkeypatch):
    from agent_sessions import template_secrets

    real = template_secrets.redact_text

    def unavailable(value, *a, **kw):
        # Only for the history page: the rest of the turn still stores and runs.
        if "M001" in str(value):
            raise template_secrets.RedactionUnavailable("key unreadable")
        return real(value, *a, **kw)

    monkeypatch.setattr(template_secrets, "redact_text", unavailable)
    _history_with(fake_jsonl, "M001 anything")
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 50}))
    raw = script.tool_results(2)[-1]["content"]
    assert "could not be checked for secrets" in raw and "M001" not in raw


SECRET = "alpha-bravo-charlie-delta-canary"  # plain words: only the known-secret net catches it


def _no_run_of(secret: str, text: str, w: int = 6) -> bool:
    return not any(secret[i : i + w] in text for i in range(len(secret) - w + 1))


def _turns(monkeypatch, *texts: str) -> None:
    """`history.load_turns` answers these assistant turns, oldest first."""
    from agent_sessions import history
    from agent_sessions.transcript import Turn

    turns = [Turn("assistant", t) for t in texts]
    monkeypatch.setattr(history, "load_turns", lambda engine, native, home=None: list(turns))


@pytest.mark.parametrize("lines", [1, 2, 3, 5, 50])
@pytest.mark.parametrize(
    "texts",
    [
        # Hermes' review 6748 probe: one long line, the secret after 480 characters of filler.
        ("x" * 480 + SECRET,),
        # Review 6746: the secret at character zero of a page longer than the character cap.
        (SECRET + "x" * 6_001,),
        # Across lines and turns: any line cut lands after the redaction.
        ("first line\n" + SECRET + "\nthird line", "next turn\nmore"),
        ("a" * 3_000 + "\n" + SECRET + "b" * 3_100,),
    ],
)
def test_no_cut_ever_leaves_a_run_of_a_known_secret(monkeypatch, texts, lines):
    """#1571 reviews 6746 and 6748: every turn is guarded whole BEFORE any line or character cut,
    so no cut — HQ's own or the line count's — can split a secret past the redactor."""
    from agent_sessions import ask_orchestrator, template_secrets

    template_secrets.register_typed([SECRET])
    _turns(monkeypatch, *texts)
    text, _more = ask_orchestrator._history_text(S_A, lines)
    assert _no_run_of(SECRET, text)


def test_guarding_each_turn_whole_holds_on_its_own(monkeypatch):
    """Each net alone: with the last check switched off, the per-turn guard still redacts a secret
    every cut would otherwise have split."""
    from agent_sessions import ask_orchestrator, template_secrets

    template_secrets.register_typed([SECRET])
    monkeypatch.setattr(ask_orchestrator, "_holds_secret_fragment", lambda text, secrets: False)
    for page in (
        "x" * 480 + SECRET,  # the whole secret on the newest line
        SECRET + "x" * 5_990,  # the character cap lands inside it
    ):
        _turns(monkeypatch, page)
        for lines in (1, 2, 200):
            text, _more = ask_orchestrator._history_text(S_A, lines)
            assert _no_run_of(SECRET, text), (page[:12], lines)


def test_a_transcript_holding_part_of_a_known_secret_is_withheld(monkeypatch):
    """What the redactor cannot match — a transcript that holds only part of a secret — the last
    check withholds."""
    from agent_sessions import ask_orchestrator, template_secrets

    template_secrets.register_typed([SECRET])
    _turns(monkeypatch, "it printed " + SECRET[5:] + " and stopped")
    assert ask_orchestrator._history_text(S_A, 10)[0] == ask_store.WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        SECRET[1:] + " and then some",
        "alpha-bravo-char\nlie-delta-canary",  # a wrap split it
        "... charlie-delta-canary",  # half of it
    ],
)
def test_any_run_of_a_known_secret_withholds_the_page(text):
    from agent_sessions import ask_orchestrator

    assert ask_orchestrator._holds_secret_fragment(text, [SECRET])


def test_text_without_a_run_of_a_known_secret_passes():
    from agent_sessions import ask_orchestrator

    assert not ask_orchestrator._holds_secret_fragment(
        "alpha team, delta wing: all [secret]", [SECRET]
    )


def test_history_lines_take_the_newest_and_say_whether_more_remain(monkeypatch):
    from agent_sessions import ask_orchestrator

    _turns(monkeypatch, "old one", "line a\nline b\nline c")
    text, more = ask_orchestrator._history_text(S_A, 2)
    assert text == "line b\nline c" and more is True
    text, more = ask_orchestrator._history_text(S_A, 50)
    assert text.splitlines() == ["[assistant] old one", "[assistant] line a", "line b", "line c"]
    assert more is False


def test_a_known_secret_on_a_long_line_never_reaches_the_endpoint(live, ai, fake_jsonl):
    """Route to model, Hermes' review 6748 case end to end: a 32-character registered secret after
    480 characters on one line, one line of history asked for."""
    from agent_sessions import template_secrets

    template_secrets.register_typed([SECRET])
    _history_with(fake_jsonl, "x" * 480 + SECRET)
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 1}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    assert "untrusted_history" in brief
    assert _no_run_of(SECRET, json.dumps(script.bodies))


def test_history_carries_tool_calls_and_their_output(monkeypatch):
    """#1559's promise (review 6754): "what did the last test run report?" is answerable — tool
    calls and their output are in the history, guarded like every other turn."""
    from agent_sessions import ask_orchestrator, history
    from agent_sessions.transcript import Turn

    turns = [
        Turn("user", "run the tests"),
        Turn("assistant", "Bash(make test)", "tool"),
        Turn("tool", "FAILED tests/test_x.py::test_y - AssertionError", "result"),
        Turn("assistant", "One test fails."),
    ]
    monkeypatch.setattr(history, "load_turns", lambda engine, native, home=None: list(turns))
    text, _more = ask_orchestrator._history_text(S_A, 50)
    assert text.splitlines() == [
        "[you] run the tests",
        "[assistant] Bash(make test)",
        "[tool] FAILED tests/test_x.py::test_y - AssertionError",
        "[assistant] One test fails.",
    ]


def test_a_secret_cut_by_the_adapter_never_reaches_the_endpoint(live, ai, fake_jsonl):
    """Adapter → session_brief → model request: a registered secret in a Bash argument, starting
    after 45 characters (the adapter keeps 60), and one straddling a tool result's cut."""
    from agent_sessions import template_secrets

    template_secrets.register_typed([SECRET])
    (path,) = fake_jsonl.rglob("11111111-1111-1111-1111-111111111111.jsonl")
    call_rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 45 + SECRET}},
            ],
        },
    }
    result_rec = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "y" * 230 + SECRET}],
        },
    }
    with path.open("a") as f:
        f.write(json.dumps(call_rec) + "\n" + json.dumps(result_rec) + "\n")
    _cid, script = _listed(live, ai, then=("session_brief", {"key": S_A, "history_lines": 50}))
    brief = json.loads(script.tool_results(2)[-1]["content"])
    history_text = brief["untrusted_history"]
    assert "first message on repo-a" in history_text
    # The call and its output are there — the secret in each redacted before any cut.
    assert "Bash(" in history_text and "[secret]" in history_text and "yyyy" in history_text
    assert _no_run_of(SECRET, json.dumps(script.bodies), w=4)


def test_the_terminal_scroll_up_is_unchanged_and_the_guard_ends_with_the_read(
    live, fake_jsonl, monkeypatch
):
    """The pre-cut guard holds for HQ's read only: the adapter outside it shows a tool argument as
    written, before and after an HQ read (a ContextVar, reset in `finally`)."""
    from agent_sessions import ask_orchestrator, template_secrets, transcript

    template_secrets.register_typed([SECRET])
    (path,) = fake_jsonl.rglob("11111111-1111-1111-1111-111111111111.jsonl")
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls " + SECRET}}],
        },
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    native = S_A.split(":", 1)[1]

    def tool_line():
        turns = transcript.adapter_for("claude")(native, fake_jsonl)
        return next(t.text for t in turns if t.kind == "tool")

    assert SECRET[:20] in tool_line()  # the terminal's own read: as written (cut at 60)
    text, _more = ask_orchestrator._history_text(S_A, 50)
    assert "Bash(ls [secret])" in text
    assert transcript.PRE_CUT_GUARD.get() is None
    assert SECRET[:20] in tool_line()  # and again after HQ's read
