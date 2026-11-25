# ruff: noqa: F811 — pytest fixtures imported from test_native_runtime
"""#2169: an API session runs on its SOURCE agent's account, fixed when the session is created.

The real worker and the scripted agents of `test_native_runtime` run every generation here. The
pool is the source agent's own (Claude's, Codex's), the choice is recorded under the API session's
logical key only, and every generation reads it: a later pick never moves a session, and a session
with no assignment (one created before this) stays on the default login. Refusals leave nothing
behind. No real CLI and no real login: the homes are the account store's own, in this test's HOME.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_sessions import agent_account_store as store
from agent_sessions import agent_accounts, native_ownership, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from test_native_runtime import (  # noqa: F401 — fixtures
    ENGINES,
    _session_limit_of,
    frames,
    host,
    ident,
    project,
    settle,
)

POOLED = ["claude", "codex"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def configs(host, tmp_path, monkeypatch):
    """Every generation config the web process writes, in order (the worker forgets its own).
    Also this test's own account store: the suite-wide sandbox is shared between tests."""
    monkeypatch.setenv(agent_accounts.ROOT_ENV, str(tmp_path / "agent-accounts"))
    seen: list[dict] = []
    real = native_state.write_config

    def record(worker_id, doc):
        seen.append(json.loads(json.dumps(doc)))
        return real(worker_id, doc)

    monkeypatch.setattr(native_state, "write_config", record)
    # A shell this suite runs in may itself be on an account: the default home is the vendor's
    # own under this test's HOME, as it is for the API child.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return seen


def _source(source: str):
    return registry._BY_ID[source]


def _assignments() -> dict:
    path = agent_accounts.root() / "state.json"
    return json.loads(path.read_text())["assignments"] if path.exists() else {}


def _pick(source: str, label: str) -> tuple[str, Path]:
    """Add an account to the source agent's pool and make it the new-session pick."""
    prov = _source(source)
    rec = agent_accounts.recipe(prov.manifest.accounts_kind)
    agent_accounts.default_home(rec, {}).mkdir(parents=True, exist_ok=True)  # "started once"
    account = store.add(prov, label)["id"]
    store.set_new_sessions(prov, account)
    return account, agent_accounts.home(prov.engine_id, account)


async def _idle_out(key: str, monkeypatch) -> None:
    """Let the session's worker exit idle, so the next turn launches a new generation."""
    worker = (await runtime.snapshot(key))["native"]["worker"]
    for _ in range(200):
        if native_state.read_lifecycle(worker).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    assert native_state.read_lifecycle(worker)["phase"] == "exited"
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 60.0)


# --- a new session takes the pick, under its logical key ----------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("source", POOLED)
async def test_a_new_api_session_runs_on_its_source_agents_pick(host, project, configs, source):
    account, home = _pick(source, "B")
    op = ident()
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=op))[
        "session_key"
    ]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hi")
    snap, done = await settle(key, turn)
    assert done["reply"] == "echo:hi"
    # The agent ran in the account's home…
    assert {f["account_home"] for f in frames(project)} == {str(home)}
    # …because the generation was told the recipe's kind and the home, and nothing else.
    kind = _source(source).manifest.accounts_kind
    assert configs[-1]["account"] == {"kind": kind, "home": str(home)}
    # The choice sits under the API session's logical key only: never its native id (Claude's
    # separate uuid, Codex's late-bound thread) and never the source's console key.
    native = snap["native"]["native_id"]
    assert native and native != op
    assert set(_assignments()) == {key}
    assert _assignments()[key]["account"] == account


# --- every later generation reads it --------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("source", POOLED)
async def test_a_later_generation_keeps_the_account_after_the_pick_changes(
    host, project, configs, monkeypatch, source
):
    """Including Claude's relaunch of a session that wrote no history, which launches in protocol
    mode `create` again: that is a new generation, not a new session."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    _account, home = _pick(source, "B")
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    if source == "codex":  # Codex resumes a bound thread; Claude relaunches with no history
        first = ident()
        await runtime.submit_turn(key, operation_id=first, text="one")
        await settle(key, first)
    _pick(source, "C")  # a newer pick, and then the default login: neither moves this session
    store.set_new_sessions(_source(source), store.DEFAULT)
    await _idle_out(key, monkeypatch)
    launched = len(configs)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await settle(key, nxt)
    assert len(configs) == launched + 1
    later = configs[-1]
    if source == "claude":
        assert later["mode"] == "create"
    assert later["account"]["home"] == str(home)
    assert frames(project)[-1]["account_home"] == str(home)


@pytest.mark.anyio
async def test_a_skip_creation_keeps_its_create_time_account_through_start_and_a_retry(
    host, project, configs, monkeypatch
):
    """A skip-permissions Create writes a pending record and launches nothing. The pick it took
    is the one `start` (and the start after a failed one) runs on."""
    _account, home = _pick("claude", "B")
    op = ident()
    snap = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=op, bypass=True
    )
    key = snap["session_key"]
    assert snap["pending_start"] is True and not configs
    _pick("claude", "C")
    real = native_runtime._wait_ready
    armed = {"fail": True}

    async def once(worker_id):
        if armed.pop("fail", False):
            raise native_runtime.NativeError(503, "the native worker stopped: boom")
        return await real(worker_id)

    monkeypatch.setattr(native_runtime, "_wait_ready", once)
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    await runtime.start_session(key)
    assert len(configs) == 2
    assert {c["account"]["home"] for c in configs} == {str(home)}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_a_session_without_an_assignment_stays_on_the_default_login(
    host, project, configs, monkeypatch, mode
):
    """An API session created before this shipped has no row. Relaunched in either mode after an
    account is picked, it runs on the default login, as it always did."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    op = ident()
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=op))[
        "session_key"
    ]
    if mode == "resume":
        first = ident()
        await runtime.submit_turn(key, operation_id=first, text="one")
        await settle(key, first)
    path = agent_accounts.root() / "state.json"
    doc = json.loads(path.read_text())
    doc["assignments"].pop(key)  # as written before #2169
    path.write_text(json.dumps(doc))
    _pick("claude", "B")
    await _idle_out(key, monkeypatch)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await settle(key, nxt)
    assert configs[-1]["mode"] == mode
    assert "account" not in configs[-1]
    assert frames(project)[-1]["account_home"] is None
    assert key not in _assignments()  # reading never records


# --- refusals leave nothing behind ---------------------------------------------------------------


def _break(home: Path) -> None:
    """A shared entry that is no longer the link `build` made: two copies would exist."""
    link = home / "projects"
    link.unlink()
    link.mkdir()


@pytest.mark.anyio
async def test_a_refused_account_at_create_writes_no_session_and_leaks_no_slot(
    host, project, configs, monkeypatch
):
    _session_limit_of(monkeypatch, 1)
    _account, home = _pick("claude", "B")
    _break(home)
    op = ident()
    key = f"{ENGINES['claude']}:{op}"
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(ENGINES["claude"], str(project), operation_id=op)
    assert refused.value.status == 409 and "'B' cannot be used" in str(refused.value)
    assert str(home) not in str(refused.value)  # a label, never a path
    assert native_state.read_session(op) is None
    assert native_ownership.lookup(key) is None
    assert not host.launches and not configs
    # A replay of the same operation resolves to the same account and is refused the same way.
    with pytest.raises(runtime.StructuredError):
        await runtime.create_session(ENGINES["claude"], str(project), operation_id=op)
    # The one slot was handed back: a session on the default login starts.
    store.set_new_sessions(_source("claude"), store.DEFAULT)
    await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident())
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_a_refused_account_at_the_first_generation_reserves_no_ownership(
    host, project, configs, monkeypatch
):
    """A skip creation's `start` is its first generation. Refused before the specialist slot and
    the ownership reservation, so no pending creation refuses the source's console launches."""
    _session_limit_of(monkeypatch, 1)
    account, _home = _pick("claude", "B")
    op = ident()
    key = (
        await runtime.create_session(ENGINES["claude"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    store.remove(_source("claude"), account)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.start_session(key)
    assert refused.value.status == 409 and "was removed" in str(refused.value)
    assert native_ownership.lookup(key) is None
    assert not native_ownership.source_snapshot(
        native_ownership.source_identity(_source("claude"))
    ).pending
    assert not host.launches and not configs
    store.set_new_sessions(_source("claude"), store.DEFAULT)
    await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident())
    assert len(host.launches) == 1  # the slot was not leaked


@pytest.mark.anyio
async def test_a_refused_account_at_a_later_generation_spawns_nothing(
    host, project, configs, monkeypatch
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    account, _home = _pick("claude", "B")
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident()))[
        "session_key"
    ]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="one")
    await settle(key, first)
    await _idle_out(key, monkeypatch)
    store.remove(_source("claude"), account)
    launched = len(host.launches)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(key, operation_id=ident(), text="two")
    assert refused.value.status == 409 and "was removed" in str(refused.value)
    assert len(host.launches) == launched


def test_every_refusal_point_comes_before_what_it_must_not_leave_behind():
    """Where each call sits (#2169): at Create before the slot and the record; at the first
    generation before the specialist slot and the ownership reservation; at a later generation
    before the specialist slot and the grant set."""
    import inspect

    def order(fn, *names):
        text = inspect.getsource(fn)
        at = [text.index(n) for n in names]
        assert at == sorted(at), (fn.__name__, names)

    order(native_runtime._create_admitted, "_account(", "_admit_session(", "write_session(")
    order(
        native_runtime._reserve_and_launch_admitted,
        "_account(",
        "_specialist_claim(",
        "native_ownership.reserve(",
    )
    order(native_runtime._launch_generation, "_account(", "_specialist_claim(", "grants.issue(")


# --- ownership and the agents without accounts are unchanged -----------------------------------


@pytest.mark.anyio
async def test_the_console_still_cannot_take_an_api_sessions_history(host, project, configs):
    _pick("claude", "B")
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident()))[
        "session_key"
    ]
    native = (await runtime.snapshot(key))["native"]["native_id"]
    with pytest.raises(native_ownership.OwnershipError) as owned:
        native_ownership.check_console(_source("claude"), native)
    assert owned.value.code == "owned"
    assert f"claude:{native}" not in _assignments()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["opencode", "copilot"])
async def test_an_api_agent_whose_source_has_no_accounts_never_reads_the_store(
    host, project, configs, source
):
    agent_accounts.ensure_root()
    (agent_accounts.root() / "state.json").write_text("{ not json")  # would refuse if read
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hi")
    await settle(key, turn)
    assert configs and all("account" not in c for c in configs)


# --- the worker applies it ------------------------------------------------------------------------


def test_the_worker_sets_the_home_and_drops_outranking_credentials():
    from agent_sessions import native_worker

    base = {"HOME": "/h", "ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t", "KEEP": "1"}
    config = {"account": {"kind": "claude-config-dir", "home": "/accounts/claude/a-1"}}
    env = native_worker.account_environment(config, dict(base))
    assert env == {"HOME": "/h", "KEEP": "1", "CLAUDE_CONFIG_DIR": "/accounts/claude/a-1"}
    assert native_worker.account_environment({}, dict(base)) == base  # the default login


@pytest.mark.parametrize(
    "value",
    [
        "claude-config-dir",
        {"kind": "claude-config-dir"},
        {"kind": "claude-config-dir", "home": "/a", "extra": 1},
        {"kind": "no-such-kind", "home": "/a"},
        {"kind": None, "home": "/a"},
        {"kind": "claude-config-dir", "home": "relative"},
        {"kind": "claude-config-dir", "home": "/a\n"},
        {"kind": "claude-config-dir", "home": "/a\0"},
        {"kind": "claude-config-dir", "home": 7},
    ],
)
def test_the_worker_refuses_an_account_it_cannot_trust(value):
    from agent_sessions import native_worker

    with pytest.raises(native_worker.WorkerError, match="invalid account"):
        native_worker.account_environment({"account": value}, {})


def test_the_account_is_applied_before_the_forced_tree():
    import inspect

    from agent_sessions import native_worker

    text = inspect.getsource(native_worker.child_environment)
    assert text.index("account_environment(") < text.index("forced_environment(")
