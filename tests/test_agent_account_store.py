"""Agent accounts, P1a (#1820): the pool, a session's account, the launch seams and the routes.

Everything runs against a fixture recipe and temporary homes; no real CLI and no real login. The
real-CLI proof is `scripts/agent-accounts-p0`, run by a person.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import agent_account_store as store
from agent_sessions import agent_accounts
from agent_sessions.plugins import kinds

ENGINE = "agent"
RECIPE = {
    "home_env": "AGENT_HOME",
    "default_home": "~/.agent",
    "private": ("login.json",),
    "shared_dirs": ("sessions", "skills"),
    "shared_files": ("settings.json",),
    "clear_env": ("AGENT_API_KEY",),
    "login": ("login",),
    "status": ("login", "status"),
}


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A provider naming the fixture recipe, with its default home and a pinned accounts dir."""
    monkeypatch.setitem(kinds.ACCOUNT_RECIPES, "fixture-home", RECIPE)
    monkeypatch.setenv(agent_accounts.ROOT_ENV, str(tmp_path / "accounts"))
    default = tmp_path / "default"
    (default / "sessions").mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(default))
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    prov = SimpleNamespace(
        engine_id=ENGINE,
        manifest=SimpleNamespace(accounts_kind="fixture-home", launch=None),
        entrypoint_path=lambda: str(tmp_path / "bin" / "agent"),
    )
    return prov


def _state() -> dict:
    return json.loads((agent_accounts.root() / "state.json").read_text())


# --- an agent without accounts is untouched ------------------------------------------------------


def test_an_agent_without_accounts_reads_nothing_and_launches_as_before(pool, tmp_path):
    plain = SimpleNamespace(engine_id="plain", manifest=SimpleNamespace(accounts_kind="none"))
    agent_accounts.ensure_root()
    (agent_accounts.root() / "state.json").write_text("{ not json")  # would refuse if read
    assert store.for_launch(plain, ["plain:x"], new=True) is None
    assert store.for_launch(plain, ["plain:x"], new=False) is None


def test_the_default_pick_is_recorded_and_launches_on_the_default_login(pool):
    assert store.for_launch(pool, [f"{ENGINE}:s1"], new=True) is None
    assert _state()["assignments"][f"{ENGINE}:s1"]["account"] == store.DEFAULT
    assert store.assignment([f"{ENGINE}:s1"]) is None
    assert store.for_launch(pool, [f"{ENGINE}:s1"], new=False) is None


def test_a_session_created_on_the_default_login_keeps_it_when_the_pick_changes(pool):
    """Hermes on #2048: default, then the pick moves to B, then the same key relaunches."""
    store.for_launch(pool, [f"{ENGINE}:s1"], new=True)
    b = store.add(pool, "B")["id"]
    store.set_new_sessions(pool, b)
    assert store.for_launch(pool, [f"{ENGINE}:s1"], new=True) is None  # relaunch before history
    assert store.for_launch(pool, [f"{ENGINE}:s1"], new=False) is None  # resume
    assert store.for_launch(pool, [f"{ENGINE}:s2"], new=True).account_id == b  # a new one moves


# --- the pool ------------------------------------------------------------------------------------


def test_adding_an_account_builds_its_home_and_lists_no_path(pool):
    acc = store.add(pool, "  Max   2 ")
    assert acc["label"] == "Max 2" and acc["id"].startswith("a-")
    home = agent_accounts.home(ENGINE, acc["id"])
    assert (home / "sessions").is_symlink()
    listing = store.listing(pool)
    assert listing["accounts"][0] == {
        "id": acc["id"],
        "label": "Max 2",
        "created_at": acc["created_at"],
        "problems": [],
    }
    assert str(agent_accounts.root()) not in json.dumps(listing)
    mode = stat.S_IMODE(os.stat(agent_accounts.root() / "state.json").st_mode)
    assert mode == 0o600


@pytest.mark.parametrize("label", ["", "   ", "x" * 41, "a\x07b", "Default", 7])
def test_a_label_is_short_printable_text(pool, label):
    with pytest.raises(store.StoreError):
        store.add(pool, label)


def test_labels_are_unique_and_the_pool_is_bounded(pool):
    store.add(pool, "One")
    with pytest.raises(store.StoreError, match="label"):
        store.add(pool, "one")
    for n in range(2, store.MAX_ACCOUNTS + 1):
        store.add(pool, f"Acc {n}")
    with pytest.raises(store.StoreError, match="at most"):
        store.add(pool, "Sixth")


def test_reorder_names_every_account_once(pool):
    a, b = store.add(pool, "A")["id"], store.add(pool, "B")["id"]
    store.reorder(pool, [b, a])
    assert [r["id"] for r in store.listing(pool)["accounts"]] == [b, a]
    for bad in ([a], [a, a], [a, b, "a-00000000"], [a, 1], "ab"):
        with pytest.raises(store.StoreError):
            store.reorder(pool, bad)


def test_an_agent_without_accounts_has_no_pool(pool):
    plain = SimpleNamespace(engine_id="plain", manifest=SimpleNamespace(accounts_kind="none"))
    with pytest.raises(store.StoreError):
        store.add(plain, "x")


# --- a session's account -------------------------------------------------------------------------


def test_a_new_session_takes_the_pick_and_keeps_it_on_every_key(pool):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    launch = store.for_launch(pool, [f"{ENGINE}:new-1", f"{ENGINE}:new-1"], new=True)
    assert launch.account_id == acc and launch.home == agent_accounts.home(ENGINE, acc)
    # The resume finds it under the runtime key alone (a placeholder-keyed session).
    again = store.for_launch(pool, [f"{ENGINE}:real-id", f"{ENGINE}:new-1"], new=False)
    assert again.account_id == acc


def test_an_existing_session_without_an_assignment_stays_on_the_default_login(pool):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    assert store.for_launch(pool, [f"{ENGINE}:older"], new=False) is None


def test_a_relaunched_new_session_keeps_its_first_account(pool):
    a, b = store.add(pool, "A")["id"], store.add(pool, "B")["id"]
    store.set_new_sessions(pool, a)
    store.for_launch(pool, [f"{ENGINE}:s"], new=True)
    store.set_new_sessions(pool, b)
    assert store.for_launch(pool, [f"{ENGINE}:s"], new=True).account_id == a


def test_a_removed_account_refuses_its_sessions_and_keeps_its_home(pool):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    store.for_launch(pool, [f"{ENGINE}:s"], new=True)
    store.remove(pool, acc)
    assert agent_accounts.home(ENGINE, acc).is_dir()
    assert store.listing(pool)["new_sessions"] == store.DEFAULT
    with pytest.raises(store.AccountRefused, match="removed"):
        store.for_launch(pool, [f"{ENGINE}:s"], new=False)


def test_a_broken_home_refuses_the_launch(pool):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    store.for_launch(pool, [f"{ENGINE}:s"], new=True)
    link = agent_accounts.home(ENGINE, acc) / "sessions"
    link.unlink()
    link.mkdir()  # a second copy of the session store: never repaired, refused
    with pytest.raises(store.AccountRefused, match="Max 2"):
        store.for_launch(pool, [f"{ENGINE}:s"], new=False)


def test_an_unreadable_store_refuses_rather_than_falling_back(pool):
    store.add(pool, "Max 2")
    (agent_accounts.root() / "state.json").write_text('{"version": 99}')
    for new in (True, False):
        with pytest.raises(store.AccountRefused, match="cannot be read"):
            store.for_launch(pool, [f"{ENGINE}:s"], new=new)
    with pytest.raises(store.StoreError):
        store.add(pool, "Other")


def test_the_launch_sets_the_home_and_drops_outranking_credentials(pool):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    launch = store.for_launch(pool, [f"{ENGINE}:s"], new=True)
    env = {"AGENT_API_KEY": "sk-x", "AGENT_HOME": "/elsewhere", "KEEP": "1"}
    launch.apply(env)
    assert env == {"AGENT_HOME": str(launch.home), "KEEP": "1"}


# --- the seams -----------------------------------------------------------------------------------


def _webterm_env(monkeypatch, tmp_path, *, launch_env=None, account=None, forced=None):
    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    observed = []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop at the spawn boundary")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    prov = SimpleNamespace(engine_id=ENGINE, agent_env=lambda: dict(forced or {}))
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key=f"{ENGINE}:s",
            launch_provider=prov,
            launch_env=launch_env,
            account=account,
        )
    )
    [env] = observed
    return env


def test_the_terminal_launch_without_an_account_is_unchanged(pool, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_API_KEY", "sk-app")
    env = _webterm_env(monkeypatch, tmp_path, launch_env={"MEMORY": "m"})
    assert env["AGENT_API_KEY"] == "sk-app" and env["MEMORY"] == "m"


def test_the_terminal_launch_applies_the_account_between_the_brokers_and_the_agent(
    pool, monkeypatch, tmp_path
):
    acc = store.add(pool, "Max 2")["id"]
    store.set_new_sessions(pool, acc)
    launch = store.for_launch(pool, [f"{ENGINE}:s"], new=True)
    monkeypatch.setenv("AGENT_API_KEY", "sk-app")
    env = _webterm_env(
        monkeypatch,
        tmp_path,
        # A launch variable naming the credential is still dropped; the agent's own forced
        # environment still wins over the account.
        launch_env={"MEMORY": "m", "AGENT_API_KEY": "sk-launch"},
        account=launch,
        forced={"ISOLATION": "i"},
    )
    assert env["AGENT_HOME"] == str(launch.home)
    assert "AGENT_API_KEY" not in env
    assert env["MEMORY"] == "m" and env["ISOLATION"] == "i"
    forced = _webterm_env(monkeypatch, tmp_path, account=launch, forced={"AGENT_HOME": "/forced"})
    assert forced["AGENT_HOME"] == "/forced"


def _source(rel: str) -> str:
    return (Path(store.__file__).parent / rel).read_text()


def test_every_launch_seam_resolves_and_applies_the_account():
    terminal = _source("routes/terminal.py")
    assert "agent_account_store.for_launch" in terminal and "account=account_launch" in terminal
    headless = _source("headless_dispatch.py")
    assert "agent_account_store.for_launch" in headless
    assert "account_launch.apply(env)" in headless
    # The auth probe asks about the account's login, not the default one.
    assert '{"env": dict(env)} if account_launch is not None' in headless


def test_the_account_is_resolved_after_the_capacity_slot_in_both_seams():
    for rel in ("routes/terminal.py", "headless_dispatch.py"):
        text = _source(rel)
        assert text.index("session_capacity.admit") < text.index("agent_account_store.for_launch")


# --- the sign-in and the status ------------------------------------------------------------------


def _fake_cli(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "bin" / "agent"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize(("code", "expected"), [(0, True), (1, False), (2, None)])
def test_signed_in_is_the_status_commands_exit_code(pool, tmp_path, code, expected):
    acc = store.add(pool, "Max 2")["id"]
    _fake_cli(tmp_path, f"exit {code}\n")
    assert store.signed_in(pool, acc) is expected


def test_the_status_command_runs_in_the_account_with_no_app_secret(pool, tmp_path, monkeypatch):
    acc = store.add(pool, "Max 2")["id"]
    home = agent_accounts.home(ENGINE, acc)
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "app-secret")
    monkeypatch.setenv("AGENT_API_KEY", "sk-app")
    _fake_cli(
        tmp_path,
        f'[ "$AGENT_HOME" = "{home}" ] || exit 5\n'
        '[ -z "$AGENT_SESSIONS_SECRET_KEY$AGENT_API_KEY" ] || exit 6\n'
        '[ "$1 $2" = "login status" ] || exit 7\n'
        "exit 0\n",
    )
    assert store.signed_in(pool, acc) is True


def test_a_status_command_that_hangs_is_unknown(pool, tmp_path, monkeypatch):
    acc = store.add(pool, "Max 2")["id"]
    monkeypatch.setattr(store, "STATUS_TIMEOUT_S", 0.5)
    _fake_cli(tmp_path, "sleep 30\n")
    assert store.signed_in(pool, acc) is None


def test_the_signin_runs_the_recipes_login_in_the_accounts_home(pool, tmp_path, monkeypatch):
    from agent_sessions.plugins import process

    acc = store.add(pool, "Max 2")["id"]
    launch = store.for_signin(pool, acc)
    captured = {}

    def wrapped(argv, cwd, unit, seconds, *, probe_kind=None, agent_env=None):
        captured.update(argv=argv, cwd=cwd, seconds=seconds, agent_env=agent_env)
        raise process.ProcessError("stop before the spawn")

    monkeypatch.setattr(process, "_wrapped", wrapped)

    async def go():
        async with process.spawn(pool, "account-signin", cwd=launch.home, account=launch):
            pass

    with pytest.raises(process.ProcessError, match="stop before"):
        asyncio.run(go())
    assert captured["argv"] == [pool.entrypoint_path(), "login"]
    assert captured["agent_env"] == {"AGENT_HOME": str(launch.home)}
    assert captured["seconds"] == process.SIGNIN_SECONDS


def test_an_account_signin_names_its_account(pool, tmp_path):
    from agent_sessions.plugins import process

    async def go(purpose, account):
        async with process.spawn(pool, purpose, cwd=tmp_path, account=account):
            pass

    with pytest.raises(process.ProcessError, match="names its account"):
        asyncio.run(go("account-signin", None))
    acc = store.add(pool, "Max 2")["id"]
    with pytest.raises(process.ProcessError, match="names its account"):
        asyncio.run(go("signin", store.for_signin(pool, acc)))


# --- what the store never does -------------------------------------------------------------------


def test_the_store_never_opens_anything_inside_a_home():
    """Its only file is its own state document; homes are touched by `agent_accounts` alone."""
    tree = ast.parse(Path(store.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "open":
                assert ast.unparse(node.func.value) == "_path()", ast.unparse(node)


# --- the routes ----------------------------------------------------------------------------------


@pytest.fixture
def client(pool, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_sessions.routes import agent_accounts as routes

    monkeypatch.setattr(
        routes.registry, "get", lambda e: pool if e == ENGINE else None, raising=True
    )
    monkeypatch.setattr(routes.registry, "present_providers", lambda: [pool])
    app = FastAPI()
    calls = {"csrf": 0}

    def logged_in():
        return "admin"

    def csrf():
        calls["csrf"] += 1

    routes.register(
        app,
        logged_in=logged_in,
        csrf_guard=csrf,
        cfg=SimpleNamespace(username="admin"),
        must_change={"v": False},
    )
    return TestClient(app), calls


def test_the_routes_manage_the_pool(client):
    c, calls = client
    acc = c.post(f"/api/agent-accounts/{ENGINE}", json={"label": "Max 2"}).json()
    assert calls["csrf"] == 1
    listed = c.get("/api/agent-accounts").json()["agents"][0]
    assert listed["engine"] == ENGINE and listed["accounts"][0]["label"] == "Max 2"
    r = c.put(f"/api/agent-accounts/{ENGINE}/new-sessions", json={"account": acc["id"]})
    assert r.json()["new_sessions"] == acc["id"]
    r = c.patch(f"/api/agent-accounts/{ENGINE}/{acc['id']}", json={"label": "Max B"})
    assert r.json()["accounts"][0]["label"] == "Max B"
    r = c.delete(f"/api/agent-accounts/{ENGINE}/{acc['id']}")
    assert r.json()["accounts"] == [] and r.json()["new_sessions"] == store.DEFAULT


def test_the_routes_refuse_bad_input_and_unknown_agents(client):
    c, _ = client
    assert c.post("/api/agent-accounts/nope", json={"label": "x"}).status_code == 404
    assert c.post(f"/api/agent-accounts/{ENGINE}", json={"label": ""}).status_code == 409
    extra = {"label": "x", "path": "/etc"}
    assert c.post(f"/api/agent-accounts/{ENGINE}", json=extra).status_code == 422
    assert c.delete(f"/api/agent-accounts/{ENGINE}/a-00000000").status_code == 409


def test_every_write_route_takes_the_csrf_guard():
    from agent_sessions.routes import agent_accounts as routes

    tree = ast.parse(Path(routes.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                method = dec.func.attr
                deps = {kw.arg: ast.unparse(kw.value) for kw in dec.keywords}
                if method in {"post", "put", "patch", "delete"}:
                    assert deps.get("dependencies") == "write", node.name
                elif method == "get":
                    assert deps.get("dependencies") == "read", node.name
