"""The skills tool (#1693 S2): per-launch bundles proved against their pins, the read-only MCP
server over one bundle, and the wiring that carries it on every agent with a tool channel.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import time
import uuid

import pytest

from agent_sessions import agent_tools, engines, native_worker, skill_bundles, skills_mcp
from agent_sessions.gallery import library
from agent_sessions.gallery import skills as lib_skills
from agent_sessions.plugins import kinds

SKILL_MD = "---\nname: Test-driven development\ndescription: Red test first, then green.\n---\n"


def _skill(sid: str, body: str = "Write the failing test first.", extra: dict | None = None):
    library.create_skill(sid, {"SKILL.md": SKILL_MD + body, **(extra or {})})
    return {"id": sid, "revision": lib_skills.get(sid)["revision"]}


def _edit_in_place(sid: str, text: str) -> None:
    """A library write under the EXCLUSIVE lock that changes ``sid``'s content (and revision)."""
    with lib_skills.locked(exclusive=True) as fd:
        dfd = os.open(sid, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            os.chmod("SKILL.md", 0o600, dir_fd=dfd)
            out = os.open("SKILL.md", os.O_WRONLY | os.O_TRUNC, dir_fd=dfd)
            try:
                os.write(out, text.encode())
            finally:
                os.close(out)
        finally:
            os.close(dfd)


# ---- bundles ------------------------------------------------------------------------------------


def test_a_bundle_holds_exactly_the_pinned_skills_read_only():
    tdd = _skill("tdd", extra={"ref/notes.md": "deeper notes"})
    path = skill_bundles.build("res-1", [tdd])
    assert (path / "tdd" / "SKILL.md").read_text().endswith("Write the failing test first.")
    assert (path / "tdd" / "ref" / "notes.md").read_text() == "deeper notes"
    assert stat.S_IMODE((path / "tdd" / "SKILL.md").stat().st_mode) == 0o400
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert sorted(p.name for p in skill_bundles.root().iterdir()) == ["res-1"]  # no staging left
    assert skill_bundles.remove("res-1") is True
    assert not path.exists()
    assert skill_bundles.remove("res-1") is False


@pytest.mark.parametrize(
    "refs, needle",
    [
        ([{"id": "tdd", "revision": "0" * 64}], "changed since this version was saved"),
        ([{"id": "missing", "revision": "r"}], "not in your skills library"),
        ([], "at least one skill"),
        ([{"id": "Bad Id", "revision": "r"}], "malformed"),
        ([{"id": "tdd"}], "{id, revision}"),
    ],
)
def test_a_bundle_that_cannot_be_proved_is_refused_and_leaves_nothing(refs, needle):
    _skill("tdd")
    with pytest.raises(skill_bundles.BundleRefused) as e:
        skill_bundles.build("res-2", refs)
    assert needle in str(e.value)
    assert not skill_bundles.root().exists() or list(skill_bundles.root().iterdir()) == []


def test_a_bundle_is_never_replaced_and_ids_are_checked():
    tdd = _skill("tdd")
    skill_bundles.build("res-3", [tdd])
    with pytest.raises(skill_bundles.BundleRefused, match="already has a bundle"):
        skill_bundles.build("res-3", [tdd])
    with pytest.raises(skill_bundles.BundleRefused, match="invalid bundle id"):
        skill_bundles.build("../escape", [tdd])


def test_an_edit_that_lands_first_refuses_the_build():
    tdd = _skill("tdd")
    _edit_in_place("tdd", SKILL_MD + "something else")
    with pytest.raises(skill_bundles.BundleRefused, match="changed since"):
        skill_bundles.build("res-4", [tdd])


def test_an_edit_racing_the_build_waits_for_it_so_the_bundle_is_never_a_mix(monkeypatch):
    """The check and the copy share ONE shared lock: an editor (exclusive) blocks until the bundle
    is built, so the bundle holds the pinned tree whole, and the edit lands afterwards."""
    tdd = _skill("tdd", extra={"b.md": "second file"})
    inside, release = threading.Event(), threading.Event()
    real = skill_bundles.files.read

    def slow(fd):
        tree = real(fd)
        inside.set()
        release.wait(3)
        return tree

    monkeypatch.setattr(skill_bundles.files, "read", slow)
    built: dict = {}
    edited: dict = {}
    builder = threading.Thread(
        target=lambda: built.update(path=skill_bundles.build("res-5", [tdd]))
    )
    builder.start()
    assert inside.wait(3)
    editor = threading.Thread(
        target=lambda: (
            _edit_in_place("tdd", SKILL_MD + "edited"),
            edited.update(at=time.monotonic()),
        )
    )
    editor.start()
    time.sleep(0.4)
    assert not edited, "the edit landed while the bundle was being built"
    release.set()
    builder.join(5)
    done_at = time.monotonic()
    editor.join(5)
    assert edited["at"] <= done_at + 5
    path = built["path"]
    assert (path / "tdd" / "SKILL.md").read_text().endswith("Write the failing test first.")
    assert (path / "tdd" / "b.md").read_text() == "second file"
    assert lib_skills.get("tdd")["revision"] != tdd["revision"]  # the edit did land, after


# ---- the MCP server -----------------------------------------------------------------------------


@pytest.fixture
def bundle():
    tdd = _skill("tdd", extra={"ref/notes.md": "deeper notes"})
    _skill("unbound")
    return skill_bundles.build("res-mcp", [tdd])


def _call(server, name, args=None):
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    result = reply["result"]
    return result["isError"], result["content"][0]["text"]


def test_skills_list_names_each_bound_skill_and_its_files(bundle):
    err, text = _call(skills_mcp.Server(str(bundle)), "skills_list", {})
    assert not err
    assert "`tdd` — Test-driven development: Red test first, then green." in text
    assert "SKILL.md" in text and "ref/notes.md" in text
    assert "unbound" not in text  # in the library, not in this specialist


def test_skill_read_serves_bound_files_only(bundle):
    s = skills_mcp.Server(str(bundle))
    err, text = _call(s, "skill_read", {"id": "tdd"})
    assert not err and text.rstrip().endswith("Write the failing test first.")
    err, text = _call(s, "skill_read", {"id": "tdd", "path": "ref/notes.md"})
    assert not err and "deeper notes" in text
    for bad in [
        {"id": "unbound"},
        {"id": "tdd", "path": "../unbound/SKILL.md"},
        {"id": "tdd", "path": "/etc/passwd"},
        {"id": "tdd", "path": "ref/../SKILL.md"},
        {"id": "tdd", "path": ".hidden"},
        {"id": "tdd", "path": "nope.md"},
        {"id": "../tdd"},
        {"id": "tdd", "extra": 1},
    ]:
        err, _ = _call(s, "skill_read", bad)
        assert err, bad


def test_symlinks_fifos_and_hard_links_in_a_bundle_are_refused(bundle, tmp_path):
    # Each refusal on its own: the symlinked secret has ONE link, so only O_NOFOLLOW stops it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET")
    linked = tmp_path / "linked.txt"
    linked.write_text("TOP SECRET")
    skill = bundle / "tdd"
    os.symlink(outside / "secret.txt", skill / "link.md")
    os.symlink(outside, skill / "dirlink")
    os.mkfifo(skill / "pipe.md")
    os.link(linked, skill / "hard.md")
    s = skills_mcp.Server(str(bundle))
    for path in ("link.md", "dirlink/secret.txt", "pipe.md", "hard.md"):
        err, text = _call(s, "skill_read", {"id": "tdd", "path": path})
        assert err and "TOP SECRET" not in text, path
    _, listed = _call(s, "skills_list", {})
    assert "link.md" not in listed and "TOP SECRET" not in listed


def test_a_replaced_bundle_is_refused(bundle):
    s = skills_mcp.Server(str(bundle))
    os.rename(bundle, str(bundle) + ".old")
    bundle.mkdir(mode=0o700)
    err, text = _call(s, "skills_list", {})
    assert err and "replaced" in text


def test_the_server_speaks_mcp_and_has_no_write_tool(bundle):
    s = skills_mcp.Server(str(bundle))
    init = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "battlelab-skills"
    tools = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["skills_list", "skill_read"]
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_main_refuses_without_an_absolute_bundle(monkeypatch, capsys):
    monkeypatch.delenv(skills_mcp.ENV_BUNDLE, raising=False)
    assert skills_mcp.main() == 2
    monkeypatch.setenv(skills_mcp.ENV_BUNDLE, "relative/path")
    assert skills_mcp.main() == 2


# ---- wiring -------------------------------------------------------------------------------------


@pytest.fixture
def real_bundle() -> str:
    """The launch seams accept only a bundle this module built (`skill_bundles.verify`)."""
    return str(skill_bundles.build("res-wire", [_skill("wired")]))


def _manifest_for(kind: str | None):
    """A registered agent's manifest with this tool channel, asked of the manifests."""
    for p in engines.all_providers():
        m = p.manifest
        if m and m.identity.kind == "agent" and m.tools_mcp == kind and m.runtime == "pty":
            return m
    pytest.skip(f"no terminal agent with tools.mcp={kind!r}")


def _skills_entry_env(launch: agent_tools.McpLaunch, kind: str) -> dict:
    if kind == agent_tools.CLAUDE_FLAG:
        flag, payload = launch.suffix
        assert flag == "--mcp-config"
        servers = json.loads(payload)["mcpServers"]
        assert set(servers) == {agent_tools.SERVER_NAME, agent_tools.SKILLS_SERVER_NAME}
        return servers[agent_tools.SKILLS_SERVER_NAME]["env"]
    if kind == agent_tools.CODEX_OVERRIDE:
        import tomllib

        pairs = [a for a in launch.prefix if a != "-c"]
        table = {}
        for pair in pairs:
            key, _, value = pair.partition("=")
            table[key] = tomllib.loads(f"v = {value}")["v"]  # every value is valid TOML
        assert f"mcp_servers.{agent_tools.SERVER_NAME}.command" in table
        return table[f"mcp_servers.{agent_tools.SKILLS_SERVER_NAME}.env"]
    content = json.loads(launch.env[agent_tools.OPENCODE_CONFIG_ENV])
    assert set(content["mcp"]) >= {agent_tools.SERVER_NAME, agent_tools.SKILLS_SERVER_NAME}
    assert content["keep"] == 1  # the existing config is merged, never replaced
    return content["mcp"][agent_tools.SKILLS_SERVER_NAME]["environment"]


@pytest.mark.parametrize("kind", sorted(kinds.MCP_KINDS))
def test_every_tool_channel_carries_the_skills_server_with_its_bundle(kind, tmp_path, real_bundle):
    from agent_sessions import memory

    manifest = _manifest_for(kind)
    memory_env = {k: str(tmp_path / k.lower()) for k in memory.ENV_KEYS}
    base = {agent_tools.OPENCODE_CONFIG_ENV: json.dumps({"keep": 1})}
    launch = agent_tools.terminal_launch(manifest, memory_env, base, skills_bundle=real_bundle)
    assert _skills_entry_env(launch, kind)[agent_tools.SKILLS_ENV] == real_bundle


def test_skills_without_memory_still_ride_alone(real_bundle):
    manifest = _manifest_for(agent_tools.CLAUDE_FLAG)
    launch = agent_tools.terminal_launch(manifest, None, {}, skills_bundle=real_bundle)
    servers = json.loads(launch.suffix[1])["mcpServers"]
    assert set(servers) == {agent_tools.SKILLS_SERVER_NAME}


def test_a_launch_that_cannot_carry_skills_is_refused_never_launched_without_them(real_bundle):
    with pytest.raises(agent_tools.SkillsRefused, match="cannot load skills"):
        agent_tools.terminal_launch(_manifest_for(None), None, {}, skills_bundle=real_bundle)
    opencode = _manifest_for(agent_tools.OPENCODE_CONTENT)
    with pytest.raises(agent_tools.SkillsRefused, match="not a JSON object"):
        agent_tools.terminal_launch(
            opencode, None, {agent_tools.OPENCODE_CONFIG_ENV: "[1]"}, skills_bundle=real_bundle
        )
    # Without skills, the same unmergeable config still launches (memory is best-effort).
    assert not agent_tools.terminal_launch(opencode, None, {agent_tools.OPENCODE_CONFIG_ENV: "[1]"})


def test_only_a_real_private_bundle_can_be_handed_to_the_tool(real_bundle, tmp_path):
    claude = _manifest_for(agent_tools.CLAUDE_FLAG)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    missing = str(skill_bundles.root() / "never-built")
    for bad, needle in [
        ("rel/x", "invalid skill bundle path"),
        (str(elsewhere), "not a skill bundle"),  # a private dir, but not a bundle
        (missing, "missing"),  # the tool would only ever answer "unavailable"
        (str(skill_bundles.root() / "../escape"), "not a skill bundle"),
    ]:
        with pytest.raises(agent_tools.SkillsRefused, match=needle):
            agent_tools.terminal_launch(claude, None, {}, skills_bundle=bad)
    os.chmod(real_bundle, 0o770)
    with pytest.raises(agent_tools.SkillsRefused, match="not private"):
        agent_tools.terminal_launch(claude, None, {}, skills_bundle=real_bundle)


@pytest.mark.parametrize("adapter", sorted(kinds.API_MCP_KINDS))
def test_api_clients_carry_the_skills_server_in_their_own_protocol_slot(adapter, real_bundle):
    tools = native_worker.tool_servers({"adapter": adapter, "skills_bundle": real_bundle})
    assert [t.name for t in tools] == [agent_tools.SKILLS_SERVER_NAME]
    name = agent_tools.SKILLS_SERVER_NAME
    claude = json.loads(agent_tools.claude_mcp_config(tools))["mcpServers"][name]
    assert claude["env"][agent_tools.SKILLS_ENV] == real_bundle
    codex = agent_tools.codex_thread_config(tools)[f"mcp_servers.{name}"]
    assert codex["env"][agent_tools.SKILLS_ENV] == real_bundle
    acp = agent_tools.acp_servers(tools)[0]
    assert (
        acp["name"] == name and {"name": agent_tools.SKILLS_ENV, "value": real_bundle} in acp["env"]
    )


def test_an_api_adapter_without_mcp_or_a_bad_bundle_refuses_the_spawn(real_bundle):
    with pytest.raises(native_worker.WorkerError, match="cannot load skills"):
        native_worker.tool_servers({"adapter": "openai-chat", "skills_bundle": real_bundle})
    for bad in ("rel/x", 7, ["x"], "", str(skill_bundles.root() / "never-built")):
        with pytest.raises(native_worker.WorkerError, match="invalid skill bundle"):
            native_worker.tool_servers({"adapter": "claude-stream-json", "skills_bundle": bad})
    assert native_worker.tool_servers({"adapter": "claude-stream-json"}) == []


#: A protocol-valid native id per adapter (claude and codex take UUIDs, opencode `ses_…`).
_NATIVE = {"opencode-acp": "ses_0123456789abcdefABCDEF0123"}


def _worker(adapter: str, bundle: str, tmp_path):
    from agent_sessions import native_ipc

    return native_worker.Worker(
        {
            "worker_id": str(uuid.uuid4()),
            "session_key": "claude:00000000-0000-4000-8000-000000000001",
            "adapter": adapter,
            "journal_root": str(tmp_path),
            "capability": native_ipc.Capability.create()._value,
            "connection_id": str(uuid.uuid4()),
            "binary": "/bin/true",
            "mode": "resume",
            "native_id": _NATIVE.get(adapter, str(uuid.uuid4())),
            "cwd": str(tmp_path),
            "skills_bundle": bundle,
        },
        tmp_path,
    )


class _Stop(Exception):
    pass


@pytest.mark.parametrize("adapter", sorted(kinds.API_MCP_KINDS))
def test_every_worker_spawn_path_carries_the_skills_server(adapter, real_bundle, tmp_path):
    """Drive the worker's own spawn path per protocol and read what it sends."""
    import asyncio

    worker = _worker(adapter, real_bundle, tmp_path)
    if adapter == "claude-stream-json":
        sent = json.dumps(worker.argv())
    else:
        frames: list = []

        async def request(frame):
            frames.append(frame)
            if len(frames) > 1:  # the handshake answers; the session frame is the one we want
                raise _Stop

        async def write(frame):
            pass

        worker.request = request
        worker.write = write
        with pytest.raises(_Stop):
            asyncio.run(worker.initialize())
        sent = json.dumps(frames)
    assert agent_tools.SKILLS_SERVER_NAME in sent
    assert json.dumps(real_bundle)[1:-1] in sent


# ---- review-driven regressions (#1695) -----------------------------------------------------------


def test_files_with_ordinary_names_are_listed_and_served():
    odd = {"my notes.md": "spaces", "Über.md": "umlaut", ".env.md": "dot", "-x.md": "dash"}
    tdd = _skill("tdd", extra={**odd, "ref/a b/c.md": "nested with space"})
    s = skills_mcp.Server(str(skill_bundles.build("res-odd", [tdd])))
    _, listed = _call(s, "skills_list", {})
    for name in [*odd, "ref/a b/c.md"]:
        assert name in listed, name
        err, text = _call(s, "skill_read", {"id": "tdd", "path": name})
        assert not err and (odd.get(name) or "nested with space") in text, name


def test_hard_links_are_not_listed_and_a_stray_root_entry_breaks_nothing(bundle, tmp_path):
    (tmp_path / "x").write_text("x")
    os.link(tmp_path / "x", bundle / "tdd" / "hard.md")
    (bundle / "stray").write_text("not a skill")  # a name that would match a skill id
    _, listed = _call(skills_mcp.Server(str(bundle)), "skills_list", {})
    assert "hard.md" not in listed and "`tdd`" in listed and "stray" not in listed


def test_nested_bundle_directories_are_0700_whatever_the_umask():
    old = os.umask(0o002)
    try:
        path = skill_bundles.build(
            "res-deep", [_skill("deep", extra={"a/b/c.md": "c", "a/b/d/e.md": "e"})]
        )
    finally:
        os.umask(old)
    for d in [
        path,
        path / "deep",
        path / "deep" / "a",
        path / "deep" / "a" / "b",
        path / "deep" / "a" / "b" / "d",
    ]:
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


@pytest.mark.parametrize("where", ["write", "open"])
def test_a_failing_write_is_a_named_refusal_and_leaves_nothing(monkeypatch, where):
    tdd = _skill("tdd")
    real_open = os.open

    def failing_open(path, flags, *a, **k):
        if flags & os.O_CREAT and str(path) == "SKILL.md":
            raise FileNotFoundError(2, "gone")
        return real_open(path, flags, *a, **k)

    def failing_write(fd, data):
        raise OSError(28, "No space left on device")

    # A scoped patch: `monkeypatch.undo()` would also drop conftest's store-path pins.
    with monkeypatch.context() as m:
        if where == "write":
            m.setattr(skill_bundles.os, "write", failing_write)
        else:
            m.setattr(skill_bundles.os, "open", failing_open)
        with pytest.raises(skill_bundles.BundleRefused, match="could not be written"):
            skill_bundles.build("res-fail", [tdd])
    assert [p.name for p in skill_bundles.root().iterdir()] == []


def test_a_symlinked_or_unsafe_root_is_refused(tmp_path, monkeypatch):
    tdd = _skill("tdd")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = skill_bundles.root()
    root.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, root)
    with pytest.raises(skill_bundles.BundleRefused, match="unavailable or unsafe"):
        skill_bundles.build("res-link", [tdd])
    assert list(target.iterdir()) == []  # nothing built through the link


def test_a_symlinked_skill_directory_in_the_library_is_refused():
    tdd = _skill("tdd")
    lib = lib_skills.storage.skills_root()
    os.rename(lib / "tdd", lib / "tdd-real")
    os.symlink(lib / "tdd-real", lib / "tdd")
    with pytest.raises(skill_bundles.BundleRefused, match="can't be read"):
        skill_bundles.build("res-sym", [tdd])


def test_publish_never_replaces_even_an_empty_directory(monkeypatch):
    """An empty directory that appears at the final name after every check: only the atomic
    no-replace rename stands between it and being silently replaced."""
    tdd = _skill("tdd")
    skill_bundles.root().mkdir(parents=True, mode=0o700, exist_ok=True)
    real = skill_bundles._exists_at

    def racing(dfd, name):
        if name == "res-race":
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=dfd)
            return False  # every check misses it
        return real(dfd, name)

    monkeypatch.setattr(skill_bundles, "_exists_at", racing)
    with pytest.raises(skill_bundles.BundleRefused, match="already has a bundle"):
        skill_bundles.build("res-race", [tdd])
    assert list((skill_bundles.root() / "res-race").iterdir()) == []  # the empty dir is untouched


def test_remove_survives_a_read_only_inner_directory_and_never_follows_links(tmp_path):
    path = skill_bundles.build("res-rm", [_skill("tdd", extra={"ref/x.md": "x"})])
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious").write_text("p")
    os.symlink(keep, path / "tdd" / "link")
    os.chmod(path / "tdd" / "ref", 0o500)  # an agent took away the write bit
    assert skill_bundles.remove("res-rm") is True
    assert not path.exists()
    assert [p.name for p in skill_bundles.root().iterdir()] == []
    assert (keep / "precious").read_text() == "p"


def test_stale_staging_is_swept_and_a_live_one_is_not():
    tdd = _skill("tdd")
    root = skill_bundles.root()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    old, fresh = root / ".staging-old-aaaa", root / ".staging-live-bbbb"
    old.mkdir(mode=0o700)
    fresh.mkdir(mode=0o700)
    past = time.time() - skill_bundles.STALE_S - 60
    os.utime(old, (past, past))
    skill_bundles.build("res-sweep", [tdd])
    names = sorted(p.name for p in root.iterdir())
    assert ".staging-old-aaaa" not in names and ".staging-live-bbbb" in names


def test_codex_toml_carries_a_del_character_validly(tmp_path):
    import tomllib

    value = agent_tools._toml('a\x7fb "q" \\ ü')
    assert tomllib.loads(f"v = {value}")["v"] == 'a\x7fb "q" \\ ü'


def test_a_lone_surrogate_in_a_path_is_a_tool_error_and_the_server_keeps_answering(bundle):
    """Through the real stdio loop: a raw JSON `\\ud800` must not end the process."""
    import io

    lines = [
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"skill_read",'
        '"arguments":{"id":"tdd","path":"x\\ud800.md"}}}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"skills_list",'
        '"arguments":{}}}',
    ]
    stdin = io.BytesIO(("\n".join(lines) + "\n").encode())
    stdout = io.BytesIO()
    skills_mcp.serve(stdin, stdout, skills_mcp.Server(str(bundle)))
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["isError"] is True
    assert replies[1]["result"]["isError"] is False
    assert "`tdd`" in replies[1]["result"]["content"][0]["text"]


@pytest.mark.parametrize("how", ["unavailable", "einval"])
def test_without_atomic_no_replace_a_bundle_is_refused_never_check_then_rename(monkeypatch, how):
    import ctypes
    import errno as errno_

    tdd = _skill("tdd")
    if how == "unavailable":
        monkeypatch.setattr(skill_bundles, "_RENAMEAT2", None)
    else:

        def no_flag(*_a):
            ctypes.set_errno(errno_.EINVAL)
            return -1

        monkeypatch.setattr(skill_bundles, "_RENAMEAT2", no_flag)
    with pytest.raises(
        skill_bundles.BundleRefused, match="cannot publish a skill bundle atomically"
    ):
        skill_bundles.build("res-atomic", [tdd])
    assert [p.name for p in skill_bundles.root().iterdir()] == []  # nothing published, no staging


def _bundle_names() -> list[str]:
    root = skill_bundles.root()
    return sorted(p.name for p in root.glob("*")) if root.exists() else []


def test_proving_pins_refuses_a_changed_or_missing_skill_and_copies_nothing():
    tdd = _skill("tdd")
    skill_bundles.prove_pins([tdd])  # current: no error
    before = _bundle_names()
    _edit_in_place("tdd", SKILL_MD + "something else")
    with pytest.raises(skill_bundles.BundleRefused, match="changed since"):
        skill_bundles.prove_pins([tdd])
    with pytest.raises(skill_bundles.BundleRefused, match="not in your skills library"):
        skill_bundles.prove_pins([{"id": "nope", "revision": tdd["revision"]}])
    after = _bundle_names()
    assert after == before
