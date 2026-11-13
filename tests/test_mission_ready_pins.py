"""Copilot and Codex are pinned off as mission roles, each with a reason naming its issue (#1903).

A later flip must touch this test and `kinds.API_MISSION_REFUSAL`, so it cannot happen by drift."""

from __future__ import annotations

import re

from agent_sessions.plugins import kinds

PINNED_OFF = {"copilot-acp": "#2004", "codex-app-server": "#2005"}  # kind-data


def test_pinned_kinds_are_off_with_a_reason_naming_their_issue():
    for kind, issue in PINNED_OFF.items():
        assert kinds.API_MISSION_READY[kind] is False
        assert issue in kinds.API_MISSION_REFUSAL[kind]


def test_refusal_map_is_exactly_the_off_kinds():
    off = {k for k, v in kinds.API_MISSION_READY.items() if not v}
    assert set(kinds.API_MISSION_REFUSAL) == off
    assert off == set(PINNED_OFF)
    for reason in kinds.API_MISSION_REFUSAL.values():
        assert re.search(r"#\d+", reason)
