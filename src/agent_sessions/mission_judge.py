"""The supervisor JUDGES a loose objective (#1088).

A probe observes a fact a server can fetch — a branch, a check, a merge. Some objectives have no
such fact: *"a finding is written down"*, *"every issue tagged type:bug is closed"*. For those the
operator chose judgment over new deterministic probes, with a confidence floor: an independent
model call reads the mission's session output, quotes its evidence and gives a confidence, and the
objective counts as **judged met** only at or above `orchestrator.judge_confidence_min`.

**The working agent never grades itself.** Its own "I'm done" is input to the judge, never
evidence, and the judge is a separate call with its own guarded registry prompt (`mission_judge`).

**Nothing the model says is trusted beyond three values.** The reply names no URL, no path, no
verb and no text to type. The server acts on `met`, `confidence` and the quotes — and a quote is
kept only if it appears verbatim (whitespace-normalised) in the exact source it names, within the
input that was sent. A `met: true` with no verified quote is UNKNOWN, never met. For an agent that
keeps a transcript the live screen is context only: at least one `transcript:` or `diff` quote is
needed. Quotes are passed through `gitwrite.redact` before they are stored or shown.

**The consequence is bounded, not the judgment.** Session output is untrusted text, and an
instruction hidden in it can shape both `met` and `confidence` — it can even quote itself. So a
judgment settles ONE objective, a settled gate set can at most PROPOSE completion (`running ->
review`), and the operator closes the mission or overrules the judgment ("Not met — judge again").
Agent-written text claiming "done" can at most move a mission to `review`.

**A judgment holds only while what it read is unchanged.** It records a fingerprint of the DURABLE
parts of its input — a hash of each transcript tail, or a normalised screen for an agent with no
transcript, plus the checkout's diff and the objective's title — and never the live screen of an
agent that has a transcript, because spinners and clocks move it on an idle session and would
re-judge it every pass. New output marks the judgment stale; the next pass judges again.

**The cost is bounded and predictable.** At most :data:`JUDGE_CALLS_PER_MISSION` calls per mission
per pass and :data:`JUDGE_CALLS_PER_SWEEP` per supervisor sweep, gating rows first; no call when
the fingerprint is unchanged — including after a failed attempt, which records the fingerprint it
tried. A deterministic failure (a reply that breaks the contract, no verified quote, too large to
store) waits for new output; a transient one (timeout, HTTP error, no endpoint) is retried on the
same output no sooner than :data:`TRANSIENT_BACKOFF_S` later.

No new outbound call site: the call is `review.complete_json` -> `review._post_chat`, the one
transport, whose system-message assertion covers this prompt like every other.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field

from . import missions, prefs, prompts, review

log = logging.getLogger(__name__)

#: At most this many judge calls for one mission in one supervisor pass (#1088 §5).
JUDGE_CALLS_PER_MISSION = 2
#: …and this many across one supervisor sweep. The sweep revisits, so nothing is skipped for good.
JUDGE_CALLS_PER_SWEEP = 6
#: How long a TRANSIENT failure waits before the same output is tried again.
TRANSIENT_BACKOFF_S = 30 * 60.0
#: The sessions one judgment reads: the mission's most recently active ones.
SESSIONS_MAX = 3
#: Input bounds, per source. Bounded because this is a background call per objective.
TRANSCRIPT_TAIL_MAX = 6000
SCREEN_MAX = 2000
DIFF_PATCH_MAX = 16 * 1024
DIFFSTAT_FILES_MAX = 40
INSTRUCTION_MAX = 4000
#: The reply contract's own bounds (the store re-checks the stored shape).
QUOTE_MAX = missions.JUDGE_QUOTE_MAX
EVIDENCE_MAX = missions.JUDGE_EVIDENCE_MAX
REASON_MAX = 300
#: What makes a quote EVIDENCE rather than a coincidence (#1088 review, blocker 2). A substring
#: check alone accepted the quote "a" — present in any English text — and settled a gate on it. A
#: quote must carry at least this many non-whitespace characters AND this many words to count; a
#: shorter one is dropped like an unverifiable one. 20 characters / 3 words is the smallest span
#: that names a thing and says something about it ("the root cause is"), while a single token,
#: a path fragment or a status word ("done", "PASSED") never qualifies.
#:
#: SCRIPT-AWARE (#1097 round 3): Chinese, Japanese, Korean and Thai are written without spaces, so
#: any quote in them is one "word" and could never qualify. For a quote containing those scripts,
#: at least :data:`QUOTE_MIN_CJK` of their characters stand in for the three words. The
#: 20-non-whitespace-character floor applies to every quote either way.
QUOTE_MIN_CHARS = 20
QUOTE_MIN_WORDS = 3
QUOTE_MIN_CJK = 10
#: CJK ideographs (incl. extension A and compatibility), kana, Hangul and Thai.
_UNSPACED_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0e00-\u0e7f]"
)
TOO_SHORT_REASON = "quotes too short to count as evidence (min 20 chars, 3 words)"
#: The exact field set a reply must have. Any extra or missing field refuses the whole reply.
REPLY_FIELDS = frozenset({"met", "confidence", "evidence", "reason"})

STALE_REASON = "the session output changed since the judgment"
NO_ENDPOINT_REASON = "cannot be judged — no AI endpoint is configured"


class Budget:
    """The judge-call budget of one window (a sweep, or the early readings between two sweeps).

    CHARGED ONLY FOR A CALL ACTUALLY MADE (#1097 round 9). Judging is the second phase of a sweep
    (`judge_batch`): every mission has already been supervised, and the judge walks the batch in
    least-recently-served order, spending a call only when it really asks the model. A mission
    with nothing to read, nothing due or no endpoint costs nothing and holds nothing, so no
    mission — and no number of them — can keep another waiting. Nothing is remembered between
    windows: the rows' own attempt times are the memory.
    """

    def __init__(self, calls: int = JUDGE_CALLS_PER_SWEEP) -> None:
        self.left = max(0, int(calls))

    def take(self, mission_id: str | None = None) -> bool:  # noqa: ARG002 — kept for callers
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def is_open(row: dict) -> bool:
    """A judged row the judge may still owe work: not waived, and never judged or marked stale
    (an attempt that produced no verdict is recorded stale too). Whether it is actually due
    (backoff, the rejected output, an unchanged failed input) is decided when the judge reaches
    it, at no cost."""
    if str(row.get("state") or "") == "waived":
        return False
    obs = row.get("observed") if isinstance(row.get("observed"), dict) else None
    return obs is None or bool(obs.get("stale"))


def rank_key(rows: list[dict], *, now: float | None = None) -> float | None:  # noqa: ARG001
    """A mission's place in the judge order: when it last RECEIVED SERVICE — the most recent
    attempt on any of its judged rows (0.0 = never served, which goes first). `None` when no row is
    open, so the mission is not in the order at all.

    ROUND ROBIN ACROSS MISSIONS (#1097 review 5126). The key is deliberately the LATEST attempt,
    not the oldest open row: the judge serves gates first and stops at two calls per mission, so a
    mission whose never-judged non-gating rows it never reaches would otherwise keep the key of
    those rows (0) for ever and win every tie-break. Any call — verdict, failure, superseded, or an
    idle "nothing to read" attempt — moves the mission to the back; gate-first order is kept WITHIN
    a mission. From the store alone, read after the sweep's supervision phase marked stale whatever
    the current input changed."""
    rows = [r for r in rows if str(r.get("state") or "") != "waived"]
    if not any(is_open(r) for r in rows):
        return None
    return max((last_attempt(r) for r in rows), default=0.0)


def rank_missions(mission_ids: list[str], *, path=None) -> list[str]:
    """The judge order for these missions: running ones with open judged rows, least recently
    SERVED first (`rank_key`), ties by mission id. DB reads only. A closed, archived or deleted
    mission is simply not in it, so nothing ever needs retiring."""
    ranked: list[tuple[float, str]] = []
    for mid in mission_ids:
        try:
            wl = missions.judge_worklist(mid, path=path)
        except Exception:  # noqa: BLE001 — an unreadable mission is left out of the order
            log.debug("mission %s: unreadable for the judge order", mid)
            continue
        if not wl or wl["state"] != "running" or wl["archived"]:
            continue
        key = rank_key(wl["rows"])
        if key is not None:
            ranked.append((key, mid))
    return [mid for _key, mid in sorted(ranked)]


class ContractError(ValueError):
    """The reply did not match the judge's contract. Deterministic: the same output gets the same
    reply, so it is not retried until the output changes."""


@dataclass
class JudgeInput:
    """What one judgment reads, exactly as it is sent — the quotes are verified against THIS."""

    #: label -> the exact text sent under that label: `transcript:<key>`, `screen:<key>`, `diff`.
    sources: dict[str, str] = field(default_factory=dict)
    #: The fingerprint of the durable parts (see the module note). The title is folded in per row.
    fingerprint: str = ""
    #: `screen:<key>` labels that COUNT as evidence — only an agent with no transcript (`shell`).
    screen_counts: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not any(t.strip() for t in self.sources.values())


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _norm_ws(text: str) -> str:
    return " ".join(str(text or "").split())


def screen_fingerprint_text(screen: str) -> str:
    """A screen with its whitespace runs collapsed and its bottom status line dropped — what an
    idle transcript-less session's screen normalises to, so a ticking status bar is not "new"."""
    lines = [_norm_ws(line) for line in str(screen or "").splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines[:-1])


def row_fingerprint(inp: JudgeInput, row: dict) -> str:
    """The fingerprint ONE row's judgment is bound to: the mission's durable input plus the
    objective's COMPLETE criterion — title and direction, everything the model reads about it. A
    changed criterion is a different question and is judged again (#1097 review 5040)."""
    crit = missions.judge_criterion(row.get("title"), row.get("direction"))
    return _h(f"{inp.fingerprint}\x1f{crit}")


# ---------------------------------------------------------------- input


def _recent_sessions(mission_id: str) -> list[str]:
    """The mission's held sessions, most recently active first, at most :data:`SESSIONS_MAX`."""
    keys = missions.active_session_keys(mission_id)
    # RANKED BY STORE ACTIVITY, NEVER BY THE SCREEN (#1088 review). The terminal's last-output time
    # moves with every spinner frame, so with more than three sessions an idle mission's chosen
    # three flipped between passes — a new fingerprint, and a judgment paid for nothing. The
    # supervisor's `growth_at` moves only when the engine STORE grows (a transcript line), falling
    # back to when the session joined; ties break on the key, so the choice is deterministic.
    added: dict[str, float] = {}
    try:
        row = missions.get_mission(mission_id) or {}
        for s in row.get("sessions") or []:
            if s.get("removed_at") is None and s.get("session_key"):
                added[str(s["session_key"])] = float(s.get("added_at") or 0.0)
    except Exception:  # noqa: BLE001 — an unread roster ranks every session equal
        added = {}

    def activity(k: str) -> float:
        try:
            cp = missions.supervisor_checkpoint(mission_id, session_key=k)
            return float(cp.get("growth_at") or 0.0) or added.get(k, 0.0)
        except Exception:  # noqa: BLE001 — an unreadable checkpoint ranks by join time
            return added.get(k, 0.0)

    return sorted(keys, key=lambda k: (-activity(k), k))[:SESSIONS_MAX]


def _checkout_diff(cwd: str | None) -> str:
    """A bounded view of the checkout's uncommitted work: a diffstat and the first
    :data:`DIFF_PATCH_MAX` characters of patch. Read through `gitpanel`'s sanitized-gitdir,
    blob-based diff — never `git diff`, whose filter drivers run — and fail-soft: no checkout, a
    path outside the home root or a git refusal all read as "no diff"."""
    if not cwd:
        return ""
    try:
        from . import gitpanel

        repo = gitpanel.discover_repo(cwd)
        if repo is None:
            return ""
        status = gitpanel.git_status(repo.toplevel)
        stat: list[str] = []
        patch: list[str] = []
        used = 0
        for e in (status.get("entries") or [])[:DIFFSTAT_FILES_MAX]:
            rel = str(e.get("path") or "")
            kind = str(e.get("kind") or "")
            if not rel:
                continue
            target = os.path.join(repo.toplevel, rel)
            text = ""
            if kind == "untracked":
                # A NEW FILE IS NAMED, NEVER READ (#1088 review). An untracked file is exactly where
                # a scratch `.env.local` or a pasted token lives, and its content would travel to
                # the endpoint and could come back as a stored quote — redaction strips only URL
                # credentials. So the judge sees the name and the size, and nothing inside it.
                try:
                    size = os.lstat(target).st_size
                    stat.append(f"{rel} (new file, {size} bytes, not shown)")
                except OSError:
                    stat.append(f"{rel} (new file, not shown)")
                continue
            else:
                try:
                    d = gitpanel.git_diff(target, staged=kind == "staged")
                except Exception:  # noqa: BLE001
                    stat.append(f"{rel} ({kind})")
                    continue
                stat.append(f"{rel} +{d.get('added') or 0} -{d.get('removed') or 0}")
                text = str(d.get("diff") or "")
            if text and used < DIFF_PATCH_MAX:
                chunk = text[: DIFF_PATCH_MAX - used]
                patch.append(chunk)
                used += len(chunk)
        if not stat:
            return ""
        return "diffstat:\n" + "\n".join(stat) + ("\n\npatch:\n" + "".join(patch) if patch else "")
    except Exception:  # noqa: BLE001 — a diff is context, never a reason to fail a pass
        return ""


def gather(mission_id: str, cwd: str | None) -> JudgeInput:
    """Build the judge's input for one mission. Blocking — call it off the event loop."""
    inp = JudgeInput()
    per: list[str] = []
    for key in _recent_sessions(mission_id):
        try:
            transcript, screen, has_transcript = review.judge_sources(
                key, TRANSCRIPT_TAIL_MAX, SCREEN_MAX
            )
        except Exception:  # noqa: BLE001 — one unreadable session must not blind the others
            log.debug("mission %s: session %s unreadable for the judge", mission_id, key)
            continue
        if transcript:
            inp.sources[f"transcript:{key}"] = transcript
        if screen:
            inp.sources[f"screen:{key}"] = screen
        if has_transcript:
            # THE SCREEN IS NOT IN THE FINGERPRINT for an agent that keeps a transcript: a spinner
            # or a clock moves it on an idle session, and hashing it would re-judge every pass.
            per.append(f"{key}=t:{_h(transcript)}")
        else:
            per.append(f"{key}=s:{_h(screen_fingerprint_text(screen))}")
            inp.screen_counts.add(f"screen:{key}")
    diff = _checkout_diff(cwd)
    if diff:
        inp.sources["diff"] = diff
    inp.fingerprint = _h("\n".join(sorted(per)) + "\ndiff=" + _h(diff))
    return inp


def render_input(inp: JudgeInput, row: dict, instruction: str) -> str:
    """The user message for one objective. Every source is labelled with the label a quote must
    name. Session text is DATA — the guarded system prompt says so, and nothing depends on it."""
    if row.get("key") == missions.DONE_AS_INSTRUCTED_KEY:
        objective = "The work the operator's instruction (below) asks for is done."
    else:
        objective = str(row.get("title") or row.get("key") or "")
    parts = [f"Objective to judge:\n{objective}"]
    if row.get("direction"):
        parts.append(f"The operator's direction for this objective:\n{row['direction']}")
    parts.append(
        "Mission instruction (context, written by the operator):\n"
        f"{str(instruction or '')[:INSTRUCTION_MAX]}"
    )
    blocks = [f"<<<{label}>>>\n{text}\n<<<end {label}>>>" for label, text in inp.sources.items()]
    parts.append(
        "Sources. Quote only from these, word for word, and name the label as `source`:\n\n"
        + "\n\n".join(blocks)
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------- the reply


def _num(v: object) -> bool:
    return (
        isinstance(v, int | float) and not isinstance(v, bool) and v == v and abs(v) != float("inf")
    )


def parse_reply(reply: object) -> dict:
    """The contract, checked strictly. Any extra or missing field refuses the WHOLE reply."""
    if not isinstance(reply, dict):
        raise ContractError("the reply is not an object")
    if set(reply) != REPLY_FIELDS:
        extra = sorted(set(reply) - REPLY_FIELDS)
        missing = sorted(REPLY_FIELDS - set(reply))
        raise ContractError(f"fields: extra {extra}, missing {missing}")
    if not isinstance(reply["met"], bool):
        raise ContractError("`met` is not true or false")
    conf = reply["confidence"]
    if not _num(conf) or not (0.0 <= conf <= 1.0):
        raise ContractError("`confidence` is not a number from 0 to 1")
    reason = reply["reason"]
    if not isinstance(reason, str) or len(reason) > REASON_MAX:
        raise ContractError(f"`reason` is not text of at most {REASON_MAX} characters")
    ev = reply["evidence"]
    if not isinstance(ev, list) or len(ev) > EVIDENCE_MAX:
        raise ContractError(f"`evidence` is not a list of at most {EVIDENCE_MAX}")
    evidence = []
    for q in ev:
        if not isinstance(q, dict) or set(q) != {"source", "quote"}:
            raise ContractError("a piece of evidence is not exactly {source, quote}")
        if not isinstance(q["source"], str) or not isinstance(q["quote"], str):
            raise ContractError("evidence `source` and `quote` must be text")
        if not q["quote"].strip() or len(q["quote"]) > QUOTE_MAX:
            raise ContractError(f"a quote is empty or longer than {QUOTE_MAX} characters")
        evidence.append({"source": q["source"], "quote": q["quote"]})
    return {"met": reply["met"], "confidence": float(conf), "evidence": evidence, "reason": reason}


def _counts(label: str, inp: JudgeInput) -> bool:
    """Does a quote from this source count as EVIDENCE (rather than context)?"""
    return label.startswith("transcript:") or label == "diff" or label in inp.screen_counts


def quote_is_substantial(quote: str) -> bool:
    """At least :data:`QUOTE_MIN_CHARS` non-whitespace characters and :data:`QUOTE_MIN_WORDS`
    words — the floor below which a verbatim match proves nothing."""
    words = str(quote or "").split()
    if sum(len(w) for w in words) < QUOTE_MIN_CHARS:
        return False
    if len(words) >= QUOTE_MIN_WORDS:
        return True
    return len(_UNSPACED_RE.findall(str(quote or ""))) >= QUOTE_MIN_CJK


def _only_too_short(evidence: list[dict], inp: JudgeInput) -> bool:
    """Did the judge quote something it really read, but only spans too short to count?"""
    return any(
        (text := inp.sources.get(q["source"])) is not None
        and (needle := _norm_ws(q["quote"]))
        and needle in _norm_ws(text)
        and not quote_is_substantial(needle)
        for q in evidence
    )


def verify_evidence(evidence: list[dict], inp: JudgeInput) -> tuple[list[dict], bool]:
    """Keep the quotes that appear verbatim (whitespace-normalised) in the source they name,
    within the exact input sent; redact what is kept. Returns ``(kept, counting)`` — whether any
    kept quote is one that can stand as evidence on its own."""
    from . import gitwrite

    kept: list[dict] = []
    for q in evidence:
        text = inp.sources.get(q["source"])
        needle = _norm_ws(q["quote"])
        if text is None or not quote_is_substantial(needle) or needle not in _norm_ws(text):
            continue
        quote = gitwrite.redact(q["quote"].strip())
        if not quote or len(quote) > QUOTE_MAX:
            # Redaction lengthened it past the bound. Dropped whole, never truncated: half a
            # quote is text nobody wrote.
            continue
        kept.append({"source": q["source"], "quote": quote})
    return kept, any(_counts(q["source"], inp) for q in kept)


# ---------------------------------------------------------------- eligibility


def needs_judgment(row: dict, fp: str, *, now: float) -> bool:
    """Should this row be judged NOW? The idle skip, applied to failures as well as verdicts."""
    if str(row.get("state") or "") == "waived":
        return False
    if row.get("judge_rejected_fp") and row["judge_rejected_fp"] == fp:
        # The operator rejected a judgment of exactly this output. Wait for new output.
        return False
    obs = row.get("observed") if isinstance(row.get("observed"), dict) else {}
    rec = obs.get("judged") if isinstance(obs.get("judged"), dict) else {}
    if "fingerprint" in rec and not obs.get("stale") and rec["fingerprint"] == fp:
        return False
    if rec.get("attempted_fp") == fp:
        if not rec.get("transient"):
            return False
        at = obs.get("at")
        if isinstance(at, int | float) and now - float(at) < TRANSIENT_BACKOFF_S:
            return False
    return True


# ---------------------------------------------------------------- staleness


def mark_stale_judgments(mission_id: str, wl: dict, inp: JudgeInput, threshold: float) -> dict:
    """Mark a judgment stale when what it read has changed, and re-apply a changed threshold to one
    whose input has not — with NO model call. Blocking; the pass calls it before it asks the
    completion question, so a stale judgment can never carry a mission into review.

    **Stale means a changed fingerprint and nothing else.** A threshold change on unchanged output
    recomputes `value` from the stored confidence, so raising the setting un-counts a 0.92 and keeps
    a 0.97, lowering it counts a stored 0.92, both on the next pass of an idle mission — and the
    store still never counts a confidence under its floor, or the output the operator rejected.

    It also forgets the rejected fingerprint once the output has moved on, which is why `assess()`
    never has to write anything.
    """
    out = {"stale": 0, "recomputed": 0, "cleared": 0}
    for row in wl.get("rows") or []:
        if str(row.get("state") or "") == "waived":
            continue
        fp = row_fingerprint(inp, row)
        key = row["key"]
        inc = str(row.get("incarnation") or "")
        rejected = row.get("judge_rejected_fp")
        if rejected and rejected != fp:
            if missions.clear_judge_rejected(mission_id, key, expect_fp=rejected):
                out["cleared"] += 1
        obs = row.get("observed") if isinstance(row.get("observed"), dict) else {}
        rec = obs.get("judged") if isinstance(obs.get("judged"), dict) else {}
        if "fingerprint" not in rec or obs.get("stale"):
            continue
        if rec["fingerprint"] != fp:
            carried = {k: v for k, v in rec.items() if k != "stale"}
            carried["stale"] = True
            if missions.observe_objective(
                mission_id,
                key,
                observed=False,
                value=False,
                detail=STALE_REASON,
                stale_kind="input",
                judged=carried,
                expect_probe="supervisor_judged",
                expect_incarnation=inc,
            ):
                out["stale"] += 1
        elif rec.get("threshold") != threshold and not obs.get("rejected_at"):
            if missions.observe_objective(
                mission_id,
                key,
                observed=True,
                value=False,  # computed by the store from the verdict
                detail=str(obs.get("detail") or ""),
                judged={**rec, "threshold": threshold},
                expect_probe="supervisor_judged",
                expect_incarnation=inc,
                expect_title=str(row.get("title") or ""),
            ):
                out["recomputed"] += 1
    return out


# ---------------------------------------------------------------- the runner


def _unknown(mission_id: str, row: dict, fp: str, *, transient: bool, reason: str) -> None:
    """Record a judgment that produced no usable verdict: stale, with the reason, and the
    fingerprint it tried — so the idle skip applies to it. Never met."""
    missions.observe_objective(
        mission_id,
        row["key"],
        observed=False,
        value=False,
        detail=reason,
        stale_kind="unknown",
        judged={"attempted_fp": fp, "transient": transient},
        expect_probe="supervisor_judged",
        expect_incarnation=str(row.get("incarnation") or ""),
    )


async def judge_row(mission_id: str, row: dict, inp: JudgeInput, instruction: str) -> str:
    """ONE judge call for ONE objective. Returns what happened: `met`, `not_met`, `unknown` or
    `superseded` (the row was dropped, re-added or retitled while the call was in flight).
    Never raises for a bad reply."""
    fp = row_fingerprint(inp, row)
    try:
        reply = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("mission_judge")},
                {"role": "user", "content": render_input(inp, row, instruction)},
            ]
        )
    except review.NotConfiguredError:
        await missions.run_admitted(
            lambda: _unknown(mission_id, row, fp, transient=True, reason=NO_ENDPOINT_REASON)
        )
        return "unknown"
    except review.MalformedReplyError:
        await missions.run_admitted(
            lambda: _unknown(
                mission_id,
                row,
                fp,
                transient=False,
                reason="the judge's reply was not valid JSON",
            )
        )
        return "unknown"
    except review.ReviewError as e:
        why = f"the judge could not answer: {e}"[:REASON_MAX]
        await missions.run_admitted(
            lambda: _unknown(mission_id, row, fp, transient=True, reason=why)
        )
        return "unknown"
    try:
        parsed = parse_reply(reply)
    except ContractError as e:
        why = f"the judge's reply did not match the contract ({e})"[:REASON_MAX]
        await missions.run_admitted(
            lambda: _unknown(mission_id, row, fp, transient=False, reason=why)
        )
        return "unknown"
    kept, counting = verify_evidence(parsed["evidence"], inp)
    # COUNTING QUOTES FIRST (#1097 review 5040, finding 3). The store fits an oversized judgment by
    # shedding quotes from the END and keeps the first to the last, so putting evidence that can
    # stand on its own first is what makes "a met verdict keeps a counting quote" hold through
    # fitting. A context-only screen quote is the first to go.
    kept.sort(key=lambda q: not _counts(q["source"], inp))
    if parsed["met"] and not counting:
        if kept:
            why = (
                "the judge quoted only the live screen, which is context for this agent, "
                "not evidence"
            )
        elif _only_too_short(parsed["evidence"], inp):
            why = TOO_SHORT_REASON
        else:
            why = "the judge quoted nothing it read"
        await missions.run_admitted(
            lambda: _unknown(mission_id, row, fp, transient=False, reason=why)
        )
        return "unknown"
    from . import gitwrite

    # THE THRESHOLD IS READ AFTER THE CALL, at the write boundary — a setting changed while the
    # model was thinking applies to this judgment, not to the next one. `missions` never reads it.
    threshold = float(prefs.get_mission_orchestration()["judge_confidence_min"])
    judged = {
        "met": parsed["met"],
        "confidence": parsed["confidence"],
        "threshold": threshold,
        "evidence": kept,
        "fingerprint": fp,
        "checked_at": time.time(),
        # The criterion this verdict answers. The store refuses to settle it against any other,
        # and completion never counts it once the row asks something else.
        "criterion": missions.judge_criterion(row.get("title"), row.get("direction")),
    }
    reason = gitwrite.redact(parsed["reason"])[:REASON_MAX]

    def _write():
        try:
            return missions.observe_objective(
                mission_id,
                row["key"],
                observed=True,
                value=False,  # the store computes it from the verdict
                detail=reason,
                judged=judged,
                expect_probe="supervisor_judged",
                expect_incarnation=str(row.get("incarnation") or ""),
                # THE QUESTION THE JUDGE WAS ASKED. A retitle keeps the incarnation, so without this
                # a verdict about the old title settles the new one (#1088 review, blocker 1).
                expect_title=str(row.get("title") or ""),
            )
        except missions.MissionError as e:
            if e.status != 422:
                raise
            # Refused by the store's own measure even after shedding quotes and shortening the
            # reason: recorded as unknown, never as met and never as a crash.
            _unknown(
                mission_id, row, fp, transient=False, reason="the judgment was too large to store"
            )
            return "too_large"

    written = await missions.run_admitted(_write)
    if written == "too_large":
        return "unknown"
    if written is None:
        # The row was dropped, re-added or RETITLED while the model was thinking: the verdict is
        # about a question the row no longer asks. Discarded, never written; the next pass judges
        # the row as it is now (its fingerprint includes the title, so it is due).
        log.info("mission %s: judgment of %s superseded while in flight", mission_id, row["key"])
        # …but the ATTEMPT is recorded, so this row's attempt time advances like any other: the
        # judge order is least-recently-served first, and an attempt that left no trace would keep
        # a mission at the front for ever (#1097, Hermes 5115). Fenced on the incarnation, so a
        # dropped or re-added row is left alone; the fingerprint it names is the old criterion's,
        # so the row stays due for its new one.
        await missions.run_admitted(
            lambda: _unknown(
                mission_id,
                row,
                fp,
                transient=True,
                reason="the objective changed while it was being judged",
            )
        )
        return "superseded"
    obs = written.get("observed") or {}
    if obs.get("value"):
        # REVALIDATE AFTER FITTING (#1097 review 5040, finding 3): a met verdict must still carry a
        # quote that counts on its own. If fitting left only context, it is unknown, not met.
        stored = (obs.get("judged") or {}).get("evidence") or []
        if not any(_counts(str(q.get("source") or ""), inp) for q in stored):
            await missions.run_admitted(
                lambda: _unknown(
                    mission_id,
                    row,
                    fp,
                    transient=False,
                    reason="the judgment's evidence did not fit the store",
                )
            )
            return "unknown"
    return "met" if obs.get("value") else "not_met"


async def mark_mission(mission_id: str, *, path=None) -> dict:
    """PHASE 1 (supervision): mark stale every judgment whose input or criterion moved, re-apply a
    changed threshold, forget a rejected fingerprint the output has left behind. Writes, never a
    model call. The completion question is asked after this, so it only ever sees verdicts that
    were just revalidated against the current input."""
    report: dict = {"stale": 0, "recomputed": 0, "cleared": 0, "judged": {}}
    wl = await missions.run_admitted(lambda: missions.judge_worklist(mission_id, path=path))
    if not wl or not wl["rows"] or wl["archived"]:
        return report
    inp = await missions.run_admitted(lambda: gather(mission_id, wl.get("cwd")))
    threshold = float(prefs.get_mission_orchestration()["judge_confidence_min"])
    report.update(
        await missions.run_admitted(lambda: mark_stale_judgments(mission_id, wl, inp, threshold))
    )
    _remember_phase1_input(mission_id, inp)
    return report


#: How long phase 1's input stays usable to phase 2 (the same sweep, or an early reading's pass).
PHASE1_INPUT_TTL_S = 120.0
_phase1_inputs: dict[str, tuple[float, JudgeInput]] = {}


def _remember_phase1_input(mission_id: str, inp: JudgeInput) -> None:
    now = time.monotonic()
    for mid in [m for m, (at, _) in _phase1_inputs.items() if now - at > PHASE1_INPUT_TTL_S]:
        _phase1_inputs.pop(mid, None)
    _phase1_inputs[mission_id] = (now, inp)


def _take_phase1_input(mission_id: str) -> JudgeInput | None:
    got = _phase1_inputs.pop(mission_id, None)
    if got is None or time.monotonic() - got[0] > PHASE1_INPUT_TTL_S:
        return None
    return got[1]


async def judge_batch(mission_ids: list[str], budget: Budget, *, path=None) -> dict:
    """PHASE 2 (judging), after the whole batch has been supervised. Walks the batch's running
    missions least recently served first and judges what is due until the budget is spent. A
    mission is charged only for the model calls it makes. Never raises for one mission."""
    order = await missions.run_admitted(lambda: rank_missions(mission_ids, path=path))
    out: dict = {}
    for mid in order:
        if budget.left <= 0:
            break
        try:
            out[mid] = await judge_one(mid, budget, path=path)
        except Exception as e:  # noqa: BLE001 — one mission's judge must not stop the rest
            log.warning("mission %s: judging failed: %s", mid, e)
    return out


async def run_for_mission(
    mission_id: str, *, state: str, budget: Budget | None = None, path=None
) -> dict:
    """Both phases for ONE mission (early readings and tests): mark, then — only while `running` —
    judge within `budget`."""
    report = await mark_mission(mission_id, path=path)
    if state == "running":
        judged = await judge_one(mission_id, budget or Budget(), path=path)
        report["judged"] = judged.get("judged", {})
        for k in ("calls", "input_moved", "idle_attempts"):
            if k in judged:
                report[k] = judged[k]
        report["stale"] += judged.get("stale", 0)
    return report


async def judge_one(mission_id: str, budget: Budget, *, path=None) -> dict:
    """Judge one mission's due rows within `budget`. Charged per model call made, nothing else."""
    report: dict = {"stale": 0, "judged": {}}
    wl = await missions.run_admitted(lambda: missions.judge_worklist(mission_id, path=path))
    if not wl or not wl["rows"] or wl["archived"] or wl["state"] != "running":
        return report
    # ONE READ PER SWEEP FOR A MISSION WITH NOTHING DUE (#1097 round 10). Phase 1 has just read
    # this mission's input; if nothing is due against it, phase 2 skips the second transcript/git
    # read. Anything due is judged against a FRESH read below, never the remembered one, and output
    # that arrived in between is caught by the next sweep's phase 1 before any completion question.
    prior = _take_phase1_input(mission_id)
    if prior is not None and not prior.empty:
        t0 = time.time()
        if not any(needs_judgment(r, row_fingerprint(prior, r), now=t0) for r in wl["rows"]):
            report["skipped"] = "nothing due"
            return report
    inp = await missions.run_admitted(lambda: gather(mission_id, wl.get("cwd")))
    threshold = float(prefs.get_mission_orchestration()["judge_confidence_min"])
    # Marked again: the input may have moved since the supervision phase read it.
    marks = await missions.run_admitted(
        lambda: mark_stale_judgments(mission_id, wl, inp, threshold)
    )
    report["stale"] += marks["stale"]
    if inp.empty:
        # NOTHING TO READ: no session output and no checkout changes. It costs nothing, but the
        # attempt is recorded so the mission moves back in the order rather than being re-read
        # first on every sweep (#1097 rounds 8-9). An unknown, never a verdict.
        report["idle_attempts"] = await _record_idle(
            mission_id, inp, reason=NOTHING_TO_READ_REASON, path=path
        )
        return report
    wl = await missions.run_admitted(lambda: missions.judge_worklist(mission_id, path=path))
    if not wl:
        return report
    now = time.time()
    due = [r for r in wl["rows"] if needs_judgment(r, row_fingerprint(inp, r), now=now)]
    # GATES FIRST, THEN THE LEAST RECENTLY JUDGED (#1097 review 5040, finding 4).
    due.sort(key=lambda r: (not r.get("gate"), last_attempt(r), int(r.get("ord") or 0)))
    if not due:
        return report
    try:
        review._require_config()
    except review.NotConfiguredError:
        # Nothing to call, so nothing is spent: each row says why it cannot be judged, and the
        # transient backoff keeps that from being rewritten every pass.
        for r in due:
            fp = row_fingerprint(inp, r)
            await missions.run_admitted(
                lambda r=r, fp=fp: _unknown(
                    mission_id, r, fp, transient=True, reason=NO_ENDPOINT_REASON
                )
            )
            report["judged"][r["key"]] = "unknown"
        return report
    calls = 0
    for r in due:
        if calls >= JUDGE_CALLS_PER_MISSION or not budget.take(mission_id):
            break
        calls += 1
        try:
            report["judged"][r["key"]] = await judge_row(mission_id, r, inp, wl["instruction"])
        except Exception as e:  # noqa: BLE001 — one row's failure must not stop the pass
            log.warning("mission %s: judging %s failed: %s", mission_id, r["key"], e)
            report["judged"][r["key"]] = "error"
            # THE ATTEMPT STILL COUNTS (#1097 round 8): best-effort, so a persistent failure after
            # the call cannot keep this row at the front of the order. If the store is what
            # failed, this write may fail too — then it is simply skipped.
            with contextlib.suppress(Exception):
                fp_r = row_fingerprint(inp, r)
                await missions.run_admitted(
                    lambda r=r, fp_r=fp_r: _unknown(
                        mission_id,
                        r,
                        fp_r,
                        transient=True,
                        reason="the judgment could not be stored",
                    )
                )
    report["calls"] = calls
    if calls:
        # THE OUTPUT MAY HAVE MOVED WHILE THE MODEL WAS READING IT (#1097 review 5040, finding 2).
        # Re-read the input after the calls: if it changed, every verdict bound to the old input —
        # including those just written — is marked stale at once, so none sits as supporting until
        # the next sweep's supervision phase would have caught it.
        fresh = await missions.run_admitted(lambda: gather(mission_id, wl.get("cwd")))
        if fresh.fingerprint != inp.fingerprint:
            report["input_moved"] = True
            wl2 = await missions.run_admitted(
                lambda: missions.judge_worklist(mission_id, path=path)
            )
            if wl2:
                marks2 = await missions.run_admitted(
                    lambda: mark_stale_judgments(mission_id, wl2, fresh, threshold)
                )
                report["stale"] += marks2["stale"]
    return report


NOTHING_TO_READ_REASON = (
    "nothing to read — the mission's sessions have no output and its checkout has no changes"
)


async def _record_idle(mission_id: str, inp: JudgeInput, *, reason: str, path=None) -> int:
    """Record a transient attempt on every due-looking row of a mission that could not be judged."""
    wl = await missions.run_admitted(lambda: missions.judge_worklist(mission_id, path=path))
    n = 0
    for r in (wl or {}).get("rows") or []:
        if not is_open(r):
            continue
        fp = row_fingerprint(inp, r)
        await missions.run_admitted(
            lambda r=r, fp=fp: _unknown(mission_id, r, fp, transient=True, reason=reason)
        )
        n += 1
    return n


def last_attempt(row: dict) -> float:
    """When this row was last JUDGED or attempted (0 = never), for fairness within a tier. A stale
    mark carries the verdict's own `checked_at`, so marking does not look like judging."""
    obs = row.get("observed") if isinstance(row.get("observed"), dict) else {}
    rec = obs.get("judged") if isinstance(obs.get("judged"), dict) else {}
    for v in (rec.get("checked_at"), obs.get("at") if "attempted_fp" in rec else None):
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
    return 0.0


__all__ = [
    "JUDGE_CALLS_PER_MISSION",
    "JUDGE_CALLS_PER_SWEEP",
    "TRANSIENT_BACKOFF_S",
    "Budget",
    "ContractError",
    "JudgeInput",
    "gather",
    "judge_row",
    "mark_stale_judgments",
    "needs_judgment",
    "parse_reply",
    "row_fingerprint",
    "run_for_mission",
    "verify_evidence",
]
