"""The `changelog/1` document: one validator for CI and the server (#2013).

The changelog lives as `changelog.json` on the orphan `changelog` branch. CI writes it
(`scripts/changelog/`), and the server reads it (#2013 Phase 2). Both run THIS module, so the
rules cannot drift apart:

* **CI is strict.** `validate_doc` raises on the first problem, and a run that raises publishes
  nothing. A document that is already invalid is never rewritten.
* **The server drops what it can't trust.** `parse_doc` keeps the valid records and reports the
  rest by rule and field. A record is kept whole or dropped, never partly rendered.

Every text field is plain, single-line text with a length cap. The model never controls a
location: a release slide names an `illustration` and an `action` from fixed enums, and trusted
code resolves them to an asset and a route. Unknown fields are rejected everywhere.

An error carries a rule id and a field path, never the offending value. The generated text may
be exactly what must not reach a log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

SCHEMA = "changelog/1"

CATEGORIES = ("new", "improved", "fixed", "internal")
# The illustration library (#2013 Phase 4): base names under `web/public/whatsnew/`.
ILLUSTRATIONS = ("missions", "files", "editing", "templates")
# Slide actions the SPA resolves to routes with a fixed map. The model picks one; it never
# writes a path or a URL.
ACTIONS = ("none", "open_settings_updates", "open_missions", "open_templates", "open_agents")

TITLE_MAX = 90
DETAIL_MAX = 240
MAX_ENTRIES = 2000
MAX_BYTES = 512 * 1024
SLIDES_MIN, SLIDES_MAX = 3, 7
SUMMARY_MAX_ITEMS, SUMMARY_ITEM_MAX = 8, 160
EYEBROW_MAX, SLIDE_TITLE_MAX, SLIDE_BODY_MAX = 40, 90, 400
POINTS_MAX, POINT_MAX = 5, 120

_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SLIDE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
# C0/C1 controls, line and paragraph separators, bidi overrides and zero-width characters: text
# that could reorder or hide what the operator reads.
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩﻿]")


@dataclass
class ChangelogError(Exception):
    """`rule` says what failed, `field` where. Neither ever holds the offending text."""

    rule: str
    field: str

    def __str__(self) -> str:
        return f"{self.rule} at {self.field}"


def _fail(rule: str, field: str) -> None:
    raise ChangelogError(rule, field)


def _keys(obj: Any, field: str, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(obj, dict):
        _fail("not-an-object", field)
    missing = required - obj.keys()
    if missing:
        _fail("missing-field", f"{field}.{sorted(missing)[0]}")
    if obj.keys() - required - optional:
        # The containing field only: an unknown key's NAME is untrusted text (a model can put
        # anything there, a newline or a workflow command included), so it never reaches an error.
        _fail("unknown-field", field)


def plain_text(value: Any, field: str, max_len: int, *, allow_empty: bool = False) -> str:
    """A single line of plain text: no controls, no invisible characters, trimmed, capped."""
    if not isinstance(value, str):
        _fail("not-a-string", field)
    if _INVISIBLE.search(value):
        _fail("control-character", field)
    if value != value.strip():
        _fail("untrimmed", field)
    if not value and not allow_empty:
        _fail("empty", field)
    if len(value) > max_len:
        _fail("too-long", field)
    return value


def _enum(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        _fail("not-in-enum", field)
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _fail("not-a-sha", field)
    return value


def _int(value: Any, field: str, minimum: int = 1) -> int:
    # bool is an int in Python; `true` is not a sequence number.
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail("not-a-positive-int", field)
    return value


def _list(value: Any, field: str, max_items: int, min_items: int = 0) -> list:
    if not isinstance(value, list):
        _fail("not-a-list", field)
    if len(value) > max_items:
        _fail("too-many", field)
    if len(value) < min_items:
        _fail("too-few", field)
    return value


def validate_entry_text(obj: Any, field: str = "entry") -> dict:
    """What the model writes for one merge: exactly `category`, `title`, `detail`."""
    _keys(obj, field, {"category", "title", "detail"})
    _enum(obj["category"], f"{field}.category", CATEGORIES)
    plain_text(obj["title"], f"{field}.title", TITLE_MAX)
    if obj["detail"] is not None:
        plain_text(obj["detail"], f"{field}.detail", DETAIL_MAX)
    return obj


def validate_entry(obj: Any, field: str = "entry") -> dict:
    _keys(
        obj,
        field,
        {"seq", "merged_at", "pr", "forgejo_sha", "mirror_sha", "category", "title", "detail"},
    )
    _int(obj["seq"], f"{field}.seq")
    if not isinstance(obj["merged_at"], str) or not _STAMP.fullmatch(obj["merged_at"]):
        _fail("not-a-timestamp", f"{field}.merged_at")
    if obj["pr"] is not None:
        _int(obj["pr"], f"{field}.pr")
    _sha(obj["forgejo_sha"], f"{field}.forgejo_sha")
    if obj["mirror_sha"] is not None:
        _sha(obj["mirror_sha"], f"{field}.mirror_sha")
    validate_entry_text({k: obj[k] for k in ("category", "title", "detail")}, field)
    return obj


def validate_slide(obj: Any, field: str) -> dict:
    _keys(obj, field, {"id", "eyebrow", "title", "body", "action"}, {"points", "illustration"})
    if not isinstance(obj["id"], str) or not _SLIDE_ID.fullmatch(obj["id"]):
        _fail("bad-slide-id", f"{field}.id")
    plain_text(obj["eyebrow"], f"{field}.eyebrow", EYEBROW_MAX)
    plain_text(obj["title"], f"{field}.title", SLIDE_TITLE_MAX)
    plain_text(obj["body"], f"{field}.body", SLIDE_BODY_MAX, allow_empty=True)
    _enum(obj["action"], f"{field}.action", ACTIONS)
    if "points" in obj:
        for i, p in enumerate(_list(obj["points"], f"{field}.points", POINTS_MAX)):
            plain_text(p, f"{field}.points[{i}]", POINT_MAX)
    if obj.get("illustration") is not None:
        _enum(obj["illustration"], f"{field}.illustration", ILLUSTRATIONS)
    return obj


def validate_release(obj: Any, field: str = "release") -> dict:
    _keys(obj, field, {"version", "tag_sha", "date", "summary", "slides"})
    if not isinstance(obj["version"], str) or not _VERSION.fullmatch(obj["version"]):
        _fail("not-a-version", f"{field}.version")
    _sha(obj["tag_sha"], f"{field}.tag_sha")
    if not isinstance(obj["date"], str) or not _DATE.fullmatch(obj["date"]):
        _fail("not-a-date", f"{field}.date")
    for i, s in enumerate(_list(obj["summary"], f"{field}.summary", SUMMARY_MAX_ITEMS, 1)):
        plain_text(s, f"{field}.summary[{i}]", SUMMARY_ITEM_MAX)
    slides = _list(obj["slides"], f"{field}.slides", SLIDES_MAX, SLIDES_MIN)
    ids: set[str] = set()
    for i, s in enumerate(slides):
        validate_slide(s, f"{field}.slides[{i}]")
        if s["id"] in ids:
            _fail("duplicate-slide-id", f"{field}.slides[{i}].id")
        ids.add(s["id"])
    return obj


def _validate_build(obj: Any, field: str, mirror: bool) -> dict:
    required = {"forgejo_tip", "covers"} | ({"mirror_sha"} if mirror else set())
    _keys(obj, field, required)
    _sha(obj["forgejo_tip"], f"{field}.forgejo_tip")
    if mirror:
        _sha(obj["mirror_sha"], f"{field}.mirror_sha")
    for i, c in enumerate(_list(obj["covers"], f"{field}.covers", MAX_ENTRIES)):
        _sha(c, f"{field}.covers[{i}]")
    return obj


def validate_builds(obj: Any, field: str = "builds") -> dict:
    _keys(obj, field, {"forgejo", "mirror"})
    for chain, mirror in (("forgejo", False), ("mirror", True)):
        for i, b in enumerate(_list(obj[chain], f"{field}.{chain}", 10 * MAX_ENTRIES)):
            _validate_build(b, f"{field}.{chain}[{i}]", mirror)
    return obj


def _top(doc: Any) -> None:
    _keys(doc, "doc", {"schema", "entries", "releases", "builds"})
    if doc["schema"] != SCHEMA:
        _fail("wrong-schema", "doc.schema")
    _list(doc["entries"], "doc.entries", MAX_ENTRIES)
    _list(doc["releases"], "doc.releases", MAX_ENTRIES)


def validate_doc(doc: Any) -> dict:
    """Strict: raise on the first problem. CI publishes only a document that passes."""
    _top(doc)
    seen: set[str] = set()
    for i, e in enumerate(doc["entries"]):
        validate_entry(e, f"doc.entries[{i}]")
        if e["forgejo_sha"] in seen:
            _fail("duplicate-entry", f"doc.entries[{i}].forgejo_sha")
        seen.add(e["forgejo_sha"])
    versions: set[str] = set()
    for i, r in enumerate(doc["releases"]):
        validate_release(r, f"doc.releases[{i}]")
        if r["version"] in versions:
            _fail("duplicate-release", f"doc.releases[{i}].version")
        versions.add(r["version"])
    validate_builds(doc["builds"], "doc.builds")
    return doc


def empty_doc() -> dict:
    return {
        "schema": SCHEMA,
        "entries": [],
        "releases": [],
        "builds": {"forgejo": [], "mirror": []},
    }


def parse_doc(raw: bytes) -> tuple[dict | None, list[ChangelogError]]:
    """Lenient, for a reader of the (mutable) branch: keep valid records, drop the rest.

    Returns `(None, [why])` when the document as a whole can't be read: too large, not JSON, the
    wrong schema, or bad top-level shape. Otherwise every invalid or duplicate record is dropped
    whole and reported.
    """
    if len(raw) > MAX_BYTES:
        return None, [ChangelogError("too-large", "doc")]
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, [ChangelogError("not-json", "doc")]
    try:
        _top(doc)
    except ChangelogError as e:
        return None, [e]
    dropped: list[ChangelogError] = []
    entries, seen = [], set()
    for i, e in enumerate(doc["entries"]):
        try:
            validate_entry(e, f"doc.entries[{i}]")
            if e["forgejo_sha"] in seen:
                _fail("duplicate-entry", f"doc.entries[{i}].forgejo_sha")
        except ChangelogError as err:
            dropped.append(err)
            continue
        seen.add(e["forgejo_sha"])
        entries.append(e)
    releases, versions = [], set()
    for i, r in enumerate(doc["releases"]):
        try:
            validate_release(r, f"doc.releases[{i}]")
            if r["version"] in versions:
                _fail("duplicate-release", f"doc.releases[{i}].version")
        except ChangelogError as err:
            dropped.append(err)
            continue
        versions.add(r["version"])
        releases.append(r)
    try:
        builds = validate_builds(doc["builds"], "doc.builds")
    except ChangelogError as err:
        # Without the coverage map no range can be computed; the entries still list.
        dropped.append(err)
        builds = {"forgejo": [], "mirror": []}
    return {"schema": SCHEMA, "entries": entries, "releases": releases, "builds": builds}, dropped


def dumps(doc: dict) -> str:
    """The one serialisation CI commits: compact, with a stable key order, so a rerun is
    byte-identical. `encoded_size` of it is what `parse_doc` holds to `MAX_BYTES`."""
    return json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"


def encoded_size(doc: dict) -> int:
    return len(dumps(doc).encode("utf-8"))
