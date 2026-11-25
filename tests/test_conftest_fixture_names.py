"""A duplicate fixture name in conftest.py is a SILENT shadow, so it gets a test (#872).

`main` carried two `@pytest.fixture(autouse=True) def _isolate_orchestrator_ledger(...)`
definitions, one directly after the other, added within hours of each other by different
sessions fixing the same bug. Python does not warn: the second binding simply replaces the
first, so pytest collected one fixture and the other was dead code that read as live. Nothing
failed, which is exactly the problem — the isolation still worked, so the only symptom was a
reader (or an editor) trusting a definition that never runs.

That is cheap to prevent and expensive to notice, so it is asserted here rather than left to
review. The check is name-level and file-level on purpose: it says nothing about what a fixture
does, only that no name in a conftest is defined twice.

It also pins the surviving isolation itself. Deleting a duplicate is only safe if the remaining
copy is doing the work, and that is asserted directly rather than through a test that happens to
touch the ledger — every existing ledger test sets the env var in its own fixture, so none of
them can see whether conftest's copy exists.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

CONFTESTS = sorted(Path(__file__).parent.rglob("conftest.py"))


def _fixture_names(path: Path) -> list[str]:
    """Top-level functions carrying a `@pytest.fixture` (or bare `@fixture`) decorator."""
    tree = ast.parse(path.read_text())
    out: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            # `@pytest.fixture`, `@pytest.fixture(...)`, `@fixture`, `@fixture(...)`
            target = dec.func if isinstance(dec, ast.Call) else dec
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "fixture":
                out.append(node.name)
                break
    return out


def test_conftest_files_were_found():
    # A glob that silently matches nothing would make every assertion below vacuous.
    assert CONFTESTS, "expected at least one tests/conftest.py"
    assert _fixture_names(CONFTESTS[0]), "expected the root conftest to define fixtures"


def test_no_conftest_defines_a_fixture_name_twice():
    for path in CONFTESTS:
        dupes = [n for n, c in Counter(_fixture_names(path)).items() if c > 1]
        assert not dupes, (
            f"{path.relative_to(Path(__file__).parent.parent)} defines {dupes} more than once; "
            "the later definition silently shadows the earlier one, so one of them never runs"
        )


def test_the_orchestrator_ledger_resolves_inside_the_test_tmp_dir(tmp_path):
    """The surviving isolation, asserted at the point it actually matters.

    `_isolate_notifications` (autouse) pins `AGENT_SESSIONS_ORCHESTRATOR_LEDGER`, and this is
    what that buys: the ledger path a test resolves must be a tmp path, never the operator's
    real `~/.config/agent-sessions/orchestrator-ledger.jsonl`. Both directions ride on it —
    a test must not read operator state that decides its outcome (which reddened every PR in
    the repo when three synthetic `act-A` records reached the live ledger), and must not append
    fixture records into the store the live orchestrator makes decisions from.

    Asserted here because no existing ledger test can: each one sets the env var in its own
    fixture, so they stay green whether or not conftest's copy exists — which is precisely how
    the repo ended up with three copies of it.
    """
    from agent_sessions import orchestrator_ledger

    resolved = orchestrator_ledger._path(None)
    assert Path.home() not in resolved.parents, (
        f"the ledger resolved under the real HOME ({resolved}) — the autouse isolation in "
        "conftest is missing or was shadowed"
    )
    assert "orchestrator-ledger" in resolved.name
