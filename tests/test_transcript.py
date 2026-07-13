"""Tests for the engine-agnostic transcript renderer + Claude adapter (issue #242, PR1).

The renderer's load-bearing property is **width-correctness**: because it wraps plain semantic
text (no terminal escapes), every rendered line fits the requested width at any width — which the
raw-byte scrollback could never guarantee. Synthetic-but-representative JSONL fully exercises this
(the renderer isn't width-fragile); real-transcript rendering is validated out-of-band.
"""

from __future__ import annotations

import json
import re

from agent_sessions import transcript as T

SGR = re.compile(r"\x1b\[[0-9;]*m")


def _visible_lines(payload: bytes) -> list[str]:
    return SGR.sub("", payload.decode("utf-8")).split("\r\n")


def _turns_sample() -> list[T.Turn]:
    return [
        T.Turn("user", "please refactor the parser and " + "lorem ipsum dolor sit amet " * 6),
        T.Turn("assistant", "Sure — here's the plan:\n\n1. step one\n2. a much longer step " * 3),
        T.Turn("assistant", "Read(src/agent_sessions/webterm.py)", "tool"),
        T.Turn("tool", "x" * 4000, "result"),
        T.Turn("user", "世界 " * 30),  # wide chars
    ]


# --- renderer ------------------------------------------------------------------------------


def test_render_is_width_correct_at_every_width():
    turns = _turns_sample()
    for cols in (24, 40, 80, 120):
        out = T.render(turns, cols)
        over = [(i, len(line)) for i, line in enumerate(_visible_lines(out)) if len(line) > cols]
        assert not over, f"cols={cols} over-width lines: {over[:3]}"


def test_render_styles_roles_and_kinds():
    out = T.render(
        [
            T.Turn("user", "hello there"),
            T.Turn("assistant", "hi back"),
            T.Turn("assistant", "Bash(ls -la)", "tool"),
            T.Turn("tool", "total 0\nstuff", "result"),
        ],
        80,
        assistant_label="Claude",
    )
    text = out.decode("utf-8")
    # #301 — looks like the real console now: no "You"/"Claude" labels; user = grey-bg block,
    # assistant = a green ● dot.
    assert "› You" not in text and "⏺ Claude" not in text
    assert "hello there" in text and "hi back" in text
    assert "●" in text  # assistant dot marker
    assert "\x1b[48;5;238m" in text  # user grey background block
    assert "⎿ Bash(ls -la)" in text  # tool call → one-line summary
    assert "total 0" in text  # tool result → shown (truncated)


def test_render_hides_nothing_but_thinking_is_never_a_turn():
    # `thinking` blocks are dropped at the adapter, so they never reach the renderer.
    out = T.render([T.Turn("assistant", "visible answer")], 60)
    assert b"visible answer" in out


def test_render_truncates_long_tool_result():
    out = T.render([T.Turn("tool", "y" * 5000, "result")], 80)
    assert len(out) < 1000  # bounded, not the full 5000


def test_render_bounds_to_max_lines():
    turns = [T.Turn("assistant", f"line {i}") for i in range(2000)]
    out = T.render(turns, 80, max_lines=50)
    assert len(_visible_lines(out)) <= 50


def test_render_empty_is_empty():
    assert T.render([], 80) == b""
    assert T.render([T.Turn("assistant", "   ")], 80) == b""


# --- clean-log polish (#260) ---------------------------------------------------------------


def test_render_strips_markdown_syntax():
    md = "## Heading\n\n**bold** and `code` and ~~old~~\n- one\n* two\n```py\nx=1\n```"
    out = T.render([T.Turn("assistant", md)], 80).decode("utf-8")
    assert "**" not in out and "`" not in out and "~~" not in out
    assert "# Heading" not in out and "Heading" in out
    assert "bold" in out and "code" in out and "old" in out
    assert "• one" in out and "• two" in out  # bullets normalized
    assert "```" not in out and "x=1" in out  # fence stripped, code text kept


def test_render_strips_markdown_but_keeps_width_exact():
    md = "**" + ("word " * 40) + "** and `" + ("y" * 60) + "`"
    for cols in (24, 50, 80):
        lines = _visible_lines(T.render([T.Turn("assistant", md)], cols))
        assert not [ln for ln in lines if len(ln) > cols]


def test_render_result_is_single_dimmed_line():
    lines = _visible_lines(T.render([T.Turn("tool", "first line\nsecond\nthird", "result")], 80))
    body = [ln for ln in lines if ln.strip()]
    assert any("⎿ first line" in ln for ln in body)
    assert all("second" not in ln and "third" not in ln for ln in body)  # only first line


def test_result_text_extracts_blocks_not_json():
    # list-of-text-blocks → joined text (the #260 bug: was json.dumps'd to [{"type":...}])
    assert T._result_text([{"type": "text", "text": "hello"}]) == "hello"
    assert T._result_text("plain") == "plain"
    # image / tool_reference blocks → a tag, never the raw blob (no base64 in scroll-up)
    assert T._result_text([{"type": "image", "source": {"data": "AAAABBBB" * 999}}]) == "[image]"
    assert "[tool_reference]" == T._result_text([{"type": "tool_reference", "tool_name": "x"}])
    assert "{" not in T._result_text([{"type": "image", "source": {"data": "z" * 5000}}])


def test_claude_parser_tool_result_list_content(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": [{"type": "text", "text": "RESULT OK"}]}
                    ],
                },
            }
        ],
    )
    turns = T.claude_turns_from_jsonl(p)
    res = [t for t in turns if t.kind == "result"]
    assert res and res[0].text == "RESULT OK"
    assert "[{" not in res[0].text  # not the JSON wrapper


# --- Claude adapter / parser ---------------------------------------------------------------


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_claude_parser_decodes_blocks(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "system", "subtype": "init"},  # ignored
            {"type": "user", "message": {"role": "user", "content": "string content here"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secret reasoning"},
                        {"type": "text", "text": "the answer"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b.py"}},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "result body"}],
                },
            },
        ],
    )
    turns = T.claude_turns_from_jsonl(p)
    kinds = [(t.role, t.kind) for t in turns]
    assert ("user", "text") in kinds
    assert ("assistant", "text") in kinds
    assert ("assistant", "tool") in kinds
    assert ("tool", "result") in kinds
    # thinking is omitted entirely
    assert all("secret reasoning" not in t.text for t in turns)
    # tool_use rendered as name(arg)
    assert any(t.kind == "tool" and "Edit(" in t.text and "/a/b.py" in t.text for t in turns)


def test_claude_parser_bad_lines_and_missing_file(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('not json\n{"type":"user","message":{"role":"user","content":"ok"}}\n\n')
    turns = T.claude_turns_from_jsonl(p)
    assert [t.text for t in turns] == ["ok"]  # bad/blank lines skipped
    assert T.claude_turns_from_jsonl(tmp_path / "nope.jsonl") == []  # missing → []


def test_claude_parser_tail_reads_large_file(tmp_path, monkeypatch):
    # Only the last _TAIL_BYTES are read, so a huge transcript still parses fast and returns the
    # most recent turns (older content beyond the tail window is dropped, by design).
    monkeypatch.setattr(T, "_TAIL_BYTES", 2000)
    p = tmp_path / "big.jsonl"
    recs = [
        {"type": "assistant", "message": {"role": "assistant", "content": f"msg {i} " + "z" * 80}}
        for i in range(200)
    ]
    _write_jsonl(p, recs)
    assert p.stat().st_size > 2000
    turns = T.claude_turns_from_jsonl(p)
    assert turns and turns[-1].text.startswith("msg 199")  # newest survives
    assert not any(t.text.startswith("msg 0 ") for t in turns)  # oldest dropped (beyond tail)


def test_adapter_registry_and_resolution(tmp_path):
    assert T.adapter_for("claude") is not None
    assert T.adapter_for("no-such-engine") is None
    # _claude_adapter resolves <id>.jsonl under home/.claude/projects/*/ and parses it.
    proj = tmp_path / ".claude" / "projects" / "-home-u-proj"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "abc-123.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "hi from disk"}}],
    )
    turns = T.adapter_for("claude")("abc-123", tmp_path)
    assert [t.text for t in turns] == ["hi from disk"]
    assert T.adapter_for("claude")("missing-id", tmp_path) == []


# --- codex adapter -------------------------------------------------------------------------


def _codex_msg(role, text):
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"text": text}]},
    }


def test_codex_parser_decodes_messages_calls_and_output():
    recs = [
        {"type": "session_meta", "payload": {}},  # ignored
        _codex_msg("user", "<environment_context>\n  <cwd>/x</cwd>"),  # machine preamble → skipped
        _codex_msg("user", "do the thing"),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": "secret"}},  # hidden
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"ls -la"}',
            },
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "total 0"}},
        _codex_msg("assistant", "done"),
    ]
    turns = T._codex_turns_from_records(recs)
    kinds = [(t.role, t.kind) for t in turns]
    assert ("user", "text") in kinds and ("assistant", "text") in kinds
    assert ("assistant", "tool") in kinds and ("tool", "result") in kinds
    assert all("secret" not in t.text for t in turns)  # reasoning hidden
    assert not any(t.text.startswith("<environment_context") for t in turns)  # preamble dropped
    assert any(t.kind == "tool" and "shell(" in t.text and "ls -la" in t.text for t in turns)
    assert any(t.kind == "result" and "total 0" in t.text for t in turns)


def test_codex_parser_skips_agents_md_preamble_keeps_full_prompt():
    # >=0.142.5 injects AGENTS.md as a plain user message (#670) - dropped via the predicate
    # shared with the provider's title fallback, while the real prompt is kept FULL
    # (multiline, >120 chars): only sidebar titles are capped, never transcript turns.
    long_prompt = "please fix this\n" + "detail line\n" * 30
    recs = [
        _codex_msg("user", "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>doc</INSTRUCTIONS>"),
        _codex_msg("user", long_prompt),
    ]
    turns = T._codex_turns_from_records(recs)
    assert len(turns) == 1
    assert turns[0].text == long_prompt.strip()
    assert len(turns[0].text) > 120


def test_codex_adapter_resolves_rollout_glob(tmp_path):
    assert T.adapter_for("codex") is not None
    d = tmp_path / ".codex" / "sessions" / "2026" / "06"
    d.mkdir(parents=True)
    _write_jsonl(d / "rollout-2026-06-07-abc-123.jsonl", [_codex_msg("user", "hello codex")])
    turns = T.adapter_for("codex")("abc-123", tmp_path)
    assert [t.text for t in turns] == ["hello codex"]
    assert T.adapter_for("codex")("missing", tmp_path) == []


def test_adapters_honor_env_overridden_store_paths(tmp_path, monkeypatch):
    # The providers discover sessions via env-overridable store locations
    # (AGENT_SESSIONS_CODEX_SESSIONS_DIR / _OPENCODE_DB / _GEMINI_TMP_DIR). The transcript adapters
    # must read the SAME location, else an overridden store renders in the sidebar but scroll-up
    # falls back to raw. (Hermes #313.) Override wins over the passed `home`.
    # codex — sessions dir elsewhere
    codex_dir = tmp_path / "elsewhere" / "codex-sessions"
    (codex_dir / "2026").mkdir(parents=True)
    _write_jsonl(codex_dir / "2026" / "rollout-x-id1.jsonl", [_codex_msg("user", "codex override")])
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(codex_dir))
    assert [t.text for t in T.adapter_for("codex")("id1", tmp_path / "wrong-home")] == [
        "codex override"
    ]
    # opencode — db elsewhere
    db = tmp_path / "elsewhere" / "oc.db"
    _opencode_db(db, "ses_x", [("m1", "user", [{"type": "text", "text": "oc override"}])])
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    assert [t.text for t in T.adapter_for("opencode")("ses_x", tmp_path / "wrong-home")] == [
        "oc override"
    ]
    # gemini — tmp dir elsewhere
    gtmp = tmp_path / "elsewhere" / "gem-tmp"
    chats = gtmp / "ph" / "chats"
    chats.mkdir(parents=True)
    sid = "11112222-3333-4444-5555-666677778888"
    _gemini_chat(
        chats / "session-2026-06-07T00-00-11112222.jsonl",
        sid,
        [{"type": "user", "content": [{"text": "gem override"}]}],
    )
    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_TMP_DIR", str(gtmp))
    assert [t.text for t in T.adapter_for("gemini")(sid, tmp_path / "wrong-home")] == [
        "gem override"
    ]


# --- opencode adapter ----------------------------------------------------------------------


def _opencode_db(path, session_id, messages):
    """messages: list of (msg_id, role, [part_dict, ...]) → a minimal opencode.db."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, data TEXT)")
    for mid, role, parts in messages:
        conn.execute(
            "INSERT INTO message VALUES (?,?,?)", (mid, session_id, json.dumps({"role": role}))
        )
        for j, part in enumerate(parts):
            conn.execute("INSERT INTO part VALUES (?,?,?)", (f"{mid}_p{j}", mid, json.dumps(part)))
    conn.commit()
    conn.close()


def test_opencode_adapter_reads_messages_and_parts(tmp_path):
    assert T.adapter_for("opencode") is not None
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    _opencode_db(
        db,
        "ses_1",
        [
            ("msg_a", "user", [{"type": "text", "text": "hi opencode"}]),
            (
                "msg_b",
                "assistant",
                [
                    {"type": "step-start"},  # chrome → omitted
                    {"type": "tool", "tool": "bash", "state": {"input": {"command": "ls"}}},
                    {"type": "text", "text": "here you go"},
                ],
            ),
        ],
    )
    turns = T.adapter_for("opencode")("ses_1", tmp_path)
    kinds = [(t.role, t.kind) for t in turns]
    assert ("user", "text") in kinds and ("assistant", "text") in kinds
    assert ("assistant", "tool") in kinds
    assert not any("step-start" in t.text for t in turns)
    assert any(t.kind == "tool" and "bash(" in t.text for t in turns)
    assert T.adapter_for("opencode")("no-session", tmp_path) == []  # unknown id → []
    assert T.adapter_for("opencode")("ses_1", tmp_path / "nohome") == []  # missing db → []


# --- gemini adapter ------------------------------------------------------------------------


def _gemini_chat(path, session_id, records):
    header = {"sessionId": session_id, "projectHash": "ph", "kind": "main"}
    path.write_text("\n".join(json.dumps(r) for r in [header, *records]), encoding="utf-8")


def test_gemini_parser_user_and_model_text_hides_thoughts(tmp_path):
    p = tmp_path / "session-2026-06-07T00-00-abcd1234.jsonl"
    _gemini_chat(
        p,
        "abcd1234-0000-0000-0000-000000000000",
        [
            {"type": "info", "content": "Update successful!"},  # system → skipped
            {"type": "user", "content": [{"text": "Hi"}]},
            {"type": "gemini", "content": "Hello there", "thoughts": "[hidden reasoning]"},
            {"type": "gemini", "content": ""},  # empty (pure thoughts) → no turn
        ],
    )
    turns = T._gemini_turns_from_jsonl(p)
    assert [(t.role, t.text) for t in turns] == [("user", "Hi"), ("assistant", "Hello there")]
    assert all("hidden reasoning" not in t.text for t in turns)


def test_gemini_adapter_resolves_by_short_id_and_header(tmp_path):
    assert T.adapter_for("gemini") is not None
    chats = tmp_path / ".gemini" / "tmp" / "projhash" / "chats"
    chats.mkdir(parents=True)
    sid = "abcd1234-1111-2222-3333-444455556666"
    _gemini_chat(
        chats / "session-2026-06-07T00-00-abcd1234.jsonl",
        sid,
        [{"type": "user", "content": [{"text": "from gemini disk"}]}],
    )
    turns = T.adapter_for("gemini")(sid, tmp_path)
    assert [t.text for t in turns] == ["from gemini disk"]
    assert T.adapter_for("gemini")("ffffffff-0000-0000-0000-000000000000", tmp_path) == []


def test_gemini_adapter_short_prefix_collision_does_not_cross_sessions(tmp_path):
    # Two chat files share the 8-char filename short-id but have DIFFERENT sessionId headers.
    # Resolving by short-id must NOT render the neighbour's conversation: an exact header match or
    # nothing. (Hermes #313 — privacy/correctness: never show a different session's transcript.)
    chats = tmp_path / ".gemini" / "tmp" / "ph" / "chats"
    chats.mkdir(parents=True)
    target = "abcd1234-1111-1111-1111-111111111111"
    other = "abcd1234-9999-9999-9999-999999999999"
    _gemini_chat(
        chats / "session-2026-06-07T00-00-abcd1234.jsonl",
        other,
        [{"type": "user", "content": [{"text": "WRONG SESSION"}]}],
    )
    # The target session has no file at all → must resolve to [] despite the short-id collision.
    assert T.adapter_for("gemini")(target, tmp_path) == []
    # And the neighbour still resolves correctly for ITS own id.
    assert [t.text for t in T.adapter_for("gemini")(other, tmp_path)] == ["WRONG SESSION"]


# --- render_with_boundary (#348 / Hermes #365 r2) -------------------------------------------


def test_render_boundary_zero_when_nothing_truncated():
    out, boundary = T.render_with_boundary(_turns_sample(), 80)
    assert out == T.render(_turns_sample(), 80)
    assert boundary == 0  # the render covers the whole turn list


def test_render_boundary_is_exact_turn_index_at_a_clean_cut():
    # 2 lines per turn (blank spacer + content). max_lines=4 keeps exactly the last 2 turns;
    # the cut lands ON a turn start → boundary is that turn's index.
    turns = [T.Turn("user" if i % 2 == 0 else "assistant", f"T{i}") for i in range(6)]
    out, boundary = T.render_with_boundary(turns, 80, max_lines=4)
    assert boundary == 4
    text = out.decode()
    assert "T4" in text and "T5" in text and "T3" not in text


def test_render_boundary_steps_past_a_turn_cut_mid_render():
    # max_lines=3 keeps the last turn whole plus only the TAIL of the one before it: that
    # partially-shown turn is NOT covered, so the boundary is one past it — a pager using
    # before=boundary re-serves it whole instead of losing its head (overlap beats a hole).
    turns = [T.Turn("user" if i % 2 == 0 else "assistant", f"T{i}") for i in range(6)]
    out, boundary = T.render_with_boundary(turns, 80, max_lines=3)
    assert boundary == 5
    assert "T5" in out.decode()


def test_render_boundary_empty_render_is_zero():
    assert T.render_with_boundary([], 80) == (b"", 0)
    assert T.render_with_boundary([T.Turn("user", "   ")], 80) == (b"", 0)


def test_user_turn_carries_amber_gutter_marker():
    # Operator report (2026-06-11): user vs agent turns were not distinguishable in long
    # transcripts. First user line carries the bold-amber ❯ on the grey band; assistant
    # turns keep the green ● — two distinct voices at a glance.
    from agent_sessions import transcript

    out = transcript.render(
        [transcript.Turn("user", "hello there"), transcript.Turn("assistant", "hi")], cols=40
    ).decode()
    assert "\x1b[1;38;5;214m❯" in out  # amber marker on the user band
    user_line = next(ln for ln in out.split("\r\n") if "hello there" in ln)
    assert transcript._SGR_USER_BG in user_line  # band kept
    assert "●" in out  # assistant dot unchanged
