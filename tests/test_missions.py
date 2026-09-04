"""The mission record — store invariants (#846, Phase 1 of #840).

The store is the thing Phases 2–6 are all clients of, so what is pinned here is the *contract*
rather than the row count: the lifecycle graph and its lost-race semantics, the schema ``CHECK``
that stops a cwd-less mission launching, exclusive session ownership as a database property,
the derived attention flag, the settlement projection that outlives ledger compaction, the
retention bounds, and the durable archive's crash points.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from agent_sessions import missions

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
CLAUDE_B = "claude:22222222-2222-2222-2222-222222222222"


def _pending_ids(**kw) -> list[str]:
    """Just the ids from the recovery worklist, which pages on `(objectives_at, id)`."""
    return [mid for _, mid in missions.missions_awaiting_objectives(**kw)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh store per test — NEVER the operator's real one."""
    db = tmp_path / "missions.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    yield db
    missions.reset_schema_cache_for_test()


def _running(instruction="do the thing", cwd="/repo"):
    """A mission in `running`, which is where most of the interesting rules live."""
    m = missions.create_mission(instruction, cwd=cwd)
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    return m["id"]


# ---- schema, path, migration ---------------------------------------------------


def test_path_is_env_overridable_and_never_the_real_home(store, monkeypatch):
    assert missions._db_path() == store
    monkeypatch.delenv("AGENT_SESSIONS_MISSIONS_DB")
    # The default lives in the operator's config dir, not under ~/.claude.
    assert missions._db_path().name == "missions.db"
    assert ".config/agent-sessions" in str(missions._db_path())


def test_db_file_is_0600_from_creation(store):
    missions.create_mission("hello")
    # 0600 from creation matters, not 0600 eventually: the file carries the operator's verbatim
    # instruction, and a widened-then-narrowed window is still a window.
    assert oct(os.stat(store).st_mode & 0o777) == "0o600"


def test_migration_runs_from_user_version_zero_and_stamps_the_version(store):
    missions.create_mission("hello")
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"missions", "mission_sessions", "mission_objectives", "mission_events"} <= names
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] in (0, 1)
    finally:
        con.close()


def test_a_newer_schema_is_refused_rather_than_corrupted(store):
    missions.create_mission("hello")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    con.execute(f"PRAGMA user_version={missions.SCHEMA_VERSION + 5}")
    con.close()
    with pytest.raises(missions.MissionError):
        missions.create_mission("second")


# ---- the draft / cwd CHECK -----------------------------------------------------


def test_a_draft_may_exist_with_no_cwd(store):
    """The ambiguous-project case: a draft is created BEFORE the project is resolved, which is
    exactly when the console needs to ask which of two matching projects was meant."""
    m = missions.create_mission("implement issue #813 in abc")
    assert m["state"] == "draft"
    assert m["cwd"] is None


def test_dispatch_is_refused_until_a_cwd_is_chosen(store):
    m = missions.create_mission("implement issue #813 in abc")
    missions.set_state(m["id"], "draft", "planned")
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(m["id"], "planned", "dispatching")
    assert e.value.status == 409
    assert "cwd" in str(e.value)


def test_the_check_is_in_the_schema_not_only_in_python(store):
    """Belt and braces: the CHECK must refuse the write even if the transition layer is bypassed."""
    m = missions.create_mission("no project yet")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE missions SET state='running' WHERE id=?", (m["id"],))
    finally:
        con.close()


def test_a_cwdless_mission_cannot_fail_but_can_be_abandoned(store):
    """The CHECK exempts draft/planned/abandoned only, and that is kept rather than widened: a
    plan that never launched has not *failed*, it was *abandoned*."""
    m = missions.create_mission("no project yet")
    missions.set_state(m["id"], "draft", "planned")
    with pytest.raises(missions.MissionError):
        missions.set_state(m["id"], "planned", "failed")
    out = missions.set_state(m["id"], "planned", "abandoned", outcome="abandoned")
    assert out["state"] == "abandoned"


# ---- lifecycle -----------------------------------------------------------------


def test_the_transition_map_is_the_whole_graph(store):
    m = missions.create_mission("x", cwd="/repo")
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(m["id"], "draft", "running")
    assert e.value.status == 409
    assert "draft cannot become running" in str(e.value)


def test_abandoned_is_terminal_in_the_strong_sense(store):
    m = missions.create_mission("x", cwd="/repo")
    missions.set_state(m["id"], "draft", "abandoned", outcome="abandoned")
    with pytest.raises(missions.MissionError):
        missions.set_state(m["id"], "abandoned", "running")


def test_a_lost_race_is_a_409_not_a_retry(store):
    """Compare-and-set: the caller states the state it believes the mission is in, and a zero
    rowcount is a *lost race*. Retrying would apply a transition decided against a stale read."""
    mid = _running()
    missions.set_state(mid, "running", "review")
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "running", "done", outcome="done")  # stale comparand
    assert e.value.status == 409
    assert "no longer running" in str(e.value)


def test_a_state_change_and_its_event_commit_together(store):
    mid = _running()
    row = missions.get_mission(mid)
    states = [e for e in row["events"] if e["kind"] == "state"]
    assert [(e["meta"]["from"], e["meta"]["to"]) for e in states] == [
        ("dispatching", "running"),
        ("planned", "dispatching"),
        ("draft", "planned"),
    ]


def test_reaching_a_terminal_state_releases_every_session(store):
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    assert missions.holder_of(CLAUDE_A) == mid
    missions.set_state(mid, "running", "done", outcome="done")
    assert missions.holder_of(CLAUDE_A) is None
    # The row STAYS — the roster keeps its history.
    keys = [s["session_key"] for s in missions.get_mission(mid)["sessions"]]
    assert keys == [CLAUDE_A]


def test_reopen_reacquires_nothing(store):
    """A terminal state released the sessions, and another mission may legitimately hold them
    now. Silently taking them back would either steal one or fail the reopen for a reason that
    has nothing to do with reopening — so re-attaching is the ordinary adopt path."""
    a = _running()
    missions.adopt(a, CLAUDE_A)
    missions.set_state(a, "running", "done", outcome="done")
    b = _running()
    missions.adopt(b, CLAUDE_A)  # legal: A released it
    missions.set_state(a, "done", "running")
    assert missions.active_session_keys(a) == []
    assert missions.holder_of(CLAUDE_A) == b


# ---- exclusive session ownership -----------------------------------------------


def test_adopting_a_held_session_is_a_409_naming_the_holder(store):
    a, b = _running(), _running()
    missions.adopt(a, CLAUDE_A)
    with pytest.raises(missions.SessionHeld) as e:
        missions.adopt(b, CLAUDE_A)
    assert e.value.status == 409
    assert e.value.holder == a
    assert a in str(e.value)


def test_two_concurrent_adopts_produce_exactly_one_winner(store):
    """The race the partial unique index exists for. "Check whether anything holds this, then
    insert" is not race-safe — both callers pass the check — so the DATABASE is the arbiter."""
    a, b = _running(), _running()
    results: list[str] = []
    start = threading.Barrier(2)

    def _try(mid):
        start.wait()
        try:
            missions.adopt(mid, CLAUDE_A)
            results.append("won")
        except missions.SessionHeld:
            results.append("lost")

    threads = [threading.Thread(target=_try, args=(m,)) for m in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["lost", "won"]
    assert missions.holder_of(CLAUDE_A) in (a, b)


def test_the_partial_unique_index_is_what_enforces_it(store):
    """Asserted against the schema, not the Python: the index is the invariant."""
    a, b = _running(), _running()
    missions.adopt(a, CLAUDE_A)
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO mission_sessions (mission_id, session_key, role, added_at) "
                "VALUES (?,?,?,?)",
                (b, CLAUDE_A, "primary", time.time()),
            )
    finally:
        con.close()


def test_detach_then_readopt_into_the_same_mission(store):
    """The composite primary key means the historical row survives a detach, so a re-adopt has to
    be an UPDATE — a fresh INSERT would collide with the mission's own history."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)
    assert missions.holder_of(CLAUDE_A) is None
    missions.adopt(mid, CLAUDE_A, role="sub")
    assert missions.holder_of(CLAUDE_A) == mid
    rows = missions.get_mission(mid)["sessions"]
    assert len(rows) == 1 and rows[0]["role"] == "sub"


def test_a_released_session_can_be_adopted_by_another_mission(store):
    a, b = _running(), _running()
    missions.adopt(a, CLAUDE_A)
    missions.detach(a, CLAUDE_A)
    missions.adopt(b, CLAUDE_A)
    assert missions.holder_of(CLAUDE_A) == b


def test_detaching_a_session_the_mission_does_not_hold_is_a_404(store):
    mid = _running()
    with pytest.raises(missions.MissionError) as e:
        missions.detach(mid, CLAUDE_B)
    assert e.value.status == 404


# ---- objectives ----------------------------------------------------------------


def _add(mid, key, *, gate=False, probe="none", **kw):
    return missions.patch_objectives(
        mid, [{"op": "add", "key": key, "title": key, "gate": gate, "probe": probe, **kw}]
    )


def test_an_edit_never_marks_an_objective_met(store):
    """`state` / `met_at` / `observed` are not writable on the operator path AT ALL, so no edit
    can back-date a met — the rule that keeps the objective list evidence rather than opinion."""
    mid = _running()
    _add(mid, "pr_open", gate=True, probe="forge_pr")
    with pytest.raises(missions.MissionError):
        missions.patch_objectives(mid, [{"op": "add", "key": "x", "title": "x", "state": "met"}])
    # …and the accepted ops leave state alone.
    rows = missions.patch_objectives(mid, [{"op": "retitle", "key": "pr_open", "title": "PR up"}])
    assert rows[0]["state"] == "pending" and rows[0]["met_at"] is None


def test_waive_is_a_decision_not_a_claim(store):
    mid = _running()
    _add(mid, "deploy", gate=True)
    rows = missions.patch_objectives(mid, [{"op": "waive", "key": "deploy"}])
    assert rows[0]["state"] == "waived"
    assert rows[0]["met_at"] is None and rows[0]["observed"] is None


def test_adding_an_unmet_gate_to_a_review_mission_reopens_it(store):
    mid = _running()
    missions.set_state(mid, "running", "review")
    _add(mid, "late_gate", gate=True, probe="forge_checks")
    row = missions.get_mission(mid)
    assert row["state"] == "running"
    reopen = [e for e in row["events"] if e["kind"] == "state"][0]
    assert reopen["meta"]["to"] == "running"
    assert "unmet gating objective" in reopen["meta"]["why"]


def test_adding_a_non_gating_objective_does_not_reopen_review(store):
    mid = _running()
    missions.set_state(mid, "running", "review")
    _add(mid, "just_a_note", gate=False)
    assert missions.get_mission(mid)["state"] == "review"


def test_agent_judged_may_never_gate(store):
    """ "the agent believes it wrote tests" is not evidence that it did."""
    mid = _running()
    with pytest.raises(missions.MissionError) as e:
        _add(mid, "tests", gate=True, probe="agent_judged")
    assert e.value.status == 422
    # Non-gating is fine — it is a visibly weaker kind, not a forbidden one.
    assert _add(mid, "tests", gate=False, probe="agent_judged")[0]["probe"] == "agent_judged"


def test_an_unknown_probe_kind_is_refused(store):
    mid = _running()
    with pytest.raises(missions.MissionError):
        _add(mid, "weird", probe="curl_whatever")


def test_reorder_must_list_every_objective_exactly_once(store):
    mid = _running()
    _add(mid, "a")
    _add(mid, "b")
    with pytest.raises(missions.MissionError):
        missions.patch_objectives(mid, [{"op": "reorder", "keys": ["a"]}])
    rows = missions.patch_objectives(mid, [{"op": "reorder", "keys": ["b", "a"]}])
    assert [r["key"] for r in rows] == ["b", "a"]


def test_unmet_gates_ignores_met_and_waived(store):
    mid = _running()
    _add(mid, "one", gate=True)
    _add(mid, "two", gate=True)
    _add(mid, "note", gate=False)
    missions.patch_objectives(mid, [{"op": "waive", "key": "two"}])
    assert missions.unmet_gates(mid) == ["one"]


# ---- settlement projection ------------------------------------------------------


def test_a_decision_survives_ledger_compaction(store, tmp_path, monkeypatch):
    """The compaction regression. The ledger bounds its terminal tail GLOBALLY (`HISTORY_MAX`),
    which is right for a feed and wrong for a mission that outlives it — without the projection
    an old mission renders a decision with no content."""
    from agent_sessions import orchestrator_ledger as ledger

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    mid = _running()
    ledger.append(
        {
            "id": "act-old",
            "state": "proposed",
            "verb": "choose",
            "rationale": "the prompt is waiting on 1 or 2",
            "session_id": CLAUDE_A,
        }
    )
    missions.append_event(mid, "approval", action_id="act-old", text="approve?")
    ledger.compare_and_set("act-old", ledger.REJECTABLE_STATES, "rejected", detail="declined")

    # Bury it past the history bound and compact: the ledger row is gone…
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"noise-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-old") is None

    # …and the mission still renders the decision, with its verb, outcome and rationale.
    event = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-old"][0]
    assert event["settlement"]["verb"] == "choose"
    assert event["settlement"]["state"] == "rejected"
    assert event["settlement"]["outcome"] == "declined"
    assert "waiting on 1 or 2" in event["settlement"]["rationale"]


def test_the_projection_is_written_once_and_never_updated(store, tmp_path, monkeypatch):
    from agent_sessions import orchestrator_ledger as ledger

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    mid = _running()
    ledger.append({"id": "act-1", "state": "proposed", "verb": "nudge"})
    missions.append_event(mid, "approval", action_id="act-1")
    ledger.transition("act-1", "delivered", detail="first")
    # A second settlement (a later transition, a recovery pass) must not rewrite history.
    assert missions.record_settlement("act-1", {"state": "failed", "detail": "second"}) == 0
    event = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-1"][0]
    assert event["settlement"]["outcome"] == "first"


def test_a_missions_failure_never_undoes_a_settled_ledger_transition(store, tmp_path, monkeypatch):
    from agent_sessions import orchestrator_ledger as ledger

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(
        missions, "record_settlement", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ledger.append({"id": "act-2", "state": "proposed"})
    assert ledger.transition("act-2", "delivered")["state"] == "delivered"
    assert ledger.get("act-2")["state"] == "delivered"


# ---- derived attention ----------------------------------------------------------


def test_needs_you_is_derived_from_the_ledger_not_stored(store, tmp_path, monkeypatch):
    from agent_sessions import orchestrator_ledger as ledger

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False

    ledger.append({"id": "a1", "state": "escalated", "session_id": CLAUDE_A})
    assert missions.derive_needs_you([mid])[mid]["why"] == ["decision"]

    # Deciding it in the bell clears the flag with no write to the missions store — which is the
    # whole point of deriving it. A stored flag would still be saying "needs you".
    ledger.compare_and_set("a1", ledger.REJECTABLE_STATES, "rejected")
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False


def test_needs_you_ignores_a_session_the_mission_has_released(store, tmp_path, monkeypatch):
    from agent_sessions import orchestrator_ledger as ledger

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)
    ledger.append({"id": "a2", "state": "proposed", "session_id": CLAUDE_A})
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False


def test_needs_you_reads_intervention_from_the_same_sidecar_pulse_does(store, monkeypatch):
    from agent_sessions import metadata

    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    monkeypatch.setattr(
        metadata,
        "load",
        lambda *a, **k: {CLAUDE_A: metadata.SessionMeta(intervention_required=True)},
    )
    assert missions.derive_needs_you([mid])[mid]["why"] == ["intervention"]


def test_an_open_question_needs_you_until_it_is_answered(store):
    # Driven through `open_question` / `answer_question` rather than by appending bare events.
    # This test predates the producer (#846 shipped the flag against nothing), and an open
    # question is now a HOLD on an objective, not "a `question` event newer than any `answer`" —
    # which could not express a superseded question or an answer to a different one (#892).
    mid = _running()
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr_open", "title": "A PR is open", "gate": True}]
    )
    q = missions.open_question(
        mid,
        "pr_open",
        "which project?",
        [
            {"label": "this one", "action": "note_answer"},
            {"label": "the other", "action": "note_answer"},
        ],
    )
    assert missions.derive_needs_you([mid])[mid]["why"] == ["question"]
    missions.answer_question(mid, q["seq"], option_index=0)
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False


def test_a_ledger_hiccup_degrades_the_flag_never_the_list(store, monkeypatch):
    from agent_sessions import orchestrator_ledger as ledger

    mid = _running()
    monkeypatch.setattr(
        ledger, "live_actions", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert missions.derive_needs_you([mid])[mid]["needs_you"] is False


# ---- retention + event cap -------------------------------------------------------


def test_the_event_cap_drops_recaps_and_keeps_decisions(store):
    mid = _running()
    missions.append_event(mid, "approval", action_id="keep-me")
    for i in range(missions.MISSION_EVENTS_MAX + 20):
        missions.append_event(mid, "recap", text=f"recap {i}")
    # The cap holds…
    assert missions.event_count(mid) == missions.MISSION_EVENTS_MAX
    con = sqlite3.connect(str(store))
    try:
        # …by dropping the oldest RECAPS…
        assert (
            con.execute("SELECT COUNT(*) FROM mission_events WHERE kind='recap'").fetchone()[0]
            < missions.MISSION_EVENTS_MAX + 20
        )
        # …and the decision survives, because trimming it would erase the settlement projection
        # that exists precisely to survive this growth.
        assert (
            con.execute("SELECT COUNT(*) FROM mission_events WHERE action_id='keep-me'").fetchone()[
                0
            ]
            == 1
        )
        # As do the lifecycle rows.
        assert (
            con.execute("SELECT COUNT(*) FROM mission_events WHERE kind='state'").fetchone()[0] == 3
        )
    finally:
        con.close()


def test_retention_deletes_closed_missions_and_their_rows(store):
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    _add(mid, "one")
    old = time.time() - 400 * 86400
    missions.set_state(mid, "running", "done", outcome="done", now=old)
    assert missions.retention_pass() == 1
    assert missions.get_mission(mid) is None
    # A row delete, not a tombstone: the children go with it (sensitive operator text).
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        for table in ("mission_sessions", "mission_objectives", "mission_events"):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    finally:
        con.close()


def test_retention_leaves_open_missions_alone(store):
    mid = _running()
    assert missions.retention_pass() == 0
    assert missions.get_mission(mid) is not None


def test_retention_window_is_env_overridable(store, monkeypatch):
    mid = _running()
    missions.set_state(mid, "running", "done", outcome="done", now=time.time() - 3 * 86400)
    assert missions.retention_pass() == 0
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_RETENTION_DAYS", "1")
    assert missions.retention_pass() == 1


# ---- reads fail soft, writes fail loudly -------------------------------------------


def test_a_corrupt_store_empties_the_rail_with_a_stated_reason(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"this is not a database" * 100)
    missions.reset_schema_cache_for_test()
    out = missions.safe_list_missions()
    assert out["missions"] == [] and out["total"] == 0
    assert out["store_error"] and "missions store unavailable" in out["store_error"]


def test_a_write_failure_surfaces_rather_than_being_swallowed(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"this is not a database" * 100)
    missions.reset_schema_cache_for_test()
    with pytest.raises(sqlite3.DatabaseError):
        missions.create_mission("a dropped write is indistinguishable from one that worked")


def test_the_store_error_never_carries_mission_content(store):
    """`instruction` is sensitive operator text; sqlite messages are about the FILE, never a row."""
    missions.create_mission("token sk-secret-do-not-leak")
    con = sqlite3.connect(str(store))
    con.execute("BEGIN EXCLUSIVE")
    try:
        missions.reset_schema_cache_for_test()
        out = missions.safe_list_missions()
        assert "sk-secret-do-not-leak" not in (out["store_error"] or "")
    finally:
        con.close()


# ---- listing: filter before paginate ------------------------------------------------


def test_filters_apply_before_the_page_so_total_is_the_filtered_total(store):
    for i in range(6):
        missions.create_mission(f"m{i}", title=f"alpha {i}" if i < 2 else f"beta {i}")
    out = missions.list_missions(q="alpha", limit=1)
    assert out["total"] == 2  # the FILTERED total, not the page and not the whole set
    assert len(out["missions"]) == 1


def test_facets_are_computed_before_the_filters(store):
    missions.create_mission("a", project_id="p1", cwd="/a")
    missions.create_mission("b", project_id="p2", cwd="/b")
    out = missions.list_missions(project_id="p1")
    assert out["total"] == 1
    # Both options stay listed regardless of the current filter — the dropdown must not collapse
    # to the thing already selected.
    assert out["facets"]["projects"] == ["p1", "p2"]


def test_archived_missions_are_a_separate_scope(store):
    mid = _running()
    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)
    missions.finish_archive(mid)
    assert missions.list_missions()["total"] == 0
    assert missions.list_missions(archived=True)["total"] == 1


# ---- timeline paging ------------------------------------------------------------------


def test_the_timeline_pages_on_seq_so_a_concurrent_append_cannot_shift_it(store):
    mid = _running()
    for i in range(10):
        missions.append_event(mid, "recap", text=f"r{i}")
    first = missions.get_mission(mid, events_limit=4)
    assert len(first["events"]) == 4
    cursor = first["events_next_seq"]
    missions.append_event(mid, "recap", text="arrived mid-read")
    second = missions.get_mission(mid, events_limit=4, events_before_seq=cursor)
    # An offset-keyed page would have shifted by one and repeated a row; a seq cursor cannot.
    assert not {e["seq"] for e in first["events"]} & {e["seq"] for e in second["events"]}
    assert all(e["seq"] < cursor for e in second["events"])


def test_readopting_a_held_session_refreshes_the_role_but_not_its_place_in_history(store):
    mid = _running()
    missions.adopt(mid, CLAUDE_A, role="primary")
    first = missions.get_mission(mid)["sessions"][0]["added_at"]
    rows = missions.adopt(mid, CLAUDE_A, role="sub")["sessions"]
    assert len(rows) == 1
    assert rows[0]["role"] == "sub"
    assert rows[0]["added_at"] == first  # re-adopting what you already hold is not re-joining


def test_the_rail_scope_query_can_use_an_index(store):
    """A parameterised scope (`(archived_at IS NOT NULL) = ?`) reads neatly and is not sargable —
    it turns the rail's own query into a full scan. Asserted on the query plan, because the
    behaviour is identical either way and only the plan tells you which one shipped."""
    missions.create_mission("a", cwd="/a")
    con = missions._ready()
    try:
        plan = " ".join(
            str(r["detail"]) for r in con.execute(f"EXPLAIN QUERY PLAN {missions._LIST_LIVE_SQL}")
        )
    finally:
        con.close()
    assert "missions_by_archived" in plan, plan


# ---- data remanence: the property must be OURS, not the build's ------------------------


def test_secure_delete_is_set_explicitly_not_inherited_from_the_build(store):
    """Debian/Ubuntu compile SQLite with `SQLITE_SECURE_DELETE=1`, so on this host — and in CI,
    which runs on it — freed pages were zeroed by the *build* and no test could see the pragma
    missing. On a build without it, an operator's instruction survived every delete.

    Proven the only way that survives a different libsqlite3: force the adverse default, then
    assert our connection turns it back on."""
    con = missions._ready()
    try:
        assert con.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        con.close()

    plain = sqlite3.connect(str(store))
    try:
        plain.execute("PRAGMA secure_delete=OFF")
        assert plain.execute("PRAGMA secure_delete").fetchone()[0] == 0
    finally:
        plain.close()
    # A fresh connection from the module sets it regardless of what anyone else left behind —
    # it is per-connection, so inheriting is not a thing that can happen.
    con = missions._ready()
    try:
        assert con.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        con.close()


def test_retention_scrubs_the_bytes_it_deletes(store):
    secret = "sk-retention-should-erase-this-" + "q" * 48  # noqa: S105 — test fixture value
    mid = missions.create_mission(secret, cwd="/repo")["id"]
    for i in range(40):
        missions.create_mission(f"filler {i} " + "x" * 200, cwd="/repo")
    missions.set_state(
        mid, "draft", "abandoned", outcome="abandoned", now=time.time() - 400 * 86400
    )
    con = missions._ready()
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        con.close()
    assert secret.encode() in store.read_bytes()
    assert missions.retention_pass() == 1
    for suffix in ("", "-wal", "-shm"):
        f = store.with_name(store.name + suffix)
        if f.exists():
            assert secret.encode() not in f.read_bytes(), f"plaintext survived in {f.name}"


# ---- bounds that are actually bounds ----------------------------------------------------


def test_the_event_cap_is_a_real_cap_even_for_preserved_kinds(store):
    """ "Preserved" cannot mean "unbounded": an operator retitling one objective in a loop grew the
    timeline forever — measured at 521 rows against a stated cap of 500. Past the hard ceiling,
    preserved rows go too, and the ones carrying a settlement projection go LAST."""
    mid = _running()
    _add(mid, "one")
    missions.append_event(mid, "approval", action_id="decision-1")
    missions.record_settlement("decision-1", {"state": "delivered", "verb": "nudge"})
    for i in range(missions.MISSION_EVENTS_HARD_MAX + 50):
        missions.patch_objectives(mid, [{"op": "retitle", "key": "one", "title": f"t{i}"}])
    assert missions.event_count(mid) <= missions.MISSION_EVENTS_HARD_MAX
    con = missions._ready()
    try:
        # The projection is the durability promise the cap exists to protect, so it is the last
        # thing sacrificed to a size bound — not the first.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM mission_events WHERE action_id='decision-1'"
            ).fetchone()[0]
            == 1
        )
    finally:
        con.close()


def test_an_oversized_probe_args_is_refused_not_silently_corrupted(store):
    """Truncating serialized JSON at an arbitrary character stores invalid JSON that reads back as
    None — the caller is told the write succeeded and the data is silently gone.

    The refusal now comes from the FIELD's own length cap rather than the serialization budget,
    which is a better error (it names the field), and the property the test exists for is
    unchanged: refused, and nothing stored.
    """
    mid = _running()
    with pytest.raises(missions.MissionError) as e:
        _add(mid, "big", probe="http_status", probe_args={"url": "https://x/" + "y" * 4000})
    assert e.value.status == 422 and "url is longer than" in str(e.value)
    assert missions.objectives(mid) == []


def test_the_serialization_budget_still_bites_where_it_is_reachable():
    """The column backstop, asserted where it can actually fire.

    Per-field caps have made it unreachable through `_validate_probe` for every current kind. That
    is a reason to pin it directly, not a reason to delete it: it guards the COLUMN, so a future
    kind with generous field caps of its own must meet a budget rather than discover it by writing
    truncated JSON.
    """
    with pytest.raises(missions.MissionError) as e:
        missions._json_or_none({"k": "v" * (missions.PROBE_ARGS_MAX + 1)}, missions.PROBE_ARGS_MAX)
    assert e.value.status == 422 and "too large" in str(e.value)


def test_reorder_rejects_a_duplicate_key(store):
    mid = _running()
    _add(mid, "a")
    _add(mid, "b")
    with pytest.raises(missions.MissionError):
        missions.patch_objectives(mid, [{"op": "reorder", "keys": ["a", "b", "b"]}])
    assert [o["key"] for o in missions.objectives(mid)] == ["a", "b"]


# ---- an outcome must agree with the state it settles --------------------------------------


def test_an_outcome_must_match_the_state_it_is_recorded_against(store):
    mid = _running()
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "running", "review", outcome="done")
    assert "only meaningful on a terminal state" in str(e.value)
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "running", "done", outcome="abandoned")
    assert "does not match state" in str(e.value)
    assert missions.set_state(mid, "running", "done", outcome="done")["outcome"] == "done"


def test_a_gate_flag_must_be_a_real_boolean(store):
    mid = _running()
    with pytest.raises(missions.MissionError) as e:
        missions.patch_objectives(mid, [{"op": "add", "key": "x", "title": "x", "gate": "false"}])
    assert e.value.status == 422 and "true or false" in str(e.value)


def test_objectives_on_a_missing_mission_raise_404(store):
    with pytest.raises(missions.MissionNotFound):
        missions.objectives(f"msn_{'b' * 32}")


# ---- a consistent read -----------------------------------------------------------------


def test_one_response_is_one_snapshot(store, monkeypatch):
    """In autocommit each SELECT saw its own snapshot, so a legal transition committing between
    them returned `state="running"` beside a `done` event and a released session — a mission whose
    parts disagree, which is exactly what the timeline exists to prevent.

    Driven deterministically: a proxy connection commits the transition at the precise gap, right
    after the mission row is read and before the events are.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    real_ready = missions._ready
    fired: list[bool] = []

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def __getattr__(self, name):
            return getattr(self._con, name)

        def execute(self, sql, *a, **k):
            out = self._con.execute(sql, *a, **k)
            if sql.startswith("SELECT * FROM missions WHERE id=?") and not fired:
                fired.append(True)
                monkeypatch.setattr(missions, "_ready", real_ready)
                missions.set_state(mid, "running", "done", outcome="done")
                monkeypatch.setattr(missions, "_ready", _wrap)
            return out

    def _wrap(*a, **k):
        return _Proxy(real_ready(*a, **k))

    monkeypatch.setattr(missions, "_ready", _wrap)
    row = missions.get_mission(mid)
    monkeypatch.setattr(missions, "_ready", real_ready)
    assert fired, "the race was never driven"

    if row["state"] == "running":
        # The snapshot predates the commit, so EVERYTHING in it must predate the commit too.
        assert not any(
            e["kind"] == "state" and e["meta"].get("to") == "done" for e in row["events"]
        )
        assert row["sessions"][0]["removed_at"] is None
    else:
        assert row["state"] == "done"
        assert row["sessions"][0]["removed_at"] is not None


def test_a_v1_store_migrates_forward_in_place(store):
    """Migrations are explicit and tested, not implicit: an install upgraded in place is a code
    path with a test rather than a hope."""
    missions.create_mission("before the upgrade", cwd="/repo")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:  # rewind to v1: drop the column v2 adds, and the version stamp with it
        con.execute("ALTER TABLE missions DROP COLUMN unarchiving_at")
        con.execute("PRAGMA user_version=1")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    # A v1 store opens, migrates, and keeps its rows.
    rows = missions.list_missions()
    assert rows["total"] == 1 and rows["missions"][0]["instruction"] == "before the upgrade"
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
        assert "unarchiving_at" in cols
    finally:
        con.close()
    # …and the v2 feature works on it.
    assert missions.pending_unarchives() == []


# ---- a busy checkpoint is not a scrub ---------------------------------------------------


def test_a_delete_that_cannot_scrub_says_so_rather_than_claiming_success(store):
    """Reproduced for real, not stubbed: a second connection holds a read snapshot, which is
    exactly what pins the log.

    Ignoring the busy result reported physical deletion that had not happened — and this asserts
    the uncomfortable half too: the plaintext genuinely IS still in the file at that moment. The
    contract of this function is that the bytes are gone, so when they are not, it says so.
    """
    secret = "sk-must-not-be-claimed-gone-" + "z" * 48  # noqa: S105 — test fixture value
    mid = missions.create_mission(secret, cwd="/repo")["id"]
    for i in range(40):  # filler, so the victim page is neither the whole file nor reused
        missions.create_mission(f"filler {i} " + "x" * 200, cwd="/repo")

    reader = sqlite3.connect(str(store))
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM missions").fetchone()
    try:
        with pytest.raises(missions.ScrubFailed) as e:
            missions.delete_mission(mid)
        assert e.value.status == 503
        assert "could not be scrubbed" in str(e.value)
        assert secret not in str(e.value)  # names the mission, never its content
        # The rows ARE gone — that transaction committed. Only the scrub is unproven…
        assert missions.get_mission(mid) is None
        # …and this is what "unproven" actually means on disk.
        assert secret.encode() in store.read_bytes()
    finally:
        reader.rollback()
        reader.close()

    # Once the reader lets go, the next scrub opportunity finishes the job.
    con = missions._ready()
    try:
        assert missions._scrub(con) is True
    finally:
        con.close()
    for suffix in ("", "-wal", "-shm"):
        f = store.with_name(store.name + suffix)
        if f.exists():
            assert secret.encode() not in f.read_bytes(), f"plaintext survived in {f.name}"


def test_a_successful_scrub_reports_success(store):
    mid = missions.create_mission("ordinary", cwd="/repo")["id"]
    assert missions.delete_mission(mid) is True


# ---- malformed request types are 422s, not 500s -------------------------------------------


def test_a_malformed_field_type_is_a_422_not_an_unhashable_typeerror(store):
    """`x in frozenset` raises `TypeError: unhashable type` for a dict or list, which escapes the
    route as a 500. A malformed body is a client error."""
    mid = _running()
    for bad in ({}, [], 7, None):
        with pytest.raises(missions.MissionError) as e:
            missions.set_state(mid, "running", "done", outcome=bad if bad is not None else {})
        assert e.value.status == 422
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "running", {})
    assert e.value.status == 422 and "must be a string" in str(e.value)
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(mid, CLAUDE_A, role={})
    assert e.value.status == 422 and "must be a string" in str(e.value)


# ---- a reopened mission is not also "finished" ---------------------------------------------


def test_reopening_clears_the_terminal_outcome(store):
    """`outcome=COALESCE(?, outcome)` kept it, so a reopened mission read `state='running',
    outcome='done'` — a record saying both "in flight" and "finished, successfully"."""
    mid = _running()
    done = missions.set_state(mid, "running", "done", outcome="done")
    assert done["outcome"] == "done" and done["closed_at"] is not None
    reopened = missions.set_state(mid, "done", "running")
    assert reopened["outcome"] is None
    assert reopened["closed_at"] is None


def test_the_public_event_writer_honours_the_lifecycle_fence(store):
    """`append_event` is what later phases write recaps and probe results through, so "every other
    mutation is fenced" has to include it."""
    mid = _running()
    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)
    with pytest.raises(missions.MissionError) as e:
        missions.append_event(mid, "recap", text="the supervisor kept talking")
    assert e.value.status == 409


def test_a_v2_store_migrates_forward_to_v3(store):
    """The projection moves off the event row into its own table, and an in-place upgrade must
    carry the rows across rather than dropping them."""
    mid = _running()
    missions.append_event(mid, "approval", action_id="act-old")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:  # rewind to v2: put the column back, drop the table, seed a projection on the row
        con.execute("ALTER TABLE mission_events ADD COLUMN settlement TEXT")
        con.execute("DROP TABLE mission_settlements")
        con.execute(
            "UPDATE mission_events SET settlement=? WHERE action_id='act-old'",
            ('{"verb":"choose","state":"rejected","rationale":"why","outcome":"declined"}',),
        )
        con.execute("PRAGMA user_version=2")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    row = missions.get_mission(mid)
    assert row is not None
    ev = [e for e in row["events"] if e["action_id"] == "act-old"][0]
    assert ev["settlement"]["verb"] == "choose"
    assert ev["settlement"]["outcome"] == "declined"
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(mission_events)")}
        assert "settlement" not in cols  # the column is gone; the table is the home now
    finally:
        con.close()


def test_a_v1_store_walks_every_step_to_current(store):
    """Migrations apply in ascending order, so a file two versions behind arrives where a fresh
    one starts rather than skipping the step in between."""
    missions.create_mission("ancient", cwd="/repo")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE missions DROP COLUMN unarchiving_at")
        con.execute("ALTER TABLE missions DROP COLUMN unarchive_sessions")
        con.execute("ALTER TABLE missions DROP COLUMN op_token")
        con.execute("ALTER TABLE mission_sessions DROP COLUMN release_reason")
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_owner")
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_at")
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_token")
        con.execute("DROP TABLE mission_settlements")
        con.execute("DROP TABLE session_reservations")
        con.execute("DROP TABLE store_flags")
        con.execute("ALTER TABLE mission_events ADD COLUMN settlement TEXT")
        con.execute("PRAGMA user_version=1")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    assert missions.list_missions()["total"] == 1
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
        assert {"unarchiving_at", "unarchive_sessions"} <= cols  # the v2 step was not skipped
        assert "op_token" in cols  # …nor the v4 one
        scols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
        assert {"release_reason", "lease_owner", "lease_at", "lease_token"} <= scols  # v5-v8
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"mission_settlements", "store_flags", "session_reservations"} <= names
    finally:
        con.close()


def test_a_failed_scrub_leaves_a_durable_obligation_a_later_call_discharges(store):
    """ "Retry" was a no-op: the rows are already deleted, so the next call returned before ever
    reaching the scrub. The obligation has to outlive the call that created it."""
    secret = "sk-owed-scrub-" + "z" * 48  # noqa: S105 — test fixture value
    mid = missions.create_mission(secret, cwd="/repo")["id"]
    for i in range(40):
        missions.create_mission(f"filler {i} " + "x" * 200, cwd="/repo")

    reader = sqlite3.connect(str(store))
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM missions").fetchone()
    try:
        with pytest.raises(missions.ScrubFailed):
            missions.delete_mission(mid)
        # The obligation is recorded, not merely announced…
        con = missions._ready()
        try:
            assert missions._flag_get(con, missions.SCRUB_PENDING) == "1"
        finally:
            con.close()
        # …and while the reader holds on, a retry still cannot discharge it — but it TRIES,
        # which is the part that used to be missing.
        assert missions.scrub_if_pending() is False
        assert secret.encode() in store.read_bytes()
    finally:
        reader.rollback()
        reader.close()

    # Once the reader lets go, the obligation is collected and the flag clears.
    assert missions.scrub_if_pending() is True
    assert missions.scrub_if_pending() is None  # nothing owed any more
    for suffix in ("", "-wal", "-shm"):
        f = store.with_name(store.name + suffix)
        if f.exists():
            assert secret.encode() not in f.read_bytes(), f"plaintext survived in {f.name}"


def test_a_later_delete_discharges_an_obligation_from_an_earlier_one(store):
    secret = "sk-owed-then-collected-" + "z" * 40  # noqa: S105 — test fixture value
    a = missions.create_mission(secret, cwd="/repo")["id"]
    b = missions.create_mission("something else", cwd="/repo")["id"]
    for i in range(40):
        missions.create_mission(f"filler {i} " + "x" * 200, cwd="/repo")

    reader = sqlite3.connect(str(store))
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM missions").fetchone()
    try:
        with pytest.raises(missions.ScrubFailed):
            missions.delete_mission(a)
    finally:
        reader.rollback()
        reader.close()

    # An ordinary later delete carries the earlier obligation with it.
    assert missions.delete_mission(b) is True
    con = missions._ready()
    try:
        assert missions._flag_get(con, missions.SCRUB_PENDING) is None
    finally:
        con.close()
    assert secret.encode() not in store.read_bytes()


def test_retention_with_nothing_to_prune_still_discharges_an_owed_scrub(store):
    con = missions._ready()
    try:
        missions._flag_set(con, missions.SCRUB_PENDING, "1")
    finally:
        con.close()
    assert missions.retention_pass() == 0  # no victims…
    con = missions._ready()
    try:
        assert missions._flag_get(con, missions.SCRUB_PENDING) is None  # …but the debt is paid
    finally:
        con.close()


def test_a_v4_store_migrates_forward_to_v5(store):
    """v5 records WHY a session left the roster. Existing released rows default to `closed`,
    which is the behaviour they already had — before this column there was no detach distinction
    to preserve."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE mission_sessions DROP COLUMN release_reason")
        con.execute("PRAGMA user_version=4")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    rows = missions.get_mission(mid)["sessions"]
    assert rows[0]["release_reason"] == "closed"
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
    finally:
        con.close()


def test_a_v3_store_migrates_forward_to_v4(store):
    missions.create_mission("before v4", cwd="/repo")
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE missions DROP COLUMN op_token")
        con.execute("DROP TABLE store_flags")
        con.execute("PRAGMA user_version=3")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    assert missions.list_missions()["total"] == 1
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
        assert "op_token" in cols
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "store_flags" in names
    finally:
        con.close()


def test_a_mission_cannot_be_deleted_mid_operation(store):
    """The same hazard retention already refuses, on the other deletion path: the row carries the
    archive journal recovery works from, and the roster naming the agents it was about to reap."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)
    with pytest.raises(missions.MissionError) as e:
        missions.delete_mission(mid)
    assert e.value.status == 409 and "in flight" in str(e.value)
    assert missions.get_mission(mid) is not None
    assert missions.pending_archives() == [mid]


def test_a_v5_store_migrates_forward_to_v6(store):
    """v6 records which process holds a lease, so recovery can tell a crashed worker's from a
    live one's instead of assuming."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_owner")
        con.execute("PRAGMA user_version=5")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()

    assert missions.get_mission(mid) is not None
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
        assert "lease_owner" in cols
    finally:
        con.close()


def test_a_v6_store_migrates_forward_to_v7(store):
    """v7 stamps WHEN a lease was taken, so one can expire. Ownership alone was not enough: the
    write that releases a lease can fail too, and a lease owned by the CURRENT process is one
    recovery refuses to touch — so a transient failure stranded the row until a restart."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_at")
        con.execute("PRAGMA user_version=6")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()
    assert missions.get_mission(mid) is not None
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
        assert "lease_at" in cols
    finally:
        con.close()


def test_a_lease_expires_so_reclamation_needs_no_other_write_to_succeed(store):
    """The emergency settlement that releases a lease can itself fail — and it is best-effort, so
    it does. Age is the one property that does not depend on another write landing at exactly the
    moment things are already going wrong."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"

    # Ours and fresh: recovery must not touch it.
    assert missions.reopen_stale_leases(mid) == 0
    # Ours and old: it is abandoned, whoever owns it.
    assert missions.reopen_stale_leases(mid, now=time.time() + missions.LEASE_MAX_AGE_S + 1) == 1
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"


def test_a_v7_store_migrates_forward_to_v8(store):
    """v8 turns the read-only guard into a reservation with a fencing token."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.reset_schema_cache_for_test()
    con = sqlite3.connect(str(store))
    try:
        con.execute("ALTER TABLE mission_sessions DROP COLUMN lease_token")
        con.execute("DROP TABLE session_reservations")
        con.execute("PRAGMA user_version=7")
        con.commit()
    finally:
        con.close()
    missions.reset_schema_cache_for_test()
    assert missions.get_mission(mid) is not None
    con = sqlite3.connect(str(store))
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
        assert "lease_token" in cols
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "session_reservations" in names
    finally:
        con.close()


# ---------------------------------------------------------------- Phase 3a boundaries (#883)


def _pb(**over):
    """A playbook block with one template, valid unless a test breaks it deliberately."""
    obj = {"key": "pr_open", "title": "A PR is open", "probe": "forge_pr", "gate": True}
    obj.update(over.pop("objective", {}))
    return {
        "default_id": over.pop("default_id", "ship"),
        "playbooks": [{"id": "ship", "label": "Ship", "objectives": [obj]}],
    }


def test_a_model_row_may_never_carry_a_probe(store, monkeypatch):
    """AUTHORITY BY SOURCE. A model row is a NOTE: it may name an objective and nothing else.

    Carrying a probe would launder model text into an operator-authored field, which is the SSRF
    surface this phase closes. Checked at the WRITE boundary so a future caller fails loudly
    rather than quietly widening it.
    """
    mid = missions.create_mission("do it", path=store)["id"]
    with pytest.raises(missions.MissionError) as e:
        missions.instantiate_objectives(
            mid,
            [
                {
                    "key": "k",
                    "title": "T",
                    "probe": "http_status",
                    "probe_args": {"url": "http://169.254.169.254/"},
                    "source": "model",
                }
            ],
            path=store,
        )
    assert e.value.status == 422
    assert missions.objectives(mid, path=store) == []


def test_instantiation_is_atomic_across_a_MIXED_batch(store):
    """No partial objective list survives a rejected batch — asserted for a mixed
    playbook/model batch, because that is the one the instantiator actually writes.

    A half-instantiated plan the operator cannot tell is partial is worse than no plan.
    """
    mid = missions.create_mission("do it", path=store)["id"]
    with pytest.raises(missions.MissionError):
        missions.instantiate_objectives(
            mid,
            [
                {"key": "good", "title": "fine", "probe": "forge_pr", "source": "playbook"},
                {"key": "note_1", "title": "a note", "source": "model"},
                # …and one that must take the whole batch down with it.
                {"key": "bad", "title": "T", "probe": "http_status", "source": "model"},
            ],
            path=store,
        )
    assert missions.objectives(mid, path=store) == [], "a partial list survived"


def test_instantiation_cannot_mint_operator_authority(store):
    """The boundary assigns `source` itself; a caller cannot ask for one it should not have."""
    mid = missions.create_mission("do it", path=store)["id"]
    with pytest.raises(missions.MissionError) as e:
        missions.instantiate_objectives(
            mid, [{"key": "k", "title": "T", "source": "operator"}], path=store
        )
    assert e.value.status == 500


def test_an_ABSENT_playbook_id_uses_the_configured_default(store, monkeypatch, tmp_path):
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    prefs.set_mission_playbooks(_pb())
    mid = missions.create_mission("do it", path=store)["id"]
    status, tpl = missions.templates_for_mission(mid, path=store)
    assert status == "ok"
    assert [t["key"] for t in tpl] == ["pr_open"]


def test_an_UNKNOWN_playbook_id_offers_NOTHING_rather_than_the_default(
    store, monkeypatch, tmp_path
):
    """The asymmetry that matters. "You did not choose" and "what you chose is gone" are
    different facts, and substituting the default on the second silently instantiates gating
    objectives with operator-authored probe targets nobody picked for this mission."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    prefs.set_mission_playbooks(_pb())
    mid = missions.create_mission("do it", playbook_id="deleted_one", path=store)["id"]
    status, tpl = missions.templates_for_mission(mid, path=store)
    assert status == "unknown_playbook"
    assert tpl == [], "an unknown playbook fell back to the default"


def test_a_STALE_default_id_offers_nothing_rather_than_the_first_playbook(
    store, monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    # Written through the raw store, because `set_mission_playbooks` is strict and would refuse
    # this — which is exactly the hand-edited case the read path exists to survive.
    from agent_sessions.atomicjson import atomic_write_json

    atomic_write_json(tmp_path / "p.json", {"mission_playbooks": {**_pb(), "default_id": "gone"}})
    mid = missions.create_mission("do it", path=store)["id"]
    status, tpl = missions.templates_for_mission(mid, path=store)
    assert status == "no_default"
    assert tpl == []


# ---- the shipped defaults, and the ONE shared value contract (#883 review) ------------
#
# Three findings met here. `DEFAULT_MISSION_PLAYBOOKS` was declared and never consumed, so a
# fresh install read `{"default_id": "", "playbooks": []}` and every mission fell through to
# notes-only. `mission_playbooks` had no `POST /api/prefs` branch, so the only way to configure
# one in production was hand-editing `prefs.json`. And `PROBE_ARG_SCHEMA` checked field NAMES
# without checking VALUES, so both durable paths accepted a `url` that was a list.


def test_a_FRESH_INSTALL_gets_the_shipped_playbooks(tmp_path):
    """Absent means "never configured", and the shipped defaults are what that should read as."""
    from agent_sessions import prefs

    got = prefs.get_mission_playbooks(tmp_path / "nothing-here.json")
    assert got["default_id"] == "ship_a_change"
    assert [p["id"] for p in got["playbooks"]] == ["ship_a_change", "investigate"]


def test_the_shipped_defaults_SURVIVE_STRICT_VALIDATION_unchanged(tmp_path):
    """The constant obeys the rules it is handed to operators as an example of.

    Asserted here rather than at import: validating in `prefs` would import `missions` while
    `prefs` is still being defined.
    """
    from agent_sessions import prefs

    strict = prefs._coerce_mission_playbooks(prefs.DEFAULT_MISSION_PLAYBOOKS, strict=True)
    assert strict == prefs.get_mission_playbooks(tmp_path / "nothing-here.json")


def test_a_PRESENT_but_malformed_block_still_fails_closed(tmp_path):
    """Absence and corruption are different facts. Only the first gets the defaults — handing
    them to a corrupt block would silently replace an operator's config with ours."""
    from agent_sessions import prefs
    from agent_sessions.atomicjson import atomic_write_json

    p = tmp_path / "p.json"
    atomic_write_json(p, {"mission_playbooks": {"playbooks": "not a list"}})
    # `revision` is 0: nothing has ever been WRITTEN through the versioned path, and a corrupt
    # block carries no version of its own to trust (#900 review 5, finding 7).
    assert prefs.get_mission_playbooks(p) == {"default_id": "", "playbooks": [], "revision": 0}


def test_an_operator_who_CLEARS_their_playbooks_does_not_get_them_back(tmp_path):
    """The empty object is a decision, and it is distinguishable from never having decided."""
    from agent_sessions import prefs

    p = tmp_path / "p.json"
    prefs.set_mission_playbooks({"default_id": "", "playbooks": []}, p)
    assert prefs.get_mission_playbooks(p) == {"default_id": "", "playbooks": [], "revision": 1}


@pytest.mark.parametrize(
    "args",
    [
        {"url": ["https://example.com"]},  # a list, not a URL
        {"url": {"href": "https://example.com"}},
        {"url": ""},
        {"url": "example.com/healthz"},  # no scheme: not the protocol they configured
        {"url": "file:///etc/shadow"},
        {"url": "https://example.com", "expect_status": {}},
        {"url": "https://example.com", "expect_status": "200"},
        {"url": "https://example.com", "expect_status": True},  # isinstance(True, int) is True
        {"url": "https://example.com", "expect_status": 7},
        # HOSTLESS targets. All three parsed, all three stored, and all three would have failed
        # only when the Phase 5 runner tried to fetch them (review on #884).
        {"url": "http://?x"},
        {"url": "https://#frag"},
        {"url": "http://:80"},
        {"url": "http://"},
        # …and an authority that is only whitespace, which `urlsplit` hands back as a hostname.
        {"url": "https://  "},
        {"url": "http:// /x"},
        {"url": "http://a b/c"},
        # PORTS. `urlsplit` parses these lazily, so `parts.port` is what raises — every one of
        # these passed the scheme and host checks and only failed when `httpx.Request` refused
        # to build it (review on #884).
        {"url": "http://example.com:abc"},
        {"url": "http://example.com:0"},
        {"url": "http://example.com:99999"},
        {"url": "http://example.com:-1"},
        {"url": "http://example.com:8080x"},
    ],
)
def test_MALFORMED_probe_args_are_refused_by_BOTH_durable_paths(store, tmp_path, args):
    """Paired, because a rule enforced on one path and not the other is not a rule.

    A playbook and an operator objective edit are two different doors to the same stored column,
    and the Phase 5 runner reads the column — it cannot tell which door a target came through,
    so both have to be shut.
    """
    from agent_sessions import prefs

    # Door 1: the playbook prefs write.
    with pytest.raises(prefs.PlaybookError):
        prefs.set_mission_playbooks(
            {
                "default_id": "p",
                "playbooks": [
                    {
                        "id": "p",
                        "label": "P",
                        "objectives": [
                            {
                                "key": "live",
                                "title": "It is live",
                                "probe": "http_status",
                                "probe_args": args,
                            }
                        ],
                    }
                ],
            },
            tmp_path / "p.json",
        )

    # Door 2: the public objective route's write path.
    mid = missions.create_mission("do it", path=store)["id"]
    with pytest.raises(missions.MissionError) as e:
        missions.patch_objectives(
            mid,
            [
                {
                    "op": "add",
                    "key": "live",
                    "title": "It is live",
                    "probe": "http_status",
                    "probe_args": args,
                }
            ],
            path=store,
        )
    assert e.value.status == 422
    assert missions.objectives(mid, path=store) == []


def test_a_hand_edited_malformed_template_DEGRADES_on_read_and_cannot_gate(tmp_path):
    """`prefs.json` is a file a person can edit, so the read path has to say what a bad template
    BECOMES — and a gating objective with no probe can never be met, which would wedge the
    mission rather than protect it."""
    from agent_sessions import prefs
    from agent_sessions.atomicjson import atomic_write_json

    p = tmp_path / "p.json"
    atomic_write_json(
        p,
        {
            "mission_playbooks": {
                "default_id": "p",
                "playbooks": [
                    {
                        "id": "p",
                        "label": "P",
                        "objectives": [
                            {
                                "key": "live",
                                "title": "It is live",
                                "probe": "http_status",
                                "probe_args": {"url": ["nope"]},
                                "gate": True,
                            }
                        ],
                    }
                ],
            }
        },
    )
    (obj,) = prefs.get_mission_playbooks(p)["playbooks"][0]["objectives"]
    assert (obj["probe"], obj["probe_args"], obj["gate"]) == ("none", None, False)
    assert obj["title"] == "It is live", "the operator's intent stayed visible"


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.com/healthz",
        "http://127.0.0.1:8080/x?a=1#f",
        "https://[::1]:9000/health",
        "https://xn--bcher-kva.example/x",
    ],
)
def test_LEGITIMATE_urls_still_pass(url):
    """The control. A host contract that also refuses IPv6 literals or punycode would be a
    different bug wearing the fix's clothes."""
    missions.validate_probe_args("http_status", {"url": url})


@pytest.mark.parametrize("gate", ["false", "true", 1, 0, {}, [], None])
def test_a_playbook_write_REFUSES_a_non_boolean_gate(tmp_path, gate):
    """`bool("false")` is `True`, so a client that stringifies a false value would silently
    create a MANDATORY gate — and a gate nobody intended can strand a mission short of
    completion for ever (review on #884)."""
    from agent_sessions import prefs

    with pytest.raises(prefs.PlaybookError):
        prefs.set_mission_playbooks(
            {
                "default_id": "p",
                "playbooks": [
                    {
                        "id": "p",
                        "label": "P",
                        "objectives": [
                            {"key": "k", "title": "T", "probe": "forge_pr", "gate": gate}
                        ],
                    }
                ],
            },
            tmp_path / "p.json",
        )


def test_a_hand_edited_non_boolean_gate_DEGRADES_on_read(tmp_path):
    """The forgiving half. `prefs.json` is a file a person can edit, and a whole install must not
    lose its playbooks over one bad value — but the row degrades explicitly rather than being
    coerced into whichever gate the truthiness happened to give."""
    from agent_sessions import prefs
    from agent_sessions.atomicjson import atomic_write_json

    p = tmp_path / "p.json"
    atomic_write_json(
        p,
        {
            "mission_playbooks": {
                "default_id": "p",
                "playbooks": [
                    {
                        "id": "p",
                        "label": "P",
                        "objectives": [
                            {"key": "k", "title": "T", "probe": "forge_pr", "gate": "false"}
                        ],
                    }
                ],
            }
        },
    )
    (obj,) = prefs.get_mission_playbooks(p)["playbooks"][0]["objectives"]
    assert (obj["probe"], obj["gate"]) == ("none", False)
    assert obj["title"] == "T", "the operator's intent stayed visible"


def test_the_objectives_intent_UPGRADE_does_not_backfill_history(tmp_path, monkeypatch):
    """The migration path, driven from a real v8 database rather than only a fresh one.

    Only the fresh-install path was exercised, and the risk lives in the upgrade: a mission that
    predates the producer must come through with `objectives_state` NULL and never be retried,
    while a mission created after the upgrade must be pending. Backfilling would propose
    objectives for the entire history at once, on the first boot after an upgrade (#883 review).
    """
    import sqlite3

    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    old_id = missions.create_mission("pre-existing", cwd="/tmp")["id"]

    # Roll a real database back to v8 — drop the columns v9 adds and reset the version stamp.
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE missions DROP COLUMN objectives_state")
    con.execute("ALTER TABLE missions DROP COLUMN objectives_at")
    con.execute("PRAGMA user_version=8")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    assert missions.get_mission(old_id) is not None, "the upgrade lost a mission"
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(missions)")}
    assert {"objectives_state", "objectives_at"} <= cols
    # Against the CONSTANT, not a literal: this test is about BACKFILL, and every later
    # migration would otherwise break it. #881's two and Phase 5a's one each did exactly that.
    assert (
        sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION
    )

    assert (
        _pending_ids() == []
    ), "the upgrade queued every historical mission for objective production"
    fresh = missions.create_mission("post-upgrade", cwd="/tmp")["id"]
    assert _pending_ids() == [fresh]


# ---- the supervisor's durable core (#885, Phase 5a) -----------------------------------------


def test_the_v9_to_v10_UPGRADE_adds_the_supervisor_tables(tmp_path, monkeypatch):
    """Driven from a real v9 database, because the risk in a migration is the upgrade."""
    import sqlite3

    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    mid = missions.create_mission("pre-existing", cwd="/tmp")["id"]

    con = sqlite3.connect(db)
    for t in (
        "mission_supervisor",
        "mission_objective_episode",
        "mission_supervisor_actions",
        "mission_escalations",
    ):
        con.execute(f"DROP TABLE {t}")
    con.execute("PRAGMA user_version=9")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    assert missions.get_mission(mid) is not None, "the upgrade lost a mission"
    names = {
        r[0]
        for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "mission_supervisor",
        "mission_objective_episode",
        "mission_supervisor_actions",
        "mission_escalations",
    } <= names
    assert (
        sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION
    )


def _ensure_objective(mid, key="k", *, path=None):
    """A lifecycle write needs a real objective (#888 review, finding 2) — create it once."""
    if not any(o.get("key") == key for o in missions.objectives(mid, path=path)):
        missions.instantiate_objectives(
            mid,
            [
                {
                    "key": key,
                    "title": key,
                    "probe": "forge_pr",
                    "gate": True,
                    "source": "playbook",
                }
            ],
            path=path,
        )


def test_exactly_ONE_escalation_per_objective_episode(store):
    """The uniqueness constraint is the arbiter, not a preceding check.

    Two overlapping passes both reading "not escalated yet" and both writing is precisely what a
    check-then-insert allows, so the database decides instead.
    """
    mid = missions.create_mission("do it", path=store)["id"]
    _ensure_objective(mid, "checks_green", path=store)
    kw = {"session_key": CLAUDE_A, "objective_key": "checks_green", "path": store}
    assert missions.escalate_once(mid, episode=1, reason="stalled", **kw) is True
    assert missions.escalate_once(mid, episode=1, reason="stalled again", **kw) is False
    # …and a NEW episode may escalate again: easing then stalling later is a new episode, not a
    # continuation of one the operator already saw. The episode has to ACTUALLY advance first —
    # a lifecycle write naming an episode the objective is not on is refused (#888 review,
    # finding 2), which is what stops an in-flight pass resurrecting rows after a drop.
    missions.bump_episode(mid, "checks_green", path=store)
    assert missions.escalate_once(mid, episode=2, reason="stalled later", **kw) is True


def test_the_budget_is_scoped_to_the_EPISODE_and_reset_is_durable(store):
    """ "Resets when the objective moved" is not expressible by derivation alone.

    Re-deriving after progress would still count the earlier nudges, so the boundary is written
    down — and a bump clears any stand-down with it, because a moved objective is a new episode
    and the operator's silence was about the old one.
    """
    mid = missions.create_mission("do it", path=store)["id"]
    _ensure_objective(mid, path=store)
    assert missions.objective_episode(mid, "k", path=store) == (1, False)
    for aid in ("a1", "a2"):
        missions.record_supervisor_action(
            mid,
            session_key=CLAUDE_A,
            objective_key="k",
            episode=1,
            action_id=aid,
            path=store,
        )
    assert missions.supervisor_action_ids(mid, "k", 1, path=store) == ["a1", "a2"]
    # The objective must EXIST for a stand-down to land on it (#888 review, finding 5); it is
    # created by `_ensure_objective` at the top of this test.
    assert missions.stand_down(mid, "k", episode=1, path=store) is True
    assert missions.objective_episode(mid, "k", path=store) == (1, True)

    assert missions.bump_episode(mid, "k", path=store) == 2
    assert missions.objective_episode(mid, "k", path=store) == (
        2,
        False,
    ), "a new episode did not clear the stand-down"
    assert (
        missions.supervisor_action_ids(mid, "k", 2, path=store) == []
    ), "the new episode inherited the old episode's charges"
    assert missions.supervisor_action_ids(mid, "k", 1, path=store) == [
        "a1",
        "a2",
    ], "the old episode's history was destroyed rather than superseded"


def test_a_stand_down_for_a_SUPERSEDED_episode_is_refused(store):
    """The operator's "stop telling me" applies to what they were looking at.

    If the objective moved between the render and the tap, the tap is about an episode that no
    longer exists — silencing the new one would suppress a report nobody has seen.
    """
    mid = missions.create_mission("do it", path=store)["id"]
    missions.bump_episode(mid, "k", path=store)  # now episode 2
    assert missions.stand_down(mid, "k", episode=1, path=store) is False
    assert missions.objective_episode(mid, "k", path=store) == (2, False)


#: The checkpoint is per (mission, SESSION) since v13 — two sessions must not share a row.
SK = "claude:11111111-1111-1111-1111-111111111111"


def test_the_recap_and_the_checkpoint_advance_TOGETHER(store):
    """Two writes, and both orders are broken alone: fingerprint-then-recap loses the recap to a
    crash and never rewrites it (the input now looks unchanged); recap-then-fingerprint writes it
    twice."""
    mid = missions.create_mission("do it", path=store)["id"]
    assert missions.supervisor_checkpoint(mid, session_key=SK, path=store) == {
        "input_fp": None,
        "recap_seq": None,
        "growth_mark": None,
        "growth_at": None,
    }

    seq = missions.advance_checkpoint(
        mid, session_key=SK, input_fp="fp1", recap_text="first recap", path=store
    )
    assert seq is not None
    assert missions.supervisor_checkpoint(mid, session_key=SK, path=store) == {
        "input_fp": "fp1",
        "recap_seq": seq,
        "growth_mark": None,
        "growth_at": None,
    }

    # A fingerprint-only advance keeps the recorded recap rather than blanking it.
    assert missions.advance_checkpoint(mid, session_key=SK, input_fp="fp2", path=store) is None
    assert missions.supervisor_checkpoint(mid, session_key=SK, path=store) == {
        "input_fp": "fp2",
        "recap_seq": seq,
        "growth_mark": None,
        "growth_at": None,
    }
    kinds = [e["kind"] for e in missions.get_mission(mid, path=store)["events"]]
    assert kinds.count("recap") == 1, "a second recap was written for unchanged input"


def test_a_FRESH_install_and_an_UPGRADED_one_get_the_SAME_supervisor_schema(tmp_path, monkeypatch):
    """Each supervisor table is declared TWICE — in the base schema and in the migration.

    Nothing forces the two to agree, and a divergence would appear on only one kind of install:
    the constraint that arbitrates escalations could be present for a fresh operator and missing
    for an upgraded one, or the reverse. Found while red-proofing — a mutation aimed at the
    UNIQUE constraint hit only the migration copy and the test stayed green, because the fixture
    builds from the base schema.
    """
    import re
    import sqlite3

    tables = (
        "mission_supervisor",
        "mission_objective_episode",
        "mission_supervisor_actions",
        "mission_escalations",
    )
    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    missions.create_mission("fresh", cwd="/tmp")

    def ddl(name: str) -> str:
        raw = (
            sqlite3.connect(db)
            .execute("SELECT sql FROM sqlite_master WHERE name=?", (name,))
            .fetchone()[0]
        )
        # Comments and whitespace are prose; the CONSTRAINTS are the contract.
        return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", raw)).strip()

    fresh = {t: ddl(t) for t in tables}

    con = sqlite3.connect(db)
    for t in tables:
        con.execute(f"DROP TABLE {t}")
    con.execute("PRAGMA user_version=9")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    missions.create_mission("upgraded", cwd="/tmp")  # forces the migration

    for t in tables:
        assert ddl(t) == fresh[t], f"{t} differs between a fresh install and an upgraded one"


def test_every_probe_ARGUMENT_publishes_the_json_type_it_takes():
    """Names alone cannot author an argument, and `expect_status` is the proof (#900, finding 6).

    The config route publishes `PROBE_ARG_SCHEMA`'s NAMES so the playbook editor can offer the
    right fields per kind. Every HTML input yields a string; `_arg_status` requires a real
    integer. So the editor offered a field the server could only ever refuse, and the operator
    who typed `204` got "must be an integer" with no way to comply.

    This asserts the type table covers the schema exactly — a contract added later without a wire
    name would otherwise default to `"text"` and reintroduce the same silent mismatch.
    """
    assert set(missions.PROBE_ARG_TYPES) == set(missions.PROBE_ARG_SCHEMA)
    for kind, spec in missions.PROBE_ARG_SCHEMA.items():
        published = missions.PROBE_ARG_TYPES[kind]
        assert set(published) == set(spec), f"{kind}: the type table and the schema disagree"
        for name, (_required, contract) in spec.items():
            assert contract in missions._ARG_TYPE_NAME, (
                f"probe {kind}: argument {name} is validated by a contract with no published "
                "wire type, so an editor cannot know what JSON to send for it"
            )
            assert published[name] == missions._ARG_TYPE_NAME[contract]
    # ...and the one that started this: it is published as an int, not as text.
    assert missions.PROBE_ARG_TYPES["http_status"]["expect_status"] == "int"


def test_an_integer_status_ROUND_TRIPS_through_a_playbook_while_the_string_is_refused(tmp_path):
    """The round trip the review asked for: what the editor now sends is what the store takes."""
    from agent_sessions import prefs

    p = tmp_path / "p.json"
    good = {
        "default_id": "ship",
        "playbooks": [
            {
                "id": "ship",
                "label": "Ship it",
                "objectives": [
                    {
                        "key": "live",
                        "title": "It is live",
                        "probe": "http_status",
                        "probe_args": {"url": "https://example.test/healthz", "expect_status": 204},
                        "gate": True,
                    }
                ],
            }
        ],
    }
    prefs.set_mission_playbooks(good, p)
    back = prefs.get_mission_playbooks(p)
    args = back["playbooks"][0]["objectives"][0]["probe_args"]
    assert args["expect_status"] == 204
    assert isinstance(args["expect_status"], int) and not isinstance(args["expect_status"], bool)

    # ...and the UI-shaped string is still refused, rather than quietly coerced at the boundary.
    import copy

    bad = copy.deepcopy(good)
    bad["playbooks"][0]["objectives"][0]["probe_args"]["expect_status"] = "204"
    with pytest.raises(Exception) as e:
        prefs.set_mission_playbooks(bad, p)
    assert "integer" in str(e.value)


def test_a_MIGRATION_STEP_survives_a_store_that_never_had_the_table(tmp_path, monkeypatch):
    """A step runs against what the PREVIOUS version left behind, not against its full schema.

    `mission_objectives` is in the base `CREATE` and therefore in every store built from v1 — but
    a step that assumes it raises `no such table` on any store where it is absent, and an upgrade
    that raises is an install that will not start. The ladder's own v9 regression builds a store
    with two tables in it and caught this; the rule deserves its own test, because the next step
    added here will be written the same way.

    Red against an unguarded `ALTER TABLE` / `SELECT` in the step.
    """
    import sqlite3 as sq

    db = tmp_path / "partial.db"
    con = sq.connect(db)
    con.executescript(
        "CREATE TABLE missions (id TEXT PRIMARY KEY);"
        "CREATE TABLE mission_turns ("
        "  mission_id TEXT NOT NULL, turn_id TEXT NOT NULL, msg_sha TEXT NOT NULL,"
        "  state TEXT NOT NULL, owner TEXT, owner_at REAL, fence TEXT NOT NULL,"
        "  write_reserved_at REAL, result TEXT, action_ids TEXT, created_at REAL NOT NULL,"
        "  settled_at REAL, PRIMARY KEY (mission_id, turn_id));"
        "PRAGMA user_version=9;"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()

    c = missions._ready(db)
    try:
        assert int(c.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
    finally:
        c.close()

    # …and where the table IS present, the column really is added — the guard skips, it does not
    # silently make the migration a no-op everywhere.
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(fresh))
    missions.reset_schema_cache_for_test()
    c = missions._ready(fresh)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(mission_objectives)")}
    finally:
        c.close()
    assert "incarnation" in cols
