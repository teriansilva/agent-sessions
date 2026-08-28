"""Unit tests for the app-preferences store + the theme config/write endpoints (#109)."""

from __future__ import annotations

import json
from pathlib import Path

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


# ---- sidebar_view retired (#139 → #357 Phase 2) --------------------------------
# The pref was written via the API but had lost its Settings surface; it is now removed
# end-to-end. A stale on-disk key must stay benign, and the API must not resurrect it.


def test_stale_sidebar_view_key_on_disk_is_tolerated(tmp_path):
    """An old prefs.json still carrying `sidebar_view` loads fine — the key is simply
    ignored on read (never scrubbed: an unknown key is benign by design)."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"sidebar_view": "overview", "theme": "light"}))
    assert prefs.get_theme(p) == "light"
    # A write through any setter preserves the stale key without choking on it.
    prefs.set_theme("dark", p)
    assert _json.loads(p.read_text())["sidebar_view"] == "overview"


def test_config_no_longer_exposes_sidebar_view(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert "sidebar_view" not in c.get("/api/config").json()


def test_post_sidebar_view_is_unknown_422(auth_cfg, tmp_home):
    # The retired key no longer counts as a known preference key.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"sidebar_view": "overview"},
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


# ---- overview lists: expanded (#144) + hidden (#174) ---------------------------


def test_overview_lists_default_empty(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_overview_expanded(p) == []
    assert prefs.get_projects_hidden(p) == []


def test_overview_lists_round_trip_and_coerce(tmp_path):
    p = tmp_path / "prefs.json"
    # dupes + non-strings are dropped; order preserved.
    assert prefs.set_overview_expanded(["/a", "/a", "/b", 3, None], p) == ["/a", "/b"]
    assert prefs.get_overview_expanded(p) == ["/a", "/b"]
    assert prefs.set_projects_hidden("nope", p) == []  # non-list → []


def test_overview_lists_coexist_with_theme(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_overview_expanded(["/x"], p)
    prefs.set_projects_hidden(["/y"], p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_overview_expanded(p) == ["/x"]
    assert prefs.get_projects_hidden(p) == ["/y"]


def test_config_exposes_overview_lists(auth_cfg, tmp_home):
    prefs.set_projects_hidden(["/home/u/secret"])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert d["projects_hidden"] == ["/home/u/secret"]
    assert d["overview_expanded"] == []
    # The legacy alias is retired (#357 Phase 2) — not emitted any more.
    assert "overview_excluded" not in d


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
        json={"projects_hidden": ["/ok", 5]},
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
    prefs.set_projects_hidden(["/x"], p)
    prefs.set_project_names({"/x": "X"}, p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_projects_hidden(p) == ["/x"]
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


# ---- projects_hidden (#174) + the one-time overview_excluded migration (#357 Phase 2).
# The legacy read-fallback is gone: `migrate_overview_excluded` union-merges an old
# on-disk `overview_excluded` into `projects_hidden` exactly once at startup and drops
# the legacy key; afterwards the reader consults only `projects_hidden`.


def test_projects_hidden_default_empty(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.get_projects_hidden(p) == []


def test_projects_hidden_round_trips(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_projects_hidden(["/y", "/x"], p)
    # `coerce_str_list` preserves insertion order, no sort applied.
    assert prefs.get_projects_hidden(p) == ["/y", "/x"]


def test_projects_hidden_reader_no_longer_falls_back_to_legacy(tmp_path):
    """The transition-window read-fallback is retired: with only the legacy key on disk
    (i.e. the migration has not run against this file), the reader returns []."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"overview_excluded": ["/legacy"]}))
    assert prefs.get_projects_hidden(p) == []


def test_migration_legacy_only_moves_hides_and_drops_old_key(tmp_path):
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"overview_excluded": ["/legacy", "/two"], "theme": "light"}))
    assert prefs.migrate_overview_excluded(p) == ["/legacy", "/two"]
    data = _json.loads(p.read_text())
    assert data["projects_hidden"] == ["/legacy", "/two"]
    assert "overview_excluded" not in data
    assert data["theme"] == "light"  # other keys preserved
    assert prefs.get_projects_hidden(p) == ["/legacy", "/two"]


def test_migration_union_merges_when_both_keys_present(tmp_path):
    """Union semantics (#357): no hidden project lost. Existing `projects_hidden` entries
    keep their order/precedence; legacy hides not already present are appended; entries
    in both lists appear exactly once."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(
        _json.dumps(
            {
                "projects_hidden": ["/new", "/both"],
                "overview_excluded": ["/both", "/legacy-only"],
            }
        )
    )
    assert prefs.migrate_overview_excluded(p) == ["/new", "/both", "/legacy-only"]
    data = _json.loads(p.read_text())
    assert data["projects_hidden"] == ["/new", "/both", "/legacy-only"]
    assert "overview_excluded" not in data


def test_migration_runs_exactly_once_and_rerun_is_a_no_op(tmp_path):
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"overview_excluded": ["/legacy"]}))
    assert prefs.migrate_overview_excluded(p) == ["/legacy"]
    after_first = p.read_bytes()
    # Re-run: the legacy key is gone → no rewrite at all (returns None, bytes identical).
    assert prefs.migrate_overview_excluded(p) is None
    assert p.read_bytes() == after_first
    assert prefs.get_projects_hidden(p) == ["/legacy"]


def test_migration_no_op_without_legacy_key_or_file(tmp_path):
    import json as _json

    # Missing file: nothing to do, nothing created.
    missing = tmp_path / "prefs.json"
    assert prefs.migrate_overview_excluded(missing) is None
    assert not missing.exists()
    # File without the legacy key: untouched byte-for-byte.
    p = tmp_path / "clean.json"
    p.write_text(_json.dumps({"projects_hidden": ["/h"], "theme": "dark"}))
    before = p.read_bytes()
    assert prefs.migrate_overview_excluded(p) is None
    assert p.read_bytes() == before


def test_migration_tolerates_corrupt_or_non_dict_file(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text("{ this is not json")
    assert prefs.migrate_overview_excluded(p) is None
    assert p.read_text() == "{ this is not json"  # left alone for the next writer to recover
    p.write_text('["a", "list"]')
    assert prefs.migrate_overview_excluded(p) is None


def test_create_app_migrates_legacy_prefs_file(auth_cfg, tmp_home):
    """End-to-end against an old prefs.json fixture: create_app runs the migration, so
    /api/config serves the legacy hides under `projects_hidden` (only), and the file on
    disk is normalized with the legacy key dropped."""
    import json as _json
    from pathlib import Path as _Path

    p = _Path.home() / ".config" / "agent-sessions" / "prefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({"overview_excluded": ["/legacy"], "theme": "light"}))
    c = _client(auth_cfg)  # create_app → migration
    _login(c, auth_cfg)
    d = c.get("/api/config").json()
    assert d["projects_hidden"] == ["/legacy"]
    assert "overview_excluded" not in d
    on_disk = _json.loads(p.read_text())
    assert on_disk["projects_hidden"] == ["/legacy"]
    assert "overview_excluded" not in on_disk


def test_projects_hidden_persists_alongside_other_keys(tmp_path):
    """Writing projects_hidden does not clobber theme / project_names."""
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


def test_default_project_roundtrip(tmp_home):
    from agent_sessions import prefs

    assert prefs.get_default_project() == ""  # unset
    assert prefs.set_default_project("/p/a") == "/p/a"
    assert prefs.get_default_project() == "/p/a"
    assert prefs.set_default_project("") == ""  # cleared
    assert prefs.get_default_project() == ""


# ---- project_roots + folder_exclusions (#465) ---------------------------------


def test_project_roots_default_empty(tmp_path):
    assert prefs.get_project_roots(tmp_path / "prefs.json") == []


def test_project_roots_round_trip_and_coerce(tmp_path):
    p = tmp_path / "prefs.json"
    # Stored RAW (no realpath/existing-dir filter here — that's project_dirs' job): a list of
    # unique strings, dupes + non-strings dropped by coerce_str_list.
    out = prefs.set_project_roots(["/home/u/code", "/home/u/code", "/work", 5], p)
    assert out == ["/home/u/code", "/work"]
    assert prefs.get_project_roots(p) == ["/home/u/code", "/work"]
    assert prefs.set_project_roots("nope", p) == []  # non-list → []


def test_folder_exclusions_round_trip_and_coerce(tmp_path):
    p = tmp_path / "prefs.json"
    out = prefs.set_folder_exclusions(["/tmp", "/tmp", "/x/scratch", None], p)
    assert out == ["/tmp", "/x/scratch"]
    assert prefs.get_folder_exclusions(p) == ["/tmp", "/x/scratch"]
    assert prefs.set_folder_exclusions(42, p) == []  # non-list → []


def test_discovery_prefs_coexist_with_other_keys(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("dark", p)
    prefs.set_project_roots(["/r"], p)
    prefs.set_folder_exclusions(["/e"], p)
    assert prefs.get_theme(p) == "dark"
    assert prefs.get_project_roots(p) == ["/r"]
    assert prefs.get_folder_exclusions(p) == ["/e"]


def test_config_exposes_folder_exclusions(auth_cfg, tmp_home):
    prefs.set_folder_exclusions(["/home/u/scratch"])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["folder_exclusions"] == ["/home/u/scratch"]


def test_set_folder_exclusions_endpoint(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"folder_exclusions": ["/a", "/b"]}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"folder_exclusions": ["/a", "/b"]}
    assert c.get("/api/config").json()["folder_exclusions"] == ["/a", "/b"]


def test_set_project_roots_endpoint_echoes_effective_list(auth_cfg, tmp_home):
    # The endpoint echoes the EFFECTIVE (normalized, existing-dir-only) list, not the raw input —
    # so the client sees what actually took effect. A real dir survives; a missing one drops.
    real = tmp_home / "code"
    real.mkdir()
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/prefs",
        json={"project_roots": [str(real), str(tmp_home / "missing")]},
        headers=hdr,
    )
    assert r.status_code == 200
    import os

    assert r.json() == {"project_roots": [os.path.realpath(real)]}
    # And /api/config now reports the same effective list.
    assert c.get("/api/config").json()["project_roots"] == [os.path.realpath(real)]
    # The RAW stored pref keeps both (the missing dir stays editable in the UI).
    assert prefs.get_project_roots() == [str(real), str(tmp_home / "missing")]


def test_set_discovery_prefs_reject_non_string_items(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"project_roots": ["/ok", 5]}, headers=hdr).status_code == 422
    assert c.post("/api/prefs", json={"folder_exclusions": [1]}, headers=hdr).status_code == 422


# ---- default_project_id (#615 Phase 2) ------------------------------------------------
#
# `default_project` named a bare cwd and was shadowed the moment any project carried a
# `default_folder` (required since #448): the picker resolves
# `selectedProject.default_folder ?? config.default_project`. `default_project_id` names the
# PROJECT instead. The migration seeds it from the legacy cwd — but ONLY when a project has
# adopted that folder, because an unadopted cwd has no id to migrate to and must keep working
# through the surviving fallback.


def test_default_project_id_defaults_to_empty(tmp_path):
    assert prefs.get_default_project_id(tmp_path / "prefs.json") == ""


def test_set_and_get_default_project_id_roundtrip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_default_project_id("p-1", p) == "p-1"
    assert prefs.get_default_project_id(p) == "p-1"
    assert prefs.set_default_project_id("", p) == ""  # "" clears
    assert prefs.get_default_project_id(p) == ""


def test_migrate_adopted_cwd_seeds_the_owning_projects_id(tmp_path):
    """Arm 1: the legacy cwd IS adopted → write the owner's id, keep the cwd."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"default_project": "/home/u/alpha", "theme": "light"}))
    assert prefs.migrate_default_project_id(lambda cwd: "p-1", p) == "p-1"
    data = _json.loads(p.read_text())
    assert data["default_project_id"] == "p-1"
    # The cwd is NOT dropped — it still seeds Onboarding and the entity-less fallback.
    assert data["default_project"] == "/home/u/alpha"
    assert data["theme"] == "light"  # other keys preserved


def test_migrate_unadopted_cwd_writes_nothing_and_keeps_the_fallback(tmp_path):
    """Arm 2: no project owns the cwd → there is no id to migrate to. Write NOTHING, so the
    picker's `?? config.default_project` fallback keeps the operator's start directory."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"default_project": "/home/u/loose"}))
    assert prefs.migrate_default_project_id(lambda cwd: "", p) is None
    data = _json.loads(p.read_text())
    assert "default_project_id" not in data
    assert data["default_project"] == "/home/u/loose"


def test_migrate_is_a_no_op_when_already_set_and_never_clobbers(tmp_path):
    """An explicit choice (or a prior migration) wins: never rewrite `default_project_id`."""
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"default_project": "/home/u/alpha", "default_project_id": "chosen"}))

    def _boom(cwd):  # the resolver must not even be consulted
        raise AssertionError("resolver called despite default_project_id already set")

    assert prefs.migrate_default_project_id(_boom, p) is None
    assert _json.loads(p.read_text())["default_project_id"] == "chosen"


def test_migrate_no_op_without_a_legacy_cwd_and_rerun_is_idempotent(tmp_path):
    import json as _json

    p = tmp_path / "prefs.json"
    p.write_text(_json.dumps({"theme": "light"}))
    assert prefs.migrate_default_project_id(lambda cwd: "p-1", p) is None
    assert "default_project_id" not in _json.loads(p.read_text())
    # Fresh install / missing file → tolerated, like every other read.
    assert prefs.migrate_default_project_id(lambda cwd: "p-1", tmp_path / "gone.json") is None
    # Second run after a real migration writes nothing new.
    p.write_text(_json.dumps({"default_project": "/a"}))
    assert prefs.migrate_default_project_id(lambda cwd: "p-9", p) == "p-9"
    assert prefs.migrate_default_project_id(lambda cwd: "p-OTHER", p) is None
    assert _json.loads(p.read_text())["default_project_id"] == "p-9"


def test_config_echoes_default_project_id_and_prefs_accepts_it(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.get("/api/config").json()["default_project_id"] == ""
    assert c.post("/api/prefs", json={"default_project_id": "p-1"}, headers=hdr).json() == {
        "default_project_id": "p-1"
    }
    assert c.get("/api/config").json()["default_project_id"] == "p-1"
    # Stored verbatim — a since-deleted id is the picker's problem, not a 422.
    assert c.post("/api/prefs", json={"default_project_id": "gone"}, headers=hdr).status_code == 200
    assert c.post("/api/prefs", json={"default_project_id": 5}, headers=hdr).status_code == 422


# ---- terminal text size (#859) -------------------------------------------------------
#
# The read/write tables are driven by tests/fixtures/term_font_size_cases.json, which the
# Vitest suite reads too (web/src/theme/termSize.test.ts). One list, both languages: the
# edge cases here are precisely the ones where two hand-written suites drift — half-values
# (Python round() is banker's, JS Math.round is half-up) and booleans (isinstance(True, int)
# is True). A case added to the JSON is automatically enforced on both sides.

_CASES = json.loads((Path(__file__).parent / "fixtures" / "term_font_size_cases.json").read_text())


def test_term_font_size_fixture_matches_the_constants():
    # If someone widens the range in prefs.py but not in the fixture (or termSize.ts), the
    # shared table stops describing the code — catch that here rather than in a confusing
    # per-case failure.
    assert _CASES["min"] == prefs.TERM_FONT_SIZE_MIN
    assert _CASES["max"] == prefs.TERM_FONT_SIZE_MAX
    assert _CASES["default"] == prefs.DEFAULT_TERM_FONT_SIZE


def test_coerce_term_font_size_shared_read_table():
    for case in _CASES["read"]:
        got = prefs.coerce_term_font_size(case["in"])
        assert got == case["out"], f"{case['in']!r} → {got}, expected {case['out']}: {case['why']}"


def test_coerce_term_font_size_python_only_edges():
    # Values JSON cannot carry, so they can't live in the shared fixture.
    assert prefs.coerce_term_font_size(float("nan")) == 13
    assert prefs.coerce_term_font_size(float("inf")) == 13
    assert prefs.coerce_term_font_size(float("-inf")) == 13
    # An integral FLOAT still reads fine (12.0 → 12); it is only the strict WRITE gate that
    # rejects it. Read is lenient, write is strict — the same split as coerce/is_valid_accent.
    assert prefs.coerce_term_font_size(12.0) == 12


def test_is_valid_term_font_size_shared_write_table():
    for v in _CASES["write_accepted"]:
        assert prefs.is_valid_term_font_size(v) is True, v
    for v in _CASES["write_rejected"]:
        assert prefs.is_valid_term_font_size(v) is False, v


def test_is_valid_term_font_size_rejects_integral_float():
    # 12.0 cannot live in the shared fixture: Python json gives float 12.0 but JS JSON.parse
    # gives the integer 12, so the two languages would disagree about the case itself.
    assert prefs.is_valid_term_font_size(12.0) is False


def test_default_term_font_size_when_unset(tmp_path):
    assert prefs.get_term_font_size(tmp_path / "p.json") == 13


def test_term_font_size_round_trip(tmp_path):
    p = tmp_path / "p.json"
    assert prefs.set_term_font_size(10, p) == 10
    assert prefs.get_term_font_size(p) == 10


def test_term_font_size_read_clamps_a_hostile_stored_value(tmp_path):
    # The failure this guards: a hand-edited prefs.json (or an older build's value) must not
    # be able to strand the terminal at an unreadable size.
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"term_font_size": 2}))
    assert prefs.get_term_font_size(p) == 8
    p.write_text(json.dumps({"term_font_size": "huge"}))
    assert prefs.get_term_font_size(p) == 13


def test_config_exposes_term_font_size(auth_cfg, tmp_home):
    prefs.set_term_font_size(10)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["term_font_size"] == 10


def test_config_default_term_font_size(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["term_font_size"] == 13


def test_set_term_font_size_endpoint_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"term_font_size": 9}, headers=hdrs)
    assert r.status_code == 200 and r.json() == {"term_font_size": 9}
    assert c.get("/api/config").json()["term_font_size"] == 9


def test_set_term_font_size_rejected_shapes_are_422_and_persist_nothing(auth_cfg, tmp_home):
    # Strict on write: the endpoint 422s rather than clamping, so a client bug surfaces
    # instead of being silently rounded away — and nothing is written on the way out.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"term_font_size": 11}, headers=hdrs).status_code == 200
    for bad in _CASES["write_rejected"] + [12.0]:
        r = c.post("/api/prefs", json={"term_font_size": bad}, headers=hdrs)
        assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"
        assert c.get("/api/config").json()["term_font_size"] == 11, bad


def test_set_term_font_size_without_clobbering_theme_or_accent(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"theme": "light"}, headers=hdrs).status_code == 200
    assert c.post("/api/prefs", json={"accent": "#00aaff"}, headers=hdrs).status_code == 200
    assert c.post("/api/prefs", json={"term_font_size": 16}, headers=hdrs).status_code == 200
    cfg = c.get("/api/config").json()
    assert cfg["term_font_size"] == 16 and cfg["theme"] == "light" and cfg["accent"] == "#00aaff"


# ---- terminal font FAMILY (#866) -----------------------------------------------------
#
# Same shared-fixture device as the size above (tests/fixtures/term_font_family_cases.json,
# read by web/src/theme/termFont.test.ts too). The rows that earn the fixture are NOT the
# obviously-forbidden characters — those are easy and both languages get them right. They are
# the stacks built entirely from ALLOWED characters that are nevertheless dead: an unbalanced
# quote, an empty comma segment, whitespace only. A charset check passes every one of those,
# and the terminal then renders in nothing.

_FAM = json.loads((Path(__file__).parent / "fixtures" / "term_font_family_cases.json").read_text())


def test_term_font_family_fixture_matches_the_constants():
    assert _FAM["default"] == prefs.DEFAULT_TERM_FONT_FAMILY
    assert _FAM["max_len"] == prefs.TERM_FONT_FAMILY_MAX_LEN


def test_coerce_term_font_family_shared_read_table():
    for case in _FAM["read"]:
        got = prefs.coerce_term_font_family(case["in"])
        why = case["why"]
        assert got == case["out"], f"{case['in']!r} → {got!r}, expected {case['out']!r}: {why}"


def test_is_valid_term_font_family_shared_write_table():
    for v in _FAM["write_accepted"]:
        assert prefs.is_valid_term_font_family(v) is True, v
    for v in _FAM["write_rejected"]:
        assert prefs.is_valid_term_font_family(v) is False, v


def test_term_font_family_length_cap_is_enforced_at_the_boundary():
    # Exactly at the cap is fine; one character over is not — asserted on the boundary itself
    # rather than on a comfortably-long string, which would pass against an off-by-one.
    at_cap = "Menlo, " + "a" * (prefs.TERM_FONT_FAMILY_MAX_LEN - len("Menlo, "))
    assert len(at_cap) == prefs.TERM_FONT_FAMILY_MAX_LEN
    assert prefs.is_valid_term_font_family(at_cap) is True
    assert prefs.is_valid_term_font_family(at_cap + "a") is False
    assert prefs.coerce_term_font_family(at_cap + "a") == prefs.DEFAULT_TERM_FONT_FAMILY


def test_term_font_family_read_never_preserves_an_unusable_stack(tmp_path):
    # The failure this guards: a hand-edited prefs.json must not be able to leave the terminal
    # rendering in nothing. Every one of these is legal-charset and structurally dead.
    p = tmp_path / "p.json"
    for dead in ('"Fira Code', "Menlo,,monospace", "   ", ",monospace"):
        p.write_text(json.dumps({"term_font_family": dead}))
        assert prefs.get_term_font_family(p) == prefs.DEFAULT_TERM_FONT_FAMILY, dead


def test_term_font_family_round_trip_and_default(tmp_path):
    p = tmp_path / "p.json"
    assert prefs.get_term_font_family(p) == prefs.DEFAULT_TERM_FONT_FAMILY
    assert prefs.set_term_font_family('"Fira Code", monospace', p) == '"Fira Code", monospace'
    assert prefs.get_term_font_family(p) == '"Fira Code", monospace'


def test_term_font_family_interior_spacing_is_preserved_verbatim(tmp_path):
    # Not cosmetic: the client decides which preset card reads as ACTIVE by comparing the
    # stored string to its preset stacks. Any rewriting here would show a preset as "Custom"
    # on the next device that seeds from the server.
    p = tmp_path / "p.json"
    odd = "Menlo ,  monospace"
    assert prefs.set_term_font_family(f"  {odd}  ", p) == odd
    assert prefs.get_term_font_family(p) == odd


def test_config_exposes_term_font_family(auth_cfg, tmp_home):
    prefs.set_term_font_family('"Fira Code", monospace')
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["term_font_family"] == '"Fira Code", monospace'


def test_config_default_term_font_family(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["term_font_family"] == prefs.DEFAULT_TERM_FONT_FAMILY


def test_set_term_font_family_endpoint_persists(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"term_font_family": "Menlo, monospace"}, headers=hdrs)
    assert r.status_code == 200 and r.json() == {"term_font_family": "Menlo, monospace"}
    assert c.get("/api/config").json()["term_font_family"] == "Menlo, monospace"


def test_set_term_font_family_rejected_shapes_are_422_and_persist_nothing(auth_cfg, tmp_home):
    # Strict on write: every rejected row must 422 AND leave the stored value untouched — the
    # security-relevant half of this axis, since the value lands in a CSS declaration.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    good = {"term_font_family": "Menlo, monospace"}
    assert c.post("/api/prefs", json=good, headers=hdrs).status_code == 200
    for bad in _FAM["write_rejected"]:
        r = c.post("/api/prefs", json={"term_font_family": bad}, headers=hdrs)
        assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"
        assert c.get("/api/config").json()["term_font_family"] == "Menlo, monospace", bad


def test_term_font_family_rejection_is_atomic_with_the_rest_of_the_payload(auth_cfg, tmp_home):
    # The preflight contract (#859): a 422 means NOTHING was persisted, not that the keys
    # before the bad one already landed.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/prefs",
        json={"theme": "light", "term_font_family": "url(evil.woff2)"},
        headers=hdrs,
    )
    assert r.status_code == 422
    cfg = c.get("/api/config").json()
    assert cfg["theme"] == "dark", "the good key must not have landed"
    assert cfg["term_font_family"] == prefs.DEFAULT_TERM_FONT_FAMILY


def test_font_family_and_size_are_independent_axes(auth_cfg, tmp_home):
    # The whole point of #866: the face and the size are two axes, and neither a theme flip
    # nor a size change may disturb the other.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post("/api/prefs", json={"term_font_family": "Menlo, monospace"}, headers=hdrs)
    c.post("/api/prefs", json={"term_font_size": 9}, headers=hdrs)
    c.post("/api/prefs", json={"theme": "light"}, headers=hdrs)
    cfg = c.get("/api/config").json()
    assert cfg["term_font_family"] == "Menlo, monospace"
    assert cfg["term_font_size"] == 9
    assert cfg["theme"] == "light"


# ---- #859 review findings: hostile ints, atomic rejection ----------------------------


def test_coerce_term_font_size_survives_a_huge_int(tmp_path):
    """A bignum must clamp, not raise.

    `math.isfinite()` and `float()` both raise OverflowError on an int too large to convert,
    so a hand-edited `prefs.json` carrying 10**1000 used to take down `GET /api/config` with a
    500 — i.e. the SPA could not boot. "Reads never throw" is the contract; this is the case
    that broke it.
    """
    assert prefs.coerce_term_font_size(10**1000) == 20
    assert prefs.coerce_term_font_size(-(10**1000)) == 8
    assert prefs.coerce_term_font_size(10**20) == 20


def test_config_survives_a_huge_stored_term_font_size(auth_cfg, tmp_home, monkeypatch):
    # Write through the isolated prefs path the conftest sets (AGENT_SESSIONS_PREFS), not a
    # guessed location — a file the app never reads would make this test pass vacuously.
    import os

    path = Path(os.environ["AGENT_SESSIONS_PREFS"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"term_font_size": 10**1000}))
    assert prefs.get_term_font_size() == 20, "the store must read the file this test wrote"
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/config")
    assert r.status_code == 200, "a corrupt pref must never stop the SPA booting"
    assert r.json()["term_font_size"] == 20


def test_a_rejected_payload_persists_nothing(auth_cfg, tmp_home):
    """A 422 must mean NOTHING was written — not that the keys before the bad one landed.

    The handler validates and writes key by key, so before the pre-flight pass a request whose
    second key was invalid returned 422 having already persisted the first. Not specific to
    term_font_size: `{"theme", "accent"}` behaved the same way and both long predate #859.
    """
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    before = c.get("/api/config").json()

    for payload in (
        {"term_font_size": 9, "compose_default": "INVALID"},  # the reported repro
        {"theme": "light", "accent": "NOT-A-COLOUR"},  # the same shape, pre-#859 keys
        {"accent": "#00aaff", "session_list_order": "nonsense"},
        {"term_font_size": 9, "onboarded": "yes"},
        {"compose_default": "open", "term_font_size": 99},  # bad key LAST
        {"term_font_size": 9, "project_names": {"a": 1}},
        {"theme": "light", "projects_hidden": ["ok", 7]},
    ):
        r = c.post("/api/prefs", json=payload, headers=hdrs)
        assert r.status_code == 422, payload
        after = c.get("/api/config").json()
        for key in ("theme", "accent", "term_font_size", "compose_default", "session_list_order"):
            assert after[key] == before[key], f"{payload} leaked {key}"


def test_a_fully_valid_multi_key_payload_still_writes_every_key(auth_cfg, tmp_home):
    # The pre-flight must not become a second gate that quietly drops valid work.
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdrs = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/prefs",
        json={"theme": "light", "accent": "#00aaff", "term_font_size": 16, "onboarded": True},
        headers=hdrs,
    )
    assert r.status_code == 200
    cfg = c.get("/api/config").json()
    assert cfg["theme"] == "light"
    assert cfg["accent"] == "#00aaff"
    assert cfg["term_font_size"] == 16
