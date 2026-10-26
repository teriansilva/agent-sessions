"""The specialist pool (#1693, #1613 P3 S1): the model → grade map, the draft checks, the versioned
store, and the routes. The grade is derived at read; versions are immutable; every write is fenced.
"""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, model_choice, model_grades, specialists
from agent_sessions import specialists_store as store

LIB = {"tdd": "r-tdd-1", "git-hygiene": "r-gh-1"}


def _draft(**over):
    d = {
        "title": "Implementer",
        "summary": "Builds a phase to its acceptance criteria.",
        "skills": [{"id": "tdd", "revision": "r-tdd-1"}],
        "engine": "claude",
        "model": "claude-opus-5",
        "effort": None,
        "capacity": 2,
    }
    d.update(over)
    return d


@pytest.fixture
def startable(monkeypatch):
    """Every registered engine can start (no binaries on the test host)."""
    monkeypatch.setattr(engines.registry, "can_start", lambda prov: prov is not None)


def _no_mcp_agent() -> str:
    """A registered agent with no tool channel, asked of the manifests (never named)."""
    for p in engines.all_providers():
        m = p.manifest
        if m and m.identity.kind == "agent" and m.tools_mcp is None and m.runtime == "pty":
            return p.engine_id
    pytest.skip("no agent without a tool channel is registered")


# ---- model grades -------------------------------------------------------------------------------


def test_grades_ship_empty_and_default_is_never_graded():
    assert model_grades.get() == {}
    assert model_grades.grade_of("claude", "claude-opus-5") is None
    assert model_grades.grade_of("claude", "default", {"claude": {"default": "fast"}}) is None


def test_grade_patch_merges_and_null_clears():
    model_grades.apply_patch({"claude": {"claude-opus-5": "strong", "claude-haiku-4-5": "fast"}})
    model_grades.apply_patch({"claude": {"claude-haiku-4-5": None, "claude-fable-5-1": "frontier"}})
    assert model_grades.get() == {
        "claude": {"claude-opus-5": "strong", "claude-fable-5-1": "frontier"}
    }
    model_grades.apply_patch({"claude": {"claude-opus-5": None, "claude-fable-5-1": None}})
    assert model_grades.get() == {}


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({}, "at least one agent"),
        ({"no-such-agent": {"m": "fast"}}, "unknown agent"),
        ({"claude": {"default": "fast"}}, "default model"),
        ({"claude": {"claude-opus-5": "genius"}}, "grade must be"),
        ({"claude": {"-bad": "fast"}}, "not a model id"),
        ({"claude": {}}, "at least one model"),
    ],
)
def test_grade_patch_refusals(patch, needle):
    err = model_grades.validate_patch(patch)
    assert err is not None and needle in err


def test_terminals_cannot_be_graded():
    terminals = engines.ids_where(lambda m: m.identity.kind != "agent")
    assert terminals, "the roster has a terminal engine"
    assert "unknown agent" in model_grades.validate_patch({terminals[0]: {"x": "fast"}})


def test_grade_read_drops_malformed_rows(monkeypatch):
    from agent_sessions import prefs

    monkeypatch.setattr(
        prefs,
        "read_block",
        lambda key: {
            "rev": 3,
            "map": {"claude": {"ok-model": "fast", "x": "genius", "default": "fast"}, "BAD": {}},
        },
    )
    assert model_grades.get() == {"claude": {"ok-model": "fast"}}
    assert model_grades.get_versioned() == {"rev": 3, "map": {"claude": {"ok-model": "fast"}}}


@pytest.mark.parametrize(
    "stored",
    [
        {"claude": {"m": "fast"}},  # a bare map, not the stored shape
        {"rev": -1, "map": {"claude": {"m": "fast"}}},
        {"rev": True, "map": {"claude": {"m": "fast"}}},
        {"rev": 1, "map": {}, "extra": 1},
        "junk",
    ],
)
def test_anything_but_the_stored_shape_reads_as_rev_0_and_empty(monkeypatch, stored):
    from agent_sessions import prefs

    monkeypatch.setattr(prefs, "read_block", lambda key: stored)
    assert model_grades.get_versioned() == {"rev": 0, "map": {}}


def test_every_commit_bumps_the_revision():
    a = model_grades.apply_patch({"claude": {"claude-opus-5": "strong"}})
    b = model_grades.apply_patch({"claude": {"claude-opus-5": None}})
    c = model_grades.apply_patch({"claude": {"claude-haiku-4-5": "fast"}})
    assert [a["rev"], b["rev"], c["rev"]] == [1, 2, 3]
    assert c["map"] == {"claude": {"claude-haiku-4-5": "fast"}}
    assert model_grades.get_versioned() == c


def test_rank_orders_grades():
    assert [model_grades.rank(g) for g in (None, "fast", "strong", "frontier")] == [0, 1, 2, 3]


# ---- draft checks -------------------------------------------------------------------------------


def test_a_valid_terminal_draft_is_canonical(startable):
    body = specialists.check_draft(_draft(model="opus", title="  Implementer  "), library=LIB)
    assert body["model"] == "claude-opus-5"  # the alias is stored as the id it names
    assert body["title"] == "Implementer"
    assert body["effort"] is None


def test_default_model_is_stored_as_default(startable):
    assert specialists.check_draft(_draft(model="default"), library=LIB)["model"] == "default"


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"extra": 1}, "unknown field"),
        ({"title": "   "}, "title is required"),
        ({"title": "x" * 61}, "longer than"),
        ({"title": "a\u202eb"}, "control"),
        ({"title": "a\ud800b"}, "invalid characters"),
        ({"summary": "\udfff"}, "invalid characters"),
        ({"capacity": 0}, "capacity"),
        ({"capacity": 9}, "capacity"),
        ({"capacity": True}, "capacity"),
        ({"skills": [{"id": "nope", "revision": "r"}]}, "not in your skills library"),
        ({"skills": [{"id": "tdd", "revision": "r-tdd-0"}]}, "changed since you loaded it"),
        ({"skills": [{"id": "tdd", "revision": "r-tdd-1"}] * 2}, "listed twice"),
        ({"skills": [{"id": "Bad Id", "revision": "r"}]}, "malformed"),
        ({"engine": ""}, "choose the agent"),
        ({"engine": "no-such-agent"}, "unknown agent"),
        ({"model": "gpt-from-elsewhere"}, "not offered"),
        ({"effort": "high"}, "takes no effort"),
    ],
)
def test_draft_refusals(startable, over, needle):
    with pytest.raises(specialists.DraftRefused) as e:
        specialists.check_draft(_draft(**over), library=LIB)
    assert needle in e.value.detail


def test_missing_fields_are_refused(startable):
    d = _draft()
    del d["capacity"]
    with pytest.raises(specialists.DraftRefused, match="missing field"):
        specialists.check_draft(d, library=LIB)


def test_a_terminal_engine_cannot_hold_a_specialist(startable):
    terminal = engines.ids_where(lambda m: m.identity.kind != "agent")[0]
    with pytest.raises(specialists.DraftRefused, match="unknown agent"):
        specialists.check_draft(_draft(engine=terminal, model="default", skills=[]), library=LIB)


def test_an_agent_that_cannot_start_is_refused(monkeypatch):
    monkeypatch.setattr(engines.registry, "can_start", lambda prov: False)
    with pytest.raises(specialists.DraftRefused, match="cannot start"):
        specialists.check_draft(_draft(), library=LIB)


def test_skills_on_an_agent_without_a_tool_channel_are_refused(startable):
    engine = _no_mcp_agent()
    with pytest.raises(specialists.DraftRefused, match="cannot load skills"):
        specialists.check_draft(_draft(engine=engine, model="default"), library=LIB)
    # The same agent holds a specialist WITHOUT skills.
    body = specialists.check_draft(_draft(engine=engine, model="default", skills=[]), library=LIB)
    assert body["engine"] == engine and body["skills"] == []


def test_an_api_client_checks_model_and_effort_against_one_snapshot(startable, monkeypatch):
    api = next(
        (p for p in engines.all_providers() if p.manifest and p.manifest.runtime == "api"), None
    )
    if api is None:
        pytest.skip("no API client registered")
    seen = []

    def choice(prov, model, effort):
        seen.append((prov.engine_id, model, effort))
        if effort == "ludicrous":
            raise model_choice.ModelRefused("not_offered", "offers no effort 'ludicrous'")
        return model, effort

    monkeypatch.setattr(model_choice, "select_api_choice", choice)
    body = specialists.check_draft(
        _draft(engine=api.engine_id, model="m-1", effort="high", skills=[]), library=LIB
    )
    assert (body["model"], body["effort"]) == ("m-1", "high")
    assert seen == [(api.engine_id, "m-1", "high")]
    with pytest.raises(specialists.DraftRefused, match="ludicrous"):
        specialists.check_draft(
            _draft(engine=api.engine_id, model="m-1", effort="ludicrous", skills=[]), library=LIB
        )


# ---- store --------------------------------------------------------------------------------------


def _body(**over):
    b = {
        "title": "Implementer",
        "summary": "",
        "skills": [{"id": "tdd", "revision": "r-tdd-1"}],
        "engine": "claude",
        "model": "claude-opus-5",
        "effort": None,
        "capacity": 2,
    }
    b.update(over)
    return b


def test_store_file_is_private():
    store.create(_body())
    assert stat.S_IMODE(os.stat(store.db_path()).st_mode) == 0o600


def test_edit_makes_a_new_version_and_never_rewrites_the_old_one():
    s = store.create(_body())
    assert (s["id"], s["revision"], s["current"]["version"]) == ("implementer", 1, 1)
    s2 = store.update(s["id"], _body(capacity=5, title="Builder"), expect_revision=1)
    assert s2["current"]["version"] == 2 and s2["revision"] == 2
    assert store.version(s["id"], 1)["capacity"] == 2  # what v1 pinned, untouched
    assert [v["version"] for v in store.versions(s["id"])] == [2, 1]


def test_writes_are_revision_fenced():
    s = store.create(_body())
    store.update(s["id"], _body(capacity=3), expect_revision=1)
    with pytest.raises(store.Conflict):
        store.update(s["id"], _body(capacity=4), expect_revision=1)
    with pytest.raises(store.Conflict):
        store.set_archived(s["id"], True, expect_revision=1)


def test_archive_hides_from_edits_but_keeps_versions_readable():
    s = store.create(_body())
    a = store.set_archived(s["id"], True, expect_revision=1)
    assert a["archived"] and a["revision"] == 2
    with pytest.raises(store.StoreError, match="unarchive"):
        store.update(s["id"], _body(), expect_revision=2)
    assert store.version(s["id"], 1)["title"] == "Implementer"
    assert not store.set_archived(s["id"], False, expect_revision=2)["archived"]


def test_ids_are_unique_slugs():
    a, b = store.create(_body()), store.create(_body())
    assert a["id"] == "implementer" and b["id"].startswith("implementer-") and a["id"] != b["id"]
    assert store.ID_RE.fullmatch(b["id"])
    assert store.create(_body(title="Ünïcode!!"))["id"]  # always some valid slug


def test_a_newer_schema_is_refused_and_left_untouched():
    # A ROLLBACK-JOURNAL file, as another build might leave it: switching it to WAL would rewrite
    # its header, so the version must be refused before anything writes.
    store.db_path().parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(store.db_path())
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE future (x)")
    con.execute("PRAGMA user_version=99")
    con.commit()
    con.close()
    store.reset_schema_cache_for_test()
    before = store.db_path().read_bytes()
    with pytest.raises(store.StoreUnsupported):
        store.list_all()
    with pytest.raises(store.StoreUnsupported):
        store.create(_body())
    assert store.db_path().read_bytes() == before


def test_a_locked_or_corrupt_store_is_a_503_with_a_reason(monkeypatch):
    store.create(_body())
    real = store._raw_connect

    class Locked:
        def __init__(self, con):
            self._con = con

        def __getattr__(self, name):
            return getattr(self._con, name)

        def execute(self, sql, *a):
            if sql.startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return self._con.execute(sql, *a)

    # A scoped patch: `monkeypatch.undo()` would also drop conftest's store-path pins.
    with monkeypatch.context() as m:
        m.setattr(store, "_raw_connect", lambda p: Locked(real(p)))
        with pytest.raises(store.StoreError) as e:
            store.create(_body())
    assert e.value.status == 503 and "database is locked" in str(e.value)
    store.db_path().write_bytes(b"not a database at all" * 100)
    store.reset_schema_cache_for_test()
    with pytest.raises(store.StoreError) as e:
        store.list_all()
    assert e.value.status == 503


@pytest.mark.parametrize(
    "damage",
    [
        "not json",
        '"a string"',
        '{"skills": ["bad"]}',
        '{"title": "x", "summary": "", "skills": [], "engine": "e", "model": "m", "effort": null, '
        '"capacity": true}',
        '{"title": "\\ud800", "summary": "", "skills": [], "engine": "e", "model": "m", '
        '"effort": null, "capacity": 1}',
        # Valid fields plus one it never writes, here a NaN that no JSON response can carry.
        '{"title": "x", "summary": "", "skills": [], "engine": "e", "model": "m", '
        '"effort": null, "capacity": 1, "extra": NaN}',
        '{"title": "x", "summary": "", "skills": [{"id": "a", "revision": "r", "x": 1}], '
        '"engine": "e", "model": "m", "effort": null, "capacity": 1}',
    ],
)
def test_a_damaged_version_body_is_isolated_through_projection(damage):
    bad = store.create(_body())
    good = store.create(_body(title="Reviewer"))
    store.update(good["id"], _body(title="Reviewer", capacity=3), expect_revision=1)
    con = sqlite3.connect(store.db_path())
    con.execute("UPDATE specialist_versions SET body = ? WHERE id = ?", (damage, bad["id"]))
    # A damaged OLD version of a healthy specialist hides nothing either.
    con.execute(
        "UPDATE specialist_versions SET body = ? WHERE id = ? AND version = 1", (damage, good["id"])
    )
    con.commit()
    con.close()
    listed = store.list_all()
    assert [s["id"] for s in listed] == [good["id"]]
    assert [store.public(s, library=LIB)["grade"] for s in listed] == [None]
    assert [v["version"] for v in store.versions(good["id"])] == [2]


def test_one_damaged_row_never_hides_the_rest_of_the_pool():
    a = store.create(_body())
    b = store.create(_body(title="Reviewer"))
    con = sqlite3.connect(store.db_path())
    con.execute("DELETE FROM specialist_versions WHERE id = ?", (a["id"],))
    con.commit()
    con.close()
    assert [s["id"] for s in store.list_all()] == [b["id"]]


def test_grade_is_derived_at_read_never_stored():
    s = store.create(_body())
    assert store.public(s, library=LIB)["grade"] is None
    model_grades.apply_patch({"claude": {"claude-opus-5": "strong"}})
    assert store.public(store.get(s["id"]), library=LIB)["grade"] == "strong"
    model_grades.apply_patch({"claude": {"claude-opus-5": "frontier"}})
    assert store.public(store.get(s["id"]), library=LIB)["grade"] == "frontier"
    assert "grade" not in store.version(s["id"], 1)


def test_missing_and_outdated_skills_are_flagged_and_an_unreadable_library_flags_nothing():
    s = store.create(
        _body(skills=[{"id": "tdd", "revision": "r-tdd-1"}, {"id": "gone", "revision": "r"}])
    )
    out = store.public(s, library={"tdd": "r-tdd-2"})
    assert out["skills_missing"] == ["gone"] and out["skills_outdated"] == ["tdd"]
    unknown = store.public(s, library=None)
    assert unknown["skills_missing"] == [] and unknown["skills_outdated"] == []


# ---- routes -------------------------------------------------------------------------------------


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


@pytest.fixture
def api(auth_cfg, startable, monkeypatch):
    monkeypatch.setattr(specialists, "library_revisions", lambda: dict(LIB))
    c = _client(auth_cfg)
    return c, _login(c, auth_cfg)


def test_routes_need_login_and_csrf(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/specialists").status_code == 401
    hdr = _login(c, auth_cfg)
    assert c.post("/api/specialists", json=_draft()).status_code == 403
    assert (
        c.post("/api/specialists", json=_draft(), headers={"Origin": auth_cfg.origin}).status_code
        == 403
    )
    r = c.get("/api/specialists")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert hdr  # logged in


def test_every_write_needs_csrf_and_origin(api, auth_cfg):
    c, hdr = api
    sid = c.post("/api/specialists", json=_draft(), headers=hdr).json()["id"]
    writes = [
        ("patch", f"/api/specialists/{sid}", {"revision": 1, "draft": _draft(capacity=3)}),
        ("post", f"/api/specialists/{sid}/archive", {"revision": 1}),
        ("post", f"/api/specialists/{sid}/unarchive", {"revision": 1}),
    ]
    for method, url, body in writes:
        no_token = getattr(c, method)(url, json=body, headers={"Origin": auth_cfg.origin})
        assert no_token.status_code == 403, url
        wrong_origin = {**hdr, "Origin": "https://evil.example"}
        assert getattr(c, method)(url, json=body, headers=wrong_origin).status_code == 403, url
    detail = c.get(f"/api/specialists/{sid}").json()
    assert detail["revision"] == 1 and not detail["archived"]  # nothing was written


def test_pool_ships_empty_with_four_templates(api):
    c, _ = api
    out = c.get("/api/specialists").json()
    assert out["specialists"] == []
    assert [t["title"] for t in out["templates"]] == [
        "Intern",
        "Implementer",
        "Reviewer",
        "Bookkeeper",
    ]
    assert all(t["grade"] in model_grades.GRADES for t in out["templates"])


def test_create_edit_archive_round_trip(api):
    c, hdr = api
    r = c.post("/api/specialists", json=_draft(), headers=hdr)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["grade"] is None and s["versions"][0]["version"] == 1
    # Map the model: the SAME specialist reads as graded, with nothing rewritten.
    g = c.post(
        "/api/prefs", json={"model_grades": {"claude": {"claude-opus-5": "strong"}}}, headers=hdr
    )
    assert g.status_code == 200 and g.json()["model_grades"] == {
        "claude": {"claude-opus-5": "strong"}
    }
    assert c.get("/api/config").json()["model_grades"] == {"claude": {"claude-opus-5": "strong"}}
    assert g.json()["model_grades_rev"] == 1
    assert c.get("/api/config").json()["model_grades_rev"] == 1
    assert c.get(f"/api/specialists/{s['id']}").json()["grade"] == "strong"

    e = c.patch(
        f"/api/specialists/{s['id']}",
        json={"revision": 1, "draft": _draft(capacity=4)},
        headers=hdr,
    )
    assert e.status_code == 200 and e.json()["current"]["version"] == 2
    stale = c.patch(
        f"/api/specialists/{s['id']}", json={"revision": 1, "draft": _draft()}, headers=hdr
    )
    assert stale.status_code == 409
    assert c.get(f"/api/specialists/{s['id']}/versions/1").json()["capacity"] == 2

    a = c.post(f"/api/specialists/{s['id']}/archive", json={"revision": 2}, headers=hdr)
    assert a.status_code == 200 and a.json()["archived"]
    assert c.get(f"/api/specialists/{s['id']}/versions/2").status_code == 200


def test_model_grades_are_saved_on_their_own(api):
    """A grade write can be refused late (caps on the merged map), so it never shares a request
    with another preference that would already have landed."""
    c, hdr = api
    before = c.get("/api/config").json()["theme"]
    other = "light" if before != "light" else "dark"
    r = c.post(
        "/api/prefs",
        json={"theme": other, "model_grades": {"claude": {"claude-opus-5": "fast"}}},
        headers=hdr,
    )
    assert r.status_code == 422 and "separately" in r.json()["detail"]
    cfg = c.get("/api/config").json()
    assert cfg["theme"] == before and cfg["model_grades"] == {}


def test_a_late_cap_refusal_leaves_the_map_as_it_was(api, monkeypatch):
    c, hdr = api
    monkeypatch.setattr(model_grades, "MAX_MODELS_PER_ENGINE", 2)
    ok = {"claude": {"claude-opus-5": "fast", "claude-sonnet-5": "strong"}}
    assert c.post("/api/prefs", json={"model_grades": ok}, headers=hdr).status_code == 200
    r = c.post("/api/prefs", json={"model_grades": {"claude": {"m-3": "fast"}}}, headers=hdr)
    assert r.status_code == 422 and "too many models" in r.json()["detail"]
    assert c.get("/api/config").json()["model_grades"] == ok


def test_refusals_write_nothing(api):
    c, hdr = api
    r = c.post(
        "/api/specialists", json=_draft(skills=[{"id": "tdd", "revision": "old"}]), headers=hdr
    )
    assert r.status_code == 422 and "changed since" in r.json()["detail"]
    assert c.get("/api/specialists").json()["specialists"] == []
    bad = c.post("/api/prefs", json={"model_grades": {"claude": {"default": "fast"}}}, headers=hdr)
    assert bad.status_code == 422
    assert c.get("/api/config").json()["model_grades"] == {}


def test_a_lone_surrogate_is_refused_and_the_pool_stays_readable(api):
    c, hdr = api
    raw = '{"title": "Bad \\ud800", "summary": "", "skills": [], "engine": "claude", '
    raw += '"model": "default", "effort": null, "capacity": 1}'
    r = c.post("/api/specialists", content=raw, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 422 and "invalid characters" in r.json()["detail"]
    assert c.get("/api/specialists").status_code == 200
    assert c.get("/api/specialists").json()["specialists"] == []


def test_the_list_response_survives_a_damaged_row(api):
    """Through the real JSON response, not just the store: a stored NaN must never reach it."""
    c, hdr = api
    bad = c.post("/api/specialists", json=_draft(), headers=hdr).json()["id"]
    good = c.post("/api/specialists", json=_draft(title="Reviewer"), headers=hdr).json()["id"]
    con = sqlite3.connect(store.db_path())
    row = con.execute("SELECT body FROM specialist_versions WHERE id = ?", (bad,)).fetchone()[0]
    con.execute(
        "UPDATE specialist_versions SET body = ? WHERE id = ?",
        (row[:-1] + ', "extra": NaN}', bad),
    )
    con.commit()
    con.close()
    r = c.get("/api/specialists")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["specialists"]] == [good]
    assert c.get(f"/api/specialists/{bad}").status_code == 500  # named damaged, not crashed


@pytest.mark.parametrize(
    "table, column, value",
    [
        ("specialists", "created_at", "NaN"),
        ("specialists", "updated_at", "yesterday"),
        ("specialists", "revision", "abc"),
        ("specialists", "archived", "maybe"),
        ("specialist_versions", "created_at", "NaN"),
    ],
)
def test_the_list_response_survives_damaged_metadata(api, table, column, value):
    """A hand edit or a bad restore can put anything in a column; the pool response survives."""
    c, hdr = api
    bad = c.post("/api/specialists", json=_draft(), headers=hdr).json()["id"]
    good = c.post("/api/specialists", json=_draft(title="Reviewer"), headers=hdr).json()["id"]
    con = sqlite3.connect(store.db_path())
    con.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, bad))
    con.commit()
    con.close()
    r = c.get("/api/specialists")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["specialists"]] == [good]


def test_a_grade_write_runs_off_the_event_loop(api, monkeypatch):
    """The merge waits on the prefs file lock: on the loop, a contended lock would stall every
    request and terminal on this worker."""
    import asyncio

    c, hdr = api
    seen = []
    real = model_grades.apply_patch

    def spy(patch):
        seen.append(asyncio._get_running_loop())  # None in a worker thread
        return real(patch)

    monkeypatch.setattr(model_grades, "apply_patch", spy)
    r = c.post(
        "/api/prefs", json={"model_grades": {"claude": {"claude-opus-5": "fast"}}}, headers=hdr
    )
    assert r.status_code == 200
    assert seen == [None]


def test_unknown_ids_and_bodies(api):
    c, hdr = api
    assert c.get("/api/specialists/nope").status_code == 404
    assert c.get("/api/specialists/BAD..id").status_code == 404
    assert c.get("/api/specialists/nope/versions/x").status_code == 404
    s = c.post("/api/specialists", json=_draft(), headers=hdr).json()
    assert (
        c.patch(f"/api/specialists/{s['id']}", json={"draft": _draft()}, headers=hdr).status_code
        == 422
    )
    assert (
        c.patch(
            f"/api/specialists/{s['id']}",
            json={"revision": 1, "draft": _draft(), "x": 1},
            headers=hdr,
        ).status_code
        == 422
    )
    big = {"revision": 1, "draft": _draft(summary="x" * 40_000)}
    assert c.patch(f"/api/specialists/{s['id']}", json=big, headers=hdr).status_code == 413
