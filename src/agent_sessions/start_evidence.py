"""Did the agent we launched actually START? (#916)

**This is not the transcript store, and the difference is the whole bug.** `headless_dispatch`
gated an unattended launch on the engine's own store record, on the reasoning that a first-run or
consent screen is armed for bracketed paste, painted and quiet — so it passes every readiness
signal, and only an artifact the engine writes can tell it apart from a working agent. That
reasoning is right and survives here unchanged.

What was wrong was the artifact. `claude` writes its transcript JSONL **when the session has its
first turn**, not when it starts: measured on 2.1.263, a live, painted, ready agent produced no
`~/.claude/projects/<slug>/<uuid>.jsonl` for 60s, and no new transcript anywhere under `~/.claude`.
`--session-id` fixes the NAME of that file, not the moment of its creation. So the gate waited for
something only the withheld brief could produce: the record waits on the turn, the turn waits on
the brief, the brief waits on the record. Nothing could ever be dispatched.

`~/.claude/sessions/<pid>.json` is the artifact that answers the question actually being asked. It
is written ~1.1s after launch with nothing typed, and — the property that matters — a session
parked on the trust screen produces **none** within 40s. Start evidence, without the deadlock.

## A filename match is not an identity

`sessionId == native` says a file mentions our uuid. It does not say the process we launched is
alive, that it is in the directory we launched it into, or that the entry is not left over from a
dead predecessor. The registry is keyed by **pid**, and pids are reused. So a candidate is bound on
three facts together:

* `sessionId` is the id this dispatch pinned;
* `cwd` is the directory this dispatch launched into (resolved, so `/tmp` vs `/private/tmp` and a
  symlinked checkout do not read as different places);
* `pid` names a **live** process whose `/proc/<pid>/stat` start time equals the recorded
  `procStart` — the same `pid:starttime` pairing `mission_dispatches.owner` uses, and for the same
  reason: a bare pid is a reusable name, and the pair is not.

`procStart` is verified to be field 22 of `/proc/<pid>/stat` verbatim, as a string.

## Three answers, never two

`found` / `absent` / `unreadable`, and the third is not a flavour of the second. "There is no entry"
and "we could not look" are opposite facts, and collapsing them is exactly the defect this module
exists to fix. **Ambiguity is unreadable, not absent**: two live entries claiming one session id is
a state nobody designed, and picking one would be a guess with a process on the end of it.

Nothing here concludes an agent DID NOT start. The caller polls until its deadline; this only ever
reports what is visible right now.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("agent_sessions.start_evidence")

FOUND = "found"
ABSENT = "absent"
UNREADABLE = "unreadable"

#: How many registry files to read in one sweep. The directory holds one file per session this
#: user has ever run and is not pruned aggressively, so it is bounded rather than trusted.
MAX_ENTRIES = 500

#: A registry entry is a handful of fields — a few hundred bytes. Anything past this is not one,
#: and reading it would be an unbounded read of a file whose size somebody else chooses.
MAX_ENTRY_BYTES = 256 * 1024


def sessions_dir(home: Path | None = None) -> Path:
    """Where `claude` records its live sessions. Overridable so tests never read the real one."""
    override = os.environ.get("AGENT_SESSIONS_CLAUDE_SESSIONS_DIR")
    if override:
        return Path(override)
    return (home or Path.home()) / ".claude" / "sessions"


def _resolved(p: str) -> str:
    """Compare directories by what they ARE, not by how they were spelled.

    A launch into a symlinked checkout and a registry entry recording the resolved path are the
    same directory, and refusing that match would fail closed on the common case rather than the
    dangerous one. Falls back to the literal string when the path cannot be resolved — an
    unresolvable cwd is not an invitation to match anything.
    """
    try:
        return str(Path(p).resolve())
    except Exception:  # noqa: BLE001
        return p


#: Three answers about a pid, and the third is not a flavour of the second. Same discipline as
#: the file reads below and for the same reason: "the kernel says there is no such process" and
#: "we were not allowed to look" are opposite facts, and only the first is evidence of anything.
PROC_LIVE = "live"
PROC_GONE = "gone"
PROC_UNREADABLE = "unreadable"


def proc_start_state(pid: int) -> tuple[str, str | None]:
    """``(state, start_time)`` for a pid, distinguishing GONE from UNREADABLE (review 4, find 3).

    The previous version collapsed every failure to `None`, and the caller read `None` as "not our
    process" — so a matching registry row whose `/proc/<pid>/stat` could not be read (permission,
    an I/O error, a stat that would not parse) became a definite NONMATCH. A single such row then
    answered `absent`, and a readable match beside an unreadable second claimant answered `found`,
    hiding exactly the ambiguity the multi-match branch exists to refuse.

    **Only `FileNotFoundError` is a definite negative.** That is the kernel saying the pid does not
    exist. Everything else is us being unable to look, which cannot rule the process out.

    Field 22 is parsed from the LAST ``)`` rather than by splitting on spaces: field 2 is the
    executable name in parentheses and may itself contain spaces and parentheses, which is the
    classic way this parse goes quietly wrong.
    """
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_bytes().decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError):
        return PROC_GONE, None
    except Exception:  # noqa: BLE001 — permission, I/O, a pid that is not usable as one
        return PROC_UNREADABLE, None
    try:
        return PROC_LIVE, raw[raw.rindex(")") + 2 :].split()[19]
    except Exception:  # noqa: BLE001 — a stat we cannot parse is not a dead process
        return PROC_UNREADABLE, None


def proc_start(pid: int) -> str | None:
    """The start time, or None when there isn't one to be had. Kept for callers that only need
    the value; anything deciding ABSENCE must use `proc_start_state` and honour UNREADABLE."""
    return proc_start_state(pid)[1]


#: What one registry entry says about this dispatch. `UNREADABLE` is an entry that CLAIMS this
#: session and whose process evidence could not be read — it is neither a match nor a nonmatch,
#: and counting it as either is finding 3.
BINDS_YES = "yes"
BINDS_NO = "no"
BINDS_UNREADABLE = "unreadable"


def _entry_binds(entry: dict, native: str, cwd: str) -> str:
    """Does this registry entry name the process THIS dispatch launched? Three answers."""
    if str(entry.get("sessionId") or "") != native:
        return BINDS_NO
    recorded_cwd = str(entry.get("cwd") or "")
    if not recorded_cwd or _resolved(recorded_cwd) != _resolved(cwd):
        return BINDS_NO
    # Past here the entry CLAIMS this session in this directory, so its process evidence is the
    # only thing left deciding — and being unable to read that evidence is not a verdict.
    pid = entry.get("pid")
    want = entry.get("procStart")
    if not isinstance(pid, int) or want is None:
        return BINDS_NO
    state, live = proc_start_state(pid)
    if state == PROC_UNREADABLE:
        return BINDS_UNREADABLE
    if state == PROC_GONE:
        return BINDS_NO  # the kernel says it is not there; a dead pid is not our agent
    # A recycled pid wears the same number and a different start time, which is a definite no.
    return BINDS_YES if str(want) == live else BINDS_NO


def _read_entry(p: Path) -> dict | None:
    """One registry entry as a dict, or `None` if it is not one we can trust.

    Opened `O_NONBLOCK` and checked with `fstat` before a byte is read, because **a path is not a
    file**. `~/.claude/sessions/<anything>.json` is whatever is at that name, and `read_text()` on
    a FIFO with no writer blocks for ever — here that is inside the dispatch's readiness poll, on
    a worker thread nothing can cancel, so one mknod in a directory the agent already writes would
    wedge every future dispatch. `O_NONBLOCK` makes the open return regardless and `S_ISREG`
    rejects what it opened; doing it on the descriptor rather than the path also closes the
    window between a `stat` and a later open.

    Bounded, for the same reason the scan is: an entry is a few hundred bytes, so anything past
    `MAX_ENTRY_BYTES` is not one, and reading it would be an unbounded read of a file somebody
    else controls the size of.
    """
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            size += len(b)
            if size > MAX_ENTRY_BYTES:
                return None
            chunks.append(b)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        entry = json.loads(b"".join(chunks))
    except Exception:  # noqa: BLE001 — a torn or half-written file is not an answer
        return None
    return entry if isinstance(entry, dict) else None


def claude_start_state(native: str, cwd: str, *, home: Path | None = None) -> tuple[str, str]:
    """``(state, detail)`` for one dispatched claude session. Never raises.

    `absent` genuinely means "no entry binds to this launch right now", which for a caller that is
    still polling means "not yet". `unreadable` means the question could not be answered — a
    missing registry directory, a directory we cannot list, or two entries claiming one id — and
    must never be reported to an operator as though the agent had failed to start.
    """
    d = sessions_dir(home)
    try:
        if not d.is_dir():
            # NOT `absent`. A registry that is not there does not say the agent did not start; it
            # says this build of the engine did not write one where we looked, and concluding a
            # failed launch from that would be the same "absence read as evidence" mistake in a
            # new place. Fail closed and let the caller time out with an honest reason.
            return UNREADABLE, f"{d} does not exist, so there is nothing to read"
        names = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except OSError as e:
        return UNREADABLE, f"the session registry could not be listed ({type(e).__name__})"

    # THE WHOLE SET IS EXAMINED, AND A TRUNCATED SCAN IS NEVER AN ANSWER (#916 review 1,
    # finding 4). The first version read a fixed 500-name prefix of a sorted listing on every poll.
    # Two failures fell out of that, both of them the defect this module exists to prevent:
    # a valid live entry at position 501 was reported `ABSENT` **for ever**, so a healthy dispatch
    # timed out and was abandoned; and matches inside AND outside the prefix returned `FOUND`
    # instead of the ambiguity refusal, picking a winner from an incomplete set.
    #
    # The cap stays — an unbounded read of somebody's registry is its own hazard — but it now
    # bounds a scan whose completeness is TRACKED, and incompleteness downgrades the answer to
    # `unreadable` rather than being silently treated as "looked everywhere".
    matches: list[dict] = []
    unreadable_files = 0
    read = 0
    for p in names:
        if read >= MAX_ENTRIES:
            break
        read += 1
        entry = _read_entry(p)
        if entry is None:
            unreadable_files += 1
            continue
        verdict = _entry_binds(entry, native, cwd)
        if verdict == BINDS_UNREADABLE:
            # Counted with the failed FILE reads, because it is the same fact one layer down: a
            # row that claims this session and could not be checked. It defeats a confident
            # absence AND a unique positive, which is what the two branches below already do.
            unreadable_files += 1
        elif verdict == BINDS_YES:
            matches.append(entry)

    complete = read >= len(names)

    if len(matches) > 1:
        # Two live processes claiming one session id is a state nobody designed. Choosing between
        # them would be a guess, and there is an unattended agent on the end of it.
        return UNREADABLE, f"{len(matches)} live entries claim session {native}"
    if matches:
        if not complete:
            # One match in a PARTIAL scan is not "the" match: the rest of the set could hold
            # another, which is the ambiguity case wearing a positive's clothes.
            return UNREADABLE, (
                f"a match was found but only {read} of {len(names)} entries could be read, "
                "so it cannot be shown to be the only one"
            )
        if unreadable_files:
            # A FAILED READ DEFEATS A POSITIVE TOO, not only an absence (review 3, finding 5).
            # Completeness was tested as "did we reach the end of the list", which a scan that
            # read every name and failed on some of them does — so one match beside one unreadable
            # entry returned `FOUND`, and the unreadable entry was exactly where a SECOND claimant
            # would have been. That is the ambiguity refusal above, arrived at by a path that
            # never checked. "The only match" is a claim about the whole set, so every row that
            # could not be read is a row that could have contradicted it.
            return UNREADABLE, (
                f"a match was found but {unreadable_files} of {read} entries could not be checked, "
                "so it cannot be shown to be the only one"
            )
        e = matches[0]
        return FOUND, f"pid {e.get('pid')} in {e.get('cwd')} (claude {e.get('version') or '?'})"
    if not complete:
        return UNREADABLE, (
            f"only {read} of {len(names)} registry entries were read, so nothing can be "
            "concluded from not finding one"
        )
    if unreadable_files:
        # ANY failed read defeats a claim of ABSENCE (review 2, finding 5). The previous version
        # required EVERY entry to have failed, so one readable unrelated row beside an unreadable
        # target answered `ABSENT` — and the caller then diagnosed a missing start and blamed an
        # input screen. It cannot: the entry that would have matched may be one of the ones that
        # could not be read.
        #
        # Absence means "we looked at all of them and none matched". A read that failed is not a
        # row we looked at, and that is the whole distinction this module exists to keep.
        return UNREADABLE, (
            f"{unreadable_files} of {read} registry entries could not be checked, so a matching "
            "one cannot be ruled out"
        )
    # Every name was read, every read succeeded, none matched. The only shape that earns ABSENT.
    return ABSENT, f"no registry entry binds session {native} to a live process in {cwd}"
