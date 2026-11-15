"""The common notebook boundary, independent of a derived search backend (#2036)."""

import sqlite3

import pytest

from agent_sessions import memory, projects, template_secrets
from agent_sessions import memory_service as service


@pytest.fixture(autouse=True)
def clean_inventory(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = projects.create("Example", folders=[str(workspace)]).id
    memory.set_enabled(pid, True)
    return pid


def save(scope=memory.HQ_SCOPE, **kwargs):
    return service.submit(
        service.operator(),
        scope,
        title="Sources in research",
        body="Include primary sources.",
        origin="example-turn",
        **kwargs,
    )


def test_hq_uses_the_existing_notebook_store_and_is_not_a_project():
    assert not memory.valid_id(memory.HQ_SCOPE)
    assert service.capture_settings() == {"enabled": False, "revision": 0, "generation": 0}
    result = save(source="HQ conversation", quote="Please include the primary sources.")
    assert result["state"] == "saved"
    assert memory.notes(memory.HQ_SCOPE)[0]["body"] == "Include primary sources."
    view = memory.view(memory.HQ_SCOPE)
    assert view["project"]["name"] == "HQ" and view["enabled"]
    assert not view["notebooks"] and not view["instructions"]
    assert projects.load() == {}
    path = memory.root() / memory.HQ_SCOPE / "state.db"
    assert path.is_file()
    assert service.read(service.operator(), memory.HQ_SCOPE, result["slug"])["source"]["quote"]


def test_workers_never_gain_hq_or_another_project(project, tmp_path):
    result = save()
    principal = service.worker([project])
    with pytest.raises(memory.MemoryStoreError, match="offered"):
        service.read(principal, memory.HQ_SCOPE, result["slug"])
    with pytest.raises(memory.MemoryStoreError):
        service.worker([memory.HQ_SCOPE])
    other = tmp_path / "other"
    other.mkdir()
    other_pid = projects.create("Other", folders=[str(other)]).id
    with pytest.raises(memory.MemoryStoreError):
        service.worker([project, other_pid])
    assert [n["id"] for n in service.notebooks(principal)] == [project]


def test_same_admission_and_read_view_for_projects(project):
    result = save(project)
    principal = service.worker([project])
    assert service.read(principal, project, result["slug"])["body"] == "Include primary sources."
    assert (memory.root() / project / "notes" / (result["slug"] + ".md")).is_file()
    memory.set_enabled(project, False)
    assert service.read(principal, project, result["slug"])["body"]
    with pytest.raises(memory.MemoryStoreError, match="off"):
        service.submit(principal, project, title="Other", body="Other body.", origin="next-turn")


def test_delete_and_clear_beat_replayed_sources():
    first = save()
    memory.delete_note(memory.HQ_SCOPE, first["slug"])
    assert save()["state"] == "forgotten"
    service.submit(
        service.operator(), memory.HQ_SCOPE, title="Other", body="Other body.", origin="two"
    )
    memory.clear(memory.HQ_SCOPE)
    assert not memory.notes(memory.HQ_SCOPE)
    assert (
        service.submit(
            service.operator(),
            memory.HQ_SCOPE,
            title="Other",
            body="Other body.",
            origin="two",
        )["state"]
        == "forgotten"
    )
    with pytest.raises(memory.MemoryStoreError, match="changed"):
        service.submit(
            service.operator(),
            memory.HQ_SCOPE,
            title="Late",
            body="Old candidate.",
            origin="three",
            expected_generation=0,
        )


def test_source_note_and_source_receipt_rollback_together(monkeypatch):
    original = memory._commit_one

    def failing(conn, candidate, now):
        original(conn, candidate, now)
        raise OSError("injected crash")

    monkeypatch.setattr(memory, "_commit_one", failing)
    with pytest.raises(OSError):
        save()
    assert memory.notes(memory.HQ_SCOPE) == []
    with service.readonly(memory.HQ_SCOPE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_note_mutations_record_transactional_index_changes_and_stale_refs():
    result = save()
    before = service.read(service.operator(), memory.HQ_SCOPE, result["slug"])
    with service.readonly(memory.HQ_SCOPE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_changes").fetchone()[0] == 1
    memory.update_note(memory.HQ_SCOPE, result["slug"], {"body": "Use current primary sources."})
    with pytest.raises(memory.MemoryStoreError, match="changed"):
        service.read(service.operator(), memory.HQ_SCOPE, result["slug"], expected=before)
    after = service.read(service.operator(), memory.HQ_SCOPE, result["slug"])
    assert after["revision"] > before["revision"]
    assert after["content_digest"] != before["content_digest"]
    memory.delete_note(memory.HQ_SCOPE, result["slug"])
    with service.readonly(memory.HQ_SCOPE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_changes").fetchone()[0] == 3


def test_newly_registered_secret_withholds_notes_and_sources(monkeypatch):
    result = save(quote="Use the sample marker orchard-harbor-linen in the report.")
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["orchard-harbor-linen"])
    with pytest.raises(memory.MemoryStoreError, match="withheld"):
        service.read(service.operator(), memory.HQ_SCOPE, result["slug"])


def test_capture_off_and_policy_revision_cancel_old_candidates():
    settings = service.set_capture(True)
    assert settings["enabled"]
    service.set_capture(False)
    with pytest.raises(memory.MemoryStoreError, match="capture changed"):
        save(capture_revision=settings["revision"])
    assert not memory.notes(memory.HQ_SCOPE)


def test_rejected_notes_use_the_existing_quarantine(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["orchard-harbor-linen"])
    result = save(quote="orchard-harbor-linen")
    assert result["state"] == "quarantined"
    assert memory.notes(memory.HQ_SCOPE) == []
    with sqlite3.connect(memory.root() / memory.HQ_SCOPE / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 1


def test_empty_hq_clear_invalidates_a_pending_first_write():
    before = service.capture_settings()
    memory.clear(memory.HQ_SCOPE)
    assert service.capture_settings()["generation"] == before["generation"] + 1
    with pytest.raises(memory.MemoryStoreError, match="changed"):
        save(expected_generation=before["generation"])
    assert not memory.notes(memory.HQ_SCOPE)


def test_forget_blocks_changed_extraction_from_old_turn_but_allows_new_assertion():
    import uuid

    from agent_sessions import ask_store

    cid = ask_store.create()
    tid = str(uuid.uuid4())
    old_source = cid + "/" + tid
    first = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Sources in research",
        body="Use primary sources.",
        origin=service.source_origin(cid, tid, "Use primary sources."),
        source=old_source,
        quote="Use primary sources.",
        source_kind="explicit",
    )
    memory.delete_note(memory.HQ_SCOPE, first["slug"])
    replay = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Changed extraction",
        body="Prefer original references.",
        quote="Prefer original references.",
        origin="different-quote-origin",
        source=old_source,
        source_kind="capture",
    )
    assert replay["state"] == "forgotten"
    reassert = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Sources in research",
        body="Use primary sources.",
        quote="Use primary sources.",
        origin="new-operator-turn",
        source=cid + "/" + str(uuid.uuid4()),
        source_kind="explicit",
    )
    assert reassert["state"] == "saved"


def test_batch_resolution_keeps_each_note_identity_and_provenance():
    for index in range(3):
        service.submit(
            service.operator(),
            memory.HQ_SCOPE,
            title=f"Research fact {index}",
            body=f"Use reference number {index}.",
            quote=f"Original quote {index}.",
            origin=f"source-{index}",
            source=f"reference-{index}",
        )
    rows = service.read_many(
        service.operator(), memory.HQ_SCOPE, [f"research-fact-{i}" for i in range(3)]
    )
    assert len(rows) == 3
    for index, note in enumerate(rows):
        assert note["slug"] == f"research-fact-{index}"
        assert note["body"] == f"Use reference number {index}."
        assert note["source"]["source"] == f"reference-{index}"
        assert note["source"]["quote"] == f"Original quote {index}."


def test_unknown_source_format_refuses_writes_without_changing_notes():
    saved = save()
    with sqlite3.connect(memory.root() / memory.HQ_SCOPE / "state.db") as conn:
        memory._set_meta(conn, "memory_format", "999")
    with pytest.raises(memory.MemoryStoreError, match="newer"):
        memory.update_note(memory.HQ_SCOPE, saved["slug"], {"body": "Changed."})
    with sqlite3.connect(memory.root() / memory.HQ_SCOPE / "state.db") as conn:
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "Include primary sources."
