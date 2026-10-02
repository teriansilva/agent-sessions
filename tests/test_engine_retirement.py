"""#853 P3 PR B — an engine whose manifest disappears RETIRES; it never vanishes, and it never
silently becomes another engine.

The matrix #1126 records (Hermes, 2026-09-24):

* a live master of a removed engine stays **attachable across restarts**, authorized by the same
  roots + exclusions check as any ATTACH (never by the socket's existence), and an excluded,
  out-of-root or unidentifiable session stays refused;
* a dead master of a retiring engine is **never relaunched**, and no new session starts;
* new work — handoff, unattended dispatch, a queued mission plan — is refused **"agent removed"**,
  before AND after the cleanup that ends retirement, and never falls back to another engine;
* the recorded roster is **input**: a copy another account could write, a malformed one, or one
  that fails its digest is refused and reported, never trusted;
* a stored **budget** survives remove → restart → cleanup → re-add unchanged.

"Restart" is `registry.reload(...)`, the app-start boundary; the fixture engine enters through the
loader's test seam, exactly as in `test_roster_conformance.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import engines, headless_dispatch, missions, prefs, ptybridge
from agent_sessions.engines import registry
from agent_sessions.main import create_app
from agent_sessions.plugins import FIRST_PARTY_DIR, kinds, roster_state
from agent_sessions.scanner import Session
from test_roster_conformance import ZETA, ZETA_ID, ZETA_MANIFEST, ZetaKind

KEY = f"{ZETA}:{ZETA_ID}"
CWD = "/work/zeta-project"


class LookupKind(ZetaKind):
    """The fixture store kind, able to answer a single-key lookup (so ATTACH can be authorized)."""

    known: dict[str, str] = {}

    def lookup(self, native):
        cwd = self.known.get(native)
        if cwd is None:
            return None
        return Session(ZETA, native, cwd, 0.0, "", False)


class Roster:
    """The in-tree manifests plus (optionally) `zeta`, reloaded through the test seam."""

    def __init__(self, fp):
        self.fp = fp

    def add(self):
        (self.fp / ZETA).mkdir(exist_ok=True)
        (self.fp / ZETA / "plugin.toml").write_text(ZETA_MANIFEST)
        registry.reload(first_party_dir=self.fp)

    def remove(self):
        shutil.rmtree(self.fp / ZETA, ignore_errors=True)
        registry.reload(first_party_dir=self.fp)

    def restart(self):
        registry.reload(first_party_dir=self.fp)


@pytest.fixture
def roster(tmp_path, monkeypatch):
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    monkeypatch.setattr(kinds, "STORE_LAYOUTS", kinds.STORE_LAYOUTS | {"zeta-store"})
    monkeypatch.setitem(registry.STORE_KINDS, "zeta-store", LookupKind)
    monkeypatch.setattr(LookupKind, "known", {ZETA_ID: CWD})
    r = Roster(fp)
    r.add()
    yield r
    registry.reload()


class Master:
    """A stand-in dtach master: a LISTENING unix socket at the session's real socket path is what
    `probe_master` calls alive; closing it leaves the file behind, which reads as dead."""

    def __init__(self, engine, native):
        self.path = ptybridge.socket_path(engine, native)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(8)

    def die(self):
        self.sock.close()  # the file stays: an orphan socket, exactly what a crashed master leaves


@pytest.fixture
def master(roster):
    m = Master(ZETA, ZETA_ID)
    yield m
    m.sock.close()
    with pytest.MonkeyPatch.context():
        if m.path.exists():
            os.unlink(m.path)


# --- the lifecycle --------------------------------------------------------------------------------


def test_a_removed_engine_with_a_live_master_RETIRES_and_stays_retiring_across_restarts(
    roster, master
):
    roster.remove()
    for _restart in range(3):  # remove, then two more app starts
        assert engines.is_retiring(ZETA)
        assert engines.get(ZETA) is None, "a retiring engine is in no listing"
        assert ZETA not in engines.engine_ids()
        prov, native = engines.parse_key(KEY)  # …but its sessions still parse (ATTACH)
        assert prov.engine_id == ZETA and native == ZETA_ID
        assert engines.removed_reason(ZETA) == "agent removed"
        roster.restart()


def test_a_retiring_engine_has_NO_entrypoint(roster, master, engine_bin):
    engine_bin(ZETA)  # even with its binary present and pinned
    roster.remove()
    prov = engines.get_any(ZETA)
    assert engines.launchable_bin(prov) is None
    with pytest.raises(engines.EngineError, match="agent removed"):
        prov.launch_argv(ZETA_ID, cwd="/w", bypass=False)


def test_when_the_last_master_dies_retirement_ends_but_the_REASON_survives(roster, master):
    roster.remove()
    assert engines.is_retiring(ZETA)
    master.die()
    roster.restart()
    assert not engines.is_retiring(ZETA)
    with pytest.raises(engines.EngineError):
        engines.parse_key(KEY)  # nothing is attachable any more
    assert engines.removed_reason(ZETA) == "agent removed", "not 'unknown engine' after cleanup"
    roster.restart()
    assert engines.removed_reason(ZETA) == "agent removed", "the tombstone persists"
    assert engines.removed_reason("never-existed") is None


def test_an_UNKNOWN_probe_verdict_keeps_the_engine_retiring(roster, master, monkeypatch):
    """A starved master times its probe out. Reading UNKNOWN as dead would end retirement while the
    session is still running — stranding the terminal this exists to keep reachable (#355)."""
    master.die()
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.UNKNOWN)
    roster.remove()
    assert engines.is_retiring(ZETA)


def test_RE_ADDING_the_manifest_brings_back_the_engine_its_sessions_and_its_budget(
    roster, master, tmp_path
):
    p = tmp_path / "prefs.json"
    prefs.set_agent_budgets({"engines": {ZETA: {"limit_tokens": 4242}}}, p)
    roster.remove()
    master.die()
    roster.restart()  # cleanup: tombstoned
    # Inert, but kept — and still editable, since it is stored.
    assert prefs.get_agent_budgets(p)["engines"][ZETA] == {"limit_tokens": 4242}
    assert prefs.validate_agent_budgets_patch({"engines": {ZETA: {"limit_tokens": 1}}}, p) is None
    roster.add()
    assert engines.get(ZETA) is not None and not engines.is_retiring(ZETA)
    assert engines.removed_reason(ZETA) is None
    assert engines.parse_key(KEY) == (engines.get(ZETA), ZETA_ID)
    assert prefs.get_agent_budgets(p)["engines"][ZETA] == {"limit_tokens": 4242}


# --- new work is refused, by reason, and never switched -------------------------------------------


@pytest.mark.parametrize("cleaned_up", [False, True])
def test_unattended_dispatch_to_a_removed_engine_says_AGENT_REMOVED(roster, master, cleaned_up):
    roster.remove()
    if cleaned_up:
        master.die()
        roster.restart()
    with pytest.raises(headless_dispatch.DispatchError, match="^agent removed$"):
        asyncio.run(
            headless_dispatch.dispatch(engine=ZETA, cwd="/w", brief="go", registry=object())
        )


@pytest.fixture
def mission_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


@pytest.mark.parametrize("cleaned_up", [False, True])
def test_a_QUEUED_mission_plan_naming_a_removed_engine_stops_with_the_reason(
    roster, master, mission_store, cleaned_up
):
    """The plan was approved for `zeta`. When `zeta` is removed before it dispatches, the mission
    stops at its next dispatch saying why — it never runs the brief on another engine."""
    from agent_sessions import mission_dispatch

    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd="/repo", engine=ZETA, brief="go")
    roster.remove()
    if cleaned_up:
        master.die()
        roster.restart()
    launched: list = []
    real = headless_dispatch.dispatch

    async def spy(**kw):
        launched.append(kw["engine"])
        return await real(**kw)

    claimed = missions.claim_plan(m["id"], plan["plan_id"])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mission_dispatch.headless_dispatch, "dispatch", spy)
        out = asyncio.run(mission_dispatch.run(m["id"], claimed, registry=object()))
    assert out["outcome"] == "refused" and out["reason"] == "agent removed"
    assert launched == [ZETA], "the dispatch was attempted for the planned engine and nothing else"
    back = missions.get_plan(m["id"])
    assert back is not None and back["engine"] == ZETA, "the plan still names its engine"


# --- the terminal: ATTACH authorized as ever; LAUNCH never ----------------------------------------
#
# These drive the real app, whose startup discovery would attach a session reader to a real socket.
# So a live master is PRETENDED at the one question retirement asks — which engines still have one
# — while the lifecycle tests above keep the real socket and the real probe.


@pytest.fixture
def pretend_live(roster, monkeypatch):
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {ZETA})


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login_headers(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"cookie": f"agent_sessions={c.cookies.get('agent_sessions')}", "origin": cfg.origin}


def _outcome(c, key, headers, monkeypatch, action):
    """Drive `/ws/term/<key>` with the ATTACH/LAUNCH decision forced, the bridge stubbed.
    Returns ("bridged", argv) or ("closed", code)."""
    from agent_sessions.routes import terminal as terminal_route

    async def _action(_engine, _native):
        return action, None

    bridged: list = []

    async def _bridge(ws, argv, **_kw):
        bridged.append(list(argv))
        await ws.close(code=4999)

    monkeypatch.setattr(terminal_route, "_open_action_offloop", _action)
    monkeypatch.setattr(terminal_route.webterm, "run", _bridge)
    code = None
    try:
        with c.websocket_connect(f"/ws/term/{key}", headers=headers) as ws:
            for _ in range(20):
                msg = ws.receive()
                if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                    code = msg.get("code")
                    break
    except WebSocketDisconnect as e:
        code = e.code
    return ("bridged", bridged[0]) if bridged else ("closed", code)


def _boundary(monkeypatch, *, roots=(), exclusions=()):
    from agent_sessions import project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: list(roots))
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: list(exclusions))


def test_an_IN_SCOPE_live_session_of_a_retiring_engine_is_attachable(
    roster, pretend_live, auth_cfg, monkeypatch
):
    from agent_sessions.routes import terminal as terminal_route

    roster.remove()
    _boundary(monkeypatch, roots=["/work"])
    c = _client(auth_cfg)
    got = _outcome(c, KEY, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.ATTACH)
    assert got[0] == "bridged", got


@pytest.mark.parametrize(
    "boundary",
    [
        {"exclusions": [CWD]},  # excluded
        {"roots": ["/elsewhere"]},  # outside every root
    ],
)
def test_an_OUT_OF_SCOPE_live_session_of_a_retiring_engine_is_refused(
    roster, pretend_live, auth_cfg, monkeypatch, boundary
):
    from agent_sessions.routes import terminal as terminal_route

    roster.remove()
    _boundary(monkeypatch, **boundary)
    c = _client(auth_cfg)
    got = _outcome(c, KEY, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.ATTACH)
    assert got == ("closed", 4404)


def test_an_UNIDENTIFIABLE_live_session_is_refused_once_a_boundary_exists(
    roster, pretend_live, auth_cfg, monkeypatch
):
    """A live socket is not authorization evidence: with nothing that can identify the session
    (no lookup answer), the existing fail-closed rule applies."""
    from agent_sessions.routes import terminal as terminal_route

    monkeypatch.setattr(LookupKind, "known", {})
    roster.remove()
    _boundary(monkeypatch, roots=["/work"])
    c = _client(auth_cfg)
    got = _outcome(c, KEY, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.ATTACH)
    assert got == ("closed", 4404)


def test_a_retiring_engine_is_NEVER_relaunched(roster, pretend_live, auth_cfg, monkeypatch):
    """The master died between the probe and the attach: the route sees LAUNCH. A retiring engine
    has nothing to launch with, and relaunching would start an agent the operator removed."""
    from agent_sessions.routes import terminal as terminal_route

    roster.remove()
    _boundary(monkeypatch)
    c = _client(auth_cfg)
    got = _outcome(c, KEY, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.LAUNCH)
    assert got == ("closed", 4404)
    placeholder = f"{ZETA}:new-0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"
    got = _outcome(
        c,
        placeholder + "?new=1&cwd=/work",
        _login_headers(c, auth_cfg),
        monkeypatch,
        terminal_route.sessions.LAUNCH,
    )
    assert got[0] == "closed" and got[1] == 4404, "no new session for a removed engine"


# --- the API --------------------------------------------------------------------------------------


def test_api_engines_marks_a_retiring_engine_and_refuses_it_as_a_handoff_target(
    roster, pretend_live, auth_cfg
):
    roster.remove()
    c = _client(auth_cfg)
    _login_headers(c, auth_cfg)
    listing = c.get("/api/engines").json()
    rows = {e["id"]: e for e in listing["engines"]}
    assert listing["problems"] == []  # #853 P4's diagnostics ride beside the retiring row
    z = rows[ZETA]
    # Listed, so its read-only detail resolves too, and says it is retiring (#1128 × #1126).
    d = c.get(f"/api/engines/{ZETA}")
    assert d.status_code == 200 and d.json()["status"] == "retiring", d.text
    assert z["status"] == "retiring" and z["status_reason"].startswith("agent removed")
    assert z["present"] is False and z["supports_new"] is False
    assert z["supports_seed_start"] is False and z["seed_reason"] == "agent removed"
    assert all(e["status"] == "active" for k, e in rows.items() if k != ZETA)
    r = c.post(
        "/api/handoff/prepare",
        json={"source_id": f"claude:{'0' * 8}-0000-4000-8000-{'0' * 12}", "target_engine": ZETA},
        headers={"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin},
    )
    # The target is refused BY REASON — it existed — before the source is even looked up.
    assert r.status_code == 422 and "agent removed" in r.json()["detail"], r.text
    pretend_live_off = pytest.MonkeyPatch()
    pretend_live_off.setattr(registry, "_engines_with_masters", lambda *_a: set())
    try:
        roster.restart()
        rows = {e["id"]: e for e in c.get("/api/engines").json()["engines"]}
    finally:
        pretend_live_off.undo()
    assert ZETA not in rows, "a tombstoned engine is not listed; only its refusals remember it"
    assert c.get(f"/api/engines/{ZETA}").status_code == 404


# --- the recorded roster is INPUT -----------------------------------------------------------------


def test_a_recorded_roster_another_account_could_write_is_refused_and_reported(roster, master):
    roster.remove()
    assert engines.is_retiring(ZETA)
    p = roster_state.state_path()
    os.chmod(p, 0o666)
    roster.restart()
    assert not engines.is_retiring(ZETA), "a foreign-writable record must not be trusted"
    assert any("only the operator can write" in w for w in registry.RETIREMENT_PROBLEMS)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.update(extra=1),
        lambda d: d["manifests"][ZETA].update(sha256="0" * 64),
        lambda d: d["manifests"][ZETA].update(text=ZETA_MANIFEST.replace('"zeta"', '"other"', 1)),
        lambda d: d["manifests"][ZETA].update(name="plugin.py"),
    ],
)
def test_a_malformed_or_tampered_record_is_refused_and_reported(roster, master, corrupt):
    roster.remove()
    p = roster_state.state_path()
    doc = json.loads(p.read_text())
    corrupt(doc)
    if (
        "sha256" in doc.get("manifests", {}).get(ZETA, {})
        and doc["manifests"][ZETA]["sha256"] != "0" * 64
    ):
        import hashlib

        rec = doc["manifests"][ZETA]
        rec["sha256"] = hashlib.sha256(rec["text"].encode()).hexdigest()
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    roster.restart()
    assert not engines.is_retiring(ZETA)
    assert registry.RETIREMENT_PROBLEMS, "the refusal is reported"


def test_the_record_is_private_to_the_operator(roster):
    st = os.stat(roster_state.state_path())
    assert st.st_mode & 0o777 == 0o600 and st.st_uid == os.geteuid()


# --- REAL built-in stores (Hermes on PR #1132) ----------------------------------------------------
#
# The synthetic `zeta` lookup above could not catch these: codex's real lookup resolves its store
# through the layout helpers, and gemini has no single-key lookup at all. Both are retired here by
# removing their real in-tree manifests, with their real stores on disk.

from agent_sessions import runtime_cleanup  # noqa: E402
from test_codex import _write_rollout  # noqa: E402
from test_gemini import _HASH, _write_chat  # noqa: E402

CODEX_ID = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
GEMINI_ID = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
CLAUDE_ID = "0123abcd-0123-4567-89ab-0123456789ab"


@pytest.fixture
def builtin(tmp_path, monkeypatch):
    """The seven in-tree manifests, with codex and gemini stores holding one session each."""
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    codex_root = tmp_path / "codex-sessions"
    gem = tmp_path / "gemini-tmp"
    gem.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(codex_root))
    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_TMP_DIR", str(gem))
    (gem / "project-map.json").write_text(json.dumps({_HASH: CWD}))
    _write_rollout(codex_root, uuid=CODEX_ID, cwd=CWD, first_user="hello")
    _write_chat(gem, sid=GEMINI_ID, project_hash=_HASH, first_user="hello")
    registry.reload(first_party_dir=fp)  # records every manifest
    for eid, native in (("codex", CODEX_ID), ("gemini", GEMINI_ID)):
        assert registry.resolve_session(eid, native).cwd == CWD, "baseline: active lookup works"
    yield fp
    registry.reload()


def _retire(fp, *eids):
    for e in eids:
        shutil.rmtree(fp / e)
    registry.reload(first_party_dir=fp)


@pytest.mark.parametrize("eid,native", [("codex", CODEX_ID), ("gemini", GEMINI_ID)])
def test_a_RETIRING_builtin_still_identifies_its_session_from_its_own_store(
    builtin, monkeypatch, eid, native
):
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"codex", "gemini"})
    _retire(builtin, "codex", "gemini")
    assert engines.is_retiring(eid)
    row = registry.resolve_session(eid, native)
    assert row is not None and row.cwd == CWD, "an in-scope live session must stay attachable"
    assert eid not in {s.engine for s in engines.scan_all()}, "…but it lists nowhere"


@pytest.mark.parametrize("key", [f"codex:{CODEX_ID}", f"gemini:{GEMINI_ID}"])
def test_the_ws_ATTACH_of_a_retiring_builtin_reaches_the_bridge_in_scope(
    builtin, auth_cfg, monkeypatch, key
):
    from agent_sessions.routes import terminal as terminal_route

    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"codex", "gemini"})
    _retire(builtin, "codex", "gemini")
    _boundary(monkeypatch, roots=["/work"])
    c = _client(auth_cfg)
    got = _outcome(c, key, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.ATTACH)
    assert got[0] == "bridged", got
    _boundary(monkeypatch, roots=["/elsewhere"])
    got = _outcome(c, key, _login_headers(c, auth_cfg), monkeypatch, terminal_route.sessions.ATTACH)
    assert got == ("closed", 4404), "…and still refused out of scope"


def test_teardown_of_a_RETIRING_late_id_engine_still_reads_the_durable_mapping(
    builtin, monkeypatch
):
    """In the adoption-commit → alias-publication window the placeholder master is only findable
    through the mission store's mapping; retirement must not skip it."""
    from agent_sessions import missions

    placeholder = "codex:new-0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"
    monkeypatch.setattr(missions, "physical_key_of", lambda logical: placeholder)
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"codex"})
    _retire(builtin, "codex")
    assert engines.is_retiring("codex")
    got = asyncio.run(runtime_cleanup.resolve_runtime_key("codex", CODEX_ID))
    assert got == placeholder


def test_an_UNLISTABLE_runtime_dir_keeps_every_copy_and_retires_rather_than_tombstones(
    builtin, monkeypatch
):
    class Unlistable:
        def iterdir(self):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ptybridge, "runtime_dir", lambda: Unlistable())
    _retire(builtin, "codex")
    assert engines.is_retiring("codex"), "unknown liveness keeps the engine reachable"
    doc = json.loads(roster_state.state_path().read_text())
    assert "codex" in doc["manifests"] and "codex" not in doc["removed"]
    assert any("could not be listed" in w for w in registry.RETIREMENT_PROBLEMS)


def test_a_legacy_BARE_claude_id_keeps_parsing_while_claude_retires(builtin, monkeypatch):
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"claude"})
    _retire(builtin, "claude")
    assert engines.is_retiring("claude")
    prov, native = engines.parse_key(CLAUDE_ID)
    assert (prov.engine_id, native) == ("claude", CLAUDE_ID)


# --- a transient READ failure of the recorded roster erases nothing (Hermes on PR #1132) ----------


def test_a_TRANSIENT_roster_read_failure_overwrites_nothing_and_recovers(builtin, monkeypatch):
    import errno

    from agent_sessions.plugins import provenance

    # codex retires (a live master); gemini is removed with none (tombstoned).
    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"codex"})
    _retire(builtin, "codex", "gemini")
    assert engines.is_retiring("codex") and engines.removed_reason("gemini") == "agent removed"
    p = roster_state.state_path()
    before = p.read_bytes()
    assert b'"codex"' in before and b'"gemini"' in before

    real = provenance.open_verified

    def eio_on_roster(path, **kw):
        if path.endswith("roster.json"):
            raise OSError(errno.EIO, "Input/output error")
        return real(path, **kw)

    monkeypatch.setattr(provenance, "open_verified", eio_on_roster)
    registry.reload(first_party_dir=builtin)  # an app start while the read fails
    assert p.read_bytes() == before, "the unreadable predecessor must be left exactly as it was"
    assert not engines.is_retiring("codex"), "nothing from an unread record is trusted"
    assert any("could not be opened" in w for w in registry.RETIREMENT_PROBLEMS)

    monkeypatch.setattr(provenance, "open_verified", real)
    registry.reload(first_party_dir=builtin)  # the next start, fault gone
    assert engines.is_retiring("codex"), "the recovery copy survived the failure"
    assert engines.removed_reason("gemini") == "agent removed", "…and so did the tombstone"


def test_one_BAD_record_is_carried_verbatim_not_erased_and_does_not_freeze_updates(builtin):
    _retire(builtin, "gemini")  # tombstoned, no master
    p = roster_state.state_path()
    doc = json.loads(p.read_text())
    doc["manifests"]["legacy-x"] = {"name": "plugin.toml", "text": "x", "sha256": "0" * 64}
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    registry.reload(first_party_dir=builtin)
    after = json.loads(p.read_text())
    assert after["manifests"]["legacy-x"] == doc["manifests"]["legacy-x"], "carried verbatim"
    assert "claude" in after["manifests"], "…while the rest of the roster keeps updating"
    assert any("fails its digest" in w for w in registry.RETIREMENT_PROBLEMS)


def test_a_TRANSIENT_stat_failure_is_not_absence_and_erases_nothing(builtin, monkeypatch):
    """`os.path.lexists` would have answered False for EIO and let "nothing recorded" be saved over
    the real record. The check is an explicit lstat: only ENOENT is absence."""
    import errno

    monkeypatch.setattr(registry, "_engines_with_masters", lambda *_a: {"codex"})
    _retire(builtin, "codex", "gemini")
    p = roster_state.state_path()
    before = p.read_bytes()
    real_lstat = os.lstat

    def eio(path, *a, **k):
        if str(path).endswith("roster.json"):
            raise OSError(errno.EIO, "Input/output error")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(roster_state.os, "lstat", eio)
    registry.reload(first_party_dir=builtin)
    monkeypatch.setattr(roster_state.os, "lstat", real_lstat)
    assert p.read_bytes() == before, "an unexaminable record must be left exactly as it was"
    assert any("could not be examined" in w for w in registry.RETIREMENT_PROBLEMS)
    registry.reload(first_party_dir=builtin)
    assert engines.is_retiring("codex") and engines.removed_reason("gemini") == "agent removed"


def test_an_engine_id_WITH_A_HYPHEN_is_seen_live_by_its_own_socket(roster, tmp_path):
    """`my-agent-<native>.sock` is never attributed by splitting at a hyphen or by longest prefix:
    every recorded id it could belong to is live (independent review + Hermes on #1132)."""
    sock_path = ptybridge.socket_path("my-agent", "abc123")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    try:
        # The name is `my-agent:abc123` OR `my:agent-abc123` — it cannot prove which, so BOTH
        # plausible owners stay live (Hermes on #1132); an unrelated id does not.
        assert registry._engines_with_masters(["my", "my-agent", "other"]) == {"my", "my-agent"}
        assert registry._engines_with_masters(["my"]) == {"my"}
        assert registry._engines_with_masters(["my-agent"]) == {"my-agent"}
    finally:
        s.close()
        os.unlink(sock_path)


def test_a_live_engine_whose_copy_cannot_be_parsed_still_says_AGENT_REMOVED(roster, master):
    p = roster_state.state_path()
    doc = json.loads(p.read_text())
    import hashlib

    bad = 'contract = 1\n[identity]\nid = "zeta"\n'
    doc["manifests"][ZETA] = {
        "name": "plugin.toml",
        "text": bad,
        "sha256": hashlib.sha256(bad.encode()).hexdigest(),
    }
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    roster.remove()
    assert not engines.is_retiring(ZETA)
    assert engines.removed_reason(ZETA) == "agent removed"
    assert ZETA in json.loads(p.read_text())["manifests"], "the unparsable copy is kept"


def test_an_AMBIGUOUS_live_socket_keeps_EVERY_plausible_owner_retiring_across_restarts(roster):
    """`my-agent-abc123.sock` is `my-agent:abc123` or `my:agent-abc123`. With both engines removed,
    neither recorded manifest may be discarded while that master lives (Hermes on #1132)."""
    import hashlib

    def manifest_for(eid: str) -> str:
        env = "AGENT_SESSIONS_" + eid.upper().replace("-", "") + "_BIN"
        return (
            ZETA_MANIFEST.replace('id = "zeta"', f'id = "{eid}"')
            .replace('name = "zeta"', f'name = "{eid}"')
            .replace("AGENT_SESSIONS_ZETA_BIN", env)
        )

    p = roster_state.state_path()
    doc = json.loads(p.read_text())
    for eid in ("my", "my-agent"):
        text = manifest_for(eid)
        doc["manifests"][eid] = {
            "name": "plugin.toml",
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    sock_path = ptybridge.socket_path("my", "agent-abc123")
    assert sock_path == ptybridge.socket_path("my-agent", "abc123"), "the premise: one file name"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    try:
        for _restart in range(2):
            roster.restart()
            assert engines.is_retiring("my") and engines.is_retiring("my-agent")
            kept = json.loads(p.read_text())["manifests"]
            assert "my" in kept and "my-agent" in kept, "no plausible owner's copy is discarded"
    finally:
        s.close()
        os.unlink(sock_path)
    roster.restart()  # the master is gone: now both are tombstoned
    assert engines.removed_reason("my") == engines.removed_reason("my-agent") == "agent removed"
