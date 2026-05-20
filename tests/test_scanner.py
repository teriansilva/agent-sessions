from agent_sessions import scanner


def test_walks_live_and_archive(fake_jsonl):
    rows = scanner.scan(home=fake_jsonl)
    uuids = {r.uuid for r in rows}
    assert uuids == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
    }
    archived = {r.uuid for r in rows if r.archived}
    assert archived == {"44444444-4444-4444-4444-444444444444"}


def test_cwd_decoding_fallback(fake_jsonl):
    # These sessions have NO cwd field in their JSONL, so the scanner falls back
    # to decoding the dir name.
    rows = scanner.scan(home=fake_jsonl)
    cwds = {r.cwd for r in rows}
    assert "/home/user/claude/repo/a" in cwds
    assert "/tmp/other" in cwds


def test_jsonl_cwd_beats_lossy_dirname(fake_jsonl):
    # The example-app session's dir name encodes to ...-example-app-io, which
    # would wrongly decode to /home/user/claude/example-app/io. The JSONL
    # carries the real cwd, which must win.
    rows = scanner.scan(home=fake_jsonl)
    row = next(r for r in rows if r.uuid.startswith("55555555"))
    assert row.cwd == "/home/user/claude/example-app"
    # And the wrong decoded form must NOT appear anywhere.
    assert "/home/user/claude/example-app/io" not in {r.cwd for r in rows}


def test_first_user_message_string(fake_jsonl):
    rows = scanner.scan(home=fake_jsonl)
    msg = next(r.first_user_message for r in rows if r.uuid.startswith("11111111"))
    assert msg == "first message on repo-a"


def test_first_user_message_content_list(fake_jsonl):
    rows = scanner.scan(home=fake_jsonl)
    msg = next(r.first_user_message for r in rows if r.uuid.startswith("22222222"))
    assert msg == "second"


def test_short_uuid(fake_jsonl):
    rows = scanner.scan(home=fake_jsonl)
    assert all(len(r.short_uuid) == 8 for r in rows)


def test_scanned_cwds_set(fake_jsonl):
    rows = scanner.scan(home=fake_jsonl)
    assert scanner.scanned_cwds(rows) == {
        "/home/user/claude/repo/a",
        "/tmp/other",
        "/home/user/claude/example-app",
        "/home/user/claude/old",
    }


def test_ignores_non_uuid_files(fake_jsonl):
    # Drop a noise file alongside; scanner must skip it.
    junk = (
        fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a" / "not-a-uuid.jsonl"
    )
    junk.write_text("garbage")
    rows = scanner.scan(home=fake_jsonl)
    assert all(r.uuid != "not-a-uuid" for r in rows)
