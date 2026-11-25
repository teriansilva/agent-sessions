"""agent-catalog-bump.yml proposes from its schedule and from `main`, never from another ref.

Forgejo's `github.ref` on a scheduled run is not `refs/heads/main`, so a guard on the ref alone
skipped the PR step on every scheduled run (#1856). The guard is evaluated here against the
contexts each trigger produces; any other expression shape fails rather than being guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parents[1] / ".forgejo/workflows/agent-catalog-bump.yml"
_CLAUSE = re.compile(r"^github\.(event_name|ref) == '([^']*)'$")


def _pr_step() -> dict:
    steps = yaml.safe_load(WF.read_text())["jobs"]["bump"]["steps"]
    (step,) = [s for s in steps if s.get("name") == "Open or refresh the bump PR"]
    return step


def _guard(expression: str, context: dict) -> bool:
    result = False
    for clause in expression.split("||"):
        match = _CLAUSE.match(clause.strip())
        assert match, f"unsupported guard clause: {clause!r}"
        result = result or context[match.group(1)] == match.group(2)
    return result


@pytest.mark.parametrize("ref", ["main", "refs/heads/main", ""])
def test_a_scheduled_run_proposes(ref):
    assert _guard(_pr_step()["if"], {"event_name": "schedule", "ref": ref})


def test_a_dispatch_on_main_proposes():
    assert _guard(_pr_step()["if"], {"event_name": "workflow_dispatch", "ref": "refs/heads/main"})


@pytest.mark.parametrize("ref", ["refs/heads/devopsagent/x", "refs/tags/v1.0.0", "main"])
def test_a_dispatch_from_another_ref_is_a_dry_run(ref):
    assert not _guard(_pr_step()["if"], {"event_name": "workflow_dispatch", "ref": ref})


def test_only_the_pr_step_holds_the_token():
    steps = yaml.safe_load(WF.read_text())["jobs"]["bump"]["steps"]
    holders = [s["name"] for s in steps if "INFRABOT_FORGEJO_TOKEN" in str(s.get("env", {}))]
    assert holders == ["Open or refresh the bump PR"]
