"""#1728 Phase 1 — client families and the interactive default (API first).

* The family is derived from what manifests already declare, never by trimming ids and never
  claimed: an API client belongs to its `api.source` agent's family; every other manifest is its
  own family, whatever its `display.order` (a local manifest cannot join another agent's family).
* `engines.default_client(family, purpose)` answers the BASE default with a typed reason
  (`api_ready`, `api_unready`, `api_absent`, `family_retired`); unattended admits API clients the
  facade marks `mission_ready` only, never a console fallback.
* `/api/engines` carries `family` per row and the `families` decision block.
* A create says whether its client is the `automatic` default or an `explicit` choice. Readiness
  changing between display and save refuses a stale automatic default (409 / ws 4412, the new
  default named, nothing launched) and honours an explicit one.
"""

from __future__ import annotations

import copy
import dataclasses
import tomllib
import types
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import engine_choice, engines, structured_runtime
from agent_sessions.engine_errors import EngineError
from agent_sessions.engines import family, registry
from agent_sessions.main import create_app
from agent_sessions.plugins import FIRST_PARTY_DIR, manifest

# --- helpers -------------------------------------------------------------------------------------


def _pair() -> tuple[str, str, str]:
    """``(family, console id, api id)`` of the first family with both clients — asked of the
    manifests, so no test names an engine it could drift from."""
    for p in engines.all_providers():
        if p.manifest.runtime == "api":
            src = engines.get(p.manifest.api.source)
            assert src is not None and src.manifest.family == p.manifest.family
            return p.manifest.family, src.engine_id, p.engine_id
    raise AssertionError("no first-party family has an API client")


def _console_only() -> tuple[str, str]:
    """A family whose only client is a console agent."""
    by_family: dict[str, list] = {}
    for p in engines.all_providers():
        by_family.setdefault(p.manifest.family, []).append(p)
    for fam, members in by_family.items():
        if len(members) == 1 and members[0].manifest.runtime == "pty":
            return fam, members[0].engine_id
    raise AssertionError("no console-only family")


@pytest.fixture
def ready(monkeypatch):
    """Which clients can start RIGHT NOW: every one, until a test says otherwise."""
    state: dict[str, bool] = {}
    monkeypatch.setattr(registry, "can_start", lambda p: state.get(p.engine_id, True))
    return state


def _doc(engine: str) -> dict:
    return tomllib.loads((FIRST_PARTY_DIR / engine / "plugin.toml").read_text())


def _client(cfg) -> TestClient:
    c = TestClient(create_app(cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": cfg.origin})
    return c


def _family_row(c: TestClient, fam: str) -> dict:
    body = c.get("/api/engines").json()
    return next(f for f in body["families"] if f["family"] == fam)


# --- the family derivation ----------------------------------------------------------------------


def test_an_api_client_is_in_the_family_of_its_api_source():
    fam, console, api = _pair()
    m = manifest.parse(_doc(api))
    assert m.family == m.api.source == console == fam
    # Pointed at another console agent, it is in THAT agent's family: the source decides, never
    # a shared prefix of the ids.
    other, _ = _console_only()
    doc = _doc(api)
    doc["api"]["source"] = other
    assert manifest.parse(doc).family == other
    # A console agent is its own family.
    assert manifest.parse(_doc(console)).family == console


def test_every_first_party_api_client_shares_its_source_agents_family():
    """A property of the derivation, for every reviewed manifest: no list to drift."""
    for path in sorted(FIRST_PARTY_DIR.glob("*/plugin.toml")):
        m = manifest.parse(tomllib.loads(path.read_text()))
        if m.runtime == "api":
            assert m.family == m.api.source == manifest.parse(_doc(m.api.source)).family, m.id
        else:
            assert m.family == m.id


def _local_console(console: str, new_id: str = "my-agent") -> manifest.Manifest:
    doc = _doc(console)
    doc["identity"]["id"] = new_id
    doc["binary"] = {**doc["binary"], "name": new_id, "env_var": "AGENT_SESSIONS_MYAGENT_BIN"}
    doc["binary"].pop("aliases", None)
    doc["session_id"]["legacy_bare_id"] = False
    doc["display"] = {**doc["display"], "name": new_id, "order": 1}  # ahead of every agent
    return manifest.parse(doc, source="local")


def test_a_manifest_cannot_claim_a_family():
    fam, console, _api = _pair()
    doc = _doc(console)
    doc["identity"]["family"] = fam  # the field does not exist: refused as unknown
    with pytest.raises(manifest.ManifestError):
        manifest.parse(doc)
    assert _local_console(console).family == "my-agent"


def test_a_local_manifest_never_becomes_another_familys_default(ready, monkeypatch):
    """The reviewed finding: a local console manifest placed FIRST in display order must not
    become a family's console fallback when its API client is unready."""
    fam, console, api = _pair()
    local = types.SimpleNamespace(
        engine_id="my-agent", manifest=_local_console(console), supports_new=True
    )
    real = registry.all_providers
    monkeypatch.setattr(registry, "all_providers", lambda: [local, *real()])
    ready[api] = False
    assert engines.family_of(local) == "my-agent"
    assert engines.default_client(fam, "interactive") == (console, "api_unready")
    lone, lone_console = _console_only()
    assert engines.default_client(lone, "interactive") == (lone_console, "api_absent")
    assert engines.default_client("my-agent", "interactive") == ("my-agent", "api_absent")
    rows = {f["family"]: f for f in family.families_block()}
    assert "my-agent" not in rows[fam]["clients"] and rows["my-agent"]["clients"] == ["my-agent"]


# --- the resolver --------------------------------------------------------------------------------


def test_interactive_prefers_a_ready_api_client(ready):
    fam, _console, api = _pair()
    assert engines.default_client(fam, "interactive") == (api, "api_ready")


def test_an_unready_api_client_leaves_the_console_as_the_default(ready):
    fam, console, api = _pair()
    ready[api] = False
    assert engines.default_client(fam, "interactive") == (console, "api_unready")
    ready[console] = False  # nothing can start: no engine, and the reason still says why
    assert engines.default_client(fam, "interactive") == (None, "api_unready")


def test_a_family_without_an_api_client_says_api_absent(ready):
    fam, console = _console_only()
    assert engines.default_client(fam, "interactive") == (console, "api_absent")


def test_an_api_client_follows_its_source_not_its_id(ready, monkeypatch):
    fam, console, api = _pair()
    prov = engines.get(api)
    other, other_console = _console_only()
    moved = dataclasses.replace(
        prov.manifest, api=dataclasses.replace(prov.manifest.api, source=other)
    )
    monkeypatch.setattr(prov, "manifest", moved)
    assert engines.family_of(api) == other
    assert engines.default_client(other, "interactive") == (api, "api_ready")
    # …and the console's family no longer has an API client.
    assert engines.default_client(fam, "interactive") == (console, "api_absent")
    assert other_console


def test_a_retired_family_has_no_default(ready, monkeypatch):
    fam, console, api = _pair()
    members = [engines.get(console), engines.get(api)]
    for p in members:
        monkeypatch.setattr(p, "retiring", True, raising=False)
    assert engines.default_client(fam, "interactive") == (None, "family_retired")
    assert engines.default_client("no-such-family", "interactive") == (None, "family_retired")


def test_unattended_admits_only_mission_ready_api_clients_never_a_console(ready, monkeypatch):
    fam, _console, api = _pair()
    # Today no client is mission_ready (#1273 P3): no default, and NO console fallback.
    assert engines.default_client(fam, "unattended") == (None, "api_unready")
    monkeypatch.setattr(family, "_mission_ready", lambda p: p.manifest.runtime == "api")
    assert engines.default_client(fam, "unattended") == (api, "api_ready")
    ready[api] = False
    assert engines.default_client(fam, "unattended") == (None, "api_unready")
    fam1, _ = _console_only()
    assert engines.default_client(fam1, "unattended") == (None, "api_absent")


def test_an_unknown_purpose_is_a_bug_not_a_default():
    fam, _console, _api = _pair()
    with pytest.raises(ValueError):
        engines.default_client(fam, "whatever")


# --- the roster API ------------------------------------------------------------------------------


def test_api_engines_serves_family_per_row_and_the_families_block(auth_cfg, tmp_home, ready):
    fam, console, api = _pair()
    ready[api] = False
    c = _client(auth_cfg)
    body = c.get("/api/engines").json()
    rows = {r["id"]: r for r in body["engines"]}
    assert rows[console]["family"] == rows[api]["family"] == fam
    row = next(f for f in body["families"] if f["family"] == fam)
    assert set(row) == {
        "family",
        "clients",
        "interactive_default",
        "unattended_admitted",
        "reasons",
    }
    assert set(row["clients"]) == {console, api}
    assert row["interactive_default"] == {"engine": console, "reason": "api_unready"}
    assert row["unattended_admitted"] == []
    assert api in row["reasons"] and console not in row["reasons"]
    # Every engine row is in exactly one family row.
    listed = [e for f in body["families"] for e in f["clients"]]
    assert sorted(listed) == sorted(rows)
    assert all(f["interactive_default"]["reason"] in family.REASONS for f in body["families"])


def test_a_retiring_family_is_listed_retired_with_its_reason(
    auth_cfg, tmp_home, ready, monkeypatch
):
    fam, console, api = _pair()
    members = [engines.get(console), engines.get(api)]
    others = [p for p in registry.all_providers() if p not in members]
    for p in members:
        monkeypatch.setattr(p, "retiring", True, raising=False)
    monkeypatch.setattr(registry, "all_providers", lambda: list(others))
    monkeypatch.setattr(registry, "retiring_providers", lambda: list(members))
    row = family.families_block()
    fr = next(f for f in row if f["family"] == fam)
    assert fr["interactive_default"] == {"engine": None, "reason": "family_retired"}
    assert fr["reasons"] == {console: engines.REMOVED_REASON, api: engines.REMOVED_REASON}


# --- readiness changing between display and save ------------------------------------------------


@pytest.fixture
def creates(monkeypatch):
    """Structured creates that reached the facade (none launches here)."""
    calls: list[dict] = []

    async def fake_create(engine, cwd, **kw):
        calls.append({"engine": engine, "cwd": cwd, **kw})
        return {"session_key": f"{engine}:{kw['operation_id']}"}

    monkeypatch.setattr(structured_runtime, "create_session", fake_create)
    return calls


def _create(c: TestClient, engine: str, **extra):
    return c.post(
        "/api/structured/sessions",
        json={"engine": engine, "cwd": "/tmp", "operation_id": str(uuid.uuid4()), **extra},
    )


def test_a_stale_automatic_api_default_is_refused_with_the_new_default(
    auth_cfg, tmp_home, ready, creates
):
    fam, console, api = _pair()
    c = _client(auth_cfg)
    # On display the API client is the default…
    assert _family_row(c, fam)["interactive_default"] == {"engine": api, "reason": "api_ready"}
    ready[api] = False  # …and stops being ready before Start.
    r = _create(c, api, choice="automatic")
    assert r.status_code == 409
    assert r.json()["default"] == {"family": fam, "engine": console, "reason": "api_unready"}
    assert "nothing was started" in r.json()["detail"]
    assert creates == []  # nothing launched


def test_an_explicit_api_choice_is_honoured_when_the_base_moved(
    auth_cfg, tmp_home, ready, creates, monkeypatch
):
    fam, console, api = _pair()
    c = _client(auth_cfg)
    # The base is the console (say the API client WAS unready on display, then became ready —
    # or the operator picked the API tile against the console default): explicit is honoured.
    monkeypatch.setattr(family, "default_client", lambda f, p: (console, "api_unready"))
    r = _create(c, api, choice="explicit")
    assert r.status_code == 201, r.text
    assert [x["engine"] for x in creates] == [api]


def test_no_choice_is_the_create_it_always_was_and_a_bad_choice_is_refused(
    auth_cfg, tmp_home, ready, creates
):
    _fam, _console, api = _pair()
    ready[api] = False  # the base moved, but an older client says nothing about provenance
    c = _client(auth_cfg)
    assert _create(c, api).status_code == 201
    r = _create(c, api, choice="whatever")
    assert r.status_code == 422 and "choice" in r.json()["detail"]
    assert [x["engine"] for x in creates] == [api]


def _ws_headers(c, cfg) -> dict:
    return {"Origin": cfg.origin, "Cookie": f"agent_sessions={c.cookies.get('agent_sessions')}"}


def _close(c, url, headers) -> tuple[int | None, str]:
    try:
        with c.websocket_connect(url, headers=headers) as ws:
            msg = ws.receive()
            if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                return msg.get("code"), msg.get("reason") or ""
            return None, ""
    except WebSocketDisconnect as e:
        return e.code, e.reason or ""


@pytest.fixture
def console_launch(fake_jsonl, monkeypatch):
    """The console client of the pair, with its argv builder captured and then refused (4500)."""
    _fam, console, _api = _pair()
    prov = engines.get(console)
    monkeypatch.setattr(prov, "_entry_path", lambda: "/opt/bin/agent")
    built: list = []

    def spy(*a, **kw):
        built.append(kw)
        raise EngineError("captured")

    monkeypatch.setattr(prov, "new_launch_argv", spy)
    cwd = fake_jsonl / "proj"
    cwd.mkdir()
    return {"engine": console, "cwd": cwd, "built": built}


def _new_url(launch, choice: str | None) -> str:
    native = str(uuid.uuid4())
    q = f"new=1&cwd={launch['cwd']}" + (f"&choice={choice}" if choice else "")
    return f"/ws/term/{launch['engine']}:{native}?{q}"


def test_a_stale_automatic_terminal_default_is_refused_before_anything_is_built(
    auth_cfg, ready, console_launch
):
    fam, console, api = _pair()
    ready[api] = False
    c = _client(auth_cfg)
    # On display the console is the default (the API client is not ready)…
    assert _family_row(c, fam)["interactive_default"] == {
        "engine": console,
        "reason": "api_unready",
    }
    ready[api] = True  # …and the API client becomes ready before Start.
    code, reason = _close(c, _new_url(console_launch, "automatic"), _ws_headers(c, auth_cfg))
    assert code == engine_choice.WS_STALE_DEFAULT
    assert engines.get(api).manifest.identity.label in reason
    assert console_launch["built"] == []  # nothing resolved, nothing spawned


def test_an_explicit_terminal_choice_is_honoured_when_the_api_client_became_ready(
    auth_cfg, ready, console_launch
):
    c = _client(auth_cfg)
    code, _ = _close(c, _new_url(console_launch, "explicit"), _ws_headers(c, auth_cfg))
    assert code == 4500  # past the choice check: the (captured) argv was built
    assert len(console_launch["built"]) == 1
    # An older client without a choice launches as it always did.
    code, _ = _close(c, _new_url(console_launch, None), _ws_headers(c, auth_cfg))
    assert code == 4500 and len(console_launch["built"]) == 2
    code, _ = _close(c, _new_url(console_launch, "bogus"), _ws_headers(c, auth_cfg))
    assert code == 4422 and len(console_launch["built"]) == 2


def test_an_automatic_choice_that_is_still_the_default_launches(auth_cfg, ready, console_launch):
    _fam, _console, api = _pair()
    ready[api] = False
    c = _client(auth_cfg)
    code, _ = _close(c, _new_url(console_launch, "automatic"), _ws_headers(c, auth_cfg))
    assert code == 4500 and len(console_launch["built"]) == 1


def test_the_family_block_never_names_a_client_outside_the_roster(ready):
    ids = set(engines.engine_ids())
    for f in family.families_block():
        assert set(f["clients"]) <= ids | {p.engine_id for p in engines.retiring_providers()}
        d = copy.deepcopy(f["interactive_default"])
        assert d["engine"] is None or d["engine"] in f["clients"]


def _interrupted_native_create(engine: str, op: str) -> None:
    """What `native_runtime._create_sync` leaves when it is interrupted after writing the session
    record and BEFORE the conversation journal exists: the record only."""
    from agent_sessions import native_state

    native_state.write_session(
        op,
        {
            "version": 1,
            "session_key": f"{engine}:{op}",
            "adapter": engines.manifest_of(engine).api.kind,
            "source_engine": engines.manifest_of(engine).api.source,
            "request": {
                "cwd": "/tmp",
                "model": None,
                "adapter": engines.manifest_of(engine).api.kind,
            },
            "owner_token": str(uuid.uuid4()),
            "native_id": None,
            "current_worker": None,
            "closed": False,
            "workers": [],
            "created_at": 0.0,
        },
    )


def test_a_retried_automatic_create_replays_instead_of_refusing(auth_cfg, tmp_home, ready, creates):
    """The first create recorded its native session (and was interrupted before the journal, or
    its response was lost); the base default moved before the retry (same operation id). The
    retry reaches the create's own replay/recovery path, never a 409 "nothing was started"."""
    _fam, _console, api = _pair()
    c = _client(auth_cfg)
    op = str(uuid.uuid4())
    _interrupted_native_create(api, op)
    assert engines.resolve_session(api, op) is None  # no journal: the store cannot see it
    ready[api] = False
    body = {"engine": api, "cwd": "/tmp", "operation_id": op, "choice": "automatic"}
    r = c.post("/api/structured/sessions", json=body)
    assert r.status_code == 201, r.text
    assert [x["operation_id"] for x in creates] == [op]  # reached native creation's own path
    # A record under that id for ANOTHER client is not this create's replay: still refused.
    other_api = next(
        p.engine_id
        for p in engines.all_providers()
        if p.manifest.runtime == "api" and p.engine_id != api
    )
    other = str(uuid.uuid4())
    _interrupted_native_create(other_api, other)
    r = c.post("/api/structured/sessions", json={**body, "operation_id": other})
    assert r.status_code == 409 and len(creates) == 1
    # A NEW operation id with the same stale choice is still refused, nothing reaches the facade.
    r = c.post("/api/structured/sessions", json={**body, "operation_id": str(uuid.uuid4())})
    assert r.status_code == 409 and len(creates) == 1
