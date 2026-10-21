"""Shared project memory (#1441): the store, ingestion, and the terminal launch seam.

The headless and API seams are pinned beside their own fixtures, in `test_headless_dispatch.py`
and `test_native_runtime.py`. Each #1198 contract below names its regression.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import memory, metadata, projects, template_secrets

SRC = Path(memory.__file__).parent
# The real inventory, captured before the autouse stub replaces it, for the tests that need it.
_REAL_REDACTION = template_secrets.redaction_values


@pytest.fixture(autouse=True)
def _settled(monkeypatch):
    # Inbox files are taken once they have settled; tests age them instead of sleeping.
    monkeypatch.setattr(memory, "SETTLE_S", 0.0)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture
def pid(workspace):
    return projects.create("demo", folders=[str(workspace)]).id


@pytest.fixture
def on(pid):
    memory.set_enabled(pid, True)
    return pid


def _launch(workspace, key="fixture:" + "a" * 8, kind="terminal"):
    return memory.launch_env(key, str(workspace), kind=kind)


def _drop(env, name, text, *, age=10.0):
    path = Path(env[memory.ENV_INBOX]) / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    old = path.stat().st_mtime - age
    os.utime(path, (old, old))
    return path


def _db(pid):
    conn = sqlite3.connect(memory.root() / pid / "state.db")
    conn.row_factory = sqlite3.Row
    return conn


def _inbox(env):
    """Every file not yet consumed: the launch's inbox plus the scope's claims (a claimed file is
    still waiting until its pass commits)."""
    inbox = Path(env[memory.ENV_INBOX])
    claims = inbox.parent.parent / "claims"
    return sorted([p.name for p in inbox.iterdir()] + [p.name for p in claims.iterdir()])


def _view(env):
    return sorted(p.name for p in Path(env[memory.ENV_READ]).iterdir())


NOTE = "---\nname: Use typed ids\ntype: decision\n---\n\nScopes are project ids, never names.\n"
OTHER_NOTE = "# Other thing\n\nA second, unrelated note.\n"


# ---- the launch contract -------------------------------------------------------------------------


def test_launch_gets_nothing_unless_the_project_has_memory_on(pid, workspace, tmp_path):
    assert _launch(workspace) == {}  # memory off (the default)
    memory.set_enabled(pid, True)
    env = _launch(workspace)
    assert set(env) == set(memory.ENV_KEYS)
    assert Path(env[memory.ENV_READ], "MEMORY.md").is_file()
    assert Path(env[memory.ENV_INBOX]).is_dir()
    memory.set_enabled(pid, False)
    assert _launch(workspace) == {}
    # A folder that belongs to no project entity cannot have memory.
    loose = tmp_path / "loose"
    loose.mkdir()
    assert memory.launch_env("fixture:x", str(loose), kind="terminal") == {}


def test_every_launch_gets_its_own_inbox_and_the_store_stays_outside_the_workspace(on, workspace):
    first, second = _launch(workspace), _launch(workspace)
    assert first[memory.ENV_INBOX] != second[memory.ENV_INBOX]
    assert first[memory.ENV_READ] == second[memory.ENV_READ]
    _drop(first, "a.md", NOTE)
    memory.ingest(on)
    for value in first.values():
        assert not Path(value).resolve().is_relative_to(workspace.resolve())
    assert list(workspace.iterdir()) == []  # nothing is ever written inside a workspace


def test_an_explicit_project_assignment_wins_over_the_folder(on, workspace, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    key = "fixture:" + "b" * 8
    metadata.patch(key, project_id=on)
    env = memory.launch_env(key, str(other), kind="terminal")
    assert env and Path(env[memory.ENV_READ]).parent.name == on


def test_a_broken_store_never_fails_a_launch(on, workspace, monkeypatch):
    monkeypatch.setattr(memory, "_connect", lambda pid: (_ for _ in ()).throw(OSError("disk")))
    assert _launch(workspace) == {}


def test_enabling_an_unknown_or_malformed_project_is_refused(pid):
    with pytest.raises(memory.MemoryStoreError) as e:
        memory.set_enabled("p-00000000", True)
    assert e.value.status == 404
    for bad in ("../" + pid, pid + "/../x", "", "A", "x" * 65, "__default__"):
        with pytest.raises(memory.MemoryStoreError):
            memory.set_enabled(bad, True)
        with pytest.raises(memory.MemoryStoreError):
            memory.notes(bad)


def test_one_project_never_selects_another_projects_scope(on, workspace, tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = projects.create("other", folders=[str(other_dir)]).id
    memory.set_enabled(other, True)
    mine = _launch(workspace)
    theirs = memory.launch_env("fixture:c", str(other_dir), kind="terminal")
    _drop(mine, "mine.md", NOTE)
    memory.ingest(on)
    memory.ingest(other)
    assert [n["slug"] for n in memory.notes(on)] == ["use-typed-ids"]
    assert memory.notes(other) == []
    assert Path(theirs[memory.ENV_READ]).parent.name == other
    assert "use-typed-ids.md" not in _view(theirs)


# ---- ingestion -----------------------------------------------------------------------------------


def test_a_note_is_ingested_rendered_attributed_and_its_file_removed(on, workspace):
    env = _launch(workspace, key="fixture:" + "d" * 8)
    path = _drop(env, "x.md", NOTE)
    assert memory.ingest(on)["notes"] == 1
    assert not path.exists()
    [note] = memory.notes(on)
    assert note["name"] == "Use typed ids" and note["type"] == "decision"
    assert note["session_key"] == "fixture:" + "d" * 8 and note["kind"] == "terminal"
    view = Path(env[memory.ENV_READ])
    assert "Scopes are project ids" in (view / "use-typed-ids.md").read_text()
    assert "[Use typed ids](use-typed-ids.md)" in (view / "MEMORY.md").read_text()


def test_input_is_lenient_without_frontmatter_and_the_same_name_replaces(on, workspace):
    env = _launch(workspace)
    _drop(env, "pitfall_one.md", "# Flaky dtach tests\n\nThey flake under load.\n")
    memory.ingest(on)
    [note] = memory.notes(on)
    assert (note["slug"], note["type"]) == ("flaky-dtach-tests", memory.DEFAULT_TYPE)
    _drop(env, "again.md", "# Flaky dtach tests\n\nOnly under CI load.\n")
    memory.ingest(on)
    [note] = memory.notes(on)
    assert note["body"].endswith("Only under CI load.")


@pytest.mark.parametrize(
    "text",
    [
        "# Deploy\n\nUse token=ghp_" + "a" * 36 + " for the push.\n",
        "# Db\n\npassword: hunter2-hunter2-hunter2\n",
    ],
)
def test_a_credential_shape_is_withheld_not_rewritten(on, workspace, text):
    env = _launch(workspace)
    path = _drop(env, "deploy.md", text)
    counts = memory.ingest(on)
    assert counts["quarantined"] == 1 and counts["notes"] == 0
    assert not path.exists()
    assert memory.notes(on) == []
    assert _view(env) == ["MEMORY.md"]  # absent from the agent-readable view
    with _db(on) as conn:
        [row] = conn.execute("SELECT body, reason FROM quarantine").fetchall()
    assert row["body"] == text  # withheld whole, never rewritten
    assert not any("hunter2" in p.read_text() for p in Path(env[memory.ENV_READ]).iterdir())


def test_a_stored_secret_value_is_withheld(on, workspace, monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["s3cret-Value-91"])
    env = _launch(workspace)
    _drop(env, "n.md", "# Note\n\nthe value is s3cret-Value-91 ok\n")
    assert memory.ingest(on)["quarantined"] == 1
    assert memory.notes(on) == []


def test_secret_redaction_unavailable_fails_closed(on, workspace, monkeypatch):
    def unavailable():
        raise template_secrets.RedactionUnavailable("key missing")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    env = _launch(workspace)
    path = _drop(env, "n.md", NOTE)
    with pytest.raises(template_secrets.RedactionUnavailable):
        memory.ingest(on)
    memory.ingest_all()  # the loop logs and keeps the file
    assert path.exists() and memory.notes(on) == []


def test_oversize_binary_and_empty_files_are_refused_and_removed(on, workspace):
    env = _launch(workspace)
    big = _drop(env, "big.md", "x" * (memory.ENTRY_MAX_BYTES + 1))
    binary = _drop(env, "bin.md", b"\xff\xfe\x00junk")
    empty = _drop(env, "empty.md", "---\nname: x\n---\n")
    counts = memory.ingest(on)
    assert counts["refused"] == 3 and memory.notes(on) == []
    assert not big.exists() and not binary.exists() and not empty.exists()
    with _db(on) as conn:
        reasons = {r["filename"]: r["reason"] for r in conn.execute("SELECT * FROM refused")}
    assert "KiB" in reasons["big.md"] and "UTF-8" in reasons["bin.md"]


def test_an_inbox_holds_at_most_its_cap(on, workspace):
    env = _launch(workspace)
    for i in range(memory.INBOX_MAX_PENDING + 3):
        _drop(env, f"n{i:02d}.md", f"# Note {i}\n\nbody {i}\n", age=100 - i)
    counts = memory.ingest(on)
    assert counts["notes"] == memory.INBOX_MAX_PENDING and counts["refused"] == 3
    assert list(Path(env[memory.ENV_INBOX]).iterdir()) == []


def test_a_full_scope_refuses_new_notes_but_replaces_existing(on, workspace, monkeypatch):
    monkeypatch.setattr(memory, "SCOPE_MAX_NOTES", 1)
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    memory.ingest(on)
    _drop(env, "b.md", "# Another\n\nnew\n")
    _drop(env, "c.md", NOTE.replace("never names", "only ids"))
    counts = memory.ingest(on)
    assert counts["refused"] == 1 and counts["notes"] == 1
    assert [n["slug"] for n in memory.notes(on)] == ["use-typed-ids"]


def test_symlinks_hardlinks_and_planted_directories_are_refused(on, workspace, tmp_path):
    secret = tmp_path / "id_ed25519"
    secret.write_text("PRIVATE KEY MATERIAL")
    env = _launch(workspace)
    inbox = Path(env[memory.ENV_INBOX])
    (inbox / "link.md").symlink_to(secret)
    os.link(secret, inbox / "hard.md")
    for p in (inbox / "link.md", inbox / "hard.md"):
        os.utime(p, (1, 1), follow_symlinks=False)
    counts = memory.ingest(on)
    assert counts["refused"] == 2 and memory.notes(on) == []
    assert secret.read_text() == "PRIVATE KEY MATERIAL"  # neither read into memory nor removed
    with _db(on) as conn:
        bodies = [r[0] for r in conn.execute("SELECT body FROM quarantine")]
    assert bodies == []
    # A token directory replaced by a symlink to elsewhere is never followed.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text(NOTE)
    os.utime(elsewhere / "x.md", (1, 1))
    other = _launch(workspace)
    Path(other[memory.ENV_INBOX]).rmdir()
    Path(other[memory.ENV_INBOX]).symlink_to(elsewhere)
    memory.ingest(on)
    assert (elsewhere / "x.md").exists() and memory.notes(on) == []


def test_a_planted_symlink_in_the_read_view_is_replaced_not_followed(on, workspace, tmp_path):
    target = tmp_path / "victim.md"
    target.write_text("untouched")
    env = _launch(workspace)
    Path(env[memory.ENV_READ], "use-typed-ids.md").symlink_to(target)
    _drop(env, "a.md", NOTE)
    memory.ingest(on)
    assert target.read_text() == "untouched"
    assert not Path(env[memory.ENV_READ], "use-typed-ids.md").is_symlink()


# ---- clear, delete and crash races (#1198's pinned regressions) ----------------------------------


def test_clear_during_ingest_commits_nothing_and_drains_the_inboxes(on, workspace, monkeypatch):
    env = _launch(workspace)
    path = _drop(env, "a.md", NOTE)
    real = memory._collect

    def collect_then_clear(pid, known):
        found = real(pid, known)
        memory.clear(pid)  # the operator clears while this pass holds what it read
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_clear)
    assert memory.ingest(on) == {"skipped": "superseded"}
    assert memory.notes(on) == [] and not path.exists()
    assert _view(env) == ["MEMORY.md"]


def test_delete_racing_ingest_and_retry_after_delete_never_resurrect(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    remove = memory._remove_inbox_file
    # The pass commits, then dies before it removes the inbox file.
    monkeypatch.setattr(memory, "_remove_inbox_file", lambda pid, c: None)
    memory.ingest(on)
    assert len(_inbox(env)) == 1  # the claimed file survived
    memory.delete_note(on, "use-typed-ids")
    monkeypatch.setattr(memory, "_remove_inbox_file", remove)
    counts = memory.ingest(on)  # the retry
    assert counts["dropped"] == 1 and memory.notes(on) == []
    assert _inbox(env) == []


def test_a_crash_before_commit_keeps_the_file_and_after_commit_never_duplicates(
    on, workspace, monkeypatch
):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    real_trim, remove = memory._trim, memory._remove_inbox_file

    def crash(conn, now):
        raise RuntimeError("killed inside the transaction")

    monkeypatch.setattr(memory, "_trim", crash)
    with pytest.raises(RuntimeError):
        memory.ingest(on)
    assert len(_inbox(env)) == 1 and memory.notes(on) == []  # rolled back: entry AND receipt
    monkeypatch.setattr(memory, "_trim", real_trim)
    monkeypatch.setattr(memory, "_remove_inbox_file", lambda pid, c: None)
    memory.ingest(on)  # committed; dies before the unlink
    assert len(_inbox(env)) == 1 and len(memory.notes(on)) == 1
    monkeypatch.setattr(memory, "_remove_inbox_file", remove)
    counts = memory.ingest(on)
    assert counts == {"notes": 0, "quarantined": 0, "refused": 0, "dropped": 1}
    assert len(memory.notes(on)) == 1 and _inbox(env) == []


def test_turning_memory_off_stops_ingestion_and_keeps_pending_files(on, workspace):
    env = _launch(workspace)
    path = _drop(env, "a.md", NOTE)
    memory.set_enabled(on, False)
    assert memory.ingest(on) == {"skipped": "off"}
    assert path.exists()
    assert "turned off" in Path(env[memory.ENV_READ], "MEMORY.md").read_text()
    memory.clear(on)
    assert not path.exists()


# ---- project deletion ----------------------------------------------------------------------------


def test_deleting_a_project_deletes_its_memory(on, workspace):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    memory.ingest(on)
    projects.delete(on)
    assert not (memory.root() / on).exists()
    assert memory.notes(on) == []


def test_deletion_racing_an_ingest_commits_nothing(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    real = memory._collect

    def collect_then_delete(pid, known):
        found = real(pid, known)
        projects.delete(pid)
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_delete)
    assert memory.ingest(on) == {"skipped": "deleted"}
    assert not (memory.root() / on).exists()


def test_deletion_racing_a_launch_binds_nothing(on, workspace, monkeypatch):
    # The launch resolved the project before the deletion; it binds after it.
    monkeypatch.setattr(memory, "_project_for", lambda key, cwd: on)
    projects.delete(on)
    assert _launch(workspace) == {}
    assert not (memory.root() / on).exists()


# ---- the terminal seam ---------------------------------------------------------------------------


@pytest.mark.parametrize("launch", [True, False])
def test_the_terminal_launch_carries_memory_and_an_attach_never_does(monkeypatch, launch, tmp_path):
    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    monkeypatch.setenv(memory.ENV_READ, "/stale/app/copy")  # never inherited from the app
    observed = []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop at spawn boundary")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    given = {memory.ENV_READ: "/m/notes", memory.ENV_INBOX: "/m/inbox/t"}
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key="fixture:test",
            launch_provider=SimpleNamespace(engine_id="fixture") if launch else None,
            launch_env=given,
        )
    )
    [env] = observed
    if launch:
        assert {k: env[k] for k in memory.ENV_KEYS} == given
    else:
        # An attach starts a dtach CLIENT; the running agent's environment is not this one, and
        # nothing memory-related is decided here.
        assert memory.ENV_INBOX not in env


def test_a_launch_without_memory_strips_the_apps_own_copies(monkeypatch, tmp_path):
    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    for name in memory.ENV_KEYS:
        monkeypatch.setenv(name, "/stale")
    observed = []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key="fixture:test",
            launch_provider=SimpleNamespace(engine_id="fixture"),
        )
    )
    assert not set(memory.ENV_KEYS) & set(observed[0])


# ---- boundaries ----------------------------------------------------------------------------------

# Who may reach the store: the launch seams, the loop, project deletion and the Memory page's
# routes. Memory is never a system prompt, never evidence and never sent to a
# model: no prompt, review, mission-judge or checklist module may import it.
ALLOWED_IMPORTERS = {
    # The memory tool (#1506): the per-launch wiring at the seams, and the MCP server the agent CLI
    # starts. Neither is a prompt, evidence or model input: the server serves the agent's own view.
    "agent_tools.py",
    "memory_mcp.py",
    "headless_dispatch.py",
    "main.py",
    "memory_loop.py",
    "native_runtime.py",
    "native_worker.py",
    "projects.py",
    "webterm.py",
    "routes/memory.py",
    "routes/terminal.py",
    "template_vars.py",
}


def test_only_the_launch_seams_the_loop_and_project_deletion_import_memory():
    found = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in ("memory.py",):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # `from . import memory` / `from .. import memory` (the package root) or
                # `from .memory import …` — never `from .routes import memory` (the routes).
                package_root = node.level and not node.module
                hit = (package_root and any(a.name == "memory" for a in node.names)) or (
                    (node.module or "").split(".")[-1] == "memory"
                    and "routes" not in (node.module or "")
                )
            elif isinstance(node, ast.Import):
                hit = any(a.name.endswith("agent_sessions.memory") for a in node.names)
            else:
                continue
            if hit:
                found.add(rel)
    assert found == ALLOWED_IMPORTERS


@pytest.mark.parametrize("takeover", [False, True])
def test_the_terminal_route_binds_memory_on_launch_and_never_on_attach(
    auth_cfg, fake_jsonl, monkeypatch, engine_bin, takeover
):
    """The route, not only the bridge: a LAUNCH asks memory for this session's paths and hands
    them to the bridge; an ATTACH never asks. Both dispatch paths (#184 and take-over)."""
    import uuid

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from agent_sessions import engines, owner, ptybridge, relaunch, scopedspawn, sessions, webterm
    from agent_sessions.main import create_app

    monkeypatch.setattr(
        engines, "launchable_bin", lambda p: f"/usr/bin/{p.engine_id}" if p else None
    )
    captured = []

    async def fake_run(ws, argv, **kwargs):
        captured.append(kwargs.get("launch_env"))
        await ws.close(code=1000)

    action = {"now": sessions.LAUNCH}
    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(owner, "takeover_enabled", lambda: takeover)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (action["now"], None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    agent_argvs = []  # the engine argv of each LAUNCH, as the route hands it to dtach
    monkeypatch.setattr(
        ptybridge,
        "launch_argv",
        lambda **kw: agent_argvs.append(kw["launch_argv"]) or ["/bin/true"],
    )
    monkeypatch.setattr(ptybridge, "attach_argv", lambda **kw: ["/bin/true"])
    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    engine_bin("claude")
    asked = []
    real = memory.launch_env
    monkeypatch.setattr(memory, "launch_env", lambda *a, **k: asked.append(a) or real(*a, **k))

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    headers = {
        "Origin": auth_cfg.origin,
        "Cookie": f"agent_sessions={c.cookies.get('agent_sessions')}",
    }
    # A folder the picker admits: the one the fake transcripts were recorded in.
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    cwd = next(r["cwd"] for r in rows if r["id"] == "claude:11111111-1111-1111-1111-111111111111")
    pid = projects.create("repo-a", folders=[cwd]).id

    def connect(url):
        try:
            with c.websocket_connect(url, headers=headers) as ws:
                while (msg := ws.receive()).get("type") != "websocket.close":
                    pass
                return msg.get("code")
        except WebSocketDisconnect as e:
            return e.code

    # Memory off: a launch gets neither the paths nor the memory tool (#1506).
    assert connect(f"/ws/term/claude:{uuid.uuid4()}?new=1&cwd={cwd}") == 1000
    memory.set_enabled(pid, True)
    assert connect(f"/ws/term/claude:{uuid.uuid4()}?new=1&cwd={cwd}") == 1000
    action["now"] = sessions.ATTACH
    assert connect("/ws/term/claude:11111111-1111-1111-1111-111111111111") == 1000
    assert len(captured) == 3 and len(asked) == 2 and len(agent_argvs) == 2
    off, launched, attached = captured
    assert off == {}
    assert Path(launched[memory.ENV_READ]).parent.name == pid
    assert set(launched) == set(memory.ENV_KEYS)  # claude takes the tool by flag, not env
    assert attached == {}
    # The memory tool rides the LAUNCH argv, last, carrying exactly this launch's paths; the
    # memory-off launch and the attach (no argv built at all) get nothing.
    assert "--mcp-config" not in agent_argvs[0]
    assert agent_argvs[1][-2] == "--mcp-config"
    server = json.loads(agent_argvs[1][-1])["mcpServers"]["battlelab_memory"]
    assert {k: server["env"][k] for k in memory.ENV_KEYS} == launched
    assert server["args"] == ["-I", "-m", "agent_sessions.memory_mcp"]


# ---- review round 1 on #1445: each finding's regression ----------------------------------------


def _all_view_bytes(env) -> bytes:
    return b"".join(p.read_bytes() for p in Path(env[memory.ENV_READ]).iterdir() if p.is_file())


def test_a_stored_secret_in_a_file_name_never_reaches_the_view(on, workspace, monkeypatch):
    secret = "Zq9-stored-SECRET-77"
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    env = _launch(workspace)
    _drop(env, f"{secret}.md", "a harmless body with no heading\n")
    counts = memory.ingest(on)
    assert counts["quarantined"] == 1 and memory.notes(on) == []
    assert secret.encode() not in _all_view_bytes(env)


def test_a_secret_stored_between_scan_and_commit_is_still_withheld(on, workspace, monkeypatch):
    secret = "late-SECRET-value-42"
    calls = {"n": 0}

    def inventory():
        calls["n"] += 1
        return [] if calls["n"] == 1 else [secret]  # the scan saw none; the commit sees it

    monkeypatch.setattr(template_secrets, "redaction_values", inventory)
    env = _launch(workspace)
    _drop(env, "n.md", f"# Note\n\nthe value is {secret}\n")
    counts = memory.ingest(on)
    assert counts == {"notes": 0, "quarantined": 1, "refused": 0, "dropped": 0}
    assert secret.encode() not in _all_view_bytes(env)


def test_a_note_holding_a_secret_saved_later_leaves_the_view_on_the_next_render(
    on, workspace, monkeypatch
):
    env = _launch(workspace)
    _drop(env, "n.md", "# Token note\n\nuse later-saved-SECRET-9 here\n")
    memory.ingest(on)
    assert len(memory.notes(on)) == 1
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["later-saved-SECRET-9"])
    memory.ingest(on)  # every pass renders
    assert memory.notes(on) == []
    assert b"later-saved-SECRET-9" not in _all_view_bytes(env)


def test_the_view_is_withdrawn_when_the_secret_check_is_unavailable(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "n.md", NOTE)
    memory.ingest(on)

    def unavailable():
        raise template_secrets.RedactionUnavailable("key missing")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    memory.set_enabled(on, True)  # any render
    assert _view(env) == ["MEMORY.md"]
    assert "temporarily unavailable" in Path(env[memory.ENV_READ], "MEMORY.md").read_text()


def test_a_fifo_swapped_in_for_a_consumed_file_never_blocks(on, workspace, monkeypatch):
    import threading

    env = _launch(workspace)
    path = _drop(env, "a.md", NOTE)
    real = memory._collect

    def collect_then_swap(pid, known):
        found = real(pid, known)
        # The worst case now: the file being judged (its private claim) swapped for a FIFO, and
        # another FIFO planted under the original name.
        [claim] = list((path.parent.parent.parent / "claims").iterdir())
        claim.unlink()
        os.mkfifo(claim)
        os.mkfifo(path)
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_swap)
    done = threading.Event()
    t = threading.Thread(target=lambda: (memory.ingest(on), done.set()), daemon=True)
    t.start()
    t.join(10)
    assert done.is_set(), "ingest blocked on a FIFO"
    assert path.exists()  # not ours to remove: it is not the file that was judged
    monkeypatch.setattr(memory, "_collect", real)
    clearer = threading.Thread(target=memory.clear, args=(on,), daemon=True)
    clearer.start()
    clearer.join(10)
    assert not clearer.is_alive() and not path.exists()


def test_a_file_rewritten_after_its_refusal_is_kept_for_the_next_pass(on, workspace, monkeypatch):
    env = _launch(workspace)
    path = _drop(env, "big.md", "x" * (memory.ENTRY_MAX_BYTES + 1))
    real = memory._collect

    def collect_then_rewrite(pid, known):
        found = real(pid, known)
        path.write_text(NOTE)
        os.utime(path, (1, 1))
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_rewrite)
    assert memory.ingest(on)["refused"] == 1
    assert path.exists()  # the new bytes were never judged, so never deleted
    monkeypatch.setattr(memory, "_collect", real)
    assert memory.ingest(on)["notes"] == 1


def test_an_interrupted_clear_is_finished_never_undone(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "kept.md", NOTE)
    memory.ingest(on)
    pending = _drop(env, "pending.md", "# Pre-clear\n\nwritten before the clear\n")
    real = memory._drain_inboxes

    def crash(pid):
        raise OSError("killed while draining")

    monkeypatch.setattr(memory, "_drain_inboxes", crash)
    with pytest.raises(OSError):
        memory.clear(on)
    monkeypatch.setattr(memory, "_drain_inboxes", real)
    assert memory.ingest(on) == {"skipped": "clear finished"}
    assert memory.notes(on) == [] and not pending.exists()
    _drop(env, "after.md", "# After\n\nwritten after the clear\n")
    assert memory.ingest(on)["notes"] == 1
    assert [n["slug"] for n in memory.notes(on)] == ["after"]


def test_deletion_tombstones_outlive_the_receipt_ttl(on, workspace):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    _drop(env, "b.md", OTHER_NOTE)
    memory.ingest(on)
    memory.delete_note(on, "use-typed-ids")
    with _db(on) as conn:
        conn.execute("UPDATE receipts SET at = 0")  # every receipt far past the TTL
    memory.ingest(on)  # trims
    with _db(on) as conn:
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM receipts")]
    assert outcomes == ["deleted"]  # the taken-in receipt aged out; the tombstone stayed
    _drop(env, "again.md", NOTE)  # the very same bytes, re-submitted
    assert memory.ingest(on)["dropped"] == 1
    assert [n["slug"] for n in memory.notes(on)] == ["other-thing"]


def test_cleared_notes_are_tombstoned_too(on, workspace):
    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    memory.ingest(on)
    memory.clear(on)
    with _db(on) as conn:
        conn.execute("UPDATE receipts SET at = 0")  # past the TTL: only a tombstone survives
    memory.ingest(on)  # trims
    _drop(env, "again.md", NOTE)
    assert memory.ingest(on)["dropped"] == 1 and memory.notes(on) == []


def test_a_failed_memory_removal_fails_the_project_deletion(on, workspace, monkeypatch):
    import shutil

    env = _launch(workspace)
    _drop(env, "a.md", NOTE)
    memory.ingest(on)
    real = shutil.rmtree

    def refuse(path, *a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(projects.ProjectError) as e:
        projects.delete(on)
    assert e.value.status == 500 and on in projects.load()  # nothing claimed deleted
    monkeypatch.setattr(shutil, "rmtree", real)
    projects.delete(on)  # the retry finishes it
    assert on not in projects.load()
    assert not [p for p in memory.root().iterdir() if p.name.startswith(".") and p.name != ".locks"]


def test_a_pass_looks_at_a_bounded_number_of_files_per_inbox(on, workspace):
    env = _launch(workspace)
    for i in range(memory.INBOX_SCAN_MAX + 10):
        _drop(env, f"n{i:02d}.md", f"# Note {i}\n\nbody {i}\n", age=500 - i)
    counts = memory.ingest(on)
    assert counts["notes"] == memory.INBOX_MAX_PENDING
    assert counts["notes"] + counts["refused"] == memory.INBOX_SCAN_MAX
    assert len(list(Path(env[memory.ENV_INBOX]).iterdir())) == 10  # left for the next pass


def test_the_app_never_carries_memory_variables_for_a_child_to_inherit(auth_cfg, monkeypatch):
    from agent_sessions.main import create_app

    for name in memory.ENV_KEYS:
        monkeypatch.setenv(name, "/stale/foreign/project")
    create_app(auth_cfg)
    assert not set(memory.ENV_KEYS) & set(os.environ)


# ---- review round 2 on #1445 ---------------------------------------------------------------------


def test_a_secret_saved_later_is_withdrawn_before_its_write_returns(on, workspace, monkeypatch):
    """The real secret writer: `template_vars` re-publishes every view inside its own fence."""
    from agent_sessions import template_vars

    monkeypatch.setattr(template_secrets, "redaction_values", _REAL_REDACTION)
    value = "staging-build-label-alpha"
    env = _launch(workspace)
    _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    assert memory.ingest(on)["notes"] == 1
    assert value.encode() in _all_view_bytes(env)
    template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    assert value.encode() not in _all_view_bytes(env)  # gone before create_variable returned
    assert memory.notes(on) == []


def test_the_view_is_never_written_outside_the_fence(on):
    conn = memory._connect(on)
    try:
        with pytest.raises(RuntimeError, match="fence"):
            memory._render(on, conn)
    finally:
        conn.close()


def test_deleting_a_note_tombstones_every_version_it_ever_had(on, workspace):
    env = _launch(workspace)
    first = NOTE
    second = NOTE.replace("never names", "only typed ids")
    _drop(env, "a.md", first)
    memory.ingest(on)
    _drop(env, "b.md", second)  # same name: replaces the note
    memory.ingest(on)
    memory.delete_note(on, "use-typed-ids")
    with _db(on) as conn:
        conn.execute("UPDATE receipts SET at = 0")  # far past the TTL
    memory.ingest(on)  # trims
    _drop(env, "replay.md", first)  # the EARLIER version's exact bytes
    assert memory.ingest(on)["dropped"] == 1 and memory.notes(on) == []


def test_a_same_size_rewrite_with_its_mtime_restored_is_never_discarded(on, workspace, monkeypatch):
    env = _launch(workspace)
    big = "x" * (memory.ENTRY_MAX_BYTES + 1)
    path = _drop(env, "big.md", big)
    st = path.stat()
    real = memory._collect

    def collect_then_rewrite(pid, known):
        found = real(pid, known)
        with open(path, "w") as fh:  # in place where possible, same size
            fh.write("y" * len(big))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime restored
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_rewrite)
    assert memory.ingest(on)["refused"] == 1
    assert path.read_text() == "y" * len(big)  # the unjudged bytes are still there


def test_clearing_tombstones_every_version_too(on, workspace):
    env = _launch(workspace)
    first = NOTE
    _drop(env, "a.md", first)
    memory.ingest(on)
    _drop(env, "b.md", NOTE.replace("never names", "only typed ids"))
    memory.ingest(on)
    memory.clear(on)
    with _db(on) as conn:
        conn.execute("UPDATE receipts SET at = 0")
    memory.ingest(on)
    _drop(env, "replay.md", first)
    assert memory.ingest(on)["dropped"] == 1 and memory.notes(on) == []


# ---- review round 3 on #1445 ---------------------------------------------------------------------


def _every_view_byte(env) -> bytes:
    """Every byte under the view directory, recursively — planted subdirectories included."""
    root_dir = Path(env[memory.ENV_READ])
    return b"".join(p.read_bytes() for p in root_dir.rglob("*") if p.is_file())


def test_a_planted_index_directory_cannot_keep_a_secret_visible(on, workspace, monkeypatch):
    from agent_sessions import template_vars

    monkeypatch.setattr(template_secrets, "redaction_values", _REAL_REDACTION)
    value = "staging-build-label-bravo"
    env = _launch(workspace)
    _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    memory.ingest(on)
    view = Path(env[memory.ENV_READ])
    (view / "MEMORY.md").unlink()
    (view / "MEMORY.md").mkdir()  # an agent's directory where the index belongs
    (view / "MEMORY.md" / "keep.txt").write_text("planted")
    template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    assert value.encode() not in _every_view_byte(env)
    assert (view / "MEMORY.md").is_file()


def test_a_view_that_cannot_be_rendered_is_evicted(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "n.md", NOTE)
    memory.ingest(on)

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "_render", broken)
    memory.secrets_changed()
    assert list(Path(env[memory.ENV_READ]).iterdir()) == []


def test_a_view_that_cannot_even_be_evicted_fails_the_secret_write(on, workspace, monkeypatch):
    from agent_sessions import template_vars

    env = _launch(workspace)
    _drop(env, "n.md", NOTE)
    memory.ingest(on)

    def broken(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(memory, "_render", broken)
    monkeypatch.setattr(memory, "_evict_view", broken)
    with pytest.raises(memory.MemoryStoreError, match="could not be withdrawn"):
        template_vars.create_variable({"name": "deploy_key", "kind": "secret", "value": "x" * 24})


def test_a_claim_replaced_after_it_was_read_is_never_discarded(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "first.md", NOTE)
    real = memory._collect
    claims = Path(env[memory.ENV_INBOX]).parent.parent / "claims"

    def collect_then_replace_claim(pid, known):
        found = real(pid, known)
        [claim] = list(claims.iterdir())
        claim.unlink()
        claim.write_text("# Second\n\nwritten over the claim\n")  # a NEW inode, unjudged
        return found

    monkeypatch.setattr(memory, "_collect", collect_then_replace_claim)
    assert memory.ingest(on)["notes"] == 1
    assert len(list(claims.iterdir())) == 1  # the replacement was left alone
    monkeypatch.setattr(memory, "_collect", real)
    assert memory.ingest(on)["notes"] == 1  # and judged by the next pass
    assert sorted(n["slug"] for n in memory.notes(on)) == ["second", "use-typed-ids"]


def test_a_claim_with_its_identity_but_other_bytes_is_never_removed(on, workspace):
    """Coarse timestamps and reused inode numbers can make a different file LOOK identical; only
    the judged bytes count."""
    env = _launch(workspace)
    _drop(env, "first.md", NOTE)
    [c] = memory._collect(on, [])
    claims = Path(env[memory.ENV_INBOX]).parent.parent / "claims"
    claim = claims / c.claim
    claim.write_text(NOTE.replace("never names", "rewritten"))  # same inode, other bytes
    st = os.lstat(claim)
    c.ident = (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mode)  # as if the tick collided
    memory._remove_inbox_file(on, c)
    assert claim.exists()
    c.seen = hashlib.sha256(claim.read_bytes()).hexdigest()
    memory._remove_inbox_file(on, c)
    assert not claim.exists()  # the judged bytes, and only they, are removed


# ---- review round 4 on #1445 ---------------------------------------------------------------------


def _scope_bytes_outside_the_store(pid: str) -> bytes:
    """Every byte a same-user process could read in the scope, except the store itself (which the
    docs state is readable by the same user: application policy, not isolation)."""
    scope = memory.root() / pid
    return b"".join(
        p.read_bytes()
        for p in scope.rglob("*")
        if p.is_file() and not p.name.startswith("state.db")
    )


def test_one_broken_scope_never_stops_the_others_from_being_withdrawn(
    workspace, tmp_path, monkeypatch
):
    from agent_sessions import template_vars

    monkeypatch.setattr(template_secrets, "redaction_values", _REAL_REDACTION)
    value = "staging-build-label-charlie"
    scopes = []
    for name in ("aaa", "bbb"):
        folder = tmp_path / name
        folder.mkdir()
        pid = projects.create(name, folders=[str(folder)]).id
        memory.set_enabled(pid, True)
        env = memory.launch_env(f"fixture:{name}", str(folder), kind="terminal")
        _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
        memory.ingest(pid)
        scopes.append(pid)
    first, second = sorted(scopes)  # visited in this order
    real = memory._connect

    def broken_first(pid):
        if pid == first:
            raise sqlite3.DatabaseError("file is not a database")
        return real(pid)

    monkeypatch.setattr(memory, "_connect", broken_first)
    template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    for pid in (first, second):
        assert value.encode() not in _scope_bytes_outside_the_store(pid), pid


def test_a_scrub_whose_unlinks_fail_leaves_no_readable_copy(on, workspace, monkeypatch):
    value = "staging-build-label-delta"
    env = _launch(workspace)
    _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    memory.ingest(on)

    def broken(*a, **k):
        raise OSError("disk full")

    def no_unlink(dfd, name):
        raise OSError("read-only directory")

    monkeypatch.setattr(memory, "_render", broken)
    monkeypatch.setattr(memory, "_unlink_at", no_unlink)
    memory.secrets_changed()  # every file was emptied, so the scope counts as safe
    assert value.encode() not in _scope_bytes_outside_the_store(on)
    assert not [p for p in (memory.root() / on).iterdir() if p.name.startswith(".evicted")]


def test_a_scrub_never_truncates_what_a_planted_hard_link_points_at(
    on, workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "id_ed25519"
    outside.write_text("PRIVATE KEY MATERIAL")
    env = _launch(workspace)
    os.link(outside, Path(env[memory.ENV_READ]) / "stolen.md")

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "_render", broken)
    memory.secrets_changed()
    assert outside.read_text() == "PRIVATE KEY MATERIAL"
    assert not (Path(env[memory.ENV_READ]) / "stolen.md").exists()


# ---- review round 5 on #1445 ---------------------------------------------------------------------


def test_a_secret_write_is_refused_when_the_views_cannot_be_enumerated(on, workspace, monkeypatch):
    """Fail before commit: an unknown set of views never settles beside a newly stored secret."""
    from agent_sessions import template_vars

    monkeypatch.setattr(template_secrets, "redaction_values", _REAL_REDACTION)
    value = "staging-build-label-echo"
    env = _launch(workspace)
    _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    memory.ingest(on)
    calls = {"n": 0}
    real = memory._list_root

    def flaky(base):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient I/O error")  # one shot
        return real(base)

    monkeypatch.setattr(memory, "_list_root", flaky)
    with pytest.raises(OSError):
        template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    assert value not in _REAL_REDACTION()  # nothing was stored
    # The retry enumerates fine, stores the secret and withdraws the note before returning.
    template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    assert value in _REAL_REDACTION()
    assert value.encode() not in _scope_bytes_outside_the_store(on)


# ---- review round 6 on #1445 ---------------------------------------------------------------------


def test_an_uncertain_lookup_after_the_write_scrubs_instead_of_skipping(on, workspace, monkeypatch):
    from agent_sessions import template_vars

    monkeypatch.setattr(template_secrets, "redaction_values", _REAL_REDACTION)
    value = "staging-build-label-foxtrot"
    env = _launch(workspace)
    _drop(env, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    memory.ingest(on)

    def uncertain(pid):
        raise OSError("transient lookup failure")

    monkeypatch.setattr(memory, "_scope_absent", uncertain)
    template_vars.create_variable({"name": "build_label", "kind": "secret", "value": value})
    assert value in _REAL_REDACTION()
    assert value.encode() not in _scope_bytes_outside_the_store(on)


def test_clear_tombstones_quarantine_items_the_cap_already_evicted(on, workspace, monkeypatch):
    monkeypatch.setattr(memory, "QUARANTINE_MAX_ITEMS", 2)
    secrets_now = ["value-one-aaaa", "value-two-bbbb", "value-three-cc"]
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: list(secrets_now))
    env = _launch(workspace)
    first = "# One\n\nholds value-one-aaaa\n"
    for i, text in enumerate(
        [first, "# Two\n\nholds value-two-bbbb\n", "# Three\n\nholds value-three-cc\n"]
    ):
        _drop(env, f"q{i}.md", text, age=100 - i)
        memory.ingest(on)
    with _db(on) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 2  # one evicted
    memory.clear(on)
    with _db(on) as conn:
        conn.execute("UPDATE receipts SET at = 0")
    secrets_now.clear()  # no longer a stored secret: only the tombstone can stop a replay
    memory.ingest(on)  # trims
    _drop(env, "replay.md", first)
    assert memory.ingest(on)["dropped"] == 1 and memory.notes(on) == []


def test_a_long_multibyte_file_name_is_claimed_and_never_stops_the_pass(on, workspace):
    env = _launch(workspace)
    long_name = "ä" * 121 + ".md"  # 245 bytes: a claim name built from characters would overflow
    assert len(long_name.encode()) == 245
    _drop(env, long_name, "# Long\n\nA note with a long name.\n")
    _drop(env, "short.md", NOTE)
    counts = memory.ingest(on)
    assert counts["notes"] == 2
    assert _inbox(env) == []


# ---- review round 7 on #1445 ---------------------------------------------------------------------


def test_a_crash_recovered_long_three_byte_name_is_taken_in(on, workspace, monkeypatch):
    env = _launch(workspace)
    long_name = "界" * 80 + ".md"  # 243 bytes: the claim cut lands inside a code point
    assert len(long_name.encode()) == 243
    _drop(env, long_name, "a heading-free body, so the file name becomes the note's name\n")
    _drop(env, "normal.md", NOTE)
    real = memory._commit

    def crash(conn, candidates, counts):
        raise RuntimeError("killed after claiming, before committing")

    monkeypatch.setattr(memory, "_commit", crash)
    with pytest.raises(RuntimeError):
        memory.ingest(on)
    monkeypatch.setattr(memory, "_commit", real)
    counts = memory.ingest(on)  # the leftover claims are judged first
    assert counts["notes"] == 2, counts
    assert _inbox(env) == []
    names = [n["name"] for n in memory.notes(on)]
    assert all(n.encode() for n in names)  # storable, showable text


def test_a_file_name_that_is_not_utf8_never_stops_the_pass(on, workspace):
    env = _launch(workspace)
    bad = os.fsdecode(b"bad-\xff\xfe-name.md")
    path = Path(env[memory.ENV_INBOX]) / bad
    path.write_text("a body without a heading\n")
    os.utime(path, (1, 1))
    _drop(env, "normal.md", NOTE)
    assert memory.ingest(on)["notes"] == 2
    assert any("�" in n["name"] for n in memory.notes(on))


def test_one_unstorable_candidate_is_refused_alone(on, workspace, monkeypatch):
    env = _launch(workspace)
    _drop(env, "bad.md", "# Bad\n\nbody\n", age=20)
    _drop(env, "good.md", NOTE, age=10)
    real = memory.parse_note

    def poisoned(text, filename):
        note = real(text, filename)
        if note and filename == "bad.md":
            note["name"] = "broken \udcff name"  # a surrogate: sqlite cannot encode it
        return note

    monkeypatch.setattr(memory, "parse_note", poisoned)
    counts = memory.ingest(on)
    assert counts["notes"] == 1 and counts["refused"] == 1
    assert [n["slug"] for n in memory.notes(on)] == ["use-typed-ids"]
    assert _inbox(env) == []  # the refused one is settled, not retried forever
