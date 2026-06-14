"""Shared pytest fixtures.

Tests don't touch the real Claude Code session history or the real sidecar —
every fixture sets up isolated paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_sessions.auth import AuthConfig, hash_password


@pytest.fixture(autouse=True)
def _isolate_scrollback(tmp_path, monkeypatch) -> None:
    """Point the persisted-scrollback cache (#206) at a per-test tmp dir and reset the
    in-memory ring state, so ``_buffer_append`` never writes to the real ``$HOME`` and no
    cache bytes leak between tests (``_SCROLLBACK_DIR`` is captured at import from
    ``Path.home()``, so setting ``$HOME`` later is not enough)."""
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_SCROLLBACK_DIR", tmp_path / "scrollback-cache")
    for d in (
        webterm._BUFFERS,
        webterm._TOTALS,
        webterm._LAST_OUTPUT_AT,
        webterm._SUPPRESS_OUTPUT_UNTIL,
        webterm.scrollback._MODES,
        webterm.scrollback._MODE_CARRY,
    ):
        d.clear()
    webterm._LOADED_FROM_DISK.clear()


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    """Pretend the user's ``$HOME`` is an empty tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_SESSIONS_METADATA",
        str(tmp_path / ".config" / "agent-sessions" / "metadata.json"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_PROJECTS",
        str(tmp_path / ".config" / "agent-sessions" / "projects.json"),
    )
    return tmp_path


@pytest.fixture
def fake_jsonl(tmp_home) -> Path:
    """Lay down a couple of Claude Code-shaped JSONLs under tmp_home/.claude/projects/."""
    projects = tmp_home / ".claude" / "projects"
    proj1 = projects / "-home-user-claude-repo-a"
    proj2 = projects / "-tmp-other"
    proj1.mkdir(parents=True)
    proj2.mkdir(parents=True)
    (proj1 / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        '{"type":"user","message":{"content":"first message on repo-a"}}\n'
    )
    (proj1 / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        '{"type":"user","message":{"content":[{"type":"text","text":"second"}]}}\n'
    )
    (proj2 / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        '{"type":"user","message":{"content":"hello tmp"}}\n'
    )
    # A dotted-path project: the dir name is lossy (demoapp.io and
    # demoapp/io both encode to ...-demoapp-io), but the JSONL carries the
    # real cwd. The scanner must prefer the JSONL cwd over the decode.
    proj3 = projects / "-home-user-claude-demoapp-io"
    proj3.mkdir(parents=True)
    (proj3 / "55555555-5555-5555-5555-555555555555.jsonl").write_text(
        '{"type":"user","cwd":"/home/user/claude/demoapp.io",'
        '"message":{"content":"dotted path session"}}\n'
    )
    # An archived one — same shape, different root.
    archive = tmp_home / ".claude" / "projects-archive" / "-home-user-claude-old"
    archive.mkdir(parents=True)
    (archive / "44444444-4444-4444-4444-444444444444.jsonl").write_text(
        '{"type":"user","message":{"content":"archived session"}}\n'
    )
    return tmp_home


@pytest.fixture
def auth_cfg(tmp_path, monkeypatch) -> AuthConfig:
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", hash_password("hunter2"))
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    # Isolate the 2FA store (#116) to a tmp path — otherwise twofactor.default_path()
    # resolves under the real HOME and the auth/login tests read the operator's real,
    # possibly-enabled 2fa.json, making the suite outcome host-dependent (Hermes #140).
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    return AuthConfig.from_env()


# opencode session ids in the fixture (≥1 top-level, 1 archived, 1 fork to skip).
OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"
OC_FORK = "ses_ffffffffffffffffffffffff"


@pytest.fixture
def opencode_db(tmp_home, monkeypatch) -> Path:
    """A minimal opencode SQLite DB (only the columns OpenCodeProvider reads).

    Two top-level sessions (one archived) + one fork (``parent_id`` set, must be
    skipped). ``time_updated`` is epoch **milliseconds**, like the real DB.
    """
    db = tmp_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
        "time_updated INTEGER, time_archived INTEGER)"
    )
    con.executemany(
        "INSERT INTO session (id, parent_id, directory, title, time_updated, time_archived) "
        "VALUES (?,?,?,?,?,?)",
        [
            (OC_TOP, None, "/home/user/claude", "OC top one", 1777460564154, None),
            (OC_ARCHIVED, None, "/tmp/other", "OC archived", 1777300000000, 1777400000000),
            (OC_FORK, OC_TOP, "/home/user/claude", "OC fork skip", 1777460564999, None),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    return db
