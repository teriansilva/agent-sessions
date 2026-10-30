"""HQ and library secrets (#1786 P4a): HQ lists secrets, proposes storing one and switching its
agent access — and never sees, holds or forwards a value.

Pinned here: the list carries no value; a create's value is typed on the card only and reaches
no model request, Ask record, sidecar, card view or decide response (canary sweep), and a create
never turns agent access on; a value as a model argument is refused; an access-on card names the
bound projects and is `stale` when that set changed before the tap; access cards are never
voice-confirmable; and a secret id must come from this turn's `list_secrets`.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json

from agent_sessions import (
    ask_secrets,
    ask_store,
    template_secrets,
    template_vars,
    variables,
    voice_calls,
)
from test_ask_operations import _confirm, _project, _result, _settled, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

CANARY = "zq9-card-typed-Vh3kP0wLx7-1786"


def _ask(a, ai, *steps, cid: str | None = None, question: str = "do it") -> tuple[str, object]:
    script = ai(*steps, answer("Proposed; confirm it on the card."))
    cid = cid or a.new()
    r = a.ask(cid, question)
    assert r.status_code == 200, r.text
    return cid, script


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _secret(name: str = "forgejo_token", value: str = "stored-value-1786-xyz") -> dict:
    return variables.create({"name": name, "kind": "secret", "value": value})


def _decide(a, pid: str, inputs: dict | None = None):
    body: dict = {"decision": "confirm"}
    if inputs is not None:
        body["inputs"] = inputs
    return a.c.post(f"/api/ask/proposals/{pid}/decide", json=body, headers=a.hdr)


def _record(name: str) -> dict:
    return next(v for v in variables.library()["variables"] if v["name"] == name)


def test_list_secrets_shows_names_and_access_never_a_value(live, ai):
    _secret(value=CANARY)
    plain = variables.create({"name": "host", "value": "a.test"})
    cid, script = _ask(live, ai, calls(("list_secrets", {})))
    out = _result(script, 1)
    assert [r["name"] for r in out["secrets"]] == ["forgejo_token"]  # a text variable is not one
    row = out["secrets"][0]
    assert row["kind"] == "text" and row["stored"] and not row["agent_access"]
    assert plain["id"] not in json.dumps(out)
    assert CANARY not in json.dumps(script.bodies)


def test_a_create_stores_the_value_typed_on_the_card_and_nothing_else_ever_holds_it(live, ai):
    cid, script = _ask(
        live, ai, call("propose_secret", {"action": "create", "name": "deploy_token"})
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "secret.create" and card["needs"] == ["value"]
    assert "HQ never sees it" in card["text"]

    missing = _decide(live, card["action_id"])
    assert missing.status_code == 422 and _cards(live, cid)[0]["state"] == "pending"
    r = _decide(live, card["action_id"], {"value": CANARY})
    assert r.status_code == 202, r.text
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled

    rec = _record("deploy_token")
    assert rec["kind"] == "secret" and rec["set"] and rec["agent_access"] is False
    assert CANARY in template_secrets.redaction_values()  # stored, and redacted from now on

    # A later turn replays history; nothing it sends carries the value either.
    _, replay = _ask(live, ai, cid=cid, question="anything else?")
    assert replay.bodies and CANARY not in json.dumps(replay.bodies)
    assert CANARY not in json.dumps(script.bodies)
    assert CANARY not in r.text and CANARY not in json.dumps(live.get(cid))
    for path in ask_store.root().rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


def test_a_create_never_turns_agent_access_on():
    _method, _path, body = ask_secrets._create_route(
        "", {"secret_name": "x", "description": "", "value": "v" * 10}, {}
    )
    assert "agent_access" not in body


def test_a_value_as_a_model_argument_is_refused_and_nothing_is_staged(live, ai):
    cid, script = _ask(
        live,
        ai,
        call("propose_secret", {"action": "create", "name": "deploy_token", "value": CANARY}),
    )
    assert _cards(live, cid) == []
    assert "deploy_token" not in [v["name"] for v in variables.library()["variables"]]
    for path in ask_store.root().rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


def test_a_name_already_in_the_library_is_refused(live, ai):
    _secret("deploy_token")
    cid, script = _ask(
        live, ai, call("propose_secret", {"action": "create", "name": "deploy_token"})
    )
    assert _cards(live, cid) == [] and "already exists" in json.dumps(_result(script, 1))


def _access_card(live, ai, vid: str, action: str = "access_on") -> tuple[str, dict]:
    cid, _ = _ask(
        live,
        ai,
        calls(("list_secrets", {})),
        call("propose_secret", {"action": action, "id": vid}, cid="p"),
    )
    (card,) = _cards(live, cid)
    return cid, card


def test_access_on_names_the_bound_projects_and_turns_access_on_when_tapped(live, ai):
    pid = _project(live, "Atlas iOS")
    row = _secret()
    variables.set_projects(row["id"], [pid], row["updated_at"])
    cid, card = _access_card(live, ai, row["id"])
    assert card["op"] == "secret.access_on" and card["risk"] == "external"
    assert "Atlas iOS" in card["text"] and "send it anywhere" in card["text"]
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    assert _record("forgejo_token")["agent_access"] is True

    cid, card = _access_card(live, ai, row["id"], "access_off")
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    assert _record("forgejo_token")["agent_access"] is False


def test_an_access_on_card_whose_project_set_changed_is_stale_and_changes_nothing(live, ai):
    a, b = _project(live, "Atlas iOS"), _project(live, "Beta")
    row = _secret()
    row = variables.set_projects(row["id"], [a], row["updated_at"])
    cid, card = _access_card(live, ai, row["id"])
    # Bound elsewhere (a playbook deployment binds by `ref: global` in the project's own record):
    # the secret's own revision does not move, so only the bound-project set can tell.
    template_vars.bind_project(b, [{"name": "forgejo_token", "kind": "secret", "ref": "global"}])
    assert _record("forgejo_token")["updated_at"] == row["updated_at"]
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "stale", settled
    assert _record("forgejo_token")["agent_access"] is False


def test_an_access_card_whose_secret_was_saved_elsewhere_is_stale(live, ai):
    row = _secret()
    cid, card = _access_card(live, ai, row["id"])
    variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "stale"


def test_a_secret_id_list_secrets_did_not_return_this_turn_is_refused(live, ai):
    row = _secret()
    cid, script = _ask(live, ai, call("propose_secret", {"action": "access_on", "id": row["id"]}))
    assert _cards(live, cid) == []
    assert "returned in this turn" in json.dumps(_result(script, 1))


def test_no_secret_card_is_ever_voice_confirmable(live, ai):
    for op in ("secret.create", "secret.access_on", "secret.access_off"):
        assert voice_calls._voice_operation(op) is False, op
    # And the real staged cards, as a call would read them back: none is offered.
    row = _secret()
    cid, _ = _access_card(live, ai, row["id"])
    cid2, _ = _ask(live, ai, call("propose_secret", {"action": "create", "name": "deploy_token"}))
    for c in (cid, cid2):
        for turn in live.get(c)["turns"]:
            assert turn["proposals"] and voice_calls.confirmable_cards(turn) == []


def test_a_value_the_store_would_refuse_is_refused_at_the_tap_and_the_card_stays_pending(live, ai):
    cid, _ = _ask(live, ai, call("propose_secret", {"action": "create", "name": "deploy_token"}))
    (card,) = _cards(live, cid)
    for bad in (" padded-value-1786", "short"):
        r = _decide(live, card["action_id"], {"value": bad})
        assert r.status_code == 422 and bad not in r.text
        assert _cards(live, cid)[0]["state"] == "pending"
    assert _decide(live, card["action_id"], {"value": CANARY}).status_code == 202
    assert _settled(live, cid)["state"] == "done"


def test_a_binding_added_between_the_check_and_the_patch_is_stale_with_a_reason(live, ai):
    a, b = _project(live, "Atlas iOS"), _project(live, "Beta")
    row = _secret()
    row = variables.set_projects(row["id"], [a], row["updated_at"])
    cid, card = _access_card(live, ai, row["id"])
    real = ask_secrets._current

    def late_binding(target, facts):
        # Lands after `_run` re-read the facts, before the PATCH body is built.
        template_vars.bind_project(
            b, [{"name": "forgejo_token", "kind": "secret", "ref": "global"}]
        )
        return real(target, facts)

    ask_secrets._current = late_binding
    try:
        _confirm(live, card["action_id"])
        settled = _settled(live, cid)
    finally:
        ask_secrets._current = real
    assert settled["state"] == "stale", settled
    assert settled["reason"].startswith("the projects that can use it changed")
    assert _record("forgejo_token")["agent_access"] is False


def test_a_bound_project_named_like_a_stored_secret_never_carries_it_to_the_model(live, ai):
    """A project's name holds a stored secret's value: the access card would name it, so it is
    refused — and neither the list, the refusal nor any later request carries the value."""
    value = "Harbor zq9 lantern 1786"  # a synthetic stored value that is also a valid name
    variables.create({"name": "other_token", "kind": "secret", "value": value})
    pid = _project(live, value)
    row = _secret()
    variables.set_projects(row["id"], [pid], row["updated_at"])
    cid, script = _ask(
        live,
        ai,
        calls(("list_secrets", {})),
        call("propose_secret", {"action": "access_on", "id": row["id"]}, cid="p"),
    )
    assert _cards(live, cid) == []
    leaked = value in json.dumps(script.bodies)
    assert not leaked, "the stored value reached a model request"


def test_an_access_card_that_cannot_name_every_project_is_refused(live, ai):
    """Every recipient is named or there is no card (Hermes on #1813): a truncated list would
    let the tap widen access to a project the operator never saw."""
    row = _secret()
    pids = [_project(live, f"Project {i} " + "x" * 50) for i in range(4)]
    variables.set_projects(row["id"], pids, row["updated_at"])
    cid, script = _ask(
        live,
        ai,
        calls(("list_secrets", {})),
        call("propose_secret", {"action": "access_on", "id": row["id"]}, cid="p"),
    )
    assert _cards(live, cid) == []
    assert "more projects than one card can name" in json.dumps(_result(script, 2))
    assert _record("forgejo_token")["agent_access"] is False


def test_an_access_card_names_every_bound_project_in_full(live, ai):
    row = _secret()
    names = [f"Project {i} " + "y" * 20 for i in range(3)]
    variables.set_projects(row["id"], [_project(live, n) for n in names], row["updated_at"])
    cid, card = _access_card(live, ai, row["id"])
    for n in names:
        assert n in card["text"]


def test_a_project_bound_after_the_check_still_never_cuts_the_card(live, ai, monkeypatch):
    """The length is judged on the sentence the card is built from (#1813 review): a project
    bound between `prepare`'s read and `stage`'s still refuses rather than cutting the card."""
    row = _secret()
    pids = [_project(live, f"Project {i} " + "z" * 50) for i in range(4)]
    variables.set_projects(row["id"], pids[:1], row["updated_at"])
    real = ask_secrets.facts
    reads = {"n": 0}

    async def late(op, target, app, operator):
        reads["n"] += 1
        if reads["n"] == 2:  # stage's read: the bindings grew since prepare's
            r = _record("forgejo_token")
            variables.set_projects(r["id"], pids, r["updated_at"])
        return await real(op, target, app, operator)

    monkeypatch.setattr(ask_secrets, "facts", late)
    cid, script = _ask(
        live,
        ai,
        calls(("list_secrets", {})),
        call("propose_secret", {"action": "access_on", "id": row["id"]}, cid="p"),
    )
    assert _cards(live, cid) == []
    assert "too long to show in full" in json.dumps(_result(script, 2))


def test_access_off_works_whatever_the_project_names(live, ai):
    row = _secret()
    pids = [_project(live, f"Project {i} " + "w" * 100) for i in range(5)]
    row = variables.set_projects(row["id"], pids, row["updated_at"])
    variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    cid, card = _access_card(live, ai, row["id"], "access_off")
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "done"
    assert _record("forgejo_token")["agent_access"] is False
