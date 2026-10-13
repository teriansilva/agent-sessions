"""A `/s/<source>/<native-id>` link to an API session's native history opens its owner (#1405).

An API session (`codex-api`, `claude-api`, `opencode-api`) has its own BattleLab id; the vendor
history behind it is hidden from its console engine (`native_discovery.filter_cached`) so one
conversation is not listed twice. `GET /api/sessions/<source>:<native>` therefore missed. On a
GENUINE miss the route now asks the ownership ledger (read-only, bound rows only) and answers with
the owner's row, through the same hard scope, marked `resolved_from` so the client can tell a
declared alias from a misrouted row. Everything else answers exactly as before.
"""

from __future__ import annotations

import time
import uuid

import pytest

import test_webterm
from agent_sessions import engines, native_ownership, prefs, project_dirs, webterm
from agent_sessions.plugins import admission, storage
from agent_sessions.routes import sessions as sessions_routes
from agent_sessions.scanner import Session
from test_api import _client, _login

# (console source, its API client, a native id in the source's own id shape)
PAIRS = [
    ("codex", "codex-api", "01a1265f-814b-7301-ab6f-8aaefc65672a"),
    ("claude", "claude-api", "24c5e209-0000-4000-8000-000000000001"),
    ("opencode", "opencode-api", "ses_ee02dcd9DeepLinkFixture"),
]
UNOWNED = {
    "codex": "00000000-0000-4000-8000-0000000000c0",
    "claude": "00000000-0000-4000-8000-0000000000c1",
    "opencode": "ses_unownedConsoleHistory",
}
CWD = "/work/proj"


@pytest.fixture
def stores(tmp_home, monkeypatch):
    """Every console source resolves to a private store under the tmp home."""
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(tmp_home / "codex-sessions"))
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / "opencode" / "opencode.db"))
    # No roots / exclusions unless a test sets one: the hard scope is open by default.
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda: [])
    return tmp_home


@pytest.fixture
def walk(monkeypatch):
    """The provider walk, as the scan would return it BEFORE the ownership filter."""
    rows: list[Session] = []
    monkeypatch.setattr(engines, "scan_all", lambda: list(rows))
    engines.set_scan_cache_ttl(0)
    engines.invalidate_scan_cache()
    yield rows
    engines.invalidate_scan_cache()


def _row(engine: str, native: str, *, cwd: str = CWD) -> Session:
    return Session(engine, native, cwd, time.time(), f"fixture {engine}", False)


def _own(source: str, api: str, native: str | None) -> str:
    """Reserve (and, given a native id, bind) a history for a new API session; its key."""
    prov = engines.get(source)
    key = f"{api}:{uuid.uuid4()}"
    with storage.locked(admission.LOCK):
        intent = native_ownership.reserve(
            key,
            native_ownership.source_identity(prov),
            operation_id=str(uuid.uuid4()),
            owner_token=str(uuid.uuid4()),
            request={"fixture": True},
        )
        if native is not None:
            native_ownership.bind(
                key,
                operation_id=intent.operation_id,
                owner_token=intent.owner_token,
                native_id=native,
            )
    return key


def _setup(walk, source, api, native):
    owner = _own(source, api, native)
    walk[:] = [
        _row(source, native),  # on disk, but owned: hidden by the discovery filter
        _row(source, UNOWNED[source]),
        _row(api, owner.partition(":")[2]),
    ]
    return owner


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_an_owned_native_id_answers_with_the_owner_row(auth_cfg, stores, walk, source, api, native):
    owner = _setup(walk, source, api, native)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    direct = c.get(f"/api/sessions/{owner}")
    assert direct.status_code == 200 and "resolved_from" not in direct.json()

    r = c.get(f"/api/sessions/{source}:{native}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == owner and body["engine"] == api
    assert body["resolved_from"] == f"{source}:{native}"
    # Exactly the owner's own row — nothing more is learned through the native id.
    assert {k: v for k, v in body.items() if k != "resolved_from"} == direct.json()
    # Still hidden from the console listing: the double-listing filter is unchanged.
    listed = {s["id"] for s in c.get("/api/sessions?limit=100").json()["sessions"]}
    assert f"{source}:{native}" not in listed and owner in listed


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_an_owned_native_id_resolves_without_a_re_walk(
    auth_cfg, stores, walk, monkeypatch, source, api, native
):
    """The ledger is asked before the miss re-walk: a bound history is hidden from discovery
    whatever the snapshot's age, so the cold walk could never find it and only delayed the
    redirect while the pane showed "not found"."""
    owner = _setup(walk, source, api, native)
    # The permit is free, so the re-walk WOULD run if the route reached it.
    monkeypatch.setattr(sessions_routes, "_last_miss_rewalk", 0.0)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rewalks = []
    real = engines.invalidate_scan_cache
    monkeypatch.setattr(engines, "invalidate_scan_cache", lambda: (rewalks.append(1), real()))
    r = c.get(f"/api/sessions/{source}:{native}")
    assert r.status_code == 200
    assert r.json()["id"] == owner and r.json()["resolved_from"] == f"{source}:{native}"
    assert rewalks == [], "an owned native id paid the miss re-walk"


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_an_owner_missing_from_a_stale_snapshot_resolves_after_the_re_walk(
    auth_cfg, stores, walk, monkeypatch, source, api, native
):
    """An API session created moments ago is bound in the ledger but absent from a warm
    snapshot. The early ledger answer finds no owner row, so the route still re-walks and asks
    again from fresh data instead of answering 404."""
    owner = _setup(walk, source, api, native)
    owner_row = walk.pop()  # not on disk when the snapshot below is taken
    monkeypatch.setattr(sessions_routes, "_last_miss_rewalk", 0.0)
    engines.set_scan_cache_ttl(60.0)
    try:
        engines.invalidate_scan_cache()
        assert all(engines.session_key(s) != owner for s in engines.scan_all_cached())
        walk.append(owner_row)
        c = _client(auth_cfg)
        _login(c, auth_cfg)
        r = c.get(f"/api/sessions/{source}:{native}")
        assert r.status_code == 200
        assert r.json()["id"] == owner and r.json()["resolved_from"] == f"{source}:{native}"
    finally:
        engines.set_scan_cache_ttl(0)


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_an_owner_outside_the_hard_scope_stays_unknown(
    auth_cfg, stores, walk, monkeypatch, source, api, native
):
    owner = _setup(walk, source, api, native)
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda: [CWD])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/sessions/{owner}").status_code == 404
    assert c.get(f"/api/sessions/{source}:{native}").status_code == 404


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_a_real_console_session_never_consults_the_ledger(
    auth_cfg, stores, walk, monkeypatch, source, api, native
):
    _setup(walk, source, api, native)

    def forbidden(*_args, **_kwargs):
        pytest.fail("a found console row must not consult ownership")

    monkeypatch.setattr(native_ownership, "owner_of", forbidden)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/{source}:{UNOWNED[source]}")
    assert r.status_code == 200
    assert r.json()["id"] == f"{source}:{UNOWNED[source]}"
    assert "resolved_from" not in r.json()


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_unknown_and_pending_ids_stay_404(auth_cfg, stores, walk, source, api, native):
    _setup(walk, source, api, native)
    _own(source, api, None)  # a pending reservation has no native id to resolve to
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    unknown = (
        "ses_neverSeenAnywhere" if source == "opencode" else "99999999-9999-4999-8999-999999999999"
    )
    assert c.get(f"/api/sessions/{source}:{unknown}").status_code == 404


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_an_unreadable_ledger_stays_404(auth_cfg, stores, walk, source, api, native):
    _setup(walk, source, api, native)
    db = native_ownership._paths()[1]
    db.write_bytes(b"not sqlite")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/sessions/{source}:{native}").status_code == 404


def test_a_provider_with_no_api_source_never_opens_the_ledger(auth_cfg, stores, walk, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("an engine with no API source must not open the ownership ledger")

    monkeypatch.setattr(native_ownership, "owner_of", forbidden)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get(f"/api/sessions/shell:{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize(("source", "api", "native"), PAIRS)
def test_the_native_keys_terminal_socket_is_refused_and_spawns_nothing(
    auth_cfg, stores, walk, monkeypatch, source, api, native
):
    """The SPA mounts a terminal for the native key until the owner row arrives. That socket
    must be refused by console admission before any process is created (#1405 decision)."""
    _setup(walk, source, api, native)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("a deep link to an API-owned history reached console process creation")

    monkeypatch.setattr(webterm.asyncio, "create_subprocess_exec", forbidden)
    client = test_webterm._client(auth_cfg)
    headers = test_webterm._login_headers(client, auth_cfg)
    code = test_webterm._close_code(client, f"/ws/term/{source}:{native}", headers)
    assert code is not None and code >= 4000, code
