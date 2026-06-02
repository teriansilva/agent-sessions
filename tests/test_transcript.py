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
    assert "› You" in text and "hello there" in text
    assert "⏺ Claude" in text and "hi back" in text
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
