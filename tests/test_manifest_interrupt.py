"""`terminal.interrupt` — the key that stops an agent's running turn (#1960 Phase 2).

HQ's `send_nudge(interrupt=true)` types it alone before the text. It is asked of the manifest,
never of the engine's name (`engine-providers.md`), and an engine that declares nothing has no
interrupt: HQ says so rather than guessing a key.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_sessions import engines
from agent_sessions.plugins import kinds, manifest
from agent_sessions.plugins.manifest import ManifestError

FIRST_PARTY = Path(manifest.__file__).resolve().parent / "first_party"
DECLARE_ESC = {"claude", "codex", "opencode", "gemini", "copilot"}


def _doc(name: str) -> dict:
    return tomllib.loads((FIRST_PARTY / name / "plugin.toml").read_text())


@pytest.mark.parametrize("name", sorted(DECLARE_ESC | {"kimi", "antigravity"}))
def test_each_first_party_agent_declares_only_what_its_vendor_documents(name):
    m = manifest.parse(_doc(name), source="first-party")
    assert m.terminal.interrupt == ("esc" if name in DECLARE_ESC else "none")


def test_the_bytes_come_from_the_manifest_and_default_to_none():
    for eid in DECLARE_ESC:
        assert engines.interrupt_bytes(engines.get(eid)) == b"\x1b", eid
    assert engines.interrupt_bytes(engines.get("kimi")) is None
    assert engines.interrupt_bytes(engines.get("shell")) is None  # not actuable at all
    assert engines.interrupt_bytes(None) is None


def test_a_terminal_plugin_may_not_declare_an_interrupt():
    doc = _doc("shell")
    doc.setdefault("terminal", {})["interrupt"] = "esc"
    with pytest.raises(ManifestError, match="terminal.interrupt"):
        manifest.parse(doc, source="first-party")


def test_an_unknown_interrupt_kind_is_refused():
    doc = _doc("claude")
    doc["terminal"]["interrupt"] = "ctrl-c"
    with pytest.raises(ManifestError, match="interrupt"):
        manifest.parse(doc, source="first-party")
    assert "ctrl-c" not in kinds.INTERRUPT_KINDS
