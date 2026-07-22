"""Transcript-ownership probe (#631).

Pins the two signals — an open fd on ``<uuid>.jsonl`` (real fd against real ``/proc``) and a
``claude`` argv referencing the transcript — plus the fail-open contract. The argv matcher is a
pure helper (a test can't rewrite its own argv), so it is exercised directly.
"""

from __future__ import annotations

from agent_sessions import transcript_owner

_UUID = "11111111-1111-1111-1111-111111111111"
_JSONL = f"{_UUID}.jsonl"


# ---- argv matcher (pure) ------------------------------------------------------


def test_argv_matches_session_id_flag():
    # A background-agent fork launches with `--session-id <new>` — its OWN id.
    assert transcript_owner._cmdline_owns(
        ["/usr/bin/claude", "--session-id", _UUID, "--fork-session"], _UUID, _JSONL
    )


def test_argv_matches_resume_path_form():
    # A fork resumes its parent by the PATH form `--resume …/<uuid>.jsonl`.
    assert transcript_owner._cmdline_owns(
        ["claude", "--resume", f"/home/u/.claude/projects/enc/{_JSONL}"], _UUID, _JSONL
    )


def test_argv_bare_resume_uuid_is_not_a_match():
    # This app resumes with a BARE `--resume <uuid>` (never a path) — must NOT self-match, or a
    # legitimate app-launched claude would look like a background agent.
    assert not transcript_owner._cmdline_owns(["claude", "--resume", _UUID], _UUID, _JSONL)


def test_argv_requires_a_claude_process():
    # The flag pattern alone isn't enough — some unrelated program carrying `--resume <path>`
    # must not trip the probe.
    assert not transcript_owner._cmdline_owns(["vim", "--resume", f"/x/{_JSONL}"], _UUID, _JSONL)


def test_argv_other_uuid_does_not_match():
    other = "22222222-2222-2222-2222-222222222222"
    assert not transcript_owner._cmdline_owns(["claude", "--session-id", other], _UUID, _JSONL)


# ---- fd matcher (pure) --------------------------------------------------------


def test_fd_target_matches_by_basename_across_trees():
    assert transcript_owner._fd_target_owns(f"/home/u/.claude/projects/enc/{_JSONL}", _JSONL)
    assert transcript_owner._fd_target_owns(
        f"/home/u/.claude/projects-archive/enc/{_JSONL}", _JSONL
    )


def test_fd_target_deleted_suffix_is_stripped():
    # A file moved out from under a still-open fork reads back as "<path> (deleted)".
    assert transcript_owner._fd_target_owns(f"/x/{_JSONL} (deleted)", _JSONL)


def test_fd_target_other_file_no_match():
    other = "/x/22222222-2222-2222-2222-222222222222.jsonl"
    assert not transcript_owner._fd_target_owns(other, _JSONL)


# ---- integration: a real open fd against real /proc ---------------------------


def test_owned_true_while_a_process_holds_the_fd(tmp_path):
    jsonl = tmp_path / _JSONL
    jsonl.write_text("{}\n")
    with jsonl.open("r"):  # THIS process now holds an open handle on <uuid>.jsonl
        assert transcript_owner.transcript_is_owned(_UUID) is True
    # fd released → no owner
    assert transcript_owner.transcript_is_owned(_UUID) is False


def test_no_owner_for_unheld_uuid():
    assert transcript_owner.transcript_is_owned("deadbeef-dead-dead-dead-deaddeaddead") is False


def test_fail_open_on_probe_error(caplog):
    # An unreadable /proc root → allow (fail-open), never raise.
    import logging

    with caplog.at_level(logging.WARNING):
        assert transcript_owner.transcript_is_owned(_UUID, proc_root="/nonexistent-proc") is False
    assert any("fail-open" in r.message for r in caplog.records)
