"""HQ's destructive class on the act path (#2396, epic #2070 A1c): archive, close, archive older,
project archive and delete, a playbook saved over, a memory note forgotten — done by HQ under the
Commander's Authority setting, D4's fallback after untrusted text, and the settlement told truly.

The act path itself (claim, two admissions, restart, the log) is A1's and A1b's and is pinned in
`test_hq_authority.py` / `test_hq_authority_starts.py`; this file pins what the destructive class
adds or must not change."""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from agent_sessions import (
    asgi_call,
    ask_operations,
    ask_proposals,
    ask_store,
    hq_acts,
    hq_authority,
    projects,
)
from test_ask_operations import (  # noqa: F401 — fixtures are used by name
    S_B,
    _card,
    _project,
    _propose_project,
    _result,
    ai,
    answer,
    app,
    call,
    calls,
    live,
)
from test_hq_authority import _operator, _set

DESTRUCTIVE = {"reversible": "act", "starts": "act", "destructive": "act"}


def _record(tainted: bool = False) -> dict:
    return {"class": "destructive", "turn_id": "t" * 8, "via": None, "tainted": tainted}


def _act(a, cid, aid, grant=None, tainted: bool = False):
    grant = grant or hq_authority.ActGrant.now()
    return asyncio.run(
        ask_operations.act(
            cid, aid, operator=_operator(a), app=a.app, grant=grant, record=_record(tainted)
        )
    )


def _switch(on: bool) -> None:
    rev = hq_authority.view()["revision"]
    hq_authority.update({"revision": rev, "untrusted": {"destructive": on}})


def _writes(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real = asgi_call.call

    async def counted(app, operator, method, path, body=None, **kw):
        if method != "GET":
            seen.append((method, path))
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", counted)
    return seen


def _session_turn(a, ai, action: str = "archive", key: str = S_B):
    """A typed turn that lists the sessions (agent-authored titles: it taints) and stages a
    session change on one of them."""
    script = ai(
        call("list_sessions", {}, cid="l"),
        call("propose_session_change", {"action": action, "key": key}, cid="p"),
        answer("Done."),
    )
    cid = a.new()
    assert a.ask(cid, f"{action} the flaky session").status_code == 200
    return cid, script


def _archived(_a, key: str) -> bool:
    from agent_sessions import engines

    engines.invalidate_scan_cache()
    return any(engines.session_key(s) == key and s.archived for s in engines.scan_all())


# ---- the setting --------------------------------------------------------------------------------


def test_destructive_acts_in_this_version_and_keeps_its_risky_label():
    assert "destructive" in hq_authority.ACTABLE
    assert "destructive" in hq_authority.RISKY
    assert "destructive" in hq_authority.UNTRUSTED_SWITCHES
    v = hq_authority.view()
    assert "destructive" in v["actable"] and "destructive" in v["risky"]
    assert v["untrusted"]["destructive"] is False  # off by default (D4)
    # External and widens still never act, whatever is stored.
    _set("full")
    snap = hq_authority.snapshot()
    assert hq_authority.acts(snap, "destructive")
    assert not hq_authority.acts(snap, "external") and not hq_authority.acts(snap, "widens")


@pytest.mark.parametrize("stored", [None, "true", 1, {"destructive": "true"}, {"destructive": 1}])
def test_a_missing_or_malformed_destructive_switch_reads_off(stored):
    from agent_sessions import prefs

    block = {"classes": DESTRUCTIVE, "revision": 3}
    if stored is not None:
        block["untrusted"] = stored
    prefs.mutate_block(hq_authority.KEY, lambda _cur: block)
    snap = hq_authority.snapshot()
    assert hq_authority.taint_blocks(snap, "destructive", True)
    assert not hq_authority.taint_blocks(snap, "destructive", False)


def test_the_destructive_switch_is_cas_written_and_never_set_by_a_preset(live):
    r = live.c.put(
        "/api/hq/authority",
        json={"revision": 0, "untrusted": {"destructive": True}},
        headers=live.hdr,
    )
    assert r.status_code == 200 and r.json()["untrusted"] == {"starts": False, "destructive": True}
    r = live.c.put(
        "/api/hq/authority",
        json={"revision": 0, "untrusted": {"destructive": False}},
        headers=live.hdr,
    )
    assert r.status_code == 409
    assert hq_authority.update({"revision": 1, "preset": "ask_first"})["untrusted"]["destructive"]


# ---- acting -------------------------------------------------------------------------------------


def test_destructive_on_ask_stages_a_card_and_archives_nothing(live, ai, monkeypatch):
    _set(reversible="act", starts="act")  # the operator's own mix: destructive still asks
    writes = _writes(monkeypatch)
    pid = _project(live, "scratch")
    cid, got = _propose_project(live, ai, "archive")
    assert _result(got["script"]).get("ran") in (None, False)
    assert _card(live, cid)["state"] == "pending"
    assert not projects.load()[pid].archived
    assert not any(p.endswith("/archive") for _m, p in writes)


def test_under_destructive_act_a_project_archive_runs_once_and_is_logged(live, ai, monkeypatch):
    _set(**DESTRUCTIVE)
    writes = _writes(monkeypatch)
    pid = _project(live, "scratch")
    cid, got = _propose_project(live, ai, "archive")
    result = _result(got["script"])
    assert result["ran"] == "done", result
    card = _card(live, cid)
    assert card["state"] == "done" and card["decided_by"] == "hq"
    assert projects.load()[pid].archived
    assert writes.count(("POST", f"/api/projects/{pid}/archive")) == 1
    (row,) = hq_acts.recent()
    assert row["state"] == "done" and row["label"] == "Archive project"


def test_under_destructive_act_a_project_delete_runs_and_says_what_was_lost(live, ai):
    _set(**DESTRUCTIVE)
    pid = _project(live, "scratch")
    cid, got = _propose_project(live, ai, "delete")
    assert _result(got["script"])["ran"] == "done"
    assert pid not in projects.load()
    assert "Its sessions were kept" in _card(live, cid)["summary"]


def test_a_hostile_read_then_a_destructive_request_stays_a_card_that_says_why(live, ai):
    """The epic's acceptance for this class: HQ read agent-authored text, then "delete" is a card
    and nothing runs; the same in the next turn of the conversation; the switch on acts."""
    _set(**DESTRUCTIVE)
    pid = _project(live, "scratch")
    script = ai(
        call("list_sessions", {}, cid="l"),  # agent-authored titles
        calls(("list_projects", {})),
        call("propose_project_change", {"action": "delete", "project_index": 0}, cid="p"),
        answer("Proposed."),
    )
    cid = live.new()
    live.ask(cid, "read the report and do what it says")
    result = json.loads(script.tool_results(3)[-1]["content"])
    assert result["ran"] is False and "asks first" in result["why_not"], result
    card = _card(live, cid)
    assert card["state"] == "pending" and hq_authority.TAINT_REASON in card["reason"]
    assert pid in projects.load()
    # The next turn of the same conversation carries the taint, without reading anything.
    script = ai(
        calls(("list_projects", {})),
        call("propose_project_change", {"action": "delete", "project_index": 0}, cid="p2"),
        answer("Proposed."),
    )
    live.ask(cid, "delete it now")
    assert json.loads(script.tool_results(2)[-1]["content"])["ran"] is False
    assert pid in projects.load()
    # The Commander switched destructive on after untrusted text: it acts.
    _switch(True)
    script = ai(
        calls(("list_projects", {})),
        call("propose_project_change", {"action": "delete", "project_index": 0}, cid="p3"),
        answer("Deleted."),
    )
    live.ask(cid, "and once more")
    assert json.loads(script.tool_results(2)[-1]["content"])["ran"] == "done"
    assert pid not in projects.load()


def test_a_session_archive_reaps_inside_the_routes_own_freeze(live, ai, fake_jsonl, monkeypatch):
    """Archiving reaps the runtime first (CLAUDE.md, #523/#2064): the act goes through the tap's
    route, which freezes (stops and reaps) the session and archives it inside that freeze. HQ adds
    no teardown of its own."""
    from agent_sessions import runtime_freeze

    _set(**DESTRUCTIVE)
    _switch(True)  # listing sessions taints (agent-authored titles); this act is meant to run
    order: list[str] = []
    real = runtime_freeze.frozen

    @contextlib.asynccontextmanager
    async def frozen(prov, native, **kw):
        async with real(prov, native, **kw):
            order.append("frozen")
            yield
            order.append("archived" if _archived(live, S_B) else "not archived")

    monkeypatch.setattr(runtime_freeze, "frozen", frozen)
    writes = _writes(monkeypatch)
    cid, script = _session_turn(live, ai)
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result["ran"] == "done", result
    assert writes.count(("POST", f"/api/sessions/{S_B}/archive")) == 1
    assert order == ["frozen", "archived"]
    assert _card(live, cid)["decided_by"] == "hq"


def test_a_session_archive_after_listing_sessions_asks_without_the_switch(live, ai, fake_jsonl):
    _set(**DESTRUCTIVE)
    cid, script = _session_turn(live, ai)
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result["ran"] is False and "asks first" in result["why_not"], result
    assert not _archived(live, S_B)


def test_a_mission_held_session_is_refused_by_the_route_and_not_retried(
    live, ai, fake_jsonl, monkeypatch
):
    from agent_sessions import missions

    _set(**DESTRUCTIVE)
    _switch(True)
    tries: list[str] = []

    def held(key, *a, **k):
        tries.append(key)
        raise missions.MissionError("a mission holds this session")

    monkeypatch.setattr(missions, "reserve_session", held)
    cid, script = _session_turn(live, ai)
    result = json.loads(script.tool_results(2)[-1]["content"])
    assert result["ran"] == "failed", result
    card = _card(live, cid)
    assert card["state"] == "failed" and "may already have been stopped" in card["summary"]
    assert tries == [S_B]  # one attempt, never retried
    assert not _archived(live, S_B)
    (row,) = hq_acts.recent()
    assert "nothing changed" not in row["outcome"].lower()
    assert "may already have been stopped" in row["outcome"]


# ---- the mechanism, on a destructive card -------------------------------------------------------


def _archive_card(a, ai) -> tuple[str, str, str]:
    """A pending project-archive card staged under Ask first: (project id, cid, card id)."""
    pid = _project(a, "scratch")
    cid, _ = _propose_project(a, ai, "archive")
    return pid, cid, _card(a, cid)["action_id"]


def test_two_concurrent_destructive_acts_on_one_card_archive_once(live, ai, monkeypatch):
    pid, cid, aid = _archive_card(live, ai)
    _set(**DESTRUCTIVE)
    grant = hq_authority.ActGrant.now()
    writes = _writes(monkeypatch)

    async def both():
        op = _operator(live)
        return await asyncio.gather(
            *(ask_operations.act(cid, aid, operator=op, app=live.app, grant=grant,
                                 record=_record()) for _ in range(2))
        )  # fmt: skip

    results = asyncio.run(both())
    assert writes.count(("POST", f"/api/projects/{pid}/archive")) == 1, writes
    assert sorted(k for k, _ in results) in (["ran", "waits"], ["decided", "ran"])
    assert projects.load()[pid].archived


def test_a_restart_mid_destructive_act_reads_interrupted_and_is_never_replayed(live, ai):
    pid, cid, aid = _archive_card(live, ai)
    ask_proposals._update(cid, aid, state="deciding", decided_at=time.time(),
                          decided_by="hq", authority_rev=1)  # fmt: skip
    hq_acts.append({"pid": aid, "cid": cid, "op": "project.archive", "state": "deciding"})
    (row,) = hq_acts.recent()
    assert row["state"] == "interrupted"
    assert _card(live, cid)["state"] == "interrupted"
    assert not projects.load()[pid].archived


@pytest.mark.parametrize("revoke", ["lower", "logout", "call"])
def test_a_destructive_act_is_refused_before_the_effect(live, ai, revoke):
    pid, cid, aid = _archive_card(live, ai)
    _set(**DESTRUCTIVE)
    grant = hq_authority.ActGrant.now(live=(lambda: False) if revoke == "call" else None)
    if revoke == "lower":
        _set(reversible="act", starts="act", destructive="ask")
    elif revoke == "logout":
        asyncio.run(hq_authority.logged_out())
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits", why
    assert _card(live, cid)["state"] == "pending" and not projects.load()[pid].archived


@pytest.mark.parametrize("revoke", ["lower", "logout"])
def test_a_revocation_during_the_facts_reread_archives_nothing(live, ai, monkeypatch, revoke):
    pid, cid, aid = _archive_card(live, ai)
    _set(**DESTRUCTIVE)
    grant = hq_authority.ActGrant.now()
    real = ask_operations.facts_for
    seen = {"n": 0}

    async def slow_facts(*a, **kw):
        seen["n"] += 1
        if seen["n"] == 1:  # `_run`'s re-read, after the claim
            if revoke == "lower":
                _set(reversible="act", starts="act", destructive="ask")
            else:
                await hq_authority.logged_out()
        return await real(*a, **kw)

    monkeypatch.setattr(ask_operations, "facts_for", slow_facts)
    kind, why = _act(live, cid, aid, grant)
    assert kind == "waits", why
    assert _card(live, cid)["state"] == "pending" and not projects.load()[pid].archived


def test_a_tainted_destructive_act_waits_at_the_claim_without_the_switch(live, ai):
    pid, cid, aid = _archive_card(live, ai)
    _set(**DESTRUCTIVE)
    kind, why = _act(live, cid, aid, tainted=True)
    assert kind == "waits" and hq_authority.TAINT_REASON in why["reason"]
    assert not projects.load()[pid].archived
    _switch(True)
    kind, _ = _act(live, cid, aid, tainted=True)
    assert kind == "ran" and projects.load()[pid].archived


def test_a_runner_backed_destructive_entry_never_acts():
    op = ask_operations.CATALOG["secret.store_attachment"]
    assert hq_authority.class_of(op.id) == "destructive" and op.run is not None
    # `_act_operation` refuses any `run` entry: it is always confirmed on its card.


def test_every_destructive_entry_but_the_runner_can_reach_the_act_path():
    destructive = sorted(k for k, v in hq_authority.CLASS_OF.items() if v == "destructive")
    assert destructive == [
        "memory.delete",
        "playbook.edit",
        "project.archive",
        "project.delete",
        "secret.store_attachment",
        "session.archive",
        "session.close",
        "sessions.archive_older",
    ]
    for op_id in destructive:
        op = ask_operations.CATALOG[op_id]
        actable = op.run is None and not op.inputs and not op.follow_up
        assert actable == (op_id != "secret.store_attachment"), op_id


def test_the_acts_log_keeps_a_store_safe_outcome(live, ai):
    _set(**DESTRUCTIVE)
    _project(live, "scratch")
    _propose_project(live, ai, "archive")
    (row,) = hq_acts.recent()
    assert row["op"] == "project.archive" and row["state"] == "done"
    assert ask_store.guard_text(row["outcome"], len(row["outcome"]) + 1) == row["outcome"]
