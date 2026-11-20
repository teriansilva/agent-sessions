"""The `assemble` card's browser fixture (#2130) is real server output, not hand-written JSON.

`web/e2e/fixtures/assemble-team.json` feeds the Playwright spec of the card's rows. This test
stages the cards through the live app exactly as HQ does — a pending card over a two-objective
plan (14 picks, a kept step, an unassigned step, a long specialist id: far past the old ~120
character sentence), the same kind of card gone stale after the pool moved, and a card staged
before #2130 (no rows, its stored sentence) — and reads them back the way the client does
(`GET` the conversation: the card merged with its live view). It fails if the committed file
differs, so the browser test can never pass against a shape the server does not send.
Regenerate deliberately with `AGENT_SESSIONS_WRITE_FIXTURES=1`.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agent_sessions import ask_team, missions
from agent_sessions import mission_team as team
from mission_team_fixture import context, document, pool, spec, store_plan
from test_ask_orchestrator import ai, answer, call, calls  # noqa: F401
from test_ask_team import _assign_by_route, live  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/assemble-team.json"
#: 48 characters: the longest id the specialist store admits.
LONG = "career-archivist-for-the-eu-tenants-backfill-ops"


def _team_pool() -> list[dict]:
    """The fixture pool with the archivist renamed to the longest id and no reviewer: every
    career-archive step picks the long id, and the review step cannot be filled."""
    return pool(
        career_archivist=None,
        application_reviewer=None,
        **{LONG.replace("-", "_"): spec(LONG, "sonnet", ["career-archive"], capacity=9)},
    )


def _card(live, ai, mid: str) -> tuple[str, dict]:
    ai(
        calls(("list_missions", {})),
        calls(("read_plan", {"mission_id": mid})),
        call("assemble", {"mission_id": mid, "reason": "cheapest that qualifies"}),
        answer("Confirm on the card to assemble the team."),
    )
    cid = live.new()
    assert live.ask(cid, "Staff the migration with the cheapest specialists").status_code == 200
    (card,) = [p for t in live.get(cid)["turns"] for p in t["proposals"]]
    return cid, card


def _mission(title: str) -> str:
    mid = missions.create_mission("migrate the billing exports", title=title)["id"]
    store_plan(mid, document(objectives=2))
    return mid


def _normalise(card: dict, n: int) -> dict:
    out = json.loads(re.sub(r"msn_[0-9a-f]{32}", "msn_" + "f" * 32, json.dumps(card)))
    out["action_id"] = str(n) * 32
    return out


def _build(live, ai, monkeypatch) -> dict:
    monkeypatch.setattr(team, "snapshot", lambda: context(specs=_team_pool()))
    # 1. Pending: one step kept (assigned by hand before), one unassigned, the rest picked.
    mid = _mission("Migrate the billing exports onto the event pipeline")
    _assign_by_route(live, mid, "o1.posting", 1)
    _, pending = _card(live, ai, mid)
    # 2. Stale: staged, then a pick leaves the pool; the tap finds the team changed.
    mid2 = _mission("Migrate the billing exports, second attempt")
    cid2, card2 = _card(live, ai, mid2)
    monkeypatch.setattr(team, "snapshot", lambda: context(specs=pool(application_reviewer=None)))
    assert live.decide(card2["action_id"], {"decision": "confirm"}).status_code in (200, 202)
    assert live.settled(cid2)["state"] == "stale"
    (stale,) = [p for t in live.get(cid2)["turns"] for p in t["proposals"]]
    # 3. Legacy: a card staged before #2130 — no rows; its stored sentence names the picks.
    monkeypatch.setattr(team, "snapshot", lambda: context(specs=_team_pool()))
    real = ask_team.facts

    async def before_rows(target, fields, app, operator):
        out = await real(target, fields, app, operator)
        if out is not None:
            out.pop("display", None)
            out["picks"] = f": o1.cv → cv-writer, o1.documents → {LONG[:40]}… and 11 more"
        return out

    monkeypatch.setattr(ask_team, "facts", before_rows)
    _, legacy = _card(live, ai, _mission("Migrate the billing exports, old card"))
    return {
        "pending": _normalise(pending, 1),
        "stale": _normalise(stale, 2),
        "legacy": _normalise(legacy, 3),
    }


def test_the_committed_fixture_is_what_the_server_sends(live, ai, monkeypatch):
    built = _build(live, ai, monkeypatch)
    pending, stale, legacy = built["pending"], built["stale"], built["legacy"]
    # What the browser spec relies on, asserted on the server's own output.
    assert len(pending["team"]) == 14 and pending["state"] == "pending" and pending["can_confirm"]
    states = [r["state"] for r in pending["team"]]
    assert states.count("kept") == 1 and states.count("unassigned") == 2
    assert any(r.get("specialist") == LONG for r in pending["team"])
    picks = " ".join(f"{r['step']} → {r.get('specialist', '')}" for r in pending["team"])
    assert len(picks) > 120, "the rows hold far more than the old sentence's ~120 characters"
    assert stale["state"] == "stale" and stale["can_confirm"] is False and stale["team"]
    assert "team" not in legacy and "→" in legacy["text"]
    text = json.dumps(built, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("AGENT_SESSIONS_WRITE_FIXTURES") == "1":
        FIXTURE.write_text(text)
    assert FIXTURE.read_text() == text, (
        "web/e2e/fixtures/assemble-team.json is not what the server sends; regenerate it with "
        "AGENT_SESSIONS_WRITE_FIXTURES=1 and review the diff"
    )
