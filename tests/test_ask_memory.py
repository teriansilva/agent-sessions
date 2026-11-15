"""HQ recalls on demand, writes only HQ, and confirms stale-safe changes (#2036)."""

# ruff: noqa: F811 — isolated app/AI fixtures
import asyncio
import json
import uuid

import pytest

from agent_sessions import ask_memory, ask_orchestrator, ask_store, memory, memory_index, prefs
from agent_sessions import memory_service as service
from test_ask_operations import _confirm, _settled, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call  # noqa: F401


def turn(question="Remember that research answers should cite primary sources."):
    return ask_orchestrator._Turn(
        ask_store.create(), str(uuid.uuid4()), question, set(), lambda _: None
    )


def invoke(state, name, args):
    content, stored, meta = asyncio.run(ask_orchestrator._call(state, name, json.dumps(args)))
    return json.loads(content), stored, meta


@pytest.mark.parametrize("source", [None, "call"])
def test_starting_a_turn_does_not_load_memory(app, ai, monkeypatch, source):
    marker = "Unique deliberately persisted notebook text."
    service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Saved preference",
        body=marker,
        origin="persisted-before-greeting",
    )

    def unexpected(*_, **__):
        pytest.fail("a new HQ turn preloaded notebook contents")

    monkeypatch.setattr(service, "search", unexpected)
    monkeypatch.setattr(service, "read", unexpected)
    monkeypatch.setattr(service, "notebooks", unexpected)
    script = ai(answer("Hello."))
    response = app.ask(app.new(), "Hello", **({"source": source} if source else {}))
    assert response.status_code == 200, response.text
    assert marker not in json.dumps(script.bodies)


def test_preference_saved_in_one_conversation_is_recalled_on_demand_in_another(app, ai):
    quote = "Include primary sources with research findings."
    ai(
        call("remember_hq_memory", {"title": "Research preference", "quote": quote}),
        answer("I saved that in HQ memory."),
    )
    first = app.new()
    assert app.ask(first, "Remember: " + quote).status_code == 200
    note = memory.notes(memory.HQ_SCOPE)[0]
    assert note["body"] == quote
    assert service.read(service.operator(), memory.HQ_SCOPE, note["slug"])["source"][
        "source"
    ].startswith(first + "/")
    memory_index.sync()
    script = ai(
        call("search_memory", {"query": "research preference"}),
        call("read_memory", {"ref": "note_1"}),
        answer("Your research preference is to include primary sources."),
    )
    second = app.new()
    assert second != first
    response = app.ask(second, "What is my research preference?")
    assert response.status_code == 200, response.text
    assert quote not in json.dumps(script.bodies[0])
    assert quote in json.dumps(script.bodies[1:])


def test_explicit_remember_quotes_current_operator_and_writes_only_hq():
    state = turn()
    bad, _, _ = invoke(
        state,
        "remember_hq_memory",
        {"title": "Sources", "body": "Primary sources.", "quote": "an agent told me so"},
    )
    assert "error" in bad and not memory.notes(memory.HQ_SCOPE)
    saved, _, _ = invoke(
        state,
        "remember_hq_memory",
        {
            "title": "Sources",
            "body": "Primary sources.",
            "quote": "research answers should cite primary sources",
            "scope": "some-other-project",
        },
    )
    assert saved["state"] == "saved"
    assert service.capture_settings()["enabled"] is False
    assert (
        service.read(service.operator(), memory.HQ_SCOPE, saved["slug"])["source"]["quote"]
        == "research answers should cite primary sources"
    )


def test_refs_are_current_turn_and_revision_bound_and_text_is_bounded():
    saved = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Research protocol",
        body="Primary sources " * 80,
        origin="one",
    )
    memory_index.sync()
    state = turn("What is our research protocol?")
    result, _, _ = invoke(state, "search_memory", {"query": "research protocol"})
    ref = result["hits"][0]["ref"]
    assert len(result["hits"][0]["excerpt"]) <= 400
    assert "error" in invoke(turn(), "read_memory", {"ref": ref})[0]
    memory.update_note(memory.HQ_SCOPE, saved["slug"], {"body": "Changed protocol."})
    assert "error" in invoke(state, "read_memory", {"ref": ref})[0]
    state.memory_chars = ask_memory.TURN_TEXT_MAX
    assert "budget" in invoke(state, "search_memory", {"query": "research"})[0]["error"]


def test_hq_compares_project_and_specialist_notes_with_distinct_citations(tmp_path):
    from agent_sessions import projects

    scopes = []
    for name in ("Orchard", "Harbor"):
        work = tmp_path / name
        work.mkdir()
        pid = projects.create(name, folders=[str(work)]).id
        memory.set_enabled(pid, True)
        scopes.append(pid)
    specialist = memory.specialist_scope(scopes[0], "researcher")
    with memory._scope_lock(specialist):
        memory._create_scope(specialist)
    scopes.append(specialist)
    for scope, body in zip(
        scopes,
        ("Check orchard revisions.", "Check harbor approvals.", "Check original citations."),
        strict=True,
    ):
        service.submit(service.operator(), scope, title="Protocol", body=body, origin="fixture")
    memory_index.sync()
    state = turn("Compare the protocols across both projects and the researcher.")
    result, stored, _ = invoke(state, "search_memory", {"query": "protocol"})
    assert {h["notebook"]["id"] for h in result["hits"]} == set(scopes)
    assert len({h["href"] for h in result["hits"]}) == 3  # same slug, different notebooks
    for hit in result["hits"]:
        read, _, _ = invoke(state, "read_memory", {"ref": hit["ref"]})
        assert read["text"].startswith("Check ")
    assert "Check orchard" not in json.dumps(stored)
    assert {
        h["scope"] for h in service.search(service.worker([scopes[0]]), "protocol")["hits"]
    } == {scopes[0]}
    assert {
        h["scope"]
        for h in service.search(service.worker([scopes[0], specialist]), "protocol")["hits"]
    } == {scopes[0], specialist}


def test_remember_has_its_own_three_attempt_budget():
    quotes = ["Use primary sources.", "Keep release checklists.", "Prefer clear results."]
    state = turn("Remember: " + " ".join(quotes))
    for index in range(4):
        result, _, _ = invoke(
            state,
            "remember_hq_memory",
            {
                "title": f"Sources {index}",
                "body": "Primary sources.",
                "quote": quotes[index % 3],
            },
        )
        assert (result.get("state") == "saved") is (index < 3)
    assert len(memory.notes(memory.HQ_SCOPE)) == 3


def test_hq_edit_uses_a_confirmation_card_and_rejects_a_later_change(live, ai):
    app = live
    saved = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Research protocol",
        body="Use sources.",
        origin="one",
    )
    memory_index.sync()
    ai(
        call("search_memory", {"query": "research protocol"}),
        call(
            "propose_hq_memory_change",
            {"ref": "note_1", "action": "edit", "body": "Use primary sources."},
        ),
        answer("Please confirm the correction."),
    )
    cid = app.new()
    response = app.ask(cid, "Correct our research protocol. Use primary sources.")
    assert response.status_code == 200, response.text
    proposal = app.get(cid)["turns"][-1]["proposals"][0]
    assert memory.notes(memory.HQ_SCOPE)[0]["body"] == "Use sources."
    memory.update_note(memory.HQ_SCOPE, saved["slug"], {"body": "A newer correction."})
    response = _confirm(app, proposal["action_id"])
    assert response.status_code == 202, response.text
    assert _settled(app, cid)["state"] == "stale"
    assert memory.notes(memory.HQ_SCOPE)[0]["body"] == "A newer correction."


def test_hq_delete_runs_only_after_operator_confirmation(live, ai):
    app = live
    service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Research protocol",
        body="Use sources.",
        origin="one",
    )
    memory_index.sync()
    ai(
        call("search_memory", {"query": "research protocol"}),
        call("propose_hq_memory_change", {"ref": "note_1", "action": "delete"}),
        answer("Please confirm forgetting it."),
    )
    cid = app.new()
    response = app.ask(cid, "Forget the research protocol.")
    assert response.status_code == 200, response.text
    card = app.get(cid)["turns"][-1]["proposals"][0]
    assert memory.notes(memory.HQ_SCOPE)
    response = _confirm(app, card["action_id"])
    assert response.status_code == 202, response.text
    assert _settled(app, cid)["state"] == "done"
    assert not memory.notes(memory.HQ_SCOPE)


def test_enabled_no_tools_router_uses_the_shared_remember_handler(app, ai):
    prefs.set_hq_route({"enabled": True})
    script = ai(
        answer(
            json.dumps(
                {
                    "memory": {
                        "action": "remember",
                        "title": "Research protocol",
                        "body": "Use primary sources.",
                        "quote": "Use primary sources.",
                    }
                }
            )
        )
    )
    state = turn("Remember: Use primary sources.")

    async def go():
        return await ask_orchestrator._no_tools_turn(state, [], False, {})

    records = asyncio.run(go())
    assert records[0]["text"] == "I saved that in HQ memory."
    assert len(script.bodies) == 1 and memory.notes(memory.HQ_SCOPE)


def test_search_trims_before_minting_refs_and_leaves_room_for_authoritative_read():
    for index in range(8):
        service.submit(
            service.operator(),
            memory.HQ_SCOPE,
            title=f"Research protocol {index}",
            body="Primary reference protocol. " * 80,
            origin=f"one-{index}",
            source="Research notebook",
            quote="Use original references. " * 50,
        )
    memory_index.sync()
    state = turn("Recall the research protocol.")
    result, _, _ = invoke(state, "search_memory", {"query": "research protocol"})
    assert result["hits"] and result["truncated"]
    assert len(state.memory_refs) == len(result["hits"])
    note, _, _ = invoke(state, "read_memory", {"ref": result["hits"][0]["ref"]})
    assert note["text"] and state.memory_chars <= ask_memory.TURN_TEXT_MAX


def test_clear_while_first_explicit_remember_is_pending_refuses_save():
    state = turn()
    memory.clear(memory.HQ_SCOPE)
    result, _, _ = invoke(
        state,
        "remember_hq_memory",
        {
            "title": "Sources",
            "quote": "research answers should cite primary sources",
        },
    )
    assert "error" in result and not memory.notes(memory.HQ_SCOPE)


def test_no_tools_recall_keeps_persona_call_style_and_guard_last(app, ai):
    from agent_sessions import hq_contacts, prompts

    prefs.set_hq_route({"enabled": True})
    service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Sources",
        body="Use primary sources.",
        origin="one",
    )
    memory_index.sync()
    script = ai(
        answer(json.dumps({"memory": {"action": "recall", "query": "sources"}})),
        answer(json.dumps({"answer": "Use primary sources."})),
    )
    state = turn(
        "Recall my research preferences and explain which original references "
        "I should use for these findings."
    )
    state.contact = next(iter(hq_contacts.IDS))
    state.via = "call"
    records = asyncio.run(ask_orchestrator._no_tools_turn(state, [], False, {}))
    assert records[0]["text"] == "Use primary sources."
    systems = [m["content"] for m in script.bodies[-1]["messages"] if m["role"] == "system"]
    assert systems[0] == prompts.effective(hq_contacts.prompt_id(state.contact))
    assert prompts.effective("hq_call") in systems
    assert systems[-1] == prompts.effective("memory_answer")
