"""HQ's hints only promise what HQ does today (#2265).

`web/src/components/ask/hqHints.ts` is a static catalog of example questions shown in HQ's empty
thread. Each names the HQ tool that answers it; a manual hint also names the manual page its
answer comes from. This test reads the catalog the way `test_ask_stop` reads `askStep.ts` and
fails when a hint's tool is gone, or when the installed manual's search no longer finds a manual
hint's page in its top three — a docs change that would leave a hint unanswered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_sessions import ask_orchestrator, docs_index

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "web" / "src" / "components" / "ask" / "hqHints.ts"
LINE = re.compile(r'^  \{ id: "([a-z0-9-]+)", kind: "([a-z]+)", tool: "([a-z_]+)",(.*)\},$')


def _hints() -> list[dict]:
    source = CATALOG.read_text(encoding="utf-8")
    block = source[source.index("export const HINTS") :]
    block = block[: block.index("\n];")]
    out = []
    for line in block.splitlines()[1:]:
        m = LINE.match(line)
        assert m, f"hqHints.ts: a HINTS line this test cannot read: {line!r}"
        rest = m.group(4)
        page = re.search(r'page: "([a-z0-9/-]+)"', rest)
        text = re.search(r'text: "([^"]+)"', rest)
        assert text, f"{m.group(1)}: no text"
        out.append(
            {
                "id": m.group(1),
                "kind": m.group(2),
                "tool": m.group(3),
                "page": page.group(1) if page else None,
                "text": text.group(1),
            }
        )
    return out


def test_the_catalog_is_read_whole():
    hints = _hints()
    assert len(hints) >= 6, "fewer hints than a rotation needs: has the catalog moved?"
    assert len({h["id"] for h in hints}) == len(hints), "duplicate hint ids"
    assert any(h["kind"] == "manual" for h in hints)
    assert len({h["kind"] for h in hints}) >= 3


def test_every_hint_names_a_tool_hq_has():
    missing = sorted({h["tool"] for h in _hints()} - set(ask_orchestrator.TOOLS))
    assert not missing, f"hints name HQ tools that do not exist: {missing}"


def test_only_manual_hints_name_a_page_and_they_use_the_manual_tool():
    for h in _hints():
        assert (h["page"] is not None) == (h["kind"] == "manual"), h["id"]
        if h["kind"] == "manual":
            assert h["tool"] == "docs_search", h["id"]


@pytest.fixture
def repo_manual(monkeypatch):
    monkeypatch.setattr(docs_index, "_ROOT", ROOT.resolve())
    monkeypatch.setattr(docs_index, "_INDEX", None)


def test_every_manual_hint_finds_its_page(repo_manual):
    """The question as written reaches its page in the top three (a hint whose answer moved, or
    whose page changed, fails here instead of sending the operator to "not covered")."""
    missed = []
    for h in (h for h in _hints() if h["kind"] == "manual"):
        top = [r["page"] for r in docs_index.search(h["text"])[:3]]
        if h["page"] not in top:
            missed.append((h["id"], h["page"], top))
    assert not missed, f"manual hints whose page the manual search no longer finds: {missed}"
