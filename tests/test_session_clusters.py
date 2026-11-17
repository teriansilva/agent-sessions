"""The session-cluster store (#1997): title, placed, protected, merge and dissolve rules, the
strict reader, and the fenced sweep commit. See ``docs/invariants/session-clusters.md``."""

from __future__ import annotations

import json
import threading

import pytest

from agent_sessions import session_clusters as sc

P = "proj_a"
Q = "proj_b"
S1, S2, S3, S4 = "claude:s1", "claude:s2", "claude:s3", "claude:s4"


def _live(*keys: str, project: str = P, **overrides: sc.Live) -> dict[str, sc.Live]:
    out = {k: sc.Live(project_id=project, candidate=True) for k in keys}
    out.update(overrides)
    return out


def _commit(plan: dict, *, keys=(S1, S2, S3, S4), live=None, basis=None, project=P) -> str:
    doc = sc.read_checked()
    return sc.commit_sweep(
        project,
        planned_basis=sc.basis(doc, project) if basis is None else basis,
        plan=plan,
        input_keys=keys,
        live=_live(*keys) if live is None else live,
        fingerprint=lambda d: "fp",
    )


def _ai_cluster(title: str = "Login flow", members=(S1, S2)) -> str:
    """Add one AI cluster through a sweep that keeps every cluster already there."""
    doc = sc.read_checked()
    existing = {
        cid: {
            "title": c["ai_title"],
            "summary": c["summary"],
            "members": [k for k, m in doc["membership"].items() if m["cluster"] == cid],
        }
        for cid, c in doc["clusters"].items()
    }
    plan = {
        "existing": existing,
        "new": [{"title": title, "summary": "s", "members": list(members)}],
    }
    assert _commit(plan) == "committed"
    doc = sc.read_checked()
    return next(cid for cid, c in doc["clusters"].items() if c["ai_title"] == title)


# --- titles ------------------------------------------------------------------------------------


def test_the_shown_title_is_the_operators_when_set_and_the_sweep_never_writes_it():
    cid = _ai_cluster()
    assert sc.cluster_ref(sc.read_checked(), cid) == {
        "id": cid,
        "title": "Login flow",
        "kept": False,
    }
    sc.rename(cid, "  Auth\twork ")
    assert sc.cluster_ref(sc.read_checked(), cid) == {"id": cid, "title": "Auth work", "kept": True}
    # A sweep renames the cluster: the AI title moves, the shown one does not.
    assert (
        _commit({"existing": {cid: {"title": "Sign-in", "summary": "x", "members": [S1, S2]}}})
        == "committed"
    )
    c = sc.read_checked()["clusters"][cid]
    assert (c["ai_title"], c["user_title"]) == ("Sign-in", "Auth work")


def test_keep_keeps_the_title_shown_now_and_let_hq_rename_hands_it_back():
    cid = _ai_cluster()
    assert sc.set_keep(cid, True)["kept"] is True
    assert sc.read_checked()["clusters"][cid]["user_title"] == "Login flow"
    assert sc.set_keep(cid, False) == {"id": cid, "title": "Login flow", "kept": False}
    assert sc.read_checked()["clusters"][cid]["user_title"] is None


@pytest.mark.parametrize("bad", ["", "   ", "\x00\x1b", "x" * 61, None, 7])
def test_an_operator_title_is_refused_not_cut(bad):
    cid = _ai_cluster()
    with pytest.raises(sc.ClustersError):
        sc.rename(cid, bad)
    assert sc.read_checked()["clusters"][cid]["user_title"] is None


def test_clean_text_is_one_bounded_line_without_control_characters():
    assert sc.clean_text("a\x00b\n\tc\x1b[31m  d", 60) == "ab c[31m d"
    assert len(sc.clean_text("x" * 10_000, 60)) == 60
    assert sc.clean_text(["not text"], 60) == ""


# --- operator clusters, placement --------------------------------------------------------------


def test_an_operator_cluster_is_kept_and_survives_empty():
    made = sc.create(P, "Release prep")
    assert made["source"] == "operator" and made["kept"] is True and made["count"] == 0
    assert _commit({}) == "committed"  # a sweep that names nothing
    assert made["id"] in sc.read_checked()["clusters"]


def test_a_project_takes_at_most_twelve_clusters():
    for i in range(sc.MAX_CLUSTERS_PER_PROJECT):
        sc.create(P, f"c{i}")
    with pytest.raises(sc.ClustersError) as e:
        sc.create(P, "one too many")
    assert e.value.status == 409
    sc.create(Q, "another project is not affected")


def test_placing_is_refused_across_projects_and_for_an_unknown_cluster():
    cid = sc.create(P, "Release prep")["id"]
    with pytest.raises(sc.ClustersError) as e:
        sc.place(S1, cid, Q)
    assert e.value.status == 409
    with pytest.raises(sc.ClustersError) as e:
        sc.place(S1, "tc_000000000000", P)
    assert e.value.status == 404
    assert sc.read_checked()["membership"] == {}


def test_the_sweep_never_moves_or_removes_a_placed_session():
    a = _ai_cluster("Login flow", (S1, S2))
    b = sc.create(P, "Release prep")["id"]
    sc.place(S1, b, P)
    # The model puts the placed session back into its old cluster and drops the new one.
    assert (
        _commit({"existing": {a: {"title": "Login flow", "summary": "", "members": [S1, S2, S3]}}})
        == "committed"
    )
    doc = sc.read_checked()
    assert doc["membership"][S1] == {"cluster": b, "placed": True}
    assert doc["membership"][S2]["cluster"] == a and doc["membership"][S3]["cluster"] == a


def test_back_to_hqs_choice_clears_placed_and_leaves_the_session_where_it_is():
    b = sc.create(P, "Release prep")["id"]
    sc.place(S1, b, P)
    sc.unplace([S1, "claude:other-key"])
    assert sc.read_checked()["membership"][S1] == {"cluster": b, "placed": False}


# --- protected, dissolve, merge ----------------------------------------------------------------


def test_an_ai_cluster_below_two_live_members_is_dissolved():
    cid = _ai_cluster("Login flow", (S1, S2))
    assert sc.reconcile(_live(S1, S2)) is False  # nothing to do, nothing written
    assert sc.reconcile(_live(S1), archived=[S2]) is True
    doc = sc.read_checked()
    assert cid not in doc["clusters"] and doc["membership"] == {}


@pytest.mark.parametrize("how", ["kept", "placed"])
def test_a_kept_cluster_or_one_with_a_placed_session_is_not_dissolved_by_the_sweep(how):
    cid = _ai_cluster("Login flow", (S1, S2))
    if how == "kept":
        sc.set_keep(cid, True)
    else:
        sc.place(S1, cid, P)
    sc.reconcile(_live(S1), archived=[S2])
    assert cid in sc.read_checked()["clusters"]
    assert _commit({}, keys=(S1,)) == "committed"
    assert cid in sc.read_checked()["clusters"]


def test_handing_a_singleton_back_to_hq_lets_the_next_sweep_dissolve_it():
    cid = _ai_cluster("Login flow", (S1, S2))
    sc.set_keep(cid, True)
    sc.reconcile(_live(S1), archived=[S2])
    sc.set_keep(cid, False)
    sc.reconcile(_live(S1))
    assert cid not in sc.read_checked()["clusters"]


def test_dissolve_frees_the_members_unplaced_and_leaves_no_tombstone():
    cid = _ai_cluster("Login flow", (S1, S2))
    sc.place(S3, cid, P)
    sc.dissolve(cid)
    assert sc.read_checked() == {
        "version": 1,
        "clusters": {},
        "membership": {},
        "sweeps": {P: sc.read_checked()["sweeps"][P]},
    }
    # The next sweep may group the same sessions again.
    again = _ai_cluster("Login flow", (S1, S2))
    assert again != cid
    with pytest.raises(sc.ClustersError) as e:
        sc.dissolve(cid)
    assert e.value.status == 404


def test_merge_moves_members_keeps_placed_and_a_kept_title_on_the_target_wins():
    a = _ai_cluster("Login flow", (S1, S2))
    b = sc.create(P, "Auth")["id"]
    sc.place(S3, a, P)
    sc.rename(a, "Sign-in")
    out = sc.merge(a, b)
    assert out == {"id": b, "title": "Auth", "kept": True}
    doc = sc.read_checked()
    assert a not in doc["clusters"]
    assert {k: m["cluster"] for k, m in doc["membership"].items()} == {S1: b, S2: b, S3: b}
    assert doc["membership"][S3]["placed"] is True and doc["membership"][S1]["placed"] is False


def test_merge_carries_a_kept_title_into_an_unkept_target():
    a = _ai_cluster("Login flow", (S1, S2))
    b = _ai_cluster("Billing", (S3, S4))
    sc.rename(a, "Sign-in")
    assert sc.merge(a, b) == {"id": b, "title": "Sign-in", "kept": True}


def test_merge_is_refused_across_projects_and_into_itself():
    a = sc.create(P, "A")["id"]
    b = sc.create(Q, "B")["id"]
    with pytest.raises(sc.ClustersError) as e:
        sc.merge(a, b)
    assert e.value.status == 409
    with pytest.raises(sc.ClustersError):
        sc.merge(a, a)
    assert set(sc.read_checked()["clusters"]) == {a, b}


# --- lifecycle: project change, mission, archive ------------------------------------------------


def test_a_session_that_changed_project_loses_its_membership_even_when_placed():
    cid = _ai_cluster("Login flow", (S1, S2))
    sc.place(S3, cid, P)
    live = _live(S1, S2) | {S3: sc.Live(project_id=Q, candidate=True)}
    # Before the sweep reconciles, the stale membership is already not shown.
    doc = sc.read_checked()
    assert sc.effective_cluster(doc, (S3,), Q, mission=False) is None
    sc.reconcile(live)
    assert S3 not in sc.read_checked()["membership"]


def test_a_mission_held_session_has_no_cluster_but_a_placed_one_returns_afterwards():
    cid = _ai_cluster("Login flow", (S1, S2))
    sc.place(S3, cid, P)
    doc = sc.read_checked()
    assert sc.effective_cluster(doc, (S3,), P, mission=True) is None
    held = _live(S1, S2) | {S3: sc.Live(project_id=P, mission=True)}
    sc.reconcile(held)
    doc = sc.read_checked()
    assert doc["membership"][S3] == {"cluster": cid, "placed": True}
    assert sc.effective_cluster(doc, (S3,), P, mission=False) == cid


def test_a_placed_session_missing_from_one_scan_is_kept_an_unplaced_one_is_dropped():
    cid = _ai_cluster("Login flow", (S1, S2))
    sc.place(S3, cid, P)
    sc.reconcile(_live(S1))  # S2 and S3 are not listed (an engine store was unreadable)
    assert set(sc.read_checked()["membership"]) == {S1, S3}


# --- the strict reader -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"clusters": []}),
        json.dumps({"clusters": {"nope": {}}}),
        json.dumps({"clusters": {}, "membership": {"k": {"cluster": 1, "placed": True}}}),
    ],
)
def test_an_unreadable_store_raises_and_is_never_rewritten(content):
    path = sc._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(sc.ClustersUnreadable):
        sc.read_checked()
    for write in (
        lambda: sc.create(P, "x"),
        lambda: sc.reconcile({}),
        lambda: sc.record_sweep(P, fingerprint="", error="e"),
        lambda: _commit({}, basis="whatever"),
    ):
        with pytest.raises(sc.ClustersUnreadable):
            write()
    assert path.read_text() == content


def test_a_missing_store_is_empty_and_the_env_var_moves_it(tmp_path, monkeypatch):
    assert sc.read_checked() == {"version": 1, "clusters": {}, "membership": {}, "sweeps": {}}
    other = tmp_path / "elsewhere" / "c.json"
    monkeypatch.setenv("AGENT_SESSIONS_CLUSTERS", str(other))
    sc.create(P, "x")
    assert other.exists() and oct(other.stat().st_mode & 0o777) == "0o600"


def test_a_write_past_the_size_cap_is_refused_and_leaves_the_document(monkeypatch):
    sc.create(P, "x")
    before = sc._path().read_text()
    monkeypatch.setattr(sc, "MAX_BYTES", 10)
    with pytest.raises(sc.ClustersError) as e:
        sc.create(P, "y")
    assert e.value.status == 507 and sc._path().read_text() == before


# --- the fenced commit -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator_write",
    [
        lambda cid: sc.rename(cid, "Mine"),
        lambda cid: sc.place(S3, cid, P),
        lambda cid: sc.dissolve(cid),
        lambda cid: sc.create(P, "Another"),
        lambda cid: sc.merge(cid, sc.create(P, "Target")["id"]),
    ],
    ids=["rename", "place", "dissolve", "create", "merge"],
)
def test_a_plan_made_before_an_operator_write_is_stale_and_writes_nothing(operator_write):
    cid = _ai_cluster("Login flow", (S1, S2))
    planned = sc.basis(sc.read_checked(), P)
    operator_write(cid)
    after = sc._path().read_text()
    plan = {"existing": {cid: {"title": "Hijacked", "summary": "", "members": [S1, S2, S3, S4]}}}
    assert _commit(plan, basis=planned) == "stale"
    assert sc._path().read_text() == after


def test_a_write_in_another_project_does_not_make_a_plan_stale():
    cid = _ai_cluster("Login flow", (S1, S2))
    planned = sc.basis(sc.read_checked(), P)
    sc.create(Q, "Elsewhere")
    plan = {"existing": {cid: {"title": "Sign-in", "summary": "", "members": [S1, S2]}}}
    assert _commit(plan, basis=planned) == "committed"


def test_two_sweeps_planned_from_one_state_commit_once():
    """A scheduled sweep and "re-cluster now" racing: the second commit sees the first's write."""
    planned = sc.basis(sc.read_checked(), P)
    results: list[str] = []

    def one(title: str) -> None:
        plan = {"new": [{"title": title, "summary": "", "members": [S1, S2]}]}
        results.append(_commit(plan, basis=planned))

    threads = [threading.Thread(target=one, args=(t,)) for t in ("First", "Second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["committed", "stale"]
    assert len(sc.read_checked()["clusters"]) == 1


def test_a_member_that_stopped_being_a_candidate_while_the_model_answered_is_not_placed():
    live = _live(S1, S2) | {
        S3: sc.Live(project_id=Q, candidate=True),  # moved to another project
        S4: sc.Live(project_id=P, mission=True),  # a mission took it
    }
    plan = {"new": [{"title": "Login flow", "summary": "", "members": [S1, S2, S3, S4, "x:gone"]}]}
    assert _commit(plan, live=live) == "committed"
    assert set(sc.read_checked()["membership"]) == {S1, S2}


def test_a_session_the_model_was_not_asked_about_keeps_its_cluster():
    cid = _ai_cluster("Login flow", (S1, S2, S3))
    # The next sweep's input was capped to S1 and S2; the model drops the cluster entirely.
    assert _commit({}, keys=(S1, S2), live=_live(S1, S2, S3)) == "committed"
    doc = sc.read_checked()
    # S3 alone is under two members, so the cluster goes, and S3 with it.
    assert cid not in doc["clusters"] and doc["membership"] == {}
    cid = _ai_cluster("Login flow", (S1, S2, S3, S4))
    assert _commit({}, keys=(S1, S2), live=_live(S1, S2, S3, S4)) == "committed"
    assert set(sc.read_checked()["membership"]) == {S3, S4}


def test_new_ai_clusters_beyond_the_cap_are_dropped_and_protected_ones_stay():
    kept_ids = [sc.create(P, f"op{i}")["id"] for i in range(sc.MAX_CLUSTERS_PER_PROJECT - 1)]
    keys = [f"claude:k{i}" for i in range(6)]
    plan = {
        "new": [
            {"title": "First", "summary": "", "members": keys[0:2]},
            {"title": "Second", "summary": "", "members": keys[2:4]},
            {"title": "Third", "summary": "", "members": keys[4:6]},
        ]
    }
    assert _commit(plan, keys=keys) == "committed"
    doc = sc.read_checked()
    assert len(doc["clusters"]) == sc.MAX_CLUSTERS_PER_PROJECT
    assert set(kept_ids) <= set(doc["clusters"])
    assert [c["ai_title"] for c in doc["clusters"].values() if c["source"] == "ai"] == ["First"]
    assert set(doc["membership"]) == set(keys[0:2])


def test_stored_titles_and_summaries_are_cleaned_and_cut_at_commit():
    plan = {
        "new": [
            {
                "title": "T" * 500 + "\x00",
                "summary": "line one\nline two" + "s" * 900,
                "members": [S1, S2],
            },
            {"title": "\x00\x01", "summary": "no title, no cluster", "members": [S3, S4]},
        ]
    }
    assert _commit(plan) == "committed"
    (c,) = sc.read_checked()["clusters"].values()
    assert c["ai_title"] == "T" * sc.TITLE_MAX
    assert len(c["summary"]) == sc.SUMMARY_MAX and "\n" not in c["summary"]


def test_sweep_records_keep_the_error_bounded_and_forget_clears_fingerprints():
    sc.record_sweep(P, fingerprint="abc", error="boom\n" + "x" * 1000)
    rec = sc.sweep_record(sc.read_checked(), P)
    assert rec["error"].startswith("boom x") and len(rec["error"]) == sc.ERROR_MAX
    sc.forget_sweeps([P])
    doc = sc.read_checked()
    assert doc["sweeps"][P]["fingerprint"] == "" and doc["sweeps"][P]["error"]
    assert sc.sweep_record(doc, Q) is None
