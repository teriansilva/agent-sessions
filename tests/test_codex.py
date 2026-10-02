"""CodexProvider discovery + launch argv, and the decoupled launch_argv contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_sessions.engines.codex as cdx
from agent_sessions import engines, metadata


def _write_rollout(root, *, uuid, cwd, first_user, day="2026/05/15", ts="2026-05-15T15-33-57"):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{ts}-{uuid}.jsonl"
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": cwd}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": first_user}],
            },
        },
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.fixture
def codex_root(tmp_path, monkeypatch):
    root = tmp_path / "codex-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    return root


def test_codex_scan_discovers_sessions(codex_root):
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    _write_rollout(codex_root, uuid=uuid, cwd="/home/u/proj", first_user="why is X broken?")
    prov = engines.CodexProvider()
    assert prov.is_present() is True
    sessions = prov.scan()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.engine == "codex"
    assert s.uuid == uuid
    assert s.cwd == "/home/u/proj"
    assert s.first_user_message == "why is X broken?"
    assert s.archived is False


def test_codex_scan_failsoft_on_garbage(codex_root):
    # a non-rollout file + a corrupt rollout must not break the scan AND must not
    # emit a bogus empty-cwd session (Hermes PR #50 review): no usable cwd -> no row.
    d = codex_root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("ignore me")
    bad = d / "rollout-x-019e2ba1-1590-7003-8e4a-51ab62cec96e.jsonl"
    bad.write_text("{not json\n")  # parse errors only -> no cwd -> skipped, no crash
    # a valid sibling still scans fine
    _write_rollout(
        codex_root,
        uuid="019e2ba1-1590-7003-8e4a-51ab62cec999",
        cwd="/home/u/ok",
        first_user="hi",
        ts="2026-05-15T16-00-00",
    )
    sessions = engines.CodexProvider().scan()
    assert [s.uuid for s in sessions] == ["019e2ba1-1590-7003-8e4a-51ab62cec999"]
    assert all(s.cwd for s in sessions)  # never an empty-cwd row


def test_codex_launch_argv(engine_bin):
    # #853 P2: argv from the manifest-built provider; argv[0] = AGENT_SESSIONS_CODEX_BIN's file.
    b = engine_bin("codex")
    prov = engines.get("codex")
    argv = prov.launch_argv("019e2ba1-1590-7003-8e4a-51ab62cec96e", cwd="/x", bypass=True)
    assert argv == [b, "resume", "019e2ba1-1590-7003-8e4a-51ab62cec96e"]


def test_parse_key_routes_codex():
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    prov, native = engines.parse_key(f"codex:{uuid}")
    assert prov.engine_id == "codex"
    assert native == uuid
    with pytest.raises(engines.EngineError):
        engines.parse_key("codex:not-a-uuid")


def test_launch_argv_contract_all_present_providers(engine_bin):
    # every provider exposes launch_argv returning a non-empty argv list of plain strings
    engine_bin()
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    native = {"opencode": "ses_abcd1234", "kimi": f"session_{uuid}"}
    for prov in engines.all_providers():
        if prov.manifest.runtime != "pty":
            continue  # a `chat` engine (#1209) launches nothing; its refusal is pinned elsewhere
        argv = prov.launch_argv(native.get(prov.engine_id, uuid), cwd="/tmp/x", bypass=False)
        assert isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)


def test_claude_launch_argv_bypass_flag(engine_bin):
    b = engine_bin("claude")
    prov = engines.get("claude")
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    assert prov.launch_argv(uuid, cwd="/x", bypass=False) == [
        b,
        "--resume",
        uuid,
    ]
    assert "--dangerously-skip-permissions" in prov.launch_argv(uuid, cwd="/x", bypass=True)


# --- new-session (launch-then-reconcile, #315) ---------------------------------------------

_U1 = "019e2ba1-1590-7003-8e4a-51ab62cec001"
_U2 = "019e2ba1-1590-7003-8e4a-51ab62cec002"


def test_codex_supports_new_and_reconciles():
    prov = engines.get("codex")
    assert prov.supports_new is True
    assert getattr(prov, "new_session_reconciles", False) is True


def test_codex_new_launch_argv_cd_and_bypass(engine_bin):
    b = engine_bin("codex")
    prov = engines.get("codex")
    assert prov.new_launch_argv(f"new-{_U1}", cwd="/work", bypass=False) == [
        b,
        "--cd",
        "/work",
    ]
    argv = prov.new_launch_argv(f"new-{_U1}", cwd="/work", bypass=True)
    assert argv[:3] == [b, "--cd", "/work"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_codex_snapshot_empty_when_dir_missing(codex_root):
    # env points at a not-yet-created dir → valid EMPTY baseline (set()), NOT a None failure.
    assert engines.CodexProvider().snapshot_session_ids("/work") == set()


def test_codex_reconcile_single_new(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")  # empty
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    assert prov.reconcile_new_session("/work", snap) == _U1


def test_codex_reconcile_none_when_nothing_new(codex_root):
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    assert snap == {_U1}
    assert prov.reconcile_new_session("/work", snap) is None  # no NEW id


def test_codex_reconcile_ambiguous_same_cwd(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="a", ts="2026-05-15T15-00-00")
    _write_rollout(codex_root, uuid=_U2, cwd="/work", first_user="b", ts="2026-05-15T16-00-00")
    result = prov.reconcile_new_session("/work", snap)
    assert isinstance(result, list) and set(result) == {_U1, _U2}  # caller fails safe


def test_codex_reconcile_ignores_other_cwd(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    _write_rollout(codex_root, uuid=_U1, cwd="/elsewhere", first_user="x")
    assert prov.reconcile_new_session("/work", snap) is None  # different cwd → not ours


def test_codex_reconcile_pending_on_malformed_head(codex_root):
    # a new rollout whose cwd head isn't written/parseable yet → excluded → stays pending
    # (None), never misattributed (Hermes #315).
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    d = codex_root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / f"rollout-2026-05-15T15-00-00-{_U1}.jsonl").write_text("{not json yet\n")  # no cwd
    assert prov.reconcile_new_session("/work", snap) is None


def test_codex_snapshot_none_on_walk_failure(codex_root, monkeypatch):
    import agent_sessions.engines.codex as cdx

    class _BadDir:
        def exists(self):
            return True

        def rglob(self, _pat):
            raise OSError("walk failed")

    monkeypatch.setattr(cdx.base, "_codex_sessions_dir", lambda *a, **k: _BadDir())
    # a read failure must be None (skip reconcile), never an empty set (would misattribute).
    assert engines.CodexProvider().snapshot_session_ids("/work") is None


def test_codex_new_placeholder_recognized_and_parses(codex_root):
    key = f"codex:new-{_U1}"
    assert engines.is_new_session_placeholder(key) is True
    assert engines.is_opencode_new_placeholder(key) is True  # back-compat alias is generic now
    # accepted ONLY on the new=1 launch path
    prov, native = engines.parse_key(key, allow_new_placeholder=True)
    assert prov.engine_id == "codex" and native.startswith("new-")
    with pytest.raises(engines.EngineError):
        engines.parse_key(key)  # NOT accepted on resume/attach


def test_codex_present_gates_new_session_advertisement(codex_root, no_engine_bin):
    # no_engine_bin: no AGENT_SESSIONS_CODEX_BIN and nothing under $HOME's search_paths, so no
    # codex entrypoint resolves (#853 §2b — PATH is never consulted).
    prov = engines.get("codex")
    assert prov.is_present() is False  # absent store + no bin → not advertised
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    assert prov.is_present() is True and prov.supports_new is True  # present → advertised


# --- first-user-message extraction (#670) ---------------------------------------------------

_AGENTS_MD = (
    "# AGENTS.md instructions for /home/u/proj\n\n<INSTRUCTIONS>\nWorkspace map — injected doc "
    "text the sidebar must never show.\n</INSTRUCTIONS>"
)


def _write_records(root, uuid, records, ts="2026-05-15T15-33-57"):
    d = root / "2026" / "05" / "15"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{ts}-{uuid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in records) + "\n")
    return f


def _session_meta(uuid, cwd):
    return {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": cwd}}


def _user_item(text):
    return {
        "timestamp": "t",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _user_event(message):
    return {
        "timestamp": "t",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": message},
    }


def _scan_one(uuid):
    return next(s for s in engines.CodexProvider().scan() if s.uuid == uuid)


def test_codex_first_user_event_beats_earlier_fallback_candidate(codex_root):
    # An injected AGENTS.md item AND an otherwise-valid response-item candidate both precede
    # the user_message event — the event is authoritative; the candidate is EOF-fallback only,
    # so it must not trip the early break before the event record is reached.
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_AGENTS_MD),
            _user_item("a plausible but non-authoritative candidate"),
            _user_event("the real prompt"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "the real prompt"


def test_codex_first_user_fallback_when_no_event(codex_root):
    # Old / truncated rollout with no user_message event: the first NON-injected user
    # response_item is the title.
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_AGENTS_MD),
            _user_item("real prompt from response item"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "real prompt from response item"


def test_codex_first_user_skips_every_known_injection_marker(codex_root):
    # All marker forms — old XML preambles and the ≥0.142.5 AGENTS.md block — are machine
    # context. An injected-only rollout yields "" (→ "(untitled)"), never the boilerplate.
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
            _user_item("<user_instructions>\ndo what AGENTS.md says\n</user_instructions>"),
            _user_item(_AGENTS_MD),
        ],
    )
    assert _scan_one(_U1).first_user_message == ""


def test_codex_first_user_kept_raw_and_title_normalized_at_display(codex_root):
    # The stored first_user_message stays RAW — it is the /api/sessions search haystack
    # (Hermes on PR #672); only the DISPLAY title is one bounded line via display_title.
    long_first_line = "fix the thing " + "x" * 200
    raw = long_first_line + "\nsearchable-second-line-term"
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_event(raw),
        ],
    )
    stored = _scan_one(_U1).first_user_message
    assert stored == raw  # full raw text, both lines — search can hit line 2
    title = metadata.display_title(metadata.SessionMeta(), stored)
    assert title == long_first_line[:120]
    assert "\n" not in title


def test_codex_first_user_failsoft_on_malformed_payloads(codex_root):
    # Non-string message, non-list content, non-dict payload: skipped, never a crash.
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            {
                "timestamp": "t",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": 42},
            },
            {
                "timestamp": "t",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": 7},
            },
            {"timestamp": "t", "type": "response_item", "payload": ["not", "a", "dict"]},
            _user_event("survived the garbage"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "survived the garbage"


# --- subagent threads are not sessions (#821) -----------------------------------------------

_SUB = "019e2ba1-1590-7003-8e4a-51ab62cec003"


def _subagent_meta(uuid, parent, cwd, *, thread_source=True, source_obj=True):
    """A codex ``session_meta`` for a SPAWNED SUBAGENT thread, shaped like the real ones
    (0.145.0 / 0.147.0): the parent's ``cwd`` and ``session_id``, its own ``id``. Each marker
    is independently toggleable so a test can prove either one alone is enough."""
    payload = {
        "session_id": parent,  # the PARENT's id — NOT this rollout's
        "id": uuid,
        "forked_from_id": parent,
        "parent_thread_id": parent,
        "cwd": cwd,
        "originator": "codex-tui",
        "cli_version": "0.147.0",
    }
    if source_obj:
        spawn = {"parent_thread_id": parent, "depth": 1, "agent_nickname": "Galileo"}
        payload["source"] = {"subagent": {"thread_spawn": spawn}}
    if thread_source:
        payload["thread_source"] = "subagent"
    return {"timestamp": "t", "type": "session_meta", "payload": payload}


def _write_subagent(root, uuid, parent, cwd, *, ts="2026-05-15T16-00-00", **markers):
    # A subagent rollout inherits the parent's conversation head, which is exactly why an
    # unfiltered scan renders it as a near-identical duplicate row.
    return _write_records(
        root,
        uuid,
        [_subagent_meta(uuid, parent, cwd, **markers), _user_event("the parent's prompt")],
        ts=ts,
    )


def test_codex_scan_excludes_subagent_threads(codex_root):
    # One real session that spawned a subagent must be ONE row, not two — the subagent
    # carries the parent's cwd and prompt, so an unfiltered scan duplicates the session.
    _write_records(codex_root, _U1, [_session_meta(_U1, "/work"), _user_event("the real prompt")])
    _write_subagent(codex_root, _SUB, _U1, "/work")
    uuids = {s.uuid for s in engines.CodexProvider().scan()}
    assert uuids == {_U1}


@pytest.mark.parametrize(
    "markers",
    [
        {"thread_source": True, "source_obj": True},  # both markers, as codex writes them
        {"thread_source": True, "source_obj": False},  # only thread_source
        {"thread_source": False, "source_obj": True},  # only the source object
    ],
)
def test_codex_subagent_marker_either_field_alone_suffices(codex_root, markers):
    # Two INDEPENDENT markers: a future codex renaming one must not quietly resurrect the rows.
    _write_subagent(codex_root, _SUB, _U1, "/work", **markers)
    assert engines.CodexProvider().scan() == []


@pytest.mark.parametrize("source", ["cli", "exec", "vscode"])
def test_codex_scan_keeps_every_top_level_origin(codex_root, source):
    # A top-level session has a STRING source; `exec` (Hermes' fan-out) and `vscode` are real
    # sessions and stay listed — this fix narrows to spawned subagents only.
    meta = _session_meta(_U1, "/work")
    meta["payload"] |= {"source": source, "thread_source": "user", "session_id": _U1}
    _write_records(codex_root, _U1, [meta, _user_event("hi")])
    assert [s.uuid for s in engines.CodexProvider().scan()] == [_U1]


def test_codex_scan_keeps_fork_without_a_subagent_marker(codex_root):
    # Negative control (Hermes on #821): a plain FORK / compaction of a real session also
    # carries parent_thread_id + forked_from_id. Those fields alone must never hide a row —
    # only the two explicit subagent markers do.
    _write_subagent(codex_root, _SUB, _U1, "/work", thread_source=False, source_obj=False)
    assert [s.uuid for s in engines.CodexProvider().scan()] == [_SUB]


def test_codex_subagent_marker_only_counts_on_the_session_meta_record(codex_root):
    # The predicate is scoped to session_meta (Hermes on #821): _meta walks generic payloads,
    # so a look-alike key on any OTHER record must not be able to hide a real session.
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            {
                "timestamp": "t",
                "type": "response_item",
                "payload": {"thread_source": "subagent", "source": {"subagent": {}}},
            },
            _user_event("still a real session"),
        ],
    )
    assert [s.uuid for s in engines.CodexProvider().scan()] == [_U1]


def test_codex_scan_keeps_rollout_predating_the_markers(codex_root):
    # Older codex builds write neither field; absent marker means top-level, never subagent.
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    assert [s.uuid for s in engines.CodexProvider().scan()] == [_U1]


def test_codex_reconcile_ignores_subagent_spawned_in_window(codex_root):
    # #315 + #821: a session we just launched immediately spawns a subagent, so TWO new
    # rollouts land in our cwd inside the poll window. Only one is a session — reconcile must
    # return it, not fail safe on a bogus ambiguity (and never adopt the subagent's uuid).
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    _write_records(codex_root, _U1, [_session_meta(_U1, "/work"), _user_event("hi")])
    _write_subagent(codex_root, _SUB, _U1, "/work")
    assert prov.reconcile_new_session("/work", snap) == _U1


# --- the first-user scan bound (#1048) ------------------------------------------------------


def _write_long_rollout(root, *, uuid, cwd, fallback, late_user_message, filler):
    """A rollout whose only ``user_message`` EVENT sits past ``filler`` records.

    Shaped from the real store: codex writes ``session_meta`` first (carrying ``cwd``), and on the
    24 of 170 rollouts here that carry no ``user_message`` at all, the old loop ran to EOF because
    the ``role:"user"`` fallback never breaks it.
    """
    d = root / "2026/05/15"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-05-15T15-33-57-{uuid}.jsonl"
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": cwd}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": fallback}],
            },
        },
    ]
    lines += [
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "n": i}}
        for i in range(filler)
    ]
    lines.append(
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": late_user_message},
        }
    )
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.fixture
def small_bound(monkeypatch):
    """Drive the bound at a size a test can build.

    The fixture patches the constant rather than sizing the fixture FROM it: a test that writes
    ``FIRST_USER_SCAN_RECORDS + 50`` records is a test that tries to allocate a billion dicts the
    moment somebody mutates that constant upward — which is exactly what a mutation check does.
    The shipped value is asserted separately, below.
    """
    monkeypatch.setattr(engines.codex, "FIRST_USER_SCAN_RECORDS", 20)
    return 20


def test_first_user_search_stops_at_the_bound_and_keeps_the_fallback(codex_root, small_bound):
    """RED before #1048: the loop read to EOF and titled the session with the far-away
    ``user_message``. A 77 MB rollout paid that on every single walk."""
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    _write_long_rollout(
        codex_root,
        uuid=uuid,
        cwd="/home/u/proj",
        fallback="the early fallback turn",
        late_user_message="a user_message far past the bound",
        filler=small_bound + 50,
    )
    (row,) = engines.CodexProvider().scan()
    assert row.first_user_message == "the early fallback turn"


def test_a_user_message_within_the_bound_still_wins_over_the_fallback(codex_root, small_bound):
    """The bound must not change which turn is preferred — only how far the search goes. On the
    real store every first ``user_message`` landed by record 11."""
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96f"
    _write_long_rollout(
        codex_root,
        uuid=uuid,
        cwd="/home/u/proj",
        fallback="the early fallback turn",
        late_user_message="the real first prompt",
        filler=5,
    )
    (row,) = engines.CodexProvider().scan()
    assert row.first_user_message == "the real first prompt"


def test_the_bound_does_not_read_past_itself(codex_root, small_bound, monkeypatch):
    """Behaviour is one thing; the COST is the point of #1048. Count the lines actually consumed."""
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec970"
    path = _write_long_rollout(
        codex_root,
        uuid=uuid,
        cwd="/home/u/proj",
        fallback="the early fallback turn",
        late_user_message="never reached",
        filler=small_bound * 4,
    )
    consumed = 0
    real_open = type(path).open

    def counting_open(self, *a, **kw):
        fh = real_open(self, *a, **kw)
        if self != path:
            return fh

        class _Counting:
            def __enter__(inner):
                fh.__enter__()
                return inner

            def __exit__(inner, *exc):
                return fh.__exit__(*exc)

            def __iter__(inner):
                nonlocal consumed
                for line in fh:
                    consumed += 1
                    yield line

        return _Counting()

    monkeypatch.setattr(type(path), "open", counting_open)
    engines.CodexProvider()._meta(path)
    assert consumed <= small_bound + 2


def test_the_shipped_bound_is_sane():
    """The constant itself, since every test above drives a patched one.

    Floor: the real store's latest first ``user_message`` was record 11 across 146 rollouts, so
    anything below ~64 would start changing titles. Ceiling: the whole point is that the read is
    BOUNDED — a value large enough to reach EOF on a 77 MB rollout is the bug again.
    """
    assert 64 <= engines.codex.FIRST_USER_SCAN_RECORDS <= 10_000


# --- the SHAPE rule for injected context (#1051) --------------------------------------------

_RECOMMENDED_PLUGINS = (
    "<recommended_plugins>\n"
    "Here is a list of plugins that are available.\n"
    "</recommended_plugins>\n"
    "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"
)
_TURN_ABORTED = "<turn_aborted>\nThe previous turn was interrupted.\n</turn_aborted>"

# The REAL shape on the author's install, and the one the first cut of this fix did not handle:
# the transcript adapter joins a record's content blocks into one string, so tagged blocks and the
# markdown `# AGENTS.md instructions` preamble arrive INTERLEAVED. A rule that accepts only tagged
# blocks stops dead at the preamble in the middle and leaves all 16 affected transcripts unfiltered
# — which is exactly what the live store showed before this case was added.
_INTERLEAVED = (
    "<recommended_plugins>\nAirtable (airtable@openai-curated-remote)\n</recommended_plugins> "
    "# AGENTS.md instructions for /home/u/proj\n\n<INSTRUCTIONS>\nmap\n</INSTRUCTIONS> "
    "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"
)


@pytest.mark.parametrize(
    ("text", "injected", "why"),
    [
        # The two markers that leaked. `<recommended_plugins>` is the one measured on 18 of 69
        # sessions here; it is emitted AHEAD of `<environment_context>`, so a prefix match on the
        # leading marker alone never saw it.
        (_RECOMMENDED_PLUGINS, True, "recommended_plugins ahead of environment_context"),
        # `<turn_aborted>` carries no known marker anywhere in it — the case that rules out a
        # "contains a known marker" rule and forces the shape rule.
        (_TURN_ABORTED, True, "no known marker present at all"),
        (_INTERLEAVED, True, "tagged blocks interleaved with the markdown preamble"),
        # The preamble LAST, with nothing after it — order does not matter.
        (
            "<turn_aborted>x</turn_aborted> # AGENTS.md instructions for /home/u/proj\n"
            "<INSTRUCTIONS>\nmap\n</INSTRUCTIONS>",
            True,
            "the preamble closing the text",
        ),
        # The bare header form (5 of 172 preambles in the corpus carry no `for <path>`).
        (
            "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nmap\n</INSTRUCTIONS>",
            True,
            "the bare AGENTS.md header",
        ),
        # Everything recognised before must stay recognised.
        ("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>", True, "old marker"),
        ("<user_instructions>\ndo what AGENTS.md says\n</user_instructions>", True, "old marker"),
        (_AGENTS_MD, True, "the >=0.142.5 preamble"),
        # …and no human turn may start being swallowed.
        ("<foo>bar</foo> why does this fail?", False, "prose survives the blocks"),
        ("why does <foo>bar</foo> fail?", False, "prose before the block"),
        ("<html><body>hi</body></html> please review", False, "prose after nested blocks"),
        ("just a normal prompt", False, "no tags at all"),
        ("<unclosed> a thought", False, "an unclosed tag is not a block"),
        ("2 < 3 and 4 > 1", False, "angle brackets that are not tags"),
        ("", False, "empty is not injected"),
        ("   \n ", False, "whitespace is not injected"),
        # Tag-shaped operator input (review 72390): a closed set of codex's own blocks, never
        # "any tag", so none of these is machine context.
        (
            "<task>Refactor auth</task>\n<constraints>no new deps</constraints>",
            False,
            "a tag-structured prompt",
        ),
        ("<div>\n<p>why is this red?</p>\n</div>", False, "pasted HTML"),
        ("<b>fix it</b>", False, "a single formatted instruction"),
        ("<image name=[Image #1]> </image>", False, "an image-only turn"),
        # A known block next to an unknown one is still operator-authored somewhere.
        (
            "<environment_context>x</environment_context>\n<task>do it</task>",
            False,
            "a known block does not launder an unknown one",
        ),
        ("<environment_context>x", False, "an unclosed known block"),
        ("<environment_context attr>x</environment_context>", False, "an opener with extras"),
        # The AGENTS.md header words in PROSE are not the preamble (review 72390, finding 4).
        (
            "<a>x</a> # AGENTS.md instructions are wrong, rewrite them",
            False,
            "the header words after an unknown tag",
        ),
        (
            "# AGENTS.md instructions are wrong, rewrite them",
            False,
            "the header words as a sentence",
        ),
        (
            "# AGENTS.md instructions are wrong, use these:\n<INSTRUCTIONS>x</INSTRUCTIONS>",
            False,
            "a prose header line directly above a closed body",
        ),
        (
            "# AGENTS.md instructions for /p\nplease rewrite them\n<INSTRUCTIONS>x</INSTRUCTIONS>",
            False,
            "prose between the header and the body",
        ),
        (
            "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>x",
            False,
            "an unclosed INSTRUCTIONS body",
        ),
        ("# AGENTS.md instructions for /p", False, "a header with no body"),
        (
            _AGENTS_MD + "\n\nand now please rewrite them",
            False,
            "prose after a real preamble",
        ),
    ],
)
def test_injected_context_shape_table(text, injected, why):
    assert cdx.is_injected_context(text) is injected, why


def test_a_recommended_plugins_preamble_is_no_longer_the_sidebar_title(codex_root):
    """RED before #1051: measured on the live store, 16 of 69 codex sessions opened with this
    block, and at least one carried it as its title."""
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_RECOMMENDED_PLUGINS),
            _user_item("the real prompt"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "the real prompt"


def test_a_turn_aborted_block_is_no_longer_the_sidebar_title(codex_root):
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_TURN_ABORTED),
            _user_item("the real prompt"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "the real prompt"


def test_a_rollout_of_nothing_but_machine_blocks_has_no_title_rather_than_a_fake_one(codex_root):
    """The row still EXISTS — `cwd` is the required field — it simply has no title. Dropping the
    session would be a worse answer than an untitled one."""
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_RECOMMENDED_PLUGINS),
            _user_item(_TURN_ABORTED),
        ],
    )
    row = _scan_one(_U1)
    assert row is not None
    assert row.cwd == "/work"
    assert row.first_user_message == ""


def test_the_transcript_adapter_drops_it_too_from_the_same_predicate(codex_root):
    """The predicate is shared with `transcript._codex_turns_from_records` deliberately, so the
    AI-review / recap input and the sidebar title can never disagree about what is machine text.
    This is the half that #1051's `bind_by_nonce` blocker (#1050) depends on."""
    from agent_sessions import transcript

    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_RECOMMENDED_PLUGINS),
            _user_item(_TURN_ABORTED),
            _user_item("the real prompt"),
        ],
    )
    adapter = transcript.adapter_for("codex")
    turns = adapter(_U1, Path.home()) or []
    user_texts = [t.text for t in turns if t.kind == "text" and t.role == "user"]
    assert user_texts == ["the real prompt"]


def test_the_interleaved_preamble_is_no_longer_the_first_transcript_turn(codex_root):
    """The half that matters for the AI review, the recap and #1050's nonce binding.

    Measured on the live store: 16 of 69 codex sessions had this as their first surviving user
    turn, and the first cut of this fix left every one of them unfiltered because it only
    understood tagged blocks.
    """
    from agent_sessions import transcript

    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_INTERLEAVED),
            _user_item("the real prompt"),
        ],
    )
    adapter = transcript.adapter_for("codex")
    turns = adapter(_U1, Path.home()) or []
    assert [t.text for t in turns if t.kind == "text" and t.role == "user"] == ["the real prompt"]


def test_the_shape_scan_classifies_a_large_block():
    """Tens-of-kilobytes payloads: a big known block classifies, and the same block with its
    closing tag cut off refuses. This checks the answer, not the running time — see the next test
    for that."""
    big = "<recommended_plugins>\n" + ("plugin line\n" * 20_000) + "</recommended_plugins>"
    assert cdx.is_injected_context(big) is True
    assert cdx.is_injected_context(big[: -len("</recommended_plugins>")]) is False


_N = 100_000


@pytest.mark.parametrize(
    "text",
    [
        # 100k complete sections and ONE stray character at the end: the scan must walk every
        # section before it can refuse. Anything that re-scans from the start, re-slices the rest
        # of the string per section, or backtracks is quadratic here.
        "<turn_aborted></turn_aborted>" * _N + "x",
        "<turn_aborted></turn_aborted>\n" * _N,
        # 100k AGENTS.md preambles back to back, then prose.
        ("# AGENTS.md instructions for /p\n<INSTRUCTIONS></INSTRUCTIONS>\n" * _N) + "hi",
        # A regex-backtracking classic: an opener followed by a flood of near-miss closers.
        "<environment_context>" + "</environment_contex" * _N,
        "<" * 1_000_000,
    ],
    ids=["sections-then-stray", "sections-only", "preambles-then-prose", "near-miss", "lt-flood"],
)
def test_the_shape_scan_is_linear_on_pathological_input(text):
    """Measured: the linear scan takes <= 0.3 s on the slowest of these; a mutant that re-slices
    the remaining string per section (``s = s[end:]``) takes ~12 s at this size and grows 4x per
    doubling. A 5 s bound leaves the real scan >15x headroom for a loaded CI runner and still
    fails the quadratic one."""
    import time

    t0 = time.perf_counter()
    cdx.is_injected_context(text)
    assert time.perf_counter() - t0 < 5.0


# --- the user_message path keeps the pre-#1051 rule (review 72390, finding 2) ----------------


@pytest.mark.parametrize(
    "prompt",
    [
        "<task>Refactor auth</task>\n<constraints>no new deps</constraints>",
        "<b>fix it</b>",
        # Even text made ONLY of codex's own block names: a user_message is what the operator
        # typed (none of 3,501 in the corpus is machine context), so it is never shape-filtered.
        "<turn_aborted>why did you stop?</turn_aborted>",
        "<recommended_plugins>which ones?</recommended_plugins>",
    ],
)
def test_a_user_message_event_is_never_shape_filtered(codex_root, prompt):
    _write_records(
        codex_root,
        _U1,
        [_session_meta(_U1, "/work"), _user_event(prompt), _user_event("a later prompt")],
    )
    assert _scan_one(_U1).first_user_message == prompt


def test_the_user_message_path_keeps_its_prefix_rule(codex_root):
    """Unchanged from main: a user_message opening with an old marker is still skipped there."""
    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_event("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
            _user_event("the real prompt"),
        ],
    )
    assert _scan_one(_U1).first_user_message == "the real prompt"


def test_a_tag_shaped_operator_turn_stays_in_the_transcript(codex_root):
    """The cost review 72390 named: a false positive drops the turn from what the AI review,
    recap and pulse read, not just from the title."""
    from agent_sessions import transcript

    _write_records(
        codex_root,
        _U1,
        [
            _session_meta(_U1, "/work"),
            _user_item(_RECOMMENDED_PLUGINS),
            _user_item("<task>Refactor auth</task>\n<constraints>no new deps</constraints>"),
            _user_item("<image name=[Image #1]> </image>"),
        ],
    )
    turns = transcript.adapter_for("codex")(_U1, Path.home()) or []
    assert [t.text for t in turns if t.kind == "text" and t.role == "user"] == [
        "<task>Refactor auth</task>\n<constraints>no new deps</constraints>",
        "<image name=[Image #1]> </image>",
    ]
