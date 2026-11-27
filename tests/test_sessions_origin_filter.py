"""The session list's "Started by" filter (#2437): sessions an automation LAUNCHED can be told
apart from every other session, and the sidebar hides them by default.

The source of truth is the automation store's launch provenance (`automation_origins`, kind
`session`), read through `automations_store.launched_among`: read-only, scoped to the keys the
request has, and "unknown" (``None``) kept apart from "none" (``set()``). Every store here is the
per-test tmp file `conftest._isolate_automations_db` points `AGENT_SESSIONS_AUTOMATIONS_DB` at.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import automations_store as store
from agent_sessions import lineage, metadata
from agent_sessions.main import create_app
from agent_sessions.routes import automations as auto_routes

_U1 = "11111111-1111-1111-1111-111111111111"
_U2 = "22222222-2222-2222-2222-222222222222"
_U3 = "33333333-3333-3333-3333-333333333333"
_REPO_A = "/home/user/claude/repo/a"  # sessions 1111 + 2222 (the fake_jsonl fixture)


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


def _automation(folder: str) -> str:
    """An enabled manual automation, made through the routes' own create + enable."""
    aid = auto_routes._create(
        {
            "name": f"auto {time.time_ns()}",
            "trigger": {"kind": "manual"},
            "action": {
                "kind": "start_session",
                "engine": "claude",
                "folder": folder,
                "message": {"text": "review the change"},
            },
            "policy": {"concurrency": "allow", "max_concurrent": 3},
        }
    )["id"]
    pub = auto_routes.public(store.get(aid))
    auto_routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _run_linked(key: str, *, origin: bool) -> None:
    """Claim a run and link ``key`` to it the way the runner does: ``origin=True`` for a session
    the automation LAUNCHED, ``origin=False`` for one it only sends to."""
    folder = Path.home() / "work"  # inside the (tmp) home: the action's folder must exist there
    folder.mkdir(exist_ok=True)
    aid = _automation(str(folder))
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    store.link(res["run"]["id"], session_key=key, origin=origin)


def _ids(page: dict) -> list[str]:
    return [s["uuid"] for s in page["sessions"]]


def _db_fingerprint(p: Path) -> tuple[str, int]:
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return digest, con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


# ---- the store read ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _home(fake_jsonl):
    """Every test here runs with ``$HOME`` = a tmp dir holding a few Claude Code sessions
    (`conftest.fake_jsonl`), so the `claude` engine an automation names is installed."""
    return fake_jsonl


def test_store_path_is_the_per_test_tmp_file():
    # Belt and braces for the live-data rule: this module must never read the operator's store.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # $HOME is patched; passwd is not
    live = real_home / ".config" / "agent-sessions" / "automations.db"
    assert store.db_path().resolve() != live.resolve()
    assert str(store.db_path()).startswith(os.environ["HOME"])
    assert "AGENT_SESSIONS_AUTOMATIONS_DB" in os.environ


def test_launched_among_missing_store_is_none_of_them():
    assert not store.db_path().exists()
    assert store.launched_among({"claude:a"}) == set()
    assert not store.db_path().exists(), "a read must never create the store"


def test_launched_among_tells_launched_from_messaged():
    _run_linked(f"claude:{_U1}", origin=True)
    _run_linked(f"claude:{_U2}", origin=False)
    got = store.launched_among({f"claude:{_U1}", f"claude:{_U2}", "claude:other", "", None})
    assert got == {f"claude:{_U1}"}


def test_launched_among_unreadable_or_foreign_file_is_unknown(tmp_path, monkeypatch):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database at all" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATIONS_DB", str(garbage))
    assert store.launched_among({"claude:a"}) is None
    foreign = tmp_path / "foreign.db"
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE something_else (x)")
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATIONS_DB", str(foreign))
    assert store.launched_among({"claude:a"}) is None


def test_launched_among_answers_many_keys_and_never_writes():
    _run_linked("claude:k-0007", origin=True)
    _run_linked("claude:k-1150", origin=True)
    before = _db_fingerprint(store.db_path())
    keys = {f"claude:k-{i:04d}" for i in range(1200)}  # more than SQLite's old 999-param cap
    assert store.launched_among(keys) == {"claude:k-0007", "claude:k-1150"}
    assert store.launched_among(set()) == set()
    assert _db_fingerprint(store.db_path()) == before


# ---- the list ----------------------------------------------------------------------------------


@pytest.fixture
def c(auth_cfg, fake_jsonl):
    client = _client(auth_cfg)
    _login(client, auth_cfg)
    return client


def test_no_origin_param_is_todays_list_with_the_stamp_and_facet(c):
    _run_linked(f"claude:{_U1}", origin=True)
    d = c.get("/api/sessions?limit=50").json()
    assert d["total"] == 4
    assert "origin_hidden" not in d and "origin_filter_unavailable" not in d
    by = {s["uuid"]: s["origin"] for s in d["sessions"]}
    assert by[_U1] == "automation"
    assert {by[u] for u in by if u != _U1} == {"operator"}
    assert d["facets"]["origins"] == {"operator": 3, "automation": 1}


def test_operator_hides_launched_sessions_and_says_how_many(c):
    _run_linked(f"claude:{_U1}", origin=True)
    d = c.get("/api/sessions?origin=operator&limit=50").json()
    assert _U1 not in _ids(d)
    assert d["total"] == 3 and len(d["sessions"]) == 3
    assert d["origin_hidden"] == {"count": 1, "needs_you": 0}
    a = c.get("/api/sessions?origin=automation&limit=50").json()
    assert _ids(a) == [_U1] and a["total"] == 1
    assert "origin_hidden" not in a


def test_a_session_an_automation_only_messaged_is_not_hidden(c):
    _run_linked(f"claude:{_U2}", origin=False)
    d = c.get("/api/sessions?origin=operator&limit=50").json()
    assert _U2 in _ids(d) and d["total"] == 4
    assert d["origin_hidden"] == {"count": 0, "needs_you": 0}


def test_facets_are_computed_before_the_origin_filter(c):
    _run_linked(f"claude:{_U1}", origin=True)
    every = c.get("/api/sessions?limit=50").json()["facets"]
    for q in ("origin=operator", "origin=automation", f"origin=operator&project={_REPO_A}"):
        assert c.get(f"/api/sessions?{q}&limit=50").json()["facets"] == every, q


def test_origin_ands_with_the_other_filters_and_the_hidden_count_follows_them(c):
    _run_linked(f"claude:{_U1}", origin=True)
    _run_linked(f"claude:{_U3}", origin=True)
    d = c.get(f"/api/sessions?origin=operator&project={_REPO_A}&limit=50").json()
    assert _ids(d) == [_U2]
    # Only U1 is in repo-a: Show would add exactly one row here, not two.
    assert d["origin_hidden"] == {"count": 1, "needs_you": 0}
    a = c.get(f"/api/sessions?origin=automation&project={_REPO_A}&limit=50").json()
    assert _ids(a) == [_U1]


def test_the_hidden_count_says_how_many_need_you(c, monkeypatch):
    _run_linked(f"claude:{_U1}", origin=True)
    real = lineage.row_state
    monkeypatch.setattr(
        lineage, "row_state", lambda r: "attention" if r["uuid"] == _U1 else real(r)
    )
    d = c.get("/api/sessions?origin=operator&limit=50").json()
    assert d["origin_hidden"] == {"count": 1, "needs_you": 1}


def test_origin_filters_before_paging(c):
    _run_linked(f"claude:{_U1}", origin=True)
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        p = c.get(f"/api/sessions?origin=operator&limit=1&offset={offset}").json()
        assert p["total"] == 3
        seen += _ids(p)
        offset = p["next_offset"]
    assert len(seen) == 3 and _U1 not in seen


def test_a_pinned_snapshot_keeps_the_filter_on_every_page(c):
    _run_linked(f"claude:{_U1}", origin=True)
    p0 = c.get("/api/sessions?origin=operator&limit=2&offset=0&snapshot=new").json()
    tok = p0["snapshot"]
    p1 = c.get(f"/api/sessions?origin=operator&limit=2&offset=2&snapshot={tok}").json()
    assert p0["total"] == p1["total"] == 3
    assert _U1 not in _ids(p0) + _ids(p1)
    assert len(_ids(p0) + _ids(p1)) == 3


def test_a_reconciled_placeholder_launch_still_matches(c):
    # The launch was recorded under the placeholder the master started as (#127); the list's row
    # is the real id, which resolves back to it.
    metadata.set_alias("claude:new-0000aaaa", f"claude:{_U1}")
    _run_linked("claude:new-0000aaaa", origin=True)
    d = c.get("/api/sessions?origin=operator&limit=50").json()
    assert _U1 not in _ids(d)
    assert d["origin_hidden"]["count"] == 1


def test_unknown_provenance_is_said_and_recovers(c, tmp_path, monkeypatch):
    _run_linked(f"claude:{_U1}", origin=True)
    good = os.environ["AGENT_SESSIONS_AUTOMATIONS_DB"]
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATIONS_DB", str(garbage))

    plain = c.get("/api/sessions?limit=50").json()
    assert {s["origin"] for s in plain["sessions"]} == {None}
    assert "origins" not in plain["facets"]
    # "Only yours" cannot be answered: the list stays whole and says so; nothing reads as 0 hidden.
    op = c.get("/api/sessions?origin=operator&limit=50").json()
    assert op["total"] == 4 and op["origin_filter_unavailable"] is True
    assert "origin_hidden" not in op
    # "Only automations" cannot be answered: an empty page that says why.
    au = c.get("/api/sessions?origin=automation&limit=50").json()
    assert au["sessions"] == [] and au["total"] == 0 and au["origin_filter_unavailable"] is True

    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATIONS_DB", good)
    back = c.get("/api/sessions?origin=operator&limit=50").json()
    assert back["total"] == 3 and "origin_filter_unavailable" not in back
    assert back["origin_hidden"] == {"count": 1, "needs_you": 0}


def test_an_unknown_origin_value_is_refused(c):
    assert c.get("/api/sessions?origin=everyone").status_code == 422


def test_a_hidden_session_still_opens_by_its_own_lookup_and_counts_on_the_dashboard(c):
    _run_linked(f"claude:{_U1}", origin=True)
    r = c.get(f"/api/sessions/claude:{_U1}")
    assert r.status_code == 200 and r.json()["origin"] == "automation"
    assert c.get("/api/dashboard/sessions").json()["recent"]["total"] == 4
