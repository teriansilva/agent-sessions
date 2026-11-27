"""#2373: the session search index — conversations indexed in full, matched with live details,
scoped like the list, in tiers, with guarded snippets."""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, lineage, metadata, session_index, transcript
from agent_sessions.main import create_app

K1, K2, K3 = (f"claude:{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}" for c in "123")
PROJ_A = "-home-user-claude-repo-a"


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


@pytest.fixture
def c(auth_cfg, fake_jsonl):
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    _login(client, auth_cfg)
    client.root = fake_jsonl
    return client


def _say(path, *lines: tuple[str, str]) -> None:
    """Append messages to a Claude JSONL: ``(role, text)``."""
    with path.open("a") as fh:
        for role, text in lines:
            if role == "user":
                rec = {"type": "user", "message": {"content": text}}
            else:
                rec = {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            fh.write(json.dumps(rec) + "\n")


def _file(c, key: str):
    uuid = key.split(":", 1)[1]
    return next(c.root.rglob(f"{uuid}.jsonl"))


def _build():
    stats = session_index.refresh_pass(list(engines.scan_all()))
    assert stats.get("failed", 0) == 0, stats
    return stats


def _q(c, q: str, **extra) -> dict:
    params = "&".join(f"{k}={v}" for k, v in {"q": q, "limit": 50, **extra}.items())
    r = c.get(f"/api/sessions?{params}")
    assert r.status_code == 200, r.text
    return r.json()


def _ids(page) -> list[str]:
    return [s["id"] for s in page["sessions"]]


# --- the index finds every word, early ones included ----------------------------------------------


def test_a_word_said_before_the_readers_tail_is_found(c, monkeypatch):
    """Every other reader keeps the newest 2,000 messages; the index reads the whole history."""
    monkeypatch.setattr(transcript, "DEFAULT_MAX_MESSAGES", 3)
    _say(_file(c, K1), ("user", "add romanian and hungarian please"))
    _say(_file(c, K1), *[("assistant", f"step {i}") for i in range(10)])
    assert "hungarian" not in " ".join(
        t.text for t in transcript.adapter_for("claude")(K1.split(":")[1], c.root.parents[1])
    )
    _build()
    page = _q(c, "hungar")
    assert _ids(page) == [K1]
    m = page["sessions"][0]["match"]
    assert m["tier"] == "conversation" and m["field"] == "you"
    assert "hungarian" in m["snippet"] and m["marks"]
    assert page["search"]["mode"] == "index"


def test_words_in_different_messages_match_session_wide(c):
    _say(
        _file(c, K1),
        ("user", "the build is flaky again"),
        ("assistant", "the deploy retried twice"),
    )
    _say(_file(c, K2), ("user", "flaky tests only"))
    _build()
    page = _q(c, "flaky%20deploy")
    assert _ids(page) == [K1]
    assert page["sessions"][0]["match"]["more"] == 1  # one word sits in another message


def test_tiers_title_then_details_then_conversation(c):
    metadata.patch(K1, title="Zebra crossing")
    metadata.patch(K2, ai_summary="Plans the zebra exhibit")
    _say(_file(c, K3), ("user", "a zebra appeared"))
    _build()
    page = _q(c, "zebra")
    assert _ids(page) == [K1, K2, K3]
    assert [s["match"]["tier"] for s in page["sessions"]] == ["title", "details", "conversation"]
    assert page["sessions"][1]["match"]["field"] == "summary"


def test_a_handoff_is_found_by_the_title_of_the_session_it_continued(c):
    metadata.patch(K1, title="Research party games")
    path = _file(c, K2)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": "# Handoff — continued from a claude session\n\n"
                    f"## Source\n- session: {K1}\n"
                },
            }
        )
        + "\n"
    )
    _say(path, ("user", "Add romanian, hungarian, polish as languages"))
    _build()
    assert K2 in _ids(_q(c, "party%20games"))
    hit = next(s for s in _q(c, "party%20games")["sessions"] if s["id"] == K2)
    assert hit["match"]["field"] == "handoff"
    assert K2 in _ids(_q(c, "polish"))  # and by its own later messages


def test_short_words_match_titles_and_details_only(c):
    metadata.patch(K1, title="Fix AI routing")
    _say(_file(c, K2), ("user", "ai everywhere"))
    _build()
    page = _q(c, "ai")
    assert _ids(page) == [K1] and page["search"]["short"] is True


def test_a_credential_shaped_message_matches_but_its_snippet_is_withheld(c):
    _say(
        _file(c, K1),
        ("user", "rotate zanzibar token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
    )
    _build()
    page = _q(c, "zanzibar")
    assert _ids(page) == [K1]
    m = page["sessions"][0]["match"]
    assert m.get("withheld") is True and "snippet" not in m


# --- scope: only scoped keys can match ------------------------------------------------------------


def test_an_in_scope_child_lifts_its_main_and_counts_stay_main_only(c):
    lineage.link(K2, K1, "launch")
    _say(_file(c, K2), ("user", "the subagent found quokkas"))
    _build()
    page = _q(c, "quokkas")
    assert _ids(page) == [K1] and page["total"] == 1
    m = page["sessions"][0]["match"]
    assert m["field"] == "subagent" and m["child"]


def test_an_out_of_scope_child_never_makes_its_main_match(c):
    """The inverse of the filtered-parent rule: a visible main is never matched, named or
    snippeted through a child the list does not show (here: archived while `archived=0`)."""
    lineage.link(K2, K1, "launch")
    _say(_file(c, K2), ("user", "secret quokkas plan"))
    metadata.patch(K2, archived=True)
    _build()
    page = _q(c, "quokkas")
    assert page["total"] == 0 and page["sessions"] == []


# --- too common words: they still constrain, or the result says it is degraded --------------------


def test_a_too_common_word_still_constrains_a_literal_hit(c, monkeypatch):
    monkeypatch.setattr(session_index, "SEARCH_TERM_KEYS_MAX", 2)
    k5 = "claude:55555555-5555-5555-5555-555555555555"
    _say(_file(c, K1), ("user", "common words and a narwhal"))
    _say(_file(c, K2), ("user", "common words only"))
    _say(_file(c, k5), ("user", "common words again"))
    _say(_file(c, K3), ("user", "a narwhal without the other word"))
    _build()
    page = _q(c, "common%20narwhal")
    # "common" is in 3 sessions (> 2): too common to list, and it still had to match.
    assert _ids(page) == [K1] and page["search"]["mode"] == "index"


def test_every_word_too_common_is_an_explicit_degraded_result(c, monkeypatch):
    monkeypatch.setattr(session_index, "SEARCH_TERM_KEYS_MAX", 1)
    _say(_file(c, K1), ("user", "common words"))
    _say(_file(c, K2), ("user", "common words"))
    _build()
    page = _q(c, "common")
    assert page["search"] == {"mode": "titles", "degraded": "common"}


def test_the_term_key_cap_boundary(c, monkeypatch):
    monkeypatch.setattr(session_index, "SEARCH_TERM_KEYS_MAX", 2)
    _say(_file(c, K1), ("user", "walrus"))
    _say(_file(c, K2), ("user", "walrus"))
    _build()
    page = _q(c, "walrus")  # exactly at the cap: still listed, not degraded
    assert sorted(_ids(page)) == sorted([K1, K2]) and "degraded" not in page["search"]


# --- freshness ------------------------------------------------------------------------------------


def test_an_unreadable_read_keeps_the_old_rows_and_retries_later(c, monkeypatch):
    _say(_file(c, K1), ("user", "pangolin sighting"))
    _build()
    _say(_file(c, K1), ("user", "more words"))  # the fingerprint changes

    def broken(*_a, **_k):
        raise OSError("locked")

    monkeypatch.setattr(transcript, "full_turns", broken)
    stats = session_index.refresh_pass(list(engines.scan_all()))
    assert stats["failed"] >= 1
    assert _ids(_q(c, "pangolin")) == [K1]  # the old rows are still there


def test_a_session_gone_from_the_scan_is_dropped(c):
    _say(_file(c, K1), ("user", "ibex trail"))
    _build()
    rest = [s for s in engines.scan_all() if engines.session_key(s) != K1]
    stats = session_index.refresh_pass(rest)
    assert stats["removed"] == 1
    con = session_index._open()
    try:
        assert con.execute("SELECT count(*) FROM msg WHERE key=?", (K1,)).fetchone()[0] == 0
    finally:
        con.close()


def test_the_index_file_is_private_and_a_corrupt_one_is_rebuilt(c):
    _build()
    path = session_index.index_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    path.write_bytes(b"not a database")
    _build()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_before_anything_is_indexed_search_falls_back_and_says_so(c):
    page = _q(c, "second")
    assert _ids(page)  # the first message still matches, as today
    assert page["search"]["mode"] == "titles"


# --- HQ's fallback candidates ---------------------------------------------------------------------


def test_candidates_rank_by_distinct_words_and_add_no_filler(c):
    _say(_file(c, K1), ("user", "okapi and tapir"))
    _say(_file(c, K2), ("user", "okapi alone"))
    _build()
    rows = [r for r in c.get("/api/sessions?limit=50").json()["sessions"]]
    got = session_index.candidates("okapi tapir llama", rows)
    assert [r["id"] for r in got] == [K1, K2]


# --- HQ's find_sessions: the index first ---------------------------------------------------------


def _hq_turn(c, auth_cfg):
    import uuid

    from agent_sessions import asgi_call, ask_orchestrator

    csrf = c.get("/api/config").json()["csrf"]
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    op = asgi_call.Operator.from_scope(
        {
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 5000),
            "headers": [
                (b"cookie", cookie.encode()),
                (b"x-csrf-token", csrf.encode()),
                (b"origin", auth_cfg.origin.encode()),
                (b"host", b"testserver"),
            ],
        }
    )
    return ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id=str(uuid.uuid4()),
        question="q",
        working_keys=set(),
        progress=lambda ev: None,
        operator=op,
        app=c.app,
    )


def _no_model(monkeypatch):
    from agent_sessions import pulse_chat

    async def boom(*_a, **_k):
        raise AssertionError("a literal search must not call a model")

    monkeypatch.setattr(pulse_chat, "ask", boom)


def test_hq_finds_a_literal_hit_with_no_model_call(c, auth_cfg, monkeypatch):
    import asyncio

    from agent_sessions import ask_orchestrator

    _say(_file(c, K2), ("user", "add romanian, hungarian, polish as languages"))
    _build()
    _no_model(monkeypatch)
    turn = _hq_turn(c, auth_cfg)
    out, meta = asyncio.run(ask_orchestrator._find_sessions(turn, {"query": "hungarian polish"}))
    assert [s["key"] for s in out["sessions"]] == [K2]
    assert "in your words" in out["sessions"][0]["why"]
    assert meta["source"] == "index" and K2 in turn.keys


def test_hq_answers_a_query_no_session_holds_without_a_model(c, auth_cfg, monkeypatch):
    import asyncio

    from agent_sessions import ask_orchestrator

    _build()
    _no_model(monkeypatch)
    out, meta = asyncio.run(
        ask_orchestrator._find_sessions(_hq_turn(c, auth_cfg), {"query": "xylophone zeppelin"})
    )
    assert out["sessions"] == [] and out["note"] and meta["source"] == "index"


def test_hq_without_a_literal_hit_reranks_only_the_index_candidates(c, auth_cfg, monkeypatch):
    import asyncio

    from agent_sessions import ask_orchestrator, pulse_chat

    _say(_file(c, K1), ("user", "the flaky deploy on staging"))
    _say(_file(c, K2), ("user", "flaky tests"))
    _build()
    seen = {}

    async def fake_ask(query, *a, candidates=None, **k):
        seen["candidates"] = candidates
        return {"answer": "the staging one", "matches": [], "mission_matches": []}

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    out, meta = asyncio.run(
        ask_orchestrator._find_sessions(_hq_turn(c, auth_cfg), {"query": "flaky deploy last week"})
    )
    assert seen["candidates"][:2] == [K1, K2]  # most distinct words first, never filler
    assert meta["source"] == "index+model"


def test_a_word_in_too_many_messages_is_too_common_without_listing(c, monkeypatch):
    monkeypatch.setattr(session_index, "ROWS_COMMON_MAX", 1)
    _say(_file(c, K1), ("user", "lemur"), ("user", "lemur again"))
    _build()
    page = _q(c, "lemur")
    assert page["search"] == {"mode": "titles", "degraded": "common"}


def test_checking_a_common_word_past_its_budget_is_degraded_not_partial(c, monkeypatch):
    monkeypatch.setattr(session_index, "SEARCH_TERM_KEYS_MAX", 1)
    monkeypatch.setattr(session_index, "WITHIN_KEYS_MAX", 0)
    _say(_file(c, K1), ("user", "common words and a narwhal"))
    _say(_file(c, K2), ("user", "common words only"))
    _build()
    page = _q(c, "common%20narwhal")
    # "narwhal" is listed (1 session); "common" (2 sessions) would have to be checked within it,
    # but no session may be checked: the answer says so instead of ignoring the word.
    assert page["search"]["degraded"] == "common" and page["search"]["mode"] == "titles"


def test_a_pass_runs_in_its_own_process_and_the_app_reads_its_rows(c):
    """`run` starts each pass as `python -I -m agent_sessions.session_index`: the child parses on
    its own interpreter and writes the index the app then reads (WAL)."""
    import subprocess
    import sys

    _say(_file(c, K1), ("user", "the aardvark plan"))
    done = subprocess.run(
        [sys.executable, "-I", "-m", "agent_sessions.session_index"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    stats = json.loads(done.stdout)
    assert stats["indexed"] >= 1 and stats["failed"] == 0
    assert _ids(_q(c, "aardvark")) == [K1]


def test_a_session_that_never_had_messages_is_indexed_as_empty_not_failed(c, monkeypatch):
    """An empty read only counts as unreadable when the session HAD rows; a resume stub or an API
    session never used is indexed as empty, so the build can complete."""
    monkeypatch.setattr(transcript, "full_turns", lambda *a, **k: [])
    stats = session_index.refresh_pass(list(engines.scan_all()))
    assert stats["failed"] == 0 and stats["indexed"] >= 1
    keys = {engines.session_key(s) for s in engines.scan_all()}
    assert session_index.progress(keys).complete


def test_an_empty_read_of_a_session_that_had_rows_keeps_them(c, monkeypatch):
    _say(_file(c, K1), ("user", "okapi tracks"))
    _build()
    _say(_file(c, K1), ("user", "more"))
    monkeypatch.setattr(transcript, "full_turns", lambda *a, **k: [])
    stats = session_index.refresh_pass(list(engines.scan_all()))
    assert stats["failed"] >= 1
    assert _ids(_q(c, "okapi")) == [K1]


# --- Hermes on PR #2394: whole messages, honest completeness, one snapshot ------------------------

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def test_a_long_message_is_indexed_whole_and_its_snippet_guarded_whole(c, monkeypatch):
    """Nothing of a long message is dropped (a word past 64k is found), and the snippet guard
    sees the WHOLE message: a credential straddling a part boundary withholds the snippet."""
    monkeypatch.setattr(session_index, "CHUNK_CHARS", 200)
    head = "filler " * 9_140  # ~64k characters: the old per-message cut fell right here
    text = head + "pangolin " + SECRET + " then much later the okapi arrived"
    _say(_file(c, K1), ("user", text))
    _build()
    assert _ids(_q(c, "okapi")) == [K1]  # a word past the old cut is found
    m = _q(c, "pangolin")["sessions"][0]["match"]
    assert m.get("withheld") is True and "snippet" not in m  # guarded whole, never a prefix
    con = session_index._open()
    try:
        rows = con.execute(
            "SELECT text, part, parts FROM msg WHERE key=? ORDER BY rowid", (K1,)
        ).fetchall()
        mine = [r for r in rows if r[2] > 1]  # the long message's parts (the fixture has others)
        parts = len(mine)
        joined = "".join(r[0] for r in mine)
        assert [r[1] for r in mine] == list(range(parts)) and mine[0][2] == parts
    finally:
        con.close()
    assert parts > 300 and joined == text  # split into parts, nothing lost or doubled


def test_a_message_too_long_to_guard_is_withheld_not_cut(c, monkeypatch):
    monkeypatch.setattr(session_index, "SNIPPET_MESSAGE_CHARS_MAX", 1_000)
    _say(_file(c, K1), ("user", "the quokka " + "word " * 400))
    _build()
    page = _q(c, "quokka")
    assert _ids(page) == [K1]
    assert page["sessions"][0]["match"].get("withheld") is True


def _traced(monkeypatch) -> list[str]:
    """Every SQL statement the index runs in this process from here on, casefolded."""
    seen: list[str] = []
    real = session_index._open

    def traced(path=None):
        con = real(path)
        con.set_trace_callback(lambda sql: seen.append(sql.casefold()))
        return con

    monkeypatch.setattr(session_index, "_open", traced)
    return seen


def test_a_page_asks_bm25_once_and_reads_only_the_chosen_messages(c, monkeypatch):
    """HQ's `find_sessions` paid seconds here: every shown row ran its own `ORDER BY bm25()` —
    and every such statement first counts each word over the WHOLE index — then read the text of
    every message it matched; each word was also counted (`count(*)`) before being listed again.
    A page now asks bm25 once (only where messages tie), lists each word in one capped pass and
    reads the text of each chosen message alone. The answer is unchanged."""
    for key in (K1, K2, K3):
        _say(
            _file(c, key),
            ("user", "the capybara met a wombat"),
            ("assistant", "a capybara and a wombat, again"),
            ("user", "only a capybara here"),
        )
    _build()
    seen = _traced(monkeypatch)
    page = _q(c, "capybara%20wombat")
    assert sorted(_ids(page)) == sorted([K1, K2, K3])
    for s in page["sessions"]:
        m = s["match"]
        assert m["tier"] == "conversation" and m["field"] in ("you", "agent")
        assert m.get("more", 0) == 0 and "wombat" in m["snippet"]  # a message holding both
    assert sum("bm25(" in sql for sql in seen) == 1
    assert not any("count(*)" in sql for sql in seen)
    assert sum(sql.lstrip().startswith("select text from msg") for sql in seen) == 3
    # Every read that may return many rows returns ONE aggregated row: Python's sqlite3 retakes
    # the GIL per row, and with the app's other threads busy that alone cost seconds.
    many = [
        sql
        for sql in seen
        if ("msg match" in sql or "from sess" in sql or "from span" in sql)
        and "where key in" not in sql
    ]
    assert many and all(
        "group_concat(" in sql or "limit 1" in sql or "select min(s)" in sql for sql in many
    ), many


@pytest.mark.parametrize(("rows_max", "common"), [(2, False), (1, True)])
def test_the_common_word_row_cap_boundary(c, monkeypatch, rows_max, common):
    """The capped listing keeps the row count's rule: a word in exactly `ROWS_COMMON_MAX`
    messages is listed, one more makes it too common."""
    monkeypatch.setattr(session_index, "ROWS_COMMON_MAX", rows_max)
    _say(_file(c, K1), ("user", "lemur"), ("user", "lemur again"))
    _build()
    page = _q(c, "lemur")
    if common:
        assert page["search"] == {"mode": "titles", "degraded": "common"}
    else:
        assert _ids(page) == [K1] and page["search"]["mode"] == "index"


def test_snippets_are_the_same_sought_per_session_or_listed_once(c, monkeypatch):
    """Past `SNIPPET_SEEK_KEYS_MAX` shown sessions a word's rows are listed once and mapped
    through the spans instead of sought per session: the snippets are the same."""
    _say(_file(c, K1), ("user", "an okapi alone"), ("assistant", "the okapi met a zebu"))
    _say(_file(c, K2), ("user", "zebu and okapi"))
    _say(_file(c, K3), ("user", "neither of them"))  # rows between the two spans
    _build()
    sought = _q(c, "okapi%20zebu")["sessions"]
    monkeypatch.setattr(session_index, "SNIPPET_SEEK_KEYS_MAX", 0)
    listed = _q(c, "okapi%20zebu")["sessions"]
    assert sorted(s["id"] for s in sought) == sorted([K1, K2])
    assert [(s["id"], s["match"]) for s in listed] == [(s["id"], s["match"]) for s in sought]
    by_id = {s["id"]: s["match"] for s in sought}
    assert by_id[K1]["field"] == "agent" and by_id[K1].get("more", 0) == 0
    assert by_id[K2]["field"] == "you"


def _unreadable(monkeypatch, key):
    real = transcript.full_turns

    def flaky(engine, native, home):
        if f"{engine}:{native}" == key:
            raise OSError("locked")
        return real(engine, native, home)

    monkeypatch.setattr(transcript, "full_turns", flaky)


def test_an_unreadable_session_settles_the_build_but_leaves_it_incomplete(c, monkeypatch):
    _say(_file(c, K1), ("user", "pangolin sighting"))
    _unreadable(monkeypatch, K2)
    session_index.refresh_pass(list(engines.scan_all()))
    page = _q(c, "pangolin")
    assert _ids(page) == [K1]  # a hit is still an answer
    assert "indexing" not in page["search"]  # every session was tried: not "still indexing"
    assert page["search"]["incomplete"] == 1
    prog = session_index.progress({K1, K2})
    assert prog.settled and not prog.complete and prog.failed == 1


def test_hq_never_concludes_no_match_while_a_conversation_is_unreadable(c, auth_cfg, monkeypatch):
    """The unreadable session might be the one: the model path runs, unrestricted."""
    import asyncio

    from agent_sessions import ask_orchestrator, pulse_chat

    _unreadable(monkeypatch, K2)
    session_index.refresh_pass(list(engines.scan_all()))
    seen = {}

    async def fake_ask(query, *a, **k):
        seen["kwargs"] = k
        return {"answer": "", "matches": [], "mission_matches": []}

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    out, meta = asyncio.run(
        ask_orchestrator._find_sessions(_hq_turn(c, auth_cfg), {"query": "xylophone zeppelin"})
    )
    assert "kwargs" in seen and "candidates" not in seen["kwargs"]
    assert meta["source"] == "model" and "note" not in out


def test_a_pass_replacing_a_session_mid_search_never_drops_its_hit(c, monkeypatch):
    """The spans and every word's lookup are one read snapshot: a session re-indexed (new rowids)
    between them is seen whole as before, never mapped through a stale span and lost."""
    _say(_file(c, K1), ("user", "the narwhal migration"))
    _build()
    real = session_index._spans
    fired = []

    def spans_then_replace(con):
        out = real(con)
        if not fired:
            fired.append(1)
            _say(_file(c, K1), ("user", "more words"))  # new fingerprint: rows replaced
            session_index.refresh_pass(list(engines.scan_all()))
        return out

    monkeypatch.setattr(session_index, "_spans", spans_then_replace)
    res = session_index.search("narwhal", [{"id": K1, "title": "t"}])
    assert fired and K1 in res.matches


# --- Hermes on PR #2394 (2nd): coverage after a failed refresh, and fail-soft first reads ---------


def _hq_absence_runs_the_model(c, auth_cfg, monkeypatch) -> dict:
    import asyncio

    from agent_sessions import ask_orchestrator, pulse_chat

    seen: dict = {}

    async def fake_ask(query, *a, **k):
        seen["kwargs"] = k
        return {"answer": "", "matches": [], "mission_matches": []}

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    out, meta = asyncio.run(
        ask_orchestrator._find_sessions(_hq_turn(c, auth_cfg), {"query": "xylophone zeppelin"})
    )
    assert "kwargs" in seen and "candidates" not in seen["kwargs"], "HQ concluded no match"
    assert meta["source"] == "model" and "note" not in out
    return out


def test_a_failed_refresh_of_an_indexed_session_leaves_the_index_incomplete(
    c, auth_cfg, monkeypatch
):
    """Indexed, then changed, then unreadable: the old rows stay searchable, but the newest
    conversation is not in them, so coverage is incomplete and HQ never concludes "none"."""
    _say(_file(c, K1), ("user", "pangolin sighting"))
    _build()
    assert session_index.progress({K1}).complete
    _say(_file(c, K1), ("user", "the xylophone zeppelin"))  # added since; never indexed
    _unreadable(monkeypatch, K1)
    session_index.refresh_pass(list(engines.scan_all()))
    assert _ids(_q(c, "pangolin")) == [K1]  # old rows kept
    prog = session_index.progress({K1})
    assert prog.failed == 1 and not prog.complete
    assert _q(c, "xylophone")["search"]["incomplete"] >= 1
    _hq_absence_runs_the_model(c, auth_cfg, monkeypatch)


def test_an_unreadable_first_read_is_failed_never_indexed_as_empty(c, auth_cfg, monkeypatch):
    """The file readers are fail-soft (``[]`` on OSError) for every other caller; the index's full
    read raises instead, so a non-empty transcript it cannot open is a failure, not "empty"."""
    path = _file(c, K2)
    _say(path, ("user", "the xylophone zeppelin"))
    os.chmod(path, 0)
    try:
        session_index.refresh_pass(list(engines.scan_all()))
        prog = session_index.progress({K2})
        assert prog.failed == 1 and prog.indexed == 0
        _hq_absence_runs_the_model(c, auth_cfg, monkeypatch)
    finally:
        os.chmod(path, 0o600)


def test_a_claude_transcript_past_the_tail_bytes_is_read_whole(c, monkeypatch):
    """The Claude reader keeps the newest `_TAIL_BYTES` for every other caller; the index's read
    lifts it, so an early word is found and the session is not reported as cut."""
    monkeypatch.setattr(transcript, "_TAIL_BYTES", 400)
    _say(_file(c, K1), ("user", "the axolotl came first"))
    _say(_file(c, K1), *[("assistant", f"a long reply number {i} " * 5) for i in range(20)])
    _build()
    assert _ids(_q(c, "axolotl")) == [K1]
    assert session_index.progress({K1}).complete


# --- Hermes on PR #2394 (3rd): native API journals and the chat store are strict in a full read ---


def _full_read(fn, *a):
    token = transcript._FULL_READ.set(True)
    try:
        return fn(*a)
    finally:
        transcript._FULL_READ.reset(token)


def test_a_corrupt_native_journal_raises_in_a_full_read_and_stays_soft_elsewhere(tmp_path):
    """An API session's journal that is there but cannot be folded is unreadable, never empty:
    the index's read raises (→ failed → incomplete → HQ's unrestricted fallback, pinned above);
    every other reader still gets ``[]``. An absent journal is simply empty."""
    import test_native_journal as journal_tests
    from agent_sessions import chat_store, native_journal

    root, sid, _binding = journal_tests.conversation.__wrapped__(tmp_path)
    assert _full_read(transcript._native_turns, root, sid) == []  # readable, no messages yet
    chat_store._path(root, sid).write_bytes(b"\x00not a journal\n" * 8)
    with pytest.raises(native_journal.JournalError):
        native_journal.read(root, sid)
    with pytest.raises(native_journal.JournalError):
        _full_read(transcript._native_turns, root, sid)
    assert transcript._native_turns(root, sid) == []  # ordinary callers: fail-soft as before
    absent = "00000000-0000-4000-8000-0000000000aa"
    assert _full_read(transcript._native_turns, root, absent) == []


def test_an_unreadable_chat_log_raises_in_a_full_read_and_stays_soft_elsewhere(
    tmp_path, monkeypatch
):
    from agent_sessions import chat_store
    from agent_sessions.engines import base

    root = tmp_path / "chat"
    root.mkdir()
    monkeypatch.setattr(base, "_store", lambda *_a, **_k: root)
    sid = "00000000-0000-4000-8000-0000000000bb"
    assert _full_read(transcript._chat_adapter, sid, tmp_path) == []  # absent: empty
    chat_store._path(root, sid).write_bytes(b"garbage without a header\n")
    assert chat_store.read(root, sid) is None
    with pytest.raises(OSError):
        _full_read(transcript._chat_adapter, sid, tmp_path)
    assert transcript._chat_adapter(sid, tmp_path) == []
