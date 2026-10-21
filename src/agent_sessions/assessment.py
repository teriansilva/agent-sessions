"""The structured session assessment (#1020): a versioned, bounded record of where a session stands.

The one-line ``ai_summary`` is a preview and the chronological ``ai_recap`` is background. Neither
was meant to carry a decision, but the standalone decision pass read them as if they did: it
fell back to the recap's first 300 characters when there was no summary, and derived "current
state" from whichever line of the recap happened to come last. A session whose recap opened with
a long history, or whose operator changed direction halfway through, reached the decision pass
as its opening chapter.

This module is the contract that replaces that. One record per session, written by the review
pass that already runs (the tail review's reply carries it — no extra model call), stored on the
session's metadata row, and read back through :func:`project`.

**Fields.** ``task``, ``constraints``, ``current_state``, ``completed``, ``remaining``,
``blocker``, ``decision_needed`` and ``evidence_refs`` come from the model; ``source_fingerprint``,
``source_read_at``, ``latest_source_at``, ``assessed_at``, ``coverage`` and ``schema_version`` are
the server's. A field that is missing means UNKNOWN, never "nothing to report": ``blocker: None``
on a record says the reviewer saw no blocker, and no record at all says nothing.

**Bounded by construction.** Every text field and list has a cap, and :data:`TOTAL_MAX` is their
sum, so the stored record can never exceed it whatever the model sends. A field cut to fit gets a
trailing ``…`` AND its name in ``coverage.truncated_fields`` — a shortened blocker or decision is
never silent. The decision-bearing fields (``current_state``, ``blocker``, ``decision_needed``)
are scalars with their own caps, so no amount of list content can crowd them out.

**Provenance.** Everything here is a model's reading of agent output: CLAIMS, not verified facts.
The record says so (``provenance``), and nothing in this module settles an objective or
authorises a send. Evidence quotes are kept only when they occur verbatim (case- and
whitespace-insensitive) in the section they name; a quote that cannot be found is dropped and
counted. A reference names a section of the reviewed input and that input's fingerprint — never
a URL or a path, and nothing ever fetches one.

**Freshness is per source.** ``assessed_at`` is when the model answered; it says nothing about
whether the session has moved since. :func:`project` therefore compares the record against the
session's own clock (activity after ``source_read_at``), against the latest successful review
(``source_fingerprint`` vs ``review_fingerprint``), and against a failed refresh recorded after
it. Any of them makes the record ``stale``: still shown — the last good context is not erased —
but never labelled current.

Pure: no I/O, no imports from the rest of the package, so the metadata store can use it to
validate both writes and reads.
"""

from __future__ import annotations

import math

SCHEMA_VERSION = 1

TASK_MAX = 300
CONSTRAINT_MAX = 200
CONSTRAINTS_MAX_ITEMS = 3
CURRENT_STATE_MAX = 300
ITEM_MAX = 160
ITEMS_MAX = 3
BLOCKER_MAX = 400
DECISION_MAX = 400
QUOTE_MAX = 200
REFS_MAX = 3

#: The sections of the reviewed input a quote may name. The unsent compose draft is deliberately
#: absent: it is not something the session did, so it cannot be evidence of where it stands.
REF_SOURCES: tuple[str, ...] = ("transcript", "screen", "operator")

#: Upper bound on the model-authored text a record can hold, in characters. The sum of the caps.
TOTAL_MAX = (
    TASK_MAX
    + CONSTRAINT_MAX * CONSTRAINTS_MAX_ITEMS
    + CURRENT_STATE_MAX
    + 2 * ITEM_MAX * ITEMS_MAX
    + BLOCKER_MAX
    + DECISION_MAX
    + QUOTE_MAX * REFS_MAX
)

#: What every record is: a reviewer's reading of the session's own output.
PROVENANCE = "reviewer_reading_of_session_output"

ELLIPSIS = "…"

#: The coverage vocabulary. Anything else read back from a store is dropped.
_COVERAGE_ENUMS: dict[str, frozenset[str]] = {
    "transcript": frozenset({"complete", "tail", "none"}),
    "operator_messages": frozenset({"complete", "partial", "none"}),
}
_COVERAGE_BOOLS: tuple[str, ...] = ("screen", "draft")

_TEXT_FIELDS: dict[str, int] = {
    "task": TASK_MAX,
    "current_state": CURRENT_STATE_MAX,
    "blocker": BLOCKER_MAX,
    "decision_needed": DECISION_MAX,
}
_LIST_FIELDS: dict[str, tuple[int, int]] = {
    "constraints": (CONSTRAINT_MAX, CONSTRAINTS_MAX_ITEMS),
    "completed": (ITEM_MAX, ITEMS_MAX),
    "remaining": (ITEM_MAX, ITEMS_MAX),
}
FIELDS: tuple[str, ...] = (
    "task",
    "constraints",
    "current_state",
    "completed",
    "remaining",
    "blocker",
    "decision_needed",
    "evidence_refs",
)

# A model asked for `null` sometimes writes the word instead — or a short negation of the field
# ("No blocker.", "None needed"). Matched WHOLE after trailing punctuation is stripped, so
# "None." is unknown while "None of the tests pass" is a real statement (#1020 review finding 6).
_NULL_WORDS = frozenset(
    {
        "none",
        "null",
        "n/a",
        "na",
        "nothing",
        "-",
        "no",
        "nil",
        "not applicable",
        "none needed",
        "none required",
        "nothing needed",
        "nothing required",
        "no blocker",
        "no blockers",
        "no decision",
        "no decision needed",
        "no decision required",
        "no decisions needed",
        "unknown",
    }
)
_NULL_TRIM = " .!,;:)(\"'`"

# The shortest quote that is EVIDENCE: a fragment like "a" or "CI" occurs in almost any section,
# so it "verifies" while proving nothing. At least this many characters OR words.
QUOTE_MIN_CHARS = 12
QUOTE_MIN_WORDS = 3


def _is_null_word(s: str) -> bool:
    return " ".join(s.lower().strip(_NULL_TRIM).split()) in _NULL_WORDS


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def _text(value: object, cap: int, name: str, truncated: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    if not s or _is_null_word(s):
        return None
    if len(s) > cap:
        if name not in truncated:
            truncated.append(name)
        s = s[: cap - 1].rstrip() + ELLIPSIS
    return s


def _items(value: object, cap: int, max_items: int, name: str, truncated: list[str]) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [t for t in (_text(v, cap, name, truncated) for v in value) if t]
    if len(out) > max_items:
        if name not in truncated:
            truncated.append(name)
        out = out[:max_items]
    return out


def _refs(value: object, sources: dict[str, str | list[str]] | None) -> tuple[list[dict], int]:
    """Evidence quotes kept only when found in the section they name. ``sources=None`` (a record
    read back from the store) re-checks shape and bounds only — the sources are long gone."""
    if not isinstance(value, list):
        return [], 0
    flat_sources = {
        k: [_flat(part) for part in ([v] if isinstance(v, str) else v) if isinstance(part, str)]
        for k, v in (sources or {}).items()
        if isinstance(v, str | list)
    }
    kept: list[dict] = []
    dropped = 0
    for raw in value:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        source, quote = raw.get("source"), raw.get("quote")
        if source not in REF_SOURCES or not isinstance(quote, str):
            dropped += 1
            continue
        q = " ".join(quote.split())
        if not q or len(q) > QUOTE_MAX:
            # Never shortened: a cut quote is a different quote, and it would still "verify".
            dropped += 1
            continue
        if len(q) < QUOTE_MIN_CHARS and len(q.split()) < QUOTE_MIN_WORDS:
            dropped += 1  # too short to point at anything in particular
            continue
        if sources is not None and not any(
            _flat(q) in part for part in flat_sources.get(source, [])
        ):
            dropped += 1
            continue
        if len(kept) < REFS_MAX:
            kept.append({"source": source, "quote": q})
        else:
            dropped += 1
    return kept, dropped


def normalize(
    raw: object, sources: dict[str, str | list[str]] | None = None
) -> tuple[dict, dict] | None:
    """Bound a model's ``assessment`` object. Returns ``(fields, notes)`` or ``None``.

    ``None`` when ``raw`` is not an object or names no ``current_state`` — a record without a
    current state cannot answer the one question it exists for, and storing it would look like
    an answer. ``notes`` carries ``truncated_fields`` and ``refs_dropped`` for the coverage block.
    """
    if not isinstance(raw, dict):
        return None
    truncated: list[str] = []
    fields: dict = {}
    for name, cap in _TEXT_FIELDS.items():
        fields[name] = _text(raw.get(name), cap, name, truncated)
    if not fields["current_state"]:
        return None
    for name, (cap, n) in _LIST_FIELDS.items():
        fields[name] = _items(raw.get(name), cap, n, name, truncated)
    fields["evidence_refs"], dropped = _refs(raw.get("evidence_refs"), sources)
    return {k: fields[k] for k in FIELDS}, {"truncated_fields": truncated, "refs_dropped": dropped}


def finite_number(value: object) -> float | None:
    """A finite JSON number, or unknown; even a valid JSON integer can overflow float()."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _coverage(raw: object) -> dict:
    """Keep only the known coverage keys, each in its own vocabulary."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for k, allowed in _COVERAGE_ENUMS.items():
        if isinstance(raw.get(k), str) and raw[k] in allowed:
            out[k] = raw[k]
    for k in _COVERAGE_BOOLS:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    tf = raw.get("truncated_fields")
    if isinstance(tf, list):
        out["truncated_fields"] = [f for f in FIELDS if f in tf]
    rd = raw.get("refs_dropped")
    if isinstance(rd, int) and not isinstance(rd, bool) and rd >= 0:
        out["refs_dropped"] = min(rd, 1000)
    return out


def record(
    fields: dict,
    notes: dict,
    *,
    source_fingerprint: str,
    source_read_at: float,
    latest_source_at: float | None,
    assessed_at: float,
    coverage: dict | None = None,
) -> dict:
    """The stored form: the model's bounded fields plus the server's provenance and coverage."""
    return {
        "schema_version": SCHEMA_VERSION,
        **{k: fields.get(k) for k in FIELDS},
        "source_fingerprint": str(source_fingerprint),
        "source_read_at": float(source_read_at),
        "latest_source_at": finite_number(latest_source_at),
        "assessed_at": float(assessed_at),
        "coverage": _coverage({**(coverage or {}), **notes}),
        "provenance": PROVENANCE,
    }


def from_stored(value: object) -> dict | None:
    """Read-side validation: a stored record re-bounded, or ``None`` when it is not one.

    A row hand-edited past the caps, written by a future schema, or simply damaged reads as "no
    assessment" (unknown) — never as a record with an unbounded or misshapen field. Truncation
    notes found here are added to the coverage, so a clamp on read is no more silent than one on
    write.
    """
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    fp = value.get("source_fingerprint")
    read_at = finite_number(value.get("source_read_at"))
    assessed_at = finite_number(value.get("assessed_at"))
    if not isinstance(fp, str) or not fp or read_at is None or assessed_at is None:
        return None
    norm = normalize(value)
    if norm is None:
        return None
    fields, notes = norm
    coverage = _coverage(value.get("coverage"))
    extra = [f for f in notes["truncated_fields"] if f not in coverage.get("truncated_fields", [])]
    if extra:
        coverage["truncated_fields"] = [
            f for f in FIELDS if f in coverage.get("truncated_fields", []) + extra
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        **fields,
        "source_fingerprint": fp[:128],
        "source_read_at": read_at,
        "latest_source_at": finite_number(value.get("latest_source_at")),
        "assessed_at": assessed_at,
        "coverage": coverage,
        "provenance": PROVENANCE,
    }


def project(
    stored: object,
    *,
    review_fingerprint: str = "",
    last_activity: float | None = None,
    review_failed_at: float | None = None,
) -> dict:
    """The API projection of a session's assessment, with its freshness decided per source.

    ``status`` is ``missing`` (no record), ``current``, or ``stale`` with ``stale_reasons``:

    * ``session_activity_since_assessment`` — the session's own clock moved after the sources were
      read (this is also how a result that arrives after the session moved on is labelled);
    * ``newer_review_without_assessment`` — a later successful review produced no usable record,
      so this one describes an older input;
    * ``refresh_failed`` — a review attempted after this record failed.

    A stale record is returned whole. Erasing it would hide a blocker that may well still hold;
    labelling it current would claim what nobody checked.
    """
    rec = from_stored(stored)
    failed = finite_number(review_failed_at)
    if rec is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "missing",
            "stale_reasons": ["refresh_failed"] if failed is not None else [],
            "refresh_failed_at": failed,
        }
    reasons: list[str] = []
    activity = finite_number(last_activity)
    if activity is not None and activity > rec["source_read_at"]:
        reasons.append("session_activity_since_assessment")
    if review_fingerprint and review_fingerprint != rec["source_fingerprint"]:
        reasons.append("newer_review_without_assessment")
    if failed is not None and failed >= rec["assessed_at"]:
        reasons.append("refresh_failed")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "stale" if reasons else "current",
        "stale_reasons": reasons,
        **{k: rec[k] for k in FIELDS},
        "source": {
            "fingerprint": rec["source_fingerprint"],
            "read_at": rec["source_read_at"],
            "latest_source_at": rec["latest_source_at"],
        },
        "assessed_at": rec["assessed_at"],
        "coverage": rec["coverage"],
        "provenance": rec["provenance"],
        "refresh_failed_at": failed,
    }
