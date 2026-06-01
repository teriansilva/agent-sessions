"""Generate the binary PTY-trace fixtures for the #242 snapshot serializer tests.

Run from the repo root:  ``python tests/fixtures/term/generate.py``

These are synthetic but representative *inline-agent* (claude/codex/gemini) traces:
SGR colour, box-drawing, long lines, and — crucially — **absolute cursor
positioning + line clears** (the spinner/status-line redraw pattern) that is what
mis-positions when raw bytes written at one width are replayed at another. The
serializer's correctness is asserted by a width round-trip (see
``tests/test_termrender.py``); these fixtures give it realistic input and enough
output to populate scrollback history.
"""

from __future__ import annotations

from pathlib import Path

ESC = "\x1b"
RESET = f"{ESC}[0m"
HERE = Path(__file__).parent


def sgr(*codes: int) -> str:
    return f"{ESC}[" + ";".join(str(c) for c in codes) + "m"


def _banner(width: int) -> str:
    # A coloured top banner with box-drawing, sized to `width`.
    bar = "─" * (width - 2)
    title = " agent-sessions "
    pad = width - 2 - len(title)
    return (
        sgr(38, 2, 45, 255, 124)
        + "╭"
        + bar
        + "╮\r\n"
        + "│"
        + title
        + " " * pad
        + "│\r\n"
        + "╰"
        + bar
        + "╯"
        + RESET
        + "\r\n"
    )


def _conversation(width: int, turns: int) -> str:
    """A few turns of inline output: coloured user/assistant lines, some wide,
    plus a tool-output block — enough lines to overflow into history."""
    out = [_banner(width)]
    for i in range(turns):
        out.append(
            sgr(1, 34)
            + f"› user[{i}]:"
            + RESET
            + f" please summarise file section {i} in detail\r\n"
        )
        # An intentionally wide assistant line (wraps differently at narrow widths).
        long = sgr(32) + "● " + RESET + f"Here is turn {i}: " + "lorem ipsum dolor sit amet " * 4
        out.append(long[: width + 60] + "\r\n")
        # A tool-output block with a coloured gutter.
        for j in range(3):
            val = (i + 1) * (j + 1)
            out.append(
                sgr(90)
                + f"  {j + 1:>3} │ "
                + RESET
                + sgr(36)
                + f"result row {i}.{j} = {val}"
                + RESET
                + "\r\n"
            )
    return "".join(out)


def _status_redraw(width: int) -> str:
    """Simulate the spinner/status-line repaint: print a status line, then move the
    cursor up and overwrite it a few times with absolute moves + line clears. This
    is the canonical layout-dependent sequence that garbles on a width change."""
    frames = ["⠋ working", "⠙ working.", "⠹ working..", "⠸ working..."]
    out = ["\r\n"]
    out.append(sgr(33) + frames[0] + RESET + "\r\n")
    for f in frames[1:]:
        # Cursor up one line, carriage return, clear line, rewrite (absolute-ish).
        out.append(f"{ESC}[1A\r{ESC}[2K" + sgr(33) + f + RESET + "\r\n")
    # Finish: clear the status line and print a done line.
    out.append(f"{ESC}[1A\r{ESC}[2K" + sgr(1, 32) + "✓ done" + RESET + "\r\n")
    return "".join(out)


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)

    # Wide trace: rendered as if at ~100 cols (long lines, wide banner).
    wide = _conversation(100, turns=8) + _status_redraw(100)
    (HERE / "claude_repaint_wide.bin").write_bytes(wide.encode("utf-8"))

    # Narrow trace: rendered as if at ~40 cols.
    narrow = _conversation(40, turns=8) + _status_redraw(40)
    (HERE / "claude_repaint_narrow.bin").write_bytes(narrow.encode("utf-8"))

    # Resize-heavy mobile trace: wide content, a redraw, then more content — the
    # bytes carry layout from multiple widths, the hardest case for raw replay.
    mobile = (
        _conversation(90, turns=4)
        + _status_redraw(90)
        + _conversation(50, turns=4)
        + _status_redraw(50)
    )
    (HERE / "claude_repaint_resize_mobile.bin").write_bytes(mobile.encode("utf-8"))

    for p in sorted(HERE.glob("*.bin")):
        print(f"{p.name}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
