"""#2097: adopt-engine new-session reconciliation stays alive while the placeholder master runs,
and `/s/<engine>/new-<uuid>` reloads attach or redirect cleanly before and after adoption.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    engines,
    main,
    metadata,
    placeholder_launch,
    prefs,
    project_dirs,
    ptybridge,
    runtime_cleanup,
    scanner,
    sessionlock,
    sessions,
    webterm,
)
from agent_sessions.auth import AuthConfig
from agent_sessions.main import create_app
from agent_sessions.plugins import admission

_UUID_1 = "11111111-1111-4111-8111-111111111111"
_UUID_2 = "22222222-2222-4222-8222-222222222222"
_PLACEHOLDER = f"new-{_UUID_1}"

ADOPT_ENGINES: dict[str, str] = {
    "opencode": "ses_adopted00000000000000001",
    "opencode2": "ses_adopted20000000000000002",
    "codex": "019e2ba1-1590-7003-8e4a-51ab62cec96e",
    "antigravity": "a02a8694-40a5-4eb7-aaef-60359c1a8695",
    "kimi": "session_b02a8694-40a5-4eb7-aaef-60359c1a8695",
}


def _authed_client(auth_cfg: AuthConfig) -> tuple[TestClient, dict[str, str]]:
    app = create_app(auth_cfg)
    c = TestClient(app, base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    cookie = r.cookies.get("agent_sessions")
    c.cookies.set("agent_sessions", cookie)
    csrf = c.get("/api/config").json()["csrf"]
    headers = {
        "Origin": auth_cfg.origin,
        "Cookie": f"agent_sessions={cookie}",
        "X-CSRF-Token": csrf,
    }
    c.headers.update(headers)
    return c, headers


def _close_code(client: TestClient, url: str, headers: dict[str, str]) -> int | None:
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(url, headers=headers) as ws:
            while True:
                msg = ws.receive()
                if msg["type"] == "websocket.close":
                    return msg.get("code")
    except WebSocketDisconnect as e:
        return e.code


class _FakeWS:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("viewer socket closed")
        self.sent.append(text)


class _StubAdoptProv:
    def __init__(self, engine_id: str, results: list) -> None:
        self.engine_id = engine_id
        self._results = list(results)
        self.calls = 0

    @property
    def manifest(self):
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].manifest

    def store_root(self):
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].store_root()

    def store_path(self, name: str):
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].store_path(name)

    def reconcile_new_session(self, cwd: str, snapshot: set[str]):
        idx = min(self.calls, len(self._results) - 1)
        self.calls += 1
        val = self._results[idx]
        if callable(val):
            return val()
        return val


def test_reconcile_continues_past_max_polls_while_master_is_alive_and_notifies_all_viewers(
    tmp_home, monkeypatch
):
    """#2097: when the placeholder's dtach master is live (`ptybridge.ALIVE`), reconciliation
    continues past `_RECONCILE_MAX_POLLS`, persists the alias before notifying viewers, and
    broadcasts `{"t":"id"}` to every subscribed viewer even if one viewer's send fails."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    monkeypatch.setattr(main, "_RECONCILE_MAX_POLLS", 3)
    monkeypatch.setattr(ptybridge, "probe_master", lambda _sock: ptybridge.ALIVE)

    real_id = ADOPT_ENGINES["antigravity"]
    # First 5 polls (well past _RECONCILE_MAX_POLLS=3) return None, 6th returns the minted UUID.
    prov = _StubAdoptProv("antigravity", [None, None, None, None, None, real_id])
    phys_key = f"antigravity:{_PLACEHOLDER}"
    real_key = f"antigravity:{real_id}"
    placeholder_launch.record_launch(
        "antigravity", _PLACEHOLDER, cwd="/home/u/proj", snapshot={"old-id"}
    )
    assert placeholder_launch.read_context("antigravity", _PLACEHOLDER) is not None

    ws_live_1 = _FakeWS()
    ws_dead = _FakeWS(fail=True)
    ws_live_2 = _FakeWS()
    placeholder_launch.subscribe(phys_key, ws_live_1)
    placeholder_launch.subscribe(phys_key, ws_dead)
    placeholder_launch.subscribe(phys_key, ws_live_2)

    got = asyncio.run(
        main._reconcile_new_session(None, prov, _PLACEHOLDER, "/home/u/proj", {"old-id"})
    )
    assert got == real_id
    assert prov.calls == 6
    assert metadata.load_aliases() == {phys_key: real_key}
    # Context file is cleaned up once the durable alias is written.
    assert placeholder_launch.read_context("antigravity", _PLACEHOLDER) is None
    expected_frame = json.dumps({"t": "id", "sid": real_key})
    assert ws_live_1.sent == [expected_frame]
    assert ws_live_2.sent == [expected_frame]


def test_reconcile_stops_and_removes_context_when_live_master_exits(tmp_home, monkeypatch):
    """#2097: once a master has been seen alive and transitions to DEAD, reconciliation stops
    immediately and removes the placeholder launch context."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    states = iter([ptybridge.ALIVE, ptybridge.ALIVE, ptybridge.DEAD])
    monkeypatch.setattr(ptybridge, "probe_master", lambda _sock: next(states, ptybridge.DEAD))

    prov = _StubAdoptProv("antigravity", [None])
    placeholder_launch.record_launch(
        "antigravity", _PLACEHOLDER, cwd="/home/u/proj", snapshot=set()
    )
    got = asyncio.run(main._reconcile_new_session(None, prov, _PLACEHOLDER, "/home/u/proj", set()))
    assert got is None
    assert prov.calls == 2
    assert placeholder_launch.read_context("antigravity", _PLACEHOLDER) is None


def test_reconcile_ambiguity_retains_cwd_and_survives_restart_with_adoption_stopped(
    tmp_home, monkeypatch
):
    """#2097 (Hermes review): on ambiguity (≥2 new IDs), `reconcile_stopped` is persisted to the
    launch context while `cwd` is retained so a later un-aliased ATTACH still authorizes `cwd`
    under configured roots, and a simulated restart does not re-enable adoption."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    monkeypatch.setattr(ptybridge, "probe_master", lambda _sock: ptybridge.ALIVE)

    prov = _StubAdoptProv("codex", [[_UUID_1, _UUID_2]])
    placeholder_launch.record_launch("codex", _PLACEHOLDER, cwd="/home/u/proj", snapshot={"base"})
    got = asyncio.run(
        main._reconcile_new_session(None, prov, _PLACEHOLDER, "/home/u/proj", {"base"})
    )
    assert got is None
    assert metadata.load_aliases() == {}

    # Simulate process restart: clear in-memory `_STOPPED` and re-read from disk.
    placeholder_launch._STOPPED.clear()
    ctx = placeholder_launch.read_context("codex", _PLACEHOLDER)
    assert ctx is not None
    assert ctx.cwd == "/home/u/proj"
    assert ctx.snapshot == {"base"}
    assert ctx.reconcile_stopped is True
    assert placeholder_launch.is_stopped(f"codex:{_PLACEHOLDER}") is True


def test_mark_stopped_falls_back_to_inplace_write_when_temp_file_creation_fails(
    tmp_home, monkeypatch
):
    """#2097 (Hermes review): if atomic temp-file creation fails (e.g. ENOSPC) during
    `mark_stopped`, the existing verified context file is updated in place so a restart never
    reads `reconcile_stopped=False`."""
    import tempfile as _tempfile

    placeholder_launch.record_launch("opencode", _PLACEHOLDER, cwd="/home/u/proj", snapshot={"s1"})
    monkeypatch.setattr(
        _tempfile,
        "mkstemp",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("No space left on device")),
    )
    placeholder_launch.mark_stopped("opencode", _PLACEHOLDER)
    placeholder_launch._STOPPED.clear()
    ctx = placeholder_launch.read_context("opencode", _PLACEHOLDER)
    assert ctx is not None and ctx.reconcile_stopped is True and ctx.cwd == "/home/u/proj"


def test_reconcile_retries_transient_publish_alias_failure_and_stops_on_permanent_refusal(
    tmp_home, monkeypatch
):
    """#2097 (Hermes review): while the master is alive, a transient `publish_alias` failure
    (OSError or `admission.UNAVAILABLE`) preserves context, sends no `{"t":"id"}` on that tick,
    and retries on the next tick; a permanent `admission.Refused` marks `reconcile_stopped=True`
    while retaining `cwd`."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    monkeypatch.setattr(ptybridge, "probe_master", lambda _sock: ptybridge.ALIVE)

    real_id = ADOPT_ENGINES["opencode"]
    prov = _StubAdoptProv("opencode", [real_id])
    placeholder_launch.record_launch("opencode", _PLACEHOLDER, cwd="/home/u/proj", snapshot=set())
    ws = _FakeWS()

    real_publish = admission.publish_alias
    attempts = {"n": 0}

    def flaky_publish(p, phys_k, real_k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("transient disk hiccup")
        if attempts["n"] == 2:
            raise admission.Refused(admission.UNAVAILABLE)
        return real_publish(p, phys_k, real_k)

    monkeypatch.setattr(admission, "publish_alias", flaky_publish)
    got = asyncio.run(main._reconcile_new_session(ws, prov, _PLACEHOLDER, "/home/u/proj", set()))
    assert got == real_id
    assert attempts["n"] == 3
    assert ws.sent == [json.dumps({"t": "id", "sid": f"opencode:{real_id}"})]
    assert metadata.load_aliases() == {f"opencode:{_PLACEHOLDER}": f"opencode:{real_id}"}

    # Permanent refusal: stops immediately and marks `reconcile_stopped=True`.
    ph2 = f"new-{_UUID_2}"
    placeholder_launch.record_launch("opencode", ph2, cwd="/home/u/proj2", snapshot=set())
    ws2 = _FakeWS()
    monkeypatch.setattr(
        admission,
        "publish_alias",
        lambda *_a: (_ for _ in ()).throw(
            admission.Refused("native history belongs to an API client")
        ),
    )
    got2 = asyncio.run(main._reconcile_new_session(ws2, prov, ph2, "/home/u/proj2", set()))
    assert got2 is None
    assert ws2.sent == []
    placeholder_launch._STOPPED.clear()
    ctx2 = placeholder_launch.read_context("opencode", ph2)
    assert ctx2 is not None and ctx2.reconcile_stopped is True and ctx2.cwd == "/home/u/proj2"


@pytest.mark.parametrize(("engine_id", "real_id"), list(ADOPT_ENGINES.items()))
def test_get_session_resolves_adopted_placeholder_for_every_adopt_engine(
    engine_id, real_id, tmp_home, auth_cfg, monkeypatch
):
    """#2097: across all 5 `adopt` engines (`opencode`, `opencode2`, `codex`, `antigravity`,
    `kimi`), `GET /api/sessions/<engine>:new-<uuid>` returns 404 before adoption, and after
    adoption returns the resolved row with `resolved_from` while enforcing root/exclusion scope."""
    cwd = str(tmp_home / "claude" / "proj")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [cwd])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])

    fake_session = scanner.Session(
        engine=engine_id,
        uuid=real_id,
        cwd=cwd,
        last_mtime=1000.0,
        first_user_message="hello",
        archived=False,
        created_at=990.0,
    )
    monkeypatch.setattr(engines, "scan_all_cached", lambda: [fake_session])
    monkeypatch.setattr(engines, "scan_all", lambda: [fake_session])

    c, _headers = _authed_client(auth_cfg)
    ph_key = f"{engine_id}:{_PLACEHOLDER}"
    real_key = f"{engine_id}:{real_id}"

    # 1. Before adoption: 404.
    assert c.get(f"/api/sessions/{ph_key}").status_code == 404

    # 2. After adoption: 200 with `id == real_key` and `resolved_from == ph_key`.
    metadata.set_alias(ph_key, real_key)
    r = c.get(f"/api/sessions/{ph_key}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == real_key
    assert body["resolved_from"] == ph_key
    assert body["cwd"] == cwd

    # 3. Scope change excluding `cwd` refuses `GET /api/sessions/<ph_key>` with 404.
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [cwd])
    assert c.get(f"/api/sessions/{ph_key}").status_code == 404


def test_ws_placeholder_reload_before_and_after_adoption_and_dead_master_guard(
    tmp_home, auth_cfg, monkeypatch
):
    """#2097: reloading `/ws/term/<engine>:new-<uuid>` without `new=1`:
    - refuses 4404 when no master is live and no alias exists (never launches a fresh master);
    - attaches (`sessions.ATTACH`) when the master is live and launch context is valid & in-scope,
      recovering reconciliation after a simulated restart;
    - refuses 4404 when roots/exclusions change to exclude the launch `cwd` or context is missing;
    - after adoption, attaches under the resolved logical identity and sends `{"t":"id"}`."""
    cwd = str(tmp_home / "claude" / "proj")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [cwd])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])

    ph_key = f"antigravity:{_PLACEHOLDER}"
    real_id = ADOPT_ENGINES["antigravity"]
    real_key = f"antigravity:{real_id}"
    c, headers = _authed_client(auth_cfg)

    # (1) No live master, no alias, no `new=1` → 4404 (never LAUNCH).
    assert _close_code(c, f"/ws/term/{ph_key}", headers) == 4404
    assert not sessionlock.is_locked(ph_key)

    # (2) Live master (`ATTACH`), but missing launch context file → 4404 fail-closed.
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.ATTACH, None))
    assert _close_code(c, f"/ws/term/{ph_key}", headers) == 4404

    # (3) Live master (`ATTACH`) with valid launch context in scope → attaches and re-arms
    # reconciliation from the persisted snapshot (simulated restart: `_TASKS` was empty).
    placeholder_launch.record_launch(
        "antigravity", _PLACEHOLDER, cwd=cwd, snapshot={"pre-existing"}
    )
    reconcile_calls: list[tuple] = []

    async def fake_webterm_run(ws, argv, **kwargs):
        await ws.send_text(json.dumps({"t": "ready"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await ws.close(code=1000)

    def spy_reconcile(ws, prov, placeholder, r_cwd, r_snap):
        reconcile_calls.append((placeholder, r_cwd, set(r_snap)))

        async def _converge():
            for viewer in placeholder_launch.subscribers(ph_key, extra=ws):
                await viewer.send_text(json.dumps({"t": "id", "sid": real_key}))
            return real_id

        return _converge()

    monkeypatch.setattr(main, "_reconcile_new_session", spy_reconcile)
    monkeypatch.setattr(webterm, "run", fake_webterm_run)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: True)
    with c.websocket_connect(f"/ws/term/{ph_key}", headers=headers) as ws:
        frames = []
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            if "text" in msg:
                frames.append(json.loads(msg["text"]))
        assert any(f.get("t") == "ready" for f in frames)
        assert {"t": "id", "sid": real_key} in frames
    assert reconcile_calls == [(_PLACEHOLDER, cwd, {"pre-existing"})]

    # (4) Scope change excluding `cwd` refuses un-aliased `ATTACH` with 4404.
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [cwd])
    assert _close_code(c, f"/ws/term/{ph_key}", headers) == 4404
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])

    # (5) After adoption (`metadata.set_alias`), connecting to `/ws/term/<ph_key>` without `new=1`
    # resolves forward to `real_key`, checks scope, and sends `{"t":"id","sid":real_key}`.
    fake_session = scanner.Session(
        engine="antigravity",
        uuid=real_id,
        cwd=cwd,
        last_mtime=1000.0,
        first_user_message="hi",
        archived=False,
        created_at=990.0,
    )
    monkeypatch.setattr(engines, "resolve_session", lambda e, n, arrival=None: fake_session)
    metadata.set_alias(ph_key, real_key)
    with c.websocket_connect(f"/ws/term/{ph_key}", headers=headers) as ws:
        frames = []
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            if "text" in msg:
                frames.append(json.loads(msg["text"]))
        assert {"t": "id", "sid": real_key} in frames

    # (6) Runtime cleanup removes any remaining placeholder launch context and task.
    monkeypatch.setattr(
        runtime_cleanup.reaper, "terminate_master", lambda *a, **kw: asyncio.sleep(0, "gone")
    )
    asyncio.run(runtime_cleanup.cleanup_runtime("antigravity", real_id))
    assert placeholder_launch.read_context("antigravity", _PLACEHOLDER) is None


def test_ws_placeholder_attach_survives_adoption_between_alias_check_and_context_read(
    tmp_home, auth_cfg, monkeypatch
):
    """#2097 (Hermes PR review, finding 1): if the poller publishes the alias and removes the
    placeholder context file between the first `resolve_logical_key` check and `read_context`
    during an `ATTACH`, `ws_term` re-reads the forward alias, authorizes the resolved session's
    `cwd` against roots/exclusions, and attaches + sends `{"t":"id"}` instead of 4404."""
    cwd = str(tmp_home / "claude" / "proj")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [cwd])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.ATTACH, None))

    ph_key = f"antigravity:{_PLACEHOLDER}"
    real_id = ADOPT_ENGINES["antigravity"]
    real_key = f"antigravity:{real_id}"
    fake_session = scanner.Session(
        engine="antigravity",
        uuid=real_id,
        cwd=cwd,
        last_mtime=1000.0,
        first_user_message="hi",
        archived=False,
        created_at=990.0,
    )
    monkeypatch.setattr(engines, "resolve_session", lambda e, n, arrival=None: fake_session)

    async def fake_webterm_run(ws, argv, **kwargs):
        await ws.send_text(json.dumps({"t": "ready"}))
        await ws.close(code=1000)

    monkeypatch.setattr(webterm, "run", fake_webterm_run)

    # At the moment `read_context` is called, simulate the poller publishing the alias and
    # deleting the context file (returning `None`).
    def publish_and_remove_at_context_read(engine: str, placeholder: str):
        metadata.set_alias(ph_key, real_key)
        placeholder_launch.remove_context(engine, placeholder)
        return None

    monkeypatch.setattr(placeholder_launch, "read_context", publish_and_remove_at_context_read)

    c, headers = _authed_client(auth_cfg)
    with c.websocket_connect(f"/ws/term/{ph_key}", headers=headers) as ws:
        frames = []
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            if "text" in msg:
                frames.append(json.loads(msg["text"]))
        assert any(f.get("t") == "ready" for f in frames)
        assert {"t": "id", "sid": real_key} in frames

    # And if the concurrently adopted session's `cwd` is out of scope, the re-read still fails
    # closed with 4404.
    ph_key_2 = f"antigravity:new-{_UUID_2}"

    def publish_and_remove_at_context_read_2(engine: str, placeholder: str):
        metadata.set_alias(f"{engine}:{placeholder}", real_key)
        placeholder_launch.remove_context(engine, placeholder)
        return None

    monkeypatch.setattr(placeholder_launch, "read_context", publish_and_remove_at_context_read_2)
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [cwd])
    assert _close_code(c, f"/ws/term/{ph_key_2}", headers) == 4404


def test_ws_placeholder_disconnect_under_unknown_liveness_or_second_subscriber_keeps_task(
    tmp_home, auth_cfg, monkeypatch
):
    """#2097 (Hermes PR review, finding 2): when a viewer disconnects while `ptybridge.probe_master`
    returns `UNKNOWN` (which real `ptybridge.session_exists` maps to `False`) or while a second
    viewer remains subscribed, the shared server-owned reconciliation task must NOT be cancelled."""
    cwd = str(tmp_home / "claude" / "proj")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [cwd])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.ATTACH, None))
    # Use the REAL `ptybridge.session_exists`, with `probe_master` returning `UNKNOWN` (timeout
    # under load: `session_exists` returns `False`, but master death is NOT confirmed).
    monkeypatch.setattr(ptybridge, "probe_master", lambda _sock: ptybridge.UNKNOWN)

    ph_key = f"antigravity:{_PLACEHOLDER}"
    real_id = ADOPT_ENGINES["antigravity"]
    real_key = f"antigravity:{real_id}"
    placeholder_launch.record_launch(
        "antigravity", _PLACEHOLDER, cwd=cwd, snapshot={"pre-existing"}
    )

    gate = {"release": False}
    cancelled = {"flag": False}

    def long_reconcile(ws, prov, placeholder, r_cwd, r_snap):
        async def _wait_then_converge():
            try:
                while not gate["release"]:
                    await asyncio.sleep(0.01)
                frame = json.dumps({"t": "id", "sid": real_key})
                for viewer in placeholder_launch.subscribers(ph_key, extra=ws):
                    await viewer.send_text(frame)
                return real_id
            except asyncio.CancelledError:
                cancelled["flag"] = True
                raise

        return _wait_then_converge()

    async def fake_webterm_run(ws, argv, **kwargs):
        await ws.send_text(json.dumps({"t": "ready"}))
        await ws.close(code=1000)

    monkeypatch.setattr(main, "_reconcile_new_session", long_reconcile)
    monkeypatch.setattr(webterm, "run", fake_webterm_run)

    c, headers = _authed_client(auth_cfg)
    with c:
        # Subscribe a second live viewer alongside the route viewer.
        second_viewer = _FakeWS()
        placeholder_launch.subscribe(ph_key, second_viewer)

        # First connect + disconnect while `second_viewer` is subscribed and `probe_master`
        # returns UNKNOWN.
        with c.websocket_connect(f"/ws/term/{ph_key}", headers=headers) as ws:
            while True:
                msg = ws.receive()
                if msg["type"] == "websocket.close":
                    break
        task = placeholder_launch._TASKS.get(ph_key)
        assert task is not None and not task.done() and not cancelled["flag"]

        # Unsubscribe `second_viewer`, then connect + disconnect a single viewer while
        # `probe_master` is still `UNKNOWN`: even with 0 subscribers left, `UNKNOWN` keeps it.
        placeholder_launch.unsubscribe(ph_key, second_viewer)
        with c.websocket_connect(f"/ws/term/{ph_key}", headers=headers) as ws:
            while True:
                msg = ws.receive()
                if msg["type"] == "websocket.close":
                    break
        task_after = placeholder_launch._TASKS.get(ph_key)
        assert task_after is task and not task_after.done() and not cancelled["flag"]
