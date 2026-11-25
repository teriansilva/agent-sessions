"""deploy-staging.yml must stay main-only.

The runner-held deploy key runs build hooks as the deployment user, so the host-side forced
command accepts only `staging main <true|false>`. A branch input here would either always be
refused or, if the host were widened to match, let anyone who can dispatch run unreviewed code
on the staging host.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".forgejo/workflows/deploy-staging.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text())


def _on(doc: dict) -> dict:
    # PyYAML parses a bare `on:` key as boolean True.
    return doc.get("on", doc.get(True))


def test_staging_offers_no_branch_input():
    for trigger in (_on(_doc()) or {}).values():
        inputs = (trigger or {}).get("inputs") or {}
        assert "branch" not in inputs
        assert set(inputs) <= {"reenable_refresh"}


def test_staging_cannot_be_dispatched_while_unprovisioned():
    # Flip `workflow_call` back to `workflow_dispatch` (and drop this test) once the
    # staging checkout, venv and unit exist on the deploy host.
    assert set(_on(_doc())) == {"workflow_call"}


def test_staging_sends_only_staging_main_to_the_forced_command():
    runs = [s.get("run") or "" for job in _doc()["jobs"].values() for s in job["steps"]]
    ssh_calls = [r for r in runs if "ci-deploy@" in r]
    assert len(ssh_calls) == 1
    cmd = re.search(r'ci-deploy@\S+\s+"([^"]*)"', ssh_calls[0]).group(1)
    assert cmd == "staging main ${REENABLE}"
