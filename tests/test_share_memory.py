"""Share bundles carry project memory (#1714 P4): one project's read view leaves (secret-bearing
and quarantined notes left out and only counted; fails closed), and comes in one note at a time
through memory's own write path into a project the operator names, whose memory is on — judged by
the withhold check an agent's note gets, never over an existing note, journaled per note."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_sessions import (
    engines,
    fileedit,
    memory,
    projects,
    specialists,
    template_secrets,
    variables,
)
from agent_sessions import specialists_store as pool
from agent_sessions.gallery import library
from agent_sessions.playbooks import loader
from agent_sessions.sharebundle import format as fmt
from agent_sessions.sharebundle import kinds, memory_kinds, service, state

SECRET = "s3cr3t-Value-for-the-deploy-0042"  # credential-shaped: ingest quarantines it
WORDS = "purple-elephant-cabbage-salad"  # a stored secret that doesn't LOOK like one
AGENT = "claude"
MODEL = "claude-opus-5"


def _install(root: Path, monkeypatch) -> Path:
    """One install: its own share state, memory store, projects, secrets and specialist pool."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(root / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_MEMORY_DIR", str(root / "memory"))
    monkeypatch.setenv("AGENT_SESSIONS_PROJECTS", str(root / "cfg" / "projects.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(root / "playbooks"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_VARS", str(root / "cfg" / "vars.json"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_SECRETS_KEY", str(root / "cfg" / "secrets.key"))
    monkeypatch.setenv("AGENT_SESSIONS_SPECIALISTS_DB", str(root / "specialists.db"))
    pool.reset_schema_cache_for_test()
    empty = root / "bundled"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(loader, "BUNDLED_ROOT", empty)
    return root


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(memory, "SETTLE_S", 0.0)
    monkeypatch.setattr(engines.registry, "can_start", lambda prov: prov is not None)


@pytest.fixture
def install(tmp_path, monkeypatch):
    assert fileedit.install_lease_signal_handler()
    return _install(tmp_path / "one", monkeypatch)


def _second(tmp_path, monkeypatch) -> Path:
    return _install(tmp_path / "two", monkeypatch)


def _project(root: Path, name: str, *, on: bool = True) -> str:
    work = root / f"work-{name}"
    work.mkdir(parents=True, exist_ok=True)
    pid = projects.create(name, folders=[str(work)]).id
    if on:
        memory.set_enabled(pid, True)
    return pid


def _note(pid: str, filename: str, text: str, scope: str | None = None) -> None:
    """An agent's note, taken in by the normal ingest (the path every stored note came from)."""
    folder = projects.load()[pid].folders[0]
    env = memory.launch_env("fixture:" + "a" * 8, folder, kind="terminal")
    inbox = Path(env[memory.ENV_INBOX])
    path = inbox / filename
    path.write_text(text)
    old = path.stat().st_mtime - 10
    os.utime(path, (old, old))
    memory.ingest(scope or pid)


def _seed(pid: str) -> None:
    _note(pid, "typed.md", "---\nname: Use typed ids\ntype: decision\n---\n\nScopes are ids.\n")
    _note(pid, "deploy.md", "# Deploy\n\nRun the staging deploy before main.\n")


def _export(*items: tuple[str, str], report: dict | None = None) -> bytes:
    _, data = service.export([{"kind": k, "id": i} for k, i in items], report=report)
    return data


def _plan(preview: dict) -> dict:
    return {i["key"]: i for i in preview["items"]}


def _keys(preview: dict) -> list[str]:
    return [i["key"] for i in preview["items"] if i["status"] not in ("refused", "reused")]


def _names(pid: str) -> list[str]:
    return sorted(n["name"] for n in memory.notes(pid))


def _bundle(items: list[dict]) -> bytes:
    return fmt.write(items, created_at="2026-11-09T00:00:00Z", app_version="test")


def _memory_item(*notes: dict, item_id: str = "project", extra: dict | None = None) -> dict:
    files = {
        memory_kinds.note_file(n): json.dumps({"format": 1, **note}).encode()
        for n, note in enumerate(notes, 1)
    }
    files.update(extra or {})
    return {"kind": "memory", "id": item_id, "title": "Project memory", "files": files}


def _n(name: str, body: str, type_: str = "finding", description: str = "") -> dict:
    return {"name": name, "description": description or name, "type": type_, "body": body}


# ---- export ---------------------------------------------------------------------------------


def test_a_project_exports_its_read_view_and_nothing_that_names_it(install):
    pid = _project(install, "zebra-ops")
    _seed(pid)
    report: dict = {}
    data = _export(("memory", pid), report=report)
    bundle = fmt.read(data)
    assert [i.key for i in bundle.items] == ["memory:project"]
    item = bundle.item("memory:project")
    assert sorted(item.files) == ["notes/0001.json", "notes/0002.json"]
    docs = [json.loads(item.files[p]) for p in sorted(item.files)]
    assert {d["name"] for d in docs} == {"Use typed ids", "Deploy"}
    assert all(set(d) == {"format", "name", "description", "type", "body"} for d in docs)
    # Neither the project's name, its id, nor a note's local slug travels.
    assert b"zebra-ops" not in data and pid.encode() not in data
    assert b"use-typed-ids" not in data
    assert report == {"withheld": 0, "quarantined": 0}


def test_a_note_holding_a_secret_stored_since_is_left_out_and_only_counted(install, monkeypatch):
    """A value stored after the note, before any re-render moved it aside: the export judges
    against the inventory as of the request and leaves the note out, counting it."""
    pid = _project(install, "alpha")
    _seed(pid)
    _note(pid, "leak.md", f"# Rotation\n\nThe phrase is {WORDS} for now.\n")
    real = template_secrets.redaction_values
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [*real(), WORDS])
    report: dict = {}
    data = _export(("memory", pid), report=report)
    assert WORDS.encode() not in data and b"Rotation" not in data
    assert report == {"withheld": 1, "quarantined": 0}
    row = next(r for r in kinds.catalog() if r["kind"] == "memory")
    assert row["memory"] == {"notes": 2, "withheld": 1, "quarantined": 0}
    assert "Rotation" not in json.dumps(row)


def test_a_secret_stored_through_the_library_moves_the_note_aside_before_export(install):
    pid = _project(install, "alpha")
    _seed(pid)
    _note(pid, "leak.md", f"# Rotation\n\nThe phrase is {WORDS} for now.\n")
    variables.create({"name": "deploy_token", "kind": "secret", "definition": {}, "value": WORDS})
    report: dict = {}
    data = _export(("memory", pid), report=report)
    assert WORDS.encode() not in data and b"Rotation" not in data
    assert report == {"withheld": 0, "quarantined": 1}  # the re-render moved it aside


def test_a_secret_folded_into_a_note_name_is_left_out(install):
    """A slug is the name lowercased: a secret typed in another case into a name would become the
    importing install's slug, so the name is scanned as an identifier (folded). The read view's
    own rule is verbatim and doesn't see it."""
    pid = _project(install, "alpha")
    _note(pid, "a.md", "# Plain thing\n\nFine body.\n")
    _note(pid, "b.md", "# Other\n\nFine body.\n")
    variables.create({"name": "tok", "kind": "secret", "definition": {}, "value": WORDS})
    memory.update_note(pid, "plain-thing", {"name": "Use Purple-Elephant-Cabbage-Salad"})
    assert len(memory.view(pid)["notes"]) == 2  # verbatim, nothing holds a stored secret
    report: dict = {}
    data = _export(("memory", pid), report=report)
    assert b"Elephant" not in data and report == {"withheld": 1, "quarantined": 0}


def test_quarantine_never_travels_and_is_counted(install):
    pid = _project(install, "alpha")
    _seed(pid)
    _note(pid, "cred.md", "token=ghp_" + "b" * 36 + "\n")
    assert memory.view(pid)["quarantine"]
    report: dict = {}
    data = _export(("memory", pid), report=report)
    assert b"ghp_" not in data and report == {"withheld": 0, "quarantined": 1}


def test_an_unavailable_secret_check_exports_no_memory(install, monkeypatch):
    pid = _project(install, "alpha")
    _seed(pid)

    def unavailable():
        raise template_secrets.RedactionUnavailable("no")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    with pytest.raises(service.ShareError) as err:
        _export(("memory", pid))
    assert err.value.status == 422 and "secret check can't run" in err.value.detail
    row = next(r for r in kinds.catalog() if r["kind"] == "memory")
    assert row["exportable"] is False and row["memory"] is None


def test_a_file_carries_the_memory_of_one_project(install):
    a, b = _project(install, "alpha"), _project(install, "beta")
    _seed(a)
    _seed(b)
    with pytest.raises(service.ShareError) as err:
        _export(("memory", a), ("memory", b))
    assert err.value.status == 422 and "one project" in err.value.detail


# ---- import ---------------------------------------------------------------------------------


def test_a_round_trip_lands_in_the_chosen_project_only(tmp_path, monkeypatch, install):
    src = _project(install, "alpha")
    _seed(src)
    data = _export(("memory", src))
    root = _second(tmp_path, monkeypatch)
    target, other = _project(root, "target"), _project(root, "other")
    preview = service.preview(data)
    entry = _plan(preview)["memory:project"]
    assert entry["status"] == "new" and entry["dest"] is None
    assert [n["verdict"] for n in entry["memory"]["notes"]] == ["new", "new"]
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    item = result["items"][0]
    assert item["status"] == "created" and item["dest"] == target
    assert item["memory"]["counts"]["created"] == 2
    assert _names(target) == ["Deploy", "Use typed ids"]
    assert memory.notes(other) == []  # never another project, unasked
    note = next(n for n in memory.view(target)["notes"] if n["name"] == "Deploy")
    assert note["session"]["kind"] == "share"  # attributed to the file, not an agent
    assert "Deploy" in (Path(memory.root()) / target / "notes" / "MEMORY.md").read_text()


def test_notes_never_land_without_a_named_project_with_memory_on(tmp_path, monkeypatch, install):
    src = _project(install, "alpha")
    _seed(src)
    data = _export(("memory", src))
    root = _second(tmp_path, monkeypatch)
    off = _project(root, "dark", on=False)
    preview = service.preview(data)
    for project, word in ((None, "Choose the project"), (off, "memory is off")):
        with pytest.raises(service.ShareError) as err:
            service.confirm(preview["token"], _keys(preview), memory_project=project)
        assert err.value.status == 422 and err.value.extra["reason"] == "memory_project"
        assert word.lower() in err.value.detail.lower()
    assert not (state.root() / f"journal-{preview['token']}.json").exists()  # nothing journaled
    assert memory.enabled(off) is False  # an import never turns memory on
    memory.set_enabled(off, True)
    result = service.confirm(preview["token"], _keys(preview), memory_project=off)
    assert result["items"][0]["memory"]["counts"]["created"] == 2


def test_memory_turned_off_after_the_confirm_began_fails_the_note_retriably(
    tmp_path, monkeypatch, install
):
    src = _project(install, "alpha")
    _seed(src)
    data = _export(("memory", src))
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    preview = service.preview(data)
    real = memory.share_import_note
    calls = []

    def off_after_first(scope, note, **kw):
        calls.append(note["name"])
        if len(calls) == 2:
            memory.set_enabled(target, False)
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", off_after_first)
    first = service.confirm(preview["token"], _keys(preview), memory_project=target)
    item = first["items"][0]
    assert item["status"] == "failed" and item["retry"] is True
    assert item["memory"]["counts"]["created"] == 1 and item["memory"]["counts"]["retry"] == 1
    memory.set_enabled(target, True)
    again = service.confirm(preview["token"], _keys(preview), memory_project=target)
    assert again["items"][0]["status"] == "created"
    assert len(memory.notes(target)) == 2


def test_an_existing_note_is_never_overwritten_an_identical_one_is_already_here(
    tmp_path, monkeypatch, install
):
    src = _project(install, "alpha")
    _seed(src)
    data = _export(("memory", src))
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    _note(target, "mine.md", "# Deploy\n\nOur own deploy rule, different.\n")
    preview = service.preview(data)
    service.confirm(preview["token"], _keys(preview), memory_project=target)
    rows = {n["slug"]: n for n in memory.notes(target)}
    assert rows["deploy"]["body"] == "# Deploy\n\nOur own deploy rule, different."  # untouched
    assert rows["deploy-2"]["body"] == "# Deploy\n\nRun the staging deploy before main."
    # The same file again, under a new preview: nothing is added twice.
    for path in state.root().iterdir():
        if path.name.startswith(("preview-", "journal-")):
            path.unlink()
    again = service.preview(data)
    result = service.confirm(again["token"], _keys(again), memory_project=target)
    assert result["items"][0]["memory"]["counts"]["already"] == 2
    assert len(memory.notes(target)) == 3


def test_a_note_carrying_a_credential_is_held_back_whole(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    cred = "token=ghp_" + "c" * 36
    data = _bundle([_memory_item(_n("Fine", "A fine note."), _n("Leaky", f"use {cred}"))])
    preview = service.preview(data)
    entry = _plan(preview)["memory:project"]
    verdicts = {n["file"]: n for n in entry["memory"]["notes"]}
    assert verdicts["notes/0002.json"]["verdict"] == "held"
    assert "name" not in verdicts["notes/0002.json"]  # its own text isn't shown
    files = {f["path"]: f for f in entry["files"]}
    assert files["notes/0002.json"].get("withheld") and "text" not in files["notes/0002.json"]
    assert "A fine note." in files["notes/0001.json"]["text"]
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    counts = result["items"][0]["memory"]["counts"]
    assert counts["created"] == 1 and counts["held"] == 1
    assert _names(target) == ["Fine"]
    assert memory.view(target)["quarantine"] == []  # never into a review tray
    raw = json.dumps(result) + (state.root() / f"journal-{preview['token']}.json").read_text()
    assert "ghp_" not in raw and "A fine note." not in raw  # ids, digests and reasons only


def test_a_note_holding_a_stored_secret_here_is_held_back(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    variables.create({"name": "t", "kind": "secret", "definition": {}, "value": SECRET})
    data = _bundle([_memory_item(_n("Ours", f"key {SECRET}"), _n("Fine", "ok"))])
    preview = service.preview(data)
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    notes = {n["file"]: n for n in result["items"][0]["memory"]["notes"]}
    assert notes["notes/0001.json"]["outcome"] == "held"
    assert notes["notes/0001.json"]["reason"] == "contains a stored secret value"
    assert _names(target) == ["Fine"]


def test_hostile_memory_items_are_refused_per_rule(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    stray = _memory_item(_n("A", "b"), extra={"run.sh": b"echo hi\n"})
    named = _memory_item(_n("A", "b"), item_id="zebra-ops")
    bad = _memory_item(_n("A", "b"))
    bad["files"]["notes/0002.json"] = b'{"format": 1, "name": "x", "body": "y"}'
    bad["files"]["notes/0003.json"] = json.dumps(
        {"format": 1, **_n("  spaced   name ", "body")}
    ).encode()
    for item, why in ((stray, "notes/<n>.json files only"), (named, "travels as 'project'")):
        preview = service.preview(_bundle([item]))
        entry = preview["items"][0]
        assert entry["status"] == "refused" and why in entry["reason"]
    preview = service.preview(_bundle([bad]))
    notes = {n["file"]: n for n in preview["items"][0]["memory"]["notes"]}
    assert notes["notes/0001.json"]["verdict"] == "new"
    assert notes["notes/0002.json"]["verdict"] == "refused"
    assert notes["notes/0003.json"]["verdict"] == "refused"
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    assert result["items"][0]["memory"]["counts"] == {
        "created": 1,
        "already": 0,
        "held": 0,
        "refused": 2,
        "failed": 0,
        "retry": 0,
        "pending": 0,
    }
    assert _names(target) == ["A"]


# ---- per-note atomicity, journal and replay -------------------------------------------------


class _Crash(BaseException):
    pass


def test_one_failing_note_leaves_the_others_and_is_retried_on_its_own(
    tmp_path, monkeypatch, install
):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"), _n("Two", "2"), _n("Three", "3"))])
    preview = service.preview(data)
    real = memory.share_import_note

    def flaky(scope, note, **kw):
        if note["name"] == "Two":
            raise OSError(5, "disk")
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", flaky)
    first = service.confirm(preview["token"], _keys(preview), memory_project=target)
    item = first["items"][0]
    assert item["status"] == "failed" and item["retry"] is True
    assert item["memory"]["counts"]["created"] == 2 and item["memory"]["counts"]["retry"] == 1
    assert "EIO" in next(n for n in item["memory"]["notes"] if n["outcome"] == "retry")["reason"]
    seen = []

    def once(scope, note, **kw):
        seen.append(note["name"])
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", once)
    again = service.confirm(preview["token"], _keys(preview), memory_project=target)
    assert seen == ["Two"]  # only the failed note is tried again
    assert again["items"][0]["status"] == "created"
    assert _names(target) == ["One", "Three", "Two"]


def test_a_crash_after_a_notes_commit_reconciles_to_created_never_twice(
    tmp_path, monkeypatch, install
):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"), _n("Two", "2"))])
    preview = service.preview(data)
    real = memory.share_import_note

    def commit_then_crash(scope, note, **kw):
        out = real(scope, note, **kw)
        if note["name"] == "One":
            raise _Crash()  # after its commit, before its outcome is journaled
        return out

    monkeypatch.setattr(memory, "share_import_note", commit_then_crash)
    with pytest.raises(_Crash):
        service.confirm(preview["token"], _keys(preview), memory_project=target)
    journal = state.read_json(f"journal-{preview['token']}.json")
    rec = journal["notes"]["memory:project"]["files"]["notes/0001.json"]
    assert "intent_at" in rec and "outcome" not in rec
    seen = []

    def watch(scope, note, **kw):
        seen.append(note["name"])
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", watch)
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    assert seen == ["Two"]  # "One" was settled by its slug and digest, never taken in again
    assert result["items"][0]["memory"]["counts"]["created"] == 2
    assert _names(target) == ["One", "Two"]


def test_a_crash_whose_slug_now_holds_another_note_fails_it_untouched(
    tmp_path, monkeypatch, install
):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"))])
    preview = service.preview(data)

    def intent_then_crash(scope, note, *, writer, intent):
        intent("one")
        raise _Crash()

    real = memory.share_import_note
    monkeypatch.setattr(memory, "share_import_note", intent_then_crash)
    with pytest.raises(_Crash):
        service.confirm(preview["token"], _keys(preview), memory_project=target)
    _note(target, "one.md", "# One\n\nsomeone else's\n")
    monkeypatch.setattr(memory, "share_import_note", real)
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    note = result["items"][0]["memory"]["notes"][0]
    assert note["outcome"] == "failed" and "nothing was overwritten" in note["reason"]
    assert [n["body"] for n in memory.notes(target)] == ["# One\n\nsomeone else's"]


def test_a_lost_response_replays_and_another_project_is_a_new_selection(
    tmp_path, monkeypatch, install
):
    root = _second(tmp_path, monkeypatch)
    target, other = _project(root, "target"), _project(root, "other")
    data = _bundle([_memory_item(_n("One", "1"))])
    preview = service.preview(data)
    first = service.confirm(preview["token"], _keys(preview), memory_project=target)
    monkeypatch.setattr(memory, "share_import_note", lambda *a, **k: pytest.fail("twice"))
    assert service.confirm(preview["token"], _keys(preview), memory_project=target) == first
    with pytest.raises(service.ShareError) as err:
        service.confirm(preview["token"], _keys(preview), memory_project=other)
    assert err.value.extra["reason"] == "selection"
    assert memory.notes(other) == []


def test_imported_notes_carry_no_library_mark(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"))])
    preview = service.preview(data)
    service.confirm(preview["token"], _keys(preview), memory_project=target)
    assert service.imports() == {}  # the notes' writer is their provenance


# ---- specialist notebooks -------------------------------------------------------------------


def _specialist(title: str = "Implementer") -> str:
    library.create_skill("tdd", {"SKILL.md": "# tdd\n\nDo tdd well.\n"})
    lib = specialists.library_revisions()
    draft = {
        "title": title,
        "summary": "Builds a phase.",
        "skills": [{"id": "tdd", "revision": lib["tdd"]}],
        "engine": AGENT,
        "model": MODEL,
        "effort": None,
        "capacity": 2,
    }
    return pool.create(specialists.check_draft(draft))["id"]


def test_a_notebook_travels_with_its_specialist_into_the_new_specialists_notebook(
    tmp_path, monkeypatch, install
):
    src = _project(install, "alpha")
    sid = _specialist()
    memory.launch_env(
        "fixture:" + "b" * 8, projects.load()[src].folders[0], kind="terminal", specialist=sid
    )
    scope = memory.specialist_scope(src, sid)
    row = next(r for r in kinds.catalog() if r["kind"] == "notebook")
    assert row["id"] == scope and row["depends_on"] == [f"specialist:{sid}"]
    with pytest.raises(service.ShareError):
        _export(("notebook", scope))  # an empty notebook has nothing to send
    _seed(src)
    env = memory.launch_env(
        "fixture:" + "c" * 8, projects.load()[src].folders[0], kind="terminal", specialist=sid
    )
    note = Path(env[memory.ENV_SPECIALIST_INBOX]) / "mine.md"
    note.write_text("# My habit\n\nAlways write the test first.\n")
    old = note.stat().st_mtime - 10
    os.utime(note, (old, old))
    memory.ingest(scope)
    data = _export(("notebook", scope))
    bundle = fmt.read(data)
    assert [i.key for i in bundle.items] == ["skill:tdd", f"specialist:{sid}", f"notebook:{sid}"]
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    _specialist()  # the same id is taken here: the import makes `<sid>-2`
    preview = service.preview(data)
    plan = _plan(preview)
    new_sid = plan[f"specialist:{sid}"]["dest"]
    assert new_sid != sid
    # Without its specialist, the notebook can't come.
    without = [k for k in _keys(preview) if not k.startswith("specialist:")]
    statuses = service._status(
        state.read_json(f"preview-{preview['token']}.json"),
        {"selection": sorted(without), "entries": {}},
    )
    assert statuses[f"notebook:{sid}"]["status"] == "refused"
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    by_key = {i["key"]: i for i in result["items"]}
    assert by_key[f"notebook:{sid}"]["dest"] == memory.specialist_scope(target, new_sid)
    assert [n["name"] for n in memory.notes(memory.specialist_scope(target, new_sid))] == [
        "My habit"
    ]
    assert memory.notes(target) == []  # the file carried the notebook only


def test_a_notebook_needs_its_specialist_in_the_file(tmp_path, monkeypatch, install):
    _second(tmp_path, monkeypatch)
    item = _memory_item(_n("A", "b"), item_id="implementer")
    item["kind"] = "notebook"
    preview = service.preview(_bundle([item]))
    entry = preview["items"][0]
    assert entry["status"] == "refused"


def test_a_slug_taken_under_the_import_is_never_written_over(tmp_path, monkeypatch, install):
    """`INSERT`, never `OR REPLACE`: even a writer that took the slug without the scope lock (it
    can't happen through memory's own paths) is never overwritten; the commit fails instead."""
    import sqlite3

    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    note = _n("One", "the import's")

    def squat(slug: str) -> None:
        conn = sqlite3.connect(memory.root() / target / "state.db")
        with conn:
            conn.execute(
                "INSERT INTO notes(slug, name, description, type, body, digest, token, created,"
                " updated) VALUES(?, 'One', 'One', 'finding', 'theirs', 'x', 't', 0, 0)",
                (slug,),
            )
        conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        memory.share_import_note(target, note, writer=memory.share_writer("a" * 64), intent=squat)
    assert [n["body"] for n in memory.notes(target)] == ["theirs"]


# ---- the routes -----------------------------------------------------------------------------


def test_the_routes_carry_the_counts_and_the_chosen_project(auth_cfg, install, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    src, target = _project(install, "alpha"), _project(install, "target")
    _seed(src)
    _note(src, "cred.md", "token=ghp_" + "d" * 36 + "\n")
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    login = client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert login.status_code == 303
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    rows = {(r["kind"], r["id"]): r for r in client.get("/api/share/catalog").json()["items"]}
    assert rows[("memory", src)]["memory"] == {"notes": 2, "withheld": 0, "quarantined": 1}
    exported = client.post(
        "/api/share/export", json={"items": [{"kind": "memory", "id": src}]}, headers=headers
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["X-Share-Withheld-Notes"] == "0"
    assert exported.headers["X-Share-Quarantined-Notes"] == "1"
    assert b"ghp_" not in exported.content
    preview = client.post("/api/share/import/preview", content=exported.content, headers=headers)
    assert preview.status_code == 200, preview.text
    body = {"token": preview.json()["token"], "selection": ["memory:project"]}
    refused = client.post("/api/share/import", json=body, headers=headers)
    assert refused.status_code == 422 and refused.json()["reason"] == "memory_project"
    done = client.post(
        "/api/share/import", json={**body, "memory_project": target}, headers=headers
    )
    assert done.status_code == 200, done.text
    assert done.json()["items"][0]["memory"]["counts"]["created"] == 2
    assert _names(target) == ["Deploy", "Use typed ids"]


# ---- independent review round 1 -------------------------------------------------------------


@pytest.mark.parametrize("undo", ["delete", "clear"])
def test_a_note_removed_after_a_crash_is_never_brought_back_by_a_retry(
    tmp_path, monkeypatch, install, undo
):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"), _n("Two", "2"))])
    preview = service.preview(data)
    real = memory.share_import_note

    def commit_then_crash(scope, note, **kw):
        out = real(scope, note, **kw)
        if note["name"] == "One":
            raise _Crash()
        return out

    monkeypatch.setattr(memory, "share_import_note", commit_then_crash)
    with pytest.raises(_Crash):
        service.confirm(preview["token"], _keys(preview), memory_project=target)
    if undo == "delete":
        memory.delete_note(target, "one")
    else:
        memory.clear(target)
    monkeypatch.setattr(memory, "share_import_note", real)
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    one = next(n for n in result["items"][0]["memory"]["notes"] if n["file"] == "notes/0001.json")
    assert one["outcome"] == "failed" and "removed" in one["reason"]
    assert "One" not in _names(target)


def test_a_slug_that_would_hold_a_stored_secret_is_held_back(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    variables.create({"name": "t", "kind": "secret", "definition": {}, "value": WORDS})
    data = _bundle([_memory_item(_n("Purple-Elephant-Cabbage-Salad", "x"), _n("Fine", "ok"))])
    preview = service.preview(data)
    result = service.confirm(preview["token"], _keys(preview), memory_project=target)
    first = result["items"][0]["memory"]["notes"][0]
    assert first["outcome"] == "held" and first["slug"] is None
    assert memory.view(target)["quarantine"] == []
    assert _names(target) == ["Fine"]
    raw = json.dumps(result) + (state.root() / f"journal-{preview['token']}.json").read_text()
    assert WORDS not in raw


def test_a_damaged_memory_store_never_breaks_the_catalog_or_another_export(install):
    pid = _project(install, "alpha")
    _seed(pid)
    library.create_skill("tdd", {"SKILL.md": "# tdd\n\nDo tdd well.\n"})
    db = memory.root() / pid / "state.db"
    for extra in ("-wal", "-shm"):
        Path(str(db) + extra).unlink(missing_ok=True)
    db.write_bytes(b"not a database at all" * 100)
    rows = {(r["kind"], r["id"]): r for r in kinds.catalog()}
    assert rows[("skill", "tdd")]["exportable"] is True
    assert rows[("memory", pid)]["exportable"] is False and rows[("memory", pid)]["reason"]
    with pytest.raises(service.ShareError) as err:
        _export(("memory", pid))
    assert err.value.status == 422


def test_a_notebook_whose_notes_are_all_held_is_never_created(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    scope = memory.specialist_scope(target, "implementer")
    cred = "token=ghp_" + "e" * 36
    out = memory.share_import_note(
        scope, _n("Leaky", cred), writer=memory.share_writer("b" * 64), intent=lambda s: None
    )
    assert out["outcome"] == "held"
    assert not (memory.root() / scope).exists()


def test_a_retry_never_writes_into_a_project_archived_since(tmp_path, monkeypatch, install):
    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"), _n("Two", "2"))])
    preview = service.preview(data)
    real = memory.share_import_note

    def flaky(scope, note, **kw):
        if note["name"] == "Two":
            raise OSError(5, "disk")
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", flaky)
    service.confirm(preview["token"], _keys(preview), memory_project=target)
    projects.update(target, archived=True)
    monkeypatch.setattr(memory, "share_import_note", real)
    again = service.confirm(preview["token"], _keys(preview), memory_project=target)
    two = next(n for n in again["items"][0]["memory"]["notes"] if n["file"] == "notes/0002.json")
    assert two["outcome"] == "retry" and "archived" in two["reason"]
    assert _names(target) == ["One"]


def test_a_busy_secret_fence_refuses_the_memory_item_not_the_preview(
    tmp_path, monkeypatch, install
):
    from agent_sessions import secretfence

    _second(tmp_path, monkeypatch)

    def busy():
        raise secretfence.SecretFenceBusy("held")

    monkeypatch.setattr(memory, "_inventory_snapshot", busy)
    preview = service.preview(_bundle([_memory_item(_n("One", "1"))]))
    entry = preview["items"][0]
    assert entry["status"] == "refused" and "busy" in entry["reason"]


def test_an_unreadable_target_store_is_a_retriable_failure_never_a_500(
    tmp_path, monkeypatch, install
):
    """Independent review round 2, nit 1: the per-confirm project check reads the store; one
    that can't be read leaves each note retriable (the first confirm is a 422), never a 500."""
    import sqlite3

    root = _second(tmp_path, monkeypatch)
    target = _project(root, "target")
    data = _bundle([_memory_item(_n("One", "1"), _n("Two", "2"))])
    preview = service.preview(data)
    real = memory.share_import_note

    def flaky(scope, note, **kw):
        if note["name"] == "Two":
            raise OSError(5, "disk")
        return real(scope, note, **kw)

    monkeypatch.setattr(memory, "share_import_note", flaky)
    service.confirm(preview["token"], _keys(preview), memory_project=target)

    def unreadable(pid):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(memory, "enabled", unreadable)
    again = service.confirm(preview["token"], _keys(preview), memory_project=target)
    two = next(n for n in again["items"][0]["memory"]["notes"] if n["file"] == "notes/0002.json")
    assert two["outcome"] == "retry" and "can't be read" in two["reason"]
    other = service.preview(_bundle([_memory_item(_n("Three", "3"))]))
    with pytest.raises(service.ShareError) as err:
        service.confirm(other["token"], _keys(other), memory_project=target)
    assert err.value.status == 422 and err.value.extra["reason"] == "memory_project"
