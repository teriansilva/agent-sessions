"""A mission's objectives fit the mission (#1061) — what is left of it once checklists went (#1653).

Phases 1 and 2 were about checklists: `playbook_id`'s three shapes, and a template selection
parameterising a probe with a `repo`/`branch` the instruction named. Both are gone with the
templates. What remains: every mission is written from its own instruction, the timeline does not
talk about a checklist that no longer exists, and a stale `playbook_id` from an old client or an
old row changes nothing.

Phase 4: the non-settling answer says it will be asked again.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_objectives as mo
from agent_sessions import mission_questions as mq
from agent_sessions import missions


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


@pytest.mark.anyio
@pytest.mark.parametrize("configured", [True, False])
async def test_the_timeline_never_talks_about_a_checklist(store, monkeypatch, configured):
    """The old producer said "checklist declined — …", "no checklist applied: no default checklist
    is set" or "…the chosen checklist no longer exists" on every mission. None of those is true or
    meaningful any more, and a line about a feature the operator cannot see is noise."""
    from agent_sessions import review

    if configured:
        monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    else:

        def _raise():
            raise review.NotConfiguredError("no endpoint")

        monkeypatch.setattr(mo.review, "_require_config", _raise)

    async def fake(_messages, **_kw):
        return {"notes": [{"title": "Look around"}]}

    monkeypatch.setattr(mo.review, "complete_json", fake)
    mid = missions.create_mission("look around")["id"]
    await mo.propose(mid)
    texts = [e.get("text") or "" for e in missions.get_mission(mid)["events"]]
    assert not any("checklist" in t.lower() for t in texts), texts
    assert len(missions.objectives(mid)) == 1


@pytest.mark.anyio
async def test_the_model_is_shown_the_instruction_and_NO_templates(store, monkeypatch):
    """The request carries the instruction and nothing a template list used to add."""
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    seen: list[list[dict]] = []

    async def fake(messages, **_kw):
        seen.append(messages)
        return {"notes": [{"title": "It is merged"}]}

    monkeypatch.setattr(mo.review, "complete_json", fake)
    mid = missions.create_mission("merge devopsagent/alpha when green")["id"]
    await mo.propose(mid)
    [msgs] = seen
    user = msgs[-1]["content"]
    assert "merge devopsagent/alpha when green" in user
    assert "Templates:" not in user and "template_index" not in user


# ---- Phase 4 -----------------------------------------------------------------------------------


def test_the_non_settling_answer_says_it_may_be_asked_again():
    """It is literally true that nothing else changes — and that is exactly why the same question
    comes back as a new episode. The consequence the card shows has to say so."""
    assert "may be asked again" in mq.CONSEQUENCE["note_answer"]
