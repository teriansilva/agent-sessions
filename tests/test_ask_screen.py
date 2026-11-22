"""HQ shows it on screen (#2102).

What is pinned here:

* **the catalog is closed and the web knows every entry** — the server's section names, target
  types and operations are the keys of the web's ONE route table
  (``web/src/app/screenDirective.ts``) and its Settings list; no path lives on the server;
* **model output picks, the server resolves** — a target this turn's tools did not return, a path,
  a URL, an unknown section or ``where: "map"`` on anything but a session is refused, and nothing
  is recorded or sent for it;
* **the directive is a request** — stored as a turn record of closed fields only (no free text),
  delivered once on the asking turn's own stream, never executed from a read;
* **it is a no-effect step with its own budget**, gated in a call's draft until the commit, and
  it reaches the call's browser as its own message;
* **no hidden state change** — ``ask_screen`` imports nothing of the app.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import ast
import asyncio
import json
import re
import uuid
from pathlib import Path

import pytest

from agent_sessions import (
    artifact_store,
    ask_orchestrator,
    ask_screen,
    ask_store,
    missions,
    voice_calls,
)
from test_ask_draft_turns import _draft, _new, _stores, held, run  # noqa: F401
from test_ask_orchestrator import (  # noqa: F401
    CRED,
    M1,
    S1,
    S2,
    ai,
    answer,
    app,
    call,
    calls,
    found,
)
from test_voice_confirm import Rig

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"
A1 = "1234-5678-1234-5678"
D0 = "sd_" + "0" * 16
FIELDS = {"type", "ts", "turn_id", "id", "op", "target", "where", "section", "key"}
KNOWN = ask_screen.Known(keys={S1}, mission_ids={M1}, artifact_ids={A1}, conversation="")


def stream(app, cid: str, text: str = "show it") -> list[dict]:
    r = app.c.post(
        f"/api/ask/conversations/{cid}/message/stream",
        json={"turn_id": str(uuid.uuid4()), "text": text},
        headers=app.hdr,
    )
    assert r.status_code == 200, r.text
    return [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]


def screens(lines: list[dict]) -> list[dict]:
    return [e for e in lines if e.get("type") == "screen"]


def records_of(cid: str, kind: str) -> list[dict]:
    return [r for r in ask_store.read(cid) if r["type"] == kind]


def results(script, i: int) -> list[dict]:
    return [json.loads(m["content"]) for m in script.bodies[i]["messages"] if m["role"] == "tool"]


# ---- the catalog, pinned across server and web --------------------------------------------------


def _web_table(name: str) -> set[str]:
    """The keys of one `export const NAME … = {` table in the web's screen module."""
    source = (WEB / "app" / "screenDirective.ts").read_text(encoding="utf-8")
    table = source[source.index(f"export const {name}") :]
    table = table[: table.index("\n};")]
    return set(re.findall(r"^  ([a-z_]+): ", table, flags=re.M))


def test_every_catalog_entry_resolves_to_a_target_the_webs_one_route_table_knows():
    """One table per side: a section, a target type or an operation the server can send and the
    web cannot route (or the reverse) fails here, like #2076's step-label table."""
    assert _web_table("SCREEN_SECTIONS") == set(ask_screen.APP_SECTIONS)
    assert _web_table("TARGET_ROUTES") == set(ask_screen.TARGET_TYPES)
    assert _web_table("SCREEN_OPS") == set(ask_screen.OPS)
    tabs = (WEB / "routes" / "settingsTabs.ts").read_text(encoding="utf-8")
    listed = tabs[tabs.index("export const SETTINGS_SECTIONS") : tabs.index("] as const satisfies")]
    pages = re.findall(r'\{ id: "([a-z-]+)", label: ', listed)
    assert pages == list(ask_screen.SETTINGS_PAGES)
    assert set(ask_screen.SECTIONS) == set(ask_screen.APP_SECTIONS) | {
        f"settings.{p}" for p in pages
    }
    assert len(set(ask_screen.SECTIONS)) == len(ask_screen.SECTIONS)


def test_projects_is_an_hq_view_not_a_settings_page():
    """#2143: Projects moved out of Settings into HQ. HQ can still put the page on screen, by its
    own section name; `settings.projects` is no longer a section, so a stale directive is
    refused."""
    assert "projects" in ask_screen.APP_SECTIONS
    assert "projects" not in ask_screen.SETTINGS_PAGES
    assert "settings.projects" not in ask_screen.SECTIONS


def test_the_server_names_sections_and_never_a_path():
    """No path strings on the server: a section is a name, an id is an id."""
    source = (ROOT / "src" / "agent_sessions" / "ask_screen.py").read_text(encoding="utf-8")
    strings = [
        n.value
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "\n" not in n.value
    ]
    assert not [s for s in strings if s.startswith("/") or "://" in s]
    assert all(re.fullmatch(r"[a-z][a-z_]*(\.[a-z][a-z-]*)?", s) for s in ask_screen.SECTIONS)


def test_the_id_shapes_are_their_owners():
    assert ask_screen.SESSION_KEY_RE.pattern == ask_store._KEY_RE.pattern
    assert ask_screen.MISSION_ID_RE.pattern == missions.MISSION_ID_RE.pattern
    assert ask_screen.ARTIFACT_ID_RE.pattern == artifact_store.ID_RE.pattern
    assert ask_screen.CONVERSATION_ID_RE.pattern == ask_store.CID_RE.pattern


def test_ask_screen_imports_nothing_of_the_app():
    """The kind has no route call at all: no `asgi_call`, and no store writer. The turn record is
    the orchestrator's, through `ask_store.append` like every record."""
    source = (ROOT / "src" / "agent_sessions" / "ask_screen.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "ask_screen must not import from the app"
            imported.add((node.module or "").split(".")[0])
    assert imported == {"__future__", "collections", "dataclasses", "re", "uuid"}
    # …and the orchestrator's handler calls no route and writes one record type.
    handler = ask_orchestrator.__dict__["_show"]
    code = Path(ask_orchestrator.__file__).read_text(encoding="utf-8")
    body = code[code.index("async def _show(") : code.index("# ---- asking back and nudging")]
    assert "asgi_call" not in body and "_stage(" not in body and handler is not None
    assert re.findall(r'"type": "([a-z_]+)"', body) == ["screen", "screen"]  # record, then event


# ---- resolution ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "directive"),
    [
        (
            {"what": "session", "key": S1},
            {"op": "open", "target": {"type": "session", "id": S1}, "where": "full"},
        ),
        (
            {"what": "session", "key": S1, "where": "map"},
            {"op": "open", "target": {"type": "session", "id": S1}, "where": "map"},
        ),
        (
            {"what": "mission", "mission_id": M1},
            {"op": "open", "target": {"type": "mission", "id": M1}, "where": "full"},
        ),
        (
            {"what": "artifact", "artifact_id": A1},
            {"op": "open", "target": {"type": "artifact", "id": A1}, "where": "full"},
        ),
        ({"what": "section", "section": "automations"}, {"op": "go", "section": "automations"}),
        (
            {"what": "section", "section": "settings.security"},
            {"op": "go", "section": "settings.security"},
        ),
        ({"what": "map", "action": "focus", "key": S1}, {"op": "map_focus", "key": S1}),
        ({"what": "map", "action": "minimize", "key": S1}, {"op": "map_minimize", "key": S1}),
        ({"what": "map", "action": "close", "key": S1}, {"op": "map_close", "key": S1}),
        ({"what": "map", "action": "close_all"}, {"op": "map_close_all"}),
        ({"what": "map", "action": "arrange"}, {"op": "map_arrange"}),
    ],
)
def test_each_catalog_member_resolves_to_its_directive(args, directive):
    resolved = ask_screen.resolve(args, KNOWN)
    assert resolved == directive
    stored = ask_screen.admit({"id": ask_screen.new_id(), **resolved})
    assert {k: v for k, v in stored.items() if k != "id"} == directive


def test_this_conversation_is_the_one_conversation_a_turn_can_open():
    cid = ask_store.new_cid()
    known = ask_screen.Known(keys=(), mission_ids=(), artifact_ids=(), conversation=cid)
    assert ask_screen.resolve({"what": "conversation"}, known) == {
        "op": "open",
        "target": {"type": "conversation", "id": cid},
        "where": "full",
    }


@pytest.mark.parametrize(
    ("args", "code"),
    [
        # A target this turn's tools did not return.
        ({"what": "session", "key": S2}, "unknown_key"),
        ({"what": "mission", "mission_id": "msn_" + "b" * 32}, "unknown_mission"),
        ({"what": "artifact", "artifact_id": "9999-9999-9999-9999"}, "unknown_artifact"),
        ({"what": "map", "action": "focus", "key": S2}, "unknown_key"),
        ({"what": "conversation"}, "unknown_conversation"),
        # A path, a URL, an unknown section.
        ({"what": "section", "section": "/settings/security"}, "unknown_section"),
        ({"what": "section", "section": "https://example.com/"}, "unknown_section"),
        ({"what": "section", "section": "settings.nope"}, "unknown_section"),
        ({"what": "section", "section": "admin"}, "unknown_section"),
        ({"what": "section"}, "unknown_section"),
        ({"what": "session", "key": S1, "path": "/s/claude/x"}, "bad_arguments"),
        ({"what": "section", "section": "map", "url": "https://example.com/"}, "bad_arguments"),
        ({"what": "url", "key": S1}, "bad_arguments"),
        ({"what": "session", "key": "/overview"}, "unknown_key"),
        # Only a session has a map window; a member takes its own arguments only.
        ({"what": "mission", "mission_id": M1, "where": "map"}, "bad_arguments"),
        ({"what": "section", "section": "map", "where": "map"}, "bad_arguments"),
        ({"what": "session", "key": S1, "where": "window"}, "bad_arguments"),
        ({"what": "session", "key": S1, "section": "map"}, "bad_arguments"),
        ({"what": "map", "action": "tile"}, "bad_arguments"),
        ({"what": "map", "action": "close_all", "key": S1}, "bad_arguments"),
        ({}, "bad_arguments"),
    ],
)
def test_anything_outside_the_catalog_or_this_turn_is_refused(args, code):
    with pytest.raises(ask_screen.Refused) as e:
        ask_screen.resolve(args, KNOWN)
    assert e.value.code == code


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "open", "target": {"type": "session", "id": S1}, "where": "map", "id": "x"},
        {"op": "navigate", "id": D0},
        {"op": "go", "section": "/settings", "id": D0},
        {"op": "open", "target": {"type": "url", "id": S1}, "where": "full", "id": D0},
        {"op": "open", "target": {"type": "mission", "id": M1}, "where": "map", "id": D0},
        {"op": "open", "target": {"type": "mission", "id": S1}, "where": "full", "id": D0},
        {"op": "map_focus", "key": "not a key", "id": D0},
    ],
)  # fmt: skip
def test_a_malformed_directive_is_never_stored(app, bad):
    cid = app.new()
    with pytest.raises(ValueError):
        ask_store.append(cid, {"type": "screen", "turn_id": str(uuid.uuid4()), **bad})
    assert records_of(cid, "screen") == []


# ---- the typed turn: record, event, view --------------------------------------------------------


def test_a_shown_session_is_recorded_and_sent_once_on_the_asking_stream(app, ai, found):
    script = ai(
        call("find_sessions", {"query": "rebase"}),
        call("show", {"what": "session", "key": S1, "where": "map"}),
        answer("On screen, Commander."),
    )
    cid = app.new()
    lines = stream(app, cid)
    (ev,) = screens(lines)
    (rec,) = records_of(cid, "screen")
    directive = {
        "id": ev["directive"]["id"],
        "op": "open",
        "target": {"type": "session", "id": S1},
        "where": "map",
    }
    assert ask_screen.DIRECTIVE_ID_RE.match(directive["id"])
    turn = lines[-1]["turn"]
    assert ev == {
        "type": "screen",
        "turn_id": turn["turn_id"],
        "directive": directive,
        "title": "api rebase",
    }
    # The record: the directive's closed fields, the turn and the time. No title, no text.
    assert {k: v for k, v in rec.items() if k != "ts"} == {
        "type": "screen",
        "turn_id": turn["turn_id"],
        **directive,
    }
    # The event comes after its record and before the step's own end.
    kinds = [e.get("step") or e["type"] for e in lines]
    assert kinds.index("screen") < len(kinds) - 1 - kinds[::-1].index("tool_done")
    # Read back, the same record is a REQUEST with the title the conversation already stores.
    assert turn["shows"] == [{**directive, "title": "api rebase"}]
    assert app.get(cid)["turns"][0]["shows"] == turn["shows"]
    # The model is told it asked, never that it opened.
    assert results(script, 2)[-1]["asked"] is True
    # A no-effect step (#2076): stored among the first 200, without the effect mark.
    step = next(s for s in turn["tools"] if s["name"] == "show")
    assert step["outcome"] == "ok" and "effect" not in step
    assert step["args"] == {"what": "session", "key": S1}  # two declared strings, guarded


def test_a_mission_an_artifact_a_section_and_the_map_each_reach_the_stream(
    app, ai, found, monkeypatch
):
    def listed(**kw):
        row = {
            "id": A1,
            "kind": "file",
            "revision": 1,
            "updated_at": 1.0,
            "origin": {"session_key": S1},
            "url": "x",
            "title": "release notes",
            "why": "",
        }
        return {"items": [row], "total": 1}

    monkeypatch.setattr(artifact_store, "list_artifacts", listed)
    ai(
        calls(("find_sessions", {"query": "docs"}), ("artifacts_find", {})),
        calls(
            ("show", {"what": "mission", "mission_id": M1}),
            ("show", {"what": "artifact", "artifact_id": A1}),
            ("show", {"what": "section", "section": "automations"}),
            ("show", {"what": "conversation"}),
            ("show", {"what": "map", "action": "arrange"}),
        ),
        answer("Done."),
    )
    cid = app.new()
    lines = stream(app, cid)
    sent = [(e["directive"], e["title"]) for e in screens(lines)]
    assert [({k: v for k, v in d.items() if k != "id"}, t) for d, t in sent] == [
        ({"op": "open", "target": {"type": "mission", "id": M1}, "where": "full"}, "docs"),
        (
            {"op": "open", "target": {"type": "artifact", "id": A1}, "where": "full"},
            "release notes",
        ),
        ({"op": "go", "section": "automations"}, ""),
        ({"op": "open", "target": {"type": "conversation", "id": cid}, "where": "full"}, ""),
        ({"op": "map_arrange"}, ""),
    ]
    assert len({d["id"] for d, _ in sent}) == 5
    shows = lines[-1]["turn"]["shows"]
    assert [s["id"] for s in shows] == [d["id"] for d, _ in sent]
    # History titles come from what the conversation stores: a mission's link, never the
    # agent's artifact title (the record keeps closed fields only).
    assert [s["title"] for s in shows] == ["docs", "", "", "", ""]


def test_a_refused_show_records_and_sends_nothing(app, ai, found):
    script = ai(
        call("find_sessions", {"query": "rebase"}),
        calls(
            ("show", {"what": "session", "key": S2}),
            ("show", {"what": "section", "section": "/settings/security"}),
            ("show", {"what": "section", "section": "https://example.com/x"}),
            ("show", {"what": "mission", "mission_id": M1, "where": "map"}),
            ("show", {"what": "session", "key": S1, "url": "https://example.com/"}),
        ),
        answer("I could not."),
    )
    cid = app.new()
    lines = stream(app, cid)
    assert screens(lines) == [] and records_of(cid, "screen") == []
    assert lines[-1]["turn"]["shows"] == []
    assert all("error" in r for r in results(script, 2)[-5:])
    codes = [s.get("error") for s in lines[-1]["turn"]["tools"] if s["name"] == "show"]
    assert codes == [
        "unknown_key",
        "unknown_section",
        "unknown_section",
        "bad_arguments",
        "bad_arguments",
    ]


def test_a_key_from_an_earlier_turn_must_be_known_to_this_one(app, ai):
    """The id rule is the turn's own: a mission id another turn found is not this turn's."""
    ai(call("show", {"what": "mission", "mission_id": M1}), answer("No."))
    cid = app.new()
    lines = stream(app, cid)
    assert screens(lines) == [] and records_of(cid, "screen") == []


def test_the_record_never_carries_free_text(app, ai, found):
    """Whatever the model or a title says, a stored directive holds an operation, a target by
    type and id, a placement, a section name — each from a closed set or an id shape."""
    ai(
        call("find_sessions", {"query": "rebase"}),
        calls(
            ("show", {"what": "session", "key": S1, "where": "map"}),
            ("show", {"what": "section", "section": "settings.security"}),
            ("show", {"what": "map", "action": "close", "key": S1}),
        ),
        answer(f"Shown. {CRED}"),
    )
    cid = app.new()
    stream(app, cid)
    stored = records_of(cid, "screen")
    assert len(stored) == 3
    for rec in stored:
        assert set(rec) <= {
            "type",
            "ts",
            "turn_id",
            "id",
            "op",
            "target",
            "where",
            "section",
            "key",
        }
        assert rec["op"] in ask_screen.OPS and ask_screen.DIRECTIVE_ID_RE.match(rec["id"])
        if "target" in rec:
            assert set(rec["target"]) == {"type", "id"}
            assert rec["target"]["type"] in ask_screen.TARGET_TYPES
            assert rec["where"] in ask_screen.WHERES
        if "section" in rec:
            assert rec["section"] in ask_screen.SECTIONS
        if "key" in rec:
            assert ask_screen.SESSION_KEY_RE.match(rec["key"])
    # A caller that hands the boundary more than the closed fields gets only those stored.
    extra = {
        "type": "screen",
        "turn_id": stored[0]["turn_id"],
        "id": ask_screen.new_id(),
        "op": "go",
        "section": "map",
        "title": "free text",
        "path": "/overview",
        "note": CRED,
    }
    (written,) = ask_store.append(cid, extra)
    assert set(written) == {"type", "ts", "turn_id", "id", "op", "section"}


# ---- the budget, Stop, the continuation ---------------------------------------------------------


def test_a_turn_shows_at_most_eight_things_and_a_refusal_gives_its_slot_back(app, ai, found):
    assert ask_screen.MAX_SHOWS_PER_TURN == 8
    assert ask_orchestrator._BUDGETS["screen"] == ("shows", 8, "things shown", "too_many_shows")
    bad = ("show", {"what": "section", "section": "nowhere"})
    good = ("show", {"what": "section", "section": "map"})
    script = ai(calls(bad, bad, bad, *([good] * 9)), answer("Done."))
    cid = app.new()
    lines = stream(app, cid)
    assert len(screens(lines)) == 8 and len(records_of(cid, "screen")) == 8
    last = results(script, 1)[-1]
    assert last == {"error": "refused: at most 8 things shown per turn"}
    steps = [s for s in lines[-1]["turn"]["tools"] if s["name"] == "show"]
    assert [s.get("error") for s in steps] == ["unknown_section"] * 3 + [None] * 8 + [
        "too_many_shows"
    ]


def test_a_stop_before_show_shows_nothing_and_one_already_sent_is_not_recalled(app, held, found):
    """Stop is honoured before `show` like before any tool; a directive already delivered stays."""
    script = held(
        call("show", {"what": "section", "section": "map"}),
        call("show", {"what": "section", "section": "dashboard"}),
        answer("never"),
        free=1,
    )
    cid = app.new()

    async def go():
        tid = str(uuid.uuid4())
        task, _ = await ask_orchestrator.begin(cid, tid, "show", working_keys=set())
        q = ask_orchestrator.listen(cid, task)
        await script.reached.wait()  # the first show ran; the model is asked again
        assert await ask_orchestrator.stop(cid, tid)
        view = await task
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        return view, seen

    view, seen = run(go())
    assert view["status"] == "stopped"
    assert [e["directive"]["section"] for e in screens(seen)] == ["map"]
    assert [r["section"] for r in records_of(cid, "screen")] == ["map"]
    assert [s["section"] for s in view["shows"]] == ["map"]


def test_the_servers_own_continuation_moves_no_screen():
    assert "screen" in ask_orchestrator.CONTINUATION_REFUSED_KINDS
    assert "screen" not in ask_orchestrator.EFFECT_KINDS  # a no-effect step
    assert "screen" not in ask_orchestrator.ABANDONED_ON_STOP  # its record and event go together
    assert "show" not in ask_orchestrator.DRAFT_READ_TOOLS


# ---- a call: the draft gate, and the call's own message -----------------------------------------


def test_a_draft_turn_shows_nothing_before_the_commit(app, held, found):
    script = held(
        call("show", {"what": "section", "section": "automations"}), answer("Here."), free=2
    )
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, task = await _draft(cid)
        q = ask_orchestrator.listen(cid, task)
        await asyncio.sleep(0.2)  # the model asked for `show`: it waits at the gate
        assert len(script.bodies) == 1 and not task.done()
        assert q.empty() and _stores() == before
        assert await ask_orchestrator.commit(cid, turn_id)
        view = await task
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        return view, seen

    view, seen = run(go())
    assert view["status"] == "done" and view["via"] == "call"
    assert [e["directive"]["section"] for e in screens(seen)] == ["automations"]
    assert [r["section"] for r in records_of(cid, "screen")] == ["automations"]


def test_a_discarded_draft_never_showed_anything(app, held, found):
    held(call("show", {"what": "section", "section": "automations"}), answer("Here."), free=1)
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, task = await _draft(cid)
        q = ask_orchestrator.listen(cid, task)
        await asyncio.sleep(0.2)
        assert await ask_orchestrator.discard(cid, turn_id)
        return q.empty()

    assert run(go())
    assert _stores() == before


def test_a_call_turns_directive_goes_to_the_calls_browser_as_its_own_message(monkeypatch):
    r = Rig(monkeypatch)
    events: asyncio.Queue = asyncio.Queue()
    release = asyncio.Event()
    directive = {"id": "sd_" + "1" * 16, "op": "go", "section": "automations"}

    async def turn_body():
        await release.wait()
        return {"turn_id": "t1", "status": "done", "answer": "Here it is."}

    async def fake_begin(cid, turn_id, text, **kw):
        return asyncio.create_task(turn_body()), None

    monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
    monkeypatch.setattr(ask_orchestrator, "listen", lambda cid, task: events)
    monkeypatch.setattr(ask_orchestrator, "unlisten", lambda cid, q: None)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Open the automations.")
        events.put_nowait({"type": "progress", "step": "tool", "tool": "show", "n": 1})
        events.put_nowait(
            {"type": "screen", "turn_id": "t1", "directive": directive, "title": "ignored"}
        )
        for _ in range(50):
            await asyncio.sleep(0.002)
        release.set()
        await r.call.turn_task

    asyncio.run(go())
    sent = [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "screen"]
    assert sent == [{"t": "screen", "turn_id": "t1", "directive": directive, "title": "ignored"}]
    # It is a message of its own: never a progress row, never spoken.
    progress = [m["step"] for m in r.to_client if isinstance(m, dict) and m.get("t") == "progress"]
    assert progress == ["show"]
    assert r.said_text() == ["Here it is."]
    assert voice_calls.Call._track(r.call, {"type": "screen"}) is None


def test_the_call_message_is_in_the_webs_message_type():
    types = (WEB / "types" / "voice.ts").read_text(encoding="utf-8")
    assert '| { t: "screen"; ' in types
