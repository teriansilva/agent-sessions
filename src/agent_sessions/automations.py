"""Automations — the model: validation, cadence, consent scope and pinned inputs (#1201 Phase 1).

An automation runs a mission or a session with nobody watching. This module owns what one IS and
what the operator agreed to; the store (`automations_store`), the runner (`automation_runner`) and
the scheduler (`automation_loop`) own what happens to it. Read `docs/invariants/automations.md`
before changing anything here.

**Strict writes, lenient reads.** `validate_config` refuses anything it does not understand (422,
nothing written). `coerce_config` reads a stored document and degrades a part it cannot read to
something that runs NOTHING (an unknown trigger is `manual`), never to something broader.

**A fixed cadence vocabulary, never a cron string** (#863): every N minutes (floor 5) or hours,
daily at HH:MM, weekly on days + time, monthly on a day + time, in an IANA timezone. Interval
cadences are elapsed time (slots aligned to the UTC epoch, so a DST change neither adds nor drops
one); calendar cadences are **local calendar slots**: a local time the clock skips runs once, at
the next valid minute, and a local time that occurs twice runs once (its first occurrence). The
slot id is the nominal local time either way, which is what makes it single-fire.

**Consent is a scope, compared field by field.** `scope_of` derives what an automation may do;
`widened` names every way a new scope exceeds an old one. Widening needs fresh consent, narrowing
does not. Content (the instruction, the template's pinned revision, the checklist's digest) is part
of the scope: a changed instruction is a different grant.

Loop and webhook triggers are Phases 2–3: they are reserved names here, and saving one is refused.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NAME_MAX = 80
TEXT_MAX = 16000
VALUE_MAX = 4000
VALUES_MAX = 32

INTERVAL_MIN_MINUTES = 5  # the cadence floor (#1201 §1)
INTERVAL_MAX_MINUTES = 1440
INTERVAL_MAX_HOURS = 168
MAX_RUNS_PER_DAY_DEFAULT = 48
MAX_RUNS_PER_DAY_MAX = 288
PAUSE_AFTER_FAILURES_DEFAULT = 3
PAUSE_AFTER_FAILURES_MAX = 20
MAX_CONCURRENT_MAX = 3  # "allow up to a small cap"
EXPIRES_MAX = 253402300799  # 9999-12-31T23:59:59Z

TRIGGER_KINDS = ("once", "schedule", "manual")
#: Reserved for later phases. Accepted as a NAME so the refusal can say why; never stored.
RESERVED_TRIGGERS = {"loop": "Phase 2", "webhook": "Phase 3"}
ACTION_KINDS = ("start_mission", "start_session", "send_to_session")
#: Mission autonomy, ranked. `propose` creates the mission and its plan and stops for the operator
#: to dispatch; `dispatch` dispatches the plan through the manual dispatch route's own fences;
#: `dispatch_auto_choose` also turns on the per-mission menu-answer opt-in (#1060), which the
#: mission-orchestration tier still gates. None of them can raise that tier (#1019).
AUTONOMY = ("propose", "dispatch", "dispatch_auto_choose")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONCURRENCY = ("skip", "allow")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class AutomationError(ValueError):
    """A request the model refuses. ``status`` is the HTTP status; nothing was written."""

    def __init__(self, message: str, status: int = 422, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


# ---- small validators ---------------------------------------------------------------------------


def _word(raw: object, what: str) -> str:
    """A scalar string, checked BEFORE it is hashed or compared against a set (#1201 review):
    `{"kind": []}` must be a 422, never an unhashable-type 500."""
    if not isinstance(raw, str):
        raise AutomationError(f"{what} must be a string")
    return raw


def _obj(raw: object, what: str, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(raw, dict):
        raise AutomationError(f"{what} must be an object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AutomationError(f"{what} does not take {', '.join(unknown)}")
    missing = sorted(required - set(raw))
    if missing:
        raise AutomationError(f"{what} needs {', '.join(missing)}")
    return raw


def _int(raw: object, what: str, lo: int, hi: int) -> int:
    # `isinstance(True, int)` is True: booleans are refused on TYPE, like every strict route here.
    if not isinstance(raw, int) or isinstance(raw, bool) or not lo <= raw <= hi:
        raise AutomationError(f"{what} must be a whole number from {lo} to {hi}")
    return raw


def _text(raw: object, what: str, max_len: int, *, multiline: bool = False) -> str:
    if not isinstance(raw, str):
        raise AutomationError(f"{what} must be a string")
    bad = [c for c in raw if ord(c) < 32 and not (multiline and c in "\n\t")] + [
        c for c in raw if ord(c) == 127
    ]
    if bad:
        # ESC could end a bracketed paste early and smuggle key input into a session (#618).
        raise AutomationError(f"{what} contains control characters")
    if len(raw) > max_len:
        raise AutomationError(f"{what} is longer than {max_len} characters")
    return raw


def host_timezone() -> str:
    """The host's IANA zone name, or ``UTC``. From the environment and ``/etc``, never guessed."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and _valid_tz(tz):
        return tz
    with _suppress():
        name = Path("/etc/timezone").read_text().strip()
        if name and _valid_tz(name):
            return name
    with _suppress():
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            name = target.split("/zoneinfo/", 1)[1]
            if _valid_tz(name):
                return name
    return "UTC"


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], Exception)


def _valid_tz(name: object) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64 or ".." in name:
        return False
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 — any failure to load is "not a zone"
        return False
    return True


def _tz(raw: object) -> str:
    if raw is None:
        return host_timezone()
    if not _valid_tz(raw):
        raise AutomationError("tz must be an IANA timezone name such as Europe/Berlin")
    return str(raw)


# ---- trigger ------------------------------------------------------------------------------------


def _cadence(raw: object) -> dict:
    c = _obj(raw, "cadence", {"kind", "every", "unit", "time", "days", "day"}, {"kind"})
    kind = _word(c["kind"], "cadence kind")
    if kind == "interval":
        _obj(c, "an interval cadence", {"kind", "every", "unit"}, {"every", "unit"})
        if _word(c["unit"], "unit") == "minutes":
            n = _int(
                c["every"],
                "every (minutes)",
                INTERVAL_MIN_MINUTES,
                INTERVAL_MAX_MINUTES,
            )
        elif c["unit"] == "hours":
            n = _int(c["every"], "every (hours)", 1, INTERVAL_MAX_HOURS)
        else:
            raise AutomationError("unit must be minutes or hours")
        return {"kind": "interval", "every": n, "unit": c["unit"]}
    if kind == "daily":
        _obj(c, "a daily cadence", {"kind", "time"}, {"time"})
        return {"kind": "daily", "time": _hhmm(c["time"])}
    if kind == "weekly":
        _obj(c, "a weekly cadence", {"kind", "days", "time"}, {"days", "time"})
        days = c["days"]
        if (
            not isinstance(days, list)
            or not all(isinstance(d, str) for d in days)
            or not days
            or any(d not in WEEKDAYS for d in days)
            or len(set(days)) != len(days)
        ):
            raise AutomationError(f"days must be a non-empty list of distinct {list(WEEKDAYS)}")
        ordered = [d for d in WEEKDAYS if d in days]
        return {"kind": "weekly", "days": ordered, "time": _hhmm(c["time"])}
    if kind == "monthly":
        _obj(c, "a monthly cadence", {"kind", "day", "time"}, {"day", "time"})
        return {"kind": "monthly", "day": _int(c["day"], "day", 1, 31), "time": _hhmm(c["time"])}
    raise AutomationError("cadence kind must be interval, daily, weekly or monthly (no cron)")


def _hhmm(raw: object) -> str:
    if not isinstance(raw, str) or not _TIME_RE.match(raw):
        raise AutomationError("time must be HH:MM (24-hour)")
    return raw


def validate_trigger(raw: object, *, now: float | None = None) -> dict:
    t = _obj(raw, "trigger", {"kind", "at", "tz", "cadence"}, {"kind"})
    kind = _word(t["kind"], "trigger kind")
    if kind in RESERVED_TRIGGERS:
        raise AutomationError(
            f"the {kind} trigger is not available yet ({RESERVED_TRIGGERS[kind]} of #1201)"
        )
    if kind == "manual":
        _obj(t, "a manual trigger", {"kind"})
        return {"kind": "manual"}
    if kind == "once":
        _obj(t, "a once trigger", {"kind", "at", "tz"}, {"at"})
        at = t["at"]
        if not isinstance(at, str) or not _AT_RE.match(at):
            raise AutomationError("at must be a local time YYYY-MM-DDTHH:MM")
        try:
            datetime.strptime(at, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise AutomationError("at is not a real date and time") from None
        out = {"kind": "once", "at": at, "tz": _tz(t.get("tz"))}
        if now is not None and _once_fire_at(out) <= now:
            raise AutomationError("that time has already passed")
        return out
    if kind == "schedule":
        _obj(t, "a schedule trigger", {"kind", "cadence", "tz"}, {"cadence"})
        return {"kind": "schedule", "cadence": _cadence(t["cadence"]), "tz": _tz(t.get("tz"))}
    raise AutomationError("trigger kind must be once, schedule or manual")


# ---- action -------------------------------------------------------------------------------------


def validate_message(raw: object, what: str) -> dict:
    m = _obj(raw, what, {"text", "template_id", "values"})
    if ("text" in m) == ("template_id" in m):
        raise AutomationError(f"{what} is either {{text}} or {{template_id, values}}")
    if "text" in m:
        if "values" in m:
            raise AutomationError(f"{what}: values go with a template")
        text = _text(m["text"], f"{what} text", TEXT_MAX, multiline=True)
        if not text.strip():
            raise AutomationError(f"{what} text is empty")
        return {"text": text}
    tid = m["template_id"]
    if not isinstance(tid, str) or not tid or len(tid) > 200:
        raise AutomationError(f"{what}: template_id must be a template id")
    values = m.get("values") or {}
    if not isinstance(values, dict) or len(values) > VALUES_MAX:
        raise AutomationError(f"{what}: values must be an object of at most {VALUES_MAX} fields")
    clean: dict[str, str] = {}
    for k, v in values.items():
        if not isinstance(k, str) or not _FIELD_RE.match(k):
            raise AutomationError(f"{what}: {k!r} is not a field name")
        clean[k] = _text(v, f"{what} value {k}", VALUE_MAX, multiline=True)
    return {"template_id": tid, "values": dict(sorted(clean.items()))}


def validate_action(raw: object) -> dict:
    a = _obj(
        raw,
        "action",
        {
            "kind",
            "project_id",
            "instruction",
            "checklist_id",
            "autonomy",
            "engine",
            "model",
            "folder",
            "bypass",
            "message",
            "session_key",
        },
        {"kind"},
    )
    kind = _word(a["kind"], "action kind")
    if kind == "start_mission":
        _obj(
            a,
            "a start_mission action",
            {"kind", "project_id", "instruction", "checklist_id", "autonomy"},
            {"project_id", "instruction"},
        )
        pid = a["project_id"]
        if not isinstance(pid, str) or not pid or len(pid) > 200:
            raise AutomationError("project_id must be a project id")
        checklist = a.get("checklist_id")
        if checklist is not None and (
            not isinstance(checklist, str) or not (checklist == ":none" or _ID_RE.match(checklist))
        ):
            raise AutomationError('checklist_id must be a checklist id, ":none" or null')
        autonomy = _word(a.get("autonomy", "propose"), "autonomy")
        if autonomy not in AUTONOMY:
            raise AutomationError(f"autonomy must be one of {list(AUTONOMY)}")
        return {
            "kind": kind,
            "project_id": pid,
            "instruction": validate_message(a["instruction"], "instruction"),
            "checklist_id": checklist,
            "autonomy": autonomy,
        }
    if kind == "start_session":
        _obj(
            a,
            "a start_session action",
            {"kind", "engine", "model", "folder", "bypass", "message"},
            {"engine", "folder", "message"},
        )
        engine = a["engine"]
        if not isinstance(engine, str) or not _ID_RE.match(engine):
            raise AutomationError("engine must be an engine id")
        if a.get("model") is not None:
            # Launches can take a model since #1189, but an automation's consent does not cover
            # one yet: what a stored consent means when the model it named is later removed or
            # renamed is its own decision (a follow-up), and a field accepted here and ignored by
            # the runner would be consent to something that never runs. Null only, until then.
            raise AutomationError("choosing a model for an automation is not available yet")
        folder = _text(a["folder"], "folder", 4096)
        if not folder.startswith("/"):
            raise AutomationError("folder must be an absolute path")
        bypass = a.get("bypass", False)
        if not isinstance(bypass, bool):
            raise AutomationError("bypass must be true or false")
        return {
            "kind": kind,
            "engine": engine,
            "model": None,
            "folder": os.path.normpath(folder),
            "bypass": bypass,
            "message": validate_message(a["message"], "message"),
        }
    if kind == "send_to_session":
        _obj(a, "a send_to_session action", {"kind", "session_key", "message"}, {"session_key"})
        key = a["session_key"]
        if not isinstance(key, str) or not key or len(key) > 300 or ":" not in key:
            raise AutomationError("session_key must be an engine-qualified session id")
        if "message" not in a:
            raise AutomationError("a send_to_session action needs message")
        return {
            "kind": kind,
            "session_key": key,
            "message": validate_message(a["message"], "message"),
        }
    raise AutomationError(f"action kind must be one of {list(ACTION_KINDS)}")


def validate_policy(raw: object) -> dict:
    p = _obj(
        {} if raw is None else raw,
        "policy",
        {"concurrency", "max_concurrent", "max_runs_per_day", "pause_after_failures", "expires_at"},
    )
    concurrency = _word(p.get("concurrency", "skip"), "concurrency")
    if concurrency not in CONCURRENCY:
        raise AutomationError("concurrency must be skip or allow")
    if concurrency == "skip":
        if "max_concurrent" in p and p["max_concurrent"] != 1:
            raise AutomationError("max_concurrent goes with concurrency: allow")
        max_concurrent = 1
    else:
        max_concurrent = _int(p.get("max_concurrent", 2), "max_concurrent", 1, MAX_CONCURRENT_MAX)
    expires = p.get("expires_at")
    if expires is not None and (
        not isinstance(expires, int | float)
        or isinstance(expires, bool)
        # Compared BEFORE any float(): a huge JSON integer overflows the conversion, and `1e999`
        # parses as infinity, which `nan`-style comparisons would otherwise wave through.
        or not 0 < expires <= EXPIRES_MAX
        or not math.isfinite(expires)
    ):
        raise AutomationError("expires_at must be a unix time before the year 10000, or null")
    return {
        "concurrency": concurrency,
        "max_concurrent": max_concurrent,
        "max_runs_per_day": _int(
            p.get("max_runs_per_day", MAX_RUNS_PER_DAY_DEFAULT),
            "max_runs_per_day",
            1,
            MAX_RUNS_PER_DAY_MAX,
        ),
        "pause_after_failures": _int(
            p.get("pause_after_failures", PAUSE_AFTER_FAILURES_DEFAULT),
            "pause_after_failures",
            1,
            PAUSE_AFTER_FAILURES_MAX,
        ),
        "expires_at": float(expires) if expires is not None else None,
    }


def validate_name(raw: object) -> str:
    name = _text(raw, "name", NAME_MAX).strip()
    if not name:
        raise AutomationError("name is required")
    return name


_CONFIG = {"name", "trigger", "action"}


def validate_config(raw: object, *, now: float | None = None) -> dict:
    """A whole automation from a create body (or a patch merged onto the stored one). STRICT."""
    c = _obj(raw, "automation", {*_CONFIG, "policy"}, _CONFIG)
    return {
        "name": validate_name(c["name"]),
        "trigger": validate_trigger(c["trigger"], now=now),
        "action": validate_action(c["action"]),
        "policy": validate_policy(c.get("policy")),
    }


def coerce_config(raw: object) -> dict | None:
    """LENIENT read of a stored config. ``None`` = unreadable, which the caller treats as nothing
    to run. A stored document that no longer validates never runs."""
    try:
        return validate_config(raw)
    except (AutomationError, TypeError, KeyError, ValueError, OverflowError):
        return None


# ---- cadence arithmetic -------------------------------------------------------------------------


def _local_instant(d: date, hhmm: str, tz: ZoneInfo) -> float:
    """The UTC instant a local calendar slot fires at.

    A local time the clock SKIPS (spring forward) fires at the next valid minute; a local time that
    occurs TWICE (fall back) fires at its first occurrence (``fold=0``). Found by round-tripping
    each candidate minute, so no zone's rules are assumed."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    naive = datetime(d.year, d.month, d.day, hh, mm)
    for step in range(0, 24 * 60):
        cand = naive + timedelta(minutes=step)
        aware = cand.replace(tzinfo=tz, fold=0)
        if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == cand:
            return aware.timestamp()
    return naive.replace(tzinfo=tz).timestamp()  # pragma: no cover — no zone skips a whole day


def _interval_s(cadence: dict) -> int:
    return int(cadence["every"]) * (60 if cadence["unit"] == "minutes" else 3600)


def _once_fire_at(trigger: dict) -> float:
    d = datetime.strptime(trigger["at"], "%Y-%m-%dT%H:%M")
    return _local_instant(d.date(), trigger["at"][11:], ZoneInfo(trigger["tz"]))


def _calendar_day_slot(cadence: dict, d: date) -> bool:
    kind = cadence["kind"]
    if kind == "daily":
        return True
    if kind == "weekly":
        return WEEKDAYS[d.weekday()] in cadence["days"]
    if kind == "monthly":
        last = calendar.monthrange(d.year, d.month)[1]
        # A day the month does not have runs on its last day, once.
        return d.day == min(int(cadence["day"]), last)
    return False


#: Bound on how far back a collapsed backlog is enumerated. The count saturates rather than walking
#: years of calendar; what matters is that the backlog is ONE run, not its exact size.
BACKLOG_SCAN_DAYS = 400


def due_slots(trigger: dict, after: float, until: float) -> dict:
    """The slots with ``after < fire_at <= until``: ``{count, first, last}`` (each ``(slot, at)``).

    ``count`` is 0 when nothing is due. Calendar slots are enumerated day by day (bounded by
    ``BACKLOG_SCAN_DAYS``); interval slots are counted arithmetically, so a year of 5-minute slots
    costs nothing."""
    empty = {"count": 0, "first": None, "last": None}
    kind = trigger.get("kind")
    if until <= after:
        return empty
    if kind == "once":
        at = _once_fire_at(trigger)
        if after < at <= until:
            slot = (trigger["at"], at)
            return {"count": 1, "first": slot, "last": slot}
        return empty
    if kind != "schedule":
        return empty
    cadence = trigger["cadence"]
    if cadence["kind"] == "interval":
        period = _interval_s(cadence)
        lo = math.floor(after / period) + 1
        hi = math.floor(until / period)
        if hi < lo:
            return empty
        return {
            "count": hi - lo + 1,
            "first": (_utc_slot(lo * period), float(lo * period)),
            "last": (_utc_slot(hi * period), float(hi * period)),
        }
    tz = ZoneInfo(trigger["tz"])
    start = max(after, until - BACKLOG_SCAN_DAYS * 86400)
    d = datetime.fromtimestamp(start, tz).date() - timedelta(days=1)
    end = datetime.fromtimestamp(until, tz).date() + timedelta(days=1)
    count, first, last = 0, None, None
    while d <= end:
        if _calendar_day_slot(cadence, d):
            at = _local_instant(d, cadence["time"], tz)
            if after < at <= until:
                slot = (f"{d.isoformat()}T{cadence['time']}", at)
                count += 1
                first = first or slot
                last = slot
        d += timedelta(days=1)
    return {"count": count, "first": first, "last": last}


def _utc_slot(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%MZ")


def next_slot(trigger: dict, after: float) -> tuple[str, float] | None:
    """The first slot strictly after ``after``, or None (a spent once, a manual trigger)."""
    kind = trigger.get("kind")
    if kind == "once":
        at = _once_fire_at(trigger)
        return (trigger["at"], at) if at > after else None
    if kind != "schedule":
        return None
    cadence = trigger["cadence"]
    if cadence["kind"] == "interval":
        period = _interval_s(cadence)
        k = math.floor(after / period) + 1
        return _utc_slot(k * period), float(k * period)
    tz = ZoneInfo(trigger["tz"])
    d = datetime.fromtimestamp(after, tz).date() - timedelta(days=1)
    for _ in range(800):
        if _calendar_day_slot(cadence, d):
            at = _local_instant(d, cadence["time"], tz)
            if at > after:
                return f"{d.isoformat()}T{cadence['time']}", at
        d += timedelta(days=1)
    return None


def runs_per_week(trigger: dict) -> float:
    """How often a trigger fires — the comparand for "a shorter interval" widening."""
    kind = trigger.get("kind")
    if kind != "schedule":
        return 0.0
    c = trigger["cadence"]
    if c["kind"] == "interval":
        return 7 * 86400 / _interval_s(c)
    if c["kind"] == "daily":
        return 7.0
    if c["kind"] == "weekly":
        return float(len(c["days"]))
    return 7 / 30.44


def local_day(ts: float, tz_name: str | None) -> str:
    tz = ZoneInfo(tz_name) if tz_name and _valid_tz(tz_name) else UTC
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def day_bounds(ts: float, tz_name: str | None) -> tuple[float, float]:
    """The local day ``ts`` falls in, as ``[start, end)`` UTC instants — the daily cap's window."""
    tz = ZoneInfo(tz_name) if tz_name and _valid_tz(tz_name) else UTC
    d = datetime.fromtimestamp(ts, tz).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
    n = d + timedelta(days=1)
    return start, datetime(n.year, n.month, n.day, tzinfo=tz).timestamp()


def trigger_tz(config: dict) -> str | None:
    return config.get("trigger", {}).get("tz")


# ---- pinned inputs ------------------------------------------------------------------------------


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def template_digest(template: dict) -> str:
    """A template's CONTENT revision: body, fields and images. Usage counters are not content."""
    return _digest(
        {"body": template["body"], "fields": template["fields"], "images": template["images"]}
    )


def _library_kinds(template: dict) -> dict[str, str]:
    """``{name: text|secret|missing}`` for every library field — a kind change is re-approval."""
    from . import template_vars

    names = sorted({f["name"] for f in template["fields"] if f["source"] == "library"})
    if not names:
        return {}
    text = template_vars.values()
    secret = template_vars.secret_state()
    return {n: ("secret" if n in secret else "text" if n in text else "missing") for n in names}


def message_of(action: dict) -> dict:
    return action.get("instruction") if action["kind"] == "start_mission" else action["message"]


def compute_pins(config: dict) -> dict:
    """What the unattended input is bound to: the template's revision, its library variables'
    kinds and the checklist's digest. Server-written; a client can never supply one."""
    from . import prefs
    from . import templates as tstore

    action = config["action"]
    pins: dict = {"template": None, "checklist": None}
    msg = message_of(action)
    if "template_id" in msg:
        try:
            t = tstore.get_template(msg["template_id"])
        except tstore.TemplateNotFound:
            pins["template"] = {"id": msg["template_id"], "missing": True}
        else:
            pins["template"] = {
                "id": t["id"],
                "updated_at": t["updated_at"],
                "digest": template_digest(t),
                "library": _library_kinds(t),
                "name": t.get("name"),
            }
    if action["kind"] == "start_mission" and action.get("checklist_id") != ":none":
        # `None` is "the operator's DEFAULT checklist" — resolved and pinned NOW, so a later change
        # of the default (or of its contents) is a drift the operator re-approves, and the run
        # creates the mission with exactly the pinned id.
        block = prefs.get_mission_playbooks()
        default = action.get("checklist_id") is None
        cid = block["default_id"] if default else action["checklist_id"]
        book = next((p for p in block["playbooks"] if p["id"] == cid), None) if cid else None
        pins["checklist"] = {
            "id": cid or None,
            "default": default,
            "label": book["label"] if book else "",
            "digest": _digest(book) if book else ("none" if default and not cid else "missing"),
        }
    if action["kind"] == "start_mission":
        pins["cwd"] = resolve_mission_cwd(action["project_id"])
    elif action["kind"] == "start_session":
        # The folder's IDENTITY, not its spelling: a symlink repointed after consent is a drift,
        # and the launch uses `real` (#1201 review).
        pins["cwd"] = folder_identity(action["folder"])
    return pins


class PinsUnavailable(RuntimeError):
    """An input could not be RESOLVED right now (an unreadable store). Not a drift: the caller
    skips this check and retries, and nothing is paused for it."""


def folder_identity(path: str) -> dict:
    """``{path, real, dev, ino}`` — the directory's identity, so a folder replaced at the same
    name is a drift too. A path that no longer exists pins ``{path, missing}``."""
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return {"path": path, "missing": "that folder no longer exists"}
    except OSError as e:
        raise PinsUnavailable(f"the folder could not be checked ({e.strerror})") from None
    return {"path": path, "real": real, "dev": st.st_dev, "ino": st.st_ino}


def resolve_mission_cwd(project_id: str) -> dict:
    """The folder a mission for ``project_id`` would launch in NOW: ``{path, real}``, or
    ``{missing: reason}``. Pinned at consent, so a repointed project is a drift, not a new target
    the operator never saw (the manual route's "the path shown is the path launched")."""
    from . import missions
    from .routes import missions as mroutes

    try:
        _pid, cwd = mroutes._resolve_cwd(project_id)
    except missions.MissionError as e:
        if e.status >= 500:
            # The project store could not be READ: that is not "the project changed".
            raise PinsUnavailable(str(e)) from None
        return {"missing": str(e)}
    except Exception as e:  # noqa: BLE001 — an unreadable store is a retry, never a drift
        raise PinsUnavailable(f"the project could not be resolved ({type(e).__name__})") from None
    if not cwd:
        return {"missing": "that project has no folder to work in"}
    return folder_identity(cwd)


def pins_drift(stored: dict, current: dict) -> str:
    """Why the current inputs differ from the approved ones, or ``""``."""
    st, ct = stored.get("template"), current.get("template")
    if (st or {}).get("digest") != (ct or {}).get("digest") or (st or {}).get("missing") != (
        ct or {}
    ).get("missing"):
        return "the template was edited since you approved this automation"
    if (st or {}).get("library") != (ct or {}).get("library"):
        return "a library variable the template uses changed kind or was removed"
    # The NAME and the saved revision are pinned too (#1252 review): the consent lines show the
    # name, and a send is fenced on the revision (`expected_updated_at`) — a rename with the same
    # content would otherwise be refused at every send with no approval ever offered.
    if (st or {}).get("name") != (ct or {}).get("name"):
        return "the template's name changed"
    if (st or {}).get("updated_at") != (ct or {}).get("updated_at"):
        return "the template was saved again since you approved this automation"
    sc, cc = stored.get("checklist") or {}, current.get("checklist") or {}
    if (sc.get("id"), sc.get("digest")) != (cc.get("id"), cc.get("digest")):
        return "the checklist was edited since you approved this automation"
    if sc.get("label") != cc.get("label"):
        return "the checklist's name changed"
    if stored.get("cwd") != current.get("cwd"):
        return "the working folder changed since you approved this automation"
    return ""


# ---- template checks ----------------------------------------------------------------------------


def check_template(config: dict) -> dict | None:
    """Refuse a template this action may not carry. Returns the template (or None for text).

    * A mission brief never carries a secret (#1090), so a mission action with a secret template
      is refused — at save, and again immediately before the mission is created.
    * A new session's brief is seeded through the launch path, which keeps the seed on disk; a
      secret template is refused there too. Only ``send_to_session`` delivers a secret, through the
      server-side ``template_send`` path.
    * A TYPED secret field would have to be stored here to be used unattended; it is refused (store
      it in the variable library instead).
    """
    from . import templates as tstore

    action = config["action"]
    msg = message_of(action)
    if "template_id" not in msg:
        return None
    try:
        t = tstore.get_template(msg["template_id"])
    except tstore.TemplateNotFound:
        raise AutomationError("unknown template", status=404) from None
    fields = {f["name"]: f for f in t["fields"]}
    unknown = sorted(set(msg["values"]) - set(fields))
    if unknown:
        raise AutomationError(f"values for fields this template does not declare: {unknown}")
    for name in msg["values"]:
        if fields[name]["source"] == "library":
            raise AutomationError(f"{name} takes its value from the variable library")
    for f in t["fields"]:
        if f["kind"] == "secret" and f["source"] != "library":
            raise AutomationError(
                f"{f['name']} is a typed secret, which an automation cannot keep; "
                "store it in the variable library"
            )
    secret = any(f["kind"] == "secret" for f in t["fields"]) or any(
        k == "secret" for k in _library_kinds(t).values()
    )
    if secret and action["kind"] == "start_mission":
        raise AutomationError(
            "a mission brief never carries a secret, so this template cannot start a mission"
        )
    if secret and action["kind"] == "start_session":
        raise AutomationError(
            "a template with a secret cannot be a new session's first message; "
            "use send to session instead"
        )
    return t


# ---- consent scope ------------------------------------------------------------------------------


def _content(action: dict) -> str:
    return _digest(message_of(action))


def scope_of(config: dict, pins: dict) -> dict:
    """Everything the operator consents to, as one comparable document (#1201 §2)."""
    action, trigger, policy = config["action"], config["trigger"], config["policy"]
    target: dict = {}
    if action["kind"] == "start_mission":
        target = {
            "project_id": action["project_id"],
            "checklist_id": action.get("checklist_id"),
            "cwd": (pins.get("cwd") or {}).get("real"),
        }
    elif action["kind"] == "start_session":
        target = {"folder": action["folder"], "cwd": (pins.get("cwd") or {}).get("real")}
    else:
        target = {"session_key": action["session_key"]}
    return {
        "version": 1,
        "action": action["kind"],
        "target": target,
        "engine": action.get("engine"),
        "model": action.get("model"),
        # An automated mission NEVER launches with bypass (Phase 1): the dispatch is called with
        # `bypass_ceiling=False`, so the receipt's `False` is enforced, not merely recorded.
        "bypass": bool(action.get("bypass", False)) if action["kind"] == "start_session" else False,
        "autonomy": action.get("autonomy"),
        "trigger": trigger["kind"],
        "cadence": trigger.get("cadence") or trigger.get("at"),
        "tz": trigger.get("tz"),
        "runs_per_week": round(runs_per_week(trigger), 6),
        "max_runs_per_day": policy["max_runs_per_day"],
        "concurrency": policy["concurrency"],
        "max_concurrent": policy["max_concurrent"],
        "pause_after_failures": policy["pause_after_failures"],
        "expires_at": policy["expires_at"],
        "content": _content(action),
        # What it sends, disclosed in the consent lines (`describe`): the text, or the template id
        # and its typed values. Never a secret — a typed secret cannot be stored.
        "message": message_of(action),
        "template_name": (pins.get("template") or {}).get("name"),
        "template": (pins.get("template") or {}).get("digest"),
        "template_library": (pins.get("template") or {}).get("library"),
        "checklist": (pins.get("checklist") or {}).get("digest"),
        "checklist_name": (pins.get("checklist") or {}).get("label")
        or (pins.get("checklist") or {}).get("id"),
        "checklist_default": bool((pins.get("checklist") or {}).get("default")),
        # Phase 3 fields, fixed here so a later phase widens against a real baseline.
        "webhook": False,
        "mapped": [],
    }


#: The ONLY scope fields compared by direction (a ladder): narrowing them needs no consent. Every
#: other field is compared for EQUALITY, and a field the receipt lacks at all is a widening — so a
#: receipt that does not match the full current scope can only ever be replaced through consent
#: (#1252 review: a PATCH once refreshed a legacy receipt that predated `message`).
LADDER_FIELDS = (
    "bypass",
    "autonomy",
    "runs_per_week",
    "max_runs_per_day",
    "max_concurrent",
    "pause_after_failures",
    "expires_at",
    "webhook",
    "mapped",
)

#: The operator's words for an equality field that changed (or that no receipt covers).
_CHANGED = {
    "action": "a different action",
    "target": "a new target",
    "engine": "a different agent or model",
    "model": "a different agent or model",
    "trigger": "a different trigger",
    # ANY change of when it runs — time, days, day of month, interval, zone, or a once's `at` — is
    # re-approved: "runs at 03:00" and "runs at 14:00" are different grants.
    "cadence": "the schedule changed",
    "tz": "the schedule changed",
    "concurrency": "what happens when a run is still going changed",
    "content": "what it sends changed",
    "message": "what it sends changed",
    "template": "the template changed",
    "template_library": "the template changed",
    "template_name": "the template changed",
    "checklist": "the checklist changed",
    "checklist_name": "the checklist changed",
    "checklist_default": "the checklist changed",
}

#: A name field and the content revision it names: a rename with the same revision is worded so.
_NAME_OF = {"template_name": "template", "checklist_name": "checklist"}
_NAME_WORD = {"template_name": "template", "checklist_name": "checklist"}

_LADDER_WORDS = {
    "bypass": "permission bypass turned on",
    "autonomy": "higher mission autonomy",
    "runs_per_week": "runs more often",
    "max_runs_per_day": "a higher daily cap",
    "max_concurrent": "more runs at the same time",
    "pause_after_failures": "tolerates more failures before pausing",
    "expires_at": "runs until later",
    "webhook": "a webhook can start it",
    "mapped": "a webhook can fill more variables",
}


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _ladder_widens(key: str, old: object, new: object) -> bool:
    """Whether ``new`` is further up ``key``'s ladder than ``old``. An unreadable value widens."""
    if key in ("bypass", "webhook"):
        return bool(new) and not bool(old)
    if key == "autonomy":
        return new is not None and _rank(new) > _rank(old)
    if key == "mapped":
        if not isinstance(new, list):
            return True
        return bool(set(map(str, new)) - set(map(str, old if isinstance(old, list) else [])))
    if key == "expires_at":
        if old is None:
            return False  # an old grant without an end already covers any end
        if new is None:
            return True  # the end was removed
        o, n = _num(old), _num(new)
        return o is None or n is None or n > o
    o, n = _num(old), _num(new)
    if o is None or n is None:
        return True
    return n > o + (1e-9 if key == "runs_per_week" else 0)


def widened(old: dict | None, new: dict) -> list[str]:
    """Every way ``new`` exceeds ``old``. Empty = equal or narrower (no fresh consent needed).

    Unreadable or missing ``old`` widens by definition: no receipt covers anything. Only the
    `LADDER_FIELDS` may narrow without consent; every other field must be EQUAL, and a field the
    receipt does not have is a widening."""
    if not isinstance(old, dict) or old.get("version") != 1:
        return ["nothing has been approved yet"]
    out: list[str] = []

    def add(words: str) -> None:
        if words not in out:
            out.append(words)

    for key in sorted(set(old) | set(new)):
        if key == "version":
            continue
        if key in LADDER_FIELDS:
            if key not in old or key not in new:
                add(_LADDER_WORDS[key])
            elif _ladder_widens(key, old[key], new[key]):
                add(_LADDER_WORDS[key])
            continue
        if key not in old or key not in new or old[key] != new[key]:
            if key in _NAME_OF and key in old and old.get(_NAME_OF[key]) == new.get(_NAME_OF[key]):
                # Only the NAME moved (its content revision is the same): still a change the lines
                # show, so still consent — but said as what it is.
                add(f"the {_NAME_WORD[key]}'s name changed")
            else:
                add(_CHANGED.get(key, f"what it is approved to do changed ({key})"))
    return out


def _rank(autonomy: object) -> int:
    return AUTONOMY.index(autonomy) if autonomy in AUTONOMY else -1


_WEEKDAY_WORDS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


def _cadence_words(cadence: object, tz: object) -> str:
    """The EXACT schedule: every field the digest holds, in words."""
    zone = f" ({tz})" if tz else ""
    if isinstance(cadence, str):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})$", cadence)
        return f"Once, on {m.group(1)} at {m.group(2)}{zone}" if m else f"Once, at {cadence}{zone}"
    if not isinstance(cadence, dict):
        return f"On a schedule: {cadence!r}{zone}"
    kind = cadence.get("kind")
    if kind == "interval" and cadence.get("unit") in ("minutes", "hours"):
        return f"Every {cadence.get('every')} {cadence.get('unit')}{zone}"
    if kind == "daily":
        return f"Every day at {cadence.get('time')}{zone}"
    if kind == "weekly":
        days = ", ".join(_WEEKDAY_WORDS.get(d, str(d)) for d in cadence.get("days") or [])
        return f"Every week on {days} at {cadence.get('time')}{zone}"
    if kind == "monthly":
        return (
            f"Every month on day {cadence.get('day')} at {cadence.get('time')}{zone}"
            " (the month's last day when it is shorter)"
        )
    return f"On a schedule: {json.dumps(cadence, sort_keys=True)}{zone}"


def _local_time(ts: float, tz: object) -> str:
    name = tz if _valid_tz(tz) else host_timezone()
    return f"{datetime.fromtimestamp(ts, ZoneInfo(name)).strftime('%Y-%m-%d %H:%M:%S')} ({name})"


def _message_lines(scope: dict, preview: str | None = None) -> list[str]:
    """What it sends, in full: plain text verbatim, or the template, its revision and every value.
    Secrets never appear — a typed secret cannot be stored, and a library secret is named only."""
    msg = scope.get("message")
    word = "Instruction" if scope.get("action") == "start_mission" else "Message"
    out: list[str] = []
    if isinstance(msg, dict) and "text" in msg:
        out.append(f"{word} (plain text):\n{msg['text']}")
    elif isinstance(msg, dict) and "template_id" in msg:
        name = scope.get("template_name") or msg.get("template_id")
        rev = str(scope.get("template") or "unknown")
        out.append(f"{word}: template “{name}” ({msg.get('template_id')}), revision {rev}")
        for k, v in sorted((msg.get("values") or {}).items()):
            out.append(f"  {k} = {v}")
        if preview is not None:
            # The template as it would be sent NOW: its body with these values and the library's
            # text values filled in, and every secret as `[secret: name]` (`message_preview`).
            out.append(f"{word} as it would be sent now:\n{preview}")
    elif msg is not None:
        out.append(f"{word}: {msg!r}")
    if not (isinstance(msg, dict) and "template_id" in msg):
        if scope.get("template_name"):
            out.append(f"Template: “{scope['template_name']}”")
        if scope.get("template"):
            out.append(f"Template revision: {scope['template']}")
    lib = scope.get("template_library")
    if lib and not isinstance(lib, dict):
        out.append(f"Template library variables: {lib!r}")
    for k, kind in sorted((lib if isinstance(lib, dict) else {}).items()):
        if kind == "secret":
            out.append(f"  {k} = [secret: {k}] (from your variable library)")
        elif kind == "missing":
            out.append(f"  {k}: not in your variable library")
        elif kind == "text":
            out.append(f"  {k}: from your variable library, read at each run")
        else:
            out.append(f"  {k}: {kind!r} (from your variable library)")
    # The content's own fingerprint — what the digest binds — so two contents never read the same.
    if scope.get("content"):
        out.append(f"Content fingerprint: {scope['content']}")
    return out


def message_preview(config: dict | None) -> str | None:
    """A template action's message, rendered for the consent lines: the body with the automation's
    values and the library's TEXT values filled in, and every secret — typed or from the library —
    as ``[secret: name]``. No secret is read to build it. ``None`` for a plain-text message (its
    text is already in the lines) or an unreadable config; a template that cannot be rendered now
    says why instead."""
    if not config:
        return None
    msg = message_of(config["action"])
    if "template_id" not in msg:
        return None
    from . import template_send, template_vars
    from . import templates as tstore

    try:
        t = tstore.get_template(msg["template_id"])
        names = [f["name"] for f in t["fields"] if f["kind"] == "secret"]
        return template_send.render(
            t,
            msg["values"],
            library=template_vars.values(),
            secrets={n: template_send.mask(n) for n in names},
            secret_state=dict.fromkeys(names, "ok"),
        ).masked
    except tstore.TemplateNotFound:
        return "(the template no longer exists)"
    except (tstore.TemplateError, template_send.SendRefused) as e:
        return f"(it can't be rendered right now: {getattr(e, 'detail', None) or e})"


def describe(scope: dict, preview: str | None = None) -> list[str]:
    """The scope in the operator's words — the consent dialog's lines, server-authored.

    **It discloses every field `scope_digest` covers** (#1201 PR B review): the consent dialog says
    "exactly this scope", so two scopes with different digests must never read the same. Pinned by
    `test_describe_discloses_every_field_the_digest_covers`, which mutates each field in turn — a
    new scope field that is not described here fails it."""
    lines: list[str] = []
    t = scope.get("target") or {}
    action = scope.get("action")
    mission = action == "start_mission"
    if mission:
        lines.append(f"Starts a mission in project {t.get('project_id')}, in {t.get('cwd')}")
    elif action == "start_session":
        where = t.get("folder")
        if t.get("cwd") and t.get("cwd") != where:
            where = f"{where} (resolves to {t['cwd']})"
        lines.append(f"Starts a {scope.get('engine')} session in {where}")
    elif action == "send_to_session":
        lines.append(f"Types into the live session {t.get('session_key')}")
        lines.append("If that session isn't running, the run is refused; nothing is started")
    else:
        lines.append(f"Action: {action} · target {json.dumps(t, sort_keys=True)}")
    if mission and not scope.get("engine"):
        lines.append("The agent is chosen by the mission planner")
    elif action != "start_session" and scope.get("engine"):
        lines.append(f"Agent: {scope['engine']}")
    if action == "start_session" or scope.get("model"):
        lines.append(f"Model: {scope.get('model') or 'the agent’s default'}")
    if scope.get("bypass"):
        lines.append("Permission bypass: ON — unattended, with tool prompts suppressed")
    elif mission:
        lines.append("Permission bypass: off (automated missions never bypass)")
    elif action == "start_session":
        lines.append("Permission bypass: off")
    else:
        lines.append("Permission bypass: unchanged — whatever that session already runs with")
    cid = t.get("checklist_id")
    if (
        scope.get("checklist_default")
        or cid
        or scope.get("checklist")
        or scope.get("checklist_name")
    ):
        name = scope.get("checklist_name")
        if scope.get("checklist_default"):
            label = f"{name} (your default)" if name else "none (you have no default)"
        elif cid == ":none":
            label = "none"
        elif name and cid and name != cid:
            label = f"{name} ({cid})"
        else:
            label = str(name or cid)
        lines.append(f"Checklist: {label}")
        if scope.get("checklist"):
            lines.append(f"Checklist revision: {scope['checklist']}")
    if scope.get("autonomy") is not None:
        lines.append(
            {
                "propose": "Autonomy: plans the mission and waits for you to dispatch it",
                "dispatch": "Autonomy: dispatches the plan without asking you",
                "dispatch_auto_choose": (
                    "Autonomy: dispatches the plan and may answer menus on its own"
                ),
            }.get(str(scope.get("autonomy")), f"Autonomy: {scope.get('autonomy')}")
        )
    lines.extend(_message_lines(scope, preview))
    trig = scope.get("trigger")
    if trig in ("schedule", "once"):
        lines.append(_cadence_words(scope.get("cadence"), scope.get("tz")))
    elif trig == "manual":
        lines.append("Only when you press Run now")
    else:
        lines.append(f"Trigger: {trig}")
    if trig == "manual" and (scope.get("cadence") or scope.get("tz")):
        lines.append(f"Schedule fields: {scope.get('cadence')!r} ({scope.get('tz')})")
    rpw = scope.get("runs_per_week") or 0
    if rpw:
        lines.append(f"About {rpw:g} runs a week")
    lines.append(f"At most {scope.get('max_runs_per_day')} runs a day")
    if scope.get("concurrency") == "allow":
        lines.append(
            f"If a run is still going: starts anyway, up to {scope.get('max_concurrent')} at once"
        )
    elif scope.get("concurrency") != "skip":
        lines.append(
            f"If a run is still going: {scope.get('concurrency')!r}, "
            f"at most {scope.get('max_concurrent')} at once"
        )
    else:
        lines.append(
            "If a run is still going: skips that slot and records why"
            + (
                ""
                if scope.get("max_concurrent") == 1
                else f" (at most {scope.get('max_concurrent')} at once)"
            )
        )
    lines.append(f"Pauses after {scope.get('pause_after_failures')} failures in a row")
    exp = scope.get("expires_at")
    lines.append(
        "Stops on " + _local_time(float(exp), scope.get("tz"))
        if isinstance(exp, int | float) and not isinstance(exp, bool)
        else ("Runs until you turn it off" if exp is None else f"Stops at {exp!r}")
    )
    if scope.get("webhook"):
        lines.append("A webhook can start it")
    if scope.get("mapped"):
        lines.append(f"A webhook can fill: {', '.join(map(str, scope['mapped']))}")
    return lines
