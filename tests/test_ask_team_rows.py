"""The `assemble` card lists each step's pick as a row (#2130).

One row per specialist-eligible step, in plan order, projected from the SAME dry run that bound
the card's team digest, stored with the card's entry through its own validator and shown as it
was STAGED — a stale card keeps its rows; today's team is never shown in their place.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import copy
import json

import pytest

from agent_sessions import ask_operations, ask_store, ask_team, missions
from agent_sessions import mission_plan_document as plan_doc
from agent_sessions import mission_team as team
from agent_sessions.playbooks import schema
from mission_team_fixture import FLOW, context, document, pool, store_plan
from test_ask_orchestrator import ai, answer, call, calls  # noqa: F401
from test_ask_team import _assign_by_route, _revisions, live  # noqa: F401

#: The fixture plan plus an external step and a note: three kinds of step no specialist fills.
MIXED = (
    FLOW
    + """
[[steps]]
id = "courier"
title = "Courier the papers"
actor = { kind = "external" }
after = ["record"]
[[steps.checklist]]
key = "couriered"
title = "Couriered"
probe = "none"

[[steps]]
id = "memo"
title = "A note for later"
actor = { kind = "none" }
after = ["courier"]
[[steps.checklist]]
key = "noted"
title = "Noted"
probe = "none"
"""
)
AGENT_STEPS = [
    "o1.posting",
    "o1.cv",
    "o1.cover_letter",
    "o1.documents",
    "o1.review",
    "o1.pull_request",
    "o1.record",
]


def _stage(live, ai, mid: str) -> tuple[str, dict, dict]:
    script = ai(
        calls(("list_missions", {})),
        calls(("read_plan", {"mission_id": mid})),
        call("assemble", {"mission_id": mid, "reason": "go"}),
        answer("done"),
    )
    cid = live.new()
    assert live.ask(cid, "assemble").status_code == 200
    result = json.loads(script.tool_results(3)[-1]["content"])
    assert result.get("proposed") is True, result
    (card,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    assert card["op"] == "team.assemble"
    return cid, card, result


def _mixed_mission(title: str) -> str:
    mid = missions.create_mission("apply for the job, mixed", title=title)["id"]
    store_plan(mid, document(MIXED))
    return mid


# ---- the real staged card ------------------------------------------------------------------------


def test_one_row_per_eligible_step_in_plan_order_from_the_bound_dry_run(live, ai, monkeypatch):
    """A mixed-actor plan: operator, external and note steps get no row; every agent step gets one,
    in plan order, whatever its state — picked, kept (by hand before) or unassigned (nobody in the
    pool qualifies for the review)."""
    mid = _mixed_mission("Apply mixed")
    kept_id = _assign_by_route(live, mid, "o1.posting", 1)
    # Nobody in the pool qualifies for the review once its only reviewer is gone.
    monkeypatch.setattr(team, "snapshot", lambda: context(specs=pool(application_reviewer=None)))
    cid, card, result = _stage(live, ai, mid)
    rows = card["team"]
    assert [r["step"] for r in rows] == AGENT_STEPS
    by = {r["step"]: r for r in rows}
    assert by["o1.posting"] == {
        **by["o1.posting"],
        "state": "kept",
        "specialist": kept_id,
    }
    assert by["o1.review"]["state"] == "unassigned"
    assert by["o1.review"]["reason"] and set(by["o1.review"]) == {"step", "state", "reason"}
    picked = [r for r in rows if r["state"] == "assembled"]
    assert len(picked) == 5
    for r in picked:
        assert r["engine"] == "claude-api" and r["model"] and type(r["version"]) is int
    # The SAME dry run that bound the digest: the rows are the bound team, in plan order.
    said = {p["step"]: p["specialist"] for p in result["team"]["assembled"]}
    assert {r["step"]: r["specialist"] for r in picked} == said
    assert card["team_revision"] == 2
    # A card with rows names no picks in its sentence.
    assert "→" not in card["text"] and "Picks the cheapest" in card["text"]


def test_a_stale_card_keeps_its_staged_rows_across_reload_and_is_not_confirmable(
    live, ai, monkeypatch
):
    """The pool moves after staging: the tap finds the team changed (`stale`, nothing written),
    and every later read shows the rows the card was STAGED with — the gone pick included —
    never today's team."""
    mid = _mixed_mission("Apply stale")
    cid, card, _ = _stage(live, ai, mid)
    staged = card["team"]
    gone = next(r["specialist"] for r in staged if r["step"] == "o1.record")
    monkeypatch.setattr(
        team, "snapshot", lambda: context(specs=pool(**{gone.replace("-", "_"): None}))
    )
    d = live.decide(card["action_id"], {"decision": "confirm"})
    assert d.status_code in (200, 202), d.text
    assert live.settled(cid)["state"] == "stale"
    assert _revisions(mid) == 1, "nothing was written"
    for _ in range(2):  # a reload, then another
        (again,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
        assert again["state"] == "stale" and again["can_confirm"] is False
        assert again["team"] == staged, "the staged rows, never today's team"
        assert any(r.get("specialist") == gone for r in again["team"])


def test_a_card_staged_before_rows_shows_its_stored_sentence_and_no_rows(live, ai, monkeypatch):
    """A legacy entry carries no `display`: the view has no rows, and nothing is recomputed."""
    real = ask_team.facts

    async def legacy(target, fields, app, operator):
        out = await real(target, fields, app, operator)
        if out is not None:
            out.pop("display", None)
            out["picks"] = ": o1.cv → somebody and 5 more"  # what the old sentence said
        return out

    monkeypatch.setattr(ask_team, "facts", legacy)
    cid, card, _ = _stage(live, ai, _mixed_mission("Apply legacy"))
    assert "team" not in card and "team_revision" not in card
    assert ": o1.cv → somebody and 5 more" in card["text"]


# ---- the projection, at the unit level -----------------------------------------------------------


def _doc_and_ctx():
    doc = document(MIXED)
    return doc, context()


def test_an_agent_step_without_a_role_gets_no_row():
    """`mission_team.assignable`: only an agent step WITH a role. A stored plan cannot hold a
    role-less agent step (it must match its procedure), so this is the projection's own rule."""
    doc, ctx = _doc_and_ctx()
    roleless = copy.deepcopy(doc)
    for s in roleless["steps"]:
        if s["id"] == "o1.documents":
            s["role"] = None
    _, rows = ask_team._preview_rows(roleless, roleless, ctx)
    assert [r["step"] for r in rows] == [s for s in AGENT_STEPS if s != "o1.documents"]


def test_no_eligible_step_is_no_rows_and_the_preview_is_unchanged():
    doc, ctx = _doc_and_ctx()
    none = copy.deepcopy(doc)
    for s in none["steps"]:
        s["role"] = None
    preview, rows = ask_team._preview_rows(none, none, ctx)
    assert rows == [] and preview == {"assembled": [], "kept": [], "unassigned": []}


def test_the_preview_and_its_digest_are_exactly_what_they_were():
    """The rows come from the same pass, but the preview a card binds must be byte-for-byte what
    it was before #2130, or every card staged before the deploy would go stale at its tap. The
    digest is pinned as a LITERAL, computed from `origin/main`'s `ask_team` before this change."""
    doc, ctx = document(), context()
    op = {
        "op": "assemble",
        "operation_id": "00000000-0000-4000-8000-000000000000",
        "expected_revision": 1,
        "reason": "x",
    }
    new = team.apply_op(doc, team.parse_op(op), ctx, mission_id="msn_" + "a" * 32, revision=1)
    preview, rows = ask_team._preview_rows(doc, new, ctx)
    assert ask_team.team_digest(preview) == "05a81581d19534c9"
    assert ask_team._preview(doc, new, ctx) == preview
    assert [r["step"] for r in rows] == AGENT_STEPS


# ---- the store's own validator -------------------------------------------------------------------


def _row(i: int = 1, **over) -> dict:
    return {
        "step": f"o{i}.step",
        "state": "assembled",
        "specialist": "career-archivist",
        "version": 2,
        "engine": "claude-api",
        "model": "sonnet",
        **over,
    }


def _admit(display, *, secrets=(), fresh=True):
    return ask_store._admit_display(display, list(secrets), fresh=fresh)


def test_the_row_bound_is_the_plans_own_step_bound():
    assert ask_store.TEAM_ROWS_MAX == plan_doc.MAX_OBJECTIVES * schema.MAX_STEPS
    at_bound = [_row(1 + i % 99) for i in range(ask_store.TEAM_ROWS_MAX)]
    assert len(_admit({"revision": 3, "team": at_bound})["team"]) == ask_store.TEAM_ROWS_MAX
    with pytest.raises(ValueError):
        _admit({"revision": 3, "team": at_bound + [_row()]})


def test_long_identifiers_are_kept_whole():
    long = _row(
        99,
        step="o99." + "a" * 32,
        specialist="s" + "x" * 47,
        engine="e" + "n" * 63,
        model="M" + "m" * 95,
    )
    assert _admit({"revision": 1, "team": [long]})["team"] == [long]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"team": []},
        {"revision": 1, "team": [], "extra": 1},
        {"revision": 0, "team": []},
        {"revision": True, "team": []},
        {"revision": 1, "team": {"o1.a": "x"}},
        {"revision": 1, "team": [_row(state="fired")]},
        {"revision": 1, "team": [_row(step="../etc")]},
        {"revision": 1, "team": [_row(specialist="Bad Id")]},
        {"revision": 1, "team": [_row(version=True)]},
        {"revision": 1, "team": [_row(model={"nested": "x"})]},
        {"revision": 1, "team": [{**_row(), "html": "<b>x</b>"}]},
        {"revision": 1, "team": [{"step": "o1.a", "state": "unassigned"}]},
        {"revision": 1, "team": [{"step": "o1.a", "state": "unassigned", "reason": ["x"]}]},
        {
            "revision": 1,
            "team": [{"step": "o1.a", "state": "unassigned", "reason": "x", "specialist": "s"}],
        },
    ],
)
def test_a_malformed_or_nested_display_is_refused(bad):
    with pytest.raises(ValueError):
        _admit(bad)


def test_a_reason_is_guarded_like_every_stored_text():
    secret = "tok-9f8e7d6c5b4a39281706"
    row = {"step": "o1.a", "state": "unassigned", "reason": f"needs {secret} to qualify"}
    with pytest.raises(ask_store.Withheld):
        _admit({"revision": 1, "team": [row]}, secrets=[secret])  # a NEW card: refused
    read = _admit({"revision": 1, "team": [row]}, secrets=[secret], fresh=False)  # a stored one
    assert read is None  # withheld whole


def test_the_view_shows_the_rows_only_when_the_entry_carries_them():
    rows = [_row()]
    shown = ask_operations.view(
        {"state": "stale", "op": "team.assemble", "display": {"revision": 4, "team": rows}}
    )
    assert shown["team"] == rows and shown["team_revision"] == 4 and shown["can_confirm"] is False
    legacy = ask_operations.view({"state": "pending", "op": "team.assemble"})
    assert "team" not in legacy and "team_revision" not in legacy


def test_the_display_never_binds_and_is_never_compared_at_the_tap():
    facts = {"title": "A", "revision": 2, "team": "0123456789abcdef", "display": {"x": 1}}
    lifted, display = ask_operations.lift_display(facts)
    assert display == {"x": 1} and "display" not in lifted and "display" in facts
    assert ask_operations.changed(lifted, {**lifted, "display": {"y": 2}}) == ""
    assert ask_operations.lift_display(None) == (None, None)


SECRET = "tok-9f8e7d6c5b4a39281706"  # synthetic; shaped like a specialist id AND a model id


@pytest.mark.parametrize("field", ["step", "specialist", "engine", "model"])
def test_an_identifier_that_is_a_secret_is_refused_new_and_withheld_stored(field):
    """Hermes on #2226: the identifiers go through the store boundary too. A NEW card carrying a
    registered secret's value as an identifier is refused; a STORED one (the secret registered
    after staging) has its display withheld whole, through the operation's admission and the
    view: no rows, never the value, never an identifier rewritten into something else."""
    # A secret that fits the field's own grammar (a step id is `o<n>.<name>`).
    value = "o1." + SECRET.replace("-", "_") if field == "step" else SECRET
    row = _row(**{field: value})
    with pytest.raises(ask_store.Withheld):
        _admit({"revision": 1, "team": [row]}, secrets=[value])
    assert _admit({"revision": 1, "team": [row]}, secrets=[value], fresh=False) is None
    entry = {
        "state": "stale",
        "op": "team.assemble",
        "target": "msn_" + "a" * 32,
        "binding": "0" * 64,
        "fields": {"op": "assemble"},
        "facts": {"revision": 1},
        "display": {"revision": 1, "team": [_row(), row]},
    }
    stored = ask_store._admit_operation(entry, "p" * 32, "t-1", [value], fresh=False)
    assert "display" not in stored and value not in json.dumps(stored)
    shown = ask_operations.view(stored)
    assert "team" not in shown and value not in json.dumps(shown)
    # Without the secret registered, the same entry keeps its rows: identifiers are checked
    # against REGISTERED secrets, not the credential-shape heuristic.
    plain = ask_store._admit_operation(entry, "p" * 32, "t-1", [], fresh=False)
    assert ask_operations.view(plain)["team"][1][field] == value


def test_real_dated_model_ids_are_never_refused_as_credential_shaped():
    """The credential-shape heuristic flags `claude-opus-4-5-20251101`; identifiers are checked
    against registered secrets only, so a card picking a dated model stages (and is shown)."""
    row = _row(model="claude-opus-4-5-20251101", specialist="career-archivist")
    assert ask_store.guard_text("claude-opus-4-5-20251101", 200, secrets=[]) != (
        "claude-opus-4-5-20251101"
    ), "the heuristic no longer flags it; this test's premise moved"
    assert _admit({"revision": 1, "team": [row]})["team"] == [row]


def test_a_pending_card_whose_team_was_withheld_offers_no_confirm():
    """Independent review of #2226: a pending card whose rows were withheld (a secret registered
    since) must not confirm a team the operator cannot see. A card staged before #2126 (no digest)
    or #2130 (its stored sentence names the picks) still confirms as before."""
    hidden = {"state": "pending", "op": "team.assemble", "facts": {"revision": 1, "team": "a" * 16}}
    assert ask_operations.team_hidden(hidden)
    assert ask_operations.view(hidden)["can_confirm"] is False
    assert ask_operations.view(hidden)["reason"] == ask_operations.TEAM_HIDDEN  # says why
    assert ask_operations.view(hidden)["can_dismiss"] is True
    shown = {**hidden, "display": {"revision": 1, "team": [_row()]}}
    assert ask_operations.view(shown)["can_confirm"] is True
    pre_2126 = {"state": "pending", "op": "team.assemble", "facts": {"revision": 1}}
    pre_2130 = {**hidden, "facts": {**hidden["facts"], "picks": ": o1.cv → x"}}
    for legacy in (pre_2126, pre_2130):
        assert not ask_operations.team_hidden(legacy)
        assert ask_operations.view(legacy)["can_confirm"] is True
    other = {**hidden, "op": "team.assign"}
    assert not ask_operations.team_hidden(other)


def test_confirming_a_card_whose_team_was_withheld_is_stale_and_writes_nothing(live, ai):
    """End to end: a staged card's rows withheld by a later sidecar write (a secret registered
    since) — its tap settles it `stale` with the server's reason, and the plan is not written."""
    mid = _mixed_mission("Apply withheld")
    cid, card, _ = _stage(live, ai, mid)
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        for r in rows:
            r.pop("display", None)  # what `_admit_display` withholding leaves
        ask_store.write_proposals(cid, rows)
    (again,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    assert "team" not in again and again["can_confirm"] is False
    d = live.decide(card["action_id"], {"decision": "confirm"})
    assert d.status_code in (200, 202), d.text
    settled = live.settled(cid)
    assert settled["state"] == "stale", settled
    assert "can no longer be shown" in json.dumps(settled)
    assert _revisions(mid) == 1, "a team nobody could see was written"


# ---- Hermes on #2226: withheld on the READ path too, not only at the next write --------------


@pytest.fixture
def typed_reset():
    from agent_sessions import template_secrets

    template_secrets._reset_for_tests()
    yield template_secrets
    template_secrets._reset_for_tests()


def test_a_secret_registered_after_staging_is_withheld_on_the_next_read(live, ai, typed_reset):
    """Stage a card, register one of its identifiers as a secret (typed, as a send would), then
    READ the conversation twice with no write in between: the rows are gone from both reads, the
    value appears in neither, the card offers no Confirm and says why, and its tap is stale."""
    mid = _mixed_mission("Read withheld")
    cid, card, _ = _stage(live, ai, mid)
    assert card["team"], "the staged card shows its rows"
    secret = next(r["specialist"] for r in card["team"] if r.get("specialist"))
    assert len(secret) >= typed_reset.SECRET_MIN
    stored = json.dumps(ask_store.read_proposals(cid))
    typed_reset.register_typed([secret])
    for _ in range(2):
        body = live.get(cid)
        (again,) = [p for t in body["turns"] for p in t["proposals"]]
        assert "team" not in again and secret not in json.dumps(body)
        assert again["can_confirm"] is False and again["reason"] == ask_operations.TEAM_HIDDEN
    assert json.dumps(ask_store.read_proposals(cid)) == stored, "a read wrote the sidecar"
    d = live.decide(card["action_id"], {"decision": "confirm"})
    assert d.status_code in (200, 202), d.text
    assert live.settled(cid)["state"] == "stale"
    assert _revisions(mid) == 1, "a team the operator could not see was written"


def test_the_rows_are_not_shown_when_the_secret_set_cannot_be_established(monkeypatch):
    """Fail closed: a read that cannot learn the registered secrets shows no rows."""
    from agent_sessions import template_secrets

    def unavailable():
        raise template_secrets.RedactionUnavailable("the secret store cannot be read")

    row = {
        "state": "pending",
        "op": "team.assemble",
        "facts": {"revision": 1, "team": "a" * 16},
        "display": {"revision": 1, "team": [_row()]},
    }
    assert ask_operations.view(row)["team"] == [_row()]
    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    shown = ask_operations.view(row)
    assert "team" not in shown and shown["can_confirm"] is False
