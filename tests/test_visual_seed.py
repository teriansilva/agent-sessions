"""The visual-capture seeder (#96 Phase 2): deterministic multi-engine fake HOME, and a
hard refusal to seed over the operator's real home."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

# seed.py lives under web/visual (not importable as a package) — load it by path.
_SEED = Path(__file__).resolve().parents[1] / "web" / "visual" / "seed.py"
_spec = importlib.util.spec_from_file_location("visual_seed", _SEED)
seed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_mod)


def test_seed_writes_all_four_engines(tmp_path):
    home = tmp_path / "home"
    seed_mod.seed(home)

    # claude: 2 projects under ~/.claude/projects with a jsonl carrying cwd + a message
    claude = list((home / ".claude" / "projects").glob("*/*.jsonl"))
    assert len(claude) == 2
    assert any("Investigate the failing build" in f.read_text() for f in claude)

    # codex rollout
    assert list((home / ".codex" / "sessions").rglob("rollout-*.jsonl"))
    # gemini chat + project-map
    assert list((home / ".gemini" / "tmp").rglob("session-*.jsonl"))
    assert (home / ".gemini" / "tmp" / "project-map.json").is_file()
    # opencode db has a readable session row
    con = sqlite3.connect(home / ".local" / "share" / "opencode" / "opencode.db")
    try:
        rows = con.execute("SELECT id, directory FROM session").fetchall()
    finally:
        con.close()
    assert rows and rows[0][0].startswith("ses_")


def test_seed_refuses_the_real_home(monkeypatch, tmp_path):
    # Point Path.home() at tmp; seeding tmp itself (the "real home") must be refused.
    monkeypatch.setattr(seed_mod.Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(SystemExit):
        seed_mod.seed(tmp_path)
