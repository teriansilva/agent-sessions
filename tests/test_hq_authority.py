"""HQ acts on reversible changes without a card (#2390, epic #2070 A1).

Pinned here, the epic's mechanism tests that apply to A1: Ask first stages a card and runs
nothing; under Standard a rename runs at once through the tap's own route, settles ``done`` with
``decided_by: "hq"`` and is logged; a class HQ can't act on yet stays a card even when set to
act; two concurrent acts on one proposal run once; a restart mid-act reads interrupted and is
never replayed (and the log tells the truth after a crash); an act after logout, after the
authority was lowered (also while the facts are re-read or the request is prepared) and after
the call ended is refused before the effect, leaving the card pending; a turn without an
operator, and the continuation after a card, cannot act; the per-turn cap; the claim log fails
closed; no catalog entry names a locked or operator-only route; HQ can't write its own
authority; taint is recorded per turn and carried by the conversation.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from agent_sessions import (
    asgi_call,
    ask_operations,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    hq_acts,
    hq_authority,
    projects,
)
from test_ask_operations import (  # noqa: F401 — fixtures are used by name
    _card,
    _project,
    _propose_project,
    _result,
    _settled,
    ai,
    answer,
    app,
    call,
    calls,
    live,
)


def _set(preset: str | None = None, **classes) -> dict:
    rev = hq_authority.view()["revision"]
    body = {"revision": rev, **({"preset": preset} if preset else {"classes": classes})}
    return hq_authority.update(body)


def _operator(a) -> asgi_call.Operator:
    """The logged-in test client's own request identity, as a turn would carry it."""
    cookie = "; ".join(f"{k}={v}" for k, v in a.c.cookies.items())
    return asgi_call.Operator(
        headers=(
            (b"cookie", cookie.encode()),
            (b"x-csrf-token", a.hdr["X-CSRF-Token"].encode()),
            (b"origin", a.hdr.get("Origin", "https://testserver").encode()),
            (b"host", b"testserver"),
        ),
        scheme="https",
        server=("testserver", 443),
        client=("testclient", 50000),
    )


def _rename_card(a, ai, name="scratch-2") -> tuple[str, str, str]:
    """A pending project-rename card staged under Ask first: (project id, cid, card id)."""
    pid = _project(a, "scratch")
    cid, _ = _propose_project(a, ai, "rename", name=name)
    return pid, cid, _card(a, cid)["action_id"]


def _record() -> dict:
    return {"class": "reversible", "turn_id": "t" * 8, "via": None, "tainted": False}


def _act(a, cid, aid, grant=None):
    _set("standard") if grant is None else None
    grant = grant or hq_authority.ActGrant.now()
    return asyncio.run(
        ask_operations.act(
            cid, aid, operator=_operator(a), app=a.app, grant=grant, record=_record()
        )
    )


# ---- the setting --------------------------------------------------------------------------------


def test_every_catalog_entry_has_exactly_one_class():
    assert set(hq_authority.CLASS_OF) == set(ask_operations.CATALOG)
    assert set(hq_authority.CLASS_OF.values()) <= set(hq_authority.CLASSES)
    # A1: the reversible class acts (A1b, #2391: and starts or sends); the RISKY ones are labelled.
    assert hq_authority.ACTABLE == {"reversible", "starts"}
    assert hq_authority.RISKY == {"destructive", "external", "widens"}


def test_an_upgrade_reads_ask_first_and_presets_derive():
    v = hq_authority.view()
    assert v["preset"] == "ask_first" and v["revision"] == 0
    assert set(v["classes"].values()) == {"ask"}
    assert _set("standard")["classes"]["reversible"] == "act"
    # The operator's own mix (their direction on #2070): starts acts too. Kept, reads Custom.
    v = _set(starts="act")
    assert v["preset"] == "custom" and v["classes"]["starts"] == "act" and v["revision"] == 2
    assert _set("full")["preset"] == "full"


def test_a_stale_revision_is_409_and_bad_bodies_422():
    _set("standard")
    with pytest.raises(hq_authority.AuthorityError) as e:
        hq_authority.update({"revision": 0, "preset": "full"})
    assert e.value.status == 409
    for body in (
        {"preset": "full"},
        {"revision": 1, "preset": "yolo"},
        {"revision": 1, "preset": []},
        {"revision": 1, "preset": {"a": 1}},
        {"revision": 1, "classes": {"screen": "act"}},
        {"revision": 1, "classes": {"reversible": "maybe"}},
        {"revision": 1, "preset": "full", "classes": {"reversible": "ask"}},
    ):
        with pytest.raises(hq_authority.AuthorityError) as e:
            hq_authority.update(body)
        assert e.value.status == 422, body
    assert hq_authority.view()["preset"] == "standard"


def test_a_stored_act_for_a_later_class_does_not_act_in_a1():
    _set("full")
    snap = hq_authority.snapshot()
    assert hq_authority.acts(snap, "reversible")
    assert hq_authority.acts(snap, "starts")  # A1b (#2391)
    for klass in ("destructive", "external", "widens"):
        assert not hq_authority.acts(snap, klass)


def test_the_route_reads_and_writes_with_the_revision(live):
    r = live.c.get("/api/hq/authority")
    assert r.status_code == 200 and r.json()["preset"] == "ask_first"
    r = live.c.put(
        "/api/hq/authority", json={"revision": 0, "preset": "standard"}, headers=live.hdr
    )
    assert r.status_code == 200 and r.json()["revision"] == 1
    r = live.c.put("/api/hq/authority", json={"revision": 0, "preset": "full"}, headers=live.hdr)
    assert r.status_code == 409
    no_csrf = {k: v for k, v in live.hdr.items() if k != "X-CSRF-Token"}
    r = live.c.put("/api/hq/authority", json={"revision": 1, "preset": "full"}, headers=no_csrf)
    assert r.status_code in (401, 403) and hq_authority.view()["preset"] == "standard"


def test_hq_can_never_write_its_own_authority(live):
    """Every HQ operation reaches a route through `asgi_call`; the Authority route refuses those,
    even with the operator's own valid cookie, CSRF token and Origin."""
    r = asyncio.run(
        asgi_call.call(
            live.app, _operator(live), "PUT", "/api/hq/authority",
            {"revision": 0, "preset": "full"},
        )
    )  # fmt: skip
    assert r.status == 403 and hq_authority.view()["preset"] == "ask_first"
    # …and the generic prefs write never touches the block.
    live.c.post("/api/prefs", json={"hq_authority": {"classes": {"reversible": "act"}}},
                headers=live.hdr)  # fmt: skip
    assert hq_authority.view()["preset"] == "ask_first"


LOCKED_OR_OPERATOR_ONLY = (
    "/api/hq/authority",
    "/api/prompts",
    "/change-password",
    "/api/auth",
    "/api/2fa",
    "/login",
    "/logout",
    "/link",
    "/plan/approve",
    "/api/pulse/actions/",
    "/api/plugins",
    "/api/federation",
    "/api/connectors",
    "/api/ai-review",
    "/api/voice",
)


def test_no_catalog_entry_names_a_locked_or_operator_only_route(monkeypatch):
    monkeypatch.setattr(ask_operations, "_folder_patch", lambda pid, facts, **kw: {})
    from agent_sessions import ask_mcp, ask_secrets

    monkeypatch.setattr(ask_secrets, "_current", lambda target, facts: 1.0)
    monkeypatch.setattr(ask_mcp, "_current", lambda target, facts: 1.0)

    class Anything(dict):
        def __missing__(self, key):
            return "x"

    fields = Anything(days=3, folder="/x", enabled=True, index=0, expected_revision=1)
    fields["_content"] = Anything(files={}, body={}, config={}, server={})
    facts = Anything(revision=2, version=1, sessions=1, matched=1)
    built = 0
    for op in ask_operations.CATALOG.values():
        if op.run is not None:
            continue  # its own runner; none of those is reversible-and-actable (see below)
        try:
            method, path, _body = op.route("p1", fields, facts)
        except Exception:  # noqa: BLE001 — a shape this sample can't build
            continue
        built += 1
        assert not any(bad in path for bad in LOCKED_OR_OPERATOR_ONLY), (op.id, path)
    assert built >= 40
    # A runner-backed entry never acts (`act` and `_act_refusal` refuse `run`): only a card.
    assert all(
        op.run is None or op.follow_up or op.inputs or hq_authority.class_of(op.id) != "reversible"
        for op in ask_operations.CATALOG.values()
    )


# ---- the act path through a turn ----------------------------------------------------------------


def test_ask_first_stages_a_card_and_runs_nothing(live, ai):
    pid, cid, _aid = _rename_card(live, ai)
    card = _card(live, cid)
    assert card["state"] == "pending" and "decided_by" not in card
    assert projects.load()[pid].name == "scratch"
    assert hq_acts.recent() == []


def test_standard_runs_a_rename_at_once_and_logs_it(live, ai):
    _set("standard")
    pid = _project(live, "scratch")
    cid, got = _propose_project(live, ai, "rename", name="scratch-2")
    result = _result(got["script"])
    assert result["ran"] == "done" and result["outcome"] == "Renamed.", result
    card = _card(live, cid)
    assert card["state"] == "done" and card["decided_by"] == "hq"
    assert projects.load()[pid].name == "scratch-2"
    (entry,) = ask_store.read_proposals(cid)
    assert entry["decided_by"] == "hq" and entry["authority_rev"] == 1
    (row,) = hq_acts.recent()
    assert row["state"] == "done" and row["label"] == "Rename project" and row["via"] == "typed"
    r = live.c.get("/api/hq/acts")
    assert r.status_code == 200 and r.json()["acts"][0]["op"] == "project.rename"
    # A tap on the done card runs nothing more.
    again = live.c.post(f"/api/ask/proposals/{card['action_id']}/decide",
                        json={"decision": "confirm"}, headers=live.hdr)  # fmt: skip
    assert again.status_code == 200 and again.json()["proposal"]["state"] == "done"


def test_a_destructive_entry_stays_a_card_even_under_full_control(live, ai):
    _set("full")
    pid = _project(live, "scratch")
    cid, got = _propose_project(live, ai, "archive")
    assert _result(got["script"]).get("proposed") is True
    assert "ran" not in _result(got["script"])
    assert _card(live, cid)["state"] == "pending"
    assert not projects.load()[pid].archived


def test_the_per_turn_cap_leaves_the_rest_as_cards(live, ai, monkeypatch):
    monkeypatch.setattr(ask_orchestrator, "MAX_ACTS_PER_TURN", 1)
    _set("standard")
    _project(live, "scratch")
    script = ai(
        calls(("list_projects", {})),
        call("propose_project_change", {"action": "rename", "project_index": 0, "name": "a1"},
             cid="p1"),
        call("propose_project_change", {"action": "color", "project_index": 0,
                                        "color": "#336699"}, cid="p2"),
        answer("Done."),
    )  # fmt: skip
    cid = live.new()
    assert live.ask(cid, "rename and colour it").status_code == 200
    first, second = (json.loads(r["content"]) for r in script.tool_results(3)[-2:])
    assert first["ran"] == "done"
    assert second["ran"] is False and "already" in second["why_not"]
    states = sorted(p["state"] for t in live.get(cid)["turns"] for p in t["proposals"])
    assert states == ["done", "pending"]


# ---- the claim ----------------------------------------------------------------------------------


def test_two_concurrent_acts_on_one_proposal_run_once(live, ai, monkeypatch):
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now()
    writes: list[str] = []
    real = asgi_call.call

    async def counted(app, operator, method, path, body, **kw):
        if method != "GET":
            writes.append(path)
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", counted)

    async def both():
        op = _operator(live)
        return await asyncio.gather(
            *(ask_operations.act(cid, aid, operator=op, app=live.app, grant=grant,
                                 record=_record()) for _ in range(2))
        )  # fmt: skip

    results = asyncio.run(both())
    assert len(writes) == 1, writes
    assert sorted(k for k, _ in results) in (["ran", "waits"], ["decided", "ran"])
    assert projects.load()[pid].name == "scratch-2"


def test_a_restart_mid_act_reads_interrupted_and_is_never_replayed(live, ai):
    pid, cid, aid = _rename_card(live, ai)
    ask_proposals._update(cid, aid, state="deciding", decided_at=time.time(),
                          decided_by="hq", authority_rev=1)  # fmt: skip
    hq_acts.append({"pid": aid, "cid": cid, "op": "project.rename", "state": "deciding"})
    (row,) = hq_acts.recent()  # the log resolves it against the sidecar: interrupted
    assert row["state"] == "interrupted"
    card = _card(live, cid)
    assert card["state"] == "interrupted" and not card["can_confirm"]
    assert projects.load()[pid].name == "scratch"


def test_the_log_reports_the_sidecar_outcome_after_a_crash_ate_the_settle_line(live, ai):
    _pid, cid, aid = _rename_card(live, ai)
    ask_proposals._update(cid, aid, state="done", outcome="Renamed.", decided_by="hq")
    hq_acts.append({"pid": aid, "cid": cid, "op": "project.rename", "state": "deciding"})
    (row,) = hq_acts.recent()
    assert row["state"] == "done" and row["outcome"] == "Renamed."


def test_an_act_after_logout_is_refused_before_the_effect(live, ai):
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now()
    asyncio.run(hq_authority.logged_out())
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits" and "signed out" in why["reason"]
    assert _card(live, cid)["state"] == "pending" and projects.load()[pid].name == "scratch"


def test_an_act_after_authority_was_lowered_mid_turn_is_refused(live, ai):
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now()
    _set("ask_first")
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits" and "changed the Authority" in why["reason"]
    assert _card(live, cid)["state"] == "pending" and projects.load()[pid].name == "scratch"


@pytest.mark.parametrize("revoke", ["lower", "logout"])
def test_a_revocation_during_the_facts_reread_runs_nothing(live, ai, monkeypatch, revoke):
    """The second admission check sits at the route dispatch: a lowering or a logout while the
    claimed act re-reads its facts returns the card to pending, nothing run."""
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now()
    real = ask_operations.facts_for
    seen = {"n": 0}

    async def slow_facts(*a, **kw):
        seen["n"] += 1
        if seen["n"] == 1:  # `_run`'s re-read, after the claim
            if revoke == "lower":
                _set("ask_first")
            else:
                await hq_authority.logged_out()
        return await real(*a, **kw)

    monkeypatch.setattr(ask_operations, "facts_for", slow_facts)
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits", why
    card = _card(live, cid)
    assert card["state"] == "pending" and card["can_confirm"] and "decided_by" not in card
    assert projects.load()[pid].name == "scratch"
    assert hq_acts.recent() == []  # nothing ran, so the acts log lists nothing


def test_a_lowering_during_the_request_preparation_runs_nothing(live, ai, monkeypatch):
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now()
    op = ask_operations.CATALOG["project.rename"]

    def route(target, fields, facts):
        _set("ask_first")  # while `_route_once` builds the request off the loop
        return op.route(target, fields, facts)

    replaced = dataclasses.replace(op, route=route)
    monkeypatch.setitem(ask_operations.CATALOG, "project.rename", replaced)
    kind, _why = _act(live, cid, aid, grant)
    assert kind == "waits"
    assert _card(live, cid)["state"] == "pending" and projects.load()[pid].name == "scratch"


def test_an_act_of_a_call_turn_after_the_call_ended_is_refused(live, ai):
    pid, cid, aid = _rename_card(live, ai)
    _set("standard")
    grant = hq_authority.ActGrant.now(live=lambda: False)
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits" and "call" in why["reason"]
    assert projects.load()[pid].name == "scratch"


def test_a_turn_without_an_operator_or_a_continuation_cannot_act():
    _set("standard")
    assert ask_orchestrator._act_grant("c", None, None, None) is None
    assert ask_orchestrator._act_grant("c", "card", object(), object()) is None
    # A call turn with no live call for this conversation never acts.
    assert ask_orchestrator._act_grant("c", "call", object(), object()) is None
    assert ask_orchestrator._act_grant("c", None, object(), object()) is not None


def test_the_claim_log_fails_closed(live, ai, monkeypatch):
    pid, cid, aid = _rename_card(live, ai)
    monkeypatch.setattr(hq_acts, "append", lambda row: False)
    kind, why = _act(live, cid, aid)
    assert kind == "waits" and "log" in why["reason"]
    assert _card(live, cid)["state"] == "pending" and projects.load()[pid].name == "scratch"


# ---- taint, recorded ----------------------------------------------------------------------------


def _status(a, cid) -> dict:
    recs = [r for r in ask_store.read(cid) if r.get("type") == "status"]
    return recs[-1]


def test_taint_is_recorded_per_turn_and_carried_by_the_conversation(live, ai):
    _project(live, "scratch")
    ai(calls(("list_projects", {})), answer("One project."))
    cid = live.new()
    live.ask(cid, "which projects?")
    assert _status(live, cid)["tainted"] is False
    assert not ask_orchestrator._history_tainted(cid, "next-turn")
    ai(calls(("list_sessions", {})), answer("Here they are."))
    live.ask(cid, "and the sessions?")
    assert _status(live, cid)["tainted"] is True
    assert ask_orchestrator._history_tainted(cid, "next-turn")


def test_a_reversible_act_still_runs_in_a_tainted_turn(live, ai):
    """Epic §1: screen and reversible act even after HQ read untrusted text; the act's log row
    records the taint for the later classes' fallback."""
    _set("standard")
    pid = _project(live, "scratch")
    ai(
        calls(("list_sessions", {}), ("list_projects", {})),
        call("propose_project_change", {"action": "rename", "project_index": 0, "name": "t2"},
             cid="p"),
        answer("Renamed."),
    )  # fmt: skip
    cid = live.new()
    live.ask(cid, "rename it")
    assert projects.load()[pid].name == "t2"
    raw = [json.loads(line) for line in hq_acts.path().read_text().splitlines()]
    assert raw and all(r["tainted"] is True for r in raw)


SECRET = "alpha-bravo-charlie-delta-canary"  # plain words: only the known-secret net catches it


@pytest.mark.parametrize("field", ["outcome", "title", "label"])
def test_a_known_secret_straddling_the_cut_never_reaches_the_acts_log(field):
    """Hermes on #2393: the log's text is redacted WHOLE, then cut. A secret that straddles the
    cap would otherwise leave its prefix in the log, and in `/api/hq/acts` and `list_acts`."""
    from agent_sessions import template_secrets

    template_secrets.register_typed([SECRET])
    text = "x" * (hq_acts.TEXT_MAX - 10) + " " + SECRET + " and more"
    assert hq_acts.append({"pid": "a" * 32, "cid": "", "state": "failed", field: text})
    raw = hq_acts.path().read_text()
    assert SECRET[:8] not in raw
    (row,) = hq_acts.recent()
    assert SECRET[:8] not in json.dumps(row)
