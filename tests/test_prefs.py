"""Unit tests for the app-preferences store + the theme config/write endpoints (#109)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions import prefs
from agent_sessions.main import create_app


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
    # CSRF token comes from the SPA bootstrap endpoint (/api/config).
    return c.get("/api/config").json()["csrf"]


# ---- store --------------------------------------------------------------------


def test_default_theme_when_unset(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_theme(p) == "dark"


def test_set_and_get_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_theme("light", p) == "light"
    assert prefs.get_theme(p) == "light"


def test_invalid_theme_coerced_to_default(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_theme("neon", p) == "dark"
    assert prefs.get_theme(p) == "dark"


def test_legacy_royal_migrates_to_dark(tmp_path):
    # `royal` is retired (#211); a persisted legacy value coerces to the dark default.
    p = tmp_path / "prefs.json"
    assert prefs.set_theme("royal", p) == "dark"
    assert prefs.get_theme(p) == "dark"


def test_corrupt_file_tolerated(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text("{ this is not json")
    assert prefs.get_theme(p) == "dark"
    # a write recovers the file
    assert prefs.set_theme("light", p) == "light"
    assert prefs.get_theme(p) == "light"


def test_set_preserves_other_keys(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text('{"keepme": 7}')
    prefs.set_theme("dark", p)
    import json

    data = json.loads(p.read_text())
    assert data == {"keepme": 7, "theme": "dark"}


def test_compose_default_unset_is_auto(tmp_path):
    assert prefs.get_compose_default(tmp_path / "prefs.json") == "auto"


def test_compose_default_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_compose_default("open", p) == "open"
    assert prefs.get_compose_default(p) == "open"
    assert prefs.set_compose_default("collapsed", p) == "collapsed"
    assert prefs.get_compose_default(p) == "collapsed"


def test_compose_default_invalid_coerced_to_auto(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_compose_default("sideways", p) == "auto"
    assert prefs.get_compose_default(p) == "auto"


def test_compose_default_preserves_theme(tmp_path):
    import json

    p = tmp_path / "prefs.json"
    prefs.set_theme("light", p)
    prefs.set_compose_default("open", p)
    data = json.loads(p.read_text())
    assert data["theme"] == "light"
    assert data["compose_default"] == "open"


# ---- endpoints ----------------------------------------------------------------


def test_config_exposes_theme(auth_cfg, tmp_home):
    prefs.set_theme("light")  # writes under tmp_home/.config/... (HOME monkeypatched)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["theme"] == "light"


def test_set_theme_endpoint_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"theme": "dark"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json() == {"theme": "dark"}
    assert c.get("/api/config").json()["theme"] == "dark"


def test_set_theme_unknown_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"theme": "neon"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_set_theme_requires_csrf(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"theme": "dark"}, headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


def test_set_theme_non_object_json_422(auth_cfg, tmp_home):
    # A valid-CSRF request with a JSON array/string/number must be a controlled 422,
    # not a 500 from .get() on a non-dict (Hermes PR #111 review).
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for body in ([], "dark", 7):
        r = c.post(
            "/api/prefs",
            json=body,
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        )
        assert r.status_code == 422, body


# ---- sidebar_view (#139) ------------------------------------------------------


def test_default_sidebar_view_when_unset(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_sidebar_view(p) == "list"


def test_sidebar_view_round_trip_and_invalid(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_sidebar_view("overview", p) == "overview"
    assert prefs.get_sidebar_view(p) == "overview"
    assert prefs.set_sidebar_view("bogus", p) == "list"  # invalid → default


def test_sidebar_view_and_theme_coexist(tmp_path):
    # Setting one pref must not clobber the other (read-modify-write).
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_sidebar_view("overview", p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_sidebar_view(p) == "overview"


def test_config_exposes_sidebar_view(auth_cfg, tmp_home):
    prefs.set_sidebar_view("overview")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["sidebar_view"] == "overview"


def test_set_sidebar_view_endpoint_persists_without_clobbering_theme(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"theme": "dark"}, headers=hdrs).status_code == 200
    r = c.post("/api/prefs", json={"sidebar_view": "overview"}, headers=hdrs)
    assert r.status_code == 200 and r.json() == {"sidebar_view": "overview"}
    cfg = c.get("/api/config").json()
    assert cfg["sidebar_view"] == "overview" and cfg["theme"] == "dark"


def test_set_sidebar_view_unknown_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"sidebar_view": "spreadsheet"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_prefs_no_known_key_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"nope": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- overview lists: expanded / excluded (#144) -------------------------------


def test_overview_lists_default_empty(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_overview_expanded(p) == []
    assert prefs.get_overview_excluded(p) == []


def test_overview_lists_round_trip_and_coerce(tmp_path):
    p = tmp_path / "prefs.json"
    # dupes + non-strings are dropped; order preserved.
    assert prefs.set_overview_expanded(["/a", "/a", "/b", 3, None], p) == ["/a", "/b"]
    assert prefs.get_overview_expanded(p) == ["/a", "/b"]
    assert prefs.set_overview_excluded("nope", p) == []  # non-list → []


def test_overview_lists_coexist_with_theme(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_overview_expanded(["/x"], p)
    prefs.set_overview_excluded(["/y"], p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_overview_expanded(p) == ["/x"]
    assert prefs.get_overview_excluded(p) == ["/y"]


def test_config_exposes_overview_lists(auth_cfg, tmp_home):
    prefs.set_overview_excluded(["/home/u/secret"])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert d["overview_excluded"] == ["/home/u/secret"]
    assert d["overview_expanded"] == []


def test_set_overview_lists_endpoint(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"overview_expanded": ["/a", "/b"]}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"overview_expanded": ["/a", "/b"]}
    assert c.get("/api/config").json()["overview_expanded"] == ["/a", "/b"]


def test_set_overview_list_rejects_non_string_items(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"overview_excluded": ["/ok", 5]},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- project_names map (#148) -------------------------------------------------


def test_project_names_default_empty(tmp_path):
    assert prefs.get_project_names(tmp_path / "prefs.json") == {}


def test_project_names_round_trip_and_coerce(tmp_path):
    p = tmp_path / "prefs.json"
    out = prefs.set_project_names(
        {"/a": "  Alpha  ", "/b": "", "/c": 3, 5: "x", "/d": "x" * 200}, p
    )
    # trimmed; empty drops; non-str key/val dropped; value capped at 80.
    assert out == {"/a": "Alpha", "/d": "x" * 80}
    assert prefs.get_project_names(p) == {"/a": "Alpha", "/d": "x" * 80}
    assert prefs.set_project_names("nope", p) == {}  # non-dict → {}


def test_project_names_coexist_with_theme_and_lists(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_overview_excluded(["/x"], p)
    prefs.set_project_names({"/x": "X"}, p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_overview_excluded(p) == ["/x"]
    assert prefs.get_project_names(p) == {"/x": "X"}


def test_config_exposes_project_names(auth_cfg, tmp_home):
    prefs.set_project_names({"/home/u/proj": "My Project"})
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["project_names"] == {"/home/u/proj": "My Project"}


def test_set_project_names_endpoint(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"project_names": {"/a": "Alpha"}}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"project_names": {"/a": "Alpha"}}
    assert c.get("/api/config").json()["project_names"] == {"/a": "Alpha"}


def test_set_project_names_rejects_non_string_values(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"project_names": {"/a": 5}}, headers=hdr).status_code == 422
    assert c.post("/api/prefs", json={"project_names": ["/a"]}, headers=hdr).status_code == 422


# ---- projects_hidden (#174): new key for global project hiding with overview_excluded
# back-compat. Reader prefers projects_hidden when both present; writer goes through
# set_projects_hidden which is also the back-end of the legacy `overview_excluded` POST.


def test_projects_hidden_default_empty(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_projects_hidden(p) == []


def test_projects_hidden_round_trips(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_projects_hidden(["/y", "/x"], p)
    # `coerce_str_list` preserves insertion order, no sort applied.
    assert prefs.get_projects_hidden(p) == ["/y", "/x"]


def test_projects_hidden_falls_back_to_legacy_overview_excluded(tmp_path):
    """A user who set the legacy key in an older release must see those hides under the
    new name without losing them (#174). Reader returns the legacy value when only the
    legacy key exists on disk."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"overview_excluded": ["/legacy"]}))
    assert prefs.get_projects_hidden(p) == ["/legacy"]


def test_projects_hidden_wins_over_legacy_when_both_present(tmp_path):
    """Migration precedence: when both keys exist (e.g. an old client wrote
    `overview_excluded` while a new client also wrote `projects_hidden`), the new key
    wins. Otherwise transitions would silently lose user intent (#174 Hermes review)."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"overview_excluded": ["/legacy"], "projects_hidden": ["/new"]}))
    assert prefs.get_projects_hidden(p) == ["/new"]


def test_projects_hidden_persists_alongside_other_keys(tmp_path):
    """Writing projects_hidden does not clobber theme / project_names / sidebar_view."""
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_project_names({"/a": "A"}, p)
    prefs.set_projects_hidden(["/h"], p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_project_names(p) == {"/a": "A"}
    assert prefs.get_projects_hidden(p) == ["/h"]


# ---- brand accent (#211 Phase 2) ----------------------------------------------


def test_default_accent_when_unset(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_accent(p) == "#ffb000"


def test_accent_round_trip_and_normalization(tmp_path):
    p = tmp_path / "prefs.json"
    # Uppercase + missing '#' normalize to lowercase #rrggbb.
    assert prefs.set_accent("#C02020", p) == "#c02020"
    assert prefs.get_accent(p) == "#c02020"
    assert prefs.set_accent("3FBF6F", p) == "#3fbf6f"
    # #rgb shorthand expands.
    assert prefs.set_accent("#0af", p) == "#00aaff"


def test_invalid_accent_coerced_to_default(tmp_path):
    p = tmp_path / "prefs.json"
    for bad in ("nope", "#12", "#12345", "#1234567", "rgb(0,0,0)", "", "#ggghhh"):
        assert prefs.set_accent(bad, p) == "#ffb000", bad
        assert prefs.get_accent(p) == "#ffb000", bad


def test_is_valid_accent():
    assert prefs.is_valid_accent("#ffb000")
    assert prefs.is_valid_accent("ffb000")
    assert prefs.is_valid_accent("#0af")
    assert not prefs.is_valid_accent("#12")
    assert not prefs.is_valid_accent("#1234567")
    assert not prefs.is_valid_accent("teal")
    assert not prefs.is_valid_accent(123)
    assert not prefs.is_valid_accent(None)


def test_accent_coexists_with_theme(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("light", p)
    prefs.set_accent("#c02020", p)
    assert prefs.get_theme(p) == "light"
    assert prefs.get_accent(p) == "#c02020"


def test_config_exposes_accent(auth_cfg, tmp_home):
    prefs.set_accent("#3fbf6f")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["accent"] == "#3fbf6f"


def test_config_default_accent(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["accent"] == "#ffb000"


def test_set_accent_endpoint_normalizes_and_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"accent": "#C02020"}, headers=hdrs)
    assert r.status_code == 200 and r.json() == {"accent": "#c02020"}
    assert c.get("/api/config").json()["accent"] == "#c02020"


def test_set_accent_invalid_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"accent": "tomato"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_set_accent_endpoint_without_clobbering_theme(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"theme": "light"}, headers=hdrs).status_code == 200
    assert c.post("/api/prefs", json={"accent": "#00aaff"}, headers=hdrs).status_code == 200
    cfg = c.get("/api/config").json()
    assert cfg["accent"] == "#00aaff" and cfg["theme"] == "light"


def test_config_exposes_compose_default(auth_cfg, tmp_home):
    prefs.set_compose_default("open")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["compose_default"] == "open"


def test_set_compose_default_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"compose_default": "collapsed"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json() == {"compose_default": "collapsed"}
    assert c.get("/api/config").json()["compose_default"] == "collapsed"


def test_set_compose_default_unknown_422(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"compose_default": "nope"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_vt_scrollback_toggle_via_prefs(auth_cfg, tmp_home, monkeypatch):
    # Experimental VT toggle (#329): /api/prefs flips it live + /api/config reflects it. Don't
    # actually spawn the Node sidecar in tests, and keep the in-memory override from leaking.
    from agent_sessions import vtsidecar

    async def _noop() -> None:
        pass

    monkeypatch.setattr(vtsidecar, "ensure_started", _noop)
    monkeypatch.setattr(vtsidecar, "_runtime_override", None)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/config").json()["vt_scrollback"] is False  # default off (env unset)

    r = c.post(
        "/api/prefs",
        json={"vt_scrollback": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json() == {"vt_scrollback": True}
    assert c.get("/api/config").json()["vt_scrollback"] is True

    r = c.post(
        "/api/prefs",
        json={"vt_scrollback": False},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.json() == {"vt_scrollback": False}
    assert c.get("/api/config").json()["vt_scrollback"] is False


def test_vt_scrollback_non_bool_422(auth_cfg, tmp_home, monkeypatch):
    from agent_sessions import vtsidecar

    monkeypatch.setattr(vtsidecar, "_runtime_override", None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"vt_scrollback": "yes"},  # not a boolean
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_project_visible_resolver_is_mode_exclusive():
    from agent_sessions import prefs

    hidden = {"/a"}
    included = {"/b"}
    # all mode: only the denylist matters (included is ignored)
    assert prefs.project_visible("/a", mode="all", hidden=hidden, included=included) is False
    assert prefs.project_visible("/b", mode="all", hidden=hidden, included=included) is True
    assert prefs.project_visible("/x", mode="all", hidden=hidden, included=included) is True
    # included mode: only the allowlist matters (hidden is ignored), new/unlisted dirs stay hidden
    assert prefs.project_visible("/b", mode="included", hidden=hidden, included=included) is True
    assert prefs.project_visible("/a", mode="included", hidden=hidden, included=included) is False
    assert prefs.project_visible("/x", mode="included", hidden=hidden, included=included) is False


def test_projects_mode_and_included_roundtrip(tmp_home):
    from agent_sessions import prefs

    assert prefs.get_projects_mode() == "all"  # safe default
    assert prefs.get_projects_included() == []
    assert prefs.set_projects_mode("included") == "included"
    assert prefs.get_projects_mode() == "included"
    assert prefs.set_projects_mode("bogus") == "all"  # invalid coerces to default
    prefs.set_projects_included(["/p/a", "/p/b", "/p/a"])  # dedup
    assert prefs.get_projects_included() == ["/p/a", "/p/b"]


def test_add_project_included_is_idempotent(tmp_home):
    from agent_sessions import prefs

    prefs.set_projects_included(["/p/a"])
    assert prefs.add_project_included("/p/b") == ["/p/a", "/p/b"]
    assert prefs.add_project_included("/p/b") == ["/p/a", "/p/b"]  # already present → no-op
    assert prefs.add_project_included("") == ["/p/a", "/p/b"]  # empty → no-op
