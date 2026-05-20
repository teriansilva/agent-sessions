"""Shared pytest fixtures.

Tests don't touch the real Zellij binary, the real Claude Code session
history, or the real sidecar — every fixture sets up isolated paths and
stub subprocess runners.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_sessions.auth import AuthConfig, hash_password


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    """Pretend the user's ``$HOME`` is an empty tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_SESSIONS_METADATA",
        str(tmp_path / ".config" / "agent-sessions" / "metadata.json"),
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
    # An archived one — same shape, different root.
    archive = tmp_home / ".claude" / "projects-archive" / "-home-user-claude-old"
    archive.mkdir(parents=True)
    (archive / "44444444-4444-4444-4444-444444444444.jsonl").write_text(
        '{"type":"user","message":{"content":"archived session"}}\n'
    )
    return tmp_home


@pytest.fixture
def auth_cfg(monkeypatch) -> AuthConfig:
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", hash_password("hunter2"))
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://terminal.example.com")
    return AuthConfig.from_env()


@pytest.fixture
def stub_zellij():
    """A subprocess.run stub for zellij; records calls + replies as if no tabs exist."""
    runner = MagicMock()
    runner.return_value.returncode = 0
    runner.return_value.stdout = ""
    return runner
