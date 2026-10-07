"""#1311: the first-party Codex/Claude API clients — roster, readiness, listing, retirement."""

from __future__ import annotations

import shutil
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import chat_store, engines, native_runtime
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import FIRST_PARTY_DIR, feed, kinds, load_first_party
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures

CLIENTS = {
    "codex-api": ("codex", "codex-app-server"),
    "claude-api": ("claude", "claude-stream-json"),
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_first_party_clients_load_with_a_source_and_their_own_store():
    loaded = load_first_party()
    for eid, (source, kind) in CLIENTS.items():
        m = loaded.providers[eid].manifest
        assert m.runtime == "api" and m.api.kind == kind and m.api.source == source
        assert m.binary is m.launch is m.install is m.endpoint is None
        assert m.store.root == f"~/.local/share/agent-sessions/native/{eid}"
        assert eid in kinds.RESERVED_IDS
    roots = {loaded.providers[e].manifest.store.root for e in CLIENTS}
    assert len(roots) == 2 and "~/.local/share/agent-sessions/chat" not in roots


def test_a_local_manifest_cannot_take_a_client_id():
    doc = feed.entry({"manifest": _doc("codex-api"), "recipe": {"artifacts": []}}, signed=True)
    assert doc.manifest.id == "codex-api"  # the signed catalog may supersede the in-tree entry
    with pytest.raises(feed.FeedError):
        feed.entry({"manifest": _doc("codex-api"), "recipe": {"artifacts": []}}, signed=False)


def _doc(eid):
    import tomllib

    return tomllib.loads((FIRST_PARTY_DIR / eid / "plugin.toml").read_text())


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "systemd is not installed; native clients need a systemd user manager",
        "no systemd user manager is running for this account",
        "codex 0.159.3 or later is required for native mode",
        "binary not found",
    ],
)
def test_can_start_and_the_unavailable_list_follow_the_adapter_readiness(monkeypatch, reason):
    seen = []

    def readiness(prov):
        seen.append(prov.engine_id)
        return (reason is None, reason)

    monkeypatch.setattr(native_runtime, "readiness", readiness)
    prov = engines.get("codex-api")
    assert registry.can_start(prov) is (reason is None)
    listed = {row["id"]: row for row in registry.unavailable_api_clients()}
    if reason is None:
        assert "codex-api" not in listed
    else:
        assert listed["codex-api"] == {"id": "codex-api", "label": "Codex — API", "reason": reason}
    assert "codex-api" in seen
    # A console agent never reaches the native readiness probe.
    assert "codex" not in seen


@pytest.fixture
def roster(tmp_path):
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    registry.reload(first_party_dir=fp)
    yield fp
    registry.reload()


def test_a_disabled_source_lists_the_client_unavailable_and_keeps_it_retiring(roster):
    assert engines.get("codex-api") is not None
    shutil.rmtree(roster / "codex")  # the console agent goes away (disabled / removed)
    registry.reload(first_party_dir=roster)
    assert engines.get("codex-api") is None  # no new work
    retiring = engines.get_any("codex-api")
    assert retiring is not None and engines.is_retiring(retiring)  # never stranded
    assert retiring.manifest.runtime == "api"
    listed = {row["id"]: row for row in registry.unavailable_api_clients()}
    assert listed["codex-api"]["label"] == "Codex — API"
    assert "codex" in listed["codex-api"]["reason"]
    registry.reload(first_party_dir=roster)  # a second restart keeps it retiring, not removed
    assert engines.is_retiring(engines.get_any("codex-api"))
    shutil.copytree(FIRST_PARTY_DIR / "codex", roster / "codex")
    registry.reload(first_party_dir=roster)
    assert engines.get("codex-api") is not None  # back by itself with its source


def test_config_reports_unavailable_clients_and_new_session_engines(app_client, monkeypatch):
    monkeypatch.setattr(
        native_runtime, "readiness", lambda prov: (prov.engine_id == "claude-api", "too old")
    )
    cfg = app_client.get("/api/config").json()
    assert "claude-api" in cfg["new_session_engines"]
    assert "codex-api" not in cfg["new_session_engines"]
    assert {"id": "codex-api", "label": "Codex — API", "reason": "too old"} in cfg[
        "unavailable_clients"
    ]
    rows = {r["id"]: r for r in app_client.get("/api/engines").json()["engines"]}
    assert rows["codex-api"]["api"] == {
        "kind": "codex-app-server",
        "source": "codex",
        "unavailable_reason": "too old",
    }
    assert rows["claude-api"]["api"]["unavailable_reason"] is None
    assert rows["claude-api"]["supports_new"] is True and rows["claude-api"]["present"] is True
    assert rows["codex"]["api"] is None


def test_structured_create_has_no_bypass_field(app_client):
    r = app_client.post(
        "/api/structured/sessions",
        json={
            "engine": "codex-api",
            "cwd": "/tmp",
            "operation_id": str(uuid.uuid4()),
            "bypass": True,
        },
    )
    assert r.status_code == 422 and "bypass" in r.json()["detail"]


@pytest.mark.anyio
async def test_an_api_session_is_listed_from_its_journal(host, project):  # noqa: F811
    engine = ENGINES["codex"]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="what is in this folder?")
    await settle(key, turn)
    native = key.partition(":")[2]
    prov = engines.get(engine)
    if prov.kind is None:  # the fixture provider has no registry-attached store kind
        prov.attach_kind(chat_store.ChatStoreKind())
    row = prov.kind.lookup(native)
    assert row is not None and row.engine == engine and row.uuid == native
    assert row.cwd == str(project) and row.first_user_message == "what is in this folder?"
    assert [r.uuid for r in prov.kind.scan()] == [native]


@pytest.mark.anyio
async def test_retiring_client_history_stays_readable_but_takes_no_new_work(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    engine = ENGINES["codex"]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)
    assert (await runtime.snapshot(key))["read_only"] is None
    prov = engines.get(engine)
    prov.retiring = True
    monkeypatch.setattr(
        registry, "_BY_ID", {k: v for k, v in registry._BY_ID.items() if k != engine}
    )
    monkeypatch.setattr(registry, "_PROVIDERS", [p for p in registry._PROVIDERS if p is not prov])
    monkeypatch.setattr(registry, "_RETIRING", {engine: prov})
    snap = await runtime.snapshot(key)
    assert snap["read_only"] == engines.REMOVED_REASON
    assert [t["reply"] for t in snap["turns"]] == ["echo:hello"]
    page = await runtime.events(key, after=0)
    assert page["events"]
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="more")
    assert (await runtime.stop(key))["session_key"] == key


@pytest.fixture
def app_client(tmp_home, auth_cfg):
    from agent_sessions.main import create_app

    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        assert r.status_code in (302, 303)
        csrf = c.get("/api/config").json()["csrf"]
        c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
        yield c


def test_a_missing_cli_is_an_unavailable_reason_not_a_launch(host, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_BIN", str(tmp_path / "nowhere" / "codex"))
    prov = registry._BY_ID[ENGINES["codex"]]
    reason = registry.api_unavailable_reason(prov)
    assert reason and not registry.can_start(prov)
    assert not host.launches


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("/usr/bin/systemd-run", "systemd is not installed"),
        ("systemd/private", "no systemd user manager is running"),
    ],
)
def test_absent_systemd_is_an_unavailable_reason(tmp_path, monkeypatch, missing, expected):
    import os.path

    real = os.path.exists
    monkeypatch.setattr(
        native_runtime.os.path, "exists", lambda p: False if str(p).endswith(missing) else real(p)
    )
    monkeypatch.setattr(native_runtime.Host, "runtime_dir", lambda self: str(tmp_path))
    (tmp_path / "systemd").mkdir()
    (tmp_path / "systemd" / "private").touch()
    reason = native_runtime.Host().available()
    assert reason and expected in reason


@pytest.mark.anyio
async def test_an_api_sessions_transcript_is_its_turns_not_an_empty_chat_log(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    # Review of #1311: the chat fold skips every native record, so search, AI review, handoff
    # and the judge saw an empty conversation. The journal is folded instead.
    from pathlib import Path

    from agent_sessions import transcript

    engine = ENGINES["codex"]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    for text in ("hello", "again"):
        turn_id = ident()
        await runtime.submit_turn(key, operation_id=turn_id, text=text)
        await settle(key, turn_id)
    # The fixture provider was built with an empty env; the reader resolves the store through
    # the live env, as a real provider does. Point both at the same root.
    monkeypatch.setenv("AGENT_SESSIONS_CHAT_DIR", str(engines.get(engine).store_root()))
    turns = transcript.adapter_for(engine)(key.partition(":")[2], Path.home())
    assert [(t.role, t.text) for t in turns] == [
        ("user", "hello"),
        ("assistant", "echo:hello"),
        ("user", "again"),
        ("assistant", "echo:again"),
    ]


def test_only_an_in_tree_api_client_is_kept_retiring_without_masters():
    # Review of #1311: the always-retire rule is for the in-tree clients, which come back with
    # their source; an operator-removed catalog API plugin follows the ordinary master rule.
    text = (FIRST_PARTY_DIR / "codex-api" / "plugin.toml").read_text()
    assert registry._is_api_copy("codex-api", "plugin.toml", text.encode()) is True
    acme = text.replace('id = "codex-api"', 'id = "acme-api"').replace(
        'name = "codex-api"', 'name = "acme-api"'
    )
    assert registry._is_api_copy("acme-api", "plugin.toml", acme.encode()) is False
    # A console manifest is never an API copy.
    claude = (FIRST_PARTY_DIR / "claude" / "plugin.toml").read_bytes()
    assert registry._is_api_copy("claude", "plugin.toml", claude) is False


def test_api_clients_are_not_playbook_assignment_targets(monkeypatch):
    # Flow steps dispatch to terminal agents (headless_dispatch.require_pty) — review of #1311 —
    # so a ready API client is not offered as an assignment, while its console source is.
    from agent_sessions.playbooks import review, schema

    monkeypatch.setattr(native_runtime, "readiness", lambda prov: (True, None))
    assert registry.can_start(engines.get("codex-api"))
    step = {"id": "s", "actor": {"kind": schema.ACTOR_AGENT, "engine": "codex", "model": "default"}}
    bundle = {"flows": {"f": {"steps": [step]}}}
    _, facts = review._assignments(bundle, {"f:s": {"engine": "codex-api", "model": "default"}})
    assert facts["engines"]["codex-api"]["present"] is False


# ---- Archive stops the native worker first (Hermes on #1315) -----------------------------------


def _client(auth_cfg):
    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    c.headers.update(
        {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    )
    return c


async def _live_session(host, project):  # noqa: F811
    engine = ENGINES["codex"]
    prov = engines.get(engine)
    if prov.kind is None:
        prov.attach_kind(chat_store.ChatStoreKind())
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn_id = ident()
    await runtime.submit_turn(key, operation_id=turn_id, text="hello")
    await settle(key, turn_id)
    assert (await runtime.probe(key))["containment"] == "live"
    return key


@pytest.mark.anyio
async def test_archiving_an_api_session_stops_its_worker_before_recording_it(
    host,  # noqa: F811
    project,  # noqa: F811
    auth_cfg,
):
    import asyncio

    key = await _live_session(host, project)
    c = _client(auth_cfg)
    r = await asyncio.to_thread(c.post, f"/api/sessions/{key}/archive")
    assert r.status_code == 200, r.text
    assert (await runtime.probe(key))["containment"] == "gone"
    assert all(p.poll() is not None for p in host.procs.values())


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["unknown", "live"])
async def test_archive_refuses_when_the_worker_is_not_proved_gone(
    host,  # noqa: F811
    project,  # noqa: F811
    auth_cfg,
    monkeypatch,
    outcome,
):
    import asyncio

    from agent_sessions import metadata

    key = await _live_session(host, project)

    async def stop(session_key):
        return {"session_key": session_key, "containment": outcome}

    monkeypatch.setattr(runtime, "stop", stop)
    c = _client(auth_cfg)
    r = await asyncio.to_thread(c.post, f"/api/sessions/{key}/archive")
    assert r.status_code == 503 and outcome in r.json()["detail"]
    assert metadata.archive_override_under_lock(key) != "archived"
    # The bulk sweep skips it rather than archiving past it.
    row = engines.get(ENGINES["codex"]).kind.lookup(key.partition(":")[2])
    import dataclasses

    old = dataclasses.replace(row, last_mtime=1.0)
    monkeypatch.setattr(engines, "scan_all", lambda: iter([old]))
    r = await asyncio.to_thread(c.post, "/api/sessions/archive-older", json={"hours": 1})
    assert r.json() == {"archived": 0, "skipped": 1}
    assert metadata.archive_override_under_lock(key) != "archived"


@pytest.mark.anyio
async def test_a_removed_catalog_client_stays_reachable_while_a_worker_may_run(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    from agent_sessions import native_runtime

    key = await _live_session(host, project)
    engine = ENGINES["codex"]
    assert native_runtime.engine_may_have_workers(engine) is True
    text = (FIRST_PARTY_DIR / "codex-api" / "plugin.toml").read_text()
    custom = text.replace('id = "codex-api"', f'id = "{engine}"').replace(
        'name = "codex-api"', f'name = "{engine}"'
    )
    # As a CATALOG client (not an in-tree id), it is kept only while a worker may run.
    monkeypatch.setattr(kinds, "RESERVED_IDS", kinds.RESERVED_IDS - {engine})
    assert registry._is_api_copy(engine, "plugin.toml", custom.encode()) is True
    await runtime.stop(key)
    assert native_runtime.engine_may_have_workers(engine) is False
    assert registry._is_api_copy(engine, "plugin.toml", custom.encode()) is False


@pytest.mark.anyio
async def test_archive_refuses_when_the_lifecycle_record_is_missing(
    host,  # noqa: F811
    project,  # noqa: F811
    auth_cfg,
):
    # Hermes on #1315: a stop 404 (record missing/mismatched) is lost state, not "no worker" —
    # the worker launched under it is still running, so archive must fail closed.
    import asyncio

    from agent_sessions import metadata, native_state

    key = await _live_session(host, project)
    native = key.partition(":")[2]
    native_state.session_path(native).unlink()
    c = _client(auth_cfg)
    r = await asyncio.to_thread(c.post, f"/api/sessions/{key}/archive")
    assert r.status_code == 503, r.text
    assert metadata.archive_override_under_lock(key) != "archived"
    assert any(p.poll() is None for p in host.procs.values())  # the worker it could not reach


@pytest.mark.anyio
async def test_a_lost_session_record_does_not_hide_a_running_worker_from_retirement(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    # Hermes on #1315: with the session record gone, the worker directory still says running —
    # a removed catalog client must stay reachable until that worker is proved gone.
    from agent_sessions import native_runtime, native_state

    key = await _live_session(host, project)
    engine, native = key.split(":", 1)
    worker = native_state.read_session(native)["current_worker"]
    native_state.session_path(native).unlink()
    monkeypatch.setattr(kinds, "RESERVED_IDS", kinds.RESERVED_IDS - {engine})
    text = (FIRST_PARTY_DIR / "codex-api" / "plugin.toml").read_text()
    assert native_runtime.engine_may_have_workers(engine) is True
    assert registry._is_api_copy(engine, "plugin.toml", text.encode()) is True
    # An unattributable running worker (its config unreadable) keeps every API client too.
    (native_state.worker_dir(worker) / "config.json").write_text("{not json")
    assert native_runtime.engine_may_have_workers("acme-api") is True
    # Control: once that worker is proved gone, nothing keeps the client.
    host.kill(worker)
    native_state.update_lifecycle(worker, phase="gone")
    assert native_runtime.engine_may_have_workers(engine) is False
    assert registry._is_api_copy(engine, "plugin.toml", text.encode()) is False
