"""Objectives across projects (#1797, #1613 P4 slice 2)."""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from agent_sessions import mission_directions, mission_probes, missions, projects


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repo(path, remote: str, branch: str):
    path.mkdir()
    _git(path, "init", "-q", "-b", branch)
    _git(path, "remote", "add", "origin", remote)
    (path / "f").write_text("x")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "one")
    return path


@pytest.fixture
def two(tmp_home, tmp_path, monkeypatch):
    """Project A (the mission's) and project B, each with its own checkout, remote and branch."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    missions.reset_schema_cache_for_test()
    a = _repo(tmp_path / "a", "https://forge.example/org/alpha.git", "feat-a")
    b = _repo(tmp_path / "b", "https://forge.example/org/beta.git", "feat-b")
    pa = projects.create("Alpha", folders=[str(a)], default_folder=str(a))
    pb = projects.create("Beta", folders=[str(b)], default_folder=str(b))
    mid = missions.create_mission("x", project_id=pa.id, cwd=str(a))["id"]
    for key, project in (("own", pa.id), ("foreign", pb.id)):
        con = missions._ready()
        try:
            con.execute("BEGIN IMMEDIATE")
            op = {"key": f"o1--{key}", "title": key, "probe": "git_local", "gate": True}
            missions._op_add(con, mid, op, "playbook", 1.0, placement={"project_id": project})
            con.execute("COMMIT")
        finally:
            con.close()
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET state='running' WHERE id=?", (mid,))
    con.commit()
    con.close()
    yield {"mission": mid, "a": a, "b": b, "pa": pa, "pb": pb}
    missions.reset_schema_cache_for_test()


def _m_and_rows(mid):
    m = missions.get_mission(mid)
    return m, {o["key"]: o for o in m["objectives"]}


def test_a_foreign_row_is_observed_in_its_own_projects_checkout(two):
    m, rows = _m_and_rows(two["mission"])
    own, foreign = rows["o1--own"], rows["o1--foreign"]
    assert mission_probes.resolve_target(m, own).cwd == str(two["a"])
    tb = mission_probes.resolve_target(m, foreign)
    assert tb.cwd == str(two["b"]) and tb.branch == "feat-b"
    assert tb.remote == "https://forge.example/org/beta.git"
    assert tb.digest != mission_probes.resolve_target(m, own).digest
    fact = mission_probes.probe_one(m, foreign)
    assert fact.extra["branch"] == "feat-b"
    assert mission_probes.probe_one(m, own).extra["branch"] == "feat-a"
    # An implicit-repo forge probe derives B's repository, not A's.
    assert mission_probes._repo_for(mission_probes.objective_view(m, foreign), {}) == "org/beta"


def test_a_single_project_row_resolves_exactly_as_before(two):
    m, rows = _m_and_rows(two["mission"])
    own = rows["o1--own"]
    assert mission_probes.objective_view(m, own) is m
    legacy = {**own, "project_id": None}  # a pre-v34 row: no project of its own
    assert (
        mission_probes.resolve_target(m, legacy).digest
        == mission_probes.resolve_target(m, own).digest
    )


@pytest.mark.parametrize("how", ["archived", "deleted", "no-folder"])
def test_an_unusable_foreign_project_is_no_checkout_and_its_probe_unknown(two, how, monkeypatch):
    m, rows = _m_and_rows(two["mission"])
    if how == "archived":
        projects.update(two["pb"].id, archived=True)
    elif how == "deleted":
        monkeypatch.setattr(projects, "load", lambda *a, **k: {two["pa"].id: two["pa"]})
    else:
        bare = projects.Project(id=two["pb"].id, name="Beta")
        monkeypatch.setattr(projects, "load", lambda *a, **k: {two["pb"].id: bare})
    assert mission_probes.resolve_target(m, rows["o1--foreign"]).cwd == ""
    assert mission_probes.probe_one(m, rows["o1--foreign"]).observed is False


_REAL_PROBE = mission_probes.probe_one


def _run_with(monkeypatch, mid, side_effect):
    real = _REAL_PROBE

    def probe(mission, obj, *, target=None):
        fact = real(mission, obj, target=target)
        side_effect(obj)
        return fact

    monkeypatch.setattr(mission_probes, "probe_one", probe)
    mission_probes.run_for_mission(mid)
    return _m_and_rows(mid)[1]


def test_the_fence_binds_each_row_to_its_own_checkout(two, monkeypatch):
    """B's checkout moving mid-probe discards B's answer; A's moving does not touch B's."""

    def move_b(obj):
        if obj["key"] == "o1--foreign":
            _git(two["b"], "commit", "-q", "--allow-empty", "-m", "moved")

    rows = _run_with(monkeypatch, two["mission"], move_b)
    assert rows["o1--foreign"]["state"] == "pending"  # discarded: B moved under its own probe
    assert rows["o1--own"]["state"] == "met"

    def move_a(obj):
        if obj["key"] == "o1--foreign":
            _git(two["a"], "commit", "-q", "--allow-empty", "-m", "moved a")

    rows = _run_with(monkeypatch, two["mission"], move_a)
    assert rows["o1--foreign"]["state"] == "met"  # A's checkout is not B's destination


def test_a_foreign_merge_never_becomes_the_missions_merge_sha(two, monkeypatch):
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_objectives SET probe='forge_merged', probe_args=? WHERE key='o1--foreign'",
        ('{"repo": "org/beta", "branch": "feat-b"}',),
    )
    con.commit()
    con.close()

    def fake(mission, obj, *, target=None):
        if obj["key"] == "o1--foreign":
            return mission_probes.Fact.seen(True, "merged", sha="beef" * 10)
        return mission_probes.Fact.unknown("not this test")

    monkeypatch.setattr(mission_probes, "probe_one", fake)
    mission_probes.run_for_mission(two["mission"])
    m, rows = _m_and_rows(two["mission"])
    assert rows["o1--foreign"]["state"] == "met"
    assert not m.get("merge_sha")
    assert mission_probes.objective_view(m, rows["o1--foreign"])["merge_sha"] == ""


def test_a_foreign_row_is_never_directed_into_the_missions_session(two):
    snap = missions.objective_snapshot(two["mission"], "o1--foreign")
    assert snap["project_id"] == two["pb"].id and snap["mission_project_id"] == two["pa"].id
    with pytest.raises(mission_directions.NotRenderable, match="another project"):
        mission_directions.render(snap, {})
    own = missions.objective_snapshot(two["mission"], "o1--own")
    assert not mission_probes.is_foreign({"project_id": own["mission_project_id"]}, own)


# ---- the review on #1896: every consumer of a row's evidence or of its session ------------------


def _set_probe(mid: str, probe: str) -> None:
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE mission_objectives SET probe=? WHERE mission_id=?", (probe, mid))
    con.commit()
    con.close()


def test_a_foreign_judged_row_is_never_judged_from_the_missions_checkout(two):
    """The judge reads THIS mission's checkout and sessions; a row in another project is left
    unjudged rather than settled from evidence that is not its own."""
    mid = two["mission"]
    _set_probe(mid, "supervisor_judged")
    wl = missions.judge_worklist(mid)
    assert wl["cwd"] == str(two["a"])
    assert [r["key"] for r in wl["rows"]] == ["o1--own"]
    assert "project_id" not in wl["rows"][0]


def test_the_probe_answers_from_the_bound_target_not_a_second_lookup(two, tmp_path, monkeypatch):
    """The project's folder is a target input: a probe given a target asks THAT checkout, even if
    the project's folder moved after it was bound."""
    m, rows = _m_and_rows(two["mission"])
    tgt = mission_probes.resolve_target(m, rows["o1--foreign"])
    c = _repo(tmp_path / "c", "https://forge.example/org/gamma.git", "feat-c")
    monkeypatch.setattr(mission_probes, "_project_checkout", lambda _pid: str(c))
    fact = mission_probes.probe_one(m, rows["o1--foreign"], target=tgt)
    assert fact.extra["branch"] == "feat-b"


def test_the_supervisor_never_nudges_or_drafts_for_a_foreign_row(two):
    from agent_sessions import mission_supervisor as sup

    mid = two["mission"]
    a = sup.assess(mid)
    by_key = {o["key"]: o for o in a["objectives"]}
    assert by_key["o1--foreign"]["foreign"] and not by_key["o1--own"]["foreign"]
    for key, kept in (("o1--foreign", False), ("o1--own", True)):
        reply = {
            "assessment": "on_track",
            "nudge": {"objective_key": key, "why": "w"},
            "draft": {"objective_key": key, "text": "do it", "confidence": 0.9},
        }
        out = sup._reading(reply, a, "fp")
        got = (out["draft"] or {}).get("objective_key") or (out["nudge"] or {}).get("objective_key")
        assert (got == key) is kept, key
    allowed, why = sup.may_nudge(mid, "o1--foreign", held_sessions=1)
    assert not allowed and why == sup.FOREIGN_WHY
    assert sup.may_nudge(mid, "o1--own", held_sessions=1)[1] != sup.FOREIGN_WHY


def test_an_unreadable_mission_holds_every_row_back_from_the_supervisor(two, monkeypatch):
    from agent_sessions import mission_supervisor as sup

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(missions, "procedure_scope", boom)
    assert all(o["foreign"] for o in sup.assess(two["mission"])["objectives"])
