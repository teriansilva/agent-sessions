"""Project MCP servers (#2163 P1a): registry, per-project resolution, the broker op, the check's
bootstrap and settlement, output withholding, and every launch shape — end to end through the real
launcher against a fixture stdio server and a fixture streamable-HTTP server."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_sessions import (
    agent_tools,
    mcp_check,
    mcp_install,
    mcp_registry,
    projects,
    secrets_broker,
    variables,
)
from agent_sessions import template_vars as store

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
LIB_KEY = "library-key-0f1e2d3c4b5a"
OWN_KEY = "atlas-own-key-9a8b7c6d5e"


# ---- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_home, tmp_path, monkeypatch):
    """A serving broker (socket in a thread) on a private runtime root, two real projects, and
    session → project resolution by table."""
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(secrets_broker, "_server_running", True)
    monkeypatch.setattr(mcp_registry, "_ALLOW_LOOPBACK_HTTP", True)
    scratch = tmp_path / "checks"
    scratch.mkdir()
    monkeypatch.setattr(
        mcp_check,
        "_scope_dir",
        lambda: Path(__import__("tempfile").mkdtemp(dir=scratch)),
    )
    atlas = projects.create("Atlas").id
    beta = projects.create("Beta").id
    table = {"claude:a1": atlas, "claude:b1": beta}
    live = set(table)
    monkeypatch.setattr(secrets_broker, "_project_for", lambda key, cwd: table.get(key))
    real = secrets_broker._liveness_impl

    def liveness(conn, cap):
        if cap["kind"] == secrets_broker.CHECK_KIND:
            return real(conn, cap)
        return secrets_broker.ALIVE if cap["phys_key"] in live else secrets_broker.DEAD

    monkeypatch.setattr(secrets_broker, "_liveness", liveness)
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
    yield {"atlas": atlas, "beta": beta, "table": table, "live": live, "tmp": tmp_path}
    stop["loop"].call_soon_threadsafe(stop["server"].close)


@pytest.fixture
def http_server():
    """A fixture MCP streamable-HTTP server. It answers only ``Authorization: Bearer <expected>``
    (or anything when ``expected`` is None); ``echo`` leaks the header back through every
    channel."""
    state = {"expected": None, "echo": False, "sse": False, "seen": [], "malformed": {}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_DELETE(self):
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            auth = self.headers.get("authorization", "")
            state["seen"].append(auth)
            if state["expected"] is not None and auth != f"Bearer {state['expected']}":
                self.send_response(401)
                self.end_headers()
                return
            msg = json.loads(body)
            method, rid = msg.get("method"), msg.get("id")
            if rid is None:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "fixture-http",
                        "version": auth if state["echo"] else "1.0",
                    },
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "search",
                            "inputSchema": {"type": "object"},
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                }
            elif method == "tools/call":
                result = {
                    "content": [{"type": "text", "text": f"ok {auth if state['echo'] else ''}"}]
                }
            else:
                result = {}
            if method in state["malformed"]:
                result = {**result, **state["malformed"][method]}
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
            self.send_response(200)
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "sess-1")
            if state["sse"] and method == "tools/list":
                data = b"event: message\ndata: " + payload + b"\n\n"
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    yield state
    srv.shutdown()


@pytest.fixture
def stdio_sources(monkeypatch):
    """Admit a package source for the fixture (P1b fills the real installer) and point its command
    at the fixture server."""
    monkeypatch.setattr(mcp_install, "validate_source", lambda raw: dict(raw))
    monkeypatch.setattr(
        mcp_registry, "_command", lambda rec: (sys.executable, ["-I", str(FIXTURE)])
    )


def _library_secret(name="api_key", value=LIB_KEY):
    return variables.create({"name": name, "kind": "secret", "value": value})


def _own_secret(pid, name="api_key", value=OWN_KEY):
    store.bind_project(pid, [{"name": name, "kind": "secret", "value": value}])


def _bind_shared(pid, name="api_key"):
    store.bind_project(pid, [{"name": name, "kind": "secret", "ref": "global"}])


def _remote(url, *, credential=True, **extra):
    settings = (
        [{"key": "Authorization", "kind": "credential", "variable": "api_key", "prefix": "Bearer "}]
        if credential
        else []
    )
    return mcp_registry.create(
        {"name": "remote1", "source": {"kind": "remote", "url": url}, "settings": settings, **extra}
    )


def _stdio(*, echo=False, argv_file=None, probe=None):
    settings = [{"key": "API_KEY", "kind": "credential", "variable": "api_key"}]
    if echo:
        settings.append({"key": "FIXTURE_ECHO", "value": "1"})
    if argv_file:
        settings.append({"key": "FIXTURE_ARGV_FILE", "value": str(argv_file)})
    return mcp_registry.create(
        {
            "name": "fixture",
            "source": {"kind": "npm", "package": "fixture", "version": "1.0.0"},
            "settings": settings,
            "probe": probe,
        }
    )


def _check(sid, scope):
    return asyncio.run(mcp_check.run_check(sid, scope))


def _launch(key="claude:a1"):
    capability = secrets_broker.launch_env(key, key, "/work", engine="claude", kind="terminal")
    token = open(capability[secrets_broker.ENV_TOKEN_FILE]).read()
    return capability, token


def _enable(sid, projects_):
    rec = mcp_registry.get(sid)
    return mcp_registry.set_projects(sid, projects_, rec["updated_at"])


# ---- the registry ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, words",
    [
        ({"name": "battlelab_x"}, "BattleLab's own"),
        ({"name": "Bad Name"}, "lowercase"),
        ({"source": {"kind": "remote", "url": "http://example.com/mcp"}}, "https"),
        ({"source": {"kind": "remote", "url": "https://u:p@example.com/mcp"}}, "never in the URL"),
        (
            {"settings": [{"key": "Authorization", "kind": "credential", "value": "x"}]},
            "stored in Variables",
        ),
        ({"settings": [{"key": "Host", "value": "x"}]}, "set by BattleLab"),
        ({"settings": [{"key": "API_KEY", "deliver": "env", "value": "x"}]}, "remote server takes"),
        ({"args": ["--x"]}, "takes no arguments"),
    ],
)
def test_the_registry_refuses_what_it_must(env, payload, words):
    base = {"name": "ok-name", "source": {"kind": "remote", "url": "https://mcp.example/mcp"}}
    with pytest.raises(mcp_registry.RegistryError, match=words):
        mcp_registry.create({**base, **payload})


def test_an_env_setting_may_not_steer_the_interpreter(env, stdio_sources):
    for key in ("PATH", "LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "BATTLELAB_SECRETS_SOCKET"):
        with pytest.raises(mcp_registry.RegistryError, match="reserved"):
            mcp_registry.create(
                {
                    "name": "x1",
                    "source": {"kind": "npm", "package": "p", "version": "1.0.0"},
                    "settings": [{"key": key, "value": "x"}],
                }
            )


def test_package_sources_are_refused_until_p1b(env):
    with pytest.raises(mcp_registry.RegistryError, match="not available yet"):
        mcp_registry.create(
            {"name": "x1", "source": {"kind": "npm", "package": "p", "version": "1.0.0"}}
        )


# ---- resolution --------------------------------------------------------------------------------


def test_own_value_wins_shared_needs_a_binding_and_there_is_never_a_fallback(env, http_server):
    _library_secret()
    rec = _remote(http_server["url"])
    atlas, beta = env["atlas"], env["beta"]
    # Beta does not bind the library secret: missing, never the library value.
    assert mcp_registry.resolve(rec, beta).missing == ("api_key",)
    _bind_shared(beta)
    r = mcp_registry.resolve(rec, beta, with_values=True)
    assert r.missing == () and r.headers["Authorization"] == f"Bearer {LIB_KEY}"
    assert r.sources == {"api_key": "shared"}
    _own_secret(atlas)
    r = mcp_registry.resolve(rec, atlas, with_values=True)
    assert r.headers["Authorization"] == f"Bearer {OWN_KEY}" and r.sources == {"api_key": "own"}
    # The shared scope reads the library record itself.
    assert (
        mcp_registry.resolve(rec, mcp_registry.SHARED, with_values=True).headers["Authorization"]
        == f"Bearer {LIB_KEY}"
    )
    # A bound project and the shared scope resolve to the same set (one check covers both).
    assert (
        mcp_registry.resolve(rec, beta).digest
        == mcp_registry.resolve(rec, mcp_registry.SHARED).digest
    )
    assert mcp_registry.resolve(rec, atlas).digest != mcp_registry.resolve(rec, beta).digest


# ---- checks: bootstrap and settlement ----------------------------------------------------------


def test_a_first_check_runs_before_enable_and_gates_it(env, http_server):
    _library_secret()
    http_server["expected"] = LIB_KEY
    rec = _remote(http_server["url"], probe={"tool": "search", "arguments": {}})
    _bind_shared(env["beta"])
    with pytest.raises(mcp_registry.RegistryError, match="run a check"):
        _enable(rec["id"], [env["beta"]])
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "passed", row["steps"]
    assert [s["step"] for s in row["steps"]] == [
        "settings",
        "started",
        "handshake",
        "tools",
        "probe",
    ]
    assert row["tools"] == ["search"]
    assert http_server["seen"] and all(a == f"Bearer {LIB_KEY}" for a in http_server["seen"])
    rec = _enable(rec["id"], [env["beta"]])
    assert rec["projects"] == [env["beta"]]


def test_a_credential_free_remote_server_checks_and_enables(env, http_server):
    rec = _remote(http_server["url"], credential=False)
    row = _check(rec["id"], env["atlas"])
    assert row["outcome"] == "passed", row["steps"]
    assert _enable(rec["id"], [env["atlas"]])["projects"] == [env["atlas"]]


def test_a_missing_required_credential_fails_at_settings(env, http_server):
    rec = _remote(http_server["url"])
    row = _check(rec["id"], env["atlas"])
    assert row["outcome"] == "failed"
    assert row["steps"] == [
        {"step": "settings", "ok": False, "detail": "missing credentials: api_key"}
    ]
    with pytest.raises(mcp_registry.RegistryError, match="add the credentials first"):
        _enable(rec["id"], [env["atlas"]])


def test_a_rejected_login_fails_the_check_in_words(env, http_server):
    _library_secret()
    http_server["expected"] = "something-else"
    rec = _remote(http_server["url"])
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "failed"
    assert any("HTTP 401" in s["detail"] for s in row["steps"])


def test_a_variables_edit_during_a_check_supersedes_it(env, http_server, monkeypatch):
    """The cross-store race: a Variables commit lands between the check's start and its
    settlement (a barrier inside the conversation). Never `passed`; enable and delivery refuse."""
    row = _library_secret()
    _bind_shared(env["beta"])
    http_server["expected"] = None
    rec = _remote(http_server["url"])
    real = mcp_check._converse

    async def converse(*a, **kw):
        names = await real(*a, **kw)
        current = next(v for v in variables.library()["variables"] if v["id"] == row["id"])
        variables.update(row["id"], {"value": "rotated-key-555"}, current["updated_at"])
        return names

    monkeypatch.setattr(mcp_check, "_converse", converse)
    settled = _check(rec["id"], env["beta"])
    assert settled["outcome"] == "superseded"
    with pytest.raises(mcp_registry.RegistryError, match="run a check"):
        _enable(rec["id"], [env["beta"]])
    # Even with the switch on ("all"), delivery refuses: no passing check for the current set.
    _enable(rec["id"], "all")
    _, token = _launch("claude:b1")
    assert secrets_broker.handle(token, "mcp", rec["id"]) == {
        "ok": False,
        "error": "check_required",
    }


def test_an_edit_after_a_pass_makes_it_stale(env, http_server):
    row = _library_secret()
    _bind_shared(env["beta"])
    rec = _remote(http_server["url"])
    assert _check(rec["id"], env["beta"])["outcome"] == "passed"
    _enable(rec["id"], [env["beta"]])
    capability, _ = _launch("claude:b1")
    assert [s["id"] for s in mcp_registry.launch_specs(env["beta"], capability)] == [rec["id"]]
    variables.update(row["id"], {"value": "rotated-key-777"}, row["updated_at"])
    status = mcp_registry.status(mcp_registry.get(rec["id"]), [env["beta"]])["projects"][0]
    assert status["check"] == "stale" and status["enabled"] and not status["delivered"]
    assert mcp_registry.launch_specs(env["beta"], capability) == []


def test_a_check_only_capability_is_exactly_one_server_and_scope_and_ends_with_the_check(env):
    _library_secret()
    a = _remote("http://127.0.0.1:9/mcp")
    b = mcp_registry.create(
        {"name": "other", "source": {"kind": "remote", "url": "http://127.0.0.1:9/mcp"}}
    )
    digest = mcp_registry.resolve(a, mcp_registry.SHARED).digest
    cap_id, cap_env = secrets_broker.mint_check(a["id"], a["rev"], mcp_registry.SHARED, digest)
    token = open(cap_env[secrets_broker.ENV_TOKEN_FILE]).read()
    got = secrets_broker.handle(token, "mcp", a["id"])
    assert got["ok"] is True
    payload = json.loads(open(got["path"]).read())
    assert payload["headers"]["Authorization"] == f"Bearer {LIB_KEY}"
    assert secrets_broker.handle(token, "mcp", b["id"]) == {"ok": False, "error": "not_offered"}
    assert secrets_broker.handle(token, "list") == {"ok": False, "error": "bad_request"}
    assert secrets_broker.handle(token, "path", "api_key") == {"ok": False, "error": "bad_request"}
    secrets_broker.end_check(cap_id)
    assert not os.path.exists(got["path"])
    assert secrets_broker.handle(token, "mcp", a["id"])["ok"] is False


def test_a_normal_launch_never_uses_the_check_bypass(env, http_server):
    _library_secret()
    _bind_shared(env["beta"])
    rec = _remote(http_server["url"])
    _, token = _launch("claude:b1")
    # Not enabled for Beta, never checked: refused even though the credential resolves.
    assert secrets_broker.handle(token, "mcp", rec["id"])["error"] == "not_enabled"


# ---- delivery ----------------------------------------------------------------------------------


def _ready(env, http_server):
    _library_secret()
    _bind_shared(env["beta"])
    rec = _remote(http_server["url"])
    assert _check(rec["id"], env["beta"])["outcome"] == "passed"
    return _enable(rec["id"], [env["beta"]])


def test_delivery_is_a_private_copy_and_is_not_agent_access(env, http_server):
    rec = _ready(env, http_server)
    _, token = _launch("claude:b1")
    got = secrets_broker.handle(token, "mcp", rec["id"])
    assert got["ok"] is True
    assert oct(os.stat(got["path"]).st_mode & 0o777) == "0o600"
    assert json.loads(open(got["path"]).read())["headers"] == {"Authorization": f"Bearer {LIB_KEY}"}
    # The credential reaches the server's launcher, not the agent: agent access is off.
    assert secrets_broker.handle(token, "list") == {"ok": True, "secrets": []}
    assert secrets_broker.handle(token, "path", "api_key")["ok"] is False
    # Another project's agent gets nothing.
    _, other = _launch("claude:a1")
    assert secrets_broker.handle(other, "mcp", rec["id"])["error"] == "not_enabled"


def test_disabling_or_changing_a_credential_reaches_copies_and_later_requests(env, http_server):
    rec = _ready(env, http_server)
    _, token = _launch("claude:b1")
    got = secrets_broker.handle(token, "mcp", rec["id"])
    lib = next(v for v in variables.library()["variables"] if v["name"] == "api_key")
    variables.update(lib["id"], {"value": "rotated-key-888"}, lib["updated_at"])
    assert not os.path.exists(got["path"])  # the store's post-commit reconcile destroyed it
    assert secrets_broker.handle(token, "mcp", rec["id"])["error"] == "check_required"
    rec = mcp_registry.get(rec["id"])
    mcp_registry.update(rec["id"], {"enabled": False}, rec["updated_at"])
    assert secrets_broker.handle(token, "mcp", rec["id"])["error"] == "server_disabled"


def test_a_refusal_is_audited_against_the_server(env, http_server):
    rec = _remote(http_server["url"])
    _, token = _launch("claude:b1")
    secrets_broker.handle(token, "mcp", rec["id"])
    conn = sqlite3.connect(secrets_broker.db_path())
    try:
        rows = conn.execute("SELECT var_id, outcome, reason FROM audit").fetchall()
    finally:
        conn.close()
    assert (f"mcp:{rec['id']}", "refused", "not_enabled") in rows


# ---- the launcher and every launch shape -------------------------------------------------------


def test_every_launch_shape_carries_ids_and_paths_only(env, http_server):
    rec = _ready(env, http_server)
    capability, _ = _launch("claude:b1")
    servers = mcp_registry.launch_servers(env["beta"], capability)
    assert [s.name for s in servers] == ["remote1"] and servers[0].tools == ("search",)
    payloads = [
        agent_tools.claude_mcp_config(servers),
        json.dumps(agent_tools.codex_overrides(servers)),
        agent_tools.opencode_config_content(servers, None),
        agent_tools.opencode2_config_content(servers, None),
        agent_tools.copilot_mcp_config(servers),
        json.dumps(agent_tools.acp_servers(servers)),
        json.dumps(agent_tools.codex_thread_config(servers)),
    ]
    for p in payloads:
        assert LIB_KEY not in p and rec["id"] in p
    copilot = json.loads(agent_tools.copilot_mcp_config(servers))["mcpServers"]["remote1"]
    assert copilot["tools"] == ["search"]


def test_copilot_skips_a_project_server_without_a_snapshot_and_keeps_the_raise_for_ours():
    unnamed = agent_tools.Server(command="/x", args=(), env={}, name="proj", tools=())
    assert json.loads(agent_tools.copilot_mcp_config([unnamed])) == {"mcpServers": {}}
    with pytest.raises(ValueError):
        agent_tools.copilot_mcp_config(
            [agent_tools.Server(command="/x", args=(), env={}, name="not_ours_unnamed")]
        )


def test_terminal_launch_appends_project_servers_after_battlelabs_own(env, http_server):
    _ready(env, http_server)
    capability, _ = _launch("claude:b1")
    manifest = type("M", (), {"tools_mcp": agent_tools.CLAUDE_FLAG})()
    launch = agent_tools.terminal_launch(
        manifest,
        None,
        {},
        secrets_env=capability,
        project_servers=mcp_registry.launch_servers(env["beta"], capability),
    )
    config = json.loads(launch.suffix[-1])["mcpServers"]
    assert list(config) == ["battlelab_secrets", "remote1"]
    assert LIB_KEY not in launch.suffix[-1]


def test_the_api_worker_validates_its_server_list():
    from agent_sessions import native_worker

    env = {
        secrets_broker.ENV_SOCKET: "/run/s.sock",
        secrets_broker.ENV_TOKEN_FILE: "/run/t",
        secrets_broker.ENV_DIR: "/run",
    }
    ok = native_worker.project_mcp_servers(
        {"mcp_servers": [{"id": "0" * 16, "name": "nc", "tools": ["a"]}]}, env
    )
    assert ok[0].args[-1] == "0" * 16 and ok[0].tools == ("a",)
    for bad in (
        [{"id": "zz", "name": "nc", "tools": []}],
        [{"id": "0" * 16, "name": "battlelab_x", "tools": []}],
        [{"id": "0" * 16, "name": "nc", "tools": ["a b"]}],
        [{"id": "0" * 16, "name": "nc", "tools": [], "env": {}}],
    ):
        with pytest.raises(native_worker.WorkerError):
            native_worker.project_mcp_servers({"mcp_servers": bad}, env)


def test_a_stdio_server_gets_its_env_and_no_value_rides_argv(env, stdio_sources, tmp_path):
    _library_secret()
    argv_file = tmp_path / "argv.bin"
    rec = _stdio(argv_file=argv_file, probe={"tool": "list_things", "arguments": {}})
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "passed", row["steps"]
    assert row["steps"][-1] == {"step": "probe", "ok": True, "detail": "12 entries"}
    argv = argv_file.read_bytes()
    assert argv and LIB_KEY.encode() not in argv
    assert row["tools"] == ["list_things", "write_thing"]


def test_a_probe_on_a_tool_not_marked_read_only_needs_confirmation(env, stdio_sources):
    _library_secret()
    rec = _stdio(probe={"tool": "write_thing", "arguments": {}})
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "failed" and "confirm it may change data" in row["steps"][-1]["detail"]


# ---- withholding -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"key={LIB_KEY}",
        "Basic " + __import__("base64").b64encode(f"user:{LIB_KEY}".encode()).decode(),
        __import__("base64").urlsafe_b64encode(LIB_KEY.encode()).decode().rstrip("="),
        __import__("urllib.parse").parse.quote(f"x {LIB_KEY}/"),
    ],
)
def test_withholding_catches_every_encoding(text):
    assert mcp_check.withhold(text, (LIB_KEY,)) == mcp_check.WITHHELD
    assert mcp_check.withhold("12 entries", (LIB_KEY,)) == "12 entries"


def test_a_server_echoing_its_credential_reaches_no_sink(env, stdio_sources, caplog):
    """The echo fixture leaks API_KEY through stderr, serverInfo, a tool name and the probe
    result; none of it reaches the check row, the stored document, the API or the log."""
    _library_secret()
    rec = _stdio(echo=True, probe={"tool": "list_things", "arguments": {}})
    caplog.set_level("DEBUG")
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "passed"
    blob = json.dumps(row)
    assert LIB_KEY not in blob
    assert mcp_check.WITHHELD in blob  # serverInfo and the probe result were withheld whole
    assert all(LIB_KEY not in t for t in row["tools"]) and len(row["tools"]) == 2
    assert LIB_KEY not in mcp_registry.store_path().read_text()
    assert LIB_KEY not in caplog.text
    # And a failing echo server's stderr is withheld too.
    mcp_registry.update(
        rec["id"],
        {"probe": {"tool": "write_thing", "arguments": {}}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "failed" and LIB_KEY not in json.dumps(row)
    assert any(s["step"] == "stderr" and s["detail"] == mcp_check.WITHHELD for s in row["steps"])


def test_an_http_server_echoing_its_header_reaches_no_sink(env, http_server):
    _library_secret()
    http_server["echo"] = True
    http_server["sse"] = True
    rec = _remote(http_server["url"], probe={"tool": "search", "arguments": {}})
    row = _check(rec["id"], mcp_registry.SHARED)
    assert row["outcome"] == "passed", row["steps"]
    assert LIB_KEY not in json.dumps(row) and row["tools"] == ["search"]


ROTATED = "rotated-before-fetch-4242"


def _rotate_before_mint(monkeypatch, row):
    """Hermes on #2166: rotate the credential AFTER the check resolved its set (and its
    withholding inventory) but BEFORE the launcher fetches its settings from the broker."""
    real = secrets_broker.mint_check

    def mint(*a, **kw):
        current = next(v for v in variables.library()["variables"] if v["id"] == row["id"])
        variables.update(row["id"], {"value": ROTATED}, current["updated_at"])
        return real(*a, **kw)

    monkeypatch.setattr(secrets_broker, "mint_check", mint)


def _assert_rotated_value_nowhere(sid, row):
    assert row["outcome"] == "superseded", row["steps"]
    assert ROTATED not in json.dumps(row)
    assert ROTATED not in json.dumps(mcp_registry.checks(sid))
    assert ROTATED not in mcp_registry.store_path().read_text()
    assert row["tools"] == []
    assert {s["detail"] for s in row["steps"]} == {mcp_registry.SUPERSEDED_DETAIL}


def test_a_credential_rotated_before_the_fetch_is_never_delivered_to_an_http_check(
    env, http_server, monkeypatch
):
    row = _library_secret()
    http_server["echo"] = True
    rec = _remote(http_server["url"], probe={"tool": "search", "arguments": {}})
    _rotate_before_mint(monkeypatch, row)
    settled = _check(rec["id"], mcp_registry.SHARED)
    _assert_rotated_value_nowhere(rec["id"], settled)
    # The broker refused the changed set: the server never saw the rotated value.
    assert all(ROTATED not in seen for seen in http_server["seen"])


def test_a_credential_rotated_before_the_fetch_is_never_delivered_to_a_stdio_check(
    env, stdio_sources, monkeypatch
):
    row = _library_secret()
    rec = _stdio(echo=True, probe={"tool": "list_things", "arguments": {}})
    _rotate_before_mint(monkeypatch, row)
    settled = _check(rec["id"], mcp_registry.SHARED)
    _assert_rotated_value_nowhere(rec["id"], settled)


def test_a_check_capability_is_bound_to_the_set_it_started_with(env):
    row = _library_secret()
    a = _remote("http://127.0.0.1:9/mcp")
    digest = mcp_registry.resolve(a, mcp_registry.SHARED).digest
    cap_id, cap_env = secrets_broker.mint_check(a["id"], a["rev"], mcp_registry.SHARED, digest)
    token = open(cap_env[secrets_broker.ENV_TOKEN_FILE]).read()
    variables.update(row["id"], {"value": ROTATED}, row["updated_at"])
    assert secrets_broker.handle(token, "mcp", a["id"]) == {
        "ok": False,
        "error": "server_changed",
    }
    secrets_broker.end_check(cap_id)


@pytest.mark.parametrize(
    "malformed",
    [
        {"initialize": {"serverInfo": "malformed"}},
        {"initialize": {"serverInfo": {"name": ["x"], "version": 7}, "protocolVersion": 1}},
        {"tools/list": {"tools": "malformed"}},
        {"tools/list": {"tools": [], "nextCursor": 5}},
        {"tools/call": {"content": [{"type": "text", "text": 5}], "isError": "yes"}},
    ],
)
def test_a_malformed_reply_is_a_recorded_failed_check_never_an_exception(
    env, http_server, malformed
):
    http_server["malformed"] = malformed
    rec = _remote(http_server["url"], credential=False, probe={"tool": "search", "arguments": {}})
    settled = _check(rec["id"], mcp_registry.SHARED)
    if "tools/call" in malformed:
        # Non-text content is ignored; a truthy isError fails the probe in words.
        assert settled["outcome"] == "failed"
        assert settled["steps"][-1]["step"] == "probe"
    elif malformed == {
        "initialize": {"serverInfo": {"name": ["x"], "version": 7}, "protocolVersion": 1}
    }:
        assert settled["outcome"] == "passed"  # odd field types are shown as "?", not raised
    else:
        assert settled["outcome"] == "failed"
        assert settled["steps"][-1]["detail"] == mcp_check.MALFORMED
    assert mcp_registry.checks(rec["id"])[-1]["outcome"] == settled["outcome"]


def test_a_reply_nothing_anticipated_is_still_a_failed_check(env, http_server, monkeypatch):
    rec = _remote(http_server["url"], credential=False)

    async def boom(*a, **kw):
        raise KeyError("unexpected shape")

    monkeypatch.setattr(mcp_check, "_converse", boom)
    settled = _check(rec["id"], mcp_registry.SHARED)
    assert settled["outcome"] == "failed"
    assert settled["steps"][-1] == {"step": "protocol", "ok": False, "detail": mcp_check.MALFORMED}


# ---- the routes --------------------------------------------------------------------------------


def test_the_routes_are_authenticated_guarded_and_never_return_a_value(env, http_server, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _library_secret()
    _bind_shared(env["beta"])
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert client.get("/api/mcp/servers").status_code == 401
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = client.get("/api/config").json()["csrf"]
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    body = {
        "name": "remote1",
        "source": {"kind": "remote", "url": http_server["url"]},
        "settings": [
            {
                "key": "Authorization",
                "kind": "credential",
                "variable": "api_key",
                "prefix": "Bearer ",
            }
        ],
    }
    assert (
        client.post("/api/mcp/servers", json=body, headers={"Origin": auth_cfg.origin}).status_code
        == 403
    )
    created = client.post("/api/mcp/servers", json=body, headers=headers)
    assert created.status_code == 201 and "no-store" in created.headers["cache-control"]
    sid = created.json()["id"]
    checked = client.post(
        f"/api/mcp/servers/{sid}/check", json={"scope": env["beta"]}, headers=headers
    )
    assert checked.status_code == 200 and checked.json()["outcome"] == "passed"
    detail = client.get(f"/api/mcp/servers/{sid}").json()
    put = client.put(
        f"/api/mcp/servers/{sid}/projects",
        json={"projects": [env["beta"]], "expected_updated_at": detail["server"]["updated_at"]},
        headers=headers,
    )
    assert put.status_code == 200 and put.json()["state"] == {"kind": "ready"}
    refused = client.put(
        f"/api/mcp/servers/{sid}/projects",
        json={"projects": [env["atlas"]], "expected_updated_at": put.json()["updated_at"]},
        headers=headers,
    )
    assert refused.status_code == 422
    for r in (
        created,
        checked,
        put,
        client.get("/api/mcp/servers"),
        client.get(f"/api/mcp/servers/{sid}"),
    ):
        assert LIB_KEY not in r.text
    # Hermes on #2166: a malformed reply is a recorded failed check, never a 500.
    http_server["malformed"] = {"initialize": {"serverInfo": "malformed"}}
    broken = client.post(
        f"/api/mcp/servers/{sid}/check", json={"scope": env["beta"]}, headers=headers
    )
    assert broken.status_code == 200 and broken.json()["outcome"] == "failed"
    history = client.get(f"/api/mcp/servers/{sid}").json()["checks"]
    assert history[0]["outcome"] == "failed"
