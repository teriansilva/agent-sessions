"""The `opencode2` agent (#1986): opencode 2.x beside opencode 1.x, never sharing its data.

opencode 2.x keeps 1.x's default paths with a new schema, so the agent's whole safety rests on two
things pinned here: every process BattleLab starts for it gets its OWN XDG tree (and a launch that
would reach a 1.x store is refused), and its store is read through the real 2.2.7 schema
(`tests/fixtures/opencode2/schema-2.2.7.sql`, read verbatim from a database the binary created).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import agent_tools, agent_usage, engines, transcript
from agent_sessions.engine_errors import EngineError
from agent_sessions.engines import base, opencode2
from agent_sessions.plugins import kinds, process
from agent_sessions.plugins.provider import forced_env

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_sessions"
SCHEMA = Path(__file__).parent / "fixtures" / "opencode2" / "schema-2.2.7.sql"
ENGINE = "opencode2"
XDG = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME")
NOW_MS = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """A fresh 2.x store root per test (the session-wide sandbox pin is shared across tests)."""
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(tmp_path / "o2root"))


def prov():
    return engines.get(ENGINE)


def root() -> Path:
    return Path(os.environ["AGENT_SESSIONS_OPENCODE2_DIR"])


def make_db(sessions=(), messages=(), parts=()) -> Path:
    """A database with 2.2.7's real schema at the path the forced data home puts it."""
    db = Path(base._opencode2_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text())
    for s in sessions:
        row = {
            "project_id": "prj",
            "slug": "s",
            "version": "2.2.7",
            "parent_id": None,
            "title": None,
            "time_archived": None,
            "time_created": NOW_MS,
            "time_updated": NOW_MS,
            **s,
        }
        cols = ", ".join(row)
        con.execute(
            f"INSERT INTO session_v2 ({cols}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
            tuple(row.values()),
        )
    for m in messages:
        con.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated,"
            " data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                m["id"],
                m["session_id"],
                m["type"],
                m["seq"],
                m.get("time", NOW_MS),
                m.get("time", NOW_MS),
                json.dumps(m["data"]),
            ),
        )
    for p in parts:
        con.execute(
            "INSERT INTO session_part (message_id, session_id, ordinal, type, time_created,"
            " time_updated, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                p["message_id"],
                p["session_id"],
                p["ordinal"],
                p["type"],
                NOW_MS,
                NOW_MS,
                json.dumps(p["data"]),
            ),
        )
    con.commit()
    con.close()
    return db


# --- the manifest and the launch argv --------------------------------------------------------


def test_the_agent_loads_beside_opencode_1x_with_its_own_vocabulary():
    p = prov()
    assert p is not None and engines.get("opencode") is not None
    m = p.manifest
    assert m.store.layout == "opencode2-sqlite" and m.transcript_kind == "opencode2-sqlite"
    assert m.usage.kind == "opencode2-store-query" and m.tools_mcp == "opencode2-config-content"
    assert m.session_id.mint == "adopt" and p.id_pattern.fullmatch("ses_abc")
    # Nothing adopted from where 1.x's `opencode` lives; the only executable name it may resolve
    # through a search is its own.
    assert m.binary.search_paths == () and m.binary.name == ENGINE
    assert ENGINE in kinds.RESERVED_IDS


def test_every_launch_runs_a_private_server(engine_bin, tmp_path):
    exe = engine_bin(ENGINE)
    p = prov()
    cwd = str(tmp_path)
    new = p.new_launch_argv("new-00000000-0000-4000-8000-000000000000", cwd=cwd, bypass=True)
    resume = p.launch_argv("ses_abc", cwd=cwd, bypass=True)
    assert new == [exe, "--standalone", cwd, "--auto"]
    assert resume == [exe, "--standalone", cwd, "--session", "ses_abc", "--auto"]
    assert p.launch_argv("ses_abc", cwd=cwd, bypass=False)[-1] == "ses_abc"


# --- isolation ----------------------------------------------------------------------------------


def test_every_process_gets_its_own_xdg_tree():
    env = forced_env(prov())
    r = root()
    assert env == {
        "XDG_DATA_HOME": str(r / "data"),
        "XDG_STATE_HOME": str(r / "state"),
        "XDG_CACHE_HOME": str(r / "cache"),
        "XDG_CONFIG_HOME": str(r / "config"),
    }
    for d in env.values():
        assert Path(d).is_dir() and (Path(d).stat().st_mode & 0o777) == 0o700
    # …and the store BattleLab reads is exactly where that data home puts 2.x's database.
    assert Path(base._opencode2_db()) == r / "data" / "opencode" / "opencode.db"


@pytest.mark.parametrize("where", ["the 1.x default", "inside the 1.x store", "around it"])
def test_a_tree_that_reaches_a_1x_store_is_refused(monkeypatch, tmp_path, where):
    home = Path.home()
    one_x = home / ".local/share/opencode"
    target = {
        # XDG_DATA_HOME=~/.local/share → 2.x's data dir IS 1.x's.
        "the 1.x default": home / ".local/share",
        "inside the 1.x store": one_x / "nested",
        "around it": home,
    }[where]
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(target))
    with pytest.raises(EngineError, match="overlaps the opencode 1.x store"):
        forced_env(prov())
    assert not (target / "data").exists()  # refused before any directory was made


def test_the_loaded_1x_agents_own_store_is_refused_too(monkeypatch, tmp_path):
    """Wherever the operator moved 1.x's database, 2.x's tree may not reach it."""
    elsewhere = tmp_path / "moved-1x"
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(elsewhere / "data/opencode/opencode.db"))
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(elsewhere))
    with pytest.raises(EngineError, match="overlaps"):
        forced_env(prov())


def test_a_db_the_data_home_would_not_hold_is_refused(tmp_path):
    r = tmp_path / "r"
    with pytest.raises(EngineError, match="must be <root>/data/opencode/opencode.db"):
        opencode2.isolation_env(r, r / "elsewhere.db", tmp_path)
    assert opencode2.isolation_env(r, r / "data/opencode/opencode.db", tmp_path)


# Every spawn seam asks `forced_env`, and applies it LAST. The behavioural tests below cover each
# seam; this ratchet keeps a NEW module that resolves an agent's executable from silently skipping
# it. A module listed here spawns nothing for a terminal agent itself, for the reason given.
SPAWNS_ELSEWHERE = {
    "plugins/provider.py": "defines the argv and `forced_env`",
    "routes/terminal.py": "spawns through `webterm.run`, which applies `forced_env`",
    "plugins/probes.py": "spawns through `process.spawn`, which applies `forced_env`",
    "engines/opencode.py": "1.x's unattended argv; spawned by `headless_dispatch`",
    "engines/registry.py": "resolves the binary for display (`launchable_bin`), spawns nothing",
    "native_models.py": "API agents' model lists only (runtime `api`); opencode2 has none",
    "native_runtime.py": "API agents' workers only (runtime `api`); opencode2 has none",
    "plugins/manager.py": "an API source's version only (runtime `api`); opencode2 has none",
    "ptybridge.py": "wraps an agent argv in dtach (its own `launch_argv`); spawns nothing",
}
APPLIES = {"webterm.py", "plugins/process.py", "headless_dispatch.py"}
RESOLVERS = {"entrypoint_path", "launch_argv", "new_launch_argv", "unattended_launch"}


def _names(tree) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
    return out


def test_every_spawn_seam_applies_the_forced_environment():
    for rel in APPLIES:
        assert "forced_env" in _names(ast.parse((SRC / rel).read_text())), rel
    unexplained = []
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(SRC).as_posix()
        names = _names(ast.parse(f.read_text()))
        if names & RESOLVERS and rel not in APPLIES and rel not in SPAWNS_ELSEWHERE:
            unexplained.append(rel)
    assert not unexplained, (
        "resolves an agent's argv/executable without `forced_env` (#1986): " + str(unexplained)
    )


def _webterm_env(monkeypatch, tmp_path, launch_provider, launch_env=None):
    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    observed, closed = [], []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop at the spawn boundary")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            closed.append(kwargs.get("code"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key=f"{ENGINE}:ses_x",
            launch_provider=launch_provider,
            launch_env=launch_env,
        )
    )
    return observed, closed


def test_the_terminal_launch_applies_it_over_everything_else(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(Path.home() / ".local/share"))  # the app's own
    observed, _ = _webterm_env(
        monkeypatch, tmp_path, prov(), launch_env={"XDG_DATA_HOME": "/from/launch/env"}
    )
    [env] = observed
    assert {k: env[k] for k in XDG} == forced_env(prov())


def test_a_refused_terminal_launch_spawns_nothing(monkeypatch, tmp_path):
    def refuse():
        raise EngineError("opencode2: refusing to run — overlaps")

    fake = SimpleNamespace(engine_id=ENGINE, agent_env=refuse)
    opened = []
    monkeypatch.setattr(os, "openpty", lambda: opened.append(1) or (_ for _ in ()).throw(OSError))
    observed, closed = _webterm_env(monkeypatch, tmp_path, fake)
    assert observed == [] and opened == [] and closed == [4500]


def test_the_temporary_operations_apply_it_last():
    """sign-in, version and verification run under `env -i`; the XDG tree rides after it."""
    env = forced_env(prov())
    argv = process._wrapped(
        ["/x/opencode", "--version"], Path("/tmp"), "u.service", 5, agent_env=env
    )
    tail = argv[argv.index("/usr/bin/env") :]
    assert tail[1] == "-i"
    for key, value in env.items():
        assert f"{key}={value}" in tail
    # The probe still gets its deny-all tool profile (2.2.7 normalizes it to a final deny).
    probe = process._wrapped(
        ["/x/opencode", "run"],
        Path("/tmp"),
        "u.service",
        5,
        probe_kind="opencode2-run",
        agent_env=env,
    )
    profile = next(a for a in probe if a.startswith("OPENCODE_CONFIG_CONTENT="))
    assert json.loads(profile.partition("=")[2])["permission"] == {"*": "deny"}


def test_a_refused_temporary_operation_spawns_nothing(monkeypatch, tmp_path):
    def refuse():
        raise EngineError("opencode2: refusing to run — overlaps")

    spawned = []

    async def spawn(*a, **k):
        spawned.append(a)
        raise AssertionError("spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    p = prov()
    monkeypatch.setattr(p, "agent_env", refuse)

    async def go():
        async with process.spawn(p, "version", cwd=tmp_path):
            pass

    with pytest.raises(process.ProcessError, match="refusing"):
        asyncio.run(go())
    assert spawned == []


def test_the_probe_argv_is_a_private_archived_json_run(engine_bin):
    exe = engine_bin(ENGINE)
    op = "00000000-0000-4000-8000-000000000001"
    live = prov()
    # The recipe's probe kind on a copy: the live roster's provider is never mutated.
    p = SimpleNamespace(
        manifest=dataclasses.replace(live.manifest, probe_kind="opencode2-run"),
        id_pattern=live.id_pattern,
        new_session_reconciles=live.new_session_reconciles,
        entrypoint_path=live.entrypoint_path,
    )
    msg = process.probe_message(op, "resume")
    assert process._probe_argv(p, "resume", "ses_abc", op) == [
        exe,
        "run",
        "--standalone",
        "--format",
        "json",
        "--archived",
        "--session",
        "ses_abc",
        msg,
    ]


# --- the store ---------------------------------------------------------------------------------


def test_the_reader_pins_columns_the_real_schema_has():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text())
    cols = {r[1] for r in con.execute("PRAGMA table_info(session_v2)")}
    assert set(opencode2.OPENCODE2_SCHEMA) <= cols


def test_scan_lists_top_level_sessions_in_seconds(tmp_path):
    make_db(
        sessions=[
            {
                "id": "ses_top",
                "directory": str(tmp_path),
                "title": "Greeting",
                "time_created": 1794529295216,
                "time_updated": 1794529298439,
            },
            {"id": "ses_child", "parent_id": "ses_top", "directory": str(tmp_path)},
            {"id": "ses_arch", "directory": str(tmp_path), "time_archived": 1794529296374},
        ]
    )
    p = prov()
    rows = {r.uuid: r for r in p.scan()}
    assert set(rows) == {"ses_top", "ses_arch"}
    top = rows["ses_top"]
    assert (top.engine, top.cwd, top.first_user_message) == (ENGINE, str(tmp_path), "Greeting")
    assert top.last_mtime == pytest.approx(1794529298.439) and rows["ses_arch"].archived
    assert p.lookup("ses_top").uuid == "ses_top" and p.lookup("ses_child") is None
    assert p.lookup("not-an-id") is None


def test_a_drifted_schema_is_a_reported_failure_never_rows(tmp_path):
    db = Path(base._opencode2_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_v2 (id text, directory text)")  # no pinned columns
    con.commit()
    con.close()
    p = prov()
    assert p.scan() == []
    rows, problems = p.scan_checked()
    assert rows == [] and problems and ENGINE in problems[0]


def test_an_absent_store_is_a_valid_empty():
    p = prov()
    assert p.scan() == [] and p.scan_checked() == ([], []) and not p.is_present()


def test_a_new_session_is_adopted_by_directory(tmp_path):
    make_db(sessions=[{"id": "ses_old", "directory": str(tmp_path)}])
    p = prov()
    snap = p.snapshot_session_ids(str(tmp_path))
    assert snap == {"ses_old"}
    assert p.reconcile_new_session(str(tmp_path), snap) is None
    con = sqlite3.connect(base._opencode2_db())
    con.execute(
        "INSERT INTO session_v2 (id, project_id, slug, directory, version, time_created,"
        " time_updated) VALUES ('ses_new', 'p', 's', ?, '2.2.7', 1, 1)",
        (str(tmp_path),),
    )
    con.commit()
    con.close()
    assert p.reconcile_new_session(str(tmp_path), snap) == "ses_new"


def test_archive_is_a_sidecar_flag_and_never_writes_the_database(tmp_path):
    db = make_db(sessions=[{"id": "ses_a", "directory": str(tmp_path)}])
    before = db.read_bytes()
    prov().archive("ses_a")
    prov().unarchive("ses_a")
    assert db.read_bytes() == before


# --- transcript and usage ------------------------------------------------------------------------


def test_the_transcript_reads_messages_and_parts_in_order(tmp_path):
    sid = "ses_t"
    make_db(
        sessions=[{"id": sid, "directory": str(tmp_path)}],
        messages=[
            {
                "id": "m1",
                "session_id": sid,
                "type": "user",
                "seq": 5,
                "data": {"time": {"created": 1794529296677}, "text": "say hi", "files": []},
            },
            {
                "id": "m2",
                "session_id": sid,
                "type": "assistant",
                "seq": 8,
                "data": {
                    "content": [{"type": "text", "text": "Hi! How can I help?"}],
                    "tokens": {"input": 1, "output": 2},
                },
            },
            {
                "id": "m3",
                "session_id": sid,
                "type": "idle",
                "seq": 13,
                "data": {"outcome": "succeeded"},
            },
        ],
        parts=[
            {
                "message_id": "m2",
                "session_id": sid,
                "ordinal": 0,
                "type": "tool",
                "data": {"type": "tool", "tool": "read", "state": {"input": {"path": "a.py"}}},
            },
        ],
    )
    adapter = transcript.adapter_for(ENGINE)
    turns = adapter(sid, Path.home())
    assert [(t.role, t.kind) for t in turns] == [
        ("user", "text"),
        ("assistant", "text"),
        ("assistant", "tool"),
    ]
    assert turns[0].text == "say hi" and turns[1].text == "Hi! How can I help?"
    assert turns[2].text.startswith("read(")
    assert transcript.adapter_for(ENGINE)("ses_missing", Path.home()) == []


def test_usage_sums_each_replys_tokens_inside_the_window(tmp_path):
    sid = "ses_u"
    old = NOW_MS - 30 * 86400 * 1000
    make_db(
        sessions=[{"id": sid, "directory": str(tmp_path)}],
        messages=[
            {
                "id": "a1",
                "session_id": sid,
                "type": "assistant",
                "seq": 1,
                "data": {
                    "tokens": {
                        "input": 6473,
                        "output": 13,
                        "reasoning": 2,
                        "cache": {"read": 1664, "write": 3},
                    }
                },
            },
            {
                "id": "a2",
                "session_id": sid,
                "type": "assistant",
                "seq": 2,
                "time": old,
                "data": {"tokens": {"input": 999999, "output": 999999}},
            },
            {
                "id": "u1",
                "session_id": sid,
                "type": "user",
                "seq": 3,
                "data": {"tokens": {"input": 777}},
            },  # never counted: not a reply
        ],
    )
    report = agent_usage.REPORTERS[ENGINE]()
    assert report.error is None and report.engine == ENGINE
    assert report.tokens == {"in": 6473, "out": 15, "cache_read": 1664, "cache_write": 3}


# --- the tool channel ---------------------------------------------------------------------------


def test_tools_ride_in_2x_shape_merged_over_the_operators_config():
    s = agent_tools.Server(name="battlelab_memory", command="/py", args=("-m", "x"), env={"A": "1"})
    existing = json.dumps({"mcp": {"servers": {"mine": {"type": "remote"}}}, "theme": "x"})
    out = json.loads(agent_tools.opencode2_config_content(s, existing))
    assert out["theme"] == "x" and out["mcp"]["servers"]["mine"] == {"type": "remote"}
    assert out["mcp"]["servers"]["battlelab_memory"] == {
        "type": "local",
        "command": ["/py", "-m", "x"],
        "environment": {"A": "1"},
        "disabled": False,
    }
    assert agent_tools.opencode2_config_content(s, "[1]") is None
    assert agent_tools.opencode2_config_content(s, '{"mcp": {"servers": []}}') is None


# --- the real binary (gated) --------------------------------------------------------------------

REAL = os.environ.get("AGENT_SESSIONS_TEST_OPENCODE2_BIN")


def _tree(pid: int) -> set[int]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                ppid = int(Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            kids.setdefault(ppid, []).append(int(d))
    out, todo = set(), [pid]
    while todo:
        x = todo.pop()
        out.add(x)
        todo += kids.get(x, [])
    return out


def _listeners(pids: set[int]) -> set[str]:
    inodes = set()
    for p in pids:
        try:
            for fd in os.listdir(f"/proc/{p}/fd"):
                t = os.readlink(f"/proc/{p}/fd/{fd}")
                if t.startswith("socket:["):
                    inodes.add(t[8:-1])
        except OSError:
            continue
    found = set()
    for fam in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{fam}").read_text().splitlines()[1:]:
            f = line.split()
            if f[3] == "0A" and f[9] in inodes:  # LISTEN, owned by the tree
                found.add(f"{fam}:{f[1]}")
    return found


def _snapshot(top: Path) -> dict[str, bytes]:
    return {str(p.relative_to(top)): p.read_bytes() for p in sorted(top.rglob("*")) if p.is_file()}


@pytest.mark.skipif(not REAL, reason="set AGENT_SESSIONS_TEST_OPENCODE2_BIN to a 2.x binary")
def test_the_real_binary_stays_in_its_tree_on_loopback_and_leaves_nothing_running(
    tmp_path, monkeypatch
):
    """A real 2.x `run --standalone` (the probe's argv) and TUI under the forced environment:
    a scratch 1.x database beside it is byte-identical afterwards, nothing is written outside the
    2.x root, every listener is 127.0.0.1, and no process of the tree survives."""
    import pty
    import select
    import signal

    home = tmp_path / "home"
    one_x = home / ".local/share/opencode"
    one_x.mkdir(parents=True)
    one_db = one_x / "opencode.db"
    c = sqlite3.connect(one_db)
    c.execute("CREATE TABLE session (id text)")
    c.commit()
    c.close()
    one_x_before = one_db.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    r = home / "o2"
    env = opencode2.isolation_env(r, r / "data/opencode/opencode.db", home)
    work = tmp_path / "work"
    work.mkdir()
    outside_before = _snapshot(tmp_path)
    full = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TERM": "xterm-256color", **env}
    results = {}
    for name, argv in {
        "run": [REAL, "run", "--standalone", "--format", "json", "--archived", "hi"],
        "tui": [REAL, "--standalone", str(work), "--auto"],
    }.items():
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover — the child
            os.chdir(work)
            os.execve(argv[0], argv, full)
        seen, listeners, end = set(), set(), time.time() + 12
        while time.time() < end:
            if select.select([fd], [], [], 0.3)[0]:
                try:
                    os.read(fd, 65536)
                except OSError:
                    break
            tree = _tree(pid)
            seen |= tree
            listeners |= _listeners(tree)
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.1)
        survivors = {p for p in seen - {pid} if Path(f"/proc/{p}").exists()}
        for p in survivors:  # only PIDs this test's own tree had; never by name
            os.kill(p, signal.SIGKILL)
        results[name] = (listeners, survivors)
    print("REAL-BINARY", {k: (sorted(v[0]), sorted(v[1])) for k, v in results.items()})
    for name, (listeners, survivors) in results.items():
        assert listeners, f"{name}: the private server was never seen listening"
        # /proc/net/tcp spells 127.0.0.1 as little-endian hex `0100007F`; ::1 as below.
        loopback = {"tcp:0100007F", "tcp6:00000000000000000000000001000000"}
        assert all(x.rsplit(":", 1)[0] in loopback for x in listeners), (name, listeners)
        assert survivors == set(), (name, survivors)
    assert one_db.read_bytes() == one_x_before
    changed = {k for k, v in _snapshot(tmp_path).items() if outside_before.get(k) != v}
    stray = {k for k in changed if not k.startswith("home/o2/")}
    assert stray == set(), stray
    assert (r / "data/opencode/opencode.db").exists()
