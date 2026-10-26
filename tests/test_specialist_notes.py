"""Specialist notes (#1693 S3, D21): a specialist's own notebook FOR ONE PROJECT, as its own memory
scope ``<project>~<specialist>`` that follows its project — bound only while the project's memory
is on, never into another project's session, and gone with the project.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    agent_tools,
    memory,
    memory_mcp,
    native_worker,
    projects,
    template_secrets,
)

KEY = "fixture:" + "a" * 8
NOTE = (
    "---\nname: Run the slow suite last\ntype: finding\n---\n\n"
    "In this repo the e2e suite takes 20 min.\n"
)


@pytest.fixture(autouse=True)
def _settled(monkeypatch):
    monkeypatch.setattr(memory, "SETTLE_S", 0.0)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


def _project(tmp_path, name: str):
    folder = tmp_path / name
    folder.mkdir()
    return projects.create(name, folders=[str(folder)]).id, folder


def _drop(inbox: str, name: str, text: str):
    path = Path(inbox) / name
    path.write_text(text)
    old = path.stat().st_mtime - 10
    os.utime(path, (old, old))


def _notes_in(scope: str) -> list[str]:
    conn = sqlite3.connect(memory.root() / scope / "state.db")
    try:
        return [r[0] for r in conn.execute("SELECT slug FROM notes ORDER BY slug")]
    finally:
        conn.close()


# ---- the launch contract ------------------------------------------------------------------------


def test_a_specialist_launch_gets_its_own_notebook_for_this_project(tmp_path):
    pid, folder = _project(tmp_path, "alpha")
    memory.set_enabled(pid, True)
    env = memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    assert set(env) == set(memory.ALL_ENV_KEYS)
    scope = memory.specialist_scope(pid, "implementer")
    assert env[memory.ENV_SPECIALIST_READ] == str(memory.root() / scope / "notes")
    assert Path(env[memory.ENV_SPECIALIST_INBOX]).is_dir()
    assert env[memory.ENV_READ] != env[memory.ENV_SPECIALIST_READ]
    # The FIRST launch already finds the notebook's index where it was told to read (no pass yet).
    index = (Path(env[memory.ENV_SPECIALIST_READ]) / "MEMORY.md").read_text()
    assert f"${memory.ENV_SPECIALIST_INBOX}" in index
    # Without a specialist, the same launch is exactly the project's two paths.
    assert set(memory.launch_env(KEY, str(folder), kind="terminal")) == set(memory.ENV_KEYS)


def test_the_memory_opt_in_holds_for_specialist_notes(tmp_path):
    pid, folder = _project(tmp_path, "alpha")
    scope = memory.specialist_scope(pid, "implementer")
    assert memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer") == {}
    assert not (memory.root() / scope).exists()  # never on: nothing written
    # On, then off again: the project's scope exists but is switched off. A first specialist
    # launch now must not create (or bind) a notebook either.
    memory.set_enabled(pid, True)
    memory.set_enabled(pid, False)
    assert memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer") == {}
    assert not (memory.root() / scope).exists()


def test_notes_never_cross_projects(tmp_path):
    a, folder_a = _project(tmp_path, "alpha")
    b, folder_b = _project(tmp_path, "beta")
    memory.set_enabled(a, True)
    memory.set_enabled(b, True)
    env_a = memory.launch_env(KEY, str(folder_a), kind="terminal", specialist="implementer")
    _drop(env_a[memory.ENV_SPECIALIST_INBOX], "slow-suite.md", NOTE)
    memory.ingest(memory.specialist_scope(a, "implementer"))
    assert _notes_in(memory.specialist_scope(a, "implementer")) == ["run-the-slow-suite-last"]
    # The same specialist in project B: B's own notebook, which knows nothing from A.
    env_b = memory.launch_env(
        "fixture:" + "b" * 8, str(folder_b), kind="terminal", specialist="implementer"
    )
    assert memory.specialist_scope(b, "implementer") in env_b[memory.ENV_SPECIALIST_READ]
    assert memory.specialist_scope(a, "implementer") not in "".join(env_b.values())
    memory.publish(memory.specialist_scope(b, "implementer"))
    assert "slow" not in (Path(env_b[memory.ENV_SPECIALIST_READ]) / "MEMORY.md").read_text().lower()


def test_specialist_notes_are_ingested_into_their_own_notebook_and_quarantined_like_any_note(
    tmp_path, monkeypatch
):
    pid, folder = _project(tmp_path, "alpha")
    memory.set_enabled(pid, True)
    env = memory.launch_env(KEY, str(folder), kind="terminal", specialist="reviewer")
    scope = memory.specialist_scope(pid, "reviewer")
    _drop(env[memory.ENV_SPECIALIST_INBOX], "slow-suite.md", NOTE)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["hunter2-super-secret"])
    _drop(
        env[memory.ENV_SPECIALIST_INBOX],
        "creds.md",
        "---\nname: Login\n---\n\npassword hunter2-super-secret\n",
    )
    counts = memory.ingest(scope)
    assert counts["notes"] == 1 and counts["quarantined"] == 1
    assert _notes_in(scope) == ["run-the-slow-suite-last"]
    assert _notes_in(pid) == []  # the project's notebook took nothing in
    view = memory.view(scope)
    assert view["specialist"] == "reviewer" and len(view["quarantine"]) == 1


# ---- following the project ----------------------------------------------------------------------


def test_specialist_notebooks_follow_the_project_switch(tmp_path):
    pid, folder = _project(tmp_path, "alpha")
    memory.set_enabled(pid, True)
    env = memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    scope = memory.specialist_scope(pid, "implementer")
    memory.set_enabled(pid, False)
    assert memory.enabled(scope) is False
    _drop(env[memory.ENV_SPECIALIST_INBOX], "slow-suite.md", NOTE)
    assert memory.ingest(scope) == {"skipped": "off"}
    assert memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer") == {}
    memory.set_enabled(pid, True)
    assert memory.enabled(scope) is True
    assert set(
        memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    ) == set(memory.ALL_ENV_KEYS)


def test_deleting_a_project_deletes_its_specialist_notebooks_and_nothing_else(tmp_path):
    abc, folder = _project(tmp_path, "abc")
    other, other_folder = _project(tmp_path, "abc-def")  # an id that merely starts the same way
    for pid, f in ((abc, folder), (other, other_folder)):
        memory.set_enabled(pid, True)
        memory.launch_env(KEY, str(f), kind="terminal", specialist="implementer")
    memory.delete_scope(abc)
    names = sorted(os.listdir(memory.root()))
    assert abc not in names and memory.specialist_scope(abc, "implementer") not in names
    assert other in names and memory.specialist_scope(other, "implementer") in names
    assert not any(n.startswith(".trash-") for n in names)


def test_scope_ids_are_validated(tmp_path):
    pid, _ = _project(tmp_path, "alpha")
    for bad in (f"{pid}~", f"{pid}~Bad", f"{pid}~a~b", "~implementer", f"{pid}/../x~y"):
        with pytest.raises(memory.MemoryStoreError):
            memory.view(bad)
    with pytest.raises(memory.MemoryStoreError):
        memory.set_enabled(memory.specialist_scope(pid, "implementer"), True)  # project-only


# ---- the tool -----------------------------------------------------------------------------------


@pytest.fixture
def bound(tmp_path):
    pid, folder = _project(tmp_path, "alpha")
    memory.set_enabled(pid, True)
    env = memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    return pid, env


def _call(server, name, args):
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    return reply["result"]["isError"], reply["result"]["content"][0]["text"]


def test_the_tool_writes_and_reads_the_chosen_notebook(bound):
    pid, env = bound
    s = memory_mcp.Server(
        env[memory.ENV_READ],
        env[memory.ENV_INBOX],
        env[memory.ENV_SPECIALIST_READ],
        env[memory.ENV_SPECIALIST_INBOX],
    )
    err, _ = _call(
        s,
        "memory_write",
        {"title": "Run the slow suite last", "body": "20 min", "notebook": "mine"},
    )
    assert not err
    assert os.listdir(env[memory.ENV_SPECIALIST_INBOX]) == ["run-the-slow-suite-last.md"]
    assert os.listdir(env[memory.ENV_INBOX]) == []
    memory.ingest(memory.specialist_scope(pid, "implementer"))
    err, text = _call(s, "memory_read", {"slug": "run-the-slow-suite-last", "notebook": "mine"})
    assert not err and "20 min" in text
    err, _ = _call(s, "memory_read", {"slug": "run-the-slow-suite-last"})  # not in the project's
    assert err
    _, index = _call(s, "memory_index", {})
    assert "Your notes as this specialist" in index and "Run the slow suite last" in index
    _, found = _call(s, "memory_search", {"query": "slow suite"})
    assert "[mine] run-the-slow-suite-last" in found


def test_without_a_specialist_notebook_the_tool_is_unchanged(bound):
    _pid, env = bound
    s = memory_mcp.Server(env[memory.ENV_READ], env[memory.ENV_INBOX])
    err, text = _call(s, "memory_write", {"title": "x", "body": "y", "notebook": "mine"})
    assert err and "'project'" in text
    tools = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert tools == memory_mcp.TOOLS
    with_mine = memory_mcp.Server(
        env[memory.ENV_READ],
        env[memory.ENV_INBOX],
        env[memory.ENV_SPECIALIST_READ],
        env[memory.ENV_SPECIALIST_INBOX],
    )
    schema = {t["name"]: t for t in with_mine.tools()}
    assert schema["memory_write"]["inputSchema"]["properties"]["notebook"]["enum"] == [
        "project",
        "mine",
    ]


def test_main_refuses_half_a_specialist_notebook(monkeypatch, bound):
    _pid, env = bound
    monkeypatch.setenv(memory.ENV_READ, env[memory.ENV_READ])
    monkeypatch.setenv(memory.ENV_INBOX, env[memory.ENV_INBOX])
    monkeypatch.setenv(memory.ENV_SPECIALIST_READ, env[memory.ENV_SPECIALIST_READ])
    monkeypatch.delenv(memory.ENV_SPECIALIST_INBOX, raising=False)
    assert memory_mcp.main() == 2


def test_the_launch_seams_carry_all_four_paths_and_refuse_a_partial_set(bound):
    _pid, env = bound
    server = agent_tools.server(env)
    assert server is not None and set(server.env) >= set(memory.ALL_ENV_KEYS)
    partial = {k: env[k] for k in (*memory.ENV_KEYS, memory.ENV_SPECIALIST_READ)}
    assert agent_tools.server(partial) is None
    assert native_worker.memory_environment({"memory_env": env}) == env
    with pytest.raises(native_worker.WorkerError):
        native_worker.memory_environment({"memory_env": partial})


# ---- the page -----------------------------------------------------------------------------------


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


def test_the_page_lists_a_projects_specialist_notebooks_and_opens_one(auth_cfg, bound):
    pid, env = bound
    c = _client(auth_cfg)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    view = c.get(f"/api/memory/{pid}").json()
    scope = memory.specialist_scope(pid, "implementer")
    assert [n["scope"] for n in view["notebooks"]] == [scope]
    assert view["notebooks"][0]["specialist"] == "implementer"
    mine = c.get(f"/api/memory/{scope}").json()
    assert mine["specialist"] == "implementer" and mine["notebooks"] == []
    assert c.post(f"/api/memory/{scope}/ingest", headers=hdr).status_code == 200
    # The switch is the project's: a specialist notebook has none of its own.
    assert (
        c.put(f"/api/memory/{scope}/enabled", json={"enabled": False}, headers=hdr).status_code
        == 404
    )


# ---- review-driven regressions (#1696) -----------------------------------------------------------


def test_one_broken_notebook_never_blocks_the_project_switch_and_none_drifts(tmp_path):
    """No copied flag: a notebook reads its project's switch, so a corrupt notebook neither fails
    the switch nor stays on behind it."""
    pid, folder = _project(tmp_path, "alpha")
    memory.set_enabled(pid, True)
    memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    env = memory.launch_env(KEY, str(folder), kind="terminal", specialist="zeta")
    (memory.root() / memory.specialist_scope(pid, "implementer") / "state.db").write_bytes(
        b"garbage" * 99
    )
    memory.set_enabled(pid, False)  # must not raise
    zeta = memory.specialist_scope(pid, "zeta")
    assert memory.enabled(zeta) is False
    _drop(env[memory.ENV_SPECIALIST_INBOX], "slow-suite.md", NOTE)
    assert memory.ingest(zeta) == {"skipped": "off"}
    memory.set_enabled(pid, True)
    assert memory.enabled(zeta) is True and memory.ingest(zeta)["notes"] == 1


def test_a_notebooks_index_points_at_its_own_inbox(bound):
    pid, env = bound
    memory.publish(memory.specialist_scope(pid, "implementer"))
    index = (Path(env[memory.ENV_SPECIALIST_READ]) / "MEMORY.md").read_text()
    assert f"${memory.ENV_SPECIALIST_INBOX}" in index
    assert f"`${memory.ENV_INBOX}`" not in index
    project_index = (Path(env[memory.ENV_READ]) / "MEMORY.md").read_text()
    assert f"`${memory.ENV_INBOX}`" in project_index


@pytest.mark.parametrize("bad", [["mine"], {}, 7, None])
def test_a_non_string_notebook_is_a_tool_error_and_the_server_keeps_answering(bound, bad):
    _pid, env = bound
    for server in (
        memory_mcp.Server(env[memory.ENV_READ], env[memory.ENV_INBOX]),
        memory_mcp.Server(
            env[memory.ENV_READ],
            env[memory.ENV_INBOX],
            env[memory.ENV_SPECIALIST_READ],
            env[memory.ENV_SPECIALIST_INBOX],
        ),
    ):
        for name, args in (
            ("memory_read", {"slug": "x", "notebook": bad}),
            ("memory_write", {"title": "t", "body": "b", "notebook": bad}),
        ):
            err, _ = _call(server, name, args)
            assert err, (name, bad)
        assert server.handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"] == {}


def test_trash_is_matched_exactly_for_projects_and_notebooks(tmp_path):
    abc, folder = _project(tmp_path, "abc")
    memory.set_enabled(abc, True)
    memory.launch_env(KEY, str(folder), kind="terminal", specialist="implementer")
    root = memory.root()
    # Leftovers of an earlier, partly failed removal of OTHER scopes: never abc's to take.
    foreign = root / ".trash-abc-def-0123abcd"
    foreign.mkdir()
    memory.delete_scope(abc)
    assert foreign.exists()
    # A notebook's own trash IS removed when that notebook is checked for absence.
    scope = memory.specialist_scope("abc-def", "implementer")
    mine = root / f".trash-{scope}-89abcdef"
    mine.mkdir()
    assert memory._scope_absent(scope) is True
    assert not mine.exists() and foreign.exists()


def test_two_large_indexes_each_get_their_share_and_say_when_truncated(bound, monkeypatch):
    _pid, env = bound
    big = "".join(f"- note-{i:04d} — " + "x" * 60 + "\n" for i in range(800))
    for key in (memory.ENV_READ, memory.ENV_SPECIALIST_READ):
        path = Path(env[key]) / "MEMORY.md"
        path.unlink(missing_ok=True)
        path.write_text(big)
    s = memory_mcp.Server(
        env[memory.ENV_READ],
        env[memory.ENV_INBOX],
        env[memory.ENV_SPECIALIST_READ],
        env[memory.ENV_SPECIALIST_INBOX],
    )
    _, text = _call(s, "memory_index", {})
    head, _, mine = text.partition("## Your notes as this specialist")
    assert "index truncated" in head and "index truncated" in mine
    assert mine.count("note-") > 100  # the notebook got its own half, not leftovers
