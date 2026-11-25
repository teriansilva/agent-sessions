"""Durable source backups and a real interpreter restart during initial backfill (#2036)."""

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from agent_sessions import memory, memory_index, template_secrets
from agent_sessions import memory_service as service


@pytest.fixture(autouse=True)
def inventory(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])


def save(title, body, origin):
    return service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title=title,
        body=body,
        origin=origin,
        source="Synthetic source",
        quote=body,
    )


def test_backfill_resumes_in_a_new_process_and_replays_changes_behind_its_cursor():
    for number in range(memory_index.BATCH_NOTES + 2):
        save(f"Protocol {number:03d}", "Original orchard reference.", f"source-{number}")
    assert memory_index.sync()  # the cursor and its initial change watermark are durable
    first = service.search(service.operator(), "protocol 000")["hits"][0]
    memory.update_note(memory.HQ_SCOPE, first["slug"], {"body": "Corrected walnut reference."})
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import json
from agent_sessions import memory_index, memory_service, template_secrets
template_secrets.redaction_values = lambda: []
while memory_index.sync():
    pass
print(json.dumps(memory_service.search(memory_service.operator(), 'walnut')))
""",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=dict(os.environ),
    )
    result = json.loads(child.stdout)
    assert result["hits"][0]["slug"] == first["slug"]
    assert "Corrected walnut" in result["hits"][0]["excerpt"]
    assert result["hits"][0]["revision"] > first["revision"]
    assert not memory_index.status()["source_backlog"]


def test_consistent_source_backup_restores_citations_receipts_and_forget(tmp_path):
    retained = save("Retained reference", "Original source quotation.", "retained")
    forgotten = save("Forgotten reference", "This information was forgotten.", "forgotten")
    memory.delete_note(memory.HQ_SCOPE, forgotten["slug"])
    memory_index.sync()
    original = service.search(service.operator(), "retained")["hits"][0]
    backup_path = tmp_path / "source-backup.db"
    # There are no concurrent writers in this fixture: the source, receipts and tombstones
    # form one consistent snapshot. No ordinary copy of an open WAL database is used.
    with (
        service.readonly(memory.HQ_SCOPE) as source,
        contextlib.closing(sqlite3.connect(backup_path)) as backup,
    ):
        source.backup(backup)
    memory.update_note(memory.HQ_SCOPE, retained["slug"], {"body": "Later divergent state."})
    with (
        contextlib.closing(sqlite3.connect(backup_path)) as backup,
        contextlib.closing(memory._connect(memory.HQ_SCOPE)) as restored,
    ):
        backup.backup(restored)
    shutil.rmtree(memory_index.root())  # only this test's isolated derived cache
    while memory_index.sync():
        pass
    recovered = service.search(service.operator(), "retained")["hits"][0]
    assert recovered["href"] == original["href"]
    assert recovered["revision"] == original["revision"]
    assert recovered["source"]["quote"] == "Original source quotation."
    assert not service.search(service.operator(), "forgotten")["hits"]
    assert (
        save("Forgotten reference", "This information was forgotten.", "forgotten")["state"]
        == "forgotten"
    )
