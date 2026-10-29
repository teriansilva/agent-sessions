"""The output bridge: waiting evidence resolves from its producer step's observation (#1777 S1c)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_sessions import mission_probes, missions
from test_mission_procedures import (  # noqa: F401
    BINDINGS,
    MANIFEST,
    _apply,
    _deploy,
    _files,
    _rows,
    _set_state,
    deployed,
    setup,
)

WAITING = "o1--review_approved"
PRODUCER = "o1--pr_open"


def _observe(mid: str, key: str, *, value: bool = True, **extra) -> None:
    missions.observe_objective(mid, key, observed=True, value=value, detail="seen", extra=extra)


def _events(mid: str) -> list[dict]:
    return [
        e
        for e in missions.get_mission(mid)["events"]
        if isinstance(e.get("meta"), dict) and e["meta"].get("by") == "bridge"
    ]


@pytest.fixture
def running(deployed):  # noqa: F811
    mid = deployed["mission"]
    _apply(mid)
    _set_state(mid, "running")
    return mid


def test_a_met_producer_fills_the_waiting_row_once(running):
    mid = running
    assert missions.bridge_outputs(mid) == []  # nothing observed yet: still waiting
    _observe(mid, PRODUCER, number=7, head_sha="abc123")
    assert missions.bridge_outputs(mid) == [WAITING]
    row = _rows(mid)[WAITING]
    assert row["probe"] == "forge_review" and row["pending"] is None
    assert row["probe_args"] == {"repo": "org/app", "branch": "feature/x"}
    assert row["gate"] is True and row["state"] == "pending"
    (ev,) = _events(mid)
    src = ev["meta"]["sources"]["branch"]
    assert src["from"] == PRODUCER and src["slot"] == "head_branch"
    assert src["incarnation"] == _rows(mid)[PRODUCER]["incarnation"]
    assert missions.bridge_outputs(mid) == []  # a repeated sweep resolves nothing more
    assert len(_events(mid)) == 1


@pytest.mark.parametrize(
    "observe",
    [
        lambda mid: _observe(mid, PRODUCER, value=False, number=7),
        lambda mid: missions.observe_objective(mid, PRODUCER, observed=False, value=False),
        lambda mid: None,
    ],
    ids=["unmet", "unknown", "never-observed"],
)
def test_an_unmet_or_unknown_producer_fills_nothing(running, observe):
    observe(running)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["pending"] is not None


def _repend(mid: str, refs: dict) -> None:
    con = sqlite3.connect(missions._db_path())
    row = con.execute(
        "SELECT pending FROM mission_objectives WHERE mission_id=? AND key=?", (mid, WAITING)
    ).fetchone()
    pending = json.loads(row[0])
    pending["refs"] = refs
    con.execute(
        "UPDATE mission_objectives SET pending=? WHERE mission_id=? AND key=?",
        (json.dumps(pending), mid, WAITING),
    )
    con.commit()
    con.close()


def test_authority_never_comes_from_an_observation(running):
    """Only `ARG_SLOT_TYPES` arguments take a slot: a stored ref into `repo` is never filled."""
    _repend(running, {"repo": "{{steps.implement.head_branch}}"})
    _observe(running, PRODUCER, number=7)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["probe"] == "none"


def _producer_branch(mid: str, branch: str) -> None:
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_objectives SET probe_args=? WHERE mission_id=? AND key=?",
        (json.dumps({"repo": "org/app", "branch": branch}), mid, PRODUCER),
    )
    con.commit()
    con.close()


@pytest.mark.parametrize("bad", ["not a branch", "-rf", "a..b", "x\x01", "é"])
def test_an_observed_value_that_is_not_a_ref_name_stays_waiting(running, bad):
    """An observed value is held to its slot's shape, stricter than an operator-typed argument."""
    _producer_branch(running, bad)
    _observe(running, PRODUCER, number=7)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["pending"] is not None


def test_a_slot_the_producer_step_does_not_declare_is_never_taken(running):
    """`implement` declares `head_branch`; a stored ref to its `branch` (which its `git_local`
    row could observe) is refused all the same."""
    _repend(running, {"branch": "{{steps.implement.branch}}"})
    _observe(running, "o1--branch_ready", branch="feature/from-checkout")
    assert missions.bridge_outputs(running) == []


def test_a_replaced_producer_row_fills_nothing(running):
    """Drop the producer and add the key back: a new incarnation, never observed — so the old
    observation can't fill the row (it is gone with the old row)."""
    _observe(running, PRODUCER, number=7)
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_objectives SET state='pending', observed=NULL, incarnation='new' "
        "WHERE mission_id=? AND key=?",
        (running, PRODUCER),
    )
    con.commit()
    con.close()
    assert missions.bridge_outputs(running) == []


def test_an_optional_waiting_item_gets_its_own_required_back(running):
    con = sqlite3.connect(missions._db_path())
    row = con.execute(
        "SELECT pending FROM mission_objectives WHERE mission_id=? AND key=?", (running, WAITING)
    ).fetchone()
    pending = json.loads(row[0]) | {"gate": False}
    con.execute(
        "UPDATE mission_objectives SET pending=? WHERE mission_id=? AND key=?",
        (json.dumps(pending), running, WAITING),
    )
    con.commit()
    con.close()
    _observe(running, PRODUCER, number=7)
    assert missions.bridge_outputs(running) == [WAITING]
    assert _rows(running)[WAITING]["gate"] is False


def test_a_resolved_row_is_never_retargeted(running):
    _observe(running, PRODUCER, number=7)
    missions.bridge_outputs(running)
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_objectives SET probe_args=? WHERE mission_id=? AND key=?",
        (json.dumps({"repo": "org/app", "branch": "feature/y"}), running, PRODUCER),
    )
    con.commit()
    con.close()
    _observe(running, PRODUCER, number=8)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["probe_args"]["branch"] == "feature/x"


def test_the_probe_sweep_bridges_in_the_pass_its_producer_settles(running, monkeypatch):
    """Through the real runner: `forge_pr` settles met, and the same pass resolves its consumer."""

    def fake(mission, obj, *, target=None):
        if obj["key"] == PRODUCER:
            return mission_probes.Fact.seen(True, "PR #7 is open", number=7, head_sha="abc")
        return mission_probes.Fact.unknown("not this test")

    monkeypatch.setattr(mission_probes, "probe_one", fake)
    mission_probes.run_for_mission(running)
    assert _rows(running)[PRODUCER]["state"] == "met"
    row = _rows(running)[WAITING]
    assert row["probe"] == "forge_review" and row["pending"] is None


def test_a_bridge_after_a_restart_converges(running):
    """Nothing is held between passes: the producer was observed, the process died before any
    bridge ran; the next pass's bridge resolves it exactly once."""
    _observe(running, PRODUCER, number=7)
    missions.reset_schema_cache_for_test()  # a fresh process opening the store
    assert missions.bridge_outputs(running) == [WAITING]
    assert missions.bridge_outputs(running) == []


def test_a_mission_without_a_plan_bridges_nothing(deployed):  # noqa: F811
    assert missions.bridge_outputs(deployed["mission"]) == []


def test_a_waived_producer_fills_nothing_even_with_a_true_observation(running):
    """Only a producer the store counts as MET produces outputs: a waiver is the operator not
    requiring the row, never evidence that it holds."""
    _observe(running, PRODUCER, number=7)
    con = sqlite3.connect(missions._db_path())  # a met row is not waivable; the state is the point
    con.execute(
        "UPDATE mission_objectives SET state='waived' WHERE mission_id=? AND key=?",
        (running, PRODUCER),
    )
    con.commit()
    con.close()
    assert missions.bridge_outputs(running) == []


SHA_FLOW = """format = 5
title = "Ship and verify the revision"
[[steps]]
id = "implement"
title = "Implement"
actor = { kind = "agent" }
outputs = ["head_sha"]
[[steps.checklist]]
key = "on_branch"
title = "On its branch"
probe = "git_local"
probe_args = { branch = "{{work_branch}}" }
[[steps]]
id = "verify"
title = "Verify"
actor = { kind = "operator" }
after = ["implement"]
[[steps.checklist]]
key = "live"
title = "The revision is live"
probe = "http_revision"
probe_args = { url = "{{revision_url}}", expect = "{{steps.implement.head_sha}}" }
"""


def test_a_git_local_head_sha_fills_its_consumer(setup, tmp_path, monkeypatch):  # noqa: F811
    """The independent review on #1792: `git_local` is declared to produce `head_sha`, so a step
    whose only `head_sha` producer is `git_local` must resolve, not wait for ever."""
    folder, _body = setup
    contents = _files()
    contents["playbook.toml"] = MANIFEST.replace(
        "[[materials]]", '[[variables]]\nname = "revision_url"\ntype = "url"\n[[materials]]'
    )
    contents["flows/sha.toml"] = SHA_FLOW
    bindings = [
        *BINDINGS,
        {"name": "revision_url", "kind": "text", "value": "https://app.example/v"},
    ]
    d = _deploy(folder, tmp_path, monkeypatch, contents, bindings)
    mid = d["mission"]
    _apply(mid, "sha")
    _set_state(mid, "running")
    assert _rows(mid)["o1--live"]["probe"] == "none"
    _observe(mid, "o1--on_branch", branch="feature/x", head_sha="abcdef1234")
    assert missions.bridge_outputs(mid) == ["o1--live"]
    row = _rows(mid)["o1--live"]
    assert row["probe"] == "http_revision"
    assert row["probe_args"] == {"url": "https://app.example/v", "expect": "abcdef1234"}
    missions.reset_schema_cache_for_test()


def test_the_git_local_probe_records_the_head_it_is_on(monkeypatch):
    monkeypatch.setattr(
        mission_probes.gitpanel, "git_status", lambda cwd: {"repo": cwd, "branch": "feat"}
    )
    monkeypatch.setattr(mission_probes.gitpanel, "head_sha", lambda cwd, *a: "abcdef1234")
    fact = mission_probes._probe_git_local({"cwd": "/repo"}, {"branch": "feat"})
    assert fact.value is True and fact.extra == {"branch": "feat", "head_sha": "abcdef1234"}


def test_a_waived_waiting_row_is_left_alone(running):
    missions.patch_objectives(running, [{"op": "waive", "key": WAITING}])
    _observe(running, PRODUCER, number=7)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["probe"] == "none" and _events(running) == []


def test_a_slot_of_another_type_never_fills_an_argument(running):
    """A declared `head_branch` (a branch) never fills `expect` (which takes a SHA)."""
    _repend(running, {"expect": "{{steps.implement.head_branch}}"})
    _observe(running, PRODUCER, number=7)
    assert missions.bridge_outputs(running) == []


def test_an_implicit_branch_producer_fills_head_branch_from_the_observed_pr(running):
    """Hermes on #1792 (1): `forge_pr` may omit `branch` and probe the checkout's; the PR it
    observed carries its head ref, which is what `head_branch` means."""
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_objectives SET probe_args=? WHERE mission_id=? AND key=?",
        (json.dumps({"repo": "org/app"}), running, PRODUCER),
    )
    con.commit()
    con.close()
    _observe(running, PRODUCER, number=7, head_ref="feature/derived")
    assert missions.bridge_outputs(running) == [WAITING]
    assert _rows(running)[WAITING]["probe_args"]["branch"] == "feature/derived"


def test_the_forge_fact_records_the_prs_head_ref():
    from agent_sessions import forge

    client = forge.ForgeClient(kind="forgejo", base_url="https://forge.example", owner="org")
    pr = {
        "number": 7,
        "state": "open",
        "head": {"ref": "feature/x", "sha": "abc1234", "repo": {"full_name": "org/app"}},
    }
    client._get = lambda path, params=None: [pr] if params.get("page") == 1 else []
    fact = client.pull_request("org/app", "feature/x", include_closed=False)
    assert fact.value is True and fact.extra["head_ref"] == "feature/x"


@pytest.mark.parametrize("state", ["failed", "abandoned", "done"])
def test_a_closed_mission_is_never_bridged(running, state):
    """Hermes on #1792 (2): a pass that lost a race to a terminal transition rewrites nothing."""
    _observe(running, PRODUCER, number=7)
    _set_state(running, state)
    assert missions.bridge_outputs(running) == []
    assert _rows(running)[WAITING]["pending"] is not None
    assert _events(running) == []


def test_an_archived_mission_is_never_bridged(running):
    _observe(running, PRODUCER, number=7)
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET archived_at=1.0 WHERE id=?", (running,))
    con.commit()
    con.close()
    assert missions.bridge_outputs(running) == []


def test_the_sweep_does_not_bridge_after_losing_a_race_to_a_terminal_state(running, monkeypatch):
    """Through the real runner: the pass read a running mission, the mission failed meanwhile."""
    _observe(running, PRODUCER, number=7)
    real = missions.get_mission

    def read_then_fail(mid, **kw):
        out = real(mid, **kw)
        _set_state(mid, "failed")
        return out

    monkeypatch.setattr(missions, "get_mission", read_then_fail)
    mission_probes.run_for_mission(running)
    assert _rows(running)[WAITING]["pending"] is not None
