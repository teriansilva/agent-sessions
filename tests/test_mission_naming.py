"""#935 — "Pulse" is gone from every surface an operator can read.

The operator found three of these one at a time after the rename shipped, which is the argument
for a test rather than another careful sweep. A rendered-page scan cannot see most of them: the
push title is minted server-side, the fallback title lives in the service worker, the scan
refusal is an API detail string, and the prompts catalog group is Python. Each producer is
therefore asserted where it is produced.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_sessions import prompts

ROOT = Path(__file__).resolve().parents[1]


def _in_block_comment(src: str, lineno: int) -> bool:
    """Is line `lineno` (1-based) inside a `/* … */`? Counts openers and closers before it."""
    head = "\n".join(src.splitlines()[: lineno - 1])
    return head.count("/*") > head.count("*/")


def test_the_push_notification_title_default_is_not_pulse():
    """A notification with no title of its own is the one an operator reads at 3am."""
    src = (ROOT / "src" / "agent_sessions" / "notifications.py").read_text()
    assert '"title", "Pulse"' not in src
    assert '"title", "Mission control"' in src


def test_the_service_worker_fallback_title_is_not_pulse():
    """`sw.ts` mints its own default when a push arrives with no title — same surface, different
    producer, and invisible to anything that scans the SPA's rendered output."""
    sw = (ROOT / "web" / "src" / "sw.ts").read_text()
    assert 'data.title || "Pulse"' not in sw
    assert "Mission control" in sw


def test_the_scan_refusal_names_mission_control():
    src = (ROOT / "src" / "agent_sessions" / "routes" / "pulse.py").read_text()
    assert "a Pulse scan is already running" not in src
    assert "a mission control scan is already running" in src


def test_no_prompt_is_catalogued_under_a_pulse_group():
    """`group` is what Settings → Prompts prints above the block. Storage is keyed by `id`, which
    is deliberately untouched — so this renames a label and nothing an operator has saved."""
    groups = {p.group for p in prompts.REGISTRY}  # REGISTRY is a tuple of Prompt
    assert "Pulse" not in groups
    assert "Mission control" in groups
    # …and the ids, which prefs are keyed on, are NOT renamed.
    assert any(p.id.startswith("pulse_") for p in prompts.REGISTRY)


def test_no_user_visible_pulse_survives_in_the_web_copy():
    """A scan of quoted copy in the SPA, with an explicit allowlist for the identifiers that are
    deliberately unchanged (the route path, the task kind, the types, the API paths).

    Deliberately narrow: it looks at string literals and JSX text, never at symbol names, because
    renaming `PulseCard` is out of scope and a test that demanded it would be wrong rather than
    strict."""
    allow = re.compile(
        r"PulseCard|PulseState|PulseConfig|PulseDepth|PulseNotification|PulseAsk|PulseOverview"
        r"|PulseSettings|PulseChat|PulseScan|PulseResult|/pulse|api/pulse|pulse-scan|pulse_"
        r"|pulseScan|pulseChat|\.pulse\b|pulse\?|pulse:"
        # the route component and its stylesheet keep their names — renaming the module is the
        # separate, larger change this issue explicitly excludes
        r"|function Pulse|const Pulse|Pulse\.module\.css|routes/Pulse|\./Pulse"
    )
    offenders: list[str] = []
    for path in sorted((ROOT / "web" / "src").rglob("*.tsx")):
        if path.name.endswith(".test.tsx"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "Pulse" not in line:
                continue
            stripped = line.strip()
            # comments explain history on purpose — "this used to be Pulse's header" is accurate
            if stripped.startswith(("//", "*", "/*")):
                continue
            # …and the CONTINUATION lines of a block comment, which start with prose. Tracking
            # comment state properly needs a parser; the cheap test is whether the line is inside
            # a `/* … */` that opened earlier in the file and has not closed.
            if _in_block_comment(path.read_text(), n):
                continue
            if allow.search(line):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{n}: {stripped[:90]}")
    assert not offenders, "user-visible 'Pulse' still rendered:\n" + "\n".join(offenders)
