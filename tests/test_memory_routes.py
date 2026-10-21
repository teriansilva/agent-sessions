"""The Memory page's routes (#1441 P2): auth on every route, CSRF + Origin on every mutation,
one project never reaching another's notes, and the withhold rule on the operator's own edits."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import memory, projects, template_secrets
from agent_sessions.main import create_app

NOTE = "---\nname: Typed ids\ntype: decision\n---\n\nScopes are ids. See [[other-note]].\n"
OTHER = "# Other note\n\nA second note.\n"
SECRET = "# Deploy\n\nUse it like this:\ntoken=ghp_" + "a" * 36 + "\nthen push.\n"


@pytest.fixture(autouse=True)
def _settled(monkeypatch):
    monkeypatch.setattr(memory, "SETTLE_S", 0.0)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


def _project(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    pid = projects.create(name, folders=[str(folder)]).id
    memory.set_enabled(pid, True)
    return pid, folder


def _drop(pid, folder, name, text, key="fixture:" + "a" * 8):
    env = memory.launch_env(key, str(folder), kind="terminal")
    path = Path(env[memory.ENV_INBOX]) / name
    path.write_text(text)
    os.utime(path, (1, 1))
    return path


@pytest.fixture
def two(tmp_path):
    a, fa = _project(tmp_path, "alpha")
    b, fb = _project(tmp_path, "beta")
    _drop(a, fa, "n.md", NOTE)
    _drop(a, fa, "o.md", OTHER)
    _drop(a, fa, "s.md", SECRET)
    memory.ingest(a)
    _drop(b, fb, "b.md", "# Beta only\n\nbeta body\n")
    memory.ingest(b)
    return a, b


@pytest.fixture
def client(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    c.headers.update(
        {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    )
    return c


def _qid(a):
    return memory.view(a)["quarantine"][0]["id"]


ROUTES = [
    ("GET", "/api/memory"),
    ("GET", "/api/memory/{a}"),
    ("POST", "/api/memory/{a}/ingest"),
    ("PUT", "/api/memory/{a}/enabled"),
    ("PATCH", "/api/memory/{a}/notes/typed-ids"),
    ("DELETE", "/api/memory/{a}/notes/typed-ids"),
    ("POST", "/api/memory/{a}/clear"),
    ("GET", "/api/memory/{a}/export"),
    ("PATCH", "/api/memory/{a}/quarantine/{q}"),
    ("DELETE", "/api/memory/{a}/quarantine/{q}"),
]


def test_every_route_needs_a_login(auth_cfg, two):
    a, _ = two
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    for method, path in ROUTES:
        r = anon.request(
            method, path.format(a=a, q=_qid(a)), json={}, headers={"Origin": auth_cfg.origin}
        )
        assert r.status_code in (401, 403), (method, path, r.status_code)
    assert len(memory.notes(a)) == 2


@pytest.mark.parametrize("headers", [{"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.test"}])
def test_every_mutation_needs_the_csrf_token_and_the_apps_origin(client, two, headers):
    a, _ = two
    before = memory.view(a)
    for method, path in ROUTES:
        if method == "GET":
            continue
        r = client.request(
            method,
            path.format(a=a, q=_qid(a)),
            json={"enabled": False, "body": "x", "text": "x"},
            headers=headers,
        )
        assert r.status_code == 403, (method, path, r.status_code)
    after = memory.view(a)
    assert after["enabled"] and len(after["notes"]) == len(before["notes"])
    assert len(after["quarantine"]) == 1


def test_one_project_never_reaches_anothers_notes(client, two):
    a, b = two
    q = _qid(a)
    assert client.patch(f"/api/memory/{b}/notes/typed-ids", json={"body": "x"}).status_code == 404
    assert client.delete(f"/api/memory/{b}/notes/typed-ids").status_code == 404
    assert (
        client.patch(f"/api/memory/{b}/quarantine/{q}", json={"text": "# x\n\ny"}).status_code
        == 404
    )
    assert client.delete(f"/api/memory/{b}/quarantine/{q}").status_code == 404
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/memory/{b}/export").content)).namelist()
    assert "memory/typed-ids.md" not in names and "memory/beta-only.md" in names
    view_b = client.get(f"/api/memory/{b}").json()
    assert [n["slug"] for n in view_b["notes"]] == ["beta-only"]
    assert len(memory.view(a)["notes"]) == 2 and len(memory.view(a)["quarantine"]) == 1


@pytest.mark.parametrize("bad", ["..", "P-UPPER", "p-1%2F..%2Fp-2", "x" * 70, "__default__"])
def test_a_malformed_or_unknown_project_is_refused(client, two, bad):
    for method, path in ROUTES:
        if "{a}" not in path:
            continue
        r = client.request(method, path.format(a=bad, q=1), json={"enabled": True})
        assert r.status_code in (404, 405, 422), (method, path, r.status_code)
    assert client.get("/api/memory/p-00000000").status_code == 404


def test_the_view_shows_writers_links_and_a_masked_quarantine(client, two):
    a, _ = two
    r = client.get(f"/api/memory/{a}")
    assert r.status_code == 200 and r.headers["cache-control"].startswith("no-store")
    view = r.json()
    notes = {n["slug"]: n for n in view["notes"]}
    assert notes["typed-ids"]["links"] == ["other-note"]
    assert notes["typed-ids"]["session"]["engine"] == "fixture"
    [q] = view["quarantine"]
    assert "ghp_" not in r.text  # the withheld value never leaves the server
    assert q["masked"] == memory._MASK  # nothing of a quarantined item's content is shown
    assert "then push." not in r.text
    assert view["instructions"] == memory.INSTRUCTION_PARAGRAPH
    listing = client.get("/api/memory").json()["projects"]
    row = next(p for p in listing if p["id"] == a)
    assert (row["enabled"], row["notes"], row["quarantined"]) == (True, 2, 1)


def test_toggle_edit_delete_clear_and_export(client, two):
    a, _ = two
    assert client.put(f"/api/memory/{a}/enabled", json={"enabled": False}).status_code == 200
    assert memory.enabled(a) is False
    assert client.put(f"/api/memory/{a}/enabled", json={"enabled": "yes"}).status_code == 422
    r = client.patch(f"/api/memory/{a}/notes/typed-ids", json={"type": "pitfall", "body": "New."})
    assert r.status_code == 200
    note = next(n for n in memory.notes(a) if n["slug"] == "typed-ids")
    assert (note["type"], note["body"]) == ("pitfall", "New.")
    assert client.patch(f"/api/memory/{a}/notes/typed-ids", json={"type": "x"}).status_code == 422
    files = zipfile.ZipFile(io.BytesIO(client.get(f"/api/memory/{a}/export").content))
    assert "New." in files.read("memory/typed-ids.md").decode()
    assert not any("ghp_" in files.read(n).decode() for n in files.namelist())
    assert client.delete(f"/api/memory/{a}/notes/typed-ids").status_code == 204
    assert client.post(f"/api/memory/{a}/clear").status_code == 204
    assert memory.view(a)["notes"] == [] and memory.view(a)["quarantine"] == []


def test_an_operator_edit_is_held_to_the_same_withhold_rule(client, two, monkeypatch):
    a, _ = two
    r = client.patch(f"/api/memory/{a}/notes/typed-ids", json={"body": "password: hunter2hunter2"})
    assert r.status_code == 422 and "credential" in r.json()["detail"]
    assert "hunter2" not in next(n for n in memory.notes(a) if n["slug"] == "typed-ids")["body"]

    def unavailable():
        raise template_secrets.RedactionUnavailable("key missing")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    r = client.patch(f"/api/memory/{a}/notes/typed-ids", json={"body": "fine"})
    assert r.status_code == 503  # fail closed: nothing saved
    assert client.post(f"/api/memory/{a}/ingest").status_code == 503


def test_a_quarantined_item_is_released_from_its_masked_text_or_dropped(client, two):
    a, _ = two
    q = memory.view(a)["quarantine"][0]
    r = client.patch(f"/api/memory/{a}/quarantine/{q['id']}", json={"text": SECRET})
    assert r.status_code == 422  # the raw secret still fails
    replacement = "# Deploy\n\nUse the deploy key from the vault, then push.\n"
    r = client.patch(f"/api/memory/{a}/quarantine/{q['id']}", json={"text": replacement})
    assert r.status_code == 200 and r.json()["slug"] == "deploy"
    assert memory.view(a)["quarantine"] == []
    assert "ghp_" not in next(n for n in memory.notes(a) if n["slug"] == "deploy")["body"]
    assert client.delete(f"/api/memory/{a}/quarantine/{q['id']}").status_code == 404


def test_ingest_runs_a_pass_now(client, tmp_path):
    a, folder = _project(tmp_path, "gamma")
    _drop(a, folder, "late.md", "# Late\n\nbody\n")
    r = client.post(f"/api/memory/{a}/ingest")
    assert r.status_code == 200 and r.json()["notes"] == 1


def test_a_dropped_quarantined_item_is_never_taken_in_again(client, two, tmp_path):
    a, _ = two
    q = memory.view(a)["quarantine"][0]
    assert client.delete(f"/api/memory/{a}/quarantine/{q['id']}").status_code == 204
    import sqlite3

    with sqlite3.connect(memory.root() / a / "state.db") as conn:
        conn.execute("UPDATE receipts SET at = 0")  # past the TTL: only a tombstone survives
    memory.ingest(a)  # trims
    folder = Path(projects.load()[a].folders[0])
    _drop(a, folder, "again.md", SECRET)  # the very same bytes
    assert memory.ingest(a)["dropped"] == 1 and memory.view(a)["quarantine"] == []


def test_export_leaves_out_a_note_holding_a_secret_saved_since(client, two, monkeypatch):
    a, _ = two
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["A second note"])
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/memory/{a}/export").content)).namelist()
    assert "memory/other-note.md" not in names and "memory/typed-ids.md" in names


def test_releasing_holds_the_original_file_name_to_the_withhold_rule(client, tmp_path, monkeypatch):
    a, folder = _project(tmp_path, "delta")
    _drop(a, folder, "s3cr3t-NAME-77.md", "token=ghp_" + "b" * 36 + "\n")
    memory.ingest(a)
    q = memory.view(a)["quarantine"][0]
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["s3cr3t-NAME-77"])
    r = client.patch(f"/api/memory/{a}/quarantine/{q['id']}", json={"text": "harmless body\n"})
    assert r.status_code == 422 and memory.notes(a) == []


def test_a_released_note_is_tombstoned_when_it_is_deleted(client, two):
    import sqlite3

    a, _ = two
    q = memory.view(a)["quarantine"][0]
    released = "# Deploy\n\nthe operator's own wording\n"
    slug = client.patch(f"/api/memory/{a}/quarantine/{q['id']}", json={"text": released}).json()[
        "slug"
    ]
    folder = Path(projects.load()[a].folders[0])
    _drop(a, folder, "newer.md", "# Deploy\n\na newer version of the same note\n")
    memory.ingest(a)  # replaces the released version under the same name
    assert client.delete(f"/api/memory/{a}/notes/{slug}").status_code == 204
    with sqlite3.connect(memory.root() / a / "state.db") as conn:
        conn.execute("UPDATE receipts SET at = 0")
    memory.ingest(a)  # trims
    _drop(a, folder, "again.md", released)  # the released bytes, re-submitted
    assert memory.ingest(a)["dropped"] == 1
    assert slug not in [n["slug"] for n in memory.notes(a)]


# ---- review round 1 on #1453 ---------------------------------------------------------------------


def test_a_secret_spanning_lines_masks_the_whole_quarantined_item(client, tmp_path, monkeypatch):
    secret = "first-half-SECRET-xy\nsecond-half-SECRET-zw"
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    a, folder = _project(tmp_path, "multi")
    _drop(a, folder, "m.md", f"# Note\n\nkey:\n{secret}\nafter\n")
    memory.ingest(a)
    r = client.get(f"/api/memory/{a}")
    [q] = r.json()["quarantine"]
    assert q["masked"] == memory._MASK
    assert "first-half-SECRET" not in r.text and "second-half-SECRET" not in r.text


def test_credential_bearing_file_names_are_masked_in_every_tray(client, tmp_path):
    a, folder = _project(tmp_path, "names")
    leaky = "token=ghp_" + "c" * 36 + ".md"
    _drop(a, folder, leaky, "a harmless body\n")
    memory.ingest(a)  # quarantined BECAUSE of its name
    env = memory.launch_env("fixture:names", str(folder), kind="terminal")
    pending = Path(env[memory.ENV_INBOX]) / ("password=" + "d" * 20 + ".md")
    pending.write_text("not yet taken in\n")  # fresh: still pending
    r = client.get(f"/api/memory/{a}")
    view = r.json()
    assert [q["filename"] for q in view["quarantine"]] == [memory._MASK_NAME]
    assert [p["filename"] for p in view["pending"]] == [memory._MASK_NAME]
    assert "ghp_" not in r.text and "d" * 20 not in r.text
    assert isinstance(view["quarantine"][0]["id"], int)  # actions still have their opaque id


def test_a_secret_whose_first_line_is_itself_masked_leaks_no_continuation(
    client, tmp_path, monkeypatch
):
    """#1453 review 2: the per-line pass masked the credential-shaped first line, after which the
    whole-text check no longer matched and the continuation went out. Now nothing goes out."""
    secret = "password=hunter2-first-half\ncontinuation-second-half-77"
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    a, folder = _project(tmp_path, "cont")
    _drop(a, folder, "c.md", f"# Note\n\n{secret}\n")
    memory.ingest(a)
    r = client.get(f"/api/memory/{a}")
    [q] = r.json()["quarantine"]
    assert q["masked"] == memory._MASK
    assert "continuation-second-half" not in r.text and "hunter2" not in r.text


# ---- review round 3 on #1453 ---------------------------------------------------------------------


def test_the_page_withholds_a_note_whose_view_could_only_be_withdrawn(
    client, tmp_path, monkeypatch
):
    """A secret saved after a note was taken in, with the re-render failing: the view was withdrawn
    but the note is still in the store. The page's API must withhold it all the same."""
    a, folder = _project(tmp_path, "withdrawn")
    value = "staging-build-label-golf"
    _drop(a, folder, "n.md", f"# Staging\n\nUse {value} as the build label.\n")
    memory.ingest(a)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [value])

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "_render", broken)
    memory.secrets_changed()  # withdraws (evicts) the view; the store row remains
    assert [n["slug"] for n in memory.notes(a)] == ["staging"]
    r = client.get(f"/api/memory/{a}")
    assert value not in r.text
    view = r.json()
    assert view["notes"] == [] and view["withheld"]["notes"] == 1


def test_the_page_withholds_every_note_when_the_secret_check_cannot_run(client, two, monkeypatch):
    a, _ = two

    def unavailable():
        raise template_secrets.RedactionUnavailable("key missing")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    view = client.get(f"/api/memory/{a}").json()
    assert view["notes"] == [] and view["withheld"]["notes"] == 2
    assert "could not run" in view["withheld"]["reason"]


# ---- review round 4 on #1453 ---------------------------------------------------------------------


def _broken(*a, **k):
    raise OSError("disk full")


def _zip_entries(blob: bytes) -> list[tuple[str, bytes]]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    return [(n, zf.read(n)) for n in zf.namelist()]


@pytest.mark.parametrize("render_fails", [False, True], ids=["re-rendered", "withdrawn"])
def test_a_note_whose_slug_is_now_a_secret_is_withheld_everywhere(
    client, tmp_path, monkeypatch, render_fails
):
    """A note keeps the slug its first name gave it. Renamed and rewritten to safe text, then that
    slug stored as a secret: the page, the export and the agents' view must all leave it out —
    names and ids included, not just the text. (No assertion prints the value.)"""
    a, folder = _project(tmp_path, "slugged")
    _drop(a, folder, "n.md", "# Orbit Kilo Ninety\n\nfirst body\n")
    memory.ingest(a)
    (slug,) = [n["slug"] for n in memory.notes(a)]
    memory.update_note(
        a, slug, {"name": "Release steps", "description": "how we ship", "body": "Tag, then wait."}
    )
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [slug])
    if render_fails:
        monkeypatch.setattr(memory, "_render", _broken)
    memory.secrets_changed()

    page = client.get(f"/api/memory/{a}")
    page_leaks = slug in page.text
    assert not page_leaks, "the page returned the stored value"
    assert page.json()["notes"] == []
    export = _zip_entries(client.get(f"/api/memory/{a}/export").content)
    export_leaks = any(slug in name or slug.encode() in data for name, data in export)
    assert not export_leaks, "the export returned the stored value"
    view = memory.root() / a / "notes"
    agent_leaks = any(
        slug in p.name or slug.encode() in p.read_bytes() for p in view.iterdir() if p.is_file()
    )
    assert not agent_leaks, "the agents' view returned the stored value"


def test_an_export_holds_the_secret_writers_fence(tmp_path, monkeypatch):
    """A secret write that lands while an export is being built — with its re-render failing, so
    the note stays in the store — waits for the export, never interleaves with it. Afterwards an
    export leaves the note out."""
    import threading

    from agent_sessions import session_input

    a, folder = _project(tmp_path, "fenced")
    value = "harbor-lantern-quartz"
    _drop(a, folder, "n.md", f"# Harbor\n\nUse {value} as the label.\n")
    memory.ingest(a)
    inventory: list[str] = []
    read, go, wrote = threading.Event(), threading.Event(), threading.Event()

    def values():
        snapshot = list(inventory)
        if threading.current_thread().name == "export" and not read.is_set():
            read.set()
            go.wait(5)
        return snapshot

    monkeypatch.setattr(template_secrets, "redaction_values", values)
    monkeypatch.setattr(memory, "_render", _broken)
    out: dict = {}
    exporter = threading.Thread(target=lambda: out.update(z=memory.export_zip(a)), name="export")
    exporter.start()
    assert read.wait(5)

    def write_secret():
        with session_input.mutation_fence():
            inventory.append(value)
            memory.secrets_changed()
        wrote.set()

    writer = threading.Thread(target=write_secret)
    writer.start()
    interleaved = wrote.wait(1.0)
    go.set()
    exporter.join(5)
    writer.join(5)
    assert not exporter.is_alive() and not writer.is_alive()
    assert not interleaved, "a secret write completed while an export was being built"
    assert [n["slug"] for n in memory.notes(a)] == ["harbor"]  # the re-render failed: still stored
    after = _zip_entries(memory.export_zip(a))
    leaks = any(value.encode() in data for _, data in after)
    assert not leaks, "an export after the secret write returned it"
