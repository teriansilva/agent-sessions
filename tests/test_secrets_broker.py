"""The secrets broker and tool (#1786 P2): who may request what, audited, revocable, fail closed."""

import asyncio
import contextlib
import json
import os
import sqlite3
import stat
import threading

import pytest

from agent_sessions import agent_tools, secrets_broker, secrets_mcp, variables
from agent_sessions import template_vars as store

P8 = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgCanaryXYZ\n-----END\n"
)
TOKEN_VALUE = "forgejo-token-value-123"


@pytest.fixture
def broker(tmp_home, tmp_path, monkeypatch):
    """A serving broker on a private runtime root; liveness and project resolution by table."""
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(secrets_broker, "_server_running", True)
    projects = {"claude:a1": "atlas", "claude:b1": "beta", "claude:a2": "atlas"}
    live: set[str] = {"claude:a1", "claude:b1", "claude:a2"}
    monkeypatch.setattr(secrets_broker, "_project_for", lambda key, cwd: projects.get(key))
    monkeypatch.setattr(
        secrets_broker,
        "_liveness",
        lambda conn, cap: secrets_broker.ALIVE if cap["phys_key"] in live else secrets_broker.DEAD,
    )
    return {"projects": projects, "live": live, "runtime": runtime}


def _global_file(name="asc_api_key", data=P8, access=True):
    row = variables.create({"name": name, "kind": "secret_file"})
    row = variables.put_file(row["id"], data, "AuthKey.p8", row["updated_at"])
    if access:
        row = variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    return row


def _global_secret(name="forgejo_token", value=TOKEN_VALUE, access=True):
    row = variables.create({"name": name, "kind": "secret", "value": value})
    if access:
        row = variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    return row


def _bind(project, name, kind):
    store.bind_project(project, [{"name": name, "kind": kind, "ref": "global"}])


def _launch(key="claude:a1"):
    env = secrets_broker.launch_env(key, key, "/work", engine="claude", kind="terminal")
    return open(env[secrets_broker.ENV_TOKEN_FILE]).read()


def _audit():
    conn = sqlite3.connect(secrets_broker.db_path())
    try:
        return conn.execute("SELECT name, outcome, reason FROM audit ORDER BY seq").fetchall()
    finally:
        conn.close()


def test_a_launch_gets_a_capability_only_while_the_broker_serves_and_in_a_project(
    broker, monkeypatch
):
    env = secrets_broker.launch_env(
        "claude:a1", "claude:a1", "/w", engine="claude", kind="terminal"
    )
    assert set(env) >= set(secrets_broker.ENV_KEYS)
    token_file = env[secrets_broker.ENV_TOKEN_FILE]
    token = open(token_file).read()
    # The token is only in its 0600 file in the launch's private RAM-backed directory: the
    # environment carries paths, and the store keeps a keyed digest.
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert os.path.dirname(token_file) == env[secrets_broker.ENV_DIR]
    assert token not in json.dumps(env)
    assert token.encode() not in secrets_broker.db_path().read_bytes()
    assert secrets_broker.launch_env("x:none", "x:none", "/w", engine="x", kind="terminal") == {}
    monkeypatch.setattr(secrets_broker, "_server_running", False)
    assert (
        secrets_broker.launch_env("claude:a1", "claude:a1", "/w", engine="claude", kind="terminal")
        == {}
    )


def test_only_a_bound_secret_with_agent_access_on_is_offered(broker):
    _global_file()
    _global_secret()
    _global_secret("off_secret", "another-value-1234", access=False)
    _bind("atlas", "asc_api_key", "secret_file")
    _bind("atlas", "off_secret", "secret")
    listing = secrets_broker.handle(_launch(), "list")
    assert listing["ok"] is True
    assert [s["name"] for s in listing["secrets"]] == ["asc_api_key"]  # forgejo_token: not bound
    assert TOKEN_VALUE not in json.dumps(listing) and "Canary" not in json.dumps(listing)
    assert secrets_broker.handle(_launch(), "path", "off_secret") == {
        "ok": False,
        "error": "access_off",
    }
    assert secrets_broker.handle(_launch(), "path", "forgejo_token")["error"] == "not_offered"


def test_another_project_is_refused_a_secret_only_atlas_binds(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    beta = _launch("claude:b1")
    assert secrets_broker.handle(beta, "list")["secrets"] == []
    assert secrets_broker.handle(beta, "path", "asc_api_key") == {
        "ok": False,
        "error": "not_offered",
    }


def test_a_grant_is_a_private_copy_on_its_launch_and_is_audited(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    reply = secrets_broker.handle(_launch(), "path", "asc_api_key")
    assert reply["ok"] is True and set(reply) == {"ok", "path"}
    path = reply["path"]
    assert open(path, "rb").read() == P8
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert _audit()[-1] == ("asc_api_key", "materialized", None)


def test_a_text_secret_is_materialized_as_its_value(broker):
    _global_secret()
    _bind("atlas", "forgejo_token", "secret")
    reply = secrets_broker.handle(_launch(), "path", "forgejo_token")
    assert open(reply["path"]).read() == TOKEN_VALUE


def test_turning_access_off_refuses_later_requests_and_deletes_the_copies(broker):
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    first, second = _launch("claude:a1"), _launch("claude:a2")
    paths = [secrets_broker.handle(t, "path", "asc_api_key")["path"] for t in (first, second)]
    variables.update(row["id"], {"agent_access": False}, row["updated_at"])
    assert not any(os.path.exists(p) for p in paths)  # the store commit re-judged every copy
    assert secrets_broker.handle(first, "path", "asc_api_key")["error"] == "access_off"


def test_a_replaced_file_invalidates_old_copies(broker):
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    old = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    variables.put_file(row["id"], b"replacement-key-contents", "AuthKey2.p8", row["updated_at"])
    assert not os.path.exists(old)
    again = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    assert open(again, "rb").read() == b"replacement-key-contents"


def test_a_revoked_or_dead_launch_is_refused_and_its_copies_removed(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch("claude:a1")
    path = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    secrets_broker.revoke_launch("claude:a1")
    assert not os.path.exists(path)
    assert secrets_broker.handle(token, "list")["error"] == "unknown_capability"
    other = _launch("claude:a2")
    path2 = secrets_broker.handle(other, "path", "asc_api_key")["path"]
    broker["live"].discard("claude:a2")
    assert secrets_broker.handle(other, "list")["error"] == "launch_ended"
    assert not os.path.exists(path2)


def test_a_session_moved_to_another_project_is_refused(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    broker["projects"]["claude:a1"] = "beta"
    assert secrets_broker.handle(token, "list")["error"] == "project_changed"


def test_nothing_is_materialized_outside_ram_backed_storage(broker, monkeypatch):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()  # minted on RAM-backed storage…
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: False)  # …which then is not
    assert secrets_broker.handle(token, "path", "asc_api_key")["error"] == "storage_unavailable"
    assert _audit()[-1][1:] == ("failed", "storage_unavailable")


def test_the_real_statfs_check_rejects_an_ordinary_directory(tmp_path):
    disk = tmp_path / "plain"
    disk.mkdir()
    expected = secrets_broker._ram_backed(disk)
    # Whatever this host's /tmp is, the answer must come from statfs, never from the path.
    with open("/proc/mounts") as mounts:
        fs = {line.split()[1]: line.split()[2] for line in mounts}
    best = max((m for m in fs if str(disk).startswith(m.rstrip("/") + "/") or m == "/"), key=len)
    assert expected == (fs[best] in ("tmpfs", "ramfs"))


def test_an_unwritable_audit_refuses_and_publishes_nothing(broker, monkeypatch):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(secrets_broker, "_audit", broken)
    assert secrets_broker.handle(token, "path", "asc_api_key")["error"] == "audit_unavailable"
    assert not list(secrets_broker.copies_root().rglob("asc_api_key"))


def test_a_failed_settlement_removes_the_copy_it_published(broker, monkeypatch):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    real_txn = secrets_broker._txn
    published = {}

    def after_publish_fails(conn):
        if published.get("now"):
            raise sqlite3.OperationalError("database is locked")
        return real_txn(conn)

    def mark():
        published["now"] = True

    monkeypatch.setattr(secrets_broker, "_before_publish", mark)
    monkeypatch.setattr(secrets_broker, "_txn", after_publish_fails)
    assert secrets_broker.handle(token, "path", "asc_api_key")["error"] == "audit_unavailable"
    assert not list(secrets_broker.copies_root().rglob("asc_api_key"))
    assert _audit()[-1][1] == "pending"  # never `materialized`


def test_startup_settles_pending_rows_keeps_live_copies_and_revokes_dead_launches(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    alive, dead = _launch("claude:a1"), _launch("claude:a2")
    kept = secrets_broker.handle(alive, "path", "asc_api_key")["path"]
    gone = secrets_broker.handle(dead, "path", "asc_api_key")["path"]
    # A crash between publication and settlement: a copy with only a pending row.
    stray_dir = os.path.join(os.path.dirname(os.path.dirname(kept)), "stray_secret")
    os.mkdir(stray_dir, 0o700)
    stray = os.path.join(stray_dir, "stray")
    open(stray, "w").write("x")
    conn = sqlite3.connect(secrets_broker.db_path())
    conn.execute(
        "INSERT INTO audit(at, name, outcome, path) VALUES(0, 'stray', 'pending', ?)", (stray,)
    )
    conn.commit()
    conn.close()
    broker["live"].discard("claude:a2")
    secrets_broker.reconcile(startup=True)  # the app restarted; a1's agent survived it
    assert os.path.exists(kept) and not os.path.exists(gone) and not os.path.exists(stray)
    assert ("stray", "interrupted", "copy removed") in _audit()
    # The surviving launch's capability still works after the restart.
    assert secrets_broker.handle(alive, "path", "asc_api_key")["ok"] is True
    assert secrets_broker.handle(dead, "list")["error"] == "unknown_capability"


def test_revocation_barrier_no_copy_survives_a_revocation_racing_a_grant(broker, monkeypatch):
    """A grant paused between its policy check and its publication while access is turned off:
    the revoker waits for the fence, then deletes the copy the grant published."""
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    revoker = {}

    def turn_off_meanwhile():
        def off():
            variables.update(row["id"], {"agent_access": False}, row["updated_at"])

        revoker["t"] = threading.Thread(target=off)
        revoker["t"].start()
        revoker["t"].join(timeout=0.5)  # it commits the store, then blocks on our fence
        assert revoker["t"].is_alive()

    monkeypatch.setattr(secrets_broker, "_before_publish", turn_off_meanwhile)
    reply = secrets_broker.handle(token, "path", "asc_api_key")
    revoker["t"].join(timeout=10)
    assert not revoker["t"].is_alive()
    assert reply["ok"] is True and not os.path.exists(reply["path"])


def test_the_audit_keeps_the_newest_rows_per_secret(broker, monkeypatch):
    monkeypatch.setattr(secrets_broker, "AUDIT_PER_SECRET", 3)
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    for _ in range(5):
        secrets_broker.handle(token, "path", "asc_api_key")
    assert len([r for r in _audit() if r[0] == "asc_api_key"]) == 3


def test_a_refusal_never_echoes_the_request(broker):
    token = _launch()
    for bad in ("../etc/passwd", "x" * 100, "name with space"):
        assert secrets_broker.handle(token, "path", bad) == {"ok": False, "error": "bad_request"}
    assert secrets_broker.handle("not-a-token", "list") == {
        "ok": False,
        "error": "unknown_capability",
    }


def test_the_socket_serves_the_mcp_tool_and_the_helper(broker, capsys):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    sock = secrets_broker.socket_path()
    started, stop = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()

        async def main():
            server = await asyncio.start_unix_server(secrets_broker._client, path=str(sock))
            stop["loop"], stop["server"] = loop, server
            started.set()
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    token_file = next(secrets_broker.copies_root().glob("*/.token"))
    assert token_file.read_text() == token
    env = {secrets_mcp.ENV_SOCKET: str(sock), secrets_mcp.ENV_TOKEN_FILE: str(token_file)}
    try:
        old = dict(os.environ)
        os.environ.update(env)
        server = secrets_mcp.Server()
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert [t["name"] for t in tools["result"]["tools"]] == ["secret_list", "secret_path"]
        listed = server.call("secret_list", {})
        assert "asc_api_key" in listed["content"][0]["text"] and "Canary" not in json.dumps(listed)
        got = server.call("secret_path", {"name": "asc_api_key"})
        assert got.get("isError") is not True and open(got["content"][0]["text"], "rb").read() == P8
        refused = server.call("secret_path", {"name": "nope_secret"})
        assert refused["isError"] is True
        assert secrets_mcp.cli(["path", "asc_api_key"]) == 0
        assert capsys.readouterr().out.strip().startswith(str(secrets_broker.copies_root()))
        assert secrets_mcp.cli(["path", "nope_secret"]) == 1
        assert secrets_mcp.cli(["frobnicate"]) == 2
    finally:
        os.environ.clear()
        os.environ.update(old)
        stop["loop"].call_soon_threadsafe(stop["server"].close)


def test_every_mcp_kind_carries_the_secrets_server_beside_memory(broker):
    env = secrets_broker.launch_env(
        "claude:a1", "claude:a1", "/w", engine="claude", kind="terminal"
    )
    srv = agent_tools.secrets_server(env)
    assert srv.name == "battlelab_secrets" and srv.args == (
        "-I",
        "-m",
        "agent_sessions.secrets_mcp",
    )
    assert srv.env[secrets_broker.ENV_TOKEN_FILE] == env[secrets_broker.ENV_TOKEN_FILE]
    token = open(env[secrets_broker.ENV_TOKEN_FILE]).read()
    # The payloads ride argv (/proc/<pid>/cmdline is world-readable): paths only, never a token.
    for payload in (
        agent_tools.claude_mcp_config([srv]),
        " ".join(agent_tools.codex_overrides([srv])),
        agent_tools.copilot_mcp_config([srv]),
        agent_tools.opencode_config_content([srv], None),
    ):
        assert token not in payload
    assert "battlelab_secrets" in agent_tools.claude_mcp_config([srv])
    assert any("battlelab_secrets" in a for a in agent_tools.codex_overrides([srv]))
    assert json.loads(agent_tools.copilot_mcp_config([srv]))["mcpServers"]["battlelab_secrets"][
        "tools"
    ] == ["secret_list", "secret_path"]

    class Manifest:
        tools_mcp = agent_tools.CLAUDE_FLAG

    launch = agent_tools.terminal_launch(Manifest(), {}, {}, secrets_env=env)
    assert "battlelab_secrets" in launch.suffix[-1]  # memory off: secrets alone still ride
    assert agent_tools.secrets_server({secrets_broker.ENV_SOCKET: "relative"}) is None


def test_an_api_generation_validates_and_carries_its_capability(broker):
    from agent_sessions import native_worker

    env = secrets_broker.launch_env(
        "claude:a1", "claude:a1", "/w", engine="claude", kind="terminal"
    )
    config = {"adapter": "claude-stream-json", "secrets_env": env}
    assert native_worker.secrets_environment(config) == env
    with pytest.raises(native_worker.WorkerError):
        native_worker.secrets_environment({"secrets_env": {"PATH": "/x"}})
    with pytest.raises(native_worker.WorkerError):
        native_worker.secrets_environment(
            {"secrets_env": {**env, secrets_broker.ENV_TOKEN_FILE: "relative/token"}}
        )


def test_the_app_never_carries_a_capability_in_its_own_environment(monkeypatch):
    for key in secrets_broker.ALL_ENV_KEYS:
        monkeypatch.setenv(key, "/leaked")
    secrets_broker.scrub_process_environment()
    assert not any(k in os.environ for k in secrets_broker.ALL_ENV_KEYS)


def test_a_label_that_later_holds_a_stored_secret_is_redacted_in_the_listing(broker):
    row = variables.create(
        {
            "name": "asc_api_key",
            "kind": "secret_file",
            "definition": {"help": "key for zz-later-secret-9"},
        }
    )
    row = variables.put_file(row["id"], P8, "AuthKey.p8", row["updated_at"])
    variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    _bind("atlas", "asc_api_key", "secret_file")
    # The store refuses this order; the listing must still hold if a store reached it anyway.
    from agent_sessions import template_secrets, variable_document

    path = store.store_path()
    doc = json.loads(path.read_text())
    vid = variable_document.new_id()
    rec = {
        "name": "later",
        "kind": "secret",
        "scope": "global",
        "project_id": None,
        "secret": template_secrets.encrypt_scoped("global", None, "later", "zz-later-secret-9"),
        "id": vid,
        "definition": {"value_type": "text"},
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    doc["variables"].append(rec)
    doc["identities"].append(variable_document.identity(rec))
    path.write_text(json.dumps(doc))  # past every store check: a hand edit or an older build
    listing = secrets_broker.handle(_launch(), "list")
    assert "zz-later-secret-9" not in json.dumps(listing)


def test_a_copy_keeps_its_file_name_in_a_directory_of_its_secret(broker):
    _global_file()
    _global_secret()
    _bind("atlas", "asc_api_key", "secret_file")
    _bind("atlas", "forgejo_token", "secret")
    token = _launch()
    file_path = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    text_path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    assert file_path.endswith("/asc_api_key/AuthKey.p8")  # e.g. altool wants AuthKey_<ID>.p8
    assert text_path.endswith("/forgejo_token/forgejo_token")
    assert stat.S_IMODE(os.stat(os.path.dirname(file_path)).st_mode) == 0o700


def test_a_launch_gets_exactly_its_own_copy_directory_and_claude_is_given_only_that(
    broker, monkeypatch
):
    env = secrets_broker.launch_env(
        "claude:a1", "claude:a1", "/w", engine="claude", kind="terminal"
    )
    launch_dir = env[secrets_broker.ENV_DIR]
    assert os.path.isdir(launch_dir) and stat.S_IMODE(os.stat(launch_dir).st_mode) == 0o700
    assert os.path.dirname(launch_dir) == str(secrets_broker.copies_root())

    class Manifest:
        tools_mcp = agent_tools.CLAUDE_FLAG

    suffix = agent_tools.terminal_launch(Manifest(), {}, {}, secrets_env=env).suffix
    assert suffix[:2] == ("--add-dir", launch_dir) and suffix[2] == "--mcp-config"
    from agent_sessions import native_protocol

    argv = native_protocol.ClaudeCodec.argv(
        "/bin/claude",
        "0b0b6b4e-0000-4000-8000-000000000001",
        mcp_config="{}",
        add_dirs=agent_tools.claude_add_dirs(env),
    )
    assert argv[-4:] == ["--add-dir", launch_dir, "--mcp-config", "{}"]
    with pytest.raises(ValueError):
        native_protocol.ClaudeCodec.argv(
            "/bin/claude",
            "0b0b6b4e-0000-4000-8000-000000000001",
            add_dirs=("--add-dir", "/a", "/b"),
        )
    # Off RAM-backed storage the launch gets nothing: no token file, no directory, no --add-dir.
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: False)
    env2 = secrets_broker.launch_env(
        "claude:a2", "claude:a2", "/w", engine="claude", kind="terminal"
    )
    assert env2 == {} and agent_tools.claude_add_dirs(env2) == ()


def test_an_entry_that_cannot_be_removed_never_stops_revocation_of_other_launches(broker):
    """Independent review of #1796, B1: an agent leaves a locked directory where its copy was;
    the pass must still destroy every other launch's copies."""
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    a1, a2 = _launch("claude:a1"), _launch("claude:a2")
    p1 = secrets_broker.handle(a1, "path", "asc_api_key")["path"]
    p2 = secrets_broker.handle(a2, "path", "asc_api_key")["path"]
    os.unlink(p1)
    os.mkdir(p1)
    os.chmod(p1, 0)  # cannot be opened: removing it raises
    try:
        variables.update(row["id"], {"agent_access": False}, row["updated_at"])
        assert not os.path.exists(p2)
    finally:
        os.chmod(p1, 0o700)


def test_a_link_planted_in_a_launch_directory_is_never_followed(broker, tmp_path):
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    p1 = secrets_broker.handle(_launch("claude:a1"), "path", "asc_api_key")["path"]
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "AuthKey.p8"
    victim.write_text("must survive")
    holder = os.path.dirname(p1)
    os.rename(holder, os.path.join(os.path.dirname(holder), "moved"))
    os.symlink(outside, holder)  # <launch>/<secret> now points elsewhere
    variables.update(row["id"], {"agent_access": False}, row["updated_at"])
    assert victim.read_text() == "must survive"
    assert not os.path.islink(holder)  # the link itself was removed


def test_archive_cleanup_revokes_off_the_event_loop(broker, monkeypatch):
    """B2: `cleanup_runtime` waits on the broker's fence in a worker thread, never on the loop."""
    import threading as th

    from agent_sessions import reaper, runtime_cleanup

    seen = {}

    async def gone(*_a, **_k):
        return "gone"

    def record(phys_key):
        seen["thread"] = th.current_thread() is not th.main_thread()

    with monkeypatch.context() as m:
        m.setattr(reaper, "terminate_master", gone)
        m.setattr(secrets_broker, "revoke_launch", record)
        asyncio.run(runtime_cleanup.cleanup_runtime("claude", "a1", physical_key="claude:a1"))
    assert seen["thread"] is True


def test_the_store_revoker_runs_after_the_writes_fences_are_released(broker, monkeypatch):
    """B3: the post-commit revoker never extends the authorization fence or the store lock."""
    from agent_sessions import template_secrets

    held = {}
    real = secrets_broker.after_store_commit

    def check():
        held["registration"] = template_secrets._registration._is_owned()
        real()

    monkeypatch.setattr(secrets_broker, "after_store_commit", check)
    _global_secret()
    assert held == {"registration": False}


def test_serve_never_takes_over_a_socket_another_broker_answers(broker, monkeypatch):
    """B4: a second instance on the host never unlinks or replaces a live broker's socket; and the
    socket lives in THIS instance's runtime dir, named after its store."""
    import socket as socket_mod

    from agent_sessions import ptybridge

    path = secrets_broker.socket_path()
    assert path.parent == ptybridge.runtime_dir() and path.name.startswith("secrets-")
    path.parent.mkdir(parents=True, exist_ok=True)
    live = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    live.bind(str(path))
    live.listen(1)
    ino = os.lstat(path).st_ino
    try:
        monkeypatch.setattr(secrets_broker, "_server_running", False)
        monkeypatch.setattr(secrets_broker, "reconcile", lambda **_k: None)
        asyncio.run(asyncio.wait_for(secrets_broker.serve(), timeout=5))
        assert os.lstat(path).st_ino == ino and secrets_broker._server_running is False
    finally:
        live.close()


def test_an_unconfirmed_launch_is_refused_but_never_revoked(broker, monkeypatch):
    """A probe that timed out is UNKNOWN, never dead: the request waits, the capability stays."""
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    with monkeypatch.context() as m:
        m.setattr(secrets_broker, "_liveness", lambda conn, cap: secrets_broker.UNKNOWN)
        assert secrets_broker.handle(token, "list")["error"] == "launch_unconfirmed"
        secrets_broker.reconcile()
    assert secrets_broker.handle(token, "list")["ok"] is True


def test_a_capability_follows_its_real_dtach_master_through_attach_and_detach(broker):
    """dtach chmods its socket on every attach and detach (independent review of #1796): that
    must never revoke a live agent; a different master on the same path must never revive an old
    capability. Driven with a real dtach master."""
    import shutil
    import sqlite3 as sq
    import subprocess
    import time

    from agent_sessions import ptybridge

    dtach = shutil.which("dtach")
    if dtach is None:
        pytest.skip("dtach is not installed")
    sock = ptybridge.socket_path("claude", "m1")

    def start_master():
        subprocess.run([dtach, "-n", str(sock), "-z", "/bin/sleep", "60"], check=True, timeout=10)  # noqa: S603
        for _ in range(100):
            if ptybridge.probe_master(sock) == ptybridge.ALIVE:
                return
            time.sleep(0.05)
        raise AssertionError("no dtach master")

    def stop_master():
        # Only the master this test started: the process holding OUR socket path open.
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            with contextlib.suppress(OSError):
                argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
                if str(sock).encode() in argv and b"-n" in argv:
                    os.kill(int(pid), 9)
        for _ in range(100):
            if ptybridge.probe_master(sock) != ptybridge.ALIVE:
                break
            time.sleep(0.05)
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()

    broker["projects"]["claude:m1"] = "atlas"
    start_master()
    try:
        secrets_broker.launch_env("claude:m1", "claude:m1", "/w", engine="claude", kind="terminal")
        conn = sq.connect(secrets_broker.db_path())
        conn.row_factory = sq.Row

        def cap():
            return conn.execute("SELECT * FROM caps WHERE phys_key = 'claude:m1'").fetchone()

        live = secrets_broker._liveness_impl  # the real probe, not the fixture's table
        assert live(conn, cap()) == secrets_broker.ALIVE
        for mode in (0o700, 0o600, 0o700):  # what an attach / a detach does to the socket
            os.chmod(sock, mode)
            assert live(conn, cap()) == secrets_broker.ALIVE
        stop_master()
        time.sleep(0.01)
        start_master()  # relaunched outside BattleLab's LAUNCH path: another master, same path
        assert live(conn, cap()) == secrets_broker.DEAD
        conn.close()
    finally:
        stop_master()


def test_a_new_launch_of_a_session_ends_its_earlier_capabilities(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    old = _launch("claude:a1")
    path = secrets_broker.handle(old, "path", "asc_api_key")["path"]
    new = _launch("claude:a1")  # a LAUNCH only happens when the old master is gone
    assert secrets_broker.handle(old, "list")["error"] == "unknown_capability"
    assert not os.path.exists(path)
    assert secrets_broker.handle(new, "list")["ok"] is True


def test_a_capability_revoked_while_it_was_probed_gets_no_copy(broker, monkeypatch):
    """The grant re-reads its capability inside the fence: an archive between the (unfenced)
    liveness probe and the fence leaves no copy behind (independent review of #1796, 2)."""
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()

    def archived_meanwhile(conn, cap):
        secrets_broker.revoke_launch(cap["phys_key"])
        return secrets_broker.ALIVE

    monkeypatch.setattr(secrets_broker, "_liveness", archived_meanwhile)
    assert secrets_broker.handle(token, "path", "asc_api_key")["error"] == "unknown_capability"
    assert not list(secrets_broker.copies_root().rglob("AuthKey.p8"))


def test_one_launch_failing_to_reconcile_never_stops_the_others(broker, monkeypatch):
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    a1, a2 = _launch("claude:a1"), _launch("claude:a2")
    secrets_broker.handle(a1, "path", "asc_api_key")
    p2 = secrets_broker.handle(a2, "path", "asc_api_key")["path"]
    real = secrets_broker._destroy_copy
    first_cap = sorted(p.name for p in secrets_broker.copies_root().iterdir())

    def fails_for_one(cap_id, name):
        if cap_id == os.path.basename(os.path.dirname(os.path.dirname(p2))):
            return real(cap_id, name)
        raise OSError("this one is broken")

    assert first_cap
    monkeypatch.setattr(secrets_broker, "_destroy_copy", fails_for_one)
    variables.update(row["id"], {"agent_access": False}, row["updated_at"])
    assert not os.path.exists(p2)


def test_a_failed_mint_leaves_no_token_file_and_no_capability(broker, monkeypatch):
    def broken(*_a):
        raise OSError("tmpfs full")

    monkeypatch.setattr(secrets_broker, "_write_token", broken)
    assert (
        secrets_broker.launch_env("claude:a1", "claude:a1", "/w", engine="claude", kind="terminal")
        == {}
    )
    assert not any(secrets_broker.copies_root().iterdir())
    conn = sqlite3.connect(secrets_broker.db_path())
    assert conn.execute("SELECT COUNT(*) FROM caps").fetchone()[0] == 0
    conn.close()


def test_a_session_moved_to_another_project_loses_its_copies_on_the_next_pass(broker):
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    path = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    broker["projects"]["claude:a1"] = "beta"
    secrets_broker.reconcile()
    assert not os.path.exists(path)


def test_an_unknown_token_never_waits_on_the_fence(broker, monkeypatch):
    def busy():
        raise AssertionError("the fence was taken for an unknown token")

    monkeypatch.setattr(secrets_broker, "access_fence", busy)
    assert secrets_broker.handle("not-a-real-token", "list") == {
        "ok": False,
        "error": "unknown_capability",
    }


def test_a_partial_write_leaves_no_bytes_an_agent_could_read(broker, monkeypatch):
    """Hermes 7421, 1: tmpfs fills mid-copy — the temporary is removed, nothing readable is left,
    and a later pass removes anything untracked in the launch directory."""
    import errno

    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    real_write = os.write
    calls = {"n": 0}

    def fills_up(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(data[:2]))
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(secrets_broker.os, "write", fills_up)
        assert secrets_broker.handle(token, "path", "asc_api_key")["error"] == "storage_unavailable"
    leftovers = [
        p for p in secrets_broker.copies_root().rglob("*") if p.is_file() and p.name != ".token"
    ]
    assert leftovers == []
    launch = next(p for p in secrets_broker.copies_root().iterdir())
    planted = launch / "planted_dir"
    planted.mkdir()
    (planted / "bytes").write_text("PEM fragment")
    variables.update(row["id"], {"agent_access": False}, row["updated_at"])
    assert not planted.exists()


def test_a_stale_pending_row_never_takes_away_a_newer_copy(broker):
    """Hermes 7421, 2: a settlement failed once (a pending row stays); a later grant of the same
    secret succeeded; the restart must keep the newer copy and its registry row."""
    _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    first = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    conn = sqlite3.connect(secrets_broker.db_path())
    cap_id = conn.execute("SELECT id FROM caps").fetchone()[0]
    conn.execute(
        "INSERT INTO audit(at, name, cap_id, outcome, path)"
        " VALUES(0, 'asc_api_key', ?, 'pending', ?)",
        (cap_id, first),
    )
    conn.commit()
    conn.close()
    newer = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    secrets_broker.reconcile(startup=True)
    assert os.path.exists(newer)
    conn = sqlite3.connect(secrets_broker.db_path())
    assert conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 1
    conn.close()
    assert ("asc_api_key", "interrupted", "superseded") in _audit()


def test_refusals_for_unknown_names_are_bounded_too(broker, monkeypatch):
    """Hermes 7421, 3: a looping agent's typos never grow the audit without bound."""
    monkeypatch.setattr(secrets_broker, "AUDIT_PER_SECRET", 3)
    token = _launch()
    for i in range(6):
        assert secrets_broker.handle(token, "path", f"typo_name_{i}")["error"] == "not_offered"
    conn = sqlite3.connect(secrets_broker.db_path())
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE var_id IS NULL").fetchone()[0] == 3
    conn.close()


@pytest.fixture
def atlas_beta(monkeypatch):
    from agent_sessions import projects

    monkeypatch.setattr(projects, "load", lambda path=None: {"atlas": object(), "beta": object()})


def test_unbinding_a_project_destroys_its_copies_and_refuses_it(broker, atlas_beta):
    row = _global_file()
    row = variables.set_projects(row["id"], ["atlas"], row["updated_at"])
    token = _launch()
    path = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    variables.set_projects(row["id"], [], row["updated_at"])
    assert not os.path.exists(path)
    assert "error" in secrets_broker.handle(token, "path", "asc_api_key")


def test_edits_that_leave_the_value_alone_keep_an_agents_copy(broker, atlas_beta):
    row = _global_secret()
    row = variables.set_projects(row["id"], ["atlas"], row["updated_at"])
    token = _launch()
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    row = variables.set_projects(row["id"], ["atlas", "beta"], row["updated_at"])
    row = variables.update(
        row["id"],
        {"definition": {**row["definition"], "label": "Forgejo token"}},
        row["updated_at"],
    )
    assert open(path).read() == TOKEN_VALUE  # still there, untouched
    variables.update(row["id"], {"value": "a-replaced-token-456"}, row["updated_at"])
    assert not os.path.exists(path)  # a new value does destroy it
    again = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    assert open(again).read() == "a-replaced-token-456"


def test_a_project_that_does_not_bind_the_secret_shows_in_its_log(broker):
    row = _global_secret()
    reply = secrets_broker.handle(_launch("claude:b1"), "path", "forgejo_token")
    assert "error" in reply
    [entry] = variables.access_log(row["id"])
    assert entry["outcome"] == "refused" and entry["project_id"] == "beta"


def test_a_reupload_after_a_clear_is_never_taken_for_the_old_copy(broker, monkeypatch):
    row = _global_file()
    _bind("atlas", "asc_api_key", "secret_file")
    token = _launch()
    old = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    # The post-commit revoker is best-effort; with it lost, the next pass must still judge.
    monkeypatch.setattr(secrets_broker, "after_store_commit", lambda: None)
    row = variables.update(row["id"], {"clear_value": True}, row["updated_at"])
    row = variables.put_file(row["id"], b"NEW-FILE-BYTES-1803", "AuthKey.p8", row["updated_at"])
    assert row["file"]["size"] == len(b"NEW-FILE-BYTES-1803")  # revision 1 again
    secrets_broker.reconcile(probe=False)
    assert not os.path.exists(old)
    again = secrets_broker.handle(token, "path", "asc_api_key")["path"]
    assert open(again, "rb").read() == b"NEW-FILE-BYTES-1803"


def test_refusals_from_any_project_never_evict_a_secrets_grants(broker, monkeypatch):
    monkeypatch.setattr(secrets_broker, "AUDIT_PER_SECRET", 3)
    row = _global_secret()
    _bind("atlas", "forgejo_token", "secret")
    secrets_broker.handle(_launch("claude:a1"), "path", "forgejo_token")
    beta = _launch("claude:b1")
    for _ in range(6):
        assert "error" in secrets_broker.handle(beta, "path", "forgejo_token")
    outcomes = [e["outcome"] for e in variables.access_log(row["id"])]
    assert outcomes.count("refused") == 3 and "materialized" in outcomes


def test_a_copy_whose_revision_cannot_be_checked_is_removed_and_the_pass_goes_on(
    broker, monkeypatch
):
    _global_file("a_file")
    _bind("atlas", "a_file", "secret_file")
    tok = _global_secret("b_secret")
    _bind("atlas", "b_secret", "secret")
    token = _launch()
    a = secrets_broker.handle(token, "path", "a_file")["path"]
    b = secrets_broker.handle(token, "path", "b_secret")["path"]

    def broken(vid, rev):
        raise OSError("vault directory is not private")

    monkeypatch.setattr(secrets_broker.vault_files, "identity", broken)
    variables.update(tok["id"], {"agent_access": False}, tok["updated_at"])
    secrets_broker.reconcile(probe=False)
    assert not os.path.exists(a)  # unknowable: fail closed
    assert not os.path.exists(b)  # and the copy after it is still judged


def test_serve_binds_a_private_socket_without_touching_the_process_umask(broker, monkeypatch):
    """The umask is process-global: narrowing it around the bind made directories other threads
    created meanwhile lose their execute bit (a flaky PermissionError in unrelated tests, and the
    same window in the running app). The socket is made 0600 on its own before it listens."""
    import socket as socket_mod

    def forbidden(mask):
        raise AssertionError("serve() must not change the process umask")

    monkeypatch.setattr(os, "umask", forbidden)
    monkeypatch.setattr(secrets_broker, "_server_running", False)
    monkeypatch.setattr(secrets_broker, "reconcile", lambda **_k: None)
    path = secrets_broker.socket_path()
    seen: dict = {}

    async def run():
        task = asyncio.create_task(secrets_broker.serve())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if secrets_broker._server_running:
                break
        seen["running"] = secrets_broker._server_running
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        try:
            await asyncio.get_running_loop().sock_connect(probe, str(path))
            seen["answers"] = True
        finally:
            probe.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert seen == {"running": True, "mode": 0o600, "answers": True}
