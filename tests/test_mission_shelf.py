"""The mission shelf's store, delivery rules, broker and tool (#1740 P1a).

What an agent can put on a mission's shelf, and what it cannot: every refusal the issue names is
exercised through `mission_shelf.deliver` (the one judge) and, end to end, through the real tool
(`shelf_mcp`) over the real broker socket (`shelf_broker`). The schema step is checked on a fresh
store and an upgraded one, and against a rollback.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import types
import zlib
from pathlib import Path

import pytest

from agent_sessions import (
    agent_tools,
    files,
    mission_shelf,
    missions,
    prefs,
    secrets_broker,
    shelf_broker,
    shelf_mcp,
)

SESSION = "claude:aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "claude:bbbbbbbb-0000-4000-8000-000000000002"
MANIFEST = types.SimpleNamespace(tools_mcp=agent_tools.CLAUDE_FLAG)


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xb0\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    missions.reset_schema_cache_for_test()
    files.reset_capabilities_for_test()
    mission_shelf.reset_cache_for_test()
    # Delivered files are written by the test a moment ago; treat them as quiet unless a test
    # says otherwise, so the fingerprint fast path is the one exercised.
    monkeypatch.setattr(mission_shelf, "SETTLE_S", -1.0)
    yield
    missions.reset_schema_cache_for_test()


@pytest.fixture
def proj(tmp_path) -> Path:
    p = tmp_path / "home" / "proj"
    p.mkdir()
    return p


@pytest.fixture
def mission(proj) -> str:
    m = missions.create_mission("make the launch assets", cwd=str(proj))
    missions.adopt(m["id"], SESSION)
    return m["id"]


def deliver(mid: str, **item):
    return mission_shelf.deliver(mid, item, session_key=SESSION, phys_key=SESSION)


def refused(mid: str, **item) -> str:
    with pytest.raises(mission_shelf.ShelfRefused) as e:
        deliver(mid, **item)
    return e.value.code


# ---- schema v35 ----------------------------------------------------------------------------------


def _tables(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    try:
        return {
            r[0]: " ".join(r[1].split())
            for r in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE name LIKE 'mission_shelf%' "
                "OR name LIKE 'idx_mission_shelf%'"
            )
        }
    finally:
        con.close()


def test_a_fresh_store_and_an_upgraded_one_get_the_same_shelf_schema(tmp_path):
    fresh_db = tmp_path / "missions.db"
    missions.create_mission("fresh", cwd="/tmp")
    fresh = _tables(fresh_db)
    assert set(fresh) == {
        "mission_shelf_items",
        "mission_shelf_state",
        "mission_shelf_caps",
        "idx_mission_shelf_caps_phys",
    }
    # Rewind to what a v34 build left, then let this build upgrade it.
    con = sqlite3.connect(fresh_db)
    for name in ("mission_shelf_items", "mission_shelf_state", "mission_shelf_caps"):
        con.execute(f"DROP TABLE {name}")
    con.execute("PRAGMA user_version=34")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions.create_mission("upgraded", cwd="/tmp")
    assert _tables(fresh_db) == fresh
    con = sqlite3.connect(fresh_db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 35
    con.close()


def test_rollback_to_34_keeps_the_shelf_and_this_build_converges(mission, proj):
    deliver(mission, title="t", why="w", text="kept")
    db = missions._db_path()
    assert missions.rollback_schema(34, path=db) == {"from": 35, "to": 34}
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM mission_shelf_items").fetchone()[0] == 1
    con.close()
    missions.reset_schema_cache_for_test()
    missions._migrate_34_to_35(sqlite3.connect(db))  # idempotent
    assert mission_shelf.manifest(mission)["counts"]["total"] == 1


def test_deleting_the_mission_deletes_its_shelf(mission):
    deliver(mission, title="t", why="w", text="a secretless finding")
    missions.set_state(mission, "draft", "abandoned")
    assert missions.delete_mission(mission)
    con = sqlite3.connect(missions._db_path())
    for table in ("mission_shelf_items", "mission_shelf_state", "mission_shelf_caps"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    con.close()


# ---- delivering: every kind --------------------------------------------------------------------


def test_every_deliverable_kind_lands_with_what_the_viewer_needs(mission, proj):
    (proj / "out").mkdir()
    (proj / "out" / "poster.png").write_bytes(png())
    (proj / "notes.md").write_text("# Notes\n\nshort\n")
    (proj / "report.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\n")
    (proj / "badge.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (proj / "clip.webm").write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 60)
    (proj / "vo.ogg").write_bytes(b"OggS" + b"\0" * 60)

    got = {
        "image": deliver(
            mission, title="Poster", why="for the page", path=str(proj / "out/poster.png")
        ),
        "video": deliver(mission, title="Clip", why="hero", path=str(proj / "clip.webm")),
        "audio": deliver(mission, title="VO", why="take 3", path=str(proj / "vo.ogg")),
        "md": deliver(mission, title="Notes", why="read me", path=str(proj / "notes.md")),
        "pdf": deliver(mission, title="Report", why="pdf", path=str(proj / "report.pdf")),
        "svg": deliver(mission, title="Badge", why="svg", path=str(proj / "badge.svg")),
        "link": deliver(mission, title="PR", why="review", url="https://git.example.org/a/pulls/4"),
        "text": deliver(mission, title="Finding", why="worth a look", text="Line one\nLine two"),
    }
    assert {k: v["kind"] for k, v in got.items()} == {
        "image": "image",
        "video": "video",
        "audio": "audio",
        "md": "document",
        "pdf": "document",
        "svg": "document",  # never served as markup
        "link": "link",
        "text": "text",
    }
    page = mission_shelf.manifest(mission)
    by = {i["title"]: i for i in page["items"]}
    assert by["Poster"]["mime"] == "image/png" and by["Poster"]["viewable"]
    assert by["Poster"]["sha256"] and by["Poster"]["file"] == {"state": "ok"}
    assert by["Notes"]["mime"] == mission_shelf.TEXT_MIME
    assert by["Report"]["mime"] is None and not by["Report"]["viewable"]  # download only
    assert by["Badge"]["mime"] is None and not by["Badge"]["viewable"]
    assert by["PR"]["host"] == "git.example.org" and by["PR"]["url_path"] == "/a/pulls/4"
    assert by["Finding"]["text"] == "Line one\nLine two"
    assert page["counts"]["by_kind"]["document"] == 3
    assert page["counts"]["by_source"] == {"delivered": 8, "found": 0}
    assert page["counts"]["unreviewed"] == 8
    assert page["revision"] == 8  # one per add


def test_titles_and_whys_are_one_bounded_line_without_control_characters(mission):
    deliver(mission, title="a\x1b[31mred\x07\nline", why="tab\there\r\nnext", text="x")
    item = mission_shelf.manifest(mission)["items"][0]
    assert item["title"] == "a[31mred line"
    assert item["why"] == "tab here next"


# ---- delivering: every refusal ---------------------------------------------------------------


def test_a_path_outside_the_missions_folders_is_refused(mission, tmp_path):
    other = tmp_path / "home" / "elsewhere"
    other.mkdir()
    (other / "x.png").write_bytes(png())
    assert refused(mission, title="t", why="w", path=str(other / "x.png")) == "outside_project"
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("key")
    assert refused(mission, title="t", why="w", path=str(ssh / "id_ed25519")) == "outside_project"


def test_a_path_under_an_exclusion_or_outside_the_roots_is_refused(mission, proj, tmp_path):
    (proj / "private").mkdir()
    (proj / "private" / "a.png").write_bytes(png())
    (proj / "b.png").write_bytes(png())
    prefs.set_folder_exclusions([str(proj / "private")])
    assert refused(mission, title="t", why="w", path=str(proj / "private" / "a.png")) == (
        "outside_project"
    )
    assert deliver(mission, title="t", why="w", path=str(proj / "b.png"))["kind"] == "image"
    prefs.set_folder_exclusions([])
    root = tmp_path / "home" / "roots"
    root.mkdir()
    prefs.set_project_roots([str(root)])  # the project is no longer under any root
    assert refused(mission, title="t", why="w", path=str(proj / "b.png")) == "outside_project"


def test_symlinks_and_symlinked_escapes_are_refused(mission, proj, tmp_path):
    secret = tmp_path / "home" / "secret.txt"
    secret.write_text("nope")
    (proj / "link.txt").symlink_to(secret)
    assert refused(mission, title="t", why="w", path=str(proj / "link.txt")) == "symlink"
    (proj / "dir").symlink_to(tmp_path / "home")  # an intermediate link that leaves the project
    assert refused(mission, title="t", why="w", path=str(proj / "dir" / "secret.txt")) == (
        "outside_project"
    )


def test_missing_special_and_relative_paths_are_refused(mission, proj):
    assert refused(mission, title="t", why="w", path=str(proj / "gone.png")) == "not_found"
    (proj / "sub").mkdir()
    assert refused(mission, title="t", why="w", path=str(proj / "sub")) == "not_a_file"
    os.mkfifo(proj / "pipe")
    assert refused(mission, title="t", why="w", path=str(proj / "pipe")) == "not_a_file"
    assert refused(mission, title="t", why="w", path="relative.png") == "bad_request"


def test_oversized_and_malformed_items_are_refused(mission, proj, monkeypatch):
    assert refused(mission, title="", why="w", text="x") == "title_required"
    assert refused(mission, title="t" * 121, why="w", text="x") == "too_long"
    assert refused(mission, title="t", why="w" * 301, text="x") == "too_long"
    assert refused(mission, title="t", why="w", text="é" * (32 * 1024 + 1)) == "too_long"
    assert refused(mission, title="t", why="w") == "bad_request"
    assert refused(mission, title="t", why="w", text="a", url="https://x.org") == "bad_request"
    assert refused(mission, title="t", why="w", text="a", extra=1) == "bad_request"
    assert refused(mission, title="t", why="w", text="a", kind="code") == "bad_request"
    assert refused(mission, title="t", why="w", text="a", kind="link") == "kind_mismatch"
    (proj / "a.png").write_bytes(png())
    assert refused(mission, title="t", why="w", path=str(proj / "a.png"), kind="video") == (
        "kind_mismatch"
    )
    monkeypatch.setattr(mission_shelf, "FILE_MAX", 10)
    assert refused(mission, title="t", why="w", path=str(proj / "a.png")) == "too_large"


def test_media_whose_bytes_do_not_match_its_extension_is_refused(mission, proj):
    (proj / "page.png").write_text("<html><script>alert(1)</script></html>")
    assert refused(mission, title="t", why="w", path=str(proj / "page.png")) == "content_mismatch"
    (proj / "fake.webm").write_bytes(png())
    assert refused(mission, title="t", why="w", path=str(proj / "fake.webm")) == (
        "content_mismatch"
    )


def test_links_with_credentials_or_another_scheme_are_refused(mission):
    assert refused(mission, title="t", why="w", url="https://u:p@host.example/") == (
        "url_credentials"
    )
    assert refused(mission, title="t", why="w", url="https://u@host.example/") == "url_credentials"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://h/x", "https://", "http:/x"):
        assert refused(mission, title="t", why="w", url=bad) == "url_invalid", bad
    assert refused(mission, title="t", why="w", url="https://h.example/a b") == "url_invalid"
    assert refused(mission, title="t", why="w", url="https://h.example/?token=abc123") == (
        "credential"
    )
    # A commit page names a git id, not a credential.
    oid = "3e9c1f2a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e"
    assert deliver(mission, title="c", why="w", url=f"https://h.example/a/b/commit/{oid}")


def test_credential_shaped_text_is_withheld_not_redacted(mission):
    assert refused(mission, title="t", why="w", text="password: hunter2hunter2") == "credential"
    assert refused(mission, title="api_key=sk-12345", why="w", text="x") == "credential"
    assert refused(mission, title="t", why="token: abcdef", text="x") == "credential"
    assert mission_shelf.manifest(mission)["counts"]["total"] == 0


def test_a_session_off_the_roster_shelves_nothing(mission, proj):
    with pytest.raises(mission_shelf.ShelfRefused) as e:
        mission_shelf.deliver(
            mission, {"title": "t", "why": "w", "text": "x"}, session_key=OTHER, phys_key=OTHER
        )
    assert e.value.code == "not_in_mission"
    missions.detach(mission, SESSION)
    assert refused(mission, title="t", why="w", text="x") == "not_in_mission"


def test_a_late_bound_session_is_a_member_by_its_runtime_key(proj):
    m = missions.create_mission("x", cwd=str(proj))
    placeholder = "claude:new-aaaaaaaa-0000-4000-8000-0000000000aa"
    missions.adopt(m["id"], SESSION)
    con = missions._ready()
    missions._bind_runtime_tx(con, SESSION, placeholder, time.time())
    con.close()
    out = mission_shelf.deliver(
        m["id"],
        {"title": "t", "why": "w", "text": "x"},
        session_key=placeholder,
        phys_key=placeholder,
    )
    assert out["kind"] == "text"


def test_the_shelf_is_bounded(mission, monkeypatch):
    monkeypatch.setattr(mission_shelf, "MISSION_ITEMS_MAX", 2)
    deliver(mission, title="1", why="w", text="a")
    deliver(mission, title="2", why="w", text="b")
    assert refused(mission, title="3", why="w", text="c") == "shelf_full"


def test_every_refusal_has_words_for_the_agent():
    assert mission_shelf.REFUSALS is shelf_mcp.MEANING
    assert shelf_broker.REFUSALS == set(shelf_mcp.MEANING)


# ---- the manifest -----------------------------------------------------------------------------


def test_counts_describe_the_whole_shelf_and_filters_narrow_before_the_page(mission, monkeypatch):
    monkeypatch.setattr(mission_shelf, "PAGE_MAX", 2)
    for i in range(3):
        deliver(mission, title=f"text {i}", why="w", text=f"t{i}")
    deliver(mission, title="link", why="w", url="https://h.example/x")
    first = mission_shelf.manifest(mission)
    assert [i["title"] for i in first["items"]] == ["text 0", "text 1"]
    assert first["counts"]["total"] == 4 and first["next_cursor"] == "s2"
    second = mission_shelf.manifest(mission, cursor=first["next_cursor"])
    assert [i["title"] for i in second["items"]] == ["text 2", "link"]
    assert second["next_cursor"] is None
    links = mission_shelf.manifest(mission, kind="link")
    assert [i["title"] for i in links["items"]] == ["link"]
    assert links["counts"]["by_kind"]["text"] == 3  # facets ignore the filter
    with pytest.raises(missions.MissionError):
        mission_shelf.manifest(mission, cursor="../x")
    with pytest.raises(missions.MissionError):
        mission_shelf.manifest(mission, kind="nope")


def test_a_verdict_raises_the_revision_and_a_stale_one_is_refused(mission):
    item = deliver(mission, title="t", why="w", text="x")
    rev = mission_shelf.manifest(mission)["revision"]
    out = mission_shelf.set_verdict(
        mission, item["id"], expected_revision=rev, verdict="needs_work", note="trim the intro"
    )
    assert out["revision"] == rev + 1
    assert out["item"]["verdict"] == "needs_work" and out["item"]["note"] == "trim the intro"
    with pytest.raises(mission_shelf.ShelfConflict) as e:
        mission_shelf.set_verdict(mission, item["id"], expected_revision=rev, verdict="good")
    assert e.value.status == 409 and e.value.revision == rev + 1
    assert mission_shelf.manifest(mission, verdict="needs_work")["items"][0]["id"] == item["id"]
    assert mission_shelf.manifest(mission)["counts"]["unreviewed"] == 0
    cleared = mission_shelf.set_verdict(
        mission, item["id"], expected_revision=rev + 1, verdict=None, note="ignored"
    )
    assert cleared["item"]["verdict"] is None and cleared["item"]["note"] == ""


def test_a_verdict_names_an_item_of_this_mission_only(mission, proj):
    item = deliver(mission, title="t", why="w", text="x")
    other = missions.create_mission("other", cwd=str(proj))["id"]
    with pytest.raises(missions.MissionError) as e:
        mission_shelf.set_verdict(other, item["id"], expected_revision=0, verdict="good")
    assert e.value.status == 404
    with pytest.raises(missions.MissionError):
        mission_shelf.set_verdict(mission, item["id"], expected_revision=1, verdict="great")
    with pytest.raises(missions.MissionError):
        mission_shelf.set_verdict(mission, item["id"], expected_revision="1", verdict="good")


def test_file_states_changed_missing_and_inaccessible_stay_on_the_shelf(mission, proj):
    for name in ("a.png", "b.png", "c.png"):
        (proj / name).write_bytes(png())
    (proj / "inner").mkdir()
    (proj / "inner" / "d.png").write_bytes(png())
    for name in ("a.png", "b.png", "c.png", "inner/d.png"):
        deliver(mission, title=name, why="w", path=str(proj / name))
    (proj / "b.png").write_bytes(png() + b"tail")  # changed since delivered
    (proj / "c.png").unlink()  # missing
    prefs.set_folder_exclusions([str(proj / "inner")])  # inaccessible
    page = mission_shelf.manifest(mission)
    states = {i["title"]: i["file"] for i in page["items"]}
    assert states["a.png"] == {"state": "ok"}
    assert states["b.png"]["state"] == "changed" and states["b.png"]["disk_sha256"]
    assert states["b.png"]["disk_sha256"] != next(
        i["sha256"] for i in page["items"] if i["title"] == "b.png"
    )
    assert states["c.png"] == {"state": "missing"}
    assert states["inner/d.png"] == {"state": "inaccessible"}
    assert page["counts"]["file_checks"] == {"changed": 1, "missing": 1, "inaccessible": 1}
    assert page["counts"]["total"] == 4


def test_a_same_size_rewrite_is_caught_even_inside_the_timestamp_tick(mission, proj, monkeypatch):
    """The fingerprint may stand in for a hash only once the file was quiet at delivery: a file
    written in the same tick is hashed on every read until it settles."""
    monkeypatch.setattr(mission_shelf, "SETTLE_S", 3600.0)
    (proj / "a.png").write_bytes(png())
    deliver(mission, title="a", why="w", path=str(proj / "a.png"))
    st = os.stat(proj / "a.png")
    data = bytearray(png())
    data[-5] ^= 0xFF  # same size, different bytes
    (proj / "a.png").write_bytes(bytes(data))
    os.utime(proj / "a.png", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert mission_shelf.manifest(mission)["items"][0]["file"]["state"] == "changed"


def test_an_unchanged_file_is_not_rehashed(mission, proj, monkeypatch):
    (proj / "a.png").write_bytes(png())
    deliver(mission, title="a", why="w", path=str(proj / "a.png"))
    calls = []
    real = mission_shelf._hash_fd
    monkeypatch.setattr(mission_shelf, "_hash_fd", lambda fd, n: calls.append(n) or real(fd, n))
    for _ in range(3):
        assert mission_shelf.manifest(mission)["items"][0]["file"] == {"state": "ok"}
    assert calls == []


# ---- the read decision -------------------------------------------------------------------------


def test_inline_types_are_decided_on_the_bytes_served_now(mission, proj):
    (proj / "a.png").write_bytes(png())
    item = deliver(mission, title="a", why="w", path=str(proj / "a.png"))
    row = mission_shelf.get_item(mission, item["id"])
    assert mission_shelf.inline_type(row, str(proj / "a.png"), png()[:64], 70) == "image/png"
    assert mission_shelf.inline_type(row, str(proj / "a.png"), b"<html>", 6) is None
    (proj / "n.md").write_text("# x")
    doc = mission_shelf.get_item(
        mission, deliver(mission, title="n", why="w", path=str(proj / "n.md"))["id"]
    )
    assert mission_shelf.inline_type(doc, str(proj / "n.md"), b"# x", 3) == mission_shelf.TEXT_MIME
    assert mission_shelf.inline_type(doc, str(proj / "n.md"), b"#\x00x", 3) is None
    big = mission_shelf.TEXT_DOC_MAX + 1
    assert mission_shelf.inline_type(doc, str(proj / "n.md"), b"# x", big) is None


# ---- the broker and the tool ------------------------------------------------------------------


@pytest.fixture
def broker(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)
    return runtime


def _token(env: dict) -> str:
    return Path(env[shelf_broker.ENV_TOKEN_FILE]).read_text()


def test_only_a_missions_session_is_given_the_tool(broker, mission):
    assert shelf_broker.launch_env(OTHER, OTHER, engine="claude", manifest=MANIFEST) == {}
    no_channel = types.SimpleNamespace(tools_mcp=None)
    assert shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=no_channel) == {}
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    assert set(env) == shelf_broker.ENV_KEYS
    assert os.stat(env[shelf_broker.ENV_TOKEN_FILE]).st_mode & 0o077 == 0
    # A dispatch names its mission before the session is adopted.
    env2 = shelf_broker.launch_env(
        OTHER, OTHER, engine="claude", manifest=MANIFEST, mission_id=mission
    )
    assert set(env2) == shelf_broker.ENV_KEYS


def test_the_broker_delivers_as_the_launch_never_as_who_the_agent_says(broker, mission):
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    out = shelf_broker.handle(_token(env), "add", {"title": "t", "why": "w", "text": "hello"})
    assert out == {"ok": True, "id": out["id"], "kind": "text"}
    item = mission_shelf.manifest(mission)["items"][0]
    assert item["session_key"] == SESSION
    # Extra identity fields are not part of an item at all.
    bad = shelf_broker.handle(
        _token(env), "add", {"title": "t", "why": "w", "text": "x", "mission_id": "msn_" + "0" * 32}
    )
    assert bad == {"ok": False, "error": "bad_request"}
    assert shelf_broker.handle("nope", "add", {})["error"] == "unknown_capability"
    assert shelf_broker.handle(_token(env), "delete", {})["error"] == "bad_request"


def test_a_relaunch_retires_the_previous_capability(broker, mission):
    first = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    old = _token(first)
    second = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    assert shelf_broker.handle(old, "add", {"title": "t", "why": "w", "text": "x"}) == {
        "ok": False,
        "error": "unknown_capability",
    }
    assert shelf_broker.handle(_token(second), "add", {"title": "t", "why": "w", "text": "x"})["ok"]


def test_a_detached_session_keeps_its_token_but_shelves_nothing(broker, mission):
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    missions.detach(mission, SESSION)
    out = shelf_broker.handle(_token(env), "add", {"title": "t", "why": "w", "text": "x"})
    assert out == {"ok": False, "error": "not_in_mission"}


def test_the_app_never_carries_a_shelf_capability_in_its_own_environment(monkeypatch):
    monkeypatch.setenv(shelf_broker.ENV_TOKEN_FILE, "/x")
    shelf_broker.scrub_process_environment()
    assert shelf_broker.ENV_TOKEN_FILE not in os.environ


def test_the_broker_socket_is_never_taken_for_a_session(broker):
    from agent_sessions import ptybridge

    assert ptybridge.is_broker_socket(shelf_broker.socket_path().name)


def test_the_tool_rides_every_mcp_channel_by_path_never_by_token(broker, mission):
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    srv = agent_tools.shelf_server(env)
    assert srv.name == "battlelab_shelf"
    assert srv.args == ("-I", "-m", "agent_sessions.shelf_mcp")
    assert _token(env) not in json.dumps(dict(srv.env))
    launch = agent_tools.terminal_launch(MANIFEST, None, {}, shelf_env=env)
    cfg = json.loads(launch.suffix[-1])
    assert set(cfg["mcpServers"]) == {"battlelab_shelf"}
    copilot = json.loads(agent_tools.copilot_mcp_config([srv]))
    assert copilot["mcpServers"]["battlelab_shelf"]["tools"] == ["shelf_add"]
    assert [t["name"] for t in shelf_mcp.TOOLS] == ["shelf_add"]
    assert agent_tools.shelf_server({"x": "/y"}) is None


def _serve(sock: Path):
    started, stop = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()

        async def main():
            server = await asyncio.start_unix_server(shelf_broker._client, path=str(sock))
            stop["loop"], stop["server"] = loop, server
            started.set()
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    threading.Thread(target=run, daemon=True).start()
    assert started.wait(5)
    return stop


def test_the_real_tool_shelves_over_the_socket_and_explains_refusals(broker, mission, proj):
    """End to end: the MCP server as the agent CLI starts it (`python -I -m …`, in the workspace),
    a relative path resolved there, the broker, the store — and a refusal in words."""
    (proj / "out").mkdir()
    (proj / "out" / "shot.png").write_bytes(png())
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    stop = _serve(shelf_broker.socket_path())
    try:
        msgs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "shelf_add",
                    "arguments": {"title": "Shot", "why": "the hero", "path": "out/shot.png"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "shelf_add",
                    "arguments": {"title": "Key", "why": "w", "path": "/etc/hostname"},
                },
            },
        ]
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "agent_sessions.shelf_mcp"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True,
            text=True,
            cwd=proj,
            env={**{k: v for k, v in os.environ.items()}, **env},
            timeout=60,
        )
    finally:
        stop["loop"].call_soon_threadsafe(stop["server"].close)
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["shelf_add"]
    assert replies[3]["result"]["isError"] is False
    assert replies[4]["result"]["isError"] is True
    assert "outside this mission's project folders" in replies[4]["result"]["content"][0]["text"]
    item = mission_shelf.manifest(mission)["items"][0]
    assert item["path"] == str(proj / "out" / "shot.png") and item["kind"] == "image"


def test_the_tool_refuses_to_start_without_its_capability():
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "agent_sessions.shelf_mcp"],
        input="",
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("BATTLELAB_SHELF")},
        timeout=30,
    )
    assert proc.returncode == 2


def test_an_exclusion_written_through_a_symlinked_path_still_excludes(tmp_path):
    """Review on #1904: the file path and the folders are resolved, so an exclusion has to be
    compared resolved too — one typed through an alias of the project otherwise never matches."""
    real = tmp_path / "home" / "proj2"
    (real / "private").mkdir(parents=True)
    (real / "private" / "notes.txt").write_text("secret plan")
    alias = tmp_path / "home" / "alias"
    alias.symlink_to(real)
    m = missions.create_mission("x", cwd=str(alias))["id"]
    missions.adopt(m, SESSION)
    prefs.set_folder_exclusions([str(alias / "private")])
    assert refused(m, title="t", why="w", path=str(alias / "private" / "notes.txt")) == (
        "outside_project"
    )
    assert refused(m, title="t", why="w", path=str(real / "private" / "notes.txt")) == (
        "outside_project"
    )


def test_the_pages_of_one_read_share_one_check_of_every_file(mission, proj, monkeypatch):
    """Review on #1904: checking every file on every page made a full read quadratic."""
    monkeypatch.setattr(mission_shelf, "PAGE_MAX", 2)
    for n in range(5):
        (proj / f"{n}.png").write_bytes(png())
        deliver(mission, title=f"{n}", why="w", path=str(proj / f"{n}.png"))
    opened = []
    real = mission_shelf.open_verified
    monkeypatch.setattr(mission_shelf, "open_verified", lambda p, b: opened.append(p) or real(p, b))
    page = mission_shelf.manifest(mission)
    while page["next_cursor"]:
        page = mission_shelf.manifest(mission, cursor=page["next_cursor"])
    assert len(opened) == 5  # one check per file for the whole read, not per page
    mission_shelf.manifest(mission)  # a NEW read (first page) checks again
    assert len(opened) == 10


def test_a_file_shelved_while_hot_gets_its_fingerprint_once_it_is_quiet(mission, proj, monkeypatch):
    """Review on #1904: a file shelved within SETTLE_S of its write kept no fingerprint, so it was
    re-hashed on every read after a restart. The first quiet, matching read stores one."""
    monkeypatch.setattr(mission_shelf, "SETTLE_S", 3600.0)
    (proj / "a.png").write_bytes(png())
    item = deliver(mission, title="a", why="w", path=str(proj / "a.png"))
    assert mission_shelf.get_item(mission, item["id"])["fingerprint"] is None
    monkeypatch.setattr(mission_shelf, "SETTLE_S", -1.0)
    assert mission_shelf.manifest(mission)["items"][0]["file"] == {"state": "ok"}
    assert mission_shelf.get_item(mission, item["id"])["fingerprint"]
    mission_shelf.reset_cache_for_test()  # a restart
    calls = []
    real = mission_shelf._hash_fd
    monkeypatch.setattr(mission_shelf, "_hash_fd", lambda fd, n: calls.append(n) or real(fd, n))
    assert mission_shelf.manifest(mission)["items"][0]["file"] == {"state": "ok"}
    assert calls == []


def test_a_session_detached_while_its_file_is_read_commits_nothing(mission, proj, monkeypatch):
    (proj / "a.png").write_bytes(png())
    real = mission_shelf._read_file

    def detach_meanwhile(item, b):
        out = real(item, b)
        missions.detach(mission, SESSION)
        return out

    monkeypatch.setattr(mission_shelf, "_read_file", detach_meanwhile)
    assert refused(mission, title="t", why="w", path=str(proj / "a.png")) == "not_in_mission"
    assert mission_shelf.manifest(mission)["counts"]["total"] == 0


def test_an_excluded_file_is_never_even_opened(mission, proj, monkeypatch):
    (proj / "private").mkdir()
    (proj / "private" / "a.png").write_bytes(png())
    prefs.set_folder_exclusions([str(proj / "private")])
    opened = []
    monkeypatch.setattr(files, "_open_verified", lambda *a, **k: opened.append(a) or 1 / 0)
    assert refused(mission, title="t", why="w", path=str(proj / "private" / "a.png")) == (
        "outside_project"
    )
    assert opened == []


def test_a_name_swapped_after_its_check_is_refused_on_the_descriptor(
    mission, proj, tmp_path, monkeypatch
):
    """The resolved name passes; what is actually opened is somewhere else. The descriptor's own
    path is judged again, and the refusal closes it."""
    (proj / "a.png").write_bytes(png())
    elsewhere = tmp_path / "home" / "elsewhere.png"
    elsewhere.write_bytes(png())
    real = files._open_verified
    monkeypatch.setattr(files, "_open_verified", lambda p, **k: real(str(elsewhere), **k))
    before = len(os.listdir("/proc/self/fd"))
    assert refused(mission, title="t", why="w", path=str(proj / "a.png")) == "outside_project"
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_retired_token_file_or_a_revoked_row_each_refuses_alone(broker, mission):
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    token = _token(env)
    os.unlink(env[shelf_broker.ENV_TOKEN_FILE])  # the file gone, the row still active
    assert shelf_broker.handle(token, "add", {"title": "t", "why": "w", "text": "x"})["error"] == (
        "unknown_capability"
    )
    env = shelf_broker.launch_env(SESSION, SESSION, engine="claude", manifest=MANIFEST)
    token = _token(env)
    con = missions._ready()
    con.execute("UPDATE mission_shelf_caps SET revoked_at=1 WHERE revoked_at IS NULL")
    con.close()  # the row revoked, the file still there
    assert shelf_broker.handle(token, "add", {"title": "t", "why": "w", "text": "x"})["error"] == (
        "unknown_capability"
    )


def test_an_archiving_mission_takes_no_verdict(mission):
    item = deliver(mission, title="t", why="w", text="x")
    con = missions._ready()
    con.execute("UPDATE missions SET archiving_at=1 WHERE id=?", (mission,))
    con.close()
    with pytest.raises(missions.MissionError) as e:
        mission_shelf.set_verdict(mission, item["id"], expected_revision=1, verdict="good")
    assert e.value.status == 409


def test_the_shared_file_states_are_never_mutated_by_a_write_back(mission, proj, monkeypatch):
    """Review on #1904: two requests can hold the same cached states. The write-back list is built
    from the fresh check alone; nothing it needs is left in (or popped out of) the shared dict."""
    monkeypatch.setattr(mission_shelf, "SETTLE_S", 3600.0)
    (proj / "a.png").write_bytes(png())
    deliver(mission, title="a", why="w", path=str(proj / "a.png"))
    monkeypatch.setattr(mission_shelf, "SETTLE_S", -1.0)
    mission_shelf.manifest(mission)
    cached = mission_shelf._STATES[mission][2]
    assert all("settled_fp" not in st for st in cached.values())
    snapshot = {k: dict(v) for k, v in cached.items()}
    mission_shelf.manifest(mission, cursor="s0")  # a later page reuses it
    assert mission_shelf._STATES[mission][2] == snapshot
