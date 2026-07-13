import json
import os
from datetime import datetime
from pathlib import Path

from agent_sessions import metadata, scanner


def _iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


# ---- last_activity_at / derive_last_activity (#525) ---------------------------
# The Update-order sort key must be the last real conversation turn, NOT the raw JSONL mtime,
# which a bare `claude --resume` bumps by appending timestamp-less app-state records.

# Records claude appends on a bare resume — all timestamp-less, none are activity.
_RESUME_APPSTATE = [
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "mode", "mode": "normal"},
    {"type": "ai-title", "aiTitle": "A title"},
    {"type": "last-prompt", "lastPrompt": "p"},
]


def test_last_activity_at_uses_newest_conversation_record(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "timestamp": "2026-01-02T00:00:00Z",
                "message": {"content": "yo"},
            },
            *_RESUME_APPSTATE,  # trailing resume records must be ignored
        ],
    )
    assert scanner.last_activity_at(f) == _iso("2026-01-02T00:00:00Z")


def test_last_activity_at_none_when_no_conversation_record(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, _RESUME_APPSTATE)  # app-state only, no user/assistant, no timestamps
    assert scanner.last_activity_at(f) is None


def test_last_activity_at_ignores_timestamped_appstate(tmp_path):
    """A `system` record is timestamped but is NOT a conversation turn (it can fire at resume, e.g.
    `scheduled_task_fire`). It must not count — last activity stays the earlier user turn (#525)."""
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}},
            {
                "type": "system",
                "subtype": "scheduled_task_fire",
                "timestamp": "2026-05-05T00:00:00Z",
            },
        ],
    )
    assert scanner.last_activity_at(f) == _iso("2026-01-01T00:00:00Z")


def test_last_activity_at_skips_malformed_tail_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps(
            {"type": "assistant", "timestamp": "2026-01-02T00:00:00Z", "message": {"content": "ok"}}
        )
        + "\n"
        + "this is not json {{{\n"
        + '{"partial": \n'
    )
    assert scanner.last_activity_at(f) == _iso("2026-01-02T00:00:00Z")


def test_last_activity_at_survives_many_trailing_appstate_records(tmp_path):
    """Anti-regression: however many timestamp-less resume records pile up after the last real turn
    (repeated idle opens), the expanding backward read must still find it — never silently fall back
    to the (bumped) file mtime. A tiny initial window forces the expansion path."""
    f = tmp_path / "s.jsonl"
    records = [
        {"type": "assistant", "timestamp": "2026-01-02T00:00:00Z", "message": {"content": "ok"}}
    ]
    records += [{"type": "mode", "mode": "normal", "n": i} for i in range(2000)]
    _write_jsonl(f, records)
    # Initial 256-byte window lands entirely inside the trailing block → only the expanding read
    # reaching further back can find the real turn.
    assert scanner.last_activity_at(f, tail_bytes=256) == _iso("2026-01-02T00:00:00Z")


def test_last_activity_at_empty_or_missing_file(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert scanner.last_activity_at(empty) is None
    assert scanner.last_activity_at(tmp_path / "nope.jsonl") is None


def test_derive_last_activity_prefers_record_over_mtime(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [{"type": "assistant", "timestamp": "2026-01-02T00:00:00Z", "message": {"content": "ok"}}],
    )
    os.utime(f, (2_000_000_000, 2_000_000_000))  # file mtime far in the future
    assert scanner.derive_last_activity(f, f.stat()) == _iso("2026-01-02T00:00:00Z")


def test_derive_last_activity_falls_back_to_fs_mtime(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [{"type": "user", "message": {"content": "no ts"}}])  # no timestamp anywhere
    os.utime(f, (1_234_567_890, 1_234_567_890))
    assert scanner.derive_last_activity(f, f.stat()) == 1_234_567_890.0


def test_dedup_prefers_archive_when_uuid_in_both_trees(fake_jsonl):
    """#194: a uuid present in BOTH the live and archive trees (an archived session whose
    JSONL was recreated under projects/ by a still-running agent) must collapse to a SINGLE
    archived row — never appear in both scopes / bounce back into the active list."""
    uuid = "11111111-1111-1111-1111-111111111111"  # starts live in the fixture
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    arch = fake_jsonl / ".claude" / "projects-archive" / "-home-user-claude-repo-a"
    arch.mkdir(parents=True, exist_ok=True)
    # Put the same uuid in the archive tree while the live copy still exists.
    (arch / f"{uuid}.jsonl").write_text((proj / f"{uuid}.jsonl").read_text())

    rows = [r for r in scanner.scan(home=fake_jsonl) if r.uuid == uuid]
    assert len(rows) == 1  # not duplicated across trees
    assert rows[0].archived is True  # the archive copy wins


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
    # The demoapp.io session's dir name encodes to ...-demoapp-io, which
    # would wrongly decode to /home/user/claude/demoapp/io. The JSONL
    # carries the real cwd, which must win.
    rows = scanner.scan(home=fake_jsonl)
    row = next(r for r in rows if r.uuid.startswith("55555555"))
    assert row.cwd == "/home/user/claude/demoapp.io"
    # And the wrong decoded form must NOT appear anywhere.
    assert "/home/user/claude/demoapp/io" not in {r.cwd for r in rows}


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
        "/home/user/claude/demoapp.io",
        "/home/user/claude/old",
    }


def test_pickable_projects_includes_claude_subdirs(fake_jsonl):
    # ~/claude subdirs should appear in the picker even without sessions.
    (fake_jsonl / "claude" / "brand-new-proj").mkdir(parents=True)
    picks = scanner.pickable_projects(home=fake_jsonl)
    assert str(fake_jsonl / "claude" / "brand-new-proj") in picks
    # scanned cwds still present
    assert "/tmp/other" in picks


def test_pickable_projects_rejects_symlink_outside_claude(fake_jsonl):
    claude = fake_jsonl / "claude"
    claude.mkdir(parents=True, exist_ok=True)
    outside = fake_jsonl / "outside-secret"
    outside.mkdir()
    (claude / "evil").symlink_to(outside)  # symlink pointing OUT of ~/claude
    picks = scanner.pickable_projects(home=fake_jsonl)
    # the symlink's real path is outside ~/claude → must be rejected
    assert str(outside) not in picks
    assert str(claude / "evil") not in picks


def test_pickable_projects_skips_hidden_dirs(fake_jsonl):
    claude = fake_jsonl / "claude"
    (claude / ".claude").mkdir(parents=True, exist_ok=True)
    (claude / ".git").mkdir(parents=True, exist_ok=True)
    (claude / "RealProj").mkdir(parents=True, exist_ok=True)
    picks = scanner.pickable_projects(home=fake_jsonl)
    assert str(claude / "RealProj") in picks
    assert str(claude / ".claude") not in picks
    assert str(claude / ".git") not in picks


# ---- root-scoped + exclusion-filtered discovery (#465) ------------------------


def _picks(home, sessions=None, roots=None, exclusions=None):
    return scanner.pickable_projects(
        home=home, sessions=sessions, roots=roots, exclusions=exclusions
    )


def test_pickable_projects_empty_roots_unchanged(fake_jsonl):
    # roots=None and roots=[] must both behave like today (session cwds ∪ ~/claude subdirs).
    (fake_jsonl / "claude" / "fresh").mkdir(parents=True)
    base = _picks(fake_jsonl)
    assert _picks(fake_jsonl, roots=[]) == base
    assert "/tmp/other" in base  # out-of-claude session cwd kept when unscoped
    assert str(fake_jsonl / "claude" / "fresh") in base


def test_pickable_projects_root_scoped_drops_out_of_root_cwds(fake_jsonl):
    # With a root configured, a session cwd OUTSIDE the root (/tmp/other) drops; ones under it stay.
    # The fixture session cwds are the literal /home/user/claude/* (not under tmp_home); scope is a
    # pure boundary test, so the root is that literal path (existing-dir filtering is
    # project_dirs').
    sessions = list(scanner.scan(home=fake_jsonl))
    picks = _picks(fake_jsonl, sessions=sessions, roots=["/home/user/claude"])
    assert "/home/user/claude/repo/a" in picks  # under the root → kept
    assert "/tmp/other" not in picks  # outside the root → dropped


def test_pickable_projects_root_subdirs_surface(fake_jsonl):
    # A fresh (session-less) immediate sub-dir of a configured root must surface.
    code = fake_jsonl / "code"
    (code / "brand-new").mkdir(parents=True)
    picks = _picks(fake_jsonl, sessions=[], roots=[str(code)])
    assert str(code / "brand-new") in picks


def test_pickable_projects_root_subdir_scan_rejects_hidden_and_symlink_out(fake_jsonl):
    code = fake_jsonl / "code"
    (code / "Real").mkdir(parents=True)
    (code / ".hidden").mkdir()
    outside = fake_jsonl / "outside-secret"
    outside.mkdir()
    (code / "evil").symlink_to(outside)  # symlink pointing OUT of the root
    picks = _picks(fake_jsonl, sessions=[], roots=[str(code)])
    assert str(code / "Real") in picks
    assert str(code / ".hidden") not in picks
    assert str(outside) not in picks
    assert str(code / "evil") not in picks


def test_pickable_projects_exclusion_drops_under_root(fake_jsonl):
    code = fake_jsonl / "code"
    (code / "keep").mkdir(parents=True)
    (code / "scratch").mkdir()
    picks = _picks(fake_jsonl, sessions=[], roots=[str(code)], exclusions=[str(code / "scratch")])
    assert str(code / "keep") in picks
    assert str(code / "scratch") not in picks


def test_pickable_projects_exclusion_applies_when_unscoped(fake_jsonl):
    # Exclusions drop a cwd even with NO roots configured (the empty-roots path).
    sessions = list(scanner.scan(home=fake_jsonl))
    picks = _picks(fake_jsonl, sessions=sessions, roots=[], exclusions=["/tmp"])
    assert "/tmp/other" not in picks
    assert "/home/user/claude/repo/a" in picks


def test_ignores_non_uuid_files(fake_jsonl):
    # Drop a noise file alongside; scanner must skip it.
    junk = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a" / "not-a-uuid.jsonl"
    junk.write_text("garbage")
    rows = scanner.scan(home=fake_jsonl)
    assert all(r.uuid != "not-a-uuid" for r in rows)


# ---- is_ephemeral_cwd (#452) --------------------------------------------------

_FAKE_HOME = Path("/home/user")


def test_is_ephemeral_cwd_none_and_empty():
    assert scanner.is_ephemeral_cwd(None, home=_FAKE_HOME) is False
    assert scanner.is_ephemeral_cwd("", home=_FAKE_HOME) is False


def test_is_ephemeral_cwd_act_root_and_descendant():
    assert scanner.is_ephemeral_cwd("/home/user/.cache/act", home=_FAKE_HOME) is True
    assert (
        scanner.is_ephemeral_cwd("/home/user/.cache/act/deadbeef/hostexecutor", home=_FAKE_HOME)
        is True
    )


def test_is_ephemeral_cwd_real_project_not_filtered(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    # Contains the letters "act" but never as a ``.cache``/``act`` component pair.
    assert scanner.is_ephemeral_cwd("/home/user/projects/react", home=_FAKE_HOME) is False
    assert scanner.is_ephemeral_cwd("/home/user/claude", home=_FAKE_HOME) is False
    # A sibling cache dir named ``actually`` must not match the exact ``act`` component.
    assert scanner.is_ephemeral_cwd("/home/user/.cache/actually/x", home=_FAKE_HOME) is False


def test_is_ephemeral_cwd_honors_xdg_cache_home(monkeypatch):
    # A relocated cache whose path has no literal ``.cache`` segment (so only the
    # env-derived root match can catch it).
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdgcache")
    assert scanner.is_ephemeral_cwd("/tmp/xdgcache/act/h/hostexecutor", home=_FAKE_HOME) is True
    assert scanner.is_ephemeral_cwd("/tmp/xdgcache/something", home=_FAKE_HOME) is False


def test_is_ephemeral_cwd_matches_foreign_home_basis(monkeypatch):
    """#452 (Hermes): a cwd recorded by a CI process under a DIFFERENT home is still
    caught via the ``.cache``/``act`` component match, even when XDG_CACHE_HOME points
    somewhere else at scan time."""
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdgcache")
    assert (
        scanner.is_ephemeral_cwd("/home/ci-runner/.cache/act/h/hostexecutor", home=_FAKE_HOME)
        is True
    )


def test_is_ephemeral_cwd_is_lexical_no_fs(monkeypatch):
    # ``..`` that resolves OUT of the cache is not ephemeral — normalization is
    # lexical (``normpath``), never a filesystem stat, so the path need not exist.
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert (
        scanner.is_ephemeral_cwd("/home/user/.cache/act/../../realproj", home=_FAKE_HOME) is False
    )


# ---- fallback-title normalization + wrapper skip (#670) -----------------------


def test_title_candidate_first_line_capped():
    # Canonical home is metadata (#670 / Hermes on PR #672): display-time-only normalization.
    assert metadata.title_candidate("  hello\nworld  ") == "hello"
    assert metadata.title_candidate("x" * 300) == "x" * metadata.TITLE_FALLBACK_MAX
    assert metadata.title_candidate("   \n  ") == ""
    assert metadata.title_candidate("") == ""


def test_first_user_message_skips_local_command_wrappers(tmp_path):
    # Claude records local-command scaffolding as ordinary user messages; consecutive
    # wrapper records are skipped until the real prompt (#670).
    p = tmp_path / "66666666-6666-6666-6666-666666666666.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "cwd": "/x",
                "message": {
                    "content": (
                        "<local-command-caveat>Caveat: local commands below."
                        "</local-command-caveat>"
                    )
                },
            },
            {
                "type": "user",
                "message": {
                    "content": (
                        "<command-name>/model</command-name>"
                        "\n<command-message>model</command-message>"
                    )
                },
            },
            {"type": "user", "message": {"content": "the real question"}},
        ],
    )
    cwd, first = scanner._read_session_meta(p)
    assert cwd == "/x"
    assert first == "the real question"


def test_first_user_message_wrapper_only_yields_empty(tmp_path):
    p = tmp_path / "77777777-7777-7777-7777-777777777777.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "cwd": "/x",
                "message": {"content": "<command-name>/clear</command-name>"},
            }
        ],
    )
    cwd, first = scanner._read_session_meta(p)
    assert cwd == "/x"
    assert first == ""
