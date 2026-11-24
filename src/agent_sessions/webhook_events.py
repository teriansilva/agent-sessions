"""Which webhook events an automation acts on, and the typed values it takes from them (#2221 P2).

A webhook is provider-free (`webhooks.py`), so what a delivery *means* is the automation's
business. This module has two closed vocabularies, both checked at save and again on every
delivery:

**Conditions** (``trigger.when``): a list of ``{path, op, value?}``, all of which must hold. The
path is a field of the delivery's signed JSON (``pull_request.draft``, ``labels[].name``), never a
header: the signature covers the body only, so an event-name header could be swapped on a replayed
body (``forge_hook`` reads the event type from the body's shape for the same reason). The
operators are a closed set (:data:`OPS`). There are no expressions. A path through a list
(``[]``) holds when ANY element holds, and for ``is_not`` when none equals.

**Mapping** (``message.from_delivery``): ``{field: {path, type, choices?}}`` fills a template
field from the delivery, typed (:data:`TYPES`). A value that fails its type refuses the run:
nothing is partly filled. Only a template's own typed fields are filled this way; the payload
never chooses the folder, the session, the agent, the template or the permissions, and free text
never reaches a message (``text`` is short and from a closed alphabet).

**Presets** (:func:`presets`) are a convenience for the wizard: recognisers of common senders'
body shapes (Forgejo/Gitea first) that produce conditions. Choosing one WRITES its conditions
into the automation, and the consent covers those conditions, so a later change to this
catalogue can never widen an enabled automation.

Read ``docs/invariants/webhooks.md``.
"""

from __future__ import annotations

import math
import re

#: A condition or mapping path: dotted keys of the body, each optionally walked as a list (``[]``).
PATH_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}(\[\])?(\.[A-Za-z0-9_-]{1,64}(\[\])?){0,5}\Z")
PATH_MAX = 160
CONDITIONS_MAX = 8
CHOICES_MAX = 20
VALUE_MAX = 200
#: The closed set of condition operators, and whether each takes a value.
OPS = {
    "is": "one",
    "is_not": "one",
    "one_of": "list",
    "contains": "text",
    "is_true": None,
    "is_false": None,
    "exists": None,
    "missing": None,
}
#: The types a mapped value may have. ``text`` is short and from a closed alphabet: an id, a
#: branch, a title-like line — never a body.
TYPES = ("number", "choice", "text", "yes_no")
TEXT_MAX = 200
#: A mapped number's bound: an integer beyond the exactly-representable range is refused (JSON
#: integers are unbounded; a float conversion of a huge one overflows).
NUMBER_MAX = 2**53
TEXT_RE = re.compile(r"\A[A-Za-z0-9 _.,:;/#@()+\-]*\Z")
#: Template field names (the same rule as `automations._FIELD_RE`).
FIELD_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
MAPPED_MAX = 16


class EventError(ValueError):
    """A condition or mapping that does not validate. The message is the operator's."""


class ValueRejected(ValueError):
    """A delivery whose value for ``field`` failed its type. The reason never quotes the value."""

    def __init__(self, field: str, why: str) -> None:
        super().__init__(f"{field}: {why}")
        self.field = field
        self.why = why


# ---- validation (at save) -----------------------------------------------------------------------


def _path(raw: object, what: str) -> str:
    if not isinstance(raw, str) or len(raw) > PATH_MAX or not PATH_RE.match(raw):
        raise EventError(f"{what}: path must be a field of the delivery (like pull_request.draft)")
    return raw


def _scalar(raw: object, what: str) -> str | int | float | bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        if isinstance(raw, float) and not math.isfinite(raw):
            raise EventError(f"{what}: a number must be finite")
        if abs(raw) > NUMBER_MAX:
            raise EventError(f"{what}: a number must be within ±{NUMBER_MAX}")
        return raw
    if isinstance(raw, str) and len(raw) <= VALUE_MAX and "\n" not in raw:
        return raw
    raise EventError(f"{what}: a value is a short text, a number, or yes/no")


def validate_conditions(raw: object) -> list[dict]:
    """``trigger.when`` (STRICT). An empty list means every signed delivery."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > CONDITIONS_MAX:
        raise EventError(f"conditions must be a list of at most {CONDITIONS_MAX}")
    out: list[dict] = []
    for i, c in enumerate(raw, 1):
        what = f"condition {i}"
        if not isinstance(c, dict) or not {"path", "op"} <= set(c) <= {"path", "op", "value"}:
            raise EventError(f"{what} is {{path, op, value?}}")
        path = _path(c["path"], what)
        op = c["op"]
        if not isinstance(op, str) or op not in OPS:
            raise EventError(f"{what}: op must be one of {sorted(OPS)}")
        takes = OPS[op]
        if takes is None:
            if "value" in c:
                raise EventError(f"{what}: {op} takes no value")
            out.append({"path": path, "op": op})
            continue
        if "value" not in c:
            raise EventError(f"{what}: {op} needs a value")
        if takes == "list":
            vals = c["value"]
            if not isinstance(vals, list) or not 1 <= len(vals) <= CHOICES_MAX:
                raise EventError(f"{what}: one_of takes a list of 1 to {CHOICES_MAX} values")
            value: object = [_scalar(v, what) for v in vals]
        elif takes == "text":
            value = _scalar(c["value"], what)
            if not isinstance(value, str) or not value:
                raise EventError(f"{what}: contains takes a text")
        else:
            value = _scalar(c["value"], what)
        out.append({"path": path, "op": op, "value": value})
    return out


def validate_mapping(raw: object) -> dict[str, dict]:
    """``message.from_delivery`` (STRICT): ``{field: {path, type, choices?}}``, sorted."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAPPED_MAX:
        raise EventError(f"from_delivery is an object of at most {MAPPED_MAX} fields")
    out: dict[str, dict] = {}
    for field, m in raw.items():
        if not isinstance(field, str) or not FIELD_RE.match(field):
            raise EventError(f"from_delivery: {field!r} is not a field name")
        what = f"from_delivery {field}"
        if not isinstance(m, dict) or not {"path", "type"} <= set(m) <= {"path", "type", "choices"}:
            raise EventError(f"{what} is {{path, type, choices?}}")
        path = _path(m["path"], what)
        if "[]" in path:
            raise EventError(f"{what}: a value comes from one field, not a list")
        typ = m["type"]
        if not isinstance(typ, str) or typ not in TYPES:
            raise EventError(f"{what}: type must be one of {list(TYPES)}")
        entry: dict = {"path": path, "type": typ}
        if typ == "choice":
            ch = m.get("choices")
            if not isinstance(ch, list) or not 1 <= len(ch) <= CHOICES_MAX:
                raise EventError(f"{what}: a choice lists 1 to {CHOICES_MAX} allowed values")
            clean = []
            for v in ch:
                if not isinstance(v, str) or not v or len(v) > TEXT_MAX or not TEXT_RE.match(v):
                    raise EventError(f"{what}: each choice is a short plain text")
                clean.append(v)
            entry["choices"] = sorted(set(clean))
        elif "choices" in m:
            raise EventError(f"{what}: only a choice has choices")
        out[field] = entry
    return dict(sorted(out.items()))


def mapping_lines(mapping: dict[str, dict]) -> list[str]:
    """The mapping as comparable strings (the consent scope's ``mapped`` ladder): a new or changed
    entry widens, a removed one narrows."""
    out = []
    for field, m in sorted(mapping.items()):
        choices = ",".join(m.get("choices") or [])
        out.append(f"{field}<-{m['path']}:{m['type']}" + (f"[{choices}]" if choices else ""))
    return out


# ---- a delivery ---------------------------------------------------------------------------------


def resolve(payload: object, path: str) -> list:
    """Every value ``path`` reaches in ``payload`` (a list walks all its elements). Missing → []."""
    current = [payload]
    for seg in path.split("."):
        many = seg.endswith("[]")
        key = seg[:-2] if many else seg
        nxt = []
        for v in current:
            if not isinstance(v, dict) or key not in v:
                continue
            got = v[key]
            if many:
                if isinstance(got, list):
                    nxt.extend(got)
            else:
                nxt.append(got)
        current = nxt
    return current


def _equal(a: object, b: object) -> bool:
    # A number never equals a yes/no (Python's True == 1): types must agree.
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _holds(c: dict, values: list) -> bool:
    op = c["op"]
    if op == "exists":
        return any(v is not None for v in values)
    if op == "missing":
        return not any(v is not None for v in values)
    if op == "is_true":
        return any(v is True for v in values)
    if op == "is_false":
        return any(v is False for v in values)
    if op == "is":
        return any(_equal(v, c["value"]) for v in values)
    if op == "is_not":
        return not any(_equal(v, c["value"]) for v in values)
    if op == "one_of":
        return any(_equal(v, w) for v in values for w in c["value"])
    if op == "contains":
        return any(isinstance(v, str) and c["value"] in v for v in values)
    return False


def matches(conditions: list[dict], payload: object) -> bool:
    """Whether every condition holds for this delivery."""
    return all(_holds(c, resolve(payload, c["path"])) for c in conditions)


def extract(mapping: dict[str, dict], payload: object) -> dict[str, str]:
    """The mapped template values, typed and rendered as text. Raises :class:`ValueRejected` on
    the first field whose value is missing or fails its type: nothing is partly filled."""
    out: dict[str, str] = {}
    for field, m in mapping.items():
        got = resolve(payload, m["path"])
        if len(got) != 1 or got[0] is None:
            raise ValueRejected(field, f"the delivery has no value at {m['path']}")
        v = got[0]
        typ = m["type"]
        if typ == "number":
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueRejected(field, "expected a number")
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueRejected(field, "expected a finite number")
            if abs(v) > NUMBER_MAX:
                raise ValueRejected(field, f"a number beyond ±{NUMBER_MAX}")
            out[field] = str(v)
        elif typ == "yes_no":
            if not isinstance(v, bool):
                raise ValueRejected(field, "expected yes or no")
            out[field] = "yes" if v else "no"
        elif typ == "choice":
            if not isinstance(v, str) or v not in m["choices"]:
                raise ValueRejected(field, "not one of the allowed choices")
            out[field] = v
        else:
            if not isinstance(v, str) or len(v) > TEXT_MAX or not TEXT_RE.match(v):
                raise ValueRejected(
                    field, f"expected a short plain text (up to {TEXT_MAX} characters)"
                )
            out[field] = v
    return out


def describe_condition(c: dict) -> str:
    """One condition in the operator's words (consent lines)."""
    path = c["path"]
    op = c["op"]
    if op == "exists":
        return f"{path} is present"
    if op == "missing":
        return f"{path} is absent"
    if op == "is_true":
        return f"{path} is yes"
    if op == "is_false":
        return f"{path} is no"
    if op == "one_of":
        return f"{path} is one of " + ", ".join(_word(v) for v in c["value"])
    if op == "contains":
        return f"{path} contains {_word(c['value'])}"
    return f"{path} {'is not' if op == 'is_not' else 'is'} {_word(c['value'])}"


def _word(v: object) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str):
        return f"“{v}”"
    return str(v)


# ---- presets (the wizard's step 2) --------------------------------------------------------------

#: ``(id, label, the fields a delivery must have for it to fit, conditions)``. Forgejo/Gitea body
#: shapes: a pull request event carries ``pull_request`` and ``action``; a formal review also
#: carries ``review.type``; an issue event carries ``issue`` and, for a comment, ``comment``.
_CATALOGUE = (
    (
        "pr_opened_or_pushed",
        "PR opened or pushed (drafts skipped)",
        {"action", "pull_request.draft"},
        [
            # A push is ``synchronized`` from Forgejo itself, ``synchronize`` when CI re-sends it.
            {
                "path": "action",
                "op": "one_of",
                "value": ["opened", "reopened", "synchronized", "synchronize"],
            },
            {"path": "pull_request.draft", "op": "is_false"},
            {"path": "review", "op": "missing"},
        ],
    ),
    (
        "issue_opened_or_edited",
        "Issue opened or edited",
        {"action", "issue.number"},
        [
            {"path": "action", "op": "one_of", "value": ["opened", "reopened", "edited"]},
            {"path": "issue.number", "op": "exists"},
            {"path": "comment", "op": "missing"},
            {"path": "pull_request", "op": "missing"},
        ],
    ),
    (
        "review_submitted",
        "Review submitted",
        {"review.type", "pull_request.number"},
        [
            {
                "path": "review.type",
                "op": "one_of",
                "value": [
                    "pull_request_review_approved",
                    "pull_request_review_rejected",
                    "pull_request_review_comment",
                ],
            },
        ],
    ),
)


def presets(shape: list[dict]) -> list[dict]:
    """The presets that fit what this webhook last received: ``[{id, label, when}]``. An unknown
    sender (no delivery yet, or another shape) gets none: the wizard offers Advanced only."""
    paths = {f.get("path") for f in shape or [] if isinstance(f, dict)}
    return [
        {"id": pid, "label": label, "when": [dict(c) for c in when]}
        for pid, label, needs, when in _CATALOGUE
        if needs <= paths
    ]
