"""HQ reads usage, the fleet and an allowlisted settings view (#1554).

Pinned here, one acceptance item per test (#1554 Phases):

* usage honesty — `source`, `stale`, `probe_failed` exactly; a `none` row has NO percentage;
* the usage tool never probes (no subprocess);
* the settings allowlist by output SHAPE, per section;
* value-level canaries in the very fields the tools DO read, and a planted-secret sweep over the
  fields they must never read — neither reaches any tool output or the conversation dir;
* the fleet's population (the strict read, then roots + exclusions), Pulse's state precedence,
  an incomplete read as unknown (never zeros), and mission totals past the 40-row list cap;
* nothing new writes anything; an end-to-end usage turn stores metadata only.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

import pytest

from agent_sessions import (
    agent_usage,
    ask_orchestrator,
    ask_settings,
    ask_store,
    missions,
    prefs,
    pulse,
    template_secrets,
)
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

NOW = 1_800_000_000.0


def _turn() -> ask_orchestrator._Turn:
    return ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id="00000000-0000-4000-8000-000000000001",
        question="q",
        working_keys=set(),
        progress=lambda ev: None,
    )


def _run(fn, args: dict | None = None):
    out, _meta = asyncio.run(fn(_turn(), args or {}))
    return out


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    """Every registered first-party agent counts as present, whatever is installed on this host
    (a CI runner has none of the vendor CLIs; a laptop has some)."""
    from agent_sessions.engines import registry

    monkeypatch.setattr(registry, "present_providers", lambda: list(registry.all_providers()))


def _engines() -> list[str]:
    from agent_sessions.engines import registry

    return [p.engine_id for p in registry.present_providers() if registry.is_agent(p)]


def _row(engine: str, **over) -> dict:
    base = {
        "engine": engine,
        "source": "none",
        "windows": None,
        "tokens": None,
        "window_days": None,
        "plan": None,
        "error": None,
        "at": 0.0,
        "checked_at": None,
        "stale": False,
        "limit_tokens": 0,
        "manual_used": 0,
        "access": None,
        "used_pct": None,
        "forecast": None,
    }
    base.update(over)
    return base


@pytest.fixture
def usage(monkeypatch):
    """`agent_usage.snapshot` answers with the rows a test sets (the store is pinned elsewhere)."""
    rows: list[dict] = []
    monkeypatch.setattr(agent_usage, "snapshot", lambda **kw: list(rows))
    return rows


# ---- usage ---------------------------------------------------------------------------------------


def test_usage_rows_are_honest_about_what_was_measured(usage, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    e = _engines()
    assert len(e) >= 3, "the test wants three agents present"
    usage += [
        _row(
            e[0],
            source="plan",
            plan="Max 5x",
            windows=[{"label": "5h", "used_pct": 87.0, "resets_at": NOW + 2 * 3600}],
            at=NOW - 1800,
            checked_at=NOW - 1800,
            used_pct=87.0,
        ),
        _row(e[1], source="tokens", tokens={"in": 1000, "out": 500}, window_days=7, at=NOW - 60),
        _row(e[2], source="none"),
    ]
    rows = _run(ask_orchestrator._agent_usage)["agents"]
    plan, tokens, none = rows
    # The vendor's plan name is never sent: no test proves vendor text is not a key (#1569).
    assert plan["source"] == "plan" and "plan" not in plan
    assert plan["windows"] == [{"label": "5h", "used_pct": 87.0, "resets_in_hours": 2.0}]
    assert plan["checked_hours_ago"] == 0.5 and plan["stale"] is False
    assert tokens["source"] == "tokens" and tokens["billable_tokens"] == 1500
    assert tokens["limit_tokens"] is None and "used_pct" not in tokens
    assert none["source"] == "none" and "used_pct" not in none
    assert "NOT 0 %" in none["note"]


def test_stale_and_failed_rows_say_so_and_the_error_text_is_withheld(usage, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    e = _engines()
    usage += [
        _row(e[0], source="plan", windows=[], at=NOW - 7200, stale=True),
        _row(e[1], source="none", error="login failed for canary-person@example.com at /home/x"),
    ]
    stale, failed = _run(ask_orchestrator._agent_usage)["agents"]
    assert stale["stale"] is True and stale["probe_failed"] is False
    assert failed["probe_failed"] is True
    blob = json.dumps(failed)
    assert "canary-person" not in blob and "/home/x" not in blob


def test_a_vendor_plan_is_never_sent_and_an_unknown_window_is_numbered(usage):
    e = _engines()
    usage += [
        _row(
            e[0],
            source="plan",
            plan="pro for canary@corp.example",
            windows=[{"label": "https://evil.example/w", "used_pct": 10.0, "resets_at": None}],
        )
    ]
    (row,) = _run(ask_orchestrator._agent_usage)["agents"]
    assert "plan" not in row
    assert row["windows"][0]["label"] == "window 1"
    assert "canary@" not in json.dumps(row) and "evil.example" not in json.dumps(row)


SEGMENTED = "sk_live_Abcd_Efgh_Ijkl_Mnop_Qrst"  # short letter runs: passes any "plain words" shape


@pytest.mark.parametrize(
    "label, shown",
    [("5h", "5h"), ("90m", "90m"), ("week", "week"), ("2 weeks", "2 weeks"), ("day", "day"),
     ("3 days", "3 days"), ("month (code)", "month (code)"), ("session", "session"),
     ("Week (Opus)", "week (Opus)"), ("week  (all models)", "week (all models)")],
)  # fmt: skip
def test_a_window_label_from_the_closed_set_is_shown_in_its_own_words(label, shown):
    assert ask_settings.window_label(label) == shown


@pytest.mark.parametrize(
    "label",
    [SEGMENTED, "sk-CANARYKEY0001", "AKIAIOSFODNN7EXAMPLE", "Max 5x", "weekly limit",
     "Gemini Models · Weekly Limit", "/home/example", "https://evil.example/w", "12345h", "", None],
)  # fmt: skip
def test_any_other_window_label_is_not_shown(label):
    assert ask_settings.window_label(label) is None


def test_agent_labels_are_the_packaged_manifests_own_words():
    """Authored in this repository: read from the package, never from a loaded manifest."""
    names = ask_settings._packaged_names()
    assert names.get("claude") == "Claude Code" and names.get("codex") == "Codex"
    labels = ask_settings.agent_labels()
    assert all(labels[e] == names[e] for e in _engines() if e in names)


class _Manifest:
    """A present agent's real manifest with a planted display name."""

    def __init__(self, real, name: str):
        self._real, self.display = real, type("D", (), {"name": name})()

    def __getattr__(self, attr):
        return getattr(self._real, attr)


def _display_names(monkeypatch, names: dict[str, str]) -> None:
    from agent_sessions.engines import registry

    real = registry.manifest_of
    monkeypatch.setattr(
        registry,
        "manifest_of",
        lambda p: _Manifest(real(p), names[p.engine_id]) if p.engine_id in names else real(p),
    )


def test_unsafe_display_names_and_vendor_labels_never_reach_the_endpoint(
    app, ai, usage, monkeypatch
):
    """Through the transport: every new read, run by the model in a real turn, and what the AI
    endpoint was SENT holds no planted path, URL or key — segmented ones included — from a plugin's
    display name or a vendor's plan or window label (#1569 reviews 6737, 6739)."""
    e = _engines()
    planted = {
        e[0]: "/home/example/private-agent",
        e[1]: "https://evil.example/agent",
        e[2]: SEGMENTED + "Name",
    }
    _display_names(monkeypatch, planted)
    # An engine this app does not ship (an installed plugin): no authored name, so `agent N`.
    packaged = dict(ask_settings._packaged_names())
    packaged.pop(e[2], None)
    monkeypatch.setattr(ask_settings, "_packaged_names", lambda: packaged)
    monkeypatch.setattr(
        prefs,
        "get_agent_budgets",
        lambda *a, **kw: {"engines": {x: {"limit_tokens": 100} for x in e[:3]}},
    )
    usage.append(
        _row(
            e[0],
            source="plan",
            plan=SEGMENTED,
            windows=[
                {"label": "AKIAIOSFODNN7EXAMPLE", "used_pct": 5.0, "resets_at": None},
                {"label": SEGMENTED + "Win", "used_pct": 6.0, "resets_at": None},
            ],
        )
    )
    monkeypatch.setattr(pulse, "build_cards", lambda **kw: [_card(f"{x}:1", x, "idle") for x in e])
    script = ai(
        calls(("agent_usage", {}), ("list_agents", {}), ("fleet_status", {})),
        calls(*(("read_settings", {"section": s}) for s in ask_settings.SECTIONS)),
        answer("done"),
    )
    cid = app.new()
    r = app.ask(cid, "usage, agents, fleet and settings please")
    assert r.status_code == 200, r.text
    sent = json.dumps(script.bodies)
    for canary in (*planted.values(), SEGMENTED, "AKIAIOSFODNN7EXAMPLE", "/home/example", "Qrst"):
        assert canary not in sent, f"{canary} reached the AI endpoint"
    # Not vacuous: every tool's result was sent, and the authored fallback stood in.
    results = [m["content"] for m in script.bodies[-1]["messages"] if m.get("role") == "tool"]
    assert len(results) == 3 + len(ask_settings.SECTIONS)
    assert all('"refused' not in c for c in results), results
    assert re.search(r'"agent \d+"', "\n".join(results))


def test_two_agents_with_one_display_name_stay_apart(usage, monkeypatch):
    e = _engines()
    packaged = {**ask_settings._packaged_names(), e[0]: "Same Agent", e[1]: "Same Agent"}
    monkeypatch.setattr(ask_settings, "_packaged_names", lambda: packaged)
    monkeypatch.setattr(
        prefs,
        "get_agent_budgets",
        lambda *a, **kw: {"engines": {e[0]: {"limit_tokens": 100}, e[1]: {"limit_tokens": 200}}},
    )
    limits = ask_settings.read_settings("usage")["token_limits"]
    assert sorted(limits.values()) == [100, 200] and len(set(limits)) == 2

    monkeypatch.setattr(
        pulse,
        "build_cards",
        lambda **kw: [_card("a:1", e[0], "idle"), _card("b:1", e[1], "idle"),
                      _card("b:2", e[1], "idle")],
    )  # fmt: skip
    sessions = _run(ask_orchestrator._fleet_status)["sessions"]
    assert sorted(sum(v.values()) for v in sessions.values()) == [1, 2]

    usage += [_row(e[0]), _row(e[1])]
    usage_names = [r["agent"] for r in _run(ask_orchestrator._agent_usage)["agents"]]
    agent_names = [r["name"] for r in _run(ask_orchestrator._list_agents)["agents"]]
    for names in (usage_names, agent_names):
        assert len(set(names)) == len(names)
    assert {"Same Agent", "Same Agent (2)"} <= set(agent_names)


def test_the_usage_tool_never_probes(usage, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("agent_usage must never start a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    for name in ("refresh", "refresh_once"):
        if hasattr(agent_usage, name):
            monkeypatch.setattr(agent_usage, name, boom)
    usage.append(_row(_engines()[0]))
    assert _run(ask_orchestrator._agent_usage)["agents"]


def test_every_first_party_agent_fits_the_tool_result_cap(usage):
    for engine in _engines():
        usage.append(
            _row(
                engine,
                source="plan",
                plan="Plan name",
                windows=[{"label": f"w{i}", "used_pct": 50.0, "resets_at": NOW} for i in range(4)],
            )
        )
    out = json.dumps(_run(ask_orchestrator._agent_usage))
    assert len(out) < ask_orchestrator.TOOL_RESULT_MAX


# ---- agents: why not ---------------------------------------------------------------------------


def test_why_not_is_a_closed_table_and_anything_else_is_generic():
    from agent_sessions import engines, handoff

    assert "removed" in ask_orchestrator._why_not(engines.REMOVED_REASON)
    assert "not installed" in ask_orchestrator._why_not(handoff._REASON_NOT_INSTALLED)
    leak = "token sk-canary-123 rejected at https://vendor.example"
    assert ask_orchestrator._why_not(leak) == ask_orchestrator.WHY_NOT_GENERIC


def test_list_agents_keeps_its_indexes_and_adds_why_not_and_retiring():
    out = _run(ask_orchestrator._list_agents)["agents"]
    assert [a["index"] for a in out] == list(range(len(out)))
    for a in out:
        assert set(a) <= {"index", "name", "can_start", "why_not", "retiring"}
        if not a["can_start"]:
            assert a["why_not"]


# ---- settings: the allowlist -------------------------------------------------------------------

SHAPES = {
    "usage": {
        "section",
        "alert_threshold_pct",
        "alert_notify",
        "token_limits",
        "sweep_interval_minutes",
        "sweep_enabled",
    },
    "agents": {
        "section",
        "default_permission_bypass",
        "default_agent",
        "agents_offered_for_new_sessions",
    },
    "automation": {
        "section",
        "session_assistance",
        "mission_orchestration",
        "unattended_permission_bypass_granted",
    },
    "ai_review": {"section", "enabled", "endpoint_configured"},
    "pulse": {
        "section",
        "background_scan",
        "scan_interval_minutes",
        "window_days",
        "session_review_notify",
        "session_review_recognise_prompts",
    },
    "appearance": {"section", "theme", "accent", "hq_contact", "terminal_font_size"},
}


@pytest.mark.parametrize("section", ask_settings.SECTIONS)
def test_each_section_answers_exactly_its_pinned_fields(section):
    out = ask_settings.read_settings(section)
    assert set(out) <= SHAPES[section], set(out) - SHAPES[section]
    # Nothing optional is missing on defaults except fields whose value is legitimately unset.
    assert set(SHAPES[section]) - set(out) <= {"default_agent"}


def test_the_shapes_cover_every_section():
    assert set(SHAPES) == set(ask_settings.SECTIONS)


@pytest.mark.parametrize("bad", ["prefs", "ai_review.api_key", "", None, 3, "Usage"])
def test_an_unknown_section_is_refused_never_repaired(bad):
    with pytest.raises(ask_settings.UnknownSection):
        ask_settings.read_settings(bad)
    with pytest.raises(ask_orchestrator._ToolRefused):
        _run(ask_orchestrator._read_settings, {"section": bad})


CANARIES = {
    "api_key": "sk-CANARYKEY0001",
    "base_url": "https://canary-endpoint.example/v1",
    "model": "canary-model-name@corp",
    "prompt": "CANARY-PROMPT-TEXT",
    "forge_token": "CANARY-FORGE-TOKEN",
    "root": "/srv/canary-root",
    "exclusion": "/srv/canary-excluded",
    "template": "CANARY-TEMPLATE-SECRET",
    "theme": "canary-theme",
    "accent": "https://canary-accent.example",
    "engine_model": "CANARY-AGENT-MODEL",
}


def _plant_everything():
    path = prefs._default_path()
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(
        {
            "ai_review": {
                "egress_acknowledged": True,
                "enabled": True,
                "api_key": CANARIES["api_key"],
                "base_url": CANARIES["base_url"],
                "model": CANARIES["model"],
                "prompt": CANARIES["prompt"],
            },
            "forge": {
                "enabled": True,
                "token": CANARIES["forge_token"],
                "base_url": CANARIES["base_url"],
            },
            "project_roots": [CANARIES["root"]],
            "folder_exclusions": [CANARIES["exclusion"]],
            "theme": CANARIES["theme"],
            "accent": CANARIES["accent"],
            "agent_defaults": {
                "bypass": False,
                "models": {e: [CANARIES["engine_model"]] for e in _engines()},
                "session_defaults": {e: {"model": CANARIES["engine_model"]} for e in _engines()},
            },
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    template_secrets.register_typed([CANARIES["template"]])


def test_no_planted_value_reaches_any_new_tool(usage, monkeypatch):
    _plant_everything()
    usage.append(_row(_engines()[0], error=CANARIES["api_key"]))
    monkeypatch.setattr(pulse, "build_cards", lambda **kw: [])
    blobs = [
        json.dumps(_run(ask_orchestrator._agent_usage)),
        json.dumps(_run(ask_orchestrator._list_agents)),
        json.dumps(_run(ask_orchestrator._fleet_status)),
        *(json.dumps(ask_settings.read_settings(s)) for s in ask_settings.SECTIONS),
    ]
    joined = "\n".join(blobs)
    for name, canary in CANARIES.items():
        assert canary not in joined, f"{name} leaked"
    # And no path at all: nothing filesystem-shaped from the roots or exclusions.
    assert "/srv/" not in joined


def test_an_out_of_enum_theme_or_accent_is_omitted(monkeypatch):
    monkeypatch.setattr(prefs, "get_theme", lambda path=None: "neon")
    monkeypatch.setattr(prefs, "get_accent", lambda path=None: "javascript:alert(1)")
    out = ask_settings.read_settings("appearance")
    assert "theme" not in out and "accent" not in out


def test_ai_review_never_names_the_model_url_or_key():
    _plant_everything()
    out = ask_settings.read_settings("ai_review")
    assert set(out) == {"section", "enabled", "endpoint_configured"}


# ---- the fleet ---------------------------------------------------------------------------------


def _card(key: str, engine: str, state: str, cwd: str = "/home/user/claude/repo-a") -> dict:
    return {"id": key, "engine": engine, "state": state, "cwd": cwd, "project": {"kind": "folder"}}


def test_pulse_precedence_needs_you_wins_over_running():
    m = type("M", (), {"intervention_required": True})()
    assert pulse._classify(m, NOW, True, NOW) == "needs_you"
    m.intervention_required = False
    assert pulse._classify(m, NOW, True, NOW) == "in_flight"


def test_fleet_counts_each_session_once_under_its_card_state(monkeypatch):
    e = _engines()[0]
    monkeypatch.setattr(
        pulse,
        "build_cards",
        lambda **kw: [
            _card("a:1", e, "needs_you"),
            _card("a:2", e, "in_flight"),
            _card("a:3", e, "in_flight"),
            _card("a:4", e, "idle"),
        ],
    )
    out = _run(ask_orchestrator._fleet_status)
    (counts,) = out["sessions"].values()
    assert counts == {"needs_you": 1, "in_flight": 2, "idle": 1}
    assert out["complete"] is True


def test_fleet_population_is_the_strict_read_inside_the_boundary(monkeypatch, tmp_path):
    inside = tmp_path / "roots" / "proj"
    excluded = tmp_path / "roots" / "secret"
    outside = tmp_path / "elsewhere"
    for d in (inside, excluded, outside):
        d.mkdir(parents=True)
    prefs.set_project_roots([str(tmp_path / "roots")])
    prefs.set_folder_exclusions([str(excluded)])
    e = _engines()[0]
    seen: dict = {}

    def cards(**kw):
        seen.update(kw)
        return [
            _card("a:in", e, "idle", str(inside)),
            _card("a:ex", e, "idle", str(excluded)),
            _card("a:out", e, "idle", str(outside)),
        ]

    monkeypatch.setattr(pulse, "build_cards", cards)
    out = _run(ask_orchestrator._fleet_status)
    assert seen["strict"] is True and seen["window_days"] is None
    assert sum(sum(v.values()) for v in out["sessions"].values()) == 1


def test_an_incomplete_card_read_is_unknown_never_zero(monkeypatch):
    def incomplete(**kw):
        raise pulse.CardsIncomplete("metadata")

    monkeypatch.setattr(pulse, "build_cards", incomplete)
    out = _run(ask_orchestrator._fleet_status)
    assert out["sessions"] is None and out["complete"] is False


@pytest.mark.parametrize("how", ["raises", "store_error"])
def test_an_unreadable_mission_store_is_unknown_never_zero(monkeypatch, how):
    monkeypatch.setattr(pulse, "build_cards", lambda **kw: [])

    def failing(**kw):
        if how == "raises":
            raise OSError("disk")
        return {"missions": [], "total": 0, "store_error": "the mission store is locked"}

    monkeypatch.setattr(missions, "safe_list_missions", failing)
    out = _run(ask_orchestrator._fleet_status)
    assert out["missions"] is None and out["complete"] is False


def test_missions_past_the_list_cap_are_counted_from_the_total(monkeypatch):
    monkeypatch.setattr(pulse, "build_cards", lambda **kw: [])

    def listing(**kw):
        n = 45 if kw.get("state") == "running" else 0
        limit = kw.get("limit") or 40
        return {"missions": [{}] * min(n, limit), "total": n, "store_error": None}

    monkeypatch.setattr(missions, "safe_list_missions", listing)
    out = _run(ask_orchestrator._fleet_status)
    assert out["missions"]["running"] == 45


# ---- nothing writes; the turn stores metadata only -----------------------------------------------


def _mtimes() -> dict:
    paths = [prefs._default_path(), ask_store.root()]
    out = {}
    for p in paths:
        if p.exists():
            for f in p.rglob("*") if p.is_dir() else [p]:
                if f.is_file():
                    out[str(f)] = (f.stat().st_mtime_ns, f.read_bytes())
    return out


def test_none_of_the_new_tools_writes_anything(usage, monkeypatch):
    usage.append(_row(_engines()[0]))
    monkeypatch.setattr(pulse, "build_cards", lambda **kw: [])
    before = _mtimes()
    _run(ask_orchestrator._agent_usage)
    _run(ask_orchestrator._list_agents)
    _run(ask_orchestrator._fleet_status)
    for s in ask_settings.SECTIONS:
        _run(ask_orchestrator._read_settings, {"section": s})
    assert _mtimes() == before


def test_a_usage_question_settles_and_stores_no_figures(app, ai, usage, monkeypatch):
    e = _engines()[0]
    usage.append(
        _row(e, source="plan", windows=[{"label": "5h", "used_pct": 63.5, "resets_at": None}])
    )
    ai(call("agent_usage", {}, cid="u"), answer("Claude is at 63.5 % of its 5h window."))
    cid = app.new()
    r = app.ask(cid, "How is my Claude usage?")
    assert r.status_code == 200, r.text
    assert r.json()["turn"]["status"] == "done"
    tools = [rec for rec in ask_store.read(cid) if rec["type"] == "tool"]
    assert [t["name"] for t in tools] == ["agent_usage"]
    # The store keeps the tool's metadata, never the row figures it returned. Its own `ts` is a
    # wall-clock float that contains "63.5" whenever the clock does (1793029963.52…): not a figure.
    assert "63.5" not in json.dumps([{k: v for k, v in t.items() if k != "ts"} for t in tools])
    for p in (ask_store.root()).rglob("*"):
        if p.is_file() and p.suffix == ".json":
            assert "resets_in_hours" not in p.read_text()
    assert os.path.exists(ask_store.root() / f"{cid}.jsonl")
