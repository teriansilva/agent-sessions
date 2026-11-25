import contextlib
import fcntl
import os

import pytest

from agent_sessions import (
    memory,
    memory_index,
    projects,
    template_secrets,
)
from agent_sessions import (
    memory_service as service,
)


@pytest.fixture(autouse=True)
def inventory(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


def save(scope, title, body, origin):
    return service.submit(service.operator(), scope, title=title, body=body, origin=origin)


def test_one_index_serves_hq_and_scoped_project_queries(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    pid = projects.create("Example", folders=[str(work)]).id
    memory.set_enabled(pid, True)
    save(memory.HQ_SCOPE, "Research sources", "Primary sources accompany research.", "one")
    save(pid, "Revision checks", "The permission protocol checks saved revisions.", "two")
    assert not memory_index.sync()
    assert memory_index.status()["state"] == "keyword-only"
    op = service.search(service.operator(), "permission protocol")
    assert [h["scope"] for h in op["hits"]] == [pid]
    assert service.search(service.worker([pid]), "primary sources")["hits"] == []
    assert (
        service.search(service.operator(), "primary sources")["hits"][0]["scope"] == memory.HQ_SCOPE
    )


def test_delete_and_new_secrets_are_excluded_even_before_index_catches_up(monkeypatch):
    result = save(
        memory.HQ_SCOPE, "Research sources", "Use orchard-harbor-linen for the example.", "one"
    )
    memory_index.sync()
    assert service.search(service.operator(), "example")["hits"]
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["orchard-harbor-linen"])
    assert service.search(service.operator(), "example")["hits"] == []
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])
    memory.delete_note(memory.HQ_SCOPE, result["slug"])
    assert service.search(service.operator(), "example")["hits"] == []
    memory_index.sync()
    assert not memory_index.status()["purge_pending"]


def test_stale_note_references_and_updates_use_authority():
    result = save(memory.HQ_SCOPE, "Permission protocol", "Check the saved revision.", "one")
    memory_index.sync()
    hit = service.search(service.operator(), "permission")["hits"][0]
    memory.update_note(memory.HQ_SCOPE, result["slug"], {"body": "Use the new revision."})
    assert service.search(service.operator(), "permission")["hits"] == []
    with pytest.raises(memory.MemoryStoreError, match="changed"):
        service.read(service.operator(), memory.HQ_SCOPE, result["slug"], expected=hit)
    memory_index.sync()
    assert "new revision" in service.search(service.operator(), "permission")["hits"][0]["excerpt"]


def test_gc_waits_for_readers_and_keeps_durable_pending_status():
    result = save(memory.HQ_SCOPE, "Permission protocol", "Check the saved revision.", "one")
    memory_index.sync()
    fd = os.open(memory_index.root() / "gc.lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        memory.delete_note(memory.HQ_SCOPE, result["slug"])
        assert memory_index.sync()
        assert memory_index.status()["purge_pending"]
    finally:
        os.close(fd)
    memory_index.sync()
    assert not memory_index.status()["purge_pending"]


def test_query_path_does_not_scan_or_migrate_notebooks(monkeypatch):
    save(memory.HQ_SCOPE, "Permission protocol", "Check the saved revision.", "one")
    memory_index.sync()
    monkeypatch.setattr(
        memory, "_connect", lambda *a: pytest.fail("source migration on request path")
    )
    monkeypatch.setattr(memory, "notes", lambda *a: pytest.fail("source scan on request path"))
    assert service.search(service.operator(), "permission")["hits"]


def test_inventory_recheck_keeps_unchanged_vectors_and_purges_only_withheld_notes(monkeypatch):
    retained = save(memory.HQ_SCOPE, "Retained protocol", "Use original references.", "keep")
    removed = save(memory.HQ_SCOPE, "Private protocol", "orchard-withhold-marker", "remove")
    memory_index.sync()
    with contextlib.closing(memory_index._connect(write=True)) as conn:
        conn.execute("UPDATE documents SET dense_generation='accepted-fixture-generation'")
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["orchard-withhold-marker"])
    with memory._fenced():
        memory._recheck_scope(memory.HQ_SCOPE)
    memory_index.sync()
    with contextlib.closing(memory_index._connect()) as conn:
        rows = conn.execute("SELECT slug,dense_generation FROM documents").fetchall()
        assert [(r[0], r[1]) for r in rows] == [(retained["slug"], "accepted-fixture-generation")]
    assert not service.search(service.operator(), "private")["hits"]
    with pytest.raises(memory.MemoryStoreError):
        service.read(service.operator(), memory.HQ_SCOPE, removed["slug"])


def test_vector_deletes_are_batched_and_acknowledged_after_all_generations(monkeypatch):
    from types import SimpleNamespace

    memory_index.sync()
    (memory_index.root() / "lance").mkdir()
    calls = []
    fail = [True]

    def remove(name, condition):
        calls.append((name, condition))
        if name == "dense_old" and fail[0]:
            fail[0] = False
            raise OSError("interrupted before all generations were updated")

    monkeypatch.setattr(memory_index, "vector_support", lambda: {"available": True})
    monkeypatch.setattr(memory_index, "_database", object)
    monkeypatch.setattr(memory_index, "_table_names", lambda _: ["dense_current", "dense_old"])
    monkeypatch.setattr(
        memory_index,
        "_table",
        lambda db, name: SimpleNamespace(delete=lambda sql: remove(name, sql)),
    )
    with contextlib.closing(memory_index._connect(write=True)) as conn:
        conn.executemany(
            "INSERT INTO vector_deletes VALUES(?,?)",
            [(scope, f"note-{n}") for scope in ("p-one", "p-two") for n in range(64)],
        )
        with pytest.raises(OSError):
            memory_index._drain_deletes(conn)
        assert conn.execute("SELECT COUNT(*) FROM vector_deletes").fetchone()[0] == 128
        calls.clear()
        assert memory_index._drain_deletes(conn)
        assert conn.execute("SELECT COUNT(*) FROM vector_deletes").fetchone()[0] == 0
    assert len(calls) == 4  # two scoped batches × two vector generations, rather than 256 commits


def test_no_index_is_unavailable_not_an_empty_memory():
    with pytest.raises(memory.MemoryStoreError) as exc:
        service.search(service.operator(), "permission")
    assert exc.value.status == 503


def test_forgotten_text_is_physically_removed_from_sqlite_and_stays_gone_after_rebuild():
    marker = "orchard-linen-unique-removal-marker"
    saved = save(memory.HQ_SCOPE, "Removal fixture", marker, "remove")
    memory_index.sync()
    assert marker.encode() in (memory_index.root() / "state.db").read_bytes()
    memory.delete_note(memory.HQ_SCOPE, saved["slug"])
    memory_index.sync()
    for path in memory_index.root().glob("state.db*"):
        assert marker.encode() not in path.read_bytes()
    with memory_index._connect(write=True) as conn:
        memory_index._set(conn, "format", "1")
    memory_index.sync()
    assert not service.search(service.operator(), "removal marker")["hits"]


def test_native_backend_is_never_imported_for_keyword_search(monkeypatch):
    import builtins

    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name in {"lancedb", "pyarrow"}:
            pytest.fail("keyword search imported the native vector backend")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked)
    save(memory.HQ_SCOPE, "Research protocol", "Primary references.", "research")
    memory_index.sync()
    assert service.search(service.operator(), "references")["hits"]


def test_configured_query_never_creates_a_missing_vector_directory(monkeypatch):
    import builtins

    from agent_sessions import memory_settings

    save(memory.HQ_SCOPE, "Research protocol", "Use original references.", "research")
    memory_index.sync()
    service.search(service.operator(), "references")  # open existing read-only SQLite sidecars
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "fixture"})
    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name in {"lancedb", "pyarrow"}:
            pytest.fail("a query imported native code before any vector index existed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr(memory_index, "vector_support", lambda: {"available": True})
    before = {p: p.read_bytes() for p in memory.root().rglob("*") if p.is_file()}
    result = service.search(service.operator(), "references")
    assert result["hits"] and result["state"] == "indexing" and not result["complete"]
    assert not (memory_index.root() / "lance").exists()
    assert before == {p: p.read_bytes() for p in memory.root().rglob("*") if p.is_file()}


def test_native_connection_is_reused_but_directory_replacement_invalidates_it():
    if not memory_index.vector_support()["available"]:
        pytest.skip("native backend unavailable")
    memory_index.sync()
    first = memory_index._database(create=True)
    assert memory_index._database() is first
    directory = memory_index.root() / "lance"
    directory.rename(directory.with_name("retired-lance-fixture"))
    directory.mkdir(mode=0o700)
    assert memory_index._database() is not first
    memory_index._reset_database()


def test_cached_native_session_observes_another_writers_commit(monkeypatch):
    from datetime import timedelta

    from agent_sessions import memory_embeddings, memory_settings

    if not memory_index.vector_support()["available"]:
        pytest.skip("native backend unavailable")
    import lancedb

    monkeypatch.setattr(memory_embeddings, "embed", lambda *a, **kw: [[1.0, 0.0]])
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "fixture"})
    note = save(memory.HQ_SCOPE, "Original references", "Use primary sources.", "source")
    while memory_index.sync():
        pass
    assert service.search(service.operator(), "paraphrased discovery")["hits"]
    peer = lancedb.connect(memory_index.root() / "lance", read_consistency_interval=timedelta(0))
    try:
        peer.open_table("dense_" + memory_settings.stored()["revision"]).delete(
            "slug=" + memory_index._literal(note["slug"])
        )
    finally:
        peer.close()
    assert not service.search(service.operator(), "paraphrased discovery")["hits"]


def test_sparse_native_delete_rewrites_masks_and_removes_uncommitted_files(monkeypatch):
    import uuid

    from agent_sessions import memory_embeddings, memory_settings

    if not memory_index.vector_support()["available"]:
        pytest.skip("native backend unavailable")
    monkeypatch.setattr(
        memory_embeddings, "embed", lambda cfg, texts, **kw: [[1.0, 0.0] for _ in texts]
    )
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "fixture"})
    notes = [
        save(memory.HQ_SCOPE, f"Reference {n:02d}", "Original source.", f"source-{n}")
        for n in range(21)
    ]
    while memory_index.sync():
        pass
    name = "dense_" + memory_settings.stored()["revision"]
    orphan = (
        memory_index.root() / "lance" / (name + ".lance") / "data" / (str(uuid.uuid4()) + ".lance")
    )
    orphan.write_bytes(b"synthetic failed-transaction bytes")
    memory.delete_note(memory.HQ_SCOPE, notes[0]["slug"])
    with monkeypatch.context() as held:
        held.setattr(memory_index, "_collect_versions", lambda _: False)
        assert memory_index.sync()
    table = memory_index._table(memory_index._database(), name)
    assert table.stats()["num_deleted_rows"] == 1  # below the native default 10% threshold
    while memory_index.sync():
        pass
    table = memory_index._table(memory_index._database(), name)
    assert table.stats()["num_deleted_rows"] == 0
    assert len(table.list_versions()) == 1 and not orphan.exists()
    assert not memory_index.status()["purge_pending"]


def test_disabling_semantics_purges_vectors_without_losing_source_notes(monkeypatch):
    from agent_sessions import memory_embeddings, memory_settings

    if not memory_index.vector_support()["available"]:
        pytest.skip("native backend unavailable")
    monkeypatch.setattr(memory_embeddings, "embed", lambda *a, **kw: [[1.0, 0.0]])
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "fixture"})
    save(memory.HQ_SCOPE, "Original references", "Use primary sources.", "source")
    while memory_index.sync():
        pass
    assert (memory_index.root() / "lance").exists()
    memory_settings.save({"base_url": ""})
    while memory_index.sync():
        pass
    assert not (memory_index.root() / "lance").exists()
    assert service.search(service.operator(), "primary sources")["hits"]


def test_native_generation_replacement_and_forget_use_the_same_authority(monkeypatch):
    from agent_sessions import memory_embeddings, memory_settings

    support = memory_index.vector_support()
    if not support["available"]:
        pytest.skip(support["reason"])
    monkeypatch.setattr(
        memory_embeddings, "embed", lambda cfg, texts, **kw: [[1.0, 0.0] for _ in texts]
    )
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "first"})
    first_generation = memory_settings.stored()["revision"]
    saved = save(memory.HQ_SCOPE, "Source protocol", "Use primary references.", "one")
    while memory_index.sync():
        pass
    assert memory_index.status()["state"] == "ready"
    hit = service.search(service.operator(), "paraphrased retrieval")["hits"][0]
    assert hit["slug"] == saved["slug"] and hit["notebook"]["kind"] == "hq"
    memory_settings.save({"model": "second"})
    assert memory_index.status()["state"] == "indexing"
    while memory_index.sync():
        pass
    assert "dense_" + first_generation not in memory_index._table_names(memory_index._database())
    memory.delete_note(memory.HQ_SCOPE, saved["slug"])
    assert not service.search(service.operator(), "paraphrased retrieval")["hits"]
    while memory_index.sync():
        pass
    assert not memory_index.status()["purge_pending"]
    db = memory_index._database()
    for name in memory_index._table_names(db):
        assert not memory_index._table(db, name).search().to_list()


def test_conflicting_native_distributions_are_refused_before_import(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(memory_index.platform, "system", lambda: "Linux")
    monkeypatch.setattr(memory_index.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(memory_index.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.41.0")
    assert memory_index.vector_support() == {
        "available": False,
        "reason": "conflicting_native_packages",
    }


def test_polling_an_unchanged_camelcase_ann_index_does_not_rebuild_it():
    from types import SimpleNamespace

    support = memory_index.vector_support()
    if not support["available"]:
        pytest.skip(support["reason"])
    memory_index.sync()
    table = SimpleNamespace(
        count_rows=lambda: 256,
        list_indices=lambda: [
            SimpleNamespace(name="memory_id", index_type="BTree"),
            SimpleNamespace(name="vector_idx", index_type="IvfHnswFlat"),
        ],
        create_index=lambda *a, **k: pytest.fail("an unchanged ANN index was rebuilt"),
    )
    with contextlib.closing(memory_index._connect(write=True)) as conn:
        memory_index._set(conn, "ann_rows_fixture", 256)
        memory_index._set(conn, "dense_maintained_fixture", 256)
        memory_index._maintain_dense(conn, table, "fixture")


def test_keyword_ranks_each_whole_note_once_and_cites_late_matches():
    body = "Ordinary source paragraph. " * 115 + "Unique orchardlatekeyword."
    save(memory.HQ_SCOPE, "Large reference", body, "one")
    memory_index.sync()
    with memory_index._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 1
        assert conn.execute("SELECT COUNT(*) FROM search_content").fetchone()[0] == 1
        assert (
            conn.execute("SELECT doc FROM vocabulary WHERE term='orchardlatekeyword'").fetchone()[0]
            == 1
        )
    result = service.search(service.operator(), "orchardlatekeyword")
    assert "orchardlatekeyword" in result["hits"][0]["excerpt"]
    assert len(result["hits"][0]["excerpt"]) <= service.EXCERPT_MAX


def test_live_index_purge_removes_postings_and_wal_bytes_without_copying_the_whole_index(
    monkeypatch,
):
    alpha = save(memory.HQ_SCOPE, "First reference", "orchardremovalalpha", "a")
    beta = save(memory.HQ_SCOPE, "Other reference", "orchardremovalbeta", "b")
    save(memory.HQ_SCOPE, "Retained reference", "Other information stays.", "c")
    memory_index.sync()
    memory.delete_note(memory.HQ_SCOPE, alpha["slug"])
    memory.update_note(memory.HQ_SCOPE, beta["slug"], {"body": "A corrected body."})
    memory_index.sync()
    assert not memory_index.status()["purge_pending"]
    with memory_index._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert not conn.execute(
            "SELECT 1 FROM vocabulary WHERE term IN (?,?)",
            ("orchardremovalalpha", "orchardremovalbeta"),
        ).fetchone()
    for path in memory_index.root().glob("state.db*"):
        data = path.read_bytes()
        assert b"orchardremovalalpha" not in data and b"orchardremovalbeta" not in data
