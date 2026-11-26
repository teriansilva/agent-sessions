"""HQ's clustering sweep (#1997) against a stubbed ``review.complete_json`` over real session
files: the gate, the candidates, aliasing, hostile replies, stable ids, the fingerprint skip, the
cap, the error record, and the commit fence while the model is answering."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import (
    clusters_loop,
    engines,
    metadata,
    missions,
    prefs,
    projects,
    review,
)
from agent_sessions import session_clusters as sc

DEFAULT = projects.DEFAULT_PROJECT_ID
REPO = "/home/user/claude/repo-a"


def _key(n: int) -> str:
    return f"claude:{n:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _no_spacing(monkeypatch):
    monkeypatch.setattr(clusters_loop, "CALL_SPACING_S", 0)
    clusters_loop._LOCKS.clear()


@pytest.fixture
def fleet(tmp_home):
    """Six reviewed sessions in one folder (the Default project), newest = highest number."""
    d = tmp_home / ".claude" / "projects" / "-home-user-claude-repo-a"
    d.mkdir(parents=True)
    for n in range(1, 7):
        f = d / f"{_key(n).split(':')[1]}.jsonl"
        f.write_text(
            json.dumps({"type": "user", "cwd": REPO, "message": {"content": f"task {n}"}}) + "\n"
        )
        metadata.patch(_key(n), ai_title=f"Session {n}", ai_summary=f"Works on topic {n}")
    # The sessions are named by their reviews here (renaming has been off by default since #2332).
    prefs.set_session_review({"rename_sessions": True})
    engines.invalidate_scan_cache()
    return tmp_home


@pytest.fixture
def model(monkeypatch):
    """Stub the one model call. ``model.reply`` is the next answer (a dict, a callable taking the
    request payload, or an exception to raise); ``model.calls`` records every request."""

    class Model:
        reply: object = {"clusters": []}
        calls: list[dict] = []

    m = Model()
    m.calls = []

    async def complete_json(messages, *, model=None):
        assert messages[0]["role"] == "system"
        payload = json.loads(messages[1]["content"])
        m.calls.append(payload)
        reply = m.reply(payload) if callable(m.reply) else m.reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(review, "complete_json", complete_json)
    return m


def _run(**kw) -> dict:
    return asyncio.run(clusters_loop.run_sweep(**kw))


def _alias(payload: dict, n: int) -> str:
    return next(s["alias"] for s in payload["sessions"] if s["title"] == f"Session {n}")


def _two_topics(payload: dict) -> dict:
    a = lambda *ns: [_alias(payload, n) for n in ns]  # noqa: E731
    return {
        "clusters": [
            {"ref": "new", "title": "Login flow", "summary": "Sign-in work", "members": a(1, 2, 3)},
            {"ref": "new", "title": "Billing", "summary": "Invoices", "members": a(4, 5)},
        ]
    }


def _clusters() -> dict[str, list[str]]:
    doc = sc.read_checked()
    return {
        sc.shown_title(c): sorted(k for k, m in doc["membership"].items() if m["cluster"] == cid)
        for cid, c in doc["clusters"].items()
    }


# --- gate ---------------------------------------------------------------------------------------


def test_the_scheduled_sweep_is_a_no_op_without_an_endpoint_or_when_turned_off(fleet, model):
    assert asyncio.run(clusters_loop.sweep())["skipped"] == "disabled"
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test/v1",
            "api_key": "sk-test",  # noqa: S106
            "model": "m",
        }
    )
    assert clusters_loop.ready() is True
    prefs.set_clusters({"enabled": False})
    assert asyncio.run(clusters_loop.sweep())["skipped"] == "disabled"
    assert model.calls == [] and not sc._path().exists()
    prefs.set_clusters({"enabled": True})
    model.reply = _two_topics
    assert asyncio.run(clusters_loop.sweep())["projects"][0]["status"] == "committed"


def test_the_env_kill_switch_stops_the_loop(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_CLUSTERS_LOOP", "0")
    asyncio.run(clusters_loop.run())  # returns at once instead of sleeping forever


# --- candidates and aliasing --------------------------------------------------------------------


def test_the_model_sees_aliases_titles_and_summaries_never_session_keys(fleet, model):
    metadata.patch(_key(6), ai_summary="x" * 900)
    model.reply = _two_topics
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "committed"}]
    (payload,) = model.calls
    raw = json.dumps(payload)
    assert "claude:" not in raw and "aaaa-4aaa" not in raw and "tc_" not in raw
    assert [s["alias"] for s in payload["sessions"]] == [f"s{i}" for i in range(1, 7)]
    assert all(len(s["summary"]) <= clusters_loop.SUMMARY_CHARS for s in payload["sessions"])
    assert payload["clusters"] == []
    assert _clusters() == {
        "Login flow": [_key(1), _key(2), _key(3)],
        "Billing": [_key(4), _key(5)],
    }


def test_archived_excluded_unreviewed_and_mission_held_sessions_are_not_candidates(
    fleet, model, tmp_path
):
    metadata.patch(_key(1), archived=True)
    metadata.patch(_key(2), review_excluded=True)
    metadata.patch(_key(3), ai_title="", ai_summary="")
    mission = missions.create_mission("ship it", title="A mission", cwd=str(tmp_path))
    missions.adopt(mission["id"], _key(4))
    engines.invalidate_scan_cache()
    got = clusters_loop.collect()
    assert [c.key for c in got.candidates[DEFAULT]] == [_key(6), _key(5)]
    assert _key(1) in got.archived and _key(1) not in got.live
    assert got.live[_key(4)].mission is True and got.live[_key(2)].candidate is False
    # Even when the model names them, none is grouped.
    model.reply = lambda p: {
        "clusters": [
            {"ref": "new", "title": "All", "summary": "", "members": ["s1", "s2", "s3", "s4", "s9"]}
        ]
    }
    _run()
    assert _clusters() == {"All": [_key(5), _key(6)]}


def test_the_input_is_capped_to_the_most_recently_active_sessions(fleet, model, monkeypatch):
    monkeypatch.setattr(clusters_loop, "MAX_CANDIDATES", 3)
    _run()
    assert len(model.calls[0]["sessions"]) == 3


def test_an_unreadable_missions_store_stops_the_pass_before_any_model_call(
    fleet, model, monkeypatch, tmp_path
):
    broken = tmp_path / "missions-db-is-a-directory"
    broken.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(broken))
    missions.reset_schema_cache_for_test()
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 — whatever the store raises
            _run()
    finally:
        missions.reset_schema_cache_for_test()
    assert model.calls == []


# --- hostile replies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        [],
        {"groups": []},
        {"clusters": "all of them"},
        {"clusters": ["s1"]},
        {"clusters": [{"ref": "new", "title": "x", "members": "s1"}]},
        {"clusters": [{"ref": "new", "title": "t", "summary": "", "members": []}] * 51},
    ],
)
def test_a_reply_of_the_wrong_shape_writes_no_clusters_and_records_the_error(fleet, model, reply):
    model.reply = reply
    (report,) = _run()["projects"]
    assert report["status"] == "error"
    doc = sc.read_checked()
    assert doc["clusters"] == {} and doc["membership"] == {}
    assert doc["sweeps"][DEFAULT]["error"] == report["error"]
    # The same inputs are not asked again until something changes.
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "unchanged"}]
    assert len(model.calls) == 1


def test_unknown_aliases_duplicates_oversize_titles_and_control_characters_are_dropped(
    fleet, model
):
    def hostile(p):
        a = lambda n: _alias(p, n)  # noqa: E731
        return {
            "clusters": [
                {
                    "ref": "new",
                    "title": "Login\x00 flow\n" + "!" * 500,
                    "summary": "s\x1b]0;x\x07" + "y" * 900,
                    "members": [a(1), a(2), "s99", "claude:" + "0" * 36, 7, None, a(1)],
                },
                # A session already taken stays with the first cluster; the rest is one member.
                {"ref": "new", "title": "Second", "summary": "", "members": [a(2), a(3)]},
                {"ref": "c7", "title": "Unknown ref", "summary": "", "members": [a(4), a(5)]},
                {"ref": "new", "title": "\x00", "summary": "", "members": [a(4), a(5)]},
            ]
        }

    model.reply = hostile
    assert _run()["projects"][0]["status"] == "committed"
    doc = sc.read_checked()
    (c,) = doc["clusters"].values()
    assert c["ai_title"].startswith("Login flow !") and len(c["ai_title"]) == sc.TITLE_MAX
    assert len(c["summary"]) == sc.SUMMARY_MAX and "\x1b" not in c["summary"]
    assert sorted(doc["membership"]) == [_key(1), _key(2)]


def test_a_placed_member_moved_and_a_kept_title_changed_are_both_re_imposed(fleet, model):
    model.reply = _two_topics
    _run()
    doc = sc.read_checked()
    login = next(cid for cid, c in doc["clusters"].items() if c["ai_title"] == "Login flow")
    billing = next(cid for cid, c in doc["clusters"].items() if c["ai_title"] == "Billing")
    sc.rename(login, "Auth")
    sc.place(_key(6), billing, DEFAULT)

    def hostile(p):
        refs = {c["title"]: c for c in p["clusters"]}
        assert refs["Auth"]["title_fixed"] is True
        assert refs["Billing"]["fixed_members"] == [_alias(p, 6)]
        return {
            "clusters": [
                {
                    "ref": refs["Auth"]["ref"],
                    "title": "Renamed by the model",
                    "summary": "",
                    "members": [_alias(p, n) for n in (1, 2, 3, 6)],
                },
                {
                    "ref": refs["Billing"]["ref"],
                    "title": "Billing",
                    "summary": "",
                    "members": [_alias(p, n) for n in (4, 5)],
                },
            ]
        }

    model.reply = hostile
    assert _run()["projects"][0]["status"] == "committed"
    doc = sc.read_checked()
    assert sc.shown_title(doc["clusters"][login]) == "Auth"
    assert doc["membership"][_key(6)] == {"cluster": billing, "placed": True}
    assert _clusters()["Auth"] == [_key(1), _key(2), _key(3)]


# --- stable ids, fingerprint, cap, errors -------------------------------------------------------


def test_a_reused_ref_keeps_its_id_and_an_unchanged_project_is_not_asked_again(fleet, model):
    model.reply = _two_topics
    _run()
    before = {sc.shown_title(c): cid for cid, c in sc.read_checked()["clusters"].items()}
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "unchanged"}]
    assert len(model.calls) == 1

    # A session's summary changes: the project is asked again, with its clusters as refs.
    metadata.patch(_key(6), ai_summary="Now on invoices")
    engines.invalidate_scan_cache()

    def regroup(p):
        refs = {c["title"]: c["ref"] for c in p["clusters"]}
        assert {s["alias"]: s["current"] for s in p["sessions"]}[_alias(p, 4)] == refs["Billing"]
        return {
            "clusters": [
                {
                    "ref": refs["Billing"],
                    "title": "Invoices",
                    "summary": "",
                    "members": [_alias(p, n) for n in (4, 5, 6)],
                },
                {
                    "ref": refs["Login flow"],
                    "title": "Login flow",
                    "summary": "",
                    "members": [_alias(p, n) for n in (1, 2, 3)],
                },
            ]
        }

    model.reply = regroup
    assert _run()["projects"][0]["status"] == "committed"
    after = {sc.shown_title(c): cid for cid, c in sc.read_checked()["clusters"].items()}
    assert after == {"Invoices": before["Billing"], "Login flow": before["Login flow"]}
    assert _clusters()["Invoices"] == [_key(4), _key(5), _key(6)]


def test_force_ignores_the_fingerprint(fleet, model):
    model.reply = _two_topics
    _run()
    assert _run(project_id=DEFAULT, force=True)["projects"][0]["status"] == "committed"
    assert len(model.calls) == 2


def test_a_transport_error_is_recorded_and_retried_on_the_next_pass(fleet, model):
    model.reply = review.ReviewError("endpoint returned HTTP 502")
    (report,) = _run()["projects"]
    assert report == {
        "project_id": DEFAULT,
        "status": "error",
        "error": "endpoint returned HTTP 502",
    }
    assert sc.sweep_record(sc.read_checked(), DEFAULT)["error"] == "endpoint returned HTTP 502"
    model.reply = _two_topics
    assert _run()["projects"][0]["status"] == "committed"
    assert sc.sweep_record(sc.read_checked(), DEFAULT)["error"] is None


def test_a_pass_sweeps_at_most_the_cap_and_the_least_recently_swept_project_first(
    fleet, model, monkeypatch
):
    other = projects.create("Other", folders=["/srv/other"])
    d = fleet / ".claude" / "projects" / "-srv-other"
    d.mkdir(parents=True)
    for n in (7, 8):
        (d / f"{_key(n).split(':')[1]}.jsonl").write_text(
            json.dumps({"type": "user", "cwd": "/srv/other", "message": {"content": "x"}}) + "\n"
        )
        metadata.patch(_key(n), ai_title=f"Session {n}", ai_summary="s")
    engines.invalidate_scan_cache()
    monkeypatch.setattr(clusters_loop, "SWEEP_CAP", 1)
    model.reply = lambda p: {
        "clusters": [
            {
                "ref": "new",
                "title": "T",
                "summary": "",
                "members": [s["alias"] for s in p["sessions"]],
            }
        ]
    }
    first = _run()
    assert first["deferred"] == 1 and len(first["projects"]) == 1
    second = _run()
    swept = {first["projects"][0]["project_id"]} | {
        r["project_id"] for r in second["projects"] if r["status"] == "committed"
    }
    assert swept == {DEFAULT, other.id}
    # A cluster never spans projects.
    doc = sc.read_checked()
    by_project = {c["project_id"]: cid for cid, c in doc["clusters"].items()}
    assert {m["cluster"] for k, m in doc["membership"].items() if k in (_key(7), _key(8))} == {
        by_project[other.id]
    }


def test_fewer_than_two_candidates_and_no_clusters_makes_no_model_call(tmp_home, model):
    assert _run() == {"projects": [], "deferred": 0}
    assert model.calls == []


# --- the fence while the model is answering -----------------------------------------------------


def test_an_operator_write_while_the_model_answers_wins_and_the_plan_is_dropped(fleet, model):
    model.reply = _two_topics
    _run()
    doc = sc.read_checked()
    login = next(cid for cid, c in doc["clusters"].items() if c["ai_title"] == "Login flow")
    metadata.patch(_key(6), ai_summary="changed, so the project is swept again")
    engines.invalidate_scan_cache()

    def answer_after_an_operator_write(p):
        sc.dissolve(login)  # the operator dissolves a cluster mid-call
        return {
            "clusters": [
                {"ref": c["ref"], "title": "Hijack", "summary": "", "members": ["s1", "s2", "s3"]}
                for c in p["clusters"]
            ]
        }

    model.reply = answer_after_an_operator_write
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "stale"}]
    assert _clusters() == {"Billing": [_key(4), _key(5)]}
    # The scheduled loop comes back to it: the stale pass recorded no fingerprint.
    model.reply = {"clusters": []}
    assert _run()["projects"][0]["status"] == "committed"
    assert _clusters() == {}


def test_recluster_now_retries_once_after_a_stale_plan(fleet, model):
    state = {"n": 0}

    def reply(p):
        state["n"] += 1
        if state["n"] == 1:
            sc.create(DEFAULT, "Made while the model answered")
        return _two_topics(p)

    model.reply = reply
    assert _run(project_id=DEFAULT, force=True)["projects"] == [
        {"project_id": DEFAULT, "status": "committed"}
    ]
    assert state["n"] == 2
    assert set(_clusters()) == {"Made while the model answered", "Login flow", "Billing"}


def test_a_session_archived_or_taken_by_a_mission_while_the_model_answers_is_not_placed(
    fleet, model, tmp_path
):
    def reply(p):
        out = _two_topics(p)
        metadata.patch(_key(1), archived=True)
        mission = missions.create_mission("ship it", title="A mission", cwd=str(tmp_path))
        missions.adopt(mission["id"], _key(4))
        engines.invalidate_scan_cache()
        return out

    model.reply = reply
    assert _run()["projects"][0]["status"] == "committed"
    # Billing lost one of its two members to the mission, so it is not created at all.
    assert _clusters() == {"Login flow": [_key(2), _key(3)]}


def _add_session(home, n: int) -> None:
    d = home / ".claude" / "projects" / "-home-user-claude-repo-a"
    (d / f"{_key(n).split(':')[1]}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": REPO, "message": {"content": f"task {n}"}}) + "\n"
    )
    metadata.patch(_key(n), ai_title=f"Session {n}", ai_summary=f"Works on topic {n}")
    engines.invalidate_scan_cache()


def test_a_session_that_appears_while_the_model_answers_is_asked_about_on_the_next_pass(
    fleet, model
):
    """The stored fingerprint describes what the model was GIVEN. A session it never saw must
    leave the project dirty, or no later pass would ever ask about it."""

    def reply(p):
        assert len(p["sessions"]) == 6
        _add_session(fleet, 7)  # reviewed while the model is answering
        return _two_topics(p)

    model.reply = reply
    assert _run()["projects"][0]["status"] == "committed"
    assert _key(7) not in sc.read_checked()["membership"]

    def second(p):
        assert len(p["sessions"]) == 7  # the new session is in front of the model now
        refs = {c["title"]: c["ref"] for c in p["clusters"]}
        return {
            "clusters": [
                {
                    "ref": refs["Login flow"],
                    "title": "Login flow",
                    "summary": "",
                    "members": [_alias(p, n) for n in (1, 2, 3, 7)],
                },
                {
                    "ref": refs["Billing"],
                    "title": "Billing",
                    "summary": "",
                    "members": [_alias(p, n) for n in (4, 5)],
                },
            ]
        }

    model.reply = second
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "committed"}]
    assert len(model.calls) == 2
    assert _key(7) in _clusters()["Login flow"]
    # …and only now is the project settled.
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "unchanged"}]


def test_a_summary_that_changes_while_the_model_answers_is_judged_on_the_next_pass(fleet, model):
    def reply(p):
        metadata.patch(_key(6), ai_summary="Now on invoices")
        engines.invalidate_scan_cache()
        return _two_topics(p)

    model.reply = reply
    assert _run()["projects"][0]["status"] == "committed"
    model.reply = lambda p: {
        "clusters": [
            {
                "ref": c["ref"],
                "title": c["title"],
                "summary": "",
                "members": [s["alias"] for s in p["sessions"] if s["current"] == c["ref"]],
            }
            for c in p["clusters"]
        ]
    }
    assert _run()["projects"][0]["status"] == "committed"
    assert model.calls[1]["sessions"][0]["summary"] == "Now on invoices"
    assert _run()["projects"] == [{"project_id": DEFAULT, "status": "unchanged"}]


def test_a_scheduled_sweep_and_recluster_now_do_not_overlap_on_one_project(
    fleet, model, monkeypatch
):
    """Both take the project's lock: the second starts from the first's committed state."""
    order: list[str] = []

    def keep_as_is(payload: dict) -> dict:
        return {
            "clusters": [
                {
                    "ref": c["ref"],
                    "title": c["title"],
                    "summary": "",
                    "members": [
                        s["alias"] for s in payload["sessions"] if s["current"] == c["ref"]
                    ],
                }
                for c in payload["clusters"]
            ]
        }

    async def both():
        started, gate = asyncio.Event(), asyncio.Event()

        async def slow(messages, *, model=None):
            payload = json.loads(messages[1]["content"])
            order.append(f"call:{len(payload['clusters'])}")
            if len(order) == 1:
                started.set()
                await gate.wait()
                return _two_topics(payload)
            return keep_as_is(payload)

        monkeypatch.setattr(review, "complete_json", slow)
        a = asyncio.create_task(clusters_loop.run_sweep())
        await asyncio.wait_for(started.wait(), 30)
        b = asyncio.create_task(clusters_loop.run_sweep(project_id=DEFAULT, force=True))
        # Long enough for the second to read the fleet and reach the project's lock.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if clusters_loop._LOCKS[DEFAULT]._waiters:
                break
        assert order == ["call:0"]  # the second is waiting on the lock, not at the model
        gate.set()
        return await a, await b

    first, second = asyncio.run(both())
    assert first["projects"][0]["status"] == "committed"
    assert second["projects"][0]["status"] == "committed"
    assert order == ["call:0", "call:2"]
    assert len(sc.read_checked()["clusters"]) == 2
