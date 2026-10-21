from __future__ import annotations

import pytest

from agent_sessions import archive, scanner


def test_archive_moves_jsonl_and_scanner_reflects_it(fake_jsonl):
    uuid = "11111111-1111-1111-1111-111111111111"
    # live before
    assert any(r.uuid == uuid and not r.archived for r in scanner.scan(home=fake_jsonl))

    archive.archive(uuid, home=fake_jsonl)

    rows = scanner.scan(home=fake_jsonl)
    row = next(r for r in rows if r.uuid == uuid)
    assert row.archived is True
    # file physically moved into projects-archive, preserving the dir name
    moved = (
        fake_jsonl / ".claude" / "projects-archive" / "-home-user-claude-repo-a" / f"{uuid}.jsonl"
    )
    assert moved.is_file()


def test_unarchive_round_trips(fake_jsonl):
    uuid = "44444444-4444-4444-4444-444444444444"  # starts archived in the fixture
    assert any(r.uuid == uuid and r.archived for r in scanner.scan(home=fake_jsonl))

    archive.unarchive(uuid, home=fake_jsonl)
    row = next(r for r in scanner.scan(home=fake_jsonl) if r.uuid == uuid)
    assert row.archived is False


def test_archive_unknown_uuid_raises(fake_jsonl):
    with pytest.raises(archive.ArchiveError):
        archive.archive("99999999-9999-9999-9999-999999999999", home=fake_jsonl)


def test_archive_rejects_bad_uuid(fake_jsonl):
    with pytest.raises(archive.ArchiveError):
        archive.archive("../../etc/passwd", home=fake_jsonl)
