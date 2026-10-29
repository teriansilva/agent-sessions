"""Procedure inputs picked by index (#1777 S1d, absorbing #1076)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import test_mission_procedures as _tmp
from agent_sessions import forge, mission_procedures, missions
from agent_sessions.main import create_app
from test_mission_procedures import _counts, _login, _rows

setup = _tmp.setup
deployed = _tmp.deployed


def _pr(number, branch, repo="org/app", title="t"):
    return {"number": number, "title": title, "head": {"ref": branch, "repo": {"full_name": repo}}}


class FakeForge:
    """`open_pull_heads` over a scripted page set, through the REAL filter."""

    def __init__(self, pulls=None, boom=False):
        self.pulls = pulls or []
        self.boom = boom
        self.calls = []

    def open_pull_heads(self, repo):
        self.calls.append(repo)
        if self.boom:
            raise forge.ForgeError("the forge is down")
        client = forge.ForgeClient(kind="forgejo", base_url="https://forge.example", owner="org")
        client._get = lambda path, params=None: self.pulls if params["page"] == 1 else []
        return client.open_pull_heads(repo)


@pytest.fixture
def pulls(monkeypatch):
    fake = FakeForge(
        [
            _pr(11, "feature/a", title="Add a"),
            _pr(12, "feature/b", title="Add b"),
            _pr(13, "feature/a", repo="someone/app"),  # a fork's same-named branch
            _pr(14, "feature/x"),  # the deployed value, already offered
            _pr(15, "bad branch"),  # not a ref name
        ]
    )
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    return fake


def _dev(mid):
    listing = mission_procedures.listing(mid)
    return 0, listing["listing"]


def test_the_choices_are_the_deployed_value_then_the_repos_open_pr_heads(deployed, pulls):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    out = mission_procedures.input_choices(mid, index, digest)
    assert list(out["inputs"]) == ["work_branch"]  # `repo` is never choosable
    values = [(c["index"], c["value"], c["source"]) for c in out["inputs"]["work_branch"]]
    assert values == [
        (0, "feature/x", "deployment"),
        (1, "feature/a", "pull_request"),
        (2, "feature/b", "pull_request"),
        (3, "feature/x", "pull_request"),  # the deployed branch's own PR, bound by number
    ]
    assert out["inputs"]["work_branch"][1]["number"] == 11
    assert pulls.calls == ["org/app"]  # the reviewed repository, and only it
    assert mission_procedures.input_choices(mid, index, digest)["choices"] == out["choices"]


def test_one_objective_per_chosen_pr(deployed, pulls):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    for n, oid in ((1, "o1"), (2, "o2")):
        out = mission_procedures.apply_listed(
            mid, index, digest, inputs={"work_branch": {"choice": n}}, choices=offered["choices"]
        )
        assert out["objective"] == oid
    rows = _rows(mid)
    assert rows["o1--pr_open"]["probe_args"] == {
        "repo": "org/app",
        "branch": "feature/a",
        "number": 11,
    }
    assert rows["o1--branch_ready"]["probe_args"] == {"branch": "feature/a"}  # not a PR probe
    assert rows["o2--pr_open"]["probe_args"] == {
        "repo": "org/app",
        "branch": "feature/b",
        "number": 12,
    }
    # The waiting row keeps its reviewed repo and is bound to the PR; its branch still comes from
    # the bridge (S1c).
    assert rows["o2--review_approved"]["pending"]["args"] == {"repo": "org/app", "number": 12}
    doc = missions.plan_revision(mid)["document"]
    assert doc["objectives"][1]["inputs"] == [
        {"name": "repo", "source": "deployment"},
        {"name": "work_branch", "source": "pull_request", "value": "feature/b", "number": 12},
    ]
    with pytest.raises(missions.MissionError) as e:  # the same PR twice is a duplicate
        mission_procedures.apply_listed(
            mid, index, digest, inputs={"work_branch": {"choice": 2}}, choices=offered["choices"]
        )
    assert e.value.status == 409


@pytest.mark.parametrize(
    ("inputs", "choices", "status"),
    [
        ({"work_branch": {"choice": 9}}, None, 422),
        ({"work_branch": {"choice": "1"}}, None, 422),
        ({"work_branch": "feature/evil"}, None, 422),
        ({"work_branch": {"value": "feature/evil"}}, None, 422),
        ({"work_branch": {"choice": 1, "value": "feature/evil"}}, None, 422),
        ({"repo": {"choice": 0}}, None, 422),
        ({}, None, 422),
        ({"work_branch": {"choice": 1}}, "0" * 32, 409),
    ],
    ids=[
        "out-of-range",
        "string-index",
        "a-value",
        "value-key",
        "extra-key",
        "repo",
        "empty",
        "stale-choices",
    ],
)
def test_an_input_is_a_position_in_the_offered_list_and_nothing_else(
    deployed, pulls, inputs, choices, status
):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(
            mid, index, digest, inputs=inputs, choices=choices or offered["choices"]
        )
    assert e.value.status == status
    assert _counts(mid) == (0, 0)


def test_a_pr_that_closed_since_the_choices_were_read_is_a_409(deployed, pulls):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    pulls.pulls = [p for p in pulls.pulls if p["number"] != 11]
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(
            mid, index, digest, inputs={"work_branch": {"choice": 1}}, choices=offered["choices"]
        )
    assert e.value.status == 409 and "changed" in str(e.value)
    assert _counts(mid) == (0, 0)


@pytest.mark.parametrize("client", [None, FakeForge(boom=True)], ids=["no-forge", "forge-down"])
def test_without_a_reachable_forge_only_the_deployed_value_is_offered(
    deployed, monkeypatch, client
):
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: client)
    mid = deployed["mission"]
    index, digest = _dev(mid)
    out = mission_procedures.input_choices(mid, index, digest)
    assert [c["value"] for c in out["inputs"]["work_branch"]] == ["feature/x"]


def test_the_forge_listing_excludes_forks_and_reports_completeness():
    client = forge.ForgeClient(kind="forgejo", base_url="https://forge.example", owner="org")
    pages = {1: [_pr(i, f"b{i}") for i in range(client.PR_PAGE)], 2: [_pr(99, "last")]}
    seen = []

    def get(path, params=None):
        seen.append((path, params["state"], params["page"]))
        return pages.get(params["page"], [])

    client._get = get
    rows, complete = client.open_pull_heads("org/app")
    assert complete is True and len(rows) == client.PR_PAGE + 1
    assert seen[0] == ("/api/v1/repos/org/app/pulls", "open", 1)
    client._get = lambda path, params=None: [_pr(1, "x")] * client.PR_PAGE
    rows, complete = client.open_pull_heads("org/app")
    assert complete is False
    client._get = lambda path, params=None: [_pr(1, "x", repo="fork/app"), {"head": "junk"}]
    assert client.open_pull_heads("org/app") == ([], True)


def test_the_inputs_route_needs_login_and_takes_a_choice(auth_cfg, deployed, pulls):
    mid = deployed["mission"]
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get(f"/api/missions/{mid}/procedures/0/inputs?listing=x").status_code == 401
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    listing = c.get(f"/api/missions/{mid}/procedures").json()["listing"]
    r = c.get(f"/api/missions/{mid}/procedures/0/inputs", params={"listing": listing})
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    offered = r.json()
    body = {
        "index": 0,
        "listing": listing,
        "inputs": {"work_branch": {"choice": 1}},
        "choices": offered["choices"],
    }
    assert c.post(f"/api/missions/{mid}/procedure", json=body, headers=hdr).status_code == 200
    assert _rows(mid)["o1--pr_open"]["probe_args"]["branch"] == "feature/a"
    bad = {**body, "inputs": {"work_branch": "feature/evil"}}
    assert c.post(f"/api/missions/{mid}/procedure", json=bad, headers=hdr).status_code == 422
    half = {"index": 0, "listing": listing, "inputs": {"work_branch": {"choice": 1}}}
    assert c.post(f"/api/missions/{mid}/procedure", json=half, headers=hdr).status_code == 422


def test_picking_the_deployed_value_is_the_same_objective_as_no_pick(deployed, pulls):
    """The independent review on #1793: choice 0 and no pick record the same inputs, so the
    second apply is a duplicate rather than a second objective with identical targets."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    mission_procedures.apply_listed(mid, index, digest)
    offered = mission_procedures.input_choices(mid, index, digest)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(
            mid, index, digest, inputs={"work_branch": {"choice": 0}}, choices=offered["choices"]
        )
    assert e.value.status == 409 and "already applies" in str(e.value)


def test_a_retitled_pr_does_not_stale_the_choices_and_order_is_by_number(deployed, pulls):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    before = mission_procedures.input_choices(mid, index, digest)
    pulls.pulls = [
        {**p, "title": "renamed"} if p["number"] == 11 else p for p in reversed(pulls.pulls)
    ]
    after = mission_procedures.input_choices(mid, index, digest)
    assert after["choices"] == before["choices"]
    assert [c["number"] for c in after["inputs"]["work_branch"][1:]] == [11, 12, 14]


def test_how_far_the_pr_list_went_is_reported(deployed, pulls, monkeypatch):
    mid = deployed["mission"]
    index, digest = _dev(mid)
    assert mission_procedures.input_choices(mid, index, digest)["listed"] == {
        "work_branch": "complete"
    }
    monkeypatch.setattr(pulls, "open_pull_heads", lambda repo: ([], False))
    assert mission_procedures.input_choices(mid, index, digest)["listed"]["work_branch"] == (
        "partial"
    )
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: None)
    out = mission_procedures.input_choices(mid, index, digest)
    assert out["listed"]["work_branch"] == "unavailable"
    assert out["deployed"] == {"work_branch": "feature/x"}


def test_resolved_picks_then_the_store_write(deployed, pulls):
    """The route's split: `resolve_picks` (the forge read) then `apply_listed(picked=...)`."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    picked = mission_procedures.resolve_picks(
        mid, index, digest, {"work_branch": {"choice": 2}}, offered["choices"]
    )
    assert picked["work_branch"]["value"] == "feature/b"
    mission_procedures.apply_listed(mid, index, digest, picked=picked)
    assert _rows(mid)["o1--pr_open"]["probe_args"]["branch"] == "feature/b"


@pytest.mark.parametrize("bad", ["not a branch", "-rf", "a..b", "", None, 7])
def test_a_picked_value_is_rechecked_whoever_passes_it(deployed, bad):
    """`apply_listed(picked=…)` re-checks each value's shape: a later caller (HQ, P5) can never
    hand it something the offered list could not have held."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(
            mid,
            index,
            digest,
            picked={"work_branch": {"value": bad, "source": "pull_request"}},
        )
    assert e.value.status == 422
    assert _counts(mid) == (0, 0)


def test_an_objective_applied_before_values_were_recorded_is_still_a_duplicate(deployed):
    """An S1b-era objective's inputs carry no `value`; the same apply today, on the branch its
    own rows target, is its duplicate."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    mission_procedures.apply_listed(mid, index, digest)
    _legacy(mid)
    with pytest.raises(missions.MissionError) as e:
        mission_procedures.apply_listed(mid, index, digest)
    assert e.value.status == 409 and "already applies" in str(e.value)


def test_a_branch_reused_by_a_later_pr_is_a_new_objective(deployed, pulls):
    """Hermes on #1793 (1): PR #11 from `feature/a` merges and PR #21 opens from `feature/a`;
    they are different PRs, so the second pick is its own objective, not a duplicate."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    mission_procedures.apply_listed(
        mid, index, digest, inputs={"work_branch": {"choice": 1}}, choices=offered["choices"]
    )
    pulls.pulls = [_pr(21, "feature/a", title="Again a"), _pr(12, "feature/b")]
    offered = mission_procedures.input_choices(mid, index, digest)
    again = next(c for c in offered["inputs"]["work_branch"] if c.get("number") == 21)
    out = mission_procedures.apply_listed(
        mid,
        index,
        digest,
        inputs={"work_branch": {"choice": again["index"]}},
        choices=offered["choices"],
    )
    assert out["objective"] == "o2"
    inputs = [o["inputs"][1] for o in missions.plan_revision(mid)["document"]["objectives"]]
    assert [i["number"] for i in inputs] == [11, 21]


def test_two_open_prs_on_one_head_are_two_choices(deployed, monkeypatch):
    fake = FakeForge([_pr(31, "feature/c", title="to main"), _pr(32, "feature/c", title="to rel")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid = deployed["mission"]
    index, digest = _dev(mid)
    rows = mission_procedures.input_choices(mid, index, digest)["inputs"]["work_branch"]
    assert [(c.get("number"), c["value"]) for c in rows] == [
        (None, "feature/x"),
        (31, "feature/c"),
        (32, "feature/c"),
    ]


def _legacy(mid: str) -> None:
    """Rewrite the plan's inputs as an S1b-era build recorded them: no `value`, no `number`."""
    import json
    import sqlite3

    con = sqlite3.connect(missions._db_path())
    row = con.execute("SELECT document_json FROM mission_plan_revisions").fetchone()
    doc = json.loads(row[0])
    for i in doc["objectives"][0]["inputs"]:
        i.pop("value", None)
    con.execute("DROP TRIGGER mission_plan_revisions_append_only")  # simulate the older write
    con.execute("UPDATE mission_plan_revisions SET document_json=?", (json.dumps(doc),))
    con.commit()
    con.close()


def test_a_legacy_objective_on_another_branch_does_not_mask_a_redeployed_target(deployed):
    """Hermes on #1793 (2): an S1b objective targets what its OWN rows target. Re-deployed to
    another branch (simulated in the review record), the new apply is not its duplicate."""
    mid = deployed["mission"]
    index, digest = _dev(mid)
    mission_procedures.apply_listed(mid, index, digest)
    _legacy(mid)

    def redeploy(rec):
        for t in rec["review_facts"]["targets"]:
            if "branch" in t["args"]:
                t["args"]["branch"] = "feature/new-target"

    _tmp._edit_record(deployed["project"].id, redeploy)
    index, digest = _dev(mid)
    assert mission_procedures.apply_listed(mid, index, digest)["objective"] == "o2"
    assert _rows(mid)["o2--pr_open"]["probe_args"]["branch"] == "feature/new-target"


# ---- the chosen PR binds its forge evidence by number (Hermes on #1793, review 7415) -----------


def _routed(prs: dict[int, dict], merged: set[int] = frozenset()):
    """A ForgeClient whose `_get` answers `/pulls?state=open` and `/pulls/{n}` from `prs`."""
    client = forge.ForgeClient(kind="forgejo", base_url="https://forge.example", owner="org")

    def get(path, params=None):
        if path.endswith("/pulls"):
            return list(prs.values()) if (params or {}).get("page") == 1 else []
        n = int(path.rsplit("/", 1)[1])
        return {**prs[n], "state": "open", "merged": n in merged}

    client._get = get
    return client


def test_pull_by_number_asks_about_exactly_that_pr():
    prs = {31: _pr(31, "feature/c"), 32: _pr(32, "feature/c"), 33: _pr(33, "x", repo="fork/app")}
    client = _routed(prs)
    assert client.pull_request("org/app", "feature/c", include_closed=False).extra["number"] == 31
    fact = client.pull_by_number("org/app", 32, "feature/c")
    assert fact.value is True and fact.extra["number"] == 32
    assert client.pull_by_number("org/app", 33).observed is False  # a fork's head: not ours
    assert client.pull_by_number("org/app", 32, "feature/other").observed is False


def test_the_probe_asks_about_the_bound_pr_not_the_first_on_its_branch(monkeypatch):
    """Two open PRs on one head: an objective bound to #32 observes #32, never #31."""
    from agent_sessions import mission_probes

    client = _routed({31: _pr(31, "feature/c"), 32: _pr(32, "feature/c")}, merged={32})
    monkeypatch.setattr(mission_probes, "_client_for", lambda target: client)
    mission = {"cwd": ""}
    base = {"repo": "org/app", "branch": "feature/c"}
    pr = mission_probes.probe_one(
        mission, {"probe": "forge_pr", "probe_args": {**base, "number": 32}}
    )
    assert pr.extra["number"] == 32
    merged = mission_probes.probe_one(
        mission, {"probe": "forge_merged", "probe_args": {**base, "number": 32}}
    )
    assert merged.value is True and merged.extra["number"] == 32
    unbound = mission_probes.probe_one(mission, {"probe": "forge_merged", "probe_args": base})
    assert unbound.extra["number"] == 31  # what branch resolution alone would have asked about


def test_a_picked_pr_on_one_head_binds_every_pr_probe_through_the_bridge(deployed, monkeypatch):
    """Through the plan, the rows and the S1c bridge: the second of two PRs on one head is the
    one its objective's evidence asks about, including the waiting review row once bridged."""
    fake = FakeForge([_pr(31, "feature/c"), _pr(32, "feature/c")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid = deployed["mission"]
    index, digest = _dev(mid)
    offered = mission_procedures.input_choices(mid, index, digest)
    pick = next(c for c in offered["inputs"]["work_branch"] if c.get("number") == 32)
    mission_procedures.apply_listed(
        mid,
        index,
        digest,
        inputs={"work_branch": {"choice": pick["index"]}},
        choices=offered["choices"],
    )
    import sqlite3

    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET state='running' WHERE id=?", (mid,))
    con.commit()
    con.close()
    missions.observe_objective(
        mid, "o1--pr_open", observed=True, value=True, extra={"number": 32, "head_ref": "feature/c"}
    )
    assert missions.bridge_outputs(mid) == ["o1--review_approved"]
    assert _rows(mid)["o1--review_approved"]["probe_args"] == {
        "repo": "org/app",
        "branch": "feature/c",
        "number": 32,
    }


def test_a_playbook_never_names_a_pr_number_and_the_editors_never_offer_one():
    from agent_sessions.playbooks import PlaybookFormatError, validate

    doc = {
        "format": 5,
        "title": "t",
        "steps": [
            {
                "id": "s",
                "title": "s",
                "actor": {"kind": "agent"},
                "checklist": [
                    {
                        "key": "k",
                        "title": "t",
                        "probe": "forge_merged",
                        "probe_args": {"number": "{{n}}"},
                    }
                ],
            }
        ],
    }
    variables = list(
        validate._variables(
            validate._Reader({"variables": [{"name": "n", "type": "int"}]}, "")
        ).values()
    )
    with pytest.raises(PlaybookFormatError, match="bound by the server"):
        validate.flow_document(doc, "f", variables)
    from agent_sessions.playbooks import authoring

    probes = authoring.vocabulary()["probes"]
    assert all("number" not in p["args"] for p in probes.values())


def test_the_config_export_never_offers_the_server_bound_number(auth_cfg, deployed):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    _login(c, auth_cfg)
    probes = c.get("/api/config").json()["mission_probes"]
    for kind in ("forge_pr", "forge_checks", "forge_review", "forge_merged"):
        assert "number" not in probes["args"][kind]["optional"]
        assert "number" not in probes["types"][kind]


REL_FLOW = """format = 5
title = "Ship to a release branch"
[[steps]]
id = "implement"
title = "Implement"
actor = { kind = "agent" }
[[steps.checklist]]
key = "pr_open"
title = "PR open"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{work_branch}}" }
[[steps.checklist]]
key = "checks_somewhere"
title = "Checks on the checkout's branch"
probe = "forge_checks"
probe_args = { repo = "{{repo}}" }
[[steps.checklist]]
key = "release_merged"
title = "The release branch merged"
probe = "forge_merged"
probe_args = { repo = "{{repo}}", branch = "{{release_branch}}" }
"""


def test_only_evidence_about_the_picked_pr_is_bound_to_its_number(setup, tmp_path, monkeypatch):
    """The independent review on #1793: an item on ANOTHER branch variable, or with no branch
    (the checkout's), is never bound to the picked PR — its gate would be unknown for ever."""
    folder, _body = setup
    contents = _tmp._files()
    contents["playbook.toml"] = _tmp.MANIFEST.replace(
        "[[materials]]", '[[variables]]\nname = "release_branch"\nrequired = true\n[[materials]]'
    )
    contents["flows/rel.toml"] = REL_FLOW
    bindings = [*_tmp.BINDINGS, {"name": "release_branch", "kind": "text", "value": "release/1"}]
    d = _tmp._deploy(folder, tmp_path, monkeypatch, contents, bindings)
    fake = FakeForge([_pr(11, "feature/a")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid = d["mission"]
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "rel")
    offered = mission_procedures.input_choices(mid, index, listing["listing"])
    pick = next(c for c in offered["inputs"]["work_branch"] if c.get("number") == 11)
    mission_procedures.apply_listed(
        mid,
        index,
        listing["listing"],
        inputs={"work_branch": {"choice": pick["index"]}},
        choices=offered["choices"],
    )
    rows = _rows(mid)
    assert rows["o1--pr_open"]["probe_args"]["number"] == 11
    assert "number" not in rows["o1--release_merged"]["probe_args"]
    assert "number" not in (rows["o1--checks_somewhere"]["probe_args"] or {})
    missions.reset_schema_cache_for_test()


TWO_FLOW = """format = 5
title = "Two pull requests"
[[steps]]
id = "first"
title = "First"
actor = { kind = "agent" }
[[steps.checklist]]
key = "first_merged"
title = "The first PR merged"
probe = "forge_merged"
probe_args = { repo = "{{repo}}", branch = "{{work_branch}}" }
[[steps]]
id = "second"
title = "Second"
actor = { kind = "agent" }
[[steps.checklist]]
key = "second_merged"
title = "The second PR merged"
probe = "forge_merged"
probe_args = { repo = "{{repo}}", branch = "{{release_branch}}" }
"""


def test_two_picks_in_one_apply_each_bind_their_own_pr_through_the_probe(
    setup, tmp_path, monkeypatch
):
    """Hermes on #1793 (7417): two branch inputs picked as #31 and #32, which share one head —
    each piece of evidence is bound to ITS PR, stored, and observed as that PR by the probe."""
    from agent_sessions import mission_probes

    folder, _body = setup
    contents = _tmp._files()
    contents["playbook.toml"] = _tmp.MANIFEST.replace(
        "[[materials]]", '[[variables]]\nname = "release_branch"\nrequired = true\n[[materials]]'
    )
    contents["flows/two.toml"] = TWO_FLOW
    bindings = [*_tmp.BINDINGS, {"name": "release_branch", "kind": "text", "value": "release/1"}]
    d = _tmp._deploy(folder, tmp_path, monkeypatch, contents, bindings)
    prs = {31: _pr(31, "feature/c"), 32: _pr(32, "feature/c")}
    fake = FakeForge(list(prs.values()))
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid = d["mission"]
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "two")
    offered = mission_procedures.input_choices(mid, index, listing["listing"])

    def at(var, number):
        return next(c["index"] for c in offered["inputs"][var] if c.get("number") == number)

    mission_procedures.apply_listed(
        mid,
        index,
        listing["listing"],
        inputs={
            "work_branch": {"choice": at("work_branch", 31)},
            "release_branch": {"choice": at("release_branch", 32)},
        },
        choices=offered["choices"],
    )
    rows = _rows(mid)
    assert rows["o1--first_merged"]["probe_args"]["number"] == 31
    assert rows["o1--second_merged"]["probe_args"]["number"] == 32
    client = _routed(prs, merged={32})
    monkeypatch.setattr(mission_probes, "_client_for", lambda target: client)
    first = mission_probes.probe_one({"cwd": ""}, rows["o1--first_merged"])
    second = mission_probes.probe_one({"cwd": ""}, rows["o1--second_merged"])
    assert (first.extra["number"], first.value) == (31, False)
    assert (second.extra["number"], second.value) == (32, True)
    missions.reset_schema_cache_for_test()


def test_each_item_binds_the_one_pick_it_is_about():
    picks = {
        "work_branch": {"number": 31, "repo": "org/app", "value": "a"},
        "release_branch": {"number": 32, "repo": "org/app", "value": "b"},
    }
    steps = {"work_branch": {"impl"}, "release_branch": {"rel"}}
    for ref, number in (
        ("{{work_branch}}", 31),
        ("{{release_branch}}", 32),
        ("{{steps.impl.head_branch}}", 31),
        ("{{steps.rel.head_branch}}", 32),
    ):
        item = {
            "probe": "forge_review",
            "probe_args": None,
            "pending": {"probe": "forge_review", "args": {"repo": "org/app"}, "refs": {}},
        }
        mission_procedures._bind_number(item, picks, ref, steps)
        assert item["pending"]["args"]["number"] == number, ref


def test_a_step_reference_into_a_step_of_several_branch_variables_is_never_bound():
    """Only one of its two variables picked: the step's output may come from either, so the
    consumer is left to branch resolution rather than bound to a number it may not match."""
    item = {
        "probe": "forge_review",
        "probe_args": None,
        "pending": {"probe": "forge_review", "args": {"repo": "org/app"}, "refs": {}},
    }
    picks = {"work_branch": {"number": 31, "repo": "org/app", "value": "a"}}
    steps = {"work_branch": {"impl"}, "release_branch": {"impl"}}
    mission_procedures._bind_number(item, picks, "{{steps.impl.head_branch}}", steps)
    assert "number" not in item["pending"]["args"]


SHARED_FLOW = """format = 5
title = "Two PRs, one implementation step"
[[steps]]
id = "impl"
title = "Implement both"
actor = { kind = "agent" }
outputs = ["head_branch"]
[[steps.checklist]]
key = "work_pr"
title = "The work PR is open"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{work_branch}}" }
[[steps.checklist]]
key = "release_pr"
title = "The release PR is open"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{release_branch}}" }
[[steps]]
id = "ship"
title = "Ship"
actor = { kind = "operator" }
after = ["impl"]
[[steps.checklist]]
key = "shipped"
title = "Merged"
probe = "forge_merged"
probe_args = { repo = "{{repo}}", branch = "{{steps.impl.head_branch}}" }
"""


def test_a_shared_step_output_carries_its_producers_pr_through_bridge_and_probe(
    setup, tmp_path, monkeypatch
):
    """Hermes on #1793 (7420): `impl` takes two branch variables, picked as #31 and #32 on one
    head. The consumer is unbound at apply; whichever producer settles names it — and its PR
    NUMBER travels through the bridge, so the probe asks about that PR, not the first on the
    branch."""
    from agent_sessions import mission_probes

    folder, _body = setup
    contents = _tmp._files()
    contents["playbook.toml"] = _tmp.MANIFEST.replace(
        "[[materials]]", '[[variables]]\nname = "release_branch"\nrequired = true\n[[materials]]'
    )
    contents["flows/shared.toml"] = SHARED_FLOW
    bindings = [*_tmp.BINDINGS, {"name": "release_branch", "kind": "text", "value": "release/1"}]
    d = _tmp._deploy(folder, tmp_path, monkeypatch, contents, bindings)
    prs = {31: _pr(31, "feature/c"), 32: _pr(32, "feature/c")}
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: FakeForge(list(prs.values())))
    mid = d["mission"]
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "shared")
    offered = mission_procedures.input_choices(mid, index, listing["listing"])

    def at(var, number):
        return next(c["index"] for c in offered["inputs"][var] if c.get("number") == number)

    mission_procedures.apply_listed(
        mid,
        index,
        listing["listing"],
        inputs={
            "work_branch": {"choice": at("work_branch", 31)},
            "release_branch": {"choice": at("release_branch", 32)},
        },
        choices=offered["choices"],
    )
    assert "number" not in _rows(mid)["o1--shipped"]["pending"]["args"]  # unbound at apply
    _set_running(mid)
    client = _routed(prs, merged={32})
    monkeypatch.setattr(mission_probes, "_client_for", lambda target: client)
    release = _rows(mid)["o1--release_pr"]
    fact = mission_probes.probe_one({"cwd": ""}, release)  # the producer bound to #32
    missions.observe_objective(
        mid, "o1--release_pr", observed=True, value=fact.value, extra=fact.extra
    )
    assert missions.bridge_outputs(mid) == ["o1--shipped"]
    shipped = _rows(mid)["o1--shipped"]
    assert shipped["probe_args"] == {"repo": "org/app", "branch": "feature/c", "number": 32}
    merged = mission_probes.probe_one({"cwd": ""}, shipped)
    assert (merged.extra["number"], merged.value) == (32, True)
    missions.reset_schema_cache_for_test()


def _set_running(mid: str) -> None:
    import sqlite3

    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET state='running' WHERE id=?", (mid,))
    con.commit()
    con.close()


IMPLICIT_FLOW = """format = 5
title = "Implicit repository consumer"
[[steps]]
id = "impl"
title = "Implement"
actor = { kind = "agent" }
outputs = ["head_branch"]
[[steps.checklist]]
key = "work_pr"
title = "The work PR is open"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{work_branch}}" }
[[steps]]
id = "ship"
title = "Ship"
actor = { kind = "operator" }
after = ["impl"]
[[steps.checklist]]
key = "shipped"
title = "Merged"
probe = "forge_merged"
probe_args = { branch = "{{steps.impl.head_branch}}" }
"""


@pytest.mark.parametrize("shared", [False, True], ids=["bound-at-apply", "bound-by-the-bridge"])
def test_an_implicit_repo_consumer_still_asks_about_the_picked_pr(
    setup, tmp_path, monkeypatch, shared
):
    """Hermes on #1793 (7422): a consumer naming no `repo` (the checkout's origin fallback) of a
    producer bound to #32 — #31 shares the head — asks about #32 through apply, bridge and the
    real probe. `shared`: `impl` also takes a second branch variable, so the binding happens in
    the bridge rather than at apply."""
    from agent_sessions import mission_probes

    folder, _body = setup
    flow = IMPLICIT_FLOW
    contents = _tmp._files()
    bindings = list(_tmp.BINDINGS)
    if shared:
        flow = flow.replace(
            '[[steps]]\nid = "ship"',
            '[[steps.checklist]]\nkey = "release_pr"\ntitle = "Release PR"\nprobe = "forge_pr"\n'
            'probe_args = { repo = "{{repo}}", branch = "{{release_branch}}" }\n'
            '[[steps]]\nid = "ship"',
        )
        contents["playbook.toml"] = _tmp.MANIFEST.replace(
            "[[materials]]",
            '[[variables]]\nname = "release_branch"\nrequired = true\n[[materials]]',
        )
        bindings.append({"name": "release_branch", "kind": "text", "value": "release/1"})
    contents["flows/implicit.toml"] = flow
    d = _tmp._deploy(folder, tmp_path, monkeypatch, contents, bindings)
    prs = {31: _pr(31, "feature/c"), 32: _pr(32, "feature/c")}
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: FakeForge(list(prs.values())))
    mid = d["mission"]
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "implicit")
    offered = mission_procedures.input_choices(mid, index, listing["listing"])
    pick = next(c["index"] for c in offered["inputs"]["work_branch"] if c.get("number") == 32)
    mission_procedures.apply_listed(
        mid,
        index,
        listing["listing"],
        inputs={"work_branch": {"choice": pick}},
        choices=offered["choices"],
    )
    waiting = _rows(mid)["o1--shipped"]["pending"]["args"]
    assert waiting == ({} if shared else {"repo": "org/app", "number": 32})
    _set_running(mid)
    client = _routed(prs, merged={32})
    monkeypatch.setattr(mission_probes, "_client_for", lambda target: client)
    producer = _rows(mid)["o1--work_pr"]
    fact = mission_probes.probe_one({"cwd": ""}, producer)
    assert fact.extra["number"] == 32
    missions.observe_objective(
        mid, "o1--work_pr", observed=True, value=fact.value, extra=fact.extra
    )
    assert missions.bridge_outputs(mid) == ["o1--shipped"]
    shipped = _rows(mid)["o1--shipped"]
    assert shipped["probe_args"] == {"repo": "org/app", "branch": "feature/c", "number": 32}
    merged = mission_probes.probe_one({"cwd": ""}, shipped)
    assert (merged.extra["number"], merged.value) == (32, True)
    missions.reset_schema_cache_for_test()
