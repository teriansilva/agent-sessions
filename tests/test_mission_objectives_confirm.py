"""#1061 Phase 3 — the facts the launch confirmation uses to say why a set is worth a look.

`propose` writes ONE timeline row when it dropped a suggestion or fitted a target to the
instruction, carrying the counts as `meta.fit`; `get_mission` exposes the newest as
`objectives_fit`, read in the same snapshot as the rest of the mission. Zeros when nothing was
unusual, and a malformed row degrades to zeros rather than failing the read.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_objectives as mo
from agent_sessions import missions, prefs


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_mission_playbooks(
        {
            "default_id": "ship",
            "playbooks": [
                {
                    "id": "ship",
                    "label": "Ship",
                    "objectives": [
                        {
                            "key": "merged",
                            "title": "It is merged",
                            "probe": "forge_merged",
                            "gate": True,
                        }
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    yield
    missions.reset_schema_cache_for_test()


def _reply(monkeypatch, reply):
    async def fake(_messages, **_kw):
        return reply

    monkeypatch.setattr(mo.review, "complete_json", fake)


def _fit_events(mid):
    return [
        e
        for e in missions.get_mission(mid)["events"]
        if e["kind"] == "objective" and isinstance(e.get("meta"), dict) and "fit" in e["meta"]
    ]


@pytest.mark.anyio
async def test_a_fitted_target_and_a_dropped_suggestion_are_recorded_once_with_their_counts(
    store, monkeypatch
):
    _reply(
        monkeypatch,
        {
            "objectives": [
                {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/alpha"}},
                {"template_index": 0, "gate": True, "probe_args": {"branch": "not-in-it"}},
            ]
        },
    )
    mid = missions.create_mission("please merge devopsagent/alpha when green")["id"]
    await mo.propose(mid)
    [ev] = _fit_events(mid)
    assert ev["meta"] == {"fit": {"dropped": 1, "parameterised": 1}}
    assert ev["text"] == (
        "1 objective fitted to targets named in the instruction; "
        "1 suggestion did not fit the checklist and was dropped"
    )
    assert missions.get_mission(mid)["objectives_fit"] == {"dropped": 1, "parameterised": 1}


@pytest.mark.anyio
async def test_an_ordinary_set_writes_no_fit_row_and_reads_as_zeros(store, monkeypatch):
    _reply(monkeypatch, {"objectives": [{"template_index": 0, "gate": True}]})
    mid = missions.create_mission("merge the thing")["id"]
    await mo.propose(mid)
    assert _fit_events(mid) == []
    assert missions.get_mission(mid)["objectives_fit"] == {"dropped": 0, "parameterised": 0}


@pytest.mark.anyio
async def test_nothing_written_means_no_fit_row_even_when_everything_was_dropped(
    store, monkeypatch
):
    # Every selection refused ⇒ no rows ⇒ the existing "no objectives" path; the fit row is about a
    # set that exists, so it is not written for one that does not.
    _reply(monkeypatch, {"objectives": [{"template_index": 9}]})
    mid = missions.create_mission("merge the thing")["id"]
    await mo.propose(mid)
    assert _fit_events(mid) == []


@pytest.mark.parametrize(
    "meta",
    [
        {"fit": {"dropped": True, "parameterised": -2}},
        {"fit": {"dropped": "3"}},
        {"fit": []},
        ["fit"],
    ],
)
def test_a_malformed_fit_row_reads_as_zeros_and_never_fails_the_mission_read(store, meta):
    mid = missions.create_mission("x")["id"]
    missions.append_event(mid, "objective", text="hand-edited", meta=meta)
    assert missions.get_mission(mid)["objectives_fit"] == {"dropped": 0, "parameterised": 0}


def test_the_newest_fit_row_wins_and_a_huge_count_is_capped(store):
    mid = missions.create_mission("x")["id"]
    missions.append_event(mid, "objective", text="a", meta={"fit": {"dropped": 2}})
    missions.append_event(mid, "objective", text="b", meta={"fit": {"parameterised": 10**9}})
    assert missions.get_mission(mid)["objectives_fit"] == {"dropped": 0, "parameterised": 1000}
