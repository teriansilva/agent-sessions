"""Which events reach the scoped checks (#2072).

A selector is only as good as the events that run it: a full-suite rule for a path the workflow's
own `paths:` filter never admits is dead, and a "full backstop on main" that a path filter skips is
no backstop. So the triggers are pinned here, against the scopers' own full-run rules.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WF = REPO / ".forgejo" / "workflows"
sys.path.insert(0, str(REPO / ".forgejo" / "scripts"))
_spec = importlib.util.spec_from_file_location("e2e_scope", REPO / ".forgejo/scripts/e2e_scope.py")
e2e_scope = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(e2e_scope)


def _on(name: str) -> dict:
    doc = yaml.safe_load((WF / name).read_text())
    return doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as True


def _admits(globs: list[str], path: str) -> bool:
    for g in globs:
        rx = re.escape(g).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if re.fullmatch(rx, path):
            return True
    return False


@pytest.mark.parametrize("name", ["pr-validate.yml", "web-ci.yml"])
def test_every_push_to_main_runs_unfiltered(name):
    push = _on(name)["push"]
    assert push["branches"] == ["main"]
    assert "paths" not in push and "paths-ignore" not in push


@pytest.mark.parametrize("name", ["pr-validate.yml", "web-ci.yml"])
def test_a_full_run_can_be_dispatched(name):
    # The supported way to force a full run on an already-open PR: dispatch on its branch.
    assert "workflow_dispatch" in _on(name)


def test_pr_validate_admits_every_pr():
    assert "paths" not in _on("pr-validate.yml")["pull_request"]


@pytest.mark.parametrize(
    "path",
    [
        ".forgejo/workflows/web-ci.yml",
        ".forgejo/scripts/e2e_scope.py",
        ".forgejo/scripts/pr_scope.py",
        ".forgejo/scripts/recheck.py",
        ".forgejo/scripts/sibling_watch.py",
        "scripts/e2e_slot_run",
        "scripts/e2e_port.sh",
        "web/src/app/App.tsx",
    ],
)
def test_web_ci_admits_the_paths_its_scoper_runs_everything_for(path):
    globs = _on("web-ci.yml")["pull_request"]["paths"]
    assert _admits(globs, path), path
    assert e2e_scope.decide([path], REPO / "web")[0] == "full"


def test_hermes_is_never_triggered_by_a_push_or_dispatch():
    doc = yaml.safe_load((WF / "pr-validate.yml").read_text())
    steps = doc["jobs"]["python"]["steps"]
    hermes = [s for s in steps if "Hermes" in s.get("name", "") or "pr-visual" in s.get("name", "")]
    assert len(hermes) == 3
    for s in hermes:
        assert "github.event_name == 'pull_request'" in str(s.get("if", "")), s["name"]


def _tracked(prefix: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", prefix], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def test_every_ci_file_starts_web_ci_and_runs_its_full_suite():
    """Not examples: every tracked file under .forgejo/ (every workflow, script and plugin), and
    every rule of the scoper's own full-run list that lives outside web/."""
    globs = _on("web-ci.yml")["pull_request"]["paths"]
    paths = _tracked(".forgejo/") + _tracked("scripts/")
    paths = [p for p in paths if p.startswith(".forgejo/") or p.startswith("scripts/e2e_")]
    assert ".forgejo/workflows/pr-validate.yml" in paths
    assert ".forgejo/pytest_plugins/recheck_report.py" in paths
    for p in paths:
        assert _admits(globs, p), f"web-ci never starts for {p}"
        assert e2e_scope.decide([p], REPO / "web")[0] == "full", p
    outside = [
        r for r in (*e2e_scope.FULL_FILES, *e2e_scope.FULL_PREFIXES) if not r.startswith("web/")
    ]
    for rule in outside:
        probe = rule if rule in e2e_scope.FULL_FILES else rule + "probe"
        assert _admits(globs, probe), f"the full-run rule {rule!r} is unreachable"
