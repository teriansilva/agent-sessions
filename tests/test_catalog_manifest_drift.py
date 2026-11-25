"""A catalog recipe never drops a fact its first-party manifest declares (#1965).

An agent installed or updated from the agent catalog runs on the CATALOG's manifest (pinned in
`plugin-state/roster.json`), not on `first_party/<id>/plugin.toml`. When a recipe silently lacks a
table the first-party manifest has, the installed agent loses that capability with no error:
catalog-installed claude, codex and opencode lost `[tools]` (every BattleLab MCP tool: memory,
secrets, HQ report, shelf, artifacts) and claude lost `[memory]` (its auto-memory source).

Rule: every first-party table except the install-specific `identity` (the version) and `binary`
(where the catalog installs it) must be present in the recipe with the same value. A recipe may
add install-only tables (`install`, `probe`, `signin`, `verify`).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECIPES = sorted((ROOT / "release/recipes/linux-x64").glob("*.json"))
FIRST_PARTY = ROOT / "src/agent_sessions/plugins/first_party"
INSTALL_SPECIFIC = frozenset({"identity", "binary"})


def test_there_are_recipes_to_check():
    assert len(RECIPES) >= 5


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.stem)
def test_a_recipe_carries_every_first_party_fact(recipe: Path):
    first_party = FIRST_PARTY / recipe.stem / "plugin.toml"
    if not first_party.exists():
        pytest.skip("no first-party manifest for this agent")
    fp = tomllib.loads(first_party.read_text())
    rec = json.loads(recipe.read_text())["manifest"]
    missing = sorted(k for k in fp if k not in INSTALL_SPECIFIC and k not in rec)
    assert not missing, f"{recipe.stem}: the recipe drops {missing} (installs would lose them)"
    differs = sorted(k for k in fp if k not in INSTALL_SPECIFIC and fp[k] != rec[k])
    assert not differs, f"{recipe.stem}: the recipe disagrees with first-party on {differs}"


def test_the_bundled_catalog_carries_the_tool_channels():
    """What an install actually reads: the release-bundled catalog generated from the recipes."""
    bundled = json.loads((ROOT / "src/agent_sessions/agent_catalog.json").read_text())
    by_id = {e["manifest"]["identity"]["id"]: e["manifest"] for e in bundled["agents"]}
    for engine in ("claude", "codex", "opencode", "copilot"):
        fp = tomllib.loads((FIRST_PARTY / engine / "plugin.toml").read_text())
        assert by_id[engine].get("tools") == fp["tools"], engine
    assert by_id["claude"].get("memory") == {"kind": "claude-auto-memory"}
