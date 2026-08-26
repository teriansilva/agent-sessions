"""Per-agent usage, asked of the agent (#839).

**The agents already track this.** An earlier design reconstructed per-agent spend by parsing every
transcript incrementally — a ledger, cursors, byte budgets, partial-record buffers — and the whole
apparatus existed to compute numbers that four of the six engines will simply tell you, three of
them against their *real plan* with the *real reset time*. Measured on a live host:

===============  =========================================  =====================================
engine           how it is asked                            what comes back
===============  =========================================  =====================================
``claude``       ``claude -p "/usage"``                     plan % per window + reset times, and
                                                            the run bills **zero tokens**
``antigravity``  ``agy -p "/usage"``                        remaining % per model family + resets
``codex``        nothing — it writes ``rate_limits`` into   ``used_percent``, window, ``resets_at``,
                 its own rollout                            ``plan_type``
``opencode``     its own database, aggregated               token totals over a window
``kimi``         — (``/usage`` is TUI-only)                 operator's manual counter
``shell``        — (no agent)                               nothing
===============  =========================================  =====================================

Two consequences shape everything here:

**A probe is a fallible external call.** It spawns a CLI that can be slow, missing, logged out, or —
measured while building this — fail on a network eligibility check. So every report carries the time
it was taken, the last good answer is kept when a refresh fails, and a stale figure is labelled
stale rather than blanked or silently re-served as current.

**Two kinds of answer, and they are not interchangeable.** ``plan`` engines report a percentage of a
real quota that the operator never has to configure; ``tokens`` engines report a count that means
nothing until compared with a limit the operator sets. The API says which kind each row is, so no
surface has to guess whether a number is authoritative.

Nothing here writes to an engine's store, and no message text is read: the probes return their own
report, and the two file-backed readers take only the fields named above.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import select
import signal
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock, read_json_doc

log = logging.getLogger("agent_sessions.agent_usage")

#: How long a probe may run before it is abandoned. Generous: `claude -p` starts a session, reads
#: local state and exits, and on a loaded host that is seconds rather than milliseconds.
PROBE_TIMEOUT_S = 90.0

#: Hard cap on what a probe may hand back. Every real answer here is a few hundred bytes; the cap
#: exists because the process on the other end is a vendor CLI whose output we do not control,
#: and an unbounded read of it is an availability hole in the server, not a parsing problem.
MAX_PROBE_BYTES = 256 * 1024

#: A report older than this is served with `stale: true` rather than as current. It is not deleted:
#: yesterday's plan percentage, labelled as yesterday's, beats an empty panel.
STALE_AFTER_S = 3600.0

SOURCE_PLAN = "plan"  # a real quota, reported by the agent — the operator configures nothing
SOURCE_TOKENS = "tokens"  # a count; meaningful only against an operator-set limit
SOURCE_MANUAL = "manual"  # the operator's own counter
SOURCE_NONE = "none"  # this engine reports nothing and no counter has been set


def store_path() -> Path:
    """Where the last good report per engine is kept between sweeps and across restarts."""
    return Path(
        os.environ.get("AGENT_SESSIONS_AGENT_USAGE")
        or (Path.home() / ".config" / "agent-sessions" / "agent-usage.json")
    )


#: The widest percentage that can mean anything. Agents do report over 100 (a plan can be
#: overshot), but a four-hundred-digit one is a malformed record, not a very full quota.
MAX_PCT = 10_000.0

#: Timestamps beyond this are not reset times. Guards a record carrying `1e309` — which `float()`
#: turns into `inf` — from reaching arithmetic or `JSONResponse`.
MAX_EPOCH = 4_102_444_800.0  # 2100-01-01


def _pct(v: object) -> float | None:
    """One percentage from an external record, or None if it is not one.

    Every reporter goes through here, because **these records are written by other programs** and
    one malformed value must not become a 500 on an authenticated read. Two concrete routes, both
    reproduced: a 400-digit decimal in `claude -p` output parses to `inf` and poisons every
    comparison it touches; a JSON `NaN` in a codex rollout reaches Starlette's `JSONResponse`,
    which raises `ValueError: Out of range float values are not JSON compliant`. Neither is a
    parsing problem — both are availability holes fed by untrusted input.
    """
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return f if 0.0 <= f <= MAX_PCT else None


def _epoch(v: object) -> float | None:
    """One timestamp from an external record, or None. Same reasoning as `_pct`, other range."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f if 0.0 < f <= MAX_EPOCH else None


@dataclass(frozen=True)
class Window:
    """One quota window an agent reports: ``session``, ``week``, ``5h`` — whatever it calls it."""

    label: str
    used_pct: float
    resets_at: float | None = None


@dataclass
class Report:
    engine: str
    source: str
    windows: list[Window] = field(default_factory=list)
    #: For ``tokens`` sources: ``{"in", "out", "cache_read", "cache_write"}`` over ``window_days``.
    tokens: dict[str, int] | None = None
    window_days: int | None = None
    plan: str | None = None
    #: When the figures were TRUE — the agent's own observation time where it states one.
    at: float = 0.0
    #: When we last looked. Differs from ``at`` for a store-backed reader whose newest record is
    #: old, and after a failed refresh. ``None`` means "same as ``at``".
    checked_at: float | None = None
    #: Why the last refresh failed, when it did. The report still carries the previous figures.
    error: str | None = None

    def as_dict(self) -> dict:
        out = asdict(self)
        out["windows"] = [asdict(w) for w in self.windows]
        return out


# --- probes ------------------------------------------------------------------------------------


def _run(argv: list[str], *, cwd: str | None = None) -> tuple[int, str]:
    """Run a probe and return ``(returncode, output)``, bounded in **time and in bytes**.

    A literal argv list, never a command string and never a shell — the same rule the engine
    launchers hold, and `pr-validate` greps for violations of it. ``stdin`` is closed because
    `claude -p` otherwise waits three seconds for piped input that is never coming.

    "Bounded" here means: bounded in wall-clock, bounded in bytes read, and the process group
    reaped on every exit path. It does **not** mean a descendant cannot escape — see
    `_reap_group` for what that would cost and why it is not claimed.

    ``subprocess.run(capture_output=True)`` would bound only the time: a malfunctioning or
    hostile CLI can emit gigabytes inside the 90-second window and the parent buffers all of it,
    exhausting the server process. The output of every probe here is a handful of lines, so
    anything past `MAX_PROBE_BYTES` is not an answer we could use — the child is killed and what
    was read so far is returned, which still lets the parser explain what went wrong.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 — literal argv, no shell, bounded
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            # Its own process group, so the timeout and the byte cap bound the whole PROBE and
            # not merely the process we happen to hold a handle to. A CLI that forks a helper
            # (a node runtime, an auth broker) otherwise leaves it running after we walk away,
            # still holding CPU, sockets and file descriptors — the bound would be advertised
            # and not enforced.
            start_new_session=True,
        )
    except OSError as exc:
        return 127, str(exc)

    chunks: list[bytes] = []
    total = 0
    overflowed = False
    deadline = time.monotonic() + PROBE_TIMEOUT_S
    try:
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return _kill(proc, 124, "timed out")
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 1.0))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = proc.stdout.read(65536)
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            total += len(chunk)
            if total > MAX_PROBE_BYTES:
                chunks.append(chunk[: max(0, MAX_PROBE_BYTES - (total - len(chunk)))])
                overflowed = True
                break
            chunks.append(chunk)
    except OSError as exc:
        return _kill(proc, 127, str(exc))
    finally:
        with contextlib.suppress(Exception):
            if proc.stdout is not None:
                proc.stdout.close()

    if overflowed:
        return _kill(proc, 125, f"output exceeded {MAX_PROBE_BYTES} bytes")
    try:
        code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return _kill(proc, 124, "timed out")
    # **Reap the group on the SUCCESS path too.** A probe that forks a helper, detaches its
    # stdio and exits 0 leaves that helper running: the parent's clean exit says nothing about
    # its descendants, so the bound would hold only for probes that misbehave in the ways we
    # already handle. Nothing legitimate outlives `claude -p "/usage"`, so the group goes.
    _reap_group(proc)
    return code, b"".join(chunks).decode("utf-8", "replace")


def _reap_group(proc: subprocess.Popen) -> None:
    """Signal whatever is left of the probe's process group. **Best-effort, not a hard bound.**

    Silent when the group is already empty, which is the normal case: `os.killpg` raises
    `ProcessLookupError` and that *is* the answer we wanted.

    **What this does not do**, stated so the guarantee is not overclaimed: a descendant that
    calls `setsid()` leaves this group and survives. Containing that would need a cgroup or
    scope the child cannot leave, which is available only where the host delegates one (a
    systemd user session does; a bare container or macOS does not) — so it would be a
    conditional bound dressed up as an absolute one.

    The threat model does not justify it either. This executes the operator's own configured
    `claude`/`agy`, deliberately, with the operator's full privileges. A binary hostile enough
    to double-fork away from cleanup already has every capability the app has, and the thing to
    contain then is not its grandchildren. What this rules out is the realistic failure — a CLI
    that hangs, or forks a helper and exits — and that it does hold.
    """
    with contextlib.suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL)


def _kill(proc: subprocess.Popen, code: int, message: str) -> tuple[int, str]:
    """End a probe and **everything it started**, then reap it.

    `proc.kill()` alone signals one pid. The probe runs in its own session (`start_new_session`),
    so the group id is the child's pid and one `killpg` reaches every descendant — which is what
    makes the timeout and the byte cap real bounds rather than advertised ones.
    """
    _reap_group(proc)
    with contextlib.suppress(Exception):
        proc.kill()  # the group is gone in the normal case; this covers a failed setsid
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    return code, message


#: ``Current session: 5% used · resets Aug 26, 1:10pm (Europe/Bucharest)``
#: ``Current week (all models): 31% used · resets Aug 30, 5pm (Europe/Bucharest)``
#: ``Current week (Fable): 0% used``
_CLAUDE_LINE = re.compile(
    r"^Current\s+(?P<label>[^:]+):\s*(?P<pct>\d+(?:\.\d+)?)%\s*used"
    r"(?:.*?resets\s+(?P<resets>[^(]+?)\s*(?:\(|$))?",
    re.MULTILINE,
)


def parse_claude_usage(text: str, now: float | None = None) -> Report:
    """Parse ``claude -p "/usage"``.

    Only the quota lines are read. The rest of that output is a behavioural breakdown ("97% of your
    usage was at >150k context") which is interesting to a human and none of this feature's
    business — and pointedly, it describes *what the operator was doing*, which is exactly the kind
    of thing that should not end up in a stored artifact.
    """
    now = time.time() if now is None else now
    windows: list[Window] = []
    for m in _CLAUDE_LINE.finditer(text):
        label = " ".join(m.group("label").split())
        pct = _pct(m.group("pct"))
        if pct is None:
            # A 400-digit decimal matches `\d+(\.\d+)?` and `float()`s to `inf`. Skipping the
            # line loses one window; forwarding it poisons every comparison downstream.
            continue
        windows.append(
            Window(
                label=label,
                used_pct=pct,
                resets_at=_parse_human_reset(m.group("resets"), now),
            )
        )
    if not windows:
        return Report(engine="claude", source=SOURCE_PLAN, at=now, error="no quota lines in output")
    return Report(engine="claude", source=SOURCE_PLAN, windows=windows, at=now)


def _parse_human_reset(text: str | None, now: float) -> float | None:
    """``Aug 30, 5pm`` → epoch seconds, in the local zone, rolling to next year if needed.

    Best-effort by design: the percentage is the load-bearing number and a reset time that cannot
    be parsed becomes ``None`` rather than failing the whole report. The agent prints its own
    timezone in parentheses, which is dropped — the app renders in the viewer's zone anyway.
    """
    if not text:
        return None
    raw = " ".join(text.split()).rstrip(",")
    lt = time.localtime(now)
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            parsed = time.strptime(f"{raw} {lt.tm_year}", f"{fmt} %Y")
        except ValueError:
            continue
        stamp = time.mktime(parsed)
        if stamp < now - 86400:  # printed in December, read in January
            stamp = time.mktime(parsed[:0] + (parsed[0] + 1,) + parsed[1:])
        return stamp
    return None


#: ``Gemini Models\tWeekly Limit Remaining\t100%\t2026-09-01T10:29:43Z``
_AGY_ROW = re.compile(
    r"^(?P<family>[^\t]+)\t(?P<label>[^\t]+)\t(?P<pct>\d+(?:\.\d+)?)%\t(?P<resets>\S+)\s*$",
    re.MULTILINE,
)


def parse_agy_usage(text: str, now: float | None = None) -> Report:
    """Parse ``agy -p "/usage"`` — tab-separated, and stated as **remaining**, not used.

    The inversion matters: every other engine here reports what it has spent, and rendering a
    "100% remaining" row as "100% used" would put a fresh account at the top of the alarm list.
    """
    now = time.time() if now is None else now
    windows: list[Window] = []
    for m in _AGY_ROW.finditer(text):
        label = f"{m.group('family').strip()} · {m.group('label').strip()}"
        label = label.replace(" Remaining", "")
        remaining = _pct(m.group("pct"))
        if remaining is None:
            continue
        windows.append(
            Window(
                label=label,
                used_pct=max(0.0, 100.0 - remaining),
                resets_at=_parse_iso(m.group("resets")),
            )
        )
    if not windows:
        return Report(
            engine="antigravity", source=SOURCE_PLAN, at=now, error="no quota rows in output"
        )
    return Report(engine="antigravity", source=SOURCE_PLAN, windows=windows, at=now)


def _parse_iso(text: str) -> float | None:
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def probe_claude(binary: str | None = None) -> Report:
    # `base.CLAUDE_BIN`, read at CALL time — not `shutil.which`. The repo's contract is that
    # `AGENT_SESSIONS_CLAUDE_BIN` pins which binary this host runs, and an operator who pins the
    # launcher away from a stale or untrusted PATH entry must not find the probe executing that
    # entry every 15 minutes instead. Call time, so an override is picked up live.
    from .engines import base

    exe = binary or base.CLAUDE_BIN
    if not exe:
        return Report(engine="claude", source=SOURCE_PLAN, at=time.time(), error="claude not found")
    # `--no-session-persistence` (claude ≥ 2.1, `--print` only): the probe leaves **no
    # transcript**. Without it every sweep wrote a JSONL file — ~35,000 a year at this cadence —
    # that the scanner then had to stat and open on every pass merely to hide again. Verified on
    # this host: with the flag, nothing appears under ~/.claude/projects.
    #
    # The scanner's `sdk-cli` filter stays, and is no longer load-bearing for this feature: it
    # covers the transcripts already on disk and any other SDK-driven `claude -p` on the host.
    code, out = _run([exe, "--no-session-persistence", "-p", "/usage"])
    report = parse_claude_usage(out)
    if code != 0 and not report.windows:
        report.error = f"claude -p /usage exited {code}"
    return report


def probe_agy(binary: str | None = None) -> Report:
    from .engines import base

    exe = binary or base.AGY_BIN  # the same binary contract as claude, above
    if not exe:
        return Report(
            engine="antigravity", source=SOURCE_PLAN, at=time.time(), error="agy not found"
        )
    code, out = _run([exe, "-p", "/usage"])
    report = parse_agy_usage(out)
    if code != 0 and not report.windows:
        # Measured while building this: an eligibility check that needs the network fails with
        # `dial tcp … connect: network is unreachable`. A probe is an external call and this is
        # what one looks like when it goes wrong — keep the last good figures, say why.
        report.error = _first_line(out) or f"agy -p /usage exited {code}"
    return report


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


# --- file-backed reporters (no subprocess) ------------------------------------------------------


def read_codex_rate_limits(home: Path | None = None) -> Report:
    """codex writes its own quota into every rollout; the newest one wins.

    A tail read of one file — codex states `used_percent`, the window length, when it resets and
    which plan it is on, so there is nothing to compute and nothing to accumulate.
    """
    from .engines import base

    now = time.time()
    try:
        # `rglob`, not a `*/*/*/` glob: the date nesting is codex's business, not ours, and a
        # hardcoded depth would fail SILENTLY if it ever changed — every codex row would just
        # read "not asked yet" forever. Same discovery the provider uses (`engines/codex.py`),
        # and measured at 0.04 s over 156 rollouts here.
        files = sorted(
            base._codex_sessions_dir(home or Path.home()).rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
    except OSError as exc:
        return Report(engine="codex", source=SOURCE_PLAN, at=now, error=str(exc))
    expired = 0
    for path in files:
        got = _last_rate_limits(path)
        if got is None:
            continue
        limits, observed_at = got
        windows = []
        for key in ("primary", "secondary"):
            block = limits.get(key)
            if not isinstance(block, dict):
                continue
            pct = _pct(block.get("used_percent"))
            if pct is None:
                continue
            resets_at = _epoch(block.get("resets_at"))
            # **A window whose reset has passed is a DEAD period, and its percentage is not
            # this period's.** codex only writes a quota while it runs, so a host that hasn't
            # used codex for a week has a rollout stating 95% against a window that rolled
            # days ago. Reporting it would show a full plan that is actually empty, and would
            # fire an alert for a crossing that already reset. Dropped, not carried.
            if resets_at is not None and resets_at <= now:
                expired += 1
                continue
            windows.append(
                Window(
                    label=_window_label(block.get("window_minutes")),
                    used_pct=pct,
                    resets_at=resets_at,
                )
            )
        if windows:
            plan = limits.get("plan_type")
            return Report(
                engine="codex",
                source=SOURCE_PLAN,
                windows=windows,
                plan=plan if isinstance(plan, str) else None,
                # When codex SAID this, not when we looked — so `stale` describes the figure
                # rather than the read, and a day-old quota is labelled as one.
                at=observed_at,
                checked_at=now,
            )
    return Report(
        engine="codex",
        source=SOURCE_PLAN,
        at=now,
        checked_at=now,
        error=(
            "every rate_limits window has already reset"
            if expired
            else "no rate_limits in recent rollouts"
        ),
    )


def _window_label(minutes: object) -> str:
    if not isinstance(minutes, int | float) or minutes <= 0:
        return "window"
    minutes = int(minutes)
    if minutes % 10080 == 0:
        return "week" if minutes == 10080 else f"{minutes // 10080} weeks"
    if minutes % 1440 == 0:
        return "day" if minutes == 1440 else f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


#: How much of a rollout's tail is searched for the newest `rate_limits`. The record is written on
#: every turn, so the last one is near the end; a file with none in this window reports nothing
#: rather than being read whole.
CODEX_TAIL_BYTES = 512 * 1024


def _last_rate_limits(path: Path) -> tuple[dict, float] | None:
    """The newest ``rate_limits`` in a rollout's tail, **and when codex wrote it**.

    The timestamp is the whole point of the tuple. codex records a quota only while it is
    running, so the newest rollout on a host can be days old — and reporting that percentage
    as if it were just measured is how a dead plan period becomes a current alert.
    """
    try:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
        with path.open("rb") as fh:
            fh.seek(max(0, size - CODEX_TAIL_BYTES))
            blob = fh.read()
    except OSError:
        return None
    if size > CODEX_TAIL_BYTES:
        blob = blob.partition(b"\n")[2]  # drop the partial first line
    found = None
    for line in blob.splitlines():
        if b'"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload") or {}
        # `payload.rate_limits`, a SIBLING of `payload.info` — verified against a live rollout.
        # `info` holds the token counters; the quota sits beside it.
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            found = (limits, _parse_iso(str(obj.get("timestamp") or "")) or mtime)
    return found


#: Aggregate window for opencode's token totals. It reports counts rather than a quota, so the
#: window only has to match what the operator's limit is expressed over.
OPENCODE_WINDOW_DAYS = 7


def read_opencode_tokens(home: Path | None = None, *, days: int = OPENCODE_WINDOW_DAYS) -> Report:
    """Aggregate opencode's own token records over the last ``days``.

    Read **read-only** from its database, which is the same guarantee the opencode provider already
    makes. The figures live in ``part`` rows of type ``step-finish`` — not in ``session_message``,
    which is where an earlier version of this feature looked before concluding, wrongly, that
    opencode records no usage at all.
    """
    from .engines import base

    now = time.time()
    db = base._opencode_db(home or Path.home())
    since_ms = int((now - days * 86400) * 1000)
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    try:
        # `mode=ro`, as the provider does — this feature never writes to opencode's store.
        # A longer timeout than the provider's 0.5 s is deliberate: that one is on the 15-second
        # session-list path where a stall is visible, this one is a background sweep every 15
        # minutes where waiting out a brief write lock beats losing the row.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return Report(engine="opencode", source=SOURCE_TOKENS, at=now, error=str(exc))
    try:
        # `part` carries (id, message_id, session_id, time_created, time_updated, data) — the
        # record's type is inside `data`, so the filter is a LIKE on the blob plus a real check
        # after parsing.
        #
        # **This is a full table scan, and it has to be.** opencode indexes `part` on
        # `session_id` and `(message_id, id)` only, so `time_created` has no index and the plan
        # is `SCAN part`. We cannot add one: the store is read-only to us, and creating an index
        # is a write. Measured on the live database here — 106,300 rows, 5.9 GB — the scan runs
        # in **0.25 s** and returns 3,305 rows for a 7-day window. That is affordable once every
        # 15 minutes in a worker thread, and it is the reason this must never move onto the
        # session-list path or the event loop.
        rows = conn.execute(
            "select data from part where time_created >= ? and data like '%step-finish%'",
            (since_ms,),
        )
        for (raw,) in rows:
            try:
                part = json.loads(raw) or {}
            except ValueError:
                continue
            if part.get("type") != "step-finish":
                continue
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            totals["in"] += _int(tokens.get("input"))
            totals["out"] += _int(tokens.get("output")) + _int(tokens.get("reasoning"))
            totals["cache_read"] += _int(cache.get("read"))
            totals["cache_write"] += _int(cache.get("write"))
    except sqlite3.Error as exc:
        # Fail soft, exactly like the provider: a locked or half-migrated database costs this
        # engine's row, never the sweep.
        return Report(engine="opencode", source=SOURCE_TOKENS, at=now, error=str(exc))
    finally:
        conn.close()
    return Report(engine="opencode", source=SOURCE_TOKENS, tokens=totals, window_days=days, at=now)


def _int(v: object) -> int:
    """One whole in-range count, or 0. Floats included: a stored `inf`/`NaN` must not survive,
    and a float that is not integral was never a token count."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")) or v != int(v):
            return 0
        v = int(v)
    return v if isinstance(v, int) and 0 <= v <= 2**53 else 0


# --- the collection ------------------------------------------------------------------------------

#: engine → how it answers. An engine absent from here reports nothing and falls back to the
#: operator's manual counter; adding one is a single entry.
REPORTERS: dict[str, object] = {
    "claude": probe_claude,
    "antigravity": probe_agy,
    "codex": read_codex_rate_limits,
    "opencode": read_opencode_tokens,
}

#: Engines that run an agent but answer nothing — a manual counter is the only option. `shell` is
#: deliberately absent from every list here: no agent, no usage, no row.
MANUAL_ONLY: tuple[str, ...] = ("kimi", "gemini")


def refresh(
    *, path: Path | None = None, engines: list[str] | None = None, budgets: dict | None = None
) -> dict:
    """Ask every reporting engine and persist the answers. **Blocking** — call under a thread.

    A failed probe never discards the previous answer: the stored report keeps its figures and
    gains an ``error``, so a transient network failure shows as "asked at 09:12, still the last
    known figures" rather than as an empty panel.
    """
    from . import prefs

    store = path or store_path()
    wanted = engines if engines is not None else list(REPORTERS)
    fresh: dict[str, dict] = {}
    for engine in wanted:
        reporter = REPORTERS.get(engine)
        if reporter is None:
            continue
        try:
            report = reporter()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 — one engine's failure is not the sweep's
            log.exception("usage probe failed for %s", engine)
            report = Report(engine=engine, source=SOURCE_PLAN, at=time.time(), error=str(exc))
        fresh[engine] = report.as_dict()

    # **Read the policy AFTER the probes, not before.** A sweep spends up to 90 seconds per
    # engine inside a vendor CLI, and the operator can change the threshold or switch alerts off
    # in that window. A snapshot taken before the probes and trusted after is the same
    # stale-policy-across-the-await bug the orchestrator was fixed for: the decision gets made
    # under a setting that has already been withdrawn. An explicit `budgets` argument still wins
    # — that is the caller pinning it deliberately, which is what the tests do.
    cfg = budgets if budgets is not None else prefs.get_agent_budgets()

    with json_write_lock(store):
        doc = read_json_doc(store)
        # **Recover the outbox first.** A key left in `pending` was written there immediately
        # BEFORE its bell write and never moved on, which means this process died mid-delivery
        # (a known delivery failure clears it explicitly — see `drop_pending`). We cannot tell
        # whether the bell write landed, and the bell itself cannot answer: the operator may
        # have dismissed the row, or the ring may have evicted it.
        #
        # So the tie is broken by #839's contract — *at most one announcement per level per
        # crossing*, and *nothing re-announced after a crash*. Attempted therefore counts as
        # announced. The cost is explicit: a crash landing between the memo and the bell write
        # loses that one announcement. It is bounded and largely self-healing — the 100% level
        # is a separate key and still fires, and a retreat past the band re-arms this one.
        leftover = [k for k in (doc.get("pending") or []) if isinstance(k, str)]
        if leftover:
            held = [k for k in (doc.get("alerted") or []) if isinstance(k, str)]
            doc["alerted"] = sorted(set(held) | set(leftover))
            doc["pending"] = []
            log.warning("usage: %d crossing(s) recovered from a mid-delivery exit", len(leftover))
        reports = doc.get("reports")
        reports = reports if isinstance(reports, dict) else {}
        for engine, new in fresh.items():
            old = reports.get(engine)
            had_figures = isinstance(old, dict) and (old.get("windows") or old.get("tokens"))
            if new.get("error") and had_figures:
                # Keep what was true, say when it was true, and say why it is not newer.
                old = dict(old)
                old["error"] = new["error"]
                old["checked_at"] = new.get("checked_at") or new["at"]
                reports[engine] = old
            else:
                reports[engine] = new
        doc["reports"] = reports
        now = time.time()
        doc["updated_at"] = now
        # Decided against the reports THIS call is writing, under the same lock, so two sweeps
        # racing can't both see "not yet announced" and each announce the same crossing.
        rows = build_rows(reports, cfg, now)
        alerts, still = evaluate_alerts(rows, cfg, doc.get("alerted"))
        # **Persist the re-arm bookkeeping, NOT the delivery.** Writing a crossing here would
        # consume it before anything reached the operator, so a single transient bell-store
        # failure would be silently permanent: the next sweep would see the key as announced
        # and never retry. `mark_announced` commits it, after the bell write is durable.
        #
        # This holds **by construction, not by subtraction**: `evaluate_alerts` builds `still`
        # from keys already in the seen-set, and a fresh crossing is by definition not in it.
        # An earlier version filtered the pending keys back out of `still`, which read as
        # load-bearing while being a no-op — and hid that an undelivered sibling window was
        # being persisted anyway.
        doc["alerted"] = sorted(still)
        atomic_write_json(store, doc)
    return {
        "asked": list(fresh),
        "errors": {e: r["error"] for e, r in fresh.items() if r["error"]},
        "alerts": alerts if cfg.get("notify") else [],
    }


def mark_pending(keys: list[str], path: Path | None = None) -> None:
    """Record that delivery of these crossings is ABOUT to be attempted.

    The outbox. Written before the bell write, so a process that dies mid-delivery leaves
    evidence that an attempt happened — which the bell cannot provide, because its rows are
    dismissible and evictable. `refresh` settles anything left here on the next sweep.
    """
    if not keys:
        return
    store = path or store_path()
    with json_write_lock(store):
        doc = read_json_doc(store)
        cur = [k for k in (doc.get("pending") or []) if isinstance(k, str)]
        doc["pending"] = sorted(set(cur) | set(keys))
        atomic_write_json(store, doc)


def drop_pending(keys: list[str], path: Path | None = None) -> None:
    """Take these crossings back out of the outbox — delivery is KNOWN to have failed.

    The distinction the outbox turns on: a key we can prove was not delivered is retried, while
    one whose fate is unknown (the process vanished) is assumed delivered. Without this, a
    transient bell-store failure would be recovered as "announced" and lost forever — which is
    the bug the outbox was introduced to avoid, arriving by the other door.
    """
    if not keys:
        return
    store = path or store_path()
    with json_write_lock(store):
        doc = read_json_doc(store)
        cur = [k for k in (doc.get("pending") or []) if isinstance(k, str)]
        doc["pending"] = sorted(set(cur) - set(keys))
        atomic_write_json(store, doc)


def mark_announced(keys: list[str], path: Path | None = None) -> list[str]:
    """Record that these crossings reached the operator. Called only AFTER the bell write.

    Split from `refresh` on purpose — see the comment there. A key that never gets here stays
    un-announced and is retried on the next sweep, which is the whole point.
    """
    if not keys:
        return []
    store = path or store_path()
    with json_write_lock(store):
        doc = read_json_doc(store)
        held = doc.get("alerted")
        held = [k for k in held if isinstance(k, str)] if isinstance(held, list) else []
        doc["alerted"] = sorted(set(held) | set(keys))
        doc["pending"] = sorted(
            set(k for k in (doc.get("pending") or []) if isinstance(k, str)) - set(keys)
        )
        atomic_write_json(store, doc)
        return doc["alerted"]


def load(path: Path | None = None) -> dict:
    doc = read_json_doc(path or store_path())
    return doc if isinstance(doc.get("reports"), dict) else {"reports": {}}


# --- what a percentage MEANS, per source ---------------------------------------------------------

#: The engines a usage panel lists, in display order. `shell` is deliberately absent throughout:
#: it launches a login shell with no agent, so it has no usage to have an opinion about.
ENGINES: tuple[str, ...] = ("claude", "codex", "antigravity", "opencode", "kimi", "gemini")


def billable(tokens: dict | None) -> int:
    """The token count a limit is compared against: input + output.

    Cache reads are excluded on purpose. They are the cheapest tokens an engine bills and on this
    host they dominate the raw total — 104M input against 6.4M cache-read for opencode over a week
    — so folding them in would make every limit read as breached for reasons the operator cannot
    act on. The full breakdown still travels in the payload; only the *comparison* is narrowed.
    """
    if not isinstance(tokens, dict):
        return 0
    return _int(tokens.get("in")) + _int(tokens.get("out"))


def derive_pct(report: dict, cfg: dict) -> float | None:
    """The single number a threshold is tested against, or None when there isn't one.

    Three sources, three meanings, and this is the only place they are reconciled:

    * ``plan`` — the agent's own percentage. The **highest** window, because a quota is breached
      when *any* of its windows is: a session at 5% while the week sits at 96% is not at 5%.
    * ``tokens`` / ``manual`` — a count over an operator-set limit. No limit, no percentage: the
      count is shown, and nothing alerts on it.
    """
    source = report.get("source")
    if source == SOURCE_PLAN:
        pcts = [
            w["used_pct"]
            for w in report.get("windows") or []
            if isinstance(w, dict) and isinstance(w.get("used_pct"), int | float)
        ]
        return max(pcts) if pcts else None
    limit = cfg.get("limit_tokens") or 0
    if not limit:
        return None
    # `.get`, not `[...]`: `build_rows` always supplies both keys, but this is a public function
    # and a KeyError here would be a 500 on an authenticated read.
    used = cfg.get("manual_used", 0) if source == SOURCE_MANUAL else billable(report.get("tokens"))
    return round(used * 100.0 / limit, 1)


def clean_tokens(tokens: object) -> dict[str, int] | None:
    """The token counts as whole in-range numbers, or None when there are none.

    Every field, not only the ones the percentage is derived from: the whole dict is serialised
    into the response, so one non-finite value anywhere in it is a 500 regardless of whether the
    arithmetic ever touched it.
    """
    if not isinstance(tokens, dict):
        return None
    return {k: _int(tokens.get(k)) for k in ("in", "out", "cache_read", "cache_write")}


def live_windows(report: dict, now: float) -> list[dict]:
    """The windows of a stored report that still describe the CURRENT period.

    The reporters drop expired windows too, but that is not sufficient and the gap was real: a
    reporter that finds *only* expired windows returns an error-only report, and `refresh`'s
    last-good retention then keeps the PREVIOUS document — expired windows and all — so the dead
    period came straight back through the retention path and alerted.

    So the rule lives here, at the boundary every row passes through, and it applies to retained
    data and to every `plan` engine rather than only to the one whose reader was patched. A
    window with no stated reset is kept: absent is not expired. Values are re-normalized on read
    for the same reason — the document on disk was written by an earlier build, or by hand.
    """
    out = []
    for w in report.get("windows") or []:
        if not isinstance(w, dict):
            continue
        pct = _pct(w.get("used_pct"))
        if pct is None:
            continue
        resets_at = _epoch(w.get("resets_at"))
        if resets_at is not None and resets_at <= now:
            continue
        out.append({"label": str(w.get("label") or ""), "used_pct": pct, "resets_at": resets_at})
    return out


def build_rows(reports: dict, budgets: dict, now: float) -> list[dict]:
    """One row per engine from an ALREADY-READ reports dict. Pure: no store, no clock.

    `refresh` calls this with the document it is holding the lock over, so the alert decision is
    made against exactly the reports it just wrote — re-reading the file to decide would let a
    concurrent write change the answer between the two.

    **The row is BUILT, not copied-and-patched.** Starting from `dict(stored_report)` and fixing
    up the fields we happened to think of shipped the same defect four times — non-finite
    percentages, then timestamps, then token counts, then `window_days` — because the default was
    "forward whatever is on disk" and each fix only narrowed it. Every field below is named and
    normalized, so an unknown or malformed key in a stored document cannot reach `JSONResponse`
    at all. Adding a field to the API means adding it here, which is the point.
    """
    cfg_all = budgets.get("engines") or {}
    stored = reports if isinstance(reports, dict) else {}
    rows = []
    for engine in ENGINES:
        cfg = dict(cfg_all.get(engine) or {})
        cfg.setdefault("limit_tokens", _int(cfg.get("limit_tokens")))
        cfg.setdefault("manual_used", _int(cfg.get("manual_used")))
        rep = stored.get(engine)
        if not isinstance(rep, dict):
            # No engine-reported figures. The operator's own counter is the only source left —
            # and if they haven't set one either, the row says so rather than showing a zero
            # that would read as "this agent has used nothing".
            source = SOURCE_MANUAL if (cfg["limit_tokens"] or cfg["manual_used"]) else SOURCE_NONE
            rep = {"engine": engine, "source": source, "at": 0.0}

        source = rep.get("source")
        source = source if source in (SOURCE_PLAN, SOURCE_TOKENS, SOURCE_MANUAL) else SOURCE_NONE
        at = _epoch(rep.get("at")) or 0.0
        # Expiry + normalization applied to what is SERVED and what is ALERTED on, not only to
        # what a reporter just produced — retained figures pass through here too.
        windows = live_windows(rep, now) if rep.get("windows") is not None else None
        row: dict = {
            "engine": engine,
            "source": source,
            "windows": windows,
            "tokens": clean_tokens(rep.get("tokens")) if rep.get("tokens") is not None else None,
            "window_days": _int(rep.get("window_days")) or None,
            "plan": _text(rep.get("plan")),
            "error": _text(rep.get("error")),
            "at": at,
            "checked_at": (_epoch(rep.get("checked_at")) or at) or None,
            "stale": bool(at) and (now - at) > STALE_AFTER_S,
            "limit_tokens": cfg["limit_tokens"],
            "manual_used": cfg["manual_used"],
        }
        row["used_pct"] = derive_pct(row, cfg)
        rows.append(row)
    return rows


def _text(v: object, cap: int = 500) -> str | None:
    """One short string field for the response, or None. Bounded because the source is another
    program's output — an error message is not a place to accept unbounded input."""
    return v[:cap] if isinstance(v, str) and v else None


def snapshot(*, path: Path | None = None, budgets: dict | None = None, now: float | None = None):
    """The full per-agent picture: one row per engine, ready to serve.

    Built from the same function the alert evaluation uses, so a panel showing 91% and an alert
    that never fires cannot drift apart.
    """
    from . import prefs

    if budgets is None:
        budgets = prefs.get_agent_budgets()
    return build_rows(load(path).get("reports") or {}, budgets, time.time() if now is None else now)


#: The second announcement level. Fixed, not configurable: "you are out" is not a preference.
EXHAUST_PCT = 100.0

#: Hysteresis. A level stays held until usage drops this far below it, so a rolling total that
#: wobbles across the line (opencode's is a 7-day sum, so it genuinely falls as old turns age
#: out) announces once rather than on every sweep that re-crosses it.
REARM_MARGIN_PP = 2.0


def alert_key(engine: str, row: dict, level: float) -> str:
    """Identity of ONE crossing: which agent, which window instance, which level, which budget.

    Every part earns its place:

    * **the window's reset time** makes this week's crossing and next week's two different
      events, without anything having to track when a period rolls;
    * **the level** is why there is no latch machinery here. The issue asks for two
      announcements — the operator's threshold, then exhaustion — and two levels in the key
      *are* two independent latches: each is held and re-armed by its own band, so easing from
      100% back to 97% clears exhaustion and leaves the 90% threshold held. One mutable
      "highest level reached" cannot express that; two keys need no code to;
    * **the limit** ends the episode when the operator edits it. A crossing measured against a
      10M limit says nothing about the same count under a 5M one, and halving a limit can
      create a crossing with no drop-below-the-band transition anywhere for a latch to see.
    """
    windows = [w for w in row.get("windows") or [] if isinstance(w, dict)]
    if windows:
        worst = max(windows, key=lambda w: w.get("used_pct") or 0)
        return _window_key(engine, worst, level)
    return f"{engine}:tokens:{row.get('limit_tokens') or 0}:{level:g}"


def _window_key(engine: str, window: dict, level: float) -> str:
    """The one place a plan window's key is spelled.

    `alert_key` and `_live_keys` both need it, and building it twice is how the two drift: a
    mutation that dropped the reset time from one of them left the other still distinguishing,
    so the defect was invisible to every test. One function, one spelling.
    """
    return f"{engine}:{window.get('label')}:{window.get('resets_at') or ''}:{level:g}"


def _live_keys(row: dict, level: float, seen: set[str]) -> set[str]:
    """Every key of this row still part of an episode at ``level``.

    A `plan` row has several windows and only one is "worst" at a time, so each gets its own key.
    A `tokens`/`manual` row has no windows and exactly one key — the same one `alert_key` builds —
    so the two shapes reduce to the same question: which of this row's quotas are still over the
    line?

    Live means at or above the level, or inside the hysteresis band **and already held** — so a
    genuine retreat past the band ends the episode and re-arms it.
    """

    def live(pct: object, key: str) -> bool:
        if not isinstance(pct, int | float):
            return False
        return pct >= level or (pct >= level - REARM_MARGIN_PP and key in seen)

    windows = [w for w in row.get("windows") or [] if isinstance(w, dict)]
    if not windows:
        key = alert_key(row["engine"], row, level)
        return {key} if live(row.get("used_pct"), key) else set()
    out = set()
    for w in windows:
        key = _window_key(row["engine"], w, level)
        if live(w.get("used_pct"), key):
            out.add(key)
    return out


def alert_levels(budgets: dict) -> list[float]:
    """The levels that announce, in order. Exhaustion collapses into the threshold when the
    operator has set it to 100 — one line at 100% should not announce twice."""
    threshold = float(budgets.get("threshold_pct") or 100)
    return [threshold] if threshold >= EXHAUST_PCT else [threshold, EXHAUST_PCT]


def evaluate_alerts(
    rows: list[dict], budgets: dict, alerted: object
) -> tuple[list[dict], list[str]]:
    """Which crossings are newly worth announcing, and the dedupe state to persist.

    Pure: no clock, no store, no notification. The caller decides what to do with the result,
    which is what makes the rule testable without a filesystem.
    """
    levels = alert_levels(budgets)
    seen = {k for k in (alerted if isinstance(alerted, list) else []) if isinstance(k, str)}
    still: set[str] = set()
    fresh: list[dict] = []
    for row in rows:
        pct = row.get("used_pct")
        if not isinstance(pct, int | float):
            continue
        for level in levels:
            key = alert_key(row["engine"], row, level)
            live = _live_keys(row, level, seen)
            # **Suppression is derived from what was DELIVERED, never from a sibling we merely
            # noticed.**
            #
            # "Worst" is a ranking and rankings swap, so a swap must not read as a new crossing —
            # but the previous attempt at that persisted every over-threshold sibling into the
            # seen-set, which marked windows as announced that had never been delivered. If the
            # worst window's bell write then failed and the ranking swapped, the sibling was
            # already "seen" and the operator got neither crossing.
            #
            # So nothing is persisted that was not delivered. An episode is suppressed only when
            # one of its own live windows is in `seen` — and `seen` contains delivered keys only,
            # because that is all `mark_announced` ever writes. A failed delivery leaves the
            # episode un-suppressed and it is offered again, by whichever window is worst next.
            delivered_here = live & seen
            still |= delivered_here
            if delivered_here:
                continue  # this episode has already reached the operator; a swap is not news
            if pct >= level and key not in seen:
                fresh.append(
                    {
                        "engine": row["engine"],
                        "used_pct": pct,
                        "level": level,
                        "key": key,
                        "row": row,
                    }
                )
    # A key that falls out of the state is re-armed: an agent that drops under a level and later
    # climbs back over it is a new crossing, not the same one announced twice.
    return fresh, sorted(still)
